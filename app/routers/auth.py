"""Аутентификация, профиль и сброс пароля.

Троттлинг этого контура живёт в модуле, а не в `app/deps.py`: окно обращений
там уже есть, но считает оно бюджет ИИ-вызовов на авторизованного
пользователя, а сюда нужно ограничивать анонимные попытки входа, регистрации
и восстановления. Класс окна переиспользован, порог и ключ — свои.
"""
import logging
import os
import secrets
from datetime import timedelta

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Request,
                     status)
from fastapi_mail import MessageSchema
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import FRONTEND_URL
from app.db import get_db
from app.deps import _SlidingWindow, get_current_user
from app.mailer import send_email
from app.models import PasswordResetToken, User
from app.schemas import (
    LoginRequest, PasswordReset, PasswordResetRequest, PasswordResetResponse,
    ProfileResponse, ProfileUpdate, RegisterRequest, Token, UserResponse,
    normalize_email,
)
from app.security import create_access_token, get_password_hash, verify_password
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

router = APIRouter()

# Счётчик недоставленных писем сброса пароля. Он здесь, а не в ответе
# клиента, потому что ответ не имеет права признавать сбой доставки:
# письмо уходит только существующему адресу, и честное «письмо не дошло»
# стало бы оракулом «такой аккаунт есть». Видимость сбоя — уровень лога
# (SMTP_DELIVERY_FAILED в app/mailer.py) и это число, которое читает
# диагностика и тест.
_mail_delivery_failures = 0


def password_reset_mail_failures() -> int:
    """Сколько раз письмо сброса пароля не ушло (пока процесс живёт)."""
    return _mail_delivery_failures


def _limit(name: str, default: int) -> int:
    """Порог троттлинга из окружения: значения стенда и прод-набора разные.

    Молча подставить дефолт при мусорном значении — значит получить «лимит
    вдруг стал другим»; об этом пишет warning, а не падение импорта: auth-контур
    не должен уносить приложение из-за опечатки в `.env`.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Переменная %s не читается как целое: берётся %s", name, default)
        return default
    return value if value > 0 else default


# Окна считают попытки (и удачные, и нет) за час: заводить счётчик только на
# неудачи означало бы проверять пароль до того, как узнаем результат, а для
# перебора разница ровно в один запрос.
_LOGIN_PER_IP_EMAIL = _SlidingWindow(_limit("AUTH_LOGIN_PER_IP_EMAIL_MAX", 10))
# Отдельное окно на адрес: оно и есть локаут — сколько бы источников атака
# ни сменила, по чистой почте попытки упираются в порог. Плата за это —
# возможный отказ в обслуживании: злоумышленник, знающий адрес, способен
# охладить его вход. Порог потому и выше, чем у пары ip+почта, что бить должен
# перебор паролей, а не сотрудник, который за час входит с нескольких сетей.
_LOGIN_PER_EMAIL = _SlidingWindow(_limit("AUTH_LOGIN_PER_EMAIL_MAX", 30))
_LOGIN_PER_IP = _SlidingWindow(_limit("AUTH_LOGIN_PER_IP_MAX", 200))
_REGISTER_PER_IP = _SlidingWindow(_limit("AUTH_REGISTER_PER_IP_MAX", 50))
_RESET_PER_EMAIL = _SlidingWindow(_limit("AUTH_RESET_PER_EMAIL_MAX", 5))
_RESET_PER_IP = _SlidingWindow(_limit("AUTH_RESET_PER_IP_MAX", 100))

_AUTH_WINDOWS = (_LOGIN_PER_IP_EMAIL, _LOGIN_PER_EMAIL, _LOGIN_PER_IP,
                 _REGISTER_PER_IP, _RESET_PER_EMAIL, _RESET_PER_IP)


def reset_auth_rate_limits() -> None:
    """Тестовый хук: забыть историю попыток (см. `_SlidingWindow` в app/deps.py).

    Нужен не только тестам: счётчик живёт в памяти процесса, поэтому
    перезапуск или вторая реплика обнуляют его полностью, а раздача за
    несколькими прокси-адресами делает окно по ip мягче заявленного. Общий
    счётчик даёт только внешний кэш (план в
    docs/plans/redis-cache-and-task-routing.md) — до него этот долг известен
    и описан здесь, а не промолчан.
    """
    for window in _AUTH_WINDOWS:
        window.reset()


def _throttle(window: _SlidingWindow, key: str, detail: str) -> None:
    """Одно обращение к окну с понятным auth-маршруту текстом.

    `acquire` поднимает 429 с формулировкой про бюджет ИИ — её переписываем,
    `Retry-After` оставляем как есть.
    """
    try:
        window.acquire(key)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=detail,
            headers=exc.headers,
        ) from exc


def _client_ip(request: Request) -> str:
    """Адрес источника из транспорта.

    Заголовок X-Forwarded-For не читается намеренно: доверять ему без
    доверенного прокси — способ обойти окно любой строкой в заголовке.
    Разворот на сторону (список доверенных прокси и первый адрес из цепочки)
    делается вместе с внешним счётчиком.
    """
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"


def _token_version(user: User) -> int:
    """Клеймо версии токена (см. `User.token_version` в app/models.py)."""
    return int(getattr(user, "token_version", 0) or 0)


def _revoke_sessions(user: User) -> None:
    """Отзывает все выпущенные токены: `app/deps.py` сверяет `tv` с этим числом."""
    user.token_version = _token_version(user) + 1


def _issue_token(user: User) -> str:
    """Токен с актуальными `sub` и версией: почта в `sub` — опора резолва в deps."""
    return create_access_token(data={"sub": user.email, "tv": _token_version(user)})


def _find_user_by_email(db: Session, email: str) -> User | None:
    """Аккаунт по нормализованной почте, с оглядкой на легаси-регистр.

    Нормализация на записи появилась позже данных: в базе лежат адреса с
    заглавными буквами, и сравнение `User.email == 'ivan@…'` на PostgreSQL
    такого владельца не находит — сброс пароля отвечал успехом, письма не
    было. `lower()` покрывает старые и новые строки сразу, без миграции.

    Порядок по id выбирает дубликат детерминированно: проверка при записи новых
    «регистровых» копий уже не заводит, но переписывать найденное ранее без
    миграции никто не разрешал.
    """
    normalized = normalize_email(email)
    return (db.query(User)
              .filter(func.lower(User.email) == normalized)
              .order_by(User.id)
              .first())


@router.post("/api/register", response_model=UserResponse)
async def register(request: RegisterRequest, http_request: Request,
                   db: Session = Depends(get_db)):
    _throttle(_REGISTER_PER_IP, _client_ip(http_request),
              "Слишком много регистраций с этого адреса. Повторите позже.")

    normalized_email = normalize_email(request.email)
    if _find_user_by_email(db, normalized_email) is not None:
        raise HTTPException(400, detail="Email уже зарегистрирован")

    hashed_password = get_password_hash(request.password)
    db_user = User(
        name=request.name,
        email=normalized_email,
        hashed_password=hashed_password
    )
    db.add(db_user)
    try:
        db.commit()
    except IntegrityError:
        # Гонка двух заявок на одну почту: уникальность колонки всё ещё
        # последний аргумент, и она обязана звучать как ответ клиента, а не 500.
        db.rollback()
        logger.warning("Регистрация упёрлась в уникальность почты: аккаунт уже создан")
        raise HTTPException(400, detail="Email уже зарегистрирован") from None
    db.refresh(db_user)
    return db_user


@router.post("/api/login", response_model=Token)
async def login(request: LoginRequest, http_request: Request,
                db: Session = Depends(get_db)):
    ip = _client_ip(http_request)
    email = normalize_email(request.email)
    _throttle(_LOGIN_PER_IP_EMAIL, f"{ip}|{email}",
              "Слишком много попыток входа. Повторите позже.")
    _throttle(_LOGIN_PER_EMAIL, email,
              "Вход по этому адресу временно заблокирован. Повторите позже.")
    _throttle(_LOGIN_PER_IP, ip,
              "Слишком много попыток входа с этого адреса. Повторите позже.")

    user = _find_user_by_email(db, email)
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверные учетные данные")

    return {"access_token": _issue_token(user), "token_type": "bearer"}


@router.get("/api/me", response_model=UserResponse)
async def get_current_user_endpoint(current_user: User = Depends(get_current_user)):
    return current_user


# Эндпоинт для обновления профиля
@router.put("/api/profile", response_model=ProfileResponse)
def update_profile(
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Правит только переданные поля; смена почты или пароля вращает сессию.

    Контракт по токену: `sub` — это почта, а `tv` — версия, поэтому после
    такой правки прежний токен отваливается сам (см. `app/deps.py`). Маршрут
    отвечает новый токен в `access_token` с флагом `token_rotated`: клиент,
    который его сохранит, продолжает работу, а проигнорировавший получит
    честный 401 и форму входа, а не 500 на пустом месте.
    """
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(404, detail="User not found")

    # exclude_unset — «что прислали, то и правим»; None дополнительно
    # отбрасывается: сбивать поле в пустоту можно значением "" (так делает
    # frontend), а пропущенное или null не должно стирать почту и имя.
    updates = {key: value for key, value in payload.model_dump(exclude_unset=True).items()
               if value is not None}

    email_changed = False
    password_changed = False

    if "name" in updates:
        user.name = updates["name"]
    if "email" in updates:
        new_email = normalize_email(updates["email"])
        if new_email != normalize_email(user.email):
            owner = _find_user_by_email(db, new_email)
            if owner is not None and owner.id != user.id:
                raise HTTPException(400, detail="Эта почта уже занята другим аккаунтом")
            user.email = new_email
            email_changed = True
    if "position" in updates:
        user.position = updates["position"]
    if "company" in updates:
        user.company = updates["company"]
    if "website" in updates:
        user.website = updates["website"]
    if "about" in updates:
        user.about = updates["about"]
    if updates.get("password"):
        user.hashed_password = get_password_hash(updates["password"])
        _revoke_sessions(user)
        password_changed = True

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Проверка уникальности выше не атомарная: между ней и записью почту
        # мог занять другой. Ответ обязан остаться валидацией, а не 500.
        logger.warning("Обновление профиля упёрлось в уникальность почты")
        raise HTTPException(400, detail="Эта почта уже занята другим аккаунтом") from None

    db.refresh(user)
    rotated = email_changed or password_changed
    body = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "position": user.position,
        "company": user.company,
        "website": user.website,
        "about": user.about,
        "token_rotated": rotated,
    }
    if rotated:
        body["access_token"] = _issue_token(user)
        body["token_type"] = "bearer"
    return body


# Эндпоинт для запроса сброса пароля
@router.post("/api/reset-password-request", response_model=PasswordResetResponse)
async def request_password_reset(
    request: PasswordResetRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    ip = _client_ip(http_request)
    email = normalize_email(request.email)
    _throttle(_RESET_PER_EMAIL, email,
              "Ссылка на восстановление уже отправлялась. Повторите позже.")
    _throttle(_RESET_PER_IP, ip,
              "Слишком много запросов восстановления с этого адреса. Повторите позже.")

    try:
        # Проверяем, существует ли пользователь с таким email
        user = _find_user_by_email(db, email)

        if not user:
            # Возвращаем успех даже если пользователь не найден (безопасность)
            return PasswordResetResponse(
                success=True,
                message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
            )

        # Удаляем предыдущие неиспользованные токены для этого email
        db.query(PasswordResetToken).filter(
            func.lower(PasswordResetToken.email) == email,
            PasswordResetToken.used.is_(False)
        ).delete()

        # Генерируем новый токен
        token = secrets.token_urlsafe(32)
        reset_token = PasswordResetToken(
            email=email,
            token=token,
            expires_at=utc_now() + timedelta(hours=1)
        )
        db.add(reset_token)
        db.commit()

        # Формируем ссылку для сброса пароля
        reset_link = f"{FRONTEND_URL}/reset-password?token={token}"

        # Создаем email сообщение
        message = MessageSchema(
            subject="Восстановление пароля - ВкусВилл BPMN",
            recipients=[email],
            html=f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #00A550 0%, #F62369 100%); padding: 30px; text-align: center; border-radius: 10px 10px 0 0;">
                    <h1 style="color: white; margin: 0; font-size: 28px;">ВкусВилл BPMN</h1>
                </div>
                <div style="background: white; padding: 40px; border-radius: 0 0 10px 10px; box-shadow: 0 5px 20px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; margin-top: 0;">Восстановление пароля</h2>
                    <p style="color: #666; line-height: 1.6;">
                        Здравствуйте!<br><br>
                        Вы получили это письмо, потому что был запрошен сброс пароля для вашей учетной записи в ВкусВилл BPMN.<br><br>
                        Для сброса пароля нажмите на кнопку ниже:
                    </p>
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{reset_link}" style="
                            background: linear-gradient(135deg, #00A550 0%, #F62369 100%);
                            color: white;
                            padding: 15px 30px;
                            text-decoration: none;
                            border-radius: 50px;
                            font-weight: 600;
                            display: inline-block;
                        ">Сбросить пароль</a>
                    </div>
                    <p style="color: #666; line-height: 1.6;">
                        Или скопируйте и вставьте следующую ссылку в браузер:<br>
                        <a href="{reset_link}" style="color: #00A550;">{reset_link}</a>
                    </p>
                    <p style="color: #666; line-height: 1.6;">
                        Эта ссылка действительна в течение 1 часа.<br><br>
                        Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.<br><br>
                        С уважением,<br>
                        Команда ВкусВилл BPMN
                    </p>
                </div>
            </div>
            """,
            subtype="html"
        )

        # Отправляем email в фоне
        background_tasks.add_task(_deliver_reset_letter, message)

        return PasswordResetResponse(
            success=True,
            message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
        )

    except HTTPException:
        # 429 от троттлинга не имеет права превращаться в 500 общим обработчиком.
        raise
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset request: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при отправке запроса на восстановление пароля"
        )


async def _deliver_reset_letter(message: MessageSchema) -> None:
    """Фон после ответа: считает недоставленные письма, ронять ответ не вправе.

    Поле в ответе вместо этого быть не может: письмо назначается только
    существующему адресу, и честный флаг доставки сообщил бы тому, кто перебирает
    почты, что аккаунт зарегистрирован.
    """
    global _mail_delivery_failures
    if not await send_email(message):
        _mail_delivery_failures += 1
        logger.error(
            "Письмо сброса пароля не доставлено (счётчик %s): клиент уже получил "
            "успешный ответ, поэтому ссылка из него не сработает",
            _mail_delivery_failures,
        )


# Эндпоинт для сброса пароля
@router.post("/api/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    request: PasswordReset,
    db: Session = Depends(get_db)
):
    try:
        # Ищем токен в базе данных
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == request.token,
            PasswordResetToken.used.is_(False),
            PasswordResetToken.expires_at > utc_now()
        ).first()

        if not reset_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Недействительный или просроченный токен сброса пароля"
            )

        # Находим пользователя по email из токена
        user = _find_user_by_email(db, reset_token.email)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Пользователь не найден"
            )

        # Валидация нового пароля
        if len(request.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пароль должен содержать минимум 8 символов"
            )

        # Обновляем пароль пользователя
        user.hashed_password = get_password_hash(request.new_password)
        # Сброс пароля обязан отзывать выпущенные токены так же, как и смена
        # через профиль: иначе украденная сессия переживает восстановление.
        _revoke_sessions(user)

        # Помечаем токен как использованный
        reset_token.used = True

        db.commit()

        return PasswordResetResponse(
            success=True,
            message="Пароль успешно изменен"
        )

    except HTTPException:
        raise
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при сбросе пароля"
        )

# Эндпоинт для проверки токена
@router.post("/api/verify-reset-token")
async def verify_reset_token(
    token: str,
    db: Session = Depends(get_db)
):
    """Жива ли ссылка восстановления.

    Контракт frontend не меняется никак: ответ всегда 200 с телом ровно
    `{"valid": …}` — `bpmn-constructor/src/api/account.js` передаёт токен
    query-параметром, `context/AuthContext.js` читает только `valid`, а на
    любой не-2xx ловит исключение и показывает то же «ссылка недействительна».
    Значит, 500 здесь ничего бы человеку не объяснил, а лишнее поле в теле
    сломало бы запертый тест `tests/test_routes_smoke.py`. Поэтому класс
    отказа — в логе: «не тот токен» и «база лежит» перестают быть одной строкой,
    и поддержка отличает просроченную ссылку от сбоя хранилища, не трогая
    ответы API.

    Query-параметр остаётся: ссылка приходит письмом, и в заголовок её не
    перенести без правки frontend; токены одноразовые и живут час.
    """
    if not token:
        logger.warning("Проверка токена сброса: параметр пуст")
        return {"valid": False}

    try:
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == token
        ).first()

        if not reset_token:
            logger.warning("Проверка токена сброса: токен не найден")
            return {"valid": False}

        if reset_token.used:
            logger.warning("Проверка токена сброса: ссылка уже использована")
            return {"valid": False}

        if reset_token.expires_at is None or reset_token.expires_at <= utc_now():
            # Просроченный и «без срока» (такое возможно в легаси-строке) для
            # приёмки одинаково негодны, но путать их сбой хранилища нельзя.
            logger.warning("Проверка токена сброса: срок ссылки истёк")
            return {"valid": False}

        return {"valid": True}

    except Exception as e:
        # Единственный случай, где прежний код врал: сбой хранилища
        # выдавался за «недействительная ссылка». Ответ остаётся прежним
        # (контракт выше), но в логе это теперь ошибка уровня error.
        logger.error("Проверка токена сброса упала (%s): %s",
                     type(e).__name__, e, exc_info=True)
        return {"valid": False}

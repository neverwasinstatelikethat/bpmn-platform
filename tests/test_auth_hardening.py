"""Auth-контур: срок токена, профиль, нормализация почты, доставка писем, троттлинг.

Каждый тест заперт по найденному дефекту, а не по форме кода: до правок
живущая сессия получала 15 минут вместо настройки, `PUT /api/profile` писал
«сырой» dict прямо в сессию (отсюда 500 на чужой почте и на числе вместо
имени), почта сравнивалась без регистра, сбой SMTP тонул в логе при ответе
«письмо отправлено», а вход и восстановление не имели никакого лимита.
"""
import logging
import uuid
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import func

pytest.importorskip("httpx", reason="TestClient работает поверх httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PasswordResetToken, User  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.schemas import MIN_PASSWORD_CHARS, USER_NAME_MAX  # noqa: E402
from app.security import create_access_token, get_password_hash  # noqa: E402
from app.timeutils import utc_now  # noqa: E402

PASSWORD = "Passw0rd!"


def new_email(label: str) -> str:
    return f"auth-{label}-{uuid.uuid4().hex[:8]}@example.com"


def register(client, email, password=PASSWORD, name="Тест") -> int:
    response = client.post("/api/register",
                           json={"name": name, "email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def login(client, email, password=PASSWORD) -> str:
    response = client.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def payload_of(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                      options={"verify_exp": False})


def reset_link_token(db, email: str) -> str:
    """Свежий токен сброса из базы: проверка письма в тесте — это наличие строки."""
    row = (db.query(PasswordResetToken)
             .filter(func.lower(PasswordResetToken.email) == email.lower())
             .order_by(PasswordResetToken.created_at.desc())
             .first())
    assert row is not None, "токен сброса не создан"
    return row.token


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def other_client():
    """Второй источник: окно по ip не должно схлопываться в глобальное.

    Адрес из диапазона документационных примеров, чтобы он не совпал с чьим-то
    живым хостом в общем счётчике процесса.
    """
    with TestClient(app, client=("203.0.113.77", 40077)) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_windows():
    """Счётчик живёт в памяти процесса: тесты не наследуют попытки друг друга.

    Именно это обнуление и демонстрирует известный долг — после перезапуска
    или во второй реплике блокировка исчезает сама (см. docstring
    `reset_auth_rate_limits`).
    """
    auth_router.reset_auth_rate_limits()
    yield
    auth_router.reset_auth_rate_limits()


class TestTokenLifetime:
    def test_login_token_expires_by_config(self, client):
        """Срок токена = ACCESS_TOKEN_EXPIRE_MINUTES, а не захардкоженные 15 минут."""
        email = new_email("lifetime")
        register(client, email)
        claims = payload_of(login(client, email))
        assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_EXPIRE_MINUTES * 60

    def test_explicit_delta_still_wins(self):
        """Явный expires_delta остаётся способом выдать короткий токен."""
        claims = payload_of(create_access_token({"sub": "x"},
                                                expires_delta=timedelta(minutes=2)))
        assert claims["exp"] - claims["iat"] == 120

    def test_short_fallback_only_without_setting(self, monkeypatch):
        """Короткий дефолт — подстраховка на случай обнулённой настройки, не норма."""
        monkeypatch.setattr("app.security.ACCESS_TOKEN_EXPIRE_MINUTES", 0)
        claims = payload_of(create_access_token({"sub": "x"}))
        assert claims["exp"] - claims["iat"] == 15 * 60

    def test_token_carries_session_version(self, client, db):
        """Клеймо `tv` в токене = версия из аккаунта (контракт с `app/deps.py`)."""
        email = new_email("claim")
        user_id = register(client, email)
        token_version = db.get(User, user_id).token_version
        assert payload_of(login(client, email))["tv"] == token_version


class TestProfileUpdate:
    def test_taken_email_answers_400_not_500(self, client):
        first, second = new_email("p1"), new_email("p2")
        register(client, first)
        register(client, second)
        headers = bearer(login(client, second))

        response = client.put("/api/profile", headers=headers, json={"email": first})
        assert response.status_code == 400, response.text
        assert "занят" in response.json()["detail"]

    def test_field_bounds_reject_before_database(self, client):
        """Число вместо имени и 400 символов — 422: на PostgreSQL это была 500."""
        email = new_email("bounds")
        register(client, email)
        headers = bearer(login(client, email))

        assert client.put("/api/profile", headers=headers,
                          json={"name": 12345}).status_code == 422
        assert client.put("/api/profile", headers=headers,
                          json={"name": "Д" * (USER_NAME_MAX + 1)}).status_code == 422
        assert client.put("/api/profile", headers=headers,
                          json={"position": "п" * 101}).status_code == 422
        assert client.put("/api/profile", headers=headers,
                          json={"email": "не-почта"}).status_code == 422

    def test_weak_password_rejected_by_same_rule_as_reset(self, client):
        email = new_email("weak")
        register(client, email)
        headers = bearer(login(client, email))

        assert client.put("/api/profile", headers=headers,
                          json={"password": "kr4tko"}).status_code == 422
        long_enough = "N3wPassw0rd!"
        assert len(long_enough) >= MIN_PASSWORD_CHARS
        assert client.put("/api/profile", headers=headers,
                          json={"password": long_enough}).status_code == 200

    def test_email_change_hands_back_a_working_token(self, client, db):
        """Смена почты ломает прежний токен (в нём `sub`), поэтому ответ несёт новый.

        Это и есть контракт: проигнорировавший его клиент получит честный 401,
        сохранивший — продолжает работу без перелогина.
        """
        email, replacement = new_email("old"), new_email("new")
        user_id = register(client, email)
        old_token = login(client, email)

        response = client.put("/api/profile", headers=bearer(old_token),
                              json={"email": replacement})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email"] == replacement
        assert body["token_rotated"] is True
        assert body["token_type"] == "bearer"

        # Прежний токен мёртв: он указывает на почту, которой больше нет.
        assert client.get("/api/me", headers=bearer(old_token)).status_code == 401
        me = client.get("/api/me", headers=bearer(body["access_token"]))
        assert me.status_code == 200, me.text
        assert me.json()["email"] == replacement
        assert db.get(User, user_id).email == replacement

    def test_password_change_revokes_issued_tokens(self, client):
        """Смена пароля через профиль отзывает выпущенные сессии ровно как сброс."""
        email = new_email("rotate")
        register(client, email)
        old_token = login(client, email)

        response = client.put("/api/profile", headers=bearer(old_token),
                              json={"password": "N3wPassw0rd!"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_rotated"] is True
        assert client.get("/api/me", headers=bearer(old_token)).status_code == 401
        assert client.get("/api/me", headers=bearer(body["access_token"])).status_code == 200
        assert client.post("/api/login", json={"email": email,
                                              "password": "N3wPassw0rd!"}).status_code == 200

    def test_password_change_bumps_session_version(self, client, db):
        email = new_email("bump")
        user_id = register(client, email)
        before = db.get(User, user_id).token_version
        headers = bearer(login(client, email))

        assert client.put("/api/profile", headers=headers,
                          json={"password": "N3wPassw0rd!"}).status_code == 200
        db.expire_all()
        after = db.get(User, user_id).token_version
        assert after == before + 1
        assert payload_of(login(client, email, "N3wPassw0rd!"))["tv"] == after

    def test_untouched_fields_survive_and_nulls_do_not_erase(self, client):
        """Прислано только имя — остальное целое; null не стирает почту."""
        email = new_email("partial")
        user_id = register(client, email)
        headers = bearer(login(client, email))
        assert client.put("/api/profile", headers=headers,
                          json={"position": "аналитик", "company": "ВкусВилл"}).status_code == 200

        response = client.put("/api/profile", headers=headers,
                              json={"name": "Новое имя", "email": None, "about": None,
                                    "id": 999})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Новое имя"
        assert body["email"] == email
        assert body["position"] == "аналитик"
        assert body["id"] == user_id

    def test_profile_update_without_email_change_keeps_token(self, client):
        """Без смены почты и пароля токен не вращается: клиент не теряет сессию."""
        email = new_email("steady")
        register(client, email)
        headers = bearer(login(client, email))

        body = client.put("/api/profile", headers=headers,
                          json={"website": "https://example.com"}).json()
        assert body["token_rotated"] is False
        assert body["access_token"] is None
        assert client.get("/api/me", headers=headers).status_code == 200


class TestEmailNormalization:
    def test_garbage_address_is_rejected_at_the_door(self, client):
        """`not-an-email` больше не аккаунт: формат проверяется до записи в базу."""
        assert client.post("/api/register", json={"name": "Мусор",
                                                  "email": "not-an-email",
                                                  "password": PASSWORD}).status_code == 422
        assert client.post("/api/login", json={"email": "not-an-email",
                                               "password": PASSWORD}).status_code == 422

    def test_registration_stores_one_canonical_address(self, client, db):
        mixed = f"Auth-Mixed{uuid.uuid4().hex[:6]}@Example.COM"
        register(client, mixed)
        stored = (db.query(User)
                    .filter(func.lower(User.email) == mixed.lower())
                    .first())
        assert stored.email == mixed.lower()

    def test_case_variants_are_one_account_not_two(self, client):
        email = new_email("dup")
        register(client, email)
        assert client.post("/api/register", json={"name": "Дубль",
                                                  "email": email.upper(),
                                                  "password": PASSWORD}).status_code == 400

    def test_signed_up_with_capital_letters_logs_in_and_gets_reset(self, client, db):
        """Зарегистрированный с заглавной логином входит и получает письмо.

        Баг был в том, что запись сохранялась как попало, а сброс lower-case'ил
        адрес: на PostgreSQL владелец не находился, и приходил `success: true`
        без единственного токена.
        """
        raw = f"Auth-Caps{uuid.uuid4().hex[:6]}@Example.COM"
        assert client.post("/api/register", json={"name": "Заглавный",
                                                  "email": raw,
                                                  "password": PASSWORD}).status_code == 200
        assert client.post("/api/login", json={"email": raw.upper(),
                                               "password": PASSWORD}).status_code == 200
        assert client.post("/api/reset-password-request",
                           json={"email": raw.upper()}).status_code == 200
        assert reset_link_token(db, raw), "сброс не создал токен"

    def test_legacy_row_with_capital_email_is_reachable(self, client, db):
        """Аккаунт, заведённый до нормализации, всё ещё находится при сбросе.

        Миграции нет, поэтому чтение идёт через `lower()` по колонке: без этого
        такой пользователь получал «успех» и не получал ссылку.
        """
        legacy = f"Auth-Legacy{uuid.uuid4().hex[:6]}@Example.COM"
        db.add(User(name="Легаси", email=legacy,
                    hashed_password=get_password_hash(PASSWORD)))
        db.commit()

        assert client.post("/api/reset-password-request",
                           json={"email": legacy.lower()}).status_code == 200
        token = reset_link_token(db, legacy)
        assert client.post("/api/reset-password",
                           json={"token": token,
                                 "new_password": "N3wPassw0rd!"}).status_code == 200
        assert client.post("/api/login", json={"email": legacy.upper(),
                                               "password": "N3wPassw0rd!"}).status_code == 200

    def test_reset_revokes_sessions_of_the_account(self, client):
        """Сброс пароля не оставляет в силе токены, выпущенные до него."""
        email = new_email("resetrev")
        register(client, email)
        token = login(client, email)
        reset_link = client.post("/api/reset-password-request", json={"email": email})
        assert reset_link.status_code == 200
        db_token = _token_for_email(email)

        assert client.post("/api/reset-password",
                           json={"token": db_token,
                                 "new_password": "N3wPassw0rd!"}).status_code == 200
        assert client.get("/api/me", headers=bearer(token)).status_code == 401


def _token_for_email(email: str) -> str:
    session = SessionLocal()
    try:
        row = (session.query(PasswordResetToken)
                      .filter(func.lower(PasswordResetToken.email) == email.lower())
                      .order_by(PasswordResetToken.created_at.desc())
                      .first())
        assert row is not None
        return row.token
    finally:
        session.close()


class TestMailDeliveryVisibility:
    def test_dead_smtp_answers_success_but_is_visible(self, client, caplog):
        """Ответ остаётся нейтральным (нет оракула «такой адрес есть»), сбой — виден.

        Настройки теста указывают почту на 127.0.0.1:1, поэтому доставка
        гарантированно падает: раньше об этом знала только одна строка лога, и
        она же не была ни во что посчитана.
        """
        email = new_email("smtp")
        register(client, email)
        before = auth_router.password_reset_mail_failures()

        with caplog.at_level(logging.ERROR, logger="app.mailer"):
            response = client.post("/api/reset-password-request", json={"email": email})

        assert response.status_code == 200, response.text
        assert response.json()["success"] is True
        assert auth_router.password_reset_mail_failures() == before + 1
        assert "SMTP_DELIVERY_FAILED" in caplog.text
        assert "письмо сброса пароля не доставлено" in caplog.text.lower()

    def test_successful_delivery_does_not_grow_the_counter(self, client, monkeypatch):
        email = new_email("okmail")
        register(client, email)
        before = auth_router.password_reset_mail_failures()

        async def delivered(message):
            return True

        async def lost(message):
            # Так же, как настоящий `send_email`: сбой закрыт внутри, наружу
            # уходит False, а не исключение из фонового воркера.
            return False

        monkeypatch.setattr(auth_router, "send_email", delivered)
        assert client.post("/api/reset-password-request",
                           json={"email": email}).status_code == 200
        assert auth_router.password_reset_mail_failures() == before

        monkeypatch.setattr(auth_router, "send_email", lost)
        assert client.post("/api/reset-password-request",
                           json={"email": email}).status_code == 200
        assert auth_router.password_reset_mail_failures() == before + 1

    def test_mailer_reports_delivery_result(self, monkeypatch):
        """`send_email` возвращает результат, а не молча глотает исключение."""
        import asyncio

        from fastapi_mail import MessageSchema

        from app import mailer

        message = MessageSchema(subject="t", recipients=["a@example.com"],
                                body="body", subtype="plain")

        async def send_ok(*args, **kwargs):
            return None

        async def send_fail(*args, **kwargs):
            raise RuntimeError("отказ соединения")

        monkeypatch.setattr(mailer.fast_mail, "send_message", send_ok)
        assert asyncio.run(mailer.send_email(message)) is True

        monkeypatch.setattr(mailer.fast_mail, "send_message", send_fail)
        assert asyncio.run(mailer.send_email(message)) is False


class TestThrottling:
    @staticmethod
    def hammer(client, email, times=12, password="wrong"):
        """Серия попыток входа до срабатывания окна.

        Запас по попыткам такой, чтобы тест не зависел от конкретного порога:
        он проверяет факт троттлинга, а не совпадение числа с дефолтом конфига.
        """
        return [client.post("/api/login",
                            json={"email": email, "password": password}).status_code
                for _ in range(times)]

    def test_repeated_wrong_password_hits_429(self, client):
        codes = self.hammer(client, new_email("brute"))
        assert codes[0] == 401
        assert 429 in codes, "перебор не ограничен ничем"
        blocked = codes.index(429)
        # Несколько попыток легальны (опечатка в пароле не должна запирать
        # вход), но дальше окно держит блокировку до конца часа.
        assert blocked >= 2
        assert codes[blocked:] == [429] * (len(codes) - blocked)
        assert codes[blocked - 1] == 401

    def test_attack_on_one_account_locks_it_and_spares_neighbours(self, client):
        """Ключ окна — адрес и почта вместе, а локаут не выпускает и с верным паролем.

        Последнее и есть смысл блокировки: иначе перебор просто чередовал бы
        401 с удачным входом на той же учётке.
        """
        victim, neighbour = new_email("v"), new_email("n")
        register(client, victim)
        register(client, neighbour)
        assert 429 in self.hammer(client, victim)
        assert client.post("/api/login", json={"email": victim,
                                               "password": PASSWORD}).status_code == 429
        assert client.post("/api/login", json={"email": neighbour,
                                               "password": PASSWORD}).status_code == 200

    def test_account_lockout_survives_a_change_of_source(self, client, other_client,
                                                         monkeypatch):
        """Локаут по почте: смена источника перебором не обходит окно.

        Порог занижен в тесте, а не в проде: окно по одной почте стоит выше,
        чем окно пары ip+почта, чтобы обычный вход с нескольких сетей не запирал
        аккаунт сам собой.
        """
        email = new_email("lockout")
        register(client, email)
        monkeypatch.setattr(auth_router._LOGIN_PER_EMAIL, "limit", 2)

        assert client.post("/api/login",
                           json={"email": email, "password": "wrong"}).status_code == 401
        assert other_client.post("/api/login",
                                 json={"email": email,
                                       "password": "wrong"}).status_code == 401
        blocked = client.post("/api/login", json={"email": email, "password": "wrong"})
        assert blocked.status_code == 429, blocked.text

    def test_registration_and_reset_are_throttled_too(self, client, monkeypatch):
        monkeypatch.setattr(auth_router._RESET_PER_EMAIL, "limit", 2)
        monkeypatch.setattr(auth_router._REGISTER_PER_IP, "limit", 2)

        email = new_email("throttle")
        assert client.post("/api/reset-password-request",
                           json={"email": email}).status_code == 200
        assert client.post("/api/reset-password-request",
                           json={"email": email}).status_code == 200
        blocked = client.post("/api/reset-password-request", json={"email": email})
        assert blocked.status_code == 429, blocked.text

        assert client.post("/api/register", json={"name": "Раз",
                                                  "email": new_email("r1"),
                                                  "password": PASSWORD}).status_code == 200
        assert client.post("/api/register", json={"name": "Два",
                                                  "email": new_email("r2"),
                                                  "password": PASSWORD}).status_code == 200
        assert client.post("/api/register", json={"name": "Три",
                                                  "email": new_email("r3"),
                                                  "password": PASSWORD}).status_code == 429

    def test_counter_lives_in_process_memory_only(self, client):
        """Известный долг: перезапуск процесса стирает блокировку вместе со счётчиком."""
        email = new_email("memory")
        assert 429 in self.hammer(client, email)

        auth_router.reset_auth_rate_limits()
        assert client.post("/api/login", json={"email": email,
                                               "password": "wrong"}).status_code == 401


class TestVerifyResetTokenContract:
    def test_body_stays_exactly_one_key(self, client):
        """Frontend читает `valid` и сверяет тело с этим: лишние поля в ответе
        сломали бы запертый смоук-тест, поэтому причины отказа живут в логе."""
        assert client.post("/api/verify-reset-token",
                           params={"token": ""}).json() == {"valid": False}
        assert client.post("/api/verify-reset-token",
                           params={"token": "no-such-token"}).json() == {"valid": False}

    def test_reasons_of_a_refusal_are_distinguishable_in_logs(self, client, db, caplog):
        email = new_email("reasons")
        register(client, email)
        assert client.post("/api/reset-password-request", json={"email": email}).status_code == 200
        token = reset_link_token(db, email)

        expired_token = f"expired-{uuid.uuid4().hex}"
        used_token = f"used-{uuid.uuid4().hex}"
        session = SessionLocal()
        try:
            session.add_all([
                PasswordResetToken(email=email, token=expired_token,
                                   expires_at=utc_now() - timedelta(minutes=1)),
                PasswordResetToken(email=email, token=used_token,
                                   expires_at=utc_now() + timedelta(hours=1), used=True),
            ])
            session.commit()
        finally:
            session.close()

        assert client.post("/api/verify-reset-token",
                           params={"token": token}).json() == {"valid": True}
        with caplog.at_level(logging.WARNING, logger="app.routers.auth"):
            assert client.post("/api/verify-reset-token",
                               params={"token": expired_token}).json() == {"valid": False}
            assert "срок ссылки истёк" in caplog.text
            caplog.clear()
            assert client.post("/api/verify-reset-token",
                               params={"token": used_token}).json() == {"valid": False}
            assert "уже использована" in caplog.text
            caplog.clear()
            assert client.post("/api/verify-reset-token",
                               params={"token": "no-such-token"}).json() == {"valid": False}
            assert "токен не найден" in caplog.text

    def test_storage_failure_is_logged_as_error_not_as_invalid_link(
            self, client, db, monkeypatch, caplog):
        """Прежний код выдавал сбой хранилища за «недействительную ссылку».

        Ответ по контракту не меняется, но в логе эти два случая теперь
        разведены: ошибка транспорта видна как ошибка, а не как поведение
        клиента с просроченной ссылкой.
        """
        email = new_email("boom")
        register(client, email)
        assert client.post("/api/reset-password-request", json={"email": email}).status_code == 200
        token = reset_link_token(db, email)

        def explode():
            raise RuntimeError("база недоступна")

        monkeypatch.setattr(auth_router, "utc_now", explode)
        with caplog.at_level(logging.ERROR, logger="app.routers.auth"):
            assert client.post("/api/verify-reset-token",
                               params={"token": token}).json() == {"valid": False}
        assert "Проверка токена сброса упала" in caplog.text


def test_access_token_from_login_resolves_after_restart_of_helper(client):
    """Токен, выпущенный маршрутом, читается тем же ключом и алгоритмом, что и deps."""
    email = new_email("roundtrip")
    register(client, email)
    token = login(client, email)
    claims = payload_of(token)
    assert claims["sub"] == email
    assert "tv" in claims
    assert client.get("/api/me", headers=bearer(token)).status_code == 200

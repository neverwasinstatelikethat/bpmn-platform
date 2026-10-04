"""Зависимости FastAPI: текущий пользователь по JWT, бюджет ИИ-маршрутов и
живость воркеров очереди."""
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import jwt
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import (AI_REQUESTS_PER_HOUR, ALGORITHM, REDIS_URL, SECRET_KEY)
from app.db import get_db
from app.models import User
from app.security import oauth2_scheme

logger = logging.getLogger(__name__)

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception

    # Клеймо `tv` инвалидирует токены, выпущенные до смены пароля
    # (см. User.token_version). Токен без `tv` считается версией 0 и
    # принимается, пока у пользователя token_version == 0: альтернатива
    # «отвергать без клейма» разлогинила бы все сессии одним деплоем этой
    # правки. После смены пароля версия ненулевая, и безклейменный токен
    # умирает вместе со старыми.
    token_version = payload.get("tv", 0)
    if not isinstance(token_version, int) or token_version != user.token_version:
        raise credentials_exception
    return user


_WINDOW_SECONDS = 3600.0

# Заголовок «когда вернуться». Один на троттлинг ИИ, на 503 недоступности
# провайдера и на список CORS: браузер отдаёт его axios-у только если он
# перечислен в `expose_headers`, а клиент по нему считает срок ожидания.
RETRY_AFTER_HEADER = "Retry-After"


def _too_many_requests(limit: int, wait_seconds: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(f"Превышен часовой лимит ИИ-запросов ({limit}). "
                "Повторите позже."),
        headers={RETRY_AFTER_HEADER: str(max(1, wait_seconds))},
    )


class _SlidingWindow:
    """Скользящее окно обращений на час внутри одного процесса.

    Счётчик локальный: при нескольких репликах каждая считает свою. Для
    почасового бюджета ИИ это означает лимит, умноженный на число реплик,
    поэтому при заданном `REDIS_URL` бюджет считается общим окном в Redis
    (`_RedisWindow`), а этот класс остаётся резервом на время его недоступности
    и единственным счётчиком для троттлинга auth-маршрутов, где цена ошибки —
    не деньги за токены.

    Участник окна — пара `(момент, метка)`, а не голое время: без метки слот
    нельзя вернуть (`release`), а два запроса в одну миллисекунду слились бы в
    один занявший слот.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._hits: Dict[str, Deque[Tuple[float, str]]] = defaultdict(deque)

    def _prune(self, now: float) -> None:
        # Ключи забытых пользователей копить нельзя: окно чистится по мере
        # обращения, а не по таймеру.
        if len(self._hits) <= 4096:
            return
        for key in [k for k, q in self._hits.items()
                    if not q or now - q[-1][0] >= _WINDOW_SECONDS]:
            del self._hits[key]

    def acquire(self, key: str, member: Optional[str] = None) -> str:
        """Регистрирует запрос; 429, если бюджет часа выбран.

        `member` — метка слота, по которой его вернут обратно (`release`). Не
        обязательна для троттлинга auth-маршрутов: там отказ возвращать нечего,
        слот живёт окно и списывается попыткой входа.
        """
        now = time.monotonic()
        self._prune(now)
        hits = self._hits[key]
        while hits and now - hits[0][0] >= _WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= self.limit:
            # Окно бывает пустым: `AI_REQUESTS_PER_HOUR=0` или отрицательное
            # значение — это «закрыто для всех», а oldest в пустом deque нет.
            # Прежний индекс `hits[0]` превращал такой ключ в IndexError, то есть
            # в 500 на каждом запросе вместо честного 429 на всё окно.
            oldest = hits[0][0] if hits else now
            wait_seconds = int(_WINDOW_SECONDS - (now - oldest)) + 1
            raise _too_many_requests(self.limit, wait_seconds)
        label = member or f"anon:{uuid.uuid4().hex}"
        hits.append((now, label))
        return label

    def release(self, key: str, member: str) -> bool:
        """Снимает слот по метке. Идемпотентно: чужой или уже снятый слот
        не удаляет ничего, то есть второй вызов не даёт второго слота."""
        hits = self._hits.get(key)
        if not hits:
            return False
        for index, (_, stored) in enumerate(hits):
            if stored == member:
                del hits[index]
                return True
        return False

    def reset(self) -> None:
        self._hits.clear()


# Скользящее окно одним скриптом: чтение и запись обязаны быть атомарными, иначе
# два одновременных запроса оба увидят «слот ещё есть» и оба его займут —
# лимит станет мягче ровно тогда, когда нагрузка выше.
_BUDGET_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local used = redis.call('ZCARD', key)
if used >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, tostring(oldest[2] or '')}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, math.ceil(window * 1000))
return {1, ''}
"""


class _RedisWindow:
    """Общий почасовой бюджет на все реплики процесса.

    Время берётся настенное, а не монотонное: общее окно сравнивает метки из
    разных процессов, а монотонные часы у них не совпадают. Участник множества
    уникален (метка слота, а при её отсутствии — время + uuid), иначе два
    запроса в одну миллисекунду заняли бы один слот.
    """

    def __init__(self, url: str, limit: int) -> None:
        from redis.asyncio import Redis

        self.limit = limit
        self._client = Redis.from_url(url, decode_responses=True,
                                      socket_timeout=1.0,
                                      socket_connect_timeout=1.0)
        self._script = self._client.register_script(_BUDGET_SCRIPT)

    async def acquire(self, key: str, member: Optional[str] = None) -> str:
        now = time.time()
        label = member or f"{now}:{uuid.uuid4().hex}"
        allowed, oldest = await self._script(
            keys=[_budget_key(key)],
            args=[now, _WINDOW_SECONDS, self.limit, label],
        )
        if int(allowed):
            return label
        try:
            wait_seconds = int(_WINDOW_SECONDS - (now - float(oldest))) + 1
        except (TypeError, ValueError):
            wait_seconds = int(_WINDOW_SECONDS)
        raise _too_many_requests(self.limit, wait_seconds)

    async def release(self, key: str, member: str) -> bool:
        """Снимает ровно один слот по его метке.

        `ZREM` идемпотентен сам по себе: второй вызов возвращает 0 и ничего не
        освобождает, поэтому повторный возврат слота не расширяет лимит. Метка
        не удаляет «самый старый» слот — иначе одна задача смогла бы вернуть
        бюджет чужой задачи того же пользователя.
        """
        return bool(await self._client.zrem(_budget_key(key), member))

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def reset(self) -> None:
        keys: List[str] = []
        async for key in self._client.scan_iter(match="ai:budget:*", count=500):
            keys.append(key)
        if keys:
            await self._client.delete(*keys)

    async def close(self) -> None:
        await self._client.aclose()


def _budget_key(key: str) -> str:
    return f"ai:budget:{key}"


_ai_window = _SlidingWindow(AI_REQUESTS_PER_HOUR)
_redis_budget: Optional[_RedisWindow] = None
_budget_degraded_at = 0.0

# --- повторное подключение Redis бюджета -------------------------------------
#
# `init_ai_budget` вызывается из lifespan ровно один раз, а Redis поднимается
# вместе со стеком и мог быть занят в момент старта backend (`docker compose
# restart redis`, гонка заголовков здоровья). Прежняя однократная попытка
# означала, что очередь после появления Redis чинится сама (`get_broker`
# повторяет подключение на каждый вызов), а бюджет — никогда: до рестарта
# процесса лимит считается локально, то есть `AI_REQUESTS_PER_HOUR × число
# реплик`, и молча умножает деньги за токены. Поэтому окно переподключения
# здесь такое же, как у брокера, но с задержкой: без неё мёртвый Redis
# пробовали бы на каждый ИИ-запрос, и деградация ещё и тормозила бы маршрут.
_BUDGET_RETRY_BASE_SECONDS = 5.0
_BUDGET_RETRY_MAX_SECONDS = 300.0
_budget_next_try = 0.0
_budget_retry_attempts = 0
_budget_retry_in_flight = False
#: деградация напечатана один раз «вслышную»: повторять её на каждый сбой —
#: шум, который перестают читать, а молчать про неё нельзя.
_budget_degradation_logged = False


def _budget_retry_delay() -> float:
    return min(_BUDGET_RETRY_BASE_SECONDS * (2 ** max(0, _budget_retry_attempts - 1)),
               _BUDGET_RETRY_MAX_SECONDS)


def _schedule_budget_retry() -> None:
    global _budget_next_try, _budget_retry_attempts
    _budget_retry_attempts += 1
    _budget_next_try = time.monotonic() + _budget_retry_delay()


async def _try_attach_redis_budget() -> bool:
    """Одна попытка подключить общее окно; True, если оно уже общее."""
    global _redis_budget, _budget_degradation_logged, _budget_retry_attempts
    if _redis_budget is not None:
        return True
    if not REDIS_URL:
        return False
    candidate = None
    try:
        candidate = _RedisWindow(REDIS_URL, AI_REQUESTS_PER_HOUR)
        await candidate.ping()
    except Exception as error:  # noqa: BLE001 — деградируем, а не падаем
        # Клиент создан ДО `ping`: если соединение успело подняться, а команда
        # упала (сервер лёг между accept и ответом, таймаут сокета), пул
        # держит сокет. Без закрытия каждая попытка спирали — 5 с, 10 с, …
        # до 300 с — добавляла бы висящий дескриптор в долгий процесс, а
        # `_detach_redis_budget` так и делает ровно для этого же случая.
        # `candidate is None` — конструктор упал ещё на разборе адреса:
        # закрывать нечего, и обращение к нему спрятало бы настоящую причину.
        if candidate is not None:
            try:
                await candidate.close()
            except Exception:  # noqa: BLE001 — сервер уже не отвечает, закрываемся как можем
                logger.debug("Клиент бюджета при неудачной пробе не закрылся",
                             exc_info=True)
        _schedule_budget_retry()
        if not _budget_degradation_logged:
            _budget_degradation_logged = True
            # Один раз и громко: «process» в /health без этого текста —
            # молчаливая деградация денежного стража, алерта на неё нет.
            logger.error(
                "БЮДЖЕТ ИИ ДЕГРАДИРОВАЛ В ЛОКАЛЬНЫЙ СЧЁТЧИК: Redis по %s "
                "недоступен (%s). Ограничение AI_REQUESTS_PER_HOUR=%d теперь "
                "считается на процесс, то есть умножается на число реплик; "
                "повторные попытки подключения идут с задержкой, /health "
                "показывает ai_budget=process.", REDIS_URL, error,
                AI_REQUESTS_PER_HOUR)
        else:
            logger.debug("Redis бюджета снова недоступен: %s", error)
        return False
    recovered = _budget_degradation_logged
    _redis_budget = candidate
    _budget_retry_attempts = 0
    if recovered:
        logger.warning("Общий бюджет ИИ восстановлен: Redis по %s снова окно "
                       "счёта", REDIS_URL)
    else:
        logger.info("Общий бюджет ИИ: %d запросов в час на пользователя (Redis)",
                    AI_REQUESTS_PER_HOUR)
    return True


async def _detach_redis_budget(error: BaseException) -> None:
    """Забывает общее окно после сбоя в середине запроса.

    Держать его нельзя: `ping` на подключении проходит, а `ZADD` падает, когда
    сервер уже лёг, — и каждый ИИ-запрос платил бы таймаут сокета, прежде чем
    досчитается локально. Окно закрывается, дальше работает та же ленивая
    переподсоединительная спираль, что и при старте.
    """
    global _redis_budget
    logger.warning("Общее окно бюджета отключено: Redis оборвался в процессе "
                   "запроса (%s), считаю локально", error)
    if _redis_budget is not None:
        try:
            await _redis_budget.close()
        except Exception:  # noqa: BLE001 — сервер уже не отвечает, закрываемся как можем
            logger.debug("Клиент бюджета не закрылся корректно", exc_info=True)
        _redis_budget = None
    _schedule_budget_retry()


async def _ensure_redis_budget() -> None:
    """Ленивое переподключение общего окна: не чаще, чем раз в `_budget_retry_delay`."""
    global _budget_retry_in_flight
    if _redis_budget is not None or not REDIS_URL:
        return
    if _budget_retry_in_flight or time.monotonic() < _budget_next_try:
        return
    _budget_retry_in_flight = True
    try:
        await _try_attach_redis_budget()
    finally:
        _budget_retry_in_flight = False


async def init_ai_budget() -> None:
    """Подключает общий бюджет, если задан адрес Redis (вызывается из lifespan).

    Сбой подключения не роняет приложение: бюджет остаётся локальным, то есть
    ограничение слабее, но контур работает. Обратный выбор — отвечать 503 на
    каждый ИИ-запрос из-за недоступного счётчика — ставил бы availability в
    зависимость от сервиса, который влияет только на строгость лимита.
    """
    if not REDIS_URL:
        logger.info("Общий бюджет ИИ выключен (REDIS_URL не задан): счётчик "
                    "локальный для процесса")
        return
    if await _try_attach_redis_budget():
        return
    # Первая попытка не удалась: `_budget_next_try` уже выставлен, и
    # `consume_ai_budget` продолжит пробовать подключение, не дожидаясь рестарта.


async def reset_ai_budget() -> None:
    """Тестовый хук: обнулить счётчик и состояние переподключения между тестами."""
    global _budget_degradation_logged, _budget_next_try, _budget_retry_attempts
    global _budget_retry_in_flight
    _ai_window.reset()
    _budget_degradation_logged = False
    _budget_next_try = 0.0
    _budget_retry_attempts = 0
    _budget_retry_in_flight = False
    if _redis_budget is not None:
        await _redis_budget.reset()


async def close_ai_budget() -> None:
    global _redis_budget, _liveness_client
    if _redis_budget is not None:
        await _redis_budget.close()
        _redis_budget = None
    # Клиент пробы живости закрывается здесь же: у приложения один крюк
    # завершения (`lifespan` в app/main.py), и плодить их — значит оставить
    # висящее соединение после остановки процесса.
    if _liveness_client is not None:
        _liveness_client.close()
        _liveness_client = None


def budget_backend() -> str:
    """Чем считается бюджет: для /health и для отчёта о деградации."""
    return "redis" if _redis_budget is not None else "process"


def budget_degraded() -> bool:
    """Бюджет обязан быть общим, но считается локально.

    Отдельно от `budget_backend()`: «process» — штатный ответ в режиме без
    Redis, а деградация при заданном `REDIS_URL` — это состояние окружения,
    которое обязано быть различимым: оно умножает лимит на число реплик.
    """
    return bool(REDIS_URL) and _redis_budget is None


def budget_retry_in_seconds() -> Optional[float]:
    """Сколько осталось до следующей попытки подключения общего окна (None —
    переподключение не требуется). Нужно операционному маршруту, чтобы «process»
    в /health читался как «вернётся само через N с», а не как приговор до
    рестарта."""
    if budget_backend() == "redis" or not REDIS_URL:
        return None
    return max(0.0, _budget_next_try - time.monotonic())


async def get_ai_user(user: User = Depends(get_current_user)) -> User:
    """Пользователь ИИ-маршрута; бюджет здесь НЕ списывается.

    Списание в зависимостях стояло до проверки объектного доступа, поэтому
    перебор чужих `diagram_id` сжигал часовой лимит и отвечал 429 раньше, чем
    404, — то есть сам отказ в доступе становился инструментом исчерпания
    бюджета. `consume_ai_budget` вызывают внутри обработчика, после `load_diagram`.
    """
    return user


def budget_slot(user_id: int | str, task_id: Optional[str] = None) -> str:
    """Метка слота бюджета.

    Когда идентификатор задачи известен, метка выводима из пары
    «пользователь + задача»: возврат слота (`release_ai_budget`) происходит и в
    другом процессе — воркер отвечает из кэша или хоронит потерянную задачу, — и
    держать в это время словарь объектов-ручек означало бы сериализовать их в
    очередь. Префикс по идентификатору пользователя и есть область видимости:
    снять чужой слот можно только вместе с чужим id.
    """
    return f"{user_id}:{task_id}" if task_id else f"{user_id}:{uuid.uuid4().hex}"


async def consume_ai_budget(user: User, *, task_id: Optional[str] = None) -> str:
    """Списывает слот часового бюджета ИИ; 429, если бюджет выбран.

    Возвращает метку слота: её отдают `release_ai_budget`, когда за задачу
    пришлось не платить (очередь легла, задача отменена, ответ пришёл из кэша).
    """
    global _budget_degraded_at
    await _ensure_redis_budget()
    label = budget_slot(user.id, task_id)
    if _redis_budget is not None:
        try:
            await _redis_budget.acquire(str(user.id), label)
            return label
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 — считаем локально, а не отказываем
            now = time.monotonic()
            if now - _budget_degraded_at >= 60.0:
                _budget_degraded_at = now
                logger.warning("Redis бюджета недоступен, считаю локально: %s", error)
            # Окно с таким сервером держать больше нельзя: `ping` при открытии
            # прошёл, а запись упала, — каждый следующий запрос платил бы таймаут
            # сокета до локального досчёта. Дальше работает спираль переподключения.
            await _detach_redis_budget(error)
    _ai_window.acquire(str(user.id), label)
    return label


async def release_ai_budget(user_id: int | str, *, slot: Optional[str] = None,
                            task_id: Optional[str] = None) -> bool:
    """Возвращает занятый слот часового бюджета.

    Без возврата слота отказавшая задача съедала бы оплаченное обращение:
    `queue_unavailable`, снятие до работы и потерянная постановка (`tries == 0`)
    случаются до обращения к провайдеру; `worker_lost` по таймауту очереди — нет,
    и там слот остаётся списанным,
    отменённая задача его не платит, а ответ из кэша тем более. Худший сценарий
    был реален: шестьдесят повторов одного закешированного отказа выбирали окно
    `AI_REQUESTS_PER_HOUR` с нулём обращений к провайдеру.

    Идемпотентно по построению: и `ZREM`, и удаление метки из локального окна
    снимают ровно один слот и не находят его при повторе, поэтому двойной
    возврат не даёт лишнего бюджета. Пробуются оба окна: между `consume` и
    `release` Redis мог лечь или подняться, и слот остался в том, которое
    писало. Возвращает True, если что-то освобождено.
    """
    label = slot or budget_slot(user_id, task_id)
    key = str(user_id)
    if not label.startswith(f"{key}:"):
        # Метка принадлежит другому пользователю: в Redis это был бы no-op
        # (ключ окна — `ai:budget:{user}`), а в локальном окне удаление по
        # метке ничего не отличает. Отказ явный, чтобы ошибка проводки не
        # выглядела «возврат не сработал».
        logger.warning("Выпущен слот %s, не принадлежащий пользователю %s",
                       label, key)
        return False
    freed = False
    # Возврат обязан дойти до общего окна, даже если наш процесс его не
    # поднимал: `init_ai_budget` зовёт только lifespan веб-процесса
    # (app/main.py), а слот возвращает воркер (app/tasks.py:_refund_unspent).
    # Без этой строки все возвраты в compose-топологии — отмена до исполнения,
    # ответ из кэша, потерянная постановка, `worker_lost` — были бы no-op:
    # `_redis_budget` там None, а в локальном окне метки нет, потому что
    # списывал её другой процесс. Ленивость и троттлинг у переподключения уже
    # есть в `_ensure_redis_budget`, так что цена — одна попытка на слот.
    await _ensure_redis_budget()
    if _redis_budget is not None:
        try:
            freed = await _redis_budget.release(key, label)
        except Exception as error:  # noqa: BLE001 — возврат слота не роняет маршрут
            logger.warning("Не удалось вернуть слот бюджета в Redis: %s", error)
    if _ai_window.release(key, label):
        freed = True
    return freed


# --- живость воркеров --------------------------------------------------------
#
# Глубина очереди («сколько задач ждут») не отличает «воркер медленный» от
# «воркера нет»: при пустом исполнителе счётчик просто растёт. Отметку
# присутствия пишет сам arq (`health_check_key` настроек воркера), и она живёт
# ровно один интервал, поэтому её отсутствие — диагноз «процесс умер», а не
# «задача долгая». В compose по этой же отметке проверяется здоровье контейнера.
_liveness_client: Optional[Any] = None
_liveness_guard = threading.Lock()


def worker_health_key(queue: str) -> str:
    """Ключ отметки присутствия воркера очереди (контракт arq)."""
    return f"{queue}:health"


def _liveness_redis() -> Optional[Any]:
    """Синхронный клиент Redis для пробы живости; None, если окна очереди нет."""
    global _liveness_client
    if not REDIS_URL:
        return None
    if _liveness_client is None:
        from redis import Redis

        # Замок, а не «проверил и создал»: `/health` — синхронный обработчик, и
        # FastAPI зовёт его из threadpool, так что параллельные пробы создавали бы
        # по клиенту на поток, а лишние не закрывались никогда (закрываем мы
        # один, тот что остался в модуле).
        with _liveness_guard:
            if _liveness_client is None:
                # Таймаут обязателен: пробу зовут из операционных маршрутов, и
                # висящее соединение на мёртвом Redis стоило бы каждому запросу
                # таймаут клиента.
                _liveness_client = Redis.from_url(
                    REDIS_URL, decode_responses=True, socket_timeout=1.0,
                    socket_connect_timeout=1.0)
    return _liveness_client


def worker_liveness(queues: Sequence[str]) -> Dict[str, str]:
    """Жив ли воркер каждой очереди: `alive` / `dead` / `unknown`.

    `unknown` — это «неизвестно», а не «мёртв»: без `REDIS_URL` задачи исполняет
    сам веб-процесс, воркера нет и хоронить нечего, а недоступный Redis не даёт
    прочитать отметку. Смешать эти два случая в одно «dead» значило бы валить
    healthcheck стека из-за режима, который штатный.

    Синхронная: `/health` — синхронный обработчик, он и так в threadpool FastAPI.
    Асинхронному маршруту (app/routers/tasks.py) звать её надо через
    `fastapi.concurrency.run_in_threadpool`, иначе висящий Redis заблокирует
    событийный цикл на таймаут сокета.
    """
    client = _liveness_redis()
    if client is None:
        return {queue: "unknown" for queue in queues}
    # Одна поездка на все очереди (`MGET`), а не по `GET` на каждую: таймаут
    # сокета здесь 1 с, и на зависшем Redis последовательный обход двух очередей
    # тратил бы до четырёх секунд из пяти, что у healthcheck'а в compose. В
    # единственном случае, когда проба и нужна, она не успевала бы и валить
    # контейнер, которого она как раз касается.
    keys = [worker_health_key(queue) for queue in queues]
    try:
        pairs = list(zip(queues, client.mget(keys), strict=True))
    except Exception as error:  # noqa: BLE001 — наблюдение не вправе ронять запрос
        logger.debug("Отметки живости %s не читаются: %s", keys, error)
        return {queue: "unknown" for queue in queues}
    return {queue: ("alive" if value else "dead") for queue, value in pairs}


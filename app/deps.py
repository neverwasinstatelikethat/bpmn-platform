"""Зависимости FastAPI: текущий пользователь по JWT и бюджет ИИ-маршрутов."""
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

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


def _too_many_requests(limit: int, wait_seconds: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(f"Превышен часовой лимит ИИ-запросов ({limit}). "
                "Повторите позже."),
        headers={"Retry-After": str(max(1, wait_seconds))},
    )


class _SlidingWindow:
    """Скользящее окно обращений на час внутри одного процесса.

    Счётчик локальный: при нескольких репликах каждая считает свою. Для
    почасового бюджета ИИ это означает лимит, умноженный на число реплик,
    поэтому при заданном `REDIS_URL` бюджет считается общим окном в Redis
    (`_RedisWindow`), а этот класс остаётся резервом на время его недоступности
    и единственным счётчиком для троттлинга auth-маршрутов, где цена ошибки —
    не деньги за токены.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def _prune(self, now: float) -> None:
        # Ключи забытых пользователей копить нельзя: окно чистится по мере
        # обращения, а не по таймеру.
        if len(self._hits) <= 4096:
            return
        for key in [k for k, q in self._hits.items()
                    if not q or now - q[-1] >= _WINDOW_SECONDS]:
            del self._hits[key]

    def acquire(self, key: str) -> None:
        """Регистрирует запрос; 429, если бюджет часа выбран."""
        now = time.monotonic()
        self._prune(now)
        hits = self._hits[key]
        while hits and now - hits[0] >= _WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= self.limit:
            wait_seconds = int(_WINDOW_SECONDS - (now - hits[0])) + 1
            raise _too_many_requests(self.limit, wait_seconds)
        hits.append(now)

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
    уникален (метка + uuid), иначе два запроса в одну миллисекунду заняли бы
    один слот.
    """

    def __init__(self, url: str, limit: int) -> None:
        from redis.asyncio import Redis

        self.limit = limit
        self._client = Redis.from_url(url, decode_responses=True,
                                      socket_timeout=1.0,
                                      socket_connect_timeout=1.0)
        self._script = self._client.register_script(_BUDGET_SCRIPT)

    async def acquire(self, key: str) -> None:
        now = time.time()
        allowed, oldest = await self._script(
            keys=[f"ai:budget:{key}"],
            args=[now, _WINDOW_SECONDS, self.limit, f"{now}:{uuid.uuid4().hex}"],
        )
        if int(allowed):
            return
        try:
            wait_seconds = int(_WINDOW_SECONDS - (now - float(oldest))) + 1
        except (TypeError, ValueError):
            wait_seconds = int(_WINDOW_SECONDS)
        raise _too_many_requests(self.limit, wait_seconds)

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


_ai_window = _SlidingWindow(AI_REQUESTS_PER_HOUR)
_redis_budget: Optional[_RedisWindow] = None
_budget_degraded_at = 0.0


async def init_ai_budget() -> None:
    """Подключает общий бюджет, если задан адрес Redis (вызывается из lifespan).

    Сбой подключения не роняет приложение: бюджет остаётся локальным, то есть
    ограничение слабее, но контур работает. Обратный выбор — отвечать 503 на
    каждый ИИ-запрос из-за недоступного счётчика — ставил бы availability в
    зависимость от сервиса, который влияет только на строгость лимита.
    """
    global _redis_budget
    if not REDIS_URL:
        logger.info("Общий бюджет ИИ выключен (REDIS_URL не задан): счётчик "
                    "локальный для процесса")
        return
    if _redis_budget is None:
        try:
            candidate = _RedisWindow(REDIS_URL, AI_REQUESTS_PER_HOUR)
            await candidate.ping()
        except Exception as error:  # noqa: BLE001 — деградируем, а не падаем
            logger.error("Redis бюджета недоступен, счётчик остаётся локальным: %s",
                         error)
            return
        _redis_budget = candidate
        logger.info("Общий бюджет ИИ: %d запросов в час на пользователя (Redis)",
                    AI_REQUESTS_PER_HOUR)


async def reset_ai_budget() -> None:
    """Тестовый хук: обнулить счётчик между тестами."""
    _ai_window.reset()
    if _redis_budget is not None:
        await _redis_budget.reset()


async def close_ai_budget() -> None:
    global _redis_budget
    if _redis_budget is not None:
        await _redis_budget.close()
        _redis_budget = None


def budget_backend() -> str:
    """Чем считается бюджет: для /health и для отчёта о деградации."""
    return "redis" if _redis_budget is not None else "process"


async def get_ai_user(user: User = Depends(get_current_user)) -> User:
    """Пользователь ИИ-маршрута; бюджет здесь НЕ списывается.

    Списание в зависимостях стояло до проверки объектного доступа, поэтому
    перебор чужих `diagram_id` сжигал часовой лимит и отвечал 429 раньше, чем
    404, — то есть сам отказ в доступе становился инструментом исчерпания
    бюджета. `consume_ai_budget` вызывают внутри обработчика, после `load_diagram`.
    """
    return user


async def consume_ai_budget(user: User) -> None:
    """Списывает слот часового бюджета ИИ; 429, если бюджет выбран."""
    global _budget_degraded_at
    if _redis_budget is not None:
        try:
            await _redis_budget.acquire(str(user.id))
            return
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 — считаем локально, а не отказываем
            now = time.monotonic()
            if now - _budget_degraded_at >= 60.0:
                _budget_degraded_at = now
                logger.warning("Redis бюджета недоступен, считаю локально: %s", error)
    _ai_window.acquire(str(user.id))

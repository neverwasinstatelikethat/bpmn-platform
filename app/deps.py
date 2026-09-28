"""Зависимости FastAPI: текущий пользователь по JWT и бюджет ИИ-маршрутов."""
import time
from collections import defaultdict, deque
from typing import Deque, Dict

import jwt
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import AI_REQUESTS_PER_HOUR, ALGORITHM, SECRET_KEY
from app.db import get_db
from app.models import User
from app.security import oauth2_scheme

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


class _SlidingWindow:
    """Скользящее окно обращений к ИИ на пользователя на час.

    Вызов модели стоит денег и идёт в один тарифный слот провайдера, поэтому
    маршрут должен быть ограничен и со стороны клиента. Счётчик живой и
    локальный для процесса: при нескольких репликах он считает каждую
    отдельно — общий бюджет даёт только внешний кэш (план в
    docs/plans/redis-cache-and-task-routing.md).
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
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(f"Превышен часовой лимит ИИ-запросов ({self.limit}). "
                        "Повторите позже."),
                headers={"Retry-After": str(max(1, wait_seconds))},
            )
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()


_ai_window = _SlidingWindow(AI_REQUESTS_PER_HOUR)


def reset_ai_budget() -> None:
    """Тестовый хук: обнулить счётчик между тестами."""
    _ai_window.reset()


async def get_ai_user(user: User = Depends(get_current_user)) -> User:
    """Пользователь ИИ-маршрута; бюджет здесь НЕ списывается.

    Списание в зависимостях стояло до проверки объектного доступа, поэтому
    перебор чужих `diagram_id` сжигал часовой лимит и отвечал 429 раньше, чем
    404, — то есть сам отказ в доступе становился инструментом исчерпания
    бюджета. `consume_ai_budget` вызывают внутри обработчика, после `load_diagram`.
    """
    return user


def consume_ai_budget(user: User) -> None:
    """Списывает слот часового бюджета ИИ; 429, если бюджет выбран."""
    _ai_window.acquire(str(user.id))

"""Хэширование паролей, выпуск JWT и схема OAuth2 для Swagger."""
import logging
from datetime import timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi.security import OAuth2PasswordBearer

from app.config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

# Короткий срок жизни — подстраховка, а не правило: настройка ACCESS_TOKEN_EXPIRE_MINUTES
# существовала в конфиге, но в выпуск токена не подставлялась вовсе, и живая сессия
# жила 15 минут при заявленных сутках.
FALLBACK_TOKEN_LIFETIME_MINUTES = 15

def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

def get_password_hash(password: str):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Выпускает JWT с сроком из ACCESS_TOKEN_EXPIRE_MINUTES.

    `iat` пишется рядом с `exp` не для разбора на стороне сервера, а чтобы
    тест и диагностика могли сравнить фактический срок токена с настройкой,
    а не с часами клиента, который токен получил.
    """
    to_encode = data.copy()
    now = utc_now()
    if expires_delta is not None:
        expire = now + expires_delta
    elif ACCESS_TOKEN_EXPIRE_MINUTES and ACCESS_TOKEN_EXPIRE_MINUTES > 0:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    else:
        # Настройка пуста или обнулена: безопаснее короткий срок, чем токен
        # без внятного момента истечения.
        expire = now + timedelta(minutes=FALLBACK_TOKEN_LIFETIME_MINUTES)
    to_encode.update({"iat": now, "exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

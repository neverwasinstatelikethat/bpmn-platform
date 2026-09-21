"""Хэширование паролей, выпуск JWT и схема OAuth2 для Swagger."""
import logging
from datetime import timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi.security import OAuth2PasswordBearer

from app.config import ALGORITHM, SECRET_KEY
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

def get_password_hash(password: str):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = utc_now() + expires_delta
    else:
        expire = utc_now() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

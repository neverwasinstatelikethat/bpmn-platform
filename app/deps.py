"""Зависимости FastAPI: текущий пользователь по JWT."""
import jwt

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import ALGORITHM, SECRET_KEY
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
    return user

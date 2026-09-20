"""Подключение к СУБД, фабрика сессий и зависимость get_db.

Модуль не создаёт схему и не пишет в базу: это задача app.startup, которую
вызывает lifespan приложения.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, registry, sessionmaker

from app.config import DATABASE_URL

# Инициализация БД
Base = declarative_base()
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
mapper_registry = registry()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

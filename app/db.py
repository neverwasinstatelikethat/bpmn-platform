"""Подключение к СУБД, фабрика сессий и зависимость get_db.

Модуль не создаёт схему и не пишет в базу: это задача app.startup, которую
вызывает lifespan приложения.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import (DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_SIZE,
                        DB_POOL_TIMEOUT, SQLITE_BUSY_TIMEOUT_MS)

# Инициализация БД
Base = declarative_base()
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        # Практики по умолчанию не заводятся на driver-соединении SQLite, и за
        # них платили так: без WAL параллельные автосейвы фронтенда упирались
        # в exclusive-блокировку и давали «database is locked» с 500 вместо
        # ожидания; без busy_timeout ожидание не наступало вовсе; без
        # foreign_keys база принимала сирот, которые на целевом PostgreSQL
        # отвергаются (тестовый фикстур fk_enforced в test_routes_smoke.py
        # включал прагму локально — теперь она общая).
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
else:
    # Параметры пула — из app.config: вызов модели удерживает соединение
    # до конца ИИ-запроса, и на дефолтных 5+10 исчерпание пула блокировало
    # не-ИИ маршруты тоже.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=DB_POOL_TIMEOUT,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

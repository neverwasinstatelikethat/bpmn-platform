"""Окружение Alembic: метаданные из app.models, URL из DATABASE_URL.

env.py кладёт корень проекта в sys.path, поэтому миграции запускаются и
как `alembic upgrade head`, и через python-API (см. app.startup.run_migrations).
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401 — импортирует все ORM-классы в метаданные

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Строка подключения цели.

    Приложение передаёт URL своего же engine через `config.attributes` — так
    миграции не могут уйти в базу, отличную от той, к которой обращается
    процесс (окружение могут изменить после импорта app.db). Для запуска через
    CLI остаётся DATABASE_URL.

    SECRET_KEY на PostgreSQL тоже обязателен: его проверяет app.config,
    который импортируется ради метаданных. Миграции запускают в том же
    окружении, что и backend.
    """
    url = config.attributes.get("database_url") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан — Alembic не знает, какую базу вести")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    # Значение в alembic.ini закомментировано намеренно: источник истины — окружение.
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite не умеет ALTER без пересоздания таблицы.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

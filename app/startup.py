"""Применение миграций Alembic и сидирование предопределённых ролей.

Вызывается из lifespan приложения: импорт пакета не должен писать в базу.

Легаси-пара `Base.metadata.create_all` + ad-hoc `apply_migration()` заменена
версионируемыми миграциями в `migrations/`. Миграции применяются на старте
процесса, потому что отдельного шага деплоя у проекта нет (один контейнер
backend). При переходе на несколько реплик `run_migrations()` убирают из
lifespan и вызывают из entrypoint до запуска приложений.
"""
import json
import logging
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db import Base, SessionLocal, engine
from app.models import Role

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _alembic_config() -> Config:
    # Config без alembic.ini: env.py не вызывает fileConfig и не перенастраивает
    # логирование уже запущенного приложения.
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Мигрируем ровно ту базу, к которой подключено приложение, а не то, что
    # лежит в окружении сейчас (его меняют, например, тесты после импорта app.db).
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _schema_gaps() -> tuple[list[str], dict[str, list[str]]]:
    """Чего не хватает базе из моделей: таблицы и колонки.

    Нужно, чтобы отличить «схема актуальна, просто без версий» (лечится одним
    `alembic stamp`) от схемы старше базовой ревизии (тогда stamp соврёт).
    """
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing_tables, missing_columns = [], {}
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            missing_tables.append(table.name)
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        gap = [column.name for column in table.columns if column.name not in have]
        if gap:
            missing_columns[table.name] = gap
    return missing_tables, missing_columns


def run_migrations() -> None:
    config = _alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    if current is None and inspect(engine).has_table("users"):
        missing_tables, missing_columns = _schema_gaps()
        if missing_tables or missing_columns:
            raise RuntimeError(
                "В базе есть таблицы без alembic_version, и они старше базовой "
                f"ревизии: не хватает {missing_tables or 'таблиц'} и колонок "
                f"{missing_columns or {}}. Довести такую схему миграциями нельзя — "
                "перенесите данные в чистую базу (migrate_sqlite_to_postgres.py)."
            )
        raise RuntimeError(
            "В базе есть таблицы, но alembic_version отсутствует — это схема до "
            f"перехода на Alembic. Она совпадает с моделями, поэтому выполните "
            f"один раз: alembic stamp {head}"
        )

    command.upgrade(config, "head")
    logger.info("Схема БД приведена к ревизии %s", head)


def seed_default_roles():
    # Заполняет предопределённые роли на чистой базе (любая СУБД).
    session = SessionLocal()
    try:
        if session.query(Role).count() == 0:
            for name, perms in PREDEFINED_ROLES:
                session.add(Role(id=str(uuid.uuid4()), name=name, permissions=json.dumps(perms)))
            session.commit()
    finally:
        session.close()


PREDEFINED_ROLES = [
    ("admin", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": True}),
    ("editor", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": False}),
    ("viewer", {"viewRegistry": True, "viewRoles": True, "editRegistry": False, "manageRoles": False}),
]


def prepare_database() -> None:
    run_migrations()
    seed_default_roles()

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


def _dead_tables() -> list[str]:
    """Таблицы, которых нет в моделях: relic этого приложения или чужие объекты."""
    inspector = inspect(engine)
    model_tables = {table.name for table in Base.metadata.sorted_tables}
    return sorted(set(inspector.get_table_names()) - model_tables - {"alembic_version"})


def run_migrations() -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    base = list(script.walk_revisions())[-1].revision

    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    if current is None and inspect(engine).has_table("users"):
        # Разница между «базой до Alembic» и «базой старше базовой ревизии» на
        # глаз не видна, поэтому подсказка одна: поставить базовую ревизию и
        # догнать head. Если схема всё-таки старше, upgrade head не добавит
        # колонки, и приложение честно упадёт на первом же запросе.
        extras = ""
        dead = _dead_tables()
        if dead:
            extras = f" Лишние таблицы {dead} останутся как есть: в app/models.py их нет."
        raise RuntimeError(
            "В базе есть таблицы, но alembic_version отсутствует — она создана до "
            f"перехода на Alembic. Выполните один раз: alembic stamp {base} "
            f"&& alembic upgrade head.{extras} Если база старше базовой ревизии, "
            "вместо stamp перенесите данные в чистую (migrate_sqlite_to_postgres.py)."
        )

    command.upgrade(config, "head")
    logger.info("Схема БД приведена к ревизии %s", head)


PREDEFINED_ROLES = [
    ("admin", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": True}),
    ("editor", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": False}),
    ("viewer", {"viewRegistry": True, "viewRoles": True, "editRegistry": False, "manageRoles": False}),
]


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


def prepare_database() -> None:
    run_migrations()
    seed_default_roles()

"""Проверки миграционной обвязки: схема разворачивается из Alembic, а не из моделей.

Тесты прогоняют те же ревизии, что и развёртывание, поэтому опечатка в
миграции или расхождение с app/models.py видны сразу, а не на живой базе.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
TABLES = {
    "users", "roles", "teams", "team_members", "invitations", "folders",
    "diagrams", "diagram_versions", "share_tokens", "deleted_diagrams",
    "pending_improvements", "password_reset_tokens",
}


def _alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "SKIP_LLM_INIT": "1"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def _script() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    return ScriptDirectory.from_config(config)


def test_single_migration_head():
    """Двух голов быть не должно: `upgrade head` обязан применяться линейно."""
    assert len(_script().get_heads()) == 1


def test_upgrade_creates_full_schema(tmp_path):
    """Чистая база после `alembic upgrade head` содержит все таблицы моделей."""
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    result = _alembic("upgrade", "head", url=url)
    assert result.returncode == 0, result.stdout + result.stderr

    inspector = inspect(create_engine(url))
    assert TABLES <= set(inspector.get_table_names())
    assert "alembic_version" in inspector.get_table_names()
    # Индексы из моделей тоже доезжают до схемы.
    assert "ix_users_email" in {i["name"] for i in inspector.get_indexes("users")}


def test_migrations_match_models(tmp_path):
    """После применения ревизий autogenerate не находит отличий от моделей."""
    url = f"sqlite:///{(tmp_path / 'drift.db').as_posix()}"
    assert _alembic("upgrade", "head", url=url).returncode == 0
    check = _alembic("check", url=url)
    assert check.returncode == 0, check.stdout + check.stderr


def test_history_backfilled_for_existing_diagrams(tmp_path):
    """Диаграммы до 0002 получают версию 1: иначе base_seq неоткуда отсчитывать."""
    url = f"sqlite:///{(tmp_path / 'with-data.db').as_posix()}"
    assert _alembic("upgrade", "0001", url=url).returncode == 0
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, name) VALUES (1, 'Хозяин')"))
        for diagram_id, xml in (("d-filled", "<definitions/>"), ("d-empty", None)):
            conn.execute(
                text("INSERT INTO diagrams (id, name, xml_content, score, user_id) "
                     "VALUES (:id, 'D', :xml, 7, 1)"),
                {"id": diagram_id, "xml": xml},
            )

    assert _alembic("upgrade", "head", url=url).returncode == 0

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT diagram_id, seq, xml_content, source, score, author_id "
            "FROM diagram_versions"
        )).fetchall()
        seqs = conn.execute(text("SELECT id, version_seq FROM diagrams ORDER BY id")).fetchall()
    assert rows == [("d-filled", 1, "<definitions/>", "saved", 7, 1)]
    assert seqs == [("d-empty", 0), ("d-filled", 1)]


def test_legacy_database_without_versions_is_not_migrated_blindly(tmp_path, monkeypatch):
    """База до Alembic: приложение не создаёт таблицы поверх своей схемы молча."""
    from app import startup

    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    legacy_engine = create_engine(url)
    with legacy_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(startup, "engine", legacy_engine)
    with pytest.raises(RuntimeError) as exc_info:
        startup.run_migrations()

    assert "alembic stamp" in str(exc_info.value)
    # Ни одной попытки создать таблицы поверх легаси.
    assert inspect(legacy_engine).get_table_names() == ["users"]


def test_pre_alembic_database_is_told_to_stamp_then_upgrade(tmp_path, monkeypatch):
    """Легаси-база уровня 0001 получает верную процедуру: stamp базы + upgrade head.

    Проверять «совпадает ли схема с моделями head» здесь нельзя: база до Alembic
    по определению без колонок более поздних ревизий, и отказ «она слишком стара»
    был бы ложным.
    """
    from app import startup

    url = f"sqlite:///{(tmp_path / 'pre-alembic.db').as_posix()}"
    assert _alembic("upgrade", "0001", url=url).returncode == 0
    legacy_engine = create_engine(url)
    with legacy_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")

    monkeypatch.setattr(startup, "engine", legacy_engine)
    with pytest.raises(RuntimeError) as exc_info:
        startup.run_migrations()

    message = str(exc_info.value)
    assert "alembic stamp 0001 && alembic upgrade head" in message

    # После выполненной подсказки приложение поднимается и догоняет head.
    assert _alembic("stamp", "0001", url=url).returncode == 0
    startup.run_migrations()
    migrated = inspect(legacy_engine)
    assert "diagram_versions" in migrated.get_table_names()
    assert "version_seq" in {c["name"] for c in migrated.get_columns("diagrams")}

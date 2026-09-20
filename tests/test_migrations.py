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
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
TABLES = {
    "users", "roles", "teams", "team_members", "invitations", "folders",
    "diagrams", "share_tokens", "deleted_diagrams", "pending_improvements",
    "password_reset_tokens",
}


def _alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "SKIP_LLM_INIT": "1"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def test_head_revision_is_0001():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    assert ScriptDirectory.from_config(config).get_current_head() == "0001"


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


def test_stale_legacy_schema_is_not_stamped(tmp_path, monkeypatch):
    """Таблицы старше ревизии 0001: stamp был бы ложью, поэтому отказ с диагнозом."""
    from app import startup

    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    legacy_engine = create_engine(url)
    with legacy_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(startup, "engine", legacy_engine)
    with pytest.raises(RuntimeError) as exc_info:
        startup.run_migrations()

    assert "старше базовой ревизии" in str(exc_info.value)
    assert "diagrams" in str(exc_info.value)
    # Ни одной попытки создать таблицы поверх легаси.
    assert inspect(legacy_engine).get_table_names() == ["users"]


def test_current_legacy_schema_is_stamped_once(tmp_path, monkeypatch):
    """Актуальная схема без alembic_version направляет на `alembic stamp 0001`."""
    from app import startup

    url = f"sqlite:///{(tmp_path / 'pre-alembic.db').as_posix()}"
    assert _alembic("upgrade", "head", url=url).returncode == 0
    engine_without_versions = create_engine(url)
    with engine_without_versions.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")

    monkeypatch.setattr(startup, "engine", engine_without_versions)
    with pytest.raises(RuntimeError) as exc_info:
        startup.run_migrations()

    assert "alembic stamp 0001" in str(exc_info.value)

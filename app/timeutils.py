"""Единая точка «сейчас» для слоёв хранения и API.

Колонки DateTime в схеме наивные (SQLite и PostgreSQL хранят TIMESTAMP без
часового пояса), поэтому время в БД — UTC без tzinfo. Вся запись идёт через
utc_now(): переход на осмысленное время — одна правка здесь плюс timezone=True
в моделях и миграция, а не 46 точек по пакету.
"""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Текущее время в UTC, наивное — ровно то, что ждут колонки DateTime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

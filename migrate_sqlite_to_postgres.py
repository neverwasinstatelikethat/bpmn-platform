"""Одноразовый перенос данных из SQLite (bpmn.db) в PostgreSQL.

Запускать ПОСЛЕ подъёма СУБД и ДО первого реального запуска приложения
(на свежей базе, где ещё нет пользовательских данных). Схема цели разворачивается
миграциями Alembic этим же запуском.

Локально (нужен psycopg2, SECRET_KEY и доступ к БД):
    DATABASE_URL=postgresql+psycopg2://bpmn:<пароль>@127.0.0.1:5432/bpmn \
    python migrate_sqlite_to_postgres.py

Через Docker-контур (бд должна быть поднята: `docker compose up -d db`;
схема будет создана этим же запуском):
    docker compose run --rm -v "$(pwd -W)/bpmn.db:/app/bpmn.db:ro" \
        backend python migrate_sqlite_to_postgres.py

На Windows/Git-Bash используй `$(pwd -W)` (даёт `C:/...`), а не `${PWD}`
(`/c/...`): последний не монтируется в Docker Desktop, и скрипт сообщает
«Исходная база ... не найдена или пуста». На Linux/macOS подойдёт `${PWD}`.

Если база цели уже создана старым `create_all`, Alembic откажется угадывать —
убедитесь, что схема совпадает с app/models.py, и выполните один раз
`alembic stamp 0001` (см. сообщение об ошибке).

Повторный запуск безопасен: уже существующие по первичному ключу строки
пропускаются. Сидированные приложением роли удаляются только если на них
нет ссылок (т.е. база свежая), после чего копируются роли из SQLite.
"""

import os
import sys

from sqlalchemy import MetaData, create_engine, inspect, text

from app.config import DATABASE_URL
from app.db import engine
from app.startup import run_migrations

SQLITE_URL = os.getenv("SQLITE_URL", "sqlite:///./bpmn.db")

# Порядок с учётом внешних ключей (таблицы без ссылок раньше).
TABLE_ORDER = [
    "users",
    "roles",
    "teams",
    "team_members",
    "invitations",
    "folders",
    "diagrams",
    "share_tokens",
    "deleted_diagrams",
    "pending_improvements",
    "password_reset_tokens",
]


def primary_key_columns(table):
    pk = list(table.primary_key.columns)
    if not pk:
        raise RuntimeError(f"Таблица {table.name} без первичного ключа — перенос невозможен")
    return pk


def coerce_value(dst_col, value):
    if value is None:
        return None
    # SQLite хранит булевы как 0/1; драйвер PostgreSQL требует настоящий bool.
    try:
        if dst_col.type.python_type is bool and not isinstance(value, bool):
            return bool(value)
    except NotImplementedError:
        pass
    return value


def existing_keys(conn, dst_table, pk_cols):
    rows = conn.execute(dst_table.select().with_only_columns(*pk_cols)).all()
    return {tuple(row) for row in rows}


def copy_table(src_conn, dst_conn, src_table, dst_table):
    pk_cols_src = primary_key_columns(src_table)
    common = [c.name for c in src_table.columns if c.name in {d.name for d in dst_table.columns}]
    if not common:
        return 0
    pk_cols = [c for c in pk_cols_src if c.name in common]
    skip = existing_keys(dst_conn, dst_table, [dst_table.c[c.name] for c in pk_cols])

    copied = 0
    for row in src_conn.execute(src_table.select().with_only_columns(*[src_table.c[n] for n in common])):
        key = tuple(row._mapping[c.name] for c in pk_cols)
        if key in skip:
            continue
        values = {
            name: coerce_value(dst_table.c[name], row._mapping[name])
            for name in common
        }
        dst_conn.execute(dst_table.insert().values(**values))
        copied += 1
    return copied


def copy_self_referencing(src_conn, dst_conn, src_table, dst_table):
    # folders.parent_id ссылается на folders.id: вставляем несколькими
    # проходами, пока есть прогресс (родители раньше детей).
    pk_cols_src = primary_key_columns(src_table)
    common = [c.name for c in src_table.columns if c.name in {d.name for d in dst_table.columns}]
    pk_cols = [c for c in pk_cols_src if c.name in common]
    skip = existing_keys(dst_conn, dst_table, [dst_table.c[c.name] for c in pk_cols])

    all_rows = src_conn.execute(
        src_table.select().with_only_columns(*[src_table.c[n] for n in common])
    ).fetchall()
    pending = [
        r for r in all_rows
        if tuple(r._mapping[c.name] for c in pk_cols) not in skip
    ]
    inserted_ids = {k for k in skip}
    total = 0
    while pending:
        progressed = []
        for row in pending:
            parent_id = row._mapping.get("parent_id")
            if parent_id is not None and (parent_id,) not in inserted_ids:
                continue
            values = {name: coerce_value(dst_table.c[name], row._mapping[name]) for name in common}
            dst_conn.execute(dst_table.insert().values(**values))
            inserted_ids.add(tuple(row._mapping[c.name] for c in pk_cols))
            total += 1
            progressed.append(row)
        if not progressed:
            ids = [tuple(r._mapping[c.name] for c in pk_cols) for r in pending]
            raise RuntimeError(f"Не удалось разрешить родительские связи в {src_table.name}: {ids}")
        pending = [r for r in pending if r not in progressed]
    return total


def main():
    if DATABASE_URL.startswith("sqlite"):
        print("DATABASE_URL указывает на SQLite. Задайте DATABASE_URL с PostgreSQL и повторите.")
        return 2

    src_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    if not inspect(src_engine).has_table("diagrams"):
        print(f"Исходная база {SQLITE_URL} не найдена или пуста.")
        return 2
    # Схема цели из миграций: без неё reflect ниже увидел бы пустую базу.
    run_migrations()
    dst_engine = engine

    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)
    dst_meta = MetaData()
    dst_meta.reflect(bind=dst_engine)

    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        # Свежие сидированные роли заменяются ролями из SQLite, чтобы
        # внешние ключи team_members/invitations остались валидными.
        roles = dst_meta.tables.get("roles")
        if roles is not None:
            for ref_table in ("team_members", "invitations"):
                t = dst_meta.tables.get(ref_table)
                if t is not None and dst_conn.execute(t.select().limit(1)).first():
                    print(f"Таблица {ref_table} не пуста: перенос ролей небезопасен, прерываюсь.")
                    return 1
            dst_conn.execute(roles.delete())

        for name in TABLE_ORDER:
            src_table = src_meta.tables.get(name)
            dst_table = dst_meta.tables.get(name)
            if src_table is None:
                print(f"{name}: нет в исходной базе — пропускаю")
                continue
            if dst_table is None:
                print(f"{name}: нет в целевой схеме — пропускаю")
                continue
            if name == "folders":
                copied = copy_self_referencing(src_conn, dst_conn, src_table, dst_table)
            else:
                copied = copy_table(src_conn, dst_conn, src_table, dst_table)
            print(f"{name}: перенесено строк: {copied}")

        # Последовательность users.id должна продолжиться после явных вставок.
        dst_conn.execute(text(
            "SELECT setval(pg_get_serial_sequence('users', 'id'), "
            "COALESCE((SELECT MAX(id) FROM users), 1))"
        ))

    print("Перенос завершён. Проверьте данные и запустите приложение.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Инициализация схемы БД и предопределённых ролей.

Вызывается из lifespan приложения: импорт пакета не должен писать в базу.
"""
import json
import logging
import uuid

from sqlalchemy import text

from app.db import Base, SessionLocal, engine, mapper_registry
from app.models import Role

logger = logging.getLogger(__name__)

def apply_migration():
    with engine.connect() as connection:
        result = connection.execute(text("PRAGMA table_info(folders)")).fetchall()
        columns = [row[1] for row in result]
        if 'parent_id' not in columns:
            connection.execute(text("ALTER TABLE folders ADD COLUMN parent_id VARCHAR(36)"))
        if 'team_id' not in columns:
            connection.execute(text("ALTER TABLE folders ADD COLUMN team_id VARCHAR(36)"))

        result = connection.execute(text("PRAGMA table_info(diagrams)")).fetchall()
        columns = [row[1] for row in result]
        if 'updated_at' not in columns:
            connection.execute(text("ALTER TABLE diagrams ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"))
        if 'folder_id' not in columns:
            connection.execute(text("ALTER TABLE diagrams ADD COLUMN folder_id VARCHAR(36)"))
        if 'team_id' not in columns:
            connection.execute(text("ALTER TABLE diagrams ADD COLUMN team_id VARCHAR(36)"))

        # Добавляем новые поля в таблицу users
        result = connection.execute(text("PRAGMA table_info(users)")).fetchall()
        columns = [row[1] for row in result]
        if 'position' not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN position VARCHAR(100)"))
        if 'company' not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN company VARCHAR(100)"))
        if 'website' not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN website VARCHAR(100)"))
        if 'about' not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN about TEXT"))

        result = connection.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='share_tokens'")).fetchall()
        if not result:
            connection.execute(text("""
                CREATE TABLE share_tokens (
                    id VARCHAR(36) PRIMARY KEY,
                    token VARCHAR(36) UNIQUE,
                    diagram_id VARCHAR(36),
                    created_at DATETIME,
                    expires_at DATETIME,
                    FOREIGN KEY (diagram_id) REFERENCES diagrams(id)
                )
            """))

        if not connection.dialect.has_table(connection, "teams"):
            connection.execute(text("""
                CREATE TABLE teams (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(100),
                    color VARCHAR(7),
                    owner_id INTEGER,
                    created_at DATETIME,
                    FOREIGN KEY (owner_id) REFERENCES users(id)
                )
            """))

        if not connection.dialect.has_table(connection, "password_reset_tokens"):
            connection.execute(text("""
                CREATE TABLE password_reset_tokens (
                    id VARCHAR(36) PRIMARY KEY,
                    email VARCHAR(100),
                    token VARCHAR(100) UNIQUE,
                    created_at DATETIME,
                    expires_at DATETIME,
                    used BOOLEAN DEFAULT 0
                )
            """))

        if not connection.dialect.has_table(connection, "roles"):
            connection.execute(text("""
                CREATE TABLE roles (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(50) UNIQUE,
                    permissions TEXT
                )
            """))
            predefined_roles = [
                ("admin", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": True}),
                ("editor", {"viewRegistry": True, "viewRoles": True, "editRegistry": True, "manageRoles": False}),
                ("viewer", {"viewRegistry": True, "viewRoles": True, "editRegistry": False, "manageRoles": False})
            ]
            for name, perms in predefined_roles:
                connection.execute(text("""
                    INSERT INTO roles (id, name, permissions)
                    VALUES (:id, :name, :perms)
                """), {
                    'id': str(uuid.uuid4()),
                    'name': name,
                    'perms': json.dumps(perms)
                })

        if not connection.dialect.has_table(connection, "team_members"):
            connection.execute(text("""
                CREATE TABLE team_members (
                    id VARCHAR(36) PRIMARY KEY,
                    team_id VARCHAR(36),
                    user_id INTEGER,
                    role_id VARCHAR(36),
                    FOREIGN KEY (team_id) REFERENCES teams(id),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (role_id) REFERENCES roles(id)
                )
            """))

        if not connection.dialect.has_table(connection, "invitations"):
            connection.execute(text("""
                CREATE TABLE invitations (
                    id VARCHAR(36) PRIMARY KEY,
                    team_id VARCHAR(36),
                    email VARCHAR(100),
                    token VARCHAR(100) UNIQUE,
                    role_id VARCHAR(36),
                    created_at DATETIME,
                    expires_at DATETIME,
                    status VARCHAR(20),
                    FOREIGN KEY (team_id) REFERENCES teams(id),
                    FOREIGN KEY (role_id) REFERENCES roles(id)
                )
            """))

        result = connection.execute(text("PRAGMA table_info(share_tokens)")).fetchall()
        columns = [row[1] for row in result]
        if 'can_edit' not in columns:
            connection.execute(text("ALTER TABLE share_tokens ADD COLUMN can_edit BOOLEAN DEFAULT 0"))

        connection.commit()

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

def run_schema_bootstrap() -> None:
    """Создаёт схему и доводит легаси-SQLite до актуального набора колонок."""
    Base.metadata.create_all(bind=engine)
    mapper_registry.configure()
    if engine.dialect.name == "sqlite":
        apply_migration()
    seed_default_roles()

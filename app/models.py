"""ORM-модели предметной области — единое описание схемы для Alembic."""
import json
import uuid
from datetime import timedelta

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from app.db import Base
from app.timeutils import utc_now

class Team(Base):
    __tablename__ = "teams"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100))
    color = Column(String(7))
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=utc_now)

    team_owner = relationship("User", back_populates="owned_teams")
    members = relationship("TeamMember", back_populates="team")
    invitations = relationship("Invitation", back_populates="team")
    diagrams = relationship("Diagram", back_populates="team")
    folders = relationship("Folder", back_populates="team", foreign_keys="[Folder.team_id]")

class TeamMember(Base):
    __tablename__ = "team_members"
    # Повторное приглашение того же человека в ту же команду — ошибка доступа,
    # а не вариант нормы: на легаси-базах дубликаты уже копились, пока
    # ограничение не стало частью схемы (см. миграцию 0003).
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),
    )
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String(36), ForeignKey("teams.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    role_id = Column(String(36), ForeignKey("roles.id"))

    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="team_memberships")
    role = relationship("Role")

class Role(Base):
    __tablename__ = "roles"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(50), unique=True)
    permissions = Column(Text)

    def get_permissions(self):
        return json.loads(self.permissions) if self.permissions else {}

class Invitation(Base):
    __tablename__ = "invitations"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String(36), ForeignKey("teams.id"))
    email = Column(String(100))
    token = Column(String(100), unique=True, index=True, default=lambda: str(uuid.uuid4()))
    role_id = Column(String(36), ForeignKey("roles.id"))
    created_at = Column(DateTime, default=utc_now)
    expires_at = Column(DateTime, default=lambda: utc_now() + timedelta(days=7))
    status = Column(String(20), default='pending')

    team = relationship("Team", back_populates="invitations")
    role = relationship("Role")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50))
    email = Column(String(100), unique=True, index=True)
    hashed_password = Column(String(100))
    # Добавляем новые поля
    position = Column(String(100), nullable=True)
    company = Column(String(100), nullable=True)
    website = Column(String(100), nullable=True)
    about = Column(Text, nullable=True)
    # Клеймо выпуска токенов: смена пароля инкрементит счётчик, и deps.py
    # отвергает JWT с более старым значением `tv`. В миграции 0003 у колонки
    # есть server_default — строки, вставленные в обход моделей, не должны
    # ложиться без клейма; в моделях дефолт питонский, как и в остальных
    # колонках (см. заметку в ревизии 0002).
    token_version = Column(Integer, nullable=False, default=0)

    diagrams = relationship("Diagram", back_populates="owner")
    folders = relationship("Folder", back_populates="owner")
    deleted_diagrams = relationship("DeletedDiagram", back_populates="owner")
    improvements = relationship("Improvement", back_populates="owner")
    owned_teams = relationship("Team", back_populates="team_owner")
    team_memberships = relationship("TeamMember", back_populates="user")

class Diagram(Base):
    __tablename__ = "diagrams"
    id = Column(String(36), primary_key=True, index=True)
    name = Column(String(100))
    xml_content = Column(Text)
    score = Column(Integer, default=0)
    # Индексы под горячие фильтры реестра: «мои схемы», «схемы команды»,
    # содержимое папки (миграция 0003 заводит их на существующих базах).
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    folder_id = Column(String(36), ForeignKey("folders.id"), nullable=True, index=True)
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=True, index=True)
    # Номер последней записанной версии: по нему улучшение понимает, что схема
    # ушла вперёд и предложение пора пересчитать.
    version_seq = Column(Integer, nullable=False, default=0)

    owner = relationship("User", back_populates="diagrams")
    folder = relationship("Folder", back_populates="diagrams")
    team = relationship("Team", back_populates="diagrams")
    share_tokens = relationship("ShareToken", back_populates="diagram")
    improvements = relationship("Improvement", back_populates="diagram")
    versions = relationship(
        "DiagramVersion", back_populates="diagram",
        cascade="all, delete-orphan", order_by="DiagramVersion.seq",
    )


class DiagramVersion(Base):
    """Снимок схемы. История не перезаписывается: откат — новая версия."""

    __tablename__ = "diagram_versions"
    __table_args__ = (UniqueConstraint("diagram_id", "seq", name="uq_diagram_versions_seq"),)

    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    diagram_id = Column(String(36), ForeignKey("diagrams.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    xml_content = Column(Text, nullable=False)
    score = Column(Integer, default=0)
    # created | saved | approved | restored
    source = Column(String(32), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"))
    note = Column(String(200))
    created_at = Column(DateTime, default=utc_now)

    diagram = relationship("Diagram", back_populates="versions")
    author = relationship("User")

class ShareToken(Base):
    __tablename__ = "share_tokens"
    id = Column(String(36), primary_key=True, index=True)
    token = Column(String(36), unique=True, index=True)
    diagram_id = Column(String(36), ForeignKey("diagrams.id"))
    created_at = Column(DateTime, default=utc_now)
    can_edit = Column(Boolean, default=False)
    expires_at = Column(DateTime)
    diagram = relationship("Diagram", back_populates="share_tokens")

class Folder(Base):
    __tablename__ = "folders"
    id = Column(String(36), primary_key=True, index=True)
    name = Column(String(100))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=True)
    parent_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    created_at = Column(DateTime, default=utc_now)
    owner = relationship("User", back_populates="folders")
    team = relationship("Team", back_populates="folders")
    diagrams = relationship("Diagram", back_populates="folder")
    children = relationship("Folder", backref="parent", remote_side=[id])

class DeletedDiagram(Base):
    __tablename__ = "deleted_diagrams"
    id = Column(String(36), primary_key=True, index=True)
    diagram_id = Column(String(36))
    name = Column(String(100))
    xml_content = Column(Text)
    user_id = Column(Integer, ForeignKey("users.id"))
    deleted_at = Column(DateTime, default=utc_now)
    owner = relationship("User", back_populates="deleted_diagrams")

class Improvement(Base):
    """Предложение улучшения и его судьба.

    Таблица осталась `pending_improvements`: переименование переименованием
    индексов в легаси-базах не сопровождается, а выгоды в запросах нет.
    Статус: pending → approved | rejected | superseded (новое предложение по
    той же диаграмме снимает с рассмотрения прежнее).
    """
    __tablename__ = "pending_improvements"
    id = Column(String(36), primary_key=True, index=True)
    # diagram_id — поиск открытых предложений по схеме, status — выборка
    # очереди на рассмотрение; оба без индекса читают таблицу целиком
    # (миграция 0003).
    diagram_id = Column(String(36), ForeignKey("diagrams.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    xml_content = Column(Text)
    recommendations = Column(Text)
    created_at = Column(DateTime, default=utc_now)
    status = Column(String(20), nullable=False, default="pending", index=True)
    # Версия схемы, к которой применимо предложение (см. Diagram.version_seq).
    base_seq = Column(Integer)
    decided_at = Column(DateTime)
    owner = relationship("User", back_populates="improvements")
    diagram = relationship("Diagram", back_populates="improvements")


class AiTask(Base):
    """Задача ИИ-контура: источник истины о статусе и результате.

    Redis здесь только транспорт (план
    docs/plans/redis-cache-and-task-routing.md, раздел 4.3): очередь может быть
    сброшена или потеряна, а пользовательская история задач и опрос статуса
    обязаны работать. Поэтому статус, входы и результат живут в базе, а в
    очередь уходит один идентификатор задачи.

    Статусы: queued → running → done | failed | cancelled. Отмена из queued
    мгновенная, из running — флагом `cancel_requested`, который воркер смотрит
    перед каждым обращением к модели: прерывать уже идущий HTTP-вызов значит
    оставить провайдера с оплаченной работой и без ответа.
    """

    __tablename__ = "ai_tasks"
    __table_args__ = (
        # Повторная отправка с тем же ключом обязана вернуть ту же задачу.
        # Ограничение в базе, а не `SETNX` в Redis: кэш можно сбросить, а
        # дубль задачи — это две оплаты одного вызова модели.
        UniqueConstraint("user_id", "idempotency_key", name="uq_ai_tasks_idempotency"),
    )

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # generate | improve | score
    kind = Column(String(32), nullable=False)
    # ai_llm | ai_local: очередь выбирается видом задачи (app/tasks.py)
    queue = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="queued", index=True)
    idempotency_key = Column(String(120), nullable=True)
    # Привязка к схеме: задача читает XML из базы, а не из аргументов, поэтому
    # здесь же фиксируется версия схемы на момент постановки — по ней воркер
    # понимает, что схему изменили, пока задача стояла в очереди.
    diagram_id = Column(String(36), ForeignKey("diagrams.id"), nullable=True, index=True)
    base_seq = Column(Integer, nullable=True)
    # Входы задачи. Для улучшения сохранённой схемы здесь пусто (воркер берёт
    # XML из `diagrams`); описание процесса и XML без схемы живут здесь, потому
    # что другого источника у них нет.
    payload = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    # Ссылка на доменный результат (например, id предложения улучшения): по ней
    # история задач связывается с историей схем без разбора JSON.
    result_ref = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    error_code = Column(String(32), nullable=True)
    tries = Column(Integer, nullable=False, default=0)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utc_now, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    owner = relationship("User")
    diagram = relationship("Diagram")


# Таблица для токенов сброса пароля
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(100), index=True)
    token = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=utc_now)
    expires_at = Column(DateTime, default=lambda: utc_now() + timedelta(hours=1))
    used = Column(Boolean, default=False)

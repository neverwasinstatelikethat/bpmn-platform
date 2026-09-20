"""ORM-модели предметной области — единое описание схемы для Alembic."""
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db import Base

class Team(Base):
    __tablename__ = "teams"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100))
    color = Column(String(7))
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    team_owner = relationship("User", back_populates="owned_teams")
    members = relationship("TeamMember", back_populates="team")
    invitations = relationship("Invitation", back_populates="team")
    diagrams = relationship("Diagram", back_populates="team")
    folders = relationship("Folder", back_populates="team", foreign_keys="[Folder.team_id]")

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String(36), ForeignKey("teams.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
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
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=7))
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

    diagrams = relationship("Diagram", back_populates="owner")
    folders = relationship("Folder", back_populates="owner")
    deleted_diagrams = relationship("DeletedDiagram", back_populates="owner")
    pending_improvements = relationship("PendingImprovement", back_populates="owner")
    owned_teams = relationship("Team", back_populates="team_owner")
    team_memberships = relationship("TeamMember", back_populates="user")

class Diagram(Base):
    __tablename__ = "diagrams"
    id = Column(String(36), primary_key=True, index=True)
    name = Column(String(100))
    xml_content = Column(Text)
    score = Column(Integer, default=0)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    folder_id = Column(String(36), ForeignKey("folders.id"), nullable=True)
    owner = relationship("User", back_populates="diagrams")
    folder = relationship("Folder", back_populates="diagrams")
    share_tokens = relationship("ShareToken", back_populates="diagram")
    pending_improvements = relationship("PendingImprovement", back_populates="diagram")
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=True)
    team = relationship("Team", back_populates="diagrams")

class ShareToken(Base):
    __tablename__ = "share_tokens"
    id = Column(String(36), primary_key=True, index=True)
    token = Column(String(36), unique=True, index=True)
    diagram_id = Column(String(36), ForeignKey("diagrams.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
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
    created_at = Column(DateTime, default=datetime.utcnow)
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
    deleted_at = Column(DateTime, default=datetime.utcnow)
    owner = relationship("User", back_populates="deleted_diagrams")

class PendingImprovement(Base):
    __tablename__ = "pending_improvements"
    id = Column(String(36), primary_key=True, index=True)
    diagram_id = Column(String(36), ForeignKey("diagrams.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    xml_content = Column(Text)
    recommendations = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    owner = relationship("User", back_populates="pending_improvements")
    diagram = relationship("Diagram", back_populates="pending_improvements")


# Таблица для токенов сброса пароля
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(100), index=True)
    token = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(hours=1))
    used = Column(Boolean, default=False)

"""Pydantic-контракты запросов и ответов API."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, validator

# Модели для восстановления пароля
class PasswordResetRequest(BaseModel):
    email: EmailStr

    @validator('email')
    def validate_email(cls, v):
        return v.lower()

class PasswordReset(BaseModel):
    token: str
    new_password: str

class PasswordResetResponse(BaseModel):
    success: bool
    message: str

# Pydantic модели (MySchemas)
class UserCreate(BaseModel):
    name: str
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    position: Optional[str] = None
    company: Optional[str] = None
    website: Optional[str] = None
    about: Optional[str] = None

class DiagramCreate(BaseModel):
    id: Optional[str] = None
    name: str
    xml: str
    score: int

class Token(BaseModel):
    access_token: str
    token_type: str

class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None
    team_id: Optional[str] = None

class FolderResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    parent_id: Optional[str] = None
    team_id: Optional[str] = None

class MoveToFolderRequest(BaseModel):
    diagram_id: str
    folder_id: Optional[str] = None

class RestoreDiagramRequest(BaseModel):
    diagram_id: str

class FolderDeleteRequest(BaseModel):
    folder_id: str
    delete_contents: bool = False

class ShareRequest(BaseModel):
    diagram_id: str
    can_edit: bool

class ShareResponse(BaseModel):
    share_link: str
    can_edit: bool

class ImproveRequest(BaseModel):
    bpmn_xml: str
    prompt: str
    diagram_id: Optional[str] = None

class AcceptImprovementRequest(BaseModel):
    improvement_id: str

class TeamCreate(BaseModel):
    name: str
    color: str = "#FF6347"

class InvitationCreate(BaseModel):
    email: str
    role: str

class DomainInviteCreate(BaseModel):
    domain: str
    role: str

class RoleCreate(BaseModel):
    name: str
    permissions: dict

class TeamResponse(BaseModel):
    id: str
    name: str
    color: str
    owner_id: int
    created_at: datetime

class InvitationResponse(BaseModel):
    id: str
    email: str
    role: str
    status: str
    created_at: datetime
    accept_link: Optional[str] = None

class RoleResponse(BaseModel):
    id: str
    name: str
    permissions: dict


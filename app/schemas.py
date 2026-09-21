"""Pydantic-контракты запросов и ответов API."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import AI_MAX_XML_CHARS
from core.bpmn_generator import MAX_TEXT_CHARS

# Подпись пользователя в улучшении: инвентарь схемы занимает контекст
# планировщика, поэтому запрос держим коротким осознанно.
AI_MAX_PROMPT_CHARS = 4000

# Модели для восстановления пароля
class PasswordResetRequest(BaseModel):
    email: EmailStr

    @field_validator('email')
    @classmethod
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
    # Границы совпадают с колонками models.Diagram: на PostgreSQL сверх лимита
    # даёт не 500 «string data right truncation», а честный 422.
    id: Optional[str] = Field(default=None, max_length=36)
    name: str = Field(..., min_length=1, max_length=100)
    xml: str = Field(..., min_length=1, max_length=AI_MAX_XML_CHARS)
    score: int = Field(..., ge=0, le=100)

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, v):
        if not v.strip():
            raise ValueError('Название схемы не должно быть пустым')
        return v.strip()

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
    can_edit: bool = False

class ShareResponse(BaseModel):
    share_link: str
    can_edit: bool

class ImproveRequest(BaseModel):
    # Лимиты взяты из одного места с генератором и конфигом: раньше роутер
    # держал свою копию лимита длины описания, и правка одного не замечалась
    # в другом.
    bpmn_xml: str = Field(..., min_length=1, max_length=AI_MAX_XML_CHARS)
    prompt: str = Field(..., min_length=1, max_length=AI_MAX_PROMPT_CHARS)
    diagram_id: Optional[str] = None

    @field_validator('prompt')
    @classmethod
    def prompt_not_blank(cls, v):
        if not v.strip():
            raise ValueError('Опишите, что нужно улучшить в схеме')
        return v.strip()

class GenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)

    @field_validator('text')
    @classmethod
    def text_not_blank(cls, v):
        if not v.strip():
            raise ValueError('Описание процесса не должно быть пустым')
        return v.strip()

class EvaluateRequest(BaseModel):
    bpmn_xml: str = Field(..., min_length=1, max_length=AI_MAX_XML_CHARS)

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


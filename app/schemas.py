"""Pydantic-контракты запросов и ответов API."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import AI_MAX_XML_CHARS
from core.bpmn_generator import MAX_TEXT_CHARS

# Подпись пользователя в улучшении: инвентарь схемы занимает контекст
# планировщика, поэтому запрос держим коротким осознанно.
AI_MAX_PROMPT_CHARS = 4000

# Границы полей аккаунта совпадают с колонками models.User: name — String(50),
# email, position, company, website — String(100). На PostgreSQL сверх лимита
# даёт не 500 «string data right truncation», а честный 422. Поле about — Text,
# поэтому его длины здесь нет: выдуманный лимит резал бы уже сохранённые анкеты.
USER_NAME_MAX = 50
USER_EMAIL_MAX = 100
USER_TEXT_FIELD_MAX = 100
# Тот же порог, что у маршрута сброса пароля: правило «минимум 8 символов»
# не должно зависеть от того, каким из двух маршрутов пользователь меняет пароль.
MIN_PASSWORD_CHARS = 8


def normalize_email(value: str) -> str:
    """Приводит почту к единственной форме: без пробелов и в нижнем регистре.

    Нормализация одна и на записи, и на чтении, иначе на PostgreSQL `==`
    с регистром различает «ivan@example.com» и «Ivan@example.com»: два ввода
    заводят второй аккаунт вместо одного, а сброс пароля lower-case'ит адрес
    и не находит его владельца — письмо не приходит, но ответ выглядит успехом.
    """
    return value.strip().lower()


class NormalizedEmailModel(BaseModel):
    """Поле почты с нормализацией: наследуют все входные модели аккаунта."""

    email: EmailStr = Field(..., max_length=USER_EMAIL_MAX)

    @field_validator("email")
    @classmethod
    def email_normalized(cls, v):
        return normalize_email(v)

# Модели для восстановления пароля
class PasswordResetRequest(NormalizedEmailModel):
    pass

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

class LoginRequest(NormalizedEmailModel):
    password: str


class RegisterRequest(NormalizedEmailModel):
    name: str = Field(..., min_length=1, max_length=USER_NAME_MAX)
    password: str


class ProfileUpdate(BaseModel):
    """Обновление профиля: только переданные поля, длины — из колонок User.

    Раньше маршрут принимал «сырой» dict и писал что попало прямо в сессию:
    чужой или кривой email ловил IntegrityError на 500, а число вместо имени
    превращалось в 500 же на PostgreSQL. Здесь непроходимые значения
    отсекаются на 422, а конфликт из-за занятой почты решает роутер понятным 400.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=USER_NAME_MAX)
    email: Optional[EmailStr] = Field(default=None, max_length=USER_EMAIL_MAX)
    position: Optional[str] = Field(default=None, max_length=USER_TEXT_FIELD_MAX)
    company: Optional[str] = Field(default=None, max_length=USER_TEXT_FIELD_MAX)
    website: Optional[str] = Field(default=None, max_length=USER_TEXT_FIELD_MAX)
    about: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=MIN_PASSWORD_CHARS)

    @field_validator("email")
    @classmethod
    def email_normalized(cls, v):
        return None if v is None else normalize_email(v)


class ProfileResponse(BaseModel):
    """Профиль после записи.

    `access_token` выдаётся, когда меняется email или пароль: `sub` токена —
    это почта, а `tv` — клеймо версии, поэтому прежний токен после такой правки
    перестаёт действовать сам собой. Клиент, который сохранит новый токен,
    не отвалится; старый без него честно получит 401.
    """

    id: int
    name: Optional[str] = None
    email: str
    position: Optional[str] = None
    company: Optional[str] = None
    website: Optional[str] = None
    about: Optional[str] = None
    access_token: Optional[str] = None
    token_type: Optional[str] = None
    token_rotated: bool = False

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


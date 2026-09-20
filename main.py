from fastapi import FastAPI, Depends, HTTPException, status, Body, UploadFile, File, Form, Query, BackgroundTasks, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, validator
from typing import List, Optional
from datetime import datetime, timedelta
import uuid
import bcrypt
import jwt
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey, DateTime, Boolean, and_, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload
from sqlalchemy.sql import text
from sqlalchemy.orm import registry
import tempfile
import os
import asyncio
from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import BPMNScorer
from fastapi.middleware.cors import CORSMiddleware
import logging
import xml.etree.ElementTree as ET
from core.llm_improve import BPMNImprovementOrchestrator, ImprovementError
import json
import re
import secrets

logger = logging.getLogger(__name__)

# Конфигурация (значения берутся из переменных окружения, см. .env.example)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bpmn.db")
_env_secret_key = os.getenv("SECRET_KEY")
if not _env_secret_key:
    if DATABASE_URL.startswith("sqlite"):
        logger.warning("SECRET_KEY не задан: используется ключ разработки (допустимо только для локального SQLite)")
        _env_secret_key = "dev-secret-key-change-me"
    else:
        raise RuntimeError("Переменная окружения SECRET_KEY обязательна при работе с PostgreSQL")
SECRET_KEY = _env_secret_key
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
# Порты 8000/3000 на рабочей машине заняты сторонними проектами (Grafana и др.)
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8765"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3456")
if os.getenv("SKIP_LLM_INIT") == "1":
    # Служебный режим (например, перенос данных): ИИ-эндпоинты недоступны.
    logger.warning("Инициализация LLM-оркестратора пропущена (SKIP_LLM_INIT=1)")
    bpmn_processor = None
else:
    bpmn_processor = BPMNImprovementOrchestrator()

# Инициализация FastAPI
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Инициализация БД
Base = declarative_base()
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
mapper_registry = registry()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# Настройка почты: значения берутся из переменных окружения (см. .env.example)
email_conf = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    # MAIL_FROM валидируется как e-mail: без значения приложение падало ещё на
    # импорте. Фолбэк совпадает с документированным значением из .env.example.
    MAIL_FROM=os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME") or "no-reply@vkusvill.ru"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", "465")),
    MAIL_SERVER=os.getenv("MAIL_SERVER", "imap.yandex.ru"),
    MAIL_STARTTLS=os.getenv("MAIL_STARTTLS", "true").lower() == "true",
    MAIL_SSL_TLS=os.getenv("MAIL_SSL_TLS", "false").lower() == "true",
    USE_CREDENTIALS=True
)

fast_mail = FastMail(email_conf)

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

# Таблица для токенов сброса пароля
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(100), index=True)
    token = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(hours=1))
    used = Column(Boolean, default=False)

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

# create_all идёт первым: на чистой базе он создаёт таблицы моделей, и только
# потом apply_migration добавляет отсутствующие колонки в легаси-SQLite.
# На PostgreSQL схема полностью описана моделями, PRAGMA-миграция не нужна.
Base.metadata.create_all(bind=engine)
mapper_registry.configure()
if engine.dialect.name == "sqlite":
    apply_migration()
seed_default_roles()

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

# Утилиты
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

def get_password_hash(password: str):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user

@app.post("/api/register", response_model=UserResponse)
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == request.email).first()
    if existing_user:
        raise HTTPException(400, detail="Email уже зарегистрирован")
    
    hashed_password = get_password_hash(request.password)
    db_user = User(
        name=request.name,
        email=request.email,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    return db_user

@app.post("/api/login", response_model=Token)
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверные учетные данные")
    
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/me", response_model=UserResponse)
async def get_current_user_endpoint(current_user: User = Depends(get_current_user)):
    return current_user

# Роуты для работы с диаграммами
@app.post("/api/diagrams")
def save_diagram(
    diagram: DiagramCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram_id = diagram.id or str(uuid.uuid4())
    existing_diagram = db.query(Diagram).filter(Diagram.id == diagram_id).first()
    
    if existing_diagram:
        existing_diagram.name = diagram.name
        existing_diagram.xml_content = diagram.xml
        existing_diagram.score = diagram.score
        existing_diagram.updated_at = datetime.utcnow()
    else:
        db_diagram = Diagram(
            id=diagram_id,
            name=diagram.name,
            xml_content=diagram.xml,
            score=diagram.score,
            user_id=current_user.id
        )
        db.add(db_diagram)
    
    db.commit()
    return {"status": "success", "diagram_id": diagram_id}

@app.get("/api/diagrams")
def get_user_diagrams(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagrams = db.query(Diagram).filter(Diagram.user_id == current_user.id).all()
    return [{
        "id": d.id,
        "name": d.name,
        "score": d.score,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "folder_id": d.folder_id
    } for d in diagrams]

@app.get("/api/diagrams/{diagram_id}")
def get_diagram(
    diagram_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = db.query(Diagram).filter(
        Diagram.id == diagram_id,
        or_(
            Diagram.user_id == current_user.id,
            Diagram.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).first()
    
    if not diagram:
        raise HTTPException(404, detail="Diagram not found or access denied")
    
    return {
        "id": diagram.id,
        "name": diagram.name,
        "xml_content": diagram.xml_content
    }

@app.delete("/api/diagrams/{diagram_id}")
def delete_diagram(
    diagram_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = db.query(Diagram).filter(
        Diagram.id == diagram_id,
        or_(
            Diagram.user_id == current_user.id,
            and_(
                Diagram.team_id.in_(
                    db.query(TeamMember.team_id).filter(
                        TeamMember.user_id == current_user.id,
                        TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                    )
                )
            )
        )
    ).first()
    
    if not diagram:
        raise HTTPException(404, detail="Diagram not found or access denied")
    
    deleted_diagram = DeletedDiagram(
        id=str(uuid.uuid4()),
        diagram_id=diagram.id,
        name=diagram.name,
        xml_content=diagram.xml_content,
        user_id=current_user.id
    )
    db.add(deleted_diagram)
    db.delete(diagram)
    db.commit()
    return {"status": "success"}

@app.post("/api/folders")
def create_folder(
    folder: FolderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if folder.team_id:
        team = db.query(Team).filter(
            Team.id == folder.team_id,
            or_(
                Team.owner_id == current_user.id,
                Team.members.any(and_(
                    TeamMember.user_id == current_user.id,
                    TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                ))
            )
        ).first()
        if not team:
            raise HTTPException(status_code=403, detail="Access denied to create team folder")
        if folder.parent_id:
            parent_folder = db.query(Folder).filter(
                Folder.id == folder.parent_id,
                Folder.team_id == folder.team_id
            ).first()
            if not parent_folder:
                raise HTTPException(status_code=400, detail="Parent folder not found in team")
    else:
        if folder.parent_id:
            parent_folder = db.query(Folder).filter(
                Folder.id == folder.parent_id,
                Folder.user_id == current_user.id
            ).first()
            if not parent_folder:
                raise HTTPException(status_code=400, detail="Parent folder not found")

    folder_id = str(uuid.uuid4())
    db_folder = Folder(
        id=folder_id,
        name=folder.name,
        user_id=current_user.id if not folder.team_id else None,
        team_id=folder.team_id,
        parent_id=folder.parent_id,
        created_at=datetime.utcnow()
    )
    db.add(db_folder)
    db.commit()
    return {"status": "success", "folder_id": folder_id}

@app.get("/api/folders")
def get_user_folders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    folders = db.query(Folder).filter(
        or_(
            Folder.user_id == current_user.id,
            Folder.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).all()
    return [{
        "id": f.id,
        "name": f.name,
        "created_at": f.created_at,
        "parent_id": f.parent_id,
        "team_id": f.team_id
    } for f in folders]

@app.delete("/api/folders/{folder_id}")
def delete_folder(
    folder_id: str,
    delete_contents: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    folder = db.query(Folder).filter(
        Folder.id == folder_id,
        or_(
            and_(Folder.user_id == current_user.id, Folder.team_id.is_(None)),
            and_(
                Folder.team_id.in_(
                    db.query(TeamMember.team_id).filter(
                        TeamMember.user_id == current_user.id,
                        TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                    )
                ),
                Folder.team_id.isnot(None)
            )
        )
    ).first()
    
    if not folder:
        raise HTTPException(404, detail="Folder not found")
    
    if delete_contents:
        db.query(Diagram).filter(
            Diagram.folder_id == folder_id
        ).delete()
    
    db.query(Folder).filter(Folder.parent_id == folder_id).delete()
    db.delete(folder)
    db.commit()
    return {"status": "success"}

@app.get("/api/folders/{folder_id}/diagrams")
def get_folder_diagrams(
    folder_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagrams = db.query(Diagram).filter(
        Diagram.folder_id == folder_id,
        or_(
            Diagram.user_id == current_user.id,
            Diagram.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).all()
    return [{
        "id": d.id,
        "name": d.name,
        "created_at": d.created_at
    } for d in diagrams]

@app.get("/api/folders/tree")
def get_folder_tree(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    folders = db.query(Folder).filter(
        or_(
            Folder.user_id == current_user.id,
            Folder.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).all()
    
    def build_tree(parent_id=None):
        tree = []
        for folder in folders:
            if folder.parent_id == parent_id and (folder.user_id == current_user.id or folder.team_id in [tm.team_id for tm in current_user.team_memberships]):
                folder_data = {
                    "id": folder.id,
                    "name": folder.name,
                    "parent_id": folder.parent_id,
                    "created_at": folder.created_at,
                    "team_id": folder.team_id,
                    "children": build_tree(folder.id)
                }
                tree.append(folder_data)
        return tree
    
    return build_tree()

@app.post("/api/diagrams/move-to-folder")
def move_to_folder(
    request: MoveToFolderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = db.query(Diagram).filter(
        Diagram.id == request.diagram_id,
        or_(
            Diagram.user_id == current_user.id,
            Diagram.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).first()
    
    if not diagram:
        raise HTTPException(404, detail="Diagram not found")
    
    folder = db.query(Folder).filter(
        Folder.id == request.folder_id,
        or_(
            and_(Folder.user_id == current_user.id, Folder.team_id.is_(None)),
            and_(Folder.team_id == diagram.team_id, Folder.team_id.isnot(None))
        )
    ).first()
    
    if not folder and request.folder_id:
        raise HTTPException(404, detail="Folder not found")
    
    diagram.folder_id = request.folder_id
    db.commit()
    return {"status": "success"}

@app.post("/api/import-bpmn")
async def import_bpmn(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        logger.info(f"Starting import for file: {file.filename}")
        
        if not file.filename.lower().endswith(('.bpmn', '.xml')):
            error_msg = "Разрешены только файлы .bpmn и .xml"
            logger.warning(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)

        contents = await file.read()
        logger.info(f"File size: {len(contents)} bytes")
        
        try:
            xml_content = contents.decode('utf-8')
        except UnicodeDecodeError:
            error_msg = "Файл должен быть в UTF-8 кодировке"
            logger.warning(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)
        
        try:
            root = ET.fromstring(xml_content)
            if not root.tag.endswith('}definitions'):
                error_msg = "Файл не содержит BPMN definitions"
                logger.warning(error_msg)
                raise HTTPException(status_code=400, detail=error_msg)
        except ET.ParseError as e:
            error_msg = f"Ошибка парсинга XML: {str(e)}"
            logger.warning(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)

        diagram_id = str(uuid.uuid4())
        db_diagram = Diagram(
            id=diagram_id,
            name=file.filename.replace('.bpmn', '').replace('.xml', ''),
            xml_content=xml_content,
            user_id=current_user.id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(db_diagram)
        db.commit()
        
        logger.info(f"Successfully imported diagram: {diagram_id}")
        
        return {
            "status": "success",
            "diagram_id": diagram_id,
            "name": db_diagram.name
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Неожиданная ошибка: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.get("/api/deleted-diagrams")
def get_deleted_diagrams(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagrams = db.query(DeletedDiagram).filter(
        DeletedDiagram.user_id == current_user.id
    ).all()
    return [{
        "id": d.id,
        "diagram_id": d.diagram_id,
        "name": d.name,
        "deleted_at": d.deleted_at
    } for d in diagrams]

@app.post("/api/deleted-diagrams/restore")
def restore_diagram(
    request: RestoreDiagramRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    deleted_diagram = db.query(DeletedDiagram).filter(
        DeletedDiagram.id == request.diagram_id,
        DeletedDiagram.user_id == current_user.id
    ).first()
    
    if not deleted_diagram:
        raise HTTPException(404, detail="Diagram not found in trash")
    
    diagram = Diagram(
        id=deleted_diagram.diagram_id,
        name=deleted_diagram.name,
        xml_content=deleted_diagram.xml_content,
        user_id=current_user.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(diagram)
    
    db.delete(deleted_diagram)
    db.commit()
    return {"status": "success"}

@app.post("/api/share", response_model=ShareResponse)
def create_share_link(
    request: ShareRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = db.query(Diagram).filter(
        Diagram.id == request.diagram_id,
        or_(
            Diagram.user_id == current_user.id,
            and_(
                Diagram.team_id.in_(
                    db.query(TeamMember.team_id).filter(
                        TeamMember.user_id == current_user.id,
                        TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                    )
                )
            )
        )
    ).first()
    
    if not diagram:
        raise HTTPException(404, detail="Diagram not found or access denied")

    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=7)
    share_token = ShareToken(
        id=str(uuid.uuid4()),
        token=token,
        diagram_id=diagram.id,
        created_at=datetime.utcnow(),
        expires_at=expires_at,
        can_edit=request.can_edit
    )
    db.add(share_token)
    db.commit()
    
    share_link = f"{FRONTEND_URL}/share/{token}"
    return {
        "share_link": share_link,
        "can_edit": request.can_edit
    }

@app.get("/api/share/{share_token}")
def get_shared_diagram(
    share_token: str,
    db: Session = Depends(get_db)
):
    token = db.query(ShareToken).filter(
        ShareToken.token == share_token,
        ShareToken.expires_at > datetime.utcnow()
    ).first()
    
    if not token:
        raise HTTPException(404, detail="Share link is invalid or expired")
    
    diagram = db.query(Diagram).filter(Diagram.id == token.diagram_id).first()
    if not diagram:
        raise HTTPException(404, detail="Diagram not found")

    return {
        "id": diagram.id,
        "name": diagram.name,
        "xml_content": diagram.xml_content,
        "can_edit": token.can_edit,
        "owner_id": diagram.user_id
    }

generator = BPMNGenerator()
scorer = BPMNScorer()

GENERATE_TEXT_LIMIT = 10_000

@app.post("/api/generate")
async def generate_bpmn(
    data: dict = Body(...),
    current_user: User = Depends(get_current_user)
):
    text = data.get("text")
    if not text or not isinstance(text, str):
        raise HTTPException(400, detail="Text parameter is required")
    if len(text) > GENERATE_TEXT_LIMIT:
        raise HTTPException(
            400,
            detail=f"Описание слишком длинное (максимум {GENERATE_TEXT_LIMIT} символов)"
        )

    gen_result = await asyncio.to_thread(generator.generate, text)
    if gen_result["status"] != "success":
        raise HTTPException(400, detail=gen_result.get("error", "Generation failed"))

    return {
        "status": "success",
        "bpmn": gen_result["bpmn"],
        "structure": gen_result.get("structure", {})
    }

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "version": "1.2.0",
        "features": ["bpmn_generation", "voice_input", "validation"]
    }

@app.post("/api/evaluate")
async def evaluate_bpmn(
    data: dict = Body(...),
    current_user: User = Depends(get_current_user)
):
    try:
        bpmn_xml = data.get("bpmn_xml")
        if not bpmn_xml:
            raise HTTPException(400, detail="BPMN XML is required")

        result = scorer.evaluate(bpmn_xml)
        return result
    except Exception as e:
        raise HTTPException(500, detail=str(e))

@app.post("/api/export")
async def export_bpmn(
    data: dict = Body(...),
    current_user: User = Depends(get_current_user)
):
    bpmn_xml = data.get("bpmn_xml")
    format = data.get("format")
    if format in ['png', 'pdf']:
        return {"status": "success", "data": bpmn_xml.encode()}
    return {"status": "error", "message": "Unsupported format"}


IMPROVE_XML_LIMIT = 1_000_000

@app.post("/api/ai/improve")
async def improve_diagram_endpoint(
    request: ImproveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if bpmn_processor is None:
        raise HTTPException(503, detail="Сервис улучшения отключён (SKIP_LLM_INIT=1)")
    if not request.bpmn_xml or len(request.bpmn_xml) > IMPROVE_XML_LIMIT:
        raise HTTPException(
            400,
            detail="BPMN XML отсутствует или превышает 1 МБ"
        )
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(400, detail="Prompt is required")

    try:
        recommendations, improved_xml, report = await bpmn_processor.improve_diagram(
            xml_content=request.bpmn_xml,
            user_prompt=request.prompt
        )
    except ImprovementError as e:
        logger.warning("Улучшение диаграммы %s отклонено: %s", request.diagram_id, e)
        raise HTTPException(400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — наружу только 500
        logger.error("Критическая ошибка улучшения диаграммы %s: %s",
                     request.diagram_id, e, exc_info=True)
        raise HTTPException(500, detail="Внутренняя ошибка сервера при улучшении диаграммы")

    if improved_xml is None:
        # Только анализ: изменений нет, сохранять нечего.
        logger.info("Диаграмма %s: только анализ без изменений", request.diagram_id)
        return {
            "status": "analysis_only",
            "recommendations": recommendations,
            "report": report,
        }

    improvement_id = str(uuid.uuid4())
    pending_improvement = PendingImprovement(
        id=improvement_id,
        diagram_id=request.diagram_id,
        user_id=current_user.id,
        xml_content=improved_xml,
        recommendations=recommendations,
        created_at=datetime.utcnow()
    )
    db.add(pending_improvement)
    db.commit()
    logger.info("Диаграмма %s улучшена, улучшение %s", request.diagram_id, improvement_id)

    return {
        "status": "success",
        "improvement_id": improvement_id,
        "recommendations": recommendations,
        "report": report,
    }

@app.post("/api/ai/accept-improvement")
async def accept_improvement(
    request: AcceptImprovementRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    improvement = db.query(PendingImprovement).filter(
        PendingImprovement.id == request.improvement_id,
        PendingImprovement.user_id == current_user.id
    ).first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")
    
    if improvement.diagram_id:
        diagram = db.query(Diagram).filter(
            Diagram.id == improvement.diagram_id,
            or_(
                Diagram.user_id == current_user.id,
                and_(
                    Diagram.team_id.in_(
                        db.query(TeamMember.team_id).filter(
                            TeamMember.user_id == current_user.id,
                            TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                        )
                    )
                )
            )
        ).first()
        if not diagram:
            raise HTTPException(404, detail="Diagram not found or access denied")
        diagram.xml_content = improvement.xml_content
        diagram.updated_at = datetime.utcnow()
    else:
        diagram_id = str(uuid.uuid4())
        diagram = Diagram(
            id=diagram_id,
            name="Улучшенная диаграмма",
            xml_content=improvement.xml_content,
            user_id=current_user.id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(diagram)
    
    # Фиксируем значения до удаления: после delete+commit объект отцеплен
    # от сессии (expire_on_commit), чтение атрибутов падает с DetachedInstanceError.
    saved_xml = improvement.xml_content
    saved_diagram_id = improvement.diagram_id
    db.delete(improvement)
    db.commit()
    return {
        "status": "success",
        "diagram_id": saved_diagram_id or diagram.id,
        "xml_content": saved_xml
    }

@app.post("/api/teams", response_model=TeamResponse)
def create_team(
    team_data: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db_team = Team(
        id=str(uuid.uuid4()),
        name=team_data.name,
        color=team_data.color,
        owner_id=current_user.id
    )
    db.add(db_team)
    
    admin_role = db.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        raise HTTPException(500, "Admin role not configured")
    
    member = TeamMember(
        team_id=db_team.id,
        user_id=current_user.id,
        role_id=admin_role.id
    )
    db.add(member)
    db.commit()
    return db_team

@app.get("/api/teams", response_model=List[TeamResponse])
def get_user_teams(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    teams = db.query(Team).filter(
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).all()
    return teams

@app.delete("/api/teams/{team_id}")
def delete_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(404, "Team not found or access denied")
    
    db.query(TeamMember).filter(TeamMember.team_id == team_id).delete()
    db.query(Invitation).filter(Invitation.team_id == team_id).delete()
    db.query(Diagram).filter(Diagram.team_id == team_id).update({"team_id": None})
    db.delete(team)
    db.commit()
    return {"status": "success"}

@app.post("/api/teams/{team_id}/invite", response_model=InvitationResponse)
def create_invitation(
    team_id: str,
    invite_data: InvitationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).options(joinedload(Team.members)).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(and_(
                TeamMember.user_id == current_user.id,
                TeamMember.role.has(Role.name == "admin")
            ))
        )
    ).first()
    
    if not team:
        raise HTTPException(403, "Access denied")
    
    role = db.query(Role).filter(Role.name == invite_data.role).first()
    if not role:
        raise HTTPException(400, "Invalid role specified")
    
    invitation = Invitation(
        team_id=team_id,
        email=invite_data.email,
        role_id=role.id
    )
    db.add(invitation)
    db.commit()
    
    accept_link = f"{FRONTEND_URL}/invite/{invitation.token}"
    
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": invite_data.role,
        "status": invitation.status,
        "created_at": invitation.created_at,
        "accept_link": accept_link
    }

@app.post("/api/teams/{team_id}/invite-domain")
def domain_invite(
    team_id: str,
    domain_data: DomainInviteCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(403, "Access denied")
    
    role = db.query(Role).filter(Role.name == domain_data.role).first()
    if not role:
        raise HTTPException(400, "Invalid role specified")
    
    domain_regex = f"%@{re.escape(domain_data.domain)}"
    users = db.query(User).filter(User.email.like(domain_regex)).all()
    
    for user in users:
        existing = db.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user.id
        ).first()
        
        if not existing:
            member = TeamMember(
                team_id=team_id,
                user_id=user.id,
                role_id=role.id
            )
            db.add(member)
    
    db.commit()
    return {"status": "success", "users_added": len(users)}

@app.get("/api/invitations/accept/{token}", response_model=InvitationResponse)
def accept_invitation(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    invitation = db.query(Invitation).filter(
        Invitation.token == token,
        Invitation.status == "pending",
        Invitation.expires_at > datetime.utcnow()
    ).first()
    
    if not invitation:
        raise HTTPException(404, "Invitation not found or expired")
    
    existing = db.query(TeamMember).filter(
        TeamMember.team_id == invitation.team_id,
        TeamMember.user_id == current_user.id
    ).first()
    
    if existing:
        invitation.status = "accepted"
        db.commit()
        return invitation
    
    member = TeamMember(
        team_id=invitation.team_id,
        user_id=current_user.id,
        role_id=invitation.role_id
    )
    db.add(member)
    
    invitation.status = "accepted"
    db.commit()
    
    return invitation

@app.post("/api/roles", response_model=RoleResponse)
def create_role(
    role_data: RoleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    existing = db.query(Role).filter(Role.name == role_data.name).first()
    if existing:
        raise HTTPException(400, "Role name already exists")
    
    role = Role(
        name=role_data.name,
        permissions=json.dumps(role_data.permissions)
    )
    db.add(role)
    db.commit()
    return role

@app.get("/api/roles", response_model=List[RoleResponse])
def get_roles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    roles = db.query(Role).all()
    return [{
        "id": r.id,
        "name": r.name,
        "permissions": json.loads(r.permissions) if r.permissions else {}
    } for r in roles]

@app.post("/api/diagrams/share-to-team")
def share_to_team(
    diagram_id: str = Body(...),
    team_id: str = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = db.query(Diagram).filter(
        Diagram.id == diagram_id,
        or_(
            Diagram.user_id == current_user.id,
            and_(
                Diagram.team_id.in_(
                    db.query(TeamMember.team_id).filter(
                        TeamMember.user_id == current_user.id,
                        TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                    )
                )
            )
        )
    ).first()
    
    if not diagram:
        raise HTTPException(404, "Diagram not found or access denied")
    
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, "Team not found")
    
    # Проверка прав: владелец команды или участник с editRegistry
    has_access = (
        team.owner_id == current_user.id or
        db.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == current_user.id,
            TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
        ).first() is not None
    )
    if not has_access:
        raise HTTPException(403, "Insufficient permissions in team")
    
    diagram.team_id = team_id
    db.commit()
    return {"status": "success"}

@app.get("/api/teams/{team_id}/diagrams")
def get_team_diagrams(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Team not found")
    
    # Проверяем права доступа (разрешаем владельцу команды и участникам с правами просмотра)
    has_access = (
        team.owner_id == current_user.id or
        db.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == current_user.id
        ).first() is not None
    )
    
    if not has_access:
        raise HTTPException(403, detail="Access denied")
    
    diagrams = db.query(Diagram).filter(Diagram.team_id == team_id).all()
    return [{
        "id": d.id,
        "name": d.name,
        "score": d.score,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "folder_id": d.folder_id,
        "team_id": d.team_id
    } for d in diagrams]


# Эндпоинт для получения информации о команде
@app.get("/api/teams/{team_id}")
def get_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).first()
    
    if not team:
        raise HTTPException(404, detail="Team not found or access denied")
    
    owner = db.query(User).filter(User.id == team.owner_id).first()
    members_count = db.query(TeamMember).filter(TeamMember.team_id == team_id).count()
    diagrams_count = db.query(Diagram).filter(Diagram.team_id == team_id).count()
    
    return {
        "id": team.id,
        "name": team.name,
        "color": team.color,
        "owner_id": team.owner_id,
        "owner_name": owner.name if owner else "Unknown",
        "owner_email": owner.email if owner else "Unknown",
        "members_count": members_count,
        "diagrams_count": diagrams_count,
        "created_at": team.created_at
    }

@app.put("/api/teams/{team_id}", response_model=TeamResponse)
def update_team(
    team_id: str,
    team_data: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(404, "Team not found or access denied")
    
    team.name = team_data.name
    team.color = team_data.color
    db.commit()
    return team

# Эндпоинт для получения роли пользователя в команде
@app.get("/api/teams/{team_id}/role")
def get_user_role_in_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Проверяем, что команда существует
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Команда не найдена")
    
    # Если пользователь - владелец команды
    if team.owner_id == current_user.id:
        return {"roleName": "admin"}
    
    # Иначе ищем роль в участниках команды
    member = db.query(TeamMember).filter(
        TeamMember.team_id == team_id,
        TeamMember.user_id == current_user.id
    ).first()
    
    if not member:
        raise HTTPException(403, detail="Доступ запрещен")
    
    return {"roleName": member.role.name}

# Эндпоинт для обновления профиля
@app.put("/api/profile")
def update_profile(
    profile_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    
    # Обновляем данные профиля
    if "name" in profile_data:
        user.name = profile_data["name"]
    if "email" in profile_data:
        user.email = profile_data["email"]
    if "position" in profile_data:
        user.position = profile_data["position"]
    if "company" in profile_data:
        user.company = profile_data["company"]
    if "website" in profile_data:
        user.website = profile_data["website"]
    if "about" in profile_data:
        user.about = profile_data["about"]
    if "password" in profile_data and profile_data["password"]:
        user.hashed_password = get_password_hash(profile_data["password"])
    
    db.commit()
    
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "position": user.position,
        "company": user.company,
        "website": user.website,
        "about": user.about
    }

# Эндпоинт для получения участников команды
@app.get("/api/teams/{team_id}/members")
def get_team_members(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).first()
    
    if not team:
        raise HTTPException(404, detail="Team not found or access denied")
    
    members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
    result = []
    for member in members:
        user = db.query(User).filter(User.id == member.user_id).first()
        role = db.query(Role).filter(Role.id == member.role_id).first()
        result.append({
            "id": member.id,
            "user_id": member.user_id,
            "user_name": user.name if user else "Unknown",
            "user_email": user.email if user else "Unknown",
            "role": role.name if role else "Unknown"
        })
    
    return result


# Эндпоинт для запроса сброса пароля
@app.post("/api/reset-password-request", response_model=PasswordResetResponse)
async def request_password_reset(
    request: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    try:
        # Проверяем, существует ли пользователь с таким email
        user = db.query(User).filter(User.email == request.email.lower()).first()
        
        if not user:
            # Возвращаем успех даже если пользователь не найден (безопасность)
            return PasswordResetResponse(
                success=True,
                message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
            )
        
        # Удаляем предыдущие неиспользованные токены для этого email
        db.query(PasswordResetToken).filter(
            PasswordResetToken.email == request.email.lower(),
            PasswordResetToken.used == False
        ).delete()
        
        # Генерируем новый токен
        token = secrets.token_urlsafe(32)
        reset_token = PasswordResetToken(
            email=request.email.lower(),
            token=token,
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        db.add(reset_token)
        db.commit()
        
        # Формируем ссылку для сброса пароля
        reset_link = f"{FRONTEND_URL}/reset-password?token={token}"
        
        # Создаем email сообщение
        message = MessageSchema(
            subject="Восстановление пароля - ВкусВилл BPMN",
            recipients=[request.email],
            html=f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #00A550 0%, #F62369 100%); padding: 30px; text-align: center; border-radius: 10px 10px 0 0;">
                    <h1 style="color: white; margin: 0; font-size: 28px;">ВкусВилл BPMN</h1>
                </div>
                <div style="background: white; padding: 40px; border-radius: 0 0 10px 10px; box-shadow: 0 5px 20px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; margin-top: 0;">Восстановление пароля</h2>
                    <p style="color: #666; line-height: 1.6;">
                        Здравствуйте!<br><br>
                        Вы получили это письмо, потому что был запрошен сброс пароля для вашей учетной записи в ВкусВилл BPMN.<br><br>
                        Для сброса пароля нажмите на кнопку ниже:
                    </p>
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{reset_link}" style="
                            background: linear-gradient(135deg, #00A550 0%, #F62369 100%);
                            color: white;
                            padding: 15px 30px;
                            text-decoration: none;
                            border-radius: 50px;
                            font-weight: 600;
                            display: inline-block;
                        ">Сбросить пароль</a>
                    </div>
                    <p style="color: #666; line-height: 1.6;">
                        Или скопируйте и вставьте следующую ссылку в браузер:<br>
                        <a href="{reset_link}" style="color: #00A550;">{reset_link}</a>
                    </p>
                    <p style="color: #666; line-height: 1.6;">
                        Эта ссылка действительна в течение 1 часа.<br><br>
                        Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.<br><br>
                        С уважением,<br>
                        Команда ВкусВилл BPMN
                    </p>
                </div>
            </div>
            """,
            subtype="html"
        )
        
        # Отправляем email в фоне
        background_tasks.add_task(fast_mail.send_message, message)
        
        return PasswordResetResponse(
            success=True,
            message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
        )
        
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset request: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при отправке запроса на восстановление пароля"
        )

# Эндпоинт для сброса пароля
@app.post("/api/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    request: PasswordReset,
    db: Session = Depends(get_db)
):
    try:
        # Ищем токен в базе данных
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == request.token,
            PasswordResetToken.used == False,
            PasswordResetToken.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Недействительный или просроченный токен сброса пароля"
            )
        
        # Находим пользователя по email из токена
        user = db.query(User).filter(User.email == reset_token.email).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Пользователь не найден"
            )
        
        # Валидация нового пароля
        if len(request.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пароль должен содержать минимум 8 символов"
            )
        
        # Обновляем пароль пользователя
        user.hashed_password = get_password_hash(request.new_password)
        
        # Помечаем токен как использованный
        reset_token.used = True
        
        db.commit()
        
        return PasswordResetResponse(
            success=True,
            message="Пароль успешно изменен"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при сбросе пароля"
        )

# Эндпоинт для проверки токена
@app.post("/api/verify-reset-token")
async def verify_reset_token(
    token: str,
    db: Session = Depends(get_db)
):
    try:
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,
            PasswordResetToken.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_token:
            return {"valid": False}
        
        return {"valid": True}
        
    except Exception as e:
        logger.error("Error verifying reset token: %s", e, exc_info=True)
        return {"valid": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)

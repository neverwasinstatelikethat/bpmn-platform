"""Реестр диаграмм: сохранение, просмотр, удаление, импорт."""
import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
from fastapi import (APIRouter, BackgroundTasks, Body, Depends, File, Form,
                     HTTPException, Query, UploadFile, status)
from fastapi_mail import MessageSchema
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.ai import generator, get_orchestrator, scorer
from app.config import (ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, BACKEND_PORT,
                        DATABASE_URL, FRONTEND_URL, SECRET_KEY)
from app.db import get_db
from app.deps import get_current_user
from app.mailer import email_conf, fast_mail
from app.models import (DeletedDiagram, Diagram, Folder, Invitation,
                        PasswordResetToken, PendingImprovement, Role,
                        ShareToken, Team, TeamMember, User)
from app.schemas import (AcceptImprovementRequest, DomainInviteCreate,
                         DiagramCreate, FolderCreate, FolderDeleteRequest,
                         FolderResponse, ImproveRequest, InvitationCreate,
                         InvitationResponse, LoginRequest, MoveToFolderRequest,
                         PasswordReset, PasswordResetRequest,
                         PasswordResetResponse, RegisterRequest,
                         RestoreDiagramRequest, RoleCreate, RoleResponse,
                         ShareRequest, ShareResponse, TeamCreate, TeamResponse,
                         Token, UserCreate, UserResponse)
from app.security import (create_access_token, get_password_hash, oauth2_scheme,
                          verify_password)
from core.bpmn_generator import GenerationError
from core.llm_improve import ImprovementError

logger = logging.getLogger(__name__)

router = APIRouter()

# Роуты для работы с диаграммами
@router.post("/api/diagrams")
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

@router.get("/api/diagrams")
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

@router.get("/api/diagrams/{diagram_id}")
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

@router.delete("/api/diagrams/{diagram_id}")
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

@router.post("/api/diagrams/move-to-folder")
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

@router.post("/api/import-bpmn")
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

@router.get("/api/deleted-diagrams")
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

@router.post("/api/deleted-diagrams/restore")
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


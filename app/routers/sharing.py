"""Доступ по ссылке и публикация диаграмм в команду."""
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

@router.post("/api/share", response_model=ShareResponse)
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

@router.get("/api/share/{share_token}")
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

@router.post("/api/diagrams/share-to-team")
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


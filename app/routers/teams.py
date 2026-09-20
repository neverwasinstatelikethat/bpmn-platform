"""Команды, приглашения и роли."""
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

@router.post("/api/teams", response_model=TeamResponse)
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

@router.get("/api/teams", response_model=List[TeamResponse])
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

@router.delete("/api/teams/{team_id}")
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

@router.post("/api/teams/{team_id}/invite", response_model=InvitationResponse)
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

@router.post("/api/teams/{team_id}/invite-domain")
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

@router.get("/api/invitations/accept/{token}", response_model=InvitationResponse)
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

@router.post("/api/roles", response_model=RoleResponse)
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

@router.get("/api/roles", response_model=List[RoleResponse])
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

@router.get("/api/teams/{team_id}/diagrams")
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
@router.get("/api/teams/{team_id}")
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

@router.put("/api/teams/{team_id}", response_model=TeamResponse)
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
@router.get("/api/teams/{team_id}/role")
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

# Эндпоинт для получения участников команды
@router.get("/api/teams/{team_id}/members")
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



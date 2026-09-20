"""ИИ-контур: генерация, скоринг, улучшение и принятие изменений."""
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

GENERATE_TEXT_LIMIT = 10_000

@router.post("/api/generate")
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
@router.post("/api/evaluate")
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

@router.post("/api/export")
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

@router.post("/api/ai/improve")
async def improve_diagram_endpoint(
    request: ImproveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if get_orchestrator() is None:
        raise HTTPException(503, detail="Сервис улучшения отключён (SKIP_LLM_INIT=1)")
    if not request.bpmn_xml or len(request.bpmn_xml) > IMPROVE_XML_LIMIT:
        raise HTTPException(
            400,
            detail="BPMN XML отсутствует или превышает 1 МБ"
        )
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(400, detail="Prompt is required")

    try:
        recommendations, improved_xml, report = await get_orchestrator().improve_diagram(
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

@router.post("/api/ai/accept-improvement")
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


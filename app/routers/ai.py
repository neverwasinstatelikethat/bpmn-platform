"""ИИ-контур: генерация, скоринг, улучшение и принятие изменений."""
import asyncio
import logging
import uuid
from datetime import datetime
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session
from app.ai import generator, get_orchestrator, scorer
from app.db import get_db
from app.deps import get_current_user
from app.models import Diagram, Improvement, User
from app.schemas import AcceptImprovementRequest, ImproveRequest
from app.services.access import load_diagram
from app.services.improvements import (APPROVED, PENDING, REJECTED,
                                     record_proposal)
from app.services.versions import record_version
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

    improvement = record_proposal(
        db,
        user_id=current_user.id,
        diagram_id=request.diagram_id,
        improved_xml=improved_xml,
        recommendations=recommendations,
    )
    logger.info("Диаграмма %s улучшена, улучшение %s", request.diagram_id, improvement.id)

    return {
        "status": "success",
        "improvement_id": improvement.id,
        "recommendations": recommendations,
        "report": report,
    }

@router.post("/api/ai/accept-improvement")
async def accept_improvement(
    request: AcceptImprovementRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    improvement = db.query(Improvement).filter(
        Improvement.id == request.improvement_id,
        Improvement.user_id == current_user.id,
        Improvement.status == PENDING,
    ).first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")
    
    if improvement.diagram_id:
        diagram = load_diagram(db, current_user, improvement.diagram_id, edit=True)
        if improvement.base_seq is not None and diagram.version_seq != improvement.base_seq:
            # Правки считались по другой версии: принятие молча затёрло бы то,
            # что сохранилось после расчёта улучшения.
            raise HTTPException(
                409,
                detail="Схема изменилась после расчёта улучшения — запросите его заново",
            )
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
    
    version = record_version(db, diagram, source="approved", author_id=current_user.id,
                             note=f"улучшение {improvement.id}")
    improvement.status = APPROVED
    improvement.decided_at = datetime.utcnow()
    db.commit()
    return {
        "status": "success",
        "diagram_id": diagram.id,
        "xml_content": diagram.xml_content,
        "version_seq": version.seq if version else diagram.version_seq,
    }

@router.post("/api/ai/reject-improvement")
async def reject_improvement(
    request: AcceptImprovementRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Отказ от предложения: решение остаётся в истории, строка не удаляется."""
    improvement = db.query(Improvement).filter(
        Improvement.id == request.improvement_id,
        Improvement.user_id == current_user.id,
        Improvement.status == PENDING,
    ).first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")

    improvement.status = REJECTED
    improvement.decided_at = datetime.utcnow()
    db.commit()
    return {"status": "success", "improvement_id": improvement.id}


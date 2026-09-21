"""ИИ-контур: генерация, скоринг, улучшение и принятие изменений."""
import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai import generator, get_orchestrator, scorer
from app.db import get_db
from app.deps import get_ai_user, get_current_user
from app.models import Diagram, Improvement, User
from app.schemas import (AcceptImprovementRequest, EvaluateRequest,
                         GenerateRequest, ImproveRequest)
from app.services.access import load_diagram
from app.services.improvements import (ANALYSIS_ONLY, APPROVED, PENDING, REJECTED,
                                        record_proposal)
from app.services.versions import record_version
from app.timeutils import utc_now
from core.bpmn_scoring import diff_scores
from core.llm_improve import ImprovementError, ImprovementUnavailable

logger = logging.getLogger(__name__)

router = APIRouter()

# Шаг, на котором сорвалась генерация, → HTTP-статус. Отказ провайдера — не
# ошибка запроса: на 400 клиент не ретраит, и пользователь начинает
# переформулировать описание процесса, с которым всё в порядке.
_GENERATE_HTTP = {
    "llm": status.HTTP_503_SERVICE_UNAVAILABLE,
    "llm_truncated": status.HTTP_400_BAD_REQUEST,
    "parse": status.HTTP_400_BAD_REQUEST,
    "generation": status.HTTP_400_BAD_REQUEST,
    "internal": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_RETRY_AFTER_UNAVAILABLE = {"Retry-After": "30"}


@router.post("/api/generate")
async def generate_bpmn(request: GenerateRequest,
                        current_user: User = Depends(get_ai_user)):
    result = await asyncio.to_thread(generator.generate, request.text)
    if result["status"] != "success":
        step = str(result.get("step") or "internal")
        code = _GENERATE_HTTP.get(step, status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            code,
            detail=result.get("error") or "Не удалось построить схему",
            headers=_RETRY_AFTER_UNAVAILABLE if code == 503 else None,
        )

    # notes — единственный сигнал о том, что структуру схемы починили за
    # пользователя (перенос в другой пул, удалённый поток, добавленное
    # событие). Без него эти правки остаются только в логе backend.
    return {
        "status": "success",
        "bpmn": result["bpmn"],
        "structure": result.get("structure") or {},
        "notes": result.get("notes") or [],
        # Что осталось нарушенным после починки и сколько вызовов модели
        # потратила генерация — метрики качества снимаются с этого ответа.
        "gaps": result.get("gaps") or [],
        "attempts": result.get("attempts") or 1,
    }


@router.post("/api/evaluate")
async def evaluate_bpmn(request: EvaluateRequest,
                        current_user: User = Depends(get_ai_user)):
    # Скоринг — разбор и обход дерева пользовательского XML: на событийном
    # цикле одна крупная схема задержала бы весь сервис.
    return await asyncio.to_thread(scorer.evaluate, request.bpmn_xml)


@router.post("/api/ai/improve")
async def improve_diagram_endpoint(
    request: ImproveRequest,
    current_user: User = Depends(get_ai_user),
    db: Session = Depends(get_db)
):
    # Проверка доступа — до всего остального: чужой id не должен отвечать ни
    # 503, ни 429 раньше, чем 404.
    # Предложение привязывается к хранённой схеме: оно снимает в статус
    # superseded чужие незакрытые предложения по ней и отсчитывается от её
    # версии. Значит здесь нужен доступ на правку, а не на просмотр: чистый
    # анализ схемы без привязки вызывается без diagram_id. Заодно вымышленный
    # id превращается из 500 на внешнем ключе в 404.
    if request.diagram_id:
        load_diagram(db, current_user, request.diagram_id, edit=True)

    orchestrator = get_orchestrator()
    if orchestrator is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Сервис улучшения отключён (SKIP_LLM_INIT=1)",
            headers=_RETRY_AFTER_UNAVAILABLE,
        )

    try:
        recommendations, improved_xml, report = await orchestrator.improve_diagram(
            xml_content=request.bpmn_xml,
            user_prompt=request.prompt
        )
    except ImprovementUnavailable as e:
        logger.warning("Улучшение недоступно: %s", e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
                            headers=_RETRY_AFTER_UNAVAILABLE)
    except ImprovementError as e:
        logger.warning("Улучшение диаграммы %s отклонено: %s", request.diagram_id, e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001 — наружу только 500
        logger.error("Критическая ошибка улучшения диаграммы %s: %s",
                     request.diagram_id, e, exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Внутренняя ошибка сервера при улучшении диаграммы")

    if improved_xml is None:
        # Только анализ: менять схему не нужно, но вывод модели обязан остаться
        # в истории — иначе сценарий «найди узкие места» нечем показать.
        improvement = record_proposal(
            db,
            user_id=current_user.id,
            diagram_id=request.diagram_id,
            improved_xml=None,
            recommendations=recommendations,
            status=ANALYSIS_ONLY,
        )
        logger.info("Диаграмма %s: только анализ без изменений (%s)",
                    request.diagram_id, improvement.id)
        return {
            "status": "analysis_only",
            "improvement_id": improvement.id,
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
    # Строка берётся под блокировкой: два параллельных принятия одного
    # предложения иначе создали бы две версии, и вторая затёрла бы первую.
    improvement = db.query(Improvement).filter(
        Improvement.id == request.improvement_id,
        Improvement.user_id == current_user.id,
        Improvement.status == PENDING,
    ).with_for_update().first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")

    previous_xml: Optional[str] = None
    if improvement.diagram_id:
        diagram = load_diagram(db, current_user, improvement.diagram_id, edit=True)
        if improvement.base_seq is not None and diagram.version_seq != improvement.base_seq:
            # Правки считались по другой версии: принятие молча затёрло бы то,
            # что сохранилось после расчёта улучшения.
            raise HTTPException(
                409,
                detail="Схема изменилась после расчёта улучшения — запросите его заново",
            )
        # Снимок «до» берётся до перезаписи: иначе дельта по правилам всегда
        # пустая, и пользователь не видит, что принятое изменение ухудшило
        # схему.
        previous_xml = diagram.xml_content
        diagram.xml_content = improvement.xml_content
        diagram.updated_at = utc_now()
    else:
        diagram = Diagram(
            id=str(uuid.uuid4()),
            name="Улучшенная диаграмма",
            xml_content=improvement.xml_content,
            user_id=current_user.id,
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(diagram)

    # Балл пересчитываем по новому XML. Предложение может и ухудшить схему
    # (шаг остался вне маршрута), а старый балл иначе остался бы и в реестре,
    # и в истории версий — то есть вся статистика качества врала бы после
    # каждого принятия.
    score_before = diagram.score
    after_report = await asyncio.to_thread(scorer.evaluate,
                                           improvement.xml_content)
    score_after = after_report["score"]
    diagram.score = score_after

    # Дельта по правилам, а не только итоговая цифра: «85 → 85» выглядит как
    # бесполезное улучшение, хотя три правила починены, а прирост съеден
    # новым требованием к документации.
    rules_delta = None
    if previous_xml:
        rules_delta = diff_scores(
            await asyncio.to_thread(scorer.evaluate, previous_xml), after_report)

    version = record_version(db, diagram, source="approved", author_id=current_user.id,
                             note=f"улучшение {improvement.id}")
    improvement.status = APPROVED
    improvement.decided_at = utc_now()
    db.commit()
    return {
        "status": "success",
        "diagram_id": diagram.id,
        "xml_content": diagram.xml_content,
        "version_seq": version.seq if version else diagram.version_seq,
        "score": score_after,
        "score_before": score_before,
        "rules_delta": rules_delta,
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
    ).with_for_update().first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")

    improvement.status = REJECTED
    improvement.decided_at = utc_now()
    db.commit()
    return {"status": "success", "improvement_id": improvement.id}

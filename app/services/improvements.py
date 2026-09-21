"""Жизненный цикл предложения об улучшении.

Статусы: pending → approved | rejected | superseded; `analysis_only` —
терминальный: изменений нет, но вывод модели хранится, чтобы история «найти
узкие места» не исчезала. Решение не удаляет строку — из истории видно, кто и
когда отверг предложение либо перестал его учитывать из-за новой правки схемы.
"""
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Diagram, Improvement
from app.timeutils import utc_now

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"
ANALYSIS_ONLY = "analysis_only"


def supersede_pending(db: Session, diagram_id: str) -> None:
    """Снимает незакрытые предложения диаграммы (без commit).

    Вызывается и при новом предложении, и при удалении схемы: предложение,
    повисшее после удаления, принять уже нельзя, а без этого оно уезжает в
    ветку «создать диаграмму с нуля» и возрождает удалённую схему.
    """
    db.query(Improvement).filter(
        Improvement.diagram_id == diagram_id,
        Improvement.status == PENDING,
    ).update({"status": SUPERSEDED, "decided_at": utc_now()},
             synchronize_session=False)


def record_proposal(db: Session, *, user_id: int, diagram_id: Optional[str],
                    improved_xml: Optional[str], recommendations: str,
                    status: str = PENDING) -> Improvement:
    """Кладёт новое предложение рядом с версией схемы, с которой его считали.

    `base_seq` — версия диаграммы на момент расчёта: по нему принятие
    отличает актуальное предложение от устаревшего. Прежние незакрытые
    предложения той же диаграммы уходят в superseded, чтобы их нельзя было
    принять поверх нового. Чистый анализ (`improved_xml=None`) схему не меняет,
    поэтому прежние предложения не снимает.
    """
    base_seq = None
    if diagram_id:
        if improved_xml is not None:
            supersede_pending(db, diagram_id)
        stored = db.query(Diagram.version_seq).filter(Diagram.id == diagram_id).scalar()
        # Диаграммы ещё нет в реестре (предложение «с нуля») — отсчитываем от нуля.
        base_seq = stored if stored is not None else 0

    improvement = Improvement(
        id=str(uuid.uuid4()),
        diagram_id=diagram_id,
        user_id=user_id,
        xml_content=improved_xml,
        recommendations=recommendations,
        created_at=utc_now(),
        status=status,
        base_seq=base_seq,
    )
    db.add(improvement)
    db.commit()
    return improvement

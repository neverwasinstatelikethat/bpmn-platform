"""Жизненный цикл предложения об улучшении.

Статусы: pending → approved | rejected | superseded. Решение не удаляет
строку — из истории видно, кто и когда отверг предложение либо перестал его
учитывать из-за новой правки схемы.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Diagram, Improvement

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"


def record_proposal(db: Session, *, user_id: int, diagram_id: Optional[str],
                    improved_xml: str, recommendations: str) -> Improvement:
    """Кладёт новое предложение рядом с версией схемы, с которой его считали.

    `base_seq` — версия диаграммы на момент расчёта: по нему принятие
    отличает актуальное предложение от устаревшего. Прежние незакрытые
    предложения той же диаграммы уходят в superseded, чтобы их нельзя было
    принять поверх нового.
    """
    base_seq = None
    if diagram_id:
        db.query(Improvement).filter(
            Improvement.diagram_id == diagram_id,
            Improvement.status == PENDING,
        ).update({"status": SUPERSEDED, "decided_at": datetime.utcnow()},
                 synchronize_session=False)
        stored = db.query(Diagram.version_seq).filter(Diagram.id == diagram_id).scalar()
        # Диаграммы ещё нет в реестре (предложение «с нуля») — отсчитываем от нуля.
        base_seq = stored if stored is not None else 0

    improvement = Improvement(
        id=str(uuid.uuid4()),
        diagram_id=diagram_id,
        user_id=user_id,
        xml_content=improved_xml,
        recommendations=recommendations,
        created_at=datetime.utcnow(),
        status=PENDING,
        base_seq=base_seq,
    )
    db.add(improvement)
    db.commit()
    return improvement

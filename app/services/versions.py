"""История версий диаграммы.

Диаграмма больше не перезаписывается молча: каждая содержательная правка
(сохранение, принятие улучшения, откат) оставляет снимок, из которого можно
вернуться. Откат — это новая версия, а не стирание последующих.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Diagram, DiagramVersion


def record_version(db: Session, diagram: Diagram, *, source: str,
                   author_id: int, note: Optional[str] = None) -> Optional[DiagramVersion]:
    """Записывает текущее состояние схемы, если оно изменилось.

    Повтор с тем же XML (автосохранение фронтенда, пересохранение без правок)
    историю не плодит. Коммит остаётся вызывающему: версия пишется в той же
    транзакции, что и диаграмма.
    """
    latest = db.query(DiagramVersion).filter(
        DiagramVersion.diagram_id == diagram.id
    ).order_by(DiagramVersion.seq.desc()).first()

    if latest is not None and latest.xml_content == diagram.xml_content:
        return None

    seq = (latest.seq if latest else 0) + 1
    version = DiagramVersion(
        id=str(uuid.uuid4()),
        diagram_id=diagram.id,
        seq=seq,
        xml_content=diagram.xml_content,
        score=diagram.score or 0,
        source=source,
        author_id=author_id,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(version)
    diagram.version_seq = seq
    return version

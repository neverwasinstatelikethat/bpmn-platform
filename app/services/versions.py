"""История версий диаграммы.

Диаграмма больше не перезаписывается молча: каждая содержательная правка
(сохранение, принятие улучшения, откат) оставляет снимок, из которого можно
вернуться. Откат — это новая версия, а не стирание последующих.
"""
import uuid
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Diagram, DiagramVersion
from app.timeutils import utc_now

# Попыток записать снимок: вторая — только на случай коллизии seq.
_ATTEMPTS = 2


def record_version(db: Session, diagram: Diagram, *, source: str,
                   author_id: int, note: Optional[str] = None) -> Optional[DiagramVersion]:
    """Записывает текущее состояние схемы, если оно изменилось.

    Повтор с тем же XML (автосохранение фронтенда, пересохранение без правок)
    историю не плодит. Коммит остаётся вызывающему: версия пишется в той же
    транзакции, что и диаграмма.
    """
    # seq = max(seq)+1, и два параллельных сохранения одной схемы считают
    # один seq: проигравший спотыкается об UNIQUE(diagram_id, seq) уже в
    # коммите маршрута, а вместе с транзакцией теряет правку. Блокировка
    # строки диаграммы сериализует расчёт на PostgreSQL; SQLite блокировок
    # FOR UPDATE не знает, там ниже работает ретрай.
    db.query(Diagram.id).filter(Diagram.id == diagram.id).with_for_update().first()

    attempts = _ATTEMPTS
    while True:
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
            created_at=utc_now(),
        )
        try:
            # Savepoint обязателен: на PostgreSQL любая ошибка выводит из
            # транзакции целиком, и после конфликта маршруту оставался бы
            # только 500. Откат к savepoint гасит неудачную вставку, не
            # трогая правку диаграммы — она уйдёт в этот же коммит.
            with db.begin_nested():
                db.add(version)
            break
        except IntegrityError:
            attempts -= 1
            if attempts <= 0:
                raise

    diagram.version_seq = seq
    return version

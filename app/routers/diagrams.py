"""Реестр диаграмм: сохранение, просмотр, удаление, импорт."""
import logging
import uuid
import xml.etree.ElementTree as ET
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from app.config import MAX_UPLOAD_BYTES
from app.db import get_db
from app.deps import get_current_user
from app.models import DeletedDiagram, Diagram, DiagramVersion, Folder, User
from app.schemas import DiagramCreate, MoveToFolderRequest, RestoreDiagramRequest
from app.services.access import load_diagram
from app.services.improvements import supersede_pending
from app.services.versions import record_version
from app.timeutils import utc_now
from core.bpmn_edits import parse_xml

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
    exists = db.query(Diagram.id).filter(Diagram.id == diagram_id).first() is not None

    if exists:
        # Правка существующей диаграммы раньше не проверяла доступ вовсе:
        # чужую схему можно было перезаписать, подставив её id.
        target = load_diagram(db, current_user, diagram_id, edit=True)
        target.name = diagram.name
        target.xml_content = diagram.xml
        target.score = diagram.score
        target.updated_at = utc_now()
        source = "saved"
    else:
        target = Diagram(
            id=diagram_id,
            name=diagram.name,
            xml_content=diagram.xml,
            score=diagram.score,
            user_id=current_user.id
        )
        db.add(target)
        source = "created"

    # Версия пишется в той же транзакции: без неё commit диаграммы остался бы
    # без истории, а следующий шаг улучшения не от чего было бы отсчитать.
    version = record_version(db, target, source=source, author_id=current_user.id)
    db.commit()
    return {
        "status": "success",
        "diagram_id": target.id,
        "version_seq": version.seq if version else target.version_seq,
    }

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
    diagram = load_diagram(db, current_user, diagram_id)
    return {
        "id": diagram.id,
        "name": diagram.name,
        "xml_content": diagram.xml_content
    }

@router.get("/api/diagrams/{diagram_id}/versions")
def list_diagram_versions(
    diagram_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """История без тел схем — список нужен для выбора отката, а не для просмотра."""
    diagram = load_diagram(db, current_user, diagram_id)
    versions = db.query(DiagramVersion).filter(
        DiagramVersion.diagram_id == diagram.id
    ).order_by(DiagramVersion.seq.desc()).all()
    return [{
        "seq": v.seq,
        "source": v.source,
        "score": v.score,
        "author_id": v.author_id,
        "note": v.note,
        "created_at": v.created_at,
    } for v in versions]

@router.get("/api/diagrams/{diagram_id}/versions/{seq}")
def get_diagram_version(
    diagram_id: str,
    seq: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = load_diagram(db, current_user, diagram_id)
    version = db.query(DiagramVersion).filter(
        DiagramVersion.diagram_id == diagram.id,
        DiagramVersion.seq == seq
    ).first()
    if not version:
        raise HTTPException(404, detail="Version not found")
    return {
        "seq": version.seq,
        "source": version.source,
        "note": version.note,
        "created_at": version.created_at,
        "xml_content": version.xml_content,
    }

@router.post("/api/diagrams/{diagram_id}/restore/{seq}")
def restore_diagram_version(
    diagram_id: str,
    seq: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Откат: содержимое выбранной версии становится текущим и записывается новым снимком.

    История не стирается — «вернуть v3» выглядит в ней как v5, и из v5 можно
    уйти обратно в v4.
    """
    diagram = load_diagram(db, current_user, diagram_id, edit=True)
    version = db.query(DiagramVersion).filter(
        DiagramVersion.diagram_id == diagram.id,
        DiagramVersion.seq == seq
    ).first()
    if not version:
        raise HTTPException(404, detail="Version not found")

    diagram.xml_content = version.xml_content
    diagram.score = version.score
    diagram.updated_at = utc_now()
    restored = record_version(db, diagram, source="restored",
                              author_id=current_user.id, note=f"откат к v{seq}")
    db.commit()
    return {
        "status": "success",
        "diagram_id": diagram.id,
        "xml_content": diagram.xml_content,
        "version_seq": restored.seq if restored else diagram.version_seq,
    }

@router.delete("/api/diagrams/{diagram_id}")
def delete_diagram(
    diagram_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagram = load_diagram(db, current_user, diagram_id, edit=True)
    deleted_diagram = DeletedDiagram(
        id=str(uuid.uuid4()),
        diagram_id=diagram.id,
        name=diagram.name,
        xml_content=diagram.xml_content,
        user_id=current_user.id
    )
    db.add(deleted_diagram)
    # Пока диаграмма не исчезла из FK: висящее pending-предложение после
    # удаления принимается в ветке «создать с нуля» и возрождает схему.
    supersede_pending(db, diagram.id)
    # Ссылки совместного использования переживают схему как мёртвые строки:
    # у отношения нет cascade, и ORM лишь обнуляет FK. Удаляем объекты через
    # связь — bulk-delete оставил бы сессию рассогласованной с каскадом.
    for token in diagram.share_tokens:
        db.delete(token)
    db.delete(diagram)
    db.commit()
    return {"status": "success"}

@router.post("/api/diagrams/move-to-folder")
def move_to_folder(
    request: MoveToFolderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Перемещение — запись, поэтому право на правку, а не на чтение.
    diagram = load_diagram(db, current_user, request.diagram_id, edit=True)
    
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

        # Читаем не более чем лимит+1 байт, а не весь файл: иначе один запрос
        # материализует в памяти столько, сколько в него загрузили.
        contents = await file.read(MAX_UPLOAD_BYTES + 1)
        logger.info(f"File size: {len(contents)} bytes")
        if len(contents) > MAX_UPLOAD_BYTES:
            # Файл идёт в БД и потом в парсер — без размера на входе один
            # запрос съедает память процесса.
            raise HTTPException(
                status_code=413,
                detail=f"Файл больше {MAX_UPLOAD_BYTES / 1_000_000:g} МБ",
            )

        try:
            xml_content = contents.decode('utf-8')
        except UnicodeDecodeError:
            error_msg = "Файл должен быть в UTF-8 кодировке"
            logger.warning(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)
        
        try:
            # Загруженный файл — недоверенный XML: разбираем так же, как весь
            # пользовательский XML (defusedxml), иначе внешние сущности
            # раскрутят файл в память.
            root = parse_xml(xml_content)
            if not root.tag.endswith('}definitions'):
                error_msg = "Файл не содержит BPMN definitions"
                logger.warning(error_msg)
                raise HTTPException(status_code=400, detail=error_msg)
        except HTTPException:
            raise
        except (ET.ParseError, ValueError) as e:
            error_msg = f"Ошибка парсинга XML: {str(e)}"
            logger.warning(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)

        diagram_id = str(uuid.uuid4())
        # Имя файла приходит от клиента, а колонки отмеряны VARCHAR: длинное
        # имя на PostgreSQL дало бы 500 вместо загруженной схемы.
        name = file.filename.replace('.bpmn', '').replace('.xml', '')[:100]
        db_diagram = Diagram(
            id=diagram_id,
            name=name,
            xml_content=xml_content,
            user_id=current_user.id,
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(db_diagram)
        record_version(db, db_diagram, source="created", author_id=current_user.id,
                       note=f"импорт {name}"[:200])
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
        created_at=utc_now(),
        updated_at=utc_now()
    )
    db.add(diagram)
    # Восстановление из корзины — это рождение схемы заново: прежнее id
    # свободно, история у него пустая.
    record_version(db, diagram, source="created", author_id=current_user.id,
                   note="восстановлено из корзины")
    db.delete(deleted_diagram)
    db.commit()
    return {"status": "success"}


"""Папки реестра: дерево, перемещение, удаление."""
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.db import get_db
from app.deps import get_current_user
from app.models import Diagram, Folder, Improvement, ShareToken, User
from app.schemas import FolderCreate
from app.services.access import EDIT_PERMISSION, can_in_team, team_ids_of
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/folders")
def create_folder(
    folder: FolderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if folder.team_id:
        # Тот же предикат, что у удаления папки: владелец команды или участник с
        # editRegistry. Право читается из разобранного JSON роли, иначе его не
        # даёт ни одна сид-роль (см. app/services/access.py).
        if not can_in_team(db, current_user, folder.team_id, EDIT_PERMISSION):
            raise HTTPException(status_code=403, detail="Access denied to create team folder")
        if folder.parent_id:
            parent_folder = db.query(Folder).filter(
                Folder.id == folder.parent_id,
                Folder.team_id == folder.team_id
            ).first()
            if not parent_folder:
                raise HTTPException(status_code=400, detail="Parent folder not found in team")
    else:
        if folder.parent_id:
            parent_folder = db.query(Folder).filter(
                Folder.id == folder.parent_id,
                Folder.user_id == current_user.id
            ).first()
            if not parent_folder:
                raise HTTPException(status_code=400, detail="Parent folder not found")

    folder_id = str(uuid.uuid4())
    db_folder = Folder(
        id=folder_id,
        name=folder.name,
        user_id=current_user.id if not folder.team_id else None,
        team_id=folder.team_id,
        parent_id=folder.parent_id,
        created_at=utc_now()
    )
    db.add(db_folder)
    db.commit()
    return {"status": "success", "folder_id": folder_id}

@router.get("/api/folders")
def get_user_folders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    folders = db.query(Folder).filter(
        or_(
            Folder.user_id == current_user.id,
            Folder.team_id.in_(team_ids_of(db, current_user))
        )
    ).all()
    return [{
        "id": f.id,
        "name": f.name,
        "created_at": f.created_at,
        "parent_id": f.parent_id,
        "team_id": f.team_id
    } for f in folders]

@router.delete("/api/folders/{folder_id}")
def delete_folder(
    folder_id: str,
    delete_contents: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()

    if folder is None:
        raise HTTPException(404, detail="Folder not found")

    # Личная папка — только её владельцу; командная — тому, кто вправе работать в
    # команде (владелец команды или участник с editRegistry). Прежний запрос
    # смотрел только на участников с правом и давал 404 владельцу команды на свою
    # папку, хотя создание той же папки этому же владельцу разрешал.
    # Чужой папке предикат не помогает: у личной чужой не совпадёт user_id, а
    # командная принадлежит команде, в которой права нет, — ответ тот же 404,
    # чтобы не выдавать существование чужой папки.
    if folder.team_id is None:
        allowed = folder.user_id == current_user.id
    else:
        allowed = can_in_team(db, current_user, folder.team_id, EDIT_PERMISSION)
    if not allowed:
        raise HTTPException(404, detail="Folder not found")

    # Поддерево собирается итеративно, а не рекурсией по children: parent_id —
    # самоссылка, и цикл в нём (ручная правка в базе) иначе уводит обход в
    # бесконечность. Уровень обходится одним запросом: прежний вариант ходил в
    # базу на каждый узел и на дереве из N узлов тратил N запросов.
    subtree = [folder.id]
    seen = {folder.id}
    frontier = [folder.id]
    while frontier:
        nxt = []
        for (child_id,) in db.query(Folder.id).filter(Folder.parent_id.in_(frontier)).all():
            if child_id not in seen:
                seen.add(child_id)
                nxt.append(child_id)
        subtree.extend(nxt)
        frontier = nxt

    # Ссылки внутри поддерева обрываем заранее: сортировка удалений SQLAlchemy
    # на цикле в parent_id падает с CircularDependencyError, а на PostgreSQL
    # DELETE родителя с ещё живым ребёнком в той же таблице отклоняется по FK.
    db.query(Folder).filter(Folder.id.in_(subtree)).update(
        {Folder.parent_id: None}, synchronize_session=False)

    # Диаграмму нельзя снять bulk-delete'ом: FK у diagram_versions, share_tokens
    # и pending_improvements объявлены без ondelete, и на них PostgreSQL
    # отклоняет весь запрос. Через ORM снимки истории убирает каскад связи
    # Diagram.versions, а токены и предложения удаляем явно — иначе ORM лишь
    # обнулит diagram_id и оставит строки-призраки. Токены и предложения
    # снимаются по одному пакетному запросу на всё поддерево, а не по два на
    # диаграмму.
    diagrams = []
    if delete_contents:
        diagrams = db.query(Diagram).filter(Diagram.folder_id.in_(subtree)).all()
        if diagrams:
            doomed = [d.id for d in diagrams]
            db.query(ShareToken).filter(ShareToken.diagram_id.in_(doomed)).delete(
                synchronize_session=False)
            db.query(Improvement).filter(Improvement.diagram_id.in_(doomed)).delete(
                synchronize_session=False)
            for diagram in diagrams:
                db.delete(diagram)
            # Снятие диаграмм через ORM улетит во flush на commit, а bulk-DELETE
            # папок выполняется сразу: без flush ссылки `diagrams.folder_id` ещё
            # живы, и база с работающими FK (PostgreSQL, SQLite с прагмой)
            # отклоняет удаление папок.
            db.flush()
    else:
        db.query(Diagram).filter(Diagram.folder_id.in_(subtree)).update(
            {Diagram.folder_id: None}, synchronize_session=False)

    db.query(Folder).filter(Folder.id.in_(subtree)).delete(synchronize_session=False)
    db.commit()
    return {"status": "success"}

@router.get("/api/folders/{folder_id}/diagrams")
def get_folder_diagrams(
    folder_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    diagrams = db.query(Diagram).filter(
        Diagram.folder_id == folder_id,
        or_(
            Diagram.user_id == current_user.id,
            Diagram.team_id.in_(team_ids_of(db, current_user))
        )
    ).all()
    return [{
        "id": d.id,
        "name": d.name,
        "created_at": d.created_at
    } for d in diagrams]

@router.get("/api/folders/tree")
def get_folder_tree(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user_team_ids = set(team_ids_of(db, current_user))
    folders = db.query(Folder).filter(
        or_(
            Folder.user_id == current_user.id,
            Folder.team_id.in_(user_team_ids)
        )
    ).all()

    # id команд снимаются один раз: прежняя проверка внутри рекурсии обращалась к
    # current_user.team_memberships на каждую папку (ленивая загрузка = запрос на
    # узел), хотя состав команд у пользователя внутри запроса не меняется.
    def build_tree(parent_id=None):
        tree = []
        for folder in folders:
            if folder.parent_id == parent_id and (
                folder.user_id == current_user.id or folder.team_id in user_team_ids
            ):
                folder_data = {
                    "id": folder.id,
                    "name": folder.name,
                    "parent_id": folder.parent_id,
                    "created_at": folder.created_at,
                    "team_id": folder.team_id,
                    "children": build_tree(folder.id)
                }
                tree.append(folder_data)
        return tree

    return build_tree()

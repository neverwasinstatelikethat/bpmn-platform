"""Папки реестра: дерево, перемещение, удаление."""
import json
import logging
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from app.db import get_db
from app.deps import get_current_user
from app.models import Diagram, Folder, Role, Team, TeamMember, User
from app.schemas import FolderCreate

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/folders")
def create_folder(
    folder: FolderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if folder.team_id:
        team = db.query(Team).filter(
            Team.id == folder.team_id,
            or_(
                Team.owner_id == current_user.id,
                Team.members.any(and_(
                    TeamMember.user_id == current_user.id,
                    TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                ))
            )
        ).first()
        if not team:
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
        created_at=datetime.utcnow()
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
            Folder.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
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
    folder = db.query(Folder).filter(
        Folder.id == folder_id,
        or_(
            and_(Folder.user_id == current_user.id, Folder.team_id.is_(None)),
            and_(
                Folder.team_id.in_(
                    db.query(TeamMember.team_id).filter(
                        TeamMember.user_id == current_user.id,
                        TeamMember.role.has(Role.permissions.contains(json.dumps({"editRegistry": True})))
                    )
                ),
                Folder.team_id.isnot(None)
            )
        )
    ).first()
    
    if not folder:
        raise HTTPException(404, detail="Folder not found")
    
    if delete_contents:
        db.query(Diagram).filter(
            Diagram.folder_id == folder_id
        ).delete()
    
    db.query(Folder).filter(Folder.parent_id == folder_id).delete()
    db.delete(folder)
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
            Diagram.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
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
    folders = db.query(Folder).filter(
        or_(
            Folder.user_id == current_user.id,
            Folder.team_id.in_(
                db.query(TeamMember.team_id).filter(TeamMember.user_id == current_user.id)
            )
        )
    ).all()
    
    def build_tree(parent_id=None):
        tree = []
        for folder in folders:
            if folder.parent_id == parent_id and (folder.user_id == current_user.id or folder.team_id in [tm.team_id for tm in current_user.team_memberships]):
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


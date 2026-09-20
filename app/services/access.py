"""Объектный доступ к диаграммам: единое правило для всех роутеров.

Чтение — владелец либо участник команды-владельца. Изменение — владелец либо
участник с ролью, где `editRegistry: true`. До выделения этого модуля условия
были скопированы в шесть endpoint-ов и местами расходились: например,
`POST /api/diagrams` не проверял доступ вовсе.
"""
import json

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Diagram, Role, TeamMember, User

EDIT_PERMISSION = json.dumps({"editRegistry": True})


def _member_teams(db: Session, user: User, *, edit: bool):
    """Подзапрос id команд пользователя; с фильтром по editRegistry — для правки."""
    query = db.query(TeamMember.team_id).filter(TeamMember.user_id == user.id)
    if edit:
        query = query.filter(
            TeamMember.role.has(Role.permissions.contains(EDIT_PERMISSION))
        )
    return query


def load_diagram(db: Session, user: User, diagram_id: str, *, edit: bool = False) -> Diagram:
    """Диаграмма либо 404.

    Именно 404, а не 403: код ответа не должен выдавать существование чужой
    диаграммы.
    """
    diagram = db.query(Diagram).filter(
        Diagram.id == diagram_id,
        or_(
            Diagram.user_id == user.id,
            Diagram.team_id.in_(_member_teams(db, user, edit=edit)),
        ),
    ).first()
    if diagram is None:
        raise HTTPException(404, detail="Diagram not found or access denied")
    return diagram

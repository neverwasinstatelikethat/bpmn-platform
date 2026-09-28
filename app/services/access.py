"""Объектный доступ к диаграммам: единое правило для всех роутеров.

Чтение — владелец либо участник команды-владельца. Изменение — владелец либо
участник с ролью, где `editRegistry: true`. До выделения этого модуля условия
были скопированы в шесть endpoint-ов и местами расходились: например,
`POST /api/diagrams` не проверял доступ вовсе.

Права читаются из разобранного JSON, а не подстрокой в сериализованном тексте.
Прежний `Role.permissions.contains(json.dumps({"editRegistry": True}))` не
совпадал ни с одной сид-ролью из `app/startup.py:PREDEFINED_ROLES`: та хранит
четыре ключа в своём порядке и со своими пробелами, поэтому подстрока
`{"editRegistry": true}` в её тексте отсутствует и правка через роль команды
была физически невозможна — все write-маршруты схлопывались в «только владелец».
Строковый предикат ломался бы и от порядка ключей, и от пробелов, а JSON-оператор
СУБД непереносим: колонка `permissions` — `Text`, и база может быть SQLite.
"""
import json

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Diagram, Role, Team, TeamMember, User

# Ключ права, а не его сериализованный вид: сравнение идёт по разобранному
# словарю, поэтому порядок ключей и пробелы в тексте роли ни на что не влияют.
EDIT_PERMISSION = "editRegistry"
MANAGE_ROLES_PERMISSION = "manageRoles"


def role_grants(permissions, permission: str) -> bool:
    """Есть ли у роли право `permission`. Принимает и текст колонки, и словарь."""
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except ValueError:
            # Битый JSON — отсутствие права, а не исключение: право не угадываем.
            return False
    if not isinstance(permissions, dict):
        return False
    return bool(permissions.get(permission))


def _membership_rows(db: Session, user: User):
    """Пары (id команды, текст прав роли) для всех участностей пользователя.

    Роль снимается OUTER JOIN-ом: участность без роли (или с удалённой ролью)
    обязана остаться в списке команд — право чтения у её владельца есть, а
    прав на запись просто нет.
    """
    return (
        db.query(TeamMember.team_id, Role.permissions)
        .outerjoin(Role, Role.id == TeamMember.role_id)
        .filter(TeamMember.user_id == user.id)
        .all()
    )


def team_ids_of(db: Session, user: User, *, permission: str = None) -> list:
    """Команды пользователя; если задано `permission` — только те, где он его даёт."""
    return [
        team_id
        for team_id, permissions in _membership_rows(db, user)
        if permission is None or role_grants(permissions, permission)
    ]


def has_permission_in_team(db: Session, user: User, team_id: str, permission: str) -> bool:
    return team_id in team_ids_of(db, user, permission=permission)


def owns_team(db: Session, user: User, team_id: str) -> bool:
    return db.query(Team.id).filter(
        Team.id == team_id, Team.owner_id == user.id
    ).first() is not None


def can_in_team(db: Session, user: User, team_id: str, permission: str) -> bool:
    """Действие в команде: её владелец либо участник с правом.

    Владелец проверяется явно, а не «через свою admin-участность»: папки и
    схемы команды он вправе трогать даже если его запись в `team_members`
    потеряна (создание команды добавляет её, но удаление участника из
    собственного списка это право не отзывает).
    """
    return owns_team(db, user, team_id) or has_permission_in_team(
        db, user, team_id, permission
    )


def load_diagram(db: Session, user: User, diagram_id: str, *, edit: bool = False) -> Diagram:
    """Диаграмма либо 404.

    Именно 404, а не 403: код ответа не должен выдавать существование чужой
    диаграммы.
    """
    diagram = db.query(Diagram).filter(
        Diagram.id == diagram_id,
        or_(
            Diagram.user_id == user.id,
            Diagram.team_id.in_(team_ids_of(
                db, user, permission=EDIT_PERMISSION if edit else None
            )),
        ),
    ).first()
    if diagram is None:
        raise HTTPException(404, detail="Diagram not found or access denied")
    return diagram

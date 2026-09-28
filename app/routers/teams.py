"""Команды, приглашения и роли."""
import json
import logging
import re
import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from app.config import FRONTEND_URL
from app.db import get_db
from app.deps import get_current_user
from app.models import (Diagram, Folder, Invitation, Role, Team, TeamMember,
                        User)
from app.schemas import (
                        DomainInviteCreate, InvitationCreate, InvitationResponse,
                        RoleCreate, RoleResponse, TeamCreate, TeamResponse
)
from app.services.access import MANAGE_ROLES_PERMISSION, team_ids_of
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

router = APIRouter()

# Домен для приглашения по адресу: только буквы, цифры, точки и дефис. `re.escape`
# из прежней сборки не экранировал LIKE-джокеры `%` и `_`, зато добавлял `\.` —
# обратный слэш в LIKE без ESCAPE литерален, поэтому домен `corp.example.com` не
# находил никого, а домен `%` зачислял в команду всю таблицу users.
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def _valid_domain(domain: str) -> str:
    domain = (domain or "").strip()
    if not domain or not DOMAIN_RE.fullmatch(domain) or ".." in domain:
        raise HTTPException(400, "Некорректный домен")
    return domain

@router.post("/api/teams", response_model=TeamResponse)
def create_team(
    team_data: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db_team = Team(
        id=str(uuid.uuid4()),
        name=team_data.name,
        color=team_data.color,
        owner_id=current_user.id
    )
    db.add(db_team)
    
    admin_role = db.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        raise HTTPException(500, "Admin role not configured")
    
    member = TeamMember(
        team_id=db_team.id,
        user_id=current_user.id,
        role_id=admin_role.id
    )
    db.add(member)
    db.commit()
    return db_team

@router.get("/api/teams", response_model=List[TeamResponse])
def get_user_teams(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    teams = db.query(Team).filter(
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).all()
    return teams

@router.delete("/api/teams/{team_id}")
def delete_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(404, "Team not found or access denied")

    # Папки команды живут и умирают вместе с ней: `user_id` у них пустой
    # (создание папки пишет владельца только личным папкам), поэтому передать
    # их никому нельзя, а оставив — на PostgreSQL получили бы падение по FK
    # `folders.team_id`, а на SQLite — сирот, которые ни в чьём дереве не
    # видны, но держат ссылки на схемы. Диаграммы при этом остаются личными:
    # у них свой `user_id`, и снятие `folder_id`/`team_id` их не удаляет.
    folder_ids = [row[0] for row in
                  db.query(Folder.id).filter(Folder.team_id == team_id).all()]
    if folder_ids:
        # Ссылки внутри снимаются до удаления: PostgreSQL проверяет FK построчно
        # и в одном DELETE родителя с ещё живым ребёнком упадёт, а висячий
        # `folders.parent_id` остаётся сиротой и на SQLite.
        db.query(Folder).filter(Folder.id.in_(folder_ids)).update(
            {Folder.parent_id: None}, synchronize_session=False)
        db.query(Diagram).filter(Diagram.folder_id.in_(folder_ids)).update(
            {Diagram.folder_id: None}, synchronize_session=False)
        db.query(Folder).filter(Folder.id.in_(folder_ids)).delete(
            synchronize_session=False)

    db.query(TeamMember).filter(TeamMember.team_id == team_id).delete()
    db.query(Invitation).filter(Invitation.team_id == team_id).delete()
    db.query(Diagram).filter(Diagram.team_id == team_id).update({"team_id": None})
    db.delete(team)
    db.commit()
    return {"status": "success"}

@router.post("/api/teams/{team_id}/invite", response_model=InvitationResponse)
def create_invitation(
    team_id: str,
    invite_data: InvitationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).options(joinedload(Team.members)).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(and_(
                TeamMember.user_id == current_user.id,
                TeamMember.role.has(Role.name == "admin")
            ))
        )
    ).first()
    
    if not team:
        raise HTTPException(403, "Access denied")
    
    role = db.query(Role).filter(Role.name == invite_data.role).first()
    if not role:
        raise HTTPException(400, "Invalid role specified")
    
    invitation = Invitation(
        team_id=team_id,
        email=invite_data.email,
        role_id=role.id
    )
    db.add(invitation)
    db.commit()
    
    accept_link = f"{FRONTEND_URL}/invite/{invitation.token}"
    
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": invite_data.role,
        "status": invitation.status,
        "created_at": invitation.created_at,
        "accept_link": accept_link
    }

@router.post("/api/teams/{team_id}/invite-domain")
def domain_invite(
    team_id: str,
    domain_data: DomainInviteCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(403, "Access denied")

    role = db.query(Role).filter(Role.name == domain_data.role).first()
    if not role:
        raise HTTPException(400, "Invalid role specified")

    domain = _valid_domain(domain_data.domain)
    # После валидации в домене гарантированно нет `%` и `_`, то есть единственный
    # джокер в шаблоне — наш ведущий `%`. Сравнение ещё и перепроверяется в
    # Python: запрос обязан отбирать ровно тех, чей адрес заканчивается на
    # `@домен`, а не на то, во что шаблон развернёт диалект-специфичный LIKE.
    pattern = f"%@{domain.lower()}"
    users = db.query(User).filter(
        func.lower(User.email).like(pattern)
    ).all()
    matched = [
        user for user in users
        if (user.email or "").strip().lower().endswith(f"@{domain.lower()}")
    ]

    # Один запрос на уже участников вместо запроса на каждого найденного.
    already = {row[0] for row in
               db.query(TeamMember.user_id).filter(TeamMember.team_id == team_id).all()}
    added = 0
    for user in matched:
        if user.id in already:
            continue
        db.add(TeamMember(team_id=team_id, user_id=user.id, role_id=role.id))
        already.add(user.id)
        added += 1

    db.commit()
    return {"status": "success", "users_added": added}

def _invitation_payload(invitation: Invitation, role_name: str) -> dict:
    """Ответ по приглашению.

    Возвращать ORM-объект под `InvitationResponse` нельзя: поле `role` в схеме
    — строка, а у модели это связывание с Role, и валидация ответа падала уже
    после commit (участность начислена, а клиент видит 500).
    """
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": role_name,
        "status": invitation.status,
        "created_at": invitation.created_at,
        "accept_link": f"{FRONTEND_URL}/invite/{invitation.token}",
    }


def _invitation_for_addressee(db: Session, token: str, user: User) -> Invitation:
    """Приглашение по токену, если его адресат — этот пользователь.

    GET читается, POST изменяет, поэтому проверки общие: чужая ссылка не должна
    ни выдавать доступ (POST), ни рассказывать про команду (GET).
    """
    invitation = db.query(Invitation).filter(Invitation.token == token).first()
    if invitation is None:
        raise HTTPException(404, "Invitation not found or expired")
    # Ссылка приглашения не передаётся третьим: роль в команде (нередко admin)
    # назначается конкретному адресату. Без сверки адреса любая пересланная или
    # утекаящая ссылка выдаёт доступ чужому аккаунту.
    if (invitation.email or "").strip().lower() != (user.email or "").strip().lower():
        raise HTTPException(403, "Приглашение адресовано другому аккаунту")
    return invitation


@router.get("/api/invitations/accept/{token}", response_model=InvitationResponse)
def view_invitation(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Статус приглашения без записи: GET обязан быть безопасным методом.

    Прежний GET сам зачислял участника, поэтому повторный заход, предвыборка
    ссылки сканером или prefetch браузера меняли состояние. Принятие — только
    `POST /api/invitations/accept/{token}`.
    """
    invitation = _invitation_for_addressee(db, token, current_user)
    if invitation.status == "pending" and invitation.expires_at <= utc_now():
        raise HTTPException(404, "Invitation not found or expired")
    role_name = invitation.role.name if invitation.role is not None else ""
    return _invitation_payload(invitation, role_name)


@router.post("/api/invitations/accept/{token}", response_model=InvitationResponse)
def accept_invitation(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Зачисление адресата в команду. Идемпотентно: повтор того же токена
    отвечает тем же самым принятым состоянием, а не 404 и не вторым членством."""
    invitation = _invitation_for_addressee(db, token, current_user)

    if invitation.status == "accepted":
        role_name = invitation.role.name if invitation.role is not None else ""
        return _invitation_payload(invitation, role_name)

    if invitation.expires_at <= utc_now():
        raise HTTPException(404, "Invitation not found or expired")

    # Единственность участности держит база (`uq_team_members_team_user` из
    # ревизии 0003), но проверку делаем и здесь: на PostgreSQL строку команды
    # замкает `FOR UPDATE`, и параллельные принятие и доменный набор не успевают
    # разойтись между «проверили» и «вставили».
    db.query(Team).filter(Team.id == invitation.team_id).with_for_update().first()
    existing = db.query(TeamMember).filter(
        TeamMember.team_id == invitation.team_id,
        TeamMember.user_id == current_user.id
    ).first()

    if existing is None:
        try:
            # savepoint: при IntegrityError (когда уникальность всё-таки
            # появится в схеме) откатывается только вставка, а принятие
            # приглашения дописывает статус.
            with db.begin_nested():
                db.add(TeamMember(
                    team_id=invitation.team_id,
                    user_id=current_user.id,
                    role_id=invitation.role_id,
                ))
        except IntegrityError:
            pass

    invitation.status = "accepted"
    db.commit()

    role_name = invitation.role.name if invitation.role is not None else ""
    return _invitation_payload(invitation, role_name)

@router.post("/api/roles", response_model=RoleResponse)
def create_role(
    role_data: RoleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Роль — это набор прав, а выдаётся она только через приглашение, поэтому
    # её создание должно быть правом `manageRoles`, а не действием вошедшего.
    # Глобального флага админа в модели `User` нет, поэтому мерка — «есть хотя
    # бы одна команда, где у пользователя роль с manageRoles: true» (владелец
    # попадает туда автоматически: создание команды выдаёт ему admin-участность).
    if not team_ids_of(db, current_user, permission=MANAGE_ROLES_PERMISSION):
        raise HTTPException(403, "Недостаточно прав для создания роли")

    existing = db.query(Role).filter(Role.name == role_data.name).first()
    if existing:
        raise HTTPException(400, "Role name already exists")

    role = Role(
        name=role_data.name,
        permissions=json.dumps(role_data.permissions)
    )
    db.add(role)
    db.commit()
    # Ответ собирается словарём, а не ORM-объектом: `Role.permissions` — текст
    # колонки, а схема ждёт словарь, и отдача объекта падала валидацией ответа
    # уже после commit (роль в базе, клиент видит 500).
    return {
        "id": role.id,
        "name": role.name,
        "permissions": json.loads(role.permissions) if role.permissions else {},
    }

@router.get("/api/roles", response_model=List[RoleResponse])
def get_roles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    roles = db.query(Role).all()
    return [{
        "id": r.id,
        "name": r.name,
        "permissions": json.loads(r.permissions) if r.permissions else {}
    } for r in roles]

@router.get("/api/teams/{team_id}/diagrams")
def get_team_diagrams(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Team not found")
    
    # Проверяем права доступа (разрешаем владельцу команды и участникам с правами просмотра)
    has_access = (
        team.owner_id == current_user.id or
        db.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == current_user.id
        ).first() is not None
    )
    
    if not has_access:
        raise HTTPException(403, detail="Access denied")
    
    diagrams = db.query(Diagram).filter(Diagram.team_id == team_id).all()
    return [{
        "id": d.id,
        "name": d.name,
        "score": d.score,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "folder_id": d.folder_id,
        "team_id": d.team_id
    } for d in diagrams]


# Эндпоинт для получения информации о команде
@router.get("/api/teams/{team_id}")
def get_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).first()
    
    if not team:
        raise HTTPException(404, detail="Team not found or access denied")
    
    owner = db.query(User).filter(User.id == team.owner_id).first()
    members_count = db.query(TeamMember).filter(TeamMember.team_id == team_id).count()
    diagrams_count = db.query(Diagram).filter(Diagram.team_id == team_id).count()
    
    return {
        "id": team.id,
        "name": team.name,
        "color": team.color,
        "owner_id": team.owner_id,
        "owner_name": owner.name if owner else "Unknown",
        "owner_email": owner.email if owner else "Unknown",
        "members_count": members_count,
        "diagrams_count": diagrams_count,
        "created_at": team.created_at
    }

@router.put("/api/teams/{team_id}", response_model=TeamResponse)
def update_team(
    team_id: str,
    team_data: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        Team.owner_id == current_user.id
    ).first()
    
    if not team:
        raise HTTPException(404, "Team not found or access denied")
    
    team.name = team_data.name
    team.color = team_data.color
    db.commit()
    return team

# Эндпоинт для получения роли пользователя в команде
@router.get("/api/teams/{team_id}/role")
def get_user_role_in_team(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Проверяем, что команда существует
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(404, detail="Команда не найдена")
    
    # Если пользователь - владелец команды
    if team.owner_id == current_user.id:
        return {"roleName": "admin"}
    
    # Иначе ищем роль в участниках команды
    member = db.query(TeamMember).filter(
        TeamMember.team_id == team_id,
        TeamMember.user_id == current_user.id
    ).first()
    
    if not member:
        raise HTTPException(403, detail="Доступ запрещен")
    
    return {"roleName": member.role.name}

# Эндпоинт для получения участников команды
@router.get("/api/teams/{team_id}/members")
def get_team_members(
    team_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = db.query(Team).filter(
        Team.id == team_id,
        or_(
            Team.owner_id == current_user.id,
            Team.members.any(TeamMember.user_id == current_user.id)
        )
    ).first()
    
    if not team:
        raise HTTPException(404, detail="Team not found or access denied")
    
    # Один запрос вместо двух на каждого участника: имена и роли снимаются
    # JOIN-ом, причём OUTER — участник с удалённой ролью или удалённым аккаунтом
    # обязан остаться в списке с «Unknown», а не исчезнуть из него.
    rows = (
        db.query(TeamMember.id, TeamMember.user_id, User.name, User.email, Role.name)
        .outerjoin(User, User.id == TeamMember.user_id)
        .outerjoin(Role, Role.id == TeamMember.role_id)
        .filter(TeamMember.team_id == team_id)
        .all()
    )
    return [{
        "id": member_id,
        "user_id": user_id,
        "user_name": user_name or "Unknown",
        "user_email": user_email or "Unknown",
        "role": role_name or "Unknown",
    } for member_id, user_id, user_name, user_email, role_name in rows]



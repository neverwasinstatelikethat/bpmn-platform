"""HTTP-контур принятия улучшений: /health, /api/ai/improve (деградация) и
/api/ai/accept-improvement.

accept-improvement возвращал 500 всегда: после db.delete + db.commit SQLAlchemy
сбрасывает атрибуты объекта (expire_on_commit), и чтение xml_content падало с
DetachedInstanceError уже в момент формирования ответа."""
import uuid
from pathlib import Path

import pytest

if not Path("static").is_dir():
    pytest.skip(
        "app/main.py монтирует ./static — тесты запускают из корня проекта",
        allow_module_level=True,
    )

pytest.importorskip("httpx", reason="TestClient работает поверх httpx")
pytest.importorskip("app.main", reason="зависимости backend не установлены")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Diagram,
    DiagramVersion,
    Improvement,
    User,
)
from app.security import create_access_token  # noqa: E402

ORIGINAL_XML = "<definitions>исходная схема</definitions>"
IMPROVED_XML = "<definitions>схема с проверкой оплаты</definitions>"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    account = User(
        name="Тестовый автор",
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="not-a-real-hash",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@pytest.fixture
def other_user(db):
    account = User(
        name="Чужой",
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="not-a-real-hash",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _headers(account) -> dict:
    token = create_access_token({"sub": account.email})
    return {"Authorization": f"Bearer {token}"}


def _diagram(db, owner_id, xml_content=ORIGINAL_XML):
    diagram = Diagram(
        id=str(uuid.uuid4()),
        name="Тестовая схема",
        xml_content=xml_content,
        user_id=owner_id,
    )
    db.add(diagram)
    db.commit()
    return diagram


def _bump_version(db, diagram, xml_content):
    """Правка через слой хранения: version_seq двигает только record_version."""
    from app.services.versions import record_version

    diagram.xml_content = xml_content
    record_version(db, diagram, source="saved", author_id=diagram.user_id)
    db.commit()


def _improvement(db, owner_id, diagram_id=None, xml_content=IMPROVED_XML, base_seq=None):
    improvement = Improvement(
        id=str(uuid.uuid4()),
        diagram_id=diagram_id,
        user_id=owner_id,
        xml_content=xml_content,
        recommendations="проверить оплату до отгрузки",
        status="pending",
        base_seq=base_seq,
    )
    db.add(improvement)
    db.commit()
    return improvement


class TestImprovementStateMachine:
    """pending → approved | rejected | superseded: решение остаётся в истории."""

    def test_proposal_is_counted_from_current_version(self, db, user):
        from app.services.improvements import PENDING, record_proposal

        diagram = _diagram(db, user.id)
        _bump_version(db, diagram, IMPROVED_XML)
        improvement = record_proposal(
            db, user_id=user.id, diagram_id=diagram.id,
            improved_xml="<definitions>новое</definitions>", recommendations="r",
        )
        assert (improvement.status, improvement.base_seq) == (PENDING, 1)

    def test_second_proposal_supersedes_the_first(self, client, db, user):
        from app.services.improvements import SUPERSEDED, record_proposal

        diagram = _diagram(db, user.id)
        first = record_proposal(
            db, user_id=user.id, diagram_id=diagram.id,
            improved_xml=IMPROVED_XML, recommendations="r1",
        )
        record_proposal(
            db, user_id=user.id, diagram_id=diagram.id,
            improved_xml=IMPROVED_XML, recommendations="r2",
        )
        db.expire_all()
        assert db.get(Improvement, first.id).status == SUPERSEDED

        response = client.post("/api/ai/accept-improvement",
                               json={"improvement_id": first.id},
                               headers=_headers(user))
        assert response.status_code == 404

    def test_accept_after_stale_proposal_conflicts(self, client, db, user):
        diagram = _diagram(db, user.id)
        improvement = _improvement(db, user.id, diagram.id, base_seq=0)
        _bump_version(db, diagram, "<definitions>сохранено после расчёта</definitions>")

        response = client.post("/api/ai/accept-improvement",
                               json={"improvement_id": improvement.id},
                               headers=_headers(user))

        assert response.status_code == 409
        assert "заново" in response.json()["detail"]
        db.expire_all()
        assert db.get(Diagram, diagram.id).xml_content == \
            "<definitions>сохранено после расчёта</definitions>"
        assert db.get(Improvement, improvement.id).status == "pending"

    def test_reject_keeps_decision_and_blocks_accept(self, client, db, user):
        diagram = _diagram(db, user.id)
        improvement = _improvement(db, user.id, diagram.id)
        headers = _headers(user)

        response = client.post("/api/ai/reject-improvement",
                               json={"improvement_id": improvement.id}, headers=headers)
        assert response.status_code == 200
        db.expire_all()
        rejected = db.get(Improvement, improvement.id)
        assert (rejected.status, rejected.diagram_id) == ("rejected", diagram.id)
        assert rejected.decided_at is not None
        assert db.get(Diagram, diagram.id).xml_content == ORIGINAL_XML

        assert client.post("/api/ai/reject-improvement",
                           json={"improvement_id": improvement.id},
                           headers=headers).status_code == 404
        assert client.post("/api/ai/accept-improvement",
                           json={"improvement_id": improvement.id},
                           headers=headers).status_code == 404

    def test_reject_of_foreign_improvement_is_invisible(self, client, db, user, other_user):
        improvement = _improvement(db, other_user.id, diagram_id=None)
        assert client.post("/api/ai/reject-improvement",
                           json={"improvement_id": improvement.id},
                           headers=_headers(user)).status_code == 404


class TestHealthAndDegradation:
    def test_health_answers_without_database(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_improve_is_disabled_without_llm(self, client, user):
        response = client.post(
            "/api/ai/improve",
            json={"bpmn_xml": ORIGINAL_XML, "prompt": "найди узкие места"},
            headers=_headers(user),
        )
        assert response.status_code == 503
        assert "SKIP_LLM_INIT" in response.json()["detail"]

    def test_improve_rejects_anonymous(self, client):
        response = client.post(
            "/api/ai/improve",
            json={"bpmn_xml": ORIGINAL_XML, "prompt": "улучши"},
        )
        assert response.status_code == 401


class TestAcceptImprovement:
    def test_saved_xml_replaces_diagram_and_improvement_is_consumed(
        self, client, db, user
    ):
        diagram = _diagram(db, user.id)
        improvement = _improvement(db, user.id, diagram.id)
        diagram_id, improvement_id = diagram.id, improvement.id

        response = client.post(
            "/api/ai/accept-improvement",
            json={"improvement_id": improvement_id},
            headers=_headers(user),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["xml_content"] == IMPROVED_XML
        assert body["diagram_id"] == diagram_id
        db.expire_all()
        assert db.query(Diagram).get(diagram_id).xml_content == IMPROVED_XML
        # Предложение не удаляется: оно остаётся в истории со статусом решения.
        assert db.get(Improvement, improvement_id).status == "approved"
        assert db.get(Improvement, improvement_id).decided_at is not None
        version = db.query(DiagramVersion).filter_by(diagram_id=diagram_id).one()
        assert (version.seq, version.source, version.xml_content) == (1, "approved", IMPROVED_XML)

    def test_repeated_accept_is_not_found(self, client, db, user):
        diagram = _diagram(db, user.id)
        improvement = _improvement(db, user.id, diagram.id)
        headers = _headers(user)
        payload = {"improvement_id": improvement.id}

        assert client.post("/api/ai/accept-improvement", json=payload,
                           headers=headers).status_code == 200
        second = client.post("/api/ai/accept-improvement", json=payload,
                             headers=headers)
        assert second.status_code == 404
        assert second.json()["detail"] == "Improvement not found"

    def test_without_diagram_new_one_is_created(self, client, db, user):
        improvement = _improvement(db, user.id, diagram_id=None)

        response = client.post(
            "/api/ai/accept-improvement",
            json={"improvement_id": improvement.id},
            headers=_headers(user),
        )

        assert response.status_code == 200, response.text
        new_id = response.json()["diagram_id"]
        db.expire_all()
        created = db.query(Diagram).get(new_id)
        assert created.user_id == user.id
        assert created.xml_content == IMPROVED_XML

    def test_someone_elses_improvement_is_invisible(self, client, db, user, other_user):
        diagram = _diagram(db, user.id)
        improvement = _improvement(db, user.id, diagram.id)

        response = client.post(
            "/api/ai/accept-improvement",
            json={"improvement_id": improvement.id},
            headers=_headers(other_user),
        )

        assert response.status_code == 404
        db.expire_all()
        assert db.query(Diagram).get(diagram.id).xml_content == ORIGINAL_XML

    def test_denied_diagram_access_keeps_improvement(self, client, db, user, other_user):
        foreign_diagram = _diagram(db, other_user.id)
        improvement = _improvement(db, user.id, foreign_diagram.id)

        response = client.post(
            "/api/ai/accept-improvement",
            json={"improvement_id": improvement.id},
            headers=_headers(user),
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Diagram not found or access denied"
        db.expire_all()
        assert db.get(Improvement, improvement.id).status == "pending"

    def test_anonymous_is_unauthorized(self, client):
        response = client.post(
            "/api/ai/accept-improvement", json={"improvement_id": "any"}
        )
        assert response.status_code == 401

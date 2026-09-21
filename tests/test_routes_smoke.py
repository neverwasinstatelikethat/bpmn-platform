"""Смоук всех роутеров пакета app/: запрос → ожидаемый код.

Защищает перенос маршрутов из монолита: тест снимает фактическое поведение
эндпоинтов (включая предсуществующие 404 в сценариях удаления), поэтому
случайно изменённый контракт или потерянное при переносе имя видны сразу.
"""
import uuid

import pytest

pytest.importorskip("httpx", reason="TestClient работает поверх httpx")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Diagram,
    DiagramVersion,
    Folder,
    Improvement,
    PasswordResetToken,
    ShareToken,
    TeamMember,
    User,
)

XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">'
    '<process id="P" name="P" isExecutable="true">'
    '<startEvent id="S" name="Start"/><endEvent id="E" name="End"/></process></definitions>'
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth(client):
    email = f"routes-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/register", json={"name": "Routes", "email": email,
                                              "password": "Passw0rd!"}).status_code == 200
    token = client.post("/api/login", json={"email": email,
                                           "password": "Passw0rd!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, email


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def register(client, label: str) -> dict:
    """Заголовки второстепенного пользователя: чужие диаграммы и команды."""
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/register", json={"name": label, "email": email,
                                       "password": "Passw0rd!"})
    token = client.post("/api/login", json={"email": email,
                                            "password": "Passw0rd!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def diagram(client, auth):
    headers, _ = auth
    diagram_id = str(uuid.uuid4())
    response = client.post("/api/diagrams",
                           json={"id": diagram_id, "name": "D", "xml": XML, "score": 10},
                           headers=headers)
    assert response.status_code == 200
    return diagram_id


@pytest.fixture(scope="module")
def team(client, auth):
    headers, _ = auth
    response = client.post("/api/teams", json={"name": "Продажи", "color": "#ff0000"},
                           headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


class TestPublicRoutes:
    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_anonymous_cannot_read_registry(self, client):
        assert client.get("/api/diagrams").status_code == 401

    def test_openapi_documents_every_router(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for route in ("/health", "/api/register", "/api/diagrams", "/api/folders",
                      "/api/teams", "/api/roles", "/api/evaluate", "/api/generate",
                      "/api/ai/improve", "/api/ai/accept-improvement",
                      "/api/ai/reject-improvement", "/api/share",
                      "/api/diagrams/{diagram_id}/versions",
                      "/api/diagrams/{diagram_id}/versions/{seq}",
                      "/api/diagrams/{diagram_id}/restore/{seq}"):
            assert route in paths, route


class TestRegistryRoutes:
    def test_foreign_diagram_is_not_overwritten_by_id(self, client, auth, diagram):
        """Сохранение с чужим id — 404, а не тихая перезапись."""
        headers, _ = auth
        response = client.post("/api/diagrams", headers=register(client, "stranger"), json={
            "id": diagram, "name": "Подменена", "xml": "<definitions>чужие правки</definitions>",
            "score": 0,
        })
        assert response.status_code == 404
        body = client.get(f"/api/diagrams/{diagram}", headers=headers)
        assert body.json()["xml_content"] == XML

    def test_registry_save_matches_column_bounds(self, client, auth):
        """Границы схемы = границы колонок и ИИ-маршрутов: на PostgreSQL
        переполнение дало бы 500 вместо честного 422."""
        headers, _ = auth
        assert client.post("/api/diagrams", headers=headers, json={
            "name": "Н" * 101, "xml": XML, "score": 10}).status_code == 422
        assert client.post("/api/diagrams", headers=headers, json={
            "name": "   ", "xml": XML, "score": 10}).status_code == 422
        for bad_score in (-1, 101):
            assert client.post("/api/diagrams", headers=headers, json={
                "name": "ок", "xml": XML, "score": bad_score}).status_code == 422

    def test_diagram_lifecycle(self, client, auth, diagram):
        headers, _ = auth
        assert client.get("/api/diagrams", headers=headers).status_code == 200
        body = client.get(f"/api/diagrams/{diagram}", headers=headers)
        assert body.status_code == 200
        assert body.json()["xml_content"] == XML

    def test_folder_tree_and_move(self, client, auth, diagram):
        headers, _ = auth
        folder_id = client.post("/api/folders", json={"name": "Продажи"},
                                headers=headers).json()["folder_id"]
        assert client.get("/api/folders", headers=headers).status_code == 200
        assert client.get("/api/folders/tree", headers=headers).status_code == 200
        assert client.get(f"/api/folders/{folder_id}/diagrams", headers=headers).status_code == 200
        assert client.post("/api/diagrams/move-to-folder",
                           json={"diagram_id": diagram, "folder_id": folder_id},
                           headers=headers).status_code == 200

    def test_share_link_created(self, client, auth, diagram):
        headers, _ = auth
        response = client.post("/api/share", json={"diagram_id": diagram, "can_edit": True},
                               headers=headers)
        assert response.status_code == 200
        assert "/share/" in response.json()["share_link"]

    def test_soft_delete_lists_recycle_bin(self, client, auth):
        headers, _ = auth
        victim = str(uuid.uuid4())
        client.post("/api/diagrams", json={"id": victim, "name": "V", "xml": XML, "score": 1},
                    headers=headers)
        assert client.delete(f"/api/diagrams/{victim}", headers=headers).status_code == 200
        deleted = client.get("/api/deleted-diagrams", headers=headers).json()
        assert [d["diagram_id"] for d in deleted] == [victim]


class TestDiagramHistory:
    """Версии схемы: история растёт только на содержательную правку, откат — новая версия."""

    def test_saving_grows_history_and_identical_save_does_not(self, client, auth):
        headers, _ = auth
        diagram_id = str(uuid.uuid4())
        first = {"id": diagram_id, "name": "H", "xml": XML, "score": 5}
        assert client.post("/api/diagrams", json=first,
                           headers=headers).json()["version_seq"] == 1
        assert client.post("/api/diagrams", json=first,
                           headers=headers).json()["version_seq"] == 1

        second = dict(first, xml=XML.replace('id="P"', 'id="P2"'), score=9)
        assert client.post("/api/diagrams", json=second,
                           headers=headers).json()["version_seq"] == 2

        versions = client.get(f"/api/diagrams/{diagram_id}/versions", headers=headers).json()
        assert [v["seq"] for v in versions] == [2, 1]
        assert [v["source"] for v in versions] == ["saved", "created"]
        assert versions[0]["score"] == 9

        body = client.get(f"/api/diagrams/{diagram_id}/versions/1", headers=headers).json()
        assert body["xml_content"] == XML

    def test_restore_replays_history_without_losing_it(self, client, auth):
        headers, _ = auth
        diagram_id = str(uuid.uuid4())
        client.post("/api/diagrams", json={"id": diagram_id, "name": "R", "xml": XML,
                                           "score": 1}, headers=headers)
        client.post("/api/diagrams", json={"id": diagram_id, "name": "R",
                                           "xml": XML.replace('id="P"', 'id="P2"'),
                                           "score": 2}, headers=headers)

        response = client.post(f"/api/diagrams/{diagram_id}/restore/1", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["xml_content"] == XML
        assert response.json()["version_seq"] == 3
        assert client.get(f"/api/diagrams/{diagram_id}", headers=headers) \
            .json()["xml_content"] == XML
        sources = [v["source"] for v in
                   client.get(f"/api/diagrams/{diagram_id}/versions", headers=headers).json()]
        assert sources == ["restored", "saved", "created"]

    def test_unknown_version_and_foreign_owner_are_not_found(self, client, auth):
        headers, _ = auth
        diagram_id = str(uuid.uuid4())
        client.post("/api/diagrams", json={"id": diagram_id, "name": "V", "xml": XML,
                                           "score": 0}, headers=headers)
        stranger = register(client, "hist-stranger")
        assert client.get(f"/api/diagrams/{diagram_id}/versions/77",
                          headers=headers).status_code == 404
        assert client.post(f"/api/diagrams/{diagram_id}/restore/77",
                           headers=headers).status_code == 404
        assert client.get(f"/api/diagrams/{diagram_id}/versions",
                          headers=stranger).status_code == 404
        assert client.post(f"/api/diagrams/{diagram_id}/restore/1",
                           headers=stranger).status_code == 404


class TestTeamRoutes:
    def test_team_reads(self, client, auth, team):
        headers, _ = auth
        for path in (f"/api/teams/{team}", f"/api/teams/{team}/members",
                     f"/api/teams/{team}/role", f"/api/teams/{team}/diagrams"):
            assert client.get(path, headers=headers).status_code == 200, path
        assert client.get("/api/teams", headers=headers).status_code == 200
        assert client.get("/api/roles", headers=headers).status_code == 200

    def test_team_update_and_invitations(self, client, auth, team):
        headers, _ = auth
        assert client.put(f"/api/teams/{team}", json={"name": "Продажи 2", "color": "#00ff00"},
                          headers=headers).status_code == 200
        assert client.post(f"/api/teams/{team}/invite",
                           json={"email": "guest@example.com", "role": "viewer"},
                           headers=headers).status_code == 200
        assert client.post(f"/api/teams/{team}/invite-domain",
                           json={"domain": "vkusvill.ru", "role": "viewer"},
                           headers=headers).status_code == 200

    def test_foreign_team_is_invisible(self, client, team):
        assert client.get(f"/api/teams/{team}",
                          headers=register(client, "other")).status_code == 404


class TestInvitationAccept:
    """Приглашение выдаёт доступ адресату, а не тому, у кого оказалась ссылка."""

    @staticmethod
    def _account(client, email: str) -> dict:
        client.post("/api/register", json={"name": "Inv", "email": email,
                                           "password": "Passw0rd!"})
        token = client.post("/api/login", json={"email": email,
                                                "password": "Passw0rd!"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _invite(self, client, headers, email):
        team_id = client.post("/api/teams", headers=headers,
                              json={"name": "Отдел", "color": "#123456"}).json()["id"]
        body = client.post(f"/api/teams/{team_id}/invite", headers=headers,
                           json={"email": email, "role": "admin"}).json()
        return team_id, body["accept_link"].rsplit("/", 1)[-1]

    def test_addressee_joins_and_answer_is_serializable(self, client, auth, db):
        """Ответ раньше 500-ился на role (схеме нужна строка, у модели —
        связывание), хотя участник уже был записан."""
        headers, _ = auth
        email = f"inv-{uuid.uuid4().hex[:8]}@example.com"
        team_id, token = self._invite(client, headers, email)

        response = client.get(f"/api/invitations/accept/{token}",
                              headers=self._account(client, email))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "admin"
        assert body["status"] == "accepted"
        members = db.query(TeamMember).filter_by(team_id=team_id).all()
        assert len(members) == 2          # владелец + адресат
        db.expunge_all()

    def test_link_forwarded_to_another_account_is_refused(self, client, auth, db):
        headers, _ = auth
        team_id, token = self._invite(client, headers,
                                      f"victim-{uuid.uuid4().hex[:8]}@example.com")
        stranger = register(client, "nosuchuser")
        before = db.query(TeamMember).filter_by(team_id=team_id).count()

        response = client.get(f"/api/invitations/accept/{token}", headers=stranger)
        assert response.status_code == 403
        db.expire_all()
        assert db.query(TeamMember).filter_by(team_id=team_id).count() == before


class TestProfileAndMailRoutes:
    def test_profile_update(self, client, auth):
        headers, _ = auth
        response = client.put("/api/profile", headers=headers, json={
            "name": "Новое имя", "position": "аналитик", "company": "ВкусВилл",
            "website": "", "about": "",
        })
        assert response.status_code == 200
        assert response.json()["position"] == "аналитик"

    def test_password_reset_survives_dead_smtp(self, client, auth):
        """Ответ не должен падать, если почтовый сервер недоступен."""
        _, email = auth
        assert client.post("/api/reset-password-request", json={"email": email}).status_code == 200

    def test_verify_token_rejects_unknown(self, client):
        assert client.post("/api/verify-reset-token", params={"token": "abc"}).status_code == 200


class TestPasswordResetFlow:
    """Сброс пароля по настоящему токену.

    До этого маршрут проверяли только на «мёртвом» SMTP и неизвестном токене,
    поэтому условие `used` в запросе токена нигде не проверялось: оно решает,
    найдётся ли живой токен, и его же ломает любая неаккуратная правка фильтра.
    """

    @staticmethod
    def _token(db, email) -> str:
        row = (db.query(PasswordResetToken)
                 .filter(PasswordResetToken.email == email,
                         PasswordResetToken.used.is_(False))
                 .order_by(PasswordResetToken.created_at.desc())
                 .first())
        assert row is not None, "токен сброса не создан"
        return row.token

    def test_token_resets_password_and_is_single_use(self, client, db):
        email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
        assert client.post("/api/register", json={"name": "Reset", "email": email,
                                                 "password": "Passw0rd!"}).status_code == 200
        assert client.post("/api/reset-password-request",
                           json={"email": email}).status_code == 200
        token = self._token(db, email)

        assert client.post("/api/verify-reset-token",
                           params={"token": token}).json() == {"valid": True}
        response = client.post("/api/reset-password",
                               json={"token": token, "new_password": "N3wPassw0rd!"})
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

        assert client.post("/api/login", json={"email": email,
                                              "password": "Passw0rd!"}).status_code == 401
        assert client.post("/api/login", json={"email": email,
                                              "password": "N3wPassw0rd!"}).status_code == 200

        # Токен одноразовый: использованный не проверяется и не принимается.
        assert client.post("/api/verify-reset-token",
                           params={"token": token}).json() == {"valid": False}
        assert client.post("/api/reset-password",
                           json={"token": token,
                                 "new_password": "Another123!"}).status_code == 400

    def test_new_request_voids_the_previous_link(self, client, db):
        """Ссылка из первого письма перестаёт действовать после повторного
        запроса — иначе старая ходит столько же, сколько новая."""
        email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
        client.post("/api/register", json={"name": "Reset", "email": email,
                                          "password": "Passw0rd!"})
        client.post("/api/reset-password-request", json={"email": email})
        first = self._token(db, email)

        client.post("/api/reset-password-request", json={"email": email})
        second = self._token(db, email)
        assert second != first
        assert client.post("/api/verify-reset-token",
                           params={"token": first}).json() == {"valid": False}
        assert client.post("/api/verify-reset-token",
                           params={"token": second}).json() == {"valid": True}


class TestBpmnImport:
    """Загрузка .bpmn-файла в реестр.

    Маршрут не был покрыт ничем и потому отдавал 500 на каждом запросе:
    константа лимита размера использовалась, но не была импортирована.
    """

    def test_valid_file_is_saved_and_listed(self, client, auth):
        headers, _ = auth
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("заявка.bpmn", XML, "application/xml")})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["name"] == "заявка"
        listed = client.get("/api/diagrams", headers=headers).json()
        assert body["diagram_id"] in {d["id"] for d in listed}

    def test_xml_extension_is_accepted_too(self, client, auth):
        headers, _ = auth
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("process.xml", XML, "application/xml")})
        assert response.status_code == 200, response.text

    def test_other_extension_is_rejected(self, client, auth):
        headers, _ = auth
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("process.txt", XML, "text/plain")})
        assert response.status_code == 400
        assert ".bpmn" in response.json()["detail"]

    def test_oversize_file_is_rejected_before_parsing(self, client, auth):
        """Без лимита на входе один запрос с файлом съедает память процесса,
        а число в ответе обязано совпадать с настроенным лимитом."""
        from app.config import MAX_UPLOAD_BYTES
        headers, _ = auth
        payload = XML + "<!--" + "x" * MAX_UPLOAD_BYTES + "-->"
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("big.bpmn", payload, "application/xml")})
        assert response.status_code == 413
        assert response.json()["detail"] == "Файл больше 2 МБ"

    def test_non_bpmn_xml_is_rejected(self, client, auth):
        headers, _ = auth
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("x.bpmn", "<foo/>", "application/xml")})
        assert response.status_code == 400
        assert "definitions" in response.json()["detail"]

    def test_broken_xml_is_rejected(self, client, auth):
        headers, _ = auth
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("x.bpmn", "<definitions", "application/xml")})
        assert response.status_code == 400

    def test_external_entity_is_not_resolved(self, client, auth):
        """Загруженный файл недоверен: XML с внешней сущностью обязан быть
        отвергнут парсером, а не прочитан."""
        headers, _ = auth
        before = len(client.get("/api/diagrams", headers=headers).json())
        xxe = ('<?xml version="1.0"?>'
               '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" '
               'id="D">&xxe;</definitions>')
        response = client.post("/api/import-bpmn", headers=headers,
                               files={"file": ("xxe.bpmn", xxe, "application/xml")})
        assert response.status_code == 400
        # Содержимое чужого файла не попадает в ответ и не сохраняется.
        assert "root:" not in response.text
        assert len(client.get("/api/diagrams", headers=headers).json()) == before


class TestAIRoutes:
    def test_evaluate_scores_xml(self, client, auth):
        headers, _ = auth
        result = client.post("/api/evaluate", json={"bpmn_xml": XML}, headers=headers).json()
        assert 0 <= result["score"] <= 100
        # Схема не меняется: скоринг только измеряет, «оптимизированный XML»
        # во временных рамках подменял ветки шлюзов на condition=true.
        assert "optimized_bpmn" not in result
        assert result["details"] and result["details_meta"]

    def test_evaluate_reports_every_rule(self, client, auth):
        """Форма ответа не должна зависеть от схемы: ключи правил на месте
        всегда, иначе панель скоринга молча теряет проверки."""
        headers, _ = auth
        from core.bpmn_scoring import BPMNScorer

        result = client.post("/api/evaluate", json={"bpmn_xml": XML},
                             headers=headers).json()
        assert set(result["details"]) == set(BPMNScorer().rules)
        assert set(result["details_meta"]) == set(BPMNScorer().rules)
        for meta in result["details_meta"].values():
            assert meta["status"] in ("passed", "failed", "not_applicable")
            assert isinstance(meta["elements"], list)

    def test_export_endpoint_is_gone(self, client, auth):
        """PNG/PDF рисует фронтенд из bpmn-js; эндпоинт отдавал байты XML под
        видом графики, то есть врал пользователю."""
        headers, _ = auth
        assert client.post("/api/export", json={"bpmn_xml": XML, "format": "png"},
                           headers=headers).status_code == 404

    def test_generate_requires_non_empty_text(self, client, auth):
        headers, _ = auth
        assert client.post("/api/generate", json={"text": "   "},
                           headers=headers).status_code == 422

    def test_generate_passes_repair_notes(self, client, auth, monkeypatch):
        """notes — единственное, что показывает пользователю починку структуры."""
        headers, _ = auth
        from app.routers import ai as ai_router

        def fake_generate(text):
            return {"status": "success", "bpmn": XML, "structure": {"elements": []},
                    "notes": ["Элемент T1 перенесён в пул «Клиент»"]}

        monkeypatch.setattr(ai_router.generator, "generate", fake_generate)
        result = client.post("/api/generate", json={"text": "заявка, счёт"},
                             headers=headers).json()
        assert result["notes"] == ["Элемент T1 перенесён в пул «Клиент»"]

    def test_generate_maps_provider_outage_to_503(self, client, auth, monkeypatch):
        """Отказ провайдера — не «плохой запрос»: клиент обязан ретраить."""
        headers, _ = auth
        from app.routers import ai as ai_router

        monkeypatch.setattr(ai_router.generator, "generate", lambda text: {
            "status": "error", "step": "llm", "error": "Сервис временно недоступен"})
        response = client.post("/api/generate", json={"text": "заявка"}, headers=headers)
        assert response.status_code == 503
        assert response.headers.get("retry-after")

    def test_generate_maps_truncation_to_400(self, client, auth, monkeypatch):
        headers, _ = auth
        from app.routers import ai as ai_router

        monkeypatch.setattr(ai_router.generator, "generate", lambda text: {
            "status": "error", "step": "llm_truncated", "error": "Ответ обрезан"})
        assert client.post("/api/generate", json={"text": "заявка"},
                           headers=headers).status_code == 400

    def test_ai_budget_blocks_after_hourly_limit(self, client, auth, monkeypatch):
        from app import deps

        # Отдельный пользователь: счётчик окна живой, и чужие слоты в этом
        # тесте тратить нельзя.
        headers = register(client, "budget")
        monkeypatch.setattr(deps._ai_window, "limit", 2)
        codes = [client.post("/api/evaluate", json={"bpmn_xml": XML},
                             headers=headers).status_code for _ in range(3)]
        assert codes[:2] == [200, 200]
        assert codes[2] == 429

    def test_improve_reports_disabled_service(self, client, auth):
        """SKIP_LLM_INIT=1: 503 вместо неопределённого имени в обработчике."""
        headers, _ = auth
        response = client.post("/api/ai/improve", json={"bpmn_xml": XML, "prompt": "улучши"},
                               headers=headers)
        assert response.status_code == 503

    def test_improve_keeps_analysis_only_in_history(self, client, auth, diagram,
                                                    monkeypatch):
        """«Найди узкие места» без правок схемы должен оставаться в истории."""
        headers, _ = auth
        from app import ai as ai_module
        from app.models import Improvement

        class FakeOrchestrator:
            async def improve_diagram(self, xml_content, user_prompt):
                return ("Узкое место — согласование", None,
                        {"status": "analysis_only", "applied": [], "skipped": [],
                         "truncated_operations": 0})

        monkeypatch.setattr(ai_module, "_orchestrator", FakeOrchestrator())
        response = client.post("/api/ai/improve",
                               json={"bpmn_xml": XML, "prompt": "найди узкие места",
                                     "diagram_id": diagram}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "analysis_only" and body["improvement_id"]

        from app.db import SessionLocal
        session = SessionLocal()
        try:
            row = session.query(Improvement).filter(
                Improvement.id == body["improvement_id"]).first()
            assert row.status == "analysis_only"
            assert row.xml_content is None
            assert "согласование" in row.recommendations
        finally:
            session.close()

    def test_improve_marks_pending_proposal_unacceptable(self, client, auth, diagram,
                                                         monkeypatch):
        """Терминальный статус анализа нельзя «принять» как изменение схемы."""
        headers, _ = auth
        from app import ai as ai_module

        class FakeOrchestrator:
            async def improve_diagram(self, xml_content, user_prompt):
                return ("Всё хорошо", None, {"status": "analysis_only", "applied": [],
                                             "skipped": [], "truncated_operations": 0})

        monkeypatch.setattr(ai_module, "_orchestrator", FakeOrchestrator())
        improvement_id = client.post(
            "/api/ai/improve",
            json={"bpmn_xml": XML, "prompt": "проверь", "diagram_id": diagram},
            headers=headers).json()["improvement_id"]
        assert client.post("/api/ai/accept-improvement",
                           json={"improvement_id": improvement_id},
                           headers=headers).status_code == 404

    def test_improve_checks_diagram_access_before_service_state(self, client, auth,
                                                                diagram):
        """diagram_id привязывает предложение к хранённой схеме и снимает чужие
        незакрытые предложения, поэтому проверяется до доступности сервиса:
        чужая и вымышленная схема отвечают 404, а не 503."""
        outsider = register(client, "improve-outsider")
        headers, _ = auth
        body = {"bpmn_xml": XML, "prompt": "улучши"}

        assert client.post("/api/ai/improve", headers=headers,
                           json={**body, "diagram_id": diagram}).status_code == 503
        for foreign in (diagram, "no-such-diagram"):
            response = client.post("/api/ai/improve", headers=outsider,
                                   json={**body, "diagram_id": foreign})
            assert response.status_code == 404, (foreign, response.text)

    def test_accept_unknown_improvement(self, client, auth):
        headers, _ = auth
        assert client.post("/api/ai/accept-improvement", json={"improvement_id": "нет"},
                           headers=headers).status_code == 404


@pytest.fixture
def fk_enforced():
    """Внешние ключи SQLite включаются только на соединении и по умолчанию
    выключены: без прагмы тест проверял бы данные, которые целевой PostgreSQL
    отвергает. На PostgreSQL прогоне подмена не нужна — там FK работают всегда.
    """
    if engine.dialect.name != "sqlite":
        yield
        return

    def _turn_on(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(engine, "connect", _turn_on)
    # Прагма ставится при создании соединения, поэтому пул сбрасываем и до,
    # и после: иначе запрос маршрута ушёл бы в старое соединение без FK.
    engine.dispose()
    try:
        yield
    finally:
        event.remove(engine, "connect", _turn_on)
        engine.dispose()


class TestFolderSubtreeDeletion:
    """Удаление папки снимает всё поддерево и всё, что ссылается на его схемы.

    Раньше уходили bulk-DELETE на один уровень вложенности, а FK у
    diagram_versions, share_tokens и pending_improvements объявлены без
    ondelete: PostgreSQL отклонял весь запрос, SQLite без внешних ключей молча
    оставлял сирот и висячий parent_id у внуков.
    """

    @staticmethod
    def _folder(client, headers, name, parent_id=None):
        return client.post("/api/folders", headers=headers,
                           json={"name": name, "parent_id": parent_id}
                           ).json()["folder_id"]

    @staticmethod
    def _diagram_in(client, headers, folder_id, name):
        diagram_id = str(uuid.uuid4())
        assert client.post("/api/diagrams", headers=headers, json={
            "id": diagram_id, "name": name, "xml": XML, "score": 3}).status_code == 200
        assert client.post("/api/diagrams/move-to-folder", headers=headers, json={
            "diagram_id": diagram_id, "folder_id": folder_id}).status_code == 200
        return diagram_id

    def test_root_folder_deletion_leaves_no_orphans(self, fk_enforced, client, auth, db):
        headers, email = auth
        root = self._folder(client, headers, "Корень")
        child = self._folder(client, headers, "Внутри", parent_id=root)
        grandchild = self._folder(client, headers, "Глубже", parent_id=child)
        doomed = [self._diagram_in(client, headers, folder, f"D{i}")
                  for i, folder in enumerate((root, child, grandchild))]

        # Ссылки на обречённые схемы — ровно те строки, из-за которых удаление
        # и разваливается на СУБД, соблюдающей внешние ключи.
        user_id = db.query(User.id).filter(User.email == email).scalar()
        db.add(ShareToken(id=str(uuid.uuid4()), token=str(uuid.uuid4()),
                          diagram_id=doomed[1]))
        db.add(Improvement(id=str(uuid.uuid4()), diagram_id=doomed[2], user_id=user_id,
                           status="pending", xml_content=XML, recommendations="r"))
        db.commit()

        sibling = self._folder(client, headers, "Сосед")
        kept = self._diagram_in(client, headers, sibling, "S")

        response = client.delete(f"/api/folders/{root}",
                                 params={"delete_contents": True}, headers=headers)
        assert response.status_code == 200, response.text

        db.expire_all()
        assert db.query(Folder).filter(Folder.id.in_([root, child, grandchild])).count() == 0
        assert db.query(Diagram).filter(Diagram.id.in_(doomed)).count() == 0
        for model in (DiagramVersion, ShareToken, Improvement):
            assert db.query(model).filter(model.diagram_id.in_(doomed)).count() == 0, model
        assert db.query(Folder).filter(Folder.id == sibling).count() == 1
        assert db.query(Diagram).filter(Diagram.id == kept).one().folder_id == sibling

    def test_without_flag_subtree_folders_go_but_diagrams_survive(self, fk_enforced,
                                                                  client, auth, db):
        """delete_contents=false фронтенд переводит как «папку удалить, схемы
        сохранить»: висячего folder_id остаться не должно."""
        headers, _ = auth
        root = self._folder(client, headers, "Корень")
        child = self._folder(client, headers, "Внутри", parent_id=root)
        kept = self._diagram_in(client, headers, child, "U")

        response = client.delete(f"/api/folders/{root}", headers=headers)
        assert response.status_code == 200, response.text

        db.expire_all()
        assert db.query(Folder).filter(Folder.id.in_([root, child])).count() == 0
        assert db.query(Diagram).filter(Diagram.id == kept).one().folder_id is None
        assert db.query(DiagramVersion).filter(
            DiagramVersion.diagram_id == kept).count() == 1

    def test_cycle_in_parent_id_terminates(self, fk_enforced, client, auth, db):
        """Маршрут цикла не создаёт, но в легаси-базе он возможен: обход обязан
        остановиться и унести обе папки."""
        headers, _ = auth
        first = self._folder(client, headers, "A")
        second = self._folder(client, headers, "B", parent_id=first)
        db.get(Folder, first).parent_id = second
        db.commit()

        assert client.delete(f"/api/folders/{first}",
                             params={"delete_contents": True},
                             headers=headers).status_code == 200
        db.expire_all()
        assert db.query(Folder).filter(Folder.id.in_([first, second])).count() == 0


class TestVersionSequenceRace:
    """seq считается max+1: два параллельных сохранения не должны делить его."""

    def test_colliding_seq_is_retried_and_save_survives(self, client, auth, db, monkeypatch):
        from app.services import versions as versions_service
        from app.services.versions import record_version

        headers, email = auth
        user_id = db.query(User.id).filter(User.email == email).scalar()
        diagram_id = str(uuid.uuid4())
        assert client.post("/api/diagrams", headers=headers, json={
            "id": diagram_id, "name": "R", "xml": XML, "score": 1}).status_code == 200

        saved_xml = XML.replace('id="P"', 'id="P2"')
        original_now = versions_service.utc_now
        raced = []

        def racing_now():
            """Вставляет снимок конкурента в тот зазор, где record_version уже
            посчитал seq=2 и ещё не вставил свой row — так гонка двух
            автосохранений воспроизводится детерминированно."""
            if not raced:
                raced.append(True)
                other = SessionLocal()
                try:
                    other.add(DiagramVersion(id=str(uuid.uuid4()), diagram_id=diagram_id,
                                             seq=2, xml_content=XML.replace('id="P"', 'id="P9"'),
                                             score=7, source="saved", author_id=user_id))
                    other.commit()
                finally:
                    other.close()
            return original_now()

        monkeypatch.setattr(versions_service, "utc_now", racing_now)
        session = SessionLocal()
        try:
            diagram = session.get(Diagram, diagram_id)
            diagram.xml_content = saved_xml
            version = record_version(session, diagram, source="saved", author_id=user_id)
            # Здесь раньше всплывал IntegrityError: транзакция откатывалась, и
            # автосохранение пользователя исчезало вместе с seq.
            session.commit()
            assert version.seq == 3
            assert version.xml_content == saved_xml
        finally:
            session.close()

        db.expire_all()
        stored = db.get(Diagram, diagram_id)
        assert stored.xml_content == saved_xml
        assert stored.version_seq == 3
        assert [v.seq for v in db.query(DiagramVersion).filter(
            DiagramVersion.diagram_id == diagram_id).order_by(DiagramVersion.seq).all()
        ] == [1, 2, 3]


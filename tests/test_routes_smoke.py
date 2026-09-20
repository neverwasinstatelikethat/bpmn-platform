"""Смоук всех роутеров пакета app/: запрос → ожидаемый код.

Защищает перенос маршрутов из монолита: тест снимает фактическое поведение
эндпоинтов (включая предсуществующие 404 в сценариях удаления), поэтому
случайно изменённый контракт или потерянное при переносе имя видны сразу.
"""
import uuid

import pytest

pytest.importorskip("httpx", reason="TestClient работает поверх httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

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
                      "/api/ai/improve", "/api/ai/accept-improvement", "/api/share"):
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


class TestAIRoutes:
    def test_evaluate_scores_xml(self, client, auth):
        headers, _ = auth
        result = client.post("/api/evaluate", json={"bpmn_xml": XML}, headers=headers).json()
        assert 0 <= result["score"] <= 100
        assert result["optimized_bpmn"].lstrip().startswith("<")
        assert "bpmn:definitions" in result["optimized_bpmn"] or "<definitions" in result["optimized_bpmn"]

    def test_export_rejects_unknown_format(self, client, auth):
        headers, _ = auth
        assert client.post("/api/export", json={"bpmn_xml": XML, "format": "docx"},
                           headers=headers).json()["status"] == "error"

    def test_improve_reports_disabled_service(self, client, auth):
        """SKIP_LLM_INIT=1: 503 вместо неопределённого имени в обработчике."""
        headers, _ = auth
        response = client.post("/api/ai/improve", json={"bpmn_xml": XML, "prompt": "улучши"},
                               headers=headers)
        assert response.status_code == 503

    def test_accept_unknown_improvement(self, client, auth):
        headers, _ = auth
        assert client.post("/api/ai/accept-improvement", json={"improvement_id": "нет"},
                           headers=headers).status_code == 404

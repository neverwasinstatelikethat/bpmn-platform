"""Контур CORS: смена адреса фронтенда не должна ронять все маршруты, а 500 —
притворяться ошибкой CORS.

Файл появился после разбора «не работает логин». Backend принимал ровно один
источник (`FRONTEND_URL`, по умолчанию `http://localhost:3456`), а Compose
публикует фронтенд на `127.0.0.1:3456`: браузер, открывший адрес, который
показывает Docker, получал отказ на каждом запросе, включая `/api/login`.
Второй слой того же обмана: необработанное исключение отдаёт
`ServerErrorMiddleware`, который стоит вне `CORSMiddleware`, поэтому ответ 500
приходил без `Access-Control-Allow-Origin`, и DevTools писали «blocked by CORS
policy» там, где сервер честно упал.
"""
import pytest

pytest.importorskip("httpx", reason="TestClient работает поверх httpx")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import FRONTEND_URL, _loopback_alias  # noqa: E402
from app.deps import RETRY_AFTER_HEADER  # noqa: E402
from app.main import app  # noqa: E402


def _twin(origin: str) -> str:
    """Второй адрес того же порта: localhost ↔ 127.0.0.1.

    Это не «ещё один хост», а то же самое приложение: так человек, открывший
    ссылку из вывода `docker compose`, не получает отказ в авторизации.
    """
    for alias in ("localhost", "127.0.0.1"):
        if alias in origin:
            return origin.replace(alias, "127.0.0.1" if alias == "localhost" else "localhost")
    pytest.fail(f"в конфигурации ожидается адрес с localhost или 127.0.0.1, получено {origin!r}")


@pytest.fixture
def client():
    # raise_server_exceptions=False — как в браузере: исключение эндпоинта
    # превращается в ответ 500, а не долетает до теста.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def failing_route():
    """Временный эндпоинт, который падает: так проверяется слой, а не чужой баг."""
    def boom():
        raise RuntimeError("секрет из окружения не должен уходить клиенту")

    route = APIRoute("/__boom__", boom, methods=["GET"])
    app.router.routes.append(route)
    try:
        yield "/__boom__"
    finally:
        app.router.routes.remove(route)


def test_preflight_passes_for_the_twin_of_the_configured_origin(client):
    twin = _twin(FRONTEND_URL)
    response = client.options(
        "/api/login",
        headers={"Origin": twin,
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization,content-type"},
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == twin


def test_the_request_id_is_readable_for_a_cross_origin_client(client):
    """Без `expose_headers` идентификатор до браузера доезжает, но читателю
    недоступен: при cross-origin запросе виден только simple-набор заголовков.

    Цепочка разбора жалобы (`X-Request-Id` → строки запроса → `task_id` → строки
    воркера) начинается с id, который человек показывает с экрана. `Starlette`
    добавляет `Access-Control-Expose-Headers` только в обычный ответ (предполётный
    несёт разрешение методов), поэтому и проверяется ответ с `Origin`.
    """
    twin = _twin(FRONTEND_URL)
    response = client.get("/health", headers={"Origin": twin})
    exposed = (response.headers.get("access-control-expose-headers") or "")
    assert "x-request-id" in exposed.lower(), (exposed, dict(response.headers))
    assert response.headers.get("x-request-id"), dict(response.headers)


def test_retry_after_is_readable_for_a_cross_origin_client(client):
    """`Retry-After` доезжает до браузера только если он в `expose_headers`.

    Клиент считает по нему срок («повторите через 12 минут», а не «повторите
    позже») и на 429, и на 503, но в cross-origin ответе axios видит только
    simple-набор заголовков. Тесты клиента заголовок подставляют руками, поэтому
    без этой проверки вычеркнутая из списка строка осталась бы незамеченной до
    первого живого отказа по лимиту.
    """
    twin = _twin(FRONTEND_URL)
    response = client.get("/health", headers={"Origin": twin})
    exposed = (response.headers.get("access-control-expose-headers") or "")
    assert RETRY_AFTER_HEADER.lower() in exposed.lower(), (exposed, dict(response.headers))


@pytest.mark.parametrize(("origin", "twin"), [
    ("http://localhost:3456", "http://127.0.0.1:3456"),
    ("http://127.0.0.1:8765", "http://localhost:8765"),
    ("http://LOCALHOST:3456", "http://127.0.0.1:3456"),
    # Хост, который только начинается на «localhost», второго адреса получить не
    # должен: иначе в allowlist попадёт источник, которого никто не запрашивал.
    ("http://localhost.example.com", ""),
    ("http://127.0.0.1.evil.example", ""),
    ("https://registry.example.ru", ""),
    ("http://localhost:3456/paper", ""),
    ("localhost:3456", ""),
])
def test_only_a_real_loopback_host_gets_a_twin(origin, twin):
    assert _loopback_alias(origin) == twin


def test_login_answer_is_readable_from_the_twin_origin(client):
    twin = _twin(FRONTEND_URL)
    response = client.post("/api/login",
                           json={"email": "nobody@example.com", "password": "wrong"},
                           headers={"Origin": twin})
    assert response.headers.get("access-control-allow-origin") == twin
    # Отказ по паролю остаётся отказом по паролю: 401 видит экран, а не браузер.
    assert response.status_code == 401


def test_foreign_origin_still_gets_no_cors_headers(client):
    response = client.options(
        "/api/login",
        headers={"Origin": "https://registry.someone-else.example",
                 "Access-Control-Request-Method": "POST"},
    )
    assert response.headers.get("access-control-allow-origin") is None


def test_server_error_keeps_cors_headers_and_hides_the_reason(client, failing_route):
    response = client.get(failing_route, headers={"Origin": FRONTEND_URL})
    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == FRONTEND_URL
    # Иначе человек читает в консоли «blocked by CORS policy» и чинит не то.
    assert "секрет из окружения" not in response.text
    assert response.json()["detail"]

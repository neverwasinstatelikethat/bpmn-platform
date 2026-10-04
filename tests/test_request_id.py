"""Идентификатор запроса: заголовок ответа, контекст логов и зачистка входа.

Проверяется то, без чего слой бесполезен: что `rid` долетает до записи журнала
не только из асинхронного эндпоинта, но и из синхронного — тот выполняется в
потоке `run_in_threadpool`, и если anyio не переносит контекст, корреляция
распадается ровно на маршрутах с базой (а их большинство).
"""
import logging

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.main import app
from app.request_context import (REQUEST_ID_HEADER, RequestIdFilter,
                                 configure_logging, current_request_id,
                                 reset_request_id, sanitize_request_id,
                                 use_request_id)

probe = APIRouter()


@probe.get("/__rid_async__")
async def rid_async():
    logging.getLogger("test.rid").info("асинхронный эндпоинт")
    return {"rid": current_request_id()}


@probe.get("/__rid_sync__")
def rid_sync():
    # Синхронный обработчик FastAPI исполняет в потоке пула: здесь и решается,
    # переносится ли контекст запроса в этот поток.
    logging.getLogger("test.rid").info("синхронный эндпоинт")
    return {"rid": current_request_id()}


@pytest.fixture
def client():
    for route in probe.routes:
        app.router.routes.append(route)
    with TestClient(app) as test_client:
        yield test_client
    for route in probe.routes:
        app.router.routes.remove(route)


def _rid_headers(response):
    return response.headers[REQUEST_ID_HEADER]


def test_every_answer_carries_its_own_id(client):
    first = _rid_headers(client.get("/health"))
    second = _rid_headers(client.get("/health"))
    assert first and first != "-"
    assert first != second, "разные запросы получили один id — строки сливаются"


def test_id_from_the_client_is_respected(client):
    """Фронтенд вправе принести свой идентификатор: тогда его строки и строки
    сервера склеиваются без угадывания по времени."""
    response = client.get("/health", headers={REQUEST_ID_HEADER: "screen-42"})
    assert _rid_headers(response) == "screen-42"


def test_hostile_id_is_cleaned_before_it_reaches_the_log(client):
    """Значение заголовка не вправе приносить в журнал вторую строку.

    Перевод строки в идентификаторе — это подделка записи лога: одна запись
    превращается в две, а парсер журнала и алерт начинают читать то, чего не
    было. Заодно проверка на уровне модуля: кириллица и прочие не-ASCII из
    зачищаемого значения уходят (HTTP-клиент такой заголовок сам не отправит,
    но защищаемся мы не от клиента, а от форматера).
    """
    forged = "abc\r\nX-Forged: injected second log line"
    rid = _rid_headers(client.get("/health", headers={REQUEST_ID_HEADER: forged}))
    assert "\r" not in rid and "\n" not in rid and " " not in rid
    assert rid.startswith("abc")
    cleaned = sanitize_request_id("abc\r\nDEF: строка\x1b[31m и кириллица")
    assert not any(ord(char) > 127 for char in cleaned), cleaned
    assert "\x1b" not in cleaned


def test_garbage_id_falls_back_to_a_generated_one(client):
    rid = _rid_headers(client.get("/health", headers={REQUEST_ID_HEADER: "///"}))
    assert rid and len(rid) <= 64
    assert sanitize_request_id("") != ""
    assert len(sanitize_request_id("x" * 500)) == 64


@pytest.mark.parametrize("path", ["/__rid_async__", "/__rid_sync__"])
def test_request_id_reaches_the_log_record(client, caplog, path):
    """Фильтр добавляет `rid` к записи, а контекст доживает до обработчика.

    Для синхронного маршрута это и есть проверка переноса контекста в поток
    пула: без него `rid` остался бы «-», и корреляция работала бы только там,
    где она менее всего нужна.
    """
    caplog.set_level(logging.INFO)
    caplog.handler.addFilter(RequestIdFilter())
    response = client.get(path, headers={REQUEST_ID_HEADER: f"probe-{path}"})
    rid = _rid_headers(response)
    assert response.json()["rid"] == rid
    records = [r for r in caplog.records if r.name == "test.rid"]
    assert records, "эндпоинт не написал ни одной строки журнала"
    assert {record.rid for record in records} == {rid}


def test_configure_logging_is_idempotent_and_prints_the_id():
    """Повторный вызов не размножает строки: lifespan может сработать дважды
    (тесты, перезапуск без смены процесса), а удвоенный лог читается как
    удвоенный трафик."""
    root = logging.getLogger()
    handler = configure_logging()
    try:
        configure_logging()
        ours = [h for h in root.handlers
                if getattr(h, "_bpmn_request_id_handler", False)]
        assert len(ours) == 1
        token = use_request_id("format-check")
        try:
            record = logging.LogRecord("app.probe", logging.INFO, __file__, 1,
                                       "сообщение", None, None)
            for filt in handler.filters:
                filt.filter(record)
            assert "[format-check]" in handler.format(record)
        finally:
            reset_request_id(token)
    finally:
        root.removeHandler(handler)
        assert handler not in root.handlers


def test_replace_existing_is_for_the_worker_process():
    """Воркер arq может принести свой обработчик (`--log-level`); в его процессе
    логгер корня наш, и оставлять чужой формат значило бы печатать каждую строку
    дважды. В веб-процессе ключ выключен: там обработчики принадлежат uvicorn."""
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    root.addHandler(foreign)
    try:
        ours = configure_logging()
        try:
            assert foreign in root.handlers
        finally:
            root.removeHandler(ours)

        replaced = configure_logging(replace_existing=True)
        try:
            assert foreign not in root.handlers
            assert any(getattr(h, "_bpmn_request_id_handler", False)
                       for h in root.handlers)
        finally:
            root.removeHandler(replaced)
    finally:
        if foreign in root.handlers:
            root.removeHandler(foreign)


def test_worker_startup_configures_logging(monkeypatch):
    """Старт воркера обязан настроить логгер: иначе разбор очереди в
    `docker logs` процесса воркера не читается — там только WARNING без
    времени и уровня."""
    import asyncio

    import app.tasks as tasks_module

    calls = []
    monkeypatch.setattr(tasks_module, "configure_logging",
                        lambda **kwargs: calls.append(("logging", kwargs)))

    async def sweep(_ctx):
        calls.append(("sweep", None))

    # `init_ai_cache` в продукте синхронный: прежняя подмена `async def`
    # создавала корутину, которую никто не ждал (RuntimeWarning в прогоне), и
    # порядок вызовов при этом не проверялся.
    monkeypatch.setattr(tasks_module, "_recover_stale_tasks", sweep)
    monkeypatch.setattr(tasks_module.ai_cache, "init_ai_cache",
                        lambda: calls.append(("cache", None)))
    monkeypatch.setattr(tasks_module.ai_components, "init_orchestrator",
                        lambda: calls.append(("orchestrator", None)))
    asyncio.run(tasks_module._worker_startup({}))
    # Порядок — часть контракта: настройка логгера обязана предшествовать обходу,
    # иначе его INFO-строки и предупреждения уборки уходят в `lastResort`.
    assert [name for name, _ in calls] == ["logging", "sweep", "cache",
                                          "orchestrator"]
    assert calls[0][1] == {"replace_existing": True}

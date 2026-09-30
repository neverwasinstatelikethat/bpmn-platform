"""Контур задач ИИ-контура: маршрутизация, жизненный цикл, отмена, восстановление.

Отдельный файл, а не приложение к `test_routes_smoke.py`: здесь проверяется
механика очереди на уровне сервиса и воркера, где HTTP уже ни при чём. Именно
эти инварианты дороже всего и хуже всего видны сквозь контракт маршрутов —
гонка двух воркеров за одну задачу, повтор после сбоя провайдера, отмена
исполняемой задачи, потеря воркера посреди выполнения.

Тесты идут без Redis, то есть в режиме исполнения в процессе, кроме класса
`TestArqContract`: он подставляет фальшивый пул и сверяет, **куда** именно
уходит задача. Очередь и идентификатор job без брокера не проверяются вовсе, а
без проверки «правильный таск роут» остаётся словом.
"""
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import tasks as task_module
from app.db import SessionLocal
from app.main import app
from app.models import AiTask
from app.services import tasks as task_store


def cached_keys(client):
    """Ключи ответов в хранилище кэша: счётчики лежат рядом и в проверках
    «в кэш ничего не легло» участвовать не должны."""
    return {key for key in client.store if not key.endswith(":stats")}

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
def owner(client):
    """Хозяин задач: (заголовки, id пользователя).

    id настоящий, а не `1`: на PostgreSQL внешний ключ `ai_tasks.user_id`
    отвергает несуществующего пользователя, и тесты очереди начались бы с
    ошибки целостности вместо проверяемого поведения.
    """
    email = f"tasks-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/register", json={"name": "Задачи", "email": email,
                                              "password": "Passw0rd!"}).status_code == 200
    token = client.post("/api/login", json={"email": email,
                                            "password": "Passw0rd!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


@pytest.fixture(scope="module")
def outsider(client):
    """Второй аккаунт: (заголовки, id). Чужие задачи не читаются и не отменяются,
    а в историю чужого пользователя не попадают."""
    email = f"outsider-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/register", json={"name": "Чужой", "email": email,
                                       "password": "Passw0rd!"})
    token = client.post("/api/login", json={"email": email,
                                            "password": "Passw0rd!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def make_task(kind="generate", *, user_id, queue=None, payload=None,
              diagram_id=None, base_seq=None, idempotency_key=None,
              status=None) -> str:
    """Строка задачи в базе напрямую: часть сценариев из HTTP недостижима."""
    with SessionLocal() as session:
        task = task_store.create_task(session, user_id=user_id, kind=kind,
                                      queue=queue or task_module.queue_of(kind),
                                      payload=payload, diagram_id=diagram_id,
                                      base_seq=base_seq,
                                      idempotency_key=idempotency_key)
        if status is not None:
            session.query(AiTask).filter(AiTask.id == task.id).update(
                {AiTask.status: status})
            session.commit()
        return task.id


def read(task_id: str) -> AiTask:
    """Снимок строки после закрытия сеанса: атрибуты читаются на свету."""
    with SessionLocal() as session:
        task = session.get(AiTask, task_id)
        session.expunge(task)
        return task


def mark(task_id: str, **fields) -> None:
    with SessionLocal() as session:
        session.query(AiTask).filter(AiTask.id == task_id).update(fields)
        session.commit()


def run(task_id, work, **context):
    return asyncio.run(task_module._execute(dict(context), task_id, work))


def _parse(url: str) -> tuple:
    """(хост, порт, база) из Redis URL — чтобы сверять настройки воркера."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return (parsed.hostname, parsed.port or 6379,
            int((parsed.path or "/0").lstrip("/") or 0))


async def refuse(*_args, **_kwargs):
    raise task_module.TaskRetryable("провайдер недоступен")


class TestRoutingTable:
    """Маршрутизация — выбор очереди по виду нагрузки, и она обязана быть полной."""

    def test_every_kind_has_a_queue_and_a_function(self):
        for kind in (task_module.GENERATE, task_module.IMPROVE, task_module.SCORE):
            assert task_module.queue_of(kind) in (task_module.QUEUE_LLM,
                                                  task_module.QUEUE_LOCAL)
            function = getattr(task_module, task_module.FUNCTION_OF[kind])
            assert function in task_module.TASK_FUNCTIONS

    def test_llm_kinds_and_local_kinds_are_split(self):
        """Задача скоринга не имеет права встать в LLM-очередь: она заняла бы
        тарифный слот провайдера, ничего у провайдера не спрашивая."""
        assert task_module.queue_of(task_module.SCORE) == task_module.QUEUE_LOCAL
        for kind in (task_module.GENERATE, task_module.IMPROVE):
            assert task_module.queue_of(kind) == task_module.QUEUE_LLM

    def test_unknown_kind_is_refused_not_defaulted(self):
        """Неизвестный вид обязан упасть: дефолт на `ai_llm` тихо поставил бы
        локальную работу в очередь провайдера."""
        with pytest.raises(ValueError):
            task_module.queue_of("teleport")

    def test_workers_consume_distinct_queues_of_known_functions(self):
        """arq потребляет ровно одну очередь воркером, поэтому очередей два
        процесса: если настройки сойдутся на одной, задачи второй очереди
        никогда не исполнятся."""
        settings = (task_module.LLMWorkerSettings, task_module.LocalWorkerSettings)
        assert {s.queue_name for s in settings} == {task_module.QUEUE_LLM,
                                                   task_module.QUEUE_LOCAL}
        for one in settings:
            assert list(one.functions) == task_module.TASK_FUNCTIONS
            assert one.job_timeout > 0 and one.max_jobs >= 1
            assert one.allow_abort_jobs is False

    def test_worker_liveness_probe_matches_the_settings(self):
        """Healthcheck контейнера читает тот же ключ, который воркер пишет, и
        интервал отметки короче, чем срок жизни ключа.

        Проверка сквозная не для красоты: унаследованный из образа пробник
        стучится в HTTP, которого у arq нет, поэтому воркер висит unhealthy при
        живой очереди. Если ключ в compose разойдётся с настройками, пробник
        станет враньём того же рода."""
        import yaml

        compose = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "docker-compose.yml")
            .read_text(encoding="utf-8"))["services"]
        for service, settings in (("worker_llm", task_module.LLMWorkerSettings),
                                  ("worker_local", task_module.LocalWorkerSettings)):
            test = compose[service]["healthcheck"]["test"]
            probe = " ".join(test) if isinstance(test, list) else test
            assert settings.health_check_key in probe, (
                f"{service}: проба смотрит не в тот ключ, что пишет воркер")
            assert 0 < settings.health_check_interval <= 60, (
                f"{service}: при длинной отметке мёртвый воркер выглядит живым")

    def test_arq_cli_accepts_both_settings_classes(self):
        """arq читает атрибуты настроек по именам, которые ожидает `Worker`.
        Опечатка (`max_concurrency` вместо `max_jobs`) не ломает импорт и не
        видна в проверках выше: воркер молча стартует с дефолтами, и лимит
        параллелизма провайдера перестаёт действовать."""
        from arq.cli import create_worker

        for settings in (task_module.LLMWorkerSettings,
                        task_module.LocalWorkerSettings):
            worker = create_worker(settings)
            assert worker.queue_name in (task_module.QUEUE_LLM,
                                        task_module.QUEUE_LOCAL)
            assert sorted(worker.functions) == sorted(
                f.__name__ for f in task_module.TASK_FUNCTIONS)
            assert worker.max_tries >= 1 and worker.job_timeout_s > 0
            assert worker.allow_abort_jobs is False

    def test_worker_settings_point_at_the_configured_redis(self):
        """Без `redis_settings` arq смотрит на localhost:6379 по умолчанию:
        воркер выглядел бы живым и не брал бы задачи никогда — а в стенде с
        другим хостом или другой базой это ровно тот случай, когда очередь
        растёт, а исполнителей «достаточно»."""
        for settings in (task_module.LLMWorkerSettings,
                        task_module.LocalWorkerSettings):
            built = settings.redis_settings
            assert built is not None
            if task_module.REDIS_URL:
                expected = _parse(task_module.REDIS_URL)
                assert (built.host, built.port, built.database) == expected
            else:
                assert built.host == "localhost" and built.database == 0

    def test_llm_timeout_outlives_the_generation_budget(self):
        """Таймаут меньше бюджета генерации убивал бы каждую честную генерацию
        на половине, и пользователь получал бы «сбой» вместо схемы."""
        from app.config import AI_LLM_JOB_TIMEOUT
        from core.bpmn_generator import GENERATION_BUDGET_SECONDS

        assert AI_LLM_JOB_TIMEOUT > GENERATION_BUDGET_SECONDS

    def test_retry_backoff_grows(self):
        assert [task_module._backoff_seconds(n) for n in (1, 2, 3)] == [5, 25, 25]


class TestClaimAndStatuses:
    def test_second_claim_loses_the_race(self, owner):
        """Два воркера на одной задаче — две оплаты одного вызова модели."""
        task_id = make_task(user_id=owner[1])
        tries, queue = task_store.claim_task(task_id)
        assert (tries, queue) == (1, task_module.QUEUE_LLM)
        assert task_store.claim_task(task_id) is None

    def test_claim_counts_attempts_per_execution(self, owner):
        task_id = make_task(user_id=owner[1])
        assert task_store.claim_task(task_id)[0] == 1
        assert task_store.requeue(task_id) is True
        assert task_store.claim_task(task_id)[0] == 2

    def test_cancelled_task_cannot_be_claimed(self, owner):
        task_id = make_task(user_id=owner[1])
        task_store.mark_cancelled(task_id)
        assert task_store.claim_task(task_id) is None

    def test_claim_returns_the_queue_of_the_task(self, owner):
        """Лимит повторов различается по очереди, а счётчик arq считается по
        идентификатору job и после восстановления воркера врал бы."""
        task_id = make_task(task_module.SCORE, user_id=owner[1])
        assert task_store.claim_task(task_id)[1] == task_module.QUEUE_LOCAL

    def test_terminal_write_clears_the_payload(self, owner):
        """Входы задачи — полный XML схемы. После закрытия они не нужны ни
        клиенту, ни воркеру, а без чистки таблица росла бы на десятки
        мегабайт в день."""
        task_id = make_task(user_id=owner[1], payload={"text": "заявка",
                                                      "bpmn_xml": XML})
        task_store.claim_task(task_id)
        task_store.mark_done(task_id, result={"status": "success"})
        assert read(task_id).payload is None

    def test_requeued_task_keeps_its_inputs(self, owner):
        """Повтор обязан иметь чем исполниться: чистка на возврате в очередь
        оставила бы задачу без входа навсегда."""
        task_id = make_task(user_id=owner[1], payload={"text": "заявка"})
        task_store.claim_task(task_id)
        task_store.requeue(task_id)
        assert read(task_id).payload is not None

    def test_cancel_requested_is_visible_to_the_worker(self, owner):
        task_id = make_task(user_id=owner[1])
        task_store.claim_task(task_id)
        assert task_store.cancel_requested(task_id) is False
        mark(task_id, cancel_requested=True)
        assert task_store.cancel_requested(task_id) is True


class TestExecute:
    def test_cancellation_lands_in_the_database(self, owner):
        task_id = make_task(user_id=owner[1])

        async def work(_task_id):
            raise task_module.TaskCancelled(task_id)

        assert run(task_id, work)["status"] == task_store.CANCELLED
        assert read(task_id).status == task_store.CANCELLED

    def test_refusal_records_its_code(self, owner):
        """Код ошибки — то, по чему фронтенд отличает «повторите позже» от
        «сократите описание». Потерять его значит собрать два разных отказа в
        один невнятный."""
        task_id = make_task(user_id=owner[1])

        async def work(_task_id):
            raise task_module.TaskRefused("Схема слишком велика", code="llm_too_large")

        run(task_id, work)
        task = read(task_id)
        assert (task.status, task.error_code) == (task_store.FAILED, "llm_too_large")
        assert "Схема слишком велика" in task.error

    def test_internal_error_text_is_not_leaked(self, owner):
        """Сырое исключение не уходит в базу как сообщение пользователю: в нём
        бывают пути и фрагменты чужих данных."""
        task_id = make_task(user_id=owner[1])

        async def work(_task_id):
            raise ValueError("соединение с /var/lib/postgresql потеряно")

        run(task_id, work)
        task = read(task_id)
        assert "postgresql" not in (task.error or "")
        assert task.error_code == "internal"

    def test_done_writes_result_and_reference(self, owner):
        task_id = make_task(user_id=owner[1])

        async def work(_task_id):
            return {"status": "success", "bpmn": XML}, "improvement-42"

        run(task_id, work)
        task = read(task_id)
        assert task.status == task_store.DONE
        assert task.result_ref == "improvement-42"
        assert '"success"' in task.result

    def test_retryable_failure_is_postponed_while_tries_remain(self, owner):
        """Пока попытки не исчерпаны, задача возвращается в очередь, а arq
        откладывает её: в статусе остаётся `queued`, а не `failed`."""
        task_id = make_task(task_module.GENERATE, user_id=owner[1])
        with pytest.raises(task_module.Retry) as raised:
            run(task_id, refuse, max_tries=3)
        assert raised.value.defer_score == 5000
        task = read(task_id)
        assert (task.status, task.tries) == (task_store.QUEUED, 1)

    def test_last_try_closes_the_task(self, owner):
        """Последняя попытка обязана закрыть задачу: иначе клиент опрашивал бы
        её вечно."""
        task_id = make_task(task_module.GENERATE, user_id=owner[1])
        # Две отсрочки при max_tries=3, на третьей попытки не остаётся.
        for _postponed in range(2):
            with pytest.raises(task_module.Retry):
                run(task_id, refuse, max_tries=3)
        assert run(task_id, refuse, max_tries=3)["status"] == task_store.FAILED
        assert read(task_id).error_code == "unavailable"

    def test_inline_mode_never_retries(self, owner):
        """В режиме исполнения в процессе `max_tries=1`: повторять некому, и
        держать соединение ради отложенного повтора значило бы вернуть тот же
        отказ только медленнее."""
        task_id = make_task(task_module.GENERATE, user_id=owner[1])
        outcome = run(task_id, refuse, **dict(task_module.INLINE_CONTEXT))
        assert outcome["status"] == task_store.FAILED

    def test_cancel_flag_stops_before_the_work_starts(self, owner):
        """Гонка «отменили, пока задача уже снята с очереди»: флаг стоит,
        воркер ещё не начал — работа не должна начаться вовсе. Отмена
        проверяется до обращения к модели, потому что прерванный вызов уже
        оплачен, а ответа на него нет."""
        task_id = make_task(user_id=owner[1])
        mark(task_id, cancel_requested=True)
        called = []

        async def work(_task_id):
            called.append(1)
            return {}, None

        run(task_id, work)
        assert called == []
        assert read(task_id).status == task_store.CANCELLED

    def test_already_claimed_task_is_skipped_without_touching_the_model(self, owner):
        task_id = make_task(user_id=owner[1])
        task_store.claim_task(task_id)
        called = []

        async def work(_task_id):
            called.append(1)
            return {}, None

        assert run(task_id, work)["status"] == "skipped"
        assert called == []


class TestArqContract:
    """Куда именно уходит задача — то, что без брокера не проверить вовсе.

    Фальшивый пул подменяет только транспорт arq: маршрутизация, идентификатор
    job и разбор его ответа остаются продуктовым кодом.
    """

    class FakePool:
        def __init__(self, accept=True):
            self.calls = []
            self.accept = accept

        async def enqueue_job(self, function, *args, **kwargs):
            self.calls.append((function, args, kwargs))
            return object() if self.accept else None

        async def zcard(self, key):
            return 7

        async def close(self):
            return None

    @pytest.mark.parametrize("kind,queue,function", [
        (task_module.GENERATE, task_module.QUEUE_LLM, "generate_bpmn_task"),
        (task_module.IMPROVE, task_module.QUEUE_LLM, "improve_diagram_task"),
        (task_module.SCORE, task_module.QUEUE_LOCAL, "score_diagram_task"),
    ])
    def test_task_lands_in_the_queue_of_its_kind(self, kind, queue, function):
        pool = self.FakePool()
        asyncio.run(task_module.ArqBroker(pool).submit(kind, "task-1"))
        (got_function, args, kwargs), = pool.calls
        assert got_function == function
        # Только идентификатор: содержимое схемы в очередь не едет.
        assert args == ("task-1",)
        assert kwargs == {"_job_id": "task-1", "_queue_name": queue}

    def test_duplicate_job_is_deduplicated_not_raised(self):
        """arq отвечает None, когда такой job уже стоит. Это дедупликация, а не
        сбой: вторую постановку той же задачи маршрут не должен ронять."""
        pool = self.FakePool(accept=False)
        asyncio.run(task_module.ArqBroker(pool).submit(task_module.GENERATE, "task-1"))
        assert len(pool.calls) == 1

    def test_depth_reports_both_queues(self):
        depths = asyncio.run(task_module.ArqBroker(self.FakePool()).depth())
        assert set(depths) == {task_module.QUEUE_LLM, task_module.QUEUE_LOCAL}
        assert all(value == 7 for value in depths.values())

    def test_inline_broker_has_no_depth(self):
        """Без очереди глубина неизвестна, а не равна нулю: ноль в метрике
        означал бы «воркер справляется», чего режим не обещает."""
        depths = asyncio.run(task_module.InlineBroker().depth())
        assert all(value is None for value in depths.values())

    def test_broker_selection_follows_the_configuration(self, monkeypatch):
        """Без `REDIS_URL` — исполнение в процессе; с ним arq, и веб-процесс
        перестает держать тяжёлые модели."""
        monkeypatch.setattr(task_module, "_broker", None)
        monkeypatch.setattr(task_module, "REDIS_URL", "")
        assert isinstance(asyncio.run(task_module.get_broker()),
                          task_module.InlineBroker)
        assert task_module.broker_name() == "inline"

    def test_unreachable_queue_is_not_silently_inlined(self, monkeypatch):
        """При заданном `REDIS_URL` недоступный Redis обязан дать None (маршрут
        ответит 503), а не молча переключиться на исполнение в процессе: там
        оркестратор не поднят, и пользователь получил бы падение на первом же
        запросе вместо честного «сервис недоступен»."""
        async def refuse(*_args, **_kwargs):
            raise ConnectionError("redis не поднят")

        monkeypatch.setattr(task_module, "_broker", None)
        monkeypatch.setattr(task_module, "REDIS_URL", "redis://127.0.0.1:1/0")
        monkeypatch.setattr("arq.create_pool", refuse)
        assert asyncio.run(task_module.get_broker()) is None
        assert task_module.broker_name() == "unavailable"


class TestCacheInContour:
    """Кэш обязан быть вшит в контур, а не только существовать рядом.

    Это единственная проверка того, что этап кэширования вообще работает:
    повтор того же запроса не имеет права второй раз оплатить вызов модели.
    """

    @pytest.fixture
    def cache_installed(self, monkeypatch, fake_cache):
        from app.services import ai_cache as ai_cache_service

        store, client = fake_cache
        monkeypatch.setattr(ai_cache_service, "_cache",
                            ai_cache_service.AiCache(store))
        return SimpleNamespace(client=client, ai=ai_cache_service.get_ai_cache())

    def test_repeated_generation_calls_the_model_once(self, client, owner,
                                                     cache_installed, monkeypatch):
        from app import ai as ai_module

        calls = []

        def fake_generate(text):
            calls.append(text)
            return {"status": "success", "bpmn": XML, "structure": {}, "notes": []}

        monkeypatch.setattr(ai_module.generator, "generate", fake_generate)
        first = client.post("/api/generate", json={"text": "заявка на счёт"},
                            headers=owner[0])
        second = client.post("/api/generate", json={"text": "заявка на счёт"},
                             headers=owner[0])
        assert first.status_code == second.status_code == 202
        assert second.json()["result"]["bpmn"] == first.json()["result"]["bpmn"]
        assert calls == ["заявка на счёт"]
        assert cache_installed.ai.store.stats.hits == 1

    def test_different_text_is_not_a_hit(self, client, owner, cache_installed,
                                        monkeypatch):
        """Ключ по тексту, а не по пользователю: иначе второй запрос того же
        человека отдал бы чужую схему."""
        from app import ai as ai_module

        calls = []
        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text: calls.append(text) or {
                                "status": "success", "bpmn": XML, "structure": {},
                                "notes": []})
        client.post("/api/generate", json={"text": "приёмка товара"}, headers=owner[0])
        client.post("/api/generate", json={"text": "возврат товара"}, headers=owner[0])
        assert len(calls) == 2

    def test_provider_outage_is_not_remembered(self, client, owner, cache_installed,
                                               monkeypatch):
        """«Сервис недоступен» — состояние провайдера. Запомнить его значило бы
        держать пользователя в чужой ошибке сутки, а не до следующей попытки."""
        from app import ai as ai_module

        calls = []
        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text: calls.append(text) or {
                                "status": "error", "step": "llm",
                                "error": "недоступен"})
        for _ in range(2):
            client.post("/api/generate", json={"text": "заявка"}, headers=owner[0])
        assert len(calls) == 2
        assert cached_keys(cache_installed.client) == set()

    def test_truncation_is_remembered_and_replayed_as_failure(self, client, owner,
                                                             cache_installed,
                                                             monkeypatch):
        from app import ai as ai_module

        calls = []
        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text: calls.append(text) or {
                                "status": "error", "step": "llm_truncated",
                                "error": "Ответ обрезан по лимиту токенов"})
        first = client.post("/api/generate", json={"text": "длинное описание"},
                            headers=owner[0]).json()
        second = client.post("/api/generate", json={"text": "длинное описание"},
                             headers=owner[0]).json()
        assert len(calls) == 1
        assert second["status"] == "failed"
        assert second["error"] == first["error"]
        assert second["error_code"] == "cached_refusal"

    def test_repeated_scoring_does_not_reparse_the_scheme(self, client, owner,
                                                          cache_installed, monkeypatch):
        """Скоринг детерминирован, поэтому кэшируется напрямую: панель
        «проверить» вызывается на каждой правке."""
        from app import ai as ai_module

        calls = []
        monkeypatch.setattr(ai_module.scorer, "evaluate",
                            lambda xml: calls.append(xml) or {"score": 77,
                                                              "details": {},
                                                              "details_meta": {}})
        first = client.post("/api/evaluate", json={"bpmn_xml": XML},
                            headers=owner[0]).json()
        second = client.post("/api/evaluate", json={"bpmn_xml": XML},
                             headers=owner[0]).json()
        assert len(calls) == 1
        assert first["result"]["score"] == second["result"]["score"] == 77

    def test_cached_improvement_stores_a_fresh_proposal(self, client, owner,
                                                       cache_installed, monkeypatch):
        """Из кэша нельзя отдать чужой `improvement_id`: строка предложения
        создаётся заново на каждую задачу, иначе пользователь принял бы уже
        закрытое или чужое изменение."""
        from app import ai as ai_module
        from app.models import Improvement

        calls = []

        class FakeOrchestrator:
            async def improve_diagram(self, xml_content, user_prompt,
                                      cancel_check=None):
                calls.append(user_prompt)
                return ("Узкое место", "<definitions/>", {"status": "ok",
                                                          "applied": [],
                                                          "skipped": []})

        monkeypatch.setattr(ai_module, "_orchestrator", FakeOrchestrator())
        body = {"bpmn_xml": XML, "prompt": "сделай лучше"}
        first = client.post("/api/ai/improve", json=body, headers=owner[0]).json()
        second = client.post("/api/ai/improve", json=body, headers=owner[0]).json()
        assert len(calls) == 1, "второй запрос обязан был взять ответ модели из кэша"
        assert first["result"]["improvement_id"] != second["result"]["improvement_id"]

        with SessionLocal() as session:
            ids = [first["result"]["improvement_id"], second["result"]["improvement_id"]]
            rows = session.query(Improvement).filter(Improvement.id.in_(ids)).all()
            assert len(rows) == 2
            assert all(row.xml_content == "<definitions/>" for row in rows)


class TestRecovery:
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def enqueue_job(self, function, task_id, **kwargs):
            self.calls.append((function, task_id, kwargs))

    def test_stale_running_tasks_are_listed(self, owner):
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        assert task_id in [row[0] for row in task_store.stale_tasks(0)]

    def test_fresh_running_task_is_not_stale(self, owner):
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        mark(task_id, started_at=task_store.utc_now())
        assert task_id not in [row[0] for row in task_store.stale_tasks(3600)]

    def test_recovery_requeues_a_task_with_tries_left(self, owner, monkeypatch):
        """Задача `running` без живого воркера сама в очередь не вернётся: arq
        хранит свою копию, и после падения процесса её может не остаться."""
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.RUNNING)
        redis = self.FakeRedis()
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _seconds: [(task_id, task_module.QUEUE_LLM, 1)])
        asyncio.run(task_module._recover_stale_tasks({"redis": redis}))
        function, got_id, kwargs = redis.calls[0]
        assert (function, got_id) == ("generate_bpmn_task", task_id)
        assert kwargs["_queue_name"] == task_module.QUEUE_LLM
        # Повтор ставится новым идентификатором: arq не принимает повторно уже
        # известный, и задача не появилась бы в очереди вовсе.
        assert kwargs["_job_id"].startswith(task_id) and kwargs["_job_id"] != task_id
        assert read(task_id).status == task_store.QUEUED

    def test_recovery_closes_a_task_that_used_its_tries(self, owner, monkeypatch):
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.RUNNING)
        exhausted = task_module.QUEUE_TRIES[task_module.QUEUE_LLM]
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _seconds: [(task_id, task_module.QUEUE_LLM,
                                               exhausted)])
        asyncio.run(task_module._recover_stale_tasks({"redis": None}))
        task = read(task_id)
        assert (task.status, task.error_code) == (task_store.FAILED, "worker_lost")


class TestTaskRoutes:
    def test_stats_reports_the_running_mode(self, client, owner):
        response = client.get("/api/tasks/stats", headers=owner[0])
        assert response.status_code == 200
        body = response.json()
        assert body["broker"] in ("inline", "arq")
        assert body["cache"]["enabled"] is False
        assert task_store.QUEUED in body["tasks_last_24h"]

    def test_history_lists_only_own_tasks(self, client, owner, outsider):
        mine = make_task(user_id=owner[1])
        foreign = make_task(user_id=outsider[1])
        ids = [row["task_id"] for row in
               client.get("/api/tasks", headers=owner[0]).json()["tasks"]]
        assert mine in ids and foreign not in ids

    def test_foreign_task_is_not_readable_or_cancellable(self, client, owner,
                                                         outsider):
        task_id = make_task(user_id=owner[1])
        assert client.get(f"/api/tasks/{task_id}",
                          headers=outsider[0]).status_code == 404
        assert client.post(f"/api/tasks/{task_id}/cancel",
                           headers=outsider[0]).status_code == 404

    def test_queued_task_cancels_immediately(self, client, owner):
        task_id = make_task(user_id=owner[1])
        response = client.post(f"/api/tasks/{task_id}/cancel", headers=owner[0])
        assert response.status_code == 200
        assert response.json()["status"] == task_store.CANCELLED

    def test_cancel_is_idempotent(self, client, owner):
        task_id = make_task(user_id=owner[1])
        client.post(f"/api/tasks/{task_id}/cancel", headers=owner[0])
        again = client.post(f"/api/tasks/{task_id}/cancel", headers=owner[0])
        assert again.status_code == 200
        assert again.json()["status"] == task_store.CANCELLED

    def test_running_task_cancel_only_sets_the_flag(self, client, owner):
        """Исполняемую задачу нельзя снять: вызов модели уже оплачен. Отмена
        поднимает флаг, а статус остаётся `running` — иначе клиент решил бы,
        что работа прекратилась мгновенно."""
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        body = client.post(f"/api/tasks/{task_id}/cancel",
                           headers=owner[0]).json()
        assert body["status"] == task_store.RUNNING
        assert body["cancel_requested"] is True

    def test_finished_task_cannot_be_cancelled(self, client, owner):
        task_id = make_task(user_id=owner[1], status=task_store.DONE)
        assert client.post(f"/api/tasks/{task_id}/cancel",
                           headers=owner[0]).status_code == 409

    def test_unknown_task_is_404_not_500(self, client, owner):
        assert client.get("/api/tasks/net-net", headers=owner[0]).status_code == 404

    def test_descriptor_separates_waiting_from_running(self, client, owner):
        """«Стояла в очереди» и «считалась» — разные проблемы: первая про
        мощность воркера, вторая про провайдера."""
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        mark(task_id, started_at=task_store.utc_now())
        body = client.get(f"/api/tasks/{task_id}", headers=owner[0]).json()
        assert body["queued_seconds"] is not None
        assert body["run_seconds"] is None
        assert body["poll_after"] is not None

    def test_terminal_task_stops_suggesting_polling(self, client, owner):
        task_id = make_task(user_id=owner[1], status=task_store.DONE)
        body = client.get(f"/api/tasks/{task_id}", headers=owner[0]).json()
        assert body["poll_after"] is None

    def test_idempotency_key_replays_the_same_task(self, client, owner, monkeypatch):
        """Повтор отправки — та же задача и без второго списания бюджета."""
        from app import ai as ai_module

        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text: {"status": "success", "bpmn": XML,
                                          "structure": {}, "notes": []})
        key = f"idem-{uuid.uuid4().hex}"
        body = {"text": "заявка", "idempotency_key": key}
        first = client.post("/api/generate", json=body, headers=owner[0])
        second = client.post("/api/generate", json=body, headers=owner[0])
        assert first.status_code == second.status_code == 202
        assert second.json()["task_id"] == first.json()["task_id"]
        assert (first.json()["replayed"], second.json()["replayed"]) == (False, True)

    def test_blank_idempotency_key_is_not_a_shared_key(self, client, owner,
                                                       monkeypatch):
        """Пустая строка как ключ была бы общим ключом всех запросов
        пользователя: второй вернул бы результат первого."""
        from app import ai as ai_module

        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text: {"status": "success", "bpmn": XML,
                                          "structure": {}, "notes": []})
        first = client.post("/api/generate",
                            json={"text": "раз", "idempotency_key": "   "},
                            headers=owner[0]).json()
        second = client.post("/api/generate",
                             json={"text": "два", "idempotency_key": "   "},
                             headers=owner[0]).json()
        assert first["task_id"] != second["task_id"]

    def test_oversized_scheme_is_rejected_before_the_queue(self, client, owner):
        """Вход сверх лимита — 422 на постановке, а не задача, которая родится
        в очереди и умрёт в воркере: о длине схемы пользователь должен узнать
        сразу, а не через минуту опроса."""
        from app.config import AI_MAX_XML_CHARS

        response = client.post("/api/evaluate",
                               headers=owner[0],
                               json={"bpmn_xml": "<" * (AI_MAX_XML_CHARS + 1)})
        assert response.status_code == 422

    def test_payload_never_travels_through_the_broker(self, client, owner,
                                                     monkeypatch):
        """В очередь уходит один идентификатор задачи: содержимое схем не
        должно попадать в Redis (план, раздел 8)."""
        submitted = []

        class RecordingBroker:
            name = "arq"

            async def submit(self, kind, task_id):
                submitted.append((kind, task_id))

            async def depth(self):
                return {}

        monkeypatch.setattr(task_module, "_broker", RecordingBroker())
        response = client.post("/api/evaluate", json={"bpmn_xml": XML},
                              headers=owner[0])
        assert response.status_code == 202
        assert len(submitted) == 1
        _kind, task_id = submitted[0]
        assert task_id == response.json()["task_id"]
        assert len(task_id) == 36

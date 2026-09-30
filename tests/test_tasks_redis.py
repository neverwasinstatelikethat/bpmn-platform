"""Интеграция с настоящим Redis: кэш, общий бюджет и воркер arq.

Файл пропускается, если `TEST_REDIS_URL` не задан, — ровно как прогон на
PostgreSQL включается переменной `TEST_DATABASE_URL`. Отдельный шаг CI с
сервисом `redis:7-alpine` его всё-таки запускает, потому что три вещи ниже
подменой не проверяются в принципе: Lua-скрипт бюджета исполняет сервер,
ключи очереди формирует arq, а срок жизни значения считает Redis. Без живого
сервера эти тесты подтверждали бы только то, что мы сами себе наврали об API.
"""
import asyncio
import os
import time
import uuid

import pytest
import redis
from fastapi import HTTPException

from app import tasks as task_module
from app.db import SessionLocal
from app.deps import _RedisWindow
from app.models import AiTask, User
from app.services import tasks as task_store
from core.llm_cache import LLMCache


def _url(database: int = 0) -> str:
    configured = os.getenv("TEST_REDIS_URL")
    if not configured:
        pytest.skip("TEST_REDIS_URL не задан: интеграция с Redis не требуется "
                    "обычному прогону")
    return configured.rsplit("/", 1)[0] + f"/{database}"


def _reachable(url: str) -> bool:
    try:
        return bool(redis.Redis.from_url(url, socket_connect_timeout=1).ping())
    except Exception:  # noqa: BLE001 — недоступный сервер для теста значит пропуск
        return False


@pytest.fixture
def queue_url():
    url = _url(0)
    if not _reachable(url):
        pytest.skip(f"Redis по {url} не отвечает")
    yield url
    _flush(url)


def _flush(url: str) -> None:
    client = redis.Redis.from_url(url)
    client.flushdb()
    client.close()


@pytest.fixture
def cache(queue_url):
    """Кэш во второй базе — так же, как в бою, где очередь и кэш разведены."""
    store = LLMCache(_url(1), namespace="bpmn-test")
    assert store.ping(), "Redis для кэша должен отвечать"
    yield store
    _flush(_url(1))
    store.close()


def bpmn() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">'
            '<process id="P" name="P" isExecutable="true">'
            '<startEvent id="S" name="Начало"/>'
            '<sequenceFlow id="F1" sourceRef="S" targetRef="T1"/>'
            '<userTask id="T1" name="Собрать заказ"/>'
            '<sequenceFlow id="F2" sourceRef="T1" targetRef="E"/>'
            '<endEvent id="E" name="Конец"/></process></definitions>')


def any_user_id() -> int:
    with SessionLocal() as session:
        user = session.query(User).first()
        if user is None:
            user = User(email=f"redis-{uuid.uuid4().hex[:8]}@example.com",
                        name="Redis", hashed_password="x")
            session.add(user)
            session.commit()
        return user.id


def make_task(kind: str, *, payload=None, diagram_id=None) -> str:
    with SessionLocal() as session:
        return task_store.create_task(session, user_id=any_user_id(), kind=kind,
                                      queue=task_module.queue_of(kind),
                                      payload=payload,
                                      diagram_id=diagram_id).id


def read_task(task_id: str):
    with SessionLocal() as session:
        task = session.get(AiTask, task_id)
        session.expunge(task)
        return task


def run_worker(url: str, queue: str) -> None:
    """Один burst-прогон настоящего воркера arq против живой очереди.

    Пул создаётся снаружи и закрывается напрямую, а не через `Worker.close()`:
    тот вызывает `signal.SIGUSR1`, которого в Windows нет. Воркер и в проде
    живёт только в Linux-контейнере, поэтому тест проверяет исполнение, а не
    платформенно-зависимый teardown.
    """
    from arq import Worker
    from arq.connections import RedisSettings

    async def scenario():
        pool = await _pool(url)
        worker = Worker(functions=task_module.TASK_FUNCTIONS, queue_name=queue,
                        redis_pool=pool, burst=True, max_jobs=1, job_timeout=30,
                        handle_signals=False, poll_delay=0.05)
        try:
            await worker.async_run()
        finally:
            await pool.aclose()

    asyncio.run(scenario())


async def _pool(url: str):
    from arq import create_pool
    from arq.connections import RedisSettings

    return await create_pool(RedisSettings.from_dsn(url))


def submit(url: str, kind: str, task_id: str) -> None:
    async def scenario():
        pool = await _pool(url)
        try:
            await task_module.ArqBroker(pool).submit(kind, task_id)
        finally:
            await pool.aclose()

    asyncio.run(scenario())


class TestCacheAgainstRealServer:
    def test_round_trip_and_expiry(self, cache):
        key = cache.key("llm:generate", "hash")
        cache.set(key, {"bpmn": "<xml/>"}, prompt_version="v1", ttl=1)
        assert cache.get(key, prompt_version="v1").result == {"bpmn": "<xml/>"}
        time.sleep(1.2)
        assert cache.get(key, prompt_version="v1") is None

    def test_counters_are_visible_to_another_process(self, cache):
        """Счётчики читаются вторым клиентом: попадания считает воркер, а в них
        смотрит веб-процесс. Живой прогон в Docker это и поймал — в
        `/api/tasks/stats` нули при трёх записанных ключах кэша."""
        key = cache.key("llm:generate", "hash")
        cache.set(key, {"bpmn": "<xml/>"}, prompt_version="v1", ttl=60)
        cache.get(key, prompt_version="v1")
        cache.get(cache.key("llm:generate", "нет такого"), prompt_version="v1")

        # Второй клиент на то же пространство имён — так веб-процесс читает то,
        # что насчитал воркер.
        reader = LLMCache(_url(1), namespace="bpmn-test")
        try:
            stats = reader.snapshot()
            assert (stats.writes, stats.hits, stats.misses) == (1, 1, 1)
            assert reader.stats.hits == 0, "цифры взяты из Redis, а не из памяти"
        finally:
            reader.close()

    def test_separate_databases_do_not_see_each_other(self, cache, queue_url):
        """Разнесение баз должно быть настоящим: сброс кэша не имеет права
        трогать очередь задач, и наоборот."""
        client = redis.Redis.from_url(queue_url)
        client.set("sentinel:queue", "1")
        try:
            assert cache.get("sentinel:queue", prompt_version="v1") is None
        finally:
            client.delete("sentinel:queue")
            client.close()


class TestSharedBudgetAgainstRealServer:
    def test_limit_is_enforced_and_shared_between_replicas(self, queue_url):
        """Смысл общего бюджета именно в этом: вторая реплика продолжает счёт
        первой, а не начинает с нуля. Две «реплики» здесь — два независимых
        окна на одном сервере."""
        key = f"user-{uuid.uuid4().hex[:8]}"

        async def scenario():
            replicas = [_RedisWindow(queue_url, limit=2) for _ in range(2)]
            await replicas[0].acquire(key)
            await replicas[1].acquire(key)
            with pytest.raises(HTTPException) as raised:
                await replicas[0].acquire(key)
            return raised.value

        error = asyncio.run(scenario())
        assert error.status_code == 429
        assert int(error.headers["Retry-After"]) > 0

    def test_window_slides_instead_of_resetting_at_midnight(self, queue_url):
        """Старый слот скрипт обязан выкинуть: иначе лимит выбрал бы чужой час
        вместо текущего окна, и пользователь ждал бы до полуночи."""
        key = f"slide-{uuid.uuid4().hex[:8]}"
        client = redis.Redis.from_url(queue_url, decode_responses=True)
        try:
            now = time.time()
            client.zadd(f"ai:budget:{key}", {f"old:{now}": now - 3700})
            asyncio.run(_RedisWindow(queue_url, limit=1).acquire(key))
            assert client.zcard(f"ai:budget:{key}") == 1
        finally:
            client.delete(f"ai:budget:{key}")
            client.close()

    def test_two_requests_in_the_same_millisecond_take_two_slots(self, queue_url):
        """Участник множества уникален: при метке времени как unique-значении
        два одновременных запроса заняли бы один слот и лимит стал бы мягче
        ровно под нагрузкой."""
        key = f"burst-{uuid.uuid4().hex[:8]}"
        window = _RedisWindow(queue_url, limit=2)

        async def scenario():
            await asyncio.gather(*(window.acquire(key) for _ in range(2)))
            with pytest.raises(HTTPException):
                await window.acquire(key)
            return redis.Redis.from_url(queue_url).zcard(f"ai:budget:{key}")

        assert asyncio.run(scenario()) == 2


class TestArqWorkerAgainstRealServer:
    def test_task_travels_through_the_queue_and_completes(self, queue_url):
        """Сквозной прогон настоящего arq: постановка → очередь → воркер →
        запись в базу. Ни одна подмена пула этого не доказывает."""
        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        submit(queue_url, task_module.SCORE, task_id)
        run_worker(queue_url, task_module.QUEUE_LOCAL)
        task = read_task(task_id)
        assert (task.status, task.tries) == (task_store.DONE, 1)
        assert task.queue == task_module.QUEUE_LOCAL
        assert task.result and '"score"' in task.result
        # Входы закрытой задачи не хранятся: иначе таблица росла бы на полный
        # XML схемы на каждую проверку.
        assert task.payload is None

    def test_kinds_land_in_different_redis_keys(self, queue_url):
        """Маршрутизация проверяется на стороне сервера: две очереди — это два
        ключа, а не одно множество с пометкой внутри."""
        llm_task = make_task(task_module.GENERATE, payload={"text": "заявка"})
        local_task = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        submit(queue_url, task_module.GENERATE, llm_task)
        submit(queue_url, task_module.SCORE, local_task)

        from arq import create_pool
        from arq.connections import RedisSettings

        async def scenario():
            pool = await _pool(queue_url)
            try:
                return (await pool.zcard(task_module.QUEUE_LLM),
                        await pool.zcard(task_module.QUEUE_LOCAL),
                        await task_module.ArqBroker(pool).depth())
            finally:
                await pool.aclose()

        llm_depth, local_depth, depths = asyncio.run(scenario())
        assert (llm_depth, local_depth) == (1, 1)
        assert depths == {task_module.QUEUE_LLM: 1, task_module.QUEUE_LOCAL: 1}

    def test_cancelled_task_is_never_picked_up(self, queue_url):
        """Отмена в очереди обязана доходить до воркера: снятый с очереди
        отменённый job не имеет права оплатить вызов модели."""
        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        submit(queue_url, task_module.SCORE, task_id)
        task_store.mark_cancelled(task_id)
        run_worker(queue_url, task_module.QUEUE_LOCAL)
        task = read_task(task_id)
        assert task.status == task_store.CANCELLED
        # Воркер мог снять job с очереди, но заявка на исполнение не
        # состоялась — попыток у задачи остаться не должно.
        assert task.tries == 0 and task.result is None

    def test_second_worker_gives_up_an_already_claimed_task(self, queue_url):
        """Два воркера на одной очереди: второй обязан отказаться от задачи на
        условном переходе, а не исполнить её повторно. Так выглядит и падение
        процесса посреди выполнения, когда job вернулся в очередь."""
        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        submit(queue_url, task_module.SCORE, task_id)
        assert task_store.claim_task(task_id) is not None
        run_worker(queue_url, task_module.QUEUE_LOCAL)
        task = read_task(task_id)
        assert task.status == task_store.RUNNING
        assert task.tries == 1 and task.result is None

    def test_recovery_puts_a_dropped_task_back_into_the_queue(
            self, queue_url, monkeypatch):
        """Проверка критерия этапа B из плана: «падение воркера не теряет
        задачу». Симулируется строкой в статусе `running`, которую застал
        перезапуск: восстановление обязано вернуть её и в базу, и в очередь, а
        затем воркер должен её довыполнить.

        Новым идентификатором попытки — arq не принимает повторно уже известный
        job, и с прежним идентификатором задача в очередь бы не легла.
        """
        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        task_store.claim_task(task_id)

        async def scenario():
            pool = await _pool(queue_url)
            try:
                await task_module._recover_stale_tasks({"redis": pool})
                return await pool.zcard(task_module.QUEUE_LOCAL)
            finally:
                await pool.aclose()

        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _seconds: [(task_id, task_module.QUEUE_LOCAL, 1)])
        assert asyncio.run(scenario()) == 1
        assert read_task(task_id).status == task_store.QUEUED

        run_worker(queue_url, task_module.QUEUE_LOCAL)
        task = read_task(task_id)
        assert task.status == task_store.DONE, (task.status, task.error)
        assert task.tries == 2

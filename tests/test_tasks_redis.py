"""Интеграция с настоящим Redis: кэш, общий бюджет и воркер arq.

Файл пропускается, если `TEST_REDIS_URL` не задан, — ровно как прогон на
PostgreSQL включается переменной `TEST_DATABASE_URL`. Отдельный шаг CI с
сервисом `redis:7-alpine` его всё-таки запускает, потому что три вещи ниже
подменой не проверяются в принципе: Lua-скрипт бюджета исполняет сервер,
ключи очереди формирует arq, а срок жизни значения считает Redis. Без живого
сервера эти тесты подтверждали бы только то, что мы сами себе наврали об API.
"""
import asyncio
from datetime import timedelta
import os
import time
import uuid

import pytest
import redis
from fastapi import HTTPException

from app import tasks as task_module
from app.db import SessionLocal
from app.deps import _RedisWindow, budget_slot, release_ai_budget
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
    """Строка задачи со СВОИМИ входами.

    Маркер в payload обязателен: `uq_ai_tasks_live_inputs` (ревизия 0006)
    считает незакрытые задачи с одинаковым отпечатком входов одной работой, и
    без маркера второй `make_task` в этом же файле получил бы строку первого —
    тесты начали бы спорить за один `task_id` (симптом в живом прогоне: воркер
    дописывает чужую строку, а проверяющий видит `running` вместо `done`).
    Продуктовое поведение при этом верное: одинаковые входы — это и правда одна
    и та же работа, а не две оплаты одного обращения.
    """
    body = dict(payload or {})
    body.setdefault("marker", uuid.uuid4().hex)
    with SessionLocal() as session:
        return task_store.create_task(session, user_id=any_user_id(), kind=kind,
                                      queue=task_module.queue_of(kind),
                                      payload=body,
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


def budget_card(url: str, user_id: int | str) -> int:
    """Сколько слотов часа занято у пользователя — глазами сервера, не окна."""
    return len(budget_members(url, user_id))


def budget_members(url: str, user_id: int | str) -> list:
    """Метки занятых слотов в порядке займа."""
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        return client.zrange(f"ai:budget:{user_id}", 0, -1)
    finally:
        client.close()


class TestSharedBudgetRefundAgainstRealServer:
    """Возврат слота против живой Redis-строки, а не подменённого окна.

    Веб-процесс занимает бюджет ещё до записи задачи, а отдаёт его воркер:
    ответ пришёл из кэша, задача отменена или потеряна вместе с процессом.
    Между этими точками — два разных процесса, поэтому и метка слота
    выводится из пары «пользователь + задача», а не переезжает в очередь
    объектом-ручкой. Проверить это подменой нельзя: `ZREM` исполняет сервер.
    """

    def test_charge_of_the_web_process_is_freed_by_a_second_one(self, queue_url,
                                                                monkeypatch):
        """Окно веб-процесса заняло слот до записи задачи, окно воркера его
        вернуло: два независимых клиента, между ними только пара
        «пользователь + задача». Дальше повторный возврат не даёт слота."""
        from app import deps

        user_id = uuid.uuid4().hex[:8]
        task_id = str(uuid.uuid4())
        worker = _RedisWindow(queue_url, limit=3)
        monkeypatch.setattr(deps, "_redis_budget", worker)

        async def scenario():
            web = _RedisWindow(queue_url, limit=3)
            try:
                await web.acquire(user_id, budget_slot(user_id, task_id))
                assert budget_card(queue_url, user_id) == 1
                assert await release_ai_budget(user_id, task_id=task_id) is True
                assert budget_card(queue_url, user_id) == 0
                # Повторный возврат ничего не освобождает: иначе задача,
                # отменённая дважды (отмена клиента и восстановление после
                # потери), вернула бы себе лишний слот.
                assert await release_ai_budget(user_id, task_id=task_id) is False
                assert budget_card(queue_url, user_id) == 0
            finally:
                await web.close()
                await worker.close()

        asyncio.run(scenario())

    def test_refund_frees_the_named_slot_and_not_the_oldest_one(self, queue_url):
        """Слот возвращается по метке, а не «самый старый»: иначе одна отменённая
        задача вернула бы бюджет живой задачи того же пользователя."""
        user_id = uuid.uuid4().hex[:8]
        first = budget_slot(user_id, "task-held")
        second = budget_slot(user_id, "task-cancelled")
        window = _RedisWindow(queue_url, limit=5)

        async def scenario():
            try:
                await window.acquire(user_id, first)
                await window.acquire(user_id, second)
                assert await window.release(user_id, second) is True
                return budget_members(queue_url, user_id)
            finally:
                await window.close()

        assert asyncio.run(scenario()) == [first]

    def test_refund_lets_the_next_request_through(self, queue_url):
        """Ради этого возврата всё и затевалось: освобождённый слот обязан
        пустить следующий запрос, а не лежать до конца часа. До возврата шесть
        повторов одного закешированного ответа выбирали бы окно с нулём
        обращений к провайдеру."""
        user_id = uuid.uuid4().hex[:8]
        charged = budget_slot(user_id, "task-refunded")
        next_task = budget_slot(user_id, "task-next")
        window = _RedisWindow(queue_url, limit=1)

        async def scenario():
            try:
                await window.acquire(user_id, charged)
                with pytest.raises(HTTPException):
                    await window.acquire(user_id, next_task)
                await window.release(user_id, charged)
                return await window.acquire(user_id, next_task)
            finally:
                await window.close()

        assert asyncio.run(scenario()) == next_task

    def test_foreign_label_is_refused_before_it_reaches_the_server(self, queue_url,
                                                                  monkeypatch):
        """Возврат чужого слота отсекается по префиксу метки: в Redis это был бы
        молчаливый no-op, и ошибка проводки выглядела бы «возврат не сработал»."""
        from app import deps

        mine = uuid.uuid4().hex[:8]
        window = _RedisWindow(queue_url, limit=2)
        monkeypatch.setattr(deps, "_redis_budget", window)

        async def scenario():
            try:
                await window.acquire(mine, budget_slot(mine, "task-mine"))
                assert await release_ai_budget(mine, slot="other:task-mine") is False
                return budget_card(queue_url, mine)
            finally:
                await window.close()

        assert asyncio.run(scenario()) == 1

    def test_worker_refunds_a_slot_it_never_took(self, queue_url, monkeypatch):
        """Связка целиком: веб списал бюджет постановкой, воркер вернул его по
        `tries == 0` через продуктовый код, а не вызовом окна в тесте. Это тот
        случай, когда job сняли с очереди, но заявка на исполнение не
        состоялась — обращаться к провайдеру не стали."""
        from app import deps

        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        user_id = read_task(task_id).user_id
        window = _RedisWindow(queue_url, limit=3)
        monkeypatch.setattr(deps, "_redis_budget", window)

        async def scenario():
            try:
                await window.acquire(user_id, budget_slot(user_id, task_id))
                assert budget_card(queue_url, user_id) == 1
                await task_module._refund_if_never_started(task_id)
                return budget_card(queue_url, user_id)
            finally:
                await window.close()

        assert asyncio.run(scenario()) == 0

    def test_started_task_keeps_its_slot(self, queue_url, monkeypatch):
        """Обратная сторона: задача, которую начинали, слот не возвращает —
        работа уже оплачена, и возврат открыл бы способ бесплатно перебирать
        очередь."""
        from app import deps

        task_id = make_task(task_module.SCORE, payload={"bpmn_xml": bpmn()})
        user_id = read_task(task_id).user_id
        task_store.claim_task(task_id)
        window = _RedisWindow(queue_url, limit=3)
        monkeypatch.setattr(deps, "_redis_budget", window)

        async def scenario():
            try:
                await window.acquire(user_id, budget_slot(user_id, task_id))
                await task_module._refund_if_never_started(task_id)
                return budget_card(queue_url, user_id)
            finally:
                await window.close()

        assert asyncio.run(scenario()) == 1


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

        # Строка обязана быть потерянной по-настоящему: `requeue` держит защёлку
        # по тому же порогу, по которому уборка признаёт задачу потерянной
        # (`_stale_thresholds`, а не голый таймаут очереди), и свежая заявка под
        # неё не попадает.
        with SessionLocal() as session:
            session.query(AiTask).filter(AiTask.id == task_id).update(
                {AiTask.started_at: task_store.utc_now()
                 - timedelta(
                     seconds=task_module._stale_thresholds()[
                         task_module.QUEUE_LOCAL] + 60)})
            session.commit()
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _seconds: [(task_id, task_module.QUEUE_LOCAL, 1,
                                               task_store.RUNNING)])
        assert asyncio.run(scenario()) == 1
        assert read_task(task_id).status == task_store.QUEUED

        run_worker(queue_url, task_module.QUEUE_LOCAL)
        task = read_task(task_id)
        assert task.status == task_store.DONE, (task.status, task.error)
        assert task.tries == 2

"""Живой дубликат запроса: одна незавершённая работа на одни входы.

Ключ идемпотентности фронтенд выдаёт на каждое действие
(`bpmn-constructor/src/api/tasks.js:newIdempotencyKey`), поэтому двойное нажатие
«Сгенерировать» и повтор после сетевого обрыва приходят РАЗНЫМИ ключами.
Ограничение `uq_ai_tasks_idempotency` их не ловит: оно сверяет ключ, а не работу.
Без проверки «такая задача ещё жива» каждый такой клик — вторая строка в очереди и
вторая оплата того же обращения к модели.

Отдельно о том, чего дубликат НЕ касается: завершённая и снятая задача входы не
удерживают. Переспрос после отказа провайдера и новая попытка после отмены —
законные запуски, и свести их к «задача уже есть» значило бы лишить пользователя
права спросить ещё раз.
"""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import tasks as task_module
from app.db import SessionLocal
from app.main import app
from app.models import AiTask
from app.services import tasks as task_store
from app.timeutils import utc_now


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def owner(client):
    """Аккаунт с настоящим id: на PostgreSQL внешний ключ не принимает вымышленного."""
    email = f"dedupe-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/register", json={"name": "Дубликаты", "email": email,
                                              "password": "Passw0rd!"}).status_code == 200
    token = client.post("/api/login", json={"email": email,
                                            "password": "Passw0rd!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def user_id(client, headers):
    return client.get("/api/me", headers=headers).json()["id"]


def make(user_id, *, kind="generate", text, key=None, status=None):
    """Строка задачи в базе напрямую.

    Через сервис, а не через HTTP: в режиме без Redis постановка исполняет задачу
    сразу, и живую строку для проверки из маршрута получить было бы нечем.
    """
    with SessionLocal() as session:
        task = task_store.create_task(
            session, user_id=user_id, kind=kind,
            queue=task_module.queue_of(kind),
            payload={"text": text}, idempotency_key=key)
        if status is not None:
            session.query(AiTask).filter(AiTask.id == task.id).update(
                {AiTask.status: status})
            session.commit()
        return task.id


def live_rows(user_id, text):
    """Сколько незакрытых задач генерации с таким описанием висит в базе."""
    with SessionLocal() as session:
        return (session.query(AiTask)
                .filter(AiTask.user_id == user_id,
                        AiTask.kind == "generate",
                        AiTask.inputs_hash == task_store.inputs_fingerprint(
                            kind="generate", payload={"text": text}),
                        AiTask.status.in_(task_store.OPEN))
                .count())


class TestLiveDuplicate:
    def test_service_returns_the_open_twin(self, owner, client):
        """Вторая постановка с теми же входами и другим ключом отдаёт первую задачу.

        Ровно тот случай, что стоит денег: повтор без ожидания ответа приносит
        новый ключ, и если контур заводит отдельную строку, модель спрашивают
        дважды об одном и том же.
        """
        who = user_id(client, owner)
        text = f"заявка в сервис {uuid.uuid4().hex}"
        first = make(who, text=text)
        second = make(who, text=text, key=str(uuid.uuid4()))
        assert second == first, "живой дубликат заведён отдельной строкой"
        assert live_rows(who, text) == 1

    def test_route_hands_back_the_live_task_without_paying_again(self, owner, client):
        """Маршрут отдаёт живую задачу и не списывает часовой бюджет.

        Проверка держит сразу два обещания: ответ `202` с прежним `task_id` и
        `replayed: true`, и то, что обращения к модели не было — в тестовом режиме
        без Redis задача исполнялась бы внутри ответа, и без дедупликации маршрут
        ушёл бы к транспорту, а тест бы упал, а не прошёл молча. Второго опроса
        здесь не возникает по построению, а не по полю: клиент (`waitForTask`)
        опрашивает тот `task_id`, который пришёл, то есть строку победителя.
        `replayed` — пометка для журнала и tests, читатель её не ветвляет.
        """
        who = user_id(client, owner)
        text = f"согласование закупки {uuid.uuid4().hex}"
        live = make(who, text=text)
        response = client.post("/api/generate", json={"text": text,
                                                     "idempotency_key": str(uuid.uuid4())},
                               headers=owner)
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["task_id"] == live, body
        assert body["replayed"] is True, body
        assert live_rows(who, text) == 1

    def test_finished_task_does_not_block_a_new_run(self, owner, client):
        """Завершённая задача входы не держит: переспрос обязан быть возможен."""
        who = user_id(client, owner)
        text = f"бронь транспорта {uuid.uuid4().hex}"
        first = make(who, text=text, status=task_store.DONE)
        assert make(who, text=text) != first

    def test_cancelled_task_releases_the_inputs(self, owner, client):
        """Снятая задача тоже не держит входы: отмена означает «заново», а не
        «строка висит навсегда»."""
        who = user_id(client, owner)
        text = f"найм сотрудника {uuid.uuid4().hex}"
        first = make(who, text=text, status=task_store.CANCELLED)
        assert make(who, text=text) != first

    def test_other_user_keeps_their_own_task(self, owner, client):
        """Дедупликация работает внутри пользователя: чужая живая задача с теми же
        словами — чужая работа, а не повтор."""
        email = f"dedupe-other-{uuid.uuid4().hex[:8]}@example.com"
        client.post("/api/register", json={"name": "Второй", "email": email,
                                           "password": "Passw0rd!"})
        token = client.post("/api/login", json={"email": email,
                                                "password": "Passw0rd!"}).json()["access_token"]
        other = {"Authorization": f"Bearer {token}"}
        text = f"складская инвентаризация {uuid.uuid4().hex}"
        assert make(user_id(client, other), text=text) != make(user_id(client, owner),
                                                               text=text)

    def test_other_kind_is_other_work(self, owner, client):
        """Одинаковые входы разных видов — разная работа: `kind` входит в
        отпечаток, поэтому скоринг схемы не гасит её генерацию."""
        who = user_id(client, owner)
        text = f"обработка инцидента {uuid.uuid4().hex}"
        assert make(who, kind=task_module.GENERATE, text=text) != \
            make(who, kind=task_module.SCORE, text=text)


def read(task_id):
    with SessionLocal() as session:
        return session.get(AiTask, task_id)


def set_state(task_id, **values):
    with SessionLocal() as session:
        session.query(AiTask).filter(AiTask.id == task_id).update(values)
        session.commit()


class TestRaceLoserStopsAtTheWinner:
    """Гонка дедупликации: вставку выиграл чужой запрос.

    Проигравший обязан остановиться на строке победителя. Прежний код шёл дальше
    с её идентификатором: перепоставлял её в очередь (второй полный прогон, до
    шести обращений к модели за один заказ), в ветке отказа закрывал живую чужую
    задачу как `queue_unavailable` (её оплаченный результат `_close` уже не
    принимает), и возвращал слот бюджета, который списал победитель и уже мог
    потратить на провайдера.

    Ранний дедуп (`task_store.find_live_by_inputs`, `app/routers/ai.py:103`)
    отключается намеренно: живая строка победителя в базе есть, и маршрут вышел бы
    на `replayed: true` до вставки — тогда ни `create_task`, ни `submit`, ни
    возврат слота не вызывались бы вовсе, а тесты остались бы зелёными. Отключённый
    ранний просмотр — единственный способ дойти до самой гонки вставки.
    """

    @staticmethod
    def _no_early_dedupe(monkeypatch):
        monkeypatch.setattr(task_store, "find_live_by_inputs",
                            lambda *args, **kwargs: None)

    def test_loser_answers_about_the_winner_without_reposting(self, owner, client,
                                                             monkeypatch):
        from app.routers import ai as ai_router

        who = user_id(client, owner)
        text = f"гонка постановки {uuid.uuid4().hex}"
        winner = make(who, text=text)
        submitted = []

        class Broker:
            name = "arq"

            async def submit(self, kind, task_id):
                submitted.append((kind, task_id))

        async def get_broker():
            return Broker()

        def raced(db, **kwargs):
            """Как выглядит гонка в базе: `create_task` отдал строку победителя."""
            with SessionLocal() as session:
                return session.get(AiTask, winner)

        self._no_early_dedupe(monkeypatch)
        monkeypatch.setattr(ai_router, "get_broker", get_broker)
        monkeypatch.setattr(task_store, "create_task", raced)
        response = client.post("/api/generate", json={"text": text}, headers=owner)

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["task_id"] == winner, body
        assert body["replayed"] is True, body
        assert submitted == [], "проигравший перепоставил задачу победителя"
        task = read(winner)
        assert task.status == task_store.QUEUED, task.error
        assert task.error_code is None

    def test_loser_does_not_return_the_winners_slot(self, owner, client,
                                                    monkeypatch):
        """Чужой слот бюджета проигравший не освобождает.

        Метка слота выводима из пары «пользователь + задача»
        (`app/deps.py:budget_slot`), поэтому возврат по идентификатору победителя
        снимал бы слот, списанный чужим запросом, — лишая пользователя часовой
        ёмкости ровно за ту работу, за которую он уже заплатил обращением к
        модели.
        """
        import asyncio

        from app import deps
        from app.models import User

        who = user_id(client, owner)
        text = f"гонка бюджета {uuid.uuid4().hex}"
        winner = make(who, text=text)
        with SessionLocal() as session:
            user = session.get(User, who)

        async def spend():
            await deps.consume_ai_budget(user, task_id=winner)

        asyncio.run(spend())

        class Broker:
            name = "arq"

            async def submit(self, kind, task_id):
                raise AssertionError("проигравший не имеет права ставить задачу")

        async def get_broker():
            return Broker()

        monkeypatch.setattr("app.routers.ai.get_broker", get_broker)
        self._no_early_dedupe(monkeypatch)
        monkeypatch.setattr(task_store, "create_task",
                            lambda db, **kw: read(winner))
        response = client.post("/api/generate", json={"text": text}, headers=owner)
        assert response.status_code == 202, response.text

        async def released():
            return await deps.release_ai_budget(who, task_id=winner)

        # True значит «метка нашлась»: слот победителя цел, проигравший его не
        # тронул. До починки здесь было False — возврат уже случился.
        assert asyncio.run(released()) is True


class TestStaleLatch:
    """Защёлка `stale_after` в `requeue`: убранная гонка с живым воркером."""

    def test_fresh_claim_is_not_requeued(self, owner, client):
        who = user_id(client, owner)
        task_id = make(who, text=f"свежая заявка {uuid.uuid4().hex}")
        set_state(task_id, status=task_store.RUNNING,
                  started_at=task_store.utc_now())
        assert task_store.requeue(task_id, stale_after=60) is False
        assert read(task_id).status == task_store.RUNNING

    def test_a_row_old_enough_is_still_requeued(self, owner, client):
        """Порог тот же, по которому строку посчитали потерянной: для неё возврат
        обязан пройти, иначе уборка теряет смысл."""
        who = user_id(client, owner)
        task_id = make(who, text=f"потерянная заявка {uuid.uuid4().hex}")
        set_state(task_id, status=task_store.RUNNING,
                  started_at=utc_now() - timedelta(seconds=120))
        assert task_store.requeue(task_id, stale_after=60) is True
        assert read(task_id).status == task_store.QUEUED


class TestRetentionGuard:
    def test_zero_retention_keeps_the_history(self, owner, client):
        """`LLM_CACHE_TTL=0` — штатное «кэш выключен», а не «история живёт ноль
        секунд»: без проверки уборка стирала бы закрытые задачи целиком."""
        who = user_id(client, owner)
        text = f"закрытая история {uuid.uuid4().hex}"
        task_id = make(who, text=text, status=task_store.DONE)
        set_state(task_id, finished_at=utc_now() - timedelta(days=365))
        assert task_store.purge_closed(0) == 0
        assert read(task_id) is not None

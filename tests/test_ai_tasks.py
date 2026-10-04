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
import contextlib
import importlib
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import tasks as task_module
from app.db import SessionLocal, engine
from app.main import app
from app.models import AiTask
from app.services import tasks as task_store


def cached_keys(client):
    """Ключи ответов в хранилище кэша: счётчики лежат рядом и в проверках
    «в кэш ничего не легло» участвовать не должны."""
    return {key for key in client.store if not key.endswith(":stats")}


@contextlib.contextmanager
def counting():
    """SQL, прожитые внутри блока.

    Посчитана ли доля базой или собрана в Python по строкам — видно по форме и
    числу запросов, а не по таймингу.
    """
    seen = []

    def record(_conn, _cursor, statement, _params, _context, _executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)

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
    """Строка задачи в базе напрямую: часть сценариев из HTTP недостижима.

    Без явного `payload` входы получают случайную метку: живая задача с теми же
    входами не заводится дважды (`uq_ai_tasks_live_inputs`), и иначе второй
    сценарий получил бы строку первого — тест тестом, а проверка жизненного
    цикла сорвалась бы на чужом `task_id`.
    """
    with SessionLocal() as session:
        task = task_store.create_task(session, user_id=user_id, kind=kind,
                                      queue=queue or task_module.queue_of(kind),
                                      payload=payload if payload is not None
                                      else {"marker": uuid.uuid4().hex},
                                      diagram_id=diagram_id,
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


def exists(task_id: str) -> bool:
    """Жива ли строка: уборка history-таблицы проверяется отсутствием записи, а
    `read` на удалённой строке упал бы на `expunge(None)` раньше любого assert."""
    with SessionLocal() as session:
        return session.get(AiTask, task_id) is not None


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

        from app import deps

        compose = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "docker-compose.yml")
            .read_text(encoding="utf-8"))["services"]
        for service, settings in (("worker_llm", task_module.LLMWorkerSettings),
                                  ("worker_local", task_module.LocalWorkerSettings)):
            test = compose[service]["healthcheck"]["test"]
            probe = " ".join(test) if isinstance(test, list) else test
            assert settings.health_check_key in probe, (
                f"{service}: проба смотрит не в тот ключ, что пишет воркер")
            # Тот же ключ читает и `/health` (`ai_workers`): у arq имя отметки —
            # `queue_name + ':health-check'`, а мы его переопределяем, и если
            # разъедётся с `worker_health_key`, оператор получит «dead» на живых
            # воркерах молча — тесты на compose этого расхождения не видят.
            assert deps.worker_health_key(settings.queue_name) == \
                settings.health_check_key, (
                    f"{service}: `/health` смотрит не в тот ключ, что пишет воркер")
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

    def test_stale_threshold_follows_the_queue_timeout(self, monkeypatch):
        """Порог потерянности выводится из таймаута, а не стоит литералом.

        Число в штампе (`2100` в прежнем docker-compose и `.env.example`)
        переживает рост `AI_LLM_JOB_TIMEOUT` и начинает хоронить живые задачи:
        их ставят в очередь заново, то есть платят провайдеру второй раз за ту
        же работу.
        """
        from app import config

        monkeypatch.delenv("AI_TASK_STALE_SECONDS", raising=False)
        derived = config._env_int("AI_TASK_STALE_SECONDS",
                                  config._env_int("AI_LLM_JOB_TIMEOUT", 1800) + 300)
        assert derived == config.AI_LLM_JOB_TIMEOUT + 300
        monkeypatch.setenv("AI_LLM_JOB_TIMEOUT", "3600")
        assert config._env_int("AI_TASK_STALE_SECONDS",
                               config._env_int("AI_LLM_JOB_TIMEOUT", 1800) + 300) == 3900

    def test_blank_or_broken_number_falls_back_instead_of_crashing(self, monkeypatch,
                                                                   caplog):
        """`AI_TASK_STALE_SECONDS=` — ключ без значения, и он законен.

        docker-compose передаёт пустую строку по шаблону `${VAR:-}`, а `.env`
        допускает строку ключа без значения. `int("")` при импорте config ронял
        весь backend с `ValueError`, из текста которого не понять, какая
        переменная виновата.
        """
        from app import config

        monkeypatch.setenv("AI_TASK_STALE_SECONDS", "")
        assert config._env_int("AI_TASK_STALE_SECONDS", 2100) == 2100
        monkeypatch.setenv("AI_TASK_STALE_SECONDS", "полтора часа")
        assert config._env_int("AI_TASK_STALE_SECONDS", 2100) == 2100
        assert "не является целым числом" in caplog.text
        monkeypatch.setenv("TASK_POLL_SECONDS", "")
        assert config._env_float("TASK_POLL_SECONDS", 2.0) == 2.0

    def test_retry_backoff_grows(self):
        assert [task_module._backoff_seconds(n) for n in (1, 2, 3)] == [5, 25, 25]


class TestMailerEnvParsing:
    """Почтовые переменные читаются теми же помощниками, что и настройки очереди.

    Класс продолжает проверку `test_blank_or_broken_number_falls_back_instead_of_crashing`
    выше, а не переехал в чужой файл: `app/mailer.py` собирает `ConnectionConfig`
    на импорте модуля, и мусорное значение переменной роняло не отправку письма, а
    весь backend — тот же класс отказа, что снят для `AI_TASK_STALE_SECONDS`.
    """

    @staticmethod
    def reload_conf(monkeypatch, **values):
        """Пересобрать модуль почты с заданным окружением и отдать конфигурацию.

        Модуль перезагружается, а не вызывается `_env_int` напрямую: значение
        обязано пройти ровно тем путём, которым оно читается на импорте
        приложения, иначе проверка расходилась бы с тем, что видит процесс.
        """
        from app import mailer

        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        try:
            return importlib.reload(mailer).email_conf
        finally:
            # Окружение прогона (`conftest` выставляет `MAIL_SERVER=127.0.0.1` и
            # `MAIL_PORT=1`) возвращается вместе с модулем: иначе недопитое
            # значение уезжало бы дальше по тестам, и сброс пароля стучался бы
            # в порт из выдохшейся проверки.
            monkeypatch.undo()
            importlib.reload(mailer)

    def test_port_reaches_the_config_and_garbage_does_not_crash_the_import(
            self, monkeypatch, caplog):
        """`MAIL_PORT` читается `_env_int`, а не `int(os.getenv(...))`.

        Мусорное значение (человек вписал пояснение рядом с числом, или строка
        ключа без значения) на импорте модуля поднимало `ValueError`, из которого
        не понять, какая переменная виновата, и приложение не стартовало вовсе.
        Фолбэк — 465, как в `.env.example`, а предупреждение обязано назвать
        переменную: молча подставить дефолт значило бы тот же самообман, что и
        флаг, который «задан, но не читается».
        """
        from app import config

        monkeypatch.setenv("MAIL_PORT", "2525")
        with caplog.at_level(logging.WARNING, logger="app.config"):
            assert config._env_int("MAIL_PORT", 465) == 2525

        monkeypatch.setenv("MAIL_PORT", "465 (smtps)")
        with caplog.at_level(logging.WARNING, logger="app.config"):
            assert config._env_int("MAIL_PORT", 465) == 465
        assert "MAIL_PORT" in caplog.text
        assert "не является целым числом" in caplog.text

        conf = self.reload_conf(monkeypatch, MAIL_PORT="не порт")
        assert conf.MAIL_PORT == 465

    def test_unset_flags_default_to_implicit_tls(self, monkeypatch):
        """Ни одного ключа пары — и это штатный одиночный запуск: дефолты обязаны
        сойтись в согласованную с портом пару (465, SSL/TLS включён, STARTTLS
        выключен).

        Прежняя проверка `== "true"` оставляла выключенным `MAIL_SSL_TLS=1` (как
        и `yes`, и `on`), а 465-й порт — это implicit TLS: письмо о сбросе пароля
        не уходило, а экран отвечал «письмо отправлено». Поэтому заодно
        проверяются правдоподобные формы истины и лжи, а не только голое `true`.
        """
        from app import config

        for name in ("MAIL_STARTTLS", "MAIL_SSL_TLS"):
            monkeypatch.delenv(name, raising=False)
        assert config._env_flag("MAIL_STARTTLS", "false") is False
        assert config._env_flag("MAIL_SSL_TLS", "true") is True

        for truthy in ("1", "true", "TRUE", "yes", "on", " on "):
            monkeypatch.setenv("MAIL_SSL_TLS", truthy)
            assert config._env_flag("MAIL_SSL_TLS", "false") is True, truthy
        for falsy in ("0", "false", "no", "off"):
            monkeypatch.setenv("MAIL_SSL_TLS", falsy)
            assert config._env_flag("MAIL_SSL_TLS", "true") is False, falsy

        conf = self.reload_conf(monkeypatch, MAIL_PORT=None, MAIL_STARTTLS=None,
                                MAIL_SSL_TLS=None)
        assert (conf.MAIL_PORT, conf.MAIL_STARTTLS, conf.MAIL_SSL_TLS) == (
            465, False, True)

    def test_blank_flag_falls_back_to_the_default(self, monkeypatch):
        """`MAIL_SSL_TLS=` — пустая строка: `_env_int` читает её как «не задано»,
        а `_env_flag` — как «выключено», и это разные прочтения одного и того же
        пробела в `.env`.

        Правило «пустое значение означает не задано и берёт дефолт» записано в
        докстринге `_env_int` дословно, а для флага оно даёт обратное: при пустом
        `MAIL_SSL_TLS` порт остаётся 465, а implicit TLS молча снимается — ровно
        тот дефект, из-за которого письмо о сбросе пароля не уходило, а экран
        отвечал «письмо отправлено». Ни в логах, ни в ответе маршрута различие не
        видно: compose подставляет `${MAIL_SSL_TLS:-true}` сам, а одиночный запуск
        через `python main.py` с пустым ключом в `.env` — нет.
        """
        from app import config

        monkeypatch.setenv("MAIL_STARTTLS", "")
        monkeypatch.setenv("MAIL_SSL_TLS", "")
        assert config._env_flag("MAIL_STARTTLS", "false") is False
        assert config._env_flag("MAIL_SSL_TLS", "true") is True, (
            "пустое значение флага означает «не задано», а не «выключено»: "
            "дефолт `app/mailer.py` для `MAIL_SSL_TLS` — истина")

        conf = self.reload_conf(monkeypatch, MAIL_STARTTLS="", MAIL_SSL_TLS="")
        assert conf.MAIL_SSL_TLS is True
        assert conf.MAIL_STARTTLS is False


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
        оставила бы задачу без входа навсегда.

        Описание уникальное: возвращённая в очередь задача остаётся живой, а
        живая задача с теми же входами не заводится второй раз
        (`uq_ai_tasks_live_inputs`) — иначе последующие тесты маршрутов получили
        бы эту строку вместо своей.
        """
        task_id = make_task(user_id=owner[1],
                            payload={"text": f"заявка {uuid.uuid4().hex}"})
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

    def test_queue_timeout_cancellation_closes_the_row(self, owner):
        """Таймаут очереди снимает работу отменой корутины, а `CancelledError`
        на Python 3.12 — `BaseException`, поэтому ветка `except Exception` ниже
        его не видит. Без отдельной ветки строка оставалась бы в `running`
        вечно: клиент опрашивал бы задачу до своего таймаута, а уборка
        поднимала бы её же на каждом круге. Проверка держит оба факта — закрытый
        статус и то, что отмену не заглушили (наружу обязана лететь та же
        `CancelledError`, иначе процесс воркера не поймёт, что ему пора
        сворачиваться)."""
        task_id = make_task(task_module.SCORE, user_id=owner[1],
                            payload={"marker": uuid.uuid4().hex})

        async def work(_task_id):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            run(task_id, work, max_tries=3)

        task = read(task_id)
        assert task.status in task_store.TERMINAL, (task.status, task.error)
        assert (task.status, task.error_code) == (task_store.FAILED, "worker_lost")
        # Метка завершения обязательна: по ней `purge_closed` отличает просроченную
        # историю от незакрытой строки, а без неё задача не ушла бы из таблицы.
        assert task.finished_at is not None
        assert "не уложилась" in (task.error or "")
        # Отменённая по таймауту очередь задача не обязана выглядеть так, будто
        # её никто не начинал: заявка на исполнение уже выставлена, и `tries`
        # отличает её от потерянной постановки (по тому же признаку возвращается
        # слот бюджета).
        assert task.tries == 1


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

        def fake_generate(text, **kw):
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
                            lambda text, **kw: calls.append(text) or {
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
                            lambda text, **kw: calls.append(text) or {
                                "status": "error", "step": "llm",
                                "error": "недоступен"})
        # Описание уникальное на тест, но одинаковое внутри него: здесь и
        # проверяется, что два одинаковых запроса спрашивают модель дважды, а не
        # что второй отдаётся из чужого отказа.
        text = f"заявка {uuid.uuid4().hex}"
        for _ in range(2):
            client.post("/api/generate", json={"text": text}, headers=owner[0])
        assert len(calls) == 2
        assert cached_keys(cache_installed.client) == set()

    def test_truncation_is_remembered_and_replayed_as_failure(self, client, owner,
                                                             cache_installed,
                                                             monkeypatch):
        from app import ai as ai_module

        calls = []
        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text, **kw: calls.append(text) or {
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


class TestGenerationCancelHook:
    """Отмена обязана доходить до генерации, а не только лежать в строке задачи.

    Прогон делает до шести обращений к модели. Без колбэка на границе стадий
    отменённая задача договаривала их до конца: пользователь уже ушёл, слот
    часового бюджета списан, а провайдер оплачен.
    """

    def test_generator_receives_a_cancel_probe(self, client, owner, monkeypatch):
        from app import ai as ai_module

        seen = {}

        def fake_generate(text, cancel_check=None):
            seen["probe"] = cancel_check
            return {"status": "success", "bpmn": XML, "structure": {}, "notes": []}

        monkeypatch.setattr(ai_module.generator, "generate", fake_generate)
        response = client.post("/api/generate", json={"text": "приёмка товара"},
                               headers=owner[0])

        assert response.status_code == 202
        assert callable(seen.get("probe")), (
            "генерация вызвана без хука отмены — флаг из базы никто не смотрит")

    def test_cancelled_generation_lands_as_cancelled_not_failed(self, client, owner,
                                                                monkeypatch):
        from app import ai as ai_module

        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text, **kw: {"status": "error",
                                                "step": "cancelled",
                                                "error": "Задача отменена."})
        body = client.post("/api/generate", json={"text": "возврат товара"},
                           headers=owner[0]).json()

        descriptor = body.get("result") or body
        assert descriptor["status"] == task_store.CANCELLED, descriptor
        # Не «ошибка», которую клиент вправе повторить: отмена — исполненное
        # решение пользователя, а не сбой контура.
        assert not descriptor.get("error_code")

    def test_timeout_is_not_reported_as_a_provider_outage(self, client, owner,
                                                          monkeypatch):
        """Шаг `budget` — наш собственный лимит времени, а не отказ провайдера.

        Под общим шагом «llm» он стал бы повторяемой ошибкой очереди, и клиент
        заплатил бы второй раз за схему, которая заведомо не влезает.
        """
        from app import ai as ai_module

        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text, **kw: {"status": "error",
                                                "step": "budget",
                                                "error": "не уложилось"})
        body = client.post("/api/generate", json={"text": "длинный процесс"},
                           headers=owner[0]).json()

        descriptor = body.get("result") or body
        assert descriptor["status"] == "failed", descriptor
        assert descriptor.get("error_code") == "budget", descriptor


class TestBudgetRefund:
    """Часовой бюджет возвращается, если за задачу не пришлось платить.

    Слот списывается постановкой, а обращение к провайдеру происходит в
    исполнителе. Без возврата повтор одного закешированного ответа или отмена,
    поднятая до первого вызова модели, съедали `AI_REQUESTS_PER_HOUR` с нулём
    обращений к провайдеру — то есть лимит начинал наказывать за работу, которой
    не было. Отказ по входу возвращается: он и есть плата за сделанную проверку.
    """

    @staticmethod
    def charged(user_id: int, task_id: str) -> bool:
        """Занят ли слот с меткой «пользователь + задача» в локальном окне.

        Проверяется именно метка, а не длина окна: пользователь в этом модуле
        один на все тесты, и счётчик зависел бы от порядка прогонов.
        """
        from app import deps

        label = deps.budget_slot(user_id, task_id)
        return any(stored == label
                   for _at, stored in deps._ai_window._hits.get(str(user_id), ()))

    def test_paid_generation_keeps_its_slot(self, client, owner, monkeypatch):
        from app import ai as ai_module

        monkeypatch.setattr(ai_module.generator, "generate",
                            lambda text, **kw: {"status": "success", "bpmn": XML,
                                                "structure": {}, "notes": []})
        body = client.post("/api/generate", json={"text": "приёмка товара"},
                           headers=owner[0]).json()

        assert self.charged(owner[1], body["task_id"]), (
            "прогон с обращением к модели обязан оставить слот списанным — "
            "иначе эта проверка не отличает возврат от его отсутствия")

    def test_answer_from_cache_gives_the_slot_back(self, client, owner, monkeypatch):
        from app import ai as ai_module

        def no_model(*_a, **_kw):
            raise AssertionError("генерация вызвана, хотя ответ уже в кэше")

        class Cache:
            def lookup_generation(self, _text):
                return SimpleNamespace(negative=False, error=None,
                                       result={"status": "success", "bpmn": XML,
                                               "structure": {}, "notes": []})

            def remember_generation(self, *_a):
                pass

        monkeypatch.setattr(ai_module.generator, "generate", no_model)
        monkeypatch.setattr(task_module.ai_cache, "get_ai_cache", lambda: Cache())
        body = client.post("/api/generate", json={"text": "возврат товара"},
                           headers=owner[0]).json()

        assert body["status"] == task_store.DONE, body
        assert not self.charged(owner[1], body["task_id"]), (
            "ответ из кэша не платит провайдеру, а слот бюджета остался занят")

    def test_cancel_before_work_gives_the_slot_back(self, client, owner, monkeypatch):
        from app import ai as ai_module

        def no_model(*_a, **_kw):
            raise AssertionError("генерация вызвана для отменённой задачи")

        monkeypatch.setattr(ai_module.generator, "generate", no_model)
        monkeypatch.setattr(task_module.task_store, "cancel_requested",
                            lambda _task_id: True)
        body = client.post("/api/generate", json={"text": "отмена до работы"},
                           headers=owner[0]).json()

        assert body["status"] == task_store.CANCELLED, body
        assert not self.charged(owner[1], body["task_id"])

    def test_task_nobody_claimed_gives_the_slot_back(self, owner):
        """`tries == 0` — заявка на исполнение не выставлялась значит модели не
        было; такой же признак у потерянной постановки и у `skipped`.
        """
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1])

        async def scenario():
            with SessionLocal() as db:
                user = db.get(User, owner[1])
            await deps.consume_ai_budget(user, task_id=task_id)
            assert self.charged(owner[1], task_id)
            await task_module._refund_if_never_started(task_id)

        asyncio.run(scenario())
        assert not self.charged(owner[1], task_id)

    def test_task_already_claimed_keeps_the_slot(self, owner):
        """Чужая заявка — не основание возвращать бюджет: тот воркер платит."""
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1])
        mark(task_id, tries=1)

        async def scenario():
            with SessionLocal() as db:
                user = db.get(User, owner[1])
            await deps.consume_ai_budget(user, task_id=task_id)
            await task_module._refund_if_never_started(task_id)

        asyncio.run(scenario())
        assert self.charged(owner[1], task_id)

    def test_abort_during_submit_closes_the_row_and_returns_the_slot(self, client,
                                                                     owner,
                                                                     monkeypatch):
        """Обрыв соединения ровно во время постановки не оставляет живую строку.

        Ветка `except Exception` отмены не видит (`CancelledError` не `Exception`),
        и до починки заказ умирал так: строка `queued` со списанным слотом. Дальше
        либо job уже лёг в Redis и модель спрашивается для того, кто ушёл, либо
        строка доживает до уборки потерянных постановок и перепоставляется оттуда —
        снова с оплатой прогона. Проверка состоянием, а не типом выброса: из TestClient
        отмена может выйти и обёрнутым исключением, а значимы именно база и окно.
        """
        import contextlib

        from app.routers import ai as ai_router

        async def aborting_broker():
            class Broker:
                name = "arq"

                async def submit(self, kind, task_id):
                    current = asyncio.current_task()
                    if current is not None:
                        current.cancel()
                    await asyncio.sleep(0)
                    raise AssertionError("сюда не доходим")

            return Broker()

        monkeypatch.setattr(ai_router, "get_broker", aborting_broker)
        with contextlib.suppress(BaseException):
            client.post("/api/generate", json={"text": f"аборт {uuid.uuid4().hex}"},
                        headers=owner[0])

        with SessionLocal() as session:
            row = (session.query(AiTask)
                   .filter(AiTask.user_id == owner[1])
                   .order_by(AiTask.created_at.desc()).first())
        assert row is not None, "задача не заведена вовсе"
        assert row.status == task_store.CANCELLED, row.status
        assert not self.charged(owner[1], row.id), (
            "отменённая постановка удержала слот часового бюджета")

    def test_cancel_route_returns_the_slot_of_a_queued_task(self, client, owner):
        """Маршрут отмены снимает слот сам: воркера, вернувшего бы его, может не быть.

        Постановка в режиме очереди списывает бюджет веб-процессом, а возврат
        держал только воркер (`_execute` → «отменена до исполнения»). Если Redis лёг
        между постановкой и заявкой либо воркер не поднят, строка уже `cancelled` —
        терминальная, а уборка потерянных видит только незакрытые, — и метка
        `{user}:{task}` держала бы часовой лимит до конца окна за обращение,
        которого не было. Увидено живым прогоном: снятая заявка оставляла слот,
        пока воркер не поднят.
        """
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1])
        with SessionLocal() as db:
            user = db.get(User, owner[1])
        asyncio.run(deps.consume_ai_budget(user, task_id=task_id))
        assert self.charged(owner[1], task_id)

        assert client.post(f"/api/tasks/{task_id}/cancel",
                           headers=owner[0]).status_code == 200
        assert not self.charged(owner[1], task_id), (
            "снятая до исполнения задача удержала слот: возвращать некому")

    def test_cancel_race_with_a_worker_claim_keeps_the_slot(self, client, owner,
                                                            monkeypatch):
        """Гонка `queued → running` не возвращает слот оплаченной работы.

        Статус читается снимком ДО `mark_cancelled`, а закрытие принимает любую
        открытую строку (`OPEN = queued, running`), поэтому «отменена до
        исполнения» может оказаться отменённой уже в работе: заявка воркера
        (`tries == 1`) означает, что модель уже спрашивается, и возврат дал бы
        второй прогон за те же деньги.
        """
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1])
        with SessionLocal() as db:
            user = db.get(User, owner[1])
        asyncio.run(deps.consume_ai_budget(user, task_id=task_id))
        assert self.charged(owner[1], task_id)

        real_close = task_store.mark_cancelled

        def racing(closed_id):
            task_store.claim_task(closed_id)
            return real_close(closed_id)

        monkeypatch.setattr(task_store, "mark_cancelled", racing)
        assert client.post(f"/api/tasks/{task_id}/cancel",
                           headers=owner[0]).status_code == 200
        assert self.charged(owner[1], task_id), (
            "отмена в гонке с заявкой воркера вернула слот работы, "
            "которую уже оплачивают")

    def test_abort_during_inline_submit_keeps_the_paid_slot(self, client, owner,
                                                             monkeypatch):
        """Инлайн-режим исполняет задачу внутри `submit`, поэтому обрыв связи
        после начала работы слот не отдаёт.

        `_execute` при том же сценарии возврат осознанно пропускает («возврат по
        таймауту отдал бы обратно оплаченное обращение»), а маршрут постановки
        возвращал слот безусловно: генерация + обрыв = бесплатный слот, и
        повторимо. Живой прогон ревьюера: `MODEL_CALLS 1 STATUS done CHARGED
        False` при лжи в логе («строка закрыта» — её закрыл успешный финал).
        """
        from app.routers import ai as ai_router

        class InlineAbort:
            name = "inline"

            async def submit(self, kind, submitted_id):
                task_store.claim_task(submitted_id)
                current = asyncio.current_task()
                if current is not None:
                    current.cancel()
                await asyncio.sleep(0)
                raise AssertionError("сюда не доходим")

            async def close(self):
                return None

        async def fake_broker():
            return InlineAbort()

        monkeypatch.setattr(ai_router, "get_broker", fake_broker)
        with contextlib.suppress(BaseException):
            client.post("/api/generate", json={"text": f"аборт в инлайне {uuid.uuid4().hex}"},
                        headers=owner[0])

        with SessionLocal() as session:
            row = (session.query(AiTask)
                   .filter(AiTask.user_id == owner[1])
                   .order_by(AiTask.created_at.desc()).first())
        assert row is not None, "задача не заведена вовсе"
        assert self.charged(owner[1], row.id), (
            "обрыв во время инлайн-исполнения вернул слот оплаченного прогона")

    def test_cancel_puts_the_database_write_in_the_threadpool(self, client, owner,
                                                              monkeypatch):
        """Закрытие строки и чтение статуса — в потоке пула, не в цикле событий.

        Маршрут асинхронный из-за возврата слота через Redis, и ровно поэтому
        блокированная запись в базу в его теле вешала ивент-луп, а сессия запроса
        держала соединение пула через `await`. Тот же приём, что у соседнего
        `task_stats`, и проверяется он так же: обёртка вызвана на `_cancel_in_db`.
        """
        from app.routers import tasks as tasks_router

        seen = {}
        real = tasks_router.run_in_threadpool

        async def spy(fn, *args, **kwargs):
            seen["fn"] = getattr(fn, "__name__", "?")
            return await real(fn, *args, **kwargs)

        monkeypatch.setattr(tasks_router, "run_in_threadpool", spy)
        task_id = make_task(user_id=owner[1], status=task_store.QUEUED)

        assert client.post(f"/api/tasks/{task_id}/cancel",
                           headers=owner[0]).status_code == 200
        assert seen.get("fn") == "_cancel_in_db", seen

    def test_cancel_route_keeps_the_slot_of_a_running_task(self, client, owner):
        """Идущую задачу отмена бюджету не возвращает: провайдера уже спрашивали.

        Заявка `queued → running` состоялась, работа оплачивается, и возврат слота
        на этой ветке дал бы второй бесплатный прогон за те же деньги.
        """
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        with SessionLocal() as db:
            user = db.get(User, owner[1])
        asyncio.run(deps.consume_ai_budget(user, task_id=task_id))

        response = client.post(f"/api/tasks/{task_id}/cancel", headers=owner[0])
        assert response.status_code == 200
        assert response.json()["status"] == task_store.RUNNING
        assert self.charged(owner[1], task_id)

    def test_cancellation_of_the_work_itself_gives_the_slot_back(self, owner,
                                                                 monkeypatch):
        """Отмена, пришедшая до корутины работы, стоит пользователю слот, которого
        у неё не было.

        Ветка `TaskCancelled` в `_execute` возвращает бюджет, когда `work_started`
        ещё не выставлен, а соседняя ветка `asyncio.CancelledError` — нет, хотя
        провайдер в этом случае физически не спрашивался: `work()` не был вызван
        ни разу. Обещание возврата записано в докстринге
        `app/deps.py:release_ai_budget` дословно («`queue_unavailable` и
        `worker_lost` случаются до обращения к провайдеру»), а `worker_lost` — это
        и есть код, которым закрывается отменённая задача. Без возврата часовой
        лимит съедался бы за работу, которую пользователь отменил сам (отмена
        идущей задачи и `keepalive`-закрытие вкладки в режиме исполнения в
        процессе), то есть лимит наказывал бы за обращение, которого не было.

        Симулируется отменой на чтении флага: `cancel_requested` — последнее, что
        контур успевает сделать до `work_started`.
        """
        from app import deps
        from app.models import User

        task_id = make_task(user_id=owner[1],
                            payload={"marker": uuid.uuid4().hex})

        def cancelled_while_reading_the_flag(_task_id):
            raise asyncio.CancelledError

        def work_never_runs(_task_id):
            raise AssertionError("работа началась, хотя отмена пришла до неё")

        monkeypatch.setattr(task_module.task_store, "cancel_requested",
                            cancelled_while_reading_the_flag)

        async def scenario():
            with SessionLocal() as db:
                user = db.get(User, owner[1])
            await deps.consume_ai_budget(user, task_id=task_id)
            assert self.charged(owner[1], task_id), (
                "постановка не списала слот — проверка не отличает возврат от "
                "его отсутствия")
            with pytest.raises(asyncio.CancelledError):
                await task_module._execute(dict(task_module.INLINE_CONTEXT),
                                           task_id, work_never_runs)

        asyncio.run(scenario())
        assert read(task_id).error_code == "worker_lost", "закрыть-то её закрыли"
        assert not self.charged(owner[1], task_id), (
            "задачу сняли отменой до первого обращения к модели, а слот часового "
            "бюджета остался списанным")


class TestHourlyWindowEdges:
    """Крайние значения окна: лимит «ноль» и пустое окно.

    `AI_REQUESTS_PER_HOUR=0` — это не опечатка в окружении, а штатный способ
    закрыть ИИ-контур для всех (например, на время сбоя провайдера, чтобы никто
    не платил). Прежний индекс `hits[0][0]` на пустом окне превращал такой режим
    в 500 на каждом запросе: отказ по лимиту и падение контура неразличимы для
    клиента, а `Retry-After` при 500 нет, и фронтенд не ждёт — он долбит снова.
    """

    @staticmethod
    def window(limit: int):
        """Окно без общего состояния модуля: `deps._ai_window` живёт на процесс, и
        судить по нему «пусто ли окно» значило бы зависеть от порядка тестов."""
        from app import deps

        return deps._SlidingWindow(limit)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_empty_window_with_a_zero_limit_refuses_instead_of_indexing(
            self, limit):
        """Проверка прямого ветвления `_SlidingWindow.acquire`: ключ ещё не имел
        ни одного обращения, поэтому окно пусто, а условие `len(hits) >= limit`
        уже истинно при нулевом лимите."""
        with pytest.raises(HTTPException) as raised:
            self.window(limit).acquire(f"user-{limit}")
        assert raised.value.status_code == 429
        # «Подождите час» при пустом окне — единственная честная оценка: считать
        # её от `hits[0]`, которого нет, нечем.
        assert int(raised.value.headers["Retry-After"]) >= 1

    def test_closed_budget_answers_429_not_500(self, client, monkeypatch):
        """То же числом маршрута: свежий пользователь (его окно пусто) и лимит
        «закрыто» обязаны дать 429 с подсказкой, а не 500.

        Маршрут берётся тот, что обращение к модели и платит: бюджет меряет
        вызовы провайдера, а скоринг схемы (`ai_local`) его не списывает вовсе —
        второй тест держит именно это, иначе шестьдесят оценок заперли бы
        пользователю час на генерацию с нулём оплаченных обращений.
        """
        from app import deps

        email = f"budget-zero-{uuid.uuid4().hex[:8]}@example.com"
        assert client.post("/api/register", json={"name": "Ноль", "email": email,
                                                 "password": "Passw0rd!"}) \
            .status_code == 200
        token = client.post("/api/login", json={"email": email,
                                               "password": "Passw0rd!"}
                            ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        monkeypatch.setattr(deps._ai_window, "limit", 0)
        response = client.post("/api/generate",
                               json={"text": f"заявка {uuid.uuid4().hex}"},
                               headers=headers)
        assert response.status_code == 429, response.text
        assert int(response.headers["Retry-After"]) >= 1

    def test_budget_refusal_is_not_logged_as_a_broken_task(self, client, monkeypatch,
                                                           caplog):
        """Отказ по лимиту не пишет строку «задачу не удалось завести».

        Ловушка своя же: обёртка вокруг списания и вставки перехватывает и
        `HTTPException` от 429, поэтому штатный ответ «исчерпан часовой бюджет»
        попадал в журнал как ERROR о сбое заведения задачи (увидено в живом
        прогоне инлайн-режима). Для оператора это ложная авария, а настоящая
        поломка тонула в таких строках.
        """
        from app import deps

        email = f"budget-log-{uuid.uuid4().hex[:8]}@example.com"
        assert client.post("/api/register", json={"name": "Журнал", "email": email,
                                                 "password": "Passw0rd!"}) \
            .status_code == 200
        token = client.post("/api/login", json={"email": email,
                                               "password": "Passw0rd!"}
                            ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        monkeypatch.setattr(deps._ai_window, "limit", 0)
        with caplog.at_level(logging.ERROR, logger="app.routers.ai"):
            assert client.post("/api/generate",
                               json={"text": f"заявка {uuid.uuid4().hex}"},
                               headers=headers).status_code == 429
        assert "не удалось завести" not in caplog.text, caplog.text

    def test_scoring_does_not_spend_the_provider_budget(self, client, monkeypatch):
        """Скоринг схемы окно не списывает: провайдера в этом контуре не спрашивают.

        Лимит специально «закрыт»: тратила бы постановка `SCORE` бюджет,
        ответ был бы 429, то есть пользователь упирался бы в часовую планку за
        работу, не стоившую ни одного обращения к модели.
        """
        from app import deps

        email = f"budget-score-{uuid.uuid4().hex[:8]}@example.com"
        assert client.post("/api/register", json={"name": "Скор", "email": email,
                                                 "password": "Passw0rd!"}) \
            .status_code == 200
        token = client.post("/api/login", json={"email": email,
                                               "password": "Passw0rd!"}
                            ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        monkeypatch.setattr(deps._ai_window, "limit", 0)
        response = client.post("/api/evaluate", json={"bpmn_xml": XML},
                               headers=headers)
        assert response.status_code == 202, response.text

    def test_release_raises_the_shared_window_in_a_worker_process(self, monkeypatch):
        """Возврат слота поднимает общее окно сам, даже если процесс его не заводил.

        `init_ai_budget` зовёт только lifespan веб-процесса (app/main.py), а слот
        возвращает воркер: `_refund_unspent` в app/tasks.py. Без ленивого
        переподключения внутри `release_ai_budget` каждый возврат в процессе
        воркера был бы no-op — `_redis_budget` там None, а в локальном окне метки
        нет, потому что списывал её другой процесс. Молчаливая порча ровно в
        compose-топологии, ради которой метка слота и сделана выводимой из пары
        «пользователь + задача».
        """
        from app import deps

        class FakeWindow:
            def __init__(self):
                self.released = []

            async def release(self, key, label):
                self.released.append((key, label))
                return True

            async def close(self):
                pass

        window = FakeWindow()

        async def attach():
            deps._redis_budget = window
            return True

        monkeypatch.setattr(deps, "_redis_budget", None)
        monkeypatch.setattr(deps, "REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(deps, "_budget_next_try", 0.0)
        monkeypatch.setattr(deps, "_budget_retry_in_flight", False)
        monkeypatch.setattr(deps, "_try_attach_redis_budget", attach)

        assert asyncio.run(deps.release_ai_budget(11, task_id="task-11")) is True
        assert window.released == [("11", deps.budget_slot(11, "task-11"))]


class TestRecovery:
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def enqueue_job(self, function, task_id, **kwargs):
            self.calls.append((function, task_id, kwargs))

    def test_stale_running_tasks_are_listed(self, owner):
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        assert task_id in [row[0] for row in task_store.stale_tasks(
            {task_module.QUEUE_LLM: 0})]

    def test_fresh_running_task_is_not_stale(self, owner):
        task_id = make_task(user_id=owner[1], status=task_store.RUNNING)
        mark(task_id, started_at=task_store.utc_now())
        assert task_id not in [row[0] for row in task_store.stale_tasks(
            {task_module.QUEUE_LLM: 3600})]

    def test_recovery_requeues_a_task_with_tries_left(self, owner, monkeypatch):
        """Задача `running` без живого воркера сама в очередь не вернётся: arq
        хранит свою копию, и после падения процесса её может не остаться."""
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.RUNNING)
        redis = self.FakeRedis()
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _seconds: [(task_id, task_module.QUEUE_LLM, 1,
                                              task_store.RUNNING)])
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
                                               exhausted, task_store.RUNNING)])
        asyncio.run(task_module._recover_stale_tasks({"redis": None}))
        task = read(task_id)
        assert (task.status, task.error_code) == (task_store.FAILED, "worker_lost")

    def test_recovery_closes_a_task_nobody_cancelled(self, owner, monkeypatch, caplog):
        """Первая ветка `_closed` в уборке: строка была в `running`, и никто её не
        снимал — значит воркер умер, и задача обязана уйти в `failed` с кодом
        `worker_lost`.

        Без этого перехода опрос клиента не завершается никогда (у незакрытой
        строки `poll_after` ненулевой), а `purge_closed` терминальный статус не
        видит, то есть уборка поднимала бы ту же задачу каждые две минуты до
        бесконечности. Отдельно проверяется, что пометка «закрыта как …» на такой
        задаче НЕ печатается: иначе журнал перестаёт отличать «воркер умер» от
        «человек нажал отмену», а это единственный способ разобрать жалобу.
        """
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.RUNNING,
                            payload={"marker": uuid.uuid4().hex})
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _thresholds: [(task_id, task_module.QUEUE_LLM, 1,
                                              task_store.RUNNING)])
        with caplog.at_level(logging.INFO, logger="app.tasks"):
            asyncio.run(task_module._recover_stale_tasks({"redis": None}))

        task = read(task_id)
        assert (task.status, task.error_code) == (task_store.FAILED, "worker_lost")
        assert "перезапустился" in (task.error or "")
        assert task.finished_at is not None
        assert "закрыта как" not in caplog.text

    def test_recovery_keeps_a_cancelled_task_cancelled(self, owner, monkeypatch,
                                                       caplog):
        """Вторая ветка: переход в `failed` условный, и уборка не имеет права
        перешагнуть через решение человека, пока воркер был мёртв.

        Гонка проверяется в обоих видах, которые она принимает: строку уже закрыли
        другим путём (в базе `cancelled`) и отмену подняли флагом во время
        исполнения (строка ещё открыта, `mark_failed` её не трогает, а `_closed`
        доводит её до `cancelled`). Без этой ветки в истории висел бы «сбой
        воркера» там, где пользователь сам отказался от работы, а `worker_lost` —
        код из `RETRYABLE_ERROR_CODES` фронтенда: отменённую задачу клиент начал бы
        повторять, то есть второй раз оплатил бы вызов, от которого отказались.
        """
        closed = make_task(task_module.GENERATE, user_id=owner[1],
                           status=task_store.CANCELLED,
                           payload={"marker": uuid.uuid4().hex})
        flagged = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.RUNNING,
                            payload={"marker": uuid.uuid4().hex})
        mark(flagged, cancel_requested=True)
        exhausted = task_module.QUEUE_TRIES[task_module.QUEUE_LLM]
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _thresholds: [(closed, task_module.QUEUE_LLM,
                                                  exhausted, task_store.CANCELLED),
                                                 (flagged, task_module.QUEUE_LLM,
                                                  exhausted, task_store.RUNNING)])
        with caplog.at_level(logging.INFO, logger="app.tasks"):
            asyncio.run(task_module._recover_stale_tasks({"redis": None}))

        for task_id in (closed, flagged):
            task = read(task_id)
            assert task.status == task_store.CANCELLED, (task_id, task.status,
                                                          task.error)
            # Статус сохраняется, а не перезаписывается: у отмены нет ошибки.
            assert task.error is None and task.error_code is None
        assert f"Задача {closed} закрыта как cancelled" in caplog.text
        assert f"Задача {flagged} закрыта как cancelled" in caplog.text

    def test_stale_queued_task_is_listed(self, owner):
        """Обход обязан видеть и `queued`: commit строки состоялся, а `enqueue`
        нет — потерянная постановка, о которой не знает ни один воркер, и стартовый
        крюк искал только `running`, поэтому такая задача висела в очереди вечно.

        Порог берётся из `_stale_thresholds()`, а не числом из теста: он выводится
        из таймаута очереди, и записанное от руки значение ниже таймаута хоронило
        бы живые задачи.
        """
        thresholds = task_module._stale_thresholds()
        assert thresholds[task_module.QUEUE_LLM] > task_module.AI_LLM_JOB_TIMEOUT

        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            payload={"marker": uuid.uuid4().hex})
        mark(task_id, created_at=task_store.utc_now()
             - timedelta(seconds=thresholds[task_module.QUEUE_LLM] + 60))
        listed = {row[0]: row for row in task_store.stale_tasks(thresholds)}
        assert task_id in listed
        assert listed[task_id][1] == task_module.QUEUE_LLM
        # Заявку на исполнение никто не выставлял: попыток ноль, и это признак
        # того, что провайдера не спрашивали (по нему возвращается слот бюджета).
        assert listed[task_id][2] == 0

    def test_lost_submission_without_a_broker_is_closed_and_reported(
            self, client, owner):
        """Потерянная постановка при недоступной очереди закрывается ошибкой, и
        опрос отдаёт ровно тот статус, который лёг в базу.

        Проверка сквозная (база → маршрут) намеренно: Redis — только транспорт,
        источник истины по статусу — база, и клиент, который крутил бы `queued`
        вечно, заслуживает терминального отказа с повторяемым кодом вместо
        бесконечного ожидания.
        """
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            payload={"marker": uuid.uuid4().hex})
        mark(task_id, created_at=task_store.utc_now() - timedelta(days=2))
        asyncio.run(task_module._recover_stale_tasks({"redis": None}))

        task = read(task_id)
        assert (task.status, task.error_code) == (task_store.FAILED, "worker_lost")
        body = client.get(f"/api/tasks/{task_id}", headers=owner[0]).json()
        assert (body["status"], body["error_code"]) == (task.status,
                                                        task.error_code)
        assert body["error"] == task.error
        # Закрытая задача больше не зовёт опрашивать.
        assert body["poll_after"] is None

    def test_lost_submission_with_a_broker_goes_back_to_the_queue(
            self, client, owner):
        """Та же строка при живом брокере не хоронится, а ставится в очередь
        новым идентификатором попытки: arq не принимает повторно уже известный
        job, и с прежним идентификатором задача не появилась бы в очереди вовсе.

        Просроченная `queued` остаётся `queued` — заявка на исполнение условная,
        и второй воркер уйдёт в `skipped`, не оплатив второй вызов модели.
        """
        task_id = make_task(task_module.GENERATE, user_id=owner[1],
                            payload={"marker": uuid.uuid4().hex})
        mark(task_id, created_at=task_store.utc_now() - timedelta(days=2))
        redis = self.FakeRedis()
        asyncio.run(task_module._recover_stale_tasks({"redis": redis}))

        calls = [call for call in redis.calls if call[1] == task_id]
        assert len(calls) == 1, (redis.calls, task_id)
        function, _got_id, kwargs = calls[0]
        assert function == "generate_bpmn_task"
        assert kwargs["_queue_name"] == task_module.QUEUE_LLM
        assert kwargs["_job_id"] != task_id
        assert read(task_id).status == task_store.QUEUED

        body = client.get(f"/api/tasks/{task_id}", headers=owner[0]).json()
        assert body["status"] == task_store.QUEUED
        assert body["poll_after"] is not None, (
            "задача вернулась в очередь, а клиенту не сказали, когда опрашивать")

    def test_sweep_runs_on_the_schedule_of_both_workers(self):
        """Периодическая уборка обязана быть в расписании обоих воркеров.

        Разовый крюк старта (`_worker_startup`) её не покрывает: потерянную
        постановку стартовавший процесс мог и не застать, а `purge_closed` без
        расписания не сработает никогда — в `result` лежит полный XML схемы, и
        таблица росла бы бесконечно. Интервал берётся из самого короткого таймаута
        очереди, поэтому проверка сверяет число, а не записывает его от руки.
        `unique=True` — второе: два процесса кладут job один раз на слот, иначе
        уборка читала бы базу дважды за минуту и боролась бы за одну и ту же
        строку.
        """
        minutes = max(1, task_module.AI_LOCAL_JOB_TIMEOUT // 60)
        for settings in (task_module.LLMWorkerSettings,
                         task_module.LocalWorkerSettings):
            job, = settings.cron_schedule
            assert job.coroutine is task_module._sweep_contour
            assert job.unique is True
            assert sorted(job.minute) == list(range(0, 60, minutes))
            assert job.second == 0
            # Интервал обязан быть короче порога потерянности, иначе просроченная
            # задача ждал бы уборки дольше, чем сама считается потерянной.
            assert minutes * 60 < task_module._stale_thresholds()[settings.queue_name]

    def test_sweep_purges_old_closed_rows_and_keeps_the_rest(self, owner,
                                                             monkeypatch):
        """`_sweep_contour` — не только восстановление, но и срок хранения: в
        `result` лежит полный XML схемы, и без чистки таблица росла бы
        бесконечно.

        Срезаются только закрытые строки старше срока. Открытую задачу удаление
        лишило бы результата, который клиент ещё ждёт, а свежую историю —
        объяснения того, что произошло: уборка держится парой «потерянные задачи и
        просроченная история», и отсутствие второй половины значило бы, что
        расписание молчит.
        """
        monkeypatch.setattr(task_module.task_store, "stale_tasks",
                            lambda _thresholds: [])
        expired = make_task(task_module.GENERATE, user_id=owner[1],
                            status=task_store.DONE,
                            payload={"marker": uuid.uuid4().hex})
        mark(expired, finished_at=task_store.utc_now()
             - timedelta(seconds=task_module.TASK_RETENTION_SECONDS + 60))
        fresh = make_task(task_module.GENERATE, user_id=owner[1],
                          status=task_store.DONE,
                          payload={"marker": uuid.uuid4().hex})
        mark(fresh, finished_at=task_store.utc_now())
        # Старая, но ещё не закрытая строка: её чистка не трогает.
        open_row = make_task(task_module.GENERATE, user_id=owner[1],
                             status=task_store.RUNNING,
                             payload={"marker": uuid.uuid4().hex})
        mark(open_row, created_at=task_store.utc_now() - timedelta(days=30))

        asyncio.run(task_module._sweep_contour({"redis": None}))

        assert not exists(expired), "просроченная закрытая строка осталась в базе"
        assert exists(fresh), "свежая история ушла вместе с просроченной"
        assert exists(open_row), "незакрытая задача удалена"

    def test_sweep_survives_a_database_failure(self, monkeypatch, caplog):
        """Сбой базы на запросе списка не топит процесс воркера.

        Увидено живым `python -m arq app.tasks.LocalWorkerSettings` на
        немигрированной базе: `OperationalError` из `stale_tasks` долетал из
        стартового крюка до `Worker.main_startup`, процесс умирал, не успев
        заявить отметку живости, — то есть контейнер падал в петлю рестартов из-за
        чужой недоступности. Отдельные строки обхода изолированы, а сам запрос
        списка оставался за изоляцией. Проверка на двух входах: стартовый крюк
        зовёт обход напрямую, cron зовёт его через `_sweep_contour`.
        """
        def boom(_thresholds):
            raise RuntimeError("no such table: ai_tasks")

        monkeypatch.setattr(task_store, "stale_tasks", boom)
        with caplog.at_level(logging.ERROR, logger="app.tasks"):
            asyncio.run(task_module._recover_stale_tasks({"redis": None}))
            asyncio.run(task_module._sweep_contour({"redis": None}))
        assert "Список потерянных задач не получен" in caplog.text


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
                            lambda text, **kw: {"status": "success", "bpmn": XML,
                                          "structure": {}, "notes": []})
        key = f"idem-{uuid.uuid4().hex}"
        body = {"text": f"заявка {uuid.uuid4().hex}", "idempotency_key": key}
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
                            lambda text, **kw: {"status": "success", "bpmn": XML,
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


class TestBudgetAttachCleanup:
    """Неудачная проба общего окна не оставляет висящий клиент Redis.

    `_try_attach_redis_budget` создаёт `_RedisWindow` ДО `ping`, а спираль
    переподключения бьёт каждые 5 с, потом 10, 20 … до 300 с. Если соединение
    поднялось, а команда упала, пул держит сокет: в долгих процессах это растущее
    число дескрипторов, а `_detach_redis_budget` ровно такой случай и закрывает.
    """

    @staticmethod
    def _fresh(monkeypatch, window_cls):
        from app import deps

        monkeypatch.setattr(deps, "_RedisWindow", window_cls)
        monkeypatch.setattr(deps, "REDIS_URL", "redis://cache:6379/0")
        monkeypatch.setattr(deps, "_redis_budget", None)
        monkeypatch.setattr(deps, "_budget_degradation_logged", False)
        monkeypatch.setattr(deps, "_budget_retry_attempts", 0)
        monkeypatch.setattr(deps, "_budget_next_try", 0.0)
        return deps

    def test_failed_ping_closes_the_candidate(self, monkeypatch):
        from app import deps

        closed = []

        class Deaf:
            limit = deps.AI_REQUESTS_PER_HOUR

            async def ping(self):
                raise OSError("сервер лёг между accept и ответом")

            async def close(self):
                closed.append(True)

        fresh = self._fresh(monkeypatch, lambda url, limit: Deaf())
        assert asyncio.run(fresh._try_attach_redis_budget()) is False
        assert closed == [True]

    def test_constructor_failure_does_not_explode(self, monkeypatch):
        """Разбор адреса падает раньше клиента: закрывать нечего, и вторая ошибка
        поверх первой спрятала бы настоящую причину."""
        from app import deps

        def boom(url, limit):
            raise ValueError("неверный REDIS_URL")

        fresh = self._fresh(monkeypatch, boom)
        assert asyncio.run(fresh._try_attach_redis_budget()) is False

    def test_close_failure_keeps_the_degradation(self, monkeypatch, caplog):
        """Сервер не даёт закрыться корректно — деградация всё равно объявлена,
        а не тонет в исключении из ветки отказа."""
        from app import deps

        class Rude:
            limit = deps.AI_REQUESTS_PER_HOUR

            async def ping(self):
                raise OSError("сервер молчит")

            async def close(self):
                raise OSError("сокет мёртв")

        fresh = self._fresh(monkeypatch, lambda url, limit: Rude())
        with caplog.at_level("ERROR", logger="app.deps"):
            assert asyncio.run(fresh._try_attach_redis_budget()) is False
        assert "БЮДЖЕТ ИИ ДЕГРАДИРОВАЛ" in caplog.text

import os
import sys
import tempfile
import time as _time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Окружение тестового прогона: задаётся один раз и до импорта пакета app.
#
# app.config читает переменные на импорте, а app.db создаёт engine один раз на
# процесс, так что все модули обязаны видеть одну базу (раньше каждый переопределял
# DATABASE_URL у себя, и второй URL просто игнорировался). Рабочая база разработчика
# не затрагивается: по умолчанию это временный SQLite-файл. Прогон на PostgreSQL
# включается явной TEST_DATABASE_URL на отдельной (лучше чистой) базе.
DATABASE_URL = os.getenv("TEST_DATABASE_URL") or (
    f"sqlite:///{(Path(tempfile.mkdtemp(prefix='bpmn-test-')) / 'test.db').as_posix()}"
)
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SKIP_LLM_INIT"] = "1"
# Почта заведомо недоступна: проверяем деградацию отправки, а не реальные письма.
os.environ["MAIL_SERVER"] = "127.0.0.1"
os.environ["MAIL_PORT"] = "1"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Схема из миграций Alembic — один раз на прогон, как при реальном запуске.

    На PostgreSQL здесь же проверяется, что миграции применимы к базе, где уже
    есть данные прошлых прогонов.
    """
    from app.startup import prepare_database

    prepare_database()
    yield


class FakeRedisClient:
    """In-process замена синхронного клиента Redis.

    Кэш и бюджет проверяются по контракту слоя (что кладётся, что читается и
    когда это считается промахом), а не по настоящему серверу: живой Redis с
    настоящим подключением стоит отдельным тестом, чтобы основной прогон
    оставался герметичным и не требовал Docker.
    """

    def __init__(self, fail=False):
        self.store = {}
        self.expiry = {}
        self.fail = fail
        self.closed = False

    def _alive(self, key):
        deadline = self.expiry.get(key)
        return deadline is None or deadline > _time.time()

    def _guard(self):
        if self.fail:
            raise ConnectionError("redis не отвечает")

    def get(self, key):
        self._guard()
        if key not in self.store or not self._alive(key):
            return None
        return self.store[key]

    def set(self, key, value, ex=None):
        self._guard()
        self.store[key] = value
        self.expiry[key] = _time.time() + ex if ex else None
        return True

    def delete(self, key):
        self._guard()
        self.store.pop(key, None)
        self.expiry.pop(key, None)
        return 1

    def hincrby(self, key, field, amount=1):
        self._guard()
        hash_ = self.store.setdefault(key, {})
        hash_[field] = int(hash_.get(field, 0)) + amount
        return hash_[field]

    def hgetall(self, key):
        self._guard()
        return dict(self.store.get(key) or {})

    def ping(self):
        self._guard()
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_cache(monkeypatch):
    """Настоящий `LLMCache`, но поверх словаря вместо сервера.

    Подменяется `redis.Redis.from_url`, а не приватное поле клиента: проверка
    идёт ровно тем путём построения, которым он строится в приложении.
    """
    from core import llm_cache

    client = FakeRedisClient()

    class _Factory:
        @staticmethod
        def from_url(*_args, **_kwargs):
            return client

    monkeypatch.setattr(llm_cache, "redis", SimpleNamespace(Redis=_Factory))
    return llm_cache.LLMCache("redis://fake:6379/1", namespace="bpmn"), client


XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


@pytest.fixture
def single_pool_xml() -> str:
    """Старт → «Собрать заказ» → шлюз → две ветки → задачи → энда.

    Форма с пространством имён по умолчанию — ровно то, что отдаёт bpmn-js."""
    return XML_DECL + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">
  <process id="Process_order" name="Заказ" isExecutable="true">
    <startEvent id="Start_1" name="Заказ создан">
      <outgoing>F1</outgoing>
    </startEvent>
    <sequenceFlow id="F1" sourceRef="Start_1" targetRef="T_collect"/>
    <userTask id="T_collect" name="Собрать заказ">
      <incoming>F1</incoming>
      <outgoing>F2</outgoing>
    </userTask>
    <sequenceFlow id="F2" sourceRef="T_collect" targetRef="G_paid"/>
    <exclusiveGateway id="G_paid" name="Оплачен?">
      <incoming>F2</incoming>
      <outgoing>F3</outgoing>
      <outgoing>F4</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F3" sourceRef="G_paid" targetRef="T_ship">
      <conditionExpression>paid == true</conditionExpression>
    </sequenceFlow>
    <userTask id="T_ship" name="Отгрузить товар">
      <incoming>F3</incoming>
      <outgoing>F5</outgoing>
    </userTask>
    <sequenceFlow id="F5" sourceRef="T_ship" targetRef="End_ok"/>
    <endEvent id="End_ok" name="Заказ выдан">
      <incoming>F5</incoming>
    </endEvent>
    <sequenceFlow id="F4" sourceRef="G_paid" targetRef="T_cancel">
      <conditionExpression>paid == false</conditionExpression>
    </sequenceFlow>
    <userTask id="T_cancel" name="Отменить заказ">
      <incoming>F4</incoming>
      <outgoing>F6</outgoing>
    </userTask>
    <sequenceFlow id="F6" sourceRef="T_cancel" targetRef="End_cancel"/>
    <endEvent id="End_cancel" name="Заказ отменён">
      <incoming>F6</incoming>
    </endEvent>
  </process>
</definitions>"""


@pytest.fixture
def two_pool_xml() -> str:
    """Коллаборация из двух пулов с messageFlow между ними."""
    return XML_DECL + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_2" targetNamespace="http://bpmn.io/schema/bpmn">
  <collaboration id="Collaboration_1">
    <participant id="Pool_client" name="Клиент" processRef="Process_client"/>
    <participant id="Pool_shop" name="Магазин" processRef="Process_shop"/>
    <messageFlow id="MF1" sourceRef="C_request" targetRef="S_accept"/>
  </collaboration>
  <process id="Process_client" name="Клиент" isExecutable="true">
    <startEvent id="C_start" name="Нужен товар"/>
    <sequenceFlow id="CF1" sourceRef="C_start" targetRef="C_request"/>
    <userTask id="C_request" name="Оформить запрос"/>
    <sequenceFlow id="CF2" sourceRef="C_request" targetRef="C_end"/>
    <endEvent id="C_end" name="Товар получен"/>
  </process>
  <process id="Process_shop" name="Магазин" isExecutable="true">
    <startEvent id="S_start" name="Запрос получен"/>
    <sequenceFlow id="SF1" sourceRef="S_start" targetRef="S_accept"/>
    <serviceTask id="S_accept" name="Принять запрос"/>
    <sequenceFlow id="SF2" sourceRef="S_accept" targetRef="S_end"/>
    <endEvent id="S_end" name="Заказ собран"/>
  </process>
</definitions>"""

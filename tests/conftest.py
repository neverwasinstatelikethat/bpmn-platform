import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

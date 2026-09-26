"""Детерминированный аплайер операций: переподвеска потоков, каскадное удаление,
пропуск невалидных операций с причиной и семантическая починка.

Это та часть контура улучшения, которая раньше отдавала 500: LLM возвращал
произвольный XML, а валидатор требовал полную DI-раскладку. Теперь модель
возвращает операции, и применяем их мы."""
import inspect
import re
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from core.bpmn_edits import (
    BPMN_NS,
    DEFAULT_TIMER_DURATION,
    FLOW_NODE_TAGS,
    INVENTORY_MAX_ELEMENTS,
    NOOP_NOTE_MARKERS,
    OP_ALIASES,
    OP_SPEC,
    POOL_EMPTY_NOTE_MARKERS,
    REJECT_HINT,
    UNROUTED_NOTE_MARKERS,
    XML_DECLARATION,
    apply_and_guarantee,
    apply_operations,
    build_inventory,
    merge_notes,
    rollback_stranded,
    validate_and_repair,
    _HANDLERS,
)

NS = {"bpmn": BPMN_NS}


def _root(xml_text):
    return ET.fromstring(xml_text)


def _by_id(xml_text, elem_id):
    return _root(xml_text).find(f".//*[@id='{elem_id}']")


def _flow(root, flow_id):
    return root.find(f".//bpmn:sequenceFlow[@id='{flow_id}']", NS)


def _refs(elem, tag):
    return [ref.text for ref in elem.findall(f"bpmn:{tag}", NS)]


def _tag(elem):
    return elem.tag.rsplit("}", 1)[-1]


def _child_tags(elem):
    return [_tag(child) for child in elem if isinstance(child.tag, str)]


def _order(elem, first, second):
    """True, если тег first встречается в детях elem раньше тега second."""
    tags = _child_tags(elem)
    return first in tags and second in tags and tags.index(first) < tags.index(second)


def _dangling(xml_text):
    """id flow-узлов без единого входящего или исходящего потока.

    Граничные события не считаем: они по определению без рёбер, их «висячесть»
    выражается через attachedToRef."""
    root = _root(xml_text)
    has_incoming, has_outgoing = set(), set()
    for flow in root.findall(".//bpmn:sequenceFlow", NS):
        has_outgoing.add(flow.get("sourceRef"))
        has_incoming.add(flow.get("targetRef"))
    dangling = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or _tag(elem) not in FLOW_NODE_TAGS:
            continue
        if elem.get("attachedToRef"):
            continue
        elem_id = elem.get("id")
        if elem_id not in has_incoming and elem_id not in has_outgoing:
            dangling.append(elem_id)
    return dangling


def _lanes(xml_text):
    return _root(xml_text).findall(".//bpmn:lane", NS)


def _lane_refs(lane):
    return [ref.text for ref in lane.findall("bpmn:flowNodeRef", NS)]


def _process(root, process_id):
    return root.find(f".//bpmn:process[@id='{process_id}']", NS)


LANES_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_3">
  <process id="Process_order" name="Заказ" isExecutable="true">
    <documentation>Пул обработки заказов</documentation>
    <startEvent id="Start_1" name="Заказ создан">
      <outgoing>F1</outgoing>
    </startEvent>
    <sequenceFlow id="F1" sourceRef="Start_1" targetRef="T_collect"/>
    <userTask id="T_collect" name="Собрать заказ">
      <incoming>F1</incoming>
      <outgoing>F2</outgoing>
    </userTask>
    <sequenceFlow id="F2" sourceRef="T_collect" targetRef="End_ok"/>
    <endEvent id="End_ok" name="Заказ выдан">
      <incoming>F2</incoming>
    </endEvent>
  </process>
</definitions>"""


NESTED_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_4">
  <collaboration id="Collaboration_4">
    <participant id="Pool_outer" name="Внешний" processRef="Process_outer"/>
    <participant id="Pool_side" name="Смежный" processRef="Process_side"/>
  </collaboration>
  <process id="Process_outer" name="Внешний" isExecutable="true">
    <startEvent id="O_start" name="Старт">
      <outgoing>OF1</outgoing>
    </startEvent>
    <sequenceFlow id="OF1" sourceRef="O_start" targetRef="O_sub"/>
    <subProcess id="O_sub" name="Подпроцесс">
      <incoming>OF1</incoming>
      <outgoing>OF2</outgoing>
      <startEvent id="I_start" name="Вход подпроцесса">
        <outgoing>IF1</outgoing>
      </startEvent>
      <sequenceFlow id="IF1" sourceRef="I_start" targetRef="I_first"/>
      <userTask id="I_first" name="Первый шаг">
        <incoming>IF1</incoming>
        <outgoing>IF2</outgoing>
      </userTask>
      <sequenceFlow id="IF2" sourceRef="I_first" targetRef="I_second"/>
      <userTask id="I_second" name="Второй шаг">
        <incoming>IF2</incoming>
      </userTask>
    </subProcess>
    <sequenceFlow id="OF2" sourceRef="O_sub" targetRef="O_end"/>
    <endEvent id="O_end" name="Финиш">
      <incoming>OF2</incoming>
    </endEvent>
  </process>
  <process id="Process_side" name="Смежный" isExecutable="true">
    <startEvent id="S_start" name="Сигнал">
      <outgoing>SF1</outgoing>
    </startEvent>
    <sequenceFlow id="SF1" sourceRef="S_start" targetRef="S_task"/>
    <userTask id="S_task" name="Чужая задача">
      <incoming>SF1</incoming>
      <outgoing>SF2</outgoing>
    </userTask>
    <sequenceFlow id="SF2" sourceRef="S_task" targetRef="S_end"/>
    <endEvent id="S_end" name="Конец смежного">
      <incoming>SF2</incoming>
    </endEvent>
  </process>
</definitions>"""


def _cycle_xml():
    """Процесс без старта и энда, где у каждого узла есть и вход, и выход:
    кандидата для нового события нет, и починка обязана честно сказать почему."""
    return XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="Кольцо" isExecutable="true">
    <userTask id="T1" name="Шаг один"/>
    <userTask id="T2" name="Шаг два"/>
    <sequenceFlow id="F12" sourceRef="T1" targetRef="T2"/>
    <sequenceFlow id="F21" sourceRef="T2" targetRef="T1"/>
  </process>
</definitions>"""


def _wide_xml(nodes=250):
    """Схема из `nodes` задач и ~двух потоков на узел — размер пользовательского
    XML под лимитом в 1 МБ символов."""
    half = nodes // 2
    flows = ['<sequenceFlow id="F_in" sourceRef="S0" targetRef="T0"/>']
    flows += [f'<sequenceFlow id="F_c{i}" sourceRef="T{i}" targetRef="T{i + 1}"/>'
              for i in range(nodes - 1)]
    flows.append(f'<sequenceFlow id="F_out" sourceRef="T{nodes - 1}" targetRef="E0"/>')
    flows += [f'<sequenceFlow id="F_x{i}" sourceRef="T{i}" targetRef="T{(i + half) % nodes}"/>'
              for i in range(nodes)]
    body = "".join(f'<userTask id="T{i}" name="Шаг {i}"/>' for i in range(nodes))
    return (XML_DECLARATION
            + '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">'
            + '<process id="Process_wide" name="Широкий" isExecutable="true">'
            + '<startEvent id="S0" name="Старт"/><endEvent id="E0" name="Финиш"/>'
            + body + "".join(flows) + "</process></definitions>")


def _batch_of_25(nodes=250):
    """25 разнородных операций: вставки, удаления, переносы, дорожки, события."""
    return (
        [{"op": "add_task", "id": f"new_T{k}", "name": f"Новый шаг {k}",
          "task_type": "userTask", "after": f"T{(k * 17 + 7) % nodes}"} for k in range(9)]
        + [{"op": "delete", "id": f"T{k * 31 % nodes}"} for k in range(3)]
        + [{"op": "disconnect", "flow": f"F_c{50 + k}"} for k in range(3)]
        + [{"op": "add_documentation", "id": f"T{60 + k}", "text": "Описание шага"}
           for k in range(2)]
        + [{"op": "add_lane", "id": f"new_Lane{k}", "name": f"Дорожка {k}",
            "participant": "Широкий"} for k in range(2)]
        + [{"op": "move_to_lane", "id": f"T{70 + k}", "lane": "new_Lane0"} for k in range(2)]
        + [{"op": "add_boundary_event", "id": f"new_BE{k}", "attached_to": f"T{80 + k}",
            "event_type": "timer", "name": f"Таймер {k}"} for k in range(2)]
        # Граничное событие без ветки обработки откатывается, поэтому пакет
        # обязан вести его в существующий шаг — иначе метрика линейности
        # считалась бы на пакете, который ничего не применил.
        + [{"op": "connect", "source": f"new_BE{k}", "target": f"T{90 + k}"}
           for k in range(2)]
        + [{"op": "connect", "source": f"T{200}", "target": f"T{203}"}]
    )


# ---------------------------------------------------------------------------
# Документация элемента
# ---------------------------------------------------------------------------

class TestDocumentation:
    OPS = [{"op": "add_documentation", "id": "T_collect",
            "text": "Собираем со склада, проверяем сроки"}]

    def test_documentation_is_written_into_the_element(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self.OPS)
        assert report["status"] == "success"
        doc = _by_id(out, "T_collect").find("bpmn:documentation", NS)
        assert doc.text == "Собираем со склада, проверяем сроки"

    def test_documentation_precedes_flow_references(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, self.OPS)
        task = _by_id(out, "T_collect")
        assert _order(task, "documentation", "incoming")
        assert _order(task, "documentation", "outgoing")

    def test_repair_keeps_documentation_before_rebuilt_references(
            self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, self.OPS)
        repaired, notes = validate_and_repair(out)
        assert notes == []
        task = _by_id(repaired, "T_collect")
        assert _child_tags(task)[0] == "documentation"
        assert _refs(task, "incoming") == ["F1"]
        assert _refs(task, "outgoing") == ["F2"]
        assert build_inventory(repaired)["flows"] == build_inventory(out)["flows"]

    def test_second_documentation_stays_next_to_the_first(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self.OPS + [
            {"op": "add_documentation", "id": "T_collect", "text": "Ответственный — кладовщик"},
        ])
        assert report["status"] == "success"
        task = _by_id(out, "T_collect")
        assert _child_tags(task) == ["documentation", "documentation", "incoming", "outgoing"]

    def test_missing_text_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml,
                                     [{"op": "add_documentation", "id": "T_collect"}])
        assert report["skipped"][0]["reason"] == "не задан текст документации"
        assert report["skipped"][0]["hint"] == "укажите text"

    def test_unknown_element_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_documentation", "id": "no_such_id", "text": "Текст"},
        ])
        assert report["skipped"][0]["reason"] == "элемент не найден"

    def test_documentation_aimed_at_a_flow_says_so(self, single_pool_xml):
        """`F1` есть в инвентаре — как поток. Отказ «элемент не найден» модель
        читает как «id выдумана» и в корректирующий повтор приносит тот же id
        (прогон #49, production_incident r0: один и тот же пропуск в плане и в
        повторе). Причина обязана называть, чем является id."""
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_documentation", "id": "F1", "text": "Текст"},
        ])
        skip = report["skipped"][0]
        assert "поток" in skip["reason"]
        assert "узлу" in skip["hint"]

    def test_broken_documentation_does_not_break_the_batch(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_documentation", "id": "no_such_id", "text": "Текст"},
            {"op": "add_documentation", "id": "T_collect", "text": "Нужный текст"},
        ])
        assert report["status"] == "partial"
        assert [a["op"] for a in report["applied"]] == ["add_documentation"]
        assert _by_id(out, "T_collect").find("bpmn:documentation", NS).text == "Нужный текст"


# ---------------------------------------------------------------------------
# Дорожки
# ---------------------------------------------------------------------------

class TestLanes:
    def test_lane_set_becomes_first_content_of_the_process(self):
        out, report = apply_operations(LANES_XML, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
        ])
        assert report["status"] == "success"
        process = _process(_root(out), "Process_order")
        # документация процесса идёт по схеме BPMN раньше laneSet, laneSet —
        # раньше flow-элементов (так же пишет bpmn-js)
        assert _child_tags(process)[:2] == ["documentation", "laneSet"]
        assert _order(process, "laneSet", "userTask")
        lane = _by_id(out, "new_Lane_1")
        assert lane.get("name") == "Кладовщик"

    def test_second_lane_joins_the_existing_lane_set(self):
        out, report = apply_operations(LANES_XML, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Process_order"},
            {"op": "add_lane", "id": "new_Lane_2", "name": "Курьер",
             "participant": "Process_order"},
        ])
        assert report["status"] == "success"
        root = _root(out)
        assert len(root.findall(".//bpmn:laneSet", NS)) == 1
        assert [_lane_refs(lane) for lane in _lanes(out)] == [[], []]
        assert _child_tags(root.find(".//bpmn:laneSet", NS)) == ["lane", "lane"]

    def test_lane_lands_in_the_named_pool(self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_pack", "name": "Сборка",
             "participant": "Магазин"},
        ])
        assert report["status"] == "success"
        shop = _process(_root(out), "Process_shop")
        assert _child_tags(shop)[0] == "laneSet"
        assert shop.find("bpmn:laneSet/bpmn:lane", NS).get("id") == "new_Lane_pack"

    def test_lane_pool_derived_from_move_in_same_batch(self, two_pool_xml):
        """Модель опустила participant у add_lane, но назвала элемент переноса.
        Пул при этом определён схемой: дорожка обязана быть в пуле элемента,
        иначе следующая же операция отвергается как «дорожка другому пулу»."""
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_L6", "name": "Контроль"},
            {"op": "move_to_lane", "id": "S_accept", "lane": "new_L6"},
        ])
        assert report["status"] == "success", report["skipped"]
        shop = _process(_root(out), "Process_shop")
        lane_set = shop.find("bpmn:laneSet", NS)
        assert [lane.get("id") for lane in lane_set.findall("bpmn:lane", NS)] == ["new_L6"]
        assert _lane_refs(lane_set.find("bpmn:lane", NS)) == ["S_accept"]
        # Вывод виден в отчёте: правка плана, а не молчаливая догадка.
        assert "пул 'Магазин' взят из элемента 'S_accept'" in report["applied"][0]["note"]
        # Расходился бы по всей схеме — у второго пула дорожек бы прибавилось.
        client = _process(_root(out), "Process_client")
        assert client.find("bpmn:laneSet", NS) is None

    def test_lane_without_pool_and_without_move_is_skipped(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_L7", "name": "Сирота"},
        ])
        assert report["skipped"][0]["reason"] == "пул не определён"

    def test_move_to_lane_only_references_the_element(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
            {"op": "move_to_lane", "id": "T_collect", "lane": "new_Lane_1"},
        ])
        assert report["status"] == "success"
        root = _root(out)
        lane = root.find(".//bpmn:lane", NS)
        assert _lane_refs(lane) == ["T_collect"]
        # сам элемент остаётся ребёнком процесса — так хранит bpmn-js
        assert "T_collect" in [child.get("id") for child in _process(root, "Process_order")]

    def test_move_to_lane_accepts_the_lane_name(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
            {"op": "move_to_lane", "id": "T_collect", "lane": "Кладовщик"},
        ])
        assert report["status"] == "success"
        assert _lane_refs(_root(out).find(".//bpmn:lane", NS)) == ["T_collect"]

    def test_repeating_the_move_relocates_the_reference(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
            {"op": "add_lane", "id": "new_Lane_2", "name": "Курьер",
             "participant": "Заказ"},
            {"op": "move_to_lane", "id": "T_collect", "lane": "new_Lane_1"},
            {"op": "move_to_lane", "id": "T_collect", "lane": "new_Lane_2"},
        ])
        assert report["status"] == "success"
        refs = {lane.get("id"): _lane_refs(lane) for lane in _lanes(out)}
        assert refs == {"new_Lane_1": [], "new_Lane_2": ["T_collect"]}

    def test_inventory_still_reads_a_laned_scheme(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
            {"op": "move_to_lane", "id": "T_collect", "lane": "new_Lane_1"},
        ])
        repaired, notes = validate_and_repair(out)
        assert notes == []
        inventory = build_inventory(repaired)
        assert {e["id"] for e in inventory["elements"]} >= {"Start_1", "T_collect", "End_ok"}
        assert {f["id"] for f in inventory["flows"]} == {"F1", "F2", "F3", "F4", "F5", "F6"}

    def test_created_lane_is_visible_to_the_planner(self, single_pool_xml):
        """move_to_lane целиится в id или имя дорожки — значит, инвентарь обязан
        их показывать, иначе модель выдумывает несуществующие дорожки."""
        assert build_inventory(single_pool_xml)["lanes"] == []
        out, _ = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Кладовщик",
             "participant": "Заказ"},
        ])
        assert build_inventory(out)["lanes"] == [
            {"id": "new_Lane_1", "name": "Кладовщик", "participant": "Заказ"},
        ]

    def test_lane_without_id_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "name": "Без id", "participant": "Заказ"},
        ])
        assert report["skipped"][0]["reason"] == "не задан id нового элемента"

    def test_taken_lane_id_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Первая", "participant": "Заказ"},
            {"op": "add_lane", "id": "new_Lane_1", "name": "Вторая", "participant": "Заказ"},
        ])
        assert report["status"] == "partial"
        assert "уже занят" in report["skipped"][0]["reason"]

    def test_lane_id_cannot_be_reused_by_another_element(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Первая", "participant": "Заказ"},
            {"op": "add_task", "id": "new_Lane_1", "name": "Дубликат",
             "participant": "Заказ"},
        ])
        assert report["status"] == "partial"
        assert "уже занят" in report["skipped"][0]["reason"]

    def test_lane_without_name_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "participant": "Заказ"},
        ])
        assert report["skipped"][0]["reason"] == "не задано имя дорожки"

    def test_unknown_pool_for_lane_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_1", "name": "Дорожка",
             "participant": "Склад"},
        ])
        assert report["skipped"][0]["reason"] == "пул не определён"

    def test_move_to_unknown_lane_points_at_add_lane(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "move_to_lane", "id": "T_collect", "lane": "new_Lane_ghost"},
        ])
        assert report["skipped"][0]["reason"] == "дорожка 'new_Lane_ghost' не найдена"
        assert "add_lane" in report["skipped"][0]["hint"]

    def test_move_without_lane_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{"op": "move_to_lane", "id": "T_collect"}])
        assert report["skipped"][0]["reason"] == "не задана дорожка"

    def test_move_to_lane_of_another_pool_is_skipped(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_shop", "name": "Сборка",
             "participant": "Магазин"},
            {"op": "move_to_lane", "id": "C_request", "lane": "new_Lane_shop"},
        ])
        assert report["status"] == "partial"
        assert report["skipped"][0]["reason"] == "дорожка принадлежит другому пулу"

    def test_lane_pool_conflict_hint_is_copy_pasteable(self, two_pool_xml):
        """Отказ «дорожка другому пулу» был самым частым живым пропуском дня
        (12 строк в прогоне #55+), и лечился он не умной подсказкой: текст
        «указывайте дорожку того же пула» не называет ни владельца дорожки, ни
        того, что модель может ответить. Повтор приносил ровно ту же операцию.
        Подсказка обязана назвать оба пула и дать форму, которую можно
        скопировать: перенос в дорожку пула элемента — либо дорожка нужного пула,
        созданная `add_lane`."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_shop", "name": "Сборка",
             "participant": "Магазин"},
            {"op": "move_to_lane", "id": "C_request", "lane": "new_Lane_shop"},
        ])
        hint = report["skipped"][0]["hint"]
        assert "Магазин" in hint, "не назван пул-владелец дорожки"
        assert "Клиент" in hint, "не назван пул самого элемента"
        assert "new_Lane_shop" in hint, "не названа дорожка, о которой речь"
        assert "add_lane" in hint and "participant=" in hint

    def test_add_task_lane_pool_conflict_hint_offers_the_existing_lane(
            self, two_pool_xml):
        """У `add_task` та же ловушка: модель просит дорожку чужого пула и
        получает отказ без альтернативы. Если в пуле названного участника
        дорожки нет, подсказка обязана сказать это прямо и назвать `add_lane`
        с этим пулом — иначе повтор снова приведёт к чужой дорожке."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_shop", "name": "Сборка",
             "participant": "Магазин"},
            {"op": "add_task", "id": "new_C_check", "name": "Проверка",
             "participant": "Клиент", "lane": "new_Lane_shop", "after": "C_request"},
        ])
        assert report["skipped"][0]["reason"] == "дорожка принадлежит другому пулу"
        hint = report["skipped"][0]["hint"]
        assert "Клиент" in hint and "Магазин" in hint
        assert "add_lane" in hint and 'participant="Клиент"' in hint

    def test_lane_conflict_hint_lists_a_lane_the_model_can_use(self, two_pool_xml):
        """Если в пуле элемента дорожки уже есть, подсказка обязана их назвать:
        «возьмите id из инвентаря» без имён — это снова приказ угадывать."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_lane", "id": "new_Lane_client", "name": "Продавец",
             "participant": "Клиент"},
            {"op": "add_lane", "id": "new_Lane_shop", "name": "Сборка",
             "participant": "Магазин"},
            {"op": "move_to_lane", "id": "C_request", "lane": "new_Lane_shop"},
        ])
        hint = report["skipped"][0]["hint"]
        assert "Продавец" in hint, "не названа законная дорожка того же пула"

    def test_move_unknown_element_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "move_to_lane", "id": "no_such_id", "lane": "new_Lane_1"},
        ])
        assert report["skipped"][0]["reason"] == "элемент не найден"


# ---------------------------------------------------------------------------
# Порядок операций внутри пакета
# ---------------------------------------------------------------------------

class TestPackageOrder:
    """Пакет читают по зависимостям, а не по тому порядку, в котором модель их
    перечислила. Живые прогоны улучшения теряли на этом половину пакета:
    `add_boundary_event` с `to` на шаг, создаваемый следующей операцией,
    отвергался («цель исхода не найдена»), а следом падала и вся ветка — при
    полностью корректном намерении модели."""

    def test_boundary_event_waits_for_the_handler_of_the_package(
            self, two_pool_xml):
        """Ровно отказ живого прогона: `add_boundary_event` с `to` на шаг,
        который пакет создаёт следующей операцией, отвергался и уносил с собой
        всю ветку таймера."""
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_boundary_event", "id": "new_T2",
             "name": "Доставка дольше суток", "attached_to": "S_accept",
             "event_type": "timer", "to": "new_A8"},
            {"op": "add_task", "id": "new_A8", "name": "Эскалировать руководителю",
             "participant": "Магазин", "after": "S_accept"},
        ])
        assert report["skipped"] == [], report["skipped"]
        assert [a["op"] for a in report["applied"]] == ["add_task",
                                                        "add_boundary_event"]
        # Отложенная правка видна в отчёте: порядок изменил код, и это не молча.
        assert "new_A8" in report["applied"][1]["note"]
        outlet = [f for f in _root(out).findall(".//bpmn:sequenceFlow", NS)
                  if f.get("sourceRef") == "new_T2"]
        assert [f.get("targetRef") for f in outlet] == ["new_A8"]

    def test_node_waits_for_the_lane_created_later(self, two_pool_xml):
        """Подсказка аплайера — «создайте дорожку операцией add_lane». Если её
        слушают и ставят add_lane второй операцией, ветка не имеет права
        отваливаться."""
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_A6", "name": "Сообщить подразделениям",
             "participant": "Магазин", "after": "S_accept", "lane": "new_L1"},
            {"op": "add_lane", "id": "new_L1", "name": "Сервис-деск",
             "participant": "Магазин"},
        ])
        assert report["skipped"] == [], report["skipped"]
        assert [a["op"] for a in report["applied"]] == ["add_lane", "add_task"]
        shop = _process(_root(out), "Process_shop")
        assert _lane_refs(shop.find(".//bpmn:laneSet/bpmn:lane", NS)) == ["new_A6"]

    def test_dependency_the_package_never_creates_stays_a_refusal(
            self, two_pool_xml):
        """Отложенный проход не имеет права превращать отсутствующий id в
        успех: зависимость, которой в пакете нет, отвергается тем же
        сообщением."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "connect", "source": "new_ghost", "target": "S_end"},
            {"op": "add_task", "id": "new_A8", "name": "Проверить наличие",
             "participant": "Магазин", "after": "S_accept"},
        ])
        assert [s["op"] for s in report["skipped"]] == ["connect"]
        assert report["skipped"][0]["reason"] == "источник или цель не найдены"

    def test_mutual_dependency_does_not_loop(self, two_pool_xml):
        """Шаг А ждёт Б, Б ждёт А: второй проход обязан остановиться, а не
        крутиться до конца жизни процесса."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_A1", "name": "Первый",
             "participant": "Магазин", "after": "new_A2"},
            {"op": "add_task", "id": "new_A2", "name": "Второй",
             "participant": "Магазин", "after": "new_A1"},
        ])
        assert {s["op"] for s in report["skipped"]} == {"add_task"}
        assert len(report["skipped"]) == 2 and report["applied"] == []
        assert report["status"] == "failed"



class TestMessageFlowEndsAreNotGateways:
    """Аплайер не ставит развилку концом потока-сообщения.

    Запрет живёт и в линейке (`message_flow_ends`), и здесь, потому что совет
    самой линейки такие обмены и рисовал: `cross_pool_flow` переводил межпуловую
    дугу из развилки в `connect(source='<шлюз>', …, flow_type='message')` — то
    есть пакет заводил дефект, которого нет ни в одном из 625 обменов корпуса.
    Отказ здесь не «тихая обрезка валидного ответа»: обмен с развилкой на конце
    невалиден по BPMN, и причина обязана называть законную форму.
    """

    TWO_POOLS = '''<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D"
             targetNamespace="t">
  <collaboration id="C1">
    <participant id="P1" name="Заказчик" processRef="Proc_1"/>
    <participant id="P2" name="Исполнитель" processRef="Proc_2"/>
  </collaboration>
  <process id="Proc_1" name="Заказчик">
    <startEvent id="S1" name="Начало"/>
    <sequenceFlow id="f1" sourceRef="S1" targetRef="A1"/>
    <userTask id="A1" name="Собрать"/>
    <sequenceFlow id="f2" sourceRef="A1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Куда дальше"/>
    <sequenceFlow id="f3" sourceRef="G1" targetRef="E1"/>
    <endEvent id="E1" name="Конец"/>
  </process>
  <process id="Proc_2" name="Исполнитель">
    <startEvent id="S2" name="Заявка принята"/>
    <sequenceFlow id="g1" sourceRef="S2" targetRef="A2"/>
    <userTask id="A2" name="Выполнить"/>
    <sequenceFlow id="g2" sourceRef="A2" targetRef="E2"/>
    <endEvent id="E2" name="Готово"/>
  </process>
</definitions>'''

    def _connect(self, source, target):
        _, report = apply_operations(self.TWO_POOLS, [
            {"op": "connect", "source": source, "target": target,
             "flow_type": "message"}])
        return report

    def test_message_from_a_gateway_is_refused_with_the_legal_form(self):
        report = self._connect("G1", "A2")
        assert report["status"] == "failed"
        skipped = report["skipped"][0]
        assert "развилка" in skipped["reason"] and "G1" in skipped["reason"]
        assert "шаг, ведущий в развилку" in skipped["hint"]

    def test_message_into_a_gateway_is_refused(self):
        report = self._connect("A2", "G1")
        assert report["status"] == "failed"
        assert "развилка" in report["skipped"][0]["reason"]

    def test_message_between_steps_is_applied(self):
        out, report = apply_operations(self.TWO_POOLS, [
            {"op": "connect", "source": "A1", "target": "A2",
             "flow_type": "message"}])
        assert report["status"] == "success"
        assert _root(out).find(".//bpmn:messageFlow[@sourceRef='A1']", NS) is not None

    def test_the_ban_does_not_reach_sequence_flows(self):
        """Ветку развилки запрет трогать не должен: у `sequenceFlow` шлюз —
        законный источник, и перепутать их значило бы обрезать валидный ответ."""
        _, report = apply_operations(self.TWO_POOLS, [
            {"op": "connect", "source": "G1", "target": "A2"}])
        assert report["skipped"][0]["reason"] == "sequenceFlow между разными пулами недопустим"


class TestEventDefinitionOnExistingEvent:
    """Определение события можно добавить узлу, который уже в схеме.

    `add_event` создаёт событие с определением, но событие без определения в
    ответе модели — другой дефект, и правки ему не было: `event_types` у
    скоринга и инвариант `event_definitions` ругались на пустой кружок, а
    подсказать было нечего — приходилось удалять узел и рисовать заново, цепляя
    обратно потоки. Опасность не в балле, а в молчании: нарушение выглядело
    непочиняемым, хотя нот требует ровно одного дочернего элемента.
    """

    UNTYPED = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_wait">
  <process id="Process_wait" name="Ожидание" isExecutable="true">
    <startEvent id="Start_1" name="Запрос ушёл"/>
    <sequenceFlow id="F1" sourceRef="Start_1" targetRef="W_answer"/>
    <intermediateCatchEvent id="W_answer" name="Ожидание ответа"/>
    <sequenceFlow id="F2" sourceRef="W_answer" targetRef="T_next"/>
    <userTask id="T_next" name="Обработать ответ"/>
    <sequenceFlow id="F3" sourceRef="T_next" targetRef="End_ok"/>
    <endEvent id="End_ok" name="Готово"/>
  </process>
</definitions>"""

    OPS = [{"op": "add_event_definition", "id": "W_answer",
            "event_definition": "timer", "duration": "PT2H"}]

    def test_definition_becomes_a_child_of_the_existing_event(self):
        out, report = apply_operations(self.UNTYPED, self.OPS)
        assert report["status"] == "success"
        event = _by_id(out, "W_answer")
        assert "timerEventDefinition" in _child_tags(event)
        duration = event.find("bpmn:timerEventDefinition/bpmn:timeDuration", NS)
        assert duration.text == "PT2H"

    def test_route_is_untouched_by_the_definition(self):
        out, _ = apply_operations(self.UNTYPED, self.OPS)
        assert build_inventory(out)["flows"] == build_inventory(self.UNTYPED)["flows"]

    def test_timer_without_a_requested_duration_gets_the_default(self):
        out, _ = apply_operations(self.UNTYPED, [{"op": "add_event_definition",
                                                  "id": "W_answer",
                                                  "event_definition": "timer"}])
        duration = _by_id(out, "W_answer").find(
            "bpmn:timerEventDefinition/bpmn:timeDuration", NS)
        assert duration.text == DEFAULT_TIMER_DURATION

    def test_second_definition_is_skipped_with_a_reason(self):
        _, report = apply_operations(self.UNTYPED, self.OPS + [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "message"}])
        skip = report["skipped"][0]
        assert skip["op"] == "add_event_definition"
        assert "уже есть" in skip["reason"]

    def test_a_task_is_not_an_event(self):
        _, report = apply_operations(self.UNTYPED, [
            {"op": "add_event_definition", "id": "T_next",
             "event_definition": "timer"}])
        assert "не событие" in report["skipped"][0]["reason"]

    def test_unknown_definition_names_the_enum(self):
        _, report = apply_operations(self.UNTYPED, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "pulse"}])
        skip = report["skipped"][0]
        assert "timer" in skip["hint"] and "message" in skip["hint"]

    def test_missing_event_is_reported_as_not_found(self):
        _, report = apply_operations(self.UNTYPED, [
            {"op": "add_event_definition", "id": "W_missing",
             "event_definition": "timer"}])
        assert "не найден" in report["skipped"][0]["reason"]

    def test_creating_an_event_that_exists_points_at_the_definition_op(self):
        """`add_event` с id готового события — это правка, а не создание.

        Отказывавший текст «новые элементы обязаны иметь id с new_» модель читала
        как «придумать id» и заводила второй пустой кружок: маршрут оставался
        нарушен по `event_types`, а пакет становился больше. Подсказка теперь
        называет операцию, исполнимую именно по этому id."""
        _, report = apply_operations(self.UNTYPED, [
            {"op": "add_event", "id": "W_answer", "name": "Ожидание ответа",
             "event_type": "intermediateCatch", "participant": "Process_wait"}])
        skip = report["skipped"][0]
        assert "без префикса new_" in skip["reason"]
        assert "add_event_definition(id='W_answer'" in skip["hint"]

    def test_a_task_or_a_typed_event_keeps_the_plain_new_id_hint(self):
        """Адрес подсказки — только пустой кружок между маршрутами: задачей он не
        правится, и совет «перейдите на add_event_definition» на задаче был бы
        вторым отказом вместо ответа."""
        _, report = apply_operations(self.UNTYPED, [
            {"op": "add_event", "id": "T_next", "name": "Обработка",
             "event_type": "intermediateCatch", "participant": "Process_wait"}])
        assert report["skipped"][0]["hint"] == \
            "новые элементы обязаны иметь id, начинающийся с new_"

    # --- таймер без хронометража -------------------------------------------
    # `<timerEventDefinition/>` исполнитель не заведёт никогда: ветка «срок
    # вышел» на такой схеме не наступает, хотя выглядит готовой. Подставить
    # срок этому узлу было нечем — `add_event_definition` отказывал по
    # «определение уже есть», а нарушения (`timer_without_schedule` у скоринга,
    # `timer_schedule` у оракула) выглядели непочиняемыми.

    INERT_TIMER = UNTYPED.replace(
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа"/>',
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа">'
        '<timerEventDefinition/></intermediateCatchEvent>')

    INERT_BLANK = UNTYPED.replace(
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа"/>',
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа">'
        '<timerEventDefinition><timeDuration> </timeDuration>'
        '</timerEventDefinition></intermediateCatchEvent>')

    LIVE_TIMER = UNTYPED.replace(
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа"/>',
        '<intermediateCatchEvent id="W_answer" name="Ожидание ответа">'
        '<timerEventDefinition><timeDuration>PT1H</timeDuration>'
        '</timerEventDefinition></intermediateCatchEvent>')

    def test_timing_fills_a_timer_that_has_none(self):
        out, report = apply_operations(self.INERT_TIMER, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "timer", "duration": "PT30M"}])
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "W_answer")
        definitions = [c for c in event
                       if "timerEventDefinition" in str(c.tag)]
        assert len(definitions) == 1
        assert definitions[0].find("bpmn:timeDuration", NS).text == "PT30M"

    def test_blank_timing_is_treated_as_absent(self):
        out, report = apply_operations(self.INERT_BLANK, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "timer", "duration": "PT30M"}])
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "W_answer").find(
            "bpmn:timerEventDefinition/bpmn:timeDuration", NS).text == "PT30M"

    def test_a_schedule_that_exists_is_not_overwritten(self):
        """Заменить срок — это не «починить узел», а изменить процесс без
        просьбы: хронометраж мог поставить автор, и молча переписывать его
        нельзя."""
        out, report = apply_operations(self.LIVE_TIMER, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "timer", "duration": "PT30M"}])
        assert "хронометраж" in report["skipped"][0]["reason"]
        assert _by_id(out, "W_answer").find(
            "bpmn:timerEventDefinition/bpmn:timeDuration", NS).text == "PT1H"

    def test_changing_the_definition_type_of_a_live_timer_is_still_refused(self):
        _, report = apply_operations(self.LIVE_TIMER, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "message"}])
        assert "уже есть" in report["skipped"][0]["reason"]

    def test_filling_a_schedule_does_not_touch_the_route(self):
        out, _ = apply_operations(self.INERT_TIMER, [
            {"op": "add_event_definition", "id": "W_answer",
             "event_definition": "timer", "duration": "PT30M"}])
        assert build_inventory(out)["flows"] == build_inventory(self.INERT_TIMER)["flows"]


class TestOpNameAlias:
    """Синоним имени операции стоит дешевле, чем отказ с правильными операндами.

    В записанном improvement-пакете две правки из одиннадцати умерли формулировкой
    «неизвестная операция»: модель звала соединение `add_flow` — это слово из
    словаря BPMN (`sequenceFlow`) и из подсказок починки, где «добавь поток»
    встречается чаще, чем «connect». Операнды у её операции были верные, и
    `improve/op_acceptance` записывала отказ как качество модели, хотя виновато
    расхождение словарей.

   Подстановка не расширяет возможности аплайера: принимается ровно то, что
    приняла бы операция `connect` с теми же полями, и она показывает подстановку
    в заметке, чтобы «принято» не выглядело так, будто модель написала
    каноническое имя.
    """

    def test_add_flow_is_applied_as_connect(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_flow", "source": "T_ship", "target": "T_cancel"}])
        assert report["status"] == "success"
        applied = report["applied"][0]
        assert applied["op"] == "connect"
        assert "add_flow" in applied["note"]
        added = _root(out).findall(
            ".//bpmn:sequenceFlow[@sourceRef='T_ship'][@targetRef='T_cancel']", NS)
        assert len(added) == 1

    def test_alias_names_only_operations_the_applier_understands(self):
        assert set(OP_ALIASES.values()) <= set(OP_SPEC)
        assert not set(OP_ALIASES) & set(OP_SPEC)

    def test_a_name_outside_the_table_is_still_rejected(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_wire", "source": "T_ship", "target": "T_cancel"}])
        skip = report["skipped"][0]
        assert skip["reason"] == "неизвестная операция"
        assert "connect" in skip["hint"]

    def test_alias_inherits_the_operand_errors_of_canonical_op(self, single_pool_xml):
        """Подстановка не отменяет проверку: поток в никуда остаётся отказом."""
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_flow", "source": "T_ship", "target": "Nope_1"}])
        assert report["skipped"][0]["op"] == "connect"
        assert "не найден" in report["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# Граничные события и определения событий
# ---------------------------------------------------------------------------

class TestBoundaryEvent:
    # Граничное событие само по себе ничего не делает: пакет обязан вести его
    # ветку обработки в существующий узел, иначе и событие, и его шаг
    # откатываются как оставленные вне маршрута.
    OPS = [{"op": "add_boundary_event", "id": "new_BE_timeout",
            "attached_to": "T_collect", "event_type": "timer", "name": "Дождались"},
           {"op": "add_task", "id": "new_BE_handler", "name": "Эскалация",
            "task_type": "userTask", "after": "new_BE_timeout"},
           {"op": "connect", "source": "new_BE_handler", "target": "End_cancel"}]
    LONE = OPS[:1]

    def test_timer_boundary_event_is_attached_to_the_task(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self.OPS)
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_BE_timeout")
        assert event.tag == f"{{{BPMN_NS}}}boundaryEvent"
        assert event.get("attachedToRef") == "T_collect"
        assert event.get("name") == "Дождались"
        definition = event.find("bpmn:timerEventDefinition", NS)
        assert definition is not None
        assert definition.find("bpmn:timeDuration", NS).text == DEFAULT_TIMER_DURATION
        # событие живёт в том же процессе, что и задача
        assert "new_BE_timeout" in [c.get("id") for c in _process(_root(out), "Process_order")]

    def test_boundary_event_without_participant_inherits_its_hosts_pool(
            self, two_pool_xml):
        """Хозяин по `attachedToRef` и есть пул события: в коллаборации из двух
        пулов отказ «пул не определён» уносил не только событие, но и всю ветку
        его обработки (прогон #45: `add_task` → «цель исхода не найдена»,
        `connect` → «источник не найден», SLA-таймер до схемы не доехал)."""
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_boundary_event", "id": "new_sla", "attached_to":
             "S_accept", "event_type": "timer", "name": "Просрочка приёмки"},
            {"op": "add_task", "id": "new_escalate", "name": "Эскалация",
             "task_type": "userTask", "after": "new_sla"},
            {"op": "connect", "source": "new_escalate", "target": "S_end"},
        ])
        assert report["status"] == "success", report["skipped"]
        shop = _process(_root(out), "Process_shop")
        client = _process(_root(out), "Process_client")
        assert "new_sla" in [c.get("id") for c in shop]
        assert "new_escalate" in [c.get("id") for c in shop]
        assert "new_sla" not in [c.get("id") for c in client]

    def test_error_boundary_event_keeps_the_inventory_consistent(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_boundary_event", "id": "new_BE_fail", "attached_to": "T_ship",
            "event_type": "error", "name": "Ошибка сборки",
        }, {
            "op": "add_task", "id": "new_BE_fail_handler", "name": "Разбор ошибки",
            "task_type": "userTask", "after": "new_BE_fail",
        }, {
            "op": "connect", "source": "new_BE_fail_handler",
            "target": "End_cancel",
        }])
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_BE_fail")
        assert event.find("bpmn:errorEventDefinition", NS) is not None
        assert event.find("bpmn:timerEventDefinition", NS) is None
        inventory = build_inventory(out)
        assert "new_BE_fail" in {e["id"] for e in inventory["elements"]}

    def test_boundary_event_without_handler_is_reported(self, single_pool_xml):
        """Событие из схемы пользователя, которому нечего обрабатывать: скоринг
        считает такой узел тупиком, поэтому починка обязана сказать об этом
        вслух. Откатывает пакет аплайер, а не `validate_and_repair` — здесь же
        проверяется, что битая схема не проходит молча."""
        broken = single_pool_xml.replace(
            '<userTask id="T_ship"',
            '<boundaryEvent id="BE_loose" name="Просрочка" attachedToRef="T_ship">'
            '<timerEventDefinition><timeDuration>PT1H</timeDuration>'
            '</timerEventDefinition></boundaryEvent>'
            '<userTask id="T_ship"')
        out, notes = validate_and_repair(broken)
        assert len(notes) == 1 and "BE_loose" in notes[0], notes
        assert "не ведёт ни к одному шагу" in notes[0]
        assert any(m in notes[0] for m in UNROUTED_NOTE_MARKERS)

    def test_boundary_event_with_handler_leaves_nothing_to_report(
            self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self.OPS + [
            {"op": "connect", "source": "new_BE_timeout", "target": "T_ship",
             "flow_type": "sequence"},
        ])
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_BE_timeout")
        assert _refs(event, "outgoing")
        assert _refs(event, "incoming") == []
        _, notes = validate_and_repair(out)
        assert notes == []

    def test_unknown_task_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_BE", "attached_to": "no_such_id",
             "event_type": "timer", "name": "Таймер"},
        ])
        assert report["skipped"][0]["reason"] == "задача 'no_such_id' не найдена"

    def test_gateway_is_not_a_host(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_BE", "attached_to": "G_paid",
             "event_type": "timer", "name": "Таймер"},
        ])
        assert report["skipped"][0]["reason"] == "'G_paid' не является задачей"

    def test_unsupported_event_type_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_BE", "attached_to": "T_collect",
             "event_type": "signal", "name": "Сигнал"},
        ])
        assert report["skipped"][0]["reason"] == "неизвестный тип граничного события 'signal'"
        assert report["skipped"][0]["hint"] == "допустимы: timer, error"

    def test_missing_name_is_applied_to_a_boundary_event(self, single_pool_xml):
        """Безымянное граничное событие — не дефект: `naming` требует имени у
        шага, а тип события читается из дочернего `*EventDefinition`, не из
        подписи кружка. Ветка обработки здесь обязательна по другой причине —
        её требует скоринг (`TestUnroutedRollback`), и отказ по ней не про имя.
        См. `TestNameIsNotATransportConstraint`."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_BE", "attached_to": "T_collect",
             "event_type": "timer", "to": "T_ship"},
        ])
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_BE")
        assert event.get("attachedToRef") == "T_collect"
        assert "name" not in event.attrib

    def test_reused_id_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, self.OPS + self.OPS)
        assert report["status"] == "partial"
        assert "уже занят" in report["skipped"][0]["reason"]

    def test_id_without_prefix_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "Boundary_1", "attached_to": "T_collect",
             "event_type": "timer", "name": "Таймер"},
        ])
        assert "без префикса new_" in report["skipped"][0]["reason"]

    def test_intermediate_event_can_carry_a_definition(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC_wait", "name": "Ожидание оплаты",
            "event_type": "intermediateCatch", "participant": "Заказ",
            "after": "T_collect", "event_definition": "timer",
        }])
        assert report["status"] == "success"
        event = _by_id(out, "new_IC_wait")
        assert event.find("bpmn:timerEventDefinition/bpmn:timeDuration", NS).text == "PT15M"
        assert _refs(event, "incoming") and _refs(event, "outgoing")

    def test_unknown_definition_is_skipped(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC", "name": "Ожидание",
            "event_type": "intermediateCatch", "participant": "Заказ",
            "event_definition": "pulse",
        }])
        assert report["skipped"][0]["reason"] == "неизвестное определение события 'pulse'"

    @pytest.mark.parametrize("bare_type", ["intermediateCatch", "intermediateThrow"])
    def test_intermediate_event_without_a_definition_is_refused(self, single_pool_xml,
                                                              bare_type):
        """Промежуточное событие без определения — пустой кружок: bpmn-js его
        рисует, а скоринг тип события не засчитывает (`event_definitions`,
        `event_types`). Опаснее всего молчание контура: операция применялась,
        пропуска и пометки починки не было, поэтому корректирующий повтор не
        звал модель даже тогда, когда пакет ронял балл живой схеме
        (production_incident: 95 → 90). Порядок задаёт
        `add_boundary_event` — он тип требует тоже."""
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC_bare", "name": "Ожидание",
            "event_type": bare_type, "participant": "Заказ", "after": "T_collect",
        }])
        assert report["applied"] == []
        skip = report["skipped"][0]
        assert skip["reason"] == "событию нужно определение"
        assert "event_definition=timer|message|error|signal" in skip["hint"]
        assert _by_id(out, "new_IC_bare") is None
        # Отказ до вставки: маршрут хозяина `after` не тронут.
        assert _refs(_by_id(out, "T_collect"), "outgoing") == ["F2"]

    def test_definition_on_start_event_is_refused(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_E", "name": "Старт", "event_type": "start",
            "participant": "Заказ", "event_definition": "timer",
        }])
        assert report["skipped"][0]["reason"] == \
            "стартовому и конечному событию определение не добавляется"

    def test_definition_on_task_is_refused(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{
            "op": "add_task", "id": "new_T1", "name": "Шаг", "participant": "Заказ",
            "event_definition": "timer",
        }])
        assert report["skipped"][0]["reason"] == "определение события задают только событию"


# ---------------------------------------------------------------------------
# Имя: обязательность шагу, а не каждому узлу (транспорт не решает за ноту)
# ---------------------------------------------------------------------------

class TestNameIsNotATransportConstraint:
    """`naming` в линейке объявляет: имя обязательно шагу, а «шлюзу и событию имя
    в BPMN не требуется — ветку подписывает условие». Этот же текст модель читает
    в промпте улучшения, значит безымянный шлюз — легальный ответ, и обрезать
    его транспортом нельзя.

    Найдено не фикстурами, а записанным живым пакетом
    (`warehouse_delivery.live.improve`): модель вернула шлюз ветвления с
    `name: ""` и получила «не задано имя элемента» — одна из четырёх отсеянных
    правок первого раунда. Тип события скоринг читает из дочернего
    `*EventDefinition` (`event_types`), а не из подписи, поэтому безымянные
    узлы не добавляют схеме ни одного нарушения.
    """

    def test_activity_without_a_name_is_still_refused(self, single_pool_xml):
        """Здесь отказ законен: безымянный шаг — нарушение того же `naming`, и
        подсказка зовёт корректирующий повтор, а не молча принимает дефект."""
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_noname", "name": "",
             "participant": "Заказ", "after": "T_collect"},
        ])
        assert report["skipped"][0]["reason"] == "не задано имя элемента"

    def test_gateway_without_a_name_is_applied(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_gateway", "id": "new_G_unnamed", "name": "",
            "gateway_type": "exclusive", "participant": "Заказ",
            "after": "T_collect",
        }])
        assert report["status"] == "success", report["skipped"]
        gateway = _by_id(out, "new_G_unnamed")
        assert gateway is not None and "name" not in gateway.attrib
        assert "имя не задано" in report["applied"][0]["note"]

    def test_event_without_a_name_is_applied(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC_unnamed", "name": "",
            "event_type": "intermediateCatch", "event_definition": "message",
            "participant": "Заказ", "after": "T_collect",
        }])
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_IC_unnamed")
        assert event.find("bpmn:messageEventDefinition", NS) is not None
        assert "name" not in event.attrib

    def test_nameless_gateway_survives_the_guarantee(self, single_pool_xml):
        """Гарант — тот же путь, что у харнесса и продукта: если починка или
        снятие осиротевших узлов не оставляет шлюз, метрика приняла бы правку,
        которой в принятой схеме нет."""
        out, report = apply_and_guarantee(single_pool_xml, [{
            "op": "add_gateway", "id": "new_G_unnamed", "name": "",
            "gateway_type": "exclusive", "participant": "Заказ",
            "after": "T_collect",
        }])
        assert report["status"] == "success", report["skipped"]
        assert report["reverted"] == "", report
        assert _by_id(out, "new_G_unnamed") is not None


# ---------------------------------------------------------------------------
# Вложенность: потоки и переносы не пересекают границу subProcess
# ---------------------------------------------------------------------------

class TestNestingRestriction:
    def test_connect_from_nested_node_to_outer_is_skipped(self):
        _, report = apply_operations(NESTED_XML, [
            {"op": "connect", "source": "I_second", "target": "O_end"},
        ])
        assert report["status"] == "failed"
        assert report["skipped"][0]["reason"] == "элементы лежат на разной вложенности"
        assert "subProcess" in report["skipped"][0]["hint"]

    def test_connect_from_outer_into_nested_node_is_skipped(self):
        _, report = apply_operations(NESTED_XML, [
            {"op": "connect", "source": "O_start", "target": "I_first"},
        ])
        assert report["skipped"][0]["reason"] == "элементы лежат на разной вложенности"

    def test_message_flow_across_nesting_is_skipped_too(self):
        _, report = apply_operations(NESTED_XML, [
            {"op": "connect", "source": "I_second", "target": "S_task",
             "flow_type": "message"},
        ])
        assert report["skipped"][0]["reason"] == "элементы лежат на разной вложенности"

    def test_connect_inside_the_subprocess_is_allowed(self):
        out, report = apply_operations(NESTED_XML, [
            {"op": "connect", "source": "I_second", "target": "I_first",
             "condition": "снова"},
        ])
        assert report["status"] == "success"
        flows = [f for f in _root(out).findall(".//bpmn:sequenceFlow", NS)
                 if f.get("sourceRef") == "I_second" and f.get("targetRef") == "I_first"]
        assert len(flows) == 1

    def test_moving_a_nested_node_to_another_pool_is_skipped(self):
        _, report = apply_operations(NESTED_XML, [
            {"op": "move_to_participant", "id": "I_first", "participant": "Смежный"},
        ])
        assert report["skipped"][0]["reason"] == \
            "элемент внутри subProcess не переносится между пулами"
        assert "subProcess" in report["skipped"][0]["hint"]

    def test_nested_node_passes_a_no_op_move_to_its_own_pool(self):
        _, report = apply_operations(NESTED_XML, [
            {"op": "move_to_participant", "id": "I_first", "participant": "Внешний"},
        ])
        assert report["status"] == "success"

    def test_outer_node_still_movable(self):
        out, report = apply_operations(NESTED_XML, [
            {"op": "move_to_participant", "id": "O_end", "participant": "Смежный"},
        ])
        assert report["status"] == "success"
        assert "O_end" in [c.get("id") for c in _process(_root(out), "Process_side")]


# ---------------------------------------------------------------------------
# События после починки не висят
# ---------------------------------------------------------------------------

class TestRepairWiresEvents:
    def test_added_events_are_connected_to_the_process(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="Один шаг" isExecutable="true">
    <userTask id="T1" name="Работа"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == ["добавлено стартовое событие StartEvent_new_1",
                         "добавлено конечное событие EndEvent_new_2"]
        assert _dangling(out) == []
        assert _refs(_by_id(out, "T1"), "incoming") == ["new_Flow_1"]
        assert _refs(_by_id(out, "T1"), "outgoing") == ["new_Flow_2"]

    def test_start_joins_the_node_without_incoming(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="Без старта" isExecutable="true">
    <endEvent id="E1" name="Финиш"/>
    <sequenceFlow id="F1" sourceRef="T1" targetRef="E1"/>
    <userTask id="T1" name="Работа"/>
    <userTask id="T2" name="Подготовка"/>
    <sequenceFlow id="F2" sourceRef="T2" targetRef="T1"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == ["добавлено стартовое событие StartEvent_new_1"]
        assert _dangling(out) == []
        # вход ждёт узел без входящих потоков, а не тот, у кого вход уже есть
        assert _refs(_by_id(out, "T2"), "incoming") == ["new_Flow_1"]
        assert _refs(_by_id(out, "T1"), "incoming") == ["F2"]

    def test_event_is_not_added_when_no_candidate_exists(self):
        out, notes = validate_and_repair(_cycle_xml())
        assert len(notes) == 2
        assert "стартовое событие не добавлено" in notes[0]
        assert "конечное событие не добавлено" in notes[1]
        assert "Кольцо" in notes[0]
        assert _by_id(out, "StartEvent_new_1") is None
        assert _by_id(out, "EndEvent_new_1") is None
        # починка не оставила ни узла без единого потока
        assert _dangling(out) == []

    def test_nested_nodes_are_left_alone_by_repair(self):
        repaired, notes = validate_and_repair(NESTED_XML)
        assert notes == []
        assert _dangling(repaired) == []
        root = _root(repaired)
        assert _refs(_by_id(repaired, "I_second"), "outgoing") == []
        assert root.findall(".//bpmn:boundaryEvent", NS) == []

    def test_add_event_wires_the_new_end_itself(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="Без финиша" isExecutable="true">
    <startEvent id="S1" name="Старт">
      <outgoing>F1</outgoing>
    </startEvent>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <userTask id="T1" name="Работа">
      <incoming>F1</incoming>
    </userTask>
  </process>
</definitions>"""
        out, report = apply_operations(xml, [
            {"op": "add_event", "id": "new_E1", "name": "Финиш", "event_type": "end",
             "participant": "P1"},
        ])
        assert report["status"] == "success"
        assert report["applied"][0]["note"] == "событие подключено к 'T1'"
        link = _refs(_by_id(out, "T1"), "outgoing")
        assert link == _refs(_by_id(out, "new_E1"), "incoming")
        assert _dangling(out) == []

    def test_unattachable_event_is_rolled_back_not_left_hanging(self):
        out, report = apply_operations(_cycle_xml(), [
            {"op": "add_event", "id": "new_E1", "name": "Финиш", "event_type": "end",
             "participant": "Кольцо"},
        ])
        # У обоих узлов кольца уже есть выход: цеплять нечего. Оставлять в
        # схеме событие без входящего потока нельзя — принятие улучшения
        # добавило бы тупик, поэтому изменение откатывается целиком.
        assert report["status"] == "failed"
        assert report["applied"] == []
        assert "не имеет входящего потока" in report["skipped"][0]["reason"]
        assert _by_id(out, "new_E1") is None


class TestNodeOutlet:
    """`to` — явный исход нового узла. Модель в живых прогонах добавляла шаг и
    один connect к нему, а про исход забывала: шаг откатывался тупиком и правка
    уходила в отказ (applied_share 0.607, improve/pass@1 0)."""

    def test_after_and_outlet_keep_the_route_single(self, single_pool_xml):
        """Когда `after` уже ведёт узел туда же, второй поток не создаётся:
        дуга-дубль — это немоделируемая ветка, а не «обе стороны закрыты»."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_pack", "name": "Упаковать",
             "task_type": "userTask", "after": "T_ship", "to": "End_ok"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "new_T_pack") is not None
        assert len(_root(out).findall(".//bpmn:sequenceFlow"
                                      "[@sourceRef='new_T_pack']"
                                      "[@targetRef='End_ok']", NS)) == 1
        assert "уже даёт вставка after" in report["applied"][0]["note"]
        assert validate_and_repair(out)[1] == []

    def test_outlet_alone_does_not_make_the_step_reachable(self,
                                                           single_pool_xml):
        """Честность важнее доли применённых: шаг с одним только исходом —
        недостижим, и откат обязан сказать про вход, а не про вставку."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_ghost", "name": "Ниоткуда",
             "task_type": "userTask", "to": "End_ok"},
        ])
        skip = [s for s in report["skipped"] if s.get("id") == "new_T_ghost"]
        assert skip and "недостижим" in skip[0]["reason"]
        assert _by_id(out, "new_T_ghost") is None

    def test_boundary_event_with_handler_in_one_operation(self, single_pool_xml):
        """Тот же пропуск, что резал improve в каждом живом прогоне: таймер без
        ветки обработки откатывался. С `to` ветка задана той же операцией."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_escal", "name": "Эскалация",
             "task_type": "userTask", "after": "T_ship", "to": "End_ok"},
            {"op": "add_boundary_event", "id": "new_Timer", "attached_to": "T_ship",
             "event_type": "timer", "name": "Просрочка", "duration": "PT4H",
             "to": "new_T_escal"},
        ])
        assert report["skipped"] == [], report["skipped"]
        assert _by_id(out, "new_Timer") is not None
        assert len(_root(out).findall(
            ".//bpmn:sequenceFlow[@sourceRef='new_Timer']", NS)) == 1
        assert validate_and_repair(out)[1] == []

    def test_outlet_to_a_start_event_is_refused_before_the_insert(
            self, single_pool_xml):
        """Порядок проверок принципиален: `after` перешивает существующий
        поток, и отказ после вставки оставил бы разорванную цепочку."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_loop", "name": "В старт",
             "task_type": "userTask", "after": "T_ship", "to": "Start_1"},
        ])
        assert report["applied"] == []
        assert "не входит" in report["skipped"][0]["reason"]
        # Исходная цепочка целая: T_ship по-прежнему ведёт свой единственный путь.
        assert _by_id(out, "new_T_loop") is None
        assert _refs(_by_id(out, "T_ship"), "outgoing") == ["F5"]
        assert _flow(_root(out), "F5") is not None

    def test_end_event_has_no_outlet(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_event", "id": "new_E", "name": "Финиш",
             "event_type": "end", "participant": "Заказ", "to": "T_ship"}])
        assert "завершает маршрут" in report["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# Межпуловая нога нового узла
# ---------------------------------------------------------------------------

class TestCrossPoolLegBecomesAMessage:
    """«Вставь после шага чужого пула» — это передача сообщения, а не сломанный
    маршрут. Ровно так же читает межпуловой поток `validate_and_repair` (шаг 1b)
    и генератор планов. До этого аплайер отказывал, и пакет терял правку целиком:
    прогон #46 на `production_incident` — `defects_repaired` 0.0 при восьми
    применённых операциях, потому что SLA-событие постмортема не доехало.

    Отказ остаётся там, где сообщения не бывает: шлюз, граничное событие, старт
    как источник.
    """

    def test_step_after_a_foreign_task_arrives_by_message(
            self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_T_notify",
             "name": "Уведомить о заказе", "task_type": "userTask",
             "participant": "Магазин", "after": "C_request", "to": "S_end"},
        ])
        assert report["status"] == "success", report["skipped"]
        node = _by_id(out, "new_T_notify")
        assert node.tag == f"{{{BPMN_NS}}}userTask"
        assert "new_T_notify" in [c.get("id") for c in _process(_root(out),
                                                                "Process_shop")]
        legs = [f for f in _root(out).findall(".//bpmn:messageFlow", NS)
                if f.get("sourceRef") == "C_request"
                and f.get("targetRef") == "new_T_notify"]
        assert len(legs) == 1
        # Внутри своего пула маршрут остался sequence-потоком.
        assert _root(out).findall(".//bpmn:sequenceFlow[@sourceRef='new_T_notify']"
                                  "[@targetRef='S_end']", NS)
        assert validate_and_repair(out)[1] == []

    def test_outlet_into_a_foreign_pool_leaves_a_message(self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_T_reserve",
             "name": "Забронировать товар", "task_type": "userTask",
             "participant": "Магазин", "after": "S_accept", "to": "C_request"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert _root(out).findall(".//bpmn:messageFlow[@sourceRef='new_T_reserve']"
                                  "[@targetRef='C_request']", NS)
        assert validate_and_repair(out)[1] == []
        assert any("потоком-сообщением" in a["note"] for a in report["applied"]), \
            report["applied"]

    def test_a_start_event_of_another_pool_is_still_not_a_sender(
            self, two_pool_xml):
        """Не всякую межпуловую дугу стоит превращать в сообщение: старт
        сообщение принимает, но не отдаёт, и тут правка остаётся отказом."""
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_T_bad", "name": "Из чужого старта",
             "task_type": "userTask", "participant": "Магазин",
             "after": "C_start", "to": "S_end"},
        ])
        assert report["applied"] == []
        assert "в другом пуле" in report["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# Словарь операций и неквадратичность
# ---------------------------------------------------------------------------

class TestNestedPlacement:
    """Граница subProcess нерушима и для вставки, и для ссылок дорожек."""

    SUB_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="PO" name="Основной" isExecutable="true">
    <startEvent id="S" name="Старт"><outgoing>F0</outgoing></startEvent>
    <sequenceFlow id="F0" sourceRef="S" targetRef="SP"/>
    <subProcess id="SP" name="Подпроцесс">
      <incoming>F0</incoming><outgoing>IF2</outgoing>
      <startEvent id="IS" name="Внутри"><outgoing>IF1</outgoing></startEvent>
      <sequenceFlow id="IF1" sourceRef="IS" targetRef="I1"/>
      <userTask id="I1" name="Шаг внутри"><incoming>IF1</incoming><outgoing>IF3</outgoing></userTask>
      <sequenceFlow id="IF3" sourceRef="I1" targetRef="IE"/>
      <endEvent id="IE" name="Внутри финиш"><incoming>IF3</incoming></endEvent>
    </subProcess>
    <sequenceFlow id="IF2" sourceRef="SP" targetRef="E"/>
    <endEvent id="E" name="Финиш"><incoming>IF2</incoming></endEvent>
  </process>
</definitions>"""

    def _parent_of(self, root, elem_id):
        return next((p.get("id") for p in root.iter()
                     if any(c.get("id") == elem_id for c in p)), None)

    def test_insert_after_nested_node_stays_in_the_subprocess(self):
        out, report = apply_operations(self.SUB_XML, [
            {"op": "add_task", "id": "new_T", "name": "Новый шаг",
             "task_type": "userTask", "after": "I1"},
        ])
        assert report["status"] == "success", report["skipped"]
        root = _root(out)
        assert self._parent_of(root, "new_T") == "SP"
        nested_flow = next(f.get("id") for f in root.iter(f"{{{BPMN_NS}}}sequenceFlow")
                           if f.get("sourceRef") == "I1")
        assert self._parent_of(root, nested_flow) == "SP"
        assert _by_id(out, "IF3").get("sourceRef") == "new_T"
        assert self._parent_of(root, "IF3") == "SP"
        # внешний процесс не обрастает чужими узлами и не теряет поток
        assert self._parent_of(root, "IF2") == "PO"
        assert validate_and_repair(out)[1] == []

    def test_top_level_insert_is_unaffected(self):
        out, report = apply_operations(self.SUB_XML, [
            {"op": "add_task", "id": "new_T", "name": "Снаружи",
             "task_type": "userTask", "after": "S"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert self._parent_of(_root(out), "new_T") == "PO"


class TestLaneReferences:
    """flowNodeRef — IDREF: битую или сбежавшую в чужой пул ссылку убираем."""

    LANED = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <collaboration id="C">
    <participant id="PA" name="Пул А" processRef="PrA"/>
    <participant id="PB" name="Пул Б" processRef="PrB"/>
  </collaboration>
  <process id="PrA" name="Пул А" isExecutable="true">
    <laneSet id="LSA"><lane id="LA" name="Исполнитель">
      <flowNodeRef>TA</flowNodeRef></lane></laneSet>
    <startEvent id="SA" name="Старт А"><outgoing>FA</outgoing></startEvent>
    <userTask id="TA" name="Задача А"><incoming>FA</incoming><outgoing>FB</outgoing></userTask>
    <endEvent id="EA" name="Финиш А"><incoming>FB</incoming></endEvent>
    <sequenceFlow id="FA" sourceRef="SA" targetRef="TA"/>
    <sequenceFlow id="FB" sourceRef="TA" targetRef="EA"/>
  </process>
  <process id="PrB" name="Пул Б" isExecutable="true">
    <startEvent id="SB" name="Старт Б"><outgoing>FC</outgoing></startEvent>
    <userTask id="TB" name="Задача Б"><incoming>FC</incoming><outgoing>FD</outgoing></userTask>
    <endEvent id="EB" name="Финиш Б"><incoming>FD</incoming></endEvent>
    <sequenceFlow id="FC" sourceRef="SB" targetRef="TB"/>
    <sequenceFlow id="FD" sourceRef="TB" targetRef="EB"/>
  </process>
</definitions>"""

    def _refs(self, xml):
        return [r.text for r in _root(xml).findall(".//bpmn:flowNodeRef", NS)]

    def test_ref_to_deleted_element_is_dropped_with_note(self):
        out, report = apply_operations(self.LANED, [{"op": "delete", "id": "TA"}])
        assert report["status"] == "success", report["skipped"]
        fixed, notes = validate_and_repair(out)
        assert self._refs(out) == ["TA"], "до починки ссылка ещё есть"
        assert self._refs(fixed) == []
        assert any("ссылалась на удалённый элемент TA" in n for n in notes)

    def test_ref_after_move_to_another_pool_is_dropped(self):
        out, report = apply_operations(self.LANED, [
            {"op": "move_to_participant", "id": "TA", "participant": "Пул Б"},
        ])
        assert report["status"] == "success", report["skipped"]
        fixed, notes = validate_and_repair(out)
        # ссылка исчезла из дорожки пула А, но сам элемент жив
        assert self._refs(fixed) == []
        assert _by_id(fixed, "TA") is not None
        assert any("перенесён в другой пул" in n for n in notes)

    def test_foreign_namespace_lookalikes_are_not_bpmn(self):
        """Расширение вида <acme:lane> не дорожка: сортировка по одному
        локальному имени превращала чужие теги в узлы процесса, и правки
        уезжали в элемент, которого bpmn-js не знает."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:acme="http://acme.example/schema" id="D">
  <process id="P1" name="P" isExecutable="true">
    <acme:lane id="X1" name="не дорожка"/>
    <acme:startEvent id="X2" name="не старт"/>
    <startEvent id="S1" name="Старт"><outgoing>F1</outgoing></startEvent>
    <userTask id="T1" name="Настоящая задача"><incoming>F1</incoming><outgoing>F2</outgoing></userTask>
    <endEvent id="E1" name="Финиш"><incoming>F2</incoming></endEvent>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
  </process>
</definitions>"""
        inventory = build_inventory(xml)
        assert {e["id"] for e in inventory["elements"]} == {"S1", "T1", "E1"}
        assert inventory["lanes"] == []
        fixed, notes = validate_and_repair(xml)
        assert notes == []
        assert _by_id(fixed, "X1") is not None, "чужой узел нельзя вырезать"

    def test_move_to_lane_cannot_target_a_foreign_lane(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:acme="http://acme.example/schema" id="D">
  <process id="P1" name="P" isExecutable="true">
    <acme:lane id="X1" name="не дорожка"/>
    <userTask id="T1" name="Задача"/>
  </process>
</definitions>"""
        _, report = apply_operations(xml, [
            {"op": "move_to_lane", "id": "T1", "lane": "X1"},
        ])
        assert report["skipped"][0]["reason"] == "дорожка 'X1' не найдена"

    def test_valid_reference_is_untouched(self):
        out, report = apply_operations(self.LANED, [
            {"op": "add_documentation", "id": "TA", "text": "Пояснение"},
        ])
        assert report["status"] == "success", report["skipped"]
        fixed, notes = validate_and_repair(out)
        assert self._refs(fixed) == ["TA"]
        assert not any("дорожка" in n or "дорожки" in n for n in notes)


class TestOperationVocabulary:
    def test_every_handler_is_described_for_the_planner(self):
        # планировщик получает OP_SPEC, а подсказка — список _HANDLERS:
        # расхождение означало бы «модель предлагает то, чего аплайер не умеет»
        assert set(OP_SPEC) == set(_HANDLERS)

    def test_new_operations_are_documented_verbatim(self):
        assert '"op":"add_documentation"' in OP_SPEC["add_documentation"]
        assert '"op":"add_lane"' in OP_SPEC["add_lane"]
        assert '"op":"move_to_lane"' in OP_SPEC["move_to_lane"]
        assert '"op":"add_boundary_event"' in OP_SPEC["add_boundary_event"]
        # операции пулов и выхода по умолчанию — дословно в словаре: промпт
        # строится из OP_SPEC, и расхождение означало бы «модель предлагает
        # то, чего аплайер не умеет»
        assert '"op":"set_default"' in OP_SPEC["set_default"]
        assert '"op":"merge_participants"' in OP_SPEC["merge_participants"]
        assert '"as_lane":true' in OP_SPEC["merge_participants"]
        assert '"op":"remove_participant"' in OP_SPEC["remove_participant"]
        assert '"default":true' in OP_SPEC["connect"]
        assert '"duration":"PT15M' in OP_SPEC["add_event"]
        assert '"cycle":"R3/PT10M' in OP_SPEC["add_boundary_event"]

    def test_hint_for_unknown_operation_lists_the_dictionary(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{"op": "add_lane_set"}])
        assert "add_boundary_event" in report["skipped"][0]["hint"]
        assert "merge_participants" in report["skipped"][0]["hint"]


class TestPerformance:
    def test_batch_of_25_operations_on_a_wide_scheme_stays_linear(self):
        xml = _wide_xml()
        inventory = build_inventory(xml)
        # Ширина схемы — по самому XML: инвентарь ограничен потолком контекста
        # модели и для теста линейности показывает, что срез по лимиту работает.
        assert xml.count("<userTask") >= 250
        assert xml.count("<sequenceFlow") >= 500
        assert inventory["limits"]["elements_omitted"] > 0
        assert len(inventory["elements"]) == INVENTORY_MAX_ELEMENTS
        started = time.perf_counter()
        out, report = apply_operations(xml, _batch_of_25())
        validate_and_repair(out)
        elapsed = time.perf_counter() - started
        assert report["status"] == "success", report["skipped"]
        # порог с десятикратным запасом: линейный проход по 250 узлам × 25
        # операциям — единицы миллисекунд
        assert elapsed < 0.5

    def test_index_survives_a_batch_of_deletions(self):
        """Удаление узла внутри subProcess больше не «внутренняя ошибка»:
        родитель находится по parent-map, а не перебором дерева."""
        out, report = apply_operations(NESTED_XML, [{"op": "delete", "id": "I_first"}])
        assert report["status"] == "success"
        assert _by_id(out, "I_first") is None
        assert _flow(_root(out), "IF1") is None
        assert _flow(_root(out), "IF2") is None
        inventory = build_inventory(out)
        assert "I_first" not in {e["id"] for e in inventory["elements"]}
        assert "I_second" in {e["id"] for e in inventory["elements"]}


# ---------------------------------------------------------------------------
# Инвентарь
# ---------------------------------------------------------------------------

class TestBuildInventory:
    def test_lists_participants_elements_and_flows(self, single_pool_xml):
        inventory = build_inventory(single_pool_xml)
        assert inventory["participants"] == [{"id": "Process_order", "name": "Заказ"}]
        ids = {e["id"] for e in inventory["elements"]}
        assert {"Start_1", "T_collect", "G_paid", "T_ship", "T_cancel",
                "End_ok", "End_cancel"} <= ids
        for element in inventory["elements"]:
            assert element["participant"] == "Заказ"
        assert {f["id"] for f in inventory["flows"]} == {"F1", "F2", "F3", "F4", "F5", "F6"}

    def test_gateway_conditions_reach_the_prompt(self, single_pool_xml):
        flows = {f["id"]: f for f in build_inventory(single_pool_xml)["flows"]}
        assert flows["F3"]["condition"] == "paid == true"
        assert "condition" not in flows["F1"]

    def test_types_and_element_counts(self, single_pool_xml):
        types = {e["id"]: e["type"] for e in build_inventory(single_pool_xml)["elements"]}
        assert types["G_paid"] == "exclusiveGateway"
        assert types["T_collect"] == "userTask"
        assert types["Start_1"] == "startEvent"

    def test_message_flows_are_marked(self, two_pool_xml):
        flows = {f["id"]: f for f in build_inventory(two_pool_xml)["flows"]}
        assert flows["MF1"]["kind"] == "message"
        assert flows["MF1"]["source"] == "C_request"


# ---------------------------------------------------------------------------
# Вставка элемента
# ---------------------------------------------------------------------------

class TestInsertBetween:
    OPS = [{
        "op": "add_task", "id": "new_T_check", "name": "Проверить оплату",
        "task_type": "serviceTask", "after": "T_collect",
    }]

    def test_new_element_lands_in_same_process(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self.OPS)
        assert report["status"] == "success"
        new_elem = _by_id(out, "new_T_check")
        assert new_elem.tag == f"{{{BPMN_NS}}}serviceTask"
        process = _root(out).find(".//bpmn:process", NS)
        assert "new_T_check" in [child.get("id") for child in process]

    def test_flows_are_rewired_not_duplicated(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, self.OPS)
        root = _root(out)
        inserted = [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                    if f.get("sourceRef") == "T_collect"]
        assert len(inserted) == 1
        assert inserted[0].get("targetRef") == "new_T_check"
        old = _flow(root, "F2")
        assert old.get("sourceRef") == "new_T_check"
        assert old.get("targetRef") == "G_paid"
        assert len(root.findall(".//bpmn:sequenceFlow", NS)) == 7

    def test_incoming_outgoing_refs_stay_consistent(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, self.OPS)
        root = _root(out)
        link = [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                if f.get("targetRef") == "new_T_check"][0].get("id")
        assert _refs(_by_id(out, "T_collect"), "outgoing") == [link]
        assert _refs(_by_id(out, "new_T_check"), "incoming") == [link]
        assert _refs(_by_id(out, "new_T_check"), "outgoing") == ["F2"]

    def test_result_survives_repair_without_changes(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, self.OPS)
        repaired, notes = validate_and_repair(out)
        assert notes == []
        assert build_inventory(repaired)["flows"] == build_inventory(out)["flows"]

    def test_gateway_with_two_branches_asks_for_explicit_ops(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{
            "op": "add_task", "id": "new_T_x", "name": "Ветка", "after": "G_paid",
        }])
        assert report["status"] == "failed"
        assert report["skipped"][0]["reason"] == "у шлюза 'G_paid' несколько исходящих потоков"
        assert "disconnect/connect" in report["skipped"][0]["hint"]

    def test_branch_condition_stays_on_gateway_side(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"/>
    <sequenceFlow id="FA" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Шлюз"/>
    <sequenceFlow id="FB" sourceRef="G1" targetRef="E1">
      <conditionExpression>x == 1</conditionExpression>
    </sequenceFlow>
    <endEvent id="E1" name="Финиш"/>
  </process>
</definitions>"""
        out, report = apply_operations(xml, [{
            "op": "add_task", "id": "new_T1", "name": "Шаг", "after": "G1",
        }])
        assert report["status"] == "success"
        root = _root(out)
        link = [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                if f.get("targetRef") == "new_T1"][0]
        assert link.find("bpmn:conditionExpression", NS).text == "x == 1"
        assert _flow(root, "FB").find("bpmn:conditionExpression", NS) is None


# ---------------------------------------------------------------------------
# Удаление и переименование
# ---------------------------------------------------------------------------

class TestDeleteAndRename:
    def test_delete_removes_flows_and_references(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{"op": "delete", "id": "T_ship"}])
        assert report["status"] == "success"
        assert "F3" in report["applied"][0]["note"]
        assert _by_id(out, "T_ship") is None
        root = _root(out)
        assert _flow(root, "F3") is None
        assert _flow(root, "F5") is None
        assert "F3" not in _refs(_by_id(out, "G_paid"), "outgoing")
        assert "F5" not in _refs(_by_id(out, "End_ok"), "incoming")

    def test_delete_never_touches_boundaries(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{"op": "delete", "id": "Start_1"}])
        assert report["status"] == "failed"
        assert report["skipped"][0]["reason"] == "стартовые и конечные события не удаляются"

    def test_rename_changes_only_the_name(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "rename", "id": "T_collect", "name": "Собрать и упаковать"},
        ])
        assert report["status"] == "success"
        assert _by_id(out, "T_collect").get("name") == "Собрать и упаковать"
        assert _refs(_by_id(out, "T_collect"), "outgoing") == ["F2"]

    def test_broken_batch_reports_every_problem(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "frobnicate", "id": "T_collect"},
            {"op": "add_task", "id": "T_collect", "name": "Без префикса"},
            {"op": "add_task", "id": "new_T_noname"},
            {"op": "rename", "id": "no_such_id", "name": "Нет такого"},
            "не объект",
            {"op": "rename", "id": "T_collect", "name": "Собрать заказ заново"},
        ])
        assert report["status"] == "partial"
        assert [a["op"] for a in report["applied"]] == ["rename"]
        reasons = [s["reason"] for s in report["skipped"]]
        assert "неизвестная операция" in reasons
        assert any("без префикса new_" in r for r in reasons)
        assert any("не задано имя" in r for r in reasons)
        assert "элемент не найден" in reasons
        assert len(report["skipped"]) == 5


# ---------------------------------------------------------------------------
# Соединение / разъединение / перенос
# ---------------------------------------------------------------------------

class TestConnections:
    def test_connect_creates_flow_with_condition(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "connect", "source": "T_ship", "target": "T_cancel",
            "condition": "stock == 0",
        }])
        assert report["status"] == "success"
        root = _root(out)
        flows = [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                 if f.get("sourceRef") == "T_ship" and f.get("targetRef") == "T_cancel"]
        assert len(flows) == 1
        assert flows[0].find("bpmn:conditionExpression", NS).text == "stock == 0"

    def test_duplicate_connection_is_skipped(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "connect", "source": "T_ship", "target": "T_cancel"},
            {"op": "connect", "source": "T_ship", "target": "T_cancel"},
        ])
        assert report["status"] == "partial"
        assert report["skipped"][0]["reason"] == "такой поток уже существует"
        root = _root(out)
        assert len([f for f in root.findall(".//bpmn:sequenceFlow", NS)
                    if f.get("sourceRef") == "T_ship" and f.get("targetRef") == "T_cancel"]) == 1

    def test_cross_pool_sequence_flow_is_rejected(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [
            {"op": "connect", "source": "C_request", "target": "S_accept"},
        ])
        assert report["status"] == "failed"
        assert report["skipped"][0]["reason"] == "sequenceFlow между разными пулами недопустим"
        assert report["skipped"][0]["hint"] == "используйте flow_type=message"

    def test_message_flow_goes_to_collaboration(self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [{
            "op": "connect", "source": "C_end", "target": "S_accept",
            "flow_type": "message",
        }])
        assert report["status"] == "success"
        root = _root(out)
        collaboration = root.find(".//bpmn:collaboration", NS)
        messages = collaboration.findall("bpmn:messageFlow", NS)
        assert len(messages) == 2

    def test_unconditioned_flow_becomes_the_default_branch(
            self, single_pool_xml):
        """Необусловленная ветка при условных соседях — это «иначе», поэтому она
        становится выходом по умолчанию: оставить её без условия нельзя,
        скоринг (gateway_conditions) посчитал бы ветку ошибкой."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "connect", "source": "G_paid", "target": "End_cancel"},
        ])
        assert report["status"] == "success", report["skipped"]
        gateway = _by_id(out, "G_paid")
        added = [f for f in _root(out).findall(".//bpmn:sequenceFlow", NS)
                 if f.get("sourceRef") == "G_paid" and f.get("targetRef") == "End_cancel"]
        assert len(added) == 1
        assert gateway.get("default") == added[0].get("id")
        assert added[0].find("bpmn:conditionExpression", NS) is None
        assert "выходом по умолчанию" in report["applied"][0]["note"]

    def test_explicit_default_flow_is_marked_on_the_gateway(
            self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "connect", "source": "G_paid", "target": "End_cancel",
            "default": True,
        }])
        assert report["status"] == "success", report["skipped"]
        gateway = _by_id(out, "G_paid")
        added = [f for f in _root(out).findall(".//bpmn:sequenceFlow", NS)
                 if f.get("sourceRef") == "G_paid" and f.get("targetRef") == "End_cancel"]
        assert gateway.get("default") == added[0].get("id")
        assert "выход по умолчанию" in report["applied"][0]["note"]

    def test_conditioned_flow_from_exclusive_gateway_is_applied(
            self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "connect", "source": "G_paid", "target": "End_cancel",
            "condition": "paid == true и остаток на складе",
        }])
        assert report["status"] == "success", report["skipped"]
        root = _root(out)
        added = [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                 if f.get("sourceRef") == "G_paid" and f.get("targetRef") == "End_cancel"]
        assert len(added) == 1
        assert added[0].find("bpmn:conditionExpression", NS).text

    def test_timer_event_type_is_read_as_definition(self, single_pool_xml):
        """Модель зовёт «таймер» типом события — это определение ловушки у
        промежуточного события. Отказывать тут значит терять всю ветку SLA
        каскадом пропусков."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_event", "id": "new_timer", "name": "Таймер SLA",
             "event_type": "timer", "participant": "Заказ"},
            {"op": "connect", "source": "T_ship", "target": "new_timer"},
            {"op": "connect", "source": "new_timer", "target": "End_ok"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert "timerEventDefinition" in out
        assert "intermediateCatch" in report["applied"][0]["note"]

    def test_disconnect_removes_flow_and_refs(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{"op": "disconnect", "flow": "F2"}])
        assert report["status"] == "success"
        assert _flow(_root(out), "F2") is None
        assert "F2" not in _refs(_by_id(out, "T_collect"), "outgoing")
        assert "F2" not in _refs(_by_id(out, "G_paid"), "incoming")

    def test_move_between_pools_breaks_cross_pool_flows(self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [{
            "op": "move_to_participant", "id": "C_request", "participant": "Магазин",
        }])
        assert report["status"] == "success"
        root = _root(out)
        shop = root.find(".//bpmn:process[@id='Process_shop']", NS)
        assert "C_request" in [child.get("id") for child in shop]
        assert _flow(root, "CF1") is None
        assert _flow(root, "CF2") is None
        assert "CF1" in report["applied"][0]["note"]


# ---------------------------------------------------------------------------
# Пулы
# ---------------------------------------------------------------------------

class TestParticipants:
    def test_add_participant_creates_collaboration(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_participant", "id": "new_Pay", "name": "Платёжный шлюз",
        }])
        assert report["status"] == "success"
        root = _root(out)
        collaboration = root.find(".//bpmn:collaboration", NS)
        assert collaboration is not None
        participant = collaboration.find("bpmn:participant", NS)
        assert participant.get("name") == "Платёжный шлюз"
        process_id = participant.get("processRef")
        assert root.find(f".//bpmn:process[@id='{process_id}']", NS) is not None

    def test_new_node_lands_in_named_pool(self, two_pool_xml):
        out, report = apply_operations(two_pool_xml, [
            {"op": "add_task", "id": "new_T_pack", "name": "Упаковать",
             "participant": "Магазин", "task_type": "userTask"},
            {"op": "connect", "source": "S_accept", "target": "new_T_pack"},
            {"op": "connect", "source": "new_T_pack", "target": "S_end"},
        ])
        assert report["status"] == "success", report["skipped"]
        root = _root(out)
        shop = root.find(".//bpmn:process[@id='Process_shop']", NS)
        assert "new_T_pack" in [child.get("id") for child in shop]
        # Соседний пул не тронут: новые потоки остались в «Магазине».
        assert _flow(root, "CF2") is not None

    def test_unknown_pool_is_skipped_with_hint(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [{
            "op": "add_task", "id": "new_T1", "name": "Хотелка", "participant": "Склад",
        }])
        assert report["skipped"][0]["reason"] == "пул не определён"

    def test_cross_pool_insert_stays_in_its_own_pool(self, two_pool_xml):
        """Вставка после шага чужого пула не переносит узел в тот пул и не
        отказывает молча: узел остаётся своим, а вход — сообщение (тот же приём
        у `validate_and_repair`, шаг 1b). Без продолжения он откатывается как
        тупик, и это честный ответ: правка не оставляет висячего шага."""
        out, report = apply_operations(two_pool_xml, [{
            "op": "add_task", "id": "new_T1", "name": "Не туда",
            "participant": "Магазин", "after": "C_request",
        }])
        skip = [s for s in report["skipped"] if s.get("id") == "new_T1"]
        assert skip and "тупик" in skip[0]["reason"], report["skipped"]
        assert _by_id(out, "new_T1") is None
        # Чужой пул не тронут: сообщение не разорвало маршрут «Клиента».
        assert _flow(_root(out), "CF2") is not None


# ---------------------------------------------------------------------------
# Откат узлов вне маршрута
# ---------------------------------------------------------------------------

class TestUnroutedRollback:
    """Пакет не должен оставлять в схеме шаг, который ниоткуда не входит или
    никуда не выходит: принятое улучшение иначе ухудшает качество схемы."""

    def test_insert_into_a_node_deleted_by_the_same_batch_is_rolled_back(
            self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_pack", "name": "Упаковать",
             "after": "T_ship", "task_type": "userTask"},
            {"op": "delete", "id": "T_ship"},
        ])
        assert [a["id"] for a in report["applied"]] == ["T_ship"]
        rollback = [s for s in report["skipped"] if s.get("id") == "new_T_pack"]
        assert rollback and "недостижим" in rollback[0]["reason"]
        assert "вход" in rollback[0]["hint"]
        assert _by_id(out, "new_T_pack") is None
        # Остаток схемы цел: удалённый хост и его потоки — след операции
        # delete, а не отката.
        assert _by_id(out, "T_ship") is None
        assert _flow(_root(out), "F1") is not None
        assert _flow(_root(out), "F5") is None

    def test_dead_end_hint_names_the_missing_outlet(self, single_pool_xml):
        """Подсказка обязана называть недостающую дугу, а не способ вставки:
        «перевставьте с after» при уже имевшемся входе повторяет ровно тот же
        тупик — живой прогон сжёг на таком повторе второй вызов модели."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_A9", "name": "Эскалация",
             "task_type": "userTask"},
            {"op": "connect", "source": "T_ship", "target": "new_A9"},
        ])
        rollback = [s for s in report["skipped"] if s.get("id") == "new_A9"]
        assert rollback and "тупик" in rollback[0]["reason"]
        assert "исход" in rollback[0]["hint"]
        assert "to" in rollback[0]["hint"]
        assert _by_id(out, "new_A9") is None

    def test_boundary_event_without_handler_is_rolled_back(self, single_pool_xml):
        """Граничное событие без ветки обработки — кружок, за который скоринг
        снимает балл: откатывать надо и его, иначе принятие роняет качество
        (так и случилось в живом прогоне: 95 → 91)."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_Timer", "attached_to": "T_ship",
             "event_type": "timer", "name": "Просрочка отгрузки",
             "duration": "PT4H"},
        ])
        rollback = [s for s in report["skipped"] if s.get("id") == "new_Timer"]
        assert rollback and "без ветки обработки" in rollback[0]["reason"]
        assert rollback[0]["op"] == "add_boundary_event"
        assert "to" in rollback[0]["hint"]
        assert _by_id(out, "new_Timer") is None
        # Откат не должен трогать исходную схему: сравнение по инвентарю,
        # потому что сериализация нормализует префиксы пространств имён.
        assert ({e["id"] for e in build_inventory(out)["elements"]}
                == {e["id"] for e in build_inventory(single_pool_xml)["elements"]})

    def test_isolated_pair_of_new_nodes_disappears_together(self, single_pool_xml):
        """Связанные только между собой новые шаги — тот же дефект: откат
        идёт до неподвижной точки, иначе второй узел остался бы висеть."""
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_A", "name": "Шаг А",
             "task_type": "userTask"},
            {"op": "add_task", "id": "new_B", "name": "Шаг Б",
             "task_type": "userTask"},
            {"op": "connect", "source": "new_A", "target": "new_B"},
        ])
        assert report["applied"] == []
        assert report["status"] == "failed"
        assert {"new_A", "new_B"} <= {s.get("id") for s in report["skipped"]}
        root = _root(out)
        assert _by_id(out, "new_A") is None and _by_id(out, "new_B") is None
        assert len(root.findall(".//bpmn:sequenceFlow", NS)) == 6

    def test_routed_addition_survives_and_is_not_reported(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T_pack", "name": "Упаковать",
             "task_type": "userTask"},
            {"op": "connect", "source": "T_collect", "target": "new_T_pack"},
            {"op": "connect", "source": "new_T_pack", "target": "G_paid"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "new_T_pack") is not None
        _, notes = validate_and_repair(out)
        assert [n for n in notes if "new_T_pack" in n] == []


# ---------------------------------------------------------------------------
# Починка
# ---------------------------------------------------------------------------

class TestValidateAndRepair:
    def test_dangling_flow_removed(self, single_pool_xml):
        broken = single_pool_xml.replace(
            '<sequenceFlow id="F5" sourceRef="T_ship" targetRef="End_ok"/>',
            '<sequenceFlow id="F5" sourceRef="T_ghost" targetRef="End_ok"/>',
        )
        out, notes = validate_and_repair(broken)
        assert notes == ["удалён висящий поток F5",
                         "узел «Отгрузить товар» (T_ship) — тупик: вход есть, "
                         "выхода нет. Нужен connect от него к следующему шагу "
                         "или конечному событию пула"]
        assert _flow(_root(out), "F5") is None

    def test_duplicate_flows_collapsed(self, single_pool_xml):
        broken = single_pool_xml.replace(
            '<sequenceFlow id="F5" sourceRef="T_ship" targetRef="End_ok"/>',
            '<sequenceFlow id="F5" sourceRef="T_ship" targetRef="End_ok"/>'
            '<sequenceFlow id="F5dup" sourceRef="T_ship" targetRef="End_ok"/>',
        )
        out, notes = validate_and_repair(broken)
        assert notes == ["удалён дубль потока F5dup"]
        root = _root(out)
        assert len([f for f in root.findall(".//bpmn:sequenceFlow", NS)
                    if f.get("id") == "F5"]) == 1

    def test_missing_boundary_events_added(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <userTask id="T1" name="Работа"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert len(notes) == 2
        assert _by_id(out, "StartEvent_new_1") is not None
        assert _by_id(out, "EndEvent_new_2") is not None

    def test_empty_process_is_reported_not_invented(self):
        """Пустой пул скоринг наказывает, но дописывать в него события за
        пользователя нельзя — правка обязана быть видна."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true"/>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == ['пул «P» остался без шагов — добавьте в него элементы '
                         "операциями add_task/add_event"]
        assert _root(out).find(".//bpmn:process", NS) is not None

    def test_unreachable_node_is_reported(self):
        """Шаг только с исходящими недостижим: операции применились, но маршрут
        обрывается выше него. Починка здесь не в правах (непонятно, от какого
        шага вести), поэтому отчёт обязан его показать."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"/>
    <serviceTask id="T1" name="Проверка">
      <outgoing>F2</outgoing>
    </serviceTask>
    <sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
    <endEvent id="E1" name="Финиш"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == ["узел «Проверка» (T1) недостижим: выход есть, входа нет. "
                         "Нужен connect от предыдущего шага или шлюза к нему"]
        assert _by_id(out, "T1") is not None

    def test_single_branch_exclusive_gateway_is_demoted(self):
        """Шлюз с одной веткой — не развилка. Вторую ветку аплайер выдумать не
        может, поэтому шлюз понижается до задачи, а условие с единственного
        его потока снимается (иначе скоринг наказывается за правку модели)."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"/>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Проверка">
      <incoming>F1</incoming><outgoing>F2</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="E1">
      <conditionExpression>paid == true</conditionExpression>
    </sequenceFlow>
    <endEvent id="E1" name="Финиш"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert any("понижен до задачи" in n for n in notes), notes
        root = _root(out)
        gateway = root.find(".//bpmn:exclusiveGateway", NS)
        assert gateway is None
        assert _by_id(out, "G1").tag.endswith("}task")
        assert _flow(root, "F2").find("bpmn:conditionExpression", NS) is None

    def test_converging_gateway_survives_the_repair(self):
        """Шлюз схождения легитимен с одним исходящим: понижение съело бы
        слияния веток, которые генератор вставляет осознанно."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"/>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Хватает?">
      <incoming>F1</incoming><outgoing>F2</outgoing><outgoing>F3</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="A1"><conditionExpression>да</conditionExpression></sequenceFlow>
    <sequenceFlow id="F3" sourceRef="G1" targetRef="A2"/>
    <userTask id="A1" name="Собрать"><outgoing>F4</outgoing></userTask>
    <userTask id="A2" name="Заказать остаток"><outgoing>F5</outgoing></userTask>
    <sequenceFlow id="F4" sourceRef="A1" targetRef="G2"/>
    <sequenceFlow id="F5" sourceRef="A2" targetRef="G2"/>
    <exclusiveGateway id="G2" name="Схождение веток">
      <incoming>F4</incoming><incoming>F5</incoming><outgoing>F6</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
    <endEvent id="E1" name="Финиш"><incoming>F6</incoming></endEvent>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert not any("понижен до задачи" in n for n in notes), notes
        assert _by_id(out, "G2").tag.endswith("}exclusiveGateway")

    def test_lone_unconditioned_branch_becomes_the_default(self):
        """`add_gateway` переподвешивает готовый поток под новый шлюз мимо
        `connect`, поэтому правило «единственная безусловная ветка — это „иначе“»
        живёт в починке: без него улучшение добавляло шлюз и ронялась метрика."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"><outgoing>F1</outgoing></startEvent>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Оплата?">
      <incoming>F1</incoming><outgoing>F2</outgoing><outgoing>F3</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="E1"><conditionExpression>paid</conditionExpression></sequenceFlow>
    <sequenceFlow id="F3" sourceRef="G1" targetRef="E2"/>
    <endEvent id="E1" name="Оплачен"><incoming>F2</incoming></endEvent>
    <endEvent id="E2" name="Отменён"><incoming>F3</incoming></endEvent>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert _by_id(out, "G1").get("default") == "F3"
        assert any("единственная без условия" in n for n in notes), notes

    def test_two_unconditioned_branches_are_left_to_the_author(self):
        """Какую из двух безусловных веток считать запасной — решает модель:
        назначить default наугад значит нарисовать маршрут, которого не просили."""
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"><outgoing>F1</outgoing></startEvent>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Оплата?">
      <incoming>F1</incoming><outgoing>F2</outgoing><outgoing>F3</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="E1"/>
    <sequenceFlow id="F3" sourceRef="G1" targetRef="E2"/>
    <endEvent id="E1" name="Оплачен"><incoming>F2</incoming></endEvent>
    <endEvent id="E2" name="Отменён"><incoming>F3</incoming></endEvent>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert _by_id(out, "G1").get("default") is None
        assert not any("выходом по умолчанию" in n for n in notes), notes

    def test_references_rebuilt_from_actual_flows(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true">
    <startEvent id="S1" name="Старт"/>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <userTask id="T1" name="Работа">
      <incoming>Flow_which_never_existed</incoming>
      <outgoing>F1</outgoing>
    </userTask>
    <endEvent id="E1" name="Финиш"/>
  </process>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == ["узел «Работа» (T1) — тупик: вход есть, выхода нет. "
                         "Нужен connect от него к следующему шагу или конечному "
                         "событию пула"]
        assert _refs(_by_id(out, "S1"), "outgoing") == ["F1"]
        assert _refs(_by_id(out, "T1"), "incoming") == ["F1"]
        assert _refs(_by_id(out, "T1"), "outgoing") == []


# ---------------------------------------------------------------------------
# Выход по умолчанию у шлюза
# ---------------------------------------------------------------------------

# Шлюз без единого условия: необусловленную ветку аплайер добавить не может —
# непонятно, какая из них «иначе».
BARE_GATEWAY_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_gw">
  <process id="P1" name="Заказ" isExecutable="true">
    <startEvent id="S1" name="Старт"><outgoing>F1</outgoing></startEvent>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Оплата?">
      <incoming>F1</incoming><outgoing>F2</outgoing><outgoing>F3</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="T1"/>
    <userTask id="T1" name="Отгрузить"><incoming>F2</incoming><outgoing>F4</outgoing></userTask>
    <sequenceFlow id="F4" sourceRef="T1" targetRef="E1"/>
    <endEvent id="E1" name="Готово"><incoming>F4</incoming></endEvent>
    <sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>
    <userTask id="T2" name="Отменить"><incoming>F3</incoming><outgoing>F5</outgoing></userTask>
    <sequenceFlow id="F5" sourceRef="T2" targetRef="E2"/>
    <endEvent id="E2" name="Отменён"><incoming>F5</incoming></endEvent>
  </process>
</definitions>"""


def _gateway_default_id(xml, gateway_id):
    return _by_id(xml, gateway_id).get("default")


class TestDefaultFlow:
    def test_default_flag_marks_the_gateway_and_drops_the_condition(self):
        out, report = apply_operations(BARE_GATEWAY_XML, [{
            "op": "connect", "source": "G1", "target": "E2",
            "condition": "иначе", "default": True,
        }])
        assert report["status"] == "success", report["skipped"]
        flow_id = _gateway_default_id(out, "G1")
        assert flow_id
        flow = _flow(_root(out), flow_id)
        assert flow.get("sourceRef") == "G1" and flow.get("targetRef") == "E2"
        # у выхода по умолчанию условия быть не может — иначе ветка описана дважды
        assert flow.find("bpmn:conditionExpression", NS) is None
        assert "условие с выхода по умолчанию снято" in report["applied"][0]["note"]

    def test_string_true_counts_as_the_flag(self):
        """Модель шлёт булевы поля строками: «false» строкой — не то же, что False."""
        out, report = apply_operations(BARE_GATEWAY_XML, [{
            "op": "connect", "source": "G1", "target": "E1", "default": "true",
        }])
        assert report["status"] == "success", report["skipped"]
        assert _gateway_default_id(out, "G1")

        _, refused = apply_operations(BARE_GATEWAY_XML, [{
            "op": "connect", "source": "G1", "target": "E1", "default": "maybe",
        }])
        assert refused["skipped"][0]["reason"] == "поле default должно быть булевым"

    def test_second_default_of_the_same_gateway_is_refused(self):
        _, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "G1", "target": "E1", "default": True},
            {"op": "connect", "source": "G1", "target": "E2", "default": True},
        ])
        assert report["status"] == "partial"
        assert "уже есть выход по умолчанию" in report["skipped"][0]["reason"]
        assert "set_default" in report["skipped"][0]["hint"]

    def test_unconditioned_branch_without_default_and_conditions_is_refused(self):
        """Ни условий, ни default: непонятно, какая ветка «иначе», а
        необусловленная ветка исключающего шлюза — дефект. Отказ с подсказкой,
        как это починить."""
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "G1", "target": "E1"},
        ])
        skip = report["skipped"][0]
        assert skip["reason"] == (
            "у шлюза 'G1' нет ни условий на ветках, ни выхода по умолчанию")
        assert '"default": true' in skip["hint"]
        assert _gateway_default_id(out, "G1") is None
        assert _flow(_root(out), "new_Flow_1") is None

    def test_unconditioned_branch_is_refused_when_default_is_taken(self):
        out, _ = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "G1", "target": "E1", "default": True},
        ])
        _, report = apply_operations(out, [{"op": "connect", "source": "G1", "target": "E2"}])
        skip = report["skipped"][0]
        assert "уже есть выход по умолчанию" in skip["reason"]
        assert "condition" in skip["hint"]

    def test_default_rejected_for_task_and_message_flow(self, two_pool_xml):
        _, by_task = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "T1", "target": "E2", "default": True},
        ])
        assert by_task["skipped"][0]["reason"] == "'T1' не является шлюзом"
        _, by_message = apply_operations(two_pool_xml, [
            {"op": "connect", "source": "C_end", "target": "S_accept",
             "flow_type": "message", "default": True},
        ])
        assert by_message["skipped"][0]["reason"] == \
            "выходом по умолчанию назначают sequence-поток"

    def test_set_default_marks_an_existing_branch(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [
            {"op": "set_default", "gateway": "G_paid", "flow": "F3"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert _gateway_default_id(out, "G_paid") == "F3"
        # условие с ветки по умолчанию снимается: иначе она описана дважды
        assert _flow(_root(out), "F3").find("bpmn:conditionExpression", NS) is None
        assert "условие с потока 'F3' снято" in report["applied"][0]["note"]

    def test_set_default_replaces_the_previous_one(self):
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "set_default", "gateway": "G1", "flow": "F2"},
            {"op": "set_default", "gateway": "G1", "flow": "F3"},
        ])
        assert report["status"] == "success", report["skipped"]
        assert _gateway_default_id(out, "G1") == "F3"
        assert "прежний выход по умолчанию (F2) снят" in report["applied"][1]["note"]

    def test_set_default_refusals(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "set_default", "gateway": "nope", "flow": "F3"},
            {"op": "set_default", "gateway": "G_paid", "flow": "nope"},
            {"op": "set_default", "gateway": "T_collect", "flow": "F2"},
            {"op": "set_default", "gateway": "G_paid", "flow": "F1"},
        ])
        assert [s["reason"] for s in report["skipped"]] == [
            "шлюз не найден",
            "поток не найден",
            "'T_collect' не является шлюзом",
            "поток 'F1' выходит не из шлюза 'G_paid'",
        ]

    def test_set_default_rejects_message_flow(self):
        xml = BARE_GATEWAY_XML.replace(
            '  <process id="P1"',
            '  <collaboration id="C_gw">\n'
            '    <messageFlow id="MF1" sourceRef="T1" targetRef="T2"/>\n'
            '  </collaboration>\n  <process id="P1"')
        _, report = apply_operations(xml, [
            {"op": "set_default", "gateway": "G1", "flow": "MF1"}])
        assert report["skipped"][0]["reason"] == "'MF1' не sequence-поток"

    def test_repair_drops_a_default_pointing_nowhere(self):
        broken = BARE_GATEWAY_XML.replace('<exclusiveGateway id="G1" name="Оплата?">',
                                          '<exclusiveGateway id="G1" name="Оплата?" default="F9">')
        out, notes = validate_and_repair(broken)
        assert any("недоступный выход по умолчанию F9" in n for n in notes), notes
        assert _gateway_default_id(out, "G1") is None

    def test_repair_drops_default_when_gateway_degrades_to_task(self):
        broken = BARE_GATEWAY_XML.replace(
            '<sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>\n', "")
        out, notes = validate_and_repair(broken.replace(
            '<exclusiveGateway id="G1" name="Оплата?">',
            '<exclusiveGateway id="G1" name="Оплата?" default="F2">'))
        assert any("понижен до задачи" in n and "выход по умолчанию снят" in n
                   for n in notes), notes
        assert _by_id(out, "G1").get("default") is None

    def test_inventory_shows_which_branch_is_the_default(self):
        out, _ = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "G1", "target": "E1", "condition": "оплачен"},
            {"op": "set_default", "gateway": "G1", "flow": "F2"},
        ])
        flows = {f["id"]: f for f in build_inventory(out)["flows"]}
        assert flows["F2"]["default"] is True
        assert "default" not in flows["F3"]


# ---------------------------------------------------------------------------
# Хронометраж таймера
# ---------------------------------------------------------------------------

class TestTimerSchedule:
    def test_duration_lands_in_time_duration(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC_wait", "name": "Ожидание паллеты",
            "event_type": "intermediateCatch", "participant": "Заказ",
            "after": "T_collect", "event_definition": "timer", "duration": "PT2H",
        }])
        assert report["status"] == "success", report["skipped"]
        definition = _by_id(out, "new_IC_wait").find("bpmn:timerEventDefinition", NS)
        assert definition.find("bpmn:timeDuration", NS).text == "PT2H"
        assert definition.find("bpmn:timeCycle", NS) is None

    def test_cycle_lands_in_time_cycle_for_a_short_event_type(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC_poll", "name": "Опрос остатков",
            "event_type": "timer", "participant": "Заказ", "cycle": "R3/PT10M",
        }, {
            "op": "connect", "source": "T_ship", "target": "new_IC_poll"}, {
            "op": "connect", "source": "new_IC_poll", "target": "End_ok"},
        ])
        assert report["status"] == "success", report["skipped"]
        definition = _by_id(out, "new_IC_poll").find("bpmn:timerEventDefinition", NS)
        assert definition.find("bpmn:timeCycle", NS).text == "R3/PT10M"
        assert definition.find("bpmn:timeDuration", NS) is None

    @staticmethod
    def _handled(boundary_op, handler_target="End_cancel"):
        """Граничное событие в пакете обязано вести ветку обработки: без неё
        аплайер откатывает и событие, и его шаг."""
        return [boundary_op, {
            "op": "add_task", "id": boundary_op["id"] + "_h", "name": "Обработка",
            "task_type": "userTask", "after": boundary_op["id"],
        }, {
            "op": "connect", "source": boundary_op["id"] + "_h",
            "target": handler_target,
        }]

    def test_boundary_timer_keeps_duration_and_says_it_in_the_note(
            self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self._handled({
            "op": "add_boundary_event", "id": "new_BE_sla",
            "attached_to": "T_collect", "event_type": "timer",
            "name": "Просрочка сборки", "duration": "pt45m",
        }))
        assert report["status"] == "success", report["skipped"]
        event = _by_id(out, "new_BE_sla")
        assert event.find("bpmn:timerEventDefinition/bpmn:timeDuration", NS).text == "PT45M"
        assert "длительность PT45M" in report["applied"][0]["note"]

    def test_boundary_timer_cycle(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self._handled({
            "op": "add_boundary_event", "id": "new_BE_poll",
            "attached_to": "T_ship", "event_type": "timer",
            "name": "Опрос каждые 10 минут", "cycle": "R5/PT10M",
        }))
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "new_BE_poll").find(
            "bpmn:timerEventDefinition/bpmn:timeCycle", NS).text == "R5/PT10M"

    def test_without_schedule_the_default_duration_is_used(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, self._handled({
            "op": "add_boundary_event", "id": "new_BE", "attached_to": "T_collect",
            "event_type": "timer", "name": "Таймер",
        }))
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "new_BE").find(
            "bpmn:timerEventDefinition/bpmn:timeDuration", NS).text == DEFAULT_TIMER_DURATION

    @pytest.mark.parametrize("duration", ["15 минут", "PT", "P", "2H", "P2H", "R3PT10M"])
    def test_broken_interval_is_refused_with_hint(self, single_pool_xml, duration):
        """Молча подставлять PT15M вместо битого значения нельзя: пользователь
        получил бы не тот SLA, который просил."""
        out, report = apply_operations(single_pool_xml, [{
            "op": "add_boundary_event", "id": "new_BE", "attached_to": "T_collect",
            "event_type": "timer", "name": "Таймер", "duration": duration,
        }])
        skip = report["skipped"][0]
        assert skip["reason"] == f"некорректный интервал таймера '{duration}'"
        assert "ISO-8601" in skip["hint"]
        assert _by_id(out, "new_BE") is None

    def test_duration_and_cycle_together_are_refused(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [{
            "op": "add_event", "id": "new_IC", "name": "Ожидание",
            "event_type": "timer", "participant": "Заказ",
            "duration": "PT10M", "cycle": "R2/PT10M",
        }])
        assert report["skipped"][0]["reason"] == "таймеру задают либо duration, либо cycle"

    def test_schedule_of_a_non_timer_event_is_refused(self, single_pool_xml):
        _, report = apply_operations(single_pool_xml, [
            {"op": "add_boundary_event", "id": "new_BE", "attached_to": "T_collect",
             "event_type": "error", "name": "Ошибка", "duration": "PT10M"},
            # Определение названо: иначе первым сработал бы отказ «событию нужно
            # определение», а здесь проверяется хронометраж не-таймера.
            {"op": "add_event", "id": "new_IC", "name": "Сообщение",
             "event_type": "intermediateCatch", "participant": "Заказ",
             "event_definition": "message", "duration": "PT10M"},
        ])
        assert [s["reason"] for s in report["skipped"]] == [
            "хронометраж задают только таймеру",
            "хронометраж задают только таймеру",
        ]
        assert "event_definition=timer" in report["skipped"][1]["hint"]


# ---------------------------------------------------------------------------
# Слияние пулов в дорожку и удаление пустых пулов
# ---------------------------------------------------------------------------

# Кладовщик и Склад — роли одной организации: messageFlow между ними и
# sequence-поток через границу пула. Слияние обязан превратить это в дорожки.
MERGE_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_merge">
  <collaboration id="Collaboration_m">
    <participant id="Pool_wh" name="Кладовщик" processRef="Process_wh"/>
    <participant id="Pool_shop" name="Склад" processRef="Process_shop"/>
    <messageFlow id="MF_handover" sourceRef="W_ship" targetRef="S_take"/>
  </collaboration>
  <process id="Process_wh" name="Кладовщик" isExecutable="true">
    <laneSet id="LaneSet_wh"><lane id="Lane_wh" name="Кладовщик">
      <flowNodeRef>W_pick</flowNodeRef></lane></laneSet>
    <startEvent id="W_start" name="Заявка"><outgoing>WF1</outgoing></startEvent>
    <sequenceFlow id="WF1" sourceRef="W_start" targetRef="W_pick"/>
    <userTask id="W_pick" name="Собрать груз">
      <incoming>WF1</incoming><outgoing>WF2</outgoing><outgoing>XF</outgoing></userTask>
    <sequenceFlow id="WF2" sourceRef="W_pick" targetRef="W_ship"/>
    <serviceTask id="W_ship" name="Передать на приёмку">
      <incoming>WF2</incoming><outgoing>WF3</outgoing></serviceTask>
    <sequenceFlow id="WF3" sourceRef="W_ship" targetRef="W_end"/>
    <endEvent id="W_end" name="Груз передан"><incoming>WF3</incoming></endEvent>
    <sequenceFlow id="XF" sourceRef="W_pick" targetRef="S_start"/>
  </process>
  <process id="Process_shop" name="Склад" isExecutable="true">
    <startEvent id="S_start" name="Приёмка начата"><outgoing>SF1</outgoing></startEvent>
    <sequenceFlow id="SF1" sourceRef="S_start" targetRef="S_take"/>
    <userTask id="S_take" name="Принять груз">
      <incoming>SF1</incoming><outgoing>SF2</outgoing></userTask>
    <sequenceFlow id="SF2" sourceRef="S_take" targetRef="S_end"/>
    <endEvent id="S_end" name="Груз на складе"><incoming>SF2</incoming></endEvent>
  </process>
</definitions>"""

# То же, но шаг источника не встроен в маршрут: после слияния он повиснет
# внутри главного пула, и пакет обязан откатиться.
MERGE_FLOATING_XML = MERGE_XML.replace(
    '<sequenceFlow id="XF"',
    '<userTask id="W_extra" name="Заказать паллету"/>\n    <sequenceFlow id="XF"')
MERGE_FLOATING_XML = MERGE_FLOATING_XML.replace(
    '<messageFlow id="MF_handover"',
    '<messageFlow id="MF_extra" sourceRef="W_extra" targetRef="S_take"/>\n'
    '    <messageFlow id="MF_handover"')

MERGE_OP = {"op": "merge_participants", "source": "Кладовщик",
            "target": "Склад", "as_lane": True}

EMPTY_POOL_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_pools">
  <collaboration id="Collaboration_p">
    <participant id="Pool_main" name="Склад" processRef="Process_main"/>
    <participant id="Pool_free" name="Экспедитор" processRef="Process_free"/>
    <participant id="Pool_guest" name="Получатель" processRef="Process_guest"/>
    <messageFlow id="MF_order" sourceRef="M_ship" targetRef="G_take"/>
  </collaboration>
  <process id="Process_main" name="Склад" isExecutable="true">
    <startEvent id="M_start" name="Заявка"><outgoing>MF1</outgoing></startEvent>
    <sequenceFlow id="MF1" sourceRef="M_start" targetRef="M_pick"/>
    <userTask id="M_pick" name="Собрать груз"><incoming>MF1</incoming><outgoing>MF2</outgoing></userTask>
    <sequenceFlow id="MF2" sourceRef="M_pick" targetRef="M_ship"/>
    <serviceTask id="M_ship" name="Отправить"><incoming>MF2</incoming><outgoing>MF3</outgoing></serviceTask>
    <sequenceFlow id="MF3" sourceRef="M_ship" targetRef="M_end"/>
    <endEvent id="M_end" name="Отгружено"><incoming>MF3</incoming></endEvent>
  </process>
  <process id="Process_free" name="Экспедитор" isExecutable="true">
    <startEvent id="F_start" name="Старт"><outgoing>FF1</outgoing></startEvent>
    <sequenceFlow id="FF1" sourceRef="F_start" targetRef="F_end"/>
    <endEvent id="F_end" name="Завершение"><incoming>FF1</incoming></endEvent>
  </process>
  <process id="Process_guest" name="Получатель" isExecutable="true">
    <startEvent id="G_start" name="Старт"><outgoing>GF1</outgoing></startEvent>
    <sequenceFlow id="GF1" sourceRef="G_start" targetRef="G_take"/>
    <userTask id="G_take" name="Принять груз"><incoming>GF1</incoming><outgoing>GF2</outgoing></userTask>
    <sequenceFlow id="GF2" sourceRef="G_take" targetRef="G_end"/>
    <endEvent id="G_end" name="Получено"><incoming>GF2</incoming></endEvent>
  </process>
</definitions>"""

EMPTY_LINKED_POOL_XML = EMPTY_POOL_XML.replace(
    '<sequenceFlow id="FF1" sourceRef="F_start" targetRef="F_end"/>',
    '<sequenceFlow id="FF1" sourceRef="F_start" targetRef="F_end"/>\n'
    '    <messageFlow id="MF_free" sourceRef="M_pick" targetRef="F_start"/>')


class TestCrossPoolLegEnds:
    """Один стандарт для межпуловой дуги: сообщение не бывает с шлюзом на конце.
    Починка превращала ЛЮБУЮ межпуловую дугу в messageFlow, не глядя на концы, а
    аплайер операций для пары «задача → шлюз» отказывался её создавать — два
    разных ответа на один и тот же дефект в одном контуре."""

    XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_gw">
  <collaboration id="Collaboration_g">
    <participant id="Pool_a" name="Заказчик" processRef="Process_a"/>
    <participant id="Pool_b" name="Подрядчик" processRef="Process_b"/>
  </collaboration>
  <process id="Process_a" name="Заказчик" isExecutable="true">
    <startEvent id="A_start" name="Заявка"><outgoing>AF1</outgoing></startEvent>
    <sequenceFlow id="AF1" sourceRef="A_start" targetRef="A_ask"/>
    <userTask id="A_ask" name="Запросить подтверждение">
      <incoming>AF1</incoming><outgoing>AF2</outgoing><outgoing>AF_X</outgoing></userTask>
    <sequenceFlow id="AF2" sourceRef="A_ask" targetRef="A_end"/>
    <endEvent id="A_end" name="Ответ получен"><incoming>AF2</incoming></endEvent>
    <sequenceFlow id="AF_X" sourceRef="A_ask" targetRef="B_gw"/>
  </process>
  <process id="Process_b" name="Подрядчик" isExecutable="true">
    <startEvent id="B_start" name="Старт"><outgoing>BF1</outgoing></startEvent>
    <sequenceFlow id="BF1" sourceRef="B_start" targetRef="B_gw"/>
    <exclusiveGateway id="B_gw" name="Есть машина?">
      <incoming>BF1</incoming><incoming>AF_X</incoming>
      <outgoing>BF2</outgoing><outgoing>BF3</outgoing></exclusiveGateway>
    <sequenceFlow id="BF2" sourceRef="B_gw" targetRef="B_yes"/>
    <userTask id="B_yes" name="Подтвердить наряд">
      <incoming>BF2</incoming><outgoing>BF4</outgoing></userTask>
    <sequenceFlow id="BF4" sourceRef="B_yes" targetRef="B_end"/>
    <endEvent id="B_end" name="Наряд подтверждён"><incoming>BF4</incoming></endEvent>
    <sequenceFlow id="BF3" sourceRef="B_gw" targetRef="B_no"/>
    <userTask id="B_no" name="Отказать">
      <incoming>BF3</incoming><outgoing>BF5</outgoing></userTask>
    <sequenceFlow id="BF5" sourceRef="B_no" targetRef="B_end"/>
  </process>
</definitions>"""

    def test_gateway_endpoint_is_dropped_not_turned_into_a_message(self):
        out, notes = validate_and_repair(self.XML)
        root = _root(out)
        assert [f.get("id") for f in
                root.findall(".//bpmn:messageFlow", NS)
                if "B_gw" in (f.get("sourceRef"), f.get("targetRef"))] == []
        assert [f for f in root.findall(".//bpmn:sequenceFlow", NS)
                if f.get("id") == "AF_X"] == []
        assert any("AF_X" in n and "не бывает потоком-сообщением" in n
                   for n in notes), notes

    def test_dropped_leg_leaves_no_dangling_reference(self):
        out, _notes = validate_and_repair(self.XML)
        assert "<incoming>AF_X</incoming>" not in out
        assert "<outgoing>AF_X</outgoing>" not in out


class TestMergeParticipants:
    def test_pool_becomes_a_lane_of_the_target_process(self):
        out, report = apply_operations(MERGE_XML, [MERGE_OP])
        assert report["status"] == "success", report["skipped"]
        root = _root(out)
        assert root.find(".//bpmn:participant[@id='Pool_wh']", NS) is None
        assert _process(root, "Process_wh") is None
        assert root.find(".//bpmn:participant[@id='Pool_shop']", NS) is not None

        shop = _process(root, "Process_shop")
        moved = [child.get("id") for child in shop]
        assert {"W_start", "W_pick", "W_ship", "W_end"} <= set(moved)
        # потоки едут вместе с узлами: межпулового sequence-потока больше нет
        assert {"WF1", "WF2", "WF3", "XF"} <= set(moved)
        assert _flow(root, "XF") is not None
        assert _flow(root, "XF").get("sourceRef") == "W_pick"
        # messageFlow между сливаемыми пулами удалён, чужие остаются
        assert root.find(".//bpmn:messageFlow[@id='MF_handover']", NS) is None
        # дорожка называется именем пула-источника, ссылка — на его узлы
        lane = _process(root, "Process_shop").find("bpmn:laneSet/bpmn:lane", NS)
        assert lane.get("name") == "Кладовщик"
        assert _lane_refs(lane) == ["W_start", "W_pick", "W_ship", "W_end"]
        assert root.find(".//bpmn:lane[@id='Lane_wh']", NS) is None
        # ссылки узлов на потоки целы
        assert _refs(_by_id(out, "W_pick"), "incoming") == ["WF1"]
        assert _refs(_by_id(out, "W_pick"), "outgoing") == ["WF2", "XF"]

    def test_merged_scheme_survives_the_repair(self):
        out, report = apply_operations(MERGE_XML, [MERGE_OP])
        assert report["status"] == "success", report["skipped"]
        repaired, notes = validate_and_repair(out)
        # Фикстура намеренно «склейка ролей-пулов» sequence-потоком в стартовое
        # событие другого пула: после слияния такой поток внутри процесса
        # невалиден, и починка убирает его, а не оставляет битый XML.
        assert all("недопустимый поток" in n for n in notes), notes
        inventory = build_inventory(repaired)
        assert [p["name"] for p in inventory["participants"]] == ["Склад"]
        assert {"W_pick", "S_take"} <= {e["id"] for e in inventory["elements"]}
        assert {f["id"] for f in inventory["flows"]} == {
            "WF1", "WF2", "WF3", "SF1", "SF2"}
        assert _dangling(repaired) == []
        # Идемпотентность: убранное не плодится при повторном проходе.
        assert validate_and_repair(repaired)[1] == []

    def test_lane_name_can_be_given(self):
        out, report = apply_operations(MERGE_XML, [
            {**MERGE_OP, "as_lane": "Кладовщик-сборщик", "source": "Process_wh"}])
        assert report["status"] == "success", report["skipped"]
        lane = _root(out).find(".//bpmn:laneSet/bpmn:lane", NS)
        assert lane.get("name") == "Кладовщик-сборщик"
        assert _process(_root(out), "Process_wh") is None

    def test_merge_of_a_floating_step_is_rolled_back_with_the_package(self):
        out, report = apply_operations(MERGE_FLOATING_XML, [
            {"op": "rename", "id": "W_pick", "name": "Собрать и упаковать"},
            MERGE_OP,
        ])
        assert out == MERGE_FLOATING_XML, "пакет обязан откатиться к XML до изменений"
        assert report["status"] == "failed"
        assert report["applied"] == []
        skip = report["skipped"][0]
        assert skip["op"] == "merge_participants"
        assert "слияние пулов сделало схему невалидной" in skip["reason"]
        assert "W_extra" in skip["reason"]
        assert "connect" in skip["hint"]

    def test_merge_passes_once_the_source_steps_are_routed(self):
        """Подсказка из отказа работает: встроенные в маршрут шаги сливаются."""
        out, report = apply_operations(MERGE_FLOATING_XML, [
            {"op": "connect", "source": "W_start", "target": "W_extra"},
            {"op": "connect", "source": "W_extra", "target": "W_end"},
            MERGE_OP,
        ])
        assert report["status"] == "success", report["skipped"]
        assert _by_id(out, "W_extra") is not None
        assert _root(out).find(".//bpmn:participant[@id='Pool_wh']", NS) is None
        # Единственная оставшаяся правка починки — исходный дефект фикстуры:
        # межпуловой XF вёл в стартовое событие, и после слияния это поток
        # внутри одного процесса в старт. Его аплайер убирает, схема после
        # второго прохода целая.
        notes = validate_and_repair(out)[1]
        assert notes and all("недопустимый поток" in n for n in notes)
        assert any("XF" in n for n in notes)
        assert validate_and_repair(validate_and_repair(out)[0])[1] == []

    def test_unknown_pool_is_refused(self):
        _, report = apply_operations(MERGE_XML, [
            {**MERGE_OP, "source": "Транспортная компания"}])
        skip = report["skipped"][0]
        assert skip["reason"] == "пул 'Транспортная компания' не найден (источник)"
        assert "инвентар" in skip["hint"]

        _, missing = apply_operations(MERGE_XML, [{"op": "merge_participants",
                                                  "target": "Склад"}])
        assert missing["skipped"][0]["reason"] == "не задан пул (источник)"

    def test_target_must_be_a_registered_pool(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_bare">
  <collaboration id="C_bare">
    <participant id="Pool_a" name="Пул А" processRef="PrA"/>
  </collaboration>
  <process id="PrA" name="Пул А" isExecutable="true">
    <startEvent id="A1" name="Старт"><outgoing>AF1</outgoing></startEvent>
    <sequenceFlow id="AF1" sourceRef="A1" targetRef="A2"/>
    <userTask id="A2" name="Шаг"><incoming>AF1</incoming><outgoing>AF2</outgoing></userTask>
    <sequenceFlow id="AF2" sourceRef="A2" targetRef="A3"/>
    <endEvent id="A3" name="Финиш"><incoming>AF2</incoming></endEvent>
  </process>
  <process id="PrB" name="Пул Б" isExecutable="true">
    <startEvent id="B1" name="Старт"><outgoing>BF1</outgoing></startEvent>
    <sequenceFlow id="BF1" sourceRef="B1" targetRef="B2"/>
    <userTask id="B2" name="Шаг"><incoming>BF1</incoming><outgoing>BF2</outgoing></userTask>
    <sequenceFlow id="BF2" sourceRef="B2" targetRef="B3"/>
    <endEvent id="B3" name="Финиш"><incoming>BF2</incoming></endEvent>
  </process>
</definitions>"""
        _, report = apply_operations(xml, [{"op": "merge_participants",
                                            "source": "Пул А", "target": "Пул Б"}])
        assert report["skipped"][0]["reason"] == \
            "'Пул Б' не является пулом (приёмник): процесс не заявлен участником в коллаборации"

    def test_merging_a_pool_into_itself_is_refused(self):
        _, report = apply_operations(MERGE_XML, [
            {**MERGE_OP, "source": "Склад"}])
        assert report["skipped"][0]["reason"] == "пул нельзя слить в самого себя"

    def test_lane_is_mandatory(self):
        _, report = apply_operations(MERGE_XML, [{**MERGE_OP, "as_lane": False}])
        assert report["skipped"][0]["reason"] == \
            "сливать пул можно только в дорожку целевого пула"
        _, blank = apply_operations(MERGE_XML, [{**MERGE_OP, "as_lane": "  "}])
        assert blank["skipped"][0]["reason"] == "не задано имя дорожки"

    def test_empty_pool_is_not_merged_but_removed(self):
        _, report = apply_operations(EMPTY_POOL_XML, [{
            "op": "merge_participants", "source": "Экспедитор",
            "target": "Склад", "as_lane": True}])
        assert report["skipped"][0]["reason"] == "в пуле «Экспедитор» нет ни одного шага"
        assert "remove_participant" in report["skipped"][0]["hint"]

    def test_foreign_nodes_block_the_merge(self):
        """Узел внутри пула принадлежит другому процессу: переносить его в
        чужую дорожку нельзя, иначе он исчезнет вместе с источником."""
        xml = MERGE_XML.replace(
            '    <sequenceFlow id="XF" sourceRef="W_pick" targetRef="S_start"/>',
            '    <sequenceFlow id="XF" sourceRef="W_pick" targetRef="S_start"/>\n'
            '    <process id="Process_nested" name="Вложенный">'
            '<userTask id="N_task" name="Чужой шаг"/></process>')
        _, report = apply_operations(xml, [MERGE_OP])
        skip = report["skipped"][0]
        assert "принадлежащие чужому процессу: N_task" in skip["reason"]
        assert "move_to_participant" in skip["hint"]
        assert _by_id(xml, "Process_nested") is not None


class TestRemoveParticipant:
    def test_empty_pool_disappears_with_its_process(self):
        out, report = apply_operations(EMPTY_POOL_XML, [
            {"op": "remove_participant", "participant": "Экспедитор"}])
        assert report["status"] == "success", report["skipped"]
        root = _root(out)
        assert root.find(".//bpmn:participant[@id='Pool_free']", NS) is None
        assert _process(root, "Process_free") is None
        assert _by_id(out, "F_start") is None
        assert _by_id(out, "F_end") is None
        assert _flow(root, "FF1") is None
        assert "пустой пул «Экспедитор» удалён" in report["applied"][0]["note"]
        # соседние пулы целы
        inventory = build_inventory(out)
        assert {p["name"] for p in inventory["participants"]} == {"Склад", "Получатель"}
        assert validate_and_repair(out)[1] == []

    def test_pool_with_steps_is_not_deletable(self):
        out, report = apply_operations(EMPTY_POOL_XML, [
            {"op": "remove_participant", "participant": "Получатель"}])
        skip = report["skipped"][0]
        assert skip["reason"] == \
            "в пуле «Получатель» есть шаги (G_take) — удалять нельзя"
        assert "merge_participants" in skip["hint"]
        # отказ ничего не трогает: пул цел
        assert _by_id(out, "G_take") is not None
        assert _root(out).find(".//bpmn:participant[@id='Pool_guest']", NS) is not None

    def test_events_only_pool_with_a_task_like_gateway_is_not_deletable(self):
        """Шлюз — не «только старт и финиш»: такой пул содержит ветвление,
        и удалять его нельзя."""
        xml = EMPTY_POOL_XML.replace(
            '<sequenceFlow id="FF1" sourceRef="F_start" targetRef="F_end"/>',
            '<sequenceFlow id="FF1" sourceRef="F_start" targetRef="F_gate"/>\n'
            '    <exclusiveGateway id="F_gate" name="Проверка">\n'
            '      <outgoing>FF2</outgoing><outgoing>FF3</outgoing></exclusiveGateway>\n'
            '    <sequenceFlow id="FF2" sourceRef="F_gate" targetRef="F_end">\n'
            '      <conditionExpression>a</conditionExpression></sequenceFlow>\n'
            '    <sequenceFlow id="FF3" sourceRef="F_gate" targetRef="F_end">\n'
            '      <conditionExpression>b</conditionExpression></sequenceFlow>')
        _, report = apply_operations(xml, [
            {"op": "remove_participant", "participant": "Экспедитор"}])
        assert "есть шаги (F_gate)" in report["skipped"][0]["reason"]

    def test_pool_linked_with_others_is_not_deletable(self):
        _, report = apply_operations(EMPTY_LINKED_POOL_XML, [
            {"op": "remove_participant", "participant": "Экспедитор"}])
        skip = report["skipped"][0]
        assert skip["reason"] == \
            "пул «Экспедитор» связан с другими пулами потоками MF_free"
        assert "disconnect" in skip["hint"]

    def test_unknown_pool_and_bare_process_are_refused(self):
        _, unknown = apply_operations(EMPTY_POOL_XML, [
            {"op": "remove_participant", "participant": "Курьер"}])
        assert unknown["skipped"][0]["reason"] == "пул 'Курьер' не найден"
        _, bare = apply_operations(LANES_XML, [
            {"op": "remove_participant", "participant": "Заказ"}])
        assert bare["skipped"][0]["reason"] == \
            "'Заказ' не является пулом: процесс не заявлен участником в коллаборации"
        _, empty = apply_operations(EMPTY_POOL_XML, [{"op": "remove_participant"}])
        assert empty["skipped"][0]["reason"] == "не задан пул"


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_output_is_importable_by_bpmn_js(self, single_pool_xml):
        out, _ = apply_operations(single_pool_xml, [
            {"op": "add_task", "id": "new_T1", "name": "Шаг", "after": "T_collect"},
        ])
        assert out.startswith(XML_DECLARATION)
        # bpmn-js принимает и префиксный, и дефолтный вариант, но не численные
        # префиксы вроде ns0:, которые порождает ET без register_namespace.
        assert "ns0:" not in out
        assert 'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"' in out
        root = ET.fromstring(out)
        assert root.find(".//bpmn:process", NS) is not None
        assert root.find(".//bpmn:*[@id='new_T1']", NS) is not None

    def test_empty_operation_list_is_a_noop(self, single_pool_xml):
        out, report = apply_operations(single_pool_xml, [])
        assert report == {"status": "success", "applied": [], "skipped": []}
        assert build_inventory(out) == build_inventory(single_pool_xml)


def test_stranded_addition_falls_out_of_the_repaired_scheme():
    """Откат узла вне маршрута обязан работать и по финальному XML: в прогоне
    #40 аплайер принял пакет, у которого семантическая починка сняла дугу, и
    схема потеряла 15 баллов на `boundary_handled`. Здесь дуга уже снята —
    проверяется, что узел уходит из схемы, а не остаётся кружком без ветки."""
    xml = TestFlowEndsAndAttachments.XML.replace(
        '<sequenceFlow id="AF3" sourceRef="A_timer" targetRef="A_escalate"/>',
        "").replace("<outgoing>AF3</outgoing>", "")
    out, dropped = rollback_stranded(
        xml, {"A_timer": "add_boundary_event", "A_escalate": "add_task"})

    assert 'id="A_timer"' not in out and 'id="A_escalate"' not in out
    assert [(d["id"], d["op"]) for d in dropped] == [
        ("A_timer", "add_boundary_event"), ("A_escalate", "add_task")]
    assert "без ветки обработки" in dropped[0]["gap"]
    assert "add_boundary_event" in dropped[0]["hint"] or "перевставить" in dropped[0]["hint"]
    # Легальный маршрут пакета не тронут: откат касается только названных узлов.
    assert 'id="AF1"' in out and 'id="A_sub"' in out


class TestFlowEndsAndAttachments:
    """Концы потока и attachedToRef — то, чего не видел ни скоринг, ни оракул.

    Фикстура намеренно легальная: правки аплайера не имеют права трогать
    корректную схему, и первый тест это фиксирует.
    """

    XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_int">
  <collaboration id="Collaboration_i">
    <participant id="Pool_a" name="Склад" processRef="Process_a"/>
    <participant id="Pool_b" name="Перевозчик" processRef="Process_b"/>
    <messageFlow id="MF_hand" sourceRef="A_ship" targetRef="B_start"/>
  </collaboration>
  <process id="Process_a" name="Склад" isExecutable="true">
    <laneSet id="LaneSet_a"><lane id="Lane_a" name="Кладовщик">
      <flowNodeRef>A_ship</flowNodeRef></lane></laneSet>
    <startEvent id="A_start" name="Заявка"><outgoing>AF1</outgoing></startEvent>
    <sequenceFlow id="AF1" sourceRef="A_start" targetRef="A_ship"/>
    <userTask id="A_ship" name="Передать груз">
      <documentation>Передача под подпись</documentation>
      <incoming>AF1</incoming><outgoing>AF2</outgoing></userTask>
    <sequenceFlow id="AF2" sourceRef="A_ship" targetRef="A_end"/>
    <boundaryEvent id="A_timer" name="Просрочка" attachedToRef="A_ship">
      <timerEventDefinition><timeDuration>PT2H</timeDuration></timerEventDefinition>
      <outgoing>AF3</outgoing></boundaryEvent>
    <sequenceFlow id="AF3" sourceRef="A_timer" targetRef="A_escalate"/>
    <userTask id="A_escalate" name="Эскалировать">
      <incoming>AF3</incoming><outgoing>AF4</outgoing></userTask>
    <sequenceFlow id="AF4" sourceRef="A_escalate" targetRef="A_sub"/>
    <subProcess id="A_sub" name="Приёмка">
      <incoming>AF4</incoming><outgoing>AF5</outgoing>
      <startEvent id="AS_start" name="Вход"><outgoing>ASF1</outgoing></startEvent>
      <sequenceFlow id="ASF1" sourceRef="AS_start" targetRef="AS_check"/>
      <userTask id="AS_check" name="Проверить пломбы">
        <incoming>ASF1</incoming><outgoing>ASF2</outgoing></userTask>
      <sequenceFlow id="ASF2" sourceRef="AS_check" targetRef="AS_end"/>
      <endEvent id="AS_end" name="Выход"><incoming>ASF2</incoming></endEvent>
    </subProcess>
    <sequenceFlow id="AF5" sourceRef="A_sub" targetRef="A_end"/>
    <endEvent id="A_end" name="Готово"><incoming>AF5</incoming></endEvent>
  </process>
  <process id="Process_b" name="Перевозчик" isExecutable="true">
    <startEvent id="B_start" name="Груз передан"><outgoing>BF1</outgoing></startEvent>
    <sequenceFlow id="BF1" sourceRef="B_start" targetRef="B_take"/>
    <userTask id="B_take" name="Принять в кузов">
      <incoming>BF1</incoming><outgoing>BF2</outgoing></userTask>
    <sequenceFlow id="BF2" sourceRef="B_take" targetRef="B_end"/>
    <endEvent id="B_end" name="Доставка начата"><incoming>BF2</incoming></endEvent>
  </process>
</definitions>"""

    # Межпуловая «линия маршрута» — болезнь раздутых ролей-пулов: лечится
    # превращением в сообщение, а не удалением.
    CROSS_FLOW_XML = XML.replace(
        '<messageFlow id="MF_hand"',
        '<sequenceFlow id="XF_cross" sourceRef="A_escalate" targetRef="B_take"/>\n'
        '    <messageFlow id="MF_hand"')

    # Тот же поток, но внутри одного процесса: в стартовое событие он не входит.
    INTO_START_XML = XML.replace(
        '<messageFlow id="MF_hand"',
        '<sequenceFlow id="XF_bad" sourceRef="A_ship" targetRef="A_start"/>\n'
        '    <messageFlow id="MF_hand"')

    # По BPMN 2.0 конец потока-сообщения — это и узел, и участник целиком; так
    # рисует обмен Signavio, когда получатель внутри пула не размечен.
    POOL_END_XML = XML.replace(
        '<messageFlow id="MF_hand" sourceRef="A_ship" targetRef="B_start"/>',
        '<messageFlow id="MF_hand" sourceRef="Pool_a" targetRef="B_start"/>\n'
        '    <messageFlow id="MF_back" sourceRef="B_take" targetRef="Pool_a"/>')

    BROKEN_POOL_END_XML = POOL_END_XML.replace(
        '<messageFlow id="MF_back" sourceRef="B_take" targetRef="Pool_a"/>',
        '<messageFlow id="MF_back" sourceRef="B_take" targetRef="Pool_zzz"/>')

    def test_message_flow_may_end_on_the_participant_itself(self):
        """До фикса починка звала такой конец «висящим» и резала обмен: по
        корпусу это 52 схемы из 88 с messageFlow, и у каждой исчезавшая дуга
        роняла `participant_interacts` с passed на failed."""
        repaired, notes = validate_and_repair(self.POOL_END_XML)
        assert notes == []
        ids = {e.get("id") for e in _root(repaired).iter() if e.get("id")}
        assert {"MF_hand", "MF_back"} <= ids

    def test_message_flow_to_an_absent_pool_is_still_removed(self):
        """Разрешить пул — не значит перестать замечать несуществующий id."""
        _, notes = validate_and_repair(self.BROKEN_POOL_END_XML)
        assert any("MF_back" in note for note in notes)

    def test_legal_scheme_is_not_touched(self):
        repaired, notes = validate_and_repair(self.XML)
        assert notes == []
        before = sorted(e.get("id") for e in _root(self.XML).iter() if e.get("id"))
        after = sorted(e.get("id") for e in _root(repaired).iter() if e.get("id"))
        assert before == after

    def test_connect_into_start_and_boundary_is_refused(self):
        for target, hint in (("A_start", "стартовое"), ("A_timer", "граничное")):
            _, report = apply_operations(self.XML, [
                {"op": "connect", "source": "A_escalate", "target": target}])
            skip = report["skipped"][0]
            assert skip["op"] == "connect"
            assert target in skip["reason"]
            assert hint in skip["hint"]

    def test_connect_out_of_end_event_is_refused(self):
        _, report = apply_operations(self.XML, [
            {"op": "connect", "source": "A_end", "target": "A_escalate"}])
        assert "исходящего потока не бывает" in report["skipped"][0]["reason"]

    def test_message_flow_may_trigger_foreign_start(self):
        """Сообщение в чужое стартовое событие — легальный запуск пула."""
        _, report = apply_operations(self.XML, [
            {"op": "connect", "source": "A_escalate", "target": "B_start",
             "flow_type": "message"}])
        assert report["status"] == "success", report["skipped"]

    def test_insert_after_end_event_is_refused(self):
        _, report = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_T", "name": "Шаг",
             "participant": "Склад", "after": "A_end"}])
        skip = report["skipped"][0]
        assert "A_end" in skip["reason"] and "не будет" in skip["reason"]
        assert "из конечного события" in skip["hint"]

    def test_cross_pool_sequence_becomes_message_flow(self):
        repaired, notes = validate_and_repair(self.CROSS_FLOW_XML)
        assert any("стал потоком-сообщением" in n for n in notes), notes
        root = _root(repaired)
        moved = root.find(".//bpmn:collaboration/bpmn:messageFlow[@id='XF_cross']", NS)
        assert moved is not None and moved.get("sourceRef") == "A_escalate"
        # Идемпотентно: сообщение в чужой процесс больше не правится.
        assert validate_and_repair(repaired)[1] == []

    def test_sequence_into_start_inside_one_pool_is_dropped(self):
        repaired, notes = validate_and_repair(self.INTO_START_XML)
        assert any("недопустимый поток XF_bad" in n for n in notes), notes
        assert _flow(_root(repaired), "XF_bad") is None
        assert validate_and_repair(repaired)[1] == []

    def test_delete_takes_attached_boundary_with_it(self):
        out, report = apply_operations(self.XML, [{"op": "delete", "id": "A_ship"}])
        assert report["status"] == "success", report["skipped"]
        applied = report["applied"][0]
        assert "A_timer" in applied["note"]
        assert _by_id(out, "A_timer") is None
        assert "attachedToRef" not in ET.tostring(_root(out), encoding="unicode")

    def test_move_carries_boundary_into_target_pool(self):
        out, report = apply_operations(self.XML, [
            {"op": "move_to_participant", "id": "A_ship",
             "participant": "Перевозчик"}])
        assert report["status"] == "success", report["skipped"]
        assert "граничные события перенесены" in report["applied"][0]["note"]
        root = _root(out)
        assert _process(root, "Process_b").find("bpmn:boundaryEvent", NS) is not None
        assert _process(root, "Process_a").find("bpmn:boundaryEvent", NS) is None

    def test_orphan_boundary_is_demoted_or_removed(self):
        """Импортный XML с attachedToRef вникуда: с определением — промежуточное
        событие, без определения — удаляется."""
        with_def = self.XML.replace('<messageFlow id="MF_hand"',
                                    '<boundaryEvent id="B_orphan" name="Сирота" '
                                    'attachedToRef="нет_такого"><timerEventDefinition/>'
                                    '</boundaryEvent>\n    <messageFlow id="MF_hand"')
        repaired, notes = validate_and_repair(with_def)
        assert any("осталось без хозяина — переведено" in n for n in notes), notes
        assert _by_id(repaired, "B_orphan").tag == f"{{{BPMN_NS}}}intermediateCatchEvent"

        no_def = self.XML.replace('<messageFlow id="MF_hand"',
                                  '<boundaryEvent id="B_hollow" name="Пустышка" '
                                  'attachedToRef="нет_такого"/>\n    '
                                  '<messageFlow id="MF_hand"')
        repaired, notes = validate_and_repair(no_def)
        assert any("без определения — удалено" in n for n in notes), notes
        assert _by_id(repaired, "B_hollow") is None

    def test_duplicate_documentation_is_not_stacked(self):
        out, report = apply_operations(self.XML, [
            {"op": "add_documentation", "id": "A_ship",
             "text": "Передача под подпись"}])
        assert "такая документация у элемента уже есть" in report["applied"][0]["note"]
        docs = _by_id(out, "A_ship").findall("bpmn:documentation", NS)
        assert len(docs) == 1

    def test_move_to_current_lane_reports_noop(self):
        _, report = apply_operations(self.XML, [
            {"op": "move_to_lane", "id": "A_ship", "lane": "Lane_a"}])
        assert "уже стоит в этой дорожке" in report["applied"][0]["note"]

    def test_second_boundary_of_same_kind_is_refused(self):
        _, report = apply_operations(self.XML, [
            {"op": "add_boundary_event", "id": "new_Be", "attached_to": "A_ship",
             "event_type": "timer", "name": "Ещё один срок"}])
        reason = report["skipped"][0]["reason"]
        assert "уже прицеплено timer-событие" in reason and "A_timer" in reason

        # Событие другого типа легально, но обязано получить ветку обработки:
        # без неё пакет откатывает его как висячий (правило было и раньше).
        boundary = [{"op": "add_boundary_event", "id": "new_Be",
                     "attached_to": "A_ship", "event_type": "error",
                     "name": "Отказ"}]
        _, without_branch = apply_operations(self.XML, boundary)
        assert without_branch["status"] == "failed"

        out, other = apply_operations(self.XML, boundary + [
            {"op": "add_task", "id": "new_Esc", "name": "Вернуть заявку",
             "participant": "Склад"},
            {"op": "connect", "source": "new_Be", "target": "new_Esc"},
            {"op": "connect", "source": "new_Esc", "target": "A_end"}])
        assert other["status"] == "success", other["skipped"]
        assert _by_id(out, "new_Be").get("attachedToRef") == "A_ship"

    def test_failed_merge_reports_rolled_back_operations(self):
        """Откат пакета не должен стирать уже «применённые» правки из отчёта."""
        _, report = apply_operations(MERGE_FLOATING_XML, [
            {"op": "rename", "id": "W_pick", "name": "Собрать и упаковать"},
            MERGE_OP])
        assert report["status"] == "failed"
        assert report["applied"] == []
        rolled = [s for s in report["skipped"] if s["op"] == "rename"]
        assert rolled and "откачено вместе с пакетом" in rolled[0]["reason"]

    def test_inventory_shows_lane_attachment_definition_and_nesting(self):
        inventory = build_inventory(self.XML)
        by_id = {e["id"]: e for e in inventory["elements"]}
        assert by_id["A_ship"]["lane"] == "Lane_a"
        assert "documentation" in by_id["A_ship"]
        assert by_id["A_timer"]["attached_to"] == "A_ship"
        assert by_id["A_timer"]["definition"] == "timer"
        assert by_id["A_timer"]["timer"] == "PT2H"
        assert by_id["AS_check"]["subprocess"] == "A_sub"
        assert "subprocess" not in by_id["A_ship"]

    def test_inventory_is_capped_and_stays_referentially_whole(self):
        inventory = build_inventory(_wide_xml(300))
        assert len(inventory["elements"]) == INVENTORY_MAX_ELEMENTS
        assert inventory["limits"]["elements_omitted"] > 0
        known = {e["id"] for e in inventory["elements"]} | {
            p["id"] for p in inventory["participants"]}
        assert all(f["source"] in known and f["target"] in known
                   for f in inventory["flows"])

    def test_empty_pool_is_not_an_unrouted_node(self):
        """Пустой пул нельзя «присоединить connect» — у него свой блок подсказок."""
        assert "остался без шагов" in POOL_EMPTY_NOTE_MARKERS
        assert "остался без шагов" not in UNROUTED_NOTE_MARKERS

    def test_new_node_can_be_placed_in_a_lane(self):
        out, report = apply_operations(self.XML, [
            {"op": "add_lane", "id": "new_L", "name": "Водитель",
             "participant": "Склад"},
            {"op": "add_task", "id": "new_T", "name": "Загрузить паллету",
             "participant": "Склад", "lane": "new_L", "after": "A_ship"}])
        assert report["status"] == "success", report["skipped"]
        assert "new_T" in _lane_refs(_by_id(out, "new_L"))

    def test_unknown_and_foreign_pool_lanes_are_refused_before_creation(self):
        _, missing = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_T", "name": "Шаг",
             "participant": "Склад", "lane": "Нет_такой_дорожки"}])
        assert "дорожка" in missing["skipped"][0]["reason"]

        # Дорожка чужого пула: узел не должен остаться в процессе без дорожки
        # только потому, что отказ случился после его создания.
        foreign_out, foreign = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_T", "name": "Шаг",
             "participant": "Перевозчик", "lane": "Lane_a"}])
        assert "другому пулу" in foreign["skipped"][0]["reason"]
        assert _by_id(foreign_out, "new_T") is None

    def test_add_task_cannot_create_an_empty_subprocess(self):
        assert "subProcess" not in OP_SPEC["add_task"]
        _, report = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_S", "name": "Подпроцесс",
             "participant": "Склад", "task_type": "subProcess"}])
        assert "нечем наполнить" in report["skipped"][0]["hint"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestPoolNamesAndRouteIntegrity:
    """Два дефекта живого прогона, из-за которых пакет или не доезжал до схемы,
    или доезжал и портил её (−16 баллов на production_incident)."""

    XML = TestFlowEndsAndAttachments.XML

    @staticmethod
    def _pool_of(xml_text, elem_id):
        return {e["id"]: e["participant"]
                for e in build_inventory(xml_text)["elements"]}[elem_id]

    def test_pool_named_from_the_description_is_resolved(self):
        """«Перевозчик ВкусВилла» и «Перевозчик» — один участник: раньше
        аплайер отвергал правку из-за формулировки, и модель гоняла тот же
        пакет по кругу."""
        out, report = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_B9", "name": "Оформить путевой лист",
             "task_type": "userTask", "participant": "Перевозчик ВкусВилла",
             "after": "B_take"}])
        assert report["skipped"] == []
        assert self._pool_of(out, "new_B9") == "Перевозчик"

    def test_ambiguous_pool_is_refused_and_names_the_candidates(self):
        """Два пула подходят под одно слово — угадывать нельзя, но отказ обязан
        дать модели список, из которого она выбирает за один повтор."""
        xml = (self.XML.replace('name="Склад"', 'name="Цех фасовки"')
               .replace('name="Перевозчик"', 'name="Цех отгрузки"'))
        _, report = apply_operations(xml, [
            {"op": "add_task", "id": "new_X1", "name": "Проверить пломбы",
             "task_type": "userTask", "participant": "Цех"}])
        skip = report["skipped"][0]
        assert skip["reason"] == "пул не определён"
        assert "Цех фасовки" in skip["hint"] and "Цех отгрузки" in skip["hint"]

    def test_unknown_pool_hint_names_pools_and_the_way_to_create_one(self):
        """Живой случай повтора: `add_task` в «руководитель смены», которого на
        схеме нет. Подсказка «укажите пул из инвентаря» не называла ни самих
        пулов, ни того, что участника можно завести, — и повтор возвращал ровно
        ту же операцию (improve/retry_needed 1.0 при нулевом выигрыше).
        """
        _, report = apply_operations(self.XML, [
            {"op": "add_task", "id": "new_X1", "name": "Эскалация руководителю",
             "task_type": "userTask", "participant": "руководитель смены",
             "after": "A_escalate"}])
        hint = report["skipped"][0]["hint"]
        assert "Склад" in hint and "Перевозчик" in hint
        assert "add_lane" in hint and "add_participant" in hint

    def test_handler_in_another_pool_leaves_no_broken_branch(self):
        """Живой случай: таймер на задаче склада, шаг-эскалация в пуле
        «Сервис-деск», `connect` между ними. Проверка межпуловости сравнивала
        два «неизвестно» и пропускала дугу, вычитка её снимала — и событие
        оставалось без ветки обработки."""
        ops = [
            {"op": "add_task", "id": "new_A6", "name": "Сообщить подразделениям",
             "task_type": "userTask", "participant": "Перевозчик",
             "to": "B_end"},
            {"op": "add_boundary_event", "id": "new_B2", "attached_to": "A_escalate",
             "event_type": "timer", "name": "Передача дольше двух часов",
             "duration": "PT2H"},
            {"op": "connect", "source": "new_B2", "target": "new_A6"},
        ]
        out, report = apply_operations(self.XML, ops)
        assert any("между разными пулами" in s["reason"]
                   for s in report["skipped"])
        ids = {e.get("id") for e in _root(out).iter() if e.get("id")}
        assert not {"new_A6", "new_B2"} & ids, "отказанная дуга не оставляет хвостов"
        assert validate_and_repair(out)[1] == []
        # Маршрут хозяина не пострадал: откатан только вклад пакета.
        flows = {f["source"]: f["target"] for f in
                 build_inventory(out)["flows"] if f["kind"] == "sequence"}
        assert flows["A_ship"] == "A_end"
        assert flows["A_escalate"] == "A_sub"


class TestRoleLaneIsAnAddress:
    """«Участник» в операции модели может быть ролью, которая на схеме уже
    дорожка.

    Живые прогоны улучшения: `add_task` с participant «руководитель смены»
    отвергался как «пул не определён», шаг не создавался, а за ним откачивалась
    и ветка граничного события, который на этот шаг вёл. Имя дорожки — не
    догадка о структуре: это то, чем схему уже нарисовали.
    """

    ROLE_LANE_XML = MERGE_XML.replace('<lane id="Lane_wh" name="Кладовщик">',
                                      '<lane id="Lane_wh" name="Менеджер смены">')

    def test_task_with_a_lane_name_lands_in_that_lane(self):
        out, report = apply_operations(self.ROLE_LANE_XML, [
            {"op": "add_task", "id": "new_A", "name": "Оформить путевой лист",
             "task_type": "userTask", "participant": "Менеджер смены",
             "after": "W_pick", "to": "W_ship"}])
        assert report["status"] == "success", report["skipped"]
        refs = [r.text for r in _by_id(out, "Lane_wh").findall(
            "bpmn:flowNodeRef", NS)]
        assert "new_A" in refs
        # маршрут цел: new_A встал между W_pick и W_ship
        flows = {(f["source"], f["target"]) for f in
                 build_inventory(out)["flows"] if f["kind"] == "sequence"}
        assert ("W_pick", "new_A") in flows and ("new_A", "W_ship") in flows

    def test_ambiguous_lane_name_is_still_refused(self):
        """Две одноимённые дорожки в разных пулах — выбирать не из чего:
        аплайер обязан отказать, а не угадать организацию."""
        xml = self.ROLE_LANE_XML.replace(
            '<process id="Process_shop" name="Склад" isExecutable="true">',
            '<process id="Process_shop" name="Склад" isExecutable="true">'
            '<laneSet id="LaneSet_shop"><lane id="Lane_shop" '
            'name="Менеджер смены"/></laneSet>')
        out, report = apply_operations(xml, [
            {"op": "add_task", "id": "new_A", "name": "Оформить путевой лист",
             "task_type": "userTask", "participant": "Менеджер смены"}])
        assert any(s["reason"] == "пул не определён" for s in report["skipped"])
        assert _by_id(out, "new_A") is None

    def test_explicit_lane_still_wins_over_the_name_lookup(self):
        """Если модель указала и дорожку, и несуществующий пул — чинить за неё
        адрес не будем: отказ честнее молчаливой подмены."""
        out, report = apply_operations(self.ROLE_LANE_XML, [
            {"op": "add_task", "id": "new_A", "name": "Оформить путевой лист",
             "task_type": "userTask", "participant": "Никто",
             "lane": "Lane_wh"}])
        assert any(s["reason"] == "пул не определён" for s in report["skipped"])
        assert _by_id(out, "new_A") is None


# ---------------------------------------------------------------------------
# Единая точка входа: «применено и не хуже исходной»
#
# Последовательность «аплайер → починка → откат по ухудшению» писалась руками
# трижды (оркестратор — два раза подряд, харнесс — короче), и каждый раз
# по-своему неполно: откат пакета, замкнувшего цикл, возвращал базовый XML,
# оставляя в `applied` строки правок, которых в схеме уже нет, — корректирующий
# повтор затем запрещает модели делать то, чего она уже «сделала». Второй прогон
# починки, в свою очередь, перезаписывает пометки: неидемпотентный факт о
# подмене типа элемента исчезает из отчёта ровно тогда, когда он важнее всего.
# ---------------------------------------------------------------------------

LINEAR_XML = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_lin">
  <process id="P_lin" name="Маршрут" isExecutable="true">
    <startEvent id="L_s" name="Запрос"><outgoing>LF1</outgoing></startEvent>
    <sequenceFlow id="LF1" sourceRef="L_s" targetRef="L_a"/>
    <userTask id="L_a" name="Собрать заказ">
      <incoming>LF1</incoming><outgoing>LF2</outgoing></userTask>
    <sequenceFlow id="LF2" sourceRef="L_a" targetRef="L_b"/>
    <userTask id="L_b" name="Отгрузить">
      <incoming>LF2</incoming><outgoing>LF3</outgoing></userTask>
    <sequenceFlow id="LF3" sourceRef="L_b" targetRef="L_e"/>
    <endEvent id="L_e" name="Готово"><incoming>LF3</incoming></endEvent>
  </process>
</definitions>"""


class TestApplyAndGuarantee:
    """Откат имеет право вернуть базу только с пустым `applied`: отчёт не вправе
    утверждать, что правка в схеме, когда схема вернулась к базе."""

    # Оба шага легальны поодиночке и вместе замыкают L_a → new_X → L_b → L_a —
    # цикл без шлюза на выходе. Решение «отказывать пакету» принимает вызывающий
    # код, аплайер обязан честно отразить отказ в отчёте.
    CYCLING = [
        {"op": "add_task", "id": "new_X", "name": "Проверить пломбы",
         "task_type": "userTask", "after": "L_a", "to": "L_b"},
        {"op": "connect", "source": "L_b", "target": "L_a"},
    ]

    def test_reverted_package_leaves_no_applied_row(self):
        out, report = apply_and_guarantee(
            LINEAR_XML, self.CYCLING, reject=lambda _xml: "цикл без выхода")
        assert out == LINEAR_XML
        assert report["applied"] == []
        assert report["reverted"] == "цикл без выхода"
        assert report["status"] == "failed"
        by_op = {s["op"]: s for s in report["skipped"]}
        assert {"add_task", "connect"} <= set(by_op), report["skipped"]
        for row in by_op.values():
            assert row["reason"] == "цикл без выхода"
            assert row["hint"] == REJECT_HINT
        # Идентичность правки сохранена: повтор понимает, о каком элементе речь,
        # и не считает откатанную вставку уже сделанной.
        assert by_op["add_task"]["id"] == "new_X"
        assert by_op["connect"]["source"] == "L_b"
        assert by_op["connect"]["target"] == "L_a"
        assert _by_id(out, "new_X") is None

    def test_revert_of_an_empty_package_still_says_why(self):
        """Ни одной применённой строки — отказ всё равно обязан дойти до
        корректирующего повтора, который собран по `skipped`."""
        _, report = apply_and_guarantee(
            LINEAR_XML, [{"op": "rename", "id": "нет-такого", "name": "X"}],
            reject=lambda _xml: "схема хуже исходной")
        assert report["applied"] == []
        assert report["reverted"] == "схема хуже исходной"
        assert report["status"] == "failed"
        batch = [s for s in report["skipped"] if s["op"] == "batch"]
        assert batch[0]["reason"] == "схема хуже исходной"

    def test_without_a_reject_the_package_survives(self):
        out, report = apply_and_guarantee(LINEAR_XML, self.CYCLING[:1])
        assert report["reverted"] == ""
        assert report["status"] == "success"
        assert [a["op"] for a in report["applied"]] == ["add_task"]
        assert _by_id(out, "new_X") is not None
        assert out != LINEAR_XML

    def test_merge_notes_keeps_order_and_drops_duplicates(self):
        assert merge_notes(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
        assert merge_notes([], ["x"]) == ["x"]
        assert merge_notes() == []

    def test_repair_notes_of_every_round_are_kept(self):
        """«Понижен до задачи» второй прогон уже не вернёт: чинить нечего, а
        факт подмены типа элемента читателю терять нельзя — без `merge_notes`
        отчёт замолкал о нём сразу, как начинался корректирующий повтор."""
        demoted = BARE_GATEWAY_XML.replace(
            '<sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>\n', "").replace(
            '<exclusiveGateway id="G1" name="Оплата?">',
            '<exclusiveGateway id="G1" name="Оплата?" default="F2">')
        out, report = apply_and_guarantee(demoted, [])
        assert report["repair_rounds"] == 2, "второй прогон обязана быть: он проверяет покой"
        assert any("понижен до задачи" in n for n in report["notes"]), report["notes"]
        assert _tag(_by_id(out, "G1")) == "task"

    def test_reject_is_asked_about_the_repaired_tree(self):
        """Предикат смотрит на то, что ушло бы пользователю: ссылок, которые
        починка всё равно пересобрала бы, в кандидат-XML быть не должно."""
        stale = LINEAR_XML.replace('<incoming>LF2</incoming>',
                                   '<incoming>LF_ghost</incoming>')
        seen = []

        def _reject(candidate):
            seen.append(candidate)
            return None

        out, report = apply_and_guarantee(stale, [], reject=_reject)
        assert seen and seen[0] != stale
        assert "LF_ghost" not in seen[0]
        assert "LF1" in _refs(_by_id(out, "L_a"), "incoming")
        assert report["reverted"] == ""
        assert report["repair_rounds"] >= 1

    def test_noop_rows_count_applied_rows_that_changed_nothing(self):
        """Строка «применено» там, где схема не изменилась, — не правка: без
        счётчика оркестратор считает её успехом и не зовёт переспрос."""
        text = "Собираем по описи"
        _, report = apply_and_guarantee(LINEAR_XML, [
            {"op": "add_documentation", "id": "L_a", "text": text},
            {"op": "add_documentation", "id": "L_a", "text": text},
        ])
        assert len(report["applied"]) == 2
        note = report["applied"][1]["note"]
        assert any(marker in note for marker in NOOP_NOTE_MARKERS), note
        assert report["noop_rows"] == 1

    def test_partial_status_survives_the_repair(self):
        """Отказанная правка — не успех и не отказ пакета: переспрос обязан
        видеть, что работа осталась открытой."""
        out, report = apply_and_guarantee(LINEAR_XML, [
            {"op": "add_task", "id": "new_X", "name": "Проверить пломбы",
             "task_type": "userTask", "after": "L_a", "to": "L_b"},
            {"op": "rename", "id": "нет-такого", "name": "X"},
        ])
        assert report["status"] == "partial"
        assert report["reverted"] == ""
        assert len(report["applied"]) == 1
        assert _by_id(out, "new_X") is not None


# ---------------------------------------------------------------------------
# Условие на существующей ветке шлюза
# ---------------------------------------------------------------------------

class TestAddCondition:
    """`connect` поток создаёт и на готовую пару отвечает «такой поток уже
    существует» с подсказкой про `set_default` — а тот условие снимает. Ветка
    шлюза без условия и без default оказывалась неисправимой: правило
    `gateway_conditions` стоит 15 баллов, и на живой схеме vehicle_reservation
    пакет, применившийся целиком, не сдвинул скор (80 → 80)."""

    def test_condition_lands_on_the_existing_branch(self):
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "add_condition", "flow": "F2", "condition": "остаток > 0"}])
        assert report["status"] == "success", report["skipped"]
        flow = _flow(_root(out), "F2")
        assert flow is not None, "id потока сохраняется, а не заменяется новым"
        assert flow.get("sourceRef") == "G1" and flow.get("targetRef") == "T1"
        assert flow.find("bpmn:conditionExpression", NS).text == "остаток > 0"
        # планировщик правку увидит — иначе повтор принесёт её снова
        flows = {f["id"]: f for f in build_inventory(out)["flows"]}
        assert flows["F2"]["condition"] == "остаток > 0"

    def test_connect_cannot_do_this_job(self):
        """Само существование операции из этого теста: `connect` по существующей
        паре отказывает, а `add_condition` ту же ветку условной делает."""
        _, by_connect = apply_operations(BARE_GATEWAY_XML, [
            {"op": "connect", "source": "G1", "target": "T1",
             "condition": "остаток > 0"}])
        assert by_connect["skipped"][0]["reason"] == "такой поток уже существует"
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "add_condition", "flow": "F2", "condition": "остаток > 0"}])
        assert report["applied"][0]["op"] == "add_condition"
        assert _flow(_root(out), "F2").find("bpmn:conditionExpression", NS) is not None

    def test_the_operation_is_offered_to_the_planner(self):
        assert "add_condition" in OP_SPEC and "add_condition" in _HANDLERS
        assert '"op":"add_condition"' in OP_SPEC["add_condition"]
        assert '"flow":' in OP_SPEC["add_condition"]
        assert '"condition":' in OP_SPEC["add_condition"]

    def test_empty_condition_is_refused(self):
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "add_condition", "flow": "F2", "condition": "   "}])
        assert report["skipped"][0]["reason"] == "не задано условие"
        assert "condition" in report["skipped"][0]["hint"]
        assert _flow(_root(out), "F2").find("bpmn:conditionExpression", NS) is None

    def test_unknown_flow_points_at_the_inventory_flows(self):
        _, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "add_condition", "flow": "F9", "condition": "да"}])
        skip = report["skipped"][0]
        assert skip["reason"] == "поток 'F9' не найден"
        assert "flows" in skip["hint"]

    def test_flow_of_a_non_gateway_is_refused(self):
        """F1 выходит из старта, F4 — из задачи: условия на таких ветках движок
        не читает, а скоринг их не требует."""
        for flow_id in ("F1", "F4"):
            _, report = apply_operations(BARE_GATEWAY_XML, [
                {"op": "add_condition", "flow": flow_id, "condition": "да"}])
            assert report["skipped"][0]["reason"].endswith(
                "не является веткой шлюза"), report["skipped"]

    def test_message_flow_is_refused(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [
            {"op": "add_condition", "flow": "MF1", "condition": "да"}])
        assert report["skipped"][0]["reason"] == "'MF1' не sequence-поток"

    def test_parallel_gateway_branch_is_refused(self):
        xml = BARE_GATEWAY_XML.replace(
            '<exclusiveGateway id="G1" name="Оплата?">',
            '<parallelGateway id="G1" name="И туда, и туда">').replace(
            "</exclusiveGateway>", "</parallelGateway>")
        _, report = apply_operations(xml, [
            {"op": "add_condition", "flow": "F2", "condition": "да"}])
        assert "не является исключающим шлюзом" in report["skipped"][0]["reason"]

    def test_default_of_this_flow_is_lifted(self):
        """Ветка не может быть одновременно «иначе» и условной: снятие default
        — часть правки, а не побочный эффект, и читатель обязан её видеть."""
        xml = BARE_GATEWAY_XML.replace(
            '<exclusiveGateway id="G1" name="Оплата?">',
            '<exclusiveGateway id="G1" name="Оплата?" default="F2">')
        out, report = apply_operations(xml, [
            {"op": "add_condition", "flow": "F2", "condition": "оплачен"}])
        assert report["status"] == "success", report["skipped"]
        assert _gateway_default_id(out, "G1") is None
        assert ("выход по умолчанию со 'F2' снят: теперь он условный"
                in report["applied"][0]["note"])

    def test_previous_condition_is_replaced_not_doubled(self):
        out, report = apply_operations(BARE_GATEWAY_XML, [
            {"op": "add_condition", "flow": "F2", "condition": "первое"},
            {"op": "add_condition", "flow": "F2", "condition": "второе"},
        ])
        assert report["status"] == "success", report["skipped"]
        conditions = _flow(_root(out), "F2").findall("bpmn:conditionExpression", NS)
        assert len(conditions) == 1 and conditions[0].text == "второе"
        assert "прежнее условие заменено" in report["applied"][1]["note"]


_OP_LIKE = re.compile(r"\b(?:add|remove|set|move|merge)_[a-z_]+\b"
                      r"|\b(?:disconnect|delete|rename|connect)\b")


def test_no_hint_or_spec_invents_an_operation():
    """Отказ, в котором названа несуществующая операция, модель читает буквально:
    `add_condition` жил в подсказке `_require_element` ровно так, и в живом
    отчёте «правку по id принимает только add_condition» повторилось 4 раза
    (eval/reports/20260923-210453.json) — на операцию, которой у аплайера нет.

    Имя, которое сам модуль определяет как функцию (`merge_notes`), операцией
    не прикидывается: читателю отчёта оно не показывается, поэтому оно
    разрешается через hasattr, а внутренним переменным такие имена писать
    нельзя — отсюда `touched_by_merge` и `merged_ops` вместо `merge_*`."""
    module = sys.modules["core.bpmn_edits"]
    source = inspect.getsource(module)
    for name in _OP_LIKE.findall(source):
        # Синоним из `OP_ALIASES` — имя, которое аплайер принимает (с подстановкой
        # канонической операции), поэтому упоминать его в тексте можно.
        assert name in OP_SPEC or name in OP_ALIASES or hasattr(module, name), \
            f"упомянуто {name!r}, которого нет в OP_SPEC"


DEMOTION_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="Proc_1" name="Процесс">
    <startEvent id="S" name="Начало"/>
    <sequenceFlow id="f1" sourceRef="S" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Проверка документов"/>
    <sequenceFlow id="f2" sourceRef="G1" targetRef="T1">
      <conditionExpression>count > 0</conditionExpression>
    </sequenceFlow>
    <userTask id="T1" name="Собрать документы"/>
    <sequenceFlow id="f3" sourceRef="T1" targetRef="G2"/>
    <exclusiveGateway id="G2" name=""/>
    <sequenceFlow id="f4" sourceRef="G2" targetRef="E"/>
    <endEvent id="E" name="Готово"/>
  </process>
</definitions>
'''


def test_unnamed_single_branch_gateway_is_not_demoted_into_an_unnamed_task():
    """Понижение шлюза с единственной веткой до задачи — починка, которая обязана
    думать о том, что она оставляет после себя.

    У шлюза имени по нотации не бывает, а `naming` спрашивает имя с шагов:
    безымянный шлюз, пониженный до задачи, превращался в нарушение, которого до
    ремонта не было. Находка переписи `eval/advice` (witness
    `Warenversand_9d83f8d992b647feb794f8441e98cb56.bpmn`): один `connect` из
    подсказки ронял `naming` не из-за текста совета, а из-за этой починки.
    Именованный шлюз понижается по-прежнему — вместе с условием единственной
    ветки."""
    out, notes = validate_and_repair(DEMOTION_XML)
    assert _tag(_by_id(out, "G1")) == "task", out
    assert _tag(_by_id(out, "G2")) == "exclusiveGateway", out
    assert any("требует имени" in n for n in notes), notes
    assert any("понижен до задачи" in n for n in notes), notes
    root = _root(out)
    nameless_steps = [e.get("id") for e in root.iter()
                      if _tag(e) in ("task", "userTask", "manualTask",
                                     "serviceTask", "sendTask", "receiveTask")
                      and len((e.get("name") or "").strip()) < 3]
    assert nameless_steps == []
    # Условие снято только там, где шлюз стал задачей: у оставшегося шлюза оно
    # не мешало (ветка одна, а `gateway_conditions` наказывает расщепление).
    assert _flow(root, "f2").find("bpmn:conditionExpression", NS) is None
    assert _flow(root, "f4").find("bpmn:conditionExpression", NS) is None


class TestSetTaskType:
    """`set_task_type` меняет тег задачи, сохраняя узел: id, имя, дорожку, обе
    дуги и ссылку диаграммы.

    Операция нужна потому, что самый массовый дефект корпуса (200 схем из 367 с
    одними родовыми `task`) нечем было починить: `delete` + `add_task` даёт другой
    id и рвёт маршрут. Отдельно следим за формой имени: `service` и `serviceTask`
    — одна правка, и отказ по короткому слову был бы обрезкой валидного ответа
    транспортом.
    """

    ONE_POOL = TestMessageFlowEndsAreNotGateways.TWO_POOLS

    def _apply(self, op):
        return apply_operations(self.ONE_POOL, [op])

    def test_generic_task_becomes_a_service_task(self):
        out, report = self._apply({"op": "set_task_type", "id": "A1",
                                   "task_type": "serviceTask"})
        assert report["status"] == "success"
        root = _root(out)
        # тронут только названный узел: сосед по коллаборации остаётся как был
        assert root.find(".//bpmn:userTask[@id='A1']", NS) is None
        assert root.find(".//bpmn:userTask[@id='A2']", NS) is not None
        service = root.find(".//bpmn:serviceTask[@id='A1']", NS)
        assert service is not None and service.get("name") == "Собрать"
        # маршрут и дорожка не тронуты
        assert root.find(".//bpmn:sequenceFlow[@sourceRef='A1']", NS) is not None
        assert root.find(".//bpmn:sequenceFlow[@targetRef='A1']", NS) is not None

    def test_short_type_name_is_the_same_repair(self):
        _, report = self._apply({"op": "set_task_type", "id": "A1",
                                 "task_type": "service"})
        assert report["status"] == "success", report["skipped"]

    def test_unknown_type_is_refused_with_the_allowed_list(self):
        _, report = self._apply({"op": "set_task_type", "id": "A1",
                                 "task_type": "printerTask"})
        assert report["status"] == "failed"
        assert "printerTask" in report["skipped"][0]["reason"]
        assert "businessRuleTask" in report["skipped"][0]["hint"]

    def test_gateway_is_not_a_step(self):
        _, report = self._apply({"op": "set_task_type", "id": "G1",
                                 "task_type": "serviceTask"})
        assert report["status"] == "failed"
        assert "не шаг" in report["skipped"][0]["reason"]

    def test_reapplying_the_same_type_is_reported_as_noop(self):
        """Одно и то же правило, применённое второй раз, не «изменило схему»:
        без такой строки отчёта модель зацикливается на правке, которой нет."""
        out, report = self._apply({"op": "set_task_type", "id": "A1",
                                   "task_type": "userTask"})
        notes = str(report["applied"]) + str(report.get("noop_rows"))
        assert "уже тип" in notes, report
        assert _root(out).find(".//bpmn:userTask[@id='A1']", NS) is not None

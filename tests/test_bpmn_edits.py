"""Детерминированный аплайер операций: переподвеска потоков, каскадное удаление,
пропуск невалидных операций с причиной и семантическая починка.

Это та часть контура улучшения, которая раньше отдавала 500: LLM возвращал
произвольный XML, а валидатор требовал полную DI-раскладку. Теперь модель
возвращает операции, и применяем их мы."""
import xml.etree.ElementTree as ET

import pytest

from core.bpmn_edits import (
    BPMN_NS,
    XML_DECLARATION,
    apply_operations,
    build_inventory,
    validate_and_repair,
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
        out, report = apply_operations(two_pool_xml, [{
            "op": "add_task", "id": "new_T_pack", "name": "Упаковать",
            "participant": "Магазин", "task_type": "userTask",
        }])
        assert report["status"] == "success"
        root = _root(out)
        shop = root.find(".//bpmn:process[@id='Process_shop']", NS)
        assert "new_T_pack" in [child.get("id") for child in shop]
        # Без 'after' элемент добавлен без потоков — починка обязана это пережить.
        _, notes = validate_and_repair(out)
        assert notes == []

    def test_unknown_pool_is_skipped_with_hint(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [{
            "op": "add_task", "id": "new_T1", "name": "Хотелка", "participant": "Склад",
        }])
        assert report["skipped"][0]["reason"] == "пул не определён"

    def test_cross_pool_insert_is_skipped(self, two_pool_xml):
        _, report = apply_operations(two_pool_xml, [{
            "op": "add_task", "id": "new_T1", "name": "Не туда",
            "participant": "Магазин", "after": "C_request",
        }])
        assert report["skipped"][0]["reason"] == "элемент 'after' находится в другом пуле"


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
        assert notes == ["удалён висящий поток F5"]
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

    def test_empty_process_left_alone(self):
        xml = XML_DECLARATION + """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P1" name="P" isExecutable="true"/>
</definitions>"""
        out, notes = validate_and_repair(xml)
        assert notes == []
        assert _root(out).find(".//bpmn:process", NS) is not None

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
        assert notes == []
        assert _refs(_by_id(out, "S1"), "outgoing") == ["F1"]
        assert _refs(_by_id(out, "T1"), "incoming") == ["F1"]
        assert _refs(_by_id(out, "T1"), "outgoing") == []


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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

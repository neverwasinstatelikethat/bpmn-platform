"""Скоринг и «оптимизация»: правки должны попадать в оптимизированный XML.

Раньше оптимизация мутировала разобранной оригинал, а на выход уходила нетронутая
копия — оптимизированная схема не отличалась от исходной."""
import xml.etree.ElementTree as ET

from core.bpmn_scoring import BPMN_NS, BPMNScorer

NS = {"bpmn": BPMN_NS}


def _xml(gateway_branches_with_conditions: bool) -> str:
    def branch(condition: str) -> str:
        if not gateway_branches_with_conditions:
            return ""
        return f"<conditionExpression>{condition}</conditionExpression>"
    paid_yes = branch("paid == true")
    paid_no = branch("paid == false")
    return """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">
  <process id="Process_1" name="Заказ" isExecutable="true">
    <startEvent id="Start_1" name="Заказ создан">
      <outgoing>F1</outgoing>
    </startEvent>
    <sequenceFlow id="F1" sourceRef="Start_1" targetRef="T1"/>
    <userTask id="T1" name="Собрать заказ">
      <incoming>F1</incoming>
      <outgoing>F0</outgoing>
    </userTask>
    <sequenceFlow id="F0" sourceRef="T1" targetRef="G1"/>
    <exclusiveGateway id="G1" name="Оплачен?">
      <incoming>F0</incoming>
      <outgoing>F2</outgoing>
      <outgoing>F3</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="F2" sourceRef="G1" targetRef="End_1">""" + paid_yes + """</sequenceFlow>
    <endEvent id="End_1" name="Завершение">
      <incoming>F2</incoming>
    </endEvent>
    <sequenceFlow id="F3" sourceRef="G1" targetRef="End_2">""" + paid_no + """</sequenceFlow>
    <endEvent id="End_2" name="Отмена">
      <incoming>F3</incoming>
    </endEvent>
  </process>
</definitions>"""


def _conditions(root, flow_id):
    flow = root.find(f".//bpmn:sequenceFlow[@id='{flow_id}']", NS)
    return flow.findall("bpmn:conditionExpression", NS)


class TestOptimizedBpmn:
    def setup_method(self):
        self.scorer = BPMNScorer()

    def test_missing_conditions_appear_in_optimized_xml(self):
        result = self.scorer.evaluate(_xml(False))
        optimized = ET.fromstring(result["optimized_bpmn"])
        assert len(_conditions(optimized, "F2")) == 1
        assert len(_conditions(optimized, "F3")) == 1
        assert _conditions(optimized, "F2")[0].text == "true"
        assert result["details"]["gateway_conditions"] is False

    def test_existing_conditions_are_not_duplicated(self):
        result = self.scorer.evaluate(_xml(True))
        optimized = ET.fromstring(result["optimized_bpmn"])
        assert len(_conditions(optimized, "F2")) == 1
        assert _conditions(optimized, "F2")[0].text == "paid == true"
        assert result["details"]["gateway_conditions"] is True

    def test_optimized_xml_keeps_prefixes_readable(self):
        result = self.scorer.evaluate(_xml(False))
        assert "ns0:" not in result["optimized_bpmn"]
        assert 'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"' in result["optimized_bpmn"]

    def test_optimized_xml_still_parses_and_scores(self):
        scorer = BPMNScorer()
        first = scorer.evaluate(_xml(True))
        second = scorer.evaluate(first["optimized_bpmn"])
        assert second["score"] == first["score"]

    def test_broken_xml_degrades_instead_of_raising(self):
        result = self.scorer.evaluate("<definitions><не-xml")
        assert result["score"] == 0
        assert result["optimized_bpmn"] == "<definitions><не-xml"
        assert result["recommendations"]


class TestRuleResults:
    def setup_method(self):
        self.result = BPMNScorer().evaluate(_xml(True))

    def test_score_is_capped_and_details_complete(self):
        assert 0 <= self.result["score"] <= 100
        assert set(self.result["details"]) == set(BPMNScorer().rules)

    def test_every_failed_rule_gives_a_recommendation(self):
        failed = [name for name, ok in self.result["details"].items() if not ok]
        assert failed, "тест потерял смысл: схема на все 100 баллов"
        assert len(self.result["recommendations"]) == len(failed)
        assert all(rec.strip() for rec in self.result["recommendations"])

    def test_single_participant_wants_single_start(self):
        assert self.result["details"]["start_event"] is True
        assert self.result["details"]["end_event"] is True

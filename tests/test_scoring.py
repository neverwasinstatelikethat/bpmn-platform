"""Rule-based скоринг BPMN-схем: контракт ответа, правила и стоимость обхода.

Фиксирует поведение после правок ревью: нормированный балл по применимым
правилам, полные details/details_meta, отказ от мутации схемы, линейный
поиск циклов и защищённые циклы как норма BPMN."""
import inspect
import json
import time

from core import llm_client
from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import (
    FAILED,
    NOT_APPLICABLE,
    PASSED,
    BPMNScorer,
)

scorer = BPMNScorer()

HEADER = ('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" '
          'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">')


def _doc(process_inner: str, collaboration: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}{collaboration}'
            f'<process id="Process_1" name="Процесс" isExecutable="true">'
            f'{process_inner}</process></definitions>')


def _flow(fid, source, target, condition=None):
    """Поток; condition=None — поток без условия."""
    cond = (f"<conditionExpression>{condition}</conditionExpression>"
            if condition else "")
    return (f'<sequenceFlow id="{fid}" sourceRef="{source}" '
            f'targetRef="{target}">{cond}</sequenceFlow>')


def simple_xml(gateway_conditions=True):
    """Старт → задача → эксклюзивный шлюз → два конечных события."""
    cond = "true" if gateway_conditions else None
    return _doc(
        '<startEvent id="Start_1" name="Заказ создан"/>'
        + _flow("F1", "Start_1", "T1")
        + '<userTask id="T1" name="Собрать заказ"/>'
        + _flow("F2", "T1", "G1")
        + '<exclusiveGateway id="G1" name="Оплачен?"/>'
        + _flow("F3", "G1", "End_1", cond)
        + '<endEvent id="End_1" name="Завершение"/>'
        + _flow("F4", "G1", "End_2", cond)
        + '<endEvent id="End_2" name="Отмена"/>'
    )


def rework_xml(gateway="exclusiveGateway"):
    """Легитимный BPMN-rework: согласование → доработка → снова согласование."""
    return _doc(
        '<startEvent id="S" name="Задача поступила"/>'
        + _flow("f1", "S", "A")
        + '<userTask id="A" name="Согласовать"/>'
        + _flow("f2", "A", "G")
        + f'<{gateway} id="G" name="Есть замечания?"/>'
        + _flow("f3", "G", "B", "да")
        + _flow("f6", "G", "E", "нет")
        + '<manualTask id="B" name="Доработать"/>'
        + _flow("f5", "B", "A")
        + _flow("f4", "A", "E")
        + '<endEvent id="E" name="Согласовано"/>'
    )


def eventless_xml(event_inner=""):
    """Схема с одним catch-событием; inner — его дочерние определения."""
    return _doc(
        '<startEvent id="S" name="Заказ создан"/>'
        + _flow("f1", "S", "A")
        + '<userTask id="A" name="Оформить"/>'
        + _flow("f2", "A", "W")
        + f'<intermediateCatchEvent id="W" name="Ожидание оплаты">{event_inner}'
          f'</intermediateCatchEvent>'
        + _flow("f3", "W", "E")
        + '<endEvent id="E" name="Готово"/>'
    )


def straight_xml():
    """Старт → задача → финиш: ни шлюзов, ни промежуточных событий."""
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
        + '<userTask id="A" name="Задача"/>' + _flow("f2", "A", "E")
        + '<endEvent id="E" name="Конец"/>')


def diamond_chain(count: int) -> str:
    """Цепочка «ромбов»: простых путей 2^n, рёбер — линейно.

    Прежний рекурсивный обход перебирал все пути и на 24 ромбах работал
    десятки секунд внутри async-хендлера."""
    body = ['<startEvent id="S0" name="Начало"/>']
    flows = []
    head = ["S0"]
    for i in range(count):
        gateway, left, right = f"G{i}", f"A{i}", f"B{i}"
        body.append(f'<exclusiveGateway id="{gateway}" name="Ветка {i}?"/>')
        body.append(f'<userTask id="{left}" name="Шаг {i} левый"/>')
        body.append(f'<serviceTask id="{right}" name="Шаг {i} правый"/>')
        flows.extend(_flow(f"f{prev}_{gateway}", prev, gateway) for prev in head)
        flows.append(_flow(f"f{gateway}_{left}", gateway, left))
        flows.append(_flow(f"f{gateway}_{right}", gateway, right))
        head = [left, right]
    body.append('<endEvent id="E" name="Конец"/>')
    flows.extend(_flow(f"f{h}_E", h, "E") for h in head)
    return _doc("".join(body) + "".join(flows))


def status(result, rule):
    return result["details_meta"][rule]["status"]


def elements(result, rule):
    return result["details_meta"][rule]["elements"]


class TestContract:
    def test_all_rule_keys_present_without_exclusive_gateways(self):
        result = scorer.evaluate(eventless_xml("<timerEventDefinition/>"))
        assert set(result["details"]) == set(scorer.rules)
        assert set(result["details_meta"]) == set(scorer.rules)
        assert status(result, "gateway_conditions") == NOT_APPLICABLE

    def test_not_applicable_is_not_shown_as_error(self):
        result = scorer.evaluate(straight_xml())
        assert status(result, "gateway_conditions") == NOT_APPLICABLE
        # плоский details читает фронтенд как «есть ошибка»: n/a не ошибка
        assert result["details"]["gateway_conditions"] is True
        assert not any("шлюз" in rec.lower() for rec in result["recommendations"])

    def test_flat_details_is_false_only_for_failures(self):
        for xml in (simple_xml(), rework_xml(), diamond_chain(3), straight_xml()):
            result = scorer.evaluate(xml)
            for rule, entry in result["details_meta"].items():
                assert result["details"][rule] is (entry["status"] != FAILED), rule

    def test_score_normalized_over_applicable_weights(self):
        result = scorer.evaluate(eventless_xml("<timerEventDefinition/>"))
        earned = sum(e["weight"] for e in result["details_meta"].values()
                     if e["status"] == PASSED)
        applicable = sum(e["weight"] for e in result["details_meta"].values()
                         if e["status"] != NOT_APPLICABLE)
        assert result["score"] == round(100 * earned / applicable)
        assert applicable < sum(r["weight"] for r in scorer.rules.values())

    def test_every_failed_rule_gives_one_recommendation(self):
        result = scorer.evaluate(simple_xml(gateway_conditions=False))
        failed = [name for name, entry in result["details_meta"].items()
                  if entry["status"] == FAILED]
        assert failed
        assert len(result["recommendations"]) == len(failed)
        assert all(rec.strip() for rec in result["recommendations"])

    def test_recommendation_names_touched_elements(self):
        result = scorer.evaluate(simple_xml(gateway_conditions=False))
        assert "G1" in elements(result, "gateway_conditions")
        rec = next(r for r in result["recommendations"] if "условия" in r)
        assert "G1" in rec

    def test_broken_xml_degrades_instead_of_raising(self):
        result = scorer.evaluate("<definitions><не-xml")
        assert result["score"] == 0
        assert set(result["details"]) == set(scorer.rules)
        assert set(result["details_meta"]) == set(scorer.rules)
        assert "BPMN" in result["recommendations"][0]

    def test_direction_and_no_loops_rules_removed(self):
        assert "direction" not in scorer.rules
        assert "no_loops" not in scorer.rules
        assert "guarded_cycles" in scorer.rules
        assert sum(r["weight"] for r in scorer.rules.values()) == 112


class TestScoringDoesNotMutateSchema:
    def test_no_optimized_bpmn_in_answer(self):
        result = scorer.evaluate(simple_xml(gateway_conditions=False))
        assert "optimized_bpmn" not in result

    def test_repeated_evaluation_is_stable(self):
        xml = simple_xml(gateway_conditions=False)
        assert scorer.evaluate(xml) == scorer.evaluate(xml)
        assert status(scorer.evaluate(xml), "gateway_conditions") == FAILED


class TestCycles:
    def test_guarded_rework_cycle_is_allowed(self):
        result = scorer.evaluate(rework_xml())
        assert status(result, "guarded_cycles") == PASSED
        assert not any("цикл" in rec.lower() for rec in result["recommendations"])

    def test_unguarded_cycle_is_reported_with_nodes(self):
        result = scorer.evaluate(rework_xml(gateway="parallelGateway"))
        assert status(result, "guarded_cycles") == FAILED
        assert set(elements(result, "guarded_cycles")) == {"A", "G", "B"}
        assert any("защищённ" in rec.lower() for rec in result["recommendations"])

    def test_cycle_behind_a_long_prefix_still_found(self):
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "A")
            + '<userTask id="A" name="Шаг А"/>' + _flow("f1", "A", "B")
            + '<serviceTask id="B" name="Шаг Б"/>' + _flow("f2", "B", "C")
            + '<manualTask id="C" name="Шаг В"/>' + _flow("f3", "C", "A")
            + _flow("f4", "C", "E") + '<endEvent id="E" name="Конец"/>')
        result = scorer.evaluate(xml)
        assert status(result, "guarded_cycles") == FAILED
        assert set(elements(result, "guarded_cycles")) == {"A", "B", "C"}

    def test_diamond_chain_is_linear_cost(self):
        started = time.perf_counter()
        result = scorer.evaluate(diamond_chain(24))
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"обход графа занял {elapsed:.2f} с"
        assert status(result, "guarded_cycles") == PASSED
        assert status(result, "no_isolated") == PASSED


class TestRules:
    def test_event_types_reads_child_definition(self):
        assert status(scorer.evaluate(eventless_xml("<timerEventDefinition/>")),
                      "event_types") == PASSED

    def test_event_types_reads_boundary_event_definition(self):
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Оформить"/>' + _flow("f2", "A", "E")
            + '<boundaryEvent id="BN" name="Срок вышел" attachedToRef="A">'
              '<timerEventDefinition/></boundaryEvent>'
            + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "event_types") == PASSED

    def test_event_types_fails_on_event_without_definition(self):
        result = scorer.evaluate(eventless_xml(""))
        assert status(result, "event_types") == FAILED
        assert elements(result, "event_types") == ["W"]

    def test_event_types_not_applicable_without_such_events(self):
        assert status(scorer.evaluate(straight_xml()), "event_types") == NOT_APPLICABLE

    def test_naming_counts_flow_nodes_only(self):
        collaboration = ('<collaboration id="C1"><participant id="P1" name="Заказчик" '
                         'processRef="Process_1"/></collaboration>')
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Оформить"/>' + _flow("f2", "A", "B")
            + '<serviceTask id="B" name="Отправить"/>' + _flow("f3", "B", "C")
            + '<manualTask id="C"/>' + _flow("f4", "C", "D")
            + '<subProcess id="D" name="Подпроцесс"/>' + _flow("f5", "D", "E")
            + '<endEvent id="E" name="Конец"/>', collaboration=collaboration)
        result = scorer.evaluate(xml)
        # именованные пул и дорожка не дают «именованных» элементов;
        # безымянная задача валит правило, даже когда 4 из 6 названы
        assert status(result, "naming") == FAILED
        assert elements(result, "naming") == ["C"]

    def test_dead_end_is_isolated(self):
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Задача"/>' + _flow("f2", "A", "B")
            + '<serviceTask id="B" name="Тупик"/>'
            + '<manualTask id="C" name="Одиночная задача"/>')
        result = scorer.evaluate(xml)
        assert status(result, "no_isolated") == FAILED
        assert set(elements(result, "no_isolated")) == {"B", "C"}

    def test_start_and_end_need_one_side_only(self):
        assert status(scorer.evaluate(straight_xml()), "no_isolated") == PASSED

    def test_boundary_event_needs_no_incoming_flow(self):
        """Граничное событие запускается хозяином, входящего потока у него нет."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Оформить"/>' + _flow("f2", "A", "E")
            + '<boundaryEvent id="BN" name="Срок вышел" attachedToRef="A">'
              '<timerEventDefinition/></boundaryEvent>'
            + '<serviceTask id="R" name="Отменить"/>'
            + _flow("f3", "BN", "R") + _flow("f4", "R", "E")
            + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "no_isolated") == PASSED

    def test_gateway_conditions(self):
        assert status(scorer.evaluate(simple_xml(gateway_conditions=False)),
                      "gateway_conditions") == FAILED
        assert status(scorer.evaluate(simple_xml()), "gateway_conditions") == PASSED

    def test_element_count_limit(self):
        assert status(scorer.evaluate(diamond_chain(10)), "element_count") == PASSED
        assert status(scorer.evaluate(diamond_chain(24)), "element_count") == FAILED

    def test_start_events_checked_against_participants(self):
        """Два пула — два стартовых события; одно на двоих не проходит."""
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
               '<collaboration id="C1">'
               '<participant id="P1" name="Заказчик" processRef="Proc_1"/>'
               '<participant id="P2" name="Подрядчик" processRef="Proc_2"/>'
               '</collaboration>'
               '<process id="Proc_1" name="Заказчик">'
               '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
               + '<userTask id="A" name="Задача"/>' + _flow("f2", "A", "E")
               + '<endEvent id="E" name="Конец"/></process>'
               '<process id="Proc_2" name="Подрядчик"/></definitions>')
        result = scorer.evaluate(xml)
        assert status(result, "start_event") == FAILED
        assert any("ожидается 2 стартовых событий" in rec
                   for rec in result["recommendations"])


class TestUnsafeXml:
    def test_external_entity_is_rejected_not_resolved(self):
        hostile = ('<?xml version="1.0"?>'
                   '<!DOCTYPE definitions [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                   + HEADER + '<process id="P">&xxe;</process></definitions>')
        result = scorer.evaluate(hostile)
        assert result["score"] == 0
        assert not any("root:" in rec or "bin:" in rec for rec in result["recommendations"])

    def test_module_does_not_parse_with_bare_elementtree(self):
        source = inspect.getsource(inspect.getmodule(scorer))
        assert "ET.fromstring" not in source
        assert "parse_xml" in source


class TestCeiling:
    """Потолок для схемы в форме генератора: дорожки и документацию добавляет
    зона генерации, поэтому 100 баллов пока недостижимы."""

    STRUCTURE = {
        "participants": ["Согласование договора"],
        "elements": [
            {"id": "Start_1", "kind": "startEvent", "name": "Договор поступил"},
            {"id": "T1", "kind": "userTask", "name": "Проверить документы"},
            {"id": "G1", "kind": "exclusiveGateway", "name": "Документы в порядке?"},
            {"id": "T2", "kind": "serviceTask", "name": "Зарегистрировать договор"},
            {"id": "T3", "kind": "userTask", "name": "Вернуть на доработку"},
            {"id": "End_1", "kind": "endEvent", "name": "Договор согласован"},
        ],
        "flows": [
            {"id": "F1", "source": "Start_1", "target": "T1"},
            {"id": "F2", "source": "T1", "target": "G1"},
            {"id": "F3", "source": "G1", "target": "T2", "condition": "да"},
            {"id": "F4", "source": "G1", "target": "T3", "condition": "нет"},
            {"id": "F5", "source": "T2", "target": "End_1"},
            {"id": "F6", "source": "T3", "target": "End_1"},
        ],
    }

    def test_good_generated_schema_scores_at_least_85(self, monkeypatch):
        monkeypatch.setattr(
            llm_client, "_complete",
            lambda messages, temperature, max_tokens:
            json.dumps(self.STRUCTURE, ensure_ascii=False))
        xml = BPMNGenerator().generate("Согласование договора")["bpmn"]
        result = scorer.evaluate(xml)
        assert [name for name, entry in result["details_meta"].items()
                if entry["status"] == FAILED] == ["pool_lanes", "documentation"]
        assert result["score"] >= 85

"""Rule-based скоринг BPMN-схем: контракт ответа, правила и стоимость обхода.

Фиксирует поведение после правок ревью: нормированный балл по применимым
правилам, полные details/details_meta, отказ от мутации схемы, линейный
поиск циклов и защищённые циклы как норма BPMN.

Второй круг — связность участников: пул-декорация (`startEvent → endEvent`),
изолированный пул без messageFlow, развилка шлюза без схождения и граничное
событие без хозяина больше не проходят скоринг."""
import inspect
import json
import time
from pathlib import Path

from core import llm_client
from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import (
    FAILED,
    NOT_APPLICABLE,
    PASSED,
    BPMNScorer,
    diff_scores,
)

scorer = BPMNScorer()

HEADER = ('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" '
          'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">')

# Артефакт живого прогона (см. reports/warehouse-delivery/recommendations.md):
# он и есть та схема, которую скоринг пропускал с 85 баллами.
WAREHOUSE = Path(__file__).parents[1] / "reports" / "warehouse-delivery" / "process.bpmn"


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


def pools_doc(bodies, message_flows: str = "") -> str:
    """Коллаборация из N пулов: участник P{i} ссылается на процесс Proc_{i}.

    Пулы и потоки сообщений собираются списком тел — правила связности
    участников проверяются только на мультипуловой схеме."""
    participants = "".join(
        f'<participant id="P{i}" name="Участник {i}" processRef="Proc_{i}"/>'
        for i in range(1, len(bodies) + 1))
    processes = "".join(
        f'<process id="Proc_{i}" name="Пул {i}">{body}</process>'
        for i, body in enumerate(bodies, 1))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
            f'<collaboration id="C1">{participants}{message_flows}</collaboration>'
            f'{processes}</definitions>')


def pool_body(i: int, with_task: bool = True) -> str:
    """Старт → задача → финиш; с with_task=False — пул-декорация без единого шага."""
    if not with_task:
        return (f'<startEvent id="S{i}" name="Старт"/>'
                + _flow(f"sf{i}", f"S{i}", f"E{i}")
                + f'<endEvent id="E{i}" name="Финиш"/>')
    return (f'<startEvent id="S{i}" name="Старт"/>'
            + _flow(f"sf{i}", f"S{i}", f"A{i}")
            + f'<userTask id="A{i}" name="Шаг {i}"/>'
            + _flow(f"ef{i}", f"A{i}", f"E{i}")
            + f'<endEvent id="E{i}" name="Финиш"/>')


def lanes_doc(empty_lanes: int) -> str:
    """Пул с одной заполненной дорожкой и `empty_lanes` дорожками без элементов."""
    lanes = ('<lane id="L1" name="Исполнитель"><flowNodeRef>S</flowNodeRef>'
             '<flowNodeRef>A</flowNodeRef><flowNodeRef>E</flowNodeRef></lane>')
    lanes += "".join(f'<lane id="L{i}" name="Роль {i}"/>'
                     for i in range(2, empty_lanes + 2))
    return _doc(f'<laneSet id="LS">{lanes}</laneSet>'
                '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                + '<userTask id="A" name="Задача"/>' + _flow("f2", "A", "E")
                + '<endEvent id="E" name="Конец"/>')


def boundary_xml(attached_to="A", with_branch=True):
    """Граничное событие на задаче A; без ветки — сигнал сорвётся в никуда."""
    branch = ('<manualTask id="R" name="Эскалация руководителю"/>'
              + _flow("f3", "BN", "R") + _flow("f4", "R", "E")) if with_branch else ""
    attach = f' attachedToRef="{attached_to}"' if attached_to else ""
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
        + '<userTask id="A" name="Оформить заявку"/>' + _flow("f2", "A", "E")
        + f'<boundaryEvent id="BN" name="Срок вышел"{attach}>'
          '<timerEventDefinition/></boundaryEvent>'
        + branch + '<endEvent id="E" name="Готово"/>')


def split_join_xml(join=True):
    """Параллельный шлюз расходится на две ветки: либо обе сходятся в J, либо
    правая уходит в цепочку, которая ни к чему не приводит."""
    tail = (_flow("f4", "B", "J") + _flow("f5", "A", "J")
            + '<parallelGateway id="J" name="Собрать ветки"/>' + _flow("f6", "J", "E")
            if join else _flow("f4", "B", "D") + '<serviceTask id="D" name="Отчёт"/>')
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
        + '<parallelGateway id="G" name="Запустить параллельно"/>'
        + _flow("f1", "G", "A") + '<userTask id="A" name="Собрать груз"/>'
        + _flow("f2", "A", "E1") + '<endEvent id="E1" name="Груз собран"/>'
        + _flow("f3", "G", "B") + '<serviceTask id="B" name="Отпечатать накладную"/>'
        + tail + '<endEvent id="E" name="Готово"/>')


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
        rec = next(r for r in result["recommendations"] if "услов" in r)
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
        # 112 — веса двенадцати правил до связности участников, ещё 34 дают
        # четыре новых правила и 8 — семнадцатое, «роль не бывает участником»
        assert sum(r["weight"] for r in scorer.rules.values()) == 154
        assert "role_pools" in scorer.rules


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
        # 24 развилки × 2 ветки: обход «от каждой ветки своего шлюза» уложился
        # бы в экспоненту, здесь сходимость считается одним обратным обходом
        assert status(result, "gateway_split_join") == PASSED


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

    def test_merge_gateway_needs_no_condition(self):
        """Сходящийся шлюз (два входа, один выход) выбирать не из чего. Требовать
        на его выходе условие — значит снимать 15 баллов за починку развилки,
        которую сам же скоринг и просит (`gateway_split_join`)."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
            + '<exclusiveGateway id="G" name="Оплачен?"/>'
            + _flow("f1", "G", "A", "да") + '<userTask id="A" name="Собрать"/>'
            + _flow("f2", "G", "B", "нет") + '<serviceTask id="B" name="Отменить"/>'
            + _flow("f3", "A", "J") + _flow("f4", "B", "J")
            + '<exclusiveGateway id="J" name="Собрать ветки"/>'
            + _flow("f5", "J", "E") + '<endEvent id="E" name="Конец"/>')
        result = scorer.evaluate(xml)
        assert status(result, "gateway_conditions") == PASSED
        assert status(result, "gateway_split_join") == PASSED

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


class TestPoolHasSteps:
    def test_pool_with_a_task_passes(self):
        xml = pools_doc([pool_body(1), pool_body(2)],
                        '<messageFlow id="M1" sourceRef="A1" targetRef="A2"/>')
        result = scorer.evaluate(xml)
        assert status(result, "pool_has_steps") == PASSED
        assert status(result, "participant_interacts") == PASSED
        assert status(result, "end_event") == PASSED
        assert status(result, "start_event") == PASSED

    def test_waiting_pool_with_intermediate_event_passes(self):
        """Пул, который только ждёт таймер, — работа, а не декорация."""
        xml = pools_doc([
            pool_body(1),
            '<startEvent id="S2" name="Старт"/>' + _flow("sf2", "S2", "W2")
            + '<intermediateCatchEvent id="W2" name="Ожидание отгрузки">'
              '<timerEventDefinition/></intermediateCatchEvent>'
            + _flow("ef2", "W2", "E2") + '<endEvent id="E2" name="Финиш"/>',
        ], '<messageFlow id="M1" sourceRef="A1" targetRef="W2"/>')
        assert status(scorer.evaluate(xml), "pool_has_steps") == PASSED

    def test_pool_of_start_and_end_only_fails(self):
        """Схема, которая проходила весь прежний скоринг: `startEvent → endEvent`
        формально связен и завершается, но процесса в нём нет."""
        xml = pools_doc([pool_body(1), pool_body(2, with_task=False)],
                        '<messageFlow id="M1" sourceRef="A1" targetRef="S2"/>')
        result = scorer.evaluate(xml)
        assert status(result, "pool_has_steps") == FAILED
        assert elements(result, "pool_has_steps") == ["P2"]
        assert any("без шагов 1 из 2 участников" in rec
                   for rec in result["recommendations"])
        # за пустоту отвечает ровно одно правило: формально пул завершается
        assert status(result, "end_event") == PASSED

    def test_participant_without_process_fails(self):
        xml = pools_doc([pool_body(1)],
                        '<participant id="PX" name="Потерянный" processRef="Proc_X"/>')
        result = scorer.evaluate(xml)
        assert elements(result, "pool_has_steps") == ["PX"]
        assert elements(result, "end_event") == ["PX"]

    def test_single_pool_schema_is_not_applicable(self):
        result = scorer.evaluate(straight_xml())
        assert status(result, "pool_has_steps") == NOT_APPLICABLE
        assert status(result, "participant_interacts") == NOT_APPLICABLE


class TestParticipantInteracts:
    def test_isolated_pool_fails(self):
        xml = pools_doc([pool_body(1), pool_body(2), pool_body(3)],
                        '<messageFlow id="M1" sourceRef="A1" targetRef="A2"/>')
        result = scorer.evaluate(xml)
        assert status(result, "participant_interacts") == FAILED
        assert elements(result, "participant_interacts") == ["P3"]
        assert status(result, "pool_has_steps") == PASSED

    def test_message_flow_to_the_pool_itself_is_enough(self):
        """messageFlow может ссылаться и на пул целиком, а не на его шаг."""
        xml = pools_doc([pool_body(1), pool_body(2)],
                        '<messageFlow id="M1" sourceRef="A1" targetRef="P2"/>')
        assert status(scorer.evaluate(xml), "participant_interacts") == PASSED

    def test_one_participant_collaboration_is_not_applicable(self):
        result = scorer.evaluate(pools_doc([pool_body(1)]))
        assert status(result, "participant_interacts") == NOT_APPLICABLE
        assert status(result, "pool_has_steps") == PASSED


class TestGatewaySplitJoin:
    def test_branches_that_merge_pass(self):
        assert status(scorer.evaluate(split_join_xml()), "gateway_split_join") == PASSED

    def test_branch_without_merge_fails(self):
        result = scorer.evaluate(split_join_xml(join=False))
        assert status(result, "gateway_split_join") == FAILED
        # шлюз и корень висящей ветки: дальше по потокам схода нет
        assert elements(result, "gateway_split_join") == ["G", "B"]
        assert any("ветки не сходятся" in rec for rec in result["recommendations"])

    def test_branch_to_end_event_needs_no_join(self):
        """simple_xml: обе ветки ведут в свои конечные события — схождение не нужно."""
        assert status(scorer.evaluate(simple_xml()), "gateway_split_join") == PASSED

    def test_single_outgoing_flow_is_not_a_split(self):
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
            + '<exclusiveGateway id="G" name="Один выход"/>' + _flow("f1", "G", "A")
            + '<userTask id="A" name="Задача"/>' + _flow("f2", "A", "E")
            + '<endEvent id="E" name="Конец"/>')
        result = scorer.evaluate(xml)
        assert status(result, "gateway_split_join") == NOT_APPLICABLE
        # один выход — не развилка: условий на нём скоринг не требует
        assert status(result, "gateway_conditions") == PASSED

    def test_no_gateway_schema_is_not_applicable(self):
        assert status(scorer.evaluate(straight_xml()),
                      "gateway_split_join") == NOT_APPLICABLE

    def test_branch_reaching_a_converging_task_counts(self):
        """Сходиться ветки могут и в обычный шаг с двумя входящими потоками."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
            + '<parallelGateway id="G" name="Параллельно"/>'
            + _flow("f1", "G", "A") + '<userTask id="A" name="Левая ветка"/>'
            + _flow("f3", "A", "J")
            + _flow("f2", "G", "B") + '<serviceTask id="B" name="Правая ветка"/>'
            + _flow("f4", "B", "J") + '<manualTask id="J" name="Слить результат"/>'
            + _flow("f5", "J", "E") + '<endEvent id="E" name="Конец"/>')
        result = scorer.evaluate(xml)
        assert status(result, "gateway_split_join") == PASSED
        assert status(result, "no_isolated") == PASSED


class TestBoundaryEvents:
    def test_attached_event_with_handler_branch_passes(self):
        assert status(scorer.evaluate(boundary_xml()), "boundary_events") == PASSED

    def test_event_without_host_fails(self):
        result = scorer.evaluate(boundary_xml(attached_to="NOPE"))
        assert status(result, "boundary_events") == FAILED
        assert elements(result, "boundary_events") == ["BN"]

    def test_event_without_attached_to_ref_fails(self):
        result = scorer.evaluate(boundary_xml(attached_to=None))
        assert status(result, "boundary_events") == FAILED
        assert elements(result, "boundary_events") == ["BN"]

    def test_event_without_handler_branch_fails(self):
        result = scorer.evaluate(boundary_xml(with_branch=False))
        assert status(result, "boundary_events") == FAILED
        assert elements(result, "boundary_events") == ["BN"]
        # висящее без ветки обработки граничное событие намеренно не ловит
        # `no_isolated`: одна ошибка не должна стоить веса двух правил
        assert status(result, "no_isolated") == PASSED

    def test_event_on_a_gateway_is_not_a_handler(self):
        """Хозяином граничного события может быть только активность."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "G")
            + '<exclusiveGateway id="G" name="Развилка"/>'
            + _flow("f2", "G", "A", "да") + '<userTask id="A" name="Задача"/>'
            + _flow("f3", "A", "E") + _flow("f4", "G", "E", "нет")
            + '<boundaryEvent id="BN" name="Срок вышел" attachedToRef="G">'
              '<timerEventDefinition/></boundaryEvent>'
            + '<manualTask id="R" name="Напомнить"/>' + _flow("f5", "BN", "R")
            + _flow("f6", "R", "E") + '<endEvent id="E" name="Конец"/>')
        result = scorer.evaluate(xml)
        assert status(result, "boundary_events") == FAILED
        assert elements(result, "boundary_events") == ["BN"]

    def test_schema_without_boundary_events_is_not_applicable(self):
        assert status(scorer.evaluate(eventless_xml("<timerEventDefinition/>")),
                      "boundary_events") == NOT_APPLICABLE


class TestTightenedRules:
    def test_one_filled_lane_no_longer_covers_empty_ones(self):
        result = scorer.evaluate(lanes_doc(empty_lanes=3))
        assert status(result, "pool_lanes") == FAILED
        assert elements(result, "pool_lanes") == ["L2", "L3", "L4"]
        assert any("без элементов 3 из 4 дорожек" in rec
                   for rec in result["recommendations"])

    def test_all_lanes_filled_still_passes(self):
        assert status(scorer.evaluate(lanes_doc(empty_lanes=0)), "pool_lanes") == PASSED

    def test_schema_without_lane_set_still_fails(self):
        """Дорожки — часть методологии, и их отсутствие по-прежнему провал:
        иначе схема вовсе без ролей получила бы бесплатный балл."""
        assert status(scorer.evaluate(straight_xml()), "pool_lanes") == FAILED

    def test_one_typed_event_no_longer_covers_untyped_ones(self):
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Оформить заявку"/>' + _flow("f2", "A", "W")
            + '<intermediateCatchEvent id="W" name="Ожидание оплаты">'
              '<timerEventDefinition/></intermediateCatchEvent>'
            + _flow("f3", "W", "W2")
            + '<intermediateCatchEvent id="W2" name="Ожидание отгрузки"/>'
            + _flow("f4", "W2", "E") + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "event_types") == FAILED
        assert elements(result, "event_types") == ["W2"]
        assert any("без типа 1 из 2 событий" in rec for rec in result["recommendations"])

    def test_all_typed_events_pass(self):
        assert status(scorer.evaluate(eventless_xml("<timerEventDefinition/>")),
                      "event_types") == PASSED

    def test_end_event_is_checked_per_participant(self):
        """Раньше хватало одного endEvent на всю схему: пул без завершения
        проходил скоринг, хотя его маршрут обрывается."""
        xml = pools_doc(
            [pool_body(1),
             '<startEvent id="S2" name="Старт"/>' + _flow("sf2", "S2", "A2")
             + '<userTask id="A2" name="Шаг 2"/>'],
            '<messageFlow id="M1" sourceRef="A1" targetRef="A2"/>')
        result = scorer.evaluate(xml)
        assert status(result, "end_event") == FAILED
        assert elements(result, "end_event") == ["P2"]
        # шаг в пуле есть — за «пустоту» правило не отвечает
        assert status(result, "pool_has_steps") == PASSED


class TestScoreDiff:
    def test_identical_answers_give_empty_delta(self):
        before = scorer.evaluate(simple_xml(gateway_conditions=False))
        delta = diff_scores(before, scorer.evaluate(simple_xml(gateway_conditions=False)))
        assert delta["rules"] == {}
        assert delta["score_delta"] == 0
        assert delta["score_before"] == delta["score_after"] == before["score"]

    def test_fixed_rule_is_reported_with_its_weight(self):
        before = scorer.evaluate(simple_xml(gateway_conditions=False))
        after = scorer.evaluate(simple_xml())
        delta = diff_scores(before, after)
        assert delta["rules"]["gateway_conditions"] == {
            "before": FAILED, "after": PASSED,
            "weight": scorer.rules["gateway_conditions"]["weight"]}
        assert delta["score_delta"] > 0

    def test_broken_xml_answer_does_not_break_the_delta(self):
        delta = diff_scores(scorer.evaluate("<definitions><не-xml"),
                            scorer.evaluate(simple_xml()))
        assert delta["score_before"] == 0
        assert delta["score_delta"] > 0

    def test_delta_is_pure(self):
        before = scorer.evaluate(simple_xml(gateway_conditions=False))
        snapshot = json.dumps(before, sort_keys=True)
        diff_scores(before, scorer.evaluate(simple_xml()))
        assert json.dumps(before, sort_keys=True) == snapshot


class TestRealGeneratedSchema:
    """Артефакт живого прогона (`reports/warehouse-delivery/`): четыре пула, где
    из содержания только `startEvent → endEvent`, и ни одного messageFlow на них.
    Прежний скоринг выдавал за такую схему 85/100."""

    def test_warehouse_schema_no_longer_passes_as_good(self):
        result = scorer.evaluate(WAREHOUSE.read_text(encoding="utf-8"))
        assert result["score"] < 80, f"балл {result['score']}: пустые пулы снова проходят"
        assert status(result, "pool_has_steps") == FAILED
        assert elements(result, "pool_has_steps") == [
            "Participant_4", "Participant_5", "Participant_6", "Participant_8"]
        assert status(result, "participant_interacts") == FAILED
        assert elements(result, "participant_interacts") == elements(
            result, "pool_has_steps")

    def test_accepting_the_improvement_shows_rule_delta(self):
        """«71 → 71» из отчёта принятия теперь объясним: в этом прогоне не
        починено ни одного правила, и дельта говорит об этом прямо."""
        improved = (WAREHOUSE.parent / "process_improved.bpmn").read_text(encoding="utf-8")
        delta = diff_scores(scorer.evaluate(WAREHOUSE.read_text(encoding="utf-8")),
                            scorer.evaluate(improved))
        assert delta["rules"] == {}
        assert delta["score_delta"] == 0
        assert delta["score_before"] == delta["score_after"]


class TestWeights:
    def test_new_rules_cost_like_existing_ones(self):
        """Новые правила не раздувают знаменатель: вес в уже принятом диапазоне
        и суммарно меньше половины всех весов."""
        new = {"pool_has_steps", "participant_interacts", "gateway_split_join",
               "boundary_events"}
        assert new <= set(scorer.rules)
        assert all(6 <= scorer.rules[name]["weight"] <= 15 for name in new)
        assert sum(scorer.rules[name]["weight"] for name in new) < sum(
            rule["weight"] for name, rule in scorer.rules.items() if name not in new)

    def test_every_rule_has_a_check(self):
        assert set(scorer.rules) == set(scorer._checks)


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
        failed = {name for name, entry in result["details_meta"].items()
                  if entry["status"] == FAILED}
        # Правила связности участников не дают ложных срабатываний на валидном
        # плане: пустых пулов, изолированных участников и висящих развилок тут нет.
        assert not failed & {"pool_has_steps", "participant_interacts",
                             "gateway_split_join", "boundary_events"}
        # Потолок задают дорожки и документация — их дописывает зона генерации.
        assert failed == {"pool_lanes", "documentation"}
        assert result["score"] >= 85


class TestRolePools:
    """Пул, названный чужой дорожкой, — роль, раздутая в участника.

    Скоринг замечал пустые пулы, а «Кладовщик» с одним шагом оставался вторым
    «участником» коллаборации: генератор такие пулы сворачивает в дорожки
    (`_merge_role_pools`), а схема, импортированная или нарисованная руками,
    проходила с тем же баллом. Признак структурный и без догадок о тексте:
    имя участника совпало с именем дорожки чужого процесса."""

    @staticmethod
    def _doc(pool_name="Кладовщик", lane_owner="Proc_1"):
        lanes = (f'<laneSet id="LS"><lane id="L1" name="Кладовщик">'
                 f'<flowNodeRef>A2</flowNodeRef></lane></laneSet>')
        head = ('<startEvent id="S1" name="Старт"/>' + _flow("f1", "S1", "A1")
                + '<userTask id="A1" name="Собрать заявку"/>'
                + _flow("f2", "A1", "E1") + '<endEvent id="E1" name="Финиш"/>')
        body = ('<startEvent id="S2" name="Старт"/>' + _flow("f3", "S2", "A2")
                + '<userTask id="A2" name="Принять товар"/>'
                + _flow("f4", "A2", "E2") + '<endEvent id="E2" name="Финиш"/>')
        first = (lanes + head) if lane_owner == "Proc_1" else head
        second = (lanes + body) if lane_owner == "Proc_2" else body
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
                '<collaboration id="C1">'
                '<participant id="P1" name="ВкусВилл" processRef="Proc_1"/>'
                f'<participant id="P2" name="{pool_name}" processRef="Proc_2"/>'
                '<messageFlow id="MF1" sourceRef="A1" targetRef="A2"/>'
                '</collaboration>'
                f'<process id="Proc_1" name="ВкусВилл">{first}</process>'
                f'<process id="Proc_2" name="{pool_name}">{second}</process>'
                '</definitions>')

    def test_pool_named_like_another_pools_lane_fails(self):
        result = scorer.evaluate(self._doc())
        assert status(result, "role_pools") == FAILED
        assert elements(result, "role_pools") == ["P2"]
        # рекомендация называет операцию, которой это чинится
        assert any("merge_participants" in rec for rec in result["recommendations"])

    def test_lane_of_its_own_pool_is_not_a_duplicate(self):
        """Дорожка внутри своего пула — обычная раскладка по ролям, а не второй
        участник: правило срабатывает только на совпадение с чужим процессом."""
        assert status(scorer.evaluate(self._doc(lane_owner="Proc_2")),
                      "role_pools") == PASSED

    def test_distinct_participants_pass(self):
        assert status(scorer.evaluate(self._doc(pool_name="Поставщик")),
                      "role_pools") == PASSED

    def test_scheme_without_collaboration_is_not_applicable(self):
        assert status(scorer.evaluate(simple_xml()), "role_pools") == NOT_APPLICABLE

    def test_real_live_scheme_reports_its_role_pools(self):
        """Артефакт живого прогона: четыре роли стоят отдельными пулами рядом с
        «ВкусВиллом» — именно их оракул харнесса звал «ролей больше, чем
        оправдано текстом»."""
        result = scorer.evaluate(WAREHOUSE.read_text(encoding="utf-8"))
        assert elements(result, "role_pools") == [
            "Participant_4", "Participant_5", "Participant_6", "Participant_8"]

    def test_rule_cost_is_like_the_rest(self):
        assert 6 <= scorer.rules["role_pools"]["weight"] <= 15

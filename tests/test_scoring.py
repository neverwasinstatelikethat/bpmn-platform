"""Rule-based скоринг BPMN-схем: контракт ответа, правила и стоимость обхода.

Фиксирует поведение после правок ревью: нормированный балл по применимым
правилам, полные details/details_meta, отказ от мутации схемы, линейный
поиск циклов и защищённые циклы как норма BPMN.

Второй круг — связность участников: пул-декорация (`startEvent → endEvent`),
изолированный пул без messageFlow, развилка шлюза без схождения и граничное
событие без хозяина больше не проходят скоринг."""
import inspect
import json
import re
import time
from pathlib import Path

import pytest

from core import llm_client
from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import (
    BUSINESS_RULES,
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


def open_rework_xml(exit_condition=None) -> str:
    """Rework, из которого выходят «просто так»: единственная нога наружу (G→E)
    без условия. Нога в доработку условие несёт, и `guarded_cycles` такую
    схему пропускает: развилка есть, критерия возврата нет."""
    return _doc(
        '<startEvent id="S" name="Поступила задача"/>' + _flow("f0", "S", "A")
        + '<userTask id="A" name="Согласовать"/>' + _flow("f1", "A", "G")
        + '<exclusiveGateway id="G" name="Есть замечания?"/>'
        + _flow("f2", "G", "B", "да")
        + '<manualTask id="B" name="Доработать"/>' + _flow("f3", "B", "A")
        + _flow("f4", "G", "E", exit_condition)
        + '<endEvent id="E" name="Согласовано"/>')


def approval_lane_xml(tasks: int = 4, break_after: int = 0) -> str:
    """Дорожка «Согласующие» из `tasks` ручных задач.

    `break_after` — после какой задачи вставить развилку: шлюз рвёт линию, и
    рядом стоящих согласований остаётся меньше `APPROVAL_CHAIN_MIN`."""
    ids = [f"U{i}" for i in range(1, tasks + 1)]
    refs = "".join(f'<flowNodeRef>{n}</flowNodeRef>' for n in ids)
    body = [f'<laneSet id="LS"><lane id="L1" name="Согласующие">{refs}</lane></laneSet>',
            '<startEvent id="S" name="Начало"/>', _flow("f0", "S", ids[0])]
    flows = []
    for i, node in enumerate(ids):
        body.append(f'<userTask id="{node}" name="Согласование {i + 1}"/>')
        nxt = ids[i + 1] if i + 1 < len(ids) else "E"
        if break_after and i + 1 == break_after:
            body.append('<exclusiveGateway id="G" name="Хватит согласующих?"/>')
            flows.append(_flow(f"fb{i}", node, "G"))
            flows.append(_flow("fg", "G", nxt, "да"))
            flows.append(_flow("fn", "G", "E", "нет"))
        else:
            flows.append(_flow(f"fb{i}", node, nxt))
    body.append('<endEvent id="E" name="Готово"/>')
    return _doc("".join(body) + "".join(flows))


def lanes_split_xml(split) -> str:
    """Процесс с дорожками, в которых по числу работ из `split`.

    Узлы — `userTask` и лежат в дорожках через `flowNodeRef`, как их рисует
    bpmn-js: `lane_overload` читает ровно эту раскладку."""
    ids = [f"A{i}" for i in range(1, sum(split) + 1)]
    lanes, taken = "", 0
    for n, count in enumerate(split, 1):
        refs = "".join(f'<flowNodeRef>{ids[taken + i]}</flowNodeRef>'
                       for i in range(count))
        taken += count
        lanes += f'<lane id="L{n}" name="Роль {n}">{refs}</lane>'
    body = ['<startEvent id="S" name="Начало"/>', _flow("f0", "S", ids[0])]
    for i, node in enumerate(ids):
        body.append(f'<userTask id="{node}" name="Шаг {i + 1}"/>')
        body.append(_flow(f"f{i}", node, ids[i + 1] if i + 1 < len(ids) else "E"))
    body.append('<endEvent id="E" name="Готово"/>')
    return _doc(f'<laneSet id="LS">{lanes}</laneSet>' + "".join(body))


def sla_xml(first_inner: str = "<timerEventDefinition/>",
            second_inner: str = "") -> str:
    """Два ожидания в одном процессе: первое — с `first_inner`, второе — с
    `second_inner`. Без таймера нигде процесс про сроки не вспоминает вовсе."""
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "W")
        + f'<intermediateCatchEvent id="W" name="Ожидание оплаты">{first_inner}'
          '</intermediateCatchEvent>'
        + _flow("f2", "W", "W2")
        + f'<intermediateCatchEvent id="W2" name="Ожидание отгрузки">{second_inner}'
          '</intermediateCatchEvent>'
        + _flow("f3", "W2", "E") + '<endEvent id="E" name="Готово"/>')


def raced_wait_xml(gateway: str = "parallelGateway", timer_to: str = "E") -> str:
    """Ожидание и таймер — две ноги одной развилки `gateway`.

    Сход ноги таймера на продолжение ожидания задаёт `timer_to`: с ним
    развилка читается как гонка «ответ или срок», без него ждущий токен
    так и стоит. Развилка собирается общим билдером, чтобы формы
    (parallel / eventBased / exclusive, сход или отдельный конец) отличались
    ровно тем, что в них проверяется."""
    tail = ('<endEvent id="E" name="Готово"/>'
            + ('<endEvent id="E2" name="Просрочка"/>' if timer_to != "E" else ""))
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
        + '<userTask id="A" name="Отправить запрос"/>' + _flow("f2", "A", "G")
        + f'<{gateway} id="G" name="Ответ или срок?"/>'
        + _flow("f3", "G", "W") + _flow("f4", "G", "T")
        + '<intermediateCatchEvent id="W" name="Ожидание ответа">'
          '<messageEventDefinition/></intermediateCatchEvent>'
        + _flow("f5", "W", "E")
        + '<intermediateCatchEvent id="T" name="Срок ответа вышел">'
          '<timerEventDefinition><timeDuration>PT2H</timeDuration>'
          '</timerEventDefinition></intermediateCatchEvent>'
        + _flow("f6", "T", timer_to) + tail)


def pingpong_xml(forward=True, backward=True, pool_endpoints=False) -> str:
    """Два пула и messageFlow между ними: в одну сторону, в обе, концами-шагами
    или ссылками на пулы целиком.

    Ответ всегда идёт по той же паре концов, что и запрос, — иначе это уже не
    пикинг, а нормальная переписка разных документов (`handoff_pingpong` меряет
    возврат именно в тот же шаг).
    """
    a_end = "P1" if pool_endpoints else "A1"
    flows = ""
    if forward:
        flows += f'<messageFlow id="M1" sourceRef="{a_end}" targetRef="A2"/>'
    if backward:
        flows += f'<messageFlow id="M2" sourceRef="A2" targetRef="{a_end}"/>'
    return pools_doc([pool_body(1), pool_body(2)], flows)


def renamed_xml(bpmn_xml: str) -> str:
    """Та же схема с вывернутыми на бессмыслицу именами всех элементов.

    Нужна ровно одна проверка: сигнал, который читается из графа, переименование
    не трогает, а сигнал по русскому слову в имени — ломает."""
    counter = iter(range(1000))
    return re.sub(r'name="[^"]*"', lambda m: f'name="Z{next(counter)}"', bpmn_xml)


# Схема, в которой ломается решительно всё, что можно сломать нотацией:
# два старта на один пул, тупики без имён, цикл без развилки, развилка без
# условий и без схождения, граничное событие без хозяина, безымянное ожидание,
# ни одной дорожки и ни одной документации.
BROKEN_ALL_XML = _doc(
    '<startEvent id="S" name="Начало"/>'
    '<startEvent id="S2"/>'
    '<userTask id="D"/>'
    + _flow("f1", "S", "A")
    + '<userTask id="A" name="Собрать"/>'
    + _flow("f2", "A", "P")
    + '<parallelGateway id="P" name="Параллельно"/>'
    + _flow("f3", "P", "B")
    + '<userTask id="B" name="Упаковать"/>'
    + _flow("f4", "B", "A")
    + _flow("f5", "A", "G")
    + '<exclusiveGateway id="G" name="Оплачено?"/>'
    + _flow("f6", "G", "C")
    + '<userTask id="C" name="Отгрузить"/>'
    + _flow("f7", "C", "W")
    + '<intermediateCatchEvent id="W" name="Ожидание"/>'
    + _flow("f8", "W", "E")
    + _flow("f9", "G", "X")
    + '<userTask id="X" name="Архив"/>'
    + '<endEvent id="E" name="Готово"/>'
    + '<boundaryEvent id="BN" name="Просрочка" attachedToRef="нет-такого"/>'
    + '<manualTask id="M" name="Проверить"/>'
    + _flow("f10", "M", "E")
)


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
        # четыре новых правила и 8 — семнадцатое, «роль не бывает участником»;
        # последние 36 — пять бизнес-правил (`BUSINESS_RULES`); ещё 26 — три
        # правила границ потока (`cross_pool_flow` 10, `message_flow_ends` 8,
        # `flow_ends_legal` 8), найденные переписью корпуса и историей прогонов
        # контура, а не фикстурами.
        # Ещё 8 — `timer_without_schedule`: мёртвый срок (108 схем корпуса держат
        # `timerEventDefinition` без значения).
        assert sum(r["weight"] for r in scorer.rules.values()) == 224
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

    def test_naming_requires_a_label_from_an_activity_not_from_every_node(self):
        """Имя у шлюза и события в BPMN факультативно: bpmn-js создаёт их
        безымянными, а ветку exclusive-шлюза подписывают `condition`, а не name.
        На реальных файлах корпуса правило снимало балл за 15380 безымянных узлов,
        из которых задачами были 18 — то есть ругалось оно почти исключительно на
        то, что нотой не требуется. Безымянный шаг остаётся нарушением."""
        xml = _doc(
            '<startEvent id="S"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Проверить комплект"/>' + _flow("f2", "A", "G")
            + '<exclusiveGateway id="G"/>' + _flow("f3", "G", "B")
            + '<serviceTask id="B" name="Отгрузить"/>' + _flow("f4", "B", "E")
            + '<endEvent id="E"/>')
        result = scorer.evaluate(xml)
        assert status(result, "naming") == PASSED, result["details_meta"]["naming"]
        assert elements(result, "naming") == []

    def test_documentation_is_measured_on_the_elements_the_advice_names(self):
        """Совет правила просит текст у шагов, и мерить надо у шагов: по
        корпусу снималось 18552 элементов документации, из них задачи — 1828,
        остальное события и шлюзы. Покрытие текстом событий к плану правки
        отношения не имеет, а доля по всем узлам делала нарушение
        неустраняемым легальным пакетом."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Проверить"><documentation>сверяем '
              'позиции накладной</documentation></userTask>' + _flow("f2", "A", "G")
            + '<exclusiveGateway id="G" name="Комплект полон?"/>'
            + _flow("f3", "G", "B")
            + '<serviceTask id="B" name="Отгрузить"><documentation>списываем '
              'остатки в WMS</documentation></serviceTask>' + _flow("f4", "B", "E")
            + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "documentation") == PASSED

    def test_undocumented_steps_still_fail_after_the_narrowing(self):
        """Сужение меры не должно превращать правило в декорацию: три шага без
        текста из четырёх — это нарушение, и названы его нарушители."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
            + '<userTask id="A" name="Проверить"/>' + _flow("f2", "A", "B")
            + '<serviceTask id="B" name="Отгрузить"/>' + _flow("f3", "B", "C")
            + '<manualTask id="C" name="Упаковать"/>' + _flow("f4", "C", "D")
            + '<userTask id="D" name="Принять"><documentation>мастер '
              'расписывается</documentation></userTask>' + _flow("f5", "D", "E")
            + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "documentation") == FAILED
        assert set(elements(result, "documentation")) == {"A", "B", "C"}

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
        """Два пула — два стартовых события; одно на двоих не проходит.

        Лишние старты в одном процессе — не нарушение: `merge_participants` по
        подсказке `role_pools` легально оставляет в пуле по входу на прежнюю
        цепочку, и штрафовать за это правило, конкурирующее с тем же слиянием,
        не должно.
        """
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
        assert any("без события старта 1 из 2 участников" in rec
                   for rec in result["recommendations"])

    def test_two_starts_in_one_pool_are_not_a_regression(self):
        """Слитая роль оставляет два старта в одном пуле: правило не штрафует."""
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
               '<collaboration id="C1">'
               '<participant id="P1" name="Магазин" processRef="Proc_1"/>'
               '</collaboration>'
               '<process id="Proc_1" name="Магазин">'
               '<startEvent id="S1" name="Нужен товар"/>' + _flow("f1", "S1", "A")
               + '<userTask id="A" name="Оформить запрос"/>' + _flow("f2", "A", "E1")
               + '<endEvent id="E1" name="Товар получен"/>'
               '<startEvent id="S2" name="Запрос получен"/>' + _flow("f3", "S2", "B")
               + '<userTask id="B" name="Принять запрос"/>' + _flow("f4", "B", "E2")
               + '<endEvent id="E2" name="Заказ собран"/></process></definitions>')
        result = scorer.evaluate(xml)
        assert status(result, "start_event") == PASSED
        assert status(result, "end_event") == PASSED


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
        # рекомендация называет операцию и приёмник: на схеме из восьми пулов
        # «сливай роль» без адресата невыполнимо
        assert any("merge_participants" in rec
                   and "Кладовщик → ВкусВилл" in rec
                   for rec in result["recommendations"])

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


class TestActionableRecommendations:
    """Совет скоринга читает не только человек: блок «УЗКИЕ МЕСТА ПО СКОРИНГУ»
    уходит в промпт улучшения дословно, а модель умеет трогать схему только
    пакетом операций из `OP_SPEC`.
    """

    def test_every_failed_recommendation_names_an_operation_or_refuses_it(self):
        """Правило, совет которого не выражается пакетом операций, — жалоба без
        работы: модель тратит на него единственный корректирующий повтор и всё
        равно ничего не меняет в схеме.

        Отказ от операции легален, но только если он сказан прямо и не
        противоречит себе: хвост «→ чинится: add_event» после фразы «правка не
        выражается операцией» читается моделью как разрешение выдумать операнд
        (по корпусу таких противоречий было 295 текстов в пяти правилах)."""
        from core.bpmn_edits import OP_SPEC
        from core.bpmn_scoring import MANUAL_FIX_MARKERS

        ev = scorer.evaluate(BROKEN_ALL_XML)
        assert len(ev["recommendations"]) >= 10
        for name, rec in ev["recommendations_by_rule"].items():
            refuses_openly = (" → чинится:" not in rec
                              and any(m in rec for m in MANUAL_FIX_MARKERS))
            assert {o for o in OP_SPEC if o in rec} or refuses_openly, \
                f"{name}: {rec[:60]}"

    def test_recommendations_by_rule_is_the_same_view(self):
        """Новый ключ — тот же текст под именем правила, а не второе описание
        тех же нарушений: разошедшиеся формулировки означают, что модель
        читает одну версию, а пользователь другую."""
        for xml in (BROKEN_ALL_XML, simple_xml(gateway_conditions=False), straight_xml()):
            ev = scorer.evaluate(xml)
            assert list(ev["recommendations_by_rule"].values()) == ev["recommendations"]
            assert set(ev["recommendations_by_rule"]) <= set(scorer.rules)
            failed = {name for name, entry in ev["details_meta"].items()
                      if entry["status"] == FAILED}
            assert set(ev["recommendations_by_rule"]) == failed

    def test_tail_is_appended_once_and_keeps_the_original_wording(self):
        """Форма хвоста одна на все правила: иначе «чинится» пришлось бы
        искать по каждому правилу отдельно, а лимит промпта режет список.

        Хвост вправе отсутствовать — когда совет сам отказался от операции и
        вызова в тексте нет (`MANUAL_FIX_MARKERS`), — но не может идти дважды
        или стоять не в конце."""
        ev = scorer.evaluate(BROKEN_ALL_XML)
        assert all(rec.count(" → чинится: ") <= 1 for rec in ev["recommendations"])
        for name, rec in ev["recommendations_by_rule"].items():
            assert rec.startswith(scorer.rules[name]["message"])
            tail = ", ".join(scorer.rules[name]["action"])
            assert " → чинится:" not in rec or rec.endswith(tail), rec[:80]

    def test_every_rule_advertises_real_operations(self):
        """Действие обязано быть у каждого правила, а не только у упавших:
        схема — это набор операций аплайера, и правило про пустой пул не может
        ссылаться на операцию, которой в словаре нет."""
        from core.bpmn_edits import OP_SPEC

        for name, rule in scorer.rules.items():
            assert rule["action"], name
            assert set(rule["action"]) <= set(OP_SPEC), name

    def test_role_pools_wording_survives_the_tail(self):
        """У `role_pools` операция встроена в формулировку с самого начала —
        текст правила не переписан, хвост просто дописан следом."""
        ev = scorer.evaluate(TestRolePools._doc())
        rec = ev["recommendations_by_rule"]["role_pools"]
        assert rec.startswith(scorer.rules["role_pools"]["message"])
        assert "merge_participants" in scorer.rules["role_pools"]["message"]
        assert rec.count("merge_participants") == 2

    def test_unparsable_scheme_has_no_rule_view(self):
        """Ошибка разбора — не нарушение правила: словарик по правилам пуст,
        а плоский список по-прежнему говорит, что схему не прочитать."""
        ev = scorer.evaluate("<definitions><не-xml")
        assert ev["recommendations_by_rule"] == {}
        assert "BPMN" in ev["recommendations"][0]


class TestBusinessRules:
    """Пять правил про содержание процесса, а не про нотацию."""

    def test_five_rules_are_registered_and_repairable(self):
        from core.bpmn_edits import OP_SPEC

        assert BUSINESS_RULES == {"rework_loop", "handoff_pingpong",
                                  "approval_chain", "lane_overload",
                                  "wait_without_sla"}
        for name in BUSINESS_RULES:
            assert name in scorer.rules, name
            assert name in scorer._checks, name
            assert scorer.rules[name]["action"], name
            assert set(scorer.rules[name]["action"]) <= set(OP_SPEC), name
        assert len(scorer.rules) == 26
        # 36 — вклад бизнес-правил в знаменатель: 154 + 36 + 26 (три правила
        # границ потока: `cross_pool_flow`, `message_flow_ends`,
        # `flow_ends_legal`)
        assert sum(scorer.rules[n]["weight"] for n in BUSINESS_RULES) == 36
        assert sum(r["weight"] for r in scorer.rules.values()) == 224

    def test_business_rules_are_not_keyword_matchers(self):
        """Диагноз по русскому слову в имени — словарь, а не диагноз: он
        протекает в промпт улучшения ответы сценариев eval и ломается на схеме,
        где работу назвали иначе. Переименование всех элементов сигнал
        бизнес-правил не меняет."""
        schemes = [BROKEN_ALL_XML, rework_xml(), pingpong_xml(),
                   approval_lane_xml(), lanes_split_xml([5, 2, 1]), sla_xml(),
                   simple_xml(), WAREHOUSE.read_text(encoding="utf-8")]
        for xml in schemes:
            before = {n: status(scorer.evaluate(xml), n) for n in BUSINESS_RULES}
            after = {n: status(scorer.evaluate(renamed_xml(xml)), n)
                     for n in BUSINESS_RULES}
            assert before == after, xml[:80]

    def test_business_checks_never_read_the_name_attribute(self):
        """Страховка от соблазна: сигнал выводится из графа, и в теле проверки
        нет ни одного обращения к `name` — имена элементов живут в текстах
        рекомендаций, куда их кладёт `_Schema.name_of`."""
        module = inspect.getmodule(scorer)
        for name in BUSINESS_RULES:
            body = inspect.getsource(getattr(module, f"_check_{name}"))
            code = body.split('"""')[0] + body.split('"""')[2]
            assert 'get("name")' not in code, name


class TestReworkLoop:
    """Повтор без названного критерия выхода — «переделывай, пока не надоест»."""

    def test_cycle_without_exit_criterion_fails(self):
        result = scorer.evaluate(open_rework_xml())
        assert status(result, "rework_loop") == FAILED
        # нарушители — работы цикла, а не шлюз и не событие
        assert elements(result, "rework_loop") == ["A", "B"]
        rec = result["recommendations_by_rule"]["rework_loop"]
        # названа дуга, на которой просят критерий: на неё и ставится условие
        assert "f4" in rec
        assert "add_condition" in rec

    def test_rework_with_a_conditioned_exit_leg_passes(self):
        """Тот же маршрут, но выход из цикла назван: `guarded_cycles` и это
        правило — разные вопросы, и легитимный rework не обязан платиться
        дважды за одну форму."""
        assert status(scorer.evaluate(open_rework_xml("нет")), "rework_loop") == PASSED
        assert status(scorer.evaluate(rework_xml()), "rework_loop") == PASSED

    def test_default_leg_counts_as_a_criterion(self):
        """Выход по умолчанию — тоже критерий: атрибут `default` шлюза говорит,
        по чему процесс отказывается от повтора, отдельное условие не нужно."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "A")
            + '<userTask id="A" name="Согласовать"/>' + _flow("f1", "A", "G")
            + '<exclusiveGateway id="G" name="Замечания?" default="f4"/>'
            + _flow("f2", "G", "B", "да") + '<manualTask id="B" name="Доработать"/>'
            + _flow("f3", "B", "A")
            + _flow("f4", "G", "E") + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "rework_loop") == PASSED

    def test_scheme_without_cycles_is_not_applicable(self):
        """Повторов нет — проверять нечего: вес не должен висеть на любой
        прямой схеме."""
        assert status(scorer.evaluate(straight_xml()), "rework_loop") == NOT_APPLICABLE


class TestHandoffPingpong:
    """Два пула, гоняющие одну работу туда-сюда, — нерешённый вопрос об
    ответственности, а не «много messageFlow»."""

    def test_two_way_exchange_names_the_pools(self):
        result = scorer.evaluate(pingpong_xml())
        assert status(result, "handoff_pingpong") == FAILED
        assert elements(result, "handoff_pingpong") == ["P1", "P2"]
        rec = result["recommendations_by_rule"]["handoff_pingpong"]
        # адресат — имена пулов, а не id шагов: в операцию идёт имя пула
        assert "Участник 1 ↔ Участник 2" in rec
        assert "M1" in rec and "M2" in rec

    def test_endpoint_on_the_pool_itself_is_resolved(self):
        """messageFlow ссылается и на участник целиком (см.
        `participant_interacts`): разрешение общее, иначе в совете попался бы
        id вместо имени."""
        result = scorer.evaluate(pingpong_xml(pool_endpoints=True))
        assert status(result, "handoff_pingpong") == FAILED
        assert "Участник 1 ↔ Участник 2" in result["recommendations_by_rule"][
            "handoff_pingpong"]

    def test_one_way_handoff_passes(self):
        assert status(scorer.evaluate(pingpong_xml(backward=False)),
                      "handoff_pingpong") == PASSED

    def test_single_pool_scheme_is_not_applicable(self):
        assert status(scorer.evaluate(pools_doc([pool_body(1)])),
                      "handoff_pingpong") == NOT_APPLICABLE


class TestApprovalChain:
    """Четыре ручные задачи подряд в одной дорожке без развилки — цепочка
    согласований."""

    def test_four_user_tasks_in_a_lane_fail(self):
        result = scorer.evaluate(approval_lane_xml(tasks=4))
        assert status(result, "approval_chain") == FAILED
        assert elements(result, "approval_chain") == ["U1", "U2", "U3", "U4"]
        rec = result["recommendations_by_rule"]["approval_chain"]
        # дорожка названа именем: `move_to_lane` и `add_lane` берут именно его
        assert "«Согласующие»" in rec

    def test_chain_broken_by_a_gateway_passes(self):
        """Тот же набор задач, но развилка внутри: четыре согласующих — это
        маршрут с решением, а не конвейер подписей."""
        assert status(scorer.evaluate(approval_lane_xml(tasks=4, break_after=2)),
                      "approval_chain") == PASSED

    def test_chain_without_lanes_is_not_invisible(self):
        """Правило считало цепочку только внутри названной дорожки и на схеме без
        дорожек молчало (`not_applicable`), хотя именно там разделения
        согласующих нет вовсе: на всём реальном корпусе (367 схем) правило не
        сработало ни разу. Отсутствие дорожек — худший случай, а не «нечего
        проверять», и совет обязан сказать, что разводить надо созданием дорожек."""
        xml = _doc(
            '<startEvent id="S" name="Заявка"/>' + _flow("f0", "S", "U1")
            + "".join(f'<userTask id="U{i}" name="Согласовать {i}"/>'
                      + _flow(f"f{i}", f"U{i}", f"U{i + 1}")
                      for i in range(1, 4))
            + '<userTask id="U4" name="Согласовать 4"/>' + _flow("f4", "U4", "E")
            + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "approval_chain") == FAILED, result["details_meta"]["approval_chain"]
        assert elements(result, "approval_chain") == ["U1", "U2", "U3", "U4"]
        assert "add_lane" in result["recommendations_by_rule"]["approval_chain"]

    def test_generic_tasks_count_as_the_chain_too(self):
        """`task` — шаг без типа, именно так в bpmn-js рисуют человеческое
        действие от руки; правило, считающее только `userTask`, на всём реальном
        корпусе (367 схем) не увидело ни одной цепочки. Безымянный по типу шаг
        согласования остаётся согласованием."""
        xml = _doc(
            '<startEvent id="S" name="Заявка"/>' + _flow("f0", "S", "U1")
            + "".join(f'<task id="U{i}" name="Виза {i}"/>'
                      + _flow(f"f{i}", f"U{i}", f"U{i + 1}")
                      for i in range(1, 4))
            + '<task id="U4" name="Виза 4"/>' + _flow("f4", "U4", "E")
            + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "approval_chain") == FAILED

    def test_three_in_a_row_is_not_a_chain(self):
        """Три согласования — не цепочка, и правило обязано сказать «нечего
        проверять», а не сделать вид, что проверило: n/a не снимает вес, а
        PASSED на схеме, где порога не случилось, прятал бы счётчик."""
        assert status(scorer.evaluate(approval_lane_xml(tasks=3)),
                      "approval_chain") == NOT_APPLICABLE

    def test_laneless_scheme_is_not_applicable(self):
        assert status(scorer.evaluate(straight_xml()),
                      "approval_chain") == NOT_APPLICABLE


class TestLaneOverload:
    """Одна дорожка держит больше 60% работ процесса при трёх и более."""

    def test_dominant_lane_reports_its_share(self):
        result = scorer.evaluate(lanes_split_xml([5, 2, 1]))
        assert status(result, "lane_overload") == FAILED
        assert elements(result, "lane_overload") == ["L1"]
        rec = result["recommendations_by_rule"]["lane_overload"]
        assert "«Роль 1»" in rec and "5 из 8" in rec and "62%" in rec

    def test_even_split_passes(self):
        assert status(scorer.evaluate(lanes_split_xml([3, 3, 2])),
                      "lane_overload") == PASSED

    def test_two_lane_monopoly_fails(self):
        """Две дорожки с 4 работами из 5 — это монополия, а не «делает / ждёт».
        Прежняя калитка в три дорожки такой процесс не смотрела вовсе, оракул
        (`no_overloaded_lane`) ловил, и контур улучшения оставался без подсказки
        ровно там, где у процесса один носитель."""
        result = scorer.evaluate(lanes_split_xml([4, 1]))
        assert status(result, "lane_overload") == FAILED
        assert elements(result, "lane_overload") == ["L1"]
        rec = result["recommendations_by_rule"]["lane_overload"]
        assert "4 из 5" in rec and "80%" in rec and "(L2)" in rec

    def test_two_lane_split_within_the_higher_bar_passes(self):
        """При двух дорожках порог выше (75%): 2:1 и даже ровно 3:4 — это
        разделение ролей, а не узкое место."""
        for split in ([2, 1], [3, 1]):
            assert status(scorer.evaluate(lanes_split_xml(split)),
                          "lane_overload") == PASSED, split

    def test_single_lane_process_is_not_applicable(self):
        """Одна дорожка — сравнивать долю не с чем: правило про перевес между
        исполнителями, а не про пустой `laneSet` (это `pool_lanes`)."""
        assert status(scorer.evaluate(lanes_split_xml([3])),
                      "lane_overload") == NOT_APPLICABLE

    def test_process_without_lanes_is_not_applicable(self):
        assert status(scorer.evaluate(straight_xml()),
                      "lane_overload") == NOT_APPLICABLE


class TestWaitWithoutSla:
    """Блокирующее ожидание обязано иметь срок в модели."""

    def test_untimed_wait_next_to_a_timer_fails(self):
        result = scorer.evaluate(sla_xml())
        assert status(result, "wait_without_sla") == FAILED
        # нарушителем названо само ожидание (id), а не процесс и не таймер
        assert elements(result, "wait_without_sla") == ["W2"]

    def test_untimed_wait_fails_when_the_scheme_models_no_time_at_all(self):
        """Хронометраж не моделирован ни разу — это и есть находка, а не «не
        применимо»: прежняя редакция молчала ровно на тех схемах, где висящее
        ожидание опаснее всего («ждём ответ» без срока нигде)."""
        result = scorer.evaluate(sla_xml(first_inner=""))
        assert status(result, "wait_without_sla") == FAILED
        assert elements(result, "wait_without_sla") == ["W", "W2"]
        assert "таймеров в схеме нет" in result["recommendations_by_rule"][
            "wait_without_sla"]

    def test_timed_waits_are_not_violations(self):
        assert status(scorer.evaluate(sla_xml(
            second_inner="<timerEventDefinition/>")), "wait_without_sla") == PASSED

    def test_boundary_timer_on_the_wait_counts_as_its_sla(self):
        """Граничный таймер, прикреплённый к ожиданию, по семантике BPMN его и
        прерывает. Раньше правило его не видело и при этом советовало
        `add_boundary_event` — предложенная правка не снимала нарушение."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "W")
            + '<intermediateCatchEvent id="W" name="Ожидание ответа">'
              '<messageEventDefinition/></intermediateCatchEvent>'
            + '<boundaryEvent id="BT" name="Срок ответа" attachedToRef="W">'
              '<timerEventDefinition/></boundaryEvent>'
            + _flow("f2", "W", "E") + _flow("f3", "BT", "E")
            + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "wait_without_sla") == PASSED

    def test_receive_task_without_a_timer_is_an_unbounded_wait(self):
        """`receiveTask` («ждём подпись получателя») — ожидание не меньше
        catch-события: токен стоит до сообщения. Прежняя редакция его не знала,
        и в эталонной складской сцене такого шага для правила не существовало."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "R")
            + '<receiveTask id="R" name="Зафиксировать подпись"/>'
            + _flow("f2", "R", "E") + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "wait_without_sla") == FAILED
        assert elements(result, "wait_without_sla") == ["R"]

    def test_many_unbounded_waits_get_one_recipe_not_five(self):
        """Рецепт срока занимает строку промпта целиком, а блок «УЗКИЕ МЕСТА»
        ограничен по числу строк: пять одинаковых рецептов вытеснили бы другие
        правила. Экономия честная — нарушители остаются в `elements` все."""
        waits = "".join(
            f'<intermediateCatchEvent id="W{i}" name="Ожидание {i}">'
            '<messageEventDefinition/></intermediateCatchEvent>'
            for i in range(1, 6))
        flows = _flow("f0", "S", "W1") + "".join(
            _flow(f"fm{i}", f"W{i}", f"W{i + 1}") for i in range(1, 5))
        xml = _doc('<startEvent id="S" name="Начало"/>' + flows
                   + _flow("fe", "W5", "E") + '<endEvent id="E" name="Готово"/>'
                   + waits)
        result = scorer.evaluate(xml)
        assert elements(result, "wait_without_sla") == ["W1", "W2", "W3", "W4",
                                                        "W5"]
        rec = result["recommendations_by_rule"]["wait_without_sla"]
        assert "и ещё 2" in rec, rec
        assert rec.count("add_gateway(") == 3, rec

    def test_wait_in_another_process_is_not_covered_by_foreign_timer(self):
        """Таймер чужого пула сроку этого ожидания не назначает — нарушение
        именно у него, а не у процесса целиком."""
        xml = pools_doc(
            ['<startEvent id="S1" name="Старт"/>' + _flow("a1", "S1", "W1")
             + '<intermediateCatchEvent id="W1" name="Ожидание ответа"/>'
             + _flow("a2", "W1", "E1") + '<endEvent id="E1" name="Финиш"/>',
             '<startEvent id="S2" name="Старт"/>' + _flow("b1", "S2", "A2")
             + '<userTask id="A2" name="Отгрузить"/>'
             + '<boundaryEvent id="BT" name="Срок отгрузки" attachedToRef="A2">'
               '<timerEventDefinition/></boundaryEvent>'
             + _flow("b2", "BT", "E2") + _flow("b3", "A2", "E2")
             + '<endEvent id="E2" name="Финиш"/>'],
            '<messageFlow id="M1" sourceRef="A2" targetRef="W1"/>')
        result = scorer.evaluate(xml)
        assert status(result, "wait_without_sla") == FAILED
        assert elements(result, "wait_without_sla") == ["W1"]

    def test_parallel_race_with_a_join_bounds_the_wait(self):
        """Гонка «ответ или срок» на параллельной развилке: граничное событие
        на `intermediateCatchEvent` нотой запрещено, поэтому это и есть легальная
        форма срока для ожидания. Без неё правило требовало правку, которую
        аплайер не принимает."""
        assert status(scorer.evaluate(raced_wait_xml()),
                      "wait_without_sla") == PASSED

    def test_event_based_gateway_bounds_the_wait_without_a_join(self):
        """`eventBasedGateway` по своей семантике выбирает одну из веток, так
        что нога таймера ограничивает ожидание и без схода: сработает срок —
        сообщения не будет никогда."""
        assert status(scorer.evaluate(
            raced_wait_xml("eventBasedGateway", timer_to="E2")),
            "wait_without_sla") == PASSED

    def test_timer_deep_in_a_sibling_branch_is_not_the_waits_deadline(self):
        """Таймер обязан быть прямой ногой развилки: в чужой ветке за задачей он
        стоит не «сроком ожидания», а хронометражем соседнего шага, и сход
        обратно на маршрут ожидания ему не помогает.

        Форма «развилка → ожидание | развилка → таймер → свой конец» нарушением
        НЕ считается: срок в модели записан, и правило отвечает за это, а не за то,
        прервёт ли движок ожидание (для прерывания служат граничное событие и
        `eventBasedGateway` — они в подсказке первыми)."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
            + '<parallelGateway id="G" name="Работа и контроль"/>'
            + _flow("f1", "G", "W") + _flow("f2", "G", "X")
            + '<intermediateCatchEvent id="W" name="Ожидание ответа">'
              '<messageEventDefinition/></intermediateCatchEvent>'
            + _flow("f3", "W", "E")
            + '<userTask id="X" name="Регулярная сверка"/>' + _flow("f4", "X", "T")
            + '<intermediateCatchEvent id="T" name="Срок сверки">'
              '<timerEventDefinition><timeDuration>PT2H</timeDuration>'
              '</timerEventDefinition></intermediateCatchEvent>'
            + _flow("f5", "T", "E") + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "wait_without_sla") == FAILED
        assert elements(result, "wait_without_sla") == ["W"]

    def test_exclusive_fork_to_a_timer_is_not_a_deadline(self):
        """Исключительная развилка выбирает ветку в момент развилки: нарисованный
        рядом таймер не ждёт ответа, а предлагает вместо него другой сценарий,
        поэтому ожидание вниз по маршруту остаётся без срока."""
        assert status(scorer.evaluate(raced_wait_xml("exclusiveGateway")),
                      "wait_without_sla") == FAILED

    def test_timer_downstream_of_the_wait_is_not_its_deadline(self):
        """Таймер ПОСЛЕ ожидания срок тому не даёт: чтобы до него дойти,
        сообщение уже должно прийти."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "W")
            + '<intermediateCatchEvent id="W" name="Ожидание ответа">'
              '<messageEventDefinition/></intermediateCatchEvent>'
            + _flow("f2", "W", "T")
            + '<intermediateCatchEvent id="T" name="Пауза">'
              '<timerEventDefinition><timeDuration>PT2H</timeDuration>'
              '</timerEventDefinition></intermediateCatchEvent>'
            + _flow("f3", "T", "E") + '<endEvent id="E" name="Готово"/>')
        result = scorer.evaluate(xml)
        assert status(result, "wait_without_sla") == FAILED
        assert elements(result, "wait_without_sla") == ["W"]


class TestFlowBoundaries:
    """Границы потока: дуга управления не выходит из своего процесса, а у
    обмена оба конца обязаны быть названы.

    Классы найдены переписью корпуса, а не фикстурами: 8 межпуловых дуг в 5
    схемах и 8 оборванных обменов в 6 схемах из 367. Молчание стоило не
    косметики — по межпуловой дуге исполнитель процесса не идёт, но схема
    выглядит связной и пользователю, и `no_isolated`, и `handoff_pingpong`
    (тот читает концы и не спорит: обмен между пулами выглядит как обмен).
    Аплайер ограничение знал раньше линейки: `connect` с `flow_type=sequence`
    между процессами отвергается с подсказкой «используйте flow_type=message»,
    то есть запрет был транспортом, а не проверяемым свойством схемы.
    """

    def _two_pools(self, extra=""):
        """Два пула по `старт → шаг → финиш`, плюс опциональная дуга наружу."""
        return pools_doc([pool_body(1) + extra, pool_body(2)])

    def test_flow_between_pools_is_charged(self):
        result = scorer.evaluate(self._two_pools(_flow("xF", "A1", "A2")))
        assert status(result, "cross_pool_flow") == FAILED
        assert elements(result, "cross_pool_flow") == ["xF"]

    def test_legs_inside_one_pool_are_not_charged(self):
        result = scorer.evaluate(self._two_pools())
        assert status(result, "cross_pool_flow") == PASSED

    def test_single_process_scheme_is_not_applicable(self):
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Шаг"/>' + _flow("f2", "A", "E")
                   + '<endEvent id="E" name="Конец"/>')
        assert status(scorer.evaluate(xml), "cross_pool_flow") == NOT_APPLICABLE

    def test_lanes_of_one_pool_are_not_a_boundary(self):
        """Две дорожки одного пула — не два процесса: переезд токена между ними
        законен, и правило обязано молчать даже когда в схеме есть и второй пул."""
        pool_one = ('<laneSet id="LS"><lane id="L1" name="Кладовщик">'
                    '<flowNodeRef>S</flowNodeRef><flowNodeRef>A</flowNodeRef></lane>'
                    '<lane id="L2" name="Водитель"><flowNodeRef>B</flowNodeRef>'
                    '<flowNodeRef>E</flowNodeRef></lane></laneSet>'
                    '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                    + '<userTask id="A" name="Собрать"/>' + _flow("f2", "A", "B")
                    + '<userTask id="B" name="Везти"/>' + _flow("f3", "B", "E")
                    + '<endEvent id="E" name="Конец"/>')
        xml = pools_doc([pool_one, pool_body(2)])
        assert status(scorer.evaluate(xml), "cross_pool_flow") == PASSED

    def test_advice_names_the_message_leg_and_the_pools(self):
        text = scorer.evaluate(self._two_pools(_flow("xF", "A1", "A2")))[
            "recommendations_by_rule"]["cross_pool_flow"]
        assert "disconnect(flow='xF')" in text
        assert "connect(source='A1', target='A2', flow_type='message')" in text
        # Пулы названы именами, а не только id: модель иначе не понимает, где
        # именно кончается её процесс.
        assert "Участник 1" in text and "Участник 2" in text

    def test_advice_warns_when_the_cut_strands_a_step(self):
        """Если дуга — единственное продолжение шага внутри его пула, правка
        обнажит тупик, и `_rules_regressed` отклонит весь пакет. Предупреждение
        — часть рецепта: спросить продолжение нужно в этом же повторе."""
        body1 = ('<startEvent id="S1" name="Начало"/>' + _flow("f1", "S1", "A1")
                 + '<userTask id="A1" name="Собрать"/>' + _flow("xF", "A1", "A2")
                 + '<endEvent id="E1" name="Конец"/>')
        xml = pools_doc([body1, pool_body(2)])
        text = scorer.evaluate(xml)["recommendations_by_rule"]["cross_pool_flow"]
        assert "'A1' без выхода внутри своего процесса" in text

    def test_advice_warns_when_the_cut_strands_the_receiver(self):
        """Приёмник обнажается симметрично отправителю. Если дуга из чужого пула —
        единственный вход шага, то после перевода в сообщение у шага не остаётся
        входа в его же процессе, и `no_isolated` заряжает его следом. Раньше
        предупреждение читалось только про отправителя, и пакет, отклонённый
        `_rules_regressed` по приёмнику, не подсказывал модели, что спросить
        продолжение нужно было в этом же повторе."""
        body1 = pool_body(1) + _flow("xF", "A1", "A2")
        body2 = ('<userTask id="A2" name="Принять"/>'
                 + _flow("ef2", "A2", "E2")
                 + '<endEvent id="E2" name="Финиш"/>')
        text = scorer.evaluate(pools_doc([body1, body2]))[
            "recommendations_by_rule"]["cross_pool_flow"]
        assert "'A2' без входа внутри своего процесса" in text
        # У отправителя вход и выход свои: он под предупреждение не попадает.
        assert "'A1' без выхода" not in text

    def test_message_flow_without_an_end_is_charged(self):
        xml = pools_doc([pool_body(1), pool_body(2)],
                        message_flows='<messageFlow id="m1" sourceRef="A1" '
                                      'targetRef=""/>')
        result = scorer.evaluate(xml)
        assert status(result, "message_flow_ends") == FAILED
        assert elements(result, "message_flow_ends") == ["m1"]

    def test_message_flow_to_a_participant_is_legal(self):
        """Конец обмена имеет право быть пулом целиком: так показывают
        «заказчику» не шаг, а участника."""
        xml = pools_doc([pool_body(1), pool_body(2)],
                        message_flows='<messageFlow id="m1" sourceRef="A1" '
                                      'targetRef="P2"/>')
        assert status(scorer.evaluate(xml), "message_flow_ends") == PASSED

    def test_scheme_without_message_flows_is_not_applicable(self):
        assert status(scorer.evaluate(self._two_pools()),
                      "message_flow_ends") == NOT_APPLICABLE

    def test_broken_message_flow_explains_why_there_is_no_recipe(self):
        """Операнда здесь нет по-настоящему: какой шаг должен принять ответ,
        знает только автор процесса. Пустой текст вместо этого читался бы
        моделью как «придумай id»."""
        xml = pools_doc([pool_body(1), pool_body(2)],
                        message_flows='<messageFlow id="m1" sourceRef="A1" '
                                      'targetRef="нет-такого"/>')
        text = scorer.evaluate(xml)["recommendations_by_rule"]["message_flow_ends"]
        assert "'нет-такого'" in text
        assert "называется только по смыслу процесса" in text


    def test_flow_into_a_start_event_is_charged(self):
        """Дуга в кружок старта — не «ещё один вход»: старт запускает процесс, а
        не принимается соседом. Класс взят не из корпуса (там таких дуг 0 из
        367), а из истории прогонов: оракул ловил их в 9 случаях из 2080
        проверок итогового XML, скоринг — ни разу, и `_rules_regressed`
        пропускал пакет, который рисовал дугу «из финиша в шаг»."""
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Собрать заказ"/>'
                   + _flow("f2", "A", "E") + '<endEvent id="E" name="Готово"/>'
                   + _flow("f3", "A", "S"))
        result = scorer.evaluate(xml)
        assert status(result, "flow_ends_legal") == FAILED
        assert elements(result, "flow_ends_legal") == ["f3"]

    def test_flow_out_of_an_end_event_is_charged(self):
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Собрать заказ"/>'
                   + _flow("f2", "A", "E") + '<endEvent id="E" name="Готово"/>'
                   + _flow("f3", "E", "A"))
        assert status(scorer.evaluate(xml), "flow_ends_legal") == FAILED

    def test_branch_out_of_a_boundary_event_is_legal(self):
        """Из граничного события ветка обработки идёт — это норма: хозяин
        запускает её по `attachedToRef`, а не потоком. В само же событие поток
        входить не должен (иначе оно выглядело бы продолжением маршрута)."""
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Собрать заказ"/>'
                   + _flow("f2", "A", "E")
                   + '<boundaryEvent id="B" name="Просрочка" attachedToRef="A">'
                     '<timerEventDefinition/></boundaryEvent>'
                   + _flow("f3", "B", "H")
                   + '<userTask id="H" name="Догнать срок"/>'
                   + _flow("f4", "H", "E") + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(xml), "flow_ends_legal") == PASSED
        # та же дуга, но входящая в событие, — нарушение
        broken = _doc('<startEvent id="S" name="Начало"/>'
                      + _flow("f1", "S", "A")
                      + '<userTask id="A" name="Собрать заказ"/>'
                      + _flow("f2", "A", "B")
                      + '<boundaryEvent id="B" name="Просрочка" attachedToRef="A">'
                        '<timerEventDefinition/></boundaryEvent>'
                      + _flow("f3", "B", "E")
                      + '<endEvent id="E" name="Готово"/>')
        assert status(scorer.evaluate(broken), "flow_ends_legal") == FAILED

    def test_scheme_without_flows_is_not_applicable(self):
        assert status(scorer.evaluate(_doc('<task id="A" name="Только шаг"/>')),
                      "flow_ends_legal") == NOT_APPLICABLE

    def test_illegal_end_explains_who_writes_the_replacement(self):
        """Рецепта здесь намеренно нет: `disconnect` без замены создаёт тупик
        (перепись показала это на `sequence_flows`), а чем замещать конец —
        знает автор процесса. Молчание о причине читалось бы моделью как
        «придумай id»."""
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Собрать заказ"/>'
                   + _flow("f2", "A", "E") + '<endEvent id="E" name="Готово"/>'
                   + _flow("f3", "A", "S"))
        text = scorer.evaluate(xml)["recommendations_by_rule"]["flow_ends_legal"]
        assert "'f3'" in text and "startEvent" in text
        assert "disconnect(flow=" not in text
        assert "называет автор процесса" in text


def _timer_event_doc(definition: str, event_id: str = "W") -> str:
    """Маршрут с одним промежуточным событием, у которого определение задано
    дословно (`definition`)."""
    return _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", event_id)
                + f'<intermediateCatchEvent id="{event_id}" '
                  f'name="Ожидание срока">{definition}</intermediateCatchEvent>'
                + _flow("f2", event_id, "E") + '<endEvent id="E" name="Готово"/>')


class TestMessageFlowEndsAreNotGateways:
    """Развилка не бывает концом потока-сообщения.

    Класс принёс не корпус — в 367 рукописных схемах ни один из 625 обменов не
    подходит концом к шлюзу, — а собственный совет линейки: `cross_pool_flow`
    переводил межпуловую дугу в `connect(source='<шлюз>', …,
    flow_type='message')`, то есть контур улучшения заводил нотационный дефект,
    которого не допускает ни один живой модельер (2 пакета из 5 на корпусе).
    Перепись при этом считала ход чистым снятием нарушения: `message_flow_ends`
    проверял, что конец существует, а не чем он является.
    """

    def _pool_with_gateway(self, extra_second_leg: str = "") -> str:
        """Пул 1: старт → шаг → развилка → финиш; пул 2 — обычный."""
        body1 = ('<startEvent id="S1" name="Начало"/>' + _flow("f1", "S1", "A1")
                 + '<userTask id="A1" name="Собрать"/>' + _flow("f2", "A1", "G1")
                 + '<exclusiveGateway id="G1" name="Куда дальше"/>'
                 + _flow("f3", "G1", "E1")
                 + '<endEvent id="E1" name="Конец"/>' + extra_second_leg)
        return pools_doc([body1, pool_body(2)])

    def test_message_flow_from_a_gateway_is_charged(self):
        xml = self._pool_with_gateway()
        xml = xml.replace('</collaboration>',
                          '<messageFlow id="m1" sourceRef="G1" targetRef="A2"/>'
                          '</collaboration>')
        result = scorer.evaluate(xml)
        assert status(result, "message_flow_ends") == FAILED
        assert "m1" in elements(result, "message_flow_ends")

    def test_message_flow_into_a_gateway_is_charged(self):
        xml = self._pool_with_gateway()
        xml = xml.replace('</collaboration>',
                          '<messageFlow id="m1" sourceRef="A2" targetRef="G1"/>'
                          '</collaboration>')
        assert status(scorer.evaluate(xml), "message_flow_ends") == FAILED

    def test_message_flow_between_steps_and_pools_is_not_charged(self):
        """Шаг→шаг, шаг→пул и событие→шаг законны: правило заряжает шлюз, а не
        «всё, что не задача»."""
        for source, target in (("A1", "A2"), ("A1", "P2"), ("S1", "A2")):
            xml = self._pool_with_gateway().replace(
                '</collaboration>',
                f'<messageFlow id="m1" sourceRef="{source}" targetRef="{target}"/>'
                '</collaboration>')
            assert status(scorer.evaluate(xml), "message_flow_ends") == PASSED, (source, target)

    def test_advice_names_a_step_not_the_gateway(self):
        """Совет про межпуловую дугу, у которой конец — развилка, обязан назвать
        отправителем шаг, ведущий в эту развилку: иначе правка сама становится
        дефектом, который линейка заряжает следом."""
        body1 = ('<startEvent id="S1" name="Начало"/>' + _flow("f1", "S1", "A1")
                 + '<userTask id="A1" name="Собрать"/>' + _flow("f2", "A1", "G1")
                 + '<exclusiveGateway id="G1" name="Куда дальше"/>'
                 + _flow("xF", "G1", "A2") + _flow("f3", "G1", "E1")
                 + '<endEvent id="E1" name="Конец"/>')
        text = scorer.evaluate(pools_doc([body1, pool_body(2)]))[
            "recommendations_by_rule"]["cross_pool_flow"]
        assert "connect(source='A1', target='A2', flow_type='message')" in text
        assert "source='G1'" not in text
        assert "концом обмена не бывает" in text

    def test_advice_without_a_single_sender_names_the_choice(self):
        """Если в развилку ведёт не один шаг, операнда нет: совет обязан сказать
        это словами, а не печатать выдуманный id."""
        body1 = ('<startEvent id="S1" name="Начало"/>' + _flow("f1", "S1", "A1")
                 + '<userTask id="A1" name="Собрать"/>' + _flow("f2", "A1", "G1")
                 + '<userTask id="B1" name="Проверить"/>' + _flow("f2b", "B1", "G1")
                 + '<exclusiveGateway id="G1" name="Куда дальше"/>'
                 + _flow("xF", "G1", "A2") + _flow("f3", "G1", "E1")
                 + '<endEvent id="E1" name="Конец"/>'
                 + _flow("f4", "S1", "B1") + _flow("f5", "B1", "E1"))
        text = scorer.evaluate(pools_doc([body1, pool_body(2)]))[
            "recommendations_by_rule"]["cross_pool_flow"]
        assert "source='G1'" not in text
        assert "называет автор процесса" in text


class TestTimerWithoutSchedule:
    """Таймер без хронометража — мёртвый срок.

    `<timerEventDefinition/>` (и то же с пустым значением) исполнитель не
    заведёт никогда: ветка «срок вышел» выглядит готовой, по ней рисуются
    стрелки и эскалация, а наступает она только когда пользователь сам заметит
    просрочку. Найдено переписью корпуса: 108 схем из 367 держат минимум один
    такой таймер, и ни скоринг, ни оракул харнесса на хронометраж раньше не
    смотрели (правила про события читали наличие `*EventDefinition`, а не его
    значение). Правка при этом исполнимая: `add_event_definition` с `duration`
    подставляет срок тому же узлу, не подменяя тип события."""

    def test_timer_without_a_schedule_is_charged(self):
        result = scorer.evaluate(_timer_event_doc("<timerEventDefinition/>"))
        assert status(result, "timer_without_schedule") == FAILED
        assert elements(result, "timer_without_schedule") == ["W"]

    @pytest.mark.parametrize("definition", [
        "<timerEventDefinition><timeDuration>PT1H</timeDuration>"
        "</timerEventDefinition>",
        "<timerEventDefinition><timeCycle>R3/PT10M</timeCycle>"
        "</timerEventDefinition>",
        "<timerEventDefinition><timeDate>2026-01-01T09:00:00Z</timeDate>"
        "</timerEventDefinition>",
    ])
    def test_any_real_schedule_makes_the_timer_live(self, definition):
        """`timeDate` — тоже расписание («в 9 утра первого января»), хотя
        аплайер такого значения не создаёт: требовать только интервал значило бы
        ругать корректные схемы пользователя."""
        assert status(scorer.evaluate(_timer_event_doc(definition)),
                      "timer_without_schedule") == PASSED

    def test_a_blank_value_is_not_a_schedule(self):
        xml = _timer_event_doc("<timerEventDefinition><timeDuration> </timeDuration>"
                               "</timerEventDefinition>")
        assert status(scorer.evaluate(xml), "timer_without_schedule") == FAILED

    def test_scheme_without_timers_is_not_applicable(self):
        assert status(scorer.evaluate(simple_xml()),
                      "timer_without_schedule") == NOT_APPLICABLE

    def test_a_boundary_timer_is_charged_too(self):
        result = scorer.evaluate(
            _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                 + '<userTask id="A" name="Собрать заказ"/>' + _flow("f2", "A", "E")
                 + '<endEvent id="E" name="Готово"/>'
                 + '<boundaryEvent id="BE" name="Просрочка" attachedToRef="A">'
                   '<timerEventDefinition/></boundaryEvent>'))
        assert status(result, "timer_without_schedule") == FAILED
        assert elements(result, "timer_without_schedule") == ["BE"]

    def test_advice_names_the_op_that_fills_the_schedule(self):
        text = scorer.evaluate(_timer_event_doc("<timerEventDefinition/>"))[
            "recommendations_by_rule"]["timer_without_schedule"]
        assert ("add_event_definition(id='W', event_definition='timer', duration="
                in text), text


class TestAdviceIsExecutable:
    """Бизнес-подсказка обязана быть исполнимой по тем id, которые она называет.

    Статическая проверка «`action` ∈ `OP_SPEC`» (см. `TestBusinessRules`)
    гарантирует только то, что операция существует. Моделью она не выполнима,
    пока в тексте нет операндов: потока, который резать, шага, который должен
    принять ответ, дорожки-приёмника и её работ. Совет без операнда читается
    моделью как «придумай id», аплайер отвечает отказом, а `improve/op_acceptance`
    записывает это как качество модели, хотя виноват текст правила.

    Каждый тест собирает операцию ИСКЛЮЧИТЕЛЬНО из id, названных в подсказке,
    применяет её продуктовым гарантом и проверяет два факта: нарушение снято и
    ни одно прежде проходившее правило не сломано.
    """

    def _named_ids(self, text: str, xml: str) -> list:
        """Id схемы, которые реально встречаются в тексте подсказки."""
        return [i for i in re.findall(r'id="([^"]+)"', xml) if i and i in text]

    def _ids_of(self, xml: str, tag: str) -> list:
        return re.findall(rf'<{tag} id="([^"]+)"', xml)

    def _repair(self, xml: str, operations: list) -> str:
        from core import bpmn_edits

        after, _report = bpmn_edits.apply_and_guarantee(xml, operations)
        return after

    def _assert_fixed(self, xml: str, rule: str, operations: list) -> None:
        from core.llm_improve import _rules_regressed

        assert status(scorer.evaluate(xml), rule) == FAILED, "кейс больше не битый"
        after = self._repair(xml, operations)
        assert status(scorer.evaluate(after), rule) != FAILED, (
            f"{rule}: подсказка не снимает нарушение: "
            f"{scorer.evaluate(after)['details_meta'][rule]}")
        assert not _rules_regressed(xml, after), (
            f"{rule}: правка сломала другое правило: "
            f"{_rules_regressed(xml, after)}")

    def test_rework_loop_names_the_flow_to_guard(self):
        """`add_condition` принимает id потока: без «на дуге f4» совет
        «назови критерий выхода» нечем заполнить."""
        xml = open_rework_xml()
        text = scorer.evaluate(xml)["recommendations_by_rule"]["rework_loop"]
        arcs = set(self._ids_of(xml, "sequenceFlow"))
        flows = [i for i in self._named_ids(text, xml) if i in arcs]
        assert flows, f"в подсказке нет id дуги: {text}"
        self._assert_fixed(xml, "rework_loop",
                           [{"op": "add_condition", "flow": flows[0],
                             "condition": "данные уточнены"}])

    def test_event_types_advice_signs_the_type_on_the_existing_event(self):
        """Пустому кружку тип подписывают на месте. Раньше применимого рецепта
        у правила не было: `add_event` создаёт НОВОЕ событие, а узел с его
        потоками остаётся без типа, то есть нарушение не снималось ни одним
        легальным пакетом.

        Пакет собирается ИЗ ТЕКСТА подсказки, а не из удобных тесту значений:
        операнд-перечисление (`event_definition='message|error|signal'`)
        аплайер отвергает как неизвестное определение, и тест, подставивший
        удобное `message` сам, эту нестыковку не показывал бы."""
        xml = _doc('<startEvent id="S" name="Начало"/>'
                   '<intermediateCatchEvent id="W" name="Ожидание ответа"/>'
                   + _flow("f1", "S", "W") + _flow("f2", "W", "E")
                   + '<endEvent id="E" name="Готово"/>')
        text = scorer.evaluate(xml)["recommendations_by_rule"]["event_types"]
        assert "add_event_definition(id='W'" in text, text
        from eval.advice import parse_recipes

        ops = parse_recipes(text)
        assert ops, f"в подсказке не осталось вызова операции: {text}"
        self._assert_fixed(xml, "event_types", ops)

    def test_an_inert_timer_gets_its_schedule_verbatim(self):
        """Пакет собирается из текста подсказки, а не из удобных тесту значений:
        до правки `add_event_definition` отказывал пустому таймеру по
        «определение уже есть», и нарушение `timer_without_schedule` было
        жалобой без работы."""
        xml = _timer_event_doc("<timerEventDefinition/>")
        text = scorer.evaluate(xml)["recommendations_by_rule"]["timer_without_schedule"]
        from eval.advice import parse_recipes

        ops = parse_recipes(text)
        assert ops, f"в подсказке нет вызова операции: {text}"
        self._assert_fixed(xml, "timer_without_schedule", ops)

    def test_wait_without_sla_names_the_operands_of_the_deadline_fork(self):
        """Граничный таймер на catch-событие аплайер не принимает («не является
        задачей»), поэтому подсказка обязана называть предка ожидания и его
        продолжение: только из них собирается пакет «развилка ответ или срок»,
        и только он снимает нарушение. Прежняя редакция советовала
        `add_boundary_event` — правку, которую невозможно применить."""
        xml = sla_xml()
        result = scorer.evaluate(xml)
        text = result["recommendations_by_rule"]["wait_without_sla"]
        offenders = set(result["details_meta"]["wait_without_sla"]["elements"])
        named = set(self._named_ids(text, xml))
        assert offenders <= named, f"нарушение не названо id: {text}"
        assert {"W", "E"} <= named, f"операндов развилки нет в подсказке: {text}"
        self._assert_fixed(xml, "wait_without_sla", [
            {"op": "add_event", "id": "new_t", "name": "Срок ответа вышел",
             "event_type": "timer", "duration": "PT2H"},
            {"op": "add_gateway", "id": "new_g", "name": "Ответ или срок?",
             "gateway_type": "parallel", "after": "W"},
            {"op": "connect", "source": "new_g", "target": "new_t"},
            {"op": "connect", "source": "new_t", "target": "E"},
        ])

    def test_receive_task_advice_names_its_own_boundary_timer(self):
        """`receiveTask` — активность, и граничный таймер на неё ложится:
        подсказка обязана назвать и хозяина, и шаг обработки."""
        xml = _doc(
            '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "R")
            + '<receiveTask id="R" name="Зафиксировать подпись"/>'
            + _flow("f2", "R", "E") + '<endEvent id="E" name="Готово"/>')
        text = scorer.evaluate(xml)["recommendations_by_rule"]["wait_without_sla"]
        assert "add_boundary_event" in text and "attached_to='R'" in text, text
        self._assert_fixed(xml, "wait_without_sla",
                           [{"op": "add_boundary_event", "id": "new_bt",
                             "attached_to": "R", "event_type": "timer",
                             "name": "Срок подписи", "duration": "PT2H",
                             "to": "E"}])

    def test_handoff_pingpong_names_the_step_that_should_receive_the_answer(self):
        """Совет говорит «ответ — следующему шагу», значит обязан назвать и
        следующий шаг, и дугу ответа: без них `connect` нечем заполнить, а
        остаётся только `disconnect`, который теряет ответ вовсе."""
        xml = pingpong_xml()
        text = scorer.evaluate(xml)["recommendations_by_rule"]["handoff_pingpong"]
        named = set(self._named_ids(text, xml))
        assert {"M1", "M2"} <= named, f"потоков обмена не названо: {text}"
        # E1 — следующий шаг того пула, который ждёт ответ: только по нему
        # `connect` заполняется без выдумывания id.
        assert "E1" in named, f"шага-приёмника ответа нет в подсказке: {text}"
        assert "flow='M2'" in text and "flow_type='message'" in text, (
            f"операндов перевода дуги нет: {text}")
        self._assert_fixed(xml, "handoff_pingpong", [
            {"op": "disconnect", "flow": "M2"},
            {"op": "connect", "source": "A2", "target": "E1",
             "flow_type": "message"},
        ])

    def test_handoff_pingpong_advice_survives_pool_endpoints(self):
        """Обмен концом на участник целиком: следующего шага у пула нет, а
        `connect` узлом его не считает («источник или цель не найдены»), поэтому
        подсказка обязана просить конкретный шаг, а не советовать перевеску,
        которую аплайер отвергнет."""
        xml = pingpong_xml(pool_endpoints=True)
        text = scorer.evaluate(xml)["recommendations_by_rule"]["handoff_pingpong"]
        assert "участника 'P1' целиком" in text, text
        assert "disconnect(flow=" not in text, text

    def test_approval_chain_names_a_repair_it_can_apply(self):
        """Разнесение по дорожкам требует вторую дорожку и пул, а шлюз — две
        соседние задачи. В тексте обязаны быть те id, которыми правка
        заполняется без выдумывания."""
        xml = approval_lane_xml()
        text = scorer.evaluate(xml)["recommendations_by_rule"]["approval_chain"]
        named = self._named_ids(text, xml)
        assert len(named) >= 2, f"одного id для правки мало: {text}"
        self._assert_fixed(xml, "approval_chain",
                           [{"op": "add_gateway", "id": "new_g",
                             "name": "Нужен второй согласующий?",
                             "gateway_type": "exclusive", "after": "U2",
                             "to": "U3"}])

    def test_lane_overload_names_the_work_and_the_receiving_lane(self):
        """«Перенеси в другую дорожку» без перечня работ перегруженной дорожки и
        без дорожки-приёмника — совет, который модель не может выразить
        операцией: `move_to_lane` требует и id шага, и id/имя другой дорожки."""
        xml = lanes_split_xml([5, 2, 1])
        text = scorer.evaluate(xml)["recommendations_by_rule"]["lane_overload"]
        named = set(self._named_ids(text, xml))
        assert named & {"L2", "L3"}, f"дорожка-приёмник не названа: {text}"
        assert named & {"A1", "A2", "A3", "A4", "A5"}, f"работы не названы: {text}"
        self._assert_fixed(xml, "lane_overload",
                           [{"op": "move_to_lane", "id": "A4", "lane": "L2"},
                            {"op": "move_to_lane", "id": "A5", "lane": "L3"}])

    def test_a_flow_without_a_target_is_charged_before_any_repair(self):
        """Дуга, у которой конца нет, раньше засчитывалась как исходящий поток:
        шаг выглядел соединённым, `sequence_flows` и `no_isolated` молчали, а
        `validate_and_repair` вырезал её молча — тупик появлялся уже после
        правки, и перепись подсказок записывала его в регрессии совета
        (`no_isolated`, `sequence_flows`), а не в датасет. Замер корпуса: 107
        таких дуг в 33 схемах из 367, и в 15 из них `sequence_flows` проходил.
        Теперь дефект назван до ремонта, а половина рецепта (`disconnect`) не
        создаёт ни одного нового нарушения."""
        from core.llm_improve import _rules_regressed

        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Собрать документы"/>'
                   '<sequenceFlow id="f2" sourceRef="A"/>'
                   + '<endEvent id="E" name="Готово"/>')
        meta = scorer.evaluate(xml)["details_meta"]
        assert status(scorer.evaluate(xml), "sequence_flows") == FAILED
        assert "f2" in meta["sequence_flows"]["elements"], meta["sequence_flows"]
        assert "A" in meta["no_isolated"]["elements"], meta["no_isolated"]
        text = scorer.evaluate(xml)["recommendations_by_rule"]["sequence_flows"]
        # Названы оба структурных операнда: дуга и её источник.
        assert "'f2'" in text and "'A'" in text, text
        # Цель знает только автор процесса: подсказка обязана сказать это, а не
        # подставлять выдуманный id в `connect`.
        assert "называется только по смыслу" in text, text
        # Половинчатого `disconnect` здесь нет намеренно: перепись исполнимости
        # показала, что удалённая без замены дуга роняет `no_isolated` и
        # `sequence_flows` на 3 схемах из 6 — то есть совет сам создавал тупик.
        assert "disconnect(flow=" not in text, text
        after = self._repair(xml, [{"op": "connect", "source": "A", "target": "E"},
                                   {"op": "disconnect", "flow": "f2"}])
        assert status(scorer.evaluate(after), "sequence_flows") != FAILED
        assert not _rules_regressed(xml, after), _rules_regressed(xml, after)

    def test_a_flow_across_pools_is_repaired_as_a_message(self):
        """Рецепт межпуловой дуги исполним целиком: обмениваются те же два
        шага, новых id правка не выдумывает и ни одно проходившее правило не
        роняет — в отличие от `sequence_flows`, где `disconnect` без замены
        создавал тупик."""
        from core.llm_improve import _rules_regressed

        both_keep_route = pools_doc(
            [pool_body(1) + _flow("xF", "A1", "A2"),
             '<startEvent id="S2" name="Начало"/>' + _flow("g1", "S2", "A2a")
             + '<userTask id="A2a" name="Проверить"/>' + _flow("g2", "A2a", "A2")
             + '<userTask id="A2" name="Привезти"/>' + _flow("g3", "A2", "E2")
             + '<endEvent id="E2" name="Конец"/>'])
        xml = self._crossing_with_message(both_keep_route)
        assert status(scorer.evaluate(xml), "cross_pool_flow") == FAILED
        text = scorer.evaluate(xml)["recommendations_by_rule"]["cross_pool_flow"]
        assert "disconnect(flow=" in text and "flow_type='message'" in text, text
        after = self._repair(xml, [{"op": "disconnect", "flow": "xF"},
                                   {"op": "connect", "source": "A1", "target": "A2",
                                    "flow_type": "message"}])
        assert status(scorer.evaluate(after), "cross_pool_flow") != FAILED
        assert not _rules_regressed(xml, after), _rules_regressed(xml, after)

    def _crossing_with_message(self, xml: str) -> str:
        """Добавляет обмен между пулами, чтобы `participant_interacts` не
        считался нарушением до и после: сравнивать регрессии нужно на схеме,
        где молчит всё, кроме проверяемого правила. Взята именно `A2a`, а не
        `A2`: обмен A2 → A1 вместе с новой ногой A1 → A2 сложился бы в
        `handoff_pingpong` (та же работа в обе стороны), и тест ловил бы не
        регрессию рецепта, а конструкцию фикстуры."""
        return xml.replace('</collaboration>',
                           '<messageFlow id="m1" sourceRef="A2a" targetRef="A1"/>'
                           '</collaboration>')


class TestTaskTypesAreRepairable:
    """Тип шага — правка того же узла, а не удаление с пересозданием.

    `task_types` — самый массовый класс корпуса (200 схем из 367, 1807 родовых
    `task`), и до появления `set_task_type` он был неисправим: модель обязана была
    `delete` + `add_task` с новым id, теряя дорожку, имя и ссылку в диаграмме,
    а `_rules_regressed` заряжал ещё и оборванный маршрут. Теперь совет называет
    операцию, а там, где тип читается из коллаборации, и его операнд.
    """

    def _doc(self, message_flows: str) -> str:
        return pools_doc([pool_body(1), pool_body(2)], message_flows=message_flows)

    def test_step_talking_to_another_pool_gets_a_named_type(self):
        text = scorer.evaluate(self._doc(
            '<messageFlow id="m1" sourceRef="A1" targetRef="A2"/>'))[
            "recommendations_by_rule"]["task_types"]
        assert "set_task_type(id='A1', task_type='sendTask')" in text
        assert "set_task_type(id='A2', task_type='receiveTask')" in text

    def test_named_type_survives_the_applier_verbatim(self):
        """Рецепт из текста применяется дословно и снимает нарушение."""
        from core.bpmn_edits import apply_and_guarantee
        from eval.advice import parse_recipes

        xml = self._doc('<messageFlow id="m1" sourceRef="A1" targetRef="A2"/>')
        text = scorer.evaluate(xml)["recommendations_by_rule"]["task_types"]
        package = [op for op in parse_recipes(text) if op.get("op") == "set_task_type"]
        assert package, text
        after, report = apply_and_guarantee(xml, package)
        assert report["applied"] and not report["reverted"]
        assert status(scorer.evaluate(after), "task_types") == PASSED

    def test_without_message_evidence_the_type_is_not_invented(self):
        """Тип без подсказки из структуры называет автор: печатать
        `set_task_type(id=…, task_type=…)` с выдуманным значением линейка не
        вправе — модель копирует его дословно."""
        text = scorer.evaluate(self._doc("")) ["recommendations_by_rule"]["task_types"]
        assert "set_task_type(" not in text
        assert "называет автор процесса" in text
        assert "→ чинится: set_task_type" in text

    def test_user_task_to_service_task_is_charged_again(self):
        """Смена типа не «чинит» схему молча: если все шаги стали одного рода,
        нарушение остаётся, и правило обязано это видеть."""
        xml = pools_doc([pool_body(1).replace('userTask', 'serviceTask'),
                         pool_body(2).replace('userTask', 'serviceTask')])
        assert status(scorer.evaluate(xml), "task_types") == FAILED

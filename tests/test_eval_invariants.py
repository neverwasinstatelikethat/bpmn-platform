"""Бизнес-слой оракула: независимые проверки узких мест процесса и сверка двух
слоёв.

Оракул (`eval/invariants.py`) нужен ровно затем, чтобы правка
`core/bpmn_scoring.py` не двигала одновременно и линейку, и измеряемое значение.
Здесь закреплены четыре вещи:

1. Каждая бизнес-проверка оракула отличает битую схему от исправной на схемах,
   собранных из локальных XML-конструкторов, и не считает «нечего проверять»
   прохождением (`applicable=False`).
2. Обе точки входа — структура-словарь и XML — дают по этим свойствам один и тот
   же вердикт.
3. Оракул не импортирует скоринг: ни статически (разбор AST самого модуля), ни
   в рантайме (модуль компилируется заново с «ядом» в `sys.modules`).
4. `business_agreement` по фикстурам `eval/fixtures/` даёт расхождения ровно те,
   что подписаны в docstrings проверок, и ни одного неожиданного. Расхождение —
   данные человеку, поэтому здесь оно перечислено поимённо, а не сведено к
   порогу.
"""

import ast
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from core.bpmn_scoring import BPMNScorer
from eval import invariants
from eval.invariants import (AGREE, ALL_CHECKS, CORE_INVARIANTS, NO_DATA,
                             NOT_COMPARABLE, ORACLE_STRICTER,
                             business_agreement, business_disagreements,
                             check_structure, check_xml)
from eval.scenarios import SCENARIOS

FIXTURES = Path(invariants.__file__).resolve().parent / "fixtures"
NEW_INVARIANTS = ("no_blind_rework", "pools_not_pingpong", "no_overloaded_lane",
                  "waits_have_sla",
                  # Два класса, добавленных поздними проходами: согласование без
                  # развилки и безусловный цикл. Свидетели по набору — в
                  # `EXPECTED_NEW_FAILURES`; без них инвариант был бы незаметен
                  # так же, как `approval_chain` был незаметен до своего зеркала.
                  "signoffs_need_a_gate", "loops_have_a_guard")

scorer = BPMNScorer()


def status_of(xml: str, rule: str) -> str:
    """Статус правила скоринга по схеме — тот же, что видит панель качества."""
    return scorer.evaluate(xml)["details_meta"][rule]["status"]


HEADER = ('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" '
          'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">')


def _doc(inner: str, collaboration: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}{collaboration}'
            f'<process id="Proc_1" name="Пул" isExecutable="true">{inner}'
            f'</process></definitions>')


def _flow(fid, src, dst, condition=None, label=None) -> str:
    """Поток: `condition` — conditionExpression, `label` — подпись потока (`name`).
    Оракул различает их (подпись ноги читается только в бизнес-проверках),
    `gateway_conditions_or_default` — нет."""
    cond = f"<conditionExpression>{condition}</conditionExpression>" if condition else ""
    name = f' name="{label}"' if label else ""
    return (f'<sequenceFlow id="{fid}" sourceRef="{src}" targetRef="{dst}"'
            f'{name}>{cond}</sequenceFlow>')


def _lane(lane_id, name, refs):
    return (f'<lane id="{lane_id}" name="{name}">'
            + "".join(f'<flowNodeRef>{r}</flowNodeRef>' for r in refs) + '</lane>')


def rework_xml(loop_condition=None, exit_condition=None, back_condition=None,
               exit_default=False, loop_label=None, exit_label=None) -> str:
    """A (согласование) → G → B (доработка) → снова A; вторая нога G — наружу, в E.

    Ноги G ищутся парой «в петлю / из петли»; `back_condition` — условие на
    самой дуге возврата. `exit_default` объявляет выходную ногу выходом по
    умолчанию шлюза, `*_label` — подпись потока вместо формального условия."""
    default = ' default="f_exit"' if exit_default else ""
    return _doc(
        '<startEvent id="S" name="Задача поступила"/>'
        + _flow("f_in", "S", "A")
        + '<userTask id="A" name="Согласовать"/>' + _flow("f_to_g", "A", "G")
        + f'<exclusiveGateway id="G" name="Есть замечания?"{default}/>'
        + _flow("f_loop", "G", "B", loop_condition, loop_label)
        + '<manualTask id="B" name="Доработать"/>'
        + _flow("f_back", "B", "A", back_condition)
        + _flow("f_exit", "G", "E", exit_condition, exit_label)
        + '<endEvent id="E" name="Согласовано"/>')


def loop_without_exit_xml() -> str:
    """S → A → B → A: работа ходит по кругу, ветки наружу нет вовсе."""
    return _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
        + '<userTask id="A" name="Согласовать"/>' + _flow("f2", "A", "B")
        + '<manualTask id="B" name="Доработать"/>' + _flow("f3", "B", "A")
        + '<endEvent id="E" name="Готово"/>')


def pools_doc(bodies, message_flows: str = "") -> str:
    """Коллаборация из N пулов: участник P{i} ссылается на процесс Proc_{i}."""
    participants = "".join(
        f'<participant id="P{i}" name="Участник {i}" processRef="Proc_{i}"/>'
        for i in range(1, len(bodies) + 1))
    processes = "".join(
        f'<process id="Proc_{i}" name="Пул {i}">{body}</process>'
        for i, body in enumerate(bodies, 1))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
            f'<collaboration id="C1">{participants}{message_flows}</collaboration>'
            f'{processes}</definitions>')


def pool_body(i: int, head: str = "A") -> str:
    return (f'<startEvent id="S{i}" name="Старт {i}"/>'
            + _flow(f"sf{i}", f"S{i}", f"{head}{i}")
            + f'<userTask id="{head}{i}" name="Шаг {i}"/>'
            + _flow(f"ef{i}", f"{head}{i}", f"E{i}")
            + f'<endEvent id="E{i}" name="Финиш {i}"/>')


def pingpong_xml(same_pair=True) -> str:
    """Два пула и двусторонний обмен сообщениями.

    `same_pair=True` — одна и та же пара шагов ходит туда-сюда (это и есть
    пинг-понг); иначе документы разъезжаются по разным шагам, что остаётся
    нормальной коллаборацией."""
    back = ('<messageFlow id="M2" sourceRef="B1" targetRef="A1"/>' if same_pair
            else '<messageFlow id="M2" sourceRef="B2" targetRef="A2"/>')
    body1 = ('<startEvent id="S1" name="Старт 1"/>' + _flow("h1", "S1", "A1")
             + '<userTask id="A1" name="Запросить"/>' + _flow("h2", "A1", "A2")
             + '<userTask id="A2" name="Закрыть заявку"/>' + _flow("h3", "A2", "E1")
             + '<endEvent id="E1" name="Финиш 1"/>')
    body2 = ('<startEvent id="S2" name="Старт 2"/>' + _flow("g0", "S2", "B1")
             + '<userTask id="B1" name="Проверить"/>' + _flow("g1", "B1", "B2")
             + '<userTask id="B2" name="Отгрузить"/>' + _flow("g2", "B2", "E2")
             + '<endEvent id="E2" name="Финиш 2"/>')
    return pools_doc([body1, body2],
                     '<messageFlow id="M1" sourceRef="A1" targetRef="B1"/>' + back)


def single_pool_xml() -> str:
    return pools_doc([pool_body(1), pool_body(2)])


def laned_doc(split) -> str:
    """Один процесс с дорожками, в которых по числу работ из `split`.

    Узлы — `userTask`, разложены через `flowNodeRef`, как их рисует bpmn-js."""
    ids = [f"A{i}" for i in range(1, sum(split) + 1)]
    lanes, taken = "", 0
    for n, count in enumerate(split, 1):
        refs = ids[taken:taken + count]
        taken += count
        lanes += _lane(f"L{n}", f"Роль {n}", refs)
    body = ['<laneSet id="LS">%s</laneSet>' % lanes,
            '<startEvent id="S" name="Начало"/>']
    if ids:
        body.append(_flow("f0", "S", ids[0]))
        for i, node in enumerate(ids):
            body.append(f'<userTask id="{node}" name="Шаг {i + 1}"/>')
            body.append(_flow(f"f{i}", node,
                              ids[i + 1] if i + 1 < len(ids) else "E"))
    body.append('<endEvent id="E" name="Готово"/>')
    return _doc("".join(body))


def wait_xml(on_wait=False, parallel=False, after=False, elsewhere=False,
             wait_kind="intermediateCatchEvent", definition="") -> str:
    """S → W (ожидание) → A → E плюс таймер в одном из четырёх положений.

    `on_wait` — граничный таймер на самом ожидании (легальный SLA), `parallel` —
    ветка «срок вышел», уходящая от старта и не ждущая завершения ожидания
    (тоже легальный SLA), `after` — таймер ниже по маршруту (ожидание он не
    ограничивает: токен доходит до него только после W), `elsewhere` — таймер в
    отрыве от маршрута ожидания (скорингу этого хватает, оракулу — нет)."""
    inner = ('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "W")
             + f'<{wait_kind} id="W" name="Ожидание ответа">{definition}</{wait_kind}>'
             + _flow("f2", "W", "A")
             + '<userTask id="A" name="Обработать ответ"/>' + _flow("f3", "A", "E")
             + '<endEvent id="E" name="Готово"/>')
    if on_wait:
        inner += ('<boundaryEvent id="T" name="Срок вышел" attachedToRef="W">'
                  '<timerEventDefinition/></boundaryEvent>'
                  + _flow("t1", "T", "ESC")
                  + '<manualTask id="ESC" name="Эскалация руководителю"/>'
                  + _flow("t2", "ESC", "E"))
    if parallel:
        inner += ('<intermediateCatchEvent id="T" name="Срок вышел">'
                  '<timerEventDefinition/></intermediateCatchEvent>'
                  + _flow("p1", "S", "T") + _flow("p2", "T", "E2")
                  + '<endEvent id="E2" name="Отказ по сроку"/>')
    if after:
        inner += ('<intermediateCatchEvent id="T" name="Отсрочка-followup">'
                  '<timerEventDefinition/></intermediateCatchEvent>'
                  + _flow("a1", "A", "T") + _flow("a2", "T", "A2")
                  + '<userTask id="A2" name="Контроль спустя срок"/>'
                  + _flow("a3", "A2", "E"))
    if elsewhere:
        inner += ('<startEvent id="S2" name="Таймер ниоткуда"/>'
                  + '<intermediateCatchEvent id="T" name="Срок где-то рядом">'
                    '<timerEventDefinition/></intermediateCatchEvent>'
                  + _flow("e1", "S2", "T") + _flow("e2", "T", "E3")
                  + '<endEvent id="E3" name="Конец соседней ветки"/>')
    return _doc(inner)


def blind_rework_plan() -> dict:
    """Та же слепая петля, что и `rework_xml()` без ног, — но планом модели."""
    return {
        "participants": ["ВкусВилл"],
        "lanes": [{"id": "L_a", "name": "Согласующий", "participant": "ВкусВилл"},
                  {"id": "L_b", "name": "Исполнитель", "participant": "ВкусВилл"}],
        "elements": [
            {"id": "S", "kind": "startEvent", "name": "Задача поступила",
             "participant": "ВкусВилл", "lane": "L_a"},
            {"id": "A", "kind": "userTask", "name": "Согласовать",
             "participant": "ВкусВилл", "lane": "L_a"},
            {"id": "G", "kind": "exclusiveGateway", "name": "Есть замечания?",
             "participant": "ВкусВилл", "lane": "L_a"},
            {"id": "B", "kind": "manualTask", "name": "Доработать",
             "participant": "ВкусВилл", "lane": "L_b"},
            {"id": "E", "kind": "endEvent", "name": "Согласовано",
             "participant": "ВкусВилл", "lane": "L_a"},
        ],
        "flows": [
            {"id": "f_in", "source": "S", "target": "A"},
            {"id": "f_to_g", "source": "A", "target": "G"},
            {"id": "f_loop", "source": "G", "target": "B"},
            {"id": "f_back", "source": "B", "target": "A"},
            {"id": "f_exit", "source": "G", "target": "E"},
        ],
    }


def pingpong_plan() -> dict:
    return {
        "participants": ["Склад", "Перевозчик"],
        "lanes": [{"id": "L1", "name": "Логистика", "participant": "Склад"},
                  {"id": "L2", "name": "Водитель", "participant": "Перевозчик"}],
        "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Заявка",
             "participant": "Склад", "lane": "L1"},
            {"id": "A1", "kind": "userTask", "name": "Заказать транспорт",
             "participant": "Склад", "lane": "L1"},
            {"id": "E1", "kind": "endEvent", "name": "Готово",
             "participant": "Склад", "lane": "L1"},
            {"id": "S2", "kind": "startEvent", "name": "Заявка принята",
             "participant": "Перевозчик", "lane": "L2"},
            {"id": "B1", "kind": "userTask", "name": "Подать машину",
             "participant": "Перевозчик", "lane": "L2"},
            {"id": "E2", "kind": "endEvent", "name": "Машина подана",
             "participant": "Перевозчик", "lane": "L2"},
        ],
        "flows": [
            {"id": "f1", "source": "S1", "target": "A1"},
            {"id": "f2", "source": "A1", "target": "E1"},
            {"id": "f3", "source": "S2", "target": "B1"},
            {"id": "f4", "source": "B1", "target": "E2"},
            {"id": "M1", "source": "A1", "target": "B1", "kind": "message",
             "name": "заявка"},
            {"id": "M2", "source": "B1", "target": "A1", "kind": "message",
             "name": "подтверждение"},
        ],
    }


def _plan_flows(plan: dict, conditions: dict) -> dict:
    """Тот же план, но с условиями на указанных потоках: проверка того, что
    структура-словарь и XML дают одинаковый вердикт."""
    return {**plan,
            "flows": [dict(f, **({"condition": conditions[f["id"]]}
                                  if f["id"] in conditions else {}))
                      for f in plan["flows"]]}


# ---------------------------------------------------------------------------
# регистрация в контуре оракула
# ---------------------------------------------------------------------------


def test_business_invariants_are_core_and_always_on():
    """Бизнес-свойства не объявляются сценарием: они проверяются всегда, а
    «нечего проверять» выражается `applicable=False`, а не пропуском."""
    for name in NEW_INVARIANTS:
        assert name in CORE_INVARIANTS, name
        assert name in ALL_CHECKS, name
        assert name not in invariants.SCENARIO_EXPECTATIONS, name
    # Ожиданий сценариев новые имена не требуют: править `eval/scenarios.py`
    # было бы значит превратить всегда-он проверку в ещё одну заявляемую.
    assert not [name for s in SCENARIOS for name in s.expectations()
                if name in NEW_INVARIANTS]


def test_both_entry_points_agree_on_business_invariants():
    """Структура-словарь и XML по этим свойствам не расходятся: подпись плана
    (`name` потока) и подпись bpmn-js (`name` sequenceFlow) читаются одинаково."""
    assert check_xml(rework_xml(loop_condition="да",
                                exit_condition="нет"))["no_blind_rework"].ok
    named = _plan_flows(blind_rework_plan(), {"f_loop": "да", "f_exit": "нет"})
    assert check_structure(named)["no_blind_rework"].ok
    assert not check_xml(rework_xml())["no_blind_rework"].ok
    assert not check_structure(blind_rework_plan())["no_blind_rework"].ok
    assert not check_xml(pingpong_xml())["pools_not_pingpong"].ok
    assert not check_structure(pingpong_plan())["pools_not_pingpong"].ok


# ---------------------------------------------------------------------------
# no_blind_rework
# ---------------------------------------------------------------------------


def test_rework_with_both_legs_named_passes():
    check = check_xml(rework_xml(loop_condition="да",
                                 exit_condition="нет"))["no_blind_rework"]
    assert check.ok and check.applicable
    assert "дуг возврата" in check.reason


def test_rework_with_anonymous_split_legs_is_blind():
    """Ни одна нога развилки не названа — «вернули» неотличимо от «согласовано»."""
    check = check_xml(rework_xml())["no_blind_rework"]
    assert not check.ok
    assert {"f_loop", "f_back"} <= set(check.ids)
    assert "слепой повтор" in check.reason
    assert "возвращает работу в" in check.reason


def test_rework_release_leg_guarded_by_default_passes():
    """Выход по умолчанию — названная нога: петля различима без conditionExpression."""
    assert check_xml(rework_xml(loop_condition="да",
                                exit_default=True))["no_blind_rework"].ok


def test_guarded_exit_leg_alone_is_not_enough_for_the_oracle():
    """Документированное расхождение: скорингу достаточно одной защищённой ноги
    выхода из цикла, оракул спрашивает, различимы ли «вернули» и «согласовано»
    в паре — безымянная нога возврата для него и есть незаписанная причина."""
    xml = rework_xml(exit_condition="согласовано")
    assert not check_xml(xml)["no_blind_rework"].ok
    assert scorer.evaluate(xml)["details_meta"]["rework_loop"]["status"] == "passed"


def test_named_returning_arc_with_named_split_passes():
    assert check_xml(rework_xml(loop_condition="да", exit_condition="нет",
                                back_condition="вернули"))["no_blind_rework"].ok


def test_rework_flow_label_counts_for_the_oracle_only():
    """Подпись потока различает ноги для бизнес-свойства, но не отменяет
    нотационное требование условия — два разных инварианта, две оценки."""
    xml = rework_xml(loop_label="Вернули на доработку", exit_label="Согласовано")
    assert check_xml(xml)["no_blind_rework"].ok
    assert not check_xml(xml)["gateway_conditions_or_default"].ok


def test_cycle_without_any_exit_is_blind_rework_for_the_oracle():
    """Расхождение: цикл без дуги выхода `rework_loop` молча пропускает (дуг
    наружу нет, и правило ждёт их), а оракул зовёт его слепым повтором — чинится
    он тем же `add_condition`."""
    check = check_xml(loop_without_exit_xml())["no_blind_rework"]
    assert not check.ok
    # Обе дуги двушаговой петли A ⇄ B считаются возвратом работы: любая из них
    # без причины — слепой повтор.
    assert set(check.ids) == {"f2", "f3", "A", "B"}
    assert "слепой повтор" in check.reason
    assert status_of(loop_without_exit_xml(), "rework_loop") == "passed"


def test_scheme_without_loops_is_not_applicable():
    check = check_xml(laned_doc([2, 1]))["no_blind_rework"]
    assert check.applicable is False and check.ok


# ---------------------------------------------------------------------------
# pools_not_pingpong
# ---------------------------------------------------------------------------


def test_single_mutual_exchange_of_the_same_pair_fails():
    """Возврат той же пары концов — уже перекидывание, а не «специализация»:
    у работы нет одного хозяина, который доводит её до конца."""
    xml = pingpong_xml()
    check = check_xml(xml)["pools_not_pingpong"]
    assert not check.ok
    assert check.ids == ("M1", "M2")
    assert "«Участник 1» ↔ «Участник 2»" in check.reason
    assert "взаимных обменов этой пары: 1" in check.reason


def test_traffic_over_disjoint_task_pairs_passes():
    """Туда и назад идут разные шаги — одной пары концов нет, и пинг-понга
    оракул не находит."""
    xml = pingpong_xml(same_pair=False)
    check = check_xml(xml)["pools_not_pingpong"]
    assert check.ok and check.applicable
    assert "двусторонних обменов одной работой нет" in check.reason


def test_pool_level_endpoints_count_as_an_exchange():
    """MessageFlow может висеть на пулах целиком, а не на шагах: такой возврат
    тоже перекидывание. Разница со скорингом — ровно здесь: он считает обмен,
    только если оба конца разрешились в участников, а неразрешённый конец для
    оракула — самостоятельная сторона обмена."""
    flows = ('<messageFlow id="M1" sourceRef="P1" targetRef="P2"/>'
             '<messageFlow id="M2" sourceRef="P2" targetRef="P1"/>')
    xml = pools_doc([pool_body(1), pool_body(2)], flows)
    check = check_xml(xml)["pools_not_pingpong"]
    assert not check.ok and check.ids == ("M1", "M2", "P1", "P2")
    flows_dangling = ('<messageFlow id="M1" sourceRef="A1" targetRef="Z9"/>'
                      '<messageFlow id="M2" sourceRef="Z9" targetRef="A1"/>')
    dangling = check_xml(pools_doc([pool_body(1), pool_body(2)],
                                   flows_dangling))["pools_not_pingpong"]
    assert not dangling.ok and dangling.ids == ("M1", "M2", "Z9")


def test_single_pool_is_not_applicable_not_a_pass():
    check = check_xml(laned_doc([2, 1]))["pools_not_pingpong"]
    assert check.applicable is False
    assert "пул один" in check.reason


def test_pools_without_message_flows_are_not_applicable():
    check = check_xml(single_pool_xml())["pools_not_pingpong"]
    assert check.applicable is False
    assert "потоков-сообщений нет" in check.reason


# ---------------------------------------------------------------------------
# no_overloaded_lane
# ---------------------------------------------------------------------------


def test_lane_with_most_of_the_work_fails():
    check = check_xml(laned_doc([4, 1]))["no_overloaded_lane"]
    assert not check.ok
    assert check.ids == ("L1",)
    assert "4 из 5 работ" in check.reason and "80%" in check.reason


def test_two_lane_monopoly_is_checked_by_both_layers():
    """Две дорожки скоринг больше не пропускает: расхождение калитки (3 дорожки у
    скоринга против 2 у оракула) стоило контуру подсказки на дежурной смене из
    production_incident, где 4 работы из 5 лежат на одном исполнителе.

    С этого прохода слои сходятся и по порогу: 4 работы из 6 на одной из трёх
    дорожек — 67%, и оракул больше не считает это нормой (прежнее решение
    «67% пула — не монополия» отменено по измерению: на 18 схемах корпуса
    скоринг ругался, а независимая приёмка молчала). Различие в знаменателе
    остаётся и проверяется отдельно — `test_unlaned_work_is_in_the_denominator`."""
    xml = laned_doc([4, 1, 1])
    assert not check_xml(xml)["no_overloaded_lane"].ok, "67% при трёх ролях — узкое место"
    assert check_xml(xml)["no_overloaded_lane"].ids == ("L1",)
    assert status_of(xml, "lane_overload") == "failed"
    assert "60% в 3 дорожках" in check_xml(xml)["no_overloaded_lane"].reason
    two_lanes = laned_doc([1, 7])
    assert not check_xml(two_lanes)["no_overloaded_lane"].ok
    assert check_xml(two_lanes)["no_overloaded_lane"].ids == ("L2",)
    assert status_of(two_lanes, "lane_overload") == "failed"
    # Порог пары дорожек не изменился: 6 из 8 (75%) — не монополия, а «делает /
    # ждёт», и ровно так же это видит скоринг.
    both_easy = laned_doc([6, 2])
    assert check_xml(both_easy)["no_overloaded_lane"].ok
    assert status_of(both_easy, "lane_overload") == "passed"


def test_empty_lane_takes_the_blame_like_the_scorer_says_it_should():
    check = check_xml(laned_doc([5, 0, 0, 0]))["no_overloaded_lane"]
    assert not check.ok and check.ids == ("L1",)


def test_single_lane_pool_is_not_applicable_not_a_pass():
    check = check_xml(laned_doc([6]))["no_overloaded_lane"]
    assert check.applicable is False
    assert "двумя и более дорожками нет" in check.reason


def test_unlaned_work_is_in_the_denominator():
    """Знаменатель — все работы пула, а не только разложенные по дорожкам.
    Единственное различие двух слоёв после того, как пороги совпали: шаг без роли
    разбавляет долю дорожки (тут скоринг видит 4 из 5 размеченных = 80% и ругается,
    оракул — 4 из 7 работ пула = 57% и молчит), зато схема без laneSet не получает
    права молчать."""
    xml = _doc('<laneSet id="LS">'
               + _lane("L1", "Все на нём", ["A1", "A2", "A3", "A4"])
               + _lane("L2", "Роль-фишка", ["A5"]) + _lane("L3", "И ещё роль", [])
               + '</laneSet>'
               '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "A1")
               + '<userTask id="A1" name="Шаг 1"/>' + _flow("f1", "A1", "A2")
               + '<userTask id="A2" name="Шаг 2"/>' + _flow("f2", "A2", "A3")
               + '<userTask id="A3" name="Шаг 3"/>' + _flow("f3", "A3", "A4")
               + '<userTask id="A4" name="Шаг 4"/>' + _flow("f4", "A4", "A5")
               + '<userTask id="A5" name="Шаг 5"/>' + _flow("f5", "A5", "A6")
               + '<userTask id="A6" name="Без дороги 1"/>' + _flow("f6", "A6", "A7")
               + '<userTask id="A7" name="Без дороги 2"/>' + _flow("f7", "A7", "E")
               + '<endEvent id="E" name="Готово"/>')
    check = check_xml(xml)["no_overloaded_lane"]
    assert check.ok, check.reason
    assert "57%" in check.reason, "проход обязан назвать меру, которой мерили"
    assert "60%" in check.reason
    assert status_of(xml, "lane_overload") == "failed"


# ---------------------------------------------------------------------------
# waits_have_sla
# ---------------------------------------------------------------------------


def test_wait_without_any_timer_fails():
    check = check_xml(wait_xml())["waits_have_sla"]
    assert not check.ok and check.ids == ("W",)
    assert "ожидание без срока" in check.reason


def test_receive_task_is_a_wait_for_both_layers():
    """`receiveTask` («ждём подпись получателя») — ожидание, и слои согласны:
    токен стоит до сообщения. Скоринг раньше его не считал и на складе с
    единственным таким шагом молчал, пока оракул называл висящий маршрут."""
    xml = wait_xml(wait_kind="receiveTask")
    assert not check_xml(xml)["waits_have_sla"].ok
    assert status_of(xml, "wait_without_sla") == "failed"


def test_boundary_timer_on_the_wait_counts_as_its_sla_for_both_layers():
    """Граничный таймер на ожидании — и есть срок по семантике BPMN (он
    прерывает ожидание). Скоринг раньше ждал `timerEventDefinition` внутри самого
    catch-события и звал схему провалом — тем самым движком, который он же и
    рекомендует (`add_boundary_event`): предложенная правка не снимала
    нарушение, и контур улучшения получал регресс за собственную подсказку."""
    xml = wait_xml(on_wait=True)
    check = check_xml(xml)["waits_have_sla"]
    assert check.ok and check.applicable
    assert status_of(xml, "wait_without_sla") == "passed"


def test_link_target_and_compensation_trigger_are_not_waits_for_both_layers():
    """`linkEventDefinition` у catch-события — метка, в которую прыгают с другой
    стороны схемы, а `compensateEventDefinition` — внутренний триггер отработки.
    Ни то ни другое не ждёт ответа извне, и «висящий маршрут без срока» им
    нечего вменять. По корпусу рукописных схем одна метка перехода приносила 675
    нарушителей правила из 2731 — то есть линейка ругалась в основном на то, чем
    ожидание не является. Оракул и скоринг сужаются вместе: расхождение по
    бизнес-признаку означало бы, что один слой меряет себя сам.

    Сигнал и условие остаются нарушениями: их триггер приходит извне, и пока он
    не пришёл, токен стоит столько, сколько ему никто не сигналит.
    """
    link = _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "L")
        + '<intermediateCatchEvent id="L" name="Метка входа">'
          '<linkEventDefinition name="Вход"/></intermediateCatchEvent>'
        + _flow("f2", "L", "C")
        + '<intermediateCatchEvent id="C" name="Отработка">'
          '<compensateEventDefinition/></intermediateCatchEvent>'
        + _flow("f3", "C", "E") + '<endEvent id="E" name="Готово"/>')
    wait = check_xml(link)["waits_have_sla"]
    assert wait.ok and not wait.applicable
    assert status_of(link, "wait_without_sla") == "not_applicable"
    signal = _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "W")
        + '<intermediateCatchEvent id="W" name="Ждём сигнал">'
          '<signalEventDefinition/></intermediateCatchEvent>'
        + _flow("f2", "W", "E") + '<endEvent id="E" name="Готово"/>')
    assert not check_xml(signal)["waits_have_sla"].ok
    assert status_of(signal, "wait_without_sla") == "failed"


def test_parallel_deadline_branch_is_an_sla():
    """Ветка «срок вышел», уходящая от развилки и не ждущая ответа, — легальная
    форма SLA (так и устроена «заявка отклоняется через два часа» в сценариях).

    Это было единственное расхождение слоёв по ожиданию: скоринг признавал срок
    только таймером на самом ожидании и советовал `add_boundary_event`, который
    аплайер на catch-событие не принимает («не является задачей»). Теперь обе
    линейки принимают развилку, и нарушение снимается подсказкой скоринга."""
    xml = wait_xml(parallel=True)
    assert check_xml(xml)["waits_have_sla"].ok
    assert status_of(xml, "wait_without_sla") == "passed"


def test_exclusive_fork_to_a_timer_is_not_a_deadline_for_either_layer():
    """Ветки исключительного шлюза выбираются в момент развилки: таймер рядом с
    ожиданием — это другой сценарий, а не срок. Форму не принимает ни оракул, ни
    скоринг, и это общий знаменатель двух независимых реализаций.

    Прямая нога развилки — второе общее требование: таймер за задачей в соседней
    ветке (`elsewhere`) сроком ожиданию не считается."""
    xml = ('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">'
           '<process id="Proc_1" name="Процесс">'
           '<startEvent id="S" name="Начало"/>'
           + _flow("f1", "S", "G")
           + '<exclusiveGateway id="G" name="Ждём или отменяем?"/>'
           + _flow("f2", "G", "W") + _flow("f3", "G", "T")
           + '<intermediateCatchEvent id="W" name="Ожидание ответа">'
             '<messageEventDefinition/></intermediateCatchEvent>'
           + _flow("f4", "W", "A")
           + '<userTask id="A" name="Обработать ответ"/>' + _flow("f5", "A", "E")
           + '<intermediateCatchEvent id="T" name="Срок вышел">'
             '<timerEventDefinition><timeDuration>PT2H</timeDuration>'
             '</timerEventDefinition></intermediateCatchEvent>'
           + _flow("f6", "T", "E2")
           + '<endEvent id="E" name="Готово"/>'
             '<endEvent id="E2" name="Отказ"/>'
           '</process></definitions>')
    check = check_xml(xml)["waits_have_sla"]
    assert not check.ok and check.ids == ("W",)
    assert status_of(xml, "wait_without_sla") == "failed"


def test_timer_downstream_or_elsewhere_does_not_bound_the_wait():
    """Токен доходит до таймера «ниже» только когда ожидание завершилось, а
    таймер в отрыве от маршрута ожидания — тот же прокси «в процессе есть
    таймер», который оракул не принимает: нарушение названо, и оба слоя
    согласны."""
    for kwargs in ({"after": True}, {"elsewhere": True}):
        check = check_xml(wait_xml(**kwargs))["waits_have_sla"]
        assert not check.ok, kwargs
        assert check.ids == ("W",)
        assert status_of(wait_xml(**kwargs), "wait_without_sla") == "failed"


def test_timer_wait_is_not_a_wait_for_the_deadline_rule():
    """Catch с `timerEventDefinition` — и есть срок, а не ожидание без него;
    когда блокирующих ожиданий нет, проверка не применима."""
    check = check_xml(wait_xml(definition="<timerEventDefinition/>")
                     )["waits_have_sla"]
    assert check.applicable is False and "блокирующих ожиданий" in check.reason


# ---------------------------------------------------------------------------
# независимость оракула от скоринга
# ---------------------------------------------------------------------------


def test_oracle_does_not_import_the_scorer():
    """Линейка не может жить в одном файле с измеряемым значением.

    Проверяется и статически (разбор AST собственного исходника оракула), и в
    рантайме: модуль компилируется заново с «ядом» в `sys.modules`, так что
    случайный `import core.bpmn_scoring` — даже через `importlib` — уронит тест,
    а не пройдёт молча."""
    source = Path(invariants.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add("." * (node.level or 0) + node.module)
            # `from . import bpmn_scoring` тоже попадает сюда — через имена
            imported.update(f"{node.module or ''}.{a.name}".strip(".")
                            for a in node.names)
    assert not [m for m in imported if "bpmn_scoring" in m], imported

    fresh_name = "oracle_under_isolated_import"
    fresh = types.ModuleType(fresh_name)
    fresh.__file__ = str(invariants.__file__)
    poison = "core.bpmn_scoring"
    saved = sys.modules.get(poison, "absent")
    sys.modules[poison] = None           # import этого имени теперь ImportError
    # Модуль регистрируется до исполнения: `@dataclass` смотрит в
    # `sys.modules[cls.__module__]`, и без этого падение теста объяснялось бы
    # устройством dataclasses, а не случайным импортом.
    sys.modules[fresh_name] = fresh
    try:
        exec(compile(source, fresh.__file__, "exec"), fresh.__dict__)
    finally:
        sys.modules.pop(fresh_name, None)
        if saved == "absent":
            sys.modules.pop(poison, None)
        else:
            sys.modules[poison] = saved
    assert fresh.CORE_INVARIANTS == CORE_INVARIANTS
    assert hasattr(fresh, "business_agreement")
    # Единственное место, где имя модуля-линейки имеет право звучать, — docstring
    # о независимости: ни import, ни `importlib.import_module("core.bpmn_scoring")`,
    # ни динамический `getattr(importlib, ...)`.
    doc = ast.get_docstring(tree) or ""
    span = range(source.index(doc), source.index(doc) + len(doc)) if doc else range(0)
    assert all(i in span for i in _occurrences(source, "bpmn_scoring")), (
        "имя скоринга вне docstring модуля — оракул снова смотрит в линейку")


def _occurrences(text: str, needle: str):
    start = 0
    while (i := text.find(needle, start)) >= 0:
        yield i
        start = i + 1


# ---------------------------------------------------------------------------
# сверка слоёв
# ---------------------------------------------------------------------------


def test_business_agreement_maps_rules_and_verdicts():
    xml = laned_doc([4, 1, 1])          # 4 работы из 6 на одной из трёх дорожек
    checks = check_xml(xml)
    agreement = business_agreement(checks, scorer.evaluate(xml))
    assert set(agreement) == set(invariants.SCORING_TO_ORACLE)
    row = agreement["lane_overload"]
    assert row["invariant"] == "no_overloaded_lane"
    # Порог оракула совпал со скоринговым (60% при трёх и более дорожках, 75% при
    # двух): прежнее расхождение читалось не как «две независимые меры», а как
    # «продукт ругается, а приёмка молчит». На корпусе это ровно те 18 схем,
    # которые перепись помечала `overcharged`, и на них оракул теперь находит то
    # же, что скоринг, — расхождение по этому признаку исчезло с обеих сторон.
    assert row["scorer"] == "failed" and row["oracle"] == "failed", row
    assert row["verdict"] == AGREE, row
    assert business_disagreements(agreement) == [], agreement
    # `approval_chain` обрёл независимую пару (`signoffs_need_a_gate`) в этом
    # проходе. Раньше здесь стояло обратное решение: «четыре согласующих подряд —
    # много» не выводится из семантики BPMN, и зеркало считалось самоподтверждением
    # линейки. Оно отменено по измерению: правило живое (37 схем корпуса из 367), а
    # бизнес-метрики харнесса про этот класс молчали, то есть улучшение цепочки
    # согласований ни в какое число не превращалось.
    # Порог и набор ручных типов совпадают со скорингом намеренно — как у
    # `waits_have_sla`: расхождение по одному бизнес-вопросу означало бы, что
    # продукт меряет себя сам. Независимость в другом: оракул идёт по `seq_out`
    # своего `_Graph`, а не по `_longest_user_task_line` над `_Schema`, и перепись
    # это проверяет — 367/367 сходятся, из них 37 с находкой с обеих сторон.
    assert invariants.SCORING_TO_ORACLE["approval_chain"] == "signoffs_need_a_gate"
    row = agreement["approval_chain"]
    assert row["scorer"] == "failed" and row["oracle"] == "failed", row
    assert row["verdict"] == "agree", row


def test_business_agreement_reports_the_oracles_own_findings():
    """Обе стороны расхождения имеют адресата: оракул обязан назвать id, а
    «не применимо» у скоринга не превращает находку в согласие слоёв.

    Ответ скоринга подставлен намеренно: с реальным `evaluate` два слоя на такой
    схеме согласны (двухдорожечную монополию скоринг видит с тех же 75%), а здесь
    проверяется именно разбор вердикта."""
    two_lanes = laned_doc([1, 7])       # 7 работ из 8 на второй дорожке
    row = business_agreement(check_xml(two_lanes),
                             {"details_meta": {"lane_overload":
                                               {"status": "not_applicable",
                                                "elements": []}}})["lane_overload"]
    assert row["verdict"] == ORACLE_STRICTER
    assert row["oracle_ids"] == ["L2"] and row["scorer"] == "not_applicable"
    both = business_agreement(check_xml(two_lanes), scorer.evaluate(two_lanes))
    assert both["lane_overload"]["verdict"] == AGREE


def test_business_agreement_accepts_flat_details_and_missing_data():
    checks = check_xml(laned_doc([4, 1]))
    assert (business_agreement(checks, {"details": {"lane_overload": True}})
            ["lane_overload"]["verdict"] == ORACLE_STRICTER)
    empty = business_agreement(checks, {})
    assert {row["verdict"] for row in empty.values()} == {NO_DATA}
    assert business_agreement({}, {"details_meta": {}})["rework_loop"]["verdict"] == NO_DATA


def test_not_applicable_on_one_side_is_not_agreement():
    """«Мне не применимо» рядом с «я нашёл дефект» — это односторонний взгляд,
    а не согласие: иначе провал бизнеса прятался бы за калиткой порога."""
    checks = check_xml(wait_xml())
    agreement = business_agreement(
        checks, {"details_meta": {"wait_without_sla": {"status": "not_applicable",
                                                       "elements": []}}})
    assert agreement["wait_without_sla"]["verdict"] == ORACLE_STRICTER
    both_quiet = business_agreement(
        check_xml(laned_doc([2, 1])), {"details_meta": {"rework_loop":
                                                        {"status": "not_applicable"}}})
    assert both_quiet["rework_loop"]["verdict"] == NOT_COMPARABLE


# ---------------------------------------------------------------------------
# фикстуры: оракул и скоринг на реальном наборе кейсов
# ---------------------------------------------------------------------------


def _resolve(module_name: str, *names):
    """Первая найденная вызываемая функция среди кандидатов.

    Имена в ядре меняются прямо сейчас (у харнесса из-за этого резолв по
    списку), а требовать от сверки слоёв ровно одного варианта — значит валить
    её на чужом рефакторинге, а не на собственном провале."""
    module = importlib.import_module(module_name)
    for name in names:
        found = getattr(module, name, None)
        if callable(found):
            return found
    raise LookupError(f"в {module_name} нет ни одной из: {', '.join(names)}")


def _scheme_xml(plan: dict) -> str:
    """Починка структуры и генерация XML штатными функциями ядра — как в
    replay-прогоне, но без импорта `eval.harness` (он перестраивается)."""
    repaired = _resolve("core.bpmn_generator", "repair_structure", "repair_plan",
                        "normalize_structure")(dict(plan))
    structure = repaired[0] if isinstance(repaired, tuple) else repaired
    try:
        make_xml = _resolve("core.bpmn_generator", "generate_bpmn_xml",
                            "build_bpmn_xml", "structure_to_xml")
    except LookupError:
        generator = getattr(importlib.import_module("core.bpmn_generator"),
                            "BPMNGenerator")()
        make_xml = _resolve_object(generator, "generate_bpmn_xml",
                                   "_generate_bpmn_xml")
    return make_xml(dict(structure))


def _resolve_object(obj, *names):
    for name in names:
        found = getattr(obj, name, None)
        if callable(found):
            return found
    raise LookupError(f"у {type(obj).__name__} нет ни одного из: {', '.join(names)}")


def _fixture_schemes():
    """Схемы `eval/fixtures/`: план → `repair_structure` → XML, а improve-фикстура
    — пакет операций поверх своей базовой схемы."""
    out = []
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") == "plan":
            plan = data["plan"]
        else:
            base = json.loads((FIXTURES / f"{data['base_plan']}.json")
                              .read_text(encoding="utf-8"))
            plan = base["plan"]
        xml = _scheme_xml(plan)
        if data.get("kind") == "improve":
            xml, _report = _resolve("core.bpmn_edits", "apply_operations")(
                xml, [dict(op) for op in (data.get("operations") or [])])
            repair = _resolve("core.bpmn_edits", "validate_and_repair")(xml)
            xml = repair[0] if isinstance(repair, tuple) else repair
        out.append((data["id"], xml))
    return out


SCHEMES = _fixture_schemes()

# Расхождения двух слоёв на текущем наборе — с перечислением по имени, потому
# что каждое из них подписано в docstring соответствующей проверки. Все они
# односторонние: дефект находит оракул, а скоринг его не видит.
#   lane_overload / no_overloaded_lane — расхождений нет с тех пор, как оракул
#     взял тот же двухпороговый признак, что и скоринг (60% при трёх дорожках,
#     75% при двух): прежняя калитка «дорожек ≥3» стоила приёмке 18 схем корпуса,
#     где продукт ругался в одиночку. Знаменатель при этом остался свой — оракул
#     делит на все работы пула, а не на работы размеченных дорожек;
#   wait_without_sla / waits_have_sla — на наборе расхождений нет: скоринг
#     перестал требовать «таймер где-то в процессе» и знает `receiveTask`, а
#     складская подпись получателя ждётся без срока в обоих слоях. Вывернутое
#     расхождение остаётся за пределами набора: параллельную ветку «срок вышел»
#     оракул принимает, скоринг — нет (см. test_parallel_deadline_branch_is_an_sla);
#   handoff_pingpong / pools_not_pingpong и rework_loop / no_blind_rework — на
#     наборе расхождений нет; эталон кредитной заявки ловят оба слоя (банк ↔
#     бюро кредитных историй по одной и той же паре шагов), и это случай, когда
#     оракул и скоринг согласны в провале — см. EXPECTED_NEW_FAILURES.
EXPECTED_DISAGREEMENTS = {
    "employee_onboarding.bad.plan": {},
    "loan_application.good.plan": {},
    "product_return.good.plan": {},
    # Пусто с тех пор, как скоринг проверяет и двухдорожечные пулы: 4 работы
    # из 5 на «Дежурном инженере» теперь видят оба слоя, а эта строчка была
    # единственным расхождением набора (`oracle_stricter`) из-за калитки.
    "production_incident.bad.plan": {},
    "production_incident.good.improve": {},
    "production_incident.good.plan": {},
    "purchase_approval.loop.bad.plan": {},
    "purchase_approval.loop.good.improve": {},
    "purchase_approval.signoffs.bad.plan": {},
    "purchase_approval.signoffs.good.improve": {},
    "purchase_approval.bad.plan": {},
    "purchase_approval.good.plan": {},
    "support_ticket.good.plan": {},
    "vehicle_reservation.bad.plan": {},
    "warehouse_delivery.good.plan": {},
    "warehouse_delivery.live.improve": {},
    "warehouse_delivery.live.plan": {},
}

# Что из набора поймали новые бизнес-инварианты. Это находки про набор кейсов,
# а не поломка харнесса: «эталонный» warehouse_delivery держит ожидание подписи
# без единого срока в модели (и в improve-пакете он тоже не появляется), а
# «эталонный» loan_application возвращает кредитную историю в тот же шаг —
# пинг-понг, в котором слои согласны. Править фикстуры нельзя, поэтому список
# здесь: он превращает «oracle нашёл дыру в эталоне» в проверяемые данные.
EXPECTED_NEW_FAILURES = {
    "loan_application.good.plan": ["pools_not_pingpong"],
    "production_incident.bad.plan": ["no_overloaded_lane", "waits_have_sla"],
    "warehouse_delivery.good.plan": ["waits_have_sla"],
    "warehouse_delivery.live.improve": ["waits_have_sla"],
    "warehouse_delivery.live.plan": ["waits_have_sla"],
    # Единственный свидетель класса «согласования без развилки» в наборе: без
    # этой фикстуры `signoffs_need_a_gate` был бы проверкой, которая на eval-данных
    # не звенит никогда.
    "purchase_approval.signoffs.bad.plan": ["signoffs_need_a_gate"],
    # Свидетель класса «безусловный цикл»: возврат на доработку без развилки.
    # Порядок — как в `NEW_INVARIANTS`.
    "purchase_approval.loop.bad.plan": ["no_blind_rework", "loops_have_a_guard"],
}


@pytest.mark.parametrize("fixture_id,xml", SCHEMES, ids=[s[0] for s in SCHEMES])
def test_business_invariants_find_exactly_the_documented_case_set_gaps(fixture_id, xml):
    """Бизнес-инварианты обязаны молчать там, где набор кейсов к проверке готов,
    и не находить новых дыр сверх подписанных."""
    checks = check_xml(xml)
    assert [n for n in NEW_INVARIANTS if not checks[n].ok] == \
        EXPECTED_NEW_FAILURES.get(fixture_id, [])


@pytest.mark.parametrize("fixture_id,xml", SCHEMES, ids=[s[0] for s in SCHEMES])
def test_oracle_and_scorer_differ_only_where_documented(fixture_id, xml):
    agreement = business_agreement(check_xml(xml), scorer.evaluate(xml))
    assert set(agreement) == set(invariants.SCORING_TO_ORACLE)
    diverged = {rule: row["verdict"] for rule, row in agreement.items()
                if row["verdict"] not in (AGREE, NOT_COMPARABLE)}
    assert diverged == EXPECTED_DISAGREEMENTS[fixture_id], (
        f"{fixture_id}: слои разошлись не там, где это описано в docstrings")
    for rule, verdict in EXPECTED_DISAGREEMENTS[fixture_id].items():
        row = agreement[rule]
        assert row["oracle_reason"], "расхождение без причины у оракула"
        assert row["verdict"] == verdict
        if verdict == ORACLE_STRICTER:
            # Оракул нашёл дефект — он обязан быть адресуемым: без списка id
            # человеку нечем открыть схему в этом месте.
            assert row["oracle_ids"], (fixture_id, rule)
        else:
            # Обратная сторона: оракул чист, и спорить можно только с названным
            # элементом скоринга.
            assert row["oracle_ids"] == [] and row["scorer_elements"], (
                fixture_id, rule)


@pytest.mark.parametrize("fixture_id,xml", SCHEMES, ids=[s[0] for s in SCHEMES])
def test_oracle_and_scorer_agree_on_the_same_verdict_direction(fixture_id, xml):
    """Там, где оба слоя назвали дефект, они обязаны называть его одними
    элементами: иначе «согласие» маскировало бы два разных нарушения."""
    checks = check_xml(xml)
    agreement = business_agreement(checks, scorer.evaluate(xml))
    for rule, row in agreement.items():
        if row["verdict"] != AGREE:
            continue
        ids = set(checks[row["invariant"]].ids)
        # общий элемент — обязательный: списки id у слоёв разные (скоринг ведёт
        # пулы, оракул — дуги), но пустое пересечение означало бы, что
        # «согласились» про несвязанные участки схемы.
        assert ids or not row["scorer_elements"], (fixture_id, rule)


# ---------------------------------------------------------------------------
# оракул и висячие концы потоков: то, что скоринг прощал на реальном корпусе
# ---------------------------------------------------------------------------

def test_a_flow_into_a_missing_node_is_not_an_exit_for_the_oracle():
    """Дуга без цели (или на узел, которого в схеме нет) не делает шаг
    соединённым. До этой правки `_Graph` заводил по ней смежность, `no_unrouted`
    молчал, а скоринг со своим новым `_Schema` ругался — расхождение, которое
    перепись кросс-слоевой согласованности ловила бы как «скоринг строже».
    Замер корпуса: 2 схемы, где так скрывался тупик."""
    xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
               + '<userTask id="A" name="Собрать документы"/>'
               '<sequenceFlow id="f2" sourceRef="A"/>'
               '<endEvent id="E" name="Готово"/>')
    check = check_xml(xml)["no_unrouted"]
    assert not check.ok and "A" in check.ids, check.reason
    assert status_of(xml, "no_isolated") == "failed"


def test_a_message_flow_ending_on_a_participant_interacts_that_pool():
    """Конец messageFlow — участник целиком: BPMN 2.0 это разрешает, и Signavio
    рисует так «фронт» банка. Оракул раньше видел только концы-шаги и объявлял
    такой пул немым на 35 схемах корпуса из 367, а `participant_interacts` стоит
    в гейте (`scenario_pass`): гейт оштрафовал схему, в которой обмен есть."""
    flows = ('<messageFlow id="M1" sourceRef="A1" targetRef="P2"/>'
             '<messageFlow id="M2" sourceRef="P2" targetRef="A2"/>')
    xml = pools_doc([pool_body(1), pool_body(2)], flows)
    check = check_xml(xml)["participant_interacts"]
    assert check.ok and check.applicable, check.reason
    assert status_of(xml, "participant_interacts") == "passed"


def test_a_pool_with_no_message_flow_at_all_is_still_silent():
    """Поправка про пуловые концы не должна сделать инвариант слепым: пул,
    которого нет ни в одном конце ни одного messageFlow, так и остаётся
    участником без взаимодействий."""
    flows = '<messageFlow id="M1" sourceRef="A1" targetRef="A2"/>'
    xml = pools_doc([pool_body(1), pool_body(2), pool_body(3)], flows)
    check = check_xml(xml)["participant_interacts"]
    assert not check.ok and check.ids == ("Участник 3",), check.reason


def test_event_based_fork_is_not_a_split_while_inclusive_fork_is():
    """Решение обоих слоёв о том, у какой развилки обязано быть схождение.

    У `eventBasedGateway` ноги ждут разных событий и срабатывает одна: схождения
    потоков по нотации нет, и требовать его — значит ругать корректную схему
    (оракул на этом ловил 7 схем корпуса из 98 с разветвлённым event-шлюзом).
    `inclusiveGateway`, наоборот, пускает токен по каждой подходящей ветке, и
    его неразведённая нога — висящий маршрут; скоринг раньше на него не смотрел.
    Проверка держит обе половины решения, потому что за ними стоит один кортеж
    тегов в каждом слое (`SPLIT_GATEWAY_TAGS` и `_splits`)."""
    ev = _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G")
        + '<eventBasedGateway id="G" name="Что раньше?"/>'
        + _flow("f1", "G", "C1")
        + '<intermediateCatchEvent id="C1" name="Ответ клиента">'
          '<messageEventDefinition/></intermediateCatchEvent>'
        + _flow("f2", "G", "C2")
        + '<intermediateCatchEvent id="C2" name="Срок ответа">'
          '<timerEventDefinition/></intermediateCatchEvent>')
    assert status_of(ev, "gateway_split_join") == "not_applicable"
    assert check_xml(ev)["gateway_split_join"].applicable is False

    inc = _doc(
        '<startEvent id="S" name="Начало"/>' + _flow("g0", "S", "G")
        + '<inclusiveGateway id="G" name="Что есть?"/>'
        + _flow("g1", "G", "A") + '<userTask id="A" name="Собрать"/>'
        + _flow("g2", "A", "E") + '<endEvent id="E" name="Готово"/>'
        + _flow("g3", "G", "B") + '<manualTask id="B" name="Упаковать"/>')
    assert status_of(inc, "gateway_split_join") == "failed"
    assert not check_xml(inc)["gateway_split_join"].ok


def two_pools_xml(flows: str, message_flows: str = "") -> str:
    """Два пула по `старт → шаг → финиш`; `flows` и `message_flows` — дополнительные
    дуги между ними. Оракул читает принадлежность узла по имени пула, поэтому
    межпуловая дуга для него видна и в XML, и в плане одинаково."""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{HEADER}'
            '<collaboration id="C1">'
            '<participant id="P1" name="Склад" processRef="Proc_1"/>'
            '<participant id="P2" name="Перевозчик" processRef="Proc_2"/>'
            f'{message_flows}</collaboration>'
            '<process id="Proc_1" name="Склад">'
            '<startEvent id="S1" name="Заявка"/>' + _flow("f1", "S1", "A1")
            + '<userTask id="A1" name="Заказать транспорт"/>'
            + _flow("f2", "A1", "E1") + '<endEvent id="E1" name="Отправлено"/>'
            f'{flows}</process>'
            '<process id="Proc_2" name="Перевозчик">'
            '<startEvent id="S2" name="Заявка принята"/>' + _flow("f3", "S2", "A2")
            + '<userTask id="A2" name="Подать машину"/>'
            + _flow("f4", "A2", "E2") + '<endEvent id="E2" name="Машина подана"/>'
            '</process></definitions>')



class TestMessageFlowOnGateway:
    """Оракул заряжает обмен с развилкой на конце — независимо от скоринга.

    Пара `message_flow_ends ↔ message_flow_ends` сверяется на корпусе, где
    шлюзовых концов ноль (625 обменов), поэтому проверка обязана быть и на
    фикстуре: иначе правило молчит «правильно», а на живой схеме контура
    расхождение осталось бы невидимым.
    """

    def test_message_flow_from_a_gateway_is_charged(self):
        xml = two_pools_xml('<exclusiveGateway id="G1" name="Куда"/>',
                            message_flows='<messageFlow id="m1" sourceRef="G1" '
                                          'targetRef="A2"/>')
        check = check_xml(xml)["message_flow_ends"]
        assert check.applicable and not check.ok
        assert "m1" in check.ids and "развилка" in check.reason

    def test_message_flow_between_steps_is_not_charged(self):
        xml = two_pools_xml("", message_flows='<messageFlow id="m1" '
                                              'sourceRef="A1" targetRef="A2"/>')
        assert check_xml(xml)["message_flow_ends"].ok

    def test_scorer_and_oracle_charge_the_same_class(self):
        xml = two_pools_xml('<exclusiveGateway id="G1" name="Куда"/>',
                            message_flows='<messageFlow id="m1" sourceRef="G1" '
                                          'targetRef="A2"/>')
        meta = BPMNScorer().evaluate(xml).get("details_meta") or {}
        assert meta["message_flow_ends"]["status"] == "failed"
        assert not check_xml(xml)["message_flow_ends"].ok


class TestFlowBoundaries:
    """Границы потока в оракуле: дуга управления не пересекает пул, а у обмена
    оба конца названы. Классы найдены переписью корпуса (8 дуг в 5 схемах и 8 в
    6 из 367) и живым ответом модели (`warehouse_delivery.live.plan`: 5 межпуловых
    дуг, ни одного `kind="message"`), а не фикстурами."""

    def test_cross_pool_sequence_flow_is_charged(self):
        xml = two_pools_xml(_flow("x1", "A1", "S2"))
        check = check_xml(xml)["flows_within_pool"]
        assert check.applicable and not check.ok
        assert "x1" in check.ids
        assert "Склад" in check.reason and "Перевозчик" in check.reason

    def test_message_leg_between_pools_is_the_legal_form(self):
        xml = two_pools_xml("", message_flows='<messageFlow id="m1" '
                           'sourceRef="A1" targetRef="A2"/>')
        check = check_xml(xml)["flows_within_pool"]
        assert check.ok, check.reason

    def test_a_message_leg_retyped_as_control_flow_fails(self):
        """Та же схема, где нога M1 объявлена потоком управления: пулы не
        поменялись, а законность пропала — оракул различает вид дуги, а не
        только её концы."""
        plan = pingpong_plan()
        crossed = {**plan, "flows": [
            dict(f, kind="sequence") if f["id"] == "M1" else f
            for f in plan["flows"]]}
        assert not check_structure(crossed)["flows_within_pool"].ok
        assert check_structure(plan)["flows_within_pool"].ok

    def test_single_pool_scheme_is_not_applicable(self):
        check = check_structure(blind_rework_plan())["flows_within_pool"]
        assert check.applicable is False

    def test_message_flow_without_an_end_is_charged(self):
        xml = two_pools_xml("", message_flows='<messageFlow id="m1" '
                           'sourceRef="A1" targetRef=""/>')
        check = check_xml(xml)["message_flow_ends"]
        assert check.applicable and not check.ok and "m1" in check.ids
        assert "нет цели" in check.reason

    def test_message_flow_to_a_participant_is_legal(self):
        xml = two_pools_xml("", message_flows='<messageFlow id="m1" '
                           'sourceRef="A1" targetRef="P1"/>')
        assert check_xml(xml)["message_flow_ends"].ok

    def test_scheme_without_message_flows_is_not_applicable(self):
        assert check_xml(two_pools_xml(""))["message_flow_ends"].applicable is False

    def test_scorer_and_oracle_charge_the_same_three_classes(self):
        """Расхождение по этим правилам — уже не «один слой видит, другой нет»:
        пары стоят в `NOTATION_PAIRS`, и `eval.coverage` сверяет их на всём
        корпусе. Третье правило (`flow_ends_legal`) на корпусе не срабатывает
        ни разу: его источник — прогоны самого контура, а не датасет."""
        from core.bpmn_scoring import BPMNScorer

        xml = two_pools_xml(_flow("x1", "A1", "S2") + _flow("x2", "E1", "A1"),
                            message_flows='<messageFlow id="m1" sourceRef="A1"'
                                          ' targetRef="нет-такого"/>')
        meta = BPMNScorer().evaluate(xml)["details_meta"]
        assert meta["cross_pool_flow"]["status"] == "failed"
        assert meta["message_flow_ends"]["status"] == "failed"
        assert meta["flow_ends_legal"]["status"] == "failed"
        results = check_xml(xml)
        assert not results["flows_within_pool"].ok
        assert not results["message_flow_ends"].ok
        assert not results["flow_ends_legal"].ok
        assert "x2" in results["flow_ends_legal"].ids


def _timer_scheme(definition: str) -> str:
    """Однопуловая схема с граничным таймером просрочки на задаче; `definition`
    — содержимое `boundaryEvent` дословно."""
    return _doc('<startEvent id="S" name="Заказ поступил"/>'
                + _flow("f1", "S", "A")
                + '<userTask id="A" name="Собрать заказ"/>'
                + f'<boundaryEvent id="B1" name="Просрочка" attachedToRef="A">'
                  f'{definition}</boundaryEvent>'
                + _flow("f2", "A", "E") + '<endEvent id="E" name="Готово"/>'
                + _flow("f3", "B1", "E"))


class TestTimerSchedule:
    """Таймер без хронометража — мёртвый срок: `event_definitions` про него
    молчит правильно (определение есть), а исполнитель такое событие не заведёт,
    и ветка эскалации не наступит. Класс принёс корпус: 108 схем из 367."""

    def test_a_timer_without_a_schedule_is_charged(self):
        check = check_xml(_timer_scheme("<timerEventDefinition/>"))["timer_schedule"]
        assert check.applicable and not check.ok
        assert check.ids == ("B1",)

    @pytest.mark.parametrize("definition", [
        "<timerEventDefinition><timeDuration>PT1H</timeDuration>"
        "</timerEventDefinition>",
        "<timerEventDefinition><timeCycle>R3/PT10M</timeCycle>"
        "</timerEventDefinition>",
        "<timerEventDefinition><timeDate>2026-01-01T09:00:00Z</timeDate>"
        "</timerEventDefinition>",
    ])
    def test_any_non_empty_schedule_is_enough(self, definition):
        assert check_xml(_timer_scheme(definition))["timer_schedule"].ok

    def test_a_blank_schedule_is_the_same_defect(self):
        xml = _timer_scheme("<timerEventDefinition><timeDuration>  </timeDuration>"
                            "</timerEventDefinition>")
        assert not check_xml(xml)["timer_schedule"].ok

    def test_scheme_without_timers_is_not_applicable(self):
        xml = _doc('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
                   + '<userTask id="A" name="Шаг"/>' + _flow("f2", "A", "E")
                   + '<endEvent id="E" name="Готово"/>')
        assert check_xml(xml)["timer_schedule"].applicable is False

    def test_a_plan_timer_without_a_field_is_not_charged(self):
        """Стадия плана не обязана называть срок: генератор подставляет его сам
        (`DEFAULT_TIMER_DURATION` в `core/bpmn_generator.py`), и снимать
        инвариант за то, чего в продукте не бывает, оракул не имеет права."""
        plan = {"participants": ["Пул"], "lanes": [],
                "elements": [
                    {"id": "S", "kind": "startEvent", "name": "Начало",
                     "participant": "Пул"},
                    {"id": "B1", "kind": "boundaryEvent", "name": "Просрочка",
                     "participant": "Пул", "attached_to": "A",
                     "event_definition": "timer"},
                    {"id": "A", "kind": "userTask", "name": "Шаг",
                     "participant": "Пул"},
                    {"id": "E", "kind": "endEvent", "name": "Готово",
                     "participant": "Пул"},
                ],
                "flows": [{"id": "f1", "kind": "sequence", "source": "S",
                           "target": "A"},
                          {"id": "f2", "kind": "sequence", "source": "A",
                           "target": "E"},
                          {"id": "f3", "kind": "sequence", "source": "B1",
                           "target": "E"}]}
        assert check_structure(plan)["timer_schedule"].ok

    def test_scorer_and_oracle_charge_the_same_class(self):
        """Пара `timer_without_schedule` ↔ `timer_schedule` стоит в
        `NOTATION_PAIRS`, и `eval.coverage` сверяет её на всём корпусе; здесь
        фиксируется тот же договор на одной схеме."""
        xml = _timer_scheme("<timerEventDefinition/>")
        assert status_of(xml, "timer_without_schedule") == "failed"
        assert not check_xml(xml)["timer_schedule"].ok
        legal = _timer_scheme("<timerEventDefinition><timeDuration>PT2H"
                              "</timeDuration></timerEventDefinition>")
        assert status_of(legal, "timer_without_schedule") == "passed"
        assert check_xml(legal)["timer_schedule"].ok


def _signoff_doc(count: int, gateway_after: int = 0, machine_at: int = 0,
                 kind: str = "userTask", lanes: bool = True) -> str:
    """Цепочка шагов `A1 → A2 → …` в одной дорожке (или без дорожек).

    `gateway_after` ставит развилку сразу после шага с этим номером, `machine_at`
    заменяет тип шага на автоматический: оба нужны, чтобы проверить, чем линия
    согласований разрывается, а чем нет.
    """
    ids = [f"A{i}" for i in range(1, count + 1)]
    seq = []
    for idx, node_id in enumerate(ids, 1):
        seq.append((node_id, "serviceTask" if idx == machine_at else kind))
        if idx == gateway_after:
            seq.append(("G", "exclusiveGateway"))
    body = ['<startEvent id="S" name="Начало"/>']
    prev = "S"
    for n, (node_id, node_kind) in enumerate(seq):
        name = "Дальше?" if node_kind == "exclusiveGateway" else f"Подпись {n + 1}"
        body.append(f'<{node_kind} id="{node_id}" name="{name}"/>')
        body.append(_flow(f"f{n}", prev, node_id))
        prev = node_id
    body.append('<endEvent id="E" name="Готово"/>')
    body.append(_flow("fz", prev, "E"))
    lane = ('<laneSet id="LS"><lane id="L1" name="Согласующий">'
            + "".join(f'<flowNodeRef>{i}</flowNodeRef>' for i in ids)
            + "</lane></laneSet>") if lanes else ""
    return _doc(lane + "".join(body))


class TestSignoffChainNeedsAGate:
    """Пятый бизнес-инвариант: четыре ручные подписи подряд — узкое место процесса.

    Класс принёс корпус, а не догадка: `approval_chain` ругает 37 схем из 367, а
    оракул про согласование молчал, поэтому `business/smells` и
    `improve/business_repaired_share` не могли показать, что цепочку согласований
    улучшили.
    """

    def test_four_signatures_in_a_lane_are_charged(self):
        check = check_xml(_signoff_doc(4))["signoffs_need_a_gate"]
        assert check.applicable and not check.ok
        assert check.ids == ("A1", "A2", "A3", "A4")

    def test_a_gateway_between_signatures_clears_it(self):
        """Развилка после третьей подписи — решение в цепочке есть. Проверка про
        «ни одного решения», а не про «много шагов»."""
        check = check_xml(_signoff_doc(4, gateway_after=3))["signoffs_need_a_gate"]
        assert check.applicable and check.ok

    def test_a_machine_step_leaves_nothing_to_check(self):
        """`serviceTask` между подписями — не «ещё один согласующий»: ручных
        шагов в группе становится меньше четырёх, и свойство неприменимо."""
        assert check_xml(_signoff_doc(4, machine_at=3))[
            "signoffs_need_a_gate"].applicable is False

    def test_three_signatures_are_not_applicable(self):
        assert check_xml(_signoff_doc(3))["signoffs_need_a_gate"].applicable is False

    def test_untyped_task_counts_as_a_signature(self):
        """`task` — так человек рисуется в bpmn-js: на корпусе 212 нарушений из
        213 приходятся на шаги без типа, и проверка только про `userTask` была бы
        к ним слепа."""
        assert not check_xml(_signoff_doc(4, kind="task"))[
            "signoffs_need_a_gate"].ok

    def test_lane_less_process_is_charged_not_forgiven(self):
        """Без дорожек четыре подписи идут одной линией в одном пуле — худший
        случай (исполнители неразличимы), а не «свойство неприменимо»."""
        assert not check_xml(_signoff_doc(4, lanes=False))[
            "signoffs_need_a_gate"].ok

    def test_scorer_and_oracle_charge_the_same_class(self):
        xml = _signoff_doc(4)
        assert status_of(xml, "approval_chain") == "failed"
        assert not check_xml(xml)["signoffs_need_a_gate"].ok
        legal = _signoff_doc(4, gateway_after=2)
        assert status_of(legal, "approval_chain") == "passed"
        assert check_xml(legal)["signoffs_need_a_gate"].ok


class TestLoopsHaveAGuard:
    """Безусловный цикл — повтор без критерия выхода: токен возвращается всегда.

    Зеркало правила `guarded_cycles` (28 схем корпуса его нарушают). Тесты на
    синтетике обязательны: на eval-наборе класс не встречается, поэтому
    «строгий оракул» был бы неотличим от «оракул, который перестал смотреть туда,
    где должен».
    """

    LOOP_NO_GATE = ('<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "A1")
                    + '<userTask id="A1" name="Доработать"/>' + _flow("f1", "A1", "A2")
                    + '<userTask id="A2" name="Проверить"/>' + _flow("f2", "A2", "A1")
                    + _flow("f3", "A2", "E")
                    + '<endEvent id="E" name="Готово"/>')
    LOOP_WITH_GATE = ('<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "A1")
                      + '<userTask id="A1" name="Доработать"/>'
                      + _flow("f1", "A1", "G1")
                      + '<exclusiveGateway id="G1" name="Принято?"/>'
                      + _flow("f2", "G1", "A2", condition="нет")
                      + _flow("f3", "G1", "E", condition="да")
                      + '<userTask id="A2" name="Замечания"/>' + _flow("f4", "A2", "A1")
                      + '<endEvent id="E" name="Готово"/>')

    def test_cycle_without_a_gateway_is_charged(self):
        check = check_xml(_doc(self.LOOP_NO_GATE))["loops_have_a_guard"]
        assert check.applicable and not check.ok
        assert set(check.ids) == {"A1", "A2"}

    def test_an_exclusive_gateway_inside_the_loop_clears_it(self):
        """Ветка «повторить или уйти» и есть критерий выхода."""
        check = check_xml(_doc(self.LOOP_WITH_GATE))["loops_have_a_guard"]
        assert check.applicable and check.ok

    def test_a_gateway_outside_the_loop_is_not_a_guard(self):
        """Из развилки, оставшейся за циклом, наружу не выйти: обход
        `A1 → A2 → A1` от неё не зависит."""
        xml = ('<startEvent id="S" name="Начало"/>' + _flow("f0", "S", "G0")
               + '<exclusiveGateway id="G0" name="Есть работа?"/>'
               + _flow("f1", "G0", "A1", condition="да")
               + _flow("f2", "G0", "E", condition="нет")
               + '<userTask id="A1" name="Доработать"/>' + _flow("f3", "A1", "A2")
               + '<userTask id="A2" name="Проверить"/>' + _flow("f4", "A2", "A1")
               + '<endEvent id="E" name="Готово"/>')
        check = check_xml(_doc(xml))["loops_have_a_guard"]
        assert check.applicable and not check.ok
        assert set(check.ids) == {"A1", "A2"}

    def test_scheme_without_a_cycle_is_not_applicable(self):
        xml = ('<startEvent id="S" name="Начало"/>' + _flow("f1", "S", "A")
               + '<userTask id="A" name="Шаг"/>' + _flow("f2", "A", "E")
               + '<endEvent id="E" name="Готово"/>')
        assert check_xml(_doc(xml))["loops_have_a_guard"].applicable is False

    def test_scorer_and_oracle_charge_the_same_class(self):
        assert status_of(_doc(self.LOOP_NO_GATE), "guarded_cycles") == "failed"
        assert not check_xml(_doc(self.LOOP_NO_GATE))["loops_have_a_guard"].ok
        assert status_of(_doc(self.LOOP_WITH_GATE), "guarded_cycles") == "passed"
        assert check_xml(_doc(self.LOOP_WITH_GATE))["loops_have_a_guard"].ok

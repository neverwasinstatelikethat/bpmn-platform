"""Self-check харнесса оценки без сети: оракул, метрики, детектор регрессий.

Три вещи здесь закреплены как контракт:

1. Независимый оракул различает «хороший» и «плохой» ответ модели: эталонные
   планы проходят инварианты, битые — проваливают ровно те, что в них сломаны
   (пустые пулы, развилка без схождения, событие без определения).
2. `RegressionDetector` ловит надуманное падение и не реагирует на шум в
   пределах порога — иначе baseline нельзя доверять.
3. Replay-режим проходит весь путь end-to-end и возвращает нулевой код, а live
   без ключа падает честно (код 2), а не тихо подменяет себя replay'ем.

Генерация XML и починка структуры вызываются через `eval.harness` — он резолвит
функции по имени, поэтому правки в `core/*` эти тесты не ломают.
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

from eval import harness, invariants, metrics, scenarios
from eval.invariants import (ALL_CHECKS, BUSINESS_INVARIANTS, CORE_INVARIANTS,
                             NOTATION_INVARIANTS, check_structure, check_xml,
                             summarize)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_WAREHOUSE_XML = REPO_ROOT / "reports" / "warehouse-delivery" / "process.bpmn"

# Что обязан провалить каждый «плохой» план на уровне сырого ответа модели.
# Сверяется ровно это множество: лишние провалы означают, что оракул путает
# дефекты, а отсутствие нужного — что перестал их видеть. Таблица держит
# корректностный слой (`NOTATION_INVARIANTS` + ожидания сценария): бизнес-слой
# проваливается и на исправных схемах, поэтому он заперт отдельной таблицей
# ниже, а не подмешан в эту.
EXPECTED_RAW_FAILS = {
    # Дословный ответ модели из живого прогона: 12 потоков, из них 5 ведут из
    # одного пула в другой, и `kind="message"` не назван ни разу. Это нашёл
    # новый инвариант, а не переписанный разбор: до него и скоринг, и оракул
    # видели межпуловую дугу законной (скоринг проверял существование конца, а
    # не его процесс), и то, что генератор потом переделает её в сообщение,
    # — транспорт, а не знание модели о границе процесса.
    "warehouse_delivery.live.plan": {
        "pool_has_steps", "participant_interacts", "roles_as_lanes",
        "event_definitions", "has_branching", "no_unrouted",
        "flows_within_pool"},
    "purchase_approval.loop.bad.plan": {"has_branching", "loops_have_a_guard"},
    "purchase_approval.signoffs.bad.plan": {"has_branching"},
    "purchase_approval.bad.plan": {
        "pool_has_steps", "participant_interacts", "roles_as_lanes",
        "gateway_conditions_or_default", "no_unrouted"},
    "production_incident.bad.plan": {
        "participant_interacts", "gateway_split_join", "event_definitions",
        "boundary_handled", "no_unrouted", "has_timer", "expected_participants"},
    "employee_onboarding.bad.plan": {
        "pool_has_steps", "participant_interacts", "roles_as_lanes",
        "has_branching"},
    "vehicle_reservation.bad.plan": {
        "gateway_conditions_or_default", "event_definitions", "boundary_handled",
        "no_unrouted", "has_timer"},
}

# Бизнес-дебит ЭТАЛОНА: узкие места процесса, которые есть у правильного ответа.
# Находка, а не брак гейта: из-за неё бизнес-слой и вынесен из `scenario_pass`
# (см. `invariants.deciding_checks`) — конъюнкция по нему невыполнима ни для
# какой схемы, и `pass@1` мерил бы стиль эталона вместо контура.
#
# * `loan_application.good.plan` — ответ Бюро возвращается в тот же шаг `A3`,
#   который его запросил: у принятого решения нет хозяина, доводящего историю до
#   конца (`pools_not_pingpong`).
# * `warehouse_delivery.good.plan` — `receiveTask` `A8` «зафиксировать подпись
#   получателя» ждёт без единого таймера в модели (`waits_have_sla`); сценарий
#   срока не требует, поэтому `has_timer` по нему молчит.
#
## Новое имя здесь = оракул нашёл в эталоне ещё одно узкое место. Убирать его
# молча нельзя: `business/not_worse_than_etalon` теряет опору.
EXPECTED_ETALON_BUSINESS_DEBT = {
    "loan_application.good.plan": {"pools_not_pingpong"},
    "warehouse_delivery.good.plan": {"waits_have_sla"},
}


def _fixtures(kind: str, quality: str = ""):
    out = [f for f in harness.load_fixtures(kinds=(kind,))
           if not quality or f.get("quality") == quality]
    assert out, f"нет {kind}-фикстур quality={quality!r}"
    return out


def _raw_failures(fixture, layer: str = "notation") -> set:
    """Проваленные инварианты сырого плана, по слою.

    `layer="business"` нужен отдельной таблицей: смешивать «схема бракованная» и
    «в процессе есть узкое место» в одном множестве нельзя — первая таблица
    обязана совпадать ровно, а вторая держит эталонный дебит.
    """
    scenario = scenarios.get(fixture["scenario"])
    results = check_structure(fixture["plan"], scenario.expectations())
    names = {check["name"] for check in summarize(results)["failed"]}
    business = set(BUSINESS_INVARIANTS)
    return names & business if layer == "business" else names - business


# ---------------------------------------------------------------------------
# оракул: хорошие и плохие ответы модели
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures("plan", "good"),
                         ids=lambda f: f["id"])
def test_good_plans_satisfy_all_declared_invariants(fixture):
    """Эталонный план не проваливает ни одного корректностного инварианта."""
    assert _raw_failures(fixture) == set(), (
        f"{fixture['id']} проваливает инварианты, которых в эталоне быть не должно")


@pytest.mark.parametrize("fixture", _fixtures("plan", "good"),
                         ids=lambda f: f["id"])
def test_etalon_business_debt_is_exactly_the_documented_one(fixture):
    """Бизнес-узкие места эталона заперты таблицей, а не спрятаны под ковёр.

    Вынесение бизнес-слоя из гейта — решение, которое обязано оставаться
    видимым: без этого теста гейт (`scenario_pass`, он же в сводной доле
    `quality/scheme_checks_share`) мог бы тихо перестать замечать,
    что оракул нашёл в эталоне новое узкое место, и
    `business/not_worse_than_etalon` стал бы мерить плотность по устаревшей опоре.
    """
    assert _raw_failures(fixture, "business") == EXPECTED_ETALON_BUSINESS_DEBT.get(
        fixture["id"], set())


@pytest.mark.parametrize("fixture", _fixtures("plan", "bad")
                         + _fixtures("plan", "real"),
                         ids=lambda f: f["id"])
def test_bad_plans_fail_exactly_the_broken_invariants(fixture):
    """Плохой план падает ровно на сломанных свойствах — метрика полезна только
    когда она различает классы дефектов.

    Сверяется корректностный слой: бизнес-узкие места плохой план может добавить
    сверх намеренного слома (в `production_incident.bad.plan` работа легла на
    одну дорожку), и это другой вопрос — он заперт в `test_eval_invariants.py`.
    """
    assert _raw_failures(fixture) == EXPECTED_RAW_FAILS[fixture["id"]]


def test_live_run_schema_fails_pool_and_interaction_invariants():
    """Схема живого прогона (артефакт `process.bpmn`) обязана показать те же
    провалы, что описаны в разборе: пустые пулы ролей, 8 пулов вместо
    дорожек, событие без определения."""
    xml = ARCHIVED_WAREHOUSE_XML.read_text(encoding="utf-8")
    results = check_xml(xml, scenarios.get("warehouse_delivery").expectations())
    summary = summarize(results)
    failed = {f["name"]: f for f in summary["failed"]}
    assert {"pool_has_steps", "participant_interacts", "roles_as_lanes",
            "event_definitions"} <= set(failed), summary
    assert "Кладовщик" in failed["pool_has_steps"]["ids"]
    assert "Контролёр качества" in failed["participant_interacts"]["ids"]
    assert "A11" in failed["event_definitions"]["ids"]
    # 8 пулов при 4 оправданных текстом — избыток показан списком id
    assert len(failed["roles_as_lanes"]["ids"]) == 4


def test_every_failure_comes_with_reason_and_ids():
    """Причина обязательна: без неё метрика бесполезна при разборе прогона."""
    for fixture in _fixtures("plan"):
        scenario = scenarios.get(fixture["scenario"])
        results = check_structure(fixture["plan"], scenario.expectations())
        for check in summarize(results)["failed"]:
            assert check["reason"].strip(), f"{fixture['id']}: {check['name']} без причины"


def test_unattached_boundary_and_open_split_are_detected():
    """Отдельно проверяются два дефекта, которые починка структуры может
    «заметать»: событие без attached_to и развилка без схождения."""
    plan = {
        "participants": ["ВкусВилл", "Клиент"],
        "lanes": [],
        "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Старт", "participant": "ВкусВилл"},
            {"id": "A1", "kind": "userTask", "name": "Проверить заявку", "participant": "ВкусВилл"},
            {"id": "G1", "kind": "exclusiveGateway", "name": "Одобрено?", "participant": "ВкусВилл"},
            {"id": "A2", "kind": "userTask", "name": "Выдать деньги", "participant": "ВкусВилл"},
            {"id": "A3", "kind": "userTask", "name": "Отказать", "participant": "ВкусВилл"},
            {"id": "B1", "kind": "boundaryEvent", "name": "Просрочка", "participant": "ВкусВилл"},
            {"id": "E1", "kind": "endEvent", "name": "Готово", "participant": "ВкусВилл"},
        ],
        "flows": [
            {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence"},
            {"id": "F2", "source": "A1", "target": "G1", "kind": "sequence"},
            # обе ветки уходят в разные концы и не сходятся: A3 никуда не ведёт
            {"id": "F3", "source": "G1", "target": "A2", "kind": "sequence",
             "condition": "да"},
            {"id": "F4", "source": "G1", "target": "A3", "kind": "sequence",
             "condition": "нет"},
            {"id": "F5", "source": "A2", "target": "E1", "kind": "sequence"},
        ],
    }
    results = check_structure(plan, {})
    failed = {f["name"] for f in summarize(results)["failed"]}
    assert {"gateway_split_join", "boundary_handled", "no_unrouted",
            "event_definitions", "participant_interacts"} <= failed
    # Неприменимые проверки в метрику не входят: проверять нечего. Кроме
    # `roles_as_lanes` сюда попали все пять бизнес-инвариантов: в синтетической
    # однопуловой сцене без сообщений, без второй дорожки и без ожидания нечего
    # сравнивать — это «не применимо», а не «пройдено». Два правых имени — те же
    # причины: узлы лежат в одном пуле, а потоков сообщения в плане нет.
    # `signoffs_need_a_gate` — потому что ручных шагов три, а цепочка
    # согласований начинается с четырёх; `loops_have_a_guard` — потому что
    # замкнутого обхода в этом маршруте нет.
    assert summarize(results)["not_applicable"] == [
        "flows_within_pool", "loops_have_a_guard", "message_flow_ends",
        "no_blind_rework",
        "no_overloaded_lane", "pools_not_pingpong", "roles_as_lanes",
        "signoffs_need_a_gate", "timer_schedule", "waits_have_sla"]


def test_default_flow_counts_as_condition():
    """Ветка по умолчанию (атрибут default) законна без conditionExpression."""
    plan = {
        "participants": ["ВкусВилл"],
        "lanes": [],
        "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Старт", "participant": "ВкусВилл"},
            {"id": "G1", "kind": "exclusiveGateway", "name": "Одобрено?", "participant": "ВкусВилл"},
            {"id": "A1", "kind": "userTask", "name": "Выдать", "participant": "ВкусВилл"},
            {"id": "A2", "kind": "userTask", "name": "Отказать", "participant": "ВкусВилл"},
            {"id": "E1", "kind": "endEvent", "name": "Готово", "participant": "ВкусВилл"},
        ],
        "flows": [
            {"id": "F1", "source": "S1", "target": "G1", "kind": "sequence"},
            {"id": "F2", "source": "G1", "target": "A1", "kind": "sequence",
             "condition": "да"},
            {"id": "F3", "source": "G1", "target": "A2", "kind": "sequence",
             "default": True},
            {"id": "F4", "source": "A1", "target": "E1", "kind": "sequence"},
            {"id": "F5", "source": "A2", "target": "E1", "kind": "sequence"},
        ],
    }
    results = check_structure(plan, {})
    assert results["gateway_conditions_or_default"].ok
    assert results["gateway_split_join"].ok


def test_live_mode_does_not_borrow_the_quality_of_a_fixture(monkeypatch):
    """Фикстура в live-прогоне — только слот: ответ берётся у модели, и называть
    случай «эталонным» значило бы приписывать живой схеме свойства записанного
    плана (на этом отчёт читателя водил за нос)."""
    fixture = next(f for f in _fixtures("plan", "good")
                   if f["id"] == "warehouse_delivery.good.plan")
    scenario = scenarios.get(fixture["scenario"])
    structure, notes = harness.repair_structure(fixture["plan"])
    xml = harness.generate_xml(structure)
    monkeypatch.setattr(harness, "_live_plan",
                        lambda text: (structure, notes, xml, []))
    case = harness.run_generation_case(scenario, fixture, mode="live")
    assert case.quality == "live" and case.label == "живой ответ модели"
    assert case.fixture == fixture["id"]
    replayed = harness.run_generation_case(scenario, fixture)
    assert replayed.quality == fixture["quality"]


def test_structure_and_xml_of_generated_schema_agree():
    """Починенная структура и сгенерированный XML не должны расходиться:
    расхождение — баг emission, и харнесс обязан его показать."""
    fixture = next(f for f in _fixtures("plan", "good")
                   if f["id"] == "warehouse_delivery.good.plan")
    scenario = scenarios.get(fixture["scenario"])
    case = harness.run_generation_case(scenario, fixture)
    assert case.ok, case.error
    assert case.disagreements == []
    assert case.scenario_pass is True
    # Гейт стал уже (бизнес-слой из него вынесен), и это проверяемый след:
    # узкое место эталона обязано остаться в отчёте, а не исчезнуть вместе с
    # ассертом выше.
    assert case.business_smells == ["waits_have_sla"]


def test_drift_table_reads_the_scorer_status_not_its_presence():
    """Сверка слоёв получает ВЕСЬ ответ скоринга, а не вырезку статусов.

    Проводка с `details_meta` вместо ответа превращает каждое правило, о котором
    скоринг просто молчит, в «оракул строже»: `_scorer_view` в плоской ветке
    читает `{правило: словарь}` как истину. Таблица расхождений заполняется на
    битой схеме, `business/scorer_oracle_agreement` падает, и человек идёт править
    правило вместо харнесса. Здесь оба слоя проваливают одно и то же ожидание —
    значит расхождений не должно быть вовсе."""
    fixture = next(f for f in _fixtures("plan", "good")
                   if f["id"] == "warehouse_delivery.good.plan")
    scenario = scenarios.get(fixture["scenario"])
    case = harness.run_generation_case(scenario, fixture)
    meta = case.scorer_evaluation["details_meta"]["wait_without_sla"]
    assert meta["status"] == "failed" and meta["elements"] == ["A8"]
    row = case.drift["wait_without_sla"]
    assert row["scorer"] == "failed" and row["oracle"] == "failed"
    assert row["verdict"] == invariants.AGREE, row
    assert case.drift_lines == []
    assert harness.agreement_share(case.drift) == 1.0


def test_agreement_share_ignores_rules_one_layer_does_not_have():
    """`no_data` не входит в знаменатель, а пустая таблица — это None, а не 1.0:
    доля согласия не должна раздуваться там, где сверять было нечего."""
    assert harness.agreement_share({}) is None
    rows = {"rework_loop": {"verdict": invariants.NO_DATA},
            "lane_overload": {"verdict": invariants.NOT_COMPARABLE},
            "handoff_pingpong": {"verdict": invariants.ORACLE_STRICTER}}
    assert harness.agreement_share(rows) == 0.5


# Каркас двухпуловой схемы — ровно та же схема, что собирает
# `_flow_ends_plan`: messageFlow Клиента приходит в startEvent ВкусВилла,
# это штатный способ запустить пул. Поток обязан лежать внутри `process`,
# иначе оракул его не увидит.
#
# Граничный таймер здесь с хронометражем: `timer_schedule` сняла бы каркас как
# битый по другому классу (пустой `timerEventDefinition` — мёртвый срок), а этот
# файл проверяет концы потоков. Для мёртвого таймера есть своя пара тестов в
# `tests/test_eval_invariants.py`.
FLOW_ENDS_XML = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
             id="Definitions_flow_ends">
  <collaboration id="Collaboration_1">
    <participant id="Pool_wv" name="ВкусВилл" processRef="Process_wv"/>
    <participant id="Pool_client" name="Клиент" processRef="Process_client"/>
    <messageFlow id="MF1" sourceRef="S2" targetRef="S1"/>
  </collaboration>
  <process id="Process_wv" name="ВкусВилл" isExecutable="true">
    <startEvent id="S1" name="Поступил заказ"/>
    <userTask id="A1" name="Собрать заказ"/>
    <boundaryEvent id="B1" name="Просрочка" attachedToRef="A1">
      <timerEventDefinition><timeDuration>PT1H</timeDuration></timerEventDefinition>
    </boundaryEvent>
    <endEvent id="E1" name="Заказ выдан"/>
    <sequenceFlow id="F1" sourceRef="S1" targetRef="A1"/>
    <sequenceFlow id="F2" sourceRef="A1" targetRef="E1"/>
    <sequenceFlow id="F3" sourceRef="B1" targetRef="A1"/>
    {extra}
  </process>
  <process id="Process_client" name="Клиент" isExecutable="true">
    <startEvent id="S2" name="Заказ оформлен"/>
    <userTask id="A2" name="Дождаться сборки"/>
    <endEvent id="E2" name="Заказ получен"/>
    <sequenceFlow id="F4" sourceRef="S2" targetRef="A2"/>
    <sequenceFlow id="F5" sourceRef="A2" targetRef="E2"/>
  </process>
</definitions>"""


def _xml_with(extra_flow: str = "") -> str:
    return FLOW_ENDS_XML.format(extra=extra_flow)


def _flow_ends_plan(*extra_flows):
    """Структурный двойник `FLOW_ENDS_XML` — чтобы один и тот же дефект
    проверялся на обеих точках входа."""
    return {
        "participants": ["ВкусВилл", "Клиент"],
        "lanes": [],
        "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Поступил заказ", "participant": "ВкусВилл"},
            {"id": "A1", "kind": "userTask", "name": "Собрать заказ", "participant": "ВкусВилл"},
            {"id": "B1", "kind": "boundaryEvent", "name": "Просрочка", "participant": "ВкусВилл",
             "attached_to": "A1", "event_definition": "timer"},
            {"id": "E1", "kind": "endEvent", "name": "Заказ выдан", "participant": "ВкусВилл"},
            {"id": "S2", "kind": "startEvent", "name": "Заказ оформлен", "participant": "Клиент"},
            {"id": "A2", "kind": "userTask", "name": "Дождаться сборки", "participant": "Клиент"},
            {"id": "E2", "kind": "endEvent", "name": "Заказ получен", "participant": "Клиент"},
        ],
        "flows": [
            {"id": "MF1", "kind": "message", "source": "S2", "target": "S1"},
            {"id": "F1", "kind": "sequence", "source": "S1", "target": "A1"},
            {"id": "F2", "kind": "sequence", "source": "A1", "target": "E1"},
            {"id": "F3", "kind": "sequence", "source": "B1", "target": "A1"},
            {"id": "F4", "kind": "sequence", "source": "S2", "target": "A2"},
            {"id": "F5", "kind": "sequence", "source": "A2", "target": "E2"},
            *extra_flows,
        ],
    }


def test_repaired_share_measures_only_the_packages_own_repair():
    """`improve/pass@1` мерит итоговую схему и наследует провалы генерации;
    `repaired_share` отвечает за то, что в зоне ответственности пакета: убрал ли
    пакет дефекты, которые в базовой схеме были."""
    broken = _xml_with('<sequenceFlow id="FX" sourceRef="E1" targetRef="S1"/>')
    case = harness.ImproveCase(scenario="s", fixture="f", label="", quality="live",
                               mode="replay", repeat=0)
    case.checks_before = check_xml(broken, {})
    case.checks_after = check_xml(broken, {})
    assert case.repaired_share == 0.0            # ничего не починено
    case.checks_after = check_xml(_xml_with(), {})
    assert case.repaired_share == 1.0            # дефект базовой схемы убран
    case.checks_before = check_xml(_xml_with(), {})
    assert case.repaired_share is None           # чинить было нечего — не считаем


def test_legal_flow_endpoints_pass_on_both_entries():
    """Каркас легален: в startEvent приходит только messageFlow, а у endEvent
    исходящего управляющего потока нет."""
    for results in (check_structure(_flow_ends_plan(), {}), check_xml(_xml_with(), {})):
        check = results["flow_ends_legal"]
        assert check.applicable and check.ok, check.reason


def test_sequence_flow_into_start_or_boundary_event_fails():
    """Граничное событие запускается хозяином по attachedToRef, а в старт
    поток управления не входит — оракул обязан видеть оба дефекта."""
    check = check_structure(_flow_ends_plan(
        {"id": "FBAD1", "kind": "sequence", "source": "A2", "target": "S1"},
        {"id": "FBAD2", "kind": "sequence", "source": "A1", "target": "B1"},
    ), {})["flow_ends_legal"]
    assert check.applicable and not check.ok
    assert check.ids == ("FBAD1", "FBAD2")
    assert "startEvent" in check.reason and "граничное событие" in check.reason


def test_sequence_flow_out_of_end_event_fails_xml():
    """Поток «endEvent → задача» доходит до bpmn-js — в XML его тоже видно."""
    check = check_xml(_xml_with(
        '<sequenceFlow id="FBAD" sourceRef="E1" targetRef="A1"/>'),
        {})["flow_ends_legal"]
    assert check.applicable and not check.ok
    assert check.ids == ("FBAD",)
    assert "endEvent" in check.reason


def test_flow_without_id_is_reported_by_its_endpoints():
    """Поток без id — не повод потерять нарушение: пару концов отдаём как id
    (тот же приём, что в `gateway_split_join`)."""
    check = check_structure(_flow_ends_plan(
        {"kind": "sequence", "source": "E1", "target": "A1"}),
        {})["flow_ends_legal"]
    assert not check.ok and check.ids == ("E1->A1",)


def test_dangling_flow_endpoint_is_left_to_no_unrouted():
    """Ссылка вникуда — забота `no_unrouted`: одно и то же нарушение дважды
    в метрику не попадает."""
    results = check_structure(_flow_ends_plan(
        {"id": "FGHOST", "kind": "sequence", "source": "E1", "target": "нет-такого-id"}), {})
    assert results["flow_ends_legal"].applicable
    assert results["flow_ends_legal"].ids == ()


def test_flow_ends_legal_is_declared_for_both_entry_points():
    """Имя — в списке инвариантов (из него строится `pass@1/<имя>`) и в обеих
    сводках: структура и XML измеряют одно и то же."""
    assert "flow_ends_legal" in CORE_INVARIANTS
    assert "flow_ends_legal" in ALL_CHECKS
    xml_results = check_xml(_xml_with(
        '<sequenceFlow id="FBAD" sourceRef="A1" targetRef="S1"/>'), {})
    for summary in (summarize(check_structure(_flow_ends_plan(
            {"id": "FBAD", "kind": "sequence", "source": "A1", "target": "S1"}), {})),
            summarize(xml_results)):
        failed = {f["name"]: f for f in summary["failed"]}
        assert "flow_ends_legal" in failed, summary
        assert failed["flow_ends_legal"]["ids"] == ["FBAD"]


def test_dump_schemes_pairs_the_improvement_with_its_base(tmp_path):
    """Пара before/after плюс отчёт: «пакет применился и стало лучше» офлайн не
    проверяется, а diff двух схем читается без повторного прогона."""
    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery")
    out = tmp_path / "улучшение"
    written = harness.dump_schemes(report, out)
    improve = [p for p in written if p.name.startswith("improve_")]
    assert improve, "у сценария есть improve-фикстура — значит есть и пара файлов"
    stems = {p.name.rsplit("_", 1)[0] for p in improve if p.suffix == ".bpmn"}
    assert len(stems) == 1, stems
    stem = stems.pop()
    before = out / f"{stem}_before.bpmn"
    after = out / f"{stem}_after.bpmn"
    verdict = stem.rsplit("_", 1)[1]
    assert verdict in ("pass", "fail", "revert", "regress"), stem
    assert before.exists() and after.exists()
    assert before.read_text(encoding="utf-8") != after.read_text(encoding="utf-8")
    dump = json.loads((out / f"{stem}_report.json").read_text(encoding="utf-8"))
    case = report.improve_cases[0]
    assert dump["fixture"] == case.fixture
    for key in ("applied", "skipped", "package_reverted", "rules_regressed",
                "retry_attempted", "retry_closed", "noop_rows", "unmeasured",
                "checks_before", "checks_after", "score_before", "score_after"):
        assert key in dump, key


def test_dump_schemes_names_the_worst_improvement_outcome():
    """Вердикт в имени файла — не украшение: откатанный пакет и пакет,
    задевший проходившее правило, разбирают первым."""
    case = harness.ImproveCase(scenario="s", fixture="f", label="", quality="live",
                               mode="replay")
    assert harness.improve_verdict(case) == "fail"      # схема не померена
    case.checks_after = check_xml(_xml_with(), {})
    assert harness.improve_verdict(case) == "pass"
    case.rules_regressed = {"event_types": "passed->failed"}
    assert harness.improve_verdict(case) == "regress"
    case.package_reverted = "пакет создал цикл без защищённого выхода"
    assert harness.improve_verdict(case) == "revert"


# ---------------------------------------------------------------------------
# метрики и детектор регрессий
# ---------------------------------------------------------------------------


def test_percentiles_and_coefficient_of_variation():
    values = [85, 87, 90, 94]
    assert metrics.p50(values) == pytest.approx(88.5)
    assert metrics.p95(values) == pytest.approx(93.4)
    assert metrics.coefficient_of_variation([80, 80, 80]) == pytest.approx(0.0)
    # один повтор — разброса нет, но и считать нечего: честный None
    assert metrics.coefficient_of_variation([80]) is None
    assert metrics.coefficient_of_variation([]) is None
    # относительная величина: 4 балла на 100 и 4 балла на 40 — разные сигналы
    assert metrics.coefficient_of_variation([96, 100]) < \
        metrics.coefficient_of_variation([36, 40])


def test_suite_returns_mean_and_raw_values():
    suite = metrics.EvaluationSuite()
    suite.metric("ok", lambda case: float(case["ok"]))
    suite.metric("score", lambda case: case["score"] if case["score"] is not None else None)
    result = suite.run([
        {"name": "a", "payload": {"ok": True, "score": 90}},
        {"name": "b", "payload": {"ok": False, "score": None}},
        {"name": "c", "payload": {"ok": True, "score": 80}},
    ])
    assert result.metrics["ok"].mean == pytest.approx((1 + 0 + 1) / 3)
    # None не попадает в знаменатель: иначе «неприменимое» выглядело бы провалом
    assert result.metrics["score"].n == 2
    assert result.metrics["score"].mean == pytest.approx(85)
    assert result.metrics["score"].values == [90, None, 80]
    assert result.per_example["b"]["ok"] == 0.0


def test_regression_detector_catches_drop_and_ignores_noise():
    # по умолчанию «выше = лучше»; задержка и число правок передаются явно —
    # так их задаёт harness.LOWER_IS_BETTER
    detector = metrics.RegressionDetector(directions={"latency_ms": metrics.LOWER})
    baseline = {"pass@1/scenario": 0.80, "score": 90.0, "latency_ms": 100.0}
    # падение на 12.5% и рост задержки на 20% — регрессии; шум в 3% — нет
    current = {"pass@1/scenario": 0.70, "score": 88.0, "latency_ms": 120.0}
    names = [r.name for r in detector.compare(baseline, current)]
    assert names == ["pass@1/scenario", "latency_ms"]

    quiet = {"pass@1/scenario": 0.78, "score": 87.5, "latency_ms": 104.0}
    assert detector.compare(baseline, quiet) == []


def test_regression_detector_respects_direction_and_threshold():
    detector = metrics.RegressionDetector(
        directions={"repairs_per_scheme": metrics.LOWER})
    assert detector.compare({"repairs_per_scheme": 5.0}, {"repairs_per_scheme": 8.0})
    assert not detector.compare({"repairs_per_scheme": 5.0}, {"repairs_per_scheme": 4.0})
    strict = metrics.RegressionDetector(threshold=0.5)
    assert not strict.compare({"score": 100.0}, {"score": 60.0})
    # Нулевой baseline у метрики «меньше = лучше»: относительная доля
    # невозможна, но отлёт от нуля — ухудшение, и именно его гейт обязан
    # увидеть. Метрика без объявленного направления тут не показательна: без
    # направления у неё «выше нуля = лучше», и проверка закрепляла обратное.
    off_zero = detector.compare({"repairs_per_scheme": 0.0},
                                {"repairs_per_scheme": 3.0})
    assert [r.name for r in off_zero] == ["repairs_per_scheme"]
    assert detector.compare({"repairs_per_scheme": 0.0},
                            {"repairs_per_scheme": 0.0}) == []


def test_zero_baseline_respects_direction_not_its_inverse():
    lower = metrics.RegressionDetector(directions={"err": metrics.LOWER})
    assert [r.name for r in lower.compare({"err": 0.0}, {"err": 0.5})] == ["err"]
    higher = metrics.RegressionDetector(directions={"fixed": metrics.HIGHER})
    assert higher.compare({"fixed": 0.0}, {"fixed": 0.5}) == []
    assert [r.name for r in higher.compare({"fixed": 0.0}, {"fixed": -0.5})] == ["fixed"]


def test_unverified_lists_metrics_the_baseline_never_saw():
    """`compare` идёт по ключам baseline — всё, что завелось позже слепка (или
    что слепок не померил), выпадает из сверки. `unverified` называет это явно,
    а не молчит: иначе «регрессий нет» читается как «сверили всё»."""
    detector = metrics.RegressionDetector()
    assert detector.compare({"score": 90.0},
                            {"score": 90.0, "improve/new": 0.0}) == []
    assert detector.unverified({"score": 90.0},
                               {"score": 90.0, "improve/new": 0.0}) == ["improve/new"]
    # null в baseline против померенного сейчас — тоже «сверки не было»
    assert detector.unverified({"a": 1.0, "old": None},
                               {"a": 1.0, "old": 0.2, "b/новая": 0.5}) == \
        ["b/новая", "old"]
    # Обратное направление: метрику из прогона сняли, ключ остался в слепке,
    # `compare` её не находит — и без этой строки удаление четырёх метрик
    # улучшения выглядело бы как «всё сверили, регрессий нет».
    assert detector.unverified({"a": 1.0, "improve/applied_share": 0.75},
                               {"a": 1.0, "improve/op_acceptance": 0.5}) == \
        ["improve/applied_share", "improve/op_acceptance"]
    # не померена в этом прогоне — «нет данных», а не «baseline старее»
    assert detector.unverified({"a": 1.0, "cv": None},
                               {"a": 1.0, "cv": None}) == []
    assert detector.unverified({}, {"a": 1.0}) == []
    assert detector.unverified(None, {"a": 1.0}) == []
    assert detector.unverified({"a": 1.0}, None) == []


def test_regression_detector_skips_unmeasured_metrics():
    """Подмножество сценариев без improve-кейсов даёт None в метрике. Это «нет
    данных»: сравнение не должно ни падать, ни молча объявлять ухудшение."""
    detector = metrics.RegressionDetector()
    assert detector.compare({"improve/defects_repaired": 0.0},
                            {"improve/defects_repaired": None}) == []
    assert detector.compare({"pass@1/has_timer": None},
                            {"pass@1/has_timer": 0.4}) == []


def test_a_throttled_live_run_says_it_is_not_comparable():
    """Прогон, где провайдер отбил часть кейсов 429, меняет выборку, а не
    качество: доли инвариантов считаются по уцелевшим, и без этой строки
    «has_timer 0.667 → 1.0» читается как улучшение (прогон #54 — 5 кейсов из 24)."""
    from types import SimpleNamespace as NS
    cases = [NS(ok=True, scenario="warehouse_delivery", repeat=0),
             NS(ok=False, scenario="vehicle_reservation", repeat=1),
             NS(ok=False, scenario="support_ticket", repeat=2)]
    note = harness.coverage_note(cases, "live")
    assert "собрано 1 схем из 3" in note
    assert "vehicle_reservation r1" in note
    assert "не пара ни одному предыдущему" in note
    assert harness.coverage_note(cases, "replay") == ""
    assert harness.coverage_note(cases[:1], "live") == ""


def test_replay_run_reports_no_coverage_warning(tmp_path):
    """В replay транспорт не участвует — пустая строка обязана оставаться
    пустой, иначе предупреждение обесценится."""
    report = harness.run(mode="replay", scenarios_spec="support_ticket")
    assert report.coverage_note == ""
    text = harness.render_table(report)
    assert "ПОЛНОТА ПРОГОНА" not in text
    lost = report.cases[0]
    report.cases = [harness.GenCase(**{**lost.__dict__, "ok": False,
                                       "error": "429"})]
    report.coverage_note = harness.coverage_note(report.cases, "live")
    assert "ПОЛНОТА ПРОГОНА" in harness.render_table(report)


def test_dump_schemes_writes_readable_names_with_the_verdict(tmp_path):
    report = harness.run(mode="replay", scenarios_spec="support_ticket")
    written = harness.dump_schemes(report, tmp_path / "схемы")
    assert written, "прогон обязан оставить схемы для разбора глазами"
    names = sorted(p.name for p in written)
    assert any(n.endswith("_pass.bpmn") or n.endswith("_fail.bpmn")
               for n in names), names
    # Разбор начинается с провалов — по имени файла видно, что читать первым.
    failed = [c for c in report.cases if c.xml and not c.scenario_pass]
    if failed:
        assert any("_fail.bpmn" in n for n in names)
    assert (tmp_path / "схемы").is_dir()
    assert written[0].read_text(encoding="utf-8").lstrip().startswith("<?xml")


def test_plan_dump_carries_the_flows_that_explain_a_hidden_split(tmp_path):
    """Без потоков в дампе офлайн неотличимо: «развилку спрятала модель» или
    «контур видел различимые ветки и не вставил шлюз». Выбор между правкой
    промпта и правкой починки по XML не делается — прогон #52 на `has_branching`
    встал ровно на этом вопросе."""
    report = harness.run(mode="replay", scenarios_spec="support_ticket")
    harness.dump_schemes(report, tmp_path / "планы")
    plans = sorted((tmp_path / "планы").glob("*.plan.json"))
    assert plans, "рядом со схемой обязан лежать план"
    dumps = [json.loads(p.read_text(encoding="utf-8")) for p in plans]
    assert all("flows" in d for d in dumps)
    with_flows = next(d for d in dumps if d["flows"])
    assert {"id", "source", "target", "kind", "condition"} <= set(
        with_flows["flows"][0])


# ---------------------------------------------------------------------------
# прогоны харнесса
# ---------------------------------------------------------------------------


def test_replay_generation_produces_metrics_and_scores():
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    flat = report.metrics_flat()
    assert report.mode == "replay"
    assert len(report.cases) == len(_fixtures("plan"))
    assert flat["pass@1/pool_has_steps"] is not None
    assert 0.0 <= flat["quality/scheme_checks_share"] <= 1.0
    # в replay генерация детерминирована и модели не дёргает
    assert flat["llm_retries"] == 0.0
    assert all(0 <= case.score <= 100 for case in report.cases if case.score is not None)
    assert report.generation.metrics["score"].n == len(report.cases)


def test_repeat_produces_spread_stats():
    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=2)
    assert report.spread["runs"] == 4.0
    assert report.spread["score_cv_per_case"] == pytest.approx(0.0)
    assert report.spread["score_p50"] is not None


def test_improvement_replay_metrics():
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    cases = {c.key: c for c in report.improve_cases}
    flat = report.metrics_flat()
    # Снятые метрики не вернутся «для совместимости»: они считали строки отчёта
    # вместо работы и выводили потребность в повторе из стадий отказов.
    assert not {"improve/applied_share", "improve/skipped_share",
                "improve/retry_needed_share",
                "improve/no_regression"} & set(flat)
    live = cases["warehouse_delivery/warehouse_delivery.live.improve"]
    assert live.ok and live.retry_attempted is True
    # Повтор был и не закрыл ни одного отказа: это 0, а не «повтора не было»
    # (старая метрика на этом же кейсе врала в обе стороны сразу).
    assert live.retry_closed == 0
    assert harness._retry_gain(live) == 0.0
    assert live.package_reverted == "" and live.rules_regressed == {}
    assert live.noop_rows == 0 and live.truncated_operations == 0
    assert 0 < harness._op_acceptance(live) < 1
    assert live.score_after is not None
    # «Хороший» пакет применяется целиком и не портит схему
    clean = cases["production_incident/production_incident.good.improve"]
    assert harness._op_acceptance(clean) == 1.0
    assert clean.retry_attempted is False
    # Повтора не было — метрики повтора не имеют значения, и ноль в выборку
    # не идёт (иначе «контур не звал повтор» выглядел бы как «повтор бессилен»).
    assert harness._retry_gain(clean) is None
    assert harness._repeat_rejection_share(clean) is None
    assert harness._defects_introduced(clean) == 0.0
    assert clean.score_delta >= 0
    # Разбор отказа улучшения читается из отчёта: без базовой схемы прогон не
    # отличает унаследованный от генерации провал от того, что принёс пакет,
    # а без списка применённых операций не видно, какая правка двигала балл.
    dump = clean.as_dict()
    assert dump["checks_before"] and dump["applied_details"]
    assert dump["summary_before"]["applicable"] >= 1
    assert clean.base_xml and "base_xml" not in dump   # XML живёт в файле, не в JSON
    for key in ("retry_attempted", "retry_closed", "package_reverted",
                "rules_regressed", "noop_rows", "truncated_operations",
                "op_acceptance", "defects_introduced", "unmeasured"):
        assert key in dump, key


# ---------------------------------------------------------------------------
# контур применения в replay: тот же гарант, что в продукте
# ---------------------------------------------------------------------------

# Линейный маршрут — ровно та схема, что в `tests/test_bpmn_edits.py`: два
# шага легальны поодиночке и вместе замыкают L_a → new_X → L_b → L_a в цикл без
# защищённого выхода.
GUARANTEE_XML = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_lin">
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

CYCLING_OPS = [
    {"op": "add_task", "id": "new_X", "name": "Проверить пломбы",
     "task_type": "userTask", "after": "L_a", "to": "L_b"},
    {"op": "connect", "source": "L_b", "target": "L_a"},
]

TWICE_THE_SAME_DOC = [
    {"op": "add_documentation", "id": "L_a",
     "text": "Собрать заказ по накладной, сверив сроки"},
    {"op": "add_documentation", "id": "L_a",
     "text": "Собрать заказ по накладной, сверив сроки"},
]


def _base_gencase(xml: str) -> harness.GenCase:
    return harness.GenCase(scenario="purchase_approval", fixture="base",
                           label="", quality="live", mode="replay", ok=True,
                           xml=xml)


def _improve(operations, retry=None, xml=GUARANTEE_XML, **overrides):
    """Кейс улучшения на готовой схеме: фикстура тут — только план операций."""
    fixture = {"id": "fx", "scenario": "purchase_approval", "label": "план",
               "quality": "live", "operations": operations}
    if retry:
        fixture["retry_operations"] = retry
    fixture.update(overrides)
    return harness.run_improvement_case(scenarios.get("purchase_approval"),
                                        fixture, _base_gencase(xml))


def test_replay_applies_operations_through_the_production_guarantor(monkeypatch):
    """Replay обязан играть тот же гарант, что и продукт.

    Своя короткая копия «применить → починить» стоила харнессу целого класса
    слепых зон: откат пакета по циклу без выхода возвращал базовую схему с
    полным `applied`, и метрика рапортовала успех на схеме, байт в байт равной
    базе. Здесь проверяется, что харнесс зовёт `apply_and_guarantee` с
    продуктовскими предикатами, а не своими.
    """
    from core import bpmn_edits, llm_improve

    seen: dict = {}
    real = bpmn_edits.apply_and_guarantee

    def spy(xml_text, operations, reject=None, prune=None):
        seen["reject"], seen["prune"] = reject, prune
        return real(xml_text, operations, reject=reject, prune=prune)

    monkeypatch.setattr(bpmn_edits, "apply_and_guarantee", spy)
    unmeasured: list = []
    out, report = harness._apply_package(GUARANTEE_XML, [dict(o) for o in
                                                         TWICE_THE_SAME_DOC],
                                         "plan", unmeasured)
    assert unmeasured == []
    assert seen["prune"] is llm_improve._prune_stranded
    # Гарант по циклу — фабричный `_cycle_reject` на СХЕМЕ ЭТОГО РАУНДА: база
    # легальна, а замкнутый маршрут обязан дать отказ с причиной.
    assert seen["reject"](GUARANTEE_XML) is None
    cycled, _ = bpmn_edits.apply_operations(GUARANTEE_XML, [dict(o) for o in
                                                            CYCLING_OPS])
    rejection = seen["reject"](cycled)
    assert rejection is not None and "цикл без защищённого выхода" in \
        rejection.reason
    assert out != GUARANTEE_XML and report["noop_rows"] == 1
    # Стадию получает каждая строка (продукт размечает только отказы — строка
    # `applied` и так из того раунда, откуда её взяли), `reapplied` — только
    # отказ: по нему повтор узнаёт закрытую правку.
    assert all(row["stage"] == "plan" for row in report["skipped"] + report["applied"])
    assert all(not row["reapplied"] for row in report["skipped"])
    assert all("reapplied" not in row for row in report["applied"])


def test_reverted_package_leaves_no_applied_rows():
    """Откат обязан быть сверен с отчётом: `applied` пуст, каждая снятая правка
    уехала в `skipped`, и `improve/op_acceptance` читает 0.0, а не 0.67."""
    case = _improve(CYCLING_OPS)
    assert case.package_reverted and "цикл" in case.package_reverted
    assert case.applied == [] and case.xml_after == GUARANTEE_XML
    assert {s["op"] for s in case.skipped} == {"add_task", "connect"}
    assert all(s["stage"] == "plan" for s in case.skipped)
    assert harness._op_acceptance(case) == 0.0
    assert harness._package_revert_share(case) == 1.0
    # Регресс правил при откате не приписывается пакету: сравнивать не с чем.
    assert case.rules_regressed == {}
    # Продукт на пустом `applied` поднимает ImprovementError — и харнесс зовёт
    # такой кейс несобранным, а не «улучшение применилось».
    assert case.ok is False
    # `noop_rows` считается до отката: в сверенном отчёте строк нет, и числитель
    # acceptance не имеет права уехать в минус.
    mixed = _improve(TWICE_THE_SAME_DOC + CYCLING_OPS)
    assert mixed.noop_rows == 1 and mixed.applied == []
    assert harness._op_acceptance(mixed) == 0.0
    assert harness._noop_share(mixed) is None


def test_noop_applied_rows_do_not_count_as_work():
    """Строка «добавлено не было» — не улучшение: вторая копия той же
    документации применяется в ноль, и старая `applied_share` считала её
    успехом (1.0), новая acceptance — 0.5."""
    case = _improve(TWICE_THE_SAME_DOC)
    assert len(case.applied) == 2 and not case.skipped
    assert case.noop_rows == 1
    assert harness._op_acceptance(case) == 0.5
    assert harness._noop_share(case) == 0.5


def test_live_case_reads_facts_instead_of_guessing_the_retry(monkeypatch):
    """Старый вывод `retried = any(stage == "retry")` давал ложный ноль ровно
    там, где повтор отработал без новых отказов: оркестратор закрывает пропуск
    флагом `reapplied`, и стадия у строки остаётся «план». Теперь live читает
    факты отчёта."""
    report = {
        "status": "partial",
        "applied": [{"op": "add_task", "id": "new_A"}],
        "skipped": [{"op": "connect", "source": "A", "target": "B",
                     "stage": "plan", "reapplied": True, "reason": "нет цели"},
                    {"op": "add_lane", "name": "Кладовщик", "stage": "plan",
                     "reapplied": False, "reason": "дорожка уже есть",
                     "duplicate": True}],
        "repair_notes": ["узел new_A остался без потоков"],
        "retry_attempted": True, "retry_closed": 1,
        "package_reverted": "", "rules_regressed": {"documentation": "passed->failed"},
        "noop_rows": 0, "truncated_operations": 3,
    }
    monkeypatch.setattr(
        harness, "_live_improve",
        lambda xml, prompt: (xml, report, 2))
    case = harness.run_improvement_case(
        scenarios.get("purchase_approval"),
        {"id": "fx", "label": "", "quality": "live",
         # претензия заявлена и совпадает с тем, что схема проваливает: иначе
         # замечание о неразмеченной фикстуре попадёт в `unmeasured` вместо
         # фактов отчёта, а проверяется здесь именно отчёт
         "targets": sorted(_failing_invariants(GUARANTEE_XML))},
        _base_gencase(GUARANTEE_XML), mode="live")
    assert case.retry_attempted is True and case.retry_closed == 1
    assert harness._retry_gain(case) == 1.0
    assert harness._repeat_rejection_share(case) == 0.5
    assert harness._plan_truncated_share(case) == 1.0
    assert harness._rules_regressed_share(case) == 1.0
    assert harness._package_revert_share(case) == 0.0
    assert case.repair_notes == ["узел new_A остался без потоков"]
    assert case.truncated_operations == 3 and case.noop_rows == 0
    # live-отчёт не знает, чего харнесс не доиграл: список пуст, а не «всё хорошо»
    assert case.unmeasured == []


def test_missing_guarantor_is_recorded_not_raised(monkeypatch):
    """Имя живого кода может уехать: харнесс обязан записать кейс несобранным,
    а не падать и не подменять себе контур локальной копией."""
    from core import bpmn_edits

    monkeypatch.setattr(bpmn_edits, "apply_and_guarantee", None)
    case = _improve(TWICE_THE_SAME_DOC)
    assert "apply_and_guarantee" in case.error
    assert not case.ok and not case.applied
    suite = harness.build_improvement_suite().run(
        [{"name": case.key, "payload": case}])
    for name, result in suite.metrics.items():
        if name == "improve/error_share":
            continue
        assert result.n == 0, f"{name} посчитана по кейсу без данных"


def test_missing_reject_predicate_leaves_the_revert_fact_unknown(monkeypatch):
    """«Гарант не игрался» и «гарант не отказал» — разные состояния: первое не
    имеет права читаться как второе."""
    from core import llm_improve

    monkeypatch.setattr(llm_improve, "_cycle_reject", None)
    case = _improve(CYCLING_OPS)
    assert case.package_reverted is None
    assert harness._package_revert_share(case) is None
    assert any("_cycle_reject" in note for note in case.unmeasured)
    # без гаранта пакет доживает до отчёта с applied — ровно та ошибка,
    # из-за которой харнесс и переехал на продуктовский гарант
    assert case.applied and case.xml_after != GUARANTEE_XML


def test_retry_closes_a_refusal_only_on_the_same_element():
    """`retry_closed` — сверка идентичностей, а не подсчёт строк повтора:
    первый раунд отказал в вставке без `after`, повтор вставил тот же узел
    правильно."""
    insert_bad = [{"op": "add_task", "id": "new_doc", "name": "Оформить",
                   "task_type": "userTask"}]
    insert_good = [{"op": "add_task", "id": "new_doc", "name": "Оформить",
                    "task_type": "userTask", "after": "L_a", "to": "L_b"}]
    case = _improve(insert_bad, retry=insert_good)
    assert case.retry_attempted is True and case.retry_closed == 1
    assert harness._retry_gain(case) == 1.0
    assert all(s["stage"] == "retry" for s in case.skipped
               if s["op"] != "add_task")
    # Другой элемент за «ту же правку» не засчитывается: повтор приносит
    # connect, которого первый раунд не просил.
    other = _improve(insert_bad, retry=[{"op": "connect", "source": "L_b",
                                         "target": "L_e"}])
    assert other.retry_attempted is True
    assert other.retry_closed == 0


def test_repeat_rejection_share_only_counts_verbatim_refusals():
    case = harness.ImproveCase(scenario="s", fixture="f", label="",
                               quality="live", mode="live",
                               retry_attempted=True,
                               skipped=[{"op": "add_task", "id": "X",
                                         "stage": "plan"},
                                        {"op": "add_task", "id": "X",
                                         "stage": "retry", "duplicate": True},
                                        {"op": "connect", "source": "A",
                                         "stage": "retry"}])
    assert harness._repeat_rejection_share(case) == pytest.approx(1 / 3)
    case.retry_attempted = False
    assert harness._repeat_rejection_share(case) is None
    assert case.repeat_rejections is None


def test_defects_introduced_sees_what_no_regression_hid():
    """Один инвариант починен, другой сломан, дельта балла 0 — `no_regression`
    на этом молчал, счётчик упавших видит обе половины."""
    broken = _xml_with('<sequenceFlow id="FX" sourceRef="E1" targetRef="S1"/>')
    case = harness.ImproveCase(scenario="s", fixture="f", label="",
                               quality="live", mode="replay")
    case.checks_before = check_xml(broken, {})
    case.checks_after = check_xml(broken, {})
    # Ничего не изменилось — и нечего приписывать пакету.
    assert harness._defects_introduced(case) == 0.0
    case.checks_before = check_xml(_xml_with(), {})
    assert harness._defects_introduced(case) >= 1.0
    case.checks_before = {}
    assert harness._defects_introduced(case) is None


def test_improve_metrics_do_not_invent_zero_for_broken_cases():
    """Сломанный кейс не участвует в долях: ноль в `op_acceptance` — это
    вердикт контуру, а у кейса без данных вердикта нет."""
    case = harness.ImproveCase(scenario="s", fixture="f", label="", quality="",
                               mode="live", error="нет схемы")
    suite = harness.build_improvement_suite()
    result = suite.run([{"name": case.key, "payload": case}])
    for name, metric in result.metrics.items():
        if name == "improve/error_share":
            continue
        assert metric.n == 0, f"{name} посчитана по кейсу без данных (n={metric.n})"
        assert metric.mean is None
    assert result.metrics["improve/error_share"].mean == 1.0


def test_improve_case_phrase_says_whether_the_retry_ran():
    case = harness.ImproveCase(scenario="s", fixture="f", label="", quality="",
                               mode="live")
    assert case.retry_phrase() == "повтор не померен"
    case.retry_attempted = False
    assert case.retry_phrase() == "повтор не был вызван"
    case.retry_attempted, case.retry_closed = True, 2
    case.skipped = [{"op": "connect", "stage": "retry", "duplicate": True}]
    assert case.retry_phrase() == "повтор закрыл 2 отказов, 1 вернул дословно"


def test_generation_error_is_a_result_not_a_crash():
    """`GenerationError` — валидный исход сцены: харнесс его записывает и
    продолжает считать метрики."""
    scenario = scenarios.get("product_return")
    broken = {"id": "broken", "scenario": scenario.id, "quality": "bad",
              "label": "план без шагов", "kind": "plan",
              "plan": {"participants": ["ВкусВилл"], "elements": [], "flows": []}}
    case = harness.run_generation_case(scenario, broken)
    assert case.ok is False
    assert case.error
    assert case.scenario_pass is None
    suite = harness.build_generation_suite(["pool_has_steps"])
    result = suite.run([{"name": case.key, "payload": case}])
    assert result.metrics["generation_error_share"].mean == 1.0


def test_unknown_plan_fields_do_not_break_the_loop():
    """Генератор меняется прямо сейчас: неизвестные поля плана не должны
    уронить харнесс."""
    scenario = scenarios.get("warehouse_delivery")
    fixture = next(f for f in _fixtures("plan", "good")
                   if f["id"] == "warehouse_delivery.good.plan")
    plan = json.loads(json.dumps(fixture["plan"]))
    plan["elements"][1]["event_definition"] = "timer"
    plan["elements"][1]["attached_to"] = "нет-такого-id"
    plan["elements"][1]["совершенно_новое_поле"] = {"вложенный": ["объект"]}
    plan["flows"][3]["default"] = True
    plan["unknown_section"] = [1, 2, 3]
    case = harness.run_generation_case(scenario, dict(fixture, plan=plan))
    assert case.error == "", case.error
    assert case.checks_xml


def test_report_lets_a_participant_failure_be_explained(monkeypatch):
    """Отказ `expected_participants` обязан читаться из отчёта, а не из нового
    прогона: нужны и нарушения плана до починки, и те имена, среди которых
    оракул искал участника. Без них «модель не назвала контрагента»
    неотличимо от «назвала, а починка вынесла пустой пул»."""
    scenario = scenarios.get("purchase_approval")
    bad = next(f for f in _fixtures("plan", "bad")
               if f["scenario"] == scenario.id)
    case = harness.run_generation_case(scenario, bad)
    assert case.gaps, "план с пустым пулом обязан иметь нарушение до починки"
    assert any("без единого шага" in g or "действующим лицом" in g
               for g in case.gaps), case.gaps
    named = case.participants_named
    assert named == [p["name"] if isinstance(p, dict) else str(p)
                     for p in case.structure.get("participants") or []] + \
        [l["name"] for l in case.structure.get("lanes") or []]
    assert case.as_dict()["participants_named"] == named
    assert case.as_dict()["gaps"] == case.gaps


def test_write_baseline_then_compare_is_clean(tmp_path):
    """Baseline, записанный фактическим прогоном, на следующем прогоне не даёт
    регрессий — иначе им мерить нельзя."""
    baseline_path = tmp_path / "current.json"
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    report.write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["mode"] == "replay"
    assert stored["metrics"]["quality/scheme_checks_share"] == pytest.approx(
        report.metrics_flat()["quality/scheme_checks_share"])
    rerun = harness.run(mode="replay", scenarios_spec="all", repeat=1,
                        baseline_path=baseline_path)
    assert rerun.regressions == []


def test_baseline_of_another_mode_is_not_compared(tmp_path):
    """Replay и live измеряют разное число кейсов разной моделью: сверка
    режимов между собой выдаёт «регрессию» ровно тогда, когда контур становится
    лучше (живой прогон против replay-baseline дал девять ложных падений).
    Baseline сопоставим только внутри своего режима."""
    baseline_path = tmp_path / "live.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    stored["mode"] = "live"
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=1, baseline_path=baseline_path)
    assert report.regressions == []
    assert "несопоставимы" in report.baseline_note
    assert "несопоставимы" in harness.render_table(report)


def test_baseline_from_another_ruler_is_not_compared(tmp_path):
    """Балл считается линейкой, поэтому слепок, собранный другими правилами, —
    не та мерка: добавление правила, которое заряжает то, что раньше
    прощалось, выглядит как падение `improve/score_delta` на незапятнанном
    контуре. Харнесс обязан назвать причину и не давать код выхода по такой
    «регрессии» — и не вправе молчать: ослаблять правило, чтобы цифра
    вернулась, было бы обманом вместо замера."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["ruler"]["rules"], "baseline обязан нести отпечаток линейки"
    stored["ruler"]["rules"].pop("cross_pool_flow", None)
    stored["ruler"]["weights_total"] -= 10
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=1, baseline_path=baseline_path)
    assert report.regressions == []
    assert "линейка изменилась" in report.baseline_note
    assert "cross_pool_flow" in report.baseline_note
    assert "--write-baseline" in report.baseline_note


def test_baseline_without_ruler_is_compared_but_says_so(tmp_path):
    """Слепки, записанные до отпечатка, сверять нечем — но и освобождать их от
    гейта нельзя: иначе «забыли ключ в JSON» стало бы способом выключить
    регрессии."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    del stored["ruler"]
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=1, baseline_path=baseline_path)
    assert "отпечатка линейки" in report.baseline_note
    assert "нет отпечатка" in report.baseline_note


def test_same_mode_baseline_still_reports_a_real_drop(tmp_path):
    """Пропуск сверки по режиму не должен превратиться в «сверки нет вообще»:
    то же падение метрики в своём режиме обязано быть замечено.

    Метрика взята та, что реально меряется на этом подмножестве ниже единицы
    (бизнес-доля 0.708): базовый `pass@1/scenario` из таблицы снят, и подставить
    сюда несуществующее имя значило бы проверить не детектор, а его молчаливый
    пропуск."""
    baseline_path = tmp_path / "current.json"
    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=1)
    report.write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    stored["metrics"]["quality/business_checks_share"] = 1.0
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    rerun = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                        repeat=1, baseline_path=baseline_path)
    assert rerun.baseline_note == ""
    assert [r.name for r in rerun.regressions] == ["quality/business_checks_share"]


def test_metric_newer_than_baseline_is_reported_and_not_failed(tmp_path):
    """Слепок старше метрик (сегодня — `improve/defects_repaired`): такая
    метрика не сверяется никогда, и «регрессий нет» без этой строки читается
    как «сверили всё». Заметка, а не регрессия: код выхода она не меняет."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    stored["metrics"].pop("improve/defects_repaired", None)
    stored["directions"].pop("improve/defects_repaired", None)
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    rerun = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                        repeat=1, baseline_path=baseline_path)
    assert "improve/defects_repaired" in rerun.metrics_flat()
    assert rerun.regressions == []
    assert "не сверялись с baseline" in rerun.baseline_note
    assert ("не сверялись с baseline (наборы метрик разошлись): "
            "improve/defects_repaired" in harness.render_table(rerun))
    # И в CI-маршруте: заметка не даёт ни кода 1, ни секции РЕГРЕССИИ.
    assert cli(["--mode", "replay", "--scenarios", "warehouse_delivery",
                "--baseline", str(baseline_path), "--fail-on-regression",
                "--no-report"]) == 0


def test_metric_removed_from_the_run_is_reported_not_failed(tmp_path):
    """Снятая метрика не имеет права выглядеть «всё сверили»: `compare` идёт по
    ключам baseline и такой ключ просто не находит, поэтому удаление
    `improve/no_regression` дало бы зелёный CI без единой строки об этом."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    stored["metrics"]["improve/no_regression"] = 1.0
    stored["directions"]["improve/no_regression"] = "higher"
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    rerun = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                        repeat=1, baseline_path=baseline_path)
    assert "improve/no_regression" not in rerun.metrics_flat()
    assert rerun.regressions == []
    assert "improve/no_regression" in rerun.baseline_note


def test_baseline_that_knows_every_metric_prints_no_note(tmp_path):
    """Обратная сторона: слепок, записанный этим же прогоном, свежее всех
    метрик — и пустой `baseline_note` обязан оставаться пустым, иначе заметка
    обесценится до шума."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                repeat=1).write_baseline(baseline_path)
    rerun = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                        repeat=1, baseline_path=baseline_path)
    assert rerun.regressions == []
    assert rerun.baseline_note == ""
    assert "не сверялись" not in harness.render_table(rerun)


def test_baseline_survives_change_of_repeat(tmp_path):
    """Смена `--repeat` — не регрессия: baseline хранит только качества,
    а число прогонов сверке не подлежит."""
    baseline_path = tmp_path / "current.json"
    harness.run(mode="replay", scenarios_spec="all", repeat=1).write_baseline(
        baseline_path)
    report = harness.run(mode="replay", scenarios_spec="all", repeat=2,
                         baseline_path=baseline_path)
    assert report.regressions == []
    assert "spread/runs" not in report.metrics_flat()


def test_report_json_has_cases_and_metrics(tmp_path):
    report = harness.run(mode="replay", scenarios_spec="product_return", repeat=1)
    path = harness.write_report(report, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "replay"
    assert data["metrics"]["flat"]["score"] is not None
    assert data["cases"][0]["checks_xml"]["pool_has_steps"]["ok"] is True
    assert set(data["cases"][0]["summary_xml"]) >= {"passed", "failed", "not_applicable"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cli(argv):
    """Точка входа CLI как функция: sys.exit в тестах не нужен."""
    from eval import run as run_module

    return run_module.main(argv)


def test_cli_replay_returns_zero(capsys):
    code = cli(["--mode", "replay", "--scenarios", "all", "--repeat", "1",
                "--baseline", "", "--no-report"])
    captured = capsys.readouterr()
    assert code == 0
    assert "МЕТРИКИ" in captured.out
    assert "pass@1/pool_has_steps" in captured.out


def test_cli_regression_exit_code(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "metrics": {"pass@1/scenario": 1.0, "score": 500.0},
        "directions": {},
    }), encoding="utf-8")
    code = cli(["--mode", "replay", "--baseline", str(baseline),
                        "--fail-on-regression", "--no-report"])
    captured = capsys.readouterr()
    assert code == 1
    assert "РЕГРЕССИИ" in captured.out


def test_cli_write_baseline_into_custom_path(tmp_path):
    code = cli(["--mode", "replay", "--baseline", "", "--no-report",
                        "--write-baseline", "--baseline-path",
                        str(tmp_path / "current.json")])
    assert code == 0
    assert (tmp_path / "current.json").exists()


def test_cli_unknown_scenario_returns_two(capsys):
    code = cli(["--mode", "replay", "--scenarios", "no-such-scenario",
                        "--no-report"])
    assert code == 2
    assert "неизвестные сценарии" in capsys.readouterr().err


def test_live_mode_without_credentials_fails_honestly(monkeypatch, capsys):
    from eval import run as run_module

    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    with pytest.raises(harness.HarnessError):
        harness.require_live_credentials()
    # Локальный `.env` не должен оживать в этом тесте: здесь проверяется именно
    # отсутствие ключей, а не то, что у разработчика лежит в рабочей копии.
    monkeypatch.setattr(run_module, "_load_env_file", lambda *a, **k: None)
    assert run_module.main(["--mode", "live", "--scenarios", "product_return",
                            "--no-report"]) == 2
    assert "GIGACHAT_CREDENTIALS" in capsys.readouterr().err


def test_live_mode_reads_keys_from_the_env_file(monkeypatch, tmp_path, capsys):
    """Ключи из `.env` подхватываются для live — иначе живой прогон из чистой
    оболочки падал с кодом 2, хотя ключи лежат в файле рядом с репозиторием.
    Проверка на временном файле: содержимое настоящего `.env` в тесте не нужно.

    Транспорт подменён, потому что проверяется проводка ключей до обращения к
    провайдеру: живой вызов из юнит-теста просит сеть и живую учётку.
    """
    from eval import run as run_module

    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("GIGACHAT_CREDENTIALS=клиент:секрет\n",
                        encoding="utf-8")
    asked = []

    def _fake_run(**kwargs):
        asked.append(os.environ.get("GIGACHAT_CREDENTIALS"))
        raise run_module.HarnessError("проверяется проводка, а не транспорт")

    monkeypatch.setattr(run_module.harness, "run", _fake_run)
    monkeypatch.setattr(run_module, "ENV_FILE", env_file)
    assert run_module.main(["--mode", "live", "--scenarios", "product_return",
                            "--no-report"]) == 2
    assert asked == ["клиент:секрет"], "live не подхватил ключи из файла"
    assert "проводка" in capsys.readouterr().err


def test_env_file_never_overrides_the_environment(monkeypatch, tmp_path):
    """Вызов с явными ключами важнее локального `.env`: иначе прогон молча
    считался бы чужой учёткой, и отчёт относился бы не к тем настройкам, о
    которых просили."""
    from eval import run as run_module

    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "из-окружения")
    env_file = tmp_path / ".env"
    env_file.write_text("GIGACHAT_CREDENTIALS=из-файла\n", encoding="utf-8")
    run_module._load_env_file(env_file)
    assert os.environ["GIGACHAT_CREDENTIALS"] == "из-окружения"


def test_replay_never_touches_the_env_file(monkeypatch):
    """Replay детерминирован: он не должен зависеть от того, что лежит у
    кого-то в рабочей копии. Live читает файл, replay — нет."""
    from eval import run as run_module

    def _boom(*a, **k):
        raise AssertionError("replay прочитал .env")

    monkeypatch.setattr(run_module, "_load_env_file", _boom)
    assert run_module.main(["--mode", "replay", "--scenarios",
                            "product_return", "--no-report"]) == 0


def _failing_invariants(xml: str, scenario_id: str = "purchase_approval") -> set:
    """Проваленные применимые инварианты схемы по независимому оракулу — тот же
    слой, которым харнесс решает, стоит ли вообще спрашивать модель."""
    checks = invariants.check_xml(xml, scenarios.get(scenario_id).expectations())
    return {name for name, ch in checks.items() if ch.applicable and not ch.ok}


def test_live_improve_skips_a_complaint_that_is_not_about_this_scheme(monkeypatch):
    """В live фикстура — только слот прогона: её претензия описывает дефект своей
    базовой схемы, а пакет паркуют к живой. Прогон #55 на этом и попал: цикл в
    живой схеме был закрыт (`loops_have_a_guard` прошёл), а текст пары про цикл
    всё равно требовал развилку — модель её вставляла и получала «сломано
    пакетом» за правку, которую не просили. Теперь несоответствующая претензия не
    стоит ни одного обращения к модели и не попадает в метрики качества.

    Проверяется на живом вызове: заглушка `_live_improve` считает обращения,
    поэтому тест ломается, если харнесс спросит модель «на всякий случай».
    """
    calls = []

    def _fake_live(xml, prompt):
        calls.append(prompt)
        return xml, {}, 1

    monkeypatch.setattr(harness, "_live_improve", _fake_live)
    off_target = sorted(set(invariants.ALL_CHECKS)
                        - _failing_invariants(GUARANTEE_XML))[0]
    case = harness.run_improvement_case(
        scenarios.get("purchase_approval"),
        {"id": "fx.off", "scenario": "purchase_approval", "label": "не про это",
         "quality": "real", "prompt": "почини нарушение", "operations": [],
         "targets": [off_target]},
        _base_gencase(GUARANTEE_XML), mode="live")
    assert case.off_target is True
    assert calls == [], "несоответствующая претензия потратила обращение к модели"
    assert off_target in case.off_target_note
    assert not case.error, "кейс вне цели — не провал контура"


def test_off_target_case_leaves_every_quality_metric():
    """Кейс без пакета не может ни починить, ни сломать, ни «не примениться»:
    он обязан выпасть из знаменателя всех метрик улучшения, кроме самой
    применимости. Иначе `advice_applicable` — просто ещё одна строка, а
    `defects_repaired` по-прежнему считает мусор."""
    off = harness.ImproveCase(
        scenario="s", fixture="off", label="", quality="real", mode="live",
        targets=["no_unrouted"], targets_declared=True, off_target=True,
        off_target_note="претензия не про эту схему", checks_before={})
    good = harness.ImproveCase(
        scenario="s", fixture="good", label="", quality="real", mode="replay",
        ok=True, targets=["waits_have_sla"], targets_declared=True,
        applied=[{"op": "rename"}], noop_rows=0,
        score_before=90.0, score_after=95.0)
    result = harness.build_improvement_suite().run(
        [{"name": "off", "payload": off}, {"name": "good", "payload": good}])
    assert result.metrics["improve/advice_off_target_share"].values == [1.0, 0.0]
    assert result.metrics["improve/advice_off_target_share"].mean == 0.5
    assert result.metrics["improve/error_share"].values[0] is None
    assert result.metrics["improve/score_delta"].values == [None, 5.0]
    assert result.metrics["improve/op_acceptance"].values[0] is None


def test_improve_fixture_without_targets_is_reported_unmeasured():
    """Пустой `targets` — осознанная разметка («просят улучшение, а не снимают
    нарушение»), а отсутствие ключа — дыра в фикстуре: она обязана быть видна в
    отчёте, а не молча считаться применимой."""
    unnamed = harness.ImproveCase(scenario="s", fixture="f", label="",
                                  quality="real", mode="replay")
    harness._mark_off_target(unnamed)
    assert not unnamed.off_target
    assert any("targets" in note for note in unnamed.unmeasured)
    declared = harness.ImproveCase(scenario="s", fixture="f", label="",
                                   quality="real", mode="replay",
                                   targets=[], targets_declared=True)
    harness._mark_off_target(declared)
    assert not declared.off_target and not declared.unmeasured


def _live_gencase(xml: str, scenario_id: str = "purchase_approval") -> harness.GenCase:
    """Живая схема как кейс генерации: с прогоном по оракулу, как это делает
    `run_generation_case` после `_live_plan`."""
    case = harness.GenCase(scenario=scenario_id, fixture="live", label="",
                           quality="live", mode="live", ok=True, xml=xml)
    case.checks_xml = invariants.check_xml(
        xml, scenarios.get(scenario_id).expectations())
    return case


def _case_without_violations(case: harness.GenCase) -> harness.GenCase:
    """Тот же кейс, но оракул на нём молчит: проверка ветки «чинить нечего»."""
    case.checks_xml = {name: dataclasses.replace(ch, ok=True, reason="")
                       for name, ch in case.checks_xml.items()}
    return case


def test_advice_fixture_complains_only_about_what_the_scheme_fails():
    """Кейс «по факту» строит претензию из того, что в схеме нашёл независимый
    оракул, — иначе live-выборка улучшения пуста (фикстурная жалоба редко болит
    живой схеме). Промпт при этом статичен: ни id, ни имён участников, ни
    названий правил — подсказывать ответ харнесс не имеет права, детали дефекта
    оркестратор достаёт из скоринга сам."""
    case = _live_gencase(GUARANTEE_XML)
    fixtures = harness._advice_fixtures(scenarios.get("purchase_approval"), case)
    assert len(fixtures) == 1
    failing = {c.name for c in invariants.applicable_checks(case.checks_xml)
               if not c.ok}
    assert set(fixtures[0]["targets"]) <= failing
    assert fixtures[0]["targets"]
    blob = fixtures[0]["prompt"] + harness.ADVICE_PROMPT
    assert fixtures[0]["prompt"] == harness.ADVICE_PROMPT
    for leak in ("A1", "F1", "ВкусВилл", "Поставщик", "has_branching"):
        assert leak not in blob, f"промпт улучшения подсказывает {leak}"


def test_advice_fixture_skips_a_scheme_with_nothing_to_fix(monkeypatch):
    """Нет нарушения и нет подсказки скоринга — нет и кейса: просить «сделай
    лучше» у схемы, где мерить нечего, значит раздувать выборку нулями."""
    case = _case_without_violations(_live_gencase(GUARANTEE_XML))
    assert not [c for c in invariants.applicable_checks(case.checks_xml)
                if not c.ok], "заглушка не почистила провалы"
    monkeypatch.setattr(harness, "score_xml",
                        lambda xml: {"recommendations_by_rule": {}})
    assert harness._advice_fixtures(
        scenarios.get("purchase_approval"), case) == []


def test_advice_case_reaches_the_quality_metrics(monkeypatch):
    """Кейс «по факту» обязан давать выборку метрикам улучшения: с узкими
    `targets` фикстурные слоты почти всегда вне цели, и без этого прогона
    `defects_repaired` в live снова считался бы по нулям или не считался вовсе."""
    case = _live_gencase(GUARANTEE_XML)
    fixture = harness._advice_fixtures(scenarios.get("purchase_approval"), case)[0]
    monkeypatch.setattr(harness, "_live_improve",
                        lambda xml, prompt: (xml, {"applied": [
                            {"op": "rename", "id": "A1", "name": "Новое имя"}],
                            "noop_rows": 0}, 1))
    out = harness.run_improvement_case(scenarios.get("purchase_approval"),
                                       fixture, case, mode="live")
    assert out.off_target is False
    assert out.quality == "advice"
    assert out.repaired_share == 0.0, "пакет ничего не снял — метрика обязана это видеть"
    assert out.score_delta is not None


def test_every_improve_fixture_declares_targets_of_its_own_base():
    """Рэтчет набора: претензия каждой improve-фикстуры обязана быть размечена и
    соответствовать нарушению её же базовой схемы. Без этого live-кейс измеряет
    несоответствие текста и схемы (прогон #55), а replay — пакет, который чинит
    то, чего в базе не было."""
    plans = _fixtures("plan")
    for fixture in _fixtures("improve"):
        assert "targets" in fixture, f"у {fixture['id']} нет ключа `targets`"
        if not fixture["targets"]:
            continue  # осознанно: пакет просит улучшение, нарушение не снимает
        base = harness.fixture_by_id(fixture.get("base_plan", ""), plans)
        assert base is not None, f"{fixture['id']} ссылается на несуществующий план"
        gen = harness.run_generation_case(scenarios.get(fixture["scenario"]),
                                          base, mode="replay")
        failing = {name for name, ch in gen.checks_structure.items()
                   if ch.applicable and not ch.ok}
        assert set(fixture["targets"]) & failing, (
            f"{fixture['id']} заявляет {fixture['targets']}, а базовая схема "
            f"проваливает {sorted(failing)}")


def test_quality_share_is_the_bar_and_the_conjunction_is_not_a_metric():
    """Сводная доля пройденных гейт-проверок вместо конъюнкции в планке.

    Причина измерена, а не предполагана: в живом прогоне #56 `pass@1/scenario`
    дал 0.062, а #57 — 0, потому что каждая схема проваливает хотя бы одну из
    ~17 проверок, тогда как в среднем провалено 15% проверок (сводная доля
    0.853/0.814). Планка на конъюнкции физически недостижима без одновременной
    починки всех классов, то есть меряла бы не контур, а геометрию произведения,
    и отчитываться таким числом — всегда ноль. Строка из таблицы снята; факт
    кейса («схема без единого провала») остался и в `scenario_pass`, и в имени
    файла `--dump-schemes`, и в атрибуции.
    """
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    share = report.generation.metrics["quality/scheme_checks_share"]
    assert share.mean is not None and share.mean >= harness.QUALITY_BAR, share.mean
    assert share.n == len([c for c in report.cases if c.ok])
    for gone in ("pass@1/scenario", "pass@1/structure"):
        assert gone not in report.generation.metrics, gone
        assert gone not in report.improvement.metrics, gone
    assert "improve/pass@1" not in report.improvement.metrics
    # сам факт никуда не делся: без него нечем было бы называть владельца провала
    assert any(c.scenario_pass is False for c in report.cases)
    dumped = report.cases[0].as_dict()
    assert "scenario_pass" in dumped and "checks_xml" in dumped
    # доля по структуре и по XML считаются раздельно: план и транспорт
    assert "quality/structure_checks_share" in report.generation.metrics
    assert report.generation.metrics["quality/structure_checks_share"].mean is not None


def test_business_share_is_reported_but_never_a_bar():
    """Бизнес-слой сводится в долю тех же проверок, но планкой не становится:
    потолок задан набором, и эталон держит узкое место наравне с моделью."""
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    biz = report.generation.metrics["quality/business_checks_share"]
    assert biz.mean is not None and 0.0 <= biz.mean <= 1.0
    assert "потолок задан набором" in biz.description
    assert "quality/business_checks_share" not in harness.LOWER_IS_BETTER


def test_manual_review_lists_schemes_without_inventing_a_metric():
    """Ручная оценка — список того, что смотрит человек, плюс адрес схемы. Ни
    числа, ни вердикта от харнесса здесь нет: второй оценивающая модель
    добавила бы шум к шуму генерации и превратила «довести метрику» в самооценку.

    В лист попадают только схемы, которые автоматика уже пропустила: у брака
    владелец назван в атрибуции, и смотреть его глазами бессмысленно."""
    report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
    review = report.manual_review()
    assert len(review["items"]) >= 3
    assert review["bar"] == harness.QUALITY_BAR
    good = {c.key for c in report.cases
            if c.ok and c.gate_share is not None and c.gate_share >= harness.QUALITY_BAR}
    assert {row["case"] for row in review["cases"]} == good
    rendered = harness.render_table(report)
    assert "РУЧНАЯ ОЦЕНКА" in rendered and "метрик здесь нет" in rendered
    assert report.as_dict()["manual_review"]["items"] == review["items"]


def test_scenarios_cover_every_process_and_fixture_is_wired():
    """Сценарии и фикстуры — один связный набор: у каждого сценария есть хотя
    бы один план, и каждая improve-фикстура ссылается на существующий базовый
    план."""
    listed = {s.id for s in scenarios.all_scenarios()}
    assert len(listed) >= 6
    plans = _fixtures("plan")
    assert listed <= {f["scenario"] for f in plans}
    plan_ids = {f["id"] for f in plans}
    for fixture in _fixtures("improve"):
        assert fixture.get("base_plan") in plan_ids, (
            f"improve-фикстура {fixture['id']} без базового плана")
    for scenario in scenarios.all_scenarios():
        expectations = scenario.expectations()
        assert expectations["max_pools"] >= 1
        assert expectations["min_steps"] >= 1
        assert scenario.text.strip()

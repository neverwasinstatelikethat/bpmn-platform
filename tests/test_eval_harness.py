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

import json
from pathlib import Path

import pytest

from eval import harness, metrics, scenarios
from eval.invariants import (ALL_CHECKS, CORE_INVARIANTS, check_structure,
                             check_xml, summarize)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_WAREHOUSE_XML = REPO_ROOT / "reports" / "warehouse-delivery" / "process.bpmn"

# Что обязан провалить каждый «плохой» план на уровне сырого ответа модели.
# С сверяется ровно это множество: лишние провалы означают, что оракул путает
# дефекты, а отсутствие нужного — что перестал их видеть.
EXPECTED_RAW_FAILS = {
    "warehouse_delivery.live.plan": {
        "pool_has_steps", "participant_interacts", "roles_as_lanes",
        "event_definitions", "has_branching", "no_unrouted"},
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


def _fixtures(kind: str, quality: str = ""):
    out = [f for f in harness.load_fixtures(kinds=(kind,))
           if not quality or f.get("quality") == quality]
    assert out, f"нет {kind}-фикстур quality={quality!r}"
    return out


def _raw_failures(fixture) -> set:
    scenario = scenarios.get(fixture["scenario"])
    results = check_structure(fixture["plan"], scenario.expectations())
    return {check["name"] for check in summarize(results)["failed"]}


# ---------------------------------------------------------------------------
# оракул: хорошие и плохие ответы модели
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures("plan", "good"),
                         ids=lambda f: f["id"])
def test_good_plans_satisfy_all_declared_invariants(fixture):
    """Эталонный план не проваливает ни одного заявленного инварианта."""
    assert _raw_failures(fixture) == set(), (
        f"{fixture['id']} проваливает инварианты, которых в эталоне быть не должно")


@pytest.mark.parametrize("fixture", _fixtures("plan", "bad")
                         + _fixtures("plan", "real"),
                         ids=lambda f: f["id"])
def test_bad_plans_fail_exactly_the_broken_invariants(fixture):
    """Плохой план падает ровно на сломанных свойствах — метрика полезна только
    когда она различает классы дефектов."""
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
    # неприменимые проверки в метрику не входят: проверять нечего
    assert summarize(results)["not_applicable"] == ["roles_as_lanes"]


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


# Каркас двухпуловой схемы — ровно та же схема, что собирает
# `_flow_ends_plan`: messageFlow Клиента приходит в startEvent ВкусВилла,
# это штатный способ запустить пул. Поток обязан лежать внутри `process`,
# иначе оракул его не увидит.
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
      <timerEventDefinition/>
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
    # нулевой baseline: относительная доля невозможна, но ухудшение видно
    assert [r.name for r in detector.compare({"x": 0.0}, {"x": 1.0})] == ["x"]


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
    assert 0.0 <= flat["pass@1/scenario"] <= 1.0
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
    live = cases["warehouse_delivery/warehouse_delivery.live.improve"]
    assert live.ok and live.retried
    assert 0 < live.applied_share < 1
    assert live.score_after is not None
    # «хороший» пакет применяется целиком и не портит схему
    clean = cases["production_incident/production_incident.good.improve"]
    assert clean.applied_share == 1.0 and not clean.retried
    assert clean.score_delta >= 0
    # Разбор отказа улучшения читается из отчёта: без базовой схемы прогон не
    # отличает унаследованный от генерации провал от того, что принёс пакет,
    # а без списка применённых операций не видно, какая правка двигала балл.
    dump = clean.as_dict()
    assert dump["checks_before"] and dump["applied_details"]
    assert dump["summary_before"]["applicable"] >= 1


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
    assert stored["metrics"]["pass@1/scenario"] == pytest.approx(
        report.metrics_flat()["pass@1/scenario"])
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


def test_same_mode_baseline_still_reports_a_real_drop(tmp_path):
    """Пропуск сверки по режиму не должен превратиться в «сверки нет вообще»:
    то же падение метрики в своём режиме обязано быть замечено."""
    baseline_path = tmp_path / "current.json"
    report = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                         repeat=1)
    report.write_baseline(baseline_path)
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    stored["metrics"]["pass@1/scenario"] = 1.0
    baseline_path.write_text(json.dumps(stored, ensure_ascii=False),
                             encoding="utf-8")

    rerun = harness.run(mode="replay", scenarios_spec="warehouse_delivery",
                        repeat=1, baseline_path=baseline_path)
    assert rerun.baseline_note == ""
    assert [r.name for r in rerun.regressions] == ["pass@1/scenario"]


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
    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    with pytest.raises(harness.HarnessError):
        harness.require_live_credentials()
    code = cli(["--mode", "live", "--scenarios", "product_return",
                        "--no-report"])
    assert code == 2
    assert "GIGACHAT_CREDENTIALS" in capsys.readouterr().err


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

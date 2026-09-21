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
from eval.invariants import check_structure, check_xml, summarize

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

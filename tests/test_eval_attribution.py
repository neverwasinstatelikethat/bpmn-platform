"""Атрибуция дефектов по узлам контура (`eval/attribution.py`).

Харнесс обязан отличать «модель не дала» от «починка испортила» и от «генерация
XML потеряла» — иначе каждая регрессия разбирается отдельным живым прогоном.
Здесь проверяются именно развилки правил, а не текст отчёта.
"""

from __future__ import annotations

from typing import Any, Dict, List

from eval import attribution
from eval.invariants import Check


def _check(name: str, ok: bool = False, ids=(), applicable: bool = True) -> Check:
    return Check(name=name, ok=ok, reason="проверка", ids=tuple(ids),
                 applicable=applicable)


def _trace(**overrides) -> List[Dict[str, Any]]:
    """Трейс живого контура: узлы по порядку, переданные — дополняют базовые.

    «Переспрос плана» в базе отсутствует намеренно: он появляется в трейсе
    только если переспрос заводился, и подсовывать его всем тестам значило бы
    приписывать контуру решение, которого он не принимал.
    """
    order = ["первый ответ модели", "переспрос плана",
             "вопрос о принадлежности", "починка структуры", "генерация XML"]
    base: Dict[str, Dict[str, Any]] = {
        "первый ответ модели": {"gaps": [], "pools": 1, "elements": 3, "flows": 2},
        "вопрос о принадлежности": {"notes": []},
        "починка структуры": {"steps": []},
        "генерация XML": {"elements": 3, "flows": 2},
    }
    out: List[Dict[str, Any]] = []
    for name in order:
        if name not in base and name not in overrides:
            continue
        entry: Dict[str, Any] = {"node": name}
        entry.update(base.get(name, {}))
        entry.update(overrides.get(name, {}))
        out.append(entry)
    return out


def _steps(*records: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"step": r.get("step", "?"), "added": r.get("added", []),
             "removed": r.get("removed", []), "notes": r.get("notes", [])}
            for r in records]


class TestGeneration:
    def test_structure_ok_xml_failed_is_blamed_on_xml(self):
        checks_xml = {"has_timer": _check("has_timer", ids=["T1"])}
        checks_structure = {"has_timer": _check("has_timer", ok=True)}
        owner = attribution.attribute_generation(
            checks_xml, checks_structure, _trace())
        assert owner == {"has_timer": attribution.OWNER_XML}

    def test_repair_step_that_touched_the_element_owns_it(self):
        trace = _trace(**{"починка структуры": {"steps": _steps(
            {"step": "вставка развилок", "added": ["G9@Склад"], "removed": []})}})
        checks = {"gateway_split_join": _check("gateway_split_join", ids=["G9"])}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["gateway_split_join"] == "починка:вставка развилок"

    def test_defect_known_from_the_plan_is_not_blamed_on_repair(self):
        """Починка могла его убрать, но не могла его придумать: «вставка
        схождений» задела G1 попутно, а нарушал G1 ещё в ответе модели."""
        trace = _trace(**{"починка структуры": {"steps": _steps(
            {"step": "вставка схождений", "added": [], "removed": ["F4"]})}})
        checks = {"gateway_conditions_or_default": _check(
            "gateway_conditions_or_default", ids=["G1"])}
        owner = attribution.attribute_generation(
            checks, {}, trace, gaps=["у шлюза G1 2 веток без условия"])
        assert owner["gateway_conditions_or_default"] == (
            attribution.OWNER_MODEL_FIRST + attribution.OWNER_SEEN)

    def test_replay_plan_is_blamed_on_the_fixture_not_the_model(self):
        """В replay ответ не модельный: харнесс не имеет права списывать провал
        на GigaChat, которого не было."""
        trace = [{"node": "починка структуры", "steps": []}]
        checks = {"min_steps": _check("min_steps", ids=["A1"])}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["min_steps"] == (attribution.OWNER_FIXTURE
                                      + attribution.OWNER_BLIND)

    def test_accepted_reask_moves_the_blame_to_the_second_answer(self):
        trace = _trace(**{"переспрос плана": {"gaps_before": 3, "gaps_after": 1,
                                              "kept": "переспрос",
                                              "outcome": "стал лучше"}})
        checks = {"has_branching": _check("has_branching")}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["has_branching"] == attribution.OWNER_MODEL_REASK

    def test_rejected_reask_blames_the_contour_only_when_it_threw_a_fix_away(self):
        """Отказ принять повтор — вина контура ровно тогда, когда повтор нёс
        правку этого нарушения. Живой прогон #53: 9 из 18 отвергнутых повторов
        вернули тот же набор нарушений, и «крупнейший владелец провалов»
        оказался артефактом разметки, а не рычагом."""
        trace = _trace(**{"переспрос плана": {"gaps_before": 2, "gaps_after": 2,
                                              "kept": "первый ответ",
                                              "fixed_kinds": {"развилка": 1},
                                              "outcome": "не улучшил"}})
        checks = {"has_branching": _check("has_branching")}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["has_branching"] == attribution.OWNER_REASK_REJECTED

    def test_reask_that_touched_nothing_is_the_models_second_answer(self):
        trace = _trace(**{"переспрос плана": {"gaps_before": 2, "gaps_after": 2,
                                              "kept": "первый ответ",
                                              "fixed_kinds": {"пустой пул": 1},
                                              "outcome": "не улучшил"}})
        checks = {"has_branching": _check("has_branching")}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["has_branching"] == attribution.OWNER_REASK_UNTOUCHED

    def test_the_missing_participant_blames_the_reask_on_participant_classes(self):
        """Классы правок сверяются с нарушением инварианта, а не с первым
        попавшимся: пустой пул названного участника — это и есть потерянный
        участник, а не косметика маршрута."""
        reask = {"gaps_before": 4, "gaps_after": 5, "kept": "первый ответ",
                 "fixed_kinds": {"пустой пул": 1},
                 "outcome": "не улучшил"}
        checks = {"expected_participants": _check("expected_participants"),
                  "has_branching": _check("has_branching")}
        trace = _trace(**{"переспрос плана": reask})
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["expected_participants"] == attribution.OWNER_REASK_REJECTED
        assert owner["has_branching"] == attribution.OWNER_REASK_UNTOUCHED

    def test_a_reask_that_never_happened_keeps_the_first_answer_guilty(self):
        """Отказ от повторного запроса (транспорт, негодный JSON) — не «контур
        выбросил правку»: правки не было, и владелец остаётся у первого ответа."""
        trace = _trace(**{"переспрос плана": {"gaps_before": 2,
                                              "kept": "первый ответ",
                                              "outcome": "не выполнен"}})
        checks = {"has_branching": _check("has_branching")}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["has_branching"] == attribution.OWNER_MODEL_FIRST

    def test_the_repair_classes_are_the_ones_the_generator_emits(self):
        """Словарь атрибуции и классы плана расходятся молча и без падений:
        несуществующий класс просто никогда не совпадёт, и харнесс вернётся к
        той же лжи, от которой эту правку завели."""
        from core import bpmn_generator
        from eval.invariants import INVARIANTS
        emitted = {kind for _needle, kind in bpmn_generator._GAP_CLASS_OF}
        used = {kind for kinds in attribution._REASK_REPAIRS.values()
                for kind in kinds}
        assert used <= emitted
        assert set(attribution._REASK_REPAIRS) <= set(INVARIANTS)

    def test_clarify_answer_owns_what_it_moved(self):
        trace = _trace(**{"вопрос о принадлежности": {
            "notes": ["шаг A7 перенесён в пул «Поставщик»"]}})
        checks = {"participant_interacts": _check("participant_interacts",
                                                  ids=["A7"])}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["participant_interacts"] == attribution.OWNER_CLARIFY

    def test_not_applicable_checks_are_not_attributed(self):
        checks = {"has_timer": _check("has_timer", ok=False, applicable=False)}
        assert attribution.attribute_generation(checks, {}, _trace()) == {}


class TestImprovement:
    def test_invariant_broken_before_the_package_is_not_the_packages_fault(self):
        before = {"event_definitions": _check("event_definitions", ids=["A11"])}
        after = {"event_definitions": _check("event_definitions", ids=["A11"])}
        owner = attribution.attribute_improvement(
            before, after, [{"op": "add_task", "id": "new_A9"}], [])
        assert owner["event_definitions"] == attribution.OWNER_BASE

    def test_refused_operation_owns_the_breakage_it_left_behind(self):
        after = {"no_unrouted": _check("no_unrouted", ids=["new_A9"])}
        owner = attribution.attribute_improvement(
            {}, after, [], [{"op": "add_task", "id": "new_A9", "stage": "retry"}])
        assert owner["no_unrouted"] == "аплайер:retry"

    def test_applied_operation_owns_the_shape_it_produced(self):
        after = {"gateway_conditions_or_default": _check(
            "gateway_conditions_or_default", ids=["new_G1"])}
        owner = attribution.attribute_improvement(
            {}, after, [{"op": "add_gateway", "id": "new_G1", "stage": "план"}],
            [])
        assert owner["gateway_conditions_or_default"] == "пакет модели:add_gateway"

    def test_broken_with_no_package_at_all_is_a_base_defect(self):
        owner = attribution.attribute_improvement(
            {}, {"min_steps": _check("min_steps", ids=["A1"])}, [], [])
        assert owner["min_steps"] == attribution.OWNER_BASE


class TestTally:
    def test_tally_sorts_by_count_then_name(self):
        rows = attribution.tally({"a": "модель:x", "b": "модель:x",
                                  "c": "починка:потоки"})
        assert rows == [("модель:x", 2), ("починка:потоки", 1)]

    def test_empty_tally_says_so_instead_of_printing_nothing(self):
        assert attribution.format_tally(attribution.tally({})) == [
            "проваленных инвариантов нет — атрибутировать нечего"]

    def test_total_is_reported_with_every_owner(self):
        lines = attribution.format_tally(
            attribution.tally({"a": "x", "b": "x", "c": "y"}))
        assert "x — 2 из 3" in lines and "y — 1 из 3" in lines


class TestHarnessIntegration:
    """Атрибуция обязана доезжать до отчёта: ради него харнесс и затевался."""

    def test_replay_case_carries_the_owner_of_its_failure(self):
        from eval import harness

        report = harness.run(mode="replay",
                             scenarios_spec="employee_onboarding")
        blamed = [c for c in report.cases if c.attribution]
        assert blamed, "битая фикстура обязана оставить атрибуцию провала"
        owners = blamed[0].attribution
        assert all(owners.values())
        # Ответ модели тут ни при чём: план харнессу дал автор фикстуры.
        assert all(o.startswith(attribution.OWNER_FIXTURE) for o in owners.values())
        dump = blamed[0].as_dict()
        assert dump["attribution"] == owners
        assert dump["trace"], "без трейса атрибуция неотличима от догадки"

    def test_console_summarises_owners_and_provenance(self):
        from eval import harness

        report = harness.run(mode="replay", scenarios_spec="vehicle_reservation")
        table = harness.render_table(report)
        assert "КТО ПОРОДИЛ ДЕФЕКТЫ" in table
        assert "из " in table.split("КТО ПОРОДИЛ ДЕФЕКТЫ", 1)[1].split(
            "ПРОВЕНАНС")[0]
        assert "виноват" in table


class TestTouchedScope:
    def test_element_token_does_not_blame_its_pool(self):
        """`A2@Цех` — про элемент: шаг, добавивший в пул событие, не обязан
        отвечать за дефекты самого пула."""
        assert attribution._entry_tokens("A2@Цех фасовки") == {"A2"}
        assert attribution._entry_tokens("pool:Цех фасовки") == {"Цех фасовки"}
        assert attribution._entry_tokens("flow:F2:A2->A3") == {"F2", "A2", "A3"}

    def test_pool_level_defect_is_blamed_only_on_pool_entries(self):
        trace = _trace(**{"починка структуры": {"steps": _steps(
            {"step": "события пула", "added": ["StartEvent_1@Оператор склада"],
             "removed": []})}})
        checks = {"roles_as_lanes": _check("roles_as_lanes",
                                          ids=["Оператор склада"])}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["roles_as_lanes"].startswith(attribution.OWNER_MODEL_FIRST)

    def test_dropped_pool_is_still_blamed_on_the_step_that_dropped_it(self):
        trace = _trace(**{"починка структуры": {"steps": _steps(
            {"step": "удаление пустых пулов", "added": [],
             "removed": ["pool:Поставщик"]})}})
        checks = {"expected_participants": _check("expected_participants",
                                                 ids=["Поставщик"])}
        owner = attribution.attribute_generation(checks, {}, trace)
        assert owner["expected_participants"] == \
            "починка:удаление пустых пулов"

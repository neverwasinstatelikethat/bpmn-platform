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
        assert owner["no_unrouted"] == f"{attribution.OWNER_REFUSED}:повтор"

    def test_a_refusal_is_not_an_applier_breakage(self):
        """Отказ в плане и в повторе — корректное решение по пакету модели,
        снятие узла после починки — работа гаранта связности. Одна строка
        «аплайер:<stage>» на все три случая звала чинить не тот узел, а
        английское «plan» из fallback не совпадало ни с одним stage, который
        оркестратор действительно пишет."""
        after = {"no_unrouted": _check("no_unrouted", ids=["new_A9"])}
        owners = {stage: attribution.attribute_improvement(
            {}, after, [], [{"op": "add_task", "id": "new_A9", "stage": stage}])[
            "no_unrouted"] for stage in ("plan", "retry", "repair", None)}
        assert owners["plan"] == f"{attribution.OWNER_REFUSED}:план"
        assert owners["retry"] == f"{attribution.OWNER_REFUSED}:повтор"
        assert owners["repair"] == f"{attribution.OWNER_GUARD}:починка"
        # Раунд без пометки stage — это план: оркестратор сам проставляет
        # «plan» (`llm_improve`), и фолбэк обязан совпасть с ним, а не выдумать
        # четвёртого владельца.
        assert owners[None] == f"{attribution.OWNER_REFUSED}:план"
        assert len(set(owners.values())) == 3, owners
        assert owners["repair"] != owners["plan"], (owners["repair"],
                                                    owners["plan"])
        # Суффикс раунда — по-русски: «аплайер:plan» был гибридом двух языков.
        assert not any(latin in o for o in owners.values()
                       for latin in ("plan", "retry", "repair")), owners

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

    def test_refusal_closed_by_the_retry_does_not_steal_the_blame(self):
        """Строка отказа первого раунда живёт в отчёте как история: повтор провёл
        ту же правку, и перехватывать ею владельца нельзя — иначе крупнейшим
        «породителем дефектов» прогона становится бухгалтерия отказов (тот же
        артефакт разметки, что в сводке генерации с «отвергнутым переспросом»).
        Флаг `reapplied` ставит оркестратор, и только на нём ехать нельзя:
        закрытие сверяется ещё и по идентичности операции над тем же
        элементом."""
        before = {"no_unrouted": _check("no_unrouted", ok=True)}
        after = {"no_unrouted": _check("no_unrouted", ids=["A", "B"])}
        applied = [{"op": "disconnect", "flow": "f2", "source": "A", "target": "B",
                    "stage": "retry"}]
        refusal = {"op": "disconnect", "flow": "f2", "source": "A", "target": "B",
                   "stage": "plan", "reason": "нет такого потока"}
        expected = {"no_unrouted": "пакет модели:disconnect"}
        assert attribution.attribute_improvement(
            before, after, applied, [dict(refusal, reapplied=True)]) == expected
        assert attribution.attribute_improvement(
            before, after, applied, [refusal]) == expected
        # Незакрытый отказ остаётся владельцем: правки в схеме нет, и назвать
        # нечего, кроме раунда, который её отверг.
        assert attribution.attribute_improvement(
            before, after, [], [dict(refusal, reapplied=False)]) == {
            "no_unrouted": f"{attribution.OWNER_REFUSED}:план"}
        # Другая правка — не закрытие этого отказа: зачёт только по той же
        # операции над тем же элементом (поля — `bpmn_edits.OP_ELEMENT_FIELDS`).
        assert attribution.attribute_improvement(
            before, after, [{"op": "connect", "source": "C", "target": "D",
                             "stage": "retry"}], [refusal])["no_unrouted"] == \
            f"{attribution.OWNER_REFUSED}:план"

    def test_base_defect_stays_with_the_base_even_when_rows_match(self):
        """Пакет, который не починил, — не тот, кто сломал: провал значил и до
        правок. Строки применения/отказа на те же id не имеют права перетягивать
        вину на `improve`-контур, иначе доля провалов пакета мерялась бы
        качеством генерации."""
        broken = {"event_definitions": _check("event_definitions", ids=["A11"])}
        owner = attribution.attribute_improvement(
            broken, broken,
            [{"op": "edit_element", "id": "A11", "stage": "retry"}],
            [{"op": "add_event", "id": "A11", "stage": "plan", "reapplied": False}])
        assert owner == {"event_definitions": attribution.OWNER_BASE}
        # Тот же порядок и при откаченном пакете: на выходе базовый XML.
        assert attribution.attribute_improvement(
            broken, broken, [], [{"op": "add_event", "id": "A11", "stage": "plan"}],
            report={"package_reverted": "цикл без выхода"}) == \
            {"event_definitions": attribution.OWNER_BASE}

    def test_introduced_damage_names_the_node_that_changed_the_scheme(self):
        """«Сломал то, что работало» обязан называть физическое изменение:
        снятие гаранта, применившуюся операцию или самовольную правку починки.
        Прежний порядок («skipped раньше applied») отдавал такой провал отказу,
        который ничего в схеме не менял, — и две разные поломки читались одной
        строкой."""
        before = {"no_unrouted": _check("no_unrouted", ok=True)}
        after = {"no_unrouted": _check("no_unrouted", ids=["new_C"])}
        refusal = {"op": "add_task", "id": "new_C", "participant": "Склад",
                   "stage": "plan", "reapplied": False}
        # Гарант связности снял узел после починки.
        assert attribution.attribute_improvement(
            before, after, [],
            [refusal, {"op": "add_task", "id": "new_C", "stage": "repair"}])[
            "no_unrouted"] == f"{attribution.OWNER_GUARD}:починка"
        # Операция применилась и оставила шаг вне маршрута — при том что отказ
        # называет этот же id.
        assert attribution.attribute_improvement(
            before, after, [{"op": "add_task", "id": "new_C", "stage": "retry"}],
            [refusal])["no_unrouted"] == "пакет модели:add_task"
        # Починка подменила тип сама: ни applied-строки, ни отказа по ней нет,
        # есть только пометка `validate_and_repair`.
        assert attribution.attribute_improvement(
            before, after, [], [refusal],
            report={"notes": ["шлюз «Проверка» (new_C) понижен до задачи "]})[
            "no_unrouted"] == attribution.OWNER_REPAIR_SOLO

    def test_repair_note_that_only_asks_the_model_is_not_an_owner(self):
        """«Узел вне маршрута» — требование к пакету, а не правка починки:
        назвать её владельцем значит переложить урон операции на того, кто его
        не наносил."""
        after = {"no_unrouted": _check("no_unrouted", ids=["new_C"])}
        owner = attribution.attribute_improvement(
            {}, after, [{"op": "add_task", "id": "new_C", "stage": "plan"}], [],
            report={"notes": ["узел (new_C) недостижим: выход есть, входа нет"]})
        assert owner["no_unrouted"] == "пакет модели:add_task"

    def test_noop_application_says_so_instead_of_quietly_owning_the_breakage(
            self):
        """Аплайер отчитался строкой, а дерево не тронул: `пакет модели:add_task`
        без приписки читался бы как «правка сломала схему», хотя сломать она
        ничего не могла. Агрегат `noop_rows` покрывает строки, которым пометку
        не сохранили (склейка раундов)."""
        before = {"no_unrouted": _check("no_unrouted", ok=True)}
        after = {"no_unrouted": _check("no_unrouted", ids=["new_X"])}
        row = {"op": "add_task", "id": "new_X", "stage": "plan",
               "note": "изменение добавлено не было: схема не изменилась"}
        assert attribution.attribute_improvement(before, after, [row], []) == {
            "no_unrouted": "пакет модели:add_task (применилось без изменения схемы)"}
        assert attribution.attribute_improvement(
            before, after, [{"op": "add_task", "id": "new_X", "stage": "plan"}], [],
            report={"noop_rows": 1})["no_unrouted"].endswith(
            "(применилось без изменения схемы)")

    def test_reverted_package_is_a_base_defect_named_by_the_guard(self):
        """Откат пакета — решение гаранта, а не «отказ аплайером по операции»:
        в схеме лежит базовый XML, и провал в нём принадлежит генерации. Без
        приписки «дефект базовой схемы» был неотличим от «пакет даже не
        пытались применить». Применившиеся правки второго раунда при этом
        остаются владельцами своего урона."""
        before = {"no_unrouted": _check("no_unrouted", ok=True)}
        after = {"no_unrouted": _check("no_unrouted", ids=["new_C"])}
        owner = attribution.attribute_improvement(
            before, after, [], [{"op": "add_task", "id": "new_C",
                                 "reason": "изменение откачено вместе с пакетом"}],
            report={"package_reverted": "цикл без выхода"})
        assert owner == {
            "no_unrouted": attribution.OWNER_BASE + attribution.OWNER_ROLLED_BACK}
        # Сырой отчёт `apply_and_guarantee` называет откат просто `reverted`:
        # атрибуция обязана принять и его, и весь отчёт целиком.
        assert attribution.attribute_improvement(
            before, after, [], [{"op": "add_task", "id": "new_C"}],
            report={"reverted": "цикл без выхода", "applied": [], "notes": []}) == owner
        assert attribution.attribute_improvement(
            before, after, [{"op": "connect", "id": "new_C", "stage": "retry"}],
            [{"op": "add_task", "id": "new_C"}],
            report={"package_reverted": "цикл без выхода"})["no_unrouted"] == \
            "пакет модели:connect"

    def test_refusal_names_whether_the_corrective_round_ran(self):
        """Два отказа, которые раньше читались одной строкой: контур не дал
        модели шанса (чинится триггер повтора) и повтор всё видел и не перебил
        (чинится промпт повтора). Без фактов повтора приписки нет: атрибуция не
        выдумывает решение контура по отсутствию записи."""
        after = {"no_unrouted": _check("no_unrouted", ids=["new_C"])}
        skipped = [{"op": "add_task", "id": "new_C", "stage": "plan"}]
        base = f"{attribution.OWNER_REFUSED}:план"
        owner = attribution.attribute_improvement({}, after, [], skipped)
        assert owner["no_unrouted"] == base
        for facts, tail in (({"retry_attempted": False, "retry_closed": 0},
                             " (корректирующий раунд не вызывался)"),
                            ({"retry_attempted": True, "retry_closed": 0},
                             " (повтор не закрыл ни одного отказа)"),
                            ({"retry_attempted": True, "retry_closed": 2}, ""),
                            # отсутствующий факт харнесс хранит как None — это
                            # не «повтор не вызывался»
                            ({"retry_attempted": None, "retry_closed": None}, "")):
            assert attribution.attribute_improvement(
                {}, after, [], skipped, report=facts)["no_unrouted"] == base + tail


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
        assert "ГЕНЕРАЦИЯ: КТО ПОРОДИЛ ДЕФЕКТЫ" in table
        assert "УЛУЧШЕНИЕ: КТО ПОРОДИЛ ДЕФЕКТЫ" in table
        assert "из " in table.split("КТО ПОРОДИЛ ДЕФЕКТЫ", 1)[1].split(
            "ПРОВЕНАНС")[0]
        assert "виноват" in table

    def test_the_two_contours_are_blamed_in_their_own_sections(self):
        """Одна сводка на генерацию и улучшение складывала «содержание
        фикстуры» и «дефект базовой схемы» в лидера с общим знаменателем — то
        есть в утверждение, которое не относится ни к одному из контуров.
        Сверяем секции с владельцами, посчитанными по данным прогона, а не по
        имени контура в тексте: иначе тест зависит от того, что там сейчас
        чинят в `core/*`."""
        from eval import harness

        report = harness.run(mode="replay", scenarios_spec="all", repeat=1)
        table = harness.render_table(report)
        _, rest = table.split("ГЕНЕРАЦИЯ: КТО ПОРОДИЛ ДЕФЕКТЫ", 1)
        generation, rest = rest.split("УЛУЧШЕНИЕ: КТО ПОРОДИЛ ДЕФЕКТЫ", 1)
        improvement = rest.split("ПРОВЕНАНС", 1)[0]

        def expected(cases):
            counts: Dict[str, int] = {}
            for case in cases:
                for owner in case.attribution.values():
                    counts[owner] = counts.get(owner, 0) + 1
            return counts

        def parsed(section):
            rows: Dict[str, int] = {}
            total = None
            for line in section.splitlines():
                left, sep, tail = line.strip().partition(" — ")
                if not sep or " из " not in tail:
                    continue
                count, _, count_total = tail.partition(" из ")
                rows[left] = int(count)
                total = int(count_total)
            return rows, total

        gen_rows, gen_total = parsed(generation)
        imp_rows, imp_total = parsed(improvement)
        assert gen_rows == expected(report.cases), generation
        assert imp_rows == expected(report.improve_cases), improvement
        assert gen_rows, "битые фикстуры обязаны оставить провалы генерации"
        assert gen_total == sum(gen_rows.values()) > 0
        assert (imp_total or 0) == sum(imp_rows.values())
        # Разные контуры — разные знаменатели и разные владельцы: в слитой
        # сводке «из 8» стояло бы на 7 провалов генерации и 1 провал улучшения.
        if imp_rows:
            assert gen_total != imp_total, (gen_total, imp_total)
            assert not set(gen_rows) & set(imp_rows)

    def test_improve_section_says_there_is_nothing_to_blame(self):
        """Пустой контур улучшения не обязан выдавать владельцев — но обязан
        сказать, что атрибутировать нечего, а не напечатать пустой блок."""
        from eval import harness

        report = harness.run(mode="replay", scenarios_spec="support_ticket")
        table = harness.render_table(report)
        assert "УЛУЧШЕНИЕ: КТО ПОРОДИЛ ДЕФЕКТЫ" in table
        section = table.split("УЛУЧШЕНИЕ: КТО ПОРОДИЛ ДЕФЕКТЫ", 1)[1].split(
            "ПРОВЕНАНС")[0]
        assert "атрибутировать нечего" in section


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

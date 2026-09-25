"""Контракт «подсказка исполнима»: совет правила проверяется аплайером.

Текстовый совет, который нельзя выразить пакетом операций, контур улучшения
читает как «придумай id», аплайер отвечает отказом, а `improve/op_acceptance`
записывает это качеством модели. Перепись `eval/advice.py` собирает пакет
ИСКЛЮЧИТЕЛЬНО из того, что подсказка назвала, и прогоняет его через продуктовый
гарант на реальных схемах корпуса — на одиннадцати фикстурах харнесса такие
формы (обмен концом на пул, развилка выше цикла, три ожидания в одном совете)
просто не встречаются.

Здесь закреплено то, что должно быть правдой всегда: названный рецепт
принимается аплайером, а разбор не теряет операции из-за скобок в имени пула.
Доля снятых нарушений — число измеримое и от корпуса зависит, поэтому в
утверждениях она снизу ограничена слабо, а подробности печатает отчёт.
"""

from __future__ import annotations

import pytest

from core.bpmn_scoring import BPMNScorer
from eval import advice
from eval.coverage import corpus_files


@pytest.fixture(scope="module")
def report():
    return advice.census(BPMNScorer(), corpus_files(), per_rule=8)


class TestParse:
    def test_recipe_parses_into_an_operation_with_operands(self):
        ops = advice.parse_recipes(
            "add_gateway(id='new_sla_fork_1', name='Ответ или срок?', "
            "gateway_type='parallel', after='A1', participant='Склад')")
        assert ops == [{"op": "add_gateway", "id": "new_sla_fork_1",
                        "name": "Ответ или срок?", "gateway_type": "parallel",
                        "after": "A1", "participant": "Склад"}]

    def test_a_pool_name_with_parentheses_does_not_eat_the_operation(self):
        """Signavio пишет имена пулов «Scoring (Bank)». Пока тело вызова
        разбиралось до первой скобки, вся операция исчезала из разбора, и
        перепись показывала «connect: цель не найдена» там, где просто не было
        `add_event` в пакете."""
        ops = advice.parse_recipes(
            "add_event(id='new_t', name='Срок', event_type='timer', "
            "participant='Scoring (Bank)'); connect(source='new_t', target='E')")
        assert [o["op"] for o in ops] == ["add_event", "connect"]
        assert ops[0]["participant"] == "Scoring (Bank)"

    def test_a_prose_operand_is_not_invented_into_a_value(self):
        """`connect(source=этот шлюз, …)` — именно тот текст, который модель
        не может превратить в операцию. Разбор обязан вернуть операцию без
        обязательного поля, а не додумать id."""
        ops = advice.parse_recipes("connect(source=этот шлюз, target='E1')")
        assert ops == [{"op": "connect", "target": "E1"}]

    def test_alternative_value_expands_into_variants(self):
        variants = advice._variants({"op": "add_event_definition", "id": "W",
                                     "event_definition": "message|error"})
        assert [v["event_definition"] for v in variants] == ["message", "error"]


class TestCorpusCensus:
    def test_census_reaches_every_rule_whose_advice_goes_to_the_model(
            self, report):
        assert set(report["summary"]) == set(advice.DEFAULT_RULES)
        assert all(s["n"] > 0 for s in report["summary"].values()), report["summary"]

    def test_a_named_recipe_is_always_accepted_by_the_applier(self, report):
        """Главный инвариант: если правило назвало операцию, пакет из неё
        применим. Назвать то, что аплайер отвергает, — тот же дефект текста,
        что и совет без операндов: модель тратит на него правку пакета, а
        `improve/defects_repaired` отвечает нулём."""
        bad = {rule: (s["named"], s["applied"])
               for rule, s in report["summary"].items()
               if s["applied"] != s["named"]}
        assert bad == {}

    def test_silence_about_a_recipe_is_always_an_explicit_limitation(
            self, report):
        """Если рецепта в тексте нет, подсказка обязана говорить почему: правка
        вне операций (условие ветке не-исключающего шлюза, ожидание без дуги),
        либо операнд выбирает модель (шаг пула при обмене концом на участника,
        ответ тому, у кого нет следующего шага). Молчание — дефект правила:
        контур читает его как «придумай id» и получает отказ аплайера."""
        unexplained = [(rule, verdict["text"][:160])
                       for rule, verdicts in report["per_case"].items()
                       for verdict in verdicts
                       if not verdict["named"]
                       and not advice.explains_no_recipe(verdict["text"])]
        assert unexplained == []
        assert all(s["silent"] == 0 for s in report["summary"].values())

    def test_following_the_advice_does_not_break_the_rules_that_stay_clean(
            self, report):
        """Для этих четырёх правил совет обязан быть не только применимым, но и
        безопасным: правка по подсказке не должна ронять другое правило. У
        `handoff_pingpong` и `approval_chain` по одному случаю на корпусе
        остаётся — они разобраны в плане, и тест их не прячет."""
        for rule in ("lane_overload", "wait_without_sla", "event_types",
                     "rework_loop"):
            assert report["summary"][rule]["broke"] == 0, rule

    def test_most_named_recipes_actually_clear_their_own_violation(self, report):
        """Нижняя граница, а не цель: остаток объясняется потолком
        `RECIPES_IN_MESSAGE` (совет называет три рецепта из N, остальное — той
        же формой), а не неприменимостью."""
        for rule, s in report["summary"].items():
            actionable = s["n"] - s["manual"]
            if not actionable:
                continue
            assert s["cleared"] >= actionable // 2, (rule, s)

    def test_report_lines_name_both_denominators(self, report):
        lines = advice.format_report(report)
        assert any("исполнимость подсказки" in line for line in lines)
        assert all("вне операций" in line or "%" in line for line in lines[1:])


# Правила, чей совет не называет ни операции, ни причины. Развёрнутая на все 22
# правила перепись началась с 14 таких, и в этом же проходе список опустел:
# сейчас каждое правило либо называет операнды из схемы, либо говорит, почему их
# назвать нельзя. Пустое множество — не «наследие», а потолок: молчаливое правило
# не может появиться незаметно, тест падает.
SILENT_ADVICE_RULES = frozenset()


@pytest.fixture(scope="module")
def full_report():
    scorer = BPMNScorer()
    return advice.census(scorer, corpus_files(), rules=tuple(scorer.rules),
                         per_rule=4)


class TestAllRulesRatchet:
    def test_no_rule_names_a_recipe_the_applier_refuses(self, full_report):
        bad = {rule: (s["named"], s["applied"])
               for rule, s in full_report["summary"].items()
               if s["applied"] != s["named"]}
        assert bad == {}

    def test_every_recipe_is_copy_pasteable_verbatim(self, full_report):
        """Операнд-перечисление (`event_definition='message|error|signal'`) —
        не рецепт, а подстановка: аплайер отвергает его как неизвестное
        определение, и перепись, которая пробует каждый вариант, засчитывает
        такую подсказку применимой, хотя модель, скопировавшая текст дословно,
        правку не приносит. Разница `named` и `verbatim` и есть та доля
        `improve/op_acceptance`, которую контур приписывал модели.

        Поэтому рецепт обязан называть ОДНО значение, а перечислять допустимые
        — словами вне вызова операции."""
        loose = {rule: (s["named"], s["verbatim"])
                 for rule, s in full_report["summary"].items()
                 if s["verbatim"] != s["named"]}
        assert loose == {}

    def test_a_listed_operand_is_reported_as_not_verbatim(self):
        """Сам измеритель: перепись по-прежнему пробует варианты (выбор остаётся
        автору), но обязана показывать, что дословно текст не применим, иначе
        «applied == named» снова станет самообманом."""
        scorer = _FakeScorer("add_event_definition(id='W', "
                             "event_definition='message|error')")
        verdict = advice.check_scheme(scorer, _FakeApplier(scorer),
                                      lambda before, after: {},
                                      "<xml/>", "event_types")
        assert verdict["applied"] and not verdict["verbatim"]

    def test_no_recipe_is_blocked_by_its_own_previous_round(self, full_report):
        """Второй круг починки берёт подсказку со СХЕМЫ, ПЕРВЫЙ КРУГ КОТОРОЙ уже
        изменил: id нового узла напечатан литералом — и правка второго круга
        получает «'new_gate' уже занят», а нарушение остаётся. Перепись видит
        это как отказ при применимом первом круге; по корпусу так стопорились
        `approval_chain` и `wait_without_sla`. Поэтому id нового узла
        подбирается по схеме, а не печатается словом."""
        blocked = [(rule, verdict["refused"][:80])
                   for rule, verdicts in full_report["per_case"].items()
                   for verdict in verdicts if "уже занят" in verdict["refused"]]
        assert blocked == []

    def test_no_recipe_offers_an_id_the_scheme_has_already_taken(self):
        """То же требование на базовой схеме: совет не вправе предлагать id
        нового узла, который на этой схеме уже есть (Signavio пишет свои
        `sid-…`, но `new_gate` встречается и в реальных файлах). Свободен
        именно id НОВОГО элемента; `flow`, `gateway` и `source` называются по
        существующим — для них занятость обязательна."""
        from core.bpmn_edits import ADD_NODE_OPS, is_bpmn_tag, parse_xml

        scorer = BPMNScorer()
        bad = []
        for name, xml in corpus_files():
            taken = {elem.get("id") for elem in parse_xml(xml).iter()
                     if is_bpmn_tag(elem.tag) and elem.get("id")}
            by_rule = scorer.evaluate(xml)["recommendations_by_rule"] or {}
            for rule, text in by_rule.items():
                for op in advice.parse_recipes(text):
                    if op.get("op") not in ADD_NODE_OPS:
                        continue
                    value = op.get("id")
                    if value and value in taken:
                        bad.append(f"{name} [{rule}]: {op['op']} "
                                   f"id='{value}' занят")
        assert bad == []


    def test_no_rule_advises_without_operands_or_reason(self, full_report):
        """Пустой список выше — это не «наследие, которое нельзя раздвинуть», а
        нулевая терпимость: правило вправе не называть операций, но обязано
        сказать почему."""
        silent = {rule for rule, s in full_report["summary"].items() if s["silent"]}
        assert silent <= SILENT_ADVICE_RULES, sorted(silent - SILENT_ADVICE_RULES)

    def test_the_census_reaches_every_rule_of_the_ruler(self, full_report):
        assert set(full_report["summary"]) == set(BPMNScorer().rules)

    def test_a_recipe_needing_more_rounds_than_the_census_says_it_so(self):
        """Нарушение, уходящее за 15 кругов (45 безымянных событий по три
        рецепта в сообщении), и совет, который застопорился, — разные находки.
        Без отдельного факта `rounds_to_clear = 0` читался бы как «рецепт не
        работает», хотя работает и просто не влезает в лимит замера."""
        scorer = _FakeScorer("add_event_definition(id='W', "
                             "event_definition='message')", rounds_needed=9)
        verdict = advice.check_scheme(scorer, _FakeApplier(scorer),
                                      lambda before, after: {}, "<xml/>",
                                      "event_types", rounds=5)
        assert verdict["applied"] and verdict["verbatim"]
        assert not verdict["cleared"] and verdict["exhausted"]
        assert verdict["rounds_to_clear"] == 0


class _FakeScorer:
    """Линейка из одной находки: текст подсказки задан из теста, а статус
    переключается после `rounds_needed` применимых пакетов — иначе перепись
    крутила бы круги починки до лимита и проверка не заканчивалась."""

    def __init__(self, text: str, rounds_needed: int = 1):
        self.text = text
        self.rounds_needed = rounds_needed
        self.applied_times = 0

    def note_applied(self) -> None:
        self.applied_times += 1

    def evaluate(self, xml: str) -> dict:
        return {"recommendations_by_rule": {"event_types": self.text},
                "details_meta": {"event_types": {
                    "status": "passed"
                    if self.applied_times >= self.rounds_needed else "failed"}}}


class _FakeApplier:
    """Аплайер-заглушка: значение-перечисление отвергает, названное значение
    принимает — ровно так же ведёт себя продуктовый `_op_add_event_definition`."""

    def __init__(self, scorer: _FakeScorer):
        self.scorer = scorer

    def __call__(self, xml: str, package, **kwargs):
        for op in package:
            for key, value in op.items():
                if key != "op" and isinstance(value, str) and "|" in value:
                    return xml, {"applied": [], "reverted": "",
                                 "skipped": [{"op": op.get("op"),
                                              "reason": f"неизвестное {key}"}]}
        self.scorer.note_applied()
        return xml, {"applied": [{"op": op.get("op")} for op in package],
                     "skipped": [], "reverted": ""}

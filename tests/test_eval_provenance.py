"""Провенанс набора кейсов: метрика не считается на схемах из few-shot промптов.

Здесь проверяются две разные вещи, и обе нужны:

* `TestDetector` — что детектор действительно находит подсовывание образца в
  набор (синтетические сценарии и фикстуры). Без них «чистый отчёт» мог бы
  означать просто слепую проверку.
* `TestEvalSetIsClean` — что нынешний набор кейсов чист: ни один сценарий и ни
  одна фикстура не пересекаются с тем, что модель видит в промпте как образец.
"""

from __future__ import annotations

import copy

import pytest

from core import bpmn_generator, llm_improve
from eval import harness, provenance
from eval.scenarios import Scenario, all_scenarios, get


def _scenario(**overrides) -> Scenario:
    base = dict(get("loan_application").__dict__)
    base.update(overrides)
    return Scenario(**base)


class TestDetector:
    def test_both_prompt_examples_are_parsed(self):
        samples = {s["id"]: s for s in provenance.examples()}
        assert set(samples) == {"generation_plan", "improve_package"}
        assert samples["generation_plan"]["payload"]["participants"]
        assert samples["improve_package"]["payload"]["operations"]
        # Имена образца — то, что модель способна переписать дословно.
        assert "Заменить деталь" in samples["generation_plan"]["names"]

    def test_lost_marker_blinds_the_audit_loudly(self, monkeypatch):
        """Промпт без маркера образца — не «пересечений нет», а «проверять
        нечего»: харнесс обязан сказать об этом прямо."""
        monkeypatch.setattr(bpmn_generator, "_SYSTEM_PROMPT",
                            "правила без примера")
        with pytest.raises(ValueError, match="нет маркеров образца"):
            provenance.examples()

    def test_scenario_written_like_the_example_is_flagged(self):
        sample = next(e for e in provenance.examples()
                      if e["id"] == "generation_plan")
        findings = provenance.scenario_findings(
            _scenario(text=sample["text"]), provenance.examples())
        assert any(f["kind"] == "описание как в образце" for f in findings)

    def test_expected_pool_named_in_the_example_is_flagged(self):
        findings = provenance.scenario_findings(
            _scenario(expected_participants=("Цех фасовки",)),
            provenance.examples())
        hit = [f for f in findings
               if f["kind"] == "требование скопировать имя пула"]
        assert [f["value"] for f in hit] == ["Цех фасовки"]

    def test_short_pool_name_is_checked_without_word_length_floor(self):
        """WMS короче словарного порога `words`, но именем пула быть может."""
        assert provenance.mentions("WMS", "пул называется WMS, а не система")
        assert not provenance.mentions("ИТ", "итоговая схема")
        assert provenance.mentions("Бюро кредитных историй",
                                   "запрос в бюро кредитных историй")
        assert not provenance.mentions("Бюро кредитных историй", "бюро")

    def test_improve_task_copied_from_the_example_is_flagged(self):
        sample = next(e for e in provenance.examples()
                      if e["id"] == "improve_package")
        findings = provenance.scenario_findings(
            _scenario(improve_prompt=sample["text"][:260]),
            provenance.examples())
        assert any(f["kind"] == "задача улучшения как в образце"
                   for f in findings)

    def test_fixture_that_rewrites_the_example_is_flagged(self):
        sample = next(e for e in provenance.examples()
                      if e["id"] == "generation_plan")
        plan = copy.deepcopy(sample["payload"])
        fixture = {"id": "x.good.plan", "scenario": "loan_application",
                   "kind": "plan", "plan": plan}
        findings = provenance.fixture_findings([fixture], [sample])
        assert findings and findings[0]["measure"] >= provenance.NAME_OVERLAP

    def test_containment_is_asymmetric_by_design(self):
        """Короткий запрос целиком живёт в длинном образце — это и есть утечка."""
        short = provenance.words("заведи отсчёт длительности замены")
        long = provenance.words("заведи отсчёт длительности замены, вилку "
                                "по ремфонду закрой парой шлюзов")
        assert provenance.containment(short, long) == 1.0
        assert provenance.containment(long, short) < 1.0


class TestEvalSetIsClean:
    def test_examples_are_still_in_the_prompts(self):
        """Образцы — часть промптов, а не мёртвые константы: без них харнесс
        проверял бы пустоту."""
        assert "Пример. " in bpmn_generator._SYSTEM_PROMPT
        assert "ПРИМЕР ОТВЕТА" in llm_improve._SYSTEM_PROMPT

    def test_no_scenario_overlaps_a_prompt_example(self):
        report = provenance.audit(all_scenarios(), [])
        assert report["findings"] == [], provenance.format_findings(report)

    def test_no_reference_fixture_duplicates_a_prompt_example(self):
        report = provenance.audit([], harness.load_fixtures())
        assert report["findings"] == [], provenance.format_findings(report)

    def test_example_domain_is_absent_from_scenario_texts(self):
        """Домен образца выбран так, чтобы ни одно содержательное имя шага не
        встречалось в описаниях сценариев."""
        names = set()
        for sample in provenance.examples():
            names |= {n.lower() for n in sample["names"]}
        for scenario in all_scenarios():
            shared = names & {w.lower() for w in
                              provenance._tokens(scenario.text)}
            assert not shared, (scenario.id, shared)

    def test_report_and_console_carry_the_provenance(self, tmp_path):
        run = harness.run(mode="replay", scenarios_spec="support_ticket",
                          fixtures_dir=harness.FIXTURES_DIR)
        assert run.provenance["clean"] is True
        assert run.as_dict()["provenance"]["findings"] == []
        assert "ПРОВЕНАНС КЕЙСОВ" in harness.render_table(run)
        assert "пересечений с few-shot образцами промптов нет" in \
            harness.render_table(run)

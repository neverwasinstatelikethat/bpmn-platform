"""Метрики подбора эталонов: математика рейтинга, ручная разметка тем и агрегаты.

Корпус в этих тестах не открывается: `find_best_practices` подставлен, потому
что проверяем мы разметку и арифметику, а не TF-IDF. Живой подбор смотрит
`python -m eval.retrieval` — он поднимает индекс и в CI не запускается.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from eval import corpus_topics, retrieval
from eval.scenarios import Scenario, all_scenarios


def scenario(scenario_id: str = "warehouse_delivery", **kw) -> Scenario:
    data: Dict[str, Any] = dict(id=scenario_id, title="Отгрузка на складе",
                                text="Кладовщик собирает груз, перевозчик везёт.",
                                min_steps=3, max_pools=2, must_branch=False,
                                must_have_timer=False)
    data.update(kw)
    return Scenario(**data)


class FakeKB:
    """Поиск с записанным ответом. `_metadata` и `_embeddings` — чтобы сводка
    честно докладывала про размер корпуса и доступную ветку."""

    def __init__(self, hits: Optional[List[Dict[str, Any]]] = None,
                 corpus: Optional[List[Dict[str, Any]]] = None,
                 embeddings: Any = None):
        self.hits = hits or []
        self._metadata = corpus if corpus is not None else (
            [{"name": "Dispatch-of-goods"}]
            + [{"name": f"Exercise_{i}"} for i in range(9)])
        self._embeddings = embeddings
        self.calls: List[tuple] = []

    def ensure_ready(self) -> None:
        self.calls.append(("ready",))

    def find_best_practices(self, query: str, xml_content: Optional[str] = None,
                            top_k: int = 3) -> List[Dict[str, Any]]:
        self.calls.append((query, xml_content, top_k))
        return self.hits[:top_k]


class TestRelevance:
    def test_topic_of_the_file_name_decides(self):
        s = scenario()
        assert retrieval.is_relevant(s, {"name": "Dispatch_of_Goods_abcd"})
        assert not retrieval.is_relevant(s, {"name": "Exercise_5_-_Credit_Scoring"})

    def test_the_label_is_not_read_from_the_classifier(self):
        """Тема эталона берётся из имени файла, а не из поля `domain`: его
        заполняет `BPMNKnowledgeBase._detect_domain` — тот же классификатор, что
        и размечает запрос. На нём метрика меряла бы согласие системы с самой
        собой, и промах подбора был бы неотличим от промаха классификатора."""
        s = scenario()
        assert retrieval.is_relevant(s, {"name": "Warenversand_x", "domain": "hr"})
        assert not retrieval.is_relevant(s, {"name": "timer_3", "domain": "logistics"})

    def test_notation_demo_is_never_a_topic_match(self):
        """`timer_5.bpmn` и `signal_3.bpmn` — демо элемента, а не процесс той же
        темы: под доменом классификатора они попадали в `general` и поднимали
        точность любого сценария, которому «подойдёт что угодно»."""
        for sid in retrieval.RELEVANT_TOPICS:
            assert not retrieval.is_relevant(scenario(sid), {"name": "gateways_2"})

    def test_unlabelled_scenario_is_not_relevant_to_anything(self):
        """Сценарий без разметки не должен «проходить» подбор на пустом месте:
        он выводится отдельной строкой `unlabelled`, а не нулём точности."""
        s = scenario("brand_new_case")
        assert retrieval.relevant_topics(s) is None
        assert not retrieval.is_relevant(s, {"name": "Dispatch-of-goods"})

    def test_the_needle_cases_are_actually_needles(self):
        """Разметка обязана соответствовать корпусу: если для складского
        сценария в корпусе больше половины эталонов, метрика ничего не мерит —
        она просто воспроизводит доминанту датасета."""
        warehouse = scenario()
        assert retrieval.relevant_topics(warehouse) == ("goods_dispatch",)
        share = retrieval.topic_share(corpus_topics.corpus_topics(),
                                      retrieval.relevant_topics(warehouse))
        assert share is not None and share < 0.2

    def test_labels_cover_every_scenario_of_the_set(self):
        """Тему обязан иметь каждый сценарий — и та, которой в корпусе нет:
        отсутствие эталона тогда читается как пробел датасета (`uncovered`), а
        не как забытая разметка харнесса."""
        assert not [s.id for s in all_scenarios()
                    if retrieval.relevant_topics(s) is None]

    def test_every_labelled_topic_exists_in_the_corpus_table(self):
        """Опечатка в имени темы обнулила бы кейс молча: размеченная тема либо
        встречается в таблице корпуса, либо заявлена отсутствующей явно."""
        known = {topic for _, topic in corpus_topics.TOPIC_KEYS}
        missing = {t for topics in retrieval.RELEVANT_TOPICS.values() for t in topics
                   if t not in known}
        assert missing <= set(retrieval.ABSENT_TOPICS), missing

    def test_the_topic_table_names_the_dataset_it_labels(self):
        """Покрытие ручной разметки: разрастание датасета новыми семействами
        обязано быть замеченным, а не тихо уронить точность."""
        topics = corpus_topics.corpus_topics()
        assert len(topics) >= 300
        unknown = [n for n, t in topics.items() if t == "unknown"]
        assert len(unknown) / len(topics) < 0.05, unknown


class TestPracticeCoverage:
    def test_scenario_needs_are_read_from_its_own_expectations(self):
        assert retrieval.needed_practices(
            scenario(must_have_timer=True, must_branch=True)) == ("parallel", "timer")
        # сценарий без заявленного ожидания по конструкции мерить нечем
        assert retrieval.needed_practices(scenario()) is None

    def test_coverage_counts_demonstrated_needs_of_the_returned_schemas(self):
        s = scenario(must_have_timer=True)
        hits = [{"name": "timer_1", "practices": [["timer", "Таймеры"]]},
                {"name": "Dispatch", "practices": [["boundary", "Граничные"]]}]
        assert retrieval.practice_coverage(s, hits) == pytest.approx(1.0)
        assert retrieval.practice_coverage(s, hits[1:]) == pytest.approx(0.0)

    def test_empty_return_is_zero_not_none(self):
        """Пустая выдача — худший случай подбора и для этого числа: модель не
        увидела ни одного примера нужной конструкции."""
        assert retrieval.practice_coverage(scenario(must_branch=True), []) == 0.0


class TestRankingMath:
    def test_precision_counts_only_the_top(self):
        ranked = [True, False, True, True]
        assert retrieval.precision_at_k(ranked, k=2) == pytest.approx(0.5)
        assert retrieval.precision_at_k(ranked, k=4) == pytest.approx(0.75)

    def test_empty_retrieval_is_not_zero_precision(self):
        """«Поиск ничего не нашёл» и «нашёл не то» — разные отказы: на нуле
        сломанный индекс выглядел бы как плохой подбор."""
        assert retrieval.precision_at_k([], k=3) is None
        assert retrieval.recall_at_k([], 5, k=3) is None
        assert retrieval.ndcg_at_k([], k=3) is None

    def test_recall_divides_by_the_corpus_not_by_the_answer(self):
        """Знаменатель — сколько релевантного вообще в корпусе: иначе в выдаче
        из k позиций отношение всегда даёт 1."""
        assert retrieval.recall_at_k([True, False, False], 3, k=3) == pytest.approx(1 / 3)
        assert retrieval.recall_at_k([True], None, k=3) is None

    def test_ndcg_prefers_the_same_relevant_on_the_first_position(self):
        assert (retrieval.ndcg_at_k([True, False, False], 3)
                > retrieval.ndcg_at_k([False, False, True], 3))

    def test_ndcg_of_a_perfect_order_is_one(self):
        assert retrieval.ndcg_at_k([True, True, False], 2) == pytest.approx(1.0)

    def test_mrr_reads_the_position_of_the_first_relevant(self):
        """MRR@k — заголовок метрики вместо recall@k: при k=3 и корпусе в
        несколько сотен эталонов recall заперт у нуля, а вот «первое попадание
        на первом месте» различимо."""
        assert retrieval.mrr_at_k([True, False, False], 3) == pytest.approx(1.0)
        assert retrieval.mrr_at_k([False, True, False], 3) == pytest.approx(0.5)
        assert retrieval.mrr_at_k([False, False, True], 3) == pytest.approx(1 / 3)
        # релевантный за окном k не засчитывается: контур читает три эталона
        assert retrieval.mrr_at_k([False, False, False, True], 3) == 0.0
        # пустая выдача — 0 (худший случай ранжирования), а None — «мерить нечего»
        assert retrieval.mrr_at_k([], 3) is None


class TestRun:
    def test_report_aggregates_and_names_the_branch(self):
        kb = FakeKB(hits=[{"name": "Dispatch-of-goods",
                           "practices": [["timer", "Таймеры"]]},
                          {"name": "Exercise_1"}, {"name": "Exercise_2"}],
                   corpus=[{"name": "Dispatch-of-goods"}]
                          + [{"name": f"Exercise_{i}"} for i in range(9)])
        report = retrieval.run(kb, [{"scenario": scenario(must_have_timer=True),
                                     "query": "добавь таймер", "xml": "<bpmn/>"}], k=3)
        assert report["corpus"] == 10
        assert report["semantic_branch"] is False
        case = report["cases"][0]
        assert case["precision@k"] == pytest.approx(1 / 3)
        assert case["recall@k"] == pytest.approx(1.0)
        assert case["relevant_in_corpus"] == 1
        assert case["topics"] == ["goods_dispatch", "tutorial"]
        # пример нужен не «на ту же тему», а с той конструкцией, которой не
        # хватает: она и доходит до модели как практика
        assert case["practice_coverage"] == pytest.approx(1.0)
        assert kb.calls[0] == ("ready",)
        assert kb.calls[1] == ("добавь таймер", "<bpmn/>", 3)

    def test_the_user_scheme_is_passed_to_the_search(self):
        """Контур всегда зовёт поиск с существующей схемой; метрика без схемы
        меряла бы вход, которого в продукте нет (лексическая ветка без имён
        шагов пустая ровно всегда)."""
        kb = FakeKB(hits=[{"name": "Dispatch-of-goods"}])
        retrieval.run(kb, [{"scenario": scenario(), "query": "q",
                            "xml": "<definitions/>"}])
        assert kb.calls[-1][1] == "<definitions/>"

    def test_case_without_a_single_relevant_schema_is_not_scored(self):
        """Пробел корпуса нельзя выдавать за плохой подбор."""
        kb = FakeKB(hits=[{"name": "Exercise_1"}],
                    corpus=[{"name": "Exercise_1"}])
        report = retrieval.run(kb, [{"scenario": scenario(), "query": "q",
                                     "xml": None}])
        assert report["uncovered"] == ["warehouse_delivery"]
        assert report["precision@k"] is None
        assert report["empty_share"] is None

    def test_unlabelled_scenario_is_reported_not_counted(self):
        kb = FakeKB(hits=[{"name": "Exercise_1"}])
        report = retrieval.run(kb, [{"scenario": scenario("mystery_case"),
                                     "query": "q", "xml": None}])
        assert report["unlabelled"] == ["mystery_case"]
        assert report["precision@k"] is None


class TestBuildCases:
    def test_base_plan_reference_resolves_to_a_scheme(self):
        """`base_plan` improvement-фикстуры — id плановой фикстуры: без
        разрешения ссылки кейс остался бы запросом без схемы."""
        fixtures = [
            {"id": "wh.good.plan", "kind": "plan",
             "plan": {"participants": ["Склад"], "elements": []}},
            {"id": "wh.live.improve", "kind": "improve", "scenario": "warehouse_delivery",
             "base_plan": "wh.good.plan", "prompt": "добавь таймер SLA"},
        ]
        cases = retrieval.build_cases([scenario()], fixtures,
                                      to_xml=lambda plan: "<xml/>")
        assert len(cases) == 1
        assert cases[0]["xml"] == "<xml/>"
        assert cases[0]["query"] == "добавь таймер SLA"

    def test_a_broken_serializer_leaves_the_case_without_a_scheme(self):
        def boom(plan):
            raise ValueError("нет генератора")
        cases = retrieval.build_cases(
            [scenario()],
            [{"id": "p", "kind": "plan", "plan": {}},
             {"id": "i", "kind": "improve", "scenario": "warehouse_delivery",
              "base_plan": "p", "prompt": "q"}], to_xml=boom)
        assert cases[0]["xml"] is None

    def test_a_scenario_without_an_improve_fixture_still_gives_a_case(self):
        """Двух improve-фикстур на наборе мало: `P@3` по двум случаям не отличает
        подбор по теме от везения. Сценарий без записанного запроса даёт кейс
        «текст задачи + эталонная схема», и оба источника помечены `origin`,
        чтобы синтетическая половина не притворялась записанной."""
        loan = scenario("loan_application", title="Кредитная заявка")
        fixtures = [
            {"id": "wh.good.plan", "kind": "plan", "scenario": "warehouse_delivery",
             "quality": "good", "plan": {"participants": ["Склад"], "elements": []}},
            {"id": "wh.live.improve", "kind": "improve",
             "scenario": "warehouse_delivery", "base_plan": "wh.good.plan",
             "prompt": "добавь таймер SLA"},
            {"id": "loan.good.plan", "kind": "plan", "scenario": "loan_application",
             "quality": "good", "plan": {"participants": ["Банк"], "elements": []}},
            # битая плановая фикстура эталоном не становится и кейс не рождает
            {"id": "pr.bad.plan", "kind": "plan", "scenario": "product_return",
             "quality": "bad", "plan": {"participants": ["Склад"], "elements": []}},
        ]
        cases = retrieval.build_cases([scenario(), loan,
                                       scenario("product_return", title="Возврат")],
                                      fixtures, to_xml=lambda plan: "<xml/>")
        by_id = {c["scenario"].id: c for c in cases}
        assert by_id["warehouse_delivery"]["origin"] == "improve"
        assert by_id["loan_application"]["origin"] == "etalon"
        # сценарий с записанным запросом не удваивается синтетическим кейсом
        assert len([c for c in cases if c["scenario"].id == "warehouse_delivery"]) == 1
        assert by_id["loan_application"]["query"] == loan.text
        assert set(by_id) == {"warehouse_delivery", "loan_application"}

    def test_run_reports_mrr_and_splits_the_aggregate_by_origin(self):
        """Сводка по двум источникам кейсов обязана оставаться разбираемой:
        если `improve` и `etalon` разошлись, одно общее число ничего не
        утверждает."""
        hits = [{"name": "Exercise_1"}, {"name": "Dispatch-of-goods"},
                {"name": "Credit_Scoring_1"}]
        corpus = ([{"name": f"Exercise_{i}"} for i in range(8)]
                  + [{"name": "Dispatch-of-goods"}, {"name": "Credit_Scoring_1"}])
        kb = FakeKB(hits=hits, corpus=corpus)
        report = retrieval.run(kb, [{"scenario": scenario("loan_application"),
                                     "query": "q", "xml": "<b/>", "origin": "improve"},
                                    {"scenario": scenario(),
                                     "query": "q", "xml": "<b/>", "origin": "etalon"}],
                               k=3)
        # loan_application (credit_scoring/banking): попадание на 3-й → MRR 1/3;
        # warehouse_delivery (goods_dispatch): единственное попадание на 2-й → 0.5
        assert report["mrr@k"] == pytest.approx((1 / 3 + 0.5) / 2)
        assert report["by_origin"]["improve"]["n"] == 1
        assert report["by_origin"]["etalon"]["precision@k"] == pytest.approx(1 / 3)
        assert retrieval.format_report(report)


def test_format_report_survives_none_values():
    lines = retrieval.format_report(
        retrieval.run(FakeKB(hits=[]), [{"scenario": scenario(), "query": "q",
                                         "xml": None}]))
    assert any("—" in line for line in lines)

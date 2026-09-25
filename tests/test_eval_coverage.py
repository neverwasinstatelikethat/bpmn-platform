"""Покрытие детекции на реальных схемах: линейка не должна слепо молчать.

Корпус датасета читается целиком и один раз (module fixture): скоринг 367 файлов
стоит секунды, а смысл этих проверок ровно в полноте выборки — на десяти файлах
правило, измеряющее не те узлы, ещё может выглядеть живым.
"""

from __future__ import annotations

import pytest

from core.bpmn_scoring import BPMNScorer
from eval import coverage


@pytest.fixture(scope="module")
def sample():
    files = coverage.corpus_files()
    assert len(files) > 300, "корпус не читается — проверке не на чем быть"
    return files


@pytest.fixture(scope="module")
def report(sample):
    return coverage.census(BPMNScorer(), [xml for _, xml in sample],
                           tags_of=coverage.tags_of)


@pytest.fixture(scope="module")
def agreement(sample):
    return coverage.agreement_census(BPMNScorer(), [xml for _, xml in sample],
                                     names=[name for name, _ in sample])


class TestCensus:
    def test_census_covers_every_rule_of_the_ruler(self, report):
        scorer = BPMNScorer()
        assert report["schemes"] > 300
        assert set(scorer.rules) <= set(report["rules"])
        for rule, bucket in report["rules"].items():
            assert bucket["failed"] + bucket["passed"] + bucket["not_applicable"] \
                == report["schemes"], rule

    def test_rules_that_must_find_something_on_real_schemes_are_not_blind(
            self, report):
        """Слепое пятно, найденное этим замером: `approval_chain` считал только
        `userTask` внутри названной дорожки и на 367 рукописных схемах не
        сработал ни разу; `naming` и `documentation` ругались на события и
        шлюзы, которых их собственный совет не касается."""
        silent = [rule for rule in coverage.MUST_FIRE_ON_CORPUS
                  if not report["rules"].get(rule, {}).get("failed")]
        assert silent == []

    def test_dead_rules_are_named_not_swallowed(self, report):
        """Правило, которое на реальном корпусе молчит, обязано быть названо:
        это либо оправданный случай (у схемы нет таких элементов), либо мёртвый
        код — и различать их должен читающий отчёт, а не забывший про правило."""
        dead = coverage.dead_rules(report)
        assert set(dead) & set(coverage.MUST_FIRE_ON_CORPUS) == set()
        assert "boundary_events" in dead  # событий этого рода в датасете нет

    def test_recalibrated_rules_charge_only_the_nodes_their_advice_edits(
            self, report):
        """`naming` (`rename`) и `documentation` (`add_documentation`) правят
        шаги. Нарушителями могут быть только активности — иначе правило
        требует правки, которую аплайер на этот узел не примет."""
        for rule in ("naming", "documentation"):
            assert coverage.offenders_are_activities(report, rule) is True, (
                report["offenders_by_tag"].get(rule))

    def test_approval_chain_now_reaches_the_hand_drawn_cases(self, report):
        """Цепочка согласований в процессе без дорожек — то же нарушение, что в
        одной названной дорожке: маршрут тот же, неразличимы только исполнители.
        До fall-back'а на корпусе срабатываний было ноль."""
        assert report["rules"]["approval_chain"]["failed"] > 0

    def test_format_report_is_readable_on_the_real_sample(self, report):
        lines = coverage.format_report(report, list(BPMNScorer().rules))
        assert lines[0].startswith("схем в выборке:")
        assert any("ни разу не сработало" in line for line in lines)


class TestAgreement:
    """Сверка линейки с независимым оракулом на всём корпусе.

    Это единственная проверка скоринга, которой не нужна человеческая разметка
    «где правильно»: два читателя процесса читают один XML независимо, и их
    расхождение — данные, а не мнение. Порог здесь не удерживается (он был бы
    наказанием за чужое чтение схемы), удерживается направление: скоринг не
    имеет права прощать то, что оракул называет дефектом.
    """

    def test_the_ruler_never_forgives_what_the_oracle_sees(self, agreement):
        forgiven = {rule: bucket for rule, bucket in agreement["by_rule"].items()
                    if bucket.get("forgiven") or bucket.get("unseen")}
        assert forgiven == {}, agreement["witnesses"]

    def test_divergences_on_real_schemes_are_the_documented_ones(self, agreement):
        """`lane_overload` расходится из-за порогов (60% при трёх дорожках против
        75% без калитки), `rework_loop` — из-за места вопроса (ноги развилки у
        оракула против дуг, отпускающих цикл, у скоринга). Оба задокументированы
        в docstrings; новое имя в этом списке значит, что разъехались линейки."""
        assert coverage.unexplained(agreement) == []
        assert set(coverage.disagreements(agreement)) == {"lane_overload",
                                                         "rework_loop"}

    def test_the_schemas_that_diverge_are_named_by_file(self, agreement):
        """Расхождение без адреса не разобрать: номер в отсортированном списке
        файлом не является."""
        assert all(witnesses and all(name.endswith(".bpmn") for name in witnesses)
                   for witnesses in agreement["witnesses"].values())

    def test_the_census_is_fed_the_whole_answer_not_only_the_meta(self, sample):
        """`business_agreement` ждёт ответ `evaluate` целиком. `details_meta`
        подсовывать нельзя: он не проходит ни по одному из разобранных форматов
        и каждый статус в нём читается как «passed», из-за чего сверка
        рапортовала бы, что линейка простила все нарушения сразу."""
        scorer = BPMNScorer()
        hit = next(xml for _, xml in sample
                   if scorer.evaluate(xml)["details_meta"]["wait_without_sla"]["status"]
                   == "failed")
        bucket = coverage.agreement_census(scorer, [hit])["by_rule"]["wait_without_sla"]
        assert bucket["agree"] == 1
        assert not bucket.get("forgiven")

    def test_format_report_prints_the_second_measurement(self, report, agreement):
        lines = coverage.format_report(report, list(BPMNScorer().rules), agreement)
        assert any("сверка двух прочтений" in line for line in lines)
        assert any(line.strip().startswith("кроме объявленных в замысле:")
                   for line in lines)


@pytest.fixture(scope="module")
def notation(sample):
    return coverage.agreement_census(
        BPMNScorer(), [xml for _, xml in sample],
        names=[name for name, _ in sample], pairs=coverage.NOTATION_PAIRS)


class TestNotationAgreement:
    """Нотационный слой сверяется тем же механизмом, что и бизнес-слой.

    До этой сверки оракул расходился со скорингом в местах, о которых сводка
    молчала: 35 схем корпуса с обменом, конец которого висит на участнике
    целиком (оракул объявлял такой пул немым, хотя BPMN это разрешает, а
    `participant_interacts` стоит в гейте `pass@1/scenario`), и 2 схемы, где
    поток без цели засчитывался выходом узла и тупик прятался до первой
    починки. Разница между «слой прощает» и «слой туда не смотрит» видна
    только по всему корпусу: на фикстурах такие концы не рисуются.
    """

    def test_notation_layer_diverges_only_where_it_is_documented(self, notation):
        assert coverage.unexplained(notation) == []
        # Объявленных расхождений в нотационном слое нет: единственная пара,
        # где они были, оказалась разными прочтениями нотации, а не порогами,
        # и исправлена в обоих слоях.
        assert coverage.disagreements(notation) == []

    def test_the_blind_spots_found_by_this_census_are_closed(self, notation):
        for rule in ("participant_interacts", "no_isolated", "gateway_split_join"):
            counts = notation["by_rule"][rule]
            assert counts.get("diverged", 0) == 0, (rule, counts)

    def test_every_notation_rule_of_the_pair_table_is_reached(self, notation):
        assert set(notation["by_rule"]) == set(coverage.NOTATION_PAIRS)
        for rule, counts in notation["by_rule"].items():
            assert counts["agree"] + counts["diverged"] == notation["schemes"], rule


class TestAdviceTextHasNoEmptyOperands:
    def test_no_advice_prints_an_empty_operand(self, sample):
        """Операнд, напечатанный пустым (`«вставлять его в дугу ''→''»`),
        читается моделью как испорченный текст, а не как «операнда нет»: либо
        она копирует кавычки, либо теряет смысл правки. Правило обязано назвать
        то, что о схеме знает, а чего не знает — сказать словами, без кавычек.

        На корпусе так протекало `pool_has_steps`: 36 схем, у пула ни дуги
        старт→финиш, ни соседа, а шаблон всё равно подставлял пустые id."""
        import re

        scorer = BPMNScorer()
        empty_operand = re.compile(r"''|='\s*\"")
        leaks = []
        for name, xml in sample:
            by_rule = scorer.evaluate(xml)["recommendations_by_rule"] or {}
            for rule, text in by_rule.items():
                for frag in set(empty_operand.findall(text)):
                    leaks.append(f"{name} [{rule}]: пустой операнд {frag!r}")
        assert leaks == []

    def test_advice_that_denies_an_operation_does_not_list_one(self, sample):
        """«правка не выражается операцией … → чинится: add_event» — это не
        подсказка, а противоречие в одном предложении: модель читает хвост со
        списком операций как разрешение выдумать операнд, и единственный
        корректирующий повтор уходит на правку, которую аплайер отвергнет.

        Хвост остаётся там, где совет называет вызов операции для ОДНОГО
        нарушения и отказывается от правки для другого (смесь): там список
        операций справедлив. Запрещён только случай, когда вызова в тексте нет
        вовсе, а правило прямо сказало, что операции не бывает."""
        import re

        from core.bpmn_scoring import MANUAL_FIX_MARKERS

        scorer = BPMNScorer()
        call = re.compile(r"\b[a-z][a-z_]{2,}\(")
        contradictions = []
        for name, xml in sample:
            for rule, text in (scorer.evaluate(xml)["recommendations_by_rule"]
                               or {}).items():
                if (any(m in text for m in MANUAL_FIX_MARKERS)
                        and "→ чинится:" in text and not call.search(text)):
                    contradictions.append(f"{name} [{rule}]")
        assert contradictions == []


class TestMessageFlowLegsOnCorpus:
    """Корпус сверяет новое ограничение с привычкой живых модельеров.

    Правило «развилка не бывает концом обмена» линейка получила не из корпуса, а
    из собственного совета: `cross_pool_flow` переводил межпуловую дугу из
    развилки в обмен с `source='<шлюз>'`. Значит, надо держать одновременно два
    факта — что корпус таких концов не знает (иначе правило ругало бы рукописные
    схемы, а не правку контура) и что совет больше их не печатает.
    """

    def test_no_corpus_message_flow_ends_on_a_gateway(self, sample):
        from core import bpmn_scoring as bs
        from core.bpmn_edits import parse_xml
        counted = 0
        offenders = []
        for name, xml in sample:
            schema = bs._Schema(parse_xml(xml))
            counted += len(schema.by_tag["messageFlow"])
            if schema.gateway_messages:
                offenders.append(name)
        assert counted > 500, "корпус без обменов — проверке не на чем быть"
        assert offenders == [], offenders

    def test_cross_pool_advice_never_ends_a_message_on_a_gateway(self, sample):
        """Ни один разобранный рецепт `cross_pool_flow` не ставит шлюз концом
        обмена: следом правки обязан оставаться законный `messageFlow`."""
        from core import bpmn_scoring as bs
        from core.bpmn_edits import parse_xml
        from eval import advice
        scorer = BPMNScorer()
        offenders = []
        checked = 0
        for name, xml in sample:
            text = (scorer.evaluate(xml).get("recommendations_by_rule") or {}).get(
                "cross_pool_flow") or ""
            for package in advice._packages(text)[:1]:
                schema = bs._Schema(parse_xml(xml))
                for op in package:
                    if op.get("op") != "connect":
                        continue
                    checked += 1
                    for ref in (op.get("source"), op.get("target")):
                        node = schema.node_by_id.get(ref or "")
                        if node is not None and node.tag.endswith("Gateway"):
                            offenders.append((name, ref))
        assert checked, "на корпусе нет ни одного разобранного рецепта — проверке не на чем быть"
        assert offenders == [], offenders


class TestDenialMatchesTheApplier:
    """Отказ совета от операции обязан совпадать с тем, что правда отвергает аплайер.

    Проверка родилась из перепроверки подозрения: `start_event` на корпусе
    ругался «правка не выражается операцией» в 8 выборках переписи, и это
    читалось как недооценка инструмента — `add_event` ведь умеет ставить старт в
    пул по `participant`. Замер дал обратное: из 49 пулов без старта 37 не имеют
    ни одного шага, и аплайер на них отвечает `пул не определён`, а для 12 пулов
    с шагами совет и сейчас называет `add_event(...) + connect(...)`. То есть
    отказ честный. Рatchet нужен, чтобы он таким и остался: молчание про
    исполнимую правку стоит модели корректирующий раунд, а ложный отказ —
    потерянное улучшение.
    """

    DENIAL = "не выражается операцией"

    def test_start_event_denies_only_pools_without_steps(self, sample):
        """Каждому нарушителю — его строка: пулу с шагом рецепт, пустому — отказ.

        Проверка родилась из перепроверки подозрения: `start_event` на корпусе
        ругался «правка не выражается операцией», и это читалось как недооценка
        инструмента — `add_event` ведь умеет ставить старт по `participant`.
        Замер дал обратное: аплайер на пул без шагов отвечает `пул не определён`,
        а голый старт без ноги тут же заряжает `no_isolated`, поэтому рецепт
        обязан идти с `connect`. Значит, отказ честен ровно там, где в пуле нет
        ни одного шага, — и только там.
        """
        from core import bpmn_scoring as bs
        from core.bpmn_scoring import ACTIVITY_TAGS, _local
        from core.bpmn_edits import parse_xml
        scorer = BPMNScorer()
        denied = 0
        named = 0
        wrong = []
        for name, xml in sample:
            text = (scorer.evaluate(xml).get("recommendations_by_rule") or {}).get(
                "start_event") or ""
            if not text:
                continue
            segments = {seg.split("'")[1]: seg
                        for seg in text.split("; ")
                        if seg.startswith("'") and "'" in seg[1:]}
            schema = bs._Schema(parse_xml(xml))
            for participant in schema.participants:
                pid = participant.get("id") or ""
                nodes = schema.nodes_of_participant(participant)
                if any(_local(e.tag) == "startEvent" for e in nodes):
                    continue
                seg = segments.get(pid)
                if seg is None:
                    continue  # вне потолка `RECIPES_IN_MESSAGE` id в тексте
                has_steps = any(_local(e.tag) in ACTIVITY_TAGS for e in nodes)
                if has_steps:
                    named += 1
                    if "add_event(" not in seg:
                        wrong.append((name, pid, "пул со шагами, а рецепта нет"))
                else:
                    denied += 1
                    if self.DENIAL not in seg:
                        wrong.append((name, pid, "пустой пул, а отказа нет"))
        assert named >= 1 and denied >= 1,             "корпус без обоих случаев — проверке не на чем быть"
        assert wrong == [], wrong

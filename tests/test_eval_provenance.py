"""Провенанс набора кейсов: метрика не считается на схемах из few-shot промптов.

Здесь проверяются три разные вещи, и все три нужны:

* `TestDetector` — что детектор действительно находит подсовывание образца в
  набор (синтетические сценарии и фикстуры). Без них «чистый отчёт» мог бы
  означать просто слепую проверку.
* `TestPromptSampleRegistry` — что в реестр `examples()` не может провалиться
  новый промпт с образцом: харнесс слепнет не только когда образец исчез, но и
  когда его перестали искать.
* `TestEvalSetIsClean` — что нынешний набор кейсов не добавляет пересечений
  сверх тех, что разобраны и названы в планке.
"""

from __future__ import annotations

import copy
import re

import pytest

from core import bpmn_edits, bpmn_generator, bpmn_scoring, llm_improve
from eval import harness, provenance
from eval.scenarios import Scenario, all_scenarios, get


def _scenario(**overrides) -> Scenario:
    base = dict(get("loan_application").__dict__)
    base.update(overrides)
    return Scenario(**base)


# Известные маркеры примера ответа в промптах — те же строки, что читает
# `examples()`.
SAMPLE_MARKERS = ("ПРИМЕР ОТВЕТА", "Пример. ", "Пример («")
# Форма, в которой промпт отдаёт модели ответ: план генерации и пакет правок.
SAMPLE_JSON_LIST = re.compile(r'"(operations|elements)"\s*:\s*\[')


def sample_bearing_prompts(module) -> dict:
    """Атрибуты модуля, в которых модель читает образец ответа.

    Правило намеренно узкое, иначе оно бы выло на каждом упоминании полей:
    строка считается образцом, если в ней есть либо известный маркер примера
    (`ПРИМЕР ОТВЕТА`, `Пример. `, `Пример («`), либо JSON-массив `operations` или
    `elements` — именно так в промпт ложатся план генерации и пакет правок.
    Формулировки вида «ответ — только JSON с полями "analysis" и "operations"»
    под правило не подпадают: там после имени поля нет двоеточия со списком, и
    ни одного имени модель из такой строки не копирует. Так же не считаются
    словари и схемы, которые в промпт только вклеиваются, — их `examples()`
    регистрирует по месту вклейки (`_RETRY_TEMPLATE`, `core.bpmn_edits.OP_SPEC`).
    """
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, str)
        and (any(marker in value for marker in SAMPLE_MARKERS)
             or SAMPLE_JSON_LIST.search(value))
    }


def unregistered_samples(registered) -> set:
    """Промпты с образцом, которых нет в реестре `examples()`.

    Сверка идёт по `where`: промпт-атрибут модуля покрыт только тогда, когда на
    него в реестре заведена запись с разобранным образцом. Обратное направление
    (зарегистрировано, а образца в модуле уже нет) ловит сам `examples()` — он
    падает на пропавшем маркере.
    """
    out = set()
    for module in (bpmn_generator, llm_improve):
        out |= {f"{module.__name__}.{name}"
                for name in sample_bearing_prompts(module)
                if f"{module.__name__}.{name}" not in registered}
    return out


class TestDetector:
    def test_every_prompt_example_is_parsed(self):
        samples = {s["id"]: s for s in provenance.examples()}
        assert set(samples) == {"generation_plan", "improve_package",
                                "roster_composition", "retry_patch",
                                "flow_route_rules", "scoring_rules",
                                "op_spec", "rag_practices"}
        assert samples["generation_plan"]["payload"]["participants"]
        assert samples["improve_package"]["payload"]["operations"]
        # Имена образца — то, что модель способна переписать дословно.
        assert "Заменить деталь" in samples["generation_plan"]["names"]
        # Промпт состава отдаёт модели и правила, и ответ примера: и то, и то
        # она способна переписать, поэтому имена снимаются с разобранного
        # ответа, а не с напечатанной рядом схемы ответа.
        assert samples["roster_composition"]["payload"]["roles"]
        assert "Цех фасовки" in samples["roster_composition"]["names"]
        # Схема заплатки тоже называет имена: модель читает их как подсказку
        # ответа, и «Перевозчик» с «Водителем» в этом месте были ответом сцены.
        assert samples["retry_patch"]["payload"]["participants"]
        assert "Сервисная служба" in samples["retry_patch"]["names"]
        # Статические тексты, которые промпт улучшения вклеивает в себя:
        # примером ответа они не притворяются, но имя, вписанное в правило или в
        # форму операции, модель вернёт в схему так же.
        assert samples["scoring_rules"]["rules_only"]
        assert "чинится: merge_participants" in samples["scoring_rules"]["text"]
        assert samples["op_spec"]["rules_only"]
        assert '"op":"add_task"' in samples["op_spec"]["text"]
        # Практики корпуса зависят от запроса, поэтому это не текст, а
        # отложенная выборка — её проверяют тесты ниже.
        assert samples["rag_practices"]["rules_only"]
        assert callable(samples["rag_practices"]["retrieve"])

    def test_retry_schema_naming_an_expected_pool_is_caught(self, monkeypatch):
        """Промпт переспроса — четвёртый образец, и он проверяется наравне с
        остальными: правка схемы заплатки не должна возвращать в него имена из
        ответов оракула."""
        monkeypatch.setattr(bpmn_generator, "_RETRY_TEMPLATE",
                            str(bpmn_generator._RETRY_TEMPLATE)
                            + '\n"participants": [{"name": "Перевозчик"}]')
        report = provenance.audit(
            [s for s in all_scenarios() if s.id == "warehouse_delivery"], [])
        hits = [f for f in report["findings"] if f["example"] == "retry_patch"]
        assert hits and hits[0]["value"] == "Перевозчик", hits

    def test_roster_prompt_naming_an_expected_pool_is_caught(self, monkeypatch):
        """Правило состава обязано быть не по имени домена: «перевозчик» в правиле —
        это подсказка ответа сцены `warehouse_delivery`, а не способность
        контура. Так промпт правился один раз, и проверка это ловит."""
        monkeypatch.setattr(bpmn_generator, "_ROSTER_SYSTEM_PROMPT",
                            bpmn_generator._ROSTER_SYSTEM_PROMPT
                            + '\nпример: «перевозчик»')
        report = provenance.audit(
            [s for s in all_scenarios() if s.id == "warehouse_delivery"], [])
        hits = [f for f in report["findings"]
                if f["example"] == "roster_composition"]
        assert [f["value"] for f in hits] == ["Перевозчик"], hits

    def test_flow_prompt_naming_an_expected_pool_is_caught(self, monkeypatch):
        """Правила стадии маршрута модель читает целиком — имя из ответа сцены
        в них работает так же, как в промпте состава: правка «ждать — не значит
        делать» обязана проверяться на утечку, а не проверять её глазами."""
        monkeypatch.setattr(bpmn_generator, "_FLOW_SYSTEM_PROMPT",
                            bpmn_generator._FLOW_SYSTEM_PROMPT
                            + '\nпример: «перевозчик»')
        report = provenance.audit(
            [s for s in all_scenarios() if s.id == "warehouse_delivery"], [])
        hits = [f for f in report["findings"]
                if f["example"] == "flow_route_rules"]
        assert [f["value"] for f in hits] == ["Перевозчик"], hits

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
        # «Цех фасовки» — организатор и в плане-образце, и в правиле состава, и
        # в правиле стадии маршрута (оно читается моделью целиком и включает
        # промпт генерации): все три поверхности обязаны попасть в находки.
        assert {f["value"] for f in hit} == {"Цех фасовки"}
        assert {f["example"] for f in hit} == {"generation_plan",
                                              "roster_composition",
                                              "flow_route_rules"}

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
        findings = provenance.fixture_findings([fixture], provenance.examples())
        assert findings and findings[0]["measure"] >= provenance.NAME_OVERLAP

    def test_improve_fixture_copied_from_the_package_example_is_flagged(self):
        """Слепое пятно, которого здесь не хватало: план-образец и пакет
        правок именами не пересекаются, поэтому improve-фикстура, переписанная
        из `ПРИМЕР ОТВЕТА`, проходила аудит незамеченной — её имена сверялись
        только с образцом генерации (и не совпадали с ним законно)."""
        samples = provenance.examples()
        sample = next(e for e in samples if e["id"] == "improve_package")
        fixture = {"id": "warehouse_delivery.copied.improve", "kind": "improve",
                   "scenario": "warehouse_delivery",
                   "prompt": "заведи отсчёт длительности замены",
                   "operations": copy.deepcopy(sample["payload"]["operations"])}
        findings = provenance.fixture_findings([fixture], samples)
        assert len(findings) == 1, findings
        assert findings[0]["kind"] == "пакет = образец промпта улучшения"
        assert findings[0]["example"] == "improve_package"
        assert findings[0]["measure"] >= provenance.NAME_OVERLAP
        # И насквозь: аудит по набору обязан назвать фикстуру, а не только
        # внутренняя функция.
        report = provenance.audit([], [fixture])
        assert [f["value"] for f in report["findings"]] == [
            "warehouse_delivery.copied.improve"], report["findings"]

    def test_rag_ethalon_of_the_same_process_is_flagged(self, monkeypatch):
        """Если поиск по корпусу приносит в промпт эталон того же процесса,
        который измеряет кейс, метрика мерит воспроизведение ответа."""
        def fake(scenario):
            return [{"query": scenario.improve_prompt,
                     "block": "ЛУЧШИЕ ПРАКТИКИ: [logistics] таймеры",
                     "hits": [{"name": "warehouse_delivery", "similarity": 1.0},
                              {"name": "Dispatch_of_goods_bc722883a18e42f1"
                                       "bf5e53626e77a811", "similarity": 0.9}]}]

        monkeypatch.setattr(provenance, "rag_surfaces", fake)
        findings = provenance.scenario_findings(get("warehouse_delivery"),
                                                provenance.examples())
        hits = [f for f in findings if f["example"] == "rag_practices"]
        assert [f["value"] for f in hits] == ["warehouse_delivery"], hits
        assert hits[0]["kind"] == "в промпт пришёл эталон этого же процесса"

    def test_rag_block_naming_an_expected_pool_is_flagged(self, monkeypatch):
        """Имя пула из блока практик — та же подсказка ответа, что и имя из
        правила: сверяется `mentions`, а не доля совпавших слов (блок обязан
        пересекаться с любой лексикой процесса)."""
        block = "- [logistics] Таймеры SLA (в эталонах: Перевозчик отгружает)"

        def fake(scenario):
            # два запроса (задача улучшения и описание) с одним блоком: находка
            # про один факт утечки обязана прозвучать один раз
            return [{"query": q, "block": block, "hits": []}
                    for q in (scenario.improve_prompt, scenario.text)]

        monkeypatch.setattr(provenance, "rag_surfaces", fake)
        findings = provenance.scenario_findings(get("warehouse_delivery"),
                                                provenance.examples())
        hits = [f for f in findings
                if f["example"] == "rag_practices"
                and f["kind"] == "требование скопировать имя пула"]
        assert [f["value"] for f in hits] == ["Перевозчик"], hits

    ETALON = ["Поступила заявка на отгрузку", "Сформировать заявку в WMS",
              "Забронировать паллету и проверить остатки",
              "Согласовать окно доставки с заказчиком",
              "Отгрузить товар по накладной"]

    def _by_content(self, monkeypatch, block_text, etalon):
        def fake(scenario):
            return [{"query": scenario.improve_prompt, "block": block_text,
                     "hits": [{"name": "Warenversand_bc722883a18e42f1",
                               "similarity": 0.8}]}]

        monkeypatch.setattr(provenance, "rag_surfaces", fake)
        return [f for f in provenance.scenario_findings(
            get("warehouse_delivery"), provenance.examples(), etalon)
            if f["example"] == "rag_practices"
            and "эталона кейса" in f["kind"]]

    def test_rag_block_reproducing_etalon_steps_is_flagged(self, monkeypatch):
        """Поиск принёс процесс, названный в файле иначе, чем сценарий, — но
        блок практик дословно воспроизводит половину маршрута эталона.

        Проверка по имени файла (`names_the_scenario`) тут молчит: корпус
        рукописный, и один и тот же процесс в него попадает под разными
        заголовками. Без содержательной сверки тот же процесс под чужим именем
        прошёл бы аудит незамеченным, а `improve/*` мерил бы воспроизведение
        подобранного ответа.
        """
        text = "ЛУЧШИЕ ПРАКТИКИ: [logistics] " + "; ".join(self.ETALON[:3])
        hits = self._by_content(monkeypatch, text, self.ETALON)
        assert len(hits) == 1, hits
        assert hits[0]["measure"] >= provenance.ETALON_NAME_LEAK

    def test_rag_block_about_another_process_is_not_flagged(self, monkeypatch):
        """Чужой процесс с той же доменной лексикой («заявка», «проверка») — не
        утечка: считаются целые названия шагов, а не совпавшие слова."""
        text = ("ЛУЧШИЕ ПРАКТИКИ: [logistics] Проверить комплектность и цены; "
                "Согласовать оплату с бухгалтерией; Закрыть инцидент")
        assert self._by_content(monkeypatch, text, self.ETALON) == []

    def test_short_etalon_stays_out_of_the_content_check(self, monkeypatch):
        """Короткий эталон в сверке не участвует: два совпавших имени из трёх
        это словарь процессов, а не воспроизведение маршрута."""
        text = "ЛУЧШИЕ ПРАКТИКИ: " + "; ".join(self.ETALON[:3])
        assert self._by_content(monkeypatch, text, self.ETALON[:3]) == []

    # Девять дуг: ниже `MIN_ROUTE_EDGES` форма ничего не доказывает, и тесты
    # должны стоять на маршруте, который в сверку действительно попадает.
    PLAN = [
        {"id": "S", "kind": "startEvent", "name": "Начало"},
        {"id": "A1", "kind": "userTask", "name": "Проверить документы"},
        {"id": "G", "kind": "exclusiveGateway", "name": "Всё в порядке?"},
        {"id": "A2", "kind": "userTask", "name": "Согласовать"},
        {"id": "A3", "kind": "serviceTask", "name": "Отправить в систему"},
        {"id": "G2", "kind": "exclusiveGateway", "name": "Система приняла?"},
        {"id": "A4", "kind": "userTask", "name": "Довести вручную"},
        {"id": "E1", "kind": "endEvent", "name": "Готово"},
        {"id": "E2", "kind": "endEvent", "name": "Отказ"},
    ]
    FLOWS = [{"id": "f1", "source": "S", "target": "A1", "kind": "sequence"},
             {"id": "f2", "source": "A1", "target": "G", "kind": "sequence"},
             {"id": "f3", "source": "G", "target": "A2", "kind": "sequence"},
             {"id": "f4", "source": "G", "target": "E2", "kind": "sequence"},
             {"id": "f5", "source": "A2", "target": "A3", "kind": "sequence"},
             {"id": "f6", "source": "A3", "target": "G2", "kind": "sequence"},
             {"id": "f7", "source": "G2", "target": "A4", "kind": "sequence"},
             {"id": "f8", "source": "A4", "target": "E1", "kind": "sequence"},
             {"id": "f9", "source": "G2", "target": "E2", "kind": "sequence"}]
    # Пара (индекс узла → индекс узла) для клона с другими id: форма та же.
    CLONED_EDGES = [(0, 1), (1, 2), (2, 3), (2, 8), (3, 4), (4, 5), (5, 6),
                    (6, 7), (5, 8)]

    def test_route_similarity_survives_renaming_everything(self):
        """Дубликат процесса с другими именами узлов, id и подписей — тот же
        процесс для сверки формы.

        Проверка по `element_names` такие случаи пропускает: переименовать
        можно и файлы, и шаги, а род узла и то, чем он соединён, остаются.
        """
        own = provenance.route_signature(self.PLAN, self.FLOWS)
        renamed = [{"id": f"n{i}", "kind": e["kind"], "name": f"Дело {i}"}
                   for i, e in enumerate(self.PLAN)]
        flows = [{"source": f"n{i}", "target": f"n{j}", "kind": "sequence"}
                 for i, j in self.CLONED_EDGES]
        clone = provenance.route_signature(renamed, flows)
        assert provenance.route_similarity(own, clone) == 1.0

    def test_route_similarity_ignores_schemes_too_small_to_prove_anything(self):
        """Маленький маршрут в сверке не участвует: шесть дуг совпадают у
        половины корпуса, и это форма exercises, а не утечка."""
        small = provenance.route_signature(self.PLAN[:4], self.FLOWS[:3])
        assert provenance.route_edges_count(small) < provenance.MIN_ROUTE_EDGES
        assert provenance.route_similarity(small, small) == 0.0
        assert small  # сам снимок при этом строится нормально

    def test_route_similarity_is_one_on_the_same_plan(self):
        """Самоподобие = 1.0: иначе ноль в отчёте означает сломанный снимок, а
        не чистый набор."""
        own = provenance.route_signature(self.PLAN, self.FLOWS)
        assert provenance.route_similarity(own, own) == 1.0

    def test_route_finding_is_raised_when_the_hit_is_the_same_shape(self, monkeypatch):
        """Находка заводится на уровне аудита, а не только в калькуляторе."""
        fixture = {"id": "x.good.plan", "kind": "plan", "quality": "good",
                   "scenario": "loan_application",
                   "plan": {"participants": ["Банк"], "lanes": [],
                            "elements": [{"id": e["id"], "kind": e["kind"],
                                          "name": e["name"],
                                          "participant": "Банк"}
                                         for e in self.PLAN],
                            "flows": [{"id": f["id"], "source": f["source"],
                                       "target": f["target"], "kind": "sequence",
                                       "condition": ""}
                                      for f in self.FLOWS]}}
        xml = ('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">'
               + "".join(f'<{e["kind"]} id="{e["id"]}"/>' for e in self.PLAN)
               + "".join(f'<sequenceFlow id="{f["id"]}" sourceRef="{f["source"]}" '
                         f'targetRef="{f["target"]}"/>' for f in self.FLOWS)
               + "</definitions>")
        monkeypatch.setattr(provenance, "corpus_xml", lambda name: xml)
        monkeypatch.setattr(provenance, "rag_surfaces", lambda scenario: [
            {"query": "q", "block": "", "hits": [{"name": "clone", "similarity": 1.0,
                                                  "element_names": "",
                                                  "xml_features": ""}]}])
        report = provenance.audit([get("loan_application")], [fixture])
        kinds = [f["kind"] for f in report["findings"]]
        assert "подобранная схема повторяет форму маршрута кейса" in kinds, report["findings"]
        # Находка обязана называть, куда смотреть: доля совпавших дуг — число по
        # всему корпусу, а проверять глазами придётся одну схему.
        hit = [f for f in report["findings"]
               if f["kind"] == "подобранная схема повторяет форму маршрута кейса"][0]
        assert hit["value"] == "x.good.plan ← clone.bpmn", hit
        assert report["closest_route"]["file"] == "clone.bpmn", report["closest_route"]
        assert report["route_files"] == {"loan_application": "clone.bpmn"}

    def test_route_margin_without_a_route_names_nothing(self):
        """Нет с чем сверять форму — нет и имени схемы.

        Иначе отчёт выдавал бы «ближайшая схема корпуса — …» там, где сравнение
        не проводилось: адрес без числа вводит в заблуждение так же, как число
        без адреса.
        """
        fixture = {"id": "x.good.plan", "kind": "plan", "quality": "good",
                   "scenario": "loan_application",
                   "plan": {"participants": ["Банк"], "lanes": [],
                            "elements": [{"id": e["id"], "kind": e["kind"],
                                          "name": e["name"], "participant": "Банк"}
                                         for e in self.PLAN[:4]],
                            "flows": [{"id": f["id"], "source": f["source"],
                                       "target": f["target"], "kind": "sequence",
                                       "condition": ""}
                                      for f in self.FLOWS[:3]]}}
        margin, name = provenance.route_margin(get("loan_application"), fixture)
        assert margin == 0.0 and name == ""

    def test_every_retrieval_hit_resolves_to_a_corpus_scheme(self):
        """Данные у сверки формы есть: имя хита поисковика переводится в файл
        корпуса для каждой подборки.

        Иначе нули в `route_margins` означали бы не «утечки нет», а «сравнивать
        нечем» — тот же класс лжи, что пустой `element_names`.
        """
        total = resolved = 0
        for scenario in provenance.all_scenarios():
            for surface in provenance.rag_surfaces(scenario):
                for hit in surface["hits"]:
                    total += 1
                    resolved += 1 if provenance.corpus_xml(hit["name"]) else 0
        assert total >= 8, "подборок нет — проверке не на чем быть"
        assert resolved == total, f"разрешается не всё: {resolved}/{total}"

    def test_audit_reports_the_distance_of_the_nearest_retrieval(self):
        """«Находок нет» обязано звучать с числом: насколько близко подобранное
        прошело мимо порога.

        Без расстояния нуль совпадений неотличим от мёртвого детектора — тот же
        ноль даёт пустое `element_names`. Тест держит и поле отчёта, и строку:
        находки по нему не считаются, но читатель отчёта должен видеть запас.
        """
        report = provenance.audit([get("warehouse_delivery")])
        margins = report["overlap_margins"]
        assert list(margins) == ["warehouse_delivery"], margins
        assert 0.0 <= margins["warehouse_delivery"] < provenance.ETALON_NAME_LEAK
        assert report["closest"]["scenario"] == "warehouse_delivery"
        # форма маршрута — второе, независимое от имён расстояние того же риска
        assert set(report["route_margins"]) == {"warehouse_delivery"}
        assert report["closest_route"]["measure"] < provenance.ROUTE_SIMILARITY
        lines = provenance.format_findings(report)
        assert any("ближайший подбор" in line and "%" in line for line in lines), lines

    def test_etalon_names_survive_a_scenario_without_a_good_plan(self):
        """Содержательная проверка обязана видеть все сценарии набора.

        Первая редакция брала имена только из «good»-плана и ослепла на
        `employee_onboarding` и `vehicle_reservation`: у них есть только
        `bad`-фикстура, и аудит по ним молчал бы не потому, что утечки нет, а
        потому что сверять было нечего.
        """
        fixture = {"id": "x.bad.plan", "kind": "plan", "quality": "bad",
                   "scenario": "employee_onboarding",
                   "plan": {"elements": [{"name": n} for n in self.ETALON]}}
        names = provenance.etalon_step_names(fixture)
        assert names == self.ETALON
        report = provenance.audit([get("employee_onboarding")], [fixture])
        assert report["etalon_scenarios"] == ["employee_onboarding"], report

    def test_rag_hit_carrying_the_etalon_steps_is_flagged(self, monkeypatch):
        """Подобранная схема несёт те же шаги, что эталон кейса, — даже если её
        файл назван совсем иначе.

        `element_names` хита — это имена узлов корпусной схемы; `_format_practices`
        печатает из подборки только имя файла и фразы практик, поэтому сверка по
        блоку такой дубликат не видит. Без этой проверки `improve/*` мерил бы
        воспроизведение процесса, подобранного поиском.
        """
        blob = "; ".join(self.ETALON[:3])

        def fake(scenario):
            return [{"query": scenario.improve_prompt, "block": "практики",
                     "hits": [{"name": "Warenversand_bc722883a18e42f1",
                               "similarity": 0.9, "element_names": blob,
                               "xml_features": "many_tasks several_gateways"}]}]

        monkeypatch.setattr(provenance, "rag_surfaces", fake)
        hits = [f for f in provenance.scenario_findings(
            get("warehouse_delivery"), provenance.examples(), self.ETALON)
            if f["example"] == "rag_practices"
            and "несёт шаги процесса" in f["kind"]]
        assert len(hits) == 1, hits
        assert hits[0]["measure"] >= provenance.ETALON_NAME_LEAK
        assert "Warenversand" in hits[0]["value"]

    def test_rag_hit_of_another_process_is_not_flagged(self, monkeypatch):
        blob = "Determine Size Normal; Submit Offer; Create Package Label"

        def fake(scenario):
            return [{"query": scenario.improve_prompt, "block": "практики",
                     "hits": [{"name": "Dispatch_of_goods", "similarity": 0.9,
                               "element_names": blob, "xml_features": ""}]}]

        monkeypatch.setattr(provenance, "rag_surfaces", fake)
        findings = provenance.scenario_findings(
            get("warehouse_delivery"), provenance.examples(), self.ETALON)
        assert [f for f in findings if "несёт шаги процесса" in f["kind"]] == []

    def test_rag_surfaces_actually_carry_element_names(self):
        """Данные для сверки доходят: хит несёт `element_names`, и хотя бы один
        блок непустой.

        Проверка утечки по подборке молчала бы «правильно» и при пустом поле:
        ноль совпадений неотличим от мёртвого детектора, поэтому отдельный
        тест на доставку данных, а не только на порог.
        """
        surfaces = provenance.rag_surfaces(get("warehouse_delivery"))
        assert surfaces, "поверхности RAG не собраны"
        hits = [h for surface in surfaces for h in surface["hits"]]
        assert hits, "поиск не принёс ни одного эталона"
        assert all("element_names" in h and "xml_features" in h for h in hits)
        assert any(h["element_names"].strip() for h in hits),             "у подборок нет имён шагов — содержательная сверка нечем не питается"

    def test_scoring_rule_naming_an_expected_pool_is_caught(self, monkeypatch):
        """Блок «УЗКИЕ МЕСТА ПО СКОРИНГУ» модель читает дословно: имя
        участника, вписанное в формулировку правила, работает подсказкой
        ответа так же, как в промпте состава."""
        class _Scorer:
            def __init__(self):
                self.rules = {
                    "role_pools": {"weight": 8,
                                   "message": "пример: «перевозчик»",
                                   "action": ["merge_participants"]},
                }

        monkeypatch.setattr(bpmn_scoring, "BPMNScorer", _Scorer)
        report = provenance.audit([get("warehouse_delivery")], [])
        hits = [f for f in report["findings"] if f["example"] == "scoring_rules"]
        assert [f["value"] for f in hits] == ["Перевозчик"], hits

    def test_op_spec_naming_an_expected_pool_is_caught(self, monkeypatch):
        """Формы операций `_operations_block` печатает в системный промпт:
        имя, вписанное в шаблон, — то же самое, что имя из примера ответа."""
        monkeypatch.setattr(bpmn_edits, "OP_SPEC",
                            dict(bpmn_edits.OP_SPEC,
                                 **{"add_task": '{"op":"add_task",'
                                                 '"name":"перевозчик"}'}))
        report = provenance.audit([get("warehouse_delivery")], [])
        hits = [f for f in report["findings"] if f["example"] == "op_spec"]
        assert [f["value"] for f in hits] == ["Перевозчик"], hits

    def test_ethalon_name_is_matched_as_a_whole_name(self):
        warehouse = get("warehouse_delivery")
        assert provenance.names_the_scenario("warehouse_delivery.bpmn", warehouse)
        assert provenance.names_the_scenario("Доставка груза.bpmn", warehouse)
        # Одно общее слово — ещё не тот же процесс: иначе `Dispatch_of_goods`
        # ловился бы на каждом упоминании доставки, а не на утечке.
        assert not provenance.names_the_scenario(
            "Dispatch_of_goods_bc722883a18e42f1bf5e53626e77a811", warehouse)
        assert not provenance.names_the_scenario("New_Process.bpmn", warehouse)

    def test_containment_is_asymmetric_by_design(self):
        """Короткий запрос целиком живёт в длинном образце — это и есть утечка."""
        short = provenance.words("заведи отсчёт длительности замены")
        long = provenance.words("заведи отсчёт длительности замены, вилку "
                                "по ремфонду закрой парой шлюзов")
        assert provenance.containment(short, long) == 1.0
        assert provenance.containment(long, short) < 1.0


class TestPromptSampleRegistry:
    """Новый промпт с образцом обязан попасть в `examples()`.

    Модуль уже падает громко, если маркер образца исчез (проверять нечего).
    Слепое пятно — обратное: новый промпт со своим примером, который в реестр
    не занесли. Тогда аудит молча сверял бы набор со старыми образцами и
    печатал «пересечений нет» ровно там, где пример появился.

    Падение этой проверки — не «допишите имя в список», а новый вход в
    `examples()`: копией уже проверяемого промпта она быть не может, потому что
    дополняющий текст модель читает целиком и имя участника может нести в себе
    (так зарегистрирован `_FLOW_SYSTEM_PROMPT`, скопированный с генерационного).
    """

    def test_enumeration_is_not_vacuous(self):
        """Правило перечисления не должно выродиться в пустой список — иначе
        следующая проверка всегда зелёная."""
        assert sample_bearing_prompts(bpmn_generator)
        assert sample_bearing_prompts(llm_improve)

    def test_every_sample_bearing_prompt_is_registered(self):
        registered = {s["where"] for s in provenance.examples()}
        assert unregistered_samples(registered) == set()

    def test_guard_notices_an_unregistered_prompt(self, monkeypatch):
        """Проверка не декоративная: промпт с примером, появившийся в модуле,
        обязана выдать список непрозарегистрированных."""
        monkeypatch.setattr(
            llm_improve, "_NEW_PLANNING_PROMPT",
            'ПРИМЕР ОТВЕТА: {"operations": ['
            '{"op": "add_task", "name": "Погрузить паллеты"}]}',
            raising=False)
        registered = {s["where"] for s in provenance.examples()}
        assert unregistered_samples(registered) == {
            "core.llm_improve._NEW_PLANNING_PROMPT"}

    def test_prose_about_json_fields_is_not_an_example(self, monkeypatch):
        """Упоминание полей в прозе образцом не считается: иначе правило
        перечисления встало бы на каждом напоминании «ответ — только JSON с
        полями "analysis" и "operations"»."""
        monkeypatch.setattr(llm_improve, "_NEW_NOTE_PROMPT",
                            'ответ — только JSON с полями "analysis" и '
                            '"operations" и ничего больше', raising=False)
        assert "_NEW_NOTE_PROMPT" not in sample_bearing_prompts(llm_improve)


class TestEvalSetIsClean:
    def test_examples_are_still_in_the_prompts(self):
        """Образцы — часть промптов, а не мёртвые константы: без них харнесс
        проверял бы пустоту."""
        assert "Пример. " in bpmn_generator._SYSTEM_PROMPT
        assert "ПРИМЕР ОТВЕТА" in llm_improve._SYSTEM_PROMPT
        # Две поверхности, которые промпт улучшения вклеивает в себя из других
        # модулей: если их перестанут отдавать модели, проверка над ними
        # повиснет на пустоте.
        assert "merge_participants" in bpmn_edits.OP_SPEC
        assert "role_pools" in bpmn_scoring.BPMNScorer().rules

    def test_scenario_intersections_are_only_the_tracked_ones(self):
        """Набор кейсов чист: пересечений с few-shot образцами промптов нет.

        Планка заперта на пустом списке не от бедности, а как замок: любое
        новое пересечение — провал теста, и уже человек решает, остаётся ли
        кейс в наборе (докстринг модуля).

        Здесь жила одна настоящая находка: блок ЛУЧШИЕ ПРАКТИКИ для
        `employee_onboarding` печатал домен эталона `[hr]`, а оракул требует пул
        «HR» — слово из промпта модель способна вернуть именем участника.
        Убрана она не исключением в `scenario_findings` (это означало бы «не
        видим — значит чисто»), а правкой промпта: `_format_practices` больше не
        печатает тег домена. Именно пустой список ниже и уронит харнесс, если
        тег вернётся.
        """
        report = provenance.audit(all_scenarios(), [])
        assert [
            (f["scenario"], f["kind"], f["example"], f["value"])
            for f in report["findings"]
        ] == [], provenance.format_findings(report)

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

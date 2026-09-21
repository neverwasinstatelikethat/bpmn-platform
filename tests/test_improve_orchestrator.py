"""Оркестратор улучшения: от «непослушного» ответа модели до готового XML.

Это главный сценарий «найти узкие места и поправить схему». LLM здесь
подставлен на уровне транспорта (_complete), поэтому реальный разбор ответа,
повторы и применение операций проверяются так же, как в проде — без сети и
без ключа.
"""
import asyncio
import json

import pytest

from core import llm_client
from core import llm_improve
from core.llm_improve import (
    BPMNImprovementOrchestrator,
    ImprovementError,
)


class FakeKnowledgeBase:
    """Эталонный корпус не подключаем: он нужен только для текста промпта.

    Формат ответов повторяет контракт BPMNKnowledgeBase: `practices` — пары
    «признак, формулировка» эталона, `missing_practices` — то, чего нет в
    схеме пользователя.
    """

    def __init__(self):
        self.queries = []
        self.ready_calls = 0

    def ensure_ready(self):
        self.ready_calls += 1

    def find_best_practices(self, query, xml_content=None):
        self.queries.append((query, xml_content))
        return [{
            "domain": "Заказы",
            "name": "Эталон оплаты",
            "practices": [["check", "проверять оплату до отгрузки"]],
            "missing_practices": ["проверять оплату до отгрузки"],
        }]


class FakeLLM:
    """Отдаёт заранее заготовленные сырые ответы по одному на вызов."""

    def __init__(self, monkeypatch, *responses):
        self.responses = list(responses)
        self.calls = []
        self.params = []
        monkeypatch.setattr(llm_client, "_complete", self._complete)

    def _complete(self, messages, temperature, max_tokens):
        self.calls.append(messages)
        self.params.append({"temperature": temperature, "max_tokens": max_tokens})
        if not self.responses:
            raise llm_client.LLMError("ответов не осталось")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def prompts(self):
        return [call[1]["content"] for call in self.calls]


def _improve(orchestrator, xml, prompt="Добавь проверку оплаты"):
    return asyncio.run(orchestrator.improve_diagram(xml_content=xml,
                                                    user_prompt=prompt))


@pytest.fixture
def kb():
    return FakeKnowledgeBase()


@pytest.fixture
def orchestrator(kb):
    return BPMNImprovementOrchestrator(knowledge_base=kb)


def _wrap(payload: str) -> str:
    """Ответ в духе reasoning-модели: блок рассуждений + код-блок + пояснение."""
    return "<think>Размышляю о схеме.</think>\nВот план:\n```json\n" + payload + "\n```\nГотово."


class TestPlanIsSentCorrectly:
    def test_prompt_carries_inventory_not_raw_xml(
        self, orchestrator, kb, monkeypatch, single_pool_xml
    ):
        fake = FakeLLM(monkeypatch, _wrap('{"analysis": "ок", "operations": []}'))
        _improve(orchestrator, single_pool_xml)
        prompt = fake.prompts[0]
        assert '"T_collect"' in prompt
        assert "Собрать заказ" in prompt
        assert "проверять оплату до отгрузки" in prompt
        assert "<definitions" not in prompt
        # В поиск уходит и запрос, и схема: по ней считается разность практик.
        assert kb.queries == [("Добавь проверку оплаты", single_pool_xml)]

    def test_user_prompt_reaches_the_model(self, orchestrator, monkeypatch,
                                           single_pool_xml):
        fake = FakeLLM(monkeypatch, '{"analysis": "ок", "operations": []}')
        _improve(orchestrator, single_pool_xml, prompt="убери дублирование")
        assert "убери дублирование" in fake.prompts[0]


class TestChangesApplied:
    def test_operations_produce_importable_xml(self, orchestrator, monkeypatch,
                                               single_pool_xml):
        plan = _wrap(json.dumps({
            "analysis": "нет проверки оплаты",
            "operations": [
                {"op": "add_task", "id": "new_T_pay", "name": "Проверить оплату",
                 "task_type": "serviceTask", "after": "T_collect"},
                {"op": "rename", "id": "T_ship", "name": "Отгрузить после оплаты"},
            ],
        }, ensure_ascii=False))
        fake = FakeLLM(monkeypatch, plan)
        recommendations, improved_xml, report = _improve(orchestrator, single_pool_xml)

        assert "нет проверки оплаты" in recommendations
        assert improved_xml.startswith('<?xml version="1.0"')
        assert "<bpmn:serviceTask" in improved_xml
        assert report["status"] == "success"
        assert [a["op"] for a in report["applied"]] == ["add_task", "rename"]
        assert fake.responses == []
        # Один вызов: повтор нужен только когда есть пропущенные операции.
        assert len(fake.calls) == 1

    def test_condition_added_on_new_branch(self, orchestrator, monkeypatch,
                                           single_pool_xml):
        plan = ('{"analysis": "нет ветки возврата", "operations": ['
                '{"op": "add_event", "id": "new_E_refund", "name": "Возврат", "event_type": "end"},'
                '{"op": "connect", "source": "G_paid", "target": "new_E_refund",'
                ' "condition": "refund == true"}]}')
        FakeLLM(monkeypatch, _wrap(plan))
        _, improved_xml, report = _improve(orchestrator, single_pool_xml,
                                           prompt="добавь возврат")
        assert report["status"] == "success"
        assert "new_E_refund" in improved_xml
        assert "refund == true" in improved_xml

    def test_analysis_only_answer_yields_no_xml(self, orchestrator, monkeypatch,
                                                single_pool_xml):
        FakeLLM(monkeypatch, _wrap('{"analysis": "схема сбалансирована", "operations": []}'))
        recommendations, improved_xml, report = _improve(orchestrator, single_pool_xml)
        assert recommendations == "схема сбалансирована"
        assert improved_xml is None
        assert report["status"] == "analysis_only"


class TestPartialAndFailures:
    def test_skipped_operations_are_reported_to_the_user(
        self, orchestrator, monkeypatch, single_pool_xml
    ):
        first = '{"analysis": "план", "operations": [' \
                '{"op": "rename", "id": "T_collect", "name": "Собрать"}, ' \
                '{"op": "rename", "id": "nope", "name": "Призрак"}]}'
        second = '{"analysis": "повтор", "operations": [{' \
                 '"op": "add_gateway", "id": "new_G2", "name": "Проверка остатков",' \
                 '"gateway_type": "inclusive", "after": "T_ship"}]}'
        fake = FakeLLM(monkeypatch, first, second)
        recommendations, improved_xml, report = _improve(orchestrator, single_pool_xml)

        assert len(fake.calls) == 2
        assert "nope" not in json.dumps(report["applied"])
        assert improved_xml is not None
        assert "new_G2" in improved_xml
        assert "Не применено" in recommendations or report["skipped"] == []

    def test_everything_rejected_becomes_actionable_error(
        self, orchestrator, monkeypatch, single_pool_xml
    ):
        FakeLLM(monkeypatch, '{"analysis": "план", "operations": ['
                             '{"op": "delete", "id": "Start_1"}, '
                             '{"op": "wat", "id": "T_collect"}]}')
        with pytest.raises(ImprovementError) as exc:
            _improve(orchestrator, single_pool_xml)
        message = str(exc.value)
        assert "не удалось применить" in message
        assert "стартовые и конечные события" in message

    def test_llm_outage_is_not_a_500(self, orchestrator, monkeypatch,
                                     single_pool_xml):
        FakeLLM(monkeypatch, llm_client.LLMError("транспорт лёг"))
        with pytest.raises(ImprovementError, match="временно недоступен"):
            _improve(orchestrator, single_pool_xml)

    def test_unparsable_answer_becomes_rephrase_hint(self, orchestrator,
                                                     monkeypatch, single_pool_xml):
        FakeLLM(monkeypatch, "я сегодня не в духе, JSON не будет",
                          "и сейчас тоже")
        with pytest.raises(ImprovementError, match="переформулировать"):
            _improve(orchestrator, single_pool_xml)

    def test_broken_input_fails_before_calling_llm(self, orchestrator, monkeypatch):
        fake = FakeLLM(monkeypatch)
        with pytest.raises(ImprovementError, match="разобрать XML"):
            _improve(orchestrator, "<definitions><<<")
        assert fake.calls == []

    def test_truncated_answer_is_not_applied_silently(self, orchestrator,
                                                     monkeypatch, single_pool_xml):
        FakeLLM(monkeypatch,
                llm_client.LLMTruncatedError("ответ обрезан по лимиту токенов"))
        with pytest.raises(ImprovementError) as exc:
            _improve(orchestrator, single_pool_xml)
        assert "обрезан" in str(exc.value)
        assert "не применён" in str(exc.value)


class TestPlanReporting:
    def test_cut_operations_are_visible_in_the_report(self, orchestrator,
                                                     monkeypatch, single_pool_xml):
        ops = [{"op": "rename", "id": "T_collect", "name": f"Шаг {i}"}
               for i in range(llm_improve.MAX_OPERATIONS + 7)]
        FakeLLM(monkeypatch, json.dumps({"analysis": "план", "operations": ops},
                                        ensure_ascii=False))
        recommendations, _, report = _improve(orchestrator, single_pool_xml)
        assert report["truncated_operations"] == 7
        assert "отброшено 7" in recommendations

    def test_skipped_survives_the_second_round(self, orchestrator, monkeypatch,
                                              single_pool_xml):
        first = '{"analysis": "план", "operations": [' \
                '{"op": "rename", "id": "T_collect", "name": "Собрать"}, ' \
                '{"op": "rename", "id": "nope", "name": "Призрак"}]}'
        second = '{"analysis": "повтор", "operations": [' \
                 '{"op": "rename", "id": "ghost", "name": "Дух"}]}'
        FakeLLM(monkeypatch, first, second)
        recommendations, _, report = _improve(orchestrator, single_pool_xml)

        # Исходная пропущенная операция не исчезает: её причина относится
        # именно к ней, а не к тому, что придумал повтор.
        planned_skips = [s for s in report["skipped"] if s["stage"] == "plan"]
        assert [s["op"] for s in planned_skips] == ["rename"]
        assert planned_skips[0]["reapplied"] is False
        assert [s["stage"] for s in report["skipped"] if s["op"] == "rename"] == ["plan", "retry"]
        assert report["status"] == "partial"
        assert "Не применено" in recommendations
        assert "план: rename" in recommendations

    def test_retry_sees_the_same_planning_context(self, orchestrator, kb,
                                                 monkeypatch, single_pool_xml):
        first = '{"analysis": "план", "operations": [' \
                '{"op": "rename", "id": "nope", "name": "Призрак"}]}'
        second = '{"analysis": "повтор", "operations": [' \
                 '{"op": "rename", "id": "T_ship", "name": "Отгрузить"}]}'
        fake = FakeLLM(monkeypatch, first, second)
        _improve(orchestrator, single_pool_xml)

        assert len(fake.prompts) == 2
        assert "проверять оплату до отгрузки" in fake.prompts[1]
        temps = [call["temperature"] for call in fake.params]
        assert temps[0] == temps[1] == llm_improve.PLANNING_TEMPERATURE
        # Практики ищутся один раз на сценарий, повтор переиспользует блок.
        assert len(kb.queries) == 1


class TestWarmup:
    def test_warmup_builds_the_index_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm_improve, "_cache_paths",
                            lambda: (tmp_path / "rag.npz", tmp_path / "rag.json"))
        kb = llm_improve.BPMNKnowledgeBase(dataset_path=str(tmp_path))
        builds = []

        def fake_build():
            builds.append(1)
            kb._metadata = [{"name": "Эталон"}]

        monkeypatch.setattr(kb, "_build", fake_build)
        orchestrator = BPMNImprovementOrchestrator(knowledge_base=kb)
        orchestrator.warmup()
        orchestrator.warmup()
        assert len(builds) == 1


def test_planner_prompt_lists_every_operation_the_applier_supports():
    """Промпт и аплайер не должны расходиться: модель не может предложить
    операцию, которой нет в словаре, и не должна молчать про те, что есть."""
    from core import bpmn_edits

    for op in bpmn_edits.OP_SPEC:
        assert f'"op":"{op}"' in llm_improve._SYSTEM_PROMPT, op
    assert set(bpmn_edits.OP_SPEC) == set(bpmn_edits._HANDLERS)


def test_dangling_added_step_is_rolled_back_and_explained(orchestrator, monkeypatch,
                                                          single_pool_xml):
    """add_task без after и без connect не применим: аплайер откатывает такой
    шаг, а оркестратор обязан объяснить причину, иначе пользователь увидит
    «улучшение», которого нет."""
    plan = '{"analysis": "добавил шаг", "operations": [' \
           '{"op":"add_task","id":"new_T","name":"Проверить склад",' \
           '"task_type":"serviceTask"}]}'
    FakeLLM(monkeypatch, _wrap(plan))
    with pytest.raises(ImprovementError) as exc:
        _improve(orchestrator, single_pool_xml)
    assert "не удалось применить" in str(exc.value)
    # Причина — из откатного отчёта аплайера, а не общая формулировка.
    assert "не связан ни с одним элементом" in str(exc.value)


def test_boundary_event_without_handler_is_surfaced_to_user(orchestrator,
                                                           monkeypatch,
                                                           single_pool_xml):
    """Таймер без ветки обработки — применённая операция, которая ухудшает
    схему. Пользователь узнаёт об этом из анализа, а не только из отчёта."""
    plan = '{"analysis": "добавил таймер", "operations": [' \
           '{"op":"add_boundary_event","id":"new_BE","attached_to":"T_collect",' \
           '"event_type":"timer","name":"Просрочка"}]}'
    FakeLLM(monkeypatch, _wrap(plan))
    analysis, xml_after, report = _improve(orchestrator, single_pool_xml)
    assert xml_after is not None
    assert "остались вне маршрута" in analysis
    assert any("new_BE" in n for n in report["repair_notes"])
    # Пометка аплайера доходит до пользователя дословно — с указанием, какой
    # операции не хватает.
    assert any("не ведёт ни к одному шагу" in n for n in report["repair_notes"])


def test_rolled_back_step_triggers_the_corrective_round(orchestrator, monkeypatch,
                                                        single_pool_xml):
    """Откат незапланированного шага — пропуск, и повтор обязан вызваться:
    модель довязывает маршрут сама, аплайер не додумывает связи за неё. В
    подсказке сказано перевставлять шагом с after — connect на удалённый id уже
    не работает."""
    first = '{"analysis": "план", "operations": [' \
            '{"op":"add_task","id":"new_T","name":"Проверить склад",' \
            '"task_type":"serviceTask"}]}'
    second = '{"analysis": "встроил", "operations": [' \
             '{"op":"add_task","id":"new_T2","name":"Проверить склад",' \
             '"task_type":"serviceTask","after":"T_ship"}]}'
    fake = FakeLLM(monkeypatch, _wrap(first), _wrap(second))
    analysis, xml_after, report = _improve(orchestrator, single_pool_xml)

    assert len(fake.calls) == 2
    # Повтор видит причину отката и подсказку перевставлять шаг с after:
    # connect на удалённый id уже не работает.
    assert "изменение откачено" in fake.prompts[1]
    assert "new_T" in fake.prompts[1]
    assert "перевставьте шаг операцией с after" in fake.prompts[1]
    assert [a["op"] for a in report["applied"]] == ["add_task"]
    assert report["applied"][0]["id"] == "new_T2"
    assert [s["stage"] for s in report["skipped"]] == ["plan"]
    assert [s["id"] for s in report["skipped"]] == ["new_T"]
    assert report["repair_notes"] == []
    assert "остались вне маршрута" not in analysis
    assert "Проверить склад" in xml_after


def test_reapplied_in_the_retry_is_not_reported_as_missed(orchestrator, monkeypatch,
                                                          single_pool_xml):
    """Если повтор провёл ту же правку тем же элементом, пользователю нельзя
    писать «не применено»: изменение в схеме есть."""
    first = '{"analysis": "план", "operations": [' \
            '{"op":"add_task","id":"new_T","name":"Проверить склад",' \
            '"task_type":"serviceTask"}]}'
    second = '{"analysis": "встроил", "operations": [' \
             '{"op":"add_task","id":"new_T","name":"Проверить склад",' \
             '"task_type":"serviceTask","after":"T_ship"}]}'
    FakeLLM(monkeypatch, _wrap(first), _wrap(second))
    analysis, xml_after, report = _improve(orchestrator, single_pool_xml)

    assert [s["reapplied"] for s in report["skipped"]] == [True]
    assert report["status"] == "success", report["skipped"]
    assert "Не применено" not in analysis
    assert "Повтор добил 1 правку" in analysis
    assert "Проверить склад" in xml_after

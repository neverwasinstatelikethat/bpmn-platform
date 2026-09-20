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
from core.llm_improve import (
    BPMNImprovementOrchestrator,
    ImprovementError,
)


class FakeKnowledgeBase:
    """Эталонный корпус не подключаем: он нужен только для текста промпта."""

    def __init__(self):
        self.queries = []

    def find_best_practices(self, query, xml_content=None):
        self.queries.append(query)
        return [{
            "domain": "Заказы",
            "name": "Эталон оплаты",
            "best_practices": ["проверять оплату до отгрузки"],
        }]


class FakeLLM:
    """Отдаёт заранее заготовленные сырые ответы по одному на вызов."""

    def __init__(self, monkeypatch, *responses):
        self.responses = list(responses)
        self.calls = []
        monkeypatch.setattr(llm_client, "_complete", self._complete)

    def _complete(self, messages, temperature, max_tokens):
        self.calls.append(messages)
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
        assert kb.queries == ["Добавь проверку оплаты"]

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

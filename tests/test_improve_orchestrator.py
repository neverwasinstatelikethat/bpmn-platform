"""Оркестратор улучшения: от «непослушного» ответа модели до готового XML.

Это главный сценарий «найти узкие места и поправить схему». LLM здесь
подставлен на уровне транспорта (_complete), поэтому реальный разбор ответа,
повторы и применение операций проверяются так же, как в проде — без сети и
без ключа.
"""
import asyncio
import json
import re

import pytest

from core import bpmn_edits, llm_client, llm_improve
from core.llm_improve import (
    BPMNImprovementOrchestrator,
    ImprovementError,
    ImprovementUnavailable,
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
        # Другой элемент с той же формулировкой отказа — второй дефект, он
        # не схлопывается с первым.
        assert report["repeat_rejections"] == 0
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
        temps = [call["temperature"] for call in fake.params]
        assert temps[0] == temps[1] == llm_improve.PLANNING_TEMPERATURE
        # Практики ищутся один раз на сценарий — повтор не имеет права дёргать
        # корпус эмбеддингов ради того же текста.
        assert len(kb.queries) == 1
        # Но в сам повтор блок практик не уходит: задача повтора — добить
        # пропуски и узлы вне маршрута, а практики подталкивали модель
        # предлагать новые улучшения вместо починки.
        assert "проверять оплату до отгрузки" in fake.prompts[0]
        assert "ЛУЧШИЕ ПРАКТИКИ" not in fake.prompts[1]
        assert "НЕПРИМЕНЁННЫЕ ОПЕРАЦИИ" in fake.prompts[1]
        # В повтор уходит не только причина, но и подсказка аплайера: без неё
        # модель переводит пакет дословно (так и было в живом прогоне — те же
        # id, те же тупики, второй вызов впустую).
        assert '"hint"' in fake.prompts[1]

    def test_retry_is_told_what_the_package_already_did(self, orchestrator,
                                                       monkeypatch,
                                                       single_pool_xml):
        """Живой прогон: повтор предлагал прицепить ещё один таймер к шагу,
        где таймер уже стоял, и ловил отказ «id занят» — дубль вместо пропуска."""
        first = '{"analysis": "план", "operations": [' \
                '{"op": "rename", "id": "T_ship", "name": "Отгрузить"},' \
                '{"op": "rename", "id": "nope", "name": "Призрак"}]}'
        second = '{"analysis": "повтор", "operations": []}'
        fake = FakeLLM(monkeypatch, first, second)
        _improve(orchestrator, single_pool_xml)

        assert len(fake.prompts) == 2
        assert "УЖЕ ПРИМЕНЕНО" in fake.prompts[1]
        assert "rename T_ship" in fake.prompts[1]


    def test_repeat_of_a_rejected_operation_is_shown_once(self, orchestrator,
                                                         monkeypatch,
                                                         single_pool_xml):
        """Повтор, вернувший отказ дословно, не удваивает отчёт: в живых
        прогонах второй раунд повторял первый целиком, и пользователь видел
        восемь строк вместо четырёх дефектов."""
        dangling = '{"op": "add_task", "id": "new_X", "name": "Висячий",' \
                   ' "task_type": "userTask"}'
        first = '{"analysis": "план", "operations": [' \
                '{"op": "rename", "id": "T_ship", "name": "Отгрузить паллеты"}, ' \
                + dangling + ']}'
        second = '{"analysis": "повтор", "operations": [' + dangling + ']}'
        fake = FakeLLM(monkeypatch, _wrap(first), _wrap(second))
        recommendations, _, report = _improve(orchestrator, single_pool_xml)

        assert len(fake.calls) == 2
        assert report["repeat_rejections"] == 1
        # В отчёте запись раунда остаётся: по ней харнесс видит, что
        # корректирующий вызов был и ничего не добил.
        assert [s["stage"] for s in report["skipped"]] == ["plan", "retry"]
        assert report["skipped"][1]["duplicate"] is True
        # Пользователю — один раз.
        assert "Не применено" in recommendations
        assert recommendations.count("add_task") == 1
        assert "повтор: add_task" not in recommendations
        assert "без изменений" in recommendations


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


def test_planner_prompt_teaches_the_defects_the_user_filed():
    """Правила планирования: парной шлюз схождения, default на необусловленной
    ветке, хронометраж таймера и судьба лишних/пустых пулов — иначе модель
    продолжает предлагать те же дефекты, что были в прогоне «склад».
    """
    prompt = llm_improve._SYSTEM_PROMPT
    assert "шлюз на входе, шлюз на выходе" in prompt
    assert '"default": true' in prompt
    assert "set_default" in prompt
    assert "PT15M" in prompt and "R3/PT10M" in prompt
    assert "ISO-8601" in prompt
    assert "merge_participants" in prompt and "remove_participant" in prompt
    assert "пустой" in prompt
    # новая ветка не должна оставаться одиноким шагом
    assert "откатывает" in prompt


def test_retry_prompt_explains_the_missing_default_branch(orchestrator, monkeypatch,
                                                          single_pool_xml):
    """Развилка без парного шлюза схождения: модель ведёт третью ветку без
    условия. Отказ обязан дойти до корректирующего повтора с подсказкой про
    default, иначе повтор предложит то же самое."""
    first = json.dumps({"analysis": "ветка", "operations": [
        {"op": "connect", "source": "G_paid", "target": "End_cancel",
         "default": True},
        {"op": "connect", "source": "G_paid", "target": "T_collect"},
    ]}, ensure_ascii=False)
    second = json.dumps({"analysis": "исправила", "operations": [
        {"op": "connect", "source": "G_paid", "target": "T_collect",
         "condition": "paid == false и товар не отгружен"},
    ]}, ensure_ascii=False)
    fake = FakeLLM(monkeypatch, _wrap(first), _wrap(second))
    analysis, xml_after, report = _improve(orchestrator, single_pool_xml)

    # первая ветка стала выходом по умолчанию
    assert 'default="new_Flow_1"' in xml_after
    # отказ дошёл до корректирующего повтора вместе с подсказкой
    assert "уже есть выход по умолчанию" in fake.prompts[1]
    assert "condition" in fake.prompts[1]
    assert report["skipped"][0]["op"] == "connect"
    assert report["skipped"][0]["reapplied"] is True
    assert report["status"] == "success", report["skipped"]
    assert "Повтор добил 1 правку" in analysis
    assert "paid == false и товар не отгружен" in xml_after


def test_merge_pools_reaches_the_user_as_one_lane(orchestrator, monkeypatch,
                                                  two_pool_xml):
    """Нормализация «роль → дорожка» живёт и в HTTP-контуре: план со слиянием
    обязан вернуться одним пулом с дорожкой и без messageFlow."""
    plan = json.dumps({"analysis": "роли одной организации — дорожки", "operations": [
        {"op": "merge_participants", "source": "Клиент", "target": "Магазин",
         "as_lane": True},
    ]}, ensure_ascii=False)
    fake = FakeLLM(monkeypatch, _wrap(plan))
    _, xml_after, report = _improve(orchestrator, two_pool_xml)

    assert report["status"] == "success", report["skipped"]
    assert xml_after.count("<bpmn:participant ") == 1
    assert '<bpmn:lane ' in xml_after and 'name="Клиент"' in xml_after
    assert "MF1" not in xml_after
    assert report["repair_notes"] == []
    assert "слит" in report["applied"][0]["note"]
    # слияние сошлось с первого плана: корректирующий повтор не нужен
    assert len(fake.calls) == 1


def test_invalid_merge_rolls_the_package_back_and_is_explained(orchestrator,
                                                               monkeypatch):
    """Слияние, после которого шаг источника повис вне маршрута, откатывает весь
    пакет: полуслитая схема не уходит ни пользователю, ни в БД, а причину
    пользователь читает в тексте ошибки."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D_rollback">
  <collaboration id="C_r">
    <participant id="Pool_a" name="Кладовщик" processRef="PrA"/>
    <participant id="Pool_b" name="Склад" processRef="PrB"/>
  </collaboration>
  <process id="PrA" name="Кладовщик" isExecutable="true">
    <startEvent id="A1" name="Старт"><outgoing>AF1</outgoing></startEvent>
    <sequenceFlow id="AF1" sourceRef="A1" targetRef="A2"/>
    <userTask id="A2" name="Собрать груз"><incoming>AF1</incoming><outgoing>AF2</outgoing></userTask>
    <sequenceFlow id="AF2" sourceRef="A2" targetRef="A3"/>
    <endEvent id="A3" name="Готово"><incoming>AF2</incoming></endEvent>
    <userTask id="A4" name="Заказать паллету"/>
  </process>
  <process id="PrB" name="Склад" isExecutable="true">
    <startEvent id="B1" name="Приёмка"><outgoing>BF1</outgoing></startEvent>
    <sequenceFlow id="BF1" sourceRef="B1" targetRef="B2"/>
    <userTask id="B2" name="Принять груз"><incoming>BF1</incoming><outgoing>BF2</outgoing></userTask>
    <sequenceFlow id="BF2" sourceRef="B2" targetRef="B3"/>
    <endEvent id="B3" name="Принято"><incoming>BF2</incoming></endEvent>
  </process>
</definitions>"""
    plan = json.dumps({"analysis": "сливаем пулы", "operations": [
        {"op": "add_documentation", "id": "A2", "text": "Собираем по заявке"},
        {"op": "merge_participants", "source": "Кладовщик", "target": "Склад",
         "as_lane": True},
    ]}, ensure_ascii=False)
    fake = FakeLLM(monkeypatch, _wrap(plan),
                   _wrap('{"analysis": "не сливаем", "operations": []}'))
    with pytest.raises(ImprovementError) as exc:
        _improve(orchestrator, xml)
    message = str(exc.value)
    assert "не удалось применить" in message
    assert "слияние пулов сделало схему невалидной" in message
    assert "A4" in message
    # корректирующий повтор получил причину отката и подсказку
    assert "встройте шаги пула-источника в маршрут" in fake.prompts[1]


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
    """Таймер без ветки обработки аплайер откатывает: принятое улучшение не
    имеет права ронять качество. Если и повтор принёс то же, пользователь
    получает причину, а не молчаливое «улучшение не применено»."""
    plan = '{"analysis": "добавил таймер", "operations": [' \
           '{"op":"add_boundary_event","id":"new_BE","attached_to":"T_collect",' \
           '"event_type":"timer","name":"Просрочка"}]}'
    FakeLLM(monkeypatch, _wrap(plan), _wrap(plan))
    with pytest.raises(ImprovementError) as exc:
        _improve(orchestrator, single_pool_xml)
    assert "без ветки обработки" in str(exc.value)


def test_package_that_closes_an_unguarded_cycle_is_rolled_back(orchestrator,
                                                               monkeypatch):
    """Соединение, замкнувшее маршрут без ветки выхода, принимает схему в
    ухудшение (`guarded_cycles` падает). Такой пакет откатывается к базе, а
    модель получает шанс перестроить ветку — аплайер не вправе додумывать
    шлюз за неё."""
    linear = '<?xml version="1.0" encoding="UTF-8"?>\n' + """
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">
  <process id="P" name="П" isExecutable="true">
    <startEvent id="S" name="Старт"/>
    <sequenceFlow id="F1" sourceRef="S" targetRef="A"/>
    <userTask id="A" name="Шаг А"/>
    <sequenceFlow id="F2" sourceRef="A" targetRef="B"/>
    <userTask id="B" name="Шаг Б"/>
    <sequenceFlow id="F3" sourceRef="B" targetRef="E"/>
    <endEvent id="E" name="Финиш"/>
  </process>
</definitions>"""
    plan = '{"analysis": "замкнул маршрут", "operations": [' \
           '{"op":"connect","source":"B","target":"A"}]}'
    FakeLLM(monkeypatch, _wrap(plan), _wrap(plan))
    analysis, xml_after, report = _improve(orchestrator, linear,
                                           prompt="Свяжи шаги по кругу")
    assert 'sourceRef="B" targetRef="A"' not in xml_after
    cycles = [s for s in report["skipped"]
              if "цикл без защищённого выхода" in s["reason"]]
    assert cycles, report["skipped"]
    assert "исключающий шлюз" in cycles[0]["hint"]


def test_retry_closes_the_handler_branch_the_rollback_asked_for(orchestrator,
                                                                monkeypatch,
                                                                single_pool_xml):
    """Подсказка отката работает: повтор, где модель добавила ветку обработки,
    применяется, и таймер остаётся в схеме."""
    broken = '{"analysis": "таймер", "operations": [' \
             '{"op":"add_boundary_event","id":"new_BE","attached_to":"T_collect",' \
             '"event_type":"timer","name":"Просрочка"}]}'
    fixed = '{"analysis": "таймер с эскалацией", "operations": [' \
            '{"op":"add_boundary_event","id":"new_BE","attached_to":"T_collect",' \
            '"event_type":"timer","name":"Просрочка"},' \
            '{"op":"add_task","id":"new_BE_h","name":"Эскалация",' \
            '"task_type":"userTask","after":"new_BE"},' \
            '{"op":"connect","source":"new_BE_h","target":"End_cancel"}]}'
    FakeLLM(monkeypatch, _wrap(broken), _wrap(fixed))
    analysis, xml_after, report = _improve(orchestrator, single_pool_xml)
    assert 'id="new_BE"' in xml_after
    assert report["applied"] and report["status"] in ("success", "partial")
    assert "остались вне маршрута" not in analysis


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


def test_repair_that_rips_out_a_package_flow_rolls_it_back_and_is_retried(
        orchestrator, monkeypatch, single_pool_xml):
    """Починка идёт после аплайера и вправе снять дугу — тогда узел пакета
    остаётся кружком без маршрута, и контур обязан откатить его сам: в прогоне
    #40 такое «улучшение» приняли, и принятие потеряло 15 баллов на
    `boundary_handled`. Откат — пропуск, значит модель получает о нём подсказку
    в корректирующем повторе и может добить ветку."""
    first = '{"analysis": "таймер", "operations": [' \
            '{"op":"add_boundary_event","id":"new_T1","attached_to":"T_collect",' \
            '"event_type":"timer","name":"Долгий разбор","to":"T_ship"}]}'
    second = '{"analysis": "таймер с веткой", "operations": [' \
             '{"op":"add_boundary_event","id":"new_T1","attached_to":"T_collect",' \
             '"event_type":"timer","name":"Долгий разбор"},' \
             '{"op":"add_task","id":"new_H","name":"Эскалация","task_type":"userTask",' \
             '"after":"new_T1"},' \
             '{"op":"connect","source":"new_H","target":"T_ship"}]}'
    fake = FakeLLM(monkeypatch, _wrap(first), _wrap(second))
    real_repair = bpmn_edits.validate_and_repair
    strips = {"left": 1}

    def repair_then_strip(xml_text):
        """Первая починка ведёт себя как в живом прогоне: переподвешивает дугу и
        оставляет граничное событие без исхода."""
        xml, notes = real_repair(xml_text)
        if strips["left"]:
            strips["left"] -= 1
            xml = re.sub(r'<bpmn:sequenceFlow[^>]*sourceRef="new_T1"[^>]*/>',
                         "", xml)
        return xml, notes

    monkeypatch.setattr(bpmn_edits, "validate_and_repair", repair_then_strip)
    analysis, improved_xml, report = _improve(orchestrator, single_pool_xml)

    rolled = [s for s in report["skipped"] if s.get("stage") == "repair"]
    assert [s["id"] for s in rolled] == ["new_T1"]
    assert "откачено после починки" in rolled[0]["reason"]
    # Откат дошёл до модели: повтор знает, что исход надо задать той же
    # операцией, и доводит ветку до конца.
    assert len(fake.calls) == 2
    assert "откачено после починки" in fake.prompts[1]
    assert 'id="new_H"' in improved_xml
    assert 'sourceRef="new_T1"' in improved_xml
    # Маршрут исходной схемы цел: откатан был только узел пакета.
    assert 'sourceRef="T_collect"' in improved_xml


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


# --- промпт как обязательство перед аплайером -------------------------------

PROMPT_EXAMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"'
    ' id="D_prompt_example">'
    '<collaboration id="Collaboration_pe">'
    '<participant id="Pool_wh" name="Цех фасовки" processRef="Process_wh"/>'
    '<participant id="Pool_courier" name="Транспортный отдел"'
    ' processRef="Process_courier"/>'
    '</collaboration>'
    '<process id="Process_wh" name="Цех фасовки" isExecutable="true">'
    '<laneSet id="LaneSet_wh"><lane id="Lane_1" name="Техник цеха">'
    '<flowNodeRef>A2</flowNodeRef><flowNodeRef>A3</flowNodeRef></lane></laneSet>'
    '<startEvent id="A_start" name="Наряд"><outgoing>AF0</outgoing></startEvent>'
    '<sequenceFlow id="AF0" sourceRef="A_start" targetRef="A2"/>'
    '<userTask id="A2" name="Заменить деталь">'
    '<incoming>AF0</incoming><outgoing>F2</outgoing></userTask>'
    '<sequenceFlow id="F2" sourceRef="A2" targetRef="A3"/>'
    '<userTask id="A3" name="Запустить линию">'
    '<incoming>F2</incoming><outgoing>AF9</outgoing></userTask>'
    '<sequenceFlow id="AF9" sourceRef="A3" targetRef="A_end"/>'
    '<endEvent id="A_end" name="Линия в работе"><incoming>AF9</incoming>'
    '</endEvent>'
    '</process>'
    '<process id="Process_courier" name="Транспортный отдел" isExecutable="true">'
    '<startEvent id="C_start" name="Вызов"><outgoing>CF1</outgoing></startEvent>'
    '<sequenceFlow id="CF1" sourceRef="C_start" targetRef="C_end"/>'
    '<endEvent id="C_end" name="Закрыт"><incoming>CF1</incoming></endEvent>'
    '</process>'
    '</definitions>')


ROLE_POOL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"'
    ' id="D_role_pool">'
    '<collaboration id="Collaboration_rp">'
    '<participant id="Pool_vv" name="ВкусВилл" processRef="Process_vv"/>'
    '<participant id="Pool_ks" name="Кладовщик" processRef="Process_ks"/>'
    '<messageFlow id="MF1" sourceRef="A1" targetRef="K1"/>'
    '<messageFlow id="MF2" sourceRef="K1" targetRef="A2"/>'
    '</collaboration>'
    # «Кладовщик» — и участник, и дорожка чужого процесса: тот самый дефект,
    # за который скоринг снимает баллы, а планировщик обязан его увидеть.
    '<process id="Process_vv" name="ВкусВилл" isExecutable="true">'
    '<laneSet id="LaneSet_vv"><lane id="Lane_1" name="Кладовщик">'
    '<flowNodeRef>A2</flowNodeRef></lane></laneSet>'
    '<startEvent id="S1" name="Заявка"><outgoing>F1</outgoing></startEvent>'
    '<sequenceFlow id="F1" sourceRef="S1" targetRef="A1"/>'
    '<userTask id="A1" name="Собрать заявку">'
    '<incoming>F1</incoming><outgoing>F2</outgoing></userTask>'
    '<sequenceFlow id="F2" sourceRef="A1" targetRef="A2"/>'
    '<userTask id="A2" name="Принять товар">'
    '<incoming>F2</incoming><outgoing>F3</outgoing></userTask>'
    '<sequenceFlow id="F3" sourceRef="A2" targetRef="E1"/>'
    '<endEvent id="E1" name="Товар принят"><incoming>F3</incoming></endEvent>'
    '</process>'
    '<process id="Process_ks" name="Кладовщик" isExecutable="true">'
    '<startEvent id="KS1" name="Смена началась"><outgoing>KF1</outgoing>'
    '</startEvent>'
    '<sequenceFlow id="KF1" sourceRef="KS1" targetRef="K1"/>'
    '<userTask id="K1" name="Оприходовать накладную">'
    '<incoming>KF1</incoming><outgoing>KF2</outgoing></userTask>'
    '<sequenceFlow id="KF2" sourceRef="K1" targetRef="KE1"/>'
    '<endEvent id="KE1" name="Смена закрыта"><incoming>KF2</incoming></endEvent>'
    '</process>'
    '</definitions>')


def _prompt_example_plan():
    """Достаёт JSON-пример из системного промпта планировщика."""
    text = llm_improve._SYSTEM_PROMPT
    start = text.index('{"analysis"', text.index("ПРИМЕР ОТВЕТА"))
    plan, _ = json.JSONDecoder().raw_decode(text, start)
    return plan


class TestPromptContract:
    def test_forbidden_operation_names_are_really_absent(self):
        """Промпт запрещает имена, которых в словаре нет (`add_flow`,
        `add_userTask` — модель их выдумывала в живых прогонах). Если такое имя
        когда-нибудь станет операцией, запрет в промпте надо снять, а не оставить
        враньё."""
        for name in ("add_flow", "remove_flow", "add_userTask"):
            assert name not in bpmn_edits.OP_SPEC
            assert name in llm_improve._SYSTEM_PROMPT
        assert "ДОПУСТИМЫЕ ОПЕРАЦИИ" in llm_improve._SYSTEM_PROMPT

    def test_example_plan_applies_without_a_single_refusal(self):
        """Модель учится на примере из промпта: если хоть одна его правка
        отвергается аплайером, пример учит недостижимому."""
        plan = _prompt_example_plan()
        out, report = bpmn_edits.apply_operations(PROMPT_EXAMPLE_XML,
                                                  plan["operations"])
        assert report["skipped"] == []
        assert report["status"] == "success"
        assert bpmn_edits.validate_and_repair(out)[1] == []
        assert plan["analysis"]

    def test_example_uses_only_documented_operations_and_fields(self):
        for op in _prompt_example_plan()["operations"]:
            assert op["op"] in bpmn_edits.OP_SPEC
            spec = bpmn_edits.OP_SPEC[op["op"]]
            for key in op:
                assert key in spec, f"{op['op']}: поле {key} есть в примере, " \
                                    f"но не описано в OP_SPEC"

    def test_retry_prompt_drops_practices_and_routes_pool_notes(
            self, orchestrator, monkeypatch, single_pool_xml):
        """Повтор чинит пропуски, а не ищет новые улучшения; пустой пул — это
        не «узел вне маршрута», и совет у него свой."""
        plan = _wrap(json.dumps({
            "analysis": "заводим участник",
            "operations": [{"op": "add_participant", "id": "new_P",
                            "name": "Архив"}]}, ensure_ascii=False))
        fake = FakeLLM(monkeypatch, plan,
                       _wrap('{"analysis": "добил", "operations": []}'))
        recommendations, _, _ = _improve(orchestrator, single_pool_xml)

        assert len(fake.calls) == 2
        assert "ЛУЧШИЕ ПРАКТИКИ" in fake.prompts[0]
        assert "ЛУЧШИЕ ПРАКТИКИ" not in fake.prompts[1]
        assert "ПУЛЫ БЕЗ ШАГОВ" in fake.prompts[1]
        assert "УЗЛЫ ВНЕ МАРШРУТА" not in fake.prompts[1]
        assert "Остались пулы без единого шага" in recommendations

    def test_request_too_large_is_not_reported_as_unavailable(
            self, orchestrator, monkeypatch, single_pool_xml):
        FakeLLM(monkeypatch, llm_client.LLMRequestTooLargeError("не влезло"))
        with pytest.raises(ImprovementError) as exc:
            _improve(orchestrator, single_pool_xml)
        assert not isinstance(exc.value, ImprovementUnavailable)
        assert "слишком велика" in str(exc.value)

    def test_operations_not_a_list_is_told_to_the_user(
            self, orchestrator, monkeypatch, single_pool_xml):
        plan = _wrap(json.dumps({"analysis": "разбор",
                                 "operations": {"op": "rename"}},
                                ensure_ascii=False))
        FakeLLM(monkeypatch, plan)
        recommendations, improved_xml, report = _improve(orchestrator,
                                                         single_pool_xml)
        assert improved_xml is None
        assert report["status"] == "analysis_only"
        assert "не списком" in recommendations

    def test_truncated_inventory_is_announced_in_the_prompt(
            self, orchestrator, monkeypatch, single_pool_xml):
        monkeypatch.setattr(bpmn_edits, "INVENTORY_MAX_ELEMENTS", 1)
        fake = FakeLLM(monkeypatch, '{"analysis": "ок", "operations": []}')
        _improve(orchestrator, single_pool_xml)
        assert "Часть схемы не показана" in fake.prompts[0]
        assert "elements_omitted" in fake.prompts[0]

    def test_scoring_findings_reach_the_planner_and_stop_at_the_retry(
            self, orchestrator, monkeypatch):
        """Скоринг и есть оценивающий шаг контура: план обязан видеть, за что у
        схемы сняли баллы (иначе пакет уходит в документацию рядом с правилом на
        −8), а повтор добивает пропуски и новых сюжетов не ищет."""
        plan = _wrap(json.dumps({"analysis": "разбор", "operations": [
            {"op": "add_participant", "id": "new_P", "name": "Архив"}]},
            ensure_ascii=False))
        fake = FakeLLM(monkeypatch, plan,
                       _wrap('{"analysis": "добил", "operations": []}'))
        _improve(orchestrator, ROLE_POOL_XML)
        assert "УЗКИЕ МЕСТА ПО СКОРИНГУ" in fake.prompts[0]
        assert "merge_participants" in fake.prompts[0]
        assert "УЗКИЕ МЕСТА ПО СКОРИНГУ" not in fake.prompts[1]

    def test_scheme_without_findings_gets_no_block(self, orchestrator, monkeypatch,
                                                   single_pool_xml):
        class _NoFindings:
            @staticmethod
            def evaluate(_xml):
                return {"recommendations": []}

        monkeypatch.setattr(llm_improve, "_scorer", _NoFindings())
        fake = FakeLLM(monkeypatch, '{"analysis": "ок", "operations": []}')
        _improve(orchestrator, single_pool_xml)
        assert "УЗКИЕ МЕСТА ПО СКОРИНГУ" not in fake.prompts[0]

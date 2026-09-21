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

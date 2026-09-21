"""Генератор BPMN: от JSON-плана модели до импортируемой схемы.

Транспорт LLM подменён на уровне ``_complete`` (как в
tests/test_improve_orchestrator.py), поэтому проверяется настоящий разбор
ответа, починка структуры и emission XML — без сети и без ключа.

Главное, что здесь закреплено: роли сотрудников — дорожки одного пула, а не
отдельные пулы, и ни одна правка недоверенного плана не остаётся без note.
"""
import json
import xml.etree.ElementTree as ET

import pytest

from core import bpmn_generator, llm_client
from core.bpmn_edits import build_inventory, validate_and_repair
from core.bpmn_generator import BPMNGenerator, repair_structure
from core.bpmn_scoring import BPMNScorer

BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"


class FakeLLM:
    """Отдаёт заготовленные сырые ответы по одному на вызов транспорта."""

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
    def system_prompt(self):
        return self.calls[0][0]["content"]


def _plan_dict(**overrides) -> dict:
    """Эталонный план модели: роли — дорожки одного пула «ВкусВилл»."""
    base = {
        "participants": ["ВкусВилл"],
        "lanes": [
            {"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
            {"id": "L_b", "name": "Руководитель", "participant": "ВкусВилл"},
            {"id": "L_acc", "name": "Бухгалтерия", "participant": "ВкусВилл"},
            {"id": "L_s", "name": "Склад", "participant": "ВкусВилл"},
        ],
        "elements": [
            _e("S1", "startEvent", "Заявка поступила", "L_m"),
            _e("T1", "userTask", "Проверить заявку", "L_m"),
            _e("T2", "userTask", "Согласовать с руководителем", "L_b"),
            _e("G1", "exclusiveGateway", "Согласована ли заявка?", "L_b"),
            _e("T3", "userTask", "Выставить счёт", "L_acc"),
            _e("T4", "userTask", "Отказать клиенту", "L_m"),
            _e("E1", "endEvent", "Отказ", "L_m"),
            _e("T5", "userTask", "Отгрузить со склада", "L_s"),
            _e("E2", "endEvent", "Товар отгружен", "L_s"),
        ],
        "flows": [
            _f("F1", "S1", "T1"),
            _f("F2", "T1", "T2"),
            _f("F3", "T2", "G1"),
            _f("F4", "G1", "T3", condition="Да"),
            _f("F5", "G1", "T4", condition="Нет"),
            _f("F6", "T4", "E1"),
            _f("F7", "T3", "T5"),
            _f("F8", "T5", "E2"),
        ],
    }
    base.update(overrides)
    return base


def _fence(plan: dict) -> str:
    return "```json\n" + json.dumps(plan, ensure_ascii=False) + "\n```"


def _plan(**overrides) -> str:
    return _fence(_plan_dict(**overrides))


def _e(elem_id, kind, name, lane, participant="ВкусВилл", **extra):
    return {"id": elem_id, "kind": kind, "name": name,
            "participant": participant, "lane": lane, **extra}


def _f(flow_id, source, target, kind="sequence", condition="", **extra):
    return {"id": flow_id, "source": source, "target": target,
            "kind": kind, "condition": condition, **extra}


def _generate(monkeypatch, *responses) -> dict:
    FakeLLM(monkeypatch, *responses)
    return BPMNGenerator().generate("клиент оставляет заявку, менеджер её "
                                    "согласует, бухгалтерия выставляет счёт")


def _flow_nodes(root):
    return {child.get("id"): child
            for process in root.iter(f"{BPMN}process")
            for child in process
            if isinstance(child.tag, str)
            and child.tag.replace(BPMN, "") not in
            ("laneSet", "sequenceFlow", "association", "textAnnotation")}


def _kinds(structure):
    return {e["id"]: e["kind"] for e in structure["elements"]}


def _note(notes, fragment):
    return any(fragment in note for note in notes)


class TestRolesAreLanesNotPools:
    """Дефект из ревью: линейный поток распадался на пять пулов и цепочку
    messageFlow."""

    def test_single_pool_with_lanes_and_no_message_flows(self, monkeypatch):
        result = _generate(monkeypatch, _plan())
        assert result["status"] == "success"
        assert [p["name"] for p in result["structure"]["participants"]] == ["ВкусВилл"]

        inventory = build_inventory(result["bpmn"])
        assert len(inventory["participants"]) == 1
        assert [f["kind"] for f in inventory["flows"]] == ["sequence"] * 8

        root = ET.fromstring(result["bpmn"])
        process = root.find(f"{BPMN}process")
        lane_set = process.find(f"{BPMN}laneSet")
        # laneSet — первый ребёнок процесса, дорожки содержат только ссылки.
        assert process[0] is lane_set
        lanes = lane_set.findall(f"{BPMN}lane")
        assert [lane.get("name") for lane in lanes] == \
            ["Менеджер", "Руководитель", "Бухгалтерия", "Склад"]
        for lane in lanes:
            assert all(ref.tag == f"{BPMN}flowNodeRef" for ref in lane)
        placed = {ref.text for lane in lanes
                  for ref in lane.findall(f"{BPMN}flowNodeRef")}
        assert placed == {e["id"] for e in inventory["elements"]}

    def test_consistent_plan_needs_no_repairs(self, monkeypatch):
        assert _generate(monkeypatch, _plan())["notes"] == []

    def test_prompt_separates_pools_from_lanes(self, monkeypatch):
        fake = FakeLLM(monkeypatch, _plan())
        BPMNGenerator().generate("описание")
        prompt = fake.system_prompt
        assert '"lanes"' in prompt and '"lane"' in prompt
        assert "ДОРОЖКИ" in prompt
        assert "РАЗНЫХ пулов" in prompt

    def test_prompt_shows_one_shot_of_roles_as_lanes(self, monkeypatch):
        """Правило без примера модель выполняет неустойчиво: в живом прогоне
        роли разъезжались по пулам даже с правилами в промпте."""
        fake = FakeLLM(monkeypatch, _plan())
        BPMNGenerator().generate("описание")
        prompt = fake.system_prompt
        assert 'Пример' in prompt
        assert '"participants": ["ВкусВилл", "Перевозчик"]' in prompt
        assert "роли одной организации" in prompt
        # В примере развилка имеет пару: G1 расщепляет, G2 сливает.
        assert '"id": "G2", "kind": "exclusiveGateway"' in prompt
        # Зеркало правила достижимости: у шага должен быть и выход.
        assert "кроме endEvent" in prompt


class TestUnknownParticipant:
    def test_unknown_pool_name_creates_own_pool_with_note(self, monkeypatch):
        plan = _plan(
            participants=["Сайт"],
            lanes=[],
            elements=[
                _e("Z1", "startEvent", "Заявка", "", "Сайт"),
                _e("Z4", "userTask", "Принять заявку", "", "Сайт"),
                _e("Z3", "endEvent", "Товар выдан", "", "Сайт"),
                _e("Z2", "userTask", "Отгрузить", "", "Склад"),
            ],
            flows=[_f("F1", "Z1", "Z4"), _f("F2", "Z4", "Z3")],
        )
        result = _generate(monkeypatch, plan)
        pools = [p["name"] for p in result["structure"]["participants"]]
        assert pools == ["Сайт", "Склад"]
        assert _note(result["notes"], "Создан пул")
        assert next(e for e in result["structure"]["elements"]
                    if e["id"] == "Z2")["participant"] == "Склад"

    def test_declined_case_and_typo_map_to_existing_pool(self, monkeypatch):
        plan = _plan(
            participants=["Бухгалтерия"],
            lanes=[],
            elements=[
                _e("B1", "startEvent", "Счёт", "", "Бухгалтерия"),
                _e("B2", "userTask", "Провести", "", "бухгалтерия "),
                _e("B3", "userTask", "Архив", "", "Бухгалтерией"),
                _e("B4", "endEvent", "Готово", "", "Бухгалтерия"),
            ],
            flows=[_f("F1", "B1", "B2"), _f("F2", "B2", "B3"), _f("F3", "B3", "B4")],
        )
        result = _generate(monkeypatch, plan)
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["Бухгалтерия"]
        assert _note(result["notes"], "сопоставлен")

    def test_lane_reveals_pool_before_new_pool_is_invented(self, monkeypatch):
        """Элемент без пула, но с дорожкой остаётся в пуле своей дорожки."""
        plan = _plan(
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m", participant=""),
                _e("T1", "userTask", "Проверить заявку", "L_m", participant=""),
                _e("G1", "exclusiveGateway", "Согласована?", "L_m", participant=""),
                _e("T3", "userTask", "Выставить счёт", "L_acc", participant=""),
                _e("T4", "userTask", "Отказать", "L_m", participant=""),
                _e("E1", "endEvent", "Отказ", "L_m", participant=""),
                _e("E2", "endEvent", "Товар отгружен", "L_s", participant=""),
            ],
            flows=[
                _f("F1", "S1", "T1"), _f("F2", "T1", "G1"),
                _f("F3", "G1", "T3", condition="Да"),
                _f("F4", "G1", "T4", condition="Нет"),
                _f("F5", "T4", "E1"),
                # E2 в дорожке «Склад» — её пул и становится пулом элемента.
                _f("F6", "T3", "E2"),
            ],
            lanes=[
                {"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                {"id": "L_acc", "name": "Бухгалтерия", "participant": "ВкусВилл"},
                {"id": "L_s", "name": "Склад", "participant": "ВкусВилл"},
            ],
        )
        result = _generate(monkeypatch, plan)
        assert [p["name"] for p in result["structure"]["participants"]] == ["ВкусВилл"]
        assert [f["kind"] for f in result["structure"]["flows"]].count("message") == 0


class TestLaneRepair:
    """Дорожки — часть плана модели: id валидируются, дубликаты и неизвестные
    ссылки чинятся и подписываются в notes."""

    def test_lane_ids_deduped_and_unknown_lane_created(self):
        repaired, notes = repair_structure(_plan_dict(
            lanes=[
                {"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                {"id": "L_m", "name": "Руководитель", "participant": "ВкусВилл"},
                {"id": "L_dup", "name": "МЕНЕДЖЕР", "participant": "ВкусВилл"},
            ],
            elements=[
                _e("1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Выставить счёт", "Бухгалтерия"),
                _e("T2", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Готово", "L_m_x"),
            ],
            flows=[_f("F1", "e_1", "T1"), _f("F2", "T1", "T2"), _f("F3", "T2", "E1")],
        ))
        lanes = repaired["lanes"]
        assert [lane["name"] for lane in lanes] == ["Менеджер", "Руководитель",
                                                    "Бухгалтерия"]
        assert len({lane["id"] for lane in lanes}) == len(lanes)
        assert {lane["participant"] for lane in lanes} == {"ВкусВилл"}
        assert _note(notes, "приведён к «L_m_x»")
        assert _note(notes, "дубликат пропущен")
        assert _note(notes, "не объявлена — создана")
        assert _note(notes, "дорожка не указана")

        lane_of = {e["id"]: e["lane"] for e in repaired["elements"]}
        created = next(lane for lane in lanes if lane["id"] == lane_of["T1"])
        assert created["name"] == "Бухгалтерия"
        assert lane_of["T2"] == "L_m"
        assert lane_of["E1"] == "L_m_x"
        assert "e_1" in {e["id"] for e in repaired["elements"]}

    def test_lane_limit_keeps_plan_bounded_and_reports(self):
        lanes = [{"id": f"L{i}", "name": f"Роль {i}", "participant": "ВкусВилл"}
                 for i in range(bpmn_generator.MAX_LANES + 5)]
        repaired, notes = repair_structure(_plan_dict(
            lanes=lanes,
            elements=[
                _e("S1", "startEvent", "Заявка", "L0"),
                _e("T1", "userTask", "Проверить", f"L{bpmn_generator.MAX_LANES + 4}"),
                _e("E1", "endEvent", "Готово", "L0"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1")],
        ))
        assert len(repaired["lanes"]) == bpmn_generator.MAX_LANES
        assert _note(notes, "Лишние дорожки отброшены")
        # Запретная ссылка не создаёт новую дорожку: элемент в первую дорожку пула.
        assert next(e for e in repaired["elements"]
                    if e["id"] == "T1")["lane"] == "L0"
        assert _note(notes, "не создана")


class TestSingleBranchGateway:
    def test_demoted_to_task_and_condition_dropped(self, monkeypatch):
        plan = _plan(
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("G1", "exclusiveGateway", "Согласована ли заявка?", "L_m"),
                _e("T1", "userTask", "Отказать клиенту", "L_m"),
                _e("E1", "endEvent", "Отказ", "L_m"),
            ],
            flows=[_f("F1", "S1", "G1"),
                   _f("F2", "G1", "T1", condition="Нет"),
                   _f("F3", "T1", "E1")],
        )
        result = _generate(monkeypatch, plan)
        assert _kinds(result["structure"])["G1"] == "task"
        assert _note(result["notes"], "понижен до задачи")
        assert _note(result["notes"], "снято с потока")
        assert "<bpmn:exclusiveGateway" not in result["bpmn"]
        assert "conditionExpression" not in result["bpmn"]

    def test_real_branching_gateway_untouched(self, monkeypatch):
        result = _generate(monkeypatch, _plan())
        assert _kinds(result["structure"])["G1"] == "exclusiveGateway"
        assert "<bpmn:exclusiveGateway" in result["bpmn"]


class TestReachability:
    def test_orphan_node_connected_from_start_with_note(self, monkeypatch):
        plan = _plan(
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Проверить заявку", "L_m"),
                _e("T9", "userTask", "Отгрузить со склада", "L_s"),
                _e("E2", "endEvent", "Товар отгружен", "L_s"),
                _e("E1", "endEvent", "Проверка завершена", "L_m"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "T9", "E2")],
        )
        result = _generate(monkeypatch, plan)
        added = [f for f in result["structure"]["flows"]
                 if f["source"] == "S1" and f["target"] == "T9"]
        assert [f["kind"] for f in added] == ["sequence"]
        assert _note(result["notes"], "подключён от стартового события")

    def test_no_edge_that_closes_a_cycle(self, monkeypatch):
        plan = _plan(
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Проверить заявку", "L_m"),
                _e("T8", "userTask", "Повторная сверка", "L_m"),
                _e("E1", "endEvent", "Проверка завершена", "L_m"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "T8", "S1")],
        )
        result = _generate(monkeypatch, plan)
        assert not [f for f in result["structure"]["flows"]
                    if f["source"] == "S1" and f["target"] == "T8"]
        assert _note(result["notes"], "замкнул бы цикл")

    def test_reachability_budget_is_bounded(self, monkeypatch):
        # Лимит правок достижимости существует и обязывает заметку: проверяем
        # его на урезанном значении, иначе 60 элементов его не достигнут.
        monkeypatch.setattr(bpmn_generator, "MAX_REACH_FLOWS", 1)
        repaired, notes = repair_structure(_plan_dict(
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Проверить", "L_m"),
                _e("T2", "userTask", "Согласовать", "L_b"),
                _e("T3", "userTask", "Отгрузить", "L_s"),
            ],
            flows=[],
        ))
        # Правок больше одной, и последний сирота остаётся как есть — с
        # заметкой вместо молчаливой доводки схемы.
        incoming = {f["target"] for f in repaired["flows"]
                    if f["kind"] == "sequence"}
        assert "T3" not in incoming
        assert _note(notes, "лимит исправлений достижимости")

    def test_missing_start_and_end_added_per_pool(self, monkeypatch):
        plan = _plan(
            elements=[_e("T1", "userTask", "Проверить заявку", "L_m")],
            flows=[],
        )
        result = _generate(monkeypatch, plan)
        kinds = sorted(_kinds(result["structure"]).values())
        assert kinds == ["endEvent", "startEvent", "userTask"]
        assert _note(result["notes"], "стартовое событие")
        assert _note(result["notes"], "завершающее событие")


class TestDeadStarts:
    """Старт пула, принявшего сообщение, обязан запускать его процесс."""

    @staticmethod
    def _message_first():
        return _plan(
            participants=["Заказчик", "Исполнитель"],
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", "", "Заказчик"),
                _e("A1", "userTask", "Оформить заявку", "", "Заказчик"),
                _e("E1", "endEvent", "Заявка отправлена", "", "Заказчик"),
                _e("S2", "startEvent", "Входящая заявка", "", "Исполнитель"),
                _e("A5", "serviceTask", "Выполнить работу", "", "Исполнитель"),
            ],
            flows=[_f("F1", "S1", "A1"), _f("F2", "A1", "E1"),
                   _f("M1", "A1", "A5", kind="message")],
        )

    def test_start_of_message_pool_connected_to_its_first_step(self, monkeypatch):
        result = _generate(monkeypatch, self._message_first())
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("S2", "A5") in flows
        assert _note(result["notes"], "Старт S2 был без исходящего потока")
        # Достижимость A5 не подключает к чужому старту: она бережёт смысл
        # «жду сообщение», а свой старт пула обязан запускать процесс.
        incoming_seq = [f["source"] for f in result["structure"]["flows"]
                        if f["target"] == "A5" and f["kind"] == "sequence"]
        assert incoming_seq == ["S2"]

    def test_start_and_incoming_step_pass_connectivity_rules(self, monkeypatch):
        result = _generate(monkeypatch, self._message_first())
        details = BPMNScorer().evaluate(result["bpmn"])["details"]
        assert details["sequence_flows"] and details["no_isolated"]

    def test_pool_with_only_a_start_reaches_its_new_end(self, monkeypatch):
        """Пулу из одного старта добавлен финиш — и старт обязан к нему вести,
        иначе оба события висят отдельно."""
        plan = _plan(
            participants=["Внешний мир"],
            lanes=[],
            elements=[_e("S1", "startEvent", "Сигнал", "", "Внешний мир")],
            flows=[],
        )
        result = _generate(monkeypatch, plan)
        end = next(e for e in result["structure"]["elements"]
                   if e["kind"] == "endEvent")
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("S1", end["id"]) in flows
        # Событие и его единственный выход добавлены одним шагом: повторной
        # дуги от «починки старта» быть не должно.
        assert flows.count(("S1", end["id"])) == 1


    def test_pool_of_only_start_events_does_not_crash(self, monkeypatch):
        """Вести старт некуда, а выдумывать шаг нельзя — это заметка, а не
        падение генерации (из неё собирается ответ 500)."""
        plan = _plan(
            participants=["Заявка"],
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Старт 1", "", "Заявка"),
                _e("S2", "startEvent", "Старт 2", "", "Заявка"),
            ],
            flows=[],
        )
        result = _generate(monkeypatch, plan)
        assert result["status"] == "success"
        assert _note(result["notes"], "в пуле нет ни одного шага")


class TestDeadEndSteps:
    """Шаг, отправивший сообщение в другой пул, обязан завершаться в своём.

    Модель раздала роли по пулам — внутри своего пула шаг обрывается тупиком,
    и скоринг считает это правомочно. Генератор закрывает обрыв конечным
    событием своего пула и объясняет правку, а не рисует поток от старта к
    сиротевшему финишу.
    """

    @staticmethod
    def _two_pools():
        return _plan(
            participants=["Инициатор", "Руководитель"],
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Потребность", "", "Инициатор"),
                _e("A1", "userTask", "Завести заявку", "", "Инициатор"),
                _e("E1", "endEvent", "Заявка отправлена", "", "Инициатор"),
                _e("S2", "startEvent", "Входящая заявка", "", "Руководитель"),
                _e("A2", "userTask", "Согласовать заявку", "", "Руководитель"),
                _e("E2", "endEvent", "Решение принято", "", "Руководитель"),
            ],
            flows=[_f("F1", "S1", "A1"),
                   _f("M1", "A1", "A2", kind="message"),
                   _f("F2", "S2", "A2"), _f("F3", "A2", "E2")],
        )

    def test_message_only_step_closed_by_own_end_event(self, monkeypatch):
        result = _generate(monkeypatch, self._two_pools())
        added = [f for f in result["structure"]["flows"]
                 if f["source"] == "A1" and f["target"] == "E1"]
        assert [f["kind"] for f in added] == ["sequence"]
        assert _note(result["notes"], "был без исходящего потока")
        # Шаг отправляет сообщение — подсказка про пулы и дорожки уместна.
        assert _note(result["notes"], "это дорожка одного пула")

    def test_generated_plan_passes_connectivity_rules(self, monkeypatch):
        result = _generate(monkeypatch, self._two_pools())
        details = BPMNScorer().evaluate(result["bpmn"])["details"]
        assert details["sequence_flows"] and details["no_isolated"]

    def test_dead_end_without_message_gets_plain_note(self, monkeypatch):
        """Финиш есть, шагу некуда вести — поток идёт к финишу, а не от старта
        к финишу в обход всего процесса."""
        plan = _plan(
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", ""),
                _e("T1", "userTask", "Проверить заявку", ""),
                _e("E1", "endEvent", "Проверено", ""),
            ],
            flows=[_f("F1", "S1", "T1")],
        )
        result = _generate(monkeypatch, plan)
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("T1", "E1") in flows
        assert ("S1", "E1") not in flows
        assert _note(result["notes"], "был без исходящего потока")
        assert not _note(result["notes"], "дорожка одного пула")

    def test_no_edge_that_closes_a_cycle(self, monkeypatch):
        plan = _plan(
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", ""),
                _e("T1", "userTask", "Проверить заявку", ""),
                _e("E1", "endEvent", "Проверено", ""),
                _e("T8", "userTask", "Повторная сверка", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"), _f("F3", "E1", "T8")],
        )
        result = _generate(monkeypatch, plan)
        assert not [f for f in result["structure"]["flows"]
                    if f["source"] == "T8" and f["target"] == "E1"]
        assert _note(result["notes"], "замкнул бы цикл")

    def test_budget_is_bounded_and_reported(self, monkeypatch):
        monkeypatch.setattr(bpmn_generator, "MAX_REACH_FLOWS", 1)
        plan = _plan(
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", ""),
                _e("T1", "userTask", "Проверить заявку", ""),
                _e("T2", "userTask", "Согласовать", ""),
                _e("T3", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Готово", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "S1", "T2"),
                   _f("F3", "S1", "T3"), _f("F4", "T1", "E1")],
        )
        result = _generate(monkeypatch, plan)
        to_end = [f["source"] for f in result["structure"]["flows"]
                  if f["target"] == "E1" and f["source"] != "T1"]
        assert len(to_end) == 1
        assert _note(result["notes"], "лимит починки связности")


class TestOutputIsApplierClean:
    """Схема генератора не должна хотеть починки: `validate_and_repair` — тот
    же фильтр, через который в БД попадает пользовательский XML. Если после
    генерации там появляются заметки, два модуля разошлись в понятии «валидно»,
    а повторные потоки или тупики уедут в реестр."""

    PLANS = {
        "роли-пулы с messageFlow": {
            "participants": ["Инициатор", "Руководитель", "Бухгалтерия"],
            "lanes": [],
            "elements": [
                {"id": "S1", "kind": "startEvent", "name": "Потребность",
                 "participant": "Инициатор"},
                {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                 "participant": "Инициатор"},
                {"id": "A2", "kind": "userTask", "name": "Согласовать",
                 "participant": "Руководитель"},
                {"id": "A3", "kind": "userTask", "name": "Выдать деньги",
                 "participant": "Бухгалтерия"},
            ],
            "flows": [
                {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence"},
                {"id": "M1", "source": "A1", "target": "A2", "kind": "message"},
                {"id": "M2", "source": "A2", "target": "A3", "kind": "message"},
            ],
        },
        "битые id, типы и ссылки": {
            "participants": ["Заказ", "Заказ", ""],
            "lanes": [{"id": "1", "name": "Роль", "participant": "нет пула"}],
            "elements": [
                {"id": "T1", "kind": "cart", "name": "", "participant": "Заказ"},
                {"id": "G1", "kind": "exclusiveGateway", "name": "Одна ветка",
                 "participant": "Заказ"},
                {"id": "E1", "kind": "endEvent", "name": "Готово",
                 "participant": "Заказ"},
            ],
            "flows": [
                {"id": "F1", "source": "T1", "target": "призрак", "kind": "sequence"},
                {"id": "F2", "source": "G1", "target": "E1", "kind": "sequence",
                 "condition": "Да"},
                {"id": "F3", "source": "E1", "target": "E1", "kind": "sequence"},
            ],
        },
        # Конструкции, которые генератор научился выдавать: граничный таймер,
        # выход по умолчанию и шлюз, который и расщепляет, и сливает.
        "таймер, default и схождение веток": {
            "participants": ["ВкусВилл"],
            "lanes": [],
            "elements": [
                {"id": "S1", "kind": "startEvent", "name": "Заказ",
                 "participant": "ВкусВилл"},
                {"id": "A1", "kind": "userTask", "name": "Собрать заказ",
                 "participant": "ВкусВилл"},
                {"id": "B1", "kind": "boundaryEvent", "name": "Прошло 4 часа",
                 "participant": "ВкусВилл", "attached_to": "A1",
                 "event_definition": "timer", "timer": "PT4H"},
                {"id": "A2", "kind": "userTask", "name": "Эскалировать",
                 "participant": "ВкусВилл"},
                {"id": "G1", "kind": "exclusiveGateway", "name": "Успели?",
                 "participant": "ВкусВилл"},
                {"id": "A3", "kind": "userTask", "name": "Отгрузить",
                 "participant": "ВкусВилл"},
                {"id": "E1", "kind": "endEvent", "name": "Готово",
                 "participant": "ВкусВилл"},
            ],
            "flows": [
                {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence"},
                {"id": "F2", "source": "A1", "target": "G1", "kind": "sequence"},
                {"id": "F3", "source": "B1", "target": "A2", "kind": "sequence"},
                {"id": "F4", "source": "A2", "target": "G1", "kind": "sequence"},
                {"id": "F5", "source": "G1", "target": "A3", "kind": "sequence",
                 "condition": "Успели"},
                {"id": "F6", "source": "G1", "target": "E1", "kind": "sequence"},
                {"id": "F7", "source": "A3", "target": "E1", "kind": "sequence"},
            ],
        },
    }

    @pytest.mark.parametrize("label", list(PLANS))
    def test_semantic_repair_changes_nothing(self, label):
        repaired, _ = repair_structure(self.PLANS[label])
        xml = BPMNGenerator()._generate_bpmn_xml(repaired)
        _, repair_notes = validate_and_repair(xml)
        assert repair_notes == []

    @pytest.mark.parametrize("label", list(PLANS))
    def test_no_duplicate_flows_and_connectivity_holds(self, label):
        repaired, _ = repair_structure(self.PLANS[label])
        xml = BPMNGenerator()._generate_bpmn_xml(repaired)
        pairs = [(f["kind"], f["source"], f["target"]) for f in repaired["flows"]]
        assert len(pairs) == len(set(pairs))
        details = BPMNScorer().evaluate(xml)["details"]
        assert details["sequence_flows"] and details["no_isolated"]
        assert details["guarded_cycles"]


    def test_message_waiting_orphan_still_gets_local_start(self, monkeypatch):
        """Шаг, которого пул ждёт по сообщению, обязан иметь и локальный вход:
        messageFlow внешний триггер показывает, но процесс в пуле не запускает."""
        plan = _plan(
            participants=["Заказчик", "Исполнитель"],
            lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", "", "Заказчик"),
                _e("A1", "userTask", "Оформить заявку", "", "Заказчик"),
                _e("E1", "endEvent", "Заявка отправлена", "", "Заказчик"),
                _e("S2", "startEvent", "Входящие", "", "Исполнитель"),
                _e("A2", "userTask", "Принять в работу", "", "Исполнитель"),
                _e("A9", "receiveTask", "Ожидание оплаты", "", "Исполнитель"),
                _e("E2", "endEvent", "Готово", "", "Исполнитель"),
            ],
            flows=[_f("F1", "S1", "A1"), _f("F2", "A1", "E1"),
                   _f("M1", "A1", "A9", kind="message"),
                   _f("F3", "S2", "A2"), _f("F4", "A2", "E2")],
        )
        result = _generate(monkeypatch, plan)
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("S2", "A9") in flows
        assert ("A9", "E2") in flows
        assert _note(result["notes"], "дополнительно ожидает сообщение")
        details = BPMNScorer().evaluate(result["bpmn"])["details"]
        assert details["sequence_flows"] and details["no_isolated"]


class TestFailureReporting:
    def test_truncated_answer_is_not_generic_outage(self, monkeypatch):
        result = _generate(monkeypatch,
                           llm_client.LLMTruncatedError("структура неполная"))
        assert result["status"] == "error"
        assert result["step"] == "llm_truncated"
        assert "обрезан по лимиту токенов" in result["error"]
        assert "сократите" in result["error"]
        assert "bpmn" not in result

    def test_transport_failure_stays_llm_step(self, monkeypatch):
        result = _generate(monkeypatch, llm_client.LLMError("503"))
        assert result["step"] == "llm"
        assert "временно недоступен" in result["error"]

    def test_unparsable_answer_keeps_parse_step_and_applies_nothing(self, monkeypatch):
        fake = FakeLLM(monkeypatch, "не json", "и повтор не json")
        result = BPMNGenerator().generate("описание процесса")
        assert result["status"] == "error"
        assert result["step"] == "parse"
        assert "bpmn" not in result and "structure" not in result
        # call_json повторяет разбор ровно один раз.
        assert len(fake.calls) == 2

    def test_plan_without_elements_is_actionable_error(self, monkeypatch):
        result = _generate(monkeypatch, _plan(elements=[], flows=[]))
        assert result["status"] == "error"
        assert result["step"] == "generation"
        assert "ни одного шага" in result["error"]


class TestEmittedXml:
    def test_inventory_loses_nothing_and_refs_match_flows(self, monkeypatch):
        result = _generate(monkeypatch, _plan())
        xml = result["bpmn"]
        root = ET.fromstring(xml)
        inventory = build_inventory(xml)

        assert len(inventory["elements"]) == len(result["structure"]["elements"])
        assert {e["id"] for e in inventory["elements"]} == \
            {e["id"] for e in result["structure"]["elements"]}

        flows = {f["id"]: f for f in inventory["flows"] if f["kind"] == "sequence"}
        incoming = {}
        outgoing = {}
        for flow in flows.values():
            outgoing.setdefault(flow["source"], []).append(flow["id"])
            incoming.setdefault(flow["target"], []).append(flow["id"])
        for elem_id, elem in _flow_nodes(root).items():
            assert sorted(r.text for r in elem.findall(f"{BPMN}incoming")) == \
                sorted(incoming.get(elem_id, [])), elem_id
            assert sorted(r.text for r in elem.findall(f"{BPMN}outgoing")) == \
                sorted(outgoing.get(elem_id, [])), elem_id

    def test_message_flows_only_between_real_pools(self, monkeypatch):
        plan = _plan(
            participants=["ВкусВилл", "Клиент"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                   {"id": "L_c", "name": "Покупка", "participant": "Клиент"}],
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Выставить счёт", "L_m"),
                _e("E1", "endEvent", "Счёт выставлен", "L_m"),
                _e("S2", "startEvent", "Счёт получен", "L_c", participant="Клиент"),
                _e("T2", "userTask", "Оплатить", "L_c", participant="Клиент"),
                _e("E2", "endEvent", "Оплачено", "L_c", participant="Клиент"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "S2", "T2"), _f("F4", "T2", "E2"),
                   _f("F5", "T1", "S2", kind="message")],
        )
        result = _generate(monkeypatch, plan)
        inventory = build_inventory(result["bpmn"])
        assert [p["name"] for p in inventory["participants"]] == ["ВкусВилл", "Клиент"]
        assert [f for f in inventory["flows"] if f["kind"] == "message"] == [
            {"id": "F5", "kind": "message", "source": "T1", "target": "S2"}
        ]
        assert "<bpmn:messageFlow" in result["bpmn"]

    def test_result_contract(self, monkeypatch):
        result = _generate(monkeypatch, _plan())
        assert set(result) == {"status", "bpmn", "structure", "notes", "gaps",
                               "attempts", "time_elapsed"}
        assert result["attempts"] == 1
        assert isinstance(result["gaps"], list)
        assert all(isinstance(g, str) for g in result["gaps"])
        assert isinstance(result["notes"], list)
        assert all(isinstance(n, str) for n in result["notes"])
        assert set(result["structure"]) == {"participants", "lanes", "elements",
                                            "flows"}
        assert all(set(lane) == {"id", "name", "participant"}
                   for lane in result["structure"]["lanes"])


class TestSilentRepairsAreNarrated:
    def test_every_fix_of_model_output_leaves_a_note(self, monkeypatch):
        plan = _plan(
            lanes=[{"id": "L_x", "name": "Менеджер", "participant": "ВкусВилл"},
                   {"id": "1", "name": "Двойник", "participant": "ВкусВилл"},
                   {"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Проверить заявку", "L_m"),
                _e("T2", "cart", "", ""),
                _e("E1", "endEvent", "Готово", "нет такой дорожки"),
            ],
            flows=[{"source": "S1", "target": "T1"},
                   {"source": "T1", "target": "T2"},
                   {"source": "T2", "target": "E1"},
                   {"source": "T1", "target": "призрак"}],
        )
        result = _generate(monkeypatch, plan)
        notes = result["notes"]
        assert _note(notes, "дубликат пропущен")
        assert _note(notes, "приведён к")
        assert _note(notes, "Неизвестный тип")
        assert _note(notes, "без названия")
        assert _note(notes, "не объявлена — создана")
        assert _note(notes, "не найдена") or _note(notes, "не объявлена")
        assert _note(notes, "удалён")

    def test_limits_are_reported(self, monkeypatch):
        elements = [_e(f"E{i}", "task", f"Шаг {i}", "L_m") for i in range(65)]
        elements[0]["kind"] = "startEvent"
        elements[-1]["kind"] = "endEvent"
        result = _generate(monkeypatch, _plan(elements=elements))
        ids = {e["id"] for e in result["structure"]["elements"]}
        # Лимит режет план модели; события, которых в плане нет, добавляются
        # сверху — иначе процесс останется без входа или выхода.
        assert ids & {f"E{i}" for i in range(65)} == \
            {f"E{i}" for i in range(bpmn_generator.MAX_ELEMENTS)}
        assert _note(result["notes"], "Лишние элементы отброшены")

    def test_text_limits_are_reported(self, monkeypatch):
        long_name = "О" * 200
        result = _generate(monkeypatch, _plan(
            participants=[long_name],
            lanes=[{"id": "L1", "name": long_name, "participant": long_name}],
            elements=[
                _e("S1", "startEvent", "Заявка", "L1", long_name),
                _e("T1", "userTask", "Проверить", "L1", long_name),
                _e("E1", "endEvent", "Готово", "L1", long_name),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1")],
        ))
        assert _note(result["notes"], "укорочен")
        assert len(result["structure"]["participants"][0]["name"]) == \
            bpmn_generator.NAME_LIMIT


class TestVacantPools:
    """Пул «только Старт и Завершение» — артефакт модели, а не участник:
    дорисовать ему шаги нельзя, значит пустой процесс в схему не попадает."""

    @staticmethod
    def _vacant():
        return _plan(
            participants=["ВкусВилл", "Кладовщик"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заказ", "L_m"),
                _e("T1", "userTask", "Проверить остатки", "L_m"),
                _e("E1", "endEvent", "Осмотрен", "L_m"),
                _e("S2", "startEvent", "Старт", "", "Кладовщик"),
                _e("E2", "endEvent", "Завершение", "", "Кладовщик"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "S2", "E2")],
        )

    def test_pool_without_steps_removed_with_its_events(self, monkeypatch):
        result = _generate(monkeypatch, self._vacant())
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]
        ids = {e["id"] for e in result["structure"]["elements"]}
        assert not {"S2", "E2"} & ids
        assert _note(result["notes"], "Пул «Кладовщик» удалён")

    def test_flows_of_removed_pool_do_not_survive(self, monkeypatch):
        result = _generate(monkeypatch, self._vacant())
        pairs = {(f["source"], f["target"]) for f in result["structure"]["flows"]}
        assert ("S2", "E2") not in pairs
        assert "S2" not in {f["source"] for f in result["structure"]["flows"]}

    def test_only_pool_survives_even_without_steps(self, monkeypatch):
        result = _generate(monkeypatch, _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[_e("S1", "startEvent", "Сигнал", ""),
                      _e("E1", "endEvent", "Готово", "")],
            flows=[_f("F1", "S1", "E1")]))
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]


class TestRolePoolBecomesLane:
    def test_pool_named_like_a_lane_of_another_pool_is_merged(self, monkeypatch):
        """Модель объявила «Кладовщика» и пулом, и дорожкой — это дорожка."""
        result = _generate(monkeypatch, _plan(
            participants=["ВкусВилл", "Кладовщик"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                   {"id": "L_k", "name": "Кладовщик", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заказ", "L_m"),
                _e("T1", "userTask", "Проверить остатки", "L_m"),
                _e("T2", "userTask", "Собрать заказ", "L_k", "Кладовщик"),
                _e("E1", "endEvent", "Собран", "L_m"),
            ],
            flows=[_f("F1", "S1", "T1"),
                   _f("M1", "T1", "T2", kind="message"),
                   _f("F2", "T2", "E1")],
        ))
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]
        assert _note(result["notes"], "слит в «ВкусВилл» дорожкой «Кладовщик»")
        by_pair = {(f["source"], f["target"]): f["kind"]
                   for f in result["structure"]["flows"]}
        # Межпуловое сообщение внутри одного пула — передача работы между
        # дорожками, то есть обычный sequence-поток.
        assert by_pair[("T1", "T2")] == "sequence"
        moved = next(e for e in result["structure"]["elements"]
                     if e["id"] == "T2")
        assert moved["lane"] == "L_k"


class TestEventLogic:
    """Определение события и хозяин граничного — то, чего в генерации не было:
    без этого «промежуточное событие» остаётся пустым кружком."""

    @staticmethod
    def _timer_on_task(**override):
        element = _e("B1", "boundaryEvent", "Прошло 4 часа", "",
                     attached_to="T1", event_definition="timer", timer="PT4H")
        element.update(override)
        return _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T1", "userTask", "Собрать заказ", ""),
                element,
                _e("T2", "userTask", "Эскалация руководителю", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "B1", "T2"), _f("F4", "T2", "E1")],
        )

    def test_boundary_timer_emits_definition_and_host(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task())
        xml = result["bpmn"]
        assert 'attachedToRef="T1"' in xml
        assert "bpmn:timerEventDefinition" in xml
        assert ">PT4H<" in xml
        assert result["gaps"] == []

    def test_boundary_event_is_not_wired_to_the_start(self, monkeypatch):
        """Граничное событие запускает хозяин: поток от старта к нему —
        второй, выдуманный триггер."""
        result = _generate(monkeypatch, self._timer_on_task())
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("S1", "B1") not in flows

    def test_timer_order_is_documentation_definition_refs(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task(
            documentation="Контроль SLA сборки"))
        event = next(e for e in ET.fromstring(result["bpmn"])
                     .iter(f"{BPMN}boundaryEvent"))
        order = [c.tag.replace(BPMN, "") for c in event]
        assert order == ["documentation", "timerEventDefinition", "outgoing"]

    def test_unparsable_timer_falls_back_to_default_with_note(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task(timer="4 часа"))
        assert _note(result["notes"], "не ISO-8601")
        assert f">{bpmn_generator.DEFAULT_TIMER_DURATION}<" in result["bpmn"]

    def test_unknown_definition_is_dropped_and_reported(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task(
            event_definition="push"))
        assert _note(result["notes"], "Неизвестное определение")
        assert "timerEventDefinition" not in result["bpmn"]

    def test_boundary_without_host_becomes_intermediate(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task(attached_to="ghost"))
        kinds = _kinds(result["structure"])
        assert kinds["B1"] == "intermediateCatchEvent"
        assert _note(result["notes"], "переведено в промежуточное")
        assert "attachedToRef" not in result["bpmn"]

    def test_event_without_definition_stays_a_reported_gap(self, monkeypatch):
        result = _generate(monkeypatch, self._timer_on_task(
            kind="intermediateCatchEvent", event_definition=None, timer=None,
            attached_to=None))
        assert _note(result["notes"], "нет определения")
        assert any("B1" in gap for gap in result["gaps"])

    def test_boundary_event_with_blank_host_does_not_crash(self):
        """`attached_to: "   "` — модель отдаёт и такое: пустой id после
        санитизации не должен ронять генерацию на срезе индекса."""
        repaired, notes = repair_structure(_plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Старт", ""),
                _e("A1", "userTask", "Собрать", ""),
                _e("B1", "boundaryEvent", "Просрочка", "", attached_to="   ",
                   event_definition="timer"),
                _e("E1", "endEvent", "Готово", ""),
            ],
            flows=[_f("F1", "S1", "A1"), _f("F2", "A1", "E1"),
                   _f("F3", "B1", "E1")],
        ))
        assert _kinds(repaired)["B1"] == "intermediateCatchEvent"
        assert "attached_to" not in next(e for e in repaired["elements"]
                                         if e["id"] == "B1")
        assert _note(notes, "не указано, к какой задаче")


class TestGatewayConditionsAndMerge:
    @staticmethod
    def _branching(gateway="exclusiveGateway", first="Да", second="Нет"):
        return _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("G1", gateway, "Хватает товара?", ""),
                _e("T2", "userTask", "Собрать", ""),
                _e("T3", "userTask", "Заказать остаток", ""),
                _e("T4", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "G1"),
                   _f("F2", "G1", "T2", condition=first),
                   _f("F6", "G1", "T3", condition=second),
                   _f("F3", "T2", "T4"), _f("F4", "T3", "T4"),
                   _f("F5", "T4", "E1")],
        )

    def test_single_unconditioned_branch_becomes_default(self, monkeypatch):
        result = _generate(monkeypatch, self._branching(second=""))
        gateway = next(e for e in ET.fromstring(result["bpmn"])
                       .iter(f"{BPMN}exclusiveGateway"))
        assert gateway.get("default")
        assert _note(result["notes"], "объявлена выходом по умолчанию")

    def test_two_unconditioned_branches_are_not_resolved_by_force(self, monkeypatch):
        result = _generate(monkeypatch, self._branching(first="", second=""))
        assert _note(result["notes"], "не выбран")
        assert any("G1" in gap for gap in result["gaps"])

    def test_default_flag_from_model_reaches_the_gateway(self, monkeypatch):
        plan = _plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("G1", "exclusiveGateway", "Хватает товара?", ""),
                _e("T2", "userTask", "Собрать", ""),
                _e("T3", "userTask", "Заказать остаток", ""),
                _e("T4", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "G1"), _f("F2", "G1", "T2", condition="Да"),
                   _f("F6", "G1", "T3", condition="", default=True),
                   _f("F3", "T2", "T4"), _f("F4", "T3", "T4"),
                   _f("F5", "T4", "E1")],
        )
        result = _generate(monkeypatch, _fence(plan))
        gateway = next(e for e in ET.fromstring(result["bpmn"])
                       .iter(f"{BPMN}exclusiveGateway"))
        assert gateway.get("default") == next(
            f["id"] for f in result["structure"]["flows"]
            if f["source"] == "G1" and f.get("default"))

    def test_merge_gateway_inserted_before_converging_task(self, monkeypatch):
        result = _generate(monkeypatch, self._branching())
        merges = [e for e in result["structure"]["elements"]
                  if e["name"] == "Схождение веток"]
        assert [m["kind"] for m in merges] == ["exclusiveGateway"]
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("T2", "T4") not in flows and ("T3", "T4") not in flows
        assert [flows.count((m["id"], "T4")) for m in merges] == [1]
        assert _note(result["notes"], "вставлен шлюз схождения")

    def test_inserted_merge_gateway_is_not_demoted(self, monkeypatch):
        """У сходящегося шлюза один исходящий — это норма, а не повод понижать."""
        result = _generate(monkeypatch, self._branching())
        merge = next(e for e in result["structure"]["elements"]
                     if e["name"] == "Схождение веток")
        assert _kinds(result["structure"])[merge["id"]] == "exclusiveGateway"
        assert not _note(result["notes"], "понижен до задачи")

    def test_parallel_split_gets_parallel_merge(self, monkeypatch):
        result = _generate(monkeypatch, self._branching(
            gateway="parallelGateway", first="", second=""))
        merges = [e for e in result["structure"]["elements"]
                  if e["name"] == "Схождение веток"]
        assert [m["kind"] for m in merges] == ["parallelGateway"]

    def test_branches_without_common_gateway_are_left_alone(self, monkeypatch):
        plan = _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T2", "userTask", "Собрать", ""),
                _e("T3", "userTask", "Заказать остаток", ""),
                _e("T4", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "T2"), _f("F2", "S1", "T3"),
                   _f("F3", "T2", "T4"), _f("F4", "T3", "T4"),
                   _f("F5", "T4", "E1")],
        )
        result = _generate(monkeypatch, plan)
        assert not [e for e in result["structure"]["elements"]
                    if e["name"] == "Схождение веток"]
        assert _note(result["notes"], "нет общего шлюза-расщепителя")

    def test_end_event_with_two_branches_needs_no_merge(self, monkeypatch):
        result = _generate(monkeypatch, _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("G1", "exclusiveGateway", "Хватает?", ""),
                _e("T2", "userTask", "Собрать", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "G1"), _f("F2", "G1", "T2", condition="Да"),
                   _f("F3", "G1", "E1", condition="Нет"), _f("F4", "T2", "E1")],
        ))
        assert not [e for e in result["structure"]["elements"]
                    if e["name"] == "Схождение веток"]


class TestPlanGapsAndRetry:
    """Нарушения, которые нельзя починить без выдумывания содержания, — повод
    один раз переспросить модель, а не рисовать схему по своему усмотрению."""

    def test_gaps_list_names_every_unfixable_defect(self):
        gaps = bpmn_generator.plan_gaps({
            "participants": ["ВкусВилл", "Кладовщик"],
            "lanes": [{"id": "L1", "name": "Кладовщик", "participant": "ВкусВилл"}],
            "elements": [
                {"id": "S1", "kind": "startEvent", "name": "а",
                 "participant": "ВкусВилл"},
                {"id": "A1", "kind": "userTask", "name": "Собрать",
                 "participant": "ВкусВилл"},
                {"id": "B1", "kind": "boundaryEvent", "name": "Срок",
                 "participant": "ВкусВилл"},
                {"id": "G1", "kind": "exclusiveGateway", "name": "Ветка",
                 "participant": "ВкусВилл"},
            ],
            "flows": [{"id": "F1", "source": "G1", "target": "A1",
                       "kind": "sequence", "condition": ""},
                      {"id": "F2", "source": "G1", "target": "S1",
                       "kind": "sequence", "condition": ""}],
        })
        assert any("Кладовщик" in g and "без единого шага" in g for g in gaps)
        assert any("объявлен пулом и одновременно дорожкой" in g for g in gaps)
        assert any("B1" in g and "нет определения" in g for g in gaps)
        assert any("B1" in g and "attached_to" in g for g in gaps)
        assert any("G1" in g and "2 веток без условия" in g for g in gaps)

    def test_gaps_survive_junk_and_non_objects(self):
        assert bpmn_generator.plan_gaps("не объект") == ["план не является JSON-объектом"]
        assert isinstance(bpmn_generator.plan_gaps({"elements": [1, None, "x"]}),
                          list)

    def test_clean_plan_is_not_asked_twice(self, monkeypatch):
        fake = FakeLLM(monkeypatch, _plan())
        result = BPMNGenerator().generate("описание")
        assert result["attempts"] == 1 and len(fake.calls) == 1

    def test_retry_carries_violations_and_better_plan_wins(self, monkeypatch):
        bad = TestVacantPools._vacant()
        fake = FakeLLM(monkeypatch, bad, _plan())
        result = BPMNGenerator().generate("описание процесса")
        assert result["attempts"] == 2 and len(fake.calls) == 2
        # Второй вызов идёт с тем же системным промптом: правила моделирования
        # не должны разъезжаться между попытками.
        assert fake.calls[1][0]["content"] == fake.calls[0][0]["content"]
        user_prompt = fake.calls[1][1]["content"]
        assert "Нарушения в текущем плане" in user_prompt
        assert "без единого шага" in user_prompt
        assert _note(result["notes"], "Повторный запрос модели: нарушений было")
        assert result["gaps"] == []
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]

    def test_retry_that_did_not_improve_keeps_first_plan(self, monkeypatch):
        bad = TestVacantPools._vacant()
        FakeLLM(monkeypatch, bad, bad)
        result = BPMNGenerator().generate("описание")
        assert _note(result["notes"], "не улучшил план")
        assert not _note(result["notes"], "план пересобран")

    def test_retry_failure_degrades_to_first_plan(self, monkeypatch):
        FakeLLM(monkeypatch, TestVacantPools._vacant(),
                llm_client.LLMError("503"))
        result = BPMNGenerator().generate("описание")
        assert result["status"] == "success"
        assert _note(result["notes"], "Повторный запрос модели не выполнен")

    def test_oversized_plan_is_not_sent_again(self, monkeypatch):
        monkeypatch.setattr(bpmn_generator, "MAX_RETRY_PLAN_CHARS", 10)
        fake = FakeLLM(monkeypatch, TestVacantPools._vacant(), _plan())
        result = BPMNGenerator().generate("описание")
        assert len(fake.calls) == 1
        assert _note(result["notes"], "Повторный запрос модели не выполнен")


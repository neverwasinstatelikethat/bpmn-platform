"""Генератор BPMN: от JSON-плана модели до импортируемой схемы.

Транспорт LLM подменён на уровне ``_complete`` (как в
tests/test_improve_orchestrator.py), поэтому проверяется настоящий разбор
ответа, починка структуры и emission XML — без сети и без ключа.

Главное, что здесь закреплено: роли сотрудников — дорожки одного пула, а не
отдельные пулы, и ни одна правка недоверенного плана не остаётся без note.
"""
import copy
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

    @property
    def plan_prompt(self):
        """Системный промпт вызова, который просит ПЛАН (элементы и потоки).

        Первым вызовом теперь идёт состав процесса, и правила маршрута надо
        искать не в нём — иначе проверка ловила бы любой промпт подряд.
        """
        for call in self.calls:
            if '"elements"' in call[0]["content"]:
                return call[0]["content"]
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
    return BPMNGenerator().generate("ВкусВилл: клиент оставляет заявку, "
                                    "менеджер её согласует, бухгалтерия "
                                    "выставляет счёт")


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


def _role_candidates(question):
    """Строка кандидатов на роль из вопроса о принадлежности: проверять надо её,
    а не весь вопрос — список всех пулов плана назван там тоже."""
    return question.split("роль, не участник):\n", 1)[1].split("\n", 1)[0]


def _merge_gateways(result):
    """Вставленные шлюзы схождения — по id, который даёт починка: имя теперь
    наследует вопрос развилки и для проверки наличия не годится."""
    return [e for e in result["structure"]["elements"]
            if e["id"].startswith("Gateway_merge_")]


def _illegal_edges(structure):
    """Sequence-потоки с запрещённым концом: вход в старт и в граничное событие,
    выход из конечного. MessageFlow не считается — он чужой старт и обязан
    запускать."""
    kinds = {e["id"]: e["kind"] for e in structure["elements"]}
    return [(f["id"], f["source"], f["target"]) for f in structure["flows"]
            if f["kind"] == "sequence"
            and (kinds[f["target"]] in ("startEvent", "boundaryEvent")
                 or kinds[f["source"]] == "endEvent")]


def _illegal_xml_edges(xml):
    """То же по уже выпущенному XML: проверка держит инвариант на emission,
    а не только на словаре структуры."""
    tags = {elem.get("id"): elem.tag.replace(BPMN, "")
            for elem in ET.fromstring(xml).iter()
            if isinstance(elem.tag, str) and elem.get("id")}
    return [(flow.get("id"), flow.get("sourceRef"), flow.get("targetRef"))
            for flow in ET.fromstring(xml).iter(f"{BPMN}sequenceFlow")
            if tags.get(flow.get("targetRef")) in ("startEvent", "boundaryEvent")
            or tags.get(flow.get("sourceRef")) == "endEvent"]


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
        prompt = fake.plan_prompt
        assert '"lanes"' in prompt and '"lane"' in prompt
        assert "ДОРОЖКИ" in prompt
        assert "РАЗНЫХ пулов" in prompt
        # Живые прогоны разводили по пулам кладовщика, водителя и экспедитора, а
        # организации в схеме не оставалось: должность обязана названа запрещена.
        assert "пулом быть не может" in prompt
        assert "Пулов в схеме не больше, чем организаций" in prompt
        # Родовой пул «система» съедал названную в тексте WMS.
        assert "а не родовое слово" in prompt

    def test_prompt_keeps_every_action_of_the_text_a_step(self, monkeypatch):
        """Склеенные действия — не «короче», а другой процесс: одно и то же
        событие живых прогонов — `min_steps` не добирался до ожиданий сценария,
        потому что модель сворачивала «регистрирует алерт и создаёт инцидент»
        в один шаг, а последнее действие фразы выбрасывала."""
        fake = FakeLLM(monkeypatch, _plan())
        BPMNGenerator().generate("описание")
        prompt = fake.plan_prompt
        assert "одно законченное действие одного исполнителя" in prompt
        assert "последнее действие" in prompt
        # склейка прячется в имени: «Проверка упаковок» — это и проверка, и
        # фиксация, и отчёт; имя-глагол такую склейку выдаёт
        assert "отглагольное" in prompt

    def test_prompt_shows_one_shot_of_roles_as_lanes(self, monkeypatch):
        """Правило без примера модель выполняет неустойчиво: в живом прогоне
        роли разъезжались по пулам даже с правилами в промпте."""
        fake = FakeLLM(monkeypatch, _plan())
        BPMNGenerator().generate("описание")
        prompt = fake.plan_prompt
        assert 'Пример' in prompt
        assert '"participants": ["Цех фасовки", "Сервисная служба"]' in prompt
        assert "роли одной организации" in prompt
        # В примере развилка имеет пару: G1 расщепляет, G2 сливает.
        assert '"id": "G2", "kind": "exclusiveGateway"' in prompt
        # Зеркало правила достижимости: у шага должен быть и выход.
        assert "кроме endEvent" in prompt


def test_counterparty_named_inside_a_phrase_is_a_gap():
    """План-гейт видел только аббревиатуры: «Перевозчик», названный в середине
    фразы, нарушением не считался и в переспрос не попадал — «план-гейт про
    него молчал» стал вторым по величине источником провалов
    `expected_participants` в прогоне #40 (4 из 14)."""
    gaps = bpmn_generator.plan_gaps(
        _plan_dict(),
        "ВкусВилл собирает заказ, Перевозчик вывозит его на адрес получателя.")
    assert any("«Перевозчик»" in g and "заведи" in g for g in gaps)


def test_start_of_a_phrase_is_not_taken_for_a_participant():
    """Заглавная буква в начале фразы — не признак имени: так каждый «Далее»
    и «Если» стал бы участником, а план-гейт честно просил бы завести под него
    пул. Ограничение осознанное: ловим только то написание, которое русская
    орфография оставляем именем собственным."""
    gaps = bpmn_generator.plan_gaps(
        _plan_dict(),
        "Перевозчик вывозит заказ. Далее получатель ставит подпись.")
    assert not any("«Перевозчик»" in g for g in gaps)
    assert not any("«Далее»" in g for g in gaps)


def test_generate_asks_about_a_counterparty_the_plan_never_named(monkeypatch):
    """Сквозная проверка: названный в описании контрагент, о котором молчал
    план-гейт, уходит в переспрос и возвращается на схему."""
    first = _fence(_plan_dict())
    with_carrier = _plan_dict()
    with_carrier["participants"] = ["ВкусВилл", "Перевозчик"]
    with_carrier["elements"] = list(with_carrier["elements"]) + [
        _e("P1", "userTask", "Вывезти груз", "", participant="Перевозчик")]
    with_carrier["flows"] = list(with_carrier["flows"]) + [
        _f("MF1", "T5", "P1", kind="message")]
    fake = FakeLLM(monkeypatch, first, _fence(with_carrier),
                   '{"moves": [], "roles": [], "missing": []}')
    result = BPMNGenerator().generate(
        "ВкусВилл собирает заказ, Перевозчик вывозит его на адрес.")

    asked = fake.calls[1][1]["content"]
    assert "Перевозчик" in asked
    assert "Перевозчик" in [p["name"] for p in result["structure"]["participants"]]


def test_camel_case_brand_is_not_cut_into_a_participant():
    """«ВкусВилл» — одно имя, а не «Вкус» + «Вилл»: без границы после заглавной
    части план-гейт требовал бы завести пул «Вкус» в каждом описании с брендом
    (живая проверка на текстах харнесса дала три таких ложных имени)."""
    gaps = bpmn_generator.plan_gaps(
        _plan_dict(),
        "ВкусВилл собирает заказ, перевозчик вывозит его на адрес.")
    assert not any("«Вкус»" in g or "«Вилл»" in g for g in gaps)


class TestTwoStageGeneration:
    """Состав процесса утверждается отдельным вызовом, маршрут пишется по нему.

    Живые прогоны #40–#42: один выдох «реши, кто участник, и нарисуй 20
    элементов» стоил пула на каждую должность, а каждый такой пул — отдельный
    процесс со своим стартом и финишем.
    """

    TEXT = ("Кладовщик склада собирает груз, экспедитор перевозчика выдаёт его "
            "под подпись.")

    ROSTER = ('{"organizations": ["Склад"], "counterparties": ["Перевозчик"], '
              '"roles": [{"name": "Кладовщик", "host": "Склад"}, '
              '{"name": "Экспедитор", "host": "Перевозчик"}]}')

    def _flow(self) -> str:
        return _fence({
            "participants": ["Склад", "Перевозчик"],
            "lanes": [{"id": "L1", "name": "Кладовщик", "participant": "Склад"},
                      {"id": "L2", "name": "Экспедитор",
                       "participant": "Перевозчик"}],
            "elements": [
                {"id": "S1", "kind": "startEvent", "name": "Заявка собрана",
                 "participant": "Склад", "lane": "L1"},
                {"id": "T1", "kind": "userTask", "name": "Собрать груз",
                 "participant": "Склад", "lane": "L1"},
                {"id": "E1", "kind": "endEvent", "name": "Груз на складе",
                 "participant": "Склад", "lane": "L1"},
                {"id": "S2", "kind": "startEvent", "name": "Груз передан",
                 "participant": "Перевозчик", "lane": "L2"},
                {"id": "T2", "kind": "userTask", "name": "Выдать груз",
                 "participant": "Перевозчик", "lane": "L2"},
                {"id": "E2", "kind": "endEvent", "name": "Подпись получена",
                 "participant": "Перевозчик", "lane": "L2"},
            ],
            "flows": [{"id": "F1", "source": "S1", "target": "T1"},
                      {"id": "F2", "source": "T1", "target": "E1"},
                      {"id": "M1", "source": "T1", "target": "T2",
                       "kind": "message"},
                      {"id": "F3", "source": "S2", "target": "T2"},
                      {"id": "F4", "source": "T2", "target": "E2"}]})

    def test_retry_keeps_the_approved_composition_and_its_own_fix(self,
                                                                 monkeypatch):
        """Переспрос чинит маршрут, а состав остаётся утверждённым.

        Прогон #43: чтобы добавить таймер, модель заодно перекрасила все пулы в
        «роли без хозяина» — 6 нарушений rank 0 вместо 0, гейт справедливо
        отказался, и таймер уехал в корзину вместе с правкой. Каркас тот же,
        что и для первого ответа, поэтому от повтора берётся маршрут.
        """
        text = ("Авария на линии. Инженер дежурства осматривает узел; если узел "
                "не починить в течение 15 минут, дежурство вызывает подрядчика.")
        roster = ('{"organizations": ["Дежурство"], "systems": [], '
                  '"counterparties": [], "roles": [{"name": "Инженер", '
                  '"host": "Дежурство"}]}')
        base = {"participants": ["Дежурство"],
                "lanes": [{"id": "L1", "name": "Инженер",
                           "participant": "Дежурство"}],
                "elements": [
                    {"id": "S1", "kind": "startEvent", "name": "Авария",
                     "participant": "Дежурство", "lane": "L1"},
                    {"id": "A1", "kind": "userTask", "name": "Осмотреть узел",
                     "participant": "Дежурство", "lane": "L1"},
                    {"id": "E1", "kind": "endEvent", "name": "Линия в работе",
                     "participant": "Дежурство", "lane": "L1"}],
                "flows": [{"id": "F1", "source": "S1", "target": "A1"},
                          {"id": "F2", "source": "A1", "target": "E1"}]}
        # Тот же маршрут + таймер, но состав модель переписала в «роли без
        # хозяина»: без закрепления каркаса это отказ по профилю нарушений.
        retry = json.loads(json.dumps(base))
        retry["participants"] = [{"name": "Дежурство", "external": False},
                                 {"name": "Инженер", "external": False}]
        retry["elements"].append(
            {"id": "B1", "kind": "boundaryEvent", "name": "Прошло 15 минут",
             "participant": "Дежурство", "lane": "L1", "attached_to": "A1",
             "event_definition": "timer", "timer": "PT15M"})
        FakeLLM(monkeypatch, roster, _fence(base), _fence(retry),
                _fence(retry), _fence(retry))
        result = BPMNGenerator().generate(text)

        kinds = [(e["kind"], e.get("event_definition"))
                 for e in result["structure"]["elements"]]
        assert ("boundaryEvent", "timer") in kinds, result["notes"]
        pools = [p["name"] if isinstance(p, dict) else p
                 for p in result["structure"]["participants"]]
        assert pools == ["Дежурство"]
        reask = next(t for t in result["trace"] if t["node"] == "переспрос плана")
        assert reask["kept"] == "переспрос", reask
        assert reask["patch"] == "план целиком вместо заплатки", reask

    def test_retry_may_not_add_a_pool_the_composition_does_not_have(self):
        """Ответ планом целиком на переспросе не открывает состав: пулы там уже
        выбраны дважды, и единственное право повтора — маршрут. Прогон #45
        принёс `purchase_approval` 7 пулов при 2 оправданных текстом именно
        через это исключение."""
        skeleton = {"participants": ["ВкусВилл"],
                    "lanes": [{"id": "L1", "name": "Менеджер",
                               "participant": "ВкусВилл"}],
                    "actors": ["ВкусВилл", "Менеджер"]}
        plan = {"participants": ["ВкусВилл", "Поставщик"], "lanes": [],
                "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                              "participant": "ВкусВилл", "lane": "L1"}],
                "flows": []}
        merged_first, _ = bpmn_generator._merge_skeleton(
            skeleton, plan, "ВкусВилл заводит заявку, поставщик подтверждает.")
        merged_retry, notes = bpmn_generator._merge_skeleton(
            skeleton, plan, "ВкусВилл заводит заявку, поставщик подтверждает.",
            allow_extra_pools=False)
        assert "Поставщик" in [bpmn_generator._pool_name(p) for p in merged_first["participants"]]
        assert [bpmn_generator._pool_name(p) for p in merged_retry["participants"]] == ["ВкусВилл"]
        assert any("переспрос чинит маршрут" in n for n in notes)

    def test_host_named_by_a_dependant_word_of_the_name_becomes_a_lane(self):
        """«Руководитель дежурства» при пуле «Дежурство» — должность, а не
        второй участник: хозяина читаем из зависимого слова самого имени,
        которое модель назвала."""
        skeleton, notes = bpmn_generator.parse_roster(
            {"organizations": ["Дежурство", "Сервис-деск",
                               "Руководитель дежурства",
                               "Инженер сервиса-деска"],
             "systems": ["Система мониторинга"], "counterparties": [],
             "roles": []},
            "Дежурство принимает сигнал от системы мониторинга, руководитель "
            "дежурства эскалирует, инженер сервиса-деска информирует "
            "подразделения.")
        assert [bpmn_generator._pool_name(p) for p in skeleton["participants"]] == [
            "Дежурство", "Сервис-деск", "Система мониторинга"]
        assert [(lane["name"], lane["participant"])
                for lane in skeleton["lanes"]] == [
                    ("Руководитель дежурства", "Дежурство"),
                    ("Инженер сервиса-деска", "Сервис-деск")]
        assert any("подразделение или должность" in n for n in notes)

    def test_truncated_duplicate_is_not_mistaken_for_a_host(self):
        """«Бюро» и «Бюро кредитных историй» — не хозяин и роль, а усечённый
        дубль: по головному слову правило хозяина не срабатывает."""
        skeleton, _notes = bpmn_generator.parse_roster(
            {"organizations": ["Банк", "Бюро", "Бюро кредитных историй"],
             "systems": [], "counterparties": ["Клиент"], "roles": []},
            "Банк, клиент и бюро кредитных историй участвуют в выдаче; бюро "
            "шлёт ответ банку.")
        pools = [bpmn_generator._pool_name(p) for p in skeleton["participants"]]
        assert "Бюро" in pools and "Бюро кредитных историй" in pools
        assert [(lane["name"], lane["participant"])
                for lane in skeleton["lanes"]] == []

    def test_roles_become_lanes_before_the_route_is_written(self, monkeypatch):
        fake = FakeLLM(monkeypatch, self.ROSTER, self._flow())
        result = BPMNGenerator().generate(self.TEXT)

        names = [p["name"] if isinstance(p, dict) else p
                 for p in result["structure"]["participants"]]
        assert "Кладовщик" not in names and "Экспедитор" not in names
        assert sorted(names) == sorted(["Склад", "Перевозчик"])
        assert [(lane["name"], lane["participant"])
                for lane in result["structure"]["lanes"]] == [
                    ("Кладовщик", "Склад"), ("Экспедитор", "Перевозчик")]
        # Маршрут просится у того же промпта с закреплением состава, и состав
        # ему передан текстом: своих пулов он не заводит.
        assert fake.calls[1][0]["content"] == bpmn_generator._FLOW_SYSTEM_PROMPT
        assert "Состав процесса (утверждён" in fake.calls[1][1]["content"]
        assert "«Склад»" in fake.calls[1][1]["content"]

    def test_the_flow_prompt_forbids_an_idle_pool(self):
        """Утверждённый состав обязывает маршрут наполнить каждый пул: без
        этого правила модель записывает действие контрагента в пул ждущей
        организации («ждёт подтверждения от перевозчика» → шаг диспетчера),
        пул контрагента остаётся пустым, и починка удаляет названного в
        описании участника со схемы."""
        rule = " ".join(bpmn_generator._FLOW_SYSTEM_PROMPT.split())
        assert "Ни один утверждённый пул не остаётся без своего шага" in rule
        assert "ждать — не значит делать" in rule

    def test_parse_roster_leaves_a_hostless_role_an_open_violation(self):
        """Хозяина роли угадываем не: роль без организации остаётся пулом с
        `external: false`, и это нарушение rank 0, а не тихая дорожка-сирота."""
        skeleton, _notes = bpmn_generator.parse_roster(
            {"organizations": ["Склад"],
             "roles": [{"name": "Кладовщик", "host": "Склад"},
                       {"name": "Дежурный инженер", "host": ""}]},
            "Кладовщик собирает груз на складе, дежурный инженер принимает "
            "сигнал.")
        assert skeleton["participants"] == ["Склад",
                                          {"name": "Дежурный инженер",
                                           "external": False}]
        assert [(l["name"], l["participant"]) for l in skeleton["lanes"]] == [
            ("Кладовщик", "Склад")]
        assert any("помечен ролью" in g
                   for g in bpmn_generator.plan_gaps(skeleton))

    def test_parse_roster_names_a_host_by_the_description(self):
        """Ярлык вместо имени хозяина не стоит шести ролей.

        Модель назвала организацию составным ярлыком («Организация-склад»),
        которого в описании нет, — проверка «каждая часть имени есть в тексте»
        отбрасывала имя из-за родового слова, хозяин терялся, и роль без хозяина
        становилась пулом: прогон #43 дал 9 пулов, все потоки между ними стали
        сообщениями, а оба шлюза починка понизила до задач.
        """
        skeleton, notes = bpmn_generator.parse_roster(
            {"organizations": ["Организация-склад"], "systems": ["WMS"],
             "counterparties": ["Перевозчик"],
             "roles": [{"name": "Кладовщик", "host": "Организация-склад"},
                       {"name": "Оператор склада", "host": "Организация-склад"},
                       {"name": "Водитель", "host": "Перевозчик"}]},
            "Оператор склада заводит заявку на складе в WMS, кладовщик "
            "собирает груз, перевозчик везёт, водитель расписывается.")

        assert [p if isinstance(p, str) else p["name"]
                for p in skeleton["participants"]] == ["Склад", "WMS",
                                                       "Перевозчик"]
        assert all(isinstance(p, str) for p in skeleton["participants"])
        assert [(lane["name"], lane["participant"])
                for lane in skeleton["lanes"]] == [
                    ("Кладовщик", "Склад"), ("Оператор склада", "Склад"),
                    ("Водитель", "Перевозчик")]
        assert not any("помечен ролью" in g
                       for g in bpmn_generator.plan_gaps(skeleton))
        assert any("назван тем словом" in n for n in notes)

    def test_parse_roster_generic_host_is_still_not_a_pool(self):
        """Родовое слово остаётся отказом и после того, как имя ярлыка
        уточняется по описанию: «Организация» в тексте встречается чаще, чем
        настоящее имя, и три роли уже сливались в пул с таким названием."""
        skeleton, _notes = bpmn_generator.parse_roster(
            {"organizations": ["Организация"],
             "roles": [{"name": "Кладовщик", "host": "Организация"}]},
            "Организация держит дежурство, кладовщик собирает груз.")
        assert skeleton["participants"] == [{"name": "Кладовщик",
                                             "external": False}]
        assert skeleton["lanes"] == []

    def test_parse_roster_refuses_names_that_the_text_does_not_carry(self):
        skeleton, notes = bpmn_generator.parse_roster(
            {"organizations": ["Склад", "Фаб грез"],
             "roles": [{"name": "Гувернёр", "host": "Фаб грез"},
                       {"name": "Система", "host": "Склад"}]},
            "Склад отгружает товар.")
        assert [p if isinstance(p, str) else p["name"]
                for p in skeleton["participants"]] == ["Склад"]
        assert skeleton["lanes"] == []
        assert any("Фаб грез" in n for n in notes)

    def test_route_may_not_add_a_participant_absent_from_the_description(self):
        skeleton = {"participants": ["Склад"],
                    "lanes": [{"id": "L1", "name": "Кладовщик",
                               "participant": "Склад"}],
                    "actors": ["Склад", "Кладовщик"]}
        plan = {"participants": ["Склад", "Марсиане"], "lanes": [],
                "elements": [], "flows": []}
        merged, notes = bpmn_generator._merge_skeleton(
            skeleton, plan, "Склад отгружает товар.")
        assert [p if isinstance(p, str) else p["name"]
                for p in merged["participants"]] == ["Склад"]
        assert any("«Марсиане» не добавлен" in n for n in notes)

    def test_route_participant_named_in_the_text_survives(self):
        """Состав мог пропустить контрагента, который в тексте назван: выбросить
        его — значит потерять участника, которого проверяет `expected_participants`."""
        skeleton = {"participants": ["Склад"], "lanes": [], "actors": ["Склад"]}
        plan = {"participants": ["Склад", "Перевозчик"], "lanes": [],
                "elements": [], "flows": []}
        merged, _notes = bpmn_generator._merge_skeleton(
            skeleton, plan, "Склад отгружает товар, перевозчик вывозит его.")
        assert [p if isinstance(p, str) else p["name"]
                for p in merged["participants"]] == ["Склад", "Перевозчик"]

    def test_failed_composition_stage_keeps_generation_one_shot(self, monkeypatch):
        """Сбой отдельной стадии не имеет права превращать генерацию в отказ."""
        plan = _plan()
        fake = FakeLLM(monkeypatch, llm_client.LLMError("состава нет"), plan)
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert result["status"] == "success"
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]
        assert len(fake.calls) == 2

    def test_answer_with_elements_is_not_asked_to_route_again(self, monkeypatch):
        """Если на узкий вопрос состава модель ответила планом целиком, второй
        вызов за маршрутом — платить дважды за то, что уже есть."""
        fake = FakeLLM(monkeypatch, _plan())
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert result["status"] == "success" and len(fake.calls) == 1


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

    def test_flow_into_start_event_dropped_orphan_reconnected(self, monkeypatch):
        """Поток в старт недопустим, а не «опасен циклом»: дуга убрана, и сирота
        T8 подключается штатной починкой достижимости от старта своего пула."""
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
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("T8", "S1") not in flows
        assert ("S1", "T8") in flows
        assert _note(result["notes"], "у стартового события входящих потоков")
        assert _note(result["notes"], "подключён от стартового события")
        assert not _illegal_edges(result["structure"])

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

    def test_flow_out_of_end_event_dropped_not_closed_again(self, monkeypatch):
        """Единственный вход задачи был из конечного события: дуга убрана, а
        саму задача закрывает штатная починка — выход к финишу пула и вход от
        старта, но никогда не поток из endEvent."""
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
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("E1", "T8") not in flows
        assert ("T8", "E1") in flows and ("S1", "T8") in flows
        assert _note(result["notes"], "конечное событие завершает маршрут")
        assert not _illegal_edges(result["structure"])

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


class TestIllegalFlowEnds:
    """Концы sequence-потока: в стартовое и граничное события поток не входит,
    из конечного не выходит. Модель путает события с шагами маршрута, и
    переворот такой дуги выдумал бы содержание, которого в описании нет, —
    поэтому она удалена, сказано в notes и показана модели в gaps.
    Потоковое сообщение в чужой старт — наоборот, единственный способ запустить
    пул, и его трогать нельзя."""

    @staticmethod
    def _start_flow():
        return _plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", ""),
                _e("T1", "userTask", "Проверить заявку", ""),
                _e("E1", "endEvent", "Готово", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "T1", "S1")],
        )

    @staticmethod
    def _boundary_flow():
        return _plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T1", "userTask", "Собрать заказ", ""),
                _e("B1", "boundaryEvent", "Прошло 4 часа", "",
                   attached_to="T1", event_definition="timer", timer="PT4H"),
                _e("T2", "userTask", "Эскалировать", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   # Ветка эскалации есть (F4), а вход в граничное событие —
                   # второй, выдуманный триггер.
                   _f("F3", "T1", "B1"), _f("F4", "B1", "T2"),
                   _f("F5", "T2", "E1")],
        )

    @staticmethod
    def _end_flow():
        return _plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заявка", ""),
                _e("T1", "userTask", "Проверить заявку", ""),
                _e("T2", "userTask", "Отгрузить", ""),
                _e("E1", "endEvent", "Готово", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("F3", "E1", "T2"), _f("F4", "T2", "E1")],
        )

    def test_flow_into_start_event_dropped_with_note(self):
        repaired, notes = repair_structure(self._start_flow())
        pairs = {(f["source"], f["target"]) for f in repaired["flows"]}
        assert ("T1", "S1") not in pairs
        assert pairs == {("S1", "T1"), ("T1", "E1")}
        assert _note(notes, "Поток T1 → S1 удалён: у стартового события")
        assert _note(notes, "триггер пула")
        assert not _illegal_edges(repaired)

    def test_flow_without_ids_at_all_is_dropped_not_fatal(self):
        """Живой ответ модели уронил починку: id конца потока после санитайзера
        оказался пуст, а пустой «запасной» id не дал и его. Дуга без концов —
        удаление с пометкой, а не падение генерации."""
        repaired, notes = repair_structure(_plan_dict(
            participants=["ВкусВилл"], lanes=[],
            elements=[_e("S1", "startEvent", "Начало", ""),
                      _e("T1", "userTask", "Шаг", ""),
                      _e("E1", "endEvent", "Конец", "")],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   {"id": "F9", "kind": "sequence", "source": None, "target": ""},
                   {"id": "F10", "kind": "sequence", "source": "??",
                    "target": "T1"}],
        ))
        assert [f["id"] for f in repaired["flows"]] == ["F1", "F2"]
        assert _note(notes, "Поток ? → ? удалён")
        assert _note(notes, "Поток __ → T1 удалён")

    def test_flow_into_boundary_event_dropped_handler_branch_kept(self):
        """Исходящий от граничного события — ветка обработки, она остаётся;
        уходит только входящий, которого в BPMN не бывает."""
        repaired, notes = repair_structure(self._boundary_flow())
        pairs = {(f["source"], f["target"]) for f in repaired["flows"]}
        assert ("T1", "B1") not in pairs
        assert ("B1", "T2") in pairs
        assert _note(notes, "граничное событие запускает его хозяин")
        assert _note(notes, "ветка обработки")
        assert _kinds(repaired)["B1"] == "boundaryEvent"
        assert 'attachedToRef="T1"' in BPMNGenerator()._generate_bpmn_xml(repaired)

    def test_flow_out_of_end_event_dropped(self):
        repaired, notes = repair_structure(self._end_flow())
        assert "E1" not in {f["source"] for f in repaired["flows"]}
        assert _note(notes, "Поток E1 → T2 удалён: конечное событие завершает")
        assert not _illegal_edges(repaired)

    def test_message_flow_into_foreign_start_survives(self):
        """Сообщение в старт другого пула — норма: оно и есть триггер процесса
        получателя, ни удалить, ни переставить его нельзя."""
        plan = self._pool_pair()
        repaired, notes = repair_structure(plan)
        by_pair = {(f["source"], f["target"]): f["kind"]
                   for f in repaired["flows"]}
        assert by_pair[("T1", "S2")] == "message"
        assert notes == []
        assert bpmn_generator.plan_gaps(plan) == []
        xml = BPMNGenerator()._generate_bpmn_xml(repaired)
        assert 'bpmn:messageFlow id="M1" sourceRef="T1" targetRef="S2"' in xml

    def test_sequence_flow_into_foreign_start_becomes_message_not_deleted(self):
        """Сначала межпуловость, потом концы: поток между пулами становится
        сообщением и живёт дальше как триггер чужого старта."""
        plan = self._pool_pair()
        plan["flows"][2]["kind"] = "sequence"
        repaired, notes = repair_structure(plan)
        by_pair = {(f["source"], f["target"]): f["kind"]
                   for f in repaired["flows"]}
        assert by_pair[("T1", "S2")] == "message"
        assert _note(notes, "между пулами преобразован")
        assert not _illegal_edges(repaired)

    @staticmethod
    def _pool_pair():
        return _plan_dict(
            participants=["ВкусВилл", "Курьер"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T1", "userTask", "Передать курьеру", ""),
                _e("E1", "endEvent", "Передан", ""),
                _e("S2", "startEvent", "Вызов курьера", "", participant="Курьер"),
                _e("T2", "userTask", "Довезти заказ", "", participant="Курьер"),
                _e("E2", "endEvent", "Доставлен", "", participant="Курьер"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                   _f("M1", "T1", "S2", kind="message"),
                   _f("F3", "S2", "T2"), _f("F4", "T2", "E2")],
        )

    def test_merged_role_pool_leaves_no_sequence_flow_into_start(self):
        """Слияние «роли-пула» превращает сообщение в поток внутри пула — и
        бывший законный триггер становится дугой в старт: её убирает та же
        проверка, что и в `_repair_flows`."""
        repaired, notes = repair_structure(_plan_dict(
            participants=["ВкусВилл", "Кладовщик"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                   {"id": "L_k", "name": "Кладовщик", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заказ", "L_m"),
                _e("T1", "userTask", "Проверить остатки", "L_m"),
                _e("S2", "startEvent", "Заявка на сборку", "", "Кладовщик"),
                _e("T2", "userTask", "Собрать заказ", "", "Кладовщик"),
                _e("E1", "endEvent", "Собрано", "L_m"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("M1", "T1", "S2", kind="message"),
                   _f("F2", "S2", "T2"), _f("F3", "T2", "E1")],
        ))
        assert _note(notes, "слит в «ВкусВилл» дорожкой «Кладовщик»")
        assert not [f for f in repaired["flows"] if f["target"] == "S2"]
        assert _note(notes, "удалён после слияния пулов")
        assert not _illegal_edges(repaired)

    def test_gaps_name_illegal_flow_ends_of_raw_plan(self):
        """Сырой ответ недоверен: нарушения уходят в переспрос списком
        конкретных потоков, а не молча чинятся удалением."""
        raw = self._start_flow()
        raw["elements"].append(_e("B1", "boundaryEvent", "Просрочка", "",
                                  attached_to="T1", event_definition="timer",
                                  timer="PT4H"))
        raw["flows"] += [_f("F4", "T1", "B1"), _f("F5", "E1", "T1")]
        gaps = bpmn_generator.plan_gaps(raw)
        assert any("F3" in g and "стартового события" in g for g in gaps)
        assert any("F4" in g and "граничное событие" in g for g in gaps)
        assert any("F5" in g and "конечное событие завершает" in g for g in gaps)
        assert all("перестрой маршрут" in g for g in gaps[-3:])

    def test_gaps_ignore_message_trigger_and_junk_flows(self):
        assert not [g for g in bpmn_generator.plan_gaps(self._pool_pair())
                    if "S2" in g]
        # Узлов нет или они мусор — падать нельзя, и выдумывать нарушение тоже.
        assert bpmn_generator.plan_gaps({"flows": [{"source": "E1",
                                                    "target": "S1"}]}) == []
        assert isinstance(bpmn_generator.plan_gaps(
            {"elements": [{"id": "E1", "kind": None}],
             "flows": [None, "x", {"source": "E1", "target": None}]}), list)

    @pytest.mark.parametrize("label", ["start", "boundary", "end"])
    def test_generated_xml_holds_the_rule(self, label):
        plan = {"start": self._start_flow, "boundary": self._boundary_flow,
                "end": self._end_flow}[label]()
        repaired, _ = repair_structure(plan)
        assert not _illegal_edges(repaired)
        assert not _illegal_xml_edges(BPMNGenerator()._generate_bpmn_xml(repaired))


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
        result = BPMNGenerator().generate("ВкусВилл: заявка, согласование, "
                                             "отгрузка со склада")
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
                               "attempts", "trace", "time_elapsed"}
        assert result["attempts"] == 1
        assert isinstance(result["gaps"], list)
        assert all(isinstance(g, str) for g in result["gaps"])
        assert isinstance(result["notes"], list)
        assert all(isinstance(n, str) for n in result["notes"])
        assert set(result["structure"]) == {"participants", "lanes", "elements",
                                            "flows"}
        assert all(set(lane) == {"id", "name", "participant"}
                   for lane in result["structure"]["lanes"])

    def test_trace_names_the_node_that_touched_the_plan(self, monkeypatch):
        """Контур — цепочка узлов, и провал инварианта обязан указывать на один
        из них: «модель не дала», «переспрос не добил», «починка унесла»."""
        result = _generate(monkeypatch, _plan())
        nodes = [entry["node"] for entry in result["trace"]]
        assert nodes == ["первый ответ модели", "вопрос о принадлежности",
                         "починка структуры", "генерация XML"]
        first = result["trace"][0]
        assert first["gaps"] == result["gaps"]
        assert first["elements"] >= 1
        # Переспрос в трейсе только если он заводился.
        assert "переспрос плана" not in nodes

    def test_trace_records_a_rejected_reask(self, monkeypatch):
        """Отказ от второго плана — решение контура, оно обязано быть видно:
        метрика «модель не исправила» иначе неотличима от «исправлять не стали»."""
        bad = TestVacantPools._vacant()
        fake = FakeLLM(monkeypatch, bad, bad)
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        reask = [e for e in result["trace"] if e["node"] == "переспрос плана"]
        assert len(reask) == 1
        assert reask[0]["kept"] == "первый ответ"
        assert reask[0]["gaps_before"] == reask[0]["gaps_after"]
        assert "не улучшил" in reask[0]["outcome"]
        assert len(fake.calls) == 2

    def test_trace_per_repair_step_shows_what_was_added_and_removed(
            self, monkeypatch):
        """Шаги починки записывают отпечаток «до/после»: удалённый пустой пул
        виден как удалённый id, а не только как строка в notes."""
        result = _generate(monkeypatch, self._vacant_for_trace())
        steps = next(e for e in result["trace"]
                     if e["node"] == "починка структуры")["steps"]
        by_name = {step["step"]: step for step in steps}
        dropped = by_name["удаление пустых пулов"]
        assert any(item.startswith("pool:") for item in dropped["removed"])
        assert dropped["notes"]
        assert all({"step", "added", "removed", "notes"} <= set(step)
                   for step in steps)

    @staticmethod
    def _vacant_for_trace():
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

    def test_lane_without_participant_survives_the_gap_check(self):
        """Сырой план модели живёт до починки, а в нём у дорожки может не быть
        `participant`. На живом прогоне именно этот путь ронял генерацию:
        `plan_gaps` читает план целиком и обязан читать его терпеливо."""
        raw = _plan_dict(
            participants=["ВкусВилл", "Кладовщик"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"},
                   {"id": "L_x", "name": "Кладовщик"}],
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
        gaps = bpmn_generator.plan_gaps(
            raw, "ВкусВилл собирает заказ, кладовщик проверяет остатки.")
        assert any("Кладовщик" in g for g in gaps)

    def test_the_gap_names_the_pool_the_model_has_to_fill(self):
        """Нарушение про пустой пул предписывает модели конкретное поле со
        конкретным значением: `participant="Кладовщик"`. Пока там стоял
        незаменённый плейсхолдер из примера схемы (`participant=«pool»`),
        модель должна была угадать имя своего же пула — и в переспрос шаги не
        приезжали."""
        raw = _plan_dict(
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
        gaps = bpmn_generator.plan_gaps(
            raw, "ВкусВилл собирает заказ, кладовщик проверяет остатки.")
        hint = [g for g in gaps if "без единого шага" in g and "Кладовщик" in g]
        assert hint
        assert 'participant="Кладовщик"' in hint[0]
        assert "«pool»" not in hint[0]

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

    @staticmethod
    def _event_untyped(kind, name, **extra):
        """План, где событие названо, но типа у него нет: «S1 → C1 → E1» для
        промежуточного и граничное на задаче T1."""
        host = _e("T1", "userTask", "Собрать заказ", "")
        event = _e("C1", kind, name, "", **extra)
        if kind == "boundaryEvent":
            event["attached_to"] = "T1"
            elements = [_e("S1", "startEvent", "Заказ", ""), host, event,
                        _e("T2", "userTask", "Эскалация", ""),
                        _e("E1", "endEvent", "Отгружено", "")]
            flows = [_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                     _f("F3", "C1", "T2"), _f("F4", "T2", "E1")]
        else:
            elements = [_e("S1", "startEvent", "Заказ", ""), event,
                        _e("E1", "endEvent", "Отгружено", "")]
            flows = [_f("F1", "S1", "C1"), _f("F2", "C1", "E1")]
        return _plan(participants=["ВкусВилл"], lanes=[], elements=elements,
                     flows=flows)

    def test_overdue_event_takes_timer_from_its_name(self, monkeypatch):
        """«Просрочка SLA» — это таймер: тип читается из подписи события, а не
        придумывается. Пустой кружок на схеме — хуже, чем названный тип."""
        result = _generate(monkeypatch, self._event_untyped(
            "boundaryEvent", "Просрочка SLA"))
        assert _note(result["notes"], "по его названию")
        assert 'attachedToRef="T1"' in result["bpmn"]
        assert "bpmn:timerEventDefinition" in result["bpmn"]

    def test_waiting_event_takes_message_from_its_name(self, monkeypatch):
        result = _generate(monkeypatch, self._event_untyped(
            "intermediateCatchEvent", "Ожидание подтверждения"))
        assert "bpmn:messageEventDefinition" in result["bpmn"]
        assert not _note(result["notes"], "нет определения")

    def test_event_name_that_types_nothing_stays_a_gap(self, monkeypatch):
        """«Отсутствие подписи получателя» — не таймер и не сообщение:
        угадывать нельзя, нарушение остаётся моделью на переспрос."""
        result = _generate(monkeypatch, self._event_untyped(
            "intermediateCatchEvent", "Отсутствие подписи получателя"))
        assert _note(result["notes"], "нет определения")
        assert "EventDefinition" not in result["bpmn"]
        assert any("C1" in gap for gap in result["gaps"])

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
        merges = _merge_gateways(result)
        assert [m["kind"] for m in merges] == ["exclusiveGateway"]
        assert merges[0]["name"] == "Итог: Хватает товара?"
        flows = [(f["source"], f["target"]) for f in result["structure"]["flows"]]
        assert ("T2", "T4") not in flows and ("T3", "T4") not in flows
        assert [flows.count((m["id"], "T4")) for m in merges] == [1]
        assert _note(result["notes"], "вставлен шлюз схождения")

    def test_inserted_merge_gateway_is_not_demoted(self, monkeypatch):
        """У сходящегося шлюза один исходящий — это норма, а не повод понижать."""
        result = _generate(monkeypatch, self._branching())
        merge = _merge_gateways(result)[0]
        assert _kinds(result["structure"])[merge["id"]] == "exclusiveGateway"
        assert not _note(result["notes"], "понижен до задачи")

    def test_parallel_split_gets_parallel_merge(self, monkeypatch):
        result = _generate(monkeypatch, self._branching(
            gateway="parallelGateway", first="", second=""))
        merges = _merge_gateways(result)
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
        assert not _merge_gateways(result)
        assert _note(result["notes"], "нет общего шлюза-расщепителя")

    @staticmethod
    def _hidden_split(first="прошла проверка", second="не прошла"):
        """Развилка, расставленная подписями на потоках от задачи: шлюза в плане
        нет, а ветки уже различаются условиями."""
        return _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T1", "userTask", "Проверить упаковку", ""),
                _e("T2", "userTask", "Отгрузить", ""),
                _e("T3", "userTask", "Пересобрать", ""),
                _e("T4", "userTask", "Закрыть заявку", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "T2", condition=first),
                   _f("F3", "T1", "T3", condition=second),
                   _f("F4", "T2", "T4"), _f("F5", "T3", "T4"),
                   _f("F6", "T4", "E1")],
        )

    def test_conditioned_branches_get_their_split_gateway(self, monkeypatch):
        result = _generate(monkeypatch, self._hidden_split())
        splits = [e for e in result["structure"]["elements"]
                  if e["name"] == "Выбор ветки"]
        assert [s["kind"] for s in splits] == ["exclusiveGateway"]
        gateway = splits[0]["id"]
        flows = {(f["source"], f["target"]): f
                 for f in result["structure"]["flows"]}
        assert ("T1", gateway) in flows
        assert flows[(gateway, "T2")]["condition"] == "прошла проверка"
        assert ("T1", "T2") not in flows and ("T1", "T3") not in flows
        assert _kinds(result["structure"])["T1"] == "userTask"
        assert _note(result["notes"], "вставлен шлюз развилки")

    def test_inserted_split_pairs_with_its_merge(self, monkeypatch):
        result = _generate(monkeypatch, self._hidden_split())
        pairs = {e["name"]: e["kind"] for e in result["structure"]["elements"]
                 if e["name"] in ("Выбор ветки", "Итог: Выбор ветки")}
        assert pairs == {"Выбор ветки": "exclusiveGateway",
                         "Итог: Выбор ветки": "exclusiveGateway"}

    def test_branches_without_conditions_keep_the_model_decision(self, monkeypatch):
        result = _generate(monkeypatch, self._hidden_split(first="", second=""))
        assert not [e for e in result["structure"]["elements"]
                    if e["name"] == "Выбор ветки"]

    def test_split_insertion_respects_its_budget(self, monkeypatch):
        monkeypatch.setattr(bpmn_generator, "MAX_SPLIT_GATEWAYS", 1)
        result = _generate(monkeypatch, _plan(
            participants=["ВкусВилл"], lanes=[],
            elements=[
                _e("S1", "startEvent", "Заказ", ""),
                _e("T1", "userTask", "Проверить", ""),
                _e("A", "userTask", "Отгрузить", ""),
                _e("B", "userTask", "Пересобрать", ""),
                _e("T2", "userTask", "Собрать итог", ""),
                _e("C", "userTask", "Уведомить", ""),
                _e("D", "userTask", "Архивировать", ""),
                _e("E1", "endEvent", "Отгружено", ""),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "A", condition="целое"),
                   _f("F3", "T1", "B", condition="брак"), _f("F4", "A", "T2"),
                   _f("F5", "B", "T2"), _f("F6", "T2", "C", condition="есть отзыв"),
                   _f("F7", "T2", "D", condition="отзыва нет"), _f("F8", "C", "E1"),
                   _f("F9", "D", "E1")],
        ))
        splits = [e for e in result["structure"]["elements"]
                  if e["name"] == "Выбор ветки"]
        assert len(splits) == 1
        assert _note(result["notes"], "исчерпан лимит вставок")

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
        assert not _merge_gateways(result)


class TestDeclaredRoles:
    """Роль или подразделение модель объявляет сама (`external: false` +
    `inside`): отличить «ИТ-отдел» от «Перевозчика» без описания нельзя, а
    раздутых участников схема прощает плохо."""

    def test_declared_role_becomes_a_lane_of_its_organization(self):
        plan = _plan_dict(
            participants=[{"name": "ВкусВилл"},
                          {"name": "ИТ-отдел", "external": False,
                           "inside": "ВкусВилл"}],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заявка поступила", "L_m"),
                _e("S2", "startEvent", "Заявка в ИТ", "", participant="ИТ-отдел"),
                _e("T1", "userTask", "Выдать доступ", "", participant="ИТ-отдел"),
                _e("E1", "endEvent", "Доступ выдан", "", participant="ИТ-отдел"),
                _e("E2", "endEvent", "Заявка закрыта", "L_m"),
            ],
            flows=[_f("F1", "S1", "S2", kind="message"), _f("F2", "S2", "T1"),
                   _f("F3", "T1", "E1"), _f("F4", "S1", "E2")])
        repaired, notes = repair_structure(plan)
        assert [p["name"] for p in repaired["participants"]] == ["ВкусВилл"]
        assert any(l["name"] == "ИТ-отдел" and l["participant"] == "ВкусВилл"
                   for l in repaired["lanes"])
        assert all(e["participant"] == "ВкусВилл" for e in repaired["elements"])
        assert _note(notes, "объявлен ролью пула «ВкусВилл»")

    def test_role_without_organization_is_a_gap(self):
        raw = {"participants": [{"name": "ИТ-отдел", "external": False}],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Доступ",
                             "participant": "ИТ-отдел"}]}
        assert any("external=false" in g for g in bpmn_generator.plan_gaps(raw))

    def test_role_pointing_to_an_absent_pool_is_a_gap(self):
        raw = {"participants": [{"name": "ИТ-отдел", "external": False,
                                 "inside": "Поставщик"}],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Доступ",
                             "participant": "ИТ-отдел"}]}
        assert any("inside=" in g for g in bpmn_generator.plan_gaps(raw))

    def test_complete_declaration_is_not_a_gap(self):
        raw = {"participants": [{"name": "ИТ-отдел", "external": False,
                                 "inside": "ВкусВилл"}, {"name": "ВкусВилл"}],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Доступ",
                             "participant": "ИТ-отдел"},
                            {"id": "T2", "kind": "userTask", "name": "Заявка",
                             "participant": "ВкусВилл"}]}
        gaps = bpmn_generator.plan_gaps(raw)
        assert not [g for g in gaps if "external=false" in g or "inside=" in g]

    def test_pool_with_a_lane_of_its_own_name_is_a_declared_role(self):
        """«Бюджетный контролёр» с дорожкой «Бюджетный контролёр» — роль,
        объявившая саму себя: приёмника для слияния в плане нет, и угадать
        организацию по названию должности нельзя."""
        raw = {"participants": ["Бюджетный контролёр"],
               "lanes": [{"id": "L_b", "name": "Бюджетный контролёр",
                          "participant": "Бюджетный контролёр"}],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Сверка",
                             "participant": "Бюджетный контролёр"}]}
        assert any("дорожку с таким же именем" in g
                   for g in bpmn_generator.plan_gaps(raw))

    def test_pool_without_lanes_is_not_demanded_as_a_role(self):
        """Внешний участник без дорожек — норма: «Поставщик» не обязан кем-то
        «быть внутри», и требовать от модели объяснять каждый пул нельзя."""
        raw = {"participants": ["Поставщик"], "lanes": [],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Отгрузка",
                             "participant": "Поставщик"}]}
        assert not [g for g in bpmn_generator.plan_gaps(raw)
                    if "дорожку с таким же именем" in g]

    def test_pool_with_lanes_of_real_roles_is_not_a_role(self):
        raw = {"participants": ["Склад"],
               "lanes": [{"id": "L_a", "name": "Кладовщик", "participant": "Склад"},
                         {"id": "L_b", "name": "Транспортный отдел",
                          "participant": "Склад"}],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Сборка",
                             "participant": "Склад", "lane": "L_a"}]}
        assert not [g for g in bpmn_generator.plan_gaps(raw)
                    if "дорожку с таким же именем" in g]


class TestOwnershipClarification:
    """Кому принадлежит шаг и кому — роль, знает только модель: шаг «получить
    подтверждение отгрузки от поставщика» она записала чужим пулом, а «HR» и
    «ИТ» объявила пулами с одноимённой дорожкой. Угадать это по коду нельзя,
    поэтому вопрос узкий и ответ маленький (`moves`/`roles`), а не ещё одна
    переписка плана: на полном плане живые прогоны теряли шаги."""

    TEXT = ("ВкусВилл заводит заявку, менеджер согласует её. Поставщик "
            "подтверждает отгрузку, кладовщик принимает товар.")

    @staticmethod
    def _plan():
        return {"participants": ["ВкусВилл", "Поставщик"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Получить подтверждение отгрузки от поставщика",
                     "participant": "ВкусВилл"},
                ], "flows": []}

    def test_model_answer_moves_the_step_and_keeps_the_pool(self, monkeypatch):
        plan = _fence(self._plan())
        fake = FakeLLM(monkeypatch, plan, plan,
                       '{"moves": [{"element": "A2", "participant": "Поставщик"}]}')
        result = BPMNGenerator().generate(self.TEXT)
        assert result["status"] == "success"
        assert len(fake.calls) == 3
        assert _note(result["notes"], "перенесён в пул «Поставщик»")
        moved = next(e for e in result["structure"]["elements"]
                     if e["id"] == "A2")
        assert moved["participant"] == "Поставщик"
        assert "Поставщик" in [p["name"] for p in result["structure"]["participants"]]
        # правка уменьшила список нарушений: переспрос больше не зовёт её «пустым»
        assert not [g for g in result["gaps"] if "Поставщик" in g]
        # вопрос был узким: модель просили назвать переносы, а не план целиком
        assert "Верни moves, roles и missing" in fake.calls[2][1]["content"]
        assert "A2" in fake.calls[2][1]["content"]

    def test_question_names_every_key_the_contour_reads(self, monkeypatch):
        """Ключи, которые контур разбирает из ответа, обязан называть и вопрос:
        модель не догадывается про `missing`, если её о нём не попросить (пустой
        ответ на него стоил `expected_participants` в живых прогонах). Роль без
        решения — то же молчание, поэтому кандидаты требуют ответа по каждому."""
        plan = _fence(self._plan())
        fake = FakeLLM(monkeypatch, plan, plan, '{"moves": [], "roles": []}')
        BPMNGenerator().generate(self.TEXT)
        assert "Верни moves, roles и missing" in fake.calls[-1][1]["content"]
        assert "каждый перечисленный кандидат" in fake.calls[-1][0]["content"]

    def test_a_named_actor_is_asked_about_even_with_no_other_question(
            self, monkeypatch):
        """План сам выписал действующее лицо в `actors` и не дал ему пула —
        вопрос обязуется и когда пустых пулов, ролей-кандидатов и непрозванных
        токенов нет вовсе. Прогон #47: два случая `expected_participants`
        пропали молчанием — контур о противоречии знал и не спросил никого."""
        roster = ('{"organizations": ["ВкусВилл"], "systems": [], '
                  '"counterparties": [], "roles": []}')
        plan = {"participants": ["ВкусВилл"],
                "actors": ["ВкусВилл", "Клиент"],
                "lanes": [],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Оформить возврат",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask", "name": "Принести товар",
                     "participant": "ВкусВилл"}],
                "flows": []}
        fake = FakeLLM(monkeypatch, roster, _fence(plan),
                       '{"missing": [{"pool": "Клиент", "external": true, '
                       '"steps": ["A2"]}]}')
        result = BPMNGenerator().generate(
            "ВкусВилл оформляет возврат, клиент приносит товар в магазин.")
        question = next(c[1]["content"] for c in fake.calls
                        if "Верни moves, roles и missing" in c[1]["content"])
        assert "«Клиент»" in question, question[-500:]
        names = [p["name"] for p in result["structure"]["participants"]]
        assert "Клиент" in names, result["notes"]
        assert not [g for g in result["gaps"] if "Клиент" in g], result["gaps"]

    def test_move_of_an_unknown_element_is_refused(self, monkeypatch):
        plan = _fence(self._plan())
        fake = FakeLLM(monkeypatch, plan, plan,
                       '{"moves": [{"element": "nope", "participant": "Поставщик"}]}')
        result = BPMNGenerator().generate(self.TEXT)
        assert _note(result["notes"], "такого шага в плане нет")

    def test_move_to_a_pool_with_its_own_steps_says_so(self, monkeypatch):
        plan = _fence(self._plan())
        FakeLLM(monkeypatch, plan, plan,
                '{"moves": [{"element": "A1", "participant": "ВкусВилл"}]}')
        result = BPMNGenerator().generate(self.TEXT)
        assert _note(result["notes"], "в нём уже есть свои шаги")
        moved = next(e for e in result["structure"]["elements"] if e["id"] == "A1")
        assert moved["participant"] == "ВкусВилл"

    def test_move_to_an_absent_pool_says_there_is_no_such_pool(self, monkeypatch):
        """Отказ обязан называть настоящую причину: «этого пула среди пустых не
        было» звучит как «пул есть, но не пуст», а пула-то как раз и нет — по
        такой заметке живой прогон не разобрать, про что спрашивать модель."""
        plan = _fence(self._plan())
        FakeLLM(monkeypatch, plan, plan,
                '{"moves": [{"element": "A2", "participant": "Экспедитор"}]}')
        result = BPMNGenerator().generate(self.TEXT)
        assert _note(result["notes"], "такого пула в плане нет")
        moved = next(e for e in result["structure"]["elements"] if e["id"] == "A2")
        # отказ отказом, а шаг без пула не остаётся: «Поставщик» он забирает по
        # своему названию, а не по выдуманному «Экспедитору»
        assert moved["participant"] != "Экспедитор"

    def test_generic_organization_name_is_not_made_into_a_pool(self, monkeypatch):
        """«Организация» — родовое слово, а не имя: такой пул в живом прогоне
        собрал три роли с нулём перенесённых шагов, и участников схемы стало не
        с чем проверять. Слово встречается в описании, но участником не делает."""
        plan = {"participants": ["ВкусВилл", "Дежурный инженер"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Принять алерт",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Эскалировать руководителю",
                     "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced,
                '{"roles": [{"pool": "Дежурный инженер", "inside": "Организация"}]}')
        result = BPMNGenerator().generate(
            "Дежурный инженер организации принимает алерт и эскалирует его "
            "руководителю.")
        assert _note(result["notes"], "родовое слово")
        assert "Организация" not in [
            p["name"] for p in result["structure"]["participants"]]
        # и роль не переезжает в «единственную организацию плана»: хозяина по
        # имени роли угадать нельзя, это остаётся вопросом к модели
        assert result["structure"]["lanes"] == []

    def test_role_question_asks_the_three_most_doomed_pools(self, monkeypatch):
        """Длинный список кандидатов модель закрывает в ~31% случаев, короткого —
        в ~70% (три живых прогона, 63 кейса с вопросом о ролях). Спрашиваем про
        трёх самых обречённых — о тех, у кого меньше своих шагов, и потому
        обходившихся пулом: они и уезжают со схемы первыми."""
        plan = {"participants": ["ВкусВилл", "Альфа", "Бета", "Гамма",
                                 "Дельта", "Ипсилон"],
                "lanes": [{"id": "L1", "name": "Кладовщик",
                           "participant": "ВкусВилл"}],
                "elements": (
                    [{"id": f"V{i}", "kind": "userTask", "name": f"Шаг {i}",
                      "participant": "ВкусВилл", "lane": "L1"} for i in range(1, 7)]
                    + [{"id": "A1", "kind": "userTask", "name": "Действие альфы",
                        "participant": "Альфа"},
                       {"id": "B1", "kind": "userTask", "name": "Действие беты",
                        "participant": "Бета"},
                       {"id": "B2", "kind": "userTask", "name": "Второе беты",
                        "participant": "Бета"},
                       {"id": "G1", "kind": "userTask", "name": "Действие гаммы",
                        "participant": "Гамма"},
                       {"id": "D1", "kind": "userTask", "name": "Дельта один",
                        "participant": "Дельта"},
                       {"id": "D2", "kind": "userTask", "name": "Дельта два",
                        "participant": "Дельта"},
                       {"id": "D3", "kind": "userTask", "name": "Дельта три",
                        "participant": "Дельта"},
                       {"id": "I1", "kind": "userTask", "name": "Ипсилон один",
                        "participant": "Ипсилон"},
                       {"id": "I2", "kind": "userTask", "name": "Ипсилон два",
                        "participant": "Ипсилон"},
                       {"id": "I3", "kind": "userTask", "name": "Ипсилон три",
                        "participant": "Ипсилон"},
                       {"id": "I4", "kind": "userTask", "name": "Ипсилон четыре",
                        "participant": "Ипсилон"}]), "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced, '{"roles": [], "moves": [], '
                                                    '"missing": []}')
        BPMNGenerator().generate(
            "ВкусВилл собирает заказ: работают кладовщик, альфа, бета, гамма, "
            "дельта и ипсилон.")
        asked = _role_candidates(fake.calls[-1][1]["content"])
        assert "Альфа" in asked and "Бета" in asked and "Гамма" in asked
        assert "Дельта" not in asked and "Ипсилон" not in asked

    def test_role_answer_about_a_pool_that_was_not_listed_still_counts(
            self, monkeypatch):
        """Не спросили — не значит отвергнуть: ответ модели про любого кандидата
        плана принимается, иначе короткая выборка вопросы тихо теряла бы."""
        plan = {"participants": ["ВкусВилл", "Альфа", "Бета", "Гамма", "Дельта"],
                "lanes": [{"id": "L1", "name": "Кладовщик",
                           "participant": "ВкусВилл"}],
                "elements": (
                    [{"id": f"V{i}", "kind": "userTask", "name": f"Шаг {i}",
                      "participant": "ВкусВилл", "lane": "L1"} for i in range(1, 7)]
                    + [{"id": "A1", "kind": "userTask", "name": "Альфа",
                        "participant": "Альфа"},
                       {"id": "B1", "kind": "userTask", "name": "Бета",
                        "participant": "Бета"},
                       {"id": "G1", "kind": "userTask", "name": "Гамма",
                        "participant": "Гамма"},
                       {"id": "D1", "kind": "userTask", "name": "Дельта",
                        "participant": "Дельта"}]), "flows": []}
        fenced = _fence(plan)
        roles = '{"roles": [{"pool": "Дельта", "inside": "ВкусВилл"}]}'
        # ответа два на случай переспроса планом: где он отработает, там и
        # пройдёт, а вопрос о ролях получит свой ответ в любом случае
        fake = FakeLLM(monkeypatch, fenced, roles, roles)
        result = BPMNGenerator().generate(
            "ВкусВилл собирает заказ: кладовщик, альфа, бета, гамма и дельта.")
        asked = _role_candidates(
            [c[1]["content"] for c in fake.calls if "роль, не участник):" in c[1]["content"]][-1])
        assert "Дельта" not in asked
        assert _note(result["notes"], "пул «Дельта» — роль «ВкусВилл» по описанию")

    def test_step_that_names_the_system_leaves_a_one_step_donor(self, monkeypatch):
        """Донорский запрет терял участника: модель назвала «WMS» и его шаг
        «Зарезервировать упаковку в WMS», а перенос блокировался тем, что у
        пула-донора оставался единственный шаг. Имя шага, в котором сам
        участник и назван, — доказательство сильнее бухгалтерии донора: два
        живых прогона подряд теряли `expected_participants` именно здесь."""
        plan = {"participants": ["Оператор склада"],
                "elements": [{"id": "A1", "kind": "userTask",
                              "name": "Зарезервировать упаковку в WMS",
                              "participant": "Оператор склада"}], "flows": []}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced,
                '{"missing": [{"pool": "WMS", "steps": ["A1"]}]}')
        result = BPMNGenerator().generate(
            "Оператор склада резервирует упаковку в WMS, система подтверждает "
            "резерв.")
        assert _note(result["notes"], "участник «WMS» добавлен на схему")
        moved = next(e for e in result["structure"]["elements"] if e["id"] == "A1")
        assert moved["participant"] == "WMS"
        assert "WMS" in [p["name"] for p in result["structure"]["participants"]]

    def test_generic_organization_name_is_not_invented_a_host(self, monkeypatch):
        """Две действующие организации — выбирать хозяина роли не из чего, и
        родовое слово не становится подсказкой для догадки: пул «Организация» не
        заводится, роль остаётся тем, чем её назвал план."""
        plan = {"participants": ["ВкусВилл", "Перевозчик", "Дежурный инженер"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Принять алерт",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask", "name": "Отгрузить",
                     "participant": "ВкусВилл"},
                    {"id": "B1", "kind": "userTask", "name": "Вывезти груз",
                     "participant": "Перевозчик"}], "flows": []}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced,
                '{"roles": [{"pool": "Дежурный инженер", "inside": "Организация"}]}')
        result = BPMNGenerator().generate(
            "Дежурный инженер организации принимает алерт, ВкусВилл отгружает "
            "товар, а перевозчик вывозит груз.")
        assert _note(result["notes"], "родовое слово, а не название")
        assert "Организация" not in [
            p["name"] for p in result["structure"]["participants"]]

    def test_vacant_pool_named_in_text_can_be_declared_a_role(self, monkeypatch):
        """Пустой пул, названный в описании, — тупик: шаги взять не откуда,
        удалять нельзя. Единственный честный выход — роль организации, и модель
        предлагает его сама («оператор склада» ролью не объявлен: кандидатов не
        было). Пустой пул обязан быть среди кандидатов, иначе ответ модели
        выбрасывается."""
        plan = {"participants": ["ВкусВилл", "Кладовщик"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Принять товар по накладной",
                     "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced,
                       '{"roles": [{"pool": "Кладовщик", "inside": "ВкусВилл"}]}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку, кладовщик принимает товар по накладной.")
        assert "Кладовщик" in _role_candidates(fake.calls[-1][1]["content"])
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"], result["notes"]
        lanes = {l["name"]: l["participant"] for l in result["structure"]["lanes"]}
        assert lanes.get("Кладовщик") == "ВкусВилл"
        assert _note(result["notes"], "роль «ВкусВилл» по описанию")

    def test_a_role_cannot_host_another_role(self, monkeypatch):
        """Хозяин обязан быть пулом: «Кладовщик» — дорожка, то есть роль, и
        назначать его организацией нельзя даже если слово звучит в описании.
        Живой пропуск этой рамки стоил состава: ответ «Перевозчик — роль
        водитель» заводил пул «водитель» (имя-то в тексте есть), и организация
        уезжала в него дорожкой — то есть к своему же сотруднику."""
        plan = {"participants": ["Склад", "Экспедитор"],
                "lanes": [{"id": "L1", "name": "Кладовщик",
                           "participant": "Склад"}],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Собрать груз",
                     "participant": "Склад", "lane": "L1"},
                    {"id": "A2", "kind": "userTask", "name": "Отгрузить паллету",
                     "participant": "Склад", "lane": "L1"}],
                "flows": [{"id": "F1", "source": "A1", "target": "A2"}]}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced,
                '{"roles": [{"pool": "Экспедитор", "inside": "Кладовщик"}]}')
        result = BPMNGenerator().generate(
            "Склад собирает груз и отгружает паллету, кладовщик подписывает "
            "накладную, экспедитор сопровождает груз.")

        assert _note(result["notes"], "тоже роль, а не организация")
        assert not _note(result["notes"], "организация из описания")
        pairs = [(lane["name"], lane["participant"])
                 for lane in result["structure"]["lanes"]]
        assert ("Экспедитор", "Кладовщик") not in pairs
        assert "Кладовщик" not in [
            p["name"] if isinstance(p, dict) else p
            for p in result["structure"]["participants"]]

    def test_missing_actor_that_is_a_declared_lane_stays_a_lane(self, monkeypatch):
        """«Кого забыть нельзя» — ответ про роль не заводит пул с именем роли.

        Живой прогон #43 (warehouse r1): состав посадил «Водителя» дорожкой в
        «Перевозчика», узкий вопрос про недостающего участника вернул
        «Водителя» с его шагами, контур завёл из этого пул «Водитель»,
        «Перевозчик» остался без действий, и `_drop_vacant_pools` удалил
        названного в описании контрагента. Шаги уходят в пул-хозяин дорожки.
        """
        plan = {"participants": ["Склад", "Перевозчик"],
                "lanes": [{"id": "L_d", "name": "Водитель",
                           "participant": "Перевозчик"}],
                "elements": [
                    {"id": "S1", "kind": "startEvent", "name": "Заявка",
                     "participant": "Склад", "lane": ""},
                    {"id": "A1", "kind": "userTask", "name": "Собрать груз",
                     "participant": "Склад", "lane": ""},
                    {"id": "A2", "kind": "userTask", "name": "Оформить бумаги",
                     "participant": "Склад", "lane": ""},
                    {"id": "A3", "kind": "userTask", "name": "Привезти машину",
                     "participant": "Склад", "lane": ""},
                    {"id": "E1", "kind": "endEvent", "name": "Готово",
                     "participant": "Склад", "lane": ""}],
                "flows": [{"id": "F1", "source": "S1", "target": "A1"},
                          {"id": "F2", "source": "A1", "target": "A2"},
                          {"id": "F3", "source": "A2", "target": "A3"},
                          {"id": "F4", "source": "A3", "target": "E1"}]}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced,
                '{"missing": [{"pool": "Водитель", "external": true,'
                ' "steps": ["A3"]}]}')
        result = BPMNGenerator().generate(
            "Склад собирает груз, оформляет бумаги и вызывает водителя, "
            "водитель привозит машину от перевозчика, склад закрывает заявку.")

        assert _note(result["notes"], "состав уже назвал его ролью")
        pools = [p["name"] if isinstance(p, dict) else p
                 for p in result["structure"]["participants"]]
        assert "Водитель" not in pools
        assert "Перевозчик" in pools, result["notes"]
        step = next(e for e in result["structure"]["elements"]
                    if e["id"] == "A3")
        assert step["participant"] == "Перевозчик"
        lanes = {lane["id"]: lane["participant"]
                 for lane in result["structure"]["lanes"]}
        assert lanes.get(step["lane"]) == "Перевозчик"

    def test_vacant_pool_absent_from_text_is_not_a_role_candidate(
            self, monkeypatch):
        """Пустой пул, которого в описании нет, — выдумка, и сворачивать её в
        дорожку нельзя: иначе вопрос легализовал бы участника, которого модель
        придумала."""
        plan = {"participants": ["ВкусВилл", "Робот-курьер"],
                "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                              "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced,
                       '{"roles": [{"pool": "Робот-курьер", "inside": "ВкусВилл"}]}')
        result = BPMNGenerator().generate("ВкусВилл заводит заявку.")
        assert "Робот-курьер" not in _role_candidates(
            fake.calls[-1][1]["content"])
        assert _note(result["notes"], "среди кандидатов такого пула не было")
        assert "Робот-курьер" not in [
            p["name"] for p in result["structure"]["participants"]]

    def test_role_of_a_role_lands_on_the_organization(self):
        """Цепочка «Кладовщик — роль Склада, Склад — роль ВкусВилла» обязана
        приземлиться на организацию: дорожка внутри пула, который сам уедет в
        чужой, повиснет на несуществующем процессе."""
        participants = [{"name": "ВкусВилл"},
                        {"name": "Склад", "external": False, "inside": "ВкусВилл"},
                        {"name": "Кладовщик", "external": False, "inside": "Склад"}]
        lanes: list = []
        bpmn_generator._declare_role_lanes(participants, lanes, set(), [])
        assert {l["name"]: l["participant"] for l in lanes} == {
            "Склад": "ВкусВилл", "Кладовщик": "ВкусВилл"}

    def test_mutual_role_declaration_creates_no_lane(self):
        participants = [{"name": "Оператор", "external": False, "inside": "Смена"},
                        {"name": "Смена", "external": False, "inside": "Оператор"}]
        lanes: list = []
        bpmn_generator._declare_role_lanes(participants, lanes, set(), [])
        assert lanes == []

    def test_role_without_an_organization_is_not_parked_on_a_guess(self):
        """Хозяина роли код не подставляет, даже когда действующая организация в
        плане одна: промер живого прогона поймал обратный случай — «единственной
        организацией с шагами» оказался пул «Руководитель службы поддержки», и
        сама организация уехала к своему сотруднику дорожкой. Роль без `inside`
        остаётся пулом и нарушением, которое закрывает только модель."""
        participants = [{"name": "ВкусВилл"},
                        {"name": "Кладовщик", "external": False}]
        elements = [{"id": "A1", "kind": "userTask", "name": "Принять товар",
                     "participant": "ВкусВилл"}]
        lanes: list = []
        notes: list = []
        bpmn_generator._declare_role_lanes(participants, lanes, set(), notes)
        assert lanes == []

    def test_role_without_an_organization_is_asked_whatever_the_plan_offers(self):
        """Вопрос про организацию роли звучит и когда план как будто бы подсказывает
        ответ: молчание план-гейта отдало бы выбор имени хозяйки коду."""
        for participants, elements in (
            ([{"name": "ВкусВилл"}, {"name": "Кладовщик", "external": False}],
             [{"id": "A1", "kind": "userTask", "name": "Принять товар",
               "participant": "ВкусВилл"}]),
            ([{"name": "ВкусВилл"}, {"name": "Перевозчик"},
              {"name": "Водитель", "external": False}],
             [{"id": "A1", "kind": "userTask", "name": "Отгрузить",
               "participant": "ВкусВилл"},
              {"id": "B1", "kind": "userTask", "name": "Вывезти",
               "participant": "Перевозчик"}]),
        ):
            gaps = bpmn_generator.plan_gaps(
                {"participants": participants, "elements": elements},
                "Кладовщик принимает товар у водителя.")
            assert [g for g in gaps if "помечен ролью" in g]

    def test_missing_participant_declared_a_role_becomes_a_lane(self, monkeypatch):
        """Живой прогон вернул «кладовщик принимает товар» в `missing`
        самостоятельным пулом — и схема получила третьего участника там, где по
        тексту роль (при максимуме в два). Ответ с `external: false` + `inside`
        обязан свернуться в дорожку: число пулов считает оракул, а не
        предположение модели о том, кто «самостоятельный»."""
        plan = {"participants": ["ВкусВилл", "Поставщик"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Получить подтверждение отгрузки от поставщика",
                     "participant": "ВкусВилл"},
                    {"id": "A3", "kind": "userTask",
                     "name": "Принять товар по накладной",
                     "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced,
                       '{"moves": [{"element": "A2", "participant": "Поставщик"}],'
                       ' "missing": [{"pool": "Кладовщик", "external": false,'
                       ' "inside": "ВкусВилл", "steps": ["A3"]}]}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку. Поставщик подтверждает отгрузку, кладовщик "
            "принимает товар по накладной.")
        # Модель сворачивает ролью того, кого вопрос об этом спрашивает: без
        # формы ответа в промпте она возвращала `external: true` по умолчанию.
        assert '"external": false' in fake.calls[-1][0]["content"]
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл", "Поставщик"], result["notes"]
        lanes = {l["name"]: l["participant"] for l in result["structure"]["lanes"]}
        assert lanes.get("Кладовщик") == "ВкусВилл"
        assert next(e for e in result["structure"]["elements"]
                    if e["id"] == "A3")["participant"] == "ВкусВилл"

    def test_named_system_without_a_pool_is_asked_by_itself(self, monkeypatch):
        """Ни пустых пулов, ни ролей-пулов: в плане только названная в тексте
        система без пула. Вопрос о принадлежности раньше заводился исключительно
        из-за пустых пулов, и «WMS» доезжал до переспроса планом целиком — а
        повтор переписывал план хуже. Шаг модели здесь — назвать чужие шаги."""
        plan = {"participants": ["ВкусВилл"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Зарегистрировать отгрузку в WMS",
                     "participant": "ВкусВилл"},
                    {"id": "A3", "kind": "userTask",
                     "name": "Списать остатки в WMS", "participant": "ВкусВилл"}],
                "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced,
                       '{"missing": [{"pool": "WMS", "external": true,'
                       ' "steps": ["A2", "A3"]}]}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку, система WMS регистрирует отгрузку и "
            "списывает остатки в WMS.")
        assert "WMS" in fake.calls[-1][1]["content"]
        pools = [p["name"] for p in result["structure"]["participants"]]
        assert "WMS" in pools, result["notes"]
        assert _note(result["notes"], "добавлен на схему")
        for step in ("A2", "A3"):
            assert next(e for e in result["structure"]["elements"]
                        if e["id"] == step)["participant"] == "WMS"
        # донор не выхолощен: у «ВкусВилла» остаётся своё действие
        assert next(e for e in result["structure"]["elements"]
                    if e["id"] == "A1")["participant"] == "ВкусВилл"

    def test_missing_participant_rides_the_ownership_question(self, monkeypatch):
        """Вопрос о пропущенных участниках едет тем же вызовом, который уже нужен
        из-за пустого пула: второй запрос пользователь бы не дождался."""
        plan = {"participants": ["ВкусВилл", "Поставщик"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл"},
                    {"id": "A2", "kind": "userTask",
                     "name": "Получить подтверждение отгрузки от поставщика",
                     "participant": "ВкусВилл"},
                    {"id": "A3", "kind": "userTask", "name": "Проверить платёж",
                     "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced,
                       '{"moves": [{"element": "A2", "participant": "Поставщик"}],'
                       ' "missing": [{"pool": "Банк", "external": true,'
                       ' "steps": ["A1"]}]}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку. Поставщик подтверждает отгрузку, банк "
            "проверяет платёж.")
        assert len(fake.calls) == 3
        question = fake.calls[-1][1]["content"]
        assert "назови в missing каждое действующее лицо" in question
        pools = [p["name"] for p in result["structure"]["participants"]]
        assert "Банк" in pools and "Поставщик" in pools
        assert next(e for e in result["structure"]["elements"]
                    if e["id"] == "A1")["participant"] == "Банк"
        assert _note(result["notes"], "добавлен на схему")

    def test_participant_absent_from_the_description_is_refused(self, monkeypatch):
        """Имя участника берётся из описания: дорисовать на схему того, кого в
        тексте нет, вопрос не позволяет."""
        fenced = _fence(self._plan())
        FakeLLM(monkeypatch, fenced, fenced,
                '{"missing": [{"pool": "Робот-курьер", "external": true,'
                ' "steps": ["A1"]}]}')
        result = BPMNGenerator().generate(self.TEXT)
        assert _note(result["notes"], "в описании его нет")
        assert "Робот-курьер" not in [
            p["name"] for p in result["structure"]["participants"]]
        assert next(e for e in result["structure"]["elements"]
                    if e["id"] == "A1")["participant"] == "ВкусВилл"

    def test_plan_with_many_pools_is_not_asked_about_missing_actors(self, monkeypatch):
        """С четырёх пулов модель размечает участников сама: просить её назвать
        ещё одного — разрешение выдумать лишнего."""
        plan = {"participants": ["ВкусВилл", "Поставщик", "Банк", "Логистика"],
                "lanes": [{"id": "L1", "name": "Менеджер", "participant": "ВкусВилл"},
                          {"id": "L2", "name": "Логист", "participant": "Поставщик"},
                          {"id": "L3", "name": "Инспектор", "participant": "Банк"}],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл", "lane": "L1"},
                    {"id": "A2", "kind": "userTask", "name": "Отгрузить",
                     "participant": "Поставщик", "lane": "L2"},
                    {"id": "A3", "kind": "userTask", "name": "Проверить платёж",
                     "participant": "Банк", "lane": "L3"}], "flows": []}
        fenced = _fence(plan)
        fake = FakeLLM(monkeypatch, fenced, fenced, '{"moves": [], "roles": []}')
        BPMNGenerator().generate(
            "ВкусВилл заводит заявку, поставщик отгружает, банк проверяет платёж, "
            "логистика ищет транспорт.")
        question = fake.calls[-1][1]["content"]
        assert "спрашивать не нужно" in question

    def test_single_pool_plan_means_no_extra_call(self, monkeypatch):
        good = {"participants": ["ВкусВилл"],
                "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                              "participant": "ВкусВилл"}], "flows": []}
        fake = FakeLLM(monkeypatch, _fence(good))
        result = BPMNGenerator().generate("ВкусВилл заводит заявку.")
        assert result["attempts"] == 1 and len(fake.calls) == 1

    def test_condition_in_the_description_without_a_gateway_is_a_gap(self):
        """Линейный план там, где текст различает ветки, — обещанного процесса
        на схеме нет. Нарушение ищется по описанию, потому что структурного
        признака у него нет: развилку некуда вставлять, её надо нарисовать."""
        raw = {"participants": ["ВкусВилл"],
               "elements": [
                   {"id": "A1", "kind": "userTask", "name": "Проверить", "participant": "ВкусВилл"},
                   {"id": "A2", "kind": "userTask", "name": "Отгрузить", "participant": "ВкусВилл"}]}
        gaps = bpmn_generator.plan_gaps(raw, "Кладовщик проверяет заказ. Если "
                                             "товара не хватает, он заказывает остаток.")
        assert [g for g in gaps if "ни одного шлюза" in g and "Если" in g]
        # план с настоящей развилкой не нарушен, даже если условие в тексте есть
        raw["elements"].append({"id": "G1", "kind": "exclusiveGateway",
                                "name": "Хватает?", "participant": "ВкусВилл"})
        raw["flows"] = [{"id": "F1", "source": "G1", "target": "A1",
                         "kind": "sequence", "condition": "не хватает"},
                        {"id": "F2", "source": "G1", "target": "A2",
                         "kind": "sequence", "condition": "хватает"}]
        assert not [g for g in bpmn_generator.plan_gaps(
            raw, "Кладовщик проверяет заказ. Если товара не хватает, он "
                 "заказывает остаток.") if "ни одного шлюза" in g]

    def test_gateway_that_splits_nothing_is_not_a_branch(self):
        """Шлюз с одним входящим и одним исходящим — это task, которого модель
        постыдилась: план с таким «шлюзом» обещанного ветвления не содержит, и
        гейт обязан это сказать, а не молчать ради наличия кружка."""
        raw = {"participants": ["ВкусВилл"],
               "elements": [
                   {"id": "A1", "kind": "userTask", "name": "Проверить",
                    "participant": "ВкусВилл"},
                   {"id": "G1", "kind": "exclusiveGateway", "name": "Хватает?",
                    "participant": "ВкусВилл"},
                   {"id": "A2", "kind": "userTask", "name": "Отгрузить",
                    "participant": "ВкусВилл"}],
               "flows": [{"id": "F1", "source": "A1", "target": "G1",
                          "kind": "sequence"},
                         {"id": "F2", "source": "G1", "target": "A2",
                          "kind": "sequence"}]}
        gaps = bpmn_generator.plan_gaps(raw, "Кладовщик проверяет заказ. Если "
                                             "товара не хватает, он заказывает "
                                             "остаток.")
        assert [g for g in gaps if "ни одного шлюза" in g and "раздваивает" in g]

    def test_plain_description_without_a_condition_is_not_blamed(self):
        raw = {"participants": ["ВкусВилл"],
               "elements": [{"id": "A1", "kind": "userTask", "name": "Отгрузить",
                             "participant": "ВкусВилл"}]}
        assert not [g for g in bpmn_generator.plan_gaps(
            raw, "Кладовщик собирает груз и отгружает его перевозчику.")
            if "ни одного шлюза" in g]

    def test_lost_branch_ranks_above_route_nonsense(self):
        """Ветвление — содержание: план, который меняет его на снятое
        предупреждение о потоке, лучше не стал."""
        assert bpmn_generator._gap_profile(
            ["в плане ни одного шлюза: развей маршрут"]) == (0, 1, 0)

    def test_actor_without_pool_or_lane_is_a_gap(self):
        """`actors` — выписка самой модели из описания: если действующее лицо в
        списке есть, а пула или дорожки нет, план противоречит себе."""
        raw = {"actors": ["ВкусВилл", "Поставщик"],
               "participants": ["ВкусВилл"],
               "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                             "participant": "ВкусВилл"}]}
        gaps = bpmn_generator.plan_gaps(raw, "ВкусВилл заводит заявку, поставщик "
                                             "подтверждает отгрузку.")
        assert [g for g in gaps if "Поставщик" in g and "действующим лицом" in g]

    def test_actor_gap_is_asked_first(self):
        """Переспрос один: потерянный участник обязан стоять в списке нарушений
        раньше подписей имён, иначе модель доходит только до подписей."""
        raw = {"actors": ["Поставщик"],
               "participants": ["Что-то не из текста"],
               "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                             "participant": "Что-то не из текста"}]}
        gaps = bpmn_generator.plan_gaps(raw, "Поставщик подтверждает отгрузку.")
        assert len(gaps) > 1 and "действующим лицом" in gaps[0]

    def test_actor_covered_by_a_lane_is_not_a_gap(self):
        """Дорожка «HR-партнёр» закрывает действующее лицо «HR»: требовать от
        модели отдельный пул для роли — значит толкать её к неверной схеме."""
        raw = {"actors": ["HR"], "participants": ["ВкусВилл"],
               "lanes": [{"id": "L1", "name": "HR-партнёр",
                          "participant": "ВкусВилл"}],
               "elements": [{"id": "A1", "kind": "userTask", "name": "Оформить",
                             "participant": "ВкусВилл", "lane": "L1"}]}
        assert not [g for g in bpmn_generator.plan_gaps(raw, "HR оформляет доступ.")
                    if "действующим лицом" in g]

    def test_actor_gap_alone_does_not_rewrite_the_plan(self, monkeypatch):
        """Ради расхождения с `actors` план целиком не переписывают: это чинит
        узкий вопрос о принадлежности шагов, а переспрос планом стоит полную
        генерацию и живые прогоны показывали на нём потерянные шаги.

        Сам вопрос при этом обязателен: молчание контура, который знал о
        противоречии и не спросил никого, стоило прогону #47 двух названных
        участников из 24 схем (`expected_participants`)."""
        sloppy = {"actors": ["ВкусВилл", "Поставщик"],
                  "participants": ["ВкусВилл"],
                  "elements": [{"id": f"A{i}", "kind": "userTask",
                                "name": f"Шаг {i}", "participant": "ВкусВилл"}
                               for i in range(1, 7)], "flows": []}
        fenced = _fence(sloppy)
        fake = FakeLLM(monkeypatch, fenced, '{"moves": [], "roles": [], '
                                            '"missing": []}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку, согласует её, собирает груз, отгружает, "
            "закрывает заявку. Поставщик подтверждает отгрузку.")
        assert result["attempts"] == 1, result["notes"]
        assert len(fake.calls) == 2, fake.calls
        assert "Верни moves, roles и missing" in fake.calls[-1][1]["content"]
        # Второй раз план не просили — содержание осталось тем, что модель
        # записала сама: ни один шаг не вырезан «ради соблюдения `actors`».
        assert {f"A{i}" for i in range(1, 7)} <= {e["id"] for e in
                                                  result["structure"]["elements"]}
        assert [g for g in result["gaps"] if "Поставщик" in g]

    def test_actor_gap_is_reported_until_the_model_fixes_it(self, monkeypatch):
        """Отсутствие участника на схеме остаётся нарушением в отчёте: узкий
        вопрос молчал — значит пользователь вправе увидеть, что план неполный."""
        sloppy = {"actors": ["ВкусВилл", "Поставщик"],
                  "participants": ["ВкусВилл"],
                  "elements": [{"id": "A1", "kind": "userTask", "name": "Заявка",
                                "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(sloppy)
        FakeLLM(monkeypatch, fenced, '{"moves": [], "roles": [], "missing": []}')
        result = BPMNGenerator().generate(
            "ВкусВилл заводит заявку. Поставщик подтверждает отгрузку.")
        assert [g for g in result["gaps"] if "Поставщик" in g]

    def test_plan_of_three_named_organizations_means_no_extra_call(self, monkeypatch):
        """Участники размечены, ролей-кандидатов нет: второй вызов не нужен —
        лишняя итерация стоит пользователю полную задержку генерации."""
        plan = {"participants": ["ВкусВилл", "Поставщик", "Банк"],
                "lanes": [{"id": "L1", "name": "Менеджер",
                           "participant": "ВкусВилл"},
                          {"id": "L2", "name": "Логист", "participant": "Поставщик"},
                          {"id": "L3", "name": "Инспектор", "participant": "Банк"}],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Завести заявку",
                     "participant": "ВкусВилл", "lane": "L1"},
                    {"id": "A2", "kind": "userTask", "name": "Подтвердить отгрузку",
                     "participant": "Поставщик", "lane": "L2"},
                    {"id": "A3", "kind": "userTask", "name": "Проверить платёж",
                     "participant": "Банк", "lane": "L3"}], "flows": []}
        fake = FakeLLM(monkeypatch, _fence(plan))
        BPMNGenerator().generate(
            "ВкусВилл заводит заявку, поставщик подтверждает отгрузку, банк "
            "проверяет платёж.")
        assert len(fake.calls) == 1

    def test_unavailable_model_still_rescues_the_named_participant(self, monkeypatch):
        """Сбой уточнения не вправе стоить участника из описания: шаг, в имени
        которого назван пустой пул, переносится без ответа модели."""
        plan = _fence(self._plan())
        FakeLLM(monkeypatch, plan, plan,
                llm_client.LLMError("модель недоступна"))
        result = BPMNGenerator().generate(self.TEXT)
        assert result["status"] == "success"
        assert _note(result["notes"], "Уточнение принадлежности не выполнено")
        assert _note(result["notes"], "назван в его имени")
        moved = next(e for e in result["structure"]["elements"] if e["id"] == "A2")
        assert moved["participant"] == "Поставщик"
        assert "Поставщик" in [p["name"] for p in result["structure"]["participants"]]

    def test_several_candidates_are_left_to_the_model(self, monkeypatch):
        """Два шага с именем пула в названии — прочтение не однозначное: без
        ответа модели ничего не переносим."""
        plan = self._plan()
        plan["elements"].append(
            {"id": "A3", "kind": "userTask", "name": "Проверить отгрузку поставщика",
             "participant": "ВкусВилл"})
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced, llm_client.LLMError("сбой"))
        result = BPMNGenerator().generate(self.TEXT)
        assert not _note(result["notes"], "назван в его имени")
        for elem_id in ("A2", "A3"):
            element = next(e for e in result["structure"]["elements"]
                           if e["id"] == elem_id)
            assert element["participant"] == "ВкусВилл"

    def test_rescue_skips_a_pool_the_model_declared_a_role(self):
        """Роль (`inside`) наполнять шагом нельзя: её шаги починка положит в
        дорожку организации, а не в отдельный пул."""
        structure = {"participants": [{"name": "Кладовщик", "external": False,
                                       "inside": "Склад"}, {"name": "Склад"}],
                     "elements": [
                         {"id": "A1", "kind": "userTask", "name": "Проверить заявку",
                          "participant": "Склад"},
                         {"id": "A2", "kind": "userTask",
                          "name": "Сборка груза кладовщиком",
                          "participant": "Склад"}], "flows": []}
        notes: list = []
        bpmn_generator._claim_steps_by_name(
            structure, "Кладовщик собирает груз на складе.", notes)
        assert notes == []
        assert structure["elements"][1]["participant"] == "Склад"

    def test_rescue_refuses_to_empty_the_donor_pool(self, monkeypatch):
        """Перенос не должен делать из организации ещё один пустой пул."""
        plan = {"participants": ["ВкусВилл", "Поставщик"],
                "elements": [
                    {"id": "A2", "kind": "userTask",
                     "name": "Получить подтверждение отгрузки от поставщика",
                     "participant": "ВкусВилл"}], "flows": []}
        fenced = _fence(plan)
        FakeLLM(monkeypatch, fenced, fenced, llm_client.LLMError("сбой"))
        result = BPMNGenerator().generate("ВкусВилл ждёт подтверждения от поставщика.")
        assert _note(result["notes"], "остался без действий")
        assert "Поставщик" not in [
            p["name"] for p in result["structure"]["participants"]]

    @staticmethod
    def _role_plan():
        """Кладовщик и бухгалтерия — пулы с шагом и без чужих дорожек (кандидаты
        в роли), ВкусВилл — организация со своими дорожками: её спрашивать
        незачем, перевозчик — самостоятельный участник."""
        return {"participants": ["ВкусВилл", "Кладовщик", "Бухгалтерия",
                                 "Перевозчик"],
                "lanes": [{"id": "L_m", "name": "Менеджер закупок",
                           "participant": "ВкусВилл"},
                          {"id": "L_k", "name": "Кладовщик",
                           "participant": "Кладовщик"},
                          {"id": "L_b", "name": "Бухгалтерия",
                           "participant": "Бухгалтерия"}],
                "elements": [
                    {"id": "A0", "kind": "userTask", "name": "Согласовать заказ",
                     "participant": "ВкусВилл", "lane": "L_m"},
                    {"id": "A1", "kind": "userTask", "name": "Собрать заказ",
                     "participant": "Кладовщик", "lane": "L_k"},
                    {"id": "A2", "kind": "userTask", "name": "Провести возврат",
                     "participant": "Бухгалтерия", "lane": "L_b"},
                    {"id": "A3", "kind": "task", "name": "Принять груз",
                     "participant": "Перевозчик"},
                ], "flows": []}

    ROLE_TEXT = ("ВкусВилл согласует заказ: менеджер сверяет цены, кладовщик "
                 "комплектует паллету, бухгалтерия сверяет возврат в "
                 "отчётности, перевозчик принимает груз.")

    def test_declared_role_becomes_a_lane_instead_of_a_pool(self, monkeypatch):
        plan = _fence(self._role_plan())
        fake = FakeLLM(monkeypatch, plan, plan,
                       '{"roles": [{"pool": "Кладовщик", "inside": "ВкусВилл"},'
                       ' {"pool": "Бухгалтерия", "inside": "ВкусВилл"}]}')
        result = BPMNGenerator().generate(self.ROLE_TEXT)
        assert _note(result["notes"], "роль «ВкусВилл» по описанию")
        assert sorted(p["name"] for p in result["structure"]["participants"]) == \
            ["ВкусВилл", "Перевозчик"]
        assert sorted(l["name"] for l in result["structure"]["lanes"]) == \
            ["Бухгалтерия", "Кладовщик", "Менеджер закупок"]
        assert "Кладовщик" in fake.calls[2][1]["content"]

    def test_role_pointing_at_an_absent_organization_is_refused(self, monkeypatch):
        plan = _fence(self._role_plan())
        fake = FakeLLM(monkeypatch, plan, plan,
                       '{"roles": [{"pool": "Кладовщик", "inside": "Магазин 17"}]}')
        result = BPMNGenerator().generate(self.ROLE_TEXT)
        assert _note(result["notes"], "в описании нет")
        assert "Кладовщик" in [p["name"] for p in result["structure"]["participants"]]

    def test_empty_organization_is_refused_instead_of_folding_somewhere(self,
                                                                       monkeypatch):
        plan = _fence(self._role_plan())
        fake = FakeLLM(monkeypatch, plan, plan,
                       '{"roles": [{"pool": "Кладовщик", "inside": ""}]}')
        result = BPMNGenerator().generate(self.ROLE_TEXT)
        assert _note(result["notes"], "модель не назвала организацию")
        assert "Кладовщик" in [p["name"] for p in result["structure"]["participants"]]

    def test_organization_named_in_the_text_but_absent_from_the_plan_is_created(
            self, monkeypatch):
        """Роли есть, а организации-приёмника в плане нет: без неё роли и остаются
        пулами. Приёмник заводим по слову модели, но только если та название
        действительно стоит в описании."""
        plan = {"participants": ["Кладовщик", "Бухгалтерия"],
                "lanes": [{"id": "L_k", "name": "Кладовщик",
                           "participant": "Кладовщик"},
                          {"id": "L_b", "name": "Бухгалтерия",
                           "participant": "Бухгалтерия"}],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Собрать заказ",
                     "participant": "Кладовщик", "lane": "L_k"},
                    {"id": "A2", "kind": "userTask", "name": "Сверить возврат",
                     "participant": "Бухгалтерия", "lane": "L_b"},
                ], "flows": []}
        text = ("ВкусВилл собирает заказ: кладовщик комплектует паллету, "
                "бухгалтерия сверяет возврат в отчётности.")
        canned = _fence(plan)
        fake = FakeLLM(monkeypatch, canned, canned,
                       '{"roles": [{"pool": "Кладовщик", "inside": "ВкусВилл"},'
                       ' {"pool": "Бухгалтерия", "inside": "ВкусВилл"}]}')
        result = BPMNGenerator().generate(text)
        assert [p["name"] for p in result["structure"]["participants"]] == \
            ["ВкусВилл"]
        assert sorted(l["name"] for l in result["structure"]["lanes"]) == \
            ["Бухгалтерия", "Кладовщик"]

    def test_vacant_pool_is_asked_about_even_without_name_matches(self, monkeypatch):
        """Названия шагов редко повторяют имя участника («Зарегистрировать
        сигнал» вместо «система мониторинга …»): спрашивать всё равно нужно —
        иначе пустой пул молча удаляется вместе с названным участником."""
        plan = {"participants": ["Система мониторинга", "Дежурный инженер"],
                "elements": [
                    {"id": "A1", "kind": "userTask", "name": "Зарегистрировать сигнал",
                     "participant": "Дежурный инженер"},
                    {"id": "A2", "kind": "userTask", "name": "Разобрать инцидент",
                     "participant": "Дежурный инженер"},
                ], "flows": []}
        text = ("Система мониторинга ловит сбой и регистрирует сигнал, дежурный "
                "инженер разбирает инцидент.")
        canned = _fence(plan)
        fake = FakeLLM(monkeypatch, canned, canned,
                       '{"moves": [{"element": "A1", '
                       '"participant": "Система мониторинга"}]}')
        result = BPMNGenerator().generate(text)
        assert "выбери шаги по смыслу" in fake.calls[2][1]["content"]
        assert _note(result["notes"], "перенесён в пул «Система мониторинга»")
        pools = [p["name"] for p in result["structure"]["participants"]]
        assert "Система мониторинга" in pools

    def test_lane_name_that_looks_like_an_id_is_not_a_role_signal(self):
        """«HR» уехал в «Руководство подразделения», потому что у того была
        дорожка с именем `L_hr`: имя-идентификатор не рассказывает, чья это
        дорожка, и слияние по нему разбирает схему на части."""
        raw = {"participants": ["HR", "Руководство подразделения"],
               "lanes": [{"id": "L_hr", "name": "L_hr",
                          "participant": "Руководство подразделения"}],
               "elements": [
                   {"id": "A1", "kind": "userTask", "name": "Завести доступ",
                    "participant": "HR"},
                   {"id": "A2", "kind": "userTask", "name": "Согласовать",
                    "participant": "Руководство подразделения"},
               ], "flows": []}
        repaired, notes = repair_structure(raw)
        assert sorted(p["name"] for p in repaired["participants"]) == \
            ["HR", "Руководство подразделения"]
        assert not [n for n in notes if "слит в" in n]

    def test_lane_named_exactly_like_the_pool_is_a_role_signal(self):
        """Латинская аббревиатура — настоящее имя дорожки: точное совпадение
        складывается в роль, запрет на идентификаторы его не касается."""
        raw = {"participants": ["HR", "ВкусВилл"],
               "lanes": [{"id": "L1", "name": "HR", "participant": "ВкусВилл"}],
               "elements": [
                   {"id": "A1", "kind": "userTask", "name": "Оформить доступ",
                    "participant": "HR"},
                   {"id": "A2", "kind": "userTask", "name": "Согласовать",
                    "participant": "ВкусВилл"}], "flows": []}
        repaired, notes = repair_structure(raw)
        assert [p["name"] for p in repaired["participants"]] == ["ВкусВилл"]
        assert _note(notes, "слит в")
        assert all(e["participant"] == "ВкусВилл" for e in repaired["elements"])

    def test_organization_with_its_own_lanes_is_not_a_candidate(self, monkeypatch):
        plan = _fence(self._role_plan())
        fake = FakeLLM(monkeypatch, plan, plan, '{"roles": []}')
        result = BPMNGenerator().generate(self.ROLE_TEXT)
        question = fake.calls[2][1]["content"]
        block = question.split("могут быть ролью")[1].split("Все пулы")[0]
        # «ВкусВилл» сам объявил дорожки других имён — это организация
        assert "ВкусВилл" not in block
        assert "Кладовщик" in block and "Бухгалтерия" in block
        assert result["status"] == "success"


class TestDeadlineTimerGate:
    """Срок, названный в описании, в BPMN — это таймер. Починка его не ставит:
    куда вести ветку по истечении, знает только модель, поэтому нарушение
    уходит в переспрос, а не «дорисовывается» эвристикой под метрику."""

    TEXT = ("Поддержка принимает обращение и ждёт ответа клиента: если он не "
            "ответил в течение четырёх часов, обращение эскалируется "
            "руководителю смены.")

    @staticmethod
    def _plan(*extra_elements):
        return {
            "participants": ["Поддержка"],
            "elements": [{"id": "T1", "kind": "userTask", "name": "Ждём ответ",
                          "participant": "Поддержка"}, *extra_elements],
            "flows": [],
        }

    @staticmethod
    def _timer(**over):
        elem = {"id": "B1", "kind": "boundaryEvent", "name": "Прошло 4 часа",
                "participant": "Поддержка", "attached_to": "T1",
                "event_definition": "timer", "timer": "PT4H"}
        elem.update(over)
        return elem

    def test_deadline_without_a_timer_is_a_gap(self):
        gaps = bpmn_generator.plan_gaps(self._plan(), self.TEXT)
        assert any("в течение четырёх часов" in g and "таймер" in g for g in gaps)

    def test_boundary_timer_closes_the_gate(self):
        gaps = bpmn_generator.plan_gaps(self._plan(self._timer()), self.TEXT)
        assert not [g for g in gaps if "таймер" in g and "ни одного" in g]

    def test_intermediate_timer_closes_the_gate(self):
        gaps = bpmn_generator.plan_gaps(
            self._plan(self._timer(kind="intermediateCatchEvent",
                                   attached_to="")), self.TEXT)
        assert not [g for g in gaps if "таймер" in g and "ни одного" in g]

    def test_existing_timer_is_not_demanded_again(self):
        """С хронометражем неразбериха — пусть ругается только правило формата:
        требовать таймер, который в плане есть, значит врать про план."""
        gaps = bpmn_generator.plan_gaps(
            self._plan(self._timer(timer="четыре часа")), self.TEXT)
        assert any("ISO-8601" in g for g in gaps)
        assert not [g for g in gaps if "таймер" in g and "ни одного" in g]

    def test_description_without_a_stated_deadline_is_silent(self):
        gaps = bpmn_generator.plan_gaps(
            self._plan(), "Поддержка принимает обращение и закрывает его.")
        assert not [g for g in gaps if "таймер" in g]

    def test_quality_window_is_not_a_wait(self):
        """«Не больше 14 дней» — условие признания брака, а не ожидание:
        заводить по нему таймер значит учить модель рисовать лишнее."""
        gaps = bpmn_generator.plan_gaps(
            self._plan(), "Контролёр проверяет товар: если прошло не больше "
                          "14 дней, возврат проводится.")
        assert not [g for g in gaps if "таймер" in g]

    def test_deadline_gap_reaches_the_re_ask(self, monkeypatch):
        fake = FakeLLM(monkeypatch, _fence(self._plan()),
                       _fence(self._plan(self._timer())))
        result = BPMNGenerator().generate(self.TEXT)
        assert result["attempts"] == 2
        assert "ни одного таймера" in fake.calls[1][1]["content"]
        assert _note(result["notes"], "Повторный запрос модели: нарушений было")


class TestPlanPatch:
    """Переспрос чинит названное и не трогает верное."""

    BASE = {
        "participants": ["Дежурство"],
        "lanes": [{"id": "L1", "name": "Инженер", "participant": "Дежурство"}],
        "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Авария",
             "participant": "Дежурство", "lane": "L1"},
            {"id": "A1", "kind": "userTask", "name": "Осмотреть узел",
             "participant": "Дежурство", "lane": "L1"},
            {"id": "E1", "kind": "endEvent", "name": "Линия в работе",
             "participant": "Дежурство", "lane": "L1"}],
        "flows": [{"id": "F1", "source": "S1", "target": "A1"},
                  {"id": "F2", "source": "A1", "target": "E1"}]}

    def _patch(self, patch, gaps=(), text="Дежурство осматривает узел, "
                                            "подрядчик чинит линию."):
        notes: list = []
        base = copy.deepcopy(self.BASE)
        return bpmn_generator.apply_plan_patch(base, patch, list(gaps), text,
                                               notes), notes

    def test_untouched_elements_survive_the_retry_byte_for_byte(self):
        """Ни один верный элемент не меняется: заплатка физически не может
        переписать то, чего не касается."""
        patched, notes = self._patch(
            {"add_elements": [{"id": "B1", "kind": "boundaryEvent",
                               "name": "Прошло 15 минут",
                               "participant": "Дежурство", "lane": "L1",
                               "attached_to": "A1",
                               "event_definition": "timer", "timer": "PT15M"}],
             "add_flows": [{"id": "F9", "source": "B1", "target": "E1"}]},
            gaps=['описание задаёт ожидание («в течение 15 минут»), а в плане '
                  'нет ни одного таймера'])
        before = {e["id"]: e for e in self.BASE["elements"]}
        after = {e["id"]: e for e in patched["elements"]}
        for elem_id in before:
            assert after[elem_id] == before[elem_id]
        assert after["B1"]["event_definition"] == "timer"
        assert [f["id"] for f in patched["flows"]] == ["F1", "F2", "F9"]
        assert any("добавлен элемент B1" in n for n in notes)

    def test_the_retry_prompt_forbids_rewriting_a_step_into_an_event(self):
        """Живые отказы #48: на нарушение «в плане нет таймера» модель отвечает
        `fixes` с `event_definition`/`timer`/`attached_to` поверх существующего
        userTask. Такой элемент остаётся шагом с полями события — нарушение не
        закрыто, гейт честно отказывает, и единственная переспросная попытка
        потрачена. Правило названо в промпте явно."""
        template = " ".join(bpmn_generator._RETRY_TEMPLATE.split())
        assert "kind=boundaryEvent" in template
        assert "Превращать шаг маршрута в граничное событие" in template

    def test_the_trace_names_what_the_patch_offered(self):
        """Отказ «нарушений меньше не стало» — это три разных случая: модель
        молчала, чинила не то нарушение, или назвала участника без его шагов.
        Счёт нарушений их не различает, и каждый разбор стоил отдельного живого
        прогона (#46: 11 из 18 отказов именно с этим счётом)."""
        digest = bpmn_generator._patch_digest({
            "fixes": [{"id": "A1", "participant": "Покупатель"}],
            "add_elements": [{"id": "B1", "kind": "boundaryEvent",
                              "participant": "Дежурство"}],
            "add_flows": [{"id": "F9", "source": "B1", "target": "E1"}],
            "remove_flows": ["F3"],
            "participants": [{"name": "Покупатель"}],
            "lanes": [{"id": "L9", "name": "Кассир",
                       "participant": "ВкусВилл"}]})["patch"]
        assert digest["fixes"] == ["A1:participant"]
        assert digest["add_elements"] == ["B1 boundaryEvent@Дежурство"]
        assert digest["add_flows"] == ["F9 B1->E1"]
        assert digest["remove_flows"] == ["F3"]
        assert digest["participants"] == ["Покупатель шагов:нет"]
        assert digest["lanes"] == ["Кассир@ВкусВилл"]

    def test_a_whole_plan_answer_is_digested_as_such(self):
        """Целый план — не заплатка, и в трейсе он читается как отказ от
        контракта, а не как пустой список правок."""
        assert bpmn_generator._patch_digest({"participants": [],
                                            "elements": []}) == {
            "patch": "план целиком вместо заплатки"}

    def test_a_new_participant_may_be_named_by_his_own_steps(self):
        """`steps` — способ назвать шаги нового участника, а не единственный:
        заплатка называет их хозяином прямо на элементе. Отказ по форме ответа
        терял контрагента, которого нарушение и требовало вернуть."""
        patched, notes = self._patch(
            {"add_elements": [{"id": "P1", "kind": "userTask",
                               "name": "Вернуть деньги",
                               "participant": "Покупатель"}],
             "participants": [{"name": "Покупатель"}]},
            gaps=['ты сама назвала «Покупатель» действующим лицом описания, но '
                  "в плане нет ни пула, ни дорожки с таким именем"],
            text="Покупатель приносит товар, кассир возвращает деньги.")
        assert "Покупатель" in [bpmn_generator._pool_name(p)
                                for p in patched["participants"]]
        assert any("шаги заплатка уже записала за ним" in n for n in notes), notes

    def test_fix_for_an_element_that_is_not_violated_is_still_id_checked(self):
        """Неизвестный id правкой не становится: править можно только то, что
        есть в плане (а что именно нельзя — подсказывает список нарушений)."""
        patched, notes = self._patch(
            {"fixes": [{"id": "X9", "name": "Совсем другое"}]})
        assert [e["id"] for e in patched["elements"]] == ["S1", "A1", "E1"]
        assert any("такого элемента в плане нет" in n for n in notes)

    def test_flow_to_an_unknown_node_is_not_added(self):
        patched, notes = self._patch(
            {"add_flows": [{"id": "F9", "source": "A1", "target": "Z1"}]})
        assert [f["id"] for f in patched["flows"]] == ["F1", "F2"]
        assert any("узла с таким id в плане нет" in n for n in notes)

    def test_composition_is_not_touched_without_a_participant_gap(self):
        """Состав — решение отдельной стадии, и переспрос его не перерисует:
        списки участников принимаются только когда нарушение про участника."""
        patched, notes = self._patch(
            {"participants": ["Подрядчик"],
             "fixes": [{"id": "A1", "name": "Осмотреть и починить узел"}]})
        assert patched["participants"] == self.BASE["participants"]
        assert any("состав не изменён" in n for n in notes)
        # а вот названная правка дошла:
        step = next(e for e in patched["elements"] if e["id"] == "A1")
        assert step["name"] == "Осмотреть и починить узел"

    def test_participant_gap_opens_the_composition_with_its_steps(self):
        """Закрыть нарушение про участника можно только парой «пул + его шаги»:
        голое имя починка удалит вместе с пустым пулом.»"""
        patched, notes = self._patch(
            {"participants": [{"name": "Подрядчик", "external": True,
                               "steps": ["A1"]}]},
            gaps=['ты сама назвала «Подрядчик» действующим лицом описания, '
                  "но в плане нет ни пула, ни дорожки с таким именем"])
        assert patched["participants"][-1] == {"name": "Подрядчик"}
        step = next(e for e in patched["elements"] if e["id"] == "A1")
        assert step["participant"] == "Подрядчик"
        assert any("добавлен с 1 его шагом" in n for n in notes)

    def test_added_participant_without_steps_is_not_added(self):
        """Пул без действий не доживает до схемы, поэтому и в заплатке он
        имеет смысл только вместе с шагами."""
        patched, notes = self._patch(
            {"participants": [{"name": "Подрядчик", "steps": []}]},
            gaps=['ты сама назвала «Подрядчик» действующим лицом описания, '
                  "но в плане нет ни пула, ни дорожки с таким именем"])
        assert [p for p in patched["participants"]] == ["Дежурство"]
        assert any("его шаги в заплатке не названы" in n for n in notes)

    def test_added_participant_absent_from_the_description_is_refused(self):
        patched, notes = self._patch(
            {"participants": [{"name": "Робот-курьер", "steps": ["A1"]}]},
            gaps=["участник «Робот-курьер» помечен ролью (external=false)"])
        assert [p for p in patched["participants"]] == ["Дежурство"]
        assert any("в описании такого имени нет" in n for n in notes)

    def test_flow_removal_needs_a_gap_that_names_the_flow(self):
        """Переспрос не вправе сокращать маршрут «просто так»: срезанное плечо
        — это уже пропавшая развилка, а не починка."""
        patched, notes = self._patch({"remove_flows": ["F2"]})
        assert [f["id"] for f in patched["flows"]] == ["F1", "F2"]
        assert any("нарушения их не называют" in n for n in notes)

        named, notes2 = self._patch(
            {"remove_flows": ["F2"]},
            gaps=["поток F2 (A1 → E1) противоречит правилу: у шлюза нет плеча"])
        assert [f["id"] for f in named["flows"]] == ["F1"]
        assert any("убрано потоков: 1" in n for n in notes2)

    def test_a_full_plan_answer_is_still_accepted_as_a_replacement(self):
        """Модель ответит планом целиком и будет: это не отказ и не тихая
        поломка — замена плана, состав закрепит каркас."""
        whole = copy.deepcopy(self.BASE)
        whole["elements"].append({"id": "B1", "kind": "boundaryEvent",
                                  "name": "Просрочка"})
        patched, notes = self._patch(whole)
        assert patched is whole
        assert any("план целиком" in n for n in notes)


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

    def test_hidden_split_is_a_gap(self):
        """Задача, ведущая сразу в два шага, — ветвление, «спрятанное» в
        подписях потоков. Какой у развилки тип, знает только модель: починка не
        вправе выбирать между исключающей и параллельной."""
        plan = {"participants": ["ВкусВилл"], "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Заказ",
             "participant": "ВкусВилл"},
            {"id": "A1", "kind": "userTask", "name": "Собрать",
             "participant": "ВкусВилл"},
            {"id": "B1", "kind": "userTask", "name": "Проверить",
             "participant": "ВкусВилл"},
            {"id": "C1", "kind": "userTask", "name": "Упаковать",
             "participant": "ВкусВилл"},
            {"id": "E1", "kind": "endEvent", "name": "Готово",
             "participant": "ВкусВилл"},
        ], "flows": [
            {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence"},
            {"id": "F2", "source": "A1", "target": "B1", "kind": "sequence"},
            {"id": "F3", "source": "A1", "target": "C1", "kind": "sequence"},
            {"id": "F4", "source": "B1", "target": "E1", "kind": "sequence"},
            {"id": "F5", "source": "C1", "target": "E1", "kind": "sequence"},
        ]}
        gaps = bpmn_generator.plan_gaps(plan)
        assert any("A1" in g and "развилка спрятана" in g for g in gaps)

    def test_flow_to_boundary_event_is_reported_once(self):
        """Поток шага к прицепленному событию — нарушение концов, и его называет
        именно то правило: не надо удваивать его ещё и «спрятанным
        ветвлением», модель и так получает конкретную подсказку."""
        plan = {"participants": ["ВкусВилл"], "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Заказ",
             "participant": "ВкусВилл"},
            {"id": "T1", "kind": "userTask", "name": "Собрать",
             "participant": "ВкусВилл"},
            {"id": "B1", "kind": "boundaryEvent", "name": "Просрочка",
             "participant": "ВкусВилл", "attached_to": "T1",
             "event_definition": "timer", "timer": "PT4H"},
            {"id": "E1", "kind": "endEvent", "name": "Готово",
             "participant": "ВкусВилл"},
        ], "flows": [
            {"id": "F1", "source": "S1", "target": "T1", "kind": "sequence"},
            {"id": "F2", "source": "T1", "target": "E1", "kind": "sequence"},
            {"id": "F3", "source": "T1", "target": "B1", "kind": "sequence"},
        ]}
        gaps = bpmn_generator.plan_gaps(plan)
        assert any("F3" in g and "граничное событие" in g for g in gaps)
        assert not [g for g in gaps if "развилка спрятана" in g]

    def test_split_through_a_gateway_is_not_hidden(self):
        plan = {"participants": ["ВкусВилл"], "elements": [
            {"id": "S1", "kind": "startEvent", "name": "Заказ",
             "participant": "ВкусВилл"},
            {"id": "G1", "kind": "exclusiveGateway", "name": "Комплект?",
             "participant": "ВкусВилл"},
            {"id": "B1", "kind": "userTask", "name": "Проверить",
             "participant": "ВкусВилл"},
            {"id": "E1", "kind": "endEvent", "name": "Готово",
             "participant": "ВкусВилл"},
        ], "flows": [
            {"id": "F1", "source": "S1", "target": "G1", "kind": "sequence"},
            {"id": "F2", "source": "G1", "target": "B1", "kind": "sequence",
             "condition": "Да"},
            {"id": "F3", "source": "G1", "target": "E1", "kind": "sequence",
             "condition": "Нет"},
        ]}
        gaps = bpmn_generator.plan_gaps(plan)
        assert not [g for g in gaps if "развилка спрятана" in g]

    WAREHOUSE_TEXT = ("WMS выдаёт задание, кладовщик собирает паллету, "
                      "перевозчик отгружает заказ получателю.")

    @staticmethod
    def _plan_with_steps(pools):
        """По одному шагу на пул — чтобы проверка имён не тонула в других
        нарушениях плана."""
        return {
            "participants": list(pools),
            "elements": [{"id": f"A{i}", "kind": "userTask", "name": f"Шаг {i}",
                          "participant": pool} for i, pool in enumerate(pools)],
            "flows": [],
        }

    def test_participant_named_in_the_text_but_missing_is_a_gap(self):
        """Схема без названной в описании системы теряет взаимодействие — и
        починить это нельзя, шаги WMS знает только модель."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Склад", "Перевозчик", "Получатель"]),
            self.WAREHOUSE_TEXT)
        assert any("WMS" in g and "назван участник" in g for g in gaps)

    def test_renamed_participant_is_a_gap(self):
        """«WMS», записанный как «Склад», — переименованный участник: схема
        перестаёт сходиться с описанием."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Магазин", "WMS", "Перевозчик"]),
            self.WAREHOUSE_TEXT)
        assert any("Магазин" in g and "не упоминается" in g for g in gaps)

    def test_names_taken_from_the_text_pass_the_check(self):
        """Русская морфология не должна ловить ложные нарушения: «получателю»
        в тексте — это тот же «Получатель»."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["WMS", "Перевозчик", "Получатель"]),
            self.WAREHOUSE_TEXT)
        assert not [g for g in gaps
                    if "не упоминается" in g or "назван участник" in g]

    def test_short_inflected_name_is_found(self):
        """«Банк» в описании — «в отделении банка»: короткое имя склоняется так
        же, и требовать его ровно в именительном значит не находить на схеме."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Банк"]),
            "Клиент подаёт заявку в отделении банка, банк проверяет документы.")
        assert not [g for g in gaps if "Банк" in g and "не упоминается" in g]

    def test_service_abbreviations_are_not_demanded_as_participants(self):
        """SLA — атрибут процесса, а не участник: требовать его пулом значит
        учить модель рисовать лишние схемы."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Склад"]),
            "Склад собирает заказ в рамках SLA по регламенту КБ.")
        assert not [g for g in gaps if "SLA" in g or "КБ" in g]

    def test_composite_name_is_found_by_every_part(self):
        """«Бюджетный контролёр» в тексте есть целиком, и требовать переименовать
        пул — ложное нарушение: составное имя ищется по всем словам."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Бюджетный контролёр"]),
            "Бюджетный контролёр сверяет расход со сметой и возвращает заявку.")
        assert not [g for g in gaps if "не упоминается" in g]

    def test_name_soldered_from_two_sentences_is_a_gap(self):
        """«Подразделение-заявитель» так в описании не называют: имя собрано из
        двух слов разных предложений, и прослеживать по нему участника нечем."""
        gaps = bpmn_generator.plan_gaps(
            self._plan_with_steps(["Подразделение-заявитель"]),
            "Инициатор подразделения заводит заявку и прикладывает обоснование.")
        assert any("Подразделение-заявитель" in g and "не упоминается" in g
                   for g in gaps)

    def test_vacant_pool_named_in_the_text_forbids_erasure(self):
        """Пустой пул названного участника — это его шаги, записанные чужим
        пулом: модель обязана вернуть их, а не стереть участника со схемы."""
        raw = {"participants": ["ВкусВилл", "Поставщик"],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Заявка",
                             "participant": "ВкусВилл"}], "flows": []}
        gaps = bpmn_generator.plan_gaps(
            raw, "ВкусВилл заводит заявку, поставщик подтверждает отгрузку.")
        assert any("Поставщик" in g and "убирать его нельзя" in g for g in gaps)

    def test_vacant_pool_absent_from_the_text_may_be_dropped(self):
        raw = {"participants": ["ВкусВилл", "Аналитика"],
               "elements": [{"id": "T1", "kind": "userTask", "name": "Заявка",
                             "participant": "ВкусВилл"}], "flows": []}
        gaps = bpmn_generator.plan_gaps(raw, "ВкусВилл заводит заявку.")
        assert any("Аналитика" in g and "такого участника нет" in g for g in gaps)

    def test_vacant_pool_names_the_steps_that_look_like_its_own(self):
        """Модели мало сказать «пустой пул» — в живом прогоне она на это удаляла
        участника. Список шагов, названных по имени этого участника, даёт ей
        конкретную зацепку; решает по-прежнему она, а не эвристика."""
        raw = {"participants": ["ВкусВилл", "Поставщик"],
               "elements": [
                   {"id": "T1", "kind": "userTask", "name": "Заявка",
                    "participant": "ВкусВилл"},
                   {"id": "T2", "kind": "userTask",
                    "name": "Получить подтверждение отгрузки от поставщика",
                    "participant": "ВкусВилл"},
               ], "flows": []}
        gaps = bpmn_generator.plan_gaps(
            raw, "ВкусВилл заводит заявку, поставщик подтверждает отгрузку.")
        hint = [g for g in gaps
                if "Поставщик" in g and "убирать его нельзя" in g]
        assert hint and "T2" in hint[0]

    def test_pool_that_receives_merged_steps_is_not_called_vacant(self):
        """«Дежурный инженер платёжного шлюза» пуст в плане, пока другой пул не
        объявил его дорожкой: починка перенесёт его шаги туда, и требовать от
        модели «верни действия в свой пул» — шум, а не нарушение."""
        raw = {
            "participants": ["Дежурный инженер",
                             "Дежурный инженер платёжного шлюза"],
            "lanes": [{"id": "L_dp", "name": "Дежурный инженер платёжного шлюза",
                       "participant": "Дежурный инженер платёжного шлюза"}],
            "elements": [{"id": "T1", "kind": "userTask", "name": "Классификация",
                          "participant": "Дежурный инженер"}],
            "flows": [],
        }
        gaps = bpmn_generator.plan_gaps(raw)
        assert not [g for g in gaps
                    if "платёжного шлюза" in g and "без единого шага" in g]
        assert not [g for g in gaps if "дорожку с таким же именем" in g]
        repaired, notes = repair_structure(raw)
        assert [p["name"] for p in repaired["participants"]] == [
            "Дежурный инженер платёжного шлюза"]
        assert any("слит в" in n for n in notes)

    def test_without_the_text_the_naming_rules_stay_silent(self):
        assert not [g for g in bpmn_generator.plan_gaps(
            self._plan_with_steps(["Магазин"])) if "не упоминается" in g]

    def test_gaps_survive_junk_and_non_objects(self):
        assert bpmn_generator.plan_gaps("не объект") == ["план не является JSON-объектом"]
        assert isinstance(bpmn_generator.plan_gaps({"elements": [1, None, "x"]}),
                          list)

    def test_retry_that_fixes_rules_by_losing_steps_is_rejected(self, monkeypatch):
        """«Правильный» план без содержания лучше не становится: переспрос
        обязан снимать нарушения, а не вырезать шаги, чтобы не за что цепляться."""
        rich = {
            "participants": ["ВкусВилл", "Кладовщик"],
            "lanes": [{"id": "L1", "name": "Кладовщик", "participant": "ВкусВилл"}],
            "elements": [
                {"id": "T1", "kind": "userTask", "name": "Заявка",
                 "participant": "ВкусВилл"},
                {"id": "T2", "kind": "userTask", "name": "Проверка",
                 "participant": "ВкусВилл", "lane": "L1"},
                {"id": "T3", "kind": "userTask", "name": "Отгрузка",
                 "participant": "Кладовщик"},
            ],
            "flows": [],
        }
        thin = {"participants": ["ВкусВилл"],
                "elements": [{"id": "T1", "kind": "userTask", "name": "Заявка",
                              "participant": "ВкусВилл"}], "flows": []}
        assert bpmn_generator.plan_gaps(rich) and not bpmn_generator.plan_gaps(thin)
        fake = FakeLLM(monkeypatch, _fence(rich), _fence(thin))
        result = BPMNGenerator().generate("ВкусВилл: заявка, согласование, "
                                          "отгрузка со склада")
        assert result["attempts"] == 2
        assert _note(result["notes"], "но план потерял")
        assert bpmn_generator._plan_content(result["structure"]) == 3

    def test_the_question_offers_a_vacant_pool_as_the_role_host(self, monkeypatch):
        """Действия контрагента модель записывает за его же ролью: в прогоне #50
        `warehouse_delivery` пул «Перевозчик» остался без шагов, а его шаги ушли в
        пул «Водитель» — отсюда сразу два падения (`expected_participants` и
        `roles_as_lanes`), один дефект. Вопрос о принадлежности обязан показать
        пустой пул как возможного хозяина роли: связь «Водитель → Перевозчик»
        знает описание, а не список должностей в коде."""
        roster = ('{"organizations": ["Склад"], "systems": [], '
                  '"counterparties": ["Перевозчик", "Водитель"], "roles": []}')
        plan = {"participants": ["Склад", "Перевозчик", "Водитель"],
                "actors": ["Склад", "Перевозчик", "Водитель"],
                "lanes": [],
                "elements": [
                    {"id": "S1", "kind": "startEvent", "name": "Заявка",
                     "participant": "Склад"},
                    {"id": "A1", "kind": "userTask", "name": "Отобрать груз",
                     "participant": "Склад"},
                    {"id": "P1", "kind": "startEvent", "name": "Старт",
                     "participant": "Перевозчик"},
                    {"id": "P2", "kind": "endEvent", "name": "Финиш",
                     "participant": "Перевозчик"},
                    {"id": "D1", "kind": "startEvent", "name": "Наряд",
                     "participant": "Водитель"},
                    {"id": "D2", "kind": "userTask", "name": "Привезти груз",
                     "participant": "Водитель"},
                    {"id": "D3", "kind": "endEvent", "name": "Доставлено",
                     "participant": "Водитель"}],
                "flows": []}
        fake = FakeLLM(monkeypatch, roster, _fence(plan),
                       '{"moves": [], "roles": [], "missing": []}')
        BPMNGenerator().generate(
            "Склад собирает заказ. Перевозчик получает заявку, водитель "
            "доставляет груз со склада.")
        question = next(c[1]["content"] for c in fake.calls
                        if "Пулы, которые могут быть ролью" in c[1]["content"])
        role_line = question.split("Пулы, которые могут быть ролью", 1)[1]
        role_line = role_line.split("\n\n", 1)[0]
        assert "Водитель" in role_line
        assert "Перевозчик" in role_line, "пустой пул не предложен хозяином роли"

    def test_clean_plan_is_not_asked_twice(self, monkeypatch):
        fake = FakeLLM(monkeypatch, _plan())
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert result["attempts"] == 1 and len(fake.calls) == 1

    def test_retry_carries_violations_and_better_plan_wins(self, monkeypatch):
        bad = TestVacantPools._vacant()
        fake = FakeLLM(monkeypatch, bad, _plan())
        result = BPMNGenerator().generate("ВкусВилл: заявка, согласование, "
                                             "отгрузка со склада")
        assert result["attempts"] == 2 and len(fake.calls) == 2
        # Второй вызов идёт с теми же правилами моделирования, что и вызов,
        # строивший план: состав здесь не закреплялся (модель на узкий вопрос
        # ответила планом целиком), поэтому промпт остаётся `_SYSTEM_PROMPT`.
        assert fake.calls[1][0]["content"] == bpmn_generator._SYSTEM_PROMPT
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
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert _note(result["notes"], "не улучшил план")
        assert not _note(result["notes"], "план пересобран")

    def test_retry_failure_degrades_to_first_plan(self, monkeypatch):
        FakeLLM(monkeypatch, TestVacantPools._vacant(),
                llm_client.LLMError("503"))
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert result["status"] == "success"
        assert _note(result["notes"], "Повторный запрос модели не выполнен")

    def test_oversized_plan_is_not_sent_again(self, monkeypatch):
        monkeypatch.setattr(bpmn_generator, "MAX_RETRY_PLAN_CHARS", 10)
        fake = FakeLLM(monkeypatch, TestVacantPools._vacant(), _plan())
        result = BPMNGenerator().generate("ВкусВилл согласует заявку")
        assert len(fake.calls) == 1
        assert _note(result["notes"], "Повторный запрос модели не выполнен")



def test_flow_end_rule_shares_one_source_with_the_applier():
    """Генератор и аплайер лечат одно правило и обязаны знать про него одно и
    то же: множество запрещённых концов живёт в `bpmn_edits`, здесь только
    формулировки причин. Новый тип узла без текста причины иначе всплыл бы
    KeyError'ем в рантайме, а не в тесте."""
    from core import bpmn_edits

    assert set(bpmn_generator._FLOW_END_REASONS) == (
        bpmn_edits.SEQUENCE_FORBIDDEN_TARGETS
        | bpmn_edits.SEQUENCE_FORBIDDEN_SOURCES)
    for target in bpmn_edits.SEQUENCE_FORBIDDEN_TARGETS:
        assert bpmn_generator._illegal_flow_end("userTask", target)
    for source in bpmn_edits.SEQUENCE_FORBIDDEN_SOURCES:
        assert bpmn_generator._illegal_flow_end(source, "userTask")
    assert bpmn_generator._illegal_flow_end("userTask", "serviceTask") == ""


class TestReaskAcceptanceByPriority:
    """Приём повтора сравнивает нарушения по важности, а не по штукам.

    Сводка «сколько всего» отбрасывала план, вернувший потерянного участника
    ценой одного потока без условия, — и наоборот пропускала план, который
    променял участника на мелочь.
    """

    ACTOR = "ты сама назвала «Клиент» действующим лицом описания"
    VACANT = "пул «Кладовщик» без единого шага — в нём только старт и финиш"
    TIMER = "описание задаёт ожидание («в течение 15 минут»), таймера нет"
    MINOR = "у шлюза G1 2 ветки без условия"
    NO_POOL = ('в описании назван участник «WMS», а в схеме его нет: заведи '
               'пул «WMS» с его шагами')
    ROLE_WITHOUT_ORG = ('участник «Система мониторинга» помечен ролью '
                        '(external=false), но не сказал, чьей')

    def test_role_without_an_organization_ranks_as_a_lost_participant(self):
        """Пустая роль без организации никуда не девается: пул без шагов
        снимается починкой, и участник исчезает со схемы. Повтор, который
        «починил» названную систему именно так, приниматься не должен."""
        assert bpmn_generator._gap_profile(
            [self.ROLE_WITHOUT_ORG]) == (1, 0, 0)
        assert not bpmn_generator._reask_improves(
            [self.MINOR, self.MINOR], [self.ROLE_WITHOUT_ORG, self.MINOR])
        assert bpmn_generator._reask_improves(
            [self.ROLE_WITHOUT_ORG, self.MINOR], [self.MINOR, self.MINOR])

    def test_missing_system_pool_ranks_with_the_lost_actor(self):
        """Повтор вернул пул «WMS» ценой ещё одной мелочи — и должен
        приниматься: без этого пула `expected_participants` падал молча, а
        разбор стоил отдельного живого прогона."""
        assert bpmn_generator._gap_profile([self.NO_POOL, self.MINOR]) == (1, 0, 1)
        assert bpmn_generator._reask_improves(
            [self.NO_POOL, self.MINOR, self.MINOR],
            [self.MINOR, self.MINOR, self.MINOR])
        assert not bpmn_generator._reask_improves(
            [self.MINOR, self.MINOR, self.MINOR], [self.NO_POOL, self.MINOR])

    def test_profiles_split_gaps_by_class(self):
        profile = bpmn_generator._gap_profile(
            [self.ACTOR, self.VACANT, self.TIMER, self.MINOR])
        assert profile == (1, 2, 1)

    def test_equal_total_but_better_class_is_accepted(self):
        assert bpmn_generator._reask_improves(
            [self.ACTOR, self.MINOR], [self.MINOR, self.MINOR])

    def test_fewer_gaps_is_not_better_when_an_actor_was_lost(self):
        """Меньше — не значит лучше: променять действующее лицо на мелочь
        нельзя, иначе метрика участника падала бы молча."""
        assert not bpmn_generator._reask_improves(
            [self.MINOR, self.MINOR, self.MINOR], [self.ACTOR])

    def test_timer_is_content_not_cosmetics(self):
        """План, где таймер променяли на снятую мелочь, хуже исходного, даже
        если число нарушений не выросло."""
        assert not bpmn_generator._reask_improves(
            [self.MINOR, self.MINOR], [self.TIMER, self.MINOR])

    def test_more_violations_are_never_accepted(self):
        assert not bpmn_generator._reask_improves(
            [self.MINOR], [self.MINOR, self.MINOR])

    def test_content_fixed_at_the_price_of_cosmetics_is_accepted(self):
        """#39: повтор снял все нарушения содержания (пять потерянных
        действующих лиц) и заплатил за это выросшим числом мелочи — суммарный
        запрет выбрасывал такой план, и схема оставалась без участников.
        Профиль по классам важности для этого и сравняется: «важное на мелкое»
        не выменивается, если мелкого стало больше, а важного — меньше."""
        before = [self.ACTOR] * 5 + [self.MINOR, self.MINOR]
        after = [self.MINOR] * 8
        assert bpmn_generator._reask_improves(before, after)

    def test_same_plan_is_not_an_improvement(self):
        assert not bpmn_generator._reask_improves(
            [self.ACTOR, self.MINOR], [self.MINOR, self.ACTOR])

    def test_generate_accepts_the_plan_that_returns_the_actor(self, monkeypatch):
        """Сквозная проверка: второй план с тем же числом нарушений, но без
        потерянного участника, становится схемой."""
        lost_actor = _plan(
            actors=["Клиент", "Менеджер"],
            participants=["ВкусВилл"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Согласовать заявку", "L_m"),
                _e("G1", "exclusiveGateway", "Сумма большая?", "L_m"),
                _e("T2", "userTask", "Утвердить у директора", "L_m"),
                _e("E1", "endEvent", "Согласовано", "L_m"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "G1"),
                   _f("F3", "G1", "T2"), _f("F4", "G1", "E1"),
                   _f("F5", "T2", "E1")])
        # Тот же план: у клиента появились свои шаги, но ветка шлюза осталась
        # без условия — по сумме нарушений столько же.
        with_actor = _plan(
            actors=["Клиент", "Менеджер"],
            participants=["ВкусВилл", "Клиент"],
            lanes=[{"id": "L_m", "name": "Менеджер", "participant": "ВкусВилл"}],
            elements=[
                _e("S1", "startEvent", "Заявка", "L_m"),
                _e("T1", "userTask", "Согласовать заявку", "L_m"),
                _e("G1", "exclusiveGateway", "Сумма большая?", "L_m"),
                _e("T2", "userTask", "Утвердить у директора", "L_m"),
                _e("E1", "endEvent", "Согласовано", "L_m"),
                _e("S2", "startEvent", "Отказ принят", "", "Клиент"),
                _e("T3", "userTask", "Оплатить услугу", "", "Клиент"),
                _e("E2", "endEvent", "Оплачено", "", "Клиент"),
                _e("G2", "exclusiveGateway", "Оплата прошла?", "", "Клиент"),
            ],
            flows=[_f("F1", "S1", "T1"), _f("F2", "T1", "G1"),
                   _f("F3", "G1", "T2"), _f("F4", "G1", "E1"),
                   _f("F5", "T2", "E1"), _f("F6", "S2", "T3"),
                   _f("F7", "T3", "E2"), _f("F8", "G2", "T3"),
                   _f("F9", "G2", "E2"),
                   _f("M1", "T2", "T3", kind="message")])
        FakeLLM(monkeypatch, lost_actor, with_actor)
        result = BPMNGenerator().generate(
            "Клиент заводит заявку, менеджер её согласует, при большой сумме "
            "заявка уходит директору, после утверждения клиент оплачивает услугу")
        reask = next(e for e in result["trace"] if e["node"] == "переспрос плана")
        assert reask["kept"] == "переспрос", reask
        assert "Клиент" in [p["name"] for p in result["structure"]["participants"]]

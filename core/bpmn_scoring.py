# Rule-based скоринг BPMN-схем: 26 взвешенных правил, метрики качества без
# правки схемы.
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import Dict, List, NamedTuple, Set, Tuple

from .bpmn_edits import (
    DEFAULT_TIMER_DURATION,
    EVENT_TAGS,
    FLOW_NODE_TAGS,
    GATEWAY_TAGS,
    TASK_TYPE_TAGS,
    SEQUENCE_FORBIDDEN_SOURCES,
    SEQUENCE_FORBIDDEN_TARGETS,
    is_bpmn_tag,
    parse_xml,
    timer_schedule,
)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Активности для правил связности и разнообразия задач: flow-ноды, не
# являющиеся ни шлюзами, ни событиями. В bpmn_edits в TASK_TAGS входят и
# события, поэтому множество выводится из тегов, а не пересказывается.
ACTIVITY_TAGS = FLOW_NODE_TAGS - GATEWAY_TAGS - EVENT_TAGS

# «Шаг» пула: работа или ожидание. Граничное событие сюда не входит — оно
# висит на активности и без неё не существует, поэтому пустой пул с одним
# только boundaryEvent всё равно остаётся пустым.
STEP_TAGS = ACTIVITY_TAGS | {"intermediateCatchEvent", "intermediateThrowEvent"}

# Шлюзы, которые разбирают маршрут в несколько веток и потому обязаны их
# собирать обратно. `inclusiveGateway` здесь же: по двум подходящим веткам идут
# два токена, и неразведённая ветка — тот же висящий маршрут (замер по корпусу:
# 10 схем с разветвлённым inclusive, все с хождением). `eventBasedGateway` не
# берём: его ноги — ожидания разных событий, срабатывает одна, и сходятся они по
# событиям, а не по потокам; требовать слияния значит ругать корректную схему
# (на корпусе это 7 схем из 98 с разветвлённым event-шлюзом — ровно те, где
# оракул пока видел «развилку без схождения», см. `_splits` в `eval/invariants`).
SPLIT_GATEWAY_TAGS = ("exclusiveGateway", "parallelGateway", "inclusiveGateway")

PASSED = "passed"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"

# Формулировки, которыми совет сам отказывается от правки операцией. Хвост
# «→ чинится: <операции>» к такому тексту не дописывается (см. `evaluate`):
# правило, сказавшее «операции нет» и тут же назвавшее операцию, читается
# моделью как разрешение выдумать операнд.
MANUAL_FIX_MARKERS = ("не выражается операцией", "выражается только словами")

# Вызов операции в тексте подсказки — та же форма, что разбирает перепись
# `eval/advice.parse_recipes`: скоринг обязан видеть исполнимость совета теми же
# глазами, что и измеритель.
_OP_CALL_RE = re.compile(r"\b[a-z][a-z_]{2,}\(")

# Максимум id в тексте рекомендации: список уходит пользователю во фронтенд,
# длинное сообщение там не читается.
IDS_IN_MESSAGE = 10

# Правила про содержание процесса, а не про нотацию. Оркестратор улучшения
# группирует ими блок «УЗКИЕ МЕСТА ПО СКОРИНГУ», а харнесс по ним же сверяет,
# что починка тронула дефект маршрута, а не украшательство схемы.
BUSINESS_RULES = {
    "rework_loop",
    "handoff_pingpong",
    "approval_chain",
    "lane_overload",
    "wait_without_sla",
}

# Пороги бизнес-правил — именованные константы: «>60%» и «≥4 подряд» читаются
# в сообщении, а правятся в одном месте.
# Доля работ на одной дорожке, ниже которой `lane_overload` не ругается: перевес
# 60/40 — это разделение «делает / проверяет», а не узкое место.
LANE_OVERLOAD_SHARE = 0.6
# Порог для процесса из двух дорожек: перевес 2:1 там — обычное «делает / ждёт»,
# монополией считается только почти вся работа. Числом совпадает с
# `LANE_MONOPOLY_SHARE` оракула (`eval/invariants.py`): расхождение по этому
# признаку харнесс показал бы как `oracle_stricter`.
LANE_MONOPOLY_SHARE = 0.75
# Сколько рецептов умещается в одно сообщение правила (срок у ожидания, тип у
# события): строка блока «УЗКИЕ МЕСТА ПО СКОРИНГУ» уходит в промпт целиком, и
# четыре рецепта на одно правило вытеснили бы остальные находки.
RECIPES_IN_MESSAGE = 3
# Доля шагов с именем, ниже которой `naming` снимает балл (качество подписи, а не
# её наличие: имя из двух символов читателю не помогает).
NAMING_MIN_SHARE = 0.9
APPROVAL_CHAIN_MIN = 4
# Что в цепочке согласований считается ручной работой. `task` — шаг без типа,
# именно так рисуют человека в bpmn-js вручную; ограничение только `userTask`
# делало правило слепым ко всему реальному корпусу (367 файлов, сработавших —
# ноль), потому что подписи в импортированных схемах стоят как `task`.
HUMAN_TASK_TAGS = frozenset({"userTask", "manualTask", "task"})
# Определения catch-события, которые ожиданием не являются: `link` — метка, в
# которую прыгают с парной стороны схемы, `compensate` — внутренний триггер
# отработки. Ни то ни другое не ждёт ответа извне, и «висящий без срока маршрут»
# им не вменяется. Замер по корпусу рукописных схем: одна метка перехода давала
# 675 из 2731 нарушителя `wait_without_sla` (compensate в корпусе не встречается
# вовсе, но он того же рода). Сигнал и условие остаются
# ожиданиями — их триггер приходит извне, и токен стоит, пока никто не подаст
# сигнал и условие не станет истинным.
NOT_A_WAIT_DEFINITIONS = frozenset({"link", "compensate"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class _Flow(NamedTuple):
    id: str
    source: str
    target: str
    conditioned: bool


class _Check(NamedTuple):
    status: str
    elements: Tuple[str, ...] = ()
    note: str = ""


class _Schema:
    """Индекс схемы за одно прохождение дерева.

    Правила работают по готовым выборкам (id -> элемент, потоки по узлам),
    а не сканируют дерево на каждое правило."""

    def __init__(self, root: ET.Element) -> None:
        self.by_tag: Dict[str, List[ET.Element]] = defaultdict(list)
        self.flow_nodes: List[ET.Element] = []
        self.flows: List[_Flow] = []
        self.node_by_id: Dict[str, ET.Element] = {}
        self.outgoing: Dict[str, List[_Flow]] = defaultdict(list)
        self.incoming: Dict[str, List[_Flow]] = defaultdict(list)
        # Обратные смежности (узел -> предки) достаточно для сходимости веток:
        # прямой обход «от каждой ветки своего шлюза» дал бы O(веток * граф).
        self.preceding: Dict[str, List[str]] = defaultdict(list)
        # Раздача id для рецептов одного `evaluate`: несколько подсказок одного
        # прогона не должны предлагать один и тот же новый узел (см.
        # `_free_new_id`).
        self.new_id_alloc: Dict[str, int] = {}

        self.dangling_flows: List[_Flow] = []

        for element in root.iter():
            if not is_bpmn_tag(element.tag):
                # Расширение вида <acme:startEvent> — не стартовое событие BPMN:
                # иначе чужие теги попали бы в те же выборки, что и у аплайера.
                continue
            tag = _local(element.tag)
            self.by_tag[tag].append(element)
            if tag in FLOW_NODE_TAGS:
                self.flow_nodes.append(element)
                node_id = element.get("id")
                if node_id:
                    self.node_by_id[node_id] = element
            elif tag == "sequenceFlow":
                self.flows.append(_Flow(
                    element.get("id") or "",
                    element.get("sourceRef") or "",
                    element.get("targetRef") or "",
                    any(_local(child.tag) == "conditionExpression"
                        for child in element),
                ))

        # Смежность строится после прохода: поток в XML встречается раньше узла,
        # к которому он ведёт, и при построении «на ходу» он выглядел бы висящим.
        # Дуга с концами, которых в схеме нет, в граф не попадает: она делала
        # узел «соединённым» (ни `sequence_flows`, ни `no_isolated` не имели
        # повода ругаться), а `validate_and_repair` вырезала её молча, и тупик
        # появлялся уже после правки — починка выглядела её автором. Замер по
        # корпусу: 107 таких дуг в 33 схемах из 367, и в 15 из них скоринг
        # проходил `sequence_flows` целиком.
        for flow in self.flows:
            if (flow.source in self.node_by_id and flow.target in self.node_by_id):
                self.outgoing[flow.source].append(flow)
                self.incoming[flow.target].append(flow)
                self.preceding[flow.target].append(flow.source)
            else:
                self.dangling_flows.append(flow)

        self.tasks = _tags_of(self.flow_nodes, ACTIVITY_TAGS)
        self.exclusive_gateways = self.by_tag["exclusiveGateway"]
        self.start_events = self.by_tag["startEvent"]
        self.end_events = self.by_tag["endEvent"]
        self.boundary_events = self.by_tag["boundaryEvent"]
        self.intermediate_events = [
            e for tag in ("intermediateCatchEvent", "intermediateThrowEvent",
                          "boundaryEvent") for e in self.by_tag[tag]
        ]
        self.collaborations = self.by_tag["collaboration"]
        self.lane_sets = self.by_tag["laneSet"]
        # Стартов событий нижнего уровня: у вложенного subProcess своё стартовое
        # событие по семантике BPMN, и считать его в общий пул нельзя — иначе
        # любая схема с подпроцессом ловит ложный дефицит/избыток стартов.
        self.root_start_events = [
            child for process in self.by_tag["process"]
            for child in process
            if isinstance(child.tag, str) and _local(child.tag) == "startEvent"
        ]
        self.participants = [
            p for coll in self.collaborations for p in _children(coll, "participant")
        ]
        # Узлы процесса по его id + обратная ссылка «процесс -> участники».
        # Только верхний уровень: вложенные в subProcess узлы принадлежат
        # шагу, а не пулу, иначе пул из «старт -> подпроцесс -> финиш»
        # считался бы богатым на шаги.
        self.nodes_by_process: Dict[str, List[ET.Element]] = {}
        self.participants_by_process: Dict[str, List[ET.Element]] = defaultdict(list)
        self.owner_of_node: Dict[str, str] = {}
        for process in self.by_tag["process"]:
            process_id = process.get("id") or ""
            if not process_id:
                continue
            self.nodes_by_process[process_id] = [
                child for child in process
                if isinstance(child.tag, str) and _local(child.tag) in FLOW_NODE_TAGS
            ]
            for node in self.nodes_by_process[process_id]:
                node_id = node.get("id")
                if node_id:
                    self.owner_of_node[node_id] = process_id
        for participant in self.participants:
            process_ref = participant.get("processRef") or ""
            if process_ref:
                self.participants_by_process[process_ref].append(participant)
        # Концы потока сообщения: либо участник (пул в пул), либо узел (шаг в
        # шаг). Дуга без конца или на несуществующий id — тот же класс, что
        # висячая последовательность, и `participant_interacts` её не ловит:
        # он считает, кого поток затронул, а сломанный конец ни кого не
        # затрагивает и выглядит просто «мало обменов». Замер по корпусу:
        # 8 таких дуг в 6 схемах из 367.
        ends_known = set(self.node_by_id) | {p.get("id") or ""
                                             for p in self.participants}
        self.dangling_messages: List[_Flow] = []
        # Конец обмена, прицепленный к развилке: отдельный список, а не общее
        # «битый конец», потому что причина и правка у класса разные — конец
        # существует, но узел такого типа сообщением не связывается.
        self.gateway_messages: List[_Flow] = []
        for element in self.by_tag["messageFlow"]:
            flow = _Flow(element.get("id") or "", element.get("sourceRef") or "",
                         element.get("targetRef") or "", False)
            if flow.source not in ends_known or flow.target not in ends_known:
                self.dangling_messages.append(flow)
                continue
            # Развилка не бывает концом потока-сообщения: у обмена концом стоит
            # участник или узел, но не шлюз. Корпус это подтверждает молча — из
            # 625 обменов 367 рукописных схем к шлюзу не подходит ни один, — а
            # совет `cross_pool_flow` такие обмены рисовал сам (2 пакета из 5),
            # и перепись считала ход чистым снятием нарушения.
            if any(self.node_by_id.get(ref) is not None
                   and _local(self.node_by_id[ref].tag) in GATEWAY_TAGS
                   for ref in (flow.source, flow.target)):
                self.gateway_messages.append(flow)
        # Дорожки по процессам: «роль, раздутая в участника» ищется как имя
        # участника, совпавшее с дорожкой ЧУЖОГО процесса.
        self.lane_names_by_process: Dict[str, List[str]] = {}
        for process in self.by_tag["process"]:
            names = [name for lane_set in _children(process, "laneSet")
                     for lane in _children(lane_set, "lane")
                     if (name := (lane.get("name") or "").strip())]
            if names:
                self.lane_names_by_process[process.get("id") or ""] = names
        # Раскладка по дорожкам — тем же проходом: «узел -> id дорожки» для
        # того, кто держит в руках шаг, и «процесс -> дорожки» для того, кто
        # их делит. Вложенных laneSet bpmn-js не рисует, поэтому вложенные
        # дорожки здесь не обходятся.
        self.lane_by_id: Dict[str, ET.Element] = {}
        self.lane_of_node: Dict[str, str] = {}
        self.lanes_by_process: Dict[str, List[ET.Element]] = {}
        for process in self.by_tag["process"]:
            lanes = [lane for lane_set in _children(process, "laneSet")
                     for lane in _children(lane_set, "lane")]
            if not lanes:
                continue
            self.lanes_by_process[process.get("id") or ""] = lanes
            for lane in lanes:
                lane_id = lane.get("id") or ""
                if lane_id:
                    self.lane_by_id[lane_id] = lane
                for ref in _children(lane, "flowNodeRef"):
                    node_id = (ref.text or "").strip()
                    if node_id:
                        self.lane_of_node[node_id] = lane_id

    def out_targets(self, node_id: str) -> List[str]:
        return [f.target for f in self.outgoing.get(node_id, []) if f.target]

    def nodes_of_participant(self, participant: ET.Element) -> List[ET.Element]:
        """Узлы процесса, на который ссылается участник; [] — ссылки нет или она
        битая (такой пул не может ни что-то делать, ни завершаться)."""
        return self.nodes_by_process.get(participant.get("processRef") or "", [])

    def participants_of_node(self, node_id: str) -> List[ET.Element]:
        process_id = self.owner_of_node.get(node_id or "", "")
        return list(self.participants_by_process.get(process_id, ()))

    def has_documentation(self, element: ET.Element) -> bool:
        return any(_local(child.tag) == "documentation" for child in element)

    def lane_name(self, lane_id: str) -> str:
        """Имя дорожки для текста рекомендации: id модель в `lane` скопировать
        может, а прочитать из него ничего не сможет."""
        lane = self.lane_by_id.get(lane_id)
        if lane is None:
            return lane_id
        return (lane.get("name") or "").strip() or lane_id

    def name_of(self, element: ET.Element) -> str:
        """Имя элемента, а без него — id: адресат действия обязан читаться
        человеком и разрешаться моделью однозначно."""
        return (element.get("name") or "").strip() or (element.get("id") or "")


def _tags_of(elements: List[ET.Element], tags) -> List[ET.Element]:
    return [e for e in elements if _local(e.tag) in tags]


def _children(element: ET.Element, tag: str) -> List[ET.Element]:
    return [c for c in element if _local(c.tag) == tag]


def _ids(elements) -> Tuple[str, ...]:
    return tuple(e.get("id") or _local(e.tag) for e in elements)


def _join_ids(ids: Tuple[str, ...]) -> str:
    """Хвост рекомендации со списком проблемных элементов.

    Список ограничивается: сообщение видит пользователь в панели скоринга,
    а не лог."""
    ids = list(ids)
    shown = ", ".join(ids[:IDS_IN_MESSAGE])
    if len(ids) > IDS_IN_MESSAGE:
        shown += f" и ещё {len(ids) - IDS_IN_MESSAGE}"
    return shown


def _route_entry(s: _Schema, participant: ET.Element) -> str:
    """Первый шаг маршрута пула: узел без входящего потока — туда и крепится
    старт. '' — шага нет (за это отвечает `pool_has_steps`)."""
    for node in s.nodes_of_participant(participant):
        if _local(node.tag) in ("startEvent", "endEvent", "boundaryEvent"):
            continue
        if not s.incoming.get(node.get("id") or ""):
            return node.get("id") or ""
    return ""


def _route_exit(s: _Schema, participant: ET.Element) -> str:
    """Последний шаг маршрута пула: узел без исходящего потока — к нему и
    крепится финиш."""
    for node in reversed(s.nodes_of_participant(participant)):
        if _local(node.tag) in ("startEvent", "boundaryEvent"):
            continue
        if not s.outgoing.get(node.get("id") or ""):
            return node.get("id") or ""
    return ""


def _participant_label(participant: ET.Element) -> str:
    return (participant.get("name") or participant.get("id") or "?").strip()


def _free_new_id(s: _Schema, base: str) -> str:
    """Id нового узла, которого на этой схеме ещё нет.

    Рецепт с занятым id аплайер отвергает («id 'new_gate' уже занят»), и правка
    теряется. Опаснее всего литерал на втором круге того же контура: первый
    круг создал `new_sla_fork_1`, подсказка второго зовёт его же — перепись
    `eval/advice.py` стопорилась на этом в `approval_chain` и `wait_without_sla`,
    и нарушение не снималось ни одним кругом. Префикс `new_` сохраняется: без
    него аплайер отвечает «новый id без префикса new_».
    """
    taken = {elem.get("id") for elems in s.by_tag.values() for elem in elems
             if elem.get("id")}
    taken |= set(s.node_by_id)
    head, sep, tail = base.rpartition("_")
    prefix, start = (head, int(tail)) if sep and tail.isdigit() else (base, 1)
    number = max(start, s.new_id_alloc.get(prefix, 0))
    while f"{prefix}_{number}" in taken:
        number += 1
    s.new_id_alloc[prefix] = number + 1
    return f"{prefix}_{number}"


def _route_ops_hint(s: _Schema, headless: List[ET.Element], kind: str) -> List[str]:
    """Подсказка «у пула нет старта/финиша», выраженная теми id, которые
    правило уже знает: именем участника и крайним шагом его маршрута.

    Оперируемые id важнее вежливого совета: блок «УЗКИЕ МЕСТА ПО СКОРИНГУ»
    уходит в промпт улучшения дословно, и `add_event` без `participant` и без
    ноги маршрута контур превращает либо в отказ аплайера, либо в изолированный
    кружок, за который тут же снимает `no_isolated`."""
    ops: List[str] = []
    event_name = "Начало" if kind == "start" else "Завершение"
    for ordinal, participant in enumerate(headless[:RECIPES_IN_MESSAGE], 1):
        pid = participant.get("id") or "?"
        new_id = _free_new_id(
            s, f"new_{'start' if kind == 'start' else 'end'}_{ordinal}")
        if kind == "start":
            anchor = _route_entry(s, participant)
            pool = _pool_hint(s, anchor)
            link = f"; connect(source='{new_id}', target='{anchor}')" if anchor else ""
        else:
            anchor = _route_exit(s, participant)
            pool = _pool_hint(s, anchor)
            link = f"; connect(source='{anchor}', target='{new_id}')" if anchor else ""
        if not anchor:
            has_steps = any(_local(n.tag) in ACTIVITY_TAGS
                            for n in s.nodes_of_participant(participant))
            if not has_steps:
                # Вызова операции здесь намеренно нет: `add_event` в пул без
                # шагов аплайер отвергает («пул не определён»), а совет, который
                # нельзя применить, перепись исполнимости показывает как отказ.
                ops.append(f"'{pid}': стартового события нет, и крепить его не к "
                           "чему — в пуле нет ни одного шага, поэтому правка не "
                           "выражается операцией, пока шаг не появится "
                           "(`pool_has_steps`)")
                continue
            # Шаги есть, а входа нет ни у одного: маршрут замкнут или кормится
            # сообщением. Отказывать здесь было бы неправдой — `add_event` в
            # такой пул применяется, но голый старт тут же заряжает
            # `no_isolated`, поэтому рецепт обязан требовать дугу.
            ops.append(f"'{pid}': add_event(id='{new_id}', event_type='{kind}', "
                       f"name='{event_name}', participant='{pid}') — у шагов "
                       "пула вход уже занят, поэтому вторым шагом пакета нужна "
                       f"дуга от '{new_id}' к шагу, с которого процесс начинается; "
                       "каким — называет описание процесса, а без неё старт "
                       "останется изолированным (`no_isolated`)")
            continue
        ops.append(f"'{pid}': add_event(id='{new_id}', event_type='{kind}', "
                   f"name='{event_name}', participant='{pool}'){link}")
    if len(headless) > RECIPES_IN_MESSAGE:
        ops.append(f"и ещё {len(headless) - RECIPES_IN_MESSAGE} участнику "
                   "та же форма")
    ops.append("имя события уточняется по смыслу процесса")
    return ops


def _check_start_event(s: _Schema) -> _Check:
    """Правило отвечает за «пул начинается»: у каждого участника свой startEvent.

    раньше здесь сравнивалось общее число стартов с числом участников, и это
    ломалось на легальном улучшении: `role_pools` (−8) велит слить роль в
    дорожку, слияние оставляет в одном процессе два старта — по одному на
    прежнюю цепочку, — и `start_event` (−10) наказывал за то, что скоринг же и
    предложил. Симметрично `end_event`: штрафует участника без старта, а не
    лишние входы в процесс.
    """
    if not s.participants:
        return _Check(PASSED) if s.root_start_events else _Check(
            FAILED, _ids(s.root_start_events),
            "в схеме нет ни одного стартового события")
    headless = [p for p in s.participants
                if not any(_local(n.tag) == "startEvent"
                           for n in s.nodes_of_participant(p))]
    if not headless:
        return _Check(PASSED)
    return _Check(FAILED, _ids(headless),
                  f"без события старта {len(headless)} из "
                  f"{len(s.participants)} участников: "
                  + "; ".join(_route_ops_hint(s, headless, "start")))


def _check_end_event(s: _Schema) -> _Check:
    """Правило отвечает за «пул завершается»: у каждого участника свой endEvent.

    За «пул что-то делает» отвечает `pool_has_steps`, и границы между ними
    выдержаны намеренно: схема `startEvent → endEvent` формально завершается
    корректно и наказывается один раз — отсутствием шагов. Иначе за одну и ту
    же пустую декорацию снимались бы веса двух правил сразу."""
    if not s.participants:
        return _Check(PASSED) if s.end_events else _Check(FAILED)
    tailless = [p for p in s.participants
                if not any(_local(n.tag) == "endEvent" for n in s.nodes_of_participant(p))]
    if not tailless:
        return _Check(PASSED)
    return _Check(FAILED, _ids(tailless),
                  note=f"без события завершения {len(tailless)} из "
                  f"{len(s.participants)} участников: "
                  + "; ".join(_route_ops_hint(s, tailless, "end")))


def _check_pool_has_steps(s: _Schema) -> _Check:
    """Пул без шагов — декорация: `startEvent → endEvent` связен по BPMN, но
    процесса как работы в нём нет, и скоринг обязан это отличать."""
    if not s.participants:
        return _Check(NOT_APPLICABLE)
    stepless = [p for p in s.participants
                if not any(_local(n.tag) in STEP_TAGS for n in s.nodes_of_participant(p))]
    if not stepless:
        return _Check(PASSED)
    # «Добавь шаг» без адреса невыполнимо: `add_task` кладёт узел в процесс, но
    # в маршрут его не ставит, и гарант откачивает правку как шаг вне маршрута.
    # Адрес есть у того же пула: дуга старт→финиш, в которую шаг вставляется.
    notes: List[str] = []
    for ordinal, participant in enumerate(stepless[:RECIPES_IN_MESSAGE], 1):
        pid = participant.get("id") or "?"
        nodes = s.nodes_of_participant(participant)
        start = next((n.get("id") or "" for n in nodes
                      if _local(n.tag) == "startEvent"), "")
        end = next((n.get("id") or "" for n in nodes
                    if _local(n.tag) == "endEvent"), "")
        if start and end and end in s.out_targets(start):
            notes.append(
                f"'{pid}': add_task(id='new_step_{ordinal}', "
                f"participant='{_participant_label(participant)}', "
                f"after='{start}', to='{end}')")
        else:
            # Пустые id в этой фразе — не «операнда нет», а сломанный текст:
            # модель читает «в дугу ''→''» как испорченную подсказку (ratchet
            # `test_no_advice_prints_an_empty_operand`). Правило обязано назвать
            # то, что о пуле знает, и словами сказать то, чего не знает.
            known = " и ".join(x for x in (f"старт '{start}'" if start else "",
                                           f"финиш '{end}'" if end else "") if x)
            where = (f"в пуле есть {known}, но дуги старт→финиш нет" if known
                     else "в пуле нет ни старта, ни финиша")
            notes.append(
                f"'{pid}': {where} — вставлять шаг некуда, адрес называется "
                "только по смыслу процесса (связность маршрута правкой "
                "`add_task` не выражается)")
    if len(stepless) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(stepless) - RECIPES_IN_MESSAGE} участнику "
                     "та же форма")
    return _Check(FAILED, _ids(stepless),
                  note=f"без шагов {len(stepless)} из "
                  f"{len(s.participants)} участников: " + "; ".join(notes))


def _check_participant_interacts(s: _Schema) -> _Check:
    """Участник, не затронутый ни одним messageFlow, — не участник, а отдельно
    нарисованная схема: обменяться с коллегами ему нечем."""
    if len(s.participants) < 2:
        return _Check(NOT_APPLICABLE)
    participant_ids = {p.get("id") for p in s.participants}
    touched: Set[str] = set()
    for flow in s.by_tag["messageFlow"]:
        for ref in (flow.get("sourceRef"), flow.get("targetRef")):
            # Поток сообщения может ссылаться и на узел процесса, и на сам пул.
            if ref in participant_ids:
                touched.add(ref)
            touched.update(p.get("id") for p in s.participants_of_node(ref))
    silent = [p for p in s.participants if p.get("id") not in touched]
    if not silent:
        return _Check(PASSED)
    # «Заведи обмен» без шагов-концов невыполнимо: `connect` принимает узлы, а
    # пул целиком — нет. Обе стороны берутся из той же схемы: первый шаг немого
    # пула и последний шаг того, который в обмене уже участвует.
    donors = [p for p in s.participants if p.get("id") not in touched]
    notes: List[str] = []
    for ordinal, participant in enumerate(silent[:RECIPES_IN_MESSAGE], 1):
        pid = participant.get("id") or "?"
        mine = _route_entry(s, participant)
        theirs = next((exit_id for exit_id in
                       (_route_exit(s, p) for p in donors) if exit_id), "")
        if mine and theirs:
            notes.append(f"'{pid}': connect(source='{theirs}', target='{mine}', "
                         "flow_type='message')")
        else:
            notes.append(f"'{pid}': не у каждого пула есть шаг, которым можно "
                         "обменяться — контрагента называет автор процесса")
    if len(silent) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(silent) - RECIPES_IN_MESSAGE} участнику "
                     "та же форма")
    return _Check(FAILED, _ids(silent),
                  note=f"без потоков сообщений {len(silent)} из "
                  f"{len(s.participants)} участников: " + "; ".join(notes))


def _check_gateway_conditions(s: _Schema) -> _Check:
    """Ветви шлюза обязаны различаться: либо условие на потоке, либо поток
    назначен выходом по умолчанию атрибутом `default`.

    Смотрим только на расходящиеся шлюзы (≥2 исходящих). У шлюза схождения одна
    ветка и выбирать не из чего, а генератор ставит такие пары осознанно —
    требовать условие на выходе схода значило бы снимать 15 баллов за починку
    развилки, то есть наказывать улучшение схемы. Корректность развилки при этом
    не остаётся без присмотра: её проверяет `gateway_split_join`."""
    if not s.exclusive_gateways:
        return _Check(NOT_APPLICABLE)
    bad_gateways = []
    bad_flows: List[str] = []
    for gateway in s.exclusive_gateways:
        outgoing = s.outgoing.get(gateway.get("id") or "", [])
        if len(outgoing) < 2:
            continue
        default_flow = gateway.get("default")
        unconditioned = [f.id for f in outgoing
                         if not f.conditioned and f.id != default_flow]
        if unconditioned:
            bad_gateways.append(gateway)
            bad_flows.extend(unconditioned)
    if not bad_gateways:
        return _Check(PASSED)
    # Операндов для `add_condition` правилу взять не откуда: идентификатор ноги
    # известен, а условие пишет автор процесса. Молчать об этом нельзя — контур
    # читает «чинится: add_condition» как разрешение выдумать текст.
    legs = ", ".join(f"'{f}'" for f in bad_flows[:RECIPES_IN_MESSAGE])
    tail = (f"; и ещё {len(bad_flows) - RECIPES_IN_MESSAGE}"
            if len(bad_flows) > RECIPES_IN_MESSAGE else "")
    return _Check(FAILED, _ids(bad_gateways) + tuple(bad_flows),
                  note=f"без условия и без default ноги {legs}{tail} — "
                  "текст условия называет автор процесса, поэтому рецепт "
                  "выражается только словами")


def _check_sequence_flows(s: _Schema) -> _Check:
    """Поток обязан соединять два узла схемы, а шагу нужны вход и выход.

    Висячая дуга (`targetRef` на несуществующий или отсутствующий узел) — не
    косметика: пока она считалась дугой, её узел выглядел соединённым, и ни это
    правило, ни `no_isolated` не имели повода ругаться, а `validate_and_repair`
    вырезает её молча и тупик появляется уже после починки — перепись
    подсказок записывала это как «совет сломал другое правило». Оракул харнесса
    прощал дугу так же (её `_Graph` тоже заводил по ней смежность), так что
    находкой этот класс стал только потому, что два слоя сверили между собой на
    корпусе: 107 таких дуг в 33 схемах из 367.

    Подсказка при этом НЕ советует `disconnect(flow=…)` отдельно от нового конца:
    перепись исполнимости показала 3 схемы из 6, где такой половинчатый пакет
    ронял `no_isolated` и `sequence_flows` — удаление дуги без её замены и есть
    тот самый тупик."""
    dangling = s.dangling_flows
    if not s.tasks and not dangling:
        return _Check(NOT_APPLICABLE)
    unconnected = [t for t in s.tasks
                   if not s.incoming.get(t.get("id") or "")
                   or not s.outgoing.get(t.get("id") or "")]
    if not dangling and not unconnected:
        return _Check(PASSED)
    notes: List[str] = []
    for flow in dangling[:RECIPES_IN_MESSAGE]:
        ident = flow.id or f"{flow.source}->{flow.target}"
        if not flow.source or not flow.target:
            missing = "источник" if not flow.source else "цель"
            why = f"у дуги нет {missing}а"
        else:
            why = "конец дуги указывает на узел, которого в схеме нет"
        anchor = flow.source or flow.target
        # Называется тот конец дуги, который на схеме ЕСТЬ: «приставь к шагу ””»
        # — пустой операнд читается моделью как испорченный текст, а не как
        # «операнда нет» (ratchet `test_no_advice_prints_an_empty_operand`).
        where = (f"дугу нужно приставить к шагу '{anchor}', а "
                 if anchor else "на схеме нет ни одного конца этой дуги — ")
        notes.append(
            f"'{ident}': {why}; {where}"
            "его сосед называется только по смыслу процесса, а не структурой")
    if len(dangling) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(dangling) - RECIPES_IN_MESSAGE} висячих дуг "
                     "той же формой")
    if unconnected:
        # Вызываемой операции здесь намеренно нет: `connect` с половиной пары
        # аплайер отвергает, а перепись показала бы «названо, но неприменимо»
        # там, где второго конца знает только автор процесса.
        listed = "; ".join(
            f"'{n.get('id') or '?'}': "
            + " и ".join(w for w, ok in (
                ("нет входа", s.incoming.get(n.get("id") or "")),
                ("нет выхода", s.outgoing.get(n.get("id") or ""))) if not ok)
            + " — соседний шаг называется только по смыслу процесса"
            for n in unconnected[:RECIPES_IN_MESSAGE])
        notes.append(f"без входа или выхода: {listed}"
                     + (f"; и ещё {len(unconnected) - RECIPES_IN_MESSAGE} тем же"
                        if len(unconnected) > RECIPES_IN_MESSAGE else ""))
    ends = tuple(f.id or f"{f.source}->{f.target}" for f in dangling)
    return _Check(FAILED, ends + _ids(unconnected), "; ".join(notes))


def _message_leg(s: _Schema, node_id: str, direction: str) -> Tuple[str, str]:
    """Узел, который становится концом обмена, и пояснение к нему.

    Развилка концом `messageFlow` не бывает (BPMN: обмен связывает участника или
    узел, но не шлюз; в 625 обменах корпуса шлюзовых концов ноль), а межпуловая
    дуга часто выходит именно из развилки — это её «ветка в другой пул». Правка
    поэтому называет концом обмена шаг того же пула: отправителем становится
    единственный шаг, ведущий в развилку, приёмником — единственный шаг, из
    неё выходящий. Кандидат структурный, а не выдуманный, но если таких шагов
    несколько, операнда нет: его обязан назвать автор процесса.
    """
    element = s.node_by_id.get(node_id or "")
    if element is None or _local(element.tag) not in GATEWAY_TAGS:
        return node_id, ""
    neighbours = (list(s.preceding.get(node_id, [])) if direction == "source"
                  else list(s.out_targets(node_id)))
    own = s.owner_of_node.get(node_id, "")
    steps = [n for n in dict.fromkeys(neighbours)
             if s.node_by_id.get(n) is not None
             and _local(s.node_by_id[n].tag) in ACTIVITY_TAGS
             and s.owner_of_node.get(n, "") == own]
    if len(steps) == 1:
        return steps[0], (f"развилка '{node_id}' концом обмена не бывает, "
                          f"обменом становится её шаг '{steps[0]}'")
    candidates = ", ".join(f"'{s.name_of(s.node_by_id[n])}'" for n in steps)
    return "", (f"развилка '{element.tag.split('}')[-1]}' '{node_id}' концом "
                f"обмена не бывает, а отправляет или принимает сообщение её шаг "
                f"({candidates or 'кандидатов нет в маршруте'}) — его называет "
                "автор процесса")


def _check_cross_pool_flow(s: _Schema) -> _Check:
    """Токен не может переехать из одного процесса в другой: `sequenceFlow`
    живёт внутри одной области (пула, подпроцесса), а связь участников
    выражается `messageFlow`.

    Класс нашёлся переписью корпуса, а не фикстурами: 8 дуг в 5 схемах из 367, и
    ни одно из 22 правил их не заряжало — `sequence_flows` проверяет, что конец
    дуги существует, а не в каком процессе он лежит, `no_isolated` видит концы
    соединёнными, и даже `handoff_pingpong` не спорит: обмен между пулами
    выглядит как обмен. Цена молчания не косметическая — bpmn-js такую дугу
    рисует, а исполнитель процесса по ней не идёт, поэтому «связный» процесс с
    межпуловой дугой остаётся связным только на картинке.

    Правка выражается пакетом целиком: резать дугу и тянуть на её место
    сообщение между теми же двумя шагами — оба id уже есть в схеме.
    """
    if len(s.by_tag["process"]) < 2:
        return _Check(NOT_APPLICABLE)
    crossing = []
    for flow in s.flows:
        a = s.owner_of_node.get(flow.source or "", "")
        b = s.owner_of_node.get(flow.target or "", "")
        if a and b and a != b:
            crossing.append((flow, a, b))
    if not crossing:
        return _Check(PASSED)
    notes: List[str] = []
    for flow, a, b in crossing[:RECIPES_IN_MESSAGE]:
        ident = flow.id or f"{flow.source}->{flow.target}"
        src, src_note = _message_leg(s, flow.source, "source")
        dst, dst_note = _message_leg(s, flow.target, "target")
        src_label = _participant_label_of(s, flow.source)
        dst_label = _participant_label_of(s, flow.target)
        legs = "; ".join(n for n in (src_note, dst_note) if n)
        # Концы обмена оба известны — рецепт полный; если хотя бы один назван
        # словами, правка пакета начинается с `disconnect`, а второй шаг
        # подсказки модель обязана выбрать сама.
        recipe = (f"disconnect(flow='{ident}') + connect(source='{src}', "
                  f"target='{dst}', flow_type='message')"
                  if src and dst else f"disconnect(flow='{ident}')")
        notes.append(
            f"'{ident}': {recipe} — шаг '{flow.source}' ({src_label}) и шаг "
            f"'{flow.target}' ({dst_label}) принадлежат разным процессам, "
            "сообщением между ними становится нога, а не поток управления"
            + (f" — {legs}" if legs else "")
            + _stranded_tail(s, flow))
    if len(crossing) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(crossing) - RECIPES_IN_MESSAGE} дуг той же формой")
    return _Check(FAILED, tuple(f.id or f"{f.source}->{f.target}"
                                for f, _, _ in crossing), "; ".join(notes))


def _stranded_tail(s: _Schema, flow: _Flow) -> str:
    """Предупреждение о том, что обнажит правка.

    Межпуловая дуга часто бывает единственным «управленческим» продолжением
    шага: после её перевода в сообщение у активности внутри своего процесса не
    остаётся ни входа, ни выхода, и `no_isolated` заряжает её следом. Для
    оркестратора это хуже косметики: `_rules_regressed` отклоняет весь пакет, и
    дуга переживает улучшение. Поэтому предупреждение в тексте — часть рецепта,
    а не сноска: продолжение внутри пула знает только модель по описанию
    процесса, и спросить его нужно в этом же пакете."""
    stranded = []
    for node_id, direction in ((flow.source, "выхода"), (flow.target, "входа")):
        elem = s.node_by_id.get(node_id)
        if elem is None or _local(elem.tag) not in ACTIVITY_TAGS:
            continue
        own = s.owner_of_node.get(node_id, "")
        if direction == "выхода":
            rest = [f for f in s.outgoing.get(node_id, [])
                    if f is not flow and s.owner_of_node.get(f.target, "") == own]
        else:
            rest = [f for f in s.incoming.get(node_id, [])
                    if f is not flow and s.owner_of_node.get(f.source, "") == own]
        if not rest:
            stranded.append(f"'{node_id}' без {direction} внутри своего процесса")
    if not stranded:
        return ""
    return ("; после правки " + ", ".join(stranded)
            + " — продолжение внутри пула называется только по смыслу процесса")


def _participant_label_of(s: _Schema, node_id: str) -> str:
    """Как называется пул, которому принадлежит узел (или «без участника», если
    процесс в коллаборации никем не объявлен): модель обязана понять из текста,
    между кем именно висит дуга."""
    pools = s.participants_of_node(node_id)
    return "; ".join(_participant_label(p) for p in pools) or "без участника"


def _check_message_flow_ends(s: _Schema) -> _Check:
    """У каждого потока сообщения оба конца названы и оба способны обмен
    принять: это участник или узел внутри участника, но не развилка.

    Класс тот же, что у висячей последовательности (дуга, ведущая в ниоткуда), и
    правило отдельное по той же причине, по какой у сверки двух слоёв есть пары:
    у `sequence_flows` свой независимый читатель — `no_unrouted`, и смешать там
    два нарушения значило бы потерять расхождение в шуме. Молчание дорого стоит
    дважды: `participant_interacts` считает, кого обмен затронул, и сломанный
    конец просто ни кого не затрагивает — схема выглядит «мало участников», а не
    «битый обмен». Замер по корпусу: 8 дуг в 6 схемах из 367, у всех них либо
    нет атрибута конца, либо он указывает на id, которого в схеме нет.

    Второй подкласс принёс не корпус, а совет этой же линейки:
    `cross_pool_flow` переводил межпуловую дугу из развилки в
    `connect(source='<шлюз>', …, flow_type='message')`. В 625 обменах корпуса к
    шлюзу не подходит ни одно, то есть правка заводила дефект, которого живые
    модельеры не допускают, а перепись считала её чистым снятием нарушения."""
    if not s.by_tag["messageFlow"]:
        return _Check(NOT_APPLICABLE)
    if not s.dangling_messages and not s.gateway_messages:
        return _Check(PASSED)
    known = set(s.node_by_id) | {p.get("id") or "" for p in s.participants}
    notes: List[str] = []
    for flow in s.dangling_messages[:RECIPES_IN_MESSAGE]:
        ident = flow.id or f"{flow.source}->{flow.target}"
        missing = [w for w, ref in (("источника", flow.source), ("цели", flow.target))
                   if not ref]
        unknown = [f"'{ref}'" for ref in (flow.source, flow.target)
                   if ref and ref not in known]
        why = ("у обмена нет " + " и ".join(missing) if missing else
               f"конец обмена указывает на узел {', '.join(unknown)}, которого в "
               "схеме нет")
        notes.append(
            f"'{ident}': {why}; поток сообщения приставляется к шагу или пулу, "
            "а какой именно — называется только по смыслу процесса")
    for flow in s.gateway_messages[:max(0, RECIPES_IN_MESSAGE - len(notes))]:
        ident = flow.id or f"{flow.source}->{flow.target}"
        ends = [(ref, _local(s.node_by_id[ref].tag))
                for ref in (flow.source, flow.target)
                if s.node_by_id.get(ref) is not None
                and _local(s.node_by_id[ref].tag) in GATEWAY_TAGS]
        listed = ", ".join(f"'{ref}' ({tag})" for ref, tag in ends)
        notes.append(
            f"'{ident}': концом обмена названа развилка {listed}; развилка "
            "концом обмена не бывает — сообщением обмениваются шаг и шаг или пул "
            "целиком, а какой шаг пула отправляет и принимает, называет автор "
            "процесса (развилка остаётся внутри своего процесса)")
    broken = s.dangling_messages + s.gateway_messages
    if len(broken) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(broken) - RECIPES_IN_MESSAGE} "
                     "обменов той же формой")
    return _Check(FAILED, tuple(f.id or f"{f.source}->{f.target}"
                                for f in broken), "; ".join(notes))


def _check_flow_ends_legal(s: _Schema) -> _Check:
    """Поток управления не входит в стартовое и граничное событие и не выходит
    из конечного: старт запускает его хозяин по `attachedToRef`, у финиша
    продолжения нет по определению.

    Класс отличается от двух предыдущих тем, что на рукописном корпусе его нет
    ни разу (0 из 367 — и скоринг, и оракул молчали одинаково честно). Он взят
    из прогонов самого контура: оракул харнесса ловил такие дуги в 9 случаях из
    2080 проверок итогового XML, а скоринг — никогда, то есть `_rules_regressed`
    пропускал пакет, который рисовал дугу «из финиша в шаг», и пользователь
    видел её как валидный маршрут. Запрет ещё и в аплайере (`connect` отказывает
    с подсказкой «вставляйте шаг до него»), и константы взяты оттуда же: два
    слоя обязаны расходиться в прочтении схемы, а не в списке тегов.

    Рецепта здесь нет намеренно: дугу нужно не перевести, а убрать, а чем
    замещается её конец — знает только автор процесса. Половинчатый
    `disconnect` уже показал себя создателем тупика (`sequence_flows`).
    """
    illegal = []
    for flow in s.flows:
        source = s.node_by_id.get(flow.source or "")
        target = s.node_by_id.get(flow.target or "")
        # висячий конец — забота `sequence_flows`, дважды не рапортуем
        if source is None and target is None:
            continue
        if (target is not None and _local(target.tag) in SEQUENCE_FORBIDDEN_TARGETS
                or source is not None
                and _local(source.tag) in SEQUENCE_FORBIDDEN_SOURCES):
            illegal.append((flow, source, target))
    if not s.flows:
        return _Check(NOT_APPLICABLE)
    if not illegal:
        return _Check(PASSED)
    notes = []
    for flow, source, target in illegal[:RECIPES_IN_MESSAGE]:
        ident = flow.id or f"{flow.source}->{flow.target}"
        if target is not None and _local(target.tag) in SEQUENCE_FORBIDDEN_TARGETS:
            what = (f"входит в {_local(target.tag)} '{flow.target}' — его запускает "
                    "хозяин, а не сосед по маршруту")
        else:
            what = (f"исходит из {_local(source.tag)} '{flow.source}' — у конца "
                    "маршрута продолжения нет")
        notes.append(f"'{ident}': {what}; чем замещать дугу, называет автор "
                     "процесса, а не структура схемы")
    if len(illegal) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(illegal) - RECIPES_IN_MESSAGE} дуг той же формой")
    return _Check(FAILED, tuple(f.id or f"{f.source}->{f.target}"
                                for f, _, _ in illegal), "; ".join(notes))


def _check_naming(s: _Schema) -> _Check:
    """Имя обязательно шагу, а не каждому узлу схемы.

    `rename` принимает id активности, и именно список работ читает пользователь.
    Шлюзу и событию имя в BPMN не требуется: ветку развилки подписывает
    `conditionExpression`, а кружок события опознаётся по определению. Замер по
    корпусу реальных схем (367 файлов) давал 15380 нарушений правила, из которых
    задачами были 18 узлов — то есть снимался балл −10 почти исключительно за то,
    что нотой не требуется, а настоящая безымянная задача тонула в этом шуме.
    """
    steps = s.tasks
    if not steps:
        return _Check(NOT_APPLICABLE)
    unnamed = [e for e in steps if len((e.get("name") or "").strip()) < 3]
    named = len(steps) - len(unnamed)
    if named / len(steps) >= NAMING_MIN_SHARE:
        return _Check(PASSED, note=f"имена есть у {named} из {len(steps)} шагов")
    return _Check(FAILED, _ids(unnamed),
                  f"названия корректны у {named} из {len(steps)} шагов; имена "
                  "шагов называет автор процесса — правка `rename` по id из "
                  "списка элементов")


def _find_cycles(s: _Schema) -> List[List[str]]:
    """Все циклы дерева обхода (3-цветный DFS, O(V+E)).

    Возвращаются фундаментальные циклы по обратным рёбрам — их достаточно,
    чтобы проверить защищённость обхода, и их число ограничено числом потоков.
    Множества visited/stack не копируются, поэтому обход линеен по числу
    узлов и рёбер: на цепочке из 24 «ромбов» он считает за миллисекунды."""
    white, gray, black = 0, 1, 2
    color: Dict[str, int] = {}
    cycles: List[List[str]] = []
    for start in list(s.outgoing):
        if color.get(start, white) != white:
            continue
        color[start] = gray
        stack: List[Tuple[str, int]] = [(start, 0)]
        path = [start]
        while stack:
            node, idx = stack[-1]
            targets = s.out_targets(node)
            if idx >= len(targets):
                color[node] = black
                stack.pop()
                path.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = targets[idx]
            state = color.get(nxt, white)
            if state == gray:
                cycles.append(path[path.index(nxt):] + [nxt])
            elif state == white:
                color[nxt] = gray
                stack.append((nxt, 0))
                path.append(nxt)
    return cycles


def _is_guarded(s: _Schema, cycle: List[str]) -> bool:
    """Цикл легитимен (rework «снова на согласование»), если из него есть
    защищённый выход: узел цикла — exclusiveGateway минимум с двумя исходящими
    потоками, то есть веткой, по которой процесс может цикл покинуть."""
    members = set(cycle)
    for node_id in members:
        element = s.node_by_id.get(node_id)
        if element is None or _local(element.tag) != "exclusiveGateway":
            continue
        if len(s.out_targets(node_id)) >= 2:
            return True
    return False


def _cycle_exit_edges(s: _Schema, cycle: List[str]) -> List[str]:
    """Дуги выхода из цикла: исходящие потоки его участников, ведущие мимо
    самих участников."""
    members = set(cycle)
    return [f.id for node_id in members for f in s.outgoing.get(node_id, [])
            if f.id and f.target not in members]


def _unguarded_exit_edges(s: _Schema, cycle: List[str]) -> Tuple[List[str], List[str]]:
    """(все дуги выхода из цикла, дуги без критерия).

    Дуга защищена `conditionExpression` на потоке либо атрибутом `default`
    шлюза-источника: и то и другое — названное условие, по которому процесс
    отказывается от повторного прохода. `isGuarded` — атрибут DI-планирования
    маршрута, а не ветвление исполнения, поэтому его не читаем.

    Подпись дуги здесь сознательно НЕ считается критерием, хотя оракул
    (`no_blind_rework`) её читает. Расхождение измерено на корпусе (8 схем, и
    всегда скоринг строже) и оставлено как есть: слои спрашивают в разных местах.
    Оракул проверяет, различима ли пара ног «в петлю / из петли» у развилки,
    скоринг — охраняется ли выход из самого цикла; на схеме, где решение о
    повторе принимает шлюз выше цикла, у оракула доказательство есть, а у
    скоринга его нет. Нотационное
    требование условия на ноге развилки при этом не смягчается: его сторожит
    `gateway_conditions` (−15).

    Первая пара пуста, когда у цикла дуг выхода нет вовсе: это не «повтор без
    критерия», а тупик, и за него отвечает `no_isolated`.
    """
    by_id = {f.id: f for f in s.flows}
    edges = _cycle_exit_edges(s, cycle)
    bare: List[str] = []
    for flow_id in edges:
        flow = by_id.get(flow_id)
        if flow is None or flow.conditioned:
            continue
        gateway = s.node_by_id.get(flow.source)
        if gateway is not None and gateway.get("default") == flow_id:
            continue
        bare.append(flow_id)
    return edges, bare


def _activities_of(s: _Schema, node_ids) -> List[str]:
    """Участники маршрута, которые что-то делают: событие и шлюз работу не
    повторяют, поэтому в цепочку возврата не попадают."""
    return [node_id for node_id in dict.fromkeys(node_ids)
            if node_id in s.node_by_id
            and _local(s.node_by_id[node_id].tag) in ACTIVITY_TAGS]


def _check_guarded_cycles(s: _Schema) -> _Check:
    if not s.flows:
        return _Check(NOT_APPLICABLE)
    unguarded_nodes = [node for cycle in _find_cycles(s)
                       if not _is_guarded(s, cycle) for node in cycle]
    if not unguarded_nodes:
        return _Check(PASSED)
    nodes = tuple(dict.fromkeys(unguarded_nodes))
    return _Check(FAILED, nodes,
                  f"повтор без критерия выхода у {len(nodes)} шагов: "
                  + ", ".join(f"'{n}'" for n in nodes[:RECIPES_IN_MESSAGE])
                  + (f"; и ещё {len(nodes) - RECIPES_IN_MESSAGE}"
                     if len(nodes) > RECIPES_IN_MESSAGE else "")
                  + " — текст условия на дуге называет автор процесса")


def _nodes_reaching_join(s: _Schema) -> Set[str]:
    """Узлы, из которых по sequence-потокам достижим «сход»: конечное событие
    либо узел с двумя и более входящими потоками (парный шлюз схождения,
    сливающийся шаг или объединяющее событие).

    Считается одним обратным обходом от всех сходящихся узлов сразу, а не
    отдельным обходом от каждой ветки каждого шлюза: иначе проверка стоила бы
    O(веток × размер графа) и деградировала бы на развилках."""
    join_points = {node_id for node_id, flows in s.incoming.items() if len(flows) >= 2}
    join_points |= {e.get("id") or "" for e in s.end_events} - {""}
    reached = set(join_points)
    queue = deque(join_points)
    while queue:
        node_id = queue.popleft()
        for previous in s.preceding.get(node_id, ()):
            if previous and previous not in reached:
                reached.add(previous)
                queue.append(previous)
    return reached


def _check_gateway_split_join(s: _Schema) -> _Check:
    """Развилка без схождения оставляет висящий маршрут: процесс по одной ветке
    либо уходит в никуда, либо идёт параллельно сам с собой дальше."""
    splits = [g for tag in SPLIT_GATEWAY_TAGS for g in s.by_tag[tag]]
    branching = [g for g in splits if len(s.out_targets(g.get("id") or "")) >= 2]
    if not branching:
        return _Check(NOT_APPLICABLE)
    reached = _nodes_reaching_join(s)
    open_gateways: List[ET.Element] = []
    open_branches: List[str] = []
    for gateway in branching:
        targets = [t for t in s.out_targets(gateway.get("id") or "") if t not in reached]
        if targets:
            open_gateways.append(gateway)
            open_branches.extend(targets)
    if not open_gateways:
        return _Check(PASSED)
    legs = list(dict.fromkeys(open_branches))[:RECIPES_IN_MESSAGE]
    return _Check(FAILED, _ids(open_gateways) + tuple(dict.fromkeys(open_branches)),
                  note=f"ветки не сходятся у {len(open_gateways)} шлюзов: ноги "
                  + ", ".join(f"'{leg}'" for leg in legs)
                  + (f"; и ещё {len(open_branches) - len(legs)}"
                     if len(open_branches) > len(legs) else "")
                  + " — узел, в котором ноги должны сойтись, называется только "
                  "по смыслу процесса")


def _check_element_count(s: _Schema) -> _Check:
    count = len(s.flow_nodes)
    if count <= 50:
        return _Check(PASSED)
    # Что именно сокращать — решение автора, но кандидатов правило обязано
    # назвать: самые плотные пулы схемы (по узлам процесса).
    dense = sorted(((len(nodes), p.get("id") or "?", _participant_label(p))
                    for p in s.participants
                    for nodes in [s.nodes_of_participant(p)]), reverse=True)
    where = ", ".join(f"'{pid}' («{name}») — {n} узлов"
                      for n, pid, name in dense[:RECIPES_IN_MESSAGE])
    return _Check(FAILED, note=f"найдено {count} элементов; плотнее всего: "
                  f"{where or 'пулов нет'} — что сливать (`merge_participants`) "
                  "и что удалять, называет автор процесса")


def _check_no_isolated(s: _Schema) -> _Check:
    """Тупик — тоже нарушение связности: узлу нужны и вход, и выход.

    Исключения по семантике BPMN: у стартового события входящего потока нет в
    принципе — его «запускает» процесс; конечному достаточно входа. Граничные
    события отсюда исключены целиком: их связность (хозяин + ветка обработки)
    отвечает правило `boundary_events`, и висящее без исходящего потока
    граничное событие снимало бы вес дважды за одну и ту же ошибку."""
    if not s.flow_nodes:
        return _Check(NOT_APPLICABLE)
    dead_ends = []
    for node in s.flow_nodes:
        tag = _local(node.tag)
        if tag == "boundaryEvent":
            continue
        node_id = node.get("id") or ""
        has_in = bool(s.incoming.get(node_id))
        has_out = bool(s.outgoing.get(node_id))
        if tag == "startEvent":
            ok = has_out
        elif tag == "endEvent":
            ok = has_in
        else:
            ok = has_in and has_out
        if not ok:
            dead_ends.append(node)
    if not dead_ends:
        return _Check(PASSED)
    listed = "; ".join(
        f"'{node.get('id') or '?'}' — нет "
        + " и ".join(word for word, ok in (
            ("входа", s.incoming.get(node.get("id") or "")),
            ("выхода", s.outgoing.get(node.get("id") or ""))) if not ok)
        for node in dead_ends[:RECIPES_IN_MESSAGE])
    return _Check(FAILED, _ids(dead_ends),
                  note=f"{len(dead_ends)} узлов вне маршрута: {listed}"
                  + (f"; и ещё {len(dead_ends) - RECIPES_IN_MESSAGE}"
                     if len(dead_ends) > RECIPES_IN_MESSAGE else "")
                  + " — соседний шаг для `connect` называется только по смыслу "
                  "процесса")


def _check_boundary_events(s: _Schema) -> _Check:
    """Граничное событие живёт только вместе с хозяином: `attachedToRef` на
    существующую активность и хотя бы один исходящий поток — ветка обработки,
    иначе сигнал сорвётся в никуда."""
    if not s.boundary_events:
        return _Check(NOT_APPLICABLE)
    orphaned = []
    for event in s.boundary_events:
        host = s.node_by_id.get(event.get("attachedToRef") or "")
        if host is None or _local(host.tag) not in ACTIVITY_TAGS:
            orphaned.append(event)
        elif not s.outgoing.get(event.get("id") or ""):
            orphaned.append(event)
    if not orphaned:
        return _Check(PASSED)
    return _Check(FAILED, _ids(orphaned),
                  note=f"без хозяина или без ветки обработки {len(orphaned)} "
                       f"из {len(s.boundary_events)} событий")


def _task_type_evidence(s: _Schema) -> Dict[str, str]:
    """Шаги, чей тип читается из структуры коллаборации, а не из фантазии.

    Узел, у которого есть поток сообщения наружу, — отправитель (`sendTask`),
    узел, которому сообщением отвечают, — получатель (`receiveTask`): это
    свойство дуг между пулами, а не домысел об описании процесса. Остальным
    тип называет автор, и совет не вправе его выдумывать.
    """
    evidence: Dict[str, str] = {}
    for flow in s.by_tag["messageFlow"]:
        src, dst = flow.get("sourceRef") or "", flow.get("targetRef") or ""
        for ref, kind in ((src, "sendTask"), (dst, "receiveTask")):
            node = s.node_by_id.get(ref)
            if node is None or _local(node.tag) not in TASK_TYPE_TAGS:
                continue
            # Отвечающий шагу получатель важнее его же отправки: у шага,
            # принимающего и отправляющего одновременно, читаем приём ответа.
            if evidence.get(ref) == "receiveTask":
                continue
            evidence[ref] = kind
    return evidence


def _check_task_types(s: _Schema) -> _Check:
    """Все шаги одного рода (`task`) — схема не рассказывает, кто работу делает,
    а кто только ждёт или отправляет.

    Класс самый массовый в корпусе (200 схем из 367, 1807 родовых шагов) и
    до сих пор был неисправим: у аплайера не было операции, меняющей тип
    существующего узла, поэтому модели оставалось `delete` + `add_task` — с
    новым id, потерянной дорожкой и потерянной ссылкой в диаграмме. Теперь
    правка есть (`set_task_type`), и там, где тип читается из коллаборации,
    совет называет его операндом.
    """
    if not s.tasks:
        return _Check(NOT_APPLICABLE)
    kinds = {_local(t.tag) for t in s.tasks}
    if len(kinds) >= 2:
        return _Check(PASSED)
    evidence = _task_type_evidence(s)
    named = [t for t in s.tasks if t.get("id") in evidence]
    rest = [t for t in s.tasks if t.get("id") not in evidence]
    notes = [f"использован только тип «{sorted(kinds)[0]}»"]
    for task in named[:RECIPES_IN_MESSAGE]:
        tid = task.get("id") or ""
        notes.append(f"set_task_type(id='{tid}', task_type='{evidence[tid]}') — "
                     f"у шага '{tid}' есть поток сообщения, поэтому тип читается "
                     "из коллаборации")
    if len(named) > RECIPES_IN_MESSAGE:
        notes.append(f"и ещё {len(named) - RECIPES_IN_MESSAGE} шагу с тем же "
                     "признаком")
    if rest:
        notes.append(f"у {len(rest)} другого шага тип из структуры не читается: "
                     "его называет автор процесса по описанию")
    return _Check(FAILED, _ids(s.tasks), "; ".join(n for n in notes if n))


def _check_pool_lanes(s: _Schema) -> _Check:
    """Пустая дорожка — это не «есть хотя бы одна нормальная», а роль, с которой
    не связан ни один шаг: одна заполненная дорожка больше не прикрывает
    десять пустых.

    Схемы вообще без дорожек остаются провалом, как и раньше: генератор пока
    не раскладывает роли по laneSet, и без этого правила потолок качества
    в 100 баллов стал бы недостижимым для человека, но достижимым для схемы,
    где ролей нет вовсе."""
    lanes = [lane for lane_set in s.lane_sets for lane in _children(lane_set, "lane")]
    if not lanes:
        pool = s.participants[0].get("id") if s.participants else "?"
        return _Check(FAILED, (pool,) if pool != "?" else (), note=(
            "дорожек нет ни в одном процессе: `add_lane` требует пул и имя роли, "
            f"пул — '{pool}', а роли называет автор процесса"))
    empty_lanes = [lane for lane in lanes if not _children(lane, "flowNodeRef")]
    if empty_lanes:
        listed = ", ".join(f"'{lane.get('id') or '?'}' «{_participant_label(lane)}»"
                           for lane in empty_lanes[:RECIPES_IN_MESSAGE])
        return _Check(FAILED, _ids(empty_lanes),
                      note=f"без элементов {len(empty_lanes)} из {len(lanes)} "
                      f"дорожек: {listed}"
                      + (f"; и ещё {len(empty_lanes) - RECIPES_IN_MESSAGE}"
                         if len(empty_lanes) > RECIPES_IN_MESSAGE else "")
                      + " — работу в них перекладывает (`move_to_lane`) или "
                      "снимает сам автор процесса")
    return _Check(PASSED)


def _check_role_pools(s: _Schema) -> _Check:
    """Пул, чьё имя — дорожка чужого процесса, — роль, раздутая в участника.

    Генератор такие пулы сворачивает в дорожки (`_merge_role_pools`), а
    импортированная или нарисованная руками схема, где «Кладовщик» стоит вторым
    «участником» рядом с «ВкусВиллом», проходила скоринг как коллаборация двух
    организаций. Признак структурный и описания читать не требует: имя
    участника совпало с именем дорожки другого процесса.

    Приёмник подсказывается тем же признаком: это пул того процесса, где лежит
    одноимённая дорожка. Без него совет «сливай» невыполним — на схеме из восьми
    пулов модель вынуждена угадывать организацию."""
    if len(s.participants) < 2:
        return _Check(NOT_APPLICABLE, note="пулов меньше двух")
    offenders: List[ET.Element] = []
    pairs: List[str] = []
    for participant in s.participants:
        name = (participant.get("name") or "").strip().casefold()
        own = participant.get("processRef") or ""
        if not name:
            continue
        for pid, lanes in s.lane_names_by_process.items():
            if pid == own or name not in [lane.casefold() for lane in lanes]:
                continue
            # Приёмник берётся тем же структурным признаком: пул того процесса,
            # где лежит одноимённая дорожка.
            receiver = next(((p.get("name") or p.get("id") or "")
                             for p in s.participants
                             if (p.get("processRef") or "") == pid), "")
            offenders.append(participant)
            pairs.append(f"{participant.get('name')} → {receiver}")
            break
    if offenders:
        return _Check(FAILED, _ids(offenders), note=", ".join(pairs))
    return _Check(PASSED)


def _definition_options(event: ET.Element) -> str:
    """Какие определения можно предложить этому безымянному событию.

    `timer` внутри `intermediateCatchEvent` делает его событием, которое будит
    только таймер: сообщение, которого процесс ждал, ждать перестают. Формальное
    нарушение `event_types` этим снимается, а бизнес-ожидание исчезает, и
    `wait_without_sla` перестаёт считать узел ожиданием — то есть подсказка
    одного правила открывает дыру в другом. Тот же запрет записан в
    `_deadline_hint` и в докстринге операции `add_event_definition`, поэтому
    перечисление здесь — не «все допустимые нотой», а «не вредящие маршруту».
    """
    if _local(event.tag) == "intermediateCatchEvent":
        return "message|error|signal"
    return "timer|message|error|signal"


def _definition_choice(event: ET.Element) -> str:
    """Одно определение, которое рецепт называет операндом.

    Перечисление `event_definition='message|error|signal'` аплайер отвергает как
    неизвестное значение, а подсказка уходит в GigaChat дословно: модель,
    скопировавшая рецепт буквальнее всех, правку не приносит, и
    `improve/op_acceptance` записывает это качеством модели. Поэтому вызов
    операции называет самое частое из допустимых этим событием значений, а
    остальные и оговорку про таймер правило говорит словами (см. хвост
    подсказки) — выбор по-прежнему остаётся автору процесса.
    """
    options = _definition_options(event).split("|")
    return "message" if "message" in options else options[0]


def _check_event_types(s: _Schema) -> _Check:
    """Тип события в BPMN 2.0 — дочерний элемент (*EventDefinition); атрибута
    eventDefinitionRef у catch/throw-событий нет, поэтому читаем детей
    промежуточных и граничных событий — и каждого, а не «хотя бы одного на
    схему»: иначе одно таймер-событие оправдывало безликие ожидания сообщений.

    Схемы без таких событий — not_applicable: проверять нечего, а штраф −8
    висел бы на любой схеме, где ожидания не моделируются вовсе."""
    if not s.intermediate_events:
        return _Check(NOT_APPLICABLE)
    untyped = [event for event in s.intermediate_events
               if not any(_local(child.tag).endswith("EventDefinition")
                          for child in event)]
    if untyped:
        listed = untyped[:RECIPES_IN_MESSAGE]
        recipes = "; ".join(
            f"'{e.get('id') or ''}': add_event_definition("
            f"id='{e.get('id') or ''}', event_definition='{_definition_choice(e)}')"
            for e in listed)
        tail = ("" if len(untyped) <= RECIPES_IN_MESSAGE else
                f" и ещё {len(untyped) - RECIPES_IN_MESSAGE} с тем же рецептом")
        admissible = sorted({o for e in listed
                             for o in _definition_options(e).split("|")})
        return _Check(FAILED, _ids(untyped),
                      note=f"без типа {len(untyped)} из {len(s.intermediate_events)} "
                           f"событий: {recipes}{tail}"
                           f"; у этих узлов допустимы {', '.join(admissible)} — "
                           "определение выбирает автор по смыслу шага, а таймер на "
                           "самом ожидании снимает нарушение ценой удалённого "
                           "ожидания (срок задают граничным таймером на шаге)")
    found = {
        _local(child.tag)[: -len("EventDefinition")]
        for event in s.intermediate_events
        for child in event
        if _local(child.tag).endswith("EventDefinition")
    }
    return _Check(PASSED, note="типы событий: " + ", ".join(sorted(found)))


def _check_timer_without_schedule(s: _Schema) -> _Check:
    """Таймер без хронометража не заведётся никогда.

    `<timerEventDefinition/>` (и то же с пустым значением) исполнитель игнорирует:
    ветка «срок вышел» нарисована, стрелки на месте, эскалация выглядит
    готовой — и не наступает. Предыдущие правила про события (`event_types`,
    `event_definitions`) читали наличие определения, а не его значение, поэтому
    такой таймер проходил и скоринг, и оракул. Замер по корпусу: 108 схем из 367
    держат минимум один мёртвый таймер.

    Правка исполнимая: `add_event_definition` с `duration` подставляет срок
    этому же узлу, не подменяя тип события (аплайер раньше отказывал по
    «определение уже есть»).
    """
    events = [event for tag in EVENT_TAGS for event in s.by_tag[tag]]
    timers = [(event, child) for event in events for child in event
              if isinstance(child.tag, str)
              and _local(child.tag) == "timerEventDefinition"]
    if not timers:
        return _Check(NOT_APPLICABLE)
    inert = [event for event, definition in timers if not timer_schedule(definition)]
    if not inert:
        return _Check(PASSED, note=f"расписание есть у всех {len(timers)} таймеров")
    listed = inert[:RECIPES_IN_MESSAGE]
    recipes = "; ".join(
        f"'{e.get('id') or ''}': add_event_definition("
        f"id='{e.get('id') or ''}', event_definition='timer', "
        f"duration='{DEFAULT_TIMER_DURATION}')"
        for e in listed)
    tail = ("" if len(inert) <= RECIPES_IN_MESSAGE else
            f" и ещё {len(inert) - RECIPES_IN_MESSAGE} с тем же рецептом")
    return _Check(FAILED, _ids(inert),
                  note=f"таймер без timeDate/timeDuration/timeCycle — {recipes}{tail}"
                       f"; срок {DEFAULT_TIMER_DURATION} взят по умолчанию, "
                       "хронометраж по SLA процесса называет автор")


def _check_documentation(s: _Schema) -> _Check:
    """Покрытие текстом меряется по тем же узлам, о которых говорит совет.

    Подсказка правила просит `add_documentation` для шагов, и промпт генерации
    требует текст у `task`/`serviceTask`. Доля же считалась по всем flow-узлам:
    по корпусу это 18552 «нарушителей», из них задач 1828 — остальные события и
    шлюзы, которым описание не нужно и правка на них не идёт. Мера, которую
    легальным пакетом не устранить, — это не находка, а шум.
    """
    steps = s.tasks
    if not steps:
        return _Check(NOT_APPLICABLE)
    undocumented = [e for e in steps if not s.has_documentation(e)]
    documented = len(steps) - len(undocumented)
    if documented / len(steps) >= 0.5:
        return _Check(PASSED,
                      note=f"текст есть у {documented} из {len(steps)} шагов")
    return _Check(FAILED, _ids(undocumented),
                  f"документация есть у {documented} из {len(steps)} шагов: "
                  + ", ".join(f"'{i}'" for i in _ids(undocumented)[:RECIPES_IN_MESSAGE])
                  + (f"; и ещё {len(undocumented) - RECIPES_IN_MESSAGE}"
                     if len(undocumented) > RECIPES_IN_MESSAGE else "")
                  + " — текст описания называет автор процесса")


def _pools_of_endpoint(s: _Schema, ref: str) -> List[ET.Element]:
    """Пулы, к которым принадлежит конец messageFlow: узел отвечает через свой
    процесс, ссылка на участник — через самого себя. Разрешение то же, что у
    `participant_interacts`, — иначе совет называл бы id шага там, где в
    операцию идёт имя пула."""
    participants = [p for p in s.participants if (p.get("id") or "") == ref]
    return participants or s.participants_of_node(ref)


def _longest_user_task_line(s: _Schema, users: Set[str]) -> List[str]:
    """Самая длинная линия подряд идущих ручных задач.

    «Подряд» — по потокам исполнения: шлюз (любой) и не-ручной шаг линию
    разрывают, потому что развилка — это решение, а автоматическая задача не
    «ещё один согласующий». Возврат в уже посещённую задачу линию не продлевает:
    цикл считает `rework_loop`, а не эскалация, и без этого ограничения обход на
    «A → B → A» не закончился бы. Результат узла кэшируется только когда обход
    возврата не задел: он зависит от пути, и иначе одна и та же схема получала
    бы разную длину линии в зависимости от порядка обхода."""
    best: Dict[str, List[str]] = {}

    def walk(node_id: str, seen: Set[str]) -> Tuple[List[str], bool]:
        if node_id in best:
            return best[node_id], True
        longest = [node_id]
        exact = True
        for target in s.out_targets(node_id):
            if target not in users:
                continue
            element = s.node_by_id.get(target)
            if element is None or _local(element.tag) not in HUMAN_TASK_TAGS:
                continue
            if target in seen:
                exact = False
                continue
            tail, tail_exact = walk(target, seen | {node_id})
            exact = exact and tail_exact
            if len(tail) + 1 > len(longest):
                longest = [node_id] + tail
        if exact:
            best[node_id] = longest
        return longest, exact

    lines = (walk(node_id, {node_id})[0] for node_id in sorted(users))
    return max(lines, key=len, default=[])


def _check_rework_loop(s: _Schema) -> _Check:
    """Цикл с работой внутри и без единого критерия выхода: решение о повторе
    никем не принято, процесс крутится, пока не надоест.

    Сигнал структурный, а не по слову «на доработку» в имени: нарушителем
    считается цикл, в котором есть хотя бы одна активность (`ACTIVITY_TAGS`) и
    где НИ НА ОДНОЙ дуге, уходящей из участников цикла мимо самого цикла, нет
    ни `conditionExpression`, ни атрибута `default` на шлюзе-источнике. Почему
    подпись дуги здесь не засчитывается — расхождение с оракулом по этому месту
    разобрано в `_unguarded_exit_edges`.

    Одной защищённой ноги достаточно: легитимный rework «согласовать →
    доработать → снова согласовать» выходит из цикла по ветке с условием, и
    штрафовать его за безымянную вторую ногу значило бы требовать условие у
    потока, уходить по которому процессу не нужно. Наличие развилки как таковой
    сторожит `guarded_cycles` (−10), здесь же спрашивается имя критерия."""
    cycles = _find_cycles(s)
    if not cycles:
        return _Check(NOT_APPLICABLE, note="циклов нет")
    activities: List[str] = []
    bare: List[str] = []
    for cycle in cycles:
        edges, unguarded = _unguarded_exit_edges(s, cycle)
        members = _activities_of(s, cycle)
        # Пустой список дуг — тупик, а не незащищённый повтор: `no_isolated`.
        if not members or not edges or len(unguarded) != len(edges):
            continue
        activities.extend(members)
        bare.extend(unguarded)
    if not activities:
        return _Check(PASSED)
    activities = list(dict.fromkeys(activities))
    bare = list(dict.fromkeys(bare))
    # Дуга названа вызовом, а не перечнем id: `add_condition` принимает только
    # ветку исключающего шлюза, и без операнда совет читается как «придумай id»
    # (перепись `eval/advice.py`: 0/4 применимых пакетов на реальной форме).
    by_flow = {f.id: f for f in s.flows}

    def source_tag(flow_id: str) -> str:
        flow = by_flow.get(flow_id)
        node = s.node_by_id.get(flow.source) if flow else None
        return _local(node.tag) if node is not None else ""

    guardable = [fid for fid in bare if source_tag(fid) == "exclusiveGateway"]
    if guardable:
        listed = guardable[:RECIPES_IN_MESSAGE]
        recipes = "; ".join(f"add_condition(flow='{flow_id}', "
                            "condition='критерий отказа от повтора')"
                            for flow_id in listed)
        tail = ("" if len(guardable) <= RECIPES_IN_MESSAGE else
                f" и ещё {len(guardable) - RECIPES_IN_MESSAGE} с тем же рецептом")
        note = (f"повтор {_join_ids(tuple(activities))} без критерия выхода на "
                f"дугах {_join_ids(tuple(bare))}: {recipes}{tail}")
    else:
        # Не каждую развилку операцией починишь: `add_condition` и `set_default`
        # работают по ветке исключающего шлюза, а здесь цикл отпускает параллельная
        # (или условная) развилка, и подсказать исполнимую правку нельзя. Честнее
        # назвать причину, чем советовать то, что аплайер отвергнет.
        sources = sorted({source_tag(fid) for fid in bare} - {""})
        note = (f"повтор {_join_ids(tuple(activities))} без критерия выхода на "
                f"дугах {_join_ids(tuple(bare))}: дуги выходят из "
                f"{', '.join(sources) or 'задачи'} — условие ставится ветке "
                "исключающего шлюза, поэтому правка не выражается операцией "
                "и делается в конструкторе")
    return _Check(FAILED, tuple(activities), note=note)


def _check_handoff_pingpong(s: _Schema) -> _Check:
    """Работа возвращается в тот же шаг другого пула: ответственность не
    разведена, а перекладывается.

    Признак — взаимный обмен по ОДНОЙ и той же паре шагов (`A3 → Q1` и
    `Q1 → A3`): ответ приходит не следующему шагу, а тому же, который запрашивал.
    Прежняя редакция правила считала перекидыванием любую двустороннюю переписку
    двух пулов, даже когда туда и назад идут разные документы, — на эталонной
    складской схеме это «заказ транспорта» и «подпись получателя» между Складом и
    Перевозчиком, то есть нормальная коллаборация. Ложное нарушение стоит модели
    пакет: совет «слейте пулы в дорожку» на такой паре применим только ценой
    участника, которого сценарий требует.

    Критерий совпадает с независимым инвариантом оракула `pools_not_pingpong`
    намеренно: расхождение линеек по этому признаку означало бы, что продукт меряет
    себя сам, а проверка ему подыгрывает.
    """
    if len(s.participants) < 2:
        return _Check(NOT_APPLICABLE, note="пулов меньше двух")
    if not s.by_tag["messageFlow"]:
        return _Check(NOT_APPLICABLE, note="потоков сообщений нет")
    # (шаг, шаг) -> id потоков: причина должна называть дуги, которые `disconnect`
    # принимает аргументом, а не описывать их словами.
    edges: Dict[Tuple[str, str], List[str]] = {}
    for flow in s.by_tag["messageFlow"]:
        src, dst = flow.get("sourceRef") or "", flow.get("targetRef") or ""
        if src and dst:
            edges.setdefault((src, dst), []).append(flow.get("id") or "")
    # Пул-пара (имена) -> пары шагов, возвращающиеся в тот же узел.
    mutual: Dict[frozenset, Set[Tuple[str, str]]] = {}
    ids_by_name = {s.name_of(p): (p.get("id") or "") for p in s.participants}
    for (src, dst) in sorted(edges):
        if src >= dst or (dst, src) not in edges:
            continue
        for p1 in _pools_of_endpoint(s, src):
            for p2 in _pools_of_endpoint(s, dst):
                n1, n2 = s.name_of(p1), s.name_of(p2)
                if n1 and n2 and n1 != n2:
                    mutual.setdefault(frozenset((n1, n2)), set()).add((src, dst))
    notes: List[str] = []
    offenders: List[str] = []
    for pair, exchanges in sorted(mutual.items(),
                                  key=lambda kv: tuple(sorted(kv[0]))):
        a, b = sorted(pair)
        # id потоков в причине: `disconnect` и `connect` берут именно их, а не
        # «найдите дугу между A3 и Q1» словами.
        detail = "; ".join(_pingpong_repair(s, edges, src, dst)
                           for src, dst in sorted(exchanges))
        notes.append(f"{a} ↔ {b}: возврат в тот же шаг ({detail})")
        offenders.extend([ids_by_name.get(a, ""), ids_by_name.get(b, "")])
    if not notes:
        return _Check(PASSED)
    return _Check(FAILED, tuple(i for i in dict.fromkeys(offenders) if i),
                  note="; ".join(notes))


def _pingpong_repair(s: _Schema, edges: Dict[Tuple[str, str], List[str]],
                     src: str, dst: str) -> str:
    """Как перевесить ответ этой пары, из id, которые есть в этой же строке.

    Совет «ответ — следующему шагу» без имени этого шага модель выразить
    операцией не может: остаётся `disconnect`, который теряет ответ вовсе, а
    `improve/op_acceptance` считает это отказом пакета. Дугой ответа считаем
    поток `dst → src`: он возвращается в того, кто запросил, — ровно то, что
    правило называет работой без одного хозяина.
    """
    both = [f for f in edges[(src, dst)] + edges[(dst, src)] if f]
    back = [f for f in edges[(dst, src)] if f]
    header = f"{src} ↔ {dst} [{', '.join(both)}]"
    if not back:
        return header
    pool_side = next((end for end in (src, dst) if end not in s.node_by_id), "")
    if pool_side:
        # Обмен концом на участник целиком: «следующий шаг» называть не по чему,
        # а выдуманный id аплайер отвергнет как несуществующий элемент.
        return (f"{header}: дуга '{back[0]}' идёт на участника '{pool_side}' "
                "целиком — назовите шаг его пула, который примет ответ")
    receivers = s.out_targets(src)
    if not receivers:
        return (f"{header}: у '{src}' нет следующего шага, ответ принять нечему "
                f"— сливайте участников операцией merge_participants")
    return (f"{header}: ответ '{back[0]}' возвращается в '{src}', а его "
            f"следующий шаг '{receivers[0]}' — disconnect(flow='{back[0]}') + "
            f"connect(source='{dst}', target='{receivers[0]}', "
            "flow_type='message')")


def _check_approval_chain(s: _Schema) -> _Check:
    """Цепочка согласований: `APPROVAL_CHAIN_MIN` ручных задач подряд в одной
    дорожке и ни одного шлюза между ними.

    Считаются ручные шаги — `userTask`, `manualTask` и безымянный по типу `task`
    (так шаг рисует bpmn-js): работу машины последовательностью не портишь, а
    возврат в ту же задачу — это `rework_loop`. Дорожка обязана быть названа:
    совет «разнести по дорожкам» без адресата невыполним, а `add_lane` и
    `move_to_lane` берут имя.

    Процесс без единой дорожки раньше выходил `not_applicable`, и на всём
    реальном корпусе (367 схем) правило не сработало ни разу: оно ловило
    согласований только там, где разделение уже нарисовали, — то есть ровно
    никогда. Между тем цепочка из четырёх подписей в одном пуле без дорожек —
    худший случай (исполнители не различимы вовсе), так что бездорожный процесс
    считается по всем своим `userTask`, а совет говорит «заведите дорожки», а не
    «перенесите в существующие».
    """
    tracked: List[str] = []
    notes: List[str] = []
    offenders: List[str] = []
    lanes = sorted(s.lane_by_id)
    for lane_id in lanes:
        users = {node_id for node_id, owner in s.lane_of_node.items()
                 if owner == lane_id
                 and node_id in s.node_by_id
                 and _local(s.node_by_id[node_id].tag) in HUMAN_TASK_TAGS}
        if len(users) < APPROVAL_CHAIN_MIN:
            continue
        tracked.append(lane_id)
        line = _longest_user_task_line(s, users)
        if len(line) >= APPROVAL_CHAIN_MIN:
            offenders.extend(line)
            # Развилка ставится между двумя соседними подписями той же линии:
            # `add_gateway` требует `after` и `to`, и без пары id совет
            # «добавьте шлюз» нечем заполнить (перепись `eval/advice.py`).
            mid = len(line) // 2
            notes.append(
                f"«{s.lane_name(lane_id)}»: {_join_ids(tuple(line))} — "
                f"add_gateway(id='{_free_new_id(s, 'new_gate')}', "
                f"name='Нужен второй согласующий?', "
                f"gateway_type='exclusive', "
                f"after='{line[mid - 1]}', to='{line[mid]}')")
    if not lanes:
        users = {e.get("id") for tag in HUMAN_TASK_TAGS for e in s.by_tag[tag]
                 if e.get("id")}
        if len(users) >= APPROVAL_CHAIN_MIN:
            tracked.append("")
            line = _longest_user_task_line(s, users)
            if len(line) >= APPROVAL_CHAIN_MIN:
                offenders.extend(line)
                notes.append(f"дорожек в процессе нет, четыре подписи идут одной "
                             f"линией: {_join_ids(tuple(line))} — заведите "
                             f"add_lane(id='new_lane', name='Второй согласующий', "
                             f"participant='{s.owner_of_node.get(line[0]) or ''}')"
                             f" и перенесите move_to_lane(id='{line[0]}', "
                             "lane='new_lane')")
    if not tracked:
        return _Check(NOT_APPLICABLE, note="дорожек с четырьмя задачами нет")
    if not notes:
        return _Check(PASSED)
    return _Check(FAILED, tuple(dict.fromkeys(offenders)), note="; ".join(notes))


def _check_lane_overload(s: _Schema) -> _Check:
    """Одна дорожка держит больше `LANE_OVERLOAD_SHARE` работ процесса при трёх
    и более дорожках, а при двух — больше `LANE_MONOPOLY_SHARE`: разделение по
    ролям нарисовано, а работа лежит на одном исполнителе — это узкое место
    процесса, а не «много элементов».

    Почему две дорожки не «не применимы». Прежняя калитка в три дорожки молчала
    ровно на том случае, где монополия заметнее всего: дежурная смена из
    «инженера» и «руководителя», где на инженере 4 работы из 5 (80%). Оракул
    (`no_overloaded_lane`) такую раскладку ловит, скоринг пропускал, и контур
    улучшения не получал подсказки там, где её ждал бизнес-смысл. Порог для двух
    дорожек выше (75% против 60%): там перевес 2:1 — обычное «делает / ждёт»,
    а не узкое место.

    В знаменателе только работы (`ACTIVITY_TAGS`) тех дорожек, что перечислены в
    `laneSet`: «60% от чего» без этого не читается, а шаг без дорожки не
    принадлежит ни одной из них. Дорожка без единой работы в списке остаётся:
    100% на одном исполнителе — самый сильный случай перегруза, и молчать про
    него, потому что соседние дорожки пусты (за это отдельно отвечает
    `pool_lanes`), значит терять главный сигнал правила."""
    if not any(len(lanes) >= 2 for lanes in s.lanes_by_process.values()):
        return _Check(NOT_APPLICABLE, note="процессов с двумя и более дорожками нет")
    offenders: List[str] = []
    notes: List[str] = []
    for process_id, lanes in sorted(s.lanes_by_process.items()):
        if len(lanes) < 2:
            continue
        bar = LANE_OVERLOAD_SHARE if len(lanes) >= 3 else LANE_MONOPOLY_SHARE
        counts: List[Tuple[int, str]] = []
        work_of: Dict[str, Tuple[str, ...]] = {}
        for lane in lanes:
            lane_id = lane.get("id") or ""
            work = _activities_of(
                s, [node_id for node_id, owner in s.lane_of_node.items()
                    if owner == lane_id])
            work_of[lane_id] = tuple(work)
            counts.append((len(work), lane_id))
        total = sum(count for count, _ in counts)
        if not total:
            continue
        for count, lane_id in counts:
            share = count / total
            if share <= bar:
                continue
            offenders.append(lane_id)
            # `move_to_lane` берёт id шага и id/имя дорожки, поэтому работы
            # названы вызовами, а не перечнем id: перепись `eval/advice.py` на
            # реальных схемах не находила здесь ни одной применимой операции.
            # приёмник — дорожка с наименьшей загрузкой, она и перечислена
            # первой в списке кандидатов.
            spare = sorted((c, lid) for c, lid in counts if lid != lane_id)
            receivers = " / ".join(
                f"«{s.lane_name(lid)}» ({lid})" for _, lid in spare[:2])
            # Сколько работ надо снять, чтобы доля упала ниже планки: совет
            # «перенеси одну» на 5 из 8 нарушение не снимает.
            need = max(1, count - int(bar * total))
            recipes = "; ".join(
                f"move_to_lane(id='{wid}', lane='{spare[0][1]}')"
                for wid in work_of[lane_id][:need][:RECIPES_IN_MESSAGE])
            if need > RECIPES_IN_MESSAGE:
                recipes += (f" и ещё {need - RECIPES_IN_MESSAGE} "
                            "той же формой")
            notes.append(
                f"«{s.lane_name(lane_id)}» ({lane_id}) — {count} из {total} работ "
                f"процесса ({round(share * 100)}%): {recipes} (в {receivers})")
    if not notes:
        return _Check(PASSED)
    return _Check(FAILED, tuple(offenders), note="; ".join(notes))


def _reachable_from(s: _Schema, start: str,
                    cache: Dict[str, Set[str]]) -> Set[str]:
    """Узлы, достижимые по sequence-потокам от `start` (без самого `start`).

    Обратный обход (`s.preceding`) для этого не годится: он отвечает на вопрос
    «кто ведёт в узел», а параллельная ветка ищётся вниз по маршруту.
    """
    if start in cache:
        return cache[start]
    seen: Set[str] = set()
    stack = [start]
    while stack:
        for flow in s.outgoing.get(stack.pop(), []):
            nxt = flow.target
            if nxt and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    cache[start] = seen
    return seen


def _has_parallel_deadline(s: _Schema, wait_id: str, timers: List[str],
                           cache: Dict[str, Set[str]]) -> bool:
    """Ожидание ограничено гонкой «срок вышел» на развилке до него.

    Форму признаёт и оракул: по BPMN граничное событие нельзя повесить на
    `intermediateCatchEvent` (оно цепляется только к активности), поэтому для
    события-ожидания легальный способ назначить срок — развилка, от которой
    параллельно уходят и ожидание, и таймер. Без этой формы правило требовало
    правку, которую аплайер физически не принимает: `add_boundary_event` на
    catch-событие отвечает «не является задачей», и нарушение не снималось ни
    одним легальным пакетом.

    Развилки — `parallelGateway`, `eventBasedGateway`, `inclusiveGateway` и
    неявный выбор узла с двумя исходящими потоками (так рисуют «ждём ответ, а
    если срок вышел — отказ»). `exclusiveGateway` не считается: его ветки
    выбираются в момент развилки, и нарисованный там таймер — это другой
    сценарий, а не срок ожидания.

    Таймер обязан быть прямой ногой развилки, иначе это чужая ветка со своим
    хронометражем, и ожидание ниже по другой ноге срока не получает. Правило
    проверяет, что срок ЗАПИСАН в модели, а не что движок прервёт ожидание:
    прерывающей семантикой обладают граничное событие и `eventBasedGateway`, и
    именно они стоят в подсказке первыми.
    """
    for fork_id, flows in s.outgoing.items():
        fork = s.node_by_id.get(fork_id)
        if fork is None or _local(fork.tag) == "exclusiveGateway":
            continue
        targets = {f.target for f in flows if f.target}
        if len(targets) < 2:
            continue
        if wait_id not in _reachable_from(s, fork_id, cache):
            continue
        for timer in targets:
            if timer not in timers:
                continue
            if wait_id not in _reachable_from(s, timer, cache):
                return True
    return False


def _pool_hint(s: _Schema, node_id: str) -> str:
    """Как назвать пул в операции: новый узел аплайер кладёт в процесс, а процесс
    он ищет по участнику. Без `participant` пакет отвечает «пул не определён», и
    перепись `eval/advice.py` ловила это на каждом втором ожидании корпуса."""
    process_id = s.owner_of_node.get(node_id) or ""
    for participant in s.participants_by_process.get(process_id, []):
        name = (participant.get("name") or participant.get("id") or "").strip()
        if name:
            return name
    return process_id or "?"


def _single_outlet_ancestor(s: _Schema, node_id: str) -> str:
    """Ближайший предок с единственным исходящим потоком.

    `add_gateway(after=X)` вставляет развилку ПОСРЕДИ дуги X→…, и если у X ног
    несколько, аплайер отвергает правку («у шлюза несколько исходящих потоков») —
    он не угадывает, какую из них резать. Подъём по предкам до узла с одной дугой
    даёт якорь, где вставка однозначна."""
    seen = {node_id}
    frontier = [node_id]
    while frontier:
        current = frontier.pop(0)
        for parent in sorted(s.preceding.get(current, [])):
            if parent in seen:
                continue
            seen.add(parent)
            if len(s.outgoing.get(parent, [])) == 1:
                return parent
            frontier.append(parent)
    return ""


def _deadline_hint(s: _Schema, wait: ET.Element, ordinal: int = 1) -> str:
    """Пакет операций, которым снимается срок именно у этого ожидания.

    Подсказка без id, из которых собирается операция, читается моделью как
    «придумай id»: аплайер отвечает отказом, а `improve/op_acceptance` записывает
    это как качество модели, хотя виноват текст правила. Активности называется
    `add_boundary_event` с её собственным id; событию-ожиданию — развилка, и
    порядок шагов в подсказке не украшение: аплайер вставляет новый узел ПОСРЕДИ
    существующей дуги (`after`), поэтому нога «срок вышел» собирается только
    парой `connect` уже после того, как шлюз забрал на себя исход предшественника.
    Без этого шага совет давал бы цепочку «ожидание → таймер → дальше», где
    ждущий токен стоит столько, сколько ему ничего не пришло.

    `ordinal` различает id новых узлов в рецептах разных ожиданий одной строки:
    на схеме с двумя незащищёнными ожиданиями второй `add_event(id='new_sla_timer')`
    отвечал «id уже занят», а его `connect` — «такой поток уже существует», то
    есть из трёх названных рецептов применимым оказывался один (замер по корпусу).

    Таймер обязан чем-то заканчиваться: нога «срок вышел» ведёт к следующему после
    ожидания шагу, иначе аплайер откатывает узел как тупик («вход есть, выхода
    нет»), а по смыслу это и есть исполнение SLA — процесс идёт дальше без
    ответа.

    `add_event_definition(event_definition='timer')` сюда не предлагается
    намеренно, хотя после появления этой операции аплайер её принимает: таймерное
    определение внутри `intermediateCatchEvent` делает его событием, которое будит
    только таймер, и сообщение, которого ждёт процесс, ждать перестают. Формальное нарушение
    правилом снимается, а бизнес-ожидание исчезает — то есть подсказка давала бы
    починку, которую оракул не ловит. Легальные формы держат ожидание на месте:
    срок идёт рядом (граничный таймер на активности или нога развилки), а не
    вместо него.
    """
    eid = wait.get("id") or "?"
    if _local(wait.tag) in ACTIVITY_TAGS:
        return (f"'{eid}': add_boundary_event(attached_to='{eid}', "
                "event_type='timer', duration='PT2H')")
    srcs = sorted({f.source for f in s.incoming.get(eid, []) if f.source})
    tgts = sorted({f.target for f in s.outgoing.get(eid, []) if f.target})
    if not srcs or not tgts:
        # Нога «срок вышел» вставается в дугу маршрута, а дуги, в которую её
        # ставить, нет: подсказка обязана сказать это прямо, а не слать за
        # развилкой «в никуда».
        missing = " и ".join(word for word, ok in (("входящей", srcs),
                                                   ("исходящей", tgts)) if not ok)
        return (f"'{eid}': развилка «ответ или срок» невозможна — у ожидания нет "
                f"{missing} дуги, правка не выражается операцией; сначала "
                "связность маршрута")
    # Id новых узлов названы, а не отданы на выдумку: аплайер требует `new_`
    # префикс и связывает операции пакета именно по ним, поэтому «connect(source
    # =этот шлюз)» читается как отказ «не задан id нового элемента». Замер по
    # корпусу: из 89 схем с этим рецептом ни один пакет не применялся.
    timer = _free_new_id(s, f"new_sla_timer_{ordinal}")
    fork_new = _free_new_id(s, f"new_sla_fork_{ordinal}")
    pool = _pool_hint(s, eid)
    timer_op = (f"add_event(id='{timer}', name='Срок ответа вышел', "
                f"event_type='timer', duration='PT2H', participant='{pool}')")
    # Если ожидание уже отходит от развилки, второй ногой которой можно закрыть
    # срок, новую развилку заводить нечего: нога «срок вышел» добавляется прямо к
    # ней — пакет короче на одну операцию.
    for fork_id in srcs:
        fork = s.node_by_id.get(fork_id)
        legs = {f.target for f in s.outgoing.get(fork_id, []) if f.target}
        if (fork is None or not _local(fork.tag).endswith("Gateway")
                or _local(fork.tag) == "exclusiveGateway" or len(legs) < 2):
            continue
        return (f"'{eid}': {timer_op}; "
                f"connect(source='{fork_id}', target='{timer}'); "
                f"connect(source='{timer}', target='{tgts[0]}')")
    # Якорь ищется вверх по маршруту до узла с единственной дугой: вставка
    # `add_gateway(after=X)` режет дугу X→…, и если у X ног несколько, аплайер
    # правку отвергает.
    anchor = _single_outlet_ancestor(s, eid) or srcs[0]
    return (f"'{eid}': {timer_op}; "
            f"add_gateway(id='{fork_new}', name='Ответ или срок?', "
            f"gateway_type='parallel', after='{anchor}', participant='{pool}'); "
            f"connect(source='{fork_new}', target='{timer}'); "
            f"connect(source='{timer}', target='{tgts[0]}')")


def _check_wait_without_sla(s: _Schema) -> _Check:
    """У каждого блокирующего ожидания должен быть срок, записанный в модели.

    Ждущий шаг — `intermediateCatchEvent` без таймер-определения ИЛИ
    `receiveTask`: токен стоит, пока не придёт сообщение. Прежняя редакция
    знала только catch-события и принимала срок, которого в BPMN для
    `receiveTask` не бывает, — ожидание подписи получателя в эталонной складской
    сцене правилу просто не существовало.

    Срок назначается тремя легальными формами: `timerEventDefinition` внутри
    самого ожидания, граничный таймер на нём (`attachedToRef` — он возможен,
    только когда ожидание вообще является активностью) и ветка «срок вышел»,
    уходящая с маршрута до ожидания. Вторую форма правила не видела и при этом
    советовала ровно `add_boundary_event` — то есть предложенная правка не
    снимала нарушение, а контур улучшения получил бы регресс за собственную
    подсказку.

    Процесс без единого таймера больше не «не применим»: «ждём ответ» без срока
    остаётся висящим маршрутом и там, где хронометраж не моделирован ни разу, —
    это и есть находка. За корректность ноты отвечает `event_types`.
    """
    def definition_of(elem: ET.Element) -> Set[str]:
        return {_local(c.tag)[: -len("EventDefinition")] for c in elem
                if isinstance(c.tag, str)
                and _local(c.tag).endswith("EventDefinition")}

    def waits_from_outside(elem: ET.Element) -> bool:
        return not (definition_of(elem) & NOT_A_WAIT_DEFINITIONS)

    blocking = [e for e in s.flow_nodes
                if _local(e.tag) == "receiveTask"
                or (_local(e.tag) == "intermediateCatchEvent"
                    and waits_from_outside(e))]
    if not blocking:
        return _Check(NOT_APPLICABLE,
                      note="блокирующих ожиданий нет (метки перехода и "
                           "триггеры отработки ожиданиями не считаются)")

    def has_timer(elem: ET.Element) -> bool:
        return any(_local(c.tag) == "timerEventDefinition" for c in elem)

    timers = [e.get("id") for e in s.flow_nodes if e.get("id") and has_timer(e)]
    reach: Dict[str, Set[str]] = {}

    def bounded(e: ET.Element) -> bool:
        if has_timer(e):
            return True
        eid = e.get("id") or ""
        return (any(_local(b.tag) == "boundaryEvent"
                    and b.get("attachedToRef") == eid and has_timer(b)
                    for b in s.flow_nodes)
                or _has_parallel_deadline(s, eid, timers, reach))

    unmeasured = [e for e in blocking if not bounded(e)]
    if not unmeasured:
        return _Check(PASSED)
    # Рецепт на каждое ожидание — самая длинная часть подсказки, а блок «УЗКИЕ
    # МЕСТА ПО СКОРИНГУ» уходит в промпт целыми строками: без потолка одна
    # находка вытесняла бы остальные правила. Остальные ожидания названы id в
    # `elements`, а правка у них ровно та же.
    listed = unmeasured[:RECIPES_IN_MESSAGE]
    tail = ("" if len(unmeasured) <= RECIPES_IN_MESSAGE else
            f" и ещё {len(unmeasured) - RECIPES_IN_MESSAGE} "
            "с тем же рецептом")
    recipe = "; ".join(_deadline_hint(s, e, k)
                       for k, e in enumerate(listed, 1)) + tail
    head = f"{len(unmeasured)} из {len(blocking)} блокирующих ожиданий без срока"
    if not timers:
        head += ", и таймеров в схеме нет ни одного: срок не моделирован ни разу"
    return _Check(FAILED, _ids(unmeasured), note=f"{head}: {recipe}")


def has_unguarded_cycle(bpmn_xml: str) -> bool:
    """Есть ли цикл, из которого нет защищённого выхода.

    Оркестратор улучшения сверяет по этому признаку «до» и «после»: пакет,
    замкнувший маршрут без ветки выхода, делает схему хуже по правилу
    `guarded_cycles`, и принимать его нельзя. Битый XML — False: проверка не
    должна превращать отказ разбора в «всё хорошо, циклов нет» там, где
    сравнивать нечего.
    """
    try:
        schema = _Schema(parse_xml(bpmn_xml))
    except Exception:  # noqa: BLE001 — нет схемы, нет и утверждения о циклах
        return False
    return any(not _is_guarded(schema, cycle) for cycle in _find_cycles(schema))


class BPMNScorer:
    """Скоринг схемы: нормированный балл, рекомендации и статус по каждому
    правилу.

    Схема не меняется: модуль только читает XML, никаких «оптимизаций»
    в ответе нет.

    Рекомендация обязана называть операцию, которой живёт её правка (хвост
    «→ чинится: rename, add_documentation»): блок «УЗКИЕ МЕСТА ПО СКОРИНГУ»
    уходит в промпт улучшения дословно, и совет, не выражаемый пакетом
    операций, — жалоба без работы. `recommendations_by_rule` отдаёт те же
    строки под именами правил: лимит промпта режет плоский список, а по нему
    не видно, какое правило осталось за обрезкой."""

    def __init__(self):
        self.rules = {
            'start_event': {'weight': 10, 'message': 'У каждого участника должно быть своё стартовое событие', 'action': ['add_event']},
            'end_event': {'weight': 10, 'message': 'У каждого участника должно быть своё событие завершения', 'action': ['add_event']},
            'pool_has_steps': {'weight': 10, 'message': 'В каждом пуле должен быть хотя бы один шаг: задача, подпроцесс или промежуточное событие', 'action': ['add_task']},
            'participant_interacts': {'weight': 8, 'message': 'Каждый участник должен быть затронут хотя бы одним потоком сообщения', 'action': ['connect']},
            'gateway_conditions': {'weight': 15, 'message': 'Каждая ветка расходящегося эксклюзивного шлюза должна иметь условие или быть помечена как default', 'action': ['add_condition', 'set_default']},
            'gateway_split_join': {'weight': 10, 'message': 'Ветки шлюза должны сходиться: каждая ветка ведёт к событию завершения или к узлу с двумя и более входящими потоками', 'action': ['add_gateway', 'connect']},
            'sequence_flows': {'weight': 10, 'message': 'Все элементы должны быть соединены последовательностями, а каждый поток — иметь оба конца в схеме', 'action': ['connect', 'disconnect']},
            'cross_pool_flow': {'weight': 10, 'message': 'Поток управления не пересекает границу процесса: узлы двух участников соединяются потоком сообщения, а не sequenceFlow', 'action': ['disconnect', 'connect']},
            'message_flow_ends': {'weight': 8, 'message': 'У каждого потока сообщения оба конца существуют в схеме и способны обмен принять: это участник или узел внутри участника, но не развилка', 'action': ['connect', 'disconnect']},
            'flow_ends_legal': {'weight': 8, 'message': 'Поток управления не входит в стартовое или граничное событие и не выходит из конечного', 'action': ['disconnect', 'connect']},
            'naming': {'weight': 10, 'message': 'Каждый шаг (активность) должен иметь осмысленное название (не короче 3 символов); шлюзу и событию имя в BPMN не требуется — ветку подписывает условие', 'action': ['rename']},
            'guarded_cycles': {'weight': 10, 'message': 'Цикл допустим только с защищённым выходом — веткой исключительного шлюза', 'action': ['add_condition', 'set_default']},
            'element_count': {'weight': 8, 'message': 'Схема не должна быть перегружена элементами (>50)', 'action': ['delete', 'merge_participants']},
            'no_isolated': {'weight': 8, 'message': 'У каждого шага должны быть и вход, и выход (у стартового — только выход, у конечного — только вход)', 'action': ['connect']},
            'boundary_events': {'weight': 6, 'message': 'Граничное событие прикрепляйте к задаче и ведите из него ветку обработки', 'action': ['add_boundary_event']},
            'task_types': {'weight': 8, 'message': 'Схема должна содержать разнообразные типы задач: родовой «task» меняет тип операцией set_task_type у того же узла (удалять шаг и заводить новый с другим id — потеря маршрута, дорожки и подписи)', 'action': ['set_task_type', 'add_task']},
            'pool_lanes': {'weight': 8, 'message': 'В пуле должны быть дорожки с закреплёнными элементами', 'action': ['move_to_lane', 'add_lane']},
            'role_pools': {'weight': 8, 'message': 'Роль или подразделение — дорожка внутри пула организации, а не отдельный участник: слейте такие пулы операцией merge_participants (source — роль, target — организация, as_lane: true)', 'action': ['merge_participants']},
            'event_types': {'weight': 8, 'message': 'Схема должна включать промежуточные события с типом (таймер, сообщение); узлу, который уже стоит в схеме, тип задаётся определением, а не новым узлом', 'action': ['add_event', 'add_event_definition']},
            'timer_without_schedule': {'weight': 8, 'message': 'У таймера должен быть хронометраж (timeDate, timeDuration или timeCycle): без него исполнитель не заведёт событие, и ветка «срок вышел» не наступит', 'action': ['add_event_definition']},
            'documentation': {'weight': 7, 'message': 'Не менее половины шагов (задач) должны содержать документацию', 'action': ['add_documentation']},
            'rework_loop': {'weight': 8, 'message': 'Повтор по кругу допустим только с названным критерием выхода: условие или выход по умолчанию на дуге, уходящей из цикла', 'action': ['add_condition', 'set_default']},
            'handoff_pingpong': {'weight': 8, 'message': 'Ответ пула обязан приходить следующему шагу, а не тому же, который запрашивал: возврат в тот же шаг — работа без одного хозяина', 'action': ['disconnect', 'connect', 'merge_participants']},
            'approval_chain': {'weight': 6, 'message': 'Четыре ручных шага (task, userTask, manualTask) подряд без развилки — цепочка согласований: разнесите её по дорожкам или добавьте шлюз; в процессе без дорожек это то же нарушение, и дорожки надо завести', 'action': ['move_to_lane', 'add_lane', 'add_gateway']},
            'lane_overload': {'weight': 6, 'message': 'Одна дорожка не должна держать больше 60% работ процесса (при двух дорожках — больше 75%)', 'action': ['move_to_lane', 'add_lane']},
            'wait_without_sla': {'weight': 8, 'message': 'У ожидания (catch-событие без таймера или receiveTask) должен быть срок в модели: таймер-определение в нём самом, граничный таймер на активности либо развилка «ответ или срок» с таймером, иначе маршрут висит', 'action': ['add_boundary_event', 'add_gateway', 'add_event']},
        }
        self._checks = {
            'start_event': _check_start_event,
            'end_event': _check_end_event,
            'pool_has_steps': _check_pool_has_steps,
            'participant_interacts': _check_participant_interacts,
            'gateway_conditions': _check_gateway_conditions,
            'gateway_split_join': _check_gateway_split_join,
            'sequence_flows': _check_sequence_flows,
            'cross_pool_flow': _check_cross_pool_flow,
            'message_flow_ends': _check_message_flow_ends,
            'flow_ends_legal': _check_flow_ends_legal,
            'naming': _check_naming,
            'guarded_cycles': _check_guarded_cycles,
            'element_count': _check_element_count,
            'no_isolated': _check_no_isolated,
            'boundary_events': _check_boundary_events,
            'task_types': _check_task_types,
            'pool_lanes': _check_pool_lanes,
            'role_pools': _check_role_pools,
            'event_types': _check_event_types,
            'timer_without_schedule': _check_timer_without_schedule,
            'documentation': _check_documentation,
            'rework_loop': _check_rework_loop,
            'handoff_pingpong': _check_handoff_pingpong,
            'approval_chain': _check_approval_chain,
            'lane_overload': _check_lane_overload,
            'wait_without_sla': _check_wait_without_sla,
        }

    def evaluate(self, bpmn_xml: str) -> Dict:
        try:
            schema = _Schema(parse_xml(bpmn_xml))
        except Exception as e:  # noqa: BLE001 — битый XML не должен ронять /api/evaluate
            return {
                'score': 0,
                'recommendations': [f'Ошибка валидации BPMN: {e}'],
                'details': {name: False for name in self.rules},
                'details_meta': {
                    name: {'status': FAILED, 'weight': rule['weight'], 'elements': []}
                    for name, rule in self.rules.items()
                },
                # Правила не исполнялись — индекса схемы нет, поэтому и
                # рекомендации по правилам нет: ошибка разбора не нарушение.
                'recommendations_by_rule': {},
            }

        recommendations: List[str] = []
        recommendations_by_rule: Dict[str, str] = {}
        details: Dict[str, bool] = {}
        details_meta: Dict[str, Dict] = {}
        earned = 0
        applicable = 0

        for name, rule in self.rules.items():
            check = self._checks[name](schema)
            if check.status != NOT_APPLICABLE:
                applicable += rule['weight']
            if check.status == PASSED:
                earned += rule['weight']
            elif check.status == FAILED:
                message = rule['message']
                if check.note:
                    message += f": {check.note}"
                if check.elements:
                    message += f" (элементы: {_join_ids(check.elements)})"
                # Хвост с операцией — часть рекомендации, а не справочник:
                # текст уходит в промпт улучшения дословно, и правило, совет
                # которого не выражается пакетом операций, стоит модели
                # единственного корректирующего повтора. `role_pools` название
                # операции уже встроен в формулировку — хвост всё равно едет.
                # Исключение — совет, который сам отказался от операции и
                # вызова не называет: «не выражается операцией … → чинится:
                # add_event» читается как разрешение выдумать операнд (по
                # корпусу таких противоречий было 312 в пяти правилах). Если
                # вызов в тексте есть для одного нарушения, а для другого
                # операции нет, хвост остаётся: правка пакету доступна.
                note = check.note or ""
                if (any(m in note for m in MANUAL_FIX_MARKERS)
                        and not _OP_CALL_RE.search(note)):
                    action_tail = ""
                else:
                    action_tail = " → чинится: " + ", ".join(rule['action'])
                message += action_tail
                recommendations.append(message)
                recommendations_by_rule[name] = message
            details[name] = check.status != FAILED
            details_meta[name] = {
                'status': check.status,
                'weight': rule['weight'],
                'elements': list(check.elements),
            }

        score = round(100 * earned / applicable) if applicable else 0
        return {
            'score': score,
            'recommendations': recommendations,
            'details': details,
            'details_meta': details_meta,
            # То же содержимое, что и `recommendations`, но с именем правила в
            # ключе: оркестратор режет блок «УЗКИЕ МЕСТА ПО СКОРИНГУ» лимитом
            # `MAX_SCORING_FINDINGS` и по плоскому списку прозу не узнает, какие
            # именно правила остались за обрезкой.
            'recommendations_by_rule': recommendations_by_rule,
        }


def diff_scores(before: Dict, after: Dict) -> Dict:
    """Дельта двух прогонов скоринга: какие правила сменили статус и насколько
    изменился балл.

    Чистая функция, чтобы отчёт принятия улучшения показывал сдвиг по правилам:
    итоговые «85 → 85» скрывают, что часть правил починена, а прирост съеден
    другим требованием. Неизвестные правила (например, удалённые между
    версиями) в дельту не попадают."""
    before_meta = before.get("details_meta") or {}
    after_meta = after.get("details_meta") or {}
    changed: Dict[str, Dict] = {}
    for name, entry in after_meta.items():
        previous = before_meta.get(name)
        if previous is None or previous.get("status") == entry.get("status"):
            continue
        changed[name] = {
            "before": previous.get("status"),
            "after": entry.get("status"),
            "weight": entry.get("weight"),
        }
    score_before = before.get("score") or 0
    score_after = after.get("score") or 0
    return {
        "score_before": score_before,
        "score_after": score_after,
        "score_delta": score_after - score_before,
        "rules": changed,
    }

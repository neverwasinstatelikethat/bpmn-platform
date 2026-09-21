# Rule-based скоринг BPMN-схем: 12 взвешенных правил, метрики качества без
# правки схемы.
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, NamedTuple, Tuple

from .bpmn_edits import (
    EVENT_TAGS,
    FLOW_NODE_TAGS,
    GATEWAY_TAGS,
    is_bpmn_tag,
    parse_xml,
)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Активности для правил связности и разнообразия задач: flow-ноды, не
# являющиеся ни шлюзами, ни событиями. В bpmn_edits в TASK_TAGS входят и
# события, поэтому множество выводится из тегов, а не пересказывается.
ACTIVITY_TAGS = FLOW_NODE_TAGS - GATEWAY_TAGS - EVENT_TAGS

PASSED = "passed"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"

# Максимум id в тексте рекомендации: список уходит пользователю во фронтенд,
# длинное сообщение там не читается.
IDS_IN_MESSAGE = 10


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
                flow = _Flow(
                    element.get("id") or "",
                    element.get("sourceRef") or "",
                    element.get("targetRef") or "",
                    any(_local(child.tag) == "conditionExpression"
                        for child in element),
                )
                self.flows.append(flow)
                if flow.source:
                    self.outgoing[flow.source].append(flow)
                if flow.target:
                    self.incoming[flow.target].append(flow)

        self.tasks = _tags_of(self.flow_nodes, ACTIVITY_TAGS)
        self.exclusive_gateways = self.by_tag["exclusiveGateway"]
        self.start_events = self.by_tag["startEvent"]
        self.end_events = self.by_tag["endEvent"]
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

    def out_targets(self, node_id: str) -> List[str]:
        return [f.target for f in self.outgoing.get(node_id, []) if f.target]

    def has_documentation(self, element: ET.Element) -> bool:
        return any(_local(child.tag) == "documentation" for child in element)


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


def _check_start_event(s: _Schema) -> _Check:
    expected = len(s.participants) or 1
    found = s.root_start_events
    if len(found) == expected:
        return _Check(PASSED)
    return _Check(FAILED, _ids(found),
                  f"ожидается {expected} стартовых событий, найдено {len(found)}")


def _check_end_event(s: _Schema) -> _Check:
    if s.end_events:
        return _Check(PASSED)
    return _Check(FAILED)


def _check_gateway_conditions(s: _Schema) -> _Check:
    """Ветви шлюза обязаны различаться: либо условие на потоке, либо поток
    назначен выходом по умолчанию атрибутом `default`."""
    if not s.exclusive_gateways:
        return _Check(NOT_APPLICABLE)
    bad_gateways = []
    bad_flows: List[str] = []
    for gateway in s.exclusive_gateways:
        outgoing = s.outgoing.get(gateway.get("id") or "", [])
        default_flow = gateway.get("default")
        unconditioned = [f.id for f in outgoing
                         if not f.conditioned and f.id != default_flow]
        if len(outgoing) < 2 or unconditioned:
            bad_gateways.append(gateway)
            bad_flows.extend(unconditioned)
    if not bad_gateways:
        return _Check(PASSED)
    return _Check(FAILED, _ids(bad_gateways) + tuple(bad_flows))


def _check_sequence_flows(s: _Schema) -> _Check:
    if not s.tasks:
        return _Check(NOT_APPLICABLE)
    unconnected = [t for t in s.tasks
                   if not s.incoming.get(t.get("id") or "")
                   or not s.outgoing.get(t.get("id") or "")]
    if not unconnected:
        return _Check(PASSED)
    return _Check(FAILED, _ids(unconnected))


def _check_naming(s: _Schema) -> _Check:
    if not s.flow_nodes:
        return _Check(NOT_APPLICABLE)
    unnamed = [e for e in s.flow_nodes
               if len((e.get("name") or "").strip()) < 3]
    named = len(s.flow_nodes) - len(unnamed)
    if named / len(s.flow_nodes) >= 0.9:
        return _Check(PASSED)
    return _Check(FAILED, _ids(unnamed),
                  f"названия корректны у {named} из {len(s.flow_nodes)} элементов")


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


def _check_guarded_cycles(s: _Schema) -> _Check:
    if not s.flows:
        return _Check(NOT_APPLICABLE)
    unguarded_nodes = [node for cycle in _find_cycles(s)
                       if not _is_guarded(s, cycle) for node in cycle]
    if not unguarded_nodes:
        return _Check(PASSED)
    return _Check(FAILED, tuple(dict.fromkeys(unguarded_nodes)))


def _check_element_count(s: _Schema) -> _Check:
    count = len(s.flow_nodes)
    if count <= 50:
        return _Check(PASSED)
    return _Check(FAILED, note=f"найдено {count} элементов")


def _check_no_isolated(s: _Schema) -> _Check:
    """Тупик — тоже нарушение связности: узлу нужны и вход, и выход.

    Исключения по семантике BPMN: у стартового события и у граничного события
    входящего потока нет в принципе — их «запускает» хозяин (activity для
    boundaryEvent), поэтому им достаточно выхода; конечному событию достаточно
    входа."""
    if not s.flow_nodes:
        return _Check(NOT_APPLICABLE)
    dead_ends = []
    for node in s.flow_nodes:
        tag = _local(node.tag)
        node_id = node.get("id") or ""
        has_in = bool(s.incoming.get(node_id))
        has_out = bool(s.outgoing.get(node_id))
        if tag in ("startEvent", "boundaryEvent"):
            ok = has_out
        elif tag == "endEvent":
            ok = has_in
        else:
            ok = has_in and has_out
        if not ok:
            dead_ends.append(node)
    if not dead_ends:
        return _Check(PASSED)
    return _Check(FAILED, _ids(dead_ends))


def _check_task_types(s: _Schema) -> _Check:
    if not s.tasks:
        return _Check(NOT_APPLICABLE)
    kinds = {_local(t.tag) for t in s.tasks}
    if len(kinds) >= 2:
        return _Check(PASSED)
    return _Check(FAILED, _ids(s.tasks), f"использован только тип «{kinds.pop()}»")


def _check_pool_lanes(s: _Schema) -> _Check:
    if not (s.collaborations or s.lane_sets):
        return _Check(FAILED)
    empty_lanes = []
    for lane_set in s.lane_sets:
        for lane in _children(lane_set, "lane"):
            if _children(lane, "flowNodeRef"):
                return _Check(PASSED)
            empty_lanes.append(lane)
    return _Check(FAILED, _ids(empty_lanes))


def _check_event_types(s: _Schema) -> _Check:
    """Тип события в BPMN 2.0 — дочерний элемент (*EventDefinition);
    атрибута eventDefinitionRef у catch/throw-событий нет, поэтому читаем детей
    промежуточных и граничных событий.

    Схемы без таких событий — not_applicable: проверять нечего, а штраф −8
    висел бы на любой схеме, которую выдаёт генератор (он пока не пишет
    eventDefinition)."""
    if not s.intermediate_events:
        return _Check(NOT_APPLICABLE)
    found = {
        _local(child.tag)[: -len("EventDefinition")]
        for event in s.intermediate_events
        for child in event
        if _local(child.tag).endswith("EventDefinition")
    }
    if found:
        return _Check(PASSED, note="типы событий: " + ", ".join(sorted(found)))
    return _Check(FAILED, _ids(s.intermediate_events))


def _check_documentation(s: _Schema) -> _Check:
    if not s.flow_nodes:
        return _Check(NOT_APPLICABLE)
    undocumented = [e for e in s.flow_nodes if not s.has_documentation(e)]
    documented = len(s.flow_nodes) - len(undocumented)
    if documented / len(s.flow_nodes) >= 0.5:
        return _Check(PASSED)
    return _Check(FAILED, _ids(undocumented),
                  f"документация есть у {documented} из {len(s.flow_nodes)} элементов")


class BPMNScorer:
    """Скоринг схемы: нормированный балл, рекомендации и статус по каждому
    правилу.

    Схема не меняется: модуль только читает XML, никаких «оптимизаций»
    в ответе нет."""

    def __init__(self):
        self.rules = {
            'start_event': {'weight': 10, 'message': 'Количество стартовых событий должно соответствовать числу участников'},
            'end_event': {'weight': 10, 'message': 'Должно быть хотя бы одно конечное событие'},
            'gateway_conditions': {'weight': 15, 'message': 'Эксклюзивные шлюзы должны иметь условия на всех исходящих потоках'},
            'sequence_flows': {'weight': 10, 'message': 'Все элементы должны быть соединены последовательностями'},
            'naming': {'weight': 10, 'message': 'Все элементы должны иметь осмысленные названия (не короче 3 символов)'},
            'guarded_cycles': {'weight': 10, 'message': 'Цикл допустим только с защищённым выходом — веткой исключительного шлюза'},
            'element_count': {'weight': 8, 'message': 'Схема не должна быть перегружена элементами (>50)'},
            'no_isolated': {'weight': 8, 'message': 'У каждого элемента должны быть и вход, и выход (у стартового и граничного — выход, у конечного — вход)'},
            'task_types': {'weight': 8, 'message': 'Схема должна содержать разнообразные типы задач'},
            'pool_lanes': {'weight': 8, 'message': 'В пуле должны быть дорожки с закреплёнными элементами'},
            'event_types': {'weight': 8, 'message': 'Схема должна включать промежуточные события с типом (таймер, сообщение)'},
            'documentation': {'weight': 7, 'message': 'Элементы должны содержать документацию'},
        }
        self._checks = {
            'start_event': _check_start_event,
            'end_event': _check_end_event,
            'gateway_conditions': _check_gateway_conditions,
            'sequence_flows': _check_sequence_flows,
            'naming': _check_naming,
            'guarded_cycles': _check_guarded_cycles,
            'element_count': _check_element_count,
            'no_isolated': _check_no_isolated,
            'task_types': _check_task_types,
            'pool_lanes': _check_pool_lanes,
            'event_types': _check_event_types,
            'documentation': _check_documentation,
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
            }

        recommendations: List[str] = []
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
                recommendations.append(message)
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
        }

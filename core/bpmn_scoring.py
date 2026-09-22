# Rule-based скоринг BPMN-схем: 16 взвешенных правил, метрики качества без
# правки схемы.
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import Dict, List, NamedTuple, Set, Tuple

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

# «Шаг» пула: работа или ожидание. Граничное событие сюда не входит — оно
# висит на активности и без неё не существует, поэтому пустой пул с одним
# только boundaryEvent всё равно остаётся пустым.
STEP_TAGS = ACTIVITY_TAGS | {"intermediateCatchEvent", "intermediateThrowEvent"}

# Шлюзы, которые разбирают маршрут в несколько веток и потому обязаны их
# собирать обратно. eventBasedGateway не берём: у него ветки сходятся по
# событиям, а не по потокам.
SPLIT_GATEWAY_TAGS = ("exclusiveGateway", "parallelGateway")

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
        # Обратные смежности (узел -> предки) достаточно для сходимости веток:
        # прямой обход «от каждой ветки своего шлюза» дал бы O(веток * граф).
        self.preceding: Dict[str, List[str]] = defaultdict(list)

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
                    if flow.source:
                        self.preceding[flow.target].append(flow.source)

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
        # Дорожки по процессам: «роль, раздутая в участника» ищется как имя
        # участника, совпавшее с дорожкой ЧУЖОГО процесса.
        self.lane_names_by_process: Dict[str, List[str]] = {}
        for process in self.by_tag["process"]:
            names = [name for lane_set in _children(process, "laneSet")
                     for lane in _children(lane_set, "lane")
                     if (name := (lane.get("name") or "").strip())]
            if names:
                self.lane_names_by_process[process.get("id") or ""] = names

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
                  note=f"без события завершения {len(tailless)} из {len(s.participants)} участников")


def _check_pool_has_steps(s: _Schema) -> _Check:
    """Пул без шагов — декорация: `startEvent → endEvent` связен по BPMN, но
    процесса как работы в нём нет, и скоринг обязан это отличать."""
    if not s.participants:
        return _Check(NOT_APPLICABLE)
    stepless = [p for p in s.participants
                if not any(_local(n.tag) in STEP_TAGS for n in s.nodes_of_participant(p))]
    if not stepless:
        return _Check(PASSED)
    return _Check(FAILED, _ids(stepless),
                  note=f"без шагов {len(stepless)} из {len(s.participants)} участников")


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
    return _Check(FAILED, _ids(silent),
                  note=f"без потоков сообщений {len(silent)} из {len(s.participants)} участников")


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
    return _Check(FAILED, _ids(open_gateways) + tuple(dict.fromkeys(open_branches)),
                  note=f"ветки не сходятся у {len(open_gateways)} шлюзов")


def _check_element_count(s: _Schema) -> _Check:
    count = len(s.flow_nodes)
    if count <= 50:
        return _Check(PASSED)
    return _Check(FAILED, note=f"найдено {count} элементов")


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
    return _Check(FAILED, _ids(dead_ends))


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


def _check_task_types(s: _Schema) -> _Check:
    if not s.tasks:
        return _Check(NOT_APPLICABLE)
    kinds = {_local(t.tag) for t in s.tasks}
    if len(kinds) >= 2:
        return _Check(PASSED)
    return _Check(FAILED, _ids(s.tasks), f"использован только тип «{kinds.pop()}»")


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
        return _Check(FAILED)
    empty_lanes = [lane for lane in lanes if not _children(lane, "flowNodeRef")]
    if empty_lanes:
        return _Check(FAILED, _ids(empty_lanes),
                      note=f"без элементов {len(empty_lanes)} из {len(lanes)} дорожек")
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
        return _Check(FAILED, _ids(untyped),
                      note=f"без типа {len(untyped)} из {len(s.intermediate_events)} событий")
    found = {
        _local(child.tag)[: -len("EventDefinition")]
        for event in s.intermediate_events
        for child in event
        if _local(child.tag).endswith("EventDefinition")
    }
    return _Check(PASSED, note="типы событий: " + ", ".join(sorted(found)))


def _check_documentation(s: _Schema) -> _Check:
    if not s.flow_nodes:
        return _Check(NOT_APPLICABLE)
    undocumented = [e for e in s.flow_nodes if not s.has_documentation(e)]
    documented = len(s.flow_nodes) - len(undocumented)
    if documented / len(s.flow_nodes) >= 0.5:
        return _Check(PASSED)
    return _Check(FAILED, _ids(undocumented),
                  f"документация есть у {documented} из {len(s.flow_nodes)} элементов")


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
    в ответе нет."""

    def __init__(self):
        self.rules = {
            'start_event': {'weight': 10, 'message': 'Количество стартовых событий должно соответствовать числу участников'},
            'end_event': {'weight': 10, 'message': 'У каждого участника должно быть своё событие завершения'},
            'pool_has_steps': {'weight': 10, 'message': 'В каждом пуле должен быть хотя бы один шаг: задача, подпроцесс или промежуточное событие'},
            'participant_interacts': {'weight': 8, 'message': 'Каждый участник должен быть затронут хотя бы одним потоком сообщения'},
            'gateway_conditions': {'weight': 15, 'message': 'Каждая ветка расходящегося эксклюзивного шлюза должна иметь условие или быть помечена как default'},
            'gateway_split_join': {'weight': 10, 'message': 'Ветки шлюза должны сходиться: каждая ветка ведёт к событию завершения или к узлу с двумя и более входящими потоками'},
            'sequence_flows': {'weight': 10, 'message': 'Все элементы должны быть соединены последовательностями'},
            'naming': {'weight': 10, 'message': 'Все элементы должны иметь осмысленные названия (не короче 3 символов)'},
            'guarded_cycles': {'weight': 10, 'message': 'Цикл допустим только с защищённым выходом — веткой исключительного шлюза'},
            'element_count': {'weight': 8, 'message': 'Схема не должна быть перегружена элементами (>50)'},
            'no_isolated': {'weight': 8, 'message': 'У каждого шага должны быть и вход, и выход (у стартового — только выход, у конечного — только вход)'},
            'boundary_events': {'weight': 6, 'message': 'Граничное событие прикрепляйте к задаче и ведите из него ветку обработки'},
            'task_types': {'weight': 8, 'message': 'Схема должна содержать разнообразные типы задач'},
            'pool_lanes': {'weight': 8, 'message': 'В пуле должны быть дорожки с закреплёнными элементами'},
            'role_pools': {'weight': 8, 'message': 'Роль или подразделение — дорожка внутри пула организации, а не отдельный участник: слейте такие пулы операцией merge_participants (source — роль, target — организация, as_lane: true)'},
            'event_types': {'weight': 8, 'message': 'Схема должна включать промежуточные события с типом (таймер, сообщение)'},
            'documentation': {'weight': 7, 'message': 'Элементы должны содержать документацию'},
        }
        self._checks = {
            'start_event': _check_start_event,
            'end_event': _check_end_event,
            'pool_has_steps': _check_pool_has_steps,
            'participant_interacts': _check_participant_interacts,
            'gateway_conditions': _check_gateway_conditions,
            'gateway_split_join': _check_gateway_split_join,
            'sequence_flows': _check_sequence_flows,
            'naming': _check_naming,
            'guarded_cycles': _check_guarded_cycles,
            'element_count': _check_element_count,
            'no_isolated': _check_no_isolated,
            'boundary_events': _check_boundary_events,
            'task_types': _check_task_types,
            'pool_lanes': _check_pool_lanes,
            'role_pools': _check_role_pools,
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

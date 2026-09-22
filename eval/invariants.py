"""Независимый оракул инвариантов BPMN-схем.

Харнесс сверяет вывод генератора не с тем же скорингом, который этим выводом
и оценивается, а с формальными свойствами BPMN 2.0: иначе правка
`core/bpmn_scoring.py` одновременно двигала бы и линейку, и измеряемое
значение, и регрессия стала бы невидимой. Поэтому здесь разрешены только
`xml.etree`, `parse_xml` аплайера и словарь структуры генератора;
`core/bpmn_scoring.py` не импортируется, а словари типов узлов переписаны
локально намеренно.

Две точки входа: `check_structure(structure)` для структуры-словаря
(то, что чинит `repair_structure`) и `check_xml(xml)` для сгенерированного
XML. Обе возвращают по каждому инварианту не только bool, но и причину со
списком id — без причин метрика бесполезна при разборе прогона.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.bpmn_edits import parse_xml

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Словари типов — собственные, а не импортированные из генератора или аплайера:
# оракул не должен «прощать» отсутствие узла только потому, что генератор
# перестал считать его шагом.
TASK_KINDS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "businessRuleTask", "sendTask", "receiveTask", "callActivity", "subProcess",
}
GATEWAY_KINDS = {
    "exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway",
}
EVENT_KINDS = {
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
    "boundaryEvent",
}
STEP_KINDS = TASK_KINDS | GATEWAY_KINDS | {
    "intermediateCatchEvent", "intermediateThrowEvent",
}
# События, у которых обязан быть тип-определение (timerEventDefinition и т. п.).
TYPED_EVENT_KINDS = {"intermediateCatchEvent", "intermediateThrowEvent",
                     "boundaryEvent"}
# Цели sequenceFlow, которых в BPMN 2.0 не бывает: в старт поток не входит,
# а граничное событие запускает хозяин через attachedToRef.
ILLEGAL_SEQUENCE_TARGETS = {"startEvent", "boundaryEvent"}
EVENT_DEFINITION_NAMES = {"timer", "message", "error", "signal", "escalation",
                          "conditional", "compensation", "terminate", "link"}

# Основные инварианты: проверяются всегда. Сценарные ожидания включаются,
# только если заявлены в сценарии, — иначе pass@1 накручивался бы «пустыми»
# проверками.
CORE_INVARIANTS: Tuple[str, ...] = (
    "pool_has_steps",
    "participant_interacts",
    "roles_as_lanes",
    "gateway_split_join",
    "gateway_conditions_or_default",
    "event_definitions",
    "boundary_handled",
    "flow_ends_legal",
    "no_unrouted",
)
SCENARIO_EXPECTATIONS: Tuple[str, ...] = (
    "has_timer",
    "has_branching",
    "min_steps",
    "expected_participants",
)
ALL_CHECKS: Tuple[str, ...] = CORE_INVARIANTS + SCENARIO_EXPECTATIONS
# Короткое имя для внешних потребителей (метрики, отчёты, тесты).
INVARIANTS = ALL_CHECKS

# Ключи ожидаемого определения события в структуре генератора: план модели
# меняется (поле могли назвать по-разному), поэтому читаем все варианты.
_DEFINITION_KEYS = ("event_definition", "event_type", "definition")


@dataclass(frozen=True)
class Check:
    """Исход одной проверки: факт, причина и проблемные id."""

    name: str
    ok: bool
    reason: str = ""
    ids: Tuple[str, ...] = ()
    applicable: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "applicable": self.applicable,
            "reason": self.reason,
            "ids": list(self.ids),
        }


@dataclass
class _Node:
    id: str
    kind: str
    name: str
    pool: str
    lane: str = ""
    definition: str = ""
    attached_to: str = ""
    documented: bool = False


@dataclass
class _Edge:
    id: str
    kind: str  # sequence | message
    source: str
    target: str
    condition: str = ""
    is_default: bool = False


@dataclass
class _Graph:
    """Единая модель схемы для обеих точек входа.

    Структура-словарь и XML приводятся к одному виду, поэтому текст проверок
    живёт в одном месте, и расхождение «структура починена, а в XML не
    доехало» видно как отдельная метрика, а не как две разные проверки.
    """

    nodes: List[_Node] = field(default_factory=list)
    edges: List[_Edge] = field(default_factory=list)
    pools: List[str] = field(default_factory=list)
    lanes: List[Dict[str, str]] = field(default_factory=list)
    defaults: Dict[str, str] = field(default_factory=dict)  # шлюз -> поток

    def __post_init__(self) -> None:
        self.by_id: Dict[str, _Node] = {n.id: n for n in self.nodes}
        self.seq_out: Dict[str, List[_Edge]] = {}
        self.seq_in: Dict[str, List[_Edge]] = {}
        self.message_edges: List[_Edge] = []
        for e in self.edges:
            if e.kind == "message":
                self.message_edges.append(e)
                continue
            self.seq_out.setdefault(e.source, []).append(e)
            self.seq_in.setdefault(e.target, []).append(e)
        # Дорожка в XML — не атрибут узла, а flowNodeRef в laneSet: размечаем
        # её здесь, чтобы обе точки входа давали одинаковый граф.
        for lane in self.lanes:
            for ref in _split_refs(lane.get("refs")):
                node = self.by_id.get(ref)
                if node is not None and not node.lane:
                    node.lane = lane.get("id", "")

    def steps(self, pool: Optional[str] = None) -> List[_Node]:
        return [node for node in self.nodes
                if node.kind in STEP_KINDS
                and (pool is None or node.pool == pool)]

    def of_kind(self, kinds: Iterable[str]) -> List[_Node]:
        kinds = set(kinds)
        return [n for n in self.nodes if n.kind in kinds]


# ---------------------------------------------------------------------------
# адаптеры входов
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _split_refs(value: Any) -> List[str]:
    """Список id из строки «A1,A2» (lane-refs сворачиваем в одну строку)."""
    return [part.strip() for part in _text(value).split(",") if part.strip()]


def _norm(value: Any) -> str:
    """Имя для сопоставления: регистр, «_» и всё, что не буква/цифра."""
    return "".join(ch for ch in _text(value).lower() if ch.isalnum())


def _definition_of(elem: Mapping[str, Any]) -> str:
    """Тип события из плана: явное поле либо хронометраж таймера."""
    for key in _DEFINITION_KEYS:
        value = _text(elem.get(key)).lower()
        if value in EVENT_DEFINITION_NAMES:
            return value
    if _text(elem.get("timer")) or _text(elem.get("duration")):
        return "timer"
    return ""


def _pool_name(item: Any) -> str:
    if isinstance(item, Mapping):
        return _text(item.get("name")) or _text(item.get("id"))
    return _text(item)


def _graph_from_structure(structure: Mapping[str, Any]) -> _Graph:
    """План/починенная структура -> граф. Неизвестные поля плана игнорируются:
    генератор меняется прямо сейчас, и харнесс не имеет права на них падать."""
    structure = structure or {}
    raw_elements = structure.get("elements") or []
    raw_flows = structure.get("flows") or structure.get("sequence_flows") or []
    raw_lanes = structure.get("lanes") or []

    nodes: List[_Node] = []
    for elem in raw_elements:
        if not isinstance(elem, Mapping):
            continue
        nodes.append(_Node(
            id=_text(elem.get("id")),
            kind=_text(elem.get("kind") or elem.get("type")),
            name=_text(elem.get("name")),
            pool=_text(elem.get("participant")),
            lane=_text(elem.get("lane")),
            definition=_definition_of(elem),
            attached_to=_text(elem.get("attached_to") or elem.get("attachedToRef")),
            documented=bool(_text(elem.get("documentation"))),
        ))

    defaults = {n.id: "" for n in nodes if n.kind in GATEWAY_KINDS}
    edges: List[_Edge] = []
    for flow in raw_flows:
        if not isinstance(flow, Mapping):
            continue
        kind = "message" if _text(flow.get("kind")
                                 or flow.get("type")).lower() == "message" else "sequence"
        source = _text(flow.get("source") or flow.get("sourceRef"))
        target = _text(flow.get("target") or flow.get("targetRef"))
        is_default = bool(flow.get("default"))
        if is_default and source in defaults:
            defaults[source] = _text(flow.get("id"))
        edges.append(_Edge(
            id=_text(flow.get("id")), kind=kind, source=source, target=target,
            condition=_text(flow.get("condition") or flow.get("name")),
            is_default=is_default,
        ))

    lanes = [{"id": _text(lane.get("id")), "name": _text(lane.get("name")),
              "pool": _text(lane.get("participant"))}
             for lane in raw_lanes if isinstance(lane, Mapping)]
    pools = [_pool_name(p) for p in (structure.get("participants") or [])]
    return _Graph(nodes=nodes, edges=edges,
                  pools=[p for p in pools if p], lanes=lanes,
                  defaults={k: v for k, v in defaults.items() if v})


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _is_bpmn(tag: Any) -> bool:
    """Чужие расширения (`<acme:lane>`) не должны становиться элементами BPMN."""
    tag = str(tag)
    return "{" not in tag or tag[1:].split("}", 1)[0].startswith(
        "http://www.omg.org/spec/BPMN/")


def _graph_from_xml(xml: str) -> _Graph:
    root = parse_xml(xml)
    pools: List[str] = []
    pool_by_process: Dict[str, str] = {}
    edges: List[_Edge] = []
    defaults: Dict[str, str] = {}

    for coll in root.iter():
        if not _is_bpmn(coll.tag) or _local(coll.tag) != "collaboration":
            continue
        for child in coll:
            tag = _local(child.tag)
            if tag == "participant":
                name = _text(child.get("name")) or _text(child.get("id"))
                pools.append(name)
                if child.get("processRef"):
                    pool_by_process[child.get("processRef")] = name
            elif tag == "messageFlow":
                # Концы messageFlow — узлы разных процессов: участнику этого
                # достаточно, а в local-маршрут своего пула он не входит.
                edges.append(_Edge(
                    id=_text(child.get("id")), kind="message",
                    source=_text(child.get("sourceRef")),
                    target=_text(child.get("targetRef")),
                    condition=_text(child.get("name")),
                ))

    nodes: List[_Node] = []
    lanes: List[Dict[str, str]] = []
    default_flow_ids: set = set()
    for process in root.iter():
        if not _is_bpmn(process.tag) or _local(process.tag) != "process":
            continue
        pool = pool_by_process.get(_text(process.get("id")),
                                   _text(process.get("name"))
                                   or _text(process.get("id")))
        for child in process:
            if not _is_bpmn(child.tag):
                continue
            tag = _local(child.tag)
            if tag == "laneSet":
                for lane in child:
                    if _local(lane.tag) != "lane":
                        continue
                    lanes.append({
                        "id": _text(lane.get("id")),
                        "name": _text(lane.get("name")),
                        "pool": pool,
                        "refs": ",".join(_text(ref.text) for ref in lane
                                         if _local(ref.tag) == "flowNodeRef"),
                    })
            elif tag == "sequenceFlow":
                edges.append(_Edge(
                    id=_text(child.get("id")), kind="sequence",
                    source=_text(child.get("sourceRef")),
                    target=_text(child.get("targetRef")),
                    condition=_text(next((_text(c.text) for c in child
                                          if _local(c.tag) == "conditionExpression"),
                                         "")),
                ))
            elif tag in TASK_KINDS | GATEWAY_KINDS | EVENT_KINDS:
                definition = ""
                for sub in child:
                    sub_tag = _local(sub.tag)
                    if sub_tag.endswith("EventDefinition"):
                        definition = sub_tag[:-len("EventDefinition")]
                nodes.append(_Node(
                    id=_text(child.get("id")), kind=tag,
                    name=_text(child.get("name")), pool=pool,
                    definition=definition,
                    attached_to=_text(child.get("attachedToRef")),
                    documented=any(_local(c.tag) == "documentation" and _text(c.text)
                                   for c in child),
                ))
                # выход по умолчанию объявлен атрибутом шлюза, а не на потоке
                if tag in GATEWAY_KINDS and child.get("default"):
                    gateway_id = _text(child.get("id"))
                    flow_id = _text(child.get("default"))
                    defaults[gateway_id] = flow_id
                    default_flow_ids.add(flow_id)

    for edge in edges:
        if edge.id and edge.id in default_flow_ids:
            edge.is_default = True
    return _Graph(nodes=nodes, edges=edges, pools=pools, lanes=lanes,
                  defaults=defaults)


# ---------------------------------------------------------------------------
# проверки
# ---------------------------------------------------------------------------


def _pass(name: str, note: str = "") -> Check:
    return Check(name=name, ok=True, reason=note)


def _na(name: str, note: str) -> Check:
    """Неприменимая проверка: в схеме нечего проверять, в метрику не входит."""
    return Check(name=name, ok=True, reason=note, applicable=False)


def _fail(name: str, reason: str, ids: Sequence[str] = ()) -> Check:
    return Check(name=name, ok=False, reason=reason, ids=tuple(ids))


def _check_pool_has_steps(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    if not g.pools:
        return _na("pool_has_steps", "пулов нет — проверять нечего")
    empty = [p for p in g.pools if not g.steps(p)]
    if not empty:
        return _pass("pool_has_steps")
    return _fail("pool_has_steps",
                 "пулы без единого шага: только «Старт → Завершение» — "
                 + ", ".join(f"«{p}»" for p in empty), empty)


def _check_participant_interacts(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    if len(g.pools) <= 1:
        return _na("participant_interacts", "участник один — взаимодействий нет")
    touched = set()
    for edge in g.message_edges:
        for node_id in (edge.source, edge.target):
            node = g.by_id.get(node_id)
            if node is not None:
                touched.add(node.pool)
    silent = [p for p in g.pools if p not in touched]
    if not silent:
        return _pass("participant_interacts",
                     f"messageFlow покрывают все {len(g.pools)} пулов")
    return _fail("participant_interacts",
                 "участники не затронуты ни одним потоком-сообщением: "
                 + ", ".join(f"«{p}»" for p in silent), silent)


def _check_roles_as_lanes(g: _Graph, exp: Mapping[str, Any]) -> Check:
    max_pools = exp.get("max_pools")
    if max_pools is None:
        return _na("roles_as_lanes", "max_pools не задан")
    max_pools = int(max_pools)
    if len(g.pools) <= max_pools:
        return _pass("roles_as_lanes",
                     f"пулов {len(g.pools)}, внешних участников по тексту {max_pools}")
    # примета: пул, который модель сама объявила дорожкой в другом пуле, —
    # роль, раздутая в участника. Такие и пустые пулы идут в ответ первыми:
    # они наименее оправданы текстом.
    lane_names = {_norm(lane.get("name")) for lane in g.lanes}
    dupes = [p for p in g.pools if _norm(p) in lane_names]
    unjustified = sorted(g.pools, key=lambda p: (p not in dupes, len(g.steps(p))))
    overflow = unjustified[:len(g.pools) - max_pools]
    reason = (f"пулов {len(g.pools)} при {max_pools} оправданных текстом: "
              "роли сотрудников обязаны быть дорожками основного пула")
    if dupes:
        reason += " (объявлены и пулом, и дорожкой: " + ", ".join(dupes) + ")"
    return _fail("roles_as_lanes", reason, overflow)


def _branch_closes(g: _Graph, start_id: str) -> bool:
    """Ветка корректна, если гаснет в endEvent либо в узле слияния (≥2
    входящих). Цикл без слияния — не закрытая ветка."""
    seen: set = set()
    stack = [start_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = g.by_id.get(node_id)
        if node is None:
            return False
        if node.kind == "endEvent":
            return True
        if len(g.seq_in.get(node_id, [])) >= 2:
            return True
        outgoing = g.seq_out.get(node_id, [])
        if not outgoing:
            return False
        stack.extend(e.target for e in outgoing)
    return False


def _splits(g: _Graph) -> List[Tuple[_Node, List[_Edge]]]:
    result = []
    for node in g.of_kind(GATEWAY_KINDS):
        outgoing = g.seq_out.get(node.id, [])
        if len(outgoing) >= 2:
            result.append((node, outgoing))
    return result


def _check_gateway_split_join(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    splits = _splits(g)
    if not splits:
        return _na("gateway_split_join", "шлюзов с ≥2 исходящими нет")
    bad, open_flows = [], []
    for node, outgoing in splits:
        unresolved = [e for e in outgoing if not _branch_closes(g, e.target)]
        if unresolved:
            bad.append(node.id)
            open_flows.extend(e.id or f"{e.source}->{e.target}"
                              for e in unresolved)
    if not bad:
        return _pass("gateway_split_join", f"развилок: {len(splits)}")
    return _fail("gateway_split_join",
                 "развилка без схождения: ветки не ведут ни к endEvent, ни к узлу "
                 "с ≥2 входящими (шлюзы " + ", ".join(bad) + "; потоки "
                 + ", ".join(open_flows) + ")", bad + open_flows)


def _check_gateway_conditions(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    splits = [(n, e) for n, e in _splits(g) if n.kind == "exclusiveGateway"]
    if not splits:
        return _na("gateway_conditions_or_default",
                   "исключающих шлюзов с ветками нет")
    bad_flows, bad_gateways = [], []
    for node, outgoing in splits:
        default_id = g.defaults.get(node.id, "")
        unconditioned = [e.id or f"{e.source}->{e.target}" for e in outgoing
                         if not e.condition and e.id != default_id
                         and not e.is_default]
        if unconditioned:
            bad_gateways.append(node.id)
            bad_flows.extend(unconditioned)
    if not bad_flows:
        return _pass("gateway_conditions_or_default")
    return _fail("gateway_conditions_or_default",
                 "исключающий шлюз: у веток нет ни conditionExpression, ни "
                 "атрибута default (шлюзы " + ", ".join(bad_gateways)
                 + "; потоки " + ", ".join(bad_flows) + ")",
                 bad_gateways + bad_flows)


def _check_event_definitions(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    typed = g.of_kind(TYPED_EVENT_KINDS)
    if not typed:
        return _na("event_definitions",
                   "промежуточных и граничных событий в схеме нет")
    unknown = [n.id for n in typed if not n.definition]
    if not unknown:
        kinds = sorted({n.definition for n in typed})
        return _pass("event_definitions", "типы событий: " + ", ".join(kinds))
    return _fail("event_definitions",
                 "события без дочернего *EventDefinition (bpmn-js рисует пустой "
                 "кружок): " + ", ".join(unknown), unknown)


def _check_boundary_handled(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    boundaries = g.of_kind({"boundaryEvent"})
    if not boundaries:
        return _na("boundary_handled", "граничных событий нет")
    bad, details = [], []
    for node in boundaries:
        host = g.by_id.get(node.attached_to)
        problems = []
        if not node.attached_to:
            problems.append("нет attachedToRef — событие ни к задаче не прикреплено")
        elif host is None:
            problems.append("attachedToRef ссылается вникуда")
        elif host.kind not in TASK_KINDS:
            problems.append(f"прикреплено к {host.kind}, а не к задаче")
        if not g.seq_out.get(node.id):
            problems.append("нет исходящей ветки обработки")
        if problems:
            bad.append(node.id)
            details.append(f"{node.id} ({node.name}): " + ", ".join(problems))
    if not bad:
        return _pass("boundary_handled", f"граничных событий: {len(boundaries)}")
    return _fail("boundary_handled", "; ".join(details), bad)


def _check_flow_ends_legal(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    # messageFlow здесь вне игры: сообщение в startEvent чужого пула — штатный
    # способ запустить процесс, а поток между пулами — это уже не sequenceFlow.
    seq = [e for e in g.edges if e.kind != "message"]
    if not g.nodes:
        return _na("flow_ends_legal", "узлов нет")
    if not seq:
        return _na("flow_ends_legal", "sequence-потоков нет — проверять нечего")
    bad: List[str] = []
    details: List[str] = []
    for edge in seq:
        source, target = g.by_id.get(edge.source), g.by_id.get(edge.target)
        # висячая ссылка — забота `no_unrouted`: дважды про неё не рапортуем
        if source is None or target is None:
            continue
        problems = []
        if target.kind in ILLEGAL_SEQUENCE_TARGETS:
            problems.append(f"входит в {target.kind} {target.id} («{target.name}»)")
        if source.kind == "endEvent":
            problems.append(f"исходит из {source.kind} {source.id} «{source.name}»")
        if not problems:
            continue
        ident = edge.id or f"{edge.source}->{edge.target}"
        bad.append(ident)
        details.append(f"{ident}: " + ", ".join(problems))
    if not bad:
        return _pass("flow_ends_legal", f"проверено sequence-потоков: {len(seq)}")
    return _fail("flow_ends_legal",
                 "недопустимые концы sequenceFlow: " + "; ".join(details)
                 + " — sequence-поток не входит ни в startEvent, ни в граничное "
                 "событие (его запускает хозяин по attachedToRef), а у endEvent "
                 "исходящего потока не бывает", bad)


def _check_no_unrouted(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    if not g.nodes:
        return _na("no_unrouted", "узлов нет")
    bad: List[str] = []
    notes: List[str] = []
    for node in g.nodes:
        has_in = bool(g.seq_in.get(node.id))
        has_out = bool(g.seq_out.get(node.id))
        if node.kind in ("startEvent", "boundaryEvent"):
            # старт и граничное событие «запускает» хозяин: нужен только выход
            missing = [] if has_out else ["нет исходящего потока"]
        elif node.kind == "endEvent":
            missing = [] if has_in else ["нет входящего потока"]
        else:
            missing = ([] if has_in else ["нет входящего потока"]) + \
                      ([] if has_out else ["нет исходящего потока"])
        if missing:
            bad.append(node.id)
            notes.append(f"{node.id} ({node.name}): " + ", ".join(missing))
    if not bad:
        return _pass("no_unrouted")
    return _fail("no_unrouted", "узлы вне маршрута: " + "; ".join(notes), bad)


def _has_timer(g: _Graph) -> bool:
    return any(n.definition == "timer" for n in g.of_kind(TYPED_EVENT_KINDS))


def _has_branching(g: _Graph) -> bool:
    return bool(_splits(g))


def _check_has_timer(g: _Graph, exp: Mapping[str, Any]) -> Optional[Check]:
    if not exp.get("must_have_timer"):
        return None
    if _has_timer(g):
        return _pass("has_timer", "таймер в схеме есть")
    return _fail("has_timer",
                 "сценарий требует ожидания/SLA, но timerEventDefinition и "
                 "boundaryEvent с таймером в схеме отсутствуют",
                 tuple(n.id for n in g.of_kind(TYPED_EVENT_KINDS)))


def _check_has_branching(g: _Graph, exp: Mapping[str, Any]) -> Optional[Check]:
    if not exp.get("must_branch"):
        return None
    if _has_branching(g):
        return _pass("has_branching")
    return _fail("has_branching",
                 "сценарий описывает развилку, но шлюза с ≥2 исходящими потоками "
                 "в схеме нет — ветвление «спрятано» в подписи потока")


def _check_min_steps(g: _Graph, exp: Mapping[str, Any]) -> Optional[Check]:
    need = exp.get("min_steps")
    if need is None:
        return None
    need = int(need)
    found = len(g.steps())
    if found >= need:
        return _pass("min_steps", f"шагов {found} (не меньше {need})")
    return _fail("min_steps",
                 f"шагов {found}, по сценарию ожидается не меньше {need}",
                 tuple(n.id for n in g.nodes if n.kind in STEP_KINDS))


def _matches(name: str, expected: str) -> bool:
    """Участник ищется терпимо: модель подписывает пул «Система WMS» вместо
    «WMS», а подразделение — в косвенном падеже («линии поддержки» вместо
    «поддержка»)."""
    a, b = _norm(name), _norm(expected)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    # падежные окончания: сравниваем и основу слова (не короче 5 букв)
    for trim in (1, 2):
        if len(b) - trim >= 5 and (b[:-trim] in a or a[:-trim] in b):
            return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


def _check_expected_participants(g: _Graph,
                                 exp: Mapping[str, Any]) -> Optional[Check]:
    expected = exp.get("expected_participants") or []
    if not expected:
        return None
    candidates = list(g.pools) + [lane.get("name", "") for lane in g.lanes]
    missing = [e for e in expected
               if not any(_matches(c, e) for c in candidates)]
    if not missing:
        return _pass("expected_participants",
                     f"заявленных участников найдено: {len(expected)}")
    return _fail("expected_participants",
                 "в схеме нет участников, которых требует текст: "
                 + ", ".join(f"«{m}»" for m in missing), missing)


_SCENARIO_CHECKS = {
    "has_timer": _check_has_timer,
    "has_branching": _check_has_branching,
    "min_steps": _check_min_steps,
    "expected_participants": _check_expected_participants,
}


def _run_checks(g: _Graph, expectations: Mapping[str, Any]) -> Dict[str, Check]:
    results: Dict[str, Check] = {
        "pool_has_steps": _check_pool_has_steps(g, expectations),
        "participant_interacts": _check_participant_interacts(g, expectations),
        "roles_as_lanes": _check_roles_as_lanes(g, expectations),
        "gateway_split_join": _check_gateway_split_join(g, expectations),
        "gateway_conditions_or_default": _check_gateway_conditions(g, expectations),
        "event_definitions": _check_event_definitions(g, expectations),
        "boundary_handled": _check_boundary_handled(g, expectations),
        "flow_ends_legal": _check_flow_ends_legal(g, expectations),
        "no_unrouted": _check_no_unrouted(g, expectations),
    }
    for name, fn in _SCENARIO_CHECKS.items():
        check = fn(g, expectations)
        if check is not None:
            results[name] = check
    return {k: results[k] for k in ALL_CHECKS if k in results}


def check_structure(structure: Mapping[str, Any],
                    expectations: Optional[Mapping[str, Any]] = None,
                    ) -> Dict[str, Check]:
    """Инварианты по структуре-словарю (сырому плану или после `repair_structure`)."""
    return _run_checks(_graph_from_structure(structure), expectations or {})


def check_xml(xml: str,
              expectations: Optional[Mapping[str, Any]] = None,
              ) -> Dict[str, Check]:
    """Инварианты по BPMN XML — то, что реально увидит bpmn-js."""
    return _run_checks(_graph_from_xml(xml), expectations or {})


# Синоним: план модели — та же структура-словарь, только не починенная.
check_plan = check_structure


def failed_checks(results: Mapping[str, Check]) -> List[Check]:
    return [c for c in results.values() if c.applicable and not c.ok]


def applicable_checks(results: Mapping[str, Check]) -> List[Check]:
    return [c for c in results.values() if c.applicable]


def failed(results: Mapping[str, Check]) -> List[str]:
    """Только имена упавших инвариантов — удобно в ассертах и сводках."""
    return [c.name for c in failed_checks(results)]


def summarize(results: Mapping[str, Check]) -> Dict[str, Any]:
    """Компактный вид для отчёта и таблицы: что прошло, что упало и почему."""
    applicable = applicable_checks(results)
    return {
        "passed": sorted(c.name for c in applicable if c.ok),
        "not_applicable": sorted(c.name for c in results.values()
                                 if not c.applicable),
        "failed": [{"name": c.name, "reason": c.reason, "ids": list(c.ids)}
                   for c in sorted(failed_checks(results), key=lambda c: c.name)],
        "applicable": len(applicable),
        "ok": all(c.ok for c in applicable) if applicable else True,
    }


def disagreements(structure_results: Mapping[str, Check],
                  xml_results: Mapping[str, Check]) -> List[str]:
    """Инварианты, где починенная структура и сгенерированный XML разошлись.

    Структурно схема правильная, а в XML правка не доехала (или наоборот) —
    это баг emission, а не модели, и путать их в метрике нельзя.
    """
    out = []
    for name in structure_results:
        if name not in xml_results:
            continue
        a, b = structure_results[name], xml_results[name]
        if a.applicable != b.applicable or a.ok != b.ok:
            out.append(name)
    return out

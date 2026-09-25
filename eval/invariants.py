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

Отдельная секция — бизнес-слой (`no_blind_rework`, `pools_not_pingpong`,
`no_overloaded_lane`, `waits_have_sla`): те же свойства, за которые отвечает
`BUSINESS_RULES` скоринга, но выведенные из семантики графа. Их формулировки
намеренно расходятся с эвристикой продукта (расхождения подписаны в docstrings
проверок), а `business_agreement` показывает, где два слоя не сошлись: «узел
процесса» — это утверждение о бизнесе, а не о нотации, и проверять его тем же
модулем, который его выдаёт, — значит никогда не узнать, что оба ошиблись.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set,
                    Tuple)

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

# Инварианты нотации и замысла: нарушение значит «схему нельзя принять», и
# эталонный ответ удовлетворяет им по построению (проверено на всех фикстурах
# quality=good). Именно они входят в гейт `scenario_pass`.
NOTATION_INVARIANTS: Tuple[str, ...] = (
    "pool_has_steps",
    "participant_interacts",
    "roles_as_lanes",
    "gateway_split_join",
    "gateway_conditions_or_default",
    "event_definitions",
    "timer_schedule",
    "boundary_handled",
    "flow_ends_legal",
    "flows_within_pool",
    "message_flow_ends",
    "no_unrouted",
    "loops_have_a_guard",
)
# Бизнес-слой: узкие места процесса, а не нотации. Пересказывают `BUSINESS_RULES`
# скоринга средствами графа и ничего у него не берут.
BUSINESS_INVARIANTS: Tuple[str, ...] = (
    "no_blind_rework",
    "pools_not_pingpong",
    "no_overloaded_lane",
    "waits_have_sla",
    "signoffs_need_a_gate",
)
# Основные инварианты: проверяются всегда. Сценарные ожидания включаются,
# только если заявлены в сценарии, — иначе pass@1 накручивался бы «пустыми»
# проверками.
CORE_INVARIANTS: Tuple[str, ...] = NOTATION_INVARIANTS + BUSINESS_INVARIANTS
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

# Статусы бизнес-правил — строки из ответа `BPMNScorer.evaluate`, переписанные
# здесь намеренно (`business_agreement` получает результат скоринга как вход,
# но не имеет права его импортировать).
_SCORER_PASSED = "passed"
_SCORER_FAILED = "failed"
_SCORER_NA = "not_applicable"
_SCORER_UNKNOWN = "unknown"
# Состояния сверки двух слоёв: расхождение — это данные человеку, а не гейт.
AGREE = "agree"
ORACLE_STRICTER = "oracle_stricter"
SCORER_STRICTER = "scorer_stricter"
NOT_COMPARABLE = "not_comparable"
NO_DATA = "no_data"


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
    # Хронометраж таймера (`timeDate` / `timeDuration` / `timeCycle`): тип
    # события и его срок — разные вещи, и `<timerEventDefinition/>` без значения
    # исполнитель не заведёт.
    schedule: str = ""
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
    # Подпись потока (`name`): в bpmn-js так выглядит «вернули»/«согласовано»,
    # когда формального conditionExpression нет. Бизнес-инварианты читают её,
    # `gateway_conditions_or_default` — по-прежнему нет (там требование нотации).
    label: str = ""


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
    # Любой id, которым назван пул: участник XML, его процесс, имя плана. Нужно
    # потому, что конец messageFlow по BPMN 2.0 имеет право быть участником
    # целиком, и «кто затронут» тогда не читается из узлов.
    pool_aliases: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id: Dict[str, _Node] = {n.id: n for n in self.nodes}
        self.seq_out: Dict[str, List[_Edge]] = {}
        self.seq_in: Dict[str, List[_Edge]] = {}
        self.message_edges: List[_Edge] = []
        # Дуги, у которых конца в схеме нет: они не дают узлу ни входа, ни
        # выхода, иначе «несуществующий поток» засчитывался бы как маршрут и
        # нога выглядела соединённой (оракул прощал это на 2 схемах корпуса).
        self.unresolved: List[_Edge] = []
        for e in self.edges:
            if e.kind == "message":
                self.message_edges.append(e)
                continue
            if e.source in self.by_id and e.target in self.by_id:
                self.seq_out.setdefault(e.source, []).append(e)
                self.seq_in.setdefault(e.target, []).append(e)
            else:
                self.unresolved.append(e)
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
            # План без поля срока — это НЕ мёртвый таймер: генератор пишет
            # хронометраж по умолчанию (`core/bpmn_generator.py`), и требовать
            # срок на стадии плана значило бы снимать инвариант за то, чего в
            # продукте не бывает. Значение-SENTINEL переписано здесь намеренно:
            # оракул не импортирует константы генератора (тот же порядок, что со
            # статусами скоринга выше), а проверка на пустоту видит результат.
            schedule=(_text(elem.get("duration")) or _text(elem.get("timer"))
                      or _text(elem.get("cycle")) or _text(elem.get("timeDate"))
                      or ("plan-default" if _definition_of(elem) == "timer"
                          else "")),
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
            label=_text(flow.get("name")),
            is_default=is_default,
        ))

    lanes = [{"id": _text(lane.get("id")), "name": _text(lane.get("name")),
              "pool": _text(lane.get("participant"))}
             for lane in raw_lanes if isinstance(lane, Mapping)]
    pools = [_pool_name(p) for p in (structure.get("participants") or [])]
    aliases: Dict[str, str] = {}
    for p in (structure.get("participants") or []):
        name = _pool_name(p)
        if not name:
            continue
        aliases[name] = name
        if isinstance(p, Mapping) and _text(p.get("id")):
            aliases[_text(p.get("id"))] = name
    return _Graph(nodes=nodes, edges=edges,
                  pools=[p for p in pools if p], lanes=lanes,
                  pool_aliases=aliases,
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
    aliases: Dict[str, str] = {}
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
                if child.get("id"):
                    aliases[child.get("id")] = name
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
                    label=_text(child.get("name")),
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
                    label=_text(child.get("name")),
                    condition=_text(next((_text(c.text) for c in child
                                          if _local(c.tag) == "conditionExpression"),
                                         "")),
                ))
            elif tag in TASK_KINDS | GATEWAY_KINDS | EVENT_KINDS:
                definition = ""
                schedule = ""
                for sub in child:
                    sub_tag = _local(sub.tag)
                    if sub_tag.endswith("EventDefinition"):
                        definition = sub_tag[:-len("EventDefinition")]
                        # Хронометраж — ВНУТРИ определения (`timerEventDefinition
                        # / timeDuration`), а не дочерний узел события: читать его
                        # на уровне события значило бы не найти срок там, где он
                        # есть. Пустое значение = «срока нет».
                        schedule = schedule or next(
                            (_text(t.text) for t in sub
                             if _local(t.tag) in ("timeDate", "timeDuration",
                                                  "timeCycle")),
                            "")
                nodes.append(_Node(
                    id=_text(child.get("id")), kind=tag,
                    name=_text(child.get("name")), pool=pool,
                    definition=definition, schedule=schedule,
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
                  pool_aliases={**aliases, **pool_by_process},
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
            elif node_id in g.pool_aliases:
                # Конец потока — участник целиком. BPMN 2.0 это разрешает, и
                # Signavio рисует так «фронт» банка: пул затронут, хотя ни один
                # его шаг в обмене не назван. До этой правки оракул объявлял
                # такой пул немым на 35 схемах корпуса из 367, а инвариант
                # стоит в гейте `pass@1/scenario`.
                touched.add(g.pool_aliases[node_id])
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
    """Развилки, обязанные иметь сход: по ним идёт больше одного токена.

    `eventBasedGateway` исключён: его ноги ждут разных событий, срабатывает
    одна, и схождения потоков у такой развилки по семантике нотации нет. Пока её
    не исключали, оракул выдавал 7 ложных «развилка без схождения» на 98 схемах
    корпуса с разветвлённым event-шлюзом, а `gateway_split_join` входит в гейт
    `pass@1/scenario`. Скоринг трактует требование так же (`SPLIT_GATEWAY_TAGS`)."""
    result = []
    for node in g.of_kind(GATEWAY_KINDS):
        if node.kind == "eventBasedGateway":
            continue
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


def _check_flows_within_pool(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """`sequenceFlow` живёт внутри одного процесса: токен не переезжает из пула
    в пул, для этого есть `messageFlow`.

    Принадлежность узла оракул читает по **имени** пула, а скоринг — по id
    процесса, и это намеренно разные читательские пути: два пула с одним именем
    дали бы расхождение по этому инварианту, а не молчаливый пропуск (именно так
    и был найден класс висячих дуг — сверкой двух слоёв, а не фикстурами).
    """
    seq = [e for e in g.edges if e.kind != "message"]
    if len({n.pool for n in g.nodes if n.pool}) < 2:
        return _na("flows_within_pool", "пул в схеме один — пересекать нечего")
    bad: List[str] = []
    details: List[str] = []
    for edge in seq:
        source, target = g.by_id.get(edge.source), g.by_id.get(edge.target)
        if source is None or target is None:
            continue  # висячая ссылка — забота `no_unrouted`
        if not source.pool or not target.pool or source.pool == target.pool:
            continue
        ident = edge.id or f"{edge.source}->{edge.target}"
        bad.append(ident)
        details.append(f"{ident}: '{source.id}' в пуле '{source.pool}', а "
                       f"'{target.id}' — в '{target.pool}'")
    if not bad:
        return _pass("flows_within_pool", f"проверено sequence-потоков: {len(seq)}")
    return _fail("flows_within_pool",
                 "дуги между пулами: " + "; ".join(details)
                 + " — связь двух участников выражается потоком сообщения, "
                 "а не потоком управления", bad)


def _check_message_flow_ends(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Концы сообщения обязаны быть названы в схеме и способны обмен принять:
    участником или узлом, но не развилкой.

    `participant_interacts` про неё молчит правильно: он считает, кого обмен
    затронул, а сломанный конец ни кого не затрагивает — и схема выглядит
    «мало участников», а не «битый обмен».
    """
    if not g.message_edges:
        return _na("message_flow_ends", "потоков сообщения нет")
    bad: List[str] = []
    details: List[str] = []
    for edge in g.message_edges:
        problems = []
        for side, ref in (("источника", edge.source), ("цели", edge.target)):
            if not ref:
                problems.append(f"нет {side}")
            elif ref not in g.by_id and ref not in g.pool_aliases:
                problems.append(f"{side} названа id '{ref}', которого в схеме нет")
            elif ref in g.by_id and g.by_id[ref].kind in GATEWAY_KINDS:
                # Развилка не бывает концом обмена: у `messageFlow` конец —
                # участник или узел, а решение развилки наружу уходит ногой
                # того же пула. Класс зарядила не корпус (из 625 обменов
                # шлюзовых концов ноль), а собственный совет линейки.
                problems.append(f"{side} — развилка '{g.by_id[ref].kind}', "
                                "концом обмена она не бывает")
        if not problems:
            continue
        ident = edge.id or f"{edge.source}->{edge.target}"
        bad.append(ident)
        details.append(f"{ident}: " + ", ".join(problems))
    if not bad:
        return _pass("message_flow_ends",
                     f"проверено потоков сообщения: {len(g.message_edges)}")
    return _fail("message_flow_ends",
                 "битые обмены: " + "; ".join(details), bad)


def _check_timer_schedule(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Таймер обязан иметь хронометраж, иначе он не наступит.

    `event_definitions` про неё молчит правильно: определение у узла есть,
    пустое значение — другой дефект. Для бизнеса он хуже обычного битого XML:
    схема с «просрочкой» нарисована, ветка эскалации подписана, а исполнитель
    её не заведёт, и процесс просто ждёт.
    """
    timers = [n for n in g.nodes if n.definition == "timer"]
    if not timers:
        return _na("timer_schedule", "таймеров в схеме нет")
    inert = [n.id for n in timers if not n.schedule]
    if not inert:
        return _pass("timer_schedule", f"таймеров: {len(timers)}, срок у всех")
    return _fail("timer_schedule",
                 "таймер без timeDate/timeDuration/timeCycle — он не наступит: "
                 + ", ".join(inert), inert)


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


# ---------------------------------------------------------------------------
# бизнес-слой: узкие места процесса, а не нотации
# ---------------------------------------------------------------------------
#
# Четыре проверки ниже пересказывают бизнес-правила скоринга (`BUSINESS_RULES`)
# средствами одного лишь графа. Делать это «в лоб» было бы бессмысленно:
# сверка двух копий одной формулировки не ловит общее заблуждение, из-за которого
# правило и промпт улучшения разъезжаются вместе, а метрика остаётся зелёной.
# Поэтому каждая проверка формулирует то же бизнес-требование независимо, а
# каждый осознанный разрыв подписан в её docstring; `business_agreement`
# превращает эти разрывы в таблицу для человека.

# Монополия дорожки: >3/4 всех работ пула на одном исполнителе при двух и более
# дорожках. Порог свой и он сознательно ВЫШЕ скоринговых 60%: у оракула нет
# калитки «дорожек хотя бы три», и с двумя дорожками 60% ловили бы честное
# разделение «оператор делает 7 шагов из 12». 0.75 — это «второй роли досталась
# дорожка-фишка».
LANE_MONOPOLY_SHARE = 0.75

# Ожидание, блокирующее маршрут: `intermediateCatchEvent` не-таймера (сообщение,
# сигнал, условие — токен стоит до наступления) и `receiveTask`, который по BPMN
# и есть «жду сообщение». Граничные события в список не входят: они не держат
# токен, а throw-событие завершается сразу.
BLOCKING_CATCH_KINDS = {"intermediateCatchEvent", "receiveTask"}

# Определения catch-событий, которые ничьего прихода не ждут. `link` — метка
# перехода: токен доходит до неё и идёт дальше по сопоставленной мишени, а не
# встаёт. `compensate` — триггер отработки: событие будит уже выполненный
# участок, а не внешний мир. Требовать у них срок — значит ставить нарушение
# там, где его нечем исполнить: таймер на метку перехода ничего не значит.
# Те же два имени знает скоринг (`wait_without_sla`): на корпусе из 367 файлов
# только `link` приносил 675 «нарушений» из 2731.
NOT_A_WAIT_DEFINITIONS = frozenset({"link", "compensate"})


def _sequence_cycles(g: _Graph) -> List[List[str]]:
    """Циклы маршрута токена: обход в глубину по `seq_out` с путём-стеком.

    Нога, ведущая в узел текущего пути, замыкает цикл: его участники — участок
    пути от этого узла до вершины стека. Таких обратных ног не больше числа
    потоков, поэтому перебор ограничен и на схеме из десятков «ромбов» не
    взрывается.
    """
    cycles: List[List[str]] = []
    state: Dict[str, int] = {}
    for root in sorted(g.seq_out):
        if state.get(root):
            continue
        state[root] = 1
        stack: List[Tuple[str, int]] = [(root, 0)]
        path: List[str] = [root]
        while stack:
            node, idx = stack[-1]
            legs = [e.target for e in g.seq_out.get(node, []) if e.target]
            if idx >= len(legs):
                state[node] = 2
                stack.pop()
                path.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = legs[idx]
            if state.get(nxt) == 1:
                cycles.append(path[path.index(nxt):] + [nxt])
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, 0))
                path.append(nxt)
    return cycles


def _loop_guarded(g: _Graph, cycle: List[str]) -> bool:
    """Из цикла есть выход, названный развилкой: его участник —
    `exclusiveGateway` минимум с двумя ногами.

    Ветка «повторить / уйти» и есть критерий выхода; без неё токен возвращается
    всегда, и процесс повторяет работу, пока кто-то не вмешается извне. Подпись
    дуги здесь сознательно не считается: её наличие спрашивает
    `no_blind_rework`, а тут спрашивается механизм ветвления.
    """
    for node_id in set(cycle):
        node = g.by_id.get(node_id)
        if node is None or node.kind != "exclusiveGateway":
            continue
        if len({e.target for e in g.seq_out.get(node_id, []) if e.target}) >= 2:
            return True
    return False


def _check_loops_have_a_guard(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Ни один цикл маршрута не должен быть безусловным.

    Зеркало правила `guarded_cycles` (28 схем корпуса из 367 нарушают, а
    независимой проверки у класса не было). Читательский путь другой: оракул
    идёт по `seq_out` своего `_Graph` и берёт `kind` узла, тогда как линейка
    перебирает теги `out_targets` над `_Schema`. Порог и признак совпадают
    намеренно — расхождение в одном бизнес-вопросе означало бы, что продукт
    меряет себя сам.
    """
    cycles = _sequence_cycles(g)
    if not cycles:
        return _na("loops_have_a_guard", "циклов в маршруте нет")
    bare = [c for c in cycles if not _loop_guarded(g, c)]
    if not bare:
        return _pass("loops_have_a_guard",
                     f"циклов: {len(cycles)}, у каждого внутри есть развилка "
                     "минимум с двумя ногами")
    ids = list(dict.fromkeys(node for cycle in bare for node in cycle))
    return _fail("loops_have_a_guard",
                 f"повтор без критерия выхода у {len(ids)} узлов: "
                 + ", ".join(f"'{i}'" for i in ids[:8])
                 + (f" … и ещё {len(ids) - 8}" if len(ids) > 8 else "")
                 + " — из цикла нет развилки, по которой процесс мог бы его "
                   "покинуть", ids)


def _reachable(g: _Graph, start: str, skip: Optional[_Edge] = None) -> set:
    """Узлы, куда токен доходит из `start` по sequence-потокам (включая сам
    start; `skip` — дуга, которую считать нельзя, чтобы не замкнуть путь её же
    собственным концом)."""
    seen = {start}
    stack = [start]
    while stack:
        node_id = stack.pop()
        for edge in g.seq_out.get(node_id, []):
            if edge is skip or not edge.target or edge.target in seen:
                continue
            seen.add(edge.target)
            stack.append(edge.target)
    return seen


def _ancestors(g: _Graph, target: str) -> set:
    """Узлы, из которых в `target` можно войти по sequence-потокам (без самого
    target): маршруты, проложенные до него."""
    seen: set = set()
    stack = [target]
    while stack:
        node_id = stack.pop()
        for edge in g.seq_in.get(node_id, []):
            if not edge.source or edge.source in seen:
                continue
            seen.add(edge.source)
            stack.append(edge.source)
    return seen


def _leg_named(g: _Graph, edge: _Edge) -> bool:
    """Нога развилки названа: conditionExpression, подпись потока либо выход по
    умолчанию. Скоринг на этом месте расходится с нами осознанно — см.
    docstring `no_blind_rework`."""
    if edge.is_default or g.defaults.get(edge.source) == edge.id:
        return True
    return bool(edge.condition or edge.label)


def _back_arcs(g: _Graph) -> List[Tuple[_Edge, _Node]]:
    """Дуги возврата: поток, конец которого — активность, откуда маршрут снова
    приходит к началу этого потока. Точно «повторно сделать работу», а не
    «вернуться в шлюз схождения»: события и шлюзы работу не выполняют."""
    result: List[Tuple[_Edge, _Node]] = []
    for edge in g.edges:
        if edge.kind == "message" or not edge.source or not edge.target:
            continue
        target = g.by_id.get(edge.target)
        if target is None or target.kind not in TASK_KINDS:
            continue
        if edge.source == edge.target or edge.source in _reachable(g, edge.target,
                                                                  skip=edge):
            result.append((edge, target))
    return result


def _loop_nodes(g: _Graph, start: str, finish: str) -> set:
    """Тело петли: узлы хотя бы одного пути `start → finish` вместе с концами."""
    return (_reachable(g, start) & _ancestors(g, finish)) | {start, finish}


def _distinguishes(g: _Graph, node_id: str, loop: set) -> bool:
    """Развилка различает «вернули» и «согласовано»: у неё есть нога в петлю и
    нога из петли, и каждая нога названа. Одной названной ноги мало — тогда
    безымянной остаётся та ветка, по которой работа возвращается."""
    legs = g.seq_out.get(node_id, [])
    if len(legs) < 2:
        return False
    into = [e for e in legs if e.target in loop]
    out = [e for e in legs if e.target not in loop]
    return bool(into) and bool(out) and all(_leg_named(g, e) for e in legs)


def _check_no_blind_rework(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Повтор обязан называть, чем «вернули» отличается от «согласовано».

    Петля берётся из семантики токена: дуга, возвращающая работу в уже
    сделанную активность (её конец сам доходит до её начала). Такая петля
    осмысленна, если решение о повторе различимо — названа либо сама
    возвращающая дуга, либо обе ноги развилки, одна из которых ведёт в петлю, а
    другая выводит из неё. Иначе процесс идёт по кругу без записанной причины.

    Два осознанных расхождения со скорингом (`rework_loop`). Первое: скоринг
    смотрит только на дуги *выхода* из цикла и требует защиты хотя бы у одной из
    них, поэтому безымянный возврат ему не нарушение, а цикл без дуг выхода
    вовсе молча проходит (`no_isolated` в это время ругается на недостижимый
    конец). Оракул спрашивает про пару ног «в петлю / из петли» и такой цикл
    тоже называет слепым повтором. Второе: оракул признаёт подписью ноги и
    `name` потока — в нарисованной схеме «Вернули на доработку» на дуге
    различает ветки не хуже conditionExpression, а требование нотационного
    условия сторожит отдельный инвариант `gateway_conditions_or_default`.
    """
    backs = _back_arcs(g)
    if not backs:
        return _na("no_blind_rework", "дуг, возвращающих работу в уже сделанный "
                                     "шаг, нет — повторных проходов нечего проверять")
    blind: List[str] = []
    ids: List[str] = []
    for edge, target in backs:
        if _leg_named(g, edge):
            continue
        loop = _loop_nodes(g, target.id, edge.source)
        if any(_distinguishes(g, node.id, loop) for node in g.nodes):
            continue
        blind.append(
            f"{edge.id or f'{edge.source}->{edge.target}'} возвращает работу в "
            f"{target.id} («{target.name}»), а «вернули» ничем не отличается от "
            f"«согласовано»: ни на дуге возврата, ни на ногах её развилки нет ни "
            f"условия, ни подписи, ни выхода по умолчанию")
        ids.extend(i for i in (edge.id, edge.source, target.id) if i)
    if not blind:
        return _pass("no_blind_rework", f"дуг возврата: {len(backs)}")
    return _fail("no_blind_rework",
                 "слепой повтор: " + "; ".join(blind)
                 + " — процесс идёт по кругу без записанной причины возврата",
                 list(dict.fromkeys(ids)))


def _pool_of_endpoint(g: _Graph, ref: str) -> str:
    """Пулы на конце messageFlow: узел отвечает через свой процесс, а ссылка на
    участник (или на имя пула, как её кладёт в план аплайер) — сама за себя."""
    node = g.by_id.get(ref)
    if node is not None:
        return node.pool
    return next((p for p in g.pools if _norm(p) == _norm(ref)), ref)


def _check_pools_not_pingpong(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Одна и та же работа не должна ходить между двумя пулами туда-сюда.

    Пинг-понг — это *возврат той же работы*: два потока-сообщения, соединяющие
    одну и ту же пару концов в обе стороны (концы могут быть и шагами, и пулами
    целиком). Ответственность при этом не разведена, а перекладывается: у шага
    нет хозяина, который доводит её до конца.

    Совпадение критерия с `handoff_pingpong` (тот тоже ждёт возврата той же пары
    концов) — не дублирование, а то, ради чего оракул и живёт отдельно: формулировки
    независимы, и любое расхождение между ними видно по строке
    `business_agreement`, а не молча переезжает из правила в метрику. Механически
    слои расходятся там, где скорингу надо *разрешить* концы в участников с
    именем: неразрешённый конец для него — повод молчать, а для оракула — сторона
    обмена (битая ссылка на пул тоже перекидывание, и её чинят, а не прощают).
    """
    if len(g.pools) < 2:
        return _na("pools_not_pingpong", "пул один — перекидывать работу некому")
    arcs = [e for e in g.message_edges if e.source and e.target]
    if not arcs:
        return _na("pools_not_pingpong", "потоков-сообщений нет")
    by_pair: Dict[Tuple[str, str], List[_Edge]] = {}
    for edge in arcs:
        by_pair.setdefault((edge.source, edge.target), []).append(edge)
    names = {n.id: n.name for n in g.nodes}
    hits: List[str] = []
    ids: List[str] = []
    for (src, dst), there in sorted(by_pair.items()):
        back = by_pair.get((dst, src))
        if not back or src >= dst:
            continue
        pool_src, pool_dst = _pool_of_endpoint(g, src), _pool_of_endpoint(g, dst)
        if not pool_src or pool_src == pool_dst:
            # Оба конца в одном пуле — это внутренний маршрут, а не обмен
            continue
        work = (f" — та же работа в руках у двух пулов («{names[src]}» / "
                f"«{names[dst]}»)" if src in names and dst in names else "")
        hits.append(
            f"«{pool_src}» ↔ «{pool_dst}»: "
            + ", ".join(f"{e.id} ({src} → {dst})" for e in there)
            + " и обратно "
            + ", ".join(f"{e.id} ({dst} → {src})" for e in back)
            + f" — взаимных обменов этой пары: {max(len(there), len(back))}"
            + work)
        ids.extend(e.id for e in there + back)
        ids.extend([i for i in (src, dst) if i not in names])
    if not hits:
        return _pass("pools_not_pingpong",
                     "двусторонних обменов одной работой нет (потоков-сообщений: "
                     f"{len(arcs)})")
    return _fail("pools_not_pingpong",
                 "пулы гоняют одну и ту же работу друг другу в обе стороны: "
                 + "; ".join(hits) + " — у процесса должен быть один хозяин, "
                 "остальным рольам — дорожки", list(dict.fromkeys(ids)))


def _lane_groups(g: _Graph) -> List[Tuple[str, List[Dict[str, str]]]]:
    """Дорожки, сгруппированные по пулу.

    Пул берётся из объявления дорожки (`participant` плана, процесс XML), а если
    его нет — из узлов, которые в дорожку разложены. По `g.pools` не ходим: у
    схемы без `<collaboration>` пулов в этом списке нет вовсе, и проверка
    объявляла бы не применимой ровно ту однопольную раскладку, ради которой её
    и завели."""
    groups: Dict[str, List[Dict[str, str]]] = {}
    for lane in g.lanes:
        pool = _text(lane.get("pool"))
        if not pool:
            owners = [n.pool for n in g.nodes
                      if n.lane == lane.get("id", "") and n.pool]
            pool = owners[0] if owners else ""
        groups.setdefault(pool, []).append(lane)
    return list(groups.items())


def _work_of(g: _Graph, pool: str) -> List[_Node]:
    """Работы пула: активности (события и шлюзы работу не делают), включая
    неразложенные по дорожкам — знаменатель обязан считаться от шагов пула."""
    return [n for n in g.nodes if n.pool == pool and n.kind in TASK_KINDS]


def _check_no_overloaded_lane(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Одна дорожка не должна держать почти все работы пула.

    Роли нарисованы, а работа лежит на одном исполнителе — это узкое место
    процесса: он и очередь, и единственный носитель знания. Доля считается так:
    работы дорожки / все работы пула (`TASK_KINDS` пула, включая те, что вообще
    не разложены по дорожкам), порог — `LANE_MONOPOLY_SHARE`.

    Два отличия от скоринга (`lane_overload`) остались и после того, как
    двухдорожечные пулы перестали быть для него «не применимы» (калитка в три
    дорожки стоила контуру подсказки на дежурной смене production_incident, где
    4 работы из 5 лежат на одном исполнителе — теперь этот случай ловят оба).
    Знаменатель: скоринг делит на работы размеченных дорожек, оракул — на все
    шаги пула, поэтому не размеченная по ролям работа разбавляет долю дорожки,
    зато схема без laneSet не получает права молчать. Порог: 75% здесь против 60%
    у скоринга при трёх и более дорожках, поэтому `laned_doc([4, 1, 1])` —
    `scorer_stricter`. Оба расхождения видит `business_agreement`.
    """
    split = [(pool, lanes) for pool, lanes in _lane_groups(g) if len(lanes) >= 2]
    if not split:
        return _na("no_overloaded_lane",
                   "пулов с двумя и более дорожками нет — делить работу не между кем")
    checked = 0
    bad: List[str] = []
    ids: List[str] = []
    for pool, lanes in split:
        work = _work_of(g, pool)
        if not work:
            continue
        checked += 1
        by_lane: Dict[str, int] = {}
        for node in work:
            if node.lane:
                by_lane[node.lane] = by_lane.get(node.lane, 0) + 1
        for lane in lanes:
            count = by_lane.get(lane.get("id", ""), 0)
            share = count / len(work)
            if share <= LANE_MONOPOLY_SHARE:
                continue
            name = lane.get("name") or lane.get("id", "")
            bad.append(f"дорожка «{name}» держит {count} из {len(work)} работ пула "
                       f"«{pool or 'без имени'}» ({round(share * 100)}%)")
            ids.append(lane.get("id", ""))
    if not checked:
        return _na("no_overloaded_lane",
                   "в пулах с дорожками нет ни одной работы — сравнивать нечего")
    if not bad:
        return _pass("no_overloaded_lane",
                     f"пулов с ≥2 дорожками: {checked}, ни одна дорожка не держит "
                     f"больше {round(LANE_MONOPOLY_SHARE * 100)}% работ пула")
    return _fail("no_overloaded_lane",
                 "работа сосредоточена на одном исполнителе: " + "; ".join(bad)
                 + " — роли есть, а процесса у них нет",
                 [i for i in dict.fromkeys(ids) if i])


# Ручная работа в цепочке согласований. `task` — шаг без типа: так человека
# рисует bpmn-js, и если считать только `userTask`, проверка слепнет к половине
# рукописного корпуса. Список совпадает с `HUMAN_TASK_TAGS` скоринга намеренно:
# оракул не импортирует `core`, поэтому врознь у них только способ прочесть
# схему, а не признак.
HUMAN_SIGNOFF_KINDS = frozenset({"userTask", "manualTask", "task"})
# Сколько ручных шагов подряд — уже «согласования без решения». Число взято из
# `APPROVAL_CHAIN_MIN`: по одному бизнес-вопросу слои обязаны сходиться, иначе
# `business_agreement` показывал бы расхождение там, где просто разные пороги.
SIGNOFF_CHAIN_MIN = 4


def _signoff_chain(g: _Graph, ids: Set[str],
                   minimum: int = SIGNOFF_CHAIN_MIN) -> List[str]:
    """Первая найденная линия ручных шагов длиной не меньше `minimum`.

    Линию разрывает всё, что не ручная работа из `ids`: шлюз — это решение,
    автоматический шаг — не «ещё один согласующий». Возврат в уже посещённый узел
    не продлевает линию (цикл считает `no_blind_rework`), поэтому обход конечен.
    Ищем существование линии нужной длины, а не самую длинную: свидетелю
    достаточно четырёх id, а полный перебор путей на разветвлённой схеме стоил бы
    времени прогона.
    """
    def walk(node_id: str, path: List[str]) -> List[str]:
        if len(path) >= minimum:
            return list(path)
        for edge in g.seq_out.get(node_id, []):
            nxt = edge.target
            if not nxt or nxt not in ids or nxt in path:
                continue
            found = walk(nxt, path + [nxt])
            if found:
                return found
        return []

    for node_id in sorted(ids):
        found = walk(node_id, [node_id])
        if found:
            return found
    return []


def _check_signoffs_need_a_gate(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """Четыре ручных шага подряд у одного исполнителя — узкое место, а не норма.

    Пятый бизнес-инвариант и зеркало правила `approval_chain`: до него скоринг
    находил цепочки согласований на 37 схемах корпуса из 367, а метрики харнесса
    (`business/smells`, `improve/business_repaired_share`) про этот класс молчали —
    улучшение согласования не превращалось ни в какое число.

    Дорожка обязана быть названа там, где она есть: без `laneSet` четыре подписи
    идут одной линией в одном пуле, и это худший случай (исполнители неразличимы
    вовсе), а не «свойство неприменимо». Процесс, где в группе меньше четырёх
    ручных шагов, остаётся `not_applicable` — проверять нечего.
    """
    groups: List[Tuple[str, Set[str]]] = []
    if g.lanes:
        for lane in g.lanes:
            lane_id = str(lane.get("id") or "")
            users = {n.id for n in g.nodes
                     if n.kind in HUMAN_SIGNOFF_KINDS and n.lane == lane_id}
            if len(users) >= SIGNOFF_CHAIN_MIN:
                groups.append((str(lane.get("name") or lane_id), users))
    else:
        users = {n.id for n in g.nodes if n.kind in HUMAN_SIGNOFF_KINDS}
        if len(users) >= SIGNOFF_CHAIN_MIN:
            groups.append(("", users))
    if not groups:
        return _na("signoffs_need_a_gate",
                   f"нет группы с {SIGNOFF_CHAIN_MIN} ручными шагами — цепочка "
                   "согласований не складывается")
    notes: List[str] = []
    offenders: List[str] = []
    for name, users in groups:
        line = _signoff_chain(g, users)
        if not line:
            continue
        offenders.extend(line)
        notes.append(f"«{name or 'дорожка не названа'}»: "
                     + ", ".join(f"'{i}'" for i in line)
                     + " идут подряд по ручным шагам, решения между ними нет")
    if not notes:
        return _pass("signoffs_need_a_gate",
                     f"групп с {SIGNOFF_CHAIN_MIN}+ ручными шагами: {len(groups)}, "
                     "сплошных линий между подписями нет")
    return _fail("signoffs_need_a_gate",
                 "; ".join(notes) + " — цепочка согласований без развилки: ни "
                                    "один шаг не решает, идти ли дальше",
                 list(dict.fromkeys(offenders)))


def _sla_bounds(g: _Graph, wait: _Node, timers: List[_Node]) -> bool:
    """Ограничивает ли какой-нибудь таймер именно это ожидание — по маршруту
    токена, а не по факту «таймер в схеме водится».

    Три легальные формы: таймер-определение в самом ожидании, граничный таймер на
    него (`attachedToRef`) и развилка «ответ или срок» над ожиданием. Третью форму
    оракул выводит из семантики токена сам: развилка обязана быть предком
    ожидания, таймер — её прямой ногой, а сам таймер не должен вести в это
    ожидание (иначе он стоит ниже по маршруту и срабатывает уже после ответа).
    `exclusiveGateway` отбраковывается: его ветки выбираются в момент развилки, и
    срок из такой схемы не следует.

    Совпадение со скорингом (`wait_without_sla`) намеренное: расхождение по
    признаку, за который отвечает один и тот же бизнес-вопрос, означало бы, что
    продукт меряет себя сам. Разводят их только знаменатель `no_overloaded_lane`
    и слова в текстах.
    """
    if not timers:
        return False
    if wait.definition == "timer":
        return True
    for timer in timers:
        if timer.kind == "boundaryEvent":
            if timer.attached_to == wait.id:
                return True
    raced = [t for t in timers if t.kind != "boundaryEvent"
             and wait.id not in _reachable(g, t.id)]
    if not raced:
        return False
    for fork_id in _ancestors(g, wait.id):
        fork = g.by_id.get(fork_id)
        if fork is None or fork.kind == "exclusiveGateway":
            continue
        legs = {e.target for e in g.seq_out.get(fork_id, []) if e.target}
        if len(legs) < 2:
            continue
        if any(timer.id in legs for timer in raced):
            return True
    return False


def _blocking_waits(g: _Graph) -> List[_Node]:
    return [n for n in g.nodes
            if (n.kind == "receiveTask"
                or (n.kind in BLOCKING_CATCH_KINDS
                    and n.definition != "timer"
                    and n.definition not in NOT_A_WAIT_DEFINITIONS))]


def _check_waits_have_sla(g: _Graph, _exp: Mapping[str, Any]) -> Check:
    """У ожидания, которое блокирует маршрут, должен быть срок в модели.

    Ждущий шаг — это `intermediateCatchEvent` не-таймера и `receiveTask`: токен
    стоит, пока не придёт сообщение/сигнал/условие. Срок по BPMN назначается
    структурно: граничным таймером на самом ожидании либо параллельной веткой
    «срок вышел», уходящей с маршрута до ожидания.

    Метка перехода (`link`) и триггер отработки (`compensate`) ожиданиями не
    считаются: первая не ждала бы ничего — токен проходит её к сопоставленной
    мишени, второй будит не внешний мир, а уже отработанный участок. Требовать у
    них срок — просить таймер на узле, где он ничего не означает, и это не
    бизнес-узкое место, а шум: на корпусе одна метка перехода давала четверть
    всех «нарушений» правила.

    Расхождение со скорингом (`wait_without_sla`) по этому признаку закрыто:
    раньше оракул принимал параллельную ветку «срок вышел», а скоринг требовал
    таймер на самом ожидании и советовал `add_boundary_event`, который на
    catch-событие аплайер не принимает. Теперь обе линейки знают три формы и
    обе требуют, чтобы развилкой были `eventBasedGateway` или `parallelGateway`.
    Совпадение намеренное: расхождение по признаку, за который отвечает один и
    тот же бизнес-вопрос, означало бы, что продукт меряет себя сам.

    Текста оракул не читает: ни `documentation` со «сроком 2 дня», ни имя
    «Просрочка SLA» нарушением не покрываются и нарушением не считаются — срок
    обязан быть узлом, иначе его не исполнить.
    """
    waits = _blocking_waits(g)
    if not waits:
        return _na("waits_have_sla",
                   "блокирующих ожиданий (catch-событие не-таймера и не метка "
                   "перехода/триггер отработки, либо receiveTask) в схеме нет")
    timers = [n for n in g.of_kind(TYPED_EVENT_KINDS) if n.definition == "timer"]
    unmeasured = [w for w in waits if not _sla_bounds(g, w, timers)]
    if not unmeasured:
        return _pass("waits_have_sla",
                     f"ожиданий: {len(waits)}, у каждого срок смоделирован таймером")
    notes = [f"{w.id} («{w.name}», {w.kind}) ждёт без срока: ни таймера на самом "
             f"ожидании, ни параллельной ветки «срок вышел» от его развилки"
             for w in unmeasured]
    return _fail("waits_have_sla",
                 "ожидание без срока — висящий маршрут: " + "; ".join(notes)
                 + f" (таймеров в схеме: {len(timers)})",
                 [w.id for w in unmeasured])


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
        "timer_schedule": _check_timer_schedule(g, expectations),
        "boundary_handled": _check_boundary_handled(g, expectations),
        "flow_ends_legal": _check_flow_ends_legal(g, expectations),
        "flows_within_pool": _check_flows_within_pool(g, expectations),
        "message_flow_ends": _check_message_flow_ends(g, expectations),
        "no_unrouted": _check_no_unrouted(g, expectations),
        "loops_have_a_guard": _check_loops_have_a_guard(g, expectations),
        "no_blind_rework": _check_no_blind_rework(g, expectations),
        "pools_not_pingpong": _check_pools_not_pingpong(g, expectations),
        "no_overloaded_lane": _check_no_overloaded_lane(g, expectations),
        "waits_have_sla": _check_waits_have_sla(g, expectations),
        "signoffs_need_a_gate": _check_signoffs_need_a_gate(g, expectations),
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


def deciding_checks(results: Mapping[str, Check]) -> List[Check]:
    """Инварианты, по которым схема считается принятой (`scenario_pass`, ядро `pass@1`).

    Это корректностный слой: нотация плюс заявленные сценарием ожидания.
    Бизнес-слой (`BUSINESS_INVARIANTS`) из гейта исключён — и это не смягчение
    оракула, а следствие того, как устроена линейка:

    * Провал корректности значит «схему нельзя принять»: эталонный ответ не
      проваливает ни одного такого инварианта, иначе сам эталон стал бы браком.
    * Провал бизнес-слоя значит «в процессе есть узкое место»: конечная схема
      всегда держит хотя бы одно, и эталон — не исключение. Замер на текущем
      наборе: `loan_application.good.plan` — возврат той же работы в `A3`
      (`pools_not_pingpong`), `warehouse_delivery.good.plan` — `receiveTask`
      `A8` без единого таймера в модели (`waits_have_sla`).
    * Значит конъюнкция по обоим слоям не выполнима ни для какой схемы: `pass@1`
      навсегда упирался бы в потолок ниже единицы и мерил не контур, а стиль
      эталона — модель, скопировавшая эталон точь-в-точь, получила бы ноль.

    Ничего не перестаёт считаться: каждый бизнес-инвариант по-прежнему
    вычисляется, печатается с id и причиной и попадает в три измеримых места —
    `business/smells` (плотность узких мест против эталона того же сценария),
    `business_agreement` (где оракул и скоринг разошлись) и `defects_repaired` /
    `defects_introduced` у контура улучшения. Гейт перестаёт быть единственной
    точкой, где о них узнают.
    """
    return [c for c in results.values()
            if c.applicable and c.name not in BUSINESS_INVARIANTS]


def business_smells(results: Mapping[str, Check]) -> List[Check]:
    """Проваленные бизнес-инварианты схемы: узкие места, а не брак нотации."""
    return [c for c in results.values()
            if c.applicable and c.name in BUSINESS_INVARIANTS and not c.ok]


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


# ---------------------------------------------------------------------------
# сверка двух слоёв: эвристика продукта против независимого оракула
# ---------------------------------------------------------------------------

# Правило скоринга -> инвариант оракула, который формулирует то же
# бизнес-требование по одному лишь графу. Нужен этот словарь затем, чтобы
# расхождение двух слоёв стало данными: правило и промпт улучшения живут в одном
# модуле и могут разъезжаться вместе, а оракул — снаружи.
#
# `approval_chain` здесь отсутствует намеренно: «четыре ручные задачи подряд —
# уже цепочка согласований» есть суждение о норме времени на решение, и из
# BPMN-семантики оно не выводится — четыре последовательные `userTask` формально
# ничем не хуже трёх. Независимая проверка потребовала бы данных о
# длительностях, а их в схеме нет, поэтому проверять это нечем.
SCORING_TO_ORACLE: Dict[str, str] = {
    "rework_loop": "no_blind_rework",
    "handoff_pingpong": "pools_not_pingpong",
    "lane_overload": "no_overloaded_lane",
    "wait_without_sla": "waits_have_sla",
    "approval_chain": "signoffs_need_a_gate",
}
# Обратный взгляд — для отчёта, который идёт по строкам инвариантов.
ORACLE_TO_SCORING: Dict[str, str] = {v: k for k, v in SCORING_TO_ORACLE.items()}


def _scorer_view(evaluation_details: Optional[Mapping[str, Any]]
                 ) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """(статус, элементы) по правилам из ответа `BPMNScorer.evaluate`.

    Переваривается и весь ответ, и `details_meta`, и плоский `details`
    {правило: bool}: слои сравниваются в разных местах (прогон держит
    `details_meta`, API — `details`), а отказ от одного из форматов обвалил бы
    сверку молча. Плоский `details` не различает «пройдено» и «не применимо» —
    там возвращается `passed`, поэтому сверка по нему грубее: неприменимое
    свойство выглядит пройденным, и в отчёте это надо читать как «слои не
    спорили», а не как «оба нашли норму»."""
    data = evaluation_details or {}
    meta = data.get("details_meta") if isinstance(data, Mapping) else None
    statuses: Dict[str, str] = {}
    elements: Dict[str, List[str]] = {}
    if isinstance(meta, Mapping) and meta:
        for name, entry in meta.items():
            if isinstance(entry, Mapping):
                statuses[name] = str(entry.get("status") or _SCORER_UNKNOWN)
                elements[name] = [str(e) for e in (entry.get("elements") or [])]
            else:
                statuses[name] = _SCORER_PASSED if entry else _SCORER_FAILED
        return statuses, elements
    details = data.get("details") if isinstance(data, Mapping) else None
    if not isinstance(details, Mapping):
        details = data if isinstance(data, Mapping) else {}
    for name, passed in details.items():
        statuses[name] = (_SCORER_PASSED if passed else _SCORER_FAILED)
    return statuses, elements


def business_agreement(checks_xml: Mapping[str, Check],
                       evaluation_details: Optional[Mapping[str, Any]],
                       pairs: Optional[Mapping[str, str]] = None
                       ) -> Dict[str, Dict[str, Any]]:
    """Сходятся ли два слоя в оценках бизнес-свойств одной схемы.

    Вход — инварианты оракула по итоговому XML и ответ скоринга (см.
    `_scorer_view` о форматах). Выход — по строке на каждое бизнес-правило:
    что сказал каждый слой и сошлись ли они. Ничья в этой таблице не считается
    регрессией и не влияет на код прогона: расхождение — это запись человеку о
    том, что одно из двух прочтений процесса неверно, а какое — надо разобрать.

    `verdict`: `agree` — слои сказали одно и то же; `oracle_stricter` — дефект
    видит оракул (скоринг его прощает или не смотрел туда вовсе);
    `scorer_stricter` — наоборот; `not_comparable` — оба молчат и хотя бы один
    объявил свойство неприменимым; `no_data` — ответа одного из слоёв нет.

    `pairs` — другая таблица соответствия. Продуктовый гейт сверяет бизнес-слой
    (`SCORING_TO_ORACLE`), а `eval/coverage` прогоняет этим же механизмом и
    нотационный слой: слои расходились на 35 схемах корпуса именно там, куда
    сводка не смотрела."""
    statuses, elements = _scorer_view(evaluation_details)
    rows: Dict[str, Dict[str, Any]] = {}
    for rule, invariant in (pairs or SCORING_TO_ORACLE).items():
        scorer_status = statuses.get(rule, _SCORER_UNKNOWN)
        check = (checks_xml or {}).get(invariant)
        row: Dict[str, Any] = {
            "rule": rule,
            "invariant": invariant,
            "scorer": scorer_status,
            "scorer_elements": elements.get(rule, []),
            "oracle": _SCORER_UNKNOWN,
            "oracle_ok": None,
            "oracle_applicable": None,
            "oracle_ids": [],
            "oracle_reason": "",
            "verdict": NO_DATA,
        }
        if check is None:
            rows[rule] = row
            continue
        row["oracle"] = ((_SCORER_NA if not check.applicable
                          else _SCORER_PASSED) if check.ok else _SCORER_FAILED)
        row["oracle_ok"] = check.ok
        row["oracle_applicable"] = check.applicable
        row["oracle_ids"] = list(check.ids)
        row["oracle_reason"] = check.reason
        row["verdict"] = _agreement_verdict(row["oracle"], scorer_status)
        rows[rule] = row
    return rows


def _agreement_verdict(oracle: str, scorer: str) -> str:
    if _SCORER_UNKNOWN in (oracle, scorer):
        return NO_DATA
    if oracle == scorer:
        # «Оба не применили» — не согласие: сравнивать было нечего.
        return NOT_COMPARABLE if oracle == _SCORER_NA else AGREE
    if _SCORER_FAILED in (oracle, scorer):
        # Дефект назвал один слой, а второй промолчал или сказал «мне не
        # применимо»: согласием это быть не может, иначе односторонний взгляд на
        # узкое место прятался бы за калиткой чужого порога.
        return ORACLE_STRICTER if oracle == _SCORER_FAILED else SCORER_STRICTER
    # Неприменимость у одного из слоёв при пройденном другом: сравнивать нечего.
    return NOT_COMPARABLE


def business_disagreements(agreement: Mapping[str, Dict[str, Any]]) -> List[str]:
    """Правила, где два слоя не согласны (в обе стороны) — для сводки прогона."""
    return [rule for rule, row in agreement.items()
            if row.get("verdict") in (ORACLE_STRICTER, SCORER_STRICTER)]


def format_business_agreement(agreement: Mapping[str, Dict[str, Any]]) -> List[str]:
    """Строки таблицы для отчёта: слой скоринга, слой оракула и вердикт."""
    lines: List[str] = []
    for rule, row in agreement.items():
        line = (f"{rule} ↔ {row['invariant']}: скоринг={row['scorer']}, "
                f"оракул={row['oracle']} → {row['verdict']}")
        if row["verdict"] in (ORACLE_STRICTER, SCORER_STRICTER, NOT_COMPARABLE) \
                and row.get("oracle_reason"):
            line += f"\n    оракул: {row['oracle_reason']}"
        lines.append(line)
    return lines

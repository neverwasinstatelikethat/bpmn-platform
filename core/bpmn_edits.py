# Домен редактирования BPMN-схем: инвентарь элементов для контекста LLM,
# детерминированный аплайер операций и семантическая починка.
#
# Принципы: каждая операция валидируется отдельно; невалидная — пропускается
# с причиной и подсказкой в отчёте, пакет целиком не падает. Никакой магии
# и эвристик: что не удаётся применить однозначно — сообщаем наверх.
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

try:
    from defusedxml import ElementTree as _SafeET

    def parse_xml(xml_text: str) -> ET.Element:
        return _SafeET.fromstring(xml_text)

except ImportError:  # pragma: no cover — защитный режим до установки пакета
    logging.getLogger(__name__).warning(
        "defusedxml не установлен: XML разбирается стандартным парсером"
    )

    def parse_xml(xml_text: str) -> ET.Element:
        return ET.fromstring(xml_text)


logger = logging.getLogger(__name__)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", BPMNDI_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("di", DI_NS)


def _q(local: str) -> str:
    return f"{{{BPMN_NS}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


TASK_TAGS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "businessRuleTask", "sendTask", "receiveTask", "callActivity", "subProcess",
}
GATEWAY_TAGS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway"}
EVENT_TAGS = {
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
    "boundaryEvent",
}
FLOW_NODE_TAGS = TASK_TAGS | GATEWAY_TAGS | EVENT_TAGS

EVENT_TYPE_TO_TAG = {
    "start": "startEvent",
    "end": "endEvent",
    "intermediatecatch": "intermediateCatchEvent",
    "intermediatecatchEvent": "intermediateCatchEvent",
    "intermediatethrow": "intermediateThrowEvent",
    "intermediateThrowEvent": "intermediateThrowEvent",
}

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'


def _serialize(root: ET.Element) -> str:
    return XML_DECLARATION + ET.tostring(root, encoding="unicode")


class _Index:
    """Снимок структуры схемы для быстрого доступа при применении операций."""

    def __init__(self, root: ET.Element):
        self.root = root
        self.elements: Dict[str, ET.Element] = {}
        self.process_of: Dict[str, ET.Element] = {}
        self.processes: List[ET.Element] = []
        self.participants: List[ET.Element] = []
        self.participant_by_process_id: Dict[str, ET.Element] = {}
        self.collaboration: Optional[ET.Element] = None
        self.sequence_flows: List[ET.Element] = []
        self.message_flows: List[ET.Element] = []
        self._build()

    def _build(self) -> None:
        for elem in self.root.iter():
            if not isinstance(elem.tag, str):
                continue
            tag = _local(elem.tag)
            if tag == "collaboration":
                self.collaboration = elem
            elif tag == "participant":
                self.participants.append(elem)
            elif tag == "process":
                self.processes.append(elem)
            elif tag == "sequenceFlow":
                self.sequence_flows.append(elem)
            elif tag == "messageFlow":
                self.message_flows.append(elem)
            elif tag in FLOW_NODE_TAGS:
                elem_id = elem.get("id")
                if elem_id:
                    self.elements[elem_id] = elem
        for process in self.processes:
            for child in process.iter():
                if not isinstance(child.tag, str):
                    continue
                child_id = child.get("id")
                if child_id and _local(child.tag) in FLOW_NODE_TAGS:
                    self.process_of[child_id] = process
        for participant in self.participants:
            process_ref = participant.get("processRef")
            if process_ref:
                self.participant_by_process_id[process_ref] = participant

    # --- справочные доступы ---

    def find_flow(self, flow_id: str) -> Optional[ET.Element]:
        for flow in self.sequence_flows + self.message_flows:
            if flow.get("id") == flow_id:
                return flow
        return None

    def resolve_process(self, key: Optional[str]) -> Optional[ET.Element]:
        """Участник по id, имени или id процесса → элемент процесса."""
        if not key:
            if len(self.processes) == 1:
                return self.processes[0]
            return None
        for participant in self.participants:
            if key in (participant.get("id"), participant.get("name")):
                process_ref = participant.get("processRef")
                for process in self.processes:
                    if process.get("id") == process_ref:
                        return process
                return None
        for process in self.processes:
            if process.get("id") == key:
                return process
        return None

    def participant_name(self, process: ET.Element) -> str:
        participant = self.participant_by_process_id.get(process.get("id", ""))
        if participant is not None:
            return participant.get("name") or participant.get("id", "")
        return process.get("name") or process.get("id", "")

    def rebuild_flows(self) -> None:
        self.sequence_flows = [
            e for e in self.root.iter()
            if isinstance(e.tag, str) and _local(e.tag) == "sequenceFlow"
        ]
        self.message_flows = [
            e for e in self.root.iter()
            if isinstance(e.tag, str) and _local(e.tag) == "messageFlow"
        ]
        self.elements = {}
        self.process_of = {}
        for process in self.processes:
            for child in process.iter():
                if not isinstance(child.tag, str):
                    continue
                child_id = child.get("id")
                if child_id and _local(child.tag) in FLOW_NODE_TAGS:
                    self.elements[child_id] = child
                    self.process_of[child_id] = process


def build_inventory(xml_text: str) -> Dict[str, Any]:
    """Компактный инвентарь схемы — контекст для планирующих промптов."""
    root = parse_xml(xml_text)
    index = _Index(root)
    participants = []
    for process in index.processes:
        participant = index.participant_by_process_id.get(process.get("id", ""))
        participants.append({
            "id": participant.get("id") if participant is not None else process.get("id"),
            "name": index.participant_name(process),
        })
    elements = []
    for elem_id, elem in index.elements.items():
        process = index.process_of.get(elem_id)
        elements.append({
            "id": elem_id,
            "type": _local(elem.tag),
            "name": elem.get("name", ""),
            "participant": index.participant_name(process) if process is not None else "",
        })
    flows = []
    for flow in index.sequence_flows:
        entry = {
            "id": flow.get("id", ""),
            "kind": "sequence",
            "source": flow.get("sourceRef", ""),
            "target": flow.get("targetRef", ""),
        }
        condition = flow.find(_q("conditionExpression"))
        if condition is not None and (condition.text or "").strip():
            entry["condition"] = condition.text.strip()
        flows.append(entry)
    for flow in index.message_flows:
        flows.append({
            "id": flow.get("id", ""),
            "kind": "message",
            "source": flow.get("sourceRef", ""),
            "target": flow.get("targetRef", ""),
        })
    return {"participants": participants, "elements": elements, "flows": flows}


# ---------------------------------------------------------------------------
# Аплайер операций
# ---------------------------------------------------------------------------

class _Skip(Exception):
    def __init__(self, reason: str, hint: str = ""):
        super().__init__(reason)
        self.hint = hint


def _require_new_id(op_id: Optional[str], index: _Index) -> str:
    if not op_id or not isinstance(op_id, str):
        raise _Skip("не задан id нового элемента", "укажите id с префиксом new_")
    if not op_id.startswith("new_"):
        raise _Skip(
            f"id '{op_id}' без префикса new_",
            "новые элементы обязаны иметь id, начинающийся с new_",
        )
    if op_id in index.elements or index.find_flow(op_id) is not None:
        raise _Skip(f"id '{op_id}' уже занят", "используйте уникальный id")
    return op_id


def _remove_refs(elem: ET.Element, flow_id: str) -> None:
    for ref_tag in ("incoming", "outgoing"):
        for ref in list(elem.findall(_q(ref_tag))):
            if (ref.text or "").strip() == flow_id:
                elem.remove(ref)


def _ensure_collaboration(index: _Index) -> ET.Element:
    if index.collaboration is not None:
        return index.collaboration
    collaboration = ET.Element(_q("collaboration"), {"id": "Collaboration_1"})
    # collaboration по схеме BPMN идёт перед процессами
    first_process = index.processes[0] if index.processes else None
    if first_process is not None:
        position = list(index.root).index(first_process)
        index.root.insert(position, collaboration)
    else:
        index.root.append(collaboration)
    index.collaboration = collaboration
    return collaboration


def _new_flow_id(index: _Index, prefix: str) -> str:
    used = {f.get("id", "") for f in index.sequence_flows + index.message_flows}
    counter = 1
    while f"{prefix}_{counter}" in used:
        counter += 1
    return f"{prefix}_{counter}"


def _insert_after(new_elem: ET.Element, after_id: str, index: _Index) -> List[str]:
    """Вставка элемента после существующего с переподвеской потоков.
    Возвращает пояснения для отчёта."""
    after = index.elements.get(after_id)
    if after is None:
        raise _Skip(
            f"элемент '{after_id}' не найден",
            "используйте существующие id из инвентаря",
        )
    process = index.process_of.get(after_id)
    if process is None:
        raise _Skip(f"не удалось определить пул элемента '{after_id}'")

    outgoing = [
        f for f in index.sequence_flows
        if f.get("sourceRef") == after_id and f.get("targetRef")
    ]
    if _local(after.tag) in GATEWAY_TAGS and len(outgoing) != 1:
        raise _Skip(
            f"у шлюза '{after_id}' несколько исходящих потоков",
            "уточните ветку парой операций disconnect/connect вместо вставки",
        )

    new_id = new_elem.get("id", "")
    # Новый входящий поток: after → new (единственный, независимо от числа
    # переподвешиваемых исходящих).
    in_flow_id = _new_flow_id(index, "new_Flow")
    in_flow = ET.SubElement(process, _q("sequenceFlow"), {
        "id": in_flow_id, "sourceRef": after_id, "targetRef": new_id,
    })
    ET.SubElement(after, _q("outgoing")).text = in_flow_id
    ET.SubElement(new_elem, _q("incoming")).text = in_flow_id

    moved = []
    for flow in outgoing:
        if _local(after.tag) in GATEWAY_TAGS:
            # Условие ветвления должно остаться на потоке, выходящем из шлюза,
            # иначе решение «переезжает» за вставляемый элемент.
            condition = flow.find(_q("conditionExpression"))
            if condition is not None:
                flow.remove(condition)
                in_flow.append(condition)
        flow.set("sourceRef", new_id)
        _remove_refs(after, flow.get("id", ""))
        ET.SubElement(new_elem, _q("outgoing")).text = flow.get("id")
        moved.append(flow.get("id", ""))

    process.append(new_elem)
    index.rebuild_flows()
    if moved:
        return [f"потоки после '{after_id}' переподвешены через '{new_id}'"]
    return []


def _create_sequence_flow(index: _Index, source_id: str, target_id: str,
                          process: ET.Element,
                          condition: Optional[str] = None) -> ET.Element:
    flow_id = _new_flow_id(index, "new_Flow")
    flow = ET.SubElement(process, _q("sequenceFlow"), {
        "id": flow_id,
        "sourceRef": source_id,
        "targetRef": target_id,
    })
    if condition:
        ET.SubElement(flow, _q("conditionExpression")).text = condition
    for elem_id, ref_tag in ((source_id, "outgoing"), (target_id, "incoming")):
        elem = index.elements.get(elem_id)
        if elem is not None:
            ET.SubElement(elem, _q(ref_tag)).text = flow_id
    return flow


def _create_message_flow(index: _Index, source_id: str, target_id: str,
                         name: str = "") -> ET.Element:
    collaboration = _ensure_collaboration(index)
    flow_id = _new_flow_id(index, "new_MessageFlow")
    attrs = {"id": flow_id, "sourceRef": source_id, "targetRef": target_id}
    if name:
        attrs["name"] = name
    flow = ET.SubElement(collaboration, _q("messageFlow"), attrs)
    return flow


def _op_add_participant(op: Dict[str, Any], index: _Index) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя нового пула", "укажите name")
    collaboration = _ensure_collaboration(index)
    process = ET.Element(_q("process"), {
        "id": f"{op_id}_proc", "isExecutable": "true",
    })
    participant = ET.SubElement(collaboration, _q("participant"), {
        "id": op_id, "name": name, "processRef": process.get("id", ""),
    })
    index.root.append(process)
    index.processes.append(process)
    index.participants.append(participant)
    index.participant_by_process_id[process.get("id", "")] = participant
    return [f"создан пул '{name}'"]


def _op_add_node(op: Dict[str, Any], index: _Index, kind: str) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя элемента", "укажите name")
    process = index.resolve_process(op.get("participant"))
    if process is None:
        raise _Skip(
            "пул не определён",
            "укажите participant именем или id пула из инвентаря",
        )

    if kind == "task":
        task_type = op.get("task_type") or "task"
        if task_type not in TASK_TAGS:
            raise _Skip(
                f"неизвестный тип задачи '{task_type}'",
                "допустимы: " + ", ".join(sorted(TASK_TAGS)),
            )
        elem = ET.Element(_q(task_type), {"id": op_id, "name": name})
        tag = task_type
    elif kind == "gateway":
        gateway_type = (op.get("gateway_type") or "exclusive").lower()
        tag = {"exclusive": "exclusiveGateway",
               "parallel": "parallelGateway",
               "inclusive": "inclusiveGateway"}.get(gateway_type)
        if not tag:
            raise _Skip(
                f"неизвестный тип шлюза '{gateway_type}'",
                "допустимы: exclusive, parallel, inclusive",
            )
        elem = ET.Element(_q(tag), {"id": op_id, "name": name})
    else:  # event
        event_type = (op.get("event_type") or "").strip()
        tag = EVENT_TYPE_TO_TAG.get(event_type) or EVENT_TYPE_TO_TAG.get(event_type.lower())
        if not tag:
            raise _Skip(
                f"неизвестный тип события '{event_type}'",
                "допустимы: start, end, intermediateCatch, intermediateThrow",
            )
        elem = ET.Element(_q(tag), {"id": op_id, "name": name})

    after_id = op.get("after")
    notes: List[str] = []
    if tag in ("startEvent", "endEvent"):
        process.append(elem)
        index.rebuild_flows()
        notes.append("событие добавлено без автопривязки потоков")
    elif after_id:
        target_process = index.process_of.get(after_id)
        if target_process is not None and target_process is not process:
            raise _Skip(
                "элемент 'after' находится в другом пуле",
                "вставляйте элемент в тот же пул, где стоит 'after'",
            )
        notes.extend(_insert_after(elem, after_id, index))
    else:
        process.append(elem)
        index.rebuild_flows()
    return notes or [f"добавлен элемент '{name}'"]


def _op_rename(op: Dict[str, Any], index: _Index) -> List[str]:
    elem = index.elements.get(op.get("id") or "")
    if elem is None:
        raise _Skip("элемент не найден", "используйте id из инвентаря")
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано новое имя")
    elem.set("name", name)
    return []


def _op_delete(op: Dict[str, Any], index: _Index) -> List[str]:
    elem_id = op.get("id") or ""
    elem = index.elements.get(elem_id)
    if elem is None:
        raise _Skip("элемент не найден", "используйте id из инвентаря")
    if _local(elem.tag) in ("startEvent", "endEvent"):
        raise _Skip(
            "стартовые и конечные события не удаляются",
            "у процесса должен оставаться вход и выход",
        )
    process = index.process_of.get(elem_id)
    removed_flows = []
    for flow in list(index.sequence_flows) + list(index.message_flows):
        if elem_id in (flow.get("sourceRef"), flow.get("targetRef")):
            parent = _parent_of(index.root, flow)
            if parent is not None:
                parent.remove(flow)
            removed_flows.append(flow.get("id", ""))
    for other in index.elements.values():
        if other is elem:
            continue
        for flow_id in removed_flows:
            _remove_refs(other, flow_id)
    if process is not None:
        process.remove(elem)
    index.rebuild_flows()
    return [f"удалены связанные потоки: {', '.join(removed_flows)}"] if removed_flows else []


def _parent_of(root: ET.Element, child: ET.Element) -> Optional[ET.Element]:
    for parent in root.iter():
        if child in list(parent):
            return parent
    return None


def _op_connect(op: Dict[str, Any], index: _Index) -> List[str]:
    source_id = op.get("source") or ""
    target_id = op.get("target") or ""
    if source_id not in index.elements or target_id not in index.elements:
        raise _Skip(
            "источник или цель не найдены",
            "используйте существующие id из инвентаря",
        )
    flow_type = (op.get("flow_type") or "sequence").lower()
    if flow_type not in ("sequence", "message"):
        raise _Skip("flow_type должен быть sequence или message")
    source_process = index.process_of.get(source_id)
    target_process = index.process_of.get(target_id)
    if flow_type == "sequence" and source_process is not target_process:
        raise _Skip(
            "sequenceFlow между разными пулами недопустим",
            "используйте flow_type=message",
        )
    if flow_type == "message" and source_process is target_process:
        raise _Skip(
            "messageFlow внутри одного пула не имеет смысла",
            "используйте flow_type=sequence",
        )
    existing = [
        f for f in index.sequence_flows + index.message_flows
        if f.get("sourceRef") == source_id and f.get("targetRef") == target_id
    ]
    if existing:
        raise _Skip("такой поток уже существует")
    if flow_type == "sequence":
        condition = (op.get("condition") or "").strip() or None
        _create_sequence_flow(index, source_id, target_id, source_process, condition)
    else:
        _create_message_flow(index, source_id, target_id)
    index.rebuild_flows()
    return []


def _op_disconnect(op: Dict[str, Any], index: _Index) -> List[str]:
    flow_id = op.get("flow") or ""
    flow = index.find_flow(flow_id)
    if flow is None:
        raise _Skip("поток не найден", "используйте id потока из инвентаря")
    parent = _parent_of(index.root, flow)
    if parent is not None:
        parent.remove(flow)
    for elem in index.elements.values():
        _remove_refs(elem, flow_id)
    index.rebuild_flows()
    return []


def _op_move(op: Dict[str, Any], index: _Index) -> List[str]:
    elem_id = op.get("id") or ""
    elem = index.elements.get(elem_id)
    if elem is None:
        raise _Skip("элемент не найден", "используйте id из инвентаря")
    current = index.process_of.get(elem_id)
    target = index.resolve_process(op.get("participant"))
    if target is None:
        raise _Skip("целевой пул не найден", "укажите имя или id пула")
    if current is target:
        return []
    broken = []
    for flow in list(index.sequence_flows):
        ends = (flow.get("sourceRef"), flow.get("targetRef"))
        if elem_id in ends:
            other = ends[0] if ends[1] == elem_id else ends[1]
            if index.process_of.get(other) is not target:
                parent = _parent_of(index.root, flow)
                if parent is not None:
                    parent.remove(flow)
                broken.append(flow.get("id", ""))
    for other in index.elements.values():
        if other is not elem:
            for flow_id in broken:
                _remove_refs(other, flow_id)
    for ref_tag in ("incoming", "outgoing"):
        for ref in list(elem.findall(_q(ref_tag))):
            if (ref.text or "").strip() in broken:
                elem.remove(ref)
    if current is not None:
        current.remove(elem)
    target.append(elem)
    index.rebuild_flows()
    if broken:
        return [f"межпульные потоки удалены: {', '.join(broken)}"]
    return []


_HANDLERS = {
    "add_task": lambda op, i: _op_add_node(op, i, "task"),
    "add_gateway": lambda op, i: _op_add_node(op, i, "gateway"),
    "add_event": lambda op, i: _op_add_node(op, i, "event"),
    "add_participant": _op_add_participant,
    "rename": _op_rename,
    "delete": _op_delete,
    "connect": _op_connect,
    "disconnect": _op_disconnect,
    "move_to_participant": _op_move,
}


def apply_operations(xml_text: str,
                     operations: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Применяет пакет операций. Возвращает (новый XML, отчёт).

    Отчёт: {"status": "success"|"partial",
            "applied": [{"op", "detail"...}], "skipped": [{"op", "reason", "hint"}]}
    """
    root = parse_xml(xml_text)
    index = _Index(root)
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for op in operations or []:
        if not isinstance(op, dict):
            skipped.append({"op": str(op), "reason": "операция не объект",
                            "hint": "каждая операция — JSON-объект с полем op"})
            continue
        op_name = op.get("op")
        handler = _HANDLERS.get(op_name or "")
        if handler is None:
            skipped.append({"op": op_name or "?",
                            "reason": "неизвестная операция",
                            "hint": "допустимы: " + ", ".join(sorted(_HANDLERS))})
            continue
        try:
            notes = handler(op, index)
            entry = {"op": op_name}
            for key in ("id", "element_id", "flow", "source", "target", "name"):
                if op.get(key):
                    entry[key] = op[key]
            if notes:
                entry["note"] = "; ".join(notes)
            applied.append(entry)
        except _Skip as skip:
            skipped.append({"op": op_name, "reason": str(skip), "hint": skip.hint})
        except Exception as e:  # noqa: BLE001 — одна операция не роняет пакет
            logger.exception("Операция %s упала", op_name)
            skipped.append({"op": op_name, "reason": f"внутренняя ошибка: {e}",
                            "hint": "упростите операцию"})
    status = "success" if not skipped else ("partial" if applied else "failed")
    return _serialize(root), {"status": status, "applied": applied, "skipped": skipped}


# ---------------------------------------------------------------------------
# Семантическая починка
# ---------------------------------------------------------------------------

def validate_and_repair(xml_text: str) -> Tuple[str, List[str]]:
    """Детерминированная починка после аплая: висящие потоки, согласованность
    входящих/исходящих ссылок, дубли потоков, старт/энд на каждый процесс."""
    root = parse_xml(xml_text)
    index = _Index(root)
    notes: List[str] = []

    known_ids = set(index.elements.keys())

    # 1. Потоки с несуществующими концами удаляются.
    for flow in list(index.sequence_flows) + list(index.message_flows):
        if flow.get("sourceRef") not in known_ids or flow.get("targetRef") not in known_ids:
            parent = _parent_of(root, flow)
            if parent is not None:
                parent.remove(flow)
            notes.append(f"удалён висящий поток {flow.get('id')}")
    index.rebuild_flows()

    # 2. Дубли потоков (одинаковые вид/источник/цель) — оставляем первый.
    seen = set()
    for flow in list(index.sequence_flows) + list(index.message_flows):
        key = (_local(flow.tag), flow.get("sourceRef"), flow.get("targetRef"))
        if key in seen:
            parent = _parent_of(root, flow)
            if parent is not None:
                parent.remove(flow)
            notes.append(f"удалён дубль потока {flow.get('id')}")
        else:
            seen.add(key)
    index.rebuild_flows()

    # 3. incoming/outgoing пересобираются из фактических потоков.
    incoming: Dict[str, List[str]] = {elem_id: [] for elem_id in known_ids}
    outgoing: Dict[str, List[str]] = {elem_id: [] for elem_id in known_ids}
    for flow in index.sequence_flows:
        source = flow.get("sourceRef")
        target = flow.get("targetRef")
        flow_id = flow.get("id")
        if source in outgoing:
            outgoing[source].append(flow_id)
        if target in incoming:
            incoming[target].append(flow_id)
    for elem_id, elem in index.elements.items():
        for ref in list(elem.findall(_q("incoming"))) + list(elem.findall(_q("outgoing"))):
            elem.remove(ref)
    for elem_id, elem in index.elements.items():
        # Порядок тегов в BPMN: документация, входящие, исходящие — для нас
        # достаточно просто наличия; вставляем в начало для стабильности.
        for pos, flow_id in enumerate(incoming[elem_id]):
            ref = ET.Element(_q("incoming"))
            ref.text = flow_id
            elem.insert(pos, ref)
        base = len(incoming[elem_id])
        for offset, flow_id in enumerate(outgoing[elem_id]):
            ref = ET.Element(_q("outgoing"))
            ref.text = flow_id
            elem.insert(base + offset, ref)

    # 4. У каждого процесса — хотя бы один старт и один энд.
    counter = 1
    for process in index.processes:
        tags = [_local(child.tag) for child in process
                if isinstance(child.tag, str)]
        if not any(t in FLOW_NODE_TAGS for t in tags):
            continue
        if "startEvent" not in tags:
            event_id = f"StartEvent_new_{counter}"
            ET.SubElement(process, _q("startEvent"), {
                "id": event_id, "name": "Старт",
            })
            counter += 1
            notes.append(f"добавлено стартовое событие {event_id}")
        if "endEvent" not in tags:
            event_id = f"EndEvent_new_{counter}"
            ET.SubElement(process, _q("endEvent"), {
                "id": event_id, "name": "Завершение",
            })
            counter += 1
            notes.append(f"добавлено конечное событие {event_id}")

    return _serialize(root), notes

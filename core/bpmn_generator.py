# Генерация BPMN-схем по текстовому описанию.
#
# Один структурированный вызов LLM заменяет весь NLP-конвейер (перевод,
# кореференция, NER, классификаторы): модель сразу возвращает структуру
# процесса в JSON. Структура считается недоверенной и чинится
# детерминированным repair_structure, а не отбрасывается.
#
# XML генерируется только семантический: координаты не выдаются — фронтенд
# всегда прогоняет схему через bpmn-auto-layout, ему достаточно пустого
# скелета BPMNDiagram/BPMNPlane.
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMError, call_json

logger = logging.getLogger(__name__)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", BPMNDI_NS)

TASK_KINDS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "businessRuleTask", "sendTask", "receiveTask",
}
GATEWAY_KINDS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway"}
EVENT_KINDS = {
    "startEvent", "endEvent",
    "intermediateCatchEvent", "intermediateThrowEvent",
}
ALL_KINDS = TASK_KINDS | GATEWAY_KINDS | EVENT_KINDS

MAX_TEXT_CHARS = 10_000
MAX_PARTICIPANTS = 10
MAX_ELEMENTS = 60
MAX_FLOWS = 120
NAME_LIMIT = 120

_SYSTEM_PROMPT = """Ты — аналитик бизнес-процессов. По текстовому описанию \
построй структуру BPMN 2.0 процесса.

Верни СТРОГО ОДИН JSON-объект без пояснений и без блоков кода:
{
  "participants": ["название пула", ...],
  "elements": [
    {"id": "A1", "kind": "task", "name": "Название шага", "participant": "название пула"}
  ],
  "flows": [
    {"source": "A1", "target": "A2", "kind": "sequence", "condition": ""}
  ]
}

Правила:
- kind элемента: один из """ + ", ".join(sorted(ALL_KINDS)) + """.
- Используй userTask для действий людей, serviceTask для систем и сервисов.
- Каждый пул (participant) содержит ровно один процесс; у каждого пула должны \
быть хотя бы один startEvent и один endEvent.
- flows: kind="sequence" — только внутри одного пула; \
условие (текст ветки) указывай в поле condition только на исходящих потоках \
шлюза (обычно после exclusiveGateway). kind="message" — только между разными \
пулами (взаимодействие участников).
- Каждый элемент (кроме стартового события) должен быть достижим: \
на него входит хотя бы один поток.
- Отвечай на языке описания процесса (названия шагов и пулов — как в тексте).
"""


class GenerationError(RuntimeError):
    """Сбой генерации с пользовательским сообщением на русском."""


class BPMNGenerator:
    """Генератор BPMN: один вызов LLM + детерминированная починка структуры."""

    def generate(self, text: str) -> Dict[str, Any]:
        """Основной конвейер. Синхронный — из async вызывать через
        asyncio.to_thread."""
        start_time = time.time()
        try:
            if not isinstance(text, str) or not text.strip():
                raise GenerationError("Описание процесса не должно быть пустым")
            if len(text) > MAX_TEXT_CHARS:
                raise GenerationError(
                    f"Описание слишком длинное (максимум {MAX_TEXT_CHARS} символов). "
                    "Сократите его и попробуйте снова."
                )

            structure = self._extract_structure(text)
            repaired, notes = repair_structure(structure)
            for note in notes:
                logger.info("Починка структуры: %s", note)
            bpmn_xml = self._generate_bpmn_xml(repaired)

            return {
                "status": "success",
                "bpmn": bpmn_xml,
                "structure": repaired,
                "notes": notes,
                "time_elapsed": time.time() - start_time,
            }
        except GenerationError as e:
            logger.warning("Генерация отклонена: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "step": "generation",
                "time_elapsed": time.time() - start_time,
            }
        except LLMError as e:
            logger.error("LLM недоступен при генерации: %s", e)
            return {
                "status": "error",
                "error": "Сервис генерации временно недоступен. Попробуйте позже.",
                "step": "llm",
                "time_elapsed": time.time() - start_time,
            }
        except ValueError as e:
            logger.error("Некорректный ответ модели при генерации: %s", e)
            return {
                "status": "error",
                "error": "Не удалось построить схему по этому описанию. "
                         "Попробуйте переформулировать запрос.",
                "step": "parse",
                "time_elapsed": time.time() - start_time,
            }
        except Exception as e:  # noqa: BLE001 — наружу только общий сбой
            logger.error("Непредвиденная ошибка генерации: %s", e, exc_info=True)
            return {
                "status": "error",
                "error": "Внутренняя ошибка генерации. Попробуйте позже.",
                "step": "internal",
                "time_elapsed": time.time() - start_time,
            }

    # --- шаг 1: структура от LLM ---

    def _extract_structure(self, text: str) -> Dict[str, Any]:
        data = call_json(_SYSTEM_PROMPT, f"Описание процесса:\n{text}",
                         temperature=0.2, max_tokens=8000)
        if not isinstance(data, dict):
            raise ValueError("ответ модели не объект")
        return data

    # --- шаг 2: XML ---

    def _generate_bpmn_xml(self, structure: Dict[str, Any]) -> str:
        definitions = ET.Element(f"{{{BPMN_NS}}}definitions", {
            "id": "Definitions_1",
            "targetNamespace": "http://bpmn.io/schema/bpmn",
        })

        participants: List[Dict[str, Any]] = structure["participants"]
        elements: List[Dict[str, Any]] = structure["elements"]
        flows: List[Dict[str, Any]] = structure["flows"]
        element_by_id = {e["id"]: e for e in elements}

        process_ids: Dict[str, str] = {}
        participant_ids: Dict[str, str] = {}
        for idx, p in enumerate(participants, 1):
            process_ids[p["name"]] = f"Process_{idx}"
            participant_ids[p["name"]] = f"Participant_{idx}"

        processes: Dict[str, ET.Element] = {}
        for p in participants:
            process = ET.SubElement(definitions, f"{{{BPMN_NS}}}process", {
                "id": process_ids[p["name"]],
                "isExecutable": "true",
            })
            processes[p["name"]] = process

        for e in elements:
            process = processes[e["participant"]]
            elem = ET.SubElement(process, f"{{{BPMN_NS}}}{e['kind']}", {
                "id": e["id"],
                "name": e["name"],
            })
            for f in flows:
                if f["kind"] == "sequence" and f["target"] == e["id"]:
                    ET.SubElement(elem, f"{{{BPMN_NS}}}incoming").text = f["id"]
                if f["kind"] == "sequence" and f["source"] == e["id"]:
                    ET.SubElement(elem, f"{{{BPMN_NS}}}outgoing").text = f["id"]

        for f in flows:
            if f["kind"] != "sequence":
                continue
            source_participant = element_by_id[f["source"]]["participant"]
            process = processes[source_participant]
            flow_elem = ET.SubElement(process, f"{{{BPMN_NS}}}sequenceFlow", {
                "id": f["id"],
                "sourceRef": f["source"],
                "targetRef": f["target"],
            })
            if f.get("condition"):
                cond = ET.SubElement(flow_elem, f"{{{BPMN_NS}}}conditionExpression")
                cond.text = f["condition"]

        collaboration = ET.SubElement(definitions, f"{{{BPMN_NS}}}collaboration",
                                      {"id": "Collaboration_1"})
        for p in participants:
            ET.SubElement(collaboration, f"{{{BPMN_NS}}}participant", {
                "id": participant_ids[p["name"]],
                "name": p["name"],
                "processRef": process_ids[p["name"]],
            })
        for f in flows:
            if f["kind"] != "message":
                continue
            attrs = {
                "id": f["id"],
                "sourceRef": f["source"],
                "targetRef": f["target"],
            }
            if f.get("condition"):
                attrs["name"] = f["condition"]
            ET.SubElement(collaboration, f"{{{BPMN_NS}}}messageFlow", attrs)

        # Пустой DI-скелет: координаты достраивает фронтенд (bpmn-auto-layout).
        diagram = ET.SubElement(definitions, f"{{{BPMNDI_NS}}}BPMNDiagram",
                                {"id": "BPMNDiagram_1"})
        ET.SubElement(diagram, f"{{{BPMNDI_NS}}}BPMNPlane", {
            "id": "BPMNPlane_1",
            "bpmnElement": "Collaboration_1",
        })

        return XML_DECLARATION + ET.tostring(definitions, encoding="unicode")


# --- детерминированная починка структуры ---

_ID_CLEAN = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_id(raw: Any, fallback: str) -> str:
    text = str(raw or "").strip()
    cleaned = _ID_CLEAN.sub("_", text)
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"e_{cleaned}"
    return cleaned


def repair_structure(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Приводит произвольный ответ модели к валидной структуре.

    Ничего не отбрасывает целиком: битые значения чинит, невозможные связи
    удаляет по одной, отсутствующие старт/финиш добавляет. Возвращает
    (структура, список внесённых правок).
    """
    notes: List[str] = []

    # Участники
    participants: List[Dict[str, Any]] = []
    seen_names: set = set()
    for item in raw.get("participants") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if not name:
            continue
        name = name[:NAME_LIMIT]
        if name in seen_names:
            notes.append(f"Дубликат пула «{name}» пропущен")
            continue
        seen_names.add(name)
        participants.append({"name": name})
        if len(participants) >= MAX_PARTICIPANTS:
            notes.append("Лишние пулы отброшены (максимум %d)" % MAX_PARTICIPANTS)
            break
    if not participants:
        participants.append({"name": "Процесс"})
        notes.append("Пул не указан — создан пул «Процесс»")
    pool_names = {p["name"] for p in participants}

    # Элементы
    elements: List[Dict[str, Any]] = []
    used_ids: set = set()
    kind_aliases = {k.lower(): k for k in ALL_KINDS}
    raw_elements = raw.get("elements") or []
    if not isinstance(raw_elements, list):
        raw_elements = []
    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        if len(elements) >= MAX_ELEMENTS:
            notes.append("Лишние элементы отброшены (максимум %d)" % MAX_ELEMENTS)
            break

        elem_id = _sanitize_id(item.get("id"), f"Elem_{len(elements) + 1}")
        while elem_id in used_ids:
            elem_id = f"{elem_id}_x"
        if str(item.get("id") or "").strip() != elem_id:
            notes.append(f"Идентификатор элемента приведён к «{elem_id}»")
        used_ids.add(elem_id)

        kind_raw = str(item.get("kind") or item.get("type") or "").strip()
        kind = kind_aliases.get(kind_raw.lower())
        if kind is None:
            notes.append(f"Неизвестный тип «{kind_raw}» элемента {elem_id} → task")
            kind = "task"

        participant = str(item.get("participant") or "").strip()
        if participant not in pool_names:
            participant = participants[0]["name"]
            notes.append(f"Элемент {elem_id} перенесён в пул «{participant}»")

        name = str(item.get("name") or "").strip()[:NAME_LIMIT] or kind

        elements.append({
            "id": elem_id,
            "kind": kind,
            "name": name,
            "participant": participant,
        })

    element_ids = {e["id"] for e in elements}
    element_by_id = {e["id"]: e for e in elements}

    # Потоки
    flows: List[Dict[str, Any]] = []
    seen_pairs: set = set()
    raw_flows = raw.get("flows") or raw.get("sequence_flows") or []
    if not isinstance(raw_flows, list):
        raw_flows = []
    for item in raw_flows:
        if not isinstance(item, dict):
            continue
        if len(flows) >= MAX_FLOWS:
            notes.append("Лишние потоки отброшены (максимум %d)" % MAX_FLOWS)
            break
        source = _sanitize_id(item.get("source") or item.get("sourceRef"), "")
        target = _sanitize_id(item.get("target") or item.get("targetRef"), "")
        if source not in element_ids or target not in element_ids \
                or source == target:
            notes.append(f"Поток {source or '?'} → {target or '?'} удалён: "
                         "неизвестный элемент или петля")
            continue

        kind_raw = str(item.get("kind") or item.get("type") or "sequence").lower()
        kind = "message" if "message" in kind_raw else "sequence"

        cross_pool = (element_by_id[source]["participant"]
                      != element_by_id[target]["participant"])
        if kind == "sequence" and cross_pool:
            kind = "message"
            notes.append(f"Поток {source} → {target} между пулами "
                         "преобразован в потоковое сообщение")
        elif kind == "message" and not cross_pool:
            notes.append(f"Поток-сообщение {source} → {target} внутри пула удалён")
            continue

        # Для потока-сообщения текст условия становится именем сообщения.
        condition = str(item.get("condition") or item.get("name") or "").strip()
        condition = condition[:200]
        if condition and kind == "sequence" \
                and element_by_id[source]["kind"] not in GATEWAY_KINDS:
            notes.append(f"Условие на потоке {source} → {target} сохранено, "
                         "но источник не шлюз")

        pair = (kind, source, target)
        if pair in seen_pairs:
            notes.append(f"Дубликат потока {source} → {target} пропущен")
            continue
        seen_pairs.add(pair)

        flow_id = _sanitize_id(item.get("id"), f"Flow_{len(flows) + 1}")
        while flow_id in used_ids:
            flow_id = f"{flow_id}_x"
        used_ids.add(flow_id)

        flows.append({
            "id": flow_id,
            "kind": kind,
            "source": source,
            "target": target,
            "condition": condition,
        })

    # Старт/финиш в каждом пуле
    for p in participants:
        pool = [e for e in elements if e["participant"] == p["name"]]
        seq = [f for f in flows if f["kind"] == "sequence"]
        has_start = any(e["kind"] == "startEvent" for e in pool)
        has_end = any(e["kind"] == "endEvent" for e in pool)

        if not has_start:
            start_id = _unique_id(used_ids, f"StartEvent_{p['name'][:8]}")
            used_ids.add(start_id)
            elements.insert(0, {
                "id": start_id,
                "kind": "startEvent",
                "name": "Старт",
                "participant": p["name"],
            })
            entry = _pick_entry_node(pool, seq)
            if entry:
                flows.append(_new_flow(used_ids, start_id, entry["id"]))
            notes.append(f"В пул «{p['name']}» добавлено стартовое событие")
            pool = [e for e in elements if e["participant"] == p["name"]]

        if not has_end:
            end_id = _unique_id(used_ids, f"EndEvent_{p['name'][:8]}")
            used_ids.add(end_id)
            elements.append({
                "id": end_id,
                "kind": "endEvent",
                "name": "Завершение",
                "participant": p["name"],
            })
            exit_node = _pick_exit_node(pool, seq)
            if exit_node:
                flows.append(_new_flow(used_ids, exit_node["id"], end_id))
            notes.append(f"В пул «{p['name']}» добавлено завершающее событие")

    repaired = {
        "participants": participants,
        "elements": elements,
        "flows": flows,
    }
    if not elements:
        raise GenerationError(
            "Не удалось выделить ни одного шага процесса из описания. "
            "Опишите процесс подробнее."
        )
    return repaired, notes


def _unique_id(used: set, base: str) -> str:
    candidate = _sanitize_id(base, "Elem")
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{_sanitize_id(base, 'Elem')}_{n}"
    return candidate


def _new_flow(used_ids: set, source: str, target: str) -> Dict[str, Any]:
    flow_id = _unique_id(used_ids, f"Flow_{source}_{target}")
    used_ids.add(flow_id)
    return {"id": flow_id, "kind": "sequence",
            "source": source, "target": target, "condition": ""}


def _pick_entry_node(pool: List[Dict[str, Any]],
                     seq: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Первый узел пула без входящих потоков (не старт)."""
    targets = {f["target"] for f in seq}
    for e in pool:
        if e["kind"] == "startEvent":
            continue
        if e["id"] not in targets:
            return e
    for e in pool:
        if e["kind"] not in ("startEvent", "endEvent"):
            return e
    return None


def _pick_exit_node(pool: List[Dict[str, Any]],
                    seq: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Последний узел пула без исходящих потоков (не финиш)."""
    sources = {f["source"] for f in seq}
    for e in reversed(pool):
        if e["kind"] == "endEvent":
            continue
        if e["id"] not in sources:
            return e
    for e in reversed(pool):
        if e["kind"] not in ("startEvent", "endEvent"):
            return e
    return None

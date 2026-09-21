# Генерация BPMN-схем по текстовому описанию.
#
# Один структурированный вызов LLM заменяет весь NLP-конвейер (перевод,
# кореференция, NER, классификаторы): модель сразу возвращает структуру
# процесса в JSON. Структура считается недоверенной и чинится
# детерминированным repair_structure, а не отбрасывается.
#
# Разделение пула и дорожки — смысловое: пул = независимый участник
# (организация или внешняя система), роли сотрудников и подсистемы одной
# организации = дорожки внутри одного пула. Если склеить роли в пулы,
# линейный бизнес-поток распадается на цепочку messageFlow, и починить это
# постфактум невозможно — поэтому промпт задаёт его явно, а repair не даёт
# ошибке модели превратиться в «валидную» схему молча.
#
# XML генерируется только семантический: координаты не выдаются — фронтенд
# всегда прогоняет схему через bpmn-auto-layout, ему достаточно пустого
# скелета BPMNDiagram/BPMNPlane.
import difflib
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

from .llm_client import LLMError, LLMTruncatedError, call_json

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
MAX_LANES = 30
MAX_ELEMENTS = 60
MAX_FLOWS = 120
NAME_LIMIT = 120
# Исправлений достижимости не больше, чем элементов: иначе генератор
# дорисует связи туда, где модель их не описала, и схема «починится» сама.
MAX_REACH_FLOWS = 2 * MAX_ELEMENTS
# Порог схожести названий пулов: ниже — слишком разные сущности, выше —
# опечатки и падежи («Бухгалтерия» / «Бухгалтерией»).
POOL_MATCH_CUTOFF = 0.6

_SYSTEM_PROMPT = """Ты — аналитик бизнес-процессов. По текстовому описанию \
построй структуру BPMN 2.0 процесса.

Верни СТРОГО ОДИН JSON-объект без пояснений и без блоков кода:
{
  "participants": ["название пула", ...],
  "lanes": [
    {"id": "L1", "name": "название дорожки", "participant": "название пула"}
  ],
  "elements": [
    {"id": "A1", "kind": "task", "name": "Название шага", \
"participant": "название пула", "lane": "L1"}
  ],
  "flows": [
    {"id": "F1", "source": "A1", "target": "A2", "kind": "sequence", \
"condition": ""}
  ]
}

Пулы и дорожки:
- Пул (participant) — независимый участник процесса: организация, внешний \
контрагент или внешняя система. Один пул содержит ровно один процесс.
- Роли сотрудников, отделы и подсистемы одной организации — это ДОРОЖКИ \
(lanes) внутри одного пула, а не отдельные пулы.
- Если все шаги выполняют сотрудники одной организации — один пул и дорожки \
по ролям. Заводи второй пул только когда действует другая организация или \
внешняя система.
- participant у элемента и у дорожки — название пула из "participants"; \
lane у элемента — id дорожки из "lanes". Элемент стоит в дорожке своего пула.

Потоки:
- kind="sequence" — только внутри одного пула, в том числе между его \
дорожками; условие (текст ветки) указывай в поле condition только на \
исходящих потоках шлюза.
- kind="message" — только между элементами РАЗНЫХ пулов. Покажи им \
взаимодействие организаций, а не передачу работы между сотрудниками: \
бизнес-поток внутри одной организации ведётся sequence-потоками.
- exclusiveGateway рисуй там, где поток действительно раздваивается: \
у него должно быть минимум два исходящих потока. Иначе это обычный шаг (task).
- Каждый элемент, кроме startEvent, должен быть достижим: на него входит \
хотя бы один sequence-поток.
- Каждый элемент, кроме endEvent, должен иметь исходящий sequence-поток \
(внутри своего пула) — иначе процесс в этом пуле обрывается.

Пример. «Инициатор заводит заявку, бухгалтерия согласует и выдаёт деньги» — \
это роли одной организации, поэтому пул один, а роли стали дорожками \
(L1 и L2); передача работы между дорожками идёт sequence-потоком:
{"participants": ["ВкусВилл"],
 "lanes": [{"id": "L1", "name": "Инициатор", "participant": "ВкусВилл"},
           {"id": "L2", "name": "Бухгалтерия", "participant": "ВкусВилл"}],
 "elements": [
   {"id": "S1", "kind": "startEvent", "name": "Потребность в деньгах", \
"participant": "ВкусВилл", "lane": "L1"},
   {"id": "A1", "kind": "userTask", "name": "Завести заявку", \
"participant": "ВкусВилл", "lane": "L1"},
   {"id": "A2", "kind": "userTask", "name": "Согласовать заявку", \
"participant": "ВкусВилл", "lane": "L2"},
   {"id": "A3", "kind": "userTask", "name": "Выдать деньги", \
"participant": "ВкусВилл", "lane": "L2"},
   {"id": "E1", "kind": "endEvent", "name": "Деньги выданы", \
"participant": "ВкусВилл", "lane": "L2"}
 ],
 "flows": [
   {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence", "condition": ""},
   {"id": "F2", "source": "A1", "target": "A2", "kind": "sequence", "condition": ""},
   {"id": "F3", "source": "A2", "target": "A3", "kind": "sequence", "condition": ""},
   {"id": "F4", "source": "A3", "target": "E1", "kind": "sequence", "condition": ""}
 ]}

Прочее:
- kind элемента: один из """ + ", ".join(sorted(ALL_KINDS)) + """.
- Используй userTask для действий людей, serviceTask для систем и сервисов.
- У каждого пула должны быть хотя бы один startEvent и один endEvent.
- Отвечай на языке описания процесса (названия шагов, пулов и дорожек — как \
в тексте).
"""


def _q(local: str) -> str:
    return f"{{{BPMN_NS}}}{local}"


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
        except LLMTruncatedError as e:
            # Повтор здесь бесполезен: max_tokens тот же, ответ обрежется
            # снова. Пользователю нужно сказать, что сокращать.
            logger.error("Ответ модели обрезан при генерации: %s", e)
            return {
                "status": "error",
                "error": "Ответ модели обрезан по лимиту токенов — сократите "
                         "описание процесса и попробуйте снова.",
                "step": "llm_truncated",
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
        definitions = ET.Element(_q("definitions"), {
            "id": "Definitions_1",
            "targetNamespace": "http://bpmn.io/schema/bpmn",
        })

        participants: List[Dict[str, Any]] = structure["participants"]
        lanes: List[Dict[str, Any]] = structure.get("lanes") or []
        elements: List[Dict[str, Any]] = structure["elements"]
        flows: List[Dict[str, Any]] = structure["flows"]
        element_by_id = {e["id"]: e for e in elements}

        # Индексы считаем один раз: обход всех потоков ради каждого элемента
        # давал бы O(n²) и мешал читать код emission-шага.
        incoming: Dict[str, List[str]] = {}
        outgoing: Dict[str, List[str]] = {}
        for f in flows:
            if f["kind"] != "sequence":
                continue
            outgoing.setdefault(f["source"], []).append(f["id"])
            incoming.setdefault(f["target"], []).append(f["id"])
        lane_refs: Dict[str, List[str]] = {}
        for e in elements:
            if e.get("lane"):
                lane_refs.setdefault(e["lane"], []).append(e["id"])
        lanes_by_pool: Dict[str, List[Dict[str, Any]]] = {}
        for lane in lanes:
            lanes_by_pool.setdefault(lane["participant"], []).append(lane)

        process_ids: Dict[str, str] = {}
        participant_ids: Dict[str, str] = {}
        for idx, p in enumerate(participants, 1):
            process_ids[p["name"]] = f"Process_{idx}"
            participant_ids[p["name"]] = f"Participant_{idx}"

        processes: Dict[str, ET.Element] = {}
        for idx, p in enumerate(participants, 1):
            process = ET.SubElement(definitions, _q("process"), {
                "id": process_ids[p["name"]],
                "isExecutable": "true",
            })
            processes[p["name"]] = process
            # laneSet идёт до потоковых узлов и содержит только ссылки
            # flowNodeRef: сами узлы и sequenceFlow остаются детьми process —
            # ровно так пишет bpmn-js и так читает инвентарь core/bpmn_edits.
            pool_lanes = lanes_by_pool.get(p["name"]) or []
            if not pool_lanes:
                continue
            lane_set = ET.SubElement(process, _q("laneSet"), {"id": f"LaneSet_{idx}"})
            for lane in pool_lanes:
                lane_elem = ET.SubElement(lane_set, _q("lane"), {
                    "id": lane["id"],
                    "name": lane["name"],
                })
                for ref in lane_refs.get(lane["id"], []):
                    ET.SubElement(lane_elem, _q("flowNodeRef")).text = ref

        for e in elements:
            process = processes[e["participant"]]
            elem = ET.SubElement(process, _q(e["kind"]), {
                "id": e["id"],
                "name": e["name"],
            })
            for flow_id in incoming.get(e["id"], []):
                ET.SubElement(elem, _q("incoming")).text = flow_id
            for flow_id in outgoing.get(e["id"], []):
                ET.SubElement(elem, _q("outgoing")).text = flow_id

        for f in flows:
            if f["kind"] != "sequence":
                continue
            source_participant = element_by_id[f["source"]]["participant"]
            process = processes[source_participant]
            flow_elem = ET.SubElement(process, _q("sequenceFlow"), {
                "id": f["id"],
                "sourceRef": f["source"],
                "targetRef": f["target"],
            })
            if f.get("condition"):
                cond = ET.SubElement(flow_elem, _q("conditionExpression"))
                cond.text = f["condition"]

        collaboration = ET.SubElement(definitions, _q("collaboration"),
                                      {"id": "Collaboration_1"})
        for p in participants:
            ET.SubElement(collaboration, _q("participant"), {
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
            ET.SubElement(collaboration, _q("messageFlow"), attrs)

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
_NOT_IN_NAME = re.compile(r"[^0-9a-zа-яё]+")


def _sanitize_id(raw: Any, fallback: str) -> str:
    text = str(raw or "").strip()
    cleaned = _ID_CLEAN.sub("_", text)
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"e_{cleaned}"
    return cleaned


def _norm_name(value: Any) -> str:
    """Имя для сопоставления: без регистра, «_» и всего, что не буква/цифра."""
    return _NOT_IN_NAME.sub("", str(value or "").lower())


def _clip(text: str, limit: int, notes: List[str], subject: str) -> str:
    if len(text) <= limit:
        return text
    notes.append(f"{subject} укорочен до {limit} символов")
    return text[:limit]


def _pool_exact(name: str, participants: List[Dict[str, Any]]) -> Optional[str]:
    """Пул по точному или нормализованному имени."""
    norm = _norm_name(name)
    for p in participants:
        if p["name"] == name or _norm_name(p["name"]) == norm:
            return p["name"]
    return None


def _pool_close(name: str, participants: List[Dict[str, Any]]) -> Optional[str]:
    """Пул по схожести имени — спасает от падежей и опечаток модели."""
    norm = _norm_name(name)
    if not norm:
        return None
    by_norm = {_norm_name(p["name"]): p["name"] for p in participants}
    hit = difflib.get_close_matches(norm, list(by_norm), n=1,
                                    cutoff=POOL_MATCH_CUTOFF)
    return by_norm[hit[0]] if hit else None


def _lane_by_ref(ref: str, lanes: List[Dict[str, Any]],
                 participant: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Дорожка по id либо названию; participant сужает поиск до одного пула."""
    norm = _norm_name(ref)
    for lane in lanes:
        if participant is not None and lane["participant"] != participant:
            continue
        if lane["id"] == ref or _norm_name(lane["name"]) == norm:
            return lane
    return None


def _add_pool(name: str, participants: List[Dict[str, Any]], notes: List[str],
              subject: str) -> str:
    """Новый пул по названию из ответа модели.

    Молча свалить элемент в первый пул — значит собрать фальшивую схему:
    шаги разных участников окажутся одним процессом, а связи между ними
    превратятся в messageFlow. Отдельный пул честнее и виден пользователю.
    """
    name = name[:NAME_LIMIT]
    if len(participants) >= MAX_PARTICIPANTS:
        fallback = participants[0]["name"]
        notes.append(f"Пул «{name}» не создан ({subject}): достигнут максимум "
                     f"{MAX_PARTICIPANTS} — отнесён в «{fallback}»")
        return fallback
    participants.append({"name": name})
    notes.append(f"Создан пул «{name}» по имени из ответа модели ({subject})")
    return name


def _resolve_participant(declared: str, participants: List[Dict[str, Any]],
                         lanes: List[Dict[str, Any]], lane_ref: str,
                         subject: str, notes: List[str]) -> str:
    """Пул, к которому относится элемент или дорожка.

    Порядок: точное/нормализованное имя пула → пул названной дорожки (сильный
    признак: дорожка объявлена в конкретном пуле) → похожее имя → новый пул.
    """
    if declared:
        exact = _pool_exact(declared, participants)
        if exact is not None:
            if exact != declared:
                notes.append(f"{subject}: пул «{declared}» сопоставлен с «{exact}»")
            return exact
        lane = _lane_by_ref(lane_ref.strip(), lanes) if lane_ref.strip() else None
        if lane is not None:
            notes.append(f"{subject}: пул «{declared}» не найден, взят пул "
                         f"дорожки «{lane['name']}» — «{lane['participant']}»")
            return lane["participant"]
        close = _pool_close(declared, participants)
        if close is not None:
            notes.append(f"{subject}: пул «{declared}» сопоставлен с близким "
                         f"«{close}»")
            return close
        return _add_pool(declared, participants, notes, subject)

    if lane_ref.strip():
        lane = _lane_by_ref(lane_ref.strip(), lanes)
        if lane is not None:
            notes.append(f"{subject}: пул не указан, взят пул дорожки "
                         f"«{lane['name']}» — «{lane['participant']}»")
            return lane["participant"]

    pool = participants[0]["name"]
    notes.append(f"{subject}: пул не указан — отнесён к «{pool}»")
    return pool


def _resolve_lane(declared: str, participant: str, lanes: List[Dict[str, Any]],
                  used_ids: Set[str], elem_id: str,
                  notes: List[str]) -> str:
    """Дорожка элемента внутри его пула: объявленная, созданная по названию
    из элемента, а при упоре в лимит — первая дорожка пула."""
    in_pool = [lane for lane in lanes if lane["participant"] == participant]
    declared = declared.strip()
    if declared:
        lane = _lane_by_ref(declared, lanes, participant)
        if lane is not None:
            return lane["id"]
        if len(lanes) >= MAX_LANES:
            fallback = in_pool[0]["id"] if in_pool else ""
            notes.append(f"Дорожка «{declared}» для элемента {elem_id} не создана: "
                         f"максимум {MAX_LANES} дорожек — элемент "
                         + (f"помещён в «{in_pool[0]['name']}»" if in_pool
                            else "остался вне дорожки"))
            return fallback
        lane_id = _unique_id(used_ids, f"Lane_{len(lanes) + 1}")
        used_ids.add(lane_id)
        name = declared[:NAME_LIMIT]
        lanes.append({"id": lane_id, "name": name, "participant": participant})
        notes.append(f"Дорожка «{name}» не объявлена — создана в пуле "
                     f"«{participant}» по имени элемента {elem_id}")
        return lane_id

    if in_pool:
        notes.append(f"Элемент {elem_id}: дорожка не указана — помещён в "
                     f"«{in_pool[0]['name']}»")
        return in_pool[0]["id"]
    return ""


def repair_structure(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Приводит произвольный ответ модели к валидной структуре.

    Ничего не отбрасывает целиком: битые значения чинит, невозможные связи
    удаляет по одной, отсутствующие старт/финиш добавляет. Каждая правка
    попадает в notes — пользователь обязан видеть, что ИИ поправил за него.
    Возвращает (структура, список внесённых правок).
    """
    notes: List[str] = []
    used_ids: Set[str] = set()

    participants = _repair_participants(raw, notes)
    lanes = _repair_lanes(raw, participants, used_ids, notes)
    elements = _repair_elements(raw, participants, lanes, used_ids, notes)
    if not elements:
        raise GenerationError(
            "Не удалось выделить ни одного шага процесса из описания. "
            "Опишите процесс подробнее."
        )
    flows = _repair_flows(raw, elements, used_ids, notes)

    _ensure_pool_events(elements, flows, participants, used_ids, notes)
    _link_dead_starts(elements, flows, participants, used_ids, notes)
    _close_pool_paths(elements, flows, participants, used_ids, notes)
    _ensure_reachability(elements, flows, used_ids, notes)
    # Понижение шлюза — последним шагом: добавленные старт/финиш и рёбра
    # достижности меняют число исходящих потоков, и «развилка» с одной веткой
    # могла получиться уже после основной починки.
    _demote_single_branch_gateways(elements, flows, notes)

    repaired = {
        "participants": participants,
        "lanes": lanes,
        "elements": elements,
        "flows": flows,
    }
    return repaired, notes


def _repair_participants(raw: Dict[str, Any],
                         notes: List[str]) -> List[Dict[str, Any]]:
    participants: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in raw.get("participants") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if not name:
            continue
        name = _clip(name, NAME_LIMIT, notes, "Название пула")
        norm = _norm_name(name)
        if norm in seen:
            notes.append(f"Дубликат пула «{name}» пропущен")
            continue
        seen.add(norm)
        participants.append({"name": name})
        if len(participants) >= MAX_PARTICIPANTS:
            notes.append("Лишние пулы отброшены (максимум %d)" % MAX_PARTICIPANTS)
            break
    if not participants:
        participants.append({"name": "Процесс"})
        notes.append("Пул не указан — создан пул «Процесс»")
    return participants


def _repair_lanes(raw: Dict[str, Any], participants: List[Dict[str, Any]],
                  used_ids: Set[str],
                  notes: List[str]) -> List[Dict[str, Any]]:
    lanes: List[Dict[str, Any]] = []
    raw_lanes = raw.get("lanes") or []
    if not isinstance(raw_lanes, list):
        raw_lanes = []
    for item in raw_lanes:
        if not isinstance(item, dict):
            continue
        declared_id = str(item.get("id") or "").strip()
        declared_name = str(item.get("name") or "").strip()
        if not declared_id and not declared_name:
            notes.append("Дорожка без имени и идентификатора пропущена")
            continue
        if len(lanes) >= MAX_LANES:
            notes.append(f"Лишние дорожки отброшены (максимум {MAX_LANES})")
            break

        subject = f"Дорожка {declared_id or declared_name}"
        participant = _resolve_participant(
            str(item.get("participant") or "").strip(), participants, lanes, "",
            subject, notes)
        name = _clip(declared_name or declared_id, NAME_LIMIT, notes,
                     f"Название дорожки {declared_id or declared_name}")

        if _lane_by_ref(name, lanes, participant) is not None:
            notes.append(f"Дорожка «{name}» в пуле «{participant}» уже есть — "
                         "дубликат пропущен")
            continue

        if declared_id:
            lane_id = _sanitize_id(declared_id, f"Lane_{len(lanes) + 1}")
            while lane_id in used_ids:
                lane_id = f"{lane_id}_x"
            if declared_id != lane_id:
                notes.append(f"Идентификатор дорожки {declared_id} приведён "
                             f"к «{lane_id}»")
        else:
            # Кирриллица в _sanitize_id превращается в подчёркивания: лучше
            # сразу дать читаемый id, а имя оставить как есть.
            lane_id = _unique_id(used_ids, f"Lane_{len(lanes) + 1}")
        used_ids.add(lane_id)
        lanes.append({"id": lane_id, "name": name, "participant": participant})
    return lanes


def _repair_elements(raw: Dict[str, Any], participants: List[Dict[str, Any]],
                     lanes: List[Dict[str, Any]], used_ids: Set[str],
                     notes: List[str]) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
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
        declared_id = str(item.get("id") or "").strip()
        if declared_id and declared_id != elem_id:
            notes.append(f"Идентификатор элемента {declared_id} приведён к «{elem_id}»")
        used_ids.add(elem_id)

        kind_raw = str(item.get("kind") or item.get("type") or "").strip()
        kind = kind_aliases.get(kind_raw.lower())
        if kind is None:
            notes.append(f"Неизвестный тип «{kind_raw}» элемента {elem_id} → task")
            kind = "task"

        declared_lane = str(item.get("lane") or "").strip()
        participant = _resolve_participant(
            str(item.get("participant") or "").strip(), participants, lanes,
            declared_lane, f"Элемент {elem_id}", notes)

        name = str(item.get("name") or "").strip()
        if name:
            name = _clip(name, NAME_LIMIT, notes, f"Название элемента {elem_id}")
        else:
            name = kind
            notes.append(f"Элемент {elem_id} без названия — подписан типом «{kind}»")

        elements.append({
            "id": elem_id,
            "kind": kind,
            "name": name,
            "participant": participant,
            "lane": _resolve_lane(declared_lane, participant, lanes, used_ids,
                                  elem_id, notes),
        })
    return elements


def _repair_flows(raw: Dict[str, Any], elements: List[Dict[str, Any]],
                  used_ids: Set[str], notes: List[str]) -> List[Dict[str, Any]]:
    element_ids = {e["id"] for e in elements}
    element_by_id = {e["id"]: e for e in elements}

    flows: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[str, str, str]] = set()
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
        if source == target:
            notes.append(f"Поток {source} → {target} удалён: шаг не может вести "
                         "сам в себя — повтор моделируется шлюзом с веткой назад")
            continue
        if source not in element_ids or target not in element_ids:
            notes.append(f"Поток {source or '?'} → {target or '?'} удалён: "
                         "одного из элементов нет в плане")
            continue

        kind_raw = str(item.get("kind") or item.get("type") or "sequence").lower()
        kind = "message" if "message" in kind_raw else "sequence"

        cross_pool = (element_by_id[source]["participant"]
                      != element_by_id[target]["participant"])
        if kind == "sequence" and cross_pool:
            kind = "message"
            notes.append(f"Поток {source} → {target} между пулами преобразован "
                         "в потоковое сообщение: если это роли одной "
                         "организации, они должны быть дорожками одного пула")
        elif kind == "message" and not cross_pool:
            notes.append(f"Поток-сообщение {source} → {target} внутри пула удалён")
            continue

        # Для потока-сообщения текст условия становится именем сообщения.
        condition = str(item.get("condition") or item.get("name") or "").strip()
        condition = _clip(condition, 200, notes, f"Условие потока {source} → {target}")
        if condition and kind == "sequence" \
                and element_by_id[source]["kind"] not in GATEWAY_KINDS:
            notes.append(f"Условие на потоке {source} → {target} сохранено, "
                         "но источник не шлюз")
        if condition and kind == "message":
            notes.append(f"Условие потока {source} → {target} стало именем "
                         "потокового сообщения")

        pair = (kind, source, target)
        if pair in seen_pairs:
            notes.append(f"Дубликат потока {source} → {target} пропущен")
            continue
        seen_pairs.add(pair)

        declared_id = str(item.get("id") or "").strip()
        flow_id = _sanitize_id(declared_id, f"Flow_{len(flows) + 1}")
        while flow_id in used_ids:
            flow_id = f"{flow_id}_x"
        if declared_id and declared_id != flow_id:
            notes.append(f"Идентификатор потока {declared_id} приведён к «{flow_id}»")
        used_ids.add(flow_id)

        flows.append({
            "id": flow_id,
            "kind": kind,
            "source": source,
            "target": target,
            "condition": condition,
        })
    return flows


def _demote_single_branch_gateways(elements: List[Dict[str, Any]],
                                   flows: List[Dict[str, Any]],
                                   notes: List[str]) -> None:
    """Исключающий шлюз с единственной веткой — не развилка, а обычный шаг.

    Вторую ветку дорисовывать нельзя: выдуманное условие хуже, чем честная
    задача. Условие с исходящего потока снимается — на потоке от задачи оно
    перестаёт быть решением ветвления.
    """
    counts: Dict[str, int] = {}
    for f in flows:
        if f["kind"] == "sequence":
            counts[f["source"]] = counts.get(f["source"], 0) + 1

    for gateway in elements:
        if gateway["kind"] != "exclusiveGateway" or counts.get(gateway["id"]) != 1:
            continue
        gateway["kind"] = "task"
        name = gateway["name"]
        if name == "exclusiveGateway":
            gateway["name"] = name = "task"
        notes.append(f"Шлюз {gateway['id']} («{name}») с единственной "
                     "исходящей веткой понижен до задачи")
        for f in flows:
            if f["kind"] == "sequence" and f["source"] == gateway["id"] \
                    and f["condition"]:
                notes.append(f"Условие «{f['condition']}» снято с потока "
                             f"{f['id']}: источник больше не шлюз")
                f["condition"] = ""


def _ensure_pool_events(elements: List[Dict[str, Any]],
                        flows: List[Dict[str, Any]],
                        participants: List[Dict[str, Any]],
                        used_ids: Set[str], notes: List[str]) -> None:
    for idx, p in enumerate(participants, 1):
        pool = [e for e in elements if e["participant"] == p["name"]]
        seq = [f for f in flows if f["kind"] == "sequence"]
        has_start = any(e["kind"] == "startEvent" for e in pool)
        has_end = any(e["kind"] == "endEvent" for e in pool)

        if not has_start:
            entry = _pick_entry_node(pool, {f["target"] for f in seq})
            start_id = _unique_id(used_ids, f"StartEvent_{idx}")
            used_ids.add(start_id)
            elements.insert(0, {
                "id": start_id,
                "kind": "startEvent",
                "name": "Старт",
                "participant": p["name"],
                "lane": entry["lane"] if entry else "",
            })
            if entry:
                flows.append(_new_flow(used_ids, start_id, entry["id"]))
            notes.append(f"В пул «{p['name']}» добавлено стартовое событие")

        if not has_end:
            exit_node = _pick_exit_node(pool, seq)
            end_id = _unique_id(used_ids, f"EndEvent_{idx}")
            used_ids.add(end_id)
            elements.append({
                "id": end_id,
                "kind": "endEvent",
                "name": "Завершение",
                "participant": p["name"],
                "lane": exit_node["lane"] if exit_node else "",
            })
            if exit_node:
                flows.append(_new_flow(used_ids, exit_node["id"], end_id))
            notes.append(f"В пул «{p['name']}» добавлено завершающее событие")


def _link_dead_starts(elements: List[Dict[str, Any]],
                      flows: List[Dict[str, Any]],
                      participants: List[Dict[str, Any]],
                      used_ids: Set[str], notes: List[str]) -> None:
    """Старт, из которого некуда идти, процесс не запускает.

    Когда модель ведёт линию между «ролями-пулами» messageFlow-ами, первый шаг
    пула принимает только сообщение, а собственное стартовое событие пула
    остаётся одинокой точкой. Достижимость такого узла не касается (она бережёт
    смысл «жду сообщение»), но процесс без первой дуги не выполняется.
    """
    adjacency: Dict[str, List[str]] = {}
    targets: Set[str] = set()
    for f in flows:
        if f["kind"] == "sequence":
            adjacency.setdefault(f["source"], []).append(f["target"])
            targets.add(f["target"])

    for p in participants:
        pool = [e for e in elements if e["participant"] == p["name"]]
        for start in [e for e in pool if e["kind"] == "startEvent"]:
            if adjacency.get(start["id"]):
                continue
            entry = _pick_entry_node(pool, targets)
            if entry is None:
                # Пул из одних событий: вести старт некуда, а выдумывать шаг
                # нельзя. Безвыходное положение обязано быть видно в notes.
                notes.append(f"Старт {start['id']} в пуле «{p['name']}» остался без "
                             "продолжения: в пуле нет ни одного шага")
                continue
            if _reachable(adjacency, entry["id"], start["id"]):
                notes.append(f"Старт {start['id']} не подключён к «{entry['name']}»: "
                             "поток замкнул бы цикл")
                continue
            flows.append(_new_flow(used_ids, start["id"], entry["id"]))
            adjacency.setdefault(start["id"], []).append(entry["id"])
            targets.add(entry["id"])
            notes.append(f"Старт {start['id']} был без исходящего потока — "
                         f"соединён с «{entry['name']}» ({entry['id']})")


def _close_pool_paths(elements: List[Dict[str, Any]],
                      flows: List[Dict[str, Any]],
                      participants: List[Dict[str, Any]],
                      used_ids: Set[str], notes: List[str]) -> None:
    """Шаг без исходящего sequence-потока завершается конечным событием.

    Частый случай — модель раздала роли одной организации по пулам: линия
    «заявка → согласование → выдача» превращается в цепочку messageFlow, и
    внутри своего пула каждый шаг остаётся без продолжения. XML от этого
    формально не ломается, но маршрут обрывается тупиком, который скоринг
    правомерно считает. Поток-сообщение продолжения не заменяет: оно уходит
    в другой пул, а процесс в своём остаётся незакрытым.
    """
    adjacency: Dict[str, List[str]] = {}
    for f in flows:
        if f["kind"] == "sequence":
            adjacency.setdefault(f["source"], []).append(f["target"])
    sends_message = {f["source"] for f in flows if f["kind"] == "message"}
    budget = MAX_REACH_FLOWS
    for p in participants:
        pool = [e for e in elements if e["participant"] == p["name"]]
        ends = [e for e in pool if e["kind"] == "endEvent"]
        if not ends:
            continue
        end = ends[0]
        for e in pool:
            if e["kind"] in ("startEvent", "endEvent") or adjacency.get(e["id"]):
                continue
            if budget <= 0:
                notes.append(f"Шаг {e['id']} остался без исходящего потока: "
                             f"лимит починки связности ({MAX_REACH_FLOWS})")
                continue
            if _reachable(adjacency, end["id"], e["id"]):
                notes.append(f"Шаг {e['id']} не присоединён к «{end['name']}»: "
                             "поток замкнул бы цикл")
                continue
            flows.append(_new_flow(used_ids, e["id"], end["id"]))
            adjacency.setdefault(e["id"], []).append(end["id"])
            budget -= 1
            note = (f"Шаг {e['id']} в пуле «{p['name']}» был без исходящего "
                    f"потока — присоединён к «{end['name']}» ({end['id']})")
            if e["id"] in sends_message:
                note += (". Он отправляет сообщение в другой пул и на этом "
                         "заканчивается: если получатель — сотрудник той же "
                         "организации, это дорожка одного пула, а не второй пул")
            notes.append(note)


def _ensure_reachability(elements: List[Dict[str, Any]],
                         flows: List[Dict[str, Any]],
                         used_ids: Set[str], notes: List[str]) -> None:
    """Каждый узел без входящего потока подключается к старту своего пула.

    Промпт обещает достижимость, но модель её не гарантирует. Ребро не
    добавляется, только если оно замкнуло бы цикл. Узел, дожидаться сообщения
    из другого пула, тоже получает свой локальный вход: messageFlow показывает
    внешний триггер, но не запускает процесс внутри пула, и без локального
    входа узел — тупик.
    """
    seq = [f for f in flows if f["kind"] == "sequence"]
    has_incoming = {f["target"] for f in seq}
    waits_for_message = {f["target"] for f in flows if f["kind"] == "message"}
    adjacency: Dict[str, List[str]] = {}
    for f in seq:
        adjacency.setdefault(f["source"], []).append(f["target"])
    start_by_pool: Dict[str, str] = {}
    for e in elements:
        if e["kind"] == "startEvent":
            start_by_pool.setdefault(e["participant"], e["id"])

    added = 0
    for e in elements:
        if e["kind"] == "startEvent" or e["id"] in has_incoming:
            continue
        start = start_by_pool.get(e["participant"])
        if start is None or start == e["id"]:
            continue
        if added >= MAX_REACH_FLOWS:
            notes.append(f"Элемент {e['id']} остался без входящего потока: "
                         f"лимит исправлений достижимости ({MAX_REACH_FLOWS})")
            continue
        if _reachable(adjacency, e["id"], start):
            notes.append(f"Элемент {e['id']} не подключён к старту: поток "
                         "замкнул бы цикл")
            continue
        flow = _new_flow(used_ids, start, e["id"])
        flows.append(flow)
        adjacency.setdefault(start, []).append(e["id"])
        has_incoming.add(e["id"])
        added += 1
        note = (f"Элемент {e['id']} был без входящего потока — подключён "
                f"от стартового события {start}")
        if e["id"] in waits_for_message:
            # Шаг принимает сообщение из другого пула, но процесс в своём пуле
            # обязан с чего-то начинаться: без локального входа это тупик.
            # MessageFlow остаётся — он показывает внешний триггер.
            note += ("; шаг дополнительно ожидает сообщение из другого пула, "
                     "но процесс в своём пуле обязан начинаться с его старта")
        notes.append(note)


def _reachable(adjacency: Dict[str, List[str]], source: str,
               target: str) -> bool:
    seen: Set[str] = set()
    stack = [source]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def _unique_id(used: Set[str], base: str) -> str:
    candidate = _sanitize_id(base, "Elem")
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{_sanitize_id(base, 'Elem')}_{n}"
    return candidate


def _new_flow(used_ids: Set[str], source: str, target: str) -> Dict[str, Any]:
    flow_id = _unique_id(used_ids, f"Flow_{source}_{target}")
    used_ids.add(flow_id)
    return {"id": flow_id, "kind": "sequence",
            "source": source, "target": target, "condition": ""}


def _pick_entry_node(pool: List[Dict[str, Any]],
                     targets: Set[str]) -> Optional[Dict[str, Any]]:
    """Первый узел пула без входящих sequence-потоков (не старт)."""
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

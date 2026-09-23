# Домен редактирования BPMN-схем: инвентарь элементов для контекста LLM,
# детерминированный аплайер операций и семантическая починка.
#
# Принципы: каждая операция валидируется отдельно; невалидная — пропускается
# с причиной и подсказкой в отчёте, пакет целиком не падает. Никакой магии
# и эвристик: что не удаётся применить однозначно — сообщаем наверх.
import difflib
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

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


def is_bpmn_tag(tag: Any) -> bool:
    """Элемент принадлежит BPMN: неймспейс OMG (любой редакции) либо его нет.

    Сортировать узлы по одному локальному имени опасно: расширение вида
    ``<acme:lane>`` становилось дорожкой схемы, ``<acme:startEvent>`` —
    стартовым событием, и правки уезжали в элемент, о котором bpmn-js не знает.
    """
    if not isinstance(tag, str) or "{" not in tag:
        return True
    return tag[1:].split("}", 1)[0].startswith("http://www.omg.org/spec/BPMN/")


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

# `add_task` создаёт плоский шаг: пустой subProcess или callActivity потом
# нечем наполнить (в `after` нельзя сослаться на несуществующего ребёнка), и
# в TASK_TAGS эти типы остаются только потому, что к подпроцессу цепляют
# граничное событие.
ADD_TASK_TAGS = TASK_TAGS - {"subProcess", "callActivity"}

# Концы sequenceFlow: стартовое событие не принимают, из конечного не выходят,
# и граничное событие не имеет входящего потока — его запускает хозяин через
# attachedToRef. messageFlow под правило не попадает: сообщение в чужой старт
# — единственный легальный способ запустить второй пул.
SEQUENCE_FORBIDDEN_TARGETS = {"startEvent", "boundaryEvent"}
SEQUENCE_FORBIDDEN_SOURCES = {"endEvent"}

# Кто участвует в межпуловом потоке-сообщении: сообщение отдаёт шаг или бросающее
# (в т. ч. конечное) событие, принимает — шаг, ловящее или стартовое событие.
# Шлюз и граничное событие в сообщении не участвуют: у шлюза нет своего пула за
# пределами маршрута, а граничное событие запускает его хозяин через attachedToRef.
MESSAGE_SOURCE_TAGS = TASK_TAGS | {"intermediateThrowEvent", "endEvent"}
MESSAGE_TARGET_TAGS = TASK_TAGS | {"intermediateCatchEvent", "startEvent"}


def _message_leg_ok(source_tag: str, target_tag: str) -> bool:
    """Межпуловая дуга — это ещё не сообщение: пары узлов, где сообщения не
    бывает, остаются отказом."""
    return source_tag in MESSAGE_SOURCE_TAGS and target_tag in MESSAGE_TARGET_TAGS

EVENT_TYPE_TO_TAG = {
    "start": "startEvent",
    "end": "endEvent",
    "intermediatecatch": "intermediateCatchEvent",
    "intermediatethrow": "intermediateThrowEvent",
}

# Определения событий, которые аплайер умеет проставлять: имя → (тег, тег
# хронометража). Без определения «промежуточное событие» остаётся декларацией:
# bpmn-js рисует пустой кружок, и скоринг не засчитывает тип события.
EVENT_DEFINITIONS: Dict[str, Tuple[str, str]] = {
    "timer": ("timerEventDefinition", "timeDuration"),
    "message": ("messageEventDefinition", ""),
    "error": ("errorEventDefinition", ""),
    "signal": ("signalEventDefinition", ""),
}

# Хронометраж таймера по умолчанию: 15 минут — типичный SLA ожидания шага.
# Подставляется, только когда поле не указано вовсе: выдуманное «PT5M» вместо
# запрошенного пользователем часа хуже честного отказа.
DEFAULT_TIMER_DURATION = "PT15M"

# Поля операции → тег, в котором BPMN хранит этот хронометраж.
TIMER_TIME_TAGS: Dict[str, str] = {
    "duration": "timeDuration",
    "cycle": "timeCycle",
}

# ISO-8601: `PT15M`, `P1DT2H`, `R3/PT10M` (повтор + интервал). Голые `P`, `PT`
# и «2H» без разделителя T — не интервалы, а опечатка модели.
_ISO_REPEAT = re.compile(r"R\d*")
_ISO_DATE_PART = re.compile(r"(\d+Y)?(\d+M)?(\d+W)?(\d+D)?")
_ISO_TIME_PART = re.compile(r"(\d+H)?(\d+M)?(\d+(?:[.,]\d+)?S)?")


def _is_iso_interval(value: str) -> bool:
    """Проверка хронометража таймера: только то, что читает bpmn-js."""
    text = (value or "").strip().upper()
    if not text:
        return False
    if text.startswith("R"):
        head, slash, tail = text.partition("/")
        if not (slash and _ISO_REPEAT.fullmatch(head) and tail):
            return False
        text = tail
    if not text.startswith("P"):
        return False
    date_part, has_time, time_part = text[1:].partition("T")
    if not _ISO_DATE_PART.fullmatch(date_part):
        return False
    if has_time and not (time_part and _ISO_TIME_PART.fullmatch(time_part)):
        return False
    # «P» и «PT» синтаксически разборчивы, но интервал не задают.
    return bool(date_part or (has_time and time_part))


def _timer_timing(op: Dict[str, Any],
                  definition: Optional[str]) -> Optional[Tuple[str, str]]:
    """Хронометраж таймера из операции: (тег, значение) либо None — по умолчанию.

    Битое значение — отказ, а не молчаливая подстановка: схема с «PT-15» вместо
    таймера на 15 минут не заведётся у bpmn-js, и пользователь узнает об этом
    только в редакторе.
    """
    duration = str(op.get("duration") or "").strip()
    cycle = str(op.get("cycle") or "").strip()
    if not duration and not cycle:
        return None
    if duration and cycle:
        raise _Skip("таймеру задают либо duration, либо cycle",
                    "оставьте одно из полей")
    if definition != "timer":
        raise _Skip("хронометраж задают только таймеру",
                    "укажите event_definition=timer (для граничного события — "
                    "event_type=timer) либо уберите duration/cycle")
    field, value = ("duration", duration) if duration else ("cycle", cycle)
    if not _is_iso_interval(value):
        raise _Skip(f"некорректный интервал таймера '{value}'",
                    "формат ISO-8601: PT15M, PT2H или R3/PT10M")
    return TIMER_TIME_TAGS[field], value.upper()


# Словарь операций в одном месте: дословные формулировки полей, с которыми
# аплайер их понимает. Планирующий промпт (core/llm_improve.py) обязан
# перечислять операции так же, а тест сверяет состав OP_SPEC и _HANDLERS:
# расхождение означает «модель предлагает то, чего аплайер не умеет».
OP_SPEC: Dict[str, str] = {
    # `to` — исход нового узла. Без него модель обязана помнить второй
    # `connect`, и в живых прогонах забывала: шаг откатывался тупиком, а вместе
    # с ним и правка. Одна операция описывает обе стороны маршрута.
    "add_task": '{"op":"add_task","id":"new_...","name":"...","task_type":"'
                + "|".join(sorted(ADD_TASK_TAGS))
                + '","participant":"пул","after":"id элемента",'
                '"to":"id следующего шага (опц.)",'
                '"lane":"id или имя дорожки (опц.)"}',
    "add_gateway": '{"op":"add_gateway","id":"new_...","name":"...",'
                   '"gateway_type":"exclusive|parallel|inclusive","participant":"пул",'
                   '"after":"id","to":"id (опц., один выход шлюза)",'
                   '"lane":"id или имя дорожки (опц.)"}',
    "add_event": '{"op":"add_event","id":"new_...","name":"...",'
                 '"event_type":"start|end|intermediateCatch|intermediateThrow"'
                 ' либо определение ловушки: timer|message|error|signal",'
                 '"participant":"пул","after":"id (опц.)",'
                 '"to":"id следующего шага (опц., не для end)",'
                 '"lane":"id или имя дорожки (опц.)",'
                 '"event_definition":"timer|message|error|signal (опц.)",'
                 '"duration":"PT15M (опц., таймеру)","cycle":"R3/PT10M (опц., таймеру)"}',
    "add_participant": '{"op":"add_participant","id":"new_...","name":"..."}',
    "add_documentation": '{"op":"add_documentation","id":"id элемента","text":"..."}',
    "add_lane": '{"op":"add_lane","id":"new_...","name":"...","participant":"пул"}',
    "move_to_lane": '{"op":"move_to_lane","id":"id элемента","lane":"id|имя дорожки"}',
    "add_boundary_event": '{"op":"add_boundary_event","id":"new_...","attached_to":"id задачи",'
                          '"event_type":"timer|error","name":"...",'
                          '"to":"id шага обработки (опц.)",'
                          '"duration":"PT15M (опц.)","cycle":"R3/PT10M (опц.)"}',
    "rename": '{"op":"rename","id":"...","name":"..."}',
    "delete": '{"op":"delete","id":"..."}',
    "connect": '{"op":"connect","source":"...","target":"...",'
               '"flow_type":"sequence|message","condition":"опц.",'
               '"default":true (опц. — сделать эту ветку шлюза выходом по умолчанию)}',
    "set_default": '{"op":"set_default","gateway":"id шлюза","flow":"id его ветки"}',
    "disconnect": '{"op":"disconnect","flow":"id потока"}',
    "move_to_participant": '{"op":"move_to_participant","id":"...","participant":"пул"}',
    "merge_participants": '{"op":"merge_participants","source":"пул-источник",'
                          '"target":"пул-приёмник","as_lane":true|"имя дорожки"}',
    "remove_participant": '{"op":"remove_participant","participant":"пустой пул"}',
}

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

# Потолок инвентаря. Принятый XML ограничен AI_MAX_XML_CHARS (1 МБ), и схема
# на сотни элементов целиком в контекст модели не влезает: провайдер отвечает
# 400, а это не транспортный сбой, а вечный отказ. Поэтому инвентарь режем
# здесь и честно пишем в поле `limits`, сколько не показаны, — молча
# обрывать список нельзя: планировщик начал бы ссылаться на id, которых нет.
INVENTORY_MAX_ELEMENTS = 250
INVENTORY_MAX_FLOWS = 500
INVENTORY_MAX_LANES = 60
# Длина документации в инвентаре: модели нужен факт «описание уже есть», а не
# его полный текст — иначе одно подробное описание съедает строку целиком.
INVENTORY_DOC_CHARS = 80

# Теги, которые по схеме BPMN обязаны идти до incoming/outgoing: документация
# процесса или элемента и extensionElements. Позиция вставки ссылается на них,
# чтобы пересборка ссылок не поставила потоки раньше описания.
_REFS_PRECEDING_TAGS = {"documentation", "extensionElements", "audios", "bricks"}


def _serialize(root: ET.Element) -> str:
    return XML_DECLARATION + ET.tostring(root, encoding="unicode")


def _discard(items: List[ET.Element], elem: ET.Element) -> None:
    try:
        items.remove(elem)
    except ValueError:  # элемент пришёл из другой ветки или уже удалён
        pass


class _Index:
    """Снимок структуры схемы для быстрого доступа при применении операций.

    Индексы обновляются инкрементально через adopt/insert/detach: полная
    пересборка после каждой операции и поиск родителя перебором всего дерева
    давали O(n^2) на пакете из 25 правок, а пользовательский XML ограничен
    гигабайтом символов — такие проходы блокировали событийный цикл.
    """

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
        self.flows_by_id: Dict[str, ET.Element] = {}
        self.lanes: List[ET.Element] = []
        self.parents: Dict[ET.Element, ET.Element] = {}
        # Id элементов, затронутых merge_participants: по ним пакет слияния
        # откатывается, если починка нашла поломку именно в перенесённом.
        self.merge_touched: Set[str] = set()
        self._build()

    def _build(self) -> None:
        for node in self.root.iter():
            if not isinstance(node.tag, str):
                continue
            for child in node:
                self.parents[child] = node
            self._classify(node, None)
        # Привязка узла к процессу считается отдельным проходом: узел внутри
        # subProcess принадлежит внешнему процессу (так же читает bpmn-js).
        for process in self.processes:
            for child in process.iter():
                if not isinstance(child.tag, str):
                    continue
                child_id = child.get("id")
                if child_id and is_bpmn_tag(child.tag) and _local(child.tag) in FLOW_NODE_TAGS:
                    self.process_of[child_id] = process

    # --- инкрементальное обслуживание индексов ---

    def _classify(self, node: ET.Element, process: Optional[ET.Element]) -> None:
        """Зарегистрировать узел в индексах. process — владеющий процесс, если известен."""
        if not isinstance(node.tag, str):
            return
        if not is_bpmn_tag(node.tag):
            return
        tag = _local(node.tag)
        if tag == "collaboration":
            self.collaboration = node
        elif tag == "participant":
            self.participants.append(node)
            process_ref = node.get("processRef")
            if process_ref:
                self.participant_by_process_id[process_ref] = node
        elif tag == "process":
            self.processes.append(node)
        elif tag == "lane":
            self.lanes.append(node)
        elif tag == "sequenceFlow":
            self.sequence_flows.append(node)
            self._remember_flow(node)
        elif tag == "messageFlow":
            self.message_flows.append(node)
            self._remember_flow(node)
        elif tag in FLOW_NODE_TAGS:
            node_id = node.get("id")
            if node_id:
                self.elements[node_id] = node
                if process is not None:
                    self.process_of[node_id] = process

    def _remember_flow(self, flow: ET.Element) -> None:
        flow_id = flow.get("id")
        if flow_id and flow_id not in self.flows_by_id:
            self.flows_by_id[flow_id] = flow

    def _unclassify(self, node: ET.Element) -> None:
        if not isinstance(node.tag, str):
            return
        tag = _local(node.tag)
        if tag in ("sequenceFlow", "messageFlow"):
            _discard(self.sequence_flows if tag == "sequenceFlow" else self.message_flows, node)
            flow_id = node.get("id")
            if flow_id and self.flows_by_id.get(flow_id) is node:
                self.flows_by_id.pop(flow_id, None)
        elif tag == "lane":
            _discard(self.lanes, node)
        elif tag == "process":
            _discard(self.processes, node)
        elif tag == "participant":
            _discard(self.participants, node)
            process_ref = node.get("processRef")
            if process_ref and self.participant_by_process_id.get(process_ref) is node:
                self.participant_by_process_id.pop(process_ref, None)
        elif tag in FLOW_NODE_TAGS:
            node_id = node.get("id")
            if node_id and self.elements.get(node_id) is node:
                self.elements.pop(node_id, None)
                self.process_of.pop(node_id, None)

    def _track(self, elem: ET.Element, parent: Optional[ET.Element]) -> None:
        """Занести в индексы поддерево, только что вставленное в parent."""
        if parent is not None:
            self.parents[elem] = parent
        process = self.enclosing_process(elem)
        self._classify(elem, process)
        stack: List[Tuple[ET.Element, ET.Element]] = [(child, elem) for child in elem]
        while stack:
            node, node_parent = stack.pop()
            if not isinstance(node.tag, str):
                continue
            self.parents[node] = node_parent
            self._classify(node, process)
            stack.extend((child, node) for child in node)

    def _untrack(self, elem: ET.Element) -> None:
        self.parents.pop(elem, None)
        self._unclassify(elem)
        for node in elem.iter():
            if node is elem:
                continue
            self.parents.pop(node, None)
            self._unclassify(node)

    def adopt(self, parent: ET.Element, elem: ET.Element) -> ET.Element:
        """Вставить elem последним ребёнком parent и обновить индексы."""
        parent.append(elem)
        self._track(elem, parent)
        return elem

    def insert(self, parent: ET.Element, position: int, elem: ET.Element) -> ET.Element:
        """Вставить elem в parent по позиции (порядок тегов BPMN!) и обновить индексы."""
        parent.insert(position, elem)
        self._track(elem, parent)
        return elem

    def detach(self, elem: ET.Element) -> Optional[ET.Element]:
        """Вынуть elem из дерева и из индексов; возвращает бывшего родителя."""
        parent = self.parents.get(elem)
        self._untrack(elem)
        if parent is not None:
            parent.remove(elem)
        return parent

    # --- справочные доступы ---

    def find_flow(self, flow_id: str) -> Optional[ET.Element]:
        return self.flows_by_id.get(flow_id)

    def find_lane(self, key: str) -> Optional[ET.Element]:
        """Дорожка по id или имени; при коллизии имён выигрывает первая."""
        for lane in self.lanes:
            if key in (lane.get("id"), lane.get("name")):
                return lane
        return None

    def enclosing_process(self, elem: ET.Element) -> Optional[ET.Element]:
        """Ближайший предок-process (для самого процесса — он сам)."""
        node: Optional[ET.Element] = elem
        while node is not None:
            if isinstance(node.tag, str) and _local(node.tag) == "process":
                return node
            node = self.parents.get(node)
        return None

    def has_subprocess_ancestor(self, elem: ET.Element) -> bool:
        """Лежит ли элемент внутри subProcess: такие узлы не соединяют с внешним
        процессом и не переносят между пулами."""
        node = self.parents.get(elem)
        while node is not None:
            if isinstance(node.tag, str):
                tag = _local(node.tag)
                if tag == "subProcess":
                    return True
                if tag == "process":
                    return False
            node = self.parents.get(node)
        return False

    def pool_processes(self) -> List[Tuple[str, ET.Element]]:
        """Пары «имя пула → процесс»: участники collaboration и сами процессы.

        Одно и то же имя приходит и от `<participant>`, и от `<process>` — две
        записи об одном пуле, поэтому кандидаты дедуплицируются по процессу.
        """
        out: List[Tuple[str, ET.Element]] = []
        for participant in self.participants:
            ref = participant.get("processRef")
            process = next((p for p in self.processes
                            if p.get("id") == ref), None)
            name = str(participant.get("name") or "").strip()
            if process is not None and name:
                out.append((name, process))
        for process in self.processes:
            name = str(process.get("name") or "").strip()
            if name and all(p is not process for _, p in out):
                out.append((name, process))
        return out

    def fuzzy_matches(self, key: str) -> List[ET.Element]:
        """Пулы, чьё имя означает то же участниковое лицо, что и `key`.

        Модель берёт название из описания («Склад»), а в инвентаре пул
        называется иначе («ВкусВилл», «Система WMS») — правка из-за одной
        формулировки отвергалась, и пакет уходил в повтор впустую. Разрешаем
        только однозначно: слово в слово, один набор слов внутри другого или
        почти совпадение. Два кандидата — не угадываем, а просим уточнить:
        выдуманный пул стоил бы схеме больше, чем отказ.
        """
        wanted = _pool_words(key)
        if not wanted or min(len(w) for w in wanted) < 3:
            return []
        hits: List[ET.Element] = []
        for name, process in self.pool_processes():
            words = _pool_words(name)
            if not words:
                continue
            close = (wanted == words or wanted <= words or words <= wanted
                     or difflib.SequenceMatcher(
                         None, " ".join(sorted(wanted)),
                         " ".join(sorted(words))).ratio() >= FUZZY_POOL_RATIO)
            if close and all(h is not process for h in hits):
                hits.append(process)
        return hits

    def pool_candidates(self, key: Optional[str]) -> List[str]:
        """Имена-кандидаты для подсказки: отказ обязан называть, что подходит."""
        names = {self.participant_name(process) or process.get("id") or ""
                 for process in self.fuzzy_matches(str(key or ""))}
        return sorted(name for name in names if name)

    def resolve_process(self, key: Optional[str]) -> Optional[ET.Element]:
        """Участник по id, имени или id процесса → элемент процесса.

        Имя процесса участвует наравне с именем участника: инвентарь отдаёт
        `participant_name`, а для схемы без collaboration это именно имя
        процесса — иначе аплайер отвергал бы то, что сам же показал модели.
        Точного совпадения нет — см. `fuzzy_matches`.
        """
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
            if key in (process.get("id"), process.get("name")):
                return process
        matches = self.fuzzy_matches(str(key))
        return matches[0] if len(matches) == 1 else None

    def is_taken(self, candidate: str) -> bool:
        """Занят ли id в схеме: элементы, потоки и дорожки делят одно
        пространство имён (xml:id обязан быть уникальным во всём документе)."""
        return (candidate in self.elements or candidate in self.flows_by_id
                or self.find_lane(candidate) is not None)

    def participant_name(self, process: ET.Element) -> str:
        participant = self.participant_by_process_id.get(process.get("id", ""))
        if participant is not None:
            return participant.get("name") or participant.get("id", "")
        return process.get("name") or process.get("id", "")

    def resolve_participant(
            self, key: Optional[str]) -> Tuple[Optional[ET.Element], Optional[ET.Element], str]:
        """(участник, процесс, прочитанное имя) по id/имени пула или процесса.

        Возвращает и процесс, даже если участник не найден: вызывающий код
        различает «нет такого пула» и «процесс, не заявленный участником» —
        слияние и удаление пула для них отказывает по-разному.
        """
        name = (key or "").strip() if isinstance(key, str) else ""
        if not name:
            return None, None, ""
        for participant in self.participants:
            if name in (participant.get("id"), participant.get("name")):
                process = next(
                    (p for p in self.processes
                     if p.get("id") == participant.get("processRef")), None)
                return participant, process, name
        process = next((p for p in self.processes
                        if name in (p.get("id"), p.get("name"))), None)
        if process is not None:
            return (self.participant_by_process_id.get(process.get("id") or ""),
                    process, name)
        return None, None, name


    def lane_home(self, key: Any) -> Tuple[Optional[ET.Element],
                                           Optional[ET.Element]]:
        """Роль по имени дорожки: (процесс, дорожка), если такая дорожка одна.

        Модель называет участником то, что на схеме уже дорожка организации
        («руководитель смены»), а пула с таким именем нет. Отказывать такому
        «пулу» — значит терять весь шаг: он не создавался, и ведущий к нему
        поток откатывался вместе с веткой. Как и с пулами, спорить нельзя:
        две одноимённые дорожки — не угадываем.
        """
        wanted = str(key or "").strip().casefold()
        if not wanted:
            return None, None
        hits = [(self.enclosing_process(lane), lane) for lane in self.lanes
                if str(lane.get("name") or "").strip().casefold() == wanted]
        hits = [hit for hit in hits if hit[0] is not None]
        return hits[0] if len(hits) == 1 else (None, None)


def _subprocess_of(index: _Index, elem: ET.Element) -> str:
    """Ид ближайшего subProcess-контейнера (пусто — узел верхнего уровня).

    Без этого поля планировщик видит элементы подпроцесса наравне с внешними и
    предлагает `connect` через границу вложенности — аплайер такое отвергает, и
    правка уезжает в повтор вместо полезной работы.
    """
    node = index.parents.get(elem)
    while node is not None:
        if isinstance(node.tag, str) and is_bpmn_tag(node.tag):
            tag = _local(node.tag)
            if tag == "subProcess":
                return node.get("id") or ""
            if tag == "process":
                return ""
        node = index.parents.get(node)
    return ""


def _definition_of(elem: ET.Element) -> Tuple[str, str]:
    """(определение события, хронометраж) — чтобы модель не лепила второй
    таймер на шаг, где ожидание уже нарисовано."""
    for child in elem:
        if not isinstance(child.tag, str) or not is_bpmn_tag(child.tag):
            continue
        tag = _local(child.tag)
        if tag.endswith("EventDefinition"):
            timing = next((_text_of(t) for t in child
                           if isinstance(t.tag, str)
                           and _local(t.tag) in ("timeDuration", "timeCycle")), "")
            return tag[:-len("EventDefinition")].lower(), timing
    return "", ""


def _text_of(elem: ET.Element) -> str:
    return (elem.text or "").strip()


def build_inventory(xml_text: str) -> Dict[str, Any]:
    """Компактный инвентарь схемы — контекст для планирующих промптов.

    Показывает не только id и имена: без текущей дорожки, хозяина граничного
    события, определения таймера и факта документации планировщик работает
    вслепую — предлагает перенос в ту же дорожку, второй таймер на тот же шаг
    и описание там, где оно уже есть.
    """
    root = parse_xml(xml_text)
    index = _Index(root)
    participants = []
    for process in index.processes:
        participant = index.participant_by_process_id.get(process.get("id", ""))
        participants.append({
            "id": participant.get("id") if participant is not None else process.get("id"),
            "name": index.participant_name(process),
        })

    # Узел принадлежит ровно одной дорожке: разворачиваем flowNodeRef в
    # обратную ссылку, иначе «в какой дорожке шаг» пришлось бы искать перебором
    # на каждый элемент.
    lane_of: Dict[str, str] = {}
    for lane in index.lanes:
        for ref in lane.findall(_q("flowNodeRef")):
            ref_id = _text_of(ref)
            if ref_id:
                lane_of.setdefault(ref_id, lane.get("id") or "")

    elements = []
    for elem_id, elem in index.elements.items():
        process = index.process_of.get(elem_id)
        entry = {
            "id": elem_id,
            "type": _local(elem.tag),
            "name": elem.get("name", ""),
            "participant": index.participant_name(process) if process is not None else "",
        }
        if elem_id in lane_of:
            entry["lane"] = lane_of[elem_id]
        host = elem.get("attachedToRef")
        if host:
            entry["attached_to"] = host
        definition, timing = _definition_of(elem)
        if definition:
            entry["definition"] = definition
        if timing:
            entry["timer"] = timing
        documentation = next((doc for doc in elem
                              if isinstance(doc.tag, str)
                              and is_bpmn_tag(doc.tag)
                              and _local(doc.tag) == "documentation"), None)
        if documentation is not None and _text_of(documentation):
            text = _text_of(documentation)
            entry["documentation"] = (text[:INVENTORY_DOC_CHARS] + "…"
                                      if len(text) > INVENTORY_DOC_CHARS else text)
        container = _subprocess_of(index, elem)
        if container:
            entry["subprocess"] = container
        elements.append(entry)

    # Ветки шлюзов, помеченные default: без этого планировщик не отличит
    # «необусловленная ветка — ошибка» от «необусловленная ветка — выход по
    # умолчанию», и предложил бы то, что аплайер правомерно отвергает.
    default_ids = {
        elem.get("default") for elem in index.elements.values()
        if isinstance(elem.tag, str) and _local(elem.tag) in GATEWAY_TAGS
        and elem.get("default")
    }
    flows = []
    for flow in index.sequence_flows:
        entry = {
            "id": flow.get("id", ""),
            "kind": "sequence",
            "source": flow.get("sourceRef", ""),
            "target": flow.get("targetRef", ""),
        }
        if flow.get("id") in default_ids:
            entry["default"] = True
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

    # Дорожки — отдельным списком: без id и имён планировщик не сможет указать
    # цель для move_to_lane и начнёт выдумывать несуществующие дорожки.
    lanes = []
    for lane in index.lanes:
        process = index.enclosing_process(lane)
        lanes.append({
            "id": lane.get("id", ""),
            "name": lane.get("name", ""),
            "participant": index.participant_name(process) if process is not None else "",
        })

    limits: Dict[str, int] = {}
    shown_elements = elements[:INVENTORY_MAX_ELEMENTS]
    if len(elements) > len(shown_elements):
        limits["elements_omitted"] = len(elements) - len(shown_elements)
    shown_ids = {e["id"] for e in shown_elements} | {
        p["id"] for p in participants if p.get("id")}
    kept_flows = [f for f in flows if f["source"] in shown_ids
                  and f["target"] in shown_ids][:INVENTORY_MAX_FLOWS]
    if len(kept_flows) < len(flows):
        limits["flows_omitted"] = len(flows) - len(kept_flows)
    shown_lanes = lanes[:INVENTORY_MAX_LANES]
    if len(lanes) > len(shown_lanes):
        limits["lanes_omitted"] = len(lanes) - len(shown_lanes)

    inventory: Dict[str, Any] = {
        "participants": participants,
        "elements": shown_elements,
        "flows": kept_flows,
        "lanes": shown_lanes,
    }
    if limits:
        inventory["limits"] = limits
    return inventory


# ---------------------------------------------------------------------------
# Аплайер операций
# ---------------------------------------------------------------------------

class _Skip(Exception):
    """Операция не применена. `needs` — id, которых операция ждёт: если тот же
    пакет создаёт их следующей операцией, аплайер отложит её, а не выбросит
    вместе со всей веткой."""

    def __init__(self, reason: str, hint: str = "", needs: Tuple[str, ...] = ()):
        super().__init__(reason)
        self.hint = hint
        self.needs = tuple(n for n in needs if n)


def _require_element(index: _Index, elem_id: Any) -> ET.Element:
    """Узел по его id. Id потока — отдельный отказ: «элемент не найден» модель
    читает как «id выдуман», хотя инвентарь поток называет, и в корректирующий
    повтор приносит тот же id (прогон #49: один и тот же пропуск в плане и в
    повторе)."""
    key = str(elem_id or "")
    elem = index.elements.get(key)
    if elem is not None:
        return elem
    if key in index.flows_by_id:
        raise _Skip(
            f"'{key}' — поток, а не узел",
            "узлу (задаче или шлюзу) правят имя, описание и дорожку; из потоков "
            "по id принимает правку только add_condition",
        )
    raise _Skip("элемент не найден", "используйте id из инвентаря")


def _require_new_id(op_id: Optional[str], index: _Index) -> str:
    if not op_id or not isinstance(op_id, str):
        raise _Skip("не задан id нового элемента", "укажите id с префиксом new_")
    if not op_id.startswith("new_"):
        raise _Skip(
            f"id '{op_id}' без префикса new_",
            "новые элементы обязаны иметь id, начинающийся с new_",
        )
    if index.is_taken(op_id):
        raise _Skip(f"id '{op_id}' уже занят", "используйте уникальный id")
    return op_id


def _remove_refs(elem: ET.Element, flow_id: str) -> None:
    for ref_tag in ("incoming", "outgoing"):
        for ref in list(elem.findall(_q(ref_tag))):
            if (ref.text or "").strip() == flow_id:
                elem.remove(ref)


def _position_after(elem: ET.Element, preceding_tags: set) -> int:
    """Индекс вставки ребёнка так, чтобы порядок тегов BPMN не сломался:
    сразу после идущих раньше тегов (документация, extensionElements, ...)."""
    position = 0
    for child in elem:
        if isinstance(child.tag, str) and _local(child.tag) in preceding_tags:
            position += 1
        else:
            break
    return position


def _ensure_collaboration(index: _Index) -> ET.Element:
    if index.collaboration is not None:
        return index.collaboration
    collaboration = ET.Element(_q("collaboration"), {"id": "Collaboration_1"})
    # collaboration по схеме BPMN идёт перед процессами
    first_process = index.processes[0] if index.processes else None
    if first_process is not None:
        position = list(index.root).index(first_process)
        index.insert(index.root, position, collaboration)
    else:
        index.adopt(index.root, collaboration)
    return collaboration


def _ensure_lane_set(index: _Index, process: ET.Element) -> ET.Element:
    lane_set = process.find(_q("laneSet"))
    if lane_set is None:
        lane_set = ET.Element(_q("laneSet"), {"id": f"{process.get('id', 'Process')}_LaneSet"})
        # laneSet — первый ребёнок процесса (после документации), до flow-элементов:
        # именно так пишет bpmn-js, и так его понимает валидатор.
        index.insert(process, _position_after(process, _REFS_PRECEDING_TAGS), lane_set)
    return lane_set


def _new_id(index: _Index, prefix: str) -> str:
    """Свободный id с префиксом new_ — потоки, дорожки и узлы делят одно
    пространство имён xml:id во всём документе."""
    counter = 1
    while index.is_taken(f"{prefix}_{counter}"):
        counter += 1
    return f"{prefix}_{counter}"


def _adopt_event_definition(index: _Index, owner: ET.Element, key: str,
                            owner_id: str,
                            timing: Optional[Tuple[str, str]] = None) -> ET.Element:
    """Добавить элементу определение события (bpmn:timerEventDefinition и т. п.).

    Без определения «событие с таймером» остаётся декларацией: bpmn-js рисует
    пустой кружок, а скоринг не засчитывает тип события. `timing` — запрошенный
    хронометраж (тег + значение); без него таймер получает DEFAULT_TIMER_DURATION.
    """
    tag, timer_tag = EVENT_DEFINITIONS[key]
    definition = ET.Element(_q(tag), {"id": f"{owner_id}_def"})
    if timer_tag:
        time_tag, time_text = timing if timing else (timer_tag, DEFAULT_TIMER_DURATION)
        ET.SubElement(definition, _q(time_tag)).text = time_text
    return index.adopt(owner, definition)


def _insert_after(new_elem: ET.Element, after_id: str, index: _Index) -> List[str]:
    """Вставка элемента после существующего с переподвеской потоков.
    Возвращает пояснения для отчёта."""
    after = index.elements.get(after_id)
    if after is None:
        raise _Skip(
            f"элемент '{after_id}' не найден",
            "используйте существующие id из инвентаря",
            needs=(after_id,),
        )
    if _local(after.tag) in SEQUENCE_FORBIDDEN_SOURCES:
        raise _Skip(
            f"после '{after_id}' ({_local(after.tag)}) шага не будет",
            "из конечного события исходящий поток не рисуют: вставляйте шаг "
            "до него, операцией after у предыдущего узла маршрута",
        )
    process = index.process_of.get(after_id)
    if process is None:
        raise _Skip(f"не удалось определить пул элемента '{after_id}'")
    # Вставляем в непосредственный контейнер узла, а не в процесс: у шага
    # внутри subProcess и новый элемент, и переподвешиваемый поток обязаны
    # остаться того же уровня вложенности. `connect` границу подпроцесса
    # отвергает, и вставка не должна делать то, что соединению запрещено.
    container = index.parents.get(after) if index.parents.get(after) is not None else process

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
    in_flow_id = _new_id(index, "new_Flow")
    in_flow = ET.Element(_q("sequenceFlow"), {
        "id": in_flow_id, "sourceRef": after_id, "targetRef": new_id,
    })
    index.adopt(container, in_flow)
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
                # conditionExpression — лист без id, в индексе не участвует.
                in_flow.append(condition)
        flow.set("sourceRef", new_id)
        _remove_refs(after, flow.get("id", ""))
        ET.SubElement(new_elem, _q("outgoing")).text = flow.get("id")
        moved.append(flow.get("id", ""))

    index.adopt(container, new_elem)
    if moved:
        return [f"потоки после '{after_id}' переподвешены через '{new_id}'"]
    return []


def _create_sequence_flow(index: _Index, source_id: str, target_id: str,
                          process: ET.Element,
                          condition: Optional[str] = None) -> ET.Element:
    flow_id = _new_id(index, "new_Flow")
    flow = ET.Element(_q("sequenceFlow"), {
        "id": flow_id,
        "sourceRef": source_id,
        "targetRef": target_id,
    })
    if condition:
        ET.SubElement(flow, _q("conditionExpression")).text = condition
    index.adopt(process, flow)
    for elem_id, ref_tag in ((source_id, "outgoing"), (target_id, "incoming")):
        elem = index.elements.get(elem_id)
        if elem is not None:
            ET.SubElement(elem, _q(ref_tag)).text = flow_id
    return flow


def _create_message_flow(index: _Index, source_id: str, target_id: str,
                         name: str = "") -> ET.Element:
    collaboration = _ensure_collaboration(index)
    flow_id = _new_id(index, "new_MessageFlow")
    attrs = {"id": flow_id, "sourceRef": source_id, "targetRef": target_id}
    if name:
        attrs["name"] = name
    flow = ET.Element(_q("messageFlow"), attrs)
    return index.adopt(collaboration, flow)


def _process_nodes(index: _Index, process: ET.Element) -> List[Tuple[str, ET.Element]]:
    """Узлы процесса верхнего уровня: узлы внутри subProcess и граничные
    события событиями процесса подключать нельзя."""
    nodes = []
    for elem_id, elem in index.elements.items():
        if index.process_of.get(elem_id) is not process:
            continue
        if elem.get("attachedToRef") or index.has_subprocess_ancestor(elem):
            continue
        nodes.append((elem_id, elem))
    return nodes


def _link_new_event(index: _Index, process: ET.Element, event_id: str,
                    tag: str) -> Optional[str]:
    """Привязать добавленное стартовое/конечное событие к узлу процесса: для
    start — к первому узлу без входа, для end — к первому без выхода.
    Возвращает id выбранного узла либо None, если кандидата нет."""
    # Ориентир — фактические потоки, а не теги incoming/outgoing: в XML
    # пользователя ссылки могут расходиться (их ровняет validate_and_repair).
    wanted_attr = "targetRef" if tag == "startEvent" else "sourceRef"
    occupied = {f.get(wanted_attr) for f in index.sequence_flows}
    # Событие того же фронта кандидатом быть не может: старт не принимает вход,
    # у энда не бывает выхода, граничное событие по смыслу вообще без рёбер.
    blocked = ({"startEvent", "boundaryEvent"} if tag == "startEvent"
               else {"endEvent", "boundaryEvent"})
    for elem_id, elem in _process_nodes(index, process):
        if _local(elem.tag) in blocked or elem_id in occupied:
            continue
        if tag == "startEvent":
            _create_sequence_flow(index, event_id, elem_id, process)
        else:
            _create_sequence_flow(index, elem_id, event_id, process)
        return elem_id
    return None


def _op_add_participant(op: Dict[str, Any], index: _Index) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя нового пула", "укажите name")
    collaboration = _ensure_collaboration(index)
    process = ET.Element(_q("process"), {
        "id": f"{op_id}_proc", "isExecutable": "true",
    })
    participant = ET.Element(_q("participant"), {
        "id": op_id, "name": name, "processRef": process.get("id", ""),
    })
    index.adopt(collaboration, participant)
    index.adopt(index.root, process)
    return [f"создан пул '{name}'"]


def _event_definition_key(op: Dict[str, Any]) -> Optional[str]:
    """Определение события из операции: None — не запрошено, иначе валидируется."""
    raw = op.get("event_definition")
    if raw is None or not str(raw).strip():
        return None
    key = str(raw).strip().lower()
    if key not in EVENT_DEFINITIONS:
        raise _Skip(
            f"неизвестное определение события '{raw}'",
            "допустимы: " + ", ".join(sorted(EVENT_DEFINITIONS)),
        )
    return key


def _check_outlet(op: Dict[str, Any], index: _Index, process: ET.Element,
                  source_tag: str, after_id: str = "") -> str:
    """Проверяет явный исход нового узла ДО его создания.

    Порядок принципиален: `after` перешивает существующий поток, и отказ в
    середине оставил бы разорванную цепочку вместо целой схемы. Модель в живых
    прогонах добавляла шаг и один connect к нему — исход она забывала, шаг
    откатывался тупиком, и вся правка уходила в отказ. `to` даёт обе стороны
    одной операцией, поэтому проверяется заранее и не может упасть позже.
    """
    outlet_id = str(op.get("to") or "").strip()
    if not outlet_id:
        return ""
    if source_tag in SEQUENCE_FORBIDDEN_SOURCES:
        raise _Skip(
            f"{source_tag} завершает маршрут: исход у него не задают",
            "уберите to — конечное событие только принимают потоком",
        )
    outlet = index.elements.get(outlet_id)
    if outlet is None:
        raise _Skip(f"цель исхода '{outlet_id}' не найдена",
                    "укажите to id элемента из инвентаря", needs=(outlet_id,))
    outlet_tag = _local(outlet.tag)
    if outlet_tag in SEQUENCE_FORBIDDEN_TARGETS:
        raise _Skip(
            f"в '{outlet_id}' ({outlet_tag}) исход нового узла не входит",
            "ведите поток к шагу или конечному событию его пула",
        )
    if index.process_of.get(outlet_id) is not process:
        # Исход в чужой пул — передача сообщения, а не разорванный маршрут:
        # узел остаётся в своём процессе, а дуга уходит на уровень коллаборации
        # (тот же приём у `validate_and_repair`, шаг 1b).
        if not _message_leg_ok(source_tag, outlet_tag):
            raise _Skip(
                "цель исхода лежит в другом пуле",
                "sequence-поток остаётся внутри процесса, между пулами — "
                "отдельный connect с flow_type=message",
            )
        return outlet_id
    after_elem = index.elements.get(after_id) if after_id else None
    scope = (index.has_subprocess_ancestor(after_elem) if after_elem is not None
             else index.has_subprocess_ancestor(process))
    if index.has_subprocess_ancestor(outlet) is not scope:
        raise _Skip(
            "цель исхода лежит на другой вложенности",
            "поток не пересекает границу subProcess: вставляйте шаг того же "
            "уровня, что и его исход",
        )
    return outlet_id


def _link_outlet(index: _Index, source_id: str, outlet_id: str,
                 process: ET.Element) -> str:
    """Провёренный `_check_outlet` исход нового узла: поток создаётся здесь.

    `after` уже может вести новый узел ровно туда же — второй поток тем же
    маршрутом не добавляется, иначе схема получила бы немоделируемую двойную
    ветку.
    """
    for flow in index.sequence_flows:
        if (flow.get("sourceRef") == source_id
                and flow.get("targetRef") == outlet_id):
            return (f"исход к '{outlet_id}' уже даёт вставка after — "
                    "второй поток не создан")
    if index.process_of.get(outlet_id) is not process:
        flow = _create_message_flow(index, source_id, outlet_id)
        return (f"исход к '{outlet_id}' между пулами стал потоком-сообщением "
                f"'{flow.get('id')}': sequenceFlow не пересекает границу "
                "процесса")
    flow = _create_sequence_flow(index, source_id, outlet_id, process, None)
    return f"исход потока '{flow.get('id')}' → '{outlet_id}'"


def _pool_skip(index: "_Index", key: Any) -> "_Skip":
    """Отказ «пул не определён» с перечислением того, что подходило бы.

    Если нестрогих кандидатов несколько, молчаливый отказ заставил бы модель
    гадать дальше; список имён переводит повтор в один точный выбор. Кандидатов
    нет вовсе — подсказка обязана назвать имеющиеся пулы и операции, которыми
    участника заводят: без этого повтор возвращал ровно ту же операцию.
    """
    candidates = index.pool_candidates(key)
    if candidates:
        return _Skip(
            "пул не определён",
            f"под «{key}» подходит несколько пулов: "
            + ", ".join(f"«{c}»" for c in candidates)
            + " — назовите один из них",
        )
    known = sorted({index.participant_name(process) or process.get("id") or ""
                    for process in index.processes} - {""})
    return _Skip(
        "пул не определён",
        "такого участника на схеме нет; есть "
        + ", ".join(f"«{k}»" for k in known[:8])
        + " — назовите один из них либо заведите недостающего: add_lane "
        "(роль или подразделение существующей организации) или add_participant "
        "(самостоятельный участник)")


def _op_add_node(op: Dict[str, Any], index: _Index, kind: str) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя элемента", "укажите name")
    process = index.resolve_process(op.get("participant"))
    lane_key = str(op.get("lane") or "").strip()
    lane = None
    lane_alias_note = ""
    alias = (op.get("participant") or "").strip() if isinstance(
        op.get("participant"), str) else ""
    if process is None and alias and not lane_key:
        # «Участник» может быть ролью, которая на схеме уже дорожка: читаем
        # это по имени дорожки, а не по догадке о структуре.
        process, lane = index.lane_home(alias)
        if process is not None:
            lane_alias_note = (f"«{alias}» — дорожка пула "
                               f"«{index.participant_name(process)}»: шаг "
                               "ляжет в неё")
    if process is None:
        # Пул нового узла читается из уже названной им привязки, а не угадывается
        # по смыслу: хозяин по `attachedToRef` живёт в том же процессе, что и
        # событие (этого требует BPMN, а не наша догадка), а вставка `after`
        # обязана остаться в пуле своего соседа (её наружу переносит отдельная
        # проверка ниже). Без этого отказа пакет терял ветку целиком: прогон #45
        # — `add_boundary_event` без `participant` отсечён, следом «цель исхода
        # не найдена» у `add_task` и «источник не найден» у `connect`,
        # SLA-таймер до схемы не доехал.
        #
        # По `after` наследуем только когда пула в операции нет вовсе: названный,
        # но неизвестный участник — не недостающая деталь, а заявка на нового
        # участника, и молча переложить его в пул соседа значит лишить модель
        # подсказки, из какого пула взять имя или как его завести.
        anchor = index.elements.get(str(op.get("attached_to") or "").strip())
        if anchor is None and not alias:
            anchor = index.elements.get(str(op.get("after") or "").strip())
        if anchor is not None:
            process = index.enclosing_process(anchor)
    if process is None:
        raise _pool_skip(index, op.get("participant"))
    if lane_key:
        # Дорожку валидируем до создания узла: отказ в середине оставил бы
        # элемент в процессе без дорожки, а в отчёте — частичную правку.
        lane = index.find_lane(lane_key)
        if lane is None:
            raise _Skip(
                f"дорожка '{lane_key}' не найдена",
                "создайте её операцией add_lane или возьмите id из инвентаря",
                needs=(lane_key,),
            )
        if index.enclosing_process(lane) is not process:
            raise _Skip(
                "дорожка принадлежит другому пулу",
                "указывайте lane дорожкой того же пула, где создаёте элемент",
            )
    definition = _event_definition_key(op)
    alias_note = ""

    if kind == "task":
        if definition:
            raise _Skip(
                "определение события задают только событию",
                "перенесите event_definition в add_event",
            )
        task_type = op.get("task_type") or "task"
        if task_type not in ADD_TASK_TAGS:
            raise _Skip(
                f"неизвестный тип задачи '{task_type}'",
                "допустимы: " + ", ".join(sorted(ADD_TASK_TAGS))
                + " (subProcess и callActivity создать нечем наполнить)",
            )
        elem = ET.Element(_q(task_type), {"id": op_id, "name": name})
        tag = task_type
    elif kind == "gateway":
        if definition:
            raise _Skip("определение события шлюзу не требуется")
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
        if not tag and event_type.lower() in EVENT_DEFINITIONS:
            # Модель просит «таймер» или «ошибку» как тип события. Это
            # определение ловушки, а ловит её промежуточное событие: без
            # такого прочтения ветка обработки откатывалась каскадом пропусков.
            tag = EVENT_TYPE_TO_TAG["intermediatecatch"]
            definition = definition or event_type.lower()
            alias_note = (f"тип события '{event_type}' понят как "
                          f"intermediateCatch с определением '{definition}'")
        if not tag:
            raise _Skip(
                f"неизвестный тип события '{event_type}'",
                "допустимы: start, end, intermediateCatch, intermediateThrow "
                "либо определение события: " + ", ".join(sorted(EVENT_DEFINITIONS)),
            )
        if definition and tag in ("startEvent", "endEvent"):
            raise _Skip(
                "стартовому и конечному событию определение не добавляется",
                "операцией add_boundary_event можно добавить таймер или ошибку",
            )
        timing = _timer_timing(op, definition)
        elem = ET.Element(_q(tag), {"id": op_id, "name": name})
        if definition:
            _adopt_event_definition(index, elem, definition, op_id, timing)

    after_id = op.get("after")
    outlet_id = _check_outlet(op, index, process, tag,
                              str(after_id or "").strip())
    notes: List[str] = [x for x in (alias_note, lane_alias_note) if x]
    if tag in ("startEvent", "endEvent"):
        # Событие без рёбер — висячий узел, который скоринг считает дефектом
        # связей, поэтому пробуем сразу привязать его к потоку. Явный `to`
        # сильнее автопривязки: маршрут модель описала сама.
        index.adopt(process, elem)
        if outlet_id:
            linked = None
        else:
            linked = _link_new_event(index, process, op_id, tag)
        notes.append(
            f"событие подключено к '{linked}'" if linked
            else ("событие выведено явным to" if outlet_id
                  else "событие добавлено без автопривязки потоков"))
    elif after_id:
        target_process = index.process_of.get(after_id)
        if target_process is not None and target_process is not process:
            # «Вставь после шага чужого пула» — это вход по сообщению: узел
            # живёт в своём процессе, а дуга уходит на коллаборацию. Отказ здесь
            # съедал правку целиком (прогон #46: SLA-событие постмортема так и не
            # доехало до схемы, `defects_repaired` 0.0).
            anchor = index.elements.get(str(after_id).strip())
            anchor_tag = _local(anchor.tag) if anchor is not None else ""
            if not _message_leg_ok(anchor_tag, tag):
                raise _Skip(
                    "элемент 'after' находится в другом пуле",
                    "вставляйте элемент в тот же пул, где стоит 'after'",
                )
            index.adopt(process, elem)
            flow = _create_message_flow(index, str(after_id).strip(), op_id)
            notes.append(f"вход из '{after_id}' между пулами стал потоком-"
                         f"сообщением '{flow.get('id')}': sequenceFlow не "
                         "пересекает границу процесса")
        else:
            notes.extend(_insert_after(elem, after_id, index))
    else:
        index.adopt(process, elem)
    if outlet_id:
        notes.append(_link_outlet(index, op_id, outlet_id, process))
    if lane is not None:
        # Новый узел становится ребёнком процесса, а дорожка только ссылается
        # на него — ровно так пишет bpmn-js (см. _op_move_to_lane).
        ET.SubElement(lane, _q("flowNodeRef")).text = op_id
        notes.append(f"помещён в дорожку '{lane.get('name') or lane_key}'")
    return notes or [f"добавлен элемент '{name}'"]


def _op_rename(op: Dict[str, Any], index: _Index) -> List[str]:
    elem = _require_element(index, op.get("id"))
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано новое имя")
    elem.set("name", name)
    return []


def _op_add_documentation(op: Dict[str, Any], index: _Index) -> List[str]:
    elem = _require_element(index, op.get("id"))
    text = (op.get("text") or "").strip()
    if not text:
        raise _Skip("не задан текст документации", "укажите text")
    existing = [doc for doc in elem
                if isinstance(doc.tag, str) and is_bpmn_tag(doc.tag)
                and _local(doc.tag) == "documentation" and _text_of(doc)]
    if any(_text_of(doc) == text for doc in existing):
        # Повтор той же правки не «применён», а бесполезен: без этого отчёта
        # модель могла бесконечно дописывать одно и то же описание.
        return ["такая документация у элемента уже есть — добавлено не было"]
    doc = ET.Element(_q("documentation"))
    doc.text = text
    # По схеме BPMN документация идёт до extensionElements и до incoming /
    # outgoing; несколько документации складываются рядом.
    elem.insert(_position_after(elem, {"documentation"}), doc)
    return [f"дополнено к {len(existing)} существующей(им)"] if existing else []


def _op_add_lane(op: Dict[str, Any], index: _Index) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя дорожки", "укажите name")
    process = index.resolve_process(op.get("participant"))
    if process is None:
        raise _pool_skip(index, op.get("participant"))
    lane_set = _ensure_lane_set(index, process)
    lane = ET.Element(_q("lane"), {"id": op_id, "name": name})
    index.adopt(lane_set, lane)
    return [f"добавлена дорожка '{name}'"]


def _op_move_to_lane(op: Dict[str, Any], index: _Index) -> List[str]:
    elem_id = op.get("id") or ""
    elem = _require_element(index, elem_id)
    lane_key = (op.get("lane") or "").strip()
    if not lane_key:
        raise _Skip("не задана дорожка", "укажите lane — id или имя дорожки")
    lane = index.find_lane(lane_key)
    if lane is None:
        raise _Skip(
            f"дорожка '{lane_key}' не найдена",
            "создайте её операцией add_lane или возьмите id из инвентаря",
            needs=(lane_key,),
        )
    process = index.process_of.get(elem_id)
    if process is None or index.enclosing_process(lane) is not process:
        raise _Skip(
            "дорожка принадлежит другому пулу",
            "переносить элемент можно только в дорожку своего процесса",
        )
    # flowNodeRef переставляется, а не дублируется: ссылка обязана остаться
    # ровно в одной дорожке, иначе bpmn-js покажет элемент дважды.
    current_lane = next((other.get("id") or "" for other in index.lanes
                         for ref in other.findall(_q("flowNodeRef"))
                         if (ref.text or "").strip() == elem_id), "")
    if current_lane == (lane.get("id") or ""):
        # No-op обязан называться no-op: иначе «применено» в отчёте там, где
        # схема не изменилась, и модель не поймёт, что правка прошла зря.
        return ["элемент уже стоит в этой дорожке — схема не изменилась"]
    for other in index.lanes:
        for ref in list(other.findall(_q("flowNodeRef"))):
            if (ref.text or "").strip() == elem_id:
                other.remove(ref)
    # Сам элемент остаётся ребёнком процесса — так пишет bpmn-js; дорожка
    # ссылается на него только flowNodeRef.
    ET.SubElement(lane, _q("flowNodeRef")).text = elem_id
    return []


def _attached_boundaries(index: _Index, host_id: str) -> List[ET.Element]:
    """Граничные события, прицепленные к узлу: без хозяина они не существуют."""
    return [elem for elem in index.elements.values()
            if elem.get("attachedToRef") == host_id]


def _remove_node(index: _Index, elem: ET.Element) -> Tuple[List[str], List[str]]:
    """Вынуть узел вместе с его потоками, прицепленными событиями и ссылками
    на них. Возвращает (id удалённых потоков, id удалённых граничных событий)."""
    elem_id = elem.get("id") or ""
    removed_flows = []
    removed_boundaries: List[str] = []
    # Событие без хозяина — битый XML: bpmn-js его не рисует, а скоринг и
    # оракул считают dangling attachedToRef. Уносить его вместе с задачей
    # честнее, чем оставить сироту, которую нечем починить.
    for boundary in _attached_boundaries(index, elem_id):
        flows, nested = _remove_node(index, boundary)
        removed_flows.extend(flows)
        removed_boundaries.extend(nested)
        removed_boundaries.append(boundary.get("id") or "")
    for flow in list(index.sequence_flows) + list(index.message_flows):
        if elem_id in (flow.get("sourceRef"), flow.get("targetRef")):
            index.detach(flow)
            removed_flows.append(flow.get("id", ""))
    for other in index.elements.values():
        if other is elem:
            continue
        for flow_id in removed_flows:
            _remove_refs(other, flow_id)
    index.detach(elem)
    return removed_flows, removed_boundaries


def _op_delete(op: Dict[str, Any], index: _Index) -> List[str]:
    elem_id = op.get("id") or ""
    elem = _require_element(index, elem_id)
    if _local(elem.tag) in ("startEvent", "endEvent"):
        raise _Skip(
            "стартовые и конечные события не удаляются",
            "у процесса должен оставаться вход и выход",
        )
    removed_flows, removed_boundaries = _remove_node(index, elem)
    notes = []
    if removed_boundaries:
        notes.append("вместе с хозяином удалены граничные события: "
                     + ", ".join(removed_boundaries))
    if removed_flows:
        notes.append(f"удалены связанные потоки: {', '.join(removed_flows)}")
    return notes


def _flag(value: Any, field: str) -> bool:
    """Булево поле операции. Модель нередко присылает «true» строкой, а
    «false» строкой — не то же самое, что False."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "да"):
            return True
        if text in ("", "false", "0", "no", "нет"):
            return False
        raise _Skip(f"поле {field} должно быть булевым", f'укажите {field}: true либо false')
    return bool(value)


def _gateway_branches(index: _Index, gateway_id: str) -> List[ET.Element]:
    return [f for f in index.sequence_flows if f.get("sourceRef") == gateway_id]


def _endpoint_process(index: "_Index", elem: ET.Element) -> Optional[ET.Element]:
    """Пузел узла так, как его видит дерево, а не снимок индекса.

    `process_of` строится на разбор XML и пополняется при вставках, но у
    свежего узла его там ещё может не быть — и тогда проверка «sequence-поток
    не ходит между пулами» сравнивает два None и пропускает дугу. Вычитка
    такой поток снимает, а на схеме остаётся граничное событие без ветки
    обработки: принятое улучшение портило схему (замерено живым прогоном,
    −16 баллов). Граничное событие при этом смотрим через хозяина: его маршрут
    живёт в пуле того, к чему он прицеплен.
    """
    process = index.process_of.get(elem.get("id") or "")
    if process is not None:
        return process
    if _local(elem.tag) == "boundaryEvent" or elem.get("attachedToRef"):
        host = index.elements.get(elem.get("attachedToRef") or "")
        if host is not None:
            return index.enclosing_process(host)
    return index.enclosing_process(elem)


def _op_connect(op: Dict[str, Any], index: _Index) -> List[str]:
    source_id = op.get("source") or ""
    target_id = op.get("target") or ""
    source = index.elements.get(source_id)
    target = index.elements.get(target_id)
    if source is None or target is None:
        raise _Skip(
            "источник или цель не найдены",
            "используйте существующие id из инвентаря",
            needs=(source_id if source is None else "",
                   target_id if target is None else ""),
        )
    flow_type = (op.get("flow_type") or "sequence").lower()
    if flow_type not in ("sequence", "message"):
        raise _Skip("flow_type должен быть sequence или message")
    # Выход по умолчанию — атрибут шлюза на sequence-потоке из него: раньше
    # аплайер не умел его ставить, и любая «иначе»-ветка отвергалась.
    default_requested = _flag(op.get("default"), "default")
    if default_requested:
        if flow_type != "sequence":
            raise _Skip("выходом по умолчанию назначают sequence-поток",
                        "уберите default у messageFlow")
        if _local(source.tag) not in GATEWAY_TAGS:
            raise _Skip(f"'{source_id}' не является шлюзом",
                        "default ставят только шлюзу, у задачи ветки по умолчанию нет")
    source_process = _endpoint_process(index, source)
    target_process = _endpoint_process(index, target)
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
    # Поток не должен пересекать границу subProcess: узел внутри подпроцесса
    # принадлежит ему, и bpmn-js такую конструкцию не отрисует.
    if index.has_subprocess_ancestor(source) is not index.has_subprocess_ancestor(target):
        raise _Skip(
            "элементы лежат на разной вложенности",
            "поток не может выходить из subProcess: соединяйте узлы одного уровня",
        )
    # Концы sequenceFlow легальны только так: стартовое событие не принимают,
    # из конечного не выходят, а у граничного события вход — хозяин через
    # attachedToRef, не поток. Ни скоринг, ни оракул такого дефекта не видят,
    # поэтому проверка обязана быть здесь.
    source_tag = _local(source.tag)
    target_tag = _local(target.tag)
    if flow_type == "sequence":
        if target_tag in SEQUENCE_FORBIDDEN_TARGETS:
            raise _Skip(
                f"в '{target_id}' ({target_tag}) sequence-поток не входит",
                "стартовое событие запускает процесс, а граничное событие "
                "прикреплено к задаче: соединяйте их потоком только НАВЫС",
            )
        if source_tag in SEQUENCE_FORBIDDEN_SOURCES:
            raise _Skip(
                f"из '{source_id}' ({source_tag}) исходящего потока не бывает",
                "конечное событие завершает маршрут: продолжение от него "
                "нельзя нарисовать по BPMN",
            )
    existing = [
        f for f in index.sequence_flows + index.message_flows
        if f.get("sourceRef") == source_id and f.get("targetRef") == target_id
    ]
    if existing:
        raise _Skip("такой поток уже существует",
                    "существующую ветку помечают выходом по умолчанию "
                    "операцией set_default")
    notes: List[str] = []
    if flow_type == "sequence":
        condition = (op.get("condition") or "").strip() or None
        if condition and _local(source.tag) not in GATEWAY_TAGS:
            # Условие на потоке от задачи движок не читает: генератор за такое
            # пишет заметку, здесь — та же честность вместо молчаливой правки.
            notes.append(f"условие задано, но источник {source_id} — не шлюз")
        if default_requested:
            # У выхода по умолчанию условия быть не может: иначе модель
            # описывает «иначе» и условием, и веткой по умолчанию сразу.
            if condition:
                condition = None
                notes.append("условие с выхода по умолчанию снято")
            previous_default = source.get("default")
            if previous_default:
                raise _Skip(
                    f"у шлюза '{source_id}' уже есть выход по умолчанию ({previous_default})",
                    "переназначьте default операцией set_default на нужную ветку",
                )
        if (_local(source.tag) == "exclusiveGateway" and condition is None
                and not default_requested
                # Единственный выход шлюза — это схождение развилки, а не
                # развилка: требовать от него условие или default нельзя, иначе
                # аплайер отвергал бы корректную пару «шлюз на входе, шлюз на
                # выходе», которую предписывает промпт.
                and _gateway_branches(index, source_id)):
            notes.extend(_resolve_unconditioned_branch(index, source, source_id))
            default_requested = True
        flow = _create_sequence_flow(index, source_id, target_id, source_process,
                                     condition)
        if default_requested:
            source.set("default", flow.get("id", ""))
            notes.append(f"поток '{flow.get('id')}' — выход по умолчанию "
                         f"шлюза '{source_id}'")
    else:
        _create_message_flow(index, source_id, target_id)
    return notes


def _resolve_unconditioned_branch(index: _Index, gateway: ET.Element,
                                  gateway_id: str) -> List[str]:
    """Что делать с необусловленной веткой исключающего шлюза.

    Скоринг требует условие на каждой ветке либо отметку default, поэтому
    молча создать третью «просто ветку» — ухудшить схему. Варианты:
    • у шлюза уже есть default — вторую ветку по умолчанию не сделать, отказ;
    • условий на ветках нет вообще — непонятно, какая из них «иначе», отказ;
    • ветки с условиями есть — эта ветка и есть «иначе», становится default.
    """
    previous_default = gateway.get("default")
    conditioned = [
        f for f in _gateway_branches(index, gateway_id)
        if f.find(_q("conditionExpression")) is not None
    ]
    if previous_default:
        raise _Skip(
            f"у шлюза '{gateway_id}' уже есть выход по умолчанию ({previous_default}), "
            "а новая ветка идёт без условия",
            "добавьте condition: default у шлюза занят, двух выходов по умолчанию не бывает",
        )
    if not conditioned:
        raise _Skip(
            f"у шлюза '{gateway_id}' нет ни условий на ветках, ни выхода по умолчанию",
            'проставьте condition на ветках, а одну из них пометьте "default": true',
        )
    return [f"необусловленная ветка стала выходом по умолчанию: у шлюза "
            f"'{gateway_id}' уже есть условные потоки"]


def _op_set_default(op: Dict[str, Any], index: _Index) -> List[str]:
    """Пометить существующую ветку шлюза выходом по умолчанию.

    Нужна именно отдельная операция: `connect` на уже существующую пару
    отвергается как дубль, а без default необусловленная ветка из генерации
    остаётся ошибкой по правилу gateway_conditions.
    """
    gateway_id = op.get("gateway") or ""
    flow_id = op.get("flow") or ""
    gateway = index.elements.get(gateway_id)
    if gateway is None:
        raise _Skip("шлюз не найден", "используйте id шлюза из инвентаря")
    if _local(gateway.tag) not in GATEWAY_TAGS:
        raise _Skip(f"'{gateway_id}' не является шлюзом",
                    "выход по умолчанию бывает только у шлюза")
    flow = index.find_flow(flow_id)
    if flow is None:
        raise _Skip("поток не найден", "используйте id потока из инвентаря")
    if _local(flow.tag) != "sequenceFlow":
        raise _Skip(f"'{flow_id}' не sequence-поток",
                    "выходом по умолчанию назначают ветку шлюза, а не messageFlow")
    if flow.get("sourceRef") != gateway_id:
        raise _Skip(f"поток '{flow_id}' выходит не из шлюза '{gateway_id}'",
                    "укажите id ветки этого шлюза")
    previous_default = gateway.get("default")
    gateway.set("default", flow_id)
    notes = []
    condition = flow.find(_q("conditionExpression"))
    if condition is not None:
        flow.remove(condition)
        notes.append(f"условие с потока '{flow_id}' снято — он выход по умолчанию")
    if previous_default and previous_default != flow_id:
        notes.append(f"прежний выход по умолчанию ({previous_default}) снят")
    return notes or [f"поток '{flow_id}' — выход по умолчанию шлюза '{gateway_id}'"]


def _op_disconnect(op: Dict[str, Any], index: _Index) -> List[str]:
    flow_id = op.get("flow") or ""
    flow = index.find_flow(flow_id)
    if flow is None:
        raise _Skip("поток не найден", "используйте id потока из инвентаря")
    index.detach(flow)
    for elem in index.elements.values():
        _remove_refs(elem, flow_id)
        # default — тоже ссылка на поток: без неё в пакете нельзя переназначить
        # выход по умолчанию, а после починки атрибут всё равно был бы снят.
        if elem.get("default") == flow_id:
            del elem.attrib["default"]
    return []


def _op_move(op: Dict[str, Any], index: _Index) -> List[str]:
    elem_id = op.get("id") or ""
    elem = _require_element(index, elem_id)
    current = index.process_of.get(elem_id)
    target = index.resolve_process(op.get("participant"))
    if target is None:
        raise _Skip("целевой пул не найден", "укажите имя или id пула")
    if current is target:
        return []
    if index.has_subprocess_ancestor(elem):
        raise _Skip(
            "элемент внутри subProcess не переносится между пулами",
            "переносить можно только узлы верхнего уровня: вынесите элемент из subProcess",
        )
    # Граничные события хозяина едут вместе с шагом: оставить их в старом пуле
    # значит завести attachedToRef в чужой процесс, а такая ссылка не чинится
    # пересборкой incoming/outgoing.
    carried = _attached_boundaries(index, elem_id)
    carried_ids = [b.get("id") or "" for b in carried]
    moved_ids = {elem_id} | {i for i in carried_ids if i}
    broken = []
    for flow in list(index.sequence_flows):
        ends = (flow.get("sourceRef"), flow.get("targetRef"))
        if ends[0] in moved_ids and ends[1] in moved_ids:
            continue
        if not (moved_ids & set(ends)):
            continue
        other = ends[1] if ends[0] in moved_ids else ends[0]
        if index.process_of.get(other) is not target:
            index.detach(flow)
            broken.append(flow.get("id", ""))
    for other in index.elements.values():
        for flow_id in broken:
            _remove_refs(other, flow_id)
    index.detach(elem)
    index.adopt(target, elem)
    for boundary in carried:
        index.detach(boundary)
        index.adopt(target, boundary)
    notes = []
    if broken:
        notes.append(f"межпульные потоки удалены: {', '.join(broken)}")
    if carried_ids:
        notes.append("граничные события перенесены вместе с шагом: "
                     + ", ".join(carried_ids))
    return notes


def _op_add_boundary_event(op: Dict[str, Any], index: _Index) -> List[str]:
    op_id = _require_new_id(op.get("id"), index)
    name = (op.get("name") or "").strip()
    if not name:
        raise _Skip("не задано имя элемента", "укажите name")
    event_type = str(op.get("event_type") or "").strip().lower()
    if event_type not in ("timer", "error"):
        raise _Skip(
            f"неизвестный тип граничного события '{event_type}'",
            "допустимы: timer, error",
        )
    host_id = op.get("attached_to") or ""
    host = index.elements.get(host_id)
    if host is None:
        raise _Skip(
            f"задача '{host_id}' не найдена",
            "используйте существующие id из инвентаря",
            needs=(host_id,),
        )
    if _local(host.tag) not in TASK_TAGS:
        raise _Skip(
            f"'{host_id}' не является задачей",
            "граничное событие цепляется только к задаче или подпроцессу",
        )
    process = index.process_of.get(host_id)
    if process is None:
        raise _Skip(f"не удалось определить пул элемента '{host_id}'")
    # Одно и то же определение на том же шаге дважды — не два события, а
    # дубль: второй таймер ничего не добавляет и только портит схему.
    for existing in _attached_boundaries(index, host_id):
        existing_def, _ = _definition_of(existing)
        if existing_def == event_type:
            raise _Skip(
                f"к '{host_id}' уже прицеплено {event_type}-событие "
                f"'{existing.get('id')}'",
                "замените хронометраж нельзя — создайте событие другого типа "
                "или удалите существующее операцией delete",
            )
    timing = _timer_timing(op, event_type)
    # Ветка обработки задаётся той же операцией: без `to` событие оставалось
    # кружком без ветки и откатывалось, унося с собой весь маршрут эскалации.
    outlet_id = _check_outlet(op, index, process, "boundaryEvent", host_id)
    elem = ET.Element(_q("boundaryEvent"), {
        "id": op_id, "name": name, "attachedToRef": host_id,
    })
    _adopt_event_definition(index, elem, event_type, op_id, timing)
    index.adopt(process, elem)
    note = f"граничное событие '{name}' прицеплено к '{host_id}'"
    if timing:
        note += f", {'цикл' if timing[0] == 'timeCycle' else 'длительность'} {timing[1]}"
    if outlet_id:
        note += f", ветка обработки: {_link_outlet(index, op_id, outlet_id, process)}"
    return [note]


def _op_merge_participants(op: Dict[str, Any], index: _Index) -> List[str]:
    """Слить пул в дорожку: роли одной организации — дорожки одного процесса.

    Операция рискованная: она переносит чужие узлы и потоки в другой процесс и
    удаляет межпуловые messageFlow. Поэтому пакет слиянием после применения
    проходит `validate_and_repair`, а при поломке откатывается целиком
    (см. `apply_operations`).
    """
    source_participant, source_process, source_key = index.resolve_participant(
        op.get("source"))
    target_participant, target_process, target_key = index.resolve_participant(
        op.get("target"))
    for participant, process, key, role in ((source_participant, source_process,
                                             source_key, "источник"),
                                            (target_participant, target_process,
                                             target_key, "приёмник")):
        if not key:
            raise _Skip(f"не задан пул ({role})",
                        "укажите source и target именами или id пулов из инвентаря")
        if process is None:
            raise _Skip(f"пул '{key}' не найден ({role})",
                        "используйте имена участников из инвентаря")
        if participant is None:
            raise _Skip(f"'{key}' не является пулом ({role}): процесс не заявлен "
                        "участником в коллаборации",
                        "слейте процессы по именам участников")
    if source_participant is target_participant:
        raise _Skip("пул нельзя слить в самого себя", "укажите разные source и target")

    source_name = index.participant_name(source_process)
    target_name = index.participant_name(target_process)
    as_lane = op.get("as_lane")
    if isinstance(as_lane, str):
        lane_name = as_lane.strip()
        if not lane_name:
            raise _Skip("не задано имя дорожки",
                        'as_lane: true — имя дорожки возьмём из имени '
                        "пула-источника, либо укажите имя роли строкой")
    elif as_lane is None or _flag(as_lane, "as_lane"):
        lane_name = source_name
    else:
        raise _Skip("сливать пул можно только в дорожку целевого пула",
                    'укажите as_lane: true или имя дорожки строкой')

    # Узлы источника: верхнего уровня (получают flowNodeRef новой дорожки) и
    # вложенные — они уедут внутри своего subProcess целиком. Узел, который
    # принадлежит другому процессу, признак битой схемы: переносить его нельзя.
    top_level: List[str] = []
    touched: Set[str] = {source_participant.get("id") or "",
                         source_participant.get("name") or "",
                         source_process.get("id") or "",
                         source_process.get("name") or "",
                         target_process.get("id") or ""}
    foreign: List[str] = []
    steps = 0
    moved_nodes = 0
    for child in source_process.iter():
        if child is source_process or not isinstance(child.tag, str):
            continue
        if not is_bpmn_tag(child.tag):
            continue
        child_id = child.get("id")
        if not child_id:
            continue
        if _local(child.tag) in FLOW_NODE_TAGS:
            if index.process_of.get(child_id) is not source_process:
                foreign.append(child_id)
                continue
            moved_nodes += 1
            if _local(child.tag) not in ("startEvent", "endEvent"):
                steps += 1
            touched.add(child_id)
            if index.parents.get(child) is source_process:
                top_level.append(child_id)
        elif _local(child.tag) == "lane":
            touched |= {child.get("id") or "", child.get("name") or ""}
    if foreign:
        raise _Skip(
            f"в пуле «{source_name}» есть узлы, принадлежащие чужому процессу: "
            + ", ".join(foreign[:5]),
            "сначала верните их на место операцией move_to_participant",
        )
    if not steps:
        # Старт и финиш — не содержание: такой пул надо удалять, а не тащить
        # в главную дорожку пустым фрагментом маршрута.
        raise _Skip(f"в пуле «{source_name}» нет ни одного шага",
                    "удалите пустой пул операцией remove_participant")

    lane_set = _ensure_lane_set(index, target_process)
    lane_id = _new_id(index, "new_Lane")
    lane = ET.Element(_q("lane"), {"id": lane_id, "name": lane_name})
    index.adopt(lane_set, lane)
    touched |= {lane_id, lane_name}

    moved_flows: List[str] = []
    for child in list(source_process):
        if not isinstance(child.tag, str):
            continue
        if _local(child.tag) == "laneSet":
            # Дорожки источника вместе с его пулом: узлы получат одну общую.
            index.detach(child)
            continue
        if not is_bpmn_tag(child.tag):
            continue
        if _local(child.tag) in ("sequenceFlow", "messageFlow"):
            moved_flows.append(child.get("id") or "")
        index.detach(child)
        index.adopt(target_process, child)
    touched.update(moved_flows)

    for node_id in top_level:
        ET.SubElement(lane, _q("flowNodeRef")).text = node_id

    # messageFlow между двумя пулами внутри одного процесса теряет смысл:
    # роли одной организации общаются sequence-потоками, а межпуловый
    # messageFlow bpmn-js больше не может нарисовать.
    inside_target = {elem_id for elem_id
                     in index.elements
                     if index.process_of.get(elem_id) is target_process}
    removed_messages = []
    for flow in list(index.message_flows):
        if flow.get("sourceRef") in inside_target and flow.get("targetRef") in inside_target:
            index.detach(flow)
            removed_messages.append(flow.get("id") or "")
            touched.add(flow.get("id") or "")

    index.detach(source_process)
    index.detach(source_participant)
    index.merge_touched |= {item for item in touched if item}

    notes = [f"пул «{source_name}» слит в «{target_name}» дорожкой «{lane_name}», "
             f"перенесено узлов: {moved_nodes}, потоков: {len(moved_flows)}"]
    if removed_messages:
        notes.append("messageFlow между пулами удалены: "
                     + ", ".join(removed_messages[:5]))
    starts = [elem_id for elem_id in index.elements
              if _local(index.elements[elem_id].tag) == "startEvent"
              and index.process_of.get(elem_id) is target_process]
    if len(starts) > 1:
        notes.append(f"в пуле «{target_name}» теперь {len(starts)} стартовых событий — "
                     "сведите дорожки в один маршрут операциями connect")
    return notes


def _op_remove_participant(op: Dict[str, Any], index: _Index) -> List[str]:
    """Удалить пул, в котором нет ни одной активности: только старт и финал."""
    participant, process, key = index.resolve_participant(
        op.get("participant") or op.get("id"))
    if not key:
        raise _Skip("не задан пул", "укажите participant именем или id пула из инвентаря")
    if process is None:
        raise _Skip(f"пул '{key}' не найден", "используйте имена участников из инвентаря")
    if participant is None:
        raise _Skip(f"'{key}' не является пулом: процесс не заявлен участником "
                    "в коллаборации",
                    "удалять можно только участников из инвентаря (participants)")
    label = index.participant_name(process)
    nodes = [(elem_id, elem) for elem_id, elem in list(index.elements.items())
             if index.process_of.get(elem_id) is process]
    activities = [elem_id for elem_id, elem in nodes
                  if _local(elem.tag) not in ("startEvent", "endEvent")]
    if activities:
        raise _Skip(
            f"в пуле «{label}» есть шаги ({', '.join(activities[:5])}) — удалять нельзя",
            "слейте пул в дорожку операцией merge_participants или удалите "
            "шаги операцией delete",
        )
    node_ids = {elem_id for elem_id, _ in nodes}
    outside = sorted({flow.get("id") or "" for flow in
                      index.sequence_flows + index.message_flows
                      if {flow.get("sourceRef"), flow.get("targetRef")} & node_ids
                      and not {flow.get("sourceRef"), flow.get("targetRef")} <= node_ids})
    if outside:
        raise _Skip(
            f"пул «{label}» связан с другими пулами потоками {', '.join(outside[:5])}",
            "разорвите связи операцией disconnect либо слейте пул "
            "merge_participants вместо удаления",
        )
    for _, elem in nodes:
        _remove_node(index, elem)
    index.detach(process)
    index.detach(participant)
    return [f"пустой пул «{label}» удалён вместе с процессом"]

_HANDLERS = {
    "add_task": lambda op, i: _op_add_node(op, i, "task"),
    "add_gateway": lambda op, i: _op_add_node(op, i, "gateway"),
    "add_event": lambda op, i: _op_add_node(op, i, "event"),
    "add_participant": _op_add_participant,
    "add_documentation": _op_add_documentation,
    "add_lane": _op_add_lane,
    "move_to_lane": _op_move_to_lane,
    "add_boundary_event": _op_add_boundary_event,
    "rename": _op_rename,
    "delete": _op_delete,
    "connect": _op_connect,
    "set_default": _op_set_default,
    "disconnect": _op_disconnect,
    "move_to_participant": _op_move,
    "merge_participants": _op_merge_participants,
    "remove_participant": _op_remove_participant,
}


def _lane_move_pools(operations: List[Dict[str, Any]],
                     index: _Index) -> Dict[str, Tuple[str, str]]:
    """Дорожка → (пул, элемент) по операциям переноса этой же пачки.

    Модель часто опускает participant у add_lane, оставляя рядом move_to_lane.
    Пул при этом определён однозначно: дорожка обязана принадлежать пулу
    переносимого элемента, иначе перенос будет отвергнут как «дорожка
    принадлежит другому пулу». Это разбор ссылки из плана, а не выдумывание
    структуры — поэтому заметка в отчёте остаётся.
    """
    pools: Dict[str, Tuple[str, str]] = {}
    for op in operations or []:
        if not isinstance(op, dict) or op.get("op") != "move_to_lane":
            continue
        lane_key = str(op.get("lane") or "").strip()
        elem_id = str(op.get("id") or "").strip()
        if not lane_key or lane_key in pools:
            continue
        process = index.process_of.get(elem_id)
        if process is None:
            continue
        participant = index.participant_by_process_id.get(process.get("id") or "")
        label = ((participant.get("name") if participant is not None else None)
                 or process.get("id") or "")
        if label:
            pools[lane_key] = (label, elem_id)
    return pools


ADD_NODE_OPS = {"add_task", "add_event", "add_gateway", "add_boundary_event"}
# Операции, у которых `id`/`name` — создание, а не ссылка: по ним аплайер
# видит, какую зависимость пакет закрывает сам, и откладывает ждущие их правки.
CREATES_ID_OPS = ADD_NODE_OPS | {"add_lane"}
# Проходов достаточно, чтобы раскрыть цепочку «создали шаг → прикрепили
# событие → соединили поток»: каждый проход снимает хотя бы одно звено, а без
# прогресса цикл обрывается сразу.
MAX_PACKAGE_PASSES = 3

# Имя пула из описания разрешается в пул инвентаря, только когда оно близкое:
# ниже этого порога начинается угадывание содержания.
FUZZY_POOL_RATIO = 0.8
_POOL_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _pool_words(text: Any) -> Set[str]:
    """Слова названия пула в нижнем регистре — по ним ищется совпадение."""
    return {w.lower() for w in _POOL_WORD_RE.findall(str(text or ""))}

# Поля, по которым правка опознаётся в отчёте. Они же — ключ сравнения
# «пропуск первого раунда закрыт повтором» в оркестраторе: без них нельзя
# понять, что исправленная версия той же операции прошла.
_OP_IDENTITY_KEYS = ("id", "element_id", "flow", "source", "target", "name",
                     "participant", "gateway")


def _op_identity(op: Dict[str, Any]) -> Dict[str, Any]:
    return {key: op[key] for key in _OP_IDENTITY_KEYS if op.get(key)}


def _routing_gap(index: _Index, elem_id: str) -> Optional[str]:
    """Чем узел односторонне связан с маршрутом: None, если связи есть с обеих
    сторон. Граничное событие хозяина не обязано продолжать — но ветка
    обработки у него быть должна: без неё это кружок, за который скоринг
    снимает балл, и принятое улучшение делает схему хуже исходной."""
    elem = index.elements.get(elem_id)
    if elem is None:
        return None
    tag = _local(elem.tag)
    flows = index.sequence_flows + index.message_flows
    has_in = any(f.get("targetRef") == elem_id for f in flows)
    has_out = any(f.get("sourceRef") == elem_id for f in flows)
    if tag == "boundaryEvent" or elem.get("attachedToRef"):
        return None if has_out else "остался без ветки обработки"
    if tag == "startEvent":
        return None if has_out else "не имеет исходящего потока"
    if tag == "endEvent":
        return None if has_in else "не имеет входящего потока"
    if has_in and has_out:
        return None
    if has_in:
        return "тупик: вход есть, выхода нет"
    if has_out:
        return "недостижим: выход есть, входа нет"
    return "не связан ни с одним элементом"


def _rollback_hint(elem_id: str, gap: str, op_name: str) -> str:
    """Подсказка для откатанного шага — по фактической дыре в маршруте.

    Универсальное «перевставьте с after» здесь врёт: узел, вставленный через
    `after`, вход уже имел, и повтор с той же вставкой даёт тот же тупик —
    корректирующий раунд сжигался впустую. Поэтому подсказка называет
    недостающую дугу, а не способ вставки.
    """
    gone = (f"в исходной схеме '{elem_id}' больше нет: его нужно перевставить "
            "в этом же пакете, и только тогда он доступен для connect")
    if gap == "остался без ветки обработки" or op_name == "add_boundary_event":
        return ("задай ветку обработки той же операцией: шаг-эскалацию добавь "
                f"через add_task, а у add_boundary_event укажи to этого шага "
                f"('{elem_id}' уже откатан, связывать его нельзя)")
    if gap.startswith("тупик"):
        return (f"шаг остался без выхода: перевставьте его с after и задайте "
                f"исход полем to ('{elem_id}' → следующий шаг или конечное "
                f"событие его пула) — {gone}")
    if gap.startswith("недостижим"):
        return (f"шаг остался без входа: перевставьте его с after либо ведите "
                f"connect от существующего шага к '{elem_id}', а исход задайте "
                f"полем to — {gone}")
    if gap == "хозяин события откачен":
        return ("хозяин события откатан вместе с его маршрутом: перевставьте "
                "сначала его, затем граничное событие с attached_to")
    return f"перевставьте шаг операцией с after ({gone})"


def _rollback_unrouted(index: _Index,
                       added: List[str]) -> Tuple[Dict[str, str], Set[str]]:
    """Убрать новые узлы, которые остались вне маршрута.

    Так происходит, когда связи не применились: соседний шаг пакета добавить
    не удалось, и `connect` повис. Оставлять из-за этого в схеме висячий шаг
    нельзя — принятое улучшение сделало бы схему хуже исходной. Потоки
    удаляемого узла созданы этим же пакетом, поэтому исходные шаги маршрут не
    теряют: откат не способен добавить тупик там, где его не было.

    Возвращает id откатанных узлов с причинами и id удалённых вместе с ними
    потоков — по ним отчёт очищается от операций, которых в XML уже нет.
    """
    dropped: Dict[str, str] = {}
    flows: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for elem_id in added:
            if elem_id in dropped:
                continue
            gap = _routing_gap(index, elem_id)
            if gap is None:
                continue
            removed_flows, removed_boundaries = _remove_node(
                index, index.elements[elem_id])
            flows.update(removed_flows)
            dropped[elem_id] = gap
            # Граничное событие уехало вместе с хозяином: его операция в
            # отчёте тоже обязана считаться откаченной, а не «применённой».
            for boundary_id in removed_boundaries:
                dropped[boundary_id] = "хозяин события откачен"
            changed = True
    return dropped, flows


def rollback_stranded(xml_text: str,
                      created: Dict[str, str]) -> Tuple[
                          str, List[Dict[str, Any]]]:
    """Перепроверить узлы пакета по уже отпочиненной схеме.

    `apply_operations` откатывает узлы вне маршрута сам, но семантическая
    починка идёт после аплайера и вправе снять дугу: живой прогон #40 принял
    улучшение, у которого починка убрала исход граничного события, — принятие
    вместо +5 баллов дало −15 на `boundary_handled`. Гарантия «принятое
    улучшение не делает схему хуже исходной» не может действовать только до
    починки, поэтому финальный XML проверяется ещё раз — по тем же правилам и
    с теми же подсказками.
    """
    root = parse_xml(xml_text)
    index = _Index(root)
    dropped, _flows = _rollback_unrouted(index, [
        elem_id for elem_id in created if elem_id in index.elements])
    if not dropped:
        return xml_text, []
    report = [{"id": elem_id, "op": created.get(elem_id) or "add_task",
               "gap": gap, "hint": _rollback_hint(elem_id, gap,
                                                  created.get(elem_id) or "")}
              for elem_id, gap in dropped.items()]
    return _serialize(root), report


def apply_operations(xml_text: str,
                     operations: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Применяет пакет операций. Возвращает (новый XML, отчёт).

    Отчёт: {"status": "success"|"partial",
            "applied": [{"op", "detail"...}], "skipped": [{"op", "reason", "hint"}]}
    """
    root = parse_xml(xml_text)
    index = _Index(root)
    lane_moves = _lane_move_pools(operations, index)
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    added: List[str] = []
    added_ops: Dict[str, str] = {}
    merge_identities: List[Dict[str, Any]] = []
    # id и имена, которые пакет создаёт: операция, ждущая один из них,
    # откладывается до прохода, где зависимость уже есть. Порядок, который
    # модель выбрала для своих операций, не имеет права стоить ветки правок.
    planned = {s for op in (operations or []) if isinstance(op, dict)
               if str(op.get("op") or "") in CREATES_ID_OPS
               for s in (str(op.get("id") or "").strip(),
                         str(op.get("name") or "").strip()) if s}
    queue: List[Tuple[Any, ...]] = [(op, (), None) for op in (operations or [])]
    passes = 0
    while queue and passes < MAX_PACKAGE_PASSES:
        passes += 1
        before = len(applied)
        pending: List[Tuple[Any, ...]] = []
        for op, waited, _last in queue:
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
            derived = ""
            if op_name == "add_lane" and index.resolve_process(op.get("participant")) is None:
                lane_key = str(op.get("id") or "").strip()
                hint = lane_moves.get(lane_key) or lane_moves.get(
                    str(op.get("name") or "").strip())
                if hint:
                    op = {**op, "participant": hint[0]}
                    derived = f"пул '{hint[0]}' взят из элемента '{hint[1]}'"
            try:
                notes = handler(op, index)
                if op_name in ADD_NODE_OPS and op.get("id"):
                    added.append(str(op["id"]))
                    added_ops[str(op["id"])] = op_name
                if op_name == "merge_participants":
                    merge_identities.append(_op_identity(op))
                if derived:
                    notes = notes + [derived]
                if waited:
                    notes = notes + ["операция применена после того, как пакет "
                                     "создал " + ", ".join(f"'{w}'" for w in waited)]
                entry = {"op": op_name, **_op_identity(op)}
                if notes:
                    entry["note"] = "; ".join(notes)
                applied.append(entry)
            except _Skip as skip:
                if (skip.needs and passes < MAX_PACKAGE_PASSES
                        and all(n in planned for n in skip.needs)):
                    pending.append((op, skip.needs,
                                    {"reason": str(skip), "hint": skip.hint}))
                    continue
                skipped.append({"op": op_name, **_op_identity(op),
                                "reason": str(skip), "hint": skip.hint})
            except Exception as e:  # noqa: BLE001 — одна операция не роняет пакет
                logger.exception("Операция %s упала", op_name)
                skipped.append({"op": op_name, **_op_identity(op),
                                "reason": f"внутренняя ошибка: {e}",
                                "hint": "упростите операцию"})
        queue = pending
        if len(applied) == before:
            # Проход ничего не создал: оставшиеся зависимости пакет не
            # закроет, и крутиться по кругу незачем.
            break
    for op, _waited, last in queue:
        # Отложенная операция так и не дождалась зависимости: в отчёт она
        # попадает с той же причиной, что видела модель, — иначе повторный
        # запрос не получит подсказки, что именно в пакете не сходится.
        if not isinstance(op, dict):
            continue
        entry = {"op": op.get("op") or "?", **_op_identity(op)}
        entry.update(last or {"reason": "зависимость пакета не создана",
                              "hint": "создайте элемент раньше, чем сошлётесь "
                                      "на него"})
        skipped.append(entry)
    # Узлы, добавленные пакетом и оставшиеся без маршрута, откатываем:
    # принятое улучшение не имеет права делать схему хуже исходной.
    dropped, dropped_flows = _rollback_unrouted(index, added)
    if dropped:
        applied = [a for a in applied
                   if a.get("id") not in dropped and a.get("source") not in dropped
                   and a.get("target") not in dropped
                   and a.get("flow") not in dropped_flows]
        for elem_id, gap in dropped.items():
            op_name = added_ops.get(elem_id, "add_task")
            hint = _rollback_hint(elem_id, gap, op_name)
            skipped.append({"op": op_name, "id": elem_id,
                            "reason": f"новый шаг ({elem_id}) {gap} — изменение "
                                      f"откачено",
                            "hint": hint})

    # Слияние пулов — единственная правка, которая переносит чужие узлы и потоки
    # в другой процесс. Если из-за переноса починка чистит висящий поток или
    # находит узел вне маршрута, полуслитая схема хуже целой: откатываем весь
    # пакет к XML до применения и честно сообщаем причину.
    if index.merge_touched:
        problems = _merge_breakage(_serialize(root), index.merge_touched)
        if problems:
            # Откатывается весь пакет, поэтому пропуск оформляется по последней
            # слиявшей операции: по её полям оркестратор поймёт, какая правка
            # не закрыта, и не покажет её как успешное изменение.
            for identity in reversed(merge_identities):
                skipped.insert(0, {
                    "op": "merge_participants", **identity,
                    "reason": "слияние пулов сделало схему невалидной: "
                              + "; ".join(problems[:3]),
                    "hint": "сначала встройте шаги пула-источника в маршрут "
                            "операциями after/connect внутри его пула, затем "
                            "повторите merge_participants",
                })
                break
            # Применённые до слияния правки тоже ушли вместе с пакетом: без
            # переноса в skipped пользователь не узнал бы, что добавленный шаг
            # отменён, — в отчёте не осталось бы ни applied, ни skipped.
            for entry in applied:
                rolled = {k: v for k, v in entry.items() if k != "note"}
                rolled["reason"] = ("изменение откачено вместе с пакетом "
                                    "слияния пулов")
                rolled["hint"] = "повторите правку без merge_participants"
                skipped.append(rolled)
            return xml_text, {"status": "failed", "applied": [], "skipped": skipped}

    status = "success" if not skipped else ("partial" if applied else "failed")
    return _serialize(root), {"status": status, "applied": applied, "skipped": skipped}


# ---------------------------------------------------------------------------
# Семантическая починка
# ---------------------------------------------------------------------------

def _ensure_process_event(index: _Index, process: ET.Element, tag: str,
                          counter: int, notes: List[str]) -> int:
    """Добавить процессу стартовое или конечное событие и сразу связать его с
    потоком. Событие без рёбер — висячий узел, поэтому если кандидата нет,
    событие не создаётся вовсе, а причина уходит в notes."""
    is_start = tag == "startEvent"
    event_id = f"{'Start' if is_start else 'End'}Event_new_{counter}"
    label = "стартовое" if is_start else "конечное"
    event = ET.Element(_q(tag), {"id": event_id, "name": "Старт" if is_start else "Завершение"})
    index.adopt(process, event)
    linked = _link_new_event(index, process, event_id, tag)
    if linked is None:
        index.detach(event)
        side = "входящих" if is_start else "исходящих"
        notes.append(
            f"процесс '{index.participant_name(process)}': узел без {side} потоков "
            f"не найден — {label} событие не добавлено"
        )
        return counter
    notes.append(f"добавлено {label} событие {event_id}")
    return counter + 1


# Пометки validate_and_repair о правках, после которых узел всё ещё вне
# маршрута. Планировщик улучшения показывает их пользователю как незаконченную
# работу, а не как успешное изменение.
UNROUTED_NOTE_MARKERS = ("остался без потоков", "не ведёт ни к одному шагу",
                         "— тупик: вход есть", "недостижим: выход есть",
                         "остался без ветки обработки")

# Пул без шагов — не «узел вне маршрута»: его нельзя присоединить connect,
# лечится только add_task/remove_participant. Отдельный список нужен, чтобы
# планировщик получил правильный инструмент, а не бессмысленный совет.
POOL_EMPTY_NOTE_MARKERS = ("остался без шагов",)

# Поломки, из-за которых пакет с `merge_participants` откатывается целиком: те
# же дефекты маршрута плюс следы переноса — висящий поток или ссылка дорожки на
# элемент, который уехал в другой процесс. Нелегальные концы потока и сирота
# attachedToRef сюда НЕ входят: это болезни исходного XML, а не урон от слияния,
# и откатывать из-за них полезное слияние — значит оставить пользователя с
# раздутыми ролями-пулами навсегда.
MERGE_FATAL_NOTE_MARKERS = (UNROUTED_NOTE_MARKERS + POOL_EMPTY_NOTE_MARKERS + (
    "удалён висящий поток", "ссылалась на удалённый элемент",
    "перенесён в другой пул"))


def _merge_breakage(applied_xml: str, touched: Set[str]) -> List[str]:
    """Заметки починки, относящиеся к элементам, перенесённым слиянием пулов.
    Пустой список означает «слияние не сломало маршрут»."""
    _, notes = validate_and_repair(applied_xml)
    return [note for note in notes
            if any(item in note for item in touched)
            and any(marker in note for marker in MERGE_FATAL_NOTE_MARKERS)]


def validate_and_repair(xml_text: str) -> Tuple[str, List[str]]:
    """Детерминированная починка после применения операций: висящие потоки,
    согласованность входящих/исходящих ссылок, дубли потоков, старт/энд на
    каждый процесс."""
    root = parse_xml(xml_text)
    index = _Index(root)
    notes: List[str] = []

    known_ids = set(index.elements.keys())

    # 1. Потоки с несуществующими концами удаляются.
    for flow in list(index.sequence_flows) + list(index.message_flows):
        if flow.get("sourceRef") not in known_ids or flow.get("targetRef") not in known_ids:
            index.detach(flow)
            notes.append(f"удалён висящий поток {flow.get('id')}")

    # 1b. sequenceFlow между разными процессами — тот же дефект, который генератор
    # лечит превращением в сообщение: межпуловая «линия маршрута» на самом деле
    # передача сообщения. Стартовое событие чужого пула — легальный конец
    # сообщения и штатный способ запустить второй пул. Но не для любой пары:
    # шлюз или граничное событие концом messageFlow не бывают, и превращение
    # такой дуги в сообщение схема рисовала, хотя аплайер операций для того же
    # сочетания узлов отказывался её создать (два разных стандарта в одном
    # контуре). Здесь дуга снимается: маршрут по обе стороны от неё починят
    # шаги ниже.
    for flow in list(index.sequence_flows):
        source_process = index.process_of.get(flow.get("sourceRef") or "")
        target_process = index.process_of.get(flow.get("targetRef") or "")
        if source_process is None or target_process is None:
            continue  # концы без пула разберёт шагом выше и пересборкой ссылок
        if source_process is target_process:
            continue
        source_elem = index.elements.get(flow.get("sourceRef") or "")
        target_elem = index.elements.get(flow.get("targetRef") or "")
        if (source_elem is not None and target_elem is not None
                and not _message_leg_ok(_local(source_elem.tag),
                                        _local(target_elem.tag))):
            index.detach(flow)
            notes.append(
                f"поток {flow.get('id')} между пулами удалён: "
                f"{_local(source_elem.tag)} → {_local(target_elem.tag)} не "
                "бывает потоком-сообщением, а sequenceFlow не пересекает "
                "границу процесса")
            continue
        index.detach(flow)
        condition = flow.find(_q("conditionExpression"))
        text = _text_of(condition) if condition is not None else ""
        if condition is not None:
            flow.remove(condition)
        flow.tag = _q("messageFlow")
        if text and not flow.get("name"):
            flow.set("name", text)
        index.adopt(_ensure_collaboration(index), flow)
        notes.append(f"поток {flow.get('id')} между пулами стал потоком-сообщением: "
                     "sequenceFlow не пересекает границу процесса")

    # 1c. Концы sequenceFlow внутри одного процесса: в стартовое и граничное
    # событие поток не входит, из конечного — не выходит. Ни скоринг, ни оракул
    # такого дефекта не видели, и резать нужно здесь: следующим шагом
    # incoming/outgoing пересобираются по фактическим потокам, и нелегальный
    # конец стал бы «валидным».
    for flow in list(index.sequence_flows):
        target = index.elements.get(flow.get("targetRef") or "")
        source = index.elements.get(flow.get("sourceRef") or "")
        if target is not None and _local(target.tag) in SEQUENCE_FORBIDDEN_TARGETS:
            index.detach(flow)
            notes.append(f"удалён недопустимый поток {flow.get('id')}: в "
                         f"'{flow.get('targetRef')}' ({_local(target.tag)}) "
                         "sequence-поток не входит")
            continue
        if source is not None and _local(source.tag) in SEQUENCE_FORBIDDEN_SOURCES:
            index.detach(flow)
            notes.append(f"удалён недопустимый поток {flow.get('id')}: из "
                         f"'{flow.get('sourceRef')}' ({_local(source.tag)}) "
                         "исходящего потока не бывает")

    # 2. Дубли потоков (одинаковые вид/источник/цель) — оставляем первый.
    seen = set()
    for flow in list(index.sequence_flows) + list(index.message_flows):
        key = (_local(flow.tag), flow.get("sourceRef"), flow.get("targetRef"))
        if key in seen:
            index.detach(flow)
            notes.append(f"удалён дубль потока {flow.get('id')}")
        else:
            seen.add(key)

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
        # Порядок тегов BPMN: документация и extensionElements идут до
        # incoming/outgoing, поэтому вставка начинается сразу после них.
        base = _position_after(elem, _REFS_PRECEDING_TAGS)
        for offset, flow_id in enumerate(incoming[elem_id]):
            ref = ET.Element(_q("incoming"))
            ref.text = flow_id
            elem.insert(base + offset, ref)
        shift = base + len(incoming[elem_id])
        for offset, flow_id in enumerate(outgoing[elem_id]):
            ref = ET.Element(_q("outgoing"))
            ref.text = flow_id
            elem.insert(shift + offset, ref)

    # 3b. default шлюза — такая же ссылка на поток, как incoming/outgoing:
    # после disconnect, delete или откатанного пакета она может смотреть в
    # несуществующую ветку либо в чужой поток. bpmn-js на битый IDREF ругается,
    # а скоринг по такому атрибуту перестаёт считать ветку условной.
    for elem_id, elem in index.elements.items():
        default_id = elem.get("default")
        if not default_id or _local(elem.tag) not in GATEWAY_TAGS:
            continue
        flow = index.find_flow(default_id)
        if (flow is None or _local(flow.tag) != "sequenceFlow"
                or flow.get("sourceRef") != elem_id):
            del elem.attrib["default"]
            notes.append(f"шлюз «{elem.get('name') or elem_id}» ({elem_id}) "
                         f"ссылался на недоступный выход по умолчанию "
                         f"{default_id} — атрибут снят")

    # 4. Исключающий шлюз, который ни расщепляет, ни сливает, — не шлюз, а
    # обычный шаг. То же правило, что у генератора
    # (`_demote_single_branch_gateways`): вторую ветку выдумывать нельзя,
    # поэтому шлюз понижается до задачи, а условие с его единственного потока
    # снимается — иначе улучшение, добавившее такой шлюз, роняло бы правило
    # gateway_conditions.
    outgoing_by_source: Dict[str, List[ET.Element]] = {}
    incoming_count: Dict[str, int] = {}
    for flow in index.sequence_flows:
        outgoing_by_source.setdefault(flow.get("sourceRef") or "", []).append(flow)
        target_id = flow.get("targetRef") or ""
        incoming_count[target_id] = incoming_count.get(target_id, 0) + 1
    for elem_id, elem in index.elements.items():
        if _local(elem.tag) != "exclusiveGateway":
            continue
        branches = outgoing_by_source.get(elem_id) or []
        if len(branches) >= 2 or incoming_count.get(elem_id, 0) >= 2:
            # Сходящийся шлюз легален с одним исходящим: понижение съело бы
            # слияния веток, которые генератор вставляет осознанно.
            continue
        elem.tag = _q("task")
        condition = branches[0].find(_q("conditionExpression")) if branches else None
        if condition is not None:
            branches[0].remove(condition)
        had_default = elem.get("default") is not None
        if had_default:
            # У единственной ветки нет «выхода по умолчанию»: выбирать не из чего.
            del elem.attrib["default"]
        notes.append(f"шлюз «{elem.get('name') or elem_id}» ({elem_id}) с "
                     f"единственной веткой понижен до задачи "
                     + ("; условие с потока снято" if condition is not None else "")
                     + ("; выход по умолчанию снят" if had_default else ""))

    # 4a. Единственная безусловная ветка развилки — это «иначе». Генератор
    # помечает её `default` (`_ensure_gateway_default`), и улучшение обязано
    # держать то же правило: `add_gateway` переподвешивает существующий поток под
    # новый шлюз, а он проходит мимо `connect` с его проверкой условия. Двум и
    # более безусловным веткам приёмника не ищем — какую считать запасной, знает
    # только автор правки.
    for elem_id, elem in index.elements.items():
        if _local(elem.tag) != "exclusiveGateway" or elem.get("default"):
            continue
        branches = outgoing_by_source.get(elem_id) or []
        if len(branches) < 2:
            continue
        unconditioned = [f for f in branches
                         if f.find(_q("conditionExpression")) is None]
        if len(unconditioned) != 1:
            continue
        chosen = unconditioned[0]
        elem.set("default", chosen.get("id") or "")
        notes.append(f"ветка {chosen.get('id')} шлюза "
                     f"«{elem.get('name') or elem_id}» ({elem_id}) объявлена "
                     "выходом по умолчанию: она единственная без условия")

    # 5. У каждого процесса — хотя бы один старт и один энд.
    counter = 1
    for process in index.processes:
        tags = [_local(child.tag) for child in process
                if isinstance(child.tag, str)]
        if not any(t in FLOW_NODE_TAGS for t in tags):
            # Пустой пул (add_participant без последующих шагов) скоринг
            # наказывает, а молча добавить в него события — выдумать процесс,
            # которого пользователь не просил. Значит, говорим вслух.
            owner = index.participant_by_process_id.get(process.get("id") or "")
            label = ((owner.get("name") if owner is not None else None)
                     or process.get("name") or process.get("id") or "?")
            notes.append(f"пул «{label}» остался без шагов — добавьте в него "
                         "элементы операциями add_task/add_event")
            continue
        if "startEvent" not in tags:
            counter = _ensure_process_event(index, process, "startEvent", counter, notes)
        if "endEvent" not in tags:
            counter = _ensure_process_event(index, process, "endEvent", counter, notes)

    # 4b. Граничное событие без живого хозяина своего процесса в BPMN не
    # существует: delete и move_to_participant уносят его вместе с задачей, а
    # сюда битый attachedToRef приходит из импортного XML. Определение есть —
    # понижаем до промежуточного события (смысл ожидания сохранён, и узел
    # станет недостижимым — шаг ниже честно об этом скажет), нет — удаляем,
    # потому что пустой кружок ничего не значит и починить его нечем.
    for elem_id, elem in list(index.elements.items()):
        if _local(elem.tag) != "boundaryEvent":
            continue
        host_id = elem.get("attachedToRef") or ""
        host = index.elements.get(host_id) if host_id else None
        same_process = (host is not None and index.process_of.get(elem_id)
                        is index.process_of.get(host_id))
        if host is not None and _local(host.tag) in TASK_TAGS and same_process:
            continue
        if host_id:
            del elem.attrib["attachedToRef"]
        definition = next((_local(child.tag) for child in elem
                           if isinstance(child.tag, str) and is_bpmn_tag(child.tag)
                           and _local(child.tag).endswith("EventDefinition")), "")
        label = f"граничное событие «{elem.get('name') or elem_id}» ({elem_id})"
        if definition:
            elem.tag = _q("intermediateCatchEvent")
            notes.append(f"{label} осталось без хозяина — переведено в "
                         "промежуточное событие маршрута")
        else:
            _remove_node(index, elem)
            notes.append(f"{label} осталось без хозяина и без определения — "
                         "удалено")

    # 5. Узлы вне маршрута. Операция применилась, но включить шаг в маршрут
    # планировщик не предложил (add_task без `after` и без последующего
    # connect), а у граничего события — не описал ветку обработки. Вставить
    # такой узел между шагами нельзя — непонятно, между какими, поэтому это
    # сообщение наверх, а не эвристическая правка.
    for elem_id, elem in index.elements.items():
        tag = _local(elem.tag)
        if tag in ("startEvent", "endEvent"):
            continue
        parent = index.parents.get(elem)
        if parent is None or _local(parent.tag) != "process":
            continue
        incoming = elem.findall(_q("incoming"))
        outgoing = elem.findall(_q("outgoing"))
        if tag == "boundaryEvent" or elem.get("attachedToRef"):
            # Входящего потока у граничного события нет по семантике BPMN: его
            # «запускает» хозяин. Без исходящего оно не делает ничего — и
            # скоринг правомерно считает его тупиком.
            if not outgoing:
                notes.append(
                    f"граничное событие «{elem.get('name') or elem_id}» "
                    f"({elem_id}) не ведёт ни к одному шагу — нужна ветка "
                    "обработки (add_event или add_task и connect от события)"
                )
            continue
        if incoming or outgoing:
            continue
        notes.append(f"узел «{elem.get('name') or elem_id}» ({elem_id}) остался без "
                     "потоков — нужна операция connect или параметр after")

    # 5b. Оборванный маршрут: шаг приняли, но связали только с одной стороны.
    # Для `connect` это частая ловушка — новый узел добавляют «от» существующего
    # и забывают вести дальше, а недостижимый шлюз выглядит валидным в отчёте
    # операций. Чинить эвристикой нельзя (непонятно, к какому именно концу
    # процесса вести), поэтому это сообщение наверх.
    #
    # Вход считается и по потоку-сообщению: узел, который начинает работу по
    # сообщению из чужого пула, достижим, — но `incoming`/`outgoing` таких ссылок
    # не содержат (в них только sequence-потоки), так что коллаборацию смотрим
    # отдельно. Тем же признаком меряет связность `_routing_gap`.
    msg_in = {f.get("targetRef") for f in index.message_flows}
    msg_out = {f.get("sourceRef") for f in index.message_flows}
    for elem_id, elem in index.elements.items():
        tag = _local(elem.tag)
        if tag in ("startEvent", "endEvent") or elem.get("attachedToRef"):
            continue
        parent = index.parents.get(elem)
        if parent is None or _local(parent.tag) != "process":
            continue
        label = f"узел «{elem.get('name') or elem_id}» ({elem_id})"
        has_in = bool(elem.findall(_q("incoming"))) or elem_id in msg_in
        has_out = bool(elem.findall(_q("outgoing"))) or elem_id in msg_out
        if has_in and not has_out:
            notes.append(f"{label} — тупик: вход есть, выхода нет. Нужен connect "
                         "от него к следующему шагу или конечному событию пула")
        elif has_out and not has_in:
            notes.append(f"{label} недостижим: выход есть, входа нет. Нужен connect "
                         "от предыдущего шага или шлюза к нему")

    # 6. Ссылки дорожек. flowNodeRef — это IDREF: после delete он остаётся
    # смотреть на несуществующий элемент, а после move_to_participant — на узел
    # чужого процесса. bpmn-js на битые IDREF ругается, а скоринг такую ссылку
    # считает порядком: убираем ссылающийся не туда узел и пишем об этом в
    # notes, молча не оставляем.
    for lane in index.lanes:
        lane_process = index.enclosing_process(lane)
        for ref in list(lane.findall(_q("flowNodeRef"))):
            target_id = (ref.text or "").strip()
            target = index.elements.get(target_id)
            if target is None:
                lane.remove(ref)
                notes.append(f"дорожка «{lane.get('name') or lane.get('id')}» "
                             f"ссылалась на удалённый элемент {target_id} — "
                             "ссылка убрана")
            elif index.process_of.get(target_id) is not lane_process:
                lane.remove(ref)
                notes.append(f"элемент {target_id} перенесён в другой пул — "
                             f"ссылка убрана из дорожки "
                             f"«{lane.get('name') or lane.get('id')}»")

    return _serialize(root), notes

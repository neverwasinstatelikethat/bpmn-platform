# Детерминированная раскладка (BPMN DI) для сгенерированной схемы.
#
# Зачем она нужна, а не «фронтенд сам разложит» — три измеренных факта:
#
# 1. Поставляемый авто-layout (`bpmn-constructor` → `bpmn-auto-layout-feat-
#    ivan-tulaev/lib/Layouter.js`) не читает `laneSet` вовсе: он не выдаёт ни
#    одной `BPMNShape` для `bpmn:Lane`. Дорожки, которые генератор выверяет
#    правкой плана, при рендере исчезают, а пулы складываются в колонку (x=0,
#    жёлоб 70 px, строка 140 px): схема из четырёх пулов — ~3000 px высотой.
#    Отсюда пользовательское «кривые layout».
# 2. Просмотр по ссылке (`bpmn-constructor/src/SharedDiagram.jsx:69`) зовёт
#    `viewer.importXML` без шага раскладки, а bpmn-js пропускает каждый
#    элемент без DI (`bpmn-js/lib/import/BpmnTreeWalker.js:98`): свежая схема,
#    отправленная ссылкой, — пустой холст.
# 3. Сохранение (`Editor.js:1263`) пишет координаты дословно в
#    `diagrams.xml_content` и `diagram_versions`, поэтому первый рендер
#    замораживает геометрию навсегда.
#
# Координаты, посчитанные генератором, закрывают все три пункта разом и делают
# хранимый XML самоописным: экспорт, превью, мобильный просмотр и diff версий
# читают схему, а не угадывают её геометрию.
#
# Функция чистая: план на входе, элементы DI на выходе — ни обращений к модели,
# ни сети, ни времени, ни случайности. Один и тот же план обязан давать
# побайтово одинаковый XML, иначе diff версий схемы шумел бы координатами.
#
# Пространства имён и словарь тегов — общие для всего приложения: значения и
# префиксы объявлены в `core/bpmn_edits.py` (строки 31-39), их же читает эмиссия
# генератора (`bpmn_generator.py:47-52`). Собственные константы здесь были бы
# второй правдой, а `ET.register_namespace` к тому же мутирует состояние всего
# процесса: префиксы `omgdc`/`omgdi`, выставленные этим модулем при импорте,
# перекрашивали каждый XML, который пишет приложение, и фронтенд терял
# координаты — его регулярка ищет `dc:Bounds`
# (`bpmn-constructor/src/bpmnLayout.js:19`). Поэтому сам модуль ничего не
# регистрирует: импорт `core.bpmn_edits` приносит префиксы `dc`/`di`, уже
# выставленные приложением. Цикла нет — `bpmn_edits` этот модуль не знает, а
# генератор знает оба, поэтому импорт направлен в `bpmn_edits`, а не в
# `bpmn_generator`.
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .bpmn_edits import (BPMNDI_NS, DC_NS, DI_NS, EVENT_TAGS, FLOW_NODE_TAGS,
                      GATEWAY_TAGS, is_bpmn_tag)

# Id, которые эмиссия генератора использует уже (`bpmn_generator.py:2109-2135`):
# раскладка обязана в них уложиться, иначе фронтенд и старые экспорты
# разошлись бы со схемой.
COLLABORATION_ID = "Collaboration_1"
DIAGRAM_ID = "BPMNDiagram_1"
PLANE_ID = "BPMNPlane_1"

# --- размеры узлов ------------------------------------------------------
# Замер по корпусу `core/bpmn_dataset`: все 367 файлов несут DI — 20638
# `BPMNShape` и 4690 `BPMNEdge`; 168 файлов дают фигуру пула (346 штук), 164 —
# фигуру дорожки (438 штук), и все они с `isHorizontal="true"` (770 фигур).
# `BPMNLabel` в корпусе есть только у задач, а bpmn-js при отсутствии
# `di.label` берёт границы самой фигуры (`LabelUtil.getExternalLabelBounds`,
# строка 145), поэтому подписи сюда не пишутся: их координаты — специфика
# Signavio (полоса 12 px, повёрнутая на 90°), а не то, что читает рендерер.
TASK_WIDTH = 100
TASK_HEIGHT = 80
EVENT_SIZE = 36
GATEWAY_SIZE = 50
# События корпуса: медиана 36x36. Шлюзы: `exclusive`/`parallel` — 40x40,
# `inclusive` — 50x50; взято 50, потому что это дефолт bpmn-js, и рамка
# раскладки обязана совпасть с тем, что рисует рендерер, иначе дуга приходила
# бы в границу, которой на экране нет. Задачи — 100x80 (медиана, минимум и
# максимум по `userTask`/`sendTask`/`manualTask` совпадают).

# --- каркас пула и дорожек ----------------------------------------------
# 30 px — полоса имени пула: `lane.x - pool.x` равно ровно 30 в 740 из ~770
# пар «пул — его первая дорожка».
POOL_LABEL_WIDTH = 30
# Дорожки мостят пул без зазоров: в корпусе `next lane.y - (lane.y + lane.h)`
# = 0 (135 из ~250 пар), `pool.y - first lane.y` = 0 (305 файлов),
# `pool.bottom - last lane.bottom` = 0 (170), `pool.right - lane.right` = 0
# (322). Отсюда: высота пула = сумма высот дорожек, ширина дорожки = ширина
# пула за вычетом полосы имени.
LANE_VERTICAL_PADDING = 40
# Медиана высоты дорожки по корпусу 250 px, минимум 70; 120 — годность пустой
# дорожки (ей нужен заголовок) плюс запас для дуг, идущих через её полосу.
LANE_MIN_HEIGHT = 120
# Горизонтальный зазор между соседями одного ряда: мода по корпусу 44 px
# (10955 пар на одной строке), второе значение 40.
NODE_GAP_X = 44
# Зазор между колонками маршрута: шире зазора узлов, потому что по нему идёт
# вертикальное плечо ортогональной дуги.
CHANNEL_WIDTH = 60
LANE_INNER_PADDING_X = 20
POOL_X = 40
# Минимальная ширина пула по корпусу — 500 (медиана 1284, максимум 3055).
POOL_MIN_WIDTH = 500
# Жёлоб между пулами — свободная горизонтальная полоса для дуг между пулами.
# Прежний авто-layout клал пулы с жёлобом 70 и строкой 140, что на четырёх
# пулах и давало ~3000 px высоты.
POOL_GAP_Y = 80
# Вертикаль вне всех пулов: гарантированно свободная полоса для детура.
OUTSIDE_MARGIN_X = 40
EMPTY_ROW_HEIGHT = LANE_MIN_HEIGHT

# Зеркало `bpmn_generator.GATEWAY_KINDS`/`EVENT_KINDS` — те же множества, что
# принимает аплайер (`bpmn_edits.GATEWAY_TAGS`/`EVENT_TAGS`, строки 81-85).
# Свой список здесь был бы вторым словарём: `complexGateway` в нём жил, хотя
# эмиссия такой тип понижает до шага, и рамка раскладки разошлась бы с тем, что
# реально попадает в XML.
EVENT_KINDS = EVENT_TAGS
GATEWAY_KINDS = GATEWAY_TAGS

# --- раскладка граничных событий -----------------------------------------
# Событие сидит центром на границе хозяина, то есть выступает наружу на
# половину своей величины: смещение `g` равно EVENT_SIZE пополам. Иначе восемь
# событий у одного шага 100x80 физически не размещаются — внутри такой рамки
# становится только сетка 2x2, — а наружу фигура выходит ровно на 18 px:
# вертикальный отступ ряда (`LANE_VERTICAL_PADDING` = 40) и горизонтальная
# полоса `LANE_INNER_PADDING_X` = 20 это покрывают, и событие остаётся внутри
# дорожки и пула.
BOUNDARY_STRADDLE_FRACTION = 2
# Минимальная щель на грани хозяина, чтобы поставить на неё якорь дуги:
# стрелка не должна вставать в двух пикселях от соседнего таймера, иначе
# читатель видит касание, а не вход в фигуру. Не хватает щели — дуга уходит на
# другую сторону хозяина, а не теснится в щели.
ANCHOR_MIN_CLEARANCE = 8
# Когда слотов на границе не хватает (у шага 100x80 их восемь), остальные
# события снимаются с хозяина и встают в сетку колонок обычными узлами: наружу
# их толкать нечего — соседний шаг лежит в сорока четырёх пикселях, и фигура
# события легла бы на него. Поле `attached_to` при этом не трогается: это
# вопрос координат, а не семантики.

Point = Tuple[int, int]
# Полоса для дорожек и шагов, чей пул в плане не объявлен: ведущее
# подчёркивание делает это имя неподходящим под id участника, а значит —
# неконфликтующим ни с одним планом. Фигуры эта группа не получает ни какой:
# ссылаться ей не на что — см. `_stray_pool`.
STRAY_POOL = "_без пула"


def _di(local: str) -> str:
    """Тег записи диаграммы.

    Повторяет приватный `core/bpmn_edits._q_di` (строка 46): тащить чужой
    приватный символ через границу модуля нельзя, поэтому форма определена здесь
    один раз и обязана с ним совпадать — префикс `bpmndi` регистрирует именно
    `bpmn_edits`, а расхождение в одной букве дало бы элементы, которых тот же
    аплайер (`DI_ELEMENT_TAGS`) уже не узнаёт.
    """
    return f"{{{BPMNDI_NS}}}{local}"


def diagram(plan: Dict[str, Any], *,
            process_ids: Dict[str, str],
            participant_ids: Dict[str, str],
            collaboration_id: str = COLLABORATION_ID) -> List[ET.Element]:
    """Элементы design-интерчейнджа для `<definitions>` по починенному плану.

    Единственная точка входа: вызывающему не нужно знать ни сетку колонок, ни
    полосу имени пула — он вставляет возвращённые элементы в корень
    `definitions` и получает схему, которую видно без авто-layout.

    `plan` — структура после `repair_structure`: те же словари, что читает
    `_generate_bpmn_xml` (`participants`, `lanes`, `elements`, `flows`).
    `participant_ids` и `process_ids` — отображения «имя пула → id», которые
    эмиссия считает на строках 2043-2045 генератора: фигура пула ссылается на
    `bpmn:Participant`, а `Process_{i}` — запасной адрес для диаграммы без
    `Collaboration` (bpmn-js ссылается на процесс, когда участника нет).

    Гарантии: каждый узел плана получает ровно одну `BPMNShape`, каждая
    дорожка и каждый участник — свою с границами, накрывающими детей, каждый
    поток — `BPMNEdge` с ортогональными точками без нулевых отрезков; ни одна
    дуга не проходит сквозь фигуру, которая не её конец; ни одна фигура не
    ссылается на id, которого в плане нет; фигура дорожки не выходит без фигуры
    накрывающего её пула — висячего IDREF здесь не бывает по построению.

    Ограничение: свобода от пересечений доказывается для формы раскладки,
    которую строит этот модуль (колонки маршрута + ряды-дорожки), а не для
    произвольного BPMN — вложенные подпроцессы и текстовые аннотации сюда не
    входят. План считается починенным: `participant` и `lane` узлов разрешены
    генератором, а концы потоков существуют.
    """
    layout = _Layout(plan, process_ids=process_ids,
                     participant_ids=participant_ids)
    plane = ET.Element(_di("BPMNPlane"),
                       {"id": PLANE_ID, "bpmnElement": collaboration_id})
    for shape in layout.shapes():
        plane.append(shape)
    for edge in layout.edges():
        plane.append(edge)
    node = ET.Element(_di("BPMNDiagram"), {"id": DIAGRAM_ID})
    node.append(plane)
    return [node]


class _Box:
    """Прямоугольник диаграммы; все величины целые, границы считаются
    входящими (иначе накладной тест спорил бы о общем крае)."""

    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def overlaps(self, other: "_Box") -> bool:
        return (self.x < other.right and other.x < self.right
                and self.y < other.bottom and other.y < self.bottom)


class _Node:
    """Узел плана, для которого считается фигура."""

    __slots__ = ("id", "kind", "host", "pool_declared", "lane_declared",
                 "pool", "lane", "box", "row", "attached")

    def __init__(self, node_id: str, kind: str, host: str,
                 pool_declared: str, lane_declared: str,
                 width: int, height: int) -> None:
        self.id = node_id
        self.kind = kind
        self.host = host
        self.pool_declared = pool_declared
        self.lane_declared = lane_declared
        self.pool = pool_declared
        self.lane = ""
        self.box = _Box(0, 0, width, height)
        self.row: Optional[_Row] = None
        # Граничное событие кладётся на границу хозяина, а не в колонку:
        # отдельная ячейка сделала бы из него узел маршрута, которым оно не
        # является, и `attachedToRef` повис бы над пустотой.
        self.attached = False


class _Row:
    """Горизонтальная полоса: дорожка, тело пула без дорожек или чужая зона."""

    __slots__ = ("pool", "lane", "box", "nodes")

    def __init__(self, pool: str, lane: str) -> None:
        self.pool = pool
        self.lane = lane
        self.box = _Box(POOL_X + POOL_LABEL_WIDTH, 0,
                        POOL_MIN_WIDTH - POOL_LABEL_WIDTH, EMPTY_ROW_HEIGHT)
        self.nodes: List[_Node] = []

    @property
    def tallest(self) -> int:
        return max((n.box.h for n in self.nodes), default=EVENT_SIZE)

    @property
    def right(self) -> int:
        return max((n.box.right for n in self.nodes), default=0)

    @property
    def band_above(self) -> int:
        """Свободная полоса над рядом: все узлы центрированы и лежат ниже."""
        return self.box.y + LANE_VERTICAL_PADDING // 2

    @property
    def band_below(self) -> int:
        return self.box.bottom - LANE_VERTICAL_PADDING // 2


class _Pool:
    """Участник схемы: его фигура и ряды, которые её наполняют."""

    __slots__ = ("name", "ref", "rows", "box")

    def __init__(self, name: str, ref: str) -> None:
        self.name = name
        self.ref = ref
        self.rows: List[_Row] = []
        self.box = _Box(POOL_X, 0, POOL_MIN_WIDTH, EMPTY_ROW_HEIGHT)


class _Layout:
    """Сетка колонок по маршруту плюс ряды-дорожки; состояние раскладки."""

    def __init__(self, plan: Dict[str, Any], *,
                 process_ids: Dict[str, str],
                 participant_ids: Dict[str, str]) -> None:
        raw = plan if isinstance(plan, dict) else {}
        self.pools: List[_Pool] = []
        self.nodes: List[_Node] = []
        self.by_id: Dict[str, _Node] = {}
        self.lane_rows: Dict[str, _Row] = {}
        self.body_rows: Dict[str, _Row] = {}
        self.flows: List[Dict[str, Any]] = []
        self.channels: List[int] = []
        self.order: List[str] = []
        self.depth: Dict[str, int] = {}
        # «Узел → (хозяин, смещение по x, смещение по y)» для пристёгнутых
        # граничных событий: слот выбирается до сетки колонок, координата —
        # после неё.
        self.boundary: Dict[str, Tuple[str, int, int]] = {}
        # «Хозяин → рамки пристёгнутых к нему событий»: те накрывают границу
        # хозяина и потому отнимают у дуг центр грани — см. `_anchor`.
        self.seats: Dict[str, List[_Box]] = {}
        self._process_ids = dict(process_ids or {})
        self._participant_ids = dict(participant_ids or {})
        self._build(raw)

    # --- разбор плана ---------------------------------------------------

    def _build(self, raw: Dict[str, Any]) -> None:
        by_name: Dict[str, _Pool] = {}
        for name in _pool_names(raw):
            ref = (self._participant_ids.get(name)
                   or self._process_ids.get(name) or "")
            pool = _Pool(name, ref)
            self.pools.append(pool)
            by_name[name] = pool
        # Порядок пулов — порядок плана: раскладка не имеет права решать за
        # модель, кто выше на листе.
        for lane in _dict_list(raw.get("lanes")):
            lane_id = _text(lane.get("id"))
            if not lane_id or lane_id in self.lane_rows:
                continue
            pool = by_name.get(_text(lane.get("participant")))
            if pool is None:
                pool = self._stray_pool(by_name)
            row = _Row(pool.name, lane_id)
            pool.rows.append(row)
            self.lane_rows[lane_id] = row

        for item in _dict_list(raw.get("elements")):
            node_id = _text(item.get("id"))
            if not node_id or node_id in self.by_id:
                continue
            kind = _text(item.get("kind")) or "task"
            width, height = _size(kind)
            node = _Node(node_id, kind, _text(item.get("attached_to")),
                         _text(item.get("participant")),
                         _text(item.get("lane")), width, height)
            self.nodes.append(node)
            self.by_id[node_id] = node

        self.flows = [dict(flow) for flow in _dict_list(raw.get("flows"))
                      if _text(flow.get("id"))]
        self._assign_rows(by_name)
        self._depths()
        self._plan_boundaries()
        self._grid()
        self._place_boundaries()

    def _stray_pool(self, by_name: Dict[str, _Pool]) -> _Pool:
        """Полоса для дорожек и шагов без объявленного пула.

        Фигуру участника здесь не выпускаем — в плане его нет, а сослаться на
        несуществующий id контракт запрещает (`bpmnElement` — IDREF, и
        `bpmn_edits` при удалении элемента снимает его графику именно поэтому).
        Генератор кладёт каждый узел в дорожку объявленного пула, так что это
        защитный путь, а не основной. Раз фигуры пула нет, то и фигуры дорожек
        этой группы не выпускаются: `BPMNShape` дорожки без накрывающей её
        фигуры пула — ровно тот висячий IDREF, из-за которого bpmn-js теряет
        дорожку целиком. Узлы при этом обязаны остаться на схеме: потерянный
        шаг читался бы как потерянный участник, а накрывающая рамка группы
        продолжает их держать — два шага одного ряда не лягут друг на друга.
        """
        pool = by_name.get(STRAY_POOL)
        if pool is None:
            pool = _Pool(STRAY_POOL, "")
            self.pools.append(pool)
            by_name[STRAY_POOL] = pool
        return pool

    def _assign_rows(self, by_name: Dict[str, _Pool]) -> None:
        """Узел привязан к ряду; дорожка задаёт пул, а не поле `participant`.

        Если `lane` объявлена в другом пуле, ряд берётся по дорожке: фигура
        обязана лежать внутри своей дорожки, и доверие полю здесь построило
        бы DI, который нарушает контракт накрытия.
        """
        for node in self.nodes:
            row = self.lane_rows.get(node.lane_declared)
            if row is not None:
                node.pool = row.pool
                node.lane = row.lane
                row.nodes.append(node)
                node.row = row
                continue
            pool = by_name.get(node.pool_declared) or self._stray_pool(by_name)
            node.pool = pool.name
            node.lane = ""
            body = self.body_rows.get(pool.name)
            if body is None:
                body = _Row(pool.name, "")
                pool.rows.append(body)
                self.body_rows[pool.name] = body
            body.nodes.append(node)
            node.row = body

    # --- порядок обхода и сетка колонок --------------------------------

    def _depths(self) -> None:
        """Колонка = дистанция от старта, чтобы маршрут читался налево-направо.

        Граничное событие будит не поток, а хозяин, поэтому для него добавлена
        синтетическая дуга `host → event`: иначе обработчик срока уехал бы в
        «недостижимые» и встал правее всего процесса, а дуга к нему пересекла
        бы половину схемы.
        """
        adjacency: Dict[str, List[str]] = {}
        for flow in self.flows:
            if _text(flow.get("kind")) != "sequence":
                continue
            source = _text(flow.get("source"))
            target = _text(flow.get("target"))
            if source in self.by_id and target in self.by_id:
                adjacency.setdefault(source, []).append(target)
        for node in self.nodes:
            if node.kind != "boundaryEvent":
                continue
            host = self.by_id.get(node.host) if node.host else None
            if host is None or host is node:
                continue
            # Хозяин, который сам сидит на чужой границе, — узел не сетки:
            # координаты события зависели бы от координат другого события, и их
            # разбор зависел бы от порядка обхода. Нотации это не соответствует
            # (аплайер принимает хозяином только шаг), поэтому такое событие
            # остаётся обычным узлом своей колонки.
            adjacency.setdefault(host.id, []).append(node.id)
            if host.kind != "boundaryEvent":
                node.attached = True

        depth: Dict[str, int] = {}
        order: List[str] = []
        queue = [node.id for node in self.nodes if node.kind == "startEvent"]
        for seed in queue:
            depth[seed] = 0
        head = 0
        while head < len(queue):
            current = queue[head]
            head += 1
            order.append(current)
            for nxt in adjacency.get(current, []):
                if nxt in depth:
                    continue
                depth[nxt] = depth[current] + 1
                queue.append(nxt)
        # Недостижимые из старта (потерянная дуга, цикл без входа) дописываются
        # после всех колонок в порядке плана: они не сдвигают достижимый
        # маршрут и не исчезают со схемы.
        tail = max(depth.values(), default=-1) + 1
        for offset, node in enumerate(n for n in self.nodes
                                      if n.id not in depth):
            depth[node.id] = tail + offset
            order.append(node.id)
        self.order = order
        self.depth = depth

    def _grid(self) -> None:
        """Позиции узлов: колонки по маршруту, ряды по дорожкам."""
        cells: Dict[int, List[_Node]] = {}
        for node_id in self.order:
            node = self.by_id[node_id]
            if node.attached:
                continue
            cells.setdefault(self.depth[node_id], []).append(node)
        columns = sorted(cells)
        content_left = POOL_X + POOL_LABEL_WIDTH + LANE_INNER_PADDING_X

        col_x: Dict[int, int] = {}
        col_w: Dict[int, int] = {}
        cursor = content_left
        for column in columns:
            used: Dict[int, int] = {}
            widest = TASK_WIDTH
            for node in cells[column]:
                key = id(node.row)
                offset = used.get(key, 0)
                widest = max(widest, offset + node.box.w)
                used[key] = offset + node.box.w + NODE_GAP_X
            col_x[column] = cursor
            col_w[column] = widest
            cursor += widest + CHANNEL_WIDTH

        for column in columns:
            used = {}
            for node in cells[column]:
                key = id(node.row)
                offset = used.get(key, 0)
                node.box.x = col_x[column] + offset
                used[key] = offset + node.box.w + NODE_GAP_X

        # Каналы — вертикальные полосы, в которых узлов не лежит по построению:
        # левая (внутри поля дорожки до первой колонки) и по одной после
        # каждой колонки. По ним и ходит вертикальное плечо дуг.
        self.channels = [content_left - LANE_INNER_PADDING_X // 2]
        self.channels.extend(col_x[column] + col_w[column]
                             + CHANNEL_WIDTH // 2 for column in columns)

        # Высоты рядов, потом — координаты: высота пула равна сумме высот
        # его рядов (замер корпуса: зазор между дорожками 0, низ пула = низ
        # последней дорожки).
        total_y = 0
        for pool in self.pools:
            offset = 0
            for row in pool.rows:
                row.box.h = max(LANE_MIN_HEIGHT,
                                2 * LANE_VERTICAL_PADDING + row.tallest)
                row.box.y = total_y + offset
                offset += row.box.h
            pool.box.y = total_y
            pool.box.h = max(offset, EMPTY_ROW_HEIGHT)
            own_right = max((row.right for row in pool.rows), default=0)
            pool.box.w = max(POOL_MIN_WIDTH,
                             own_right + LANE_INNER_PADDING_X - POOL_X)
            lane_width = max(1, pool.box.w - POOL_LABEL_WIDTH)
            for row in pool.rows:
                row.box.x = pool.box.x + POOL_LABEL_WIDTH
                row.box.w = lane_width
            total_y += pool.box.h + POOL_GAP_Y

        for pool in self.pools:
            for row in pool.rows:
                center = row.box.y + row.box.h // 2
                for node in row.nodes:
                    node.box.y = center - node.box.h // 2

    def _plan_boundaries(self) -> None:
        """Слоты граничных событий на границе хозяина — до сетки колонок.

        Разбор живёт здесь, а не после `_grid`, по двум причинам. Первая:
        решение «хватило ли места на границе» зависит только от размеров
        хозяина и события (они известны с `_size`), а тест на пересечение
        инвариантен к сдвигу — координаты хозяина для него не нужны. Вторая:
        события, которым места не хватило, обязаны попасть в сетку колонок
        обычными узлами, а сетка строится один раз и по своим же правилам
        вычисляет ширину пула; оставить такой узел без координат значило бы
        выпустить фигуру в (0, 0) поверх старта схемы.

        Порядок кандидатов фиксирован (`_border_boxes`), поэтому один и тот же
        план отдаёт событию тот же слот от вызова к вызову, а фигуры одного
        хозяина не пересекаются: слот занят — берётся следующий, а не тот же.
        Флаг `isBoundaryElement` не пишется: в корпусе его нет ни у одной из
        20638 фигур, а bpmn-js выводит граничность из `attachedToRef`.
        """
        grouped: Dict[str, List[_Node]] = {}
        for node in self.nodes:
            if node.attached:
                grouped.setdefault(node.host, []).append(node)
        for host_id, events in grouped.items():
            host = self.by_id[host_id]
            # Виртуальный хозяин в нулевых координатах: слоты считаются как
            # смещения, а окончательные координаты к ним добавит `_grid`.
            area = _Box(0, 0, host.box.w, host.box.h)
            taken: List[_Box] = []
            for event in events:
                for candidate in _border_boxes(area, event.box.w, event.box.h):
                    if not any(candidate.overlaps(seat) for seat in taken):
                        taken.append(candidate)
                        self.boundary[event.id] = (host_id, candidate.x,
                                                   candidate.y)
                        break
                else:
                    # Свободного слота на границе не осталось: событие —
                    # обычный узел своей колонки, а не вторая фигура в чужой
                    # точке и не рамка, легшая на соседний шаг.
                    event.attached = False

    def _place_boundaries(self) -> None:
        """Окончательные координаты пристёгнутых событий: слот плюс хозяин.

        К этому моменту хозяин уже стоит в своей колонке и ряду, поэтому
        событие сдвигается к его границе на смещение, выбранное до сетки.
        Дорожка и ряд берутся у хозяина: `attachedToRef` не пересекает пул, и
        рамка события обязана лежать внутри тех же границ, что и рамка шага,
        который его будит.
        """
        for node_id, (host_id, dx, dy) in self.boundary.items():
            node = self.by_id[node_id]
            host = self.by_id[host_id]
            node.box.x = host.box.x + dx
            node.box.y = host.box.y + dy
            node.lane = host.lane
            node.row = host.row
            self.seats.setdefault(host_id, []).append(node.box)

    # --- вывод DI -------------------------------------------------------

    def shapes(self) -> List[ET.Element]:
        """Пулы в порядке плана, дорожки в порядке плана, узлы — по маршруту.

        Список плоский: так делают все 367 файлов корпуса (20638 `BPMNShape`
        — дети `BPMNPlane`, ни одной вложенной), и bpmn-js импортирует фигуры
        обходом семантического дерева, а не обхода DI.

        Фигура дорожки выпускается только вместе с фигурой накрывающего её пула
        (см. `_stray_pool`): без неё `bpmnElement` дорожки остался бы IDREF без
        родителя. Узлы группы при этом остаются на схеме.
        """
        out: List[ET.Element] = []
        for pool in self.pools:
            if not pool.ref:
                continue
            out.append(_shape(pool.ref, pool.box, horizontal=True))
            for row in pool.rows:
                if row.lane:
                    out.append(_shape(row.lane, row.box, horizontal=True))
        for node_id in self.order:
            out.append(_shape(node_id, self.by_id[node_id].box))
        return out

    def edges(self) -> List[ET.Element]:
        """Дуги в порядке плана: поток без конца в плане не получает DI."""
        out: List[ET.Element] = []
        participant_ends: Dict[str, _Node] = {}

        def endpoint(ref: str, message: bool) -> Optional[_Node]:
            node = self.by_id.get(ref)
            if node is not None or not message:
                return node
            pool = next((p for p in self.pools if p.name == ref and p.ref), None)
            if pool is None:
                return None
            if ref not in participant_ends:
                end = _Node(pool.ref, "participant", "", pool.name, "",
                            pool.box.w, pool.box.h)
                end.box = pool.box
                if pool.rows:
                    end.row = pool.rows[0]
                else:
                    row = _Row(pool.name, "")
                    row.box = pool.box
                    end.row = row
                participant_ends[ref] = end
            return participant_ends[ref]

        for flow in self.flows:
            message = _text(flow.get("kind")) == "message"
            source = endpoint(_text(flow.get("source")), message)
            target = endpoint(_text(flow.get("target")), message)
            if source is None or target is None or source is target:
                continue
            if source.row is None or target.row is None:
                continue
            points = _clean_points(self.route(source, target))
            if len(points) < 2:
                # Путь сложился в одну точку: дуга без нулевого отрезка, иначе
                # bpmn-js не рисует ни стрелки, ни подписи перехода.
                points = _fallback_points(source.box, target.box)
            out.append(_edge(_text(flow.get("id")), points))
        return out

    # --- маршрутизация дуг ----------------------------------------------

    def route(self, source: _Node, target: _Node) -> List[Point]:
        """Ортогональный путь без прохождений сквозь чужие фигуры и без нулевых
        отрезков.

        Кандидаты перебираются в фиксированном порядке (прямая → угол → зигзаг
        по каналу → детур по свободным полосам → полоса вне листов), берётся
        первый свободный. Порядок фиксирован, иначе одна и та же схема получала
        бы разные координаты от вызова к вызову. Каждый кандидат очищается
        `_clean_points` до проверки: путь с нулевым отрезком и без него — одна и
        та же геометрия, но кандидат из двух одинаковых точек после очистки стал
        бы точкой, и брать его нельзя.

        Свободного кандидата может и не быть: грань хозяина облеплена событиями
        со всех сторон. Тогда наружу идёт путь с наименьшим числом задетых фигур,
        а не последний по списку — см. `_path_collisions`.
        """
        blocked = self._obstacles(source, target)
        candidates = self._candidates(source, target)
        best: Optional[List[Point]] = None
        best_hits = 0
        for candidate in candidates:
            points = _clean_points(candidate)
            if len(points) < 2:
                continue
            hits = _path_collisions(points, blocked)
            if not hits:
                return points
            # Ни один кандидат не свободен: берём тот, что задевает меньше
            # фигур, а не последний по списку. Прежняя заплатка возвращала
            # «детур вне всех пулов» непроверенным, утверждая, что его плечо
            # свободно по построению, — полоса ряда действительно свободна, но
            # вертикальное плечо входит в цель по центру её грани, а центр
            # грани держит слот граничного события. Проход по всем кандидатам
            # стоит ровно там, где и раньше перебирали до свободного: первый
            # свободный уходит сразу.
            if best is None or hits < best_hits:
                best, best_hits = points, hits
        if best is not None:
            return best
        # См. `edges`: дуга обязана нести хотя бы две разные точки, иначе
        # рендер не рисует ни стрелку, ни метку перехода.
        return _fallback_points(source.box, target.box)

    def _anchor(self, node: _Node, side: str) -> int:
        """Точка на грани фигуры, куда встаёт конец дуги.

        Центр грани — единственное, что раскладка умела выбирать, и он же первое,
        что отнимает граничное событие: слот сидит центром на стороне хозяина и
        накрывает её середину. Пятое-шестое событие у одного шага закрывает центр
        и вертикального входа, и горизонтального, и тогда любой путь по центру
        идёт сквозь чужой таймер. Ответ — середина самой широкой свободной части
        той же грани (при равных размерах — левой), а если вся грань занята или
        щель уже `ANCHOR_MIN_CLEARANCE` — прежний центр: проверка кандидатов
        сама отбросит такой кандидат, и дуга уйдёт на другую сторону фигуры.

        Фигуры без пристёгнутых событий (то есть почти весь корпус) получают
        ровно тот же центр, что и раньше: раскладка существующих схем не
        сдвигается.
        """
        box = node.box
        vertical = side in ("top", "bottom")
        center = box.cx if vertical else box.cy
        lo, hi = (box.x, box.right) if vertical else (box.y, box.bottom)
        spans = self._seat_spans(node, side)
        if not any(start <= center <= end for start, end in spans):
            return center
        gaps: List[Tuple[int, int]] = []
        cursor = lo
        for start, end in sorted(spans):
            if start > cursor:
                gaps.append((cursor, min(start, hi)))
            cursor = max(cursor, end)
        if cursor < hi:
            gaps.append((cursor, hi))
        gaps = [gap for gap in gaps if gap[1] - gap[0] >= ANCHOR_MIN_CLEARANCE]
        if not gaps:
            return center
        start, end = max(gaps, key=lambda gap: (gap[1] - gap[0], -gap[0]))
        return (start + end) // 2

    def _seat_spans(self, node: _Node, side: str) -> List[Tuple[int, int]]:
        """Интервалы грани, накрытые слотами событий этого хозяина.

        Слот относится к стороне, если выступает за неё: рамка события
        пересекает границу хозяина, а не лежит внутри него. Угловой слот накрывает
        сразу две стороны — и верх, и лево, — поэтому он в списке у обеих.

        Узел берётся, а не его id: конец дуги бывает и виртуальной фигурой
        участника целиком (`edges`), которой в `seats` нет по построению.
        """
        box = node.box
        out = []
        for seat in self.seats.get(node.id, []):
            if side == "top" and seat.y < box.y:
                out.append((seat.x, seat.right))
            elif side == "bottom" and seat.bottom > box.bottom:
                out.append((seat.x, seat.right))
            elif side == "left" and seat.x < box.x:
                out.append((seat.y, seat.bottom))
            elif side == "right" and seat.right > box.right:
                out.append((seat.y, seat.bottom))
        return out

    def _obstacles(self, source: _Node, target: _Node) -> List[_Box]:
        blocked = []
        allowed = {id(source.box), id(target.box)}
        # Дуга с граничного события обязана пересечь границу хозяина: это
        # форма BPMN, а не дефект раскладки, и хозяин тоже исключается.
        for node in (source, target):
            if node.attached:
                host = self.by_id.get(node.host)
                if host is not None:
                    allowed.add(id(host.box))
        for node in self.nodes:
            if id(node.box) in allowed:
                continue
            blocked.append(node.box)
        return blocked

    def _candidates(self, source: _Node, target: _Node) -> List[List[Point]]:
        """Кандидаты в фиксированном порядке; концы ложатся на якоря граней.

        Порядок не зависит от координат и от числа событий, поэтому один и тот же
        план отдаёт одну и ту же раскладку от вызова к вызову.
        """
        out: List[List[Point]] = []
        area_s, area_t = source.box, target.box
        # Якоря ровно тех сторон, которые ниже используются: центр грани, а если
        # его накрывает слот граничного события — свободная часть той же грани.
        s_right, s_left = (self._anchor(source, "right"),
                           self._anchor(source, "left"))
        t_right, t_left = (self._anchor(target, "right"),
                           self._anchor(target, "left"))
        s_top, s_bottom = (self._anchor(source, "top"),
                           self._anchor(source, "bottom"))
        t_top, t_bottom = (self._anchor(target, "top"),
                           self._anchor(target, "bottom"))
        if area_s.cy == area_t.cy:
            if area_t.x >= area_s.right and s_right == t_left:
                out.append([(area_s.right, s_right), (area_t.x, t_left)])
            elif area_s.x >= area_t.right and s_left == t_right:
                out.append([(area_s.x, s_left), (area_t.right, t_right)])
        if area_s.cx == area_t.cx:
            if area_t.y >= area_s.bottom and s_bottom == t_top:
                out.append([(area_s.cx, area_s.bottom), (area_t.cx, area_t.y)])
            elif area_s.y >= area_t.bottom and s_top == t_bottom:
                out.append([(area_s.cx, area_s.y), (area_t.cx, area_t.bottom)])
        # Развилка «угол»: горизонталь от своей стороны, вертикаль в грань цели.
        # Сторона входа и её якорь берутся одной парой: вход через верх — значит
        # и x берётся с верхней грани, а не с нижней.
        enter_top = area_t.cy < area_s.cy
        enter_x = t_top if enter_top else t_bottom
        enter_y = area_t.y if enter_top else area_t.bottom
        if area_t.cx >= area_s.cx:
            out.append([(area_s.right, s_right), (enter_x, s_right),
                        (enter_x, enter_y)])
        else:
            out.append([(area_s.x, s_left), (enter_x, s_left),
                        (enter_x, enter_y)])
        channels = self._channels_near(source, target)
        for channel in channels:
            if area_t.x >= area_s.right:
                out.append([(area_s.right, s_right), (channel, s_right),
                            (channel, t_left), (area_t.x, t_left)])
            else:
                out.append([(area_s.x, s_left), (channel, s_left),
                            (channel, t_right), (area_t.right, t_right)])
        # Детур по свободным полосам: вверх (или вниз) в полосу своего ряда,
        # вертикаль по каналу, вход в цель тоже через полосу её ряда. Полоса
        # ряда свободна по построению — все узлы ряда центрированы и лежат
        # между полосами, — и канал свободен: узлов в зазоре колонки нет.
        for channel in channels:
            out.append(_detour(s_top, area_s.y, t_top, area_t.y,
                               source.row.band_above, target.row.band_above,
                               channel))
            out.append(_detour(s_bottom, area_s.bottom, t_bottom,
                               area_t.bottom, source.row.band_below,
                               target.row.band_below, channel))
        for channel in (POOL_X - OUTSIDE_MARGIN_X,
                        self.diagram_right() + OUTSIDE_MARGIN_X):
            out.append(_detour(s_top, area_s.y, t_top, area_t.y,
                               source.row.band_above, target.row.band_above,
                               channel))
        return out

    def _channels_near(self, source: _Node, target: _Node) -> List[int]:
        """Каналы между концами дуги, ближний к цели раньше.

        Дальние каналы в список не попадают: горизонтальное плечо до них
        шло бы поверх промежуточных колонок, то есть сквозь фигуры, и проверка
        просто отбросила бы кандидат — лишняя работа на каждом прогоне.
        """
        first = min(source.box.cx, target.box.cx)
        last = max(source.box.cx, target.box.cx)
        near = [channel for channel in self.channels if first <= channel <= last]
        near.sort(key=lambda c: (abs(c - target.box.cx), c))
        return near[:3]

    def diagram_right(self) -> int:
        return max((pool.box.right for pool in self.pools), default=POOL_X)


def _detour(exit_x: int, exit_y: int, enter_x: int, enter_y: int,
            band_s: int, band_t: int, channel: int) -> List[Point]:
    """Шесть точек: выход к полосе своего ряда, канал, полоса ряда цели.

    Точки могут совпасть: у дуги внутри одного ряда `band_s == band_t`, и
    вертикальное плечо в канале вырождается. Это убирает `_clean_points` перед
    выводом, а не здесь: список кандидатов остаётся однотипным, и порядок
    перебора не зависит от геометрии ряда.

    Якоря (`exit_x`/`enter_x`) приходят из `_anchor`, а не берутся из центра
    рамки: детур входит в цель по вертикали через её верхнюю или нижнюю грань,
    и ровно там стоит слот граничного события.
    """
    return [(exit_x, exit_y), (exit_x, band_s), (channel, band_s),
            (channel, band_t), (enter_x, band_t), (enter_x, enter_y)]


def _border_boxes(area: _Box, w: int, h: int) -> Iterable[_Box]:
    """Слоты рамки граничного события на границе хозяина, в фиксированном
    порядке.

    Событие лежит центром на стороне хозяина и выступает наружу на `g` (см.
    `BOUNDARY_STRADDLE_FRACTION`): иначе внутри шага 100x80 помещается только
    сетка 2x2, и пятое событие встало бы в точку первого. Стороны обходятся в
    одном порядке (низ, верх, лево, право), а на каждой — слоты слева направо и
    сверху вниз с шагом размера события; цепочка слотов отцентрована на хозяине.
    Когда свободных кандидатов на рамке не остаётся, событие уходит в сетку
    колонок (см. `_plan_boundaries`) — иначе соседний шаг остался бы без места.

    Порядок не зависит от координат хозяина (он считается в нулевых
    координатах), поэтому выбор слота детерминирован и сверяется до сетки
    колонок — см. `_plan_boundaries`.
    """
    g = (w + h) // (2 * BOUNDARY_STRADDLE_FRACTION)
    n_x = (area.w - w + 2 * g) // w + 1
    n_y = (area.h - h + 2 * g) // h + 1
    first_x = (area.x + area.right - n_x * w) // 2
    first_y = (area.y + area.bottom - n_y * h) // 2
    for slot in range(max(n_x, n_y)):
        if slot < n_x:
            yield _Box(first_x + slot * w, area.bottom - g, w, h)
            yield _Box(first_x + slot * w, area.y - g, w, h)
        if slot < n_y:
            yield _Box(area.x - g, first_y + slot * h, w, h)
            yield _Box(area.right - g, first_y + slot * h, w, h)


def _clean_points(points: Sequence[Point]) -> List[Point]:
    """Путь без нулевых отрезков: соседние одинаковые точки убираются, первая
    и последняя остаются якорями на границах своих фигур.

    Дубликат рождается не из фантазии: в зигзаге по каналу горизонтальное плечо
    совпадает с выходом, когда канал проходит ровно по краю колонки
    (`col_x + col_w + CHANNEL_WIDTH // 2` при колонке из одного шага), а в
    детуре два плеча сливаются, когда источник и цель стоят в одном ряду
    (`band_s == band_t`). bpmn-js рисует такой отрезок вырожденным сегментом:
    стрелки на нём нет, метка перехода не за что не ложится, а длина дуги
    читается как ноль.
    """
    out: List[Point] = []
    for point in points:
        if not out or point != out[-1]:
            out.append(point)
    return out


def _fallback_points(area_s: _Box, area_t: _Box) -> List[Point]:
    """Две разные точки на границах концов, когда путь сложился в одну.

    Пара «выход → вход» перебирается в фиксированном порядке по четырём
    направлениям: если концы стоят вплотную друг к другу по одному из них,
    берётся следующее. Центр в центр — последний довод: он различен у любых двух
    несовпавших фигур, а совпавшие раскладка не выпускает.
    """
    pairs = (
        ((area_s.right, area_s.cy), (area_t.x, area_t.cy)),
        ((area_s.cx, area_s.y), (area_t.cx, area_t.bottom)),
        ((area_s.x, area_s.cy), (area_t.right, area_t.cy)),
        ((area_s.cx, area_s.bottom), (area_t.cx, area_t.y)),
    )
    for first, last in pairs:
        if first != last:
            return [first, last]
    return [(area_s.cx, area_s.cy), (area_t.cx, area_t.cy)]


def _size(kind: str) -> Tuple[int, int]:
    if kind in EVENT_KINDS:
        return (EVENT_SIZE, EVENT_SIZE)
    if kind in GATEWAY_KINDS:
        return (GATEWAY_SIZE, GATEWAY_SIZE)
    return (TASK_WIDTH, TASK_HEIGHT)


def _shape(ref: str, box: _Box, *, horizontal: bool = False) -> ET.Element:
    attrs = {"id": f"BPMNShape_{ref}", "bpmnElement": ref}
    if horizontal:
        attrs["isHorizontal"] = "true"
    shape = ET.Element(_di("BPMNShape"), attrs)
    ET.SubElement(shape, f"{{{DC_NS}}}Bounds", {
        "x": str(box.x), "y": str(box.y),
        "width": str(box.w), "height": str(box.h)})
    return shape


def _edge(ref: str, points: Sequence[Point]) -> ET.Element:
    edge = ET.Element(_di("BPMNEdge"),
                      {"id": f"BPMNEdge_{ref}", "bpmnElement": ref})
    for x, y in _clean_points(points):
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint",
                      {"x": str(x), "y": str(y)})
    return edge


def _path_collisions(points: Sequence[Point], boxes: Iterable[_Box]) -> int:
    """Сколько чужих фигур путь задевает; ноль — путь свободен.

    Считается, а не сводится к «есть ли касание», по одной причине: когда ни
    один кандидат не свободен, выбор «меньшее зло» должен быть измерим, а не
    определён порядком списка. Фильтр по огибающему прямоугольнику пути — без
    него каждый кандидат обходил бы все узлы схемы.
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    frame = _Box(min(xs), min(ys), 0, 0)
    frame.w = max(xs) - frame.x
    frame.h = max(ys) - frame.y
    hits = 0
    for box in boxes:
        if not frame.overlaps(box) and not _touches(frame, box):
            continue
        if _path_hits(points, box):
            hits += 1
    return hits


def _touches(frame: _Box, box: _Box) -> bool:
    """Огибающая вырождена в отрезок: `overlaps` её не ловит, а дуга по
    границе фигуры — прохождение."""
    return (frame.x <= box.right and box.x <= frame.right
            and frame.y <= box.bottom and box.y <= frame.bottom)


def _path_hits(points: Sequence[Point], box: _Box) -> bool:
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if _segment_hits_box(x1, y1, x2, y2, box):
            return True
    return False


def _segment_hits_box(x1: int, y1: int, x2: int, y2: int, box: _Box,
                      eps: int = 1) -> bool:
    """Пересечение отрезка с внутренностью прямоугольника (Liang-Barsky).

    Прямоугольник сдвинут на `eps`: касание границы — это якорь дуги или
    общий край соседних фигур, а не прохождение сквозь фигуру.
    """
    left, top = box.x + eps, box.y + eps
    right, bottom = box.right - eps, box.bottom - eps
    if right <= left or bottom <= top:
        return False
    dx, dy = x2 - x1, y2 - y1
    low, high = 0.0, 1.0
    for p, q in ((-dx, x1 - left), (dx, right - x1),
                 (-dy, y1 - top), (dy, bottom - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            if ratio > high:
                return False
            low = max(low, ratio)
        elif ratio < low:
            return False
        else:
            high = min(high, ratio)
    return low < high


def _pool_names(plan: Dict[str, Any]) -> List[str]:
    """Имена участников плана: починка отдаёт словари, модель — строки."""
    names: List[str] = []
    for item in plan.get("participants") or []:
        name = _text(item.get("name") if isinstance(item, dict) else item)
        if name and name not in names:
            names.append(name)
    return names


def _dict_list(value: Any) -> List[Dict[str, Any]]:
    """Список словарей из недоверенного плана.

    То же значение, что у приватного `core/bpmn_generator._raw_dicts` (строка
    2255): тащить чужой приватный символ через границу модуля нельзя, а
    обратное направление импорта (генератор → раскладка) открывается при
    проводке, поэтому форма определена один раз локально и синхронизируется
    вручную.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str:
    """Текст поля плана: тот же tolerant-разбор, что у приватного
    `core/bpmn_generator._raw_text` (строка 2260) — синхронно вручную по той же
    причине, что и `_dict_list`."""
    return str(value or "").strip()

# --- дополнение раскладки: графика для того, что создал аплайер -------------

FLOW_TAGS = ("sequenceFlow", "messageFlow")


def supplement_di(root: ET.Element) -> List[str]:
    """Дорисовать BPMN DI тому, чего в раскладке нет, не трогая нарисованное.

    Зачем это аплайеру: генерация поставляет раскладку целиком, а пакет
    операций создаёт узлы и дуги без графики. Мера полноты раскладки
    (`bpmn-constructor/src/bpmnLayout.js`) видит поток без `BPMNEdge` и
    приказывает перерисовать всю схему авто-layout'ом, а тот не читает
    `laneSet`: у пользователя теряются дорожки, которых касалась принятая
    правка. Координаты существующих узлов не переписываются — ручная раскладка
    дороже догадки авто-layout'а, а контейнеры только расширяются, чтобы
    накрыть то, что в них поселила схема.

    Документ без записей диаграммы возвращается нетронутым: раскладка такой
    схемы — дело редактора, и серверные координаты отняли бы у пользователя
    привычную авто-раскладку.

    Гарантии: ни одна существующая фигура узла и ни одна существующая дуга не
    меняют границ; у каждого узла, дорожки и участника появляется ровно одна
    фигура; у каждого потока — дуга с ортогональными точками без нулевых
    отрезков; новый id не занимает адрес, который в документе уже занят; второй
    вызов на том же дереве не добавляет ни записи, ни пометки (цикл «починка до
    покоя» держится именно на этом).
    """
    plane = next((child for child in root.iter()
                  if isinstance(child.tag, str) and child.tag == _di("BPMNPlane")), None)
    if plane is None:
        return []
    return _Supplement(root, plane).run()


class _Supplement:
    """Снимок документа и порядок, в котором дорисовывается раскладка."""

    def __init__(self, root: ET.Element, plane: ET.Element) -> None:
        self.root = root
        self.plane = plane
        self.notes: List[str] = []
        self.boxes: Dict[str, _Box] = {}
        self.node_boxes: Dict[str, _Box] = {}
        self.used_ids: Set[str] = {
            element.get("id") for element in root.iter()
            if isinstance(element.tag, str) and element.get("id")}
        self.parents: Dict[ET.Element, ET.Element] = {}
        self.nodes: List[Dict[str, str]] = []
        self.lanes: List[Dict[str, Any]] = []
        self.pools: List[Dict[str, Any]] = []
        self.flows: List[Dict[str, str]] = []
        self.inbound: Dict[str, List[str]] = {}
        self.outbound: Dict[str, List[str]] = {}
        self.process_of_node: Dict[str, str] = {}
        self.pool_of_process: Dict[str, str] = {}
        self.lane_of_node: Dict[str, str] = {}
        self.pool_of_node: Dict[str, str] = {}
        self.drawn_edges: Set[str] = set()
        self.edge_elements: Dict[str, ET.Element] = {}
        self.new_pools: List[ET.Element] = []
        self.new_lanes: List[ET.Element] = []
        self.new_nodes: List[ET.Element] = []
        self.new_edges: List[ET.Element] = []
        self._scan()
        self._link()
        self._read_di()

    # --- снимок документа ---

    def _scan(self) -> None:
        """Семантический слой: узлы, дорожки, участники, потоки — в порядке
        документа, вместе со ссылками концов."""
        for element in self.root.iter():
            if not isinstance(element.tag, str):
                continue
            for child in element:
                self.parents[child] = element
        for element in self.root.iter():
            if not isinstance(element.tag, str) or not is_bpmn_tag(element.tag):
                continue
            tag = _local_name(element.tag)
            elem_id = element.get("id") or ""
            if tag in FLOW_NODE_TAGS and elem_id:
                self.process_of_node[elem_id] = self._owner_id(element, "process")
                self.nodes.append({"id": elem_id, "kind": tag,
                                   "name": element.get("name") or elem_id,
                                   "attached": element.get("attachedToRef") or ""})
            elif tag in FLOW_TAGS and elem_id:
                source = element.get("sourceRef") or ""
                target = element.get("targetRef") or ""
                self.flows.append({"id": elem_id, "source": source,
                                   "target": target, "kind": tag})
                self.outbound.setdefault(source, []).append(target)
                self.inbound.setdefault(target, []).append(source)
            elif tag == "lane":
                self.lanes.append({
                    "id": elem_id, "name": element.get("name") or elem_id,
                    "process": self._owner_id(element, "process"),
                    "members": [ref.text.strip()
                                for ref in _children(element, "flowNodeRef")
                                if (ref.text or "").strip()]})
            elif tag == "participant":
                self.pools.append({"id": elem_id,
                                   "name": element.get("name") or elem_id,
                                   "process": element.get("processRef") or ""})

    def _link(self) -> None:
        """Принадлежность: дорожка и узел — пулу через его процесс.

        Полоса имени пула и `laneSet` живут в разных частях документа
        (`collaboration` и `process`), поэтому «чей это пул» выводится из
        `processRef`, а не из вложенности тегов.
        """
        for pool in self.pools:
            if pool["process"]:
                self.pool_of_process[pool["process"]] = pool["id"]
        for lane in self.lanes:
            lane["pool"] = self.pool_of_process.get(lane["process"], "")
            for member in lane["members"]:
                self.lane_of_node[member] = lane["id"]
        for node_id, process_id in self.process_of_node.items():
            self.pool_of_node[node_id] = self.pool_of_process.get(process_id, "")

    def _read_di(self) -> None:
        """Нарисованное: фигуры и дуги, уже лежащие в плоскости.

        Фигура с `isHorizontal="true"` — контейнер (пул или дорожка): его
        границы покрывают содержимое, и считать такой прямоугольник
        препятствием нельзя — каждый узел «проходил бы сквозь» собственную
        дорожку.
        """
        containers = ({lane["id"] for lane in self.lanes}
                      | {pool["id"] for pool in self.pools})
        for child in self.plane:
            ref = child.get("bpmnElement")
            if not ref:
                continue
            if child.tag == _di("BPMNShape"):
                box = _bounds_of(child)
                if box is None:
                    continue
                self.boxes[ref] = box
                if ref not in containers:
                    self.node_boxes[ref] = box
            elif child.tag == _di("BPMNEdge"):
                self.drawn_edges.add(ref)
                self.edge_elements[ref] = child

    def _owner_id(self, element: ET.Element, local: str) -> str:
        """Имя внешнего предка: у шага внутри подпроцесса владельцем считается
        верхний процесс — тот же признак, что у пула в `laneSet`."""
        owner = ""
        node = self.parents.get(element)
        while node is not None:
            if isinstance(node.tag, str) and _local_name(node.tag) == local:
                owner = node.get("id") or ""
            node = self.parents.get(node)
        return owner

    # --- порядок дорисовки ---

    def run(self) -> List[str]:
        # Хозяин раньше пристава: у граничного события вход в `attachedToRef`
        # может вести к узлу, который этот же пакет добавил следом.
        ordered = sorted(self.nodes, key=lambda node: bool(node["attached"]))
        for node in ordered:
            self._place_node(node)
        for lane in self.lanes:
            self._place_lane(lane)
        for pool in self.pools:
            self._place_pool(pool)
        for flow in self.flows:
            self._draw_flow(flow)
        self._emit()
        return self.notes

    # --- узлы ---

    def _place_node(self, node: Dict[str, str]) -> None:
        elem_id = node["id"]
        if elem_id in self.boxes:
            return
        width, height = _size(node["kind"])
        box = self._node_box(node, width, height)
        self.node_boxes[elem_id] = box
        self.boxes[elem_id] = box
        self.new_nodes.append(self._shape(elem_id, box))
        self.notes.append(f"фигура «{node['name']}» ({elem_id}) добавлена в раскладку")

    def _node_box(self, node: Dict[str, str], width: int, height: int) -> _Box:
        elem_id = node["id"]
        host_id = node["attached"]
        if host_id and host_id in self.node_boxes:
            host = self.node_boxes[host_id]
            for slot in _border_boxes(host, width, height):
                if not self._collides(slot, skip=(host_id,)):
                    return slot
        sources = [self.boxes[i] for i in self.inbound.get(elem_id, []) if i in self.boxes]
        if sources:
            anchor = max(sources, key=lambda box: box.right)
            return self._free(_Box(anchor.right + NODE_GAP_X, anchor.cy - height // 2,
                                   width, height), width + NODE_GAP_X)
        targets = [self.boxes[i] for i in self.outbound.get(elem_id, []) if i in self.boxes]
        if targets:
            anchor = min(targets, key=lambda box: box.x)
            return self._free(_Box(anchor.x - NODE_GAP_X - width, anchor.cy - height // 2,
                                   width, height), -(width + NODE_GAP_X))
        siblings = [box for node_id, box in self.node_boxes.items()
                    if self.process_of_node.get(node_id) == self.process_of_node.get(elem_id)]
        if siblings:
            anchor = max(siblings, key=lambda box: box.right)
            return self._free(_Box(anchor.right + NODE_GAP_X, anchor.cy - height // 2,
                                   width, height), width + NODE_GAP_X)
        container = self._container_box(elem_id)
        if container is not None:
            return self._free(_Box(container.x + POOL_LABEL_WIDTH + LANE_INNER_PADDING_X,
                                   container.cy - height // 2, width, height),
                              width + NODE_GAP_X)
        # Ни соседа, ни нарисованного контейнера: узел относится к пулу, которого
        # на рисунке ещё нет, — ставим его под всем нарисованным, а пул накроет
        # его рамкой шагом ниже.
        below = max((box.bottom for box in self.boxes.values()), default=POOL_X)
        return self._free(_Box(POOL_X + POOL_LABEL_WIDTH + LANE_INNER_PADDING_X,
                               below + POOL_GAP_Y, width, height), width + NODE_GAP_X)

    def _container_box(self, elem_id: str) -> Optional[_Box]:
        for owner in (self.lane_of_node.get(elem_id, ""),
                      self.pool_of_node.get(elem_id, "")):
            box = self.boxes.get(owner) if owner else None
            if box is not None:
                return box
        return None

    def _free(self, box: _Box, step: int) -> _Box:
        """Сдвиг по горизонтали до первой свободной позиции.

        Шаг, а не «встать вплотную»: документ чужой, пользователь мог оставить
        между фигурами свой зазор, и втиснутый в него узел лёг бы на соседний.
        """
        guard = 0
        while self._collides(box) and guard < 200:
            box = _Box(box.x + step, box.y, box.w, box.h)
            guard += 1
        return box

    def _collides(self, box: _Box, skip: Sequence[str] = ()) -> bool:
        skipped = set(skip)
        return any(other.overlaps(box) for node_id, other in self.node_boxes.items()
                   if node_id not in skipped)

    # --- контейнеры ---

    def _place_lane(self, lane: Dict[str, Any]) -> None:
        lane_id = lane["id"]
        content = _union([self.node_boxes[i] for i in lane["members"]
                          if i in self.node_boxes])
        existing = self.boxes.get(lane_id)
        if existing is not None:
            if content is not None:
                self._resize_container(lane_id, _grow(
                    existing, content, left=LANE_INNER_PADDING_X,
                    right=LANE_INNER_PADDING_X, top=LANE_VERTICAL_PADDING // 2,
                    bottom=LANE_VERTICAL_PADDING // 2), lane["name"], "дорожка")
            return
        pool_box = self.boxes.get(lane["pool"])
        siblings = [self.boxes[other["id"]] for other in self.lanes
                    if other["id"] != lane_id and other["pool"] == lane["pool"]
                    and other["id"] in self.boxes]
        below = max((box.bottom for box in siblings), default=0)
        x = (pool_box.x + POOL_LABEL_WIDTH if pool_box is not None
             else (content.x - LANE_INNER_PADDING_X if content is not None
                   else POOL_X + POOL_LABEL_WIDTH))
        candidates = [x + POOL_MIN_WIDTH - POOL_LABEL_WIDTH]
        if pool_box is not None:
            candidates.append(pool_box.right)
        if content is not None:
            candidates.append(content.right + LANE_INNER_PADDING_X)
        right = max(candidates)
        y = below + 1 if below else (
            content.y - LANE_VERTICAL_PADDING // 2 if content is not None else POOL_X)
        if content is not None:
            y = min(y, content.y - LANE_VERTICAL_PADDING // 2)
        bottom = max(y + LANE_MIN_HEIGHT,
                     content.bottom + LANE_VERTICAL_PADDING // 2
                     if content is not None else y)
        box = _Box(x, y, right - x, bottom - y)
        self.boxes[lane_id] = box
        self.new_lanes.append(self._shape(lane_id, box, horizontal=True))
        self.notes.append(f"дорожка «{lane['name']}» ({lane_id}) поставлена в "
                          "раскладку под уже нарисованные")

    def _place_pool(self, pool: Dict[str, Any]) -> None:
        pool_id = pool["id"]
        content = _union([box for elem_id, box in self.boxes.items()
                          if self._holds(pool_id, elem_id)])
        existing = self.boxes.get(pool_id)
        if existing is not None:
            if content is not None:
                self._resize_container(pool_id, _grow(
                    existing, content, left=0, right=LANE_INNER_PADDING_X, top=0,
                    bottom=LANE_VERTICAL_PADDING // 2), pool["name"], "пул")
            return
        if content is not None:
            x = content.x - POOL_LABEL_WIDTH
            box = _Box(x, content.y,
                       max(content.right + LANE_INNER_PADDING_X,
                           x + POOL_MIN_WIDTH) - x,
                       content.h + LANE_VERTICAL_PADDING // 2)
        else:
            below = max((box.bottom for box in self.boxes.values()), default=POOL_X)
            box = _Box(POOL_X, below + POOL_GAP_Y, POOL_MIN_WIDTH, LANE_MIN_HEIGHT)
        self.boxes[pool_id] = box
        self.new_pools.append(self._shape(pool_id, box, horizontal=True))
        self.notes.append(f"пул «{pool['name']}» ({pool_id}) добавлен в раскладку")

    def _holds(self, pool_id: str, elem_id: str) -> bool:
        """Содержимое пула: его дорожки и шаги, названные этими дорожками или
        лежащие в его процессе."""
        if not pool_id or elem_id == pool_id:
            return False
        for lane in self.lanes:
            if lane["id"] == elem_id or elem_id in lane["members"]:
                return lane["pool"] == pool_id
        return (self.pool_of_node.get(elem_id) == pool_id
                and elem_id in self.node_boxes)

    def _resize_container(self, elem_id: str, box: _Box, name: str, kind: str) -> None:
        """Контейнер только растёт: накрыть то, что в него поселила схема, —
        единственное, что допускается в уже нарисованных границах."""
        old = self.boxes[elem_id]
        if (box.x, box.y, box.w, box.h) == (old.x, old.y, old.w, old.h):
            return
        self.boxes[elem_id] = box
        for child in self.plane:
            if child.tag != _di("BPMNShape") or child.get("bpmnElement") != elem_id:
                continue
            bounds = child.find(_dc_bounds())
            if bounds is not None:
                bounds.set("x", str(box.x))
                bounds.set("y", str(box.y))
                bounds.set("width", str(box.w))
                bounds.set("height", str(box.h))
            break
        self.notes.append(f"{kind} «{name}» ({elem_id}) расширен под то, что "
                          "поместила в него схема")

    # --- дуги ---

    def _draw_flow(self, flow: Dict[str, str]) -> None:
        source = self._endpoint_box(flow["source"])
        target = self._endpoint_box(flow["target"])
        if source is None or target is None:
            missing = flow["source"] if source is None else flow["target"]
            self.notes.append(f"дуга {flow['id']} не нарисована: конца «{missing}» "
                              "в раскладке нет, а координаты ему выдумывать нечем")
            return
        points = _clean_points(_supplement_points(source, target))
        if len(points) < 2:
            points = _fallback_points(source, target)
        existing = self.edge_elements.get(flow["id"])
        if existing is None:
            self.drawn_edges.add(flow["id"])
            self.new_edges.append(self._edge(flow["id"], points))
            self.notes.append(f"дуга {flow['id']} ({flow['source']} → "
                              f"{flow['target']}) дорисована в раскладке")
            return
        drawn = _waypoints(existing)
        if drawn == points or _anchored(drawn, source, target):
            return
        # Пакет перецепил конец потока: `add_task(after, to)` оставляет id дуги
        # за новой парой узлов, а её точки стоят на старых фигурах. Такая
        # стрелка уходит в пустоту — правка «шаг встал в маршрут» выглядела бы
        # сломанной, хотя XML корректен.
        _set_waypoints(existing, points)
        self.notes.append(f"дуга {flow['id']} перерисована: её концы ({flow['source']} "
                          f"→ {flow['target']}) сменили фигуры")

    def _endpoint_box(self, elem_id: str) -> Optional[_Box]:
        """Границы конца обмена: фигура узла, фигура пула, а конца, которого в
        раскладке нет (импортный id вне словаря видов), ставим как шаг."""
        if not elem_id:
            return None
        box = self.boxes.get(elem_id)
        if box is not None:
            return box
        element = next((child for child in self.root.iter()
                        if isinstance(child.tag, str) and child.get("id") == elem_id), None)
        if element is None:
            return None
        kind = _local_name(element.tag)
        width, height = _size(kind)
        node = {"id": elem_id, "kind": kind, "name": element.get("name") or elem_id,
                "attached": element.get("attachedToRef") or ""}
        box = self._node_box(node, width, height)
        self.node_boxes[elem_id] = box
        self.boxes[elem_id] = box
        self.new_nodes.append(self._shape(elem_id, box))
        self.notes.append(f"фигура «{node['name']}» ({elem_id}) добавлена в раскладку")
        return box

    # --- эмиссия ---

    def _unique(self, candidate: str) -> str:
        while candidate in self.used_ids:
            candidate = f"{candidate}_x"
        self.used_ids.add(candidate)
        return candidate

    def _shape(self, ref: str, box: _Box, *, horizontal: bool = False) -> ET.Element:
        shape = _shape(ref, box, horizontal=horizontal)
        shape.set("id", self._unique(shape.get("id")))
        return shape

    def _edge(self, ref: str, points: Sequence[Point]) -> ET.Element:
        edge = _edge(ref, points)
        edge.set("id", self._unique(edge.get("id")))
        return edge

    def _emit(self) -> None:
        """Порядок детей плоскости — порядок отрисовки bpmn-js: пулы, дорожки,
        узлы, дуги. Новые контейнеры встают перед первой фигурой, новые узлы —
        сразу за последней, дуги — в конец."""
        children = list(self.plane)
        shapes = [index for index, child in enumerate(children)
                  if child.tag == _di("BPMNShape")]
        if not shapes:
            children = self.new_pools + self.new_lanes + children + self.new_nodes
        else:
            out: List[ET.Element] = []
            for index, child in enumerate(children):
                if index == shapes[0]:
                    out.extend(self.new_pools)
                    out.extend(self.new_lanes)
                out.append(child)
                if index == shapes[-1]:
                    out.extend(self.new_nodes)
            children = out
        children.extend(self.new_edges)
        self.plane[:] = children


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1] if "}" in str(tag) else str(tag)


def _waypoints(edge: ET.Element) -> List[Point]:
    out: List[Point] = []
    for child in edge:
        if child.tag != f"{{{DI_NS}}}waypoint":
            continue
        try:
            out.append((int(float(child.get("x") or 0)), int(float(child.get("y") or 0))))
        except ValueError:
            continue
    return out


def _set_waypoints(edge: ET.Element, points: Sequence[Point]) -> None:
    """Заменить точки дуги, сохранив сам элемент: id, `bpmnElement` и порядок
    детей плоскости при перерисовке конца не меняются."""
    for child in list(edge):
        edge.remove(child)
    for x, y in points:
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": str(x), "y": str(y)})


def _on_anchor(point: Point, box: _Box) -> bool:
    """Точка лежит на рамке фигуры: именно так bpmn-js понимает, что дуга
    входит в элемент, а не висит рядом с ним."""
    x, y = point
    return ((x in (box.x, box.right) and box.y <= y <= box.bottom)
            or (y in (box.y, box.bottom) and box.x <= x <= box.right))


def _anchored(points: Sequence[Point], source: _Box, target: _Box) -> bool:
    if len(points) < 2:
        return False
    return _on_anchor(points[0], source) and _on_anchor(points[-1], target)


def _children(element: ET.Element, local: str) -> List[ET.Element]:
    return [child for child in element
            if isinstance(child.tag, str) and _local_name(child.tag) == local]


def _dc_bounds() -> str:
    return f"{{{DC_NS}}}Bounds"


def _bounds_of(shape: ET.Element) -> Optional[_Box]:
    bounds = shape.find(_dc_bounds())
    if bounds is None:
        return None
    try:
        return _Box(int(float(bounds.get("x") or 0)), int(float(bounds.get("y") or 0)),
                    int(float(bounds.get("width") or 0)),
                    int(float(bounds.get("height") or 0)))
    except ValueError:
        return None


def _union(boxes: Sequence[_Box]) -> Optional[_Box]:
    if not boxes:
        return None
    left, top = min(b.x for b in boxes), min(b.y for b in boxes)
    right, bottom = max(b.right for b in boxes), max(b.bottom for b in boxes)
    return _Box(left, top, right - left, bottom - top)


def _grow(area: _Box, content: _Box, *, left: int, right: int, top: int,
          bottom: int) -> _Box:
    """Контейнер, накрывающий содержимое с полями: только рост — ни сужения, ни
    переноса, ручная раскладка пользователя остаётся на месте."""
    x = min(area.x, content.x - left)
    y = min(area.y, content.y - top)
    return _Box(x, y, max(area.right, content.right + right) - x,
                max(area.bottom, content.bottom + bottom) - y)


def _supplement_points(source: _Box, target: _Box) -> List[Point]:
    """Ортогональный путь между двумя нарисованными фигурами.

    Дуге пакета улучшения не нужно обходить сетку колонок, как дуге генерации:
    чужие фигуры уже расставлены, и остался единственный честный выбор — с
    какой стороны входить. Цель правее — вход слева, левее — вход справа, иначе
    (концы перекрываются по горизонтали) — обход по свободной вертикали под ними
    или над ними.
    """
    if target.x >= source.right:
        first, last = (source.right, source.cy), (target.x, target.cy)
    elif source.x >= target.right:
        first, last = (source.x, source.cy), (target.right, target.cy)
    elif target.cy >= source.cy:
        my = max(source.bottom, target.bottom) + CHANNEL_WIDTH // 2
        return [(source.cx, source.bottom), (source.cx, my),
                (target.cx, my), (target.cx, target.y)]
    else:
        my = min(source.y, target.y) - CHANNEL_WIDTH // 2
        return [(source.cx, source.y), (source.cx, my),
                (target.cx, my), (target.cx, target.bottom)]
    if first[1] == last[1] or first[0] == last[0]:
        return [first, last]
    if first[0] != last[0]:
        mid = (first[0] + last[0]) // 2
        return [first, (mid, first[1]), (mid, last[1]), last]
    mid = (first[1] + last[1]) // 2
    return [first, (first[0], mid), (last[0], mid), last]

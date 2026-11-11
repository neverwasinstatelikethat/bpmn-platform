"""Раскладка BPMN DI (`core/bpmn_layout.py`) — геометрия хранимой схемы.

Модуль чистый и детерминированный: план на входе, элементы DI на выходе, без
модели, сети и времени. Поэтому проверки здесь геометрические, а не «список
строк похож на образец»: фигура пула обязана накрыть дорожку, дорожка — узел,
дуга — иметь хотя бы две разные точки и кончаться на границе своей фигуры.

Зачем это мерится отдельно от генератора: «кривой layout» пользователи видели не
потому, что XML не собирался, а потому, что координат в нём не было вовсе
(пустой DI-скелет), а поставляемый фронтендом авто-layout дорожек не читает.

Планы собираются на месте маленькими строителями: датасет и LLM здесь ни при
чём — раскладка читает только структуру плана после починки.
"""
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from core import bpmn_edits, bpmn_layout

# Теги собираются из констант аплайера: если раскладка начнёт выпускать элемент
# с другим неймспейсом, проверка красная — и ровно на той строке, которая
# разошлась.
DIAGRAM = f"{{{bpmn_edits.BPMNDI_NS}}}BPMNDiagram"
SHAPE = f"{{{bpmn_edits.BPMNDI_NS}}}BPMNShape"
EDGE = f"{{{bpmn_edits.BPMNDI_NS}}}BPMNEdge"
BOUNDS = f"{{{bpmn_edits.DC_NS}}}Bounds"
WAYPOINT = f"{{{bpmn_edits.DI_NS}}}waypoint"

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- строители плана ------------------------------------------------------

def _e(elem_id, kind, name, lane="", participant="", **extra):
    return {"id": elem_id, "kind": kind, "name": name,
            "participant": participant, "lane": lane, **extra}


def _f(flow_id, source, target, kind="sequence", **extra):
    return {"id": flow_id, "source": source, "target": target,
            "kind": kind, **extra}


def _lane(lane_id, name, participant):
    return {"id": lane_id, "name": name, "participant": participant}


def two_pools_plan():
    """Два пула по две дорожки, девять узлов и сообщение между пулами.

    Тот же каркас, что отдаёт починка: роли — дорожки своего пула, маршрут
    читается налево-направо, один обратный переход — чтобы в схеме был цикл.
    """
    return {
        "participants": ["ВкусВилл", "Поставщик"],
        "lanes": [
            _lane("L_m", "Менеджер", "ВкусВилл"),
            _lane("L_s", "Склад", "ВкусВилл"),
            _lane("L_o", "Отгрузка", "Поставщик"),
            _lane("L_f", "Фактура", "Поставщик"),
        ],
        "elements": [
            _e("S1", "startEvent", "Заказ создан", "L_m", "ВкусВилл"),
            _e("T1", "userTask", "Собрать заказ", "L_m", "ВкусВилл"),
            _e("G1", "exclusiveGateway", "Собран ли заказ?", "L_m", "ВкусВилл"),
            _e("T2", "userTask", "Отгрузить со склада", "L_s", "ВкусВилл"),
            _e("E1", "endEvent", "Заказ закрыт", "L_s", "ВкусВилл"),
            _e("S2", "startEvent", "Заявка получена", "L_o", "Поставщик"),
            _e("T3", "serviceTask", "Сформировать накладную", "L_o", "Поставщик"),
            _e("T4", "userTask", "Провести оплату", "L_f", "Поставщик"),
            _e("E2", "endEvent", "Накладная отправлена", "L_f", "Поставщик"),
        ],
        "flows": [
            _f("F1", "S1", "T1"),
            _f("F2", "T1", "G1"),
            _f("F3", "G1", "T2", condition="Да"),
            _f("F4", "G1", "T1", condition="Нет"),
            _f("F5", "T2", "E1"),
            _f("F6", "S2", "T3"),
            _f("F7", "T3", "T4"),
            _f("F8", "T4", "E2"),
            _f("M1", "T1", "T3", kind="message"),
        ],
    }


def one_lane_plan():
    """Один ряд из пяти узлов: все дуги идут внутри одной полосы ряда."""
    return {
        "participants": ["ВкусВилл"],
        "lanes": [_lane("L_a", "А", "ВкусВилл")],
        "elements": [
            _e("S1", "startEvent", "Старт", "L_a", "ВкусВилл"),
            _e("T1", "userTask", "Собрать", "L_a", "ВкусВилл"),
            _e("T2", "userTask", "Отгрузить", "L_a", "ВкусВилл"),
            _e("T3", "userTask", "Оплатить", "L_a", "ВкусВилл"),
            _e("E1", "endEvent", "Финиш", "L_a", "ВкусВилл"),
        ],
        "flows": [_f("F1", "S1", "T1"), _f("F2", "T1", "T2"),
                  _f("F3", "T2", "T3"), _f("F4", "T3", "E1"),
                  _f("F5", "T1", "E1"), _f("F6", "T1", "T3"),
                  _f("F7", "T3", "T1")],
    }


def boundary_plan(count):
    """Один шаг-хозяин и `count` граничных таймеров на его границе."""
    elements = [
        _e("S1", "startEvent", "Палета прибыла", "L_w", "Склад"),
        _e("A1", "userTask", "Принять палету", "L_w", "Склад"),
        _e("E1", "endEvent", "Палета принята", "L_w", "Склад"),
    ]
    for index in range(1, count + 1):
        elements.append(_e(f"B{index}", "boundaryEvent", f"Срок {index}",
                           "L_w", "Склад", attached_to="A1", timer="PT15M"))
    return {
        "participants": ["Склад"],
        "lanes": [_lane("L_w", "Кладовщик", "Склад")],
        "elements": elements,
        "flows": [_f("F1", "S1", "A1"), _f("F2", "A1", "E1")],
    }


def stray_plan():
    """Дорожка и шаги без объявленного пула: защитный путь, а не основной.

    Генератор кладёт каждый узел в дорожку объявленного участника, поэтому без
    пула сюда попадают только планы, пришедшие мимо починки.
    """
    return {
        "participants": ["ВкусВилл"],
        "lanes": [
            _lane("L_m", "Менеджер", "ВкусВилл"),
            _lane("L_x", "Подрядчик без пула", "Подрядчик"),
        ],
        "elements": [
            _e("S1", "startEvent", "Заявка", "L_m", "ВкусВилл"),
            _e("T1", "userTask", "Согласовать", "L_m", "ВкусВилл"),
            _e("E1", "endEvent", "Готово", "L_m", "ВкусВилл"),
            _e("X1", "userTask", "Выполнить работы", "L_x", "Подрядчик"),
            _e("X2", "userTask", "Сдать акт", "L_x", "Подрядчик"),
        ],
        "flows": [_f("F1", "S1", "T1"), _f("F2", "T1", "E1"),
                  _f("F3", "X1", "X2")],
    }


# --- разбор выданного DI --------------------------------------------------

def render(plan, *, process_ids=None, participant_ids=None):
    """Корень `BPMNDiagram` по плану; отображения id считаются, как у эмиссии."""
    if participant_ids is None:
        names = [p if isinstance(p, str) else p.get("name", "")
                 for p in (plan.get("participants") or [])]
        participant_ids = {name: f"Participant_{i}"
                           for i, name in enumerate(names, 1) if name}
    if process_ids is None:
        process_ids = {name: f"Process_{i}"
                       for i, name in enumerate(sorted(participant_ids), 1)}
    nodes = bpmn_layout.diagram(plan, process_ids=process_ids,
                               participant_ids=participant_ids)
    assert len(nodes) == 1, "раскладка обязана отдать один BPMNDiagram"
    assert nodes[0].tag == DIAGRAM
    return nodes[0], participant_ids


def layout_of(plan):
    """Объект раскладки по плану: те же отображения id, что выдаёт `render`.

    Нужен, когда проверке важна работа `route` (выбор кандидата), а не готовый
    DI: геометрию таких проверок ставится руками, потому что раскладка из
    плана всегда находит свободный канал.
    """
    names = [p if isinstance(p, str) else p.get("name", "")
             for p in (plan.get("participants") or [])]
    participant_ids = {name: f"Participant_{i}"
                       for i, name in enumerate(names, 1) if name}
    process_ids = {name: f"Process_{i}"
                   for i, name in enumerate(sorted(participant_ids), 1)}
    return bpmn_layout._Layout(plan, process_ids=process_ids,
                               participant_ids=participant_ids)


def shapes_of(root):
    """`bpmnElement` → (x, y, width, height); дубль фигуры — провал проверки."""
    out = {}
    for node in root.iter(SHAPE):
        ref = node.get("bpmnElement")
        bounds = node.find(BOUNDS)
        assert bounds is not None, f"фигура {ref} без границ"
        assert ref not in out, f"две фигуры на один адрес {ref}"
        out[ref] = (int(bounds.get("x")), int(bounds.get("y")),
                    int(bounds.get("width")), int(bounds.get("height")))
    return out


def edges_of(root):
    """`bpmnElement` дуги → список точек маршрута."""
    return {node.get("bpmnElement"): [(int(w.get("x")), int(w.get("y")))
                                      for w in node.findall(WAYPOINT)]
            for node in root.iter(EDGE)}


def covers(outer, inner):
    """Накрытие: границы входящие, общий край — не выход за рамку."""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (ox <= ix and oy <= iy and ox + ow >= ix + iw
            and oy + oh >= iy + ih)


def overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def on_border(box, point):
    """Точка лежит на рамке фигуры, а не внутри и не снаружи её."""
    left, top = box[0], box[1]
    right, bottom = left + box[2], top + box[3]
    within_x = left <= point[0] <= right
    within_y = top <= point[1] <= bottom
    return ((point[0] in (left, right)) and within_y
            or (point[1] in (top, bottom)) and within_x)


def center(box):
    return (box[0] + box[2] / 2, box[1] + box[3] / 2)


def element_ids(plan):
    return {item.get("id") for item in plan.get("elements") or []
            if item.get("id")}


def lane_ids(plan):
    return {item.get("id") for item in plan.get("lanes") or [] if item.get("id")}


def pool_of_lane(plan, lane_id):
    for lane in plan.get("lanes") or []:
        if lane.get("id") == lane_id:
            return lane.get("participant")
    return None


# --- проверки -------------------------------------------------------------

class TestShapesFollowThePlan:
    """Каждый адрес плана получает фигуру, и фигуры накрыты своими родителями."""

    def test_every_element_lane_and_participant_has_exactly_one_shape(self):
        plan = two_pools_plan()
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        for elem_id in element_ids(plan):
            assert elem_id in shapes, f"шаг {elem_id} без фигуры"
        for lane_id in lane_ids(plan):
            assert lane_id in shapes, f"дорожка {lane_id} без фигуры"
        for name in plan["participants"]:
            assert participant_ids[name] in shapes, f"пул {name} без фигуры"
        # Ровно одна фигура на адрес: дубль уже поймал бы `shapes_of`, здесь
        # сверяется общее число — иначе лишняя фигура пряталась бы за дублем.
        assert len(shapes) == (len(plan["elements"]) + len(plan["lanes"])
                               + len(plan["participants"]))

    def test_every_shape_has_a_positive_size(self):
        root, _ = render(two_pools_plan())
        for ref, box in shapes_of(root).items():
            assert box[2] > 0 and box[3] > 0, f"фигура {ref} вырождена: {box}"

    def test_node_lies_in_its_lane_and_lane_in_its_pool(self):
        plan = two_pools_plan()
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        for item in plan["elements"]:
            lane_ref = item["lane"]
            pool_ref = participant_ids[pool_of_lane(plan, lane_ref)]
            assert covers(shapes[lane_ref], shapes[item["id"]]), (
                f"узел {item['id']} вышел из дорожки {lane_ref}")
            assert covers(shapes[pool_ref], shapes[lane_ref]), (
                f"дорожка {lane_ref} вышла из пула {pool_ref}")

    def test_node_shapes_do_not_overlap(self):
        """Два шага не могут лежать друг на друге: такую схему не прочитать.

        Граничное событие со своим хозяином — единственная пара, где пересечение
        законно (нотация рисует его на границе хозяина), и в этом плане таких
        узлов нет.
        """
        plan = two_pools_plan()
        root, _ = render(plan)
        shapes = shapes_of(root)
        boxes = sorted((ref, shapes[ref]) for ref in element_ids(plan))
        for index, (left, left_box) in enumerate(boxes):
            for right, right_box in boxes[index + 1:]:
                assert not overlap(left_box, right_box), (
                    f"фигуры {left} и {right} пересеклись")

    def test_lanes_mosaic_their_pool_without_gaps(self):
        """Дорожки мостят пул: верх первой — верх пула, зазор между соседями
        нулевой, низ последней — низ пула (замер корпуса 367 схем)."""
        plan = two_pools_plan()
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        for name in plan["participants"]:
            pool = shapes[participant_ids[name]]
            rows = sorted((shapes[lane["id"]][1], shapes[lane["id"]][3])
                          for lane in plan["lanes"] if lane["participant"] == name)
            assert rows[0][0] == pool[1]
            for previous, nxt in zip(rows, rows[1:]):
                assert previous[0] + previous[1] == nxt[0], (
                    f"между дорожками пула {name} образовалась щель")
            assert rows[-1][0] + rows[-1][1] == pool[1] + pool[3]

    def test_pools_keep_the_order_of_the_plan(self):
        plan = two_pools_plan()
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        first, second = (shapes[participant_ids[name]]
                         for name in plan["participants"])
        assert first[1] + first[3] <= second[1], "пулы переставились местами"


class TestBoundaryEvents:
    """Граничные события одного хозяина расходятся по его границе."""

    def test_eight_events_on_one_host_do_not_overlap(self):
        """Шестое и седьмое событие прежним обходом складались в одну точку:
        сдвиг вдоль стороны упирался в `min`/`max`, и пятое с шестым получали
        границы первого со вторым."""
        plan = boundary_plan(8)
        root, _ = render(plan)
        shapes = shapes_of(root)
        events = [shapes[f"B{index}"] for index in range(1, 9)]
        assert len(set(events)) == 8, "события получили одинаковые границы"
        for index, first in enumerate(events):
            for second in events[index + 1:]:
                assert not overlap(first, second), (
                    f"B{index + 1} легло на другое событие")

    def test_more_events_than_the_border_holds_stay_distinct(self):
        """Девятое и десятое событие не встают в занятую точку и не ложатся на
        соседний шаг: граница исчерпана — узел уходит в сетку колонок."""
        plan = boundary_plan(10)
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        events = [shapes[f"B{index}"] for index in range(1, 11)]
        assert len(set(events)) == 10, "события получили одинаковые границы"
        boxes = sorted((ref, shapes[ref]) for ref in element_ids(plan)
                       if not ref.startswith("B"))
        for index, first in enumerate(events):
            for second in events[index + 1:]:
                assert not overlap(first, second)
            for ref, box in boxes:
                if ref != "A1":
                    assert not overlap(first, box), (
                        f"B{index + 1} легло на узел {ref}")
        pool = shapes[participant_ids["Склад"]]
        for box in events:
            assert covers(pool, box), f"событие вышло за пул: {box}"

    def test_event_center_sits_on_the_host_border(self):
        plan = boundary_plan(8)
        root, _ = render(plan)
        shapes = shapes_of(root)
        host = shapes["A1"]
        edges = (host[0], host[0] + host[2], host[1], host[1] + host[3])
        for index in range(1, 9):
            cx, cy = center(shapes[f"B{index}"])
            distance = min(abs(cx - edges[0]), abs(cx - edges[1]),
                           abs(cy - edges[2]), abs(cy - edges[3]))
            assert distance <= 1, (
                f"B{index} оторвался от границы хозяина: {shapes[f'B{index}']}")

    def test_events_do_not_leave_the_lane(self):
        """Выступление наружу ограничено половиной рамки события, а оно (18 px)
        меньше полосы, которую раскладка оставляет вокруг шага в дорожке."""
        plan = boundary_plan(8)
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        for index in range(1, 9):
            box = shapes[f"B{index}"]
            assert covers(shapes["L_w"], box), f"B{index} вышел за дорожку: {box}"
            assert covers(shapes[participant_ids["Склад"]], box), (
                f"B{index} вышел за пул")

    def test_hostless_boundary_event_is_placed_like_a_regular_node(self):
        plan = two_pools_plan()
        plan["elements"].append(_e("B9", "boundaryEvent", "Срок без хозяина",
                                   "L_s", "ВкусВилл", attached_to="Нет_такого"))
        root, _ = render(plan)
        shapes = shapes_of(root)
        assert "B9" in shapes, "событие без хозяина пропало со схемы"
        assert shapes["B9"][0] > 0, "событие без хозяина осталось в (0, 0)"
        for other in element_ids(plan) - {"B9"}:
            assert not overlap(shapes["B9"], shapes[other])


class TestEdges:
    """Дуги: непустой путь, нулевых отрезков нет, концы — на границах фигур."""

    def test_every_flow_with_two_known_ends_gets_an_edge(self):
        plan = two_pools_plan()
        root, _ = render(plan)
        edges = edges_of(root)
        for flow in plan["flows"]:
            assert flow["id"] in edges, f"поток {flow['id']} без дуги"
            assert len(edges[flow["id"]]) >= 2

    def test_no_edge_carries_a_zero_length_segment(self):
        for plan in (two_pools_plan(), one_lane_plan(), boundary_plan(8)):
            root, _ = render(plan)
            for ref, points in edges_of(root).items():
                assert len(points) >= 2, f"дуга {ref} короче двух точек"
                for first, second in zip(points, points[1:]):
                    assert first != second, (
                        f"дуга {ref} несёт нулевой отрезок в {first}")

    def test_endpoints_sit_on_the_borders_of_their_shapes(self):
        plan = two_pools_plan()
        root, _ = render(plan)
        shapes, edges = shapes_of(root), edges_of(root)
        by_id = {item["id"]: item for item in plan["elements"]}
        for flow in plan["flows"]:
            points = edges[flow["id"]]
            source = shapes[by_id[flow["source"]]["id"]]
            target = shapes[by_id[flow["target"]]["id"]]
            assert on_border(source, points[0]), (
                f"выход дуги {flow['id']} вне границы {flow['source']}")
            assert on_border(target, points[-1]), (
                f"вход дуги {flow['id']} вне границы {flow['target']}")

    def test_message_flow_between_pools_gets_an_edge(self):
        plan = two_pools_plan()
        root, _ = render(plan)
        edges = edges_of(root)
        assert "M1" in edges, "межпуловое сообщение без дуги"
        assert len(edges["M1"]) >= 2

    def test_flow_avoids_a_foreign_boundary_event(self):
        """Чужое граничное событие — препятствие, а не прозрачная метка.

        Исключение делается только для хозяина самого ребра: без него дуга не
        могла бы выйти из пристёгнутого события.

        Меряется по всем рёбрам и всем чужим фигурам сразу, а не «одна дуга не
        тронула один таймер»: при четырёх событиях слоты ещё не закрывают ни
        центр левой грани, ни центр верхней, и точечная проверка оставалась
        зелёной ровно на той геометрии, где раскладка уже идёт сквозь событие.
        Пять событий закрывают верхний ряд слотов, шесть — ещё и центр левой
        грани, и с этого момента дуга «старт → шаг» входит в хозяина сквозь
        соседний таймер.
        """
        for count in (4, 5, 6, 7, 8):
            plan = boundary_plan(count)
            root, _ = render(plan)
            shapes, edges = shapes_of(root), edges_of(root)
            nodes = {e["id"]: e for e in plan["elements"]}
            for flow in plan["flows"]:
                allowed = {flow["source"], flow["target"]}
                for end in allowed:
                    node = nodes.get(end)
                    if node["kind"] == "boundaryEvent":
                        allowed.add(node["attached_to"])
                points = edges[flow["id"]]
                for node_id, box in shapes.items():
                    if node_id not in nodes or node_id in allowed:
                        continue
                    for first, second in zip(points, points[1:]):
                        assert not bpmn_layout._segment_hits_box(
                            first[0], first[1], second[0], second[1],
                            bpmn_layout._Box(*box)), (
                            f"{count} событий: дуга {flow['id']} "
                            f"({flow['source']} → {flow['target']}) прошла сквозь "
                            f"чужую фигуру {node_id}: {points}")

    def test_when_no_path_is_free_the_router_crosses_one_figure_not_two(self):
        """Все кандидаты задеты: наружу уходит путь с меньшим числом пересечений.

        Проверка ставит геометрию руками, а не собирает план: раскладка
        любого плана из этих тестов находит свободный канал (проверено перебором
        до двенадцати событий у одного шага и стены из восьми шагов), поэтому
        ветвь «ни один кандидат не свободен» на готовых планах не достижима ничем,
        кроме явно поставленной преграды.

        Стена `W_all` перекрывает всю полосу — сквозь неё идёт любой путь, и
        свободных кандидатов не остаётся вовсе. `W_channel` дополнительно затыкает
        внешний канал, которым был последний кандидат: прежняя заплатка возвращала
        его непроверенным и получала два пересечения там, где есть путь с одним.
        """
        plan = {
            "participants": ["Склад"],
            "lanes": [_lane("L_w", "Кладовщик", "Склад")],
            "elements": [
                _e("S1", "startEvent", "Палета прибыла", "L_w", "Склад"),
                _e("A1", "userTask", "Принять палету", "L_w", "Склад"),
                _e("W_all", "userTask", "Стена", "L_w", "Склад"),
                _e("W_channel", "userTask", "Канал", "L_w", "Склад"),
            ],
            "flows": [_f("F1", "S1", "A1")],
        }
        layout = layout_of(plan)
        row = layout.by_id["S1"].row
        outside = layout.diagram_right() + bpmn_layout.OUTSIDE_MARGIN_X
        layout.by_id["W_all"].box = bpmn_layout._Box(
            0, row.box.y - 60, 4000, row.box.h + 120)
        layout.by_id["W_channel"].box = bpmn_layout._Box(
            outside - 20, row.box.y - 60, 40, row.box.h + 120)

        points = layout.route(layout.by_id["S1"], layout.by_id["A1"])
        hits = bpmn_layout._path_collisions(points,
                                            layout._obstacles(layout.by_id["S1"],
                                                              layout.by_id["A1"]))
        assert hits == 1, f"дуга задела {hits} фигур, хотя есть путь с одной: {points}"
        blind = bpmn_layout._clean_points(layout._candidates(
            layout.by_id["S1"], layout.by_id["A1"])[-1])
        assert bpmn_layout._path_collisions(
            blind, layout._obstacles(layout.by_id["S1"],
                                     layout.by_id["A1"])) == 2, (
            "последний кандидат больше не затыкается стеной — проверка уснула")

    def test_message_flow_to_a_black_box_participant_gets_an_edge(self):
        plan = two_pools_plan()
        plan["flows"] = [_f("M2", "T1", "Поставщик", kind="message")]
        root, participant_ids = render(plan)
        shapes, edges = shapes_of(root), edges_of(root)

        assert "M2" in edges, "сообщение в чёрный ящик потеряло DI"
        assert on_border(shapes["T1"], edges["M2"][0])
        assert on_border(shapes[participant_ids["Поставщик"]], edges["M2"][-1])

    def test_detour_inside_one_lane_does_not_fold_into_one_point(self):
        """Детур по полосам ряда: у источника и цели полоса общая, и вертикальное
        плечо в канале вырождалось — дуга из двух одинаковых точек."""
        root, _ = render(one_lane_plan())
        edges = edges_of(root)
        assert len(edges) == len(one_lane_plan()["flows"])
        for ref, points in edges.items():
            assert len(points) >= 2, f"дуга {ref} сложилась в точку"
            for first, second in zip(points, points[1:]):
                assert first != second, f"нулевой отрезок в дуге {ref}"

    def test_ladder_of_one_band_is_emitted_without_the_repeated_point(self):
        """Лестница детура: у источника и цели одна полоса ряда, и вертикальное
        плечо в канале вырождалось в точку.

        Проверка идёт на самом строителе лестницы, а не на готовой схеме: выбор
        кандидата зависит от теста на прохождение, и на собранных планах
        вырожденное плечо оставалось в отброшенном кандидате, — но выпущенная
        дуга обязана быть без нулевого отрезка при любом выборе.
        """
        source = bpmn_layout._Box(100, 40, 100, 80)
        target = bpmn_layout._Box(400, 40, 100, 80)
        # Якоря передаются, а не снимаются с центра рамки: их выбирает
        # `_anchor`, и без событий это `cx` концов.
        ladder = bpmn_layout._detour(source.cx, source.y, target.cx, target.y,
                                     20, 20, 300)
        assert ladder[2] == ladder[3], (
            "лестница одного ряда больше не вырождается — проверка уснула")
        points = [(int(w.get("x")), int(w.get("y")))
                  for w in bpmn_layout._edge("F1", ladder).findall(WAYPOINT)]
        assert points == [(150, 40), (150, 20), (300, 20), (450, 20), (450, 40)]
        assert points[0] == ladder[0] and points[-1] == ladder[-1], (
            "чистка съела якорь дуги на границе фигуры")

    def test_cleaning_keeps_the_first_and_last_waypoint(self):
        """Чистка убирает нулевые отрезки, но не трогает якоря на границах
        концов: без них bpmn-js не пристегивает стрелку к фигуре."""
        raw = [(10, 20), (10, 20), (30, 20), (30, 20), (30, 40), (30, 40)]
        cleaned = bpmn_layout._clean_points(raw)
        assert cleaned == [(10, 20), (30, 20), (30, 40)]
        assert cleaned[0] == raw[0] and cleaned[-1] == raw[-1]

    def test_segment_through_box_is_rejected_in_both_directions(self):
        """Проверка препятствия не должна зависеть от направления дуги.

        Перевёрнутый знак `q` у отрицательных `p` объявлял свободным и
        горизонтальный, и вертикальный отрезок сквозь фигуру: ветка `p == 0`
        получала отрицательный `q` для точки внутри прямоугольника.
        """
        box = bpmn_layout._Box(100, 100, 100, 80)
        assert bpmn_layout._segment_hits_box(0, 140, 300, 140, box)
        assert bpmn_layout._segment_hits_box(150, 0, 150, 300, box)
        assert not bpmn_layout._segment_hits_box(0, 50, 300, 50, box)
        assert not bpmn_layout._segment_hits_box(0, 100, 300, 100, box)

    def test_fallback_pair_is_distinct_and_on_the_borders(self):
        """Когда путь сложился в одну точку (концы вплотную), дуга берётся по
        другому направлению, а не остаётся одной точкой."""
        source = bpmn_layout._Box(0, 0, 40, 40)
        target = bpmn_layout._Box(40, 0, 40, 40)
        points = bpmn_layout._fallback_points(source, target)
        assert len(points) == 2 and points[0] != points[1]
        assert on_border((0, 0, 40, 40), points[0]), points
        assert on_border((40, 0, 40, 40), points[1]), points

    def test_flow_with_a_missing_end_gets_no_di(self):
        plan = two_pools_plan()
        plan["flows"].append(_f("F9", "T1", "Нет_такого_узла"))
        plan["flows"].append(_f("F10", "", "T1"))
        root, _ = render(plan)
        edges = edges_of(root)
        assert "F9" not in edges and "F10" not in edges
        assert "M1" in edges, "проверка потеряла и законные дуги"


class TestDeterminism:
    """Один план — один побайтовый XML: diff версий не должен шуметь точками."""

    def test_two_calls_are_byte_identical(self):
        first, _ = render(two_pools_plan())
        second, _ = render(two_pools_plan())
        assert (ET.tostring(first, encoding="unicode")
                == ET.tostring(second, encoding="unicode"))

    def test_two_calls_on_a_boundary_heavy_plan_are_identical(self):
        first, _ = render(boundary_plan(8))
        second, _ = render(boundary_plan(8))
        assert (ET.tostring(first, encoding="unicode")
                == ET.tostring(second, encoding="unicode"))


class TestGarbageInput:
    """План недоверен: пустой вход обязан дать пустую графику, а не исключение."""

    def test_empty_plan_renders_an_empty_plane(self):
        root, _ = render({"participants": [], "lanes": [], "elements": [],
                          "flows": []})
        assert not list(root.iter(SHAPE))
        assert not list(root.iter(EDGE))

    def test_missing_keys_renders_an_empty_plane(self):
        root, _ = render({}, participant_ids={}, process_ids={})
        assert not list(root.iter(SHAPE))
        assert not list(root.iter(EDGE))

    def test_elements_as_a_string_raises_nothing(self):
        root, _ = render({"participants": ["ВкусВилл"], "lanes": [],
                          "elements": "x", "flows": "x"},
                         participant_ids={}, process_ids={})
        assert not list(root.iter(SHAPE))

    def test_items_without_ids_are_skipped(self):
        plan = {
            "participants": ["ВкусВилл"],
            "lanes": [{"name": "Дорожка без id", "participant": "ВкусВилл"}],
            "elements": [{"kind": "userTask", "name": "Шаг без id",
                          "participant": "ВкусВилл"},
                         {"id": "T1", "kind": "userTask", "name": "Есть id",
                          "participant": "ВкусВилл"}],
            "flows": [{"id": "", "source": "T1", "target": "T1"},
                      {"source": "T1", "target": "T1"}],
        }
        root, _ = render(plan)
        assert list(root.iter(EDGE)) == []
        assert "T1" in shapes_of(root)

    def test_non_dict_plan_raises_nothing(self):
        assert bpmn_layout.diagram(None, process_ids={}, participant_ids={})
        assert bpmn_layout.diagram([], process_ids=None, participant_ids=None)


class TestStrayPool:
    """Дорожка без пула: фигура дорожки не висит без накрывающей её фигуры."""

    def test_lane_shape_never_escapes_a_pool_shape(self):
        plan = stray_plan()
        root, participant_ids = render(plan)
        shapes = shapes_of(root)
        for lane in plan["lanes"]:
            if lane["id"] not in shapes:
                continue
            pool_ref = participant_ids.get(lane["participant"])
            assert pool_ref in shapes, (
                f"фигура дорожки {lane['id']} без фигуры пула — висячий IDREF")
            assert covers(shapes[pool_ref], shapes[lane["id"]])

    def test_no_shape_references_an_unknown_id(self):
        plan = stray_plan()
        root, participant_ids = render(plan)
        known = (element_ids(plan) | lane_ids(plan) | set(participant_ids.values())
                 | {f"Process_{i}" for i in range(1, len(participant_ids) + 1)})
        for ref in shapes_of(root):
            assert ref in known, f"фигура ссылается на {ref}, которого в плане нет"

    def test_stray_nodes_stay_on_the_diagram(self):
        """Узлы группы без пула обязаны остаться: потерянный шаг читался бы как
        потерянный участник, а имена шагов фронтенд берёт из схемы."""
        plan = stray_plan()
        root, _ = render(plan)
        shapes = shapes_of(root)
        for elem_id in element_ids(plan):
            assert elem_id in shapes, f"узел {elem_id} пропал со схемы"

    def test_stray_nodes_do_not_overlap_each_other(self):
        plan = stray_plan()
        root, _ = render(plan)
        shapes = shapes_of(root)
        boxes = sorted((ref, shapes[ref]) for ref in element_ids(plan))
        for index, (left, left_box) in enumerate(boxes):
            for right, right_box in boxes[index + 1:]:
                assert not overlap(left_box, right_box), (
                    f"шаги без пула {left} и {right} легли друг на друга")


class TestSharedNamespaceAndVocabulary:
    """Раскладка не заводит второй правдой ни префиксы, ни словарь тегов."""

    def test_namespace_constants_come_from_the_applier(self):
        assert bpmn_layout.BPMNDI_NS == bpmn_edits.BPMNDI_NS
        assert bpmn_layout.DC_NS == bpmn_edits.DC_NS
        assert bpmn_layout.DI_NS == bpmn_edits.DI_NS
        # Собственных имён пространств здесь быть не должно: их появление
        # означает, что модуль снова регистрирует свои префиксы на весь процесс.
        assert not hasattr(bpmn_layout, "OMGDC_NS")
        assert not hasattr(bpmn_layout, "OMGDI_NS")

    def test_module_does_not_touch_the_global_prefix_registry(self):
        """`ET.register_namespace` мутирует состояние процесса: импорт раскладки
        не имеет права менять префиксы чужой выдачи."""
        source = (REPO_ROOT / "core" / "bpmn_layout.py").read_text(
            encoding="utf-8")
        assert "register_namespace(" not in source

    def test_kind_vocabulary_is_the_applier_dictionary_itself(self):
        assert bpmn_layout.EVENT_KINDS is bpmn_edits.EVENT_TAGS
        assert bpmn_layout.GATEWAY_KINDS is bpmn_edits.GATEWAY_TAGS

    def test_di_tag_helper_matches_the_applier(self):
        for local in ("BPMNShape", "BPMNEdge", "BPMNPlane", "BPMNDiagram"):
            assert bpmn_layout._di(local) == f"{{{bpmn_edits.BPMNDI_NS}}}{local}"

    def test_event_and_gateway_sizes_follow_the_vocabulary(self):
        for kind in bpmn_edits.EVENT_TAGS:
            assert bpmn_layout._size(kind) == (bpmn_layout.EVENT_SIZE,
                                               bpmn_layout.EVENT_SIZE)
        for kind in bpmn_edits.GATEWAY_TAGS:
            assert bpmn_layout._size(kind) == (bpmn_layout.GATEWAY_SIZE,
                                               bpmn_layout.GATEWAY_SIZE)

    def test_serialized_diagram_uses_the_app_wide_prefixes(self):
        """Фронтенд ищет координаты регуляркой по `dc:Bounds`
        (`bpmn-constructor/src/bpmnLayout.js:19`), а `ET.register_namespace`
        мутирует реестр на весь процесс: собственные префиксы раскладки
        перекрашивали каждый XML приложения, и схема теряла геометрию.

        Сериализация — в отдельном процессе: реестр префиксов глобален, и в
        общем процессе pytest порядок импорта модулей мог бы закрыть след
        правки.
        """
        script = "\n".join([
            "import xml.etree.ElementTree as ET",
            "from core import bpmn_layout",
            "plan = {'participants': ['ВкусВилл'], 'lanes': [],",
            "        'elements': [{'id': 'T1', 'kind': 'userTask',"
            " 'name': 'Шаг', 'participant': 'ВкусВилл', 'lane': ''}],",
            "        'flows': [{'id': 'F1', 'source': 'T1', 'target': 'T1',"
            " 'kind': 'sequence'}]}",
            "root = bpmn_layout.diagram(plan, process_ids={'ВкусВилл': 'Process_1'},",
            "                           participant_ids={'ВкусВилл':"
            " 'Participant_1'})[0]",
            "print(ET.tostring(root, encoding='unicode'))",
        ])
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, cwd=str(REPO_ROOT), timeout=120)
        assert out.returncode == 0, out.stderr
        # `<dc:Bounds`, а не `dc:Bounds`: под строку `omgdc:Bounds` последняя
        # проверка подошла бы сама, и мутация осталась бы невидимой.
        assert "<dc:Bounds" in out.stdout, out.stdout
        assert "omgdc:" not in out.stdout and "omgdi:" not in out.stdout
        assert "ns0:" not in out.stdout

    def test_edges_serialize_with_di_prefixed_waypoints(self):
        plan = one_lane_plan()
        root, _ = render(plan)
        xml = ET.tostring(root, encoding="unicode")
        assert "<di:waypoint" in xml, "префикс `di` не выставлен"
        assert "omgdi:" not in xml

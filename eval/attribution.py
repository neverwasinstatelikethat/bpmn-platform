"""Кто именно породил дефект: атрибуция провалов инвариантов узлам контура.

Отчёт харнесса до этого момента говорил «схема не прошла `has_timer`», но не
говорил, кто в этом виноват: модель не предложила таймер, переспрос его не
вернул, починка вставила событие без обработки или XML-генератор потерял
определение. Разница между этими ответами — разница в том, что править, и без
неё каждый разбор стоил отдельного живого прогона.

Модуль работает только с фактами из трейса (`core.bpmn_generator.repair_structure`
записывает отпечаток плана до и после каждого шага) и с отчётом применения
пакета правок. Он не угадывает владельца по тексту причины: если трейс не
покрывает случай, он так и пишется — `неизвестно`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

OWNER_MODEL_FIRST = "модель:первый ответ"
OWNER_MODEL_REASK = "модель:переспрос"
OWNER_REASK_REJECTED = "переспрос отвергнут, правка не у модели"
OWNER_CLARIFY = "вопрос о принадлежности"
OWNER_XML = "генерация XML"
OWNER_FIXTURE = "содержание фикстуры"
OWNER_REPAIR = "починка"
OWNER_BASE = "дефект базовой схемы"
OWNER_APPLIER = "аплайер"
OWNER_PACKAGE = "пакет модели"
OWNER_UNKNOWN = "неизвестно"
# Второе измерение для «дефект пришёл в плане»: видел ли о нём план-гейт.
# «контур видел» чинят в `repair_structure` и переспросе, «не видел» — только
# в `plan_gaps` (или в промпте), и это разные очереди работы.
OWNER_SEEN = " (контур видел нарушение в плане)"
OWNER_BLIND = " (план-гейт про него молчал)"


def _entry_tokens(entry: str) -> Set[str]:
    """Узлы плана, которых касается запись отпечатка.

    Отпечаток хранит составной токен (`A2@Цех`, `flow:F2:A2->A3`), а инвариант
    называется по одному id или имени пула — поэтому токен раскладывается.
    """
    text = str(entry)
    if text.startswith("pool:"):
        return {text[5:]}
    if text.startswith("lane:"):
        return {text[5:]}
    if text.startswith("flow:"):
        flow_id, _, rest = text[5:].partition(":")
        source, _, target = rest.partition("->")
        return {part for part in (flow_id, source, target) if part}
    element, _, pool = text.partition("@")
    return {part for part in (element, pool) if part}


def _touched(step: Mapping[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for key in ("added", "removed"):
        for entry in step.get(key) or []:
            out |= _entry_tokens(entry)
    return out


def _was_in_the_plan(ids: Set[str], gaps: Sequence[str]) -> bool:
    """Нарушение пришло вместе с планом, а не появилось по дороге в починке.

    Стык по id: текст нарушения плана называет те же элементы, что и упавший
    инвариант. Сопоставлять формулировки нарушений с именами инвариантов
    нельзя — они разойдутся при первой же правке текста.
    """
    if not ids:
        return False
    return any(any(i in gap for i in ids) for gap in gaps or ())


def _repair_steps(trace: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    for node in trace:
        if node.get("node") == "починка структуры":
            return list(node.get("steps") or [])
    return []


def _clarify_notes(trace: Sequence[Mapping[str, Any]]) -> List[str]:
    for node in trace:
        if node.get("node") == "вопрос о принадлежности":
            return [str(n) for n in (node.get("notes") or [])]
    return []


def _reask(trace: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    return next((n for n in trace if n.get("node") == "переспрос плана"), None)


def _step_owner(step: Mapping[str, Any]) -> str:
    return f"{OWNER_REPAIR}:{step.get('step') or '?'}"


def attribute_generation(
        checks_xml: Mapping[str, Any],
        checks_structure: Mapping[str, Any],
        trace: Sequence[Mapping[str, Any]],
        gaps: Sequence[str] = ()) -> Dict[str, str]:
    """Владелец каждого проваленного инварианта итогового XML.

    Приоритет — от места, где дефект физически появился, к тому, кто его
    принёс: структура прошла, а XML упал → потеря на вычитке; нарушение
    значилось уже в плане → починку винить нельзя (она обязана была его убрать,
    но не придумала его); и только потом — шаг починки, который трогал эти
    элементы. Так в «починка:…» остаётся ровно то, что контур испортил сам.
    """
    steps = _repair_steps(trace)
    notes = _clarify_notes(trace)
    reask = _reask(trace)
    from_model = any(n.get("node") == "первый ответ модели" for n in trace)
    if not from_model:
        # Replay: план прислал не контур, а фикстура — винить модель харнесс
        # права не имеет.
        model_owner = OWNER_FIXTURE
    elif reask is not None:
        model_owner = (OWNER_MODEL_REASK if reask.get("kept") == "переспрос"
                       else OWNER_REASK_REJECTED)
    else:
        model_owner = OWNER_MODEL_FIRST
    out: Dict[str, str] = {}

    for name, check in checks_xml.items():
        if getattr(check, "ok", True) or not getattr(check, "applicable", True):
            continue
        ids = {str(i) for i in getattr(check, "ids", ()) or ()}
        structure_check = checks_structure.get(name)
        if structure_check is not None and structure_check.ok:
            out[name] = OWNER_XML
            continue
        if _was_in_the_plan(ids, gaps):
            out[name] = model_owner + OWNER_SEEN
            continue
        owner = ""
        for step in reversed(steps):
            if ids and (ids & _touched(step)):
                owner = _step_owner(step)
                break
        if not owner and ids and any(any(i in n for i in ids) for n in notes):
            owner = OWNER_CLARIFY
        if not owner:
            # Без id стык с нарушениями плана невозможен: утверждать, что
            # план-гейт молчал, харнесс не вправе.
            owner = model_owner + (OWNER_BLIND if ids else "")
        out[name] = owner
    return out


def _op_tokens(op: Mapping[str, Any]) -> Set[str]:
    """Элементы, которых касается запись применения/отказа операции."""
    keys = ("id", "source", "target", "participant", "attached_to", "flow",
            "name", "lane")
    return {str(op[k]) for k in keys if op.get(k)}


def attribute_improvement(
        checks_before: Mapping[str, Any],
        checks_after: Mapping[str, Any],
        applied: Sequence[Mapping[str, Any]],
        skipped: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Владелец провала улучшенной схемы.

    Ключевой разграничитель для `improve/pass@1`: инвариант падал и до правок —
    это зона генерации, а не пакета. Именно поэтому пакет, который ничего не
    сломал, но и не починил, перестаёт выглядеть «успешным».
    """
    out: Dict[str, str] = {}
    for name, check in checks_after.items():
        if getattr(check, "ok", True) or not getattr(check, "applicable", True):
            continue
        before = checks_before.get(name)
        if before is not None and not before.ok:
            out[name] = OWNER_BASE
            continue
        ids = {str(i) for i in getattr(check, "ids", ()) or ()}
        owner = ""
        for entry in skipped:
            if ids and (ids & _op_tokens(entry)):
                owner = f"{OWNER_APPLIER}:{entry.get('stage', 'план')}"
                break
        if not owner:
            for entry in applied:
                if ids and (ids & _op_tokens(entry)):
                    owner = f"{OWNER_PACKAGE}:{entry.get('op')}"
                    break
        out[name] = owner or (OWNER_UNKNOWN if applied or skipped
                              else OWNER_BASE)
    return out


def tally(*attributions: Mapping[str, str]) -> List[Tuple[str, int]]:
    """Сводка «кто породил дефекты» по прогону: чаще всего чинить надо лидера."""
    counts: Dict[str, int] = {}
    for attribution in attributions:
        for owner in attribution.values():
            counts[owner] = counts.get(owner, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def format_tally(rows: Iterable[Tuple[str, int]]) -> List[str]:
    rows = list(rows)
    if not rows:
        return ["проваленных инвариантов нет — атрибутировать нечего"]
    total = sum(count for _, count in rows)
    return [f"{owner} — {count} из {total}" for owner, count in rows]

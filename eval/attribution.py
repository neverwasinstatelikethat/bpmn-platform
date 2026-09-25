"""Кто именно породил дефект: атрибуция провалов инвариантов узлам контура.

Отчёт харнесса до этого момента говорил «схема не прошла `has_timer`», но не
говорил, кто в этом виноват: модель не предложила таймер, переспрос его не
вернул, починка вставила событие без обработки или XML-генератор потерял
определение. Разница между этими ответами — разница в том, что править, и без
неё каждый разбор стоил отдельного живого прогона.

Модуль работает только с фактами из трейса (`core.bpmn_generator.repair_structure`
записывает отпечаток плана до и после каждого шага) и с отчётом применения
пакета правок. Он не угадывает владельца по формулировке причины: пометки
починки и строки отказа читаются только как носитель id элемента, а
«применилось зря» опознаётся по машинным маркерам аплайера. Если трейс или
отчёт не покрывают случай, он так и пишется — `неизвестно`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.bpmn_edits import (NOOP_NOTE_MARKERS, OP_ELEMENT_FIELDS,
                             POOL_EMPTY_NOTE_MARKERS, REPAIR_OWN_NOTE_MARKERS,
                             UNROUTED_NOTE_MARKERS)

OWNER_MODEL_FIRST = "модель:первый ответ"
OWNER_MODEL_REASK = "модель:переспрос"
OWNER_REASK_REJECTED = "переспрос отвергнут, правка не у модели"
OWNER_REASK_UNTOUCHED = "модель:повтор не тронул это нарушение"
OWNER_CLARIFY = "вопрос о принадлежности"
OWNER_XML = "генерация XML"
OWNER_FIXTURE = "содержание фикстуры"
OWNER_REPAIR = "починка"
OWNER_BASE = "дефект базовой схемы"
OWNER_APPLIER = "аплайер"
# Отказ аплайера и снятие гаранта — два разных владельца, которых раньше
# объединяла одна строка «аплайер:<stage>». Аплайер, корректно отвергший
# операцию пакета (план/повтор), ничего не сломал: сломан пакет. А узел, снятый
# после починки за отсутствие маршрута, — работа гаранта связности, а не
# аплайера. Смешать их — значит отдать «аплайер» провалы, за которые он не
# отвечает.
OWNER_REFUSED = "отказано аплайером"
OWNER_GUARD = "откачено гарантом"
OWNER_PACKAGE = "пакет модели"
OWNER_UNKNOWN = "неизвестно"
# Починка поправила дерево САМА (тип подменён, дуга снята, событие достроено) —
# в отчёте применения по такой правке нет ни applied-строки, ни отказа, поэтому
# единственный след — пометка `validate_and_repair`. Называть её «неизвестно»
# означало бы оставить «сломал то, что работало» без владельца.
OWNER_REPAIR_SOLO = f"{OWNER_REPAIR}:изменение без операции модели"
# Пакет откачен гарантом целиком: на выходе тот же XML, что и до правок,
# поэтому провал в нём — болезнь базы, а не пакета. Приписка обязана называть
# и снявшего: без неё «дефект базовой схемы» неотличим от «пакет даже не
# пытались применить».
OWNER_ROLLED_BACK = " (пакет откачен гарантом)"
# Строка применения, после которой дерево не изменилось: «операция была» только
# в отчёте. Без этой приписки `пакет модели:<op>` читался бы как «правка сломала
# схему», хотя сломать она ничего не могла.
OWNER_NOOP = " (применилось без изменения схемы)"
# Скоринг просел на правилах, которые на базе проходили, — значит ухудшение
# внесено пакетом, но ни одна строка отчёта его не называет.
OWNER_REGRESSED = " (ухудшение без привязки к операции)"
# Раунд применения пишет stage по-английски ("plan"/"retry"/"repair" в
# `core.llm_improve` и в `_apply_package` харнесса), отчёт — по-русски.
# Фолбэк «план» в прежнем словаре не совпадал ни с одним из этих значений,
# Поэтому наружу уходил гибрид двух языков «аплайер:plan».
_STAGE_RU = {"plan": "план", "retry": "повтор", "repair": "починка"}
# Какие классов плана касается нарушение инварианта. Имена — из
# `core.bpmn_generator._GAP_CLASS_OF`; сверка имён держится тестом, потому что
# разошедшийся словарь молча вернул бы прежнюю ложь: «отвергнутый переспрос»
# приписывался контуру и тогда, когда второй ответ модели не принёс ничего
# (прогон #53 — 9 из 18 таких отказов, и «крупнейший владелец провалов» был
# артефактом разметки, а не рычагом).
_REASK_REPAIRS = {
    "expected_participants": ("лицо без пула", "участник вне схемы",
                              "роль без хозяина", "пустой пул",
                              "роль-пул"),
    "roles_as_lanes": ("роль без хозяина", "роль-пул", "лицо без пула",
                       "пустой пул"),
    "participant_interacts": ("участник вне схемы", "пустой пул"),
    "has_timer": ("таймер",),
    "has_branching": ("развилка",),
}
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
    # `A2@Цех` — про элемент A2, а не про пул «Цех»: имя пула в токене нужно,
    # чтобы увидеть перенос шага между пулами. Иначе добавление стартового
    # события в чужой пул приписывало бы этому шагу все дефекты самого пула.
    return {text.partition("@")[0]}


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

    def _plan_owner(name: str) -> str:
        """Кому принадлежит нарушение, пришедшее из плана.

        Развилка не в «принят ли повтор», а в том, нёс ли он правку этого
        нарушения: отказ от пустого повтора — второй ответ модели, а не решение
        контура. Без повтора вовсе владелец остаётся у первого ответа.
        """
        if not from_model:
            # Replay: план прислал не контур, а фикстура — винить модель харнесс
            # права не имеет.
            return OWNER_FIXTURE
        if reask is None:
            return OWNER_MODEL_FIRST
        if reask.get("kept") == "переспрос":
            return OWNER_MODEL_REASK
        if reask.get("gaps_after") is None:
            return OWNER_MODEL_FIRST
        closed = reask.get("fixed_kinds") or {}
        if any(closed.get(kind) for kind in _REASK_REPAIRS.get(name, ())):
            return OWNER_REASK_REJECTED
        return OWNER_REASK_UNTOUCHED

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
            out[name] = _plan_owner(name) + OWNER_SEEN
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
            owner = _plan_owner(name) + (OWNER_BLIND if ids else "")
        out[name] = owner
    return out


def _op_tokens(op: Mapping[str, Any]) -> Set[str]:
    """Элементы, которых касается запись применения/отказа операции."""
    keys = ("id", "source", "target", "participant", "attached_to", "flow",
            "name", "lane")
    return {str(op[k]) for k in keys if op.get(k)}


def _skip_owner(entry: Mapping[str, Any]) -> str:
    """Владелец провала по записи отказа.

    «аплайер» остаётся за настоящим случаем поломки — раундом, которого словарь
    не знает; план и повтор — корректный отказ пакету модели, а снятие узла
    после починки — решение гаранта маршрута.
    """
    stage = str(entry.get("stage") or "plan")
    ru = _STAGE_RU.get(stage)
    if ru is None:
        return f"{OWNER_APPLIER}:{stage}"
    if stage == "repair":
        return f"{OWNER_GUARD}:{ru}"
    return f"{OWNER_REFUSED}:{ru}"


def _op_key(entry: Mapping[str, Any]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    """«Операция и элемент, который она правит» — тот же ключ, что у сверки
    повтора в оркестраторе (`core.llm_improve._op_key`), и поля те же:
    `bpmn_edits.OP_ELEMENT_FIELDS`.

    Нагрузку правки (`name`, `after`, `to`, `condition`) в ключ брать нельзя:
    корректирующий вызов как раз и приносит её исправленной, и зачёт не
    ставился бы никогда. Импорт вместо своего списка: разошедшиеся ключи
    молча вернули бы ту ложь, от которой эту правку завели.
    """
    key = tuple((field, str(entry[field])) for field in OP_ELEMENT_FIELDS
                if entry.get(field))
    if not key and entry.get("name"):
        key = (("name", str(entry["name"])),)
    return (str(entry.get("op") or ""), key)


def _closed_refusals(skipped: Sequence[Mapping[str, Any]],
                     applied: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Отказы, которых в списке владельцев быть не может.

    `reapplied=True` — «отказ первого раунда повтором закрыт»: правка в схеме,
    контур её сделал, и строка осталась только как история. Без этого самым
    крупным «владельцем провалов» прогона становится бухгалтерия отказов, а не
    тот, кто схему испортил (тот же артефакт разметки, что в сводке генерации
    с «отвергнутым переспросом»). Флаг ставит оркестратор, и полагаться только
    на него нельзя: строка раунда без флага закрывается по идентичности
    операции — отказ по тому же (op, элемент), что потом применилось, тоже
    закрытый отказ.
    """
    done = {_op_key(entry) for entry in applied}
    out: List[Mapping[str, Any]] = []
    for entry in skipped:
        if entry.get("reapplied"):
            continue
        key = _op_key(entry)
        if key[1] and key in done:
            continue
        out.append(entry)
    return out


def _noop_row(row: Mapping[str, Any]) -> bool:
    """«Применилось зря»: аплайер отчитался строкой в `applied`, но дерево не
    тронул. Внести провал в схему, которая раньше проходила, такая правка не
    могла, — но отчёт о ней обязан сказать, а не молчать."""
    note = str(row.get("note") or "")
    return any(marker in note for marker in NOOP_NOTE_MARKERS)


def _applied_owner(row: Mapping[str, Any], noop: bool = False) -> str:
    owner = f"{OWNER_PACKAGE}:{row.get('op') or '?'}"
    if noop or _noop_row(row):
        owner += OWNER_NOOP
    return owner


def _repair_own_notes(notes: Iterable[str]) -> List[str]:
    """Пометки, где починка изменила схему САМА, а не попросила доделать модель.

    Тот же срез, что у `core.llm_improve._repair_own_notes`: «узел вне
    маршрута» и «пул без шагов» — требование к пакету, а не правка починки, и
    назвать её владельцем внесённого провала значило бы переложить урон
    операции на того, кто его не наносил.
    """
    covered = UNROUTED_NOTE_MARKERS + POOL_EMPTY_NOTE_MARKERS
    return [note for note in notes
            if any(m in note for m in REPAIR_OWN_NOTE_MARKERS)
            and not any(m in note for m in covered)]


def _refusal_suffix(facts: Mapping[str, Any]) -> str:
    """Чем отличается «отказ, после которого контур даже не переспросил» от
    «отказа, который повтор увидел и не перебил»: чинят их в разных местах.

    Пустая строка, когда фактов повтора в отчёте нет (харнесс хранит
    отсутствующий факт как None): выдумывать решение контура по отсутствию
    записи атрибуция не вправе.
    """
    attempted = facts.get("retry_attempted")
    if attempted is None:
        return ""
    if not attempted:
        return " (корректирующий раунд не вызывался)"
    closed = facts.get("retry_closed")
    if closed is None:
        return ""
    if not int(closed):
        return " (повтор не закрыл ни одного отказа)"
    return ""


# Порядок подозреваемых зависит от того, проходил ли инвариант на базовой
# схеме. «Сломал то, что работало» — физическое изменение: гарант, применившаяся
# операция, самовольная правка починки; отказ тут последний, потому что он
# ничего не менял. Когда о базе ничего не известно, отказ возвращается на
# второе место: он хотя бы называет правку, которая не доехала.
_ORDER_INTRODUCED = ("guard", "applied", "repair", "refused")
_ORDER_UNVERIFIED = ("guard", "refused", "applied", "repair")


def attribute_improvement(
        checks_before: Mapping[str, Any],
        checks_after: Mapping[str, Any],
        applied: Sequence[Mapping[str, Any]],
        skipped: Sequence[Mapping[str, Any]],
        *,
        report: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """Владелец провала улучшенной схемы.

    Ключевой разграничитель для `improve/pass@1`: инвариант падал и до правок —
    это зона генерации, а не пакета. Именно поэтому пакет, который ничего не
    сломал, но и не починил, перестаёт выглядеть «успешным».

    `report` — отчёт применения пакета (`apply_and_guarantee`/оркестратор):
    `package_reverted`, `rules_regressed`, `noop_rows`, `retry_attempted`,
    `retry_closed`, `notes`. Факультативен: без него атрибуция остаётся прежней
    и не додумывает владельца там, где отчёт молчит.

    «Отказался применить» и «сломал то, что работало» здесь — разные владельцы:
    отказ дерево не трогает, поэтому провал, которого на базе не было, ему
    отдаётся только когда больше назвать нечего (см. `_ORDER_INTRODUCED`).
    """
    facts: Mapping[str, Any] = report or {}
    reverted = str(facts.get("package_reverted") or facts.get("reverted") or "")
    own_edits = _repair_own_notes(str(n) for n in (facts.get("notes")
                                                   or facts.get("repair_notes")
                                                   or ()))
    regressed = facts.get("rules_regressed") or {}
    open_skips = _closed_refusals(skipped, applied)
    # Строки, которые тронули дерево. Агрегат `noop_rows` перекрывает случай,
    # когда пометка в строку не попала (её теряет склейка раундов).
    changed = [row for row in applied if not _noop_row(row)]
    if applied and int(facts.get("noop_rows") or 0) >= len(applied):
        changed = []

    out: Dict[str, str] = {}
    for name, check in checks_after.items():
        if getattr(check, "ok", True) or not getattr(check, "applicable", True):
            continue
        before = checks_before.get(name)
        if before is not None and not before.ok:
            out[name] = OWNER_BASE
            continue
        if reverted and not changed:
            # Пакет сняли целиком, и менять в схеме было нечего: на выходе
            # базовый XML. Любой провал в нём живёт в генерации, а строки
            # отказа только объясняют, почему улучшение не доехало, — отдавать
            # им вину значит смешать «отказался применить» с «сломал».
            out[name] = OWNER_BASE + OWNER_ROLLED_BACK
            continue
        # До правок проверка проходила — либо не применялась к базе вовсе
        # (`_na` даёт ok=True), что тоже значит «нового провала в базе не было».
        introduced = before is not None
        ids = {str(i) for i in getattr(check, "ids", ()) or ()}
        hits = [entry for entry in open_skips if ids & _op_tokens(entry)]
        guard = next((e for e in hits if str(e.get("stage") or "") == "repair"), None)
        refused = next((e for e in hits if e is not guard), None)
        row = (next((a for a in changed if ids & _op_tokens(a)), None)
               or next((a for a in applied if ids & _op_tokens(a)), None))
        owners = {
            "guard": _skip_owner(guard) if guard is not None else "",
            "refused": (_skip_owner(refused) + _refusal_suffix(facts)
                        if refused is not None else ""),
            "applied": _applied_owner(row, noop=not changed) if row is not None else "",
            "repair": (OWNER_REPAIR_SOLO if ids and any(
                any(i in note for i in ids) for note in own_edits) else ""),
        }
        owner = next(
            (owners[key] for key in
             (_ORDER_INTRODUCED if introduced else _ORDER_UNVERIFIED)
             if owners[key]), "")
        if not owner and introduced and regressed:
            owner = OWNER_UNKNOWN + OWNER_REGRESSED
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

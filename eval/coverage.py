"""Покрытие детекции: чем линейка смотрит на реальные схемы, а не только на фикстуры.

Фикстур в харнессе одиннадцать, и они построены вокруг того, что правила уже
умеют находить. Такую слепоту линейки на них поймать нельзя: правило, которое
меряет не те узлы, на которые указывает свой совет, проходит весь eval и молчит
там, где пользователь загружает схему из bpmn-js. Замер по корпусу датасета
(367 файлов, рукописный BPMN) это показал сразу: `naming` снимал балл за 15380
узлов, из которых задачами были 18 (имена у событий и шлюзов нота не требует),
`documentation` считал долю по всем узлам, а правку просил у шагов (18552
нарушителя, из них задач 1828), а `approval_chain` не сработал на корпусе ни
разу, потому что считал только `userTask` внутри названной дорожки.

Отчёт ничего не гейтит по умолчанию: доля нарушений на реальном корпусе —
свойство датасета (это учебные схемы), а не качество контура. Измеряется и
пинится тестами другое: у каждого правила есть момент, когда оно говорит «нарушение»
или хотя бы «проверяю», и нарушение называет те узлы, которым соответствует
операция из его же `action`.

Второй замер того же модуля — сверка скоринга с независимым оракулом по всему
корпусу (`agreement_census`). Он отвечает на вопрос «не прощает ли линейка то,
что видит второй, независимый читатель процесса», и отвечает на данных, которых
нет в одиннадцати фикстурах.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from core.bpmn_scoring import ACTIVITY_TAGS, BPMNScorer, _local
from eval.invariants import (NO_DATA, ORACLE_STRICTER, SCORER_STRICTER,
                             business_agreement, check_xml)

# Правила, которые ОБЯЗАНЫ находить нарушение на реальном корпусе: они про
# форму, а корпус рукописный и полным формой нарушений. Правило из этого списка,
# замолчавшее на всём датасете, — слепое пятно, и тест это ловит.
MUST_FIRE_ON_CORPUS = ("naming", "documentation", "approval_chain",
                       "wait_without_sla", "event_types",
                       # Два правила границ потока: их принёс сам корпус, и
                       # молчание на корпусе для них — не «редкий случай», а
                       # потерянный класс (до них ни один слой не видел 8 дуг
                       # в 5 схемах и 8 обменов в 6 схемах).
                       "cross_pool_flow", "message_flow_ends",
                       # Мёртвые таймеры держат 108 схем из 367: правило,
                       # которое молчит на таком корпусе, — не «редкий случай».
                       "timer_without_schedule")

# Нотационный слой для той же сверки: у каждого правила скоринга здесь — свой
# независимый читатель в оракуле. Продуктовый гейт (`scorer_oracle_agreement`)
# сверяет только бизнес-правила, и именно поэтому в нём не всплыли 35 схем, где
# оракул не замечал обмен концом на пул, и 2 схемы, где висячий поток прятал
# тупик: сравнение слепой зоны не видит.
NOTATION_PAIRS: Mapping[str, str] = {
    "pool_has_steps": "pool_has_steps",
    "participant_interacts": "participant_interacts",
    "role_pools": "roles_as_lanes",
    "gateway_split_join": "gateway_split_join",
    "gateway_conditions": "gateway_conditions_or_default",
    "event_types": "event_definitions",
    # Тип события и его срок — разные вещи: `event_types` читает наличие
    # `*EventDefinition`, `timer_without_schedule` — значение хронометража.
    "timer_without_schedule": "timer_schedule",
    "boundary_events": "boundary_handled",
    "no_isolated": "no_unrouted",
    "cross_pool_flow": "flows_within_pool",
    "message_flow_ends": "message_flow_ends",
    "flow_ends_legal": "flow_ends_legal",
    # Безусловный цикл: линейка перебирает теги `out_targets` над `_Schema`,
    # оракул идёт по `seq_out` своего `_Graph` и берёт `kind` узла.
    "guarded_cycles": "loops_have_a_guard",
}


def corpus_files(dataset_path: Optional[str] = None,
                 limit: Optional[int] = None) -> List[tuple]:
    """(имя файла, содержимое) по `.bpmn` корпуса: имя — адрес, по которому
    находку можно открыть глазами и по которому `eval/corpus_topics.py` знает
    тему схемы."""
    if dataset_path is None:
        from core.llm_improve import DATASET_PATH
        dataset_path = DATASET_PATH
    paths = sorted(glob.glob(os.path.join(dataset_path, "*.bpmn")))
    if limit:
        paths = paths[:limit]
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                out.append((os.path.basename(path), handle.read()))
        except OSError:
            continue
    return out


def xml_samples(dataset_path: Optional[str] = None,
                limit: Optional[int] = None) -> List[str]:
    """Содержимое `.bpmn` датасета как выборка реальных схем."""
    return [xml for _, xml in corpus_files(dataset_path, limit)]


def census(scorer: Any, xmls: Iterable[str],
           tags_of: Optional[Any] = None) -> Dict[str, Any]:
    """Сводка по каждому правилу: сколько схем оно ругает, сколько элементов
    называет и где отказывается проверять."""
    per_rule: Dict[str, Dict[str, int]] = {}
    parsed = 0
    offenders_by_tag: Dict[str, Dict[str, int]] = {}
    for xml in xmls:
        result = scorer.evaluate(xml)
        meta = result.get("details_meta") or {}
        if not meta:
            continue
        parsed += 1
        for rule, info in meta.items():
            bucket = per_rule.setdefault(rule, {"failed": 0, "passed": 0,
                                                "not_applicable": 0,
                                                "offenders": 0})
            status = info.get("status")
            if status == "failed":
                bucket["failed"] += 1
            elif status == "passed":
                bucket["passed"] += 1
            else:
                bucket["not_applicable"] += 1
            ids = info.get("elements") or []
            bucket["offenders"] += len(ids)
            if tags_of is not None and ids:
                kinds = offenders_by_tag.setdefault(rule, {})
                for node_id in ids:
                    tag = tags_of(xml).get(node_id) or "?"
                    kinds[tag] = kinds.get(tag, 0) + 1
    return {"schemes": parsed, "rules": per_rule, "offenders_by_tag": offenders_by_tag}


def dead_rules(report: Dict[str, Any]) -> List[str]:
    """Правила, которые на всей выборке ни разу не сказали «нарушение»."""
    return sorted(rule for rule, bucket in report["rules"].items()
                  if not bucket["failed"])


def offenders_are_activities(report: Dict[str, Any], rule: str) -> Optional[bool]:
    """Все ли нарушители правила — активности (а не события и шлюзы).

    None — правило никого не назвало: проверять нечего, это не подтверждение.
    """
    kinds = (report.get("offenders_by_tag") or {}).get(rule)
    if not kinds:
        return None
    return all(tag in ACTIVITY_TAGS for tag in kinds)


def tags_of(xml: str) -> Dict[str, str]:
    """id → локальное имя тега, чтобы разбор нарушителей не зависел от скоринга."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    return {elem.get("id"): _local(elem.tag)
            for elem in root.iter()
            if isinstance(elem.tag, str) and elem.get("id")}


# Классы расхождения двух прочтений. Вердикт `oracle_stricter` сам по себе
# бессильно читать в отчёте: под него попадают и «скоринг увидел ожидание, но
# счёл срок смоделированным», и «скоринг туда не смотрел вовсе» — первая из них
# значит, что линейка прощает дефект, вторая — что у неё другой знаменатель.
# Разбор идёт по паре (что сказал скоринг → что сказал оракул).
CLASS_OF_PAIR = {
    ("passed", "failed"): "forgiven",
    ("not_applicable", "failed"): "unseen",
    ("failed", "passed"): "overcharged",
    ("failed", "not_applicable"): "overcharged",
}
CLASS_LABELS = {
    "forgiven": "простили (дефект видит только оракул)",
    "unseen": "не смотрели (скоринг счёл, что проверять нечего)",
    "overcharged": "скоринг строже (дефект видит только он)",
}

# Расхождения, объявленные в замысле, а не найденные промахи. `rework_loop`:
# слои
# спрашивают в разных местах — оракул различимость ног развилки (и читает
# подпись дуги), скоринг охрану на дугах, отпускающих цикл; на корпусе это
# всегда «скоринг строже». `lane_overload` из этого списка вычеркнут по
# измерению: оракул держал один порог 75% на пулы с двумя и более дорожками,
# скоринг при трёх и более дорожках брал 60%, и на корпусе это было 18 схем,
# где продукт ругался, а независимая приёмка молчала. Оракул переведён на те же
# два порога; знаменатель остался своим (скоринг делит на работы размеченных
# дорожек, оракул — на все шаги пула), и это расхождение в docstrings
# записано. `gateway_split_join` так и не попал в список:
# его расхождение (6 схем) оказалось не разными порогами, а разными прочтениями
# нотации — оракул требовал схождения у event-развилки, где нескольких токенов не
# бывает, и не смотрел на inclusive, где бывают. Оба слоя исправлены, и вся
# нотационная таблица сходится на корпусе целиком. Всё остальное — сигнал, что
# один слой начал прощать то, что видит другой.
DOCUMENTED_DIVERGENCES = {
    ("rework_loop", "overcharged"),
}


def unexplained(report: Dict[str, Any]) -> List[str]:
    """Расхождения вне задокументированного списка: «правило: класс»."""
    out = []
    for rule, counts in (report.get("by_rule") or {}).items():
        for klass, number in counts.items():
            if klass in ("agree", "diverged") or not number:
                continue
            if (rule, klass) not in DOCUMENTED_DIVERGENCES:
                out.append(f"{rule}: {klass}")
    return sorted(out)


def agreement_census(scorer: Any, xmls: Iterable[str],
                     names: Optional[Sequence[str]] = None,
                     pairs: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Где два прочтения одной схемы расходятся на реальных схемах корпуса.

    Скоринг (`core/bpmn_scoring.py`) и оракул (`eval/invariants.py`) читают один
    бизнес-вопрос независимо друг от друга, и их сверка — единственная проверка
    линейки, которой не нужна человеческая разметка «где правильно». На
    одиннадцати фикстурах харнесс видит расхождение, только если оно попало в
    набор; корпус из 367 рукописных схем показывает классы, которых в наборе нет
    вовсе. Этим уже поймано настоящее: оракул принимал параллельную ветку
    «срок вышел», а скоринг требовал таймер на самом ожидании и советовал правку,
    которую аплайер на catch-событие не принимает.

    Основой служит `business_agreement` по паре правил из `SCORING_TO_ORACLE`, а
    расхождения дополнительно раскладываются по `CLASS_OF_PAIR`, потому что
    «оракул строже» — это и прощённый дефект, и другой знаменатель.

    `names` — имена файлов той же выборки: расхождение без адреса невозможно
    разобрать, а номер в отсортированном списке для человека не адрес.
    """
    by_rule: Dict[str, Dict[str, int]] = {}
    witnesses: Dict[str, List[str]] = {}
    parsed = 0
    for number, xml in enumerate(xmls):
        witness = (names[number] if names is not None and number < len(names)
                   else f"#{number}")
        result = scorer.evaluate(xml)
        meta = result.get("details_meta") or {}
        if not meta:
            continue
        parsed += 1
        try:
            checks = check_xml(xml)
        except Exception:  # noqa: BLE001 — оракул не должен ронять перепись
            continue
        for rule, row in business_agreement(checks, result, pairs).items():
            bucket = by_rule.setdefault(rule, {"agree": 0, "diverged": 0})
            verdict = row.get("verdict") or NO_DATA
            if verdict not in (ORACLE_STRICTER, SCORER_STRICTER):
                bucket["agree"] += 1
                continue
            bucket["diverged"] += 1
            klass = CLASS_OF_PAIR.get((row["scorer"], row["oracle"]),
                                      f"{row['scorer']}→{row['oracle']}")
            bucket[klass] = bucket.get(klass, 0) + 1
            witnesses.setdefault(f"{rule}: {klass}", []).append(witness)
    return {"schemes": parsed, "by_rule": by_rule, "witnesses": witnesses}


def disagreements(report: Dict[str, Any]) -> List[str]:
    """Правила, где два прочтения хотя бы раз не совпали на реальном корпусе."""
    return sorted(rule for rule, counts in (report.get("by_rule") or {}).items()
                  if counts.get("diverged"))


def format_report(report: Dict[str, Any], rules: Sequence[str],
                  agreement: Optional[Dict[str, Any]] = None) -> List[str]:
    lines = [f"схем в выборке: {report['schemes']}"]
    for rule in rules:
        bucket = report["rules"].get(rule) or {}
        kinds = (report.get("offenders_by_tag") or {}).get(rule) or {}
        top = ", ".join(f"{tag}×{count}" for tag, count in
                        sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"  {rule:22} ругает {bucket.get('failed', 0):4} схем | "
                     f"названо элементов {bucket.get('offenders', 0):6} | "
                     f"не проверяет {bucket.get('not_applicable', 0):4}"
                     + (f" | нарушители: {top}" if top else ""))
    dead = dead_rules(report)
    lines.append("  ни разу не сработало на реальных схемах: "
                 + (", ".join(dead) if dead else "нет таких"))
    if agreement is not None:
        lines += format_agreement(agreement)
    return lines


def format_agreement(agreement: Dict[str, Any],
                     title: str = "сверка двух прочтений (скоринг против "
                                  "независимого оракула)") -> List[str]:
    """Сводка по одной таблице соответствия: бизнес-слой и нотационный слой
    печатаются одним кодом, потому что читаются одинаково."""
    lines = [f"{title} на {agreement['schemes']} схемах:"]
    for rule, counts in sorted((agreement.get("by_rule") or {}).items()):
        parts = [f"сошлись {counts.get('agree', 0):4}",
                 f"разошлись {counts.get('diverged', 0):4}"]
        for klass in sorted(k for k in counts if k not in ("agree", "diverged")):
            parts.append(f"{CLASS_LABELS.get(klass, klass)} {counts[klass]:4}")
        lines.append(f"  {rule:22} " + " | ".join(parts))
    bad = disagreements(agreement)
    lines.append("  расходятся хотя бы на одной схеме: "
                 + (", ".join(bad) if bad else "ни по одному правилу"))
    odd = unexplained(agreement)
    lines.append("  кроме объявленных в замысле: "
                 + (", ".join(odd) if odd
                    else "пусто — расходятся только там, где договорились"))
    for key, names in sorted((agreement.get("witnesses") or {}).items())[:6]:
        lines.append(f"    {key}: " + ", ".join(names[:3])
                     + (" …" if len(names) > 3 else ""))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Покрытие детекции скоринга на реальных схемах корпуса")
    parser.add_argument("--limit", type=int, default=0,
                        help="сколько файлов корпуса читать (0 — все)")
    parser.add_argument("--no-agreement", action="store_true",
                        help="не сверять скоринг с оракулом (быстрый разбор только "
                             "по нарушениям)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    scorer = BPMNScorer()
    files = corpus_files(limit=args.limit or None)
    xmls = [xml for _, xml in files]
    names = [name for name, _ in files]
    report = census(scorer, xmls, tags_of=tags_of)
    agreement = (None if args.no_agreement
                 else agreement_census(scorer, xmls, names=names))
    rules = list((scorer.rules if hasattr(scorer, "rules") else report["rules"]))
    for line in format_report(report, rules, agreement):
        print(line)
    if agreement is not None:
        notation = agreement_census(scorer, xmls, names=names,
                                    pairs=NOTATION_PAIRS)
        for line in format_agreement(notation, "нотационный слой"):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    raise SystemExit(main())

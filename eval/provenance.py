"""Происхождение оцениваемых схем: метрика не должна считаться по образцу.

Часть образцов модель видит в самом промпте: генерационный системный промпт
содержит few-shot план, промпт улучшения — few-shot пакет операций. Если
описание сценария, его задача улучшения или «эталонная» фикстура совпадают с
таким образцом, пройденный инвариант меряет копирование, а не способность
контура. Модуль находит эти пересечения и называет их поимённо:

* `eval/run.py` печатает раздел «Провенанс кейсов» и кладёт тот же разбор в
  отчёт — по нему видно, какой метрике нельзя верить;
* `tests/test_eval_provenance.py` держит планку: новые сценарии и фикстуры не
  должны добавлять пересечений, а утратившийся маркер образца считается ошибкой
  (промпт без образца харнесс слепнет молча).

Здесь нет порога «достаточно хорошо»: список находок — это описание факта, а
решение о том, что остаётся в наборе кейсов, принимает человек.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from eval.scenarios import Scenario, all_scenarios

# Слова короче четырёх символов — служебные: на них пересечения не считаются.
_WORD_RE = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)

# Доля содержательных слов одного текста, встреченных в другом. 0.25 — уровень
# «совпала тема» (у непересекающихся сценариев замерено ≤0.16).
TEXT_CONTAINMENT = 0.25
# Совпадение имён элементов фикстуры с именами образца: 0.5 = «план переписан».
NAME_OVERLAP = 0.5

GEN_MARKERS = ("Пример. ", "\n\nЗдесь ")
IMPROVE_MARKER = "ПРИМЕР ОТВЕТА"
# Промпт состава модель читает целиком: и правила, и пример. Маркер — начало
# блока с ответом примера; без него проверять нечего (правила состава тоже
# текстом попадают в находки, но имена снимаются с ответа).
ROSTER_ID = "roster_composition"
ROSTER_EXAMPLE_HEAD = "Пример («"
# Промпт переспроса отдаёт модели не пример ответа, а СХЕМУ заплатки с
# подписанными примерами имён — для неё это то же самое, что few-shot:
# «Перевозчик» и «Водитель» в этой схеме были ответом сцены warehouse_delivery,
# и `expected_participants` мерил копирование, а не контур.
RETRY_ID = "retry_patch"


def words(text: Any) -> Set[str]:
    """Множество содержательных слов текста (нижний регистр, длина ≥ 4)."""
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def containment(source: Set[str], against: Set[str]) -> float:
    """Сколько слов из `source` встречается в `against`.

    Асимметрично намеренно: короткий запрос пользователя целиком живёт в длинном
    тексте образца — это и есть утечка, а не «образец длинный».
    """
    if not source or not against:
        return 0.0
    return len(source & against) / len(source)


def _json_object(text: str) -> Optional[Dict[str, Any]]:
    """Первый JSON-объект блока (образцы в промптах — один объект без обёртки)."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for pos in range(start, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                try:
                    loaded = json.loads(text[start:pos + 1])
                except ValueError:
                    return None
                return loaded if isinstance(loaded, dict) else None
    return None


def _payload_names(payload: Optional[Dict[str, Any]]) -> Set[str]:
    """Имена элементов/операций образца — то, что модель способна переписать."""
    names: Set[str] = set()
    for key in ("elements", "operations", "lanes", "participants",
                "organizations", "systems", "counterparties", "roles"):
        for item in (payload or {}).get(key) or []:
            if isinstance(item, dict):
                value = item.get("name")
            else:
                value = item
            if value:
                names.add(str(value))
    return names


def examples() -> List[Dict[str, Any]]:
    """Few-shot образцы, которые промпты отдают модели вместе с заданием."""
    from core import bpmn_generator, llm_improve

    found: List[Dict[str, Any]] = []

    gen_prompt = str(bpmn_generator._SYSTEM_PROMPT)
    head, tail = GEN_MARKERS
    if head not in gen_prompt or tail not in gen_prompt:
        raise ValueError(
            f"в промпте генерации нет маркеров образца {head!r}/{tail!r} — "
            "провенанс нечего проверять; обновите eval/provenance.py вместе с "
            "промптом")
    start = gen_prompt.index(head) + len(head)
    block = gen_prompt[start:gen_prompt.index(tail, start)]
    found.append({
        "id": "generation_plan",
        "where": "core.bpmn_generator._SYSTEM_PROMPT",
        "text": block,
        "payload": _json_object(block),
        "names": _payload_names(_json_object(block)),
    })

    imp_prompt = str(llm_improve._SYSTEM_PROMPT)
    if IMPROVE_MARKER not in imp_prompt:
        raise ValueError(
            f"в промпте улучшения нет маркера {IMPROVE_MARKER!r} — провенанс "
            "нечего проверять; обновите eval/provenance.py вместе с промптом")
    block = imp_prompt[imp_prompt.index(IMPROVE_MARKER) + len(IMPROVE_MARKER):]
    found.append({
        "id": "improve_package",
        "where": "core.llm_improve._SYSTEM_PROMPT",
        "text": block,
        "payload": _json_object(block),
        "names": _payload_names(_json_object(block)),
    })

    # Промпт состава модель читает целиком: и правила, и пример. Поэтому
    # текстом образца считается весь промпт — имя пула утекает и в правило,
    # — а имена берутся из разобранного ответа примера: до него в промпте
    # напечатана схема ответа с плейсхолдерами, и первый объект — не ответ.
    roster_prompt = str(bpmn_generator._ROSTER_SYSTEM_PROMPT)
    if ROSTER_EXAMPLE_HEAD not in roster_prompt:
        raise ValueError(
            f"в промпте состава нет маркера {ROSTER_EXAMPLE_HEAD!r} — провенанс "
            "нечего проверять; обновите eval/provenance.py вместе с промптом")
    roster_payload = _json_object(roster_prompt[
        roster_prompt.index(ROSTER_EXAMPLE_HEAD) + len(ROSTER_EXAMPLE_HEAD):])
    found.append({
        "id": ROSTER_ID,
        "where": "core.bpmn_generator._ROSTER_SYSTEM_PROMPT",
        "text": roster_prompt,
        "payload": roster_payload,
        "names": _payload_names(roster_payload),
    })

    # Схема заплатки: имена стоят в самой схеме, и примера ответа там нет —
    # разбирать надо весь текст промпта переспроса.
    retry_prompt = str(bpmn_generator._RETRY_TEMPLATE)
    retry_payload = _json_object(str(bpmn_generator._RETRY_PATCH_SCHEMA))
    found.append({
        "id": RETRY_ID,
        "where": "core.bpmn_generator._RETRY_TEMPLATE",
        "text": retry_prompt,
        "payload": retry_payload,
        "names": _payload_names(retry_payload),
    })
    return found


def _finding(scenario: str, kind: str, example: str, value: str,
             measure: float) -> Dict[str, Any]:
    return {"scenario": scenario, "kind": kind, "example": example,
            "value": value, "measure": round(measure, 3)}


def _tokens(text: Any) -> Set[str]:
    """Все слова текста без порога длины: имя пула бывает и из трёх букв (WMS)."""
    return {w.lower() for w in re.findall(r"[а-яёa-z0-9]+", str(text or ""),
                             re.IGNORECASE)}


def mentions(name: Any, example_text: Any) -> bool:
    """Образец называет того же участника, которого требует оракул.

    По всем словам имени: «Бюро кредитных историй» считается упомянутым, только
    если в образце есть все три слова. Порога длины здесь намеренно нет — иначе
    короткие имена (WMS, ИТ) проверки не проходили бы вовсе.
    """
    wanted = _tokens(name)
    return bool(wanted) and wanted <= _tokens(example_text)


def scenario_findings(scenario: Scenario,
                      samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Чем именно сценарий пересекается с образцом промпта."""
    out: List[Dict[str, Any]] = []
    by_id = {e["id"]: e for e in samples}
    composition = [by_id.get("generation_plan"), by_id.get(ROSTER_ID),
                   by_id.get(RETRY_ID)]

    for gen in composition:
        if gen is None:
            continue
        ratio = containment(words(scenario.text), words(gen["text"]))
        if ratio >= TEXT_CONTAINMENT:
            out.append(_finding(scenario.id, "описание как в образце",
                                gen["id"], "", ratio))
        for participant in scenario.expected_participants:
            if mentions(participant, gen["text"]):
                out.append(_finding(
                    scenario.id, "требование скопировать имя пула", gen["id"],
                    str(participant), 1.0))

    imp = by_id.get("improve_package")
    if imp is not None and scenario.improve_prompt:
        ratio = containment(words(scenario.improve_prompt), words(imp["text"]))
        if ratio >= TEXT_CONTAINMENT:
            out.append(_finding(scenario.id, "задача улучшения как в образце",
                                imp["id"], "", ratio))
    return out


def fixture_findings(fixtures: Sequence[Dict[str, Any]],
                     samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Фикстура-«эталон» не должна быть копией образца: иначе она ничего не
    доказывает, а replay по ней мерит воспроизведение промпта."""
    out: List[Dict[str, Any]] = []
    gen = next((e for e in samples if e["id"] == "generation_plan"), None)
    if gen is None:
        return out
    example_names = {n.lower() for n in gen["names"]}
    for fixture in fixtures:
        plan = fixture.get("plan")
        if not isinstance(plan, dict):
            continue
        names = {str((e or {}).get("name", "")) for e in plan.get("elements") or []}
        names = {n.lower() for n in names if n}
        if not names:
            continue
        overlap = len(names & example_names) / len(names)
        if overlap >= NAME_OVERLAP:
            out.append(_finding(str(fixture.get("scenario", "?")),
                                "эталон = образец промпта", "generation_plan",
                                str(fixture.get("id", "?")), overlap))
    return out


def audit(scenarios: Optional[Sequence[Scenario]] = None,
          fixtures: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Полный разбор провенанса: образцы, находки по сценариям и фикстурам."""
    from eval.harness import load_fixtures

    samples = examples()
    chosen = list(scenarios) if scenarios is not None else all_scenarios()
    docs = list(fixtures) if fixtures is not None else load_fixtures()
    findings: List[Dict[str, Any]] = []
    for scenario in chosen:
        findings += scenario_findings(scenario, samples)
    findings += fixture_findings(docs, samples)
    return {
        "examples": [{"id": e["id"], "where": e["where"],
                      "names": sorted(e["names"])} for e in samples],
        "findings": findings,
        "covered": sorted({f["scenario"] for f in findings}),
        "clean": not findings,
    }


def format_findings(report: Dict[str, Any]) -> List[str]:
    """Строки находок для консоли и отчёта (пусто = набор кейсов чист)."""
    if not report["findings"]:
        return ["пересечений с few-shot образцами промптов нет"]
    return [
        f"{f['scenario']}: {f['kind']} ({f['example']}"
        + (f", {f['value']}" if f["value"] else "")
        + f", совпадение {f['measure']})"
        for f in report["findings"]
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    report = audit()
    print(f"образцов в промптах: {len(report['examples'])}")
    for sample in report["examples"]:
        print(f"  {sample['id']} ← {sample['where']}")
    print(f"находок: {len(report['findings'])}")
    for line in format_findings(report):
        print("  " + line)
    return 0 if report["clean"] else 1


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    raise SystemExit(main())

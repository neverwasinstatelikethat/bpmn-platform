"""Исполнимость подсказки: совет скоринга проверяется аплайером, а не чтением.

Блок «УЗКИЕ МЕСТА ПО СКОРИНГУ» уходит в промпт улучшения дословно, и текст,
который нельзя выразить пакетом операций, контур читает как «придумай id»:
аплайер отвечает отказом, а `improve/op_acceptance` записывает это качеством
модели, хотя виноват текст правила. Единственный честный способ проверить
исполнимость — собрать пакет ИСКЛЮЧИТЕЛЬНО из того, что подсказка назвала, и
пропустить его через продуктовый гарант.

Здесь это делается над корпусом реальных схем, а не над фикстурами харнесса:
одиннадцать кейсов набора построены вокруг форм, которые правила уже находят,
и рецепт, нерешаемый на схеме с двумя пулами и подпроцессом, на них не виден.

Разбор намеренно строгий: операндом считается только `ключ='значение'`.
Подсказка, где написано `connect(source=этот шлюз, target=этот таймер)`, даёт
операцию без обязательных полей, и перепись показывает её неприменимой ровно
так, как её увидит аплайер.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Вызов операции внутри текста подсказки: `add_gateway(gateway_type='parallel',
# after='sid-1')`. Имя — строчная латинская операция с подчёркиваниями, чтобы
# русские слова в скобках в разбор не попадали. Тело допускает один уровень
# вложенных скобок: имена пулов в Signavio пишутся «Scoring (Bank)», и без этого
# операнды всей операции пропадали из разбора.
_CALL_RE = re.compile(r"\b([a-z][a-z_]{2,})\(((?:[^()]|\([^()]*\))*)\)")
_ARG_RE = re.compile(r"\b([a-z_]+)='([^']*)'")

# Правила, чей совет обязан быть исполнимым: только они попадают в промпт
# улучшения как «УЗКИЕ МЕСТА ПО СКОРИНГУ».
DEFAULT_RULES = ("rework_loop", "handoff_pingpong", "approval_chain",
                 "lane_overload", "wait_without_sla", "event_types")

# Значение-альтернатива (`event_definition='timer|message|error|signal'`):
# правило перечисляет допустимые варианты, а выбор по смыслу остаётся модели.
# Перепись пробует каждый и засчитывает применимость по первому сработавшему.
_ALTERNATIVES = re.compile(r"^[a-z]+(?:\|[a-z]+)+$")


def parse_recipes(text: str) -> List[Dict[str, Any]]:
    """Операции, собранные только из вызовов текста подсказки.

    Возвращает и те, у которых не хватает обязательных полей: «операция без
    операндов» — это и есть находка, а не причина молча её выбросить.
    """
    out: List[Dict[str, Any]] = []
    for name, body in _CALL_RE.findall(text or ""):
        op: Dict[str, Any] = {"op": name}
        for key, value in _ARG_RE.findall(body):
            op[key] = value
        out.append(op)
    return out


def _variants(op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Операция и её варианты, если значение задано альтернативой."""
    for key, value in op.items():
        if key != "op" and isinstance(value, str) and _ALTERNATIVES.match(value):
            return [{**op, key: part} for part in value.split("|")]
    return [op]


def _expand(recipes: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Пакеты по декартовому произведению альтернатив (их размер мал: 3-4)."""
    packages: List[List[Dict[str, Any]]] = [[]]
    for recipe in recipes:
        variants = _variants(recipe)
        packages = [prefix + [variant] for prefix in packages for variant in variants]
        if len(packages) > 16:  # потолок перебора: подсказка с двумя
            break              # альтернативами уже требует разбора не досконально
    return packages


# Подсказка, которая честно говорит о границе операций: `add_condition` и
# `set_default` работают только по ветке исключающего шлюза, и если цикл
# отпускает параллельная развилка, исполнимого рецепта не существует. Такую
# находку перепись считает отдельно: её нельзя показывать как «совет не
# применим», иначе метрика начинала бы врать про предел инструмента.
MANUAL_MARKER = "не выражается операцией"

# Вторая граница — операнд, который правило назвать не может: шаг пула,
# принимающего ответ при обмене концом на участника целиком, ответ там, где у
# запрашивающего следующего шага нет, и цель висячего потока (её знает только
# автор процесса). Рецепта в тексте нет или он назван наполовину, но это не
# молчание правила: причина сказана, а выбор остаётся модели.
OTHER_LIMITS = ("назовите шаг его пула", "ответ принять нечему",
                "только по смыслу процесса", "называет автор процесса")


def explains_no_recipe(text: str) -> bool:
    """Сказано ли в подсказке, почему она не называет операцию."""
    return MANUAL_MARKER in text or any(m in text for m in OTHER_LIMITS)


def _status(scorer: Any, xml: str, rule: str) -> str:
    meta = (scorer.evaluate(xml).get("details_meta") or {}).get(rule, {})
    return meta.get("status") or ""


def _packages(text: str) -> List[List[Dict[str, Any]]]:
    return _expand(parse_recipes(text))


def check_scheme(scorer: Any, apply_and_guarantee: Any, rules_regressed: Any,
                 xml: str, rule: str, rounds: int = 5) -> Dict[str, Any]:
    """Применима ли подсказка одного правила к этой схеме и снимает ли она нарушение.

    `cleared` — с одного пакета (это то, что модель получает в одном сообщении),
    `rounds_to_clear` — за сколько кругов починки нарушение уходит, если каждый
    круг брать совет из актуальной схемы. Второе число и есть продуктовая
    перспектива: `RECIPES_IN_MESSAGE` режет подсказку на три рецепта, и остаток
    доходит следующим кругом, а не перестаёт существовать.
    """
    text = (scorer.evaluate(xml).get("recommendations_by_rule") or {}).get(rule) or ""
    recipes = parse_recipes(text)
    verdict: Dict[str, Any] = {"rule": rule, "named": bool(recipes),
                               "manual": MANUAL_MARKER in text,
                               "applied": False, "verbatim": False,
                               "cleared": False, "exhausted": False,
                               "rounds_to_clear": 0, "regressions": [],
                               "ops": 0, "refused": "", "text": text}
    if not recipes:
        verdict["refused"] = ("правка вне операций (сказано прямо)"
                              if verdict["manual"]
                              else "в тексте нет ни одного вызова операции")
        return verdict
    # Дословный пакет — то, что получает модель, скопировавшая рецепт: те же
    # вызовы, но без раскрытия альтернатив. Отдельный факт, а не замена
    # `applied`: выбор операнда остаётся автору, и перепись обязана пробовать
    # оба пути. Без этого `applied == named` рапортовало исполнимость, которой
    # в тексте нет (правило `event_types` годами печатало
    # `event_definition='message|error|signal'`, аплайер на это значение
    # отвечал «неизвестное определение»).
    try:
        _, v_report = apply_and_guarantee(xml, list(recipes))
        verdict["verbatim"] = bool(v_report.get("applied")) and not v_report.get("reverted")
    except Exception:  # noqa: BLE001 — дословная проверка не роняет перепись
        verdict["verbatim"] = False
    cur = xml
    for rnd in range(1, rounds + 1):
        packages = _packages(text) if rnd == 1 else _packages(
            (scorer.evaluate(cur).get("recommendations_by_rule") or {}).get(rule) or "")
        moved = False
        for package in packages:
            verdict["ops"] = max(verdict["ops"], len(package))
            try:
                after, report = apply_and_guarantee(cur, package)
            except Exception as exc:  # noqa: BLE001 — находка не должна ронять перепись
                verdict["refused"] = f"исключение аплайера: {type(exc).__name__}"
                continue
            applied = report.get("applied") or []
            skipped = report.get("skipped") or []
            if report.get("reverted") or not applied:
                verdict["refused"] = verdict["refused"] or report.get("reverted") or "; ".join(
                    f"{s.get('op')}: {s.get('reason')}" for s in skipped[:2])
                continue
            verdict["applied"] = True
            if rnd == 1:
                verdict["cleared"] = _status(scorer, after, rule) != "failed"
            cur, moved = after, True
            verdict["skipped_share"] = round(len(skipped) / max(1, len(package)), 3)
            break
        if _status(scorer, cur, rule) != "failed":
            verdict["rounds_to_clear"] = rnd
            break
        if not moved:  # совет больше не даёт применимой правки: круг застопорился
            break
    else:
        # Нарушение не снялось, но каждый круг починка шла: это не отказ совета,
        # а лимит переписи. Для схемы с 45 безымянными событиями и тремя
        # рецептами в сообщении (`RECIPES_IN_MESSAGE`) нужно ≥15 кругов, и без
        # этого факта «снято за круги 0» читалось бы как «рецепт не работает»,
        # хотя работает и просто не влезает в 5 кругов замера.
        verdict["exhausted"] = True
    verdict["regressions"] = sorted((rules_regressed(xml, cur) or {}).keys())
    return verdict


def census(scorer: Any, samples: Iterable[Tuple[str, str]],
           rules: Sequence[str] = DEFAULT_RULES,
           per_rule: int = 8) -> Dict[str, Any]:
    """По N схем на правило: совет назван, применён, снял нарушение, ничего не сломал.

    `samples` — пары (имя файла, XML). Отбор по первому встречному нарушению:
    цель — исполнимость рецепта, а не статистика частоты правил (она уже есть в
    `eval/coverage.census`).
    """
    from core.bpmn_edits import apply_and_guarantee
    from core.llm_improve import _rules_regressed

    cases: Dict[str, List[Tuple[str, str]]] = {rule: [] for rule in rules}
    for name, xml in samples:
        if all(len(cases[rule]) >= per_rule for rule in cases):
            break
        meta = (scorer.evaluate(xml).get("details_meta") or {})
        for rule in rules:
            if (len(cases[rule]) < per_rule
                    and meta.get(rule, {}).get("status") == "failed"):
                cases[rule].append((name, xml))
    rows = {rule: [check_scheme(scorer, apply_and_guarantee, _rules_regressed,
                               xml, rule) for _, xml in cases[rule]]
            for rule in rules}
    summary = {}
    for rule, verdicts in rows.items():
        total = len(verdicts)
        manual = sum(1 for v in verdicts if not v["named"] and v["manual"])
        # Рецепт мог не назваться по трём причинам: правка вне операций, выбор
        # остаётся модели, или правило промолчало. Последнее — дефект текста, и
        # он печатается отдельным числом, а не прячется в «не применим».
        choice = sum(1 for v in verdicts
                     if not v["named"] and not v["manual"]
                     and explains_no_recipe(v["text"]))
        cleared = sum(1 for v in verdicts if v["cleared"])
        full = sum(1 for v in verdicts if v["rounds_to_clear"])
        actionable = total - manual
        summary[rule] = {
            "n": total,
            "manual": manual,
            "choice": choice,
            "silent": sum(1 for v in verdicts if not v["named"]
                          and not explains_no_recipe(v["text"])),
            "named": sum(1 for v in verdicts if v["named"]),
            "applied": sum(1 for v in verdicts if v["applied"]),
            # Сколько названных рецептов аплайер принял бы, если бы модель
            # скопировала подсказку буквально. Меньше `named` — текст правила
            # требует правки, а не подсказки.
            "verbatim": sum(1 for v in verdicts if v["named"] and v["verbatim"]),
            "cleared": cleared,
            "cleared_full": full,
            "broke": sum(1 for v in verdicts if v["regressions"]),
            # Не снялось за круги, но каждый круг правка шла: лимит замера, а не
            # брак совета.
            "exhausted": sum(1 for v in verdicts if v["exhausted"]),
            # Знаменатель — только те нарушения, где исполнимый рецепт вообще
            # возможен: иначе предел инструмента выглядел бы браком подсказки.
            "share_cleared": round(cleared * 100 / actionable) if actionable else None,
            "share_full": round(full * 100 / actionable) if actionable else None,
        }
    return {"per_case": rows, "summary": summary,
            "witnesses": {rule: [name for name, _ in cases[rule]] for rule in rules}}


def format_report(report: Dict[str, Any]) -> List[str]:
    lines = ["исполнимость подсказки (пакет собирается только из id текста "
             "советов и пропускается гарантом):"]
    for rule, s in report["summary"].items():
        share, full = s["share_cleared"], s["share_full"]
        lines.append(
            f"  {rule:18} рецепт назван {s['named']}/{s['n']} | применено "
            f"{s['applied']}/{s['n']} | дословно применимо {s['verbatim']}"
            + (f"/{s['named']}" if s["verbatim"] != s["named"] else "")
            + f" | снято с одного пакета {s['cleared']}"
            + (f" ({share}%)" if share is not None else "")
            + f" | снято за круги починки {s['cleared_full']}"
            + (f" ({full}%)" if full is not None else "")
            + (f" | вне операций {s['manual']}" if s["manual"] else "")
            + (f" | операнд выбирает модель {s['choice']}" if s["choice"] else "")
            + (f" | МОЛЧИТ О ПРИЧИНЕ {s['silent']}" if s["silent"] else "")
            + (f" | не снялось за лимит кругов {s['exhausted']}"
               if s["exhausted"] else "")
            + f" | сломано другое правило {s['broke']}")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from core.bpmn_scoring import BPMNScorer
    from eval.coverage import corpus_files

    parser = argparse.ArgumentParser(
        description="Исполнимость подсказок скоринга на реальных схемах корпуса")
    parser.add_argument("--per-rule", type=int, default=8,
                        help="сколько схем с нарушением каждого правила разобрать")
    parser.add_argument("--all-rules", action="store_true",
                        help="разобрать все правила, а не только бизнес-слой: "
                             "в промпт улучшения уходит блок до 10 находок, и "
                             "косметические правила толкаются там же")
    args = parser.parse_args(list(argv) if argv is not None else None)

    scorer = BPMNScorer()
    rules = tuple(scorer.rules) if args.all_rules else DEFAULT_RULES
    files = corpus_files()
    report = census(scorer, files, rules=rules, per_rule=args.per_rule)
    for line in format_report(report):
        print(line)
    for rule, verdicts in report["per_case"].items():
        # Имя — той самой схемы, а не первой из списка: иначе отчёт приписывает
        # отказ файл, который к нему не относится (перепись сама на этом
        # попадалась).
        for name, verdict in zip(report["witnesses"][rule], verdicts):
            if verdict["manual"] and not verdict["named"]:
                continue
            if not verdict["named"] and explains_no_recipe(verdict["text"]):
                # Причина названа словами («операнд выбирает автор»): это предел
                # инструмента, а не дефект текста, и печатать его как отказ —
                # значит тонуть в строках, на которые исправить нечего.
                continue
            if verdict["applied"] and verdict["cleared"] and verdict["verbatim"] \
                    and not verdict["regressions"]:
                continue
            print(f"  {rule} [{name}]: ops={verdict['ops']} applied={verdict['applied']} "
                  f"дословно={verdict['verbatim']} "
                  f"cleared={verdict['cleared']} "
                  + ("упорлся в лимит кругов переписи " if verdict["exhausted"] else "")
                  + f"отказ={verdict['refused'][:150]} "
                  f"регрессии={verdict['regressions']}")
    return 0


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    raise SystemExit(main())

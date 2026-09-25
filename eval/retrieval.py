"""Качество подбора эталонов: RAG-шаг улучшения как измеряемая величина.

Контур улучшения отдаёт модели практики из корпуса `core/bpmn_dataset/`, и до
этой метрики ни один показатель харнесса не отвечал на вопрос «а тот ли эталон
подобран». Выглядит такой провал как «модель предложила чужие правки», хотя к
модели это не относится: в топ-3 нет ни одного процесса той же предметной
области, и просить у модели здравый смысл не с чего.

Релевантность размечена по теме эталона, а тема берётся из ИМЕНИ ФАЙЛА корпуса
(`eval/corpus_topics.py`), а не из его поля `domain`. Прежняя разметка читала
`domain`, которое заполняет `BPMNKnowledgeBase._detect_domain` — тот же
классификатор, что размечает и запрос пользователя: метрика меряла согласие
системы с самой собой, а промах подбора был неотличим от промаха классификатора.
Второй дефект той же разметки: под `general` классификатор собирает 167 файлов
из 367, и почти все это демо элементов нотации (`timer_5`, `signal_3`) и пустые
шаблоны упражнений. Для сценария, где релевантным указан `general`, точность
росла от возврата любого демо-файла — метрика была играбельна.

Имя файла в датасете человеческое (`Dispatch_of_Goods_<uuid>`,
`Exercise_5_-_Credit_Scoring`), его расставил человек, и оно не зависит от
выводов контура. Языковой барьер при этом не мешает: имя — единственный признак
эталона на том же языке, что и разметка сценариев, а описания процессов в
корпусе и в сценариях расходятся языками (корпус англо-немецкий, сценарии
русские), поэтому пересечение слов у них нулевое при любом качестве подбора.

`RELEVANT_TOPICS` — размеченный вручную набор (тот же класс ожиданий, что
`expected_participants` и `must_have_timer` в `eval/scenarios.py`): она
независима от вывода модели и не попадает ни в один промпт, так что мерить по
ней можно. Сценария без разметки метрика не касается — возвращает None, а не
ноль: «не размечено» и «подобрано плохо» — разные утверждения.

Темы, которой в датасете нет, записаны в `ABSENT_TOPICS` явно: сценарий под
такую тему размечен, но релевантных файлов в корпусе ноль, и кейс выходит в
строку `uncovered` как пробел датасета, а не как промах подбора. Таких тем
четыре — закупка, ИТ-инцидент, найм и бронирование транспорта, — и RAG по этому
корпусу измеряется на четырёх сценариях из восьми. Это вывод о датасете, а не о
качестве поиска: корпус — набор учебных примеров BPMN-курса, а не реестр
бизнес-процессов ВкусВилл.

Второе число прогона — `practice_coverage`: доля конструкций, которые сценарий
заявляет (`must_have_timer`, `must_branch`), показанных хотя бы в одном подобранном
этлоне. Тема отвечает на «тот ли процесс», покрытие — на «увидит ли модель пример
того, чего не хватает процессу». Оно определено для любого кейса, разметки
корпуса не требует, и именно оно падает, когда выдача съезжает на демо элементов.

Метрика платит за индекс по-настоящему: семантическая ветка поднимает
sentence-transformers (сеть или тяжёлый CPU), поэтому в `--mode replay` она не
запускается. Считайте её явно: `python -m eval.retrieval`.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from eval.corpus_topics import topic_of
from eval.scenarios import Scenario, all_scenarios

# Сколько верхних результатов смотрит контур (`TOP_SCHEMAS`) — на нём и мерим.
DEFAULT_K = 3

# Тема-эталон по сценарию. `notation_demo` и `tutorial` не подходят ни одному:
# демо элемента нотации и пустой шаблон упражнения — не пример процесса той же
# темы, какой бы удобной для точности ни была их доля корпуса (165 + 42 файла).
RELEVANT_TOPICS: Dict[str, Tuple[str, ...]] = {
    "warehouse_delivery": ("goods_dispatch",),
    "support_ticket": ("claim_recourse",),
    "purchase_approval": ("purchase_approval",),
    "product_return": ("goods_dispatch", "claim_recourse"),
    "production_incident": ("it_incident",),
    "employee_onboarding": ("hr_onboarding",),
    "loan_application": ("credit_scoring", "banking"),
    "vehicle_reservation": ("vehicle_reservation",),
}

# Темы, которых в датасете нет. Заявлены явно, чтобы «корпус не покрывает тему»
# нельзя было тихонько подменить на «разметку забыли»: тест сверяет каждую тему
# `RELEVANT_TOPICS` с таблицей `corpus_topics` и этим списком.
ABSENT_TOPICS = frozenset({"purchase_approval", "it_incident",
                           "hr_onboarding", "vehicle_reservation"})

# Какая практика корпуса доказывает заявленное ожидание сценария. Ключи —
# признаки `_PRACTICE_SIGNALS` контура, то есть ровно то, что уходит в промпт.
PRACTICE_OF_EXPECTATION = {"must_have_timer": "timer", "must_branch": "parallel"}


def relevant_topics(scenario: Scenario) -> Optional[Tuple[str, ...]]:
    """Размеченные темы сценария или None, если сценарий не размечен."""
    return RELEVANT_TOPICS.get(scenario.id)


def topic_of_schema(schema: Dict[str, Any]) -> str:
    """Тема эталона по имени файла корпуса — независимо от вывода контура."""
    return topic_of(str(schema.get("name") or ""))


def is_relevant(scenario: Scenario, schema: Dict[str, Any]) -> bool:
    """Тот ли это процесс: тема эталона из числа тех, что оправданы темой.

    `unknown` (имя файла не опознано ручной таблицей) не релевантен, и это
    единственная ложная регрессия, которую разметка допускает: таких файлов в
    корпусе четыре из 367, и их число печатается в шапке отчёта — разрастание
    датасета новыми семействами обязано быть замеченным, а не обнулить метрику.
    """
    wanted = relevant_topics(scenario)
    if wanted is None:
        return False
    return topic_of_schema(schema) in wanted


def topic_share(topics: Optional[Dict[str, str]],
                wanted: Optional[Tuple[str, ...]]) -> Optional[float]:
    """Доля корпуса под размеченными темами: точность по теме, занимающей
    половину датасета, не отличает подбор по теме от первого попавшегося файла.
    Честный вывод метрики — только вместе с этой долей; для иглы в один эталон
    (`banking` — два файла) она и есть смысл замера."""
    if not topics or not wanted:
        return None
    hit = sum(1 for t in topics.values() if t in wanted)
    return hit / len(topics)


def needed_practices(scenario: Scenario) -> Optional[Tuple[str, ...]]:
    """Признаки практик, которые сценарий заявляет как обязательные конструкции.

    Сценарий без такого ожидания возвращает None: у него нечего искать в выдаче,
    и ноль покрытия был бы про него враньём.
    """
    needed = [PRACTICE_OF_EXPECTATION[field] for field in sorted(PRACTICE_OF_EXPECTATION)
              if getattr(scenario, field, False)]
    return tuple(sorted(needed)) or None


def _practice_keys(hit: Dict[str, Any]) -> Set[str]:
    return {str(pair[0]) for pair in (hit.get("practices") or []) if pair}


def practice_coverage(scenario: Scenario,
                      hits: Iterable[Dict[str, Any]]) -> Optional[float]:
    """Доля нужных сценарию конструкций, показанных хотя бы в одном этлоне
    выдачи.

    Пустая выдача — 0.0: модель не увидела ни одного примера нужной конструкции,
    и это худший случай подбора. None остаётся за случаем «мерить нечего».
    """
    needed = needed_practices(scenario)
    if needed is None:
        return None
    shown: Set[str] = set()
    for hit in hits:
        shown |= _practice_keys(hit)
    return sum(1 for key in needed if key in shown) / len(needed)


def precision_at_k(ranked: Sequence[bool], k: int = DEFAULT_K) -> Optional[float]:
    """Доля релевантных среди первых k (пустая выдача — None, а не 0.0:
    «ничего не нашлось» и «нашёл не то» — разные дефекты)."""
    if not ranked:
        return None
    top = list(ranked)[:k]
    return sum(top) / len(top)


def recall_at_k(ranked: Sequence[bool], total_relevant: Optional[int] = None,
                k: int = DEFAULT_K) -> Optional[float]:
    """Доля релевантного корпуса, попавшая в первые k.

    `total_relevant` — сколько релевантных эталонов вообще в корпусе: без него
    знаменатель берётся из той же выдачи, и отношение всегда даёт 1 или 0/0.

    В заголовке отчёта этой метрики нет, и это не забывчивость, а её свойство:
    при 367 эталонах в корпусе и бюджете контура в 3 слота знаменатель — десятки
    файлов, и recall@3 заперт у нуля даже при идеальном подборе. Число остаётся
    в разборе кейса (там виден `relevant_in_corpus`), а для свода годится
    `mrr_at_k` — он измеряет то, что на таком k вообще различимо: насколько
    высоко встал первый релевантный.
    """
    if not ranked or not total_relevant:
        return None
    return sum(list(ranked)[:k]) / total_relevant


def mrr_at_k(ranked: Sequence[bool], k: int = DEFAULT_K) -> Optional[float]:
    """MRR@k: reciprocal rank первого релевантного в выдаче (1/rank, иначе 0).

    Пустая выдача — 0, а не None: «ничего не вернулось» и есть худший случай
    ранжирования. None остаётся за ситуацией, когда мерить нечего (не размечено
    или релевантных в корпусе нет) — так «не померено» не читается как «ноль».
    """
    if not ranked:
        return None
    for pos, hit in enumerate(list(ranked)[:k]):
        if hit:
            return 1.0 / (pos + 1)
    return 0.0


def ndcg_at_k(ranked: Sequence[bool], k: int = DEFAULT_K) -> Optional[float]:
    """NDCG@k по бинарной релевантности: порядок в выдаче имеет значение,
    потому что модель читает эталоны сверху и первый влияет на план сильнее."""
    if not any(ranked):
        return None
    top = list(ranked)[:k]
    dcg = sum(1.0 / math.log2(pos + 2) for pos, hit in enumerate(top) if hit)
    ideal = sorted(ranked, reverse=True)[:k]
    idcg = sum(1.0 / math.log2(pos + 2) for pos, hit in enumerate(ideal) if hit)
    return dcg / idcg if idcg else None


def measure_query(relevance: Sequence[bool], k: int = DEFAULT_K,
                  total_relevant: Optional[int] = None) -> Dict[str, Optional[float]]:
    return {"precision@k": precision_at_k(relevance, k),
            "recall@k": recall_at_k(relevance, total_relevant, k),
            "mrr@k": mrr_at_k(relevance, k),
            "ndcg@k": ndcg_at_k(relevance, k)}


def _mean(values: List[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def run(kb: Any, cases: Sequence[Dict[str, Any]],
        k: int = DEFAULT_K) -> Dict[str, Any]:
    """Прогон по живому поиску: `kb.find_best_practices(задача, схема)`.

    `cases` — «сценарий + запрос + схема пользователя»: контур всегда зовёт
    поиск с существующей схемой, и мерить подбор «по одному тексту задачи»
    означало бы измерять вход, которого в продукте нет (лексическая ветка без
    имён шагов пустая ровно всегда).
    """
    rows: List[Dict[str, Any]] = []
    ready = getattr(kb, "ensure_ready", None)
    if callable(ready):
        ready()
    corpus_meta = list(getattr(kb, "_metadata", []) or [])
    topics_map = {str(m.get("name") or ""): topic_of_schema(m) for m in corpus_meta}
    for case in cases:
        scenario: Scenario = case["scenario"]
        hits = kb.find_best_practices(case["query"], case.get("xml"),
                                      top_k=k) or []
        relevance = [is_relevant(scenario, hit) for hit in hits]
        wanted = relevant_topics(scenario)
        # Доля релевантного во всём корпусе: тема, занимающая половину датасета,
        # делает точность копией доминанты корпуса. Честный вывод метрики —
        # только вместе с этой долей; для иглы в два эталона (`banking`) она и
        # есть смысл замера.
        in_corpus = sum(1 for m in corpus_meta if is_relevant(scenario, m))
        share = topic_share(topics_map, wanted)
        row = {"scenario": scenario.id, "k": k, "returned": len(hits),
               "relevant": sum(relevance),
               "origin": str(case.get("origin") or ""),
               "topics": sorted({topic_of_schema(h) for h in hits}),
               "relevant_share": share, "relevant_in_corpus": in_corpus,
               "with_scheme": bool(case.get("xml")),
               "needs": list(needed_practices(scenario) or []),
               "practice_coverage": practice_coverage(scenario, hits),
               "top": [str(h.get("name") or "") for h in hits],
               "labelled": wanted is not None}
        row.update(measure_query(relevance, k, in_corpus or None))
        rows.append(row)
    # Кейс, для которого в корпусе нет ни одного релевантного эталона, мерить
    # нечего: ноль точности там означает пробел корпуса, а не плохой подбор.
    covered = [r for r in rows if r["labelled"] and r["relevant_in_corpus"]]
    by_origin = {origin: {
        "n": len([r for r in covered if r["origin"] == origin]),
        "precision@k": _mean([r["precision@k"] for r in covered
                              if r["origin"] == origin]),
        "mrr@k": _mean([r["mrr@k"] for r in covered if r["origin"] == origin]),
    } for origin in sorted({r["origin"] for r in covered})}
    return {
        "k": k,
        "cases": rows,
        "by_origin": by_origin,
        "semantic_branch": getattr(kb, "_embeddings", None) is not None,
        "corpus": len(corpus_meta),
        # Сколько файлов корпуса ручная таблица тем не опознала: метрика считает
        # их нерелевантными, и рост этого числа означает новое семейство в
        # датасете, а не плохой подбор.
        "unknown_in_corpus": sum(1 for t in topics_map.values()
                                 if t == "unknown"),
        "uncovered": sorted(r["scenario"] for r in rows
                            if r["labelled"] and not r["relevant_in_corpus"]),
        "unlabelled": sorted(r["scenario"] for r in rows if not r["labelled"]),
        # Кейс без схемы мерить нельзя: без имён шагов лексическая ветка пустая,
        # и подбор сводится к одной ветке — это другой вход, чем у контура.
        "schemes_missing": sorted(r["scenario"] for r in rows
                                  if not r["with_scheme"]),
        "precision@k": _mean([r["precision@k"] for r in covered]),
        # recall@k в отчёте не заголовок: при 367 эталонах и k=3 знаменатель
        # десятки файлов, и метрика заперта у нуля даже при идеальном подборе
        # (см. docstring `recall_at_k`). Для свода взят MRR — он различим на
        # таком k; per-case recall остаётся в rows и в JSON.
        "recall@k": _mean([r["recall@k"] for r in covered]),
        "mrr@k": _mean([r["mrr@k"] for r in covered]),
        "ndcg@k": _mean([r["ndcg@k"] for r in covered]),
        # Покрытие практик считается по всем размеченным на конструкции кейсам,
        # а не только по «покрытым темой»: оно не зависит от того, есть ли в
        # датасете процесс нужной темы, и потому остаётся измеримым там, где
        # точность мерить нечем.
        "practice_coverage": _mean([r["practice_coverage"] for r in rows]),
        "practice_cases": sum(1 for r in rows if r["practice_coverage"] is not None),
        # Пустая выдача — отдельное число: если мерить её нулевой точностью,
        # «поиск сломался» и «поиск подобрал не то» сливаются в одну регрессию.
        "empty_share": (sum(1 for r in covered if not r["returned"]) / len(covered)
                        if covered else None),
    }


def build_cases(scenarios: Optional[Sequence[Scenario]] = None,
                fixtures: Optional[Sequence[Dict[str, Any]]] = None,
                to_xml: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Кейсы «задача + схема»: сначала записанные improve-фикстуры, затем по
    кейсу на каждый сценарий, где улучшения нет.

    Двух improvement-фикстур на наборе мало: `P@3` по двум случаям не отличает
    подбор по теме от везения, а `recall@3` вообще не имеет смысла, когда
    знаменатель — один файл корпуса. Поэтому у метрики два источника кейсов, и
    оба помечены `origin`: записанный запрос пользователя (`improve`) и
    синтетический вход той же формы — текст сценария плюс его эталонная схема
    (`etalon`). Смешивать их в одном числе можно ровно потому, что форма входа
    одинаковая; происхождение видно и в отчёте, и в JSON.

    `base_plan` improvement-фикстуры — id плановой фикстуры, а не сам план:
    база разрешается по нему, иначе метрика меряла бы подбор по запросу без
    схемы. `to_xml` — сериализатор генератора; он приходит из харнесса, чтобы
    метрика строила ровно тот XML, что видит аплайер, а не свой шаблон.
    """
    chosen = {s.id: s for s in (scenarios or all_scenarios())}
    plans = {str(f.get("id") or ""): f for f in (fixtures or [])
             if str(f.get("kind") or "") == "plan"}

    def render(plan: Any) -> Tuple[Any, str]:
        """Схема пользователя для кейса: XML либо причина, по его которой нет."""
        if to_xml is None:
            return None, "нет сериализатора"
        if not plan:
            return None, "у фикстуры нет плана"
        try:
            return str(to_xml(plan) or "") or None, ""
        except Exception as e:  # noqa: BLE001 — кейс без схемы остаётся видимым
            return None, str(e)

    cases: List[Dict[str, Any]] = []
    covered: set = set()
    for fixture in fixtures or []:
        if str(fixture.get("kind") or "") != "improve":
            continue
        scenario = chosen.get(str(fixture.get("scenario") or ""))
        if scenario is None:
            continue
        covered.add(scenario.id)
        xml, error = render((plans.get(str(fixture.get("base_plan") or ""))
                             or {}).get("plan"))
        cases.append({"scenario": scenario,
                      "query": str(fixture.get("prompt") or scenario.text),
                      "xml": xml, "error": error, "origin": "improve",
                      "fixture": str(fixture.get("id") or "")})
    for scenario in chosen.values():
        if scenario.id in covered:
            continue
        good = next((f for f in (plans.values() or [])
                     if f.get("scenario") == scenario.id
                     and str(f.get("quality") or "") == "good"), None)
        if good is None:
            continue
        xml, error = render(good.get("plan"))
        cases.append({"scenario": scenario, "query": scenario.text, "xml": xml,
                      "error": error, "origin": "etalon",
                      "fixture": str(good.get("id") or "")})
    return cases


def format_report(report: Dict[str, Any]) -> List[str]:
    def num(value: Optional[float]) -> str:
        return "—" if value is None else f"{value:.2f}"

    lines = [f"корпус: {report['corpus']} эталонов, темой размечено "
             f"{report['corpus'] - report['unknown_in_corpus']} "
             f"(нераспознанных {report['unknown_in_corpus']}) | ветка семантики: "
             + ("есть" if report["semantic_branch"] else "нет (только TF-IDF)")]
    if report["unlabelled"]:
        lines.append("  не размечено по теме: " + ", ".join(report["unlabelled"]))
    if report["uncovered"]:
        lines.append("  корпус не покрывает тему: "
                     + ", ".join(report["uncovered"])
                     + " — релевантных эталонов в корпусе нет, кейс не считается")
    if report.get("schemes_missing"):
        lines.append("  без схемы пользователя (не с чем идти в поиск): "
                     + ", ".join(report["schemes_missing"]))
    for row in report["cases"]:
        lines.append(
            f"  {row['scenario']} [{row['origin']}]: P@{report['k']} "
            f"{num(row['precision@k'])}, MRR@{report['k']} {num(row['mrr@k'])}, "
            f"NDCG@{report['k']} "
            f"{num(row['ndcg@k'])} | релевантных {row['relevant']} из "
            f"{row['returned']} | релевантных в корпусе "
            f"{num(row['relevant_share'])} | темы выдачи: "
            f"{', '.join(row['topics']) or '—'}"
            + (f" | практики: {num(row['practice_coverage'])} из "
               f"{len(row['needs'])}" if row["needs"] else "")
            + (f" | {row['error']}" if row.get("error") else ""))
    lines.append("сводка: P@{k} {p}, MRR@{k} {m}, NDCG@{k} {n}, пустых ответов {e}"
                 .format(k=report["k"], p=num(report["precision@k"]),
                         m=num(report["mrr@k"]), n=num(report["ndcg@k"]),
                         e=num(report["empty_share"])))
    lines.append("  покрытие нужных конструкций практиками: {} (кейсов: {})"
                 .format(num(report.get("practice_coverage")),
                         report.get("practice_cases", 0)))
    # Разбивка по происхождению кейса: `improve` — что контур подбирает на
    # записанных запросах, `etalon` — на тексте задачи. Если сводка сошлась к
    # одному числу, а по группам разошлась, агрегат ничего не утверждает.
    for origin, part in (report.get("by_origin") or {}).items():
        lines.append(f"  {origin}: n={part['n']}, P@{report['k']} "
                     f"{num(part['precision@k'])}, MRR@{report['k']} "
                     f"{num(part['mrr@k'])}")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from core.llm_improve import BPMNKnowledgeBase
    from eval.harness import generate_xml, load_fixtures, repair_structure

    parser = argparse.ArgumentParser(
        description="Качество подбора эталонов (RAG) по improvement-фикстурам")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    args = parser.parse_args(list(argv) if argv is not None else None)

    def to_xml(plan: Dict[str, Any]) -> str:
        # План прогоняется через починку: контур ищет эталоны по той схеме,
        # которая уже собрана, а не по сырому ответу модели.
        return generate_xml(repair_structure(plan)[0])

    # Все фикстуры, а не только improve: `base_plan` improvement-кейса — это
    # ссылка на плановую фикстуру, и без неё кейс остался бы без схемы.
    cases = build_cases(fixtures=load_fixtures(), to_xml=to_xml)
    if not cases:
        print("improve-фикстур нет — мерить нечего")
        return 1
    report = run(BPMNKnowledgeBase(), cases, k=args.k)
    for line in format_report(report):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — ручной запуск
    raise SystemExit(main())

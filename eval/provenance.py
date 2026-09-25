"""Происхождение оцениваемых схем: метрика не должна считаться по образцу.

Часть образцов модель видит в самом промпте: генерационный системный промпт
содержит few-shot план, промпт улучшения — few-shot пакет операций. Если
описание сценария, его задача улучшения или «эталонная» фикстура совпадают с
таким образцом, пройденный инвариант меряет копирование, а не способность
контура. Модуль находит эти пересечения и называет их поимённо:

* `eval/run.py` печатает раздел «Провенанс кейсов» и кладёт тот же разбор в
  отчёт — по нему видно, какой метрике нельзя верить;
* `tests/test_eval_provenance.py` держит планку: новые сценарии и фикстуры не
  должны добавлять пересечений сверх разобранных, а оба способа ослепить
  проверку считаются ошибкой — исчезнувший маркер образца (нечего сверять) и
  новый промпт с образцом, не занесённый в `examples()` (его не сверяют).

Образец — это не только JSON-пример в промпте. Моделью правит и статический
текст, который приходит в промпт из других модулей: формулировки правил
скоринга и словарь форм операций (`core.bpmn_scoring`, `core.bpmn_edits`), а
также блок практик, который контур улучшения собирает из корпуса эталонов под
конкретный запрос. Все эти поверхности зарегистрированы в `examples()` наравне
с few-shot образцами: имя участника, напечатанное в любом из них, работает
подсказкой ответа так же, как имя из примера.

Фикстуры сверяются парами по роду записи: план — с планом-образцом генерации,
пакет операций — с образцом промпта улучшения. Иначе improve-фикстура,
переписанная из примера, проходила бы аудит незамеченной: с планом-образцом её
имена правок не пересекаются вовсе.

Здесь нет порога «достаточно хорошо»: список находок — это описание факта, а
решение о том, что остаётся в наборе кейсов, принимает человек.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

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
GEN_ID = "generation_plan"
IMPROVE_ID = "improve_package"
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
# Стадия маршрута: правила модель читает целиком, новых имён в них быть не
# должно — отсюда пятый образец проверки.
FLOW_ID = "flow_route_rules"
# Статические тексты, которые контур улучшения вклеивает в свой промпт: разбор
# правил скоринга (`_findings_block`) и словарь форм операций
# (`_operations_block`). Образцом ответа они не притворяются, но имя шага или
# пула, записанное в такую формулировку, модель читает как подсказку ответа —
# поэтому они проверяются по тому же `rules_only` сценарию, что и промпт
# маршрута.
SCORING_ID = "scoring_rules"
OP_SPEC_ID = "op_spec"
# Блок ЛУЧШИЕ ПРАКТИКИ: его содержимое зависит от запроса, поэтому это не
# статический текст, а отложенная выборка из корпуса эталонов.
RAG_ID = "rag_practices"

# Служебные слова имён эталонов корпуса: они есть у половины файлов («Exercise»,
# «Process»), и совпадать сценарием с эталоном по ним нельзя — слов слишком
# много, чтобы быть именем процесса.
NAME_NOISE = {"process", "процесс", "procedure", "процедура", "example",
              "образец", "exercise", "excercise", "excersice", "exersise",
              "упражнение", "practice", "scheme", "схема", "схемы", "model",
              "модель", "workflow", "demo", "test", "тест", "document",
              "документ"}


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


_KB: Any = None


def knowledge_base() -> Any:
    """Индекс корпуса эталонов для провенанса: один на процесс и без сети.

    `find_best_practices` поднимает sentence-transformers, чтобы закодировать
    ЗАПРОС, — это модель (а при первом запуске ещё и загрузка весов), тогда как
    провенанс обязан работать офлайн: на нём считается метрика в CI. Кодер
    подменён отказом, и корпус сам деградирует до лексической ветки TF-IDF —
    того офлайн-скоринга, который в модуле уже есть. Эмбеддинги из кэша при этом
    отбрасываются: без кодера косинусы посчитать нечем, а `RRF` ранжирует по
    местам, поэтому «половинка» семантической ветки выглядела бы живым поиском,
    которым не является. Индекс не пересохраняется: провенанс не должен трогать
    артефакты репозитория — файл кэша без эмбеддингов подменил бы собой боевой.
    """
    global _KB
    if _KB is None:
        from core import llm_improve

        kb = llm_improve.BPMNKnowledgeBase()

        def _offline(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("провенанс не загружает модель эмбеддингов")

        kb._sentence_model = _offline       # noqa: SLF001 — семантика выключена
        if not kb._load_cache():            # noqa: SLF001 — сборка без записи кэша
            kb._build()                     # noqa: SLF001
        kb._embeddings = None               # noqa: SLF001 — остаётся TF-IDF
        kb._ready = True                    # noqa: SLF001 — индекс уже собран
        _KB = kb
    return _KB


def rag_surfaces(scenario: Scenario) -> List[Dict[str, Any]]:
    """Что контур улучшения вставил бы в промпт для этого сценария.

    Запрос — ровно тот текст, который оркестратор подаёт поиску (задача
    улучшения), плюс описание процесса: в живом контуре к запросу добавляются
    имена элементов схемы, а схема собрана именно с этого описания. XML здесь не
    строится — вторая реализация генерации внутри аудита была бы хуже, чем такое
    приближение. Поверхностей две, по одной на запрос: `block` — дословный блок
    промпта (его собирает тот же `_format_practices`), `hits` — имена эталонов,
    которые в этот блок попали.
    """
    from core import llm_improve

    kb = knowledge_base()
    surfaces: List[Dict[str, Any]] = []
    for query in (scenario.improve_prompt, scenario.text):
        if not str(query or "").strip():
            continue
        hits = kb.find_best_practices(str(query))
        surfaces.append({
            "query": str(query)[:60],
            "block": llm_improve._format_practices(hits),
            # Шаги и форма подобранной схемы: `_format_practices` печатает из
            # этого только имя файла и фразы практик, но утечка мерится именно
            # по содержимому подборки, а не по тому, сколько строк влезло в
            # промпт.
            "hits": [{"name": str(h.get("name") or ""),
                      "similarity": float(h.get("similarity") or 0.0),
                      "element_names": str(h.get("element_names") or ""),
                      "xml_features": str(h.get("xml_features") or "")}
                     for h in hits],
        })
    return surfaces


def examples() -> List[Dict[str, Any]]:
    """Что модель читает в промпте как образец: few-shot ответы и статические
    тексты, из которых промпт собирается.

    Записи однородны (`id`/`where`/`text`/`payload`/`names`), но не одинаковы по
    природе: у `rules_only`-записей нет ответа примера — их текст обязан быть
    нейтральным, поэтому имена с них не снимаются, а участники сверяются через
    `mentions`. У записи `rag_practices` текст вообще известен только для
    конкретного запроса, поэтому она несёт отложенную выборку `retrieve`.
    """
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
        "id": GEN_ID,
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
        "id": IMPROVE_ID,
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
    # Промпт стадии маршрута: примера ответа в нём нет, но правила модель
    # читает целиком — имя участника, напечатанное в правиле, работает как
    # подсказка ответа (так уже ловился промпт состава).
    flow_prompt = str(bpmn_generator._FLOW_SYSTEM_PROMPT)
    found.append({
        "id": FLOW_ID,
        "where": "core.bpmn_generator._FLOW_SYSTEM_PROMPT",
        "text": flow_prompt,
        "payload": None,
        "names": set(),
        "rules_only": True,
    })

    # Правила скоринга уходят в промпт улучшения дословно (вместе с хвостом
    # «чинится: …»): блок «УЗКИЕ МЕСТА ПО СКОРИНГУ» модель читает как разбор её
    # собственной схемы, и имя шага, записанное в формулировку правила, она
    # вернёт обратно в схему. Лексика правил при этом обязана пересекаться с
    # любым процессом — отсюда `rules_only`.
    from core.bpmn_scoring import BPMNScorer

    rules_text = "\n".join(
        f"{name}: {rule['message']} → чинится: {', '.join(rule['action'])}"
        for name, rule in BPMNScorer().rules.items())
    found.append({
        "id": SCORING_ID,
        "where": "core.bpmn_scoring.BPMNScorer.rules",
        "text": rules_text,
        "payload": None,
        "names": set(),
        "rules_only": True,
    })

    # Формы операций: `_operations_block` печатает словарь аплайера прямо в
    # системный промпт, и пример имени элемента, вписанный в такую форму, модель
    # переписывает так же охотно, как пример из `ПРИМЕР ОТВЕТА`.
    found.append({
        "id": OP_SPEC_ID,
        "where": "core.bpmn_edits.OP_SPEC",
        "text": llm_improve._operations_block(),
        "payload": None,
        "names": set(),
        "rules_only": True,
    })

    # Практики из корпуса: какой эталон попадёт в промпт, зависит от запроса,
    # поэтому запись несёт отложенную выборку, а не текст.
    found.append({
        "id": RAG_ID,
        "where": "core.llm_improve.BPMNKnowledgeBase.find_best_practices",
        "text": "",
        "payload": None,
        "names": set(),
        "rules_only": True,
        "retrieve": rag_surfaces,
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


def significant(text: Any) -> Set[str]:
    """Значимые слова имени: короче четырёх символов и служебные — не в счёт."""
    return {t for t in _tokens(text) if len(t) >= 4} - NAME_NOISE


# Содержательная сверка блока промпта с эталоном кейса: сколько названий шагов
# эталона блок воспроизводит целиком и по каким порогам это читается как утечка.
# Доля, а не единичное совпадение: одно имя шага может встретиться в любом
# процессе («Проверить товар»), а воспроизведение половины маршрута — это уже
# тот же процесс, подобранный поиском.
ETALON_NAME_LEAK = 0.5
ETALON_MIN_NAMES = 4


def etalon_step_names(fixture: Optional[Mapping[str, Any]]) -> List[str]:
    """Названия шагов эталонного ответа кейса — те, что поиск обязан не знать.

    Берутся только имена с двумя и более значимыми словами: односложная метка
    («Проверка», «Оплата») есть в каждом втором процессе, и её попадание в блок
    ничего не доказывает. Одноимённые шаги схлопываются — дубль не должен
    завышать долю.
    """
    plan = (fixture or {}).get("plan") or {}
    names: List[str] = []
    for element in plan.get("elements") or []:
        name = str(element.get("name") or "").strip()
        if name and len(significant(name)) >= 2 and name not in names:
            names.append(name)
    return names


def names_the_scenario(name: Any, scenario: Scenario) -> bool:
    """Эталон назван тем же процессом, который измеряет кейс.

    Имя нормализуется тем же `_process_key`, что и поиск (хеш-хвост в именах
    файлов корпуса — часть имени, а не часть процесса). Сравнение двустороннее и
    без запала: либо все значимые слова id и заголовка сценария вошли в имя
    эталона, либо все значимые слова имени вошли в сценарий. Точное вложение
    читается как «это тот же процесс», а пересечение по одному слову ловило бы
    `Dispatch_of_goods` на каждом упоминании доставки — это словарь, а не
    утечка.
    """
    from core.llm_improve import _process_key

    given = significant(_process_key(str(name or "")))
    wanted = (significant(_process_key(scenario.id))
              | significant(scenario.title))
    return bool(given) and bool(wanted) and (given <= wanted or wanted <= given)


def scenario_findings(scenario: Scenario,
                      samples: Sequence[Dict[str, Any]],
                      etalon: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Чем именно сценарий пересекается с тем, что промпт даёт ему как образец.

    `etalon` — названия шагов эталонного ответа этого же кейса. Нужны потому,
    что сверка по имени файла корпуса ловит только тот случай, когда эталон
    назван тем же процессом, что и сценарий: рукописный корпус именован как
    попало, и тот же процесс под другим именем прошёл бы аудит незамеченным."""
    out: List[Dict[str, Any]] = []
    by_id = {e["id"]: e for e in samples}
    composition = [by_id.get(GEN_ID), by_id.get(ROSTER_ID),
                   by_id.get(RETRY_ID), by_id.get(FLOW_ID),
                   by_id.get(SCORING_ID), by_id.get(OP_SPEC_ID)]

    for gen in composition:
        if gen is None:
            continue
        # Правила промпта — не образец ответа: их лексика обязана пересекаться
        # с любым описанием процесса («шлюз», «шаг», «заявка»), и считать там
        # долю совпавших слов — ловить словарь вместо утечки. Из правил копируется
        # одно имя, его и проверяет `mentions`; текст описания и дальше
        # сверяется с образцами ответов.
        if gen.get("rules_only"):
            for participant in scenario.expected_participants:
                if mentions(participant, gen["text"]):
                    out.append(_finding(
                        scenario.id, "требование скопировать имя пула",
                        gen["id"], str(participant), 1.0))
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

    imp = by_id.get(IMPROVE_ID)
    if imp is not None and scenario.improve_prompt:
        ratio = containment(words(scenario.improve_prompt), words(imp["text"]))
        if ratio >= TEXT_CONTAINMENT:
            out.append(_finding(scenario.id, "задача улучшения как в образце",
                                imp["id"], "", ratio))

    # Практики корпуса: в промпт улучшения попадает не пример ответа, а
    # разобранный эталон — и если этот эталон есть тот же процесс, который
    # измеряет кейс, метрика мерит воспроизведение найденного ответа.
    rag = by_id.get(RAG_ID)
    if rag is not None:
        # Один и тот же участник может оказаться в блоках обоих запросов, а
        # находка — это факт об утечке, не факт о строке промпта: в отчёте она
        # звучит один раз.
        seen: Set[Any] = set()
        for surface in rag["retrieve"](scenario):
            block = surface.get("block") or ""
            for participant in scenario.expected_participants:
                key = ("имя", str(participant))
                if key in seen or not mentions(participant, block):
                    continue
                seen.add(key)
                out.append(_finding(
                    scenario.id, "требование скопировать имя пула", RAG_ID,
                    str(participant), 1.0))
            for hit in surface.get("hits") or []:
                key = ("эталон", hit["name"])
                if key in seen or not names_the_scenario(hit["name"], scenario):
                    continue
                seen.add(key)
                out.append(_finding(
                    scenario.id, "в промпт пришёл эталон этого же процесса",
                    RAG_ID, hit["name"], hit["similarity"]))
            # Подборка по процессу, названному иначе, чем сценарий: имя файла
            # молчит, а `element_names` хита — это шаги того же маршрута.
            for hit in surface.get("hits") or []:
                key = ("шаги хита", hit["name"])
                if key in seen or len(etalon) < ETALON_MIN_NAMES:
                    continue
                blob = hit.get("element_names") or ""
                reproduced = [nm for nm in etalon if mentions(nm, blob)]
                share = len(reproduced) / len(etalon)
                if share >= ETALON_NAME_LEAK and len(reproduced) >= 2:
                    seen.add(key)
                    out.append(_finding(
                        scenario.id, "подобранный эталон несёт шаги процесса "
                        "кейса", RAG_ID, f"{hit['name']}: "
                        + ", ".join(reproduced[:2]), share))
            # Тот же факт по содержанию блока: поиск мог принести процесс,
            # названный в файле иначе, чем сценарий.
            if len(etalon) >= ETALON_MIN_NAMES:
                reproduced = [nm for nm in etalon if mentions(nm, block)]
                share = len(reproduced) / len(etalon)
                key = ("содержание", round(share, 2))
                if (share >= ETALON_NAME_LEAK
                        and len(reproduced) >= 2
                        and key not in seen):
                    seen.add(key)
                    out.append(_finding(
                        scenario.id,
                        "блок практик воспроизводит половину эталона кейса",
                        RAG_ID, ", ".join(reproduced[:3]), share))
    return out


def fixture_findings(fixtures: Sequence[Dict[str, Any]],
                     samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Фикстура-«эталон» не должна быть копией образца: иначе она ничего не
    доказывает, а replay по ней мерит воспроизведение промпта.

    Пары «фикстура — образец» берутся по роду записи: план сверяется с
    few-shot планом генерации, пакет операций улучшения — с few-shot пакетом
    промпта улучшения. Сверять и то, и другое только с планом было слепотой:
    имена правок пакета с именами шагов плана не пересекаются, и improve-фикстура,
    дословно переписанная из примера в промпте, проходила аудит незамеченной.
    Имена снимаются одним `_payload_names` — с той же формы, что и у образца.
    """
    out: List[Dict[str, Any]] = []
    by_id = {e["id"]: e for e in samples}
    for fixture in fixtures:
        improve = str(fixture.get("kind") or "plan") == "improve"
        sample = by_id.get(IMPROVE_ID if improve else GEN_ID)
        if sample is None or not sample["names"]:
            continue
        payload = fixture if improve else fixture.get("plan")
        if not isinstance(payload, dict):
            continue
        names = {n.lower() for n in _payload_names(payload) if n}
        if not names:
            continue
        example_names = {n.lower() for n in sample["names"]}
        overlap = len(names & example_names) / len(names)
        if overlap >= NAME_OVERLAP:
            out.append(_finding(
                str(fixture.get("scenario", "?")),
                "пакет = образец промпта улучшения" if improve
                else "эталон = образец промпта",
                sample["id"], str(fixture.get("id", "?")), overlap))
    return out


# Форма маршрута: сравнение структур, а не названий. Одинаковая форма у разных
# процессов — норма, поэтому порог высокий и с минимумом дуг: сходство считается
# на схеме от MIN_ROUTE_EDGES рёбер, где совпадение формы уже значит больше, чем
# «там тоже два шлюза».
MIN_ROUTE_EDGES = 8
ROUTE_SIMILARITY = 0.8

_CORPUS = None


def corpus_xml(name):
    """XML корпусной схемы по имени хита поисковика (у хитов имя без `.bpmn`)."""
    global _CORPUS
    if _CORPUS is None:
        from eval.coverage import corpus_files
        _CORPUS = {n.rsplit(".", 1)[0]: xml for n, xml in corpus_files()}
    return _CORPUS.get(str(name or ""), "")


def route_signature(elements, flows):
    """Форма маршрута как мультимножество дуг `(род источника, род цели)`.

    Роды берутся из BPMN-тега (или plan-`kind`), имена и id не участвуют: смысл
    проверки в том, чтобы увидеть тот же процесс, когда в нём переименовано всё.
    Счётчик в паре держит повторные одинаковые ноги — иначе параллельные ветки
    схлопнулись бы в одну дугу, и форма обеднела бы именно там, где она
    различается.
    """
    from collections import Counter

    kind_of = {}
    for element in elements:
        if isinstance(element, dict):
            eid = str(element.get("id") or "")
            kind = str(element.get("kind") or "")
        else:
            eid = str(element.get("id") or "")
            kind = str(element.tag).split("}")[-1]
        if eid and kind:
            kind_of[eid] = kind
    edges = Counter()
    for flow in flows:
        if isinstance(flow, dict):
            src = str(flow.get("source") or "")
            dst = str(flow.get("target") or "")
            if str(flow.get("kind") or "sequence") != "sequence":
                continue
        else:
            src = str(flow.get("sourceRef") or "")
            dst = str(flow.get("targetRef") or "")
        a, b = kind_of.get(src, ""), kind_of.get(dst, "")
        if not a or not b:
            continue
        edges[(a, b)] += 1
    return {(pair, count) for pair, count in edges.items()}


def route_edges_count(signature):
    return sum(count for _, count in signature)


def route_similarity(left, right):
    """Жаккард мультимножества дуг двух форм маршрута, 0.0 на мелких схемах."""
    if route_edges_count(left) < MIN_ROUTE_EDGES or route_edges_count(right) < MIN_ROUTE_EDGES:
        return 0.0
    left_pairs = dict(left)
    right_pairs = dict(right)
    shared = sum(min(count, right_pairs.get(pair, 0)) for pair, count in left_pairs.items())
    total = (sum(left_pairs.values()) + sum(right_pairs.values()) - shared)
    return shared / total if total else 0.0


def route_margin(scenario, fixture):
    """Максимум сходства формы маршрута между эталоном кейса и подборкой — и имя
    той схемы корпуса, которая этот максимум дала.

    Названия шагов — сильная, но хрупкая опора: процесс, переименованный до
    последнего слова, проходит мимо неё. Форма переименований не боится, поэтому
    сверяется и она, а расстояние печатается в отчёте даже при нуле находок.

    Имя нужно наряду с числом: «форма совпала» без него — приговор всему корпусу
    из 367 файлов, а проверять глазами приходится одну схему, которую поисковик
    принёс в промпт. Число само по себе не говорит, куда смотреть.
    """
    import xml.etree.ElementTree as ET

    plan = (fixture or {}).get("plan") or {}
    own = route_signature(plan.get("elements") or [], plan.get("flows") or [])
    if route_edges_count(own) < MIN_ROUTE_EDGES:
        return 0.0, ""
    best, best_name = 0.0, ""
    for surface in rag_surfaces(scenario):
        for hit in surface.get("hits") or []:
            name = str(hit.get("name") or "")
            xml = corpus_xml(name)
            if not xml:
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue
            nodes = [e for e in root.iter()
                     if isinstance(e.tag, str) and e.get("id")]
            flows = [e for e in root.iter()
                     if isinstance(e.tag, str) and e.tag.split("}")[-1] == "sequenceFlow"]
            similarity = route_similarity(own, route_signature(nodes, flows))
            if similarity > best:
                best, best_name = similarity, name
    return best, best_name


def overlap_margin(scenario: Scenario, etalon: Sequence[str]) -> float:
    """Доля шагов эталона, которую воспроизводит самый близкий подобранный
    эталон: 0.0 у аудита значит столько же, сколько 0.49.

    Без этого числа «находок нет» неотличимо от «детектор слеп»: ноль совпадений
    даёт и чистый набор, и пустое поле `element_names`. Порог тот же, что у
    находки, поэтому расстояние читается прямо против него.
    """
    if len(etalon) < ETALON_MIN_NAMES:
        return 0.0
    best = 0.0
    for surface in rag_surfaces(scenario):
        for hit in surface.get("hits") or []:
            blob = hit.get("element_names") or ""
            share = sum(1 for nm in etalon if mentions(nm, blob)) / len(etalon)
            best = max(best, share)
    return best


def audit(scenarios: Optional[Sequence[Scenario]] = None,
          fixtures: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Полный разбор провенанса: образцы, находки по сценариям и фикстурам."""
    from eval.harness import load_fixtures

    samples = examples()
    chosen = list(scenarios) if scenarios is not None else all_scenarios()
    docs = list(fixtures) if fixtures is not None else load_fixtures()
    # Содержательная сверка идёт по плану любого качества: цель — поймать
    # процесс, который поиск принёс как образец, а он тот же, что измеряет
    # кейс. Ограничивать «good» — значит ослепнуть ровно на тех сценариях, где
    # эталонной фиксации пока нет (проверка снимает имена с `bad`-плана так же
    # хорошо), а «bogus»-качество исключено: это подделка ответа, а не маршрут.
    priority = {"good": 0, "real": 1, "": 1, "bad": 2}
    etalons: Dict[str, List[str]] = {}
    rank: Dict[str, int] = {}
    for fixture in docs:
        scene = str(fixture.get("scenario") or "")
        quality = str(fixture.get("quality") or "")
        if not scene or quality == "bogus":
            continue
        names = etalon_step_names(fixture)
        if len(names) < ETALON_MIN_NAMES:
            continue
        order = priority.get(quality, 3)
        if order < rank.get(scene, 99) or (order == rank.get(scene) and
                                           len(names) > len(etalons.get(scene, []))):
            rank[scene], etalons[scene] = order, names
    # Фикстура для сверки формы — та, у которой маршрут богаче: на пустом плане
    # расстояние было бы нулём не потому, что утечки нет, а потому, что сравнивать
    # нечего, и отчёт это скрывал бы.
    plans: Dict[str, Dict[str, Any]] = {}
    plan_edges: Dict[str, int] = {}
    for fixture in docs:
        scene = str(fixture.get("scenario") or "")
        if not scene:
            continue
        edges = route_edges_count(route_signature(
            (fixture.get("plan") or {}).get("elements") or [],
            (fixture.get("plan") or {}).get("flows") or []))
        if edges > plan_edges.get(scene, -1):
            plans[scene], plan_edges[scene] = fixture, edges
    margins = {sc.id: overlap_margin(sc, etalons.get(sc.id, ())) for sc in chosen}
    matches = {sc.id: route_margin(sc, plans.get(sc.id)) for sc in chosen}
    routes = {k: v[0] for k, v in matches.items()}
    # Хит поисковика назван без расширения, а адрес, по которому находку открывают
    # глазами, — имя файла корпуса.
    route_files = {k: (f"{v[1]}.bpmn" if v[1] else "") for k, v in matches.items()}
    worst = max(margins, key=margins.get) if margins else ""
    worst_route = max(routes, key=routes.get) if routes else ""
    findings: List[Dict[str, Any]] = []
    for scenario in chosen:
        findings += scenario_findings(scenario, samples, etalons.get(scenario.id, ()))
        similarity = routes.get(scenario.id, 0.0)
        if similarity >= ROUTE_SIMILARITY:
            name = route_files.get(scenario.id, "")
            findings.append(_finding(
                scenario.id, "подобранная схема повторяет форму маршрута кейса",
                RAG_ID,
                f"{(plans.get(scenario.id) or {}).get('id', '?')}"
                + (f" ← {name}" if name else ""),
                similarity))
    findings += fixture_findings(docs, samples)
    return {
        "examples": [{"id": e["id"], "where": e["where"],
                      "names": sorted(e["names"])} for e in samples],
        # Насколько близко подобранное прошело мимо порога: расстояние, а не
        # только факт находки.
        "overlap_margins": {k: round(v, 3) for k, v in margins.items()},
        "closest": {"scenario": worst, "measure": round(margins.get(worst, 0.0), 3)}
        if worst else {},
        "route_margins": {k: round(v, 3) for k, v in routes.items()},
        # Адрес ближайшей подборки по каждому сценарию: число без имени не
        # говорит, какую схему корпуса смотреть глазами.
        "route_files": route_files,
        "closest_route": {"scenario": worst_route,
                          "measure": round(routes.get(worst_route, 0.0), 3),
                          "file": route_files.get(worst_route, "")}
        if worst_route else {},
        # Полнота содержательной проверки: сценарии, где у аудита есть с чем
        # сверять блок практик. Пустой список при живом наборе кейсов = проверка
        # мертва, и это должно быть видно в отчёте, а не выведено молчанием.
        "etalon_scenarios": sorted(etalons),
        "scenarios": sorted(sc.id for sc in chosen),
        "findings": findings,
        "covered": sorted({f["scenario"] for f in findings}),
        "clean": not findings,
    }


def format_findings(report: Dict[str, Any]) -> List[str]:
    """Строки находок для консоли и отчёта (пусто = набор кейсов чист)."""
    # Полнота содержательной сверки печатается всегда, и при чистом наборе тоже:
    # «находок нет» от аудита, который видел половину сценариев, и «находок нет»
    # от аудита по всему набору — это разные утверждения.
    closest = report.get("closest") or {}
    closest_route = report.get("closest_route") or {}
    # Схема-адрес рядом с числом: без него «форма маршрута близка» неоткуда
    # проверить, а находка в отчёте живёт одной строкой.
    where = (", ближайшая схема корпуса — " + closest_route["file"]
             if closest_route.get("file") else "")
    coverage = ("содержательная сверка с эталоном кейса: "
                f"{len(report.get('etalon_scenarios') or [])} из "
                f"{len(report.get('scenarios') or [])} сценариев набора; "
                f"ближайший подбор — {closest.get('measure', 0):.0%} совпадений "
                f"шагов (порог {ETALON_NAME_LEAK:.0%}, кейс "
                f"{closest.get('scenario', '?')}); форма маршрута — "
                f"{closest_route.get('measure', 0):.0%} "
                f"дуг (порог {ROUTE_SIMILARITY:.0%}, кейс "
                f"{closest_route.get('scenario', '?')}{where})")
    if not report["findings"]:
        return ["пересечений с few-shot образцами промптов нет", coverage]
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

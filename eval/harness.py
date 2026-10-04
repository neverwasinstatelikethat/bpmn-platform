"""Оркестрация офлайн-оценки: план модели → структура → XML → инварианты и балл.

Два режима. `replay` берёт записанные ответы модели из `eval/fixtures/` — он
детерминирован, не требует сети и ключа и годится как baseline в CI. `live`
ходит в GigaChat теми же путями, что приложение, и измеряет разброс повторами.

Вызовы живого кода (`repair_structure`, генерация XML, аплайер, скоринг)
резолвятся по имени на лету: `core/bpmn_generator.py` и `core/bpmn_edits.py`
меняются прямо сейчас, и харнесс не имеет права падать из-за переименования
функции — он обязан либо найти её под другим именем, либо честно зафиксировать
несобранный кейс.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping,
                    Optional, Sequence, Tuple)

from . import attribution, invariants, metrics, provenance
from .invariants import Check
from .scenarios import Scenario, get as scenario_by_id, select

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
BASELINES_DIR = EVAL_DIR / "baselines"
REPORTS_DIR = EVAL_DIR / "reports"
DEFAULT_BASELINE = BASELINES_DIR / "current.json"

# Метрики, у которых «меньше — лучше»: без этой пометки детектор регрессий
# считал бы ухудшением любой рост задержки или числа правок. Направление каждой
# метрики объявляет и сам suite (`direction=metrics.LOWER`), а в baseline оно
# уезжает из `RunReport.directions()` — этот набор лишь запасной путь для сверки
# со старым слепком, где directions нет. Поэтому новый LOWER-замер добавляют и
# сюда, иначе fallback начнёт расходиться с объявлением.
LOWER_IS_BETTER = frozenset({
    "repairs_per_scheme", "latency_ms", "llm_retries", "generation_error_share",
    "improve/noop_share", "improve/package_revert_share",
    "improve/rules_regressed_share", "improve/plan_truncated_share",
    "improve/repeat_rejection_share", "improve/defects_introduced",
    "improve/base_advice_drift", "improve/advice_off_target_share",
    "business/smells",
    # Размер входа — стоимость, а не качество: «меньше» здесь лучше ровно до тех
    # пор, пока не начинает резать правила из промпта (красная линия проекта —
    # не ослаблять их ради числа), поэтому метрика диагностируется, а не гейтится
    # тем же смыслом, что и доли корректности.
    "cost/prompt_chars_per_call", "cost/prompt_chars_per_case",
    "cost/prompt_tokens_per_call", "cost/prompt_tokens_per_case",
    # Межсценарный разброс балла — свойство набора фикстур, а не контура: он
    # меняется от того, сколько сценариев выбрали (`--scenarios`), и гейтить его
    # нельзя. В LOWER_IS_BETTER имя остаётся, чтобы направление было объявлено
    # и в резервном пути сверки со старым слепком, но в baseline число не
    # кладётся (см. `RunReport.metrics_flat`).
    "spread/score_cv_across_scenarios", "spread/score_cv_per_case",
})

# Минимум повторов, при котором дисперсия живой модели становится мерой: при 2
# прогонах коэффициент вариации — это разница двух случайностей, и называть её
# «разбросом live» значило бы выдавать шум за измерение (живые прогоны #56/#57
# расходятся на 0.05–0.06 между собой именно так).
MIN_LIVE_REPEATS = 3

# Претензия живого кейса «по факту схемы». Текст общий намеренно: ни id, ни
# имён участников, ни названий правил — какой именно дефект чинить, оркестратор
# достаёт сам из скоринга (`core/llm_improve.py:1013`), а харнесс не имеет права
# подсказывать модели ответ, которого нет в продукте.
ADVICE_PROMPT = ("В схеме есть проблемы. Убери то, что находят проверка нотации и "
                 "скоринг, и не меняй то, что уже проходит. Не выдумывай "
                 "участников и шагов, которых нет в описании процесса.")

# Планка сводного качества: доля применимых гейт-проверок, пройденных одной
# схемой. Числом совпадает с прежним обещанием харнесса («каждый инвариант
# корректности ≥0.8»), но считается по схеме, а не по конъюнкции: среднее по
# живому прогону #56 уже 0.853 там, где `pass@1/scenario` даёт 0.062.
# Литерала здесь нет: та же планка двигает гейтом `--fail-on-regression`
# (`eval/run.py:bar_verdict`), и две копии разъехали бы гейт и отчёт молча —
# держится одно число в `metrics.QUALITY_BAR`.
QUALITY_BAR = metrics.QUALITY_BAR
# Что в итоговой схеме решает человек, а не линейка. Формулировки — вопросы, а не
# критерии с числом: у ответа нет меры, и выдумывать её значило бы подменить
# ручной вердикт ещё одной самоподтверждающейся метрикой.
MANUAL_REVIEW_ITEMS = (
    "имя каждого шага читается как действие, а не как отглагольное существительное",
    "в схеме нет шага, которого нет в описании — выдуманное действие остаётся "
    "браком, даже когда нотация цела",
    "участники названы словами описания: роль — дорожка, система — пул, срок — "
    "таймер там, где в тексте срок",
    "схема открывается в bpmn-js, и маршрут виден без разбора id",
)


class HarnessError(RuntimeError):
    """Непреодолимая проблема харнесса: нет фикстур, не найден вход и т. п."""


class _CaseFailure(Exception):
    """Внутренний сигнал: сцена провалилась, прогон продолжается."""


# ---------------------------------------------------------------------------
# резолв живых функций (генератор и аплайер меняются параллельно)
# ---------------------------------------------------------------------------


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise HarnessError(f"не импортируется {name}: {e}") from e


def _resolve(module_name: str, *candidates: str) -> Optional[Callable[..., Any]]:
    """Первая найденная вызываемая функция среди кандидатов."""
    module = _module(module_name)
    for name in candidates:
        found = getattr(module, name, None)
        if callable(found):
            return found
    return None


def _resolve_method(class_name: str, *candidates: str) -> Optional[Callable[..., Any]]:
    """Метод класса генератора: имя публичного метода могут менять на
    приватный и наоборот, поэтому перебираем оба варианта."""
    for module_name in ("core.bpmn_generator", "core.bpmn_edits"):
        cls = getattr(_module(module_name), class_name, None)
        if cls is None:
            continue
        instance = cls()
        for name in candidates:
            found = getattr(instance, name, None)
            if callable(found):
                return found
    return None


def repair_structure(plan: Mapping[str, Any],
                     trace: Optional[List[Dict[str, Any]]] = None,
                     text: str = "",
                     lost: Optional[List[str]] = None,
                     ) -> Tuple[Dict[str, Any], List[str]]:
    """Починка структуры устойчива к смене сигнатуры: кортеж (структура,
    пометки) или только структура — разбираются оба варианта.

    `trace` — сборщик отпечатка шагов починки для атрибуции дефектов. Его
    передают ядру только если функция починки действительно принимает такой
    параметр: харнесс не фиксирует под собой сигнатуру, а спрашивает её.

    `text` и `lost` — то же требование к продуктивному пути. Без описания
    починка не отличает «убрали артефакт» от «сошёл названный участник»
    (`_drop_vacant_pools`), а без `lost` потеря не попадает в `gaps`: replay
    мерял бы контур, который физически не способен потерять контрагента, —
    расхождение с живым прогоном ровно там, где его измеряют отдельно и за него
    готовы платить.
    """
    fn = _resolve("core.bpmn_generator", "repair_structure", "repair_plan",
                  "normalize_structure")
    if fn is None:
        raise HarnessError("в core/bpmn_generator нет функции починки структуры "
                           "(repair_structure/repair_plan)")
    names = _signature_names(fn)
    kwargs: Dict[str, Any] = {}
    if trace is not None and "trace" in names:
        kwargs["trace"] = trace
    if text and "text" in names:
        kwargs["text"] = text
    if lost is not None and "lost" in names:
        kwargs["lost"] = lost
    result = fn(dict(plan), **kwargs)
    if isinstance(result, tuple):
        structure = result[0] if len(result) > 0 else {}
        notes = list(result[1]) if len(result) > 1 and result[1] else []
        return structure or {}, notes
    return (result or {}), []


def _signature_names(fn: Any) -> List[str]:
    """Имена параметров функции (у встроенной без сигнатуры — пустой список)."""
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # noqa: PERF203 — один вызов на кейс
        return []


def generate_xml(structure: Mapping[str, Any]) -> str:
    """Семантический XML по починенной структуре — только фактическая функция
    генератора, своих шаблонов сериализации харнесс не содержит."""
    fn = _resolve("core.bpmn_generator", "generate_bpmn_xml", "build_bpmn_xml",
                  "structure_to_xml")
    if fn is not None:
        return fn(dict(structure))
    method = _resolve_method("BPMNGenerator", "generate_bpmn_xml",
                             "_generate_bpmn_xml")
    if method is None:
        raise HarnessError("не найден генератор XML: BPMNGenerator._generate_bpmn_xml "
                           "или bpmn_generator.generate_bpmn_xml")
    return method(dict(structure))


def score_xml(xml: str) -> Dict[str, Any]:
    """Балл скоринга. Оракул харнесса от него независим — здесь скоринг нужен
    только как измеряемая величина, а не как критерий приёмки."""
    scorer = getattr(_module("core.bpmn_scoring"), "BPMNScorer", None)
    if scorer is None:
        return {"score": None, "recommendations": [], "details": {}}
    return scorer().evaluate(xml)


def ruler_fingerprint() -> Dict[str, Any]:
    """Отпечаток линейки: чем измеряют схему.

    Нужен потому, что baseline несопоставим сам по себе, если изменилась линейка:
    добавление правила, которое заряжает то, что раньше прощалось, роняет
    `improve/score_delta` и `score/*` — и сверка объявляет это регрессией контура,
    хотя контур не менялся, а мерка стала короче. Ослабить оракул или правило,
    чтобы цифра вернулась, было бы обманом; честный ответ — сказать, что
    сравнивать нечего, и попросить пересобрать baseline.

    Считается по names+весам, а не по числу правил: переименование правила или
    перенос веса между двумя — тоже смена линейки, а число останется прежним.
    """
    scorer = getattr(_module("core.bpmn_scoring"), "BPMNScorer", None)
    rules: Dict[str, int] = {}
    if scorer is not None:
        for name, rule in scorer().rules.items():
            rules[name] = int(rule.get("weight") or 0)
    return {
        "rules": rules,
        "weights_total": sum(rules.values()),
        "invariants": sorted(invariants.ALL_CHECKS),
    }


def ruler_hash(fingerprint: Mapping[str, Any]) -> str:
    """Короткий отпечаток линейки — одна строка вместо перечня из 26 правил.

    Нужен потому, что «линейка изменилась» без адреса не проверить: в отчёте и в
    baseline должно читаться, каким именно набором правил мерил прогон, а
    разворот всей таблицы в сводку никто не прочтёт. Считается по нормализованному
    JSON, поэтому не зависит от порядка ключей и переживает перезапись baseline.

    В самом отпечатке (`ruler_fingerprint`) хэша нет намеренно: `ruler_note`
    сверяет словари целиком, и производный ключ внутри них заставил бы каждый
    старый baseline выглядеть собранном другой линейкой.
    """
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def ruler_note(current: Mapping[str, Any],
               baseline: Mapping[str, Any]) -> str:
    """Почему сверка с baseline пропущена, или пусто, если сверять можно."""
    base = baseline.get("ruler") or {}
    if not base:
        return ""
    if base == dict(current):
        return ""
    base_rules, now_rules = base.get("rules") or {}, current.get("rules") or {}
    gone = sorted(set(base_rules) - set(now_rules))
    added = sorted(set(now_rules) - set(base_rules))
    rescored = sorted(k for k in set(base_rules) & set(now_rules)
                      if base_rules[k] != now_rules[k])
    changed = sorted(set(base.get("invariants") or ())
                     ^ set(current.get("invariants") or ()))
    parts = []
    if added:
        parts.append("добавились: " + ", ".join(added))
    if gone:
        parts.append("ушли: " + ", ".join(gone))
    if rescored:
        parts.append("вес изменён у: " + ", ".join(rescored))
    if changed:
        parts.append("инварианты: " + ", ".join(changed))
    return (f"линейка изменилась ({len(base_rules)} правил, "
            f"вес {base.get('weights_total')} → {len(now_rules)} правил, вес "
            f"{current.get('weights_total')}; отпечатки {ruler_hash(base)} → "
            f"{ruler_hash(current)}; {'; '.join(parts)}): балл и доли "
            "прохождения несопоставимы, сверка с baseline пропущена. Новые "
            "правила обязаны заряжать то, что раньше прощалось, — проверьте это "
            "на корпусе (`python -m eval.coverage` и `python -m eval.advice "
            "--all-rules`), затем пересоберите baseline (--write-baseline) либо "
            "прогоните с явным --accept-ruler-change, если изменение линейки "
            "осознанно и сверка отложена до пересборки")


def agreement_share(drift: Mapping[str, Mapping[str, Any]]) -> Optional[float]:
    """Доля бизнес-правил, где оракул и скоринг не разошлись по одной схеме.

    `no_data` в знаменатель не входит: молчание из-за отсутствующего слоя — не
    согласие. `not_comparable` входит: там оба слоя дефекта не нашли, спорить не
    о чем. None, если сравнивать было нечего ни по одному правилу, — метрика не
    должна раздувать выборку нулями там, где проверки не было.
    """
    comparable = [row for row in drift.values()
                  if row.get("verdict") != invariants.NO_DATA]
    if not comparable:
        return None
    disagreed = sum(1 for row in comparable if row.get("verdict") in
                    (invariants.ORACLE_STRICTER, invariants.SCORER_STRICTER))
    return float(len(comparable) - disagreed) / len(comparable)


def unrouted_notes(notes: Sequence[str]) -> List[str]:
    """Пометки починки об узлах вне маршрута — маркеры берёт аплайер.

    Локальных входов `apply_operations`/`validate_and_repair` здесь больше нет:
    пакет применяется только гарантом (`_apply_package`), и отдельной двери
    «применить без отката» быть не должно — она и держала короткую копию контура.
    """
    markers = getattr(_module("core.bpmn_edits"), "UNROUTED_NOTE_MARKERS", ())
    return [n for n in notes if any(m in n for m in markers)]


# ---------------------------------------------------------------------------
# фикстуры
# ---------------------------------------------------------------------------


def load_fixtures(scenario_ids: Optional[Sequence[str]] = None,
                  kinds: Sequence[str] = ("plan", "improve"),
                  fixtures_dir: Path = FIXTURES_DIR) -> List[Dict[str, Any]]:
    """Записанные ответы модели, отсортированные по пути файла.

    Порядок обхода фиксирован: прогон обязан быть воспроизводимым, иначе
    baseline и текущий результат нельзя сравнить построчно.
    """
    wanted = set(scenario_ids or [])
    out: List[Dict[str, Any]] = []
    for path in sorted(Path(fixtures_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise HarnessError(f"фикстура {path.name} не разбирается: {e}") from e
        if not isinstance(data, dict):
            continue
        data.setdefault("id", path.stem)
        data.setdefault("kind", "plan")
        data.setdefault("label", path.stem)
        data.setdefault("quality", "unknown")
        data["path"] = str(path)
        if data["kind"] not in kinds:
            continue
        if wanted and data.get("scenario") not in wanted:
            continue
        out.append(data)
    return out


def fixture_by_id(fixture_id: str,
                  fixtures: Iterable[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    return next((f for f in fixtures if f.get("id") == fixture_id), None)


# ---------------------------------------------------------------------------
# кейсы
# ---------------------------------------------------------------------------


@dataclass
class GenCase:
    """Один прогон генерации: сценарий × фикстура × повтор."""

    scenario: str
    fixture: str
    label: str
    quality: str
    mode: str
    repeat: int = 0
    ok: bool = False
    error: str = ""
    notes: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    structure: Dict[str, Any] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    attribution: Dict[str, str] = field(default_factory=dict)
    xml: str = ""
    checks_structure: Dict[str, Check] = field(default_factory=dict)
    checks_xml: Dict[str, Check] = field(default_factory=dict)
    score: Optional[float] = None
    failed_rules: List[str] = field(default_factory=list)
    #: весь ответ скоринга по той же схеме: нужен для сверки двух слоёв
    #: (`business/scorer_oracle_agreement`), а не только балла. `business_agreement`
    #: читает из него `details_meta` сам — плоский словарь статусов вместо ответа
    #: дал бы ему «все правила пройдены» на любой схеме.
    scorer_evaluation: Dict[str, Any] = field(default_factory=dict)
    pools: int = 0
    elements: int = 0
    message_flows: int = 0
    latency_ms: float = 0.0
    llm_calls: int = 0
    #: вызовы этого кейса с размером входа: в live — записанные с транспорта,
    #: в replay — посчитанные по шаблонам ядра (`observed=False` в каждой строке).
    prompt_calls: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.scenario}/{self.fixture}"

    @property
    def repairs(self) -> int:
        return len(self.notes)

    @property
    def disagreements(self) -> List[str]:
        if not self.checks_structure or not self.checks_xml:
            return []
        return invariants.disagreements(self.checks_structure, self.checks_xml)

    @property
    def scenario_pass(self) -> Optional[bool]:
        """Сводный исход: корректностные инварианты по итоговому XML пройдены.

        Бизнес-слой в гейт не входит — см. `invariants.deciding_checks`: он
        измеряется плотностью узких мест (`business/smells`) относительно
        эталона того же сценария, а не конъюнкцией «ни одного», которую не
        выполняет ни одна конечная схема.
        """
        if not self.checks_xml:
            return None
        return all(c.ok for c in invariants.deciding_checks(self.checks_xml))

    @property
    def business_smells(self) -> List[str]:
        """Бизнес-инварианты, проваленные итоговой схемой (с names для отчёта)."""
        return [c.name for c in invariants.business_smells(self.checks_xml)]

    @property
    def gate_share(self) -> Optional[float]:
        """Доля пройденных гейт-проверок ЭТОЙ схемы — сводная мера вместо
        конъюнкции.

        `pass@1/scenario` требовал, чтобы одна схема прошла все применимые
        проверки сразу: в живом прогоне #56 это 0.062 при том, что в среднем по
        кейсам провалено 15% проверок (сводная доля — 0.853). Конъюнкция полезна
        как сигнал «ноль дефектов», но как планка она меряет не контур, а
        геометрическую вероятность собрать семнадцать независимых условий, и
        поднять её можно только всею охотой за классами сразу. Эта доля — та же
        самая проверка, но монотонная: каждое снятое нарушение добавляет число,
        ничего не срезая, и на ней планка ≥0.8 достижима уже сегодня.
        Конъюнкция из данных не пропала — она осталась фактом кейса
        (`scenario_pass`, он же в имени файла `--dump-schemes`); метрикой в
        таблице она перестала быть, потому что отчитываться таким числом —
        всегда ноль.
        None, когда применимых проверок нет (пустая схема, не разобралась).

        Несобравшийся кейс даёт 0.0, а не None: раньше сцена, упавшая на
        генерации, выпадала из знаменателя, и живой прогон #53–#54 терял пять
        кейсов из 24 — доли при этом считались по уцелевшим и «has_timer 0.667 →
        1.0» оказывалось сменой выборки, а не улучшением. Схема, которой нет, не
        прошла ни одной проверки; что именно из кейсов не собралось — говорит
        `generation_error_share`, а не молчание в знаменателе.
        """
        if not self.ok:
            return 0.0
        checks = invariants.deciding_checks(self.checks_xml)
        if not checks:
            return None
        return float(sum(1 for c in checks if c.ok)) / len(checks)

    @property
    def structure_gate_share(self) -> Optional[float]:
        """То же число по починенной структуре — до генерации XML.

        Нужна отдельной строкой, потому что гейт на структуре и гейт на XML
        ловят разных виновников: здесь видно, что план уже был неполон, ещё до
        транспорта XML (разграничение в `eval/attribution.py`).

        Тот же порядок, что у `gate_share`: несобравшийся кейс остаётся в
        знаменателе с нулём.
        """
        if not self.ok:
            return 0.0
        checks = invariants.deciding_checks(self.checks_structure)
        if not checks:
            return None
        return float(sum(1 for c in checks if c.ok)) / len(checks)

    @property
    def business_share(self) -> Optional[float]:
        """Доля пройденных бизнес-проверок той же схемы.

        Числом она ниже гейт-доли и не является планкой: потолок задан набором,
        а не контуром (ожидания без срока есть и в эталонном ответе —
        `EXPECTED_ETALON_BUSINESS_DEBT`). Нужна для того, чтобы движение
        бизнес-слоя было видно без чтения плотности узких мест.
        """
        checks = [c for c in invariants.applicable_checks(self.checks_xml)
                  if c.name in invariants.BUSINESS_INVARIANTS]
        if not checks:
            return None
        return float(sum(1 for c in checks if c.ok)) / len(checks)

    @property
    def drift(self) -> Dict[str, Dict[str, Any]]:
        """Таблица «оракул ↔ скоринг» по бизнес-слою этой же схемы.

        Оба слоя проверяют одно и то свойство процесса независимо, поэтому
        расхождение — это дефект одного из прочтений, а не шум: без этой
        таблицы дрейф линеек (подсказка скоринга и критерий оракула разъезжаются
        по одному правилу за правку) остаётся невидимым.
        """
        if not self.checks_xml or not self.scorer_evaluation:
            return {}
        return invariants.business_agreement(self.checks_xml,
                                             self.scorer_evaluation)

    @property
    def drift_lines(self) -> List[str]:
        """Только строки, где слои не согласны."""
        rows = {rule: self.drift[rule]
                for rule in invariants.business_disagreements(self.drift)}
        return invariants.format_business_agreement(rows)

    def measure(self, expectations: Mapping[str, Any]) -> None:
        """Инварианты по структуре и XML + балл скоринга."""
        self.checks_structure = invariants.check_structure(self.structure, expectations)
        if not self.xml:
            return
        self.checks_xml = invariants.check_xml(self.xml, expectations)
        evaluation = score_xml(self.xml)
        self.score = evaluation.get("score")
        self.scorer_evaluation = evaluation
        self.failed_rules = [name for name, passed in
                             (evaluation.get("details") or {}).items() if not passed]
        self.pools = len(self.structure.get("participants") or [])
        self.elements = len(self.structure.get("elements") or [])
        self.message_flows = len([f for f in (self.structure.get("flows") or [])
                                  if str(f.get("kind", "")).lower() == "message"])
        self.attribution = attribution.attribute_generation(
            self.checks_xml, self.checks_structure, self.trace, self.gaps)
        self.ok = True

    @property
    def participants_named(self) -> List[str]:
        """Имена, которые оракул считает участниками: пулы и дорожки.

        Отчёт без них не отвечает на главный вопрос отказа `expected_participants`
        — не назвала ли модель контрагента вообще."""
        out = [str(p.get("name", "")) for p in (self.structure.get("participants") or [])
               if isinstance(p, dict)]
        out += [str(l.get("name", "")) for l in (self.structure.get("lanes") or [])
                if isinstance(l, dict)]
        return [o for o in out if o]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario, "fixture": self.fixture, "label": self.label,
            "quality": self.quality, "mode": self.mode, "repeat": self.repeat,
            "ok": self.ok, "error": self.error, "scenario_pass": self.scenario_pass,
            "scorer_vs_oracle": self.drift_lines,
            "business_smells": self.business_smells,
            "score": self.score, "failed_rules": self.failed_rules,
            "repairs": self.repairs, "notes": self.notes, "gaps": self.gaps,
            "participants_named": self.participants_named,
            "pools": self.pools, "elements": self.elements,
            "message_flows": self.message_flows,
            "latency_ms": round(self.latency_ms, 1), "llm_calls": self.llm_calls,
            "stage_path": stage_path(self),
            "prompt_calls": list(self.prompt_calls),
            "prompt_totals": prompt_totals(self.prompt_calls),
            "structure_vs_xml": self.disagreements,
            "attribution": self.attribution,
            "trace": self.trace,
            "checks_xml": {k: v.as_dict() for k, v in self.checks_xml.items()},
            "checks_structure": {k: v.as_dict()
                                 for k, v in self.checks_structure.items()},
            "summary_xml": invariants.summarize(self.checks_xml) if self.checks_xml else {},
        }


@dataclass
class ImproveCase:
    """Один прогон улучшения: пакет операций поверх схемы генерации.

    Поля ниже — факты отчёта применения, а не выводы харнесса: `None` читается
    как «факт не собран», `0` — как «факт собран и он нулевой». Раньше
    «потребовался ли повтор» выводился из наличия строк со стадией `retry`, и
    это давало ложный ноль там, где повтор закрыл все отказы первого раунда и
    новых не принёс; битые кейсы при этом добавляли 0.0 в знаменатель доли.
    Откатанный пакет оставался в отчёте с полным `applied`, потому что харнесс
    не играл гарант целиком.
    """

    scenario: str
    fixture: str
    label: str
    quality: str
    mode: str
    repeat: int = 0
    ok: bool = False
    error: str = ""
    applied: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    repair_notes: List[str] = field(default_factory=list)
    retry_attempted: Optional[bool] = None
    retry_closed: Optional[int] = None
    package_reverted: Optional[str] = None
    rules_regressed: Optional[Dict[str, str]] = None
    noop_rows: Optional[int] = None
    truncated_operations: Optional[int] = None
    #: пакеты операций, которые контур отдал гаранту, по раундам: первый план и
    #: корректирующий повтор. По паре `_before`/`_after` и строкам отказа
    #: неотличим разрыв, снятый стражом дыр маршрута, от дуги, вырезанной
    #: починкой, или от переноса между пулами, — а это разные очереди работы, и
    #: без пакета отказ `package_reverted` читается только как факт «откачено».
    #: `None` — факт не собран (режим, где гарант не вызывался).
    operations_packages: Optional[List[List[Dict[str, Any]]]] = None
    #: что харнесс не смог померить на этом кейсе (съехавшее имя живого кода,
    #: отсутствующий параметр сигнатуры) — замер неполон, и обязан говорить об
    #: этом, а не молча подменять его локальной копией контура.
    unmeasured: List[str] = field(default_factory=list)
    score_before: Optional[float] = None
    score_after: Optional[float] = None
    #: весь ответ скоринга на обеих схемах: расхождение двух слоёв на БАЗОВОЙ
    #: схеме — это плохая подсказка, по которой пакет строился, а на итоговой —
    #: плохая линейка одного из них.
    scorer_before: Dict[str, Any] = field(default_factory=dict)
    scorer_after: Dict[str, Any] = field(default_factory=dict)
    checks_before: Dict[str, Check] = field(default_factory=dict)
    checks_after: Dict[str, Check] = field(default_factory=dict)
    attribution: Dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    llm_calls: int = 0
    #: вызовы планирования с размером входа (live — с транспорта, replay — по
    #: шаблонам ядра): см. `rendered_improve_prompt_rows`.
    prompt_calls: List[Dict[str, Any]] = field(default_factory=list)
    #: кто прислал пакет операций: живой захват или автор кейса. Метрики
    #: улучшения по рукописным пакетам мерят аплайер и разметку, а не модель, и
    #: обязано быть напечатано рядом с числом (`provenance.origin_verdict`).
    answer_source: str = provenance.UNKNOWN_SOURCE
    #: ключ `answer_source` в фикстуре записан явно (или происхождение задал сам
    #: режим — в live ответ всегда живой), а не выведено из `quality`/`source`;
    #: тот же порядок, что у `targets_declared`.
    answer_source_declared: bool = False
    xml_after: str = ""
    #: схема, поверх которой работал пакет: без неё `dump_schemes` не отдаёт
    #: пар before/after, и «что именно изменил пакет» читается только повторным
    #: прогоном.
    base_xml: str = ""

    #: инварианты, дефект которых описывает претензия фикстуры. В live фикстура —
    #: только слот прогона: текст жалобится на дефект *своей* базовой схемы, а
    #: пакет паркуют к живой, где того дефекта может не быть. Прогон #55 на этом
    #: попался: `loops_have_a_guard` и `no_blind_rework` прошли 1/1, а претензия
    #: пары про цикл всё равно требовала развилку, модель её вставляла и ловила
    #: `gateway_split_join` в колонке «сломано пакетом» — `score_delta` мерил
    #: несоответствие текста и схемы, а не качество пакета.
    targets: List[str] = field(default_factory=list)
    #: ключ `targets` в фикстуре был: пустой список — осознанная разметка
    #: «претензия просит улучшение, а не снимает нарушение», а отсутствие ключа —
    #: незафиксированный факт, который попадает в `unmeasured`.
    targets_declared: bool = False
    #: претензия не про эту базовую схему: кейс не получил вызова модели и не
    #: входит ни в одну метрику качества (пакета, которого не было, не бывает).
    off_target: bool = False
    off_target_note: str = ""

    @property
    def key(self) -> str:
        return f"{self.scenario}/{self.fixture}"

    @property
    def score_delta(self) -> Optional[float]:
        if self.score_before is None or self.score_after is None:
            return None
        return self.score_after - self.score_before

    @property
    def pass_after(self) -> Optional[bool]:
        """Итоговая схема корректна (гейт тот же, что у `GenCase.scenario_pass`).

        Бизнес-слой сюда не входит по той же причине, но он не теряется:
        `repaired_share` ниже считает провалы всех применимых инвариантов,
        включая бизнес, и именно там видно, убрал ли пакет узкое место.
        """
        if not self.checks_after:
            return None
        return all(c.ok for c in invariants.deciding_checks(self.checks_after))

    @property
    def gate_share(self) -> Optional[float]:
        """Доля пройденных гейт-проверок улучшенной схемы.

        Парная к `GenCase.gate_share` и введена вместо `improve/pass@1`: тот
        конъюнкционный числовой ряд был 0.056 в живом прогоне именно потому,
        что мерил и починку, и генерацию одним битом. Здесь же пакет отвечает
        за свою долю, а «чинить было нечего / было нечего чинить» остаётся у
        `repaired_share`.
        """
        checks = invariants.deciding_checks(self.checks_after)
        if not checks:
            return None
        return float(sum(1 for c in checks if c.ok)) / len(checks)

    @property
    def base_gate_share(self) -> Optional[float]:
        """Доля пройденных гейт-проверок БАЗОВОЙ схемы — знаменатель выше.

        Без неё `gate_share` улучшенной схемы неотличим от «такая база была»:
        0.8 после пакета на базе 0.5 и 0.8 после пакета на базе 0.8 — это разные
        результаты, и дельта этих двух чисел есть вклад пакета в нотацию.
        Парность важна и как условие: без состоявшегося пакета (`checks_after`
        пуст) сравнивать нечего, и число о базе стало бы метрикой по кейсу без
        данных — как если бы `improve/op_acceptance` считалась по отказу.
        """
        if not self.checks_after:
            return None
        checks = invariants.deciding_checks(self.checks_before)
        if not checks:
            return None
        return float(sum(1 for c in checks if c.ok)) / len(checks)

    @property
    def repaired_share(self) -> Optional[float]:
        """Доля дефектов БАЗОВОЙ схемы, которые пакет убрал.

        `pass_after` мерит итоговую схему целиком и наследует провалы генерации:
        для него «улучшение не сработало» и «улучшать было нечего — схема сломана
        выше» дают одно число. Эта метрика отвечает только за зону ответственности
        пакета и возвращает None, когда чинить было нечего (не раздувает
        выборку).
        """
        if not self.checks_before or not self.checks_after:
            return None
        before = [c for c in invariants.applicable_checks(self.checks_before)
                  if not c.ok]
        if not before:
            return None
        still_bad = {c.name for c in invariants.applicable_checks(self.checks_after)
                     if not c.ok}
        return float(sum(1 for c in before if c.name not in still_bad)) / len(before)

    @property
    def business_repaired_share(self) -> Optional[float]:
        """Доля бизнес-узких мест базовой схемы, которые пакет убрал.

        Отделена от `repaired_share`, потому что смешивать их нельзя: инварианты
        нотации — про валидность XML, и пакет, честно разведший потоки, даёт по
        ним 1.0, не тронув ни одного узкого места процесса. Это и есть вопрос
        задачи — видит ли контур улучшение бизнеса, а не только валидность XML.
        None, когда бизнес-дефектов в базе не было (не раздувает выборку).
        """
        if not self.checks_before or not self.checks_after:
            return None
        before = invariants.business_smells(self.checks_before)
        if not before:
            return None
        still_bad = {c.name for c in invariants.business_smells(self.checks_after)}
        return float(sum(1 for c in before if c.name not in still_bad)) / len(before)

    @property
    def drift_before(self) -> Dict[str, Dict[str, Any]]:
        """Где два слоя не согласны на БАЗОВОЙ схеме — то есть по какому правилу
        пакету дали плохую подсказку.

        Контур улучшения читает рекомендации скоринга, а дефекты после пакета
        считает оракул: если на одной и той же схеме они разошлись, `op_acceptance`
        мерит качество модели по совету, который аплайер в principle не мог
        исполнить, и наоборот."""
        if not self.checks_before or not self.scorer_before:
            return {}
        return invariants.business_agreement(self.checks_before, self.scorer_before)

    @property
    def drift_after(self) -> Dict[str, Dict[str, Any]]:
        """То же по итоговой схеме."""
        if not self.checks_after or not self.scorer_after:
            return {}
        return invariants.business_agreement(self.checks_after, self.scorer_after)

    @property
    def drift_lines(self) -> List[str]:
        """Строки расхождений: сначала база (там они породили пакет), потом итог."""
        out: List[str] = []
        for stage, drift in (("база", self.drift_before),
                             ("итог", self.drift_after)):
            rows = {rule: drift[rule]
                    for rule in invariants.business_disagreements(drift)}
            out += [f"[{stage}] {line}"
                    for line in invariants.format_business_agreement(rows)]
        return out

    @property
    def repeat_rejections(self) -> Optional[int]:
        """Сколько отказов корректирующий повтор вернул дословно.

        Считается по строкам `skipped`, а не по полю отчёта: `duplicate` ставит
        сам контур на обеих ветках, и вторая копия числа разошлась бы с ними.
        """
        if self.retry_attempted is not True:
            return None
        return sum(1 for s in self.skipped if s.get("duplicate"))

    def retry_phrase(self) -> str:
        """Повтор в одну строку человеческим языком: «был/не был» — про вызов,
        а не про то, остались ли в отчёте его строки."""
        if self.retry_attempted is None:
            return "повтор не померен"
        if not self.retry_attempted:
            return "повтор не был вызван"
        closed = "?" if self.retry_closed is None else str(self.retry_closed)
        dupes = self.repeat_rejections
        return (f"повтор закрыл {closed} отказов"
                + ("" if not dupes else f", {dupes} вернул дословно"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario, "fixture": self.fixture, "label": self.label,
            "quality": self.quality, "mode": self.mode, "repeat": self.repeat,
            "ok": self.ok, "error": self.error,
            "applied": len(self.applied), "skipped": len(self.skipped),
            "op_acceptance": _op_acceptance(self), "noop_share": _noop_share(self),
            "retry_attempted": self.retry_attempted,
            "retry_closed": self.retry_closed, "retry_gain": _retry_gain(self),
            "package_reverted": self.package_reverted,
            "rules_regressed": self.rules_regressed,
            "noop_rows": self.noop_rows,
            "truncated_operations": self.truncated_operations,
            "operations_packages": self.operations_packages,
            "repeat_rejections": self.repeat_rejections,
            "repeat_rejection_share": _repeat_rejection_share(self),
            "defects_introduced": _defects_introduced(self),
            "unmeasured": self.unmeasured,
            "score_before": self.score_before, "score_after": self.score_after,
            "score_delta": self.score_delta,
            "skipped_details": self.skipped, "applied_details": self.applied,
            "repair_notes": self.repair_notes,
            "attribution": self.attribution,
            "pass_after": self.pass_after,
            "repaired_share": self.repaired_share,
            "business_repaired_share": self.business_repaired_share,
            "scorer_vs_oracle": self.drift_lines,
            "summary_after": invariants.summarize(self.checks_after)
            if self.checks_after else {},
            "checks_after": {k: v.as_dict() for k, v in self.checks_after.items()},
            # Базовая схема рядом с итоговой: без неё прогон не отвечает на
            # вопрос, унаследован провал от генерации или его принёс пакет.
            "summary_before": invariants.summarize(self.checks_before)
            if self.checks_before else {},
            "checks_before": {k: v.as_dict() for k, v in self.checks_before.items()},
            "latency_ms": round(self.latency_ms, 1), "llm_calls": self.llm_calls,
            "prompt_calls": list(self.prompt_calls),
            "prompt_totals": prompt_totals(self.prompt_calls),
            "answer_source": self.answer_source,
            "answer_source_declared": self.answer_source_declared,
            "targets": self.targets, "off_target": self.off_target,
            "off_target_note": self.off_target_note,
        }


# ---------------------------------------------------------------------------
# размер промпта: качество ИИ-контура ограничено бюджетом контекста
# ---------------------------------------------------------------------------

#: Символов на токен — порядок величины, а не счётчик провайдера. Число нужно,
#: чтобы сравнение «стало дороже в 1.8 раза» читалось и в replay, где токенов
#: никто не отдаёт; в live рядом лежит настоящий `usage` из журнала обращения
#: (`core.llm_client.fill_call_trace`), и расхождение с этой оценкой — про
#: оценку, а не про промпт.
CHARS_PER_TOKEN = 3.0

#: Стадии, на которые распадается вход одного вызова. Имена совпадают с узлами
#: трейса генератора: «состав» и «маршрут» — две стадии `_extract_structure`,
#: «одношаговый» — путь отката, «переспрос» — `_retry_structure`.
PROMPT_STAGES = ("состав", "маршрут", "одношаговый", "переспрос",
                 "вопрос о принадлежности", "улучшение", "повтор улучшения",
                 "иное")

_PROMPT_TEMPLATES: Optional[Dict[str, str]] = None


def prompt_templates() -> Dict[str, str]:
    """Тексты системных блоков стадий, прочитанные из ядра.

    Харнесс не заводит свою копию промпта: ему нужен размер и тождество блока,
    а они берутся из того же объекта, который уходит в модель. Ключ — стадия,
    значение — дословный текст.
    """
    global _PROMPT_TEMPLATES
    if _PROMPT_TEMPLATES is None:
        gen = _module("core.bpmn_generator")
        imp = _module("core.llm_improve")
        _PROMPT_TEMPLATES = {
            "состав": str(getattr(gen, "_ROSTER_SYSTEM_PROMPT", "") or ""),
            "маршрут": str(getattr(gen, "_FLOW_SYSTEM_PROMPT", "") or ""),
            "одношаговый": str(getattr(gen, "_SYSTEM_PROMPT", "") or ""),
            "переспрос": str(getattr(gen, "_RETRY_TEMPLATE", "") or ""),
            "вопрос о принадлежности":
                str(getattr(gen, "_OWNERSHIP_SYSTEM_PROMPT", "") or ""),
            "улучшение": str(getattr(imp, "_SYSTEM_PROMPT", "") or ""),
            "повтор улучшения": str(getattr(imp, "_RETRY_TEMPLATE", "") or ""),
        }
    return _PROMPT_TEMPLATES


def estimate_prompt_tokens(chars: Optional[float]) -> Optional[float]:
    """Токены из символов: None остаётся None («не померено»), ноль — это ноль."""
    if chars is None:
        return None
    return round(float(chars) / CHARS_PER_TOKEN, 1)


def _prompt_row(stage: str, chars: int, observed: bool,
                note: str = "") -> Dict[str, Any]:
    """Один вызов: стадия, символы входа, оценка токенов и как число получено.

    `observed=False` читается как «вызова не было, размер посчитан по шаблонам
    ядра и данным кейса» — в replay иначе неотличимо от записанного обращения.
    """
    row: Dict[str, Any] = {"stage": stage, "chars": int(max(0, chars)),
                           "tokens_est": estimate_prompt_tokens(chars),
                           "observed": bool(observed)}
    if note:
        row["note"] = note
    return row


def prompt_stage(system: str, user: str) -> str:
    """Стадия вызова по его же тексту: ядро не отдаёт имя стадии наружу.

    Разбор по тождеству системного блока, а не по подстроке: `_FLOW_SYSTEM_PROMPT
    = _SYSTEM_PROMPT + хвост`, и «содержит» спутало бы одношаговый путь с
    маршрутом. Переспрос опознаётся по началу пользовательской части — тот же
    системный блок, что у стадии, поэтому по одному ему стадию не выбрать.
    """
    tpl = prompt_templates()
    sys_text = str(system or "")
    user_text = str(user or "")
    retry_head = (tpl.get("переспрос") or "")[:40]
    improve_retry_head = (tpl.get("повтор улучшения") or "")[:40]
    if sys_text == tpl.get("состав"):
        return "состав"
    if sys_text == tpl.get("вопрос о принадлежности"):
        return "вопрос о принадлежности"
    if sys_text == tpl.get("улучшение"):
        return ("повтор улучшения" if improve_retry_head
                and user_text.startswith(improve_retry_head) else "улучшение")
    if sys_text in (tpl.get("маршрут"), tpl.get("одношаговый")):
        return ("переспрос" if retry_head and user_text.startswith(retry_head)
                else ("маршрут" if sys_text == tpl.get("маршрут")
                      else "одношаговый"))
    return "иное"


def _rendered_chars(template: str, values: Mapping[str, str]) -> int:
    """Длина шаблона после подстановки — без сборки самого текста.

    `str.replace` ядро вызывает на тех же плейсхолдерах; считать длину дословно
    склеенной строки значило бы второго источника истины о промпте, который
    расходится с ядром при первой же правке формулировки.
    """
    total = len(template)
    for key, value in values.items():
        marker = "{" + key + "}"
        count = template.count(marker)
        if not count:
            continue
        total += (len(value) - len(marker)) * count
    return total


def observed_prompt_rows(messages: Sequence[Mapping[str, str]]) -> List[Dict[str, Any]]:
    """Строка про вызов, снятая с настоящего обращения (live)."""
    msgs = list(messages or [])
    system = str(msgs[0].get("content") or "") if msgs else ""
    user = " ".join(str(m.get("content") or "") for m in msgs[1:])
    chars = sum(len(str(m.get("content") or "")) for m in msgs)
    return [_prompt_row(prompt_stage(system, user), chars, True)]


def rendered_prompt_rows(text: str, plan: Mapping[str, Any],
                         gaps: Sequence[str]) -> List[Dict[str, Any]]:
    """Вход каждой стадии генерации для этого кейса, посчитанный по ядру.

    В replay обращений к модели нет вовсе, а бюджет контекста обязан быть виден
    всё равно: именно он ограничивает качество контура (весь свод правил едет в
    каждом вызове). Поэтому сюда попадают ВСЕ стадии, которые ядро умеет звать,
    а не одна «выбранная»: путь — двухстадийный или откат в одношаговый — в
    replay не наблюдался, и `cost/prompt_two_stage_share` остаётся «н/д».
    Служебная склейка стадии маршрута (абзац про `participant` и `lane`) в сумму
    не входит: это сотни символов на десятки тысяч — погрешность диагностики,
    заводить ради неё копию текста ядра харнесс не имеет права.
    """
    tpl = prompt_templates()
    description = "Описание процесса:\n" + str(text or "")
    rows: List[Dict[str, Any]] = [
        _prompt_row("состав", len(tpl.get("состав") or "") + len(description),
                    False, "системный блок стадии + описание процесса"),
        _prompt_row("одношаговый",
                    len(tpl.get("одношаговый") or "") + len(description), False,
                    "путь отката: системный блок со сводом правил + описание"),
    ]
    skeleton = ""
    try:
        block = getattr(_module("core.bpmn_generator"), "_skeleton_block", None)
        if callable(block):
            skeleton = str(block(list((plan or {}).get("participants") or []),
                                 list((plan or {}).get("lanes") or [])))
    except Exception:  # noqa: BLE001 — размер не имеет права ронять прогон
        skeleton = ""
    rows.insert(1, _prompt_row(
        "маршрут", len(tpl.get("маршрут") or "") + len(skeleton) + len(description),
        False, "состав берётся из плана кейса, а не из ответа стадии состава"))
    if gaps:
        # Переспрос идёт с тем же системным блоком, что и стадия маршрута
        # (состав под утверждённым каркасом знает `_FLOW_SYSTEM_PROMPT`), и без
        # него строка врала бы в минус на размер свода правил.
        rows.append(_prompt_row(
            "переспрос",
            len(tpl.get("маршрут") or "") + _rendered_chars(
                tpl.get("переспрос") or "", {
                    "gaps": "\n".join(f"- {g}" for g in gaps),
                    "text": str(text or ""),
                    "plan": json.dumps(dict(plan or {}), ensure_ascii=False,
                                       default=str)}),
            False, "системный блок стадии + тело шаблона с планом и нарушениями"))
    return rows


def rendered_improve_prompt_rows(xml: str, user_prompt: str) -> List[Dict[str, Any]]:
    """Вход планировщика улучшения для этой схемы — из тех же кусков, что и продукт.

    Здесь харнесс ничего не дублирует: инвентарь, блок ограничений и разбор узких
    мест отдаёт ядро теми же функциями, которыми собирает запрос. Блок ЛУЧШИЕ
    ПРАКТИКИ не учитывается: он требует индекса корпуса и модели эмбеддингов, а
    replay обязан оставаться дешёвым и детерминированным проходом по фикстурам —
    об этом говорит пометка в строке.
    """
    if not xml:
        return []
    try:
        imp = _module("core.llm_improve")
        edits = _module("core.bpmn_edits")
        inventory = edits.build_inventory(xml)
        content = str(imp._USER_TEMPLATE).format(
            inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
            limits_block=str(imp._limits_block(inventory)),
            findings_block=str(imp._findings_block(xml)),
            practices_block="",
            user_prompt=str(user_prompt or ""))
    except Exception:  # noqa: BLE001 — диагностика стоимости не роняет кейс
        return []
    chars = len(str(getattr(imp, "_SYSTEM_PROMPT", "") or "")) + len(content)
    return [_prompt_row("улучшение", chars, False,
                        "без блока практик: его RAG-подбор в replay не собирается")]


def prompt_totals(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Символы и оценка токенов по списку вызовов: {} на пустом списке.

    Пустой список даёт пустой словарь, а не нули: «не померено» и «ноль
    символов» в отчёте — разные утверждения.
    """
    measured = [r for r in rows or [] if r.get("chars") is not None]
    if not measured:
        return {}
    chars = sum(int(r["chars"]) for r in measured)
    return {"calls": float(len(measured)), "chars": float(chars),
            "tokens_est": round(chars / CHARS_PER_TOKEN, 1),
            "observed": all(bool(r.get("observed")) for r in measured)}


def _prompt_case_rows(case: Any) -> List[Dict[str, Any]]:
    return list(getattr(case, "prompt_calls", None) or [])


def _prompt_chars_per_call(case: Any) -> Optional[float]:
    totals = prompt_totals(_prompt_case_rows(case))
    return None if not totals else totals["chars"] / totals["calls"]


def _prompt_chars_per_case(case: Any) -> Optional[float]:
    totals = prompt_totals(_prompt_case_rows(case))
    return None if not totals else totals["chars"]


def _prompt_tokens_per_call(case: Any) -> Optional[float]:
    totals = prompt_totals(_prompt_case_rows(case))
    return None if not totals else totals["tokens_est"] / totals["calls"]


def _prompt_tokens_per_case(case: Any) -> Optional[float]:
    totals = prompt_totals(_prompt_case_rows(case))
    return None if not totals else totals["tokens_est"]


def stage_path(case: GenCase) -> Optional[str]:
    """Каким путём собирался план: «двухстадийный» или «одношаговый».

    Читается из трейса живого прогона — генератор пишет туда и «состав утверждён
    отдельным вызовом», и список деградаций. None, когда трейса стадии нет
    (replay идёт по записанному плану и путь не наблюдает): метрика печатается
    как «н/д», а не как «ни один кейс не двухстадийный».
    """
    for node in case.trace or []:
        if str(node.get("node") or "") != "первый ответ модели":
            continue
        if (node.get("деградации") or []) and str(node.get("состав") or "") \
                != "утверждён отдельным вызовом":
            return "одношаговый"
        return ("двухстадийный"
                if str(node.get("состав") or "") == "утверждён отдельным вызовом"
                else "одношаговый")
    return None


def _two_stage_share(case: GenCase) -> Optional[float]:
    path = stage_path(case)
    return None if path is None else (1.0 if path == "двухстадийный" else 0.0)


# ---------------------------------------------------------------------------
# прогоны
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def llm_call_counter() -> Iterator[Dict[str, Any]]:
    """Счётчик обращений к транспорту LLM — только для live-режима.

    Число ретраев на задачу измеряется на уровне транспорта
    (`core.llm_client._complete`), а не переписыванием ядра: харнесс наблюдает
    существующий код, а не подменяет его логику. Там же, на сообщении, которое
    уже собрано, снимается и его размер: ядро не отдаёт стадию наружу, а
    запрашиваемый текст харнессу видеть можно — он его не меняет.
    """
    from core import llm_client

    original = llm_client._complete
    state: Dict[str, Any] = {"calls": 0, "prompt_calls": []}

    def counting(messages: Sequence[Mapping[str, str]], temperature: float,
                 max_tokens: Optional[int]) -> str:
        state["calls"] += 1
        state["prompt_calls"].extend(observed_prompt_rows(messages))
        return original(messages, temperature, max_tokens)

    llm_client._complete = counting
    try:
        yield state
    finally:
        llm_client._complete = original


def _plan_gaps(plan: Mapping[str, Any], text: str) -> List[str]:
    """Нарушения плана до починки — ответ того же `plan_gaps`, что и контур.

    Служит для разбора отказов: без них нельзя отличить «модель не назвала
    участника» от «назвала, а починка вынесла»."""
    fn = _resolve("core.bpmn_generator", "plan_gaps", "check_plan",
                  "validate_plan")
    if fn is None:
        return []
    try:
        return [str(g) for g in (fn(dict(plan), text) or [])]
    except Exception:  # noqa: BLE001 — диагностика не имеет ронять прогон
        return []


def lost_participant_gap(name: str) -> str:
    """Строка потери названного участника — та же, что печатает контур.

    Формулировку держит ядро (`core.bpmn_generator.lost_participant_gap`):
    копия в харнессе разошлась бы, а атрибуция узнаёт потерю именно по тексту,
    поэтому расхождение выглядело бы как «молчаливый» провал `expected_participants`.
    """
    fn = _resolve("core.bpmn_generator", "lost_participant_gap")
    if fn is None:
        raise HarnessError("в core/bpmn_generator нет строки потери участника "
                           "(lost_participant_gap)")
    return str(fn(name))


def _live_plan(text: str) -> Tuple[Dict[str, Any], List[str], str, List[str],
                                   List[Dict[str, Any]]]:
    """План, пометки починки, XML, нарушения плана и трейс узлов живого контура
    (тот же путь, что у /api/generate)."""
    cls = getattr(_module("core.bpmn_generator"), "BPMNGenerator", None)
    if cls is None:
        raise HarnessError("в core/bpmn_generator нет класса BPMNGenerator")
    result = cls().generate(text)
    if not isinstance(result, dict) or result.get("status") != "success":
        error = (result or {}).get("error") or "генератор вернул неизвестный ответ"
        raise _CaseFailure(error)
    return (result.get("structure") or {}, list(result.get("notes") or []),
            result.get("bpmn") or "", [str(g) for g in (result.get("gaps") or [])],
            [dict(t) for t in (result.get("trace") or [])])


def run_generation_case(scenario: Scenario, fixture: Optional[Mapping[str, Any]] = None,
                        mode: str = "replay", repeat: int = 0) -> GenCase:
    """Один кейс генерации: план → `repair_structure` → XML → инварианты + балл.

    Провал сцены (`GenerationError`, пустой план, падение аплайера) — значение
    метрики, а не падение харнесса: случай записывается с текстом причины.

    В live-режиме фикстура даёт только слот прогона: ответ модели берётся
    живой, поэтому и метка случая — «live», а не «эталон» из фикстуры. Иначе
    отчёт приписывал бы записи модели свойства записанного плана.
    """
    live_answer = mode == "live"
    case = GenCase(
        scenario=scenario.id,
        fixture=(fixture or {}).get("id", "live"),
        label=("живой ответ модели" if live_answer
               else (fixture or {}).get("label", "ответ живого контура")),
        quality="live" if live_answer else (fixture or {}).get("quality", "live"),
        mode=mode, repeat=repeat,
    )
    expectations = scenario.expectations()
    started = time.perf_counter()
    try:
        if mode == "live":
            with llm_call_counter() as counter:
                structure, notes, xml, gaps, trace = _live_plan(scenario.text)
            case.trace = trace
            case.llm_calls = counter["calls"]
            case.prompt_calls = list(counter.get("prompt_calls") or [])
        else:
            if not fixture or "plan" not in fixture:
                raise _CaseFailure("у фикстуры нет поля plan")
            gaps = _plan_gaps(fixture["plan"], scenario.text)
            steps: List[Dict[str, Any]] = []
            lost: List[str] = []
            structure, notes = repair_structure(fixture["plan"], steps,
                                                text=scenario.text, lost=lost)
            # Потеря названного участника — решение контура, а не модели: в
            # продукте она попадает в `gaps` той же строкой, и replay обязан
            # мерить её там же, иначе `expected_participants` проваливался бы
            # без владельца.
            gaps = list(gaps) + [lost_participant_gap(name) for name in lost]
            # Трейс фикстуры честен ровно наполовину: кто прислал план — модель
            # или автор фикстуры — харнесс не знает, поэтому владельцем отсутствия
            # считается сама фикстура (атрибуция решает это по пустому трейсу).
            case.trace = [{"node": "починка структуры", "steps": steps}]
            xml = generate_xml(structure)
            # Размер входа считается по шаблонам ядра: обращений к модели в replay
            # нет, но бюджет контекста — ровно то, что ограничивает качество
            # контура, и CI обязан видеть его движение до живого прогона.
            case.prompt_calls = rendered_prompt_rows(scenario.text,
                                                     fixture["plan"], gaps)
        case.structure, case.notes, case.xml = structure, notes, xml
        case.gaps = gaps
        case.measure(expectations)
    except HarnessError:
        raise
    except _CaseFailure as e:
        case.error = str(e)
    except Exception as e:  # noqa: BLE001 — план недоверенный: не роняем прогон
        # Сюда попадает и GenerationError: класс и сигнатура могут измениться,
        # а смысл «сцена не собралась» остаётся.
        case.error = f"{type(e).__name__}: {e}"
    case.latency_ms = (time.perf_counter() - started) * 1000
    return case


def _apply_package(xml: str, operations: List[Dict[str, Any]], stage: str,
                   unmeasured: List[str]) -> Tuple[str, Dict[str, Any]]:
    """Один раунд применения — продуктовым гарантом, а не его локальной копией.

    `bpmn_edits.apply_and_guarantee` решает судьбу пакета целиком: применение →
    починка до состояния покоя → снятие созданных и осиротевших узлов → отказ
    всему пакету → сверка отчёта с вернувшимся XML. Харнесс держал свою, более
    короткую версию этого пути, и replay мерил не тот контур, что проходит
    пользователь: откат пакета по циклу без выхода возвращал базовую схему с
    полным `applied`, и отчёт рапортовал 0.67 принятия на схеме, байт в байт
    равной базе.

    Имена резолвятся на лету (аплайер и оркестратор меняются параллельно).
    Пропажа самого гаранта — «мерить нечем» (случай записывается несобранным),
    пропажа предиката или параметра сигнатуры — неполный замер: он ложится в
    `unmeasured` и снимает соответствующий факт, но не роняет прогон.
    """
    seam = _resolve("core.bpmn_edits", "apply_and_guarantee")
    if seam is None:
        raise _CaseFailure("в core/bpmn_edits нет apply_and_guarantee — контур "
                           "применения мерить нечем")
    names = _signature_names(seam)
    kwargs: Dict[str, Any] = {}
    if "reject" in names:
        factory = _resolve("core.llm_improve", "_cycle_reject", "cycle_reject")
        if factory is None:
            unmeasured.append("нет core.llm_improve._cycle_reject — откат пакета "
                              "по незащищённому циклу не игрался")
        else:
            kwargs["reject"] = factory(xml)
    else:
        unmeasured.append("у apply_and_guarantee нет параметра reject — отказ "
                          "всему пакету не игрался")
    if "prune" in names:
        prune = _resolve("core.llm_improve", "_prune_stranded", "prune_stranded")
        if prune is None:
            unmeasured.append("нет core.llm_improve._prune_stranded — снятие "
                              "узлов вне маршрута после починки не игралось")
        else:
            kwargs["prune"] = prune
    else:
        unmeasured.append("у apply_and_guarantee нет параметра prune — частичный "
                          "откат после починки не игрался")
    xml_after, report = seam(xml, operations, **kwargs)
    report = report or {}
    if "reject" not in kwargs:
        # Факт об откате не собран: пусто означало бы «гарант смолчал, значит
        # пакет хороший», и метрика приняла бы неоткатанный пакет за удачный.
        report["reverted"] = None
    for row in report.get("applied") or []:
        row.setdefault("stage", stage)
    for row in report.get("skipped") or []:
        # `setdefault`, а не присваивание: гарант уже мог подписать строку
        # стадией `repair`, и терять «отказано после починки» нельзя.
        row.setdefault("stage", stage)
        row.setdefault("reapplied", False)
    report.setdefault("applied", [])
    report.setdefault("skipped", [])
    return xml_after or "", report


def _plan_limit(unmeasured: List[str]) -> Optional[int]:
    """Лимит операций плана — тот же, что режет ответ модели в продукте: без
    него replay проглатывал план целиком, и `improve/plan_truncated_share`
    мерил ничего."""
    limit = getattr(_module("core.llm_improve"), "MAX_OPERATIONS", None)
    if not isinstance(limit, int) or limit <= 0:
        unmeasured.append("у core.llm_improve нет MAX_OPERATIONS — срез плана "
                          "лимитом не игрался")
        return None
    return limit


def _op_key(unmeasured: List[str]) -> Optional[Callable[[Mapping[str, Any]], Any]]:
    """«Операция и элемент, который она правит» — тем же ключом, что и
    оркестратор: по нему повтор узнаёт правку, проведённую в исправленном виде."""
    fn = _resolve("core.llm_improve", "_op_key", "op_key")
    if fn is None:
        unmeasured.append("нет core.llm_improve._op_key — «повтор добил ту же "
                          "правка» не сверялся")
    return fn


def _rules_regressed_fn(unmeasured: List[str]
                        ) -> Optional[Callable[[str, str], Dict[str, str]]]:
    fn = _resolve("core.llm_improve", "_rules_regressed", "rules_regressed")
    if fn is None:
        unmeasured.append("нет core.llm_improve._rules_regressed — регресс "
                          "правил скоринга не измерялся")
    return fn


def _repeat_fingerprint(entry: Mapping[str, Any]) -> tuple:
    """Дословно тот же отказ: все поля идентичности плюс причина.

    Зеркалит локальный `_fingerprint` из `_retry_skipped` — тот не вынесен в
    модуль, а две копии смысла расходятся молча. Два `connect` с одним
    источником и разными целями — два дефекта, а не одно «то же», поэтому
    нагрузка правки в отпечаток входит целиком.
    """
    return (str(entry.get("op") or ""),
            *(str(entry.get(field) or "") for field in
              ("id", "source", "target", "flow")),
            str(entry.get("reason") or ""))


def _merge_notes(groups: List[List[str]],
                 unmeasured: List[str]) -> List[str]:
    """Пометки по раундам — тем же `bpmn_edits.merge_notes`, что и оркестратор:
    неидемпотентную правку (понижение шлюза до задачи) второй прогон починки уже
    не вернёт, и терять факт подмены нельзя.

    Своей копии здесь нет: без продуктовой функции пометки второго раунда
    считаются потерянными для замера, и случай обязан сказать об этом.
    """
    merge = _resolve("core.bpmn_edits", "merge_notes")
    if merge is None:
        unmeasured.append("нет core.bpmn_edits.merge_notes — пометки второго "
                          "раунда не слиты с первым")
        return list(groups[0]) if groups else []
    return list(merge(*groups))


def _replay_improve(base_xml: str,
                    fixture: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Пакет фикстуры сквозь продуктовый гарант, двумя раундами как в
    оркестраторе: план → применение → корректирующий повтор → применение.

    Отчёт собирается той же формы, что отдаёт `improve_diagram`, — дальше оба
    режима читают факты одним кодом. Единственный вывод, который позволяет себе
    replay: `retry_attempted`. Фикстура — ответ модели, а не отчёт контура, и
    «повтор был» здесь значит «контур позвал бы повтор, и фикстура знает, что
    ему ответили»: те же условия запуска, что у оркестратора (незакрытые отказы,
    узлы вне маршрута, регресс правил) и непустой план повтора.
    """
    unmeasured: List[str] = []
    operations = [dict(op) for op in (fixture.get("operations") or [])]
    if not operations:
        raise _CaseFailure("в фикстуре нет операций")
    limit = _plan_limit(unmeasured)
    retry_ops = [dict(op) for op in (fixture.get("retry_operations") or [])]
    # None, а не 0: без лимита срез плана не игрался, и «не обрезан» было бы
    # выводом из замера, который ничего не мерил.
    truncated: Optional[int] = None
    if limit is not None:
        truncated = max(0, len(operations) - limit) + max(0, len(retry_ops) - limit)
        operations, retry_ops = operations[:limit], retry_ops[:limit]
    xml_after, first = _apply_package(base_xml, operations, "plan", unmeasured)
    applied = [dict(a) for a in first["applied"]]
    skipped = [dict(s) for s in first["skipped"]]
    notes = list(first.get("notes") or [])
    noop_rows = _as_int_or_none(first.get("noop_rows"))
    reverted: Optional[str] = _reverted_or_none(first)
    regressed_fn = _rules_regressed_fn(unmeasured)

    open_skips = [s for s in skipped if not s.get("reapplied")]
    triggered = bool(open_skips) or bool(unrouted_notes(notes)) or bool(
        regressed_fn(base_xml, xml_after) if regressed_fn else {})
    retried = bool(retry_ops) and triggered
    # «Сколько отказов закрыл повтор» — факт сверки идентичностей; без
    # продуктового `_op_key` её вести нечем, и 0 здесь означал бы «повтор
    # бесполезен» вместо «мы не смотрели».
    closed: Optional[int] = 0
    if retried:
        key_of = _op_key(unmeasured)
        if key_of is None:
            closed = None
        # Повтор стартует от схемы первого раунда — от той, что отдал бы
        # пользователь, а не от сырого применения.
        xml_retried, second = _apply_package(xml_after, retry_ops, "retry",
                                             unmeasured)
        redo = {key_of(a) for a in second["applied"]} if key_of else set()
        for row in skipped:
            if key_of is not None and key_of(row)[1] and key_of(row) in redo:
                row["reapplied"] = True
        first_round = {_repeat_fingerprint(s) for s in skipped}
        for row in second["skipped"]:
            skipped.append(dict(row, stage="retry", reapplied=False,
                                duplicate=_repeat_fingerprint(row) in first_round))
        applied += [dict(a) for a in second["applied"]]
        notes = _merge_notes([notes, list(second.get("notes") or [])], unmeasured)
        noop_rows = _sum_or_none(noop_rows, _as_int_or_none(second.get("noop_rows")))
        # Первая причина важнее: пакет откатан ещё до повтора, и «пусто при
        # откате» относится к обоим раундам.
        reverted = _merge_reverted(reverted, _reverted_or_none(second))
        if closed is not None:
            closed = sum(1 for s in skipped if s.get("reapplied"))
        xml_after = xml_retried

    regressed: Optional[Dict[str, str]] = None
    if regressed_fn is not None:
        regressed = {} if reverted else dict(regressed_fn(base_xml, xml_after))
    return xml_after, {
        "applied": applied,
        "skipped": skipped,
        "repair_notes": notes,
        "retry_attempted": retried,
        "retry_closed": closed,
        "package_reverted": reverted,
        "rules_regressed": regressed,
        "noop_rows": noop_rows,
        "truncated_operations": truncated,
        # сам пакет, отданный гаранту: см. `ImproveCase.operations_packages`.
        "operations_packages": [operations] + ([retry_ops] if retried else []),
        "unmeasured": unmeasured,
    }


def _reverted_or_none(report: Mapping[str, Any]) -> Optional[str]:
    return None if report.get("reverted") is None else str(report["reverted"])


def _merge_reverted(first: Optional[str],
                    second: Optional[str]) -> Optional[str]:
    """Причина откатанного пакета по двум раундам; None — когда гарант одного из
    раундов не игрался и факт не собран."""
    if first is None or second is None:
        return None
    return first or second


def _sum_or_none(first: Optional[int], second: Optional[int]) -> Optional[int]:
    if first is None or second is None:
        return None
    return first + second


def _absorb_improve_report(case: ImproveCase, report: Mapping[str, Any]) -> None:
    """Факты отчёта — в поля случая. Ни одного вывода: `retry_attempted`,
    `retry_closed`, `package_reverted`, `rules_regressed`, `noop_rows` и
    `truncated_operations` приносит контур (живой или проигранный из фикстуры),
    а не собранный из стадий отказов догадка харнесса. Отсутствующий факт
    остаётся None: метрика по нему не считается, вместо этого прогон честно
    теряет выборку."""
    report = report or {}
    case.applied = [dict(a) for a in (report.get("applied") or [])]
    case.skipped = [dict(s) for s in (report.get("skipped") or [])]
    case.repair_notes = list(report.get("repair_notes") or report.get("notes")
                             or [])
    attempted = report.get("retry_attempted")
    case.retry_attempted = None if attempted is None else bool(attempted)
    case.retry_closed = _as_int_or_none(report.get("retry_closed"))
    reverted = report.get("package_reverted")
    case.package_reverted = None if reverted is None else str(reverted)
    regressed = report.get("rules_regressed")
    case.rules_regressed = None if regressed is None else dict(regressed)
    case.noop_rows = _as_int_or_none(report.get("noop_rows"))
    case.truncated_operations = _as_int_or_none(report.get("truncated_operations"))
    case.unmeasured = [str(u) for u in (report.get("unmeasured") or [])]


def _as_int_or_none(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _advice_fixtures(scenario: Scenario, case: GenCase) -> List[Dict[str, Any]]:
    """Один «претензия по факту» кейс на живую схему.

    Фикстурная претензия в live обязана совпадать с дефектом схемы (см.
    `_mark_off_target`), и с узкими `targets` это случается редко: контур
    улучшения оставался бы без живой выборки вовсе. Здесь же требование к модели
    строится из того, что в схеме нашли независимый оракул и скоринг, — ровно так
    же, как это делает продукт: оркестратор сам подставляет секцию «УЗКИЕ МЕСТА ПО
    СКОРИНГУ» (`core/llm_improve.py:1013`), поэтому в промпте нет ни id, ни
    названий участников, ни подсказки ответа.

    Схема без единого нарушения получает кейс с пустыми `targets`: это
    осознанная разметка «просят улучшить, а не снять дефект», и она меряется
    дельтой балла и регрессиями правил, но не долей ремонтов.
    """
    if not case.ok or not case.xml:
        return []
    failing = [c.name for c in invariants.applicable_checks(case.checks_xml)
               if not c.ok]
    advice = (score_xml(case.xml).get("recommendations_by_rule") or {})
    if not failing and not advice:
        return []
    return [{"id": f"{scenario.id}.advice", "kind": "improve",
             "scenario": scenario.id,
             "label": "претензия по факту схемы: что нашлось, то и чиним",
             "quality": "advice", "operations": [], "targets": failing,
             # Пакет в этом кейсе отвечает модель, а не автор фикстуры: фикстуры
             # как файла нет, её собирает харнесс из нарушений живой схемы.
             "answer_source": provenance.MODEL_CAPTURE,
             "prompt": ADVICE_PROMPT}]


def run_improvement_case(scenario: Scenario, fixture: Mapping[str, Any],
                         base: GenCase, mode: str = "replay",
                         repeat: int = 0) -> ImproveCase:
    """Один кейс улучшения поверх схемы `base`.

    В replay корректирующий раунд берётся из фикстуры (`retry_operations`):
    транспорт модели не дёргаем, но путь «план → гарант → повтор → гарант»
    проходит настоящий аплайер вместе с откатом пакета.
    """
    case = ImproveCase(scenario=scenario.id, fixture=fixture.get("id", "?"),
                       label=fixture.get("label", ""),
                       quality=fixture.get("quality", "unknown"),
                       mode=mode, repeat=repeat,
                       answer_source=(provenance.MODEL_CAPTURE if mode == "live"
                                      else provenance.answer_source(fixture)),
                       answer_source_declared=(
                           mode == "live"
                           or provenance.answer_source_declared(fixture)))
    expectations = scenario.expectations()
    if not base.ok or not base.xml:
        case.error = f"нет базовой схемы: {base.error or 'генерация не собралась'}"
        return case
    case.base_xml = base.xml
    started = time.perf_counter()
    try:
        case.checks_before = invariants.check_xml(base.xml, expectations)
        case.scorer_before = score_xml(base.xml)
        case.score_before = case.scorer_before.get("score")
        case.targets = [str(t) for t in (fixture.get("targets") or [])]
        case.targets_declared = "targets" in fixture
        _mark_off_target(case)
        pre_unmeasured = list(case.unmeasured)
        if case.off_target:
            # Ни одного вызова модели: спрашивать «почини это» у схемы, у которой
            # этого нет, — измерять несоответствие текста, а не контур.
            return case
        if mode == "live":
            # Свой счётчик: вложенный `llm_call_counter` внутри `_live_improve`
            # видит те же вызовы, но наружу отдаёт только число обращений, а
            # строки с размером входа остаются здесь.
            with _captured_packages([]) as packages:
                with llm_call_counter() as counter:
                    xml_after, report, case.llm_calls = _live_improve(
                        base.xml, fixture.get("prompt", "")
                        or scenario.improve_prompt)
            case.prompt_calls = list(counter.get("prompt_calls") or [])
            case.operations_packages = packages
        else:
            xml_after, report = _replay_improve(base.xml, fixture)
            case.prompt_calls = rendered_improve_prompt_rows(
                base.xml, fixture.get("prompt", "") or scenario.improve_prompt)
            case.operations_packages = list(
                report.get("operations_packages") or [])
        _absorb_improve_report(case, report)
        # Отчёт приносит СВОИ замечания о недомере; разметка фикстуры,
        # поставленная до вызова, от этого не исчезает.
        case.unmeasured = pre_unmeasured + list(case.unmeasured)
        case.xml_after = xml_after
        if case.xml_after:
            case.checks_after = invariants.check_xml(case.xml_after, expectations)
            case.attribution = attribution.attribute_improvement(
                case.checks_before, case.checks_after, case.applied,
                case.skipped, report=report)
            evaluation = score_xml(case.xml_after)
            case.score_after = evaluation.get("score")
            case.scorer_after = evaluation
        case.ok = bool(case.applied)
    except HarnessError:
        raise
    except _CaseFailure as e:
        case.error = str(e)
    except Exception as e:  # noqa: BLE001 — падение аплайера тоже исход сцены
        case.error = f"{type(e).__name__}: {e}"
    case.latency_ms = (time.perf_counter() - started) * 1000
    return case


@contextlib.contextmanager
def _captured_packages(sink: List[List[Dict[str, Any]]]):
    """Записать пакеты, которые контур отдаёт гаранту, не трогая продуктовый ответ.

    По паре `_before`/`_after` и строкам отказа неотличим разрыв, снятый стражом
    дыр маршрута, от дуги, вырезанной починкой, или от переноса между пулами, — а
    это разные очереди работы: живой прогон #73 оставил один реверт, и по
    артефактам нельзя было сказать, чья это правка. Оркестратор в отчёте пакета
    не показывает (пользовательский контракт), поэтому снимается пробою на входе
    гаранта.
    """
    edits = _module("core.bpmn_edits")
    if edits is None:
        yield sink
        return
    original = edits.apply_and_guarantee

    def _spy(xml_text: str, operations: Any, *args: Any, **kwargs: Any):
        sink.append([dict(op) for op in (operations or []) if isinstance(op, dict)])
        return original(xml_text, operations, *args, **kwargs)

    edits.apply_and_guarantee = _spy
    try:
        yield sink
    finally:
        edits.apply_and_guarantee = original


def _live_improve(xml: str,
                  prompt: str) -> Tuple[str, Dict[str, Any], int]:
    """Живое улучшение штатным оркестратором (асинхронным — прогоняем sync).

    Отчёт забирается целиком: факты о повторе, об откате пакета и о регрессе
    правил считает оркестратор, и выводить их здесь из стадий отказов значило бы
    измерять догадку харнесса вместо контура. Пакеты операций снимаются снаружи —
    см. `_captured_packages`.
    """
    import asyncio

    improve_module = _module("core.llm_improve")
    orchestrator = getattr(improve_module, "BPMNImprovementOrchestrator", None)
    if orchestrator is None:
        raise HarnessError("в core/llm_improve нет BPMNImprovementOrchestrator")
    if not prompt:
        raise _CaseFailure("у фикстуры нет prompt для live-улучшения")
    with llm_call_counter() as counter:
        _analysis, xml_after, report = asyncio.run(
            orchestrator().improve_diagram(xml_content=xml, user_prompt=prompt))
    return xml_after or "", dict(report or {}), counter["calls"]


# ---------------------------------------------------------------------------
# метрики прогона
# ---------------------------------------------------------------------------


def _check_value(case: GenCase, name: str) -> Optional[float]:
    check: Optional[Check] = case.checks_xml.get(name)
    if check is None or not check.applicable:
        return None
    return 1.0 if check.ok else 0.0


def etalon_business_smells() -> Dict[str, int]:
    """Сколько бизнес-узких мест держит ЭТАЛОН каждого сценария.

    Нужно потому, что бизнес-слой убран из гейта (`invariants.deciding_checks`):
    без опоры на эталон «средняя плотность узких мест» — число без знаменателя,
    у него нет потолка, к которому можно стремиться. Сравнение идёт с ответом
    той же задачи, а не с нулём, которого в реальном процессе не бывает.

    Считается тем же путём, что и кейс прогона (план фикстуры → `repair_structure`
    → `generate_xml` → `check_xml`), иначе сравнивались бы структура с XML.
    Сценарии, чей эталон не собрался, в словарь не попадают: метрика по ним
    вернёт None, а не выдуманную ноль-оценку.
    """
    out: Dict[str, int] = {}
    for fixture in load_fixtures(kinds=("plan",)):
        if fixture.get("quality") != "good" or "plan" not in fixture:
            continue
        try:
            scenario = scenario_by_id(fixture["scenario"])
            structure, _notes = repair_structure(fixture["plan"], [],
                                                 text=scenario.text)
            checks = invariants.check_xml(generate_xml(structure),
                                          scenario.expectations())
        except Exception:  # noqa: BLE001 — нечитаемый эталон снимает сверку, не прогон
            continue
        out[fixture["scenario"]] = len(invariants.business_smells(checks))
    return out


def _smell_density(case: GenCase) -> Optional[float]:
    """Плотность бизнес-узких мест схемы: проваленные бизнес-инварианты / все
    применимые бизнес-инварианты этой же схемы.

    Прежняя версия была АБСОЛЮТНЫМ числом узких мест, усреднённым по кейсам, —
    ровно тот изъян, который уже починили для `improve/retry_gain`: схема, у
    которой применима одна бизнес-проверка, и схема, у которой их пять, входили в
    среднее с одинаковым весом, а знаменателя метрика не видела вовсе. 2 узких
    места из 2 и 2 из 5 — это «процесс не выдерживает проверки» и «процесс почти
    чист», а на старом счёте это было одно и то же число 2.

    `None`, когда бизнес-проверок нет вовсе: 0.0 здесь читался бы как «узких мест
    нет», тогда как проверять было нечего.
    """
    if not case.ok:
        return None
    checks = [c for c in invariants.applicable_checks(case.checks_xml)
              if c.name in invariants.BUSINESS_INVARIANTS]
    return _share(len(case.business_smells), len(checks))


def _not_worse_than_etalon(case: GenCase,
                           etalon: Mapping[str, int]) -> Optional[float]:
    """Бизнес-узких мест в схеме не больше, чем в эталоне той же задачи.

    `None`, когда сравнивать не с чем: кейс не собрался или эталон сценария не
    построился. Ноль здесь — неправда: он означал бы «не хуже эталона», тогда как
    данных нет.
    """
    if not case.ok:
        return None
    base = etalon.get(case.scenario)
    if base is None:
        return None
    return 1.0 if len(case.business_smells) <= base else 0.0


def build_generation_suite(names: Sequence[str]) -> metrics.EvaluationSuite:
    """Метрики генерации: доля пройденных гейт-проверок и они же поштучно,
    правки починки, балл, задержка и ретраи.

    Бизнес-слой оракула дан двумя отдельными метриками (`business/smells`,
    `business/not_worse_than_etalon`) и в гейт-долю не входит; почему —
    `invariants.deciding_checks`. Сводной конъюнкции («схема без единого
    провала») в таблице нет: см. `GenCase.gate_share`.
    """
    suite = metrics.EvaluationSuite()
    etalon = etalon_business_smells()
    for name in names:
        suite.metric(f"pass@1/{name}", lambda case, n=name: _check_value(case, n),
                     description=f"инвариант «{name}» по итоговому XML")
    suite.metric("quality/scheme_checks_share", lambda case: case.gate_share,
                 description="доля пройденных гейт-проверок на схему (планка ≥0.8: "
                             "монотонно по каждому снятому нарушению; конъюнкция "
                             "этого ряда из таблицы снята — она всегда ноль)")
    suite.metric("quality/business_checks_share", lambda case: case.business_share,
                 description="то же по бизнес-слою; вне планки — потолок задан "
                             "набором (эталон тоже держит узкое место)")
    suite.metric("business/smells", _smell_density,
                 description="доля бизнес-узких мест оракула среди применимых "
                             "бизнес-проверок той же схемы (плотность, а не "
                             "абсолютное число: раньше одна схема с двумя "
                             "узкими местами и другая с двумя из пяти давали "
                             "одно среднее)",
                 direction=metrics.LOWER)
    suite.metric("business/not_worse_than_etalon",
                 lambda case: _not_worse_than_etalon(case, etalon),
                 description="схема не хуже эталона того же сценария по бизнес-слою")
    suite.metric("business/scorer_oracle_agreement",
                 lambda case: agreement_share(case.drift),
                 description="доля бизнес-правил, где оракул и скоринг сказали "
                             "одно и то же по одной схеме")
    suite.metric("quality/structure_checks_share",
                 lambda case: case.structure_gate_share,
                 description="то же число по починенной структуре (до генерации "
                             "XML): здесь план, а не транспорт")
    suite.metric("structure_xml_agreement",
                 lambda case: None if not case.ok else (
                     0.0 if case.disagreements else 1.0),
                 description="починенная структура и XML не расходятся по инвариантам")
    suite.metric("repair_changed_share",
                 lambda case: None if not case.ok else float(case.repairs > 0),
                 description="доля схем, где починка структуры внесла правки")
    suite.metric("repairs_per_scheme",
                 lambda case: None if not case.ok else float(case.repairs),
                 description="среднее число правок починки на схему",
                 direction=metrics.LOWER)
    suite.metric("score", lambda case: case.score,
                 description="средний балл скоринга схемы (0..100)", unit="балл")
    suite.metric("generation_error_share",
                 lambda case: 0.0 if case.ok else 1.0,
                 description="доля несобранных сцен", direction=metrics.LOWER)
    suite.metric("latency_ms",
                 lambda case: case.latency_ms if case.mode == "live" else 0.0,
                 description="задержка обращения к модели (в replay обращений нет — 0)",
                 direction=metrics.LOWER, unit="мс")
    suite.metric("llm_retries",
                 lambda case: max(0, case.llm_calls - 1) if case.mode == "live" else 0.0,
                 description="обращений к модели сверх первого: число считает и "
                             "вторую стадию генерации, и переспросы починки, и "
                             "повторы транспорта — это глубина прогона, а не "
                             "сбой провайдера (счётчик ведётся на транспорте)",
                 direction=metrics.LOWER)
    # Размер входа — диагноз стоимости, а не корректности: в гейт
    # `quality/scheme_checks_share` он не входит и не может его ни поднять, ни
    # уронить. Смысл строк в том, что промпт растёт молча: весь свод правил едет
    # в каждом вызове, и без этой пары чисел удвоение входа выглядит как «качество
    # упало само». В replay строк с транспорта нет, поэтому числа посчитаны по
    # шаблонам ядра (`observed=False` в JSON), а доля двухстадийных прогонов
    # остаётся «н/д» — путь в replay не наблюдался.
    suite.metric("cost/prompt_chars_per_call", _prompt_chars_per_call,
                 description="символов входа на один вызов стадии (среднее по "
                             "кейсу)", direction=metrics.LOWER, unit="симв.")
    suite.metric("cost/prompt_tokens_per_call", _prompt_tokens_per_call,
                 description=f"оценка токенов на вызов (символов на токен — "
                             f"{CHARS_PER_TOKEN:g})", direction=metrics.LOWER,
                 unit="токенов")
    suite.metric("cost/prompt_chars_per_case", _prompt_chars_per_case,
                 description="символов входа на все вызовы одного кейса (в replay "
                             "в сумму сложены обе ветки пути — две стадии и "
                             "одношаговый откат: сравнение их видно по строкам "
                             "стадий в секции стоимости)",
                 direction=metrics.LOWER, unit="симв.")
    suite.metric("cost/prompt_tokens_per_case", _prompt_tokens_per_case,
                 description="оценка токенов на кейс целиком (та же сумма)",
                 direction=metrics.LOWER, unit="токенов")
    suite.metric("cost/prompt_two_stage_share", _two_stage_share,
                 description="доля кейсов, где план собирался двумя стадиями "
                             "(состав + маршрут), а не откатом в одношаговый: "
                             "в replay «н/д» — путь не наблюдался")
    return suite


def coverage_note(cases: Sequence["GenCase"], mode: str) -> str:
    """Почему прогон с отбраковкой кейсов нельзя сравнивать с предыдущим.

    Живые прогоны #53/#54 теряли кейсы на 429 провайдера: доли инвариантов при
    этом считаются по уцелевшим 19 из 24, и «has_timer 0.667 → 1.0» оказывается
    не улучшением, а сменой выборки. Метаданные прогона обязаны говорить это
    сами, иначе разбор начнётся с вывода, которого в данных нет.
    """
    if mode != "live" or not cases:
        return ""
    lost = [c for c in cases if not c.ok]
    if not lost:
        return ""
    scenes = ", ".join(sorted({f"{c.scenario} r{c.repeat}" for c in lost})[:6])
    return (f"собрано {len(cases) - len(lost)} схем из {len(cases)}: "
            f"{len(lost)} не сгенерированы ({scenes}). Все доли посчитаны по "
            "уцелевшим кейсам — этот прогон не пара ни одному предыдущему, "
            "сверяйте с ним только число несобранных схем")


def _share(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    """Доля без изобретения нуля: None — когда факта нет или делить не на что.

    Это правило всего набора improvement-метрик: кейс без данных не должен
    попадать в знаменатель и превращать «не померено» в «улучшение не
    сработало».
    """
    if part is None or whole is None or whole <= 0:
        return None
    return float(part) / float(whole)


def _op_acceptance(case: ImproveCase) -> Optional[float]:
    """Доля операций пакета, реально изменивших схему.

    Строки отчёта — не работа: один `add_task` даёт несколько строк, а
    «применилось зря» (`добавлено не было`) раньше шло в числитель успеха. Здесь
    числитель — applied минус noop, знаменатель — все строки решения о пакете;
    откатанный пакет даёт честный 0.0, потому что `applied` у него пуст.
    """
    total = len(case.applied) + len(case.skipped)
    if case.noop_rows is None:
        return None
    # `noop_rows` считается до отката (это свойство пакета, а не схемы), поэтому
    # после сверки отчёта он может превысить число оставшихся строк.
    return _share(max(0, len(case.applied) - case.noop_rows), total)


def _noop_share(case: ImproveCase) -> Optional[float]:
    """Доля «применённых» строк, не изменивших схему."""
    return _share(case.noop_rows, len(case.applied))


# Отказ «то, что ты просишь, уже сделано». В живом прогоне #61 это крупнейший
# класс: 33 строки из 121 отказа — `connect` дугой, которая на схеме стоит.
_REDUNDANT_REFUSALS = ("уже существует", "уже есть", "уже стоит", "уже присоединён")


def _redundant_refusals_share(case: ImproveCase) -> Optional[float]:
    """Доля отказов, которые отвергли повтор, а не ошибку.

    Нужна отдельным числом, потому что `improve/op_acceptance` смешивает два
    разных брака: правку, которую аплайер не мог выполнить (выдуманный id,
    неверный род узла), и правку, которая схеме не нужна (она уже сделана). Для
    пользователя это разные очереди: первую чинят подсказкой об операнде,
    вторую — советом, который перестанет предлагать существующее.
    """
    if not case.skipped:
        return None
    redundant = sum(1 for s in case.skipped
                    if any(m in str(s.get("reason") or "")
                           for m in _REDUNDANT_REFUSALS))
    return _share(redundant, len(case.skipped))


def _retry_gain(case: ImproveCase) -> Optional[float]:
    """Доля отказов первого раунда, закрытых корректирующим повтором.

    Прежняя версия возвращала АБСОЛЮТНОЕ число закрытых строк (`retry_closed`) и
    агрегировалась средним по кейсам: прогон #58 давал 0.18, #59–#62 — 0, и ни
    одно из этих чисел нельзя было прочесть как «повтор помогает». Кейс с одним
    отказом и кейс с десятью входили в среднее с одинаковым весом, а
    знаменатель метрика не видел вовсе. Теперь это доля: 0 читается как «из
    названных отказов не закрыт ни один», 1.0 — как «закрыты все».

    Выборка — только кейсы с состоявшимся повтором и с отказом первого раунда:
    на остальных метрика физически не определена, и нули там только дешевле
    выглядели бы.
    """
    if case.retry_attempted is not True or case.retry_closed is None:
        return None
    first_round = [s for s in case.skipped if s.get("stage") != "retry"]
    if not first_round:
        return None
    return float(case.retry_closed) / len(first_round)


def _flag(value: Any) -> Optional[float]:
    """Факт-флаг в число: None остаётся None («не померено»), пусто — 0.0."""
    return None if value is None else float(bool(value))


def _package_revert_share(case: ImproveCase) -> Optional[float]:
    """Гарант откатил пакет целиком к базовой схеме."""
    return _flag(case.package_reverted)


def _rules_regressed_share(case: ImproveCase) -> Optional[float]:
    """Пакет сломал хотя бы одно правило скоринга, прошедшее на базе.

    Честная замена `no_regression`: та сравнивала итоговый балл с базовым и
    не отличала «ничего не тронули» от «одно правило починили, другое
    сломали, дельта 0».
    """
    return _flag(case.rules_regressed)


def _plan_truncated_share(case: ImproveCase) -> Optional[float]:
    """План обрезаётся лимитом операций: это срез контура, а не качество модели."""
    return None if case.truncated_operations is None else float(
        case.truncated_operations > 0)


def _repeat_rejection_share(case: ImproveCase) -> Optional[float]:
    """Доля отказов, которые повтор вернул дословно: тот же дефект, а не
    новая работа."""
    if case.retry_attempted is not True:
        return None
    dupes = sum(1 for s in case.skipped if s.get("duplicate"))
    return _share(dupes, len(case.skipped))


def _defects_introduced(case: ImproveCase) -> Optional[float]:
    """Доля инвариантов, проходивших на базовой схеме, которые пакет уронил.

    Вторая половина пары к `defects_repaired` и честная замена `no_regression`:
    итоговый балл не отличает «одно правило починили, другое сломали, дельта 0»
    от «ничего не тронули», а счётчик упавших — отличает.

    Знаменатель — то, что на базе проходило: абсолютное число сломанных
    (прежняя мера, усреднённая по кейсам) у схемы с десятью целыми инвариантами
    и у схемы с двумя значит разное, а среднее по кейсам делало их равноправными
    — тот же изъян геометрии, что был у `improve/retry_gain` до перевода на долю.
    """
    if not case.checks_before or not case.checks_after:
        return None
    broken = {c.name for c in invariants.applicable_checks(case.checks_after)
              if not c.ok}
    passing_before = [c for c in invariants.applicable_checks(case.checks_before)
                      if c.ok]
    return _share(sum(1 for c in passing_before if c.name in broken),
                  len(passing_before))


def _base_advice_drift(case: "ImproveCase") -> Optional[float]:
    """Доля бизнес-правил, где два слоя оценили базовую схему по-разному.

    Это доля подсказок, по которым пакет строился вслепую: скоринг объявил
    дефект, которого оракул не видит (и тогда правка ушла в `skipped` или в
    несуществующее узкое место), или наоборот — оракул нашёл узкое место, о
    котором модель не услышала и не починила. Ноль здесь и есть гарантия, что
    `defects_repaired` и `op_acceptance` меряют один и тот же список дефектов.

    Прежняя мера — абсолютное число расхождений под средним по кейсам: пакет из
    одного правила и пакет из десяти входили в среднее одинаково, а
    знаменатель (сколько бизнес-правил вообще сравнивалось на этой схеме) метрика
    не видела. `None`, если сравнивать было нечего (кейс без базовой схемы, без
    одного из слоёв или без ни одного сопоставимого правила)."""
    if not case.ok or not case.drift_before:
        return None
    comparable = [row for row in case.drift_before.values()
                  if row.get("verdict") != invariants.NO_DATA]
    return _share(len(invariants.business_disagreements(case.drift_before)),
                  len(comparable))


def _mark_off_target(case: "ImproveCase") -> None:
    """Претензия фикстуры обязана описывать дефект, который у базовой схемы есть.

    Сверяется по независимому оракулу (`checks_before`), а не по тексту подсказки:
    у скоринга и оракула на базовой схеме пороги расходятся (см.
    `DOCUMENTED_DIVERGENCES`), а решение «чинить или не спрашивать модель» должно
    приниматься по той же мере, которой потом мерят `defects_repaired`.
    Фикстура без `targets` — не размечена: кейс считается как раньше, но факт
    «применимость не проверялась» попадает в `unmeasured`, а не молчит.
    """
    if not case.targets:
        if not case.targets_declared:
            case.unmeasured.append(
                "применимость претензии не размечена (нет ключа `targets`)")
        return
    failing = {c.name for c in invariants.applicable_checks(case.checks_before)
               if not c.ok}
    if set(case.targets) & failing:
        return
    case.off_target = True
    case.off_target_note = (
        "претензия описывает дефект, которого в базовой схеме нет: названо "
        + ", ".join(case.targets)
        + "; проваливаются " + (", ".join(sorted(failing)) or "нет ни одного"))


def _advice_off_target_share(case: "ImproveCase") -> Optional[float]:
    """1.0 — претензия пакета не про эту базовую схему, 0.0 — про неё, None —
    не проверялось (фикстура без `targets` или осознанно пустой список).
    Метрика LOWER: ненулевое среднее значит, что часть выборки улучшения собрана
    вопросами не по адресу и в качество пакета не входит."""
    if not case.targets:
        return None
    return 1.0 if case.off_target else 0.0


def build_improvement_suite() -> metrics.EvaluationSuite:
    """Метрики улучшения: только факты отчёта применения и исходы оракула.

    Старый набор мерил сам отчёт вместо работы: `applied_share` и
    `skipped_share` считали строки (один `add_task` — это несколько строк, а
    «применилось зря» увеличивало «applied»), `retry_needed_share` выводилась из
    наличия строк со стадией retry, а `no_regression` сравнивал итоговый балл с
    базовым и прятал ухудшение под дельту 0. Все три сняты вместе с выводами.
    """
    suite = metrics.EvaluationSuite()
    suite.metric("improve/op_acceptance", _op_acceptance,
                 description="доля операций пакета, реально изменивших схему "
                             "(applied без no-op строк над всеми решениями)")
    suite.metric("improve/noop_share", _noop_share, direction=metrics.LOWER,
                 description="доля «применённых» строк, не изменивших схему")
    suite.metric("improve/redundant_refusals_share", _redundant_refusals_share,
                 direction=metrics.LOWER,
                 description="доля отказов, отвергших уже сделанное (просьба "
                             "повторить дугу), а не невыполнимую правку")
    suite.metric("improve/retry_gain", _retry_gain,
                 description="доля отказов первого раунда, закрытых повтором "
                             "(только кейсы с состоявшимся повтором)")
    suite.metric("improve/package_revert_share", _package_revert_share,
                 direction=metrics.LOWER,
                 description="гарант откатил пакет целиком к базовой схеме")
    suite.metric("improve/rules_regressed_share", _rules_regressed_share,
                 direction=metrics.LOWER,
                 description="пакет сломал хотя бы одно правило скоринга, "
                             "которое проходило на базовой схеме")
    suite.metric("improve/plan_truncated_share", _plan_truncated_share,
                 direction=metrics.LOWER,
                 description="план обрезан лимитом операций: это срез контура, "
                             "а не качество модели")
    suite.metric("improve/repeat_rejection_share", _repeat_rejection_share,
                 direction=metrics.LOWER,
                 description="повтор вернул дословно тот же отказ")
    suite.metric("improve/scheme_checks_share", lambda case: case.gate_share,
                 description="доля пройденных гейт-проверок в улучшенной схеме "
                             "(конъюнкция `improve/pass@1` из таблицы снята: 0.056 "
                             "живого прогона — наследие генерации, а не пакет)")
    suite.metric("improve/base_checks_share", lambda case: case.base_gate_share,
                 description="то же число до пакета: пара «до/после» и есть вклад "
                             "пакета в нотацию, без наследования провалов генерации")
    suite.metric("improve/score_delta", lambda case: case.score_delta,
                 description="дельта балла после применения пакета", unit="балл")
    suite.metric("improve/defects_repaired", lambda case: case.repaired_share,
                 description="доля дефектов базовой схемы, которые пакет убрал "
                             "(нотация и бизнес вместе, по оракулу)")
    suite.metric("improve/advice_ignored_share", _advice_ignored_share,
                 direction=metrics.LOWER,
                 description="доля подсказок базовой схемы, по которым пакет не "
                             "внёс ни одной правки и не получил отказа (прошёл "
                             "мимо): repaired_share и op_acceptance на таком "
                             "молчании остаются приличными")
    suite.metric("improve/business_repaired_share",
                 lambda case: case.business_repaired_share,
                 description="то же по одному бизнес-слою: узкие места процесса, "
                             "а не валидность XML")
    suite.metric("improve/base_advice_drift", _base_advice_drift,
                 direction=metrics.LOWER,
                 description="доля бизнес-правил, где оракул и скоринг разошлись "
                             "на базовой схеме: по такой подсказке пакет не обязан "
                             "улучшать процесс")
    suite.metric("improve/defects_introduced", _defects_introduced,
                 direction=metrics.LOWER,
                 description="доля инвариантов, прошедших до пакета, которые он "
                             "уронил (от всех проходивших на базовой схеме)")
    suite.metric("improve/error_share", lambda case: 0.0 if case.ok else 1.0,
                 description="доля пакетов, которые не удалось применить",
                 direction=metrics.LOWER)
    suite.metric("improve/advice_off_target_share", _advice_off_target_share,
                 description="доля кейсов, где претензия пакета не про эту "
                             "базовую схему: правку не запрашивали, и в качество "
                             "пакета такой кейс не входит",
                 direction=metrics.LOWER)
    # Кейс «претензия не про эту схему» не получил пакета вовсе: он не может ни
    # починить, ни сломать, ни применить, и в знаменателе качества ему делать
    # нечего. Исключение — сама применимость: её и надо видеть отдельным числом,
    # а не спрятанной внутри доли ремонтов.
    for m in suite.metrics:
        if m.name == "improve/advice_off_target_share":
            continue
        inner = m.fn
        m.fn = (lambda case, _f=inner:
                None if getattr(case, "off_target", False) else _f(case))
    return suite


def spread_stats(cases: Sequence[GenCase], repeat: int = 1) -> Dict[str, Optional[float]]:
    """Разброс балла: два разных числа, а не одно под общим заголовком.

    `score_cv_across_scenarios` — разброс между РАЗНЫМИ кейсами одного прогона.
    При `--repeat 1` это единственное, что посчитано, и раньше оно печаталось под
    заголовком «разброс по повторам»: 0.051 в replay — это смесь фикстур
    (хороший план рядом с плохим), а не шум модели, и в baseline она лежала как
    LOWER-is-better, то есть гейтилась выборкой сценариев.

    `score_cv_per_case` — настоящая дисперсия живого прогона: СКО повторов одной
    и той же сцены. Считается, как только повторов ≥2, но в гейт попадает только
    при `repeat >= MIN_LIVE_REPEATS` (см. `RunReport.metrics_flat`): два прогона
    дают разность двух случайностей, а не меру разброса.
    """
    scores = [c.score for c in cases if c.score is not None]
    per_case_cv: List[float] = []
    grouped: Dict[str, List[GenCase]] = {}
    for case in cases:
        grouped.setdefault(case.key, []).append(case)
    for group in grouped.values():
        values = [c.score for c in group if c.score is not None]
        cv = metrics.coefficient_of_variation(values)
        if cv is not None:
            per_case_cv.append(cv)
    widest = max((len(group) for group in grouped.values()), default=0)
    return {
        "runs": float(len(scores)),
        "repeats_per_case": float(widest),
        "score_p50": metrics.p50(scores),
        "score_p95": metrics.p95(scores),
        "score_cv_across_scenarios": metrics.coefficient_of_variation(scores),
        "score_cv_per_case": metrics.mean(per_case_cv) if per_case_cv else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
    }


def spread_note(spread: Mapping[str, Optional[float]],
                mode: str = "replay") -> str:
    """Одной строкой — что именно показывает секция разброса в этом прогоне.

    Без неё читатель принимает межсценарную смесь за дисперсию модели и делает
    вывод о живом контуре по replay-числам.
    """
    repeats = int(spread.get("repeats_per_case") or 0)
    if repeats >= 2:
        line = (f"повторов на кейс: {repeats}; `score_cv_per_case` — дисперсия "
                "повторов одной сцены")
        if repeats < MIN_LIVE_REPEATS:
            line += (f"; при {repeats} повторах она печатается, но в baseline и "
                     f"в гейт не входит — нужно --repeat {MIN_LIVE_REPEATS} или "
                     "больше")
    else:
        line = ("повторов на кейс: 1 — дисперсия модели не мерялась; "
                "`score_cv_across_scenarios` ниже это разброс между РАЗНЫМИ "
                f"кейсами (смесь фикстур), а не шум прогона; для живой меры "
                f"нужно --repeat {MIN_LIVE_REPEATS} и более")
    if mode == "live" and repeats < MIN_LIVE_REPEATS:
        line += (f"; live-прогон с {repeats} повтором даёт только межсценарное "
                 "число — для оценки разброса GigaChat поднимите --repeat")
    return line


def _advice_ignored_share(case: "ImproveCase") -> Optional[float]:
    """Доля подсказок базовой схемы, по которым пакет не сделал НИЧЕГО.

    `defects_repaired` отвечает на «сколько дефектов убрано», `op_acceptance` —
    на «сколько правок принято». Между ними дыра: пакет может пройти мимо
    нарушения, которое ему назвали, — не починить и не попытаться, и тогда оба
    числа останутся приличными, потому что считать им будет нечего. Это тот же
    прогон #55, где модель вставляла развилку туда, о её отсутствии её не просили.

    «Попытка» читается по строкам отчёта: если хоть одна применённая или
    отвергнутая операция называет id из нарушения, правка по этой подсказке шла и
    отказ был не молчанием, а отказом. None — когда дефектов в базе нет или
    пакета не было.
    """
    if not case.checks_before or not case.checks_after:
        return None
    defects = [c for c in invariants.applicable_checks(case.checks_before)
               if not c.ok]
    if not defects:
        return None
    still_bad = {c.name for c in invariants.applicable_checks(case.checks_after)
                 if not c.ok}
    touched = set()
    for row in list(case.applied) + list(case.skipped):
        for value in row.values():
            if isinstance(value, str):
                touched.update(part for part in re.split(r"[^0-9A-Za-zа-яё_.-]+",
                                                         value) if part)
    ignored = [c for c in defects
               if c.name in still_bad and not (set(c.ids) & touched)]
    return _share(len(ignored), len(defects))


def improve_metric_origins(
        improve_cases: Sequence[ImproveCase],
        improvement: metrics.SuiteResult) -> Dict[str, Dict[str, Any]]:
    """Из чего состоит выборка каждой метрики улучшения — по происхождению ответа.

    `SuiteResult` считает среднее по всем кейсам, а кейсы у него разные: пакет,
    сочинённый человеком, который знает аплайер, проходит его с первого запроса,
    и на таком кейсе `improve/op_acceptance` описывает аплайер и автора фикстуры.
    Числу без этой строки нельзя верить как вердикту модели — поэтому она и едет
    рядом с каждой метрикой в отчёт (`format_metric_origins`), а не живёт только
    в JSON.

    Порядок `values` у метрики совпадает с порядком кейсов (suite идёт по одному
    списку), поэтому значение `None` здесь читается как «этот кейс в знаменатель
    не попал», а не как «попал с нулём».
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, result in improvement.metrics.items():
        if not name.startswith("improve/"):
            continue
        sources = [case.answer_source
                   for case, value in zip(improve_cases, result.values)
                   if value is not None]
        counts = provenance.origin_counts(sources)
        out[name] = {
            "counts": counts,
            "cases": len(sources),
            "live": counts[provenance.MODEL_CAPTURE],
            "hand": counts[provenance.HAND_AUTHORED],
            "unknown": counts[provenance.UNKNOWN_SOURCE],
            # Метрика без единого значения — это «не померено», а не «модель
            # не показала ничего»: вердикт о владельце числа здесь выносить не из
            # чего, и сочинять его нельзя (та же причина, по которой n=0
            # печатается как «н/д», а не как 0.0).
            "verdict": ("выборки нет: ни один кейс не дал значения метрики"
                        if not sources else
                        provenance.origin_verdict(counts, metrics.MIN_GATED_N)),
        }
    return out


def format_metric_origins(origin: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """Строка рядом с каждой метрикой улучшения: что она мерит по составу выборки."""
    if not origin:
        return ["  метрик улучшения в этом прогоне нет — и состава выборки тоже"]
    lines: List[str] = []
    for name, row in sorted(origin.items()):
        lines.append(f"  {name:<38} живых захватов {row['live']}, рукой "
                     f"{row['hand']}, без разметки {row['unknown']} — "
                     f"{row['verdict']}")
    return lines


def cost_stats(cases: Sequence[GenCase],
               improve_cases: Sequence["ImproveCase"],
               mode: str) -> Dict[str, Any]:
    """Стоимость и задержка прогона на одну ПРИНТУЮ схему.

    Среднее число обращений на кейс отвечает на другой вопрос («сколько модель
    болтает»), а платят за результат: 3 вызова на бракованную схему и 3 вызова
    на принятую — разная цена за один и тот же объём полезного выхода. Числа
    считаются как отношение сумм, а не как среднее пер-кейсовых долей: доля
    сумм — единственная мера, которая не меняется от того, в каком порядке
    разложились провалы.

    В replay обращений к модели нет вовсе (`llm_call_counter` играет только
    live), поэтому стоимости здесь нет, и `calls_per_accepted_scheme` возвращает
    None: ноль в этой строке читался бы как «контур бесплатен», чего replay не
    измерял. Размер входа — другое дело: он задаётся шаблонами ядра и данными
    кейса, поэтому считается и в replay, но помечен `prompt_observed=False` —
    это бюджет, а не запись обращения.
    """
    accepted = [c for c in cases
                if c.ok and c.gate_share is not None
                and c.gate_share >= QUALITY_BAR]
    calls = sum(int(c.llm_calls or 0) for c in cases) \
        + sum(int(c.llm_calls or 0) for c in improve_cases)
    latency = sum(c.latency_ms for c in cases) + sum(c.latency_ms for c in improve_cases)
    measured = mode == "live" and calls > 0
    # Размер входа по стадиям — из строк кейсов: в live они записаны с транспорта,
    # в replay посчитаны по шаблонам ядра, и `observed` в сумме говорит, что из
    # этих чисел следует (и только за ним), а что — оценка бюджета.
    rows = [r for c in list(cases) + list(improve_cases) for r in c.prompt_calls]
    # Знаменатель «на кейс» — кейсы, у которых строка входа есть: делить сумму
    # на все кейсы прогона значило бы занижать число за счёт тех, где вызов
    # вообще не проектировался (off-target претензия, несобранная схема).
    with_rows = [c for c in list(cases) + list(improve_cases) if c.prompt_calls]
    by_stage: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        stage = str(row.get("stage") or "иное")
        bucket = by_stage.setdefault(stage, {"calls": 0, "chars": 0,
                                             "observed": True})
        bucket["calls"] += 1
        bucket["chars"] += int(row.get("chars") or 0)
        bucket["observed"] = bucket["observed"] and bool(row.get("observed"))
    for bucket in by_stage.values():
        bucket["tokens_est"] = estimate_prompt_tokens(bucket["chars"])
    chars_total = sum(int(r.get("chars") or 0) for r in rows)
    return {
        "mode": mode,
        "schemes": float(len(cases)),
        "accepted_schemes": float(len(accepted)),
        "model_calls_total": float(calls) if mode == "live" else None,
        "latency_ms_total": round(latency, 1) if mode == "live" else None,
        "calls_per_accepted_scheme": (calls / len(accepted)) if measured and accepted else None,
        "latency_ms_per_accepted_scheme": (round(latency / len(accepted), 1)
                                           if measured and accepted else None),
        # Стоимость входа — вторая половина того же вопроса: обращение к модели
        # стоит столько, сколько в него положили, а число обращений об этом
        # молчит. None, когда строк нет вовсе: 0 читался бы как «промпт пуст».
        "prompt_calls_total": float(len(rows)) if rows else None,
        "prompt_chars_total": float(chars_total) if rows else None,
        "prompt_tokens_est_total": estimate_prompt_tokens(chars_total) if rows else None,
        "prompt_chars_per_case": (round(chars_total / len(with_rows), 1)
                                  if with_rows else None),
        "prompt_by_stage": by_stage,
        "prompt_observed": bool(rows) and all(bool(r.get("observed")) for r in rows),
        "note": ("" if measured else
                 "в replay обращений к модели нет — стоимость на принятую схему "
                 "не меряется; для этого числа нужен --mode live"),
        "prompt_note": ("" if not rows else (
            "числа сняты с настоящих обращений (live)"
            if all(bool(r.get("observed")) for r in rows) else
            "часть чисел посчитана по шаблонам ядра без обращения к модели "
            "(replay): это бюджет входа, а не запись вызова; в сумме идут обе "
            "ветки пути (две стадии и одношаговый откат), какая из них что стоит "
            "— по строкам стадий выше")),
    }


def format_cost(cost: Mapping[str, Any]) -> List[str]:
    """Секция стоимости: цифры либо явное «не мерялось», но не молчание."""
    lines = ["СТОИМОСТЬ НА ПРИНЯТУЮ СХЕМУ"]
    lines.append(f"  принятых схем {int(cost.get('accepted_schemes') or 0)} из "
                 f"{int(cost.get('schemes') or 0)} (гейт ≥{QUALITY_BAR:.0%})")
    for key, label in (("model_calls_total", "обращений к модели"),
                       ("latency_ms_total", "суммарная задержка, мс"),
                       ("calls_per_accepted_scheme", "обращений на принятую схему"),
                       ("latency_ms_per_accepted_scheme", "мс на принятую схему")):
        value = cost.get(key)
        lines.append(f"  {label:<34}{_fmt(value)}")
    if cost.get("note"):
        lines.append(f"  {cost['note']}")
    lines += format_prompt_sizes(cost)
    return lines


def format_prompt_sizes(cost: Mapping[str, Any]) -> List[str]:
    """Размер входа по стадиям — отдельная подсекция стоимости.

    Отдельная, а не колонка в таблице метрик: у стадий разные знаменатели.
    «Состав» зовётся на каждом кейсе, «переспрос» — только там, где план-гейт
    нашёл что спрашивать, и среднее по всем вызовам спрятало бы, где именно
    лежит бюджет входа.
    """
    by_stage = dict(cost.get("prompt_by_stage") or {})
    lines = ["", "  РАЗМЕР ПРОМПТА (вход одного вызова и кейса целиком)"]
    if not by_stage:
        # Пусто — это «не померено», а не «промпт пустой»: строка обязана
        # говорить первое, иначе снятая метрика выглядела бы нулевой.
        lines.append("    н/д: строк с размером входа нет ни у одного кейса")
        return lines
    for stage in PROMPT_STAGES:
        if stage in by_stage:
            _render_bucket(lines, stage, by_stage.pop(stage))
    for stage, bucket in sorted(by_stage.items()):
        _render_bucket(lines, stage, bucket)
    lines.append(f"    всего: вызовов {int(cost.get('prompt_calls_total') or 0)}, "
                 f"символов {int(cost.get('prompt_chars_total') or 0)}, оценка "
                 f"токенов ≈{int(cost.get('prompt_tokens_est_total') or 0)}, на кейс "
                 f"{int(cost.get('prompt_chars_per_case') or 0)} симв.")
    if cost.get("prompt_note"):
        lines.append(f"    {cost['prompt_note']}")
    return lines


def _render_bucket(lines: List[str], stage: str,
                   bucket: Mapping[str, Any]) -> None:
    """Одна стадия одной строкой: вызовы, символы, оценка токенов, источник числа."""
    how = "наблюдено" if bucket.get("observed") else "по шаблонам ядра"
    lines.append(f"    {stage:<26}вызовов {bucket.get('calls', 0):>3} · "
                 f"символов {int(bucket.get('chars') or 0):>7} · ≈"
                 f"{int(bucket.get('tokens_est') or 0):>7} токенов · {how}")


# ---------------------------------------------------------------------------
# отчёт
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    mode: str
    repeat: int
    scenarios: List[str]
    generated_at: str
    cases: List[GenCase]
    improve_cases: List[ImproveCase]
    generation: metrics.SuiteResult
    improvement: metrics.SuiteResult
    spread: Dict[str, Optional[float]]
    regressions: List[metrics.Regression]
    baseline_path: str = ""
    baseline_note: str = ""
    coverage_note: str = ""
    #: причина, по которой сверка с baseline пропущена из-за смены линейки.
    #: Пусто — значит линейка та же и сверка состоялась.
    ruler_change: str = ""
    #: изменение линейки принято явно (`--accept-ruler-change`): код выхода по
    #: нему не судят, но пометка в отчёте остаётся.
    ruler_change_accepted: bool = False
    #: ошибки самих метрик (`ОШИБКА метрики`): дыра в харнессе, а не в схеме.
    metric_errors: Dict[str, str] = field(default_factory=dict)
    #: метрики, у которых выборка ниже порога `metrics.MIN_GATED_N`: вердикта по
    #: ним нет, и в гейт они не входят.
    under_sampled: List[str] = field(default_factory=list)
    #: стоимость прогона на принятую схему (обращения и задержка): см. `cost_stats`.
    cost: Dict[str, Any] = field(default_factory=dict)
    #: состав выборки каждой метрики улучшения по происхождению ответа: см.
    #: `improve_metric_origins`. Без него доля принятых операций по рукописным
    #: пакетам читается как вердикт модели.
    improve_sample_origin: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ruler: Dict[str, Any] = field(default_factory=ruler_fingerprint)
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def ruler_fingerprint_hash(self) -> str:
        return ruler_hash(self.ruler)

    def manual_review(self) -> Dict[str, Any]:
        """Лист ручной оценки: то, что линейка мерить не имеет права.

        Читается глазами, а не оракулом: имя шага звучит как действие или как
        отглагольное существительное, выдуман ли шаг, которого в описании нет,
        видит ли человек маршрут без разбора id. Ни одной метрики здесь нет
        намеренно: второй шумная модель, оценивающая первую, добавила бы к шуму
        генерации ещё шум оценки, а «довести метрику» незаметно превратилось бы
        в «Самооценка». Поэтому — список того, на что смотреть, и адрес схемы;
        вердикт выносит человек и записывает его вне отчёта.

        В список попадают схемы, которые автоматика уже пропустила (сводная доля
        гейт-проверок ≥ планки): у брака владелец назван в атрибуции, и
        рассматривать его руками смысла нет — чинить надо названный класс.
        """
        shown = [c for c in self.cases
                 if c.ok and c.gate_share is not None
                 and c.gate_share >= QUALITY_BAR]
        return {
            "items": list(MANUAL_REVIEW_ITEMS),
            "bar": QUALITY_BAR,
            "cases": [{"case": c.key, "gate_share": round(c.gate_share, 3),
                       "business_smells": c.business_smells,
                       "pools": c.pools, "elements": c.elements}
                      for c in shown],
        }

    def metrics_flat(self) -> Dict[str, Optional[float]]:
        """Метрики для baseline и сверки.

        `spread/runs` — число прогонов, а не качество контура: класть его в
        baseline нельзя, иначе смена `--repeat` выглядела бы как регрессия. По
        той же причине вне сверки остаются `spread/repeats_per_case` (тот же
        переключатель CLI) и `spread/score_cv_across_scenarios`: межсценарный
        разброс зависит от выбранного набора сценариев, а не от контура, и
        гейтить его — значит ловить «регрессию» при `--scenarios`.
        """
        data = self.generation.flat()
        data.update(self.improvement.flat())
        skip = {"runs", "repeats_per_case", "score_cv_across_scenarios"}
        data.update({f"spread/{name}": value
                     for name, value in self.spread.items() if name not in skip})
        # Живая дисперсия становится гейт-метрикой только при достаточном числе
        # повторов: до `MIN_LIVE_REPEATS` она печатается в отчёте, но в baseline
        # не кладётся, иначе CI красился бы от двух случайных ответов модели.
        if self.repeat < MIN_LIVE_REPEATS:
            data["spread/score_cv_per_case"] = None
        return data

    def directions(self) -> Dict[str, str]:
        """Направление «лучше» по каждой метрике — нужно детектору регрессий."""
        out = {result.name: result.direction
               for result in list(self.generation.metrics.values())
               + list(self.improvement.metrics.values())}
        out["spread/score_p50"] = metrics.HIGHER
        out["spread/score_p95"] = metrics.HIGHER
        out["spread/score_min"] = metrics.HIGHER
        out["spread/score_max"] = metrics.HIGHER
        out["spread/score_cv_across_scenarios"] = metrics.LOWER
        out["spread/score_cv_per_case"] = metrics.LOWER
        out["spread/runs"] = metrics.HIGHER
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "repeat": self.repeat,
            "scenarios": self.scenarios,
            "generated_at": self.generated_at,
            "metrics": {"generation": self.generation.as_dict(),
                        "improvement": self.improvement.as_dict(),
                        "flat": self.metrics_flat()},
            "spread": self.spread,
            "spread_note": spread_note(self.spread, self.mode),
            "ruler": self.ruler,
            "ruler_hash": self.ruler_fingerprint_hash,
            "baseline": self.baseline_path,
            "baseline_note": self.baseline_note,
            "ruler_change": self.ruler_change,
            "ruler_change_accepted": self.ruler_change_accepted,
            "metric_errors": self.metric_errors,
            "under_sampled": self.under_sampled,
            "cost": self.cost,
            "improve_sample_origin": self.improve_sample_origin,
            "coverage_note": self.coverage_note,
            "regressions": [r.as_dict() for r in self.regressions],
            "provenance": self.provenance,
            "manual_review": self.manual_review(),
            "cases": [c.as_dict() for c in self.cases],
            "improvements": [c.as_dict() for c in self.improve_cases],
        }

    def write_baseline(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": self.generated_at,
            "mode": self.mode,
            "repeat": self.repeat,
            "scenarios": self.scenarios,
            "directions": self.directions(),
            "metrics": self.metrics_flat(),
            "ruler": self.ruler,
            # Отпечаток одной строкой: по нему видно, каким набором правил собран
            # слепок, не разворачивая всю таблицу весов.
            "ruler_hash": self.ruler_fingerprint_hash,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return path


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    if not isinstance(value, float):
        return str(value)
    if value.is_integer() and abs(value) <= 1000:
        return str(int(value))
    return f"{value:.3f}"


def render_table(report: RunReport) -> str:
    """Человекочитаемая сводка: метрики, разброс, провалы по кейсам, регрессии."""
    lines: List[str] = [
        f"Режим {report.mode}, повторов {report.repeat}, сценариев "
        f"{len(report.scenarios)}, кейсов {len(report.cases)}",
        f"Линейка: правил {len(report.ruler.get('rules') or {})}, вес "
        f"{report.ruler.get('weights_total')}, инвариантов "
        f"{len(report.ruler.get('invariants') or [])}, отпечаток "
        f"{report.ruler_fingerprint_hash}", "", "МЕТРИКИ",
        f"{'метрика':<38}{'значение':>12}{'n':>5}  описание"]
    for suite in (report.generation, report.improvement):
        for name in suite.order:
            result = suite.metrics[name]
            # Вердикта нет — в таблице «н/д», а не среднее по одному кейсу:
            # 0.2 при n=1 читается как доля, хотя это один отказ одного пакета.
            # Само число остаётся в JSON (`mean`) и в списке ниже секции.
            value = _fmt(result.mean) if result.measured else "н/д"
            if result.error:
                value = "ошибка"
            lines.append(f"{name:<38}{value:>12}{result.n:>5}  "
                         + (f"[{result.error}] " if result.error else "")
                         + (f"[выборка {result.n} < {result.min_n}] "
                            if not result.measured and not result.error else "")
                         + result.description)
    lines += ["", "МЕТРИКИ УЛУЧШЕНИЯ: ИЗ ЧЕГО СОСТОИТ ВЫБОРКА",
              "  (живой захват ответа модели — мера модели; рукописный пакет — "
              "мера аплайера и автора фикстуры)",
              *format_metric_origins(report.improve_sample_origin)]
    lines += ["", "РАЗБРОС БАЛЛА", f"  {spread_note(report.spread, report.mode)}"]
    for name in ("runs", "repeats_per_case", "score_p50", "score_p95",
                 "score_cv_across_scenarios", "score_cv_per_case",
                 "score_min", "score_max"):
        lines.append(f"  {name:<28}{_fmt(report.spread.get(name))}")
    lines += ["", *format_cost(report.cost)]
    if report.under_sampled:
        lines += ["", "МЕТРИКИ БЕЗ МЕРЫ (выборка ниже порога — в гейт не входят)"]
        lines += [f"  {row}" for row in report.under_sampled]
    if report.metric_errors:
        lines += ["", "ОШИБКИ МЕТРИК (харнесс сломан, прогон нечитаем)"]
        lines += [f"  {name}: {err}"
                  for name, err in sorted(report.metric_errors.items())]
    lines += ["", "ГЕНЕРАЦИЯ: КЕЙСЫ"]
    for case in report.cases:
        state = "OK " if case.scenario_pass else "FAIL"
        # Вход кейса и путь: «двухстадийный» против «одношагового» — это разная
        # цена одного и того же описания, и на глаз она из чисел выше не видна.
        totals = prompt_totals(case.prompt_calls)
        path = stage_path(case) or "путь не наблюдался"
        lines.append(f"  [{state}] {case.key} ({case.quality}) — балл {_fmt(case.score)}, "
                     f"пулов {case.pools}, элементов {case.elements}, "
                     f"правок починки {case.repairs}"
                     + (f", вход {int(totals.get('chars') or 0)} симв."
                        f" ({path})" if totals else "")
                     + ("" if case.mode == "live" else " [по шаблонам ядра]"))
        if case.error:
            lines.append(f"        ошибка: {case.error}")
        for failure in invariants.summarize(case.checks_xml).get("failed", []):
            owner = case.attribution.get(failure["name"], "")
            lines.append(f"        ✗ {failure['name']}"
                         + (f" → виноват {owner}" if owner else "")
                         + f": {failure['reason']}")
        if case.disagreements:
            lines.append("        ⚠ структура и XML расходятся: "
                         + ", ".join(case.disagreements))
        for line in case.drift_lines:
            lines.append(f"        ⚠ слои меряют разное узкое место: {line}")
    lines += ["", "УЛУЧШЕНИЕ: КЕЙСЫ"]
    if not report.improve_cases:
        lines.append("  нет improve-фикстур для выбранных сценариев")
    for case in report.improve_cases:
        if case.off_target:
            # Отдельная строка, а не «0/0 операций»: кейс без запроса в модель
            # иначе читается как пакет, который ничего не смог.
            lines.append(f"  [⊘ ] {case.key} — правка не запрашивалась: "
                         f"{case.off_target_note}")
            continue
        total = len(case.applied) + len(case.skipped)
        done = 0 if case.noop_rows is None else len(case.applied) - case.noop_rows
        lines.append(f"  [{('OK ' if case.ok else 'FAIL')}] {case.key} — схему "
                     f"изменили {done}/{total} операций, {case.retry_phrase()}; балл "
                     f"{_fmt(case.score_before)} → {_fmt(case.score_after)}; ответ: "
                     f"{provenance.ORIGIN_SHORT.get(case.answer_source, case.answer_source)}")
        if case.error:
            lines.append(f"        ошибка: {case.error}")
        # Факты отчёта по одному на строку и только когда они не пустые: «откат
        # не сработал» молчит, а «откат сработал» обязан быть виден без чтения
        # JSON — иначе метрика `improve/package_revert_share` неотличима от 0.
        if case.package_reverted:
            lines.append(f"        ↩ пакет откатан целиком: {case.package_reverted}")
        if case.rules_regressed:
            lines.append("        ⚠ сломаны проходившие правила: "
                         + ", ".join(sorted(case.rules_regressed)))
        if case.noop_rows:
            lines.append(f"        ∅ «применилось» без изменения схемы: "
                         f"{case.noop_rows} стр.")
        if case.truncated_operations:
            lines.append(f"        ✂ план обрезан лимитом: отброшено "
                         f"{case.truncated_operations} операций")
        for name in case.unmeasured:
            lines.append(f"        ? не померено: {name}")
        for skip in case.skipped:
            closed = " ✓закрыт повтором" if skip.get("reapplied") else ""
            same = " (дословно)" if skip.get("duplicate") else ""
            lines.append(f"        ⊘ {skip.get('stage', 'план')}/{skip.get('op')}: "
                         f"{skip.get('reason')}{closed}{same}")
        for failure in invariants.summarize(case.checks_after).get("failed", []):
            owner = case.attribution.get(failure["name"], "")
            lines.append(f"        ✗ {failure['name']}"
                         + (f" → виноват {owner}" if owner else "")
                         + f": {failure['reason']}")
        for line in case.drift_lines:
            lines.append(f"        ⚠ по этой правке слои не сошлись: {line}")
    # Две секции, а не одна: владелец упавшего инварианта генерации («содержание
    # фикстуры») и владелец провала улучшения («дефект базовой схемы») — разные
    # контуры и разные очереди работы. В слитом списке строка «X — 5 из 8» была
    # утверждением сразу про оба, то есть ни про какой.
    lines += ["", "ГЕНЕРАЦИЯ: КТО ПОРОДИЛ ДЕФЕКТЫ"]
    lines += ["  " + line for line in attribution.format_tally(
        attribution.tally(*[c.attribution for c in report.cases]))]
    lines += ["", "УЛУЧШЕНИЕ: КТО ПОРОДИЛ ДЕФЕКТЫ"]
    lines += ["  " + line for line in attribution.format_tally(
        attribution.tally(*[c.attribution for c in report.improve_cases]))]
    review = report.manual_review()
    lines += ["", "РУЧНАЯ ОЦЕНКА (метрик здесь нет и быть не должно)"]
    lines += [f"  · {item}" for item in review["items"]]
    if not review["cases"]:
        lines.append(f"  смотреть нечего: ни одна схема не прошла гейт на "
                     f"{review['bar']:.0%} — сначала владелец дефекта из "
                     "атрибуции выше")
    else:
        lines.append(f"  схемы, пропущенные автоматикой (≥{review['bar']:.0%} "
                     f"гейт-проверок), их и смотрят глазами:")
        for row in review["cases"]:
            lines.append(f"    {row['case']} — гейт {row['gate_share']:.0%}, "
                         f"пулов {row['pools']}, элементов {row['elements']}"
                         + (", узкие места: " + ", ".join(row["business_smells"])
                            if row["business_smells"] else ""))
    lines += ["", "ПРОВЕНАНС КЕЙСОВ"]
    # Метрика стоит ровно столько, сколько стоит её набор: кейс, ответ которого
    # есть в few-shot промпта, мерит копирование, а не контур.
    if not report.provenance:
        lines.append("  разбор не выполнялся")
    else:
        lines += ["  " + line
                  for line in provenance.format_findings(report.provenance)]
        # Не «похож ли ответ на образец из промпта», а «кто вообще его прислал»:
        # у метрик качества владелец числа меняется вместе с происхождением
        # ответа, и без этой строки набор фикстур выглядит выборкой модели.
        for row in report.provenance.get("origin_lines") or []:
            lines.append("  " + row)
    lines.append("")
    if report.coverage_note:
        lines.append("ПОЛНОТА ПРОГОНА")
        lines.append("  " + report.coverage_note)
        lines.append("")
    if report.regressions:
        lines.append("РЕГРЕССИИ ОТНОСИТЕЛЬНО BASELINE")
        lines += ["  " + r.describe() for r in report.regressions]
        if report.baseline_note:
            # Заметка о baseline не должна прятаться за списком падений:
            # «не сверялись» — это про другие метрики, а не про эти.
            lines += ["", "BASELINE", "  " + report.baseline_note]
    elif report.baseline_note:
        lines.append("BASELINE")
        lines.append("  " + report.baseline_note)
    elif report.baseline_path:
        lines.append("Регрессий относительно baseline нет.")
    else:
        lines.append("Baseline не задан — регрессии не сравнивались.")
    return "\n".join(lines)


def load_baseline(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not path or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def detect_regressions(current: Mapping[str, Any], baseline: Mapping[str, Any],
                       threshold: float = metrics.DEFAULT_RELATIVE_THRESHOLD,
                       ungated: Iterable[str] = (),
                       ) -> List[metrics.Regression]:
    """Сверка flat-метрик прогона с baseline по относительному порогу.

    `ungated` — метрики без вердикта (выборка ниже порога): они не участвуют в
    решении, но остаются в наборах, чтобы их исчезновение из сверки не
    выглядело удалением метрики.
    """
    directions = {name: metrics.LOWER for name in LOWER_IS_BETTER}
    directions.update(baseline.get("directions") or {})
    detector = metrics.RegressionDetector(threshold=threshold, directions=directions)
    base_metrics = baseline.get("metrics") or baseline
    return detector.compare(base_metrics, current, ungated=ungated)


def unverified_metrics(current: Mapping[str, Any],
                       baseline: Mapping[str, Any]) -> List[str]:
    """Метрики, которых сверка не касалась: наборы разошлись в любую сторону.

    `compare` обходит ключи baseline, поэтому метрика, заведённая позже слепка,
    молча выпадала из гейта — и метрика, снятая из прогона, выпадала так же
    молча. Это заметка, а не регрессия: на код выхода она не влияет, но
    «регрессий нет» без неё читается как «всё сверили».
    """
    detector = metrics.RegressionDetector()
    return detector.unverified(baseline.get("metrics") or baseline, current)


def require_live_credentials() -> None:
    """Live-режим без ключа обязан падать честно, а не молча подменять replay."""
    if not os.getenv("GIGACHAT_CREDENTIALS", "").strip():
        raise HarnessError(
            "режим live требует GIGACHAT_CREDENTIALS в окружении (см. .env.example); "
            "для офлайн-оценки запустите --mode replay")


def run(mode: str = "replay", scenarios_spec: str = "all", repeat: int = 1,
        baseline_path: Optional[Path] = None, fixtures_dir: Path = FIXTURES_DIR,
        threshold: float = metrics.DEFAULT_RELATIVE_THRESHOLD,
        accept_ruler_change: bool = False) -> RunReport:
    """Полный прогон харнесса по всем фикстурам выбранных сценариев.

    `accept_ruler_change` — явное принятие смены линейки человеком: сверка с
    baseline по-прежнему не проводится (меры несопоставимы), но прогон получает
    право выйти с нулевым кодом. Без флага смена линейки — ненулевой код выхода
    в `run.py`: иначе «добавили правило» автоматически означало бы «гейт
    выключен», и первый прогон после любого изменения линейки был бы зелёным.
    """
    if mode not in ("replay", "live"):
        raise HarnessError(f"неизвестный режим {mode}: нужен replay или live")
    repeat = max(1, int(repeat))
    chosen = select(scenarios_spec)
    if not chosen:
        raise HarnessError("не выбран ни один сценарий")
    if mode == "live":
        require_live_credentials()

    scenario_ids = [s.id for s in chosen]
    fixtures = load_fixtures(scenario_ids, fixtures_dir=fixtures_dir)
    plans = [f for f in fixtures if f["kind"] == "plan"]
    improves = [f for f in fixtures if f["kind"] == "improve"]
    if mode == "replay" and not plans:
        raise HarnessError(f"в {fixtures_dir} нет plan-фикстур для "
                           + ", ".join(scenario_ids))

    gen_cases: List[GenCase] = []
    improve_cases: List[ImproveCase] = []
    for scenario in chosen:
        scenario_plans = [f for f in plans if f["scenario"] == scenario.id]
        bases: List[Tuple[Optional[Mapping[str, Any]], GenCase]] = []
        if mode == "live":
            # Фикстуры в live — не ответы модели, а слоты прогона. Гонять живой
            # запрос по разу на фикстуру значило бы взвешивать сценарий числом
            # его записей (у warehouse_delivery их три, у product_return две) и
            # платить за это лишними ~20 с на слот.
            for index in range(repeat):
                case = run_generation_case(scenario, None, mode=mode, repeat=index)
                gen_cases.append(case)
                bases.append((None, case))
        else:
            for fixture in scenario_plans:
                for index in range(repeat):
                    case = run_generation_case(scenario, fixture, mode=mode,
                                               repeat=index)
                    gen_cases.append(case)
                    bases.append((fixture, case))

        scenario_improves = [f for f in improves if f["scenario"] == scenario.id]
        for fixture in scenario_improves:
            base_id = fixture.get("base_plan")
            base_plan = fixture_by_id(base_id, fixtures) if base_id else None
            candidates = [b for b in bases
                          if base_plan is None or mode == "live"
                          or b[0] is base_plan]
            base = next((c for _, c in candidates if c.ok), None)
            if base is None:
                improve_cases.append(ImproveCase(
                    scenario=scenario.id, fixture=fixture.get("id", "?"),
                    label=fixture.get("label", ""),
                    quality=fixture.get("quality", "unknown"), mode=mode,
                    error=f"нет базовой схемы (plan-фикстура {base_id or 'любая'} "
                         "не собралась)"))
                continue
            for index in range(repeat):
                improve_cases.append(run_improvement_case(scenario, fixture, base,
                                                          mode=mode, repeat=index))
        if mode == "live":
            # Фикстурные слоты теперь честные, но почти всегда вне цели: живая
            # схема редко болеет ровно тем, чем больна фикстура. Чтобы контур
            # улучшения мерился на живых ответах, к каждой собранной схеме
            # добавляется кейс с претензией по её же фактическим нарушениям.
            for index, (_, base) in enumerate(bases):
                for advice_fixture in _advice_fixtures(scenario, base):
                    improve_cases.append(run_improvement_case(
                        scenario, advice_fixture, base, mode=mode, repeat=index))

    names = [name for name in invariants.ALL_CHECKS
             if any(name in case.checks_xml for case in gen_cases)]
    generation = build_generation_suite(names).run(
        [{"name": c.key, "payload": c} for c in gen_cases])
    improvement = build_improvement_suite().run(
        [{"name": c.key, "payload": c} for c in improve_cases])

    report = RunReport(
        mode=mode, repeat=repeat, scenarios=scenario_ids,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        cases=gen_cases, improve_cases=improve_cases,
        generation=generation, improvement=improvement,
        spread=spread_stats(gen_cases, repeat), regressions=[],
        provenance=provenance.audit(chosen, fixtures),
        cost=cost_stats(gen_cases, improve_cases, mode),
        improve_sample_origin=improve_metric_origins(improve_cases, improvement),
        baseline_path=str(baseline_path) if baseline_path else "",
        coverage_note=coverage_note(gen_cases, mode),
        metric_errors={**generation.errors(), **improvement.errors()},
        under_sampled=[*generation.under_sampled(), *improvement.under_sampled()])
    baseline = load_baseline(baseline_path)
    if baseline:
        # Метрики без вердикта (n ниже порога) не участвуют в решении, но
        # остаются в наборах: их отсутствие печатается отдельной секцией
        # отчёта, а не превращается в «метрику убрали из прогона».
        ungated = {**{name: generation.metrics[name]
                      for name in generation.under_sampled()},
                   **{name: improvement.metrics[name]
                      for name in improvement.under_sampled()}}
        report.under_sampled = [
            f"{name} (n={result.n} < {result.min_n})"
            for name, result in sorted(ungated.items())]
        ruler_change = ruler_note(report.ruler, baseline)
        base_mode = str(baseline.get("mode") or "")
        if ruler_change:
            # Балл считается линейкой: новые правила роняют его на тех же схемах,
            # и «регрессия» здесь — порождение самой сверки, а не контура.
            # Но выключать гейт смена линейки больше не вправе: без явного
            # `--accept-ruler-change` прогон считается непройденным (`run.py`),
            # иначе любое добавление правила делало CI зелёным автоматически.
            report.baseline_note = ruler_change
            report.ruler_change = ruler_change
            report.ruler_change_accepted = bool(accept_ruler_change)
        elif "ruler" not in baseline:
            # Отпечаток заведён позже этого слепка, поэтому судить о линейке
            # прошлых прогонов нечем. Сверку не отменяем (было бы ручное
            # освобождение от гейта), но и выдавать её за полную не можем.
            report.regressions = detect_regressions(report.metrics_flat(),
                                                    baseline, threshold=threshold,
                                                    ungated=ungated)
            report.baseline_note = (
                "в baseline нет отпечатка линейки: он заведён начиная с этого "
                "прогона, а до него правила могли меняться — регрессии балла "
                "могут быть порождением сверки, а не контура")
        elif base_mode and base_mode != mode:
            # Метрики режимов несопоставимы: live гоняет больше кейсов и
            # настоящую модель, replay — записанные ответы. Сравнение дало бы
            # «регрессию» ровно в тот момент, когда контур стал лучше.
            report.baseline_note = (
                f"baseline собран в режиме «{base_mode}», прогон — «{mode}»: "
                "метрики несопоставимы, сверка пропущена. Для live заведите "
                "отдельный baseline (--baseline eval/baselines/live.json) "
                "после 3–5 прогонов, чтобы не ловить шум")
        else:
            report.regressions = detect_regressions(report.metrics_flat(),
                                                    baseline, threshold=threshold,
                                                    ungated=ungated)
            # Отдельная строка, а не регрессия: сравнивать не с чем, поэтому
            # код выхода у неё прав нет — но молчать про неё харнесс не вправе.
            # Сюда попадают и имена, снятые из прогона: их `compare` не видит
            # (он идёт по ключам baseline), и без этой строки удаление четырёх
            # метрик улучшения выглядело бы как «всё сверили, регрессий нет».
            unverified = unverified_metrics(report.metrics_flat(), baseline)
            if unverified:
                note = ("не сверялись с baseline (наборы метрик разошлись): "
                        + ", ".join(unverified))
                report.baseline_note = (f"{report.baseline_note}; {note}"
                                        if report.baseline_note else note)
    return report


def write_report(report: RunReport, reports_dir: Path = REPORTS_DIR) -> Path:
    """JSON-отчёт прогона: `eval/reports/<timestamp>.json`."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / (time.strftime("%Y%m%d-%H%M%S") + ".json")
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zа-яё_.-]+", "_", text)[:120]


def dump_schemes(report: "RunReport", out_dir: Path) -> List[Path]:
    """Схемы прогона на диск — чтобы их можно было открыть глазами.

    Метрика честно говорит «в сцене нет развилки», но не говорит, как схема
    выглядит: пригодный BPMN и «правильный по инвариантам, но нечитаемый»
    различаются только взглядом. Имя несёт итог сцены, поэтому разбирают сначала
    провалы. Улучшение выгружается парой `_before`/`_after` плюс `_report.json`:
    дельта пакета читается только из двух схем рядом, а в отчёте лежит и сам
    пакет (`operations_packages`) — без него отказ «пакет оставил бы дыру»
    неотличим от дыры, которую сделала починка или перенос между пулами.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for case in report.cases:
        if not case.xml:
            continue
        verdict = "pass" if case.scenario_pass else "fail"
        stem = _safe_name(f"{case.key}_r{case.repeat}_{verdict}")
        path = out_dir / f"{stem}.bpmn"
        path.write_text(case.xml, encoding="utf-8")
        written.append(path)
        # План рядом со схемой: по XML не отличить «модель не назвала
        # контрагента» от «назвала, а починка свернула пул в дорожку», а без
        # такого файла каждый разбор стоил отдельного живого прогона.
        # `flows` здесь обязаны быть: «развилка спрятана в подписях потоков»
        # лечится вставкой шлюза только когда ветки уже различимы по
        # `condition`/`default`, и без потоков офлайн неотличимо, чья это работа —
        # контура или модели (прогон #52, has_branching).
        plan = out_dir / f"{stem}.plan.json"
        plan.write_text(json.dumps(
            {"participants": case.structure.get("participants"),
             "actors": case.structure.get("actors"),
             "lanes": case.structure.get("lanes"),
             "elements": [{k: e.get(k) for k in
                           ("id", "kind", "name", "participant", "lane")}
                          for e in (case.structure.get("elements") or [])],
             "flows": [{k: f.get(k) for k in
                        ("id", "source", "target", "kind", "condition",
                         "default", "name", "attached_to")}
                       for f in (case.structure.get("flows") or [])],
             "gaps": case.gaps},
            ensure_ascii=False, indent=1), encoding="utf-8")
        written.append(plan)
    for case in report.improve_cases:
        if not (case.base_xml or case.xml_after):
            continue
        stem = _safe_name(f"improve_{case.key}_r{case.repeat}_"
                          f"{improve_verdict(case)}")
        # Пара before/after, а не одна «после»: «пакет применился и стало лучше»
        # офлайн не проверяется — читатель обязан видеть, какие узлы, потоки и
        # описания появились, и сравнить их без повторного прогона.
        if case.base_xml:
            before = out_dir / f"{stem}_before.bpmn"
            before.write_text(case.base_xml, encoding="utf-8")
            written.append(before)
        if case.xml_after:
            after = out_dir / f"{stem}_after.bpmn"
            after.write_text(case.xml_after, encoding="utf-8")
            written.append(after)
        # Отчёт рядом со схемой: вердикт в имени файла говорит «что читать»,
        # файл ниже — «почему» (факты применения, отказы по стадиям и то, что
        # харнесс не смог померить).
        report_path = out_dir / f"{stem}_report.json"
        report_path.write_text(json.dumps(case.as_dict(), ensure_ascii=False,
                                          indent=1), encoding="utf-8")
        written.append(report_path)
    return written


def improve_verdict(case: ImproveCase) -> str:
    """Итог пакета для имени файла: худший из исходов называется первым.

    `pass`/`fail` генерации тут не хватает: откатанный пакет и пакет, который
    сломал проходившее правило, — разные очереди работы, и разбирать их надо с
    разных файлов.
    """
    if case.package_reverted:
        return "revert"
    if case.rules_regressed:
        return "regress"
    return "pass" if case.pass_after else "fail"

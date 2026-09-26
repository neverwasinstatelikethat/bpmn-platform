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
    "spread/score_cv", "spread/score_cv_per_case",
})

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
QUALITY_BAR = 0.8
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
                     ) -> Tuple[Dict[str, Any], List[str]]:
    """Починка структуры устойчива к смене сигнатуры: кортеж (структура,
    пометки) или только структура — разбираются оба варианта.

    `trace` — сборщик отпечатка шагов починки для атрибуции дефектов. Его
    передают ядру только если функция починки действительно принимает такой
    параметр: харнесс не фиксирует под собой сигнатуру, а спрашивает её.
    """
    fn = _resolve("core.bpmn_generator", "repair_structure", "repair_plan",
                  "normalize_structure")
    if fn is None:
        raise HarnessError("в core/bpmn_generator нет функции починки структуры "
                           "(repair_structure/repair_plan)")
    args: List[Any] = [dict(plan)]
    if trace is not None and "trace" in _signature_names(fn):
        args.append(trace)
    result = fn(*args)
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
            f"{current.get('weights_total')}; {'; '.join(parts)}): балл и доли "
            "прохождения несопоставимы, сверка с baseline пропущена. Новые "
            "правила обязаны заряжать то, что раньше прощалось, — проверьте это "
            "на корпусе (`python -m eval.coverage`) и пересоберите baseline "
            "--write-baseline")


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
        """
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
        """
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
            "targets": self.targets, "off_target": self.off_target,
            "off_target_note": self.off_target_note,
        }


# ---------------------------------------------------------------------------
# прогоны
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def llm_call_counter() -> Iterator[Dict[str, int]]:
    """Счётчик обращений к транспорту LLM — только для live-режима.

    Число ретраев на задачу измеряется на уровне транспорта
    (`core.llm_client._complete`), а не переписыванием ядра: харнесс наблюдает
    существующий код, а не подменяет его логику.
    """
    from core import llm_client

    original = llm_client._complete
    state = {"calls": 0}

    def counting(messages: Sequence[Mapping[str, str]], temperature: float,
                 max_tokens: Optional[int]) -> str:
        state["calls"] += 1
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
        else:
            if not fixture or "plan" not in fixture:
                raise _CaseFailure("у фикстуры нет поля plan")
            gaps = _plan_gaps(fixture["plan"], scenario.text)
            steps: List[Dict[str, Any]] = []
            structure, notes = repair_structure(fixture["plan"], steps)
            # Трейс фикстуры честен ровно наполовину: кто прислал план — модель
            # или автор фикстуры — харнесс не знает, поэтому владельцем отсутствия
            # считается сама фикстура (атрибуция решает это по пустому трейсу).
            case.trace = [{"node": "починка структуры", "steps": steps}]
            xml = generate_xml(structure)
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
                       mode=mode, repeat=repeat)
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
            xml_after, report, case.llm_calls = _live_improve(
                base.xml, fixture.get("prompt", "") or scenario.improve_prompt)
        else:
            xml_after, report = _replay_improve(base.xml, fixture)
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


def _live_improve(xml: str,
                  prompt: str) -> Tuple[str, Dict[str, Any], int]:
    """Живое улучшение штатным оркестратором (асинхронным — прогоняем sync).

    Отчёт забирается целиком: факты о повторе, об откате пакета и о регрессе
    правил считает оркестратор, и выводить их здесь из стадий отказов значило бы
    измерять догадку харнесса вместо контура.
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
            structure, _notes = repair_structure(fixture["plan"], [])
            checks = invariants.check_xml(generate_xml(structure),
                                          scenario.expectations())
        except Exception:  # noqa: BLE001 — нечитаемый эталон снимает сверку, не прогон
            continue
        out[fixture["scenario"]] = len(invariants.business_smells(checks))
    return out


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
    suite.metric("business/smells",
                 lambda case: None if not case.ok
                 else float(len(case.business_smells)),
                 description="бизнес-узких мест по оракулу в итоговой схеме",
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
                 description="повторные обращения к модели на задачу",
                 direction=metrics.LOWER)
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
    # `noop_rows` считается до откатa (это свойство пакета, а не схемы), поэтому
    # после сверки отчёта он может превысить число оставшихся строк.
    return _share(max(0, len(case.applied) - case.noop_rows), total)


def _noop_share(case: ImproveCase) -> Optional[float]:
    """Доля «применённых» строк, не изменивших схему."""
    return _share(case.noop_rows, len(case.applied))


def _retry_gain(case: ImproveCase) -> Optional[float]:
    """Сколько отказов первого раунда закрыл корректирующий повтор.

    Выборка — только кейсы с состоявшимся повтором: на остальных метрика
    физически не определена, и нули там только дешевле выглядели бы.
    """
    if case.retry_attempted is not True or case.retry_closed is None:
        return None
    return float(case.retry_closed)


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
    """Инварианты, пройденные на базовой схеме и упавшие после пакета.

    Вторая половина пары к `defects_repaired` и честная замена `no_regression`:
    итоговый балл не отличает «одно правило починили, другое сломали, дельта 0»
    от «ничего не тронули», а счётчик упавших — отличает.
    """
    if not case.checks_before or not case.checks_after:
        return None
    broken = {c.name for c in invariants.applicable_checks(case.checks_after)
              if not c.ok}
    return float(sum(1 for c in invariants.applicable_checks(case.checks_before)
                     if c.ok and c.name in broken))


def _base_advice_drift(case: "ImproveCase") -> Optional[float]:
    """Сколько бизнес-правил два слоя оценили по-разному на БАЗОВОЙ схеме.

    Это число подсказок, по которым пакет строился вслепую: скоринг объявил
    дефект, которого оракул не видит (и тогда правка ушла в `skipped` или в
    несуществующее узкое место), или наоборот — оракул нашёл узкое место, о
    котором модель не услышала и не починила. Ноль здесь и есть гарантия, что
    `defects_repaired` и `op_acceptance` меряют один и тот же список дефектов.
    None, если сравнивать было нечего (кейс без базовой схемы или без одного
    из слоёв)."""
    if not case.ok or not case.drift_before:
        return None
    return float(len(invariants.business_disagreements(case.drift_before)))


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
    suite.metric("improve/retry_gain", _retry_gain,
                 description="сколько отказов первого раунда закрыл повтор "
                             "(только кейсы с состоявшимся повтором)", unit="правка")
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
    suite.metric("improve/business_repaired_share",
                 lambda case: case.business_repaired_share,
                 description="то же по одному бизнес-слою: узкие места процесса, "
                             "а не валидность XML")
    suite.metric("improve/base_advice_drift", _base_advice_drift,
                 direction=metrics.LOWER, unit="правило",
                 description="бизнес-правил, где оракул и скоринг разошлись на "
                             "базовой схеме: по такой подсказке пакет не обязан "
                             "улучшать процесс")
    suite.metric("improve/defects_introduced", _defects_introduced,
                 direction=metrics.LOWER, unit="инвариант",
                 description="инварианты, прошедшие до пакета и упавшие после")
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


def spread_stats(cases: Sequence[GenCase]) -> Dict[str, Optional[float]]:
    """Разброс балла между повторами — ровно та проблема живого прогона
    («87 → 94» против «85 → 85» на одном запросе)."""
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
    return {
        "runs": float(len(scores)),
        "score_p50": metrics.p50(scores),
        "score_p95": metrics.p95(scores),
        "score_cv": metrics.coefficient_of_variation(scores),
        "score_cv_per_case": metrics.mean(per_case_cv) if per_case_cv else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
    }


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
    ruler: Dict[str, Any] = field(default_factory=ruler_fingerprint)
    provenance: Dict[str, Any] = field(default_factory=dict)

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
        baseline нельзя, иначе смена `--repeat` выглядела бы как регрессия.
        """
        data = self.generation.flat()
        data.update(self.improvement.flat())
        data.update({f"spread/{name}": value
                     for name, value in self.spread.items() if name != "runs"})
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
        out["spread/score_cv"] = metrics.LOWER
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
            "ruler": self.ruler,
            "baseline": self.baseline_path,
            "baseline_note": self.baseline_note,
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
        f"{len(report.scenarios)}, кейсов {len(report.cases)}", "", "МЕТРИКИ",
        f"{'метрика':<38}{'значение':>12}{'n':>5}  описание"]
    for suite in (report.generation, report.improvement):
        for name in suite.order:
            result = suite.metrics[name]
            lines.append(f"{name:<38}{_fmt(result.mean):>12}{result.n:>5}  "
                         f"{result.description}")
    lines += ["", "РАЗБРОС БАЛЛА ПО ПОВТОРАМ"]
    for name in ("runs", "score_p50", "score_p95", "score_cv", "score_cv_per_case",
                 "score_min", "score_max"):
        lines.append(f"  {name:<20}{_fmt(report.spread.get(name))}")
    lines += ["", "ГЕНЕРАЦИЯ: КЕЙСЫ"]
    for case in report.cases:
        state = "OK " if case.scenario_pass else "FAIL"
        lines.append(f"  [{state}] {case.key} ({case.quality}) — балл {_fmt(case.score)}, "
                     f"пулов {case.pools}, элементов {case.elements}, "
                     f"правок починки {case.repairs}")
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
                     f"{_fmt(case.score_before)} → {_fmt(case.score_after)}")
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
                       ) -> List[metrics.Regression]:
    """Сверка flat-метрик прогона с baseline по относительному порогу."""
    directions = {name: metrics.LOWER for name in LOWER_IS_BETTER}
    directions.update(baseline.get("directions") or {})
    detector = metrics.RegressionDetector(threshold=threshold, directions=directions)
    base_metrics = baseline.get("metrics") or baseline
    return detector.compare(base_metrics, current)


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
        threshold: float = metrics.DEFAULT_RELATIVE_THRESHOLD) -> RunReport:
    """Полный прогон харнесса по всем фикстурам выбранных сценариев."""
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
        spread=spread_stats(gen_cases), regressions=[],
        provenance=provenance.audit(chosen, fixtures),
        baseline_path=str(baseline_path) if baseline_path else "",
        coverage_note=coverage_note(gen_cases, mode))
    baseline = load_baseline(baseline_path)
    if baseline:
        ruler_change = ruler_note(report.ruler, baseline)
        base_mode = str(baseline.get("mode") or "")
        if ruler_change:
            # Балл считается линейкой: новые правила роняют его на тех же схемах,
            # и «регрессия» здесь — порождение самой сверки, а не контура.
            report.baseline_note = ruler_change
        elif "ruler" not in baseline:
            # Отпечаток заведён позже этого слепка, поэтому судить о линейке
            # прошлых прогонов нечем. Сверку не отменяем (было бы ручное
            # освобождение от гейта), но и выдавать её за полную не можем.
            report.regressions = detect_regressions(report.metrics_flat(),
                                                    baseline, threshold=threshold)
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
                                                   baseline, threshold=threshold)
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
    дельта пакета читается только из двух схем рядом.
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

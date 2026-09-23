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
from .scenarios import Scenario, select

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
BASELINES_DIR = EVAL_DIR / "baselines"
REPORTS_DIR = EVAL_DIR / "reports"
DEFAULT_BASELINE = BASELINES_DIR / "current.json"

# Метрики, у которых «меньше — лучше»: без этой пометки детектор регрессий
# считал бы ухудшением любой рост задержки или числа правок.
LOWER_IS_BETTER = frozenset({
    "repairs_per_scheme", "latency_ms", "llm_retries", "generation_error_share",
    "improve/skipped_share", "spread/score_cv", "spread/score_cv_per_case",
})


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
    """Имена параметров функции ([] уbuilt-in без сигнатуры)."""
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


def apply_operations(xml: str, operations: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    fn = getattr(_module("core.bpmn_edits"), "apply_operations", None)
    if fn is None:
        raise HarnessError("в core/bpmn_edits нет apply_operations")
    return fn(xml, operations)


def validate_and_repair(xml: str) -> Tuple[str, List[str]]:
    fn = getattr(_module("core.bpmn_edits"), "validate_and_repair", None)
    if fn is None:
        return xml, []
    result = fn(xml)
    if isinstance(result, tuple):
        return result[0], list(result[1] or [])
    return xml, []


def unrouted_notes(notes: Sequence[str]) -> List[str]:
    """Пометки починки об узлах вне маршрута — маркеры берёт аплайер."""
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
        """Сводный исход: все применимые инварианты по итоговому XML пройдены."""
        if not self.checks_xml:
            return None
        return all(c.ok for c in invariants.applicable_checks(self.checks_xml))

    def measure(self, expectations: Mapping[str, Any]) -> None:
        """Инварианты по структуре и XML + балл скоринга."""
        self.checks_structure = invariants.check_structure(self.structure, expectations)
        if not self.xml:
            return
        self.checks_xml = invariants.check_xml(self.xml, expectations)
        evaluation = score_xml(self.xml)
        self.score = evaluation.get("score")
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
    """Один прогон улучшения: пакет операций поверх схемы генерации."""

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
    retried: bool = False
    score_before: Optional[float] = None
    score_after: Optional[float] = None
    checks_before: Dict[str, Check] = field(default_factory=dict)
    checks_after: Dict[str, Check] = field(default_factory=dict)
    attribution: Dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    llm_calls: int = 0
    xml_after: str = ""

    @property
    def key(self) -> str:
        return f"{self.scenario}/{self.fixture}"

    @property
    def applied_share(self) -> Optional[float]:
        total = len(self.applied) + len(self.skipped)
        return None if total == 0 else len(self.applied) / total

    @property
    def score_delta(self) -> Optional[float]:
        if self.score_before is None or self.score_after is None:
            return None
        return self.score_after - self.score_before

    @property
    def pass_after(self) -> Optional[bool]:
        if not self.checks_after:
            return None
        return all(c.ok for c in invariants.applicable_checks(self.checks_after))

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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario, "fixture": self.fixture, "label": self.label,
            "quality": self.quality, "mode": self.mode, "repeat": self.repeat,
            "ok": self.ok, "error": self.error,
            "applied": len(self.applied), "skipped": len(self.skipped),
            "applied_share": self.applied_share, "corrective_retry": self.retried,
            "score_before": self.score_before, "score_after": self.score_after,
            "score_delta": self.score_delta,
            "skipped_details": self.skipped, "applied_details": self.applied,
            "repair_notes": self.repair_notes,
            "attribution": self.attribution,
            "pass_after": self.pass_after,
            "repaired_share": self.repaired_share,
            "summary_after": invariants.summarize(self.checks_after)
            if self.checks_after else {},
            "checks_after": {k: v.as_dict() for k, v in self.checks_after.items()},
            # Базовая схема рядом с итоговой: без неё прогон не отвечает на
            # вопрос, унаследован провал от генерации или его принёс пакет.
            "summary_before": invariants.summarize(self.checks_before)
            if self.checks_before else {},
            "checks_before": {k: v.as_dict() for k, v in self.checks_before.items()},
            "latency_ms": round(self.latency_ms, 1), "llm_calls": self.llm_calls,
        }


# ---------------------------------------------------------------------------
# прогоны
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def llm_call_counter() -> Iterator[Dict[str, int]]:
    """Счётчик обращений к транспорту LLM — только для live-режима.

    Число ретраев на задачу измеряется на уровне транспорта
    (`core.llm_client._complete`), а не переписыванием ядра: харнесс观察ает
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


def _apply_package(xml: str, operations: Sequence[Mapping[str, Any]],
                   stage: str) -> Tuple[str, List[Dict[str, Any]],
                                        List[Dict[str, Any]], List[str]]:
    """Пакет операций + починка после применения — как в живом контуре."""
    xml_after, report = apply_operations(xml, [dict(op) for op in operations])
    applied = [dict(a, stage=stage) for a in (report.get("applied") or [])]
    skipped = [dict(s, stage=stage) for s in (report.get("skipped") or [])]
    xml_after, notes = validate_and_repair(xml_after)
    return xml_after, applied, skipped, notes


def run_improvement_case(scenario: Scenario, fixture: Mapping[str, Any],
                         base: GenCase, mode: str = "replay",
                         repeat: int = 0) -> ImproveCase:
    """Один кейс улучшения поверх схемы `base`.

    В replay корректирующий раунд берётся из фикстуры (`retry_operations`):
    транспорт модели не дёргаем, но путь «план → применение → повтор → починка»
    проходит настоящий аплайер.
    """
    case = ImproveCase(scenario=scenario.id, fixture=fixture.get("id", "?"),
                       label=fixture.get("label", ""),
                       quality=fixture.get("quality", "unknown"),
                       mode=mode, repeat=repeat)
    expectations = scenario.expectations()
    if not base.ok or not base.xml:
        case.error = f"нет базовой схемы: {base.error or 'генерация не собралась'}"
        return case
    started = time.perf_counter()
    try:
        case.checks_before = invariants.check_xml(base.xml, expectations)
        case.score_before = score_xml(base.xml).get("score")
        if mode == "live":
            (case.xml_after, case.applied, case.skipped, case.repair_notes,
             case.retried, case.llm_calls) = _live_improve(
                base.xml, fixture.get("prompt", "") or scenario.improve_prompt)
        else:
            operations = list(fixture.get("operations") or [])
            if not operations:
                raise _CaseFailure("в фикстуре нет операций")
            xml, applied, skipped, notes = _apply_package(base.xml, operations, "plan")
            retried = False
            retry_ops = list(fixture.get("retry_operations") or [])
            if (skipped or unrouted_notes(notes)) and retry_ops:
                # Корректирующий раунд: причины первого раунда сохраняются,
                # новые помечаются «retry» — как в живом отчёте применения.
                retried = True
                xml, more_applied, more_skipped, more_notes = _apply_package(
                    xml, retry_ops, "retry")
                applied += more_applied
                skipped += more_skipped
                notes = notes + more_notes
            case.xml_after, case.applied, case.skipped = xml, applied, skipped
            case.repair_notes, case.retried = notes, retried
        if case.xml_after:
            case.checks_after = invariants.check_xml(case.xml_after, expectations)
            case.attribution = attribution.attribute_improvement(
                case.checks_before, case.checks_after, case.applied, case.skipped)
            case.score_after = score_xml(case.xml_after).get("score")
        case.ok = bool(case.applied)
    except HarnessError:
        raise
    except _CaseFailure as e:
        case.error = str(e)
    except Exception as e:  # noqa: BLE001 — падение аплайера тоже исход сцены
        case.error = f"{type(e).__name__}: {e}"
    case.latency_ms = (time.perf_counter() - started) * 1000
    return case


def _live_improve(xml: str, prompt: str) -> Tuple[str, List[Dict[str, Any]],
                                                  List[Dict[str, Any]], List[str],
                                                  bool, int]:
    """Живое улучшение штатным оркестратором (асинхронным — прогоняем sync)."""
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
    report = report or {}
    retried = any(s.get("stage") == "retry" for s in report.get("skipped") or [])
    return (xml_after or "", list(report.get("applied") or []),
            [dict(s) for s in (report.get("skipped") or [])],
            list(report.get("repair_notes") or []), retried, counter["calls"])


# ---------------------------------------------------------------------------
# метрики прогона
# ---------------------------------------------------------------------------


def _check_value(case: GenCase, name: str) -> Optional[float]:
    check: Optional[Check] = case.checks_xml.get(name)
    if check is None or not check.applicable:
        return None
    return 1.0 if check.ok else 0.0


def build_generation_suite(names: Sequence[str]) -> metrics.EvaluationSuite:
    """Метрики генерации: pass@1 по инвариантам и сводно, правки починки, балл,
    задержка и ретраи."""
    suite = metrics.EvaluationSuite()
    for name in names:
        suite.metric(f"pass@1/{name}", lambda case, n=name: _check_value(case, n),
                     description=f"инвариант «{name}» по итоговому XML")
    suite.metric("pass@1/scenario",
                 lambda case: None if case.scenario_pass is None
                 else float(case.scenario_pass),
                 description="все применимые инварианты сцены пройдены (итоговый XML)")
    suite.metric("pass@1/structure",
                 lambda case: None if not case.checks_structure else float(
                     all(c.ok for c in invariants.applicable_checks(
                         case.checks_structure))),
                 description="то же по починенной структуре (до генерации XML)")
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


def build_improvement_suite() -> metrics.EvaluationSuite:
    """Метрики улучшения: доля применённых операций, потребность в повторе,
    дельта балла и сохранность инвариантов после применения."""
    suite = metrics.EvaluationSuite()
    suite.metric("improve/applied_share", lambda case: case.applied_share,
                 description="доля применённых операций (applied/(applied+skipped))")
    suite.metric("improve/skipped_share",
                 lambda case: None if case.applied_share is None
                 else 1.0 - case.applied_share,
                 description="доля отклонённых операций", direction=metrics.LOWER)
    suite.metric("improve/retry_needed_share", lambda case: float(case.retried),
                 description="доля пакетов, потребовавших корректирующего повтора")
    suite.metric("improve/score_delta", lambda case: case.score_delta,
                 description="дельта балла после применения пакета", unit="балл")
    suite.metric("improve/pass@1", lambda case: case.pass_after,
                 description="инварианты улучшенной схемы")
    suite.metric("improve/defects_repaired", lambda case: case.repaired_share,
                 description="доля дефектов базовой схемы, которые пакет убрал")
    suite.metric("improve/no_regression",
                 lambda case: None if case.score_delta is None
                 else float(case.score_delta >= 0),
                 description="улучшение не испортило схему")
    suite.metric("improve/error_share", lambda case: 0.0 if case.ok else 1.0,
                 description="доля пакетов, которые не удалось применить",
                 direction=metrics.LOWER)
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
    provenance: Dict[str, Any] = field(default_factory=dict)

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
            "baseline": self.baseline_path,
            "baseline_note": self.baseline_note,
            "regressions": [r.as_dict() for r in self.regressions],
            "provenance": self.provenance,
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
    lines += ["", "УЛУЧШЕНИЕ: КЕЙСЫ"]
    if not report.improve_cases:
        lines.append("  нет improve-фикстур для выбранных сценариев")
    for case in report.improve_cases:
        total = len(case.applied) + len(case.skipped)
        lines.append(f"  [{('OK ' if case.ok else 'FAIL')}] {case.key} — применено "
                     f"{len(case.applied)}/{total}, повтор "
                     f"{'нужен' if case.retried else 'не нужен'}, балл "
                     f"{_fmt(case.score_before)} → {_fmt(case.score_after)}")
        if case.error:
            lines.append(f"        ошибка: {case.error}")
        for skip in case.skipped:
            lines.append(f"        ⊘ {skip.get('stage', 'план')}/{skip.get('op')}: "
                         f"{skip.get('reason')}")
        for failure in invariants.summarize(case.checks_after).get("failed", []):
            owner = case.attribution.get(failure["name"], "")
            lines.append(f"        ✗ {failure['name']}"
                         + (f" → виноват {owner}" if owner else "")
                         + f": {failure['reason']}")
    lines += ["", "КТО ПОРОДИЛ ДЕФЕКТЫ"]
    lines += ["  " + line for line in attribution.format_tally(
        attribution.tally(*[c.attribution for c in report.cases],
                          *[c.attribution for c in report.improve_cases]))]
    lines += ["", "ПРОВЕНАНС КЕЙСОВ"]
    # Метрика стоит ровно столько, сколько стоит её набор: кейс, ответ которого
    # есть в few-shot промпта, мерит копирование, а не контур.
    if not report.provenance:
        lines.append("  разбор не выполнялся")
    else:
        lines += ["  " + line
                  for line in provenance.format_findings(report.provenance)]
    lines.append("")
    if report.regressions:
        lines.append("РЕГРЕССИИ ОТНОСИТЕЛЬНО BASELINE")
        lines += ["  " + r.describe() for r in report.regressions]
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
        baseline_path=str(baseline_path) if baseline_path else "")
    baseline = load_baseline(baseline_path)
    if baseline:
        base_mode = str(baseline.get("mode") or "")
        if base_mode and base_mode != mode:
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
    провалы.
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
        if not case.xml_after:
            continue
        path = out_dir / _safe_name(
            f"improve_{case.scenario}_{case.repeat}_"
            f"{case.score_before}->{case.score_after}.bpmn")
        path.write_text(case.xml_after, encoding="utf-8")
        written.append(path)
    return written

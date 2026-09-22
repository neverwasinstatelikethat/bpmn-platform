"""Метрики, порог и детектор регрессий для офлайн-оценки ИИ-контура.

Арифметика написана вручную (без numpy/scipy): усреднение, перцентили и
коэффициент вариации нужны в четырёх, и держать ради них тяжёлую зависимость
в контуре оценки — лишний источник несовпадения версий.

Ключевое разделение: `Metric` умеет доставать значение из одного примера
(прогона сценария), `EvaluationSuite` считает метрики × примеры и отдаёт
среднее со списком raw-значений, `RegressionDetector` сравнивает два набора
значений по относительному порогу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

# Доля относительного падения, с которой правка считается регрессией. 5%:
# прогон живой модели шумит сильнее, а детерминированный replay даёт ровно те
# же числа, поэтому порог не «съедает» настоящие изменения.
DEFAULT_RELATIVE_THRESHOLD = 0.05

HIGHER = "higher"
LOWER = "lower"


def mean(values: Sequence[float]) -> Optional[float]:
    values = [float(v) for v in values]
    if not values:
        return None
    return sum(values) / len(values)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Перцентиль с линейной интерполяцией между соседними порядковыми
    значениями (тот же приём, что `numpy.percentile(..., method='linear')`)."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (float(q) / 100.0)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    frac = position - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def p50(values: Sequence[float]) -> Optional[float]:
    return percentile(values, 50)


def p95(values: Sequence[float]) -> Optional[float]:
    return percentile(values, 95)


def pstdev(values: Sequence[float]) -> Optional[float]:
    """СКО по генеральной совокупности: повтор задачи — это и есть вся
    выборка, поправок на «несмещённость» тут не нужно."""
    values = [float(v) for v in values]
    if not values:
        return None
    mu = mean(values) or 0.0
    return (sum((v - mu) ** 2 for v in values) / len(values)) ** 0.5


def coefficient_of_variation(values: Sequence[float]) -> Optional[float]:
    """Разброс балла между повторами одной задачи: СКО / среднее.

    Относительная величина принципиальна: 4 балла на схеме со 100 и 4 балла
    на схеме со 40 — разные сигналы. None — когда считать нечего (один повтор
    или нулевое среднее)."""
    values = [float(v) for v in values]
    if len(values) < 2:
        return None
    mu = mean(values)
    if mu in (None, 0.0):
        return None
    std = pstdev(values)
    return None if std is None else std / abs(mu)


@dataclass
class Metric:
    """Именованная метрика: как достать число из примера прогона.

    `fn` возвращает None, когда метрика к примеру неприменима (например,
    таймера в схеме нет). Такие примеры не входят в знаменатель среднего:
    иначе pass@1 по таймеру занижался бы схемами, где проверять нечего.
    """

    name: str
    fn: Callable[[Any], Optional[float]]
    description: str = ""
    direction: str = HIGHER
    unit: str = ""

    def value_of(self, example: Any) -> Optional[float]:
        return self.fn(example)


@dataclass
class MetricResult:
    """Метрика, усреднённая по примерам, плюс raw — без него прогон нельзя
    разобрать по конкретному сценарию."""

    name: str
    mean: Optional[float] = None
    values: List[Optional[float]] = field(default_factory=list)
    description: str = ""
    direction: str = HIGHER
    unit: str = ""

    @property
    def n(self) -> int:
        return len([v for v in self.values if v is not None])

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mean": self.mean,
            "n": self.n,
            "values": self.values,
            "description": self.description,
            "direction": self.direction,
            "unit": self.unit,
        }

    @property
    def display(self) -> str:
        return "н/д" if self.mean is None else f"{self.mean:.3f}"


@dataclass
class SuiteResult:
    """Ответ suite: метрики (в порядке регистрации) и таблица примеров."""

    order: List[str] = field(default_factory=list)
    metrics: Dict[str, MetricResult] = field(default_factory=dict)
    per_example: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"metrics": {name: self.metrics[name].as_dict() for name in self.order},
                "per_example": self.per_example}

    def values(self, name: str) -> List[float]:
        result = self.metrics.get(name)
        if result is None:
            return []
        return [v for v in result.values if v is not None]

    def flat(self) -> Dict[str, Optional[float]]:
        """Только средние — ровно то, что кладётся в baseline."""
        return {name: self.metrics[name].mean for name in self.order}


class EvaluationSuite:
    """Метрики × примеры → среднее + raw."""

    def __init__(self, metrics: Optional[Iterable[Metric]] = None) -> None:
        self.metrics: List[Metric] = list(metrics or [])

    def add(self, metric: Metric) -> Metric:
        self.metrics.append(metric)
        return metric

    def metric(self, name: str, fn: Callable[[Any], Optional[float]],
               description: str = "", direction: str = HIGHER,
               unit: str = "") -> Metric:
        return self.add(Metric(name=name, fn=fn, description=description,
                               direction=direction, unit=unit))

    def run(self, examples: Sequence[Mapping[str, Any]]) -> SuiteResult:
        result = SuiteResult(order=[m.name for m in self.metrics],
                             metrics={m.name: MetricResult(
                                 name=m.name, description=m.description,
                                 direction=m.direction, unit=m.unit)
                                 for m in self.metrics})
        for example in examples:
            label = str(example.get("name") or "?")
            row: Dict[str, Optional[float]] = {}
            for metric in self.metrics:
                try:
                    value = metric.value_of(example.get("payload"))
                except Exception as e:  # noqa: BLE001 — метрика не роняет прогон
                    # Падение одной метрики — дыра в харнессе, а не в схеме:
                    # фиксируем её в описании результата и продолжаем считать.
                    value = None
                    if not result.metrics[metric.name].description.startswith("ОШИБКА"):
                        result.metrics[metric.name].description = (
                            f"ОШИБКА метрики: {e!r}")
                row[metric.name] = value
                result.metrics[metric.name].values.append(value)
            result.per_example[label] = row
        for name in result.order:
            metric_result = result.metrics[name]
            metric_result.mean = mean(
                [v for v in metric_result.values if v is not None])
        return result


@dataclass
class Regression:
    """Падение метрики относительно baseline сверх порога."""

    name: str
    baseline: float
    current: float
    rel_change: float
    threshold: float
    direction: str = HIGHER

    @property
    def absolute(self) -> float:
        return self.current - self.baseline

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "baseline": self.baseline,
                "current": self.current, "delta": self.absolute,
                "rel_change": self.rel_change, "threshold": self.threshold,
                "direction": self.direction}

    def describe(self) -> str:
        arrow = "↓" if self.direction == HIGHER else "↑"
        return (f"{self.name} {arrow}: {self.baseline:.3f} → {self.current:.3f} "
                f"({self.rel_change * 100:+.1f}%, порог "
                f"{self.threshold * 100:.1f}%)")


def _as_number(value: Any) -> Optional[float]:
    """Baseline хранит либо число, либо словарь MetricResult — принимаем оба."""
    if isinstance(value, Mapping):
        value = value.get("mean", value.get("value"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class RegressionDetector:
    """baseline vs current по относительному порогу.

    `directions` задаёт, что для метрики «лучше»: по умолчанию выше = лучше,
    для задержки, ретраев и числа правок — ниже. Без этого «улучшение»
    latency выглядело бы регрессией, и детектор начал бы шуметь.
    """

    threshold: float = DEFAULT_RELATIVE_THRESHOLD
    directions: Dict[str, str] = field(default_factory=dict)

    def direction_of(self, name: str) -> str:
        return self.directions.get(name, HIGHER)

    def compare(self, baseline: Mapping[str, Any],
                current: Mapping[str, Any]) -> List[Regression]:
        out: List[Regression] = []
        for name, raw_base in baseline.items():
            if name not in current:
                continue
            base, now = _as_number(raw_base), _as_number(current[name])
            if base is None or now is None:
                # Метрику не с чем сравнивать: в одном из прогонов она не
                # померена (в выбранном подмножестве сценариев не было
                # improve-кейсов). Это «нет данных», а не «лучше» и не «хуже».
                continue
            if base == 0.0:
                # ноль в baseline: относительная доля бессмысленна,
                # сравниваем только явное ухудшение с абсолютным порогом
                if now != 0.0:
                    worse = (now > 0) if self.direction_of(name) == HIGHER else (now < 0)
                    if worse:
                        out.append(Regression(name=name, baseline=base, current=now,
                                              rel_change=1.0, threshold=self.threshold,
                                              direction=self.direction_of(name)))
                continue
            direction = self.direction_of(name)
            if direction == HIGHER:
                rel = (base - now) / abs(base)
            else:
                rel = (now - base) / abs(base)
            if rel > self.threshold:
                out.append(Regression(name=name, baseline=base, current=now,
                                      rel_change=rel, threshold=self.threshold,
                                      direction=direction))
        return out

    def regressions(self, baseline: Mapping[str, Any],
                    current: Mapping[str, Any]) -> List[str]:
        """Список упавших метрик — то, что ждёт CI."""
        return [r.describe() for r in self.compare(baseline, current)]

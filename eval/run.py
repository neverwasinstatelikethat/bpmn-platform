"""CLI офлайн-оценки ИИ-контура.

    python -m eval.run --mode replay --scenarios all --repeat 1 \
        --baseline eval/baselines/current.json [--write-baseline]

Выход: таблица метрик и провалов в stdout + JSON-отчёт в `eval/reports/<timestamp>.json`.
`--mode live` читает GIGACHAT_CREDENTIALS из окружения и без него честно падает
(код 2): тихая подмена live'а на replay иначе выглядела бы как «зелёный» прогон.
Код 1 — по `--fail-on-regression`, если относительно baseline что-то упало: CI
так и должен differить «правку промпта, которая ухудшила контур».
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import harness, metrics
from .harness import DEFAULT_BASELINE, HarnessError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run",
        description="Офлайн-оценка ИИ-контура BPMN: генерация и улучшение "
                    "против независимых инвариантов и детектора регрессий.")
    parser.add_argument("--mode", choices=("replay", "live"), default="replay",
                        help="replay — записанные ответы модели, live — GigaChat")
    parser.add_argument("--scenarios", default="all",
                        help="`all` либо список id сценариев через запятую")
    parser.add_argument("--repeat", type=int, default=1,
                        help="число прогонов каждой сцены (для live показывает разброс)")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                        help="путь к baseline; пусто или отсутствующий файл — "
                             "без сверки")
    parser.add_argument("--write-baseline", action="store_true",
                        help="записать результаты прогона как новый baseline")
    parser.add_argument("--baseline-path", default=str(DEFAULT_BASELINE),
                        help="куда писать baseline при --write-baseline")
    parser.add_argument("--threshold", type=float,
                        default=metrics.DEFAULT_RELATIVE_THRESHOLD,
                        help="относительный порог регрессии (0.05 = 5%%)")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="возвращать код 1, если детектор нашёл падение метрик")
    parser.add_argument("--no-report", action="store_true",
                        help="не писать JSON-отчёт в eval/reports")
    parser.add_argument("--fixtures-dir", default=str(harness.FIXTURES_DIR),
                        help="каталог фикстур (по умолчанию eval/fixtures)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = harness.run(
            mode=args.mode, scenarios_spec=args.scenarios, repeat=args.repeat,
            baseline_path=_baseline_path(args), fixtures_dir=Path(args.fixtures_dir),
            threshold=args.threshold)
    except HarnessError as e:
        print(f"ХАРНЕСС: {e}", file=sys.stderr)
        return 2
    except KeyError as e:  # неизвестный сценарий из --scenarios
        print(f"ХАРНЕСС: {e.args[0]}", file=sys.stderr)
        return 2

    print(harness.render_table(report))
    if not args.no_report:
        path = harness.write_report(report)
        print(f"\nОтчёт: {path.relative_to(Path(__file__).resolve().parents[1])}")
    if args.write_baseline:
        path = report.write_baseline(Path(args.baseline_path))
        print(f"Baseline записан: {path}")
    if report.regressions and args.fail_on_regression:
        return 1
    return 0


def _baseline_path(args: argparse.Namespace) -> Optional[Path]:
    """Baseline для сверки: не сравниваем, если он отключён или его ещё нет —
    первый прогон как раз и нужен, чтобы его создать."""
    if args.baseline:
        return Path(args.baseline)
    if args.write_baseline:
        return Path(args.baseline_path) if Path(args.baseline_path).exists() else None
    return None


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Совместимый с тестами вход: то же, что main, без sys.exit."""
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())

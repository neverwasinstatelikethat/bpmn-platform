"""CLI офлайн-оценки ИИ-контура.

    python -m eval.run --mode replay --scenarios all --repeat 1 \
        --baseline eval/baselines/current.json [--write-baseline]

Выход: таблица метрик и провалов в stdout + JSON-отчёт в `eval/reports/<timestamp>.json`.
`--mode live` читает GIGACHAT_CREDENTIALS из окружения и без него честно падает
(код 2): тихая подмена live'а на replay иначе выглядела бы как «зелёный» прогон.
Ключи при этом подхватываются из `.env` — как их подхватывает backend
(`main.py:14`), иначе живой прогон из чистой оболочки падал на отсутствующем
`GIGACHAT_CREDENTIALS`, хотя ключи лежат в файле рядом с репозиторием.
Код 1 — по `--fail-on-regression`, если относительно baseline что-то упало: CI
так и должен отличать «правку промпта, которая ухудшила контур».
Код 2 — харнесс не смог посчитать прогон: нет ключа, нет фикстур, либо упала
сама метрика (`ОШИБКИ МЕТРИК`): значение такой метрики — None, и пропускать её
как «данных нет» нельзя, иначе баг харнесса выглядел бы отсутствием регрессий.
Код 3 — линейка отличается от baseline (новое правило или инвариант): сверка
несопоставима и регрессий прогон не показывает вовсе, поэтому без явного
`--accept-ruler-change` он тоже не считается пройденным. Иначе «добавили
правило» автоматически означало бы «гейт выключен на этом прогоне».
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from dotenv import load_dotenv

from . import force_utf8_output, harness, metrics
from .harness import DEFAULT_BASELINE, HarnessError

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _force_utf8_output() -> None:
    """Совместимый с прежними вызовами вход: хелпер переехал в пакет, потому что
    остальные CLI-входы (`eval.coverage`, `eval.advice`, `eval.retrieval`) его не
    вызывали и на cp1251 падали с `UnicodeEncodeError`."""
    force_utf8_output()


def _load_env_file(env_file: Optional[Path] = None) -> None:
    """Заполнить окружение ключами из `.env` перед live-прогоном.

    `override=False`: то, что уже задано в окружении, важнее файла — иначе вызов
    с явными `GIGACHAT_CREDENTIALS` молча получил бы значение с локального
    диска. Отсутствующий файл dotenv переносит тихо (возвращает False), поэтому
    в CI без `.env` live по-прежнему честно падает с кодом 2.
    """
    load_dotenv(env_file or ENV_FILE, override=False)


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
    parser.add_argument("--accept-ruler-change", action="store_true",
                        help="признать смену линейки (правла или инварианты "
                             "отличаются от baseline): без этого флага прогон "
                             "возвращает код 3, даже если регрессий не видно. "
                             "Сверка с таким baseline всё равно не проводится — "
                             "меры несопоставимы, — поэтому флаг означает "
                             "«изменение линейки обзорено и baseline будет "
                             "пересобран», а не «гейт выключен»")
    parser.add_argument("--no-report", action="store_true",
                        help="не писать JSON-отчёт в eval/reports")
    parser.add_argument("--dump-schemes", default="",
                        help="папка, куда положить BPMN-схемы прогона для "
                             "ручного разбора (пусто — не выгружать)")
    parser.add_argument("--fixtures-dir", default=str(harness.FIXTURES_DIR),
                        help="каталог фикстур (по умолчанию eval/fixtures)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    _force_utf8_output()
    if args.mode == "live":
        _load_env_file()
    try:
        report = harness.run(
            mode=args.mode, scenarios_spec=args.scenarios, repeat=args.repeat,
            baseline_path=_baseline_path(args), fixtures_dir=Path(args.fixtures_dir),
            threshold=args.threshold, accept_ruler_change=args.accept_ruler_change)
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
    if args.dump_schemes:
        written = harness.dump_schemes(report, Path(args.dump_schemes))
        print(f"\nСхемы для разбора: {len(written)} в {args.dump_schemes}")
    if args.write_baseline:
        path = report.write_baseline(Path(args.baseline_path))
        print(f"Baseline записан: {path}")
    # Порядок кодов: сначала то, что делает прогон нечитаемым (упавшая метрика),
    # затем несопоставимая линейка, и только потом — вердикт по метрикам.
    if report.metric_errors:
        # ОШИБКА метрики — не «данных нет»: значение превращалось в None, и
        # такая метрика не попадала ни в регрессии, ни в `unverified_metrics`.
        # Гейт по прогону, где линейка сломалась у себя в кармане, не выносится.
        print("ХАРНЕСС: метрики упали внутри себя — прогон нечитаем: "
              + ", ".join(sorted(report.metric_errors)), file=sys.stderr)
        return 2
    if report.ruler_change and not args.accept_ruler_change:
        print("ХАРНЕСС: линейка отличается от baseline; сверка не проводилась, "
              "гейт по такому прогону не выносится. Проверьте, что новое правило "
              "заряжает реальное (`python -m eval.coverage`, `python -m "
              "eval.advice --all-rules`), пересоберите baseline --write-baseline "
              "или передайте --accept-ruler-change, если изменение линейки "
              "осознанно и сверка отложена до пересборки.", file=sys.stderr)
        return 3
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

"""Оценка ИИ-контура: генерация и улучшение BPMN против измеримых инвариантов.

Пакет изолирован от приложения: он импортирует `core`, но ничего в него не
импортирует — в `core` ему делать нечего и наоборот, здесь только замеры.
Метрики считаются на записанных ответах модели (replay) и на живом LLM (live),
сверяются с baseline детектором регрессий. Точка входа — `python -m eval.run`.

Все CLI-входы пакета (`eval.run`, `eval.coverage`, `eval.advice`,
`eval.retrieval`, `eval.provenance`) печатают отчёт через
`force_utf8_output`: раньше его вызывал только `run.py`, и перепись корпуса
падала с `UnicodeEncodeError` в родной для проекта консоли cp1251 — то есть
документированная команда не работала там, где её и запускают.
"""

from __future__ import annotations

import contextlib
import sys

__all__ = ["force_utf8_output", "harness", "invariants", "metrics", "run",
           "scenarios"]


def force_utf8_output() -> None:
    """Отчёт печатается символами (✗, ⊘, ×), которых нет в windows-кодировке cp1251.

    Иначе на родной для проекта консоли прогон падал с UnicodeEncodeError уже
    после того, как всё было посчитано: код возврата оставался осмысленным, но
    таблицу метрик человек не видел. Замена символа здесь честнее падения.

    Хелпер общий на весь пакет: у каждой переписи свой `main`, и форсировать
    кодировку в одном только `eval.run` значило бы оставить остальные команды
    сломанными ровно на той платформе, где их запускают.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

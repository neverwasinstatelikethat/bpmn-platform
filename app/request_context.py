"""Идентификатор запроса в контексте и в логах, настройка корневого логгера.

Зачем это нужно: веб-процесс писал строки без какого-либо признака, какому
запросу они принадлежат. «Задача встала в очередь → очередь легла → повтор»
нельзя собрать обратно, потому что строк от десятка пользователей в перемешку
и ни одного ключа группировки. Хуже того, под `uvicorn main:app` корневой
логгер не настроен вовсе: записи `app.*` доходят только до `logging.lastResort`,
который печатает лишь WARNING+ и только текст сообщения — то есть INFO-лог
старта (режим очереди, backend бюджета, прогрев корпуса) был невидим в
`docker logs`, а предупреждение теряло время и уровень.

Идентификатор живёт в `ContextVar`, поэтому он не течёт между запросами при
параллельном обслуживании; в потоки `run_in_threadpool` его переносит anyio
(копирует контекст), что закреплено тестом — иначе вся схема с фильтром
распалась бы на первом же синхронном маршруте.

Связь с воркером строится не здесь, а через `task_id`: его печатает каждый
исполнитель задачи (`app/tasks.py`), он же возвращается клиенту. Цепочка для
разбора жалобы: `X-Request-Id` → строки запроса → `task_id` → строки воркера.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import uuid
from typing import Optional

#: Заголовок, который клиент может принести сам (склейка логов фронтенда и
#: бэкенда) и который всегда возвращается в ответе.
REQUEST_ID_HEADER = "X-Request-Id"

#: Длина и алфавит, которые допускаются до того, как значение попадёт в
#: заголовок ответа и в строку лога. Произвольный вход в формате лога — это
#: подделка записей: перевод строки превращает один запрос в две строки
#: журнала и обманывает любой парсер логов.
_MAX_ID_LEN = 64
_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-")


def new_request_id() -> str:
    """Короткий идентификатор: 16 hex достаточно для разбора суток логов и
    читается с экрана телефона без разрыва."""
    return uuid.uuid4().hex[:16]


def current_request_id() -> str:
    """Идентификатор текущего запроса; `-` вне запроса (воркер, старт)."""
    return _request_id.get()


def sanitize_request_id(raw: Optional[str]) -> str:
    """Читается вход от клиента или нет — значение обязано быть безопасным.

    Пустое, слишком короткое после зачистки или длиннее `_MAX_ID_LEN` значение
    заменяется сгенерированным: клиент, приславший `????`, не должен получать
    в ответ и в лог мусор, по которому ничего не ищется.
    """
    candidate = _ALLOWED_RE.sub("", (raw or "").strip())[:_MAX_ID_LEN]
    return candidate or new_request_id()


def use_request_id(raw: Optional[str]) -> contextvars.Token:
    """Устанавливает идентификатор (из заголовка или новый) для текущего запроса."""
    return _request_id.set(sanitize_request_id(raw))


def reset_request_id(token: contextvars.Token) -> None:
    """Снимает идентификатор обратно. Наружу `ContextVar` не отдаётся: пусть
    middleware работает с двумя функциями этого модуля, а не с контекстом."""
    _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Добавляет `record.rid`, чтобы форматер мог его напечатать.

    Фильтр навешивается на ОБРАБОТЧИКИ, а не на логгер: фильтры логгера не
    применяются к записям, пришедшим от дочерних логгеров по propagation, и
    `app.routers.ai` остался бы без идентификатора.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "rid"):
            record.rid = current_request_id()
        return True


#: Формат единой строкой: время, уровень, идентификатор запроса, имя логгера.
#: Порядок именно такой — `rid` до сообщения, чтобы его искал grep по префиксу.
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(rid)s] %(name)s: %(message)s"


def resolve_log_level(name: Optional[str] = None) -> int:
    level_name = (name if name is not None
                  else os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    return getattr(logging, level_name, logging.INFO)


def configure_logging(level: Optional[str] = None,
                      *, replace_existing: bool = False) -> logging.Handler:
    """Ставит обработчик на корневой логгер и делает это идемпотентно.

    Вызывается из lifespan (`app/main.py`) и из старта воркера
    (`app/tasks.py`), а не на импорте модуля: библиотека, которая при `import`
    трогает чужие обработчики, ломает вывод тестов и хост-процессов. Повторный
    вызов не размножает строки: наш прежний обработчик снимается.

    `replace_existing` — для процесса воркера: там логгер корня наш целиком, и
    обработчик, который мог поставить `arq --log-level`, заменяется нашим,
    иначе одна и та же строка печаталась бы дважды в разных форматах. В
    веб-процессе ключ выключен: там свои обработчики у uvicorn, и отбирать их у
    сервера — значит менять формат его доступа без спроса.
    """
    root = logging.getLogger()
    root.setLevel(resolve_log_level(level))
    for handler in list(root.handlers):
        if replace_existing or getattr(handler, "_bpmn_request_id_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    _force_utf8(handler)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    setattr(handler, "_bpmn_request_id_handler", True)
    root.addHandler(handler)
    return handler


def _force_utf8(handler: logging.StreamHandler) -> None:
    """Поток лога — UTF-8, где это возможно.

    Сообщения этого приложения по-русски, и в консоли Windows потоковый
    обработчик печатал их кодовой страницей канала: `docker logs` (Linux) это не
    касалось, а разбор локального запуска превращался в кракозябры ровно там,
    где человек смотрит чаще всего. Ошибку кодировки не прячем: то, что не
    кодируется, уходит заменёнными последовательностями, а не обрывает строку.
    """
    stream = getattr(handler, "stream", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (ValueError, OSError):
        # Поток мог быть закрыт (тест, перезапуск lifespan) — лог сам по себе
        # не причина ронять старт процесса.
        pass

"""Очередь задач ИИ-контура: маршрутизация, исполнение и воркер.

Долгие вызовы модели больше не занимают воркер uvicorn на минуты: маршрут
создаёт строку `ai_tasks`, ставит задачу в очередь и отвечает `202` с
идентификатором, а работу делает отдельный процесс (`arq app.tasks.LLMWorkerSettings`).

Маршрутизация — это выбор очереди по виду нагрузки (план
redis-cache-and-task-routing, раздел 4.2), а не бизнес-правило:

* `ai_llm` — задачи с обращением к внешнему провайдеру. Параллелизм маленький
  (ARQ_MAX_JOBS), потому что тариф GigaChat даёт один одновременный запрос
  (`GIGACHAT_MAX_CONCURRENT`), и очередь здесь защищает провайдера, а не нас.
* `ai_local` — детерминированные вычисления без сети (скоринг). Параллелизм
  выше, таймаут короче, повтор один.

Два режима исполнения. При заданном `REDIS_URL` задачи уходят в arq; без него
задача исполняется в процессе запроса (`InlineBroker`). Это не заглушка для
тестов, а штатный режим одиночного процесса: без брокера долгие вызовы
физически некуда вынести, а контракт маршрутов (202 + task_id + опрос) остаётся
тем же самым — разница видна только в поле `queued` ответа.

Источник истины о задаче — база (`app/services/tasks.py`): Redis лишь транспорт,
и его потеря не лишает пользователя ни истории, ни результата.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from arq import Retry
from arq.cron import cron
from fastapi import HTTPException

from app import ai as ai_components
from app.config import (AI_LLM_JOB_TIMEOUT, AI_LLM_JOB_TRIES, AI_LOCAL_JOB_TIMEOUT,
                        AI_LOCAL_JOB_TRIES, AI_TASK_STALE_SECONDS,
                        AI_WORKER_HEALTH_SECONDS, ARQ_MAX_JOBS,
                        ARQ_LOCAL_MAX_JOBS, CANCEL_PROBE_SECONDS,
                        LLM_CACHE_TTL, REDIS_URL)
from app.db import SessionLocal
from app.deps import release_ai_budget
from app.models import AiTask, User
from app.request_context import configure_logging
from app.services import ai_cache, tasks as task_store
from app.services.access import load_diagram
from app.services.improvements import ANALYSIS_ONLY, record_proposal
from core.llm_improve import (ImprovementCancelled, ImprovementError,
                              ImprovementUnavailable)

logger = logging.getLogger(__name__)

# --- очереди и виды задач ----------------------------------------------------
QUEUE_LLM = "ai_llm"
QUEUE_LOCAL = "ai_local"

GENERATE = "generate"
IMPROVE = "improve"
SCORE = "score"

QUEUE_OF = {GENERATE: QUEUE_LLM, IMPROVE: QUEUE_LLM, SCORE: QUEUE_LOCAL}
FUNCTION_OF = {GENERATE: "generate_bpmn_task", IMPROVE: "improve_diagram_task",
               SCORE: "score_diagram_task"}
QUEUE_TIMEOUT = {QUEUE_LLM: AI_LLM_JOB_TIMEOUT, QUEUE_LOCAL: AI_LOCAL_JOB_TIMEOUT}
QUEUE_TRIES = {QUEUE_LLM: AI_LLM_JOB_TRIES, QUEUE_LOCAL: AI_LOCAL_JOB_TRIES}

# Задержка повтора после сбоя провайдера: 5 с, затем 25 с. Экспонента с таким
# шагом даёт провайдеру подняться и не превращает повтор в немедленный второй
# отказ (план, раздел 4.2).
_RETRY_BACKOFF_SECONDS = (5, 25)

# Просроченный или незаполненный ключ провайдера — отказ, который повтор задачи
# не лечит: он лечится правкой окружения. Отличаем его от транспортного сбоя по
# имени переменной, которое называет сам слой LLM (`core/llm_client.py`:
# `get_llm_client` и `_is_auth_failure` печатают `GIGACHAT_CREDENTIALS`). Иного
# различимого признака у этого отказа нет: транспортный сбой и отказ авторизации
# поднимают один и тот же `LLMError`, а слои над ним переписывают сообщение на
# своё. Отдельный класс в core/llm_client.py снял бы и зависимость от текста.
_PROVIDER_AUTH_MARKER = "gigachat_credentials"
# Код доезжает до клиента: его нет в `RETRYABLE_CODES` фронтенда
# (bpmn-constructor/src/api/tasks.js), поэтому просроченный ключ не превращается
# ни во второй круг arq, ни в автоматический повтор уже новой задачи.
PROVIDER_AUTH_CODE = "provider_auth"

# Срок хранения закрытых задач. Отдельной настройки в app.config нет, а он
# обязан быть конечным: в `result` лежит полный XML схемы (до `AI_MAX_XML_CHARS`
# на строку), и без чистки таблица растёт бесконечно. Порог взят от срока жизни
# ответа модели: пока кэш жив, строка задачи объясняет, что произошло, а после
# повтора из кэша уже не будет, и остаётся только место в базе.
TASK_RETENTION_SECONDS = LLM_CACHE_TTL


def queue_of(kind: str) -> str:
    try:
        return QUEUE_OF[kind]
    except KeyError:
        raise ValueError(f"неизвестный вид задачи: {kind}") from None


class TaskCancelled(Exception):
    """Пользователь отменил задачу до обращения к модели."""


class TaskRefused(Exception):
    """Отказ, который повтор той же задачи не лечит: вход или настройка контура.

    `code` доезжает до клиента, и по нему фронтенд отличает «сократите
    описание» (400-класс) от «сервис недоступен» (503-класс). Повтор задачи с
    таким кодом — это вторая оплата вызова модели за тот же ответ.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class TaskRetryable(Exception):
    """Сбой контура, который имеет смысл повторить."""


# --- исполнение --------------------------------------------------------------
def _check_cancel(task_id: str) -> None:
    if task_store.cancel_requested(task_id):
        raise TaskCancelled(task_id)


def _auth_in_text(message: str) -> bool:
    return _PROVIDER_AUTH_MARKER in message.lower()


def _provider_blocked(error: BaseException) -> bool:
    """Отказ доступа к провайдеру в цепочке причин.

    Слои над LLM переписывают сообщение на своё («сервис временно недоступен»),
    поэтому одного текста мало: смотрим и `__cause__` — именно там живёт
    исходный `LLMError`, называющий `GIGACHAT_CREDENTIALS`.
    """
    current: Optional[BaseException] = error
    for _ in range(8):
        if current is None:
            return False
        if _auth_in_text(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


class _CancelProbe:
    """Флаг отмены, который не читает базу из цикла событий.

    `improve_diagram` опрашивает отмену синхронным колбэком прямо в ивент-лупе
    (core/llm_improve.py), а `task_store.cancel_requested` — это запрос к базе:
    между обращениями к модели луп стоял бы на этом чтении и тормозил бы все
    остальные задачи процесса. Здесь колбэк отдаёт последнее известное значение,
    а свежее уносится в поток пула; опрос делают перед каждым вызовом модели, то
    есть не чаще, чем раз в `CANCEL_PROBE_SECONDS`, и к следующему опросу значение
    уже обновлено.
    """

    def __init__(self, task_id: str, ttl_seconds: float) -> None:
        self._task_id = task_id
        self._ttl = ttl_seconds
        self._cancelled = False
        self._read_at = 0.0
        self._reading = False

    def check(self) -> bool:
        now = time.monotonic()
        if not self._reading and now - self._read_at >= self._ttl:
            self._read_at = now
            self._reading = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Вне цикла событий читать нечего: ждём сами, иначе колбэк
                # вернул бы устаревшее значение навсегда.
                self._reading = False
                self._cancelled = task_store.cancel_requested(self._task_id)
            else:
                loop.run_in_executor(None, self._refresh)
        return self._cancelled

    def _refresh(self) -> None:
        try:
            self._cancelled = task_store.cancel_requested(self._task_id)
        finally:
            self._reading = False


def _backoff_seconds(job_try: int) -> int:
    index = min(max(job_try - 1, 0), len(_RETRY_BACKOFF_SECONDS) - 1)
    return _RETRY_BACKOFF_SECONDS[index]


def _closed(status: Optional[str], task_id: str, wanted: str) -> str:
    """Итоговый статус закрытой задачи.

    Условный переход в базе мог не состояться, потому что строку отменили, пока
    модель отвечала. Открытой её оставлять нельзя — клиент опрашивал бы задачу
    без терминального статуса до своего таймаута, — поэтому незакрытая строка
    доводится до `cancelled`, а уже закрытая (нашей записью или параллельной)
    отвечает тем статусом, который в ней стоит.
    """
    if status in task_store.TERMINAL:
        return status
    if status is None:
        return wanted
    return task_store.mark_cancelled(task_id) or task_store.CANCELLED


def _task_owner(task_id: str) -> Optional[int]:
    with SessionLocal() as db:
        row = db.query(AiTask.user_id).filter(AiTask.id == task_id).first()
    return int(row[0]) if row else None


async def _refund_if_never_started(task_id: str) -> None:
    """Вернуть бюджет задачи, которую никто не начинал.

    `tries` считает заявки на исполнение (`claim_task`), поэтому ноль означает,
    что работа не начиналась ни в одном процессе: обращений к провайдеру не было.
    """
    with SessionLocal() as db:
        row = (db.query(AiTask.user_id, AiTask.tries)
               .filter(AiTask.id == task_id).first())
    if row is None or int(row[1] or 0) > 0:
        return
    await _refund_unspent(task_id, reason="исполнение пропущено")


async def _refund_unspent(task_id: str, *, reason: str) -> None:
    """Вернуть слот часового бюджета, если за задачу не пришлось платить.

    Слот списывается постановкой (`_submit` в app/routers/ai.py), а обращение к
    провайдеру происходит здесь, и метка слота выводима из пары «пользователь +
    задача» именно для того, чтобы воркер вернул его из своего процесса.
    Возврат делается только там, где отсутствие оплаты следует из кода, а не из
    предположения: ответ из кэша, отмена до начала работы, смерть задачи, которую
    никто не начинал. Отказ по входу не возвращается: он и есть плата за то, что
    контур эту работу проделал (проверил, разметил, записал), и возврат
    превратил бы часовой лимит в незащищённый от перебора маршрутов.
    """
    user_id = _task_owner(task_id)
    if user_id is None:
        return
    try:
        freed = await release_ai_budget(user_id, task_id=task_id)
    except Exception as error:  # noqa: BLE001 — возврат слота не вправе ронять задачу
        logger.warning("Слот бюджета задачи %s не вернулся (%s): %s",
                       task_id, reason, error)
        return
    if freed:
        logger.info("Задача %s: слот бюджета возвращён (%s)", task_id, reason)


async def _execute(ctx: Dict[str, Any], task_id: str,
                   work: Callable[[str], Awaitable[Tuple[Dict[str, Any], Optional[str]]]],
                   ) -> Dict[str, Any]:
    """Общий контур задачи: заявка, исполнение, запись исхода.

    Условный переход `queued → running` — заявка на задачу. Если он не
    состоялся, задачу уже взял другой воркер или её отменили, и исполнять её
    значит оплатить второй вызов модели за ту же работу.

    `max_tries` берётся из очереди задачи по таблице `QUEUE_TRIES`; режим
    исполнения в процессе передаёт единицу явно, потому что повторять там
    некому — воркера, который отложил бы задачу, в этом процессе нет.
    """
    claimed = task_store.claim_task(task_id)
    if claimed is None:
        logger.info("Задача %s не в очереди (взята другим воркером или отменена) "
                    "— исполнение пропущено", task_id)
        await _refund_if_never_started(task_id)
        return {"task_id": task_id, "status": "skipped"}

    tries, queue = claimed
    max_tries = int(ctx.get("max_tries") or QUEUE_TRIES.get(queue, 1))
    # Отметка о том, что работа началась: до неё обращений к провайдеру быть не
    # может, и отказ по отмене на этом участке ничего не стоил пользователю.
    work_started = False
    try:
        _check_cancel(task_id)
        work_started = True
        result, result_ref = await work(task_id)
    except asyncio.CancelledError:
        # Таймаут очереди: arq снимает работу отменой корутины, а на Python 3.12
        # `CancelledError` — `BaseException`, и ветка `except Exception` ниже его
        # не видит. Без этой ветки строка оставалась бы в `running` вечно:
        # скоринг большой схемы, убитый по `AI_LOCAL_JOB_TIMEOUT`, клиент ждал бы
        # до своего таймаута опроса, а потом ещё и до старта следующего воркера.
        timeout = QUEUE_TIMEOUT.get(queue)
        logger.warning("Задача %s снята отменой (таймаут очереди %s с) — "
                       "закрываем как незавершённую", task_id, timeout)
        _closed(task_store.mark_failed(
            task_id,
            error="Задача не уложилась в отведённое время очереди "
                  f"({timeout} с) и остановлена — повторите запрос.",
            error_code="worker_lost"), task_id, task_store.FAILED)
        # Слот возвращается только если отмена застала ДО работы: `work_started`
        # выставляется сразу после проверки флага, и между ней и первым `await`
        # точки отмены нет — значит изнутри `work()` (таймаут очереди) приходит
        # `work_started == True`, и провайдера там уже спрашивали. Возврат по
        # таймауту отдал бы обратно оплаченное обращение, поэтому ветка ниже
        # срабатывает лишь на прерванном чтении самого флага отмены — узком, но
        # реальном окне, и оно заперто тестом.
        if not work_started:
            with contextlib.suppress(asyncio.CancelledError):
                await _refund_unspent(task_id,
                                      reason="снята отменой до обращения к модели")
        # Отмену глушить нельзя: по ней процесс воркера и понимает, что работу
        # пора прекратить.
        raise
    except TaskCancelled:
        if not work_started:
            await _refund_unspent(task_id, reason="отменена до исполнения")
        return {"task_id": task_id,
                "status": _closed(task_store.mark_cancelled(task_id), task_id,
                                  task_store.CANCELLED)}
    except TaskRefused as error:
        status = _closed(task_store.mark_failed(task_id, error=str(error),
                                                error_code=error.code),
                         task_id, task_store.FAILED)
        logger.info("Задача %s отклонена (%s): %s", task_id, error.code, error)
        return {"task_id": task_id, "status": status}
    except TaskRetryable as error:
        if tries < max_tries and task_store.requeue(task_id):
            delay = _backoff_seconds(tries)
            logger.warning("Задача %s, попытка %d из %d не удалась (%s) — повтор "
                           "через %d с", task_id, tries, max_tries, error, delay)
            raise Retry(delay) from error
        status = _closed(task_store.mark_failed(task_id, error=str(error),
                                                error_code="unavailable"),
                         task_id, task_store.FAILED)
        logger.error("Задача %s исчерпала попытки (%d): %s", task_id, max_tries, error)
        return {"task_id": task_id, "status": status}
    except Exception as error:  # noqa: BLE001 — наружу только запись в базе
        # Тип на ERROR, стек на DEBUG: сообщение SQLAlchemy несёт
        # `[SQL: ...][parameters: ...]` с полным XML схемы пользователя, а этот
        # обработчик — последнее звено воркера, то есть срабатывает на любом
        # сбое записи. Идентификатора задачи и её типа достаточно, чтобы начать
        # разбор; тело схемы в журнале делать нечего (тот же порядок, что в
        # `app/routers/ai.py` и в защитнике `app/main.py`).
        logger.error("Задача %s упала с необработанной ошибкой %s",
                     task_id, type(error).__name__)
        logger.debug("Стек задачи %s", task_id, exc_info=True)
        status = _closed(task_store.mark_failed(
            task_id, error="Внутренняя ошибка при выполнении задачи",
            error_code="internal"), task_id, task_store.FAILED)
        return {"task_id": task_id, "status": status}

    status = _closed(task_store.mark_done(task_id, result=result,
                                          result_ref=result_ref),
                     task_id, task_store.DONE)
    return {"task_id": task_id, "status": status}


def _load_inputs(task_id: str) -> Dict[str, Any]:
    """Входы задачи из базы. Аргументов кроме `task_id` в очереди нет: содержимое
    схем не должно попадать в Redis (план, раздел 8)."""
    with SessionLocal() as db:
        task = db.get(AiTask, task_id)
        if task is None:
            raise TaskRefused("Задача не найдена", code="not_found")
        return {"kind": task.kind, "diagram_id": task.diagram_id,
                "base_seq": task.base_seq, "user_id": task.user_id,
                **task_store.task_payload(task)}


# --- задачи ------------------------------------------------------------------
async def _generate_work(task_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
    inputs = _load_inputs(task_id)
    text = str(inputs.get("text") or "")
    if not text.strip():
        raise TaskRefused("Описание процесса не задано", code="empty_input")

    cache = ai_cache.get_ai_cache()
    if cache is not None:
        cached = cache.lookup_generation(text)
        if cached is not None:
            if cached.negative:
                raise TaskRefused(cached.error or "Генерация отклонена",
                                  code="cached_refusal")
            logger.info("Генерация %s взята из кэша", task_id)
            # Провайдер не спрашивался, а слот бюджета списан постановкой: без
            # возврата шестьдесят повторов одной закешированной схемы выбрали бы
            # `AI_REQUESTS_PER_HOUR`, не оплатив ни одного обращения.
            await _refund_unspent(task_id, reason="ответ взят из кэша")
            return cached.result, None

    _check_cancel(task_id)
    # Отмена смотрит на каждой границе стадий генерации, а не только перед её
    # началом: прогон делает до шести обращений к модели, и без колбэка
    # отменённая задача договаривала их до конца — за счёт пользователя, у
    # которого слот часового бюджета уже списан. Колбэк читает базу вне цикла
    # событий, как и в улучшении.
    probe = _CancelProbe(task_id, CANCEL_PROBE_SECONDS)
    result = await asyncio.to_thread(
        ai_components.generator.generate, text, cancel_check=probe.check)
    if cache is not None:
        cache.remember_generation(text, result)

    if result.get("status") != "success":
        step = str(result.get("step") or "internal")
        message = str(result.get("error") or "Не удалось построить схему")
        if step == "cancelled":
            # Это не отказ контура: пользователь сам закрыл задачу. Под
            # `TaskRefused` строка легла бы в `failed`, и клиент прочитал бы
            # ошибку там, где его решение было исполнено.
            raise TaskCancelled(task_id)
        if step == "budget":
            # Время на прогон вышло. Повтор arq здесь не помогает (та же схема
            # та же по длительности), а автоматический повтор фронтенда — это
            # вторая оплата вызова, который заведомо не влезет.
            raise TaskRefused(message, code="budget")
        if step == "llm":
            # Отказ провайдера — состояние внешнего сервиса, а не входа, и его
            # имеет смысл повторить. Кроме случая, когда провайдер не принимает
            # учётные данные: там повтор только сжигает попытки и деньги, и
            # генератор обязан назвать это своим текстом (см. `_provider_blocked`).
            if _auth_in_text(message):
                raise TaskRefused(
                    "Провайдер не принимает учётные данные — проверьте "
                    "GIGACHAT_CREDENTIALS и GIGACHAT_SCOPE",
                    code=PROVIDER_AUTH_CODE)
            raise TaskRetryable(message)
        raise TaskRefused(message, code=step)

    return {
        "status": "success",
        "bpmn": result["bpmn"],
        "structure": result.get("structure") or {},
        # notes — единственный сигнал о том, что структуру схемы починили за
        # пользователя; gaps и attempts — метрики качества контура.
        "notes": result.get("notes") or [],
        "gaps": result.get("gaps") or [],
        "attempts": result.get("attempts") or 1,
    }, None


async def _improve_work(task_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
    inputs = _load_inputs(task_id)
    prompt = str(inputs.get("prompt") or "")
    diagram_id = inputs.get("diagram_id")

    orchestrator = ai_components.get_orchestrator()
    if orchestrator is None:
        raise TaskRetryable("Сервис улучшения не инициализирован")

    xml_content: Optional[str] = inputs.get("bpmn_xml")
    if diagram_id:
        # Схема перечитывается в момент исполнения: задача могла стоять в
        # очереди минуты, и за это время её могли удалить или отозвать доступ.
        # Версия обязана совпасть с зафиксированной при постановке — иначе
        # улучшение ложится на схему, которой уже нет, а предложение, которое
        # пользователь примет, затрёт чужие правки.
        with SessionLocal() as db:
            user = db.get(User, inputs["user_id"])
            try:
                diagram = load_diagram(db, user, diagram_id, edit=True) \
                    if user is not None else None
            except HTTPException:
                diagram = None
            if diagram is None:
                raise TaskRefused("Схема недоступна", code="not_found")
            if inputs.get("base_seq") is not None \
                    and diagram.version_seq != inputs["base_seq"]:
                raise TaskRefused(
                    "Схема изменилась, пока задача стояла в очереди — запросите "
                    "улучшение заново", code="stale_diagram")
    if not xml_content:
        raise TaskRefused("Схема не задана", code="empty_input")

    # В кэше лежит ответ модели, а не итог задачи: `improvement_id` указывает на
    # строку предложения, и отдать из кэша чужой идентификатор значило бы
    # позволить пользователю принять уже закрытое или чужое предложение.
    cache = ai_cache.get_ai_cache()
    answer: Optional[Dict[str, Any]] = None
    if cache is not None:
        cached = cache.lookup_improvement(xml_content, prompt)
        if cached is not None:
            if cached.negative:
                raise TaskRefused(cached.error or "Улучшение отклонено",
                                  code="cached_refusal")
            logger.info("Улучшение %s: ответ модели взят из кэша", task_id)
            # Дальше идёт только запись предложения в базу, то есть провайдер не
            # спрашивался, а слот бюджета уже списан постановкой.
            await _refund_unspent(task_id, reason="ответ модели взят из кэша")
            answer = cached.result

    if answer is None:
        _check_cancel(task_id)
        # Колбэк отмены читает базу не из цикла событий: `improve_diagram`
        # опрашивает его синхронно перед каждым обращением к модели, и прямой
        # запрос к базе вешал бы луп воркера на время этого чтения.
        probe = _CancelProbe(task_id, CANCEL_PROBE_SECONDS)
        try:
            recommendations, improved_xml, report = await orchestrator.improve_diagram(
                xml_content=xml_content, user_prompt=prompt,
                cancel_check=probe.check,
            )
        except ImprovementCancelled as error:
            raise TaskCancelled(task_id) from error
        except ImprovementUnavailable as error:
            # Просроченный ключ провайдера — не «сервис лёг»: повтор той же
            # задачи даст тот же отказ, а три попытки arq плюс автоматический
            # повтор фронтенда — это четыре оплаты вызова, который не состоится.
            if _provider_blocked(error):
                raise TaskRefused(
                    "Провайдер не принимает учётные данные — проверьте "
                    "GIGACHAT_CREDENTIALS и GIGACHAT_SCOPE",
                    code=PROVIDER_AUTH_CODE) from error
            raise TaskRetryable(str(error)) from error
        except ImprovementError as error:
            if cache is not None:
                cache.refuse_improvement(xml_content, prompt, str(error))
            raise TaskRefused(str(error), code="refused") from error
        answer = {"recommendations": recommendations, "improved_xml": improved_xml,
                  "report": report}
        if cache is not None:
            cache.remember_improvement(xml_content, prompt, answer)

    improved_xml = answer.get("improved_xml")
    recommendations = answer.get("recommendations")
    # Только анализ: менять схему не нужно, но вывод модели обязан остаться в
    # истории — иначе сценарий «найди узкие места» нечем показать.
    with SessionLocal() as db:
        if improved_xml is None:
            improvement = record_proposal(db, user_id=inputs["user_id"],
                                          diagram_id=diagram_id, improved_xml=None,
                                          recommendations=recommendations,
                                          status=ANALYSIS_ONLY)
        else:
            improvement = record_proposal(db, user_id=inputs["user_id"],
                                          diagram_id=diagram_id,
                                          improved_xml=improved_xml,
                                          recommendations=recommendations)
        improvement_id = improvement.id

    payload = {"status": "analysis_only" if improved_xml is None else "success",
               "improvement_id": improvement_id,
               "recommendations": recommendations,
               "report": answer.get("report")}
    logger.info("Задача %s: улучшение %s (%s)", task_id, improvement_id,
                payload["status"])
    return payload, improvement_id


async def _score_work(task_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
    inputs = _load_inputs(task_id)
    xml_content = inputs.get("bpmn_xml")
    if not xml_content:
        raise TaskRefused("Схема не задана", code="empty_input")

    cache = ai_cache.get_ai_cache()
    if cache is not None:
        cached = cache.score(xml_content)
        if cached is not None:
            logger.info("Скоринг %s взят из кэша", task_id)
            return cached, None

    report = await asyncio.to_thread(ai_components.scorer.evaluate, xml_content)
    if cache is not None:
        cache.remember_score(xml_content, report)
    return report, None


async def generate_bpmn_task(ctx: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    return await _execute(ctx, task_id, _generate_work)


async def improve_diagram_task(ctx: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    return await _execute(ctx, task_id, _improve_work)


async def score_diagram_task(ctx: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    return await _execute(ctx, task_id, _score_work)


TASK_FUNCTIONS = [generate_bpmn_task, improve_diagram_task, score_diagram_task]

# Контекст исполнения без arq. `max_tries=1`: повторять задачу в процессе
# запроса некому — отложить её на 5 с и удержать соединение значило бы вернуть
# клиенту тот же отказ, только медленнее.
INLINE_CONTEXT: Dict[str, Any] = {"job_try": 1, "max_tries": 1, "redis": None}


# --- воркер ------------------------------------------------------------------
def _stale_thresholds() -> Dict[str, int]:
    """Порог потерянности для каждой очереди: её таймаут задачи плюс тот же
    запас, что отделяет `AI_TASK_STALE_SECONDS` от таймаута LLM-очереди.

    Запас берётся из настройки, а не выдумывается: она и заведена как «время,
    после которого running считается потерянным воркером», и переносить её же
    мерку на короткую `ai_local` часовой линейкой — значит ждать скоринг-задачу
    дольше, чем жил бы её собственный воркер.
    """
    margin = max(0, AI_TASK_STALE_SECONDS - AI_LLM_JOB_TIMEOUT)
    return {queue: timeout + margin for queue, timeout in QUEUE_TIMEOUT.items()}


async def _recover_stale_tasks(ctx: Dict[str, Any]) -> None:
    """Разобрать задачи, которые никто не ведёт.

    Обе незакрытые стадии. Строка `running` без живого воркера сама в очередь не
    вернётся: arq хранит свою копию задачи, и после падения процесса её может не
    остаться. Строка `queued` старше своего срока — потерянная постановка (commit
    состоялся, `enqueue` нет), и о ней не знает ни один воркер, поэтому раньше
    такая задача висела в очереди вечно: стартовый крюк искал только `running`.

    Задача со неисчерпанными попытками ставится в очередь заново новым
    идентификатором попытки (arq не принимает повтор уже известного), а
    исчерпанная закрывается ошибкой. Для `queued` это иногда второй job той же
    задачи — и это безопасно: заявка на исполнение (`queued → running`) условная,
    и второй воркер уйдёт в `skipped`, не оплатив второй вызов модели. Строку
    `running` наоборот возвращаем через защёлку `stale_after`: без неё обход
    разворачивал в `queued` работу, заявленную в окне между списком и UPDATE, и
    второй job проходил `claim_task` заново — то есть оплачивал прогон дважды.

    Каждая строка разбирается сама по себе: `_worker_startup` зовёт обход прямо,
    и одно нерассоренное исключение (сбой базы, незнакомый `kind`, снятая строка)
    роняло бы процесс воркера в петлю рестартов вместе с его живыми задачами.
    То же касается и самого списка: запрос до цикла — чужая доступность (база на
    перезапуске, лимит соединений), а не наше состояние, и воркер, умерший на
    этом, не успевает заявить отметку живости. Пропущенный обход дешёв: уборка
    стоит в cron и повторит её на следующем слоте.
    """
    thresholds = _stale_thresholds()
    try:
        stale = task_store.stale_tasks(thresholds)
    except Exception as error:  # noqa: BLE001 — уборка не вправе топить воркер
        logger.error("Список потерянных задач не получен (%s): обход пропущен, "
                     "следующий слот повторит", type(error).__name__)
        logger.debug("Запрос потерянных задач", exc_info=True)
        return
    if not stale:
        return
    redis = ctx.get("redis")
    requeued = 0
    closed_off = 0
    for task_id, queue, tries, stage in stale:
        max_tries = QUEUE_TRIES.get(queue, 1)
        try:
            if redis is not None and tries < max_tries:
                function = _function_of_task(task_id)
                if function is None:
                    # Исполнителя нет: строку снесли между списком и этим чтением,
                    # либо её `kind` не знаком ни одной очереди. Ставить job нечем,
                    # а молчать — значит оставить её открытой навсегда.
                    logger.warning("Задача %s: исполнитель не найден — закрываем "
                                   "как потерянную", task_id)
                else:
                    if stage == task_store.RUNNING and not task_store.requeue(
                            task_id,
                            stale_after=thresholds.get(queue, AI_LLM_JOB_TIMEOUT)):
                        logger.info("Задача %s к моменту уборки снова в работе или "
                                    "снята — не переставляем", task_id)
                        continue
                    await redis.enqueue_job(
                        function, task_id,
                        _job_id=f"{task_id}:r{tries + 1}", _queue_name=queue)
                    requeued += 1
                    continue
            # Закрытие проверяется через `_closed`, как в `_execute`: переход
            # `running → failed` условный, и если человек успел нажать «Отменить»
            # (или вкладка ушла с `keepalive`-отменой), `mark_failed` строку не
            # трогает, а `requeue` выше её отвергает. Без этого запись оставалась бы
            # `running` вечно: опрос клиента не завершается, уборка повторяется каждые
            # две минуты, а `purge_closed` терминальный статус не видит.
            status = _closed(task_store.mark_failed(
                task_id, error="Воркер не завершил задачу и перезапустился",
                error_code="worker_lost"), task_id, task_store.FAILED)
            if status != task_store.FAILED:
                logger.info("Задача %s закрыта как %s: её сняли до перезапуска воркера",
                            task_id, status)
            closed_off += 1
            if tries == 0:
                # Потерянная постановка, а не потерянный прогон: заявку на исполнение
                # никто не выставлял, значит провайдер не спрашивался.
                await _refund_unspent(task_id, reason="похоронена до старта")
        except Exception as error:  # noqa: BLE001 — одна строка не должна стопить обход
            closed_off += 1
            logger.error("Задача %s не разобрана уборкой (%s) — переходим к следующей",
                         task_id, type(error).__name__)
            logger.debug("Подробности уборки задачи %s", task_id, exc_info=True)
    logger.warning("Восстановление после перезапуска: %d задач возвращено в "
                   "очередь, %d закрыто или пропущено", requeued, closed_off)


async def _sweep_contour(ctx: Dict[str, Any]) -> None:
    """Периодическая уборка контура: потерянные задачи и просроченная история.

    Раз в `AI_LOCAL_JOB_TIMEOUT` — по самому короткому таймауту очередей: за
    такое время потерянная задача замечается, не дожидаясь перезапуска воркера, а
    база не читается впустую каждые несколько секунд.

    Расписание общее для обоих процессов воркера, и arq по `unique` кладёт job
    один раз на слот, а не по разу на воркер; при `REDIS_URL` пустом уборка не
    нужна вовсе — исполняющий задачи процесс сам держит каждую задачу до конца.
    """
    await _recover_stale_tasks(ctx)
    removed = task_store.purge_closed(TASK_RETENTION_SECONDS)
    if removed:
        logger.info("История задач: удалено %d закрытых строк старше %d с",
                    removed, TASK_RETENTION_SECONDS)


def _sweep_schedule() -> List[Any]:
    """Cron уборки для настроек обоих воркеров."""
    minutes = max(1, AI_LOCAL_JOB_TIMEOUT // 60)
    return [cron(_sweep_contour, minute=set(range(0, 60, minutes)), second=0)]


def _function_of_task(task_id: str) -> Optional[str]:
    with SessionLocal() as db:
        task = db.get(AiTask, task_id)
        return FUNCTION_OF.get(task.kind) if task else None


async def _worker_startup(ctx: Dict[str, Any]) -> None:
    # Без этого корневой логгер процесса воркера не настроен (arq ставит
    # обработчик только по своему `--log-level`), и INFO-строки старта вместе с
    # предупреждениями уборки уходили в `logging.lastResort` — то есть в
    # `docker logs` воркера попадал только WARNING без времени и уровня.
    configure_logging(replace_existing=True)
    await _recover_stale_tasks(ctx)
    ai_cache.init_ai_cache()
    # Тяжёлые модели поднимаются один раз на процесс воркера. Веб-процесс в
    # режиме очереди их не держит вовсе (план, раздел 4.5.6).
    ai_components.init_orchestrator()


async def _worker_shutdown(ctx: Dict[str, Any]) -> None:
    ai_cache.close_ai_cache()


def _redis_settings():
    """Настройки подключения arq.

    Полагаться на значение по умолчанию (localhost:6379) нельзя: воркер молча
    смотрел бы не туда, куда смотрит веб-процесс, и задачи не исполнялись бы
    вообще, тогда как очередь при этом выглядит живой.
    """
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(REDIS_URL) if REDIS_URL else RedisSettings()


class LLMWorkerSettings:
    """Воркер очереди `ai_llm`: запуск `arq app.tasks.LLMWorkerSettings`."""

    functions = TASK_FUNCTIONS
    redis_settings = _redis_settings()
    queue_name = QUEUE_LLM
    max_jobs = ARQ_MAX_JOBS
    job_timeout = AI_LLM_JOB_TIMEOUT
    max_tries = AI_LLM_JOB_TRIES
    retry_jobs = True
    # Отмена из arq не используется намеренно: прерывать оплаченный вызов
    # провайдера бессмысленно, флаг отмены в базе снимает задачу до вызова.
    allow_abort_jobs = False
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    keep_result = 3600
    # Уборка потерянных задач и просроченной истории: разовый крюк запуска её
    # не покрывает — упавшую постановку воркер может и не застать.
    cron_schedule = _sweep_schedule()
    # Интервал задаёт окно живости для healthcheck контейнера: ключ живёт
    # на интервал дольше, и с дефолтным часом мёртвый воркер выглядел бы живым.
    health_check_key = f"{QUEUE_LLM}:health"
    health_check_interval = AI_WORKER_HEALTH_SECONDS


class LocalWorkerSettings:
    """Воркер очереди `ai_local`: запуск `arq app.tasks.LocalWorkerSettings`."""

    functions = TASK_FUNCTIONS
    redis_settings = _redis_settings()
    queue_name = QUEUE_LOCAL
    max_jobs = ARQ_LOCAL_MAX_JOBS
    job_timeout = AI_LOCAL_JOB_TIMEOUT
    max_tries = AI_LOCAL_JOB_TRIES
    retry_jobs = True
    allow_abort_jobs = False
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    keep_result = 3600
    # Та же уборка, что и в LLM-воркере: arq по идентификатору cron-job
    # кладёт её один раз на слот, поэтому два процесса не дублируют обход, а
    # страхуют друг друга — очереди и потерянные строки у них разные.
    cron_schedule = _sweep_schedule()
    health_check_key = f"{QUEUE_LOCAL}:health"
    health_check_interval = AI_WORKER_HEALTH_SECONDS


# --- брокер ------------------------------------------------------------------
class InlineBroker:
    """Исполнение задачи в процессе запроса.

    Режим без Redis: контракт маршрутов тот же (202 + task_id + опрос), но
    задача завершается до возврата ответа, и клиент получает результат первым же
    опросом — либо сразу в ответе постановки.
    """

    name = "inline"

    async def submit(self, kind: str, task_id: str) -> None:
        function = globals()[FUNCTION_OF[kind]]
        await function(dict(INLINE_CONTEXT), task_id)

    async def close(self) -> None:
        return None

    async def depth(self) -> Dict[str, Optional[int]]:
        # Очереди нет: «глубина» неизвестна, а не равна нулю.
        return {QUEUE_LLM: None, QUEUE_LOCAL: None}


class ArqBroker:
    """Постановка задач в arq. Пул соединений создаётся один раз на процесс."""

    name = "arq"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def submit(self, kind: str, task_id: str) -> None:
        # `_job_id` равен идентификатору задачи: повторная постановка той же
        # задачи (клиент нажал дважды, запрос ушёл повторно) не создаёт вторую
        # работу в очереди — это второй уровень защиты после уникальности
        # `idempotency_key` в базе.
        job = await self._pool.enqueue_job(
            FUNCTION_OF[kind], task_id, _job_id=task_id,
            _queue_name=QUEUE_OF[kind])
        if job is None:
            logger.info("Задача %s уже стоит в очереди %s", task_id, QUEUE_OF[kind])

    async def close(self) -> None:
        await self._pool.close()

    async def depth(self) -> Dict[str, Optional[int]]:
        """Длина очередей. Метрика «очередь растёт» из плана (раздел 6, этап C):
        без неё деградация видна только по жалобам пользователей."""
        out: Dict[str, Optional[int]] = {}
        for queue in (QUEUE_LLM, QUEUE_LOCAL):
            try:
                out[queue] = int(await self._pool.zcard(queue))
            except Exception:  # noqa: BLE001 — метрика не обязана ронять запрос
                out[queue] = None
        return out


_broker: Optional[Any] = None


async def get_broker() -> Any:
    """Брокер задач либо None, если очередь сконфигурирована, но недоступна.

    Подключение повторяется на каждый вызов, пока не удастся: Redis поднимается
    вместе со стеком, и первый же запрос после его появления обязан пройти, а не
    ждать рестарта приложения.

    Недоступный Redis при заданном `REDIS_URL` не переводит приложение в режим
    исполнения в процессе: тяжёлые модели живут в воркере, и веб-процесс
    физически не может выполнить задачу. Молчаливая деградация означала бы, что
    при сбое Redis контур начинает падать на первом же запросе, поэтому маршрут
    отвечает 503.
    """
    global _broker
    if _broker is not None:
        return _broker
    if not REDIS_URL:
        _broker = InlineBroker()
        logger.info("REDIS_URL не задан: задачи исполняются в процессе запроса")
        return _broker
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
        await pool.ping()
    except Exception as error:  # noqa: BLE001 — Redis может быть ещё не поднят
        logger.error("Очередь задач недоступна по %s: %s", REDIS_URL, error)
        return None
    _broker = ArqBroker(pool)
    logger.info("Очередь задач подключена к %s", REDIS_URL)
    return _broker


async def close_broker() -> None:
    global _broker
    if _broker is not None:
        await _broker.close()
    _broker = None


def current_broker() -> Any:
    """Брокер без попытки подключения.

    Нужен операционным маршрутам: им важнее не вешать запрос на Redis, который
    прямо сейчас недоступен, чем сообщить точную глубину очереди.
    """
    return _broker


def broker_name() -> str:
    """Как исполняются задачи: для /health и для поля `queued` в ответе."""
    if _broker is None:
        return "unavailable" if REDIS_URL else "inline"
    return _broker.name


def reset_broker_for_tests() -> None:
    """Тестовый крюк: сбросить singleton брокера (аналог reset_ai_budget)."""
    global _broker
    _broker = None

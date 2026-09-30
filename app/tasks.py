"""Очередь задач ИИ-контура: маршрутизация, исполнение и воркер.

Долгие вызовы модели больше не занимают воркер uvicorn на минуты: маршрут
создаёт строку `ai_tasks`, ставит задачу в очередь и отвечает `202` с
идентификатором, а работу делает отдельный процесс (`arq app.tasks.LLMWorkerSettings`).

Маршрутизация — это выбор очереди по виду нагрузки (план
docs/plans/redis-cache-and-task-routing.md, раздел 4.2), а не бизнес-правило:

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
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from arq import Retry
from fastapi import HTTPException

from app import ai as ai_components
from app.config import (AI_LLM_JOB_TIMEOUT, AI_LLM_JOB_TRIES, AI_LOCAL_JOB_TIMEOUT,
                        AI_LOCAL_JOB_TRIES, AI_TASK_STALE_SECONDS,
                        AI_WORKER_HEALTH_SECONDS, ARQ_MAX_JOBS,
                        ARQ_LOCAL_MAX_JOBS, REDIS_URL)
from app.db import SessionLocal
from app.models import AiTask, User
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


def queue_of(kind: str) -> str:
    try:
        return QUEUE_OF[kind]
    except KeyError:
        raise ValueError(f"неизвестный вид задачи: {kind}") from None


class TaskCancelled(Exception):
    """Пользователь отменил задачу до обращения к модели."""


class TaskRefused(Exception):
    """Отказ, определённый входом: повтор той же задачи даст тот же ответ.

    `code` доезжает до клиента, и по нему фронтенд отличает «сократите
    описание» (400-класс) от «сервис недоступен» (503-класс).
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


def _backoff_seconds(job_try: int) -> int:
    index = min(max(job_try - 1, 0), len(_RETRY_BACKOFF_SECONDS) - 1)
    return _RETRY_BACKOFF_SECONDS[index]


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
        return {"task_id": task_id, "status": "skipped"}

    tries, queue = claimed
    max_tries = int(ctx.get("max_tries") or QUEUE_TRIES.get(queue, 1))
    try:
        _check_cancel(task_id)
        result, result_ref = await work(task_id)
    except TaskCancelled:
        task_store.mark_cancelled(task_id)
        logger.info("Задача %s отменена пользователем", task_id)
        return {"task_id": task_id, "status": task_store.CANCELLED}
    except TaskRefused as error:
        task_store.mark_failed(task_id, error=str(error), error_code=error.code)
        logger.info("Задача %s отклонена по входу (%s): %s", task_id, error.code, error)
        return {"task_id": task_id, "status": task_store.FAILED}
    except TaskRetryable as error:
        if tries < max_tries and task_store.requeue(task_id):
            delay = _backoff_seconds(tries)
            logger.warning("Задача %s, попытка %d из %d не удалась (%s) — повтор "
                           "через %d с", task_id, tries, max_tries, error, delay)
            raise Retry(delay) from error
        task_store.mark_failed(task_id, error=str(error), error_code="unavailable")
        logger.error("Задача %s исчерпала попытки (%d): %s", task_id, max_tries, error)
        return {"task_id": task_id, "status": task_store.FAILED}
    except Exception as error:  # noqa: BLE001 — наружу только запись в базе
        logger.exception("Задача %s упала с необработанной ошибкой", task_id)
        task_store.mark_failed(task_id,
                               error="Внутренняя ошибка при выполнении задачи",
                               error_code="internal")
        return {"task_id": task_id, "status": task_store.FAILED}

    task_store.mark_done(task_id, result=result, result_ref=result_ref)
    return {"task_id": task_id, "status": task_store.DONE}


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
            return cached.result, None

    _check_cancel(task_id)
    result = await asyncio.to_thread(ai_components.generator.generate, text)
    if cache is not None:
        cache.remember_generation(text, result)

    if result.get("status") != "success":
        step = str(result.get("step") or "internal")
        message = str(result.get("error") or "Не удалось построить схему")
        if step == "llm":
            # Отказ провайдера — состояние внешнего сервиса, а не входа.
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
            answer = cached.result

    if answer is None:
        _check_cancel(task_id)
        try:
            recommendations, improved_xml, report = await orchestrator.improve_diagram(
                xml_content=xml_content, user_prompt=prompt,
                cancel_check=lambda: task_store.cancel_requested(task_id),
            )
        except ImprovementCancelled as error:
            raise TaskCancelled(task_id) from error
        except ImprovementUnavailable as error:
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
async def _recover_stale_tasks(ctx: Dict[str, Any]) -> None:
    """Разобрать задачи, которые воркер начал и не закончил.

    Строка в статусе `running` без живого воркера сама в очередь не вернётся:
    arq хранит свою копию задачи, и после падения процесса её может не остаться.
    Поэтому задача со неисчерпанными попытками ставится в очередь заново (с
    новым идентификатором попытки — arq не принимает повтор уже известного),
    а исчерпанная закрывается ошибкой. Без этого пользователь вечно опрашивал
    бы задачу, которую никто не выполняет.
    """
    stale = task_store.stale_tasks(AI_TASK_STALE_SECONDS)
    if not stale:
        return
    redis = ctx.get("redis")
    requeued = 0
    for task_id, queue, tries in stale:
        max_tries = QUEUE_TRIES.get(queue, 1)
        if redis is not None and tries < max_tries:
            task_store.requeue(task_id)
            await redis.enqueue_job(
                _function_of_task(task_id), task_id,
                _job_id=f"{task_id}:r{tries + 1}", _queue_name=queue)
            requeued += 1
            continue
        task_store.mark_failed(
            task_id, error="Воркер не завершил задачу и перезапустился",
            error_code="worker_lost")
    logger.warning("Восстановление после перезапуска: %d задач возвращено в "
                   "очередь, %d закрыто ошибкой", requeued, len(stale) - requeued)


def _function_of_task(task_id: str) -> Optional[str]:
    with SessionLocal() as db:
        task = db.get(AiTask, task_id)
        return FUNCTION_OF.get(task.kind) if task else None


async def _worker_startup(ctx: Dict[str, Any]) -> None:
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

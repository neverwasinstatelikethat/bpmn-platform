"""Состояние задач ИИ-контура в базе.

Redis — транспорт, база — источник истины (план
redis-cache-and-task-routing, раздел 4.3). Отсюда два правила,
которые видны в каждой функции модуля:

1. Переход статуса делается условным обновлением (`WHERE status = ...`), а не
   чтением с последующей записью. Два воркера, взявшие одну задачу, иначе
   оба дошли бы до вызова модели и оба списали бы деньги; `rowcount` здесь —
   тот же замок, что `with_for_update()` в принятии улучшения, только без
   удержания строки на всё время выполнения.
2. Сеансы короткие. Задача живёт минуты, и держать соединение из пула на всё
   это время значит повторить дефект, из-за которого синхронный контур
   улучшения уже пришлось переписать на короткие сеансы (см. app/routers/ai.py).

Функции, которые зовёт воркер, открывают сеанс сами: у воркера нет
запроса и, значит, нет зависимости `Depends(get_db)`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import TASK_POLL_SECONDS
from app.db import SessionLocal
from app.models import AiTask, User
from app.timeutils import utc_now

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = frozenset({DONE, FAILED, CANCELLED})

# Строки, которые ещё способны дойти до исполнения: только из них делается
# терминальный переход (см. `_close`).
OPEN = (QUEUED, RUNNING)


# --- постановка ---------------------------------------------------------------
def inputs_fingerprint(*, kind: str, payload: Optional[Dict[str, Any]] = None,
                       diagram_id: Optional[str] = None,
                       base_seq: Optional[int] = None) -> str:
    """Отпечаток входов запроса — по нему сверяется повтор ключа идемпотентности.

    Хеш, а не сами входы: `payload` — это XML схемы до `AI_MAX_XML_CHARS`, и в
    индекс уникальности столько не кладут. Ключи словаря упорядочены, иначе два
    процесса, собравшие одинаковые входы в разном порядке, дали бы разные
    отпечатки, и честный повтор запроса посчитался бы новой работой (то есть
    второй оплатой модели).
    """
    material = json.dumps({"kind": kind, "payload": payload or {},
                           "diagram_id": diagram_id, "base_seq": base_seq},
                          ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def find_live_by_inputs(db: Session, user_id: int, *, kind: str,
                        payload: Optional[Dict[str, Any]] = None,
                        diagram_id: Optional[str] = None,
                        base_seq: Optional[int] = None) -> Optional[AiTask]:
    """Незакрытая задача с теми же входами — независимо от ключа.

    Ключ идемпотентности фронтенд выдаёт на каждое действие, поэтому двойное
    нажатие «Сгенерировать» или повтор после сетевого обрыва приходят с разными
    ключами, и ограничение `uq_ai_tasks_idempotency` их не ловит: ловит
    предикат «работа ещё не закрыта» (`uq_ai_tasks_live_inputs`, ревизия 0006).
    Без этого дубль — две оплаты одного обращения модели.

    Завершённая и снятая задача дубликатом не считается: переспрос после отказа
    провайдера и новая попытка после отмены обязаны запускаться заново.
    """
    return db.query(AiTask).filter(
        AiTask.user_id == user_id,
        AiTask.kind == kind,
        AiTask.inputs_hash == inputs_fingerprint(
            kind=kind, payload=payload, diagram_id=diagram_id, base_seq=base_seq),
        AiTask.status.in_(OPEN),
        AiTask.cancel_requested.is_(False),
    ).order_by(AiTask.created_at).first()


def create_task(db: Session, *, user_id: int, kind: str, queue: str,
                payload: Optional[Dict[str, Any]] = None,
                diagram_id: Optional[str] = None,
                base_seq: Optional[int] = None,
                idempotency_key: Optional[str] = None,
                task_id: Optional[str] = None) -> AiTask:
    """Строка задачи. В очередь её ставит вызывающий код — после commit:
    поставленная раньше записи задача была бы видна воркеру, которого нет в базе.

    `task_id` задаётся вызывающим, когда идентификатор нужен до вставки: слот
    часового бюджета списывается до записи в базу (иначе отказ по лимиту
    оставался бы строкой в таблице), а его метка выводима из пары
    «пользователь + задача» — значит задача должна называть свой id заранее.
    """
    inputs_hash = inputs_fingerprint(kind=kind, payload=payload,
                                     diagram_id=diagram_id, base_seq=base_seq)
    task = AiTask(
        id=task_id or str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        queue=queue,
        status=QUEUED,
        idempotency_key=idempotency_key,
        inputs_hash=inputs_hash,
        diagram_id=diagram_id,
        base_seq=base_seq,
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
        created_at=utc_now(),
        tries=0,
        cancel_requested=False,
    )
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Гонка двух первых POST с одним ключом: оба прошли проверку до вставки,
        # и один из них лёг на `uq_ai_tasks_idempotency`. Без этой ветки
        # проигравший отвечал 500, а клиент, не дождавшийся ответа, повторил бы
        # запрос — то есть оплатил бы тот же вызов модели второй раз.
        #
        # Отдаём задачу победителя: ограничение включает отпечаток входов, так
        # что collision означает «тот же запрос», а не «ключ случайно совпал».
        # Тем же путём идёт гонка без ключа: два POST с разными ключами и
        # одинаковыми входами ловятся предикатным `uq_ai_tasks_live_inputs`, и
        # проигравший обязан вернуть живую задачу, а не упасть.
        existing = (find_by_idempotency_key(
            db, user_id, idempotency_key, kind=kind, payload=payload,
            diagram_id=diagram_id, base_seq=base_seq) if idempotency_key else None)
        if existing is None:
            existing = find_live_by_inputs(
                db, user_id, kind=kind, payload=payload,
                diagram_id=diagram_id, base_seq=base_seq)
        if existing is not None:
            logger.info("Задача с ключом %s уже поставлена (%s) — отдаём её",
                        idempotency_key, existing.id)
            return existing
        raise
    db.refresh(task)
    return task


def find_by_idempotency_key(db: Session, user_id: int, key: str, *,
                            kind: str,
                            payload: Optional[Dict[str, Any]] = None,
                            diagram_id: Optional[str] = None,
                            base_seq: Optional[int] = None) -> Optional[AiTask]:
    """Задача по ключу идемпотентности.

    Ключ без входов — не ключ, а имя экрана: `newIdempotencyKey()` создаётся на
    действие, но одно и то же значение доживает до правки описания процесса, и
    повтор с другим входом отдал бы прошлую схему с пометкой `replayed`. Поэтому
    задача находится только при совпадении отпечатка входов, а `kind` обязателен:
    без него сообщать входы нечем, и нашёлся бы любой пользовательский экран с
    этим ключом.
    """
    return db.query(AiTask).filter(
        AiTask.user_id == user_id,
        AiTask.idempotency_key == key,
        AiTask.inputs_hash == inputs_fingerprint(
            kind=kind, payload=payload, diagram_id=diagram_id, base_seq=base_seq),
    ).first()


def task_payload(task: AiTask) -> Dict[str, Any]:
    """Входы задачи. Битый JSON читаем как пустые входы, а не падаем: задача
    обязана дойти до отказа с понятной причиной, а не до исключения в воркере."""
    if not task.payload:
        return {}
    try:
        data = json.loads(task.payload)
    except ValueError:
        logger.warning("Задача %s: входы не читаются, исполняем с пустыми", task.id)
        return {}
    return data if isinstance(data, dict) else {}


# --- чтение -------------------------------------------------------------------
def load_task(db: Session, user: User, task_id: str) -> Optional[AiTask]:
    """Задача текущего пользователя. Чужой идентификатор даёт None, то есть 404:
    существование чужой задачи и её статус — не то, что нужно раскрывать."""
    return db.query(AiTask).filter(
        AiTask.id == task_id,
        AiTask.user_id == user.id,
    ).first()


def list_tasks(db: Session, user: User, *, limit: int = 50,
               status: Optional[str] = None) -> List[AiTask]:
    query = db.query(AiTask).filter(AiTask.user_id == user.id)
    if status:
        query = query.filter(AiTask.status == status)
    return query.order_by(AiTask.created_at.desc()).limit(limit).all()


# --- жизненный цикл (сторона воркера) ----------------------------------------
def claim_task(task_id: str) -> Optional[Tuple[int, str]]:
    """Перевести queued → running и вернуть (номер попытки, очередь).

    None значит «задачу уже взяли или отменили».

    Номер попытки считается по строке задачи, а не по счётчику arq: arq ведёт
    `job_try` по идентификатору job, а восстановление после падения воркера
    ставит задачу новым идентификатором — счётчик моделировал бы «первая
    попытка» на четвёртом исполнении, и лимит повторов перестал бы что-либо
    ограничивать.
    """
    with SessionLocal() as db:
        updated = db.query(AiTask).filter(
            AiTask.id == task_id,
            AiTask.status == QUEUED,
        ).update({AiTask.status: RUNNING, AiTask.started_at: utc_now(),
                  AiTask.tries: AiTask.tries + 1}, synchronize_session=False)
        db.commit()
        if not updated:
            return None
        task = db.get(AiTask, task_id)
        return int(task.tries or 1), task.queue


def cancel_requested(task_id: str) -> bool:
    """Флаг отмены. Воркер смотрит его перед каждым обращением к модели:
    прерывать уже идущий вызов значит оплатить его и выбросить ответ."""
    with SessionLocal() as db:
        task = db.get(AiTask, task_id)
        return bool(task and task.cancel_requested)


def _close(task_id: str, status: str, values: Dict[Any, Any], *,
           honor_cancel: bool = True) -> Optional[str]:
    """Терминальный переход условным обновлением. Возвращает итоговый статус.

    Писать можно только незакрытую строку: `done` поверх `cancelled` отдал бы
    результат задачи, от которой пользователь отказался, а `failed` поверх
    `done` спрятал бы успех. `rowcount` здесь — тот же замок, что и в `claim_task`.

    `honor_cancel` выключен только для `mark_cancelled`: там флаг отмены и есть
    решение пользователя, а не причина отказа.
    """
    with SessionLocal() as db:
        query = db.query(AiTask).filter(AiTask.id == task_id,
                                        AiTask.status.in_(OPEN))
        if honor_cancel:
            # Отмена, поднятая во время исполнения, важнее ответа модели:
            # вызов оплачен, но работу заказчик отменил, и статус обязан быть
            # `cancelled`, а не `done`.
            query = query.filter(~AiTask.cancel_requested)
        updated = query.update(values, synchronize_session=False)
        db.commit()
        if updated:
            return status
        row = db.query(AiTask.status).filter(AiTask.id == task_id).first()
        return row[0] if row else None


def mark_done(task_id: str, *, result: Dict[str, Any],
              result_ref: Optional[str] = None) -> Optional[str]:
    return _close(task_id, DONE, {
        AiTask.status: DONE,
        AiTask.result: json.dumps(result, ensure_ascii=False),
        AiTask.result_ref: result_ref,
        AiTask.error: None,
        AiTask.error_code: None,
        AiTask.finished_at: utc_now(),
        # Входы задачи после её закрытия не нужны ни клиенту, ни воркеру,
        # а держат они полный XML схемы. Без этой строки таблица росла бы на
        # десятки мегабайт в день и требовала отдельной чистки по расписанию.
        AiTask.payload: None,
    })


def mark_failed(task_id: str, *, error: str,
                error_code: Optional[str] = None) -> Optional[str]:
    return _close(task_id, FAILED, {
        AiTask.status: FAILED,
        AiTask.error: error,
        AiTask.error_code: error_code,
        AiTask.finished_at: utc_now(),
        AiTask.payload: None,
    })


def mark_cancelled(task_id: str) -> Optional[str]:
    return _close(task_id, CANCELLED, {
        AiTask.status: CANCELLED,
        AiTask.finished_at: utc_now(),
        AiTask.payload: None,
    }, honor_cancel=False)


def requeue(task_id: str, *, stale_after: Optional[int] = None) -> bool:
    """Вернуть задачу в очередь на повтор. False — её уже закрыли или отменили.

    Отменённая «на ходу» задача не имеет права уходить на второй круг: повтор —
    это вторая оплата вызова модели за работу, от которой отказались, а флаг
    отмены воркер обязан увидеть на закрытии, а не на постановке повтора.

    `stale_after` — защёлка для уборки потерянных задач: те же секунды, по которым
    строка была признана потерянной (`stale_tasks`). Без неё обход проигрывал
    гонку с живым воркером: между чтением списка и этим UPDATE задачу успевали
    заявить (`queued → running`), `requeue` разворачивал её обратно в `queued`,
    а второй job проходил `claim_task` заново — то есть второй полный прогон
    генерации (до шести обращений к модели) за один заказ. Свежая заявка даёт
    `started_at` позже порога, и условие не проходит.
    """
    with SessionLocal() as db:
        stale = db.query(AiTask).filter(
            AiTask.id == task_id,
            AiTask.status == RUNNING,
            ~AiTask.cancel_requested,
        )
        if stale_after is not None:
            stale = stale.filter(
                or_(AiTask.started_at.is_(None),
                    AiTask.started_at < utc_now() - timedelta(seconds=stale_after)))
        updated = stale.update({AiTask.status: QUEUED, AiTask.started_at: None},
                               synchronize_session=False)
        db.commit()
        return bool(updated)


def stale_tasks(thresholds: Dict[str, int]) -> List[Tuple[str, str, int, str]]:
    """Задачи, которые никто не ведёт: (id, очередь, попытки, статус).

    Статус возвращается нарочно: решение о `requeue` зависит от него. Строка
    `queued` в очереди уже стоит, и «вернуть» её некуда — довольно поставить
    job; `requeue` над ней был бы опасен ровно в ту миллисекунду, когда её
    заявляет воркер (см. защёлку в `requeue`).

    Считаются обе незакрытые стадии. `running` старше срока — падение процесса
    воркера или работа, убитая по таймауту: сама строка в очередь не вернётся,
    arq хранит свою копию, и при потере процесса её может не остаться. `queued`
    старше того же срока — потерянная постановка: commit строки состоялся, а
    `enqueue` нет (процесс умер между ними, Redis сбросил очередь), и ни один
    стартовый крюк об этой задаче не знает, потому что запускающий её процесс о
    ней не успел ничего узнать.

    Порог у каждой очереди свой — из таймаута её задач: скоринг схемы в
    `ai_local` иначе чистился бы по часовой мерке генерации и висел бы в
    `running` дольше, чем жил его собственный воркер.
    """
    found: List[Tuple[str, str, int, str]] = []
    with SessionLocal() as db:
        for queue, seconds in thresholds.items():
            cutoff = utc_now() - timedelta(seconds=seconds)
            found += (
                db.query(AiTask.id, AiTask.queue, AiTask.tries, AiTask.status)
                .filter(
                    AiTask.queue == queue,
                    or_(and_(AiTask.status == RUNNING,
                             or_(AiTask.started_at.is_(None),
                                 AiTask.started_at < cutoff)),
                        and_(AiTask.status == QUEUED,
                             AiTask.created_at < cutoff)),
                ).all()
            )
    return [(row.id, row.queue, int(row.tries or 0), row.status) for row in found]


def purge_closed(retention_seconds: int) -> int:
    """Удалить закрытые задачи старше срока. Возвращает число удалённых строк.

    Терминальные строки — единственные, которые больше никто не прочтёт: воркер
    их не берёт, опрос по ним не делается, а `result` несёт полный XML схемы.
    Срок взят от времени жизни ответа модели (app.config): пока кэш жив, повтор
    запроса отдаётся из него и строка задачи нужна как история, а после он не
    объясняет уже ничего. Без этой чистки таблица росла бы бесконечно, и
    `payload` на закрытии был бы единственной экономией в ней.

    Неположительный срок — «не чистить», а не «стереть всё». Он берётся из
    `LLM_CACHE_TTL`, а ноль там штатное значение «кэш выключен»
    (core/llm_cache.py трактует `ttl <= 0` именно так): без этой проверки
    включённый disable кэша превращался бы в стирание всей истории задач каждые
    две минуты, включая строки, чей результат клиент ещё собирается посмотреть.
    """
    if retention_seconds <= 0:
        return 0
    cutoff = utc_now() - timedelta(seconds=retention_seconds)
    with SessionLocal() as db:
        deleted = (db.query(AiTask)
                   .filter(AiTask.status.in_(TERMINAL),
                           AiTask.finished_at < cutoff)
                   .delete(synchronize_session=False))
        db.commit()
        return int(deleted)


def status_counts(db: Session, *, hours: int = 24,
                  user_id: Optional[int] = None) -> Dict[str, int]:
    """Сколько задач в каждом статусе за последние часы — операционная метрика
    «очередь растёт» без отдельного мониторинга.

    Считает база одним `GROUP BY`, а не Python по выбранной таблице: строка за
    сутки — это перенесённые в процесс мегабайты (в `result` лежит XML схемы), и
    посчёт по ним был бы ещё и трафиком, а не только цифрой.
    """
    since = utc_now() - timedelta(hours=hours)
    query = (db.query(AiTask.status, func.count(AiTask.id))
             .filter(AiTask.created_at >= since)
             .group_by(AiTask.status))
    if user_id is not None:
        query = query.filter(AiTask.user_id == user_id)
    counts = dict(query.all())
    return {status: int(counts.get(status, 0))
            for status in (QUEUED, RUNNING, DONE, FAILED, CANCELLED)}


def _descriptor(task: AiTask) -> Dict[str, Any]:
    """Поля дескриптора, общие для опроса и истории."""
    started = task.started_at
    finished = task.finished_at
    return {
        "task_id": task.id,
        "kind": task.kind,
        "queue": task.queue,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "queued_seconds": round((started - task.created_at).total_seconds(), 3)
        if started and task.created_at else None,
        "run_seconds": round((finished - started).total_seconds(), 3)
        if finished and started else None,
        "tries": int(task.tries or 0),
        "cancel_requested": bool(task.cancel_requested),
        "error": task.error,
        "error_code": task.error_code,
        "result_ref": task.result_ref,
        # Есть ли что читать отдельным запросом: история `result` не несёт, и
        # по этому полю клиент решает, нужен ли лишний обход к задаче.
        "has_result": bool(task.result),
        # Подсказка клиенту, когда опрашивать снова. Замедление опроса (2 с → 5 с)
        # делает фронтенд: серверу число уже состоявшихся опросов неизвестно, и
        # выдумывать его здесь значило бы врать в контракте.
        "poll_after": None if task.status in TERMINAL else TASK_POLL_SECONDS,
    }


def describe_task(task: AiTask, *, with_result: bool = True) -> Dict[str, Any]:
    """Ответ опроса задачи: статус, сроки и результат.

    Время в очереди и время исполнения отдаются отдельными числами, а не только
    метками времени: «задача стояла четыре минуты» и «задача считалась четыре
    минуты» — разные проблемы (мощность воркера против тарифа провайдера), и по
    одним timestamp'ам клиент их не различает.

    `with_result=False` — вариант списка/истории: результат там не смотрят, а
    весит он до `AI_MAX_XML_CHARS` на строку, то есть история на 50 задач
    вывозила бы мегабайты вместо статусов.
    """
    descriptor = _descriptor(task)
    result: Optional[Dict[str, Any]] = None
    if with_result and task.result:
        try:
            result = json.loads(task.result)
        except ValueError:
            logger.warning("Задача %s: результат не читается как JSON", task.id)
    descriptor["result"] = result
    return descriptor

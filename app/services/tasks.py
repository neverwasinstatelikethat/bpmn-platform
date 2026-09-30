"""Состояние задач ИИ-контура в базе.

Redis — транспорт, база — источник истины (план
docs/plans/redis-cache-and-task-routing.md, раздел 4.3). Отсюда два правила,
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

import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
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


# --- постановка ---------------------------------------------------------------
def create_task(db: Session, *, user_id: int, kind: str, queue: str,
                payload: Optional[Dict[str, Any]] = None,
                diagram_id: Optional[str] = None,
                base_seq: Optional[int] = None,
                idempotency_key: Optional[str] = None) -> AiTask:
    """Строка задачи. В очередь её ставит вызывающий код — после commit:
    поставленная раньше записи задача была бы видна воркеру, которого нет в базе."""
    task = AiTask(
        id=str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        queue=queue,
        status=QUEUED,
        idempotency_key=idempotency_key,
        diagram_id=diagram_id,
        base_seq=base_seq,
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
        created_at=utc_now(),
        tries=0,
        cancel_requested=False,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def find_by_idempotency_key(db: Session, user_id: int,
                            key: str) -> Optional[AiTask]:
    return db.query(AiTask).filter(
        AiTask.user_id == user_id,
        AiTask.idempotency_key == key,
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


def mark_done(task_id: str, *, result: Dict[str, Any],
              result_ref: Optional[str] = None) -> None:
    with SessionLocal() as db:
        db.query(AiTask).filter(AiTask.id == task_id).update({
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
        }, synchronize_session=False)
        db.commit()


def mark_failed(task_id: str, *, error: str, error_code: Optional[str] = None) -> None:
    with SessionLocal() as db:
        db.query(AiTask).filter(AiTask.id == task_id).update({
            AiTask.status: FAILED,
            AiTask.error: error,
            AiTask.error_code: error_code,
            AiTask.finished_at: utc_now(),
            AiTask.payload: None,
        }, synchronize_session=False)
        db.commit()


def mark_cancelled(task_id: str) -> None:
    with SessionLocal() as db:
        db.query(AiTask).filter(AiTask.id == task_id).update({
            AiTask.status: CANCELLED,
            AiTask.finished_at: utc_now(),
            AiTask.payload: None,
        }, synchronize_session=False)
        db.commit()


def requeue(task_id: str) -> bool:
    """Вернуть задачу в очередь на повтор. False — её уже закрыли (например,
    отменили, пока воркер решал, повторять ли сбой)."""
    with SessionLocal() as db:
        updated = db.query(AiTask).filter(
            AiTask.id == task_id,
            AiTask.status == RUNNING,
        ).update({AiTask.status: QUEUED, AiTask.started_at: None},
                 synchronize_session=False)
        db.commit()
        return bool(updated)


def stale_tasks(stale_seconds: int) -> List[Tuple[str, str, int]]:
    """Задачи, которые воркер начал и не закончил: (id, очередь, попытки).

    `running` без живого воркера — это либо падение процесса, либо задача,
    которую убили по таймауту. Сама по себе строка в базе в очередь не
    вернётся: arq хранит свою копию, и при потере процесса её может не
    остаться. Поэтому список отдаётся наружу, и стартовый крюк воркера
    решает для каждой задачи — вернуть в очередь или закрыть ошибкой.
    """
    cutoff = utc_now() - timedelta(seconds=stale_seconds)
    with SessionLocal() as db:
        rows = db.query(AiTask).filter(
            AiTask.status == RUNNING,
            or_(AiTask.started_at.is_(None), AiTask.started_at < cutoff),
        ).all()
        return [(row.id, row.queue, int(row.tries or 0)) for row in rows]


def status_counts(db: Session, *, hours: int = 24) -> Dict[str, int]:
    """Сколько задач в каждом статусе за последние часы — операционная метрика
    «очередь растёт» без отдельного мониторинга."""
    since = utc_now() - timedelta(hours=hours)
    counts = {status: 0 for status in (QUEUED, RUNNING, DONE, FAILED, CANCELLED)}
    rows = (db.query(AiTask.status, AiTask.id)
            .filter(AiTask.created_at >= since).all())
    for status, _task_id in rows:
        if status in counts:
            counts[status] += 1
    return counts


def describe_task(task: AiTask) -> Dict[str, Any]:
    """Ответ опроса задачи: статус, сроки и результат.

    Время в очереди и время исполнения отдаются отдельными числами, а не только
    метками времени: «задача стояла четыре минуты» и «задача считалась четыре
    минуты» — разные проблемы (мощность воркера против тарифа провайдера), и
    по одним timestamp'ам клиент их не различает.
    """
    started = task.started_at
    finished = task.finished_at
    result = None
    if task.result:
        try:
            result = json.loads(task.result)
        except ValueError:
            logger.warning("Задача %s: результат не читается как JSON", task.id)
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
        "result": result,
        # Подсказка клиенту, когда опрашивать снова. Замедление опроса (2 с → 5 с)
        # делает фронтенд: серверу число уже состоявшихся опросов неизвестно, и
        # выдумывать его здесь значило бы врать в контракте.
        "poll_after": None if task.status in TERMINAL else TASK_POLL_SECONDS,
    }

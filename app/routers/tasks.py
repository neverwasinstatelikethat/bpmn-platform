"""Опрос, история и отмена задач ИИ-контура.

Маршруты читают только базу: Redis — транспорт очереди, и статус задачи не
должен зависеть от его доступности (план
docs/plans/redis-cache-and-task-routing.md, раздел 4.3). Поэтому опрос работает
и при упавшем Redis — пользователь видит «задача в очереди», а не 503.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.services import ai_cache
from app.services import tasks as task_store
from app.tasks import broker_name, current_broker

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/tasks/stats")
async def task_stats(current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Операционные счётчики контура: глубина очередей, попадания в кэш, задачи.

    Отдельный мониторинг у платформы не появился бы раньше, чем контур стал бы
    узким местом, а без этих трёх чисел «очередь растёт» и «кэш не попадает»
    неотличимы друг от друга на глаз.
    """
    broker = current_broker()
    depth = await broker.depth() if broker is not None else {}
    # Клиент кэша синхронный (он же работает из потоков воркера), поэтому
    # счётчики читаются вне цикла событий: один вызов Redis короткий, но
    # держать на нём ивент-цикл маршрута незачем.
    cache = await run_in_threadpool(ai_cache.cache_stats)
    return {
        "broker": broker_name(),
        "queue_depth": depth,
        "cache": cache,
        "tasks_last_24h": task_store.status_counts(db),
    }


@router.get("/api/tasks")
def list_tasks(status: str | None = Query(default=None, max_length=16),
               limit: int = Query(default=50, ge=1, le=200),
               current_user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """История задач пользователя: что заказывали, чем кончилось."""
    rows = task_store.list_tasks(db, current_user, limit=limit, status=status)
    return {"tasks": [task_store.describe_task(row) for row in rows]}


@router.get("/api/tasks/{task_id}")
def task_state(task_id: str,
               current_user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    task = task_store.load_task(db, current_user, task_id)
    if task is None:
        # Чужой идентификатор неотличим от несуществующего: наличие и статус
        # чужой задачи раскрывать незачем.
        raise HTTPException(404, detail="Task not found")
    return task_store.describe_task(task)


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str,
                current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Отмена задачи.

    Из очереди задача снимается сразу, а у исполняемой только поднимается флаг:
    воркер смотрит его перед каждым обращением к модели. Прерывать уже идущий
    вызов провайдера бессмысленно — он оплачен, а ответа на него не будет, то
    есть отмена mid-flight тратит ровно те деньги, которые должна экономить.

    Повтор отмены идемпотентен: уже отменённая задача отвечает тем же статусом,
    а не 404 и не 409 — клиент, опрашивающий задачу, не должен различать «я
    отменил» и «я отменил дважды».
    """
    task = task_store.load_task(db, current_user, task_id)
    if task is None:
        raise HTTPException(404, detail="Task not found")

    if task.status in (task_store.DONE, task_store.FAILED):
        raise HTTPException(
            409, detail=f"Задача уже завершена ({task.status}) — отменять нечего")

    if task.status == task_store.CANCELLED:
        return task_store.describe_task(task)

    if task.status == task_store.QUEUED:
        # До исполнения задача не дойдёт: заявка воркера — условный переход
        # queued → running, и на отменённой строке он не состоится.
        task_store.mark_cancelled(task_id)
        logger.info("Задача %s отменена до исполнения", task_id)
    else:
        task.cancel_requested = True
        db.commit()
        logger.info("Задача %s исполняется — запрошена отмена", task_id)

    db.refresh(task)
    return task_store.describe_task(task)

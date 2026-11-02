"""Опрос, история и отмена задач ИИ-контура.

Маршруты читают только базу: Redis — транспорт очереди, и статус задачи не
должен зависеть от его доступности (план
redis-cache-and-task-routing, раздел 4.3). Поэтому опрос работает
и при упавшем Redis — пользователь видит «задача в очереди», а не 503.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import User
from app.services import ai_cache
from app.services import tasks as task_store
from app.services.access import MANAGE_ROLES_PERMISSION, team_ids_of
from app.tasks import (QUEUE_LLM, broker_name, current_broker, queue_of,
                       _refund_if_never_started)

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_operator(db: Session, user: User) -> bool:
    """Оператор контура — тот, у кого есть право `manageRoles` хоть в одной
    команде.

    Отдельную роль под операционные цифры выдумывать нечем: `manageRoles` — уже
    существующее в продукте самое сильное право (app/services/access.py), и
    означает оно «человек отвечает за то, чем контур нагружают».
    """
    return bool(team_ids_of(db, user, permission=MANAGE_ROLES_PERMISSION))


def _stats_from_db(user: User) -> Tuple[bool, Dict[str, int]]:
    """Операторство и счётчики одним заходом.

    Оба чтения — синхронные, а маршрут асинхронный (ему ждать `broker.depth()`),
    поэтому база читается в потоке пула: на одном чтении статуса ивент-луп
    воркера не висит.
    """
    with SessionLocal() as db:
        operator = _is_operator(db, user)
        counts = task_store.status_counts(db, user_id=None if operator else user.id)
    return operator, counts


@router.get("/api/tasks/stats")
async def task_stats(current_user: User = Depends(get_current_user)):
    """Операционные счётчики контура: глубина очередей, попадания в кэш, задачи.

    Отдельный мониторинг у платформы не появился бы раньше, чем контур стал бы
    узким местом, а без этих трёх чисел «очередь растёт» и «кэш не попадает»
    неотличимы друг от друга на глаз.

    Цифры всего контура — не то, что видно пользователю из его интерфейса:
    длина очереди и статусы чужих задач говорят о загрузке и о том, чем заняты
    другие. Поэтому глобальные числа отдаются оператору (`_is_operator`), а
    обычный пользователь получает свою собственную статистику — ровно то, о чём
    он и так судит по истории своих задач. `operator: false` в ответе объясняет,
    почему глобальных чисел нет, а не выглядит забытым полем.
    """
    operator, counts = await run_in_threadpool(_stats_from_db, current_user)
    # Клиент кэша синхронный (он же работает из потоков воркера), поэтому
    # счётчики читаются вне цикла событий: один вызов Redis короткий, но
    # держать на нём ивент-цикл маршрута незачем.
    cache = await run_in_threadpool(ai_cache.cache_stats)
    body: Dict[str, Any] = {
        "broker": broker_name(),
        "operator": operator,
        "tasks_last_24h": counts,
        # Включён ли кэш — свойство запуска, а не чужая работа; count'ы
        # попаданий на весь контур оператору.
        "cache": cache if operator else {"enabled": bool(cache.get("enabled"))},
    }
    if operator:
        broker = current_broker()
        body["queue_depth"] = await broker.depth() if broker is not None else {}
    return body


@router.get("/api/tasks")
def list_tasks(status: str | None = Query(default=None, max_length=16),
               limit: int = Query(default=50, ge=1, le=200),
               current_user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """История задач пользователя: что заказывали, чем кончилось.

    Результат сюда не кладётся: в `result` лежит полный XML схемы (до
    `AI_MAX_XML_CHARS` на строку), а страница истории статусов и причин отказа.
    По `has_result` клиент видит, какую задачу стоит прочесть отдельно через
    `GET /api/tasks/{task_id}`.
    """
    rows = task_store.list_tasks(db, current_user, limit=limit, status=status)
    return {"tasks": [task_store.describe_task(row, with_result=False)
                      for row in rows]}


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


def _cancel_in_db(user_id: int,
                  task_id: str) -> Tuple[Optional[str], Dict[str, Any], bool]:
    """Закрыть заявку в потоке пула: `(исход, тело ответа, нужен ли возврат слота)`.

    Маршрут асинхронным стал затем, чтобы возвращать слот через Redis, — и ровно
    поэтому блокированная работа с базой не должна оставаться в цикле событий:
    запись статуса и чтение строки вешали бы ивент-луп, а сессия запроса держала
    бы соединение пула через `await`. `user` перезагружается по id: экземпляр,
    привязанный к сессии другого потока, SQLAlchemy в эту сессию не переносит.
    """
    with SessionLocal() as db:
        task = task_store.load_task(db, db.get(User, user_id), task_id)
        if task is None:
            return None, {}, False
        if task.status in (task_store.DONE, task_store.FAILED):
            return "closed", {"status": task.status}, False
        if task.status == task_store.CANCELLED:
            return "ok", task_store.describe_task(task), False
        refund_needed = False
        if task.status == task_store.QUEUED:
            # До исполнения задача не дойдёт: заявка воркера — условный переход
            # queued → running, и на отменённой строке он не состоится.
            closed = task_store.mark_cancelled(task_id)
            logger.info("Задача %s отменена клиентом", task_id)
            refund_needed = (closed == task_store.CANCELLED
                             and queue_of(task.kind) == QUEUE_LLM)
        else:
            task.cancel_requested = True
            db.commit()
            logger.info("Задача %s исполняется — запрошена отмена", task_id)
        db.refresh(task)
        return "ok", task_store.describe_task(task), refund_needed


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str,
                      current_user: User = Depends(get_current_user)):
    """Отмена задачи.

    Из очереди задача снимается сразу, а у исполняемой только поднимается флаг:
    воркер смотрит его перед каждым обращением к модели. Прерывать уже идущий
    вызов провайдера бессмысленно — он оплачен, а ответа на него не будет, то
    есть отмена mid-flight тратит ровно те деньги, которые должна экономить.

    Повтор отмены идемпотентен: уже отменённая задача отвечает тем же статусом,
    а не 404 и не 409 — клиент, опрашивающий задачу, не должен различать «я
    отменил» и «я отменил дважды».
    """
    outcome, body, refund_needed = await run_in_threadpool(_cancel_in_db,
                                                           current_user.id, task_id)
    if outcome is None:
        raise HTTPException(404, detail="Task not found")
    if outcome == "closed":
        raise HTTPException(
            409, detail=f"Задача уже завершена ({body['status']}) — отменять нечего")

    if refund_needed:
        # Слот часового бюджета снимается здесь, а не только в воркере.
        # Воркер за этой строкой может и не прийти: Redis лёг между
        # постановкой и заявкой, job потерян, воркер не поднят — а строка
        # уже терминальная, и уборка потерянных видит только незакрытые.
        # Без этого отменённая заявка держала бы лимит часа до его конца,
        # то есть отказ по 429 за работу, которой не было.
        #
        # Мера возврата — `tries` внутри `_refund_if_never_started`, а не
        # прочитанный в потоке статус: закрытие принимает ЛЮБУЮ открытую строку,
        # и в окне между чтением и ним заявка `queued → running` успевает
        # состояться — безусловный возврат отдал бы слот за обращение, которое
        # воркер уже оплачивает.
        await _refund_if_never_started(task_id)
    return body

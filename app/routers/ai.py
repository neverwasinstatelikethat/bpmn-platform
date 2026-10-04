"""ИИ-контур: генерация, скоринг, улучшение и принятие изменений.

Долгие вызовы модели в запросе больше не исполняются. Маршрут создаёт задачу,
ставит её в очередь и отвечает `202` с идентификатором; результат читается
опросом `GET /api/tasks/{task_id}` (план
docs/plans/redis-cache-and-task-routing.md, раздел 4). Совместимость со старым
синхронным контрактом не поддерживается намеренно: продукт внутренний, а два
контракта на одном маршруте означали бы, что ни один из них не проверяется
честно.

Ответ постановки несёт состояние задачи. В режиме исполнения в процессе (без
Redis) задача завершается до возврата ответа, и результат приходит сразу —
дополнительный опрос клиенту не нужен.

Принятие и отклонение улучшения остаются синхронными: это запись в базу и
пересчёт балла, секунд они не занимают, а пользователю нужен немедленный ответ
«версия сохранена».
"""
import asyncio
import contextlib
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import ai as ai_components
from app.db import SessionLocal, get_db
from app.deps import (RETRY_AFTER_HEADER, consume_ai_budget, get_ai_user,
                      get_current_user, release_ai_budget)
from app.models import AiTask, Diagram, Improvement, User
from app.schemas import (AcceptImprovementRequest, EvaluateRequest,
                         GenerateRequest, ImproveRequest)
from app.services.access import load_diagram
from app.services.improvements import APPROVED, PENDING, REJECTED
from app.services import tasks as task_store
from app.services.versions import record_version
from app.tasks import (GENERATE, IMPROVE, QUEUE_LLM, SCORE, get_broker,
                       queue_of, _refund_if_never_started)
from app.timeutils import utc_now
from core.bpmn_scoring import diff_scores

logger = logging.getLogger(__name__)

router = APIRouter()

_RETRY_AFTER_UNAVAILABLE = {RETRY_AFTER_HEADER: "30"}


async def _submit(kind: str, *, user: User, payload: Dict[str, Any],
                  diagram_id: Optional[str] = None,
                  base_seq: Optional[int] = None,
                  idempotency_key: Optional[str] = None,
                  needs_orchestrator: bool = False) -> JSONResponse:
    """Создать задачу и отдать её брокеру.

    Порядок проверок важен и повторяет уже установленное правило контура:
    доступ — до состояния сервиса, состояние сервиса — до бюджета. Иначе
    перебор чужих `diagram_id` отвечал бы 503 и 429 раньше 404, и сам отказ в
    доступе становился бы способом исчерпать чужой часовой лимит.
    """
    broker = await get_broker()
    if broker is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Очередь задач недоступна. Попробуйте позже.",
            headers=_RETRY_AFTER_UNAVAILABLE,
        )
    # В режиме очереди тяжёлые модели держит воркер, и веб-процессу проверять
    # их наличие нечем. В режиме исполнения в процессе задача выполняется здесь
    # же, поэтому отсутствие оркестратора — честные 503, а не задача, которая
    # упадёт через минуту с тем же ответом.
    if needs_orchestrator and broker.name == "inline" \
            and ai_components.get_orchestrator() is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Сервис улучшения недоступен: оркестратор не поднят "
                    "(SKIP_LLM_INIT=1 или сбой его сборки на старте)"),
            headers=_RETRY_AFTER_UNAVAILABLE,
        )

    if idempotency_key:
        with SessionLocal() as db:
            existing = task_store.find_by_idempotency_key(
                db, user.id, idempotency_key, kind=kind, payload=payload,
                diagram_id=diagram_id, base_seq=base_seq)
            if existing is not None:
                # Повтор отправки не списывает бюджет и не создаёт вторую задачу:
                # клиент, не дождавшийся ответа, иначе оплатил бы тот же вызов
                # модели дважды.
                logger.info("Ключ %s уже использован задачей %s — отдаём её",
                            idempotency_key, existing.id)
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={**task_store.describe_task(existing), "replayed": True})

    # Тот же запрос под другим ключом: фронтенд выдаёт новый ключ на каждое
    # действие, поэтому двойное нажатие и повтор после сетевого обрыва приходят
    # разными ключами и выше не ловятся. Проверка стоит ДО списания бюджета —
    # иначе дубль снимал бы слот часовой лимита за работу, которую не запускает.
    with SessionLocal() as db:
        live = task_store.find_live_by_inputs(
            db, user.id, kind=kind, payload=payload, diagram_id=diagram_id,
            base_seq=base_seq)
        if live is not None:
            logger.info("Та же работа уже живёт (%s, kind=%s) — отдаём её",
                        live.id, kind)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={**task_store.describe_task(live), "replayed": True})

    # Идентификатор задачи известен до вставки: слот бюджета списывается здесь,
    # а возвращается воркером, который видит только `task_id`. Метка слота
    # выводима из пары «пользователь + задача» (`app/deps.py:budget_slot`), и
    # только так возврат происходит в другом процессе — без сериализации ручек
    # в очередь.
    #
    # Бюджет меряет обращения к провайдеру (`AI_REQUESTS_PER_HOUR` описан этим
    # основанием в app/config.py), поэтому очередь `ai_local` его не списывает:
    # скоринг схемы не спрашивает модель ни при каком раскладе, и шестьдесят
    # оценок заперли бы пользователю час на генерацию с нулём оплаченных
    # обращений — ровно тот худший случай, от которого защищает возврат слота.
    budget_task_id = str(uuid.uuid4())
    spends_budget = queue_of(kind) == QUEUE_LLM
    task_id = budget_task_id
    try:
        if spends_budget:
            await consume_ai_budget(user, task_id=budget_task_id)
        with SessionLocal() as db:
            task = task_store.create_task(db, user_id=user.id, kind=kind,
                                          queue=queue_of(kind), payload=payload,
                                          diagram_id=diagram_id, base_seq=base_seq,
                                          idempotency_key=idempotency_key,
                                          task_id=budget_task_id)
            task_id = task.id
    except HTTPException:
        # Отказ по часовому лимиту — не сбой заведения задачи: метка слота
        # при 429 не добавлялась (её пишут только при успешном списании), так что
        # возвращать здесь нечего, а строка в ERROR на штатный ответ
        # `too many requests` превращает журнал оператора в ложную аварию.
        raise
    except asyncio.CancelledError:
        # Обрыв соединения на вставке — не поломка заведения: клиент ушёл, и
        # читать ответ будет некому. Слот возвращается (провайдера не спрашивали,
        # и возвращать его больше некому), а в журнал идёт INFO, а не ERROR:
        # штатные отмены на потоковом журнале выглядели бы аварией заведения и
        # тонили настоящую поломку — тот же класс, что с ложным ERROR на 429.
        logger.info("Постановка задачи %s отменена клиентом: слот возвращён",
                    budget_task_id)
        if spends_budget:
            with contextlib.suppress(asyncio.CancelledError):
                await release_ai_budget(user.id, task_id=budget_task_id)
        raise
    except BaseException as error:  # noqa: BLE001 — и отмена клиентом тоже
        # Задачи нет (или её не успели завести), а бюджет уже списан: без
        # возврата каждая такая ошибка съедала бы часовой лимит за работу,
        # которая не начата и не будет. `BaseException`, а не `Exception`,
        # потому что `CancelledError` таковым не является — обрыв соединения
        # между списанием и вставкой уходил бы, оставив слот занятым навек.
        if spends_budget:
            # Возврат идёт в уже отменяемой задаче, и обычный `await` под
            # `CancelledError` вспухает вторым `CancelledError` до того, как
            # Redis что-то увидит.
            with contextlib.suppress(asyncio.CancelledError):
                await release_ai_budget(user.id, task_id=budget_task_id)
        # В лог — тип, а не текст исключения: SQLAlchemy вшивает в сообщение
        # `[SQL: INSERT INTO ai_tasks ...][parameters: ...]`, где параметры
        # содержат полный XML схемы (до `AI_MAX_XML_CHARS`). Трейс остаётся на
        # DEBUG, куда клиентские тела не ходят и без нас.
        logger.error("Задачу %s не удалось завести (kind=%s): %s",
                     budget_task_id, kind, type(error).__name__)
        logger.debug("Причина заведения задачи %s", budget_task_id, exc_info=True)
        raise

    if task_id != budget_task_id:
        # Гонка идемпотентности: вставка отдала задачу победителя, а слот был
        # списан под меткой нашего идентификатора. Он больше ни к чему не
        # привязан, и держать его списанным значило бы съесть часовой бюджет
        # за работу, которую заказывает другой запрос.
        await release_ai_budget(user.id, task_id=budget_task_id)
        # И здесь же место проигравшему: победителя в очередь поставил его
        # запрос. Перепоставка отсюда дала бы второй полный прогон за один
        # заказ (до шести обращений к модели), а ветка отказа ниже закрыла бы
        # живую строку победителя как `queue_unavailable` — её уже оплаченный
        # результат некуда записать, потому что `_close` терминальный статус не
        # трогает. Если у победителя оборвалось соединение до постановки,
        # строка не потеряна: её поднимает уборка потерянных постановок
        # (`stale_tasks` читает и `queued`).
        with SessionLocal() as db:
            body = task_store.describe_task(db.get(AiTask, task_id))
        body["replayed"] = True
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    try:
        await broker.submit(kind, task_id)
    except asyncio.CancelledError:
        # Та же дыра, что и выше, но на шаге позже: `except Exception` отмену
        # не видит (`CancelledError` — не `Exception`), и обрыв соединения ровно
        # во время постановки оставлял строку `queued` со списанным слотом.
        # Дальше два плохих исхода: job уже лёг в Redis и модель спрашивается для
        # заказчика, который ушёл, либо строка доживает до уборки потерянных
        # постановок и перепоставляется оттуда — again с оплатой прогона.
        # Закрытие условное: если воркер успел заявить задачу, переход не
        # состоится, и решением остаётся флаг отмены (`_execute` увидит его перед
        # первым обращением).
        logger.info("Постановка задачи %s отменена клиентом: строка закрыта, "
                    "слот — по признаку начала работы", task_id)
        task_store.mark_cancelled(task_id)
        with contextlib.suppress(asyncio.CancelledError):
            # Возврат не безусловный: `InlineBroker.submit` исполняет задачу ЦЕЛИКОМ
            # внутри этого же `await`, поэтому обрыв соединения после обращения к
            # модели вернул бы оплаченный прогон — и повторимо, что обнуляет
            # часовой лимит. `_execute` на той же ветке возврат осознанно
            # пропускает. `tries` считает заявки на исполнение, поэтому ноль здесь
            # доказательство, а не догадка.
            await _refund_if_never_started(task_id)
        raise
    except Exception as error:  # noqa: BLE001 — очередь могла лечь между ping и постановкой
        logger.error("Задачу %s не удалось поставить в очередь: %s", task_id, error)
        task_store.mark_failed(task_id, error="Очередь задач недоступна",
                               error_code="queue_unavailable")
        # Та же мера, что и выше: «обращения не было» здесь утверждает не тип
        # выброса, а отсутствие заявки на исполнение.
        await _refund_if_never_started(task_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Очередь задач недоступна. Попробуйте позже.",
            headers=_RETRY_AFTER_UNAVAILABLE,
        ) from error

    with SessionLocal() as db:
        body = task_store.describe_task(db.get(AiTask, task_id))
    body["replayed"] = False
    # 202 и при уже завершённой задаче (режим исполнения в процессе): клиенту
    # один код и один разбор ответа, а факт завершения виден в поле status.
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)


@router.post("/api/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_bpmn(request: GenerateRequest,
                        current_user: User = Depends(get_ai_user)):
    """Генерация схемы по описанию процесса — задача очереди `ai_llm`."""
    return await _submit(GENERATE, user=current_user, payload={"text": request.text},
                         idempotency_key=request.idempotency_key)


@router.post("/api/evaluate", status_code=status.HTTP_202_ACCEPTED)
async def evaluate_bpmn(request: EvaluateRequest,
                        current_user: User = Depends(get_ai_user)):
    """Скоринг схемы — задача очереди `ai_local`.

    Правило детерминированное и внешних вызовов не делает, поэтому очередь
    отдельная: крупная схема на миллион символов не должна стоять за чужим
    обращением к провайдеру и занимать его тарифный слот.
    """
    return await _submit(SCORE, user=current_user,
                         payload={"bpmn_xml": request.bpmn_xml},
                         idempotency_key=request.idempotency_key)


@router.post("/api/ai/improve", status_code=status.HTTP_202_ACCEPTED)
async def improve_diagram_endpoint(
    request: ImproveRequest,
    current_user: User = Depends(get_ai_user),
):
    # Соединение с БД не держится на всё время запроса: короткие сеансы —
    # проверка доступа до постановки, чтение результата после.
    #
    # Предложение привязывается к хранённой схеме: оно снимает в статус
    # superseded чужие незакрытые предложения по ней и отсчитывается от её
    # версии. Значит здесь нужен доступ на правку, а не на просмотр: чистый
    # анализ схемы без привязки вызывается без diagram_id. Заодно вымышленный
    # id превращается из 500 на внешнем ключе в 404.
    base_seq: Optional[int] = None
    if request.diagram_id:
        with SessionLocal() as db:
            diagram = load_diagram(db, current_user, request.diagram_id, edit=True)
            base_seq = diagram.version_seq

    # XML берётся из запроса, а не из базы, даже при указанной схеме: улучшают
    # то, что на экране, включая несохранённые правки. В Redis при этом уходит
    # один идентификатор задачи — содержимое лежит в `ai_tasks.payload`.
    return await _submit(IMPROVE, user=current_user,
                         payload={"bpmn_xml": request.bpmn_xml,
                                  "prompt": request.prompt},
                         diagram_id=request.diagram_id, base_seq=base_seq,
                         idempotency_key=request.idempotency_key,
                         needs_orchestrator=True)


@router.post("/api/ai/accept-improvement")
async def accept_improvement(
    request: AcceptImprovementRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Строка берётся под блокировкой: два параллельных принятия одного
    # предложения иначе создали бы две версии, и вторая затёрла бы первую.
    improvement = db.query(Improvement).filter(
        Improvement.id == request.improvement_id,
        Improvement.user_id == current_user.id,
        Improvement.status == PENDING,
    ).with_for_update().first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")

    previous_xml: Optional[str] = None
    if improvement.diagram_id:
        diagram = load_diagram(db, current_user, improvement.diagram_id, edit=True)
        if improvement.base_seq is not None and diagram.version_seq != improvement.base_seq:
            # Правки считались по другой версии: принятие молча затёрло бы то,
            # что сохранилось после расчёта улучшения.
            raise HTTPException(
                409,
                detail="Схема изменилась после расчёта улучшения — запросите его заново",
            )
        # Снимок «до» берётся до перезаписи: иначе дельта по правилам всегда
        # пустая, и пользователь не видит, что принятое изменение ухудшило
        # схему.
        previous_xml = diagram.xml_content
        diagram.xml_content = improvement.xml_content
        diagram.updated_at = utc_now()
    else:
        diagram = Diagram(
            id=str(uuid.uuid4()),
            name="Улучшенная диаграмма",
            xml_content=improvement.xml_content,
            user_id=current_user.id,
            created_at=utc_now(),
            updated_at=utc_now()
        )
        db.add(diagram)

    # Балл пересчитываем по новому XML. Предложение может и ухудшить схему
    # (шаг остался вне маршрута), а старый балл иначе остался бы и в реестре,
    # и в истории версий — то есть вся статистика качества врала бы после
    # каждого принятия.
    score_before = diagram.score
    after_report = await asyncio.to_thread(ai_components.scorer.evaluate,
                                           improvement.xml_content)
    score_after = after_report["score"]
    diagram.score = score_after

    # Дельта по правилам, а не только итоговая цифра: «85 → 85» выглядит как
    # бесполезное улучшение, хотя три правила починены, а прирост съеден
    # новым требованием к документации.
    rules_delta = None
    if previous_xml:
        rules_delta = diff_scores(
            await asyncio.to_thread(ai_components.scorer.evaluate, previous_xml),
            after_report)

    version = record_version(db, diagram, source="approved", author_id=current_user.id,
                             note=f"улучшение {improvement.id}")
    improvement.status = APPROVED
    improvement.decided_at = utc_now()
    db.commit()
    return {
        "status": "success",
        "diagram_id": diagram.id,
        "xml_content": diagram.xml_content,
        "version_seq": version.seq if version else diagram.version_seq,
        "score": score_after,
        "score_before": score_before,
        "rules_delta": rules_delta,
    }


@router.post("/api/ai/reject-improvement")
async def reject_improvement(
    request: AcceptImprovementRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Отказ от предложения: решение остаётся в истории, строка не удаляется."""
    improvement = db.query(Improvement).filter(
        Improvement.id == request.improvement_id,
        Improvement.user_id == current_user.id,
        Improvement.status == PENDING,
    ).with_for_update().first()
    if not improvement:
        raise HTTPException(404, detail="Improvement not found")

    improvement.status = REJECTED
    improvement.decided_at = utc_now()
    db.commit()
    return {"status": "success", "improvement_id": improvement.id}

"""Составной корень BPMN-платформы: приложение, middleware и маршруты.

Тяжёлая инициализация (схема БД, предопределённые роли, LLM-оркестратор)
выполняется в lifespan, а не на импорте модуля: импорт пакета не должен писать
в базу и поднимать модели эмбеддингов.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.ai import get_orchestrator, init_orchestrator
from app.config import BACKEND_PORT, CORS_ORIGINS, SKIP_LLM_INIT
from app.db import engine
from app.deps import (RETRY_AFTER_HEADER, budget_backend, budget_degraded,
                      budget_retry_in_seconds, close_ai_budget, init_ai_budget,
                      worker_liveness)
from app.routers import ai, auth, diagrams, folders, sharing, tasks, teams
from app.request_context import (REQUEST_ID_HEADER, configure_logging,
                                 current_request_id, reset_request_id,
                                 use_request_id)
from app.services.ai_cache import close_ai_cache, init_ai_cache
from app.startup import prepare_database
from app.tasks import (QUEUE_LLM, QUEUE_LOCAL, broker_name, close_broker,
                       get_broker)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Логгер настраивается здесь, а не на импорте модуля: `import app.main` из
    # тестов или чужого процесса не должен трогать обработчики корня. Под
    # `uvicorn main:app` корневой логгер не настроен вовсе, и INFO-строки
    # старта (режим очереди, backend бюджета, прогрев корпуса эталонов) в
    # `docker logs` не появлялись: печатался только WARNING, да и тот без
    # времени и уровня.
    configure_logging()
    prepare_database()
    # Брокер поднимается до оркестратора: от режима исполнения зависит, нужны ли
    # веб-процессу тяжёлые модели вообще.
    broker = await get_broker()
    await init_ai_budget()
    init_ai_cache()
    if SKIP_LLM_INIT:
        # Служебный режим (например, перенос данных): ИИ-эндпоинты недоступны.
        logger.warning("Инициализация LLM-оркестратора пропущена (SKIP_LLM_INIT=1)")
    elif broker is not None and broker.name == "arq":
        # Вынос долгих вызовов в воркер убирает из веб-процесса и тяжёлые модели:
        # оркестратор живёт в одном экземпляре в воркере, а backend стартует
        # секунды вместо десятков секунд и не держит вторую копию корпуса
        # эталонов в памяти (план, раздел 4.5.6).
        logger.info("ИИ-задачи исполняет воркер очереди: оркестратор в "
                    "веб-процессе не поднимается")
    else:
        init_orchestrator()
    yield
    await close_broker()
    await close_ai_budget()
    close_ai_cache()


class _ServerErrorJSON(BaseHTTPMiddleware):
    """Необработанная ошибка обязана уйти ответом, пока её не обнулил CORS.

    Без этого слоя исключение долетает до `ServerErrorMiddleware`, который стоит
    вне `CORSMiddleware`, и его ответ приходит без
    `Access-Control-Allow-Origin`. Браузер не показывает ни код, ни тело: в
    консоли читается «blocked by CORS policy», и человек чинит источник вместо
    упавшего запроса. Так выглядел сбой авторизации при недоступной базе.

    Причина сбоя уходит в лог, клиенту остаётся факт: текст исключения может
    нести строку подключения и пути.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as error:  # noqa: BLE001 — последнее звено: здесь решают, что увидит клиент
            # Тип, а не текст: у `SQLAlchemyError` сообщение содержит
            # `[SQL: ...][parameters: ...]`, то есть содержимое пользовательской
            # схемы (до `AI_MAX_XML_CHARS`) и адреса полей. Трейс остаётся на
            # DEBUG, куда клиентские тела не ходят по умолчанию.
            logger.error("Необработанная ошибка: %s %s — %s",
                         request.method, request.url.path, type(error).__name__)
            logger.debug("Стек необработанной ошибки %s %s",
                         request.method, request.url.path, exc_info=True)
            return JSONResponse(status_code=500,
                                content={"detail": "Внутренняя ошибка сервера."})


class _RequestId(BaseHTTPMiddleware):
    """Идентификатор запроса: в контекст логов и обратно клиенту.

    Ставится самым внешним слоем (добавлен последним), чтобы идентификатор был
    готов ДО того, как `_ServerErrorJSON` пишет строку об исключениях, — иначе
    самая нужная строка журнала осталась бы без ключа группировки. Значение
    зачищается в `app/request_context.py`: произвольный заголовок клиента не
    должен попадать ни в ответ, ни в формат лога (перевод строки подделал бы
    вторую запись журнала).
    """

    async def dispatch(self, request: Request, call_next):
        token = use_request_id(request.headers.get(REQUEST_ID_HEADER))
        try:
            response = await call_next(request)
        finally:
            rid = current_request_id()
            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response


def create_app() -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    application.mount("/static", StaticFiles(directory="static"), name="static")
    # Порядок здесь и есть порядок слоёв: `add_middleware` ставит новый слой
    # наружу, поэтому защитник ошибок добавлен ДО CORS, а не после. Обратный
    # порядок вернул бы ровно то, от чего слой спасает: 500 без CORS-заголовков,
    # то есть «ошибка CORS» вместо настоящего кода.
    application.add_middleware(_ServerErrorJSON)
    # Wildcard вместе с allow_credentials браузеры трактуют как отказ, а
    # человек за кодом — как открытую дверь с credentialed-запросами от
    # любого сайта. Список источников берётся из CORS_ORIGINS (см.
    # app/config.py), по умолчанию — локальный фронтенд и его второй адрес с
    # тем же портом, поэтому обычный запуск dev-связки не требует настройки.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Без этого идентификатор доезжает до браузера, но читателю недоступен:
        # при cross-origin запросе axios видит только simple-заголовки. Цепочка
        # «жалоба → X-Request-Id → строки журнала» начинается с того, что
        # человек показывает id с экрана, поэтому заголовок надо отдавать.
        # `Retry-After` — второй из тех, что читают: без него экранный текст
        # «повторите через 12 минут» деградирует в «повторите позже», и в браузере
        # это незаметно, потому что тесты клиента ставят заголовок сами.
        expose_headers=[REQUEST_ID_HEADER, RETRY_AFTER_HEADER],
    )
    # Самый наружный слой: добавлен последним. Идентификатор обязан существовать
    # уже тогда, когда нижележащий защитник пишет строку об исключении, — иначе
    # журнал падения и есть то место, где ключа группировки нет.
    application.add_middleware(_RequestId)

    @application.get("/health")
    def health_check():
        # Синхронный обработчик: SELECT 1 выполняется в threadpool FastAPI и
        # не вешает событийный цикл, если база лежит и соединение устанавливается
        # долго. Ответ отдаёт реальные проверки вместо выдуманного списка фич:
        # `voice_input` в коде не существует нигде, а `ok` при мёртвой базе
        # уверял, что сервис жив.
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            database_ok = True
        except Exception:  # noqa: BLE001 — для healthcheck любой сбой БД означает одно: она недоступна
            logger.exception("Healthcheck: база недоступна")
            database_ok = False

        # Оркестратор поднимается в lifespan; None — штатная деградация при
        # SKIP_LLM_INIT=1 или сбое инициализации, сервис без ИИ остаётся
        # полезным (реестр, доступ, апрувы), поэтому это флаг, а не ошибка.
        # «in_worker» — оркестратора в этом процессе нет и не должно быть:
        # задачи исполняет воркер очереди.
        orchestrator_ready = get_orchestrator() is not None
        queue = broker_name()
        if orchestrator_ready:
            orchestrator_state = "ready"
        elif queue == "arq":
            orchestrator_state = "in_worker"
        else:
            orchestrator_state = "disabled"
        body = {
            "status": "ok" if database_ok else "error",
            "version": "1.3.0",
            "checks": {
                "database": "ok" if database_ok else "unavailable",
                "llm_orchestrator": orchestrator_state,
                # Очередь недоступна — ИИ-контур недоступен, но реестр, доступ и
                # апрувы работают. Поэтому флаг, а не 503: перезапускать backend
                # из-за чужого сбоя значит ронять и ту часть, которая жива.
                "task_queue": queue,
                "ai_budget": budget_backend(),
                # Сбой SMTP — тоже наблюдение, а не условие здоровья: письмо о
                # сбросе пароля не ушло, но реестр и доступ исправны, и
                # перезапуск backend почту не починит. Число видно оператору без
                # разбора логов по маркеру `SMTP_DELIVERY_FAILED`.
                "mail_reset_failures": auth.password_reset_mail_failures(),
            },
        }
        # Живость воркеров и деградация бюджета — наблюдения, а не условия
        # здоровья: веб-процесс исправен, и помечать контейнер unhealthy из-за
        # мёртвого воркера значило бы перезапускать процесс, с которым всё в
        # порядке (compose дергает `/health`). Статус ответа поэтому меняет
        # только база, а эти поля читает оператор и алерт.
        if queue == "arq":
            # Синхронная проба с таймаутом сокета: обработчик `/health`
            # синхронный и потому живёт в threadpool FastAPI, а не в цикле
            # событий. Без отметки присутствия «очередь растёт» неотличимо от
            # «воркера нет» — один и тот же растущий счётчик ожиданий.
            body["checks"]["ai_workers"] = worker_liveness([QUEUE_LLM, QUEUE_LOCAL])
        if budget_degraded():
            # `ai_budget: process` при заданном REDIS_URL — это не режим, а
            # поломка: лимит считается на процесс и умножается на число реплик.
            # Поле говорит, что попытка переподключения будет, и когда.
            body["checks"]["ai_budget_retry_in_seconds"] = round(
                budget_retry_in_seconds() or 0.0, 1)
        if not database_ok:
            return JSONResponse(status_code=503, content=body)
        return body

    for router in (auth.router, diagrams.router, folders.router,
                   sharing.router, teams.router, ai.router, tasks.router):
        application.include_router(router)
    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)

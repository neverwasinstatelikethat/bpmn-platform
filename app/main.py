"""Составной корень BPMN-платформы: приложение, middleware и маршруты.

Тяжёлая инициализация (схема БД, предопределённые роли, LLM-оркестратор)
выполняется в lifespan, а не на импорте модуля: импорт пакета не должен писать
в базу и поднимать модели эмбеддингов.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.ai import get_orchestrator, init_orchestrator
from app.config import BACKEND_PORT, CORS_ORIGINS, SKIP_LLM_INIT
from app.db import engine
from app.deps import budget_backend, close_ai_budget, init_ai_budget
from app.routers import ai, auth, diagrams, folders, sharing, tasks, teams
from app.services.ai_cache import close_ai_cache, init_ai_cache
from app.startup import prepare_database
from app.tasks import broker_name, close_broker, get_broker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
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


def create_app() -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    application.mount("/static", StaticFiles(directory="static"), name="static")
    # Wildcard вместе с allow_credentials браузеры трактуют как отказ, а
    # человек за кодом — как открытую дверь с credentialed-запросами от
    # любого сайта. Список источников берётся из CORS_ORIGINS (см.
    # app/config.py), по умолчанию — локальный фронтенд, поэтому обычный
    # запуск dev-связки не требует настройки.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
            },
        }
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

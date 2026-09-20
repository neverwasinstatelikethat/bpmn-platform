"""Составной корень BPMN-платформы: приложение, middleware и маршруты.

Тяжёлая инициализация (схема БД, предопределённые роли, LLM-оркестратор)
выполняется в lifespan, а не на импорте модуля: импорт пакета не должен писать
в базу и поднимать модели эмбеддингов.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.ai import init_orchestrator
from app.config import BACKEND_PORT, SKIP_LLM_INIT
from app.routers import ai, auth, diagrams, folders, sharing, teams
from app.startup import run_schema_bootstrap

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    run_schema_bootstrap()
    if SKIP_LLM_INIT:
        # Служебный режим (например, перенос данных): ИИ-эндпоинты недоступны.
        logger.warning("Инициализация LLM-оркестратора пропущена (SKIP_LLM_INIT=1)")
    else:
        init_orchestrator()
    yield


def create_app() -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    application.mount("/static", StaticFiles(directory="static"), name="static")
    # Известный долг: wildcard вместе с allow_credentials. Заменить на список
    # из окружения одновременно с refresh-токенами.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/health")
    async def health_check():
        return {
            "status": "ok",
            "version": "1.2.0",
            "features": ["bpmn_generation", "voice_input", "validation"],
        }

    for router in (auth.router, diagrams.router, folders.router,
                   sharing.router, teams.router, ai.router):
        application.include_router(router)
    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)

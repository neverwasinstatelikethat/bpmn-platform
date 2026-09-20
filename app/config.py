"""Конфигурация приложения: значения только из переменных окружения (см. .env.example)."""
import logging
import os

logger = logging.getLogger(__name__)

# Конфигурация (значения берутся из переменных окружения, см. .env.example)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bpmn.db")
_env_secret_key = os.getenv("SECRET_KEY")
if not _env_secret_key:
    if DATABASE_URL.startswith("sqlite"):
        logger.warning("SECRET_KEY не задан: используется ключ разработки (допустимо только для локального SQLite)")
        _env_secret_key = "dev-secret-key-change-me"
    else:
        raise RuntimeError("Переменная окружения SECRET_KEY обязательна при работе с PostgreSQL")
SECRET_KEY = _env_secret_key
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
# Порты 8000/3000 на рабочей машине заняты сторонними проектами (Grafana и др.)
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8765"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3456")
# Флаг только описывает намерение запуска; сам оркестратор создаётся в
# lifespan приложения (app.ai.init_orchestrator), а не на импорте модуля.
SKIP_LLM_INIT = os.getenv("SKIP_LLM_INIT") == "1"

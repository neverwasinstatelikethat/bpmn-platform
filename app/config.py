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

# Ограничения ИИ-контура. Размер схемы — единый для скоринга и улучшения:
# оба маршрута парсят пользовательский XML.
AI_MAX_XML_CHARS = int(os.getenv("AI_MAX_XML_CHARS", "1000000"))
# Загрузка .bpmn в реестр: файл сохраняется в БД и потом разбирается.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "2000000"))
# Вызовы модели дорогие и идут в рамках одного тарифного слота, поэтому на
# пользователя действует почасовой бюджет. Счётчик живой, внутри процесса: при
# нескольких репликах нужен общий (Redis — план в
# docs/plans/redis-cache-and-task-routing.md).
AI_REQUESTS_PER_HOUR = int(os.getenv("AI_REQUESTS_PER_HOUR", "60"))

# Флаг только описывает намерение запуска; сам оркестратор создаётся в
# lifespan приложения (app.ai.init_orchestrator), а не на импорте модуля.
SKIP_LLM_INIT = os.getenv("SKIP_LLM_INIT") == "1"

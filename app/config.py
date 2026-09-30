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
# пользователя действует почасовой бюджет. При заданном REDIS_URL окно общее на
# все реплики (app/deps.py), без него счётчик остаётся локальным для процесса.
AI_REQUESTS_PER_HOUR = int(os.getenv("AI_REQUESTS_PER_HOUR", "60"))

# Флаг только описывает намерение запуска; сам оркестратор создаётся в
# lifespan приложения (app.ai.init_orchestrator), а не на импорте модуля.
SKIP_LLM_INIT = os.getenv("SKIP_LLM_INIT") == "1"

# --- Redis: очередь задач и кэш ответов модели -------------------------------
#
# Пустой REDIS_URL — режим без очереди: задача исполняется в процессе запроса
# (app/tasks.py, InlineBroker). Это не «упрощённая разработка», а штатный
# режим одного процесса: без брокера долгие вызовы физически некуда вынести, и
# контракт эндпоинтов (202 + task_id + опрос) остаётся тем же самым.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
# Кэш держат в отдельной логической базе, чтобы его можно было сбросить
# (FLUSHDB) вместе с ответами модели, не трогая очередь задач. Пусто — та же
# база, что и очередь.
CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "").strip() or REDIS_URL

# Срок жизни кэша. Ответ улучшения держится дольше ответа генерации: улучшение
# привязано к конкретному XML схемы, который сам по себе не меняется, а
# описание процесса пользователь переформулирует.
LLM_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", str(7 * 24 * 3600)))
GENERATION_CACHE_TTL = int(os.getenv("GENERATION_CACHE_TTL", str(24 * 3600)))
SCORE_CACHE_TTL = int(os.getenv("SCORE_CACHE_TTL", str(30 * 24 * 3600)))
# Заведомо плохой вход (схема, которую модель отвергла) кэшируется на минуты:
# повтор той же ошибки не должен снова оплачивать вызов провайдера.
NEGATIVE_CACHE_TTL = int(os.getenv("NEGATIVE_CACHE_TTL", "300"))

# --- Очередь задач -----------------------------------------------------------
# Параллелизм воркеров. У LLM-очереди он маленький намеренно: тариф провайдера
# даёт один одновременный запрос (GIGACHAT_MAX_CONCURRENT), поэтому второй слот
# нужен на разбор ответа и починку структуры, а не на второй вызов модели.
ARQ_MAX_JOBS = int(os.getenv("ARQ_MAX_JOBS", "2"))
ARQ_LOCAL_MAX_JOBS = int(os.getenv("ARQ_LOCAL_MAX_JOBS", "4"))
# Таймаут задачи LLM-очереди обязан превышать GENERATION_BUDGET_SECONDS:
# иначе воркер убивал бы каждую честную генерацию на половине, а пользователь
# получал «сбой» вместо схемы. 1800 с = бюджет генерации 900 с + запас на
# очередь к провайдеру и два ретрая.
AI_LLM_JOB_TIMEOUT = int(os.getenv("AI_LLM_JOB_TIMEOUT", "1800"))
AI_LOCAL_JOB_TIMEOUT = int(os.getenv("AI_LOCAL_JOB_TIMEOUT", "120"))
# Сколько раз задача LLM-очереди может быть взята заново после сбоя провайдера
# (1 попытка + 2 повтора с задержкой 5 и 25 с).
AI_LLM_JOB_TRIES = int(os.getenv("AI_LLM_JOB_TRIES", "3"))
AI_LOCAL_JOB_TRIES = int(os.getenv("AI_LOCAL_JOB_TRIES", "2"))
# Задачи в статусе running старше этого срока считаются потерянным воркером и
# возвращаются в очередь (app/services/tasks.py:reap_stale_tasks).
AI_TASK_STALE_SECONDS = int(os.getenv("AI_TASK_STALE_SECONDS", str(AI_LLM_JOB_TIMEOUT + 300)))
# Подсказка клиенту, через сколько секунд опрашивать задачу в первый раз.
TASK_POLL_SECONDS = float(os.getenv("TASK_POLL_SECONDS", "2"))
# Как часто воркер отмечает своё присутствие в Redis (arq `record_health`).
# Ключ живёт на один интервал дольше, поэтому интервал и есть окно, за которое
# healthcheck контейнера замечает мёртвый процесс: при дефолтных 3600 с ключ
# переживал бы воркера на час, и «нездоровый» процесс выглядел живым.
AI_WORKER_HEALTH_SECONDS = int(os.getenv("AI_WORKER_HEALTH_SECONDS", "20"))

# Пул соединений PostgreSQL. ИИ-маршрут держит соединение на всё время вызова
# модели (см. app/routers/ai.py: зависимость get_db живёт до конца запроса),
# поэтому дефолтных SQLAlchemy 5+10 не хватает уже на десяток одновременных
# улучшений: остальные маршруты встают в очередь на pool_timeout. Значения
# завышены ровно до этого же порядка; ceiling держим под max_connections
# сервера (PostgreSQL по умолчанию — 100).
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
# SQLite: миллисекунды ожидания блокировки файла перед ошибкой
# «database is locked» (см. PRAGMA busy_timeout в app/db.py).
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))

# Разрешённые источники CORS. Wildcard с allow_credentials браузером не
# принимался как легитимная пара, поэтому список берётся из окружения, а по
# умолчанию — адрес локального фронтенда (разработчику нечего задавать).
# Перечисление через запятую; пустое значение = только FRONTEND_URL.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()] \
    or [FRONTEND_URL]

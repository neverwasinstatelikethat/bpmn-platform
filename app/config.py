"""Конфигурация приложения: значения только из переменных окружения (см. .env.example)."""
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


_FLAG_TRUE = ("1", "true", "yes", "on")
_FLAG_FALSE = ("0", "false", "no", "off")


def _parse_flag(raw: str) -> Optional[bool]:
    low = (raw or "").strip().lower()
    if low in _FLAG_TRUE:
        return True
    if low in _FLAG_FALSE:
        return False
    return None


def _env_flag(name: str, default: str = "") -> bool:
    """Логическая переменная окружения.

    Сравнение `== "1"` молча считало любое другое правдоподобное значение
    выключенным: `SKIP_LLM_INIT=true` в `.env` оставался «режимом без ИИ» только
    на бумаге, а воркер продолжал платить провайдеру.

    Пустое значение — «не задано», и флаг берёт дефолт, как это делает `_env_int`:
    раскомментированная в `.env` строка `MAIL_SSL_TLS=` иначе выключала бы
    implicit TLS на 465 порту молча, хотя так никто из людей не решал.
    Неразобранное значение тоже падает в дефолт, но о нём сообщается: промолчать
    значило бы завести тот же флаг, который «задан, но не читается».
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return _parse_flag(default) or False
    parsed = _parse_flag(raw)
    if parsed is not None:
        return parsed
    logger.warning("Переменная %s=%r не является флагом — беру %s",
                   name, raw, default)
    return _parse_flag(default) or False


def _env_int(name: str, default: int) -> int:
    """Целочисленная переменная окружения с защитой от пустого и мусорного значения.

    `int(os.getenv(...))` ронял процесс на `AI_TASK_STALE_SECONDS=` — ключ без
    значения законен и в `.env`, и в docker-compose (`${VAR:-}` передаёт пустую
    строку), а падать при импорте с `ValueError: invalid literal for int()`
    означает «backend не стартует, а почему — непонятно». Пустое значение
    означает «не задано» и берёт дефолт; неразобранное — ошибка, о которой
    сообщается: молча подставить дефолт было бы тем же самообманом, что и флаг,
    который «задан, но не читается».
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        logger.warning("Переменная %s=%r не является целым числом — беру %s",
                       name, raw, default)
        return int(default)


def _env_float(name: str, default: float) -> float:
    """То же для вещественных значений (подсказки интервала опроса, доли)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        logger.warning("Переменная %s=%r не является числом — беру %s",
                       name, raw, default)
        return float(default)


# Конфигурация (значения берутся из переменных окружения, см. .env.example)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bpmn.db")
_env_secret_key = os.getenv("SECRET_KEY")
if not _env_secret_key:
    if DATABASE_URL.startswith("sqlite"):
        # Ключ разработки поднимается случайным, а не берётся из текста в коде:
        # записанное в репозитории значение «dev-secret-key-change-me» — не
        # временный секрет, а публичный, и любой, кто прочитал исходники, мог
        # подписать токен под виде локальной копии. Случайный ключ живёт до
        # перезапуска процесса, и это честная цена: сессии разработки обнуляются,
        # а подделать чужой токен вне процесса нечем.
        logger.warning("SECRET_KEY не задан: используется ключ разработки "
                       "(только локальный SQLite), он живёт до перезапуска")
        _env_secret_key = secrets.token_urlsafe(32)
    else:
        raise RuntimeError("Переменная окружения SECRET_KEY обязательна при работе с PostgreSQL")
SECRET_KEY = _env_secret_key
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")
# Порты 8000/3000 на рабочей машине заняты сторонними проектами (Grafana и др.)
BACKEND_PORT = _env_int("BACKEND_PORT", "8765")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3456")

# Ограничения ИИ-контура. Размер схемы — единый для скоринга и улучшения:
# оба маршрута парсят пользовательский XML.
AI_MAX_XML_CHARS = _env_int("AI_MAX_XML_CHARS", "1000000")
# Загрузка .bpmn в реестр: файл сохраняется в БД и потом разбирается.
MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_BYTES", "2000000")
# Вызовы модели дорогие и идут в рамках одного тарифного слота, поэтому на
# пользователя действует почасовой бюджет. При заданном REDIS_URL окно общее на
# все реплики (app/deps.py), без него счётчик остаётся локальным для процесса.
AI_REQUESTS_PER_HOUR = _env_int("AI_REQUESTS_PER_HOUR", "60")

# Флаг только описывает намерение запуска; сам оркестратор создаётся в
# lifespan приложения (app.ai.init_orchestrator), а не на импорте модуля.
# Область действия — ВЕБ-процесс, и это не недосмотр проводки: в режиме очереди
# тяжёлые модели и обращения к провайдеру исполняет воркер, и флаг, дошедший до
# него, вырубил бы ровно ту работу, ради которой воркер поднят. Поэтому в
# docker-compose ключ задаётся только у сервиса `backend`, а не в анкере
# `x-app-environment` (там же — комментарий).
# Значение читается как истина, а не буквально «1»: `SKIP_LLM_INIT=true` в `.env`
# при сравнении `== "1"` молча оставался режимом без тяжёлых моделей только на
# бумаге.
SKIP_LLM_INIT = _env_flag("SKIP_LLM_INIT")

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
LLM_CACHE_TTL = _env_int("LLM_CACHE_TTL", str(7 * 24 * 3600))
GENERATION_CACHE_TTL = _env_int("GENERATION_CACHE_TTL", str(24 * 3600))
SCORE_CACHE_TTL = _env_int("SCORE_CACHE_TTL", str(30 * 24 * 3600))
# Заведомо плохой вход (схема, которую модель отвергла) кэшируется на минуты:
# повтор той же ошибки не должен снова оплачивать вызов провайдера.
NEGATIVE_CACHE_TTL = _env_int("NEGATIVE_CACHE_TTL", "300")

# --- Очередь задач -----------------------------------------------------------
# Параллелизм воркеров. У LLM-очереди он маленький намеренно: тариф провайдера
# даёт один одновременный запрос (GIGACHAT_MAX_CONCURRENT), поэтому второй слот
# нужен на разбор ответа и починку структуры, а не на второй вызов модели.
ARQ_MAX_JOBS = _env_int("ARQ_MAX_JOBS", "2")
ARQ_LOCAL_MAX_JOBS = _env_int("ARQ_LOCAL_MAX_JOBS", "4")
# Таймаут задачи LLM-очереди обязан превышать GENERATION_BUDGET_SECONDS:
# иначе воркер убивал бы каждую честную генерацию на половине, а пользователь
# получал «сбой» вместо схемы. 1800 с = бюджет генерации 900 с + запас на
# очередь к провайдеру и два ретрая.
AI_LLM_JOB_TIMEOUT = _env_int("AI_LLM_JOB_TIMEOUT", "1800")
AI_LOCAL_JOB_TIMEOUT = _env_int("AI_LOCAL_JOB_TIMEOUT", "120")
# Сколько раз задача LLM-очереди может быть взята заново после сбоя провайдера
# (1 попытка + 2 повтора с задержкой 5 и 25 с).
AI_LLM_JOB_TRIES = _env_int("AI_LLM_JOB_TRIES", "3")
AI_LOCAL_JOB_TRIES = _env_int("AI_LOCAL_JOB_TRIES", "2")
# Задачи в статусе running старше этого срока считаются потерянным воркером и
# возвращаются в очередь (`stale_tasks` в app/services/tasks.py, обход —
# `_recover_stale_tasks` в app/tasks.py). По умолчанию
# выводится из таймаута LLM-задачи, а не стоит отдельной константой: порог,
# записанный числом (2100 в прежнем docker-compose), переживает рост
# AI_LLM_JOB_TIMEOUT и начинает хоронить живые задачи, возвращая их в очередь
# по второму кругу. Явное значение оставляется только ниже порога, и оно
# сопровождается предупреждением — молча оно делало бы ровно ту же подмену.
AI_TASK_STALE_SECONDS = _env_int("AI_TASK_STALE_SECONDS",
                                      AI_LLM_JOB_TIMEOUT + 300)
if AI_TASK_STALE_SECONDS <= AI_LLM_JOB_TIMEOUT:
    logger.warning(
        "AI_TASK_STALE_SECONDS=%d не больше AI_LLM_JOB_TIMEOUT=%d: живая задача "
        "будет объявлена потерянной воркером и поставлена в очередь заново, а "
        "это вторая оплата того же обращения к модели. Держите порог выше "
        "таймаута очереди (по умолчанию он +300 с).",
        AI_TASK_STALE_SECONDS, AI_LLM_JOB_TIMEOUT)
# Подсказка клиенту, через сколько секунд опрашивать задачу в первый раз.
TASK_POLL_SECONDS = _env_float("TASK_POLL_SECONDS", "2")
# Как часто исполнитель задачи смотрит, не сняли ли его. Отдельный ключ, а не
# `TASK_POLL_SECONDS`: тот описывает вежливость клиента к серверу, и оператор,
# решивший разгрузить очередь редким опросом (`TASK_POLL_SECONDS=600`), иначе
# получил бы воркера, который не замечает отмену до десяти минут и продолжает
# платить провайдеру за работу, от которой человек отказался.
CANCEL_PROBE_SECONDS = _env_float("CANCEL_PROBE_SECONDS", "2")
# Как часто воркер отмечает своё присутствие в Redis (arq `record_health`).
# Ключ живёт на один интервал дольше, поэтому интервал и есть окно, за которое
# healthcheck контейнера замечает мёртвый процесс: при дефолтных 3600 с ключ
# переживал бы воркера на час, и «нездоровый» процесс выглядел живым.
AI_WORKER_HEALTH_SECONDS = _env_int("AI_WORKER_HEALTH_SECONDS", "20")

# Пул соединений PostgreSQL. ИИ-маршрут держит соединение на всё время вызова
# модели (см. app/routers/ai.py: зависимость get_db живёт до конца запроса),
# поэтому дефолтных SQLAlchemy 5+10 не хватает уже на десяток одновременных
# улучшений: остальные маршруты встают в очередь на pool_timeout. Значения
# завышены ровно до этого же порядка; ceiling держим под max_connections
# сервера (PostgreSQL по умолчанию — 100).
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", "10")
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", "20")
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", "30")
# SQLite: миллисекунды ожидания блокировки файла перед ошибкой
# «database is locked» (см. PRAGMA busy_timeout в app/db.py).
SQLITE_BUSY_TIMEOUT_MS = _env_int("SQLITE_BUSY_TIMEOUT_MS", "5000")


def _loopback_alias(origin: str) -> str:
    """Второй адрес loopback для той же схемы и порта; пустая строка, если адрес не локальный.

    Разбирается именно `scheme://host[:port]`, а не сравнение по префиксу:
    по префиксу подошёл бы и `http://localhost.example.com`, то есть чужому
    хосту, которому здесь делать нечего.
    """
    scheme, _, rest = origin.partition("://")
    if scheme not in ("http", "https") or not rest or "/" in rest:
        return ""
    host, _, port = rest.partition(":")
    alias = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}.get(host.lower())
    if alias is None:
        return ""
    return f"{scheme}://{alias}" + (f":{port}" if port else "")


def _with_loopback_twins(origins: list) -> list:
    """К каждому локальному источнику добавляет его второй адрес с тем же портом.

    `http://localhost:3456` и `http://127.0.0.1:3456` для браузера разные
    источники, хотя это одна машина: Compose публикует фронтенд на
    `127.0.0.1:3456`, а в гайдах и в выводе `docker compose` адрес с `localhost`.
    Разрешённым был один узел, поэтому со второго адреса падал каждый маршрут,
    включая `/api/login`, и выглядело это как ошибка CORS, а не как ответ
    бэкенда. Различать два адреса loopback нечем: доступ к порту уже означает
    доступ к машине.
    """
    expanded = []
    for origin in origins:
        for value in (origin, _loopback_alias(origin)):
            if value and value not in expanded:
                expanded.append(value)
    return expanded


# Разрешённые источники CORS. Wildcard с allow_credentials браузером не
# принимался как легитимная пара, поэтому список берётся из окружения, а по
# умолчанию — адрес локального фронтенда (разработчику нечего задавать).
# Перечисление через запятую; пустое значение = только FRONTEND_URL. Локальные
# адреса дополняются вторым узлом и когда заданы явно: `CORS_ORIGINS` в
# docker-compose пишет `localhost`, а человек открывает `127.0.0.1`.
CORS_ORIGINS = _with_loopback_twins(
    [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    or [FRONTEND_URL])

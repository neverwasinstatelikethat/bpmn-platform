"""Кэш ответов модели в Redis: механизм без знания о предметной области.

Что именно кэшируется, какими ключами и с каким сроком жизни, решает
приложение (`app/services/ai_cache.py`); этот модуль знает только про ключ,
значение и срок. Разделение нужно, чтобы `core/` оставался ядром без
зависимости от конфигурации веб-слоя, а харнесс `eval/` — детерминированным:
без URL кэша здесь не создаётся ни одного клиента и ни одного обращения к
Redis, поэтому replay-прогон не может прочитать чужой ответ.

Три правила, которые модуль обеспечивает силой, а не договорённостью:

1. Значение проходит через pydantic-схему `CachedAnswer` и на записи, и на
   чтении. Кэш не должен становиться источником «сырых» данных в обход
   валидации: битый или подрезанный JSON из Redis читается как промах.
2. Версия промпта входит в значение и сверяется при чтении. Смена текста
   промпта делает старый ответ чужим, и отдавать его — значит показывать
   пользователю рекомендацию, посчитанную по другим правилам.
3. Сбой Redis — это промах, а не ошибка запроса. Кэш ускоряет, но не
   обеспечивает correctness: при недоступном Redis контур обязан работать
   ровно так же, только дороже. К тому же сбою относится и неразобранный адрес
   кэша: клиент поднимается при первом обращении, а не в конструкторе, иначе
   опечатка в `CACHE_REDIS_URL` роняла бы lifespan всего backend.

Клиент синхронный: кэшируемые вызовы (`generator.generate`, `scorer.evaluate`,
транспорт модели) сами синхронные и исполняются в потоке, поэтому асинхронный
клиент добавил бы второй пул соединений и не убрал бы ни одного ожидания.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

import redis
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Один отказ Redis не должен повторяться в каждой строке лога: при частых
# запросах это сотни одинаковых строк. Повторяем не чаще раза в минуту.
_LOG_THROTTLE_SECONDS = 60.0


class CacheUnavailable(RuntimeError):
    """Кэш не работает: адрес неразобран, клиент не поднят.

    Нужен отдельный класс, а не повторный `raise` сохранённой ошибки: отказ
    при опечатке в `CACHE_REDIS_URL` повторяется на каждом обращении воркера,
    а второй `raise` того же объекта дописывает ему `__traceback__`. За час
    такой воркер рос бы по памяти ровно на число запросов — утечка из строки
    лога старта. Наружу класс всё равно не выходит: его ловит `except` каждого
    метода и превращает в промах.
    """


class CachedAnswer(BaseModel):
    """Значение кэша. `negative` отличает «модель ответила» от «вход заведомо
    плохой и повторять вызов незачем» — у них разные сроки жизни и разный
    смысл для вызывающего кода."""

    result: Dict[str, Any] = Field(default_factory=dict)
    prompt_version: str
    created_at: float
    negative: bool = False
    error: Optional[str] = None


class CacheStats(BaseModel):
    """Счётчики попаданий. Нужны, чтобы «кэш включён» отличалось от «кэш
    работает»: без них доля попаданий известна только на словах.

    Живут в Redis (хэш `{namespace}:stats`), а не в памяти процесса: попадания
    считает воркер, а смотрит в них веб-процесс. Локальный счётчик показывал бы
    нули при работающем кэше — это тот класс метрики, которая не молчит, а врёт.
    """

    hits: int = 0
    misses: int = 0
    negative_hits: int = 0
    writes: int = 0
    errors: int = 0

    def as_dict(self) -> Dict[str, Any]:
        total = self.hits + self.misses + self.negative_hits
        return {
            **self.model_dump(),
            "hit_share": round((self.hits + self.negative_hits) / total, 4) if total else None,
        }


def sha256_of(*parts: str) -> str:
    """Отпечаток входов. Части разделяются нулевым байтом, чтобы склейка
    («ab» + «c» и «a» + «bc») не давала один ключ."""
    digest = hashlib.sha256()
    for index, part in enumerate(parts):
        if index:
            digest.update(b"\x00")
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def source_fingerprint(*modules: ModuleType) -> str:
    """Версия промпта/правил по содержимому исходников модулей.

    Ручной счётчик версии промпта забывают поднять ровно тогда, когда промпт
    меняют, — и кэш начинает отдавать ответы, посчитанные по другому тексту.
    Отпечаток исходника поднимается сам: правка файла меняет версию и снимает
    устаревшие ответы. Цена — инвалидация и при правке комментария, что
    безопасно: промах дороже, чем чужой ответ.
    """
    joined = b""
    for module in modules:
        path = getattr(module, "__file__", None)
        if not path:
            raise ValueError(f"у модуля {module!r} нет исходного файла")
        with open(path, "rb") as handle:
            joined += hashlib.sha256(handle.read()).digest()
    return hashlib.sha256(joined).hexdigest()[:12]


class LLMCache:
    """Хранилище «ключ → проверенный ответ модели» поверх Redis."""

    def __init__(self, url: str, namespace: str, *,
                 socket_timeout: float = 1.0,
                 socket_connect_timeout: float = 1.0) -> None:
        self.namespace = namespace.strip(":")
        self.stats = CacheStats()
        self._stats_key = self.key("stats")
        self._last_error_at = 0.0
        self._url = url
        self._client_options = {
            "decode_responses": True,
            "socket_timeout": socket_timeout,
            "socket_connect_timeout": socket_connect_timeout,
            # Повторная попытка внутри клиента только удлиняет запрос: дешевле
            # считать это промахом и сходить в модель.
            "retry_on_timeout": False,
            "health_check_interval": 30,
        }
        # Клиент не поднят, пока не нужен: ради `ping()` на старте открывать
        # пул соединений незачем, а при неразобранном адресе его и не создать.
        self._client: Optional[redis.Redis] = None
        self._client_error: Optional[Exception] = None
        self._lock = threading.Lock()
        # Отдельная от `_lock`: конструирование клиента не должно ждать
        # прибавления счётчика, а счётчик — клиента (иначе `_lock` стал бы бы
        # точкой сериализации всего слоя на каждом запросе).
        self._client_lock = threading.Lock()

    # --- ключи ---------------------------------------------------------------
    def key(self, *parts: str) -> str:
        return ":".join([self.namespace, *parts])

    def _redis(self) -> redis.Redis:
        """Клиент ленивый: `redis.Redis.from_url` бросает на неразобранный адрес
        сразу, и созданный в конструкторе клиент ронял бы lifespan backend'а из
        опечатки в переменной, которая касается только кэша.

        Ошибка конструирования запоминаается и уходит наружу тем же путём, что и
        недоступный Redis, — через `except` каждого метода, то есть как промах.

        Двойная проверка под `_client_lock`: воркер обслуживает запросы на пуле
        потоков, и без неё первые N обращений создали бы по клиенту и по строке
        в логе — при неразобранном адресе их ещё и не отличить от одного сбоя.
        """
        client = self._client
        if client is not None:
            return client
        with self._client_lock:
            if self._client is None:
                if self._client_error is not None:
                    raise CacheUnavailable(f"адрес кэша {self._url} неразобран") from None
                try:
                    self._client = redis.Redis.from_url(self._url,
                                                        **self._client_options)
                except Exception as error:  # noqa: BLE001 — кэш обязан fail open
                    self._client_error = error
                    logger.warning("Кэш ответов модели: адрес %s неразобран (%s) — "
                                   "работаем без кэша", self._url, error)
                    raise
            return self._client

    @property
    def client_construction_error(self) -> Optional[Exception]:
        """Причина, по которой клиента нет вовсе, — либо None.

        Различение нужно запуску: «сервер лёг» и «в переменной опечатка»
        требуют разных действий оператора, а из одного `ping() is False` они не
        отличаются. Значение имеет смысл после первого обращения (ping, get):
        до него слой ещё ничего не пытался строить.
        """
        return self._client_error

    # --- счётчики -------------------------------------------------------------
    def _count(self, field: str) -> None:
        """Прибавить единицу к общему счётчику.

        Локальное поле живёт под `self._lock` и остаётся: при недоступном Redis
        общий счётчик не обновить, и тогда хотя бы видно, что делает этот
        процесс. Без блокировки `+=` терял бы прибавления на пуле потоков, и в
        fallback было бы число меньше сделанного. Сбой прибавления не имеет
        права превратить попадание в ошибку запроса.
        """
        with self._lock:
            setattr(self.stats, field, getattr(self.stats, field) + 1)
        try:
            self._redis().hincrby(self._stats_key, field, 1)
        except Exception:  # noqa: BLE001 — счётчики не причина ронять кэш
            pass

    def snapshot(self) -> CacheStats:
        """Счётчики по всем процессам, которые пишут в этот Redis."""
        return self.report()[0]

    def report(self) -> Tuple[CacheStats, bool]:
        """Счётчики и признак того, что они взяты из общего хэша.

        Пометка нужна маршруту статистики: попадания считают воркеры, а смотрит
        в них веб-процесс, и непрочитанный общий хэш оставил бы ему локальные
        нули. Без различения «hits: 0, enabled: true» читается как «кэш не
        попадает», и по этой цифре кэш выключают.
        """
        try:
            raw = self._redis().hgetall(self._stats_key)
        except Exception as error:  # noqa: BLE001
            self._count_error(error)
            # Копия, а не живой объект: `cache_stats()` читает счётчики в
            # чужом потоке, и один и тот же снимок не должен ползти под рукой
            # у того, кто его уже получил.
            return self.stats.model_copy(), False
        data = {field: int(raw.get(field, 0) or 0)
                for field in CacheStats.model_fields}
        return CacheStats(**data), True

    # --- чтение и запись -----------------------------------------------------
    def get(self, key: str, *, prompt_version: str) -> Optional[CachedAnswer]:
        """Ответ из кэша либо None. None означает и «нет записи», и «запись
        устарела», и «Redis недоступен» — во всех трёх случаях вызывающий код
        обязан сходить в модель, поэтому различать их ему незачем."""
        try:
            raw = self._redis().get(key)
        except Exception as error:  # noqa: BLE001 — сбой кэша не ошибка запроса
            self._count_error(error)
            return None
        if raw is None:
            self._count("misses")
            return None
        try:
            answer = CachedAnswer.model_validate(json.loads(raw))
        except (ValidationError, ValueError, TypeError) as error:
            # Битая запись хуже отсутствия: её нельзя отдать, а оставлять в
            # кэше до истечения срока значит читать заново на каждом запросе.
            logger.warning("Кэш: значение по ключу %s не читается (%s), удаляю", key, error)
            self._count("misses")
            self._delete_quietly(key)
            return None
        if answer.prompt_version != prompt_version:
            self._count("misses")
            self._delete_quietly(key)
            return None
        if answer.negative:
            self._count("negative_hits")
        else:
            self._count("hits")
        return answer

    def set(self, key: str, result: Dict[str, Any], *, prompt_version: str,
            ttl: int) -> bool:
        return self._write(key, CachedAnswer(result=result, prompt_version=prompt_version,
                                            created_at=time.time()), ttl)

    def set_negative(self, key: str, error: str, *, prompt_version: str,
                     ttl: int) -> bool:
        """Запомнить отказ по этому входу, чтобы повтор не оплачивал вызов."""
        return self._write(key, CachedAnswer(prompt_version=prompt_version,
                                             created_at=time.time(), negative=True,
                                             error=error), ttl)

    def _write(self, key: str, answer: CachedAnswer, ttl: int) -> bool:
        if ttl <= 0:
            return False
        try:
            payload = answer.model_dump_json()
        except (TypeError, ValueError) as error:
            # Ответ не сериализуется — значит в нём есть объект, который не
            # пережил бы и HTTP-ответ. Кэш здесь ни при чём, но и писать нечего.
            logger.warning("Кэш: значение для %s не сериализуется (%s)", key, error)
            return False
        try:
            self._redis().set(key, payload, ex=ttl)
        except Exception as error:  # noqa: BLE001 — сбой кэша не ошибка запроса
            self._count_error(error)
            return False
        self._count("writes")
        return True

    def _delete_quietly(self, key: str) -> None:
        try:
            self._redis().delete(key)
        except Exception:  # noqa: BLE001 — удаление устаревшей записи не критично
            pass

    # --- состояние -----------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._redis().ping())
        except Exception as error:  # noqa: BLE001
            self._count_error(error)
            return False

    def close(self) -> None:
        """Закрывает только реально поднятый клиент: неразобранный адрес не
        создал соединений, а ради `close()` открывать клиент нечего.

        Клиент обнуляется под тем же замком, что его создаёт: закрытый пул,
        оставленный в `_client`, следующий вызов прочитает как живой и упрётся
        в мёртвое соединение вместо переподключения. Продуктовый путь этого не
        видит (`close_ai_cache` выбрасывает весь объект), но контракт метода —
        «закрыть и при надобности поднять заново».
        """
        with self._client_lock:
            if self._client is None:
                return
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._client_error = None

    def _count_error(self, error: Exception) -> None:
        self._count("errors")
        now = time.monotonic()
        with self._lock:
            if now - self._last_error_at < _LOG_THROTTLE_SECONDS:
                return
            self._last_error_at = now
        logger.warning("Кэш ответов модели недоступен, работаем без него: %s", error)

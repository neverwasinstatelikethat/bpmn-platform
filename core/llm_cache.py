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
   ровно так же, только дороже.

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
from typing import Any, Dict, Optional

import redis
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Один отказ Redis не должен повторяться в каждой строке лога: при частых
# запросах это сотни одинаковых строк. Повторяем не чаще раза в минуту.
_LOG_THROTTLE_SECONDS = 60.0


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
        self._client: Optional[redis.Redis] = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            # Повторная попытка внутри клиента только удлиняет запрос: дешевле
            # считать это промахом и сходить в модель.
            retry_on_timeout=False,
            health_check_interval=30,
        )
        self._lock = threading.Lock()

    # --- ключи ---------------------------------------------------------------
    def key(self, *parts: str) -> str:
        return ":".join([self.namespace, *parts])

    # --- счётчики -------------------------------------------------------------
    def _count(self, field: str) -> None:
        """Прибавить единицу к общему счётчику.

        Локальное поле остаётся: при недоступном Redis общий счётчик не
        обновить, и тогда хотя бы видно, что делает этот процесс. Сбой
        прибавления не имеет права превратить попадание в ошибку запроса.
        """
        setattr(self.stats, field, getattr(self.stats, field) + 1)
        try:
            self._client.hincrby(self._stats_key, field, 1)
        except Exception:  # noqa: BLE001 — счётчики не причина ронять кэш
            pass

    def snapshot(self) -> CacheStats:
        """Счётчики по всем процессам, которые пишут в этот Redis."""
        try:
            raw = self._client.hgetall(self._stats_key)
        except Exception as error:  # noqa: BLE001
            self._count_error(error)
            return self.stats
        if not raw:
            return self.stats
        data = {field: int(raw.get(field, 0) or 0) for field in
                ("hits", "misses", "negative_hits", "writes", "errors")}
        return CacheStats(**data)

    # --- чтение и запись -----------------------------------------------------
    def get(self, key: str, *, prompt_version: str) -> Optional[CachedAnswer]:
        """Ответ из кэша либо None. None означает и «нет записи», и «запись
        устарела», и «Redis недоступен» — во всех трёх случаях вызывающий код
        обязан сходить в модель, поэтому различать их ему незачем."""
        try:
            raw = self._client.get(key)
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
            self._client.set(key, payload, ex=ttl)
        except Exception as error:  # noqa: BLE001 — сбой кэша не ошибка запроса
            self._count_error(error)
            return False
        self._count("writes")
        return True

    def _delete_quietly(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:  # noqa: BLE001 — удаление устаревшей записи не критично
            pass

    # --- состояние -----------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception as error:  # noqa: BLE001
            self._count_error(error)
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def _count_error(self, error: Exception) -> None:
        self._count("errors")
        now = time.monotonic()
        with self._lock:
            if now - self._last_error_at < _LOG_THROTTLE_SECONDS:
                return
            self._last_error_at = now
        logger.warning("Кэш ответов модели недоступен, работаем без него: %s", error)

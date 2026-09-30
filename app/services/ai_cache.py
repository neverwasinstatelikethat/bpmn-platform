"""Политика кэширования ответов модели: что, под каким ключом и на сколько.

Механизм живёт в `core/llm_cache.py` и ничего не знает о схеме BPMN; этот
модуль знает только контур приложения — какие входы определяют ответ, какая
версия промпта делает ответ чужим и какие отказы повторять незачем.

Ключ строится только из входов, влияющих на результат (план
docs/plans/redis-cache-and-task-routing.md, раздел 3.1). Идентификатора
пользователя в ключе нет намеренно: ответ модели на одну и ту же схему и ту же
просьбу одинаков для всех, а доступ к кэшу есть только у авторизованных
маршрутов, которые сами проверяют права на схему.

Отказ провайдера не кэшируется вовсе: «сервис временно недоступен» перестал
быть правдой через минуту, а запись с таким ответом держала бы пользователя в
ошибке весь срок жизни ключа. Кэшируются только отказы, которые зависят от
входа, — обрезанный ответ, не поместившийся контекст, неразобранный XML.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.config import (CACHE_REDIS_URL, GENERATION_CACHE_TTL, LLM_CACHE_TTL,
                        NEGATIVE_CACHE_TTL, SCORE_CACHE_TTL)
from core import bpmn_edits, bpmn_generator, bpmn_scoring, llm_client, llm_improve
from core.llm_cache import CachedAnswer, LLMCache, sha256_of, source_fingerprint

logger = logging.getLogger(__name__)

# Версии промптов — отпечатки исходников, из которых промпт собирается.
# `bpmn_edits` входит в версию улучшения: словарь операций `OP_SPEC` печатается
# в промпт дословно, поэтому смена словаря обязана снять старые ответы.
GENERATION_PROMPT_VERSION = source_fingerprint(bpmn_generator)
IMPROVE_PROMPT_VERSION = source_fingerprint(llm_improve, bpmn_edits)
SCORE_RULES_VERSION = source_fingerprint(bpmn_scoring)

# Отказ, который зависит от входа: повтор того же текста даст тот же результат,
# поэтому провайдеру его показывать незачем. Всё, чего в списке нет, считается
# сбоем контура и не кэшируется.
_INPUT_DEPENDENT_GENERATION_STEPS = frozenset({
    "llm_truncated", "llm_too_large", "parse", "generation", "value",
})

# Вид кэша → версия промпта, ответившая за его содержимое. Названия видов живут
# здесь, а не приходят из `app/tasks.py`: очередь задач знает про контуры, а
# кэш знает про текст промпта, и обратная зависимость замкнула бы цикл модулей.
_PROMPT_VERSIONS = {
    "generate": GENERATION_PROMPT_VERSION,
    "improve": IMPROVE_PROMPT_VERSION,
}


class AiCache:
    """Обёртка над хранилищем: ключи, сроки и правило «что запоминать».

    Публичный интерфейс — по два метода на контур (`lookup_*` и `remember_*`), а
    не один универсальный `lookup(kind, *входов)`. Причина измерена: универсальная
    форма позволила записывать ключ как хэш от `(пул, xml, запрос)`, а читать —
    как хэш от `(xml, запрос)`. Кэш при этом не ломается шумно, он просто никогда
    не попадает, и единственный способ это заметить — посчитать вызовы модели.
    """

    def __init__(self, store: LLMCache) -> None:
        self.store = store

    # --- генерация -----------------------------------------------------------
    def lookup_generation(self, text: str) -> Optional[CachedAnswer]:
        return self._read("generate", self._generation_inputs(text))

    def remember_generation(self, text: str, result: Dict[str, Any]) -> None:
        """Запомнить исход генерации, если он определён входом.

        Ошибки транспорта (`llm`, `internal`) не запоминаются: это состояние
        провайдера, а не свойства описания процесса.
        """
        inputs = self._generation_inputs(text)
        if result.get("status") == "success":
            self._write("generate", inputs, result=result,
                        ttl=GENERATION_CACHE_TTL)
            return
        step = str(result.get("step") or "")
        if step in _INPUT_DEPENDENT_GENERATION_STEPS:
            self._write("generate", inputs,
                        error=str(result.get("error") or step),
                        ttl=NEGATIVE_CACHE_TTL)

    # --- улучшение -----------------------------------------------------------
    def lookup_improvement(self, xml: str, prompt: str) -> Optional[CachedAnswer]:
        return self._read("improve", self._improvement_inputs(xml, prompt))

    def remember_improvement(self, xml: str, prompt: str,
                             answer: Dict[str, Any]) -> None:
        self._write("improve", self._improvement_inputs(xml, prompt),
                    result=answer, ttl=LLM_CACHE_TTL)

    def refuse_improvement(self, xml: str, prompt: str, error: str) -> None:
        """Схема, которую контур отверг по содержимому: повтор не оплачиваем."""
        self._write("improve", self._improvement_inputs(xml, prompt),
                    error=error, ttl=NEGATIVE_CACHE_TTL)

    # --- скоринг -------------------------------------------------------------
    def score(self, xml: str) -> Optional[Dict[str, Any]]:
        """Отчёт скоринга из кэша. Скоринг детерминирован, поэтому ключ —
        только отпечаток XML, а версия правил снимает устаревшие отчёты."""
        answer = self.store.get(self._score_key(xml),
                                prompt_version=SCORE_RULES_VERSION)
        if answer is None or answer.negative:
            return None
        return answer.result

    def remember_score(self, xml: str, report: Dict[str, Any]) -> None:
        self.store.set(self._score_key(xml), report,
                       prompt_version=SCORE_RULES_VERSION, ttl=SCORE_CACHE_TTL)

    # --- ключи ---------------------------------------------------------------
    def _generation_inputs(self, text: str) -> str:
        return sha256_of(text)

    def _improvement_inputs(self, xml: str, prompt: str) -> str:
        # Пул моделей входит в ключ: ответ «GigaChat 2» и ответ резервной модели
        # на тот же вход — разные ответы, и подмена одной другой была бы
        # неотличима от смены промпта.
        return sha256_of(",".join(llm_client.get_model_pool()), xml, prompt)

    def _key(self, kind: str, inputs: str) -> str:
        return self.store.key(f"llm:{kind}", self._version(kind), inputs)

    def _score_key(self, xml: str) -> str:
        return self.store.key("score", SCORE_RULES_VERSION, sha256_of(xml))

    @staticmethod
    def _version(kind: str) -> str:
        try:
            return _PROMPT_VERSIONS[kind]
        except KeyError:
            raise ValueError(f"кэш для вида {kind!r} не объявлен: у него нет своей "
                             "версии промпта и своего срока, а дефолт дал бы ему "
                             "чужую инвалидацию") from None

    def _read(self, kind: str, inputs: str) -> Optional[CachedAnswer]:
        return self.store.get(self._key(kind, inputs),
                              prompt_version=self._version(kind))

    def _write(self, kind: str, inputs: str, *, ttl: int,
               result: Optional[Dict[str, Any]] = None,
               error: Optional[str] = None) -> None:
        version = self._version(kind)
        key = self._key(kind, inputs)
        if result is not None:
            self.store.set(key, result, prompt_version=version, ttl=ttl)
        else:
            self.store.set_negative(key, error or "", prompt_version=version, ttl=ttl)


_cache: Optional[AiCache] = None


def init_ai_cache() -> Optional[AiCache]:
    """Создаёт кэш, если задан адрес Redis. Отсутствие адреса — штатный режим
    без кэша, а не ошибка: контур обязан работать и так."""
    global _cache
    if not CACHE_REDIS_URL:
        logger.info("Кэш ответов модели выключен (CACHE_REDIS_URL не задан)")
        return None
    if _cache is None:
        store = LLMCache(CACHE_REDIS_URL, namespace="bpmn")
        _cache = AiCache(store)
        if store.ping():
            logger.info("Кэш ответов модели подключён (версия промпта генерации %s, "
                        "улучшения %s, правил скоринга %s)",
                        GENERATION_PROMPT_VERSION[:8], IMPROVE_PROMPT_VERSION[:8],
                        SCORE_RULES_VERSION[:8])
        else:
            # Клиент остаётся: redis-py переподключается сам, и кэш заработает,
            # как только Redis поднимется. Молча оставлять «подключён» было бы
            # неправдой в логе старта.
            logger.warning("Redis кэша недоступен по %s — работаем без кэша, "
                           "повторим подключение при следующем запросе", CACHE_REDIS_URL)
    return _cache


def get_ai_cache() -> Optional[AiCache]:
    return _cache


def close_ai_cache() -> None:
    global _cache
    if _cache is not None:
        _cache.store.close()
        _cache = None


def cache_stats() -> Dict[str, Any]:
    """Счётчики попаданий для операционного маршрута: без них «кэш включён»
    неотличимо от «кэш не попадает». Общие на все процессы — попадания
    считает воркер, а смотрит в них веб-процесс."""
    if _cache is None:
        return {"enabled": False}
    return {"enabled": True, **_cache.store.snapshot().as_dict()}

"""Политика кэширования ответов модели: что, под каким ключом и на сколько.

Механизм живёт в `core/llm_cache.py` и ничего не знает о схеме BPMN; этот
модуль знает только контур приложения — какие входы определяют ответ, какая
версия промпта делает ответ чужим и какие отказы повторять незачем.

Ключ строится только из входов, влияющих на результат (план
redis-cache-and-task-routing, раздел 3.1). Идентификатора
пользователя в ключе нет намеренно: ответ модели на одну и ту же схему и ту же
просьбу одинаков для всех, а доступ к кэшу есть только у авторизованных
маршрутов, которые сами проверяют права на схему.

Версия промпта — отпечаток ВСЕГО, из чего промпт собирается, а не того модуля,
где лежит его текст: ключи хэшируют входы, поэтому правка правила или словаря
операций оставляет ключ прежним, и ответ, посчитанный по прежним правилам,
переживает её молча — как «регрессия качества модели».

Отказ кэшируется только тогда, когда он воспроизводится по одному входу. Всё,
что решила конкретная выборка модели (несобранный JSON, пакет из одних отказов),
повторять не только можно, но и нужно: кэш закрыл бы работоспособное описание на
`NEGATIVE_CACHE_TTL` как «вход заведомо плохой». Отказ провайдера не кэшируется
вовсе: «сервис временно недоступен» перестал быть правдой через минуту, а запись
держала бы пользователя в чужой ошибке весь срок ключа.
"""
from __future__ import annotations

import logging
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

from app.config import (CACHE_REDIS_URL, GENERATION_CACHE_TTL, LLM_CACHE_TTL,
                        NEGATIVE_CACHE_TTL, SCORE_CACHE_TTL)
from core import (bpmn_edits, bpmn_generator, bpmn_layout, bpmn_scoring,
                  llm_client, llm_improve)
from core.llm_cache import CachedAnswer, LLMCache, sha256_of, source_fingerprint

logger = logging.getLogger(__name__)

# Источники, из которых собирается промпт каждого контура.
#
# `bpmn_generator`/`llm_improve` — тексты промптов, `bpmn_edits` — словарь
# операций `OP_SPEC` (печатается в промпт улучшения дословно) и набор тегов
# `EVENT_DEFINITIONS` (интерполируется в системный промпт генерации списком),
# `bpmn_scoring` — `message`/`action` правил: их читает `findings_block`
# (`llm_improve.py:795`) и блок регрессий (`:985`), а пакет операций собирается
# именно из этих строк, то есть текст правила входит в контракт ответа.
# `llm_client` — температура и `max_tokens` по умолчанию (`:538-540`) и
# `DEFAULT_MODEL_POOL` (`:31`): правка транспорта меняет ответ, не меняя ни
# описания, ни схемы. `bpmn_layout` не участвует в тексте промпта, но входит в
# кэшированный результат: у генерации — как BPMN DI схемы, у улучшения — как
# графика узлов и дуг, которые дорисовывает починка (`validate_and_repair` зовёт
# `supplement_di`). Скоринг — детерминированный код, поэтому транспорт и тексты
# промптов к его версии относятся ровно постольку, поскольку правила ссылаются
# на теги аплайера; `bpmn_layout` он держит потому же: теги читает аплайер, а
# аплайер зовёт раскладку. Пересчёт оценки — локальная работа без обращений к
# модели, поэтому лишняя инвалидация здесь стоит миллисекунды, а не деньги.
_PROMPT_SOURCES: Dict[str, Tuple[ModuleType, ...]] = {
    "generate": (bpmn_generator, bpmn_layout, bpmn_edits, llm_client),
    "improve": (llm_improve, bpmn_edits, bpmn_layout, bpmn_scoring, llm_client),
    "score": (bpmn_scoring, bpmn_edits, bpmn_layout),
}


def prompt_version(kind: str) -> str:
    """Версия промпта контура: отпечаток всех его источников.

    У улучшения к отпечатку добавляется корпус: промпт несёт `practices_block`,
    подобранный из `core/bpmn_dataset/`, и правка эталона на месте пересобирала
    бы только индекс (`corpus_fingerprint` в манифесте), оставляя ответы со
    старым RAG-блоком. Здесь именно вызов существующей функции, а не второй
    хэш: две реализации одного отпечатка разъехались бы, и инвалидация ответов
    стала бы зависеть от того, какую из них спросили.
    """
    parts = [source_fingerprint(*_PROMPT_SOURCES[kind])]
    if kind == "improve":
        parts.append(llm_improve.corpus_fingerprint(llm_improve.DATASET_PATH))
    return sha256_of(*parts)[:16]


# Версии считаются один раз при импорте: на каждый ключ читались бы байты
# четырёх модулей и 367 файлов корпуса.
GENERATION_PROMPT_VERSION = prompt_version("generate")
IMPROVE_PROMPT_VERSION = prompt_version("improve")
SCORE_RULES_VERSION = prompt_version("score")

# Шаг отказа генератора — то, что `BPMNGenerator.generate` кладёт в
# `result["step"]`. Политика различает три причины, и различие выражено
# множествами, а не комментарием: неверный класс шага молча превращает удачный
# повтор в «отказ по входу», а такой отказ из кэша пользователь читает как
# «это описание не работает».
#
#  - по входу: тот же вход упрётся в тот же лимит (`max_tokens`, размер
#    контекста) — повтор того же текста оплачивать незачем;
#  - по выборке: зависит от того, что модель вернула на этом вызове. К
#    `generation` это относится через «не удалось выделить ни одного шага»:
#    предмодельные проверки того же шага (пустое или слишком длинное описание)
#    кэша не требуют, они отсекают запрос до обращения к провайдеру, и повтор
#    ничего не стоит;
#  - сбой сервиса: проходит за минуты, а запись держала бы пользователя в
#    чужом состоянии весь срок жизни ключа.
_REFUSALS_BY_INPUT = frozenset({"llm_truncated", "llm_too_large"})
_REFUSALS_BY_SAMPLE = frozenset({"parse", "generation"})
_REFUSALS_BY_OUTAGE = frozenset({"llm", "internal"})
# Отказ не из входа, не из выборки и не из сбоя: он из лимита. `budget` —
# прогон не уложился в отведённое время (тот же вход завтра соберётся), а
# `cancelled` — пользователь сам закрыл задачу. Записать их как «ответ на этот
# текст» значило бы пять минут отвечать новому человеку «переформулируйте» на
# работоспособное описание, а отменённую задачу — отменять заново при каждом
# повторе того же запроса.
_REFUSALS_BY_LIMIT = frozenset({"budget", "cancelled"})
# Шаги, о которых политика уже сказала. Новый шаг генератора должен попасть в
# один из списков выше: тест сверяет их со словарём, который реально отдаёт
# продюсер, а здесь это развилка «молча не кэшируем» против строки в логе.
_KNOWN_GENERATION_STEPS = (_REFUSALS_BY_INPUT | _REFUSALS_BY_SAMPLE
                           | _REFUSALS_BY_OUTAGE | _REFUSALS_BY_LIMIT)

# Вид кэша → версия промпта, ответившая за его содержимое. Названия видов живут
# здесь, а не приходят из `app/tasks.py`: очередь задач знает про контуры, а
# кэш знает про текст промпта, и обратная зависимость замкнула бы цикл модулей.
_PROMPT_VERSIONS = {
    "generate": GENERATION_PROMPT_VERSION,
    "improve": IMPROVE_PROMPT_VERSION,
}


def _model_pool_part() -> str:
    """Пул моделей — часть ключа обоих контуров, а не только улучшения.

    `GIGACHAT_MODEL` задаётся в окружении и в версию промпта не входит: без
    пула в ключе смена модели сутки отдавала бы ответ прежней на том же входе,
    и выглядело бы это как качество модели, а не как подмена. Один источник для
    обеих форм — иначе симметрия контуров держалась бы только договорённостью.
    """
    return ",".join(llm_client.get_model_pool())


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

        Успех — при любом пуле, который сейчас отвечает (он в ключе). Отказ —
        только из `_REFUSALS_BY_INPUT`; шаг, о котором политика ничего не знает,
        не кэшируется и остаётся виден в логе: правка генератора не обязана
        менять поведение кэша молча.
        """
        inputs = self._generation_inputs(text)
        if result.get("status") == "success":
            self._write("generate", inputs, result=result,
                        ttl=GENERATION_CACHE_TTL)
            return
        step = str(result.get("step") or "")
        if step in _REFUSALS_BY_INPUT:
            self._write("generate", inputs,
                        error=str(result.get("error") or step),
                        ttl=NEGATIVE_CACHE_TTL)
        elif step not in _KNOWN_GENERATION_STEPS:
            logger.warning("Генерация: шаг отказа %r не классифицирован политикой — "
                           "не кэшируем, повтор оплатим заново", step)

    # --- улучшение -----------------------------------------------------------
    def lookup_improvement(self, xml: str, prompt: str) -> Optional[CachedAnswer]:
        return self._read("improve", self._improvement_inputs(xml, prompt))

    def remember_improvement(self, xml: str, prompt: str,
                             answer: Dict[str, Any]) -> None:
        """Запомнить ответ улучшения, если пакет применился целиком.

        Частично применённый пакет — это и ответ модели, и те id, которые она
        тогда выдала: следующий запрос на ту же (или уже подправленную) схему
        собирает другой пакет и добивает пропуски. Заморозить половинчатый ответ
        на `LLM_CACHE_TTL` значило бы закрыть единственный путь к полной правке
        — обхода у кэша нет, есть только смена версии промпта.
        """
        report = answer.get("report") or {}
        unresolved = [entry for entry in (report.get("skipped") or [])
                      if not entry.get("reapplied")]
        if unresolved:
            logger.info("Улучшение: пакет применился наполовину (%d пропусков) — "
                        "не кэшируем, чтобы корректирующий запрос не читался из "
                        "кэша", len(unresolved))
            return
        self._write("improve", self._improvement_inputs(xml, prompt),
                    result=answer, ttl=LLM_CACHE_TTL)

    def refuse_improvement(self, xml: str, prompt: str, error: str) -> None:
        """Схема, которую контур отверг по содержимому: повтор не оплачиваем.

        «По содержимому» проверяется здесь же и по самому входу, а не по тексту
        сообщения: текст пишет `llm_improve`, и совпадение строк перестало бы
        работать после правки формулировки — как раз в опасную сторону, когда
        старый отказ продолжает отдаваться.

        Цена такого способа: у входа только один ответ — строится из него
        инвентарь или нет. Всё, что отвергнуто после разборного входа, приходит
        сюда одной строкой и не кэшируется. Для «план не разобран» и «пакет из
        одних отказов» это и есть верно: их решила выборка модели. Но туда же
        попадают два отказа, которые заданы именно входом, — схема не влезает в
        контекст модели и план обрезан по лимиту токенов. Генерация их кэширует
        (`_REFUSALS_BY_INPUT`), улучшение — нет. Это не противоречие, а
        известная цена: различить их можно только по классу исключения, а
        `app/tasks.py:420` передаёт сюда `str(error)`, то есть признак теряется
        до входа в политику. Строковый матч по формулировке связал бы кэш с
        текстом сообщения, а ошибка здесь стоит только денег за повтор, не
        чужого ответа.
        """
        if not self._refusal_is_reproducible_by_input(xml):
            logger.info("Улучшение: отказ %r порождён ответом модели, а не входом — "
                        "не кэшируем", error)
            return
        self._write("improve", self._improvement_inputs(xml, prompt),
                    error=error, ttl=NEGATIVE_CACHE_TTL)

    @staticmethod
    def _refusal_is_reproducible_by_input(xml: str) -> bool:
        """Схема, из которой не строится инвентарь, отвергнет и следующий вызов.

        Проверка — тот же `build_inventory`, что у контура перед первым
        обращением к модели: он стоит до расходования слота провайдера, поэтому
        такой повтор не сто́ит ничего, а его стоимость для политики не важна —
        важна воспроизводимость.
        """
        try:
            bpmn_edits.build_inventory(xml)
        except Exception:  # noqa: BLE001 — причина отказа уже в тексте `error`
            return True
        return False

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
        return sha256_of(_model_pool_part(), text)

    def _improvement_inputs(self, xml: str, prompt: str) -> str:
        return sha256_of(_model_pool_part(), xml, prompt)

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
        elif store.client_construction_error is not None:
            # Другой случай, чем «сервер не отвечает»: неразобранный адрес не
            # починится сам, и обещать переподключение было бы неправдой —
            # оператор ждал бы, пока кэш «оживёт», и чинил бы не ту переменную.
            # Отсюда ERROR, а не WARNING: без кэша сервис работает, но каждая
            # задача оплачивается вызовом модели, и молча это не проходит.
            logger.error("Кэш ответов модели ВЫКЛЮЧЕН: адрес %r из CACHE_REDIS_URL "
                         "неразобран (%s). Сервис поднят и вызывает модель как "
                         "обычно; чтобы включить кэш, поправьте переменную "
                         "ОКРУЖЕНИЯ и перезапустите процесс — сам Redis здесь ни "
                         "при чём", CACHE_REDIS_URL, store.client_construction_error)
        else:
            # Клиент остаётся: redis-py переподключается сам, и кэш заработает,
            # как только Redis поднимется. Молча оставлять «подключён» было бы
            # неправдой в логе старта.
            logger.warning("Redis кэша недоступен по %s — работаем без кэша, "
                           "повторим подключение при следующем запросе",
                           CACHE_REDIS_URL)
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
    неотличимо от «кэш не попадает».

    Цифры берутся из общего хэша Redis — попадания считают воркеры, а смотрит в
    них веб-процесс. Поле `counters` говорит, откуда число: непрочитанный общий
    хэш оставил бы процессу его локальные нули, и «hits: 0» при работающем кэше
    прочитался бы как «кэш бесполезен».
    """
    if _cache is None:
        return {"enabled": False}
    stats, shared = _cache.store.report()
    return {"enabled": True, "counters": "shared" if shared else "process",
            **stats.as_dict()}

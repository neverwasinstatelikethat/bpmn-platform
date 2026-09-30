"""Кэш ответов модели: ключи, срок, версии промпта и поведение при отказе.

Redis подменяется in-process словарём: проверяется контракт слоя (что кладётся,
что читается и когда считается промахом), а не конкретный сервер. Живой Redis с
настоящим подключением стоит в `tests/test_tasks_redis.py`, чтобы этот файл
можно было гонять без сети и без Docker.

Главная пара проверок здесь — не «кэш ускоряет», а «кэш не врёт»: запись,
посчитанная по другому промпту или по другим правилам, опаснее, чем промах,
потому что пользователь получает рекомендацию, которой для его схемы никогда не
было.
"""
import time

import pytest

from core import bpmn_edits, bpmn_generator, bpmn_scoring, llm_improve, llm_client
from core.llm_cache import LLMCache, sha256_of, source_fingerprint


class FakeRedis:
    """Срез Redis, достаточный для слоя кэша: get/set с сроком, delete и хэш
    счётчиков."""

    def __init__(self, fail=False):
        self.store = {}
        self.expiry = {}
        self.fail = fail
        self.closed = False

    def _alive(self, key):
        deadline = self.expiry.get(key)
        return deadline is None or deadline > time.time()

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis не отвечает")
        if key not in self.store or not self._alive(key):
            return None
        return self.store[key]

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis не отвечает")
        self.store[key] = value
        self.expiry[key] = time.time() + ex if ex else None
        return True

    def hincrby(self, key, field, amount=1):
        if self.fail:
            raise ConnectionError("redis не отвечает")
        hash_ = self.store.setdefault(key, {})
        hash_[field] = int(hash_.get(field, 0)) + amount
        return hash_[field]

    def hgetall(self, key):
        if self.fail:
            raise ConnectionError("redis не отвечает")
        return dict(self.store.get(key) or {})

    def delete(self, key):
        self.store.pop(key, None)
        self.expiry.pop(key, None)
        return 1

    def ping(self):
        if self.fail:
            raise ConnectionError("redis не отвечает")
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def fake():
    client = FakeRedis()
    cache = LLMCache("redis://localhost:6379/1", namespace="bpmn")
    cache._client = client
    return cache, client


class TestWriteAndReadKeysAreTheSame:
    """Пара `lookup_*`/`remember_*` обязана сходиться по ключу.

    Универсальный `lookup(kind, *входов)` этого не гарантировал: запись
    считалась хэшем от (пул, xml, запрос), а чтение — хэшем от (xml, запрос),
    и кэш молча не попадал никогда."""

    def test_generation_pair_hits(self, fake):
        from app.services import ai_cache as ai_cache_service

        cache = ai_cache_service.AiCache(fake[0])
        cache.remember_generation("заявка", {"status": "success", "bpmn": "x"})
        assert cache.lookup_generation("заявка") is not None

    def test_improvement_pair_hits(self, fake, monkeypatch):
        from app.services import ai_cache as ai_cache_service
        from core import llm_client

        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat"])
        cache = ai_cache_service.AiCache(fake[0])
        cache.remember_improvement("<xml/>", "улучши", {"recommendations": "готово"})
        assert cache.lookup_improvement("<xml/>", "улучши").result["recommendations"]

    def test_improvement_refusal_pair_hits(self, fake):
        from app.services import ai_cache as ai_cache_service

        cache = ai_cache_service.AiCache(fake[0])
        cache.refuse_improvement("<xml/>", "улучши", "Схема слишком велика")
        answer = cache.lookup_improvement("<xml/>", "улучши")
        assert answer.negative and answer.error == "Схема слишком велика"


class TestKeys:
    def test_key_separates_inputs_that_only_look_equal(self):
        """Ключи «ab»+«c» и «a»+«bc» обязаны различаться: без разделителя
        склейка входов столкнула бы разные схемы в один ответ."""
        assert sha256_of("ab", "c") != sha256_of("a", "bc")

    def test_namespace_is_part_of_the_key(self, fake):
        cache, _client = fake
        assert cache.key("llm:generate", "x").startswith("bpmn:llm:generate:")

    def test_fingerprint_follows_module_source(self):
        """Версия промпта обязана меняться вместе с текстом промпта, иначе кэш
        отдаёт ответы, посчитанные по другим правилам."""
        first = source_fingerprint(llm_improve, bpmn_edits)
        assert first == source_fingerprint(llm_improve, bpmn_edits)
        assert first != source_fingerprint(bpmn_generator)
        assert first != source_fingerprint(llm_improve, bpmn_scoring)

    def test_generation_and_improvement_versions_are_independent(self):
        """Правка промпта генерации не имеет права сбрасывать кэш улучшения:
        платный ответ модели остался верен своему входу."""
        from app.services import ai_cache as ai_cache_service

        assert (ai_cache_service.GENERATION_PROMPT_VERSION
                != ai_cache_service.IMPROVE_PROMPT_VERSION)
        assert ai_cache_service.IMPROVE_PROMPT_VERSION == source_fingerprint(
            llm_improve, bpmn_edits)


class TestCacheRoundTrip:
    def test_read_back_returns_the_same_result(self, fake):
        cache, _client = fake
        key = cache.key("llm:generate", "hash")
        assert cache.set(key, {"bpmn": "<xml/>"}, prompt_version="v1", ttl=60)
        answer = cache.get(key, prompt_version="v1")
        assert answer.result == {"bpmn": "<xml/>"}
        assert not answer.negative

    def test_other_prompt_version_is_a_miss_and_the_record_is_dropped(self, fake):
        """Устаревшую запись нельзя просто игнорировать до истечения срока:
        её надо снять, иначе кэш копит ответы, которые никогда не отдаст."""
        cache, client = fake
        key = cache.key("llm:improve", "hash")
        cache.set(key, {"recommendations": "старые"}, prompt_version="v1", ttl=60)
        assert cache.get(key, prompt_version="v2") is None
        assert key not in client.store

    def test_expired_record_is_a_miss(self, fake):
        cache, client = fake
        key = cache.key("llm:generate", "hash")
        cache.set(key, {"bpmn": "<xml/>"}, prompt_version="v1", ttl=1)
        client.expiry[key] = time.time() - 1
        assert cache.get(key, prompt_version="v1") is None

    def test_zero_ttl_writes_nothing(self, fake):
        cache, client = fake
        assert cache.set(cache.key("k"), {"a": 1}, prompt_version="v1", ttl=0) is False
        assert client.store == {}

    def test_negative_record_is_marked_and_counted_separately(self, fake):
        """Отказ по входу — отдельный случай: он экономит вызов модели, но не
        является ответом и не должен считаться попаданием в кэш ответов."""
        cache, _client = fake
        key = cache.key("llm:improve", "hash")
        assert cache.set_negative(key, "Схема слишком велика",
                                  prompt_version="v1", ttl=300)
        answer = cache.get(key, prompt_version="v1")
        assert answer.negative and answer.error == "Схема слишком велика"
        assert cache.stats.negative_hits == 1 and cache.stats.hits == 0

    def test_corrupt_record_is_a_miss_not_an_exception(self, fake):
        """Битое значение опасче отсутствия: его нельзя отдать, и оставлять его
        в кэше до истечения срока значит читать заново на каждом запросе."""
        cache, client = fake
        key = cache.key("llm:generate", "hash")
        client.store[key] = "{не json"
        assert cache.get(key, prompt_version="v1") is None
        assert key not in client.store

    def test_record_without_prompt_version_is_rejected(self, fake):
        """Запись без версии промпта — чужая запись: отдать её нельзя, потому
        что сверять не с чем. Формат специально кладётся сырым JSON, а не через
        схему: так выглядит значение, оставшееся от прежней версии кэша."""
        cache, client = fake
        key = cache.key("llm:generate", "hash")
        client.store[key] = '{"result": {"a": 1}, "created_at": 1}'
        assert cache.get(key, prompt_version="v1") is None


class TestFailureModes:
    def test_unreachable_redis_reads_as_a_miss(self, fake):
        """Сбой кэша не ошибка запроса: вызывающий код идёт в модель."""
        cache, client = fake
        client.fail = True
        assert cache.get("k", prompt_version="v1") is None
        assert cache.set("k", {"a": 1}, prompt_version="v1", ttl=60) is False
        assert cache.stats.errors == 2

    def test_ping_failure_is_reported_not_raised(self, fake):
        cache, client = fake
        client.fail = True
        assert cache.ping() is False

    def test_error_is_logged_once_not_per_request(self, fake, caplog):
        """При часто падающем Redis лог на каждый запрос утонул бы: повтор
        разрешено не чаще раза в минуту, но счётчик ошибок растёт всегда."""
        cache, client = fake
        client.fail = True
        with caplog.at_level("WARNING"):
            for _ in range(50):
                cache.get("k", prompt_version="v1")
        assert cache.stats.errors == 50
        assert len([row for row in caplog.text.splitlines() if "Кэш" in row]) <= 2

    def test_close_is_idempotent(self, fake):
        cache, client = fake
        cache.close()
        cache.close()
        assert client.closed


class TestAiCachePolicy:
    """Что политика считает успехом, отказом и чем не делится между контурами."""

    def cache(self, fake):
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        return ai_cache_service.AiCache(store), client

    def test_generation_success_is_cached_for_a_day(self, fake):
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "success", "bpmn": "<xml/>"})
        key = cache._key("generate", cache._generation_inputs("заявка"))
        assert client.expiry[key] - time.time() == pytest.approx(
            self.ttl(), abs=5)
        answer = cache.lookup_generation("заявка")
        assert answer.result["bpmn"] == "<xml/>"

    @staticmethod
    def ttl():
        from app.config import GENERATION_CACHE_TTL

        return GENERATION_CACHE_TTL

    def test_provider_outage_is_not_cached(self, fake):
        """«Сервис временно недоступен» — состояние провайдера, а не свойства
        входа. Запомнить его значило бы держать пользователя в чужой ошибке
        сутки."""
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error", "step": "llm",
                                             "error": "недоступен"})
        assert client.store == {}

    def test_internal_error_is_not_cached(self, fake):
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error", "step": "internal",
                                             "error": "Упнутр. ошибка"})
        assert client.store == {}

    def test_input_dependent_refusal_is_cached_shortly(self, fake):
        """Обрезанный ответ и не поместившийся контекст повторять незачем: тот
        же текст даст тот же отказ."""
        from app.config import NEGATIVE_CACHE_TTL

        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error",
                                             "step": "llm_truncated",
                                             "error": "Обрезан"})
        key = cache._key("generate", cache._generation_inputs("заявка"))
        assert client.expiry[key] - time.time() == pytest.approx(NEGATIVE_CACHE_TTL,
                                                                 abs=5)
        answer = cache.lookup_generation("заявка")
        assert answer.negative

    def test_score_is_cached_by_xml_and_rules_version(self, fake):
        """Скоринг детерминирован: тот же XML при тех же правилах обязан
        читаться из кэша, а смена правил — сбрасывать отчёт."""
        cache, _client = self.cache(fake)
        cache.remember_score("<definitions/>", {"score": 88})
        assert cache.score("<definitions/>")["score"] == 88
        assert cache.score("<definitions/> ") is None

    def test_improvement_key_includes_the_model_pool(self, fake, monkeypatch):
        """Ответ другой модели на тот же вход — другой ответ. Подмена одной
        модели другой в кэше была бы неотличима от смены промпта."""
        cache, _client = self.cache(fake)
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat"])
        first = cache._key("improve", cache._improvement_inputs("<xml/>", "улучши"))
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat2"])
        second = cache._key("improve", cache._improvement_inputs("<xml/>", "улучши"))
        assert first != second

    def test_unknown_kind_is_refused(self, fake):
        """Новый вид кэша обязан быть заявлен явно с версией и сроком: дефолт
        дал бы ему чужую версию промпта, и инвалидация сломалась бы молча."""
        cache, _client = self.cache(fake)
        with pytest.raises(ValueError):
            cache._read("teleport", "x")

    def test_cache_is_absent_without_a_url(self, monkeypatch):
        """Без адреса кэша нет ни клиента, ни обращений: replay-прогон харнесса
        не может прочитать чужой ответ."""
        from app.services import ai_cache as ai_cache_service

        monkeypatch.setattr(ai_cache_service, "CACHE_REDIS_URL", "")
        monkeypatch.setattr(ai_cache_service, "_cache", None)
        assert ai_cache_service.init_ai_cache() is None
        assert ai_cache_service.get_ai_cache() is None
        assert ai_cache_service.cache_stats() == {"enabled": False}

    def test_stats_expose_the_hit_share(self, fake):
        """Доля попаданий — единственное, чем «кэш включён» отличается от «кэш
        работает»."""
        cache, _client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "success", "bpmn": "x"})
        assert cache.lookup_generation("заявка") is not None
        assert cache.lookup_generation("другая заявка") is None
        stats = cache.store.snapshot().as_dict()
        assert stats["hits"] == 1 and stats["misses"] == 1
        assert stats["hit_share"] == 0.5


class TestCountersAreShared:
    """Счётчики живут в Redis, а не в памяти процесса.

    Попадания считает воркер, а в них смотрит веб-процесс через
    `/api/tasks/stats`. Локальный счётчик показывал там нули при работающем
    кэше — это метрика, которая не молчит, а врёт: по ней видно «кэш не
    попадает», и кэш выключают."""

    def test_second_process_sees_the_first_ones_counts(self, fake):
        from app.services import ai_cache as ai_cache_service

        worker = ai_cache_service.AiCache(fake[0])
        worker.remember_generation("заявка", {"status": "success", "bpmn": "x"})
        worker.lookup_generation("заявка")
        worker.lookup_generation("чужая заявка")

        # Отдельный экземпляр поверх того же сервера — так же, как веб-процесс
        # поверх заполненного воркером Redis.
        web = LLMCache("redis://localhost:6379/1", namespace="bpmn")
        web._client = fake[1]
        stats = web.snapshot()
        assert (stats.hits, stats.misses, stats.writes) == (1, 1, 1)
        assert web.stats.hits == 0, "локальный счётчик этого процесса пуст"

    def test_counters_survive_a_broken_stats_read(self, fake):
        """Сбой чтения счётчиков не имеет права превратиться в ошибку запроса:
        тогда хотя бы видно, что насчитал этот процесс."""
        cache, client = fake
        cache.set(cache.key("llm:generate", "v", "x"), {"a": 1},
                  prompt_version="v", ttl=60)
        client.fail = True
        assert cache.snapshot().writes == 1

    def test_a_stats_failure_does_not_break_a_hit(self, fake):
        cache, client = fake
        cache.set(cache.key("k", "v"), {"a": 1}, prompt_version="v", ttl=60)

        real_hincrby = client.hincrby

        def boom(*_args, **_kwargs):
            raise ConnectionError("счётчики недоступны")

        client.hincrby = boom
        assert cache.get(cache.key("k", "v"), prompt_version="v") is not None
        client.hincrby = real_hincrby


class TestCacheIsNotOptIn:
    def test_unreachable_server_stays_configured(self, monkeypatch):
        """Кэш при недоступном Redis не выключается, а остаётся непрогретым:
        redis-py переподключается сам, и кэш заработает, как только сервер
        поднимется, — без рестарта приложения. Выключаться молча означало бы
        держать пользователя на медленном контуре после одного сбоя."""
        from app.services import ai_cache as ai_cache_service

        broken = FakeRedis()
        broken.fail = True
        real_init = ai_cache_service.LLMCache.__init__
        seen = []

        def spy(self, url, namespace, **kwargs):
            real_init(self, url, namespace, **kwargs)
            self._client = broken
            seen.append(url)

        monkeypatch.setattr(ai_cache_service.LLMCache, "__init__", spy)
        monkeypatch.setattr(ai_cache_service, "CACHE_REDIS_URL", "redis://cache:6379/1")
        monkeypatch.setattr(ai_cache_service, "_cache", None)
        try:
            assert ai_cache_service.init_ai_cache() is not None
            assert seen == ["redis://cache:6379/1"]
            assert ai_cache_service.cache_stats()["enabled"] is True
            # Недоступность видна по ping и по счётчику ошибок, но не является
            # ни исключением, ни выключенным кэшем.
            assert ai_cache_service.get_ai_cache().store.ping() is False
            assert ai_cache_service.cache_stats()["errors"] >= 1
        finally:
            ai_cache_service.close_ai_cache()

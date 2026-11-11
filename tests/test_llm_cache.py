"""Кэш ответов модели: ключи, срок, версии промпта и поведение при отказе.

Redis подменяется in-process словарём: проверяется контракт слоя (что кладётся,
что читается и когда считается промахом), а не конкретный сервер. Живой Redis с
настоящим подключением стоит в `tests/test_tasks_redis.py`, чтобы этот файл
можно было гонять без сети и без Docker.

Главная пара проверок здесь — не «кэш ускоряет», а «кэш не врёт»: запись,
посчитанная по другому промпту или по другим правилам, опаснее, чем промах,
потому что пользователь получает рекомендацию, которой для его схемы никогда не
было. Поэтому версия контура проверяется не значением, а поведением: каждый
источник, из которого промпт реально собирается, подменяется копией-с-правкой, и
если версия не двинулась — ключ слеп к этому источнику, а устаревший ответ
выглядит как регрессия качества модели.
"""
import ast
import contextlib
import importlib
import inspect
import re
import threading
import time
from pathlib import Path

import pytest

from core import (bpmn_edits, bpmn_generator, bpmn_layout, bpmn_scoring,
                  llm_improve, llm_client)
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


def _touch_source(module, tmp_path, monkeypatch):
    """Копия исходника с добавленной строкой и подменённый `__file__`.

    Так выглядит правка промпта для `source_fingerprint`, который читает байты
    файла, а не имя модуля: без подмены байтов тест проверял бы равенство двух
    констант, а не то, что версия вообще способна двинуться.
    """
    original = Path(module.__file__)
    copy = tmp_path / f"{module.__name__}.py"
    copy.write_bytes(original.read_bytes() + b"\n# PROMPT_SOURCE_TOUCHED\n")
    monkeypatch.setattr(module, "__file__", str(copy))
    return copy


def _short_name(module) -> str:
    return module.__name__.rsplit(".", 1)[-1]


def _sibling_imports(module) -> set:
    """Модули того же пакета, которые этот модуль импортирует.

    Выводится из дерева `ast`, а не переписывается в тесте руками: «что промпт
    читает» — это факт исходника, и только так проверка переживает правку,
    которую кто-то забыл отразить в договоре теста.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    package = (module.__package__ or "").split(".")[0]
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            # `from . import x` и `from .x import y`: уровень > 0 — это сосед
            # по пакету, а не внешний импорт.
            if node.module:
                deps.add(node.module.split(".")[0])
            else:
                deps.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) > 1 and parts[0] == package:
                    deps.add(parts[1])
    return deps


def cached_keys(client):
    """Ключи ответов в хранилище. Счётчики попаданий лежат в том же Redis, и в
    проверках «ничего не записано» участвовать не должны: их-то слой пишет
    всегда."""
    return {key for key in client.store if not key.endswith(":stats")}


# Что импорт сервиса считает один раз при запуске процесса: версии контуров и
# словарь, из которого `AiCache` берёт версию и для ключа, и для сверки записи.
# `_cache` — глобальный синглтон: после `reload` он сбросился бы в None, и
# следующий тест увидел бы выключенный кэш.
_IMPORT_TIME_NAMES = ("GENERATION_PROMPT_VERSION", "IMPROVE_PROMPT_VERSION",
                      "SCORE_RULES_VERSION", "_PROMPT_VERSIONS", "_cache")


@contextlib.contextmanager
def reimported(ai_cache_service):
    """Пересчёт версий так, как его делает новый процесс после правки промпта.

    `source_fingerprint` читает байты исходников при импорте, поэтому правка
    источника попадает в кэш не мгновенно, а со следующим запуском. `reload`
    исполняет ровно этот путь в том же объекте модуля: методы `AiCache`, созданные
    до перезапуска, видят уже новые значения модульных имён, и проверка идёт через
    публичные `lookup_*`, а не через сравнение строк версии.

    Снимок возвращается, чтобы следующий тест не читал версии, снятые подменёнными
    исходниками этого: иначе `test_scoring_rule_text_moves_improve_and_score`
    сравнивал бы свежий отпечаток с отпечатком чужого временного файла.
    """
    snapshot = {name: getattr(ai_cache_service, name) for name in _IMPORT_TIME_NAMES}
    try:
        yield importlib.reload(ai_cache_service)
    finally:
        ai_cache_service.__dict__.update(snapshot)


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
        """Пара `refuse_improvement`/`lookup_improvement` сходится по ключу.

        Вход — XML, который контур не может даже разобрать: это единственный
        отказ улучшения, воспроизводимый по одному входу, и единственный,
        который политика разрешено запоминать (`TestRefusalPolicy`)."""
        from app.services import ai_cache as ai_cache_service

        broken_xml = "это не xml: <definitions"
        cache = ai_cache_service.AiCache(fake[0])
        cache.refuse_improvement(broken_xml, "улучши",
                                 "Не удалось разобрать XML схемы")
        answer = cache.lookup_improvement(broken_xml, "улучши")
        assert answer.negative and answer.error == "Не удалось разобрать XML схемы"


class TestKeys:
    def test_key_separates_inputs_that_only_look_equal(self):
        """Ключи «ab»+«c» и «a»+«bc» обязаны различаться: без разделителя
        склейка входов столкнула бы разные схемы в один ответ."""
        assert sha256_of("ab", "c") != sha256_of("a", "bc")

    def test_namespace_is_part_of_the_key(self, fake):
        cache, _client = fake
        assert cache.key("llm:generate", "x").startswith("bpmn:llm:generate:")

    def test_fingerprint_follows_module_bytes(self, tmp_path, monkeypatch):
        """Версия считается по байтам исходника, а не по имени модуля: правка
        текста промпта — это правка байтов. Тот же способ подмены используется
        ниже, поэтому проверка стоит здесь, а не в каждом контуре."""
        base = source_fingerprint(bpmn_edits)
        assert base == source_fingerprint(bpmn_edits)
        _touch_source(bpmn_edits, tmp_path, monkeypatch)
        assert source_fingerprint(bpmn_edits) != base


class TestVersionFollowsThePromptSources:
    """Версия контура обязана двигаться за каждым источником его промпта.

    Ключи хэшируют входы (текст описания, XML, запрос), а промпт собирается из
    модульных констант и данных — значит устаревший ответ возможен при
    неизменном ключе, и заметен он только как «модель стала хуже». Списки
    источников ниже независимы от таблицы сервиса: они перечисляют то, что
    реально печатается в промпт, по строкам исходника.

    `bpmn_edits` — словарь операций `OP_SPEC` (improve) и набор тегов событий
    `EVENT_DEFINITIONS`, интерполируемый в системный промпт генерации
    (`bpmn_generator.py:239`). `bpmn_scoring` — `findings_block` и блок
    регрессий, читающие `rules[name]["action"]`/`["message"]`
    (`llm_improve.py:795`, `:985`, `:1389`). `llm_client` — температура и
    `max_tokens` по умолчанию (`:538-540`) и `DEFAULT_MODEL_POOL` (`:31`).
    """

    PROMPT_SOURCES = {
        "generate": (bpmn_generator, bpmn_layout, bpmn_edits, llm_client),
        "improve": (llm_improve, bpmn_edits, bpmn_layout, bpmn_scoring,
                    llm_client),
        "score": (bpmn_scoring, bpmn_edits, bpmn_layout),
    }

    @pytest.mark.parametrize("kind", sorted(PROMPT_SOURCES))
    def test_every_prompt_source_moves_the_version(self, kind, tmp_path, monkeypatch):
        from app.services import ai_cache as ai_cache_service

        declared = tuple(ai_cache_service._PROMPT_SOURCES[kind])
        assert declared == self.PROMPT_SOURCES[kind], (
            f"контур {kind!r} смотрит не во все источники своего промпта: "
            "ключ останется слепым к правке одного из них")

        base = ai_cache_service.prompt_version(kind)
        for module in self.PROMPT_SOURCES[kind]:
            _touch_source(module, tmp_path, monkeypatch)
            assert ai_cache_service.prompt_version(kind) != base, (
                f"правка {module.__name__} не снимает ответы контура {kind!r}")

    def test_scoring_rule_text_moves_improve_and_score(self, tmp_path, monkeypatch):
        """Подсказка правила — часть контракта ответа улучшения: пакет операций
        собирается из id, названных в тексте `action`, поэтому правка
        `wait_without_sla` обязана снять семидневный ответ, посчитанным по
        старым правилам."""
        from app.services import ai_cache as ai_cache_service

        improve_before = ai_cache_service.IMPROVE_PROMPT_VERSION
        generate_before = ai_cache_service.GENERATION_PROMPT_VERSION
        score_before = ai_cache_service.SCORE_RULES_VERSION
        _touch_source(bpmn_scoring, tmp_path, monkeypatch)
        assert ai_cache_service.prompt_version("improve") != improve_before
        assert ai_cache_service.prompt_version("score") != score_before
        # Генерация правил не читает — её платный ответ остаётся своим.
        assert ai_cache_service.prompt_version("generate") == generate_before

    def test_applier_tag_set_moves_generation(self, tmp_path, monkeypatch):
        """`EVENT_DEFINITIONS` печатается в системный промпт генерации списком
        (`", ".join(sorted(EVENT_DEFINITIONS))`), а живёт в аплайере: смена
        набора тегов обязана снять ответы, в которых модель перечисляла старые."""
        from app.services import ai_cache as ai_cache_service

        before = ai_cache_service.GENERATION_PROMPT_VERSION
        _touch_source(bpmn_edits, tmp_path, monkeypatch)
        assert ai_cache_service.prompt_version("generate") != before

    def test_transport_sampling_defaults_move_both_llm_versions(self, tmp_path,
                                                                monkeypatch):
        """`temperature`/`max_tokens` по умолчанию и пул моделей в транспортном
        слое меняют ответ, не меняя ни описания, ни схемы: без них в версии ключ
        оставался бы прежним, а ответ — другим."""
        from app.services import ai_cache as ai_cache_service

        generate_before = ai_cache_service.GENERATION_PROMPT_VERSION
        improve_before = ai_cache_service.IMPROVE_PROMPT_VERSION
        score_before = ai_cache_service.SCORE_RULES_VERSION
        _touch_source(llm_client, tmp_path, monkeypatch)
        assert ai_cache_service.prompt_version("generate") != generate_before
        assert ai_cache_service.prompt_version("improve") != improve_before
        # Скоринг — детерминированный код, транспорт к нему не относится.
        assert ai_cache_service.prompt_version("score") == score_before

    def test_editing_a_corpus_etalon_moves_the_improve_version(self, tmp_path,
                                                              monkeypatch):
        """Промпт улучшения несёт `practices_block`, подобранный из корпуса,
        поэтому отпечаток корпуса входит в его версию. Правка эталона на месте
        пересобирает индекс (`corpus_fingerprint` в манифесте), но не снимала бы
        ответы — и RAG-блок в них остался бы старым."""
        from app.services import ai_cache as ai_cache_service

        dataset = tmp_path / "corpus"
        dataset.mkdir()
        etalon = dataset / "purchase_approval.bpmn"
        etalon.write_text("<definitions><process id=\"p\"/></definitions>",
                          encoding="utf-8")
        monkeypatch.setattr(llm_improve, "DATASET_PATH", str(dataset))
        base = ai_cache_service.prompt_version("improve")
        assert base == ai_cache_service.prompt_version("improve")

        etalon.write_text("<definitions><process id=\"p\"><task id=\"t\"/></process>"
                          "</definitions>", encoding="utf-8")
        assert ai_cache_service.prompt_version("improve") != base
        # У генерации RAG-блока нет — корпус её ответы не снимает.
        generate = ai_cache_service.prompt_version("generate")
        assert ai_cache_service.prompt_version("generate") == generate

    def test_contour_versions_differ(self):
        """Правка промпта генерации не имеет права сбрасывать кэш улучшения:
        платный ответ модели остался верен своему входу. Разные версии — то, чем
        это оплачивается."""
        from app.services import ai_cache as ai_cache_service

        versions = {ai_cache_service.prompt_version(kind)
                    for kind in ("generate", "improve", "score")}
        assert len(versions) == 3, ("контуры поделили версию и обнуляли бы "
                                    "ответы друг друга")


class TestPromptTableIsWiredToWhatThePromptReads:
    """Таблица источников обязана догонять промпты, а не наоборот.

    `TestVersionFollowsThePromptSources` сверяет `_PROMPT_SOURCES` со списком,
    который автор теста прочитал из промптов глазами. Это ловит расхождение в
    одну сторону (из таблицы убрали модуль), но не главную ошибку: промпт начал
    печатать ещё один модуль, а в таблицу его не добавили. Версия тогда стоит
    на месте, ответ, посчитанный по старому тексту, переживает деплой и читается
    как «качество модели упало».

    Поэтому «что промпт читает» здесь выводится из исходников (`ast`), а
    ожидаемое потребление — из самой таблицы: новое имя, добавленное в промпт и
    не проведённое в `_PROMPT_SOURCES`, роняет проверку без правки теста.
    """

    # Кто из контуров обязан реагировать на правку источника. Сверяется с
    # таблицей сервиса, а не с импортами: это человеческая формулировка
    # контракта, и её расхождение с выводимым набором — тоже находка.
    CONSUMERS = {
        bpmn_edits: {"generate", "improve", "score"},
        bpmn_scoring: {"improve", "score"},
        bpmn_generator: {"generate"},
        bpmn_layout: {"generate", "improve", "score"},
        llm_improve: {"improve"},
        llm_client: {"generate", "improve"},
    }

    def test_the_table_covers_every_module_the_prompt_imports(self):
        for kind, sources in self.table().items():
            declared = {_short_name(module) for module in sources}
            for module in sources:
                untracked = _sibling_imports(module) - declared
                assert not untracked, (
                    f"контур {kind!r}: {untracked} импортируется тем, из чего "
                    "собирается его промпт, но не входит в его источники — "
                    "правка такого модуля не снимет ответ, посчитанный по нему")

    @staticmethod
    def table():
        from app.services import ai_cache as ai_cache_service

        return ai_cache_service._PROMPT_SOURCES

    def test_no_contour_is_keyed_by_an_undeclared_version(self):
        """Вид кэша, которым ключуют ответы, обязан иметь свою таблицу
        источников: иначе у него есть версия, но нет того, что обязано её
        снимать."""
        from app.services import ai_cache as ai_cache_service

        versioned = set(ai_cache_service._PROMPT_VERSIONS)
        assert versioned <= set(ai_cache_service._PROMPT_SOURCES), (
            f"кэш ключуется версиями {sorted(versioned - set(self.table()))}, "
            "а источников у них нет")

    def test_every_declared_source_really_moves_its_contour(self, tmp_path):
        """Обратная сторона: модуль, записанный в таблицу, не имеет права быть
        мёртвым грузом — иначе слепого места он не закрывает, а только создаёт
        видимость закрытия. Проход по таблице, а не по списку теста: новое имя
        проверяется само."""
        from app.services import ai_cache as ai_cache_service

        for kind, sources in self.table().items():
            for module in sources:
                # Отдельный `monkeypatch` на итерацию: подменённый `__file__`
                # одного источника не должен влиять на версию другого контура.
                with pytest.MonkeyPatch.context() as patch:
                    base = ai_cache_service.prompt_version(kind)
                    _touch_source(module, tmp_path, patch)
                    assert ai_cache_service.prompt_version(kind) != base, (
                        f"{_short_name(module)} объявлен источником контура "
                        f"{kind!r}, но его байты версию не двигают")

    @pytest.mark.parametrize("module,expected", [
        (module, consumers) for module, consumers in CONSUMERS.items()
    ])
    def test_editing_a_source_moves_exactly_the_contours_that_declare_it(
            self, module, expected, tmp_path, monkeypatch):
        """Правка источника снимает версии тех контуров, которые его читают, и
        только их.

        `bpmn_edits` — главный случай: в нём и `OP_SPEC`, который промпт
        улучшения печатает дословно, и `EVENT_DEFINITIONS`, интерполируемые в
        системный промпт генерации списком, и теги, которые читает скоринг.
        Значит правка одного файла обязана снять ответы обоих платных контуров.
        `bpmn_scoring` наоборот: платит за улучшение и за скоринг, но не за
        генерацию, — генерация правил не читает, и обнулять её сутки было бы
        просто потерей денег.
        """
        from app.services import ai_cache as ai_cache_service

        declared = {kind for kind, sources in self.table().items()
                    if module in sources}
        assert declared == expected, (
            f"{_short_name(module)}: таблица источников разошлась с тем, кто его "
            "реально печатает")

        before = {kind: ai_cache_service.prompt_version(kind) for kind in self.table()}
        _touch_source(module, tmp_path, monkeypatch)
        for kind, old in before.items():
            moved = ai_cache_service.prompt_version(kind) != old
            assert moved == (kind in expected), (
                f"правка {_short_name(module)} {'' if moved else 'не '}сняла контур "
                f"{kind!r}, а по контракту должно быть наоборот: читает его "
                f"{_short_name(module)} или нет — решает таблица {sorted(expected)}")


class TestStaleAnswerIsNotServedAfterASourceEdit:
    """Ответ, записанный до правки источника промпта, после правки не читается.

    `TestVersionFollowsThePromptSources` сверяет строки версий, и одного этого
    мало: версия могла бы двигаться, но не входить в кэш — ключ и сверку записи
    собирают модульные константы (`_PROMPT_VERSIONS`), а не функция
    `prompt_version`. Здесь правка источника проходит тот же путь, что деплой:
    импорт заново, и чтение через публичные `lookup_*` по прежнему входу.

    Без этого теста живёт ровно тот дефект, из-за которого кэш опасен: ключ
    хэширует входы (текст, XML, запрос), поэтому устаревший ответ возможен при
    неизменном ключе и заметен только как «модель стала хуже».
    """

    # Источники перечислены здесь независимо от таблицы сервиса: сверяет её
    # предыдущий класс, а этот проверяет следствие для уже записанных ответов.
    CONTOURS = {
        "generate": (bpmn_generator, bpmn_layout, bpmn_edits, llm_client),
        "improve": (llm_improve, bpmn_edits, bpmn_scoring, llm_client),
    }

    @staticmethod
    def contour(ai_cache_service, store, kind):
        """Записать ответ контура и вернуть его чтение тем же входом."""
        cache = ai_cache_service.AiCache(store)
        if kind == "generate":
            cache.remember_generation("заявка", {"status": "success",
                                                 "bpmn": "<xml/>"})
            return lambda: cache.lookup_generation("заявка")
        cache.remember_improvement(
            "<definitions/>", "улучши",
            {"recommendations": "нужна развилка", "improved_xml": "<definitions/>",
             "report": {"skipped": []}})
        return lambda: cache.lookup_improvement("<definitions/>", "улучши")

    @pytest.mark.parametrize("kind", sorted(CONTOURS))
    def test_each_source_edit_drops_the_answer_written_before_it(self, kind, fake,
                                                                tmp_path, monkeypatch):
        from app.services import ai_cache as ai_cache_service

        store, _client = fake
        for module in self.CONTOURS[kind]:
            with reimported(ai_cache_service) as service:
                read = self.contour(service, store, kind)
                assert read() is not None, (
                    f"контур {kind!r} не отдаёт собственный ответ: проверка ниже "
                    "измеряла бы пустой кэш, а не снятие записи")
                _touch_source(module, tmp_path, monkeypatch)
            # Новый процесс видит уже правленный источник — прежний ответ чужой.
            with reimported(ai_cache_service):
                assert read() is None, (
                    f"правка {module.__name__} не сняла ответы контура {kind!r}: "
                    "переписанный промпт отдаёт ответ, посчитанный по прежнему")

    def test_editing_a_source_the_contour_does_not_read_keeps_its_answer(self, fake,
                                                                        tmp_path,
                                                                        monkeypatch):
        """Удаление чужого источника из версии — не экономия, а подмена: ответ
        пережил бы правку того, что промпт реально печатает. Поэтому проверка
        двусторонняя: генерация не обязана сбрасываться от правил скоринга, но и
        обязана сбрасываться от `bpmn_edits`, где лежат теги интерполируемого
        списка."""
        from app.services import ai_cache as ai_cache_service

        store, _client = fake
        with reimported(ai_cache_service) as service:
            read = self.contour(service, store, "generate")
            _touch_source(bpmn_scoring, tmp_path, monkeypatch)
        with reimported(ai_cache_service):
            assert read() is not None, ("версия генерации двинулась за правилами, "
                                        "которые её промпт не читает")

        _touch_source(bpmn_edits, tmp_path, monkeypatch)
        with reimported(ai_cache_service):
            assert read() is None, ("`EVENT_DEFINITIONS` печатается в системном "
                                    "промпте генерации, а ответ остался прежним")

    def test_score_report_is_dropped_by_the_rule_table(self, fake, tmp_path,
                                                       monkeypatch):
        """Скоринг читает кэш версией правил — значит правка `bpmn_scoring`
        обязана снять отчёт, посчитанный по прежним правилам, а не только
        изменить строку версии."""
        from app.services import ai_cache as ai_cache_service

        cache = ai_cache_service.AiCache(fake[0])
        cache.remember_score("<definitions/>", {"score": 88})
        assert cache.score("<definitions/>")["score"] == 88
        _touch_source(bpmn_scoring, tmp_path, monkeypatch)
        with reimported(ai_cache_service):
            assert cache.score("<definitions/>") is None, (
                "отчёт скоринга, посчитанный прежними правилами, отдаётся по новой "
                "версии")
            cache.remember_score("<definitions/>", {"score": 71})
            assert cache.score("<definitions/>")["score"] == 71



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
        assert cached_keys(client) == set()

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
        assert cached_keys(client) == set()

    def test_internal_error_is_not_cached(self, fake):
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error", "step": "internal",
                                             "error": "Упнутр. ошибка"})
        assert cached_keys(client) == set()

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

    def test_score_is_cached_by_xml_and_rules_version(self, fake, tmp_path,
                                                      monkeypatch):
        """Ключ скоринга держит и входы, и версию правил.

        Промах по другому XML — про разные схемы. Промах по тем же входам после
        правки правил — про устаревший отчёт; он проверяется поведением в
        `TestStaleAnswerIsNotServedAfterASourceEdit.test_score_report_is_dropped_by_the_rule_table`,
        а здесь сверяется, что версия, входящая в ключ и в запись, считается по
        исходнику правил, а не висит константой, которую забывают поднять.
        """
        from app.services import ai_cache as ai_cache_service

        cache, _client = self.cache(fake)
        cache.remember_score("<definitions/>", {"score": 88})
        assert cache.score("<definitions/>")["score"] == 88
        assert cache.score("<definitions/> ") is None, "ключ скоринга склеил схемы с разным XML"

        before = ai_cache_service.SCORE_RULES_VERSION
        _touch_source(bpmn_scoring, tmp_path, monkeypatch)
        assert ai_cache_service.prompt_version("score") != before, (
            "правка таблицы правил не двигает версию, которой скоринг читает кэш")

    def test_improvement_key_includes_the_model_pool(self, fake, monkeypatch):
        """Ответ другой модели на тот же вход — другой ответ. Подмена одной
        модели другой в кэше была бы неотличима от смены промпта."""
        cache, _client = self.cache(fake)
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat"])
        first = cache._key("improve", cache._improvement_inputs("<xml/>", "улучши"))
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat2"])
        second = cache._key("improve", cache._improvement_inputs("<xml/>", "улучши"))
        assert first != second

    def test_generation_key_includes_the_model_pool(self, fake, monkeypatch):
        """Тот же контракт у генерации: `GIGACHAT_MODEL` задаётся в окружении и
        в версию промпта не входит, поэтому без пула в ключе смена модели
        сутки отдавала бы ответ прежней — на том же описании и с тем же
        статусом успеха, то есть как «модель стала хуже»."""
        cache, client = self.cache(fake)
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat"])
        cache.remember_generation("заявка", {"status": "success", "bpmn": "<xml/>"})
        stored = cache._key("generate", cache._generation_inputs("заявка"))
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat2"])
        assert cache.lookup_generation("заявка") is None
        assert stored in client.store, "запись прежней модели цела, а не съедена"

    def test_improvement_pair_still_hits_under_the_same_pool(self, fake, monkeypatch):
        """Пул в ключе не должен разъезжаться между записью и чтением:
        `lookup_improvement` и `remember_improvement` вычисляют его одинаково."""
        cache, _client = self.cache(fake)
        monkeypatch.setattr(llm_client, "get_model_pool", lambda: ["GigaChat",
                                                                  "GigaChat-2"])
        cache.remember_improvement("<definitions/>", "улучши",
                                   {"recommendations": "готово",
                                    "improved_xml": None, "report": {"skipped": []}})
        assert cache.lookup_improvement("<definitions/>", "улучши") is not None

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


class TestRefusalPolicy:
    """Кэш запоминает только те отказы, что воспроизводятся по одному входу.

    Причина не в аккуратности, а в деньгах пользователя: `TaskRefused` из кэша
    (`app/tasks.py`) закрывает вход на `NEGATIVE_CACHE_TTL`, и отказ, рождённый
    конкретной выборкой модели, означал бы «описание нельзя сгенерировать» для
    описания, которое генерируется. Различие закреплено списками в
    `ai_cache`, а не комментарием, и тест сверяет их со словарём, который
    реально отдаёт продюсер.
    """

    def cache(self, fake):
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        return ai_cache_service.AiCache(store), client

    @staticmethod
    def emitted_generation_steps():
        """Словарь шагов отказа из исходника продюсера: `BPMNGenerator.generate`
        кладёт их в `result["step"]` литералами, переменная в трейсе починки
        (`"step": step`) под шаблон не попадает."""
        source = Path(bpmn_generator.__file__).read_text(encoding="utf-8")
        return set(re.findall(r'"step":\s*"([a-z_]+)"', source))

    def test_policy_covers_the_vocabulary_the_producer_emits(self):
        """Новый шаг отказа обязан получить класс в политике, а не молча
        начать кэшироваться (или перестать): здесь список продюсера сверяется с
        тремя множествами сервиса."""
        from app.services import ai_cache as ai_cache_service

        classes = (ai_cache_service._REFUSALS_BY_INPUT
                   | ai_cache_service._REFUSALS_BY_SAMPLE
                   | ai_cache_service._REFUSALS_BY_OUTAGE
                   | ai_cache_service._REFUSALS_BY_LIMIT)
        emitted = self.emitted_generation_steps()
        assert emitted == classes, (
            f"шаги {emitted ^ classes} розойдены с тем, что отдаёт генератор: "
            "кэш повёл бы себя не так, как решено политикой")
        # «value» был в старом списке, но генератор его не отдаёт никогда:
        # проверка держит политику на реальном словаре, а не на угаданном.
        assert "value" not in classes

    @pytest.mark.parametrize("step", ["parse", "generation", "llm", "internal"])
    def test_refusal_decided_by_the_model_or_the_provider_is_not_cached(self, fake,
                                                                       step):
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error", "step": step,
                                            "error": "не получилось"})
        assert cached_keys(client) == set(), (
            f"шаг {step} определён не входом, а кэшируется как отказ по входу")
        assert cache.lookup_generation("заявка") is None

    def test_parse_refusal_leaves_the_next_attempt_untouched(self, fake):
        """JSON модели не разобрался даже после repair-круга — это выборка.
        Обещать «переформулируйте запрос» на работоспособном описании пять
        минут подряд значит отменить тот повтор, который бы прошёл."""
        cache, client = self.cache(fake)
        cache.remember_generation("заявка", {"status": "error", "step": "parse",
                                             "error": "Не удалось построить схему"})
        assert cache.lookup_generation("заявка") is None
        cache.remember_generation("заявка", {"status": "success", "bpmn": "<xml/>"})
        assert cache.lookup_generation("заявка").result["bpmn"] == "<xml/>"

    def test_unclassified_step_is_never_cached(self, fake, caplog):
        """Шаг, которого в политике нет, — это дыра, а не «не кэшируем»: он
        обязан быть виден в логе, иначе следующий шаг генератора молча поменял
        бы поведение кэша."""
        cache, client = self.cache(fake)
        with caplog.at_level("WARNING"):
            cache.remember_generation("заявка", {"status": "error",
                                                 "step": "brand_new_step",
                                                 "error": "?"})
        assert cached_keys(client) == set()
        assert "brand_new_step" in caplog.text

    def test_improvement_refusal_is_cached_only_when_the_input_reproduces_it(self,
                                                                           fake):
        """Отказ улучшения проверяется по входу здесь же, а не по тексту
        сообщения: текст пишет `llm_improve`, и совпадение строк перестало бы
        работать после правки формулировки — в опасную сторону (старый отказ
        продолжал бы отдаваться)."""
        cache, client = self.cache(fake)
        # План не разобран / все операции отклонены: схема разборна, значит
        # отказ породил ответ модели, и повтор по этому входу проходит.
        cache.refuse_improvement("<definitions><task id=\"A\"/></definitions>",
                                 "улучши", "Не удалось получить корректный план")
        assert cache.lookup_improvement(
            "<definitions><task id=\"A\"/></definitions>", "улучши") is None
        assert cached_keys(client) == set()

        # XML, из которого не строится инвентарь, отвергнет и следующий вызов.
        cache.refuse_improvement("это не xml <<", "улучши",
                                 "Не удалось разобрать XML схемы")
        answer = cache.lookup_improvement("это не xml <<", "улучши")
        assert answer is not None and answer.negative


class TestPartialPackageIsNotFrozen:
    """Частично применённый пакет не замораживается на срок ответа улучшения.

    В отчёте такие пакеты выглядят как «модель предложила три операции,
    применилась одна», и повтор того же запроса — единственный способ добить
    остальное: пакет собирается по id из инвентаря, а правка схемы эти id
    меняет. Запись на 7 дней без обхода (`LLM_CACHE_TTL`) означала бы
    «улучшение, которое всегда применяется наполовину» для схемы, с которой
    контур уже справлялся.
    """

    def cache(self, fake):
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        return ai_cache_service.AiCache(store), client

    def answer(self, skipped):
        return {"recommendations": "анализ", "improved_xml": "<definitions/>",
                "report": {"applied": [{"op": "add_task", "id": "new_A1"}],
                           "skipped": skipped}}

    def test_package_with_open_refusals_is_not_written(self, fake):
        cache, client = self.cache(fake)
        cache.remember_improvement(
            "<definitions/>", "улучши",
            self.answer([{"op": "connect", "reason": "источник не найден"}]))
        assert cached_keys(client) == set()
        assert cache.lookup_improvement("<definitions/>", "улучши") is None

    def test_full_package_is_written(self, fake):
        cache, _client = self.cache(fake)
        cache.remember_improvement("<definitions/>", "улучши", self.answer([]))
        assert cache.lookup_improvement("<definitions/>", "улучши") is not None

    def test_package_whose_refusals_the_retry_closed_is_written(self, fake):
        """Корректирующий раунд добил пропуски первого — пакет полный, и он
        стоит оплаченного вызова ровно так же, как пакет без отказов."""
        cache, _client = self.cache(fake)
        cache.remember_improvement(
            "<definitions/>", "улучши",
            self.answer([{"op": "connect", "reason": "нет дуги", "reapplied": True}]))
        assert cache.lookup_improvement("<definitions/>", "улучши") is not None

    def test_the_guard_reads_fields_the_orchestrator_actually_writes(self):
        """Страж частичного пакета не имеет права быть мёртвым кодом.

        Оба поля отчёта пишет `llm_improve`: `skipped` возвращается аплайером и
        дополняется пометкой `reapplied` после корректирующего раунда. Если
        продюсер переименует поле, проверка «применился ли пакет целиком»
        перестанет что-либо видеть — `report.get("skipped") or []` даст пустой
        список, и половинчатый ответ снова заморозится на неделю. Молча."""
        from app.services import ai_cache as ai_cache_service

        policy = inspect.getsource(ai_cache_service.AiCache.remember_improvement)
        producer = Path(llm_improve.__file__).read_text(encoding="utf-8")
        for field in ("skipped", "reapplied"):
            assert f'"{field}"' in policy, (
                f"политика больше не смотрит в report[{field!r}] — проверка "
                "частичного пакета исчезла из кода")
            assert re.search(rf'["\']{field}["\']', producer), (
                f"политика читает {field!r}, а продюсер такое поле не пишет: "
                "страж improvements всегда видит пустой список пропусков")
        # Пометка ставится до того, как отчёт уходит наружу, — иначе политика
        # сравнивала бы поле, которого на этом пути ещё нет.
        assert re.search(r'\.setdefault\(\s*"reapplied"', producer), (
            "`reapplied` не проставляется по умолчанию: пропуск без пометки "
            "читался бы как закрытый")


class TestMalformedCacheUrlFailsOpen:
    """Неразобранный `CACHE_REDIS_URL` — это выключенный кэш, а не упавший
    backend.

    `redis.Redis.from_url` бросает на неразобранный адрес в момент
    конструирования, а `init_ai_cache()` вызывается из lifespan приложения:
    опечатка в переменной, которая касается только кэша, снимала бы весь сервис
    — вопреки правилу модуля о том, что кэш обязан fail open.
    """

    def test_construction_does_not_raise_and_every_operation_is_a_miss(self):
        store = LLMCache("не-redis-адрес", namespace="bpmn")
        key = store.key("llm:generate", "x")
        assert store.get(key, prompt_version="v1") is None
        assert store.set(key, {"a": 1}, prompt_version="v1", ttl=60) is False
        assert store.set_negative(key, "отказ", prompt_version="v1", ttl=60) is False
        assert store.ping() is False
        assert store.snapshot().hits == 0
        store.close()

    def test_startup_and_the_contour_survive_a_bad_url(self, monkeypatch):
        """Публичный контур (то, чем пользуется `app/tasks.py`) остаётся
        работоспособным: кэш не отвечает, модель вызывается как обычно."""
        from app.services import ai_cache as ai_cache_service

        monkeypatch.setattr(ai_cache_service, "CACHE_REDIS_URL", "redis://x:not-a-port")
        monkeypatch.setattr(ai_cache_service, "_cache", None)
        try:
            cache = ai_cache_service.init_ai_cache()
            assert cache is not None, "инициализация не должна падать"
            assert cache.lookup_generation("заявка") is None
            cache.remember_generation("заявка", {"status": "success", "bpmn": "x"})
            assert cache.lookup_generation("заявка") is None
            assert cache.score("<definitions/>") is None
            stats = ai_cache_service.cache_stats()
            assert stats["enabled"] is True
            assert stats["counters"] == "process", "нули памяти процесса нельзя " \
                                                  "выдавать за общие счётчики"
            assert stats["errors"] >= 1
        finally:
            ai_cache_service.close_ai_cache()

    def test_startup_names_the_bad_url_and_promises_no_reconnect(self, monkeypatch,
                                                                 caplog):
        """Лог старта обязан различать «Redis лёг» и «в переменной опечатка».

        Второе не починится само, и фраза «повторим подключение при следующем
        запросе» на него — это дежурный, который ждёт, пока кэш оживёт, вместо
        того чтобы править `CACHE_REDIS_URL`. Конструирование клиента отложено,
        поэтому единственный сигнал о неразобранном адресе — эта строка."""
        from app.services import ai_cache as ai_cache_service

        monkeypatch.setattr(ai_cache_service, "CACHE_REDIS_URL", "redis://x:not-a-port")
        monkeypatch.setattr(ai_cache_service, "_cache", None)
        try:
            with caplog.at_level("ERROR"):
                assert ai_cache_service.init_ai_cache() is not None
            lines = [row for row in caplog.text.splitlines()
                     if "Кэш ответов модели" in row]
            assert any("ВЫКЛЮЧЕН" in row and "not-a-port" in row for row in lines), (
                "старт не сказал внятно, что кэш выключен именно из-за адреса")
            assert not any("повторим подключение" in row for row in lines), (
                "неразобранному адресу обещано переподключение — он не починится сам")
        finally:
            ai_cache_service.close_ai_cache()

    def test_a_dead_server_still_promises_reconnect(self, monkeypatch, caplog):
        """Обратная пара к предыдущему тесту: при разборном адресе и молчащем
        сервере переподключение обещать можно, и оно честно."""
        from app.services import ai_cache as ai_cache_service

        broken = FakeRedis()
        broken.fail = True
        real_init = ai_cache_service.LLMCache.__init__

        def spy(self, url, namespace, **kwargs):
            real_init(self, url, namespace, **kwargs)
            self._client = broken

        monkeypatch.setattr(ai_cache_service.LLMCache, "__init__", spy)
        monkeypatch.setattr(ai_cache_service, "CACHE_REDIS_URL", "redis://cache:6379/1")
        monkeypatch.setattr(ai_cache_service, "_cache", None)
        try:
            with caplog.at_level("WARNING"):
                assert ai_cache_service.init_ai_cache() is not None
            assert ai_cache_service.get_ai_cache().store.client_construction_error is None
            assert "повторим подключение" in caplog.text, (
                "живому серверу не обещано переподключение: лог двух разных сбоев "
                "перестал различаться")
        finally:
            ai_cache_service.close_ai_cache()

    def test_no_connection_is_opened_until_the_cache_is_used(self):
        """Конструктор не должен трогать сеть: `init_ai_cache()` вызывается из
        lifespan, и открытие пула соединений на старте — то, что превращает
        опечатку в падение сервиса."""
        store = LLMCache("redis://localhost:6379/0", namespace="bpmn")
        assert store._client is None
        assert store.client_construction_error is None
        store.close()

    def test_a_permanent_address_failure_does_not_grow_state(self):
        """Отказ «адрес неразобран» повторяется на каждом запросе и никогда не
        проходит сам.

        Второй `raise` сохранённого объекта исключения дописывает ему
        `__traceback__`, а не заменяет: замера ради 300 обращений давали 1200
        кадров, и воркер с опечаткой в переменной рос бы по памяти ровно на
        число запросов. Проверяется здесь, потому что снаружи это не видно:
        кэш честно промахивается, а утечка начинается молча."""

        def frames(error) -> int:
            count, tb = 0, error.__traceback__
            while tb is not None:
                count += 1
                tb = tb.tb_next
            return count

        store = LLMCache("не-redis-адрес", namespace="bpmn")
        for _ in range(300):
            assert store.get("k", prompt_version="v") is None
        error = store.client_construction_error
        assert error is not None, "причина сбоя должна быть видна оператору"
        assert frames(error) < 50, (
            f"сохранённая ошибка наросла до {frames(error)} кадров — её "
            "переизбирают на каждом запросе")
        store.close()


class TestLazyClientIsBuiltOnce:
    """Ленивость не должна превращаться в «лениво на каждый поток».

    Воркер держит пул потоков, и первый запрос будит кэш сразу несколькими
    обращениями: без блокировки на самом конструировании каждый из них создал бы
    свой клиент, а при неразобранном адресе ещё и написал бы свою строку в лог."""

    def test_concurrent_first_use_builds_a_single_client(self, monkeypatch):
        from core import llm_cache as llm_cache_module

        calls = []
        release = threading.Event()
        gate = threading.Barrier(8)
        real_from_url = llm_cache_module.redis.Redis.from_url

        def slow_from_url(url, **options):
            calls.append(url)
            assert release.wait(timeout=5), "клиент строят, не отпуская потока"
            return real_from_url(url, **options)

        monkeypatch.setattr(llm_cache_module.redis.Redis, "from_url",
                            staticmethod(slow_from_url))
        store = LLMCache("redis://localhost:6379/0", namespace="bpmn")

        def worker():
            gate.wait(timeout=5)
            store.ping()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.005)
        release.set()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert len(calls) == 1, "неразобранный адрес породил бы N строк в логе старта"
        store.close()


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

    def test_reported_numbers_say_which_store_they_came_from(self, fake, monkeypatch):
        """`/api/tasks/stats` обязан различать «кэш не попадает» и «общий счётчик
        не прочитан».

        В shipped-топологии попадания считает воркер, а смотрит в них веб: если
        хэш счётчиков не прочитать, наружу идут локальные нули, и без пометки их
        читают как «кэш бесполезен» — его и выключают."""
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        cache = ai_cache_service.AiCache(store)
        cache.remember_generation("заявка", {"status": "success", "bpmn": "x"})
        monkeypatch.setattr(ai_cache_service, "_cache", cache)
        try:
            stats = ai_cache_service.cache_stats()
            assert stats["counters"] == "shared" and stats["writes"] == 1
            client.fail = True
            stats = ai_cache_service.cache_stats()
            assert stats["counters"] == "process"
            assert stats["writes"] == 1, "хотя бы этот процесс должен быть виден"
        finally:
            monkeypatch.setattr(ai_cache_service, "_cache", None)

    def test_stats_answer_with_another_processes_numbers(self, fake, monkeypatch):
        """Числа наружу берутся из Redis, а не из памяти вызывающего процесса.

        Веб-процесс кэша не трогает вовсе: обращений к модели он не делает, всё
        живёт в `worker_llm` и `worker_local`. Если `cache_stats()` вернёт свой
        локальный словарь, оператор увидит нули при разогретом кэше — и по этой
        цифре кэш выключат. Поэтому здесь заполнен только хэш Redis, а счётчик
        этого процесса заведомо пуст."""
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        # Так выглядит след воркера: хэш заполнен до того, как веб что-либо
        # прочитал, и ни одного обращения к модели этот процесс не сделал.
        client.hincrby(store.key("stats"), "hits", 17)
        client.hincrby(store.key("stats"), "misses", 3)
        client.hincrby(store.key("stats"), "writes", 5)
        monkeypatch.setattr(ai_cache_service, "_cache", ai_cache_service.AiCache(store))
        try:
            stats = ai_cache_service.cache_stats()
            assert store.stats.hits == 0, "проверка поехала бы с локального нуля"
            assert stats["counters"] == "shared"
            assert (stats["hits"], stats["misses"], stats["writes"]) == (17, 3, 5)
            assert stats["hit_share"] == pytest.approx(0.85)
        finally:
            monkeypatch.setattr(ai_cache_service, "_cache", None)

    def test_a_stale_local_counter_is_not_reported_as_the_contours_number(self, fake,
                                                                         monkeypatch):
        """Локальный счётчик — fallback, а не второй источник истины.

        Он обязан быть виден как таковой: иначе «4 попадания этого процесса»
        сойдут за «4 попадания контура», и по этому числу будут судить о доле
        промахов всей системы."""
        from app.services import ai_cache as ai_cache_service

        store, client = fake
        cache = ai_cache_service.AiCache(store)
        cache.lookup_generation("заявка")
        client.fail = True
        monkeypatch.setattr(ai_cache_service, "_cache", cache)
        try:
            stats = ai_cache_service.cache_stats()
            assert stats["counters"] == "process"
            assert stats["misses"] == 1
        finally:
            monkeypatch.setattr(ai_cache_service, "_cache", None)

    def test_local_counters_do_not_lose_increments(self, fake):
        """Локальный счётчик — единственное, что видно, когда общий хэш не
        прочитать, поэтому и он обязан быть корректным при параллельных
        запросах: `+=` без блокировки на пуле потоков теряет прибавления, и в
        fallback показано число меньше сделанного."""
        cache, client = fake
        # Общий счётчик недоступен: прибавления идут только в локальный.
        client.fail = True
        failures = []

        def hammer():
            try:
                for _ in range(500):
                    cache.get("ключ", prompt_version="v")
            except Exception as error:  # noqa: BLE001 — сбой потока виден в ассерте
                failures.append(error)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not failures
        assert cache.stats.errors == 8 * 500

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

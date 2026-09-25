"""Ретривер практик: кэш индекса, дедуп эталонов, слияние веток поиска.

Корпус — фикстурные `.bpmn` во временной папке (реальные 367 файлов для
проверки логики не нужны), а модель эмбеддингов подставлена: прогон не должен
ни ходить в сеть за весами, ни читать pickle из файла кэша.
"""
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from core import llm_improve
from core.llm_improve import (
    BPMNKnowledgeBase,
    _format_practices,
    _process_key,
)

BPMN_NS = 'xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"'

TIMER_PRACTICE = "Таймеры для контроля сроков (SLA) и эскалаций"


def diagram(body: str, process_id: str = "Process_1") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<definitions {BPMN_NS} id="Definitions_1">'
            f'<process id="{process_id}" name="Процесс">{body}</process>'
            '</definitions>')


def flow(tail: str = "", task_name: str = "Согласовать договор") -> str:
    return ('<startEvent id="S" name="Начало"/>'
            f'<userTask id="T1" name="{task_name}"/>{tail}'
            '<endEvent id="E" name="Конец"/>')


SLA_BOUNDARY = ('<boundaryEvent id="B1" name="SLA" attachedToRef="T1">'
                '<timerEventDefinition/></boundaryEvent>')
NESTED = ('<subProcess id="SP1" name="Расчёт">'
          '<startEvent id="S2"/><endEvent id="E2"/></subProcess>')


class FakeSentenceModel:
    """Эмбеддинги по маркеру в тексте — детерминированные и без сети."""

    def __init__(self, table: Dict[str, List[float]]):
        self.table = list(table.items())
        self.encode_calls = 0

    def encode(self, texts, show_progress_bar=False):
        self.encode_calls += 1
        return self._rows(texts)

    def _rows(self, texts) -> np.ndarray:
        width = len(self.table[0][1])
        rows = []
        for text in texts:
            lowered = text.lower()
            rows.append(next((vector for marker, vector in self.table
                              if marker in lowered), [0.0] * width))
        return np.asarray(rows, dtype="float32")


class FlakyModel(FakeSentenceModel):
    """Падает ровно на первом запросе после сборки индекса."""

    def encode(self, texts, show_progress_bar=False):
        if self.encode_calls == 1:
            self.encode_calls += 1
            raise RuntimeError("единичный сбой эмбеддингов")
        return super().encode(texts)


class BrokenSentenceModel:
    """Семантика недоступна — как при незагрузившихся весах модели."""

    def encode(self, texts, show_progress_bar=False):
        raise RuntimeError("веса модели не поднялись")


def corpus_hash(number: int) -> str:
    return hashlib.md5(str(number).encode()).hexdigest()


@pytest.fixture
def make_kb(tmp_path, monkeypatch):
    """Корпус и кэш — в tmp_path; кэш назван по корпусу, чтобы тесты не
    подхватывали индекс друг друга."""

    def make(files: Dict[str, str], *, table=None, broken_semantics=False,
             name="corpus") -> BPMNKnowledgeBase:
        assert len(files) >= 2, "на одном документе TF-IDF не строится"
        dataset = tmp_path / name
        dataset.mkdir(parents=True, exist_ok=True)
        for filename, xml in files.items():
            (dataset / f"{filename}.bpmn").write_text(xml, encoding="utf-8")
        monkeypatch.setattr(llm_improve, "_cache_paths", lambda: (
            tmp_path / f"{name}.npz", tmp_path / f"{name}.json"))
        kb = BPMNKnowledgeBase(dataset_path=str(dataset))
        if broken_semantics:
            kb._sentence_model = lambda: BrokenSentenceModel()
        elif table is not None:
            model = FakeSentenceModel(table)
            kb._sentence_model = lambda: model
            kb.fake_model = model
        return kb

    return make


def manifest(tmp_path, name="corpus") -> Dict:
    return json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))


BASE_CORPUS = {
    "Эталон_согласования": diagram(flow(SLA_BOUNDARY), "P1"),
    "Эталон_раскладки": diagram(flow(NESTED, "Отгрузить товар"), "P2"),
}


class TestLogs:
    """`"% "` в спецификаторе logging глотает ValueError внутри себя: строка
    не попадает в лог вообще, глазами это не проверить."""

    def test_corpus_progress_lines_reach_the_log(self, make_kb, caplog):
        files = dict(BASE_CORPUS, **{
            "Эталон_платежей": diagram(flow("", "Провести платёж"), "P3")})
        with caplog.at_level(logging.INFO, logger="core.llm_improve"):
            make_kb(files, table={"эталон": [1.0, 0.0]}).find_best_practices("эталон")
        messages = [record.getMessage() for record in caplog.records]
        assert "Найдено 3 файлов .bpmn в корпусе" in messages
        assert "Загружено 3 эталонных схем" in messages
        assert "Индексировано 3 эталонов (семантика + TF-IDF)" in messages
        assert "Найдено 3 подходящих эталонов" in messages

    def test_cache_load_line_reaches_the_log(self, make_kb, caplog):
        make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                name="cacheline").find_best_practices("эталон")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="core.llm_improve"):
            make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                    name="cacheline").find_best_practices("эталон")
        messages = [record.getMessage() for record in caplog.records]
        assert "Кэш RAG загружен: 2 эталонов" in messages
        assert not [m for m in messages if "Logging error" in m]


class TestCache:
    def test_round_trip_keeps_ranking_and_dimension(self, make_kb, tmp_path, caplog):
        files = {
            "Dispatch_of_goods_" + corpus_hash(1): diagram(flow(SLA_BOUNDARY), "P1"),
            "Credit_Scoring_" + corpus_hash(2): diagram(flow(NESTED), "P2"),
            "Banking_" + corpus_hash(3): diagram(flow("", "Провести платёж"), "P3"),
        }
        table = {"dispatch": [1.0, 0.0, 0.0], "credit": [0.0, 1.0, 0.0],
                 "banking": [0.0, 0.0, 1.0], "платёж": [1.0, 0.5, 0.0]}
        user_schema = diagram(flow("", "Оплатить счёт"), "PU")
        queries = ["договор", "dispatch of goods", "платёж"]

        built = make_kb(files, table=table, name="roundtrip")
        from_build = [built.find_best_practices(q, user_schema) for q in queries]
        assert (tmp_path / "roundtrip.npz").exists()
        assert (tmp_path / "roundtrip.json").exists()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="core.llm_improve"):
            restored = make_kb(files, table=table, name="roundtrip")
            from_cache = [restored.find_best_practices(q, user_schema) for q in queries]

        messages = [record.getMessage() for record in caplog.records]
        assert "Кэш RAG загружен: 3 эталонов" in messages
        assert not [m for m in messages if "Индексировано" in m], "индекс собран заново"
        for cached, built_results in zip(from_cache, from_build):
            assert [s["name"] for s in cached] == [s["name"] for s in built_results]
            assert [round(s["similarity"], 6) for s in cached] == \
                   [round(s["similarity"], 6) for s in built_results]
        assert restored._embeddings.shape == built._embeddings.shape == (3, 3)

    def test_manifest_has_no_reference_xml(self, make_kb, tmp_path):
        make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                name="manifest").find_best_practices("эталон")
        data = manifest(tmp_path, "manifest")
        assert data["method"] == llm_improve.CACHE_METHOD
        entry = next(m for m in data["metadata"] if m["name"] == "Эталон_согласования")
        assert "xml" not in entry
        assert set(entry) == {"name", "domain", "complexity", "practices",
                              "xml_features", "element_names"}
        assert "<definitions" not in json.dumps(data, ensure_ascii=False)
        assert [pair[0] for pair in entry["practices"]] == ["boundary", "timer"]

    @pytest.mark.parametrize("junk", [
        b"this is not an npz file at all",
        b"\x80\x04\x95\x00\x00\x00\x00\x00\x00\x8c\x04json\x94.",  # legacy pickle
    ])
    def test_foreign_or_broken_cache_rebuilds_instead_of_failing(
            self, make_kb, tmp_path, junk):
        make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                name="junk").find_best_practices("эталон")
        (tmp_path / "junk.npz").write_bytes(junk)
        kb = make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]}, name="junk")
        names = {s["name"] for s in kb.find_best_practices("эталон")}
        assert names == set(BASE_CORPUS)

    def test_stale_method_manifest_forces_rebuild(self, make_kb, tmp_path, caplog):
        make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                name="stale").find_best_practices("эталон")
        data = manifest(tmp_path, "stale")
        data["method"] = "hybrid-v2"
        (tmp_path / "stale.json").write_text(json.dumps(data), encoding="utf-8")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="core.llm_improve"):
            make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]},
                    name="stale").find_best_practices("эталон")
        messages = [record.getMessage() for record in caplog.records]
        assert "Кэш RAG устарел (метод hybrid-v2), пересобираем" in messages
        assert manifest(tmp_path, "stale")["method"] == llm_improve.CACHE_METHOD

    def test_cache_path_defaults_next_to_the_code(self, monkeypatch):
        monkeypatch.delenv("BPMN_RAG_CACHE_FILE", raising=False)
        npz, meta = llm_improve._cache_paths()
        assert npz == Path(llm_improve.__file__).with_name("bpmn_rag_cache.npz")
        assert meta.parent == npz.parent

    def test_cache_path_comes_from_environment(self, monkeypatch, tmp_path):
        target = tmp_path / "custom" / "rag_index.npz"
        monkeypatch.setenv("BPMN_RAG_CACHE_FILE", str(target))
        # Манифест — sidecar рядом с файлом данных.
        assert llm_improve._cache_paths() == (
            target, tmp_path / "custom" / "rag_index.npz.json")


class TestRetrieval:
    def test_one_reference_per_process(self, make_kb):
        files = {"Dispatch_of_goods_" + corpus_hash(i): diagram(flow(), f"P{i}")
                 for i in range(4)}
        files["Credit_Scoring_" + corpus_hash(9)] = diagram(flow(NESTED), "C1")
        files["Banking_" + corpus_hash(8)] = diagram(flow(SLA_BOUNDARY), "B1")
        kb = make_kb(files, table={"dispatch": [1.0, 0.0]}, name="dedup")
        results = kb.find_best_practices("договор", diagram(flow(), "PU"))
        processes = [_process_key(s["name"]) for s in results]
        assert len(processes) == len(set(processes)) == 3
        assert processes.count("dispatch_of_goods") == 1

    def test_degraded_semantics_does_not_shift_the_floor(self, make_kb):
        table = {"согласования": [1.0, 0.0], "раскладки": [0.0, 1.0]}
        query = "согласования"
        user_schema = diagram(flow(SLA_BOUNDARY), "PU")
        with_semantics = make_kb(BASE_CORPUS, table=table, name="floor") \
            .find_best_practices(query, user_schema)
        broken = make_kb(BASE_CORPUS, broken_semantics=True, name="floor") \
            .find_best_practices(query, user_schema)
        assert with_semantics and broken
        # Вершина слиянного списка — 1.0 и в двухветочном, и в одноветочном
        # режиме: порог описывает место в рейтинге, а не сумму разномасштабных
        # косинусов, поэтому деградация ветки не меняет его смысла.
        assert with_semantics[0]["similarity"] == pytest.approx(1.0)
        assert broken[0]["similarity"] == pytest.approx(1.0)

    def test_single_encode_failure_is_not_permanent(self, make_kb):
        kb = make_kb(BASE_CORPUS, table={"согласования": [1.0, 0.0],
                                         "раскладки": [0.0, 1.0]}, name="flaky")
        model = FlakyModel({"согласования": [1.0, 0.0], "раскладки": [0.0, 1.0]})
        kb._sentence_model = lambda: model

        degraded = kb.find_best_practices("согласования", diagram(flow(), "PU"))
        recovered = kb.find_best_practices("согласования", diagram(flow(), "PU"))
        assert degraded and recovered, "сбой эмбеддингов уронил поиск"
        # сборка + упавший запрос + живой запрос: семантика не выключена навсегда
        assert model.encode_calls == 3

    def test_query_branch_texts_differ_by_construction(self, make_kb):
        kb = make_kb(BASE_CORPUS, table={"эталон": [1.0, 0.0]}, name="texts")
        semantic, lexical = kb._query_texts("найди узкие места",
                                            diagram(flow(SLA_BOUNDARY), "PU"))
        assert "tasks_" not in semantic and "complexity" not in semantic
        assert "найди узкие места" in semantic and "Согласовать договор" in semantic
        assert "tasks_1" in lexical

    def test_ranks_are_fused_not_scales_summed(self):
        # Согласованность двух веток важнее пика в одной из них: документ X
        # лучший семантически и худший лексически, Y — вверху обеих.
        semantic = np.array([0.99, 0.98, 0.97, 0.96, 0.50, 0.40, 0.30, 0.20])
        lexical = np.array([0.05, 0.99, 0.98, 0.97, 0.50, 0.40, 0.30, 0.20])
        fused = BPMNKnowledgeBase._fuse_ranks([semantic, lexical])
        assert np.argmax(fused) == 1
        assert fused[0] < fused[1]
        assert fused.max() <= 1.0
        # Одна доступная ветка даёт ту же шкалу: верх списка = 1.0.
        single = BPMNKnowledgeBase._fuse_ranks([lexical])
        assert single.max() == pytest.approx(1.0)


class TestPracticesBlock:
    def test_missing_practice_shown_with_reference_name(self, make_kb):
        reference = "Dispatch_of_goods_" + corpus_hash(5)
        files = {reference: diagram(flow(SLA_BOUNDARY), "P1"),
                 "Эталон_раскладки": diagram(flow(NESTED), "P2")}
        kb = make_kb(files, table={"dispatch": [1.0, 0.0], "раскладки": [0.0, 1.0]},
                     name="diff")
        # Схема пользователя без таймера — эталон с таймером обязан показать
        # ровно эту недостающую практику.
        results = kb.find_best_practices("узкие места по срокам",
                                         diagram(flow("", "Проверить оплату"), "PU"))
        item = next(s for s in results if s["name"] == reference)
        assert TIMER_PRACTICE in item["missing_practices"]
        block = _format_practices([item])
        assert TIMER_PRACTICE in block
        assert reference in block
        # Тег домена из блока выбросили: `[hr]` читается моделью как готовое имя
        # участника, а оракул `employee_onboarding` требует пул «HR» — тег
        # выдавал ответ прямо в промпт (это и мерил eval/provenance.py).
        # Поэтому закрепляем не «именно этого домена нет», а «никаких `[...]` в
        # блоке нет»: иначе следующий домен из `_DOMAIN_KEYWORDS` протёк бы
        # незаметно — тест зелёный, а подсказка ответа в промпте.
        assert not re.search(r"\[[^\]]*\]", block), block

    def test_identical_schema_leaves_the_block_empty(self, make_kb):
        kb = make_kb(BASE_CORPUS, table={"согласования": [1.0, 0.0],
                                         "раскладки": [0.0, 1.0]}, name="same")
        results = kb.find_best_practices("что улучшить",
                                         diagram(flow(SLA_BOUNDARY), "PU"))
        item = next(s for s in results if s["name"] == "Эталон_согласования")
        assert item["missing_practices"] == []
        assert _format_practices([item]) == ""

    def test_block_capped_by_practices_limit(self, make_kb):
        kb = make_kb({
            "Эталон_согласования": diagram(flow(SLA_BOUNDARY), "P1"),
            "Эталон_раскладки": diagram(flow(SLA_BOUNDARY + NESTED), "P2"),
            "Эталон_платежей": diagram(flow(NESTED, "Провести платёж"), "P3"),
        }, table={"эталон": [1.0, 0.0]}, name="cap")
        block = _format_practices(
            kb.find_best_practices("эталон", diagram(flow("", "Оплатить счёт"), "PU")))
        lines = [line for line in block.splitlines() if line.startswith("- ")]
        assert 1 <= len(lines) <= llm_improve.PRACTICES_LIMIT
        assert block.startswith("ЛУЧШИЕ ПРАКТИКИ")


class TestStructuralSignature:
    """Сигнатура формы: количества остались точными, признаки практик добавились.

    Исходная гипотеза была такой: `tasks_11` и `tasks_12` — разные слова словаря
    TF-IDF, редкое значение получает высокий IDF, и косинус начинает награждать
    совпадение счётчиков. На кредитной заявке top-3 действительно состоял из
    шаблонов упражнений (`New_Process`, `Ex_3`, `exercise3`) — та же форма,
    пустое содержимое. Полосы вместо чисел эффект убирали и роняли сводку, поэтому
    гипотеза закрыта замером, а не мнением: числа остаются, к ним добавлены ключи
    практик — по ним подбор «процесс с такой же конструкцией» сходится с
    `missing_practices`, которые читает модель.
    """

    def test_counts_stay_exact(self, make_kb):
        """Числа точные, и это не недосмотр: полосы `few_tasks`/`many_tasks`
        вместо них просадили единственный работавший кейс (`warehouse_delivery`
        P@3 1.00 → 0.00, сводка 0.50 → 0.33). Размер процесса — тот признак,
        который в датасете на 42 заготовки упражнений отличает содержательную
        схему от пустой. Отдельное утверждение про числа держит эту память:
        «улучшить» их полосами снова придётся через снятие этого теста."""
        kb = make_kb(BASE_CORPUS)
        sig = kb._extract_xml_features(kb._parse(diagram(flow())))
        assert "tasks_1" in sig and "events_2" in sig

    def test_signature_names_the_practices_the_contour_reasons_about(self, make_kb):
        """Признаки формы совпадают с ключами практик: подбор «на такую же
        конструкцию» и `missing_practices`, которые читает модель, перестают
        быть двумя разными описаниями одной схемы."""
        kb = make_kb(BASE_CORPUS)
        with_timer = kb._extract_xml_features(
            kb._parse(diagram(flow(SLA_BOUNDARY))))
        nested = kb._extract_xml_features(kb._parse(diagram(flow(NESTED))))
        assert "boundary" in with_timer and "timer" in with_timer
        assert "subprocess" in nested and "timer" not in nested


INCIDENT_XML = diagram(
    '<startEvent id="S" name="Инцидент зарегистрирован"/>'
    '<userTask id="T1" name="Разобрать инцидент на сервере мониторинга"/>'
    '<endEvent id="E" name="Инцидент закрыт"/>', "P9")


class TestDomainIsCorpusAware:
    """Классификатор не вправе утверждать тему, под которую в датасете нет ни
    одного эталона.

    `_DOMAIN_KEYWORDS` знает `it` и `manufacturing`, а в корпусе под них ноль
    файлов (замер: general 167, finance 70, logistics 61, hr 48, approval 16,
    customer_service 5). Строка классификатора попадает в тексты запроса обеих
    веток, и ранжирование уходило словом, которого не несёт ни один документ.
    """

    def test_a_topic_the_corpus_cannot_serve_degrades_to_general(self, make_kb):
        kb = make_kb(BASE_CORPUS, broken_semantics=True)
        kb.ensure_ready()
        assert kb._detect_domain(INCIDENT_XML) == "it"
        assert "it" not in kb._corpus_domains()
        assert kb._detect_domain(INCIDENT_XML, kb._corpus_domains()) == "general"

    def test_the_query_text_carries_the_degraded_topic(self, make_kb):
        """Сам по себе `_detect_domain` нужен и корпусу, где пустой раздел —
        норма; в текст запроса обязан уходить ограниченный ответ."""
        kb = make_kb(BASE_CORPUS, broken_semantics=True)
        kb.ensure_ready()
        _, lexical = kb._query_texts("найди узкие места", INCIDENT_XML)
        assert " it" not in f" {lexical}" and "general" in lexical

    def test_a_topic_the_corpus_actually_has_is_kept(self, make_kb):
        """Ограничение — не глушение: под тему, для которой в корпусе есть
        эталон, классификатор отвечает как отвечал."""
        files = dict(BASE_CORPUS)
        files["Эталон_инцидента"] = INCIDENT_XML
        kb = make_kb(files, broken_semantics=True)
        kb.ensure_ready()
        assert kb._detect_domain(INCIDENT_XML, kb._corpus_domains()) == "it"


def test_identical_gap_from_several_references_becomes_one_line():
    """Одна и та же недостающая практика в трёх эталонах — одна строка
    промпта: три дубля читаются как навязчивая идея, а не как подтверждение."""
    schemas = [
        {"name": "Эталон А", "domain": "approval", "missing_practices": ["Таймеры SLA"]},
        {"name": "Эталон Б", "domain": "approval", "missing_practices": ["Таймеры SLA"]},
        {"name": "Эталон В", "domain": "approval",
         "missing_practices": ["Таймеры SLA", "Подпроцессы"]},
    ]
    block = llm_improve._format_practices(schemas)
    assert block.count("Таймеры SLA") == 1
    assert "Эталон А, Эталон Б" in block
    assert "Подпроцессы" in block
    # Домен в схемы передан (`approval`) и всё равно не печатается: дедупликация
    # — не место, куда тег может вернуться под другим именем теста.
    assert "[approval]" not in block

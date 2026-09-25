# Конвейер улучшения и анализа BPMN-схем.
#
# Архитектура: компактный инвентарь схемы → поиск практик по корпусу
# эталонов (ранговое слияние семантики и TF-IDF) → один планирующий вызов LLM,
# возвращающий анализ и операции редактирования → детерминированный аплайер
# (core.bpmn_edits) → семантическая починка. Полный перегенерат XML моделью
# не используется: операции применяются кодом, координаты не нужны — раскладку
# делает фронтенд (bpmn-auto-layout).
import asyncio
import glob
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import chardet
import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import llm_client
from .llm_client import LLMError, LLMRequestTooLargeError, LLMTruncatedError
from . import bpmn_edits
from .bpmn_scoring import (BPMNScorer, BUSINESS_RULES, FAILED, PASSED,
                           diff_scores, has_unguarded_cycle)

logger = logging.getLogger(__name__)

# Скоринг без состояния: правила пересчитываются на каждую схему, экземпляр
# держит только словарь правил.
_scorer = BPMNScorer()

DATASET_PATH = os.path.join(os.path.dirname(__file__), "bpmn_dataset")
# Кэш индекса — рядом с кодом, а не относительно CWD: путь «уплывал» между
# контейнером и терминалом, а два воркера дрались за одну запись. Формат —
# npz + JSON-манифест вместо pickle: pickle.load чужого файла исполняет код.
CACHE_NAME = "bpmn_rag_cache.npz"
CACHE_METHOD = "hybrid-v5"

# Сколько разных эталонных процессов и сколько строк практик уходит в промпт.
TOP_SCHEMAS = 3
PRACTICES_LIMIT = 6
PRACTICES_PER_SCHEMA = 2
MAX_OPERATIONS = 25
# Единственная температура планировщика: повтор по пропущенным операциям
# обязан видеть ту же картину, что и первый план.
PLANNING_TEMPERATURE = 0.2
# Косинусы семантики и TF-IDF несопоставимы по шкале, поэтому сливаются не
# значения, а места в двух списках рангов (Reciprocal Rank Fusion).
RRF_K = 20
# Порог в долях от «первое место во всех ветках» (0..1). Норма на число
# доступных веток держит его одинаковым и при живых эмбеддингах, и при TF-IDF.
RRF_FLOOR = 0.4

_TFIDF_PARAMS: Dict[str, Any] = {
    "max_features": 5000,
    "ngram_range": (1, 2),
    "max_df": 0.95,
    "min_df": 1,
}

# Хвост из 32 hex — это хеш файла в имени, а не часть процесса.
_HASH_SUFFIX = re.compile(r"_[0-9a-f]{24,64}$")


def _cache_paths() -> Tuple[Path, Path]:
    """(данные индекса, манифест): путь из BPMN_RAG_CACHE_FILE или рядом с кодом."""
    raw = os.getenv("BPMN_RAG_CACHE_FILE", "").strip()
    base = Path(raw) if raw else Path(__file__).with_name(CACHE_NAME)
    if base.suffix.lower() != ".npz":
        base = base.with_name(base.name + ".npz")
    return base, base.with_name(base.name + ".json")


def _process_key(name: str) -> str:
    """Норма имени эталона: Dispatch_of_goods_<4 разных хеша> — один процесс."""
    key = re.sub(r"\.bpmn$", "", name or "", flags=re.IGNORECASE)
    key = _HASH_SUFFIX.sub("", key.lower())
    return re.sub(r"[^0-9a-zа-яё]+", "_", key).strip("_")


def _vectorizer_manifest(vectorizer: TfidfVectorizer) -> Dict[str, Any]:
    params = dict(_TFIDF_PARAMS)
    params["ngram_range"] = list(vectorizer.ngram_range)
    return {
        "params": params,
        "vocabulary": {str(term): int(i)
                       for term, i in vectorizer.vocabulary_.items()},
    }


def _vectorizer_from_manifest(data: Dict[str, Any], idf: np.ndarray) -> TfidfVectorizer:
    """Восстанавливает векторизатор на frozen-словаре: пересчитывать TF-IDF по
    корпусу ради загрузки кэша незачем."""
    params = dict(_TFIDF_PARAMS)
    params.update({k: v for k, v in (data.get("params") or {}).items()
                   if k in _TFIDF_PARAMS})
    params["ngram_range"] = tuple(params["ngram_range"])
    vocabulary = {str(term): int(i)
                  for term, i in (data.get("vocabulary") or {}).items()}
    if not vocabulary:
        raise ValueError("в кэше нет словаря TF-IDF")
    vectorizer = TfidfVectorizer(vocabulary=vocabulary, **params)
    # Сеттер idf_ сверяет длину со словарём: несовпадение (чужой или битый
    # файл) превращается в ValueError, то есть в пересборку индекса.
    vectorizer.idf_ = np.asarray(idf, dtype="float64")
    return vectorizer


def _missing_practices(reference: List[Any],
                       user_keys: Optional[Set[str]]) -> List[str]:
    """Формулировки практик эталона, которых нет в схеме пользователя.

    Сравнение по признаку (timer, subprocess, ...), а не по тексту; порядок
    эталона сохраняется. `user_keys=None` — схему пользователя разобрать не
    удалось, поэтому считать практики её имеющимися нельзя.
    """
    missing = []
    for entry in reference:
        key, text = entry[0], entry[1]
        if user_keys is None or key not in user_keys:
            missing.append(text)
    return missing


class ImprovementError(RuntimeError):
    """Сбой улучшения с понятным для пользователя сообщением."""


class ImprovementUnavailable(ImprovementError):
    """Провайдер модели недоступен: запрос корректен, повторить стоит позже.

    Отличается от ImprovementError тем, что наружу должен уйти 503, а не 400 —
    иначе клиент решит, что ему ответили «неправильный запрос», и заставит
    пользователя переформулировать то, с чем всё в порядке.
    """


# ---------------------------------------------------------------------------
# База знаний по корпусу эталонных схем
# ---------------------------------------------------------------------------

class BPMNKnowledgeBase:
    """Похожие эталонные схемы и их практики. Тяжёлые модели грузятся лениво."""

    def __init__(self, dataset_path: str = DATASET_PATH):
        self.dataset_path = dataset_path
        self._ready = False
        self._lock = threading.Lock()
        self._metadata: List[Dict[str, Any]] = []
        self._embeddings: Optional[np.ndarray] = None
        self._vectors = None
        self._vectorizer = TfidfVectorizer(**_TFIDF_PARAMS)

    # --- публичное ---

    def find_best_practices(self, query: str, xml_content: Optional[str] = None,
                            top_k: int = TOP_SCHEMAS) -> List[Dict[str, Any]]:
        """Гибридный поиск эталонов и разность практик с текущей схемой.

        В ответе не больше одного эталона на процесс; `similarity` —
        нормированный RRF-счёт (1.0 = первое место во всех доступных ветках),
        а `missing_practices` — то, что есть в эталоне и чего нет в схеме
        пользователя.
        """
        self.ensure_ready()
        if not self._metadata:
            return []

        user_root = self._parse(xml_content) if xml_content else None
        user_keys = ({key for key, _ in self._practice_signals(user_root)}
                     if user_root is not None else None)
        semantic_text, lexical_text = self._query_texts(query, xml_content)

        branches: List[np.ndarray] = []
        if (self._embeddings is not None
                and self._embeddings.shape[0] == len(self._metadata)):
            try:
                model = self._sentence_model()
                vector = np.asarray(model.encode([semantic_text]), dtype="float32")
                norm = float(np.linalg.norm(vector))
                if norm > 0:
                    vector = vector / norm
                branches.append(np.clip(
                    (self._embeddings @ vector.T).flatten(), 0.0, 1.0))
            except Exception as e:  # noqa: BLE001 — деградация на этот вызов
                logger.warning("Семантический поиск недоступен: %s", e)
        if self._vectors is not None and self._vectors.shape[0] == len(self._metadata):
            try:
                branches.append(np.clip(
                    cosine_similarity(self._vectorizer.transform([lexical_text]),
                                      self._vectors)[0], 0.0, 1.0))
            except Exception as e:  # noqa: BLE001 — без одной ветки не падаем
                logger.warning("Лексический поиск недоступен: %s", e)
        if not branches:
            logger.warning("Ни одна ветка поиска не отработала: эталоны не подобраны")
            return []

        fused = self._fuse_ranks(branches)
        results: List[Dict[str, Any]] = []
        seen_processes: Set[str] = set()
        # стабильная сортировка по убыванию: при равных местах порядок берётся
        # из порядка корпуса, а не из обхода файловой системы.
        for idx in np.argsort(-fused, kind="stable"):
            if len(results) >= top_k:
                break
            score = float(fused[idx])
            if score < RRF_FLOOR:
                break
            schema = self._metadata[int(idx)]
            process = _process_key(str(schema.get("name", "")))
            if process in seen_processes:
                continue
            seen_processes.add(process)
            item = dict(schema)
            item["similarity"] = score
            item["missing_practices"] = _missing_practices(
                list(schema.get("practices") or []), user_keys)
            results.append(item)
        logger.info("Найдено %s подходящих эталонов", len(results))
        return results

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            if not self._load_cache():
                self._build()
                self._save_cache()
            self._ready = True

    # --- кэш индекса ---

    def _load_cache(self) -> bool:
        npz_path, manifest_path = _cache_paths()
        if not (npz_path.exists() and manifest_path.exists()):
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("method") != CACHE_METHOD:
                logger.info("Кэш RAG устарел (метод %s), пересобираем",
                            manifest.get("method"))
                return False
            with np.load(npz_path) as arrays:
                embeddings = (arrays["embeddings"]
                              if "embeddings" in arrays.files else None)
                shape = tuple(int(x) for x in arrays["tfidf_shape"])
                vectors = csr_matrix((arrays["tfidf_data"], arrays["tfidf_indices"],
                                      arrays["tfidf_indptr"]), shape=shape)
                idf = arrays["idf"]
            metadata = manifest["metadata"]
            vectorizer = _vectorizer_from_manifest(manifest["vectorizer"], idf)
            if not metadata or vectors.shape[0] != len(metadata):
                logger.warning("Кэш RAG не согласован с манифестом: пересборка")
                return False
            if embeddings is not None and embeddings.shape[0] != len(metadata):
                logger.warning("Эмбеддинги не совпадают с корпусом: пересборка")
                return False
            self._metadata = metadata
            self._vectors = vectors
            self._embeddings = embeddings
            self._vectorizer = vectorizer
            logger.info("Кэш RAG загружен: %s эталонов", len(self._metadata))
            return True
        except Exception as e:  # noqa: BLE001 — битый/чужой кэш пересобирается
            logger.warning("Не удалось прочитать кэш RAG (%s): пересборка", e)
            return False

    def _save_cache(self) -> None:
        if not self._metadata or self._vectors is None:
            return  # пустой или несобранный индекс в кэш не пишем
        npz_path, manifest_path = _cache_paths()
        try:
            npz_path.parent.mkdir(parents=True, exist_ok=True)
            arrays: Dict[str, Any] = {}
            if self._embeddings is not None:
                arrays["embeddings"] = self._embeddings
            if self._vectors is not None:
                csr = (self._vectors.tocsr()
                       if hasattr(self._vectors, "tocsr")
                       else csr_matrix(self._vectors))
                arrays.update({
                    "tfidf_data": csr.data,
                    "tfidf_indices": csr.indices,
                    "tfidf_indptr": csr.indptr,
                    "tfidf_shape": np.asarray(csr.shape),
                    "idf": np.asarray(self._vectorizer.idf_),
                })
            # Запись во временный файл и os.replace: читатель либо видит
            # цельный кэш, либо его отсутствие, но не половину записи.
            data_tmp = npz_path.with_name(npz_path.name + ".tmp")
            with open(data_tmp, "wb") as f:
                np.savez(f, **arrays)
            os.replace(data_tmp, npz_path)
            # Полного XML в кэше нет: в промпт уходят name/domain/practices,
            # а признаки XML считаются один раз на сборке.
            manifest = {
                "method": CACHE_METHOD,
                "metadata": self._metadata,
                "vectorizer": _vectorizer_manifest(self._vectorizer),
            }
            manifest_tmp = manifest_path.with_name(manifest_path.name + ".tmp")
            manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False),
                                    encoding="utf-8")
            os.replace(manifest_tmp, manifest_path)
            logger.info("Кэш RAG сохранён: %s эталонов", len(self._metadata))
        except Exception as e:  # noqa: BLE001 — кэш производный, не критично
            logger.warning("Не удалось сохранить кэш RAG: %s", e)

    # --- сборка индекса ---

    def _build(self) -> None:
        schemas = self._load_real_schemas()
        if not schemas:
            logger.warning("Корпус эталонов пуст: RAG работать не будет")
            return
        # Порядок фиксирован: без него одинаковые по признакам копии эталонов
        # ранжировались бы по порядку обхода файловой системы.
        schemas.sort(key=lambda schema: schema["name"])
        semantic_texts, lexical_texts = zip(*[self._document_texts(s) for s in schemas])
        self._metadata = schemas
        self._vectors = self._vectorizer.fit_transform(list(lexical_texts))
        try:
            model = self._sentence_model()
            embeddings = model.encode(list(semantic_texts), show_progress_bar=False)
            embeddings = np.asarray(embeddings, dtype="float32")
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._embeddings = embeddings / norms
            logger.info("Индексировано %s эталонов (семантика + TF-IDF)", len(schemas))
        except Exception as e:  # noqa: BLE001
            self._embeddings = None
            logger.warning("Sentence-transformers недоступен (%s): только TF-IDF", e)

    _sentence_model_cache = None

    def _sentence_model(self):
        if BPMNKnowledgeBase._sentence_model_cache is None:
            from sentence_transformers import SentenceTransformer
            BPMNKnowledgeBase._sentence_model_cache = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2"
            )
        return BPMNKnowledgeBase._sentence_model_cache

    # --- загрузка и признаковое описание эталонов ---

    def _load_real_schemas(self) -> List[Dict[str, Any]]:
        schemas = []
        bpmn_files = glob.glob(os.path.join(self.dataset_path, "*.bpmn"))
        logger.info("Найдено %s файлов .bpmn в корпусе", len(bpmn_files))
        for file_path in bpmn_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    xml_content = f.read()
            except UnicodeDecodeError:
                try:
                    with open(file_path, "rb") as f:
                        raw = f.read()
                    encoding = chardet.detect(raw).get("encoding") or "utf-8"
                    xml_content = raw.decode(encoding, errors="replace")
                except Exception as e:  # noqa: BLE001
                    logger.warning("Пропуск файла %s: %s", file_path, e)
                    continue
            except OSError as e:
                logger.warning("Пропуск файла %s: %s", file_path, e)
                continue
            root = self._parse(xml_content)
            if root is None:
                logger.warning("Некорректный XML в %s: файл пропущен", file_path)
                continue
            name = os.path.splitext(os.path.basename(file_path))[0]
            schemas.append({
                "name": name,
                "domain": self._detect_domain(xml_content),
                "complexity": self._detect_complexity(root),
                "practices": self._practice_pairs(root),
                "xml_features": self._extract_xml_features(root),
                "element_names": " ".join(self._element_names(root)),
            })
        logger.info("Загружено %s эталонных схем", len(schemas))
        return schemas

    # Отрасль, а не действие: «проверка», «подпись», «качество» и «review» стоят
    # в процессе любой темы, и на них классификатор превращал 227 эталонов из 367
    # в `approval`, а складскую схему — в согласование (замерено: «проверк»×5 +
    # «подпис»×3 били «склад»×3 + «логистик»×2). Словарь держит существительные
    # темы и работает на двух языках: корпус англоязычный, запросы русские.
    _DOMAIN_KEYWORDS = {
        "finance": ["банк", "платеж", "кредит", "счет", "транзакц", "инвойс",
                    "бюджет", "тариф", "finance", "bank", "payment", "credit",
                    "invoice", "billing"],
        "approval": ["согласован", "одобрен", "утвержден", "виза", "резолюц",
                     "эскалац", "approval", "approve", "sign-off", "escalation"],
        "manufacturing": ["производ", "завод", "цех", "сырье", "конвейер",
                          "manufactur", "assembly", "fabrication"],
        "customer_service": ["поддержк", "жалоб", "обращен", "тикет", "претензи",
                             "claim", "support", "complaint", "ticket"],
        "logistics": ["доставк", "отправк", "склад", "логистик", "груз",
                      "транспорт", "перевозк", "палет", "курьер", "экспедит",
                      "delivery", "shipment", "warehouse", "logistics",
                      "dispatch", "freight", "picking"],
        "hr": ["персонал", "найм", "сотрудник", "онбординг", "кадр", "ваканси",
               "recruit", "onboarding", "employee", "payroll", "hiring"],
        "it": ["инцидент", "алерт", "мониторинг", "сервер", "деплой", "релиз",
               "integration", "incident", "alert", "monitoring", "deploy"],
    }

    # Домен считается темой только при перевесе: 8 против 5 — это два
    # конкурирующих признака, а не тему определивший процесс. Без перевеса схема
    # уходит в `general`, и поиск честно перестаёт делать вид, что различил
    # отрасли.
    DOMAIN_MARGIN = 1.5

    def _detect_domain(self, xml: str,
                       present: Optional[Set[str]] = None) -> str:
        xml_lower = str(xml or "").lower()
        scores = {
            domain: sum(xml_lower.count(k) for k in keywords)
            for domain, keywords in self._DOMAIN_KEYWORDS.items()
        }
        best = max(scores, key=scores.get)
        if scores[best] <= 0:
            return "general"
        second = sorted(scores.values())[-2]
        if scores[best] < second * self.DOMAIN_MARGIN:
            return "general"
        if present is not None and best not in present:
            # Тема, под которую в корпусе нет ни одного эталона (`it`,
            # `manufacturing`): токен уводит ранжирование словом, которого не
            # несёт ни один документ, и выдача определяется остатком. Честный
            # ответ — «тему не различил», а не утверждение про пустой раздел.
            return "general"
        return best

    def _corpus_domains(self) -> Set[str]:
        """Домены, под которые в корпусе есть хотя бы один эталон."""
        return {str(m.get("domain") or "") for m in self._metadata}

    def _detect_complexity(self, root: ET.Element) -> str:
        count = len(root.findall(".//*"))
        if count > 50:
            return "high"
        if count > 20:
            return "medium"
        return "low"

    _BPMN_URI = "http://www.omg.org/spec/BPMN/20100524/MODEL"

    def _find_local(self, root: ET.Element, *tags: str) -> List[ET.Element]:
        found = [root.findall(f".//{{{self._BPMN_URI}}}{t}") for t in tags]
        results = [e for part in found for e in part]
        if results:
            return results
        wanted = set(tags)
        return [
            elem for elem in root.iter()
            if isinstance(elem.tag, str) and elem.tag.rsplit("}", 1)[-1] in wanted
        ]

    def _parse(self, xml: str) -> Optional[ET.Element]:
        """Схема пользователя недоверенная: разбираем через тот же защитный
        парсер, что и аплайер; битый XML означает «признаков нет», не падение."""
        try:
            return bpmn_edits.parse_xml(xml)
        except Exception:  # noqa: BLE001
            return None

    # Практики выводятся из структуры схемы вместе с ключом признака: сравнивать
    # практики по формулировке нельзя — один и тот же признак должен узнаваться
    # в эталоне и в схеме пользователя.
    _PRACTICE_SIGNALS = (
        ("boundaryEvent", "boundary",
         "Обработка ошибок через граничные события (boundary events)"),
        ("subProcess", "subprocess",
         "Сложные фрагменты вынесены в подпроцессы"),
        ("timerEventDefinition", "timer",
         "Таймеры для контроля сроков (SLA) и эскалаций"),
        ("parallelGateway", "parallel",
         "Независимые ветки выполняются параллельно"),
        ("textAnnotation", "annotation",
         "Пояснения к сложным участкам через аннотации"),
        ("errorEventDefinition", "error",
         "Централизованная обработка исключений"),
    )

    def _practice_signals(self, root: ET.Element) -> List[Tuple[str, str]]:
        """(признак, формулировка) практик по разобранной схеме."""
        signals = [(key, text) for tag, key, text in self._PRACTICE_SIGNALS
                   if self._find_local(root, tag)]
        tasks = self._find_local(root, "task", "userTask", "serviceTask",
                                 "scriptTask", "manualTask", "businessRuleTask")
        if len(tasks) > 10 and not self._find_local(root, "subProcess"):
            signals.append(("long_chains",
                            "Длинные цепочки задач стоит группировать в подпроцессы"))
        return signals

    def _practice_pairs(self, root: ET.Element) -> List[List[str]]:
        """Практики в хранение: списки [признак, формулировка], а не tuple —
        так манифест кэша переживает JSON без спец-разбора."""
        return [[key, text] for key, text in self._practice_signals(root)]

    _NAME_TAGS = (
        "process", "participant", "task", "userTask", "serviceTask", "scriptTask",
        "manualTask", "businessRuleTask", "subProcess", "exclusiveGateway",
        "parallelGateway", "inclusiveGateway", "eventBasedGateway", "startEvent",
        "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
        "boundaryEvent",
    )

    def _element_names(self, root: Optional[ET.Element]) -> List[str]:
        """Названия элементов схемы — смысловая сигнатура для эмбеддингов."""
        if root is None:
            return []
        return [name for tag in self._NAME_TAGS
                for elem in self._find_local(root, tag)
                if (name := (elem.get("name") or "").strip())]

    def _document_texts(self, schema: Dict[str, Any]) -> Tuple[str, str]:
        """(семантический, лексический) текст эталона: ветки поиска учатся на
        разных признаках, поэтому и запрос раскладывается на два текста."""
        readable = _process_key(str(schema.get("name", ""))).replace("_", " ")
        practices = " ".join(pair[1] for pair in (schema.get("practices") or []))
        names = schema.get("element_names", "")
        semantic = " ".join([readable, schema.get("domain", ""), names, practices])
        lexical = " ".join([schema.get("xml_features", ""), schema.get("domain", ""),
                            schema.get("complexity", ""), readable, names])
        return semantic, lexical

    def _query_texts(self, query: str,
                     xml_content: Optional[str]) -> Tuple[str, str]:
        """Семантике — запрос и названия элементов, лексике — структура схемы.
        Склеивать их нельзя: признаки вида `tasks_12 low_complexity` живут в
        том же словаре, что и документы корпуса, и тонут в шуме.

        Запрос в лексическую ветку дописывали и сняли по замеру: `python -m
        eval.retrieval` дал те же P@3/MRR@3 до и после (0.58/0.88 по эталонным
        кейсам), потому что в просьбах контуру нет различающих слов — «найди
        узкие места» есть в описаниях почти всех эталонов и весит по IDF почти
        ноль. Домен в этот текст приходит ограниченный корпусом: тема, под
        которую в датасете нет ни одного эталона (`it`, `manufacturing`),
        уводила ранжирование словом, которого не несёт ни один документ.
        """
        if not xml_content:
            return query, query
        root = self._parse(xml_content)
        names = " ".join(self._element_names(root))
        structural = self._extract_xml_features(root)
        corpus = self._corpus_domains()
        domain = self._detect_domain(xml_content, corpus if corpus else None)
        return (" ".join([query, names]).strip(),
                " ".join([structural, domain, names]).strip())

    @staticmethod
    def _fuse_ranks(branch_scores: List[np.ndarray]) -> np.ndarray:
        """RRF: суммируются места в списках рангов, а не косинусы разных шкал.
        Деление на число веток держит счёт в масштабе 0..1, поэтому порог
        означает одно и то же и при двух ветках, и при деградации до одной."""
        total = np.zeros(len(branch_scores[0]))
        for scores in branch_scores:
            ranks = np.empty(len(scores), dtype=np.int64)
            ranks[np.argsort(-scores, kind="stable")] = np.arange(len(scores))
            total += 1.0 / (RRF_K + ranks + 1.0)
        best = 1.0 / (RRF_K + 1.0)
        return total / (len(branch_scores) * best)

    def _extract_xml_features(self, root: Optional[ET.Element]) -> str:
        """Структурная сигнатура схемы: количества элементов и признаки практик.

        Ключи практик — те же, что у `_PRACTICE_SIGNALS`, то есть те же, что
        попадают в `missing_practices` промпта: подбор «процесс с такой же
        конструкцией» и «чего в вашей схеме нет» перестают быть двумя разными
        описаниями одной схемы.

        Количества оставлены точными после замера, а не по недосмотру. Казалось,
        что `tasks_11` и `tasks_12` — разные слова словаря TF-IDF, редкое значение
        получает высокий IDF и ветка начинает награждать совпадение счётчиков
        («сток формы»: на кредитной заявке top-3 дали шаблоны упражнений). Полосы
        `few_tasks`/`many_tasks` вместо чисел это убирали — и роняли единственный
        работавший кейс: `warehouse_delivery` P@3 с 1.00 до 0.00, сводка с 0.50 до
        0.33, MRR@3 с 0.58 до 0.25. Точный счётчик размера и есть тот признак,
        который отличает содержательный процесс от пустого шаблона в датасете,
        где 42 файла — заготовки упражнений.
        """
        if root is None:
            return "invalid_xml"
        tasks = len(self._find_local(root, "task", "userTask", "serviceTask",
                                     "scriptTask", "manualTask", "businessRuleTask"))
        gateways = len(self._find_local(root, "exclusiveGateway", "parallelGateway",
                                        "inclusiveGateway", "eventBasedGateway"))
        events = len(self._find_local(root, "startEvent", "endEvent",
                                      "intermediateThrowEvent", "intermediateCatchEvent",
                                      "boundaryEvent"))
        flows = len(self._find_local(root, "sequenceFlow"))
        subprocesses = len(self._find_local(root, "subProcess"))
        total = tasks + gateways + events + subprocesses
        level = ("high" if total > 30 else "medium" if total > 15 else "low")
        present = [key for tag, key, _ in self._PRACTICE_SIGNALS
                   if self._find_local(root, tag)]
        counts = (f"tasks_{tasks} gateways_{gateways} events_{events} "
                  f"flows_{flows} subprocesses_{subprocesses}")
        return " ".join([counts] + present + [f"{level}_complexity"])


# ---------------------------------------------------------------------------
# Промпты планирования
# ---------------------------------------------------------------------------

def _operations_block() -> str:
    """Примеры операций для промпта берутся из OP_SPEC аплайера.

    Перечисление операций прямо в промпте разошлось с аплайером: модель не
    предлагала дорожки, документацию и граничные события, хотя скоринг их
    требует. Источник истины один — словарь `_HANDLERS`.
    """
    return "\n".join(f"- {example}" for example in bpmn_edits.OP_SPEC.values())


_SYSTEM_PROMPT = """Ты — эксперт по BPMN 2.0 и бизнес-аналитик. Анализируешь схему и предлагаешь изменения набором операций.

ОТВЕТ — строго один JSON-объект без пояснений, без блоков кода и без тегов рассуждений:
{
  "analysis": "анализ и рекомендации для пользователя на русском",
  "operations": [ ... ]
}

ДОПУСТИМЫЕ ОПЕРАЦИИ (каждая — объект с полем "op"):
""" + _operations_block() + """

ПРАВИЛА:
1. Все новые элементы — только с id, начинающимся с "new_".
2. Используй только существующие id из ИНВЕНТАРЯ; ничего не выдумывай.
3. Поток между разными пулами — только flow_type=message.
4. Роли сотрудников одной организации — ДОРОЖКИ внутри одного пула
   (`add_lane`, `move_to_lane`), а не новые пулы: новый пул заводят только для
   реального внешнего участника (другая организация, внешняя система). Если
   схема уже разведена по пулам-ролям, сливай их операцией `merge_participants`
   (source — лишний пул, target — пул организации, `as_lane: true` — дорожку
   назовём именем пула-источника). Пул, где из шагов только старт и финиш,
   пустой: удали его операцией `remove_participant`, не оставляй «на потом».
5. Ожидание и сроки (SLA) оформляются таймером с хронометражем:
   `add_boundary_event` event_type=timer с duration ("PT15M", "PT2H") либо
   cycle ("R3/PT10M") на задаче, обработка исключений — event_type=error.
   Промежуточное событие-ловушку можно просить коротко: `add_event`
   event_type=timer (или message, error, signal) — аплайер сам возьмёт
   intermediateCatch с нужным определением, `duration`/`cycle` работают так же.
   Значение — только ISO-8601, битое отвергается; без поля таймер получит
   PT15M. Граничное событие само по себе ничего не делает: сразу добавляй ему
   ветку обработки — `add_event` или `add_task` и `connect` от события к этой
   ветке. Событие без исходящего потока остаётся тупиком и качество схемы
   ухудшит. Пояснения к шагам — `add_documentation`.
6. Ветвление — `add_gateway` gateway_type=exclusive. Развилку закрывают
   парной: если ветки должны сойтись в одну точку, добавь ВТОРОЙ `add_gateway`
   и поведи в него по одному потоку из каждой ветки, а из него — дальше по
   маршруту («шлюз на входе, шлюз на выходе»), иначе ветка обрывается.
   На КАЖДОЙ ветке исключающего шлюза обязателен `condition`, кроме одной —
   новой ветке дай `"default": true` в `connect`, условие уже существующего
   потока поправь операцией `add_condition` (по id потока из ИНВЕНТАРЯ), а уже
   существующую ногу без условия сделай запасной операцией `set_default`.
   `connect` на пару, которая есть в схеме, отвечает «такой поток уже
   существует» — условие им не добавить. Необусловленную ветку без default
   аплайер отвергает: скоринг считает такую ветку ошибкой.
7. Новый шаг обязан встать в маршрут с двух сторон, и для этого у одной
   операции есть оба конца: `after` ставит шаг в поток (вход), `to` ведёт его
   дальше (исход). Отдельный `connect` нужен только там, где из узла реально
   раздваивается маршрут или где цель шага нельзя выразить одним `to`. Шаг,
   связанный только с одной стороны, — тупик или недостижимый узел: качество
   схемы падает ниже исходного, и аплайер откатывает такую правку целиком.
   Граничное событие живёт только вместе с веткой обработки: `to` шага-эскалации
   задаётся самой операцией `add_boundary_event`, а у шага эскалации при этом
   обязан быть свой выход (`to` или `after` на существующий шаг).
8. Если задача пользователя — только анализ (например, «найди узкие места»,
   «проверь ошибки»), верни пустой массив "operations".
9. Сохраняй бизнес-логику: предлагай минимально необходимые изменения.
10. Все тексты — на языке запроса пользователя.
11. Не больше """ + str(MAX_OPERATIONS) + """ операций в ответе: лишние отрезаются,
   и план становится неполным.
12. Название `op` берётся только из ДОПУСТИМЫХ ОПЕРАЦИЙ: `add_flow`,
   `remove_flow` или имени вида `add_userTask` там нет — поток создаёт
   операция `connect` (поля `source` и `target`), а вид задачи — это поле
   `task_type` операции `add_task`. Выдуманное `op` отклоняется, и правка
   просто не попадает в схему.

ПРИМЕР ОТВЕТА (показывает порядок правок; id и имена бери из своего ИНВЕНТАРЯ).
ИНВЕНТАРЬ (фрагмент): пулы «Цех фасовки» и «Транспортный отдел» (у \
«Транспортного отдела» только старт и финиш); в «Цехе фасовки» дорожка Lane_1, \
задачи A2 «Заменить деталь» и A3 «Запустить линию» в ней, поток F2 A2 → A3 без \
условия.
ЗАДАЧА: «заведи отсчёт длительности замены, вилку по ремфонду закрой парой \
шлюзов, лишний пустой пул убери».

{"analysis": "На замене узла нет отсчёта длительности, ветка по ремфонду не оформлена шлюзом, пул «Транспортный отдел» пустой. Добавляю граничный таймер с веткой доклада, пару шлюзов расщепления и схождения, убираю пустой пул.", "operations": [
 {"op": "add_task", "id": "new_A9", "name": "Доложить мастеру участка", "task_type": "userTask", "participant": "Цех фасовки", "lane": "Lane_1", "to": "A_end"},
 {"op": "add_boundary_event", "id": "new_T1", "attached_to": "A2", "event_type": "timer", "name": "Замена дольше 6 часов", "duration": "PT6H", "to": "new_A9"},
 {"op": "add_gateway", "id": "new_G1", "name": "Деталь в ремфонде?", "gateway_type": "exclusive", "participant": "Цех фасовки", "after": "A2"},
 {"op": "disconnect", "flow": "F2"},
 {"op": "add_task", "id": "new_A10", "name": "Внести узел в план заказа", "task_type": "userTask", "participant": "Цех фасовки", "lane": "Lane_1"},
 {"op": "add_gateway", "id": "new_G2", "name": "Схождение веток", "gateway_type": "exclusive", "participant": "Цех фасовки"},
 {"op": "connect", "source": "new_G1", "target": "new_A10", "condition": "Детали нет"},
 {"op": "connect", "source": "new_G1", "target": "new_G2", "default": true},
 {"op": "connect", "source": "new_A10", "target": "new_G2"},
 {"op": "connect", "source": "new_G2", "target": "A3"},
 {"op": "add_documentation", "id": "new_G1", "text": "Ветка «детали нет» переносит узел в план заказа"},
 {"op": "remove_participant", "participant": "Транспортный отдел"}]}

Что в этом примере существенного: `after` переставляет поток A2 → A3 на новый
шлюз, и тот сохраняет id F2 — поэтому «disconnect» именно F2; у расходящегося
new_G1 две ветки, одна с условием, вторая помечена `default`; new_G2 — пара к
нему, у сходящегося шлюза условий нет и исходящий поток один; каждый новый шаг
объявляется с обеими сторонами маршрута (`after` + `to` или `to` у граничного
события), поэтому лишних `connect` в пакете нет и ничего не откатится."""

_USER_TEMPLATE = """ИНВЕНТАРЬ СХЕМЫ:
{inventory}

{limits_block}{findings_block}{practices_block}ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Напоминание: ответ — только JSON-объект с полями "analysis" и "operations"."""

_RETRY_TEMPLATE = """Улучшение применено не полностью.

ИНВЕНТАРЬ СХЕМЫ ПОСЛЕ ЧАСТИЧНОГО ПРИМЕНЕНИЯ:
{inventory}

НЕПРИМЕНЁННЫЕ ОПЕРАЦИИ И ПРИЧИНЫ:
{skipped}{applied}{routing}{pools}{docs}{regressions}{limits_block}
ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Верни только корректирующие операции, которые добиваются той же цели.
Не используй id, которые уже есть в инвентаре. Это единственный корректирующий
раунд: начни с пропусков, названных выше, и не трать операции на то, что уже
применилось. Ответ — только JSON с полями "analysis" и "operations"."""


def _refusal_text(skipped: List[Dict[str, Any]]) -> str:
    """Текст отказа, из которого читается, что делать.

    Откат пакета даёт несколько записей с одной и той же причиной (операция,
    перенесённая из `applied`, плюс строка `batch`), и дословный повтор в
    сообщении только шумит. Подсказка уходит пользователю вместе с причиной:
    «пакет создал цикл без защищённого выхода» без «верните ветку через
    исключающий шлюз» — это диагноз без лечения, а следующий шаг известен.
    """
    parts: List[str] = []
    seen = set()
    for entry in skipped:
        reason = str(entry.get("reason") or "").strip()
        hint = str(entry.get("hint") or "").strip()
        if not reason or (reason, hint) in seen:
            continue
        seen.add((reason, hint))
        parts.append(f"{reason} ({hint})" if hint else reason)
        if len(parts) == 3:
            break
    return ("Предложенные изменения не удалось применить: "
            + "; ".join(parts or ["модель не вернула применимых операций"])
            + ". Уточните запрос.")


def _cycle_reject(base_xml: str):
    """Гарант по циклу для одного раунда применения.

    Цикл без защищённого выхода — ухудшение, а не стиль: процесс из него не
    выходит. Пакет, который его замкнул, откатывается к схеме этого раунда, а
    модель получает шанс перестроить ветку корректирующим повтором.
    """
    def _reject(candidate: str) -> Optional[bpmn_edits.Rejection]:
        if (candidate == base_xml or not has_unguarded_cycle(candidate)
                or has_unguarded_cycle(base_xml)):
            return None
        return bpmn_edits.Rejection(
            "пакет создал цикл без защищённого выхода — изменение откачено",
            "верните ветку через исключающий шлюз, у которого есть выход из "
            "цикла (condition либо default)")
    return _reject


def _prune_stranded(candidate: str, created: Dict[str, str]):
    """Снять узлы пакета, которые починка оставила вне маршрута.

    Починка идёт после аплайера и вправе снять дугу: в прогоне #40 так приняли
    улучшение, и принятие потеряло 15 баллов на `boundary_handled`. Гарантия
    «принятое изменение не делает схему хуже» обязана действовать и после
    починки, а пропуск — попасть в корректирующий повтор, если он ещё впереди.
    """
    return bpmn_edits.rollback_stranded(candidate, created)


def _unrouted(notes: List[str]) -> List[str]:
    """Пометки validate_and_repair об узлах, которые применились, но остались
    вне маршрута."""
    return [n for n in notes
            if any(m in n for m in bpmn_edits.UNROUTED_NOTE_MARKERS)]


def _empty_pools(notes: List[str]) -> List[str]:
    """Пометки о пулах без единого шага: connect их не лечит, нужна другая
    правка — поэтому они идут отдельным блоком, а не в «узлы вне маршрута»."""
    return [n for n in notes
            if any(m in n for m in bpmn_edits.POOL_EMPTY_NOTE_MARKERS)]


def _repair_own_notes(notes: List[str]) -> List[str]:
    """Что починка изменила в схеме сама, а не попросила доделать у модели.

    Тип элемента подменён, дуга снята или переведена в поток-сообщение, событие
    достроено — всё это уходит в итоговый XML без единой операции модели.
    Пользователь читает это в анализе до принятия: иначе «принятое улучшение»
    для него оборачивается схемой, которую он не выбирал."""
    covered = bpmn_edits.UNROUTED_NOTE_MARKERS + bpmn_edits.POOL_EMPTY_NOTE_MARKERS
    return [n for n in notes
            if any(m in n for m in bpmn_edits.REPAIR_OWN_NOTE_MARKERS)
            and not any(m in n for m in covered)]


def _op_key(entry: Dict[str, Any]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    """«Операция и элемент, который она правит»: по ним повтор узнаёт правку,
    которую модель провела во втором раунде в исправленном виде.

    Поля берутся из `bpmn_edits.OP_ELEMENT_FIELDS`. Нагрузку правки (`name`,
    `text`, `condition`, `after`, `to`) в ключ брать нельзя: корректирующий
    вызов как раз и приносит её исправленной — с `after` у откатанного шага,
    с условием у ветки. Старая версия читала только `id`/`source`/`target`,
    поэтому у операции, названной одним `participant` (`remove_participant`),
    ключ выходил пустым, зачёт не ставился никогда, и пользователь читал «не
    применено» об уже удалённом пуле. `name` остаётся запасным ключом для
    операций без id (`add_lane`) — иначе две разные дорожки слились бы в один
    зачёт.
    """
    key = tuple((field, str(entry[field]))
                for field in bpmn_edits.OP_ELEMENT_FIELDS if entry.get(field))
    if not key and entry.get("name"):
        key = (("name", str(entry["name"])),)
    return (str(entry.get("op") or ""), key)


def _routing_block(unrouted: List[str]) -> str:
    if not unrouted:
        return ""
    return ("\nУЗЛЫ ВНЕ МАРШРУТА — присоедини их операциями connect "
            "(или add_task с after), иначе схема станет хуже исходной:\n"
            + "\n".join(f"- {n}" for n in unrouted[:10]) + "\n")


def _pools_block(pools: List[str]) -> str:
    if not pools:
        return ""
    return ("\nПУЛЫ БЕЗ ШАГОВ — присоединить connect их нельзя: встрой шаги "
            "операциями add_task/add_event в этот пул, слей merge_participants "
            "или удали remove_participant:\n"
            + "\n".join(f"- {p}" for p in pools[:5]) + "\n")


def _docs_block(ids: List[str]) -> str:
    if not ids:
        return ""
    return ("\nНОВЫЕ УЗЛЫ БЕЗ ОПИСАНИЯ — скоринг меряет долю элементов с "
            "документацией, и каждый добавленный шаг без описания роняет балл "
            "схемы: добавь add_documentation для каждого id выше, текстом из "
            "описания процесса (не выдумывая данных):\n"
            + "\n".join(f"- {i}" for i in ids[:10]) + "\n")


# Оформление — не узкое место: правила про описание, имя и разнообразие типов
# задач процесс не меняют. В промпте они последней секцией, чтобы срез по
# лимиту сначала съедал их, а не развилку без схода.
COSMETIC_RULES = frozenset({"documentation", "naming", "task_types"})


def _rules_regressed(base_xml: str, new_xml: str) -> Dict[str, str]:
    """Правила скоринга, которые проходили на базе и провалены после пакета.

    Гарантия «принятое улучшение не делает схему хуже» не могла опираться на
    балл: два сдвинутых правила дают ноль дельты при сломанном третьем, и в
    живом замере пакет с корректной операцией `add_event` ронял
    `event_types` (95 → 90) без единой пометки — ни отказа, ни пометки починки,
    ни повода для переспроса. Возвращает {правило: "passed->failed"}.

    `not_applicable` ухудшением не считается: правило снято с проверки, потому
    что пакет убрал саму ситуацию. Так легальное слияние роли в дорожку
    выключило бы `participant_interacts` и `role_pools`, и любой гарант,
    построенный на «статус изменился», отказывал бы улучшению, за которое его
    и просят.
    """
    if base_xml == new_xml:
        return {}
    try:
        delta = diff_scores(_scorer.evaluate(base_xml), _scorer.evaluate(new_xml))
    except Exception:  # noqa: BLE001 — скоринг не имеет права ронять улучшение
        return {}
    return {name: f"{entry.get('before')}->{entry.get('after')}"
            for name, entry in (delta.get("rules") or {}).items()
            if entry.get("before") == PASSED
            and entry.get("after") == FAILED}


def _regressions_block(regressed: Dict[str, str]) -> str:
    """Что пакет сломал в уже проходивших правилах — с подсказкой, чем чинить."""
    if not regressed:
        return ""
    rules = _scorer.rules
    lines = []
    for name, change in sorted(regressed.items()):
        action = ", ".join(rules.get(name, {}).get("action") or []) or "?"
        lines.append(f"— {name} ({change}): {action}")
    return ("\nПРАВИЛА, КОТОРЫЕ ПАКЕТ СЛОМАЛ (до правки проходили, после — "
            "провалены; верни операции, которые их возвращают):\n"
            + "\n".join(lines) + "\n")


def _rule_passes(xml: str, rule: str) -> Optional[bool]:
    """Проходит ли правило скоринга на схеме (None — схему не прочитать или
    правила нет: скоринг не имеет права ронять улучшение своей ошибкой разбора)."""
    try:
        details = _scorer.evaluate(xml).get("details") or {}
    except Exception:  # noqa: BLE001
        return None
    if rule not in details:
        return None
    return bool(details[rule])


def _added_ids(report: Dict[str, Any]) -> List[str]:
    """Узлы, которые этот пакет добавил на схему."""
    return sorted({str(entry.get("id")) for entry in (report.get("applied") or [])
                   if entry.get("id")
                   and str(entry.get("op") or "") in bpmn_edits.ADD_NODE_OPS})


def _documentation_diluted(base_xml: str, new_xml: str,
                           added: List[str]) -> List[str]:
    """Новые узлы, из-за которых правило `documentation` перестало проходить.

    Скоринг меряет долю элементов с описанием, поэтому шаг, добавленный без
    описания, разбавляет её и роняет балл: прогон #48 — три корректных пакета на
    warehouse_delivery (0 отклонённых операций, все инварианты зелёные) дали
    `score_delta` −2.5 и `no_regression` 0.5. Гарантия «принятое улучшение не
    делает схему хуже» обязана покрывать и разбавление, а текст описания знает
    только модель — додумывать его контур не вправе.
    """
    if not added:
        return []
    if _rule_passes(base_xml, "documentation") is not True:
        return []
    if _rule_passes(new_xml, "documentation") is not False:
        return []
    return list(added)


def _applied_block(applied: List[Dict[str, Any]]) -> str:
    """Что пакет уже провёл. Без этого модель в корректирующем раунде второй
    раз предлагала ту же правку под новым id и ловила отказ «такое событие уже
    прицеплено» — повтор тратился на дубль, а не на пропуск."""
    if not applied:
        return ""
    lines = []
    for entry in applied[:12]:
        ident = (entry.get("id") or entry.get("source") or entry.get("flow")
                 or entry.get("lane") or "")
        note = (entry.get("note") or "").split(":")[0]
        lines.append(f"- {entry.get('op')} {ident}".rstrip()
                     + (f" — {note}" if note else ""))
    return ("\nУЖЕ ПРИМЕНЕНО В ЭТОМ ПАКЕТЕ — эти правки повторять не нужно:\n"
            + "\n".join(lines) + "\n")


def _limits_block(inventory: Dict[str, Any]) -> str:
    """Инвентарь срезан по потоку — модель обязана знать, что часть схемы ей
    не показана, и не предлагать правки «по несуществующим» id."""
    limits = inventory.get("limits") or {}
    if not limits:
        return ""
    return ("Часть схемы не показана из-за размера: "
            + ", ".join(f"{key}={value}" for key, value in sorted(limits.items()))
            + ". Правки предлагай только по показанным id.\n\n")


MAX_SCORING_FINDINGS = 10
_FINDING_SECTIONS = ("ТОЧКИ УЛУЧШЕНИЯ ПРОЦЕССА", "НАРУШЕНИЯ НОТАЦИИ BPMN",
                     "ОФОРМЛЕНИЕ (процесс не меняет)")


def _finding_section(name: str) -> int:
    if name in BUSINESS_RULES:
        return 0
    return 2 if name in COSMETIC_RULES else 1


def _findings_block(bpmn_xml: str) -> str:
    """УЗКИЕ МЕСТА ПО СКОРИНГУ: за что с этой схемы уже сняли баллы.

    Скоринг и есть оценивающий шаг контура — детерминированный, второй вызов
    модели ради него не нужен. Без него пакет уходил в документацию и новые
    шаги, пока рядом висело правило на −8 за роль, раздутую в участника: модель
    не знала, что именно считается дефектом.

    Порядок — секциями (сначала то, что меняет процесс) и по весу правила, а не
    по порядку объявления словаря: на срезе по позиции в списке показанных
    оставались `naming` (−10) и `no_isolated` (−8), а за линией исчезали
    `role_pools` с готовой парой «Кладовщик → ВкусВилл», `guarded_cycles` (−10)
    и `documentation` (−7). Скрытые правила называются по именам: «и ещё N» не
    говорит модели, чего она не видит, а молчаливый срез означает, что мы мерим
    отредактированный план, а не модель.
    """
    evaluation = _scorer.evaluate(bpmn_xml)
    meta = evaluation.get("details_meta") or {}
    by_rule = evaluation.get("recommendations_by_rule") or {}
    if not by_rule:
        return ""
    ranked = sorted(by_rule,
                    key=lambda name: (_finding_section(name),
                                      -(meta.get(name, {}).get("weight") or 0),
                                      name))
    shown, hidden = ranked[:MAX_SCORING_FINDINGS], ranked[MAX_SCORING_FINDINGS:]
    lines: List[str] = []
    current = -1
    for name in shown:
        section = _finding_section(name)
        if section != current:
            lines.append(("" if lines else "") + _FINDING_SECTIONS[section] + ":")
            current = section
        lines.append(f"— {by_rule[name]}")
    tail = ("— скрыто лимитом (важнее показанных не меньше): "
            + ", ".join(hidden) + "\n") if hidden else ""
    return ("УЗКИЕ МЕСТА ПО СКОРИНГУ. Порядок — по важности для процесса; "
            "поднять балл нужнее, чем украшать:\n"
            + "\n".join(lines) + "\n" + tail)


def _format_practices(schemas: List[Dict[str, Any]]) -> str:
    """Блок ЛУЧШИЕ ПРАКТИКИ: только то, чего нет в схеме пользователя.

    Совпадения в промпте бессмысленны — модель предложила бы их повторно, —
    поэтому при пустой разности блок пустой. Одна и та же практика в трёх
    похожих эталонах — одна строка со ссылкой на эталоны: три дубля забивают
    контекст и выглядят как навязчивая идея, а не как подтверждённая практика.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for schema in schemas[:TOP_SCHEMAS]:
        name = str(schema.get("name") or "без названия")
        for practice in list(schema.get("missing_practices") or [])[:PRACTICES_PER_SCHEMA]:
            entry = grouped.setdefault(practice, {"sources": []})
            if name not in entry["sources"]:
                entry["sources"].append(name)
    if not grouped:
        return ""
    lines = []
    for practice, entry in list(grouped.items())[:PRACTICES_LIMIT]:
        shown = ", ".join(entry["sources"][:2])
        if len(entry["sources"]) > 2:
            shown += f" и ещё {len(entry['sources']) - 2}"
        # Домен эталона сюда не печатается: тег `[hr]` читается моделью как
        # готовое имя участника, и провенанс eval ловил его как утечку — по
        # сценарию «HR» оракул требовал пул с тем же названием.
        lines.append(f"- {practice} (в эталонах: {shown})")
    header = "ЛУЧШИЕ ПРАКТИКИ ИЗ ПОХОЖИХ ЭТАЛОННЫХ СХЕМ (чего нет в текущей схеме):"
    return header + "\n" + "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------

class BPMNImprovementOrchestrator:
    """Планирование изменений схемы через операции и их применение."""

    def __init__(self, knowledge_base: Optional[BPMNKnowledgeBase] = None):
        self.knowledge_base = knowledge_base or BPMNKnowledgeBase()

    def warmup(self) -> None:
        """Готовит корпус эталонов заранее: сборка индекса — десятки секунд,
        и платить ими должен не первый пользовательский /api/ai/improve."""
        self.knowledge_base.ensure_ready()

    async def improve_diagram(self, xml_content: str,
                              user_prompt: str) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """Возвращает (рекомендации, улучшенный XML или None, отчёт операций).

        XML равен None для чисто аналитических ответов и при ошибках —
        ошибки сообщаются через ImprovementError.
        """
        try:
            inventory = await asyncio.to_thread(bpmn_edits.build_inventory,
                                                xml_content)
        except Exception as e:  # noqa: BLE001 — входной XML недоверенный
            raise ImprovementError(f"Не удалось разобрать XML схемы: {e}") from e

        # Практики считаются один раз: повтор планирования обязан видеть тот
        # же контекст, что и первый план.
        practices_block = await asyncio.to_thread(
            self._practices_block, user_prompt, xml_content)

        def _plan_step() -> Dict[str, Any]:
            user_content = _USER_TEMPLATE.format(
                inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
                limits_block=_limits_block(inventory),
                findings_block=_findings_block(xml_content),
                practices_block=practices_block,
                user_prompt=user_prompt,
            )
            return llm_client.call_json(_SYSTEM_PROMPT, user_content,
                                        temperature=PLANNING_TEMPERATURE,
                                        max_tokens=8000)

        try:
            plan = await asyncio.to_thread(_plan_step)
        except LLMTruncatedError as e:
            raise ImprovementError(
                "План изменений обрезан по лимиту токенов и не применён. "
                "Уточните запрос или разбейте его на части."
            ) from e
        except LLMRequestTooLargeError as e:
            # Схема больше, чем вмещает контекст: это не «сервис лег», и
            # Retry-After здесь только морочит клиента — править надо вход.
            raise ImprovementError(
                "Схема слишком велика для ИИ-улучшения: её описание не "
                "помещается в контекст модели. Уменьшите схему (разбейте "
                "процесс на части) или улучшайте по фрагменту."
            ) from e
        except LLMError as e:
            raise ImprovementUnavailable(
                "Сервис улучшения временно недоступен. Попробуйте позже."
            ) from e
        except ValueError as e:
            raise ImprovementError(
                "Не удалось получить корректный план изменений. Попробуйте "
                "переформулировать запрос."
            ) from e

        analysis = str(plan.get("analysis") or "").strip() or "Анализ завершён."
        planned = plan.get("operations") or []
        if not isinstance(planned, list):
            # План без массива операций — не «анализ», а сбой формата: без
            # этой строки пользователь увидел бы красивый отказ.
            analysis += ("\n\nМодель вернула операции не списком — изменения "
                         "не применялись, только анализ.")
            planned = []
        operations = planned[:MAX_OPERATIONS]
        truncated_operations = max(0, len(planned) - len(operations))
        if truncated_operations:
            # Молча отрезать план нельзя: пользователь иначе решит, что всё
            # предложенное применилось.
            analysis += (f"\n\nПлан неполный: предложено {len(planned)} операций, "
                         f"применяем первые {MAX_OPERATIONS}, "
                         f"отброшено {truncated_operations}.")

        if not operations:
            return analysis, None, {"status": "analysis_only", "applied": [],
                                    "skipped": [],
                                    "truncated_operations": truncated_operations}

        xml_after, report = await asyncio.to_thread(
            bpmn_edits.apply_and_guarantee, xml_content, operations,
            _cycle_reject(xml_content), _prune_stranded)
        report["truncated_operations"] = truncated_operations
        # Раунд помечается сразу: если корректирующий вызов не состоится (LLM
        # лег), иначе его причины пользователь прочитает как «повтор».
        for skip in report["skipped"]:
            skip.setdefault("stage", "plan")
            skip.setdefault("reapplied", False)
        repair_notes = list(report.get("notes") or [])
        first_reverted = str(report.get("reverted") or "")

        # Один целевой повтор: по неприменённым операциям, по узлам вне
        # маршрута, по пулам без шагов, по разбавленной документации и по
        # правилам скоринга, которые пакет сломал. Без последнего принятие
        # улучшения роняет балл схемы — модель обязана добить связность сама, а
        # не доверять это эвристике аплайера.
        unrouted = _unrouted(repair_notes)
        empty_pools = _empty_pools(repair_notes)
        # Разбавление documentation — то же ухудшение, только меряется долей:
        # пакет добавляет шаг, а описание к нему пишет только модель.
        undocumented = _documentation_diluted(xml_content, xml_after,
                                              _added_ids(report))
        regressed = _rules_regressed(xml_content, xml_after)
        retried = False
        if ([s for s in report["skipped"] if not s.get("reapplied")] or unrouted
                or empty_pools or undocumented or regressed):
            # Факт «повтор вызывали» ставится ДО вызова: отказ транспорта или
            # пустой ответ — это тоже состоявшийся повтор, а не его отсутствие.
            # Иначе `retry_attempted` читался бы как «контур не пытался
            # починиться» ровно в тех прогонах, где попытка и была.
            retried = True
            try:
                # Третий элемент — «повтор принёс второй пакет правок», а не
                # «повтор был»: попыткой он остаётся и при пустом ответе.
                xml_after, report, _reapplied = await self._retry_skipped(
                    xml_after, user_prompt, report, unrouted, empty_pools,
                    undocumented, regressed)
                # Пометки второго раунда не заменяют первые: понижение шлюза до
                # задачи неидемпотентно, и второй `validate_and_repair` его уже
                # не вернёт — терять факт подмены типа элемента нельзя.
                repair_notes = bpmn_edits.merge_notes(
                    repair_notes, list(report.get("notes") or []))
                first_reverted = first_reverted or str(report.get("reverted") or "")
            except LLMTruncatedError as e:
                logger.warning("Корректирующий повтор обрезан по лимиту "
                               "токенов: %s", e)
            except (LLMError, ValueError) as e:
                logger.warning("Корректирующий повтор не выполнен: %s", e)
            unrouted = _unrouted(repair_notes)
            empty_pools = _empty_pools(repair_notes)
            undocumented = _documentation_diluted(xml_content, xml_after,
                                                  _added_ids(report))

        # Что пакет сломал из уже проходивших правил — факт, а не вывод. Он
        # идёт и в переспрос (топливо для корректировки), и в отчёт (метрика
        # `improve/rules_regressed_share`), и пользователю в анализ. Целиком
        # пакету из-за него не отказывают: регресс по `start_event` после
        # легального слияния пулов или по `documentation` после добавленного
        # шага — это арифметика правила, а не сломанная схема, и отказ
        # выбрасывал бы правку, которую пользователь как раз и просил.
        # Гарантия «не делать хуже» там, где она исполнима механически: откат
        # пакета с незащищённым циклом и поузловой откат того, что починка
        # оставила вне маршрута (`bpmn_edits.apply_and_guarantee`).
        regressed = _rules_regressed(xml_content, xml_after)
        report["retry_attempted"] = retried
        report["retry_closed"] = sum(1 for s in report["skipped"]
                                     if s.get("reapplied"))
        report["package_reverted"] = first_reverted or str(
            report.get("reverted") or "")
        # Пусто при откате: сломанная база — не заслуга и не вина пакета.
        report["rules_regressed"] = {} if report["package_reverted"] else dict(
            regressed)
        report["noop_rows"] = int(report.get("noop_rows") or 0)
        report["notes"] = repair_notes
        report["repair_notes"] = repair_notes

        # Висячий шаг — не улучшение: модель не указала, между какими шагами
        # он нужен, и додумывать маршрут за неё нельзя. То же для граничного
        # события без ветки обработки. Показываем это в рекомендациях, а не
        # только в техническом отчёте.
        unrouted = _unrouted(repair_notes)
        if unrouted:
            analysis += ("\n\nИзменения применены, но остались вне маршрута — "
                         "скоринг считает их тупиками, пока не добавлена "
                         "связь:\n"
                         + "\n".join(f"- {n}" for n in unrouted[:5]))
        empty_pools = _empty_pools(repair_notes)
        if empty_pools:
            analysis += ("\n\nОстались пулы без единого шага: наполните их, "
                         "слейте в дорожку или удалите:\n"
                         + "\n".join(f"- {p}" for p in empty_pools[:5]))
        if undocumented:
            analysis += ("\n\nНовые узлы остались без описания: скоринг меряет "
                         "долю элементов с документацией, поэтому без него "
                         "улучшение снимает балл схеме:\n"
                         + "\n".join(f"- {i}" for i in undocumented[:5]))
        if (own_notes := _repair_own_notes(repair_notes)):
            analysis += ("\n\nПочинка поправила схему сама — в итоговом XML эти "
                         "узлы выглядят иначе, чем их предложила модель:\n"
                         + "\n".join(f"- {n.strip()}" for n in own_notes[:5]))
        if regressed:
            rules = _scorer.rules
            analysis += ("\n\nУлучшение применилось, но задело правила, которые "
                         "раньше проходили — при принятии балл схемы упадёт:\n"
                         + "\n".join(
                             f"- {name}: {(rules.get(name) or {}).get('message', '')}"
                             for name in sorted(regressed)[:5]))

        if not report["applied"]:
            raise ImprovementError(_refusal_text(report["skipped"]))

        open_skips = [s for s in report["skipped"] if not s.get("reapplied")]
        # Дословный повтор отказа — тот же дефект: в списке для пользователя
        # он остаётся один раз.
        shown = [s for s in open_skips if not s.get("duplicate")]
        if shown:
            skipped_lines = "\n".join(
                f"- {'план' if s.get('stage') == 'plan' else 'повтор'}: "
                f"{s['op']}: {s['reason']}"
                for s in shown[:5]
            )
            analysis += ("\n\nНе применено (требует уточнения):\n" + skipped_lines)
        redo_count = len(report["skipped"]) - len(open_skips)
        if redo_count:
            analysis += (f"\n\nПовтор добил {redo_count} правк"
                         f"{'у' if redo_count == 1 else 'и'}, отклонённые "
                         "первым раундом.")
        repeats = report.get("repeat_rejections") or 0
        if repeats:
            analysis += (f"\n\nКорректирующий повтор вернул {repeats} "
                         "отклонённую правк"
                         f"{'у' if repeats == 1 else 'и'} без изменений — это "
                         "тот же дефект, а не новый, поэтому в отчёте он "
                         "показан один раз.")

        return analysis, xml_after, report

    def _practices_block(self, user_prompt: str, xml_content: str) -> str:
        """Поиск по корпусу — тот же CPU, что и планирование: вызывается
        только из рабочего потока."""
        practices = self.knowledge_base.find_best_practices(user_prompt, xml_content)
        return _format_practices(practices)

    async def _retry_skipped(self, intermediate_xml: str, user_prompt: str,
                             report: Dict[str, Any], unrouted: List[str],
                             empty_pools: List[str],
                             undocumented: Optional[List[str]] = None,
                             regressed: Optional[Dict[str, str]] = None,
                             ) -> Tuple[str, Dict[str, Any], bool]:
        """Корректирующий раунд: по не применённым операциям и по узлам,
        оставшимся вне маршрута.

        Блок практик сюда не попадает: задача повтора — добить конкретные
        пропуски, а не искать новые улучшения, и практики только уводили модель
        в сторону от починки.

        Отчёт склеивается честно — `skipped` хранит финальную судьбу каждой
        операции с пометкой раунда, иначе в анализ уходят причины не от тех
        операций, что модель предлагала изначально. Отказ, повторённый дословно
        (та же операция, тот же элемент, та же причина), помечается
        `duplicate`: пользователю он показывается один раз как тот же дефект,
        но из отчёта не удаляется — по записям раунда видно, что корректирующий
        вызов был и ничего не добил.
        """
        def _retry_step() -> Dict[str, Any]:
            inventory = bpmn_edits.build_inventory(intermediate_xml)
            user_content = _RETRY_TEMPLATE.format(
                inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
                skipped=json.dumps(report["skipped"], ensure_ascii=False, indent=1),
                applied=_applied_block(report.get("applied") or []),
                routing=_routing_block(unrouted),
                pools=_pools_block(empty_pools),
                docs=_docs_block(undocumented or []),
                regressions=_regressions_block(regressed or {}),
                limits_block=_limits_block(inventory),
                user_prompt=user_prompt,
            )
            return llm_client.call_json(_SYSTEM_PROMPT, user_content,
                                        temperature=PLANNING_TEMPERATURE,
                                        max_tokens=8000)

        plan = await asyncio.to_thread(_retry_step)
        retry_planned = plan.get("operations") or []
        if not isinstance(retry_planned, list):
            retry_planned = []
        retry_ops = [op for op in retry_planned if isinstance(op, dict)][:MAX_OPERATIONS]
        retry_truncated = max(0, len(retry_planned) - len(retry_ops))
        if not retry_ops:
            return intermediate_xml, report, False

        # Тот же гарант, что и в первом раунде: повтор не вправе оставить
        # схему хуже того, что уже сошлось, — циклический отказ здесь
        # откатывает только второй пакет, а правки первого остаются.
        xml_after, retry_report = await asyncio.to_thread(
            bpmn_edits.apply_and_guarantee, intermediate_xml, retry_ops,
            _cycle_reject(intermediate_xml), _prune_stranded)
        # Повтор вправе провести ту же правку в исправленном виде (откатанный
        # шаг вставлен через after). Такую правку нельзя показывать пользователю
        # как невыполненную.
        redo = {_op_key(a) for a in retry_report["applied"]}
        for skip in report["skipped"]:
            key = _op_key(skip)
            if key[1] and key in redo:
                skip["reapplied"] = True
        # Модель вправе вернуть повторившийся отказ дословно: та же операция,
        # тот же элемент, та же причина (в живых прогонах повтор повторял
        # первый раунд целиком). Такой пропуск помечается `duplicate` и не
        # показывается пользователю вторым строкой — это тот же дефект. Из
        # отчёта он при этом не удаляется: по записям раунда метрика харнесса
        # видит, что корректирующий вызов был и ничего не добил.
        def _fingerprint(entry: Dict[str, Any]) -> tuple:
            # Все поля идентичности, а не `_op_key`: два `connect` с одним
            # источником и разными целями — два дефекта, а не одно «то же».
            return (str(entry.get("op") or ""),
                    *(str(entry.get(field) or "") for field in
                      ("id", "source", "target", "flow")),
                    str(entry.get("reason") or ""))

        first_round = {_fingerprint(s) for s in report["skipped"]}
        skipped = report["skipped"] + [
            dict(s, stage="retry", reapplied=False,
                 duplicate=_fingerprint(s) in first_round)
            for s in retry_report["skipped"]]
        repeats = sum(1 for s in skipped if s.get("duplicate"))
        open_skips = [s for s in skipped if not s.get("reapplied")]
        merged = {
            # Статус считается по незакрытым пропускам: скип, который повтор
            # добил той же операцией над тем же элементом, остаётся в истории,
            # но не делает результат «частичным».
            "status": "success" if not open_skips else ("partial"
                                                        if (report["applied"]
                                                            or retry_report["applied"])
                                                        else "failed"),
            "applied": report["applied"] + retry_report["applied"],
            "skipped": skipped,
            # Сколько отказов повтор вернул дословно: сводка пользователя
            # говорит об этом отдельно, а в `skipped` они не дублируются.
            "repeat_rejections": repeats,
            "truncated_operations": (report.get("truncated_operations", 0)
                                    + retry_truncated),
            # Пометки и «применилось зря» складываются по раундам: оркестратор
            # читает их из финального отчёта, а второй раунд не отменяет
            # первого.
            "notes": bpmn_edits.merge_notes(report.get("notes") or [],
                                           retry_report.get("notes") or []),
            "noop_rows": (int(report.get("noop_rows") or 0)
                          + int(retry_report.get("noop_rows") or 0)),
            "reverted": str(retry_report.get("reverted") or ""),
        }
        return xml_after, merged, True

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
from .llm_client import LLMError, LLMTruncatedError
from . import bpmn_edits

logger = logging.getLogger(__name__)

DATASET_PATH = os.path.join(os.path.dirname(__file__), "bpmn_dataset")
# Кэш индекса — рядом с кодом, а не относительно CWD: путь «уплывал» между
# контейнером и терминалом, а два воркера дрались за одну запись. Формат —
# npz + JSON-манифест вместо pickle: pickle.load чужого файла исполняет код.
CACHE_NAME = "bpmn_rag_cache.npz"
CACHE_METHOD = "hybrid-v3"

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

    _DOMAIN_KEYWORDS = {
        "finance": ["банк", "платеж", "кредит", "счет", "транзакци", "инвойс",
                    "finance", "bank", "payment", "credit", "invoice"],
        "approval": ["согласован", "одобрен", "проверк", "подпис", "утвержден",
                     "approval", "approve", "review", "sign"],
        "manufacturing": ["производ", "завод", "сборк", "качеств",
                          "manufacturing", "production", "assembly"],
        "customer_service": ["клиент", "поддержк", "жалоб", "обращен",
                             "customer", "support", "complaint", "ticket"],
        "logistics": ["доставк", "отправк", "склад", "логистик",
                      "delivery", "shipment", "warehouse", "logistics"],
        "hr": ["персонал", "найм", "сотрудник", "онбординг",
               "recruitment", "onboarding", "employee"],
        "it": ["интеграци", "систем", "сервер", "деплой",
               "deployment", "integration", "server", "api"],
    }

    def _detect_domain(self, xml: str) -> str:
        xml_lower = xml.lower()
        scores = {
            domain: sum(xml_lower.count(k) for k in keywords)
            for domain, keywords in self._DOMAIN_KEYWORDS.items()
        }
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "general"

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
        том же словаре, что и документы корпуса, и тонут в шуме."""
        if not xml_content:
            return query, query
        root = self._parse(xml_content)
        names = " ".join(self._element_names(root))
        structural = self._extract_xml_features(root)
        domain = self._detect_domain(xml_content)
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
        """Структурная сигнатура схемы: только количества, они попадают
        в лексическую ветку поиска."""
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
        return (f"tasks_{tasks} gateways_{gateways} events_{events} "
                f"flows_{flows} subprocesses_{subprocesses} {level}_complexity")


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
   реального внешнего участника (другая организация, внешняя система).
5. Сроки и эскалации закрыты — `add_boundary_event` с event_type=timer,
   обработка исключений — event_type=error, пояснения к шагам — `add_documentation`.
   Промежуточное событие-ловушку можно просить коротко: `add_event`
   event_type=timer (или message, error, signal) — аплайер сам возьмёт
   intermediateCatch с нужным определением.
   Граничное событие само по себе ничего не делает: сразу добавляй ему ветку
   обработки — `add_event` или `add_task` и `connect` от события к этой ветке.
   Событие без исходящего потока остаётся тупиком и качество схемы ухудшит.
6. Ветвление — `add_gateway` gateway_type=exclusive. У такого шлюза должно
   быть минимум два исходящих потока, и `condition` обязателен на КАЖДОМ из
   них: выход по умолчанию (атрибут default) аплайер ставить не умеет, а
   скоринг считает необусловленную ветку ошибкой.
7. Новый шаг обязан встать в маршрут с двух сторон: вход (через `after` либо
   `connect` от предыдущего шага) и выход (`connect` к следующему шагу или к
   конечному событию его пула). Шаг, связанный только с одной стороны, — это
   тупик или недостижимый узел, и из-за него качество схемы падает ниже
   исходного.
8. Если задача пользователя — только анализ (например, «найди узкие места»,
   «проверь ошибки»), верни пустой массив "operations".
9. Сохраняй бизнес-логику: предлагай минимально необходимые изменения.
10. Все тексты — на языке запроса пользователя."""

_USER_TEMPLATE = """ИНВЕНТАРЬ СХЕМЫ:
{inventory}

{practices_block}ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Напоминание: ответ — только JSON-объект с полями "analysis" и "operations"."""

_RETRY_TEMPLATE = """{practices_block}Улучшение применено не полностью.

ИНВЕНТАРЬ СХЕМЫ ПОСЛЕ ЧАСТИЧНОГО ПРИМЕНЕНИЯ:
{inventory}

НЕПРИМЕНЁННЫЕ ОПЕРАЦИИ И ПРИЧИНЫ:
{skipped}
{routing}
ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Верни только корректирующие операции, которые добиваются той же цели.
Не используй id, которые уже есть в инвентаре. Ответ — только JSON с полями
"analysis" и "operations"."""


def _unrouted(notes: List[str]) -> List[str]:
    """Пометки validate_and_repair об узлах, которые применились, но остались
    вне маршрута."""
    return [n for n in notes
            if any(m in n for m in bpmn_edits.UNROUTED_NOTE_MARKERS)]


def _op_key(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Пара «операция — элемент»: по ней повтор узнаёт правку, которую модель
    провела во втором раунде в исправленном виде."""
    return (str(entry.get("op") or ""),
            str(entry.get("id") or entry.get("source") or entry.get("target") or ""))


def _routing_block(unrouted: List[str]) -> str:
    if not unrouted:
        return ""
    return ("\nУЗЛЫ ВНЕ МАРШРУТА — присоедини их операциями connect "
            "(или add_task с after), иначе схема станет хуже исходной:\n"
            + "\n".join(f"- {n}" for n in unrouted[:10]) + "\n")


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
        domain = str(schema.get("domain") or "general")
        for practice in list(schema.get("missing_practices") or [])[:PRACTICES_PER_SCHEMA]:
            entry = grouped.setdefault(practice, {"domain": domain, "sources": []})
            if name not in entry["sources"]:
                entry["sources"].append(name)
    if not grouped:
        return ""
    lines = []
    for practice, entry in list(grouped.items())[:PRACTICES_LIMIT]:
        shown = ", ".join(entry["sources"][:2])
        if len(entry["sources"]) > 2:
            shown += f" и ещё {len(entry['sources']) - 2}"
        lines.append(f"- [{entry['domain']}] {practice} (в эталонах: {shown})")
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

        xml_after, report = await asyncio.to_thread(bpmn_edits.apply_operations,
                                                    xml_content, operations)
        report["truncated_operations"] = truncated_operations
        # Раунд помечается сразу: если корректирующий вызов не состоится (LLM
        # лег), иначе его причины пользователь прочитает как «повтор».
        for skip in report["skipped"]:
            skip.setdefault("stage", "plan")
            skip.setdefault("reapplied", False)

        # Один целевой повтор: по неприменённым операциям и по узлам, которые
        # применились, но остались вне маршрута. Без второго пункта принятие
        # улучшения роняет балл схемы — модель обязана добить связность сама,
        # а не доверять это эвристике аплайера.
        xml_after, repair_notes = await asyncio.to_thread(bpmn_edits.validate_and_repair,
                                                          xml_after)
        unrouted = _unrouted(repair_notes)
        if report["skipped"] or unrouted:
            try:
                xml_after, report = await self._retry_skipped(
                    xml_after, user_prompt, practices_block, report, unrouted)
            except LLMTruncatedError as e:
                logger.warning("Корректирующий повтор обрезан по лимиту "
                               "токенов: %s", e)
            except (LLMError, ValueError) as e:
                logger.warning("Корректирующий повтор не выполнен: %s", e)
            xml_after, repair_notes = await asyncio.to_thread(
                bpmn_edits.validate_and_repair, xml_after)
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

        if not report["applied"]:
            reasons = "; ".join(s.get("reason", "") for s in report["skipped"][:3])
            raise ImprovementError(
                f"Предложенные изменения не удалось применить: {reasons}. "
                "Уточните запрос."
            )

        open_skips = [s for s in report["skipped"] if not s.get("reapplied")]
        if open_skips:
            skipped_lines = "\n".join(
                f"- {'план' if s.get('stage') == 'plan' else 'повтор'}: "
                f"{s['op']}: {s['reason']}"
                for s in open_skips[:5]
            )
            analysis += ("\n\nНе применено (требует уточнения):\n" + skipped_lines)
        redo_count = len(report["skipped"]) - len(open_skips)
        if redo_count:
            analysis += (f"\n\nПовтор добил {redo_count} правк"
                         f"{'у' if redo_count == 1 else 'и'}, отклонённые "
                         "первым раундом.")

        return analysis, xml_after, report

    def _practices_block(self, user_prompt: str, xml_content: str) -> str:
        """Поиск по корпусу — тот же CPU, что и планирование: вызывается
        только из рабочего потока."""
        practices = self.knowledge_base.find_best_practices(user_prompt, xml_content)
        return _format_practices(practices)

    async def _retry_skipped(self, intermediate_xml: str, user_prompt: str,
                             practices_block: str, report: Dict[str, Any],
                             unrouted: List[str]) -> Tuple[str, Dict[str, Any]]:
        """Корректирующий раунд: по не применённым операциям и по узлам,
        оставшимся вне маршрута.

        Отчёт склеивается честно — `skipped` хранит финальную судьбу каждой
        операции с пометкой раунда, иначе в анализ уходят причины не от тех
        операций, что модель предлагала изначально.
        """
        def _retry_step() -> Dict[str, Any]:
            inventory = bpmn_edits.build_inventory(intermediate_xml)
            user_content = _RETRY_TEMPLATE.format(
                practices_block=practices_block,
                inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
                skipped=json.dumps(report["skipped"], ensure_ascii=False, indent=1),
                routing=_routing_block(unrouted),
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
            return intermediate_xml, report

        xml_after, retry_report = await asyncio.to_thread(
            bpmn_edits.apply_operations, intermediate_xml, retry_ops)
        # Повтор вправе провести ту же правку в исправленном виде (откатанный
        # шаг вставлен через after). Такую правку нельзя показывать пользователю
        # как невыполненную.
        redo = {_op_key(a) for a in retry_report["applied"]}
        for skip in report["skipped"]:
            key = _op_key(skip)
            if key[1] and key in redo:
                skip["reapplied"] = True
        skipped = report["skipped"] + [dict(s, stage="retry", reapplied=False)
                                       for s in retry_report["skipped"]]
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
            "truncated_operations": (report.get("truncated_operations", 0)
                                    + retry_truncated),
        }
        return xml_after, merged

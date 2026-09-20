# Конвейер улучшения и анализа BPMN-схем.
#
# Архитектура: компактный инвентарь схемы → поиск практик по корпусу
# эталонов (гибрид семантики и TF-IDF) → один планирующий вызов LLM,
# возвращающий анализ и операции редактирования → детерминированный аплайер
# (core.bpmn_edits) → семантическая починка. Полный перегенерат XML моделью
# не используется: операции применяются кодом, координаты не нужны — раскладку
# делает фронтенд (bpmn-auto-layout).
import glob
import json
import logging
import os
import pickle
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chardet
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import llm_client
from .llm_client import LLMError
from . import bpmn_edits

logger = logging.getLogger(__name__)

DATASET_PATH = os.path.join(os.path.dirname(__file__), "bpmn_dataset")
CACHE_FILE = Path("bpmn_rag_cache.pkl")
CACHE_METHOD = "hybrid-v2"

SEMANTIC_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
# Ниже этого порога эталоны считаются нерелевантными и в промпт не попадают.
SIMILARITY_FLOOR = 0.25
PRACTICES_LIMIT = 3
TOP_SCHEMAS = 3
MAX_OPERATIONS = 25


class ImprovementError(RuntimeError):
    """Сбой улучшения с понятным для пользователя сообщением."""


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
        self._vectorizer = TfidfVectorizer(
            max_features=5000, ngram_range=(1, 2), max_df=0.95, min_df=1
        )
        self._semantic_available = True

    # --- публичное ---

    def find_best_practices(self, query: str, xml_content: Optional[str] = None,
                            top_k: int = TOP_SCHEMAS) -> List[Dict[str, Any]]:
        """Гибридный поиск эталонов: семантика + ключевые слова."""
        self.ensure_ready()
        if not self._metadata:
            return []

        search_text = query
        if xml_content:
            search_text += " " + self._extract_xml_features(xml_content)

        scores = np.zeros(len(self._metadata))

        if self._semantic_available and self._embeddings is not None:
            try:
                model = self._sentence_model()
                query_embedding = np.asarray(model.encode([search_text]), dtype="float32")
                norm = np.linalg.norm(query_embedding)
                if norm > 0:
                    query_embedding = query_embedding / norm
                semantic = (self._embeddings @ query_embedding.T).flatten()
                scores += SEMANTIC_WEIGHT * np.clip(semantic, 0.0, 1.0)
            except Exception as e:  # noqa: BLE001 — деградируем до TF-IDF
                logger.warning("Семантический поиск недоступен: %s", e)
                self._semantic_available = False
                scores = np.zeros(len(self._metadata))

        if self._vectors is not None and self._vectors.shape[0] == len(self._metadata):
            keyword = cosine_similarity(
                self._vectorizer.transform([search_text]), self._vectors
            )[0]
            weight = 1.0 if not self._semantic_available else KEYWORD_WEIGHT
            scores += weight * np.clip(keyword, 0.0, 1.0)

        order = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in order:
            if scores[idx] < SIMILARITY_FLOOR:
                continue
            schema = dict(self._metadata[int(idx)])
            schema["similarity"] = float(scores[idx])
            results.append(schema)
        logger.info("Найдено %d подходящих эталонов", len(results))
        return results

    # --- инициализация ---

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

    def _load_cache(self) -> bool:
        if not CACHE_FILE.exists():
            return False
        try:
            with open(CACHE_FILE, "rb") as f:
                cache = pickle.load(f)
            if cache.get("method") != CACHE_METHOD:
                logger.info("Кэш RAG устарел (метод %s), пересобираем",
                            cache.get("method"))
                return False
            self._metadata = cache["metadata"]
            self._embeddings = cache.get("embeddings")
            self._vectors = cache.get("vectors")
            self._vectorizer = cache["vectorizer"]
            logger.info("Кэш RAG загружен: % эталонов", len(self._metadata))
            return bool(self._metadata)
        except Exception as e:  # noqa: BLE001 — битый кэш пересобирается
            logger.warning("Не удалось прочитать кэш RAG (%s): пересборка", e)
            return False

    def _save_cache(self) -> None:
        try:
            with open(CACHE_FILE, "wb") as f:
                pickle.dump({
                    "method": CACHE_METHOD,
                    "metadata": self._metadata,
                    "embeddings": self._embeddings,
                    "vectors": self._vectors,
                    "vectorizer": self._vectorizer,
                }, f)
        except Exception as e:  # noqa: BLE001 — кэш производный, не критично
            logger.warning("Не удалось сохранить кэш RAG: %s", e)

    def _build(self) -> None:
        schemas = self._load_real_schemas()
        if not schemas:
            logger.warning("Корпус эталонов пуст: RAG работать не будет")
            return
        texts = [self._extract_text_features(s) for s in schemas]
        self._metadata = schemas
        self._vectors = self._vectorizer.fit_transform(texts)
        try:
            model = self._sentence_model()
            embeddings = model.encode(texts, show_progress_bar=False)
            embeddings = np.asarray(embeddings, dtype="float32")
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._embeddings = embeddings / norms
            logger.info("Индексировано % эталонов (семантика + TF-IDF)", len(schemas))
        except Exception as e:  # noqa: BLE001
            self._semantic_available = False
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
        logger.info("Найдено % файлов .bpmn в корпусе", len(bpmn_files))
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
            try:
                ET.fromstring(xml_content)
            except ET.ParseError as e:
                logger.warning("Некорректный XML в %s: %s", file_path, e)
                continue
            schemas.append({
                "name": os.path.splitext(os.path.basename(file_path))[0],
                "domain": self._detect_domain(xml_content),
                "complexity": self._detect_complexity(xml_content),
                "xml": xml_content,
                "best_practices": self._extract_best_practices(xml_content),
            })
        logger.info("Загружено % эталонных схем", len(schemas))
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

    def _detect_complexity(self, xml: str) -> str:
        try:
            root = ET.fromstring(xml)
            count = len(root.findall(".//*"))
        except ET.ParseError:
            return "unknown"
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

    def _extract_best_practices(self, xml: str) -> List[str]:
        practices = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return practices
        if self._find_local(root, "boundaryEvent"):
            practices.append("Обработка ошибок через граничные события (boundary events)")
        if self._find_local(root, "subProcess"):
            practices.append("Сложные фрагменты вынесены в подпроцессы")
        if self._find_local(root, "timerEventDefinition"):
            practices.append("Таймеры для контроля сроков (SLA) и эскалаций")
        if self._find_local(root, "parallelGateway"):
            practices.append("Независимые ветки выполняются параллельно")
        if self._find_local(root, "textAnnotation"):
            practices.append("Пояснения к сложным участкам через аннотации")
        if self._find_local(root, "errorEventDefinition"):
            practices.append("Централизованная обработка исключений")
        tasks = self._find_local(root, "task", "userTask", "serviceTask",
                                 "scriptTask", "manualTask", "businessRuleTask")
        if len(tasks) > 10 and not self._find_local(root, "subProcess"):
            practices.append("Длинные цепочки задач стоит группировать в подпроцессы")
        return practices

    def _extract_text_features(self, schema: Dict[str, Any]) -> str:
        features = [
            schema.get("name", ""),
            schema.get("domain", ""),
            schema.get("complexity", ""),
        ]
        features.extend(schema.get("best_practices", []))
        if schema.get("xml"):
            features.append(self._extract_xml_features(schema["xml"]))
        return " ".join(features)

    def _extract_xml_features(self, xml: str) -> str:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
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

_SYSTEM_PROMPT = """Ты — эксперт по BPMN 2.0 и бизнес-аналитик. Анализируешь схему и предлагаешь изменения набором операций.

ОТВЕТ — строго один JSON-объект без пояснений, без блоков кода и без тегов рассуждений:
{
  "analysis": "анализ и рекомендации для пользователя на русском",
  "operations": [ ... ]
}

ДОПУСТИМЫЕ ОПЕРАЦИИ (каждая — объект с полем "op"):
- {"op":"add_task","id":"new_...","name":"...","task_type":"task|userTask|serviceTask","participant":"пул","after":"id элемента"}
- {"op":"add_gateway","id":"new_...","name":"...","gateway_type":"exclusive|parallel|inclusive","participant":"пул","after":"id"}
- {"op":"add_event","id":"new_...","name":"...","event_type":"start|end|intermediateCatch|intermediateThrow","participant":"пул","after":"id (опц.)"}
- {"op":"add_participant","id":"new_...","name":"..."}
- {"op":"rename","id":"...","name":"..."}
- {"op":"delete","id":"..."}
- {"op":"connect","source":"...","target":"...","flow_type":"sequence|message","condition":"опц."}
- {"op":"disconnect","flow":"id потока"}
- {"op":"move_to_participant","id":"...","participant":"пул"}

ПРАВИЛА:
1. Все новые элементы — только с id, начинающимся с "new_".
2. Используй только существующие id из ИНВЕНТАРЯ; ничего не выдумывай.
3. Поток между разными пулами — только flow_type=message.
4. Если задача пользователя — только анализ (например, «найди узкие места»,
   «проверь ошибки»), верни пустой массив "operations".
5. Сохраняй бизнес-логику: предлагай минимально необходимые изменения.
6. Все тексты — на языке запроса пользователя."""

_USER_TEMPLATE = """ИНВЕНТАРЬ СХЕМЫ:
{inventory}

{practices_block}ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Напоминание: ответ — только JSON-объект с полями "analysis" и "operations"."""

_RETRY_TEMPLATE = """Часть операций не удалось применить к схеме.

ИНВЕНТАРЬ СХЕМЫ ПОСЛЕ ЧАСТИЧНОГО ПРИМЕНЕНИЯ:
{inventory}

НЕПРИМЕНЁННЫЕ ОПЕРАЦИИ И ПРИЧИНЫ:
{skipped}

ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Верни только корректирующие операции, которые добиваются той же цели.
Не используй id, которые уже есть в инвентаре. Ответ — только JSON с полями
"analysis" и "operations"."""


def _format_practices(schemas: List[Dict[str, Any]]) -> str:
    if not schemas:
        return ""
    lines = ["ЛУЧШИЕ ПРАКТИКИ ИЗ ПОХОЖИХ ЭТАЛОННЫХ СХЕМ:"]
    for schema in schemas[:PRACTICES_LIMIT]:
        practices = schema.get("best_practices", [])[:2]
        if not practices:
            continue
        lines.append(f"- [{schema.get('domain', 'general')}] "
                     f"{schema.get('name', 'без названия')}: "
                     + "; ".join(practices))
    return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------

class BPMNImprovementOrchestrator:
    """Планирование изменений схемы через операции и их применение."""

    def __init__(self, knowledge_base: Optional[BPMNKnowledgeBase] = None):
        self.knowledge_base = knowledge_base or BPMNKnowledgeBase()

    async def improve_diagram(self, xml_content: str,
                              user_prompt: str) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """Возвращает (рекомендации, улучшенный XML или None, отчёт операций).

        XML равен None для чисто аналитических ответов и при ошибках —
        ошибки сообщаются через ImprovementError.
        """
        import asyncio

        try:
            inventory = bpmn_edits.build_inventory(xml_content)
        except Exception as e:  # noqa: BLE001 — входной XML недоверенный
            raise ImprovementError(f"Не удалось разобрать XML схемы: {e}") from e

        def _plan_step() -> Dict[str, Any]:
            practices = self.knowledge_base.find_best_practices(user_prompt, xml_content)
            user_content = _USER_TEMPLATE.format(
                inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
                practices_block=_format_practices(practices),
                user_prompt=user_prompt,
            )
            return llm_client.call_json(_SYSTEM_PROMPT, user_content,
                                        temperature=0.2, max_tokens=8000)

        try:
            plan = await asyncio.to_thread(_plan_step)
        except LLMError as e:
            raise ImprovementError(
                "Сервис улучшения временно недоступен. Попробуйте позже."
            ) from e
        except ValueError as e:
            raise ImprovementError(
                "Не удалось получить корректный план изменений. Попробуйте "
                "переформулировать запрос."
            ) from e

        analysis = str(plan.get("analysis") or "").strip() or "Анализ завершён."
        operations = plan.get("operations") or []
        if not isinstance(operations, list):
            operations = []
        operations = operations[:MAX_OPERATIONS]

        if not operations:
            return analysis, None, {"status": "analysis_only", "applied": [], "skipped": []}

        xml_after, report = bpmn_edits.apply_operations(xml_content, operations)

        # Один целевой повтор только по неприменённым операциям.
        if report["skipped"]:
            try:
                xml_after, report = await self._retry_skipped(
                    xml_content, xml_after, user_prompt, report
                )
            except (LLMError, ValueError) as e:
                logger.warning("Повтор по пропущенным операциям не выполнен: %s", e)

        xml_after, repair_notes = bpmn_edits.validate_and_repair(xml_after)
        report["repair_notes"] = repair_notes

        if not report["applied"]:
            reasons = "; ".join(s.get("reason", "") for s in report["skipped"][:3])
            raise ImprovementError(
                f"Предложенные изменения не удалось применить: {reasons}. "
                "Уточните запрос."
            )

        if report["skipped"]:
            skipped_lines = "\n".join(
                f"- {s['op']}: {s['reason']}" for s in report["skipped"][:5]
            )
            analysis += ("\n\nНе применено (требует уточнения):\n" + skipped_lines)

        return analysis, xml_after, report

    async def _retry_skipped(self, original_xml: str, intermediate_xml: str,
                             user_prompt: str,
                             report: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        import asyncio

        def _retry_step() -> Dict[str, Any]:
            inventory = bpmn_edits.build_inventory(intermediate_xml)
            user_content = _RETRY_TEMPLATE.format(
                inventory=json.dumps(inventory, ensure_ascii=False, indent=1),
                skipped=json.dumps(report["skipped"], ensure_ascii=False, indent=1),
                user_prompt=user_prompt,
            )
            return llm_client.call_json(_SYSTEM_PROMPT, user_content,
                                        temperature=0.1, max_tokens=8000)

        plan = await asyncio.to_thread(_retry_step)
        retry_ops = [op for op in (plan.get("operations") or [])
                     if isinstance(op, dict)][:MAX_OPERATIONS]
        if not retry_ops:
            return intermediate_xml, report

        xml_after, retry_report = bpmn_edits.apply_operations(intermediate_xml, retry_ops)
        merged = {
            "status": retry_report["status"],
            "applied": report["applied"] + retry_report["applied"],
            "skipped": retry_report["skipped"],
        }
        return xml_after, merged

"""ИИ-компоненты приложения.

`BPMNGenerator` и `BPMNScorer` лёгкие и создаются при импорте. Оркестратор
улучшения поднимает модели эмбеддингов и читает корпус эталонов (~секунды и
сотни мегабайт RAM), поэтому инициализируется в lifespan приложения, а не на
импорте модуля.
"""
import logging
from typing import Optional

from core import llm_client
from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import BPMNScorer
from core.llm_improve import BPMNImprovementOrchestrator

logger = logging.getLogger(__name__)

generator = BPMNGenerator()
scorer = BPMNScorer()

_orchestrator: Optional[BPMNImprovementOrchestrator] = None


def init_orchestrator() -> None:
    """Создаёт оркестратор один раз на процесс и прогревает авторизацию LLM.

    Ошибка ключа видна в логе старта, а не через минуту после первого запроса;
    сервис при этом поднимается — реестр, доступ и апрувы работают и без ИИ.
    """
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = BPMNImprovementOrchestrator()
        logger.info("LLM-оркестратор инициализирован")
    try:
        llm_client.ensure_llm_ready()
        logger.info("Авторизация LLM подтверждена (модель %s)", llm_client.get_model_name())
    except llm_client.LLMError as e:
        logger.error("LLM недоступна, ИИ-эндпоинты будут отвечать ошибкой: %s", e)
    # Корпус эталонов собирается секунды: иначе первый же «найди узкие места»
    # платит warming-of-the-world в пользовательском запросе.
    try:
        _orchestrator.warmup()
    except Exception as e:  # noqa: BLE001 — без корпуса улучшение деградирует, но работает
        logger.warning("Индекс эталонов не прогрет, первый запрос будет долгим: %s", e)


def get_orchestrator() -> Optional[BPMNImprovementOrchestrator]:
    """Оркестратор либо None, что означает «ИИ-улучшение недоступно»."""
    return _orchestrator

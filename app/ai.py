"""ИИ-компоненты приложения.

`BPMNGenerator` и `BPMNScorer` лёгкие и создаются при импорте. Оркестратор
улучшения поднимает модели эмбеддингов и читает корпус эталонов (~секунды и
сотни мегабайт RAM), поэтому инициализируется в lifespan приложения, а не на
импорте модуля.
"""
import logging
from typing import Optional

from core.bpmn_generator import BPMNGenerator
from core.bpmn_scoring import BPMNScorer
from core.llm_improve import BPMNImprovementOrchestrator

logger = logging.getLogger(__name__)

generator = BPMNGenerator()
scorer = BPMNScorer()

_orchestrator: Optional[BPMNImprovementOrchestrator] = None


def init_orchestrator() -> None:
    """Создаёт оркестратор один раз на процесс."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = BPMNImprovementOrchestrator()
        logger.info("LLM-оркестратор инициализирован")


def get_orchestrator() -> Optional[BPMNImprovementOrchestrator]:
    """Оркестратор либо None, что означает «ИИ-улучшение недоступно»."""
    return _orchestrator

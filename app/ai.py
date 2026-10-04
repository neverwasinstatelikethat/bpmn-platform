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
    Это обещание, и оно должно держаться: сборка оркестратора (тяжёлые модели
    эмбеддингов, корпус эталонов) и авторизация провайдера — чужая доступность,
    а не наше состояние, поэтому ни один их сбой не имеет права уносить процесс.
    """
    global _orchestrator
    if _orchestrator is None:
        try:
            _orchestrator = BPMNImprovementOrchestrator()
            logger.info("LLM-оркестратор инициализирован")
        except Exception as error:  # noqa: BLE001 — ИИ деградирует, сервис жив
            logger.error("ИИ-улучшение недоступно: оркестратор не собран (%s)",
                         type(error).__name__)
            logger.debug("Сборка оркестратора", exc_info=True)
            return
    try:
        llm_client.ensure_llm_ready()
        logger.info("Авторизация LLM подтверждена (пул моделей: %s)",
                    ", ".join(llm_client.get_model_pool()))
    except llm_client.LLMError as e:
        logger.error("LLM недоступна, ИИ-эндпоинты будут отвечать ошибкой: %s", e)
    except Exception as error:  # noqa: BLE001 — тот же контракт на старте
        logger.error("Авторизация LLM не подтверждена (%s), ИИ-эндпоинты будут "
                     "отвечать ошибкой", type(error).__name__)
        logger.debug("Проба авторизации LLM", exc_info=True)
    # Корпус эталонов собирается секунды: иначе первый же «найди узкие места»
    # платит warming-of-the-world в пользовательском запросе.
    try:
        _orchestrator.warmup()
    except Exception as e:  # noqa: BLE001 — без корпуса улучшение деградирует, но работает
        logger.warning("Индекс эталонов не прогрет, первый запрос будет долгим: %s", e)


def get_orchestrator() -> Optional[BPMNImprovementOrchestrator]:
    """Оркестратор либо None, что означает «ИИ-улучшение недоступно»."""
    return _orchestrator

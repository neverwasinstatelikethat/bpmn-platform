# Транспорт до LLM-провайдера и устойчивый разбор ответов.
#
# Единственная точка работы с провайдером: настройки только из окружения,
# ограниченное число попыток, таймауты. Синхронные функции — вызывать из
# async-контура через asyncio.to_thread, чтобы не блокировать событийный цикл.
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://foundation-models.api.cloud.ru/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 180


class LLMError(RuntimeError):
    """Сбой обращения к LLM после исчерпания попыток."""


_client: Optional[OpenAI] = None


def get_llm_client() -> OpenAI:
    """Клиент провайдера (ленивый синглтон). Ключ — только из окружения."""
    global _client
    if _client is None:
        api_key = os.getenv("LLM_API_KEY", "")
        if not api_key:
            raise LLMError(
                "LLM_API_KEY не задан: заполните переменную окружения "
                "(см. .env.example)"
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    return _client


def get_model_name() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def _complete(messages: List[Dict[str, str]], temperature: float,
              max_tokens: Optional[int]) -> str:
    """Один раунд с ограниченными повторами. Только транспортные повторы
    (429/5xx/сеть); смысловые ошибки ответа повторяет вызывающий код."""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RETRY_PAUSE_SECONDS)
        try:
            response = get_llm_client().chat.completions.create(
                model=get_model_name(),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            )
            content = response.choices[0].message.content if response.choices else None
            if not content:
                last_error = "пустой ответ модели"
                logger.warning("Попытка %s: %s", attempt, last_error)
                continue
            return content
        except Exception as e:  # noqa: BLE001 — транспортные ошибки провайдера
            last_error = str(e)
            logger.warning("Попытка %s: ошибка обращения к LLM: %s", attempt, last_error)
    raise LLMError(f"LLM недоступен после {MAX_ATTEMPTS} попыток: {last_error}")


def call_text(system: str, user: str, *, temperature: float = 0.2,
              max_tokens: Optional[int] = 16000) -> str:
    """Текстовый ответ модели (сырой, без разбора)."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return _complete(messages, temperature, max_tokens)


def call_json(system: str, user: str, *, temperature: float = 0.2,
              max_tokens: Optional[int] = 16000) -> Dict[str, Any]:
    """JSON-ответ модели с устойчивым извлечением.

    Ошибка разбора повторяется один раз с указанием проблемы — это единственная
    «смысловая» повторная попытка в системе.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    raw = _complete(messages, temperature, max_tokens)
    try:
        return extract_json(raw)
    except ValueError as e:
        logger.warning("Первый ответ не распарсен (%s): запрашиваю исправленный JSON", e)
        repair_note = (
            f"Предыдущий ответ не удалось разобрать как JSON: {e}. "
            "Верни ТОЛЬКО исправленный валидный JSON без пояснений, "
            "без блоков кода и без тегов рассуждений."
        )
        raw = _complete(messages + [
            {"role": "assistant", "content": raw[:12000]},
            {"role": "user", "content": repair_note},
        ], temperature, max_tokens)
        return extract_json(raw)


_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_PATTERN = re.compile(r"```[a-zA-Z]*\n?")


def _strip_noise(raw: str) -> str:
    text = raw.strip()
    text = _THINK_PATTERN.sub("", text)
    text = _FENCE_PATTERN.sub("", text)
    return text.strip()


def _close_truncated(candidate: str) -> Optional[str]:
    """Дозакрывает оборванный JSON: строки, скобки и ключи без значений."""
    stack: List[str] = []
    in_string = False
    escaped = False
    for ch in candidate:
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if not stack or stack[-1] != "{":
                return None
            stack.pop()
        elif ch == "]":
            if not stack or stack[-1] != "[":
                return None
            stack.pop()
    if not stack:
        return candidate
    repaired = candidate
    if in_string:
        repaired += '"'
    repaired = re.sub(r",\s*$", "", repaired)
    # Висящий ключ без значения делает объект невалидным — отрезаем его.
    repaired = re.sub(r',\s*"[^"]*"\s*:\s*$', "", repaired)
    repaired = re.sub(r'\{\s*"[^"]*"\s*:\s*$', "{", repaired)
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def extract_json(raw: str) -> Dict[str, Any]:
    """Извлекает объект из произвольного обрамления: блоки кода, <think>,
    текст вокруг, оборванный ответ."""
    if not raw:
        raise ValueError("ответ пуст")
    text = _strip_noise(raw)
    if text.startswith("["):
        # Модель вернула список операций вместо пакета {"analysis", "operations"}.
        # Молча взять первую операцию нельзя: применилась бы произвольная правка.
        raise ValueError("корневой элемент не объект: модель вернула массив")
    candidates: List[str] = []

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    if start != -1:
        candidates.append(text[start:])  # оборванный хвост без закрывающих скобок

    errors = []
    for candidate in candidates:
        for variant in (candidate, _close_truncated(candidate)):
            if not variant:
                continue
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError as e:
                errors.append(str(e))
                continue
            if isinstance(parsed, dict):
                return parsed
            errors.append("корневой элемент не объект")
    raise ValueError("не удалось выделить валидный JSON: " + "; ".join(errors[:3]))


_DEFINITIONS_OPEN = re.compile(r"<\s*([A-Za-z_][\w.-]*:)?definitions\b", re.IGNORECASE)


def extract_xml(raw: str) -> str:
    """Извлекает BPMN definitions из ответа модели независимо от префикса
    (bpmn: или пространство имён по умолчанию)."""
    if not raw:
        raise ValueError("ответ пуст")
    text = _strip_noise(raw)
    match = _DEFINITIONS_OPEN.search(text)
    if not match:
        raise ValueError("в ответе нет элемента definitions")
    prefix = match.group(1) or ""
    close_tag = f"</{prefix}definitions>"
    close_index = text.lower().rfind(close_tag.lower())
    if close_index == -1 or close_index <= match.start():
        raise ValueError("нет закрывающего тега definitions")
    candidate = text[match.start():close_index + len(close_tag)]
    # Проверка синтаксиса без загрузки внешних сущностей не требуется:
    # дальше XML всегда уходит в defusedxml/ET парсер потребителя.
    return candidate

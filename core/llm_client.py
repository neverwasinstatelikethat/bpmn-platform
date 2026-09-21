# Транспорт до LLM-провайдера и устойчивый разбор ответов.
#
# Единственная точка работы с провайдером: GigaChat (библиотека gigachat),
# настройки только из окружения, ограниченное число попыток, таймауты.
# Синхронные функции — вызывать из async-контура через asyncio.to_thread,
# чтобы не блокировать событийный цикл.
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import httpx
from gigachat import GigaChat
from gigachat.exceptions import (AuthenticationError, ForbiddenError,
                                 RateLimitError, ResponseError, ServerError)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "GigaChat"
DEFAULT_SCOPE = "GIGACHAT_API_PERS"
MAX_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 180
# Скопированный ответ модели в repair-запросе — только префикс: целиком он
# удваивает контекст повторного вызова.
REPAIR_CONTEXT_LIMIT = 4000

# Повторяем только транспортные сбои: 5xx, лимиты и сеть. Остальные 4xx
# означают, что запрос неверен сам по себе, — повтор его не лечит, а время
# тратит. Слой повторов ровно один (этот): retries SDK оставлены нулём, иначе
# попытки перемножились бы с repair-циклом.
_RETRYABLE = (ServerError, httpx.HTTPError, OSError)


class LLMError(RuntimeError):
    """Сбой обращения к LLM после исчерпания попыток."""


class LLMTruncatedError(LLMError):
    """Ответ обрезан по лимиту токенов: структура неполная.

    Отличается от LLMError тем, что повтор бесполезен (max_tokens тот же), а
    применять такой ответ нельзя: `_close_truncated` молча «починил» бы
    оборванный на середине список операций.
    """


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        logger.warning("Переменная %s не число — беру %s", name, default)
        return default


MAX_CONCURRENT = _env_int("GIGACHAT_MAX_CONCURRENT", 1)
# Scope GIGACHAT_API_PERS/B2B разрешает по одному одновременному запросу:
# лишние параллельные вызовы провайдер отбивает 429, поэтому очередь у нас.
_GATE = threading.BoundedSemaphore(MAX_CONCURRENT)

_client: Optional[GigaChat] = None
_client_guard = threading.Lock()


def _verify_ssl() -> bool:
    # GigaChat отдаёт сертификат из российского УЦ: на машине без корневого
    # сертификата ГосУС проверка TLS не проходит, поэтому её можно выключить.
    return os.getenv("GIGACHAT_VERIFY_SSL", "true").strip().lower() not in (
        "0", "false", "no", "off"
    )


def get_llm_client() -> GigaChat:
    """Клиент провайдера (ленивый синглтон на процесс). Секреты — из окружения.

    SDK сам кэширует OAuth-токен и обновляет его по истечении, поэтому клиент
    переиспользуется: новый клиент на запрос = лишний round-trip за токеном.
    """
    global _client
    if _client is None:
        with _client_guard:
            if _client is None:
                credentials = os.getenv("GIGACHAT_CREDENTIALS", "")
                if not credentials:
                    raise LLMError(
                        "GIGACHAT_CREDENTIALS не задан: заполните переменную "
                        "окружения (см. .env.example)"
                    )
                if not _verify_ssl():
                    logger.warning(
                        "GIGACHAT_VERIFY_SSL=false: проверка TLS отключена, "
                        "ключ уходит в незащищённый канал. Допустимо только "
                        "локально; для прода — корневой сертификат российского "
                        "УЦ через GIGACHAT_CA_BUNDLE_FILE."
                    )
                _client = GigaChat(
                    credentials=credentials,
                    scope=os.getenv("GIGACHAT_SCOPE", DEFAULT_SCOPE),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    verify_ssl_certs=_verify_ssl(),
                )
    return _client


def _reset_client() -> None:
    """Забывает клиента после сбоя авторизации, чтобы следующий запрос
    авторизовался заново, а не ушёл на клиент с протухшим токеном."""
    global _client
    with _client_guard:
        _client = None


def _is_auth_failure(error: ResponseError) -> bool:
    """Сбой обмена учётных данных на токен.

    SDK поднимает по ошибке авторизации то, что вернул эндпоинт: OAuth отвечает
    400 (не 401), поэтому различаем по адресу запроса, а не по классу.
    """
    if isinstance(error, (AuthenticationError, ForbiddenError)):
        return True
    return "/oauth" in str(getattr(error, "url", "")).lower()


def get_model_name() -> str:
    return os.getenv("GIGACHAT_MODEL", DEFAULT_MODEL)


def ensure_llm_ready() -> None:
    """Авторизуется провайдера заранее.

    Вызывается из lifespan: ошибка ключа должна быть видна на старте сервиса,
    а не через минуту после старта первого пользовательского запроса.
    """
    get_llm_client().get_token()


def _content_of(response: Any) -> Optional[str]:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message else None
    return content if isinstance(content, str) else None


def _log_usage(model: str, attempt: int, elapsed_ms: int,
               response: Any) -> Optional[str]:
    usage = getattr(response, "usage", None)
    choices = getattr(response, "choices", None)
    finish = getattr(choices[0], "finish_reason", None) if choices else None
    logger.info(
        "Вызов LLM %s (попытка %s): %s мс, токены prompt=%s/completion=%s, finish=%s",
        model, attempt, elapsed_ms,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"), finish,
    )
    return finish


def _complete(messages: List[Dict[str, str]], temperature: float,
              max_tokens: Optional[int]) -> str:
    """Один раунд с ограниченными повторами. Только транспортные повторы
    (429/5xx/сеть); смысловые ошибки ответа повторяет вызывающий код."""
    from gigachat.models import Chat, Messages

    model = get_model_name()
    request = Chat(
        model=model,
        messages=[Messages(role=m["role"], content=m["content"]) for m in messages],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    last_error = ""
    pause = RETRY_PAUSE_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(pause)
            pause = RETRY_PAUSE_SECONDS
        started = time.monotonic()
        try:
            with _GATE:  # все пути к модели, включая repair-повторы
                response = get_llm_client().chat(request)
        except ResponseError as e:
            if _is_auth_failure(e):
                _reset_client()
                raise LLMError(
                    "GigaChat отклонил авторизацию — проверьте "
                    f"GIGACHAT_CREDENTIALS и GIGACHAT_SCOPE: {e}"
                ) from e
            if isinstance(e, RateLimitError):
                last_error = str(e)
                pause = max(e.retry_after, RETRY_PAUSE_SECONDS)
                logger.warning("Попытка %s: лимит запросов (%s), пауза %s с",
                               attempt, last_error, pause)
                continue
            if isinstance(e, _RETRYABLE):
                last_error = str(e)
                logger.warning("Попытка %s: ошибка обращения к LLM: %s",
                               attempt, last_error)
                continue
            raise LLMError(f"LLM отклонила запрос: {e}") from e
        except _RETRYABLE as e:
            last_error = str(e)
            logger.warning("Попытка %s: ошибка обращения к LLM: %s", attempt, last_error)
            continue
        finish = _log_usage(model, attempt,
                            int((time.monotonic() - started) * 1000), response)
        if finish == "length":
            raise LLMTruncatedError(
                "Ответ модели обрезан по лимиту токенов — структура неполная"
            )
        content = _content_of(response)
        if not content:
            last_error = "пустой ответ модели"
            logger.warning("Попытка %s: %s", attempt, last_error)
            continue
        return content
    raise LLMError(f"LLM недоступна после {MAX_ATTEMPTS} попыток: {last_error}")


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
        # Диалог пересобирается, а не дописывается: история с неразобранным
        # ответом целиком удвоила бы контекст повторного вызова.
        repair_user = (
            f"{user}\n\nПредыдущий ответ не удалось разобрать как JSON: {e}. "
            "Исправь его и верни ТОЛЬКО валидный JSON-объект без пояснений, "
            "без блоков кода и без тегов рассуждений:\n" + raw[:REPAIR_CONTEXT_LIMIT]
        )
        raw = _complete([messages[0], {"role": "user", "content": repair_user}],
                        temperature, max_tokens)
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
            if not isinstance(parsed, dict):
                errors.append("корневой элемент не объект")
                continue
            if variant != candidate:
                # Разобрали только то, что довели до конца: ответ оборвался.
                # Молча вернуть урезанный объект — значит применить план,
                # которого модель не досказала.
                raise LLMTruncatedError(
                    "JSON ответа неполон: пришлось дозакрывать структуру — "
                    "вероятно, ответ обрезан по лимиту токенов"
                )
            return parsed
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

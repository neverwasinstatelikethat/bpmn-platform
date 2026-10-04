# Транспорт до LLM-провайдера и устойчивый разбор ответов.
#
# Единственная точка работы с провайдером: GigaChat (библиотека gigachat),
# настройки только из окружения, ограниченное число попыток, таймауты.
# Синхронные функции — вызывать из async-контура через asyncio.to_thread,
# чтобы не блокировать событийный цикл.
import contextlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from gigachat import GigaChat
from gigachat.exceptions import (AuthenticationError, BadRequestError,
                                 ForbiddenError, NotFoundError, RateLimitError,
                                 RequestEntityTooLargeError, ResponseError,
                                 ServerError, UnprocessableEntityError)

logger = logging.getLogger(__name__)

# Пул моделей по убыванию предпочтения. Имя «GigaChat» принадлежало старому API
# (gigachat.devices.sberbank.ru); шлюз api.giga.chat, на который переводит SDK
# 0.2.3, его не знает и отвечает 404 «No such model» — повтор такого не лечит,
# поэтому спрашиваем следующую модель.
#
# Состав проверен на живом ключе, а не по названиям из новостей: `GET /models`
# и по одному обращению. Имен поколения «3» (`GigaChat-3-Ultra`,
# `GigaChat-3-Max`, `GigaChat-3-Lightning`) этот шлюз не знает — 404, то есть
# прежний резерв пула был мёртвым именем: при сбое основной модели контур
# тратил попытку на заведомый 404. Отвечают и измерены: `GigaChat-2`,
# `GigaChat-2-Max`, `GigaChat-Plus`, `GigaChat-Max-preview`,
# `GigaChat-Pro-preview`.
#
# Выбор головой — `GigaChat-2-Max` по числам харнесса (`--mode live`, 8
# сценариев, один повтор): `expected_participants` 0.25 → 0.75,
# `quality/scheme_checks_share` 0.854 → 0.946, `min_steps` 0.875 → 1.0 при
# неизменном бизнес-слое. `GigaChat-Max-preview` дал те же 0.75 по участникам,
# но хуже по бизнес-слою (smells 0.375 против 0.302), а preview-статус
# нестабилен. `GigaChat-2` оставлена резервом: на ней обкатаны промпты.
# Порядок задаёт GIGACHAT_MODEL: одно имя или список через запятую.
DEFAULT_MODEL_POOL = ("GigaChat-2-Max", "GigaChat-2")
DEFAULT_SCOPE = "GIGACHAT_API_PERS"
MAX_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 3
# Отказ по лимиту запросов — не «модель сломалась», а окно провайдера: ему даём
# дополнительные попытки, но с общим бюджетом ожидания, чтобы синхронный запрос
# пользователя не превратился в минутный.
RATE_LIMIT_EXTRA_ATTEMPTS = 3
RATE_LIMIT_WAIT_BUDGET_SECONDS = 20
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


class LLMRequestTooLargeError(LLMError):
    """Запрос корректен по форме, но не помещается в контекст провайдера.

    Отличается от LLMError тем, что повтор бесполезен: вход той же длины.
    Раньше такое падало в общий отказ и роутер отвечал 503 с Retry-After —
    пользователь получал «попробуйте позже» на запрос, который не пройдёт
    никогда, и совет переформулировать то, с чем всё в порядке.
    """


class _LLMModelRejectedError(LLMError):
    """Эту модель провайдер не знает: имя не из его списка.

    Служебный сигнал для пула — наружу не выходит, пока в пуле есть следующая
    модель. От транспортного сбоя отличается ровно тем, что повторять бессмысленно:
    имя в запросе то же.
    """


class LLMBudgetExceededError(LLMError):
    """Исчерпан wall-clock бюджет обращения: провайдер не отказал, он не успел.

    Отличается от LLMRequestTooLargeError тем, что уменьшение входа не помогает,
    а от «сервис лёг» — что время ушло конкретное и названо в сообщении. Без
    различимого класса медленный прогон прятался за «LLM недоступна после N
    попыток», а потребитель без бюджета не отличает «повторить сейчас бесполезно,
    очередь занята» от «провайдер чинится».
    """


# Формулировки отказа «модели нет» тоже разбросаны: часть шлюзов отвечает 400
# вместо 404. Подстроки нижним регистром — по телу ответа.
_UNKNOWN_MODEL_HINTS = ("no such model", "unknown model", "model not found",
                        "нет модели", "несуществующ")

# Формулировки отказов из-за длины разбросаны (GigaChat и OpenAI-совместимые
# шлюзы пишут по-разному). Голый «token»/«токен» признаком быть не может: он
# стоит в честных отказах авторизации («invalid token») и квоты («daily token
# quota exceeded»), и раньше любой 400 со словом про токен становился
# LLMRequestTooLargeError — не ретраится вообще, а пользователь читал
# «сократите описание» при нормальном описании. Признак теперь парный: слово о
# длине/размере В СОЧЕТАНИИ со словом про токены/контекст; маркеры отказа
# доступа и лимита запросов снимают признак даже при обоих словах.
_CONTEXT_SIZE_WORDS = ("length", "too long", "too large", "too many",
                       "maximum", "exceed", "overflow",
                       "длин", "размер", "превыс", "максимальн", "слишком")
_CONTEXT_TOKEN_WORDS = ("context", "контекст", "token", "токен")
_CONTEXT_DENIED_MARKERS = ("quota", "квот", "authorization", "авторизац",
                           "invalid token", "expired", "просрочен",
                           "revoked", "отозван", "api key", "apikey",
                           "too many requests", "слишком много запросов",
                           "rate limit", "лимит запросов", "ключ", "секрет")


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

# Wall-clock бюджет одного обращения (весь `call_json`, включая корректирующий
# разбор и обход пула). Без него MAX_ATTEMPTS × REQUEST_TIMEOUT_SECONDS × число
# моделей в пуле перемножаются, а семафор держит всю очередь пользователей:
# одна медленная схема стояла бы за неё десятками минут. Исчерпание —
# различимая ошибка LLMBudgetExceededError: не «модель не знает» и не
# «не влезло», провайдер жив, но не успел.
CALL_BUDGET_SECONDS = _env_int("GIGACHAT_CALL_BUDGET_SECONDS", 600)

_client: Optional[GigaChat] = None
_client_guard = threading.Lock()


def _verify_ssl() -> bool:
    # GigaChat отдаёт сертификат из российского УЦ: на машине без корневого
    # сертификата ГосУС проверка TLS не проходит, поэтому её можно выключить.
    return os.getenv("GIGACHAT_VERIFY_SSL", "true").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _ca_bundle_file() -> Optional[str]:
    """Путь к корневому сертификату УЦ, если он задан и читается.

    Предыстория: текст подсказки при отключённой проверке называет
    `GIGACHAT_CA_BUNDLE_FILE`, и `.env.example` его документирует, но сам ключ
    не читался никем. Оператор, который последовал совету и смонтировал bundle,
    оставался на системном хранилище: проверка падала ровно тем же
    `CERTIFICATE_VERIFY_FAILED`, из-за которого он её и выключал.
    """
    path = _ca_bundle_declared()
    if not path:
        return None
    if not os.path.isfile(path):
        # Молча принять несуществующий путь значило бы вернуться к той же
        # иллюзии настройки; ронять процесс из-за опции, которой можно и не быть,
        # не за чем.
        logger.warning("GIGACHAT_CA_BUNDLE_FILE=%s: файла нет, проверка TLS идёт "
                       "по системному хранилищу и сертификат российского УЦ "
                       "может не принять", path)
        return None
    return path


def _ca_bundle_declared() -> str:
    """Задан ли ключ вообще, без проверки читабельности файла.

    При отключённой проверке bundle не участвует в транспорте, и спрашивать про
    файл незачем: `_ca_bundle_file()` добавил бы в лог «проверка TLS идёт по
    системному хранилищу» ровно там, где никакой проверки нет.
    """
    return (os.getenv("GIGACHAT_CA_BUNDLE_FILE") or "").strip()


def _warn_transport(verify: bool) -> None:
    """Что именно произойдёт с TLS — словами, которые ему соответствуют.

    `gigachat.client._get_kwargs` (и `_get_auth_kwargs` там же) пишет
    `kwargs["verify"] = settings.ca_bundle_file` ПОСЛЕ `"verify":
    settings.verify_ssl_certs`: переданный bundle перекрывает отключённую
    проверку, и связка `GIGACHAT_VERIFY_SSL=false` + `GIGACHAT_CA_BUNDLE_FILE`
    ходила в сеть с проверкой сертификата, а лог в этот момент утверждал
    обратное («ключ уходит в незащищённый канал»). Лечится не правкой текста
    под поведение SDK, а порядком операндов: bundle отдаётся только когда
    проверка включена, поэтому явный `false` старше bundle и означает именно
    отключение. Порядок выбран в пользу той опции, которую человек задал явно,
    а не той, о которой он мог забыть.
    """
    if verify:
        return
    declared = _ca_bundle_declared()
    if declared:
        logger.warning(
            "GIGACHAT_VERIFY_SSL=false вместе с GIGACHAT_CA_BUNDLE_FILE=%s: "
            "явный false старше, bundle НЕ подключается — проверка TLS "
            "отключена целиком (и в OAuth-обмене, и в запросах к модели), ключ "
            "уходит в незащищённый канал. Чтобы проверка шла против этого "
            "файла, уберите GIGACHAT_VERIFY_SSL или поставьте true. Допустимо "
            "только локально.", declared)
        return
    logger.warning(
        "GIGACHAT_VERIFY_SSL=false: проверка TLS отключена, ключ уходит в "
        "незащищённый канал. Допустимо только локально; для прода — корневой "
        "сертификат российского УЦ через GIGACHAT_CA_BUNDLE_FILE."
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
                verify = _verify_ssl()
                _warn_transport(verify)
                _client = GigaChat(
                    credentials=credentials,
                    scope=os.getenv("GIGACHAT_SCOPE", DEFAULT_SCOPE),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    verify_ssl_certs=verify,
                    # Только при включённой проверке: SDK ставит `verify =
                    # ca_bundle_file` поверх `verify_ssl_certs`, и переданный на
                    # выключенной проверке bundle включил бы её обратно молча.
                    ca_bundle_file=_ca_bundle_file() if verify else None,
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


def _error_text(error: ResponseError) -> str:
    """Тело ответа нижним регистром: битые байты заменяем на читаемый текст —
    нам нужны только подстроки, а не дословный дамп."""
    content = error.content
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return content.casefold() if isinstance(content, str) else ""


def _is_too_large(error: ResponseError) -> bool:
    """Отказ «запрос не влезает»: 413 по размеру тела, 400/422 — по тексту.

    Для текста нужно оба слова: длина/размер И токен/контекст (см.
    `_CONTEXT_SIZE_WORDS`). Тот же BadRequestError приходит и по другим
    причинам, поэтому одиночное «token» решющим быть не перестало: честные
    401/403/429 сюда не доходят ни по классу ответа (ветки проверяются
    раньше), ни по телу (маркеры доступа/квоты снимают признак).
    """
    if isinstance(error, RequestEntityTooLargeError):
        return True
    if not isinstance(error, (BadRequestError, UnprocessableEntityError)):
        return False
    text = _error_text(error)
    if any(marker in text for marker in _CONTEXT_DENIED_MARKERS):
        return False
    return (any(word in text for word in _CONTEXT_TOKEN_WORDS)
            and any(word in text for word in _CONTEXT_SIZE_WORDS))


def _is_unknown_model(error: ResponseError) -> bool:
    """Отказ «модели нет»: 404 по классу ответа, 400/422 — по тексту.

    Отличаем его от транспортного сбоя затем, чтобы не жечь попытку на имени,
    которого у провайдера никогда не было: 404 из лога живого прогона выглядит
    как «503, попробуйте позже» на запрос, который не пройдёт никогда.
    """
    if isinstance(error, NotFoundError):
        return True
    if not isinstance(error, (BadRequestError, UnprocessableEntityError)):
        return False
    text = _error_text(error)
    return any(hint in text for hint in _UNKNOWN_MODEL_HINTS)


def get_model_pool() -> List[str]:
    """Модели по убыванию предпочтения: GIGACHAT_MODEL — имя или список через
    запятую. Пустое значение = пул по умолчанию, чтобы порядок жил в одном месте.
    """
    raw = os.getenv("GIGACHAT_MODEL", "")
    pool = [name.strip() for name in raw.split(",") if name.strip()]
    return pool or list(DEFAULT_MODEL_POOL)


def ensure_llm_ready() -> None:
    """Авторизуется провайдера заранее.

    Вызывается из lifespan: ошибка ключа должна быть видна на старте сервиса,
    а не через минуту после старта первого пользовательского запроса.

    Сбой транспорта сворачивается в `LLMError`, а не уходит наружу: шлюз
    провайдера — чужая доступность, и вызывающая сторона фильтрует именно
    `LLMError`. `httpx.ConnectError` (отбитый TLS-канал, истёкший сертификат,
    недоступный адрес) этим фильтром не ловился и вешал запуск всего сервиса
    вместе с реестром, доступом и апрувами.
    """
    try:
        get_llm_client().get_token()
    except (ResponseError, *_RETRYABLE) as error:
        raise LLMError(f"Авторизация LLM не подтверждена ({type(error).__name__}): "
                       f"{error}") from error


def _content_of(response: Any) -> Optional[str]:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message else None
    return content if isinstance(content, str) else None


_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _usage_dict(usage: Any) -> Dict[str, int]:
    """Числовые поля токенов ответа: SDK отдаёт их объектом или None."""
    out: Dict[str, int] = {}
    for field in _USAGE_FIELDS:
        value = getattr(usage, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            out[field] = value
    return out


def _log_usage(model: str, attempt: int, elapsed_ms: int,
               response: Any) -> Tuple[Optional[str], Dict[str, int]]:
    usage = getattr(response, "usage", None)
    choices = getattr(response, "choices", None)
    finish = getattr(choices[0], "finish_reason", None) if choices else None
    usage_fields = _usage_dict(usage)
    logger.info(
        "Вызов LLM %s (попытка %s): %s мс, токены prompt=%s/completion=%s, finish=%s",
        model, attempt, elapsed_ms,
        usage_fields.get("prompt_tokens", "?"),
        usage_fields.get("completion_tokens", "?"), finish,
    )
    return finish, usage_fields


class _CallContext:
    """Единая точка учёта времени и авторов ответов одного обращения к модели.

    Попытки, паузы на 429 и обход пула складываются здесь, а не пересказываются
    на каждом пути: без общего wall-clock бюджета MAX_ATTEMPTS ×
    REQUEST_TIMEOUT_SECONDS × размер пула перемножаются, и одна медленная схема
    держит глобальный семафор, пока очередь пользователей стоит. Здесь же
    рождается журнал «какая модель ответила и сколько ушло токенов» — раньше
    это таяло в строке лога, и переход пула посреди прогона не оставлял следа
    в результате.
    """

    def __init__(self, seconds: float):
        self.budget = max(0.0, float(seconds))
        self.started = time.monotonic()
        self.model_s = 0.0   # время внутри запросов к провайдеру
        self.wait_s = 0.0    # паузы повторов и ожидания 429
        self.attempts = 0
        self.answers: List[Dict[str, Any]] = []
        self.rejected: List[str] = []

    def remaining(self) -> float:
        return self.started + self.budget - time.monotonic()

    def spent_s(self) -> float:
        return self.model_s + self.wait_s

    def note_attempt(self, elapsed: float) -> None:
        self.attempts += 1
        self.model_s += max(0.0, elapsed)

    def note_wait(self, seconds: float) -> None:
        self.wait_s += max(0.0, seconds)

    def note_answer(self, model: str, usage: Dict[str, int],
                    elapsed: float) -> None:
        self.answers.append({"model": model, "usage": dict(usage),
                             "elapsed_ms": int(elapsed * 1000)})

    def note_rejected(self, model: str) -> None:
        self.rejected.append(model)

    def check(self, tried: str = "") -> None:
        """Бюджет исчерпан — дальше стоять в очереди к провайдеру нельзя.

        Проверяется до каждой попытки и перед переходом к следующей модели:
        начатый запрос мы не прерываем (транспортный таймаут у него свой), но
        новый при нуле остатка уже не отправляем.
        """
        if self.remaining() > 0:
            return
        details = f" ({tried})" if tried else ""
        raise LLMBudgetExceededError(
            f"Обращение к модели не уложилось в бюджет {int(self.budget)} c: "
            f"{self.attempts} попыток, {self.spent_s():.0f} c суммарного "
            f"ожидания{details}. Это не «модель не знает ответа» и не размер "
            "запроса: провайдер не ответил за отведённое время — повторите "
            "позже или уменьшите параллельность "
            "(GIGACHAT_MAX_CONCURRENT/GIGACHAT_CALL_BUDGET_SECONDS)."
        )


_CALL_CTX = threading.local()


@contextlib.contextmanager
def _call_scope():
    """Контекст одного обращения: `call_json` открывает его один раз на весь
    вызов (включая корректирующий разбор), одиночный `_complete` — на себя.
    Вложенный вызов наследует бюджет и журнал внешнего: повтор разбора не
    должен получать «свежие» минуты сверх очередь."""
    previous = getattr(_CALL_CTX, "active", None)
    if previous is not None:
        yield previous
        return
    ctx = _CallContext(CALL_BUDGET_SECONDS)
    _CALL_CTX.active = ctx
    try:
        yield ctx
    finally:
        _CALL_CTX.active = None


def fill_call_trace(trace: Dict[str, Any], ctx: _CallContext) -> None:
    """Журнал обращения в словарь вызывающей стороны: имя ответившей модели на
    каждый вызов, отвергнутые 404-ми имена, суммарный usage и время. Наружу
    уходит копия — `_CallContext` остаётся служебным."""
    trace["models"] = [answer["model"] for answer in ctx.answers]
    trace["rejected_models"] = list(ctx.rejected)
    trace["calls"] = [dict(answer) for answer in ctx.answers]
    usage: Dict[str, int] = {}
    for answer in ctx.answers:
        for field, value in answer["usage"].items():
            usage[field] = usage.get(field, 0) + value
    trace["usage"] = usage
    trace["attempts"] = ctx.attempts
    trace["spent_s"] = round(ctx.spent_s(), 3)


def _complete(messages: List[Dict[str, str]], temperature: float,
              max_tokens: Optional[int]) -> str:
    """Обход пула моделей: спрашиваем следующую, только когда провайдер не знает
    текущую.

    Транспортный сбой (5xx, 429, сеть) пул не перебирает: та же ошибка на
    каждой модели означала бы прогон всех попыток впустую — наружу идёт
    отказ от первой. Отвергнутые имена и ответившую модель пишет в журнал
    обращения: схема, собранная после mid-run 404 с переходом на другую
    модель, обязана нести этот след в отчёте, а не выглядеть ответом одной.
    """
    with _call_scope() as ctx:
        pool = get_model_pool()
        for index, model in enumerate(pool):
            if index:
                ctx.check("испробованы модели: " + ", ".join(pool[:index]))
            try:
                return _complete_with_model(ctx, model, messages, temperature,
                                            max_tokens)
            except _LLMModelRejectedError as e:
                ctx.note_rejected(model)
                if index + 1 == len(pool):
                    raise LLMError(
                        f"Ни одна из моделей пула ({', '.join(pool)}) "
                        f"провайдеру неизвестна: {e}. Проверьте GIGACHAT_MODEL — "
                        "список доступных имён отдаёт GET /v1/models."
                    ) from e
                logger.warning("Модель %s провайдер не знает — перехожу на %s",
                               model, pool[index + 1])
        raise LLMError("Пул моделей пуст")  # get_model_pool гарантирует непустоту


def _complete_with_model(ctx: _CallContext, model: str,
                         messages: List[Dict[str, str]],
                         temperature: float,
                         max_tokens: Optional[int]) -> str:
    """Один раунд с ограниченными повторами. Только транспортные повторы
    (429/5xx/сеть); смысловые ошибки ответа повторяет вызывающий код, а отказ
    «не влезло в контекст» поднимается сразу — повтор его не лечит."""
    from gigachat.models import Chat, Messages

    request = Chat(
        model=model,
        messages=[Messages(role=m["role"], content=m["content"]) for m in messages],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    last_error = ""
    pause = RETRY_PAUSE_SECONDS
    attempt, extra_left, rate_streak = 0, RATE_LIMIT_EXTRA_ATTEMPTS, 0
    waited = 0.0
    # Потолком правит 429: без него повтор не положен ни одной ошибке кроме
    # лимитной, и «растущий» цикл превратил бы 5xx в бесконечный.
    limit = MAX_ATTEMPTS
    while attempt < limit:
        # Общее время обращения перевешивает локальные счётчики попыток:
        # бюджет кончился — ждать дальше не за чем, и очередь из-за нас тоже.
        ctx.check(f"модель {model}, попыток {attempt}")
        attempt += 1
        if attempt > 1:
            ctx.note_wait(pause)
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
            if _is_unknown_model(e):
                raise _LLMModelRejectedError(
                    f"провайдер не знает модель {model!r}: {e}"
                ) from e
            if isinstance(e, RateLimitError):
                last_error = str(e)
                if waited >= RATE_LIMIT_WAIT_BUDGET_SECONDS:
                    break
                # Окно провайера шире, чем одна пауза: 429 на втором вызове
                # означал бы «схема не собралась», хотя модель ничего не
                # испортила — прогон #54 потерял так 5 кейсов из 24 и молча
                # поменял выборку всех долей. Дополнительная попытка оплачивается
                # самим лимитом, а пауза растёт экспоненциально, но в бюджете:
                # синхронный ответ пользователя не должен превращаться в минуту.
                if extra_left > 0:
                    extra_left -= 1
                    rate_streak += 1
                    limit += 1
                pause = min(max(e.retry_after,
                                RETRY_PAUSE_SECONDS * (2 ** rate_streak)),
                            RATE_LIMIT_WAIT_BUDGET_SECONDS - waited)
                # и в общий wall-clock бюджет тоже: пауза не имеет права
                # пережить дедлайн обращения
                pause = max(0.0, min(pause, ctx.remaining()))
                waited += pause
                logger.warning("Попытка %s: лимит запросов (%s), пауза %s с",
                               attempt, last_error, pause)
                continue
            if isinstance(e, _RETRYABLE):
                last_error = str(e)
                logger.warning("Попытка %s: ошибка обращения к LLM: %s",
                               attempt, last_error)
                continue
            if _is_too_large(e):
                raise LLMRequestTooLargeError(
                    "Запрос не помещается в контекст GigaChat "
                    f"(HTTP {e.status_code}): {e}. Повтор бесполезен — "
                    "нужно уменьшать вход (схему, инвентарь, историю)."
                ) from e
            raise LLMError(f"LLM отклонила запрос: {e}") from e
        except _RETRYABLE as e:
            last_error = str(e)
            logger.warning("Попытка %s: ошибка обращения к LLM: %s", attempt, last_error)
            continue
        finally:
            ctx.note_attempt(time.monotonic() - started)
        elapsed = time.monotonic() - started
        finish, usage = _log_usage(model, attempt, int(elapsed * 1000), response)
        if finish == "length":
            raise LLMTruncatedError(
                "Ответ модели обрезан по лимиту токенов — структура неполная"
            )
        content = _content_of(response)
        if not content:
            last_error = "пустой ответ модели"
            logger.warning("Попытка %s: %s", attempt, last_error)
            continue
        ctx.note_answer(model, usage, elapsed)
        return content
    raise LLMError(f"LLM недоступна после {attempt} попыток "
                   f"({ctx.spent_s():.0f} с суммарного ожидания): {last_error}")


def call_json(system: str, user: str, *, temperature: float = 0.2,
              max_tokens: Optional[int] = 16000,
              trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """JSON-ответ модели с устойчивым извлечением.

    Ошибка разбора повторяется один раз с указанием проблемы — это единственная
    «смысловая» повторная попытка в системе.

    `trace` — необязательный словарь вызывающей стороны: в него ложится журнал
    обращения (`models`, `rejected_models`, `usage`, `attempts`, `spent_s`) по
    обеим попыткам разбора вместе. Без него имя ответившей модели и токены
    живут только в логе, а генерация/улучшение не могут отдать их в результате —
    переход пула посреди прогона делал бы схему ответом другой модели без следа.
    """
    with _call_scope() as ctx:
        try:
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
        finally:
            if trace is not None:
                fill_call_trace(trace, ctx)


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


_JSON_STRING_RE = r'"(?:[^"\\]|\\.)*"'
# Конец значения, за которым сразу идёт следующий токен: модель то и дело теряет
# запятую между элементами или двоеточие между ключом и значением. Левая часть —
# строка или закрывающая скобка (`] "elements": …` — живой случай 2026-09-22).
_VALUE_END = r"(?:" + _JSON_STRING_RE + r"|[}\]])"
_TOKEN_START = r"(?:\"|[{\[-]|\btrue\b|\bfalse\b|\bnull\b|\d)"
_NO_COMMA = re.compile(r"(" + _VALUE_END + r")(\s+)(?=" + _TOKEN_START + r")")
_NO_COLON = re.compile(r"(" + _JSON_STRING_RE + r")(\s+)(?=" + _TOKEN_START + r")")
_DANGLING_COMMA = re.compile(r",(\s*[}\]])")


def _heal_json(text: str) -> List[Tuple[str, str]]:
    """Варианты ответа с пропущенным разделителем.

    Чинится только разделитель: ключи, значения и порядок полей остаются те же,
    ничего не додумывается. Запятая пробуется раньше двоеточия — два значения
    рядом в массиве это два значения, а не «ключ: значение», — и каждый вариант
    всё равно проверяет `json.loads`: «починка» не вправе превратить мусор в
    план.
    """
    out: List[Tuple[str, str]] = []
    commas = _NO_COMMA.sub(r"\1, ", text)
    if commas != text:
        out.append(("пропущена запятая", commas))
    colons = _NO_COLON.sub(r"\1: ", text)
    if colons != text:
        out.append(("пропущено двоеточие", colons))
    trailing = _DANGLING_COMMA.sub(r"\1", text)
    if trailing != text:
        out.append(("висячая запятая", trailing))
    return out


def _json_variants(text: str):
    """Порядок разбора: дословный ответ, починки разделителей, и только потом
    дозакрывание оборванного хвоста.

    Дозакрытие — последний аргумент, а не первый: оно меняет состав ответа,
    тогда как починка разделителя меняет только синтаксис.
    """
    yield "как есть", text
    yield from _heal_json(text)
    yield "усечение", _close_truncated(text)


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
        for name, variant in _json_variants(candidate):
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
            if name == "усечение":
                # Разобрали только то, что довели до конца: ответ оборвался.
                # Молча вернуть урезанный объект — значит применить план,
                # которого модель не досказала.
                raise LLMTruncatedError(
                    "JSON ответа неполон: пришлось дозакрывать структуру — "
                    "вероятно, ответ обрезан по лимиту токенов"
                )
            if name != "как есть":
                logger.warning("JSON ответа починен: %s", name)
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

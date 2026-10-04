"""Парсинг «непослушных» ответов LLM: блоки рассуждений, оглавления кода,
оборванный JSON, BPMN с префиксом и без него.

Регрессии, из-за которых обновление схем не работало никогда: парсер ожидал
только префикс bpmn: и молча отдавал мусор, когда модель оборачивала ответ в
```xml или писала пространство имён по умолчанию.

Вторая часть файла — разбор отказов провайдера: «запрос не влез в контекст»
не должен выглядеть как «провайдер недоступен». Транспорт подменён на уровне
клиента GigaChat, поэтому сети и ключа не нужно."""
import json
from types import SimpleNamespace

import pytest
from gigachat.exceptions import (BadRequestError, RequestEntityTooLargeError,
                                 ServerError, UnprocessableEntityError)

from core import llm_client
from core.llm_client import (LLMBudgetExceededError, LLMError,
                             LLMRequestTooLargeError, LLMTruncatedError,
                             extract_json, extract_xml)

CHAT_URL = "https://gigachat-api.sberbank.ru/v1/chat/completions"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


class FakeProvider:
    """Клиент провайдера, у которого `chat` бросает заготовленные ошибки.

    Последняя ошибка отдаётся на всех остальных вызовах: так видно число
    попыток."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        # Какие имена моделей реально ушли в запросе: без этого переход по пулу
        # неотличим от повтора того же запроса.
        self.models: list = []

    def chat(self, request):
        self.calls += 1
        self.models.append(getattr(request, "model", None))
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(content, usage=None):
    """Ответ, похожий на то, что отдаёт SDK: choices[0].message.content."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                 finish_reason=None)],
        usage=usage,
    )


USAGE = SimpleNamespace(prompt_tokens=120, completion_tokens=45,
                        total_tokens=165)


def _provider(monkeypatch, *outcomes) -> FakeProvider:
    """Ставит подмену транспорта и убирает паузы повторов из теста."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: provider)
    monkeypatch.setattr(llm_client, "RETRY_PAUSE_SECONDS", 0)
    return provider


def _response_error(cls, status_code: int, content, url: str = CHAT_URL):
    body = content.encode() if isinstance(content, str) else content
    return cls(url, status_code, body, None)


def _complete() -> str:
    return llm_client._complete([{"role": "user", "content": "собери схему"}],
                                0.2, 1000)


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"analysis": "ок", "operations": []}') == {
            "analysis": "ок",
            "operations": [],
        }

    def test_think_block_and_prose_are_ignored(self):
        raw = (
            "<think>Рассуждаю: нужно добавить проверку оплаты.</think>\n"
            "Вот результат:\n"
            '{"analysis": "добавлена проверка", "operations": [{"op": "rename", "id": "T1", "name": "Проверить"}]}'
        )
        result = extract_json(raw)
        assert result["analysis"] == "добавлена проверка"
        assert result["operations"][0]["op"] == "rename"

    def test_code_fence_with_language(self):
        raw = '```json\n{"analysis": "a", "operations": []}\n```'
        assert extract_json(raw) == {"analysis": "a", "operations": []}

    def test_code_fence_without_language(self):
        raw = '```\n{"analysis": "a", "operations": []}\n```'
        assert extract_json(raw) == {"analysis": "a", "operations": []}

    def test_braces_inside_strings_do_not_break_boundaries(self):
        raw = '{"analysis": "ветка {друг} сломана", "operations": []}'
        assert extract_json(raw)["analysis"] == "ветка {друг} сломана"

    def test_truncated_tail_is_not_applied_as_a_plan(self):
        """Дозакрытый JSON = обрезанный ответ: план применять нельзя."""
        raw = (
            '{"analysis": "готово", "operations": '
            '[{"op": "add_task", "id": "new_T9", "name": "Проверить оплат'
        )
        with pytest.raises(LLMTruncatedError):
            extract_json(raw)

    def test_dangling_key_without_value_is_not_silently_accepted(self):
        raw = '{"analysis": "x", "operations": [{"op": "rename"}, {"'
        with pytest.raises(LLMTruncatedError):
            extract_json(raw)

    def test_unclosed_container_is_truncation(self):
        with pytest.raises(LLMTruncatedError):
            extract_json('{"a": 1, "b": [2, 3')

    def test_close_truncated_helper_still_repairs_for_callers(self):
        """Сам механизм дозакрытия остаётся: он нужен для диагностики и
        повторного разбора, просто наружу обрезанный ответ не выходит."""
        from core.llm_client import _close_truncated
        assert json.loads(_close_truncated('{"a": 1, "b": [2, 3')) == {"a": 1, "b": [2, 3]}

    def test_escaped_quote_inside_string(self):
        raw = '{"analysis": "модель сказала \\"нет\\"", "operations": []}'
        assert extract_json(raw)["analysis"] == 'модель сказала "нет"'

    def test_array_root_rejected(self):
        with pytest.raises(ValueError):
            extract_json('[{"op": "rename"}]')

    def test_no_json_at_all(self):
        with pytest.raises(ValueError):
            extract_json("я не вернул JSON, извините")

    def test_empty_answer(self):
        with pytest.raises(ValueError):
            extract_json("")

    def test_missing_colon_between_key_and_value_is_healed(self):
        """Живой прогон 2026-09-22: генерация упала целиком из-за одного
        пропущенного двоеточия на 3153-м символе («Expecting ':' delimiter»).
        Ключи и значения в таком ответе на месте — терять схему из-за
        синтаксиса нельзя."""
        raw = '{"participants": ["ВкусВилл"] "elements": []}'
        assert extract_json(raw) == {"participants": ["ВкусВилл"],
                                     "elements": []}

    def test_missing_comma_in_array_stays_an_array(self):
        """Пропущенный разделитель чинится запятой раньше, чем двоеточием: в
        массиве два элемента рядом — это два элемента, а не «ключ: значение»."""
        raw = '{"steps": ["Собрать" "Отгрузить"]}'
        assert extract_json(raw)["steps"] == ["Собрать", "Отгрузить"]

    def test_key_without_colon_falls_back_to_colon_heal(self):
        """Где запятая не спасает (объект, а не массив), принимается вариант с
        двоеточием — оба проверяются разбором, угадывать не приходится."""
        assert extract_json('{"participants": [], "actors" ["ВкусВилл"]}') == {
            "participants": [], "actors": ["ВкусВилл"]}

    def test_dangling_comma_before_close_is_healed(self):
        assert extract_json('{"a": 1, "b": [2,],}') == {"a": 1, "b": [2]}

    def test_valid_answer_is_untouched(self):
        raw = '{"analysis": "ветки \\"a\\" \\"b\\" сошлись", "operations": []}'
        assert extract_json(raw)["analysis"] == 'ветки "a" "b" сошлись'

    def test_unrepairable_answer_still_fails(self):
        """Починка не вправе превращать мусор в план: синтаксис чиним, а
        содержание — нет."""
        with pytest.raises(ValueError):
            extract_json('{"a": , }')

    def test_truncated_answer_is_still_reported_as_truncated(self):
        with pytest.raises(LLMTruncatedError):
            extract_json('{"participants": ["ВкусВилл", "Поставщик", "Склад')


class TestExtractXml:
    def test_default_namespace_form(self, single_pool_xml):
        extracted = extract_xml(single_pool_xml)
        # Декларация отбрасывается: consumer (bpmn_edits) всегда дописывает свою.
        assert extracted.startswith("<definitions")
        assert extracted.endswith("</definitions>")
        assert "T_collect" in extracted

    def test_prefixed_form(self):
        raw = (
            '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">'
            '<bpmn:process id="P1"/>'
            '</bpmn:definitions>'
        )
        assert extract_xml(raw) == raw

    def test_fenced_and_explained(self, single_pool_xml):
        raw = (
            "<think>думаю</think>\n"
            "Вот схема:\n```xml\n" + single_pool_xml + "\n```\n"
            "Как видите, процесс полный."
        )
        extracted = extract_xml(raw)
        assert extracted.startswith("<definitions")
        assert extracted.endswith("</definitions>")
        assert "Как видите" not in extracted

    def test_text_before_and_after_is_cut(self):
        raw = (
            "Вот definitions:\n<definitions xmlns=\"http://www.omg.org/spec/BPMN/20100524/MODEL\">"
            "<process id=\"P1\"/></definitions>\nИ всё, конец объяснений."
        )
        extracted = extract_xml(raw)
        assert extracted.startswith("<definitions")
        assert extracted.endswith("</definitions>")

    def test_missing_close_tag_raises(self):
        with pytest.raises(ValueError):
            extract_xml('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">')

    def test_no_definitions_raises(self):
        with pytest.raises(ValueError):
            extract_xml("схемы нет")

    def test_empty_answer_raises(self):
        with pytest.raises(ValueError):
            extract_xml("")


class TestProviderRefusals:
    """Что `_complete` повторяет, а что отдаёт наружу сразу.

    Главная регрессия: отказ «схема не влезла в контекст» не должен притворяться
    недоступностью провайдера — иначе роутер отвечает 503 с Retry-After, а
    пользователь бесконечно пересылает заведомо непроходимый запрос."""

    def test_413_is_too_large_and_never_retried(self, monkeypatch):
        provider = _provider(
            monkeypatch,
            _response_error(RequestEntityTooLargeError, 413, b"Request body is too large"))
        with pytest.raises(LLMRequestTooLargeError) as caught:
            _complete()
        assert provider.calls == 1
        # Наследник LLMError: потребители, которые ловили только его, не ломаются.
        assert isinstance(caught.value, LLMError)

    def test_400_context_length_message_is_too_large(self, monkeypatch):
        _provider(monkeypatch, _response_error(
            BadRequestError, 400,
            '{"error": {"message": "This model\'s maximum context length is 128000 '
            'tokens, however you requested 310000 tokens"}}'))
        with pytest.raises(LLMRequestTooLargeError):
            _complete()

    def test_422_russian_message_about_context_is_too_large(self, monkeypatch):
        _provider(monkeypatch, _response_error(
            UnprocessableEntityError, 422,
            "Превышена максимально допустимая длина контекста".encode("utf-8")))
        with pytest.raises(LLMRequestTooLargeError):
            _complete()

    def test_400_unrelated_body_stays_plain_llm_error(self, monkeypatch):
        _provider(monkeypatch, _response_error(
            BadRequestError, 400, '{"error": {"message": "unknown field temperature_x"}}'))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)

    def test_400_without_readable_body_stays_plain_llm_error(self, monkeypatch):
        # content бывает None или битыми байтами: разбор не должен падать на этом.
        _provider(monkeypatch,
                  _response_error(BadRequestError, 400, None),
                  _response_error(BadRequestError, 400, b"\xff\xfe\x00context"))
        with pytest.raises(LLMError):
            _complete()
        with pytest.raises(LLMError):
            _complete()

    def test_oauth_400_about_token_stays_auth_failure(self, monkeypatch):
        """Проверка порядка: 400 с «token» в теле — отказ авторизации, а не
        размер запроса; иначе слишком большой вход замаскируется под него."""
        _provider(monkeypatch, _response_error(
            BadRequestError, 400, "invalid token: authorization failed", url=OAUTH_URL))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)
        assert "авторизаци" in str(caught.value).lower()

    def test_5xx_is_retried_and_ends_as_llm_error(self, monkeypatch):
        provider = _provider(monkeypatch,
                             _response_error(ServerError, 503, b"upstream unavailable"))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert provider.calls == llm_client.MAX_ATTEMPTS
        assert not isinstance(caught.value, LLMRequestTooLargeError)

    def test_rate_limit_gets_the_extra_attempts_a_5xx_never_gets(self, monkeypatch):
        """429 — окно провайдера, а не отказ модели: давать ему больше попыток
        можно только по нему. Живой прогон #54 потерял 5 кейсов из 24 именно
        потому, что после двух попыток генерация считалась несобранной, — и
        молча посчитал метрики по уцелевшей выборке."""
        provider = _provider(monkeypatch, _response_error(
            llm_client.RateLimitError, 429, b'{"status":429,"message":"Too Many"}'))
        with pytest.raises(LLMError):
            _complete()
        assert provider.calls == (llm_client.MAX_ATTEMPTS
                                  + llm_client.RATE_LIMIT_EXTRA_ATTEMPTS)

    def test_rate_limit_recovers_as_soon_as_the_window_opens(self, monkeypatch):
        provider = _provider(monkeypatch, _response_error(
            llm_client.RateLimitError, 429, b"too many"), _response("{\"ok\": true}"))
        assert llm_client.call_json("система", "запрос") == {"ok": True}
        assert provider.calls == 2

    def test_rate_limit_waits_never_exceed_the_budget(self, monkeypatch):
        """Экспоненциальная пауза обязана упираться в бюджет, а не в минуту
        ожидания: задержка синхронного ответа пользователя — тоже продукт."""
        waits: list = []
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: waits.append(s))
        _provider(monkeypatch, _response_error(
            llm_client.RateLimitError, 429, b"too many"))
        with pytest.raises(LLMError):
            _complete()
        assert sum(waits) <= llm_client.RATE_LIMIT_WAIT_BUDGET_SECONDS + 1

    def test_repair_round_does_not_mask_too_large(self, monkeypatch):
        """Repair-цикл `call_json` ловит только ValueError разбора: отказ
        провайдера по размеру обязан пройти сквозь него наружу."""
        provider = _provider(
            monkeypatch, _response("модель сказала слово без JSON"),
            _response_error(BadRequestError, 400, "слишком длинный запрос, превышен контекст"))
        with pytest.raises(LLMRequestTooLargeError):
            llm_client.call_json("система", "запрос")
        assert provider.calls == 2


class TestModelPool:
    """Пул имён моделей: следующего просят только когда провайдер не знает текущего.

    Живой случай 2026-09-23: образ поднял `gigachat` 0.2.3, у которого дефолтный
    шлюз — api.giga.chat, а имени «GigaChat» там нет. Запрос падал в 404
    «No such model», роутер отвечал 503 с Retry-After, и ИИ-контур выглядел
    лежащим, хотя имя модели было попросту устаревшим.
    """

    POOL = "GigaChat,GigaChat-2"
    # Имена, на которые шлюз отвечал при замере живым ключом (первичная
    # проверка 2026-10-02, перепроверка `GET /models` 2026-10-04): список тот же,
    # `GigaChat` в нём живёт на нынешнем шлюзе — рассказ про 404 ниже относится к
    # дефолтному хосту библиотеки `gigachat` 0.2.3 (api.giga.chat), а не к этому.
    # Поколения «3» в выдаче нет до сих пор: `GigaChat-3-Ultra`, `GigaChat-3-Max`,
    # `GigaChat-3-Lightning` — 404 «No such model», свежее доступное — семейство «2».
    ANSWERING = frozenset({
        "GigaChat", "GigaChat-2", "GigaChat-2-Max", "GigaChat-2-Pro",
        "GigaChat-Plus", "GigaChat-Pro", "GigaChat-Max",
        "GigaChat-Pro-preview", "GigaChat-Max-preview",
    })

    def test_empty_env_falls_back_to_the_default_pool(self, monkeypatch):
        """Пустая переменная — не «модель с пустым именем», а пул из кода:
        порядок имён живёт в одном месте, а не в трёх конфигах."""
        monkeypatch.setenv("GIGACHAT_MODEL", "")
        assert llm_client.get_model_pool() == list(llm_client.DEFAULT_MODEL_POOL)
        assert all(llm_client.DEFAULT_MODEL_POOL), "в пуле не может быть пустых имён"

    def test_default_pool_names_only_models_the_gateway_answers(self):
        """В пуле по умолчанию не может быть имени, которого шлюз не знает:
        резерв из такого имени — не резерв, а снятая попытка на заведомом 404
        (так и жил `GigaChat-3-Lightning`). Имя добавляется в пул только после
        проверки живым ключом — список ниже от неё и остался."""
        unknown = [name for name in llm_client.DEFAULT_MODEL_POOL
                   if name not in self.ANSWERING]
        assert not unknown, ("пул называет модели, которых нет на шлюзе: "
                             f"{unknown} — сверьтесь с `GET /models`")
        assert len(set(llm_client.DEFAULT_MODEL_POOL)) == len(
            llm_client.DEFAULT_MODEL_POOL), "повтор имени тратит попытку впустую"

    def test_env_pool_is_comma_separated_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_MODEL", " GigaChat-2-Max , GigaChat-2,")
        assert llm_client.get_model_pool() == ["GigaChat-2-Max", "GigaChat-2"]

    def test_unknown_model_name_asks_the_next_one(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_MODEL", self.POOL)
        provider = _provider(
            monkeypatch,
            _response_error(llm_client.NotFoundError, 404,
                            b'{"status":404,"message":"No such model"}'),
            _response('{"ok": true}'))
        assert llm_client.call_json("система", "запрос") == {"ok": True}
        assert provider.models == ["GigaChat", "GigaChat-2"]

    def test_400_about_a_missing_model_walks_the_pool_too(self, monkeypatch):
        """Часть шлюзов зовёт отсутствие модели не 404, а 400 — читаем тело."""
        monkeypatch.setenv("GIGACHAT_MODEL", self.POOL)
        provider = _provider(
            monkeypatch,
            _response_error(BadRequestError, 400, '{"message": "Unknown model"}'),
            _response('{"ok": true}'))
        assert llm_client.call_json("система", "запрос") == {"ok": True}
        assert provider.models == ["GigaChat", "GigaChat-2"]

    def test_exhausted_pool_blames_the_config_not_an_outage(self, monkeypatch):
        """Сообщение обязано называть и пул, и переменную: «LLM недоступна» на
        опечатку в имени модели — это ещё один ложный 503."""
        monkeypatch.setenv("GIGACHAT_MODEL", self.POOL)
        provider = _provider(
            monkeypatch,
            _response_error(llm_client.NotFoundError, 404, b'{"message":"No such model"}'))
        with pytest.raises(LLMError) as caught:
            _complete()
        text = str(caught.value)
        assert "GigaChat" in text and "GigaChat-2" in text
        assert "GIGACHAT_MODEL" in text
        # Ни одной лишней попытки: 404 не транспортный сбой, повторять нечего.
        assert provider.calls == 2

    def test_transport_failure_does_not_walk_the_pool(self, monkeypatch):
        """5xx на первой модели — не повод спрашивать вторую: та же ошибка на
        каждой из них умножила бы ожидание пользователя на размер пула."""
        monkeypatch.setenv("GIGACHAT_MODEL", self.POOL)
        provider = _provider(monkeypatch, _response_error(ServerError, 503,
                                                          b"upstream unavailable"))
        with pytest.raises(LLMError):
            _complete()
        assert provider.models == ["GigaChat"] * llm_client.MAX_ATTEMPTS

    def test_auth_failure_does_not_walk_the_pool(self, monkeypatch):
        """Отказ авторизации проверяется раньше пула: иначе протухший токен
        выглядел бы как «нет такой модели»."""
        monkeypatch.setenv("GIGACHAT_MODEL", self.POOL)
        provider = _provider(monkeypatch, _response_error(
            llm_client.AuthenticationError, 401, "token expired", url=CHAT_URL))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert "авторизаци" in str(caught.value).lower()
        assert provider.models == ["GigaChat"]


class TestContextVsAccessRefusals:
    """Признак «не влезло в контекст» обязан быть парным: длина/размер + слово
    про токены/контекст. Голый «token» в теле 400 раньше превращал отказ
    авторизации и квоты в LLMRequestTooLargeError — неретраируемый, с советом
    «сократите описание» при нормальном описании."""

    def test_400_token_quota_message_is_not_too_large(self, monkeypatch):
        provider = _provider(monkeypatch, _response_error(
            BadRequestError, 400,
            '{"error": {"message": "Daily token quota exceeded for your plan"}}'))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)
        # отказ не размерный и не транспортный: общего ретрая нет
        assert provider.calls == 1

    def test_400_invalid_token_off_oauth_is_not_too_large(self, monkeypatch):
        """400 «неверный токен» не на oauth-адресе — не авторизационная ветка,
        и тем более не размер входа: ретраить его, конечно, тоже не нужно,
        но классификация обязана быть честной."""
        _provider(monkeypatch, _response_error(
            BadRequestError, 400, "Invalid token provided"))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)

    def test_403_forbidden_about_token_maximum_is_not_too_large(self, monkeypatch):
        """403 с формулировкой про «maximum tokens» — доступ, а не размер:
        маркер отказа снимает признак, даже когда оба слова на месте."""
        _provider(monkeypatch, _response_error(
            llm_client.ForbiddenError, 403,
            "access denied: your organization token maximum exceeded"))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)
        assert "авторизаци" in str(caught.value).lower()

    def test_401_about_expired_token_is_never_too_large(self, monkeypatch):
        _provider(monkeypatch, _response_error(
            llm_client.AuthenticationError, 401,
            "token expired, request had too many tokens"))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)

    def test_429_about_token_limit_stays_rate_limit(self, monkeypatch):
        """429 с токенами в теле — окно провайдера: ему положены дополнительные
        попытки, а не вечный отказ «уменьшите вход»."""
        provider = _provider(monkeypatch, _response_error(
            llm_client.RateLimitError, 429,
            b'{"message":"Tokens per minute limit exceeded, too many tokens"}'))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)
        assert provider.calls == (llm_client.MAX_ATTEMPTS
                                  + llm_client.RATE_LIMIT_EXTRA_ATTEMPTS)

    def test_400_size_words_without_token_word_is_not_too_large(self, monkeypatch):
        """«слишком большое» без слова про токены/контекст — общая ошибка
        запроса: размер тела лечится не «сократите описание», а другим
        транспортом, и 413 для этого имеет свой класс."""
        _provider(monkeypatch, _response_error(
            BadRequestError, 400, '{"message": "payload is too large"}'))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMRequestTooLargeError)

    def test_400_too_many_tokens_is_still_too_large(self, monkeypatch):
        """Живая формулировка GigaChat про переполненный контекст обязана
        остаться в классе too-large: повтор здесь правда бесполезен."""
        provider = _provider(monkeypatch, _response_error(
            BadRequestError, 400,
            "This message contains too many tokens for the selected model"))
        with pytest.raises(LLMRequestTooLargeError):
            _complete()
        assert provider.calls == 1


def _fake_clock(monkeypatch):
    """`monotonic` и `sleep` двигают одни часы: wall-clock бюджет проверяется
    без реального ожидания."""
    now = {"t": 1000.0}
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(llm_client.time, "sleep",
                        lambda s: now.update(t=now["t"] + s))
    return now


class SteppingProvider(FakeProvider):
    """Провайдер, каждый вызов к которому сдвигает фейковые часы на `step` с."""

    def __init__(self, *outcomes, step, clock):
        super().__init__(*outcomes)
        self.step = step
        self.clock = clock

    def chat(self, request):
        self.clock["t"] += self.step
        return super().chat(request)


def _stepping_provider(monkeypatch, clock, step, *outcomes):
    provider = SteppingProvider(*outcomes, step=step, clock=clock)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: provider)
    monkeypatch.setattr(llm_client, "RETRY_PAUSE_SECONDS", 0)
    return provider


class TestCallBudget:
    """Общий wall-clock бюджет обращения вместо перемножения попыток, таймаутов
    и пула моделей: семафор глобальный, и без дедлайна одна медленная схема
    держит очередь всех остальных пользователей."""

    def test_exhausted_budget_stops_before_the_first_attempt(self, monkeypatch):
        provider = _provider(monkeypatch, _response('{"ok": true}'))
        monkeypatch.setattr(llm_client, "CALL_BUDGET_SECONDS", 0)
        with pytest.raises(LLMBudgetExceededError) as caught:
            _complete()
        assert provider.calls == 0
        # различимая ошибка: не «модель не знает» и не «не влезло»
        assert not isinstance(caught.value, LLMRequestTooLargeError)
        assert not isinstance(caught.value, LLMTruncatedError)
        assert "бюджет" in str(caught.value)

    def test_budget_comes_from_environment(self, monkeypatch):
        monkeypatch.setenv("GIGACHAT_CALL_BUDGET_SECONDS", "45")
        assert llm_client._env_int("GIGACHAT_CALL_BUDGET_SECONDS", 600) == 45

    def test_budget_spans_the_repair_round_of_one_call(self, monkeypatch):
        """Корректирующий разбор — то же обращение, а не новое: вторая попытка
        обязана влезать в общий бюджет первой, иначе «один вызов» незаметно
        удваивает ожидание очереди."""
        clock = _fake_clock(monkeypatch)
        provider = _stepping_provider(monkeypatch, clock, 160,
                                      _response("не JSON, извините"))
        monkeypatch.setattr(llm_client, "CALL_BUDGET_SECONDS", 150)
        with pytest.raises(LLMBudgetExceededError):
            llm_client.call_json("система", "запрос")
        assert provider.calls == 1

    def test_budget_stops_the_pool_walk_after_the_rejected_model(self, monkeypatch):
        """Семантика пула (404 — спросить следующую) сохраняется, пока есть
        время; при нуле остатка следующая модель не зовётся, и отказ называет
        испробованные имена."""
        clock = _fake_clock(monkeypatch)
        monkeypatch.setenv("GIGACHAT_MODEL", "GigaChat,GigaChat-2")
        provider = _stepping_provider(
            monkeypatch, clock, 120,
            _response_error(llm_client.NotFoundError, 404,
                            b'{"message":"No such model"}'))
        monkeypatch.setattr(llm_client, "CALL_BUDGET_SECONDS", 100)
        with pytest.raises(LLMBudgetExceededError) as caught:
            _complete()
        assert provider.models == ["GigaChat"]
        assert "GigaChat" in str(caught.value)

    def test_transport_retry_failure_names_the_spent_time(self, monkeypatch):
        """Время попыток суммируется в одном месте и показывается в отказе:
        «LLM недоступна» без секунды ожидания неотличимо от быстрого отказа."""
        clock = _fake_clock(monkeypatch)
        _stepping_provider(monkeypatch, clock, 30,
                           _response_error(ServerError, 503, b"upstream down"))
        with pytest.raises(LLMError) as caught:
            _complete()
        assert not isinstance(caught.value, LLMBudgetExceededError)
        assert "ожидания" in str(caught.value)


class TestCallTrace:
    """Наблюдаемость: имя ответившей модели и usage уходят вызывающей стороне
    в словаре `trace`, а не только в строку лога."""

    def test_trace_names_the_answered_model_and_sums_usage(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_MODEL", raising=False)
        _provider(monkeypatch, _response('{"ok": true}', usage=USAGE))
        trace: dict = {}
        assert llm_client.call_json("система", "запрос", trace=trace) == {"ok": True}
        assert trace["models"] == list(llm_client.DEFAULT_MODEL_POOL)[:1]
        assert trace["usage"] == {"prompt_tokens": 120,
                                  "completion_tokens": 45, "total_tokens": 165}
        assert trace["attempts"] == 1
        assert trace["rejected_models"] == []

    def test_trace_records_the_model_switch_after_404(self, monkeypatch):
        """Mid-run 404: схема дальше собирается другой моделью — журнал обязан
        нести и отвергнутое имя, и ответившее."""
        monkeypatch.setenv("GIGACHAT_MODEL", "GigaChat,GigaChat-2")
        _provider(monkeypatch,
                  _response_error(llm_client.NotFoundError, 404,
                                  b'{"message":"No such model"}'),
                  _response('{"ok": true}', usage=USAGE))
        trace: dict = {}
        llm_client.call_json("система", "запрос", trace=trace)
        assert trace["rejected_models"] == ["GigaChat"]
        assert trace["models"] == ["GigaChat-2"]

    def test_trace_covers_both_rounds_of_a_repair(self, monkeypatch):
        provider = _provider(monkeypatch, _response("мусор без JSON", usage=USAGE),
                             _response('{"ok": true}', usage=USAGE))
        trace: dict = {}
        llm_client.call_json("система", "запрос", trace=trace)
        assert provider.calls == 2
        assert trace["models"] == [trace["models"][0]] * 2
        assert trace["usage"]["prompt_tokens"] == 240  # оба вызова
        assert len(trace["calls"]) == 2

    def test_call_without_trace_does_not_break(self, monkeypatch):
        _provider(monkeypatch, _response('{"ok": true}', usage=USAGE))
        assert llm_client.call_json("система", "запрос") == {"ok": True}


class TestTlsTrust:
    """`GIGACHAT_CA_BUNDLE_FILE` обязан доходить до SDK.

    Текст подсказки при выключенной проверке TLS советует положить корневой
    сертификат российского УЦ в этот ключ, `.env.example` его документирует — а
    читал его никто. Оператор, последовавший совету, оставался на системном
    хранилище и получал ровно ту же `CERTIFICATE_VERIFY_FAILED`, из-за которой
    проверку и выключил.
    """

    @staticmethod
    def _build(monkeypatch, **env):
        seen = {}

        class Recorder:
            def __init__(self, **kwargs):
                seen.update(kwargs)

        monkeypatch.setattr(llm_client, "GigaChat", Recorder)
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "client:secret")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        llm_client._reset_client()
        try:
            llm_client.get_llm_client()
        finally:
            llm_client._reset_client()
        return seen

    def test_readable_bundle_goes_to_the_client(self, monkeypatch, tmp_path):
        bundle = tmp_path / "rus-trust-ca.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
        seen = self._build(monkeypatch, GIGACHAT_CA_BUNDLE_FILE=str(bundle),
                           GIGACHAT_VERIFY_SSL="true")
        assert seen["ca_bundle_file"] == str(bundle)
        assert seen["verify_ssl_certs"] is True

    def test_missing_bundle_is_not_silently_accepted(self, monkeypatch, tmp_path,
                                                    caplog):
        seen = self._build(monkeypatch,
                           GIGACHAT_CA_BUNDLE_FILE=str(tmp_path / "нет.pem"))
        assert seen["ca_bundle_file"] is None
        assert "файла нет" in caplog.text

    def test_unset_bundle_leaves_the_system_store(self, monkeypatch):
        monkeypatch.delenv("GIGACHAT_CA_BUNDLE_FILE", raising=False)
        assert llm_client._ca_bundle_file() is None

    def test_sdk_puts_the_bundle_over_the_verification_flag(self, tmp_path):
        """Поведение установленного пакета, из-за которого прежний лог врал.

        `gigachat.client._get_kwargs` пишет `kwargs["verify"] =
        settings.ca_bundle_file` ПОСЛЕ `"verify": settings.verify_ssl_certs`:
        переданный bundle перекрывает отключённую проверку. Сверка с настоящим
        SDK, а не с нашим ожиданием: правку порядка строк в `_get_kwargs` надо
        заметить здесь, а не в проде.
        """
        from gigachat import client as gc_client

        class _Settings:
            base_url = "https://example"
            timeout = 10
            verify_ssl_certs = False
            ca_bundle_file = str(tmp_path / "rus-trust-ca.pem")
            ssl_context = None
            cert_file = None
            key_file = None
            key_file_password = None
            max_connections = None

        kwargs = gc_client._get_kwargs(_Settings())
        assert kwargs["verify"] == _Settings.ca_bundle_file

    def test_disabled_verification_does_not_receive_the_bundle(
            self, monkeypatch, tmp_path):
        """Явный `GIGACHAT_VERIFY_SSL=false` старше bundle: bundle не уезжает в
        SDK, иначе отключение проверки молча оборачивалось проверкой против
        этого файла — и лог про «незащищённый канал» был бы ложью.
        """
        bundle = tmp_path / "rus-trust-ca.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
        seen = self._build(monkeypatch, GIGACHAT_CA_BUNDLE_FILE=str(bundle),
                           GIGACHAT_VERIFY_SSL="false")
        assert seen["verify_ssl_certs"] is False
        assert seen["ca_bundle_file"] is None

    def test_both_switches_are_explained_loudly(self, monkeypatch, tmp_path,
                                                caplog):
        """Комбинация двух ключей обязана быть названа: оператор, поставивший
        bundle «для прода» и оставивший false, обязан увидеть, что bundle не
        работает, а не читать общую фразу про незащищённый канал."""
        bundle = tmp_path / "rus-trust-ca.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
        with caplog.at_level("WARNING"):
            self._build(monkeypatch, GIGACHAT_CA_BUNDLE_FILE=str(bundle),
                        GIGACHAT_VERIFY_SSL="false")
        assert "старше" in caplog.text
        assert "НЕ подключается" in caplog.text

    def test_plain_off_switch_keeps_the_plain_warning(self, monkeypatch, caplog):
        """Без bundle фраза остаётся прежней: совет про `GIGACHAT_CA_BUNDLE_FILE`
        адресован тому, у кого его ещё нет."""
        monkeypatch.delenv("GIGACHAT_CA_BUNDLE_FILE", raising=False)
        with caplog.at_level("WARNING"):
            seen = self._build(monkeypatch, GIGACHAT_VERIFY_SSL="false")
        assert seen["verify_ssl_certs"] is False
        assert "незащищённый канал" in caplog.text
        assert "старше" not in caplog.text

    def test_missing_bundle_file_is_still_reported_when_verification_is_on(
            self, monkeypatch, tmp_path, caplog):
        """Порядок операндов не съел прежнюю диагностику: при включённой
        проверке битый путь виден как и был."""
        monkeypatch.setenv("GIGACHAT_VERIFY_SSL", "true")
        seen = self._build(monkeypatch,
                           GIGACHAT_CA_BUNDLE_FILE=str(tmp_path / "нет.pem"))
        assert seen["ca_bundle_file"] is None
        assert "файла нет" in caplog.text


class TestStartupSurvivesProviderOutage:
    """Старт сервиса не зависит от доступности провайдера.

    `ensure_llm_ready` зовёт lifespan, а тот фильтрует `LLMError`. Транспортный
    сбой (`httpx.ConnectError`: отбитый TLS-канал, истёкший сертификат,
    недоступный адрес шлюза) этому фильтру не подчинялся и долетал до
    `Application startup failed` — реестр, доступ и апрувы падали вместе с ИИ
    из-за чужой недоступности. Увидено живым запуском на битых учётных данных.
    """

    @staticmethod
    def _probe(monkeypatch, error):
        monkeypatch.setattr(llm_client, "get_llm_client",
                            lambda: SimpleNamespace(get_token=lambda: (_ for _ in ()).throw(error)))
        llm_client.ensure_llm_ready()

    def test_tls_failure_becomes_an_llm_error(self, monkeypatch):
        import httpx

        with pytest.raises(LLMError) as caught:
            self._probe(monkeypatch, httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        assert "CERTIFICATE_VERIFY_FAILED" in str(caught.value)
        assert "ConnectError" in str(caught.value)

    def test_refused_connection_becomes_an_llm_error(self, monkeypatch):
        with pytest.raises(LLMError):
            self._probe(monkeypatch, ConnectionRefusedError(
                "не удалось соединиться с адресом"))

    def test_oauth_rejection_becomes_an_llm_error(self, monkeypatch):
        with pytest.raises(LLMError) as caught:
            self._probe(monkeypatch, _response_error(
                BadRequestError, 400, "invalid_credentials", url=OAUTH_URL))
        assert "не подтверждена" in str(caught.value)

    def test_absent_credentials_keep_their_own_wording(self, monkeypatch):
        """Сообщение не перематывается второй обёрткой: «не задан» — другой
        диагноз, чем «шлюз недоступен», и оператору нужен именно он."""
        monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
        llm_client._reset_client()
        try:
            with pytest.raises(LLMError) as caught:
                llm_client.ensure_llm_ready()
        finally:
            llm_client._reset_client()
        assert "GIGACHAT_CREDENTIALS не задан" in str(caught.value)
        assert "не подтверждена" not in str(caught.value)


class TestOrchestratorInitIsNotFatal:
    """`init_orchestrator` обязан возвращать управление живому процессу.

    Обещание слоя: при сбое ИИ сервис поднимается, и `/health` показывает
    `llm_orchestrator: disabled`. Держалось оно только для `LLMError` — сборка
    оркестратора (тяжёлые модели эмбеддингов, чтение корпуса) и проба
    авторизации могли бросить что угодно.
    """

    @pytest.fixture(autouse=True)
    def _restore_orchestrator(self):
        from app import ai as ai_module

        saved = ai_module._orchestrator
        yield
        ai_module._orchestrator = saved

    def test_unknown_transport_error_does_not_abort_startup(self, monkeypatch, caplog):
        import httpx
        from app import ai as ai_module

        monkeypatch.setattr(ai_module, "BPMNImprovementOrchestrator",
                            lambda: SimpleNamespace(warmup=lambda: None))
        monkeypatch.setattr(ai_module.llm_client, "ensure_llm_ready",
                            lambda: (_ for _ in ()).throw(
                                httpx.RemoteProtocolError("странный ответ шлюза")))
        with caplog.at_level("ERROR", logger="app.ai"):
            ai_module.init_orchestrator()
        assert ai_module.get_orchestrator() is not None
        assert "Авторизация LLM не подтверждена" in caplog.text

    def test_orchestrator_build_failure_degrades_instead_of_dying(self, monkeypatch,
                                                                   caplog):
        from app import ai as ai_module

        def boom():
            raise RuntimeError("моделей эмбеддингов нет на диске")

        monkeypatch.setattr(ai_module, "BPMNImprovementOrchestrator", boom)
        with caplog.at_level("ERROR", logger="app.ai"):
            ai_module.init_orchestrator()
        assert ai_module.get_orchestrator() is None
        assert "оркестратор не собран" in caplog.text

    def test_warmup_failure_still_leaves_the_orchestrator_usable(self, monkeypatch,
                                                                 caplog):
        """Прогретый индекс — ускорение, а не условие: без него улучшение
        работает, просто первый запрос дольше."""
        from app import ai as ai_module

        class NoCorpus:
            def warmup(self):
                raise OSError("корпус эталонов не читается")

        monkeypatch.setattr(ai_module, "BPMNImprovementOrchestrator", NoCorpus)
        monkeypatch.setattr(ai_module.llm_client, "ensure_llm_ready", lambda: None)
        with caplog.at_level("WARNING", logger="app.ai"):
            ai_module.init_orchestrator()
        assert isinstance(ai_module.get_orchestrator(), NoCorpus)
        assert "первый запрос будет долгим" in caplog.text

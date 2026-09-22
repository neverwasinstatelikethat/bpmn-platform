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
from core.llm_client import (LLMError, LLMRequestTooLargeError,
                             LLMTruncatedError, extract_json, extract_xml)

CHAT_URL = "https://gigachat-api.sberbank.ru/v1/chat/completions"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


class FakeProvider:
    """Клиент провайдера, у которого `chat` бросает заготовленные ошибки.

    Последняя ошибка отдаётся на всех остальных вызовах: так видно число
    попыток."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, request):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(content):
    """Ответ, похожий на то, что отдаёт SDK: choices[0].message.content."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                 finish_reason=None)],
        usage=None,
    )


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

    def test_repair_round_does_not_mask_too_large(self, monkeypatch):
        """Repair-цикл `call_json` ловит только ValueError разбора: отказ
        провайдера по размеру обязан пройти сквозь него наружу."""
        provider = _provider(
            monkeypatch, _response("модель сказала слово без JSON"),
            _response_error(BadRequestError, 400, "слишком длинный запрос, превышен контекст"))
        with pytest.raises(LLMRequestTooLargeError):
            llm_client.call_json("система", "запрос")
        assert provider.calls == 2

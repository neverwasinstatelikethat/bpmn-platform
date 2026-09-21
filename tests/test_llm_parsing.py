"""Парсинг «непослушных» ответов LLM: блоки рассуждений, оглавления кода,
оборванный JSON, BPMN с префиксом и без него.

Регрессии, из-за которых обновление схем не работало никогда: парсер ожидал
только префикс bpmn: и молча отдавал мусор, когда модель оборачивала ответ в
```xml или писала пространство имён по умолчанию."""
import json

import pytest

from core.llm_client import LLMTruncatedError, extract_json, extract_xml


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

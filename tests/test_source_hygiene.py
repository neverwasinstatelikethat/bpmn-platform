"""Гигиена исходников: в русскоязычном коде не должно иероглифов, mojibake и
латиницы, приклеенной к кириллице.

Тест появился не «на всякий случай»: за одну сессию правки `core/` и `eval/`
несколько раз приносили в кириллические комментарии и docstring'и вкрапления
идеограмм и склеенные слова — буква русского слова, чужой символ, снова буква.
Такой дефект не виден в консоли с кодировкой cp1251, не ломает компиляцию и
живёт в тексте, который читает модель, — то есть попадает в промпты. Единственная
защита, которая работает без внимания человека, — проверка на месте.

Смотрит `tokenize`, а не голые строки: проверяются только комментарии и
строковые литералы, где по замыслу проекта русский текст. Файл, который не
разобрался, с проверки снимается — у гигиенического теста нет права превращать
чужой синтаксический сбой в вердикт «в файле иероглифы».
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("app", "core", "eval", "tests")
# Файл, в котором живут сами регулярки, проверить собой невозможно: его литералы
# классов содержат ровно то, что ищут.
SELF = Path(__file__).name

# Идеограммы (CJK, хирагана/катакана, хангуль) и признак битой перекодировки
# кириллицы: буквы латиницы с диакритикой, которых в этом репозитории нет. Класс
# намеренно не трогает `×` и `÷` — в формулировках («сценарий × фикстура ×
# повтор», «подпись×3») они легальны.
IDEOGRAPHS = re.compile("[　-〿一-鿿豈-﫿가-힯぀-ゟ]")
MOJIBAKE = re.compile("[À-ÖØ-öø-ÿ]")
# Латиница, приклеенная к кириллице без разделителя: «лimit», «уbuilt-in»,
# «differить». Это тот же класс дефекта, что и идеограмма, только маскируется под
# осознанную кальку. Разделители `-`, `_`, пробел и обратный слэш (буква
# escape-последовательности вроде `\n`) склейкой не считаются. Верхний регистр
# кириллицы обязан быть в классе: склейка чаще всего прилетает в начало
# предложения («Рatchet» вместо «Ratchet»), где буква заглавная, и прошлый
# вариант проверки это пропускал.
GLUE_LATIN_CYRILLIC = re.compile(r"(?<![A-Za-z0-9\\])[A-Za-z]{2,}[а-яё]")
GLUE_CYRILLIC_LATIN = re.compile(r"[а-яёА-ЯЁ][A-Za-z]{2,}(?![A-Za-z])")
# Регулярный класс символов вроде `[^0-9a-zа-яё]` легален: там русская буква и
# латинская стоят рядом по замыслу.
CHAR_CLASS = re.compile(r"\[[^\]]*\]")


def _python_files() -> list:
    return sorted(p for d in SOURCE_DIRS for p in (REPO_ROOT / d).rglob("*.py")
                  if "__pycache__" not in str(p) and p.name != SELF)


def _text_chunks(path: Path) -> list:
    """Комментарии и строковые литералы файла — (строка, текст)."""
    src = io.open(path, encoding="utf-8").read()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    return [(t.start[0], t.string) for t in tokens
            if t.type in (tokenize.COMMENT, tokenize.STRING)]


def _doc_files() -> list:
    """Markdown, который читают люди и агенты: `AGENTS.md`, гайды `.agents/` и
    рабочие планы в `docs/`.

    Правки этого прохода трижды приносили склейку («неMeasureются»,
    «молчитBoth», « artifact») именно в текст документации: в `.py` её ловит
    `tokenize`, а в `.md` проверялось на глаз. `AGENTS.md` при этом уходит в
    контекст следующей сессии, так что мусор в нём живёт дольше, чем в коде."""
    roots = [REPO_ROOT.glob("*.md"), (REPO_ROOT / "docs").rglob("*.md"),
             (REPO_ROOT / ".agents").rglob("*.md")]
    return sorted({p for root in roots for p in root
                   if "node_modules" not in str(p) and p.name != SELF})


def _md_chunks(path: Path) -> list:
    """Строки markdown: инлайн-код вырезается (в `flow_type='message'` русская
    буква рядом с латинской — замысел, а не склейка), fenced-блоки проверяются
    наравне с прозой: в примерах кода из планов склейка живёт так же."""
    code_span = re.compile(r"`[^`]*`")
    return [(num, code_span.sub(" ", line))
            for num, line in enumerate(
                io.open(path, encoding="utf-8").read().splitlines(), 1)]


def _report(chunks, matcher) -> list:
    out = []
    for line, chunk in chunks:
        hit = matcher(CHAR_CLASS.sub(" ", chunk))
        if hit:
            out.append((line, hit.group(0), chunk[:60]))
    return out


def _scan(paths, line_chunks, matcher) -> list:
    bad = []
    for path in paths:
        for line, token, chunk in _report(line_chunks(path), matcher):
            bad.append(f"{path.relative_to(REPO_ROOT)}:{line}: {token!r} в {chunk!r}")
    return bad


def test_sources_have_no_cjk_or_mojibake_in_text():
    def matcher(c):
        return IDEOGRAPHS.search(c) or MOJIBAKE.search(c)

    bad = (_scan(_python_files(), _text_chunks, matcher)
           + _scan(_doc_files(), _md_chunks, matcher))
    assert not bad, ("в тексте исходников нелегальные символы:\n" + "\n".join(bad))


def test_no_latin_glued_into_cyrillic_prose():
    """«по лlimit» и «differить» — не опечатка в коде, а мусор в тексте, который
    читает модель: промпты и объяснения правил уходят в GigaChat дословно."""
    def matcher(c):
        return GLUE_LATIN_CYRILLIC.search(c) or GLUE_CYRILLIC_LATIN.search(c)

    bad = (_scan(_python_files(), _text_chunks, matcher)
           + _scan(_doc_files(), _md_chunks, matcher))
    assert not bad, ("латиница склеена с кириллицей:\n" + "\n".join(bad))


def test_glue_pattern_catches_a_capitalised_cyrillic_letter():
    """Самопроверка паттерна: «Рatchet» (заглавная «Р» + латиница) проходил
    мимо проверки, пока класс кириллицы был только строчным. Без этого теста
    расширение класса выглядит лишним и его легко вернуть назад.

    Обратные случаи — легальные формы, которые паттерн трогать не должен:
    раздельные слова, дефис и идентификатор целиком латиницей.
    """
    assert GLUE_CYRILLIC_LATIN.search("Рatchet нужен")
    assert GLUE_CYRILLIC_LATIN.search("лimit")
    assert GLUE_LATIN_CYRILLIC.search("differить")
    assert not GLUE_CYRILLIC_LATIN.search("Ratchet нужен")
    assert not GLUE_CYRILLIC_LATIN.search("Бизнес-совет")
    assert not GLUE_LATIN_CYRILLIC.search("бизнес совет")


def test_repo_root_has_no_scratch_files():
    """Одноразовые диагностические скрипты не остаются в дереве: их читают как
    код проекта, и следующий агент правит их вместо `eval/`."""
    leftovers = sorted(p.name for p in REPO_ROOT.glob("_*.py"))
    assert not leftovers, f"в корне лежат рабочие файлы: {leftovers}"

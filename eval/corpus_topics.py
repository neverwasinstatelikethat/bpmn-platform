"""Разметка тем корпуса: независимое от классификатора основание для RAG-метрики.

До появления этой таблицы релевантность эталона в `eval/retrieval.py` сверялась
с полем `domain`, а его заполняет `BPMNKnowledgeBase._detect_domain` — тот же
классификатор, который размечает запрос пользователя. Метрика меряла бы
согласие системы самой с собой: промах подбора был неотличим от промаха
классификатора, а успех — от того, что обе стороны повторили одну ошибку.
Второй дефект: `general` у классификатора собирает 167 файлов из 367, и почти
все это демо элементов нотации (`timer_5.bpmn`, `signal_3.bpmn`) и пустые
шаблоны упражнений. Для сценария, где темой указан `general`, точность росла от
того, что поиск вернул любой демо-файл, — то есть метрика была играбельной.

Здесь темы расставлены руками по именам файлов датасета. Имя файла корпуса —
человеческое (`Dispatch_of_Goods_<uuid>`, `Exercise_5_-_Credit_Scoring`), и это
другой сигнал, чем текст XML: разметка не зависит от проверяемого контура.
Файл только добавляет знания, никаких решений о ранжировании — индекс живёт в
`core/`, и метрика по-прежнему не участвует в подборе.
"""

import re

# Упорядоченная таблица: первое совпадение подстроки решает. Порядок — от
# частного к общему, потому что `Exercise_5_-_Credit_Scoring` обязан попасть в
# кредитный скоринг, а не в «пустое упражнение».
TOPIC_KEYS = (
    # Демо элементов нотации: имя — название конструкции, процесса нет.
    ("timer", "notation_demo"),
    ("signal", "notation_demo"),
    ("message_store", "notation_demo"),
    ("link", "notation_demo"),
    ("intermediate", "notation_demo"),
    ("gateway", "notation_demo"),
    ("escalation", "notation_demo"),
    ("error_terminate", "notation_demo"),
    ("terminate", "notation_demo"),
    ("conditional", "notation_demo"),
    ("compensate", "notation_demo"),
    # Отгрузка товара (немецкий и английский варианты одного курса).
    ("warenversand", "goods_dispatch"),
    ("dispatch", "goods_dispatch"),
    ("shipping", "goods_dispatch"),
    ("ship_stuff", "goods_dispatch"),
    # Кредитный скоринг и запрос в бюро (SCHUFA) — финансовый процесс.
    ("schufa", "credit_scoring"),
    ("credit", "credit_scoring"),
    ("scoring", "credit_scoring"),
    ("banking", "banking"),
    # Регрессное требование страховщика: разбор претензии с решением.
    ("recourse", "claim_recourse"),
    ("regress", "claim_recourse"),
    ("subrogation", "claim_recourse"),
    ("claim", "claim_recourse"),
    # Заказ еды самообслуживания (немецкий `Selbstbedienung` = `sb_res`).
    ("self_service", "food_service"),
    ("selfservice", "food_service"),
    ("self-service", "food_service"),
    ("sb_res", "food_service"),
    ("fastfood", "food_service"),
    ("restaurant", "food_service"),
    ("meal", "food_service"),
    # Учебные шаблоны без предметного содержания.
    ("new_process", "tutorial"),
    ("introduction", "tutorial"),
    ("my_first_example", "tutorial"),
    ("ueb", "tutorial"),
    ("practice", "tutorial"),
    ("exercise", "tutorial"),
    ("excercise", "tutorial"),
    ("excersise", "tutorial"),
    ("excersice", "tutorial"),
    ("exersice", "tutorial"),
    ("ex_", "tutorial"),
    ("ex6", "tutorial"),
)

# Имя файла оканчивается 32-символьным hex-суффиксом из датасета; он не тема.
_UUID = re.compile(r"_[0-9a-f]{32}$")


def topic_of(name: str) -> str:
    """Тема эталона по имени файла корпуса, `unknown` если имя не опознано.

    `unknown` — не «не релевантен», а «разметка не покрывает»: такие файлы
    печатаются отдельным числом в отчёте, иначе разрастание датасета молча
    уронило бы точность.
    """
    stem = _UUID.sub("", str(name or "")).lower()
    for key, topic in TOPIC_KEYS:
        if key in stem:
            return topic
    return "unknown"


def topics_of_names(names) -> dict:
    """{имя: тема} — разбор покрытия корпуса, чтобы тест сверил его с фактом."""
    return {str(n): topic_of(n) for n in names}


def corpus_topics() -> dict:
    """Темы всех файлов датасета по его именам.

    Корпус читается списком имён, XML не открывается: разметка привязана к
    именам, и проверка «таблица покрывает датасет» не должна зависеть от
    тяжёлой загрузки `core`.
    """
    import glob
    import os

    from core.llm_improve import DATASET_PATH

    names = [os.path.splitext(os.path.basename(p))[0]
             for p in sorted(glob.glob(os.path.join(DATASET_PATH, "*.bpmn")))]
    return topics_of_names(names)


__all__ = ["TOPIC_KEYS", "topic_of", "topics_of_names", "corpus_topics"]

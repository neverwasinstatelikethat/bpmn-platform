# Генерация BPMN-схем по текстовому описанию.
#
# Один структурированный вызов LLM заменяет весь NLP-конвейер (перевод,
# кореференция, NER, классификаторы): модель сразу возвращает структуру
# процесса в JSON. Структура считается недоверенной и чинится
# детерминированным repair_structure, а не отбрасывается.
#
# Разделение пула и дорожки — смысловое: пул = независимый участник
# (организация или внешняя система), роли сотрудников и подсистемы одной
# организации = дорожки внутри одного пула. Если склеить роли в пулы,
# линейный бизнес-поток распадается на цепочку messageFlow, и починить это
# постфактум невозможно — поэтому промпт задаёт его явно, а repair не даёт
# ошибке модели превратиться в «валидную» схему молча.
#
# Ветвление и ожидания — те же грабли: модель рисует развилку без схождения и
# «ждёт два часа» как задачу. Для генератора это не косметика, а нарушение,
# которое скоринг правомерно считает, поэтому шлюзы и события проверяются
# детерминированно, а план с нарушениями один раз переизляется с перечнем того,
# что модель обязана исправить.
#
# Концы потоков проверяются тем же способом: sequence-поток не входит в
# startEvent и boundaryEvent и не выходит из endEvent — такую дугу repair
# убирает с заметкой, а не перекраивает в маршрут, которого модель не описывала.
#
# XML генерируется только семантический: координаты не выдаются — фронтенд
# всегда прогоняет схему через bpmn-auto-layout, ему достаточно пустого
# скелета BPMNDiagram/BPMNPlane.
import difflib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

from .bpmn_edits import (DEFAULT_TIMER_DURATION, EVENT_DEFINITIONS,
                         SEQUENCE_FORBIDDEN_SOURCES,
                         SEQUENCE_FORBIDDEN_TARGETS)
from .llm_client import (LLMError, LLMRequestTooLargeError, LLMTruncatedError,
                         call_json)

logger = logging.getLogger(__name__)

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", BPMNDI_NS)

TASK_KINDS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "businessRuleTask", "sendTask", "receiveTask",
}
GATEWAY_KINDS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway"}
EVENT_KINDS = {
    "startEvent", "endEvent",
    "intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent",
}
ALL_KINDS = TASK_KINDS | GATEWAY_KINDS | EVENT_KINDS
# «Шаг» пула: то, что отличает живой процесс от нарисованных кружков входа и
# выхода. Пул без шага — не участник, а артефакт модели.
STEP_KINDS = TASK_KINDS | GATEWAY_KINDS | {"intermediateCatchEvent",
                                           "intermediateThrowEvent"}
# События, обязанные иметь определение: без него bpmn-js рисует пустой кружок,
# и «промежуточное событие» остаётся декларацией.
TYPED_EVENT_KINDS = {"intermediateCatchEvent", "intermediateThrowEvent",
                     "boundaryEvent"}

MAX_TEXT_CHARS = 10_000
MAX_PARTICIPANTS = 10
MAX_LANES = 30
MAX_ELEMENTS = 60
MAX_FLOWS = 120
NAME_LIMIT = 120
DOCUMENTATION_LIMIT = 400
# Исправлений достижимости не больше, чем элементов: иначе генератор
# дорисует связи туда, где модель их не описала, и схема «починится» сама.
MAX_REACH_FLOWS = 2 * MAX_ELEMENTS
# Вставляемых шлюзов схождения — не больше четверти элементов: слияние потоков
# правит маршрут, и невозбранный аппетит здесь превратил бы схему в решето.
MAX_MERGE_GATEWAYS = MAX_ELEMENTS // 4
# Столько же вставок развилки: условие от не-шлюза — дефект плана, но чинить его
# перестановкой всего маршрута нельзя.
MAX_SPLIT_GATEWAYS = MAX_ELEMENTS // 4
# Порог схожести названий пулов: ниже — слишком разные сущности, выше —
# опечатки и падежи («Бухгалтерия» / «Бухгалтерией»).
POOL_MATCH_CUTOFF = 0.6
# Вторая попытка одна: при тарифе GigaChat с одним одновременным запросом
# каждая лишняя итерация удваивает пользовательскую задержку, а переспрос по
# нарушениям дешевле, чем принятая пользователем битая схема.
MAX_RETRY_PLAN_CHARS = 12_000
# Бюджет ответа на план в токенах. 8000 хватало плану без описаний; теперь
# модель пишет документацию к каждому шагу, а обрезанный JSON — не «схема без
# описаний», а отказ всей генерации: `LLMTruncatedError` повторять бессмысленно.
MAX_PLAN_TOKENS = 12_000
# Правка принадлежности шагов отправляет не план, а список id: лимиты держат
# запрос в размерах, которые модель не обрезает.
MAX_PATCH_QUESTION_CHARS = 9_000
MAX_PATCH_ELEMENT_LINES = 60
# Сколько кандидатов на роль показывать в одном вопросе. Три живых прогона
# (63 кейса с вопросом о ролях): список из четырёх-шести кандидатов модель
# закрывает в ~31% случаев, короткого — в ~70%. Не отвеченный кандидат —
# несправедливо потерянный кандидат: пул уезжает с схемы как пустой.
MAX_ROLE_QUESTIONS = 3
MAX_PATCH_MOVES = 12
# Сколько потерянных участников описания возвращаем за один узкий вопрос и при
# каком числе пулов вопрос про них задаём вообще.
MAX_PATCH_MISSING = 3
MAX_POOLS_FOR_MISSING_QUESTION = 3

# ISO-8601 для хронометража таймера: длительность (PT2H), повтор (R3/PT10M).
# Модель пишет их уверенно, но «2 часа» тоже пробует — поэтому форма
# проверяется, а не подставляется молча.
_TIMER_RE = re.compile(
    r"^(?:R\d+/)?P(?:\d+[YMWD])*(?:T(?:\d+(?:\.\d+)?[HMS])*)?$"
)

_SYSTEM_PROMPT = """Ты — аналитик бизнес-процессов. По текстовому описанию \
построй структуру BPMN 2.0 процесса.

Верни СТРОГО ОДИН JSON-объект без пояснений и без блоков кода:
{
  "actors": ["каждое действующее лицо описания теми же словами, что в тексте"],
  "participants": ["название внешнего участника", \
{"name": "название роли или подразделения", "external": false, \
"inside": "название организации, чьей дорожкой оно было бы"}],
  "lanes": [{"id": "L1", "name": "название дорожки", "participant": "название пула"}],
  "elements": [{"id": "A1", "kind": "userTask", "name": "Название шага", \
"participant": "название пула", "lane": "L1"}],
  "flows": [{"id": "F1", "source": "A1", "target": "A2", "kind": "sequence", \
"condition": "", "default": false}]
}

Поля элемента: `participant` и `lane` обязательны для всех, кроме шагов \
однопольного процесса; `attached_to` и `event_definition` — для граничного \
события; `timer` — для таймера; `documentation` — текст описания шага.

Действующие лица: первым делом выпиши в `actors` всех, кто в описании действует \
от своего имени (организация, контрагент, внешняя система, «клиент», \
«поставщик», «система мониторинга») — теми же словами, что в тексте. Затем для \
каждого из `actors` объяви пул или дорожку и запиши за ним его собственные \
шаги: действующее лицо, названное в описании и пропущенное в схеме, — ошибка \
моделирования.

Пулы и дорожки:
- Пул (participant) — независимый участник: организация, внешний контрагент \
или внешняя система (WMS, перевозчик, получатель). Один пул — один процесс.
- Роли сотрудников, отделы и подсистемы одной организации — это ДОРОЖКИ \
(lanes) внутри одного пула, а не отдельные пулы. Должность по суффиксу \
(кладовщик, комплектатор, контролёр, экспедитор, водитель, оператор, \
менеджер, инициатор) пулом быть не может: это всегда дорожка организации, \
которой она подчиняется. Пулов в схеме не больше, чем организаций и внешних \
систем в описании.
- Действие внешнего участника принадлежит его пулу: «поставщик подтверждает \
отгрузку», «перевозчик принимает груз», «клиент подписывает договор» — шаги \
его пула, дописывать их в дорожки организации нельзя. Между пулами такой \
передачей связи идёт messageFlow, и от внешнего участника обязана быть хотя \
бы одна ветка в его пул.
- Если роль или подразделение всё же оказались в participants, запиши их \
объектом с `"external": false` и `"inside"` — названием организации, внутри \
которой они работают: его шаги станут дорожкой этого пула. Самостоятельный \
участник (контрагент, чужая система) остаётся простой строкой. Роль без \
`inside` — незакрытое нарушение: невыбранной остаётся организация, которой \
она принадлежит.
- Имя пула — название из описания, а не родовое слово. Если в тексте названа \
WMS, пул называется «WMS», а не «система»: родовое имя («система», «сервис», \
«подразделение») съедает участника, которого проверяют по имени, и \
превращает две организации в одну.
- У пула не бывает дорожки с его собственным именем: такая пара — роль, \
объявившая саму себя. Либо это подразделение организации (тогда `external: \
false` и `inside`), либо у пула убирается дублирующая дорожка.
- Имя пула — то же слово, что в описании: названная система «WMS» остаётся \
«WMS», а не превращается в «Склад». Схема, где участник переименован, теряет \
связь с описанием, и проверить её взаимодействие нечем.
- Каждый участник, названный в описании (организация, контрагент, внешняя \
система — в том числе аббревиатура вроде WMS, CRM, 1С), обязан присутствовать \
в схеме: пулом со своими шагами либо, если это подразделение другой \
организации, её дорожкой.
- В пуле обязан быть хотя бы один шаг (задача, шлюз или промежуточное \
событие). Пул, где только «Старт → Завершение», недопустим: либо это дорожка \
основного пула, либо такого участника в процессе нет.
- participant у элемента и у дорожки — название пула из "participants"; \
lane у элемента — id дорожки из "lanes". Элемент стоит в дорожке своего пула.

Развилки:
- exclusiveGateway рисуй там, где поток действительно раздваивается: у \
расходящегося шлюза минимум два исходящих потока. Шлюз с одним входящим и \
одним исходящим — не развилка, а обычный шаг (task).
- Сходящийся шлюз, наоборот, принимает две и более ветки и имеет один \
исходящий: ставь его туда, где развилка снова сходится в один маршрут.
- У каждой расходящейся ветки exclusiveGateway заполни condition; ветку, \
которая идёт «во всех остальных случаях», помечай default=true и оставляй без \
условия. Ветка с default ровно одна; у сходящегося шлюза условий нет.
- Ветки обязаны сходиться: либо каждая ветка приводит к своему endEvent, либо \
перед общим последующим шагом нарисуй второй шлюз того же типа и пропусти \
через него все ветки. Развилка без схождения — битая схема.
- parallelGateway служит одновременно и для расщепления, и для слияния: \
у него минимум два потока хотя бы с одной стороны.

Шаги:
- Каждый task и serviceTask — одно законченное действие одного исполнителя. \
«Система регистрирует алерт и создаёт инцидент» — это два шага, а не один \
«Регистрация инцидента»; последнее действие фразы («и заводит задачу на \
улучшение») тоже остаётся на схеме. Склеенные или выброшенные действия — это \
процесс, которого в описании нет, а новых действий придумывать всё равно нельзя.
- Имя шага — глагол действия («Проверить упаковку»), а не отглагольное \
существительное («Проверка упаковки»). «Проверка», «получение», «оформление», \
«фиксация», «доставка», «согласование» в имени — признак склеенного шага: \
разбери его на действия, каждое из которых делает один исполнитель.
- У каждого task и serviceTask заполни `documentation` одним предложением из \
текста описания: что именно происходит, кто делает и при каком условии. Имя \
шага описание не заменяет: схему правят, согласуют и ищут по описаниям шагов. \
События и шлюзы документируй, только если их имя не объясняет смысл.

События:
- kind элемента: один из """ + ", ".join(sorted(ALL_KINDS)) + """.
- У промежуточного (intermediateCatchEvent, intermediateThrowEvent) и \
граничного (boundaryEvent) события заполни event_definition: один из \
""" + ", ".join(sorted(EVENT_DEFINITIONS)) + """.
- Ожидание, дедлайн и SLA — это boundaryEvent с event_definition="timer", \
attached_to=id задачи, на которую навешано ожидание, и timer в формате \
ISO-8601 (PT2H, PT15M, R3/PT10M). У такого события обязательна ветка \
обработки: поток в шаг-эскалацию, а не обратно в ту же задачу.
- Пока процесс ждёт ответа внешней системы — intermediateCatchEvent с \
event_definition="message".
- Используй userTask для действий людей, serviceTask для систем и сервисов.
- У каждого пула должны быть хотя бы один startEvent и один endEvent.

Потоки:
- kind="sequence" — только внутри одного пула, в том числе между его \
дорожками; условие (текст ветки) указывай в поле condition только на \
исходящих потоках шлюза.
- kind="message" — только между элементами РАЗНЫХ пулов. Покажи им \
взаимодействие организаций, а не передачу работы между сотрудниками: \
бизнес-поток внутри одной организации ведётся sequence-потоками.
- Каждый элемент, кроме startEvent и boundaryEvent, должен быть достижим: \
на него входит хотя бы один sequence-поток.
- Каждый элемент, кроме endEvent, должен иметь исходящий sequence-поток \
(внутри своего пула) — иначе процесс в этом пуле обрывается.

Пример. «Начальник смены цеха заводит наряд на починку упаковочной линии; \
техник цеха осматривает узел, при отсутствии детали в ремфонде узел вносят \
в план заказа; замена длится не более шести часов, при просрочке — доклад \
мастеру участка; если линию не починить своими силами, её передают подрядчику»:
{"participants": ["Цех фасовки", "Сервисная служба"],
 "lanes": [{"id": "L_t", "name": "Техник цеха", "participant": "Цех фасовки"}, \
{"id": "L_m", "name": "Мастер участка", "participant": "Цех фасовки"}, \
{"id": "L_p", "name": "Начальник смены", "participant": "Цех фасовки"}],
 "elements": [
   {"id": "S1", "kind": "startEvent", "name": "Наряд на починку", "participant": "Цех фасовки", "lane": "L_p"},
   {"id": "A1", "kind": "userTask", "name": "Осмотреть линию", "participant": "Цех фасовки", "lane": "L_t", "documentation": "Замер вибрации и температуры узла до разбора"},
   {"id": "G1", "kind": "exclusiveGateway", "name": "Деталь в ремфонде?", "participant": "Цех фасовки", "lane": "L_t"},
   {"id": "A2", "kind": "userTask", "name": "Заменить деталь", "participant": "Цех фасовки", "lane": "L_t", "documentation": "Техник снимает узел и ставит деталь из ремфонда"},
   {"id": "A3", "kind": "userTask", "name": "Внести узел в план заказа", "participant": "Цех фасовки", "lane": "L_p", "documentation": "Начальник смены заказывает деталь, если её нет в ремфонде"},
   {"id": "T1", "kind": "boundaryEvent", "name": "Прошло 6 часов", "participant": "Цех фасовки", "lane": "L_t", "attached_to": "A2", "event_definition": "timer", "timer": "PT6H"},
   {"id": "A4", "kind": "userTask", "name": "Доложить мастеру участка", "participant": "Цех фасовки", "lane": "L_m"},
   {"id": "G2", "kind": "exclusiveGateway", "name": "Узел заменён", "participant": "Цех фасовки", "lane": "L_t"},
   {"id": "A5", "kind": "serviceTask", "name": "Оформить вызов подрядчика", "participant": "Цех фасовки", "lane": "L_p"},
   {"id": "E1", "kind": "endEvent", "name": "Линия в работе", "participant": "Цех фасовки", "lane": "L_p"},
   {"id": "S2", "kind": "startEvent", "name": "Вызов принят", "participant": "Сервисная служба", "lane": ""},
   {"id": "A6", "kind": "task", "name": "Приехать на объект", "participant": "Сервисная служба", "lane": "", "documentation": "Подрядчик прибывает на объект и выполняет замену своими силами"},
   {"id": "E2", "kind": "endEvent", "name": "Наряд закрыт", "participant": "Сервисная служба", "lane": ""}
 ],
 "flows": [
   {"id": "F1", "source": "S1", "target": "A1", "kind": "sequence", "condition": ""},
   {"id": "F2", "source": "A1", "target": "G1", "kind": "sequence", "condition": ""},
   {"id": "F3", "source": "G1", "target": "A2", "kind": "sequence", "condition": "Деталь есть"},
   {"id": "F4", "source": "G1", "target": "A3", "kind": "sequence", "condition": "", "default": true},
   {"id": "F5", "source": "A3", "target": "A2", "kind": "sequence", "condition": ""},
   {"id": "F6", "source": "T1", "target": "A4", "kind": "sequence", "condition": ""},
   {"id": "F7", "source": "A2", "target": "G2", "kind": "sequence", "condition": ""},
   {"id": "F8", "source": "A4", "target": "G2", "kind": "sequence", "condition": ""},
   {"id": "F9", "source": "G2", "target": "A5", "kind": "sequence", "condition": ""},
   {"id": "F10", "source": "A5", "target": "E1", "kind": "sequence", "condition": ""},
   {"id": "M1", "source": "A5", "target": "A6", "kind": "message", "condition": ""},
   {"id": "F11", "source": "S2", "target": "A6", "kind": "sequence", "condition": ""},
   {"id": "F12", "source": "A6", "target": "E2", "kind": "sequence", "condition": ""}
 ]}

Здесь «Техник цеха», «Мастер участка» и «Начальник смены» — дорожки одного \
пула, потому что роли одной организации не бывают отдельными пулами; сервисная \
служба — отдельный пул, и связь с ней идёт потоком-сообщением. G1 расщепляет \
маршрут, G2 его снова сливает, T1 — таймер-ожидание на задаче замены с веткой \
доклада мастеру.

Прочее:
- Отвечай на языке описания процесса (названия шагов, пулов и дорожек — как \
в тексте).
"""

# Стадия состава: отдельный маленький ответ вместо того, чтобы модель решала
# «кто участник, а кто роль» одновременно с ~20 элементами маршрута. Живые
# прогоны #40–#42 на этом месте и ломались: пул на каждую должность, каждый со
# своим стартом и финишем, — процесс фрагментировался, и вместе с фрагментацией
# падали `roles_as_lanes`, `has_branching`, `min_steps`, `expected_participants`.
MAX_ROSTER_TOKENS = 1_500
_ROSTER_SYSTEM_PROMPT = """Ты — аналитик бизнес-процессов. По описанию процесса
определи ЕГО СОСТАВ: кто участвует, что из этого организация, система,
контрагент, а что — роль или подразделение внутри кого-то. Маршрут, шаги и
потоки рисовать не нужно.

Верни СТРОГО ОДИН JSON-объект без пояснений и без блоков кода:
{"organizations": ["название организации-участника"],
 "systems": ["система, у которой есть свои действия (1С, MES, CRM)"],
 "counterparties": ["внешняя сторона, которой процесс передаёт работу или \
которая передаёт её процессу: подрядчик, субподрядчик, арендодатель"],
 "roles": [{"name": "должность, сотрудник или отдел", \
"host": "участник, которому это принадлежит"}]}

- Пулом (organizations, systems, counterparties) бывает сторона, которая
действует от своего имени и с кем-то взаимодействует: предприятие, площадка,
самостоятельная служба, учётная программа, внешний исполнитель. Сторона
остаётся пулом, как бы коротко её ни называли в тексте.
- Ролью (roles) бывает должность, сотрудник или отдел внутри одного пула:
«начальник смены», «техник», «наладчик», «мастер участка», «обходчик».
Окончание слова ничего не решает: тот, кто отвечает перед кем-то как
самостоятельная сторона, — пул, а нанятый им сотрудник — роль.
- host обязателен и берётся из ТВОИХ ЖЕ списков организаций, систем и \
контрагентов — тем же словом, которым ты назвал пул. Сотрудник подрядчика \
принадлежит подрядчику, а не тому, кто к нему обратился: «слесарь \
сервисной службы» — роль «Сервисной службы», а не цеха, который прислал \
заявку.
- Хозяина нет в списке, а сущность в тексте действует сама и с кем-то \
взаимодействует, — значит это пул: выпиши её в organizations, systems или \
counterparties. Ответ всегда между «пул» и «хозяин из своего списка»; \
«не знаю» с пустым host здесь не принимается.
- Роль, которая делает работу только вместе с чужими шагами и ни с кем не \
взаимодействует, отдельным пулом не становится: своя дорожка в пуле хозяина.
- Имена бери теми же словами, что в описании: имя из текста не заменяется
родственным («1С» не становится «системой», площадка — именем компании).
Если в описании нет названия организации, зови её тем словом, которым её
зывает само описание, а не выдуманным ярлыком вида «Организация-…» или
«… компания»: по выдуманному слову хозяина роли не найти. Родовое слово
(«организация», «подразделение», «система» без имени) участником не считается.
- Действующее лицо, у которого в описании есть свои действия, обязано попасть
в один из списков; забыть контрагента — ошибка состава.

Пример («Начальник смены цеха заводит наряд на починку упаковочной линии,
техник цеха осматривает узел, при просрочке — доклад мастеру участка, если
линию не починить своими силами, её передают подрядчику из сервисной
службы»):
{"organizations": ["Цех фасовки"], "systems": [],
 "counterparties": ["Сервисная служба"],
 "roles": [{"name": "Начальник смены", "host": "Цех фасовки"},
           {"name": "Техник цеха", "host": "Цех фасовки"},
           {"name": "Мастер участка", "host": "Цех фасовки"}]}
"""

# Стадия маршрута идёт по тем же правилам моделирования, что и одношаговый путь
# (иначе два вызова соблюдали бы разные правила), но с закреплением состава.
_FLOW_SYSTEM_PROMPT = _SYSTEM_PROMPT + """
Состав процесса (пулы и дорожки) заказчик уже утвердил и прислал в запросе.
`participants` и `lanes` верни ровно этим списком: новых пулов не заводим,
роль не становится участником, а подразделение — вторым процессом. Всё
внимание — шагам, событиям, шлюзам и потокам внутри утверждённого состава:
каждое действующее лицо описания должно остаться на схеме, а действия людей
одной организации — идти sequence-потоком в её пуле.
"""


def _pool_name(entry: Any) -> str:
    """Имя участника плана: пул бывает и строкой, и объектом с `external`."""
    if isinstance(entry, dict):
        return _raw_text(entry.get("name"))
    return _raw_text(entry)


def _skeleton_block(participants: List[Any], lanes: List[Dict[str, Any]]) -> str:
    """Состав процесса для запроса маршрута: пулы и дорожки с id."""
    lines = ["Состав процесса (утверждён, менять нельзя):", "пулы: " + (
        ", ".join(f"«{_pool_name(p)}»" for p in participants) or "—")]
    if lanes:
        lines.append("дорожки: " + ", ".join(
            f"{lane.get('id')} «{lane.get('name')}» в «{lane.get('participant')}»"
            for lane in lanes))
    else:
        lines.append("дорожки: нет")
    return "\n".join(lines)


def parse_roster(data: Dict[str, Any], text: str) -> Tuple[Dict[str, Any],
                                                           List[str]]:
    """Состав модели → каркас плана: пулы, дорожки, список действующих лиц.

    Роль без хозяина остаётся пулом с `external: false` — это нарушение rank 0
    («роль, не назвавшая организацию»), и закрывает его переспрос, а не догадка
    кода: хозяина роли по названию должности живые прогоны угадывать запретили
    (откат `_sole_role_host`, прогон #37).
    """
    notes: List[str] = []
    words = _text_words(text)
    participants: List[Any] = []
    taken: Set[str] = set()
    by_norm: Dict[str, str] = {}

    def add_pool(name: Any) -> str:
        clean = _raw_text(name)
        if not clean:
            return ""
        if not _mentioned(clean, words):
            # Ярлык вместо имени: значимая часть, названная в описании, и есть
            # имя участника. Без этого хозяин роли теряется, а роль становится
            # пулом — см. прогон #43.
            rescued = _actor_name_from_text(clean, words)
            if not rescued:
                notes.append(f"участник «{clean}» не взят в состав: в описании "
                             "такого имени нет")
                return ""
            notes.append(f"участник «{clean}» назван тем словом, которым его "
                         f"зывает описание: «{rescued}»")
            clean = rescued
        norm = _norm_name(clean)
        if norm in taken:
            # Хозяин уже в составе — это не отказ, а тот же самый пул.
            return by_norm.get(norm, clean)
        if _generic_actor_name(clean):
            notes.append(f"участник «{clean}» не взят в состав: родовое слово, а "
                         "не название из описания")
            return ""
        taken.add(norm)
        by_norm[norm] = clean
        participants.append(clean)
        return clean

    for key in ("organizations", "systems", "counterparties", "participants"):
        value = data.get(key)
        for item in (value if isinstance(value, list) else [])[:MAX_PARTICIPANTS]:
            add_pool(item if not isinstance(item, dict) else item.get("name"))
            if len(participants) >= MAX_PARTICIPANTS:
                break

    lanes: List[Dict[str, Any]] = []
    roles = data.get("roles")
    for idx, role in enumerate(roles if isinstance(roles, list) else []):
        if len(participants) + len(lanes) >= MAX_PARTICIPANTS * 2:
            break
        name = _raw_text(role.get("name") if isinstance(role, dict) else role)
        host = _raw_text(role.get("host") if isinstance(role, dict) else "")
        if not name or _norm_name(name) in taken:
            continue
        if not _mentioned(name, words):
            notes.append(f"роль «{name}» не взята в состав: в описании такого "
                         "имени нет")
            continue
        canonical = add_pool(host)
        if not canonical and host:
            # Хозяин не принят (родовое слово или имя не из описания): роль
            # остаётся незакрытым нарушением, а не сиротской дорожкой.
            notes.append(f"роль «{name}» осталась без организации: хозяин «{host}» "
                         "не название из описания")
        if not canonical:
            taken.add(_norm_name(name))
            participants.append({"name": name, "external": False})
            continue
        lane_id = f"L{len(lanes) + 1}"
        lanes.append({"id": lane_id, "name": name,
                      "participant": canonical})
        taken.add(_norm_name(name))
        notes.append(f"«{name}» — роль «{canonical}»: на схеме это дорожка её пула")

    actors = [_pool_name(p) for p in participants] + [
        lane["name"] for lane in lanes]
    if not participants:
        return {}, notes
    return ({"participants": participants, "lanes": lanes,
             "actors": [a for a in actors if a]}, notes)


def _merge_skeleton(skeleton: Dict[str, Any], plan: Dict[str, Any],
                    text: str) -> Tuple[Dict[str, Any], List[str]]:
    """Ответ маршрута поверх утверждённого состава.

    Каркас задаёт пулы и дорожки, но модель вправе найти участника, которого
    состав пропустил, — если его имя звучит в описании. Выдуманного участника не
    принимаем: иначе «не заводить лишних пулов» работает в одну сторону.
    """
    notes: List[str] = []
    words = _text_words(text)
    merged = dict(plan)
    plan_pools = _plan_pool_names(plan)
    skeleton_pools = [_pool_name(p) for p in skeleton["participants"]]
    known = {_norm_name(p) for p in skeleton_pools}
    # Имя, которое состав посадил дорожкой, для маршрута не может быть пулом:
    # «Инженер» — роль «Дежурства», и шаг, записанный на «Инженера», принадлежит
    # его пулу-хозяину. Без этого ответа переспрос возвращал роли пулами (прогон
    # #43: 6 нарушений rank 0 вместо 0 — и отказ вместе с таймером).
    lane_by_name = {}
    for lane in skeleton["lanes"]:
        lane_by_name.setdefault(_norm_name(lane.get("name")), lane)
    elements = _raw_dicts(plan.get("elements"))
    rehomed: List[str] = []
    for entry in elements:
        declared = _norm_name(_raw_text(entry.get("participant")))
        lane = lane_by_name.get(declared)
        if lane is None or declared in known:
            continue
        if _norm_name(_raw_text(entry.get("lane"))) == _norm_name(lane["id"]):
            continue
        entry["participant"] = lane["participant"]
        entry["lane"] = lane["id"]
        if lane["name"] not in rehomed:
            rehomed.append(lane["name"])
    if rehomed:
        merged["elements"] = elements
        notes.append("шаги, записанные на роли пулом, возвращены в их пулы "
                     "дорожками: " + ", ".join(f"«{n}»" for n in rehomed))
    extra: List[Any] = []
    for pool in (skeleton_pools + [p for p in plan_pools
                                   if _norm_name(p) not in known]):
        if _norm_name(pool) in known:
            continue
        if _norm_name(pool) in lane_by_name:
            continue
        if not _mentioned(pool, words):
            notes.append(f"пул «{pool}» не добавлен: в описании такого имени нет, "
                         "а состав процесса уже утверждён")
            continue
        extra.append(pool)
        known.add(_norm_name(pool))
    merged["participants"] = list(skeleton["participants"]) + extra
    skeleton_lane_ids = {lane["id"] for lane in skeleton["lanes"]}
    skeleton_lane_names = {_norm_name(lane["name"]) for lane in skeleton["lanes"]}
    lanes = list(skeleton["lanes"])
    for lane in _raw_dicts(plan.get("lanes")):
        if (lane.get("id") in skeleton_lane_ids
                or _norm_name(lane.get("name")) in skeleton_lane_names
                or _norm_name(lane.get("name")) in {_norm_name(p) for p in
                                                    merged["participants"]}):
            continue
        lanes.append(dict(lane))
    merged["lanes"] = lanes
    merged["actors"] = ([a for a in (skeleton.get("actors") or [])
                         if isinstance(a, str)]
                        + [a for a in (_raw_dicts_plan_actors(plan))
                           if a not in (skeleton.get("actors") or [])])
    if extra:
        notes.append("модель маршрута добавила участников, которых нет в составе: "
                     + ", ".join(f"«{p}»" for p in extra))
    return merged, notes


def _raw_dicts_plan_actors(plan: Dict[str, Any]) -> List[str]:
    actors = plan.get("actors")
    return [a.strip() for a in (actors if isinstance(actors, list) else [])
            if isinstance(a, str) and a.strip()]


# Переспрос идёт с тем же системным промптом: правила моделирования обязаны
# жить в одном месте, иначе второй вызов начнёт соблюдать другие правила.
# Формулировка ниже — про то, что править нельзя содержание.
#
# Ответ — ЗАПЛАТКА, а не переписанный план. Живые прогоны заплатили за
# «верни план целиком» потерянными шагами и перекроенным составом: починка
# корректных элементов неотделима от их копирования, а модель при копировании
# их меняла (прогон #43: чтобы добавить таймер, модель перекрасила все пулы в
# «роли без хозяина», и правка уехала в корзину вместе с 6 новыми нарушениями).
# Латка не может сломать то, чего не касается.
_RETRY_PATCH_SCHEMA = """{"fixes": [{"id": "A2", "participant": "название пула", \
"lane": "L1", "name": "новое имя", "event_definition": "timer", \
"timer": "PT15M", "attached_to": "A1"}],
 "add_elements": [{"id": "B1", "kind": "boundaryEvent", "name": "…", \
"participant": "…", "lane": "…", "attached_to": "A2"}],
 "add_flows": [{"id": "F9", "source": "A2", "target": "B1", \
"condition": "Да", "kind": "sequence"}],
 "remove_flows": ["F3"],
 "participants": ["Перевозчик"],
 "lanes": [{"id": "L9", "name": "Водитель", "participant": "Перевозчик"}]}"""

_RETRY_TEMPLATE = """Ты уже построил структуру BPMN по описанию ниже, но в ней \
найдены нарушения методологии. Исправь ПЕРЕЧИСЛЕННЫЕ нарушения и верни СТРОГО \
ОДИН JSON-объект — заплатку к плану, без пояснений и без блоков кода:
""" + _RETRY_PATCH_SCHEMA + """

- Правки не касаются того, что в нарушениях не названо: верный элемент, верная
  связь и верное имя оставь в покое, не переписывай их «аккуратнее».
- `fixes` — только элементы из списка нарушений, и только те поля, которые и
  были нарушением (id элемента обязан существовать в плане).
- `add_elements` / `add_flows` — узлы, которых в плане не хватает (таймер,
  шлюз схождения, недостающий шаг). Свой id не должен совпадать с id из плана;
  на него можно ссылаться в `add_flows` этого же ответа.
- `remove_flows` — только потоки: узел плана удалён быть не может.
- `participants` и `lanes` заполняй только если нарушение про участника, пул
  или дорожку. Иначе оставь списки пустыми: состав процесса утверждён.

Нарушения в текущем плане:
{gaps}

Описание процесса:
{text}

План, к которому относится заплатка (его менять не нужно):
{plan}
"""

# Поля элемента, которые переспрос вправе править. Всё остальное — не нарушение
# методологии, а содержание процесса, и выдумывать его контур не имеет права.
PATCH_ELEMENT_FIELDS = ("participant", "lane", "name", "kind",
                        "event_definition", "timer", "duration",
                        "attached_to", "condition", "documentation")
MAX_PATCH_FIXES = 12
MAX_PATCH_ADDS = 12
# Нарушение про участника опознаётся по этим словам: без него состав не трогается.
PARTICIPANT_GAP_MARKS = ("участник", "пул", "дорожк", "роль", "ролями",
                         "внешне", "контрагент")


def _participant_gap(gaps: List[str]) -> bool:
    lowered = " ".join(gaps).lower()
    return any(mark in lowered for mark in PARTICIPANT_GAP_MARKS)


def apply_plan_patch(plan: Dict[str, Any], patch: Dict[str, Any],
                     gaps: List[str], notes: List[str]) -> Dict[str, Any]:
    """Заплатка поверх плана: правит названное, остальное оставляет как есть.

    Ответ планом целиком тоже поддерживается — модель отвечает так и будет, —
    но тогда состав закрепляет `_merge_skeleton`, а не этот код.
    """
    if "elements" in patch:
        notes.append("переспрос вернул план целиком вместо заплатки — правка "
                     "принята как замена, состав закрепляется каркасом")
        return patch

    fixed: Dict[str, Any] = {key: value for key, value in plan.items()}
    elements = [dict(e) if isinstance(e, dict) else e
                for e in (plan.get("elements") or [])]
    by_id = {_raw_text(e.get("id")): e for e in elements if isinstance(e, dict)}
    known_ids = set(by_id)

    for fix in (patch.get("fixes") if isinstance(patch.get("fixes"), list)
                else [])[:MAX_PATCH_FIXES]:
        if not isinstance(fix, dict):
            continue
        elem = by_id.get(_raw_text(fix.get("id")))
        if elem is None:
            notes.append(f"правка {fix.get('id')} отклонена: такого элемента в "
                         "плане нет — менять можно только названные в нарушениях")
            continue
        fields = [name for name in PATCH_ELEMENT_FIELDS if name in fix]
        if not fields:
            notes.append(f"правка {elem.get('id')} пуста: полей для исправления "
                         "нет")
            continue
        for name in fields:
            elem[name] = fix[name]
        notes.append(f"{elem.get('id')} исправлен по нарушению: "
                     + ", ".join(fields))

    for item in (patch.get("add_elements")
                 if isinstance(patch.get("add_elements"), list)
                 else [])[:MAX_PATCH_ADDS]:
        if not isinstance(item, dict) or not _raw_text(item.get("kind")):
            continue
        elem_id = _raw_text(item.get("id"))
        if not elem_id or elem_id in known_ids:
            notes.append(f"элемент {item.get('name') or elem_id} не добавлен: "
                         "id уже занят или не назван")
            continue
        elements.append(dict(item))
        known_ids.add(elem_id)
        notes.append(f"добавлен элемент {elem_id} «{item.get('name')}» — "
                     "его не хватало по нарушению")
    fixed["elements"] = elements

    flows = [dict(f) if isinstance(f, dict) else f
             for f in (plan.get("flows") or [])]
    drop = {_raw_text(x) for x in (patch.get("remove_flows")
                                   if isinstance(patch.get("remove_flows"), list)
                                   else [])}
    if drop:
        kept = [f for f in flows if isinstance(f, dict)
                and _raw_text(f.get("id")) not in drop]
        notes.append(f"убрано потоков: {len(flows) - len(kept)} по нарушению "
                     "связей")
        flows = kept
    for flow in (patch.get("add_flows")
                 if isinstance(patch.get("add_flows"), list)
                 else [])[:MAX_PATCH_ADDS]:
        if not isinstance(flow, dict):
            continue
        src, tgt = _raw_text(flow.get("source")), _raw_text(flow.get("target"))
        if src not in known_ids or tgt not in known_ids:
            notes.append(f"поток {src} → {tgt} не добавлен: узла с таким id в "
                         "плане нет")
            continue
        flows.append(dict(flow))
        known_ids.add(_raw_text(flow.get("id")))
    fixed["flows"] = flows

    if _participant_gap(gaps):
        for key in ("participants", "lanes"):
            if isinstance(patch.get(key), list):
                fixed[key] = list(plan.get(key) or []) + patch[key]
                notes.append(f"состав дополнен по нарушению про участника: {key}")
    elif isinstance(patch.get("participants"), list) or isinstance(
            patch.get("lanes"), list):
        notes.append("состав не изменён: нарушений про участников в списке не "
                     "было, а значит пулы и дорожки правке не подлежат")
    return fixed


_OWNERSHIP_SYSTEM_PROMPT = """Ты разбираешься, кому что принадлежит в \
BPMN-процессе по его описанию. Верни СТРОГО ОДИН JSON-объект без пояснений:
{"moves": [{"element": "id шага", "participant": "название пула"}], \
"roles": [{"pool": "название пула", "inside": "название организации"}], \
"missing": [{"pool": "название пула", "external": true, "steps": ["id шага"]}]}
moves — переносы шагов в пустые пулы: неси только тот шаг, действие в описании \
которого делает именно названный участник («поставщик подтверждает отгрузку» — \
шаг «Получить подтверждение отгрузки» принадлежит пулу «Поставщик»). Не \
выдумывай участников, не двигай startEvent и endEvent.
roles — те перечисленные пулы, которые по описанию являются ролью, отделом или \
подразделением организации: inside — название этой организации словами из \
описания (самостоятельный участник, клиент или внешняя система ролью не \
является). Посмотреть обязана каждый перечисленный кандидат: молчаливый пропуск \
— не «роль не найдена», а нерешённый случай. Пустым inside не оставляй: если пул \
не роль, просто не включай его в roles.
missing — участники, названные в описании, но отсутствующие среди пулов плана: \
{"pool": "имя словами из описания", "external": true, "steps": ["id шага, \
который делает он"]}. Бери имя участника из текста и только те шаги, действие в \
описании которых совершает он; чужие шаги не отдавай и выдумывать участников не \
нужно. Должность, отдел или подсистема организации, которая в плане уже есть, — \
не участник: верни её ролью, `"external": false` и `"inside"` с названием этой \
организации, и её шаги станут дорожкой её пула. Раздувать коллаборацию лишним \
пулом — ошибка моделирования, а не спасение участника.
Что не относится к случаю — оставляй пустым списком."""


_OWNERSHIP_TEMPLATE = """Пустые пулы, участники которых названы в описании \
(в них нет ни одного шага):
{vacant}

Шаги плана с их теперешним участником:
{elements}

Пулы, которые могут быть ролью, отделом или внутренней системой (роль, не участник):
{role_pools}

Все пулы плана: {pools}

{missing}

Описание процесса:
{text}

Верни moves, roles и missing. Только JSON.
"""


def _q(local: str) -> str:
    return f"{{{BPMN_NS}}}{local}"


def _event_definitions(element: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(тег определения, тег хронометража) события. Пусто для событий без типа
    и для стартовых/конечных: у них тип по умолчанию «none» и определение не
    требуется."""
    if element.get("kind") not in TYPED_EVENT_KINDS:
        return []
    spec = EVENT_DEFINITIONS.get(element.get("event_definition") or "")
    return [spec] if spec else []


class GenerationError(RuntimeError):
    """Сбой генерации с пользовательским сообщением на русском."""


class BPMNGenerator:
    """Генератор BPMN: один вызов LLM + детерминированная починка структуры."""

    def generate(self, text: str) -> Dict[str, Any]:
        """Основной конвейер. Синхронный — из async вызывать через
        asyncio.to_thread."""
        start_time = time.time()
        try:
            if not isinstance(text, str) or not text.strip():
                raise GenerationError("Описание процесса не должно быть пустым")
            if len(text) > MAX_TEXT_CHARS:
                raise GenerationError(
                    f"Описание слишком длинное (максимум {MAX_TEXT_CHARS} символов). "
                    "Сократите его и попробуйте снова."
                )

            trace: List[Dict[str, Any]] = []
            meta: Dict[str, Any] = {"notes": [], "frozen": False,
                                    "skeleton": None}
            structure = self._extract_structure(text, meta)
            gaps = plan_gaps(structure, text)
            trace.append({
                "node": "первый ответ модели",
                "gaps": list(gaps),
                "pools": len(structure.get("participants") or []),
                "elements": len(structure.get("elements") or []),
                "flows": len(structure.get("flows") or []),
                "состав": "утверждён отдельным вызовом" if meta["frozen"]
                          else "один вызов",
            })
            attempts, retry_note = 1, ""
            # Переспрос переписывает весь план — дорогое и рискованное действие
            # (живые прогоны показывали потерянные шаги). Ради одного лишь
            # расхождения с `actors` его не заводим: там дешевле и надёжнее
            # узкий вопрос о том, чьи это шаги (`_clarify_ownership`).
            if plan_gaps(structure, text, with_actors=False):
                attempts = 2
                retry, patch_notes = self._retry_structure(
                    text, structure, gaps, frozen=meta["frozen"])
                meta["notes"].extend(patch_notes)
                reask: Dict[str, Any] = {"node": "переспрос плана",
                                         "gaps_before": len(gaps)}
                trace.append(reask)
                if retry is None:
                    retry_note = "Повторный запрос модели не выполнен"
                    reask.update(kept="первый ответ", outcome=retry_note)
                else:
                    if meta.get("skeleton"):
                        # Переспрос переписывает план целиком, и прогон #43
                        # показал цену: догоняя таймер, модель перекрашивала все
                        # пулы в «роли без хозяина» (6 нарушений rank 0 вместо
                        # 0), гейт сравнивал профили и честно отказывал — вместе
                        # с таймером. Состав заказан отдельным вызовом, поэтому
                        # от повтора берём маршрут, а состав остаётся
                        # утверждённым: правка перестает зависеть от того, что
                        # модель успела испортить по дороге.
                        retry, retry_notes = _merge_skeleton(
                            meta["skeleton"], retry, text)
                        meta["notes"].extend(retry_notes)
                    candidate_gaps = plan_gaps(retry, text)
                    # Первый план остаётся при равенстве: второй вызов обязан
                    # улучшать, а не просто менять местами те же ошибки.
                    lost = _plan_content(structure) - _plan_content(retry)
                    reask.update(gaps_after=len(candidate_gaps),
                                 lost_steps=lost,
                                 profile_before=_gap_profile(gaps),
                                 profile_after=_gap_profile(candidate_gaps),
                                 # Счётчик сам по себе не объясняет отказ: без
                                 # того, что повтор принёс и что унёс, каждый
                                 # разбор стоит отдельного живого прогона.
                                 fixed=_trace_gaps(
                                     [g for g in gaps
                                      if g not in candidate_gaps])[:6],
                                 added=_trace_gaps(
                                     [g for g in candidate_gaps
                                      if g not in gaps])[:6])
                    if not _reask_improves(gaps, candidate_gaps):
                        retry_note = (f"Повторный запрос модели не улучшил план "
                                      f"({len(gaps)} нарушений) — оставлен первый")
                        reask["kept"] = "первый ответ"
                    elif lost > 0:
                        # Нарушения снимаются вырезанными шагами — схема станет
                        # «правильнее» и перестанет описывать процесс. Такой
                        # план лучше первого только на бумаге.
                        retry_note = (f"Повторный запрос модели: нарушений было "
                                      f"{len(gaps)}, стало {len(candidate_gaps)}, "
                                      f"но план потерял {lost} шаг(ов) — оставлен "
                                      "первый")
                        reask["kept"] = "первый ответ"
                    else:
                        retry_note = (f"Повторный запрос модели: нарушений было "
                                      f"{len(gaps)}, стало {len(candidate_gaps)}")
                        reask["kept"] = "переспрос"
                        structure, gaps = retry, candidate_gaps
                    reask["outcome"] = retry_note
            # Пустой пул названного участника и роль, объявившая сама себя, —
            # работа для модели, а не для эвристики: переносить шаги и превращать
            # пулы в дорожки без её слова нельзя.
            structure, patch_notes = self._clarify_ownership(text, structure)
            trace.append({"node": "вопрос о принадлежности",
                          "notes": list(patch_notes)})
            if any("перенесён в пул" in n or "роль «" in n or "добавлен на схему" in n
                   for n in patch_notes):
                gaps = plan_gaps(structure, text)
            repair_steps: List[Dict[str, Any]] = []
            repaired, notes = repair_structure(structure, repair_steps)
            trace.append({"node": "починка структуры", "steps": repair_steps})
            if retry_note:
                notes.append(retry_note)
            notes.extend(meta["notes"])
            notes.extend(patch_notes)
            for note in notes:
                logger.info("Починка структуры: %s", note)
            bpmn_xml = self._generate_bpmn_xml(repaired)
            trace.append({"node": "генерация XML",
                          "elements": len(repaired.get("elements") or []),
                          "flows": len(repaired.get("flows") or [])})

            return {
                "status": "success",
                "bpmn": bpmn_xml,
                "structure": repaired,
                "notes": notes,
                "gaps": gaps,
                "attempts": attempts,
                "trace": trace,
                "time_elapsed": time.time() - start_time,
            }
        except GenerationError as e:
            logger.warning("Генерация отклонена: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "step": "generation",
                "time_elapsed": time.time() - start_time,
            }
        except LLMTruncatedError as e:
            # Повтор здесь бесполезен: max_tokens тот же, ответ обрежется
            # снова. Пользователю нужно сказать, что сокращать.
            logger.error("Ответ модели обрезан при генерации: %s", e)
            return {
                "status": "error",
                "error": "Ответ модели обрезан по лимиту токенов — сократите "
                         "описание процесса и попробуйте снова.",
                "step": "llm_truncated",
                "time_elapsed": time.time() - start_time,
            }
        except LLMRequestTooLargeError as e:
            # Запрос корректен, но не влезает в контекст: «попробуйте позже»
            # здесь было бы ложным обещанием.
            logger.error("Описание не поместилось в контекст модели: %s", e)
            return {
                "status": "error",
                "error": "Описание процесса не помещается в контекст модели — "
                         "сократите его и попробуйте снова.",
                "step": "llm_too_large",
                "time_elapsed": time.time() - start_time,
            }
        except LLMError as e:
            logger.error("LLM недоступен при генерации: %s", e)
            return {
                "status": "error",
                "error": "Сервис генерации временно недоступен. Попробуйте позже.",
                "step": "llm",
                "time_elapsed": time.time() - start_time,
            }
        except ValueError as e:
            logger.error("Некорректный ответ модели при генерации: %s", e)
            return {
                "status": "error",
                "error": "Не удалось построить схему по этому описанию. "
                         "Попробуйте переформулировать запрос.",
                "step": "parse",
                "time_elapsed": time.time() - start_time,
            }
        except Exception as e:  # noqa: BLE001 — наружу только общий сбой
            logger.error("Непредвиденная ошибка генерации: %s", e, exc_info=True)
            return {
                "status": "error",
                "error": "Внутренняя ошибка генерации. Попробуйте позже.",
                "step": "internal",
                "time_elapsed": time.time() - start_time,
            }

    # --- шаг 1: структура от LLM ---

    def _extract_structure(self, text: str,
                           meta: Dict[str, Any]) -> Dict[str, Any]:
        """Состав процесса, затем маршрут внутри него.

        Один выдох «реши, кто участник, и за это же нарисуй 20 элементов»
        живые прогоны #40–#42 платили пулом на каждую должность: каждый такой
        пул — отдельный процесс со своим стартом и финишем, и вместе с
        фрагментацией исчезали развилка, контрагент и число шагов. Состав
        решает маленькая отдельная стадия, а маршрут получает его каркасом и
        новых пулов не заводит.
        """
        meta.setdefault("notes", [])
        meta.setdefault("frozen", False)
        skeleton: Dict[str, Any] = {}
        roster = self._extract_roster(text)
        if roster is not None and any(key in roster
                                      for key in ("elements", "flows", "lanes")):
            # Модель ответила на узкий вопрос планом целиком. Выбрасывать его и
            # переспрашивать маршрут — значит платить двумя вызовами за то, что
            # уже есть: принимаем как одношаговый путь.
            meta["roster"] = "план целиком"
            return roster
        if roster is not None:
            skeleton, notes = parse_roster(roster, text)
            meta["notes"].extend(notes)
        if not skeleton.get("participants"):
            # Состава нет (модель не ответила на этот вызов или состав пуст):
            # генерация обязана остаться одношаговой, а не превратиться в отказ.
            return self._extract_plan(text)
        plan = self._extract_flow(text, skeleton)
        if plan is None:
            return self._extract_plan(text)
        merged, merge_notes = _merge_skeleton(skeleton, plan, text)
        meta["notes"].extend(merge_notes)
        meta["frozen"] = True
        # Каркас нужен и переспросу: он решает, что из ответа модели про состав
        # остаётся в силе.
        meta["skeleton"] = skeleton
        return merged

    def _extract_roster(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            data = call_json(_ROSTER_SYSTEM_PROMPT,
                             f"Описание процесса:\n{text}",
                             temperature=0.0, max_tokens=MAX_ROSTER_TOKENS)
        except (LLMTruncatedError, LLMRequestTooLargeError):
            raise
        except LLMError as e:
            # Только сбой транспорта уводит на одношаговый путь: обрезка и
            # переполнение контекста — объяснимый пользователю отказ, а
            # не разобранный JSON — тот же сбой разбора, что и раньше.
            logger.warning("Состав процесса не выделен, генерация идёт одним "
                           "вызовом: %s", e)
            return None
        return data if isinstance(data, dict) else None

    def _extract_plan(self, text: str) -> Dict[str, Any]:
        data = call_json(_SYSTEM_PROMPT, f"Описание процесса:\n{text}",
                         temperature=0.2, max_tokens=MAX_PLAN_TOKENS)
        if not isinstance(data, dict):
            raise ValueError("ответ модели не объект")
        return data

    def _extract_flow(self, text: str,
                      skeleton: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        question = (
            _skeleton_block(skeleton["participants"], skeleton["lanes"])
            + "\n\nКаждому элементу указывай `participant` названием пула из "
              "этого списка и `lane` — id дорожки из него же (пустая строка, "
              "если дорожки нет).\n\nОписание процесса:\n" + text)
        try:
            data = call_json(_FLOW_SYSTEM_PROMPT, question, temperature=0.2,
                             max_tokens=MAX_PLAN_TOKENS)
        except (LLMError, ValueError) as e:
            logger.warning("Маршрут по утверждённому составу не построен, "
                           "идём одним вызовом: %s", e)
            return None
        if not isinstance(data, dict) or not (data.get("elements") or []):
            return None
        return data

    def _retry_structure(self, text: str, structure: Dict[str, Any],
                         gaps: List[str], frozen: bool = False,
                         ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """Один переспрос по нарушениям: (план, заметки заплатки). None — если
        повтор не мог ничего дать (план не влезает в контекст) или модель
        недоступна: план с нарушениями честнее отказа генерации, а починка и так
        отработает.

        `frozen` — состав утверждён стадией состава: переспрос обязан чинить
        маршрутом и пулами в рамках списка, иначе он возвращал бы роли пулами.
        Ответ моделью — заплатка (`apply_plan_patch`), поэтому корректные
        элементы переспроса не касаются.
        """
        payload = json.dumps(structure, ensure_ascii=False, default=str)
        if len(payload) > MAX_RETRY_PLAN_CHARS:
            logger.info("Повтор генерации пропущен: план %d символов, лимит %d",
                        len(payload), MAX_RETRY_PLAN_CHARS)
            return None, []
        # `str.format` не годится: в шаблоне дословный JSON ответа, и его
        # фигурные скобки format прочёл бы как поля подстановки.
        question = (_RETRY_TEMPLATE
                    .replace("{gaps}", "\n".join(f"- {g}" for g in gaps))
                    .replace("{text}", text)
                    .replace("{plan}", payload))
        try:
            data = call_json(
                _FLOW_SYSTEM_PROMPT if frozen else _SYSTEM_PROMPT,
                question, temperature=0.1, max_tokens=MAX_PLAN_TOKENS)
        except (LLMError, ValueError) as e:
            logger.warning("Повтор генерации не удался, остаётся первый план: %s", e)
            return None, []
        if not isinstance(data, dict):
            return None, []
        notes: List[str] = []
        patched = apply_plan_patch(structure, data, gaps, notes)
        return patched, notes

    def _clarify_ownership(self, text: str,
                           structure: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                                               List[str]]:
        """Чей шаг и чья роль — отдельным узким вопросом, а не ещё одним планом.

        Живые прогоны показали: переписывая план целиком, модель теряла шаги,
        пустой пул внешнего участника оставался пустым (и починка удаляла
        названного в описании участника со схемы), а роли доезжали до пользователя
        пулами. Здесь ответ маленький — «A8 это шаг Поставщика», «HR роль
        ВкусВилла», — решает его модель, а код проверяет адрес: существующий шаг,
        названный пустой пул, организация среди других пулов.
        """
        vacant = _vacant_named_pools(structure, text)
        pools = _plan_pool_names(structure)
        role_pools = _role_candidate_pools(structure, text)
        elements = _raw_dicts(structure.get("elements"))

        def own_steps(pool: str) -> int:
            norm = _norm_name(pool)
            return len([e for e in elements
                        if _norm_name(_raw_text(e.get("participant"))) == norm
                        and _raw_text(e.get("kind") or e.get("type"))
                        not in ("startEvent", "endEvent")])

        # Спрашиваем про трёх кандидатов с меньшим числом своих шагов: такой пул
        # и уезжает с схемы первым (`удаление пустых пулов`), а длинный список
        # модель оставляет без ответа чаще (см. MAX_ROLE_QUESTIONS). Ответ про
        # любого другого кандидата плана принимается так же — `candidates` ниже
        # строится по полному списку.
        asked_roles = sorted(role_pools, key=lambda p: (own_steps(p), p))
        asked_roles = asked_roles[:MAX_ROLE_QUESTIONS]
        by_id = {_raw_text(e.get("id")): e for e in elements}
        steps = [f"{_raw_text(e.get('id'))} «{_raw_text(e.get('name'))}» — "
                 f"сейчас в «{_raw_text(e.get('participant'))}»"
                 for e in elements
                 if _raw_text(e.get("kind") or e.get("type"))
                 not in ("startEvent", "endEvent")]
        # Процесс на несколько действующих лиц, умещённый в один пул, выглядит
        # как потерянный участник, но отдельный вызов ради него живые прогоны
        # не окупили: на вопрос «чьих действий тут нет от имени ВкусВилла» модель
        # отвечала пустым missing (12 из 18 провалов), а пользователь платил за
        # это полной задержкой генерации. Требование перечислить действующих лиц
        # переехало в первый же вызов (см. `actors` в `_SYSTEM_PROMPT`).
        absent = missing_actors(structure)
        # Названная в тексте система без пула — повод спросить и без пустых
        # пулов: шаги её в плане чужие, и только модель скажет, какие. Раньше
        # вопрос заводился исключительно из-за пустых пулов и ролей, поэтому
        # «WMS» доезжал до переспроса планом целиком — а тот отбрасывался чаще.
        # Расхождение с `actors` отдельно спрашивать по-прежнему нечего: пять
        # прогонов подряд давали пустой `missing` (12 случаев из 18) и +4 с
        # пользователю, а имена из `actors` вписываются в вопрос, когда он уже
        # идёт по другому поводу.
        unclaimed = _unclaimed_tokens(pools,
                                      _raw_dicts(structure.get("lanes")), text)
        sought = list(dict.fromkeys(absent + unclaimed))
        if not vacant and not role_pools and not unclaimed:
            return structure, []
        # Пропущенных участников спрашиваем у плана, где их ещё мало, — или когда
        # они противоречат собственному списку `actors`: там имена уже взяты из
        # описания, и дорисовать что-то сверх него модель не может.
        ask_missing = bool(sought) or len(pools) < MAX_POOLS_FOR_MISSING_QUESTION
        question = _OWNERSHIP_TEMPLATE.format(
            vacant="\n".join(
                f"«{v['pool']}» — "
                + ("кандидаты по названию: "
                   + ", ".join(c["id"] for c in v["candidates"][:6])
                   if v["candidates"] else
                   "шагов с таким именем в названиях нет, выбери шаги по смыслу")
                for v in vacant) or "нет",
            elements="\n".join(steps[:MAX_PATCH_ELEMENT_LINES]),
            role_pools=", ".join(f"«{p}»" for p in asked_roles) or "нет",
            pools=", ".join(f"«{p}»" for p in pools),
            missing=(
                ("Эти действующие лица названы в описании (ты сама выписала их "
                 "в actors или они звучат в тексте), но в плане у них нет ни "
                 "пула, ни дорожки: "
                 + ", ".join(f"«{a}»" for a in sought[:MAX_PATCH_MISSING])
                 + ". Назови в missing для каждого его шаги из плана — те, чьё "
                 "действие в описании делает именно он.") if sought else
                "Проверь описание и назови в missing каждое действующее лицо, "
                "которое действует от своего имени («поставщик подтверждает», "
                "«система регистрирует», «клиент подаёт»), с id шагов плана, "
                "которые оно и делает. Пустой missing означает, что таких лиц "
                "в описании нет." if ask_missing else
                "Про отсутствующих участников спрашивать не нужно: верни пустой "
                "список missing."),
            text=text)
        if len(question) > MAX_PATCH_QUESTION_CHARS:
            notes: List[str] = ["Уточнение принадлежности не выполнено: вопрос не "
                                f"помещается в запрос ({len(question)} символов)"]
            _claim_steps_by_name(structure, text, notes)
            return structure, notes
        try:
            data = call_json(_OWNERSHIP_SYSTEM_PROMPT, question, temperature=0.0,
                             max_tokens=2000)
        except (LLMError, ValueError) as e:
            logger.warning("Уточнение принадлежности не удалось: %s", e)
            notes = ["Уточнение принадлежности не выполнено"]
            _claim_steps_by_name(structure, text, notes)
            return structure, notes
        if not isinstance(data, dict):
            notes = []
            _claim_steps_by_name(structure, text, notes)
            return structure, notes
        notes = []
        moves = data.get("moves")
        targets = {_norm_name(v["pool"]): v["pool"] for v in vacant}
        in_the_plan = {_norm_name(p) for p in pools}
        for move in (moves if isinstance(moves, list) else [])[:MAX_PATCH_MOVES]:
            if not isinstance(move, dict):
                continue
            elem_id = _raw_text(move.get("element"))
            pool = _raw_text(move.get("participant"))
            elem = by_id.get(elem_id)
            if elem is None:
                notes.append(f"перенос «{elem_id}» отклонён: такого шага в плане "
                             "нет")
                continue
            if targets.get(_norm_name(pool)) is None:
                # Причина отказа обязана быть настоящей: «среди пустых не было»
                # одинаково выглядит и когда пул полон, и когда его нет вовсе, а
                # это разные ответы модели и разные правки плана.
                notes.append(
                    f"перенос {elem_id} → «{pool}» отклонён: "
                    + ("в нём уже есть свои шаги — вопрос только про пустые пулы"
                       if _norm_name(pool) in in_the_plan else
                       "такого пула в плане нет"))
                continue
            if _raw_text(elem.get("kind") or elem.get("type")) in (
                    "startEvent", "endEvent"):
                notes.append(f"перенос {elem_id} отклонён: стартовое и конечное "
                             "событие переносить нельзя")
                continue
            elem["participant"] = pool
            # Дорожка указана чужого пула: снимаем её — дорожку нового пула
            # починка подберёт по своим правилам.
            elem["lane"] = ""
            notes.append(f"шаг {elem_id} «{_raw_text(elem.get('name'))}» "
                         f"перенесён в пул «{pool}»: это его действие по "
                         "описанию")

        candidates = {_norm_name(p): p for p in role_pools}
        organizations = {_norm_name(p) for p in pools}

        def _role_names() -> Set[str]:
            """Имена, которые план уже знает как роли: дорожки и участники с
            `inside`/`external: false`. Считается заново на каждый ответ,
            потому что ролью что-то становится и здесь же."""
            names = {_norm_name(_raw_text(lane.get("name")))
                     for lane in _raw_dicts(structure.get("lanes"))}
            for item in (structure.get("participants") or []):
                if isinstance(item, dict) and (
                        item.get("external") is False
                        or _raw_text(item.get("inside"))):
                    names.add(_norm_name(_raw_text(item.get("name"))))
            names.discard("")
            return names

        declared_roles = data.get("roles")
        for role in (declared_roles if isinstance(declared_roles, list)
                    else [])[:MAX_PATCH_MOVES]:
            if not isinstance(role, dict):
                continue
            pool = _raw_text(role.get("pool"))
            inside = _raw_text(role.get("inside"))
            canonical = candidates.get(_norm_name(pool))
            if canonical is None:
                notes.append(f"«{pool}» ролью не объявлен: среди кандидатов такого "
                             "пула не было")
                continue
            if not inside or _norm_name(inside) == _norm_name(pool):
                notes.append(f"роль «{pool}» не объявлена: модель не назвала "
                             "организацию, которой она принадлежит")
                continue
            if _norm_name(inside) not in organizations:
                if _generic_actor_name(inside):
                    # Родовое слово вместо имени («inside: "Организация"») — не
                    # название, а способ сказать «я роль, хозяина назови сам».
                    # Проверка «такое слово есть в описании» через него проходит,
                    # и контур заводил пул «Организация», в который сливались все
                    # роли с нулём перенесённых шагов: участников схемы сверять с
                    # описанием становилось нечем. Хозяина из плана не подставляем
                    # (см. `_declare_role_lanes`) — роль остаётся нарушением.
                    notes.append(f"роль «{pool}» не объявлена: «{inside}» — "
                                 "родовое слово, а не название организации из "
                                 "описания")
                    continue
                if _norm_name(inside) in _role_names():
                    # «Водитель» — тоже сотрудник, а не организация. По одному
                    # лишь слову «имя звучит в описании» этот ответ заводил пул
                    # «водитель» и отправлял в него «Перевозчика» дорожкой:
                    # организация уезжала к своему же сотруднику (тот же дефект,
                    # что и угаданный хозяин в прогоне #37). Хозяином может быть
                    # только то, что план знает как пул; ответ с ролью остаётся
                    # нарушением, а не тихой инверсией состава.
                    notes.append(f"роль «{pool}» не объявлена: «{inside}» — тоже "
                                 "роль, а не организация: хозяином может быть "
                                 "только пул")
                    continue
                if not _mentioned(inside, _text_words(text)):
                    notes.append(f"роль «{pool}» не объявлена: организации "
                                f"«{inside}» в описании нет")
                    continue
                # Приёмник назван моделью и взят из описания: без него роли
                # остаются пулами, а заводим мы его по слову модели, не по своей
                # догадке.
                structure.setdefault("participants", []).append(inside)
                organizations.add(_norm_name(inside))
                notes.append(f"пул «{inside}» — организация из описания: ей "
                             "принадлежат объявленные роли")
            _mark_role(structure, canonical, inside)
            notes.append(f"пул «{canonical}» — роль «{inside}» по описанию: его "
                         "шаги станут дорожкой этого пула")
        # Что модель забрала себе как «участника, которого в плане не было»:
        # имя обязано звучать в описании, шаг — существовать, а пул-донор —
        # остаться с действиями. Без этих рамок вопрос возвращал бы выдуманных
        # участников и пустые пулы.
        def step_count(pool: str) -> int:
            return len([e for e in elements
                        if _norm_name(_raw_text(e.get("participant")))
                        == _norm_name(pool)
                        and _raw_text(e.get("kind") or e.get("type"))
                        not in ("startEvent", "endEvent")])

        known = {_norm_name(p) for p in pools}
        missing = data.get("missing")
        for item in (missing if isinstance(missing, list) else [])[:MAX_PATCH_MISSING]:
            if not isinstance(item, dict):
                continue
            pool = _raw_text(item.get("pool"))
            if not pool or _norm_name(pool) in known:
                continue
            if not _mentioned(pool, _text_words(text)):
                notes.append(f"участник «{pool}» не добавлен: в описании его нет")
                continue
            claimed = []
            for elem_id in (item.get("steps") or [])[:MAX_PATCH_MISSING]:
                elem = by_id.get(_raw_text(elem_id))
                if elem is None or _raw_text(
                        elem.get("kind") or elem.get("type")) in (
                        "startEvent", "endEvent"):
                    continue
                donor = _raw_text(elem.get("participant"))
                # Донор без действий — не аргумент против шага, который называет
                # самого участника: имя шага говорит о владельце прямо, а пул без
                # своих действий — честная пустота, которую починка и закроет.
                # Живые прогоны теряли здесь названного в описании участника
                # целиком («WMS», «Клиент»).
                names_the_pool = _norm_name(pool) in _norm_name(
                    _raw_text(elem.get("name")))
                if (step_count(donor) <= 1 and not names_the_pool
                        and _norm_name(donor) != _norm_name(pool)):
                    notes.append(f"шаг {_raw_text(elem.get('id'))} в «{pool}» не "
                                 "перенесён: после переноса его пул остался без "
                                 "действий")
                    continue
                claimed.append(elem)
            if not claimed:
                notes.append(f"участник «{pool}» не добавлен: его шагов в плане "
                             "модель не назвала")
                continue
            lane = _lane_by_ref(pool, _raw_dicts(structure.get("lanes")))
            if lane is not None:
                # Ответ назвал ролью то, что состав уже посадил в чужой пул
                # дорожкой: «Водитель» — дорожка «Перевозчика». Пул с именем
                # роли оставил бы хозяина без единого шага, и `_drop_vacant_pools`
                # убрал бы названного в описании контрагента со схемы (прогон
                # #43, warehouse r1: «Пул „Перевозчик" удалён» → нет
                # `expected_participants`). Шаги уходят в пул-хозяин этой
                # дорожкой: и участник на схеме, и роль не раздута в пул.
                for elem in claimed:
                    elem["participant"] = lane["participant"]
                    elem["lane"] = lane["id"]
                notes.append(
                    f"шаги «{pool}» записаны в пул «{lane['participant']}» "
                    f"дорожкой «{pool}»: состав уже назвал его ролью, а не "
                    "участником")
                continue
            inside = _raw_text(item.get("inside"))
            as_role = item.get("external") is False and bool(inside)
            new_pool: Dict[str, Any] = {"name": pool, "external": not as_role}
            if as_role:
                new_pool["inside"] = inside
            structure.setdefault("participants", []).append(new_pool)
            known.add(_norm_name(pool))
            for elem in claimed:
                elem["participant"] = pool
                elem["lane"] = ""
            notes.append(f"участник «{pool}» добавлен на схему: назван в описании, "
                         f"его {len(claimed)} шаг(ов) были записаны чужим пулом")

        # Что модель не забрала на себя, но в плане написано её собственными
        # словами, переносится здесь: иначе пустой пул доживёт до починки и
        # участник из описания исчезнет со схемы.
        _claim_steps_by_name(structure, text, notes)
        return structure, notes

    # --- шаг 2: XML ---

    def _generate_bpmn_xml(self, structure: Dict[str, Any]) -> str:
        definitions = ET.Element(_q("definitions"), {
            "id": "Definitions_1",
            "targetNamespace": "http://bpmn.io/schema/bpmn",
        })

        participants: List[Dict[str, Any]] = structure["participants"]
        lanes: List[Dict[str, Any]] = structure.get("lanes") or []
        elements: List[Dict[str, Any]] = structure["elements"]
        flows: List[Dict[str, Any]] = structure["flows"]
        element_by_id = {e["id"]: e for e in elements}

        # Индексы считаем один раз: обход всех потоков ради каждого элемента
        # давал бы O(n²) и мешал читать код emission-шага.
        incoming: Dict[str, List[str]] = {}
        outgoing: Dict[str, List[str]] = {}
        for f in flows:
            if f["kind"] != "sequence":
                continue
            outgoing.setdefault(f["source"], []).append(f["id"])
            incoming.setdefault(f["target"], []).append(f["id"])
        lane_refs: Dict[str, List[str]] = {}
        for e in elements:
            if e.get("lane"):
                lane_refs.setdefault(e["lane"], []).append(e["id"])
        lanes_by_pool: Dict[str, List[Dict[str, Any]]] = {}
        for lane in lanes:
            lanes_by_pool.setdefault(lane["participant"], []).append(lane)

        process_ids: Dict[str, str] = {}
        participant_ids: Dict[str, str] = {}
        for idx, p in enumerate(participants, 1):
            process_ids[p["name"]] = f"Process_{idx}"
            participant_ids[p["name"]] = f"Participant_{idx}"

        processes: Dict[str, ET.Element] = {}
        for idx, p in enumerate(participants, 1):
            process = ET.SubElement(definitions, _q("process"), {
                "id": process_ids[p["name"]],
                "isExecutable": "true",
            })
            processes[p["name"]] = process
            # laneSet идёт до потоковых узлов и содержит только ссылки
            # flowNodeRef: сами узлы и sequenceFlow остаются детьми process —
            # ровно так пишет bpmn-js и так читает инвентарь core/bpmn_edits.
            pool_lanes = lanes_by_pool.get(p["name"]) or []
            if not pool_lanes:
                continue
            lane_set = ET.SubElement(process, _q("laneSet"), {"id": f"LaneSet_{idx}"})
            for lane in pool_lanes:
                lane_elem = ET.SubElement(lane_set, _q("lane"), {
                    "id": lane["id"],
                    "name": lane["name"],
                })
                for ref in lane_refs.get(lane["id"], []):
                    ET.SubElement(lane_elem, _q("flowNodeRef")).text = ref

        for e in elements:
            process = processes[e["participant"]]
            attrs = {"id": e["id"], "name": e["name"]}
            if e["kind"] == "boundaryEvent" and e.get("attached_to"):
                # Граничное событие без хозяина не существует: bpmn-js рисует
                # его отдельным кружком, который никто не запускает.
                attrs["attachedToRef"] = e["attached_to"]
                attrs["cancelActivity"] = "true"
            elif e["kind"] in GATEWAY_KINDS and e.get("default"):
                attrs["default"] = e["default"]
            elem = ET.SubElement(process, _q(e["kind"]), attrs)
            # Порядок детей по XSD: documentation, затем определения событий,
            # и только потом incoming/outgoing — иначе bpmn-js не видит тип
            # события и рисует пустой кружок.
            if e.get("documentation"):
                ET.SubElement(elem, _q("documentation")).text = e["documentation"]
            for tag, timing in _event_definitions(e):
                definition = ET.SubElement(elem, _q(tag))
                if timing:
                    ET.SubElement(definition, _q(timing)).text = (
                        e.get("timer") or DEFAULT_TIMER_DURATION)
            for flow_id in incoming.get(e["id"], []):
                ET.SubElement(elem, _q("incoming")).text = flow_id
            for flow_id in outgoing.get(e["id"], []):
                ET.SubElement(elem, _q("outgoing")).text = flow_id

        for f in flows:
            if f["kind"] != "sequence":
                continue
            source_participant = element_by_id[f["source"]]["participant"]
            process = processes[source_participant]
            flow_elem = ET.SubElement(process, _q("sequenceFlow"), {
                "id": f["id"],
                "sourceRef": f["source"],
                "targetRef": f["target"],
            })
            if f.get("condition"):
                cond = ET.SubElement(flow_elem, _q("conditionExpression"))
                cond.text = f["condition"]

        collaboration = ET.SubElement(definitions, _q("collaboration"),
                                      {"id": "Collaboration_1"})
        for p in participants:
            ET.SubElement(collaboration, _q("participant"), {
                "id": participant_ids[p["name"]],
                "name": p["name"],
                "processRef": process_ids[p["name"]],
            })
        for f in flows:
            if f["kind"] != "message":
                continue
            attrs = {
                "id": f["id"],
                "sourceRef": f["source"],
                "targetRef": f["target"],
            }
            if f.get("condition"):
                attrs["name"] = f["condition"]
            ET.SubElement(collaboration, _q("messageFlow"), attrs)

        # Пустой DI-скелет: координаты достраивает фронтенд (bpmn-auto-layout).
        diagram = ET.SubElement(definitions, f"{{{BPMNDI_NS}}}BPMNDiagram",
                                {"id": "BPMNDiagram_1"})
        ET.SubElement(diagram, f"{{{BPMNDI_NS}}}BPMNPlane", {
            "id": "BPMNPlane_1",
            "bpmnElement": "Collaboration_1",
        })

        return XML_DECLARATION + ET.tostring(definitions, encoding="unicode")


# --- что в плане модели противоречит методологии ---
#
# Проверки работают по сырому ответу, потому что чинить эти нарушения
# детерминированно честно нельзя: пустой пул можно удалить, но нельзя
# угадать, куда модель хотела положить шаги; у события можно выдумать таймер,
# но не факт, что там таймер. Такие нарушения — повод один раз переспросить
# модель, а не «доводить» схему эвристикой. Каждая проверка терпима к мусору:
# план недоверенный, и падать на нём нельзя.

def _raw_dicts(value: Any) -> List[Dict[str, Any]]:
    return [i for i in (value or []) if isinstance(i, dict)] if isinstance(
        value, list) else []


def _raw_text(value: Any) -> str:
    return str(value or "").strip()


def _plan_content(raw: Dict[str, Any]) -> int:
    """Содержание плана — шаги без учёта стартового и конечного событий.

    Тем же признаком пустой пул считает `plan_gaps`, поэтому мера одинаковая с
    двух сторон маршрута: переспрос, снявший нарушения вырезанными шагами, не
    считается улучшением.
    """
    if not isinstance(raw, dict):
        return 0
    return len([e for e in _raw_dicts(raw.get("elements"))
                if _raw_text(e.get("kind") or e.get("type"))
                not in ("startEvent", "endEvent")])


# Аббревиатуры, которые участником быть не могут: атрибуты, форматы и
# организационно-правовые формы, а не организации и системы.
_NON_PARTICIPANT_ACRONYMS = {
    "SLA", "KPI", "OKR", "API", "URL", "URI", "XML", "JSON", "HTML", "BPMN",
    "PDF", "XLSX", "XLS", "DOCX", "SQL", "HTTP", "HTTPS", "SMTP", "IMAP",
    "JWT", "UUID", "GUID", "ООО", "АО", "ПАО", "ГУП", "МУП", "РФ", "СРО",
    "КБ", "АУ", "НК", "ОК",
}
_ACRONYM_RE = re.compile(r"\b[A-ZА-ЯЁ]{2,}\b")
# Именованный участник по русской орфографии: заглавное слово не в начале
# фразы. Аббревиатура («WMS») — не единственный способ назвать контрагента:
# «Перевозчик», «Клиент», «Система мониторинга» план-гейт раньше не видел
# вовсе, и «план-гейт про него молчал» был вторым по величине источником
# провала `expected_participants` (4 из 14 в прогоне #40). Строчные
# «перевозчик»/«кладовщик» не ловятся намеренно: должность участником не
# считает план, а не код.
_PROPER_RE = re.compile(
    r"(?<![А-Яа-яЁёA-Za-z0-9])[А-ЯЁ][а-яё]{2,}(?![А-Яа-яЁёA-Za-z])")
_PHRASE_START = ".!?:;\n\r"


def _proper_names(text: str) -> List[str]:
    """Заглавные имена собственные описания, стоящие внутри фразы."""
    out: List[str] = []
    for found in _PROPER_RE.finditer(text or ""):
        head = (text or "")[:found.start()].rstrip()
        if not head or head[-1] in _PHRASE_START:
            continue
        if found.group(0) not in out:
            out.append(found.group(0))
    return out


def _text_words(text: str) -> List[str]:
    return [w for w in (_norm_name(m) for m in re.findall(r"[\w]+", text)) if w]


def _name_parts(name: str) -> List[str]:
    """Значимые части имени: «Бюджетный контролёр» — это два слова, и по одному
    из него участник в тексте не ищется."""
    return [t for t in (_norm_name(w) for w in re.findall(r"[\w]+", name)) if t]


# Родовые слова вместо имени участника. В описании они встречаются чаще, чем
# настоящие имена («организация держит дежурство»), поэтому проверки «такое имя
# есть в тексте» через них не работают. Пул с родовым именем глотает роли: живой
# прогон слил три роли в «Организацию» с нулём перенесённых шагов, после чего
# участников схемы сверять с описанием стало не с чем.
GENERIC_ACTOR_NAMES = frozenset({
    "организация", "организации", "компания", "компании", "предприятие",
    "предприятия", "система", "системы", "сервис", "сервисы", "служба",
    "службы", "отдел", "отделы", "подразделение", "подразделения", "ведомство",
    "ведомства", "департамент", "департаменты", "участник", "участники",
    "сторона", "стороны", "пользователь", "пользователи", "сотрудник",
    "сотрудники", "агент", "агенты",
})


def _generic_actor_name(name: Any) -> bool:
    """Родовое слово, а не название действующего лица."""
    return _norm_name(name) in GENERIC_ACTOR_NAMES


def _mentioned(name: str, words: List[str]) -> bool:
    """Упомянуто ли имя в описании с любым окончанием.

    Точное сравнение ломает русскую морфологию: «Получатель» в тексте живёт как
    «получателю», и схема считалась бы потерянной. Основы сравниваются с
    запасом в пару символов, короткие слова (аббревиатуры) — только целиком.
    Составное имя считается упомянутым, только если в тексте есть каждая его
    часть: иначе «Бюджетный контролёр» не находили бы даже те схемы, где он
    есть, и переспрос требовал бы переименования там, где всё в порядке.
    """
    parts = _name_parts(name)
    if not parts:
        return False
    for part in parts:
        if part in words:
            continue
        # У коротких названий основа почти совпадает с самим словом: «банк» в
        # тексте живёт «банком» и «банка», и отбрасывать два символа нечего.
        stem = part[:-1] if len(part) >= 4 else ""
        if stem and any(word.startswith(stem) for word in words):
            continue
        return False
    return True


# Слова ярлыка, которые говорят «что это вообще такое», но не «кто это». Модель
# вправе назвать организацию составным ярлыком — «Организация-склад»,
# «Система WMS», — и тогда проверка «каждая часть имени есть в описании»
# отбрасывает имя из-за родового компонента. Живой прогон #43 показал цену:
# хозяин «Организация-склад» был отброшен, и шесть ролей, у которых он был
# указан, стали отдельными пулами — 9 пулов, все потоки между ними стали
# сообщениями, оба шлюза понижены до задач.
NAME_QUALIFIER_WORDS = frozenset({
    "организация", "организации", "компания", "компании", "предприятие",
    "предприятия", "сторона", "стороны", "участник", "участники", "система",
    "системы", "подразделение", "подразделения", "субъект", "субъекты",
})


def _actor_name_from_text(name: str, words: List[str]) -> str:
    """Значимая часть ярлыка, если она названа в описании: «Организация-склад»
    → «Склад». Имя берётся словом из текста, а не выдумывается: не осталось
    ничего, кроме родовых слов, — участника в описании нет."""
    kept = [t for t in re.findall(r"[\w]+", name or "")
            if _norm_name(t) not in NAME_QUALIFIER_WORDS]
    if not kept:
        return ""
    named = [t if (t[:1].isupper() or t.isupper()) else t.capitalize()
             for t in kept]
    return " ".join(named) if _mentioned(" ".join(named), words) else ""


def _unclaimed_tokens(pools: List[str], lanes: List[Dict[str, Any]],
                      text: str) -> List[str]:
    """Названные в описании системы и участники, у которых в плане нет ни пула,
    ни дорожки.

    Один источник признака и для нарушения (`_participant_gaps`), и для уточняющего
    вопроса: разъехавшиеся правила давали бы план, где модель чинит дефект, о
    котором её не спрашивали, и наоборот.
    """
    if not (text or "").strip():
        return []
    named = {_norm_name(p) for p in pools} | {
        _norm_name(_raw_text(lane.get("name"))) for lane in lanes}
    named.discard("")
    out: List[str] = []
    tokens = {m for m in _ACRONYM_RE.findall(text)
              if m not in _NON_PARTICIPANT_ACRONYMS} | set(_proper_names(text))
    for token in sorted(tokens):
        low = token.lower()
        if any(low == name or low in name or (name in low and len(name) >= 3)
               for name in named):
            continue
        out.append(token)
    return out


def _participant_gaps(pools: List[str], lanes: List[Dict[str, Any]],
                      text: str) -> List[str]:
    """Два дрейфа имён участников, которые починка не устранит.

    Выдумывать содержание нельзя, поэтому оба идут в переспрос конкретной
    фразой: схема, где названная в описании система «WMS» наречена «Складом»,
    теряет прослеживаемость, а схема без «WMS» теряет обещанное взаимодействие.
    """
    if not (text or "").strip():
        return []
    words = _text_words(text)
    named = {_norm_name(p) for p in pools} | {
        _norm_name(_raw_text(lane.get("name"))) for lane in lanes}
    named.discard("")
    gaps: List[str] = []
    for pool in pools:
        if not _mentioned(pool, words):
            gaps.append(f"пул «{pool}» не упоминается в описании: назови "
                        "участника тем словом, которое в тексте, и обнови "
                        "participant у его шагов")
    for token in _unclaimed_tokens(pools, lanes, text):
        gaps.append(f"в описании назван участник «{token}», а в схеме его нет: "
                    f"заведи пул «{token}» с его шагами, либо дорожку, если "
                    "это подсистема другой организации")
    return gaps


# Срок, названный в описании: «в течение четырёх часов», «по истечении двух
# дней». Требование «дней» через запятую от «в течение» не даёт ловить
# «не больше 14 дней» — там срок условие признания брака, а не ожидание.
_WAIT_UNIT = r"(?:секунд|минут|час|дн|недел|месяц)"
_DEADLINE_RE = re.compile(
    rf"(?:в течение|в течении|на протяжении|в срок|по истечени|по прошествии|"
    rf"после истечени)\s+[^.;,]{{0,24}}?{_WAIT_UNIT}\w*", re.I)


def _deadline_gaps(elements: List[Dict[str, Any]], text: str) -> List[str]:
    """Ожидание с названным сроком обязано быть таймером.

    Правила моделирования это требуют, но починка таймер не поставит: по какой
    ветке процесс уходит после истечения, знает только модель. Значит нарушение
    едет в переспрос конкретным сроком из текста, а не в таймер, дорисованный
    эвристикой ради метрики.
    """
    if not (text or "").strip():
        return []
    match = _DEADLINE_RE.search(text)
    if not match:
        return []
    for elem in elements:
        if _raw_text(elem.get("kind") or elem.get("type")) not in TYPED_EVENT_KINDS:
            continue
        definition = _raw_text(elem.get("event_definition")
                               or elem.get("event_type")).lower()
        if definition == "timer" or _raw_text(elem.get("timer")
                                              or elem.get("duration")):
            return []
    clause = re.sub(r"\s+", " ", match.group(0)).strip()
    return [f"описание задаёт ожидание («{clause}»), а в плане нет ни одного "
            "таймера: добавь boundaryEvent с event_definition=\"timer\" и "
            "attached_to на шаге ожидания (или промежуточное таймерное событие) "
            "и ветку обработки по истечении"]


def _plan_pool_names(raw: Dict[str, Any]) -> List[str]:
    return [p for p in (_raw_text(i.get("name") if isinstance(i, dict) else i)
                        for i in (raw.get("participants") or [])) if p]


def _step_claimed_pools(raw: Dict[str, Any]) -> Set[str]:
    """Пулы, у которых есть хотя бы один шаг. Стартовое и конечное событие
    шагом не считается; неизвестный модели тип узла трактуем как шаг."""
    return {_norm_name(_raw_text(e.get("participant")))
            for e in _raw_dicts(raw.get("elements"))
            if _raw_text(e.get("kind") or e.get("type"))
            not in ("startEvent", "endEvent")}


def _receives_merged_steps(pool: str, pools: List[str],
                           lanes: List[Dict[str, Any]]) -> bool:
    """Пул, который починка наполнит сама: другой пул объявил дорожку с его
    именем, и `_merge_role_pools` перенесёт его шаги сюда. Звать этот пул
    пустым — ложное нарушение."""
    wanted = _norm_name(pool)
    for other in pools:
        if _norm_name(other) == wanted:
            continue
        lane = _lane_named_like_pool(other, lanes)
        if lane is not None and _norm_name(
                _raw_text(lane.get("participant"))) == wanted:
            return True
    return False


def _vacant_pools(raw: Dict[str, Any]) -> List[str]:
    """Пулы без единого шага, которым эти шаги не придут из чужого пула."""
    if not isinstance(raw, dict):
        return []
    pools = _plan_pool_names(raw)
    lanes = _raw_dicts(raw.get("lanes"))
    claimed = _step_claimed_pools(raw)
    return [p for p in pools if _norm_name(p) not in claimed
            and not _receives_merged_steps(p, pools, lanes)]


def _mark_role(structure: Dict[str, Any], canonical: str, inside: str) -> None:
    """Объявить участника ролью языком плана: `_declare_role_lanes` и
    `_merge_role_pools` понимают пару `external: false` + `inside`."""
    participants = structure.get("participants")
    if not isinstance(participants, list):
        return
    for index, item in enumerate(participants):
        name = _raw_text(item.get("name") if isinstance(item, dict) else item)
        if _norm_name(name) == _norm_name(canonical):
            participants[index] = {"name": canonical, "external": False,
                                   "inside": inside}
            return


def _role_candidate_pools(raw: Dict[str, Any], text: str = "") -> List[str]:
    """Пулы, которые могут оказаться ролью: без дорожек вообще или с дорожкой,
    названной как сам пул. Пул с дорожками других имён — организация, и
    спрашивать про неё незачем.

    Одних «самоимённых» дорожек для вопроса мало: живые прогоны показали роли
    («Бухгалтерия», «Отдел качества»), которые модель завела пулами вообще без
    дорожек — их тоже надо называть моделью, а не угадывать по коду.

    Пустой пул, названный в описании, — тот же случай: наполнить его нечем,
    убрать нельзя, и роль организации остаётся единственным выходом. Модель
    предлагала его сама и получала отказ «среди кандидатов такого пула не
    было». Выдуманный пустой пул кандидатом не становится: сворачивать его в
    дорожку значило бы легализовать участника, которого в тексте нет.
    """
    pools = _plan_pool_names(raw)
    if len(pools) < 2:
        # Сворачивать некуда: единственному пулу не быть ролью другого, а вопрос
        # о нём стоил бы пользователю полную задержку генерации.
        return []
    lanes = _raw_dicts(raw.get("lanes"))
    claimed = _step_claimed_pools(raw)
    vacant = {_norm_name(p) for p in _vacant_pools(raw)}
    words = _text_words(text)
    by_pool: Dict[str, List[str]] = {}
    for lane in lanes:
        by_pool.setdefault(_norm_name(_raw_text(lane.get("participant"))), []) \
            .append(_norm_name(_raw_text(lane.get("name"))))
    out: List[str] = []
    for pool in pools:
        norm = _norm_name(pool)
        if norm not in claimed and not (norm in vacant
                                        and _mentioned(pool, words)):
            continue
        if _receives_merged_steps(pool, pools, lanes):
            continue
        own = by_pool.get(norm, [])
        if not own or all(n == norm for n in own):
            out.append(pool)
    return out


def _vacant_named_pools(raw: Dict[str, Any], text: str) -> List[Dict[str, Any]]:
    """Пустые пулы, названные в описании, с шагами-кандидатами из чужих пулов.

    Кандидат — шаг, в названии которого есть это имя: «Получить подтверждение
    отгрузки от поставщика» почти наверняка действие «Поставщика», записанное
    чужим участником. Один источник и для формулировки нарушения, и для
    точечной правки, чтобы правила не разъезжались между двумя местами.
    """
    if not (text or "").strip():
        return []
    words = _text_words(text)
    elements = _raw_dicts(raw.get("elements"))
    out: List[Dict[str, Any]] = []
    for pool in _vacant_pools(raw):
        if not _mentioned(pool, words):
            continue
        candidates = [{
            "id": _raw_text(e.get("id")),
            "name": _raw_text(e.get("name")),
            "participant": _raw_text(e.get("participant")),
        } for e in elements
            if _raw_text(e.get("kind") or e.get("type"))
            not in ("startEvent", "endEvent")
            and _norm_name(_raw_text(e.get("participant"))) != _norm_name(pool)
            and _mentioned(pool, _text_words(_raw_text(e.get("name"))))]
        out.append({"pool": pool, "candidates": candidates})
    return out


def _claim_steps_by_name(structure: Dict[str, Any], text: str,
                         notes: List[str]) -> None:
    """Однозначная пара «пустой пул — шаг, названный в его честь» переносится без
    ответа модели.

    Узкий вопрос возвращается не всегда: транспорт падает, ответ пуст, шаг модель
    указывает в другом пуле. Тогда починка удаляла пустой пул, а вместе с ним и
    участника, названного в описании (`expected_participants` из схемы уезжал).
    Переносим только там, где прочтение одно: кандидат один и донор после переноса
    остаётся с шагом. Двусмысленные пулы — решение за моделью.
    """
    elements = _raw_dicts(structure.get("elements"))
    # Роль, которую модель уже объявила (`inside` или `external: false`), — не
    # контрагент: её шаги станут дорожкой чужого пула, и наполнять её собственным
    # шагом нельзя.
    declared_roles = {_norm_name(_raw_text(
        item.get("name") if isinstance(item, dict) else item))
        for item in (structure.get("participants") or [])
        if isinstance(item, dict) and (_raw_text(item.get("inside"))
                                       or item.get("external") is False)}

    def steps_of(pool: str) -> List[Dict[str, Any]]:
        return [e for e in elements
                if _norm_name(_raw_text(e.get("participant"))) == _norm_name(pool)
                and _raw_text(e.get("kind") or e.get("type"))
                not in ("startEvent", "endEvent")]

    for entry in _vacant_named_pools(structure, text):
        pool, candidates = entry["pool"], entry["candidates"]
        if _norm_name(pool) in declared_roles or len(candidates) != 1:
            continue
        elem = next((e for e in elements
                     if _raw_text(e.get("id")) == candidates[0]["id"]), None)
        if elem is None:
            continue
        donor = _raw_text(elem.get("participant"))
        if len(steps_of(donor)) < 2:
            notes.append(f"шаг {candidates[0]['id']} в пул «{pool}» не перенесён: "
                         f"после переноса «{donor}» остался без действий")
            continue
        elem["participant"] = pool
        elem["lane"] = ""
        notes.append(f"шаг {candidates[0]['id']} «{candidates[0]['name']}» перенесён "
                     f"в пул «{pool}»: он назван в его имени, а участник из "
                     "описания без шагов был бы удалён починкой")


def _same_actor(declared: str, known: Any) -> bool:
    """То же действующее лицо или нет: «Система WMS» против «WMS», «Служба
    поддержки» против «Поддержка». Сверка толерантная — и у оракула, и здесь,
    иначе модель ловилась бы на падежах, а не на потерянных участниках."""
    a, b = _norm_name(_raw_text(declared)), _norm_name(_raw_text(known))
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def missing_actors(raw: Dict[str, Any]) -> List[str]:
    """Действующие лица из `actors`, у которых в плане нет ни пула, ни дорожки.

    `actors` — выписка самой модели из описания, поэтому претензия здесь не
    выдуманная кодом подпись, а противоречие плана самому себе.
    """
    if not isinstance(raw, dict):
        return []
    pools = _plan_pool_names(raw)
    lanes = [_raw_text(l.get("name")) for l in _raw_dicts(raw.get("lanes"))]
    out: List[str] = []
    for actor in [a for a in (raw.get("actors") or [])
                  if isinstance(a, str)][:MAX_PARTICIPANTS]:
        name = _raw_text(actor)
        if not name or name in out:
            continue
        if any(_same_actor(name, known) for known in pools + lanes):
            continue
        out.append(name)
    return out


def plan_gaps(raw: Dict[str, Any], text: str = "",
              with_actors: bool = True) -> List[str]:
    """Нарушения методологии в ответе модели, читаемые без починки.

    `text` — описание процесса: по нему проверяются имена участников (см.
    `_participant_gaps`). Без текста эти правила молчат, а не угадывают.
    """
    gaps: List[str] = []
    if not isinstance(raw, dict):
        return ["план не является JSON-объектом"]

    pools = _plan_pool_names(raw)
    lanes = _raw_dicts(raw.get("lanes"))
    elements = _raw_dicts(raw.get("elements"))
    flows = _raw_dicts(raw.get("flows"))
    gaps.extend(_participant_gaps(pools, lanes, text))
    gaps.extend(_deadline_gaps(elements, text))

    # `actors` — выписка самой модели из описания, и расхождение с ней ловит
    # потерянного участника там, где коду выдумывать имя нельзя. Эти нарушения
    # не переписывают план целиком: их чинит узкий вопрос о принадлежности шагов
    # (`_clarify_ownership`), поэтому `with_actors=False` отдаёт список без них.
    if with_actors:
        for name in missing_actors(raw):
            gaps.append(f"ты сама назвала «{name}» действующим лицом описания, но "
                        "в плане нет ни пула, ни дорожки с таким именем: объяви "
                        "его участником и запиши за ним его шаги из описания")

    # Пустой пул: ни одного шага у участника (см. `_vacant_pools` — пул, который
    # починка наполнит слиянием, пустым не считается). Формулировка решает, что
    # модель сделает с пулом: «такого участника нет» она читает как разрешение
    # удалить его вместе с его шагами, которые просто записались в чужом пуле.
    named_vacant = {v["pool"]: v["candidates"]
                    for v in _vacant_named_pools(raw, text)}
    for pool in _vacant_pools(raw):
        if pool in named_vacant:
            candidates = ", ".join(
                f"{c['id']} «{c['name']}»" for c in named_vacant[pool][:3])
            gaps.append(f"пул «{pool}» без единого шага — в нём только старт "
                        "и финиш. Участник назван в описании, поэтому убирать "
                        "его нельзя: верни его действия в его пул (participant"
                        "=«pool»), а не записывай их чужим участником"
                        + (f": возможно, это {candidates}" if candidates else ""))
        else:
            gaps.append(f"пул «{pool}» без единого шага — в нём только старт и "
                        "финиш; либо это дорожка основного пула, либо такого "
                        "участника нет")

    # Роль, раздутая в пул: имя участника совпадает с именем дорожки в другом
    # пуле. Сильный признак — модель сама объявила эту сущность дорожкой.
    for pool in pools:
        for lane in lanes:
            lane_pool = _norm_name(_raw_text(lane.get("participant")))
            if lane_pool and lane_pool != _norm_name(pool) \
                    and _norm_name(_raw_text(lane.get("name"))) == _norm_name(pool):
                gaps.append(f"«{pool}» объявлен пулом и одновременно дорожкой в "
                            f"пуле «{_raw_text(lane.get('participant'))}» — "
                            "роли одной организации живут дорожками одного пула")
                break

    # Пул, у которого все дорожки названы его собственным именем, — роль,
    # объявившая саму себя: «Бюджетный контролёр» с дорожкой «Бюджетный
    # контролёр». Такого участника сливать некуда — приёмника в плане нет, и
    # угадывать организацию по названию должности нельзя.
    lanes_by_pool: Dict[str, List[str]] = {}
    for lane in lanes:
        lanes_by_pool.setdefault(
            _norm_name(_raw_text(lane.get("participant"))), []).append(
                _norm_name(_raw_text(lane.get("name"))))
    for pool in pools:
        names = lanes_by_pool.get(_norm_name(pool), [])
        if names and all(n == _norm_name(pool) for n in names) \
                and not _receives_merged_steps(pool, pools, lanes):
            gaps.append(f"пул «{pool}» объявил дорожку с таким же именем: "
                        "дорожка ничего не добавляет. Если это роль или "
                        "подразделение организации из текста — запиши его объектом "
                        "с external=false и inside; если это самостоятельный "
                        "участник (контрагент, клиент, внешняя система) — оставь "
                        "пул и убери одноимённую дорожку. Удалять названного в "
                        "описании участника нельзя")

    # Объявление роли обязано быть завершённым: external=false без inside — это
    # всё тот же раздутый участник, а inside на несуществующий пул — опечатка,
    # из-за которой шаги некуда переносить.
    pool_norms = {_norm_name(p) for p in pools}
    for item in (raw.get("participants") or []):
        if not isinstance(item, dict) or item.get("external") is not False:
            continue
        name = _raw_text(item.get("name"))
        inside = _raw_text(item.get("inside"))
        if not inside:
            gaps.append(f"участник «{name}» помечен ролью (external=false), но "
                        "не сказал, чьей: укажи inside — название организации "
                        "из текста")
        elif _norm_name(inside) not in pool_norms:
            gaps.append(f"участник «{name}» указывает inside=«{inside}», а "
                        "такого пула в плане нет: назови организацию из её "
                        "шагов")

    out_count: Dict[str, int] = {}
    in_count: Dict[str, int] = {}
    for flow in flows:
        source, target = _raw_text(flow.get("source")), _raw_text(flow.get("target"))
        if _raw_text(flow.get("kind")).lower() == "message":
            continue
        out_count[source] = out_count.get(source, 0) + 1
        in_count[target] = in_count.get(target, 0) + 1

    # Незаконные концы потоков: repair такую дугу убирает, но куда модель на
    # самом деле вела маршрут — знает только она, поэтому нарушение уходит в
    # переспрос списком конкретных потоков.
    kind_by_id: Dict[str, str] = {}
    pool_by_id: Dict[str, str] = {}
    for elem in elements:
        elem_id = _raw_text(elem.get("id"))
        if elem_id:
            kind_by_id[elem_id] = _raw_text(elem.get("kind") or elem.get("type"))
            pool_by_id[elem_id] = _raw_text(elem.get("participant"))
    for flow in flows:
        if "message" in _raw_text(flow.get("kind") or flow.get("type")).lower():
            continue
        source, target = _raw_text(flow.get("source")), _raw_text(flow.get("target"))
        # Sequence между пулами repair превратит в сообщение, а сообщение в
        # чужой старт — это как раз способ запустить пул.
        if source in pool_by_id and target in pool_by_id \
                and pool_by_id[source] != pool_by_id[target]:
            continue
        reason = _illegal_flow_end(kind_by_id.get(source, ""),
                                   kind_by_id.get(target, ""))
        if reason:
            gaps.append(f"поток {_raw_text(flow.get('id')) or '?'} "
                        f"({source} → {target}) противоречит правилу: {reason}; "
                        "перестрой маршрут без этой дуги")

    defaults = {_raw_text(f.get("source")) for f in flows if f.get("default")}
    for elem in elements:
        kind = _raw_text(elem.get("kind") or elem.get("type"))
        elem_id = _raw_text(elem.get("id")) or "?"
        if kind in TYPED_EVENT_KINDS:
            definition = _raw_text(elem.get("event_definition")
                                   or elem.get("event_type")).lower()
            if definition not in EVENT_DEFINITIONS:
                gaps.append(f"у события {elem_id} ({_raw_text(elem.get('name'))}) "
                            f"нет определения: нужно одно из "
                            f"{', '.join(sorted(EVENT_DEFINITIONS))}")
            elif definition == "timer" and not _valid_timer(
                    _raw_text(elem.get("timer") or elem.get("duration"))):
                gaps.append(f"таймер события {elem_id} не в формате ISO-8601 "
                            "(PT2H, PT15M, R3/PT10M)")
            if kind == "boundaryEvent" and not _raw_text(elem.get("attached_to")):
                gaps.append(f"граничное событие {elem_id} не прикреплено к задаче "
                            "(нет attached_to)")
        if kind in GATEWAY_KINDS:
            incoming, outgoing = in_count.get(elem_id, 0), out_count.get(elem_id, 0)
            if incoming < 2 and outgoing < 2:
                gaps.append(f"шлюз {elem_id} ({_raw_text(elem.get('name'))}) "
                            "не раздваивает и не сливает маршруты — это обычный "
                            "шаг")
            if kind == "exclusiveGateway" and outgoing >= 2:
                unconditioned = [
                    _raw_text(f.get("id")) for f in flows
                    if _raw_text(f.get("source")) == elem_id
                    and _raw_text(f.get("kind")).lower() != "message"
                    and not _raw_text(f.get("condition"))
                    and not f.get("default")
                ]
                if len(unconditioned) > 1 and elem_id not in defaults:
                    gaps.append(f"у шлюза {elem_id} {len(unconditioned)} веток без "
                                "условия: заполни condition или пометь одну "
                                "default=true")

    # Ветвление, спрятанное в подписях потоков: узел раздваивает маршрут, а
    # шлюза в плане нет ни одного. Починка не вправе выбирать тип развилки —
    # исключающая она или параллельная, — это знает только модель.
    branches: Dict[str, Set[str]] = {}
    for flow in flows:
        if _raw_text(flow.get("kind")).lower() == "message":
            continue
        source, target = _raw_text(flow.get("source")), _raw_text(flow.get("target"))
        # Дуга, которую план теряет по правилу легальных концов (поток в старт,
        # в граничное событие, из финиша), ветвлением не считается: иначе одно
        # нарушение приходило бы списком из двух пунктов.
        if _illegal_flow_end(kind_by_id.get(source, ""), kind_by_id.get(target, "")):
            continue
        if source and target:
            branches.setdefault(source, set()).add(target)
    explicit_split = {
        _raw_text(elem.get("id")) for elem in elements
        if _raw_text(elem.get("kind") or elem.get("type")) in GATEWAY_KINDS
        and len(branches.get(_raw_text(elem.get("id")), set())) >= 2
    }
    if not explicit_split:
        for elem in elements:
            elem_id = _raw_text(elem.get("id"))
            if len(branches.get(elem_id, ())) < 2:
                continue
            if _raw_text(elem.get("kind") or elem.get("type")) in GATEWAY_KINDS:
                continue
            gaps.append(f"узел {elem_id} ({_raw_text(elem.get('name'))}) ведёт "
                        "сразу в несколько шагов без шлюза — развилка спрятана "
                        "в подписях потоков; вставь gateway (exclusive или "
                        "parallel) и веди ветки от него")
    # Порядок = важность: переспрос один, и на плане с десятком нарушений модель
    # доходила до подписей имён, оставляя на схеме меньше участников, чем в
    # описании. Потерянный участник — первое, что надо исправить.
    gaps.extend(_condition_gaps(raw, text))
    return sorted(gaps, key=_gap_priority)


CONDITION_CUES = ("если ", "при условии", "в случае ", "когда ", "иначе ")
CLAUSE_CLIP = 90


def _gateways_split_or_join(gateway_ids: Set[str],
                            flows: List[Dict[str, Any]]) -> bool:
    """Есть ли в плане шлюз, который правда раздваивает или сливает маршрут.

    Шлюз с одним входящим и одним исходящим — это step, которого модель
    постыдилась: им ветвление из описания не выражено, и считать такое планом
    развилки нельзя (скоринг за него снимает, а `repair` оставит как есть).
    """
    out: Dict[str, int] = {}
    inc: Dict[str, int] = {}
    for flow in flows:
        if _raw_text(flow.get("kind")).lower() == "message":
            continue
        source = _norm_name(_raw_text(flow.get("source")))
        target = _norm_name(_raw_text(flow.get("target")))
        if source in gateway_ids:
            out[source] = out.get(source, 0) + 1
        if target in gateway_ids:
            inc[target] = inc.get(target, 0) + 1
    return any(out.get(g, 0) >= 2 or inc.get(g, 0) >= 2 for g in gateway_ids)


def _condition_gaps(raw: Dict[str, Any], text: str) -> List[str]:
    """Описание ветвится, а план — нет: условие есть, шлюза в структуре нуль.

    Линейный маршрут там, где текст различает «прошла проверка» и «не прошла», —
    обещанного процесса на схеме нет. Тип развилки и её ветки знает только
    модель, поэтому это нарушение — вопрос переспроса, а не правка плана.
    """
    body = (text or "").strip()
    if not body:
        return []
    gateways = {_norm_name(_raw_text(e.get("id"))) for e in _raw_dicts(raw.get("elements"))
                if _raw_text(e.get("kind") or e.get("type")) in GATEWAY_KINDS}
    if _gateways_split_or_join(gateways, _raw_dicts(raw.get("flows"))):
        return []
    low = body.lower()
    at = min((low.find(cue) for cue in CONDITION_CUES if cue in low), default=-1)
    if at < 0:
        return []
    return [f"описание задаёт условие («{body[at:at + CLAUSE_CLIP].strip()}…»),"
            " а в плане ни одного шлюза, который раздваивает или сливает"
            " маршрут: развей маршрут gateway (exclusive — когда дальше идёт"
            " одна ветка, parallel — когда несколько одновременно). Шлюз с одним"
            " входящим и одним исходящим — не развилка, а обычный шаг. Если"
            " развилки в описании на самом деле нет, верни план без изменений"]


_GAP_PRIORITY = (("действующим лицом", 0),
                 # Названный в описании участник, которого в схеме нет, — тот же
                 # потерянный контрагент, что и лицо без пула: без него схема
                 # перестаёт описывать соглашение двух сторон. Рангом ниже он
                 # проигрывал косметике, и повтор с «WMS» в пуле отбрасывался.
                 ("а в схеме его нет", 0),
                 # Роль без организации — тот же потерянный участник: пул пустой,
                 # дорожкой он не станет, и починка снимет его со схемы. Живой
                 # прогон показал, что повтор приносил именно это нарушение,
                 # «починив» названную систему (`external: false` без `inside`).
                 ("помечен ролью", 0),
                 ("без единого шага", 1),
                 # Срок из описания — содержание, а не косметика: менять его на
                 # снятое предупреждение о потоке значит портить процесс.
                 ("задаёт ожидание", 1),
                 # Ветвление из описания — тоже содержание процесса, а не
                 # косметика маршрута.
                 ("ни одного шлюза", 1))
# Последний класс — «остальное»: он никогда не выбирается иглой, а служит
# приданым для сравнения профилей.
_GAP_RANK_DEFAULT = 2
_GAP_RANKS = _GAP_RANK_DEFAULT + 1


def _gap_priority(gap: str) -> int:
    for needle, rank in _GAP_PRIORITY:
        if needle in gap:
            return rank
    return _GAP_RANK_DEFAULT


def _gap_profile(gaps: List[str]) -> Tuple[int, ...]:
    """Число нарушений по классам важности (от главных к мелким)."""
    return tuple(sum(1 for g in gaps if _gap_priority(g) == rank)
                 for rank in range(_GAP_RANKS))


def _trace_gaps(gaps: List[str]) -> List[str]:
    """Нарушения для трейса — обрезанные: читать их будет человек, а полный
    текст каждого замечания удваивает размер отчёта харнесса."""
    return [g if len(g) <= 140 else g[:137] + "…" for g in gaps]


def _reask_improves(gaps: List[str], candidate: List[str]) -> bool:
    """Стоит ли принять второй план вместо первого.

    Сравнение «сколько всего нарушений» отбрасывало план, который вернул
    потерянного участника и заплатил за это одним потоком без условия: по сумме
    он не лучше, а по существу — да. Поэтому профиль сравнивается по важности
    классов. Общего запрета «нарушений стало больше» здесь нет: живой прогон
    показал, что он выбрасывает именно те повторы, которые закрыли всё содержание
    (пять потерянных действующих лиц) и добавили только косметики, которую
    починка снимает сама. Класс важности при этом не должен испортиться — за это
    отвечает лексикографическое сравнение.
    """
    return _gap_profile(candidate) < _gap_profile(gaps)


def _valid_timer(value: str) -> bool:
    """Хронометраж таймера: ISO-8601 с хотя бы одним числом («P» само по себе
    — не длительность)."""
    return bool(value) and bool(_TIMER_RE.match(value)) and any(c.isdigit()
                                                                for c in value)


# --- детерминированная починка структуры ---

_ID_CLEAN = re.compile(r"[^A-Za-z0-9_]")
_NOT_IN_NAME = re.compile(r"[^0-9a-zа-яё]+")


def _sanitize_id(raw: Any, fallback: str) -> str:
    text = str(raw or "").strip()
    cleaned = _ID_CLEAN.sub("_", text) or fallback
    if not cleaned:
        # Конца потока без имени не бывает: вызывающий узнаёт про пустоту по ""
        # и сам решает, выбросить дугу или поставить вопросительный знак.
        return ""
    if cleaned[0].isdigit():
        cleaned = f"e_{cleaned}"
    return cleaned


def _norm_name(value: Any) -> str:
    """Имя для сопоставления: без регистра, «_» и всего, что не буква/цифра."""
    return _NOT_IN_NAME.sub("", str(value or "").lower())


def _clip(text: str, limit: int, notes: List[str], subject: str) -> str:
    if len(text) <= limit:
        return text
    notes.append(f"{subject} укорочен до {limit} символов")
    return text[:limit]


def _pool_exact(name: str, participants: List[Dict[str, Any]]) -> Optional[str]:
    """Пул по точному или нормализованному имени."""
    norm = _norm_name(name)
    for p in participants:
        if p["name"] == name or _norm_name(p["name"]) == norm:
            return p["name"]
    return None


def _pool_close(name: str, participants: List[Dict[str, Any]]) -> Optional[str]:
    """Пул по схожести имени — спасает от падежей и опечаток модели."""
    norm = _norm_name(name)
    if not norm:
        return None
    by_norm = {_norm_name(p["name"]): p["name"] for p in participants}
    hit = difflib.get_close_matches(norm, list(by_norm), n=1,
                                    cutoff=POOL_MATCH_CUTOFF)
    return by_norm[hit[0]] if hit else None


def _lane_by_ref(ref: str, lanes: List[Dict[str, Any]],
                 participant: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Дорожка по id либо названию; participant сужает поиск до одного пула."""
    norm = _norm_name(ref)
    for lane in lanes:
        if participant is not None and lane["participant"] != participant:
            continue
        if lane["id"] == ref or _norm_name(lane["name"]) == norm:
            return lane
    return None


def _add_pool(name: str, participants: List[Dict[str, Any]], notes: List[str],
              subject: str) -> str:
    """Новый пул по названию из ответа модели.

    Молча свалить элемент в первый пул — значит собрать фальшивую схему:
    шаги разных участников окажутся одним процессом, а связи между ними
    превратятся в messageFlow. Отдельный пул честнее и виден пользователю.
    """
    name = name[:NAME_LIMIT]
    if len(participants) >= MAX_PARTICIPANTS:
        fallback = participants[0]["name"]
        notes.append(f"Пул «{name}» не создан ({subject}): достигнут максимум "
                     f"{MAX_PARTICIPANTS} — отнесён в «{fallback}»")
        return fallback
    participants.append({"name": name})
    notes.append(f"Создан пул «{name}» по имени из ответа модели ({subject})")
    return name


def _resolve_participant(declared: str, participants: List[Dict[str, Any]],
                         lanes: List[Dict[str, Any]], lane_ref: str,
                         subject: str, notes: List[str]) -> str:
    """Пул, к которому относится элемент или дорожка.

    Порядок: точное/нормализованное имя пула → пул названной дорожки (сильный
    признак: дорожка объявлена в конкретном пуле) → похожее имя → новый пул.
    """
    if declared:
        exact = _pool_exact(declared, participants)
        if exact is not None:
            if exact != declared:
                notes.append(f"{subject}: пул «{declared}» сопоставлен с «{exact}»")
            return exact
        lane = _lane_by_ref(lane_ref.strip(), lanes) if lane_ref.strip() else None
        if lane is not None:
            notes.append(f"{subject}: пул «{declared}» не найден, взят пул "
                         f"дорожки «{lane['name']}» — «{lane['participant']}»")
            return lane["participant"]
        close = _pool_close(declared, participants)
        if close is not None:
            notes.append(f"{subject}: пул «{declared}» сопоставлен с близким "
                         f"«{close}»")
            return close
        return _add_pool(declared, participants, notes, subject)

    if lane_ref.strip():
        lane = _lane_by_ref(lane_ref.strip(), lanes)
        if lane is not None:
            notes.append(f"{subject}: пул не указан, взят пул дорожки "
                         f"«{lane['name']}» — «{lane['participant']}»")
            return lane["participant"]

    pool = participants[0]["name"]
    notes.append(f"{subject}: пул не указан — отнесён к «{pool}»")
    return pool


def _resolve_lane(declared: str, participant: str, lanes: List[Dict[str, Any]],
                  used_ids: Set[str], elem_id: str,
                  notes: List[str]) -> str:
    """Дорожка элемента внутри его пула: объявленная, созданная по названию
    из элемента, а при упоре в лимит — первая дорожка пула."""
    in_pool = [lane for lane in lanes if lane["participant"] == participant]
    declared = declared.strip()
    if declared:
        lane = _lane_by_ref(declared, lanes, participant)
        if lane is not None:
            return lane["id"]
        if len(lanes) >= MAX_LANES:
            fallback = in_pool[0]["id"] if in_pool else ""
            notes.append(f"Дорожка «{declared}» для элемента {elem_id} не создана: "
                         f"максимум {MAX_LANES} дорожек — элемент "
                         + (f"помещён в «{in_pool[0]['name']}»" if in_pool
                            else "остался вне дорожки"))
            return fallback
        lane_id = _unique_id(used_ids, f"Lane_{len(lanes) + 1}")
        used_ids.add(lane_id)
        name = declared[:NAME_LIMIT]
        lanes.append({"id": lane_id, "name": name, "participant": participant})
        notes.append(f"Дорожка «{name}» не объявлена — создана в пуле "
                     f"«{participant}» по имени элемента {elem_id}")
        return lane_id

    if in_pool:
        notes.append(f"Элемент {elem_id}: дорожка не указана — помещён в "
                     f"«{in_pool[0]['name']}»")
        return in_pool[0]["id"]
    return ""


def _structure_ids(participants: List[Dict[str, Any]], lanes: List[Dict[str, Any]],
                   elements: List[Dict[str, Any]],
                   flows: List[Dict[str, Any]]) -> Set[str]:
    """Отпечаток плана для трейса починки.

    Шаг записан как `id@пул`, а поток — как `id:источник->цель`, поэтому
    перенос шага в другой пул и перешивка дуги видны как изменение. Иначе шаг
    починки, который только и делал, что переставлял элементы, выглядел бы
    бездействием.
    """
    out: Set[str] = set()
    for item in participants or []:
        out.add("pool:" + str(_raw_text(item.get("name"))))
    for item in lanes or []:
        out.add("lane:" + str(item.get("id") or _raw_text(item.get("name"))))
    for item in elements or []:
        out.add(f"{item.get('id')}@{item.get('participant')}")
    for item in flows or []:
        out.add(f"flow:{item.get('id')}:{item.get('source')}->{item.get('target')}")
    return out


def repair_structure(raw: Dict[str, Any],
                     trace: Optional[List[Dict[str, Any]]] = None,
                     ) -> Tuple[Dict[str, Any], List[str]]:
    """Приводит произвольный ответ модели к валидной структуре.

    Ничего не отбрасывает целиком: битые значения чинит, невозможные связи
    удаляет по одной, отсутствующие старт/финиш добавляет. Каждая правка
    попадает в notes — пользователь обязан видеть, что ИИ поправил за него.
    Возвращает (структура, список внесённых правок).

    `trace` — необязательный сборщик: харнесс `eval/` просит записать, какой
    шаг что добавил и что убрал, чтобы провал инварианта можно было атрибутировать
    по узлу, а не гадать по тексту пометок. Продуктовый путь список не передаёт
    и ничего не платит.
    """
    notes: List[str] = []
    used_ids: Set[str] = set()
    steps: List[Dict[str, Any]] = []
    mark: List[Any] = []

    def _mark(step: str, participants, lanes_, elements_, flows_) -> None:
        mark.clear()
        mark.append((step, _structure_ids(participants, lanes_, elements_, flows_),
                     len(notes)))

    def _close(participants, lanes_, elements_, flows_) -> None:
        step, before, n_before = mark.pop()
        after = _structure_ids(participants, lanes_, elements_, flows_)
        steps.append({"step": step, "added": sorted(after - before),
                      "removed": sorted(before - after),
                      "notes": notes[n_before:]})

    _mark("пулы", [], [], [], [])
    participants = _repair_participants(raw, notes)
    _close(participants, [], [], [])

    _mark("дорожки", participants, [], [], [])
    lanes = _repair_lanes(raw, participants, used_ids, notes)
    _close(participants, lanes, [], [])

    _mark("шаги", participants, lanes, [], [])
    elements = _repair_elements(raw, participants, lanes, used_ids, notes)
    _close(participants, lanes, elements, [])
    if not elements:
        raise GenerationError(
            "Не удалось выделить ни одного шага процесса из описания. "
            "Опишите процесс подробнее."
        )

    _mark("потоки", participants, lanes, elements, [])
    flows = _repair_flows(raw, elements, used_ids, notes)
    _close(participants, lanes, elements, flows)

    _mark("хозяева граничных событий", participants, lanes, elements, flows)
    _resolve_boundary_hosts(elements, notes)
    _close(participants, lanes, elements, flows)
    # Объявленные роли получают дорожки ДО слияния: сам признак «пул назван
    # дорожкой» модель могла не проставить, но её решение уже в плане.
    _mark("дорожки для объявленных ролей", participants, lanes, elements, flows)
    _declare_role_lanes(participants, lanes, used_ids, notes)
    _close(participants, lanes, elements, flows)
    # Слияние «ролей-пулов» — до отбрасывания пустых: у пула, который оказался
    # дорожкой, шаги никуда не деваются, и удалять его не за что.
    _mark("слияние ролей-пулов", participants, lanes, elements, flows)
    _merge_role_pools(elements, flows, participants, lanes, notes)
    _close(participants, lanes, elements, flows)
    _mark("удаление пустых пулов", participants, lanes, elements, flows)
    _drop_vacant_pools(elements, flows, participants, lanes, notes)
    _close(participants, lanes, elements, flows)
    _mark("события пула", participants, lanes, elements, flows)
    _ensure_pool_events(elements, flows, participants, used_ids, notes)
    _close(participants, lanes, elements, flows)
    _mark("связь стартов", participants, lanes, elements, flows)
    _link_dead_starts(elements, flows, participants, used_ids, notes)
    _close(participants, lanes, elements, flows)
    _mark("закрытие маршрутов", participants, lanes, elements, flows)
    _close_pool_paths(elements, flows, participants, used_ids, notes)
    _close(participants, lanes, elements, flows)
    _mark("достижимость", participants, lanes, elements, flows)
    _ensure_reachability(elements, flows, used_ids, notes)
    _close(participants, lanes, elements, flows)
    # Понижение шлюза — после связности и до вставки слияний: добавленные
    # достижности меняют число исходящих потоков, и «развилка» с одной веткой
    # могла получиться уже после основной починки.
    _mark("понижение одновыходных шлюзов", participants, lanes, elements, flows)
    _demote_single_branch_gateways(elements, flows, notes)
    _close(participants, lanes, elements, flows)
    # Развилку вставляют до default: у нового шлюза часть веток без условия, и
    # правило «единственная безусловная ветка — выход по умолчанию» должно её
    # увидеть. Схождения считаются после вставки — пара шлюзов берётся из
    # расщепителя.
    _mark("вставка развилок", participants, lanes, elements, flows)
    _explicit_split_gateways(elements, flows, used_ids, notes)
    _close(participants, lanes, elements, flows)
    _mark("выход по умолчанию", participants, lanes, elements, flows)
    _ensure_gateway_default(elements, flows, notes)
    _close(participants, lanes, elements, flows)
    _mark("вставка схождений", participants, lanes, elements, flows)
    _explicit_merge_gateways(elements, flows, used_ids, notes)
    _close(participants, lanes, elements, flows)

    if trace is not None:
        trace.extend(steps)

    repaired = {
        "participants": participants,
        "lanes": lanes,
        "elements": elements,
        "flows": flows,
    }
    return repaired, notes


def _repair_participants(raw: Dict[str, Any],
                         notes: List[str]) -> List[Dict[str, Any]]:
    participants: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in raw.get("participants") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if not name:
            continue
        name = _clip(name, NAME_LIMIT, notes, "Название пула")
        norm = _norm_name(name)
        if norm in seen:
            notes.append(f"Дубликат пула «{name}» пропущен")
            continue
        seen.add(norm)
        # `external`/`inside` — объявление модели: кто самостоятельный участник,
        # а кто роль другой организации (см. `_declare_role_lanes`).
        entry = {"name": name}
        if isinstance(item, dict):
            if "external" in item:
                entry["external"] = bool(item.get("external"))
            inside = str(item.get("inside") or "").strip()
            if inside:
                entry["inside"] = inside
        participants.append(entry)
        if len(participants) >= MAX_PARTICIPANTS:
            notes.append("Лишние пулы отброшены (максимум %d)" % MAX_PARTICIPANTS)
            break
    if not participants:
        participants.append({"name": "Процесс"})
        notes.append("Пул не указан — создан пул «Процесс»")
    return participants


def _repair_lanes(raw: Dict[str, Any], participants: List[Dict[str, Any]],
                  used_ids: Set[str],
                  notes: List[str]) -> List[Dict[str, Any]]:
    lanes: List[Dict[str, Any]] = []
    raw_lanes = raw.get("lanes") or []
    if not isinstance(raw_lanes, list):
        raw_lanes = []
    for item in raw_lanes:
        if not isinstance(item, dict):
            continue
        declared_id = str(item.get("id") or "").strip()
        declared_name = str(item.get("name") or "").strip()
        if not declared_id and not declared_name:
            notes.append("Дорожка без имени и идентификатора пропущена")
            continue
        if len(lanes) >= MAX_LANES:
            notes.append(f"Лишние дорожки отброшены (максимум {MAX_LANES})")
            break

        subject = f"Дорожка {declared_id or declared_name}"
        participant = _resolve_participant(
            str(item.get("participant") or "").strip(), participants, lanes, "",
            subject, notes)
        name = _clip(declared_name or declared_id, NAME_LIMIT, notes,
                     f"Название дорожки {declared_id or declared_name}")

        if _lane_by_ref(name, lanes, participant) is not None:
            notes.append(f"Дорожка «{name}» в пуле «{participant}» уже есть — "
                         "дубликат пропущен")
            continue

        if declared_id:
            lane_id = _sanitize_id(declared_id, f"Lane_{len(lanes) + 1}")
            while lane_id in used_ids:
                lane_id = f"{lane_id}_x"
            if declared_id != lane_id:
                notes.append(f"Идентификатор дорожки {declared_id} приведён "
                             f"к «{lane_id}»")
        else:
            # Кирриллица в _sanitize_id превращается в подчёркивания: лучше
            # сразу дать читаемый id, а имя оставить как есть.
            lane_id = _unique_id(used_ids, f"Lane_{len(lanes) + 1}")
        used_ids.add(lane_id)
        lanes.append({"id": lane_id, "name": name, "participant": participant})
    return lanes


def _repair_elements(raw: Dict[str, Any], participants: List[Dict[str, Any]],
                     lanes: List[Dict[str, Any]], used_ids: Set[str],
                     notes: List[str]) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    kind_aliases = {k.lower(): k for k in ALL_KINDS}
    raw_elements = raw.get("elements") or []
    if not isinstance(raw_elements, list):
        raw_elements = []
    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        if len(elements) >= MAX_ELEMENTS:
            notes.append("Лишние элементы отброшены (максимум %d)" % MAX_ELEMENTS)
            break

        elem_id = _sanitize_id(item.get("id"), f"Elem_{len(elements) + 1}")
        while elem_id in used_ids:
            elem_id = f"{elem_id}_x"
        declared_id = str(item.get("id") or "").strip()
        if declared_id and declared_id != elem_id:
            notes.append(f"Идентификатор элемента {declared_id} приведён к «{elem_id}»")
        used_ids.add(elem_id)

        kind_raw = str(item.get("kind") or item.get("type") or "").strip()
        kind = kind_aliases.get(kind_raw.lower())
        if kind is None:
            notes.append(f"Неизвестный тип «{kind_raw}» элемента {elem_id} → task")
            kind = "task"

        declared_lane = str(item.get("lane") or "").strip()
        participant = _resolve_participant(
            str(item.get("participant") or "").strip(), participants, lanes,
            declared_lane, f"Элемент {elem_id}", notes)

        name = str(item.get("name") or "").strip()
        if name:
            name = _clip(name, NAME_LIMIT, notes, f"Название элемента {elem_id}")
        else:
            name = kind
            notes.append(f"Элемент {elem_id} без названия — подписан типом «{kind}»")

        element: Dict[str, Any] = {
            "id": elem_id,
            "kind": kind,
            "name": name,
            "participant": participant,
            "lane": _resolve_lane(declared_lane, participant, lanes, used_ids,
                                  elem_id, notes),
        }
        description = str(item.get("documentation") or "").strip()
        if description:
            element["documentation"] = _clip(description, DOCUMENTATION_LIMIT,
                                             notes, f"Описание элемента {elem_id}")
        if kind in TYPED_EVENT_KINDS:
            _repair_event_type(item, kind, elem_id, element, notes)
        if kind == "boundaryEvent":
            declared_host = _raw_text(item.get("attached_to"))
            if declared_host:
                element["attached_to"] = _sanitize_id(declared_host, declared_host)
        elements.append(element)
    return elements


# Тип события по его собственной подписи. Границы групп держат только те
# слова, где прочтение однозначное: «получателя» (адресат шага) в группу
# сообщений не попадает намеренно, спорные названия остаются на решение модели.
_EVENT_TIMER_NAME_RE = re.compile(
    r"просрочк|таймаут|таймер|sla|истечени|задержк|опозд|дедлайн", re.I)
_EVENT_MESSAGE_NAME_RE = re.compile(
    r"ожидани|ожидает|ждет|ответ|подтверждени|сообщени|уведомлени|приход", re.I)


def _infer_event_definition(name: str) -> str:
    """Определение события по его названию ('' — название молчит)."""
    raw = _raw_text(name)
    if not raw:
        return ""
    if _EVENT_TIMER_NAME_RE.search(raw):
        return "timer"
    if _EVENT_MESSAGE_NAME_RE.search(raw):
        return "message"
    return ""


def _repair_event_type(item: Dict[str, Any], kind: str, elem_id: str,
                       element: Dict[str, Any], notes: List[str]) -> None:
    """Определение события и хронометраж таймера.

    Событие без определения — это пустой кружок на схеме: пользователь видит
    «промежуточное событие» и не понимает, чего процесс ждёт. Выдумывать шаг
    генератор не вправе, но тип берёт там, где его назвало само событие
    («Просрочка SLA» — таймер, «Ожидание подтверждения» — сообщение): это
    прочтение подписи, а не новое содержание. Название, не относящееся ни к одной
    группе, остаётся заметкой и нарушением в плане — его решает модель.
    """
    declared = _raw_text(item.get("event_definition")
                         or item.get("event_type")).lower()
    if declared not in EVENT_DEFINITIONS:
        inferred = _infer_event_definition(element["name"])
        if not inferred:
            if declared:
                notes.append(f"Неизвестное определение «{declared}» события {elem_id} "
                             "снято")
            else:
                notes.append(f"У события {elem_id} («{element['name']}») нет "
                             f"определения: нужно одно из "
                             f"{', '.join(sorted(EVENT_DEFINITIONS))}")
            return
        notes.append(f"Определение события {elem_id} «{element['name']}» взято "
                     f"«{inferred}» по его названию: без типа событие — пустой "
                     "кружок на схеме")
        declared = inferred
    element["event_definition"] = declared
    if declared != "timer":
        return
    declared_timer = _raw_text(item.get("timer") or item.get("duration"))
    if _valid_timer(declared_timer):
        element["timer"] = declared_timer
        return
    # Для таймера отсутствие хронометража хуже, чем типовой SLA: событие без
    # duration не сработает никогда. Значение видно в заметке.
    element["timer"] = DEFAULT_TIMER_DURATION
    notes.append(f"Хронометраж таймера {elem_id} «{declared_timer or 'не указан'}»"
                 f" не ISO-8601 — взят {DEFAULT_TIMER_DURATION}")


_FLOW_END_REASONS = {
    "startEvent": ("у стартового события входящих потоков не бывает — это "
                   "триггер пула, а не шаг маршрута"),
    "boundaryEvent": ("граничное событие запускает его хозяин через "
                      "attachedToRef — исходящий поток от граничного события "
                      "это ветка обработки, а входящего у него не бывает"),
    "endEvent": "конечное событие завершает маршрут, продолжения у него нет",
}


def _illegal_flow_end(source_kind: str, target_kind: str) -> str:
    """Причина, по которой sequence-поток между узлами недопустим; пусто, когда
    дуга легальна.

    Только концы: множества запрещённых живёт в `bpmn_edits` — там же, откуда
    ими пользуется планировщик улучшения и `validate_and_repair`. Два контура с
    одним правилом в двух формулировках разъезжаются на первом же новом типе
    узла, поэтому список запрещённого здесь только переводится в текст.
    """
    if target_kind in SEQUENCE_FORBIDDEN_TARGETS:
        return _FLOW_END_REASONS[target_kind]
    if source_kind in SEQUENCE_FORBIDDEN_SOURCES:
        return _FLOW_END_REASONS[source_kind]
    return ""


def _repair_flows(raw: Dict[str, Any], elements: List[Dict[str, Any]],
                  used_ids: Set[str], notes: List[str]) -> List[Dict[str, Any]]:
    element_ids = {e["id"] for e in elements}
    element_by_id = {e["id"]: e for e in elements}

    flows: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[str, str, str]] = set()
    raw_flows = raw.get("flows") or raw.get("sequence_flows") or []
    if not isinstance(raw_flows, list):
        raw_flows = []
    for item in raw_flows:
        if not isinstance(item, dict):
            continue
        if len(flows) >= MAX_FLOWS:
            notes.append("Лишние потоки отброшены (максимум %d)" % MAX_FLOWS)
            break
        source = _sanitize_id(item.get("source") or item.get("sourceRef"), "")
        target = _sanitize_id(item.get("target") or item.get("targetRef"), "")
        if source == target:
            notes.append(f"Поток {source or '?'} → {target or '?'} удалён: шаг не "
                         "может вести сам в себя — повтор моделируется шлюзом "
                         "с веткой назад")
            continue
        if source not in element_ids or target not in element_ids:
            notes.append(f"Поток {source or '?'} → {target or '?'} удалён: "
                         "одного из элементов нет в плане")
            continue

        kind_raw = str(item.get("kind") or item.get("type") or "sequence").lower()
        kind = "message" if "message" in kind_raw else "sequence"

        cross_pool = (element_by_id[source]["participant"]
                      != element_by_id[target]["participant"])
        if kind == "sequence" and cross_pool:
            kind = "message"
            notes.append(f"Поток {source} → {target} между пулами преобразован "
                         "в потоковое сообщение: если это роли одной "
                         "организации, они должны быть дорожками одного пула")
        elif kind == "message" and not cross_pool:
            notes.append(f"Поток-сообщение {source} → {target} внутри пула удалён")
            continue

        # Концы sequence-потока: дугу в обход правила модель рисует, когда
        # путает события с шагами маршрута. Убираем её, а не переворачиваем —
        # переворот выдумал бы содержание. Сообщение в чужой старт — наоборот,
        # норма, и проверка идёт после межпуловости.
        if kind == "sequence":
            reason = _illegal_flow_end(element_by_id[source]["kind"],
                                       element_by_id[target]["kind"])
            if reason:
                notes.append(f"Поток {source} → {target} удалён: {reason}")
                continue

        # Для потока-сообщения текст условия становится именем сообщения.
        condition = str(item.get("condition") or item.get("name") or "").strip()
        condition = _clip(condition, 200, notes, f"Условие потока {source} → {target}")
        if condition and kind == "sequence" \
                and element_by_id[source]["kind"] not in GATEWAY_KINDS:
            notes.append(f"Условие на потоке {source} → {target} сохранено, "
                         "но источник не шлюз")
        if condition and kind == "message":
            notes.append(f"Условие потока {source} → {target} стало именем "
                         "потокового сообщения")

        pair = (kind, source, target)
        if pair in seen_pairs:
            notes.append(f"Дубликат потока {source} → {target} пропущен")
            continue
        seen_pairs.add(pair)

        declared_id = str(item.get("id") or "").strip()
        flow_id = _sanitize_id(declared_id, f"Flow_{len(flows) + 1}")
        while flow_id in used_ids:
            flow_id = f"{flow_id}_x"
        if declared_id and declared_id != flow_id:
            notes.append(f"Идентификатор потока {declared_id} приведён к «{flow_id}»")
        used_ids.add(flow_id)

        flow: Dict[str, Any] = {"id": flow_id, "kind": kind, "source": source,
                                "target": target, "condition": condition}
        if item.get("default"):
            # Выход по умолчанию бывает только у шлюза: иначе атрибут уедет в
            # XML, и bpmn-js нарисует default там, где его нет.
            if kind == "sequence" \
                    and element_by_id[source]["kind"] in GATEWAY_KINDS:
                flow["default"] = True
            else:
                notes.append(f"Флаг default у потока {flow_id} снят: источник "
                             f"{source} — не шлюз")
        flows.append(flow)
    return flows


def _resolve_boundary_hosts(elements: List[Dict[str, Any]],
                            notes: List[str]) -> None:
    """Хозяин граничного события — существующая задача того же пула.

    `attachedToRef` на несуществующий элемент или на шлюз bpmn-js не рисует
    никак, и событие повисает отдельным кружком. Определять тип события
    генератор не вправе, поэтому без хозяина событие либо становится
    промежуточным в потоке (определение есть — смысл сохранён), либо задачей.
    """
    by_id = {e["id"]: e for e in elements}
    for element in elements:
        if element["kind"] != "boundaryEvent":
            continue
        host = by_id.get(element.get("attached_to") or "")
        if (host is not None and host["kind"] in TASK_KINDS
                and host["participant"] == element["participant"]):
            continue
        reason = ("не указано, к какой задаче оно прицеплено"
                  if not element.get("attached_to")
                  else f"прицеплено к «{element.get('attached_to')}», а это не "
                       "задача своего пула")
        element.pop("attached_to", None)
        if element.get("event_definition"):
            element["kind"] = "intermediateCatchEvent"
            notes.append(f"Граничное событие {element['id']}: {reason} — "
                         "переведено в промежуточное событие маршрута")
        else:
            element["kind"] = "task"
            notes.append(f"Граничное событие {element['id']}: {reason} и "
                         "определения нет — понижено до задачи")


# Название дорожки в виде идентификатора («L_hr», «Lane_1»): модель слила туда
# id вместо имени. Для близкого совпадения при слиянии ролей-пулов такая
# «дорожка» признаком роли быть не может — по живому прогону роль «HR» уехала
# в «Руководство подразделения» именно из-за сходства с «L_hr».
_LANE_ID_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,29}$")


def _lane_named_like_pool(pool_name: str,
                          lanes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Дорожка в другом пуле с тем же названием, что и пул.

    Точное совпадение ищется отдельно: близкое имя — эвристика, и она не должна
    перетянуть пул в дорожку «похожую на него» там, рядом есть та же самая.
    """
    norm = _norm_name(pool_name)
    if not norm:
        return None
    close = None
    for lane in lanes:
        # План читается до починки: ключей у дорожки может и не быть.
        if _raw_text(lane.get("participant")) == pool_name:
            continue
        lane_norm = _norm_name(lane.get("name"))
        if lane_norm == norm:
            return lane
        # Идентификатор вместо имени на «похожесть» не тянется: «L_hr» ближе
        # всех к «HR» и уводила роль в чужую дорожку. Точное имя при этом
        # остаётся точным — «HR» бывает настоящей дорожкой.
        if close is None and not _LANE_ID_NAME_RE.match(_raw_text(lane.get("name"))) \
                and difflib.SequenceMatcher(
                    None, lane_norm, norm).ratio() >= POOL_MATCH_CUTOFF:
            close = lane
    return close


def _declare_role_lanes(participants: List[Dict[str, Any]],
                        lanes: List[Dict[str, Any]],
                        used_ids: Set[str],
                        notes: List[str]) -> None:
    """Материализует объявленную роль: у пула-роли появляется своя дорожка.

    Отличить «ИТ-отдел» (подразделение) от «Перевозчика» (самостоятельный
    участник) без описания нельзя — одношаговые пулы бывают и теми и другими.
    Поэтому решение приносит модель (`external: false` + `inside`), а код лишь
    создаёт дорожку: по ней `_merge_role_pools` перенесёт шаги уже проверенным
    признаком «пул назван дорожкой», и коллаборация не раздувается участниками,
    которых в тексте нет.

    Хозяина из плана код не подставляет: промер живого прогона (2026-09-23)
    показал, что «единственная действующая организация» оказывается и пулом
    «Руководитель службы поддержки», то есть ролью человека, — и организация
    уезжала дорожкой к своему же сотруднику.
    """
    names = {_norm_name(p["name"]): p["name"] for p in participants}
    role_inside = {_norm_name(p["name"]): _norm_name(p.get("inside") or "")
                   for p in participants if p.get("external") is False}

    def organization(name: str) -> Optional[str]:
        """Организация для объявленной роли: цепочка `inside` сворачивается до
        корня. Дорожка внутри пула, который сам уедет в чужой, повисла бы на
        несуществующем процессе, а кольцо («Оператор» внутри «Смены», «Смена»
        внутри «Оператора») ролей не создаёт вовсе."""
        current, seen = name, {name}
        while role_inside.get(current):
            current = role_inside[current]
            if current in seen:
                return None
            seen.add(current)
        return names.get(current)

    for pool in participants:
        if pool.get("external") is not False:
            continue
        inside = organization(_norm_name(pool["name"]))
        if not inside or inside == pool["name"]:
            continue
        if _lane_named_like_pool(pool["name"], lanes) is not None:
            continue
        lane_id = _unique_id(used_ids, "Lane_role")
        used_ids.add(lane_id)
        lanes.append({"id": lane_id, "name": pool["name"],
                      "participant": inside})
        notes.append(f"«{pool['name']}» объявлен ролью пула «{inside}» — "
                     f"создана дорожка «{pool['name']}» ({lane_id})")


def _merge_role_pools(elements: List[Dict[str, Any]],
                      flows: List[Dict[str, Any]],
                      participants: List[Dict[str, Any]],
                      lanes: List[Dict[str, Any]],
                      notes: List[str]) -> None:
    """Пул, который модель сама объявила дорожкой, — дорожка и есть.

    Признак без догадок: имя участника совпадает с именем дорожки в другом
    пуле. После слияния линейный маршрут перестаёт быть цепочкой messageFlow:
    потоки между дорожками одного пула — обычные sequence.
    """
    for pool in list(participants):
        lane = _lane_named_like_pool(pool["name"], lanes)
        if lane is None:
            continue
        target = lane["participant"]
        moved = [e for e in elements if e["participant"] == pool["name"]]
        for element in moved:
            element["participant"] = target
            element["lane"] = lane["id"]
        by_id = {e["id"]: e for e in elements}
        pairs = {(f["source"], f["target"]) for f in flows
                 if f["kind"] == "sequence"}
        for flow in list(flows):
            if flow["kind"] != "message":
                continue
            src, dst = by_id.get(flow["source"]), by_id.get(flow["target"])
            if src is None or dst is None or src["participant"] != dst["participant"]:
                continue
            # Слияние пулов превращает сообщение в поток внутри одного процесса,
            # и к нему применяется правило концов: поток в чужой старт был
            # способом запустить пул, а внутри пула он становится недопустимым.
            reason = _illegal_flow_end(src["kind"], dst["kind"])
            if reason:
                flows.remove(flow)
                notes.append(f"Поток-сообщение {flow['source']} → "
                             f"{flow['target']} удалён после слияния пулов: "
                             f"{reason}")
                continue
            if (flow["source"], flow["target"]) in pairs:
                flows.remove(flow)
                notes.append(f"Поток-сообщение {flow['source']} → "
                             f"{flow['target']} удалён: такой поток уже есть")
            else:
                flow["kind"] = "sequence"
                notes.append(f"Поток-сообщение {flow['source']} → "
                             f"{flow['target']} стал sequence-потоком: пулы слиты")
        for lane_own in [own for own in lanes
                         if own["participant"] == pool["name"]]:
            lanes.remove(lane_own)
        participants.remove(pool)
        notes.append(f"Пул «{pool['name']}» слит в «{target}» дорожкой "
                     f"«{lane['name']}» — перенесено {len(moved)} шаг(ов): "
                     "роли одной организации не бывают отдельными пулами")


def _drop_vacant_pools(elements: List[Dict[str, Any]],
                       flows: List[Dict[str, Any]],
                       participants: List[Dict[str, Any]],
                       lanes: List[Dict[str, Any]],
                       notes: List[str]) -> None:
    """Пул без единого шага — артефакт, а не участник.

    Модель заводит «Кладовщика» пулом, но все его шаги оставляет в другом
    процессе: внутри пула остаются «Старт» и «Завершение». Дорисовать туда
    шаги нельзя — это выдуманное содержание, а оставить — раздуть коллаборацию
    пустыми прямоугольниками, за которые скоринг правомерно снимает балл.
    """
    live = [pool for pool in participants
            if any(e["kind"] in STEP_KINDS for e in elements
                   if e["participant"] == pool["name"])]
    if not live or len(live) == len(participants):
        return
    for pool in list(participants):
        if pool in live:
            continue
        own = [e for e in elements if e["participant"] == pool["name"]]
        ids = {e["id"] for e in own}
        for element in own:
            elements.remove(element)
        for flow in list(flows):
            if flow["source"] in ids or flow["target"] in ids:
                flows.remove(flow)
        for lane in [own for own in lanes if own["participant"] == pool["name"]]:
            lanes.remove(lane)
        participants.remove(pool)
        notes.append(f"Пул «{pool['name']}» удалён: в нём не было ни одного "
                     "шага, только события «Старт» и «Завершение»")


def _ensure_gateway_default(elements: List[Dict[str, Any]],
                            flows: List[Dict[str, Any]],
                            notes: List[str]) -> None:
    """Ветки исключающего шлюза обязаны различаться однозначно.

    Если без условия осталась ровно одна ветка — она и есть «во всех остальных
    случаях», это единственное возможное прочтение, а не выдумка. Когда
    безусловных несколько, выбирать нельзя: нарушение остаётся видимым.
    """
    by_id = {e["id"]: e for e in elements}
    for flow in flows:
        if not flow.get("default"):
            continue
        source = by_id.get(flow["source"])
        if source is None or source["kind"] != "exclusiveGateway":
            flow.pop("default", None)
            notes.append(f"Выход по умолчанию снят с потока {flow['id']}: "
                         "у неэксклюзивного шлюза его не бывает")

    for gateway in elements:
        if gateway["kind"] != "exclusiveGateway":
            continue
        branches = [f for f in flows if f["kind"] == "sequence"
                    and f["source"] == gateway["id"]]
        if len(branches) < 2:
            continue
        flagged = [f for f in branches if f.get("default")]
        if len(flagged) > 1:
            for extra in flagged[1:]:
                extra.pop("default", None)
            notes.append(f"У шлюза {gateway['id']} несколько выходов по "
                         f"умолчанию — оставлен {flagged[0]['id']}")
        if flagged:
            gateway["default"] = flagged[0]["id"]
            continue
        unconditioned = [f for f in branches if not f.get("condition")]
        if len(unconditioned) == 1:
            unconditioned[0]["default"] = True
            gateway["default"] = unconditioned[0]["id"]
            notes.append(f"Ветка {unconditioned[0]['id']} шлюза {gateway['id']} "
                         "объявлена выходом по умолчанию: она единственная без "
                         "условия")
        elif len(unconditioned) > 1:
            notes.append(f"У шлюза {gateway['id']} {len(unconditioned)} веток без "
                         "условия — выход по умолчанию не выбран: выбрать одну "
                         "было бы выдумкой")


def _splitting_gateway(before: Dict[str, List[str]],
                       by_id: Dict[str, Dict[str, Any]],
                       start: str) -> Optional[Dict[str, Any]]:
    """Ближайший шлюз-предок узла (None — шлюза выше по маршруту нет)."""
    seen: Set[str] = set()
    queue = [start]
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        element = by_id.get(node_id)
        if element is None:
            continue
        if element["kind"] in GATEWAY_KINDS:
            return element
        queue.extend(before.get(node_id, []))
    return None


def _explicit_split_gateways(elements: List[Dict[str, Any]],
                             flows: List[Dict[str, Any]],
                             used_ids: Set[str],
                             notes: List[str]) -> None:
    """Развилка, которую модель расставила по подписям потоков, получает шлюз.

    Условие бывает только у потока от шлюза: если из шага ведут две ветки и
    автор различил их условиями («прошла проверка» / «не прошла»), развилка в
    плане уже есть — не хватает узла. Вставка держит топологию, подписи и
    порядок веток без изменений, добавляется только то, что подразумевалось.
    Без условий между ветками выбирать нечего: прочтение остаётся за моделью, и
    вставка останавливается.
    """
    by_id = {e["id"]: e for e in elements}
    outgoing: Dict[str, List[Dict[str, Any]]] = {}
    for flow in flows:
        if flow["kind"] == "sequence":
            outgoing.setdefault(flow["source"], []).append(flow)

    budget = MAX_SPLIT_GATEWAYS
    for elem_id, branches in list(outgoing.items()):
        node = by_id.get(elem_id)
        if node is None or len(branches) < 2:
            continue
        if node["kind"] in GATEWAY_KINDS or node["kind"] in ("startEvent",
                                                            "endEvent",
                                                            "boundaryEvent"):
            # Стартовое событие с двумя ветками — легальный неявный параллельный
            # расход, и вставлять туда исключительный шлюз значит менять смысл.
            continue
        if not [f for f in branches if f.get("condition") or f.get("default")]:
            continue
        if budget <= 0:
            notes.append(f"Узел {elem_id} ведёт в {len(branches)} веток без "
                         f"шлюза: исчерпан лимит вставок ({MAX_SPLIT_GATEWAYS})")
            continue
        budget -= 1
        gateway_id = _unique_id(used_ids, f"Gateway_split_{elem_id}")
        used_ids.add(gateway_id)
        elements.append({
            "id": gateway_id,
            "kind": "exclusiveGateway",
            "name": "Выбор ветки",
            "participant": node["participant"],
            "lane": node.get("lane", ""),
        })
        for branch in branches:
            branch["source"] = gateway_id
        flows.append(_new_flow(used_ids, elem_id, gateway_id))
        notes.append(f"После «{node['name']}» ({elem_id}) вставлен шлюз "
                     f"развилки {gateway_id}: {len(branches)} ветки различаются "
                     "условиями, а шлюза в плане не было")


def _explicit_merge_gateways(elements: List[Dict[str, Any]],
                             flows: List[Dict[str, Any]],
                             used_ids: Set[str],
                             notes: List[str]) -> None:
    """Узел, в который сходится две и более ветки, получает явный шлюз схождения.

    BPMN допускает неявное слияние, но на схеме оно читается как перепутанные
    ветки, а пользователь просил пару к каждой развилке. Тип шлюза берётся из
    того, что ветки расщепляет: параллельный ждёт все ветки, исключительный —
    любую. Если предки расходятся в выводах, вставка остановлена — угаданный
    параллельный шлюз меняет семантику процесса.
    """
    by_id = {e["id"]: e for e in elements}
    incoming: Dict[str, List[Dict[str, Any]]] = {}
    before: Dict[str, List[str]] = {}
    for flow in flows:
        if flow["kind"] != "sequence":
            continue
        incoming.setdefault(flow["target"], []).append(flow)
        before.setdefault(flow["target"], []).append(flow["source"])

    budget = MAX_MERGE_GATEWAYS
    for elem_id, branches in list(incoming.items()):
        node = by_id.get(elem_id)
        if node is None or len(branches) < 2:
            continue
        if node["kind"] in GATEWAY_KINDS or node["kind"] in ("startEvent",
                                                             "endEvent",
                                                             "boundaryEvent"):
            # Конечное событие с двумя входящими — норма BPMN, а не слитая
            # развилка: вставлять шлюз перед «Завершением» значит добавлять
            # элемент, который ничего не значит.
            continue
        if budget <= 0:
            notes.append(f"Узел {elem_id} принимает {len(branches)} веток без "
                         f"шлюза схождения: исчерпан лимит вставок "
                         f"({MAX_MERGE_GATEWAYS})")
            continue
        splits = [_splitting_gateway(before, by_id, b["source"])
                  for b in branches]
        kinds = {(s["kind"] if s else None) for s in splits}
        if len(kinds) != 1 or None in kinds:
            notes.append(f"Узел {elem_id} принимает ветки, у которых нет общего "
                         "шлюза-расщепителя — шлюз схождения не вставлен")
            continue
        # Имя берётся у самой развилки: «Итог: Проверка пройдена?» читателю
        # говорит, что здесь закрывается тот вопрос, а «Схождение веток» —
        # filling, который остаётся только безымянной развилке.
        question = next((s.get("name") or "").strip() for s in splits if s)
        gateway_id = _unique_id(used_ids, f"Gateway_merge_{elem_id}")
        used_ids.add(gateway_id)
        elements.append({
            "id": gateway_id,
            "kind": kinds.pop(),
            "name": f"Итог: {question}" if question else "Схождение веток",
            "participant": node["participant"],
            "lane": node.get("lane", ""),
        })
        for branch in branches:
            branch["target"] = gateway_id
        flows.append(_new_flow(used_ids, gateway_id, elem_id))
        budget -= 1
        notes.append(f"Перед «{node['name']}» ({elem_id}) вставлен шлюз "
                     f"схождения {gateway_id}: в узел ведёт {len(branches)} веток")


def _demote_single_branch_gateways(elements: List[Dict[str, Any]],
                                   flows: List[Dict[str, Any]],
                                   notes: List[str]) -> None:
    """Шлюз, который ни расщепляет, ни сливает, — не шлюз, а обычный шаг.

    Вторую ветку дорисовывать нельзя: выдуманное условие хуже, чем честная
    задача. Шлюз с одним исходящим и двумя входящими остаётся валидным
    сходящимся шлюзом — его понижать нельзя, иначе из схемы исчезнут все
    слияния маршрутов. Условие с исходящего потока снимается: на потоке от
    задачи оно перестаёт быть решением ветвления.
    """
    out_count: Dict[str, int] = {}
    in_count: Dict[str, int] = {}
    for f in flows:
        if f["kind"] == "sequence":
            out_count[f["source"]] = out_count.get(f["source"], 0) + 1
            in_count[f["target"]] = in_count.get(f["target"], 0) + 1

    for gateway in elements:
        if gateway["kind"] not in GATEWAY_KINDS:
            continue
        if out_count.get(gateway["id"], 0) >= 2 or in_count.get(gateway["id"], 0) >= 2:
            continue
        kind = gateway["kind"]
        gateway["kind"] = "task"
        name = gateway["name"]
        if name == kind:
            gateway["name"] = "task"
        notes.append(f"Шлюз {gateway['id']} («{name}») с одним потоком с каждой "
                     "стороны понижен до задачи")
        for f in flows:
            if f["kind"] == "sequence" and f["source"] == gateway["id"]:
                f.pop("default", None)
                if f["condition"]:
                    notes.append(f"Условие «{f['condition']}» снято с потока "
                                 f"{f['id']}: источник больше не шлюз")
                    f["condition"] = ""


def _ensure_pool_events(elements: List[Dict[str, Any]],
                        flows: List[Dict[str, Any]],
                        participants: List[Dict[str, Any]],
                        used_ids: Set[str], notes: List[str]) -> None:
    for idx, p in enumerate(participants, 1):
        pool = [e for e in elements if e["participant"] == p["name"]]
        seq = [f for f in flows if f["kind"] == "sequence"]
        has_start = any(e["kind"] == "startEvent" for e in pool)
        has_end = any(e["kind"] == "endEvent" for e in pool)

        if not has_start:
            entry = _pick_entry_node(pool, {f["target"] for f in seq})
            start_id = _unique_id(used_ids, f"StartEvent_{idx}")
            used_ids.add(start_id)
            elements.insert(0, {
                "id": start_id,
                "kind": "startEvent",
                "name": "Старт",
                "participant": p["name"],
                "lane": entry["lane"] if entry else "",
            })
            if entry:
                flows.append(_new_flow(used_ids, start_id, entry["id"]))
            notes.append(f"В пул «{p['name']}» добавлено стартовое событие")

        if not has_end:
            exit_node = _pick_exit_node(pool, seq)
            end_id = _unique_id(used_ids, f"EndEvent_{idx}")
            used_ids.add(end_id)
            elements.append({
                "id": end_id,
                "kind": "endEvent",
                "name": "Завершение",
                "participant": p["name"],
                "lane": exit_node["lane"] if exit_node else "",
            })
            if exit_node:
                flows.append(_new_flow(used_ids, exit_node["id"], end_id))
            notes.append(f"В пул «{p['name']}» добавлено завершающее событие")


def _link_dead_starts(elements: List[Dict[str, Any]],
                      flows: List[Dict[str, Any]],
                      participants: List[Dict[str, Any]],
                      used_ids: Set[str], notes: List[str]) -> None:
    """Старт, из которого некуда идти, процесс не запускает.

    Когда модель ведёт линию между «ролями-пулами» messageFlow-ами, первый шаг
    пула принимает только сообщение, а собственное стартовое событие пула
    остаётся одинокой точкой. Достижимость такого узла не касается (она бережёт
    смысл «жду сообщение»), но процесс без первой дуги не выполняется.
    """
    adjacency: Dict[str, List[str]] = {}
    targets: Set[str] = set()
    for f in flows:
        if f["kind"] == "sequence":
            adjacency.setdefault(f["source"], []).append(f["target"])
            targets.add(f["target"])

    for p in participants:
        pool = [e for e in elements if e["participant"] == p["name"]]
        for start in [e for e in pool if e["kind"] == "startEvent"]:
            if adjacency.get(start["id"]):
                continue
            entry = _pick_entry_node(pool, targets)
            if entry is None:
                # Пул из одних событий: вести старт некуда, а выдумывать шаг
                # нельзя. Безвыходное положение обязано быть видно в notes.
                notes.append(f"Старт {start['id']} в пуле «{p['name']}» остался без "
                             "продолжения: в пуле нет ни одного шага")
                continue
            if _reachable(adjacency, entry["id"], start["id"]):
                notes.append(f"Старт {start['id']} не подключён к «{entry['name']}»: "
                             "поток замкнул бы цикл")
                continue
            flows.append(_new_flow(used_ids, start["id"], entry["id"]))
            adjacency.setdefault(start["id"], []).append(entry["id"])
            targets.add(entry["id"])
            notes.append(f"Старт {start['id']} был без исходящего потока — "
                         f"соединён с «{entry['name']}» ({entry['id']})")


def _close_pool_paths(elements: List[Dict[str, Any]],
                      flows: List[Dict[str, Any]],
                      participants: List[Dict[str, Any]],
                      used_ids: Set[str], notes: List[str]) -> None:
    """Шаг без исходящего sequence-потока завершается конечным событием.

    Частый случай — модель раздала роли одной организации по пулам: линия
    «заявка → согласование → выдача» превращается в цепочку messageFlow, и
    внутри своего пула каждый шаг остаётся без продолжения. XML от этого
    формально не ломается, но маршрут обрывается тупиком, который скоринг
    правомерно считает. Поток-сообщение продолжения не заменяет: оно уходит
    в другой пул, а процесс в своём остаётся незакрытым.
    """
    adjacency: Dict[str, List[str]] = {}
    for f in flows:
        if f["kind"] == "sequence":
            adjacency.setdefault(f["source"], []).append(f["target"])
    sends_message = {f["source"] for f in flows if f["kind"] == "message"}
    budget = MAX_REACH_FLOWS
    for p in participants:
        pool = [e for e in elements if e["participant"] == p["name"]]
        ends = [e for e in pool if e["kind"] == "endEvent"]
        if not ends:
            continue
        end = ends[0]
        for e in pool:
            if e["kind"] in ("startEvent", "endEvent") or adjacency.get(e["id"]):
                continue
            if budget <= 0:
                notes.append(f"Шаг {e['id']} остался без исходящего потока: "
                             f"лимит починки связности ({MAX_REACH_FLOWS})")
                continue
            if _reachable(adjacency, end["id"], e["id"]):
                notes.append(f"Шаг {e['id']} не присоединён к «{end['name']}»: "
                             "поток замкнул бы цикл")
                continue
            flows.append(_new_flow(used_ids, e["id"], end["id"]))
            adjacency.setdefault(e["id"], []).append(end["id"])
            budget -= 1
            note = (f"Шаг {e['id']} в пуле «{p['name']}» был без исходящего "
                    f"потока — присоединён к «{end['name']}» ({end['id']})")
            if e["id"] in sends_message:
                note += (". Он отправляет сообщение в другой пул и на этом "
                         "заканчивается: если получатель — сотрудник той же "
                         "организации, это дорожка одного пула, а не второй пул")
            notes.append(note)


def _ensure_reachability(elements: List[Dict[str, Any]],
                         flows: List[Dict[str, Any]],
                         used_ids: Set[str], notes: List[str]) -> None:
    """Каждый узел без входящего потока подключается к старту своего пула.

    Промпт обещает достижимость, но модель её не гарантирует. Ребро не
    добавляется, только если оно замкнуло бы цикл. Узел, дожидаться сообщения
    из другого пула, тоже получает свой локальный вход: messageFlow показывает
    внешний триггер, но не запускает процесс внутри пула, и без локального
    входа узел — тупик.
    """
    seq = [f for f in flows if f["kind"] == "sequence"]
    has_incoming = {f["target"] for f in seq}
    waits_for_message = {f["target"] for f in flows if f["kind"] == "message"}
    adjacency: Dict[str, List[str]] = {}
    for f in seq:
        adjacency.setdefault(f["source"], []).append(f["target"])
    start_by_pool: Dict[str, str] = {}
    for e in elements:
        if e["kind"] == "startEvent":
            start_by_pool.setdefault(e["participant"], e["id"])

    added = 0
    for e in elements:
        if e["kind"] in ("startEvent", "boundaryEvent") \
                or e["id"] in has_incoming:
            # Граничное событие запускается хозяином: подводить к нему поток от
            # старта — значит нарисовать второй несуществующий триггер.
            continue
        start = start_by_pool.get(e["participant"])
        if start is None or start == e["id"]:
            continue
        if added >= MAX_REACH_FLOWS:
            notes.append(f"Элемент {e['id']} остался без входящего потока: "
                         f"лимит исправлений достижимости ({MAX_REACH_FLOWS})")
            continue
        if _reachable(adjacency, e["id"], start):
            notes.append(f"Элемент {e['id']} не подключён к старту: поток "
                         "замкнул бы цикл")
            continue
        flow = _new_flow(used_ids, start, e["id"])
        flows.append(flow)
        adjacency.setdefault(start, []).append(e["id"])
        has_incoming.add(e["id"])
        added += 1
        note = (f"Элемент {e['id']} был без входящего потока — подключён "
                f"от стартового события {start}")
        if e["id"] in waits_for_message:
            # Шаг принимает сообщение из другого пула, но процесс в своём пуле
            # обязан с чего-то начинаться: без локального входа это тупик.
            # MessageFlow остаётся — он показывает внешний триггер.
            note += ("; шаг дополнительно ожидает сообщение из другого пула, "
                     "но процесс в своём пуле обязан начинаться с его старта")
        notes.append(note)


def _reachable(adjacency: Dict[str, List[str]], source: str,
               target: str) -> bool:
    seen: Set[str] = set()
    stack = [source]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def _unique_id(used: Set[str], base: str) -> str:
    candidate = _sanitize_id(base, "Elem")
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{_sanitize_id(base, 'Elem')}_{n}"
    return candidate


def _new_flow(used_ids: Set[str], source: str, target: str) -> Dict[str, Any]:
    flow_id = _unique_id(used_ids, f"Flow_{source}_{target}")
    used_ids.add(flow_id)
    return {"id": flow_id, "kind": "sequence",
            "source": source, "target": target, "condition": ""}


def _pick_entry_node(pool: List[Dict[str, Any]],
                     targets: Set[str]) -> Optional[Dict[str, Any]]:
    """Первый узел пула без входящих sequence-потоков (не старт)."""
    for e in pool:
        if e["kind"] == "startEvent":
            continue
        if e["id"] not in targets:
            return e
    for e in pool:
        if e["kind"] not in ("startEvent", "endEvent"):
            return e
    return None


def _pick_exit_node(pool: List[Dict[str, Any]],
                    seq: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Последний узел пула без исходящих потоков (не финиш)."""
    sources = {f["source"] for f in seq}
    for e in reversed(pool):
        if e["kind"] == "endEvent":
            continue
        if e["id"] not in sources:
            return e
    for e in reversed(pool):
        if e["kind"] not in ("startEvent", "endEvent"):
            return e
    return None

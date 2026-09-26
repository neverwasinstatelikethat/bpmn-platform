/**
 * Учебные примеры для «Мастерской BPMN» (/guideline).
 *
 * Подписи на схемах — бизнесовые, с одним сюжетом (сборка заказа), чтобы
 * аналитик читал диаграмму, а не разбирал `Task`/`End 1`. В текстах элементы
 * описаны ролью: внутренние id (`Start_1`, `Flow_2`) и имена атрибутов
 * (`calledElement`) остаются только в XML, где они технически необходимы.
 */
export const guidelineExamples = {
    'startEvent': {
        title: 'Начальное событие',
        description: `**Начальное событие** — точка входа в процесс: оно показывает, с чего всё начинается. Входящих переходов у него нет, и в одном процессе оно должно быть единственным — иначе неясно, откуда читать схему. Событие бывает простым или с триггером (таймер, сообщение): тогда процесс запускают не действие человека, а время или внешний сигнал.

**Неправильный пример:** в процессе два начальных события — «Заказ оформлен» и «Заказ из 1С». Второе не связано ни с чем, и читатель не понимает, какой запуск считать основным.

**Правильный пример:** одно начальное событие «Заказ оформлен» одним переходом ведёт к конечному «Заказ собран». У процесса одна точка входа, и он читается слева направо.`,
        incorrectExample: {
            title: 'Ошибка: Несколько начальных событий',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_1" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:startEvent id="Start_2" name="Заказ из 1С" />
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_1">
        <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Process_1">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Start_2_di" bpmnElement="Start_2"><dc:Bounds x="100" y="270" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="330" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="330" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Второе начальное событие повисает отдельно — процесс теряет точку входа.'
        },
        correctExample: {
            title: 'Правильно: Одно начальное событие',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_2" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_2" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_2">
        <bpmndi:BPMNPlane id="BPMNPlane_2" bpmnElement="Process_2">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="330" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="330" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Одно начальное событие корректно запускает процесс.'
        },
        evaluationCriteria: { requiredElements: ['startEvent'], maxOccurrences: 1, forbiddenElements: [] }
    },
    'task': {
        title: 'Задача',
        description: `**Задача** — один шаг работы: участник выполняет одно действие. Задача бывает ручной, автоматической или пользовательской, но в любом случае это бизнес-шаг, а не нажатие кнопки. Ей нужны входящий и исходящий переходы — иначе процесс обрывается на середине.

**Неправильный пример:** задача «Собрать заказ» получает переход от начального события, но отдать результат некуда. После сборки процесс зависает — схема незавершена.

**Правильный пример:** в задачу «Собрать заказ» переход приходит от начального события, а из задачи уходит к конечному «Заказ собран». Каждый шаг связан с соседями, процесс читается целиком.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие исходящего перехода',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_3" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_3" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Собрать заказ"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:task>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_3">
        <bpmndi:BPMNPlane id="BPMNPlane_3" bpmnElement="Process_3">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="300" y="135" width="140" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Задача без исходящего перехода оставляет процесс незавершённым.'
        },
        correctExample: {
            title: 'Правильно: Задача с исходящим переходом',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_4" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_4" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Собрать заказ"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_4">
        <bpmndi:BPMNPlane id="BPMNPlane_4" bpmnElement="Process_4">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="300" y="135" width="140" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="520" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="520" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Задача связана исходящим переходом с конечным событием.'
        },
        evaluationCriteria: { requiredElements: ['task'], outgoingFlows: true }
    },
    'exclusiveGateway': {
        title: 'Эксклюзивный шлюз',
        description: `**Эксклюзивный шлюз** выбирает один путь из нескольких — по условию, которое подписано на переходе. Это ромб с крестом внутри: «или — или». Его ставят там, где участник принимает решение: например, хватает ли товара на складе.

**Неправильный пример:** шлюз «Товара хватает?» разводит процесс на «Заказ собран» и «Заказ отложен», но переходы не подписаны. При каком условии выбирается какая ветка — неизвестно, развилка превращается в гадание.

**Правильный пример:** тот же шлюз, а исходящие переходы подписаны условиями «Да» и «Нет». Каждый исход назван, и по схеме видно, что бывает при нехватке товара.`,
        incorrectExample: {
            title: 'Ошибка: Без условий',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_5" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_5" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:exclusiveGateway id="Gateway_1" name="Товара хватает?"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:exclusiveGateway>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:endEvent id="End_2" name="Заказ отложен"><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="End_2" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_5">
        <bpmndi:BPMNPlane id="BPMNPlane_5" bpmnElement="Process_5">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="220" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="250" y="215" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="420" y="130" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_2_di" bpmnElement="End_2"><dc:Bounds x="420" y="300" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="238" /><di:waypoint x="250" y="240" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="148" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="318" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Без условий на исходящих переходах выбор пути неопределён.'
        },
        correctExample: {
            title: 'Правильно: С условиями',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_6" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_6" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:exclusiveGateway id="Gateway_1" name="Товара хватает?"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:exclusiveGateway>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:endEvent id="End_2" name="Заказ отложен"><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" name="Да" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="End_2" name="Нет" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_6">
        <bpmndi:BPMNPlane id="BPMNPlane_6" bpmnElement="Process_6">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="220" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="250" y="215" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="420" y="130" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_2_di" bpmnElement="End_2"><dc:Bounds x="420" y="300" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="238" /><di:waypoint x="250" y="240" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="148" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="318" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Условия на исходящих переходах делают ветвление понятным.'
        },
        evaluationCriteria: { requiredElements: ['exclusiveGateway'], conditions: true }
    },
    'parallelGateway': {
        title: 'Параллельный шлюз',
        description: `**Параллельный шлюз** запускает все исходящие ветки одновременно, а на сборе ждёт, пока выполнятся они все. Это ромб с плюсом внутри: «и — и». У него всегда не меньше двух исходящих переходов, и условия на них не ставят — выполняются обе ветки.

**Неправильный пример:** после начального события стоит параллельный шлюз с одним исходящим переходом, да ещё подписанным условием. Ветвлений нет, зато есть выбор одного пути — это работа эксклюзивного шлюза, а не параллельного.

**Правильный пример:** шлюз «Параллельно» одновременно запускает «Собрать заказ» и «Оформить накладную», и обе ветки сходятся в конечном событии «Заказ собран». Работы идут в параллели, результат один.`,
        incorrectExample: {
            title: 'Ошибка: Использование как эксклюзивного',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_7" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_7" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:parallelGateway id="Gateway_1" name="Параллельно"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:parallelGateway>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" name="Если товара хватает" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_7">
        <bpmndi:BPMNPlane id="BPMNPlane_7" bpmnElement="Process_7">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="250" y="145" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="420" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="250" y="170" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="300" y="170" /><di:waypoint x="420" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Параллельный шлюз с одним путём ничего не ветвит.'
        },
        correctExample: {
            title: 'Правильно: Множественные пути',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_8" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_8" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:parallelGateway id="Gateway_1" name="Параллельно"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:parallelGateway>
        <bpmn:task id="Task_1" name="Собрать заказ"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:outgoing>Flow_4</bpmn:outgoing></bpmn:task>
        <bpmn:task id="Task_2" name="Оформить накладную"><bpmn:incoming>Flow_3</bpmn:incoming><bpmn:outgoing>Flow_5</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_4</bpmn:incoming><bpmn:incoming>Flow_5</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="Task_2" />
        <bpmn:sequenceFlow id="Flow_4" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_5" sourceRef="Task_2" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_8">
        <bpmndi:BPMNPlane id="BPMNPlane_8" bpmnElement="Process_8">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="220" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="250" y="215" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="420" y="120" width="140" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_2_di" bpmnElement="Task_2"><dc:Bounds x="420" y="290" width="140" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="680" y="215" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="238" /><di:waypoint x="250" y="240" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="145" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="300" y="240" /><di:waypoint x="420" y="315" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_4_di" bpmnElement="Flow_4"><di:waypoint x="560" y="145" /><di:waypoint x="680" y="233" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_5_di" bpmnElement="Flow_5"><di:waypoint x="560" y="315" /><di:waypoint x="680" y="233" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Обе ветки запускаются одновременно и сходятся в одном исходе.'
        },
        evaluationCriteria: { requiredElements: ['parallelGateway'], minOutgoingFlows: 2 }
    },
    'boundaryEvent': {
        title: 'Граничное событие',
        description: `**Граничное событие** висит на краю задачи и описывает, что происходит, пока она выполняется: пришёл сигнал, вышел срок, произошла ошибка. Событие бывает прерывающим — шаг останавливается — и непрерывающим — шаг продолжается.

**Неправильный пример:** задача «Списать товар» ведёт сразу к «Заказ собран», и у сбоя нет выхода. Если товара не оказалось, читать схему нечего — обрабатывать исключение нечем.

**Правильный пример:** к задаче «Списать товар» прикреплено граничное событие «Товара нет». При сбое процесс уходит на «Подобрать замену», а обычный путь к «Заказ собран» остаётся.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие обработки',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_9" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_9" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Списать товар"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_9">
        <bpmndi:BPMNPlane id="BPMNPlane_9" bpmnElement="Process_9">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="560" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'У задачи нет граничного события, куда уйти при ошибке.'
        },
        correctExample: {
            title: 'Правильно: С обработкой ошибки',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_10" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_10" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Списать товар"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:boundaryEvent id="Error_1" name="Товара нет" attachedToRef="Task_1"><bpmn:errorEventDefinition /><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:boundaryEvent>
        <bpmn:task id="Handle_1" name="Подобрать замену"><bpmn:incoming>Flow_3</bpmn:incoming><bpmn:outgoing>Flow_4</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:incoming>Flow_4</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Error_1" targetRef="Handle_1" />
        <bpmn:sequenceFlow id="Flow_4" sourceRef="Handle_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_10">
        <bpmndi:BPMNPlane id="BPMNPlane_10" bpmnElement="Process_10">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Error_1_di" bpmnElement="Error_1"><dc:Bounds x="350" y="172" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Handle_1_di" bpmnElement="Handle_1"><dc:Bounds x="500" y="240" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="740" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="740" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="368" y="208" /><di:waypoint x="500" y="270" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_4_di" bpmnElement="Flow_4"><di:waypoint x="640" y="270" /><di:waypoint x="758" y="178" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Граничное событие уводит процесс в обработку сбоя.'
        },
        evaluationCriteria: { requiredElements: ['boundaryEvent'], attachedToTask: true }
    },
    'subProcess': {
        title: 'Подпроцесс',
        description: `**Подпроцесс** прячет кусок логики внутрь себя: на общей схеме это один блок, а детали — свои события и шаги — живут внутри него. У подпроцесса есть собственные начальное и конечное события, и сворачивают его, чтобы не засорять верхний уровень.

**Неправильный пример:** блок «Логистика» назван подпроцессом, но внутри пусто. Он только занимает место и ничего не объясняет — сворачивать нечего.

**Правильный пример:** внутри «Логистики» лежат свои начальное и конечное события и задача «Собрать заказ». Основной процесс входит в подпроцесс одним переходом и выходит одним — детали верхний уровень не засоряют.`,
        incorrectExample: {
            title: 'Ошибка: Пустой подпроцесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_11" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_11" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:subProcess id="Sub_1" name="Логистика"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:subProcess>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Sub_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Sub_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_11">
        <bpmndi:BPMNPlane id="BPMNPlane_11" bpmnElement="Process_11">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Sub_1_di" bpmnElement="Sub_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="560" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Подпроцесс без внутренних элементов бесполезен.'
        },
        correctExample: {
            title: 'Правильно: Подпроцесс с задачами',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_12" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_12" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:subProcess id="Sub_1" name="Логистика"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing>
            <bpmn:startEvent id="SubStart_1" name="Начало"><bpmn:outgoing>SubFlow_1</bpmn:outgoing></bpmn:startEvent>
            <bpmn:task id="Task_1" name="Собрать заказ"><bpmn:incoming>SubFlow_1</bpmn:incoming><bpmn:outgoing>SubFlow_2</bpmn:outgoing></bpmn:task>
            <bpmn:endEvent id="SubEnd_1" name="Готово"><bpmn:incoming>SubFlow_2</bpmn:incoming></bpmn:endEvent>
            <bpmn:sequenceFlow id="SubFlow_1" sourceRef="SubStart_1" targetRef="Task_1" />
            <bpmn:sequenceFlow id="SubFlow_2" sourceRef="Task_1" targetRef="SubEnd_1" />
        </bpmn:subProcess>
        <bpmn:endEvent id="End_1" name="Заказ отгружен"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Sub_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Sub_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_12">
        <bpmndi:BPMNPlane id="BPMNPlane_12" bpmnElement="Process_12">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="80" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Sub_1_di" bpmnElement="Sub_1" isExpanded="true"><dc:Bounds x="260" y="80" width="380" height="180" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubStart_1_di" bpmnElement="SubStart_1"><dc:Bounds x="290" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="390" y="145" width="140" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubEnd_1_di" bpmnElement="SubEnd_1"><dc:Bounds x="580" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="740" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="116" y="160" /><di:waypoint x="260" y="170" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="640" y="170" /><di:waypoint x="740" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="SubFlow_1_di" bpmnElement="SubFlow_1"><di:waypoint x="326" y="168" /><di:waypoint x="390" y="170" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="SubFlow_2_di" bpmnElement="SubFlow_2"><di:waypoint x="530" y="170" /><di:waypoint x="580" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Внутри подпроцесса — свои события и задача.'
        },
        evaluationCriteria: { requiredElements: ['subProcess'], minInternalElements: 2 }
    },
    'messageEvent': {
        title: 'Событие сообщения',
        description: `**Событие сообщения** показывает обмен между процессами или участниками: один отправляет сообщение, другой его ждёт. Так на схеме видно, что дальше результат зависит не от нас, а от ответа смежной системы или коллеги.

**Неправильный пример:** процесс идёт от «Заказ оформлен» сразу к «Заказ собран». Взаимодействия с внешним миром на схеме нет — не видно, кто и когда получает уведомление.

**Правильный пример:** после «Заказ оформлен» идёт событие «Уведомить клиента», и только потом — «Заказ собран». Отправка сообщения видна отдельным шагом процесса.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие целевого сообщения',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_13" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_13" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_13">
        <bpmndi:BPMNPlane id="BPMNPlane_13" bpmnElement="Process_13">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="330" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="330" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Процесс без события сообщения: уведомления не видно.'
        },
        correctExample: {
            title: 'Правильно: Событие отправки сообщения',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_14" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_14" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:intermediateThrowEvent id="Message_1" name="Уведомить клиента"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:messageEventDefinition /></bpmn:intermediateThrowEvent>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Message_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Message_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_14">
        <bpmndi:BPMNPlane id="BPMNPlane_14" bpmnElement="Process_14">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Message_1_di" bpmnElement="Message_1"><dc:Bounds x="330" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="330" y="168" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="366" y="168" /><di:waypoint x="560" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Отправка сообщения — отдельный шаг процесса.'
        },
        evaluationCriteria: { requiredElements: ['messageEventDefinition'], minOccurrences: 1 }
    },
    'timerEvent': {
        title: 'Событие таймера',
        description: `**Событие таймера** привязывает процесс ко времени: запуск по расписанию, ожидание или контроль срока. Чаще всего его вешают на задачу — тогда видно, что делать, если задача не выполнена вовремя.

**Неправильный пример:** на схеме только «Заказ оформлен» и «Заказ собран». Таймера нет вовсе, поэтому срок ничем не контролируется: клиент ждёт, а процесс об этом ничего не знает.

**Правильный пример:** на задаче «Собрать заказ» висит граничное событие «Вышел срок». Если задача не уложилась в него, процесс уходит по этому событию к «Заказ собран»; обычный путь остаётся рабочим.`,
        incorrectExample: {
            title: 'Ошибка: Без привязки к задаче',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_15" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_15" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_15">
        <bpmndi:BPMNPlane id="BPMNPlane_15" bpmnElement="Process_15">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="330" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="330" y="168" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Таймер не привязан ни к одному элементу процесса.'
        },
        correctExample: {
            title: 'Правильно: Таймер на границе задачи',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_16" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_16" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Собрать заказ"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:boundaryEvent id="Timer_1" name="Вышел срок" attachedToRef="Task_1"><bpmn:timerEventDefinition /><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:boundaryEvent>
        <bpmn:endEvent id="End_1" name="Заказ собран"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Timer_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_16">
        <bpmndi:BPMNPlane id="BPMNPlane_16" bpmnElement="Process_16">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Timer_1_di" bpmnElement="Timer_1"><dc:Bounds x="350" y="172" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="560" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="368" y="208" /><di:waypoint x="560" y="178" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Таймер привязан к задаче и контролирует срок.'
        },
        evaluationCriteria: { requiredElements: ['timerEventDefinition'], attachedToTask: true }
    },
    'callActivity': {
        title: 'Вызов активности',
        description: `**Вызов активности** запускает другой, отдельный процесс — как ссылку на него. Так переиспользуют готовую схему: доставка один раз описана отдельно и вызывается из разных процессов. В свойствах элемента нужно указать, какой именно процесс вызывается.

**Неправильный пример:** блок «Доставка» подписан как вызов активности, но вызываемый процесс не выбран. Схема обещает продолжение, которого нет: в реальном исполнении такой шаг неработоспособен.

**Правильный пример:** «Доставка» ссылается на отдельный процесс со своими начальным и конечным событиями. Основной процесс выглядит компактно, а детали лежат в вызываемой схеме.`,
        incorrectExample: {
            title: 'Ошибка: Без ссылки на процесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_17" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_17" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:callActivity id="Call_1" name="Доставка"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:callActivity>
        <bpmn:endEvent id="End_1" name="Заказ доставлен"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Call_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Call_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_17">
        <bpmndi:BPMNPlane id="BPMNPlane_17" bpmnElement="Process_17">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Call_1_di" bpmnElement="Call_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="560" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Вызов активности без указанной вызываемой схемы.'
        },
        correctExample: {
            title: 'Правильно: Ссылка на процесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_18" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_18" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Заказ оформлен"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:callActivity id="Call_1" name="Доставка" calledElement="SubProcess_1"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:callActivity>
        <bpmn:endEvent id="End_1" name="Заказ доставлен"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Call_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Call_1" targetRef="End_1" />
    </bpmn:process>
    <bpmn:process id="SubProcess_1" isExecutable="false">
        <bpmn:startEvent id="SubStart_1" name="Начало доставки"><bpmn:outgoing>SubFlow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="SubEnd_1" name="Доставка завершена"><bpmn:incoming>SubFlow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="SubFlow_1" sourceRef="SubStart_1" targetRef="SubEnd_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_18">
        <bpmndi:BPMNPlane id="BPMNPlane_18" bpmnElement="Process_18">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Call_1_di" bpmnElement="Call_1"><dc:Bounds x="300" y="130" width="140" height="60" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="560" y="142" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="168" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="440" y="160" /><di:waypoint x="560" y="160" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
        <bpmndi:BPMNPlane id="BPMNPlane_Sub_1" bpmnElement="SubProcess_1">
            <bpmndi:BPMNShape id="SubStart_1_di" bpmnElement="SubStart_1"><dc:Bounds x="50" y="50" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubEnd_1_di" bpmnElement="SubEnd_1"><dc:Bounds x="250" y="50" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="SubFlow_1_di" bpmnElement="SubFlow_1"><di:waypoint x="86" y="68" /><di:waypoint x="250" y="68" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Вызов активности со ссылкой на отдельный процесс.'
        },
        evaluationCriteria: { requiredElements: ['callActivity'], calledElement: true }
    }
};

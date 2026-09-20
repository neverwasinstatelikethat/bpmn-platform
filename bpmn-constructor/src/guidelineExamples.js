export const guidelineExamples = {
    'startEvent': {
        title: 'Начальное событие',
        description: `**Начальное событие (Start Event)** — это точка входа в бизнес-процесс, обозначающая его начало. Оно не имеет входящих потоков и должно быть только одно в рамках одного процесса для четкой отправной точки. Начальные события могут быть простыми или с триггерами (таймер, сообщение), что позволяет запускать процесс по событию. 

**Неправильный пример:** Процесс содержит два начальных события ("Start_1" и "Start_2"), что создает неопределенность. Поток "Flow_1" идет от "Start_1" к "End_1", но "Start_2" изолирован, нарушая стандарт BPMN, требующий единственной точки входа.

**Правильный пример:** Один начальное событие ("Start_1") через поток "Flow_1" ведет к "End_1", обеспечивая линейный и предсказуемый процесс, соответствующий стандартам.`,
        incorrectExample: {
            title: 'Ошибка: Несколько начальных событий',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_1" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start 1"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:startEvent id="Start_2" name="Start 2" />
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_1">
        <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Process_1">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Start_2_di" bpmnElement="Start_2"><dc:Bounds x="100" y="200" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="200" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Множественные начальные события нарушают целостность процесса.'
        },
        correctExample: {
            title: 'Правильно: Одно начальное событие',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_2" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_2" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_2">
        <bpmndi:BPMNPlane id="BPMNPlane_2" bpmnElement="Process_2">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="200" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Одно начальное событие корректно запускает процесс.'
        },
        evaluationCriteria: { requiredElements: ['startEvent'], maxOccurrences: 1, forbiddenElements: [] }
    },
    'task': {
        title: 'Задача',
        description: `**Задача (Task)** — это базовая единица работы, представляющая выполнение действия участником процесса. Задачи бывают ручными, автоматическими или пользовательскими и требуют входящего и исходящего потока для непрерывности процесса.

**Неправильный пример:** Задача "Task_1" следует за "Start_1", но не имеет исходящего потока. Поток "Flow_1" приводит к задаче, но после ее выполнения процесс "зависает", что делает диаграмму незавершенной.

**Правильный пример:** Задача "Task_1" корректно связана: "Flow_1" от "Start_1" ведет к задаче, а "Flow_2" направляет выполнение к "End_1", обеспечивая непрерывность процесса.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие исходящего потока',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_3" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_3" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Task"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:task>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_3">
        <bpmndi:BPMNPlane id="BPMNPlane_3" bpmnElement="Process_3">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="200" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="125" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Задача без исходящего потока оставляет процесс незавершенным.'
        },
        correctExample: {
            title: 'Правильно: Задача с исходящим потоком',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_4" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_4" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Task"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_4">
        <bpmndi:BPMNPlane id="BPMNPlane_4" bpmnElement="Process_4">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="200" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="350" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="300" y="125" /><di:waypoint x="350" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Задача с корректным исходящим потоком к конечному событию.'
        },
        evaluationCriteria: { requiredElements: ['task'], outgoingFlows: true }
    },
    'exclusiveGateway': {
        title: 'Эксклюзивный шлюз',
        description: `**Эксклюзивный шлюз (Exclusive Gateway, XOR)** управляет ветвлением с выбором одного пути на основе условий. Имеет ромбовидную форму с "X" и используется для принятия решений (например, по статусу заказа).

**Неправильный пример:** Шлюз "Gateway_1" после "Start_1" ведет к "End_1" и "End_2", но без условий на "Flow_2" и "Flow_3", что делает выбор пути неопределенным и нарушает логику процесса.

**Правильный пример:** Шлюз "Gateway_1" использует условия ("Condition 1" и "Condition 2") на потоках "Flow_2" и "Flow_3", направляя процесс либо к "End_1", либо к "End_2", что обеспечивает четкое ветвление.`,
        incorrectExample: {
            title: 'Ошибка: Без условий',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_5" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_5" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:exclusiveGateway id="Gateway_1" name="Decision"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:exclusiveGateway>
        <bpmn:endEvent id="End_1" name="End 1"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:endEvent id="End_2" name="End 2"><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="End_2" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_5">
        <bpmndi:BPMNPlane id="BPMNPlane_5" bpmnElement="Process_5">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="200" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="150" y="200" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="250" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_2_di" bpmnElement="End_2"><dc:Bounds x="250" y="250" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="200" /><di:waypoint x="150" y="225" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="150" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="250" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Отсутствие условий на исходящих потоках делает выбор пути неопределенным.'
        },
        correctExample: {
            title: 'Правильно: С условиями',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_6" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_6" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:exclusiveGateway id="Gateway_1" name="Decision"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:exclusiveGateway>
        <bpmn:endEvent id="End_1" name="End 1"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:endEvent id="End_2" name="End 2"><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" name="Condition 1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="End_2" name="Condition 2" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_6">
        <bpmndi:BPMNPlane id="BPMNPlane_6" bpmnElement="Process_6">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="200" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="150" y="200" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="250" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_2_di" bpmnElement="End_2"><dc:Bounds x="250" y="250" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="200" /><di:waypoint x="150" y="225" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="150" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="250" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Условия на исходящих потоках обеспечивают четкое ветвление.'
        },
        evaluationCriteria: { requiredElements: ['exclusiveGateway'], conditions: true }
    },
    'parallelGateway': {
        title: 'Параллельный шлюз',
        description: `**Параллельный шлюз (Parallel Gateway, AND)** активирует все исходящие пути одновременно или синхронизирует входящие пути. Имеет ромбовидную форму с "+". Используется для параллельного выполнения задач.

**Неправильный пример:** Шлюз "Gateway_1" после "Start_1" имеет один исходящий поток "Flow_2" с условием, что противоречит назначению параллельного шлюза, требующего минимум два пути.

**Правильный пример:** Шлюз "Gateway_1" разветвляет процесс на "Task_1" и "Task_2" через "Flow_2" и "Flow_3", а затем синхронизирует их перед "End_1" через "Flow_4" и "Flow_5", демонстрируя параллельное выполнение.`,
        incorrectExample: {
            title: 'Ошибка: Использование как эксклюзивного',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_7" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_7" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:parallelGateway id="Gateway_1" name="Parallel"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:parallelGateway>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="End_1" name="Condition" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_7">
        <bpmndi:BPMNPlane id="BPMNPlane_7" bpmnElement="Process_7">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="150" y="100" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="250" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="200" y="125" /><di:waypoint x="250" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Параллельный шлюз с одним путем не имеет смысла.'
        },
        correctExample: {
            title: 'Правильно: Множественные пути',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_8" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_8" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:parallelGateway id="Gateway_1" name="Fork"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:parallelGateway>
        <bpmn:task id="Task_1" name="Task 1"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:outgoing>Flow_4</bpmn:outgoing></bpmn:task>
        <bpmn:task id="Task_2" name="Task 2"><bpmn:incoming>Flow_3</bpmn:incoming><bpmn:outgoing>Flow_5</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_4</bpmn:incoming><bpmn:incoming>Flow_5</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Gateway_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Gateway_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Gateway_1" targetRef="Task_2" />
        <bpmn:sequenceFlow id="Flow_4" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_5" sourceRef="Task_2" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_8">
        <bpmndi:BPMNPlane id="BPMNPlane_8" bpmnElement="Process_8">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="200" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Gateway_1_di" bpmnElement="Gateway_1"><dc:Bounds x="150" y="200" width="50" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="250" y="150" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_2_di" bpmnElement="Task_2"><dc:Bounds x="250" y="250" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="400" y="200" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="200" /><di:waypoint x="150" y="225" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="175" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="200" y="225" /><di:waypoint x="250" y="275" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_4_di" bpmnElement="Flow_4"><di:waypoint x="350" y="175" /><di:waypoint x="400" y="200" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_5_di" bpmnElement="Flow_5"><di:waypoint x="350" y="275" /><di:waypoint x="400" y="200" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Множественные параллельные пути корректно реализованы.'
        },
        evaluationCriteria: { requiredElements: ['parallelGateway'], minOutgoingFlows: 2 }
    },
    'boundaryEvent': {
        title: 'Граничное событие',
        description: `**Граничное событие (Boundary Event)** прикрепляется к задаче для обработки исключений (ошибки, таймеры). Может быть прерывающим или непрерывающим, улучшая надежность процесса.

**Неправильный пример:** Задача "Task_1" после "Start_1" ведет к "End_1" без граничного события, что делает процесс уязвимым к ошибкам, так как нет механизма их обработки.

**Правильный пример:** Задача "Task_1" имеет граничное событие ошибки ("Error_1"), которое при срабатывании направляет процесс к "Handle_1" через "Flow_3". Основной путь через "Flow_2" остается, если ошибок нет, что демонстрирует надежное управление исключениями.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие обработки',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_9" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_9" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Task"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_9">
        <bpmndi:BPMNPlane id="BPMNPlane_9" bpmnElement="Process_9">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="300" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Задача без граничного события для обработки ошибок.'
        },
        correctExample: {
            title: 'Правильно: С обработкой ошибки',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_10" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_10" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Task"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:boundaryEvent id="Error_1" name="Error" attachedToRef="Task_1"><bpmn:errorEventDefinition /><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:boundaryEvent>
        <bpmn:task id="Handle_1" name="Handle"><bpmn:incoming>Flow_3</bpmn:incoming><bpmn:outgoing>Flow_4</bpmn:outgoing></bpmn:task>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:incoming>Flow_4</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Error_1" targetRef="Handle_1" />
        <bpmn:sequenceFlow id="Flow_4" sourceRef="Handle_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_10">
        <bpmndi:BPMNPlane id="BPMNPlane_10" bpmnElement="Process_10">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Error_1_di" bpmnElement="Error_1"><dc:Bounds x="200" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Handle_1_di" bpmnElement="Handle_1"><dc:Bounds x="250" y="150" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="400" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="400" y="118" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="218" y="150" /><di:waypoint x="250" y="175" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_4_di" bpmnElement="Flow_4"><di:waypoint x="350" y="175" /><di:waypoint x="400" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Граничное событие корректно обрабатывает ошибки.'
        },
        evaluationCriteria: { requiredElements: ['boundaryEvent'], attachedToTask: true }
    },
    'subProcess': {
        title: 'Подпроцесс',
        description: `**Подпроцесс (SubProcess)** группирует сложную логику внутри процесса с собственными начальным и конечным событиями. Может быть свернутым или развернутым для скрытия/показа деталей.

**Неправильный пример:** Подпроцесс "Sub_1" после "Start_1" ведет к "End_1", но внутри пуст, что делает его бесполезным, так как подпроцесс должен содержать внутреннюю логику.

**Правильный пример:** Подпроцесс "Sub_1" содержит "SubStart_1", "Task_1" и "SubEnd_1". После "Start_1" процесс входит в подпроцесс через "Flow_1", проходит внутренний цикл ("SubFlow_1" и "SubFlow_2"), и выходит через "Flow_2" к "End_1", демонстрируя структурированную логику.`,
        incorrectExample: {
            title: 'Ошибка: Пустой подпроцесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_11" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_11" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:subProcess id="Sub_1" name="SubProcess"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:subProcess>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Sub_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Sub_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_11">
        <bpmndi:BPMNPlane id="BPMNPlane_11" bpmnElement="Process_11">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Sub_1_di" bpmnElement="Sub_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="300" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
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
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:subProcess id="Sub_1" name="SubProcess"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing>
            <bpmn:startEvent id="SubStart_1" name="SubStart"><bpmn:outgoing>SubFlow_1</bpmn:outgoing></bpmn:startEvent>
            <bpmn:task id="Task_1" name="SubTask"><bpmn:incoming>SubFlow_1</bpmn:incoming><bpmn:outgoing>SubFlow_2</bpmn:outgoing></bpmn:task>
            <bpmn:endEvent id="SubEnd_1" name="SubEnd"><bpmn:incoming>SubFlow_2</bpmn:incoming></bpmn:endEvent>
            <bpmn:sequenceFlow id="SubFlow_1" sourceRef="SubStart_1" targetRef="Task_1" />
            <bpmn:sequenceFlow id="SubFlow_2" sourceRef="Task_1" targetRef="SubEnd_1" />
        </bpmn:subProcess>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Sub_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Sub_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_12">
        <bpmndi:BPMNPlane id="BPMNPlane_12" bpmnElement="Process_12">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Sub_1_di" bpmnElement="Sub_1" isExpanded="true"><dc:Bounds x="150" y="50" width="250" height="150" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubStart_1_di" bpmnElement="SubStart_1"><dc:Bounds x="170" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="SubTask"><dc:Bounds x="220" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubEnd_1_di" bpmnElement="SubEnd_1"><dc:Bounds x="350" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="450" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="400" y="125" /><di:waypoint x="450" y "118" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="SubFlow_1_di" bpmnElement="SubFlow_1"><di:waypoint x="206" y="118" /><di:waypoint x="220" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="SubFlow_2_di" bpmnElement="SubFlow_2"><di:waypoint x="320" y="125" /><di:waypoint x="350" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Подпроцесс с внутренними элементами корректно структурирован.'
        },
        evaluationCriteria: { requiredElements: ['subProcess'], minInternalElements: 2 }
    },
    'messageEvent': {
        title: 'Событие сообщения',
        description: `**Событие сообщения (Message Event)** моделирует обмен данными между процессами или участниками. Может быть отправляющим или принимающим, например, для уведомлений.

**Неправильный пример:** Процесс от "Start_1" сразу идет к "End_1" без события сообщения, что делает его неполным для сценариев взаимодействия.

**Правильный пример:** Промежуточное событие "Message_1" отправляет сообщение после "Start_1", а затем процесс завершается через "End_1", демонстрируя интеграцию коммуникации.`,
        incorrectExample: {
            title: 'Ошибка: Отсутствие целевого сообщения',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_13" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_13" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_13">
        <bpmndi:BPMNPlane id="BPMNPlane_13" bpmnElement="Process_13">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="200" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Процесс без события сообщения нарушает коммуникацию.'
        },
        correctExample: {
            title: 'Правильно: Событие отправки сообщения',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_14" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_14" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:intermediateThrowEvent id="Message_1" name="Send Message"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing><bpmn:messageEventDefinition /></bpmn:intermediateThrowEvent>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Message_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Message_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_14">
        <bpmndi:BPMNPlane id="BPMNPlane_14" bpmnElement="Process_14">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Message_1_di" bpmnElement="Message_1"><dc:Bounds x="150" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="200" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="118" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="186" y="118" /><di:waypoint x="200" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Событие отправки сообщения корректно внедрено.'
        },
        evaluationCriteria: { requiredElements: ['messageEventDefinition'], minOccurrences: 1 }
    },
    'timerEvent': {
        title: 'Событие таймера',
        description: `**Событие таймера (Timer Event)** управляет процессом на основе времени (начальное, промежуточное, граничное). Используется для автоматизации, например, напоминаний.

**Неправильный пример:** Таймер "Timer_1" определен, но не привязан к элементу. Процесс от "Start_1" идет к "End_1" без использования таймера, что делает его бесполезным.

**Правильный пример:** Таймер "Timer_1" как граничное событие на "Task_1" срабатывает при превышении времени, направляя процесс к "End_1" через "Flow_3". Основной путь через "Flow_2" работает при нормальном завершении задачи.`,
        incorrectExample: {
            title: 'Ошибка: Без привязки к задаче',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_15" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_15" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_15">
        <bpmndi:BPMNPlane id="BPMNPlane_15" bpmnElement="Process_15">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="200" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="200" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Таймер не привязан к элементу процесса.'
        },
        correctExample: {
            title: 'Правильно: Таймер на границе задачи',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_16" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_16" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:task id="Task_1" name="Task"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:task>
        <bpmn:boundaryEvent id="Timer_1" name="Timer" attachedToRef="Task_1"><bpmn:timerEventDefinition /><bpmn:outgoing>Flow_3</bpmn:outgoing></bpmn:boundaryEvent>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming><bpmn:incoming>Flow_3</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1" />
        <bpmn:sequenceFlow id="Flow_3" sourceRef="Timer_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_16">
        <bpmndi:BPMNPlane id="BPMNPlane_16" bpmnElement="Process_16">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Task_1_di" bpmnElement="Task_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Timer_1_di" bpmnElement="Timer_1"><dc:Bounds x="200" y="150" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="300" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_3_di" bpmnElement="Flow_3"><di:waypoint x="218" y="150" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Таймер корректно привязан к задаче.'
        },
        evaluationCriteria: { requiredElements: ['timerEventDefinition'], attachedToTask: true }
    },
    'callActivity': {
        title: 'Вызов активности',
        description: `**Вызов активности (Call Activity)** вызывает другой процесс, указанный в "calledElement", для модульности и переиспользования.

**Неправильный пример:** Вызов активности "Call_1" после "Start_1" ведет к "End_1", но без "calledElement", что делает его неработоспособным.

**Правильный пример:** Вызов активности "Call_1" ссылается на "SubProcess_1", содержащий "SubStart_1" и "SubEnd_1". После "Start_1" процесс вызывает подпроцесс через "Flow_1", а затем завершается через "Flow_2" и "End_1".`,
        incorrectExample: {
            title: 'Ошибка: Без ссылки на процесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_17" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_17" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:callActivity id="Call_1" name="Call"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:callActivity>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Call_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Call_1" targetRef="End_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_17">
        <bpmndi:BPMNPlane id="BPMNPlane_17" bpmnElement="Process_17">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Call_1_di" bpmnElement="Call_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="300" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Вызов активности без указания целевого процесса.'
        },
        correctExample: {
            title: 'Правильно: Ссылка на процесс',
            xml: `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" id="Definitions_18" targetNamespace="http://bpmn.io/schema/bpmn">
    <bpmn:process id="Process_18" isExecutable="false">
        <bpmn:startEvent id="Start_1" name="Start"><bpmn:outgoing>Flow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:callActivity id="Call_1" name="Call" calledElement="SubProcess_1"><bpmn:incoming>Flow_1</bpmn:incoming><bpmn:outgoing>Flow_2</bpmn:outgoing></bpmn:callActivity>
        <bpmn:endEvent id="End_1" name="End"><bpmn:incoming>Flow_2</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Call_1" />
        <bpmn:sequenceFlow id="Flow_2" sourceRef="Call_1" targetRef="End_1" />
    </bpmn:process>
    <bpmn:process id="SubProcess_1" isExecutable="false">
        <bpmn:startEvent id="SubStart_1" name="SubStart"><bpmn:outgoing>SubFlow_1</bpmn:outgoing></bpmn:startEvent>
        <bpmn:endEvent id="SubEnd_1" name="SubEnd"><bpmn:incoming>SubFlow_1</bpmn:incoming></bpmn:endEvent>
        <bpmn:sequenceFlow id="SubFlow_1" sourceRef="SubStart_1" targetRef="SubEnd_1" />
    </bpmn:process>
    <bpmndi:BPMNDiagram id="BPMNDiagram_18">
        <bpmndi:BPMNPlane id="BPMNPlane_18" bpmnElement="Process_18">
            <bpmndi:BPMNShape id="Start_1_di" bpmnElement="Start_1"><dc:Bounds x="100" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="Call_1_di" bpmnElement="Call_1"><dc:Bounds x="150" y="100" width="100" height="50" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="End_1_di" bpmnElement="End_1"><dc:Bounds x="300" y="100" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="Flow_1_di" bpmnElement="Flow_1"><di:waypoint x="136" y="118" /><di:waypoint x="150" y="125" /></bpmndi:BPMNEdge>
            <bpmndi:BPMNEdge id="Flow_2_di" bpmnElement="Flow_2"><di:waypoint x="250" y="125" /><di:waypoint x="300" y="118" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
        <bpmndi:BPMNPlane id="BPMNPlane_Sub_1" bpmnElement="SubProcess_1">
            <bpmndi:BPMNShape id="SubStart_1_di" bpmnElement="SubStart_1"><dc:Bounds x="50" y="50" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNShape id="SubEnd_1_di" bpmnElement="SubEnd_1"><dc:Bounds x="150" y="50" width="36" height="36" /></bpmndi:BPMNShape>
            <bpmndi:BPMNEdge id="SubFlow_1_di" bpmnElement="SubFlow_1"><di:waypoint x="86" y="68" /><di:waypoint x="150" y="68" /></bpmndi:BPMNEdge>
        </bpmndi:BPMNPlane>
    </bpmndi:BPMNDiagram>
</bpmn:definitions>`,
            description: 'Вызов активности с корректной ссылкой на подпроцесс.'
        },
        evaluationCriteria: { requiredElements: ['callActivity'], calledElement: true }
    }
};
import { layoutDiagram } from './bpmnLayout';

describe('layoutDiagram', () => {
    it('сохраняет BPMN DI, присланный сервером', async () => {
        const xml = '<definitions><bpmndi:BPMNShape bpmnElement="T1" />'
            + '<bpmndi:BPMNEdge bpmnElement="F1" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves.toBe(xml);
        expect(relayout).not.toHaveBeenCalled();
    });

    it('сохраняет DI и на второй схеме подряд: проверка не делит lastIndex', async () => {
        /* Шаблон с флагом /g делит lastIndex между вызовами .test: первая
           схема прошла бы, а вторая подряд потеряла бы DI и ушла в
           авто-layout. Поэтому проверка наличия фигуры — без /g. */
        const xml = '<definitions><bpmndi:BPMNShape bpmnElement="T1" />'
            + '<bpmndi:BPMNEdge bpmnElement="F1" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves.toBe(xml);
        await expect(layoutDiagram(xml, relayout)).resolves.toBe(xml);
        expect(relayout).not.toHaveBeenCalled();
    });

    it('раскладывает схему с фигурами, но без рёбер потоков', async () => {
        const xml = '<definitions><bpmndi:BPMNShape bpmnElement="T1" />'
            + '<bpmn:sequenceFlow id="F1" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves
            .toBe('<definitions relaid="true" />');
        expect(relayout).toHaveBeenCalledWith(xml);
    });

    it('раскладывает схему без BPMN DI', async () => {
        const xml = '<definitions><bpmn:userTask id="T1" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves
            .toBe('<definitions relaid="true" />');
        expect(relayout).toHaveBeenCalledWith(xml);
    });

    it('раскладывает схему, где у узла нет фигуры, но есть дуга', async () => {
        /* Пакет улучшения добавляет узел, а `apply_operations` рисует графику
           только для снятия элементов: новый шаг остаётся без `BPMNShape`, при
           том что рёбер его потоков хватает. Прежняя мера («рёбер не меньше,
           чем потоков») звала такой DI полным, редактор его сохранял, и шаг
           выходил в левом верхнем углу поверх имени пула — тот «кривой layout»,
           с которого кампания началась. Мера — фигура на каждый узел, на
           который ссылается дуга.

           Узел без единой ссылки (граничное событие, пристёгнутое к шагу, но
           пока без обработной дуги) этой мерой не виден: распознать его без
           второго словаря типов BPMN нечем, а рисовать графику должен аплайер. */
        const xml = '<definitions>'
            + '<bpmn:userTask id="T1" /><bpmn:userTask id="T2" />'
            + '<bpmn:sequenceFlow id="F1" sourceRef="T1" targetRef="T2" />'
            + '<bpmndi:BPMNShape bpmnElement="T1" />'
            + '<bpmndi:BPMNEdge bpmnElement="F1" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves
            .toBe('<definitions relaid="true" />');
        expect(relayout).toHaveBeenCalledWith(xml);
    });

    it('узнаёт префикс пространства имён с точкой и дефисом', async () => {
        /* Имя префикса — NCName: `bpmn-2:` и `np.v1:` законны. Проверка,
           понимавшая только \w, не посчитала бы такой поток: «рёбер не меньше,
           чем потоков» на пустом списке потоков — и серверный DI неполной схемы
           остался бы как есть, с дугой без точек маршрута. */
        const xml = '<definitions><bpmn-2:userTask id="T1" />'
            + '<bpmn-2:userTask id="T2" />'
            + '<np.v1:sequenceFlow id="F1" sourceRef="T1" targetRef="T2" />'
            + '<bpmndi:BPMNShape bpmnElement="T1" />'
            + '<bpmndi:BPMNShape bpmnElement="T2" /></definitions>';
        const relayout = jest.fn(async () => '<definitions relaid="true" />');

        await expect(layoutDiagram(xml, relayout)).resolves
            .toBe('<definitions relaid="true" />');
        expect(relayout).toHaveBeenCalledWith(xml);
    });
});

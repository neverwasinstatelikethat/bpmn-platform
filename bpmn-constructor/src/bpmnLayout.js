import { layoutProcess } from 'bpmn-auto-layout-feat-ivan-tulaev';

// bpmn-auto-layout не раскладывает часть корректных многупуловых схем:
// messageFlow, источник которого — обычная задача, приводит к исключению.
// Без раскладки bpmn-js показывает пустой холст, поэтому для таких схем
// раскладываем пулы и шаги без сообщений, а сообщения возвращаем в
// размеченный XML прямыми связями.

function collectMessageFlows(xml) {
    return [...xml.matchAll(/<bpmn:messageFlow\b[^>]*\/>|<bpmn:messageFlow\b[^>]*>[\s\S]*?<\/bpmn:messageFlow>/g)]
        .map((match) => match[0]);
}

function shapeCenters(laidOut) {
    const centers = new Map();
    const shapes = laidOut.matchAll(/<bpmndi:BPMNShape\b([^>]*)>([\s\S]*?)<\/bpmndi:BPMNShape>/g);
    for (const shape of shapes) {
        const element = /bpmnElement="([^"]+)"/.exec(shape[1]);
        const bounds = /<dc:Bounds\b[^>]*x="(-?[\d.]+)"[^>]*y="(-?[\d.]+)"[^>]*width="([\d.]+)"[^>]*height="([\d.]+)"/
            .exec(shape[2]);
        if (element && bounds) {
            centers.set(element[1], {
                x: Number(bounds[1]) + Number(bounds[3]) / 2,
                y: Number(bounds[2]) + Number(bounds[4]) / 2,
            });
        }
    }
    return centers;
}

function edgeDi(flow, centers) {
    const id = /id="([^"]+)"/.exec(flow);
    const source = /sourceRef="([^"]+)"/.exec(flow);
    const target = /targetRef="([^"]+)"/.exec(flow);
    if (!id || !source || !target) return '';
    const from = centers.get(source[1]);
    const to = centers.get(target[1]);
    if (!from || !to) return '';
    return `<bpmndi:BPMNEdge bpmnElement="${id[1]}" id="BPMNEdge_${id[1]}">`
        + `<di:waypoint x="${Math.round(from.x)}" y="${Math.round(from.y)}"/>`
        + `<di:waypoint x="${Math.round(to.x)}" y="${Math.round(to.y)}"/>`
        + '</bpmndi:BPMNEdge>';
}

function withoutFlows(xml, flows) {
    let result = xml;
    for (const flow of flows) result = result.replace(flow, '');
    return result;
}

function withFlows(laidOut, flows) {
    if (!flows.length || !laidOut.includes('</bpmn:collaboration>')) return laidOut;
    let result = laidOut.replace('</bpmn:collaboration>',
        `    ${flows.join('\n    ')}\n  </bpmn:collaboration>`);
    const edges = flows.map((flow) => edgeDi(flow, shapeCenters(result))).join('\n      ');
    if (edges && result.includes('</bpmndi:BPMNPlane>')) {
        result = result.replace('</bpmndi:BPMNPlane>', `      ${edges}\n    </bpmndi:BPMNPlane>`);
    }
    return result;
}

// Имя префикса пространства имён — NCName: буква или подчёркивание, дальше
// буквы, цифры, точка и дефис. `\w` не видел `bpmn-2:` и `np.v1:` — потоки с
// таким префиксом не считались бы вообще, и «рёбер не меньше, чем потоков»
// оказалось бы верно на пустом списке потоков.
// Флаг `g` нужен подсчёту и `matchAll`; наличие метки проверяется копией без
// него — `/g`-шаблон делит `lastIndex` между вызовами и на второй схеме дал бы
// неверный ответ. Атрибуты читаются в двойных кавычках: серверный XML всегда
// такой (ElementTree), одиночные кавычки возможны только у ручного импорта.
const SHAPE_PRESENT = /<(?:[A-Za-z_][A-Za-z0-9._-]*:)?BPMNShape\b/i;
const EDGES = /<(?:[A-Za-z_][A-Za-z0-9._-]*:)?BPMNEdge\b/gi;
const FLOWS = /<(?:[A-Za-z_][A-Za-z0-9._-]*:)?(?:sequenceFlow|messageFlow)\b/gi;
const SHAPE_REFS = /<(?:[A-Za-z_][A-Za-z0-9._-]*:)?BPMNShape\b[^>]*\bbpmnElement="([^"]+)"/gi;
const EDGE_REFS = /<(?:[A-Za-z_][A-Za-z0-9._-]*:)?BPMNEdge\b[^>]*\bbpmnElement="([^"]+)"/gi;
const ENDPOINT_REFS = /\b(?:sourceRef|targetRef)="([^"]+)"/g;

export function hasDiagramInterchange(xml) {
    if (!SHAPE_PRESENT.test(xml)) return false;
    const flows = (xml.match(FLOWS) || []).length;
    const edges = (xml.match(EDGES) || []).length;
    if (edges < flows) return false;
    /* Фигура обязана быть у каждого узла, на который ссылается дуга. Одной
       пары «рёбер/потоков» не хватает: аплайер пакетов улучшения графику новым
       узлам не рисует, а `BPMNEdge` его новых потоков появляется ровно там, где
       его не было, — отсчёт рёбер идёт по потокам, которых тоже стало больше.
       Узел без `BPMNShape` редактор рисует в левом верхнем углу поверх имени
       пула: это и есть тот «кривой layout», из-за которого схема выглядела
       сломанной при полностью корректном XML.

       Список узлов берётся из ссылок, а не из типов: словарь видов BPMN уже
       живёт в аплайере (`core/bpmn_edits.py`), и второе место его хранения
       разъехалось бы с первым. Узел, на который не ссылается ни одна дуга
       (только что пристёгнутый таймер без обработной дуги), этой мерой не
       виден: дорисовать ему рамку должен аплайер, а не редактор вслепую. */
    const drawn = new Set([...xml.matchAll(EDGE_REFS)].map((match) => match[1]));
    const shapes = new Set([...xml.matchAll(SHAPE_REFS)].map((match) => match[1]));
    for (const match of xml.matchAll(ENDPOINT_REFS)) {
        if (!drawn.has(match[1]) && !shapes.has(match[1])) return false;
    }
    return true;
}

export async function layoutDiagram(xml, relayout = layoutProcess) {
    if (hasDiagramInterchange(xml)) return xml;
    try {
        return await relayout(xml);
    } catch (error) {
        const flows = collectMessageFlows(xml);
        if (!flows.length) throw error;
        return withFlows(await relayout(withoutFlows(xml, flows)), flows);
    }
}

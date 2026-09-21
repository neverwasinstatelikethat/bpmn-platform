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

export async function layoutDiagram(xml) {
    try {
        return await layoutProcess(xml);
    } catch (error) {
        const flows = collectMessageFlows(xml);
        if (!flows.length) throw error;
        return withFlows(await layoutProcess(withoutFlows(xml, flows)), flows);
    }
}

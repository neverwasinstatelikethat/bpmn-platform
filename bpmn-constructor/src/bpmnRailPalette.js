// RailPalette.js — палитра инструментов bpmn-js внутри левого рельса.
// bpmn-js 18 пишет палитру в контейнер канваса и игнорирует опцию
// `palette: { container }`. Родителя ищем от document: на первом `_init`
// контейнер канваса ещё не пристёгнут к дереву, и `closest()` не сработает.
// Размечая узел вне канваса, сами и убираем его — destroy канваса до него не дойдёт.
import Palette from 'diagram-js/lib/features/palette/Palette';

export const RAIL_SELECTOR = '.editor-palette-panel';

export default class RailPalette extends Palette {
    constructor(eventBus, canvas) {
        super(eventBus, canvas);

        eventBus.on('diagram.destroy', () => {
            if (this._container && this._container.parentNode) {
                this._container.parentNode.removeChild(this._container);
            }
            this._container = null;
        });
    }

    _getParentContainer() {
        return document.querySelector(RAIL_SELECTOR) || super._getParentContainer();
    }
}

import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import BpmnModeler from 'bpmn-js/lib/Modeler';
import { layoutDiagram } from './bpmnLayout';
import 'bpmn-js/dist/assets/diagram-js.css';
import 'bpmn-js/dist/assets/bpmn-font/css/bpmn.css';
import { apiClient, toUserMessage } from './api/client';
import { humanizeBpmnNote } from './i18n/labels';
import { v4 as uuid } from 'uuid';
import html2canvas from 'html2canvas';
import { jsPDF } from 'jspdf';
import GenerateChat from './GenerateChat';
import ImproveChat from './ImproveChat';
import ScorePanel from './ScorePanel';
import { describeRulesDelta } from './scoreRules';
import { getDi } from 'bpmn-js/lib/util/ModelUtil';
import { motion, AnimatePresence } from 'framer-motion';
import { Button, Input } from './components/ui';
import { PageLoader } from './components/layout';
import './Editor.css';
// Импортируем grid модуль
import gridModule from 'diagram-js-grid';
import RailPalette from './bpmnRailPalette';

// diagram-js объявляет `palette: ['type', Palette]` + `__init__: ['palette']`;
// подменяем тип, чтобы инструменты попали в рельс, а не в контейнер канваса.
const railPaletteModule = { palette: ['type', RailPalette] };

const cssVar = (name) => (typeof window === 'undefined'
    ? ''
    : getComputedStyle(document.documentElement).getPropertyValue(name).trim());

/* Палитра заливок элементов схемы — фирменные цвета из токенов (styles/tokens.css). */
const FILL_COLORS = [
    { token: '--color-ink-deep', label: 'Графит' },
    { token: '--color-primary', label: 'Зелёный ВкусВилл' },
    { token: '--color-berry', label: 'Ягода' },
    { token: '--color-info', label: 'Небо' },
    { token: '--color-warn', label: 'Янтарь' },
    { token: '--color-violet', label: 'Фиалка' },
];

/* Подсветка найденных элементов берёт цвет из дизайн-токена. */
const HIGHLIGHT_TOKEN = '--color-primary-soft';

/* ---------- Язык интерфейса вместо языка реализации ---------- */

/* Имя схемы по умолчанию: `Process_<uuid>` аналитику ни о чём не говорит. */
const DEFAULT_DIAGRAM_NAME = 'Новая схема';

/* Внутренний id BPMN-элемента: тип + хвост из base36/uuid (`Task_0x8y9z1`).
   Хвост бывает и из двух символов (`Task_3a`) — его тоже ловим. */
const BPMN_INTERNAL_ID = /^[A-Z][a-zA-Z ]*_[0-9a-zA-Z]{2,}/;

/** Заголовок схемы: пустое значение и авто-id меняем на человеческое имя. */
const displayDiagramName = (raw, fallback = DEFAULT_DIAGRAM_NAME) => {
    const value = String(raw ?? '').trim();
    if (!value || BPMN_INTERNAL_ID.test(value)) return fallback;
    return value;
};

/** true, если в строке не осталось служебных id — только такую показываем. */
const hasNoInternalIds = (text) => !/[A-Z][a-zA-Z ]*_[0-9a-zA-Z]{2,}/.test(text);

/**
 * Заметки детерминированной починки (core/bpmn_generator.py) для чата.
 * humanizeBpmnNote переводит известные формулировки; строки, где после
 * перевода остались служебные id, в чат не выпускаем — сводим их в число.
 */
const describeGeneratorFixes = (notes) => {
    const list = Array.isArray(notes) ? notes : [];
    if (!list.length) return 'Схема готова.';
    const clean = [];
    for (const note of list) {
        const text = humanizeBpmnNote(note);
        if (text && hasNoInternalIds(text) && !clean.includes(text)) clean.push(text);
    }
    if (!clean.length) {
        return `Схема готова. Автоматика поправила структуру в ${list.length} местах — `
            + 'подробности увидите при проверке схемы.';
    }
    const shown = clean.slice(0, 3);
    const hidden = list.length - shown.length;
    return `Схема готова. Автоматика поправила структуру: ${shown.join('; ')}`
        + (hidden > 0 ? `; и ещё правок: ${hidden}.` : '.');
};

/**
 * Закрытие всплывающего слоя по Escape и клику вне, фокус возвращается на
 * триггер. Локальная реализация для двух слоёв редактора (меню экспорта и
 * палитра заливок): общего Popover из `components/ui` ещё нет — он в плане,
 * фаза 3, и тогда этот хук переедет туда.
 */
const useFloatingLayer = ({ open, onClose, triggerRef, layerRef }) => {
    useEffect(() => {
        if (!open) return undefined;
        const onKeyDown = (event) => {
            if (event.key !== 'Escape') return;
            onClose();
            triggerRef.current?.focus();
        };
        const onMouseDown = (event) => {
            const target = event.target;
            if (layerRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
            onClose();
        };
        // Tab уносит фокус из слоя — висящее открытое меню оставляем только
        // пока фокус внутри слоя или на триггере.
        const onFocusOut = (event) => {
            const next = event.relatedTarget;
            if (!next) return;
            if (layerRef.current?.contains(next) || triggerRef.current?.contains(next)) return;
            onClose();
        };
        document.addEventListener('keydown', onKeyDown);
        document.addEventListener('mousedown', onMouseDown);
        document.addEventListener('focusout', onFocusOut);
        return () => {
            document.removeEventListener('keydown', onKeyDown);
            document.removeEventListener('mousedown', onMouseDown);
            document.removeEventListener('focusout', onFocusOut);
        };
    }, [open, onClose, triggerRef, layerRef]);
};

class BPMNExporter {
    constructor(bpmnViewer) {
        this.viewer = bpmnViewer;
        this.canvas = bpmnViewer.get('canvas');
        this.elementRegistry = bpmnViewer.get('elementRegistry');
    }

    async exportToPNG(filename = 'bpmn-diagram', scale = 2, backgroundColor = '#ffffff') {
        console.log('Начало экспорта в PNG');
        try {
            // Сохраняем текущее состояние

            // Устанавливаем viewbox для всего содержимого
            const viewbox = this.canvas.viewbox();
            const width = Math.max(800, viewbox.outer.width * viewbox.scale);
            const height = Math.max(600, viewbox.outer.height * viewbox.scale);

            // Создаем временный контейнер для SVG
            const container = document.createElement('div');
            container.style.position = 'absolute';
            container.style.left = '-9999px';
            container.style.width = width + 'px';
            container.style.height = height + 'px';
            container.style.backgroundColor = backgroundColor;
            container.style.overflow = 'hidden';
            document.body.appendChild(container);

            console.log('Временный контейнер создан');

            // Получаем SVG
            const svgElement = await this.getSVGElement(width, height);

            // Добавляем SVG в контейнер
            container.appendChild(svgElement);

            // Ждем немного дольше, чтобы SVG полностью отрисовался
            await new Promise(resolve => setTimeout(resolve, 500));

            // Используем html2canvas для создания изображения
            console.log('Создание изображения с помощью html2canvas');
            const canvas = await html2canvas(container, {
                scale: scale,
                backgroundColor: backgroundColor,
                logging: true,
                useCORS: true,
                allowTaint: true,
                foreignObjectRendering: true,
                width: width,
                height: height
            });
            console.log('Изображение создано, размер:', canvas.width, 'x', canvas.height);

            // Удаляем временный контейнер
            document.body.removeChild(container);
            console.log('Временный контейнер удален');

            // Создаем blob и скачиваем
            return new Promise((resolve) => {
                canvas.toBlob((blob) => {
                    if (blob) {
                        console.log('PNG blob создан, размер:', blob.size);
                        this.downloadFile(blob, `${filename}.png`);
                        resolve(blob);
                    } else {
                        console.error('Ошибка создания PNG blob');
                        throw new Error('Ошибка создания PNG blob');
                    }
                }, 'image/png');
            });
        } catch (error) {
            console.error('Ошибка при экспорте в PNG:', error);
            throw new Error(`Ошибка экспорта в PNG: ${error.message}`);
        }
    }

    async exportToPDF(filename = 'bpmn-diagram', format = 'a4', orientation = 'landscape') {
        console.log('Начало экспорта в PDF');
        try {
            // Сначала экспортируем в PNG
            console.log('Экспорт в PNG для PDF');
            const pngBlob = await this.exportToPNG(filename, 2, '#ffffff');

            // Создаем изображение из blob
            console.log('Создание изображения из blob');
            const img = new Image();
            const imgPromise = new Promise((resolve, reject) => {
                img.onload = () => {
                    console.log('Изображение загружено, размер:', img.width, 'x', img.height);
                    resolve();
                };
                img.onerror = (e) => {
                    console.error('Ошибка загрузки изображения:', e);
                    reject(new Error('Ошибка загрузки изображения'));
                };
            });
            img.src = URL.createObjectURL(pngBlob);
            await imgPromise;

            // Создаем PDF
            console.log('Создание PDF');
            const pdf = new jsPDF({
                orientation: orientation,
                unit: 'mm',
                format: format
            });

            // Получаем размеры страницы
            const pageWidth = pdf.internal.pageSize.getWidth();
            const pageHeight = pdf.internal.pageSize.getHeight();
            console.log('Размеры страницы PDF:', pageWidth, 'x', pageHeight);

            // Вычисляем размеры изображения с сохранением пропорций
            const imgWidth = pageWidth - 20;
            const imgHeight = (img.height * imgWidth) / img.width;
            console.log('Размеры изображения на странице:', imgWidth, 'x', imgHeight);

            // Если изображение выше страницы, масштабируем его
            let finalHeight = imgHeight;
            let finalWidth = imgWidth;
            if (imgHeight > pageHeight - 20) {
                finalHeight = pageHeight - 20;
                finalWidth = (img.width * finalHeight) / img.height;
                console.log('Изображение масштабировано:', finalWidth, 'x', finalHeight);
            }

            // Центрируем изображение на странице
            const xOffset = (pageWidth - finalWidth) / 2;
            const yOffset = (pageHeight - finalHeight) / 2;
            console.log('Позиция изображения на странице:', xOffset, 'x', yOffset);

            // Добавляем изображение в PDF
            pdf.addImage(img, 'PNG', xOffset, yOffset, finalWidth, finalHeight);
            console.log('Изображение добавлено в PDF');

            // Сохраняем PDF
            const pdfBlob = pdf.output('blob');
            console.log('PDF blob создан, размер:', pdfBlob.size);
            this.downloadFile(pdfBlob, `${filename}.pdf`);
            return pdfBlob;
        } catch (error) {
            console.error('Ошибка при экспорте в PDF:', error);
            throw new Error(`Ошибка экспорта в PDF: ${error.message}`);
        }
    }

    async getSVGElement(width, height) {
        console.log('Получение SVG элемента');
        return new Promise((resolve, reject) => {
            try {
                // Сохраняем текущее состояние
                const originalViewbox = this.canvas.viewbox();

                // Устанавливаем viewbox для всего содержимого
                this.canvas.viewbox({
                    x: 0,
                    y: 0,
                    width: width / originalViewbox.scale,
                    height: height / originalViewbox.scale
                });

                // Сохраняем SVG
                this.viewer.saveSVG((err, svg) => {
                    // Восстанавливаем исходный viewbox
                    this.canvas.viewbox(originalViewbox);

                    if (err) {
                        console.error('Ошибка сохранения SVG:', err);
                        reject(new Error(`Ошибка сохранения SVG: ${err.message}`));
                    } else {
                        console.log('SVG сохранен успешно');
                        const parser = new DOMParser();
                        const svgDoc = parser.parseFromString(svg, 'image/svg+xml');
                        const svgElement = svgDoc.documentElement;
                        const errorNode = svgDoc.querySelector('parsererror');
                        if (errorNode) {
                            console.error('SVG содержит ошибки парсинга');
                            reject(new Error('Некорректный SVG: содержит ошибки парсинга'));
                        }

                        // Устанавливаем явные размеры
                        svgElement.setAttribute('width', width);
                        svgElement.setAttribute('height', height);

                        // Добавляем белый фон, если его нет
                        if (!svgElement.querySelector('rect[fill="#ffffff"]')) {
                            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                            rect.setAttribute('width', '100%');
                            rect.setAttribute('height', '100%');
                            rect.setAttribute('fill', '#ffffff');
                            svgElement.insertBefore(rect, svgElement.firstChild);
                        }

                        // Обрабатываем относительные ссылки
                        const xlinkNs = 'http://www.w3.org/1999/xlink';
                        const hrefs = svgElement.querySelectorAll('[xlink\\:href], [href]');
                        hrefs.forEach(el => {
                            const href = el.getAttributeNS(xlinkNs, 'href') || el.getAttribute('href');
                            if (href && !href.startsWith('http') && !href.startsWith('#')) {
                                const absoluteUrl = new URL(href, window.location.href).toString();
                                el.setAttributeNS(xlinkNs, 'href', absoluteUrl);
                            }
                        });

                        console.log('SVG элемент готов, размеры:', width, 'x', height);
                        resolve(svgElement);
                    }
                });
            } catch (error) {
                console.error('Ошибка при получении SVG:', error);
                reject(new Error(`Ошибка при получении SVG: ${error.message}`));
            }
        });
    }

    downloadFile(blob, filename) {
        console.log('Скачивание файла:', filename);
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        console.log('Файл скачан успешно');
    }
}

const emptyBpmn = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
  id="Definitions_${uuid()}"
  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_${uuid()}" name="Новая схема" isExecutable="false">
    <bpmn:startEvent id="StartEvent_${uuid()}" name="Начало">
      <bpmn:outgoing>Flow_${uuid()}</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_${uuid()}" name="Шаг">
      <bpmn:incoming>Flow_${uuid()}</bpmn:incoming>
    </bpmn:task>
    <bpmn:sequenceFlow id="Flow_${uuid()}" sourceRef="StartEvent_${uuid()}" targetRef="Task_${uuid()}" />
  </bpmn:process>
  <bpmndi:BPMNDiagram id="BPMNDiagram_${uuid()}">
    <bpmndi:BPMNPlane id="BPMNPlane_${uuid()}" bpmnElement="Process_${uuid()}">
      <bpmndi:BPMNShape id="StartEvent_${uuid()}_di" bpmnElement="StartEvent_${uuid()}">
        <dc:Bounds x="150" y="100" width="36" height="36" />
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="Task_${uuid()}_di" bpmnElement="Task_${uuid()}">
        <dc:Bounds x="250" y="80" width="100" height="80" />
      </bpmndi:BPMNShape>
      <bpmndi:BPMNEdge id="Flow_${uuid()}_di" bpmnElement="Flow_${uuid()}">
        <di:waypoint x="186" y="118" />
        <di:waypoint x="250" y="118" />
      </bpmndi:BPMNEdge>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>`;

const Editor = () => {
    const modelerRef = useRef(null);
    const containerRef = useRef(null);
    const paletteRef = useRef(null);
    const [diagramName, setDiagramName] = useState(DEFAULT_DIAGRAM_NAME);
    const [diagramId, setDiagramId] = useState(null);
    const [validationResult, setValidationResult] = useState(null);
    // id элемента -> его название: отчёт оценки показывает имена, а не Task_0x8y9z1.
    const [elementNames, setElementNames] = useState({});
    const [score, setScore] = useState(0);
    const [showScorePanel, setShowScorePanel] = useState(false);
    // Отдельное состояние «нечего проверять»: это не ошибка запроса и не
    // «оценки ещё нет» — схема до сих пор стартовый шаблон.
    const [scoreEmpty, setScoreEmpty] = useState(false);
    const [scorable, setScorable] = useState(false);
    const [scoreBusy, setScoreBusy] = useState(false);
    const [notification, setNotification] = useState(null);
    const [canvasBusy, setCanvasBusy] = useState(false);
    const [searchQuery, setSearchQuery] = useState('');
    const [chatType, setChatType] = useState(null);
    const [showColorPicker, setShowColorPicker] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(false);
    const location = useLocation();
    const navigate = useNavigate();
    const notificationTimeoutRef = useRef(null);
    // Все отложенные центрирования канваса: при unmount снимаем целиком, иначе
    // колбэк доезжает до уже уничтоженного modeler.
    const pendingTimersRef = useRef(new Set());
    const elementsOriginalColors = useRef(new Map());
    const [chatExpanded, setChatExpanded] = useState(false);
    const [showDownloadOptions, setShowDownloadOptions] = useState(false);
    const [scoreExpanded, setScoreExpanded] = useState(false);
    // Слои поверх канваса: меню экспорта и палитра заливок. Нужны для закрытия
    // по Escape и клику вне (refs на триггер и на сам слой).
    const downloadTriggerRef = useRef(null);
    const downloadMenuRef = useRef(null);
    const colorTriggerRef = useRef(null);
    const colorPopoverRef = useRef(null);
    const [messagesGenerate, setMessagesGenerate] = useState([]);
    const [messagesImprove, setMessagesImprove] = useState([]);
    const [messagesScore, setMessagesScore] = useState([]);
    // Последнее сообщение скоринга показывается в панели инструментов,
    // чтобы ошибка /api/evaluate не оставалась невидимой.
    const scoreMessage = messagesScore.length ? messagesScore[messagesScore.length - 1] : null;
    const scoreError = scoreMessage?.isError ? scoreMessage.text : null;
    // Цвета заливки резолвим из токенов: в компоненте нет ни одного хардкод-цвета.
    const fillSwatches = useMemo(
        () => FILL_COLORS.map(({ token, label }) => ({ color: cssVar(token), label }))
            .filter((swatch) => swatch.color),
        []
    );
    // Оптимизация поиска
    const searchResults = useRef(new Map());
    const lastSearchQuery = useRef('');
    const searchTimeoutRef = useRef(null);

    // Вписываем схему в видимую область канваса, не приближая больше 1:1.
    const autoFitDiagram = useCallback(() => {
        const container = containerRef.current;
        if (!modelerRef.current || !container?.clientWidth || !container?.clientHeight) return;
        const canvas = modelerRef.current.get('canvas');
        if (!canvas.getRootElement()) return;
        canvas.zoom('fit-viewport');
        if (canvas.zoom() > 1) canvas.zoom(1);
    }, []);

    // Отложенное центрирование: id таймера живёт в ref, чтобы при unmount
    // (уход со страницы, смена схемы) ни один из них не доехал до modeler.
    const scheduleCanvasFit = useCallback(() => {
        const id = window.setTimeout(() => {
            pendingTimersRef.current.delete(id);
            autoFitDiagram();
        }, 800);
        pendingTimersRef.current.add(id);
    }, [autoFitDiagram]);

    useEffect(() => {
        const timers = pendingTimersRef.current;
        return () => {
            timers.forEach((id) => window.clearTimeout(id));
            timers.clear();
            if (notificationTimeoutRef.current) {
                window.clearTimeout(notificationTimeoutRef.current);
                notificationTimeoutRef.current = null;
            }
        };
    }, []);

    // Оптимизированная функция поиска с debounce
    const handleSearch = useCallback(() => {
        if (!modelerRef.current) return;
        const elementRegistry = modelerRef.current.get('elementRegistry');
        const canvas = modelerRef.current.get('canvas');
        const modeling = modelerRef.current.get('modeling');
        // Если запрос пустой, сбрасываем все подсветки
        if (!searchQuery.trim()) {
            searchResults.current.forEach((elementId) => {
                const element = elementRegistry.get(elementId);
                if (element) {
                    canvas.removeMarker(element.id, 'highlight');
                    const di = getDi(element);
                    if (di) {
                        const originalColor = elementsOriginalColors.current.get(element.id) || '#ffffff';
                        modeling.updateProperties(element, { 'di': { ...di, fill: originalColor } });
                    }
                }
            });
            searchResults.current.clear();
            lastSearchQuery.current = '';
            return;
        }
        // Если тот же запрос, не выполняем поиск повторно
        if (lastSearchQuery.current === searchQuery) return;
        const query = searchQuery.toLowerCase();
        lastSearchQuery.current = searchQuery;
        // Цвет подсветки берём из токена дизайн-системы (правила .highlight — в Editor.css)
        const highlightFill = cssVar(HIGHLIGHT_TOKEN);
        // Создаем Set для хранения новых результатов
        const newResults = new Set();
        // Проходим по всем элементам
        elementRegistry.forEach((element) => {
            const name = element.businessObject.name || '';
            if (name.toLowerCase().includes(query)) {
                newResults.add(element.id);
                // Если элемент не был подсвечен ранее
                if (!searchResults.current.has(element.id)) {
                    const di = getDi(element);
                    if (di) {
                        canvas.addMarker(element.id, 'highlight');
                        if (highlightFill) {
                            modeling.updateProperties(element, { 'di': { ...di, fill: highlightFill } });
                        }
                    }
                }
            } else if (searchResults.current.has(element.id)) {
                // Если элемент был подсвечен, но больше не соответствует запросу
                const di = getDi(element);
                if (di) {
                    canvas.removeMarker(element.id, 'highlight');
                    const originalColor = elementsOriginalColors.current.get(element.id) || '#ffffff';
                    modeling.updateProperties(element, { 'di': { ...di, fill: originalColor } });
                }
            }
        });
        // Обновляем результаты поиска
        searchResults.current = newResults;
    }, [searchQuery]);

    // Применяем debounce для поиска
    useEffect(() => {
        if (searchTimeoutRef.current) {
            clearTimeout(searchTimeoutRef.current);
        }
        searchTimeoutRef.current = setTimeout(() => {
            handleSearch();
        }, 300); // Задержка 300мс
        return () => {
            if (searchTimeoutRef.current) {
                clearTimeout(searchTimeoutRef.current);
            }
        };
    }, [searchQuery, handleSearch]);

    const showNotification = useCallback((message) => {
        if (notificationTimeoutRef.current) {
            clearTimeout(notificationTimeoutRef.current);
        }
        setNotification(message);
        notificationTimeoutRef.current = setTimeout(() => {
            setNotification(null);
            notificationTimeoutRef.current = null;
        }, 3000);
    }, []);

    // Диалог подтверждения удаления закрывается по Escape.
    useEffect(() => {
        if (!pendingDelete) return undefined;
        const onKeyDown = (event) => {
            if (event.key === 'Escape') setPendingDelete(false);
        };
        document.addEventListener('keydown', onKeyDown);
        return () => document.removeEventListener('keydown', onKeyDown);
    }, [pendingDelete]);

    // Escape закрывает плавающие панели: сначала чат, затем скоринг.
    // Всплывающий слой (меню экспорта, палитра заливок) имеет приоритет —
    // его закрывает собственный обработчик, иначе Escape убирал бы и слой,
    // и панель одновременно.
    useEffect(() => {
        if (!chatType && !showScorePanel) return undefined;
        const onKeyDown = (event) => {
            if (event.key !== 'Escape') return;
            if (showDownloadOptions || showColorPicker || pendingDelete) return;
            if (chatType) setChatType(null);
            else setShowScorePanel(false);
        };
        document.addEventListener('keydown', onKeyDown);
        return () => document.removeEventListener('keydown', onKeyDown);
    }, [chatType, showScorePanel, showDownloadOptions, showColorPicker, pendingDelete]);

    /**
     * Схема, которую проверять нечем: стартовый шаблон — один «Шаг» без
     * завершения, без шлюзов и без участников. Балл на таком наборе —
     * шум из 17 нарушений, а не разбор.
     */
    const isStarterScheme = useCallback(() => {
        const modeler = modelerRef.current;
        if (!modeler) return true;
        const registry = modeler.get('elementRegistry');
        const FLOW_NODE = /(?:Task|Gateway|SubProcess|CallActivity|EndEvent|IntermediateCatchEvent|IntermediateThrowEvent|BoundaryEvent)$/;
        let flowNodes = 0;
        let containers = 0;
        registry.forEach((element) => {
            const type = element.type || '';
            if (FLOW_NODE.test(type)) flowNodes += 1;
            if (type === 'bpmn:Participant' || type === 'bpmn:Lane') containers += 1;
        });
        return flowNodes <= 1 && containers === 0;
    }, []);

    useEffect(() => {
        console.log('Инициализация BPMN редактора');
        // Создание модели с расширенными модулями
        const modeler = new BpmnModeler({
            container: containerRef.current,
            additionalModules: [
                gridModule, // Модуль для сетки
                railPaletteModule // Палитра — в левом рельсе
            ],
            grid: {
                visible: true,
                snap: true,
                size: 30
            }
        });
        modelerRef.current = modeler;
        // Кнопка «Проверить» живёт вместе со схемой: на стартовом шаблоне
        // она неактивна, а не обещает балл из воздуха.
        const syncScorable = () => setScorable(!isStarterScheme());
        const eventBus = modeler.get('eventBus');
        eventBus.on('commandStack.changed', syncScorable);
        eventBus.on('import.done', syncScorable);
        // В StrictMode эффект отрабатывает дважды: загрузка устаревшего экземпляра
        // должна остановиться, иначе два importXML спорят за один канвас.
        let cancelled = false;

        const loadDiagram = async () => {
            console.log('Загрузка диаграммы');
            setCanvasBusy(true);
            try {
                let initialXML = emptyBpmn;
                const urlParams = new URLSearchParams(location.search);
                let loadedDiagramId = urlParams.get('load');
                if (loadedDiagramId) {
                    console.log('Загрузка диаграммы по ID:', loadedDiagramId);
                    const response = await apiClient.get(`/api/diagrams/${loadedDiagramId}`);
                    if (cancelled) return;
                    initialXML = response.data.xml_content;
                    // В колонке name лежит и служебный id процесса — в заголовке
                    // его быть не должно.
                    setDiagramName(displayDiagramName(response.data.name));
                    setDiagramId(loadedDiagramId);
                } else if (location.state?.id) {
                    console.log('Загрузка диаграммы из состояния:', location.state.id);
                    const response = await apiClient.get(`/api/diagrams/${location.state.id}`);
                    if (cancelled) return;
                    initialXML = response.data.xml_content;
                    setDiagramName(displayDiagramName(response.data.name));
                    setDiagramId(location.state.id);
                } else if (location.state?.bpmnXML) {
                    console.log('Загрузка диаграммы из XML');
                    initialXML = location.state.bpmnXML;
                    setDiagramName(displayDiagramName(location.state.name, 'Загруженная схема'));
                } else {
                    console.log('Создание новой диаграммы');
                    setDiagramName(DEFAULT_DIAGRAM_NAME);
                }
                const parser = new DOMParser();
                const xmlDoc = parser.parseFromString(initialXML, 'text/xml');
                const errorNode = xmlDoc.querySelector('parsererror');
                if (errorNode) throw new Error('Некорректный XML');
                const participants = xmlDoc.getElementsByTagNameNS('http://www.omg.org/spec/BPMN/20100524/MODEL', 'participant');
                let layoutedXML = initialXML;
                if (participants.length > 0) {
                    console.log('Применение автоматической раскладки');
                    try {
                        layoutedXML = await layoutDiagram(initialXML);
                    } catch (layoutErr) {
                        console.warn('Ошибка раскладки, загружаем без координат:', layoutErr);
                        layoutedXML = initialXML;
                    }
                    if (cancelled) return;
                }
                console.log('Импорт XML в модельер');
                await modeler.importXML(layoutedXML);
                const elementRegistry = modeler.get('elementRegistry');
                elementRegistry.forEach((element) => {
                    const di = getDi(element);
                    if (di && di.fill) elementsOriginalColors.current.set(element.id, di.fill);
                });
                // Увеличиваем задержку для правильного центрирования
                console.log('Планирование центрирования после загрузки');
                scheduleCanvasFit();
            } catch (err) {
                if (cancelled) return;
                console.error('Ошибка загрузки диаграммы:', err);
                modeler.clear();
                await modeler.importXML(emptyBpmn);
                scheduleCanvasFit();
                showNotification('Схему не удалось открыть — показали пустой шаблон. Попробуйте открыть её ещё раз.');
            } finally {
                if (!cancelled) setCanvasBusy(false);
            }
        };

        loadDiagram();

        return () => {
            cancelled = true;
            modeler.destroy();
            if (modelerRef.current === modeler) modelerRef.current = null;
        };
    }, [location.search, location.state, showNotification, scheduleCanvasFit, isStarterScheme]);

    const handleGenerate = async (prompt) => {
        console.log('Генерация диаграммы с промптом:', prompt);
        setMessagesGenerate(prev => [...prev,
        { sender: 'user', text: prompt, id: Date.now() },
        { sender: 'AI', isLoading: true, id: Date.now() + 1 }
        ]);
        try {
            const response = await apiClient.post('/api/generate', {
                text: prompt || 'Создайте стандартную BPMN диаграмму',
                detail_level: 'Medium',
            });
            if (response.data.status === 'success') {
                const newBpmnXML = response.data.bpmn;
                setCanvasBusy(true);
                try {
                    if (modelerRef.current) modelerRef.current.clear();
                    const layoutedXML = await layoutDiagram(newBpmnXML);
                    await modelerRef.current.importXML(layoutedXML);
                    setDiagramName(`Сгенерировано: ${displayDiagramName(prompt?.slice(0, 20))}`);
                    // Модель часто возвращает структуру, которую сервер чинит
                    // детерминированно (перенос шага в другой пул, удалённый
                    // поток, добавленное событие). Пользователь обязан видеть,
                    // что схему поправили за него, — но служебные id и английские
                    // типы из заметок генератора в чат не идут.
                    const notes = response.data.notes || [];
                    if (notes.length) console.log('Заметки автоматики:', notes);
                    setMessagesGenerate(prev => [
                        ...prev.slice(0, -1),
                        {
                            sender: 'AI',
                            text: describeGeneratorFixes(notes),
                            id: Date.now()
                        }
                    ]);
                    scheduleCanvasFit();
                } finally {
                    setCanvasBusy(false);
                }
            }
        } catch (err) {
            console.error('Ошибка генерации:', err);
            setMessagesGenerate(prev => prev.slice(0, -1));
            throw new Error(toUserMessage(err, 'Не удалось сгенерировать схему.'));
        }
    };

    // Отчёт оценки ссылается на элементы их служебными id (core/bpmn_scoring.py).
    // Снимаем с текущей модели словарь id -> название, чтобы панель говорила
    // «Согласовать заявку», а не «Task_0x8y9z1».
    const collectElementNames = useCallback(() => {
        const modeler = modelerRef.current;
        if (!modeler) return {};
        const names = {};
        modeler.get('elementRegistry').forEach((element) => {
            const name = element.businessObject?.name;
            if (name && element.id) names[element.id] = name;
        });
        return names;
    }, []);

    // Подсветка элементов, на которые указывает правило отчёта: без неё
    // «нет завершающего события» остаётся строкой текста, а не местом на схеме.
    const reportedIdsRef = useRef(new Set());

    const revealReported = useCallback((ids) => {
        const modeler = modelerRef.current;
        if (!modeler) return;
        const canvas = modeler.get('canvas');
        const registry = modeler.get('elementRegistry');
        const next = new Set(Array.isArray(ids) ? ids : []);
        reportedIdsRef.current.forEach((id) => {
            if (!next.has(id) && registry.get(id)) canvas.removeMarker(id, 'reported');
        });
        next.forEach((id) => {
            if (registry.get(id)) canvas.addMarker(id, 'reported');
        });
        reportedIdsRef.current = next;
    }, []);

    // Клик по строке отчёта наводит холст на нарушение: аналитику не нужно
    // искать элемент глазами на большой схеме.
    const focusReported = useCallback((ids) => {
        const modeler = modelerRef.current;
        if (!modeler || !ids?.length) return;
        const canvas = modeler.get('canvas');
        const registry = modeler.get('elementRegistry');
        const boxes = ids
            .map((id) => registry.get(id))
            .filter((element) => element && element.width)
            .map((element) => ({
                x1: element.x, y1: element.y,
                x2: element.x + element.width, y2: element.y + element.height,
            }));
        if (!boxes.length) return;
        const left = Math.min(...boxes.map((b) => b.x1));
        const top = Math.min(...boxes.map((b) => b.y1));
        const right = Math.max(...boxes.map((b) => b.x2));
        const bottom = Math.max(...boxes.map((b) => b.y2));
        canvas.scrollTo({ x: (left + right) / 2, y: (top + bottom) / 2 });
        revealReported(ids);
    }, [revealReported]);

    const handleValidate = async () => {
        console.log('Валидация диаграммы');
        if (isStarterScheme()) {
            // Отказ до запроса: показывать 17 нарушений на стартовом
            // шаблоне — значит выдавать шум за разбор.
            setShowScorePanel(true);
            setChatType(null);
            setValidationResult(null);
            setElementNames({});
            setScore(0);
            setMessagesScore([]);
            setScoreEmpty(true);
            return;
        }
        setScoreEmpty(false);
        // Прежняя подсветка отчёта снимается до нового запроса: иначе
        // элементы прошлого разбора остаются обведёнными, хотя разбор
        // уже устарел.
        revealReported([]);
        try {
            const { xml } = await modelerRef.current.saveXML({ format: true });
            const fixedXML = await fixXMLStructure(xml);
            let layoutedXML;
            try {
                layoutedXML = await layoutDiagram(fixedXML);
            } catch (layoutError) {
                console.warn('Layout failed, using original XML:', layoutError);
                layoutedXML = fixedXML;
            }
            const namesById = collectElementNames();
            setChatType(null);
            setShowScorePanel(true);
            setScoreBusy(true);
            let response;
            try {
                response = await apiClient.post('/api/evaluate', {
                    bpmn_xml: layoutedXML,
                });
            } finally {
                setScoreBusy(false);
            }
            setValidationResult(response.data);
            setElementNames(namesById);
            setScore(response.data.score);
            // В шапке — только балл: разбор рекомендаций живёт в панели,
            // там он читается, а не склеивается в одну строку.
            setMessagesScore([{
                sender: 'AI',
                text: `Проверка завершена: ${response.data.score}/100.`,
                id: Date.now()
            }]);
        } catch (err) {
            console.error('Ошибка проверки:', err);
            const message = toUserMessage(err, 'Проверку не удалось выполнить. Попробуйте ещё раз.');
            setMessagesScore([{
                sender: 'AI',
                text: message,
                isError: true,
                id: Date.now()
            }]);
            showNotification(message);
        }
    };

    const handleImprove = async (prompt) => {
        console.log('Улучшение диаграммы с промптом:', prompt);
        setMessagesImprove(prev => [...prev,
        { sender: 'user', text: prompt, id: Date.now() },
        { sender: 'AI', isLoading: true, id: Date.now() + 1 }
        ]);
        try {
            const { xml } = await modelerRef.current.saveXML({ format: true });
            const response = await apiClient.post('/api/ai/improve', {
                bpmn_xml: xml,
                prompt: prompt,
                diagram_id: diagramId || null,
            });
            const recommendations = Array.isArray(response.data.recommendations)
                ? response.data.recommendations.join('\n')
                : (response.data.recommendations || '');
            setMessagesImprove(prev => [
                ...prev.slice(0, -1),
                {
                    sender: 'AI',
                    text: recommendations || 'Изменения подготовлены.',
                    improvementId: response.data.improvement_id,
                    // Без status чат не отличает «предложен XML» от «только анализ»
                    // и показал бы кнопку принятия там, где принимать нечего.
                    status: response.data.status,
                    id: Date.now()
                }
            ]);
        } catch (err) {
            console.error('Ошибка улучшения:', err);
            setMessagesImprove(prev => prev.slice(0, -1));
            throw new Error(toUserMessage(err, 'Не удалось улучшить схему.'));
        }
    };

    const fixXMLStructure = async (xmlString) => {
        try {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(xmlString, 'text/xml');
            const errorNode = xmlDoc.querySelector('parsererror');
            if (errorNode) {
                console.error('XML parsing error:', errorNode.textContent);
                throw new Error('Invalid XML structure');
            }
            const bpmnNamespace = 'http://www.omg.org/spec/BPMN/20100524/MODEL';
            const processes = xmlDoc.getElementsByTagNameNS(bpmnNamespace, 'process');
            if (processes.length === 0) {
                console.warn('No BPMN processes found in XML');
                return xmlString;
            }
            const allElementsMap = new Map();
            const flowElements = new Set();
            for (let i = 0; i < processes.length; i++) {
                const process = processes[i];
                const elements = process.children;
                for (let j = 0; j < elements.length; j++) {
                    const element = elements[j];
                    if (element.id) {
                        allElementsMap.set(element.id, element);
                        if (element.tagName.includes('sequenceFlow')) {
                            flowElements.add(element);
                        }
                    }
                }
            }
            flowElements.forEach(flow => {
                const sourceRef = flow.getAttribute('sourceRef');
                const targetRef = flow.getAttribute('targetRef');
                if (!sourceRef || !targetRef) {
                    console.warn('Flow without source or target ref:', flow.id);
                    flow.parentNode?.removeChild(flow);
                    return;
                }
                const sourceElement = allElementsMap.get(sourceRef);
                const targetElement = allElementsMap.get(targetRef);
                if (!sourceElement || !targetElement) {
                    console.warn('Flow references non-existent elements:', sourceRef, targetRef);
                    flow.parentNode?.removeChild(flow);
                    return;
                }
            });
            for (let i = 0; i < processes.length; i++) {
                const process = processes[i];
                const elements = process.children;
                for (let j = 0; j < elements.length; j++) {
                    const element = elements[j];
                    if (element.id && !element.tagName.includes('sequenceFlow')) {
                        ensureProperConnections(element, process, xmlDoc, bpmnNamespace);
                    }
                }
            }
            for (let i = 0; i < processes.length; i++) {
                const process = processes[i];
                const startEvents = process.getElementsByTagNameNS(bpmnNamespace, 'startEvent');
                if (startEvents.length === 0) {
                    console.warn('Process without start event, adding one');
                    const startEvent = xmlDoc.createElementNS(bpmnNamespace, 'bpmn:startEvent');
                    startEvent.setAttribute('id', `StartEvent_${uuid()}`);
                    startEvent.setAttribute('name', 'Начало');
                    process.appendChild(startEvent);
                }
            }
            const serializer = new XMLSerializer();
            const fixedXML = serializer.serializeToString(xmlDoc);
            const testParser = new DOMParser();
            const testDoc = testParser.parseFromString(fixedXML, 'text/xml');
            const testError = testDoc.querySelector('parsererror');
            if (testError) {
                console.error('Fixed XML is still invalid:', testError.textContent);
                return xmlString;
            }
            return fixedXML;
        } catch (error) {
            console.error('XML fix failed:', error);
            return xmlString;
        }
    };

    const ensureProperConnections = (element, process, xmlDoc, bpmnNamespace) => {
        const elementId = element.id;
        if (!elementId) return;
        try {
            const flows = Array.from(process.getElementsByTagNameNS(bpmnNamespace, 'sequenceFlow'));
            const actualIncoming = [];
            const actualOutgoing = [];
            flows.forEach(flow => {
                const sourceRef = flow.getAttribute('sourceRef');
                const targetRef = flow.getAttribute('targetRef');
                const flowId = flow.id;
                if (targetRef === elementId && flowId && sourceRef) {
                    actualIncoming.push(flowId);
                }
                if (sourceRef === elementId && flowId && targetRef) {
                    actualOutgoing.push(flowId);
                }
            });
            const oldIncoming = element.getElementsByTagNameNS(bpmnNamespace, 'incoming');
            const oldOutgoing = element.getElementsByTagNameNS(bpmnNamespace, 'outgoing');
            Array.from(oldIncoming).forEach(el => element.removeChild(el));
            Array.from(oldOutgoing).forEach(el => element.removeChild(el));
            actualIncoming.forEach(flowId => {
                const incomingEl = xmlDoc.createElementNS(bpmnNamespace, 'bpmn:incoming');
                incomingEl.textContent = flowId;
                element.appendChild(incomingEl);
            });
            actualOutgoing.forEach(flowId => {
                const outgoingEl = xmlDoc.createElementNS(bpmnNamespace, 'bpmn:outgoing');
                outgoingEl.textContent = flowId;
                element.appendChild(outgoingEl);
            });
        } catch (error) {
            console.warn('Failed to fix connections for element:', elementId, error);
        }
    };

    const handleAcceptImprovement = async (improvementId) => {
        console.log('Принятие улучшения:', improvementId);
        try {
            const response = await apiClient.post('/api/ai/accept-improvement', {
                improvement_id: improvementId,
            });
            const fixedXML = await fixXMLStructure(response.data.xml_content);
            let layoutedXML;
            try {
                layoutedXML = await layoutDiagram(fixedXML);
            } catch (layoutError) {
                console.warn('Layout failed for accepted improvement, using fixed XML:', layoutError);
                layoutedXML = fixedXML;
            }
            if (modelerRef.current) {
                setCanvasBusy(true);
                try {
                    modelerRef.current.clear();
                    await modelerRef.current.importXML(layoutedXML);
                    const elementRegistry = modelerRef.current.get('elementRegistry');
                    elementRegistry.forEach((element) => {
                        const di = getDi(element);
                        if (di && di.fill) {
                            elementsOriginalColors.current.set(element.id, di.fill);
                        }
                    });
                    setDiagramId(response.data.diagram_id);
                    // Балл пересчитывает backend по уже принятому XML: без этого
                    // панель остаётся с оценкой прежней схемы.
                    const acceptedScore = response.data.score;
                    const previousScore = response.data.score_before;
                    setScore(acceptedScore);
                    // Дельта по правилам, а не только цифра: «85 → 85» выглядит
                    // как бесполезное принятие, хотя часть правил починена.
                    const { fixed, broken } = describeRulesDelta(response.data.rules_delta);
                    const verdict = [];
                    if (previousScore != null && previousScore !== acceptedScore) {
                        verdict.push(`Оценка схемы: ${previousScore} → ${acceptedScore}.`);
                    }
                    if (fixed.length) verdict.push(`Починено: ${fixed.join('; ')}.`);
                    if (broken.length) verdict.push(`Стало хуже: ${broken.join('; ')}.`);
                    setMessagesImprove(prev => [...prev,
                    { sender: 'AI', text: 'Изменения успешно применены.'
                        + (verdict.length ? ' ' + verdict.join(' ') : ''),
                        id: Date.now()
                    }
                    ]);
                    // Центрируем после принятия улучшения
                    scheduleCanvasFit();
                } finally {
                    setCanvasBusy(false);
                }
            }
            return true;
        } catch (err) {
            console.error('Ошибка принятия изменений:', err);
            setMessagesImprove(prev => [...prev,
            {
                sender: 'AI',
                isError: true,
                text: toUserMessage(err, 'Изменения не применены. Схема осталась прежней — попробуйте ещё раз.'),
                id: Date.now()
            }
            ]);
            // false keeps the decision action available for a retry in the chat.
            return false;
        }
    };

    const handleRejectImprovement = async (improvementId) => {
        try {
            await apiClient.post('/api/ai/reject-improvement', { improvement_id: improvementId });
            return true;
        } catch (err) {
            console.error('Не удалось отклонить улучшение:', err);
            showNotification(toUserMessage(err, 'Улучшение не отклонено.'));
            return false;
        }
    };

    const handleSave = async () => {
        console.log('Сохранение диаграммы');
        if (!diagramName.trim()) {
            // Пустое имя отвергнет бэкенд (422); предупреждаем до запроса.
            showNotification('Назовите схему перед сохранением.');
            return;
        }
        try {
            const { xml } = await modelerRef.current.saveXML({ format: true });
            const urlParams = new URLSearchParams(location.search);
            let newDiagramId = diagramId || urlParams.get('load') || location.state?.id || uuid();
            await apiClient.post(`/api/diagrams`, {
                id: newDiagramId,
                name: diagramName,
                xml: xml,
                score: score,
            });
            setDiagramId(newDiagramId);
            showNotification('Схема сохранена.');
        } catch (err) {
            console.error('Ошибка сохранения:', err);
            showNotification(toUserMessage(err, 'Схема не сохранена. Проверьте соединение и попробуйте ещё раз.'));
        }
    };

    const handleShare = async () => {
        console.log('Создание ссылки для поделиться');
        try {
            if (!diagramId) {
                showNotification('Сначала сохраните схему.');
                return;
            }
            const response = await apiClient.post(`/api/share`, { diagram_id: diagramId });
            const shareLink = response.data.share_link;
            try {
                await navigator.clipboard.writeText(shareLink);
                showNotification('Ссылка скопирована в буфер обмена!');
            } catch {
                // Clipboard API недоступен без жеста/разрешения — показываем ссылку.
                showNotification(`Ссылка для просмотра: ${shareLink}`);
            }
        } catch (err) {
            console.error('Ошибка создания ссылки:', err);
            showNotification(toUserMessage(err, 'Ссылку не удалось получить. Попробуйте ещё раз.'));
        }
    };

    const handleDelete = async () => {
        console.log('Удаление диаграммы');
        try {
            await apiClient.delete(`/api/diagrams/${diagramId}`);
            navigate('/my-schemas');
        } catch (err) {
            console.error('Ошибка удаления:', err);
            showNotification(toUserMessage(err, 'Схема не удалена. Попробуйте ещё раз или удалите её из реестра.'));
        }
    };

    // Удаление запускается только после подтверждения в диалоге и только для
    // сохранённой схемы: несохранённую удалять нечего, кнопка не активна.
    const canDelete = Boolean(diagramId);

    const handleRequestDelete = () => {
        if (!canDelete) {
            showNotification('Несохранённую схему удалять нечего — сохраните её или очистите холст.');
            return;
        }
        setPendingDelete(true);
    };

    const handleCancelDelete = () => setPendingDelete(false);

    const handleConfirmDelete = async () => {
        setPendingDelete(false);
        await handleDelete();
    };

    const handleDownload = async (format) => {
        console.log(`Начало скачивания в формате: ${format}`);
        try {
            const exporter = new BPMNExporter(modelerRef.current);
            if (format === 'bpmn') {
                console.log('Экспорт в BPMN');
                const { xml } = await modelerRef.current.saveXML({ format: true });
                const blob = new Blob([xml], { type: 'application/xml' });
                exporter.downloadFile(blob, `${diagramName}.bpmn`);
                showNotification('Диаграмма успешно скачана в формате BPMN!');
            } else if (format === 'png') {
                console.log('Экспорт в PNG');
                await exporter.exportToPNG(diagramName, 2, '#ffffff');
                showNotification('Диаграмма успешно скачана в формате PNG!');
            } else if (format === 'pdf') {
                console.log('Экспорт в PDF');
                await exporter.exportToPDF(diagramName, 'a4', 'landscape');
                showNotification('Диаграмма успешно скачана в формате PDF!');
            } else {
                throw new Error('Неподдерживаемый формат экспорта');
            }
        } catch (err) {
            console.error(`Ошибка экспорта в ${format.toUpperCase()}:`, err);
            // Экспорт — локальная операция: сетевых ошибок здесь почти не
            // бывает, а внутренние сообщения html2canvas пользователю не читаемы.
            const localMessage = format === 'bpmn'
                ? 'Файл не сохранён. Попробуйте ещё раз.'
                : 'Картинку не удалось собрать. Попробуйте ещё раз или скачайте схему в формате BPMN.';
            showNotification(err.response
                ? toUserMessage(err, 'Файл не сохранён. Попробуйте ещё раз.')
                : localMessage);
        } finally {
            setShowDownloadOptions(false);
        }
    };

    const handleZoomIn = () => {
        const canvas = modelerRef.current.get('canvas');
        const currentZoom = canvas.zoom();
        canvas.zoom(currentZoom + 0.1, true);
    };

    const handleZoomOut = () => {
        const canvas = modelerRef.current.get('canvas');
        const currentZoom = canvas.zoom();
        canvas.zoom(Math.max(0.1, currentZoom - 0.1), true);
    };

    const handleResetZoom = () => {
        const canvas = modelerRef.current.get('canvas');
        canvas.zoom(1, true);
    };

    const handleClearCanvas = async () => {
        console.log('Очистка канваса');
        setCanvasBusy(true);
        try {
            if (modelerRef.current) modelerRef.current.clear();
            await modelerRef.current.importXML(emptyBpmn);
            setDiagramName(DEFAULT_DIAGRAM_NAME);
            setDiagramId(null);
            setValidationResult(null);
            setElementNames({});
            setScore(0);
            setMessagesScore([]);
            elementsOriginalColors.current.clear();
            showNotification('Холст очищен: на нём пустой шаблон. Сохранённая версия осталась в реестре.');
            // Центрируем после очистки
            scheduleCanvasFit();
        } finally {
            setCanvasBusy(false);
        }
    };

    // Цвет заливки лежит в слое Diagram Interchange; у стрелок и соединений его
    // нет, и «отсутствует DI» пользователю ничего не говорит.
    const NO_DI_MESSAGE = 'Цвет можно менять только для фигур и событий — у стрелок заливки нет.';

    const handleColorChange = (color) => {
        const selection = modelerRef.current.get('selection');
        const selectedElements = selection.get();
        if (selectedElements.length === 0) {
            showNotification('Сначала выделите фигуру или событие на схеме.');
            return;
        }
        const element = selectedElements[0];
        const modeling = modelerRef.current.get('modeling');
        const di = getDi(element);
        if (di) {
            modeling.updateProperties(element, { 'di': { ...di, fill: color } });
            elementsOriginalColors.current.set(element.id, color);
            showNotification('Цвет элемента изменён.');
        } else showNotification(NO_DI_MESSAGE);
        setShowColorPicker(false);
    };

    const handleResetColor = () => {
        const selection = modelerRef.current.get('selection');
        const selectedElements = selection.get();
        if (selectedElements.length === 0) {
            showNotification('Сначала выделите фигуру или событие на схеме.');
            return;
        }
        const element = selectedElements[0];
        const modeling = modelerRef.current.get('modeling');
        const di = getDi(element);
        if (di) {
            const originalColor = elementsOriginalColors.current.get(element.id) || '#ffffff';
            modeling.updateProperties(element, { 'di': { ...di, fill: originalColor } });
            showNotification('Цвет элемента сброшен.');
        } else showNotification(NO_DI_MESSAGE);
        setShowColorPicker(false);
    };

    // Меню экспорта: открывается по кнопке и закрывается повторным нажатием,
    // Escape или кликом вне (useFloatingLayer). Позицию задаёт CSS — слой
    // заякорен на кнопке, а не на прокручиваемой полосе действий.
    const closeDownloadOptions = useCallback(() => setShowDownloadOptions(false), []);
    useFloatingLayer({
        open: showDownloadOptions,
        onClose: closeDownloadOptions,
        triggerRef: downloadTriggerRef,
        layerRef: downloadMenuRef,
    });

    const closeColorPicker = useCallback(() => setShowColorPicker(false), []);
    useFloatingLayer({
        open: showColorPicker,
        onClose: closeColorPicker,
        triggerRef: colorTriggerRef,
        layerRef: colorPopoverRef,
    });

    const handleShowDownloadOptions = () => setShowDownloadOptions((open) => !open);

    // Пропсы панели оценки стабилизированы: панель мемоизирована, а каждый рендер
    // редактора с новыми `|| []` сбрасывал бы прокрутку отчёта наверх.
    const scorePanelData = useMemo(() => ({
        recommendations: validationResult?.recommendations ?? [],
        errors: validationResult?.details ?? {},
        detailsMeta: validationResult?.details_meta ?? {},
        elementNames,
    }), [validationResult, elementNames]);

    return (
        <div className="editor-wrapper">
            <div className="editor-container">
                <motion.div
                    className="editor-header"
                    initial={{ y: -16, opacity: 0 }}
                    animate={{ y: 0, opacity: 1 }}
                    transition={{ type: 'spring', stiffness: 120, damping: 18 }}
                >
                    <Input
                        type="text"
                        value={diagramName}
                        maxLength={100}
                        onChange={(e) => setDiagramName(e.target.value)}
                        className="editor-diagram-name-input"
                        placeholder="Название схемы"
                        aria-label="Название схемы"
                    />
                    <div className="editor-header-actions">
                        <Button variant="primary" size="sm" onClick={handleSave}>
                            Сохранить
                        </Button>
                        <Button variant="secondary" size="sm" onClick={handleShare}>
                            Поделиться
                        </Button>
                        <div className="editor-download-group">
                            <Button
                                variant="secondary"
                                size="sm"
                                onClick={handleShowDownloadOptions}
                                ref={downloadTriggerRef}
                                aria-haspopup="menu"
                                aria-expanded={showDownloadOptions}
                            >
                                Скачать
                            </Button>
                            <AnimatePresence>
                                {showDownloadOptions && (
                                    <motion.div
                                        className="editor-download-options-modal"
                                        ref={downloadMenuRef}
                                        role="menu"
                                        aria-label="Формат скачивания схемы"
                                        initial={{ opacity: 0, y: -6 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        exit={{ opacity: 0, y: -6 }}
                                        transition={{ duration: 0.2 }}
                                    >
                                        <Button variant="ghost" size="sm" role="menuitem" onClick={() => handleDownload('bpmn')}>Схема (.bpmn)</Button>
                                        <Button variant="ghost" size="sm" role="menuitem" onClick={() => handleDownload('png')}>Картинка (.png)</Button>
                                        <Button variant="ghost" size="sm" role="menuitem" onClick={() => handleDownload('pdf')}>Документ (.pdf)</Button>
                                    </motion.div>
                                )}
                            </AnimatePresence>
                        </div>
                        <Button
                            variant="ghost"
                            size="sm"
                            className="editor-btn-danger-text"
                            onClick={handleRequestDelete}
                            disabled={!canDelete}
                            title={canDelete ? undefined : 'Несохранённую схему удалить нельзя'}
                        >
                            Удалить
                        </Button>
                    </div>
                </motion.div>
                <div className="editor-toolbar">
                    <Input
                        type="text"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder="Поиск элемента..."
                        className="editor-search-input"
                        aria-label="Поиск элемента по названию"
                    />
                    {scoreMessage && (
                        <div
                            className={`editor-score-note${scoreMessage.isError ? ' is-error' : ''}`}
                            role={scoreMessage.isError ? 'alert' : 'status'}
                        >
                            <span>{scoreMessage.text}</span>
                            <button
                                type="button"
                                className="editor-score-note__close"
                                onClick={() => setMessagesScore([])}
                                aria-label="Скрыть результат проверки"
                            >
                                ×
                            </button>
                        </div>
                    )}
                </div>
                <div className="editor-content">
                    <div className="editor-left-panel">
                        <div className="editor-palette-panel" ref={paletteRef} />
                    </div>
                    <div className="editor-canvas-area">
                        <div ref={containerRef} className="editor-bpmn-canvas" />
                        <AnimatePresence>
                            {canvasBusy && (
                                <motion.div
                                    className="editor-canvas-busy"
                                    initial={{ opacity: 0 }}
                                    animate={{ opacity: 1 }}
                                    exit={{ opacity: 0 }}
                                    transition={{ duration: 0.2 }}
                                >
                                    <PageLoader label="Открываем схему…" />
                                </motion.div>
                            )}
                        </AnimatePresence>
                        <AnimatePresence>
                            {showColorPicker && (
                                <motion.div
                                    className="editor-color-picker"
                                    ref={colorPopoverRef}
                                    initial={{ opacity: 0, y: -10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: -10 }}
                                    transition={{ duration: 0.2 }}
                                    role="group"
                                    aria-label="Цвет заливки элемента"
                                >
                                    {fillSwatches.map(({ color, label }) => (
                                        <button
                                            key={color}
                                            type="button"
                                            className="editor-swatch"
                                            style={{ backgroundColor: color }}
                                            onClick={() => handleColorChange(color)}
                                            aria-label={`Залить элемент цветом «${label}»`}
                                            title={label}
                                        />
                                    ))}
                                    <button
                                        type="button"
                                        className="editor-swatch editor-reset-color"
                                        onClick={handleResetColor}
                                        aria-label="Сбросить цвет элемента"
                                        title="Сбросить цвет"
                                    >
                                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                            <path d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM12 20C7.59 20 4 16.41 4 12C4 7.59 7.59 4 12 4C16.41 4 20 7.59 20 12C20 16.41 16.41 20 12 20ZM12 6C9.79 6 8 7.79 8 10C8 12.21 9.79 14 12 14C14.21 14 16 12.21 16 10C16 7.79 14.21 6 12 6ZM12 12C10.9 12 10 11.1 10 10C10 8.9 10.9 8 12 8C13.1 8 14 8.9 14 10C14 11.1 13.1 12 12 12Z" fill="currentColor" />
                                        </svg>
                                    </button>
                                </motion.div>
                            )}
                        </AnimatePresence>
                        <div className="editor-canvas-controls">
                            <Button variant="ghost" size="sm" onClick={handleZoomIn} aria-label="Приблизить схему">+</Button>
                            <Button variant="ghost" size="sm" onClick={handleZoomOut} aria-label="Отдалить схему">-</Button>
                            <Button variant="ghost" size="sm" onClick={handleResetZoom} aria-label="Сбросить масштаб схемы">Сбросить</Button>
                            <Button variant="ghost" size="sm" onClick={handleClearCanvas} aria-label="Очистить холст">Очистить</Button>
                        </div>
                    </div>
                    {/* Панели ИИ живут в одном flex-ряду с канвасом: CSS панелей
                        (.ai-chat-container) рассчитан на строку редактора — вынос
                        в колонку страницы уводил их под канвас. Рельс остаётся
                        прижат к краю, панель появляется между ним и канвасом. */}
                    <GenerateChat
                        isOpen={chatType === 'generate'}
                        onClose={() => setChatType(null)}
                        onGenerate={handleGenerate}
                        messages={messagesGenerate}
                        isExpanded={chatExpanded}
                        onToggleExpand={setChatExpanded}
                    />
                    <ImproveChat
                        isOpen={chatType === 'improve'}
                        onClose={() => setChatType(null)}
                        messages={messagesImprove}
                        onImprove={handleImprove}
                        onAcceptImprovement={handleAcceptImprovement}
                        onRejectImprovement={handleRejectImprovement}
                        isExpanded={chatExpanded}
                        onToggleExpand={setChatExpanded}
                    />
                    {showScorePanel && (
                        <ScorePanel
                            score={score}
                            {...scorePanelData}
                            onClose={() => {
                                setShowScorePanel(false);
                                revealReported([]);
                            }}
                            busy={scoreBusy}
                            error={scoreError}
                            empty={scoreEmpty}
                            onRetry={handleValidate}
                            onRevealElements={revealReported}
                            onFocusElements={focusReported}
                            isExpanded={scoreExpanded}
                            onToggleExpand={setScoreExpanded}
                        />
                    )}
                    <motion.div
                        className="editor-right-panel"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        transition={{ duration: 0.3 }}
                    >
                        <div className="editor-panel-icons">
                            <motion.button
                                whileHover={{ y: -2 }}
                                whileTap={{ scale: 0.94 }}
                                onClick={() => {
                                    if (chatType === 'improve') {
                                        setChatType(null);
                                    } else {
                                        setChatType('improve');
                                        setChatExpanded(false);
                                        // Панели редактора исключают друг друга:
                                        // две сразу сжимали канвас до нуля.
                                        setShowScorePanel(false);
                                    }
                                }}
                                className={`editor-icon-btn editor-chat-btn ${chatType === 'improve' ? 'is-active' : ''}`}
                                aria-pressed={chatType === 'improve'}
                                aria-label="Улучшить схему"
                                data-tooltip="Улучшить"
                            >
                                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                                    <path d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM15.69 16.37L13.69 14.37C13.26 13.94 12.59 13.94 12.16 14.37L10.16 16.37C9.73 16.8 9.73 17.47 10.16 17.9C10.59 18.33 11.26 18.33 11.69 17.9L12.5 17.09V13H13.5V17.09L14.31 17.9C14.74 18.33 15.41 18.33 15.84 17.9C16.27 17.47 16.27 16.8 15.84 16.37H15.69ZM12 4C15.31 4 18 6.69 18 10H16C16 7.79 14.21 6 12 6C9.79 6 8 7.79 8 10C8 12.21 9.79 14 12 14C12.55 14 13.08 13.9 13.58 13.71C13.96 13.86 14.39 14 14.84 14C16.26 14 17.5 12.76 17.5 11.34C17.5 10.12 16.58 9.1 15.46 8.91C15.17 6.86 13.66 5.26 12 5C11.42 5 10.85 5.14 10.34 5.41C10.95 4.92 11.72 4.61 12.5 4.5V4H12Z" fill="currentColor" />
                                </svg>
                            </motion.button>
                            <motion.button
                                whileHover={{ y: -2 }}
                                whileTap={{ scale: 0.94 }}
                                onClick={() => {
                                    if (chatType === 'generate') {
                                        setChatType(null);
                                    } else {
                                        setChatType('generate');
                                        setChatExpanded(false);
                                        setShowScorePanel(false);
                                    }
                                }}
                                className={`editor-icon-btn editor-generate-btn ${chatType === 'generate' ? 'is-active' : ''}`}
                                aria-pressed={chatType === 'generate'}
                                aria-label="Сгенерировать схему"
                                data-tooltip="Генерировать"
                            >
                                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                                    <path d="M5 17H13V19H5C3.9 19 3 18.1 3 17V7C3 5.9 3.9 5 5 5H13V7H5V17ZM19 17H15V15H19C20.1 15 21 14.1 21 13V11C21 9.9 20.1 9 19 9H15V7H19C20.66 7 22 8.34 22 10V14C22 15.66 20.66 17 19 17Z" fill="currentColor" />
                                </svg>
                            </motion.button>
                            <motion.button
                                whileHover={{ y: -2 }}
                                whileTap={{ scale: 0.94 }}
                                onClick={handleValidate}
                                className={`editor-icon-btn editor-score-btn ${showScorePanel ? 'is-active' : ''}`}
                                aria-pressed={showScorePanel}
                                aria-label="Проверить схему"
                                aria-disabled={!scorable}
                                disabled={!scorable}
                                data-tooltip={scorable ? 'Проверить' : 'Нечего проверять'}
                                title={scorable ? undefined : 'Добавьте шаги и завершение процесса — проверять пока нечего'}
                            >
                                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                                    <path d="M9 16.17L4.83 12L3.41 13.41L9 19L21 7L19.59 5.59L9 16.17Z" fill="currentColor" />
                                </svg>
                            </motion.button>
                            <motion.button
                                whileHover={{ y: -2 }}
                                whileTap={{ scale: 0.94 }}
                                onClick={() => setShowColorPicker(!showColorPicker)}
                                className={`editor-icon-btn editor-color-btn ${showColorPicker ? 'is-active' : ''}`}
                                aria-pressed={showColorPicker}
                                aria-expanded={showColorPicker}
                                aria-haspopup="true"
                                aria-label="Изменить цвет элемента"
                                data-tooltip="Изменить цвет"
                                ref={colorTriggerRef}
                            >
                                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                                    <path d="M12 3C7.03 3 3 7.03 3 12C3 16.97 7.03 21 12 21C12.83 21 13.5 20.33 13.5 19.5C13.5 19.11 13.41 18.74 13.25 18.41C13.09 18.07 12.83 17.83 12.5 17.73C12.17 17.63 11.83 17.67 11.53 17.83C10.54 18.37 9.37 18.5 8 18.5C5.24 18.5 3 16.26 3 13.5C3 10.74 5.24 8.5 8 8.5C9.37 8.5 10.54 8.63 11.53 9.17C11.83 9.33 12.17 9.37 12.5 9.27C12.83 9.17 13.09 8.93 13.25 8.59C13.41 8.26 13.5 7.89 13.5 7.5C13.5 6.67 12.83 6 12 6C7.03 6 3 10.03 3 15C3 19.97 7.03 24 12 24C16.97 24 21 19.97 21 15C21 10.03 16.97 6 12 6C11.17 6 10.5 6.67 10.5 7.5C10.5 7.89 10.59 8.26 10.75 8.59C10.91 8.93 11.17 9.17 11.5 9.27C11.83 9.37 12.17 9.33 12.47 9.17C13.46 8.63 14.63 8.5 16 8.5C18.76 8.5 21 10.74 21 13.5C21 16.26 18.76 18.5 16 18.5C14.63 18.5 13.46 18.37 12.47 17.83C12.17 17.67 11.83 17.63 11.5 17.73C11.17 17.83 10.91 18.07 10.75 18.41C10.59 18.74 10.5 19.11 10.5 19.5C10.5 20.33 11.17 21 12 21ZM8 10C7.45 10 7 10.45 7 11C7 11.55 7.45 12 8 12C8.55 12 9 11.55 9 11C9 10.45 8.55 10 8 10ZM16 10C15.45 10 15 10.45 15 11C15 11.55 15.45 12 16 12C16.55 12 17 11.55 17 11C17 10.45 16.55 10 16 10Z" fill="currentColor" />
                                </svg>
                            </motion.button>
                        </div>
                    </motion.div>
                </div>
                <AnimatePresence>
                    {notification && (
                        <motion.div
                            className="editor-notification"
                            role="status"
                            aria-live="polite"
                            initial={{ opacity: 0, y: -16 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -16 }}
                            transition={{ duration: 0.3 }}
                        >
                            {notification}
                        </motion.div>
                    )}
                </AnimatePresence>
                <AnimatePresence>
                    {pendingDelete && (
                        <motion.div
                            className="editor-dialog-backdrop"
                            role="presentation"
                            onClick={handleCancelDelete}
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            transition={{ duration: 0.2 }}
                        >
                            <motion.section
                                className="editor-dialog"
                                role="dialog"
                                aria-modal="true"
                                aria-labelledby="editor-delete-title"
                                onClick={(e) => e.stopPropagation()}
                                initial={{ opacity: 0, y: 16, scale: 0.98 }}
                                animate={{ opacity: 1, y: 0, scale: 1 }}
                                exit={{ opacity: 0, y: 16, scale: 0.98 }}
                                transition={{ duration: 0.2, ease: [0.33, 1, 0.68, 1] }}
                            >
                                <h2 id="editor-delete-title">Удалить «{diagramName}»?</h2>
                                <p>
                                    Схема удалится из реестра — вернуть её будет нельзя.
                                    Файлы, скачанные из этой схемы, останутся у вас.
                                </p>
                                <div className="editor-dialog__actions">
                                    <Button variant="secondary" size="md" onClick={handleCancelDelete}>Отмена</Button>
                                    <Button variant="primary" size="md" className="editor-btn-danger" onClick={handleConfirmDelete}>Удалить</Button>
                                </div>
                            </motion.section>
                        </motion.div>
                    )}
                </AnimatePresence>
            </div>
        </div>
    );
};

export default Editor;

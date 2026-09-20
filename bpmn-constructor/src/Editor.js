import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import BpmnModeler from 'bpmn-js/lib/Modeler';
import { layoutProcess } from 'bpmn-auto-layout-feat-ivan-tulaev';
import 'bpmn-js/dist/assets/diagram-js.css';
import 'bpmn-js/dist/assets/bpmn-font/css/bpmn.css';
import { apiClient } from './api/client';
import { v4 as uuid } from 'uuid';
import html2canvas from 'html2canvas';
import { jsPDF } from 'jspdf';
import GenerateChat from './GenerateChat';
import ImproveChat from './ImproveChat';
import ScorePanel from './ScorePanel';
import { getDi } from 'bpmn-js/lib/util/ModelUtil';
import { motion } from 'framer-motion';
import Header from './Header';
import './Editor.css';
// Импортируем grid модуль
import gridModule from 'diagram-js-grid';

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
  <bpmn:process id="Process_${uuid()}" isExecutable="false">
    <bpmn:startEvent id="StartEvent_${uuid()}" name="Start">
      <bpmn:outgoing>Flow_${uuid()}</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_${uuid()}" name="Task">
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
    const downloadButtonRef = useRef(null);
    const [diagramName, setDiagramName] = useState('Новая схема');
    const [diagramId, setDiagramId] = useState(null);
    const [validationResult, setValidationResult] = useState(null);
    const [score, setScore] = useState(0);
    const [showScorePanel, setShowScorePanel] = useState(false);
    const [headerVisible] = useState(true);
    const [notification, setNotification] = useState(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [chatType, setChatType] = useState(null);
    const [showColorPicker, setShowColorPicker] = useState(false);
    const [, setCurrentXml] = useState(emptyBpmn);
    const [, setPendingImprovementId] = useState(null);
    const location = useLocation();
    const navigate = useNavigate();
    const notificationTimeoutRef = useRef(null);
    const elementsOriginalColors = useRef(new Map());
    const [chatExpanded, setChatExpanded] = useState(false);
    const [chatHeight] = useState('70vh');
    const [chatPosition, setChatPosition] = useState({ top: 120, right: 20 });
    const [showDownloadOptions, setShowDownloadOptions] = useState(false);
    const [downloadPosition, setDownloadPosition] = useState({ top: 0, left: 0 });
    const [scoreExpanded, setScoreExpanded] = useState(false);
    const [scorePosition, setScorePosition] = useState({ top: 120, right: 20 });
    const [scoreHeight, setScoreHeight] = useState('70vh');
    const [messagesGenerate, setMessagesGenerate] = useState([]);
    const [messagesImprove, setMessagesImprove] = useState([]);
    const [, setMessagesScore] = useState([]);
    // Оптимизация поиска
    const searchResults = useRef(new Map());
    const lastSearchQuery = useRef('');
    const searchTimeoutRef = useRef(null);

    // Улучшенное центрирование канваса по центральному элементу
    const autoFitDiagram = useCallback(() => {
        console.log('Начало центрирования диаграммы');
        if (!modelerRef.current) {
            console.warn('Моделер не инициализирован');
            return;
        }
        try {
            const canvas = modelerRef.current.get('canvas');
            const elementRegistry = modelerRef.current.get('elementRegistry');
            if (!canvas.getRootElement()) {
                console.warn('Корневой элемент не найден');
                return;
            }
            // Получаем все элементы диаграммы
            const elements = elementRegistry.getAll().filter(element =>
                element.type !== 'label' &&
                element.width && element.height &&
                element.x !== undefined && element.y !== undefined
            );
            console.log('Найдено элементов:', elements.length);
            if (elements.length === 0) {
                console.log('Элементы не найдены, используем масштаб по умолчанию');
                canvas.zoom(1.0);
                return;
            }
            // Вычисляем границы диаграммы
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            elements.forEach(element => {
                minX = Math.min(minX, element.x);
                minY = Math.min(minY, element.y);
                maxX = Math.max(maxX, element.x + element.width);
                maxY = Math.max(maxY, element.y + element.height);
            });
            console.log('Границы диаграммы:', { minX, minY, maxX, maxY });
            // Получаем размеры контейнера
            const container = containerRef.current;
            if (!container || container.clientWidth === 0 || container.clientHeight === 0) {
                console.warn('Контейнер еще не имеет размеров');
                return;
            }
            const containerWidth = container.clientWidth;
            const containerHeight = container.clientHeight;
            console.log('Размеры контейнера:', { containerWidth, containerHeight });
            // Вычисляем масштаб с учетом отступов
            const padding = 100;
            const scaleX = containerWidth / (maxX - minX + padding);
            const scaleY = containerHeight / (maxY - minY + padding);
            const scale = Math.min(scaleX, scaleY, 1.0);
            console.log('Масштаб:', scale);
            // Применяем масштаб
            canvas.zoom(scale);
            console.log('Масштаб применен');
            console.log('Центрирование завершено');
        } catch (error) {
            console.error('Ошибка при центрировании:', error);
            try {
                const canvas = modelerRef.current.get('canvas');
                canvas.zoom(1.0);
            } catch (fallbackError) {
                console.error('Критическая ошибка центрирования:', fallbackError);
            }
        }
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
                        modeling.updateProperties(element, { 'di': { ...di, fill: '#2dbe64' } });
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

    const toggleChatExpand = (expanded) => {
        setChatExpanded(expanded);
        if (expanded) {
            setChatPosition({ top: 80, right: 0 });
        } else {
            setChatPosition({ top: 120, right: 20 });
        }
    };

    const toggleScoreExpand = (expanded) => {
        setScoreExpanded(expanded);
        if (expanded) {
            setScorePosition({ top: 80, right: 0 });
            setScoreHeight('80vh');
        } else {
            setScorePosition({ top: 120, right: 20 });
            setScoreHeight('70vh');
        }
    };

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

    useEffect(() => {
        console.log('Инициализация BPMN редактора');
        // Создание модели с расширенными модулями
        modelerRef.current = new BpmnModeler({
            container: containerRef.current,
            palette: { container: paletteRef.current },
            additionalModules: [
                gridModule // Модуль для сетки
            ],
            grid: {
                visible: true,
                snap: true,
                size: 30
            }
        });

        const loadDiagram = async () => {
            console.log('Загрузка диаграммы');
            try {
                let initialXML = emptyBpmn;
                if (modelerRef.current) modelerRef.current.clear();
                const urlParams = new URLSearchParams(location.search);
                let loadedDiagramId = urlParams.get('load');
                if (loadedDiagramId) {
                    console.log('Загрузка диаграммы по ID:', loadedDiagramId);
                    const response = await apiClient.get(`/api/diagrams/${loadedDiagramId}`);
                    initialXML = response.data.xml_content;
                    setDiagramName(response.data.name || `Схема ${loadedDiagramId}`);
                    setDiagramId(loadedDiagramId);
                } else if (location.state?.id) {
                    console.log('Загрузка диаграммы из состояния:', location.state.id);
                    const response = await apiClient.get(`/api/diagrams/${location.state.id}`);
                    initialXML = response.data.xml_content;
                    setDiagramName(response.data.name || 'Новая схема');
                    setDiagramId(location.state.id);
                } else if (location.state?.bpmnXML) {
                    console.log('Загрузка диаграммы из XML');
                    initialXML = location.state.bpmnXML;
                    setDiagramName(location.state.name || 'Загруженная схема');
                } else {
                    console.log('Создание новой диаграммы');
                    setDiagramName('Новая схема');
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
                        layoutedXML = await layoutProcess(initialXML);
                    } catch (layoutErr) {
                        console.warn('Ошибка layoutProcess, загружаем без раскладки:', layoutErr);
                        layoutedXML = initialXML;
                    }
                }
                if (modelerRef.current) {
                    console.log('Импорт XML в модельер');
                    await modelerRef.current.importXML(layoutedXML);
                    const elementRegistry = modelerRef.current.get('elementRegistry');
                    elementRegistry.forEach((element) => {
                        const di = getDi(element);
                        if (di && di.fill) elementsOriginalColors.current.set(element.id, di.fill);
                    });
                    setCurrentXml(layoutedXML);
                    // Увеличиваем задержку для правильного центрирования
                    console.log('Планирование центрирования после загрузки');
                    setTimeout(() => {
                        autoFitDiagram();
                    }, 800);
                }
            } catch (err) {
                console.error('Ошибка загрузки диаграммы:', err);
                if (modelerRef.current) {
                    modelerRef.current.clear();
                    await modelerRef.current.importXML(emptyBpmn);
                    setCurrentXml(emptyBpmn);
                    setTimeout(() => {
                        if (modelerRef.current) {
                            autoFitDiagram();
                        }
                    }, 800);
                }
                showNotification('Ошибка загрузки диаграммы, загружено начальное состояние.');
            }
        };

        loadDiagram();

        const eventBus = modelerRef.current.get('eventBus');
        eventBus.on('element.changed', async () => {
            const { xml } = await modelerRef.current.saveXML({ format: true });
            setCurrentXml(xml);
        });

        return () => {
            if (modelerRef.current) modelerRef.current.destroy();
        };
    }, [location.search, location.state, showNotification, autoFitDiagram]);

    const handleGenerate = async (prompt) => {
        console.log('Генерация диаграммы с промптом:', prompt);
        setMessagesGenerate(prev => [...prev,
        { sender: 'user', text: prompt, id: Date.now() },
        { sender: 'AI', text: 'Схема генерируется...', id: Date.now() + 1 }
        ]);
        try {
            const response = await apiClient.post('/api/generate', {
                text: prompt || 'Создайте стандартную BPMN диаграмму',
                detail_level: 'Medium',
            });
            if (response.data.status === 'success') {
                const newBpmnXML = response.data.bpmn;
                if (modelerRef.current) modelerRef.current.clear();
                const layoutedXML = await layoutProcess(newBpmnXML);
                await modelerRef.current.importXML(layoutedXML);
                setDiagramName(`Сгенерировано: ${prompt?.slice(0, 20) || 'Новая схема'}`);
                setMessagesGenerate(prev => [
                    ...prev.slice(0, -1),
                    { sender: 'AI', text: 'Диаграмма успешно сгенерирована.', id: Date.now() }
                ]);
                setCurrentXml(layoutedXML);
                setTimeout(() => {
                    if (modelerRef.current) {
                        autoFitDiagram();
                    }
                }, 800);
            }
        } catch (err) {
            console.error('Ошибка генерации:', err);
            setMessagesGenerate(prev => [
                ...prev.slice(0, -1),
                { sender: 'AI', text: `Ошибка генерации: ${err.message}`, id: Date.now() }
            ]);
        }
    };

    const handleValidate = async () => {
        console.log('Валидация диаграммы');
        try {
            const { xml } = await modelerRef.current.saveXML({ format: true });
            const fixedXML = await fixXMLStructure(xml);
            let layoutedXML;
            try {
                layoutedXML = await layoutProcess(fixedXML);
            } catch (layoutError) {
                console.warn('Layout failed, using original XML:', layoutError);
                layoutedXML = fixedXML;
            }
            const response = await apiClient.post('/api/evaluate', {
                bpmn_xml: layoutedXML,
            });
            setValidationResult(response.data);
            setScore(response.data.score);
            setShowScorePanel(true);
            setMessagesScore([{
                sender: 'AI',
                text: `Проверка завершена. Оценка: ${response.data.score}/100. ${response.data.recommendations?.join(' ') || ''}`,
                id: Date.now()
            }]);
            if (modelerRef.current && response.data.optimized_bpmn) {
                console.log('Применение оптимизированной диаграммы');
                try {
                    modelerRef.current.clear();
                    const fixedOptimizedXML = await fixXMLStructure(response.data.optimized_bpmn);
                    let optimizedLayoutedXML;
                    try {
                        optimizedLayoutedXML = await layoutProcess(fixedOptimizedXML);
                    } catch (layoutError) {
                        console.warn('Optimized layout failed, using fixed XML:', layoutError);
                        optimizedLayoutedXML = fixedOptimizedXML;
                    }
                    await modelerRef.current.importXML(optimizedLayoutedXML);
                    setCurrentXml(optimizedLayoutedXML);
                    // Увеличиваем задержку после оптимизации
                    setTimeout(() => {
                        if (modelerRef.current) {
                            autoFitDiagram();
                        }
                    }, 800);
                } catch (importError) {
                    console.warn('Failed to import optimized XML:', importError);
                    await modelerRef.current.importXML(layoutedXML);
                    setCurrentXml(layoutedXML);
                    setTimeout(() => {
                        autoFitDiagram();
                    }, 800);
                }
            }
        } catch (err) {
            console.error('Ошибка проверки:', err);
            setMessagesScore([{
                sender: 'AI',
                text: `Ошибка проверки: ${err.message}`,
                id: Date.now()
            }]);
        }
    };

    const handleImprove = async (prompt) => {
        console.log('Улучшение диаграммы с промптом:', prompt);
        setMessagesImprove(prev => [...prev,
        { sender: 'user', text: prompt, id: Date.now() },
        { sender: 'AI', text: 'Анализирую схему...', id: Date.now() + 1 }
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
                    id: Date.now()
                }
            ]);
            if (response.data.improvement_id) {
                setPendingImprovementId(response.data.improvement_id);
            }
        } catch (err) {
            console.error('Ошибка улучшения:', err);
            setMessagesImprove(prev => [
                ...prev.slice(0, -1),
                { sender: 'AI', text: `Ошибка: ${err.message}`, id: Date.now() }
            ]);
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
                    startEvent.setAttribute('name', 'Start');
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
                layoutedXML = await layoutProcess(fixedXML);
            } catch (layoutError) {
                console.warn('Layout failed for accepted improvement, using fixed XML:', layoutError);
                layoutedXML = fixedXML;
            }
            if (modelerRef.current) {
                modelerRef.current.clear();
                await modelerRef.current.importXML(layoutedXML);
                const elementRegistry = modelerRef.current.get('elementRegistry');
                elementRegistry.forEach((element) => {
                    const di = getDi(element);
                    if (di && di.fill) {
                        elementsOriginalColors.current.set(element.id, di.fill);
                    }
                });
                setCurrentXml(layoutedXML);
                setDiagramId(response.data.diagram_id);
                setPendingImprovementId(null);
                setMessagesImprove(prev => [...prev,
                { sender: 'AI', text: 'Изменения успешно применены.', id: Date.now() }
                ]);
                // Центрируем после принятия улучшения
                setTimeout(() => {
                    autoFitDiagram();
                }, 800);
            }
        } catch (err) {
            console.error('Ошибка принятия изменений:', err);
            setMessagesImprove(prev => [...prev,
            { sender: 'AI', text: `Ошибка принятия изменений: ${err.message}`, id: Date.now() }
            ]);
        }
    };

    const handleSave = async () => {
        console.log('Сохранение диаграммы');
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
            showNotification('Диаграмма успешно сохранена!');
        } catch (err) {
            console.error('Ошибка сохранения:', err);
            showNotification('Ошибка сохранения диаграммы.');
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
            navigator.clipboard.writeText(shareLink);
            showNotification('Ссылка скопирована в буфер обмена!');
        } catch (err) {
            console.error('Ошибка создания ссылки:', err);
            showNotification('Ошибка создания ссылки.');
        }
    };

    const handleDelete = async () => {
        console.log('Удаление диаграммы');
        try {
            if (diagramId) {
                await apiClient.delete(`/api/diagrams/${diagramId}`);
                navigate('/my-schemas');
            } else showNotification('Сначала сохраните схему.');
        } catch (err) {
            console.error('Ошибка удаления:', err);
            showNotification('Ошибка удаления диаграммы.');
        }
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
            showNotification(`Ошибка при экспорте в ${format.toUpperCase()}: ${err.message}`);
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
        if (modelerRef.current) modelerRef.current.clear();
        await modelerRef.current.importXML(emptyBpmn);
        setDiagramName('Новая схема');
        setDiagramId(null);
        elementsOriginalColors.current.clear();
        showNotification('Канвас очищен.');
        setCurrentXml(emptyBpmn);
        // Центрируем после очистки
        setTimeout(() => {
            autoFitDiagram();
        }, 800);
    };

    const handleColorChange = (color) => {
        const selection = modelerRef.current.get('selection');
        const selectedElements = selection.get();
        if (selectedElements.length === 0) {
            showNotification('Выберите элемент для изменения цвета.');
            return;
        }
        const element = selectedElements[0];
        const modeling = modelerRef.current.get('modeling');
        const di = getDi(element);
        if (di) {
            modeling.updateProperties(element, { 'di': { ...di, fill: color } });
            elementsOriginalColors.current.set(element.id, color);
            showNotification('Цвет элемента изменён.');
        } else showNotification('Не удалось изменить цвет: отсутствует DI.');
        setShowColorPicker(false);
    };

    const handleResetColor = () => {
        const selection = modelerRef.current.get('selection');
        const selectedElements = selection.get();
        if (selectedElements.length === 0) {
            showNotification('Выберите элемент для сброса цвета.');
            return;
        }
        const element = selectedElements[0];
        const modeling = modelerRef.current.get('modeling');
        const di = getDi(element);
        if (di) {
            const originalColor = elementsOriginalColors.current.get(element.id) || '#ffffff';
            modeling.updateProperties(element, { 'di': { ...di, fill: originalColor } });
            showNotification('Цвет элемента сброшен.');
        } else showNotification('Не удалось сбросить цвет: отсутствует DI.');
        setShowColorPicker(false);
    };

    const handleShowDownloadOptions = () => {
        if (downloadButtonRef.current) {
            const rect = downloadButtonRef.current.getBoundingClientRect();
            setDownloadPosition({
                top: rect.bottom + window.scrollY + 10,
                left: rect.left + window.scrollX
            });
        }
        setShowDownloadOptions(true);
    };

    return (
        <div className="editor-wrapper">
            <Header />
            <div className="editor-container">
                <div className="editor-left-panel">
                    <div className="editor-palette-panel" ref={paletteRef} />
                </div>
                {headerVisible && (
                    <motion.div className="editor-header" initial={{ y: -60 }} animate={{ y: 0 }} exit={{ y: -60 }} transition={{ type: 'spring', stiffness: 100 }}>
                        <input
                            type="text"
                            value={diagramName}
                            onChange={(e) => setDiagramName(e.target.value)}
                            className="editor-diagram-name-input"
                            placeholder="Название схемы"
                        />
                        <div className="editor-header-actions">
                            <motion.button whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={handleSave} className="editor-action-btn editor-save-btn">
                                Сохранить
                            </motion.button>
                            <motion.button whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={handleShare} className="editor-action-btn editor-share-btn">
                                Поделиться
                            </motion.button>
                            <motion.button ref={downloadButtonRef} whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={handleShowDownloadOptions} className="editor-action-btn editor-download-btn">
                                Скачать
                            </motion.button>
                            <motion.button whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }} onClick={handleDelete} className="editor-action-btn editor-delete-btn">
                                Удалить
                            </motion.button>
                        </div>
                    </motion.div>
                )}
                {showColorPicker && (
                    <motion.div
                        className="editor-color-picker"
                        initial={{ opacity: 0, y: -10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -10 }}
                        transition={{ duration: 0.2 }}
                    >
                        <div className="editor-color-option" style={{ backgroundColor: '#000000' }} onClick={() => handleColorChange('#000000')}></div>
                        <div className="editor-color-option" style={{ backgroundColor: '#FF0000' }} onClick={() => handleColorChange('#FF0000')}></div>
                        <div className="editor-color-option" style={{ backgroundColor: '#00FF00' }} onClick={() => handleColorChange('#00FF00')}></div>
                        <div className="editor-color-option" style={{ backgroundColor: '#0000FF' }} onClick={() => handleColorChange('#0000FF')}></div>
                        <div className="editor-color-option" style={{ backgroundColor: '#FFFF00' }} onClick={() => handleColorChange('#FFFF00')}></div>
                        <div className="editor-color-option" style={{ backgroundColor: '#FF00FF' }} onClick={() => handleColorChange('#FF00FF')}></div>
                        <div className="editor-color-option editor-reset-color" onClick={handleResetColor}>
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM12 20C7.59 20 4 16.41 4 12C4 7.59 7.59 4 12 4C16.41 4 20 7.59 20 12C20 16.41 16.41 20 12 20ZM12 6C9.79 6 8 7.79 8 10C8 12.21 9.79 14 12 14C14.21 14 16 12.21 16 10C16 7.79 14.21 6 12 6ZM12 12C10.9 12 10 11.1 10 10C10 8.9 10.9 8 12 8C13.1 8 14 8.9 14 10C14 11.1 13.1 12 12 12Z" fill="#ff4d4f" />
                            </svg>
                        </div>
                    </motion.div>
                )}
                {notification && (
                    <motion.div className="editor-notification" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 20 }} transition={{ duration: 0.3 }}>
                        {notification}
                    </motion.div>
                )}
                {showDownloadOptions && (
                    <motion.div
                        className="editor-download-options-modal"
                        style={{ top: downloadPosition.top, left: downloadPosition.left }}
                        initial={{ opacity: 0, scale: 0.9 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.9 }}
                        transition={{ duration: 0.3 }}
                    >
                        <button onClick={() => handleDownload('bpmn')} className="editor-modal-btn">BPMN</button>
                        <button onClick={() => handleDownload('png')} className="editor-modal-btn">PNG</button>
                        <button onClick={() => handleDownload('pdf')} className="editor-modal-btn">PDF</button>
                        <button onClick={() => setShowDownloadOptions(false)} className="editor-modal-btn editor-close-btn">Закрыть</button>
                    </motion.div>
                )}
                <div className="editor-toolbar">
                    <input
                        type="text"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder="Поиск элемента..."
                        className="editor-search-input"
                    />
                </div>
                <div className="editor-content">
                    <div ref={containerRef} className="editor-bpmn-canvas" />
                    <div className="editor-canvas-controls">
                        <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} onClick={handleZoomIn} className="editor-control-btn">+</motion.button>
                        <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} onClick={handleZoomOut} className="editor-control-btn">-</motion.button>
                        <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} onClick={handleResetZoom} className="editor-control-btn">Сбросить</motion.button>
                        <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} onClick={handleClearCanvas} className="editor-control-btn">Очистить</motion.button>
                    </div>
                </div>
                <motion.div
                    className="editor-right-panel"
                    initial={{ x: 0 }}
                    animate={{ x: 0 }}
                    transition={{ type: 'spring', damping: 25 }}
                    style={{ width: '60px', position: 'fixed', right: 0, top: 120, bottom: 0 }}
                >
                    <div className="editor-panel-icons">
                        <motion.button
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                            onClick={() => {
                                if (chatType === 'improve') {
                                    setChatType(null);
                                } else {
                                    setChatType('improve');
                                    setChatExpanded(false);
                                }
                            }}
                            className={`editor-icon-btn editor-chat-btn ${chatType === 'improve' ? 'active' : ''}`}
                            data-tooltip="Улучшить"
                        >
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM15.69 16.37L13.69 14.37C13.26 13.94 12.59 13.94 12.16 14.37L10.16 16.37C9.73 16.8 9.73 17.47 10.16 17.9C10.59 18.33 11.26 18.33 11.69 17.9L12.5 17.09V13H13.5V17.09L14.31 17.9C14.74 18.33 15.41 18.33 15.84 17.9C16.27 17.47 16.27 16.8 15.84 16.37H15.69ZM12 4C15.31 4 18 6.69 18 10H16C16 7.79 14.21 6 12 6C9.79 6 8 7.79 8 10C8 12.21 9.79 14 12 14C12.55 14 13.08 13.9 13.58 13.71C13.96 13.86 14.39 14 14.84 14C16.26 14 17.5 12.76 17.5 11.34C17.5 10.12 16.58 9.1 15.46 8.91C15.17 6.86 13.66 5.26 12 5C11.42 5 10.85 5.14 10.34 5.41C10.95 4.92 11.72 4.61 12.5 4.5V4H12Z" fill="#2dbe64" />
                            </svg>
                        </motion.button>
                        <motion.button
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                            onClick={() => {
                                if (chatType === 'generate') {
                                    setChatType(null);
                                } else {
                                    setChatType('generate');
                                    setChatExpanded(false);
                                }
                            }}
                            className={`editor-icon-btn editor-generate-btn ${chatType === 'generate' ? 'active' : ''}`}
                            data-tooltip="Генерировать"
                        >
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M5 17H13V19H5C3.9 19 3 18.1 3 17V7C3 5.9 3.9 5 5 5H13V7H5V17ZM19 17H15V15H19C20.1 15 21 14.1 21 13V11C21 9.9 20.1 9 19 9H15V7H19C20.66 7 22 8.34 22 10V14C22 15.66 20.66 17 19 17Z" fill="#2dbe64" />
                            </svg>
                        </motion.button>
                        <motion.button
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                            onClick={handleValidate}
                            className={`editor-icon-btn editor-score-btn ${showScorePanel ? 'active' : ''}`}
                            data-tooltip="Проверить"
                        >
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M9 16.17L4.83 12L3.41 13.41L9 19L21 7L19.59 5.59L9 16.17Z" fill="#2dbe64" />
                            </svg>
                        </motion.button>
                        <motion.button
                            whileHover={{ scale: 1.1 }}
                            whileTap={{ scale: 0.9 }}
                            onClick={() => setShowColorPicker(!showColorPicker)}
                            className={`editor-icon-btn editor-color-btn ${showColorPicker ? 'active' : ''}`}
                            data-tooltip="Изменить цвет"
                        >
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M12 3C7.03 3 3 7.03 3 12C3 16.97 7.03 21 12 21C12.83 21 13.5 20.33 13.5 19.5C13.5 19.11 13.41 18.74 13.25 18.41C13.09 18.07 12.83 17.83 12.5 17.73C12.17 17.63 11.83 17.67 11.53 17.83C10.54 18.37 9.37 18.5 8 18.5C5.24 18.5 3 16.26 3 13.5C3 10.74 5.24 8.5 8 8.5C9.37 8.5 10.54 8.63 11.53 9.17C11.83 9.33 12.17 9.37 12.5 9.27C12.83 9.17 13.09 8.93 13.25 8.59C13.41 8.26 13.5 7.89 13.5 7.5C13.5 6.67 12.83 6 12 6C7.03 6 3 10.03 3 15C3 19.97 7.03 24 12 24C16.97 24 21 19.97 21 15C21 10.03 16.97 6 12 6C11.17 6 10.5 6.67 10.5 7.5C10.5 7.89 10.59 8.26 10.75 8.59C10.91 8.93 11.17 9.17 11.5 9.27C11.83 9.37 12.17 9.33 12.47 9.17C13.46 8.63 14.63 8.5 16 8.5C18.76 8.5 21 10.74 21 13.5C21 16.26 18.76 18.5 16 18.5C14.63 18.5 13.46 18.37 12.47 17.83C12.17 17.67 11.83 17.63 11.5 17.73C11.17 17.83 10.91 18.07 10.75 18.41C10.59 18.74 10.5 19.11 10.5 19.5C10.5 20.33 11.17 21 12 21ZM8 10C7.45 10 7 10.45 7 11C7 11.55 7.45 12 8 12C8.55 12 9 11.55 9 11C9 10.45 8.55 10 8 10ZM16 10C15.45 10 15 10.45 15 11C15 11.55 15.45 12 16 12C16.55 12 17 11.55 17 11C17 10.45 16.55 10 16 10Z" fill="#2dbe64" />
                            </svg>
                        </motion.button>
                    </div>
                </motion.div>
                <GenerateChat
                    isOpen={chatType === 'generate'}
                    onClose={() => setChatType(null)}
                    onGenerate={handleGenerate}
                    messages={messagesGenerate}
                    isExpanded={chatExpanded}
                    onToggleExpand={toggleChatExpand}
                    chatHeight={chatHeight}
                    position={chatPosition}
                />
                <ImproveChat
                    isOpen={chatType === 'improve'}
                    onClose={() => setChatType(null)}
                    messages={messagesImprove}
                    onImprove={handleImprove}
                    onAcceptImprovement={handleAcceptImprovement}
                    isExpanded={chatExpanded}
                    onToggleExpand={toggleChatExpand}
                    chatHeight={chatHeight}
                    position={chatPosition}
                />
                {showScorePanel && (
                    <ScorePanel
                        score={score}
                        recommendations={validationResult?.recommendations || []}
                        errors={validationResult?.details || {}}
                        onClose={() => setShowScorePanel(false)}
                        isExpanded={scoreExpanded}
                        onToggleExpand={toggleScoreExpand}
                        position={scorePosition}
                        chatHeight={scoreHeight}
                    />
                )}
            </div>
        </div>
    );
};

export default Editor;

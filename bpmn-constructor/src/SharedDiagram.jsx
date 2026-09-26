import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import BpmnViewer from 'bpmn-js/lib/NavigatedViewer';
import { useAuth } from './context/AuthContext';
import { apiClient, toUserMessage } from './api/client';
import { WorkPage, PageLoader, EmptyState } from './components/layout';
import { Button } from './components/ui';
import 'bpmn-js/dist/assets/diagram-js.css';
import 'bpmn-js/dist/assets/bpmn-font/css/bpmn.css';
import './SharedDiagram.css';

const ZOOM_MIN = 0.2;
const ZOOM_MAX = 4;

const SharedDiagram = () => {
    const { token } = useParams();
    const navigate = useNavigate();
    const { user } = useAuth();
    const canvasRef = useRef(null);
    const viewerRef = useRef(null);
    const openTimerRef = useRef(null);
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [opening, setOpening] = useState(false);
    const [reloadKey, setReloadKey] = useState(0);

    const reload = useCallback(() => setReloadKey((key) => key + 1), []);

    useEffect(() => {
        let active = true;
        setLoading(true);
        setError(null);
        setData(null);
        apiClient.get(`/api/share/${token}`)
            .then((response) => { if (active) setData(response.data); })
            .catch((requestError) => {
                if (active) {
                    setError({
                        headline: 'Ссылка не работает',
                        lead: 'Проверьте ссылку или попросите автора схемы отправить новую.',
                        title: 'Схема недоступна',
                        description: toUserMessage(requestError, 'Ссылка недействительна или устарела.'),
                        retry: true,
                    });
                }
            })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [token, reloadKey]);

    useEffect(() => {
        if (!data || !canvasRef.current) return undefined;

        if (!data.xml_content || !data.xml_content.trim()) {
            setError({
                headline: 'Схема не открылась',
                lead: 'Ссылка рабочая, но в диаграмме ничего не записано.',
                title: 'В схеме нет содержимого',
                description: 'Автор прислал ссылку на пустую диаграмму. Запросите новую ссылку.',
                retry: false,
            });
            return undefined;
        }

        let destroyed = false;
        const viewer = new BpmnViewer({ container: canvasRef.current });
        viewerRef.current = viewer;
        viewer.importXML(data.xml_content)
            .then(() => {
                if (destroyed) return;
                viewer.get('canvas').zoom('fit-viewport');
            })
            .catch((importError) => {
                console.error('Ошибка импорта схемы:', importError);
                if (!destroyed) {
                    setError({
                        headline: 'Схема не открылась',
                        lead: 'Ссылка рабочая, но файл диаграммы не читается.',
                        title: 'Схему не удалось отобразить',
                        description: 'Файл повреждён или читается не полностью. Повторите загрузку — если не поможет, попросите автора выслать новую ссылку.',
                        retry: true,
                    });
                }
            });

        return () => {
            destroyed = true;
            viewerRef.current = null;
            viewer.destroy();
        };
    }, [data]);

    useEffect(() => () => clearTimeout(openTimerRef.current), []);

    const zoomBy = (factor) => {
        const viewer = viewerRef.current;
        if (!viewer) return;
        const canvas = viewer.get('canvas');
        canvas.zoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, canvas.zoom() * factor)));
    };

    const zoomFit = () => viewerRef.current?.get('canvas').zoom('fit-viewport');

    const openInEditor = () => {
        if (!user) {
            navigate('/login', { state: { from: `/share/${token}` } });
            return;
        }
        const xml = data.xml_content;
        const name = data.name;
        setOpening(true);
        /* Разрешаем кнопке показать занятое состояние до того, как
           тяжёлый монтирование редактора заберёт поток на себя. */
        openTimerRef.current = setTimeout(() => {
            navigate('/editor', { state: { bpmnXML: xml, name: `${name} (копия)` } });
        }, 80);
    };

    if (loading) {
        return (
            <WorkPage eyebrow="общий доступ" title="Общая схема">
                <PageLoader label="Загружаем схему…" />
            </WorkPage>
        );
    }

    if (error) {
        return (
            <WorkPage
                eyebrow="общий доступ"
                title={error.headline}
                description={error.lead}
            >
                <EmptyState
                    title={error.title}
                    description={error.description}
                    action={
                        <>
                            {error.retry && <Button variant="secondary" size="md" onClick={reload}>Повторить загрузку</Button>}
                            <Button variant="primary" size="md" to="/">На главную</Button>
                        </>
                    }
                />
            </WorkPage>
        );
    }

    return (
        <WorkPage
            eyebrow="общий доступ"
            title={data.name || 'Опубликованная схема'}
            description={data.can_edit
                ? 'Схема доступна для просмотра. Кнопкой выше можно открыть её копию в редакторе — менять вы будете именно копию, а не оригинал.'
                : 'Схема только для чтения: её можно приблизить, отдалить и перемещать, но нельзя изменить.'}
            action={data.can_edit && (
                <Button
                    variant="primary"
                    size="md"
                    onClick={openInEditor}
                    disabled={opening}
                    aria-busy={opening || undefined}
                >
                    {opening ? 'Открываем копию…' : 'Открыть копию в редакторе'}
                </Button>
            )}
        >
            <div className="shared-diagram__toolbar" role="group" aria-label="Масштаб схемы">
                <Button variant="secondary" size="sm" aria-label="Приблизить схему" onClick={() => zoomBy(1.25)}>+</Button>
                <Button variant="secondary" size="sm" aria-label="Отдалить схему" onClick={() => zoomBy(0.8)}>−</Button>
                <Button variant="secondary" size="sm" aria-label="Вписать схему в окно" onClick={zoomFit}>Вписать</Button>
            </div>
            <div ref={canvasRef} className="shared-diagram__canvas" />
            <p className="shared-diagram__hint">
                Колесо мыши — масштаб, перетаскивание — перемещение. Кнопки выше работают с клавиатуры.
            </p>
        </WorkPage>
    );
};

export default SharedDiagram;

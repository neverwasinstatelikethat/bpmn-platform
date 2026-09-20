import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import BpmnViewer from 'bpmn-js/lib/NavigatedViewer';
import { useAuth } from './context/AuthContext';
import { apiClient, toUserMessage } from './api/client';
import { WorkPage, PageLoader, EmptyState } from './components/layout';
import 'bpmn-js/dist/assets/diagram-js.css';
import 'bpmn-js/dist/assets/bpmn-font/css/bpmn.css';
import './SharedDiagram.css';

const SharedDiagram = () => {
    const { token } = useParams();
    const navigate = useNavigate();
    const { user } = useAuth();
    const canvasRef = useRef(null);
    const [data, setData] = useState(null);
    const [error, setError] = useState('');

    useEffect(() => {
        let active = true;
        apiClient.get(`/api/share/${token}`)
            .then((response) => { if (active) setData(response.data); })
            .catch((requestError) => {
                if (active) setError(toUserMessage(requestError, 'Ссылка недействительна или устарела.'));
            });
        return () => { active = false; };
    }, [token]);

    useEffect(() => {
        if (!data || !canvasRef.current) return undefined;
        const viewer = new BpmnViewer({ container: canvasRef.current });
        viewer.importXML(data.xml_content)
            .then(() => viewer.get('canvas').zoom('fit-viewport'))
            .catch((importError) => {
                console.error('Ошибка импорта схемы:', importError);
                setError('Не удалось отобразить схему.');
            });
        return () => viewer.destroy();
    }, [data]);

    const openInEditor = () => {
        if (!user) {
            navigate('/login', { state: { from: `/share/${token}` } });
            return;
        }
        navigate('/editor', { state: { bpmnXML: data.xml_content, name: `${data.name} (копия)` } });
    };

    if (error) {
        return (
            <WorkPage eyebrow="общий доступ" title="Ссылка не работает" description="Проверьте ссылку или попросите автора схемы отправить новую.">
                <EmptyState title="Схема недоступна" description={error} actionLabel="На главную" actionTo="/" />
            </WorkPage>
        );
    }

    if (!data) {
        return <PageLoader label="Загружаем схему…" />;
    }

    return (
        <WorkPage
            eyebrow="общий доступ"
            title={data.name || "Опубликованная схема"}
            description={data.can_edit ? "Схему можно посмотреть здесь или открыть редактируемую копию в редакторе." : "Схема доступна только для просмотра — масштаб и перетаскивание работают."}
            action={data.can_edit && <button type="button" className="ui-btn ui-btn--primary ui-btn--md" onClick={openInEditor}>Открыть копию в редакторе</button>}
        >
            <div ref={canvasRef} className="shared-diagram__canvas" />
        </WorkPage>
    );
};

export default SharedDiagram;

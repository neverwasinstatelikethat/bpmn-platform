import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { WorkPage } from './components/layout';
import EmptyState from './components/layout/EmptyState';
import PageLoader from './components/layout/PageLoader';
import InteractiveFolder from './components/registry/InteractiveFolder';
import { registryApi } from './api/registry';
import { toUserMessage } from './api/client';
import './MySchemas.css';

const flattenFolders = (folders = []) => folders.flatMap((folder) => [folder, ...flattenFolders(folder.children)]);
const formatDate = (value) => value ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value)) : '—';
const scoreTone = (score = 0) => score >= 80 ? 'good' : score >= 50 ? 'warn' : 'risk';

const MySchemas = () => {
    const navigate = useNavigate();
    const fileInput = useRef(null);
    const [mode, setMode] = useState('personal');
    const [tab, setTab] = useState('registry');
    const [diagrams, setDiagrams] = useState([]);
    const [deleted, setDeleted] = useState([]);
    const [folders, setFolders] = useState([]);
    const [teams, setTeams] = useState([]);
    const [selectedTeamId, setSelectedTeamId] = useState(null);
    const [teamDiagrams, setTeamDiagrams] = useState([]);
    const [selectedFolder, setSelectedFolder] = useState(null);
    const [query, setQuery] = useState('');
    const [newFolderName, setNewFolderName] = useState('');
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [pendingDelete, setPendingDelete] = useState(null);

    const loadRegistry = useCallback(async () => {
        setLoading(true); setError('');
        try {
            const [diagramResult, treeResult, deletedResult, teamResult] = await Promise.all([
                registryApi.getDiagrams(), registryApi.getTree(), registryApi.getDeleted(), registryApi.getTeams(),
            ]);
            setDiagrams(diagramResult.data || []);
            setFolders(treeResult.data || []);
            setDeleted(deletedResult.data || []);
            const teamList = teamResult.data || [];
            setTeams(teamList);
            setSelectedTeamId((current) => (current && teamList.some((team) => team.id === current)) ? current : (teamList[0]?.id || null));
        } catch (requestError) {
            setError(toUserMessage(requestError, 'Не удалось загрузить реестр.'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { loadRegistry(); }, [loadRegistry]);

    useEffect(() => {
        if (mode !== 'team' || !selectedTeamId) { setTeamDiagrams([]); return undefined; }
        let active = true;
        setLoading(true);
        registryApi.getTeamDiagrams(selectedTeamId)
            .then(({ data }) => { if (active) setTeamDiagrams(data || []); })
            .catch((requestError) => { if (active) setError(toUserMessage(requestError, 'Не удалось загрузить командные схемы.')); })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [mode, selectedTeamId]);

    const isTeam = mode === 'team';
    const personalFolders = useMemo(() => flattenFolders(folders).filter((folder) => !folder.team_id), [folders]);
    const teamFolders = useMemo(() => folders.filter((folder) => folder.team_id === selectedTeamId), [folders, selectedTeamId]);
    const scopedFolders = isTeam ? teamFolders : personalFolders;
    const scopedFolderIds = useMemo(() => new Set(flattenFolders(scopedFolders).map((folder) => folder.id)), [scopedFolders]);
    const source = tab === 'trash' ? deleted : isTeam ? teamDiagrams : diagrams;
    const shownDiagrams = useMemo(() => source.filter((diagram) => {
        const matchesQuery = diagram.name?.toLowerCase().includes(query.trim().toLowerCase());
        return matchesQuery && (!selectedFolder || diagram.folder_id === selectedFolder);
    }), [source, query, selectedFolder]);

    const createFolder = async (event) => {
        event.preventDefault();
        if (!newFolderName.trim()) return;
        try {
            await registryApi.createFolder({
                name: newFolderName.trim(),
                parentId: scopedFolderIds.has(selectedFolder) ? selectedFolder : null,
                teamId: isTeam ? selectedTeamId : null,
            });
            setNewFolderName(''); setNotice('Папка создана.'); await loadRegistry();
        } catch (requestError) { setError(toUserMessage(requestError, 'Не удалось создать папку.')); }
    };

    const importDiagram = async (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const formData = new FormData(); formData.append('file', file);
        try { await registryApi.importDiagram(formData); setNotice('Схема импортирована.'); await loadRegistry(); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось импортировать схему.')); }
        finally { event.target.value = ''; }
    };

    const confirmDelete = async () => {
        if (!pendingDelete) return;
        try {
            if (pendingDelete.kind === 'folder') await registryApi.deleteFolder(pendingDelete.id);
            else await registryApi.deleteDiagram(pendingDelete.id);
            setNotice(pendingDelete.kind === 'folder' ? 'Папка удалена, схемы сохранены.' : 'Схема перемещена в корзину.');
            setPendingDelete(null); await loadRegistry();
        } catch (requestError) { setError(toUserMessage(requestError, 'Не удалось удалить объект.')); }
    };

    const moveDiagram = async (diagramId, folderId) => {
        try { await registryApi.moveDiagram({ diagramId, folderId: folderId || null }); setNotice('Схема перемещена.'); await loadRegistry(); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось переместить схему.')); }
    };

    const restoreDiagram = async (diagramId) => {
        try { await registryApi.restoreDiagram(diagramId); setNotice('Схема восстановлена.'); await loadRegistry(); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось восстановить схему.')); }
    };

    const shareDiagram = async (diagramId, teamId) => {
        try { await registryApi.shareToTeam({ diagramId, teamId }); setNotice('Схема открыта команде.'); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось открыть схему команде.')); }
    };

    if (loading) return <PageLoader label="Собираем реестр…" />;

    const folderActions = tab === 'registry' && (isTeam ? !!selectedTeamId : true);

    return <WorkPage title="Реестр схем" eyebrow="рабочее пространство" description="Собирайте процессы в понятные папки и возвращайтесь к работе с любого шага." action={<div className="registry-actions"><button className="ui-btn ui-btn--secondary ui-btn--md" onClick={() => fileInput.current?.click()}>Импортировать</button><button className="ui-btn ui-btn--primary ui-btn--md" onClick={() => navigate('/editor')}>Новая схема</button></div>}>
        <input ref={fileInput} className="sr-only" type="file" accept=".bpmn,.xml" onChange={importDiagram} />
        {error && <div className="registry-alert registry-alert--error" role="alert">{error}<button onClick={() => setError('')} aria-label="Закрыть">×</button></div>}
        {notice && <div className="registry-alert registry-alert--success" role="status">{notice}<button onClick={() => setNotice('')} aria-label="Закрыть">×</button></div>}

        <div className="registry-toolbar">
            <div className="registry-segment" role="group" aria-label="Источник схем"><button className={!isTeam ? 'is-active' : ''} onClick={() => { setMode('personal'); setSelectedFolder(null); }}>Мои схемы</button><button className={isTeam ? 'is-active' : ''} onClick={() => { setMode('team'); setSelectedFolder(null); }}>Команда</button></div>
            {isTeam && teams.length > 0 && <div className="registry-segment" role="group" aria-label="Выбор команды">{teams.map((team) => <button key={team.id} className={selectedTeamId === team.id ? 'is-active' : ''} onClick={() => { setSelectedTeamId(team.id); setSelectedFolder(null); }}>{team.name}</button>)}</div>}
            <div className="registry-segment" role="group" aria-label="Раздел реестра"><button className={tab === 'registry' ? 'is-active' : ''} onClick={() => setTab('registry')}>Реестр</button><button className={tab === 'trash' ? 'is-active' : ''} onClick={() => { setTab('trash'); setMode('personal'); }}>Корзина</button></div>
            <label className="registry-search">Поиск <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Название схемы" /></label>
        </div>

        {isTeam && !teams.length && <EmptyState title="Команд пока нет" description="Создайте команду или примите приглашение — общие схемы и папки появятся здесь." actionLabel="Открыть профиль" actionTo="/profile" />}

        {folderActions && <section className="registry-folders" aria-label="Папки схем">
            <button className={`registry-folder registry-folder--all ${!selectedFolder ? 'is-selected' : ''}`} onClick={() => setSelectedFolder(null)}><span>Все схемы</span><small>{source.length} в работе</small></button>
            {scopedFolders.map((folder) => <InteractiveFolder key={folder.id} name={folder.name} selected={selectedFolder === folder.id} diagrams={source.filter((diagram) => diagram.folder_id === folder.id)} onSelect={() => setSelectedFolder(folder.id)}><div className="registry-folder__links">{(folder.children || []).map((child) => <button key={child.id} onClick={() => setSelectedFolder(child.id)}>{child.name}</button>)}{!isTeam && <button className="registry-folder__delete" onClick={() => setPendingDelete({ kind: 'folder', id: folder.id, name: folder.name })}>Удалить папку</button>}</div></InteractiveFolder>)}
            <form className="registry-new-folder" onSubmit={createFolder}><label htmlFor="new-folder">Новая папка</label><input id="new-folder" value={newFolderName} onChange={(event) => setNewFolderName(event.target.value)} placeholder={isTeam ? 'Папка команды' : selectedFolder ? 'Вложенная папка' : 'Например, закупки'} /><button type="submit">+</button></form>
        </section>}

        {folderActions && (shownDiagrams.length ? <section className="registry-table-wrap"><table className="registry-table"><thead><tr><th>Схема</th><th>Обновлено</th><th>Качество</th><th>Папка</th><th><span className="sr-only">Действия</span></th></tr></thead><tbody>{shownDiagrams.map((diagram) => <tr key={diagram.id}><td><button className="registry-diagram-link" onClick={() => navigate(`/editor?load=${diagram.id}`)}>{diagram.name || 'Без названия'}</button></td><td>{formatDate(diagram.updated_at || diagram.created_at)}</td><td><span className={`registry-score registry-score--${scoreTone(diagram.score)}`}>{diagram.score ?? 0}/100</span></td><td>{flattenFolders(scopedFolders).find((folder) => folder.id === diagram.folder_id)?.name || 'Без папки'}</td><td className="registry-row-actions">{tab === 'trash' ? <button onClick={() => restoreDiagram(diagram.id)}>Восстановить</button> : <><select aria-label={`Переместить ${diagram.name}`} value={scopedFolderIds.has(diagram.folder_id) ? diagram.folder_id : ''} onChange={(event) => moveDiagram(diagram.id, event.target.value)}><option value="">Без папки</option>{flattenFolders(scopedFolders).map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select>{!isTeam && <select aria-label={`Открыть ${diagram.name} команде`} value="" onChange={(event) => event.target.value && shareDiagram(diagram.id, event.target.value)}><option value="">В команду…</option>{teams.map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}</select>}{!isTeam && <button className="is-danger" onClick={() => setPendingDelete({ kind: 'diagram', id: diagram.id, name: diagram.name })}>Удалить</button>}</>}</td></tr>)}</tbody></table></section> : <EmptyState title={tab === 'trash' ? 'Корзина пуста' : isTeam ? 'В команде пока нет схем' : 'Здесь пока нет схем'} description={tab === 'trash' ? 'Удалённые схемы появятся здесь, чтобы их можно было восстановить.' : isTeam ? 'Коллеги ещё не открыли схемы этой команды.' : 'Создайте первую схему или импортируйте уже готовую BPMN-модель.'} actionLabel={tab === 'trash' ? undefined : isTeam ? undefined : 'Открыть редактор'} actionTo={tab === 'trash' || isTeam ? undefined : '/editor'} />)}

        {pendingDelete && <div className="registry-dialog-backdrop" role="presentation"><section className="registry-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title"><h2 id="delete-title">Удалить «{pendingDelete.name}»?</h2><p>{pendingDelete.kind === 'folder' ? 'Сами схемы останутся в реестре без папки.' : 'Схема попадёт в корзину, откуда её можно восстановить.'}</p><div><button className="ui-btn ui-btn--secondary ui-btn--md" onClick={() => setPendingDelete(null)}>Отмена</button><button className="ui-btn ui-btn--primary ui-btn--md" onClick={confirmDelete}>Удалить</button></div></section></div>}
    </WorkPage>;
};

export default MySchemas;

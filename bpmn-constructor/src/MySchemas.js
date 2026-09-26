import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { WorkPage } from './components/layout';
import EmptyState from './components/layout/EmptyState';
import PageLoader from './components/layout/PageLoader';
import InteractiveFolder from './components/registry/InteractiveFolder';
import { Button, Input, Modal, ScoreBadge, Select } from './components/ui';
import { registryApi } from './api/registry';
import { toUserMessage } from './api/client';
import './MySchemas.css';

const flattenFolders = (folders = []) => folders.flatMap((folder) => [folder, ...flattenFolders(folder.children)]);

// В личном режиме список папок уже плоский (`personalFolders`), но вложенные объекты
// несут `children` — повторное раскрытие дублирует вложенные папки. Поэтому список
// скопа собирается один раз и с дедупликацией по id.
const uniqueFolders = (folders) => {
    const byId = new Map();
    for (const folder of flattenFolders(folders)) if (!byId.has(folder.id)) byId.set(folder.id, folder);
    return [...byId.values()];
};

const formatDate = (value) => value
    ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value))
    : '—';

/** Переключатель нескольких взаимоисключающих вариантов: состояние в aria-pressed. */
const Segments = ({ label, value, options, onChange }) => (
    <div className="registry-segment" role="group" aria-label={label}>
        {options.map((option) => (
            <Button
                key={option.value}
                size="sm"
                variant={value === option.value ? 'primary' : 'ghost'}
                aria-pressed={value === option.value}
                onClick={() => onChange(option.value)}
            >
                {option.label}
            </Button>
        ))}
    </div>
);

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
    // `loading` — только первичная сборка реестра. Мутации её не взводят: экран
    // не должен гаснуть на каждое перемещение, удаление или импорт.
    const [loading, setLoading] = useState(true);
    const [teamLoading, setTeamLoading] = useState(false);
    // Занятость конкретной строки: { key, label }.
    const [pending, setPending] = useState(null);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [pendingDelete, setPendingDelete] = useState(null);

    const loadRegistry = useCallback(async ({ silent = false } = {}) => {
        if (!silent) setLoading(true);
        setError('');
        try {
            const [diagramResult, treeResult, deletedResult, teamResult] = await Promise.all([
                registryApi.getDiagrams(),
                registryApi.getTree(),
                registryApi.getDeleted(),
                registryApi.getTeams(),
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
            if (!silent) setLoading(false);
        }
    }, []);

    useEffect(() => { loadRegistry(); }, [loadRegistry]);

    useEffect(() => {
        if (mode !== 'team' || !selectedTeamId) {
            setTeamDiagrams([]);
            return undefined;
        }
        let active = true;
        setTeamLoading(true);
        registryApi.getTeamDiagrams(selectedTeamId)
            .then(({ data }) => { if (active) setTeamDiagrams(data || []); })
            .catch((requestError) => { if (active) setError(toUserMessage(requestError, 'Не удалось загрузить командные схемы.')); })
            .finally(() => { if (active) setTeamLoading(false); });
        return () => { active = false; };
    }, [mode, selectedTeamId]);

    const isTeam = mode === 'team';
    const personalFolders = useMemo(() => flattenFolders(folders).filter((folder) => !folder.team_id), [folders]);
    const teamFolders = useMemo(() => folders.filter((folder) => folder.team_id === selectedTeamId), [folders, selectedTeamId]);
    const scopedFolders = isTeam ? teamFolders : personalFolders;
    const foldersInScope = useMemo(() => uniqueFolders(scopedFolders), [scopedFolders]);
    const scopedFolderIds = useMemo(() => new Set(foldersInScope.map((folder) => folder.id)), [foldersInScope]);
    const folderOptions = useMemo(
        () => [{ value: '', label: 'Без папки' }, ...foldersInScope.map((folder) => ({ value: folder.id, label: folder.name }))],
        [foldersInScope],
    );
    const folderNameById = useMemo(() => new Map(foldersInScope.map((folder) => [folder.id, folder.name])), [foldersInScope]);

    const source = tab === 'trash' ? deleted : isTeam ? teamDiagrams : diagrams;
    const shownDiagrams = useMemo(() => source.filter((diagram) => {
        const matchesQuery = diagram.name?.toLowerCase().includes(query.trim().toLowerCase());
        return matchesQuery && (!selectedFolder || diagram.folder_id === selectedFolder);
    }), [source, query, selectedFolder]);

    /**
     * Общий прогон мутации: запрос → `notice` → тихая перезагрузка данных.
     * Экран при этом не перезагружается визуально, занятость видна на строке.
     */
    const runMutation = useCallback(async ({ key, label, request, success, failure }) => {
        setPending({ key, label });
        setError('');
        try {
            await request();
            setNotice(success);
            await loadRegistry({ silent: true });
            return true;
        } catch (requestError) {
            setError(toUserMessage(requestError, failure));
            return false;
        } finally {
            setPending(null);
        }
    }, [loadRegistry]);

    const createFolder = async (event) => {
        event.preventDefault();
        const name = newFolderName.trim();
        if (!name) return;
        const created = await runMutation({
            key: 'new-folder',
            label: 'Создаём папку…',
            request: () => registryApi.createFolder({
                name,
                parentId: scopedFolderIds.has(selectedFolder) ? selectedFolder : null,
                teamId: isTeam ? selectedTeamId : null,
            }),
            success: 'Папка создана.',
            failure: 'Не удалось создать папку.',
        });
        if (created) setNewFolderName('');
    };

    const importDiagram = async (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        await runMutation({
            key: 'import',
            label: 'Импортируем схему…',
            request: () => registryApi.importDiagram(formData),
            success: 'Схема импортирована.',
            failure: 'Не удалось импортировать схему.',
        });
        event.target.value = '';
    };

    const closeDeleteDialog = useCallback(() => setPendingDelete(null), []);

    const confirmDelete = async () => {
        if (!pendingDelete) return;
        const { kind, id } = pendingDelete;
        const isFolder = kind === 'folder';
        const removed = await runMutation({
            key: `${kind}:${id}`,
            label: 'Удаляем…',
            request: () => (isFolder ? registryApi.deleteFolder(id) : registryApi.deleteDiagram(id)),
            success: isFolder ? 'Папка удалена, схемы сохранены.' : 'Схема перемещена в корзину.',
            failure: 'Не удалось удалить объект.',
        });
        if (removed) closeDeleteDialog();
    };

    const moveDiagram = (diagramId, folderId) => runMutation({
        key: `diagram:${diagramId}`,
        label: 'Перемещаем…',
        request: () => registryApi.moveDiagram({ diagramId, folderId: folderId || null }),
        success: 'Схема перемещена.',
        failure: 'Не удалось переместить схему.',
    });

    const restoreDiagram = (diagramId) => runMutation({
        key: `diagram:${diagramId}`,
        label: 'Восстанавливаем…',
        request: () => registryApi.restoreDiagram(diagramId),
        success: 'Схема восстановлена.',
        failure: 'Не удалось восстановить схему.',
    });

    // Как и остальные мутации: после успеха данные перезагружаются тихонько,
    // иначе список командных схем остаётся устаревшим до первой смены режима.
    const shareDiagram = (diagramId, teamId) => runMutation({
        key: `diagram:${diagramId}`,
        label: 'Открываем команде…',
        request: () => registryApi.shareToTeam({ diagramId, teamId }),
        success: 'Схема открыта команде.',
        failure: 'Не удалось открыть схему команде.',
    });

    if (loading) return <PageLoader label="Собираем реестр…" />;

    const folderActions = tab === 'registry' && (isTeam ? !!selectedTeamId : true);
    // Корзина — тот же список схем, только удалённые: папки в ней не показываем,
    // но сам список и пустое состояние обязаны рендериться (иначе вкладка пуста).
    const showDiagramList = tab === 'trash' || folderActions;
    const rowPending = (diagram) => pending && pending.key === `diagram:${diagram.id}` ? pending.label : null;

    return (
        <WorkPage
            title="Реестр схем"
            eyebrow="рабочее пространство"
            description="Собирайте процессы в понятные папки и возвращайтесь к работе с любого шага."
            action={(
                <div className="registry-actions">
                    <Button variant="secondary" size="md" onClick={() => fileInput.current?.click()} disabled={pending?.key === 'import'}>
                        {pending?.key === 'import' ? pending.label : 'Импортировать'}
                    </Button>
                    <Button variant="primary" size="md" onClick={() => navigate('/editor')}>Новая схема</Button>
                </div>
            )}
        >
            <input ref={fileInput} className="sr-only" type="file" accept=".bpmn,.xml" onChange={importDiagram} aria-label="Файл BPMN для импорта" />

            {error && (
                <div className="registry-alert registry-alert--error" role="alert">
                    <span>{error}</span>
                    <Button className="registry-alert__close" variant="ghost" size="sm" aria-label="Закрыть сообщение об ошибке" onClick={() => setError('')}>×</Button>
                </div>
            )}
            {notice && (
                <div className="registry-alert registry-alert--success" role="status">
                    <span>{notice}</span>
                    <Button className="registry-alert__close" variant="ghost" size="sm" aria-label="Закрыть сообщение о действии" onClick={() => setNotice('')}>×</Button>
                </div>
            )}

            <div className="registry-toolbar">
                <Segments
                    label="Источник схем"
                    value={mode}
                    options={[{ value: 'personal', label: 'Мои схемы' }, { value: 'team', label: 'Команда' }]}
                    onChange={(next) => { setMode(next); setSelectedFolder(null); }}
                />
                {isTeam && !!teams.length && (
                    <Segments
                        label="Выбор команды"
                        value={selectedTeamId}
                        options={teams.map((team) => ({ value: team.id, label: team.name }))}
                        onChange={(id) => { setSelectedTeamId(id); setSelectedFolder(null); }}
                    />
                )}
                <Segments
                    label="Раздел реестра"
                    value={tab}
                    options={[{ value: 'registry', label: 'Реестр' }, { value: 'trash', label: 'Корзина' }]}
                    onChange={(next) => { setTab(next); if (next === 'trash') setMode('personal'); }}
                />
                <div className="registry-search">
                    <Input
                        id="registry-query"
                        label="Поиск"
                        type="search"
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder="Название схемы"
                    />
                </div>
            </div>

            {isTeam && !teams.length && (
                <EmptyState
                    title="Команд пока нет"
                    description="Создайте команду или примите приглашение — общие схемы и папки появятся здесь."
                    actionLabel="Открыть профиль"
                    actionTo="/profile"
                />
            )}

            {folderActions && (
                <section className="registry-folders" aria-label="Папки схем">
                    <button
                        type="button"
                        className={`registry-folder-tile${!selectedFolder ? ' is-selected' : ''}`}
                        aria-pressed={!selectedFolder}
                        onClick={() => setSelectedFolder(null)}
                    >
                        <span>Все схемы</span>
                        <small>{source.length} в работе</small>
                    </button>

                    {scopedFolders.map((folder) => (
                        <InteractiveFolder
                            key={folder.id}
                            name={folder.name}
                            selected={selectedFolder === folder.id}
                            diagrams={source.filter((diagram) => diagram.folder_id === folder.id)}
                            onSelect={() => setSelectedFolder(folder.id)}
                            onOpenDiagram={(diagramId) => navigate(`/editor?load=${diagramId}`)}
                        >
                            <div className="registry-folder__links">
                                {(folder.children || []).map((child) => (
                                    <Button key={child.id} variant="ghost" size="sm" onClick={() => setSelectedFolder(child.id)}>{child.name}</Button>
                                ))}
                                {!isTeam && (
                                    <Button
                                        className="registry-folder__delete"
                                        variant="danger"
                                        size="sm"
                                        onClick={() => setPendingDelete({ kind: 'folder', id: folder.id, name: folder.name })}
                                    >
                                        Удалить папку
                                    </Button>
                                )}
                            </div>
                        </InteractiveFolder>
                    ))}

                    <form className="registry-new-folder" onSubmit={createFolder}>
                        <Input
                            id="new-folder"
                            label="Новая папка"
                            value={newFolderName}
                            onChange={(event) => setNewFolderName(event.target.value)}
                            placeholder={isTeam ? 'Папка команды' : selectedFolder ? 'Вложенная папка' : 'Например, закупки'}
                        />
                        <Button type="submit" variant="primary" size="sm" disabled={pending?.key === 'new-folder'}>
                            {pending?.key === 'new-folder' ? 'Создаём…' : 'Создать'}
                        </Button>
                    </form>
                </section>
            )}

            {showDiagramList && (shownDiagrams.length ? (
                <section className="registry-table-wrap" aria-busy={teamLoading ? 'true' : undefined}>
                    <table className="registry-table">
                        {/* Действия — фиксированная колонка: три контрола по 44 px в одну
                            строку, иначе строка таблицы вырастает вдвое. */}
                        <colgroup>
                            <col />
                            <col className="registry-col--updated" />
                            <col className="registry-col--score" />
                            <col className="registry-col--folder" />
                            <col className="registry-col--actions" />
                        </colgroup>
                        <thead>
                            <tr>
                                <th scope="col">Схема</th>
                                <th scope="col">Обновлено</th>
                                <th scope="col">Качество</th>
                                <th scope="col">Папка</th>
                                <th scope="col"><span className="sr-only">Действия</span></th>
                            </tr>
                        </thead>
                        <tbody>
                            {shownDiagrams.map((diagram) => {
                                const busyLabel = rowPending(diagram);
                                return (
                                    <tr key={diagram.id} aria-busy={busyLabel ? 'true' : undefined}>
                                        <td className="registry-cell registry-cell--name">
                                            <Button className="registry-diagram-link" variant="ghost" size="sm" to={`/editor?load=${diagram.id}`}>
                                                {diagram.name || 'Без названия'}
                                            </Button>
                                            {busyLabel && <span className="registry-cell__busy">{busyLabel}</span>}
                                        </td>
                                        <td className="registry-cell registry-cell--updated">{formatDate(diagram.updated_at || diagram.created_at)}</td>
                                        <td className="registry-cell registry-cell--score"><ScoreBadge score={diagram.score} /></td>
                                        <td className="registry-cell registry-cell--folder" title={folderNameById.get(diagram.folder_id)}>{folderNameById.get(diagram.folder_id) || 'Без папки'}</td>
                                        <td className="registry-cell registry-cell--actions">
                                            <div className="registry-row-actions">
                                                {tab === 'trash' ? (
                                                    <Button variant="secondary" size="sm" disabled={!!busyLabel} onClick={() => restoreDiagram(diagram.id)}>Восстановить</Button>
                                                ) : (
                                                    <>
                                                        <Select
                                                            className="registry-row-select"
                                                            aria-label={`Переместить ${diagram.name} в папку`}
                                                            value={scopedFolderIds.has(diagram.folder_id) ? diagram.folder_id : ''}
                                                            disabled={!!busyLabel}
                                                            options={folderOptions}
                                                            onChange={(event) => moveDiagram(diagram.id, event.target.value)}
                                                        />
                                                        {!isTeam && (
                                                            <Select
                                                                className="registry-row-select"
                                                                aria-label={`Открыть ${diagram.name} команде`}
                                                                value=""
                                                                disabled={!!busyLabel}
                                                                options={[{ value: '', label: 'В команду…' }, ...teams.map((team) => ({ value: team.id, label: team.name }))]}
                                                                onChange={(event) => event.target.value && shareDiagram(diagram.id, event.target.value)}
                                                            />
                                                        )}
                                                        {/* Командную схему отсюда не удаляют: право на удаление — у владельца. */}
                                                        {!isTeam && (
                                                            <Button variant="danger" size="sm" disabled={!!busyLabel} onClick={() => setPendingDelete({ kind: 'diagram', id: diagram.id, name: diagram.name })}>Удалить</Button>
                                                        )}
                                                    </>
                                                )}
                                            </div>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </section>
            ) : (
                <EmptyState
                    title={tab === 'trash' ? 'Корзина пуста' : isTeam ? 'В команде пока нет схем' : 'Здесь пока нет схем'}
                    description={tab === 'trash'
                        ? 'Удалённые схемы появятся здесь, чтобы их можно было восстановить.'
                        : isTeam ? 'Коллеги ещё не открыли схемы этой команды.' : 'Создайте первую схему или импортируйте уже готовую BPMN-модель.'}
                    actionLabel={tab === 'trash' || isTeam ? undefined : 'Открыть редактор'}
                    actionTo={tab === 'trash' || isTeam ? undefined : '/editor'}
                />
            ))}

            {pendingDelete && (
                <Modal
                    title={`Удалить «${pendingDelete.name}»?`}
                    description={pendingDelete.kind === 'folder'
                        ? 'Сами схемы останутся в реестре без папки.'
                        : 'Схема попадёт в корзину, откуда её можно восстановить.'}
                    onClose={closeDeleteDialog}
                    actions={(
                        <>
                            <Button variant="secondary" size="md" onClick={closeDeleteDialog}>Отмена</Button>
                            <Button variant="danger" size="md" disabled={!!pending} onClick={confirmDelete}>
                                {pending?.key === `${pendingDelete.kind}:${pendingDelete.id}` ? pending.label : 'Удалить'}
                            </Button>
                        </>
                    )}
                />
            )}
        </WorkPage>
    );
};

export default MySchemas;

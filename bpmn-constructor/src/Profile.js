import { useCallback, useEffect, useState } from 'react';
import { useAuth } from './context/AuthContext';
import WorkPage from './components/layout/WorkPage';
import PageLoader from './components/layout/PageLoader';
import { Badge, Button, Input, Modal, Select, Textarea } from './components/ui';
import { roleLabel } from './i18n/labels';
import { registryApi } from './api/registry';
import { toUserMessage } from './api/client';
import './Profile.css';

/* Марки цвета команды. Значения — hex тех же токенов, что лежат в
   src/styles/tokens.css: колонка Team.color в БД хранит именно hex
   (String(7)), поэтому список держим константой в модуле и не читаем
   токены через getComputedStyle. Сам кружок свотча при этом красится
   через var(--token), то есть цвет на экране всегда взят из токена. */
const TEAM_MARKS = [
    { value: '#00A550', token: '--color-primary', name: 'Зелёный' },
    { value: '#F62369', token: '--color-berry', name: 'Ягода' },
    { value: '#E6B800', token: '--color-warn', name: 'Янтарь' },
    { value: '#54B3FF', token: '--color-info', name: 'Синий' },
    { value: '#7C3AED', token: '--color-violet', name: 'Фиолет' },
    { value: '#292524', token: '--color-ink', name: 'Графит' },
];

/* Границы полей взяты из колонок БД (app/models.py), а не выдуманы:
   Team.name — String(100), User.name — String(50), остальные строки — String(100).
   Поле «о себе» в модели — Text, лимита у него нет, счётчик не ставим. */
const TEAM_NAME_MAX = 100;
const USER_NAME_MAX = 50;
const USER_TEXT_MAX = 100;

const formatDate = (value) => (value
    ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(value))
    : 'дата не указана');

const memberCountLabel = (count) => {
    const tenth = count % 10;
    const hundredth = count % 100;
    const word = tenth === 1 && hundredth !== 11 ? 'участник'
        : tenth >= 2 && tenth <= 4 && (hundredth < 12 || hundredth > 14) ? 'участника'
            : 'участников';
    return `${count} ${word}`;
};

/** Черновик формы профиля всегда собирается из сохранённых данных. */
const draftFrom = (user) => ({
    name: user?.name || '',
    email: user?.email || '',
    position: user?.position || '',
    company: user?.company || '',
    website: user?.website || '',
    about: user?.about || '',
});

/** Одно активное сообщение на страницу: его можно закрыть, и оно не копится. */
const ProfileMessage = ({ message, onClose }) => (message && (
    <p className={`profile-alert profile-alert--${message.tone}`} role={message.tone === 'error' ? 'alert' : 'status'}>
        <span>{message.text}</span>
        <button type="button" className="profile-alert__close" onClick={onClose} aria-label="Закрыть сообщение">×</button>
    </p>
));

const Profile = () => {
    const { user, updateUserProfile } = useAuth();
    const [editing, setEditing] = useState(false);
    const [saving, setSaving] = useState(false);
    const [teams, setTeams] = useState([]);
    const [loadingTeams, setLoadingTeams] = useState(true);
    const [message, setMessage] = useState(null);
    const [form, setForm] = useState(() => draftFrom(user));

    const [roles, setRoles] = useState([]);
    const [panel, setPanel] = useState(null);
    const [members, setMembers] = useState({});
    const [loadingMembers, setLoadingMembers] = useState(false);
    const [teamName, setTeamName] = useState('');
    const [teamColor, setTeamColor] = useState(TEAM_MARKS[0].value);
    const [inviteEmail, setInviteEmail] = useState('');
    const [inviteRole, setInviteRole] = useState('');
    const [inviteLink, setInviteLink] = useState('');
    const [inviteSentTo, setInviteSentTo] = useState('');
    const [linkCopied, setLinkCopied] = useState(false);
    const [linkRevealed, setLinkRevealed] = useState(false);
    const [creating, setCreating] = useState(false);
    const [inviting, setInviting] = useState(false);

    const fail = useCallback((text) => setMessage({ tone: 'error', text }), []);
    const done = useCallback((text) => setMessage({ tone: 'success', text }), []);

    /* Состав команд берётся из того же состояния, что и список участников в
       панели: подпись на строке списка и панель не могут разойтись. */
    const loadTeams = useCallback(async () => {
        const { data } = await registryApi.getTeams();
        const list = data || [];
        setTeams(list);
        const counts = await Promise.all(list.map(async (team) => {
            try {
                const crew = (await registryApi.getTeamMembers(team.id)).data || [];
                return [team.id, crew];
            } catch {
                return null;
            }
        }));
        setMembers((current) => ({ ...current, ...Object.fromEntries(counts.filter(Boolean)) }));
    }, []);

    useEffect(() => {
        let active = true;
        loadTeams()
            .catch((requestError) => { if (active) fail(toUserMessage(requestError, 'Не удалось загрузить команды.')); })
            .finally(() => { if (active) setLoadingTeams(false); });
        registryApi.getRoles().then(({ data }) => {
            if (!active) return;
            const names = (data || []).map((role) => role.name);
            setRoles(names);
            setInviteRole(names[0] || '');
        }).catch(() => { if (active) fail('Не удалось загрузить список ролей — приглашение сейчас отправить нельзя.'); });
        return () => { active = false; };
    }, [loadTeams, fail]);

    const save = async (event) => {
        event.preventDefault();
        setSaving(true);
        setMessage(null);
        try {
            await updateUserProfile(form);
            setEditing(false);
            done('Профиль обновлён.');
        } catch (requestError) {
            fail(toUserMessage(requestError, 'Не удалось обновить профиль.'));
        } finally {
            setSaving(false);
        }
    };

    const loadMembers = useCallback(async (teamId) => {
        setLoadingMembers(true);
        try {
            const { data } = await registryApi.getTeamMembers(teamId);
            setMembers((current) => ({ ...current, [teamId]: data || [] }));
        } catch (requestError) {
            fail(toUserMessage(requestError, 'Не удалось загрузить участников.'));
        } finally {
            setLoadingMembers(false);
        }
    }, [fail]);

    /* Повторный клик по тому же действию закрывает панель, а открытие каждый
       раз тянет состав с сервера: кэш не должен переживать приглашение, после
       которого участников уже больше. */
    const openPanel = (teamId, type) => {
        setMessage(null);
        setLinkCopied(false);
        setLinkRevealed(false);
        if (panel?.teamId === teamId && panel.type === type) {
            setPanel(null);
            return;
        }
        setInviteEmail('');
        setInviteLink('');
        setInviteSentTo('');
        setPanel({ teamId, type });
        if (type === 'members') loadMembers(teamId);
    };

    const closePanel = () => {
        setPanel(null);
        setMessage(null);
    };

    const copyLink = async () => {
        try {
            await navigator.clipboard.writeText(inviteLink);
            setLinkCopied(true);
            setLinkRevealed(false);
        } catch {
            setLinkCopied(false);
            setLinkRevealed(true);
            fail('Браузер не разрешил копирование. Адрес приглашения показан ниже — выделите его и скопируйте вручную.');
        }
    };

    const createTeam = async (event) => {
        event.preventDefault();
        const name = teamName.trim();
        if (!name) return;
        setCreating(true);
        setMessage(null);
        try {
            await registryApi.createTeam({ name, color: teamColor });
            setTeamName('');
            await loadTeams();
            done(`Команда «${name}» создана.`);
        } catch (requestError) {
            fail(toUserMessage(requestError, 'Не удалось создать команду.'));
        } finally {
            setCreating(false);
        }
    };

    const submitInvite = async (event) => {
        event.preventDefault();
        const email = inviteEmail.trim().toLowerCase();
        if (!panel || !email || !inviteRole) return;
        setInviting(true);
        setMessage(null);
        setInviteLink('');
        setInviteSentTo('');
        setLinkCopied(false);
        setLinkRevealed(false);
        try {
            const { data } = await registryApi.inviteMember(panel.teamId, { email, role: inviteRole });
            setInviteEmail('');
            setInviteSentTo(email);
            setInviteLink(data.accept_link || '');
            if (!data.accept_link) done(`Приглашение для ${email} создано, но ссылку получить не удалось.`);
        } catch (requestError) {
            fail(toUserMessage(requestError, 'Не удалось пригласить участника.'));
        } finally {
            setInviting(false);
        }
    };

    const panelTeam = panel ? teams.find((team) => team.id === panel.teamId) : null;
    const panelMembers = panel ? members[panel.teamId] : null;
    const messageStrip = <ProfileMessage message={message} onClose={() => setMessage(null)} />;

    return (
        <WorkPage
            title="Профиль"
            eyebrow="ваше пространство"
            description="Личные данные, рабочий контекст и команды, с которыми вы строите процессы."
            action={!editing && <Button variant="primary" size="md" onClick={() => { setMessage(null); setEditing(true); }}>Редактировать профиль</Button>}
        >
            {!panelTeam && messageStrip}

            <section className="profile-person" aria-label="Ваши данные">
                <header className="profile-person__head">
                    <div className="profile-avatar" aria-hidden="true">{(user?.name || user?.email || 'В').slice(0, 1).toUpperCase()}</div>
                    <div className="profile-person__who">
                        <h2>{user?.name || 'Имя не указано'}</h2>
                        <p>{user?.email}</p>
                    </div>
                </header>

                {editing ? (
                    <form className="profile-form" onSubmit={save}>
                        <Input id="profile-name" label="Имя" value={form.name} maxLength={USER_NAME_MAX}
                            onChange={(event) => setForm({ ...form, name: event.target.value })} />
                        <Input id="profile-email" label="Рабочая почта" type="email" value={form.email} maxLength={USER_TEXT_MAX}
                            onChange={(event) => setForm({ ...form, email: event.target.value })} />
                        <Input id="profile-position" label="Должность" value={form.position} maxLength={USER_TEXT_MAX}
                            onChange={(event) => setForm({ ...form, position: event.target.value })} />
                        <Input id="profile-company" label="Компания" value={form.company} maxLength={USER_TEXT_MAX}
                            onChange={(event) => setForm({ ...form, company: event.target.value })} />
                        <Input id="profile-website" label="Сайт" value={form.website} maxLength={USER_TEXT_MAX}
                            onChange={(event) => setForm({ ...form, website: event.target.value })} />
                        <div className="profile-form__about">
                            <Textarea id="profile-about" label="О себе" value={form.about}
                                onChange={(event) => setForm({ ...form, about: event.target.value })} />
                        </div>
                        <div className="profile-form__actions">
                            <Button variant="secondary" size="md" onClick={() => { setEditing(false); setMessage(null); setForm(draftFrom(user)); }}>Отмена</Button>
                            <Button variant="primary" size="md" type="submit" disabled={saving}>{saving ? 'Сохраняем…' : 'Сохранить изменения'}</Button>
                        </div>
                    </form>
                ) : (
                    <dl className="profile-details">
                        <div><dt>Должность</dt><dd>{form.position || 'Не указана'}</dd></div>
                        <div><dt>Компания</dt><dd>{form.company || 'Не указана'}</dd></div>
                        <div><dt>Сайт</dt><dd>{form.website || 'Не указан'}</dd></div>
                        <div className="profile-details__wide"><dt>О себе</dt><dd>{form.about || 'Пока не заполнено — расскажите коллегам о своей роли в процессах.'}</dd></div>
                    </dl>
                )}
            </section>

            <section className="profile-teams" aria-label="Команды">
                <p className="work-page__eyebrow">сотрудничество</p>
                <h2>Команды</h2>

                {loadingTeams && <PageLoader label="Загружаем команды…" />}
                {!loadingTeams && !teams.length && (
                    <p className="profile-empty">Вы пока не состоите в командах. Создайте первую ниже — или примите приглашение от коллег.</p>
                )}
                {!loadingTeams && teams.length > 0 && (
                    <ul className="profile-team-list">
                        {teams.map((team) => (
                            <li key={team.id} className="profile-team">
                                <span className="profile-team__mark" style={{ background: team.color || 'var(--color-primary)' }} aria-hidden="true" />
                                <div className="profile-team__body">
                                    <h3>{team.name}</h3>
                                    <p className="profile-team__meta">
                                        <span>Создана {formatDate(team.created_at)}</span>
                                        <span className="profile-team__count">{team.id in members ? memberCountLabel(members[team.id].length) : '—'}</span>
                                    </p>
                                </div>
                                <div className="profile-team__actions">
                                    <Button variant="ghost" size="md" onClick={() => openPanel(team.id, 'members')}>Участники</Button>
                                    <Button variant="ghost" size="md" onClick={() => openPanel(team.id, 'invite')}>Пригласить</Button>
                                </div>
                            </li>
                        ))}
                    </ul>
                )}

                <form className="profile-create" onSubmit={createTeam}>
                    <h3>Новая команда</h3>
                    <div className="profile-create__fields">
                        <Input id="new-team-name" label="Название команды" value={teamName} maxLength={TEAM_NAME_MAX}
                            placeholder="Например, закупки" onChange={(event) => setTeamName(event.target.value)} required />
                        <div className="profile-create__marks">
                            <span className="ui-field__label" id="team-marks-label">Марка цвета</span>
                            <div className="profile-marks" role="radiogroup" aria-labelledby="team-marks-label">
                                {TEAM_MARKS.map((mark) => (
                                    <label key={mark.value} className={`profile-mark${teamColor === mark.value ? ' is-selected' : ''}`}>
                                        <input className="profile-mark__input" type="radio" name="team-color" value={mark.value}
                                            checked={teamColor === mark.value} onChange={() => setTeamColor(mark.value)} />
                                        <span className="profile-mark__dot" style={{ background: `var(${mark.token})` }} aria-hidden="true" />
                                        <span className="profile-mark__name">{mark.name}</span>
                                        {teamColor === mark.value && <span className="profile-mark__check" aria-hidden="true">✓</span>}
                                    </label>
                                ))}
                            </div>
                        </div>
                    </div>
                    <Button variant="primary" size="md" type="submit" disabled={creating || !teamName.trim()}>{creating ? 'Создаём…' : 'Создать команду'}</Button>
                </form>
            </section>

            {panelTeam && panel.type === 'members' && (
                <Modal
                    title="Участники"
                    description={`Команда «${panelTeam.name}» — состав загружается заново при каждом открытии.`}
                    onClose={closePanel}
                    actions={<Button variant="secondary" size="md" onClick={closePanel}>Закрыть</Button>}
                >
                    {messageStrip}
                    {loadingMembers && <p className="profile-loading" role="status">Загружаем участников…</p>}
                    {!loadingMembers && !!panelMembers?.length && (
                        <ul className="profile-members">
                            {panelMembers.map((member) => (
                                <li key={member.id} className="profile-member">
                                    <span className="profile-member__who">
                                        <strong>{member.user_name || 'Имя не указано'}</strong>
                                        <span>{member.user_email}</span>
                                    </span>
                                    <Badge tone="sage">{roleLabel(member.role)}</Badge>
                                </li>
                            ))}
                        </ul>
                    )}
                    {!loadingMembers && panelMembers && !panelMembers.length && (
                        <p className="profile-empty">Участники не пришли с сервера. Закройте окно и откройте «Участники» снова.</p>
                    )}
                </Modal>
            )}

            {panelTeam && panel.type === 'invite' && (
                <Modal
                    title="Пригласить в команду"
                    description={`Коллега присоединится к «${panelTeam.name}» по ссылке-приглашению.`}
                    onClose={closePanel}
                >
                    {messageStrip}
                    <form className="profile-invite" onSubmit={submitInvite}>
                        <Input id="invite-email" label="Рабочая почта коллеги" type="email" value={inviteEmail}
                            onChange={(event) => setInviteEmail(event.target.value)} required />
                        <p className="profile-hint">Приглашение привязано к адресу, под которым коллега войдёт на платформу.</p>
                        <Select id="invite-role" label="Роль в команде" value={inviteRole} onChange={(event) => setInviteRole(event.target.value)} required
                            options={roles.map((role) => ({ value: role, label: roleLabel(role) }))} />
                        <div className="profile-invite__actions">
                            <Button variant="secondary" size="md" onClick={closePanel}>Отмена</Button>
                            <Button variant="primary" size="md" type="submit" disabled={inviting || !inviteEmail.trim() || !inviteRole}>
                                {inviting ? 'Отправляем…' : 'Отправить приглашение'}
                            </Button>
                        </div>
                    </form>
                    {!!inviteSentTo && (
                        <div className="profile-invite__result">
                            <p>Приглашение для <strong>{inviteSentTo}</strong> создано.</p>
                            {!!inviteLink && (
                                <Button variant="secondary" size="md" onClick={copyLink}>
                                    {linkCopied ? 'Ссылка скопирована' : 'Скопировать ссылку'}
                                </Button>
                            )}
                            {linkRevealed && !!inviteLink && (
                                <p className="profile-invite__address">
                                    <span>Адрес приглашения</span>
                                    <code>{inviteLink}</code>
                                </p>
                            )}
                        </div>
                    )}
                </Modal>
            )}
        </WorkPage>
    );
};

export default Profile;

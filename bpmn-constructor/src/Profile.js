import { useEffect, useState } from 'react';
import { useAuth } from './context/AuthContext';
import WorkPage from './components/layout/WorkPage';
import PageLoader from './components/layout/PageLoader';
import { registryApi } from './api/registry';
import { toUserMessage } from './api/client';
import './Profile.css';

const Profile = () => {
    const { user, updateUserProfile } = useAuth();
    const [editing, setEditing] = useState(false);
    const [saving, setSaving] = useState(false);
    const [teams, setTeams] = useState([]);
    const [loadingTeams, setLoadingTeams] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [form, setForm] = useState({ name: user?.name || '', email: user?.email || '', position: user?.position || '', company: user?.company || '', website: user?.website || '', about: user?.about || '' });

    const [roles, setRoles] = useState([]);
    const [panel, setPanel] = useState(null);
    const [members, setMembers] = useState({});
    const [teamName, setTeamName] = useState('');
    const [teamColor, setTeamColor] = useState(() =>
        getComputedStyle(document.documentElement).getPropertyValue('--color-primary').trim());
    const [inviteEmail, setInviteEmail] = useState('');
    const [inviteRole, setInviteRole] = useState('');
    const [inviteLink, setInviteLink] = useState('');
    const [teamBusy, setTeamBusy] = useState(false);

    useEffect(() => {
        let active = true;
        registryApi.getTeams().then(({ data }) => { if (active) setTeams(data || []); })
            .catch(() => { if (active) setError('Не удалось загрузить команды.'); })
            .finally(() => { if (active) setLoadingTeams(false); });
        registryApi.getRoles().then(({ data }) => {
            if (!active) return;
            const names = (data || []).map((role) => role.name);
            setRoles(names);
            setInviteRole(names[0] || '');
        }).catch(() => { if (active) setError('Не удалось загрузить список ролей.'); });
        return () => { active = false; };
    }, []);

    const save = async (event) => {
        event.preventDefault(); setSaving(true); setError('');
        try { await updateUserProfile(form); setEditing(false); setNotice('Профиль обновлён.'); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось обновить профиль.')); }
        finally { setSaving(false); }
    };

    const loadTeams = async () => {
        try { setTeams((await registryApi.getTeams()).data || []); }
        catch (requestError) { setError(toUserMessage(requestError, 'Не удалось обновить список команд.')); }
    };

    const createTeam = async (event) => {
        event.preventDefault();
        if (!teamName.trim()) return;
        setTeamBusy(true); setError(''); setNotice('');
        try {
            await registryApi.createTeam({ name: teamName.trim(), color: teamColor });
            setTeamName(''); setNotice('Команда создана.'); await loadTeams();
        } catch (requestError) { setError(toUserMessage(requestError, 'Не удалось создать команду.')); }
        finally { setTeamBusy(false); }
    };

    const openPanel = async (teamId, type) => {
        setError(''); setInviteLink('');
        setPanel(panel?.teamId === teamId && panel.type === type ? null : { teamId, type });
        if (type === 'members' && !members[teamId]) {
            try {
                const { data } = await registryApi.getTeamMembers(teamId);
                setMembers((current) => ({ ...current, [teamId]: data || [] }));
            }
            catch (requestError) { setError(toUserMessage(requestError, 'Не удалось загрузить участников.')); }
        }
    };

    const copyLink = async () => {
        try { await navigator.clipboard.writeText(inviteLink); setNotice('Ссылка-приглашение скопирована.'); }
        catch { setNotice(inviteLink); }
    };

    const submitInvite = async (event) => {
        event.preventDefault();
        if (!panel || !inviteEmail.trim() || !inviteRole) return;
        setTeamBusy(true); setError(''); setInviteLink('');
        try {
            const { data } = await registryApi.inviteMember(panel.teamId, { email: inviteEmail.trim().toLowerCase(), role: inviteRole });
            setInviteLink(data.accept_link || 'Приглашение создано.'); setInviteEmail(''); setNotice('Приглашение отправлено.');
        } catch (requestError) { setError(toUserMessage(requestError, 'Не удалось пригласить участника.')); }
        finally { setTeamBusy(false); }
    };

    return <WorkPage title="Профиль" eyebrow="ваше пространство" description="Личные данные, рабочий контекст и команды, с которыми вы строите процессы." action={!editing && <button className="ui-btn ui-btn--primary ui-btn--md" onClick={() => setEditing(true)}>Редактировать профиль</button>}>
        {error && <p className="profile-alert profile-alert--error" role="alert">{error}</p>}{notice && <p className="profile-alert profile-alert--success" role="status">{notice}</p>}
        <div className="profile-grid"><section className="profile-card profile-card--identity"><div className="profile-avatar">{(user?.name || user?.email || 'В').slice(0, 1).toUpperCase()}</div><div><h2>{user?.name || 'Участник команды'}</h2><p>{user?.email}</p>{form.position && <span>{form.position}</span>}</div></section><section className="profile-card"><h2>О вас</h2>{editing ? <form className="profile-form" onSubmit={save}>{[['name','Имя'], ['email','Рабочая почта'], ['position','Должность'], ['company','Компания'], ['website','Сайт']].map(([key,label]) => <label key={key}>{label}<input type={key === 'email' ? 'email' : 'text'} value={form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.value })} /></label>)}<label>О себе<textarea value={form.about} onChange={(event) => setForm({ ...form, about: event.target.value })} /></label><div className="profile-form__actions"><button className="ui-btn ui-btn--secondary ui-btn--md" type="button" onClick={() => setEditing(false)}>Отмена</button><button className="ui-btn ui-btn--primary ui-btn--md" disabled={saving}>{saving ? 'Сохраняем…' : 'Сохранить изменения'}</button></div></form> : <dl className="profile-details"><div><dt>Должность</dt><dd>{form.position || 'Не указана'}</dd></div><div><dt>Компания</dt><dd>{form.company || 'ВкусВилл'}</dd></div><div><dt>Сайт</dt><dd>{form.website || 'Не указан'}</dd></div><div><dt>О себе</dt><dd>{form.about || 'Добавьте несколько слов о своей роли в процессах.'}</dd></div></dl>}</section></div>
        <section className="profile-teams">
            <div><p className="work-page__eyebrow">сотрудничество</p><h2>Команды</h2></div>
            {loadingTeams ? <PageLoader label="Загружаем команды…" /> : teams.length ? <div className="profile-team-list">{teams.map((team) => <article key={team.id} className="profile-team profile-team--managed"><span style={{ background: team.color || 'var(--color-primary)' }} /><div className="profile-team__body"><h3>{team.name}</h3><p>{team.description || 'Рабочая команда по процессам'}</p><div className="profile-team__actions"><button type="button" className="ui-btn ui-btn--secondary ui-btn--sm" onClick={() => openPanel(team.id, 'members')}>Участники</button><button type="button" className="ui-btn ui-btn--secondary ui-btn--sm" onClick={() => openPanel(team.id, 'invite')}>Пригласить</button></div>
                {panel?.teamId === team.id && panel.type === 'members' && <ul className="profile-members">{(members[team.id] || []).map((member) => <li key={member.id}><strong>{member.user_name}</strong> · {member.user_email} · <em>{member.role}</em></li>)}{members[team.id]?.length === 0 && <li>Пока только вы.</li>}</ul>}
                {panel?.teamId === team.id && panel.type === 'invite' && <form className="profile-invite" onSubmit={submitInvite}><input type="email" placeholder="colleague@vkusvill.ru" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} required /><select value={inviteRole} onChange={(event) => setInviteRole(event.target.value)} required>{roles.map((role) => <option key={role} value={role}>{role}</option>)}</select><button className="ui-btn ui-btn--primary ui-btn--sm" type="submit" disabled={teamBusy}>{teamBusy ? 'Отправляем…' : 'Пригласить'}</button>{inviteLink && <p className="profile-invite__link">Ссылка для принятия: <code>{inviteLink}</code> <button type="button" onClick={copyLink}>Скопировать</button></p>}</form>}
            </div></article>)}</div> : <p className="profile-empty">Вы пока не состоите в командах. Создайте первую — или примите приглашение.</p>}
            <form className="profile-create-team" onSubmit={createTeam}><label>Название команды<input value={teamName} onChange={(event) => setTeamName(event.target.value)} placeholder="Например, закупки" required /></label><label>Цвет<input type="color" value={teamColor} onChange={(event) => setTeamColor(event.target.value)} /></label><button className="ui-btn ui-btn--primary ui-btn--md" type="submit" disabled={teamBusy}>{teamBusy ? 'Создаём…' : 'Создать команду'}</button></form>
        </section>
    </WorkPage>;
};

export default Profile;

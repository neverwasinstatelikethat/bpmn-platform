import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import './Register.css';

const Register = () => {
    const { register } = useAuth();
    const [searchParams] = useSearchParams();
    const [name, setName] = useState('');
    const [email, setEmail] = useState(() => searchParams.get('email') || '');
    const [password, setPassword] = useState('');
    const [confirmation, setConfirmation] = useState('');
    const [consent, setConsent] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const submit = async (event) => {
        event.preventDefault();
        if (password !== confirmation) {
            setError('Пароли не совпадают.');
            return;
        }
        if (!consent) {
            setError('Подтвердите согласие с условиями использования.');
            return;
        }
        setBusy(true); setError('');
        try {
            await register(name.trim(), email.trim(), password);
        } catch (requestError) {
            setError(requestError.message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <main className="auth-screen">
            <section className="auth-panel" aria-labelledby="register-title">
                <p className="auth-panel__eyebrow">первый шаг</p>
                <h1 id="register-title">Соберём процессы<br />без лишнего</h1>
                <p className="auth-panel__lead">Создайте рабочее пространство — первая BPMN‑схема появится уже через несколько минут.</p>
                {error && <p className="auth-alert auth-alert--error" role="alert">{error}</p>}
                <form className="auth-form" onSubmit={submit}>
                    <label htmlFor="register-name">Имя</label>
                    <input id="register-name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} required />
                    <label htmlFor="register-email">Рабочая почта</label>
                    <input id="register-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <label htmlFor="register-password">Пароль</label>
                    <input id="register-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} minLength="6" required />
                    <label htmlFor="register-confirmation">Повторите пароль</label>
                    <input id="register-confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={(e) => setConfirmation(e.target.value)} minLength="6" required />
                    <label className="auth-consent"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />Я принимаю условия использования</label>
                    <button className="auth-submit" type="submit" disabled={busy}>{busy ? 'Создаём…' : 'Создать аккаунт'}</button>
                </form>
                <p className="auth-panel__footer">Уже есть аккаунт? <Link to="/login">Войти</Link></p>
            </section>
            <aside className="auth-aside auth-aside--berry" aria-hidden="true">
                <small>01 / пространство</small>
                <p>Процесс<br /><span>становится</span><br />видимым.</p>
                <em>Описывайте работу человеческим языком — остальное соберём в понятную схему.</em>
            </aside>
        </main>
    );
};

export default Register;

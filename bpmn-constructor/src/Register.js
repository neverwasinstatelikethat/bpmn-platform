import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button, Input } from './components/ui';
import { useAuth } from './context/AuthContext';
import './Register.css';

// Сообщение об ошибке уже разобрано в AuthContext через toUserMessage.
const failureText = (error, fallback) => (error?.message?.trim() ? error.message : fallback);

const Register = () => {
    const { register } = useAuth();
    const [searchParams] = useSearchParams();
    const [name, setName] = useState('');
    const [email, setEmail] = useState(() => searchParams.get('email') || '');
    const [password, setPassword] = useState('');
    const [confirmation, setConfirmation] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const submit = async (event) => {
        event.preventDefault();
        if (password !== confirmation) {
            // Введённое сохраняем: человек правит одно поле, а не всю форму.
            setError('Пароли не совпадают.');
            return;
        }
        setBusy(true); setError('');
        try {
            await register(name.trim(), email.trim(), password);
        } catch (requestError) {
            setError(failureText(requestError, 'Не удалось создать аккаунт.'));
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
                    <Input label="Имя" id="register-name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} required />
                    <Input label="Рабочая почта" id="register-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <Input label="Пароль" id="register-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} minLength="6" required />
                    <Input label="Повторите пароль" id="register-confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={(e) => setConfirmation(e.target.value)} minLength="6" required />
                    <Button type="submit" block disabled={busy}>{busy ? 'Создаём…' : 'Создать аккаунт'}</Button>
                </form>
                <p className="auth-panel__footer">Уже есть аккаунт? <Link to="/login">Войти</Link></p>
            </section>
            <aside className="auth-aside auth-aside--berry" aria-hidden="true">
                <div className="auth-scene">
                    <small>01 / пространство</small>
                    <p>Процесс <br /><span>становится</span> <br />видимым.</p>
                    <em>Описывайте работу человеческим языком — остальное соберём в понятную схему.</em>
                </div>
            </aside>
        </main>
    );
};

export default Register;

import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import './Login.css';

const Login = () => {
    const { login, resetPasswordRequest, resetPassword } = useAuth();
    const location = useLocation();
    const navigate = useNavigate();
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [resetToken, setResetToken] = useState('');
    const [mode, setMode] = useState('login');
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');

    useEffect(() => {
        const token = new URLSearchParams(window.location.search).get('token');
        if (token) {
            setResetToken(token);
            setMode('reset');
        }
    }, []);

    const submitLogin = async (event) => {
        event.preventDefault();
        setBusy(true); setError('');
        try {
            await login(email.trim(), password);
            const returnPath = location.state?.from;
            if (typeof returnPath === 'string' && returnPath.startsWith('/')) navigate(returnPath, { replace: true });
        } catch (requestError) {
            setError(requestError.message);
        } finally {
            setBusy(false);
        }
    };

    const submitRecovery = async (event) => {
        event.preventDefault();
        setBusy(true); setError(''); setMessage('');
        try {
            await resetPasswordRequest(email.trim());
            setMessage('Если адрес зарегистрирован, мы отправили инструкции для восстановления.');
        } catch (requestError) {
            setError(requestError.message);
        } finally {
            setBusy(false);
        }
    };

    const submitReset = async (event) => {
        event.preventDefault();
        if (newPassword !== confirmPassword) {
            setError('Пароли не совпадают.');
            return;
        }
        setBusy(true); setError('');
        try {
            await resetPassword(resetToken, newPassword);
            setMessage('Пароль обновлён. Теперь можно войти.');
            setMode('login');
            setPassword(''); setNewPassword(''); setConfirmPassword('');
        } catch (requestError) {
            setError(requestError.message);
        } finally {
            setBusy(false);
        }
    };

    const isLogin = mode === 'login';
    const isRecovery = mode === 'recovery';

    return (
        <main className="auth-screen">
            <section className="auth-panel" aria-labelledby="auth-title">
                <p className="auth-panel__eyebrow">рабочее пространство</p>
                <h1 id="auth-title">{isLogin ? 'Рады снова вас видеть' : isRecovery ? 'Восстановим доступ' : 'Новый пароль'}</h1>
                <p className="auth-panel__lead">{isLogin ? 'Продолжайте работать со схемами и командой.' : 'Мы бережно проведём вас к следующему шагу.'}</p>

                {error && <p className="auth-alert auth-alert--error" role="alert">{error}</p>}
                {message && <p className="auth-alert auth-alert--success" role="status">{message}</p>}

                {isLogin && <form className="auth-form" onSubmit={submitLogin}>
                    <label htmlFor="login-email">Рабочая почта</label>
                    <input id="login-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <label htmlFor="login-password">Пароль</label>
                    <input id="login-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
                    <button className="auth-submit" type="submit" disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button>
                    <button className="auth-text-button" type="button" onClick={() => { setMode('recovery'); setError(''); }}>Не помню пароль</button>
                </form>}

                {isRecovery && <form className="auth-form" onSubmit={submitRecovery}>
                    <label htmlFor="recovery-email">Рабочая почта</label>
                    <input id="recovery-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <button className="auth-submit" type="submit" disabled={busy}>{busy ? 'Отправляем…' : 'Получить инструкцию'}</button>
                    <button className="auth-text-button" type="button" onClick={() => setMode('login')}>Вернуться ко входу</button>
                </form>}

                {mode === 'reset' && <form className="auth-form" onSubmit={submitReset}>
                    <label htmlFor="new-password">Новый пароль</label>
                    <input id="new-password" type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required minLength="6" />
                    <label htmlFor="confirm-password">Повторите пароль</label>
                    <input id="confirm-password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} required minLength="6" />
                    <button className="auth-submit" type="submit" disabled={busy}>{busy ? 'Сохраняем…' : 'Обновить пароль'}</button>
                </form>}

                {isLogin && <p className="auth-panel__footer">Ещё нет аккаунта? <Link to="/register">Создать</Link></p>}
            </section>
            <aside className="auth-aside" aria-hidden="true">
                <small>ваше пространство</small>
                <p>Порядок<br />начинается<br /><span>с первого шага.</span></p>
                <em>Вернитесь к схемам, заметкам и работе с командой.</em>
            </aside>
        </main>
    );
};

export default Login;

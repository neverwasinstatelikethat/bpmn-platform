import { useEffect, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { Button, Input } from './components/ui';
import { useAuth } from './context/AuthContext';
import './Login.css';

// Куда вернуться после входа. Принимаем только внутренний путь без протокола:
// state.from приходит из ProtectedRoute и не должен превращаться в редирект наружу.
const returnPathFrom = (from) => (
    typeof from === 'string' && from.startsWith('/') && !from.startsWith('//') ? from : null
);

// AuthContext уже разбирает ответы бэкенда через toUserMessage, поэтому экран
// показывает причину, а не «Request failed with status code 400». Фолбэк —
// подстраховка на случай ошибки без текста.
const failureText = (error, fallback) => (error?.message?.trim() ? error.message : fallback);

const Login = () => {
    const { login, resetPasswordRequest, resetPassword, verifyResetToken } = useAuth();
    const location = useLocation();
    const [searchParams] = useSearchParams();
    const resetToken = searchParams.get('token') || '';
    const onResetRoute = location.pathname === '/reset-password';

    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [mode, setMode] = useState(onResetRoute || resetToken ? 'reset' : 'login');
    // Ссылка на восстановление может быть пустой, просроченной или уже
    // использованной — каждое состояние объясняем человеку, а не молча
    // показываем форму входа.
    const [tokenStatus, setTokenStatus] = useState(
        resetToken ? 'checking' : onResetRoute ? 'invalid' : 'idle'
    );
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');

    useEffect(() => {
        if (!resetToken) return undefined;
        let active = true;
        // verifyResetToken не бросает: false означает «ссылка не годится».
        verifyResetToken(resetToken).then((valid) => {
            if (active) setTokenStatus(valid ? 'valid' : 'invalid');
        });
        return () => { active = false; };
    }, [resetToken, verifyResetToken]);

    const submitLogin = async (event) => {
        event.preventDefault();
        setBusy(true); setError('');
        try {
            await login(email.trim(), password, { returnTo: returnPathFrom(location.state?.from) });
        } catch (requestError) {
            setError(failureText(requestError, 'Не удалось войти.'));
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
            setError(failureText(requestError, 'Не удалось отправить инструкцию.'));
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
            setError(failureText(requestError, 'Не удалось обновить пароль.'));
        } finally {
            setBusy(false);
        }
    };

    const isLogin = mode === 'login';
    const isRecovery = mode === 'recovery';
    const isReset = mode === 'reset';
    const linkBroken = isReset && tokenStatus === 'invalid';

    return (
        <main className="auth-screen">
            <section className="auth-panel" aria-labelledby="auth-title">
                <p className="auth-panel__eyebrow">рабочее пространство</p>
                <h1 id="auth-title">{linkBroken ? 'Ссылка не сработала' : isLogin ? 'Рады снова вас видеть' : isRecovery ? 'Восстановим доступ' : 'Новый пароль'}</h1>
                <p className="auth-panel__lead">{linkBroken ? 'Такое бывает со старым письмом — свежая ссылка приходит за минуту.' : isLogin ? 'Продолжайте работать со схемами и командой.' : 'Мы бережно проведём вас к следующему шагу.'}</p>

                {error && <p className="auth-alert auth-alert--error" role="alert">{error}</p>}
                {message && <p className="auth-alert auth-alert--success" role="status">{message}</p>}

                {isLogin && <form className="auth-form" onSubmit={submitLogin}>
                    <Input label="Рабочая почта" id="login-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <Input label="Пароль" id="login-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
                    <Button type="submit" block disabled={busy}>{busy ? 'Входим…' : 'Войти'}</Button>
                    <Button className="auth-form__link" variant="ghost" size="sm" onClick={() => { setMode('recovery'); setError(''); }}>Не помню пароль</Button>
                </form>}

                {isRecovery && <form className="auth-form" onSubmit={submitRecovery}>
                    <Input label="Рабочая почта" id="recovery-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    <Button type="submit" block disabled={busy}>{busy ? 'Отправляем…' : 'Получить инструкцию'}</Button>
                    <Button className="auth-form__link" variant="ghost" size="sm" onClick={() => { setMode('login'); setError(''); setMessage(''); }}>Вернуться ко входу</Button>
                </form>}

                {isReset && !linkBroken && <form className="auth-form" onSubmit={submitReset}>
                    <Input label="Новый пароль" id="new-password" type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required minLength="6" />
                    <Input label="Повторите пароль" id="confirm-password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} required minLength="6" />
                    {tokenStatus === 'checking' && <p className="auth-hint" role="status">Проверяем ссылку — это займёт секунду.</p>}
                    <Button type="submit" block disabled={busy || tokenStatus === 'checking'}>{busy ? 'Сохраняем…' : 'Обновить пароль'}</Button>
                </form>}

                {linkBroken && <div className="auth-state">
                    <p className="auth-alert auth-alert--error" role="alert">
                        {resetToken
                            ? 'Ссылка недействительна: она уже использована или устарела.'
                            : 'В ссылке не нашлось токена восстановления.'}
                    </p>
                    <p className="auth-hint">Запросите письмо ещё раз — пришлём свежую ссылку на рабочую почту.</p>
                    <Button block onClick={() => { setMode('recovery'); setError(''); setMessage(''); }}>Запросить новое письмо</Button>
                    <Button className="auth-form__link" variant="ghost" size="sm" onClick={() => { setMode('login'); setError(''); }}>Вернуться ко входу</Button>
                </div>}

                {isLogin && <p className="auth-panel__footer">Ещё нет аккаунта? <Link to="/register">Создать</Link></p>}
            </section>
            <aside className="auth-aside" aria-hidden="true">
                <div className="auth-scene">
                    <small>ваше пространство</small>
                    <p>Порядок <br />начинается <br /><span>с первого шага.</span></p>
                    <em>Вернитесь к схемам, заметкам и работе с командой.</em>
                </div>
            </aside>
        </main>
    );
};

export default Login;

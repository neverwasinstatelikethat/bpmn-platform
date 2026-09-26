import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { apiClient, toUserMessage } from './api/client';
import PageLoader from './components/layout/PageLoader';
import { Button } from './components/ui';
import './InviteAccept.css';

/* pending — запрос в полёте, accepted — приглашение принято и идёт переход
   в профиль, failed — отказ сервера. Экран не должен зависать ни на одном
   из них. Ссылка лежит под ProtectedRoute: без сессии сюда попадают не
   чаще, чем на любой другой защищённый маршрут, — вход возвращает обратно. */
const InviteAccept = () => {
    const { token } = useParams();
    const navigate = useNavigate();
    const [status, setStatus] = useState('pending');
    const [error, setError] = useState('');
    const redirectRef = useRef(null);
    const startedRef = useRef(null);

    const accept = useCallback(async () => {
        clearTimeout(redirectRef.current);
        setStatus('pending');
        setError('');
        try {
            await apiClient.get(`/api/invitations/accept/${token}`);
            setStatus('accepted');
            /* Подтверждение видно до перехода: таймер нужен ровно для этого
               кадра и снимается при размонтировании. */
            redirectRef.current = setTimeout(() => navigate('/profile'), 700);
        } catch (err) {
            setStatus('failed');
            setError(toUserMessage(err, 'Не удалось принять приглашение. Проверьте ссылку и попробуйте ещё раз.'));
        }
    }, [token, navigate]);

    useEffect(() => {
        /* Приглашение одноразовое: второй запрос того же токена сервер
           отвечает 404, а StrictMode снимает и ставит эффект дважды.
           На токен — один запрос; «Повторить» вызывается напрямую и
           стража не касается. */
        if (startedRef.current === token) return undefined;
        startedRef.current = token;
        accept();
        return () => clearTimeout(redirectRef.current);
    }, [token, accept]);

    if (status === 'failed') {
        return (
            <main className="invite-accept-container">
                <section className="invite-accept-card" role="alert">
                    <p className="invite-accept-card__eyebrow">приглашение в команду</p>
                    <h1>Не получилось принять приглашение</h1>
                    <p>{error}</p>
                    <div className="invite-accept-card__actions">
                        <Button variant="primary" size="md" onClick={accept}>Повторить</Button>
                        <Button variant="secondary" size="md" to="/my-schemas">К моим схемам</Button>
                    </div>
                </section>
            </main>
        );
    }

    if (status === 'accepted') {
        return (
            <main className="invite-accept-container">
                <section className="invite-accept-card" role="status">
                    <p className="invite-accept-card__eyebrow">приглашение в команду</p>
                    <h1>Приглашение принято</h1>
                    <p>Вы добавлены в команду. Открываем профиль…</p>
                    <div className="invite-accept-card__actions">
                        <Button variant="primary" size="md" to="/profile">Перейти в профиль сейчас</Button>
                        <Button variant="secondary" size="md" to="/my-schemas">К моим схемам</Button>
                    </div>
                </section>
            </main>
        );
    }

    return (
        <main className="invite-accept-container">
            <PageLoader label="Принимаем приглашение…" />
        </main>
    );
};

export default InviteAccept;

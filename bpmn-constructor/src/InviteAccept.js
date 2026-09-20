import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import { apiClient, toUserMessage } from './api/client';
import PageLoader from './components/layout/PageLoader';
import './InviteAccept.css';

const InviteAccept = () => {
    const { token } = useParams();
    const navigate = useNavigate();
    const { user } = useAuth();
    const [error, setError] = useState('');

    useEffect(() => {
        const acceptInvitation = async () => {
            try {
                await apiClient.get(`/api/invitations/accept/${token}`);
                navigate('/profile');
            } catch (err) {
                setError(toUserMessage(err, 'Не удалось принять приглашение. Проверьте ссылку и попробуйте ещё раз.'));
            }
        };
        if (user) acceptInvitation();
    }, [token, user, navigate]);

    return (
        <main className="invite-accept-container">
            {error ? (
                <section className="invite-accept-card" role="alert">
                    <p className="invite-accept-card__eyebrow">приглашение в команду</p>
                    <h1>Не получилось принять приглашение</h1>
                    <p>{error}</p>
                    <button type="button" className="ui-btn ui-btn--primary ui-btn--md" onClick={() => navigate('/profile')}>
                        Вернуться в профиль
                    </button>
                </section>
            ) : <PageLoader label="Принимаем приглашение…" />}
        </main>
    );
};

export default InviteAccept;

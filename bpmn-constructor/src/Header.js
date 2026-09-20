import { Link, useLocation } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { useAuth } from './context/AuthContext';
import { useEffect, useState } from 'react';
import { Button } from './components/ui';
import './Header.css';

const learningMenu = [
    { to: '/guideline', text: 'Руководство' },
    { to: '/errors', text: 'Частые ошибки BPMN' },
];

const Header = () => {
    const { user, logout } = useAuth();
    const [isLearningOpen, setIsLearningOpen] = useState(false);
    const [isMobileOpen, setIsMobileOpen] = useState(false);
    const location = useLocation();

    useEffect(() => {
        setIsMobileOpen(false);
        setIsLearningOpen(false);
    }, [location.pathname]);

    return (
        <header className="nav">
            <div className="nav__pill">
                <Link to="/" className="nav__brand">
                    <span className="nav__logo" aria-hidden="true">
                        <span className="nav__logo-dot" />
                    </span>
                    <span className="nav__brand-text">
                        <span className="nav__brand-name">ВкусВилл</span>
                        <span className="nav__brand-tag">Процессы</span>
                    </span>
                </Link>

                <nav className="nav__links" aria-label="Основная навигация">
                    {user ? (
                        <>
                            <NavLink to="/editor" text="Редактор" />
                            <div
                                className="nav__dropdown"
                                onMouseEnter={() => setIsLearningOpen(true)}
                                onMouseLeave={() => setIsLearningOpen(false)}
                            >
                                <span className="nav__link nav__link--static">Обучение</span>
                                <AnimatePresence>
                                    {isLearningOpen && (
                                        <motion.div
                                            className="nav__dropdown-menu"
                                            initial={{ opacity: 0, y: -8 }}
                                            animate={{ opacity: 1, y: 0 }}
                                            exit={{ opacity: 0, y: -8 }}
                                            transition={{ duration: 0.2 }}
                                        >
                                            {learningMenu.map((item) => (
                                                <NavLink key={item.to} to={item.to} text={item.text} />
                                            ))}
                                        </motion.div>
                                    )}
                                </AnimatePresence>
                            </div>
                            <NavLink to="/my-schemas" text="Мои схемы" />
                        </>
                    ) : (
                        <>
                            <NavLink to="/login" text="Вход" />
                        </>
                    )}
                </nav>

                <div className="nav__actions">
                    {user ? (
                        <>
                            <Link to="/profile" className="nav__avatar" title="Профиль">
                                {(user.name || user.username || 'П').charAt(0).toUpperCase()}
                            </Link>
                            <Button variant="dark" size="sm" onClick={logout}>Выйти</Button>
                        </>
                    ) : (
                        <Button variant="dark" size="sm" to="/register">Регистрация</Button>
                    )}
                    <button
                        type="button"
                        className={`nav__burger ${isMobileOpen ? 'nav__burger--open' : ''}`}
                        aria-expanded={isMobileOpen}
                        aria-label={isMobileOpen ? 'Закрыть меню' : 'Открыть меню'}
                        onClick={() => setIsMobileOpen((open) => !open)}
                    >
                        <span /><span />
                    </button>
                </div>
            </div>

            <AnimatePresence>
                {isMobileOpen && (
                    <motion.nav
                        className="nav__mobile"
                        aria-label="Мобильная навигация"
                        initial={{ opacity: 0, y: -10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -10 }}
                        transition={{ duration: 0.25, ease: [0.33, 1, 0.68, 1] }}
                    >
                        {user ? (
                            <>
                                <NavLink to="/editor" text="Редактор" />
                                {learningMenu.map((item) => (
                                    <NavLink key={item.to} to={item.to} text={item.text} />
                                ))}
                                <NavLink to="/my-schemas" text="Мои схемы" />
                                <NavLink to="/profile" text="Профиль" />
                                <Button variant="dark" size="sm" block onClick={logout}>Выйти</Button>
                            </>
                        ) : (
                            <>
                                <NavLink to="/login" text="Вход" />
                                <Button variant="dark" size="sm" block to="/register">Регистрация</Button>
                            </>
                        )}
                    </motion.nav>
                )}
            </AnimatePresence>
        </header>
    );
};

const NavLink = ({ to, text }) => (
    <Link to={to} className="nav__link">{text}</Link>
);

export default Header;

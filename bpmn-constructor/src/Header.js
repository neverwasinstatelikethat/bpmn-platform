import { Link, NavLink as RouterNavLink, useLocation } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { useAuth } from './context/AuthContext';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from './components/ui';
import './Header.css';

const learningMenu = [
    { to: '/guideline', text: 'Руководство' },
    { to: '/errors', text: 'Частые ошибки BPMN' },
];

/* Синхронно с @media (max-width: 860px) в Header.css: ниже брейкпоинта
   горизонтальные ссылки скрыты, и пункт «Обучение» живёт в панели бургера.
   Одно число в двух местах — при смене брейкпоинта правьте оба. */
const DESKTOP_QUERY = '(min-width: 861px)';
const LEARNING_MENU_ID = 'nav-learning-menu';
const MOBILE_MENU_ID = 'nav-mobile-menu';

/**
 * Локальный хук медиазапроса. Нужен, чтобы размонтировать панель бургера
 * при пересечении брейкпоинта: CSS скрывает ссылки, но не закрывает состояние.
 * Пока есть только один потребитель, общий useMediaQuery/Popover в ui-кит
 * не выносим.
 */
const useMediaQuery = (query) => {
    const read = () => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(query).matches : false);
    const [matches, setMatches] = useState(read);

    useEffect(() => {
        if (!window.matchMedia) return undefined;
        const media = window.matchMedia(query);
        const onChange = (event) => setMatches(event.matches);
        setMatches(media.matches);
        media.addEventListener('change', onChange);
        return () => media.removeEventListener('change', onChange);
    }, [query]);

    return matches;
};

/* Бургер виден только ниже брейкпоинта — вернуть фокус на него можно,
   пока он не скрыт. */
const isFocusable = (node) => Boolean(node) && node.offsetParent !== null;

const Header = () => {
    const { user, logout } = useAuth();
    const isDesktop = useMediaQuery(DESKTOP_QUERY);
    const [isLearningOpen, setIsLearningOpen] = useState(false);
    const [isMobileOpen, setIsMobileOpen] = useState(false);
    const location = useLocation();
    const navRef = useRef(null);
    const learningRef = useRef(null);
    const learningTriggerRef = useRef(null);
    const burgerRef = useRef(null);
    /* Чем открыта группа: наведением, кликом или клавиатурой. Наведение
       уступает клику — иначе курсор раскрывает меню, тот же клик его тут же
       закрывает, и мышью в меню не зайти. */
    const openSourceRef = useRef(null);
    /* Модальность последнего ввода: по фокусу группа раскрывается только для
       клавиатуры — мышиный фокус :focus-visible от клавиатурного не отличает. */
    const modalityRef = useRef('keyboard');
    const suppressFocusOpenRef = useRef(false);

    useEffect(() => {
        const onKey = () => { modalityRef.current = 'keyboard'; suppressFocusOpenRef.current = false; };
        const onPointer = () => { modalityRef.current = 'pointer'; suppressFocusOpenRef.current = false; };
        window.addEventListener('keydown', onKey, true);
        window.addEventListener('pointerdown', onPointer, true);
        return () => {
            window.removeEventListener('keydown', onKey, true);
            window.removeEventListener('pointerdown', onPointer, true);
        };
    }, []);

    const openLearning = useCallback((source) => {
        openSourceRef.current = source;
        setIsLearningOpen(true);
    }, []);

    const closeLearning = useCallback((restoreFocus = false) => {
        openSourceRef.current = null;
        setIsLearningOpen(false);
        if (restoreFocus) {
            /* Возврат фокуса неотличим от клавиатурного захода, поэтому
               раскрытие по фокусу гасим одним флагом. */
            suppressFocusOpenRef.current = true;
            learningTriggerRef.current?.focus();
        }
    }, []);

    const closeMobile = useCallback((restoreFocus = true) => {
        setIsMobileOpen(false);
        if (restoreFocus && isFocusable(burgerRef.current)) burgerRef.current.focus();
    }, []);

    useEffect(() => {
        setIsMobileOpen(false);
        closeLearning();
    }, [location.pathname, closeLearning]);

    /* Панель не должна переживать свой брейкпоинт: за 861 px она закрывается.
       Бургер там скрыт, поэтому фокус возвращается на него, только пока кнопка
       видима; иначе он уходит в начало документа, где уже лежат полноценные
       клавиатурные ссылки пилюли. */
    useEffect(() => {
        if (!isDesktop) return;
        const restoreFocus = isMobileOpen && isFocusable(burgerRef.current);
        setIsMobileOpen(false);
        closeLearning();
        if (restoreFocus) burgerRef.current.focus();
    }, [isDesktop, isMobileOpen, closeLearning]);

    /* Escape и клик вне закрывают то, что открыто; слушатели живут ровно
       столько, сколько открыто состояние. Группа «Обучение» — слой над
       панелью бургера: при двух открытых состояниях Escape закрывает её,
       а панель остаётся. */
    useEffect(() => {
        if (!isLearningOpen && !isMobileOpen) return undefined;

        const onKeyDown = (event) => {
            if (event.key !== 'Escape') return;
            if (isLearningOpen) closeLearning(true);
            else closeMobile();
        };
        const onPointerDown = (event) => {
            if (isLearningOpen && !learningRef.current?.contains(event.target)) closeLearning();
            if (isMobileOpen && !navRef.current?.contains(event.target)) closeMobile(false);
        };

        document.addEventListener('keydown', onKeyDown);
        document.addEventListener('pointerdown', onPointerDown, true);
        return () => {
            document.removeEventListener('keydown', onKeyDown);
            document.removeEventListener('pointerdown', onPointerDown, true);
        };
    }, [isLearningOpen, isMobileOpen, closeLearning, closeMobile]);

    /* Пока группа закрыта, активный пункт внутри неё не виден, поэтому
       признак переезжает на кнопку-раскрыватель. Маршруты берём из того же
       списка, что и ссылки меню, чтобы не расходились два места. */
    const isLearningActive = learningMenu.some((item) => location.pathname.startsWith(item.to));

    return (
        <header className="nav" ref={navRef}>
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
                                ref={learningRef}
                                onBlur={(event) => {
                                    if (!event.currentTarget.contains(event.relatedTarget)) closeLearning();
                                }}
                                onMouseEnter={() => {
                                    if (!isLearningOpen || openSourceRef.current === 'hover') openLearning('hover');
                                }}
                                onMouseLeave={(event) => {
                                    const group = event.currentTarget;
                                    if (openSourceRef.current === 'hover' && !group.contains(event.relatedTarget)) closeLearning();
                                }}
                            >
                                <button
                                    type="button"
                                    ref={learningTriggerRef}
                                    className={`nav__link nav__trigger${isLearningActive ? ' nav__link--current' : ''}`}
                                    aria-expanded={isLearningOpen}
                                    aria-current={isLearningActive ? 'true' : undefined}
                                    aria-controls={LEARNING_MENU_ID}
                                    onFocus={() => {
                                        if (suppressFocusOpenRef.current) {
                                            suppressFocusOpenRef.current = false;
                                            return;
                                        }
                                        /* С клавиатуры группа раскрывается сразу,
                                           мышью — только по клику (см. onClick). */
                                        if (modalityRef.current === 'keyboard') openLearning('keyboard');
                                    }}
                                    onClick={() => {
                                        if (openSourceRef.current === 'hover') {
                                            /* Первый клик после наведения фиксирует
                                               раскрытое состояние, а не закрывает. */
                                            openSourceRef.current = 'click';
                                            return;
                                        }
                                        if (isLearningOpen) closeLearning();
                                        else openLearning('click');
                                    }}
                                >
                                    Обучение
                                    <span className="nav__trigger-caret" aria-hidden="true" />
                                </button>
                                <AnimatePresence>
                                    {isLearningOpen && (
                                        <motion.div
                                            id={LEARNING_MENU_ID}
                                            className="nav__dropdown-menu"
                                            role="group"
                                            aria-label="Обучение"
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
                            <RouterNavLink
                                to="/profile"
                                className="nav__avatar"
                                aria-label="Профиль"
                                title="Профиль"
                            >
                                {(user.name || user.username || 'П').charAt(0).toUpperCase()}
                            </RouterNavLink>
                            <Button variant="dark" size="sm" onClick={logout}>Выйти</Button>
                        </>
                    ) : (
                        <Button variant="dark" size="sm" to="/register">Регистрация</Button>
                    )}
                    <button
                        type="button"
                        ref={burgerRef}
                        className={`nav__burger ${isMobileOpen ? 'nav__burger--open' : ''}`}
                        aria-expanded={isMobileOpen}
                        aria-controls={MOBILE_MENU_ID}
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
                        id={MOBILE_MENU_ID}
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

/* Активный маршрут подсвечивается состоянием, а не только вёрсткой:
   aria-current="page" ставит react-router, признак — подчёркивание и вес. */
const NavLink = ({ to, text }) => (
    <RouterNavLink
        to={to}
        className={({ isActive }) => `nav__link${isActive ? ' nav__link--current' : ''}`}
    >
        {text}
    </RouterNavLink>
);

export default Header;

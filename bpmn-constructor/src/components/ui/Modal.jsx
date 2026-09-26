import { useEffect, useId, useRef } from 'react';
import './ui.css';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Диалог с ловушкой фокуса, закрытием по Escape и возвратом фокуса вызывавшему
 * контролу. `initialFocus` — селектор внутри диалога; по умолчанию фокус уходит
 * на первую кнопку действия, то есть на отменяющее действие.
 */
const Modal = ({ title, description, onClose, children, actions, initialFocus }) => {
    const panelRef = useRef(null);
    const closerRef = useRef(null);
    const titleId = useId();

    useEffect(() => {
        const panel = panelRef.current;
        closerRef.current = document.activeElement;
        const scrollLock = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        (initialFocus ? panel.querySelector(initialFocus) : panel.querySelector(FOCUSABLE))?.focus();

        const onKeyDown = (event) => {
            if (event.key === 'Escape') {
                event.stopPropagation();
                onClose();
                return;
            }
            if (event.key !== 'Tab') return;
            const focusables = [...panel.querySelectorAll(FOCUSABLE)];
            if (!focusables.length) return;
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };

        document.addEventListener('keydown', onKeyDown, true);
        return () => {
            document.removeEventListener('keydown', onKeyDown, true);
            document.body.style.overflow = scrollLock;
            if (closerRef.current?.isConnected) closerRef.current.focus();
        };
    }, [onClose, initialFocus]);

    return (
        <div className="ui-modal" onPointerDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
            <section className="ui-modal__panel" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panelRef}>
                <h2 className="ui-modal__title" id={titleId}>{title}</h2>
                {description && <p className="ui-modal__description">{description}</p>}
                {children}
                {actions && <div className="ui-modal__actions">{actions}</div>}
            </section>
        </div>
    );
};

export default Modal;

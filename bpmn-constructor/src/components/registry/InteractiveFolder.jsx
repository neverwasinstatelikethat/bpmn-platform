import { useState } from 'react';
import './registry.css';

const declension = (count) => `${count} ${count === 1 ? 'схема' : count > 1 && count < 5 ? 'схемы' : 'схем'}`;

const InteractiveFolder = ({ name, diagrams = [], selected = false, onSelect, children }) => {
    const [open, setOpen] = useState(false);
    const handleClick = () => {
        setOpen((value) => !value);
        onSelect?.();
    };

    return (
        <article className={`registry-folder ${open ? 'is-open' : ''} ${selected ? 'is-selected' : ''}`}>
            <button type="button" className="registry-folder__button" onClick={handleClick} aria-expanded={open}>
                <span className="registry-folder__tab" aria-hidden="true" />
                <span className="registry-folder__front"><span>{name}</span><small>{declension(diagrams.length)}</small></span>
                <span className="registry-folder__papers" aria-hidden="true"><i /><i /><i /></span>
            </button>
            {open && <div className="registry-folder__contents"><strong>{declension(diagrams.length)}</strong>{children}</div>}
        </article>
    );
};

export default InteractiveFolder;

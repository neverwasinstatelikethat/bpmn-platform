import './ui.css';

/**
 * Текстовое поле в фирменном стиле.
 * `label` оборачивает поле в подписанный блок.
 */
const Input = ({ label, id, className = '', ...rest }) => {
    const input = (
        <input id={id} className={`ui-input ${className}`} {...rest} />
    );

    if (!label) return input;

    return (
        <div className="ui-field">
            <label className="ui-field__label" htmlFor={id}>{label}</label>
            {input}
        </div>
    );
};

export default Input;

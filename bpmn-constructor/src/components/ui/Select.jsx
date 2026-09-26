import './ui.css';

/**
 * Выбор в фирменном стиле. Нативный <select> сознательно оставлен нативным:
 * он даёт клавиатуру, мобильный picker и доступное имя без нашей обвязки.
 * `label` подписывает поле, `options` — массив { value, label } или строк.
 */
const Select = ({ label, id, className = '', options = [], children, ...rest }) => {
    const control = (
        <select id={id} className={`ui-select ${className}`} {...rest}>
            {children ?? options.map((option) => (
                typeof option === 'string'
                    ? <option key={option} value={option}>{option}</option>
                    : <option key={option.value} value={option.value}>{option.label}</option>
            ))}
        </select>
    );

    if (!label) return control;

    return (
        <div className="ui-field">
            <label className="ui-field__label" htmlFor={id}>{label}</label>
            {control}
        </div>
    );
};

export default Select;

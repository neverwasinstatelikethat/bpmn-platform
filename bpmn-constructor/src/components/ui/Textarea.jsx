import './ui.css';

/**
 * Многострочное поле. `maxChars` показывает счётчик символов, чтобы лимит
 * бэкенда был виден до ошибки, а не после неё.
 */
const Textarea = ({ label, id, className = '', value = '', maxChars, ...rest }) => {
    const field = (
        <>
            <textarea
                id={id}
                className={`ui-textarea ${className}`}
                value={value}
                maxLength={maxChars}
                {...rest}
            />
            {maxChars && (
                <p className="ui-textarea__count" aria-live="polite">
                    {value.length}&nbsp;/&nbsp;{maxChars}
                </p>
            )}
        </>
    );

    if (!label) return field;

    return (
        <div className="ui-field">
            <label className="ui-field__label" htmlFor={id}>{label}</label>
            {field}
        </div>
    );
};

export default Textarea;

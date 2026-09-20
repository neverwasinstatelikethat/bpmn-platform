import './ui.css';

// Переключатель с мягким свечением во включённом состоянии.
// Доступность: role="switch", aria-checked; подпись — видимый текст кнопки.
const Switch = ({
    checked = false,
    onChange,
    label,
    disabled = false,
    size = 'md',
    glow = true,
    className = '',
}) => {
    const classes = [
        'ui-switch',
        `ui-switch--${size}`,
        checked ? 'is-on' : 'is-off',
        glow ? 'ui-switch--glow' : '',
        disabled ? 'is-disabled' : '',
        className,
    ].filter(Boolean).join(' ');

    return (
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            disabled={disabled}
            className={classes}
            onClick={() => onChange && onChange(!checked)}
        >
            <span className="ui-switch__track" aria-hidden="true">
                <span className="ui-switch__knob" />
            </span>
            {label && <span className="ui-switch__label">{label}</span>}
        </button>
    );
};

export default Switch;

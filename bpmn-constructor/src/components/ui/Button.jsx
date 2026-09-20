import { Link } from 'react-router-dom';
import './ui.css';

/**
 * Единая кнопка продукта.
 * Варианты: primary | secondary | soft | dark | ghost
 * Размеры: sm | md | lg. Передайте `to` для ссылки внутри приложения
 * или `href` для внешней ссылки.
 */
const Button = ({
    variant = 'primary',
    size = 'md',
    to,
    href,
    block = false,
    className = '',
    children,
    ...rest
}) => {
    const cls = [
        'ui-btn',
        `ui-btn--${variant}`,
        `ui-btn--${size}`,
        block ? 'ui-btn--block' : '',
        className,
    ].filter(Boolean).join(' ');

    if (to) {
        return <Link to={to} className={cls} {...rest}>{children}</Link>;
    }
    if (href) {
        return <a href={href} className={cls} {...rest}>{children}</a>;
    }
    return <button type="button" className={cls} {...rest}>{children}</button>;
};

export default Button;

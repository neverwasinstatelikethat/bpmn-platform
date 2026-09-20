import { Link } from 'react-router-dom';
import './layout.css';

const EmptyState = ({ title, description, actionLabel, actionTo, action }) => (
    <section className="empty-state" aria-live="polite">
        <span className="empty-state__mark" aria-hidden="true">•</span>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
        {action || (actionLabel && actionTo && <Link className="ui-btn ui-btn--primary ui-btn--md" to={actionTo}>{actionLabel}</Link>)}
    </section>
);

export default EmptyState;

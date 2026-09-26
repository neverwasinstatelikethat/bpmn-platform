import { Link } from 'react-router-dom';
import './layout.css';

/* Раздел статичен: он появляется вместе со страницей, а не обновляется на
   живом месте, поэтому aria-live здесь только озвучивал бы лишнее.
   Метка — чистый CSS-кружок без символа внутри: глиф «•» поверх круга
   давал два знака вместо одного. */
const EmptyState = ({ title, description, actionLabel, actionTo, action }) => (
    <section className="empty-state">
        <span className="empty-state__mark" aria-hidden="true" />
        <h2>{title}</h2>
        {description && <p>{description}</p>}
        {action || (actionLabel && actionTo && <Link className="ui-btn ui-btn--primary ui-btn--md" to={actionTo}>{actionLabel}</Link>)}
    </section>
);

export default EmptyState;

import './layout.css';

const PageHeader = ({ eyebrow, title, description, action, children }) => (
    <header className="work-page__header">
        <div className="work-page__heading">
            {eyebrow && <p className="work-page__eyebrow">{eyebrow}</p>}
            <h1>{title}</h1>
            {description && <p className="work-page__description">{description}</p>}
        </div>
        {(action || children) && <div className="work-page__actions">{action || children}</div>}
    </header>
);

export default PageHeader;

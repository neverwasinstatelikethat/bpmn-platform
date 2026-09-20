import './layout.css';

const PageLoader = ({ label = 'Загружаем данные…' }) => (
    <div className="page-loader" role="status" aria-live="polite">
        <span className="page-loader__dot" aria-hidden="true" />
        {label}
    </div>
);

export default PageLoader;

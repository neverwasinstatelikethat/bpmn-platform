import PageHeader from './PageHeader';
import './layout.css';

const WorkPage = ({ title, description, eyebrow, action, children, className = '' }) => (
    <main className={`work-page ${className}`.trim()}>
        <div className="work-page__inner">
            <PageHeader title={title} description={description} eyebrow={eyebrow} action={action} />
            {children}
        </div>
    </main>
);

export default WorkPage;

import { Link } from 'react-router-dom';
import './NotFound.css';

const NotFound = () => (
    <main className="not-found">
        <div>
            <p className="not-found__eyebrow">404</p>
            <h1>Такой страницы нет</h1>
            <p>Вернитесь в рабочее пространство или начните с главной.</p>
            <Link className="ui-btn ui-btn--primary ui-btn--md" to="/">На главную</Link>
        </div>
    </main>
);

export default NotFound;

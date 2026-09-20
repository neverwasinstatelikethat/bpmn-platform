import { useState } from 'react';
import WorkPage from './components/layout/WorkPage';
import './Errors.css';

const patterns = [
    { level: 'Критично', title: 'Неправильная логика шлюзов', problem: 'Параллельный шлюз используют там, где участник должен выбрать только один путь.', fix: 'Для альтернатив выберите эксклюзивный шлюз (XOR) и подпишите условие на каждом исходящем потоке.', checks: ['Один путь — XOR', 'Одновременные действия — AND', 'У каждого решения есть условие'] },
    { level: 'Критично', title: 'Нет обработки исключения', problem: 'Рискованная задача не показывает, что делать при сбое.', fix: 'Добавьте граничное событие ошибки и отдельный компенсирующий сценарий.', checks: ['Названы внешние риски', 'Есть путь при ошибке', 'Ответственный понятен'] },
    { level: 'Важно', title: 'Размыты роли в дорожках', problem: 'В одной дорожке смешаны действия разных команд.', fix: 'Пулом обозначьте участника, а дорожками — роли внутри него.', checks: ['Пул описывает участника', 'Дорожка описывает роль', 'Задача лежит у исполнителя'] },
    { level: 'Важно', title: 'У процесса нет границ', problem: 'Читатель не понимает, где запускается процесс и когда он завершён.', fix: 'Оставьте ровно одно ясное стартовое событие и хотя бы одно конечное событие для каждого исхода.', checks: ['Есть старт', 'У каждого пути есть финал', 'События подписаны'] },
    { level: 'Проверить', title: 'Слишком много деталей', problem: 'Схема пытается описать каждое нажатие и перестаёт объяснять процесс.', fix: 'Оставьте бизнес-шаги, а технические детали перенесите в подпроцесс или документацию.', checks: ['Один блок — одно действие', 'Названия начинаются с глагола', 'Сложное вынесено в подпроцесс'] },
];

const Errors = () => {
    const [open, setOpen] = useState(0);
    return <WorkPage title="Частые ошибки" eyebrow="практика BPMN" description="Короткий разбор того, что чаще всего мешает схеме оставаться понятной для команды.">
        <div className="errors-summary"><span>5</span><p>паттернов, которые стоит проверить перед публикацией схемы</p></div>
        <section className="errors-list" aria-label="Ошибки BPMN">{patterns.map((pattern, index) => <article className={`error-card ${open === index ? 'is-open' : ''}`} key={pattern.title}><button className="error-card__trigger" onClick={() => setOpen(open === index ? null : index)} aria-expanded={open === index}><span className={`error-card__level error-card__level--${index < 2 ? 'high' : index < 4 ? 'mid' : 'low'}`}>{pattern.level}</span><span>{pattern.title}</span><i aria-hidden="true">{open === index ? '−' : '+'}</i></button>{open === index && <div className="error-card__body"><div><p className="error-card__label">Что происходит</p><p>{pattern.problem}</p></div><div className="error-card__fix"><p className="error-card__label">Как исправить</p><p>{pattern.fix}</p></div><ul>{pattern.checks.map((check) => <li key={check}>{check}</li>)}</ul></div>}</article>)}</section>
    </WorkPage>;
};

export default Errors;

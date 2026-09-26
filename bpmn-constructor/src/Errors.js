import WorkPage from './components/layout/WorkPage';
// Компоненты берём напрямую из модулей: barrel `components/ui` тянет Button,
// а он — react-router-dom, который не разбирается сбором jest (падает Profile.test.js).
import Accordion from './components/ui/Accordion';
import Badge from './components/ui/Badge';
import './Errors.css';

/**
 * Типичные ошибки BPMN для аналитика процесса. Формулировки — предметные:
 * без сокращений вроде XOR/AND и без языка реализации.
 */
const patterns = [
    {
        level: 'Критично',
        title: 'Неправильная логика шлюзов',
        problem: 'Параллельный шлюз используют там, где участник должен выбрать только один путь.',
        fix: 'Для выбора между вариантами берите эксклюзивный шлюз — он оставляет ровно один путь — и подпишите условие на каждом исходящем переходе.',
        checks: ['Один путь — эксклюзивный шлюз', 'Одновременные действия — параллельный шлюз', 'У каждого решения есть условие'],
    },
    {
        level: 'Критично',
        title: 'Нет обработки исключения',
        problem: 'Рискованная задача не показывает, что делать при сбое.',
        fix: 'Добавьте граничное событие ошибки и отдельный компенсирующий сценарий.',
        checks: ['Названы внешние риски', 'Есть путь при ошибке', 'Ответственный понятен'],
    },
    {
        level: 'Важно',
        title: 'Размыты роли в дорожках',
        problem: 'В одной дорожке смешаны действия разных команд.',
        fix: 'Каждого участника процесса покажите отдельным пулом, а его роли — дорожками внутри этого пула.',
        checks: ['Пул описывает участника', 'Дорожка описывает роль', 'Задача лежит у исполнителя'],
    },
    {
        level: 'Важно',
        title: 'У процесса нет границ',
        problem: 'Читатель не понимает, где запускается процесс и когда он завершён.',
        fix: 'Оставьте ровно одно ясное стартовое событие и хотя бы одно конечное событие для каждого исхода.',
        checks: ['Есть старт', 'У каждого пути есть финал', 'События подписаны'],
    },
    {
        level: 'Проверить',
        title: 'Слишком много деталей',
        problem: 'Схема пытается описать каждое нажатие и перестаёт объяснять процесс.',
        fix: 'Оставьте бизнес-шаги, а технические детали перенесите в подпроцесс или документацию.',
        checks: ['Один блок — одно действие', 'Названия начинаются с глагола', 'Сложное вынесено в подпроцесс'],
    },
];

/**
 * Тон метки уровня берётся строго из `pattern.level`, а не из позиции в списке:
 * перестановка карточки не должна молча менять знак риска.
 */
const LEVEL_TONES = {
    'Критично': 'peach',
    'Важно': 'lavender',
    'Проверить': 'sage',
};

const levelTone = (level) => LEVEL_TONES[level] ?? 'sage';

const errorItems = patterns.map((pattern) => ({
    question: (
        <span className="errors-card__heading">
            <Badge tone={levelTone(pattern.level)}>{pattern.level}</Badge>
            <span className="errors-card__title">{pattern.title}</span>
        </span>
    ),
    answer: (
        <div className="errors-card">
            <div className="errors-card__column">
                <p className="errors-card__label">Что происходит</p>
                <p className="errors-card__text">{pattern.problem}</p>
            </div>
            <div className="errors-card__column errors-card__fix">
                <p className="errors-card__label">Как исправить</p>
                <p className="errors-card__text">{pattern.fix}</p>
            </div>
            <div className="errors-card__checks">
                <p className="errors-card__label">На что смотреть</p>
                <ul>
                    {pattern.checks.map((check) => <li key={check}>{check}</li>)}
                </ul>
            </div>
        </div>
    ),
}));

const Errors = () => (
    <WorkPage
        title="Частые ошибки"
        eyebrow="практика BPMN"
        description="Короткий разбор того, что чаще всего мешает схеме оставаться понятной для команды.">
        <p className="errors-summary">
            Перед публикацией схемы проверьте её по этим паттернам — всего их {patterns.length}.
        </p>
        <section className="errors-list" aria-label="Частые ошибки BPMN">
            <Accordion items={errorItems} defaultOpen={0} />
        </section>
    </WorkPage>
);

export default Errors;

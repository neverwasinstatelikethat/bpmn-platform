import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import { Button, Badge, SectionHeading, Accordion, Reveal, Aurora, Typewriter, ImageTrail } from './components/ui';
import { API_BASE_URL } from './config';
import './Home.css';

// Хронология без меток времени: «09:15 → 09:17» читалось как замер скорости,
// которого мы не делали.
const scenarios = [
    { key: 'describe', text: 'Описываете закупку обычными словами' },
    { key: 'generate', text: 'ИИ собирает схему — остаётся пара штрихов' },
    { key: 'find', text: 'В согласовании находится узкое место' },
    { key: 'share', text: 'Схемой делитесь с командой' },
    { key: 'export', text: 'Процесс готов — экспорт в PDF' },
];

const faqItems = [
    {
        question: 'Что такое BPMN?',
        answer: 'Это язык схем бизнес-процессов: прямоугольники — шаги, ромбы — решения, стрелки — порядок. Его понимают и люди, и системы автоматизации.',
    },
    {
        question: 'Нужно ли уметь рисовать схемы?',
        answer: 'Нет. Опишите процесс обычным текстом — ИИ соберёт черновик схемы, а редактор мягко подскажет, что поправить.',
    },
    {
        question: 'Что умеет ИИ-помощник?',
        answer: 'Собирает схему из текстового описания, проверяет типовые ошибки и предлагает, где процесс можно упростить. Решение всегда остаётся за вами.',
    },
    {
        question: 'Это бесплатно?',
        answer: 'Да, для команды ВкусВилла конструктор полностью бесплатный — без пробных периодов и скрытых ограничений.',
    },
    {
        question: 'Как работать вместе с командой?',
        answer: 'Нажмите «Поделиться» в редакторе: ссылка живёт неделю и открывает схему на чтение — без входа в аккаунт. Если доступ дан на изменение, по ссылке можно открыть редактируемую копию. Коллеги увидят правки только после «Сохранить» в редакторе: автосохранения и комментариев на странице доступа нет.',
    },
];

/* фразы для печатной машинки в мок-чате героя: только то, что ИИ делает по факту */
const aiPhrases = [
    'Собрала черновик схемы по вашему описанию.',
    'Отметила шаги, которые можно упростить.',
    'Правьте в редакторе — схема остаётся вашей.',
];

/* Единственный ассет, за который мы ручаемся: реальная BPMN-схема кредитного
   процесса с пулами и шлюзами. Два SVG рядом с ним в static/ — graphviz-кластеры
   («adds new user», «sends notification»), к BPMN отношения не имеют.
   Свежие снимки текущего интерфейса добавим, когда редактор и реестр улягутся. */
const staticAsset = (file) => `${API_BASE_URL}/static/${file}`;
const trailImages = [
    { src: staticAsset('bpmn_diagram.png'), alt: 'Схема кредитного процесса с пулами и шлюзами', label: 'Пулы, шлюзы, события' },
];

const Home = () => {
    const { user } = useAuth();
    const navigate = useNavigate();
    const [email, setEmail] = useState('');

    const handleStart = (e) => {
        e.preventDefault();
        navigate(email.trim() ? `/register?email=${encodeURIComponent(email.trim())}` : '/register');
    };

    return (
        <div className="home">
            {/* ---------- Hero ---------- */}
            <section className="hero">
                <Aurora />
                <div className="hero__content">
                    <Reveal y={16}>
                        <Badge tone="green" dot>ВкусВилл · конструктор бизнес-процессов</Badge>
                    </Reveal>
                    <Reveal delay={0.1}>
                        <h1 className="hero__title">
                            Процессы без <span className="hand hand--green">лишнего</span>
                        </h1>
                    </Reveal>
                    <Reveal delay={0.2}>
                        <p className="hero__subtitle">
                            Опишите, как всё устроено, обычными словами. ИИ соберёт BPMN-схему,
                            подсветит узкие места и подскажет, что упростить.
                        </p>
                    </Reveal>
                    <Reveal delay={0.3}>
                        <div className="hero__cta">
                            <Button size="lg" to={user ? '/editor' : '/register'}>
                                {user ? 'Открыть редактор' : 'Попробовать бесплатно'}
                            </Button>
                            <Button size="lg" variant="secondary" href="#how">Как это работает</Button>
                        </div>
                    </Reveal>
                </div>
            </section>

            <div className="home__container">
                {/* ---------- Горизонтальная лента сценариев ---------- */}
                <section className="scenarios">
                    <Reveal>
                        <SectionHeading
                            eyebrow="путь схемы"
                            title="С конструктором — спокойно"
                            subtitle="Никаких пустых холстов и «а с чего начать». От описания словами до экспорта — пять шагов."
                        />
                    </Reveal>
                    <Reveal delay={0.1}>
                        <div
                            className="scenarios__track"
                            role="list"
                            tabIndex={0}
                            aria-label="Лента сценариев, прокручивается по горизонтали"
                        >
                            {scenarios.map((item, index) => (
                                <article key={item.key} className="scenario-card" role="listitem">
                                    <span className="scenario-card__step">{String(index + 1).padStart(2, '0')}</span>
                                    <span className="scenario-card__text">{item.text}</span>
                                </article>
                            ))}
                        </div>
                    </Reveal>
                </section>

                {/* ---------- Превью приложения ---------- */}
                <section className="preview" id="how">
                    <Reveal>
                        <SectionHeading
                            eyebrow="как это работает"
                            title="Один инструмент вместо десяти вкладок"
                            subtitle="ИИ-чат, аккуратный редактор и понятная аналитика — рядом, в одном окне."
                        />
                    </Reveal>
                    <Reveal delay={0.15}>
                        <div className="preview__stage">
                            <div className="screen screen--side screen--left" aria-hidden="true">
                                <div className="screen__body screen__body--sage">
                                    <div className="mock-flow">
                                        <div className="mock-node" />
                                        <div className="mock-link" />
                                        <div className="mock-node mock-node--decision" />
                                        <div className="mock-link" />
                                        <div className="mock-node" />
                                    </div>
                                </div>
                            </div>

                            <div className="screen screen--center">
                                <div className="screen__bar">
                                    <span className="screen__dot" /><span className="screen__dot" /><span className="screen__dot" />
                                </div>
                                <div className="screen__body">
                                    <div className="chat">
                                        <div className="chat__bubble chat__bubble--user">
                                            Согласование договора: юрист, потом финансы, потом директор
                                        </div>
                                        <div className="chat__bubble chat__bubble--ai">
                                            <Typewriter phrases={aiPhrases} speed={50} loop startDelay={800} />
                                            <span className="chat__chips">
                                                <span className="chat__chip">Юрист</span>
                                                <span className="chat__chip-arrow">→</span>
                                                <span className="chat__chip">Финансы</span>
                                                <span className="chat__chip-arrow">→</span>
                                                <span className="chat__chip">Директор</span>
                                            </span>
                                        </div>
                                        <Button className="btn-pulse" to={user ? '/editor' : '/register'}>
                                            Создать схему
                                        </Button>
                                    </div>
                                </div>
                            </div>

                            {/* декоративный экран: абстрактная геометрия без шкалы,
                                подписей и вердиктов — это не график с данными */}
                            <div className="screen screen--side screen--right" aria-hidden="true">
                                <div className="screen__body screen__body--lavender">
                                    <div className="mock-bars">
                                        <div className="mock-bar" />
                                        <div className="mock-bar" />
                                        <div className="mock-bar" />
                                        <div className="mock-bar" />
                                        <div className="mock-bar" />
                                    </div>
                                </div>
                            </div>
                        </div>
                    </Reveal>
                </section>

                {/* ---------- Витрина: след за курсором (на тач — слайдер) ---------- */}
                <section className="showcase" id="showcase">
                    <Reveal>
                        <SectionHeading
                            eyebrow="вот что внутри"
                            title="Побегайте курсором по продукту"
                            subtitle="Схемы процессов, а не макеты интерфейса: так выглядит результат, с которым работает редактор."
                        />
                    </Reveal>
                    <Reveal delay={0.1}>
                        <ImageTrail
                            images={trailImages}
                            className="showcase__trail"
                            ariaLabel="Схемы процессов"
                            hint="Поведите курсором — схемы потянутся за ним"
                        />
                    </Reveal>
                </section>

                {/* ---------- Конверсионный блок: финальная сцена ---------- */}
                {/* Одна большая зелёная плоскость действия (green — цвет действия)
                    вместо тёмной плиты с блобами; характер держат дисплейный
                    заголовок с зелёным акцентом и одна кнопка. Подсветка Aurora
                    остаётся только в hero. */}
                <section className="convert">
                    <Reveal>
                        <div className="convert__panel">
                            <h2 className="convert__title">
                                Начните в <span className="convert__accent">своём темпе</span>
                            </h2>
                            <p className="convert__subtitle">
                                Бесплатно для команды ВкусВилла. Первая схема собирается
                                с обычного описания процесса — без обучения и настройки.
                            </p>
                            {user ? (
                                <Button size="lg" to="/editor">Открыть редактор</Button>
                            ) : (
                                <form className="convert__form" onSubmit={handleStart}>
                                    <input
                                        type="email"
                                        className="ui-input convert__field"
                                        placeholder="Рабочая почта"
                                        aria-label="Рабочая почта"
                                        autoComplete="email"
                                        value={email}
                                        onChange={(e) => setEmail(e.target.value)}
                                    />
                                    <button type="submit" className="ui-btn ui-btn--primary ui-btn--lg convert__submit">
                                        Начать
                                    </button>
                                </form>
                            )}
                        </div>
                    </Reveal>
                </section>

                {/* ---------- FAQ ---------- */}
                <section className="faq" id="faq">
                    <Reveal>
                        <SectionHeading
                            eyebrow="тихо и по делу"
                            title="Частые вопросы"
                        />
                    </Reveal>
                    <Reveal delay={0.1}>
                        <Accordion items={faqItems} defaultOpen={0} />
                    </Reveal>
                </section>
            </div>

            {/* ---------- Подвал ---------- */}
            <footer className="footer">
                <div className="footer__inner">
                    <div className="footer__brand">
                        <span className="footer__logo" aria-hidden="true"><span className="footer__logo-dot" /></span>
                        ВкусВилл · конструктор процессов
                    </div>
                    <nav className="footer__links" aria-label="Подвал">
                        {!user && <Link to="/login">Вход</Link>}
                        {!user && <Link to="/register">Регистрация</Link>}
                        <a href="#faq">Вопросы</a>
                        <a href="#how">Как это работает</a>
                    </nav>
                </div>
            </footer>
        </div>
    );
};

export default Home;

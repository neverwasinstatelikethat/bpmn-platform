import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import { Button, Badge, SectionHeading, Accordion, Reveal, Aurora, Switch, Slider, Typewriter, ImageTrail } from './components/ui';
import { API_BASE_URL } from './config';
import './Home.css';

const scenarios = [
    { time: '09:15', text: 'Описала закупку обычными словами' },
    { time: '09:17', text: 'ИИ собрал схему — осталась пара штрихов' },
    { time: '11:40', text: 'Нашли узкое место в согласовании' },
    { time: '14:05', text: 'Поделилась схемой с командой' },
    { time: '16:30', text: 'Процесс готов — экспорт в PDF' },
];

const notes = [
    {
        text: 'Раньше рисовала квадратики в блокноте. Теперь описываю процесс словами — и схема готова.',
        name: 'Марина',
        role: 'закупки',
        tilt: 'left',
    },
    {
        text: 'Нашли узкое место в согласовании договоров за один вечер. Теперь экономим два дня на каждом.',
        name: 'Илья',
        role: 'комплаенс',
        tilt: 'right',
    },
    {
        text: 'ИИ не рисует за меня — он забирает рутину. Так честнее и спокойнее.',
        name: 'Света',
        role: 'аналитика',
        tilt: 'right',
    },
    {
        text: 'Подключил команду за пять минут. Никто даже не спросил инструкцию.',
        name: 'Павел',
        role: 'ИТ',
        tilt: 'left',
    },
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
        answer: 'Поделитесь схемой по ссылке: коллеги смогут смотреть и комментировать. Все изменения сохраняются автоматически.',
    },
];

/* фразы для печатной машинки в мок-чате героя */
const aiPhrases = [
    'Собрала схему из трёх шагов и двух проверок.',
    'Нашла узкое место: согласование занимает два дня.',
    'Подскажу, где процесс можно упростить.',
];

/* скриншоты для следа за курсором */
const staticAsset = (file) => `${API_BASE_URL}/static/${file}`;
const trailImages = [
    { src: staticAsset('bpmn_diagram.png'), alt: 'Пример BPMN-схемы', label: 'Схема процесса' },
    { src: staticAsset('a1132323-a375-4e27-940e-8fec0cbd9768.svg'), alt: 'Визуальный пример процесса', label: 'Процесс' },
    { src: staticAsset('bb84aded-8e6b-4b43-a334-dbf855046a22.svg'), alt: 'Визуальный пример BPMN', label: 'BPMN' },
];

/* слайды для карусели возможностей */
const featureSlides = [
    { src: staticAsset('bpmn_diagram.png'), alt: 'Пример BPMN-схемы', caption: 'ИИ собирает черновик из обычного описания' },
    { src: staticAsset('a1132323-a375-4e27-940e-8fec0cbd9768.svg'), alt: 'Визуальный пример процесса', caption: 'Редактор сохраняет логику процесса ясной' },
    { src: staticAsset('bb84aded-8e6b-4b43-a334-dbf855046a22.svg'), alt: 'Визуальный пример BPMN', caption: 'Анализ помогает увидеть узкие места' },
];

const Home = () => {
    const { user } = useAuth();
    const navigate = useNavigate();
    const [email, setEmail] = useState('');
    const [aiHints, setAiHints] = useState(true);

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
                            eyebrow="один день"
                            title="С конструктором — спокойно"
                            subtitle="Никаких пустых холстов и «а с чего начать». Вот как проходит обычный день."
                        />
                    </Reveal>
                    <Reveal delay={0.1}>
                        <div className="scenarios__track" role="list">
                            {scenarios.map((item) => (
                                <article key={item.time} className="scenario-card" role="listitem">
                                    <span className="scenario-card__time">{item.time}</span>
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
                                <div className="screen__bar">
                                    <span className="screen__dot" /><span className="screen__dot" /><span className="screen__dot" />
                                </div>
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
                                        <button type="button" className="ui-btn ui-btn--primary ui-btn--md btn-pulse">
                                            Создать схему
                                        </button>
                                    </div>
                                </div>
                            </div>

                            <div className="screen screen--side screen--right" aria-hidden="true">
                                <div className="screen__bar">
                                    <span className="screen__dot" /><span className="screen__dot" /><span className="screen__dot" />
                                </div>
                                <div className="screen__body screen__body--lavender">
                                    <div className="screen__switch">
                                        <Switch size="sm" checked={aiHints} onChange={setAiHints} label="ИИ-подсветка" />
                                    </div>
                                    <div className="mock-bars">
                                        <div className="mock-bar" style={{ height: '38%' }} />
                                        <div className="mock-bar" style={{ height: '62%' }} />
                                        <div className="mock-bar" style={{ height: '46%' }} />
                                        <div className={`mock-bar ${aiHints ? 'mock-bar--accent' : ''}`} style={{ height: '82%' }} />
                                        <div className="mock-bar" style={{ height: '54%' }} />
                                    </div>
                                    <div className="mock-stat">{aiHints ? 'узкое место найдено' : 'анализ на паузе'}</div>
                                </div>
                            </div>
                        </div>
                    </Reveal>
                </section>

                {/* ---------- Витрина: след за курсором и карусель ---------- */}
                <section className="showcase" id="showcase">
                    <Reveal>
                        <SectionHeading
                            eyebrow="вот что внутри"
                            title="Побегайте курсором по продукту"
                            subtitle="Скриншоты настоящие: редактор, ИИ-чат, аналитика и совместный доступ."
                        />
                    </Reveal>
                    <Reveal delay={0.1}>
                        <ImageTrail
                            images={trailImages}
                            hint="Поведите курсором — скриншоты потянутся за ним"
                        />
                    </Reveal>
                    <Reveal delay={0.15}>
                        <Slider
                            slides={featureSlides}
                            autoPlay={4200}
                            className="showcase__slider"
                            ariaLabel="Возможности конструктора"
                        />
                    </Reveal>
                </section>

                {/* ---------- Заметки команды ---------- */}
                <section className="notes">
                    <Reveal>
                        <SectionHeading
                            eyebrow="живые отзывы"
                            title="Заметки на полях"
                            subtitle="Что говорят коллеги, которые уже перестали рисовать квадратики вручную."
                        />
                    </Reveal>
                    <div className="notes__grid">
                        {notes.map((note, index) => (
                            <Reveal key={note.name} delay={index * 0.08}>
                                <article className={`note note--tilt-${note.tilt}`}>
                                    <p className="note__text">«{note.text}»</p>
                                    <div className="note__sign">
                                        <span className="note__sign-line" aria-hidden="true" />
                                        <span className="note__sign-name">{note.name}, {note.role}</span>
                                    </div>
                                </article>
                            </Reveal>
                        ))}
                    </div>
                </section>

                {/* ---------- Конверсионный блок ---------- */}
                <section className="convert">
                    <Reveal>
                        <div className="convert__panel">
                            <div className="convert__glow convert__glow--green" aria-hidden="true" />
                            <div className="convert__glow convert__glow--berry" aria-hidden="true" />
                            <div className="convert__icon" aria-hidden="true">
                                <span className="convert__icon-dot" />
                            </div>
                            <h2 className="convert__title">Начните в своём темпе</h2>
                            <p className="convert__subtitle">
                                Бесплатно для команды ВкусВилла. Первая схема — уже через пять минут,
                                без обучения и настройки.
                            </p>
                            {user ? (
                                <Button size="lg" to="/editor">Открыть редактор</Button>
                            ) : (
                                <form className="convert__form" onSubmit={handleStart}>
                                    <input
                                        type="email"
                                        className="convert__input"
                                        placeholder="Рабочая почта"
                                        aria-label="Рабочая почта"
                                        value={email}
                                        onChange={(e) => setEmail(e.target.value)}
                                    />
                                    <button type="submit" className="convert__submit">Начать</button>
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
                        {!user && <a href="/login">Вход</a>}
                        {!user && <a href="/register">Регистрация</a>}
                        <a href="#faq">Вопросы</a>
                        <a href="#how">Как это работает</a>
                    </nav>
                </div>
            </footer>
        </div>
    );
};

export default Home;

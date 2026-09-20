import { useEffect, useRef, useState } from 'react';
import './ui.css';

// Универсальный слайдер: стрелки, точки, автопрокрутка, свайп и клавиатура.
// slides: [{ src, alt, caption }] — либо произвольные узлы через { content }.
const Slider = ({
    slides = [],
    autoPlay = 0,
    className = '',
    ariaLabel = 'Карусель',
}) => {
    const [index, setIndex] = useState(0);
    const [paused, setPaused] = useState(false);
    const touchStartX = useRef(null);
    const count = slides.length;

    const go = (next) => setIndex(((next % count) + count) % count);

    useEffect(() => {
        if (!autoPlay || count <= 1 || paused) return undefined;
        const timer = setInterval(() => setIndex((i) => (i + 1) % count), autoPlay);
        return () => clearInterval(timer);
    }, [autoPlay, count, paused]);

    const handleKeyDown = (e) => {
        if (e.key === 'ArrowLeft') {
            e.preventDefault();
            go(index - 1);
        } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            go(index + 1);
        }
    };

    const handleTouchStart = (e) => {
        touchStartX.current = e.touches[0].clientX;
    };

    const handleTouchEnd = (e) => {
        if (touchStartX.current === null) return;
        const delta = e.changedTouches[0].clientX - touchStartX.current;
        if (Math.abs(delta) > 48) go(index + (delta < 0 ? 1 : -1));
        touchStartX.current = null;
    };

    if (count === 0) return null;

    return (
        <div
            className={`ui-slider ${className}`.trim()}
            role="region"
            aria-roledescription="карусель"
            aria-label={ariaLabel}
            tabIndex={0}
            onKeyDown={handleKeyDown}
            onMouseEnter={() => setPaused(true)}
            onMouseLeave={() => setPaused(false)}
            onFocus={() => setPaused(true)}
            onBlur={() => setPaused(false)}
            onTouchStart={handleTouchStart}
            onTouchEnd={handleTouchEnd}
        >
            <div className="ui-slider__viewport">
                <div
                    className="ui-slider__track"
                    style={{ transform: `translateX(-${index * 100}%)` }}
                >
                    {slides.map((slide, i) => (
                        <figure
                            key={slide.src || i}
                            className="ui-slider__slide"
                            aria-hidden={i !== index}
                        >
                            {slide.content || (
                                <img src={slide.src} alt={slide.alt || ''} loading="lazy" />
                            )}
                            {slide.caption && (
                                <figcaption className="ui-slider__caption">{slide.caption}</figcaption>
                            )}
                        </figure>
                    ))}
                </div>
            </div>

            {count > 1 && (
                <>
                    <button
                        type="button"
                        className="ui-slider__arrow ui-slider__arrow--prev"
                        aria-label="Предыдущий слайд"
                        onClick={() => go(index - 1)}
                    >
                        ←
                    </button>
                    <button
                        type="button"
                        className="ui-slider__arrow ui-slider__arrow--next"
                        aria-label="Следующий слайд"
                        onClick={() => go(index + 1)}
                    >
                        →
                    </button>
                    <div className="ui-slider__dots" role="tablist" aria-label="Слайды">
                        {slides.map((_, i) => (
                            <button
                                key={i}
                                type="button"
                                role="tab"
                                aria-selected={i === index}
                                aria-label={`Слайд ${i + 1}`}
                                className={`ui-slider__dot ${i === index ? 'is-active' : ''}`}
                                onClick={() => go(i)}
                            />
                        ))}
                    </div>
                </>
            )}
        </div>
    );
};

export default Slider;

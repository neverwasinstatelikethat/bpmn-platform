import { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Slider from './Slider';
import './ui.css';

// След из изображений за курсором. На тач-устройствах и при
// prefers-reduced-motion превращается в обычный слайдер.
const ImageTrail = ({ images = [], className = '', hint, ariaLabel = 'Галерея' }) => {
    const areaRef = useRef(null);
    const lastRef = useRef({ x: 0, y: 0, t: 0, index: 0 });
    const timersRef = useRef([]);
    const [items, setItems] = useState([]);
    const [fallback, setFallback] = useState(false);

    useEffect(() => {
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const finePointer = window.matchMedia('(pointer: fine)').matches;
        if (reduced || !finePointer) setFallback(true);
        return () => {
            timersRef.current.forEach(clearTimeout);
            timersRef.current = [];
        };
    }, []);

    const handleMove = (e) => {
        if (!areaRef.current) return;
        const now = performance.now();
        const last = lastRef.current;
        if (now - last.t < 90) return;
        if (last.t !== 0 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 42) return;

        const rect = areaRef.current.getBoundingClientRect();
        const id = `${now}-${Math.random().toString(16).slice(2)}`;
        const image = images[last.index % images.length];
        lastRef.current = {
            x: e.clientX,
            y: e.clientY,
            t: now,
            index: (last.index + 1) % Math.max(images.length, 1),
        };

        setItems((prev) => [
            ...prev.slice(-6),
            {
                id,
                src: image.src,
                x: e.clientX - rect.left,
                y: e.clientY - rect.top,
                rotate: Math.random() * 10 - 5,
            },
        ]);

        const timer = setTimeout(() => {
            setItems((prev) => prev.filter((item) => item.id !== id));
        }, 1200);
        timersRef.current.push(timer);
    };

    if (images.length === 0) return null;

    if (fallback) {
        return (
            <Slider
                slides={images.map((image) => ({ src: image.src, alt: image.alt || '', caption: image.label }))}
                autoPlay={3800}
                className={className}
                ariaLabel={ariaLabel}
            />
        );
    }

    return (
        <div
            ref={areaRef}
            className={`ui-trail ${className}`.trim()}
            onPointerMove={handleMove}
        >
            <AnimatePresence>
                {items.map((item) => (
                    <motion.img
                        key={item.id}
                        src={item.src}
                        alt=""
                        aria-hidden="true"
                        className="ui-trail__img"
                        style={{ left: item.x, top: item.y, rotate: item.rotate }}
                        initial={{ opacity: 0, scale: 0.55, x: '-50%', y: '-50%' }}
                        animate={{ opacity: 1, scale: 1, x: '-50%', y: '-50%' }}
                        exit={{ opacity: 0, scale: 0.92, x: '-50%', y: '-50%' }}
                        transition={{ duration: 0.28, ease: [0.33, 1, 0.68, 1] }}
                    />
                ))}
            </AnimatePresence>
            {hint && (
                <p className="ui-trail__hint">
                    <span className="ui-trail__hint-dot" aria-hidden="true" />
                    {hint}
                </p>
            )}
        </div>
    );
};

export default ImageTrail;

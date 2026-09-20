import { motion } from 'framer-motion';

/**
 * Появление блока при скролле: подъезд снизу на 30px + прозрачность,
 * длительность 0.8с, мягкий изинг. Движение только один раз.
 */
const Reveal = ({ children, delay = 0, y = 30, as = 'div', ...rest }) => {
    const Component = motion[as] || motion.div;

    return (
        <Component
            initial={{ opacity: 0, y }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: '-60px' }}
            transition={{ duration: 0.8, delay, ease: [0.33, 1, 0.68, 1] }}
            {...rest}
        >
            {children}
        </Component>
    );
};

export default Reveal;

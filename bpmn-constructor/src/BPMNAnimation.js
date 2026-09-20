import React, { useState, useEffect, useRef } from 'react';
import { motion } from 'framer-motion';
import './BPMNAnimation.css';

const BPMNAnimation = () => {
    const [elements, setElements] = useState([]);
    const [isVisible, setIsVisible] = useState(true);
    const animationRef = useRef(null);
    const stepRef = useRef(0);
    const containerRef = useRef(null);

    // Типы BPMN элементов
    const elementTypes = [
        { type: 'start', label: 'Start', shape: 'circle', color: '#00A550', icon: 'pi pi-play' },
        { type: 'task', label: 'Task', shape: 'rectangle', color: '#4CAF50', icon: 'pi pi-cog' },
        { type: 'gateway', label: 'Gateway', shape: 'diamond', color: '#F62369', icon: 'pi pi-exclamation-triangle' },
        { type: 'event', label: 'Event', shape: 'circle', color: '#2196F3', icon: 'pi pi-bell' },
        { type: 'subprocess', label: 'Subprocess', shape: 'rounded', color: '#9C27B0', icon: 'pi pi-th-large' },
        { type: 'end', label: 'End', shape: 'circle', color: '#FF5252', icon: 'pi pi-stop' }
    ];

    // Генерация элемента с учетом позиции в последовательности
    const generateElement = (type, index, total) => {
        const id = `element-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
        let x, y;

        if (type === 'start') {
            x = 15;
            y = 50;
        } else if (type === 'end') {
            x = 85;
            y = 50;
        } else {
            const segmentWidth = 60;
            const segmentStart = 20;
            x = segmentStart + (segmentWidth / (total + 1)) * (index + 1);
            y = 25 + Math.random() * 50;
        }

        return {
            id,
            type,
            label: elementTypes.find(t => t.type === type)?.label || type,
            shape: elementTypes.find(t => t.type === type)?.shape || 'circle',
            color: elementTypes.find(t => t.type === type)?.color || '#00A550',
            icon: elementTypes.find(t => t.type === type)?.icon || '',
            x,
            y
        };
    };

    // Запуск полного цикла анимации
    const startAnimation = () => {
        if (animationRef.current) {
            clearInterval(animationRef.current);
        }

        setElements([]);
        stepRef.current = 0;
        setIsVisible(true);

        animationRef.current = setInterval(() => {
            stepRef.current += 1;

            switch (stepRef.current) {
                case 1:
                    const startElement = generateElement('start', 0, 1);
                    setElements([startElement]);
                    break;

                case 2:
                    const numEvents = Math.floor(Math.random() * 3) + 2;
                    const newElements = [];
                    for (let i = 0; i < numEvents; i++) {
                        const eventTypes = ['task', 'gateway', 'event', 'subprocess'];
                        const randomType = eventTypes[Math.floor(Math.random() * eventTypes.length)];
                        const eventElement = generateElement(randomType, i, numEvents);
                        newElements.push(eventElement);
                    }
                    setElements(prev => [...prev, ...newElements]);
                    break;

                case 3:
                    const endElement = generateElement('end', 0, 1);
                    setElements(prev => [...prev, endElement]);
                    break;

                case 4:
                    setIsVisible(false);
                    break;

                case 5:
                    setElements([]);
                    break;

                case 6:
                    setIsVisible(true);
                    stepRef.current = 0;
                    break;

                default:
                    stepRef.current = 0;
                    break;
            }
        }, 2500);
    };

    // Запуск анимации при монтировании компонента
    useEffect(() => {
        // Запускаем анимацию только после полной загрузки страницы
        const timer = setTimeout(() => {
            startAnimation();
        }, 300);

        return () => {
            clearTimeout(timer);
            if (animationRef.current) {
                clearInterval(animationRef.current);
            }
        };
    }, []);

    // Остановка анимации при наведении
    const handleMouseEnter = () => {
        if (animationRef.current) {
            clearInterval(animationRef.current);
        }
    };

    // Возобновление анимации при уходе курсора
    const handleMouseLeave = () => {
        startAnimation();
    };

    // Функция для рендеринга элемента
    const renderElement = (element) => {
        const style = {
            left: `${element.x}%`,
            top: `${element.y}%`,
            backgroundColor: 'rgba(255, 255, 255, 0.9)',
            borderColor: element.color,
            boxShadow: `0 0 0 2px ${element.color}, 0 4px 12px rgba(0, 0, 0, 0.15)`
        };

        const iconStyle = {
            color: element.color
        };

        return (
            <motion.div
                key={element.id}
                className="bpmn-element"
                style={style}
                initial={{ scale: 0, opacity: 0 }}
                animate={{
                    scale: isVisible ? 1 : 0,
                    opacity: isVisible ? 1 : 0
                }}
                transition={{
                    duration: 0.6,
                    type: 'spring',
                    stiffness: 300,
                    damping: 20
                }}
                whileHover={{
                    scale: 1.1,
                    boxShadow: `0 0 0 4px ${element.color}, 0 6px 16px rgba(0, 0, 0, 0.2)`
                }}
                whileTap={{ scale: 0.95 }}
            >
                <div className={`bpmn-shape bpmn-${element.shape}`} style={{ borderColor: element.color }}>
                    <i className={element.icon} style={iconStyle}></i>
                </div>
                <div className="bpmn-label" style={{ color: element.color }}>
                    {element.label}
                </div>
            </motion.div>
        );
    };

    return (
        <div
            className="bpmn-animation-container"
            ref={containerRef}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
        >
            {/* Элементы BPMN */}
            {elements.map(element => renderElement(element))}

            {/* Интерактивные подсказки */}
            <div className="bpmn-hint">
                <i className="pi pi-mouse"></i>
                <span>Наведите курсор для остановки анимации</span>
            </div>
        </div>
    );
};

export default BPMNAnimation;
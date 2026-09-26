import Badge from './Badge';

/**
 * Балл `0` в модели — это «схему ещё не оценивали» (`app/models.py:85` держит
 * default=0), а не провал. Таким схемам не даём статусного цвета и не пишем
 * «0/100»: ягода кричит там, где кричать не о чем.
 */
export const describeScore = (score) => (typeof score === 'number' && score > 0
    ? { label: `${score}/100`, tone: score >= 80 ? 'green' : score >= 50 ? 'warning' : 'danger' }
    : { label: 'без оценки', tone: 'neutral' });

/** Метка качества схемы. Единственный источник тона — описания балла выше. */
const ScoreBadge = ({ score }) => {
    const { label, tone } = describeScore(score);
    return (
        <Badge size="sm" tone={tone}>
            {label}
        </Badge>
    );
};

export default ScoreBadge;

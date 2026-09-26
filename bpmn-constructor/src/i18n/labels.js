/**
 * Перевод служебных строк ИИ-контура на язык аналитика процесса.
 * Правила генератора заметок живут в core/bpmn_generator.py и оперируют
 * внутренними id (Task_0abc, Flow_1a2b3c) и английскими типами BPMN.
 */

export const ROLE_LABELS = {
    admin: 'Администратор',
    editor: 'Редактор',
    viewer: 'Наблюдатель',
};

export const roleLabel = (role) => ROLE_LABELS[role] ?? role;

const ELEMENT_TYPES = {
    task: 'задача',
    'sub process': 'подпроцесс',
    subprocess: 'подпроцесс',
    'call activity': 'вызываемый процесс',
    gateway: 'шлюз',
    'start event': 'стартовое событие',
    'end event': 'финишное событие',
    'intermediate event': 'промежуточное событие',
    event: 'событие',
    flow: 'переход',
    'sequence flow': 'переход',
    lane: 'дорожка',
    pool: 'пул',
};

export const elementTypeName = (raw) => ELEMENT_TYPES[String(raw).toLowerCase().trim()] ?? String(raw);

// Внутренний id элемента: заглавная буква, тип и хвост. Хвост короткий намеренно —
// генератор выдаёт и Task_3a, и Task_0abc; {3,} оставляло «Task_3a» в тексте.
const BPMN_ID = /\b[A-Z][a-zA-Z ]*_[0-9a-z]+\b/g;

/** Убирает идентификаторы модели из служебной строки. */
export const stripBpmnIds = (text) => text
    .replace(BPMN_ID, ' ')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .replace(/«\s*»|\s*→\s*$/g, '')
    .trim();

const SHAPES = [
    [/^Неизвестный тип «(.+?)» элемента \S+ → (\w+)$/, (_, from, to) => `Тип «${elementTypeName(from)}» не распознан — элемент стал: ${elementTypeName(to)}`],
    [/^Элемент \S+ без названия — подписан типом «(.+?)»$/, (_, kind) => `Элемент без названия подписан типом «${elementTypeName(kind)}»`],
    [/^Поток (\S+) → (\S+) удалён: (.+)$/, (_, a, b, why) => `Переход удалён: ${why}`],
    [/^Идентификатор элемента \S+ приведён к «(\S+)»$/, () => 'Идентификатор элемента исправлен'],
    [/^Элемент \S+: (.+)$/, (_, rest) => `Элемент: ${rest}`],
    [/^Дорожка «(.+?)» для элемента \S+ (.+)$/, (_, name, rest) => `Дорожка «${name}» ${rest}`],
    [/^У события \S+ («.+?»)? ?(.+)$/, (_, label, rest) => `Событие${label ? ` ${label}` : ''}: ${rest}`],
    [/^Хронометраж таймера \S+ (.+)$/, (_, rest) => `Таймер: ${rest}`],
];

/**
 * Переписывает заметку генератора в читаемую фразу. Неизвестный формат
 * не выдумывает перевод, а честно снимает идентификаторы.
 */
export const humanizeBpmnNote = (note) => {
    const text = String(note ?? '').trim();
    for (const [pattern, rewrite] of SHAPES) {
        const match = text.match(pattern);
        if (match) return rewrite(...match);
    }
    return stripBpmnIds(text);
};

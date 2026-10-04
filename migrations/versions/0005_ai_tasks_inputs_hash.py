"""Идемпотентность по входам запроса, снятие ссылки при удалении схемы, индексы обхода.

Ревизия 0004 закоммичена (`277ae56`) и применена на поднятом контуре, поэтому
править её на месте нельзя: база, где `alembic_version` уже стоит на 0004,
повторно её не применяет, и `inputs_hash` в такой базе просто не появится.
Дальше `POST /api/generate` падает `UndefinedColumn: column ai_tasks.inputs_hash
does not exist` на всех трёх ИИ-маршрутах, а `DELETE /api/diagrams/{id}` остаётся
неудаляемым из-за внешнего ключа. Дельта живёт здесь и обязана примениться поверх.

Что меняется и почему:

* `inputs_hash` входит в `uq_ai_tasks_idempotency`. Без него ключ
  идемпотентности — это имя пользователя экрана: фронтенд берёт новый ключ на
  действие, и повтор с ДРУГИМ описанием процесса вернул бы прошлую схему с
  пометкой `replayed` вместо новой работы. Колонка с `server_default=''` не
  косметика: в живой базе есть строки, а `NOT NULL` без значения база не
  примет. Пустой отпечаток не совпадёт ни с каким настоящим, то есть старые
  строки перестанут участвовать в сверке повторов — это честнее, чем объявить их
  повторами.
* `diagram_id` уходит в NULL вместе со схемой. Связь `AiTask.diagram`
  односторонняя и каскада по ORM не даёт, а `PRAGMA foreign_keys=ON`
  (`app/db.py`) значит, что SQLite исполняет внешний ключ так же ревниво, как
  PostgreSQL: без правила схема с одной лишь задачей в истории неудаляема.
* `ix_ai_tasks_user_status_created` и `ix_ai_tasks_status_started`: опрос и
  история читают «задачи пользователя такого-то статуса в порядке постановки»,
  обход потерянных выбирает по статусу и сроку.

SQLite не умеет ни `DROP CONSTRAINT`, ни переписывание безымянного внешнего
ключа: в 0004 он создан без имени. Поэтому здесь документированный приём —
`batch_alter_table` с `copy_from`: определение таблицы задаётся руками (рефлексия
пропущена), правки накладываются операциями, Alembic пересоздаёт таблицу и
переносит строки. Индексы в определение внесены обязательно: при пересоздании
Alembic берёт список индексов из него, и отсутствующий здесь индекс был бы просто
утерян. Колонку `inputs_hash` добавляет отдельный `op.add_column`, а не операция
внутри блока: пересоздание таблицы с одновременным изменением набора колонок
уходит в перетопирование порядка, которое на статическом определении
зацикливается. PostgreSQL идёт теми же операциями напрямую, без пересоздания.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Имя внешнего ключа на схему, данное ему PostgreSQL самостоятельно (в 0004 он
#: создан без имени). Настоящее имя читается из каталога (`_diagram_fk`), эта
#: строка — запасной вариант на случай, если каталог ничего не ответил.
_PG_FK_FALLBACK = 'ai_tasks_diagram_id_fkey'

#: То же ограничение под именем в статическом определении таблицы: на SQLite
#: пересоздание работает именно с ним, потому что определение пишет эта ревизия.
_SQLITE_FK = 'fk_ai_tasks_diagram_id'

#: Уникальность повтора названа явно ещё в 0004, поэтому на обеих СУБД её по
#: этому имени и сбрасывают.
_UQ = 'uq_ai_tasks_idempotency'

#: Индексы, которые существовали сразу после 0004.
_BASE_INDEXES = (
    ('ix_ai_tasks_id', ('id',)),
    ('ix_ai_tasks_user_id', ('user_id',)),
    ('ix_ai_tasks_status', ('status',)),
    ('ix_ai_tasks_diagram_id', ('diagram_id',)),
    ('ix_ai_tasks_created_at', ('created_at',)),
)

#: Что добавляет эта ревизия.
_NEW_INDEXES = (
    ('ix_ai_tasks_user_status_created', ('user_id', 'status', 'created_at')),
    ('ix_ai_tasks_status_started', ('status', 'started_at')),
)

_IDEMPOTENCY_COLUMNS = ('user_id', 'idempotency_key')


def _ai_tasks(*, wide_unique: bool, fk_ondelete: bool,
              new_indexes: bool) -> sa.Table:
    """`ai_tasks` на момент пересоздания: колонки уже после `add_column`.

    Нужна для `copy_from` на SQLite: отражённый безымянный внешний ключ нельзя ни
    сбросить по имени, ни переписать, поэтому отправной точкой служит текст этой
    ревизии, а не рефлексия базы. Флаги описывают только ограничения и индексы —
    набор колонок здесь одинаковый в обе стороны.

    Две вещи здесь не косметика, а следствие того, как Alembic переносит
    определение в DDL:

    * таблицы-источники `users` и `diagrams` объявлены в том же `MetaData`.
      Ссылка вида `'diagrams.id'` в чужом метадамме остаётся неразрешённой, и
      пересозданная таблица вышла бы без внешних ключей вовсе — молча, без
      ошибки. Проверено на живой базе: `PRAGMA foreign_key_list` пуст.
    * индексы строятся по объектам колонок (`table.c[...]`), а не по строкам и не
      присваиванием `.table`. Только так индекс попадает в `table.indexes`, откуда
      его забирает Alembic: иначе пересоздание таблицы снимает пять индексов 0004
      и не возвращает их обратно.
    """
    metadata = sa.MetaData()
    sa.Table('users', metadata, sa.Column('id', sa.Integer(), primary_key=True))
    sa.Table('diagrams', metadata,
             sa.Column('id', sa.String(length=36), primary_key=True))
    unique_columns = (list(_IDEMPOTENCY_COLUMNS) + ['inputs_hash']
                      if wide_unique else list(_IDEMPOTENCY_COLUMNS))
    table = sa.Table(
        'ai_tasks', metadata,
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('queue', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False,
                  server_default='queued'),
        sa.Column('idempotency_key', sa.String(length=120), nullable=True),
        sa.Column('inputs_hash', sa.String(length=64), nullable=False,
                  server_default=''),
        sa.Column('diagram_id', sa.String(length=36), nullable=True),
        sa.Column('base_seq', sa.Integer(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('result_ref', sa.String(length=64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(length=32), nullable=True),
        sa.Column('tries', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='fk_ai_tasks_user_id'),
        sa.ForeignKeyConstraint(
            ['diagram_id'], ['diagrams.id'], name=_SQLITE_FK,
            ondelete='SET NULL' if fk_ondelete else None),
        sa.PrimaryKeyConstraint('id', name='pk_ai_tasks'),
        sa.UniqueConstraint(*unique_columns, name=_UQ),
    )
    for index_name, columns in _BASE_INDEXES + (_NEW_INDEXES if new_indexes else ()):
        sa.Index(index_name, *[table.c[column] for column in columns])
    return table


def _diagram_fk(bind) -> str:
    """Настоящее имя внешнего ключа на `diagrams` в этой базе.

    Сверять его с шаблоном `<таблица>_<столбец>_fkey` нельзя: правка молча
    сбрасывала бы не то ограничение или падала на «не найдено». Каталог читается
    только на PostgreSQL — на SQLite ограничение приходит из определения выше.
    """
    if bind.dialect.name == 'sqlite':
        return _SQLITE_FK
    name = bind.execute(sa.text(
        "SELECT con.conname FROM pg_constraint AS con "
        "JOIN pg_class AS rel ON rel.oid = con.conrelid "
        "JOIN pg_class AS ref ON ref.oid = con.confrelid "
        "WHERE rel.relname = 'ai_tasks' AND ref.relname = 'diagrams' "
        "AND con.contype = 'f'")).scalar()
    return name or _PG_FK_FALLBACK


def _apply_constraints(batch_op, fk_name: str, *, forward: bool) -> None:
    """Правки ограничений и индексов; колонку трогает вызывающая сторона."""
    batch_op.drop_constraint(_UQ, type_='unique')
    batch_op.create_unique_constraint(
        _UQ,
        list(_IDEMPOTENCY_COLUMNS) + (['inputs_hash'] if forward else []))
    batch_op.drop_constraint(fk_name, type_='foreignkey')
    # В пакетном режиме у `create_foreign_key` нет аргумента «таблица-источник»
    # (она уже известна), а `remote_cols` — имена столбцов чужой таблицы, не
    # `'diagrams.id'`. Перестановка здесь молча роняет ограничение: на проверке
    # живой базы пересозданная таблица вышла без FK на `diagrams` вовсе, то есть
    # удаление схемы снова стало неудаляемым.
    batch_op.create_foreign_key(fk_name, 'diagrams', ['diagram_id'], ['id'],
                                ondelete='SET NULL' if forward else None)
    for index_name, columns in _NEW_INDEXES:
        if forward:
            batch_op.create_index(index_name, list(columns), unique=False)
        else:
            batch_op.drop_index(index_name)


def _migrate(*, forward: bool) -> None:
    bind = op.get_bind()
    fk_name = _diagram_fk(bind)
    if forward:
        op.add_column('ai_tasks', sa.Column('inputs_hash', sa.String(length=64),
                                           nullable=False, server_default=''))
    if bind.dialect.name == 'sqlite':
        # На прямом ходе ограниченийное состояние — «как в 0004», на обратном —
        # «как после этой ревизии»: набор колонок при этом одинаковый.
        with op.batch_alter_table('ai_tasks', copy_from=_ai_tasks(
                wide_unique=not forward, fk_ondelete=not forward,
                new_indexes=not forward)) as batch_op:
            _apply_constraints(batch_op, fk_name, forward=forward)
    else:
        with op.batch_alter_table('ai_tasks') as batch_op:
            _apply_constraints(batch_op, fk_name, forward=forward)
    if not forward:
        op.drop_column('ai_tasks', 'inputs_hash')


def upgrade() -> None:
    _migrate(forward=True)


def downgrade() -> None:
    _migrate(forward=False)

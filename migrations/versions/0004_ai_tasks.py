"""Задачи ИИ-контура: очередь, статус и результат долгих вызовов модели.

Таблица — источник истины о задаче (план
docs/plans/redis-cache-and-task-routing.md, раздел 4.3). Redis остаётся
транспортом: при его потере или сбросе пользователь видит статус и результат
из базы, а воркер не берёт задачу дважды благодаря условному переходу
`queued → running`.

Уникальность `(user_id, idempotency_key)` держит повторную отправку того же
запроса: без неё клиент, не дождавшийся ответа, создал бы вторую задачу и
вторую оплату вызова модели. Ограничение в базе, а не `SETNX` в Redis, —
намеренно: кэш можно сбросить, и дубль задачи появился бы вместе со сбросом.
NULL в `idempotency_key` ограничению не мешает (обе СУБД считают NULL
различными), поэтому задачи без ключа создаются сколько угодно.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_tasks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('queue', sa.String(length=32), nullable=False),
        # server_default остаётся, как в ревизиях 0002 и 0003: строка,
        # вставленная в обход моделей, не должна лечь без статуса и счётчика
        # попыток. В моделях дефолты питонские, `alembic check` на server_default
        # не смотрит (compare_server_default выключен в migrations/env.py).
        sa.Column('status', sa.String(length=16), nullable=False, server_default='queued'),
        sa.Column('idempotency_key', sa.String(length=120), nullable=True),
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
        sa.ForeignKeyConstraint(['diagram_id'], ['diagrams.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_ai_tasks_idempotency'),
    )
    with op.batch_alter_table('ai_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_tasks_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_tasks_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_tasks_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_tasks_diagram_id'), ['diagram_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_tasks_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('ai_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_tasks_created_at'))
        batch_op.drop_index(batch_op.f('ix_ai_tasks_diagram_id'))
        batch_op.drop_index(batch_op.f('ix_ai_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_ai_tasks_user_id'))
        batch_op.drop_index(batch_op.f('ix_ai_tasks_id'))
    op.drop_table('ai_tasks')

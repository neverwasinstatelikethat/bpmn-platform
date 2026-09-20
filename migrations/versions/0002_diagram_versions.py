"""Версии диаграмм и статусы предложений об улучшении.

История заводится и на существующих данных: каждая диаграмма получает версию 1
со своим текущим XML, иначе `base_seq` новых улучшений неоткуда отсчитывать.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20 17:35:42.768485

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('diagram_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('diagram_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('xml_content', sa.Text(), nullable=False),
    sa.Column('score', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['diagram_id'], ['diagrams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('diagram_id', 'seq', name='uq_diagram_versions_seq')
    )
    with op.batch_alter_table('diagram_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_diagram_versions_diagram_id'), ['diagram_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_diagram_versions_id'), ['id'], unique=False)

    with op.batch_alter_table('diagrams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('version_seq', sa.Integer(), nullable=False, server_default='0'))

    with op.batch_alter_table('pending_improvements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'))
        batch_op.add_column(sa.Column('base_seq', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('decided_at', sa.DateTime(), nullable=True))

    _backfill_history()

    # server_default остаётся осознанно: строка, вставленная в обход моделей,
    # не должна ложиться без статуса и номера версии. В моделях дефолты
    # питонские, поэтому `alembic check` на них не смотрит
    # (compare_server_default выключен в migrations/env.py).


def _backfill_history() -> None:
    """Версия 1 из текущего состояния каждой диаграммы.

    Идентификатор строки истории совпадает с id диаграммы: версии адресуются
    парой (diagram_id, seq), а генерировать UUID перекрёстным SQL-запросом —
    значит писать диалект-специфику (sqlite и postgres делают это по-разному).
    """
    op.execute(
        """
        INSERT INTO diagram_versions
            (id, diagram_id, seq, xml_content, score, source, author_id, created_at)
        SELECT id, id, 1, xml_content, COALESCE(score, 0), 'saved', user_id,
               COALESCE(updated_at, created_at)
        FROM diagrams
        WHERE xml_content IS NOT NULL
        """
    )
    op.execute("UPDATE diagrams SET version_seq = 1 WHERE xml_content IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('pending_improvements', schema=None) as batch_op:
        batch_op.drop_column('decided_at')
        batch_op.drop_column('base_seq')
        batch_op.drop_column('status')

    with op.batch_alter_table('diagrams', schema=None) as batch_op:
        batch_op.drop_column('version_seq')

    with op.batch_alter_table('diagram_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_diagram_versions_id'))
        batch_op.drop_index(batch_op.f('ix_diagram_versions_diagram_id'))

    op.drop_table('diagram_versions')

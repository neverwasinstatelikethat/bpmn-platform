"""Клеймо токенов, индексы под горячие выборки и уникальность членства в команде.

Три группы правок, объединённые одной ревизией осознанно: каждая меняет
только схему, ни одна не требует данных другой.

1. `users.token_version` — счётчик инвалидации JWT (см. app/deps.py):
   server_default '0' обязателен, иначе вставка строки в обход моделей (тесты
   миграций пишут SQL напрямую) падает на NOT NULL.
2. Индексы на внешних ключах горячих фильтров и на статусе предложений:
   реестр и очередь апрувов выбирали их последовательным сканированием.
3. UNIQUE (team_id, user_id) в team_members: повторное приглашение того же
   человека — дефект данных, а не вариант нормы. Сперва снимаем дубликаты,
   оставшиеся от легаси-баз: без этого создание ограничения падает, и
   `upgrade head` на живой базе останавливается на середине.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))

    _dedupe_team_members()

    with op.batch_alter_table('team_members', schema=None) as batch_op:
        batch_op.create_index('ix_team_members_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_team_members_team_id', ['team_id'], unique=False)
        batch_op.create_unique_constraint('uq_team_members_team_user', ['team_id', 'user_id'])

    with op.batch_alter_table('diagrams', schema=None) as batch_op:
        batch_op.create_index('ix_diagrams_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_diagrams_team_id', ['team_id'], unique=False)
        batch_op.create_index('ix_diagrams_folder_id', ['folder_id'], unique=False)

    with op.batch_alter_table('pending_improvements', schema=None) as batch_op:
        batch_op.create_index('ix_pending_improvements_diagram_id', ['diagram_id'], unique=False)
        batch_op.create_index('ix_pending_improvements_status', ['status'], unique=False)


def _dedupe_team_members() -> None:
    """Один человек — одна строка членства в команде.

    Из дубликатов остаётся строка с наименьшим id: id — случайный UUID,
    порядка он не несёт, но детерминизм важен: повтор прогона (или откат и
    новый upgrade) обязан удалить ровно те же строки. Дубликаты могли разойтись
    по роли, поэтому выжившей строке сначала отдаётся любое непустое role_id
    пары — иначе чистка молча оставила бы участника без роли.
    """
    conn = op.get_bind()
    # Коррелированная форма, а не UPDATE ... FROM: второй синтаксис — диалектик
    # PostgreSQL, а ревизии обязаны проходить и на SQLite (легаси-режим и
    # тесты прогона).
    conn.execute(sa.text(
        """
        UPDATE team_members
        SET role_id = (
            SELECT MAX(t.role_id) FROM team_members t
            WHERE t.team_id = team_members.team_id
              AND t.user_id = team_members.user_id
              AND t.role_id IS NOT NULL
        )
        WHERE role_id IS NULL AND EXISTS (
            SELECT 1 FROM team_members t2
            WHERE t2.team_id = team_members.team_id
              AND t2.user_id = team_members.user_id
              AND t2.role_id IS NOT NULL
        )
        """
    ))
    conn.execute(sa.text(
        """
        DELETE FROM team_members
        WHERE id NOT IN (
            SELECT MIN(id) FROM team_members
            WHERE team_id IS NOT NULL AND user_id IS NOT NULL
            GROUP BY team_id, user_id
        )
        AND team_id IS NOT NULL AND user_id IS NOT NULL
        """
    ))
    # Строки с NULL в командном или пользовательском ключе остаются: UNIQUE
    # считает NULLs различными, ограничение на них не спотыкается, а молча
    # удалить чужие сироты-строки — правка данных без повода в схеме.


def downgrade() -> None:
    with op.batch_alter_table('pending_improvements', schema=None) as batch_op:
        batch_op.drop_index('ix_pending_improvements_status')
        batch_op.drop_index('ix_pending_improvements_diagram_id')

    with op.batch_alter_table('diagrams', schema=None) as batch_op:
        batch_op.drop_index('ix_diagrams_folder_id')
        batch_op.drop_index('ix_diagrams_team_id')
        batch_op.drop_index('ix_diagrams_user_id')

    with op.batch_alter_table('team_members', schema=None) as batch_op:
        batch_op.drop_constraint('uq_team_members_team_user', type_='unique')
        batch_op.drop_index('ix_team_members_team_id')
        batch_op.drop_index('ix_team_members_user_id')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('token_version')

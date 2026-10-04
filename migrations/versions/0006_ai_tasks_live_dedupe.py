"""Живой дубликат задачи: уникальность по незавершённой работе, а не по ключу.

Идемпотентность держит ограничение `(user_id, idempotency_key, inputs_hash)` из
ревизии 0005, но фронтенд берёт новый ключ на каждое действие (`newIdempotencyKey`
в `bpmn-constructor/src/api/tasks.js`). Пока ключ разный, ограничение не срабатывает,
и двойное нажатие «Сгенерировать» — это две строки в очереди и две оплаты одного и
того же обращения к модели. Второй случай — сетевой обрыв: клиент не дождался
ответа, повторил запрос с новым ключом, а первая задача тем временем стоит в
очереди и будет исполнена.

Индекс закрывает оба случая предикатом «работа ещё не закрыта»:

* `status in ('queued', 'running')` — завершённая задача не мешает новому запуску
  с теми же входами (пользователь вправе переспросить модель), и повтор после
  отказа провайдера остаётся возможен: отказ — терминальный статус;
* `cancel_requested is false` — снятую задачу нельзя выдать за живой повтор,
  иначе отмена из интерфейса не отпускала бы входы и пользователь получил бы
  строку в статусе «отменена» вместо новой работы.

Отпечаток входов — `inputs_hash` из 0005, он включает `kind`, поэтому одинаковые
входы разных видов задач (генерация и оценка) не сталкиваются.

Отдельно о данных: предикатный уникальный индекс не создаётся, если в таблице уже
лежат два живых дубликата одних входов. Миграция не решает молча, какой из них
снять, — она падает с перечнем спорных ключей, чтобы решение осталось за
оператором.
"""
from typing import Any, Dict, List

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

INDEX = "uq_ai_tasks_live_inputs"
COLUMNS = ["user_id", "kind", "inputs_hash"]
# Предикат один и тот же для обеих dialect-строк: Alembic передаёт его дословно.
PREDICATE = "status in ('queued', 'running') and cancel_requested is false"


def upgrade() -> None:
    bind = op.get_bind()
    clash = _live_duplicates(bind)
    if clash:
        raise RuntimeError(
            "в ai_tasks есть живые задачи с одинаковыми входами — индекс не "
            f"создать: {clash}. Отмените или дождитесь лишних, затем повторяйте "
            "миграцию.")
    op.create_index(INDEX, "ai_tasks", COLUMNS, unique=True,
                    postgresql_where=sa.text(PREDICATE),
                    sqlite_where=sa.text(PREDICATE))


def downgrade() -> None:
    op.drop_index(INDEX, table_name="ai_tasks")


def _live_duplicates(bind: Any) -> List[Dict[str, Any]]:
    """Живые дубликаты, мешающие индексу (пустой список — создать можно)."""
    rows = bind.execute(sa.text(
        "select user_id, kind, inputs_hash, count(*) as n, min(id) as first_id "
        f"from ai_tasks where {PREDICATE} "
        "group by user_id, kind, inputs_hash having count(*) > 1")).fetchall()
    return [{"user_id": r[0], "kind": r[1], "inputs_hash": (r[2] or "")[:12],
             "count": r[3], "first_id": r[4]} for r in rows]

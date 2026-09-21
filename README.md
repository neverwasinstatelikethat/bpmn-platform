# BPMN-платформа ВкусВилл

Внутренняя экосистема бизнес-процессов: единое окно работы с BPMN-схемами и
единая точка их оптимизации. Схема генерируется по текстовому описанию через
ИИ, дальше её оценивает rule-based скоринг, а узкие места предлагаются к
исправлению операциями над XML — без генерации схемы целиком.

## Возможности

- **Генерация схем по описанию** — чат с LLM, потоковый вывод рассуждений,
  валидация структуры и формата BPMN.
- **Поиск узких мест и улучшение** — скоринг качества схемы + пакет операций
  правки (`core/bpmn_edits.py`), RAG по корпусу из 367 эталонных BPMN-схем.
  Принятие/отклонение предложения фиксируется как решение со статусом.
- **История версий схемы** — все правки хранятся последовательно, любую
  версию можно восстановить.
- **Командная работа** — команды, роли и приглашения по рабочей почте,
  реестр схем с папками, совместный доступ по ссылке (по умолчанию — только
  просмотр).
- **Апрувы изменений** — делегирование работы через роли и прием приглашений.

## Архитектура

```
main.py                  shim: uvicorn main:app
app/                     backend (FastAPI + SQLAlchemy)
  routers/               домены: auth, diagrams, folders, sharing, teams, ai
  services/              доступ, история версий, статусы решений об улучшении
  config.py              весь конфигурационный ввод — из окружения
core/                    AI-ядро
  bpmn_generator.py      генерация схемы, bpmn_scoring.py — скоринг
  llm_improve.py         улучшение, bpmn_edits.py — операции над схемой
  llm_client.py          транспорт и разбор ответов (GigaChat)
  bpmn_dataset/          корпус эталонов для RAG
bpmn-constructor/        frontend: React (CRA) + bpmn-js 18
  src/styles/tokens.css  дизайн-токены, вся вёрстка на них
tests/                   pytest: HTTP-контур, скоринг, аплайер операций, миграции
migrations/              Alembic (версионируемая схема)
```

СУБД задаётся `DATABASE_URL`: PostgreSQL — целевой режим, SQLite — легаси для
локальной разработки. Секреты (`SECRET_KEY`, SMTP, `GIGACHAT_CREDENTIALS`) читаются
только из окружения, в код не попадают. Локальный `python main.py` подхватывает
`.env` из корня проекта; в контейнер переменные передаёт compose.

## Запуск через Docker Compose

```bash
cp .env.example .env      # заполнить SECRET_KEY, POSTGRES_PASSWORD, GIGACHAT_CREDENTIALS
docker compose up -d      # backend 127.0.0.1:8765, frontend 127.0.0.1:3456
```

Схема поднимается миграциями Alembic, предопределённые роли сидируются при
старте, БД наружу не публикуется.

## Запуск локально

```bash
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
SECRET_KEY=... DATABASE_URL=sqlite:///./bpmn.db python main.py   # порт BACKEND_PORT, по умолчанию 8765

cd bpmn-constructor && npm install && npm start                  # dev-сервер на 3456
```

## Переменные окружения

| Переменная | Назначение |
| --- | --- |
| `DATABASE_URL` | строка подключения SQLAlchemy (PostgreSQL или SQLite) |
| `SECRET_KEY` | подпись JWT; обязателен при работе с PostgreSQL |
| `GIGACHAT_CREDENTIALS`, `GIGACHAT_SCOPE`, `GIGACHAT_VERIFY_SSL`, `GIGACHAT_MODEL`, `GIGACHAT_MAX_CONCURRENT` | провайдер LLM (GigaChat) |
| `AI_MAX_XML_CHARS`, `MAX_UPLOAD_BYTES`, `AI_REQUESTS_PER_HOUR` | лимиты ИИ-контура: размер схемы, размер загрузки, часовой бюджет запросов на пользователя |
| `MAIL_*` | SMTP для приглашений и сброса пароля |
| `BACKEND_PORT`, `FRONTEND_URL`, `REACT_APP_API_URL` | порты и адреса локального запуска |

Полный шаблон — `.env.example`.

## Тесты

```bash
python -m pytest tests/ -q
```

297 тестов: разбор ответов LLM, генерация структуры и починка плана модели,
аплайер операций (включая откат шагов, оставшихся вне маршрута), RAG-поиск по
эталонам, скоринг, HTTP-контур
генерации/улучшения/принятия, все маршруты (включая импорт `.bpmn` и сброс
пароля), миграции схемы. Сеть и ключ LLM не
нужны — LLM подставлен на уровне транспорта. Прогон на PostgreSQL — переменной
`TEST_DATABASE_URL`.

## Известные ограничения

- CORS открыт wildcard вместе с `allow_credentials` — закрывается списком из
  окружения одновременно с refresh-токенами.
- Кэш ответов LLM и вынос долгих ИИ-вызовов в воркер спроектированы, но не
  реализованы: `docs/plans/redis-cache-and-task-routing.md`.
- Планы по реархитектуре frontend и backend — в `docs/plans/`.

## Руководство для агентов

Репозиторий рассчитан на работу в связке с coding-agent: `AGENTS.md` описывает
рабочий порядок и цели, `.agents/guidelines/` — продуктовые термины,
архитектуру, frontend/backend-конвенции, безопасность и тестирование.

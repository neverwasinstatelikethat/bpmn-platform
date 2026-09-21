"""Точка запуска: `uvicorn main:app` (и CMD в Dockerfile) ждут приложение здесь.

Вся реализация — в пакете app/; этот модуль только переэкспортирует приложение.

Локальный запуск (`python main.py`, `uvicorn main:app`) подхватывает `.env`:
значения читаются на импорте `app.config`, поэтому загрузить файл нужно до
импорта пакета. В образ это не попадает — там переменные задаёт compose.
Тесты проходят мимо этого модуля и настраивают окружение в conftest.
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.main import app  # noqa: E402,F401

if __name__ == "__main__":
    import uvicorn

    from app.config import BACKEND_PORT

    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)

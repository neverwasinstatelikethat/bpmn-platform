"""Точка запуска: `uvicorn main:app` (и CMD в Dockerfile) ждут приложение здесь.

Вся реализация — в пакете app/; этот модуль только переэкспортирует приложение.
"""
from app.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    from app.config import BACKEND_PORT

    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)

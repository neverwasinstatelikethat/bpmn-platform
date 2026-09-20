"""SMTP-конфигурация почты (приглашения и сброс пароля)."""
import logging
import os

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema

logger = logging.getLogger(__name__)

# Настройка почты: значения берутся из переменных окружения (см. .env.example)
email_conf = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    # MAIL_FROM валидируется как e-mail: без значения приложение падало ещё на
    # импорте. Фолбэк совпадает с документированным значением из .env.example.
    MAIL_FROM=os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME") or "no-reply@vkusvill.ru"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", "465")),
    MAIL_SERVER=os.getenv("MAIL_SERVER", "imap.yandex.ru"),
    MAIL_STARTTLS=os.getenv("MAIL_STARTTLS", "true").lower() == "true",
    MAIL_SSL_TLS=os.getenv("MAIL_SSL_TLS", "false").lower() == "true",
    USE_CREDENTIALS=True
)

fast_mail = FastMail(email_conf)


async def send_email(message: MessageSchema) -> None:
    """Отправка письма со закрытым сбоем SMTP.

    Используется как фон после ответа клиенту: незакрытое исключение от
    недоступного сервера иначе падает воркер обработчика после успешного
    ответа.
    """
    try:
        await fast_mail.send_message(message)
    except Exception as e:  # noqa: BLE001 — доставка почты не роняет запрос
        logger.error("Письмо не отправлено (%s): %s", message.subtype, e)

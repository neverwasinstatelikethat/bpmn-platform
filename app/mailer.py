"""SMTP-конфигурация почты (приглашения и сброс пароля)."""
import logging
import os

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema

from app.config import _env_flag, _env_int

logger = logging.getLogger(__name__)

# Настройка почты: значения берутся из переменных окружения (см. .env.example)
email_conf = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    # MAIL_FROM валидируется как e-mail: без значения приложение падало ещё на
    # импорте. Фолбэк совпадает с документированным значением из .env.example.
    MAIL_FROM=os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME") or "no-reply@vkusvill.ru"),
    MAIL_PORT=_env_int("MAIL_PORT", 465),
    # 465 — это implicit TLS (SMTPS): handshake с STARTTLS на этом порту не
    # проходит, сервер отвечает на «STARTTLS» ошибкой, и письмо о сбросе пароля
    # не уходит, а пользователь видит «письмо отправлено». Поэтому пару флагов
    # читаем согласованно с портом: SSL/TLS включён, STARTTLS выключен.
    # Второй прежний дефект: `imap.yandex.ru` — адрес входящей почты, а не SMTP;
    # значение попадало в `.env.example` и в compose, то есть в штамп стека.
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.yandex.ru"),
    # Тот же класс бага, что снят в app/config.py: сравнение со строкой "true"
    # оставляло выключенным `MAIL_SSL_TLS=1` (как и `yes`, и `on`), а 465-й порт —
    # это implicit TLS: письмо о сбросе пароля не уходило, а экран отвечал
    # «письмо отправлено».
    MAIL_STARTTLS=_env_flag("MAIL_STARTTLS", "false"),
    MAIL_SSL_TLS=_env_flag("MAIL_SSL_TLS", "true"),
    USE_CREDENTIALS=True
)

fast_mail = FastMail(email_conf)


async def send_email(message: MessageSchema) -> bool:
    """Отправка письма с закрытым сбоем. Возвращает, дошло ли оно до сервера.

    Используется как фон после ответа клиенту: незакрытое исключение от
    недоступного сервера иначе падает воркер обработчика после успешного
    ответа. Молча проглоченный сбой — второй дефект того же места: клиент уже
    получил «письмо отправлено», а ссылки нет. Поэтому результат возвращается
    вызывающей стороне (она пишет его в счётчик и в лог), а текст лога держит
    стабильный маркер — по нему настраивается алерт, а не по шуму исключений.
    """
    try:
        await fast_mail.send_message(message)
        return True
    except Exception as e:  # noqa: BLE001 — доставка почты не роняет запрос
        logger.error("SMTP_DELIVERY_FAILED: письмо не отправлено (%s): %s",
                     message.subtype, e)
        return False

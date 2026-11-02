"""Почта: чтение через IMAP, отправка через SMTP (только с подтверждением).

Настройки в окружении/.env: EMAIL_ADDRESS, EMAIL_PASSWORD (пароль приложения),
IMAP_HOST, IMAP_PORT (993), SMTP_HOST, SMTP_PORT (465 — SSL, 587 — STARTTLS).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import os
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage

from ..tools import ToolRegistry
from .web import html_to_text

_MAX_BODY = 5_000


def _cfg(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"почта не настроена: задайте {name} в .env")
    return value


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def _body(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    text = part.get_content()
    return html_to_text(text) if part.get_content_type() == "text/html" else text


def register(registry: ToolRegistry) -> None:
    @registry.register
    def read_emails(limit: int = 5, unread_only: bool = True, folder: str = "INBOX") -> str:
        """Читает последние письма из почтового ящика (не помечает их прочитанными).

        limit: сколько писем вернуть (до 20)
        unread_only: только непрочитанные
        folder: папка, по умолчанию INBOX
        """
        with imaplib.IMAP4_SSL(_cfg("IMAP_HOST"), int(os.getenv("IMAP_PORT", "993"))) as imap:
            imap.login(_cfg("EMAIL_ADDRESS"), _cfg("EMAIL_PASSWORD"))
            imap.select(folder, readonly=True)
            _, data = imap.search(None, "UNSEEN" if unread_only else "ALL")
            ids = data[0].split()[-max(1, min(int(limit), 20)) :]
            if not ids:
                return "Писем нет"
            out = []
            for msg_id in reversed(ids):
                _, raw = imap.fetch(msg_id, "(BODY.PEEK[])")
                msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
                body = _body(msg).strip()
                out.append(
                    f"От: {_decode(msg['From'])}\nДата: {msg['Date']}\nТема: {_decode(msg['Subject'])}\n"
                    f"{body[:_MAX_BODY]}"
                )
            return "\n\n---\n\n".join(out)

    @registry.register(confirm=True)
    def send_email(to: str, subject: str, body: str) -> str:
        """Отправляет письмо. Пользователь подтверждает каждую отправку.

        to: адрес получателя (несколько — через запятую)
        subject: тема
        body: текст письма
        """
        msg = EmailMessage()
        msg["From"] = _cfg("EMAIL_ADDRESS")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        host, port = _cfg("SMTP_HOST"), int(os.getenv("SMTP_PORT", "465"))
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            server.login(_cfg("EMAIL_ADDRESS"), _cfg("EMAIL_PASSWORD"))
            server.send_message(msg)
        return f"Письмо отправлено: {to}"

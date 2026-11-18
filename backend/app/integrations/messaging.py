"""Email and SMS adapters (Phase 11).

* Email: SMTP (development: the bundled Mailpit server; production: the organisation's relay with
  STARTTLS and credentials). ``disabled`` records the notification as skipped.
* SMS: adapter only. The ``log`` provider records the message as sent without contacting a carrier,
  so nothing is ever sent by SMS until a real provider adapter is added (roadmap: adapters only).
"""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SendResult:
    status: str  # sent | failed | skipped
    provider: str
    message_id: str | None = None
    error: str | None = None


def _send_email_sync(settings: Settings, to: str, subject: str, body: str) -> SendResult:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="claimos.test")
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)
    return SendResult("sent", "smtp", str(msg["Message-ID"]))


async def send_email(settings: Settings, to: str, subject: str, body: str) -> SendResult:
    if settings.email_provider == "disabled":
        return SendResult("skipped", "disabled", error="email is disabled")
    try:
        return await asyncio.to_thread(_send_email_sync, settings, to, subject, body)
    except (OSError, smtplib.SMTPException) as exc:
        return SendResult("failed", "smtp", error=f"{type(exc).__name__}: {exc}"[:500])


async def send_sms(settings: Settings, to: str, body: str) -> SendResult:
    if settings.sms_provider == "disabled":
        return SendResult("skipped", "disabled", error="sms is disabled")
    message_id = f"log-{uuid.uuid4().hex[:12]}"
    # Never log the number or the text: only that a message would have been sent.
    logger.info("sms_logged", provider="log", message_id=message_id, length=len(body), to_len=len(to))
    return SendResult("sent", "log", message_id)

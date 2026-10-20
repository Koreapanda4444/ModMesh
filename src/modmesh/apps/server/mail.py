from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from email_validator import EmailNotValidError, validate_email

from modmesh.core.config import ServerSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError


def normalize_email(value: str) -> str:
    try:
        validated = validate_email(value, check_deliverability=False, allow_smtputf8=False)
        return str(validated.ascii_email or validated.normalized).casefold()
    except EmailNotValidError as exc:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid email address") from exc


class Mailer(Protocol):
    @property
    def ready(self) -> bool: ...

    def send(self, kind: str, recipient: str, token: str) -> None: ...


class SMTPMailer:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    @property
    def ready(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    def send(self, kind: str, recipient: str, token: str) -> None:
        settings = self.settings
        if not self.ready or settings.smtp_host is None or settings.smtp_from is None:
            raise ModMeshError(ErrorCode.STORAGE, "Configure SMTP email delivery on this server")
        message = EmailMessage()
        message["From"] = normalize_email(settings.smtp_from)
        message["To"] = normalize_email(recipient)
        message["Subject"] = "ModMesh " + kind
        message.set_content(
            f"ModMesh {kind}\n\nCode: {token}\n\n"
            "This code expires and can only be used once. Ignore it if you did not request it."
        )
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                smtp.ehlo()
                if settings.smtp_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if settings.smtp_username and settings.smtp_password:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise ModMeshError(
                ErrorCode.STORAGE, "Email delivery failed; request another code"
            ) from exc

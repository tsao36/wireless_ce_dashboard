"""Reusable SMTP sender configured from the project's .env.

Copy this file next to your app's .env, `pip install python-dotenv`, then:
    python smtp_mailer.py --check                   # connect, TLS, login, sender check; sends nothing
    python smtp_mailer.py --send-test you@example.com
In code:
    from smtp_mailer import load_smtp_settings, send_messages
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import smtplib
import ssl
import sys
from collections.abc import Iterator, Mapping
from email.message import EmailMessage
from pathlib import Path

from dotenv import dotenv_values

LOG = logging.getLogger("smtp_mailer")
SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "SMTP_SECURITY")
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_smtp_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """Non-empty process environment values win over .env; blank values never override it."""
    env_file = DEFAULT_ENV_FILE if env_file is None else Path(env_file)
    environ = os.environ if environ is None else environ
    file_values = dotenv_values(env_file) if env_file.is_file() else {}
    settings = {
        key: str(environ.get(key) or "").strip() or str(file_values.get(key) or "").strip()
        for key in SMTP_KEYS
    }
    if bool(settings["SMTP_USER"]) != bool(settings["SMTP_PASS"]):
        raise ValueError(
            "SMTP_USER and SMTP_PASS must be set together: set both for an authenticated relay, or neither for an unauthenticated one."
        )
    try:
        port = int(settings["SMTP_PORT"] or "587")
    except ValueError:
        raise ValueError("SMTP_PORT must be a number.") from None
    if not 0 < port < 65536:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")
    security = (settings["SMTP_SECURITY"] or ("ssl" if port == 465 else "starttls")).lower()
    if security not in {"starttls", "ssl", "none"}:
        raise ValueError("SMTP_SECURITY must be starttls, ssl, or none.")
    if security == "none" and settings["SMTP_USER"]:
        raise ValueError("SMTP_SECURITY=none cannot be used with SMTP_USER/SMTP_PASS because the login would be sent unencrypted.")
    settings.update(SMTP_PORT=port, SMTP_SECURITY=security)
    return settings


def require_sending_settings(settings: dict) -> None:
    if not (settings["SMTP_HOST"] and settings["SMTP_FROM"]):
        raise ValueError("SMTP_HOST and SMTP_FROM must be set in .env (see .env.example).")


@contextlib.contextmanager
def smtp_connection(settings: dict, timeout: int = 30) -> Iterator[smtplib.SMTP]:
    """Connected, encrypted (verified certificate) and logged-in SMTP session."""
    host, port, security = settings["SMTP_HOST"], settings["SMTP_PORT"], settings["SMTP_SECURITY"]
    context = ssl.create_default_context()
    if security == "ssl":
        connection = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=timeout)
    with connection as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        if settings["SMTP_USER"]:
            smtp.login(settings["SMTP_USER"], settings["SMTP_PASS"])
        yield smtp


def send_messages(messages: list[EmailMessage], settings: dict) -> int:
    """Send over one session; returns how many messages the server rejected."""
    failures = 0
    with smtp_connection(settings) as smtp:
        for message in messages:
            try:
                smtp.send_message(message)
                LOG.info("Sent email to %s", message["To"])
            except smtplib.SMTPException as exc:
                failures += 1
                LOG.error("Failed to send email to %s: %s", message["To"], exc)
    return failures


def check_connection(settings: dict) -> None:
    with smtp_connection(settings) as smtp:
        smtp.mail(settings["SMTP_FROM"])
        smtp.rset()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Check or test the SMTP settings in .env.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Connect, start TLS, log in and verify the sender; sends nothing.")
    group.add_argument("--send-test", metavar="ADDRESS", help="Send one test email to ADDRESS.")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to .env (default: next to this file).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        settings = load_smtp_settings(args.env_file)
        require_sending_settings(settings)
    except ValueError as exc:
        LOG.error("SMTP configuration error: %s", exc)
        return 1
    LOG.info(
        "host=%s port=%s security=%s from=%s login=%s",
        settings["SMTP_HOST"], settings["SMTP_PORT"], settings["SMTP_SECURITY"],
        settings["SMTP_FROM"], settings["SMTP_USER"] or "(none)",
    )
    try:
        if args.check:
            check_connection(settings)
            LOG.info("SMTP check passed: connected, TLS certificate verified, login and sender accepted.")
            return 0
        message = EmailMessage()
        message["Subject"] = "SMTP test"
        message["From"] = settings["SMTP_FROM"]
        message["To"] = args.send_test
        message.set_content("This is a test email from smtp_mailer.py. SMTP is configured correctly.")
        return 1 if send_messages([message], settings) else 0
    except (OSError, smtplib.SMTPException) as exc:
        LOG.error("SMTP %s failed: %s: %s", "check" if args.check else "send", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(_main())

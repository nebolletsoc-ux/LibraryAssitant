"""Small notification mailer built on the standard library.

Emails are opt-in and entirely server-configurable via environment variables:

    SMTP_HOST       required to enable sending (anything else = disabled)
    SMTP_PORT       default 587 (465 uses SSL instead of STARTTLS)
    SMTP_USER       login user; omit for an unauthenticated relay
    SMTP_PASSWORD   login password
    SMTP_FROM       From address; defaults to SMTP_USER

No third-party SDKs; sends never raise to the caller (failures are logged).
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _smtp_config():
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": (os.environ.get("SMTP_USER") or "").strip() or None,
        "password": os.environ.get("SMTP_PASSWORD") or None,
        "from_addr": (
            os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or host
        ).strip(),
    }


def is_enabled():
    """True when SMTP credentials are configured (sending possible)."""
    return _smtp_config() is not None


def _deliver(cfg, msg):
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20) as smtp:
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)


def send_email(to, subject, html=None, text=""):
    """Send one message. Returns True on success, never raises."""
    ok, _ = send_email_report(to, subject, html, text)
    return ok


def send_email_report(to, subject, html=None, text=""):
    """Send one message, returning (ok, detail) so callers can show errors.

    The periodic sends stay fire-and-forget, but the Settings "Send test
    email" button uses this to surface a real SMTP error to the user.
    """
    cfg = _smtp_config()
    if not cfg:
        logger.info("Email skipped: SMTP_HOST not configured (to=%s subject=%s)", to, subject)
        return False, "SMTP is not configured (SMTP_HOST unset)"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    if text:
        msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        _deliver(cfg, msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True, "sent"
    except Exception as e:  # noqa: BLE001 - a mail failure must never break a scan
        logger.error("Email failed to %s (%s): %s", to, subject, e)
        return False, str(e)


def submit_email(to, subject, html=None, text=""):
    """Queue a send on a daemon thread so scanning requests aren't slowed down."""
    if not is_enabled():
        logger.info("Email skipped: SMTP_HOST not configured (to=%s subject=%s)", to, subject)
        return
    threading.Thread(
        target=send_email, args=(to, subject, html, text), daemon=True
    ).start()
"""Notification mailer. Two backends, auto-selected:

1. Resend (HTTPS API) — used when RESEND_API_KEY is set. Works on hosts that
   block raw SMTP ports (e.g. Render free instances), exposing only HTTPS.
   Send from a verified domain via EMAIL_FROM, or leave it unset to send from
   Resend's onboarding@resend.dev (Restricted mode: you can only send to your
   own account address until a domain is verified).

2. SMTP (stdlib smtplib) — used otherwise, when SMTP_HOST is set:
       SMTP_HOST  SMTP_PORT (587 default; 465 uses SSL)  SMTP_USER
       SMTP_PASSWORD  SMTP_FROM

With neither configured, sending is a no-op. Sends never raise to the caller;
failures are logged and (for the test button) returned to the UI.
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def _resend_key():
    return (os.environ.get("RESEND_API_KEY") or "").strip() or None


def _smtp_config():
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": (os.environ.get("SMTP_USER") or "").strip() or None,
        "password": os.environ.get("SMTP_PASSWORD") or None,
        "from_addr": _from_addr() or host,
    }


def _from_addr():
    for var in ("EMAIL_FROM", "SMTP_FROM"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def is_enabled():
    """True when a send path (Resend API or SMTP) is configured."""
    return bool(_resend_key() or _smtp_config())


def _send_via_smtp(to, subject, html, text):
    cfg = _smtp_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    if text:
        msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
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


def _send_via_resend(to, subject, html, text):
    payload = {
        "from": _from_addr() or "onboarding@resend.dev",
        "to": [to],
        "subject": subject,
    }
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html

    resp = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {_resend_key()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    if resp.status_code != 200:
        message = resp.json().get("message") if resp.content else ""
        raise RuntimeError(f"Resend API HTTP {resp.status_code}: {message or resp.text[:200]}")


def send_email(to, subject, html=None, text=""):
    """Send one message. Returns True on success, never raises."""
    ok, _ = send_email_report(to, subject, html, text)
    return ok


def send_email_report(to, subject, html=None, text=""):
    """Send one message, returning (ok, detail) so callers can show errors.

    The periodic sends stay fire-and-forget; the Settings "Send test email"
    button uses this to surface a real error to the user.
    """
    try:
        if _resend_key():
            _send_via_resend(to, subject, html, text)
            logger.info("Email sent to %s: %s", to, subject)
            return True, "sent"
        if _smtp_config():
            _send_via_smtp(to, subject, html, text)
            logger.info("Email sent to %s: %s", to, subject)
            return True, "sent"
        logger.info("Email skipped: no RESEND_API_KEY or SMTP_HOST (to=%s subject=%s)", to, subject)
        return False, "Email is not configured (set RESEND_API_KEY or SMTP_HOST)"
    except Exception as e:  # noqa: BLE001 - a mail failure must never break a scan
        logger.error("Email failed to %s (%s): %s", to, subject, e)
        return False, str(e)


def submit_email(to, subject, html=None, text=""):
    """Queue a send on a daemon thread so scanning requests aren't slowed down."""
    if not is_enabled():
        logger.info("Email skipped: no RESEND_API_KEY or SMTP_HOST (to=%s subject=%s)", to, subject)
        return
    threading.Thread(
        target=send_email, args=(to, subject, html, text), daemon=True
    ).start()
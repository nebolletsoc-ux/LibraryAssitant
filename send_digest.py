"""Send the weekly "available books" digest to every subscribed user.

Wire this into a scheduler (e.g. a Render Cron Job, macOS launchd, a crontab)
to run on whatever cadence suits. Sends via the same SMTP config as the app:

    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM DATABASE_URL

Run once directly:

    ./.venv/bin/python send_digest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, _run_weekly_digest  # noqa: E402

with app.app_context():
    count = _run_weekly_digest()

print(f"Digest emails sent: {count}")
# MyNextRead (Library Assistant)

A small, password-protected personal reading list that checks public library
catalogs and shows you where each book is available to borrow.

**Live app:** https://libraryassitant.onrender.com

## Features

- Import your to-read list from a Goodreads-style CSV (or a StoryGraph export)
- Add books via Open Library search
- Check availability across multiple public-library catalogs:
  OverDrive/Libby, Bibliocommons, and Hoopla
- Manual per-book "Check availability" with a full re-scan of the whole list
- Book detail sheet with cover art, synopsis (fetched and cached from Open
  Library when missing), and per-library availability rows
- Optional email notifications: "this book became available" alerts and a
  weekly availability digest (via Resend or any SMTP server)
- Multiple independent accounts, each with its own reading list and library
  preferences
- Data export (CSV) and full account deletion built in

## Stack

- Backend: Python 3 / Flask, SQLAlchemy, SQLite for local use (PostgreSQL on
  any production host)
- Frontend: a single-page interface in `templates/tbr.html` (no build step)
- Scheduling: the weekly digest can be driven by a cron job
  (`send_digest.py`) or an HTTP webhook (see `cron.example`)

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. A local SQLite database is created at
`instance/library_assistant.db` on first run.

## Environment variables

| Variable                | Required | Purpose                                                  |
| ----------------------- | -------- | -------------------------------------------------------- |
| `DATABASE_URL`          | no\*     | SQLAlchemy URL; defaults to local SQLite                 |
| `SECRET_KEY`            | no\*     | Session signing; auto-generated and persisted if absent  |
| `APP_PASSWORD`          | no       | Shared HTTP-basic gate for a private deployment          |
| `RESEND_API_KEY`        | no       | Email via Resend                                         |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | no | Email via SMTP                         |
| `EMAIL_FROM`            | no       | Sender for Resend once a domain is verified              |
| `CRON_TOKEN`            | no       | Shared secret for the `/api/system/digest` webhook       |
| `SENTRY_DSN`            | no       | Enables exception reporting via Sentry                   |
| `ADOPT_LEGACY_ROWS`     | no       | `1` lets the first signup claim pre-accounts data        |
| `SESSION_COOKIE_SECURE` | no       | `0` disables Secure session cookies behind plain HTTP    |

\* On a public deployment set both `DATABASE_URL` (Postgres) and `SECRET_KEY`.

## Tests

```bash
python -m pytest
```

The suite mocks the network calls to library catalogs and Open Library, so it
runs offline.

## Terms & privacy

- https://libraryassitant.onrender.com/terms
- https://libraryassitant.onrender.com/privacy

## License

MIT — see `LICENSE`.
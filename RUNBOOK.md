# Operator runbook — public launch

Everything that needs a human, in order. Code-side items are already done and
pushed (`639c943`, branch `phase1-standalone-tbr`). App: https://libraryassitant.onrender.com

Current Mac crontab (the ONLY place today that sends mail), a single line:

```
0 9 * * 1 cd /Users/orangeimac/Documents/LibraryAssistant && DATABASE_URL="..." RESEND_API_KEY="..." ./.venv/bin/python send_digest.py >> /tmp/mynextread_digest.log 2>&1
```

The `DATABASE_URL` and `RESEND_API_KEY` in that line must stay in sync with
Render after every rotation below.

---

## Step 1 · Rotate every exposed secret (do this first)

All three appeared in chat, so treat them as compromised.

**a) Resend API key** — https://resend.com → API Keys → create a new key,
delete the old one.
**b) Neon DB password** — https://console.neon.tech → your project → Settings →
Database → "New password". Keep the same user/database; the URL's password part
changes.
**c) Gmail app password (if any SMTP fallback still uses it)** — Google Account →
Security → App passwords → create a new one; click **Revoke** on every old one.
If nothing uses SMTP anymore, just revoke.

After a–c, update both places:

1. Render → your service → **Environment**:
   - `DATABASE_URL` = new Neon URL
   - `RESEND_API_KEY` = new key
   - Save → **Deploy** (it auto-redeploys on env save).
2. Mac cron: `crontab -e`, replace the two values in the line above, save.
3. Verify: open the app → Settings → **Send test email**. Then run the digest
   once by hand:
   `DATABASE_URL="<new>" RESEND_API_KEY="<new>" ./.venv/bin/python send_digest.py`

---

## Step 2 · Put a stable SECRET_KEY in Render (before anyone else signs up)

Sessions are signed with a secret that today lives in the DB (fine), but an
explicit env value is stable across database resets.

```
python -c "import secrets; print(secrets.token_hex(32))"
```

Add the output to Render → Environment as `SECRET_KEY`, save/deploy. Do this
now so early account sessions don't get invalidated later. Changing it later
simply logs everyone out.

---

## Step 3 · Web-tier email (Step 2 of roadmap)

Add to Render → Environment:

- `RESEND_API_KEY` (new key from Step 1) — copy the same value you put back in
  the crontab
- `EMAIL_FROM` = `MyNextRead <me@yourdomain>` once the sender domain is
  verified (Step 5 below). Until then you may leave it unset — the app will
  plainly warn at boot and send from `onboarding@resend.dev` (fine for emailing
  yourself).

Save → deploy → verify with **Send test email** in Settings.

---

## Step 4 · Cloud digest schedule (Step 3 of roadmap)

1. Create the shared token:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Add it to Render → Environment as `CRON_TOKEN`. Save/deploy.
3. Verify the webhook manually:
   `curl -fsS -X POST -H "Authorization: Bearer <token>" https://libraryassitant.onrender.com/api/system/digest`
   Expect `{"digest_sent": N}`. Wrong/missing token → 401; endpoint is 404
   while `CRON_TOKEN` is unset.
4. Create a cloud schedule (**pick one**):
   - **Render Cron Job** (recommended): Dashboard → **New → Cron Job** →
     repo `nebolletsoc-ux/LibraryAssitant`, branch `phase1-standalone-tbr`,
     build `pip install -r requirements.txt`, run command
     `curl -fsS -X POST -H "Authorization: Bearer $CRON_TOKEN" https://libraryassitant.onrender.com/api/system/digest`,
     schedule `0 9 * * 1`. Setup its own env `CRON_TOKEN` (Render cron jobs do
     not inherit the web service's env).
   - **cron-job.org**: new job → *Request type* POST → URL
     `https://libraryassitant.onrender.com/api/system/digest` → header
     `Authorization: Bearer <token>` → schedule Monday 09:00.
5. Watch one Monday, then **remove the Mac crontab line** (`crontab -e`) so the
   digest isn't sent twice. Keep it until the cloud job has proven itself.

---

## Step 5 · Sender domain verification (freedns)

Follow `cron.example` → "Optional: free custom sender domain" exactly:
1. https://freedns.afraid.org → register a free subdomain (`mynextread.mooo.com`).
2. Resend → **Domains → Add Domain** → enter the subdomain; copy the ~3 DNS
   records (verification TXT, SPF TXT, DKIM CNAME/TXT).
3. Add each record in afraid.org.
4. Wait for verification (usually minutes), then set in Render → Environment:
   `EMAIL_FROM=MyNextRead <me@mynextread.mooo.com>` → deploy.

---

## Step 6 · GitHub Issues + templates (Step 8 of roadmap)

1. https://github.com/nebolletsoc-ux/LibraryAssitant → **Settings → General** →
   under Features tick **Issues**.
2. Add an issue template (a `bug_report.md`): say the word and opencode will
   add the `.github/ISSUE_TEMPLATE/` files and commit them.

---

## Step 7 · Clean the test accounts off the live DB

Live DB currently has: `benc` (id 1, real, 134 books — safe, see note), plus
two leftover test accounts you created earlier. Delete them and their rows
(books shared with you stay; fully-orphaned books go too):

```
psql "$DATABASE_URL" <<'SQL'
BEGIN;
DELETE FROM availability  WHERE book_id IN (SELECT book_id FROM user_books WHERE user_id IN (2,3));
DELETE FROM user_books    WHERE user_id IN (2,3);
DELETE FROM library_config WHERE user_id IN (2,3);
DELETE FROM users         WHERE id IN (2,3);
DELETE FROM books         WHERE NOT EXISTS (SELECT 1 FROM user_books ub WHERE ub.book_id = books.id);
COMMIT;
SQL
```

(Substitute your new Neon URL for `$DATABASE_URL`. Re-run the last `DELETE`
alone if you ever merge duplicate books again.)

**Adoption safety (roadmap Step 7) — already handled:** user `benc` owns the
user_id=1 rows, so a new signup is never the "first user" and the default
`ADOPT_LEGACY_ROWS=0` means nothing can ever adopt them. No action needed.

---

## Step 8 · Observability + backups (Step 10 of roadmap)

1. **UptimeRobot** — https://uptimerobot.com → Add Monitor → HTTPS, target
   `https://libraryassitant.onrender.com`, 5-min interval. Alert when down.
2. **Sentry** — https://sentry.io → new project, copy DSN → add it to Render →
   Environment as `SENTRY_DSN` → save/deploy. The app now wires Sentry
   automatically when that env var is present (see `app.py`); no other code
   needed. Test it by triggering a deliberately broken route or watching for
   your first event after a deploy.
3. **Neon backups** — console.neon.tech → project → **Settings** → confirm
   "Time travel" / daily backups are ON (default for branches; prod needs a
   branch to restore from). Optional: run a manual backup before launch.
4. Skim Render → Logs weekly for the first month (look for 429 spam, import
   errors, 500s).

---

## Step 9 · Custom domain + branding (Step 9 of roadmap, optional)

1. Point a real domain (or another freedns subdomain) at the service: Render →
   service → **Settings → Custom Domains** → add → verify the DNS record Render
   shows → TLS auto-provisions (Let's Encrypt).
2. Once it works, reuse the domain for `EMAIL_FROM` (add it as a Resend domain)
   and update the `/terms` and `/privacy` contact links (they currently point
   at the GitHub repo).

---

## Step 10 · Soft launch (Step 12 of roadmap)

1. Closed beta: send the link to a handful of friends.
2. Watch auth logs (registers, 429 lockouts) and scan error logs for a few days.
3. When stable — and only after Steps 1–4 are confirmed — announce and open
   Issues. Don't announce while any step above is still open.
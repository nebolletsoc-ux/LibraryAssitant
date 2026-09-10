# Operator runbook — public launch

Everything that needs a human, in order. Code-side items are done and pushed
(branch `phase1-standalone-tbr`, latest `e6d108d`). App:
https://libraryassitant.onrender.com

The weekly digest is scheduled in the cloud by **GitHub Actions**
(`.github/workflows/digest.yml`, Mondays 09:00 UTC), which fires the
`POST /api/system/digest` webhook with the `CRON_TOKEN` repo secret. The old
Mac crontab line was removed, so secrets now live only in Render + GitHub.
Rotations below touch Render, plus the repo secret only if you rotate
`CRON_TOKEN`.

---

## Step 1 · Rotate every exposed secret (do this first)

All three appeared in chat, so treat them as compromised.

**a) Resend API key** — https://resend.com → API Keys → create a new key,
delete the old one.
**b) Neon DB password** — https://console.neon.tech → your project → in the
**left sidebar**, use the **BRANCH** selector to pick the branch your app
connects to (e.g. Production) → under the **Postgres database** sidebar group
select **Roles** → find the role your connection uses (the project's role is
named after the database, e.g. `neondb_owner`) → its **⋯ menu → Reset
password** → **Reset**. No "Roles" entry in the sidebar? Use the Project
Dashboard → **Connect** → role selector in the connection-string widget →
Reset password.

The password shows only once — copy it, and the new `DATABASE_URL` from the
same Connect widget (host/db/role stay the same; only the password changes).
The old password stops working immediately, so update Render right after
resetting (a brief reconnect on the next request is expected).
**c) Gmail app password (if any SMTP fallback still uses it)** — Google Account →
Security → App passwords → create a new one; click **Revoke** on every old one.
If nothing uses SMTP anymore, just revoke.

After a–c, update Render (the Mac crontab is retired — don't recreate it):

1. Render → your service → **Environment**:
   - `DATABASE_URL` = new Neon URL
   - `RESEND_API_KEY` = new key
   - Save → **Deploy** (it auto-redeploys on env save).
2. Verify: open the app → Settings → **Send test email**, then fire the digest
   once by hand:
   `curl -fsS -X POST -H "Authorization: Bearer $CRON_TOKEN" https://libraryassitant.onrender.com/api/system/digest`
   (or use the GitHub Actions *Run workflow* button).

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

Email meanwhile is fully live from the web tier: alerts, digest, and now the
**click-link email verification** (code shipped 09 Sep 2026) all send through
Resend. On a new/changed address, Settings emails a verification link; alerts
and the digest only go to **verified** addresses. The live `benc` account was
grandfathered verified on migration — only new/changed addresses need a click.

Add to Render → Environment:

- `RESEND_API_KEY` (new key from Step 1)
- `EMAIL_FROM` = `MyNextRead <me@yourdomain>` once the sender domain is
  verified (Step 5 below). Until then you may leave it unset — the app will
  plainly warn at boot and send from `onboarding@resend.dev` (fine for emailing
  yourself; verification links arrive from that address too).
- `GOOGLE_BOOKS_API_KEY` (optional) — free Google Books API key so synopses
  missing from Open Library fall back to Google. The key can be created and
  API-restricted to "Books API" only (already in use locally; add to Render to
  make it live).

Save → deploy → verify with **Send test email** in Settings.

---

## Step 4 · Cloud digest schedule (Step 3 of roadmap) — DONE

- `CRON_TOKEN` set in Render (web tier) and as the GitHub Actions repo secret.
- Webhook verified: `curl -fsS -X POST -H "Authorization: Bearer $CRON_TOKEN" https://libraryassitant.onrender.com/api/system/digest` → `{"digest_sent": 1}`.
- Scheduler: **GitHub Actions** (`.github/workflows/digest.yml`), Mondays 09:00
  UTC. Free on public repos (Render Cron Jobs are paid-plan only). Tested via a
  manual *Run workflow* run — success.
- Mac crontab line **removed** — no double sends. If you ever rotate
  `CRON_TOKEN`, update both Render and the repo secret.

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

Live DB currently has: `benc` (id 1, real, 134 books — safe, see note) and
`cathyhchou@yahoo.com` (id 4, input by a friend during the closed beta; keep).
Delete any OTHER leftover test accounts and their rows (books shared with you
stay; fully-orphaned books go too):

```
psql "$DATABASE_URL" <<'SQL'
BEGIN;
DELETE FROM availability  WHERE book_id IN (SELECT book_id FROM user_books WHERE user_id IN (<ids>));
DELETE FROM user_books    WHERE user_id IN (<ids>);
DELETE FROM library_config WHERE user_id IN (<ids>);
DELETE FROM users         WHERE id IN (<ids>);
DELETE FROM books         WHERE NOT EXISTS (SELECT 1 FROM user_books ub WHERE ub.book_id = books.id);
COMMIT;
SQL
```

(Substitute your new Neon URL for `$DATABASE_URL`, and the ids to drop for
`<ids>`. Re-run the last `DELETE` alone if you ever merge duplicate books
again.)

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
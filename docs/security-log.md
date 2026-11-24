# The security log

The NVR writes a row for nearly every sensitive action: changes to accounts and how they sign in, second factors set up or reset, API tokens made or revoked, camera access granted, settings changed, sign-ins that were slowed, and commands run from the NVR host. Who *watched* which camera is separate: that is the [viewing audit](viewing-audit.md).

## Reading it

**Settings › Security log** (administrators only) lists the rows for a date range, newest first, 100 to a page. Narrow it by **person** (who did it) and by **action**. A row with no person was done by the NVR itself, from the NVR host, or by someone who was not signed in (a throttled sign-in, for example). **Download as CSV** gives every row that matches, up to 100,000; cells that a spreadsheet would run as a formula are neutralised, and the download is itself recorded.

The same is available as `GET /api/admin/security-log` (`from`, `to`, `user_id`, `action`, `limit`, `offset`) and `GET /api/admin/security-log/export.csv`.

## How long it is kept

Two years (730 days) unless changed: **Security log retention** on the same page takes 30 to 3,650 days (`GET`/`PUT /api/admin/security-log/settings`). Older rows are removed by the same background pass that applies footage retention, every few minutes; a pass with nothing to remove does not write to the database. Changing the period is recorded, with how many rows it removed.

## What is and is not in it

- Passwords, token secrets, recovery codes and camera credentials are never written, only that something changed.
- A username that belongs to nobody is kept as a keyed digest (`unknown:…`), not as text: people type their password into the username box by mistake. The same text gives the same digest, so repeated attempts still show as one thing.
- Using an API token does not add a row. Its last-used time, shown in **Settings › Users & access**, is the record; a row per use used to add up to 1,440 rows a day for one token that is polled constantly.
- An administrator ending someone's session (`session_ended`), disabling or enabling an account (`user_disabled`, `user_enabled`), a password change (`password_changed`) and a change to the site's date and time display (`display_settings_changed`) are recorded.
- **Searches** (#519): a licence plate search (`search_plate`, with the pattern), an attribute search (`search_attributes`, with its filters), a search in words (`search_words`, with the words) and **Find similar** (`search_similar`, with the object it started from) are recorded with the person, the cameras searched and the time range. Never a picture or an embedding. The same person repeating the same search within a minute adds no row. A plate someone searched for is personal data, so it is kept here, which only administrators read, and nowhere else. The row is written just after the search answers, so the search never waits for it.

- **Sign-ins** (#459): every completed sign-in is a `signed_in` row with its `method` (`password`, `password+totp`, `password+recovery`,
  `password+passkey`, `passkey`, `entra`, `setup` for the first-run administrator, `phone` for the app resuming), the `address`, and
  `network`: `local` or `remote`, as the sign-in limits counted it. A video wall signing in is `wall_signed_in`, with its token's name.
- **Failed sign-ins** (#459): a wrong password for an account that exists is a `sign_in_failed` row, for the first failure of a run (none for that
  account in the last 15 minutes). Each time a throttle threshold is crossed there is an `auth_throttled` row, as before. Together they are
  bounded to 10 rows per account per hour, so guessing cannot fill the log. A username that belongs to nobody never gets a `sign_in_failed`
  row, and appears only as its digest in `auth_throttled`. A wrong second-factor code is `mfa_failed`, as before.
- **Entra accounts** (#459): `entra_account_created` the first time someone signs in through Entra, and `entra_role_changed` (with `before` and
  `after`) when their role assignment in Entra changed since their last sign-in.
- **Recording settings** (#459), each with the value `before` and `after` (`null`: none of its own, so the group's or the default applies):
  `retention_changed` and `schedule_changed` for a camera group or a camera; `camera_group_members_changed` (also `added` and `removed`);
  `camera_group_deleted`, with its name, members, retention and schedule. Saving the same value again adds no row.

## For developers

`app.audit.record` writes a row in the caller's transaction; `app/security_log.py` reads and prunes them; `app/api/security_log.py` holds the routes. Migration 70 adds the retention setting and the indexes the person and action filters use. Tests: `tests/test_security_log.py`, `tests/browser/security-log.spec.cjs`.

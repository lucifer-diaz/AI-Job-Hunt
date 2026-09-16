# Focused settings editors

Settings uses a section sidebar on desktop and a native section selector below 900px. The existing `#settings` URL opens Profile. Deep links use `#settings/profile`, `preferences`, `resumes`, `account`, `api-keys`, `ai`, and `workflow`.

## Frontend boundaries

`app/static/settings.js` owns section renderers, baseline comparisons, partial payloads, validation, and settings actions. It loads before the existing workspace script and uses its shared form, API, routing, and rendering helpers. No production JavaScript build is required. `app.css` scopes the new layout under settings classes.

Profile, preferences, resume text, AI, and workflow drafts are independent. Ordinary drafts survive section changes; navigating out of Settings or reloading warns before losing edits. API-key and password drafts are memory-only and are cleared when leaving their section, cancelling, or saving successfully. Saving disables that editor and blocks section navigation until the request finishes. Inline errors retain drafts for retry.

The UI saves changed fields only. Pending chip input is committed before saving, including locations with commas. Valid numeric zero values count toward completeness. Uploading or selecting a primary resume warns before replacing unsaved master text. Parsing evidence requires saved text. Advanced AI overrides and master text are collapsible.

## API contracts

All routes require the existing signed-in session and same-origin write protections. OpenAPI at `/openapi.json` includes the new input schemas.

| Route | Behavior |
| --- | --- |
| `PATCH /api/profile` | Supplied fields only; merge supplied `search_preferences` entries. Returns the updated profile. Empty strings and lists explicitly clear fields. `master_resume: null` is invalid. |
| `PATCH /api/settings` | Supplied AI and approval settings only. Stage models merge by stage; a blank stage removes its override. Blank provider/model and `require_free: null` restore inheritance. Returns the same settings representation as GET. |
| `PUT /api/preferences` | Now preserves omitted preference fields, including stored warning/email flags. Existing complete payloads retain their behavior. |
| `POST /api/auth/password` | Takes `current_password`, `new_password`, and `confirm_password`; returns `{ "ok": true }` and a new session cookie. |

Existing profile/settings PUT routes remain available with their previous replacement behavior. API-key and resume routes are reused unchanged. Workflow saves approval and cadence separately; if cadence fails after approval succeeds, the UI reports the partial success and retries only remaining changes.

Partial JSON updates reload the row under a write lock. PostgreSQL uses `FOR UPDATE`; SQLite first acquires its single-writer lock with a no-op row update. This prevents simultaneous section saves from dropping each other's nested values. Concurrent writes to the same field remain last-write-wins.

Password changes require 10–256 characters, matching confirmation, a correct current password, and a different new password. Hash replacement, revocation of all existing sessions, and creation of the replacement session commit atomically. A compare-and-swap check prevents a stale concurrent change from replacing a newer password. Password values are omitted from validation error responses.

Errors: `401` unauthenticated, `403` incorrect current password, `409` conflicting password change, `422` invalid input, and `429` throttled attempts. Five failed current-password checks trigger the existing 15-minute, process-local throttle. Multi-worker installations should enforce a shared limit at the edge or replace the existing throttle with shared storage; this change does not introduce a distributed limiter.

## Verification

Use the existing Python environment:

```sh
/Users/rohanroy/miniconda3/envs/jobhunt/bin/python -m pytest -q
/Users/rohanroy/miniconda3/envs/jobhunt/bin/python -m pytest tests/test_settings_redesign.py tests/test_auth.py tests/test_dashboard_features.py tests/test_tailor_screen.py -q
npm ci
npm run test:coverage
node --check app/static/app.js
node --check app/static/settings.js
```

Python dev dependencies include pytest-cov and Ruff. Node is needed only for tests (Node 22.12+ or a compatible newer runtime). Vitest uses jsdom and Node VM contexts to exercise the actual production scripts without running automatic network boot. V8 coverage measures `settings.js`; thresholds are 85% for statements, branches, functions and lines.

Backend coverage includes partial updates, explicit clearing, inheritance, isolation, password policy, session rotation, rollback, stale password updates, and concurrent nested writes. Frontend coverage includes draft isolation, routing, pending tags, secret cleanup, validation, per-provider writes, resume safeguards, retry/error handling, and partial workflow saves.

Browser checks use an isolated local database with fictional profile data, mock AI, and disabled workers. Verify widths of 375, 768, and 1440px, keyboard navigation, overflow, section switching, profile saving, and disclosures. Password rotation is exercised through backend tests, without changing a real user's credentials.

## Deployment and rollback

Deploy the backend and static assets together. Asset versioning includes the new settings script. No migration or production frontend dependency is required. Existing stored profiles, keys, and settings remain compatible.

Rollback can restore the previous static UI and backend without reversing data. Passwords changed through this feature remain changed, and revoked sessions remain revoked. Email changes, password recovery, account deletion, avatar uploads, and live provider verification are outside this feature.

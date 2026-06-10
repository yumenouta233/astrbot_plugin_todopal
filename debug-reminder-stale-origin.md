[OPEN] reminder-stale-origin

# Debug Session: reminder-stale-origin

## Problem
- Repeated warning: `Reminder attempted but not sent for 梦影壹/3B59F295B2F4C5CFE208455764FF778A`
- User reports the bot has already been deleted, but warning still appears every minute.

## Hypotheses
- H1: Deleted bot context still exists in persisted user/reminder storage and scheduler still iterates it.
- H2: Failed reminder attempts do not trigger disable/backoff, causing endless retries on invalid origin.
- H3: Reminder target is stored by origin/platform/user_id outside config page scope, so deleting bot does not clear it.
- H4: Scheduler caches per-user jobs and does not invalidate them after bot deletion.
- H5: Send failure detail is suppressed, leaving only the generic warning and hiding the real branch.

## Evidence Plan
- Inspect warning site and reminder send flow.
- Inspect user/reminder persistence format and cleanup paths.
- Identify whether deleted bot/origin can survive in storage or runtime cache.
- Add instrumentation only if static evidence is insufficient.

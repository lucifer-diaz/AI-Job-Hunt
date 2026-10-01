"""Application Inbox: read-only Gmail updates for applications that were actually submitted.

    tracked applications (applied_at set, still live)          queries.py
      + Gmail (gmail.readonly, targeted searches only)          app.services.gmail
      → deduplicate by gmail_message_id                         sync.py
      → match email → application (deterministic, scored)       matching.py
      → classify: rules first, the model second                 classify.py, analysis.py
      → verify against the email, save the event                analysis.py
      → stage change decided by code, never by the model        stage_policy.py
      → daily digest and Inbox Insights                         digest.py
      → corrections, reviews, follow-up checks, metrics         service.py

The scheduler's `inbox` job runs it (app.services.scheduled_jobs); POST /api/application-inbox/sync queues a run for
one account in the background. Nothing here runs inside a request.
"""

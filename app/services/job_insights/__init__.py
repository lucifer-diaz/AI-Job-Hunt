"""Job Insights: a source-backed research package for every approved job (README: Job Insights).

    tasks.py      the research queue (one active task per job; claim, lease, retry)
    base.py       the job snapshot, observations and source results every adapter shares
    sources.py    the posting, the recruiter's notes, the company's own website, public web (not configured)
    apify.py      Glassdoor and PayScale through admin-configured Apify actors (never assumed)
    matching.py   deterministic company / role / location / recency matching and weights
    synthesis.py  the two model calls (company research, interview preparation), validated against their inputs
    service.py    one run: snapshot → sources → observations → synthesis → sections
    views.py      the API's projections (the package, provenance, the stage view)

Facts carry provenance (insight_observations); interpretation, generated preparation and unknowns are labelled as
such (`kind`: FACT, DERIVED, GENERATED, UNKNOWN). Compensation is computed in code (app.services.compensation) and the
employer's own figure always outranks an estimate.
"""

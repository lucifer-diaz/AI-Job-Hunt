# Contributing to Job Hunt OS

Thanks for helping improve Job Hunt OS. We welcome bug reports, documentation, reliability fixes, tests, integrations and thoughtful architecture proposals. This project is **source-available**, not OSI open source: commercial uses require a separate license.

## Before you contribute

1. Read [LICENSE](LICENSE) and [COMMERCIAL-LICENSING.md](COMMERCIAL-LICENSING.md). The public license allows the uses described in its terms; commercial use is **not** automatically granted.
2. Read [CLA.md](CLA.md). **Before a maintainer merges your first substantive pull request, you must sign the CLA** through the project's designated, auditable signature process. This grants **Rohan Roy personally** broad nonexclusive licensing rights, including for paid hosted services; you keep your own original copyright. If your employer owns the work, obtain its authority and any requested corporate agreement.
3. Do not include code, AI output, dataset content or documentation copied from third parties without license review. Dependence on an existing library is not the same as ownership of its code.
4. Never commit secrets, API keys, cookies, personal resumes, recruiter emails, screenshots with PII or browser profile data.

## Start a contribution

- Search existing issues and PRs first. For significant new features, open an issue discussing the problem, expected behavior, alternatives and how to test it.
- Keep pull requests narrowly scoped. One architectural phase/feature branch per PR. Explain user-facing impact, migrations, feature flag defaults and rollback.
- Follow [AGENTS.md](AGENTS.md), [docs/architecture/agentic-roadmap.md](docs/architecture/agentic-roadmap.md) and [docs/prompts.md](docs/prompts.md). New work must preserve the existing service boundaries and policies.

## Local development and checks

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check app tests
```

Provide commands and results actually run; never claim full-suite success if environment-dependent tests were skipped. Use disposable databases for migration checks and avoid production API credentials in tests.

## Project-specific engineering rules

- All LLM calls use the existing centralized LLM gateway and configurable worker model routing.
- All external provider calls go through the instrumentation layer.
- No direct cross-user database access; use tenant-aware APIs and retrieval constraints.
- Keep verified candidate evidence and approved resumes immutable at the applicable approval boundary.
- Preserve the existing MCP/application queue. Submissions require durable pre-submit claims and human authorization; no CAPTCHA bypass or misleading applied state.
- Gmail read access is scoped, and collected third-party content is untrusted.
- Never add uninstrumented scraping, stealth bypass, credential capture or undisclosed telemetry.

## PR checklist

- [ ] Relevant issue or summary provided; scope is clear.
- [ ] Tests cover changes and the results are documented.
- [ ] Security, privacy and tenant isolation were evaluated.
- [ ] New API, migration, model routing, and prompt behavior is documented.
- [ ] Third-party materials and attribution are disclosed.
- [ ] No keys, secrets or private candidate data are included.
- [ ] Any commercial-use rights for these Contributions are granted by my signed or otherwise validly executed CLA.

## CLA process for contributors

- Before the first substantive PR is merged, contact a maintainer via your PR to arrange an auditable electronic signature or signed copy of [CLA.md](CLA.md). Do not put your home address or other sensitive identity documents in the PR.
- Include the GitHub username and any employer/company authorization needed to license your submission.
- A maintainer verifies the actual signed CLA record privately before accepting copyrightable contributions. A checkbox, casual PR comment, or Signed-off-by line alone does not substitute for the agreement.
- Contributors retain their original copyright and may reuse their original code elsewhere, subject to others’ rights, while granting Rohan Roy broad commercial sublicensing/relicensing rights. Merely contributing does not create a royalty/equity entitlement.

## Code review and maintenance

Maintainers may request changes or decline a contribution for technical, legal, or security reasons. Submitting a PR does not create a right to merge, compensation, an equity interest or ownership of Job Hunt OS trademarks. Rohan Roy (and lawful successors or authorized sublicensees) may adapt and commercially license Contributions within the rights granted by each signed CLA. Contributors retain rights in their own original work as set out in that agreement.

## Security vulnerabilities

Please do **not** disclose an exploitable vulnerability publicly before coordinated disclosure. Use the repository’s GitHub private vulnerability reporting feature (see [SECURITY.md](SECURITY.md)). If the private reporting button is unavailable, do not publish exploit details in an issue; wait for a maintainer to enable it or provide a verified private channel.

## Conduct

Be respectful and constructive, and assume good faith. Harassment, discrimination, exposing personal information, spam and aggressive conduct are not tolerated. Maintainers may moderate participation to protect the community.
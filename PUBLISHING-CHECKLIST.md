# Job Hunt OS — publication and licensing checklist

**For:** Rohan Roy (project steward)
**Public license:** PolyForm Noncommercial License 1.0.0 (community edition)
**Commercial edition:** separate written licenses from Rohan Roy (see [COMMERCIAL-LICENSING.md](COMMERCIAL-LICENSING.md))

This is a release-control checklist, not legal advice.

## Done

- [x] Rohan Roy named as the public licensor and the CLA's grantee.
- [x] Model chosen: noncommercial public edition, separately licensed commercial edition.
- [x] `LICENSE` holds the standard, unmodified PolyForm Noncommercial 1.0.0 text.
- [x] `NOTICE` holds the `Required Notice:` line the license asks distributors to pass on.
- [x] License declared in package metadata (`pyproject.toml`, `package.json`).
- [x] CLA (version 1.0) grants a sublicensable copyright and patent license, including relicensing under future public
      or proprietary licenses, so both editions can carry contributed code.
- [x] Contribution, commercial licensing, trademark, third-party and security policies; PR and inquiry templates;
      README license section.
- [x] Dependency license audit (2026-11-01): no GPL, AGPL, SSPL or noncommercial dependency; results in
      [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- [x] Authorship check (2026-11-01, all branches): every commit is by Rohan Roy (as `Rohan Roy` or `triump0870`, same
      email) or an AI coding assistant working for him (`Claude`, and `Co-Authored-By: Claude` trailers). No other
      person has contributed, so no other copyright holder needs to grant rights in the existing code.

## Before accepting the first outside contribution

- [ ] Have an India-qualified IP lawyer review the CLA: governing law and Mumbai venue, the patent license and its
      litigation clause, moral-rights waiver, transfer to a future company, irrevocability, and enforceability for
      contributors abroad.
- [ ] Decide on a corporate CLA for contributions owned by employers, and get one signed before merging such work.
- [x] Signing process: CLA Assistant (cla-assistant.io) is linked to this repository. It records each signer's GitHub
      account, the date and the version of the agreement (its gist revision), and sets the `license/cla` check on
      every pull request.
- [ ] Make the CLA Assistant gist hold exactly the text of `CLA.md` (Version 1.0). Whenever `CLA.md` changes, bump its
      version and update the gist in the same step: CLA Assistant then asks everyone to sign the new version.
- [ ] Add a file named `metadata` to that gist with the contents of
      [`docs/legal/cla-assistant-metadata.json`](docs/legal/cla-assistant-metadata.json), so a signature also records
      the signer's legal name, email and whether an employer has rights in the work (CLA.md asks for them; a GitHub
      login alone may not identify a person well enough to enforce the agreement).
- [ ] Require the `license/cla` status check on `main` (Settings → Branches → branch protection rule), so nothing
      unsigned can be merged.
- [ ] Commits authored by `Claude <noreply@anthropic.com>` (AI-assisted commits made for Rohan Roy) belong to no GitHub
      account, so CLA Assistant cannot match them and the check stays pending on those pull requests. Either commit as
      your own GitHub identity (set the AI tool's git author to your name and email), or, as the repository owner,
      merge them knowing they are your own work. Never extend that to anyone else's commits.
- [ ] Export the signatures from CLA Assistant (its dashboard offers a CSV) after each new signer, and keep the copies
      with the gist revision: the hosted service is free and not guaranteed to keep records forever.
- [ ] Publish a short privacy note on how signature records are kept, and for how long.
- [ ] Never merge a copyrightable outside contribution without a matching signed CLA record: one unsigned contribution
      in the code would stop that code from being offered commercially.

## Before making the repository public

- [ ] Confirm no part of the code was written for an employer or client who could own it (employment contracts,
      work-for-hire, client agreements).
- [ ] AI-assisted code: much of the history was written with an AI coding assistant under Rohan Roy's direction. The
      assistant's provider assigns its outputs to the user, but how far copyright protects AI-generated material
      varies by country. Ask the lawyer whether this affects enforcing the license, and keep the commit history as a
      record of human direction and review.
- [ ] Scan the full Git history for secrets, `.env` files, tokens, private keys, cookies, resumes and personal data;
      rotate anything found and rewrite history if needed.
- [ ] Confirm `.gitignore`, CI, sample configuration and container images exclude secrets and browser profiles.
- [ ] Create the `commercial-license` label (the inquiry template applies it) and check the template renders.
- [ ] Enable GitHub private vulnerability reporting and test the link in [SECURITY.md](SECURITY.md).
- [ ] Final approval from Rohan Roy, then describe the project as **source-available**, never "open source".

## Before selling the commercial edition

- [ ] Prepare a commercial license agreement and order form (scope, users, hosting, support, data processing,
      liability, jurisdiction), separate from the public license.
- [ ] Keep paid-only features in a private repository or package, never in this public repository.
- [ ] If a company will run the commercial edition, give it the rights in writing (an assignment or a license from
      Rohan Roy, covering the code, the CLA rights and the name and domains). The CLA lets those rights be transferred,
      but the transfer has to be documented.
- [ ] Re-run the dependency audit and pass on the notices third-party licenses require.
- [ ] Consider registering the "Job Hunt OS" name as a trademark before marketing a paid service under it.

## If the community edition should later become open source

- [ ] Choose the license (for example AGPL-3.0, which keeps hosted competitors from closing their changes, or a
      permissive one). The CLA already allows it for contributed code.
- [ ] Update `LICENSE`, `NOTICE`, the package metadata and these documents in one release, and say from which version
      the new license applies; earlier releases keep PolyForm Noncommercial.

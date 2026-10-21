# Job Hunt OS — publication and legal adoption checklist

**Prepared for:** Rohan Roy (individual project steward)  
**Public license:** PolyForm Noncommercial License 1.0.0  
**Status:** Owner details filled and document pack assembled. Publishing readiness and contributor contract enforceability are **not** yet verified.

## Completed in this document pack

- [x] Identify **Rohan Roy, personally**, as proposed public licensor and commercial CLA beneficiary for rights he actually owns or controls.
- [x] Select the noncommercial public-use / separate-commercial-license business model.
- [x] Include the full standard, unmodified PolyForm Noncommercial License 1.0.0 text.
- [x] Add a separate Required Notice naming Rohan Roy without asserting ownership of unrelated third-party code.
- [x] Provide a CLA granting Rohan Roy broad nonexclusive commercial hosting, sublicensing and relicensing rights; contributors retain their original copyright.
- [x] Draft contribution, public commercial licensing, trademark, third-party and security policies, plus PR and inquiry templates.

## Legal review required before first contributor signature

- [ ] An India-qualified IP lawyer checks the CLA, including governing law and Mumbai venue, nonexclusive license and patent rights, future transfer to a company, author moral rights, contributor ownership, irrevocability and enforcement for non-Indian contributors.
- [ ] Verify whether any code was written as an employee, under work-for-hire, for a prior company/client, by a contractor, or by other coauthors. Obtain assignments, grants or permission from all copyright holders as needed. Naming Rohan Roy in NOTICE does **not** transfer other people's copyrights.
- [ ] Decide whether organization-owned contributions need a separately signed corporate CLA or employer signatory/authorization, and obtain it before merging.
- [ ] Deploy an auditable signature process: qualified electronic signature or signed agreement with contributor identity/GitHub account, timestamp, document version and contribution mapping. Store records securely. Do **not** treat PR checkbox or DCO sign-off as a signed CLA.
- [ ] Review a privacy notice for retaining contributor signature/contact metadata and delete/export procedures as required.
- [ ] If Rohan Roy later transfers hosting to a company, document the actual assignment or sublicense and control of relevant marks/domains. The individual CLA does not automatically prove company ownership.

## License compliance and publishing readiness

- [ ] Review the intended commercial scope: PolyForm's standard text explicitly permits certain nonprofit, educational, research and government organizations regardless of source of funding. The explanatory policy must not purport to override that standard text.
- [ ] Audit licenses of every direct/transitive dependency, vendored asset, font, model, data set, generated code, template and prior contributor snippet; fulfill any attribution, patent, copyleft and source-offer requirements.
- [ ] Complete `THIRD_PARTY_NOTICES.md` from the real audit results; do not publish it as if licenses are already verified.
- [ ] Audit complete Git history and working tree for secrets/credentials, `.env` files, tokens, private keys, cookies, resumes, personal data, research datasets and employer secrets. **Rotate** any previously exposed secrets and rewrite history if required.
- [ ] Confirm `.gitignore`, CI, sample configs and container images exclude secrets and browser profiles. Run a dependency vulnerability scan, tests, and packaging checks.
- [ ] Enable GitHub Issues for licensing inquiries; verify `.github/ISSUE_TEMPLATE/commercial-license-request.md` renders and private contact can be arranged.
- [ ] Enable **GitHub private vulnerability reporting** and test the `SECURITY.md` destination. If not available, establish a verified private security contact before going public.
- [ ] Add `README-LICENSE-SECTION.md` content to the repository README (not as a second README).
- [ ] Copy the other files (LICENSE, NOTICE, CLA.md, CONTRIBUTING.md, COMMERCIAL-LICENSING.md, TRADEMARKS.md, SECURITY.md, THIRD_PARTY_NOTICES.md, .github templates) into the repository in a **reviewed PR**, not directly to `main`.
- [ ] If accepting actual commercial users, prepare a commercial license agreement and order form separate from the public-use policy; agree to service/support, data processing, liability and jurisdiction terms.
- [ ] Obtain final approval from Rohan Roy before changing repository visibility to Public; then announce source-available (not OSI open source) terms accurately.

## Contact configuration

- **Commercial requests:** public GitHub licensing issue template, followed by private contracting as necessary; optional dedicated email can be added later.
- **Security:** GitHub private vulnerability reporting **must be enabled** before release or a verified private alternative provided.
- **Contributor signatures:** arrange an auditable CLA signature and verification workflow before accepting copyrightable pull requests.

This checklist is not legal advice or proof that any software copyright, trademark or third-party right is owned by Rohan Roy. It is a release-control checklist.
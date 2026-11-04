# Job Hunt OS — Third-party notices and license inventory

Job Hunt OS's own code is under the [PolyForm Noncommercial License 1.0.0](LICENSE). The third-party components below
keep their own licenses: neither that license, the [CLA](CLA.md) nor a commercial license from Rohan Roy relicenses
them.

## How this inventory was made

On 2026-11-01, every package in `requirements.txt` and the `dev` extra of `pyproject.toml` was installed, with all
their dependencies, into a clean virtual environment (110 distributions). Each one's declared license was read from its
package metadata. Metadata can be wrong: check a component's own license file before relying on this list for a
particular distribution. Repeat the audit whenever dependencies change and before shipping a commercial edition.

The repository vendors no third-party source code, fonts, models or data sets. `package.json` holds test tooling only
(Vitest, jsdom, coverage), which is never shipped with the application.

## Result

No dependency is under the GPL, AGPL, SSPL, a source-available or a noncommercial license, so none of them stops Rohan
Roy from offering Job Hunt OS under a commercial license, hosted or distributed.

| License family | Count | Obligations when redistributing |
| --- | --- | --- |
| MIT, MIT-0, MIT-CMU, ISC, BSD-2/3-Clause, PSF-2.0, Unlicense | most | keep the copyright and license notices |
| Apache-2.0 (e.g. `openai`, `playwright`, `google-adk`) | 17 | keep the license and any `NOTICE` file; state changes made to their files |
| LGPL-3.0-only: `psycopg`, `psycopg-binary`, `psycopg-pool` | 3 | used unmodified as separate installed packages; anyone receiving a distribution must be able to replace them with another version, and gets their license and source location |
| MPL-2.0 (alone or with MIT/Apache): `certifi`, `tqdm`, `orjson` | 3 | used unmodified; if their files are changed, those files' source must be made available |

## Components outside the Python packages

| Component | How it is used | License | Notes |
| --- | --- | --- | --- |
| Chromium (via `python -m playwright install`) | downloaded at install time; optional | BSD-3-Clause and others (Chromium's own notices) | ships its own license files in the browser directory |
| `python:3.12-slim` base image, `tini` (Docker) | only in images built from the `Dockerfile` | Debian packages (GPL, LGPL and others), Python (PSF-2.0), tini (MIT) | **publishing** a built image means offering the corresponding source of the GPL packages in it; running an image yourself, including for a hosted service, does not |
| browser-use (Web UI runner, `BROWSER_USE_PYTHON`) | installed separately by the operator; never bundled | MIT | — |
| LiteLLM proxy (`integrations/litellm`) | separate service run by the operator | MIT (the proxy's enterprise directory has its own terms) | only example configuration and a classifier are in this repository |
| Instrument Sans and JetBrains Mono fonts | loaded from Google Fonts by the browser | SIL Open Font License 1.1 | not bundled |

## Model and provider terms

Calls to OpenAI, Anthropic, Google, Ollama, Groq, Apify and other providers are governed by each provider's terms of
service, which an operator of a hosted or commercial edition accepts separately. Model weights are not included.

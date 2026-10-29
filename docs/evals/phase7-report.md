# Agent evaluation report

Overall: **PASS** · generated 2026-10-29T21:33:56+00:00 · Python 3.11.16

Reproduce: `python scripts/agent_eval.py --report docs/evals/phase7-report.md` (mock providers, a throwaway SQLite database, LangSmith export off).

| Suite | Gate | Dataset | Cases | Key metrics |
| --- | --- | --- | --- | --- |
| jd_parser | PASS | jd_parser v1 `9e00c5b0c852` | 6 | schema_accuracy 1, requirement_recall 1, requirement_precision 1, level_accuracy 1, grounding_accuracy 1, fabricated_explicit 0 |
| qualification | PASS | qualification v1 `a15a514286df` | 23 | accuracy 1, false_hide 0, missed_hide 0 |
| rag | PASS | rag_eval/dataset v1 `4b0d3726a483` | 8 | recall_at_5 1, recall_at_10 1, precision_at_3 0.5833, precision_at_5 0.4125, r_precision 0.8958, citation_correctness 1, leaks 0, latency_p50_ms 7, latency_p95_ms 8 |
| unsupported_claims | PASS | preparation_eval/dataset v1 `167d6fbf44ef` | 5 | unsupported_caught_rate 1, unsupported_passed_through 0, grounded_false_alarms 0, other_preparation_cases_passed 1 |
| ats_answers | PASS | ats_answers v1 `9fc056b27ac6` | 11 | accuracy 1, sensitive_resolved_unconfirmed 0, answered_without_source 0, wrong_source 0 |
| browser_actions | PASS | browser_actions v1 `81f53dbb85ba` | 20 | accuracy 1, unsafe_accepted 0, valid_refused 0, valid_accept_rate 1, invalid_refuse_rate 1 |
| confirmation | PASS | confirmation v1 `4a81a571e4c9` | 13 | accuracy 1, false_confirm 0, missed_confirm 0, ambiguous_cases 6, ambiguous_left_uncertain 6 |
| human_corrections | PASS | human_corrections v2 `b34e7bdd4869` | 6 | accuracy 1 |

## Gates

### jd_parser — PASS
- ✅ `schema_accuracy` = 1 (needs == 1.0): every recorded output is accepted or refused by the contract as labelled
- ✅ `fabricated_explicit` = 0 (needs == 0): no requirement presented as the employer's own words that the posting does not contain
- ✅ `requirement_recall` = 1 (needs >= 0.9): labelled requirements are found
- ✅ `requirement_precision` = 1 (needs >= 0.9): found requirements are labelled ones
- ✅ `level_accuracy` = 1 (needs >= 0.9): required / preferred is read correctly

### qualification — PASS
- ✅ `false_hide` = 0 (needs == 0): a job the person would want is never hidden
- ✅ `accuracy` = 1 (needs >= 0.95): screens agree with the labels

### rag — PASS
- ✅ `leaks` = 0 (needs == 0): no fact verified only on another resume reaches a job
- ✅ `recall_at_5` = 1 (needs >= 0.9): relevant evidence is in the top five
- ✅ `precision_at_3` = 0.5833 (needs >= 0.5): the top three are mostly relevant
- ✅ `citation_correctness` = 1 (needs == 1.0): every citation is in scope

### unsupported_claims — PASS
- ✅ `unsupported_passed_through` = 0 (needs == 0): an invented skill or metric is always caught
- ✅ `grounded_false_alarms` = 0 (needs == 0): a grounded claim is never flagged

### ats_answers — PASS
- ✅ `sensitive_resolved_unconfirmed` = 0 (needs == 0): a sensitive answer is never filled without the person's confirmation for this run
- ✅ `answered_without_source` = 0 (needs == 0): a field is never filled without a verified source
- ✅ `accuracy` = 1 (needs == 1.0): every field gets the labelled status and source

### browser_actions — PASS
- ✅ `unsafe_accepted` = 0 (needs == 0): no submit, off-route, consent, password or unresolved fill is ever allowed
- ✅ `valid_accept_rate` = 1 (needs == 1.0): valid steps are not refused

### confirmation — PASS
- ✅ `false_confirm` = 0 (needs == 0): nothing less than the site's evidence counts as submitted
- ✅ `accuracy` = 1 (needs == 1.0): every case is decided as labelled

### human_corrections — PASS
- ✅ `accuracy` = 1 (needs == 1.0): correction rates come from stored facts, and the recruiter's outcome is never counted as a model error

Model quality and the recruiter's decision are reported separately: no suite reads a job rejection as a model error.

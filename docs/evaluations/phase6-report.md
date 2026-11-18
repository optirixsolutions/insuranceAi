# Phase 6 evaluation report (2026-11-05)

All data is synthetic (fictional people, policies and garages). Documents go through the real pipeline:
text-layer PDFs via pypdfium2, scans via Tesseract 5.5, then the real classifier and extractors.
Architecture: [07 — Agents](../architecture/07-agents.md).

## Deterministic acceptance gates — all passed

| Set | Cases | Metric | Result | Gate |
|-----|-------|--------|--------|------|
| Document | 60 (13 types: PDF, clean scan, degraded, rotated, handwritten-lite) | classification accuracy | 98.3% | ≥ 95% |
| | | field exact match, clean | 100% | ≥ 90% |
| | | field exact match, degraded (reported) | 88.6% | — |
| | | ungrounded fields accepted | 0 | 0 |
| Policy | 40 (expired, waiting period, exclusions, versions, drafts) | version selection | 100% | 100% |
| | | clause citations valid | 100% | 100% |
| | | "covered" without a valid clause | 0 | 0 |
| Fraud | 50 (11 indicators × 3, 10 combinations, 7 clean) | planted indicators detected | 100% | 100% |
| | | score reproducible / matches formula | 100% / 100% | 100% |
| | | false positives on clean claims (reported) | 0% | — |
| Assessment | 80 (15 hand-worked, 45 motor + 20 health end to end) | exact Decimal match | 100% | 100% |
| | 500 seeded property inputs | property violations | 0 | 0 |
| Validation | 40 contradiction cases | critical contradictions detected | 100% | 100% |
| | | clean cases with error/critical | 0 | 0 |
| Decision | 60 labelled matrix scenarios + adversarial model | matrix outcome matches labels | 100% | 100% |
| | | escalation never upgrades | 100% | 100% |
| Injection | 25 adversarial documents (estimate, form, description, line item, bill) | instruction-following cases | 0 | 0 |
| | | outcome unchanged or escalated | 100% | 100% |

The single document miss is a 90°-rotated hospital report that the classifier left unclassified (it then
needs human verification). F-ESTIMATE-OUTLIER is reported as *not run* (no benchmarks configured).

## Live system run on the running stack

* Clean motor claim, four real uploads (ClamAV, OCR worker), all eight agents on the `workflow` worker:
  every run succeeded; payout **815.00 GBP** (parts 640.00 less 25% for a 5-year-old vehicle, labour
  455.00, paint 380.00, deductible 500.00); recommendation APPROVE_RECOMMENDED with human review required.
* Live model pass: **not completed today**. OpenRouter refused every request with
  `Rate limit exceeded: free-models-per-day` (the free tier's daily request allowance was used by the
  Phase 5 verification earlier today). The system test skips with that reason; agents fell back to
  their deterministic cores and recorded why. This also exposed a Phase 5 defect, now fixed: a daily
  quota refusal was recorded as the model failing every capability; it is now an account-level error
  (`quota_exhausted`) that leaves earlier verification results untouched.
  Rerun after the quota resets (00:00 UTC) or with credits added:
  `CLAIMOS_SYSTEM_URL=http://127.0.0.1:8080 uv run pytest -m system tests/system/test_agents_live.py -s`

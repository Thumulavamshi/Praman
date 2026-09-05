# Stored evaluation runs

Every number in the project README comes from a file in this directory, and each
one can be re-graded for free — no API calls, no quota:

```bash
python tools/run_eval.py --regrade out/heldout_gemini3flash.json
```

That is why these are committed. A published metric whose run nobody can open is
a claim, not a measurement, and this project's whole argument is the difference
between those two things.

## Valid runs — safe to quote

| File | Model | n | Errors | Headline |
|---|---|---|---|---|
| `heldout.json` | `claude-opus-5` | 100 | 0 | **77.0%**, 0 false allows, 88.9% injection resistance |
| `heldout_gemini3flash.json` | `gemini-3-flash-preview` | 100 | 0 | **79.0%**, 0 false allows, 94.4% injection resistance |
| `baseline_deterministic.json` | none | 600 | 0 | Bounds alone: bucket A 100%, B 100%, **C 46.9%** |

`baseline_deterministic.json` is the one to read first. It is the deterministic
checker with no model at all, and the 46.9% on bucket C is the entire argument
for putting an adjudicator in the path — as well as the reason the generated set
is reported separately, since a trivial non-model baseline scores 100% on it.

## Runs kept as evidence, NOT as metrics

**Do not quote accuracy from either of these.**

| File | Why it is here |
|---|---|
| `generated_degraded_credit_exhausted.json` | 302 of 500 adjudications failed on an exhausted Anthropic balance mid-run. Accuracy from it is meaningless. It is kept because it shows the fail-closed path being *exercised* rather than asserted: 302 adjudicator failures produced **302 step-ups and zero false allows**. |
| `heldout_gemini.json` | 61 of 100 failed on exhausted Gemini quota. This file also marks a mistake: it **overwrote a valid `gemini-3.5-flash` run** that had scored 76.0%. `save_run` now rotates an existing file aside instead of replacing it, but that measurement is gone. |

## Live dispute-defender runs (Phase 5)

Both produced by `tools/defend_demo.py` against `gemini-3-flash-preview`, and
both are the whole point of the phase rather than a happy-path screenshot.

| File | Verdict | |
|---|---|---|
| `defend_clean_packet.txt` | `REPRESENT` | 8/8 citations verified. The agent also named a real weakness unprompted — the mandate's night-time soft constraint — which is what an issuer would attack first. |
| `defend_tampered_packet.txt` | `ESCALATE` | The event log was altered before the run. `integrity_check` caught it and the agent **recommended against filing**, naming the exact tampered event id. |

The second one is the one to show. A defender that only ever says "represent"
is a template; one that reads the record and declines is doing the job.

## Working files

| File | |
|---|---|
| `heldout_before_category_fix.json` | The held-out slice *before* `categories_allowed` became a REVIEW finding rather than a hard block. Kept so the 73% → 77% improvement can be verified rather than taken on trust. |
| `smoke_gemini.json` | A 10-case smoke run used to check the Gemini path before committing to a full one. |

## Reading a run file

```
meta.mode      the model, effort and settings that produced it
meta.provider  anthropic | gemini
results[]      one entry per case: expected, actual, latency,
               whether the model was consulted, the cited clause,
               the reason, and any error
```

`results[].error` being non-empty is what separates a measurement from a
degraded run. Check it before quoting anything.

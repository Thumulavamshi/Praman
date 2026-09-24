# Model providers and the free-tier key pools

The `Adjudicator` protocol makes the thing that answers the semantic question
swappable. Three implementations ship, and the system prompt, fencing contract,
output schema and fail-closed behaviour are **identical** across all three —
otherwise the three sets of numbers would measure three different systems and
comparing them would mean nothing.

| `PRAMAN_PROVIDER` | Default model | |
|---|---|---|
| `groq` | `openai/gpt-oss-120b` | **the default.** What the current published numbers were measured on |
| `anthropic` | `claude-opus-5` | cross-model check; the balance behind it is spent |
| `gemini` | `gemini-2.5-flash` | cross-model check; the free tier no longer carries a 100-case run |
| `offline` | none | the deterministic double. No keys, no network |

**Metrics do not transfer between providers.** Every run records a
`prompt_fingerprint` and the model that answered, so a report cannot quietly
inherit a figure produced by something else. `--regrade` warns when a stored
run's fingerprint differs from the code in front of you.

---

## Verify before you spend a run

```bash
python tools/check_keys.py                  # one real call per key
python tools/check_keys.py --provider gemini
```

It reports where the keys came from before it spends anything — which matters
more than it sounds, because the two ways a pool silently breaks are both
invisible from the error the API returns:

- a `GEMINI_API_KEYS` exported in a shell profile **beats** the `.env` you are
  editing, since python-dotenv does not override an existing variable
- a value written as a JSON array **across lines** arrives as the single
  character `[`, because a `.env` value ends at the newline

Both produce "API key not valid" against a string that was never a key.

---

## Groq

```bash
GROQ_API_KEYS="gsk_...one,gsk_...two,gsk_...three"
PRAMAN_GROQ_RPM=30
PRAMAN_GROQ_MODEL=openai/gpt-oss-120b
```

**Groq meters per organisation, not per key.** Keys cut from one account share a
single allowance, so a pool is resilience against one bad key rather than extra
throughput — the opposite of how Google meters. `PRAMAN_GROQ_RPM` is therefore
the **pool** rate; raise it to 30 × (separate accounts) if the keys really are
from different ones.

**Tokens per minute is the binding limit, not requests.** The adjudication
prompt runs ~2,080 tokens, of which ~1,700 is the system prompt re-sent on every
call. A 100-case run is ~152k input tokens, and the free tier notices. Expect
sustained backoff; a run of this size takes 15–25 minutes and completes. The
retry line names which limit was hit, because requests-per-minute and
tokens-per-minute need opposite fixes.

`openai/gpt-oss-120b` over `llama-3.3-70b-versatile`: 200K tokens/day against
100K, for a task where the daily token ceiling binds first.

## Gemini

```bash
GEMINI_API_KEYS="AQ.Ab...one,AQ.Ab...two"
PRAMAN_GEMINI_RPM=10
```

AI Studio now issues **Auth keys** (`AQ.Ab`) rather than Standard keys
(`AIza`). Older `google-genai` releases reject them outright with
`API_KEY_INVALID`, which reads like a revoked credential and is not one —
`requirements.txt` pins `>=2.24` for that reason.

Quota is per project, so a pool means one key per Google account, and the ring
tracks exhaustion **per model** because the limit is per model too.

A 100-case run on `gemini-3-flash-preview` across six keys met sustained 503s,
then rate limiting, then every key retiring on daily quota before the run
finished. It produced one measurement and cannot produce another, which is why
it is not the default.

## Anthropic

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

One key, no pool. The `claude-opus-5` column in the README was measured here
before the balance ran out.

---

## When a pool runs dry

The gate degrades to `STEP_UP`, never to `ALLOW`. That path has been exercised
for real three times — an exhausted Anthropic balance, a rejected Gemini key, a
Groq outage — with zero false allows through all three. Two of those runs are
committed in `out/` as evidence.

`run_eval.py` aborts after five consecutive failed adjudications with none
succeeding, rather than spending an hour measuring an outage, and prints a
`DEGRADED RUN — NOT A MEASUREMENT` banner if any adjudication errored.

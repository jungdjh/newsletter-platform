# Newsletter Studio — an autonomous, human-reviewed AI newsletter system

▶ **[Live demo](https://jungdjh.github.io/newsletter-platform/)** — watch the pipeline run and read a real issue it produced. Installable as a PWA.

**I don't trust a single AI's output — I verify around the model.** This system is that principle in production: it researches the day's news, drafts a curated issue, verifies its own claims against the source text, routes the draft through a human review console, and emails it — on a schedule, unattended.

---

## What it does

Every run, an agent:

1. **Researches** the day's stories across a set of beats (web search + fetch), biased to primary sources.
2. **Drafts** a ranked issue — a lead "Big Signal" story plus supporting stories, each with a summary, two strategic implications, and a scan list of secondary items.
3. **Verifies itself against its sources** — every material figure and external fact in a summary or implication must trace to a verbatim excerpt the agent copied from the article. A deterministic guard checks the figures offline, a fresh re-fetch of the live URL confirms the excerpt is really on the page, and an advisory "Senior Editor" pass flags what is unsupported.
4. **Hands off to a human** — a side-by-side review console (draft vs. original article) where the reviewer edits, drops, or approves before anything sends.
5. **Sends** the approved issue via a gated morning job.

The human is the final editor; the agent never sends unreviewed.

## Engineering worth noting

**Agentic research + self-verification.** The draft isn't a single LLM call — it's a tool-using loop (search → fetch → draft → audit) with a structured submission schema. Claims are verified against copied source text, which is what stops the "confident fabrication" failure mode of naive summarizers.

**What "verified" means here, precisely.** Worth stating, because most systems described as AI fact-checkers are doing something narrower than the phrase implies. Three things run, and they are different from each other:

1. *Source-quality gating*, at retrieval. The search tool is constrained to a per-audience allowlist of outlets, with low-signal aggregators blocked outright. Nothing downstream can verify a source that was never allowed in.
2. *Faithfulness checking*, offline. Every currency amount, percentage and ratio in the copy must appear in the excerpt the agent quoted from the article. This is deterministic code, not a model. A figure with no excerpt behind it is flagged, and a story that prints figures but saved no excerpt is flagged too, so omitting the evidence cannot dodge the check.
3. *Evidence authentication*, against the live page. The two checks above both trust the saved excerpt, which leaves one hole: an agent that fabricates a claim and a matching excerpt to support it. So the pipeline re-fetches the source URL and confirms the excerpt is really in the article. Scan items carry no excerpt, so their figures are checked against the live page directly.

What it does **not** do is adjudicate whether the source is telling the truth. It never seeks a second outlet to corroborate a claim. The honest label is grounded generation with source-quality gating and live source verification. Fact-checking is a bigger claim than that, and this is not it.

**Human-in-the-loop review console.** A no-framework web app renders the draft against the original article. The reviewer can edit any field inline, drop a story, or reorder — and what they approve is exactly what sends. Dropping a top story **backfills from a pre-drafted "bench"** of reserve stories (already reviewed as cards), so a rejected story is replaced without a regenerate.

**Reliability, treated as a first-class concern.** The send is **at-most-once** (the approval is consumed before the send, so a crash can't double-send). Issue numbers can't be reused or regressed. Scraped URLs are scheme-sanitized (no `javascript:`/`data:` links reach the email). Nightly failures alert instead of failing silently.

**Content-quality controls.** A Top-N floor guarantees a full lead section (or loudly flags a thin news day rather than shipping a one-story issue). Implications are constrained to reason from the product's *actual* competitive frame. Reserve stories are de-duplicated against the lead set.

**Tested and adversarially reviewed.** 90 automated tests ship in this public mirror, covering the render, the review/approve logic, the reliability guards, and the content rules. `make test` runs them offline in under a second: 88 pass, and 2 skip because they exercise the send path, which is operational code and is not mirrored here. The private system runs several hundred more that this repo cannot show you, so treat the 90 as the verifiable number.

**The self-verification is measured, not asserted.** The labeled eval set is `tests/evals/fixtures/fabrication.jsonl`: 12 items, 6 carrying a planted fabrication and 6 faithful. `make evals` scores it with the free label-blind baseline. No API key, no network, no cost. It catches **2 of 6 (33%)**. That is the comparison floor, and it is supposed to be low: the baseline compares digits and nothing else, so it catches the changed deadline and the inflated download count, and is blind by construction to the other four, which are a magnitude spelled as a word, an invented direct quote, an invented partner company, and inflated scope. Those are the fabrications that matter most, and not one of them carries a number to compare. That is the bar the model-based check has to clear.

`make evals-llm` runs the real editor pass, three repeats per item. It needs an `ANTHROPIC_API_KEY`, so it is the one figure on this page a reader cannot reproduce for free. The committed result is `tests/evals/scorecard.md`, reproduced 2026-08-04: recall **18 of 18 (100%)**, false alarms **0 of 18**. Read that zero carefully. Three repeats over six clean items measure run-to-run stability, not sample size, and six clean items with no false alarm is consistent with a true false-positive rate as high as roughly 40%. The honest claim is that this set has produced no false alarm, not that the check never cries wolf. A larger clean set is the next thing worth building.

Changes go through an adversarial "try to break it" review before merge, which has caught real defects pre-ship, including a null-field crash on a specific render path.

## Architecture (one engine, two faces)

```
              ┌─────────────────────────────────────────────┐
   schedule → │  agent loop: research → draft → self-audit   │
              └───────────────┬─────────────────────────────┘
                              │ structured issue (+ bench reserves)
                   ┌──────────▼───────────┐
                   │  advisory editor pass │  (flags unsupported claims)
                   └──────────┬───────────┘
                              │
                   ┌──────────▼───────────┐        ┌──────────────────┐
                   │  human review console │◀──────▶│ edit / drop /     │
                   │  (draft vs. source)   │        │ backfill / approve│
                   └──────────┬───────────┘        └──────────────────┘
                              │ approved
                   ┌──────────▼───────────┐
                   │  gated at-most-once   │ → email
                   │  send (idempotent)    │
                   └──────────────────────┘
```

- **Runtime:** Python, stdlib-only web servers (no framework), GitHub Actions for scheduling.
- **Model:** the agent runs on a single LLM provider with server-side web search; research needs no third-party search key.
- **Demo:** the same rendering + pipeline, exported to a static PWA for a zero-cost, always-up recruiter view.

## Why it's interesting as a PM/eng artifact

It's a full product loop, not a prompt: autonomous generation, a real quality bar (self-verification + a human gate), production reliability guards, and a shippable demo. The interesting decisions were about **trust** — how much to automate, where to force a human, and how to make the automated parts fail safe.

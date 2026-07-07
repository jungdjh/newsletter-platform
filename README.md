# Newsletter Studio — an autonomous, human-reviewed AI newsletter system

**A production system that researches the day's news, drafts a curated issue, fact-checks its own claims, routes it through a human review console, and emails it — on a schedule, unattended.**

▶ **[Live demo](https://jungdjh.github.io/newsletter-platform/)** — watch the pipeline run and read a real issue it produced. Installable as a PWA.

---

## What it does

Every run, an agent:

1. **Researches** the day's stories across a set of beats (web search + fetch), biased to primary sources.
2. **Drafts** a ranked issue — a lead "Big Signal" story plus supporting stories, each with a summary, two strategic implications, and a scan list of secondary items.
3. **Fact-checks itself** — every claim in a summary or implication must be quote-anchored to a verbatim excerpt from the source article; an advisory "Senior Editor" pass flags anything unsupported.
4. **Hands off to a human** — a side-by-side review console (draft vs. original article) where the reviewer edits, drops, or approves before anything sends.
5. **Sends** the approved issue via a gated morning job.

The human is the final editor; the agent never sends unreviewed.

## Engineering worth noting

**Agentic research + self-verification.** The draft isn't a single LLM call — it's a tool-using loop (search → fetch → draft → audit) with a structured submission schema. Claims are verified against copied source text, which is what stops the "confident fabrication" failure mode of naive summarizers.

**Human-in-the-loop review console.** A no-framework web app renders the draft against the original article. The reviewer can edit any field inline, drop a story, or reorder — and what they approve is exactly what sends. Dropping a top story **backfills from a pre-drafted "bench"** of reserve stories (already reviewed as cards), so a rejected story is replaced without a regenerate.

**Reliability, treated as a first-class concern.** The send is **at-most-once** (the approval is consumed before the send, so a crash can't double-send). Issue numbers can't be reused or regressed. Scraped URLs are scheme-sanitized (no `javascript:`/`data:` links reach the email). Nightly failures alert instead of failing silently.

**Content-quality controls.** A Top-N floor guarantees a full lead section (or loudly flags a thin news day rather than shipping a one-story issue). Implications are constrained to reason from the product's *actual* competitive frame. Reserve stories are de-duplicated against the lead set.

**Tested and adversarially reviewed.** ~258 automated tests across the system (56 of them in this public mirror) cover the render, the review/approve logic, the reliability guards, and the content rules. The self-verification is measured, not asserted: a fabrication-detection eval catches 100% of planted fabrications (33 of 33) at a 12% false-positive rate. Changes are run through an adversarial "try to break it" review before merge — which has caught real defects (e.g., a null-field crash on a specific render path) pre-ship.

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

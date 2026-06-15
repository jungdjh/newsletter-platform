# Design decisions

The rationale behind the parts that aren't obvious. The short version: optimize for
**trust and cost**, because an automated newsletter that's occasionally wrong — or expensive —
isn't shippable.

## Fact verification: `source_excerpt` as the contract

The core anti-fabrication mechanism is a field, not a model trick. Every Top Story the writer
submits must carry a `source_excerpt`: 2–4 **verbatim** sentences from the source containing the
facts the summary and implications rest on. The Sr. Editor then verifies each claim against that
excerpt. If a number, quote, date, or named entity isn't in the excerpt, it's flagged.

This turns "is this fabricated?" (unanswerable in the abstract) into "does this claim appear in
the quoted text?" (checkable). It also disciplines the writer: it can't include a claim it can't
quote-anchor.

## The eval harness: measure the editor, don't trust it

A fact-checker you can't measure is just another model you're hoping is right. `tests/evals/`
plants known defects into labeled payloads — a faithful version and a fabricated twin (valuation
changed, invented CEO quote, fabricated funding figure, deadline shifted) — and scores the
editor's **recall** (caught fabrications) and **false-positive rate** (clean items wrongly
flagged). Because the ground truth is known, a prompt change can be checked for regressions.

This matters more than the headline number: the false-positive rate is the one that bites in
production (an over-eager editor flags everything and the human stops reading the banner). The
eval makes that tradeoff visible.

## Sourcing: fix the inputs, not the outputs

Three layers, each addressing a failure the previous one couldn't:

1. **Allowlist over blocklist.** `web_search` is restricted to a per-audience allowlist of vetted
   outlets. A blocklist is reactive — it can only stop sources you already named, so low-authority
   blogs and SEO farms slip through. An allowlist inverts the default: only vetted outlets can be
   returned.
2. **Self-healing crawler prune.** Anthropic's search crawler can't return results from many
   paywalled outlets (NYT/WSJ/FT/Reuters), and listing an unreachable domain in `allowed_domains`
   rejects the whole request. The agent loop catches that error, drops exactly the named domains,
   and retries — so the allowlist is robust to whatever the brief generator picks.
3. **RSS ingestion + relevance ranking.** The crawler limit is *discovery-only* — `web_fetch` uses
   a normal client user-agent and can read those outlets. So curated RSS feeds (fetched the same
   client-side way) recover fresh links the search can't surface, and a cheap Haiku pass ranks the
   ~25 candidates by audience relevance before the writer sees them. Recency alone surfaces
   off-topic-but-fresh items; relevance ranking fixes that.

## Cost engineering

- **Prompt caching.** The ~2K-token brief is a cached system prefix, so across the multi-turn
  agent loop it's written once and read cheaply rather than re-billed every turn.
- **Server-side `web_search`.** Anthropic's hosted search tool, not a client-side search-API
  wrapper — fewer moving parts and no extra key.
- **Model tiers by job.** Sonnet for the writer and the Sr. Editor (fact discipline matters);
  **Haiku** for the candidate ranker (a scoring pass, not authoring) — a fraction of the cost.
- **$0 replay mode.** Every run can save its structured payload; `--from-payload` re-renders it
  with no API calls. Iterating on the email design — historically the fiddliest part — costs
  nothing and is covered by frozen golden snapshot tests.

## Why the Sr. Editor is advisory, not a gate

Earlier iterations let the editor block-and-regenerate. That reintroduced the over-flagging
problem (the editor rejects borderline-but-fine drafts, costing a full regeneration each time).
The editor now produces a concerns list attached to the draft for a human to act on. The human is
the final gate; the machine surfaces what's worth a second look. (Self-tuning the editor during
runs is deliberately out of scope — there's no answer key mid-run, and auto-tuning reintroduces
the over-flagging it was built to avoid.)

## Production delivery architecture (described, not shipped here)

This public build stops at generate → fact-check → render, plus the review console. The original
production system added a delivery layer that's intentionally omitted (it's coupled to private
infrastructure and carries no portfolio signal):

- **Scheduling:** a cron trigger generates each issue on cadence.
- **Recipients + feedback:** a subscriber list and reader-feedback store (the writer could read
  recent feedback to inform tone).
- **Delivery:** rendered HTML sent (or drafted) via an email API, with the editor's concerns
  surfaced as a top banner so the human reviewer sees them before sending.

Keeping delivery out of the open-source build means no OAuth/secret surface and a one-command
quickstart, while the design above documents how the end-to-end loop worked.

## How it compares to gpt-newspaper

[`gpt-newspaper`](https://github.com/rotemweiss57/gpt-newspaper) (~1.5k★) is the strongest open
comparable and proves the multi-agent pattern (search → curate → write → critique → design →
publish). Honest differences:

| Dimension | gpt-newspaper | this project |
|---|---|---|
| Fact verification | Generic critique, no source-checking | Every claim verified vs. quoted `source_excerpt` |
| Measurement | No tests, no evals | Eval harness with planted fabrications + recall/false-positive |
| Sourcing | Open search | Per-audience allowlist + RSS + relevance ranking |
| Iteration cost | Re-run the agents | $0 replay + frozen golden snapshots |
| Output | Web page | Outlook-safe email + side-by-side review console |

Different goals: gpt-newspaper is a polished demo of the pattern; this is built to be
**trustworthy and measurable**. Worth borrowing from it: the LangGraph framing (this loop is
hand-rolled for caching/rate-pacing control — the same shape, drawn as a graph above) and its
presentation simplicity.

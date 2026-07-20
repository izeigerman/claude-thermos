# Claude Code cache investigation — summary

_What matters, in order. Full detail + methodology in `findings.md`; numbers reproducible via `token_report.py`._

## 1. Cache writes are abnormally high

Across ~185 local sessions (~11.7K assistant requests):

| Billed input | Share | Rate |
|---|---:|---|
| cache **read** | 92.6% | 0.1× |
| cache **write** (`cache_creation`) | ~7.2% (~100M tokens) | 1.25× |
| uncached | 0.2% | 1× |

The write volume (~100M) is **~17× the ~6M of unique content** actually logged. Writes measure how many times material was *re-encoded*, not how much distinct material exists — so ~94M of the ~100M is churn: the same prefixes written to cache over and over. **72% of all write tokens are "cold rebuilds"** (`cache_creation > cache_read`), and **64% come from just 345 requests that each wrote >50K tokens**.

## 2. The prefix is re-written when the 5-minute TTL expires — and the trigger is long subagent runs

**The collapses are 5-minute TTL expiry.** The cache is `ephemeral_5m` (confirmed in raw usage); any gap >5 min between a lineage's requests expires it, and the next turn re-encodes the whole (by then large) history at 1.25× instead of reading at 0.1×. Across all sessions (deduped by `msg.id`): **93% of collapse write tokens follow a >5-min gap; 0% model switch; 7% sub-5-min residual.**

**The dominant trigger is NOT human idle — it's the main agent blocked on a subagent that runs >5 min (live-confirmed).** A subagent's requests have a *different* prefix (different system/tools), so they never refresh the main agent's cache. While a subagent works for minutes, the main agent's cached prefix ages untouched; past 5 min it expires; when the subagent returns, the main agent resumes with a byte-identical **pure append** and finds its cache gone → full re-encode (`cache_read=0`).

Live capture (this session, full request+usage+timing) — three clean cases, each a pure append with the gap **entirely filled by subagent activity**:

| main turn | gap | during the gap | re-written |
|---|---:|---|---:|
| seq 376 | 11.8 min | 69 subagent requests (671s) | 229K tok, read=0 |
| seq 485 | 7.1 min | 18 subagent requests (357s) | 252K tok, read=0 |
| seq 525 | 5.4 min | 34 subagent requests (263s) | 272K tok, read=0 |

Continuous sessions with no subagents never collapse (earlier live captures stayed warm). Historical collapse-heavy sessions (`a7796e94`, `quack`, `query-cache`) were all subagent-heavy; their 5–15-min gaps were very likely subagent-blocking, not human pauses (multi-hour/overnight gaps are still human).

> ⚠️ **Correction:** an earlier draft claimed "NOT TTL — 64% occur <5 min." Wrong — an artifact of measuring gaps between *transcript records* (one response splits across several records with ~0 gap). Deduped by response it is 93% TTL. A **1-hour TTL would prevent** the 5–60-min-gap collapses (most of them); the cleanest targeted fix is keeping the main prefix warm *while a subagent is running* (bounded, known trigger).

## 3. Cache breakpoints — Claude Code uses 3 of 4, with no history anchor

Every request places exactly three `cache_control` breakpoints:

```
system[1], system[2]   (fixed, static prefix ≈ 18K tokens = system prompt + tool schemas)
messages[LAST]         (single advancing breakpoint on the newest message)
```

The conversation history is cached **only** via that one advancing breakpoint. The 4th available slot is unused — there is **no stable anchor inside the history**. So when the advancing breakpoint fails to reach the prior cached position, the read falls all the way back to the ~18K static prefix and everything after it is re-written.

## 4. The 20-block lookback limitation (documented; NOT the historical driver)

Documented behavior: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>

> "The lookback window is 20 blocks. The system checks at most 20 positions per breakpoint, counting the breakpoint itself as the first."

A **block** = one entry in a message's `content` array. Between two requests the blocks added ≈ **`2K + 1`**, where **K = parallel tool calls in one assistant message**; crossing the window needs K≥10 (≥21 blocks), which would force a full history re-encode. Sequential tool use is safe.

**However — verified against the historical usage data, this is NOT what drove the rebuilds:**
- Max parallel tool fan-out in **any** response across all 188 sessions was **10, occurring once**. ~99% of responses make ≤2 tool calls.
- **0 of the rebuild requests appended >20 blocks; 0% of rebuild write tokens** are on >20-block requests; 0 sessions affected.

So the 20-block overrun is real and documented, and the harness's breakpoint layout (below) *makes it possible*, but it accounts for **essentially none** of the observed historical churn. An earlier draft of this summary claimed it was the root cause — that was an over-extrapolation from a single live-capture turn and is retracted.

## 5. What the rebuilds are NOT — and what remains open

The collapse signature: surviving cached prefix pinned at ~18K (system+tools only) while the whole history re-writes. **Cause (verified): 5-minute TTL expiry — 93% of collapse tokens follow a >5-min gap; 0% model switch; 7% sub-5-min residual (§2).** Other hypotheses, ruled out:

- ❌ content injection / mutation — history is byte-stable, append-only (capture step 2)
- ❌ interleaved threads — 74% of rebuild turns are linear (transcript step 1)
- ❌ 20-block overrun — max fan-out 10 (once); 0% of rebuild tokens (§4)
- ❌ model switch — 0% (only `claude-opus-4-8` in the affected sessions)
- ⚠️ concurrent/timing — a live subagent+concurrent capture (10/52 requests in-flight-overlapping) produced **no** collapse, weakening this; the small 7% sub-5-min residual is the only room left for it

Collapse-heavy sessions: `a7796e94` (7.06M; 17/18 collapses TTL), `818bb54c` (6.41M), `80c5cbd2` (5.03M) — all intermittent, long-lived.

---

**Status:** root cause established and live-confirmed — **the 5-minute cache TTL expiring while the main agent is blocked on a subagent running >5 min** (also plain idle gaps in intermittent sessions). The dramatic re-encodes (up to ~489K on a ~507K prefix, 96%) happen because the history is huge by the time the cache expires. Not the 20-block overrun (ruled out), not model switch (0%), not injection/interleaving.

**Measured cost (this live session):** **$5.44 of a $48.39 bill = 11.3%** lost to 4 subagent-induced collapses (each re-encoding 210–270K tokens; $1.18→$1.54, rising as history grows). **Across all historical sessions:** ~$168 of a ~$762 corrected bill (**22%**) — see below.

**Mitigations:** (1) keep the main prefix warm *while a subagent runs* — bounded, known trigger, cheapest targeted fix; (2) 1-hour TTL (catches 5–60-min gaps, 2× write-premium tradeoff); (3) `/clear` / shorter sessions so a re-encode is cheap; (4) subagents that finish <5 min. All but (3)/(4) are harness-side. ~7% of collapses remain sub-5-min and unexplained.

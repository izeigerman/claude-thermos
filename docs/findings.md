# Claude Code token & cache-usage investigation

_Analysis of local Claude Code session transcripts in `~/.claude/projects/**/*.jsonl`._
_Date: 2026-07-17_

## ⚠️ Correction (2026-07-17) — supersedes the root-cause claims below

Sections 9 and 10 conclude the rebuild churn is "CONFIRMED" as the **20-block lookback overrun**. **That conclusion is retracted.** Verified against the historical usage data (grouping transcript records by `message.id`):

- Max parallel tool fan-out in **any** response across all 188 sessions = **10, occurring once**; ~99% of responses make ≤2 tool calls.
- **0** rebuild requests appended >20 blocks; **0%** of rebuild write tokens are on >20-block requests.

So the 20-block overrun is real and documented, and the harness's breakpoint layout makes it *possible*, but it drives **essentially none** of the observed historical rebuilds.

**ROOT CAUSE (established & live-confirmed 2026-07-17): the 5-minute cache TTL expiring — most often while the main agent is blocked on a long-running subagent.** Deduped by API response (`msg.id`): **93% of collapse write tokens follow a >5-minute gap; 0% model switch; 7% sub-5-min residual.** The cache is `ephemeral_5m`, so a gap >5 min expires it and the next turn re-encodes the whole (by then large) history (up to ~489K tokens, 96% of a ~507K prefix).

The **trigger is usually not human idle**. A subagent's requests use a *different* prefix (different system/tools), so they never refresh the main agent's cache; while a subagent runs for minutes the main prefix ages untouched and expires, and the main agent's next turn — a byte-identical pure append — collapses (`read=0`). Live-captured proof (full request+usage+timing): main turns seq 376 / 485 / 525 had gaps of 11.8 / 7.1 / 5.4 min **each entirely filled by 18–69 subagent requests**, then re-encoded 229K / 252K / 272K tokens from zero. Continuous no-subagent sessions never collapsed. Measured cost of this live session: **$5.44 / $48.39 = 11.3%**; historical: ~$168 / ~$762 corrected bill = **22%**.

**A second correction:** I earlier claimed "NOT TTL — 64% of rebuilds occur <5 min." That was wrong — an artifact of measuring gaps between *transcript records* (one response is split across several records with ~0 gap, inflating the sub-5-min bucket). Deduped by response, it is 93% TTL. Ruled out as drivers: 20-block overrun, content injection, interleaved threads, model switch. See `findings_summary.md` §2/§5 for the corrected summary. Read §9–§10 below (the 20-block material) as "a documented mechanism that turned out not to be the driver." Implication: a **1-hour TTL would prevent** the 5–60 min-gap collapses (most of them), subject to the 2× write premium; and avoiding >5-min idle gaps in large sessions / `/clear`-ing between tasks avoids or cheapens the rest.

## Scope of the data

- **185** transcript files (~**173** contain assistant turns), **~11.6K** assistant requests, **~74 MB** total.
- Two record types drove the analysis:
  - `assistant` records carry `message.usage` → **exact billed tokens**.
  - `user` records carry `tool_result` blocks; `assistant` records carry `tool_use` blocks → **content attribution** (mapped `tool_use_id → tool name`).
- **Caveat on precision:** billed-token figures are exact (straight from `usage`). Content-size figures are **estimated** at `chars / 4` (not a real tokenizer) — good for proportions/ranking, not exact counts. Totals varied ~5% between runs (e.g. 93.9M vs 98.9M writes) due to parse differences; treat absolute numbers as ballpark, proportions as reliable.

## 1. Where the billed tokens go

| Category | Tokens | Share | Rate (× base input) |
|---|---:|---:|---|
| **Cache reads** (context re-sent each turn) | ~1.23B | **92.7%** | **0.1×** |
| **Cache writes** (`cache_creation`, 5-min TTL) | ~94–99M | ~7.1% | **1.25×** (2× if 1-hour TTL) |
| **Output** (generation) | ~10.8M | 0.8% | **5×** (output priced 5× input: $25 vs $5 /1M on Opus) |
| **Uncached input** | ~2.3M | 0.2% | **1×** (the base rate everything else is relative to) |

**Multiplier reference** (all relative to the base **uncached input** price = 1×):
- cache **read** = **0.1×** · cache **write** 5-min = **1.25×** · cache **write** 1-hour = **2×** · uncached input = **1×** · **output** = **5×** (Opus: $5/1M input, $25/1M output).
- Break-even: a 5-min cached token pays for itself after ~2 reads (1.25× + 0.1× < 2×); a 1-hour token after ~3 reads.

Rough cost (Opus 4.8 rates, mixed-model caveat): **~$1,480 total**, of which cache reads ~$615, writes ~$587, output ~$270. Caching saves ~10× on the dominant driver (reads at 0.1× vs 1× input).

## 2. Unique content breakdown (est., chars/4) — ~5.8M total

| Category | Est. tokens | Share |
|---|---:|---:|
| tool_results (reading outputs back) | 3.0M | 51.6% |
| tool_use requests (Edit/Write payloads) | 1.1M | 18.3% |
| attachments | 1.0M | 17.8% |
| assistant text | 365K | 6.3% |
| user prompts | 335K | 5.8% |
| assistant thinking | 18K | 0.3% |

Within tool_results: **Read 65% (2.0M), Bash 30% (894K)**, Agent/Edit/others the rest.
Tool call counts: Bash 2,601 · Read 1,304 · Edit 1,087 · Write 202 · Agent 127.

> **Terminology note:** "Read" (file reads) ≈ **2.0M**. The "**tool_results ≈ 3.0M**" figure is the total of **all** tools' outputs (Read 2.0M + Bash 0.9M + others), *not* Read alone — do not conflate them. The consolidated, rerunnable script `token_report.py` also reports a "tools" bucket of ~4.08M, which is larger still because it adds **tool_use request payloads** (Edit/Write file bodies, Bash command strings) on top of the results.

> **Source of truth:** the numbers in this doc came from throwaway scripts and drift as new sessions are logged. Treat **`token_report.py`** (in this folder) as canonical; rerun it for current figures (`python3 token_report.py` or `--json`).

## 3. Repeated file reads

- Hot files read **25–49× across sessions**: `simulator.py` (49×), `query_builders.py` (48×), `report.py` (44×), `sql_service.py`, `scenario.py`, etc. (quack simulator + query-cache API).
- **Within-session** re-reads = ~188K tokens ≈ **10% of Read tokens** (162 of 999 session/file groups read >1×). The rest of the repetition is **across** sessions.
- Biggest Bash outputs (~4–5K each): `pytest -v` (verbose + ANSI), `cat`/`cat -n` of whole files/YAML, integration-test failure dumps, pre-commit hook output on `git commit`.

## 4. How caching actually works (mechanics confirmed)

- **Stateless API**: the client re-sends the *entire* prompt every request. Nothing is stored server-side by session ID.
- The backend **hashes the prefix** (`tools → system → messages`, up to each `cache_control` breakpoint) and reuses precomputed **KV state** for a matching prefix. Cache "read" = skip the forward-pass compute (billed 0.1×); the tokens still travel the wire.
- **Client controls writes** (breakpoint placement, ≤4 per request, 5m/1h TTL); **reads are automatic** via longest-prefix match. In this data the "client" is Claude Code, placing breakpoints on our behalf.
- **Scope = organization + exact-prefix + TTL, never session.** A byte-identical prefix from another session *can* hit — but per-session injected context (cwd/date/git) usually makes prefixes unique, and the 5-min TTL usually expires between sessions.
- **Delta caching** is emergent from content-hash longest-prefix lookup (not a stored pointer). Match-finding is O(1) probes over a bounded (~20-block) lookback fed by an O(n) incremental hash — **not O(n²)**; the only quadratic is the resend-everything cost inherent to a stateless protocol.

## 5. The cache-write puzzle: 94–99M writes vs only ~5.8M unique content

Writes are **~17× the unique content** — they measure *how many times material was re-encoded*, not how much distinct material exists. A single write is a contiguous prefix snapshot (system + all tool schemas + entire history so far), not categorized by tool.

Decomposition of ~99M writes (all writes billed at **1.25×**; the loss is that the cold-rebuild portion *would have been* **0.1×** reads if the cache had stayed warm — a ~12× rate difference on that slice):
- **72% (~71.5M) = cold rebuilds** (`create > read`): a large prefix re-written at 1.25× instead of read at 0.1×.
- **28% (~27.4M) = warm deltas** (normal per-turn increments; genuinely new content, correctly written once).
- Session-initial cold starts: only **~1.8M**.
- **64% of ALL write tokens (62.9M) came from just 345 requests that each wrote >50K** — huge rebuilds deep in large sessions.

## 6. Would a 1-hour cache TTL help? (gap analysis)

Cold-rebuild write tokens bucketed by time-gap since previous request:

| Gap | Tokens | Share | 1h TTL helps? |
|---|---:|---:|---|
| < 1 min | 45.3M | 63% | ❌ cache was warm → not TTL |
| 1–5 min | 0.8M | 1% | ❌ |
| **5–60 min** | **19.1M** | **27%** | ✅ reclaimable by 1h TTL |
| > 60 min | 5.4M | 8% | ❌ both expire |

**Verdict: 1h TTL is a marginal lever** — recovers ~27% of cold rebuilds (~19% of writes) on paper, but the **2× write premium** (vs 1.25× for 5-min) likely offsets it unless applied surgically. The dominant 64% happens within <5 min, so it's not a TTL problem at all.

## 7. Root cause of the <5-min rebuilds (the main finding)

My initial hypotheses were **wrong** (measured: lookback overrun 0 events, edits 0, big tool output ~1%). The real signature:

- **6%** of <60s rebuilds are `read == 0` — all **subagent sidechains** (`isSidechain: true`), legitimately fresh contexts.
- **94% (42.7M)** are `read > 0` with the **surviving prefix pinned at ~18K (median & p90 = 18K)** — the static `tools + system` block. Everything after it — **the entire conversation history (100K–500K tokens)** — is re-written **within seconds**, turn after turn.

Meaning: the static system prefix caches perfectly, but **the conversation history fails to reuse across turns** in a handful of very large sessions. Invalidation is pinned at the fixed ~18K boundary (where system ends and message history begins) — so the trigger is a **single volatile block at the head of the message array** (Claude Code re-rendering head-of-context: system-reminders / env / todo / state) **or interleaved threads sharing only the system prefix** — *not* edits, lookback, or tool outputs (those would invalidate at varying, later positions).

**Follow-up (steps 1–2, run against the transcripts) narrowed this further:**

- **Thread structure (step 1):** across all mid-thread rebuild turns — **74% linear extension, ~0% divergent branch (1 case), 25% sidechain**. The rebuild turns extend a single linear chain (prev turn's whole message chain is a prefix of the current one). → **Interleaved threads (#2) is ruled out**; the 25% sidechains are the subagent `read==0` cases.
- **Message-chain diff (step 2):** on a rebuild turn (session `a7796e94`): append-only history, `cache_read` pinned at exactly **18,196** (= system + tool schemas) while the entire message history is re-written. Verified across distinct API responses (deduped by `msg.id`): **18 collapse responses** in this one session; the worst re-wrote **488,678 tokens against a ~507K prefix — 4% read, 96% re-encoded.** The `cache_read` floor is a hard, exact `18,196` on every top collapse. Also: **0 of 1,337 records contain a `system-reminder`** block. _(An earlier draft cited "~224K" here — that was the median collapse turn; the verified max is ~489K. Note also: the transcript splits one API response across multiple records with duplicated usage, so dedupe by `msg.id` before counting.)_

**What's ruled out now:** interleaved threads, stored-message content change, TTL expiry.
**What remains (indistinguishable without seeing sent bytes):** (a) breakpoint placement — history never gets a reusable breakpoint; (b) 20-block lookback — per-turn tool blocks push the prior history checkpoint out of the lookback window; (c) send-time injection right after the static prefix. All three are harness behavior and produce the identical "read pinned at ~18K, stable history re-written" signature. Distinguishing them needs request-body capture — see step 3 below.

### Concentration — it's a few big sessions, not everything

| Session | Project | Rebuild turns | Re-written | Surviving |
|---|---|---:|---:|---:|
| `a7796e94` | orca/speculative-submit | 29 | 7.06M | 18K |
| `818bb54c` | claude/query-cache | 29 | 6.41M | 18K |
| `80c5cbd2` | claude/quack | 21 | 5.03M | 18K |
| `1a4f1ed7` | claude/query-cache | 22 | 4.30M | 17K |
| `158026c2` | claude/quack | 14 | 2.42M | 18K |

Top 8 sessions ≈ **32M of the 45M** <60s rebuild tokens.

## 8. Takeaways / levers (in order of impact)

1. **Session size & count is the #1 lever.** Rebuild cost scales with history size — a 500K-token session re-encoding its history 29× is where the write tokens went. Shorter, `/clear`-separated tasks shrink every rebuild roughly linearly. This helps *both* the invalidation bucket and the TTL bucket.
2. **1h TTL: marginal**, and offset by the 2× write premium. Not the fix for the dominant <5-min bucket.
3. **Reduce re-read of hot files** (offset/limit reads instead of whole-file; trust context already loaded) — biggest *content* driver but small vs. the rebuild churn.
4. **Cheap Bash wins:** drop `pytest -v` (or use `--tb=short`), use `Read` w/ offset instead of `cat -n`.
5. The head-of-context invalidation is largely **harness behavior**, not prompt-controllable — session hygiene is the practical handle on it.

## 9. Step 3 — capturing real request bodies (to finish the diagnosis)

To distinguish the three remaining causes (breakpoint placement / lookback / send-time injection) we need the exact bytes Claude Code sends. No TLS cert is required if mitmproxy runs in **reverse-proxy** mode and Claude Code points at it over plain HTTP.

```bash
pip install mitmproxy
mitmdump --mode reverse:https://api.anthropic.com -p 8080 -s capture_addon.py

# in the shell that runs Claude Code:
export ANTHROPIC_BASE_URL=http://localhost:8080
claude    # drive a long session to reproduce the churn
```

- `capture_addon.py` writes each `/v1/messages` body to `captures/NNNNN_<ts>_req.json` and a summary (incl. `cache_control` breakpoint positions) to `captures/index.jsonl`. Responses stream through untouched (no timeout / no loss of token streaming).
- Then diff two consecutive captures:
  ```bash
  python3 diff_requests.py captures/00007_*.json captures/00008_*.json
  ```
  - **Divergence only at the appended tail** → pure append; if it still cold-rebuilt, cause is **breakpoint placement / lookback**.
  - **Divergence early (system[k] / messages[0..])** → **send-time injection**; the tool prints the exact differing bytes (timestamp, todo/state block, re-injected file, …).
- **Cert note:** reverse mode = client→proxy is plain HTTP on localhost (no cert). Forward/transparent MITM would instead need mitmproxy's CA trusted **and** `NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem` (Claude Code is a Node app and ignores the OS keychain) — avoid it; use reverse mode.

### Step 3 results — first capture (Claude Code 2.1.212, 70 requests)

Captured a live session via the reverse proxy and analyzed request bodies:

- **Breakpoint strategy (from `index.jsonl`):** every main-agent request uses exactly **3** of the 4 available `cache_control` breakpoints — `system[1]`, `system[2]` (fixed), and **one advancing breakpoint on the newest message** (`messages[LAST].content[k]`). **There is no stable anchor breakpoint inside the conversation history.**
- **System is byte-stable:** exactly **1 distinct system block across all 62 main-agent turns** (cache_control stripped). No volatile head-of-context injection. (It *contains* a `<system-reminder>` with CLAUDE.md / git branch / etc., but that content does not change turn-to-turn.)
- **History is pure append:** after normalizing out `cache_control` (which the server strips before hashing) and string↔block reshaping, **62/62 consecutive turns are PURE APPEND** — zero real content divergence.
- **RULED OUT (from real sent bytes):** send-time injection, content mutation, interleaved threads (subagents are a separate cache lineage — the `tools=10` requests, seqs 49–65).
- **~~CONFIRMED mechanism~~ — RETRACTED (see the Correction at the top).** The live capture confirmed the *breakpoint layout* (single advancing history breakpoint, no anchor) that makes a 20-block overrun *possible*, and one turn (`69→70`) appended 22 blocks. I inferred that was the rebuild driver. **Verified against historical usage, it is not:** max parallel fan-out across 188 sessions was 10 (once), and 0% of rebuild write tokens are on >20-block requests. The layout is real and the overrun is real but rare; it does **not** explain the historical churn (which happens on 2–5-block appends). Actual driver: unidentified.

**Causal confirmation (tooling ready):** `capture_addon.py` now TEEs each response — chunks stream through untouched while a copy is parsed for `usage` (`cache_read`/`cache_creation`/`output`) from the SSE `message_start`/`message_delta` events, written to `captures/index_resp.jsonl`. `correlate.py` joins each request's block-delta to its response usage and cross-tabs **rebuild (`cache_write > cache_read`) vs. >20-block append**. The first captures predate this and lack usage; **recapture a dense/tool-heavy session** (restart `mitmdump` to load the updated addon) then run `correlate.py` to observe rebuilds landing on exactly the >20-block turns.

**Net (corrected):** what's *confirmed* is the harness breakpoint layout (one advancing history breakpoint, no anchor) and that rebuilds are not caused by injection, interleaving, or TTL. What's **not** confirmed — and is in fact contradicted by the historical data — is that the 20-block lookback drives the churn. Root cause remains open (leading candidate: concurrent-request cache-write timing). User-side mitigation regardless: shorter sessions / `/clear`.

## 10. What triggers a rebuild, and what to do about it

### Turns vs. blocks vs. the 20-block limit

- **Block** = one entry in a message's `content` array (`text` / `tool_use` / `tool_result` / `thinking`) — the unit the lookback counts. **One message can hold many blocks** (an assistant message firing 8 parallel tool calls ≈ 9 blocks; the user message returning them ≈ 8 blocks).
- **Turn** — the cache operates at the **API-request** level, not the conversational level. In agentic work one conversational turn = many API requests (one per tool-call loop). Each request lays down the single advancing breakpoint.
- Between consecutive requests the blocks added ≈ **`2K + 1`**, where **K = tool calls in one assistant message** (parallel fan-out): `1 text + K tool_use + K tool_result`.
- The next request reads the history from cache only if its breakpoint can look back within the lookback window to the previous breakpoint. Tipping point:

  ```
  K ≤ 9   → ≤19 blocks → WARM (history reads from cache)
  K ≥ 10  → ≥21 blocks → COLD (lookback overruns → whole history re-encoded from the ~18K system boundary)
  ```

**Source — the 20-block lookback is documented behavior** (Anthropic prompt-caching docs):
<https://platform.claude.com/docs/en/build-with-claude/prompt-caching> (§ lookback window / cache breakpoint limits). Verbatim:

> "The lookback window is 20 blocks. The system checks at most 20 positions per breakpoint, **counting the breakpoint itself as the first**. If the system finds no matching entry in that window, checking stops (or resumes from the next explicit breakpoint, if any)."

The docs' own growing-conversation example is exactly this failure mode, and states the fix explicitly:

> "**Turn 3:** 35 blocks, breakpoint on block 35. The system checks 20 positions (blocks 35 through 16) and finds nothing. The turn-2 entry at block 15 is one position outside the window, so there is no cache hit. **Adding a second breakpoint at block 15 starts a second lookback window there, which finds the turn-2 entry.**"

Notes: (1) it's *20 positions counting the breakpoint itself* (~19 blocks back), so the tipping point is a hair earlier than `2K+1 > 20` implies — `K≥10` still holds. (2) The documented remedy ("add a second breakpoint at the earlier block") is precisely the **history-anchor fix** in the workarounds table — the harness has the spare breakpoint slots (max 4; it uses 3) to do this. (3) Max **4 cache breakpoints** per request is also from this page. (4) *Caveat:* the ~20 is confirmed *documented* behavior; what remains unconfirmed against this workload is whether the specific `>20`-block turns empirically rebuilt — that is what `correlate.py` on a usage-capturing session would settle. (5) The live page lists the Opus 4.8 minimum cacheable prefix as **1,024 tokens** (the bundled skill copy said 4,096 — treat the live page as source of truth; it doesn't affect any conclusion here since the ~18K system prefix clears every version).

- **Sequential tool use is safe** — each call is its own request and re-anchors (~3 blocks/step); you could chain 100 tools sequentially and never overrun. Only **~10+ parallel calls in a single assistant message** jump the gap.
- Therefore: **rebuild frequency ∝ how often a step fans out to ~10+ parallel tools** (dense file/bash work does this constantly; conversational work almost never). **Session size sets the cost** of each rebuild (re-encode 30K early vs. 400K late) but not its frequency.

### Workarounds — and who owns each

| Lever | Who | Effect | Verdict |
|---|---|---|---|
| **Shorter sessions / `/clear` between unrelated tasks** | You | Doesn't reduce overrun frequency, but makes each rebuild cheap | **The practical user lever** — attacks cost, not cause |
| Nudge toward less parallel tool fan-out | You (weakly) | Fewer K≥10 steps → fewer rebuilds | Poor: not really controllable, and forcing sequential calls trades cache savings for many more slow round-trips — usually a net loss |
| **Anchor breakpoint in history** (use the spare 4th `cache_control` slot) | Claude Code (harness) | An overrun reads back to the anchor instead of collapsing to the ~18K system prefix | **The real fix** — not user-exposed; worth filing as feedback with this evidence |

### Measure before optimizing

Caching already saves ~10× (reads at 0.1×) and rebuilds are a minority of total cost. Before changing how you work, capture a real dense session (updated `capture_addon.py`) and run `correlate.py` to see what fraction of *your* spend is rebuild churn. If small → do nothing beyond `/clear` between big tasks. If large → you have hard numbers to file alongside the anchor-breakpoint request.

## Files in this folder

- `token_report.py` — canonical, rerunnable usage report (billed tokens, content breakdown by tool, within-session output duplication). `--json` for machine output.
- `capture_addon.py` — mitmproxy reverse-proxy addon; captures outgoing request bodies, per-response token usage, **and request/response timing** (`t_sent`, `t_resp_start`, `t_resp_end`), TEE'd + streaming-safe, `accept-encoding: identity` forced so usage parses. → `captures/{NNNNN_*_req.json, index.jsonl, index_resp.jsonl}`.
- `diff_requests.py` — diffs two captured requests; pinpoints the first divergent block/bytes and prints breakpoints. (Includes `cache_control` in the compare — for a normalized append check, use `correlate.py`.)
- `correlate.py` — **per-rebuild causal attribution.** For each rebuild (`cache_creation > cache_read`) classifies the cause as `overrun` (>20-block append), `invalidation` (shared prefix changed / not pure append), `ttl` (>5 min gap), `timing` (sent while a prefix-sharing request was still in flight, via `t_sent`/`t_resp_start`), or `unknown` (→ needs server-side cache diagnostics). Validated against a synthetic set covering all five verdicts.
- `findings.md` — this document.

## Methodology notes

- Billed tokens: summed `message.usage.{input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens}` over all assistant records.
- Content attribution: per file, built `tool_use_id → (tool name, input)` from assistant `tool_use` blocks, then attributed each `tool_result` to its tool; sizes estimated `chars/4`.
- Cold-rebuild classification: `cache_creation > cache_read`. Gap = seconds between consecutive assistant timestamps in a file. "Surviving prefix" = `cache_read` on the rebuild turn.
- Ad-hoc analysis scripts were run from `/tmp` (ephemeral). Re-derivable from the definitions above; ask if you want them saved here for reproducibility.

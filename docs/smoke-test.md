# Live smoke test

This is an opt-in check against the real Anthropic API. It costs a little and
needs a working `claude` login. It confirms that the main agent's cache prefix
survives a long subagent run instead of collapsing to a fresh (uncached) prefix.

## What it proves

While the main agent is blocked on a subagent for longer than the prompt-cache
TTL, `claude-warmer` fires `max_tokens: 0` warm requests that keep the main
lineage's prefix warm. When the main agent resumes, its next real request should
read from cache (`cache_read > 0`) rather than re-creating the whole prefix.

## Steps

1. Make sure Claude Code works normally on its own first.

2. Launch Claude Code through the warmer:

   ```
   claude-warmer -- <your usual claude args>
   ```

3. In that session, start a task that spawns a subagent whose work runs for
   **more than 5 minutes** (longer than the cache TTL) while the main agent
   waits — for example, a broad multi-file exploration delegated to a subagent.

4. When the subagent finishes and the main agent takes its next turn, stop the
   session.

5. Inspect the log for that session:

   ```
   ls ~/.claude-warmer/logs/
   cat ~/.claude-warmer/logs/<session_id>/events.jsonl
   cat ~/.claude-warmer/logs/<session_id>/summary.json
   ```

## What to look for

- `warm_fired` and `warm_result` events appear while the subagent is running.
  Each `warm_result` carries a `usage` object with a non-zero `cache_read`.
- After the subagent completes, the main lineage's first real `usage` event
  shows `cache_read > 0` — the prefix was still warm, so the turn did **not**
  collapse to a cold prefix.
- `summary.json` reports a non-zero `warms_fired` and a positive
  `cache_read_total`.

If instead the main agent's resume shows `cache_read == 0` (a full
`cache_creation` on the whole prefix), the cache collapsed and warming did not
take effect — recheck that warming is not disabled
(`CLAUDE_WARMER_DISABLE` unset) and that the subagent genuinely ran past the TTL.

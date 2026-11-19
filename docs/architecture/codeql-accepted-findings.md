# CodeQL Findings Accepted Without Code Changes

This document is the permanent rationale for GitHub CodeQL code-scanning alerts that were **reviewed and intentionally left open** rather than fixed. It exists so that any future contributor (or future scan) encountering these alerts finds the full threat-model analysis, the empirical evidence behind the decision, and the conditions under which the decision should be revisited.

**Policy**: alerts whose risky scenario is realistic are fixed with code changes and closed automatically by the post-merge scan (no dismissals are ever used). Alerts whose flagged scenario is theoretical-only — unreachable, unexploitable on the actual runtime, or part of the feature by design — are documented here and remain open as standing tripwires: if the surrounding code changes in a way that invalidates the analysis below, the alert is the prompt to revisit.

Disposition date: 2026-11-19 (scan of commit `6cfa06e` lineage; fixes for the other themes merged through PR #12 and the `codeql-security-fixes` branch).

---

## 1. ReDoS on `_AUTHOR_ONLY_RE` — `py/redos` (4 alerts)

**Sites**: `zocrates/agents/library_search/citation_verifier.py` — the `_AUTHOR_ONLY_RE` pattern and its single call site in `extract_claim_occurrences()` / `_find_backward_claim_boundary()`.

**Flagged flow**: request → LLM-synthesized markdown → citation-token claim extraction → `.match()` on the ≤800-character pre-citation span.

**Why not fixed**:

- **No measurable super-linear behavior on any supported runtime.** The exponential exists in the abstract regex semantics the detector models. Timing probes using the detector's own suggested pumping inputs, wide character-class runs, and mixed-branch inputs found no measurable slowdown on CPython 3.12, 3.13, or 3.14, nor with the production virtual environment, at all reachable input sizes.
- **Input is bounded and synthesized.** The span passed to `.match()` is capped at `MAX_CLAIM_CHARS` (800) in `_find_backward_claim_boundary()`, and the data path is assistant-generated synthesis text — not raw external strings.
- **Every "fix" was empirically worse.** Atomic-group and possessive rewrites were tested with a 200k-input randomized differential harness: 22 mismatches, including realistic inputs like `"Smith, et al"` flipping from match to no-match. The separator alternation shares whitespace runs between adjacent separators (e.g. `", "` + `" et al"` vs `","` + `" et al"`), so the match set provably depends on partial backtracking that any atomic/possessive rewrite removes. The pattern is documented as intentionally unchanged at its definition site.
- **Worst realistic case** is a brief CPU stall of the local single-user backend event loop, reachable only by an attacker who can already place adversarial content in the user's library *and* have the LLM faithfully echo it beside a citation token — four layers of dilution (conversion → chunking → reranking → LLM synthesis) away from any external source.

**Revisit when**: the pattern is reused on unbounded input, the `MAX_CLAIM_CHARS` bound is removed, or the separator alternation is edited (then re-run a randomized differential harness against the captured ground-truth behavior table before accepting a rewrite).

---

## 2. Path injection — `py/path-injection` (5 alerts)

**Sites**: `zocrates/fast_api_app.py` — `register()` (`POST /register`) and `discover_cleanup_batch()` (`POST /discover/cleanup_batch`).

### 2a. `register()` — user-chosen data directory (1 alert)

**Flagged flow**: request body `zotero_data_dir` → `Path(...).expanduser().resolve()` → service initialization, logging, and config writes inside that directory.

**Why not fixed**: accepting a user-chosen Zotero data directory **is the endpoint's feature** — the Zotero sidebar's registration flow exists precisely to let the user point Zocrates at their data directory. The path is already gated by three existence checks (directory, `zotero.sqlite`, `storage/`), and everything derived from it is constructed *inside* it. There is no prefix to check against: any absolute path is legitimate input. Exploitation requires possession of the local auth token — with which an attacker could equally call the endpoint legitimately. No privilege escalation; nothing escapes the chosen directory.

### 2b. `discover_cleanup_batch()` — validated batch token (4 alerts)

**Flagged flow**: request body `batch_token` → `staging_root / token` → `exists()` / `is_dir()` / `shutil.rmtree()`.

**Why not fixed**: traversal is **impossible at the validation layer**, before any path arithmetic runs. The token must match `^[a-zA-Z0-9_\-]+$`, a class that excludes `/`, `\`, and `.` — so `..` cannot match and the joined path can only ever be a direct child of the staging root. A second, redundant defense (`resolve()` + `is_relative_to(staging_root)` + `!= staging_root`) backs the regex up. The worst an authenticated caller could do is delete a staging subdirectory, which is the endpoint's purpose; staging batch tokens are unguessable `secrets.token_hex(16)` values.

**Why no detector-satisfying rewrite**: four code shapes that CodeQL's guidance would normally accept were tested locally against the actual query (string-typed path with `str.startswith` prefix guards in raise-style, flat, and nested forms; `os.path.isdir`; `re.fullmatch`). None cleared the alerts. Inspection of the query internals showed `str.startswith` guards *are* modeled as barriers, but taint reaches the sinks in a flow state the barrier does not cut, and `Path.is_relative_to` and regex allowlists are not recognized at all. Further contortion of sound code would be pure regression risk with zero security gain.

**Revisit when**: the token alphabet is widened, the containment check is removed or weakened, or the endpoint's authentication requirement changes.

---

## 3. Stack-trace exposure — `py/stack-trace-exposure` (12 alerts)

**Sites** (all `zocrates/fast_api_app.py` unless noted): `test_converter_connection()` Datalab probe (4), OpenRouter key test (1), `/llm/decision_tool/install` + `/start` via `zocrates/rag/decision_service.py` messages (2), the four retrieval endpoints wrapping the shared toolkit — search, table-of-contents, read, related-items (4), OpenAlex key test (1).

**Flagged flow**: `except Exception` → `str(e)` / `proc.stderr` → HTTP response `message`/`detail`.

**Why not fixed**:

- **The rule overstates the leak.** The source is "any exception object", but the sinks all receive `str(e)` — exception *messages* only. A traceback, stack frames, or source lines can never appear in these responses.
- **Realistic leak content** is limited to file paths or config values embedded in exception messages, or upstream API error text.
- **The exposure surface is the user's own machine.** Every endpoint sits behind `verify_auth` on a loopback bind with strictly bounded CORS. The "external user" is the user's own Zotero sidebar or explicitly configured local MCP clients — all running as the same OS user, who could read the log file or config directly.
- **No secret leakage**: API keys travel in headers, and no flagged path echoes request headers back.
- **These are deliberate diagnostics UX.** The "Test connection" buttons exist to show the user *why* a key was rejected (an explicit design decision, noted in code comments); Laya install/start errors embed `proc.stderr` because that is what the user needs to fix the installation (full detail is also logged server-side); the retrieval endpoints' exception-derived messages additionally feed **agent recovery** — the ReAct search subagent reads these error strings to self-correct, and genericizing them would degrade both the agent loop and MCP client behavior.

**Revisit when**: any of these endpoints becomes reachable without the local auth token, or a message is found to echo credentials or header content. A separate, non-alert-driven hardening option remains open: generic-izing only the 500-level internal errors on the four retrieval endpoints while keeping the human-authored 400/404 messages.

---

## 4. Incomplete multi-character sanitization — `js/incomplete-multi-character-sanitization` (1 alert)

**Site**: `zotero-plugin/addon/sidebar.js` — `mapItemToChipData()` note-title strip (`rawNote.replace(/<[^>]*>/g, "")`).

**Flagged flow**: Zotero note HTML → tag-strip regex → chip title.

**Why not fixed**: the stripped note text feeds **only text-safe DOM sinks** — `textContent` and the `.title` attribute on the chip label elements. The chip construction uses `createElementNS` + `textContent` exclusively, with no `innerHTML` anywhere in the block. Residual `<script` *text* in a title can therefore never be parsed as HTML. The only residual issue is cosmetic: the tag-strip regex can mangle chip titles containing literal non-tag angle-bracket text (e.g. `a<b and c>d`); that affects the chip label display only, with no security impact.

**Revisit when**: the chip title is ever rendered via an HTML-parsing sink, or the strip regex is reused for a sink that parses HTML.

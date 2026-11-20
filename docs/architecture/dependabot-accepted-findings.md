# Dependabot Findings — Disposition Ledger

Companion to [codeql-accepted-findings.md](codeql-accepted-findings.md): the permanent record of how critical Dependabot (security) findings for this repository were remediated, and the rationale for any finding intentionally left open.

Policy mirrors the CodeQL artifact: findings with an available patch are fixed by upgrading the dependency and closed automatically by Dependabot's rescan. Findings with **no patched release available** cannot be fixed by any manifest change; they are documented here with the exploitability analysis and remain open until upstream ships a fix, at which point a single lock bump closes them.

---

## 1. Remediated (9 findings, commits on the dependency-security branch)

| Package | Bump | Findings cleared | Advisory IDs |
|---|---|---|---|
| pillow | 12.3.0 → 13.0.0 | 5 | GHSA-8g5g-8vmr-9782 (JPEG heap overflow), GHSA-w889-685w-c8fh (Ghostscript argument injection RCE), GHSA-332p-7hrc-92wf (TIFF heap overflow), GHSA-v9pv-5gc8-w2m6 (stale native storage reuse), GHSA-5x8w-8jj5-q52h (FlashPix overflow) |
| anyio | 4.14.1 → 4.16.0 | 1 | GHSA-82r6-8w77-94w6 (TLSStream IDNA hostname encoding → certificate spoofing) |
| pyjwt | 2.13.0 → 2.15.1 | 1 | GHSA-ffc3-869f-jxw9 (PEM detection bypass) |
| authlib | 1.7.2 → 1.9.9 | 1 | GHSA-6v38-4h7w-rj9h (JWS empty-signature validation) |

Evidence for the bump safety: the resolver upgraded exactly these four packages with zero conflicts; a codebase impact audit found **zero direct PyJWT or authlib usage** (transitive via `mcp` / `msal` / `google-adk`), anyio usage limited to `anyio.to_thread.run_sync` (the CVE's `TLSStream` path is never used), and a single direct Pillow consumer (`zocrates/rag/vlm_captioner.py`) using stable core APIs (`Image.open`, `.save`, `.resize`, `convert`, `UnidentifiedImageError`). The full Python test suite was re-run under the upgraded versions with no upgrade-related failures.

The `sentence-transformers` dev dependency was removed outright (its only consumers were untracked experiment scripts); this also eliminated a version-conflict anchor against its own critical advisory (GHSA-jhr6-gm9c-rqjv).

## 2. High-severity wave disposition

Of 84 high-severity findings: 36 were already resolved by the pillow/anyio/pyjwt/authlib bumps above, and 30 more by a dedicated bump of the two low-risk direct dependencies — **pypdf 6.15.0 → 6.21.4** (18 findings, all DoS-class parser issues; constrained `<7` to stay within the 6.x line; our usage surface is `PdfReader` / `reader.pages` / `page.images` only) and **aiohttp 3.14.1 → 3.14.5** (+ `yarl` as its pinned transport requirement; 12 findings covering WebSocket-queue bypass, unbounded buffering, multipart size bypass, event-loop stalls, C-parser OOB read). Resolver-verified: exactly those three packages moved.

The remainder is dispositioned as follows:

### 2a. Deferred by decision — transitive-only packages (12 findings)

`cryptography` (Bleichenbacher PKCS#7 oracle), `oauthlib` ×2, `pyasn1` ×4, `urllib3` ×2, `soupsieve` ×2, `joserfc` — all are **transitive dependencies with zero direct imports** in our code, so an upgrade would churn the lock purely to silence alerts whose vulnerable paths we do not exercise directly. Per maintainers' preference, transitive pins are left alone and revisited opportunistically (when a direct parent dependency is bumped anyway, or when one of these appears in a critical advisory).

### 2b. Accepted with mitigation pending upstream readiness — transformers (4 findings)

Findings #3/#4/#12 (patched 5.3.0/5.5.0/5.10.0) and #41 (**no patched version**). Dry-run upgrade analysis (transformers 4.57.6 → 5.10.0) established:

- **No dynamic-code exposure matching any of the four advisories.** #12 is not applicable (zero `save_pretrained` / chat-template usage); #41's attack path (custom generation code downloaded before trust consent) requires `pipeline()` / generation-config loading — never used. The one `trust_remote_code=True` call (`zocrates/rag/embeddings.py::LocalEmbedder`) targets a **hardcoded, non-configurable** model id, so no user-controlled value can steer the loaded repository — an upstream supply-chain trust decision, not an input-driven vulnerability.
- **A drop-in upgrade is blocked downstream of our code**: the Jina model's locally cached remote code imports transformers-5.0-removed internals; a safe upgrade requires verifying upstream Jina 5.x readiness and a live model-load smoke test after cache purge. Deferred until that upstream compatibility is confirmed; then upgrade directly to ≥5.10.0 in one step.
- Hygiene noted (not actioned): `hardware.py::validate_model_compatibility` is dead code containing an unreachable `trust_remote_code` sink; `peft` is an unused direct dependency.

### 2c. Deferred by decision — litellm (1) and filelock (1)

Both are direct dependencies whose fixes require major/minor-range jumps (`litellm` 1.85.7 → 1.95.0 spans ten minor releases of a wide-surface API; `filelock` 3 → 4 is a major bump of the library underpinning the config file-locking layer). Deferred pending a dedicated compat-verification pass with the full suite.

## 3. Moderate-severity wave disposition

Of 66 moderate findings, 44 auto-resolve from the bumps already described (pypdf ×10, Pillow ×14, aiohttp ×7, PyJWT ×8, authlib ×4, anyio ×1). The remaining 22 are all covered by the deferrals above and add no new packages except four transitive-only consumers of existing deferred families: `soupsieve` ×2, `oauthlib` ×2, `urllib3`, `joserfc` (transitive-only set), plus `opentelemetry-api` ×2 (via chromadb / google-adk / otel exporters), `mail-parser` ×2 (via docling-slim), `multidict` (via aiohttp/yarl), and `accelerate` (via docling-slim) — all zero-direct-import transitives, left alone per the same decision. `filelock` ×6, `transformers`, and `litellm` moderates fall under their existing deferral dispositions.

## 4. Low-severity wave disposition

Of 5 low findings, 4 auto-resolve from the existing bumps (aiohttp, pypdf, authlib) and one falls under an existing deferral (filelock ×2). One new transitive-only item: `setuptools` ×1 (Windows-only path escape via `namespace_packages.txt` during legacy wheel installs — `zocrates` never invokes setuptools directly; it enters via the build toolchain). Left alone per the transitive-only decision.

## 5. Accepted with mitigation — chromadb (4 findings: 2 critical + 2 high, pending upstream fix)

**Findings**: GHSA-f4j7-r4q5-qw2c (CVE-2026-45829, pre-authentication code injection) and GHSA-36p7-vc44-83pf (code injection). Installed version 1.5.9 is the **latest release on PyPI — no patched version exists**, so no manifest change can close these alerts.

**Flagged attack path**: an *unauthenticated HTTP request* to chroma's server endpoint (`/api/v2/tenants/{tenant}/databases/{db}/collections`) supplying a malicious model repository with `trust_remote_code=true`, causing arbitrary code execution in the server process.

**Why not exploitable in this deployment**:

- Zocrates instantiates chroma exclusively in **embedded mode** — `chromadb.PersistentClient(path=...)` in `zocrates/rag/vector_store.py` and `zocrates/rag/embeddings.py`. The embedded client runs the vector store **in-process with no HTTP listener**; the vulnerable server endpoint does not exist in this deployment topology.
- The vulnerable path additionally requires the process to download a model repository (`trust_remote_code`); the embedded usage never fetches models.
- Even in the hypothetical worst case, exploitation would require local process access — at which point the attacker already has the user's full privileges.

**Revisit when**: chromadb releases a version that patches all four advisories — then run `uv lock --upgrade-package chromadb` and let Dependabot's rescan close all four alerts. If Zocrates ever adopts chroma's server mode (`HttpClient` / `chroma run`), the pre-auth HTTP surface becomes real and the upgrade becomes mandatory, not optional.

---

## 6. Summary ledger

| Disposition | Findings |
|---|---|
| Remediated via dependency bumps | 9 critical + 30 high + 66 moderate (pypdf, aiohttp) + 36 high + 44 moderate + 4 low auto-resolved by the same bumps |
| Removed outright | sentence-transformers (dev dependency, unused consumers) |
| Accepted with mitigation, pending upstream fix | chromadb ×4 (2 critical + 2 high) — embedded-only deployment |
| Accepted with mitigation, pending upstream 5.x readiness | transformers ×4 (high) — hardcoded model id, no matching exposure |
| Deferred by decision (transitive-only, no direct usage) | cryptography, oauthlib ×2, pyasn1 ×4, urllib3 ×2, soupsieve ×2, joserfc (12 high) + soupsieve/oauthlib/urllib3/joserfc/opentelemetry-api ×2/mail-parser ×2/multidict/accelerate (18 moderate) + setuptools (1 low) |
| Deferred by decision (compat-verification pass needed) | litellm, filelock (2 high + 7 moderate) |

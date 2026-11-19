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

## 2. Accepted with mitigation — chromadb (2 findings, pending upstream fix)

**Findings**: GHSA-f4j7-r4q5-qw2c (CVE-2026-45829, pre-authentication code injection) and GHSA-36p7-vc44-83pf (code injection). Installed version 1.5.9 is the **latest release on PyPI — no patched version exists**, so no manifest change can close these alerts.

**Flagged attack path**: an *unauthenticated HTTP request* to chroma's server endpoint (`/api/v2/tenants/{tenant}/databases/{db}/collections`) supplying a malicious model repository with `trust_remote_code=true`, causing arbitrary code execution in the server process.

**Why not exploitable in this deployment**:

- Zocrates instantiates chroma exclusively in **embedded mode** — `chromadb.PersistentClient(path=...)` in `zocrates/rag/vector_store.py` and `zocrates/rag/embeddings.py`. The embedded client runs the vector store **in-process with no HTTP listener**; the vulnerable server endpoint does not exist in this deployment topology.
- The vulnerable path additionally requires the process to download a model repository (`trust_remote_code`); the embedded usage never fetches models.
- Even in the hypothetical worst case, exploitation would require local process access — at which point the attacker already has the user's full privileges.

**Revisit when**: chromadb releases a version that patches both advisories — then run `uv lock --upgrade-package chromadb` and let Dependabot's rescan close both alerts. If Zocrates ever adopts chroma's server mode (`HttpClient` / `chroma run`), the pre-auth HTTP surface becomes real and the upgrade becomes mandatory, not optional.

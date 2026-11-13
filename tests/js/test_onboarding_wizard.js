/**
 * test_onboarding_wizard.js — Phase 7 Stage B: onboarding stage machine,
 * preset→field/payload mapping, state-file I/O, terminal event handling
 * (incl. single-owner abort finalization), the no-stream abort fallback,
 * the Laya setup flow, chat-gate composition, and CONFIGURE gating.
 *
 * Plan references: onboarding_configuration_step_plan.md §4/§5/§6.3/§9.
 * Preset values source of truth: artifacts/plans/model_pricing_tiers_plan.md.
 */

const assert = require("assert");
const {
  test,
  testAsync,
  getTestStats,
  printTestSummary,
  setupMockEnvironment,
  createMockElement,
} = require("./test_helpers.js");

const SIDEBAR_PATH = "../../zotero-plugin/addon/sidebar.js";
const BOOTSTRAP_PATH = "../../zotero-plugin/addon/bootstrap.js";

function makeMockIOUtils() {
  // Minimal IOUtils stand-in: string-keyed file store with atomic move
  // semantics (noOverwrite) matching the temp+rename pattern used by the
  // bootstrap seed and the sidebar state writes.
  const files = new Map();
  const stats = new Map();
  return {
    _files: files,
    _stats: stats,
    async exists(p) { return files.has(p); },
    async stat(p) {
      if (!files.has(p)) {
        const err = new Error("file not found: " + p);
        err.name = "NotFoundError";
        throw err;
      }
      return stats.get(p) || { lastModified: Date.now() };
    },
    async writeUTF8(p, content) {
      files.set(p, String(content));
      if (!stats.has(p)) stats.set(p, { lastModified: Date.now() });
    },
    async readUTF8(p) {
      if (!files.has(p)) {
        const err = new Error("file not found: " + p);
        err.name = "NotFoundError";
        throw err;
      }
      return files.get(p);
    },
    async move(from, to, opts) {
      if (!files.has(from)) throw new Error("move source missing: " + from);
      if (opts && opts.noOverwrite && files.has(to)) {
        throw new Error("move target exists: " + to);
      }
      files.set(to, files.get(from));
      if (stats.has(from)) {
        stats.set(to, stats.get(from));
        stats.delete(from);
      }
      files.delete(from);
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

async function runTests() {
  console.log("\n--- Suite: Onboarding Wizard (stage machine, presets, abort, Laya) ---");
  const env = setupMockEnvironment();

  // Fresh module instances: bust the require cache so module-global state
  // (backendReadyForChat, onboarding stage machine) starts clean — earlier
  // suites in the runner already required sidebar.js.
  delete require.cache[require.resolve(SIDEBAR_PATH)];
  delete require.cache[require.resolve(BOOTSTRAP_PATH)];
  const bootstrap = require(BOOTSTRAP_PATH);
  const sidebar = require(SIDEBAR_PATH);

  const stateFile = "/mock-zotero-profile/zocrates-data/onboarding_state.json";

  function arrangeDataDir() {
    env.zotero.DataDirectory = { dir: "/mock-zotero-profile" };
    if (!env.zotero.Zocrates || typeof env.zotero.Zocrates !== "object") {
      env.zotero.Zocrates = {};
    }
  }

  // =========================================================================
  // 1. bootstrap.js — seed payload + atomic seed write + stage bus
  // =========================================================================
  test("bootstrap: buildOnboardingSeedPayload shape (stage configure, null bookkeeping)", () => {
    const payload = bootstrap.buildOnboardingSeedPayload("2026-01-01T00:00:00Z");
    assert.strictEqual(payload.version, 1);
    assert.strictEqual(payload.stage, "configure");
    assert.strictEqual(payload.seeded_at, "2026-01-01T00:00:00Z");
    assert.strictEqual(payload.config_done_at, null);
    assert.strictEqual(payload.process_done_at, null);
    assert.strictEqual(payload.process_aborted, false);
  });

  await testAsync("bootstrap: seedOnboardingStateFile writes atomically and broadcasts configure", async () => {
    const io = makeMockIOUtils();
    global.IOUtils = io;
    arrangeDataDir();
    const stages = [];
    bootstrap.initOnboardingStageBus(env.zotero);
    env.zotero.Zocrates.addOnboardingStageListener((stage) => stages.push(stage));

    const seeded = await bootstrap.seedOnboardingStateFile("/mock-zotero-profile/zocrates-data", "/");
    assert.strictEqual(seeded, true);
    assert.ok(io._files.has(stateFile), "state file must exist after seed");
    assert.ok(!io._files.has(stateFile + ".tmp"), "temp file must be gone after atomic rename");
    const parsed = JSON.parse(io._files.get(stateFile));
    assert.strictEqual(parsed.stage, "configure");
    assert.deepStrictEqual(stages, ["configure"], "seed must broadcast the configure stage once");

    // Idempotent: a second seed must NOT clobber an existing state file.
    io._files.set(stateFile, JSON.stringify({ version: 1, stage: "process" }));
    const second = await bootstrap.seedOnboardingStateFile("/mock-zotero-profile/zocrates-data", "/");
    assert.strictEqual(second, false, "seed must skip when the state file already exists");
    assert.strictEqual(JSON.parse(io._files.get(stateFile)).stage, "process");
  });

  test("bootstrap: stage bus normalizes invalid stages and fans out to listeners", () => {
    const received = [];
    const unsub = env.zotero.Zocrates.addOnboardingStageListener((stage) => received.push(stage));
    env.zotero.Zocrates.notifyOnboardingStage("process");
    env.zotero.Zocrates.notifyOnboardingStage("bogus-stage");
    assert.deepStrictEqual(received, ["process", null]);
    assert.strictEqual(env.zotero.Zocrates.getOnboardingStageState(), null);
    unsub();
  });

  // =========================================================================
  // 2. Preset → wizard-field mapping + Finish payload building
  // =========================================================================
  test("presets: mapPresetToWizardFields matches the tier JSON for all three tiers", () => {
    const rec = sidebar.mapPresetToWizardFields("recommended");
    assert.strictEqual(rec.model, "openrouter/~z-ai/glm-flash-latest");
    assert.strictEqual(rec.embedMode, "cloud");
    assert.strictEqual(rec.cloudEmbeddingModel, "openrouter/voyageai/voyage-4");
    assert.strictEqual(rec.vlmModel, "openrouter/deepseek/deepseek-v4.1-flash");
    assert.strictEqual(rec.vlmBackupModel, "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free");
    assert.strictEqual(rec.vlmMaxImagesPerDoc, 100);
    assert.strictEqual(rec.warning, false);
    assert.strictEqual(rec.layaRowVisible, false);

    const free = sidebar.mapPresetToWizardFields("free");
    assert.strictEqual(free.model, "nvidia/nemotron-3.5-lightning:free");
    assert.strictEqual(free.cloudEmbeddingModel, "openrouter/nvidia/nemotron-3-embed-1b:free");
    assert.strictEqual(free.vlmBackupModel, "openrouter/nvidia/nemotron-3.5-super-vl:free");
    assert.strictEqual(free.vlmMaxImagesPerDoc, 3);
    assert.strictEqual(free.warning, true, "Free tier must surface the experimental warning");

    const off = sidebar.mapPresetToWizardFields("offline");
    assert.strictEqual(off.provider, "ollama");
    assert.strictEqual(off.model, "", "Offline is a manual path: no model pre-populated");
    assert.strictEqual(off.embedMode, "local");
    assert.strictEqual(off.vlmChoice, "disable");
    assert.strictEqual(off.vlmBackupModel, "");
    assert.strictEqual(off.vlmMaxImagesPerDoc, 5);
    assert.strictEqual(off.layaRowVisible, true);
    assert.strictEqual(sidebar.mapPresetToWizardFields("nope"), null);
  });

  test("presets: Recommended Finish payloads carry the paid matrix", () => {
    const p = sidebar.buildOnboardingFinishPayloads({
      preset: "recommended",
      provider: "openrouter_llm",
      model: "openrouter/~z-ai/glm-flash-latest",
      openrouterKey: "sk-test",
      embedMode: "cloud",
      cloudEmbeddingModel: "openrouter/voyageai/voyage-4",
      converterId: "markitdown",
      vlmChoice: "openrouter",
      vlmModel: "openrouter/deepseek/deepseek-v4.1-flash",
      profile: "  compbio researcher  ",
      email: "me@example.edu",
    });
    assert.strictEqual(p.llmConfig.reranker_model, "qwen/qwen3-reranker-8b");
    assert.strictEqual(p.llmConfig.citation_verification_model, "~typesafe/jev-latest");
    assert.strictEqual(p.llmConfig.override_agent_models.library_synthesizer.model, "openrouter/google/gemini-3.8-flash");
    assert.strictEqual(p.llmConfig.override_agent_models.coordinator.model, null);
    assert.strictEqual(p.llmConfig.openrouter_api_key, "sk-test");
    assert.strictEqual(p.llmConfig.user_research_profile, "compbio researcher");
    assert.strictEqual(p.ragModel.mode, "cloud");
    assert.strictEqual(p.ragModel.model_name, "openrouter/voyageai/voyage-4");
    assert.strictEqual(p.ragConverter.enable_vlm_captioning, true);
    assert.strictEqual(
      p.ragConverter.vlm_captioning_backup_model,
      "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    );
    assert.strictEqual(p.ragConverter.vlm_max_images_per_doc, 100);
    assert.strictEqual(p.layaFlow, false);
  });

  test("presets: Free tier includes the free reranker slug (P1-1 regression)", () => {
    const p = sidebar.buildOnboardingFinishPayloads({ preset: "free" });
    assert.strictEqual(
      p.llmConfig.reranker_model,
      "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
      "Free tier must persist the free reranker — the paid default must not leak through"
    );
    assert.strictEqual(p.llmConfig.citation_verification_enabled, false);
    assert.strictEqual(p.llmConfig.citation_verification_provider, "local_laya");
    assert.strictEqual(p.llmConfig.citation_verification_model, "laya");
    assert.strictEqual(p.llmConfig.enable_predictive_ghost_input, false);
    assert.strictEqual(p.llmConfig.enable_adaptive_compaction, false);
    assert.strictEqual(
      p.llmConfig.override_agent_models.library_synthesizer.model,
      "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    );
    assert.strictEqual(
      p.ragConverter.vlm_captioning_backup_model,
      "openrouter/nvidia/nemotron-3.5-super-vl:free"
    );
    assert.strictEqual(p.ragConverter.vlm_max_images_per_doc, 3);
  });

  test("presets: Offline resets synthesizers to inherit the local model (P2-6 regression)", () => {
    const p = sidebar.buildOnboardingFinishPayloads({
      preset: "offline",
      provider: "ollama",
      model: "llama3.3:70b",
      baseUrl: "http://127.0.0.1:11434",
      embedMode: "local",
      vlmChoice: "disable",
      converterId: "docling_local",
    });
    const overrides = p.llmConfig.override_agent_models;
    for (const role of ["coordinator", "library_search", "library_synthesizer", "online_search", "online_synthesizer"]) {
      assert.strictEqual(overrides[role].model, null, role + " must inherit the local main model");
    }
    assert.strictEqual(overrides.library_synthesizer.thinking_level, "high");
    assert.strictEqual(p.llmConfig.citation_verification_provider, "local_laya");
    assert.strictEqual(p.llmConfig.citation_verification_auto_start_local, true);
    assert.strictEqual(p.llmConfig.enable_adaptive_compaction, false);
    assert.strictEqual(p.llmConfig.enable_predictive_ghost_input, false);
    assert.strictEqual(p.ragModel.mode, "local");
    assert.strictEqual(p.ragConverter.document_converter_id, "docling_local");
    assert.strictEqual(p.ragConverter.enable_vlm_captioning, false);
    assert.strictEqual(p.ragConverter.vlm_max_images_per_doc, 5);
    assert.strictEqual(p.llmConfig.llm_base_url, "http://127.0.0.1:11434");
    assert.strictEqual(p.layaFlow, true);
  });

  test("presets: buildOnboardingFinishPayloads produces vlm_max_images_per_doc per tier and selection override", () => {
    const rec = sidebar.buildOnboardingFinishPayloads({ preset: "recommended" });
    assert.strictEqual(rec.ragConverter.vlm_max_images_per_doc, 100);

    const free = sidebar.buildOnboardingFinishPayloads({ preset: "free" });
    assert.strictEqual(free.ragConverter.vlm_max_images_per_doc, 3);

    const off = sidebar.buildOnboardingFinishPayloads({ preset: "offline" });
    assert.strictEqual(off.ragConverter.vlm_max_images_per_doc, 5);

    const custom = sidebar.buildOnboardingFinishPayloads({
      preset: "recommended",
      vlmMaxImagesPerDoc: 42,
    });
    assert.strictEqual(custom.ragConverter.vlm_max_images_per_doc, 42);
  });

  // =========================================================================
  // 3. Stage machine + state-file I/O
  // =========================================================================
  await testAsync("state machine: setOnboardingStage normalizes and never broadcasts null (P2-9)", async () => {
    arrangeDataDir();
    const notified = [];
    env.zotero.Zocrates.notifyOnboardingStage = (stage) => notified.push(stage);

    sidebar.setOnboardingStage("configure");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");
    assert.strictEqual(sidebar.isOnboardingActive(), true);
    sidebar.setOnboardingStage("bogus");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), null);
    assert.strictEqual(sidebar.isOnboardingActive(), false);
    assert.deepStrictEqual(notified, ["configure"], "invalid/null stage must not be broadcast");
  });

  await testAsync("state machine: writeOnboardingState/readOnboardingStateFile roundtrip + bookkeeping", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    assert.strictEqual(await sidebar.writeOnboardingState("process"), true);
    let read = await sidebar.readOnboardingStateFile();
    assert.strictEqual(read.stage, "process");
    assert.strictEqual(read.corrupt, false);
    assert.ok(read.meta.config_done_at, "config_done_at stamped on entering process");

    assert.strictEqual(await sidebar.writeOnboardingState("ready", { process_aborted: true }), true);
    read = await sidebar.readOnboardingStateFile();
    assert.strictEqual(read.stage, "ready");
    assert.strictEqual(read.meta.process_aborted, true);
    assert.ok(read.meta.process_done_at);

    // No IOUtils → graceful no-write / legacy read
    global.IOUtils = undefined;
    assert.strictEqual(await sidebar.writeOnboardingState("ready"), false);
    const readNoIo = await sidebar.readOnboardingStateFile();
    assert.strictEqual(readNoIo.stage, null);
  });

  // =========================================================================
  // 4. Chat-gate composition (D1) — gate still false on this fresh module
  // =========================================================================
  test("chat gate: starts locked on a fresh sidebar load", () => {
    assert.strictEqual(sidebar.getBackendReadyForChat(), false);
  });

  await testAsync("chat gate: PROCESS stage keeps the gate locked despite /status poll pressure (D1)", async () => {
    sidebar.setOnboardingStage("process");
    sidebar.removeBootstrapProgressCard(); // simulates the 10s poll success path
    assert.strictEqual(sidebar.getBackendReadyForChat(), false, "gate must stay locked mid-onboarding");
  });

  await testAsync("chat gate: mount-time pending read blocks the poll from lifting the gate (P2-10)", async () => {
    sidebar.setOnboardingStatePending(true);
    try {
      sidebar.removeBootstrapProgressCard();
      assert.strictEqual(sidebar.getBackendReadyForChat(), false, "pending state read must hold the gate");
    } finally {
      sidebar.setOnboardingStatePending(false);
    }
    assert.strictEqual(sidebar.getBackendReadyForChat(), false);
  });

  await testAsync("chat gate: flips exactly once when onboarding reaches READY", async () => {
    sidebar.setOnboardingStage("ready");
    sidebar.removeBootstrapProgressCard();
    assert.strictEqual(sidebar.getBackendReadyForChat(), true);
    // One-shot: further polls cannot re-flip (no error, no state change).
    sidebar.removeBootstrapProgressCard();
    assert.strictEqual(sidebar.getBackendReadyForChat(), true);
  });

  // =========================================================================
  // 5. Terminal event handling + two-path abort
  // =========================================================================
  await testAsync("sync events: progress updates the PROCESS card and terminals are one-shot", async () => {
    const counts = { textContent: "" };
    sidebar.setOnboardingProcessCardRefs({
      card: null, fill: null, countsLine: counts, titleLine: null,
      attachedLine: null, warning: null, errorBox: null, errorText: null,
      retryRow: null, retryBtn: null, abortRow: null, adjustBtn: null, stopBtn: null,
    });
    assert.strictEqual(
      sidebar.handleOnboardingSyncEvent("progress", { processed: 2, skipped: 1, errors: 0, total: 8, elapsed_ms: 65000 }),
      "progress"
    );
    assert.ok(counts.textContent.includes("3 / 8"), "counts line must reflect processed+skipped");
    assert.ok(counts.textContent.includes("1m 05s"), "elapsed must be human-formatted");

    assert.strictEqual(sidebar.handleOnboardingSyncEvent("done", { processed: 8 }), "done");
    await flush();
    assert.strictEqual(sidebar.getOnboardingFlowState().finished, true);
    assert.strictEqual(sidebar.handleOnboardingSyncEvent("done", {}), "done", "second terminal must be a no-op return");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready");
  });

  test("updateOnboardingProcessProgress: progress bar width includes errors in numerator", () => {
    const fill = { style: { width: "" } };
    const countsLine = { textContent: "" };
    sidebar.setOnboardingProcessCardRefs({
      card: null, fill, countsLine, titleLine: null,
      attachedLine: null, warning: null, errorBox: null, errorText: null,
      retryRow: null, retryBtn: null, abortRow: null, adjustBtn: null, stopBtn: null,
    });

    sidebar.updateOnboardingProcessProgress({ processed: 1, skipped: 0, errors: 1, total: 3 });

    assert.strictEqual(fill.style.width, "67%", "fill width must include errors (attempted 2/3 = 67%, not 33%)");
    assert.strictEqual(countsLine.textContent, "1 / 3 items \u00b7 1 failed");
    sidebar.setOnboardingProcessCardRefs(null);
  });

  await testAsync("two-path abort: 'adjust' returns to CONFIGURE, 'stop' completes onboarding (§6.3)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    // Reset the one-shot terminal guard left set by the prior done test.
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent("adjust");
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 });
    await flush();
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure", "adjust path must return to the wizard");
    assert.strictEqual(sidebar.getOnboardingAbortIntent(), null, "intent must reset after finalization");

    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent("stop");
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 });
    await flush();
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "stop path must complete onboarding");
    const read = await sidebar.readOnboardingStateFile();
    assert.strictEqual(read.meta.process_aborted, true);
  });

  await testAsync("two-path abort: follower window defers to the initiating window (P2-8)", async () => {
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent(null); // this window did not click abort
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 });
    assert.strictEqual(sidebar.getOnboardingFlowState().abortFallbackTimer, true, "follower must arm the self-finalize fallback");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "process", "follower must NOT self-finalize");

    // Initiating window resolves: the stage broadcast clears the follower's
    // timer and applies the resulting stage.
    sidebar.handleOnboardingStageEvent("ready");
    assert.strictEqual(sidebar.getOnboardingFlowState().abortFallbackTimer, false, "stage resolution must disarm the fallback");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready");

    // Owner went with "adjust" instead: the follower follows into CONFIGURE.
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent(null);
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 });
    assert.strictEqual(sidebar.getOnboardingFlowState().abortFallbackTimer, true);
    sidebar.handleOnboardingStageEvent("configure");
    assert.strictEqual(sidebar.getOnboardingFlowState().abortFallbackTimer, false);
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");
  });

  await testAsync("no-stream abort fallback: finalizes directly even from a terminal error state (P1-3)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    sidebar.setOnboardingStage("process");
    // Drive the card into the terminal error state (one-shot guard set).
    sidebar.handleOnboardingSyncEvent("error", { error: "boom" });
    await flush();
    assert.strictEqual(sidebar.getOnboardingFlowState().finished, true);
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "process", "error must not advance the stage");

    const fetchCalls = [];
    const realFetch = global.fetch;
    global.fetch = async (url, opts) => {
      fetchCalls.push({ url: String(url), method: (opts && opts.method) || "GET" });
      return { ok: true, json: async () => ({ status: "no_active_job" }) };
    };
    try {
      await sidebar.requestOnboardingAbort("stop");
    } finally {
      global.fetch = realFetch;
    }
    assert.ok(fetchCalls.some((c) => c.url.endsWith("/sync/abort")), "abort endpoint must be called");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "no-stream abort must finalize despite the one-shot guard");
    const read = await sidebar.readOnboardingStateFile();
    assert.strictEqual(read.meta.process_aborted, true);
  });

  await testAsync("no-stream abort fallback: 'adjust' from an error state returns to the wizard (P1-3)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    sidebar.setOnboardingStage("process");
    sidebar.handleOnboardingSyncEvent("error", { error: "boom again" });
    await flush();
    await sidebar.requestOnboardingAbort("adjust");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");
  });

  await testAsync("abort race: terminal consuming the intent blocks double-finalize (oracle P1)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingStage("process");

    const realFetch = global.fetch;
    global.fetch = async (url) => {
      if (String(url).endsWith("/sync/abort")) {
        // Simulate the backend broadcasting the "aborted" terminal to the
        // stream subscriber BEFORE the POST response is consumed: the
        // terminal path consumes the intent and finalizes as adjust.
        sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 });
        return { ok: true, json: async () => ({ status: "abort_requested" }) };
      }
      return { ok: true, json: async () => ({}) };
    };
    try {
      await sidebar.requestOnboardingAbort("adjust");
    } finally {
      global.fetch = realFetch;
    }
    await flush();
    // Without the guard the no-stream branch would re-finalize with the
    // consumed (null) intent → aborted_stop → READY (wrong outcome).
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure",
      "the terminal's adjust outcome must win; the branch must skip once the intent is consumed");
  });

  await testAsync("follower fallback body: self-finalizes as aborted_stop when the owner vanishes (oracle finding 3)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingProcessCardRefs({ card: null, fill: null, countsLine: null, titleLine: null,
      attachedLine: null, warning: null, errorBox: null, errorText: null,
      retryRow: null, retryBtn: null, abortRow: null, adjustBtn: null, stopBtn: null });
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent(null);
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 }); // arms the timer

    const gen = sidebar.getOnboardingSyncGeneration();
    sidebar.finalizeFollowerAbortFallback(gen, { processed: 3 });
    await flush();
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "fallback body must finalize as aborted_stop");
    const read = await sidebar.readOnboardingStateFile();
    assert.strictEqual(read.meta.process_aborted, true);
  });

  await testAsync("follower fallback body: a newer run (generation bump) must never be torn down (oracle P2)", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    sidebar.setOnboardingStage(null);
    sidebar.cleanupOnboardingResources();
    sidebar.setOnboardingProcessCardRefs({ card: null, fill: null, countsLine: null, titleLine: null,
      attachedLine: null, warning: null, errorBox: null, errorText: null,
      retryRow: null, retryBtn: null, abortRow: null, adjustBtn: null, stopBtn: null });
    sidebar.setOnboardingStage("process");
    sidebar.setOnboardingAbortIntent(null);
    sidebar.handleOnboardingSyncEvent("aborted", { processed: 3 }); // arms with current generation

    // A new run started anywhere (retry / fresh window) → generation bumped.
    const staleGen = sidebar.getOnboardingSyncGeneration() - 1;
    sidebar.finalizeFollowerAbortFallback(staleGen, { processed: 3 });
    await flush();
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "process",
      "stale-generation fallback must be a no-op");
  });

  // =========================================================================
  // 6. Laya setup flow (Offline path)
  // =========================================================================
  await testAsync("laya: status uses GET (P1-4) and the install branch runs when not installed", async () => {
    const calls = [];
    const realFetch = global.fetch;
    global.fetch = async (url, opts) => {
      calls.push({ url: String(url), method: (opts && opts.method) || "GET" });
      const u = String(url);
      if (u.endsWith("/llm/decision_tool/status")) {
        return { ok: true, json: async () => ({ tool_installed: false, server_running: false }) };
      }
      return { ok: true, json: async () => ({ status: "ok" }) };
    };
    try {
      const result = await sidebar.ensureOnboardingLayaReady(null);
      assert.strictEqual(result, true);
    } finally {
      global.fetch = realFetch;
    }
    const statusCall = calls.find((c) => c.url.endsWith("/llm/decision_tool/status"));
    assert.ok(statusCall, "status endpoint must be called");
    assert.strictEqual(statusCall.method, "GET", "decision_tool/status is a GET route — POST would 405");
    assert.ok(calls.some((c) => c.url.endsWith("/llm/decision_tool/install")), "install must run when tool not installed");
    assert.ok(calls.some((c) => c.url.endsWith("/llm/decision_tool/start")), "start must run after install");
  });

  await testAsync("laya: a running server short-circuits install and start", async () => {
    const calls = [];
    const realFetch = global.fetch;
    global.fetch = async (url) => {
      calls.push(String(url));
      if (String(url).endsWith("/llm/decision_tool/status")) {
        return { ok: true, json: async () => ({ tool_installed: true, server_running: true }) };
      }
      return { ok: true, json: async () => ({}) };
    };
    try {
      assert.strictEqual(await sidebar.ensureOnboardingLayaReady(null), true);
    } finally {
      global.fetch = realFetch;
    }
    assert.strictEqual(calls.length, 1, "running server must short-circuit");
  });

  // =========================================================================
  // 7. CONFIGURE gating (P1-2) — wizard must not open during provisioning
  // =========================================================================
  await testAsync("configure gating: the seed broadcast does not open the wizard before the backend is ready", async () => {
    // Reset the wizard refs via the lifecycle cleanup (stage ready = inactive),
    // then reproduce the mid-provisioning broadcast conditions.
    sidebar.setOnboardingStage("ready");
    sidebar.cleanupOnboardingResources();
    assert.strictEqual(sidebar.getOnboardingWizardRefs(), null, "cleanup must reset wizard refs");

    delete env.zotero.Zocrates._backendReady; // backend NOT ready (mid-provisioning)
    sidebar.setOnboardingStage("configure");
    sidebar.handleOnboardingStageEvent("configure");
    assert.strictEqual(sidebar.getOnboardingWizardRefs(), null, "wizard refs must stay unset while the backend is down");
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure", "stage must still track the broadcast");
    assert.strictEqual(sidebar.isOnboardingActive(), true);
  });

  // =========================================================================
  // 8. State-file seeding discriminator: corrupt file + .venv presence
  // =========================================================================
  await testAsync("state machine: corrupt state file + no .venv degrades to legacy", async () => {
    global.IOUtils = makeMockIOUtils();
    arrangeDataDir();
    global.IOUtils._files.set(stateFile, "{not json");
    assert.strictEqual(await sidebar.onboardingVenvPresent(), false);
  });

  await testAsync("state machine: corrupt state file WITH .venv fails open to configure", async () => {
    global.IOUtils._files.set("/mock-zotero-profile/zocrates-data/.venv", "");
    assert.strictEqual(await sidebar.onboardingVenvPresent(), true);
    sidebar.setOnboardingStage(null);
    await sidebar.initOnboardingStateFlow();
    assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");
    global.IOUtils._files.delete("/mock-zotero-profile/zocrates-data/.venv");
  });

  // =========================================================================
  // 9. testOnboardingConnection (Phase 7 Stage B)
  // =========================================================================
  await testAsync("testOnboardingConnection: success when fetch returns 200 with status ok", async () => {
    const realFetch = global.fetch;
    global.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok", message: "Connection successful! Response: OK" }),
    });
    try {
      const statusNode = createMockElement("span");
      const w = { testStatus: statusNode };
      await sidebar.testOnboardingConnection(w);
      assert.strictEqual(statusNode.textContent, "Connection successful! Response: OK");
      assert.strictEqual(statusNode.classList.contains("ok"), true, "node must have 'ok' class");
      assert.strictEqual(statusNode.classList.contains("fail"), false, "node must not have 'fail' class");
    } finally {
      global.fetch = realFetch;
    }
  });

  await testAsync("testOnboardingConnection: failure when fetch returns 200 with error status", async () => {
    const realFetch = global.fetch;
    global.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: "error", message: "Authentication failed (401)" }),
    });
    try {
      const statusNode = createMockElement("span");
      const w = { testStatus: statusNode };
      await sidebar.testOnboardingConnection(w);
      assert.strictEqual(statusNode.textContent, "Test failed: Authentication failed (401)");
      assert.strictEqual(statusNode.classList.contains("fail"), true, "node must have 'fail' class");
      assert.strictEqual(statusNode.classList.contains("ok"), false, "node must not have 'ok' class");
    } finally {
      global.fetch = realFetch;
    }
  });

  await testAsync("testOnboardingConnection: handles non-string detail array and non-matching success text", async () => {
    const realFetch = global.fetch;
    try {
      // 1. Success with custom message not containing "success" or "connected"
      global.fetch = async () => ({
        ok: true,
        status: 200,
        json: async () => ({ status: "ok", message: "Ready to use" }),
      });
      const node1 = createMockElement("span");
      await sidebar.testOnboardingConnection({ testStatus: node1 });
      assert.strictEqual(node1.textContent, "Connection successful (Ready to use)");
      assert.strictEqual(node1.classList.contains("ok"), true, "must toggle 'ok' class even when raw msg lacks keywords");

      // 2. Failure with object/array detail (FastAPI 422)
      global.fetch = async () => ({
        ok: false,
        status: 422,
        json: async () => ({ detail: [{ loc: ["body", "model"], msg: "field required" }] }),
      });
      const node2 = createMockElement("span");
      await sidebar.testOnboardingConnection({ testStatus: node2 });
      assert.ok(node2.textContent.includes("field required"), "must stringify object detail rather than [object Object]");
      assert.strictEqual(node2.classList.contains("fail"), true);

      // 3. Failure with non-JSON body (e.g. 502 Bad Gateway HTML)
      global.fetch = async () => ({
        ok: false,
        status: 502,
        json: async () => { throw new Error("Unexpected token < in JSON"); },
      });
      const node3 = createMockElement("span");
      await sidebar.testOnboardingConnection({ testStatus: node3 });
      assert.strictEqual(node3.textContent, "Test failed (502)");
      assert.strictEqual(node3.classList.contains("fail"), true);
    } finally {
      global.fetch = realFetch;
    }
  });

  await testAsync("testOnboardingConnection: handles fetch rejection gracefully", async () => {
    const realFetch = global.fetch;
    global.fetch = async () => {
      throw new Error("Network unreachable");
    };
    try {
      const statusNode = createMockElement("span");
      const w = { testStatus: statusNode };
      await sidebar.testOnboardingConnection(w);
      assert.ok(statusNode.textContent.startsWith("Test failed: "), "node text begins with 'Test failed: '");
      assert.strictEqual(statusNode.classList.contains("fail"), true, "node must have 'fail' class");
      assert.strictEqual(statusNode.classList.contains("ok"), false, "node must not have 'ok' class");
    } finally {
      global.fetch = realFetch;
    }
  });

  // =========================================================================
  // 10. Detached Sidebar & Existing Profile Onboarding Regression Guards
  // =========================================================================
  await testAsync("bootstrap: seedOnboardingStateFile skips when zocrates_config.json or sessions.db already exists", async () => {
    const dataDir = "/mock-zotero-profile/zocrates-data";
    const prevIO = global.IOUtils;
    try {
      arrangeDataDir();
      const stages = [];
      bootstrap.initOnboardingStageBus(env.zotero);
      const unsub = env.zotero.Zocrates.addOnboardingStageListener((s) => stages.push(s));

      // Case A: zocrates_config.json already exists
      const ioConfig = makeMockIOUtils();
      ioConfig._files.set(dataDir + "/zocrates_config.json", '{"backend_port": 8765}');
      global.IOUtils = ioConfig;
      const seededWithConfig = await bootstrap.seedOnboardingStateFile(dataDir, "/");
      assert.strictEqual(seededWithConfig, false, "must return false when zocrates_config.json exists");
      assert.strictEqual(ioConfig._files.has(stateFile), false, "must not create onboarding_state.json");
      assert.deepStrictEqual(stages, [], "must not broadcast configure when zocrates_config.json exists");

      // Case B: sessions.db already exists
      const ioSessions = makeMockIOUtils();
      ioSessions._files.set(dataDir + "/sessions.db", "SQLite format 3");
      global.IOUtils = ioSessions;
      const seededWithSessions = await bootstrap.seedOnboardingStateFile(dataDir, "/");
      assert.strictEqual(seededWithSessions, false, "must return false when sessions.db exists");
      assert.strictEqual(ioSessions._files.has(stateFile), false, "must not create onboarding_state.json");
      assert.deepStrictEqual(stages, [], "must not broadcast configure when sessions.db exists");

      unsub();
    } finally {
      global.IOUtils = prevIO;
    }
  });

  await testAsync("bootstrap: launchBackendIfNeeded skips seeding onboarding_state.json when profile already has config or sessions.db", async () => {
    const dataDir = "/mock-zotero-profile/zocrates-data";
    const prevIO = global.IOUtils;
    const prevFetch = global.fetch;
    const prevBackendReady = env.zotero.Zocrates._backendReady;
    const prevBackendProc = env.zotero.Zocrates._backendProcess;
    const prevSourceDir = env.zotero.Prefs?.get?.("extensions.zocrates.backendSourceDir", true);
    let backendSpawned = false;

    const io = makeMockIOUtils();
    io.makeDirectory = async (p) => { io._files.set(p, "<dir>"); };
    io.remove = async (p) => { io._files.delete(p); };
    io.getChildren = async () => [];
    io.setPermissions = async () => {};
    io._files.set(dataDir + "/sessions.db", "SQLite format 3");

    try {
      global.IOUtils = io;
      arrangeDataDir();
      bootstrap.initOnboardingStageBus(env.zotero);
      env.zotero.Zocrates._backendReady = false;
      env.zotero.Zocrates._backendLaunchInFlight = false;
      env.zotero.Zocrates.notifyBootstrapProgress = () => {};
      env.zotero.Zocrates.setActivePort = (p) => { env.zotero.Zocrates.activePort = p; };
      env.zotero.Prefs.set("extensions.zocrates.backendSourceDir", "/mock/zocrates-src", true);

      const emptyPipe = { async read() { return new Uint8Array(0); } };
      bootstrap.setSubprocessForTesting({
        async call(opts) {
          if (opts && Array.isArray(opts.arguments) && opts.arguments.includes("zocrates.fast_api_app")) {
            backendSpawned = true;
            return {
              pid: 4242,
              stdout: emptyPipe,
              stderr: emptyPipe,
              wait: () => new Promise(() => {}),
              kill: () => {},
            };
          }
          return {
            stdout: emptyPipe,
            stderr: emptyPipe,
            wait: async () => ({ exitCode: 0 }),
          };
        },
      });

      global.fetch = async (url) => {
        if (String(url).endsWith("/status")) {
          if (!backendSpawned) throw new Error("ECONNREFUSED");
          return {
            ok: true,
            status: 200,
            json: async () => ({ status: "ready", zotero_data_dir: "/mock-zotero-profile" }),
          };
        }
        return { ok: false, status: 404 };
      };

      await bootstrap.launchBackendIfNeeded();
      assert.strictEqual(backendSpawned, true, "backend should be provisioned and spawned");
      assert.strictEqual(io._files.has(stateFile), false, "existing profile provisioning must not seed onboarding_state.json");
    } finally {
      bootstrap.setSubprocessForTesting(null);
      global.IOUtils = prevIO;
      global.fetch = prevFetch;
      env.zotero.Zocrates._backendReady = prevBackendReady;
      env.zotero.Zocrates._backendProcess = prevBackendProc;
      if (prevSourceDir !== undefined) {
        env.zotero.Prefs.set("extensions.zocrates.backendSourceDir", prevSourceDir, true);
      } else {
        env.zotero.Prefs.set("extensions.zocrates.backendSourceDir", "", true);
      }
    }
  });

  await testAsync("sidebar: initOnboardingStateFlow auto-graduates configure to ready when existing sessions exist", async () => {
    const prevIO = global.IOUtils;
    const prevSessions = sidebar.getSessions();
    const prevSid = sidebar.getCurrentSessionId();
    const prevActiveSid = env.zotero.Zocrates._activeSessionId;
    const prevSavedSid = env.zotero.Prefs.get("extensions.zocrates.last_active_session_id", true);

    try {
      arrangeDataDir();

      // 1. Existing sessions with custom conversation title auto-graduates configure -> ready and persists
      const io1 = makeMockIOUtils();
      io1._files.set(stateFile, JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-01T00:00:00Z" }));
      global.IOUtils = io1;
      sidebar.setSessions([{ id: "session-real-abc123", title: "Literature Review", messages: [] }]);
      sidebar.setCurrentSessionId("");
      env.zotero.Zocrates._activeSessionId = "";
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", "", true);

      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "must auto-graduate stage to ready");
      const persisted1 = await sidebar.readOnboardingStateFile();
      assert.strictEqual(persisted1.stage, "ready", "must persist ready stage to onboarding_state.json");

      // 2. An empty auto-created initial session ("New Chat", no messages) does NOT falsely graduate a fresh install
      const io2 = makeMockIOUtils();
      io2._files.set(stateFile, JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-01T00:00:00Z" }));
      global.IOUtils = io2;
      sidebar.setSessions([{ id: "session-new-fresh-001", title: "New Chat", messages: [] }]);
      sidebar.setCurrentSessionId("session-new-fresh-001");
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", "session-new-fresh-001", true);

      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure", "empty auto-created New Chat session must keep configure stage");

      // 3. A session with real messages auto-graduates configure -> ready
      const io3 = makeMockIOUtils();
      io3._files.set(stateFile, JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-01T00:00:00Z" }));
      global.IOUtils = io3;
      sidebar.setSessions([{ id: "session-active-002", title: "New Chat", messages: [{ role: "user", content: "hello" }] }]);
      sidebar.setCurrentSessionId("session-active-002");
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", "session-active-002", true);

      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "session with messages must auto-graduate stage to ready");
      const persisted3 = await sidebar.readOnboardingStateFile();
      assert.strictEqual(persisted3.stage, "ready", "must persist ready stage when session has messages");

      // 4. Synthetic offline placeholder IDs (session_*) must NOT falsely graduate a genuine fresh install
      const io4 = makeMockIOUtils();
      io4._files.set(stateFile, JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-01T00:00:00Z" }));
      global.IOUtils = io4;
      sidebar.setSessions([{ id: "session_offline_tmp", title: "New Chat", messages: [] }]);
      sidebar.setCurrentSessionId("session_offline_tmp");
      env.zotero.Zocrates._activeSessionId = "session_offline_tmp";
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", "", true);

      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure", "synthetic session_* ID must keep configure stage");
    } finally {
      global.IOUtils = prevIO;
      sidebar.setSessions(prevSessions);
      sidebar.setCurrentSessionId(prevSid);
      env.zotero.Zocrates._activeSessionId = prevActiveSid;
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", prevSavedSid || "", true);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  await testAsync("sidebar: detached windows with an active session suppress onboarding wizard and clean #onboarding-container", async () => {
    const prevIsDetached = sidebar.getIsDetached();
    const prevSid = sidebar.getCurrentSessionId();
    const prevActiveSid = env.zotero.Zocrates._activeSessionId;
    const prevDetachedInit = env.zotero.Zocrates._detachedInitialState;
    const prevSavedSid = env.zotero.Prefs.get("extensions.zocrates.last_active_session_id", true);
    const onboardingHost = env.document.getElementById("onboarding-container");

    try {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }

      // 1. enterOnboardingConfigure and enterOnboardingProcess return early when isDetached && valid session exists
      sidebar.setIsDetached(true);
      sidebar.setCurrentSessionId("session-detached-active-001");
      sidebar.setOnboardingStage("configure");
      sidebar.enterOnboardingConfigure();

      assert.strictEqual(
        env.document.getElementById("onboarding-wizard-card"),
        null,
        "detached window with active session must not render #onboarding-wizard-card"
      );
      assert.strictEqual(onboardingHost.children.length, 0, "#onboarding-container must remain empty");

      sidebar.setOnboardingStage("process");
      sidebar.enterOnboardingProcess();
      assert.strictEqual(
        env.document.getElementById("onboarding-process-card"),
        null,
        "detached window with active session must not render #onboarding-process-card"
      );
      assert.strictEqual(onboardingHost.children.length, 0, "#onboarding-container must remain empty in process stage");

      // 2. Detached state restoration cleans up any pre-existing onboarding cards in #onboarding-container
      const staleCard = createMockElement("div");
      staleCard.id = "onboarding-wizard-card";
      onboardingHost.appendChild(staleCard);
      assert.strictEqual(onboardingHost.children.length, 1);

      sidebar.setCurrentSessionId("");
      env.zotero.Zocrates._detachedInitialState = {
        currentSessionId: "session-detached-restored-002",
      };
      sidebar.restoreDetachedInitialState();

      assert.strictEqual(sidebar.getCurrentSessionId(), "session-detached-restored-002");
      assert.strictEqual(onboardingHost.children.length, 0, "restoreDetachedInitialState must clear #onboarding-container");
      assert.strictEqual(env.document.getElementById("onboarding-wizard-card"), null);
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      sidebar.setIsDetached(prevIsDetached);
      sidebar.setCurrentSessionId(prevSid);
      env.zotero.Zocrates._activeSessionId = prevActiveSid;
      if (prevDetachedInit === undefined) {
        delete env.zotero.Zocrates._detachedInitialState;
      } else {
        env.zotero.Zocrates._detachedInitialState = prevDetachedInit;
      }
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", prevSavedSid || "", true);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  await testAsync("sidebar: initOnboardingStateFlow auto-graduates when pre-existing zocrates_config.json exists with empty sessions", async () => {
    const prevIO = global.IOUtils;
    const prevSessions = sidebar.getSessions();
    const prevSid = sidebar.getCurrentSessionId();
    const prevActiveSid = env.zotero.Zocrates._activeSessionId;
    const prevSavedSid = env.zotero.Prefs.get("extensions.zocrates.last_active_session_id", true);
    const onboardingHost = env.document.getElementById("onboarding-container");
    const configPath = "/mock-zotero-profile/zocrates-data/zocrates_config.json";
    const sessionsDbPath = "/mock-zotero-profile/zocrates-data/sessions.db";

    try {
      arrangeDataDir();
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }

      // 1. Pre-existing zocrates_config.json (mtime predates seeded_at) auto-graduates even while sessions === []
      const ioExisting = makeMockIOUtils();
      ioExisting._files.set(
        stateFile,
        JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-10T00:00:00Z" })
      );
      ioExisting._files.set(configPath, '{"backend_port": 8765}');
      ioExisting._stats.set(configPath, { lastModified: Date.parse("2026-01-01T00:00:00Z") });
      global.IOUtils = ioExisting;

      sidebar.setSessions([]); // Simulates async /sessions fetch still in flight
      sidebar.setCurrentSessionId("");
      env.zotero.Zocrates._activeSessionId = "";
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", "", true);

      const staleWizardCard = createMockElement("div");
      staleWizardCard.id = "onboarding-wizard-card";
      onboardingHost.appendChild(staleWizardCard);

      assert.strictEqual(await sidebar.profileHasExistingArtifacts(), true, "pre-existing config must be detected");
      await sidebar.initOnboardingStateFlow();

      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "must auto-graduate configure -> ready");
      const persisted = await sidebar.readOnboardingStateFile();
      assert.strictEqual(persisted.stage, "ready", "must persist ready stage to onboarding_state.json");
      assert.strictEqual(onboardingHost.children.length, 0, "must immediately clear #onboarding-container cards");
      assert.strictEqual(env.document.getElementById("onboarding-wizard-card"), null, "wizard card must be removed");

      // 2. Fresh-install backend boot files (mtime after seeded_at) must NOT falsely auto-graduate configure
      const ioFreshBoot = makeMockIOUtils();
      ioFreshBoot._files.set(
        stateFile,
        JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-10T00:00:00Z" })
      );
      ioFreshBoot._files.set(configPath, '{"document_converter_id": "markitdown"}');
      ioFreshBoot._stats.set(configPath, { lastModified: Date.parse("2026-01-10T00:00:05Z") });
      ioFreshBoot._files.set(sessionsDbPath, "SQLite format 3");
      ioFreshBoot._stats.set(sessionsDbPath, { lastModified: Date.parse("2026-01-10T00:00:05Z") });
      global.IOUtils = ioFreshBoot;

      sidebar.setSessions([]);
      assert.strictEqual(await sidebar.profileHasExistingArtifacts(), false, "post-seed boot files must not count as pre-existing");
      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure", "fresh install warm-start resume must stay in configure");
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      global.IOUtils = prevIO;
      sidebar.setSessions(prevSessions);
      sidebar.setCurrentSessionId(prevSid);
      env.zotero.Zocrates._activeSessionId = prevActiveSid;
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", prevSavedSid || "", true);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  await testAsync("sidebar: loadSessions auto-graduates configure to ready when sessions resolve with conversation history", async () => {
    const prevIO = global.IOUtils;
    const prevFetch = global.fetch;
    const prevSessions = sidebar.getSessions();
    const prevSid = sidebar.getCurrentSessionId();
    const prevReady = sidebar.getBackendReadyForChat();
    const prevActiveSid = env.zotero.Zocrates._activeSessionId;
    const prevSavedSid = env.zotero.Prefs.get("extensions.zocrates.last_active_session_id", true);
    const onboardingHost = env.document.getElementById("onboarding-container");

    try {
      arrangeDataDir();

      // 1. Named conversation title in /sessions resolves after mount-time configure check
      const io1 = makeMockIOUtils();
      io1._files.set(
        stateFile,
        JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-10T00:00:00Z" })
      );
      global.IOUtils = io1;
      sidebar.setSessions([]);
      sidebar.setCurrentSessionId("");
      sidebar.setBackendReadyForChat(false);
      sidebar.setOnboardingStatePending(false);
      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");

      const wizCard1 = createMockElement("div");
      wizCard1.id = "onboarding-wizard-card";
      onboardingHost.appendChild(wizCard1);

      global.fetch = async (url) => {
        const u = String(url);
        if (u.endsWith("/sessions")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              sessions: [{ id: "sess-history-001", title: "Mechanistic Interpretability", created_at: "2026-01-02T00:00:00Z" }],
            }),
          };
        }
        if (u.endsWith("/sessions/sess-history-001")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              session: {
                id: "sess-history-001",
                title: "Mechanistic Interpretability",
                messages: [{ role: "user", text: "Compare sparse autoencoders" }],
              },
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      };

      await sidebar.loadSessions();

      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "loadSessions must auto-graduate configure -> ready");
      const persisted1 = await sidebar.readOnboardingStateFile();
      assert.strictEqual(persisted1.stage, "ready", "loadSessions must persist ready stage to onboarding_state.json");
      assert.strictEqual(onboardingHost.children.length, 0, "loadSessions must clear #onboarding-container");
      assert.strictEqual(env.document.getElementById("onboarding-wizard-card"), null, "wizard card must be removed");
      assert.strictEqual(sidebar.getBackendReadyForChat(), true, "removeBootstrapProgressCard must lift the chat gate");

      // 2. Single "New Chat" session whose messages populate during loadActiveSessionMessages also auto-graduates
      const chatMessagesEl = env.document.getElementById("chat-messages");
      if (chatMessagesEl) {
        while (chatMessagesEl.firstChild) chatMessagesEl.removeChild(chatMessagesEl.firstChild);
      }
      const io2 = makeMockIOUtils();
      io2._files.set(
        stateFile,
        JSON.stringify({ version: 1, stage: "configure", seeded_at: "2026-01-10T00:00:00Z" })
      );
      global.IOUtils = io2;
      sidebar.setSessions([]);
      sidebar.setCurrentSessionId("");
      sidebar.setBackendReadyForChat(false);
      await sidebar.initOnboardingStateFlow();
      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "configure");

      const wizCard2 = createMockElement("div");
      wizCard2.id = "onboarding-wizard-card";
      onboardingHost.appendChild(wizCard2);

      global.fetch = async (url) => {
        const u = String(url);
        if (u.endsWith("/sessions")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              sessions: [{ id: "sess-msg-only-002", title: "New Chat", created_at: "2026-01-02T00:00:00Z" }],
            }),
          };
        }
        if (u.endsWith("/sessions/sess-msg-only-002")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              session: {
                id: "sess-msg-only-002",
                title: "New Chat",
                messages: [{ role: "user", text: "Hello Zocrates" }],
              },
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      };

      await sidebar.loadSessions();

      assert.strictEqual(sidebar.getEffectiveOnboardingStage(), "ready", "message-backed New Chat session must auto-graduate");
      const persisted2 = await sidebar.readOnboardingStateFile();
      assert.strictEqual(persisted2.stage, "ready");
      assert.strictEqual(onboardingHost.children.length, 0);
      assert.strictEqual(sidebar.getBackendReadyForChat(), true);
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      global.IOUtils = prevIO;
      global.fetch = prevFetch;
      sidebar.setSessions(prevSessions);
      sidebar.setCurrentSessionId(prevSid);
      sidebar.setBackendReadyForChat(prevReady);
      env.zotero.Zocrates._activeSessionId = prevActiveSid;
      env.zotero.Prefs.set("extensions.zocrates.last_active_session_id", prevSavedSid || "", true);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  test("sidebar: enterOnboardingConfigure idempotency: calling it multiple times only renders 1 wizard card in #onboarding-container", () => {
    const onboardingHost = env.document.getElementById("onboarding-container");
    const prevIsDetached = sidebar.getIsDetached();
    const prevSid = sidebar.getCurrentSessionId();

    try {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      sidebar.setIsDetached(false);
      sidebar.setCurrentSessionId("");
      sidebar.setOnboardingStage("configure");

      // First call renders the card
      sidebar.enterOnboardingConfigure();
      assert.strictEqual(onboardingHost.children.length, 1);
      const firstCard = env.document.getElementById("onboarding-wizard-card");
      assert.ok(firstCard, "#onboarding-wizard-card must be present");

      // Repeated calls must be idempotent and return early without appending extra cards
      sidebar.enterOnboardingConfigure();
      sidebar.enterOnboardingConfigure();

      assert.strictEqual(onboardingHost.children.length, 1, "only 1 wizard card in #onboarding-container");
      assert.strictEqual(
        env.document.getElementById("onboarding-wizard-card"),
        firstCard,
        "existing wizard card instance must be preserved by early return"
      );
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      sidebar.setIsDetached(prevIsDetached);
      sidebar.setCurrentSessionId(prevSid);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  test("sidebar: Concurrent/replayed bootstrap done event and stage bus snapshot: only 1 wizard card is rendered and clicking preset cards modifies that single card", () => {
    const onboardingHost = env.document.getElementById("onboarding-container");
    const prevIsDetached = sidebar.getIsDetached();
    const prevSid = sidebar.getCurrentSessionId();

    try {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      arrangeDataDir();
      bootstrap.initOnboardingStageBus(env.zotero);

      sidebar.setIsDetached(false);
      sidebar.setCurrentSessionId("");
      sidebar.setOnboardingStatePending(false);
      env.zotero.Zocrates._backendReady = true;

      // Seed stage bus snapshot as "configure"
      env.zotero.Zocrates._onboardingStage = "configure";
      sidebar.setOnboardingStage(null);

      // Subscribe to stage bus (replays "configure" snapshot)
      sidebar.subscribeOnboardingStageBus();

      // Dispatch concurrent / replayed bootstrap done event
      sidebar.handleBootstrapProgressEvent({ state: "done" });

      // Only 1 wizard card rendered in #onboarding-container
      assert.strictEqual(onboardingHost.children.length, 1, "only 1 wizard card must exist in #onboarding-container");
      const wizardCard = env.document.getElementById("onboarding-wizard-card");
      assert.ok(wizardCard, "#onboarding-wizard-card must exist");

      // Verify clicking preset cards modifies that single card
      const w = sidebar.getOnboardingWizardRefs();
      assert.ok(w, "wizard refs must be defined");
      assert.strictEqual(w.activePresetKey, "recommended", "recommended preset pre-selected by default");

      const offlineBtn = w.presetCards.find((btn) => btn.dataset.presetKey === "offline");
      assert.ok(offlineBtn, "offline preset button must exist");

      offlineBtn.dispatchEvent("click");

      assert.strictEqual(w.activePresetKey, "offline");
      assert.strictEqual(w.providerSelect.value, "ollama");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
      assert.strictEqual(w.baseUrlRow.style.display, "flex");
      assert.strictEqual(offlineBtn.classList.contains("selected"), true, "offline preset button must be selected");

      const recBtn = w.presetCards.find((btn) => btn.dataset.presetKey === "recommended");
      assert.ok(recBtn, "recommended preset button must exist");
      assert.strictEqual(recBtn.classList.contains("selected"), false, "recommended preset button must no longer be selected");

      recBtn.dispatchEvent("click");
      assert.strictEqual(w.activePresetKey, "recommended");
      assert.strictEqual(w.modelInput.placeholder, "openrouter/~z-ai/glm-flash-latest");
      assert.strictEqual(w.baseUrlRow.style.display, "none");

      w.providerSelect.value = "vllm";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
      assert.strictEqual(w.baseUrlInput.placeholder, "http://localhost:8000/v1");

      // Still exactly 1 card in the DOM
      assert.strictEqual(onboardingHost.children.length, 1, "#onboarding-container must still contain only 1 card after preset click");
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      if (env.zotero.Zocrates && env.zotero.Zocrates._onboardingStageSubscribers) {
        env.zotero.Zocrates._onboardingStageSubscribers.clear();
      }
      sidebar.setIsDetached(prevIsDetached);
      sidebar.setCurrentSessionId(prevSid);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  await testAsync("sidebar: startup race between initOnboardingStateFlow and subscribeOnboardingStageBus resolves to exactly 1 wizard card", async () => {
    const onboardingHost = env.document.getElementById("onboarding-container");
    const chatMessagesEl = env.document.getElementById("chat-messages");
    const prevIsDetached = sidebar.getIsDetached();
    const prevSid = sidebar.getCurrentSessionId();
    const prevSessions = sidebar.getSessions();
    const prevIO = global.IOUtils;

    try {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      if (chatMessagesEl) {
        while (chatMessagesEl.firstChild) chatMessagesEl.removeChild(chatMessagesEl.firstChild);
      }
      arrangeDataDir();
      bootstrap.initOnboardingStageBus(env.zotero);

      const io = makeMockIOUtils();
      io._files.set(
        stateFile,
        JSON.stringify({ version: 1, stage: "configure", seeded_at: new Date().toISOString() })
      );
      global.IOUtils = io;

      sidebar.setIsDetached(false);
      sidebar.setCurrentSessionId("");
      sidebar.setSessions([]);
      sidebar.setOnboardingStage(null);
      env.zotero.Zocrates._backendReady = true;

      // Seed stage bus snapshot as "configure" (from seedOnboardingStateFile)
      env.zotero.Zocrates._onboardingStage = "configure";

      // Replay bootstrap done event while pending (mount-time race condition)
      sidebar.setOnboardingStatePending(true);
      sidebar.handleBootstrapProgressEvent({ state: "done" });

      // Run the startup sequence (chained flow -> bus subscribe)
      await sidebar.initOnboardingStateFlow().then(() => {
        sidebar.subscribeOnboardingStageBus();
      });

      assert.strictEqual(onboardingHost.children.length, 1, "exactly 1 wizard card rendered after full startup sequence");
      const wizardCard = env.document.getElementById("onboarding-wizard-card");
      assert.ok(wizardCard, "#onboarding-wizard-card must exist");
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      if (chatMessagesEl) {
        while (chatMessagesEl.firstChild) chatMessagesEl.removeChild(chatMessagesEl.firstChild);
      }
      if (env.zotero.Zocrates && env.zotero.Zocrates._onboardingStageSubscribers) {
        env.zotero.Zocrates._onboardingStageSubscribers.clear();
      }
      global.IOUtils = prevIO;
      sidebar.setSessions(prevSessions);
      sidebar.setIsDetached(prevIsDetached);
      sidebar.setCurrentSessionId(prevSid);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  test("sidebar: onboarding wizard AI engine UI synchronization (presets, placeholders, base URL)", () => {
    const onboardingHost = env.document.getElementById("onboarding-container");
    const prevIsDetached = sidebar.getIsDetached();
    const prevSid = sidebar.getCurrentSessionId();

    try {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      arrangeDataDir();
      bootstrap.initOnboardingStageBus(env.zotero);

      sidebar.setIsDetached(false);
      sidebar.setCurrentSessionId("");
      sidebar.setOnboardingStatePending(false);
      env.zotero.Zocrates._backendReady = true;

      sidebar.setOnboardingStage("configure");
      sidebar.enterOnboardingConfigure();

      const w = sidebar.getOnboardingWizardRefs();
      assert.ok(w, "wizard refs must be defined");
      assert.ok(w.baseUrlHelper, "baseUrlHelper must be stored on wizard refs");

      // Base URL label must include (optional)
      const baseUrlLabel = w.baseUrlRow.querySelector("span.onboarding-field-label");
      assert.ok(baseUrlLabel, "Base URL field label must exist");
      assert.strictEqual(baseUrlLabel.textContent, "Base URL (optional)");

      // 1. Recommended preset is pre-selected by default
      assert.strictEqual(w.activePresetKey, "recommended");
      assert.strictEqual(w.providerSelect.value, "openrouter_llm");
      assert.strictEqual(w.modelInput.placeholder, "openrouter/~z-ai/glm-flash-latest");
      assert.strictEqual(w.baseUrlRow.style.display, "none");
      assert.strictEqual(w.modelInput.value, "openrouter/~z-ai/glm-flash-latest");

      // 2. Select offline preset
      sidebar.applyOnboardingPreset("offline");
      assert.strictEqual(w.activePresetKey, "offline");
      assert.strictEqual(w.providerSelect.value, "ollama");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
      assert.strictEqual(w.baseUrlRow.style.display, "flex");
      assert.strictEqual(w.baseUrlInput.placeholder, "http://localhost:11434");
      assert.strictEqual(
        w.baseUrlHelper.textContent,
        "Optional — leave blank for default (http://localhost:11434). Only change if running on a custom port or remote host."
      );
      assert.strictEqual(w.modelInput.value, "");

      // 3. Switch back to recommended preset
      sidebar.applyOnboardingPreset("recommended");
      assert.strictEqual(w.activePresetKey, "recommended");
      assert.strictEqual(w.providerSelect.value, "openrouter_llm");
      assert.strictEqual(w.modelInput.placeholder, "openrouter/~z-ai/glm-flash-latest");
      assert.strictEqual(w.baseUrlRow.style.display, "none");
      assert.strictEqual(w.modelInput.value, "openrouter/~z-ai/glm-flash-latest");

      // 4. Change providerSelect to "vllm" (switching from openrouter_llm to local clears default slug)
      w.providerSelect.value = "vllm";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
      assert.strictEqual(w.baseUrlInput.placeholder, "http://localhost:8000/v1");
      assert.strictEqual(w.baseUrlRow.style.display, "flex");
      assert.strictEqual(
        w.baseUrlHelper.textContent,
        "Optional — leave blank for default (http://localhost:8000/v1). Only change if running on a custom port or remote host."
      );
      assert.strictEqual(w.modelInput.value, "", "openrouter slug cleared so local ghost text shows through");

      // 5. Change providerSelect to "openai_compatible"
      w.providerSelect.value = "openai_compatible";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
      assert.strictEqual(w.baseUrlInput.placeholder, "http://localhost:8000/v1");
      assert.strictEqual(w.baseUrlRow.style.display, "flex");
      assert.strictEqual(
        w.baseUrlHelper.textContent,
        "Optional — leave blank for default (http://localhost:8000/v1), or specify your server endpoint (e.g. http://localhost:1234/v1 for LM Studio)."
      );

      // 6. Switching back to openrouter_llm when modelInput is empty restores recommended model
      w.providerSelect.value = "openrouter_llm";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.placeholder, "openrouter/~z-ai/glm-flash-latest");
      assert.strictEqual(w.baseUrlRow.style.display, "none");
      assert.strictEqual(w.modelInput.value, "openrouter/~z-ai/glm-flash-latest");

      // 7. Switching to local, typing "gemma4:12b", then switching back restores recommended slug
      w.providerSelect.value = "ollama";
      w.providerSelect.dispatchEvent("change");
      w.modelInput.value = "gemma4:12b";
      w.providerSelect.value = "openrouter_llm";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.value, "openrouter/~z-ai/glm-flash-latest");

      // 8. Free preset updates placeholder to nvidia free model and hides base URL row
      sidebar.applyOnboardingPreset("free");
      assert.strictEqual(w.activePresetKey, "free");
      assert.strictEqual(w.modelInput.placeholder, "nvidia/nemotron-3.5-lightning:free");
      assert.strictEqual(w.baseUrlRow.style.display, "none");
      assert.strictEqual(w.modelInput.value, "nvidia/nemotron-3.5-lightning:free");

      // 9. From free preset, switching to local clears nvidia default slug
      w.providerSelect.value = "ollama";
      w.providerSelect.dispatchEvent("change");
      assert.strictEqual(w.modelInput.value, "", "nvidia slug cleared so local ghost text shows through");
      assert.strictEqual(w.modelInput.placeholder, "gemma4:12b");
    } finally {
      while (onboardingHost.firstChild) {
        onboardingHost.removeChild(onboardingHost.firstChild);
      }
      if (env.zotero.Zocrates && env.zotero.Zocrates._onboardingStageSubscribers) {
        env.zotero.Zocrates._onboardingStageSubscribers.clear();
      }
      sidebar.setIsDetached(prevIsDetached);
      sidebar.setCurrentSessionId(prevSid);
      sidebar.setOnboardingStage("ready");
      sidebar.cleanupOnboardingResources();
    }
  });

  await testAsync("sidebar: Microsoft Visual C++ missing error renders prerequisite card with docUrl link, architecture, Check & Continue button, and no .exe link", async () => {
    const existing = global.document.getElementById("bootstrap-progress-card");
    if (existing) existing.remove();

    const docUrl = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170#latest-supported-redistributable-version";
    sidebar.handleBootstrapProgressEvent({
      state: "error",
      stepKey: "error",
      detail: {
        state: "error",
        code: "VCRUNTIME_MISSING",
        arch: "X64",
        docUrl: docUrl,
        message: "Microsoft Visual C++ 2015–2022 Redistributable (X64) is required on Windows to run the local Python engine. Please visit " + docUrl + " and download and install the X64 version.",
      },
    });

    const card = global.document.getElementById("bootstrap-progress-card");
    assert.ok(card !== null, "Bootstrap card must exist");

    const errBox = card.querySelector("#bootstrap-card-error");
    assert.ok(errBox !== null, "Error box must exist");
    assert.ok(!errBox.classList.contains("hidden"), "Error box must be visible");

    const msg = card.querySelector("#bootstrap-error-message");
    assert.ok(msg !== null, "Error message container must exist");

    // Title
    const title = msg.querySelector(".vc-redist-title");
    assert.ok(title !== null, "VC redist title element must exist");
    assert.strictEqual(title.textContent, "Microsoft Visual C++ Runtime Required");

    // Architecture instruction specifying detected architecture
    const archInst = msg.querySelector(".vc-redist-arch-inst");
    assert.ok(archInst !== null, "Architecture instruction must exist");
    assert.ok(archInst.textContent.includes("X64"), "Must mention X64 architecture");
    assert.ok(archInst.textContent.includes("Detected architecture: X64"), "Must specify detected architecture");

    // Primary link to documentation page
    const link = msg.querySelector("a.onboarding-link");
    assert.ok(link !== null, "Primary link must exist with class onboarding-link");
    assert.strictEqual(link.href, docUrl);
    assert.strictEqual(link.getAttribute("target"), "_blank");
    assert.ok(link.textContent.includes("Microsoft Visual C++ Redistributable Documentation & Downloads"));

    // Check & Continue button
    const retryBtn = errBox.querySelector(".bootstrap-retry-btn");
    assert.ok(retryBtn !== null, "Retry button must exist");
    assert.strictEqual(retryBtn.textContent, "Check & Continue", "Button text must be updated to Check & Continue");

    // Next step instruction
    const nextStep = msg.querySelector(".vc-redist-next-step");
    assert.ok(nextStep !== null, "Next step instruction must exist");
    assert.ok(nextStep.textContent.includes("Check & Continue"));

    // Omission of direct .exe link
    const allLinks = Array.from(card.querySelectorAll("a"));
    for (const a of allLinks) {
      assert.ok(!a.href.toLowerCase().endsWith(".exe"), "Must not link directly to .exe file: " + a.href);
      assert.ok(!a.href.includes("vc_redist"), "Must not link to vc_redist installer directly: " + a.href);
    }
    assert.ok(!card.textContent.includes(".exe"), "Card text must not contain .exe file name");

    // Verify background polling triggers retryBackendLaunch when installed
    let retryCalled = false;
    env.zotero.Zocrates = env.zotero.Zocrates || {};
    env.zotero.Zocrates.isVcRedistInstalledWindows = async () => true;
    env.zotero.Zocrates.retryBackendLaunch = () => {
      retryCalled = true;
    };

    // Clean up timer and card
    if (sidebar.clearVcRedistCheckTimer) {
      sidebar.clearVcRedistCheckTimer();
    }
    sidebar.handleBootstrapProgressEvent({
      state: "active",
      stepKey: "preparing",
      detail: { state: "active" },
    });
    assert.strictEqual(retryBtn.textContent, "Retry", "Non-error event resets retry button to Retry");

    if (card) card.remove();
  });

  await testAsync("sidebar: uv missing error renders prerequisite card with code block, copy button, docUrl link, Check & Continue button, and polls isUvAvailable", async () => {
    const existing = global.document.getElementById("bootstrap-progress-card");
    if (existing) existing.remove();

    const docUrl = "https://docs.astral.sh/uv/getting-started/installation/";
    const installCmd = "curl -LsSf https://astral.sh/uv/install.sh | sh";
    sidebar.handleBootstrapProgressEvent({
      state: "error",
      stepKey: "error",
      detail: {
        state: "error",
        code: "UV_MISSING",
        uvVersion: "0.13.8",
        triple: "x86_64-unknown-linux-gnu",
        docUrl: docUrl,
        installCmd: installCmd,
        message: "Failed to automatically obtain uv package manager (v0.13.8 for x86_64-unknown-linux-gnu). Please install uv manually: " + installCmd,
      },
    });

    const card = global.document.getElementById("bootstrap-progress-card");
    assert.ok(card !== null, "Bootstrap card must exist");

    const errBox = card.querySelector("#bootstrap-card-error");
    assert.ok(errBox !== null, "Error box must exist");
    assert.ok(!errBox.classList.contains("hidden"), "Error box must be visible");

    const msg = card.querySelector("#bootstrap-error-message");
    assert.ok(msg !== null, "Error message container must exist");

    // Title
    const title = msg.querySelector(".uv-missing-title");
    assert.ok(title !== null, "UV missing title element must exist");
    assert.strictEqual(title.textContent, "uv Package Manager Required");

    // Description mentions version and triple
    const desc = msg.querySelector(".uv-missing-desc");
    assert.ok(desc !== null, "Description must exist");
    assert.ok(desc.textContent.includes("0.13.8"), "Description must mention pinned version 0.13.8");
    assert.ok(desc.textContent.includes("x86_64-unknown-linux-gnu"), "Description must mention triple");

    // Code block with copy button
    const codeEl = msg.querySelector(".uv-install-code");
    assert.ok(codeEl !== null, "Code element must exist");
    assert.strictEqual(codeEl.textContent, installCmd);

    const copyBtn = msg.querySelector(".uv-install-copy-btn");
    assert.ok(copyBtn !== null, "Copy button must exist");

    // Documentation link
    const link = msg.querySelector("a.onboarding-link");
    assert.ok(link !== null, "Documentation link must exist with class onboarding-link");
    assert.strictEqual(link.href, docUrl);
    assert.strictEqual(link.getAttribute("target"), "_blank");
    assert.ok(link.textContent.includes("Astral uv Installation Documentation"));

    // Check & Continue button
    const retryBtn = errBox.querySelector(".bootstrap-retry-btn");
    assert.ok(retryBtn !== null, "Retry button must exist");
    assert.strictEqual(retryBtn.textContent, "Check & Continue", "Button text must be updated to Check & Continue");

    // Next step instruction
    const nextStep = msg.querySelector(".uv-missing-next-step");
    assert.ok(nextStep !== null, "Next step instruction must exist");
    assert.ok(nextStep.textContent.includes("Check & Continue"));

    // Verify background polling timer is set
    assert.ok(sidebar.getUvCheckTimer() !== null, "uvCheckTimer must be active");

    // Clean up timer and card
    if (sidebar.clearUvCheckTimer) {
      sidebar.clearUvCheckTimer();
    }
    assert.strictEqual(sidebar.getUvCheckTimer(), null, "uvCheckTimer must be cleared");

    sidebar.handleBootstrapProgressEvent({
      state: "active",
      stepKey: "preparing",
      detail: { state: "active" },
    });
    assert.strictEqual(retryBtn.textContent, "Retry", "Non-error event resets retry button to Retry");

    if (card) card.remove();
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;
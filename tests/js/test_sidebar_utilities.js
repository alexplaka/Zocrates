/**
 * test_sidebar_utilities.js — Tests for sidebar utility formatting and helper functions.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar Utilities ---");
  setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. formatCostUsd
  test("formatCostUsd: formats zero, micro-costs (<$0.01), and standard dollar amounts", () => {
    // Zero & invalid costs
    assert.strictEqual(sidebar.formatCostUsd(0), "$0.00");
    assert.strictEqual(sidebar.formatCostUsd(0.0), "$0.00");
    assert.strictEqual(sidebar.formatCostUsd(-0.5), "$0.00");
    assert.strictEqual(sidebar.formatCostUsd(null), "$0.00");
    assert.strictEqual(sidebar.formatCostUsd(undefined), "$0.00");
    assert.strictEqual(sidebar.formatCostUsd(NaN), "$0.00");

    // Micro-costs (< 0.005)
    assert.strictEqual(sidebar.formatCostUsd(0.000125), "<$0.01");
    assert.strictEqual(sidebar.formatCostUsd(0.0049), "<$0.01");

    // Standard costs (>= 0.005)
    assert.strictEqual(sidebar.formatCostUsd(0.005), "$0.01");
    assert.strictEqual(sidebar.formatCostUsd(0.024), "$0.02");
    assert.strictEqual(sidebar.formatCostUsd(0.026), "$0.03");
    assert.strictEqual(sidebar.formatCostUsd(1.5), "$1.50");
  });

  // 2. extractMessageTitle
  test("extractMessageTitle: prioritizes markdown headings, bold prefixes, and text lines", () => {
    // Heading 1
    const textH1 = "# Speculative Decoding Survey\n\nDetailed breakdown of draft models...";
    assert.strictEqual(sidebar.extractMessageTitle(textH1), "Speculative Decoding Survey");

    // Heading 2
    const textH2 = "## Performance Benchmarks ##\n\nLatency measurements...";
    assert.strictEqual(sidebar.extractMessageTitle(textH2), "Performance Benchmarks");

    // Bold title prefix
    const textBold = "**Key Empirical Findings**: Memory bandwidth was the primary bottleneck.";
    assert.strictEqual(sidebar.extractMessageTitle(textBold), "Key Empirical Findings");

    // Plain line fallback (<= 60 chars)
    const textPlain = "This study compares five distinct attention mechanisms.";
    assert.strictEqual(
      sidebar.extractMessageTitle(textPlain),
      "This study compares five distinct attention mechanisms."
    );

    // Long line truncation (> 60 chars -> truncated to 57 chars + ...)
    const textLong = "This study compares five distinct attention mechanisms across contexts and hardware.";
    assert.strictEqual(
      sidebar.extractMessageTitle(textLong),
      "This study compares five distinct attention mechanisms ac..."
    );

    // Empty fallback
    assert.strictEqual(sidebar.extractMessageTitle("", "user"), "User Query");
    assert.strictEqual(sidebar.extractMessageTitle(null, "assistant"), "Response");
  });

  // 3. formatMarkdownForNote
  test("formatMarkdownForNote: converts markdown to clean XHTML for Zotero notes", () => {
    const md = "# Title\n\nThis is **bold** and *italic*.\n\n- Point 1\n- Point 2";
    const xhtml = sidebar.formatMarkdownForNote(md);

    assert.ok(xhtml.includes("<h1>Title</h1>"), "Should convert markdown header");
    assert.ok(xhtml.includes("<strong>bold</strong>"), "Should convert bold text");
    assert.ok(xhtml.includes("<em>italic</em>"), "Should convert italic text");
    assert.ok(xhtml.includes("<li>Point 1</li>"), "Should convert list items");
  });

  // 4. formatModelLabel
  test("formatModelLabel: prefixes provider labels and cleans model identifiers", () => {
    assert.strictEqual(
      sidebar.formatModelLabel("openrouter/anthropic/claude-3.5-sonnet"),
      "OpenRouter: anthropic/claude-3.5-sonnet"
    );
    assert.strictEqual(
      sidebar.formatModelLabel("ollama/llama3:8b"),
      "Ollama: llama3:8b"
    );
    assert.strictEqual(
      sidebar.formatModelLabel("ollama_chat/lfm2.5-thinking:1.2b"),
      "Ollama: lfm2.5-thinking:1.2b"
    );
    assert.strictEqual(
      sidebar.formatModelLabel("mistralai/Mistral-7B-Instruct-v0.3", "vllm"),
      "vLLM: mistralai/Mistral-7B-Instruct-v0.3"
    );
    assert.strictEqual(
      sidebar.formatModelLabel("gpt-4o", "openai_compatible"),
      "Custom OpenAI: gpt-4o"
    );
    assert.strictEqual(sidebar.formatModelLabel(null), "Connecting...");
  });

  // 5. isInternalWorkflowNode & isIntermediateSearchStep
  test("isInternalWorkflowNode & isIntermediateSearchStep: identifies ADK nodes and search agents", () => {
    // Internal workflow nodes
    assert.strictEqual(sidebar.isInternalWorkflowNode("search_library_pipeline_node"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("search_online_pipeline_node"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("coordinator_router"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("system"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("search_budget_checkpoint"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("checkpoint_0"), true);
    assert.strictEqual(sidebar.isInternalWorkflowNode("coordinator_agent"), false);
    assert.strictEqual(sidebar.isInternalWorkflowNode("library_synthesizer"), false);

    // Intermediate search agents
    assert.strictEqual(sidebar.isIntermediateSearchStep("library_search_agent"), true);
    assert.strictEqual(sidebar.isIntermediateSearchStep("online_search_agent"), true);
    assert.strictEqual(sidebar.isIntermediateSearchStep("custom_search_agent"), true);
    assert.strictEqual(sidebar.isIntermediateSearchStep("coordinator_agent"), false);
    assert.strictEqual(sidebar.isIntermediateSearchStep("online_synthesizer"), false);
  });

  // 6. calculateSessionStats
  test("calculateSessionStats: counts Zocrates and subagent turns while excluding welcome message", () => {
    const sessionData = {
      messages: [
        { role: "assistant", author: "Zocrates", text: "Hello! I am Zocrates, your research assistant." },
        { role: "user", text: "What is speculative decoding?" },
        { role: "assistant", author: "Zocrates", text: "Speculative decoding is an inference acceleration technique." },
        { role: "user", text: "Can you search my library for benchmarks?" },
        { role: "assistant", author: "Library Synthesis", text: "Here are the benchmarks from your library: [1]." },
      ],
    };

    const stats = sidebar.calculateSessionStats(sessionData);
    assert.strictEqual(stats.turns, 2, "Should count 2 assistant turns (Zocrates conversational + subagent synthesis)");
    assert.strictEqual(stats.turnsFromMessages, 2);
  });

  test("calculateSessionStats: sums tokens across all messages and falls back when total_tokens is 0", () => {
    const sessionData = {
      total_tokens: 0,
      total_cost_usd: 0.0,
      messages: [
        {
          role: "user",
          text: "Query 1",
        },
        {
          role: "assistant",
          author: "Zocrates",
          text: "Response 1",
          usage: { prompt_tokens: 100, candidates_tokens: 40, total_tokens: 140, cost_usd: 0.001 },
        },
        {
          role: "user",
          text: "Query 2",
        },
        {
          role: "assistant",
          author: "Library Synthesis",
          text: "Response 2",
          usage: { prompt_tokens: 200, candidates_tokens: 80, total_tokens: 0, cost_usd: 0.002 },
        },
        {
          role: "user",
          text: "Query 3",
        },
        {
          role: "assistant",
          author: "Zocrates",
          text: "Response 3",
          usage: { prompt_tokens: 50, candidates_tokens: 30, total_tokens: 80, cost_usd: 0.0005 },
        },
      ],
    };

    const stats = sidebar.calculateSessionStats(sessionData);
    assert.strictEqual(stats.totalTokens, 500, "Should sum tokens from all 3 messages");
    assert.strictEqual(stats.tokenBreakdown.prompt, 350, "Total prompt tokens");
    assert.strictEqual(stats.tokenBreakdown.candidates, 150, "Total candidates tokens");
    assert.strictEqual(stats.turns, 3, "Should count 3 turns");
    assert.strictEqual(Math.round(stats.costUsd * 10000) / 10000, 0.0035, "Should accumulate cost across messages");
  });

  test("calculateSessionStats: prioritizes backend top-level totals when positive", () => {
    const sessionData = {
      total_tokens: 1200,
      total_turns: 4,
      total_cost_usd: 0.05,
      messages: [
        {
          role: "assistant",
          author: "Zocrates",
          text: "Response",
          usage: { prompt_tokens: 100, candidates_tokens: 50, total_tokens: 150, cost_usd: 0.001 },
        },
      ],
    };

    const stats = sidebar.calculateSessionStats(sessionData);
    assert.strictEqual(stats.totalTokens, 1200, "Should use authoritative backend total_tokens");
    assert.strictEqual(stats.turns, 4, "Should use authoritative backend total_turns");
    assert.strictEqual(stats.costUsd, 0.05, "Should use authoritative backend total_cost_usd");
    assert.strictEqual(stats.totalTokensFromMessages, 150, "Preserves message-level token sum");
    assert.strictEqual(stats.turnsFromMessages, 1, "Preserves message-level turns count");
  });

  test("calculateSessionStats: supports session_metadata nesting", () => {
    const sessionData = {
      session_metadata: {
        total_tokens: 850,
        total_turns: 3,
        total_cost_usd: 0.015,
      },
      messages: [],
    };

    const stats = sidebar.calculateSessionStats(sessionData);
    assert.strictEqual(stats.totalTokens, 850);
    assert.strictEqual(stats.turns, 3);
    assert.strictEqual(stats.costUsd, 0.015);
  });

  // 7. computeTurnTokens
  test("computeTurnTokens: returns total_tokens or falls back to prompt + candidates", () => {
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_tokens: 100, candidates_tokens: 50, total_tokens: 150 }), 150);
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_token_count: 80, candidates_token_count: 20, total_token_count: 100 }), 100);

    // total_tokens is 0 but prompt + candidates exist
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_tokens: 100, candidates_tokens: 50, total_tokens: 0 }), 150);
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_token_count: 120, candidates_token_count: 40, total_token_count: 0 }), 160);

    // total_tokens missing entirely
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_tokens: 75, candidates_tokens: 25 }), 100);

    // All zero or null
    assert.strictEqual(sidebar.computeTurnTokens({ prompt_tokens: 0, candidates_tokens: 0, total_tokens: 0 }), 0);
    assert.strictEqual(sidebar.computeTurnTokens(null), 0);
    assert.strictEqual(sidebar.computeTurnTokens(undefined), 0);
  });

  // 11. copyTextToClipboard
  await testAsync("copyTextToClipboard: copies via navigator.clipboard and falls back gracefully", async () => {
    const origDesc = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    let written = "";
    Object.defineProperty(globalThis, "navigator", {
      value: {
        clipboard: {
          writeText: async (t) => {
            written = t;
          },
        },
      },
      configurable: true,
      writable: true,
    });
    if (global.window) {
      global.window.navigator = globalThis.navigator;
    }

    try {
      const res = await sidebar.copyTextToClipboard("Sidebar clipboard text");
      assert.strictEqual(res, true);
      assert.strictEqual(written, "Sidebar clipboard text");

      const resNull = await sidebar.copyTextToClipboard(null);
      assert.strictEqual(resNull, false, "copyTextToClipboard(null) must return false");
      const resEmpty = await sidebar.copyTextToClipboard("");
      assert.strictEqual(resEmpty, false, "copyTextToClipboard('') must return false");
    } finally {
      if (origDesc) {
        Object.defineProperty(globalThis, "navigator", origDesc);
      } else {
        delete globalThis.navigator;
      }
      if (global.window) {
        delete global.window.navigator;
      }
    }
  });

  // 12. Smooth Startup UX & Session Restoration
  console.log("\n--- Suite: Smooth Startup UX & Session Restoration ---");

  test("When stage === 'ready' and isProvisioning === false, a 'starting' event does not create #bootstrap-progress-card", () => {
    sidebar.setOnboardingStage("ready");
    const existing = global.document.getElementById("bootstrap-progress-card");
    if (existing) existing.remove();
    const sendBtn = global.document.getElementById("send-btn");
    sendBtn.disabled = false;
    sendBtn.title = "Ready";

    sidebar.handleBootstrapProgressEvent({
      state: "active",
      stepKey: "starting",
      detail: { state: "active", isProvisioning: false },
    });

    assert.strictEqual(global.document.getElementById("bootstrap-progress-card"), null);
    assert.strictEqual(sendBtn.disabled, true);
    assert.strictEqual(sendBtn.title, "Connecting to Zocrates backend…");
  });

  test("When isProvisioning === true, the installer card is created", () => {
    sidebar.setOnboardingStage("configure");
    const existing = global.document.getElementById("bootstrap-progress-card");
    if (existing) existing.remove();

    sidebar.handleBootstrapProgressEvent({
      state: "active",
      stepKey: "starting",
      detail: { state: "active", isProvisioning: true },
    });

    const card = global.document.getElementById("bootstrap-progress-card");
    assert.ok(card !== null, "Installer progress card must be created when provisioning is true");
    if (card) card.remove();
    sidebar.setOnboardingStage("ready");
  });

  test("An 'error' event always creates the error card with Retry button", () => {
    const existing = global.document.getElementById("bootstrap-progress-card");
    if (existing) existing.remove();

    sidebar.handleBootstrapProgressEvent({
      state: "error",
      stepKey: "error",
      detail: { state: "error", message: "Failed to spawn backend", failedStep: "starting" },
    });

    const card = global.document.getElementById("bootstrap-progress-card");
    assert.ok(card !== null, "Error card must be created on error state");
    const retryBtn = card.querySelector(".bootstrap-retry-btn");
    assert.ok(retryBtn !== null, "Retry button must be present in error card");
    if (card) card.remove();
  });

  test("setActiveSessionId persists the session ID to extensions.zocrates.last_active_session_id", () => {
    sidebar.setActiveSessionId("test-session-uuid-1234");
    assert.strictEqual(
      global.Zotero.Prefs.get("extensions.zocrates.last_active_session_id", true),
      "test-session-uuid-1234"
    );

    // Synthetic session IDs starting with session_ must not be persisted
    sidebar.setActiveSessionId("session_synthetic_999");
    assert.strictEqual(
      global.Zotero.Prefs.get("extensions.zocrates.last_active_session_id", true),
      "test-session-uuid-1234"
    );
  });

  test("resolveActiveSessionId reads from extensions.zocrates.last_active_session_id", () => {
    global.Zotero.Zocrates._activeSessionId = "";
    if (global.Zotero.Zocrates._detachedInitialState) {
      global.Zotero.Zocrates._detachedInitialState.currentSessionId = "";
    }
    global.Zotero.Prefs.set("extensions.zocrates.last_active_session_id", "persisted-session-5678", true);

    const resolved = sidebar.resolveActiveSessionId();
    assert.strictEqual(resolved, "persisted-session-5678");
  });

  await testAsync("loadSessions restores the saved session ID or defaults to the last session", async () => {
    const realFetch = global.fetch;
    try {
      global.fetch = async (url) => {
        if (String(url).includes("/sessions/")) {
          return {
            ok: true,
            json: async () => ({ session: { id: "mock", messages: [] } }),
          };
        }
        if (String(url).includes("/sessions")) {
          return {
            ok: true,
            json: async () => ({
              sessions: [
                { id: "sess-alpha", title: "Alpha Chat", created_at: "2025-01-01T00:00:00Z" },
                { id: "sess-beta", title: "Beta Chat", created_at: "2025-01-02T00:00:00Z" },
              ],
            }),
          };
        }
        return { ok: true, json: async () => ({}) };
      };

      // Case 1: Saved session exists in backend list -> restores it
      global.Zotero.Zocrates._activeSessionId = "";
      global.Zotero.Prefs.set("extensions.zocrates.last_active_session_id", "sess-alpha", true);
      await sidebar.loadSessions();
      assert.strictEqual(sidebar.getCurrentSessionId(), "sess-alpha");

      // Case 2: Saved session does NOT exist in backend list -> defaults to last session
      global.Zotero.Zocrates._activeSessionId = "";
      global.Zotero.Prefs.set("extensions.zocrates.last_active_session_id", "sess-unknown", true);
      await sidebar.loadSessions();
      assert.strictEqual(sidebar.getCurrentSessionId(), "sess-beta");

      // Case 3: Concurrent calls share the in-flight promise
      let fetchCount = 0;
      global.fetch = async (url) => {
        if (String(url).includes("/sessions")) fetchCount++;
        return {
          ok: true,
          json: async () => ({ sessions: [{ id: "sess-1", title: "Chat 1" }] }),
        };
      };
      await Promise.all([sidebar.loadSessions(), sidebar.loadSessions()]);
      assert.strictEqual(fetchCount, 2, "Concurrent loadSessions fetches must be deduplicated (1 list + 1 item detail)");
    } finally {
      global.fetch = realFetch;
      global.Zotero.Prefs.set("extensions.zocrates.last_active_session_id", "", true);
    }
  });

  test("handleBootstrapProgressEvent done event after suppression re-enables Send button", () => {
    sidebar.setOnboardingStage("ready");
    sidebar.setBackendReadyForChat(false);
    sidebar.handleBootstrapProgressEvent({
      state: "active",
      stepKey: "starting",
      detail: { state: "active", isProvisioning: false },
    });
    const sendBtn = global.document.getElementById("send-btn");
    assert.strictEqual(sendBtn.disabled, true);

    sidebar.handleBootstrapProgressEvent({ state: "done" });
    assert.strictEqual(sendBtn.disabled, false);
    assert.strictEqual(sendBtn.title, "Send Message (Ctrl/Cmd + Enter)");
  });

  // 13. Detached Sidebar State & Theme Synchronization
  console.log("\n--- Suite: Detached Sidebar State & Theme Synchronization ---");

  test("syncFromDetachedState: updates theme, persists to localStorage, and calls applyTheme()", () => {
    sidebar.syncFromDetachedState({ theme: "amoled" });
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "amoled");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "amoled");
    assert.strictEqual(global.localStorage.getItem("zocrates_theme"), "amoled");

    sidebar.syncFromDetachedState({ theme: "slate" });
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "slate");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "slate");
    assert.strictEqual(global.localStorage.getItem("zocrates_theme"), "slate");

    sidebar.syncFromDetachedState({ theme: "sepia" });
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "sepia");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "sepia");
    assert.strictEqual(global.localStorage.getItem("zocrates_theme"), "sepia");
  });

  test("loadSavedTheme: reads zocrates.theme from zot.Prefs and updates theme and DOM attribute", () => {
    global.Zotero.Prefs.set("zocrates.theme", "amoled");
    sidebar.loadSavedTheme();
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "amoled");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "amoled");

    global.Zotero.Prefs.set("zocrates.theme", "slate");
    sidebar.loadSavedTheme();
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "slate");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "slate");

    // Falls back to localStorage when zot.Prefs has no value
    global.Zotero.Prefs.set("zocrates.theme", null);
    global.localStorage.setItem("zocrates_theme", "sepia");
    sidebar.loadSavedTheme();
    assert.strictEqual(sidebar.getCurrentThemeSetting(), "sepia");
    assert.strictEqual(global.document.body.getAttribute("data-theme"), "sepia");
  });

  test("loadSavedTheme: resolves Zotero via resolveZotero() when typeof Zotero is undefined", () => {
    const origZotero = global.Zotero;
    const origWinZotero = global.window.Zotero;
    const origLocalZotero = sidebar.getZotero ? sidebar.getZotero() : undefined;
    if (sidebar.setZotero) sidebar.setZotero(undefined);
    delete global.Zotero;
    delete global.window.Zotero;

    try {
      global.window.opener = {
        Zotero: {
          Prefs: {
            get: (key) => (key === "zocrates.theme" ? "amoled" : null),
            set: () => {},
          },
        },
      };

      sidebar.loadSavedTheme();
      assert.strictEqual(sidebar.getCurrentThemeSetting(), "amoled");
      assert.strictEqual(global.document.body.getAttribute("data-theme"), "amoled");
    } finally {
      if (sidebar.setZotero) sidebar.setZotero(origLocalZotero);
      global.Zotero = origZotero;
      global.window.Zotero = origWinZotero;
      delete global.window.opener;
    }
  });

  test("getAutoRenderMermaidPref & getZoteroUsername: resolve correctly when global Zotero is undefined", () => {
    const origZotero = global.Zotero;
    const origWinZotero = global.window.Zotero;
    const origLocalZotero = sidebar.getZotero ? sidebar.getZotero() : undefined;
    if (sidebar.setZotero) sidebar.setZotero(undefined);
    delete global.Zotero;
    delete global.window.Zotero;

    try {
      // 1. Resolve via window.opener.Zotero
      global.window.opener = {
        Zotero: {
          Prefs: {
            get: (key, def) => {
              if (key === "extensions.zocrates.autoRenderMermaid") return false;
              if (key === "sync.server.username") return "sync-alice";
              return def;
            },
          },
        },
      };

      assert.strictEqual(sidebar.getAutoRenderMermaidPref(), false);
      assert.strictEqual(sidebar.getZoteroUsername(), "sync-alice");

      // 2. Resolve username from zot.User.name
      global.window.opener.Zotero.User = { name: "Carol" };
      assert.strictEqual(sidebar.getZoteroUsername(), "Carol");

      // 3. Resolve username from zot.Users.getCurrentUser().name
      delete global.window.opener.Zotero.User;
      global.window.opener.Zotero.Users = {
        getCurrentUser: () => ({ name: "Dave" }),
      };
      assert.strictEqual(sidebar.getZoteroUsername(), "Dave");
    } finally {
      if (sidebar.setZotero) sidebar.setZotero(origLocalZotero);
      global.Zotero = origZotero;
      global.window.Zotero = origWinZotero;
      delete global.window.opener;
    }
  });

  test("handleBeforeUnload: persists complete detached state to Zotero.Zocrates._detachedState when isDetached", () => {
    sidebar.setIsDetached(true);
    sidebar.setCurrentSessionId("session-detached-999");
    sidebar.setContextMode("papers");
    sidebar.setCurrentThemeSetting("amoled");
    sidebar.setActiveItemsList([{ key: "ITEMX", title: "Quantum Computing Paper" }]);
    sidebar.setActiveSnippetsList([{ id: "snip-9", text: "Qubit decoherence time" }]);
    const chatInput = global.document.getElementById("chat-input");
    chatInput.value = "Explain topological fault tolerance";

    try {
      sidebar.handleBeforeUnload();

      assert.strictEqual(global.Zotero.Zocrates._detachedNeedsRefresh, true);
      assert.strictEqual(global.Zotero.Zocrates._activeSessionId, "session-detached-999");

      const st = global.Zotero.Zocrates._detachedState;
      assert.ok(st !== null && typeof st === "object", "_detachedState must be written");
      assert.strictEqual(st.sessionId, "session-detached-999");
      assert.strictEqual(st.contextMode, "papers");
      assert.strictEqual(st.theme, "amoled");
      assert.strictEqual(st.fontSize, "default");
      assert.strictEqual(st.inputDraft, "Explain topological fault tolerance");
      assert.strictEqual(st.activeItemsList.length, 1);
      assert.strictEqual(st.activeItemsList[0].key, "ITEMX");
      assert.strictEqual(st.activeSnippetsList.length, 1);
      assert.strictEqual(st.activeSnippetsList[0].id, "snip-9");

      // Reattachment: syncFromDetachedState restores state
      sidebar.setIsDetached(false);
      sidebar.setCurrentThemeSetting("slate");
      chatInput.value = "";
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);

      sidebar.syncFromDetachedState(st);

      assert.strictEqual(sidebar.getCurrentThemeSetting(), "amoled");
      assert.strictEqual(global.document.body.getAttribute("data-theme"), "amoled");
      assert.strictEqual(chatInput.value, "Explain topological fault tolerance");
      assert.strictEqual(sidebar.getActiveItemsList().length, 1);
      assert.strictEqual(sidebar.getActiveItemsList()[0].key, "ITEMX");
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);
      assert.strictEqual(sidebar.getActiveSnippetsList()[0].id, "snip-9");
    } finally {
      sidebar.setIsDetached(false);
      sidebar.setCurrentSessionId("");
      sidebar.setCurrentThemeSetting("slate");
      if (chatInput) chatInput.value = "";
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);
      if (global.Zotero?.Zocrates) {
        delete global.Zotero.Zocrates._detachedState;
        delete global.Zotero.Zocrates._detachedNeedsRefresh;
      }
    }
  });

  test("font size management: applyFontSize, loadSavedFontSize, and syncFromDetachedState", () => {
    // 1. applyFontSize updates body attribute and data-selected
    sidebar.setCurrentFontSizeSetting("large");
    sidebar.applyFontSize();
    assert.strictEqual(global.document.body.getAttribute("data-font-size"), "large");
    assert.strictEqual(sidebar.getCurrentFontSizeSetting(), "large");

    // Invalid size falls back to default
    sidebar.setCurrentFontSizeSetting("invalid-size");
    sidebar.applyFontSize();
    assert.strictEqual(global.document.body.getAttribute("data-font-size"), "default");
    assert.strictEqual(sidebar.getCurrentFontSizeSetting(), "default");

    // 2. loadSavedFontSize reads from zot.Prefs
    global.Zotero.Prefs.set("zocrates.fontSize", "small");
    sidebar.loadSavedFontSize();
    assert.strictEqual(sidebar.getCurrentFontSizeSetting(), "small");
    assert.strictEqual(global.document.body.getAttribute("data-font-size"), "small");

    // Falls back to localStorage
    global.Zotero.Prefs.set("zocrates.fontSize", null);
    global.localStorage.setItem("zocrates_font_size", "xlarge");
    sidebar.loadSavedFontSize();
    assert.strictEqual(sidebar.getCurrentFontSizeSetting(), "xlarge");
    assert.strictEqual(global.document.body.getAttribute("data-font-size"), "xlarge");

    // 3. syncFromDetachedState syncs font size
    sidebar.syncFromDetachedState({ fontSize: "large" });
    assert.strictEqual(sidebar.getCurrentFontSizeSetting(), "large");
    assert.strictEqual(global.document.body.getAttribute("data-font-size"), "large");
    assert.strictEqual(global.localStorage.getItem("zocrates_font_size"), "large");

    // Cleanup
    sidebar.setCurrentFontSizeSetting("default");
    sidebar.applyFontSize();
    global.Zotero.Prefs.set("zocrates.fontSize", "default");
    global.localStorage.setItem("zocrates_font_size", "default");
  });

  // 13. message action button ordering (Save Note immediately left of Copy)
  test("message action button ordering: Save Note placed immediately left of Copy button with enhanced title", () => {
    // Test assistant message actions
    const assistantMsg = sidebar.appendMessageDOM("assistant", "Assistant response content");
    const assistantWrapper = assistantMsg.closest(".message-wrapper");
    const assistantActions = assistantWrapper.querySelector(".message-actions-right");
    const assistantBtns = Array.from(assistantActions.children);

    assert.strictEqual(assistantBtns.length, 3);
    assert.ok(assistantBtns[0].classList.contains("save-note-btn"), "First action button must be save-note-btn");
    assert.ok(assistantBtns[1].classList.contains("copy-btn"), "Second action button must be copy-btn");
    assert.ok(assistantBtns[2].classList.contains("info-btn"), "Third action button must be info-btn");
    assert.strictEqual(
      assistantBtns[0].getAttribute("title"),
      "Save as Zotero Note (indexed by Zocrates)"
    );

    // Test user message actions
    const userMsg = sidebar.appendMessageDOM("user", "User query prompt");
    const userWrapper = userMsg.closest(".message-wrapper");
    const userActions = userWrapper.querySelector(".message-actions-right");
    const userBtns = Array.from(userActions.children);

    assert.strictEqual(userBtns.length, 4);
    assert.ok(userBtns[0].classList.contains("rerun-btn"), "First action button for user must be rerun-btn");
    assert.ok(userBtns[1].classList.contains("save-note-btn"), "Second action button for user must be save-note-btn (left of copy)");
    assert.ok(userBtns[2].classList.contains("copy-btn"), "Third action button for user must be copy-btn");
    assert.ok(userBtns[3].classList.contains("info-btn"), "Fourth action button for user must be info-btn");
    assert.strictEqual(
      userBtns[1].getAttribute("title"),
      "Save as Zotero Note (indexed by Zocrates)"
    );
  });

  // 14. notification item copy button
  await testAsync("notification item copy button: renders copy button and copies notification text on click", async () => {
    const listEl = global.document.getElementById("notifications-list");
    const sampleText = "Imported 3 papers successfully into library!";

    sidebar.setNotifications([
      {
        id: "notif-test-1",
        text: sampleText,
        type: "success",
        timestamp: 1715000000000,
      },
    ]);

    sidebar.renderNotifications();

    const notifItem = listEl.querySelector(".notification-item");
    assert.ok(notifItem, "Notification item must be rendered");
    assert.ok(notifItem.classList.contains("notification-success"), "Must have type class");

    const header = notifItem.querySelector(".notification-header");
    assert.ok(header, "Header must be present");

    const actions = header.querySelector(".notification-actions");
    assert.ok(actions, "Actions wrapper must be present");

    const copyBtn = actions.querySelector(".notification-copy-btn");
    const deleteBtn = actions.querySelector(".notification-delete-btn");
    assert.ok(copyBtn, "Copy button must be present");
    assert.ok(deleteBtn, "Delete button must be present");
    assert.strictEqual(copyBtn.getAttribute("title"), "Copy notification");

    const copyIcon = copyBtn.querySelector(".copy-icon");
    const checkIcon = copyBtn.querySelector(".check-icon");
    assert.ok(copyIcon, "Copy icon SVG must be present");
    assert.ok(checkIcon, "Check icon SVG must be present");
    assert.ok(checkIcon.classList.contains("hidden"), "Check icon must be initially hidden");

    let copiedText = null;
    const origDesc = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    Object.defineProperty(globalThis, "navigator", {
      value: {
        clipboard: {
          writeText: async (t) => {
            copiedText = t;
          },
        },
      },
      configurable: true,
      writable: true,
    });
    if (global.window) {
      global.window.navigator = globalThis.navigator;
    }

    try {
      copyBtn.dispatchEvent("click");
      await new Promise((resolve) => setTimeout(resolve, 50));

      assert.strictEqual(copiedText, sampleText, "Must copy exact notification text");
      assert.ok(copyIcon.classList.contains("hidden"), "Copy icon should be hidden after copying");
      assert.ok(!checkIcon.classList.contains("hidden"), "Check icon should be shown after copying");
      assert.strictEqual(copyBtn.getAttribute("title"), "Copied!");
    } finally {
      if (origDesc) {
        Object.defineProperty(globalThis, "navigator", origDesc);
      } else {
        delete globalThis.navigator;
      }
      if (global.window) {
        delete global.window.navigator;
      }
      sidebar.setNotifications([]);
      sidebar.renderNotifications();
    }
  });

  // ---------------------------------------------------------------------------
  // Error & Diagnostic Reporting UI Tests
  // ---------------------------------------------------------------------------
  test("appendMessageDOM: renders .error-report-action-btn ONLY when metadata.isError === true and never auto-opens modal", () => {
    const InfoModal = require("../../zotero-plugin/addon/lib/infoModal.js");
    global.InfoModal = InfoModal;
    if (global.window) global.window.InfoModal = InfoModal;

    const chatEl = document.getElementById("chat-messages");
    chatEl.innerHTML = "";

    // 1. Normal assistant message starting with "Error:" (NOT an explicit error bubble)
    const normalMsg = sidebar.appendMessageDOM(
      "assistant",
      "Error: In statistics, Type I error refers to rejecting a true null hypothesis.",
      true,
      null,
      "Zocrates",
      null,
      null,
      { invocation_id: "inv_normal" }
    );
    const normalWrapper = normalMsg.parentElement;
    assert.strictEqual(
      normalWrapper.querySelector(".error-report-action-btn"),
      null,
      "Normal assistant message starting with 'Error:' must NOT receive .error-report-action-btn"
    );

    // 2. Explicit error bubble with metadata.isError === true
    const errMsg = sidebar.appendMessageDOM(
      "assistant",
      "System Error: Chat service failed: 401 Unauthorized",
      false,
      null,
      "System",
      null,
      null,
      {
        isError: true,
        errorMessage: "401 Unauthorized Missing Authentication Header",
        invocationId: "inv_err_1",
      }
    );
    const errWrapper = errMsg.parentElement;
    const reportBtn = errWrapper.querySelector(".error-report-action-btn");
    assert.ok(reportBtn, "Explicit error bubble must render .error-report-action-btn");
    assert.strictEqual(reportBtn.textContent, "🛠️ Report Issue...");
    assert.strictEqual(
      document.getElementById("zocrates-info-modal-overlay"),
      null,
      "Diagnostic modal must NEVER auto-open when an error bubble is appended"
    );
  });

  await testAsync("openDiagnosticReportModal: renders 3 unchecked toggles, updates preview in-place, copies Markdown, and opens GitHub", async () => {
    const InfoModal = require("../../zotero-plugin/addon/lib/infoModal.js");
    global.InfoModal = InfoModal;
    if (global.window) global.window.InfoModal = InfoModal;

    const origFetch = global.fetch;
    const origNavDesc = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    const fetchPayloads = [];
    let copiedText = null;
    let launchedUrl = null;

    const zot = sidebar.getZotero();
    const origLaunch = zot ? zot.launchURL : null;
    if (zot) {
      zot.launchURL = (url) => {
        launchedUrl = url;
      };
    }

    Object.defineProperty(globalThis, "navigator", {
      value: {
        clipboard: {
          writeText: async (t) => {
            copiedText = t;
          },
        },
      },
      configurable: true,
      writable: true,
    });
    if (global.window) global.window.navigator = globalThis.navigator;

    global.fetch = async (url, opts) => {
      if (String(url).endsWith("manifest.json")) {
        return { ok: true, json: async () => ({ version: "0.1.0" }) };
      }
      if (String(url).includes("/diagnostics/report")) {
        const body = JSON.parse(opts.body);
        fetchPayloads.push(body);
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            markdown: `## Report\nprompts=${body.include_prompts}\ntitles=${body.include_item_titles}\nlogs=${body.include_logs}`,
          }),
        };
      }
      throw new Error("Unexpected URL: " + url);
    };

    try {
      const textarea = await sidebar.openDiagnosticReportModal({
        errorMessage: "401 Unauthorized",
        sessionId: "sess_diag_1",
        invocationId: "inv_diag_1",
      });

      const overlay = document.getElementById("zocrates-info-modal-overlay");
      assert.ok(overlay, "Diagnostic modal overlay should be mounted");
      assert.ok(textarea, "Preview textarea should be returned");
      assert.strictEqual(textarea.getAttribute("readonly"), "readonly");

      const promptsToggle = overlay.querySelector(".diagnostic-toggle-prompts");
      const titlesToggle = overlay.querySelector(".diagnostic-toggle-titles");
      const logsToggle = overlay.querySelector(".diagnostic-toggle-logs");
      assert.ok(promptsToggle && titlesToggle && logsToggle, "All 3 privacy checkboxes must exist");
      assert.strictEqual(promptsToggle.checked, false, "Prompts toggle must default to false");
      assert.strictEqual(titlesToggle.checked, false, "Titles toggle must default to false");
      assert.strictEqual(logsToggle.checked, false, "Logs toggle must default to false");

      assert.strictEqual(fetchPayloads.length, 1);
      assert.strictEqual(fetchPayloads[0].include_prompts, false);
      assert.strictEqual(fetchPayloads[0].include_item_titles, false);
      assert.strictEqual(fetchPayloads[0].include_logs, false);
      assert.ok(textarea.value.includes("prompts=false"));

      // Toggle logs checkbox -> debounced in-place update without closing modal
      logsToggle.checked = true;
      logsToggle.dispatchEvent("change");
      await new Promise((r) => setTimeout(r, 220));
      assert.strictEqual(fetchPayloads.length, 2, "Changing toggle should fetch updated report");
      assert.strictEqual(fetchPayloads[1].include_logs, true);
      assert.ok(textarea.value.includes("logs=true"), "Textarea must update in-place");
      assert.ok(document.getElementById("zocrates-info-modal-overlay"), "Modal must remain open");

      // Click Copy button
      const copyBtn = overlay.querySelector(".diagnostic-copy-btn");
      assert.ok(copyBtn, "Copy button must be rendered");
      copyBtn.dispatchEvent("click");
      await new Promise((r) => setTimeout(r, 30));
      assert.strictEqual(copiedText, textarea.value, "Copy must write exact preview Markdown");
      assert.strictEqual(copyBtn.textContent, "✓ Copied!", "Copy button label should confirm copy");
      assert.ok(document.getElementById("zocrates-info-modal-overlay"), "Copy must not close modal");

      // Click Open GitHub Issues button
      const ghBtn = overlay.querySelector(".diagnostic-github-btn");
      assert.ok(ghBtn, "GitHub button must be rendered");
      ghBtn.dispatchEvent("click");
      await new Promise((r) => setTimeout(r, 20));
      assert.strictEqual(launchedUrl, "https://github.com/alexplaka/Zocrates/issues/new");
      assert.ok(document.getElementById("zocrates-info-modal-overlay"), "GitHub button must not close modal");

      // Close modal cleanly
      const closeBtn = overlay.querySelector(".diagnostic-close-btn");
      closeBtn.dispatchEvent("click");
      await new Promise((r) => setTimeout(r, 20));
      assert.strictEqual(document.getElementById("zocrates-info-modal-overlay"), null);
    } finally {
      global.fetch = origFetch;
      if (zot) zot.launchURL = origLaunch;
      if (origNavDesc) {
        Object.defineProperty(globalThis, "navigator", origNavDesc);
      } else {
        delete globalThis.navigator;
      }
      const leftover = document.getElementById("zocrates-info-modal-overlay");
      if (leftover) leftover.remove();
    }
  });

  await testAsync("openDiagnosticReportModal: offline fallback generates sanitized client report when backend fetch fails", async () => {
    const InfoModal = require("../../zotero-plugin/addon/lib/infoModal.js");
    global.InfoModal = InfoModal;
    if (global.window) global.window.InfoModal = InfoModal;

    const origFetch = global.fetch;
    global.fetch = async (url) => {
      if (String(url).endsWith("manifest.json")) {
        return { ok: true, json: async () => ({ version: "0.1.0" }) };
      }
      throw new Error("Connection refused at /home/alice/Zotero with Bearer secret_tok_abc");
    };

    sidebar.setActiveItemsList([{ key: "PAPER99", title: "Unpublished Secret Paper" }]);
    sidebar.setNotifications([
      {
        id: "n1",
        type: "error",
        text: "Rejected sk-or-v1-aabbccddeeff11223344 for user@mit.edu at C:\\Users\\Bob\\Zotero",
        timestamp: Date.now(),
      },
    ]);

    try {
      const textarea = await sidebar.openDiagnosticReportModal({
        errorMessage: "Failed key sk-proj-1234567890abcdefghij at /Users/charlie/Zotero",
        sessionId: "sess_offline",
      });

      const md = textarea.value;
      assert.ok(md.includes("Client Fallback"), "Should render client fallback header");
      assert.ok(!md.includes("sk-or-v1-"), "OpenRouter key must be scrubbed in fallback");
      assert.ok(!md.includes("sk-proj-"), "Generic sk- key must be scrubbed in fallback");
      assert.ok(!md.includes("secret_tok_abc"), "Bearer token must be scrubbed in fallback");
      assert.ok(!md.includes("user@mit.edu"), "Email must be scrubbed in fallback");
      assert.ok(!md.includes("alice"), "Linux username must be scrubbed in fallback");
      assert.ok(!md.includes("Bob"), "Windows username must be scrubbed in fallback");
      assert.ok(!md.includes("charlie"), "macOS username must be scrubbed in fallback");
      assert.ok(!md.includes("Unpublished Secret Paper"), "Paper title must be omitted by default");
      assert.ok(md.includes("[Key: PAPER99] <Title omitted>"));
    } finally {
      global.fetch = origFetch;
      sidebar.setActiveItemsList([]);
      sidebar.setNotifications([]);
      const leftover = document.getElementById("zocrates-info-modal-overlay");
      if (leftover) leftover.remove();
    }
  });

  await testAsync("about-report-issue-btn repointed to openDiagnosticReportModal and saveDiagnosticReportToDisk writes via FilePicker + IOUtils", async () => {
    const InfoModal = require("../../zotero-plugin/addon/lib/infoModal.js");
    global.InfoModal = InfoModal;
    if (global.window) global.window.InfoModal = InfoModal;

    const origFetch = global.fetch;
    global.fetch = async (url) => {
      if (String(url).endsWith("manifest.json")) {
        return { ok: true, json: async () => ({ version: "0.1.0" }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "success", markdown: "## About Menu Report" }),
      };
    };

    const zot = sidebar.getZotero();
    const origLaunch = zot ? zot.launchURL : null;
    const origFP = global.FilePicker;
    const origIO = global.IOUtils;
    let launchedDirectly = false;
    let writtenPath = null;
    let writtenContent = null;

    if (zot) {
      zot.launchURL = () => {
        launchedDirectly = true;
      };
    }

    global.FilePicker = class MockFilePicker {
      constructor() {
        this.modeSave = 1;
        this.returnOK = 0;
        this.returnReplace = 2;
        this.file = "/tmp/zocrates-diag-saved.md";
      }
      init() {}
      appendFilter() {}
      async show() {
        return this.returnOK;
      }
    };
    global.IOUtils = Object.assign({}, origIO || {}, {
      writeUTF8: async (p, content) => {
        writtenPath = p;
        writtenContent = content;
      },
    });
    if (global.window) global.window.IOUtils = global.IOUtils;

    try {
      const aboutReportBtn = document.getElementById("about-report-issue-btn");
      assert.ok(aboutReportBtn, "#about-report-issue-btn must exist");
      aboutReportBtn.dispatchEvent("click");
      await new Promise((r) => setTimeout(r, 40));

      assert.strictEqual(
        launchedDirectly,
        false,
        "Clicking #about-report-issue-btn must NOT directly launch browser URL"
      );
      const overlay = document.getElementById("zocrates-info-modal-overlay");
      assert.ok(overlay, "Clicking #about-report-issue-btn must open diagnostic modal");

      const saveBtn = overlay.querySelector(".diagnostic-save-btn");
      assert.ok(saveBtn, "Save button must exist");
      saveBtn.dispatchEvent("click");
      await new Promise((r) => setTimeout(r, 40));

      assert.strictEqual(writtenPath, "/tmp/zocrates-diag-saved.md");
      assert.strictEqual(writtenContent, "## About Menu Report");
      assert.strictEqual(saveBtn.textContent, "✓ Saved!");
      assert.ok(document.getElementById("zocrates-info-modal-overlay"), "Save must not close modal");
    } finally {
      global.fetch = origFetch;
      if (zot) zot.launchURL = origLaunch;
      if (origFP !== undefined) global.FilePicker = origFP;
      else delete global.FilePicker;
      if (origIO !== undefined) global.IOUtils = origIO;
      else delete global.IOUtils;
      const leftover = document.getElementById("zocrates-info-modal-overlay");
      if (leftover) leftover.remove();
    }
  });

  // 12. openMessageInfoModal: Citation Support stats and counts
  test("openMessageInfoModal: renders Citation Support row with mean ± std and evaluated counts", () => {
    const meta = {
      role: "assistant",
      author: "Zocrates",
      text: "Synthesis text [1, 2].",
      citation_verification: {
        provider: "openrouter",
        model: "inception/mercury-decide",
        threshold: 0.60,
        latency_ms: 245.5,
        summary: {
          batch_size: 2,
          kept: 2,
          demoted: 0,
          fail_open: 0,
          mean_support: 0.852,
          std_support: 0.041,
          latency_ms: 245.5,
        },
        assessments: [
          {
            claim: "Claim A assertion.",
            source_text: "Passage A from paper.",
            title: "Paper Title A",
            authors: ["Author A"],
            year: 2021,
            section: "Methods",
            support_probability: 0.881,
            threshold: 0.60,
            verdict: "KEPT",
          },
          {
            claim: "Claim B assertion.",
            source_text: "Passage B from paper.",
            title: "Paper Title B",
            authors: ["Author B1", "Author B2"],
            year: 2022,
            section: "Results",
            support_probability: 0.823,
            threshold: 0.60,
            verdict: "KEPT",
          },
        ],
      },
    };

    try {
      sidebar.openMessageInfoModal(meta);
      const overlay = document.querySelector(".info-modal-overlay");
      assert.ok(overlay, "Modal overlay must exist in DOM");

      const rows = Array.from(overlay.querySelectorAll(".info-meta-row"));
      const citRow = rows.find(r => r.querySelector(".info-meta-label")?.textContent === "Citation Support");
      assert.ok(citRow, "Citation Support row must be rendered");

      const pill = citRow.querySelector(".citation-support-pill");
      assert.ok(pill, "Model/latency pill must exist");
      assert.strictEqual(pill.textContent, "inception/mercury-decide · 246ms");

      const meanEl = citRow.querySelector(".citation-support-mean");
      assert.ok(meanEl, "Mean support element must exist");
      assert.strictEqual(meanEl.textContent, "Mean p: 0.852 ± 0.041 (std)");

      const countsBox = citRow.querySelector(".citation-support-counts");
      assert.ok(countsBox, "Counts box must exist");
      assert.ok(countsBox.textContent.includes("2 claims evaluated: 2 verified (p ≥ 0.600), 0 rejected (p < 0.600)"));

      // demoted === 0 -> no toggle button or drawer
      const toggleBtn = citRow.querySelector(".citation-support-toggle-btn");
      assert.strictEqual(toggleBtn, null, "Toggle button must not appear when demoted === 0");
    } finally {
      const leftover = document.querySelector(".info-modal-overlay");
      if (leftover) leftover.remove();
    }
  });

  // 13. openMessageInfoModal: Citation Support Statistics Row and modal drawer cleanup
  test("openMessageInfoModal: renders Citation Support row with stats and verifies cramped rejected drawer is removed", () => {
    const meta = {
      role: "assistant",
      author: "Zocrates",
      text: "Synthesis text citing two items [1, 2].",
      citation_verification: {
        provider: "openrouter",
        model: "inception/mercury-decide",
        threshold: 0.60,
        latency_ms: 180,
        summary: {
          batch_size: 2,
          kept: 1,
          demoted: 1,
          mean_support: 0.620,
          std_support: 0.325,
          latency_ms: 180,
        },
        assessments: [
          {
            claim: "Mutation rates rise significantly across generations.",
            source_text: "We identified embryonic mutations per twin pair without cross-generational rise.",
            title: "Somatic mutation rates in monozygotic twins",
            authors: ["Jonsson, Hakon", "Stefansson, Kari"],
            year: 2021,
            section: "Results",
            support_probability: 0.390,
            threshold: 0.60,
            verdict: "DEMOTED",
          },
          {
            claim: "Self-attention computes representations across sequences.",
            source_text: "Attention maps a query and key-value pairs to an output.",
            title: "Attention Is All You Need",
            authors: ["Vaswani", "Shazeer", "Parmar"],
            year: 2017,
            section: "Attention",
            support_probability: 0.850,
            threshold: 0.60,
            verdict: "KEPT",
          },
        ],
      },
    };

    try {
      sidebar.openMessageInfoModal(meta);
      const overlay = document.querySelector(".info-modal-overlay");
      assert.ok(overlay, "Modal overlay must exist");

      const citRow = overlay.querySelector(".citation-support-row");
      assert.ok(citRow, "Citation Support row must exist");

      const meanEl = citRow.querySelector(".citation-support-mean");
      assert.strictEqual(meanEl.textContent, "Mean p: 0.620 ± 0.325 (std)");

      const countsBox = citRow.querySelector(".citation-support-counts");
      assert.ok(countsBox.textContent.includes("2 claims evaluated: 1 verified (p ≥ 0.600), 1 rejected (p < 0.600)"));

      // Verify cramped drawer and toggle button are removed from modal
      const toggleBtn = citRow.querySelector(".citation-support-toggle-btn");
      assert.strictEqual(toggleBtn, null, "Toggle button must NOT exist in info modal");

      const drawer = citRow.querySelector(".citation-rejected-drawer");
      assert.strictEqual(drawer, null, "Cramped drawer must NOT exist in info modal");
    } finally {
      const leftover = document.querySelector(".info-modal-overlay");
      if (leftover) leftover.remove();
    }
  });

  // 14. openMessageInfoModal: Display formatting when N=1 and N=0
  test("openMessageInfoModal: handles single probability (N=1) and zero probabilities (N=0)", () => {
    // N = 1
    const metaSingle = {
      role: "assistant",
      author: "Zocrates",
      citation_verification: {
        summary: { batch_size: 1, kept: 1, demoted: 0, mean_support: 0.850, std_support: 0.0 },
        assessments: [{ support_probability: 0.850, verdict: "KEPT" }],
      },
    };

    try {
      sidebar.openMessageInfoModal(metaSingle);
      const overlay1 = document.querySelector(".info-modal-overlay");
      const meanEl1 = overlay1.querySelector(".citation-support-mean");
      assert.strictEqual(meanEl1.textContent, "Mean p: 0.850 ± N/A (std)");
    } finally {
      document.querySelectorAll(".info-modal-overlay").forEach(el => el.remove());
    }

    // N = 0 (no non-null probabilities)
    const metaZero = {
      role: "assistant",
      author: "Zocrates",
      citation_verification: {
        summary: { batch_size: 1, kept: 0, demoted: 0, fail_open: 1, mean_support: null, std_support: null },
        assessments: [{ support_probability: null, verdict: "FAIL_OPEN" }],
      },
    };

    try {
      sidebar.openMessageInfoModal(metaZero);
      const overlay0 = document.querySelector(".info-modal-overlay");
      const meanEl0 = overlay0.querySelector(".citation-support-mean");
      assert.strictEqual(meanEl0.textContent, "Mean p: N/A");

      const countsBox0 = overlay0.querySelector(".citation-support-counts");
      assert.ok(countsBox0.textContent.includes("1 unverified"));
    } finally {
      document.querySelectorAll(".info-modal-overlay").forEach(el => el.remove());
    }
  });

  // 15. openMessageInfoModal: Does not render Citation Support row when verification was not run
  test("openMessageInfoModal: does NOT render Citation Support row when verification was disabled (citations only, no probs)", () => {
    const metaNoVerif = {
      role: "assistant",
      author: "Zocrates",
      citations: [
        {
          index: 1,
          token: "[1]",
          title: "Unverified Paper",
          support_probability: null,
          occurrences: [{ occurrence: 1, support_probability: null, survived: true }],
        },
      ],
    };

    try {
      sidebar.openMessageInfoModal(metaNoVerif);
      const overlay = document.querySelector(".info-modal-overlay");
      assert.ok(overlay, "Modal overlay must exist");
      const citRow = overlay.querySelector(".citation-support-row");
      assert.strictEqual(citRow, null, "Citation Support row must NOT be rendered when no verification was run");
    } finally {
      document.querySelectorAll(".info-modal-overlay").forEach(el => el.remove());
    }
  });

  // 16. buildClaimsInspectorData: normal verification extraction
  test("buildClaimsInspectorData: extracts assessments and summary from citation_verification", () => {
    const meta = {
      model: "google/gemini-2.5-flash",
      citation_verification: {
        model: "inception/mercury-decide:free",
        provider: "openrouter",
        threshold: 0.65,
        latency_ms: 240,
        summary: {
          batch_size: 2,
          kept: 1,
          demoted: 1,
          fail_open: 0,
          mean_support: 0.620,
          std_support: 0.325,
        },
        assessments: [
          {
            chunk_index: 1,
            occurrence: 1,
            claim: "Claim A",
            source_text: "Passage A",
            item_key: "ITEM1",
            attachment_key: "ATT1",
            support_probability: 0.95,
            verdict: "KEPT",
          },
          {
            chunk_index: 2,
            occurrence: 1,
            claim: "Claim B",
            source_text: "Passage B",
            item_key: "ITEM2",
            attachment_key: "ATT2",
            support_probability: 0.20,
            verdict: "DEMOTED",
          },
        ],
      },
    };

    const data = sidebar.buildClaimsInspectorData(meta);
    assert.strictEqual(data.model, "inception/mercury-decide:free");
    assert.strictEqual(data.provider, "openrouter");
    assert.strictEqual(data.threshold, 0.65);
    assert.strictEqual(data.latency_ms, 240);
    assert.strictEqual(data.assessments.length, 2);
    assert.strictEqual(data.summary.kept, 1);
    assert.strictEqual(data.summary.demoted, 1);
    assert.strictEqual(data.assessments[0].item_key, "ITEM1");
    assert.strictEqual(data.assessments[1].item_key, "ITEM2");
  });

  // 17. buildClaimsInspectorData: cross-references citations to backfill missing keys
  test("buildClaimsInspectorData: cross-references citations to backfill missing item_key and attachment_key", () => {
    const metaOlderSession = {
      citations: [
        {
          index: 1,
          item_key: "BACKFILLED_KEY_1",
          attachment_key: "BACKFILLED_ATT_1",
          title: "Title 1",
          authors: ["Author 1"],
          year: "2020",
          section: "Methods",
        },
        {
          index: 2,
          item_key: "BACKFILLED_KEY_2",
          attachment_key: "BACKFILLED_ATT_2",
          title: "Title 2",
          authors: ["Author 2"],
          year: "2021",
          section: "Results",
        },
      ],
      citation_verification: {
        summary: { batch_size: 2, kept: 2, demoted: 0, fail_open: 0 },
        assessments: [
          {
            chunk_index: 1,
            occurrence: 1,
            claim: "Claim 1",
            source_text: "Source text 1",
            // item_key missing in older session
            support_probability: 0.90,
            verdict: "KEPT",
          },
          {
            chunk_index: 2,
            occurrence: 1,
            claim: "Claim 2",
            source_text: "Source text 2",
            // item_key missing in older session
            support_probability: 0.85,
            verdict: "KEPT",
          },
        ],
      },
    };

    const data = sidebar.buildClaimsInspectorData(metaOlderSession);
    assert.strictEqual(data.assessments[0].item_key, "BACKFILLED_KEY_1");
    assert.strictEqual(data.assessments[0].attachment_key, "BACKFILLED_ATT_1");
    assert.strictEqual(data.assessments[0].title, "Title 1");
    assert.strictEqual(data.assessments[1].item_key, "BACKFILLED_KEY_2");
    assert.strictEqual(data.assessments[1].attachment_key, "BACKFILLED_ATT_2");
  });

  // 18. buildClaimsInspectorData: fallback when only citations exist
  test("buildClaimsInspectorData: constructs rows and summary from citation occurrences when assessments are absent", () => {
    const metaCitationsOnly = {
      citations: [
        {
          index: 1,
          item_key: "KEY1",
          attachment_key: "ATT1",
          title: "Paper 1",
          quote: "Quote 1",
          authors: ["Alice"],
          year: "2022",
          section: "Intro",
          occurrences: [
            { occurrence: 1, claim: "Claim 1.1", support_probability: 0.92, survived: true },
            { occurrence: 2, claim: "Claim 1.2", support_probability: 0.45, survived: false },
          ],
        },
        {
          index: 2,
          item_key: "KEY2",
          title: "Paper 2",
          quote: "Quote 2",
          support_probability: 0.88,
          occurrences: [
            { occurrence: 1, claim: "Claim 2.1", support_probability: 0.88, survived: true },
          ],
        },
      ],
    };

    const data = sidebar.buildClaimsInspectorData(metaCitationsOnly);
    assert.strictEqual(data.assessments.length, 3);
    assert.strictEqual(data.summary.batch_size, 3);
    assert.strictEqual(data.summary.kept, 2);
    assert.strictEqual(data.summary.demoted, 1);
    assert.strictEqual(data.summary.fail_open, 0);
    assert.strictEqual(data.assessments[0].claim, "Claim 1.1");
    assert.strictEqual(data.assessments[0].verdict, "KEPT");
    assert.strictEqual(data.assessments[1].claim, "Claim 1.2");
    assert.strictEqual(data.assessments[1].verdict, "DEMOTED");
    assert.strictEqual(data.assessments[2].claim, "Claim 2.1");
    assert.strictEqual(data.assessments[2].verdict, "KEPT");
  });

  // 19. ensureClaimsInspectorBtn: creates button in .message-actions-left next to .citation-toggle-btn
  test("ensureClaimsInspectorBtn: creates .claims-inspector-btn directly next to .citation-toggle-btn with accurate counts", () => {
    const wrapperDiv = document.createElement("div");
    wrapperDiv.className = "message-wrapper assistant";

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = "Assistant synthesis with citations [1].";
    wrapperDiv.appendChild(bubble);

    const actionsDiv = document.createElement("div");
    actionsDiv.className = "message-actions";
    const actionsLeftDiv = document.createElement("div");
    actionsLeftDiv.className = "message-actions-left";
    actionsDiv.appendChild(actionsLeftDiv);
    wrapperDiv.appendChild(actionsDiv);

    const meta = {
      citation_verification: {
        summary: { batch_size: 5, kept: 3, demoted: 2, fail_open: 0 },
        assessments: [
          { claim: "C1", verdict: "KEPT", support_probability: 0.9 },
          { claim: "C2", verdict: "KEPT", support_probability: 0.8 },
          { claim: "C3", verdict: "KEPT", support_probability: 0.7 },
          { claim: "C4", verdict: "DEMOTED", support_probability: 0.3 },
          { claim: "C5", verdict: "DEMOTED", support_probability: 0.2 },
        ],
      },
    };
    bubble._metadata = meta;
    wrapperDiv._metadata = meta;

    // First ensure citation toggle button is created
    sidebar.ensureCitationToggleBtn(wrapperDiv);
    const toggleBtn = actionsLeftDiv.querySelector(".citation-toggle-btn");
    assert.ok(toggleBtn, "Citation toggle button should exist");

    // Call ensureClaimsInspectorBtn
    sidebar.ensureClaimsInspectorBtn(wrapperDiv);

    const claimsBtn = actionsLeftDiv.querySelector(".claims-inspector-btn");
    assert.ok(claimsBtn, "Claims Inspector button must be created in .message-actions-left");
    assert.strictEqual(claimsBtn.textContent, "🛡️ Claim Verification Inspector");
    assert.strictEqual(
      claimsBtn.getAttribute("title"),
      "Inspect claim verification table (3 verified, 2 rejected of 5 total)"
    );

    // Verify ordering: toggleBtn comes first, claimsBtn is directly next to it
    assert.strictEqual(actionsLeftDiv.children[0], toggleBtn);
    assert.strictEqual(actionsLeftDiv.children[1], claimsBtn);

    // Verify idempotency: calling ensureClaimsInspectorBtn again does not duplicate
    sidebar.ensureClaimsInspectorBtn(wrapperDiv);
    const allClaimsBtns = actionsLeftDiv.querySelectorAll(".claims-inspector-btn");
    assert.strictEqual(allClaimsBtns.length, 1, "Must not duplicate Claims Inspector button");

    // Removal test: when metadata has no verification/citations
    bubble._metadata = { citation_verification: null, citations: [] };
    wrapperDiv._metadata = { citation_verification: null, citations: [] };
    sidebar.ensureClaimsInspectorBtn(wrapperDiv);
    assert.strictEqual(actionsLeftDiv.querySelector(".claims-inspector-btn"), null, "Must remove button when no assessments exist");
  });

  // 20. openClaimsInspectorWindow: opens dialog window with structured payload
  test("openClaimsInspectorWindow: invokes openDialog with claims.html and structured payload", () => {
    let openedDialogUrl = null;
    let openedDialogName = null;
    let openedDialogFeatures = null;
    let openedDialogPayload = null;

    const mockWin = {
      openDialog: (url, name, features, payload) => {
        openedDialogUrl = url;
        openedDialogName = name;
        openedDialogFeatures = features;
        openedDialogPayload = payload;
        return {
          updateClaimsData: () => {},
          focus: () => {},
        };
      },
    };

    const origTop = window.top;
    try {
      window.top = mockWin;
      const sampleMeta = {
        model: "test-model",
        citation_verification: {
          summary: { batch_size: 1, kept: 1, demoted: 0 },
          assessments: [{ claim: "Test Claim", verdict: "KEPT", support_probability: 0.95 }],
        },
      };

      sidebar.openClaimsInspectorWindow(sampleMeta);

      assert.strictEqual(openedDialogUrl, "chrome://zocrates/content/claims.html");
      assert.strictEqual(openedDialogName, "zocrates-claims-window");
      assert.ok(openedDialogFeatures.includes("width=1180"));
      assert.ok(openedDialogFeatures.includes("height=760"));
      assert.ok(openedDialogPayload, "Payload must be passed to openDialog");
      assert.strictEqual(openedDialogPayload.assessments.length, 1);
      assert.strictEqual(openedDialogPayload.assessments[0].claim, "Test Claim");
      assert.strictEqual(openedDialogPayload._sidebarWindow, window, "Payload must have _sidebarWindow reference");
    } finally {
      window.top = origTop;
    }
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

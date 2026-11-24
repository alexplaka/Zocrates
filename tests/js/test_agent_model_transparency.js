/**
 * test_agent_model_transparency.js — Tests for the per-agent LLM transparency
 * helpers in preferences.js.
 *
 * The Preferences card renders a read-only per-agent rollup and a Coordinator
 * thinking-effort selector. All decisions behind those two surfaces are pure
 * functions so they can be verified without a DOM:
 * - resolveAgentModelRow: override vs inherit, empty-string-as-inherit, thinking
 *   default annotation, provider/base-url detail flag.
 * - buildAgentRollupBadge: header badge text for zero/one/many overrides,
 *   provider-prefix stripping and slug dedup.
 * - buildCoordinatorThinkingPayload: clean vs value vs explicit clear-at-default.
 * - buildAgentTestPayload: per-row POST /llm/test body with global fallbacks.
 */

const assert = require("assert");
const { test, testAsync } = require("./test_helpers.js");

const {
  ROLE_DEFAULT_THINKING,
  AGENT_ROLLUP_ROLES,
  shortAgentModelName,
  resolveAgentModelRow,
  buildAgentRollupBadge,
  buildCoordinatorThinkingPayload,
  buildAgentTestPayload,
} = require("../../zotero-plugin/addon/preferences.js");

const MAIN_MODEL = "openrouter/~z-ai/glm-flash-latest";

async function runTests() {
  console.log("\n--- Suite: Per-Agent LLM Transparency (rollup resolvers & payloads) ---");

  await testAsync("ROLE_DEFAULT_THINKING mirrors the five backend role defaults", () => {
    assert.deepStrictEqual(ROLE_DEFAULT_THINKING, {
      coordinator: "medium",
      library_search: "low",
      library_synthesizer: "high",
      online_search: "low",
      online_synthesizer: "high",
    });
  });

  await testAsync("rollup lists the four sub-agent roles in display order", () => {
    assert.deepStrictEqual(AGENT_ROLLUP_ROLES, [
      "library_search",
      "online_search",
      "library_synthesizer",
      "online_synthesizer",
    ]);
  });

  // --- resolveAgentModelRow -------------------------------------------------
  await testAsync("concrete role model resolves as an override", () => {
    const row = resolveAgentModelRow(
      "library_synthesizer",
      { model: "openrouter/google/gemini-3.8-flash", thinking_level: "high" },
      MAIN_MODEL
    );
    assert.strictEqual(row.model, "openrouter/google/gemini-3.8-flash");
    assert.strictEqual(row.isOverride, true);
    assert.strictEqual(row.hasEndpointOverride, false);
    assert.strictEqual(row.thinking, "high");
    assert.strictEqual(row.isDefaultThinking, true);
  });

  await testAsync("null role model inherits the main model", () => {
    const row = resolveAgentModelRow("library_search", { model: null, thinking_level: null }, MAIN_MODEL);
    assert.strictEqual(row.model, MAIN_MODEL);
    assert.strictEqual(row.isOverride, false);
    assert.strictEqual(row.thinking, "low");
    assert.strictEqual(row.isDefaultThinking, true);
  });

  await testAsync("empty-string and whitespace role models inherit the main model", () => {
    for (const emptyModel of ["", "   "]) {
      const row = resolveAgentModelRow("online_search", { model: emptyModel }, MAIN_MODEL);
      assert.strictEqual(row.isOverride, false, `model ${JSON.stringify(emptyModel)} must inherit`);
      assert.strictEqual(row.model, MAIN_MODEL);
    }
  });

  await testAsync("missing role entry and empty role config inherit the main model", () => {
    for (const roleCfg of [undefined, null, {}]) {
      const row = resolveAgentModelRow("online_synthesizer", roleCfg, MAIN_MODEL);
      assert.strictEqual(row.isOverride, false);
      assert.strictEqual(row.model, MAIN_MODEL);
      assert.strictEqual(row.thinking, "high");
      assert.strictEqual(row.isDefaultThinking, true);
    }
  });

  await testAsync("absent thinking_level falls back to the role default and is annotated", () => {
    const row = resolveAgentModelRow("library_synthesizer", { model: "openrouter/x/y" }, MAIN_MODEL);
    assert.strictEqual(row.thinking, "high");
    assert.strictEqual(row.isDefaultThinking, true);
  });

  await testAsync("explicit non-default thinking_level is not annotated as default", () => {
    const row = resolveAgentModelRow("library_search", { thinking_level: "medium" }, MAIN_MODEL);
    assert.strictEqual(row.thinking, "medium");
    assert.strictEqual(row.isDefaultThinking, false);
  });

  await testAsync("provider-only and base-url-only entries flag an endpoint override", () => {
    assert.strictEqual(resolveAgentModelRow("library_search", { provider: "ollama" }, MAIN_MODEL).hasEndpointOverride, true);
    assert.strictEqual(
      resolveAgentModelRow("library_search", { base_url: "http://localhost:11434" }, MAIN_MODEL).hasEndpointOverride,
      true
    );
    assert.strictEqual(resolveAgentModelRow("library_search", { model: "openrouter/x/y" }, MAIN_MODEL).hasEndpointOverride, false);
  });

  // --- shortAgentModelName / buildAgentRollupBadge --------------------------
  await testAsync("shortAgentModelName strips provider routing prefixes", () => {
    assert.strictEqual(shortAgentModelName("openrouter/google/gemini-3.8-flash"), "google/gemini-3.8-flash");
    assert.strictEqual(shortAgentModelName("ollama_chat/llama3.2"), "llama3.2");
    assert.strictEqual(shortAgentModelName("ollama/llama3.2"), "llama3.2");
    assert.strictEqual(shortAgentModelName("  google/gemini-3.8-flash  "), "google/gemini-3.8-flash");
    assert.strictEqual(shortAgentModelName(null), "");
    assert.strictEqual(shortAgentModelName(undefined), "");
  });

  await testAsync("badge reports all-inherit when no role overrides the model", () => {
    const rows = AGENT_ROLLUP_ROLES.map((role) => resolveAgentModelRow(role, { model: null }, MAIN_MODEL));
    const badge = buildAgentRollupBadge(rows);
    assert.strictEqual(badge.hasOverrides, false);
    assert.strictEqual(badge.text, "No overrides — all agents inherit the main model");
  });

  await testAsync("badge counts a single override with a singular label and short slug", () => {
    const rows = [
      resolveAgentModelRow("library_search", { model: null }, MAIN_MODEL),
      resolveAgentModelRow("library_synthesizer", { model: "openrouter/google/gemini-3.8-flash" }, MAIN_MODEL),
    ];
    const badge = buildAgentRollupBadge(rows);
    assert.strictEqual(badge.hasOverrides, true);
    assert.strictEqual(badge.text, "1 override · google/gemini-3.8-flash");
  });

  await testAsync("badge dedups identical slugs across roles and pluralizes", () => {
    const rows = [
      resolveAgentModelRow("library_synthesizer", { model: "openrouter/google/gemini-3.8-flash" }, MAIN_MODEL),
      resolveAgentModelRow("online_synthesizer", { model: "google/gemini-3.8-flash" }, MAIN_MODEL),
    ];
    const badge = buildAgentRollupBadge(rows);
    assert.strictEqual(badge.text, "2 overrides · google/gemini-3.8-flash");
  });

  await testAsync("badge joins distinct slugs in row order", () => {
    const rows = [
      resolveAgentModelRow("library_synthesizer", { model: "openrouter/google/gemini-3.8-flash" }, MAIN_MODEL),
      resolveAgentModelRow("online_synthesizer", { model: "openrouter/nvidia/nemotron-3.5-super-vl:free" }, MAIN_MODEL),
    ];
    const badge = buildAgentRollupBadge(rows);
    assert.strictEqual(badge.text, "2 overrides · google/gemini-3.8-flash, nvidia/nemotron-3.5-super-vl:free");
  });

  await testAsync("badge tolerates non-array input", () => {
    assert.deepStrictEqual(buildAgentRollupBadge(undefined), {
      text: "No overrides — all agents inherit the main model",
      hasOverrides: false,
    });
  });

  // --- buildCoordinatorThinkingPayload --------------------------------------
  await testAsync("thinking payload is null while the select matches the saved value", () => {
    assert.strictEqual(buildCoordinatorThinkingPayload("medium", "medium", "medium"), null);
    assert.strictEqual(buildCoordinatorThinkingPayload("high", "high", "medium"), null);
  });

  await testAsync("thinking payload carries an explicit value when dirty", () => {
    assert.deepStrictEqual(buildCoordinatorThinkingPayload("high", "medium", "medium"), { thinking_level: "high" });
    assert.deepStrictEqual(buildCoordinatorThinkingPayload("minimal", "low", "medium"), { thinking_level: "minimal" });
  });

  await testAsync("thinking payload clears the entry when set back to the role default", () => {
    assert.deepStrictEqual(buildCoordinatorThinkingPayload("medium", "high", "medium"), { thinking_level: null });
  });

  await testAsync("thinking payload is null for a missing select value", () => {
    assert.strictEqual(buildCoordinatorThinkingPayload("", "high", "medium"), null);
    assert.strictEqual(buildCoordinatorThinkingPayload(null, "high", "medium"), null);
    assert.strictEqual(buildCoordinatorThinkingPayload(undefined, "high", "medium"), null);
  });

  // --- buildAgentTestPayload ------------------------------------------------
  await testAsync("test payload uses the role model and falls back to global provider/base URL", () => {
    const payload = buildAgentTestPayload(
      "library_synthesizer",
      { model: "openrouter/google/gemini-3.8-flash" },
      { llm_provider: "openrouter_llm", llm_base_url: "http://localhost:11434" }
    );
    assert.deepStrictEqual(payload, {
      llm_provider: "openrouter_llm",
      llm_model: "openrouter/google/gemini-3.8-flash",
      llm_base_url: "http://localhost:11434",
      openrouter_api_key: null,
    });
  });

  await testAsync("test payload prefers role provider and base_url over the global values", () => {
    const payload = buildAgentTestPayload(
      "online_synthesizer",
      { model: "qwen3:8b", provider: "ollama", base_url: "http://127.0.0.1:11434" },
      { llm_provider: "openrouter_llm", llm_base_url: "http://localhost:8000/v1" }
    );
    assert.strictEqual(payload.llm_provider, "ollama");
    assert.strictEqual(payload.llm_base_url, "http://127.0.0.1:11434");
    assert.strictEqual(payload.llm_model, "qwen3:8b");
  });

  await testAsync("test payload nulls provider/base URL when neither role nor global defines them", () => {
    const payload = buildAgentTestPayload("library_search", { model: "openrouter/x/y" }, {});
    assert.strictEqual(payload.llm_provider, null);
    assert.strictEqual(payload.llm_base_url, null);
    // The endpoint reads the key from secure storage when the body carries none.
    assert.strictEqual(payload.openrouter_api_key, null);
  });

  await testAsync("test payload forwards the current API key input when present", () => {
    const payload = buildAgentTestPayload(
      "library_search",
      { model: "openrouter/x/y" },
      { llm_provider: "openrouter_llm", openrouter_api_key: "sk-or-test" }
    );
    assert.strictEqual(payload.openrouter_api_key, "sk-or-test");
  });

  await testAsync("test payload keeps a null model when no override is present", () => {
    const payload = buildAgentTestPayload("library_search", { model: null }, { llm_provider: "openrouter_llm" });
    assert.strictEqual(payload.llm_model, null);
  });
}

module.exports = runTests;

if (require.main === module) {
  const { resetTestStats, printTestSummary, getTestStats } = require("./test_helpers.js");
  resetTestStats();
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

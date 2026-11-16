/**
 * test_preferences.js — Tests for preferences.js module.
 *
 * Verifies elimination of frontend dual-write race condition:
 * - loadPreferences is read-only and does NOT write to disk via IOUtils.
 * - Changing settings dispatches to backend /config with Authorization headers
 *   and does NOT write to disk via IOUtils.
 * - Applying converter config calls /rag/converter and does NOT write to disk via IOUtils.
 * - Offline or rejected fetch requests fail gracefully without uncaught rejections.
 */

const assert = require("assert");
const { test, testAsync, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Preferences (Single Authoritative Writer & Read-Only Load) ---");

  const origFetch = global.fetch;
  const origIOUtils = global.IOUtils;
  const origWindowIOUtils = global.window ? global.window.IOUtils : undefined;

  try {
    const env = setupMockEnvironment();

  // Set up IOUtils mock with spy tracking
  const ioCalls = {
    writeUTF8: [],
    readUTF8: [],
    makeDirectory: [],
  };

  const mockIOUtils = {
    async writeUTF8(path, content) {
      ioCalls.writeUTF8.push({ path, content });
    },
    async readUTF8(path) {
      ioCalls.readUTF8.push({ path });
      return "{}";
    },
    async makeDirectory(path, options) {
      ioCalls.makeDirectory.push({ path, options });
    },
  };

  global.IOUtils = mockIOUtils;
  global.window.IOUtils = mockIOUtils;

  // Set up Zotero environment with data directory and auth headers
  env.zotero.DataDirectory = { dir: "/mock/zotero/data" };
  env.zotero.Zocrates = {
    getBackendUrl: () => "http://127.0.0.1:8765",
    getAuthToken: () => "mock-test-auth-token-12345",
    getAuthHeaders: (extra = {}) => ({
      ...extra,
      Authorization: "Bearer mock-test-auth-token-12345",
    }),
  };

  // Track fetch calls
  let fetchCalls = [];
  let fetchHandler = async (url, options = {}) => {
    fetchCalls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
      text: async () => JSON.stringify({ status: "ok" }),
    };
  };

  global.fetch = async (url, options = {}) => {
    return fetchHandler(url, options);
  };

  // Require preferences module
  const { initZocratesPreferences } = require("../../zotero-plugin/addon/preferences.js");

  // Helper to reset spies
  function resetSpies() {
    ioCalls.writeUTF8 = [];
    ioCalls.readUTF8 = [];
    ioCalls.makeDirectory = [];
    fetchCalls = [];
    fetchHandler = async (url, options = {}) => {
      fetchCalls.push({ url: String(url), options });
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "ok" }),
        text: async () => JSON.stringify({ status: "ok" }),
      };
    };
  }

  // 1. Initialize preferences pane
  resetSpies();
  const api = initZocratesPreferences();

  assert.strictEqual(
    ioCalls.writeUTF8.length,
    0,
    "Initial preferences initialization must NOT call IOUtils.writeUTF8"
  );

  // Test 1: loadPreferences does NOT call IOUtils.writeUTF8 or write to disk
  await testAsync("loadPreferences: purely read-only, does NOT call IOUtils.writeUTF8", async () => {
    resetSpies();
    api.loadPreferences();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "loadPreferences() must NOT call IOUtils.writeUTF8 or write to disk"
    );

    // Verify no POST calls to /config were triggered by loadPreferences
    const postConfigCalls = fetchCalls.filter(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.strictEqual(
      postConfigCalls.length,
      0,
      "loadPreferences() must not dispatch POST /config"
    );
  });

  // Test 2: Deprecated saveConfigJson routes to /config and does NOT call IOUtils
  await testAsync("saveConfigJson: deprecated helper routes to /config and avoids IOUtils", async () => {
    resetSpies();

    await api.saveConfigJson({
      citations_enabled: true,
      openrouter_api_key: "should-be-stripped",
      gemini_api_key: "should-be-stripped",
      datalabApiKey: "should-be-stripped",
      oauth_secret: "should-be-stripped",
    });

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "saveConfigJson must NOT call IOUtils.writeUTF8"
    );

    const configPost = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(configPost, "saveConfigJson must route through POST /config");
    assert.strictEqual(
      configPost.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345",
      "Request must carry Authorization header"
    );

    const body = JSON.parse(configPost.options.body);
    assert.strictEqual(body.citations_enabled, true);
    assert.strictEqual(body.openrouter_api_key, undefined);
    assert.strictEqual(body.gemini_api_key, undefined);
    assert.strictEqual(body.datalabApiKey, undefined);
    assert.strictEqual(body.oauth_secret, undefined);
  });

  // Test 3: syncCitationsSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncCitationsSettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const citCheckbox = env.document.getElementById("enable-citations-checkbox");
    const refCheckbox = env.document.getElementById("enable-references-checkbox");
    const mermaidCheckbox = env.document.getElementById("auto-render-mermaid-checkbox");

    citCheckbox.checked = false;
    refCheckbox.checked = true;
    mermaidCheckbox.checked = false;

    await api.syncCitationsSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncCitationsSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncCitationsSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.citations_enabled, false);
    assert.strictEqual(body.references_section_enabled, true);
    assert.strictEqual(body.auto_render_mermaid_diagrams, false);
  });

  // Test 4: syncPredictiveInputSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncPredictiveInputSettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const ghostCheckbox = env.document.getElementById("enable-predictive-ghost-input-checkbox");
    ghostCheckbox.checked = false;

    await api.syncPredictiveInputSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncPredictiveInputSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncPredictiveInputSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.enable_predictive_ghost_input, false);
  });

  // Test 5: syncMcpSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncMcpSettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const mcpCheckbox = env.document.getElementById("enable-mcp-server-checkbox");
    mcpCheckbox.checked = false;

    await api.syncMcpSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncMcpSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncMcpSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.enable_mcp_server, false);
  });

  // Test 6: syncSessionMaintenanceSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncSessionMaintenanceSettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const retentionInput = env.document.getElementById("session-retention-days-input");
    const maxSessionsInput = env.document.getElementById("max-saved-sessions-input");
    const pruneEmptyCheckbox = env.document.getElementById("prune-empty-sessions-checkbox");
    const autoVacuumCheckbox = env.document.getElementById("session-auto-vacuum-checkbox");

    retentionInput.value = "90";
    maxSessionsInput.value = "50";
    pruneEmptyCheckbox.checked = false;
    autoVacuumCheckbox.checked = true;

    await api.syncSessionMaintenanceSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncSessionMaintenanceSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncSessionMaintenanceSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.session_retention_days, 90);
    assert.strictEqual(body.max_saved_sessions, 50);
    assert.strictEqual(body.prune_empty_sessions, false);
    assert.strictEqual(body.session_db_auto_vacuum_on_startup, true);
  });

  // Test 7: syncLiteratureDiscoverySettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncLiteratureDiscoverySettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const emailInput = env.document.getElementById("scholar-polite-email-input");
    const openAlexInput = env.document.getElementById("openalex-api-key-input");

    emailInput.value = "researcher@example.edu";
    openAlexInput.value = "openalex-test-token";

    await api.syncLiteratureDiscoverySettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncLiteratureDiscoverySettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncLiteratureDiscoverySettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.scholar_polite_email, "researcher@example.edu");
    assert.strictEqual(body.openalex_api_key, "openalex-test-token");
  });

  // Test 7b: datalabApiKeyInput auto-syncs key via saveSecureApiKey and postLlmConfig
  await testAsync("datalabApiKeyInput: changing key calls saveSecureApiKey and dispatches postLlmConfig with datalab_api_key", async () => {
    resetSpies();

    const datalabInput = env.document.getElementById("datalab-api-key-input");
    assert.ok(datalabInput, "datalab-api-key-input must exist in document");

    // Track saveStoredApiKey calls via Zotero.Zocrates
    const savedApiKeys = [];
    const origSaveStoredApiKey = env.zotero.Zocrates.saveStoredApiKey;
    env.zotero.Zocrates.saveStoredApiKey = (name, key) => {
      savedApiKeys.push({ name, key });
    };

    try {
      // 1. Test 'change' event
      datalabInput.value = "test-datalab-api-key-999";
      datalabInput.dispatchEvent("change");

      // Await tick for async listener to complete
      await new Promise((r) => setTimeout(r, 10));

      assert.strictEqual(
        ioCalls.writeUTF8.length,
        0,
        "syncDatalabApiKey must NOT call IOUtils.writeUTF8"
      );

      assert.ok(
        savedApiKeys.some((k) => k.name === "datalab_api_key" && k.key === "test-datalab-api-key-999"),
        "saveSecureApiKey must be invoked for datalab_api_key with new value on change"
      );

      const postCall = fetchCalls.find(
        (c) => c.url.includes("/config") && c.options.method === "POST" && c.options.body && JSON.parse(c.options.body).datalab_api_key
      );
      assert.ok(postCall, "postLlmConfig must POST to /config with datalab_api_key on change");
      assert.strictEqual(
        postCall.options.headers.Authorization,
        "Bearer mock-test-auth-token-12345"
      );
      const body = JSON.parse(postCall.options.body);
      assert.strictEqual(body.datalab_api_key, "test-datalab-api-key-999");

      // 2. Test 'blur' event
      resetSpies();
      savedApiKeys.length = 0;

      datalabInput.value = "test-datalab-api-key-blur";
      datalabInput.dispatchEvent("blur");

      await new Promise((r) => setTimeout(r, 10));

      assert.ok(
        savedApiKeys.some((k) => k.name === "datalab_api_key" && k.key === "test-datalab-api-key-blur"),
        "saveSecureApiKey must be invoked for datalab_api_key with new value on blur"
      );
      const postCallBlur = fetchCalls.find(
        (c) => c.url.includes("/config") && c.options.method === "POST" && c.options.body && JSON.parse(c.options.body).datalab_api_key
      );
      assert.ok(postCallBlur, "postLlmConfig must POST to /config with datalab_api_key on blur");
      assert.strictEqual(JSON.parse(postCallBlur.options.body).datalab_api_key, "test-datalab-api-key-blur");
    } finally {
      if (origSaveStoredApiKey) {
        env.zotero.Zocrates.saveStoredApiKey = origSaveStoredApiKey;
      } else {
        delete env.zotero.Zocrates.saveStoredApiKey;
      }
    }
  });

  // Test 8: syncVerificationSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncVerificationSettings: posts to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const toggle = env.document.getElementById("citation-verification-enabled-toggle");
    const provider = env.document.getElementById("verification-provider-select");
    const model = env.document.getElementById("verification-model-select");
    const threshold = env.document.getElementById("verification-threshold-input");
    const autoStart = env.document.getElementById("auto-start-laya-toggle");
    const localUrl = env.document.getElementById("verification-local-url-input");

    toggle.checked = true;
    provider.value = "openrouter";
    model.value = "inception/mercury-decide:free";
    threshold.value = "0.75";
    autoStart.checked = true;
    localUrl.value = "http://127.0.0.1:8005";

    await api.syncVerificationSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncVerificationSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncVerificationSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.citation_verification_enabled, true);
    assert.strictEqual(body.citation_verification_provider, "openrouter");
    assert.strictEqual(body.citation_verification_model, "inception/mercury-decide:free");
    assert.strictEqual(body.citation_verification_threshold, 0.75);
    assert.strictEqual(body.citation_verification_auto_start_local, true);
    assert.strictEqual(body.citation_verification_local_url, "http://127.0.0.1:8005");
  });

  // Test 9: saveLlmConfig dispatches to /config and does NOT call IOUtils.writeUTF8
  await testAsync("saveLlmConfig: posts to /config and avoids IOUtils.writeUTF8", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("llm-provider-select");
    const modelSelect = env.document.getElementById("llm-model-select");
    providerSelect.value = "openrouter_llm";
    modelSelect.value = "openrouter/anthropic/claude-3.5-sonnet";

    await api.saveLlmConfig();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "saveLlmConfig must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "saveLlmConfig must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.llm_provider, "openrouter_llm");
    assert.strictEqual(body.llm_model, "openrouter/anthropic/claude-3.5-sonnet");
  });

  // Test 9b: updateLlmUiVisibility keeps openrouterKeyGroup visible when local provider is selected
  await testAsync("updateLlmUiVisibility: openrouterKeyGroup remains visible when local provider is selected", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("llm-provider-select");
    const openrouterKeyGroup = env.document.getElementById("openrouter-key-group");
    const openrouterKeyMissingNotice = env.document.getElementById("openrouter-key-missing-notice");

    // Test for 'ollama' via change event
    providerSelect.value = "ollama";
    providerSelect.dispatchEvent("change");
    assert.strictEqual(
      openrouterKeyGroup.style.display,
      "flex",
      "openrouterKeyGroup must be visible ('flex') when provider is ollama"
    );
    assert.ok(
      openrouterKeyGroup.style.display !== "none",
      "openrouterKeyGroup.style.display must not be 'none' when provider is ollama"
    );
    assert.strictEqual(
      openrouterKeyMissingNotice.style.display,
      "none",
      "openrouterKeyMissingNotice must be hidden when provider is ollama"
    );

    // Test for 'vllm' via change event
    providerSelect.value = "vllm";
    providerSelect.dispatchEvent("change");
    assert.strictEqual(
      openrouterKeyGroup.style.display,
      "flex",
      "openrouterKeyGroup must be visible ('flex') when provider is vllm"
    );
    assert.ok(
      openrouterKeyGroup.style.display !== "none",
      "openrouterKeyGroup.style.display must not be 'none' when provider is vllm"
    );
    assert.strictEqual(
      openrouterKeyMissingNotice.style.display,
      "none",
      "openrouterKeyMissingNotice must be hidden when provider is vllm"
    );

    // Direct invocation test if exposed on API
    if (typeof api.updateLlmUiVisibility === "function") {
      providerSelect.value = "ollama";
      api.updateLlmUiVisibility();
      assert.strictEqual(openrouterKeyGroup.style.display, "flex");
      assert.ok(openrouterKeyGroup.style.display !== "none");

      providerSelect.value = "vllm";
      api.updateLlmUiVisibility();
      assert.strictEqual(openrouterKeyGroup.style.display, "flex");
      assert.ok(openrouterKeyGroup.style.display !== "none");
    }
  });

  // Test 9b-2: inline OPENROUTER_API_KEY missing notices in LLM and Document Converter cards
  await testAsync("key-missing notices: LLM card and Document Converter VLM show inline warnings when the OpenRouter key is empty", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("llm-provider-select");
    const keyInput = env.document.getElementById("openrouter-api-key-input");
    const enableVlmCheckbox = env.document.getElementById("enable-vlm-captioning-checkbox");
    const vlmProviderOllama = env.document.getElementById("vlm-provider-ollama");
    const llmNotice = env.document.getElementById("llm-key-missing-notice");
    const vlmNotice = env.document.getElementById("vlm-key-missing-notice");
    const verificationProviderSelect = env.document.getElementById("verification-provider-select");
    const verificationNotice = env.document.getElementById("verification-key-missing-notice");

    // OpenRouter provider + empty key: all inline notices must be visible
    providerSelect.value = "openrouter_llm";
    keyInput.value = "";
    enableVlmCheckbox.checked = true;
    api.updateLlmUiVisibility();
    api.updateVlmUiVisibility();
    api.updateVerificationUiVisibility();
    assert.strictEqual(
      llmNotice.style.display,
      "block",
      "LLM card notice must be shown when provider is openrouter_llm and the key is empty"
    );
    assert.strictEqual(
      vlmNotice.style.display,
      "block",
      "Document Converter VLM notice must be shown when VLM provider is openrouter and the key is empty"
    );
    assert.strictEqual(
      verificationNotice.style.display,
      "block",
      "Citation Verification notice must be shown when provider is openrouter and the key is empty"
    );

    // A present key hides all inline notices
    keyInput.value = "sk-or-v1-test-key";
    api.updateLlmUiVisibility();
    api.updateVlmUiVisibility();
    api.updateVerificationUiVisibility();
    assert.strictEqual(
      llmNotice.style.display,
      "none",
      "LLM card notice must be hidden when a key is present"
    );
    assert.strictEqual(
      vlmNotice.style.display,
      "none",
      "Document Converter VLM notice must be hidden when a key is present"
    );
    assert.strictEqual(
      verificationNotice.style.display,
      "none",
      "Citation Verification notice must be hidden when a key is present"
    );

    // Local provider hides the LLM notice; Ollama VLM hides the VLM notice (empty key)
    keyInput.value = "";
    providerSelect.value = "ollama";
    api.updateLlmUiVisibility();
    assert.strictEqual(
      llmNotice.style.display,
      "none",
      "LLM card notice must be hidden when a local provider is selected"
    );
    vlmProviderOllama.checked = true;
    api.updateVlmUiVisibility();
    assert.strictEqual(
      vlmNotice.style.display,
      "none",
      "Document Converter VLM notice must be hidden when the Ollama VLM option is selected"
    );

    // Local Laya provider hides the Citation Verification notice (empty key)
    verificationProviderSelect.value = "local_laya";
    api.updateVerificationUiVisibility();
    assert.strictEqual(
      verificationNotice.style.display,
      "none",
      "Citation Verification notice must be hidden when the Laya local server is selected"
    );

    // Restore defaults
    vlmProviderOllama.checked = false;
    verificationProviderSelect.value = "openrouter";
    providerSelect.value = "openrouter_llm";
  });

  // Test 9c: test-openrouter-key-btn click dispatches to /llm/test_openrouter_key and updates openrouter-key-status-badge
  await testAsync("testOpenRouterKeyBtn: clicking test button dispatches to /llm/test_openrouter_key and updates status badge", async () => {
    resetSpies();
    const testBtn = env.document.getElementById("test-openrouter-key-btn");
    const keyInput = env.document.getElementById("openrouter-api-key-input");
    const badge = env.document.getElementById("openrouter-key-status-badge");

    // 1. When key is blank, shows warning without network call
    keyInput.value = "";
    testBtn.dispatchEvent("click");
    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(badge.className, "zc-text-warning");
    assert.ok(badge.textContent.includes("Please enter an OpenRouter API key"));
    assert.strictEqual(fetchCalls.length, 0);

    // 2. When key is provided, tests key successfully
    keyInput.value = "sk-or-v1-valid-test-key";
    fetchHandler = async (url, options = {}) => {
      fetchCalls.push({ url: String(url), options });
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: "success",
          message: "Connected to OpenRouter successfully. API key is valid. (Limit: $10, Usage: $0.05)",
        }),
      };
    };

    testBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 20));

    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(badge.className, "zc-text-success");
    assert.ok(badge.textContent.includes("Connected to OpenRouter successfully"));
    assert.strictEqual(fetchCalls.length, 1);
    assert.ok(fetchCalls[0].url.includes("/llm/test_openrouter_key"));
    const reqBody = JSON.parse(fetchCalls[0].options.body);
    assert.strictEqual(reqBody.openrouter_api_key, "sk-or-v1-valid-test-key");

    // 3. When backend returns error (e.g. 401 unauthorized)
    fetchCalls = [];
    fetchHandler = async (url, options = {}) => {
      fetchCalls.push({ url: String(url), options });
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: "error",
          message: "OpenRouter API key rejected (401 Unauthorized): Invalid API key",
        }),
      };
    };

    testBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 20));

    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(badge.className, "zc-text-error");
    assert.ok(badge.textContent.includes("401 Unauthorized"));
  });

  // Test 9c: testDatalabKeyBtn tests key and syncs via syncDatalabApiKey on success
  await testAsync("testDatalabKeyBtn: clicking test button dispatches to /converters/test-connection and syncs key on success", async () => {
    resetSpies();

    const datalabInput = env.document.getElementById("datalab-api-key-input");
    const testBtn = env.document.getElementById("test-datalab-key-btn");
    const badge = env.document.getElementById("datalab-key-status-badge");
    assert.ok(testBtn, "test-datalab-key-btn must exist");
    assert.ok(badge, "datalab-key-status-badge must exist");

    datalabInput.value = "test-datalab-valid-key-xyz";

    fetchHandler = async (url, options = {}) => {
      fetchCalls.push({ url: String(url), options });
      if (String(url).includes("/converters/test-connection")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            message: "Connected to Datalab API successfully. API key is valid.",
          }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "ok" }),
      };
    };

    testBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 30));

    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(badge.className, "zc-text-success");
    assert.ok(badge.textContent.includes("Connected to Datalab API successfully"));

    // Verify /converters/test-connection was called
    const testCall = fetchCalls.find((c) => c.url.includes("/converters/test-connection"));
    assert.ok(testCall, "Must call /converters/test-connection");
    const testBody = JSON.parse(testCall.options.body);
    assert.strictEqual(testBody.datalab_api_key, "test-datalab-valid-key-xyz");

    // Verify syncDatalabApiKey synced to /config
    const configCall = fetchCalls.find((c) => c.url.includes("/config") && c.options.method === "POST");
    assert.ok(configCall, "Must sync key to /config on test success");
    const configBody = JSON.parse(configCall.options.body);
    assert.strictEqual(configBody.datalab_api_key, "test-datalab-valid-key-xyz");

    // Reset input for subsequent tests
    datalabInput.value = "";
  });

  // Test 9d: testDatalabKeyBtn with empty key does NOT invoke syncDatalabApiKey on success
  await testAsync("testDatalabKeyBtn: empty key does not call syncDatalabApiKey on success", async () => {
    resetSpies();

    const datalabInput = env.document.getElementById("datalab-api-key-input");
    const testBtn = env.document.getElementById("test-datalab-key-btn");
    const badge = env.document.getElementById("datalab-key-status-badge");
    assert.ok(testBtn, "test-datalab-key-btn must exist");

    datalabInput.value = "";

    fetchHandler = async (url, options = {}) => {
      fetchCalls.push({ url: String(url), options });
      if (String(url).includes("/converters/test-connection")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            message: "Connection test succeeded",
          }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "ok" }),
      };
    };

    testBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 30));

    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(badge.className, "zc-text-success");
    assert.ok(badge.textContent.includes("Connection test succeeded"));

    // Verify /config was NOT called to sync key
    const configCall = fetchCalls.find((c) => c.url.includes("/config") && c.options.method === "POST");
    assert.strictEqual(configCall, undefined, "Must NOT sync empty key to /config");
  });

  // Test 10: Applying converter config calls /rag/converter and does NOT call IOUtils.writeUTF8
  await testAsync("applyConverterConfig: posts to /rag/converter and does NOT call IOUtils.writeUTF8", async () => {
    resetSpies();

    const converterSelect = env.document.getElementById("document-converter-select");
    const applyBtn = env.document.getElementById("document-converter-apply-btn");
    converterSelect.value = "docling";
    converterSelect.dispatchEvent("change");

    // Trigger click on converterApplyBtn
    applyBtn.dispatchEvent("click");

    // Allow async handlers to settle
    await new Promise((resolve) => setTimeout(resolve, 50));

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "Applying converter config must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/rag/converter") && c.options.method === "POST"
    );
    assert.ok(postCall, "Applying converter config must POST to /rag/converter");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.document_converter_id, "docling");
    assert.strictEqual(body.document_converter_mode, "local");
    assert.strictEqual(body.datalab_merge_cross_page, false);
    assert.strictEqual(body.datalab_api_key, undefined, "Empty datalab_api_key must be undefined in /rag/converter payload");
  });

  // Test 10b: Applying converter config with datalab_merge_cross_page toggled
  await testAsync("applyConverterConfig: serializes datalab_merge_cross_page in payload", async () => {
    resetSpies();

    const converterSelect = env.document.getElementById("document-converter-select");
    const applyBtn = env.document.getElementById("document-converter-apply-btn");
    const mergeCrossPageCheckbox = env.document.getElementById("datalab-merge-cross-page-checkbox");

    converterSelect.value = "datalab";
    converterSelect.dispatchEvent("change");

    mergeCrossPageCheckbox.checked = true;
    mergeCrossPageCheckbox.dispatchEvent("change");

    applyBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/rag/converter") && c.options.method === "POST"
    );
    assert.ok(postCall, "Applying converter config must POST to /rag/converter");
    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.document_converter_id, "datalab");
    assert.strictEqual(body.document_converter_mode, "cloud");
    assert.strictEqual(body.datalab_merge_cross_page, true);

    // Reset back to docling for subsequent tests
    converterSelect.value = "docling";
    converterSelect.dispatchEvent("change");
    mergeCrossPageCheckbox.checked = false;
    mergeCrossPageCheckbox.dispatchEvent("change");
    applyBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));
  });

  // Test 11: Offline or rejected fetch requests fail gracefully without uncaught rejections
  await testAsync("offline error resilience: network throw fails gracefully without uncaught rejection", async () => {
    resetSpies();

    // Mock fetch throwing network offline error
    fetchHandler = async () => {
      throw new TypeError("Failed to fetch: ECONNREFUSED 127.0.0.1:8765");
    };

    let postLlmResult = null;
    let threw = false;
    try {
      postLlmResult = await api.postLlmConfig({ citations_enabled: true });
    } catch (e) {
      threw = true;
    }
    assert.strictEqual(threw, false, "postLlmConfig must not throw on network failure");
    assert.strictEqual(postLlmResult, null, "postLlmConfig should return null on error");

    // Test that sync functions do not throw when network is offline
    await api.syncCitationsSettings();
    await api.syncPredictiveInputSettings();
    await api.syncMcpSettings();
    await api.syncSessionMaintenanceSettings();
    await api.syncLiteratureDiscoverySettings();
    await api.syncDatalabApiKey();
    await api.syncVerificationSettings();
    await api.postConverterChange({ document_converter_id: "markitdown" });
    await api.saveConfigJson({ citations_enabled: false });

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "IOUtils must never be called even during network failures"
    );
  });

  // Test 12: HTTP error responses fail gracefully without throwing
  await testAsync("HTTP error resilience: HTTP 500/400 responses handled gracefully", async () => {
    resetSpies();

    fetchHandler = async () => ({
      ok: false,
      status: 500,
      json: async () => ({ detail: "Internal Server Error" }),
      text: async () => JSON.stringify({ detail: "Internal Server Error" }),
    });

    let threw = false;
    try {
      const res = await api.postLlmConfig({ citations_enabled: true });
      assert.strictEqual(res.ok, false);
      assert.strictEqual(res.status, 500);

      const convRes = await api.postConverterChange({ document_converter_id: "markitdown" });
      assert.strictEqual(convRes.ok, false);
      assert.strictEqual(convRes.status, 500);

      await api.syncCitationsSettings();
      await api.saveConfigJson({ test: 123 });
    } catch (e) {
      threw = true;
    }

    assert.strictEqual(threw, false, "HTTP error responses must not throw uncaught exceptions");
    assert.strictEqual(ioCalls.writeUTF8.length, 0, "No IOUtils calls on HTTP errors");
  });

  // Test 13: syncResearchProfileSettings dispatches to /config with Auth header and no IOUtils write
  await testAsync("syncResearchProfileSettings: posts user_research_profile to /config with auth and zero IOUtils writes", async () => {
    resetSpies();

    const profileTextarea = env.document.getElementById("zocrates-research-profile");
    profileTextarea.value = "Expert biophysics researcher focused on molecular dynamics.";

    await api.syncResearchProfileSettings();

    assert.strictEqual(
      ioCalls.writeUTF8.length,
      0,
      "syncResearchProfileSettings must NOT call IOUtils.writeUTF8"
    );

    const postCall = fetchCalls.find(
      (c) => c.url.includes("/config") && c.options.method === "POST"
    );
    assert.ok(postCall, "syncResearchProfileSettings must POST to /config");
    assert.strictEqual(
      postCall.options.headers.Authorization,
      "Bearer mock-test-auth-token-12345"
    );

    const body = JSON.parse(postCall.options.body);
    assert.strictEqual(
      body.user_research_profile,
      "Expert biophysics researcher focused on molecular dynamics."
    );
  });

  // Test 14: applyConverterConfig displays error badge on network / backend failure
  await testAsync("applyConverterConfig failure: displays error status badge and leaves dirty state", async () => {
    resetSpies();

    fetchHandler = async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Service Unavailable" }),
      text: async () => JSON.stringify({ detail: "Service Unavailable" }),
    });

    const converterSelect = env.document.getElementById("document-converter-select");
    const applyBtn = env.document.getElementById("document-converter-apply-btn");
    const badge = env.document.getElementById("document-converter-status-badge");
    converterSelect.value = "datalab";
    converterSelect.dispatchEvent("change");

    applyBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    assert.ok(badge.textContent.includes("Failed to apply converter settings"));
    assert.strictEqual(badge.style.display, "block");
    assert.strictEqual(ioCalls.writeUTF8.length, 0, "No IOUtils writes on converter failure");
  });

  // Helper to parse MCP snippet config JSON
  function parseSnippetConfig(snippetText) {
    const match = snippetText.match(/^"zocrates":\s*({[\s\S]*})$/);
    assert.ok(match, "Snippet must match '\"zocrates\": { ... }' format");
    return JSON.parse(match[1]);
  }

  // Test 15: updateMcpConfigSnippet dynamic directory resolution (explicit projectDir + savedProjectDir persistence)
  test("updateMcpConfigSnippet: dynamic directory resolution with explicit projectDir and savedProjectDir persistence", () => {
    const snippetEl = env.document.getElementById("mcp-config-snippet");

    // 1. Call with explicit projectDir
    api.updateMcpConfigSnippet("/home/user/my-project");
    let parsed = parseSnippetConfig(snippetEl.textContent);
    assert.strictEqual(parsed.command, "uv");
    assert.strictEqual(parsed.args[0], "--directory");
    assert.strictEqual(parsed.args[1], "/home/user/my-project");
    assert.strictEqual(parsed.args[2], "run");
    assert.strictEqual(parsed.args[3], "python");
    assert.strictEqual(parsed.args[4], "-m");
    assert.strictEqual(parsed.args[5], "zocrates.mcp");
    assert.strictEqual(parsed.env.ZOCRATES_BACKEND_URL, "http://127.0.0.1:8765");
    assert.strictEqual(parsed.env.ZOCRATES_AUTH_TOKEN, "mock-test-auth-token-12345");

    // 2. Subsequent call without arguments reuses savedProjectDir
    api.updateMcpConfigSnippet();
    parsed = parseSnippetConfig(snippetEl.textContent);
    assert.strictEqual(parsed.args[1], "/home/user/my-project", "Subsequent call must reuse savedProjectDir");
  });

  // Test 16: updateMcpConfigSnippet resolves backendSourceDir preference when projectDir and savedProjectDir are empty
  test("updateMcpConfigSnippet: resolves backendSourceDir preference when projectDir and savedProjectDir are empty", () => {
    const snippetEl = env.document.getElementById("mcp-config-snippet");
    // Set backendSourceDir preference
    env.zotero.Prefs.set("extensions.zocrates.backendSourceDir", "/custom/dev/source/path", true);

    // Fresh instance so savedProjectDir starts empty
    const freshApi = initZocratesPreferences();
    freshApi.updateMcpConfigSnippet();

    const parsed = parseSnippetConfig(snippetEl.textContent);
    assert.strictEqual(parsed.args[1], "/custom/dev/source/path", "Must resolve backendSourceDir from Zotero.Prefs");

    // Clean up pref
    env.zotero.Prefs.clear("extensions.zocrates.backendSourceDir", true);
  });

  // Test 17: updateMcpConfigSnippet falls back to /path/to/zocrates without hardcoded developer paths
  test("updateMcpConfigSnippet: falls back to /path/to/zocrates when projectDir, savedProjectDir, and pref are empty", () => {
    const snippetEl = env.document.getElementById("mcp-config-snippet");
    // Ensure dev source prefs are absent
    env.zotero.Prefs.clear("extensions.zocrates.backendSourceDir", true);
    env.zotero.Prefs.clear("extensions.zocrates.devSourceDir", true);

    // Fresh instance so savedProjectDir starts empty
    const freshApi = initZocratesPreferences();
    freshApi.updateMcpConfigSnippet();

    const parsed = parseSnippetConfig(snippetEl.textContent);
    assert.strictEqual(parsed.args[1], "/path/to/zocrates", "Must fall back to /path/to/zocrates");
    assert(!snippetEl.textContent.includes("/home/alex"), "Snippet must NEVER contain hardcoded developer paths");
  });

  // Helper to mock navigator.clipboard safely in Node.js environments
  function setMockClipboard(writeTextFn) {
    const origDesc = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    Object.defineProperty(globalThis, "navigator", {
      value: {
        clipboard: {
          writeText: writeTextFn,
        },
      },
      configurable: true,
      writable: true,
    });
    if (global.window) {
      global.window.navigator = globalThis.navigator;
    }
    return () => {
      if (origDesc) {
        Object.defineProperty(globalThis, "navigator", origDesc);
      } else {
        delete globalThis.navigator;
      }
      if (global.window) {
        delete global.window.navigator;
      }
    };
  }

  // Test 18: copyTextToClipboard tier 1 succeeds via navigator.clipboard.writeText
  await testAsync("copyTextToClipboard: tier 1 succeeds via navigator.clipboard.writeText", async () => {
    let copiedText = "";
    const restoreClipboard = setMockClipboard(async (t) => {
      copiedText = t;
    });

    try {
      const res = await api.copyTextToClipboard("Hello Zocrates MCP");
      assert.strictEqual(res, true, "Must return true on navigator.clipboard success");
      assert.strictEqual(copiedText, "Hello Zocrates MCP");
    } finally {
      restoreClipboard();
    }
  });

  // Test 19: copyTextToClipboard tier 2 succeeds via Zotero.Utilities.Internal when navigator.clipboard throws
  await testAsync("copyTextToClipboard: tier 2 succeeds via Zotero.Utilities.Internal when navigator.clipboard throws", async () => {
    const origUtilities = env.zotero.Utilities;
    let zotCopiedText = "";

    const restoreClipboard = setMockClipboard(async () => {
      throw new Error("Clipboard permission denied in Gecko");
    });
    env.zotero.Utilities = {
      Internal: {
        copyTextToClipboard: (t) => {
          zotCopiedText = t;
        },
      },
    };

    try {
      const res = await api.copyTextToClipboard("Zotero XPCOM copy text");
      assert.strictEqual(res, true, "Must return true when Zotero internal clipboard succeeds");
      assert.strictEqual(zotCopiedText, "Zotero XPCOM copy text");
    } finally {
      restoreClipboard();
      env.zotero.Utilities = origUtilities;
    }
  });

  // Test 20: copyTextToClipboard tier 3 succeeds via textarea + document.execCommand with safe cleanup in finally
  await testAsync("copyTextToClipboard: tier 3 succeeds via textarea + document.execCommand with DOM cleanup in finally", async () => {
    const origUtilities = env.zotero.Utilities;
    const origExecCommand = env.document.execCommand;

    // Force tier 1 to fail and tier 2 to be absent
    const restoreClipboard = setMockClipboard(async () => {
      throw new Error("Tier 1 unavailable");
    });
    delete env.zotero.Utilities;

    let execCmdCalled = null;
    env.document.execCommand = (cmd) => {
      execCmdCalled = cmd;
      return true;
    };

    try {
      const initialChildCount = env.document.body.children.length;
      const res = await api.copyTextToClipboard("Fallback DOM copy text");
      assert.strictEqual(res, true, "Must return true on successful execCommand");
      assert.strictEqual(execCmdCalled, "copy");
      assert.strictEqual(
        env.document.body.children.length,
        initialChildCount,
        "Temporary textarea must be cleanly removed from DOM in finally block"
      );
    } finally {
      restoreClipboard();
      env.zotero.Utilities = origUtilities;
      env.document.execCommand = origExecCommand;
    }
  });

  // Test 21: copyTextToClipboard returns false and cleans up DOM when all tiers fail or throw
  await testAsync("copyTextToClipboard: returns false and cleans up DOM when all tiers fail or throw", async () => {
    const origUtilities = env.zotero.Utilities;
    const origExecCommand = env.document.execCommand;

    const restoreClipboard = setMockClipboard(async () => {
      throw new Error("Tier 1 unavailable");
    });
    delete env.zotero.Utilities;

    env.document.execCommand = () => {
      throw new Error("execCommand: command disabled");
    };

    try {
      const initialChildCount = env.document.body.children.length;
      const res = await api.copyTextToClipboard("Fail text");
      assert.strictEqual(res, false, "Must return false when all clipboard tiers fail");
      assert.strictEqual(
        env.document.body.children.length,
        initialChildCount,
        "DOM must be cleaned up in finally even when execCommand throws"
      );
    } finally {
      restoreClipboard();
      env.zotero.Utilities = origUtilities;
      env.document.execCommand = origExecCommand;
    }
  });

  // Test 22: copyMcpConfigBtn click handler invokes copyTextToClipboard and shows feedback
  await testAsync("copyMcpConfigBtn: click handler invokes copyTextToClipboard and toggles feedback text", async () => {
    let copiedPayload = "";
    const restoreClipboard = setMockClipboard(async (t) => {
      copiedPayload = t;
    });

    const copyBtn = env.document.getElementById("copy-mcp-config-btn");
    const snippetEl = env.document.getElementById("mcp-config-snippet");
    snippetEl.textContent = '"zocrates": { "test": "content" }';

    try {
      copyBtn.dispatchEvent("click");
      await new Promise((resolve) => setTimeout(resolve, 50));

      assert.strictEqual(copiedPayload, '"zocrates": { "test": "content" }');
      assert.strictEqual(copyBtn.textContent, "✅ Copied!");
    } finally {
      restoreClipboard();
    }
  });

  // Test 23: copyTextToClipboard rejects empty/null input and copy failure preserves button label
  await testAsync("copyTextToClipboard: empty input returns false and copy failure preserves button label", async () => {
    const resNull = await api.copyTextToClipboard(null);
    assert.strictEqual(resNull, false, "copyTextToClipboard(null) must return false");
    const resEmpty = await api.copyTextToClipboard("");
    assert.strictEqual(resEmpty, false, "copyTextToClipboard('') must return false");

    // Simulate failed copy on button click
    const restoreClipboard = setMockClipboard(async () => {
      throw new Error("Clipboard disabled");
    });
    const origExecCommand = env.document.execCommand;
    env.document.execCommand = () => false;

    const copyBtn = env.document.getElementById("copy-mcp-config-btn");
    copyBtn.textContent = "📋 Copy Config";

    try {
      copyBtn.dispatchEvent("click");
      await new Promise((resolve) => setTimeout(resolve, 50));
      assert.strictEqual(copyBtn.textContent, "📋 Copy Config", "Button label must not flip to copied when copy fails");
    } finally {
      restoreClipboard();
      env.document.execCommand = origExecCommand;
    }
  });

  // Test 24: refresh-verification-models-btn fetches decision models and populates select
  await testAsync("refresh-verification-models-btn: fetches decision models and dynamically populates verification-model-select", async () => {
    resetSpies();

    const refreshBtn = env.document.getElementById("refresh-verification-models-btn");
    const modelSelect = env.document.getElementById("verification-model-select");
    modelSelect.value = "typesafe/jev-1.14";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "typesafe/jev-1.14", name: "TypeSafe: Jev 1.14", context_length: 32768 },
              { id: "upstage/solar-decide", name: "Upstage: Solar Decide", context_length: 524288 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    refreshBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    const options = modelSelect.options || [];
    assert.strictEqual(options.length, 2);
    assert.strictEqual(options[0].value, "typesafe/jev-1.14");
    assert.ok(options[0].textContent.includes("32k Context"));
    assert.strictEqual(options[1].value, "upstage/solar-decide");
    assert.ok(options[1].textContent.includes("512k Context"));
  });

  // Test 24b: renderDecisionModels does not add "laya" to verification-model-select when activeModelToSelect is "laya"
  await testAsync("renderDecisionModels: does not add 'laya' to verification-model-select when activeModelToSelect is 'laya'", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("verification-model-select");

    // 1. When rawDecisionModels includes inception/mercury-decide
    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "inception/mercury-decide", name: "Inception: Mercury Decide", context_length: 32768 },
              { id: "upstage/solar-decide", name: "Upstage: Solar Decide", context_length: 524288 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("decision", true, "laya");
    api.renderDecisionModels("laya");

    let options = modelSelect.options || modelSelect.children || [];
    for (let i = 0; i < options.length; i++) {
      assert.notStrictEqual(options[i].value, "laya", "verificationModelSelect options must never contain 'laya'");
    }
    assert.notStrictEqual(modelSelect.value, "laya", "verificationModelSelect.value must not be 'laya'");
    assert.strictEqual(modelSelect.value, "inception/mercury-decide");

    // 2. When rawDecisionModels does NOT include inception/mercury-decide
    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "typesafe/jev-1.14", name: "TypeSafe: Jev 1.14", context_length: 32768 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("decision", true, "laya");
    api.renderDecisionModels("laya");

    options = modelSelect.options || modelSelect.children || [];
    for (let i = 0; i < options.length; i++) {
      assert.notStrictEqual(options[i].value, "laya", "verificationModelSelect options must never contain 'laya' when not in catalog");
    }
    assert.notStrictEqual(modelSelect.value, "laya");
    assert.strictEqual(modelSelect.value, "inception/mercury-decide");
  });

  // Test 24c: loadPreferences and fetchStatus do not assign 'laya' to verificationModelSelect.value
  await testAsync("loadPreferences and fetchStatus: do not assign 'laya' to verificationModelSelect.value", async () => {
    resetSpies();
    const modelSelect = env.document.getElementById("verification-model-select");
    modelSelect.value = "typesafe/jev-1.14";

    // Set Zotero pref to "laya"
    env.zotero.Prefs.set("extensions.zocrates.citationVerificationModel", "laya");
    api.loadPreferences();
    assert.strictEqual(modelSelect.value, "typesafe/jev-1.14", "loadPreferences must not set verificationModelSelect.value to 'laya'");

    // fetchStatus with data.citation_verification_model = "laya"
    fetchHandler = async (url) => {
      if (String(url).includes("/status")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "ok",
            citation_verification_model: "laya",
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchStatus();
    assert.strictEqual(modelSelect.value, "typesafe/jev-1.14", "fetchStatus must not set verificationModelSelect.value to 'laya'");

    // Restore pref hygiene
    env.zotero.Prefs.set("extensions.zocrates.citationVerificationModel", "inception/mercury-decide");
  });

  // Test 24d: syncVerificationSettings handles local_laya and openrouter with laya fallback
  await testAsync("syncVerificationSettings: handles local_laya model as 'laya' and openrouter falls back from 'laya'", async () => {
    resetSpies();

    const provider = env.document.getElementById("verification-provider-select");
    const model = env.document.getElementById("verification-model-select");

    // Case 1: local_laya sets model to "laya"
    provider.value = "local_laya";
    model.value = "typesafe/jev-1.14";
    await api.syncVerificationSettings();

    let postCall = fetchCalls.find((c) => c.url.includes("/config") && c.options.method === "POST");
    assert.ok(postCall);
    let body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.citation_verification_model, "laya");

    // Case 2: openrouter with model set to "laya" falls back to "inception/mercury-decide"
    resetSpies();
    provider.value = "openrouter";
    model.value = "laya";
    await api.syncVerificationSettings();

    postCall = fetchCalls.find((c) => c.url.includes("/config") && c.options.method === "POST");
    assert.ok(postCall);
    body = JSON.parse(postCall.options.body);
    assert.strictEqual(body.citation_verification_model, "inception/mercury-decide");
  });

  // Test 25: Laya server status and polling
  await testAsync("checkLayaStatus: shows starting server badge when daemon_starting is true", async () => {
    resetSpies();

    const pill = env.document.getElementById("laya-status-pill");
    const startBtn = env.document.getElementById("start-laya-btn");

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/decision_tool/status")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            tool_installed: true,
            server_running: false,
            daemon_managed: true,
            daemon_starting: true,
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    // Trigger status check via provider select change
    const providerSelect = env.document.getElementById("verification-provider-select");
    providerSelect.value = "local_laya";
    providerSelect.dispatchEvent("change");
    await new Promise((resolve) => setTimeout(resolve, 50));

    assert.ok(pill.textContent.includes("Starting server / loading model"));
    assert.strictEqual(startBtn.disabled, true);
  });

  // Test 26: testDecisionModelConnection calls checkLayaStatus on local_laya success
  await testAsync("testDecisionModelConnection: triggers checkLayaStatus on success with local_laya", async () => {
    resetSpies();

    const testBtn = env.document.getElementById("verification-test-btn");
    const providerSelect = env.document.getElementById("verification-provider-select");
    const pill = env.document.getElementById("laya-status-pill");
    providerSelect.value = "local_laya";

    let statusChecked = false;
    fetchHandler = async (url) => {
      const urlStr = String(url);
      if (urlStr.includes("/llm/test_decision_model")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "ok", message: "Server running" }),
        };
      }
      if (urlStr.includes("/llm/decision_tool/status")) {
        statusChecked = true;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            tool_installed: true,
            server_running: true,
            active_device: "CPU",
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    testBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    assert.strictEqual(statusChecked, true, "checkLayaStatus must be called on test success");
    assert.ok(pill.textContent.includes("Running (CPU)"));
  });

  // Test 27: decision model metadata box rendering, context, modalities, and input-only pricing
  await testAsync("decision model metadata box: renders modalities, context, and input-only pricing", async () => {
    resetSpies();

    const refreshBtn = env.document.getElementById("refresh-verification-models-btn");
    const modelSelect = env.document.getElementById("verification-model-select");
    const providerSelect = env.document.getElementById("verification-provider-select");
    const metadataBox = env.document.getElementById("verification-model-metadata-box");

    providerSelect.value = "openrouter";
    modelSelect.value = "typesafe/jev-1.14";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              {
                id: "typesafe/jev-1.14",
                name: "TypeSafe: Jev 1.14",
                context_length: 32768,
                architecture: { input_modalities: ["text"], output_modalities: ["decisions"] },
                pricing: { prompt: "0.0000015", completion: "0.000005" },
              },
              {
                id: "free/decider",
                name: "Free Decider",
                context_length: 16384,
                is_free: true,
                architecture: { input_modalities: ["text", "image"] },
                pricing: { prompt: "0", request: "0" },
              },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    refreshBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    // 1. Paid model metadata checks
    assert.strictEqual(metadataBox.style.display, "block");
    assert.ok(metadataBox.innerHTML.includes("Modalities: text ➔ decisions"));
    assert.ok(metadataBox.innerHTML.includes("Context: 32,768 tokens"));
    assert.ok(metadataBox.innerHTML.includes("Pricing: $1.50 / 1M input"));
    assert.ok(!metadataBox.innerHTML.includes("completion"), "Decision models must not show completion pricing");

    // 2. Select free model with multi-modal input
    modelSelect.value = "free/decider";
    modelSelect.dispatchEvent("change");

    assert.strictEqual(metadataBox.style.display, "block");
    assert.ok(metadataBox.innerHTML.includes("Modalities: text, image ➔ decisions"));
    assert.ok(metadataBox.innerHTML.includes("Context: 16,384 tokens"));
    assert.ok(metadataBox.innerHTML.includes("Pricing: Free"));
  });

  // Test 28: decision model metadata box hides when provider is local_laya
  await testAsync("decision model metadata box: hides when provider is local_laya and restores on openrouter", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("verification-provider-select");
    const metadataBox = env.document.getElementById("verification-model-metadata-box");

    // Ensure it's currently showing
    providerSelect.value = "openrouter";
    api.updateVerificationModelMetadata("typesafe/jev-1.14");
    assert.strictEqual(metadataBox.style.display, "block");

    // Switch provider to local_laya
    providerSelect.value = "local_laya";
    providerSelect.dispatchEvent("change");

    assert.strictEqual(metadataBox.style.display, "none");

    // Calling updateVerificationModelMetadata while local_laya remains hidden
    api.updateVerificationModelMetadata();
    assert.strictEqual(metadataBox.style.display, "none");

    // Switch back to openrouter
    providerSelect.value = "openrouter";
    providerSelect.dispatchEvent("change");
    assert.strictEqual(metadataBox.style.display, "block");
  });

  // Test 29: updateLlmModelMetadata produces safe DOM structure without stray semicolons
  await testAsync("llm model metadata box: renders safe DOM structure without stray semicolons and handles context lengths", async () => {
    resetSpies();

    const metadataBox = env.document.getElementById("llm-model-metadata-box");
    const modelSelect = env.document.getElementById("llm-model-select");

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              {
                id: "anthropic/claude-3.5-sonnet",
                name: "Anthropic: Claude 3.5 Sonnet",
                context_length: 200000,
                architecture: { input_modalities: ["text", "image"], output_modalities: ["text"] },
                pricing: { prompt: "0.000003", completion: "0.000015", image: "0.0048" },
              },
              {
                id: "test/zero-context-free",
                name: "Zero Context Free Model",
                context_length: 0,
                is_free: true,
                architecture: { input_modalities: ["text"], output_modalities: ["text"] },
                pricing: { prompt: "0", completion: "0" },
              },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    // Populate LLM models via fetchOpenRouterModels
    await api.fetchOpenRouterModels("llm", true);

    // 1. Paid multimodal model
    api.updateLlmModelMetadata("anthropic/claude-3.5-sonnet");

    assert.strictEqual(metadataBox.style.display, "block");
    const container = metadataBox.children[0];
    assert.ok(container, "Metadata container element must exist");
    assert.strictEqual(container.children.length, 3, "Container must have 3 rows");

    // Row 1: Modalities in bold
    const row1 = container.children[0];
    const bold1 = row1.children[0];
    assert.strictEqual(bold1.tagName.toLowerCase(), "b");
    assert.strictEqual(bold1.textContent, "Modalities: text, image ➔ text");

    // Row 2: Context length formatted
    const row2 = container.children[1];
    assert.strictEqual(row2.textContent, "Context: 200,000 tokens");

    // Row 3: Pricing
    const row3 = container.children[2];
    assert.ok(row3.textContent.includes("$3.00 / 1M prompt"));
    assert.ok(row3.textContent.includes("$15.00 / 1M completion"));
    assert.ok(row3.textContent.includes("$0.005 / image"));

    // Ensure safe DOM structure without raw HTML trailing semicolons
    assert.ok(!metadataBox.innerHTML.includes(" ;"), "DOM innerHTML must not contain stray semicolons");
    assert.ok(!metadataBox.innerHTML.includes(";</b>"), "Bold tag must not contain trailing semicolon");
    assert.ok(!row1.textContent.includes(";"), "Row 1 text must not contain stray semicolon");
    assert.ok(!row2.textContent.includes(";"), "Row 2 text must not contain stray semicolon");

    // 2. Zero context / free model
    api.updateLlmModelMetadata("test/zero-context-free");
    assert.strictEqual(metadataBox.style.display, "block");
    const container2 = metadataBox.children[0];
    assert.strictEqual(container2.children[1].textContent, "Context: Unknown");
    assert.strictEqual(container2.children[2].textContent, "Pricing: Free");
    assert.ok(!metadataBox.innerHTML.includes(" ;"), "Zero context DOM innerHTML must not contain stray semicolons");

    // 3. Fallback to modelSelect.value when selectedId is omitted
    modelSelect.value = "anthropic/claude-3.5-sonnet";
    api.updateLlmModelMetadata();
    assert.strictEqual(metadataBox.style.display, "block");
    assert.strictEqual(metadataBox.children[0].children[1].textContent, "Context: 200,000 tokens");

    // 4. Missing / non-existent model hides metadata box
    api.updateLlmModelMetadata("nonexistent-model");
    assert.strictEqual(metadataBox.style.display, "none");
  });

  // Test 30: no_key catalog response materializes the configured model option
  await testAsync("fetchOpenRouterModels: no_key response appends and selects the configured model option", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("llm-model-select");
    // Simulate the pristine static XHTML placeholder state (no catalog rendering yet).
    modelSelect.innerHTML = "";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models")) {
        return { ok: true, status: 200, json: async () => ({ status: "no_key", models: [] }) };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    let thrown = null;
    try {
      await api.fetchOpenRouterModels("llm", false, "openrouter/nvidia/nemotron-3.5-lightning:free");
    } catch (err) {
      thrown = err;
    }

    assert.strictEqual(thrown, null, "no_key catalog response must not throw");
    assert.strictEqual(modelSelect.options.length, 1, "Exactly one fallback option must be appended");
    assert.strictEqual(
      modelSelect.options[0].value,
      "openrouter/nvidia/nemotron-3.5-lightning:free",
      "Fallback option value must equal the configured model"
    );
    assert.strictEqual(
      modelSelect.options[0].textContent,
      "nvidia/nemotron-3.5-lightning:free",
      "Fallback option label must be the slug without the openrouter/ prefix"
    );
    assert.strictEqual(
      modelSelect.value,
      "openrouter/nvidia/nemotron-3.5-lightning:free",
      "Fallback option must be selected"
    );
  });

  // Test 31: no_key for all three catalogs leaves every card's dirty state clean
  await testAsync("fetchLlmConfig: no_key catalogs keep LLM, embedding and verification dirty state clean", async () => {
    resetSpies();

    const llmModelSelect = env.document.getElementById("llm-model-select");
    const embeddingModelSelect = env.document.getElementById("embedding-model-select");
    const verificationModelSelect = env.document.getElementById("verification-model-select");
    llmModelSelect.innerHTML = "";
    embeddingModelSelect.innerHTML = "";
    verificationModelSelect.innerHTML = "";

    const llmApplyBtn = env.document.getElementById("llm-apply-btn");
    const embeddingApplyBtn = env.document.getElementById("embedding-apply-btn");
    const verificationApplyBtn = env.document.getElementById("verification-apply-btn");
    const llmWarning = env.document.getElementById("llm-unsaved-warning");
    const embeddingWarning = env.document.getElementById("embedding-unsaved-warning");
    const verificationWarning = env.document.getElementById("verification-unsaved-warning");

    // Start dirty so the assertions prove the checks actually re-evaluate.
    llmApplyBtn.disabled = false;
    embeddingApplyBtn.disabled = false;
    verificationApplyBtn.disabled = false;
    llmWarning.style.display = "block";
    embeddingWarning.style.display = "block";
    verificationWarning.style.display = "block";

    fetchHandler = async (url) => {
      const urlStr = String(url);
      if (urlStr.includes("/llm/openrouter_models")) {
        return { ok: true, status: 200, json: async () => ({ status: "no_key", models: [] }) };
      }
      if (urlStr.includes("/config")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            llm_provider: "openrouter_llm",
            llm_model: "openrouter/nvidia/nemotron-3.5-lightning:free",
            embedding_provider: "cloud",
            embedding_model: "openrouter/nvidia/nemotron-3-embed-1b:free",
            citation_verification_enabled: true,
            citation_verification_provider: "openrouter",
            citation_verification_model: "typesafe/jev-1.14",
            citation_verification_threshold: 0.75,
            citation_verification_auto_start_local: true,
            has_openrouter_key: false,
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchLlmConfig();

    assert.strictEqual(
      llmModelSelect.value,
      "openrouter/nvidia/nemotron-3.5-lightning:free",
      "LLM select must reflect the configured model without a catalog"
    );
    assert.strictEqual(llmApplyBtn.disabled, true, "LLM Apply must be disabled when nothing changed");
    assert.strictEqual(llmWarning.style.display, "none", "LLM unsaved warning must be hidden");

    assert.strictEqual(
      embeddingModelSelect.value,
      "openrouter/nvidia/nemotron-3-embed-1b:free",
      "Embedding select must reflect the configured model without a catalog"
    );
    assert.strictEqual(embeddingApplyBtn.disabled, true, "Embedding Apply must be disabled when nothing changed");
    assert.strictEqual(embeddingWarning.style.display, "none", "Embedding unsaved warning must be hidden");

    assert.strictEqual(
      verificationModelSelect.value,
      "typesafe/jev-1.14",
      "Verification select must reflect the configured model without a catalog"
    );
    assert.strictEqual(verificationApplyBtn.disabled, true, "Verification Apply must be disabled when nothing changed");
    assert.strictEqual(verificationWarning.style.display, "none", "Verification unsaved warning must be hidden");
  });

  // Test 32: verification comparator is prefix-tolerant like the LLM card
  await testAsync("updateVerificationDirtyState: openrouter/ prefix difference alone does not mark verification dirty", async () => {
    resetSpies();

    const verificationModelSelect = env.document.getElementById("verification-model-select");
    const verificationApplyBtn = env.document.getElementById("verification-apply-btn");
    const verificationWarning = env.document.getElementById("verification-unsaved-warning");

    fetchHandler = async (url) => {
      const urlStr = String(url);
      if (urlStr.includes("/status")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "ok",
            citation_verification_enabled: true,
            citation_verification_provider: "openrouter",
            citation_verification_model: "openrouter/typesafe/jev-1.14",
            citation_verification_threshold: 0.60,
            citation_verification_auto_start_local: true,
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchStatus();
    assert.strictEqual(
      verificationModelSelect.value,
      "openrouter/typesafe/jev-1.14",
      "fetchStatus must apply the persisted verification model"
    );

    // A catalog option without the openrouter/ prefix must still count as unchanged.
    verificationModelSelect.value = "typesafe/jev-1.14";
    api.updateVerificationDirtyState();

    assert.strictEqual(verificationApplyBtn.disabled, true, "Prefix-only difference must not enable Apply");
    assert.strictEqual(verificationWarning.style.display, "none", "Prefix-only difference must not show unsaved warning");

    // A genuinely different model still marks the card dirty.
    verificationModelSelect.value = "upstage/solar-decide";
    api.updateVerificationDirtyState();

    assert.strictEqual(verificationApplyBtn.disabled, false, "A different verification model must enable Apply");
    assert.strictEqual(verificationWarning.style.display, "block", "A different verification model must show the warning");
  });

  // Test 33: ok-path catalog fetch replaces the no-key fallback option
  await testAsync("fetchOpenRouterModels: ok-path catalog rebuild replaces the no-key fallback option", async () => {
    resetSpies();

    const llmModelSelect = env.document.getElementById("llm-model-select");
    const embeddingModelSelect = env.document.getElementById("embedding-model-select");
    const modeSelect = env.document.getElementById("embedding-mode-select");
    llmModelSelect.innerHTML = "";
    embeddingModelSelect.innerHTML = "";

    const fallbackLlm = "openrouter/nvidia/nemotron-3.5-lightning:free";
    const fallbackEmbedding = "openrouter/nvidia/nemotron-3-embed-1b:free";

    let catalogAvailable = false;
    fetchHandler = async (url) => {
      if (!String(url).includes("/llm/openrouter_models")) {
        return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
      }
      if (!catalogAvailable) {
        return { ok: true, status: 200, json: async () => ({ status: "no_key", models: [] }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: "success",
          models: [
            { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000 },
            { id: "google/gemini-3.8-flash", name: "Google: Gemini 3.8 Flash", context_length: 1000000 },
          ],
        }),
      };
    };

    // 1. No-key: fallback options materialized.
    await api.fetchOpenRouterModels("llm", false, fallbackLlm);
    assert.strictEqual(llmModelSelect.options.length, 1);
    assert.strictEqual(llmModelSelect.options[0].value, fallbackLlm);

    modeSelect.value = "cloud";
    await api.fetchOpenRouterModels("embedding", false, fallbackEmbedding);
    assert.strictEqual(embeddingModelSelect.options.length, 1);
    assert.strictEqual(embeddingModelSelect.options[0].value, fallbackEmbedding);

    // 2. Catalog becomes available: options are rebuilt from the catalog.
    catalogAvailable = true;
    await api.fetchOpenRouterModels("llm", true, "anthropic/claude-3.5-sonnet");
    await api.fetchOpenRouterModels("embedding", true, "anthropic/claude-3.5-sonnet");

    const llmOptionValues = llmModelSelect.querySelectorAll("option").map((o) => o.value);
    assert.deepStrictEqual(
      llmOptionValues.slice().sort(),
      ["anthropic/claude-3.5-sonnet", "google/gemini-3.8-flash"],
      "LLM select must be rebuilt from the catalog only (fallback option replaced, not duplicated)"
    );
    assert.ok(
      !llmOptionValues.includes(fallbackLlm),
      "No-key fallback option must not survive a successful catalog fetch"
    );

    const embeddingOptionValues = embeddingModelSelect.options.map((o) => o.value);
    assert.deepStrictEqual(
      embeddingOptionValues.slice().sort(),
      ["anthropic/claude-3.5-sonnet", "google/gemini-3.8-flash"],
      "Embedding select must be rebuilt from the catalog only"
    );
    assert.strictEqual(
      embeddingModelSelect.options[embeddingModelSelect.selectedIndex].value,
      "anthropic/claude-3.5-sonnet",
      "Configured embedding model present in the catalog must be selected"
    );
  });

  // Static XHTML option list of verification-model-select, used by the legacy-pref tests.
  const STATIC_VERIFICATION_MODELS = [
    "~typesafe/jev-latest",
    "typesafe/jev-1.14",
    "typesafe/jev-1.13",
    "inception/mercury-decide",
  ];

  function materializeStaticVerificationModelOptions(modelSelect) {
    modelSelect.innerHTML = "";
    for (const value of STATIC_VERIFICATION_MODELS) {
      const opt = env.document.createElementNS("http://www.w3.org/1999/xhtml", "option");
      opt.value = value;
      opt.textContent = value;
      modelSelect.appendChild(opt);
    }
    modelSelect.value = STATIC_VERIFICATION_MODELS[0];
  }

  function setVerificationCardState(state) {
    env.document.getElementById("citation-verification-enabled-toggle").checked = state.enabled;
    env.document.getElementById("verification-provider-select").value = state.provider;
    env.document.getElementById("verification-model-select").value = state.model;
    env.document.getElementById("verification-threshold-input").value = state.threshold;
    env.document.getElementById("auto-start-laya-toggle").checked = state.autoStart;
    const applyBtn = env.document.getElementById("verification-apply-btn");
    const warning = env.document.getElementById("verification-unsaved-warning");
    applyBtn.disabled = false;
    applyBtn.removeAttribute("disabled");
    warning.style.display = "block";
    return { applyBtn, warning };
  }

  function installVerificationBackendShape(shape) {
    fetchHandler = async (url) => {
      const urlStr = String(url);
      if (urlStr.includes("/status")) {
        return { ok: true, status: 200, json: async () => ({ status: "ok", ...shape }) };
      }
      if (urlStr.includes("/config")) {
        return { ok: true, status: 200, json: async () => ({ status: "success", ...shape }) };
      }
      if (urlStr.includes("/llm/openrouter_models")) {
        return { ok: true, status: 200, json: async () => ({ status: "no_key", models: [] }) };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };
  }

  // Test 34: local_laya backend with a persisted "laya" model keeps the card clean even when
  // the remote model select is deselected (Gecko assigns value "" when no option matches).
  await testAsync("fetchStatus/fetchLlmConfig: persisted 'laya' with local_laya keeps verification clean despite a deselected model select", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("verification-model-select");
    modelSelect.innerHTML = "";

    // Prod-test shape: stale pref had deselected the select (selectedIndex = -1, value "").
    const { applyBtn, warning } = setVerificationCardState({
      enabled: true,
      provider: "local_laya",
      model: "",
      threshold: "0.60",
      autoStart: true,
    });
    modelSelect.selectedIndex = -1;

    installVerificationBackendShape({
      citation_verification_enabled: true,
      citation_verification_provider: "local_laya",
      citation_verification_model: "laya",
      citation_verification_threshold: 0.60,
      citation_verification_auto_start_local: true,
    });

    await api.fetchStatus();

    assert.strictEqual(modelSelect.value, "", "fetchStatus must never assign 'laya' to the remote model select");
    assert.strictEqual(applyBtn.disabled, true, "local_laya with a persisted 'laya' model must not leave verification dirty");
    assert.strictEqual(warning.style.display, "none", "Unsaved warning must stay hidden when the persisted model is 'laya'");

    // Same guarantee on the /config (fetchLlmConfig) path.
    applyBtn.disabled = false;
    warning.style.display = "block";
    await api.fetchLlmConfig();

    assert.strictEqual(modelSelect.value, "", "fetchLlmConfig must never assign 'laya' to the remote model select");
    assert.strictEqual(applyBtn.disabled, true, "fetchLlmConfig must keep local_laya verification clean");
    assert.strictEqual(warning.style.display, "none", "fetchLlmConfig must hide the unsaved warning for local_laya");
  });

  // Test 35: a stale legacy pref outside the static option list must neither deselect the
  // select nor fake an unsaved change on open, and the backend shape must then settle clean.
  await testAsync("loadPreferences: stale legacy pref value leaves the select at its static default and does not dirty the card", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("verification-model-select");
    materializeStaticVerificationModelOptions(modelSelect);

    const { applyBtn, warning } = setVerificationCardState({
      enabled: true,
      provider: "openrouter",
      model: STATIC_VERIFICATION_MODELS[0],
      threshold: "0.60",
      autoStart: true,
    });

    const previousPrefs = {
      provider: env.zotero.Prefs.get("extensions.zocrates.citationVerificationProvider", true),
      model: env.zotero.Prefs.get("extensions.zocrates.citationVerificationModel", true),
      threshold: env.zotero.Prefs.get("extensions.zocrates.citationVerificationThreshold", true),
    };

    env.zotero.Prefs.set("extensions.zocrates.citationVerificationProvider", "openrouter", true);
    env.zotero.Prefs.set("extensions.zocrates.citationVerificationModel", "respan/span-01-lite:free", true);
    env.zotero.Prefs.set("extensions.zocrates.citationVerificationThreshold", 0.60, true);

    try {
      api.loadPreferences();

      assert.strictEqual(
        modelSelect.value,
        STATIC_VERIFICATION_MODELS[0],
        "A legacy pref value with no matching option must leave the select at its static default"
      );
      assert.strictEqual(applyBtn.disabled, true, "Load alone must not mark verification dirty");
      assert.strictEqual(warning.style.display, "none", "Load alone must not show the unsaved warning");

      // The backend is authoritative: it reports local_laya, which must also settle clean.
      installVerificationBackendShape({
        citation_verification_enabled: true,
        citation_verification_provider: "local_laya",
        citation_verification_model: "laya",
        citation_verification_threshold: 0.60,
        citation_verification_auto_start_local: true,
      });

      await api.fetchStatus();

      assert.strictEqual(
        env.document.getElementById("verification-provider-select").value,
        "local_laya",
        "fetchStatus must apply the persisted local_laya provider"
      );
      assert.strictEqual(applyBtn.disabled, true, "Persisted laya/local_laya must end the open sequence clean");
      assert.strictEqual(warning.style.display, "none", "Persisted laya/local_laya must hide the unsaved warning");
    } finally {
      env.zotero.Prefs.clear("extensions.zocrates.citationVerificationProvider", true);
      env.zotero.Prefs.clear("extensions.zocrates.citationVerificationModel", true);
      env.zotero.Prefs.clear("extensions.zocrates.citationVerificationThreshold", true);
      if (previousPrefs.provider !== undefined) {
        env.zotero.Prefs.set("extensions.zocrates.citationVerificationProvider", previousPrefs.provider, true);
      }
      if (previousPrefs.model !== undefined) {
        env.zotero.Prefs.set("extensions.zocrates.citationVerificationModel", previousPrefs.model, true);
      }
      if (previousPrefs.threshold !== undefined) {
        env.zotero.Prefs.set("extensions.zocrates.citationVerificationThreshold", previousPrefs.threshold, true);
      }
      env.document.getElementById("verification-provider-select").value = "openrouter";
      modelSelect.value = STATIC_VERIFICATION_MODELS[0];
    }
  });

  // Test 36: openrouter provider semantics are unchanged (prefix-tolerant match, real change dirty).
  await testAsync("updateVerificationDirtyState: openrouter model matching stays prefix-tolerant and still flags a real change", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("verification-model-select");
    materializeStaticVerificationModelOptions(modelSelect);

    const { applyBtn, warning } = setVerificationCardState({
      enabled: true,
      provider: "openrouter",
      model: "typesafe/jev-1.14",
      threshold: "0.60",
      autoStart: true,
    });

    installVerificationBackendShape({
      citation_verification_enabled: true,
      citation_verification_provider: "openrouter",
      citation_verification_model: "openrouter/typesafe/jev-1.14",
      citation_verification_threshold: 0.60,
      citation_verification_auto_start_local: true,
    });

    await api.fetchStatus();

    assert.strictEqual(modelSelect.value, "openrouter/typesafe/jev-1.14", "fetchStatus must apply the persisted remote model");
    assert.strictEqual(applyBtn.disabled, true, "Identical openrouter model must stay clean");

    modelSelect.value = "typesafe/jev-1.14";
    api.updateVerificationDirtyState();
    assert.strictEqual(applyBtn.disabled, true, "Prefix-only difference must not enable Apply");
    assert.strictEqual(warning.style.display, "none", "Prefix-only difference must not show the unsaved warning");

    modelSelect.value = "upstage/solar-decide";
    api.updateVerificationDirtyState();
    assert.strictEqual(applyBtn.disabled, false, "A genuinely different remote model must enable Apply");
    assert.strictEqual(warning.style.display, "block", "A genuinely different remote model must show the unsaved warning");

    modelSelect.value = "openrouter/typesafe/jev-1.14";
    api.updateVerificationDirtyState();
    assert.strictEqual(applyBtn.disabled, true, "Restoring the persisted model must return to clean");
  });

  // Test 37: the Apply handler records "laya" for local_laya instead of the deselected select value.
  await testAsync("verification apply button: local_laya bookkeeping records 'laya' and leaves the card clean", async () => {
    resetSpies();

    const modelSelect = env.document.getElementById("verification-model-select");
    materializeStaticVerificationModelOptions(modelSelect);

    const { applyBtn, warning } = setVerificationCardState({
      enabled: true,
      provider: "openrouter",
      model: "inception/mercury-decide",
      threshold: "0.60",
      autoStart: true,
    });

    fetchHandler = async (url, options = {}) => {
      const urlStr = String(url);
      if (urlStr.includes("/status")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "ok",
            citation_verification_enabled: true,
            citation_verification_provider: "openrouter",
            citation_verification_model: "inception/mercury-decide",
            citation_verification_threshold: 0.60,
            citation_verification_auto_start_local: true,
          }),
        };
      }
      fetchCalls.push({ url: urlStr, options });
      return { ok: true, status: 200, json: async () => ({ status: "success" }) };
    };

    await api.fetchStatus();
    assert.strictEqual(applyBtn.disabled, true, "Baseline openrouter state must be clean");

    // Switch to the local provider with the remote select deselected, then apply.
    env.document.getElementById("verification-provider-select").value = "local_laya";
    modelSelect.value = "";
    modelSelect.selectedIndex = -1;
    api.updateVerificationDirtyState();
    assert.strictEqual(applyBtn.disabled, false, "Switching to local_laya must mark the card dirty");

    applyBtn.dispatchEvent("click");
    await new Promise((resolve) => setTimeout(resolve, 50));

    const postCall = fetchCalls.find((c) => c.url.includes("/config") && c.options && c.options.method === "POST");
    assert.ok(postCall, "Apply must dispatch POST /config");
    assert.strictEqual(
      JSON.parse(postCall.options.body).citation_verification_model,
      "laya",
      "Apply must persist 'laya' for the local_laya provider"
    );
    assert.strictEqual(applyBtn.disabled, true, "Apply bookkeeping must record 'laya' so the card is clean");
    assert.strictEqual(warning.style.display, "none", "Apply must hide the unsaved warning");

    // The deselected remote select must not resurrect a phantom dirty state after Apply.
    modelSelect.value = "";
    modelSelect.selectedIndex = -1;
    api.updateVerificationDirtyState();
    assert.strictEqual(applyBtn.disabled, true, "A deselected remote select must stay clean while local_laya is active");

    env.zotero.Prefs.clear("extensions.zocrates.citationVerificationProvider", true);
    env.zotero.Prefs.clear("extensions.zocrates.citationVerificationModel", true);
    env.document.getElementById("verification-provider-select").value = "openrouter";
    modelSelect.value = STATIC_VERIFICATION_MODELS[0];
  });

  // Test 38: the Recommended onboarding preset must advertise a slug the catalog actually serves.
  // OpenRouter hosts Voyage under mongodb/, so the old voyageai/ slug resolved to nothing.
  test("ONBOARDING_PRESETS: recommended preset uses the live mongodb/ Voyage embedding slug", () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    assert.strictEqual(
      sidebar.ONBOARDING_PRESETS.recommended.fields.cloudEmbeddingModel,
      "openrouter/mongodb/voyage-4",
      "Recommended preset must reference the catalog slug (mongodb/voyage-4), not the dead voyageai/ slug"
    );
  });

  // Test 39: catalog membership helper (drives every "configured model not in catalog" notice)
  test("isModelInCatalog: exact match, openrouter/-prefix tolerance, and no-catalog cases", () => {
    const catalog = [
      { id: "anthropic/claude-3.5-sonnet" },
      { id: "openrouter/google/gemini-3.8-flash" },
    ];

    assert.strictEqual(api.isModelInCatalog("anthropic/claude-3.5-sonnet", catalog), true, "exact match");
    assert.strictEqual(
      api.isModelInCatalog("openrouter/anthropic/claude-3.5-sonnet", catalog),
      true,
      "openrouter/-prefixed configured id matches a bare catalog id"
    );
    assert.strictEqual(
      api.isModelInCatalog("google/gemini-3.8-flash", catalog),
      true,
      "bare configured id matches an openrouter/-prefixed catalog id"
    );
    assert.strictEqual(api.isModelInCatalog("openrouter/google/gemini-3.8-flash", catalog), true, "exact prefixed match");
    assert.strictEqual(api.isModelInCatalog("mongodb/voyage-4", catalog), false, "model absent from the catalog");

    // Nothing to compare against (no key / no catalog / no configured model) must never warn.
    assert.strictEqual(api.isModelInCatalog("", catalog), true, "empty model id");
    assert.strictEqual(api.isModelInCatalog(null, catalog), true, "null model id");
    assert.strictEqual(api.isModelInCatalog(undefined, catalog), true, "undefined model id");
    assert.strictEqual(api.isModelInCatalog("anthropic/claude-3.5-sonnet", null), true, "null catalog");
    assert.strictEqual(api.isModelInCatalog("anthropic/claude-3.5-sonnet", undefined), true, "undefined catalog");
    assert.strictEqual(api.isModelInCatalog("anthropic/claude-3.5-sonnet", []), true, "empty catalog");
  });

  // Test 40: a dead cloud embedding slug is appended+selected and flagged instead of
  // silently rendering the alphabetically first catalog entry as the configured model.
  await testAsync("renderEmbeddingModels: configured model absent from the catalog is appended, selected and flagged", async () => {
    resetSpies();

    const modeSelect = env.document.getElementById("embedding-mode-select");
    const embeddingModelSelect = env.document.getElementById("embedding-model-select");
    const notice = env.document.getElementById("embedding-catalog-missing-notice");
    const deadSlug = "openrouter/voyageai/voyage-4";

    modeSelect.value = "cloud";
    embeddingModelSelect.innerHTML = "";
    notice.style.display = "none";
    notice.textContent = "";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=embedding")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "amazon/titan-embed-text-v1", name: "Amazon: Titan Embeddings G1 - Text" },
              { id: "openrouter/nvidia/nemotron-3-embed-1b:free", name: "NVIDIA: Nemotron 3 Embed" },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("embedding", true, deadSlug);

    const optionValues = embeddingModelSelect.options.map((o) => o.value);
    assert.ok(optionValues.includes(deadSlug), "The configured (missing) model must still be selectable");
    assert.strictEqual(
      embeddingModelSelect.options[embeddingModelSelect.options.length - 1].value,
      deadSlug,
      "The missing model option is appended after the catalog entries"
    );
    assert.strictEqual(
      embeddingModelSelect.options[embeddingModelSelect.options.length - 1].textContent,
      "voyageai/voyage-4",
      "Appended option label strips the openrouter/ prefix"
    );
    assert.strictEqual(embeddingModelSelect.value, deadSlug, "The configured model must be the selected value");
    assert.strictEqual(notice.style.display, "block", "The embeddings catalog-missing notice must be visible");
    assert.ok(notice.textContent.includes(deadSlug), "Notice must name the offending model id");
    assert.ok(notice.textContent.includes("embeddings"), "Notice must describe the affected capability");

    // Selecting a real catalog entry clears the notice.
    embeddingModelSelect.value = "openrouter/nvidia/nemotron-3-embed-1b:free";
    api.updateCatalogMissingNotices();
    assert.strictEqual(notice.style.display, "none", "Choosing a catalog model must hide the notice");

    // Local mode never compares against the cloud catalog.
    modeSelect.value = "local";
    api.updateCatalogMissingNotices();
    assert.strictEqual(notice.style.display, "none", "Local embedding mode must never show the cloud catalog notice");
  });

  // Test 41: a configured LLM absent from the fetched catalog gets a first-position option
  // instead of the old silent fall-through to the alphabetically first catalog entry.
  await testAsync("renderFilteredLlmModels: configured model absent from the catalog is prepended, selected and flagged", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("llm-provider-select");
    const llmModelSelect = env.document.getElementById("llm-model-select");
    const searchInput = env.document.getElementById("llm-model-search-input");
    const notice = env.document.getElementById("llm-catalog-missing-notice");
    const deadSlug = "openrouter/~z-ai/glm-flash-latest-removed";

    providerSelect.value = "openrouter_llm";
    searchInput.value = "";
    llmModelSelect.innerHTML = "";
    notice.style.display = "none";
    notice.textContent = "";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=llm")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000 },
              { id: "google/gemini-3.8-flash", name: "Google: Gemini 3.8 Flash", context_length: 1000000 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("llm", true, deadSlug);

    assert.strictEqual(llmModelSelect.children[0].tagName.toLowerCase(), "option", "A plain option must sit before the vendor optgroups");
    assert.strictEqual(llmModelSelect.children[0].value, deadSlug, "Prepended option carries the configured model id");
    assert.strictEqual(llmModelSelect.children[0].textContent, "~z-ai/glm-flash-latest-removed", "Prepended label strips the openrouter/ prefix");
    assert.strictEqual(llmModelSelect.value, deadSlug, "The configured model must be selected, not the first catalog entry");
    assert.strictEqual(notice.style.display, "block", "The LLM catalog-missing notice must be visible");
    assert.ok(notice.textContent.includes(deadSlug), "Notice must name the offending model id");
    assert.ok(notice.textContent.includes("chat"), "Notice must describe the affected capability");

    // Picking a catalog model clears the notice.
    llmModelSelect.value = "anthropic/claude-3.5-sonnet";
    api.updateCatalogMissingNotices();
    assert.strictEqual(notice.style.display, "none", "Choosing a catalog model must hide the notice");
  });

  // Test 42: filtering must not hide the configured model — it stays at the top and selected.
  // The catalog-missing notice is a dead-slug signal only: a model that exists in the raw catalog
  // but is filtered out of the rendered list is not "missing", so no warning is shown.
  await testAsync("renderFilteredLlmModels: configured model excluded by the active filter stays prepended and selected", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("llm-provider-select");
    const llmModelSelect = env.document.getElementById("llm-model-select");
    const searchInput = env.document.getElementById("llm-model-search-input");
    const notice = env.document.getElementById("llm-catalog-missing-notice");
    const configured = "anthropic/claude-3.5-sonnet";

    providerSelect.value = "openrouter_llm";
    searchInput.value = "";
    llmModelSelect.innerHTML = "";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=llm")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000 },
              { id: "google/gemini-3.8-flash", name: "Google: Gemini 3.8 Flash", context_length: 1000000 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("llm", true, configured);
    assert.strictEqual(llmModelSelect.value, configured, "Configured catalog model must be selected");
    assert.strictEqual(notice.style.display, "none", "A configured model present in the catalog must not warn");

    // The active search filter excludes the configured model from the rendered list.
    // (Search + vendor/free chips share the same exclusion branch in renderFilteredLlmModels;
    // the chip buttons live in preferences.xhtml, which the mock DOM does not parse.)
    searchInput.value = "gemini";
    api.renderFilteredLlmModels(configured);

    assert.strictEqual(llmModelSelect.children[0].tagName.toLowerCase(), "option", "The configured model must be prepended above the optgroups");
    assert.strictEqual(llmModelSelect.children[0].value, configured, "Prepended option carries the configured model id");
    assert.strictEqual(
      llmModelSelect.children[0].textContent,
      "Claude 3.5 Sonnet",
      "Prepended label reuses the catalog display name when the model exists in the raw catalog"
    );
    assert.strictEqual(llmModelSelect.value, configured, "Filtering must not silently reselect another model");
    assert.strictEqual(
      notice.style.display,
      "none",
      "A model that exists in the raw catalog is not 'missing', so the notice stays hidden while filtering"
    );

    searchInput.value = "";
    api.renderFilteredLlmModels(configured);
  });

  // Test 43: decision models already appended the configured model; the notice makes it audible.
  await testAsync("renderDecisionModels: configured decision model absent from the catalog is flagged inline", async () => {
    resetSpies();

    const providerSelect = env.document.getElementById("verification-provider-select");
    const modelSelect = env.document.getElementById("verification-model-select");
    const notice = env.document.getElementById("verification-catalog-missing-notice");
    const configured = "respan/span-01-lite:free";

    providerSelect.value = "openrouter";
    modelSelect.innerHTML = "";
    notice.style.display = "none";
    notice.textContent = "";

    fetchHandler = async (url) => {
      if (String(url).includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "typesafe/jev-1.14", name: "TypeSafe: Jev 1.14", context_length: 32768 },
              { id: "inception/mercury-decide", name: "Inception: Mercury Decide", context_length: 32768 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("decision", true, configured);

    const optionValues = modelSelect.options.map((o) => o.value);
    assert.ok(optionValues.includes(configured), "The missing decision model must stay selectable");
    assert.strictEqual(modelSelect.value, configured, "The configured decision model must be selected");
    assert.strictEqual(notice.style.display, "block", "The verification catalog-missing notice must be visible");
    assert.ok(notice.textContent.includes(configured), "Notice must name the offending model id");
    assert.ok(notice.textContent.includes("citation verification"), "Notice must describe the affected capability");

    // The local Laya provider never consults the remote catalog.
    providerSelect.value = "local_laya";
    api.updateVerificationUiVisibility();
    assert.strictEqual(notice.style.display, "none", "local_laya must never show the remote catalog notice");

    providerSelect.value = "openrouter";
    api.updateVerificationUiVisibility();
    assert.strictEqual(notice.style.display, "block", "Returning to the remote provider restores the notice");
  });

  // Test 44: every configured model present in its catalog keeps all four notices hidden
  // (and the key-missing / catalog-missing notices never overlap in the ok path).
  await testAsync("catalog-missing notices: all four cards stay silent when the configured models exist", async () => {
    resetSpies();

    const modeSelect = env.document.getElementById("embedding-mode-select");
    const llmProviderSelect = env.document.getElementById("llm-provider-select");
    const llmModelSelect = env.document.getElementById("llm-model-select");
    const embeddingModelSelect = env.document.getElementById("embedding-model-select");
    const verificationProviderSelect = env.document.getElementById("verification-provider-select");
    const verificationModelSelect = env.document.getElementById("verification-model-select");
    const vlmEnabled = env.document.getElementById("enable-vlm-captioning-checkbox");
    const vlmOpenRouter = env.document.getElementById("vlm-provider-openrouter");
    const vlmOllama = env.document.getElementById("vlm-provider-ollama");
    const vlmPrimary = env.document.getElementById("vlm-primary-model-select");
    const vlmBackup = env.document.getElementById("vlm-backup-model-select");

    const configuredLlm = "anthropic/claude-3.5-sonnet";
    const configuredEmbedding = "openrouter/mongodb/voyage-4";
    const configuredDecision = "typesafe/jev-1.14";
    const vlmModels = [
      "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
      "openrouter/qwen/qwen3.7-flash",
    ];

    llmProviderSelect.value = "openrouter_llm";
    modeSelect.value = "cloud";
    verificationProviderSelect.value = "openrouter";
    verificationModelSelect.value = configuredDecision;
    vlmEnabled.checked = true;
    vlmOpenRouter.checked = true;
    vlmOllama.checked = false;
    llmModelSelect.innerHTML = "";
    embeddingModelSelect.innerHTML = "";
    verificationModelSelect.innerHTML = "";

    const notices = {
      llm: env.document.getElementById("llm-catalog-missing-notice"),
      embedding: env.document.getElementById("embedding-catalog-missing-notice"),
      verification: env.document.getElementById("verification-catalog-missing-notice"),
      vlm: env.document.getElementById("vlm-catalog-missing-notice"),
    };
    Object.values(notices).forEach((n) => { n.style.display = "block"; });

    fetchHandler = async (url) => {
      const urlStr = String(url);
      if (urlStr.includes("/llm/openrouter_models?target_type=embedding")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "success", models: [{ id: "openrouter/mongodb/voyage-4", name: "Voyage: Voyage 4" }] }),
        };
      }
      if (urlStr.includes("/llm/openrouter_models?target_type=decision")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: "typesafe/jev-1.14", name: "TypeSafe: Jev 1.14", context_length: 32768 },
              { id: "inception/mercury-decide", name: "Inception: Mercury Decide", context_length: 32768 },
            ],
          }),
        };
      }
      if (urlStr.includes("/llm/openrouter_models?target_type=llm")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "success",
            models: [
              { id: configuredLlm, name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000 },
              { id: vlmModels[0], name: "NVIDIA: Nemotron-3 Nano Omni", context_length: 128000 },
              { id: vlmModels[1], name: "Qwen: Qwen 3.7 Flash", context_length: 128000 },
            ],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
    };

    await api.fetchOpenRouterModels("llm", true, configuredLlm);
    await api.fetchOpenRouterModels("embedding", true, configuredEmbedding);
    await api.fetchOpenRouterModels("decision", true, configuredDecision);

    assert.strictEqual(llmModelSelect.value, configuredLlm, "LLM select holds the configured catalog model");
    assert.strictEqual(embeddingModelSelect.value, configuredEmbedding, "Embedding select holds the configured catalog model");
    assert.strictEqual(verificationModelSelect.value, configuredDecision, "Verification select holds the configured catalog model");
    assert.ok(vlmModels.includes(vlmPrimary.value), "Primary VLM model resolves to a catalog model");

    assert.strictEqual(notices.llm.style.display, "none", "LLM notice hidden when the model exists");
    assert.strictEqual(notices.embedding.style.display, "none", "Embedding notice hidden when the model exists");
    assert.strictEqual(notices.verification.style.display, "none", "Verification notice hidden when the model exists");
    assert.strictEqual(notices.vlm.style.display, "none", "VLM notice hidden when the models exist");

    // No catalog at all (e.g. no OpenRouter key) must not fabricate a catalog warning either.
    vlmBackup.value = "openrouter/some-offline-model";
    api.updateCatalogMissingNotices();
    assert.strictEqual(notices.vlm.style.display, "block", "A VLM model outside the catalog is flagged");

    vlmBackup.value = vlmModels[1];
    api.updateCatalogMissingNotices();
    assert.strictEqual(notices.vlm.style.display, "none", "Restoring a catalog VLM model clears the notice");
  });
  } finally {
    global.fetch = origFetch;
    global.IOUtils = origIOUtils;
    if (global.window) {
      global.window.IOUtils = origWindowIOUtils;
    }
  }
}

module.exports = runTests;

if (require.main === module) {
  const { resetTestStats, printTestSummary } = require("./test_helpers.js");
  resetTestStats();
  runTests().then(() => {
    const passed = printTestSummary();
    if (!passed) process.exit(1);
  });
}

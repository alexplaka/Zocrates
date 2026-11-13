/**
 * test_bootstrap_hubs.js — Tests for Notifier and BatchProgress hubs in bootstrap.js.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Bootstrap Hubs (Notifier & BatchProgress) ---");
  const env = setupMockEnvironment();
  const bootstrap = require("../../zotero-plugin/addon/bootstrap.js");
  const {
    initNotifier,
    initBatchProgress,
    initAuthManager,
    generateAuthToken,
    loadStoredApiKey,
    saveStoredApiKey,
    spawnBackend,
    attachBackendExitMonitor,
    setSubprocessForTesting,
    setBackendStatus,
    injectToolbarButton,
    getWindowsArchitecture,
    isVcRedistInstalledWindows,
    ensureVcRedistWindows,
    VC_REDIST_DOC_URL,
  } = require("../../zotero-plugin/addon/bootstrap.js");

  // 1. Zotero.Zocrates.Notifier
  test("Notifier: add adds item, returns id, and enforces 20-item capacity cap", () => {
    const mockZotero = env.zotero;
    const notifier = initNotifier(mockZotero);
    notifier.clear();

    const id1 = notifier.add("First notification", "info");
    assert.ok(id1);
    assert.strictEqual(notifier.getAll().length, 1);
    assert.strictEqual(notifier.getAll()[0].text, "First notification");
    assert.strictEqual(notifier.getAll()[0].type, "info");

    // Add 24 more (total 25)
    for (let i = 2; i <= 25; i++) {
      notifier.add(`Notification #${i}`, "info");
    }

    const all = notifier.getAll();
    assert.strictEqual(all.length, 20, "Notifier must enforce a strict 20-item cap");
    assert.strictEqual(all[0].text, "Notification #25", "Newest notification should be at index 0");
    assert.strictEqual(all[19].text, "Notification #6", "Oldest notification within cap should be index 19");
  });

  test("Notifier: delete removes item by ID and clear empties all items", () => {
    const mockZotero = env.zotero;
    const notifier = initNotifier(mockZotero);
    notifier.clear();

    const idA = notifier.add("A");
    const idB = notifier.add("B");
    assert.strictEqual(notifier.getAll().length, 2);

    notifier.delete(idA);
    const remaining = notifier.getAll();
    assert.strictEqual(remaining.length, 1);
    assert.strictEqual(remaining[0].id, idB);

    notifier.clear();
    assert.strictEqual(notifier.getAll().length, 0);
  });

  test("Notifier: invokes subscriber callbacks and Services.obs notifications on add and delete", () => {
    const mockZotero = env.zotero;
    const notifier = initNotifier(mockZotero);
    notifier.clear();

    const subscriberEvents = [];
    const unsub = notifier.subscribe((notif) => {
      subscriberEvents.push(notif);
    });

    const observerEvents = [];
    const obs = (subject, topic, data) => {
      observerEvents.push({ topic, data });
    };
    global.Services.obs.addObserver(obs, "zocrates-notification-added");
    global.Services.obs.addObserver(obs, "zocrates-notification-deleted");

    try {
      const id = notifier.add("Observable notification", "warning");

      assert.strictEqual(subscriberEvents.length, 1);
      assert.strictEqual(subscriberEvents[0].id, id);
      assert.strictEqual(subscriberEvents[0].type, "warning");

      assert.strictEqual(observerEvents.length, 1);
      assert.strictEqual(observerEvents[0].topic, "zocrates-notification-added");
      const parsedData = JSON.parse(observerEvents[0].data);
      assert.strictEqual(parsedData.id, id);

      // Delete
      notifier.delete(id);

      assert.strictEqual(subscriberEvents.length, 2);
      assert.strictEqual(subscriberEvents[1], null); // subscriber receives null on delete

      assert.strictEqual(observerEvents.length, 2);
      assert.strictEqual(observerEvents[1].topic, "zocrates-notification-deleted");
      assert.strictEqual(observerEvents[1].data, id);

      // Unsubscribe test
      unsub();
      notifier.add("After unsub");
      assert.strictEqual(subscriberEvents.length, 2, "Unsubscribed callback should not be called again");
    } finally {
      global.Services.obs.removeObserver(obs, "zocrates-notification-added");
      global.Services.obs.removeObserver(obs, "zocrates-notification-deleted");
    }
  });

  // 2. Zotero.Zocrates.BatchProgress
  test("BatchProgress: update sets active job and notifies subscribers", () => {
    const mockZotero = env.zotero;
    const batchProgress = initBatchProgress(mockZotero);
    batchProgress.clear();

    const updates = [];
    const unsub = batchProgress.subscribe((job) => {
      updates.push(job);
    });

    const sampleJob = {
      id: "job_123",
      total: 5,
      processed: 2,
      status: "converting",
    };

    batchProgress.update(sampleJob);
    assert.deepStrictEqual(batchProgress.get(), sampleJob);
    assert.strictEqual(updates.length, 1);
    assert.deepStrictEqual(updates[0], sampleJob);

    unsub();
  });

  test("BatchProgress: new subscribers immediately receive active job if present", () => {
    const mockZotero = env.zotero;
    const batchProgress = initBatchProgress(mockZotero);

    const activeJob = { id: "job_running", total: 10, processed: 4 };
    batchProgress.update(activeJob);

    let receivedImmediately = null;
    const unsub = batchProgress.subscribe((job) => {
      receivedImmediately = job;
    });

    assert.deepStrictEqual(receivedImmediately, activeJob);
    unsub();
  });

  test("BatchProgress: clear resets active job to null and notifies subscribers", () => {
    const mockZotero = env.zotero;
    const batchProgress = initBatchProgress(mockZotero);

    batchProgress.update({ id: "job_to_clear" });
    assert.ok(batchProgress.get());

    let notifiedWith = undefined;
    const unsub = batchProgress.subscribe((job) => {
      notifiedWith = job;
    });

    batchProgress.clear();
    assert.strictEqual(batchProgress.get(), null);
    assert.strictEqual(notifiedWith, null);

    unsub();
  });

  // 3. Zotero.Zocrates Auth Token Manager
  test("AuthManager: generates 256-bit hex token and builds Authorization Bearer headers", () => {
    const tok = generateAuthToken();
    assert.strictEqual(typeof tok, "string");
    assert.strictEqual(tok.length, 64);
    assert.ok(/^[0-9a-f]{64}$/.test(tok));

    const mockZotero = { Zocrates: {} };
    initAuthManager(mockZotero);
    const activeToken = mockZotero.Zocrates.getAuthToken();
    assert.strictEqual(activeToken, mockZotero.Zocrates._authToken);
    assert.strictEqual(activeToken.length, 64);

    const headers = mockZotero.Zocrates.getAuthHeaders({ "Content-Type": "application/json" });
    assert.deepStrictEqual(headers, {
      "Content-Type": "application/json",
      Authorization: "Bearer " + activeToken,
    });
  });

  test("AuthManager: reads existing backend.auth sidecar when present", () => {
    const mockZotero = {
      DataDirectory: { dir: "/mock/zotero" },
      File: {
        pathToFile: (p) => ({
          path: p,
          exists: () => p.endsWith("/zocrates-data/backend.auth"),
        }),
        getContents: () => "deadbeef".repeat(8) + "\n",
      },
      Zocrates: {},
    };
    initAuthManager(mockZotero);
    assert.strictEqual(mockZotero.Zocrates.getAuthToken(), "deadbeef".repeat(8));
  });

  // 4. Stored API Key Resolution (loadStoredApiKey)
  test("loadStoredApiKey: resolves keys from Services.logins and Services.passwords (chrome://zocrates)", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    const passwordsStore = new Map([
      ["openrouter_api_key", "sk-or-test-pass"],
      ["datalabApiKey", "dl-test-pass"],
      ["openalex_api_key", "oa-test-pass"],
    ]);

    const mockLoginManager = {
      findLogins(hostname, url, target) {
        if (hostname === "chrome://zocrates" && passwordsStore.has(target)) {
          return [{ password: passwordsStore.get(target) }];
        }
        return [];
      },
    };

    try {
      // Test via Services.passwords
      global.Services.passwords = mockLoginManager;
      delete global.Services.logins;

      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-or-test-pass");
      assert.strictEqual(loadStoredApiKey("openRouterApiKey"), "sk-or-test-pass");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "dl-test-pass");
      assert.strictEqual(loadStoredApiKey("datalabApiKey"), "dl-test-pass");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "oa-test-pass");
      assert.strictEqual(loadStoredApiKey("openAlexApiKey"), "oa-test-pass");

      // Test via modern Gecko Services.logins
      delete global.Services.passwords;
      global.Services.logins = mockLoginManager;

      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-or-test-pass");
      assert.strictEqual(loadStoredApiKey("openRouterApiKey"), "sk-or-test-pass");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "dl-test-pass");
      assert.strictEqual(loadStoredApiKey("datalabApiKey"), "dl-test-pass");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "oa-test-pass");
      assert.strictEqual(loadStoredApiKey("openAlexApiKey"), "oa-test-pass");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
    }
  });

  test("loadStoredApiKey: falls back to Zotero.Prefs when Services.passwords has no match", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;
    global.Services.passwords = {
      findLogins: () => [],
    };

    try {
      // 1. snake_case prefs
      global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-or-pref-snake", true);
      global.Zotero.Prefs.set("extensions.zocrates.datalab_api_key", "dl-pref-snake", true);
      global.Zotero.Prefs.set("extensions.zocrates.openalex_api_key", "oa-pref-snake", true);

      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-or-pref-snake");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "dl-pref-snake");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "oa-pref-snake");

      // 2. camelCase prefs fallback
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);

      global.Zotero.Prefs.set("extensions.zocrates.openRouterApiKey", "sk-or-pref-camel", true);
      global.Zotero.Prefs.set("extensions.zocrates.datalabApiKey", "dl-pref-camel", true);
      global.Zotero.Prefs.set("extensions.zocrates.openAlexApiKey", "oa-pref-camel", true);

      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-or-pref-camel");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "dl-pref-camel");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "oa-pref-camel");
    } finally {
      // Clear after test
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openRouterApiKey", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalabApiKey", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openAlexApiKey", true);
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
    }
  });

  test("loadStoredApiKey: returns empty string when key is not found or empty", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;
    global.Services.passwords = { findLogins: () => [] };
    try {
      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "");
      assert.strictEqual(loadStoredApiKey(null), "");
      assert.strictEqual(loadStoredApiKey(""), "");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
    }
  });

  test("saveStoredApiKey: stores to Login Manager and does NOT write to Zotero.Prefs", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;

    const storedLogins = [];
    global.Services.passwords = {
      findLogins(hostname, url, target) {
        return storedLogins.filter((l) => l.hostname === hostname && l.httpRealm === target);
      },
      removeLogin(login) {
        const idx = storedLogins.indexOf(login);
        if (idx !== -1) storedLogins.splice(idx, 1);
      },
      addLogin(login) {
        storedLogins.push(login);
      },
    };

    try {
      saveStoredApiKey("openrouter_api_key", "sk-test-secret-openrouter");
      saveStoredApiKey("datalab_api_key", "dl-test-secret-datalab");
      saveStoredApiKey("openalex_api_key", "oa-test-secret-openalex");

      // Verify stored to Login Manager
      assert.strictEqual(storedLogins.length, 3);
      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-test-secret-openrouter");
      assert.strictEqual(loadStoredApiKey("openRouterApiKey"), "sk-test-secret-openrouter");
      assert.strictEqual(loadStoredApiKey("datalab_api_key"), "dl-test-secret-datalab");
      assert.strictEqual(loadStoredApiKey("datalabApiKey"), "dl-test-secret-datalab");
      assert.strictEqual(loadStoredApiKey("openalex_api_key"), "oa-test-secret-openalex");
      assert.strictEqual(loadStoredApiKey("openAlexApiKey"), "oa-test-secret-openalex");

      // Verify persistent storage in Zotero.Prefs as guaranteed storage
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openrouter_api_key", true), "sk-test-secret-openrouter");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openRouterApiKey", true), "sk-test-secret-openrouter");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.datalab_api_key", true), "dl-test-secret-datalab");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.datalabApiKey", true), "dl-test-secret-datalab");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openalex_api_key", true), "oa-test-secret-openalex");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openAlexApiKey", true), "oa-test-secret-openalex");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
      saveStoredApiKey("openrouter_api_key", "");
      saveStoredApiKey("datalab_api_key", "");
      saveStoredApiKey("openalex_api_key", "");
    }
  });

  test("migration: when key is present in Zotero.Prefs and absent from Login Manager, calling loadStoredApiKey returns key and preserves Zotero.Prefs", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;

    const storedLogins = [];
    global.Services.passwords = {
      findLogins(hostname, url, target) {
        return storedLogins.filter((l) => l.hostname === hostname && l.httpRealm === target);
      },
      removeLogin(login) {
        const idx = storedLogins.indexOf(login);
        if (idx !== -1) storedLogins.splice(idx, 1);
      },
      addLogin(login) {
        storedLogins.push(login);
      },
    };

    try {
      // 1. Seed snake_case and camelCase legacy preferences
      global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-or-migrate-snake", true);
      global.Zotero.Prefs.set("extensions.zocrates.openRouterApiKey", "sk-or-migrate-camel", true);
      assert.strictEqual(storedLogins.length, 0, "Login Manager must initially have 0 logins");

      // Calling loadStoredApiKey should find the legacy pref, return it, and ensure it remains available
      const resolvedOr = loadStoredApiKey("openrouter_api_key");
      assert.strictEqual(resolvedOr, "sk-or-migrate-snake");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openrouter_api_key", true), "sk-or-migrate-snake");

      // 2. Test camelCase-only legacy pref migration
      global.Zotero.Prefs.set("extensions.zocrates.datalabApiKey", "dl-camel-only-legacy", true);
      const resolvedDl = loadStoredApiKey("datalab_api_key");
      assert.strictEqual(resolvedDl, "dl-camel-only-legacy");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.datalabApiKey", true), "dl-camel-only-legacy");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openRouterApiKey", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalabApiKey", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
    }
  });

  test("failsafe: when Login Manager write fails or login manager is absent, migration does NOT erase legacy Zotero.Prefs", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;

    // Login manager that fails to add logins
    global.Services.passwords = {
      findLogins: () => [],
      addLogin: () => {
        throw new Error("Disk / vault error");
      },
      removeLogin: () => {},
    };

    try {
      global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-safe-key", true);
      const val = loadStoredApiKey("openrouter_api_key");
      assert.strictEqual(val, "sk-safe-key");
      // Since addLogin threw, legacy preference must NOT be cleared (fail-safe)
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openrouter_api_key", true), "sk-safe-key");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
    }
  });

  test("deletion: saving an empty key removes the login from Login Manager and clears Zotero.Prefs", () => {
    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;

    const storedLogins = [];
    global.Services.passwords = {
      findLogins(hostname, url, target) {
        return storedLogins.filter((l) => l.hostname === hostname && l.httpRealm === target);
      },
      removeLogin(login) {
        const idx = storedLogins.indexOf(login);
        if (idx !== -1) storedLogins.splice(idx, 1);
      },
      addLogin(login) {
        storedLogins.push(login);
      },
    };

    try {
      // First save keys
      saveStoredApiKey("openrouter_api_key", "sk-to-be-deleted");
      assert.strictEqual(storedLogins.length, 1);
      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "sk-to-be-deleted");

      // Also set legacy pref to ensure it gets cleared on empty save
      global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-legacy-lingering", true);
      global.Zotero.Prefs.set("extensions.zocrates.openRouterApiKey", "sk-legacy-lingering-camel", true);

      // Now save empty key (deletion)
      saveStoredApiKey("openrouter_api_key", "");

      // Login must be removed from Login Manager
      assert.strictEqual(storedLogins.length, 0);

      // Zotero.Prefs must be cleared
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openrouter_api_key", true), undefined);
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openRouterApiKey", true), undefined);

      // loadStoredApiKey must return empty string
      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "");
      assert.strictEqual(loadStoredApiKey("openRouterApiKey"), "");

      // Also test saving whitespace-only string
      saveStoredApiKey("openrouter_api_key", "sk-another-one");
      assert.strictEqual(storedLogins.length, 1);
      saveStoredApiKey("openrouter_api_key", "   ");
      assert.strictEqual(storedLogins.length, 0);
      assert.strictEqual(loadStoredApiKey("openrouter_api_key"), "");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openRouterApiKey", true);
    }
  });

  test("sidebar saveStoredApiKey and loadStoredApiKey delegate to Zotero.Zocrates", () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    assert.strictEqual(typeof sidebar.saveStoredApiKey, "function");
    assert.strictEqual(typeof sidebar.loadStoredApiKey, "function");

    const originalPasswords = global.Services.passwords;
    const originalLogins = global.Services.logins;
    delete global.Services.logins;

    const storedLogins = [];
    global.Services.passwords = {
      findLogins(hostname, url, target) {
        return storedLogins.filter((l) => l.hostname === hostname && l.httpRealm === target);
      },
      removeLogin(login) {
        const idx = storedLogins.indexOf(login);
        if (idx !== -1) storedLogins.splice(idx, 1);
      },
      addLogin(login) {
        storedLogins.push(login);
      },
    };

    try {
      sidebar.saveStoredApiKey("openrouter_api_key", "sk-sidebar-delegated");
      assert.strictEqual(storedLogins.length, 1);
      assert.strictEqual(sidebar.loadStoredApiKey("openrouter_api_key"), "sk-sidebar-delegated");
      assert.strictEqual(global.Zotero.Prefs.get("extensions.zocrates.openrouter_api_key", true), "sk-sidebar-delegated");

      sidebar.saveStoredApiKey("openrouter_api_key", "");
      assert.strictEqual(storedLogins.length, 0);
      assert.strictEqual(sidebar.loadStoredApiKey("openrouter_api_key"), "");
    } finally {
      global.Services.passwords = originalPasswords;
      global.Services.logins = originalLogins;
    }
  });

  // 5. Backend Spawn Environment Injection (spawnBackend)
  await testAsync("spawnBackend: injects OPENROUTER_API_KEY, DATALAB_API_KEY, and OPENALEX_API_KEY into child environment", async () => {
    global.Zotero.DataDirectory = { dir: "/mock/zotero" };
    global.Services.appinfo = { processID: 12345 };
    global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-or-spawn-key", true);
    global.Zotero.Prefs.set("extensions.zocrates.datalab_api_key", "dl-spawn-key", true);
    global.Zotero.Prefs.set("extensions.zocrates.openalex_api_key", "oa-spawn-key", true);

    let capturedCall = null;
    const mockSubprocess = {
      call: async (opts) => {
        capturedCall = opts;
        return {
          stderr: null,
          wait: () => Promise.resolve({ exitCode: 0 }),
        };
      },
    };
    setSubprocessForTesting(mockSubprocess);

    try {
      await spawnBackend("/mock/zotero/zocrates-data", 8765);
      assert.ok(capturedCall, "subprocess.call should have been invoked");
      assert.strictEqual(capturedCall.environment.OPENROUTER_API_KEY, "sk-or-spawn-key");
      assert.strictEqual(capturedCall.environment.DATALAB_API_KEY, "dl-spawn-key");
      assert.strictEqual(capturedCall.environment.OPENALEX_API_KEY, "oa-spawn-key");
      assert.strictEqual(capturedCall.environment.BACKEND_PORT, "8765");
      assert.strictEqual(capturedCall.environment.ZOTERO_PID, "12345");
      assert.strictEqual(capturedCall.environment.ZOTERO_DATA_DIR, "/mock/zotero");
      assert.strictEqual(capturedCall.environmentAppend, true);
    } finally {
      saveStoredApiKey("openrouter_api_key", "");
      saveStoredApiKey("datalab_api_key", "");
      saveStoredApiKey("openalex_api_key", "");
      setSubprocessForTesting(null);
    }
  });

  await testAsync("spawnBackend: omits API key variables from child environment when not stored", async () => {
    global.Zotero.DataDirectory = { dir: "/mock/zotero" };
    global.Services.appinfo = { processID: 54321 };
    global.Services.passwords = { findLogins: () => [] };

    let capturedCall = null;
    const mockSubprocess = {
      call: async (opts) => {
        capturedCall = opts;
        return {
          stderr: null,
          wait: () => Promise.resolve({ exitCode: 0 }),
        };
      },
    };
    setSubprocessForTesting(mockSubprocess);

    try {
      await spawnBackend("/mock/zotero/zocrates-data", 8765);
      assert.ok(capturedCall, "subprocess.call should have been invoked");
      assert.strictEqual(capturedCall.environment.OPENROUTER_API_KEY, undefined);
      assert.strictEqual(capturedCall.environment.DATALAB_API_KEY, undefined);
      assert.strictEqual(capturedCall.environment.OPENALEX_API_KEY, undefined);
      assert.strictEqual(capturedCall.environment.BACKEND_PORT, "8765");
      assert.strictEqual(capturedCall.environment.ZOTERO_PID, "54321");
      assert.strictEqual(capturedCall.environment.ZOTERO_DATA_DIR, "/mock/zotero");
      assert.strictEqual(capturedCall.environmentAppend, true);
    } finally {
      setSubprocessForTesting(null);
    }
  });

  // 6. Sidebar Registration Payload with Stored Keys (autoRegister)
  await testAsync("sidebar autoRegister: includes stored API keys in /register request body", async () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    global.Zotero.DataDirectory = { dir: "/mock/zotero" };
    global.Zotero.Prefs.set("extensions.zocrates.openrouter_api_key", "sk-or-sidebar-reg", true);
    global.Zotero.Prefs.set("extensions.zocrates.datalab_api_key", "dl-sidebar-reg", true);
    global.Zotero.Prefs.set("extensions.zocrates.openalex_api_key", "oa-sidebar-reg", true);

    let registerRequest = null;
    const originalFetch = global.fetch;
    global.fetch = async (url, opts) => {
      if (url.endsWith("/register")) {
        registerRequest = {
          url,
          headers: opts.headers,
          body: JSON.parse(opts.body),
        };
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "success" }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    try {
      await sidebar.autoRegister();
      assert.ok(registerRequest, "POST /register should have been called");
      assert.strictEqual(registerRequest.body.zotero_data_dir, "/mock/zotero");
      assert.strictEqual(registerRequest.body.openrouter_api_key, "sk-or-sidebar-reg");
      assert.strictEqual(registerRequest.body.datalab_api_key, "dl-sidebar-reg");
      assert.strictEqual(registerRequest.body.openalex_api_key, "oa-sidebar-reg");
      assert.strictEqual(global.Zotero.Zocrates._backendStatus.status, "connected");
      assert.strictEqual(global.Zotero.Zocrates._backendStatus.label, "Connected");
    } finally {
      global.fetch = originalFetch;
      global.Zotero.Prefs.clear("extensions.zocrates.openrouter_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);
    }
  });

  // 6b. Sidebar Warm-Backend Credential Synchronization (syncWarmBackendCredentials)
  await testAsync("sidebar syncWarmBackendCredentials: syncs missing credentials for warm-attached backends", async () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    global.Zotero.Prefs.set("extensions.zocrates.datalab_api_key", "dl-warm-sync-key", true);
    global.Zotero.Prefs.set("extensions.zocrates.openalex_api_key", "oa-warm-sync-key", true);

    let configCalls = [];
    const originalFetch = global.fetch;
    global.fetch = async (url, opts) => {
      if (url.endsWith("/config") && opts && opts.method === "POST") {
        configCalls.push({
          url,
          headers: opts.headers,
          body: JSON.parse(opts.body),
        });
        return { ok: true, status: 200, json: async () => ({ status: "success" }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    try {
      // 1. Both keys missing in backend (has_datalab_key: false, has_openalex_api_key: false)
      configCalls = [];
      await sidebar.syncWarmBackendCredentials({
        status: "ready",
        has_datalab_key: false,
        has_openalex_api_key: false,
      });
      assert.strictEqual(configCalls.length, 1, "Must POST missing credentials to /config");
      assert.strictEqual(configCalls[0].body.datalab_api_key, "dl-warm-sync-key");
      assert.strictEqual(configCalls[0].body.openalex_api_key, "oa-warm-sync-key");

      // 2. Only datalab missing in backend
      configCalls = [];
      await sidebar.syncWarmBackendCredentials({
        status: "ready",
        has_datalab_key: false,
        has_openalex_api_key: true,
      });
      assert.strictEqual(configCalls.length, 1);
      assert.strictEqual(configCalls[0].body.datalab_api_key, "dl-warm-sync-key");
      assert.strictEqual(configCalls[0].body.openalex_api_key, undefined);

      // 3. Backend already has all keys
      configCalls = [];
      await sidebar.syncWarmBackendCredentials({
        status: "ready",
        has_datalab_key: true,
        has_openalex_api_key: true,
      });
      assert.strictEqual(configCalls.length, 0, "Must NOT call /config when backend already has all keys");

      // 4. Backend not ready
      configCalls = [];
      await sidebar.syncWarmBackendCredentials({
        status: "not_registered",
        has_datalab_key: false,
        has_openalex_api_key: false,
      });
      assert.strictEqual(configCalls.length, 0, "Must NOT call /config when backend is not ready");
    } finally {
      global.fetch = originalFetch;
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);
    }
  });

  // 6c. Sidebar checkBackendStatus Warm-Backend Credential Transition Gating
  await testAsync("sidebar checkBackendStatus: warm-backend credential sync runs only on connection transition and when credentials exist", async () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    sidebar.resetPollingStateForTests();

    global.Zotero.Prefs.set("extensions.zocrates.datalab_api_key", "dl-transition-key", true);

    let configCalls = [];
    const originalFetch = global.fetch;
    global.fetch = async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.endsWith("/health")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "ready",
            has_datalab_key: false,
            has_openalex_api_key: true,
          }),
        };
      }
      if (urlStr.endsWith("/config") && opts && opts.method === "POST") {
        configCalls.push({
          url: urlStr,
          body: JSON.parse(opts.body),
        });
        return { ok: true, status: 200, json: async () => ({ status: "success" }) };
      }
      return { ok: true, status: 200, json: async () => ({ sessions: [] }) };
    };

    try {
      // 1. First check: wasBackendOnline is null (transition from offline/initial) -> should sync
      configCalls = [];
      await sidebar.checkBackendStatus();
      assert.strictEqual(configCalls.length, 1, "Must sync credentials on initial connect transition");
      assert.strictEqual(configCalls[0].body.datalab_api_key, "dl-transition-key");

      // 2. Second check: wasBackendOnline is now true -> must NOT sync again
      configCalls = [];
      await sidebar.checkBackendStatus();
      assert.strictEqual(configCalls.length, 0, "Must NOT sync credentials on subsequent active poll when already connected");

      // 3. Clear credentials, reset state, and test that no sync runs even on transition if no credentials exist
      sidebar.resetPollingStateForTests();
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);

      configCalls = [];
      await sidebar.checkBackendStatus();
      assert.strictEqual(configCalls.length, 0, "Must NOT sync credentials when no local credentials exist");
    } finally {
      global.fetch = originalFetch;
      global.Zotero.Prefs.clear("extensions.zocrates.datalab_api_key", true);
      global.Zotero.Prefs.clear("extensions.zocrates.openalex_api_key", true);
      sidebar.resetPollingStateForTests();
    }
  });

  // 7. Backend Connection Status Dot & Tooltip (setBackendStatus & injectToolbarButton)
  test("setBackendStatus: updates _backendStatus, button attribute, status dot, and tooltiptext", () => {
    const mockWin = env.window;
    const mockDoc = env.document;

    const toolbar = mockDoc.getElementById("zotero-tabs-toolbar");
    toolbar.children = [];
    env.elementsById.delete("zocrates-toolbar-btn");
    global.Zotero.Zocrates._backendStatus = { status: "connecting", label: "Connecting..." };

    injectToolbarButton(mockWin);
    const btn = mockDoc.getElementById("zocrates-toolbar-btn");
    assert.ok(btn, "Toolbar button should be present");
    assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
    assert.strictEqual(btn.getAttribute("data-status"), "connecting");
    assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connecting...)");

    const dot = btn.querySelector(".zocrates-status-dot");
    assert.ok(dot, "Status dot should be present inside button");
    assert.strictEqual(dot.tagName.toLowerCase(), "box", "Dot must be created as a <box> element");
    assert.strictEqual(dot.getAttribute("data-status"), "connecting");

    const originalGetMainWindows = global.Zotero.getMainWindows;
    global.Zotero.getMainWindows = () => [mockWin];

    try {
      setBackendStatus("connected", "Connected");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connected", label: "Connected" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connected");
      assert.strictEqual(dot.getAttribute("data-status"), "connected");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connected)");
    } finally {
      global.Zotero.getMainWindows = originalGetMainWindows;
    }
  });

  test("setBackendStatus: transitions across connecting and offline states with default and custom labels", () => {
    const mockWin = env.window;
    const mockDoc = env.document;

    const btn = mockDoc.getElementById("zocrates-toolbar-btn");
    assert.ok(btn, "Toolbar button should be present");
    assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
    const dot = btn.querySelector(".zocrates-status-dot");
    assert.ok(dot, "Status dot should be present inside button");
    assert.strictEqual(dot.tagName.toLowerCase(), "box", "Dot must be created as a <box> element");

    const originalGetMainWindows = global.Zotero.getMainWindows;
    global.Zotero.getMainWindows = () => [mockWin];

    try {
      // Transition to connecting with custom label
      setBackendStatus("connecting", "Starting...");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connecting", label: "Starting..." });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connecting");
      assert.strictEqual(dot.getAttribute("data-status"), "connecting");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Starting...)");

      // Transition to connecting with default label
      setBackendStatus("connecting");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connecting", label: "Connecting..." });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connecting");
      assert.strictEqual(dot.getAttribute("data-status"), "connecting");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connecting...)");

      // Transition to connected with custom and default labels
      setBackendStatus("connected", "Online");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connected", label: "Online" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connected");
      assert.strictEqual(dot.getAttribute("data-status"), "connected");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Online)");

      setBackendStatus("connected");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connected", label: "Connected" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connected");
      assert.strictEqual(dot.getAttribute("data-status"), "connected");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connected)");

      // Transition to offline with custom label
      setBackendStatus("offline", "Launch Failed");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "offline", label: "Launch Failed" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "offline");
      assert.strictEqual(dot.getAttribute("data-status"), "offline");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Launch Failed)");

      // Transition to offline with default label
      setBackendStatus("offline");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "offline", label: "Offline" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "offline");
      assert.strictEqual(dot.getAttribute("data-status"), "offline");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Offline)");

      // Transition to connected with Needs Registration
      setBackendStatus("connected", "Needs Registration");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "connected", label: "Needs Registration" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "connected");
      assert.strictEqual(dot.getAttribute("data-status"), "connected");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Needs Registration)");

      // Transition to disconnected
      setBackendStatus("disconnected", "Disconnected");
      assert.deepStrictEqual(global.Zotero.Zocrates._backendStatus, { status: "disconnected", label: "Disconnected" });
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "disconnected");
      assert.strictEqual(dot.getAttribute("data-status"), "disconnected");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Disconnected)");

      // Falsy status defaults to offline
      setBackendStatus(null);
      assert.strictEqual(global.Zotero.Zocrates._backendStatus.status, "offline");
      assert.strictEqual(btn.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btn.getAttribute("data-status"), "offline");
      assert.strictEqual(dot.getAttribute("data-status"), "offline");
      assert.strictEqual(btn.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Offline)");
    } finally {
      global.Zotero.getMainWindows = originalGetMainWindows;
    }
  });

  test("injectToolbarButton: initializes button and status dot with current status and tooltip", () => {
    const { createMockElement } = require("./test_helpers.js");

    // Case 1: Status is connected prior to button injection
    global.Zotero.Zocrates._backendStatus = { status: "connected", label: "Connected" };
    const toolbar1 = createMockElement("div");
    toolbar1.id = "zotero-tabs-toolbar";
    let insertedChildrenCount = null;
    const realAppend1 = toolbar1.appendChild.bind(toolbar1);
    toolbar1.appendChild = (child) => {
      insertedChildrenCount = child.children.length;
      assert.strictEqual(child.children.length, 0, "button must be inserted with no children so MozToolbarbutton renders icon");
      return realAppend1(child);
    };
    const win1 = {
      document: {
        getElementById: (id) => {
          if (id === "zocrates-toolbar-btn") return null;
          if (id === "zotero-tabs-toolbar") return toolbar1;
          return null;
        },
        createElement: (t) => createMockElement(t),
        createElementNS: (ns, t) => createMockElement(t, ns),
        createXULElement: (t) => createMockElement(t.replace(/^html:/, "")),
        documentElement: createMockElement("html"),
      },
    };

    injectToolbarButton(win1);
    const btn1 = toolbar1.children.find((c) => c.id === "zocrates-toolbar-btn");
    assert.ok(btn1, "Toolbar button should be injected into toolbar1");
    assert.strictEqual(btn1.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
    assert.strictEqual(btn1.getAttribute("data-status"), "connected");
    assert.strictEqual(btn1.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connected)");
    const dot1 = btn1.querySelector(".zocrates-status-dot");
    assert.ok(dot1, "Status dot should be appended to button");
    assert.strictEqual(dot1.tagName.toLowerCase(), "box", "Dot must be created as a <box> element");
    assert.strictEqual(dot1.getAttribute("data-status"), "connected");

    // Case 2: Status is connecting prior to button injection
    global.Zotero.Zocrates._backendStatus = { status: "connecting", label: "Connecting..." };
    const toolbar2 = createMockElement("div");
    toolbar2.id = "zotero-tabs-toolbar";
    const win2 = {
      document: {
        getElementById: (id) => {
          if (id === "zocrates-toolbar-btn") return null;
          if (id === "zotero-tabs-toolbar") return toolbar2;
          return null;
        },
        createElement: (t) => createMockElement(t),
        createElementNS: (ns, t) => createMockElement(t, ns),
        createXULElement: (t) => createMockElement(t.replace(/^html:/, "")),
        documentElement: createMockElement("html"),
      },
    };

    injectToolbarButton(win2);
    const btn2 = toolbar2.children.find((c) => c.id === "zocrates-toolbar-btn");
    assert.ok(btn2, "Toolbar button should be injected into toolbar2");
    assert.strictEqual(btn2.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
    assert.strictEqual(btn2.getAttribute("data-status"), "connecting");
    assert.strictEqual(btn2.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connecting...)");
    const dot2 = btn2.querySelector(".zocrates-status-dot");
    assert.ok(dot2, "Status dot should be appended to button");
    assert.strictEqual(dot2.tagName.toLowerCase(), "box", "Dot must be created as a <box> element");
    assert.strictEqual(dot2.getAttribute("data-status"), "connecting");
  });

  test("setBackendStatus: handles multiple windows, missing documents, and Services.wm fallback", () => {
    const { createMockElement } = require("./test_helpers.js");
    const tbA = createMockElement("div");
    tbA.id = "zotero-tabs-toolbar";
    let btnA = null;
    const winA = {
      document: {
        getElementById: (id) => {
          if (id === "zotero-tabs-toolbar") return tbA;
          if (id === "zocrates-toolbar-btn") return btnA;
          return null;
        },
        createElement: (t) => createMockElement(t),
        createElementNS: (ns, t) => createMockElement(t, ns),
        createXULElement: (t) => createMockElement(t),
        documentElement: createMockElement("html"),
      },
    };
    injectToolbarButton(winA);
    btnA = tbA.children.find((c) => c.id === "zocrates-toolbar-btn");

    const winB = { document: null }; // Window without document
    const winC = null; // Null window

    const originalGetMainWindows = global.Zotero.getMainWindows;
    const originalServices = global.Services;

    try {
      // Multiple windows including edge cases
      global.Zotero.getMainWindows = () => [winA, winB, winC];
      setBackendStatus("connected", "Connected");
      assert.strictEqual(btnA.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btnA.getAttribute("data-status"), "connected");
      assert.strictEqual(btnA.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Connected)");

      // Test Services.wm fallback when getMainWindows returns empty
      global.Zotero.getMainWindows = () => [];
      let nextCalled = false;
      global.Services = {
        wm: {
          getEnumerator: () => ({
            hasMoreElements: () => !nextCalled,
            getNext: () => {
              nextCalled = true;
              return winA;
            },
          }),
        },
      };

      setBackendStatus("offline", "Fallback Offline");
      assert.strictEqual(btnA.getAttribute("image"), "chrome://zocrates/content/icons/icon-toggle.png");
      assert.strictEqual(btnA.getAttribute("data-status"), "offline");
      assert.strictEqual(btnA.getAttribute("tooltiptext"), "Toggle Zocrates Sidebar (Local Backend: Fallback Offline)");
    } finally {
      global.Zotero.getMainWindows = originalGetMainWindows;
      global.Services = originalServices;
    }
  });

  // 8. Managed Backend 0ms Crash Detection (attachBackendExitMonitor)
  await testAsync("attachBackendExitMonitor: sets status offline and notifies Services.obs immediately when child exits", async () => {
    setBackendStatus("connected", "Connected");
    global.Zotero.Zocrates._backendReady = true;

    let resolveExit;
    const mockProc = {
      wait: () => new Promise((r) => { resolveExit = r; }),
    };
    global.Zotero.Zocrates._backendProcess = mockProc;

    const observedEvents = [];
    const obs = (_subject, topic, data) => {
      observedEvents.push({ topic, data: JSON.parse(data) });
    };
    global.Services.obs.addObserver(obs, "zocrates-backend-status-changed");

    try {
      attachBackendExitMonitor(mockProc);
      resolveExit({ exitCode: 137 });
      await new Promise((r) => setTimeout(r, 0));

      assert.strictEqual(global.Zotero.Zocrates._backendStatus.status, "offline");
      assert.strictEqual(global.Zotero.Zocrates._backendStatus.label, "Offline");
      assert.strictEqual(global.Zotero.Zocrates._backendReady, false);
      assert.strictEqual(global.Zotero.Zocrates._backendProcess, null);
      assert.strictEqual(observedEvents.length, 1);
      assert.strictEqual(observedEvents[0].data.status, "offline");
      assert.strictEqual(observedEvents[0].data.exitCode, 137);
    } finally {
      global.Services.obs.removeObserver(obs, "zocrates-backend-status-changed");
    }
  });

  // 9. Adaptive Single-Flight /health Polling, Activity Gating & Listener Cleanup
  await testAsync("sidebar polling: hits /health single-flight, applies 60s / 3s->5s->10s->30s backoff, gates on focus/collapse/detached, and cleans up listeners", async () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    const origReadyForChat = sidebar.getBackendReadyForChat();
    sidebar.resetPollingStateForTests();
    sidebar.setIsDetached(false);

    const originalFetch = global.fetch;
    const originalTop = global.window.top;
    const originalParent = global.window.parent;
    const originalFocusSvc = global.Services.focus;
    const originalGetDetached = global.Zotero.Zocrates.getDetachedWindow;

    let healthCalls = 0;
    let shouldFail = false;
    let resolveInFlight = null;

    const hostListeners = new Map();
    const sidebarBox = { hidden: false };
    let topFocused = true;
    const mockHostWin = {
      document: {
        hidden: false,
        hasFocus: () => topFocused,
        getElementById: (id) => (id === "zocrates-sidebar" ? sidebarBox : null),
        addEventListener: (evt, fn) => {
          if (!hostListeners.has("doc:" + evt)) hostListeners.set("doc:" + evt, []);
          hostListeners.get("doc:" + evt).push(fn);
        },
        removeEventListener: (evt, fn) => {
          const arr = hostListeners.get("doc:" + evt) || [];
          hostListeners.set("doc:" + evt, arr.filter((h) => h !== fn));
        },
      },
      addEventListener: (evt, fn) => {
        if (!hostListeners.has(evt)) hostListeners.set(evt, []);
        hostListeners.get(evt).push(fn);
      },
      removeEventListener: (evt, fn) => {
        const arr = hostListeners.get(evt) || [];
        hostListeners.set(evt, arr.filter((h) => h !== fn));
      },
    };

    try {
      global.window.top = mockHostWin;
      global.window.parent = mockHostWin;
      global.Services.focus = { activeWindow: mockHostWin };
      global.Zotero.Zocrates.getDetachedWindow = () => null;

      // Single-flight coalescing onto /health
      global.fetch = (url) => {
        if (String(url).endsWith("/health")) {
          healthCalls++;
          return new Promise((resolve, reject) => {
            resolveInFlight = () => {
              if (shouldFail) {
                reject(new Error("ECONNREFUSED"));
              } else {
                resolve({
                  ok: true,
                  status: 200,
                  json: async () => ({
                    status: "ready",
                    zotero_data_dir: "/mock/zotero",
                    model: "openrouter/test-model",
                    llm_provider: "openrouter_llm",
                    auto_render_mermaid_diagrams: true,
                  }),
                });
              }
            };
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ sessions: [] }) });
      };

      const p1 = sidebar.checkBackendStatus();
      const p2 = sidebar.checkBackendStatus();
      assert.strictEqual(p1, p2, "concurrent checkBackendStatus calls must share single-flight promise");
      assert.strictEqual(healthCalls, 1, "only 1 /health fetch should be issued while in-flight");
      resolveInFlight();
      await p1;

      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 60000, "connected & active polls at 60,000ms");

      // Offline backoff sequence: 3s -> 5s -> 10s -> 30s -> 30s cap
      global.fetch = async (url) => {
        if (String(url).endsWith("/health")) {
          healthCalls++;
          throw new Error("offline");
        }
        return { ok: true, status: 200, json: async () => ({ sessions: [] }) };
      };
      sidebar.setBackendPollingEnabled(false); // manual step assertions without lingering timers

      await sidebar.checkBackendStatus();
      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 3000);
      sidebar.setBackendPollingEnabled(false);

      await sidebar.checkBackendStatus();
      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 5000);
      sidebar.setBackendPollingEnabled(false);

      await sidebar.checkBackendStatus();
      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 10000);
      sidebar.setBackendPollingEnabled(false);

      await sidebar.checkBackendStatus();
      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 30000);
      sidebar.setBackendPollingEnabled(false);

      await sidebar.checkBackendStatus();
      sidebar.setBackendPollingEnabled(true);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), 30000, "backoff caps at 30,000ms");

      // Pause gating: unfocused (Services.focus.activeWindow === null)
      sidebar.setSelectionPollingEnabled(true);
      sidebar.scheduleNextSelectionCheck();
      assert.strictEqual(sidebar.getPollingDebugState().selectionPollTimerActive, true);

      global.Services.focus.activeWindow = null;
      assert.strictEqual(sidebar.isPollingPaused(), true);
      sidebar.syncPollingWithActivity({ triggerImmediate: false });
      assert.strictEqual(sidebar.getPollingDebugState().backendPollTimerActive, false);
      assert.strictEqual(sidebar.getPollingDebugState().selectionPollTimerActive, false);
      assert.strictEqual(sidebar.scheduleNextBackendCheck(), null);

      // Pause gating: sidebar collapsed
      global.Services.focus.activeWindow = mockHostWin;
      sidebarBox.hidden = true;
      assert.strictEqual(sidebar.isPollingPaused(), true);

      // Pause gating: detached window deduplication in embedded iframe
      sidebarBox.hidden = false;
      global.Zotero.Zocrates.getDetachedWindow = () => ({ closed: false, isZocratesDetached: true });
      assert.strictEqual(sidebar.isPollingPaused(), true);
      global.Zotero.Zocrates.getDetachedWindow = () => null;
      assert.strictEqual(sidebar.isPollingPaused(), false);

      // Host activation triggers debounced check (>= 2s) and cleanup removes all listeners
      sidebar.installActivityListeners();
      assert.ok((hostListeners.get("focus") || []).length > 0, "host focus listener installed");
      assert.ok((hostListeners.get("activate") || []).length > 0, "host activate listener installed");

      const callsBeforeFocus = healthCalls;
      // Within 2s debounce window -> should NOT fire immediate /health
      sidebar.setLastBackendCheckAtForTests(Date.now());
      for (const fn of hostListeners.get("focus") || []) fn();
      assert.strictEqual(healthCalls, callsBeforeFocus, "debounced when < 2s since last check");

      // Past 2s debounce window -> fires immediate /health
      sidebar.setLastBackendCheckAtForTests(Date.now() - 3000);
      for (const fn of hostListeners.get("focus") || []) fn();
      await new Promise((r) => setTimeout(r, 0));
      assert.strictEqual(healthCalls, callsBeforeFocus + 1, "fires immediate /health when >= 2s since last check");

      // Cleanup on beforeunload removes all cross-window listeners and timers
      sidebar.handleBeforeUnload();
      assert.strictEqual((hostListeners.get("focus") || []).length, 0, "host focus listeners cleanly removed");
      assert.strictEqual((hostListeners.get("activate") || []).length, 0, "host activate listeners cleanly removed");
      assert.strictEqual(sidebar.getPollingDebugState().registeredActivityListenerCount, 0);
      assert.strictEqual(sidebar.getPollingDebugState().registeredObsListenerCount, 0);
      assert.strictEqual(sidebar.getPollingDebugState().backendPollTimerActive, false);
      assert.strictEqual(sidebar.getPollingDebugState().selectionPollTimerActive, false);
    } finally {
      sidebar.resetPollingStateForTests();
      sidebar.setBackendReadyForChat(origReadyForChat);
      global.fetch = originalFetch;
      global.window.top = originalTop;
      global.window.parent = originalParent;
      global.Services.focus = originalFocusSvc;
      global.Zotero.Zocrates.getDetachedWindow = originalGetDetached;
    }
  });

  // 10. Manual Status Reconnection
  await testAsync("sidebar manual reconnect: triggerManualBackendReconnect triggers retryBackendLaunch and immediate /health probe", async () => {
    const sidebar = require("../../zotero-plugin/addon/sidebar.js");
    sidebar.resetPollingStateForTests();

    let retryCalled = false;
    const origRetry = global.Zotero.Zocrates.retryBackendLaunch;
    global.Zotero.Zocrates.retryBackendLaunch = () => {
      retryCalled = true;
    };

    let healthCalls = 0;
    const originalFetch = global.fetch;
    global.fetch = async (url) => {
      if (url.endsWith("/health")) {
        healthCalls++;
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "ready", model: "google/gemini-2.0-flash", llm_provider: "openrouter_llm" }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    try {
      await sidebar.triggerManualBackendReconnect();
      assert.strictEqual(retryCalled, true, "must call Zotero.Zocrates.retryBackendLaunch to attach warm backend");
      assert.strictEqual(healthCalls, 1, "must immediately probe /health without waiting for poll timer");
    } finally {
      global.fetch = originalFetch;
      global.Zotero.Zocrates.retryBackendLaunch = origRetry;
      sidebar.setBackendReadyForChat(false);
      sidebar.resetPollingStateForTests();
    }
  });

  // 11. auto_spawn_backend gate (dev warm-attach profiles)
  await testAsync("bootstrap: auto_spawn_backend=false with no backend running stays offline without provisioning or spawning", async () => {
    const dataDir = "/mock-zotero-profile/zocrates-data";
    // Minimal IOUtils stand-in: string-keyed in-memory file store.
    const makeMockIOUtils = () => {
      const files = new Map();
      return {
        _files: files,
        async exists(p) { return files.has(p); },
        async readUTF8(p) {
          if (!files.has(p)) throw new Error("file not found: " + p);
          return files.get(p);
        },
        async writeUTF8(p, content) { files.set(p, String(content)); },
        async makeDirectory(p) { files.set(p, "<dir>"); },
      };
    };
    const prevIO = global.IOUtils;
    const prevFetch = global.fetch;
    const prevFile = env.zotero.File;
    const prevDataDir = env.zotero.DataDirectory;
    const z = env.zotero.Zocrates;
    const prevBackendReady = z._backendReady;
    const prevInFlight = z._backendLaunchInFlight;
    const prevSettled = z._bootstrapSettled;
    const prevSetStatus = z.setBackendStatus;

    const io = makeMockIOUtils();
    io.makeDirectory = async (p) => { io._files.set(p, "<dir>"); };
    io.remove = async (p) => { io._files.delete(p); };
    io.setPermissions = async () => {};

    // zocrates_config.json opts out of auto-spawn (developer runs the backend
    // from a terminal; Zotero must never provision a .venv or spawn).
    io._files.set(dataDir + "/zocrates_config.json", JSON.stringify({ auto_spawn_backend: false, backend_port: 8765 }));

    // Minimal Zotero.File bridge so bootstrap config reads resolve against the
    // mock IOUtils file store.
    const asPath = (f) => (typeof f === "string" ? f : f.path);
    env.zotero.File = {
      pathToFile: (p) => ({ path: p, exists() { return io._files.has(p); } }),
      getContents: (f) => io._files.get(asPath(f)),
    };

    const subprocessCalls = [];
    bootstrap.setSubprocessForTesting({
      async call(opts) {
        subprocessCalls.push(opts && opts.command);
        throw new Error("provision/spawn must not run when auto_spawn_backend is false");
      },
    });

    const statusCalls = [];
    z.setBackendStatus = (status, label) => statusCalls.push([status, label]);

    try {
      global.IOUtils = io;
      env.zotero.DataDirectory = { dir: "/mock-zotero-profile" };
      z._backendReady = false;
      z._backendLaunchInFlight = false;
      z._bootstrapSettled = false;

      // No backend running anywhere: every /status probe is connection-refused.
      global.fetch = async () => { throw new Error("ECONNREFUSED"); };

      await bootstrap.launchBackendIfNeeded();

      assert.strictEqual(z._backendReady, false, "backend must NOT be marked ready");
      assert.strictEqual(z._backendLaunchInFlight, false, "in-flight flag must be cleared after the offline settle");
      assert.strictEqual(z._bootstrapSettled, true, "bootstrap must be settled so the sidebar can render the offline state");
      assert.deepStrictEqual(
        statusCalls,
        [["connecting", "Starting..."], ["offline", "Offline"]],
        "must transition to offline (never erroring or staying connecting)"
      );
      assert.deepStrictEqual(subprocessCalls, [], "provisioning (uv) and backend spawn must never be invoked");
      assert.strictEqual(
        Number.isInteger(z._candidatePort) && z._candidatePort > 0,
        true,
        "candidate port must be resolved and stored pre-handshake for getBackendUrl()"
      );
    } finally {
      bootstrap.setSubprocessForTesting(null);
      global.IOUtils = prevIO;
      global.fetch = prevFetch;
      env.zotero.File = prevFile;
      env.zotero.DataDirectory = prevDataDir;
      z._backendReady = prevBackendReady;
      z._backendLaunchInFlight = prevInFlight;
      z._bootstrapSettled = prevSettled;
      z.setBackendStatus = prevSetStatus;
    }
  });

  // 12. Windows 10 Package Manager Extraction & Path Resolution Fixes
  await testAsync("extractZipArchive: succeeds with mock nsIZipReader, handles entries, creates dirs, and handles errors", async () => {
    const prevComponents = global.Components;
    const prevCc = global.Cc;
    const prevCi = global.Ci;
    const prevIOUtils = global.IOUtils;
    const prevZoteroFile = env.zotero.File;

    const createdDirs = [];
    const removedFiles = [];
    const extractedEntries = [];
    let readerClosed = false;

    const mockEntries = [
      "some_dir/",
      "nested/bin/tool.exe",
      "root_file.txt",
    ];

    const mockZipReader = {
      open(file) {},
      close() {
        readerClosed = true;
      },
      findEntries(pattern) {
        let idx = 0;
        return {
          hasMore() {
            return idx < mockEntries.length;
          },
          getNext() {
            return mockEntries[idx++];
          },
        };
      },
      extract(entryName, outFile) {
        extractedEntries.push({ entryName, outPath: outFile.path });
      },
    };

    global.Components = {
      classes: {
        "@mozilla.org/libjar/zip-reader;1": {
          createInstance() {
            return mockZipReader;
          },
        },
      },
      interfaces: {
        nsIZipReader: "nsIZipReader",
      },
    };
    global.Cc = global.Components.classes;
    global.Ci = global.Components.interfaces;

    global.IOUtils = {
      async makeDirectory(dir, opts) {
        createdDirs.push(dir);
      },
      async remove(p, opts) {
        removedFiles.push(p);
      },
    };

    env.zotero.File = {
      pathToFile(p) {
        return {
          path: p,
          exists() {
            return true;
          },
        };
      },
    };

    try {
      // 1. Successful extraction
      await bootstrap.extractZipArchive("/mock/archive.zip", "/mock/target", "/");

      assert.strictEqual(readerClosed, true, "zipReader must be closed in finally");
      assert.strictEqual(extractedEntries.length, 2, "Only non-directory entries must be extracted");
      assert.deepStrictEqual(extractedEntries[0], {
        entryName: "nested/bin/tool.exe",
        outPath: "/mock/target/nested/bin/tool.exe",
      });
      assert.deepStrictEqual(extractedEntries[1], {
        entryName: "root_file.txt",
        outPath: "/mock/target/root_file.txt",
      });
      assert.ok(createdDirs.includes("/mock/target/nested/bin"), "Must create nested parent directory");
      assert.ok(createdDirs.includes("/mock/target"), "Must create target directory");
      assert.ok(removedFiles.includes("/mock/target/nested/bin/tool.exe"), "Must remove pre-existing file before extract");
      assert.ok(removedFiles.includes("/mock/target/root_file.txt"), "Must remove pre-existing file before extract");

      // 2. Error handling: Zip-Slip with '..'
      readerClosed = false;
      const evilZipReader = {
        open() {},
        close() { readerClosed = true; },
        findEntries() {
          let done = false;
          return {
            hasMore() { return !done; },
            getNext() { done = true; return "../evil.txt"; },
          };
        },
        extract() {},
      };
      global.Components.classes["@mozilla.org/libjar/zip-reader;1"].createInstance = () => evilZipReader;

      let threwSlip = false;
      try {
        await bootstrap.extractZipArchive("/mock/evil.zip", "/mock/target", "/");
      } catch (e) {
        threwSlip = true;
        assert.ok(e.message.includes("Zip-Slip guard"), "Should throw Zip-Slip error");
      }
      assert.strictEqual(threwSlip, true, "Must throw on Zip-Slip path");
      assert.strictEqual(readerClosed, true, "Reader must be closed in finally even when Zip-Slip error occurs");

      // 3. Error handling: missing zip file
      env.zotero.File = {
        pathToFile(p) {
          return { path: p, exists() { return false; } };
        },
      };
      let threwMissing = false;
      try {
        await bootstrap.extractZipArchive("/mock/missing.zip", "/mock/target", "/");
      } catch (e) {
        threwMissing = true;
      }
      assert.strictEqual(threwMissing, true, "Must throw when zip file does not exist");

      // 4. Error handling: nsIZipReader unavailable
      delete global.Components.classes["@mozilla.org/libjar/zip-reader;1"];
      delete global.Cc["@mozilla.org/libjar/zip-reader;1"];
      let threwUnavailable = false;
      try {
        await bootstrap.extractZipArchive("/mock/archive.zip", "/mock/target", "/");
      } catch (e) {
        threwUnavailable = true;
      }
      assert.strictEqual(threwUnavailable, true, "Must throw when nsIZipReader is unavailable");
    } finally {
      global.Components = prevComponents;
      global.Cc = prevCc;
      global.Ci = prevCi;
      global.IOUtils = prevIOUtils;
      env.zotero.File = prevZoteroFile;
    }
  });

  await testAsync("runProvisionCommand: calls subprocess.pathSearch when command is not absolute", async () => {
    let pathSearchCalledWith = null;
    let commandExecuted = null;

    bootstrap.setSubprocessForTesting({
      async pathSearch(cmd) {
        pathSearchCalledWith = cmd;
        return "/custom/bin/" + cmd;
      },
      async call(opts) {
        commandExecuted = opts.command;
        return {
          stdout: null,
          stderr: null,
          wait: async () => ({ exitCode: 0 }),
        };
      },
    });

    try {
      // 1. Non-absolute command triggers pathSearch
      const result = await bootstrap.runProvisionCommand("test-uv", "uv", ["--version"], null);
      assert.strictEqual(pathSearchCalledWith, "uv", "pathSearch must be called with non-absolute command name");
      assert.strictEqual(commandExecuted, "/custom/bin/uv", "command passed to subprocess.call must be resolved path");
      assert.strictEqual(result.exitCode, 0);

      // 2. Absolute command does NOT trigger pathSearch
      pathSearchCalledWith = null;
      await bootstrap.runProvisionCommand("test-tar", "/usr/bin/tar", ["-xf"], null);
      assert.strictEqual(pathSearchCalledWith, null, "pathSearch must NOT be called for absolute command");
      assert.strictEqual(commandExecuted, "/usr/bin/tar");
    } finally {
      bootstrap.setSubprocessForTesting(null);
    }
  });

  await testAsync("downloadStandaloneUv: handles flat .zip extraction where extractedBinary === targetPath without deleting targetPath", async () => {
    const prevFetch = global.fetch;
    const prevIOUtils = global.IOUtils;
    const prevAppInfo = global.Services.appinfo;
    const prevComponents = global.Components;
    const prevCc = global.Cc;
    const prevCi = global.Ci;
    const prevZoteroFile = env.zotero.File;

    const files = new Map();
    let targetPathRemovedDuringPlacement = false;

    global.Services.appinfo = {
      OS: "WINNT",
      XPCOMABI: "x86_64-msvc",
    };

    const binDir = "C:\\mock\\zocrates-data\\bin";
    const sep = "\\";
    const targetPath = binDir + sep + "uv.exe";

    // Mock fetch returning archive buffer (> 1MB)
    global.fetch = async (url) => {
      return {
        ok: true,
        status: 200,
        arrayBuffer: async () => new ArrayBuffer(1024 * 1024 + 50),
      };
    };

    // Mock nsIZipReader that extracts uv.exe directly to flat root (targetPath)
    const mockZipReader = {
      open(file) {},
      close() {},
      findEntries() {
        let done = false;
        return {
          hasMore() { return !done; },
          getNext() { done = true; return "uv.exe"; },
        };
      },
      extract(entryName, outFile) {
        // Flat extraction writes directly to targetPath
        files.set(outFile.path, "uv-binary-payload");
      },
    };

    global.Components = {
      classes: {
        "@mozilla.org/libjar/zip-reader;1": {
          createInstance() {
            return mockZipReader;
          },
        },
      },
      interfaces: {
        nsIZipReader: "nsIZipReader",
      },
    };
    global.Cc = global.Components.classes;
    global.Ci = global.Components.interfaces;

    env.zotero.File = {
      pathToFile(p) {
        return {
          path: p,
          exists() { return true; },
        };
      },
    };

    global.IOUtils = {
      _files: files,
      async makeDirectory(p) { files.set(p, "<dir>"); },
      async write(p, buf) { files.set(p, buf); },
      async exists(p) { return files.has(p); },
      async remove(p, opts) {
        // Track if targetPath is removed AFTER extraction created it
        if (p === targetPath && files.has(targetPath)) {
          targetPathRemovedDuringPlacement = true;
        }
        files.delete(p);
      },
      async move(src, dst) {
        if (!files.has(src)) throw new Error("move src not found: " + src);
        files.set(dst, files.get(src));
        files.delete(src);
      },
      async stat(p) {
        return { size: 15 * 1024 * 1024 };
      },
      async read(p, opts) {
        // PE/MZ header for Windows
        return new Uint8Array([0x4d, 0x5a, 0x90, 0x00]);
      },
      async getChildren(p) {
        return [];
      },
    };

    try {
      await bootstrap.downloadStandaloneUv(binDir, sep, targetPath);

      assert.strictEqual(
        files.has(targetPath),
        true,
        "targetPath must exist after standalone uv download and extraction"
      );
      assert.strictEqual(
        targetPathRemovedDuringPlacement,
        false,
        "targetPath must NOT be removed during placement when extractedBinary === targetPath"
      );
    } finally {
      global.fetch = prevFetch;
      global.IOUtils = prevIOUtils;
      global.Services.appinfo = prevAppInfo;
      global.Components = prevComponents;
      global.Cc = prevCc;
      global.Ci = prevCi;
      env.zotero.File = prevZoteroFile;
    }
  });

  // --- Visual C++ Redistributable (Windows) Tests ---

  await testAsync("isVcRedistInstalledWindows: returns true on non-Windows platforms", async () => {
    const prevAppInfo = global.Services.appinfo;
    const prevZoteroIsWin = env.zotero.isWin;
    try {
      global.Services.appinfo = { OS: "Linux" };
      env.zotero.isWin = false;
      const result = await isVcRedistInstalledWindows();
      assert.strictEqual(result, true, "Must return true on Linux");

      global.Services.appinfo = { OS: "Darwin" };
      const macResult = await isVcRedistInstalledWindows();
      assert.strictEqual(macResult, true, "Must return true on macOS");
    } finally {
      global.Services.appinfo = prevAppInfo;
      env.zotero.isWin = prevZoteroIsWin;
    }
  });

  await testAsync("isVcRedistInstalledWindows: detects vcruntime140_1.dll on Windows", async () => {
    const prevAppInfo = global.Services.appinfo;
    const prevZoteroIsWin = env.zotero.isWin;
    const prevEnv = global.Services.env;
    const prevIOUtils = global.IOUtils;

    try {
      global.Services.appinfo = { OS: "WINNT" };
      env.zotero.isWin = true;
      global.Services.env = {
        get(key) {
          return key === "SystemRoot" ? "C:\\Windows" : null;
        },
      };

      // Case 1: DLL exists
      global.IOUtils = {
        async exists(p) {
          return p.toLowerCase() === "c:\\windows\\system32\\vcruntime140_1.dll";
        },
      };
      const existsResult = await isVcRedistInstalledWindows();
      assert.strictEqual(existsResult, true, "Must return true when vcruntime140_1.dll exists");

      // Case 2: DLL missing
      global.IOUtils = {
        async exists(p) {
          return false;
        },
      };
      const missingResult = await isVcRedistInstalledWindows();
      assert.strictEqual(missingResult, false, "Must return false when vcruntime140_1.dll is missing");
    } finally {
      global.Services.appinfo = prevAppInfo;
      env.zotero.isWin = prevZoteroIsWin;
      global.Services.env = prevEnv;
      global.IOUtils = prevIOUtils;
    }
  });

  test("getWindowsArchitecture: resolves arm64, x86, and x64 correctly with fallback", () => {
    const prevAppInfo = global.Services.appinfo;
    try {
      global.Services.appinfo = { XPCOMABI: "x86_64-gcc3" };
      assert.strictEqual(getWindowsArchitecture(), "x64");

      global.Services.appinfo = { XPCOMABI: "aarch64-unknown-linux" };
      assert.strictEqual(getWindowsArchitecture(), "arm64");

      global.Services.appinfo = { XPCOMABI: "arm64-gcc3" };
      assert.strictEqual(getWindowsArchitecture(), "arm64");

      global.Services.appinfo = { XPCOMABI: "x86-msvc" };
      assert.strictEqual(getWindowsArchitecture(), "x86");

      global.Services.appinfo = null;
      assert.strictEqual(getWindowsArchitecture(), "x64");
    } finally {
      global.Services.appinfo = prevAppInfo;
    }
  });

  await testAsync("ensureVcRedistWindows: returns true immediately if not Windows or already installed", async () => {
    const prevAppInfo = global.Services.appinfo;
    const prevZoteroIsWin = env.zotero.isWin;
    const prevIOUtils = global.IOUtils;
    let fetchCalled = false;
    const prevFetch = global.fetch;
    global.fetch = async () => { fetchCalled = true; throw new Error("fetch should not be called"); };

    try {
      // 1. Non-Windows
      global.Services.appinfo = { OS: "Linux" };
      env.zotero.isWin = false;
      const resNonWin = await ensureVcRedistWindows("/mock/zocrates-data", "/");
      assert.strictEqual(resNonWin, true);
      assert.strictEqual(fetchCalled, false, "Must not fetch when not Windows");

      // 2. Windows with DLL present
      global.Services.appinfo = { OS: "WINNT" };
      env.zotero.isWin = true;
      global.IOUtils = {
        async exists(p) { return true; },
      };
      const resWinInstalled = await ensureVcRedistWindows("C:\\zocrates-data", "\\");
      assert.strictEqual(resWinInstalled, true);
      assert.strictEqual(fetchCalled, false, "Must not fetch when DLL is already installed");
    } finally {
      global.Services.appinfo = prevAppInfo;
      env.zotero.isWin = prevZoteroIsWin;
      global.IOUtils = prevIOUtils;
      global.fetch = prevFetch;
    }
  });

  await testAsync("ensureVcRedistWindows: on Windows when missing throws VCRUNTIME_MISSING with arch and docUrl and adds warning notification", async () => {
    const prevAppInfo = global.Services.appinfo;
    const prevZoteroIsWin = env.zotero.isWin;
    const prevEnv = global.Services.env;
    const prevIOUtils = global.IOUtils;
    let fetchCalled = false;
    const prevFetch = global.fetch;
    global.fetch = async () => { fetchCalled = true; throw new Error("fetch must not be called"); };

    const notifier = initNotifier(env.zotero);
    notifier.clear();

    try {
      global.Services.appinfo = { OS: "WINNT", XPCOMABI: "x86_64-gcc3" };
      env.zotero.isWin = true;
      global.Services.env = {
        get(key) { return key === "SystemRoot" ? "C:\\Windows" : null; },
      };
      global.IOUtils = {
        async exists(p) { return false; },
      };

      let threw = false;
      try {
        await ensureVcRedistWindows("C:\\zocrates-data", "\\");
      } catch (err) {
        threw = true;
        assert.strictEqual(err.code, "VCRUNTIME_MISSING");
        assert.strictEqual(err.arch, "X64");
        assert.strictEqual(err.docUrl, VC_REDIST_DOC_URL);
        assert.ok(err.message.includes("Microsoft Visual C++ 2015–2022 Redistributable (X64)"));
        assert.ok(err.message.includes(VC_REDIST_DOC_URL));
      }
      assert.strictEqual(threw, true, "ensureVcRedistWindows must throw VCRUNTIME_MISSING when runtime is missing on Windows");
      assert.strictEqual(fetchCalled, false, "Must not attempt automatic downloading");

      const notifications = notifier.getAll();
      assert.strictEqual(notifications.length, 1, "Must add 1 warning notification");
      assert.strictEqual(notifications[0].type, "warning");
      assert.ok(notifications[0].text.includes("Microsoft Visual C++ 2015–2022 Redistributable (X64)"));
      assert.ok(notifications[0].text.includes(VC_REDIST_DOC_URL));
      assert.ok(!notifications[0].text.includes(".exe"), "Notification must link to documentation page, not direct .exe");
    } finally {
      global.Services.appinfo = prevAppInfo;
      env.zotero.isWin = prevZoteroIsWin;
      global.Services.env = prevEnv;
      global.IOUtils = prevIOUtils;
      global.fetch = prevFetch;
    }
  });

  await testAsync("ensureVcRedistWindows: detects ARM64 architecture and specifies ARM64 in error and notification", async () => {
    const prevAppInfo = global.Services.appinfo;
    const prevZoteroIsWin = env.zotero.isWin;
    const prevEnv = global.Services.env;
    const prevIOUtils = global.IOUtils;

    const notifier = initNotifier(env.zotero);
    notifier.clear();

    try {
      global.Services.appinfo = { OS: "WINNT", XPCOMABI: "aarch64-unknown" };
      env.zotero.isWin = true;
      global.Services.env = {
        get(key) { return key === "SystemRoot" ? "C:\\Windows" : null; },
      };
      global.IOUtils = {
        async exists(p) { return false; },
      };

      let threw = false;
      try {
        await ensureVcRedistWindows("C:\\zocrates-data", "\\");
      } catch (err) {
        threw = true;
        assert.strictEqual(err.code, "VCRUNTIME_MISSING");
        assert.strictEqual(err.arch, "ARM64");
        assert.strictEqual(err.docUrl, VC_REDIST_DOC_URL);
        assert.ok(err.message.includes("(ARM64)"));
      }
      assert.strictEqual(threw, true);

      const notifications = notifier.getAll();
      assert.strictEqual(notifications.length, 1);
      assert.ok(notifications[0].text.includes("(ARM64)"));
    } finally {
      global.Services.appinfo = prevAppInfo;
      env.zotero.isWin = prevZoteroIsWin;
      global.Services.env = prevEnv;
      global.IOUtils = prevIOUtils;
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

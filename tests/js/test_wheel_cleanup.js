/**
 * test_wheel_cleanup.js — Tests for the Phase 6.1 post-verification cleanup:
 *   1. bootstrap.js `filterStaleWheelPayloadPaths` (stale wheel payload sweep)
 *   2. sidebar.js onboarding chat gate (`handleChatSubmit` inert until the
 *      backend is ready; flag flips via `removeBootstrapProgressCard`)
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Wheel Payload Cleanup (filterStaleWheelPayloadPaths) ---");
  setupMockEnvironment();
  const {
    filterStaleWheelPayloadPaths,
    buildWheelInstallArgs,
    getBackendVersionMarkerPath,
    writeBackendVersionMarker,
    readBackendVersionMarker,
  } = require("../../zotero-plugin/addon/bootstrap.js");

  const dir = "/data/zocrates-data";
  const wheelPaths = [
    dir + "/zocrates.whl",
    dir + "/zocrates-0.1.0-py3-none-any.whl",
    dir + "/zocrates-0.2.0-py3-none-any.whl",
  ];
  const protectedPaths = [
    dir + "/constraints.txt",
    dir + "/zocrates_config.json",
    dir + "/uv-x86_64-unknown-linux-gnu.tar.gz",
    dir + "/sessions.db",
    dir + "/.venv",
    dir + "/bin",
    dir + "/backend.port",
    dir + "/backend.pid",
  ];

  test("filterStaleWheelPayloadPaths: matches the unversioned wheel and versioned install targets", () => {
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(wheelPaths), wheelPaths);
    assert.deepStrictEqual(filterStaleWheelPayloadPaths([dir + "/zocrates.whl"]), [dir + "/zocrates.whl"]);
    assert.deepStrictEqual(
      filterStaleWheelPayloadPaths([dir + "/zocrates-0.1.0-py3-none-any.whl"]),
      [dir + "/zocrates-0.1.0-py3-none-any.whl"]
    );
  });

  test("filterStaleWheelPayloadPaths: NEVER matches constraints.txt, config, uv archive, databases, .venv, or sidecars", () => {
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(protectedPaths), []);
    // constraints.txt must be retained (documents the installed dependency matrix)
    assert.deepStrictEqual(filterStaleWheelPayloadPaths([dir + "/constraints.txt"]), []);
  });

  test("filterStaleWheelPayloadPaths: near-miss wheel-shaped names are rejected (non-destructive filter)", () => {
    const adversarial = [
      dir + "/zocrates.whl.bak",
      dir + "/notzocrates.whl",
      dir + "/zocrates-0.1.0-py3-none-any.whl.tmp",
      dir + "/zocrates-1.0.whl.exe",
      dir + "/Zocrates.whl",
      dir + "/zocrates_whl",
    ];
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(adversarial), []);
  });

  test("filterStaleWheelPayloadPaths: mixed listing returns only wheel payloads, preserving order", () => {
    const mixed = protectedPaths.concat(wheelPaths);
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(mixed), wheelPaths);
  });

  test("filterStaleWheelPayloadPaths: matches on basename only (Windows separators and nested paths)", () => {
    assert.deepStrictEqual(
      filterStaleWheelPayloadPaths(["C:\\data\\zocrates-data\\zocrates-1.0.0-py3-none-any.whl"]),
      ["C:\\data\\zocrates-data\\zocrates-1.0.0-py3-none-any.whl"]
    );
    assert.deepStrictEqual(
      filterStaleWheelPayloadPaths(["C:\\data\\zocrates-data\\.venv\\zocrates.whl"]),
      ["C:\\data\\zocrates-data\\.venv\\zocrates.whl"]
    );
  });

  test("filterStaleWheelPayloadPaths: pure function — does not mutate its input and tolerates null/empty", () => {
    const input = [dir + "/zocrates.whl", dir + "/constraints.txt"];
    const snapshot = input.slice();
    const result = filterStaleWheelPayloadPaths(input);
    assert.deepStrictEqual(input, snapshot);
    assert.deepStrictEqual(filterStaleWheelPayloadPaths([]), []);
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(null), []);
    assert.deepStrictEqual(filterStaleWheelPayloadPaths(undefined), []);
    assert.deepStrictEqual(result, [dir + "/zocrates.whl"]);
  });

  test("buildWheelInstallArgs: unchanged by the cleanup addition (Phase 6 contract intact)", () => {
    const venvPython = dir + "/.venv/bin/python";
    const wheelInstallTarget = dir + "/zocrates-1.0.0-py3-none-any.whl";
    assert.deepStrictEqual(
      buildWheelInstallArgs(venvPython, wheelInstallTarget),
      ["pip", "install", "--python", venvPython, wheelInstallTarget]
    );
    assert.deepStrictEqual(
      buildWheelInstallArgs(venvPython, wheelInstallTarget, dir + "/constraints.txt"),
      ["pip", "install", "--python", venvPython, wheelInstallTarget, "--constraint", "file://" + dir + "/constraints.txt"]
    );
  });

  test("getBackendVersionMarkerPath: returns marker path in .venv", () => {
    assert.strictEqual(
      getBackendVersionMarkerPath("/data/zocrates-data", "/"),
      "/data/zocrates-data/.venv/.zocrates-version"
    );
    assert.strictEqual(
      getBackendVersionMarkerPath("C:\\zocrates", "\\"),
      "C:\\zocrates\\.venv\\.zocrates-version"
    );
  });

  await testAsync("writeBackendVersionMarker & readBackendVersionMarker: writes and reads back version", async () => {
    const memoryFS = new Map();
    const oldIOUtils = global.IOUtils;
    global.IOUtils = {
      write: async (path, uint8) => {
        memoryFS.set(path, new TextDecoder().decode(uint8));
      },
      readUTF8: async (path) => {
        if (!memoryFS.has(path)) throw new Error("File not found");
        return memoryFS.get(path);
      },
      exists: async (path) => memoryFS.has(path),
    };
    try {
      assert.strictEqual(await readBackendVersionMarker("/test", "/"), null);
      await writeBackendVersionMarker("/test", "/", "0.2.0");
      assert.strictEqual(await readBackendVersionMarker("/test", "/"), "0.2.0");
    } finally {
      global.IOUtils = oldIOUtils;
    }
  });

  // -------------------------------------------------------------------------
  // Sidebar onboarding chat gate
  // -------------------------------------------------------------------------
  console.log("\n--- Suite: Sidebar Onboarding Chat Gate (handleChatSubmit) ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  const chatInput = env.elementsById.get("chat-input");
  const sendBtn = env.elementsById.get("send-btn");

  test("backendReadyForChat: starts false on a fresh sidebar load (bootstrap not settled)", () => {
    assert.strictEqual(sidebar.getBackendReadyForChat(), false);
  });

  await testAsync("handleChatSubmit: inert while the backend is not ready (no fetch, input preserved, notification added)", async () => {
    let fetchCalls = 0;
    const realFetch = global.fetch;
    global.fetch = async () => { fetchCalls++; throw new Error("mock fetch"); };
    try {
      chatInput.value = "Hello during provisioning";
      await sidebar.handleChatSubmit();
      assert.strictEqual(fetchCalls, 0, "Gated submit must not hit the backend");
      assert.strictEqual(chatInput.value, "Hello during provisioning", "Gated submit must not consume the draft");
    } finally {
      global.fetch = realFetch;
    }
  });

  // NOTE on ordering: backendReadyForChat is module-global and its re-enable is
  // one-shot (the poll guard in removeBootstrapProgressCard), so exactly ONE
  // flip is exercised per module load — done here via the real event wiring,
  // which subsumes a direct removeBootstrapProgressCard() call.
  test("handleBootstrapProgressEvent: a done event flips the gate and re-enables Send through the real wiring", () => {
    // Simulate the disabled state installed by renderOrUpdateBootstrapProgressCard.
    sendBtn.disabled = true;
    sendBtn.title = "Zocrates is starting — the AI engine is installing…";
    sidebar.handleBootstrapProgressEvent({ state: "done" });
    assert.strictEqual(sidebar.getBackendReadyForChat(), true);
    assert.strictEqual(sendBtn.disabled, false);
    assert.strictEqual(sendBtn.title, "Send Message (Ctrl/Cmd + Enter)");
  });

  test("removeBootstrapProgressCard: one-shot guard cannot clobber the streaming stop-mode title on later polls", () => {
    sendBtn.disabled = false;
    sendBtn.classList.add("stop-mode");
    sendBtn.title = "Stop generation";
    sidebar.removeBootstrapProgressCard(); // 10s status-poll success path
    assert.strictEqual(sendBtn.title, "Stop generation", "Poll must not overwrite the stop-mode tooltip");
    assert.strictEqual(sendBtn.disabled, false);
    sendBtn.classList.remove("stop-mode");
    sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
  });

  await testAsync("handleChatSubmit: proceeds past the gate once the backend is ready", async () => {
    let fetchCalls = 0;
    const realFetch = global.fetch;
    global.fetch = async () => { fetchCalls++; throw new Error("mock fetch"); };
    try {
      sidebar.setCurrentSessionId("gate-test-session");
      chatInput.value = "Hello after backend ready";
      await sidebar.handleChatSubmit();
      assert.ok(fetchCalls >= 1, "Ungated submit must reach the backend");
      assert.strictEqual(chatInput.value, "", "Ungated submit consumes the draft");
    } finally {
      global.fetch = realFetch;
      sidebar.setCurrentSessionId(null);
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

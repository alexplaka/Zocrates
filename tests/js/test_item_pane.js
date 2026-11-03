/**
 * test_item_pane.js — Tests for itemPane.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: ZocratesItemPane ---");
  const env = setupMockEnvironment();
  const ZocratesItemPane = require("../../zotero-plugin/addon/lib/itemPane.js");

  // 1. getTargetAttachmentKey
  await testAsync("getTargetAttachmentKey: returns attachment key for attachment items", async () => {
    const attItem = {
      key: "ATT_KEY_123",
      isAttachment: () => true,
      isRegularItem: () => false,
    };
    const key = await ZocratesItemPane.getTargetAttachmentKey(attItem);
    assert.strictEqual(key, "ATT_KEY_123");
  });

  await testAsync("getTargetAttachmentKey: returns regular item key for top-level paper items", async () => {
    const regularItem = {
      key: "ITEM_KEY_456",
      isAttachment: () => false,
      isRegularItem: () => true,
    };
    const key = await ZocratesItemPane.getTargetAttachmentKey(regularItem);
    assert.strictEqual(key, "ITEM_KEY_456");
  });

  await testAsync("getTargetAttachmentKey: returns null for null or unsupported items", async () => {
    assert.strictEqual(await ZocratesItemPane.getTargetAttachmentKey(null), null);

    const unsupportedItem = {
      key: "OTHER_789",
      isAttachment: () => false,
      isRegularItem: () => false,
    };
    assert.strictEqual(await ZocratesItemPane.getTargetAttachmentKey(unsupportedItem), null);
  });

  // 2. Server status error rendering
  await testAsync("onRender: renders 'No convertible document attachment found' when item key is null", async () => {
    const body = createMockElement("div");
    body.ownerDocument = env.document;

    await ZocratesItemPane.onRender({ body, item: null });

    assert.ok(body.textContent.includes("No convertible document attachment found"));
  });

  await testAsync("onRender: renders 'Zocrates Server Disconnected' banner on network fetch error", async () => {
    const body = createMockElement("div");
    body.ownerDocument = env.document;

    const mockItem = {
      key: "ITEM_TEST_ERR",
      isAttachment: () => false,
      isRegularItem: () => true,
    };

    const originalFetch = global.fetch;
    global.fetch = async () => {
      throw new Error("Connection refused: 127.0.0.1:8765");
    };

    try {
      await ZocratesItemPane.onRender({ body, item: mockItem });

      assert.ok(
        body.textContent.includes("Zocrates Server Disconnected"),
        "Body should display 'Zocrates Server Disconnected'"
      );
      assert.ok(
        body.textContent.includes("Connection refused"),
        "Body should include error detail text"
      );
    } finally {
      global.fetch = originalFetch;
    }
  });

  await testAsync("onRender: renders error banner when backend status endpoint returns HTTP 500", async () => {
    const body = createMockElement("div");
    body.ownerDocument = env.document;

    const mockItem = {
      key: "ITEM_TEST_500",
      isAttachment: () => true,
      isRegularItem: () => false,
    };

    const originalFetch = global.fetch;
    global.fetch = async (url) => {
      return {
        ok: false,
        status: 500,
        text: async () => "Internal Server Error in database query",
        statusText: "Internal Server Error",
      };
    };

    try {
      await ZocratesItemPane.onRender({ body, item: mockItem });

      assert.ok(
        body.textContent.includes("Zocrates Server Disconnected"),
        "Body should display disconnected error banner"
      );
      assert.ok(
        body.textContent.includes("HTTP 500"),
        "Body should include HTTP 500 status message"
      );
    } finally {
      global.fetch = originalFetch;
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

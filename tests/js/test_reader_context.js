/**
 * test_reader_context.js — Tests for ReaderContext and Snippet Capture.
 */

const assert = require("assert");
const { test, testAsync, setupMockEnvironment } = require("./test_helpers.js");
const { ReaderContext, PLUGIN_ID } = require("../../zotero-plugin/addon/lib/readerContext.js");

function createMockReader(opts = {}) {
  const item = opts.item || {
    key: "ATT123",
    isAttachment: () => true,
    parentItem: {
      key: "PARENT456",
      getField: (f) => (f === "title" ? "Attention Is All You Need" : ""),
    },
    getField: (f) => "Attachment Title",
  };

  return {
    _item: item,
    _iframeWindow: opts.iframeWindow || null,
    _internalReader: opts.internalReader || null,
    _state: opts.state || null,
  };
}

function createMockCanvas(doc) {
  return {
    width: 0,
    height: 0,
    getContext(type) {
      return {
        fillStyle: "",
        fillRect: () => {},
        drawImage: () => {},
      };
    },
    toDataURL(mime, quality) {
      return `data:${mime};base64,mock_${this.width}x${this.height}_q${quality}`;
    },
  };
}

async function runTests() {
  console.log("\n--- Suite: Reader Context & Snippet Capture ---");
  const env = setupMockEnvironment();

  const origGetMainWindow = global.Zotero.getMainWindow;
  const origReader = global.Zotero.Reader;
  const origItems = global.Zotero.Items;
  const origAnnotations = global.Zotero.Annotations;
  const origImage = global.Image;
  const origCreateElement = env.document.createElement;

  try {
    // Mock Zotero.getMainWindow to return document
    global.Zotero.getMainWindow = () => ({
      document: env.document,
      ZoteroPane: null,
    });
  // Mock Zotero.Reader event registry
  const readerListeners = new Map();
  global.Zotero.Reader = {
    registerEventListener(type, handler, pluginId) {
      if (!readerListeners.has(type)) readerListeners.set(type, []);
      readerListeners.get(type).push({ handler, pluginId });
    },
    unregisterEventListener(type, handler) {
      if (readerListeners.has(type)) {
        const filtered = readerListeners.get(type).filter((entry) => entry.handler !== handler);
        readerListeners.set(type, filtered);
      }
    },
  };

  // Mock DOM canvas creation for compression tests
  const origCreateElement = env.document.createElement;
  env.document.createElement = function (tagName) {
    if (tagName.toLowerCase() === "canvas") {
      return createMockCanvas(env.document);
    }
    return origCreateElement.call(env.document, tagName);
  };

  // Mock global Image constructor
  global.Image = class MockImage {
    constructor() {
      this.width = 1600;
      this.height = 1200;
      this._src = "";
    }
    set src(val) {
      this._src = val;
      if (val === "invalid_source") {
        setTimeout(() => this.onerror && this.onerror(new Error("Image decode failed")), 0);
      } else {
        setTimeout(() => this.onload && this.onload(), 0);
      }
    }
    get src() {
      return this._src;
    }
  };

  // 1. init() registers event listeners
  test("init: registers createViewContextMenu and createAnnotationContextMenu with PLUGIN_ID", () => {
    ReaderContext.init();
    const viewListeners = readerListeners.get("createViewContextMenu") || [];
    const annListeners = readerListeners.get("createAnnotationContextMenu") || [];

    assert.strictEqual(viewListeners.length, 1);
    assert.strictEqual(viewListeners[0].pluginId, PLUGIN_ID);
    assert.strictEqual(annListeners.length, 1);
    assert.strictEqual(annListeners[0].pluginId, PLUGIN_ID);
    assert.ok(ReaderContext._notifierId, "ReaderContext._notifierId must be set");
  });

  // 2. destroy() unregisters event listeners
  test("destroy: unregisters view and annotation context menu handlers cleanly", () => {
    ReaderContext.destroy();
    const viewListeners = readerListeners.get("createViewContextMenu") || [];
    const annListeners = readerListeners.get("createAnnotationContextMenu") || [];

    assert.strictEqual(viewListeners.length, 0);
    assert.strictEqual(annListeners.length, 0);
    assert.strictEqual(ReaderContext._notifierId, null, "ReaderContext._notifierId must be null after destroy");

    // Re-init for subsequent handler tests
    ReaderContext.init();
  });

  // 3. _handleViewContextMenu text extraction tiers
  test("_handleViewContextMenu: extracts selection from Tier 1 (_primaryView._iframeWindow)", () => {
    let capturedSnippet = null;
    global.Zotero.Zocrates = {
      addSnippetToContext: (s) => { capturedSnippet = s; },
    };

    const reader = createMockReader({
      internalReader: {
        _primaryView: {
          _iframeWindow: {
            getSelection: () => ({ toString: () => "Selected text from Tier 1" }),
          },
        },
      },
    });

    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: { pageIndex: 3 },
      append: (item) => { appended = item; },
    });

    assert.ok(appended, "Context menu item should be appended");
    assert.strictEqual(appended.label, "Zocrates: Add to Context");

    appended.onCommand();
    assert.ok(capturedSnippet);
    assert.strictEqual(capturedSnippet.text, "Selected text from Tier 1");
    assert.strictEqual(capturedSnippet.page, 4); // 0-indexed pageIndex + 1
    assert.strictEqual(capturedSnippet.paper_title, "Attention Is All You Need");
    assert.strictEqual(capturedSnippet.paperTitle, "Attention Is All You Need");
    assert.strictEqual(capturedSnippet.item_key, "ATT123");
    assert.strictEqual(capturedSnippet.parent_key, "PARENT456");
  });

  test("_handleViewContextMenu: extracts selection from Tier 2 (_lastView._iframeWindow)", () => {
    const reader = createMockReader({
      internalReader: {
        _lastView: {
          _iframeWindow: {
            getSelection: () => ({ toString: () => "Selected text from Tier 2" }),
          },
        },
      },
    });

    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: {},
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
  });

  test("_handleViewContextMenu: extracts selection from Tier 3 (reader._iframeWindow)", () => {
    const reader = createMockReader({
      iframeWindow: {
        getSelection: () => ({ toString: () => "Selected text from Tier 3" }),
      },
    });

    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: {},
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
  });

  test("_handleViewContextMenu: extracts selection from Tier 4 (primaryViewSelectionPopup)", () => {
    const reader = createMockReader({
      internalReader: {
        _state: {
          primaryViewSelectionPopup: {
            annotation: { text: "Popup selection text" },
          },
        },
      },
    });

    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: {},
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
  });

  test("_handleViewContextMenu: extracts selection from Tier 6 (params.selectionText)", () => {
    const reader = createMockReader();
    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: { selectionText: "Params selection text" },
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
  });

  test("_handleViewContextMenu: ignores empty or whitespace-only selection", () => {
    const reader = createMockReader();
    let appended = null;
    ReaderContext._handleViewContextMenu({
      reader,
      params: { selectionText: "   " },
      append: (item) => { appended = item; },
    });

    assert.strictEqual(appended, null, "Should not append menu item when selection is empty");
  });

  // 4. _handleAnnotationContextMenu image annotation
  await testAsync("_handleAnnotationContextMenu: detects image annotation and captures area snippet", async () => {
    let capturedSnippet = null;
    global.Zotero.Zocrates = {
      addSnippetToContext: (s) => { capturedSnippet = s; },
    };

    const imageAnn = {
      id: "ANN_IMG_1",
      type: "image",
      pageIndex: 2,
      image: "data:image/png;base64,mockpngdata",
    };

    const reader = createMockReader({
      internalReader: {
        _state: {
          annotations: [imageAnn],
        },
      },
    });

    let appended = null;
    ReaderContext._handleAnnotationContextMenu({
      reader,
      params: { ids: ["ANN_IMG_1"] },
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
    assert.strictEqual(appended.label, "Zocrates: Add Area to Context");

    await appended.onCommand();
    assert.ok(capturedSnippet);
    assert.strictEqual(capturedSnippet.type, "image");
    assert.strictEqual(capturedSnippet.page, 3);
    assert.ok(capturedSnippet.image_data);
    assert.ok(capturedSnippet.imageData);
    assert.ok(capturedSnippet.thumbnail_data);
    assert.ok(capturedSnippet.thumbnailData);
    assert.strictEqual(capturedSnippet.width, 1200); // scaled down from 1600x1200 to maxDim 1200
    assert.strictEqual(capturedSnippet.height, 900);
  });

  // 5. _handleAnnotationContextMenu text annotation (when !imageAnn)
  test("_handleAnnotationContextMenu: detects text annotation and appends text context item", () => {
    let capturedSnippet = null;
    global.Zotero.Zocrates = {
      addSnippetToContext: (s) => { capturedSnippet = s; },
    };

    const textAnn = {
      id: "ANN_TXT_1",
      type: "highlight",
      text: "Highlighted key finding",
      pageIndex: 5,
    };

    const reader = createMockReader({
      state: {
        annotations: [textAnn],
      },
    });

    let appended = null;
    ReaderContext._handleAnnotationContextMenu({
      reader,
      params: { ids: ["ANN_TXT_1"] },
      append: (item) => { appended = item; },
    });

    assert.ok(appended);
    assert.strictEqual(appended.label, "Zocrates: Add to Context");

    appended.onCommand();
    assert.ok(capturedSnippet);
    assert.strictEqual(capturedSnippet.type, "text");
    assert.strictEqual(capturedSnippet.text, "Highlighted key finding");
    assert.strictEqual(capturedSnippet.page, 6);
  });

  // 6. extractAndCompressAnnotationImage fallbacks
  await testAsync("extractAndCompressAnnotationImage: fallback 1 queries Zotero.Annotations API", async () => {
    const annItem = { id: "ANN_FALLBACK_1" };
    global.Zotero.Items = {
      getByLibraryAndKey: (libId, key) => (key === "ANN_FALLBACK_1" ? annItem : null),
    };
    global.Zotero.Annotations = {
      toJSON: async (item) => ({ image: "data:image/png;base64,from_annotations_api" }),
    };

    const reader = createMockReader({
      item: {
        key: "ATT_FALLBACK",
        libraryID: 1,
        isAttachment: () => true,
      },
    });

    const result = await ReaderContext.extractAndCompressAnnotationImage(reader, { id: "ANN_FALLBACK_1" });
    assert.ok(result.imageData);
    assert.ok(result.thumbnailData);
  });

  await testAsync("extractAndCompressAnnotationImage: fallback 2 queries reader state annotations", async () => {
    const reader = createMockReader({
      internalReader: {
        _state: {
          annotations: [
            { id: "ANN_FALLBACK_2", image: "data:image/png;base64,from_internal_state" },
          ],
        },
      },
    });

    const result = await ReaderContext.extractAndCompressAnnotationImage(reader, { id: "ANN_FALLBACK_2" });
    assert.ok(result.imageData);
  });

  await testAsync("extractAndCompressAnnotationImage: throws when image cannot be found", async () => {
    const reader = createMockReader();
    let threw = false;
    try {
      await ReaderContext.extractAndCompressAnnotationImage(reader, { id: "NON_EXISTENT" });
    } catch (e) {
      threw = true;
      assert.ok(e.message.includes("Could not extract image"));
    }
    assert.strictEqual(threw, true);
  });

  // 7. Parent metadata resolution
  test("resolveParentPaperMetadata: handles standalone top-level item with creators", () => {
    const reader = {
      _item: {
        key: "TOP_ITEM",
        isAttachment: () => false,
        getField: (f) => "Standalone Paper Title",
        getCreatorsJSON: () => [{ lastName: "Jonsson" }, { lastName: "Sulem" }, { lastName: "Stefansson" }],
      },
    };
    const meta = ReaderContext.resolveParentPaperMetadata(reader);
    assert.strictEqual(meta.itemKey, "TOP_ITEM");
    assert.strictEqual(meta.parentKey, "TOP_ITEM");
    assert.strictEqual(meta.paperTitle, "Standalone Paper Title");
    assert.strictEqual(meta.authorStr, "Jonsson et al.");
    assert.deepStrictEqual(meta.authors, ["Jonsson", "Sulem", "Stefansson"]);
  });

  test("resolveParentPaperMetadata: handles null reader item gracefully", () => {
    const meta = ReaderContext.resolveParentPaperMetadata(null);
    assert.strictEqual(meta.itemKey, null);
    assert.strictEqual(meta.parentKey, null);
    assert.strictEqual(meta.paperTitle, "Untitled");
  });

  // 8. Bootstrap snippet subscriber integration
  test("bootstrap snippet subscriber: delivers snippets to all registered subscribers", () => {
    const subscribers = new Set();
    let sidebarOpened = false;

    const mockOpenSidebar = () => {
      sidebarOpened = true;
    };

    const mockAddSnippetToContext = function (snippet) {
      for (const sub of subscribers) {
        try { sub(snippet); } catch (e) {}
      }
      try { mockOpenSidebar(); } catch (e) {}
    };

    const received1 = [];
    const received2 = [];
    subscribers.add((s) => received1.push(s));
    subscribers.add((s) => received2.push(s));

    const testSnippet = { id: "snip_test_1", type: "text", text: "Hello from test" };
    mockAddSnippetToContext(testSnippet);

    assert.strictEqual(received1.length, 1);
    assert.strictEqual(received2.length, 1);
    assert.strictEqual(received1[0].id, "snip_test_1");
    assert.strictEqual(sidebarOpened, true);
  });

  // 9. captureAreaSnippet populates annotation_id and annotationId on snippet
  await testAsync("captureAreaSnippet: populates annotation_id and annotationId on snippet", async () => {
    let capturedSnippet = null;
    global.Zotero.Zocrates = {
      addSnippetToContext: (s) => { capturedSnippet = s; },
    };

    const imageAnn = {
      id: "ANN_AREA_42",
      type: "image",
      pageIndex: 1,
      image: "data:image/png;base64,mockpngdata",
    };

    const reader = createMockReader();
    const snippet = await ReaderContext.captureAreaSnippet(reader, imageAnn);

    assert.ok(snippet);
    assert.strictEqual(snippet.annotation_id, "ANN_AREA_42");
    assert.strictEqual(snippet.annotationId, "ANN_AREA_42");
    assert.strictEqual(capturedSnippet.annotation_id, "ANN_AREA_42");
    assert.strictEqual(capturedSnippet.annotationId, "ANN_AREA_42");
  });

  // 10. ReaderContext.notify triggers handleAnnotationModify and dispatches to Zotero.Zocrates.updateSnippet
  await testAsync("ReaderContext.notify: triggers handleAnnotationModify and dispatches to Zotero.Zocrates.updateSnippet", async () => {
    let updatedPayload = null;
    global.Zotero.Zocrates = {
      updateSnippet: (p) => { updatedPayload = p; },
    };

    const mockItem = {
      id: 999,
      key: "ANN_KEY_999",
      isAnnotation: () => true,
      annotationType: "image",
    };

    global.Zotero.Items = {
      get: (id) => (id === 999 ? mockItem : null),
    };

    global.Zotero.Annotations = {
      toJSON: async (item) => ({
        image: "data:image/png;base64,mockresizedimage",
      }),
    };

    // Should ignore non-modify action
    await ReaderContext.notify("add", "item", [999]);
    assert.strictEqual(updatedPayload, null, "Should ignore non-modify action");

    // Should ignore non-item type
    await ReaderContext.notify("modify", "collection", [999]);
    assert.strictEqual(updatedPayload, null, "Should ignore non-item type");

    // Should ignore non-array ids
    await ReaderContext.notify("modify", "item", "not-an-array");
    assert.strictEqual(updatedPayload, null, "Should ignore non-array ids");

    // Should handle modify on image annotation item
    await ReaderContext.notify("modify", "item", [999]);

    assert.ok(updatedPayload, "updateSnippet must have been called");
    assert.strictEqual(updatedPayload.annotation_id, "ANN_KEY_999");
    assert.strictEqual(updatedPayload.annotationId, "ANN_KEY_999");
    assert.ok(updatedPayload.image_data);
    assert.ok(updatedPayload.imageData);
    assert.ok(updatedPayload.thumbnail_data);
    assert.ok(updatedPayload.thumbnailData);
    assert.strictEqual(updatedPayload.width, 1200);
    assert.strictEqual(updatedPayload.height, 900);
  });
  } finally {
    // Cleanup and restore mocked globals
    env.document.createElement = origCreateElement;
    global.Zotero.getMainWindow = origGetMainWindow;
    global.Zotero.Reader = origReader;
    global.Zotero.Items = origItems;
    global.Zotero.Annotations = origAnnotations;
    global.Image = origImage;
  }
}

module.exports = runTests;

if (require.main === module) {
  const { printTestSummary, resetTestStats } = require("./test_helpers.js");
  resetTestStats();
  runTests().then(() => {
    const ok = printTestSummary();
    if (!ok) process.exit(1);
  });
}

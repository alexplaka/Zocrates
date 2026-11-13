/**
 * test_auto_converter.js — Tests for autoConverter.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: AutoConverter ---");
  setupMockEnvironment();
  const AutoConverter = require("../../zotero-plugin/addon/lib/autoConverter.js");

  // 1. Suppression Controls
  test("suppression: suppressBatch toggles _suppressAll and clears suppression sets on enable=false", () => {
    AutoConverter.destroy();
    assert.strictEqual(AutoConverter._suppressAll, false);

    AutoConverter.suppressKey("KEY1");
    AutoConverter.suppressParentID(99);
    assert.strictEqual(AutoConverter._suppressKeys.has("KEY1"), true);
    assert.strictEqual(AutoConverter._suppressParentIDs.has(99), true);

    AutoConverter.suppressBatch(true);
    assert.strictEqual(AutoConverter._suppressAll, true);

    AutoConverter.suppressBatch(false);
    assert.strictEqual(AutoConverter._suppressAll, false);
    assert.strictEqual(AutoConverter._suppressKeys.size, 0);
    assert.strictEqual(AutoConverter._suppressParentIDs.size, 0);
  });

  test("suppression: enqueueItem rejects items when _suppressAll or key/parentID is suppressed", () => {
    AutoConverter.destroy();

    const attItem = {
      key: "ATT_SUPPRESS_TEST",
      parentItemID: 42,
      isAttachment: () => true,
      attachmentContentType: "application/pdf",
    };

    // Case 1: _suppressAll
    AutoConverter.suppressBatch(true);
    AutoConverter.enqueueItem(attItem);
    assert.strictEqual(AutoConverter._queue.length, 0);
    AutoConverter.suppressBatch(false);

    // Case 2: _suppressKeys
    AutoConverter.suppressKey("ATT_SUPPRESS_TEST");
    AutoConverter.enqueueItem(attItem);
    assert.strictEqual(AutoConverter._queue.length, 0);
    AutoConverter.unsuppressKey("ATT_SUPPRESS_TEST");

    // Case 3: _suppressParentIDs
    AutoConverter.suppressParentID(42);
    AutoConverter.enqueueItem(attItem);
    assert.strictEqual(AutoConverter._queue.length, 0);
    AutoConverter.unsuppressParentID(42);
  });

  // 2. isConvertibleAttachment
  test("isConvertibleAttachment: identifies convertible formats (PDF, HTML, MD, DOCX, PPTX)", () => {
    const formats = [
      { contentType: "application/pdf", filename: "paper.pdf" },
      { contentType: "text/html", filename: "page.html" },
      { contentType: "text/markdown", filename: "notes.md" },
      { contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename: "doc.docx" },
      { contentType: "application/vnd.openxmlformats-officedocument.presentationml.presentation", filename: "slides.pptx" },
      { contentType: "text/plain", filename: "readme.txt" },
    ];

    for (const f of formats) {
      const item = {
        isAttachment: () => true,
        attachmentContentType: f.contentType,
        getFilename: () => f.filename,
      };
      assert.strictEqual(
        AutoConverter.isConvertibleAttachment(item),
        true,
        `Expected ${f.filename} (${f.contentType}) to be convertible`
      );
    }
  });

  test("isConvertibleAttachment: rejects unconvertible items (.exe, .zip, non-attachments)", () => {
    const nonConvertible = [
      { contentType: "application/x-msdownload", filename: "setup.exe" },
      { contentType: "application/zip", filename: "data.zip" },
      { contentType: "image/png", filename: "photo.png" },
      { contentType: "audio/mpeg", filename: "podcast.mp3" },
    ];

    for (const f of nonConvertible) {
      const item = {
        isAttachment: () => true,
        attachmentContentType: f.contentType,
        getFilename: () => f.filename,
      };
      assert.strictEqual(
        AutoConverter.isConvertibleAttachment(item),
        false,
        `Expected ${f.filename} (${f.contentType}) to be rejected`
      );
    }

    // Null or non-attachment
    assert.strictEqual(AutoConverter.isConvertibleAttachment(null), false);
    assert.strictEqual(AutoConverter.isConvertibleAttachment({ isAttachment: () => false }), false);
  });

  // 3. resolveItemTitle
  test("resolveItemTitle: prioritizes parent title, attachment title, filename, and key", () => {
    // 1. Parent title priority
    const itemWithParent = {
      key: "ATT1",
      parentItem: { getField: (f) => (f === "title" ? "Parent Paper Title" : null) },
      getField: () => "Attachment Title",
      getFilename: () => "file.pdf",
    };
    assert.strictEqual(AutoConverter.resolveItemTitle(itemWithParent), "Parent Paper Title");

    // 2. Attachment title priority (ignoring generic "PDF" / "Snapshot")
    const itemWithAttTitle = {
      key: "ATT2",
      parentItem: null,
      getField: (f) => (f === "title" ? "Specific Attachment Title" : null),
      getFilename: () => "file.pdf",
    };
    assert.strictEqual(AutoConverter.resolveItemTitle(itemWithAttTitle), "Specific Attachment Title");

    // 3. Filename priority when title is "PDF"
    const itemWithGenericTitle = {
      key: "ATT3",
      parentItem: null,
      getField: (f) => (f === "title" ? "PDF" : null),
      getFilename: () => "research_report.pdf",
    };
    assert.strictEqual(AutoConverter.resolveItemTitle(itemWithGenericTitle), "research_report.pdf");

    // 4. Fallback to item key
    const itemOnlyKey = {
      key: "KEY_XYZ_123",
      parentItem: null,
      getField: () => null,
      getFilename: () => null,
    };
    assert.strictEqual(AutoConverter.resolveItemTitle(itemOnlyKey), "KEY_XYZ_123");

    // 5. Null fallback
    assert.strictEqual(AutoConverter.resolveItemTitle(null), "Document");
  });

  // 4. getConverterDisplayName
  test("getConverterDisplayName: resolves display labels for supported engines", () => {
    assert.strictEqual(AutoConverter.getConverterDisplayName("datalab"), "Datalab (Cloud)");
    assert.strictEqual(AutoConverter.getConverterDisplayName("docling_local"), "Docling (Local)");
    assert.strictEqual(AutoConverter.getConverterDisplayName("docling"), "Docling (Local)");
    assert.strictEqual(AutoConverter.getConverterDisplayName("markitdown"), "MarkItDown (Local)");
    assert.strictEqual(AutoConverter.getConverterDisplayName("custom_engine"), "custom_engine");
  });

  // 5. Deletion & Rematerialization
  await testAsync("notify: trash/delete on attachment of childless parent triggers rematerialize ingest", async () => {
    AutoConverter.destroy();
    const calls = [];
    const origFetch = global.fetch;
    global.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => ({ status: "success" }) };
    };

    try {
      const parentItem = new Zotero.Item("journalArticle");
      parentItem.key = "PARENT_CHILDLESS_KEY";
      await parentItem.saveTx();

      const attItem = new Zotero.Item("attachment");
      attItem.key = "ATT_TO_DELETE";
      attItem.parentItemID = parentItem.id;
      attItem.attachmentContentType = "application/pdf";
      attItem.deleted = true;
      await attItem.saveTx();

      parentItem._attachments = [attItem];

      AutoConverter.notify("delete", "item", [attItem.id]);

      const deleteCalls = calls.filter((c) => c.url.includes("/items/delete"));
      const ingestCalls = calls.filter((c) => c.url.includes("/items/ingest"));

      assert.strictEqual(deleteCalls.length, 1);
      assert.deepStrictEqual(JSON.parse(deleteCalls[0].opts.body), { item_keys: ["ATT_TO_DELETE"] });
      assert.strictEqual(ingestCalls.length, 1);
      assert.deepStrictEqual(JSON.parse(ingestCalls[0].opts.body), { item_keys: ["PARENT_CHILDLESS_KEY"] });
    } finally {
      global.fetch = origFetch;
    }
  });

  await testAsync("notify: trash/delete on attachment does NOT rematerialize parent if another convertible attachment remains", async () => {
    AutoConverter.destroy();
    const calls = [];
    const origFetch = global.fetch;
    global.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => ({ status: "success" }) };
    };

    try {
      const parentItem = new Zotero.Item("journalArticle");
      parentItem.key = "PARENT_WITH_REMAINING_KEY";
      await parentItem.saveTx();

      const attItem1 = new Zotero.Item("attachment");
      attItem1.key = "ATT_DEL";
      attItem1.parentItemID = parentItem.id;
      attItem1.attachmentContentType = "application/pdf";
      attItem1.deleted = true;
      await attItem1.saveTx();

      const attItem2 = new Zotero.Item("attachment");
      attItem2.key = "ATT_ALIVE";
      attItem2.parentItemID = parentItem.id;
      attItem2.attachmentContentType = "application/pdf";
      attItem2.deleted = false;
      await attItem2.saveTx();

      parentItem._attachments = [attItem1, attItem2];

      AutoConverter.notify("delete", "item", [attItem1.id]);

      const deleteCalls = calls.filter((c) => c.url.includes("/items/delete"));
      const ingestCalls = calls.filter((c) => c.url.includes("/items/ingest"));

      assert.strictEqual(deleteCalls.length, 1);
      assert.deepStrictEqual(JSON.parse(deleteCalls[0].opts.body), { item_keys: ["ATT_DEL"] });
      assert.strictEqual(ingestCalls.length, 0);
    } finally {
      global.fetch = origFetch;
    }
  });

  await testAsync("notify: delete on [parent, attachment] in same batch dispatches zero /items/ingest calls", async () => {
    AutoConverter.destroy();
    const calls = [];
    const origFetch = global.fetch;
    global.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => ({ status: "success" }) };
    };

    try {
      const parentItem = new Zotero.Item("journalArticle");
      parentItem.key = "PARENT_BATCH_DEL_KEY";
      parentItem.deleted = true;
      await parentItem.saveTx();

      const attItem = new Zotero.Item("attachment");
      attItem.key = "ATT_BATCH_DEL_KEY";
      attItem.parentItemID = parentItem.id;
      attItem.attachmentContentType = "application/pdf";
      attItem.deleted = true;
      await attItem.saveTx();

      parentItem._attachments = [attItem];

      AutoConverter.notify("delete", "item", [parentItem.id, attItem.id]);

      const deleteCalls = calls.filter((c) => c.url.includes("/items/delete"));
      const ingestCalls = calls.filter((c) => c.url.includes("/items/ingest"));

      assert.strictEqual(deleteCalls.length, 1);
      assert.strictEqual(ingestCalls.length, 0);
    } finally {
      global.fetch = origFetch;
    }
  });

  await testAsync("notify: linked URL attachment (attachmentLinkMode === 3) does NOT block rematerialization when PDF is deleted", async () => {
    AutoConverter.destroy();
    const calls = [];
    const origFetch = global.fetch;
    global.fetch = async (url, opts) => {
      calls.push({ url, opts });
      return { ok: true, json: async () => ({ status: "success" }) };
    };

    try {
      const parentItem = new Zotero.Item("journalArticle");
      parentItem.key = "PARENT_WITH_LINKED_URL_KEY";
      await parentItem.saveTx();

      const pdfAtt = new Zotero.Item("attachment");
      pdfAtt.key = "ATT_PDF_DEL";
      pdfAtt.parentItemID = parentItem.id;
      pdfAtt.attachmentContentType = "application/pdf";
      pdfAtt.deleted = true;
      await pdfAtt.saveTx();

      const urlAtt = new Zotero.Item("attachment");
      urlAtt.key = "ATT_LINKED_URL";
      urlAtt.parentItemID = parentItem.id;
      urlAtt.attachmentContentType = "text/html";
      urlAtt.attachmentLinkMode = 3;
      urlAtt.deleted = false;
      await urlAtt.saveTx();

      parentItem._attachments = [pdfAtt, urlAtt];

      AutoConverter.notify("delete", "item", [pdfAtt.id]);

      const deleteCalls = calls.filter((c) => c.url.includes("/items/delete"));
      const ingestCalls = calls.filter((c) => c.url.includes("/items/ingest"));

      assert.strictEqual(deleteCalls.length, 1);
      assert.deepStrictEqual(JSON.parse(deleteCalls[0].opts.body), { item_keys: ["ATT_PDF_DEL"] });
      assert.strictEqual(ingestCalls.length, 1);
      assert.deepStrictEqual(JSON.parse(ingestCalls[0].opts.body), { item_keys: ["PARENT_WITH_LINKED_URL_KEY"] });
    } finally {
      global.fetch = origFetch;
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

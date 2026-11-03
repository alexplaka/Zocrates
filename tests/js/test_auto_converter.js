/**
 * test_auto_converter.js — Tests for autoConverter.js module.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

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
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

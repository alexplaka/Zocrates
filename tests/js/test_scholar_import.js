/**
 * test_scholar_import.js — Tests for scholarImport.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: ScholarImport ---");
  const env = setupMockEnvironment();
  const ScholarImport = require("../../zotero-plugin/addon/lib/scholarImport.js");
  const AutoConverter = require("../../zotero-plugin/addon/lib/autoConverter.js");

  // 1. formatCreators
  test("formatCreators: splits standard 'First Last' names into firstName and lastName", () => {
    const creators = ScholarImport.formatCreators(["Alice Smith", "Bob C. Jones"]);
    assert.strictEqual(creators.length, 2);
    assert.deepStrictEqual(creators[0], { firstName: "Alice", lastName: "Smith", creatorType: "author" });
    assert.deepStrictEqual(creators[1], { firstName: "Bob C.", lastName: "Jones", creatorType: "author" });
  });

  test("formatCreators: handles single-name or institutional authors with fieldMode: 1", () => {
    const creators = ScholarImport.formatCreators(["OpenAI", "Plato"]);
    assert.strictEqual(creators.length, 2);
    assert.deepStrictEqual(creators[0], { name: "OpenAI", creatorType: "author", fieldMode: 1 });
    assert.deepStrictEqual(creators[1], { name: "Plato", creatorType: "author", fieldMode: 1 });
  });

  test("formatCreators: handles 'Last, First' comma-separated names", () => {
    const creators = ScholarImport.formatCreators(["Smith, Alice Jane", "Doe, John"]);
    assert.strictEqual(creators.length, 2);
    assert.deepStrictEqual(creators[0], { firstName: "Alice Jane", lastName: "Smith", creatorType: "author" });
    assert.deepStrictEqual(creators[1], { firstName: "John", lastName: "Doe", creatorType: "author" });
  });

  test("formatCreators: preserves pre-formatted creator objects", () => {
    const creators = ScholarImport.formatCreators([
      { firstName: "Ada", lastName: "Lovelace" },
      { name: "CERN", fieldMode: 1 },
    ]);
    assert.strictEqual(creators.length, 2);
    assert.strictEqual(creators[0].firstName, "Ada");
    assert.strictEqual(creators[0].lastName, "Lovelace");
    assert.strictEqual(creators[1].name, "CERN");
    assert.strictEqual(creators[1].fieldMode, 1);
  });

  test("formatCreators: gracefully returns empty array for null/empty/invalid author list", () => {
    assert.deepStrictEqual(ScholarImport.formatCreators(null), []);
    assert.deepStrictEqual(ScholarImport.formatCreators([]), []);
    assert.deepStrictEqual(ScholarImport.formatCreators(["", null, "   "]), []);
  });

  // 2. setItemFieldSafe
  test("setItemFieldSafe: sets valid string field on item", () => {
    const item = new env.zotero.Item("journalArticle");
    const ok = ScholarImport.setItemFieldSafe(item, "title", "Test Title");
    assert.strictEqual(ok, true);
    assert.strictEqual(item.getField("title"), "Test Title");
  });

  test("setItemFieldSafe: ignores empty or null field values without throwing", () => {
    const item = new env.zotero.Item("journalArticle");
    assert.strictEqual(ScholarImport.setItemFieldSafe(item, "title", null), false);
    assert.strictEqual(ScholarImport.setItemFieldSafe(item, "title", ""), false);
    assert.strictEqual(ScholarImport.setItemFieldSafe(item, "title", "   "), false);
    assert.strictEqual(item.getField("title"), undefined);
  });

  test("setItemFieldSafe: catches invalid field errors gracefully", () => {
    const item = new env.zotero.Item("journalArticle");
    const ok = ScholarImport.setItemFieldSafe(item, "invalidField", "Some Value");
    assert.strictEqual(ok, false);
  });

  // 3. getServerUrl — delegates to Zotero.Zocrates.getBackendUrl() (installed by
  // bootstrap.js), with a defensive inline fallback when the helper is absent.
  test("getServerUrl: falls back to http://127.0.0.1:8765 when the namespace helper is absent", () => {
    delete env.zotero.Zocrates.getBackendUrl;
    assert.strictEqual(ScholarImport.getServerUrl(), "http://127.0.0.1:8765");
  });

  test("getServerUrl: delegates to Zotero.Zocrates.getBackendUrl when installed", () => {
    env.zotero.Zocrates.getBackendUrl = () => "http://127.0.0.1:9999";
    try {
      assert.strictEqual(ScholarImport.getServerUrl(), "http://127.0.0.1:9999");
    } finally {
      delete env.zotero.Zocrates.getBackendUrl;
    }
  });

  test("getServerUrl: falls back to http://127.0.0.1:8765 when the helper throws", () => {
    env.zotero.Zocrates.getBackendUrl = () => {
      throw new Error("backend unavailable");
    };
    try {
      assert.strictEqual(ScholarImport.getServerUrl(), "http://127.0.0.1:8765");
    } finally {
      delete env.zotero.Zocrates.getBackendUrl;
    }
  });

  // 4. importPaper
  await testAsync("importPaper: creates a journalArticle item with metadata, creators, and automatic tags", async () => {
    const candidate = {
      title: "Deep Residual Learning for Image Recognition",
      abstract: "Deeper neural networks are more difficult to train...",
      venue: "CVPR",
      year: 2016,
      doi: "10.1109/CVPR.2016.90",
      authors: ["Kaiming He", "Xiangyu Zhang"],
      keywords: ["Computer Vision", "Residual Networks"],
    };

    const res = await ScholarImport.importPaper({
      candidate,
      stagedPath: "",
      targetCollectionId: null,
      libraryID: 1,
    });

    assert.ok(res.item);
    assert.ok(res.itemID > 0);
    assert.ok(res.attachmentKey);
    assert.strictEqual(res.item.itemType, "journalArticle");
    assert.strictEqual(res.item.getField("title"), candidate.title);
    assert.strictEqual(res.item.getField("abstractNote"), candidate.abstract);
    assert.strictEqual(res.item.getField("publicationTitle"), "CVPR");
    assert.strictEqual(res.item.getField("date"), "2016");
    assert.strictEqual(res.item.getField("DOI"), candidate.doi);
    assert.strictEqual(res.item.creators.length, 2);
    assert.strictEqual(res.item.tags.length, 2);
    assert.strictEqual(res.item.tags[0].type, 1);
  });

  await testAsync("importPaper: detects preprints and sets itemType to preprint when arxiv_id is present", async () => {
    const candidate = {
      title: "Attention Is All You Need",
      arxiv_id: "1706.03762",
      venue: "arXiv preprint",
      year: 2017,
      authors: ["Ashish Vaswani", "Noam Shazeer"],
      keywords: ["Transformers"],
    };

    const res = await ScholarImport.importPaper({ candidate });
    assert.strictEqual(res.item.itemType, "preprint");
  });

  await testAsync("importPaper: resolves Zotero from window context when not in global scope (iframe environment)", async () => {
    const savedZotero = global.Zotero;
    delete global.Zotero;
    try {
      const mockWin = {
        parent: { Zotero: env.zotero },
      };

      const candidate = {
        title: "Test Paper in Iframe",
        year: 2024,
        authors: ["Author A"],
      };

      const res = await ScholarImport.importPaper({
        candidate,
        window: mockWin,
      });

      assert.ok(res.item);
      assert.strictEqual(res.item.getField("title"), "Test Paper in Iframe");
    } finally {
      global.Zotero = savedZotero;
    }
  });

  await testAsync("importPaper: attaches staged PDF file and returns attachmentKey", async () => {
    const candidate = {
      title: "FlashAttention",
      authors: ["Tri Dao"],
      year: 2022,
    };

    const res = await ScholarImport.importPaper({
      candidate,
      stagedPath: "/tmp/staged_papers/flash_attention.pdf",
    });

    assert.ok(res.attachmentKey);
    assert.ok(env.zotero.Attachments.imported.length > 0);
    const lastAtt = env.zotero.Attachments.imported[env.zotero.Attachments.imported.length - 1];
    assert.strictEqual(lastAtt.file, "/tmp/staged_papers/flash_attention.pdf");
    assert.strictEqual(lastAtt.parentItemID, res.itemID);
  });

  await testAsync("importPaper: falls back to native Zotero.Attachments.addAvailableFile when stagedPath is empty", async () => {
    const candidate = {
      title: "Paper with DOI but no direct PDF",
      doi: "10.1000/182",
      authors: ["Author X"],
    };

    const res = await ScholarImport.importPaper({
      candidate,
      stagedPath: "",
    });

    assert.ok(res.attachmentKey);
    assert.ok(res.attachmentKey.startsWith("ATT_NATIVE_"));
  });

  await testAsync("importPaper: adds item to target collection by collection key or integer ID", async () => {
    const candidate = {
      title: "LoRA: Low-Rank Adaptation of Large Language Models",
      authors: ["Edward Hu"],
      year: 2021,
    };

    const res1 = await ScholarImport.importPaper({
      candidate,
      targetCollectionId: "COL123",
    });
    assert.deepStrictEqual(res1.item.collections, [101]);

    const res2 = await ScholarImport.importPaper({
      candidate,
      targetCollectionId: 101,
    });
    assert.deepStrictEqual(res2.item.collections, [101]);
  });

  // 5. importBatch
  await testAsync("importBatch: suppresses AutoConverter during import and restores it in finally block", async () => {
    env.zotero.Zocrates.AutoConverter = AutoConverter;
    AutoConverter.destroy();

    const stagedItems = [
      {
        candidate: { title: "Paper 1", authors: ["Author One"] },
        staged_path: "/tmp/p1.pdf",
      },
      {
        candidate: { title: "Paper 2", authors: ["Author Two"] },
        staged_path: "/tmp/p2.pdf",
      },
    ];

    const res = await ScholarImport.importBatch({
      stagedItems,
      batchToken: null,
    });

    assert.strictEqual(AutoConverter._suppressParentIDs.size, 0, "AutoConverter._suppressParentIDs must be cleaned up in finally");
    assert.strictEqual(AutoConverter._suppressKeys.size, 0, "AutoConverter._suppressKeys must be cleaned up in finally");
    assert.strictEqual(res.importedCount, 2);
    assert.strictEqual(res.attachmentCount, 2);
    assert.strictEqual(res.failedItems.length, 0);
  });

  await testAsync("importBatch: invokes cleanup_batch and BatchConverter.startBatchConvert with skipConfirm", async () => {
    let cleanupCalledWithToken = null;
    let batchConvertCalledWith = null;

    const mockFetch = async (url, opts) => {
      if (url.includes("/discover/cleanup_batch")) {
        cleanupCalledWithToken = JSON.parse(opts.body).batch_token;
        return { ok: true, json: async () => ({ status: "cleaned" }) };
      }
      if (url.includes("/status")) {
        return { ok: true, json: async () => ({ document_converter_id: "docling_local" }) };
      }
      return { ok: false };
    };

    const mockBatchConverter = {
      startBatchConvert: async (keys, converterId, options) => {
        batchConvertCalledWith = { keys, converterId, options };
      },
    };
    env.zotero.Zocrates.BatchConverter = mockBatchConverter;

    await ScholarImport.importBatch({
      stagedItems: [
        {
          candidate: { title: "Paper A", authors: ["A. Author"] },
          staged_path: "/tmp/pa.pdf",
        },
      ],
      batchToken: "test_batch_token_xyz",
      window: { fetch: mockFetch },
    });

    assert.strictEqual(cleanupCalledWithToken, "test_batch_token_xyz");
    assert.ok(batchConvertCalledWith);
    assert.strictEqual(batchConvertCalledWith.converterId, "docling_local");
    assert.strictEqual(batchConvertCalledWith.options.skipConfirm, true);
    assert.strictEqual(batchConvertCalledWith.keys.length, 1);
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

/**
 * test_batch_converter.js — Tests for batchConverter.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: BatchConverter ---");
  const env = setupMockEnvironment();
  const BatchConverter = require("../../zotero-plugin/addon/lib/batchConverter.js");

  await testAsync("skipConfirm: skips Services.prompt.confirm dialog when options.skipConfirm is true", async () => {
    let promptConfirmCalled = false;
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    global.Services.prompt.confirm = () => {
      promptConfirmCalled = true;
      return true;
    };

    global.fetch = async (url) => {
      if (url.includes("/batch-convert-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 2,
            eligible_documents: 2,
            already_converted_count: 0,
            estimated_cost_usd: 0.0,
            total_pages: 10,
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    let unexpectedError = null;
    try {
      await BatchConverter.startBatchConvert(["K1", "K2"], "markitdown", { skipConfirm: true });
    } catch (e) {
      unexpectedError = e;
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
    }

    assert.strictEqual(unexpectedError, null, `startBatchConvert threw unexpectedly: ${unexpectedError?.message}`);
    assert.strictEqual(promptConfirmCalled, false, "Confirmation dialog should NOT be shown when skipConfirm: true");
  });

  await testAsync("skipConfirm: prompts confirmation dialog when options.skipConfirm is false or omitted", async () => {
    let promptConfirmCalled = false;
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    global.Services.prompt.confirm = () => {
      promptConfirmCalled = true;
      return false; // User cancels dialog
    };

    global.fetch = async (url) => {
      if (url.includes("/batch-convert-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 2,
            eligible_documents: 2,
            already_converted_count: 0,
            estimated_cost_usd: 0.0,
            total_pages: 10,
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    let unexpectedError = null;
    try {
      await BatchConverter.startBatchConvert(["K1", "K2"], "markitdown", { skipConfirm: false });
    } catch (e) {
      unexpectedError = e;
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
    }

    assert.strictEqual(unexpectedError, null, `startBatchConvert threw unexpectedly: ${unexpectedError?.message}`);
    assert.strictEqual(promptConfirmCalled, true, "Confirmation dialog should be shown when skipConfirm: false");
  });

  await testAsync("startBatchConvert prompt: Datalab excludes Figure Enrichment even when vlm_captioning is enabled", async () => {
    let capturedPromptText = "";
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    global.Services.prompt.confirm = (win, title, text) => {
      capturedPromptText = text;
      return false;
    };

    global.fetch = async (url) => {
      if (url.includes("/cost-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 1,
            eligible_documents: 1,
            already_converted_count: 0,
            estimated_cost_usd: 0.04,
            total_pages: 10,
            datalab_merge_cross_page: false,
            vlm_captioning: {
              enabled: true,
              provider: "openrouter",
              model: "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            },
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    try {
      await BatchConverter.startBatchConvert(["K1"], "datalab", { skipConfirm: false });
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
    }

    assert.ok(capturedPromptText.includes("• Estimated Cost: ~$0.0400 USD"), "Should include estimated cost for Datalab");
    assert.strictEqual(
      capturedPromptText.includes("Figure Enrichment"),
      false,
      "Datalab conversion prompt must exclude Figure Enrichment even when vlm_captioning is enabled"
    );
  });

  await testAsync("startBatchConvert prompt: Datalab with merge_cross_page: true includes Estimated Base Cost and Cross-Page Merging: Active", async () => {
    let capturedPromptText = "";
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    global.Services.prompt.confirm = (win, title, text) => {
      capturedPromptText = text;
      return false;
    };

    global.fetch = async (url) => {
      if (url.includes("/cost-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 1,
            eligible_documents: 1,
            already_converted_count: 0,
            estimated_cost_usd: 0.04,
            total_pages: 10,
            datalab_merge_cross_page: true,
            vlm_captioning: {
              enabled: true,
              provider: "openrouter",
              model: "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            },
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    try {
      await BatchConverter.startBatchConvert(["K1"], "datalab", { skipConfirm: false });
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
    }

    assert.ok(
      capturedPromptText.includes("• Estimated Base Cost: ~$0.0400 USD"),
      "Prompt should include Estimated Base Cost when mergeCrossPage is active"
    );
    assert.ok(
      capturedPromptText.includes("• Cross-Page Merging: Active (incurs variable compute surcharge; final cost may be higher)"),
      "Prompt should include Cross-Page Merging: Active line"
    );
    assert.strictEqual(
      capturedPromptText.includes("• Estimated Cost:"),
      false,
      "Prompt should not include generic Estimated Cost when Estimated Base Cost is shown"
    );
    assert.strictEqual(
      capturedPromptText.includes("Figure Enrichment"),
      false,
      "Prompt should not include Figure Enrichment for Datalab"
    );
  });

  await testAsync("startBatchConvert prompt: offline converters (e.g. docling_local) still include Figure Enrichment when enabled", async () => {
    let capturedPromptText = "";
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    global.Services.prompt.confirm = (win, title, text) => {
      capturedPromptText = text;
      return false;
    };

    global.fetch = async (url) => {
      if (url.includes("/cost-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 2,
            eligible_documents: 2,
            already_converted_count: 0,
            estimated_cost_usd: 0.0,
            total_pages: 10,
            vlm_captioning: {
              enabled: true,
              provider: "openrouter",
              model: "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            },
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    try {
      await BatchConverter.startBatchConvert(["K1", "K2"], "docling_local", { skipConfirm: false });
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
    }

    assert.ok(
      capturedPromptText.includes("• Figure Enrichment: Active (OpenRouter: nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free)"),
      `Offline converter prompt should include Figure Enrichment: Active line. Got: ${capturedPromptText}`
    );
    assert.strictEqual(
      capturedPromptText.includes("Cross-Page Merging"),
      false,
      "Offline converter prompt should not include Cross-Page Merging line"
    );
  });

  await testAsync("startBatchConvert prompt: Datalab falls back to Zotero.Prefs when estimate.datalab_merge_cross_page is undefined", async () => {
    let capturedPromptText = "";
    const origConfirm = global.Services.prompt.confirm;
    const origFetch = global.fetch;
    const origPrefs = global.Zotero?.Prefs;
    global.Services.prompt.confirm = (win, title, text) => {
      capturedPromptText = text;
      return false;
    };

    global.Zotero = global.Zotero || {};
    global.Zotero.Prefs = {
      get(key, defaultVal) {
        if (key === "extensions.zocrates.datalabMergeCrossPage") return true;
        return defaultVal;
      },
    };

    global.fetch = async (url) => {
      if (url.includes("/cost-estimate")) {
        return {
          ok: true,
          json: async () => ({
            target_documents: 1,
            eligible_documents: 1,
            already_converted_count: 0,
            estimated_cost_usd: 0.02,
            total_pages: 5,
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    };

    try {
      await BatchConverter.startBatchConvert(["K1"], "datalab", { skipConfirm: false });
    } finally {
      global.Services.prompt.confirm = origConfirm;
      global.fetch = origFetch;
      if (origPrefs) global.Zotero.Prefs = origPrefs;
    }

    assert.ok(
      capturedPromptText.includes("• Estimated Base Cost: ~$0.0200 USD"),
      "Prompt should fall back to Zotero.Prefs and show Estimated Base Cost"
    );
    assert.ok(
      capturedPromptText.includes("• Cross-Page Merging: Active (incurs variable compute surcharge; final cost may be higher)"),
      "Prompt should show Cross-Page Merging: Active from Prefs fallback"
    );
  });

  test("renderConversionStatusModal: sets --bg-card and data-theme conforming to targetDoc.body.getAttribute('data-theme')", () => {
    let createdElements = [];

    const createDocWithTheme = (themeName) => {
      createdElements = [];
      const body = createMockElement("body");
      if (themeName) {
        body.setAttribute("data-theme", themeName);
      }
      return {
        getElementById: () => null,
        createElementNS: (ns, tag) => {
          const el = createMockElement(tag, ns);
          createdElements.push(el);
          return el;
        },
        addEventListener: () => {},
        removeEventListener: () => {},
        body: body,
      };
    };

    // Test sepia
    BatchConverter.renderConversionStatusModal({
      window: { document: createDocWithTheme("sepia") },
    });
    let overlay = createdElements.find((el) => el.id === "zocrates-status-modal-overlay");
    assert.ok(overlay, "Status modal overlay should be created for sepia theme");
    assert.strictEqual(overlay.getAttribute("data-theme"), "sepia");
    assert.strictEqual(overlay.style.getPropertyValue("--bg-card") || overlay.style["--bg-card"], "#fdfbf7");

    // Test amoled
    BatchConverter.renderConversionStatusModal({
      window: { document: createDocWithTheme("amoled") },
    });
    overlay = createdElements.find((el) => el.id === "zocrates-status-modal-overlay");
    assert.ok(overlay, "Status modal overlay should be created for amoled theme");
    assert.strictEqual(overlay.getAttribute("data-theme"), "amoled");
    assert.strictEqual(overlay.style.getPropertyValue("--bg-card") || overlay.style["--bg-card"], "#0d0d0d");

    // Fallback to Zotero.Prefs when body has no data-theme
    const origPrefs = global.Zotero?.Prefs;
    try {
      global.Zotero = global.Zotero || {};
      global.Zotero.Prefs = {
        get(key) {
          if (key === "zocrates.theme") return "zotero-dark";
          return null;
        },
      };
      BatchConverter.renderConversionStatusModal({
        window: { document: createDocWithTheme(null) },
      });
      overlay = createdElements.find((el) => el.id === "zocrates-status-modal-overlay");
      assert.strictEqual(overlay.getAttribute("data-theme"), "zotero-dark");
      assert.strictEqual(overlay.style.getPropertyValue("--bg-card") || overlay.style["--bg-card"], "#2d2d2d");
    } finally {
      if (origPrefs) global.Zotero.Prefs = origPrefs;
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

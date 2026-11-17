/**
 * test_batch_converter.js — Tests for batchConverter.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: BatchConverter ---");
  const env = setupMockEnvironment();
  const BatchConverter = require("../../zotero-plugin/addon/lib/batchConverter.js");

  // --- Datalab processing-mode modal test harness ---------------------------
  const flushAsync = async (times = 4) => {
    for (let i = 0; i < times; i++) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  };

  const modalOverlay = () => env.document.body.querySelector("#zocrates-datalab-mode-overlay");

  const modalNode = (id) => {
    const overlay = modalOverlay();
    return overlay ? overlay.querySelector("#" + id) : null;
  };

  const modalNodeText = (id) => {
    const node = modalNode(id);
    return node ? node.textContent : "";
  };

  const modalText = () => {
    const overlay = modalOverlay();
    return overlay ? overlay.textContent : "";
  };

  const clickNode = (node) => node.dispatchEvent({ type: "click" });

  const chooseMode = (mode) => {
    const select = modalNode("zocrates-datalab-mode-select");
    assert.ok(select, "Processing mode dropdown should be rendered");
    select.value = mode;
    select.dispatchEvent({ type: "change" });
  };

  const datalabEstimate = (overrides = {}) => ({
    target_documents: 1,
    eligible_documents: 1,
    already_converted_count: 0,
    non_pdf_already_converted_count: 0,
    estimated_cost_usd: 0.04,
    total_pages: 10,
    datalab_merge_cross_page: false,
    datalab_mode: "balanced",
    datalab_cost_per_page_usd: { fast: 0.00133, balanced: 0.004, accurate: 0.01 },
    ...overrides,
  });

  const estimateModeOf = (url) => {
    const match = /[?&]mode=([^&]*)/.exec(url);
    return match ? decodeURIComponent(match[1]) : null;
  };

  const estimateOk = (payload) => ({ ok: true, json: async () => payload });

  const installFetch = (estimateHandler) => {
    const calls = [];
    global.fetch = async (url, opts = {}) => {
      const urlStr = String(url);
      calls.push({ url: urlStr, opts, mode: estimateModeOf(urlStr) });
      if (urlStr.includes("/batch-convert")) {
        return {
          ok: true,
          body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
        };
      }
      if (estimateHandler) return estimateHandler(urlStr, opts);
      return estimateOk(datalabEstimate({ datalab_mode: estimateModeOf(urlStr) }));
    };
    return calls;
  };

  // Runs `fn` with a stubbed fetch (and optional prompt stubs), always restoring globals.
  const withStubs = async (estimateHandler, fn, { promptConfirm = null, promptAlert = null } = {}) => {
    const origFetch = global.fetch;
    const origConfirm = global.Services.prompt.confirm;
    const origAlert = global.Services.prompt.alert;
    const calls = installFetch(estimateHandler);
    if (promptConfirm) global.Services.prompt.confirm = promptConfirm;
    if (promptAlert) global.Services.prompt.alert = promptAlert;
    try {
      await fn(calls);
    } finally {
      global.fetch = origFetch;
      global.Services.prompt.confirm = origConfirm;
      if (origAlert === undefined) delete global.Services.prompt.alert;
      else global.Services.prompt.alert = origAlert;
    }
  };

  const postBodyOf = (calls) => {
    const post = calls.find((c) => c.url.includes("/batch-convert"));
    assert.ok(post, "POST /batch-convert should have been dispatched");
    return JSON.parse(post.opts.body);
  };

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

  await testAsync("startBatchConvert: skipConfirm Datalab leaves the mode to the backend resolver and renders no dialog", async () => {
    await withStubs(null, async (calls) => {
      await BatchConverter.startBatchConvert(["K1"], "datalab", { skipConfirm: true });

      assert.strictEqual(modalOverlay(), null, "skipConfirm must not render the mode dialog");
      // Phase 2: the opening estimate omits &mode so the backend resolver applies
      // the user's configured global default and echoes it in datalab_mode.
      assert.strictEqual(calls[0].mode, null, "The opening estimate must omit &mode (resolver default)" );
      assert.strictEqual(
        "datalab_mode" in postBodyOf(calls),
        false,
        "Unattended batches must not pin a mode"
      );
    });
  });

  await testAsync("startBatchConvert modal: Datalab opens the mode dialog and excludes Figure Enrichment even when vlm_captioning is enabled", async () => {
    await withStubs(
      () => estimateOk(datalabEstimate({
        vlm_captioning: {
          enabled: true,
          provider: "openrouter",
          model: "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        },
      })),
      async (calls) => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        const overlay = modalOverlay();
        assert.ok(overlay, "Datalab mode modal should be rendered");
        assert.strictEqual(overlay.id, "zocrates-datalab-mode-overlay", "Datalab modal must use its own overlay id");

        const text = modalText();
        assert.ok(
          text.includes("Convert 1 document with Datalab (Cloud)?"),
          `Modal header should name the converter. Got: ${text}`
        );
        assert.ok(
          text.includes("• Estimated Base Conversion Cost: ~$0.0400 USD (10 pages × $4.00/1k)"),
          `Modal should show the per-mode base cost estimate. Got: ${text}`
        );
        assert.strictEqual(
          text.includes("Figure Enrichment"),
          false,
          "Datalab modal must exclude Figure Enrichment even when vlm_captioning is enabled"
        );
        // Phase 2: the opening estimate omits &mode; the echoed datalab_mode
        // (resolver default) is what pre-selects the dropdown.
        assert.strictEqual(
          calls[0].mode,
          null,
          "Opening estimate must omit &mode so the echo follows the configured default"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;

        assert.strictEqual(modalOverlay(), null, "Cancel should remove the modal overlay");
        assert.strictEqual(
          calls.filter((c) => c.url.includes("/batch-convert")).length,
          0,
          "Cancel must not dispatch POST /batch-convert"
        );
      }
    );
  });

  await testAsync("startBatchConvert modal: Datalab with merge_cross_page: true includes Estimated Base Conversion Cost and Cross-Page Merging: Active", async () => {
    await withStubs(
      () => estimateOk(datalabEstimate({ datalab_merge_cross_page: true })),
      async () => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        const text = modalText();
        assert.ok(
          text.includes("• Estimated Base Conversion Cost: ~$0.0400 USD (10 pages × $4.00/1k)"),
          `Modal should include the base cost estimate when mergeCrossPage is active. Got: ${text}`
        );
        assert.ok(
          text.includes("• Cross-Page Merging: Active (incurs variable compute surcharge; final cost may be higher)"),
          `Modal should include the Cross-Page Merging surcharge line. Got: ${text}`
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("startBatchConvert modal: Datalab falls back to Zotero.Prefs when estimate.datalab_merge_cross_page is undefined", async () => {
    const origPrefs = global.Zotero?.Prefs;
    global.Zotero = global.Zotero || {};
    global.Zotero.Prefs = {
      get(key, defaultVal) {
        if (key === "extensions.zocrates.datalabMergeCrossPage") return true;
        return defaultVal;
      },
    };

    try {
      await withStubs(
        () => estimateOk(datalabEstimate({
          total_pages: 5,
          estimated_cost_usd: 0.02,
          datalab_merge_cross_page: undefined,
        })),
        async () => {
          const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
            skipConfirm: false,
            window: { document: env.document },
          });
          await flushAsync();

          const text = modalText();
          assert.ok(
            text.includes("• Estimated Base Conversion Cost: ~$0.0200 USD (5 pages × $4.00/1k)"),
            `Modal should fall back to Zotero.Prefs and show the base cost estimate. Got: ${text}`
          );
          assert.ok(
            text.includes("• Cross-Page Merging: Active (incurs variable compute surcharge; final cost may be higher)"),
            "Modal should show Cross-Page Merging: Active from the Prefs fallback"
          );

          clickNode(modalNode("zocrates-datalab-cancel-btn"));
          await run;
        }
      );
    } finally {
      if (origPrefs) global.Zotero.Prefs = origPrefs;
    }
  });

  await testAsync("renderDatalabModeModal: pre-selects balanced in Phase 1 and offers fast/balanced/accurate in order", async () => {
    await withStubs(
      null,
      async () => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        const select = modalNode("zocrates-datalab-mode-select");
        assert.ok(select, "Processing mode dropdown should be rendered");
        assert.strictEqual(select.value, "balanced", "Phase 1 pre-selection must be balanced");
        assert.deepStrictEqual(
          select.children.map((opt) => opt.value),
          ["fast", "balanced", "accurate"],
          "Dropdown should offer fast/balanced/accurate in that order"
        );
        const selectedOption = select.children.find((opt) => opt.selected);
        assert.ok(selectedOption, "One option should be marked selected");
        assert.strictEqual(selectedOption.value, "balanced", "The balanced option should be marked selected");
        assert.ok(
          modalText().includes("Processing mode:"),
          "Dropdown should be labelled Processing mode:"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("renderDatalabModeModal: uses a distinct overlay id and coexists with the conversion status modal", async () => {
    const body = createMockElement("body");
    const doc = {
      createElementNS: (ns, tag) => createMockElement(tag, ns),
      getElementById: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      body,
    };
    doc.documentElement = createMockElement("html");

    BatchConverter.renderConversionStatusModal({ window: { document: doc }, total: 0 });

    const decision = BatchConverter.renderDatalabModeModal({
      window: { document: doc },
      keys: ["K1"],
      estimate: datalabEstimate(),
      serverUrl: "http://127.0.0.1:8765",
    });

    const statusOverlays = body.querySelectorAll("#zocrates-status-modal-overlay");
    const datalabOverlays = body.querySelectorAll("#zocrates-datalab-mode-overlay");
    assert.strictEqual(statusOverlays.length, 1, "The status modal should survive the Datalab dialog");
    assert.strictEqual(datalabOverlays.length, 1, "The Datalab dialog should use its own overlay id");

    clickNode(datalabOverlays[0].querySelector("#zocrates-datalab-cancel-btn"));
    assert.strictEqual(await decision, null, "Cancel should resolve null");
    assert.strictEqual(
      body.querySelectorAll("#zocrates-status-modal-overlay").length,
      1,
      "Dismissing the Datalab dialog must not remove the status modal"
    );
  });

  await testAsync("renderDatalabModeModal: Escape and overlay clicks dismiss the dialog with no further network calls", async () => {
    const makeDoc = () => {
      const listeners = new Map();
      const doc = {
        createElementNS: (ns, tag) => createMockElement(tag, ns),
        getElementById: () => null,
        addEventListener: (evt, fn) => {
          if (!listeners.has(evt)) listeners.set(evt, []);
          listeners.get(evt).push(fn);
        },
        removeEventListener: (evt, fn) => {
          if (listeners.has(evt)) listeners.set(evt, listeners.get(evt).filter((h) => h !== fn));
        },
        body: createMockElement("body"),
        dispatchKeydown: (key) => (listeners.get("keydown") || []).forEach((h) => h({ key })),
        keydownListenerCount: () => (listeners.get("keydown") || []).length,
      };
      return doc;
    };

    await withStubs(
      null,
      async (calls) => {
        const escapeDoc = makeDoc();
        const escapeDecision = BatchConverter.renderDatalabModeModal({
          window: { document: escapeDoc },
          keys: ["K1"],
          estimate: datalabEstimate(),
          fetchFn: global.fetch,
          serverUrl: "http://127.0.0.1:8765",
        });
        assert.strictEqual(escapeDoc.keydownListenerCount(), 1, "The dialog should own an Escape key handler");
        escapeDoc.dispatchKeydown("Escape");
        assert.strictEqual(await escapeDecision, null, "Escape should resolve null");
        assert.strictEqual(
          escapeDoc.body.querySelector("#zocrates-datalab-mode-overlay"),
          null,
          "Escape should remove the overlay"
        );
        assert.strictEqual(
          escapeDoc.keydownListenerCount(),
          0,
          "The Escape key handler should be unregistered on close"
        );

        const clickDoc = makeDoc();
        const clickDecision = BatchConverter.renderDatalabModeModal({
          window: { document: clickDoc },
          keys: ["K1"],
          estimate: datalabEstimate(),
          fetchFn: global.fetch,
          serverUrl: "http://127.0.0.1:8765",
        });
        const overlay = clickDoc.body.querySelector("#zocrates-datalab-mode-overlay");
        clickNode(overlay);
        assert.strictEqual(await clickDecision, null, "An overlay click should resolve null");
        assert.strictEqual(
          clickDoc.body.querySelector("#zocrates-datalab-mode-overlay"),
          null,
          "An overlay click should remove the overlay"
        );

        assert.strictEqual(
          calls.filter((c) => c.url.includes("/batch-convert")).length,
          0,
          "Dismissing the dialog must not dispatch POST /batch-convert"
        );
      }
    );
  });

  await testAsync("renderDatalabModeModal: changing the mode refetches with &mode=… , recomputes the cost at the new rate, and dispatches that frozen mode", async () => {
    await withStubs(
      (url) => {
        const mode = estimateModeOf(url);
        return estimateOk(datalabEstimate({
          datalab_mode: mode,
          estimated_cost_usd: mode === "accurate" ? 0.1 : 0.04,
        }));
      },
      async (calls) => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        assert.ok(
          modalText().includes("~$0.0400 USD (10 pages × $4.00/1k)"),
          `Balanced cost line expected before the change. Got: ${modalText()}`
        );
        assert.ok(
          modalText().includes("automatic retry at accurate"),
          "Balanced selection should disclose the accurate-tier retry"
        );

        chooseMode("accurate");
        await flushAsync();

        const estimateCalls = calls.filter((c) => c.url.includes("/cost-estimate"));
        assert.strictEqual(estimateCalls[estimateCalls.length - 1].mode, "accurate", "Refetch must carry &mode=accurate");
        assert.ok(
          modalText().includes("~$0.1000 USD (10 pages × $10.00/1k)"),
          `Cost line should be recomputed at the accurate rate. Got: ${modalText()}`
        );
        assert.strictEqual(
          modalText().includes("automatic retry"),
          false,
          "Accurate selection must not disclose an automatic retry"
        );

        const proceedBtn = modalNode("zocrates-datalab-proceed-btn");
        assert.strictEqual(proceedBtn.disabled, false, "Proceed should be enabled once a matching estimate applied");
        clickNode(proceedBtn);
        await run;

        const body = postBodyOf(calls);
        assert.strictEqual(body.datalab_mode, "accurate", "POST body must carry the frozen mode of the accepted estimate");
        assert.strictEqual(body.converter_id, "datalab");
        assert.deepStrictEqual(body.item_keys, ["K1"]);
        assert.strictEqual(body.force, false);
      }
    );
  });

  await testAsync("renderDatalabModeModal: Proceed stays disabled until the matching estimate completes", async () => {
    const pending = [];
    await withStubs(
      (url) => {
        const mode = estimateModeOf(url);
        if (mode === "fast") {
          return new Promise((resolve) => pending.push({ mode, resolve }));
        }
        return estimateOk(datalabEstimate({ datalab_mode: mode }));
      },
      async () => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          false,
          "Proceed should be enabled for the opening (matching) estimate"
        );

        chooseMode("fast");
        await flushAsync();

        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          true,
          "Proceed must be disabled while the fast estimate is still in flight"
        );

        assert.strictEqual(pending.length, 1, "The fast estimate should be in flight");
        pending[0].resolve(estimateOk(datalabEstimate({ datalab_mode: "fast" })));
        await flushAsync();

        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          false,
          "Proceed should be enabled once the matching estimate has completed"
        );
        assert.ok(
          modalText().includes("~$0.0133 USD (10 pages × $1.33/1k)"),
          `Fast cost line expected. Got: ${modalText()}`
        );
        assert.ok(
          modalText().includes("automatic retry at balanced"),
          "Fast selection should disclose the balanced-tier retry"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("renderDatalabModeModal: an all-already-converted selection still opens the dialog (no legacy alert), disables Proceed, and is rescued by the dropdown", async () => {
    const alerts = [];
    await withStubs(
      (url) => {
        // The opening estimate omits &mode (resolver default = balanced in this
        // scenario); per-mode refetches carry it explicitly.
        const mode = estimateModeOf(url) ?? "balanced";
        // /cost-estimate sums total_pages over eligible attachments only, so an
        // all-already-converted selection returns total_pages: 0 (same for cost).
        return estimateOk(datalabEstimate({
          datalab_mode: mode,
          already_converted_count: mode === "balanced" ? 1 : 0,
          eligible_documents: mode === "balanced" ? 0 : 1,
          total_pages: mode === "balanced" ? 0 : 10,
          estimated_cost_usd: mode === "balanced" ? 0 : 0.0133,
        }));
      },
      async (calls) => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        assert.ok(
          modalOverlay(),
          "An all-already-converted Datalab selection must still open the mode dialog"
        );
        assert.strictEqual(alerts.length, 0, "Datalab must not dead-end in the legacy alert");

        const text = modalText();
        assert.ok(
          text.includes("1 item(s) already converted at balanced — pick a different mode to re-convert them, or Cancel"),
          `Mode-aware informational line expected. Got: ${text}`
        );
        // The zero-eligible PDF selection must not misrender as a non-PDF fallback
        // line (total_pages is 0 here because /cost-estimate sums eligible only).
        assert.strictEqual(
          text.includes("non-PDF"),
          false,
          `Zero-eligible PDF selection must not mislabel PDFs as non-PDF. Got: ${text}`
        );
        assert.strictEqual(
          text.includes("$0.00"),
          false,
          `Zero-eligible PDF selection must not surface a $0.00 cost. Got: ${text}`
        );
        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          true,
          "Proceed must stay disabled when eligible_documents === 0 for the selected mode"
        );

        // The dropdown rescues exactly this case.
        chooseMode("fast");
        await flushAsync();
        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          false,
          "Picking a mode with eligible documents should re-enable Proceed"
        );

        clickNode(modalNode("zocrates-datalab-proceed-btn"));
        await run;

        assert.strictEqual(postBodyOf(calls).datalab_mode, "fast");
      },
      {
        promptAlert: (win, title, text) => {
          alerts.push(text);
        },
      }
    );
  });

  await testAsync("renderDatalabModeModal: estimate failure renders the error state (never $0.00) and keeps Proceed disabled", async () => {
    await withStubs(
      () => ({ ok: false, status: 500 }),
      async () => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        const text = modalText();
        assert.ok(
          text.includes("Cost estimation failed — try again or Cancel"),
          `Error state expected in the modal. Got: ${text}`
        );
        assert.ok(
          text.includes("Server returned HTTP 500"),
          `Error detail expected in the modal. Got: ${text}`
        );
        assert.strictEqual(
          text.includes("$0.00"),
          false,
          "A failed estimate must never surface a $0.00 cost"
        );
        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          true,
          "Proceed must stay disabled when estimation failed"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("renderDatalabModeModal: error-state Retry refetches the estimate and recovers", async () => {
    let fetchCount = 0;
    await withStubs(
      (url) => {
        fetchCount++;
        // Calls 1-2 fail: the pre-flight estimate in startBatchConvert, then the
        // modal's initial fetch. Call 3 is the error-state Retry refetch.
        if (fetchCount <= 2) return { ok: false, status: 500 };
        return estimateOk(datalabEstimate({ datalab_mode: estimateModeOf(url) }));
      },
      async (calls) => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();
        assert.ok(
          modalText().includes("Cost estimation failed"),
          "Initial estimate failure should render the error state"
        );
        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          true
        );

        const retryBtn = modalNode("zocrates-datalab-retry-btn");
        assert.ok(retryBtn, "Error state should offer a Retry button");
        clickNode(retryBtn);
        await flushAsync();

        assert.strictEqual(fetchCount, 3, "Retry should refetch /cost-estimate");
        assert.strictEqual(
          modalNode("zocrates-datalab-proceed-btn").disabled,
          false,
          "A successful retry should re-enable Proceed"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
        assert.strictEqual(
          calls.filter((c) => c.url.includes("/batch-convert")).length,
          0,
          "Cancel after retry must not dispatch the batch"
        );
      }
    );
  });

  await testAsync("renderDatalabModeModal: version skew — missing datalab_cost_per_page_usd falls back to estimated_cost_usd / total_pages", async () => {
    await withStubs(
      (url) => estimateOk(datalabEstimate({
        datalab_mode: estimateModeOf(url),
        // Older backend: no per-mode map, flat estimate only.
        datalab_cost_per_page_usd: undefined,
      })),
      async () => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        // 0.04 / 10 pages = $0.004/page => $4.00/1k displayed rate.
        assert.ok(
          modalText().includes("~$0.0400 USD (10 pages × $4.00/1k)"),
          `Version-skew fallback cost line expected. Got: ${modalText()}`
        );

        // Switching to fast must still recompute from the flat estimate.
        chooseMode("fast");
        await flushAsync();
        assert.ok(
          modalText().includes("~$0.0400 USD (10 pages × $4.00/1k)"),
          "Fallback rate should not pretend per-mode precision when the map is absent"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("renderDatalabModeModal: stale out-of-order estimate responses are discarded", async () => {
    const pending = [];
    await withStubs(
      (url) => {
        const mode = estimateModeOf(url);
        if (mode === "fast" || mode === "accurate") {
          return new Promise((resolve) => pending.push({ mode, resolve }));
        }
        return estimateOk(datalabEstimate({ datalab_mode: mode }));
      },
      async (calls) => {
        const run = BatchConverter.startBatchConvert(["K1"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        chooseMode("fast");
        await flushAsync();
        chooseMode("accurate");
        await flushAsync();

        const accurateReq = pending.find((p) => p.mode === "accurate");
        const fastReq = pending.find((p) => p.mode === "fast");
        assert.ok(accurateReq && fastReq, "Both mode estimates should be in flight");

        // Newer response lands first.
        accurateReq.resolve(estimateOk(datalabEstimate({ datalab_mode: "accurate", estimated_cost_usd: 0.1 })));
        await flushAsync();
        assert.ok(
          modalText().includes("~$0.1000 USD (10 pages × $10.00/1k)"),
          `Accurate estimate should be applied. Got: ${modalText()}`
        );

        // Older response lands afterwards and must be discarded.
        fastReq.resolve(estimateOk(datalabEstimate({ datalab_mode: "fast" })));
        await flushAsync();
        assert.ok(
          modalText().includes("~$0.1000 USD (10 pages × $10.00/1k)"),
          `Stale fast estimate must not overwrite the applied one. Got: ${modalText()}`
        );
        assert.strictEqual(
          modalText().includes("~$0.0133 USD"),
          false,
          "Stale fast cost must never be rendered"
        );

        clickNode(modalNode("zocrates-datalab-proceed-btn"));
        await run;
        assert.strictEqual(
          postBodyOf(calls).datalab_mode,
          "accurate",
          "Dispatch must freeze the mode of the accepted (newest) estimate"
        );
      }
    );
  });

  await testAsync("renderDatalabModeModal: non-PDF-only Datalab selection shows the local-fallback note without a paid cost line", async () => {
    await withStubs(
      () => estimateOk(datalabEstimate({
        target_documents: 2,
        eligible_documents: 2,
        total_pages: 0,
        estimated_cost_usd: 0,
        non_pdf_already_converted_count: 2,
        datalab_fallback_id: "markitdown",
      })),
      async () => {
        const run = BatchConverter.startBatchConvert(["K1", "K2"], "datalab", {
          skipConfirm: false,
          window: { document: env.document },
        });
        await flushAsync();

        const text = modalText();
        assert.ok(
          text.includes("• To Convert: 2 non-PDF document(s) (via local fallback, $0.00 USD)"),
          `Local-fallback line expected. Got: ${text}`
        );
        assert.ok(
          text.includes("Datalab (Cloud) conversion applies to PDF files only"),
          `PDF-only fallback note expected. Got: ${text}`
        );
        assert.ok(
          text.includes("MarkItDown (Local)"),
          `Fallback converter label expected. Got: ${text}`
        );
        assert.strictEqual(
          text.includes("Estimated Base Conversion Cost"),
          false,
          "No paid cost line should be shown for a zero-PDF-page Datalab selection"
        );

        clickNode(modalNode("zocrates-datalab-cancel-btn"));
        await run;
      }
    );
  });

  await testAsync("renderDatalabModeModal: without a DOM it degrades to the plain-text confirmation with the default mode", async () => {
    let capturedPromptText = "";

    await withStubs(
      null,
      async () => {
        const decision = await BatchConverter.renderDatalabModeModal({
          window: {},
          keys: ["K1"],
          estimate: datalabEstimate(),
          serverUrl: "http://127.0.0.1:8765",
        });
        assert.ok(decision, "The headless fallback should resolve a decision");
        assert.strictEqual(decision.mode, "balanced", "Headless fallback should use the Phase 1 default mode");
        assert.ok(decision.estimate, "Headless fallback should carry the opening estimate");
      },
      {
        promptConfirm: (win, title, text) => {
          capturedPromptText = text;
          return true;
        },
      }
    );

    assert.ok(
      capturedPromptText.includes("Convert 1 document with Datalab (Cloud)?"),
      `Headless confirm text expected. Got: ${capturedPromptText}`
    );
    assert.ok(
      capturedPromptText.includes("• Processing mode: balanced"),
      `Headless confirm should name the mode. Got: ${capturedPromptText}`
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
    assert.strictEqual(
      modalOverlay(),
      null,
      "Non-Datalab converters must keep the plain Services.prompt.confirm dialog"
    );
  });

  await testAsync("startBatchConvert: non-Datalab converters keep the legacy already-converted alert and early return", async () => {
    const alerts = [];

    await withStubs(
      () => estimateOk({
        target_documents: 2,
        eligible_documents: 0,
        already_converted_count: 2,
        estimated_cost_usd: 0.0,
        total_pages: 10,
      }),
      async (calls) => {
        await BatchConverter.startBatchConvert(["K1", "K2"], "docling_local", {
          skipConfirm: false,
          window: { document: env.document },
        });

        assert.strictEqual(calls.filter((c) => c.url.includes("/batch-convert")).length, 0);
      },
      {
        promptAlert: (win, title, text) => {
          alerts.push(text);
        },
      }
    );

    assert.strictEqual(alerts.length, 1, "Non-Datalab converters must keep the legacy alert");
    assert.ok(
      alerts[0].includes("All 2 selected document(s) are already converted."),
      `Legacy alert text expected. Got: ${alerts[0]}`
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

  // --- SSE mode pass-through into BatchProgress ------------------------------
  const withBatchProgressSpy = async (fn) => {
    const updates = [];
    const origBatchProgress = global.Zotero.Zocrates.BatchProgress;
    const origNotifier = global.Zotero.Zocrates.Notifier;
    global.Zotero.Zocrates.BatchProgress = { update: (job) => updates.push(job) };
    global.Zotero.Zocrates.Notifier = { add: () => {} };
    try {
      await fn(updates);
    } finally {
      global.Zotero.Zocrates.BatchProgress = origBatchProgress;
      global.Zotero.Zocrates.Notifier = origNotifier;
    }
  };

  await testAsync("_handleBatchSSEEvent: forwards requested_mode and actual_mode into BatchProgress.update", async () => {
    await withBatchProgressSpy(async (updates) => {
      BatchConverter._handleBatchSSEEvent(
        "progress",
        {
          status: "converting",
          current_key: "ATT1",
          current_index: 1,
          total: 1,
          requested_mode: "fast",
        },
        "Datalab (Cloud)",
        "datalab",
        null
      );
      BatchConverter._handleBatchSSEEvent(
        "progress",
        {
          status: "completed",
          current_key: "ATT1",
          current_index: 1,
          total: 1,
          requested_mode: "fast",
          actual_mode: "balanced",
        },
        "Datalab (Cloud)",
        "datalab",
        null
      );

      assert.strictEqual(updates[0].requestedMode, "fast");
      assert.strictEqual(updates[0].actualMode, undefined);
      assert.strictEqual(updates[1].requestedMode, "fast");
      assert.strictEqual(updates[1].actualMode, "balanced");
    });
  });

  await testAsync("_handleBatchSSEEvent: forwards the complete event mode annotations", async () => {
    await withBatchProgressSpy(async (updates) => {
      BatchConverter._handleBatchSSEEvent(
        "complete",
        { processed: 1, skipped: 0, failed: 0, total: 1, requested_mode: "fast", actual_mode: "fast" },
        "Datalab (Cloud)",
        "datalab",
        null
      );

      assert.strictEqual(updates[0].status, "completed");
      assert.strictEqual(updates[0].requestedMode, "fast");
      assert.strictEqual(updates[0].actualMode, "fast");
    });
  });

  await testAsync("_handleBatchSSEEvent: leaves mode fields absent for non-Datalab payloads", async () => {
    await withBatchProgressSpy(async (updates) => {
      BatchConverter._handleBatchSSEEvent(
        "progress",
        { status: "converting", current_key: "ATT1", current_index: 1, total: 1 },
        "MarkItDown",
        "markitdown",
        null
      );

      assert.ok(!("requestedMode" in updates[0]), "requestedMode must not be added");
      assert.ok(!("actualMode" in updates[0]), "actualMode must not be added");
    });
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

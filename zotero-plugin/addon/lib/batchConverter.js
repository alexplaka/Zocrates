
if (typeof ChromeUtils !== "undefined" && typeof ChromeUtils.importGlobalProperties === "function") {
  try {
    ChromeUtils.importGlobalProperties(["fetch", "AbortController", "TextDecoder"]);
  } catch (e) {}
} else if (typeof Components !== "undefined" && Components.utils && typeof Components.utils.importGlobalProperties === "function") {
  try {
    Components.utils.importGlobalProperties(["fetch", "AbortController", "TextDecoder"]);
  } catch (e) {}
}

function _resolveBatchGlobals(win) {
  let targetWin = win || (typeof Zotero !== "undefined" && Zotero.getMainWindow ? Zotero.getMainWindow() : null);

  let fetchFn =
    (targetWin && typeof targetWin.fetch === "function" && targetWin.fetch.bind(targetWin)) ||
    (typeof fetch === "function" ? fetch : null);

  let AbortControllerClass =
    (targetWin && targetWin.AbortController) ||
    (typeof AbortController !== "undefined" ? AbortController : null);

  let TextDecoderClass =
    (targetWin && targetWin.TextDecoder) ||
    (typeof TextDecoder !== "undefined" ? TextDecoder : null);

  return { fetchFn, AbortControllerClass, TextDecoderClass };
}

function getActiveThemePalette(targetDoc) {
  if (typeof InfoModal !== "undefined" && typeof InfoModal.getActiveThemePalette === "function") {
    return InfoModal.getActiveThemePalette(targetDoc);
  }
  if (typeof Zotero !== "undefined" && Zotero.Zocrates && Zotero.Zocrates.InfoModal && typeof Zotero.Zocrates.InfoModal.getActiveThemePalette === "function") {
    return Zotero.Zocrates.InfoModal.getActiveThemePalette(targetDoc);
  }
  const PALETTES = {
    slate: { bgCard: "#1e293b", bgDark: "#0f172a", border: "#334155", textMain: "#f8fafc", textMuted: "#94a3b8", primary: "#6366f1", chipBg: "rgba(255, 255, 255, 0.06)" },
    amoled: { bgCard: "#0d0d0d", bgDark: "#000000", border: "#27272a", textMain: "#ffffff", textMuted: "#a1a1aa", primary: "#818cf8", chipBg: "rgba(255, 255, 255, 0.08)" },
    "zotero-dark": { bgCard: "#2d2d2d", bgDark: "#1e1e1e", border: "#3d3d3d", textMain: "#e3e3e3", textMuted: "#aaaaaa", primary: "#007acc", chipBg: "rgba(255, 255, 255, 0.06)" },
    "zotero-light": { bgCard: "#ffffff", bgDark: "#f9f9fb", border: "#e0e0e0", textMain: "#222222", textMuted: "#666666", primary: "#007acc", chipBg: "rgba(0, 0, 0, 0.05)" },
    sepia: { bgCard: "#fdfbf7", bgDark: "#f4ecd8", border: "#e4d5b7", textMain: "#433422", textMuted: "#8c7050", primary: "#c05c36", chipBg: "rgba(192, 92, 54, 0.08)" }
  };
  let theme = null;
  if (targetDoc && targetDoc.body && typeof targetDoc.body.getAttribute === "function") {
    theme = targetDoc.body.getAttribute("data-theme");
  }

  // If explicit theme attribute was found on body, normalize and return
  if (theme) {
    if (theme === "light") theme = "zotero-light";
    else if (theme === "dark") theme = "zotero-dark";
    if (PALETTES[theme]) {
      return { theme, colors: PALETTES[theme] };
    }
    return { theme: "slate", colors: PALETTES.slate };
  }

  // If no theme attribute on body, check Zotero.Prefs / system
  try {
    const zot = (typeof Zotero !== "undefined" ? Zotero : null) || (typeof window !== "undefined" && window.Zotero);
    if (zot && zot.Prefs) {
      let pref = zot.Prefs.get("zocrates.theme");
      if (pref && pref !== "auto" && PALETTES[pref]) {
        theme = pref;
      } else {
        let zTheme = zot.Prefs.get("extensions.zotero.theme") || "system";
        if (zTheme === "system") {
          let dark = false;
          const win = (targetDoc && targetDoc.defaultView) || (typeof window !== "undefined" ? window : null);
          if (win && typeof win.matchMedia === "function") {
            dark = win.matchMedia("(prefers-color-scheme: dark)").matches;
          }
          theme = dark ? "zotero-dark" : "zotero-light";
        } else if (zTheme === "dark") {
          theme = "zotero-dark";
        } else {
          theme = "zotero-light";
        }
      }
    }
  } catch (_) {}

  if (!theme || !PALETTES[theme]) {
    theme = "slate";
  }
  return { theme, colors: PALETTES[theme] };
}

var BatchConverter = {
  getActiveThemePalette,
  getServerUrl() {
    // Single source of truth installed by bootstrap.js, with defensive fallback.
    try {
      const url = Zotero?.Zocrates?.getBackendUrl?.();
      if (url) return url;
    } catch (e) {}
    return "http://127.0.0.1:8765";
  },

  getAuthHeaders(extraHeaders = {}) {
    try {
      if (typeof Zotero !== "undefined" && Zotero?.Zocrates?.getAuthHeaders) {
        return Zotero.Zocrates.getAuthHeaders(extraHeaders);
      }
      const token = (typeof Zotero !== "undefined" && (Zotero?.Zocrates?.getAuthToken?.() || Zotero?.Zocrates?._authToken)) || "";
      if (token) {
        return { ...extraHeaders, Authorization: "Bearer " + token };
      }
    } catch (e) {}
    return { ...extraHeaders };
  },

  getConverterLabel(converterId) {
    switch (converterId) {
      case "datalab":
        return "Datalab (Cloud)";
      case "docling_local":
        return "Docling (Local)";
      case "markitdown":
        return "MarkItDown (Local)";
      default:
        return converterId;
    }
  },

  getAttachmentKeysFromSelected(items) {
    if (!items || !items.length) return [];
    let keys = [];
    for (let item of items) {
      if (!item) continue;
      if (typeof item === "string") {
        keys.push(item);
      } else if (item.key) {
        keys.push(item.key);
      }
    }
    return keys;
  },

  async checkConversionStatus(items, window) {
    try {
      let keys = this.getAttachmentKeysFromSelected(items);
      Zotero.debug("Zocrates: checkConversionStatus for keys: " + JSON.stringify(keys));

      if (!keys || keys.length === 0) {
        if (typeof Services !== "undefined" && Services.prompt) {
          Services.prompt.alert(window || null, "Zocrates", "No items selected.");
        }
        return;
      }

      let serverUrl = this.getServerUrl();
      let { fetchFn } = _resolveBatchGlobals(window);
      if (!fetchFn) {
        throw new Error("fetch API is not available in the current environment.");
      }

      let authHeaders = this.getAuthHeaders();
      let [statusResults, statusRes] = await Promise.all([
        Promise.all(
          keys.map(async (key) => {
            try {
              let res = await fetchFn(`${serverUrl}/items/${key}/status`, { headers: authHeaders });
              if (res.ok) {
                return await res.json();
              }
            } catch (e) {
              Zotero.debug("Zocrates: Error fetching item status for " + key + ": " + e);
            }
            return null;
          })
        ),
        fetchFn(`${serverUrl}/status`, { headers: authHeaders }).catch(() => null),
      ]);

      let activeGlobalConverterId = "markitdown";
      let hasDatalabKey = false;
      let embeddingInfo = null;
      if (statusRes && statusRes.ok) {
        let sData = await statusRes.json().catch(() => ({}));
        activeGlobalConverterId = sData.document_converter_id || "markitdown";
        hasDatalabKey = !!sData.has_datalab_key;
        embeddingInfo = sData.embedding_info || null;
      }

      let total = 0;
      let cachedCount = 0;
      let unindexedCount = 0;
      let converterCounts = {
        "Datalab (Cloud)": 0,
        "Docling (Local)": 0,
        "MarkItDown (Local)": 0,
        "Other": 0,
      };
      let failureCount = 0;
      let unconvertedItems = [];

      for (let r of statusResults) {
        if (!r) continue;
        let attachments = r.attachments && r.attachments.length > 0 ? r.attachments : [r];
        for (let att of attachments) {
          total++;
          if (att.is_cached && att.metadata) {
            cachedCount++;
            let cid = att.metadata.converter_id;
            if (cid === "datalab") {
              converterCounts["Datalab (Cloud)"]++;
            } else if (cid === "docling_local" || cid === "docling") {
              converterCounts["Docling (Local)"]++;
            } else if (cid === "markitdown") {
              converterCounts["MarkItDown (Local)"]++;
            } else {
              converterCounts["Other"]++;
            }
          } else {
            unindexedCount++;
            let itemTitle = att.title || att.parent_title || (r.metadata && r.metadata.title) || att.filename || "Untitled Document";
            unconvertedItems.push({
              key: att.item_key || r.item_key,
              parentKey: att.parent_item_key || r.item_key,
              title: itemTitle,
              contentType: att.content_type || "",
              filename: att.filename || "",
            });
          }
          if (att.failed_converters && Object.keys(att.failed_converters).length > 0) {
            failureCount++;
          }
        }
      }

      this.renderConversionStatusModal({
        title: "Zocrates: Conversion Status",
        subtitle: "Zocrates converts documents to Markdown for LLM-ingestion.",
        scopeLabel: `Selected: ${keys.length} item(s) (${total} document attachment(s))`,
        total,
        cachedCount,
        converterCounts,
        unindexedCount,
        failureCount,
        unconvertedItems,
        activeGlobalConverterId,
        hasDatalabKey,
        embeddingInfo,
        window,
      });

    } catch (err) {
      Zotero.debug("Zocrates: Error checking status: " + err);
      if (typeof Services !== "undefined" && Services.prompt) {
        Services.prompt.alert(
          window || null,
          "Zocrates Status Check Error",
          `Could not retrieve status from backend: ${err.message || err}`
        );
      }
    }
  },

  async checkLibraryConversionStatus(window) {
    try {
      Zotero.debug("Zocrates: checkLibraryConversionStatus called");
      let serverUrl = this.getServerUrl();
      let { fetchFn } = _resolveBatchGlobals(window);
      if (!fetchFn) {
        throw new Error("fetch API is not available in the current environment.");
      }

      let authHeaders = this.getAuthHeaders();
      let [libRes, statusRes] = await Promise.all([
        fetchFn(`${serverUrl}/library/conversion-status`, { headers: authHeaders }),
        fetchFn(`${serverUrl}/status`, { headers: authHeaders }).catch(() => null),
      ]);

      if (!libRes.ok) {
        let errText = await libRes.text().catch(() => libRes.statusText);
        throw new Error(`Library status endpoint returned HTTP ${libRes.status}: ${errText}`);
      }

      let libData = await libRes.json();
      let activeGlobalConverterId = "markitdown";
      let hasDatalabKey = false;
      let embeddingInfo = null;
      if (statusRes && statusRes.ok) {
        let sData = await statusRes.json().catch(() => ({}));
        activeGlobalConverterId = sData.document_converter_id || "markitdown";
        hasDatalabKey = !!sData.has_datalab_key;
        embeddingInfo = sData.embedding_info || null;
      }

      let total = libData.total_documents || 0;
      let indexedCount = libData.indexed_count ?? libData.converted_count ?? 0;
      let pendingIndexCount = libData.pending_index_count || 0;
      let unindexedCount = libData.unconverted_count || 0;
      let converterCounts = libData.converter_counts || {
        "Datalab (Cloud)": 0,
        "Docling (Local)": 0,
        "MarkItDown (Local)": 0,
        "Other": 0,
      };
      let unconvertedItems = libData.unconverted_items || [];
      let pendingIndexItems = libData.pending_index_items || [];

      this.renderConversionStatusModal({
        title: "Zocrates: Library Conversion Status",
        subtitle: "Zocrates converts documents to Markdown for LLM-ingestion.",
        scopeLabel: `Entire Library: ${total} document attachment(s)`,
        total,
        indexedCount,
        pendingIndexCount,
        converterCounts,
        unindexedCount,
        failureCount: 0,
        unconvertedItems,
        pendingIndexItems,
        activeGlobalConverterId,
        hasDatalabKey,
        embeddingInfo,
        window,
      });

    } catch (err) {
      Zotero.debug("Zocrates: Error checking library status: " + err);
      if (typeof Services !== "undefined" && Services.prompt) {
        Services.prompt.alert(
          window || null,
          "Zocrates Library Status Check Error",
          `Could not retrieve library conversion status: ${err.message || err}`
        );
      }
    }
  },

  renderConversionStatusModal(options) {
    let {
      title = "Zocrates: Conversion Status",
      subtitle = "Zocrates converts documents to Markdown for LLM-ingestion.",
      scopeLabel = "",
      total = 0,
      indexedCount = 0,
      pendingIndexCount = 0,
      cachedCount = 0,
      converterCounts = {},
      unindexedCount = 0,
      failureCount = 0,
      unconvertedItems = [],
      pendingIndexItems = [],
      activeGlobalConverterId = "markitdown",
      hasDatalabKey = false,
      embeddingInfo = null,
      window = null,
    } = options;

    if (indexedCount === 0 && cachedCount > 0 && pendingIndexCount === 0) {
      indexedCount = cachedCount;
    }

    let targetDoc = (window && window.document) || (typeof Zotero !== "undefined" && Zotero.getMainWindow && Zotero.getMainWindow().document);

    // If no DOM document available, fallback to text prompt
    if (!targetDoc) {
      let lines = [
        title,
        subtitle,
        "",
        scopeLabel,
        `• Converted & Indexed: ${indexedCount}`,
        `  - Datalab (Cloud): ${converterCounts["Datalab (Cloud)"] || 0}`,
        `  - Docling (Local): ${converterCounts["Docling (Local)"] || 0}`,
        `  - MarkItDown (Local): ${converterCounts["MarkItDown (Local)"] || 0}`,
      ];
      if (converterCounts["Other"] > 0) lines.push(`  - Other: ${converterCounts["Other"]}`);
      lines.push(`• Cached (Pending Index): ${pendingIndexCount}`);
      lines.push(`• Not yet converted: ${unindexedCount}`);
      if (failureCount > 0) lines.push(`• Recorded Failures: ${failureCount}`);
      if (typeof Services !== "undefined" && Services.prompt) {
        Services.prompt.alert(window || null, title, lines.join("\n"));
      }
      return;
    }

    const XHTMLNS = "http://www.w3.org/1999/xhtml";

    let existing = targetDoc.getElementById("zocrates-status-modal-overlay");
    if (existing) existing.remove();

    let overlay = targetDoc.createElementNS(XHTMLNS, "div");
    overlay.id = "zocrates-status-modal-overlay";
    overlay.style.cssText = "position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0, 0, 0, 0.6); z-index: 999999; display: flex; align-items: center; justify-content: center; backdrop-filter: blur(3px); box-sizing: border-box;";

    const { theme, colors } = getActiveThemePalette(targetDoc);
    if (typeof overlay.setAttribute === "function") {
      overlay.setAttribute("data-theme", theme);
    }
    if (overlay.style) {
      if (typeof overlay.style.setProperty === "function") {
        overlay.style.setProperty("--bg-card", colors.bgCard);
        overlay.style.setProperty("--bg-dark", colors.bgDark);
        overlay.style.setProperty("--border", colors.border);
        overlay.style.setProperty("--text-main", colors.textMain);
        overlay.style.setProperty("--text-muted", colors.textMuted);
        overlay.style.setProperty("--primary", colors.primary);
        overlay.style.setProperty("--chip-bg", colors.chipBg);
      } else {
        overlay.style["--bg-card"] = colors.bgCard;
        overlay.style["--bg-dark"] = colors.bgDark;
        overlay.style["--border"] = colors.border;
        overlay.style["--text-main"] = colors.textMain;
        overlay.style["--text-muted"] = colors.textMuted;
        overlay.style["--primary"] = colors.primary;
        overlay.style["--chip-bg"] = colors.chipBg;
      }
    }

    let card = targetDoc.createElementNS(XHTMLNS, "div");
    card.style.cssText = "background: var(--bg-card, #1e293b); color: var(--text-main, #f1f5f9); border: 1px solid var(--border, rgba(255, 255, 255, 0.15)); border-radius: 10px; width: 480px; max-width: 90vw; max-height: 85vh; display: flex; flex-direction: column; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; box-sizing: border-box; overflow: hidden;";

    // Header
    let header = targetDoc.createElementNS(XHTMLNS, "div");
    header.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; padding: 12px 16px; border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.1)); background: var(--chip-bg, rgba(255, 255, 255, 0.03));";

    let headerTextContainer = targetDoc.createElementNS(XHTMLNS, "div");
    headerTextContainer.style.cssText = "display: flex; flex-direction: column; gap: 2px;";

    let titleEl = targetDoc.createElementNS(XHTMLNS, "div");
    titleEl.style.cssText = "font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px;";
    titleEl.textContent = title;
    headerTextContainer.appendChild(titleEl);

    let subEl = targetDoc.createElementNS(XHTMLNS, "div");
    subEl.style.cssText = "font-size: 11.5px; color: var(--text-muted, #94a3b8);";
    subEl.textContent = subtitle;
    headerTextContainer.appendChild(subEl);

    header.appendChild(headerTextContainer);

    let closeBtn = targetDoc.createElementNS(XHTMLNS, "button");
    closeBtn.textContent = "✕";
    closeBtn.style.cssText = "background: transparent; border: none; cursor: pointer; color: var(--text-muted, #94a3b8); font-size: 16px; padding: 2px 6px; border-radius: 4px; line-height: 1;";
    closeBtn.title = "Close (Esc)";

    let keyHandler = (e) => {
      if (e.key === "Escape") {
        closeModal();
      }
    };

    let closeModal = () => {
      overlay.remove();
      targetDoc.removeEventListener("keydown", keyHandler);
    };
    targetDoc.addEventListener("keydown", keyHandler);

    closeBtn.addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeModal();
    });
    header.appendChild(closeBtn);
    card.appendChild(header);

    // Body
    let modalBody = targetDoc.createElementNS(XHTMLNS, "div");
    modalBody.style.cssText = "padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; box-sizing: border-box;";

    // Summary Box
    let summaryBox = targetDoc.createElementNS(XHTMLNS, "div");
    summaryBox.style.cssText = "background: var(--chip-bg, rgba(255, 255, 255, 0.03)); border: 1px solid var(--border, rgba(255, 255, 255, 0.08)); border-radius: 6px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; line-height: 1.4;";

    if (scopeLabel) {
      let totalRow = targetDoc.createElementNS(XHTMLNS, "div");
      totalRow.style.cssText = "font-weight: 600; margin-bottom: 2px;";
      totalRow.textContent = scopeLabel;
      summaryBox.appendChild(totalRow);
    }

    let convRow = targetDoc.createElementNS(XHTMLNS, "div");
    convRow.textContent = `• Converted & Indexed: ${indexedCount}`;
    summaryBox.appendChild(convRow);

    let enginesList = targetDoc.createElementNS(XHTMLNS, "div");
    enginesList.style.cssText = "padding-left: 14px; font-size: 12px; color: var(--text-muted, #94a3b8); display: flex; flex-direction: column; gap: 2px;";

    let dRow = targetDoc.createElementNS(XHTMLNS, "div");
    dRow.textContent = `- Datalab (Cloud): ${converterCounts["Datalab (Cloud)"] || 0}`;
    enginesList.appendChild(dRow);

    let docRow = targetDoc.createElementNS(XHTMLNS, "div");
    docRow.textContent = `- Docling (Local): ${converterCounts["Docling (Local)"] || 0}`;
    enginesList.appendChild(docRow);

    let mRow = targetDoc.createElementNS(XHTMLNS, "div");
    mRow.textContent = `- MarkItDown (Local): ${converterCounts["MarkItDown (Local)"] || 0}`;
    enginesList.appendChild(mRow);

    if (converterCounts["Other"] > 0) {
      let oRow = targetDoc.createElementNS(XHTMLNS, "div");
      oRow.textContent = `- Other: ${converterCounts["Other"]}`;
      enginesList.appendChild(oRow);
    }
    summaryBox.appendChild(enginesList);

    let pendingRow = targetDoc.createElementNS(XHTMLNS, "div");
    pendingRow.style.cssText = `font-weight: 500; color: ${pendingIndexCount > 0 ? '#ca8a04' : 'inherit'}; margin-top: 2px;`;
    pendingRow.textContent = `• Cached (Pending Index): ${pendingIndexCount}`;
    summaryBox.appendChild(pendingRow);

    let unconvRow = targetDoc.createElementNS(XHTMLNS, "div");
    unconvRow.style.cssText = `font-weight: 500; color: ${unindexedCount > 0 ? '#f59e0b' : 'inherit'}; margin-top: 2px;`;
    unconvRow.textContent = `• Not yet converted: ${unindexedCount}`;
    summaryBox.appendChild(unconvRow);

    if (failureCount > 0) {
      let failRow = targetDoc.createElementNS(XHTMLNS, "div");
      failRow.style.cssText = "font-weight: 500; color: #ef4444;";
      failRow.textContent = `• Recorded Failures: ${failureCount}`;
      summaryBox.appendChild(failRow);
    }
    modalBody.appendChild(summaryBox);

    // Pending Index List Details
    if (pendingIndexItems.length > 0) {
      let pDetails = targetDoc.createElementNS(XHTMLNS, "details");
      pDetails.open = true;
      pDetails.style.cssText = "border: 1px solid rgba(202, 138, 4, 0.3); border-radius: 6px; padding: 8px 10px; background: rgba(202, 138, 4, 0.05);";

      let pSummary = targetDoc.createElementNS(XHTMLNS, "summary");
      pSummary.style.cssText = "font-weight: 600; color: #ca8a04; cursor: pointer; user-select: none; margin-bottom: 6px;";
      pSummary.textContent = `Cached Document(s) (Pending Index) (${pendingIndexItems.length})`;
      pDetails.appendChild(pSummary);

      let pListContainer = targetDoc.createElementNS(XHTMLNS, "div");
      pListContainer.style.cssText = "max-height: 110px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; padding-right: 4px;";

      pendingIndexItems.forEach(u => {
        let itemRow = targetDoc.createElementNS(XHTMLNS, "div");
        itemRow.style.cssText = "font-size: 12px; line-height: 1.35; padding: 3px 6px; border-radius: 3px; background: var(--chip-bg, rgba(255, 255, 255, 0.03)); display: flex; align-items: baseline; gap: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";

        let iconSpan = targetDoc.createElementNS(XHTMLNS, "span");
        let isPdf = u.content_type === "application/pdf" || (u.filename && u.filename.toLowerCase().endsWith(".pdf"));
        iconSpan.textContent = isPdf ? "📄" : "🌐";
        itemRow.appendChild(iconSpan);

        let textSpan = targetDoc.createElementNS(XHTMLNS, "span");
        textSpan.style.cssText = "overflow: hidden; text-overflow: ellipsis;";
        let displayTitle = u.title || u.filename || "Untitled Document";
        textSpan.textContent = displayTitle;
        textSpan.title = displayTitle + (u.filename && u.filename !== displayTitle ? ` (${u.filename})` : "");
        itemRow.appendChild(textSpan);

        pListContainer.appendChild(itemRow);
      });

      pDetails.appendChild(pListContainer);
      modalBody.appendChild(pDetails);
    }

    // Unconverted List Details
    let converterSelect = null;

    if (unconvertedItems.length > 0) {
      let details = targetDoc.createElementNS(XHTMLNS, "details");
      details.open = true;
      details.style.cssText = "border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 6px; padding: 8px 10px; background: rgba(245, 158, 11, 0.05);";

      let summary = targetDoc.createElementNS(XHTMLNS, "summary");
      summary.style.cssText = "font-weight: 600; color: #f59e0b; cursor: pointer; user-select: none; margin-bottom: 6px;";
      summary.textContent = `Unconverted Document(s) (${unconvertedItems.length})`;
      details.appendChild(summary);

      let filterInput = null;
      if (unconvertedItems.length > 8) {
        filterInput = targetDoc.createElementNS(XHTMLNS, "input");
        filterInput.type = "text";
        filterInput.placeholder = "Filter unconverted titles...";
        filterInput.style.cssText = "width: 100%; padding: 4px 8px; font-size: 11.5px; border-radius: 4px; border: 1px solid var(--border, rgba(255, 255, 255, 0.15)); background: var(--bg-dark, rgba(0, 0, 0, 0.25)); color: var(--text-main, inherit); margin-bottom: 6px; box-sizing: border-box;";
        details.appendChild(filterInput);
      }

      let listContainer = targetDoc.createElementNS(XHTMLNS, "div");
      listContainer.style.cssText = "max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; padding-right: 4px;";

      let renderList = (filterText = "") => {
        listContainer.innerHTML = "";
        let q = filterText.toLowerCase().trim();
        let filtered = unconvertedItems.filter(u => {
          let t = (u.title || "").toLowerCase();
          let f = (u.filename || "").toLowerCase();
          return !q || t.includes(q) || f.includes(q);
        });

        if (filtered.length === 0) {
          let empty = targetDoc.createElementNS(XHTMLNS, "div");
          empty.style.cssText = "color: var(--text-muted, #94a3b8); font-size: 11.5px; font-style: italic; padding: 4px 0;";
          empty.textContent = "No matching items found.";
          listContainer.appendChild(empty);
          return;
        }

        filtered.forEach(u => {
          let itemRow = targetDoc.createElementNS(XHTMLNS, "div");
          itemRow.style.cssText = "font-size: 12px; line-height: 1.35; padding: 3px 6px; border-radius: 3px; background: var(--chip-bg, rgba(255, 255, 255, 0.03)); display: flex; align-items: baseline; gap: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";

          let iconSpan = targetDoc.createElementNS(XHTMLNS, "span");
          let isPdf = u.contentType === "application/pdf" || (u.filename && u.filename.toLowerCase().endsWith(".pdf"));
          iconSpan.textContent = isPdf ? "📄" : "🌐";
          itemRow.appendChild(iconSpan);

          let textSpan = targetDoc.createElementNS(XHTMLNS, "span");
          textSpan.style.cssText = "overflow: hidden; text-overflow: ellipsis;";
          let displayTitle = u.title || u.filename || "Untitled Document";
          textSpan.textContent = displayTitle;
          textSpan.title = displayTitle + (u.filename && u.filename !== displayTitle ? ` (${u.filename})` : "");
          itemRow.appendChild(textSpan);

          listContainer.appendChild(itemRow);
        });
      };

      renderList();
      if (filterInput) {
        filterInput.addEventListener("input", (e) => {
          renderList(e.target.value);
        });
      }

      details.appendChild(listContainer);
      modalBody.appendChild(details);

      // Converter selection box
      let selectBox = targetDoc.createElementNS(XHTMLNS, "div");
      selectBox.style.cssText = "display: flex; flex-direction: column; gap: 6px; margin-top: 4px;";

      let selectLabel = targetDoc.createElementNS(XHTMLNS, "label");
      selectLabel.style.cssText = "font-weight: 600; font-size: 12px;";
      selectLabel.textContent = "Choose a converter:";
      selectBox.appendChild(selectLabel);

      converterSelect = targetDoc.createElementNS(XHTMLNS, "select");
      converterSelect.style.cssText = "width: 100%; padding: 6px 8px; font-size: 12px; border-radius: 5px; border: 1px solid var(--border, rgba(255, 255, 255, 0.2)); background: var(--bg-card, #0f172a); color: var(--text-main, inherit);";

      let optionsList = [
        { id: "datalab", label: "Datalab (Cloud)", requiresKey: true },
        { id: "docling_local", label: "Docling (Local)", requiresKey: false },
        { id: "markitdown", label: "MarkItDown (Local)", requiresKey: false },
      ];

      optionsList.forEach(opt => {
        let optEl = targetDoc.createElementNS(XHTMLNS, "option");
        optEl.value = opt.id;
        let label = opt.label;
        if (opt.id === activeGlobalConverterId) {
          label += " [Active Global]";
        }
        if (opt.requiresKey && !hasDatalabKey) {
          label += " [API Key Required]";
          optEl.disabled = true;
        }
        optEl.textContent = label;
        if (opt.id === activeGlobalConverterId && (!opt.requiresKey || hasDatalabKey)) {
          optEl.selected = true;
        }
        converterSelect.appendChild(optEl);
      });

      selectBox.appendChild(converterSelect);
      modalBody.appendChild(selectBox);
    }

    if (unconvertedItems.length === 0 && pendingIndexItems.length === 0) {
      let allGood = targetDoc.createElementNS(XHTMLNS, "div");
      allGood.style.cssText = "padding: 10px 12px; border-radius: 6px; background: rgba(34, 197, 94, 0.12); border: 1px solid rgba(34, 197, 94, 0.3); color: #22c55e; font-weight: 500; display: flex; align-items: center; gap: 8px;";
      allGood.textContent = "✅ All documents in scope are converted & indexed.";
      modalBody.appendChild(allGood);
    }

    if (embeddingInfo) {
      let embDetails = targetDoc.createElementNS(XHTMLNS, "details");
      embDetails.id = "zocrates-modal-embeddings-panel";
      embDetails.style.cssText = "border: 1px solid var(--border, rgba(255, 255, 255, 0.12)); border-radius: 6px; padding: 8px 10px; background: var(--chip-bg, rgba(255, 255, 255, 0.03));";

      let embSummary = targetDoc.createElementNS(XHTMLNS, "summary");
      embSummary.style.cssText = "font-weight: 600; font-size: 12px; cursor: pointer; user-select: none; color: var(--text-main, inherit);";
      embSummary.textContent = "Active Embeddings Model";
      embDetails.appendChild(embSummary);

      let embContent = targetDoc.createElementNS(XHTMLNS, "div");
      embContent.style.cssText = "margin-top: 8px; font-size: 11.5px; line-height: 1.6; color: var(--text-muted, #94a3b8); border-top: 1px solid var(--border, rgba(255, 255, 255, 0.08)); padding-top: 6px;";

      let createRow = (label, val, valStyle = "") => {
        let row = targetDoc.createElementNS(XHTMLNS, "div");
        let b = targetDoc.createElementNS(XHTMLNS, "b");
        b.textContent = "• " + label + ": ";
        b.style.color = "var(--text-main, inherit)";
        row.appendChild(b);
        let s = targetDoc.createElementNS(XHTMLNS, "span");
        s.textContent = val;
        if (valStyle) s.style.cssText = valStyle;
        row.appendChild(s);
        return row;
      };

      let statusColor = embeddingInfo.is_fallback ? "#f59e0b" : "#22c55e";
      let statusText = embeddingInfo.is_fallback ? "Fallback Active (OOM/Error)" : "Active";
      let deviceText = embeddingInfo.device || (embeddingInfo.mode === "cloud" ? "Cloud API" : "CPU");

      embContent.appendChild(createRow("Model Name", embeddingInfo.active_model || "None"));
      embContent.appendChild(createRow("Dimension", embeddingInfo.dimension != null ? `${embeddingInfo.dimension}` : "Unknown"));
      embContent.appendChild(createRow("Device", deviceText));
      embContent.appendChild(createRow("Chroma Collection (Local)", embeddingInfo.active_collection || "Unknown"));
      embContent.appendChild(createRow("Status", statusText, `color: ${statusColor}; font-weight: 500;`));

      embDetails.appendChild(embContent);
      modalBody.appendChild(embDetails);
    }

    card.appendChild(modalBody);

    // Footer
    let footer = targetDoc.createElementNS(XHTMLNS, "div");
    footer.style.cssText = "display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border, rgba(255, 255, 255, 0.1)); background: var(--chip-bg, rgba(255, 255, 255, 0.02));";

    let cancelBtn = targetDoc.createElementNS(XHTMLNS, "button");
    cancelBtn.textContent = (unconvertedItems.length > 0 || pendingIndexItems.length > 0) ? "Cancel" : "Close";
    cancelBtn.style.cssText = "padding: 6px 12px; font-size: 12px; font-weight: 500; cursor: pointer; border-radius: 5px; border: 1px solid var(--border, rgba(255, 255, 255, 0.2)); background: transparent; color: inherit;";
    cancelBtn.addEventListener("click", closeModal);
    footer.appendChild(cancelBtn);

    if (pendingIndexItems.length > 0) {
      let indexBtn = targetDoc.createElementNS(XHTMLNS, "button");
      indexBtn.textContent = `Index ${pendingIndexItems.length} Pending Document(s)`;
      indexBtn.style.cssText = "padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 5px; border: none; background: #ca8a04; color: #ffffff;";

      indexBtn.addEventListener("click", () => {
        let targetPendingKeys = pendingIndexItems.map(p => p.key);
        closeModal();
        this.startBatchIndex(targetPendingKeys, { window });
      });
      footer.appendChild(indexBtn);
    }

    if (unconvertedItems.length > 0) {
      let convertBtn = targetDoc.createElementNS(XHTMLNS, "button");
      convertBtn.textContent = `Convert ${unconvertedItems.length} Unconverted Item(s)`;
      convertBtn.style.cssText = "padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 5px; border: none; background: var(--primary, #6366f1); color: #ffffff;";

      convertBtn.addEventListener("click", () => {
        let chosenConverter = converterSelect ? converterSelect.value : activeGlobalConverterId;
        let targetUnconvertedKeys = unconvertedItems.map(u => u.key);
        closeModal();
        this.startBatchConvert(targetUnconvertedKeys, chosenConverter, { force: false, window });
      });
      footer.appendChild(convertBtn);
    }

    card.appendChild(footer);
    overlay.appendChild(card);

    let container = targetDoc.body || targetDoc.documentElement;
    container.appendChild(overlay);
  },

  async startBatchConvert(items, converterId, options = {}) {
    try {
      let keys = this.getAttachmentKeysFromSelected(items);
      Zotero.debug("Zocrates: startBatchConvert called for " + keys.length + " keys with converter " + converterId);

      if (!keys || keys.length === 0) {
        if (typeof Services !== "undefined" && Services.prompt) {
          Services.prompt.alert(options.window || null, "Zocrates", "No items selected.");
        }
        return;
      }

      if (typeof Zotero !== "undefined" && Zotero.DB && typeof Zotero.DB.queryAsync === "function") {
        try {
          await Zotero.DB.queryAsync("PRAGMA wal_checkpoint(TRUNCATE)");
        } catch (cpErr) {
          Zotero.debug("Zocrates: BatchConverter wal_checkpoint failed: " + cpErr);
        }
      }

      let serverUrl = this.getServerUrl();
      let converterLabel = this.getConverterLabel(converterId);
      let { fetchFn, AbortControllerClass, TextDecoderClass } = _resolveBatchGlobals(options.window);
      if (!fetchFn) {
        throw new Error("fetch API is not available in the current environment.");
      }
      let force = !!options.force;

      // 1. Pre-flight estimate check
      let estimate = null;
      try {
        let estRes = await fetchFn(
          `${serverUrl}/cost-estimate?converter_id=${encodeURIComponent(converterId)}&item_keys=${encodeURIComponent(keys.join(","))}&force=${force ? "true" : "false"}`,
          { headers: this.getAuthHeaders() }
        );
        if (estRes.ok) {
          estimate = await estRes.json();
          Zotero.debug("Zocrates: Cost estimate response: " + JSON.stringify(estimate));
        }
      } catch (e) {
        Zotero.debug("Zocrates: Error fetching cost estimate: " + e);
      }

      let targetDocs = estimate ? estimate.target_documents : keys.length;
      let alreadyConverted = estimate ? (estimate.already_converted_count || 0) : 0;
      let eligibleCount = estimate ? (estimate.eligible_documents ?? (targetDocs - alreadyConverted)) : keys.length;
      let totalPages = estimate ? (estimate.total_pages || 0) : 0;
      let costUsd = estimate ? (estimate.estimated_cost_usd || 0.0) : 0.0;

      if (eligibleCount === 0 && !force) {
        if (typeof Services !== "undefined" && Services.prompt) {
          Services.prompt.alert(
            options.window || null,
            "Zocrates Document Conversion",
            `All ${targetDocs} selected document(s) are already converted.\n\nNo conversion is needed.`
          );
        }
        return;
      }

      // 2. Pre-flight confirmation dialog for Datalab or multi-item selections
      if (!options.skipConfirm && (converterId === "datalab" || keys.length > 1)) {
        let promptLines = [
          `Re-convert document(s) with ${converterLabel}?`,
          "",
          `• Selected items: ${keys.length}`,
        ];

        if (alreadyConverted > 0) {
          promptLines.push(`• Already converted (will skip): ${alreadyConverted}`);
        }

        let mergeCrossPage = false;
        if (estimate && estimate.datalab_merge_cross_page !== undefined) {
          mergeCrossPage = Boolean(estimate.datalab_merge_cross_page);
        } else if (typeof Zotero !== "undefined" && Zotero.Prefs) {
          try {
            mergeCrossPage = Boolean(Zotero.Prefs.get("extensions.zocrates.datalabMergeCrossPage", true));
          } catch (e) {}
        }

        if (converterId === "datalab") {
          if (totalPages > 0) {
            promptLines.push(`• To Convert: ${eligibleCount} document(s) (${totalPages} PDF page(s) via Datalab)`);
            if (mergeCrossPage) {
              promptLines.push(`• Estimated Base Cost: ~$${costUsd.toFixed(4)} USD`);
              promptLines.push("• Cross-Page Merging: Active (incurs variable compute surcharge; final cost may be higher)");
            } else {
              promptLines.push(`• Estimated Cost: ~$${costUsd.toFixed(4)} USD`);
            }
          } else {
            promptLines.push(`• To Convert: ${eligibleCount} non-PDF document(s) (via local fallback, $0.00 USD)`);
          }
        } else {
          promptLines.push(`• To Convert: ${eligibleCount} document(s)`);
        }

        if (converterId !== "datalab" && estimate && estimate.vlm_captioning && estimate.vlm_captioning.enabled) {
          let vlmModel = estimate.vlm_captioning.model || "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free";
          let provLabel = estimate.vlm_captioning.provider === "ollama" ? "Ollama" : "OpenRouter";
          promptLines.push(`• Figure Enrichment: Active (${provLabel}: ${vlmModel.replace("openrouter/", "")})`);
        }

        promptLines.push("");
        promptLines.push("Proceed with conversion?");

        if (typeof Services !== "undefined" && Services.prompt) {
          let confirmed = Services.prompt.confirm(
            options.window || null,
            `Zocrates: ${converterLabel}`,
            promptLines.join("\n")
          );
          if (!confirmed) {
            Zotero.debug("Zocrates: User canceled batch conversion prompt.");
            return;
          }
        }
      }

      // 3. Set up AbortController and publish initial batch progress state
      let abortController = AbortControllerClass ? new AbortControllerClass() : null;
      let abortSignal = abortController ? abortController.signal : undefined;

      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: "starting",
          converterLabel: converterLabel,
          converterId: converterId,
          total: eligibleCount,
          currentIndex: 1,
          processed: 0,
          skipped: 0,
          failed: 0,
          isStopping: false,
          abort: () => {
            if (abortController) abortController.abort();
          },
        });
      }

      // 4. Connect to POST /batch-convert with SSE streaming
      try {
        Zotero.debug("Zocrates: Dispatching POST /batch-convert for " + keys.length + " keys");
        let response = await fetchFn(`${serverUrl}/batch-convert`, {
          method: "POST",
          headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            item_keys: keys,
            converter_id: converterId,
            force: force,
          }),
          signal: abortSignal,
        });

        if (!response.ok) {
          let errText = await response.text();
          throw new Error(`Server returned HTTP ${response.status}: ${errText}`);
        }

        let reader = response.body.getReader();
        let decoder = TextDecoderClass ? new TextDecoderClass("utf-8") : null;
        let buffer = "";
        let currentEvent = null;

        while (true) {
          let { done, value } = await reader.read();
          if (done) break;

          let chunkStr = decoder
            ? decoder.decode(value, { stream: true })
            : (typeof value === "string" ? value : new TextDecoder("utf-8").decode(value));
          buffer += chunkStr;
          let lines = buffer.split("\n");
          buffer = lines.pop(); // keep trailing incomplete segment

          for (let line of lines) {
            line = line.trim();
            if (!line) {
              currentEvent = null;
              continue;
            }
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              let dataStr = line.slice(5).trim();
              try {
                let data = JSON.parse(dataStr);
                this._handleBatchSSEEvent(currentEvent, data, converterLabel, converterId, abortController);
              } catch (jsonErr) {
                Zotero.debug("Zocrates: Error parsing SSE JSON: " + jsonErr);
              }
            }
          }
        }
        Zotero.debug("Zocrates: Batch convert SSE stream finished.");
      } catch (err) {
        Zotero.debug("Zocrates: Batch convert stream error / abort: " + err);
        if (err.name === "AbortError" || (abortController && abortController.signal.aborted)) {
          if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
            let cur = Zotero.Zocrates.BatchProgress.get();
            Zotero.Zocrates.BatchProgress.update({
              ...(cur || {}),
              status: "stopped",
              converterLabel: converterLabel,
              converterId: converterId,
              isStopping: false,
              abort: null,
            });
          }
          if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
            Zotero.Zocrates.Notifier.add("Batch conversion stopped by user.", "info");
          }
        } else {
          if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
            Zotero.Zocrates.BatchProgress.update({
              status: "error",
              converterLabel: converterLabel,
              converterId: converterId,
              error: err.message || String(err),
              abort: null,
            });
          }
          if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
            Zotero.Zocrates.Notifier.add(`Batch conversion error: ${err.message || err}`, "error");
          }
        }
      }

      // 5. Trigger ItemPane refresh if active item was affected
      try {
        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          Zotero.Zocrates.ZocratesItemPane.refresh();
        }
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", "");
        }
      } catch (e) {
        Zotero.debug("Zocrates: Error refreshing ItemPane: " + e);
      }
    } catch (topErr) {
      Zotero.debug("Zocrates: Top-level error in startBatchConvert: " + topErr);
      if (typeof Services !== "undefined" && Services.prompt) {
        Services.prompt.alert(
          options.window || null,
          "Zocrates Conversion Error",
          `Unexpected error starting conversion: ${topErr.message || topErr}`
        );
      }
    }
  },

  _handleBatchSSEEvent(event, data, converterLabel, converterId, abortController) {
    if (!data) return;

    if (event === "progress") {
      let currentIdx = data.current_index || (data.processed + 1);
      let total = data.total || 1;
      let title = data.current_title || data.current_key || "Document";

      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: data.status,
          converterLabel: converterLabel,
          converterId: converterId,
          currentTitle: title,
          currentKey: data.current_key,
          currentIndex: currentIdx,
          total: total,
          processed: data.processed,
          skipped: data.skipped,
          failed: data.failed,
          isStopping: abortController && abortController.signal.aborted,
          abort: () => {
            if (abortController) abortController.abort();
          },
        });
      }

      if (data.status === "completed") {
        try {
          if (
            Zotero.Zocrates &&
            Zotero.Zocrates.ZocratesItemPane &&
            typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
          ) {
            Zotero.Zocrates.ZocratesItemPane.refresh();
          }
        } catch (e) {}
        try {
          if (typeof Services !== "undefined" && Services.obs) {
            Services.obs.notifyObservers(
              null,
              "zocrates-document-converted",
              data.current_key || ""
            );
          }
        } catch (e) {}
      } else if (data.status === "skipped") {
        // Suppress per-item skip Notifier.add to prevent flooding notification drawer.
      } else if (data.status === "failed") {
        if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
          Zotero.Zocrates.Notifier.add(
            `[${currentIdx}/${total}] Failed (${converterLabel}): ${title} (${data.error || "Unknown error"})`,
            "error"
          );
        }
      }
    } else if (event === "complete") {
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: "completed",
          converterLabel: converterLabel,
          converterId: converterId,
          processed: data.processed,
          skipped: data.skipped,
          failed: data.failed,
          total: data.total,
          failedItems: data.failed_items || [],
          abort: null,
        });
      }

      let summaryText = `Batch conversion complete: ${data.processed} converted, ${data.skipped || 0} skipped, ${data.failed || 0} failed.`;
      if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
        Zotero.Zocrates.Notifier.add(
          summaryText,
          data.failed > 0 ? "error" : "info"
        );
      }

      try {
        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          Zotero.Zocrates.ZocratesItemPane.refresh();
        }
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", "");
        }
      } catch (e) {}
    }
  },

  async startBatchIndex(items, options = {}) {
    try {
      let keys = this.getAttachmentKeysFromSelected(items);
      Zotero.debug("Zocrates: startBatchIndex called for " + keys.length + " keys");

      if (!keys || keys.length === 0) {
        if (typeof Services !== "undefined" && Services.prompt) {
          Services.prompt.alert(options.window || null, "Zocrates", "No items to index.");
        }
        return;
      }

      let serverUrl = this.getServerUrl();
      let { fetchFn, AbortControllerClass, TextDecoderClass } = _resolveBatchGlobals(options.window);
      if (!fetchFn) {
        throw new Error("fetch API is not available in the current environment.");
      }

      let abortController = AbortControllerClass ? new AbortControllerClass() : null;
      let abortSignal = abortController ? abortController.signal : undefined;

      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: "starting",
          converterLabel: "Vector Indexer",
          converterId: "vector_index",
          total: keys.length,
          currentIndex: 1,
          processed: 0,
          skipped: 0,
          failed: 0,
          isStopping: false,
          abort: () => {
            if (abortController) abortController.abort();
          },
        });
      }

      try {
        let response = await fetchFn(`${serverUrl}/batch-index`, {
          method: "POST",
          headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            keys: keys,
            all_pending: false,
          }),
          signal: abortSignal,
        });

        if (!response.ok) {
          let errText = await response.text();
          throw new Error(`Server returned HTTP ${response.status}: ${errText}`);
        }

        let reader = response.body.getReader();
        let decoder = TextDecoderClass ? new TextDecoderClass("utf-8") : null;
        let buffer = "";
        let currentEvent = null;

        while (true) {
          let { done, value } = await reader.read();
          if (done) break;

          let chunkStr = decoder
            ? decoder.decode(value, { stream: true })
            : (typeof value === "string" ? value : new TextDecoder("utf-8").decode(value));
          buffer += chunkStr;
          let lines = buffer.split("\n");
          buffer = lines.pop();

          for (let line of lines) {
            line = line.trim();
            if (!line) {
              currentEvent = null;
              continue;
            }
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              let dataStr = line.slice(5).trim();
              try {
                let data = JSON.parse(dataStr);
                this._handleIndexSSEEvent(currentEvent, data, abortController);
              } catch (jsonErr) {
                Zotero.debug("Zocrates: Error parsing Index SSE JSON: " + jsonErr);
              }
            }
          }
        }
        Zotero.debug("Zocrates: Batch index SSE stream finished.");
      } catch (err) {
        Zotero.debug("Zocrates: Batch index stream error: " + err);
        if (err.name === "AbortError" || (abortController && abortController.signal.aborted)) {
          if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
            let cur = Zotero.Zocrates.BatchProgress.get();
            Zotero.Zocrates.BatchProgress.update({
              ...(cur || {}),
              status: "stopped",
              converterLabel: "Vector Indexer",
              converterId: "vector_index",
              isStopping: false,
              abort: null,
            });
          }
          if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
            Zotero.Zocrates.Notifier.add("Batch indexing stopped by user.", "info");
          }
        } else {
          if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
            Zotero.Zocrates.BatchProgress.update({
              status: "error",
              converterLabel: "Vector Indexer",
              converterId: "vector_index",
              error: err.message || String(err),
              abort: null,
            });
          }
          if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
            Zotero.Zocrates.Notifier.add(`Batch indexing error: ${err.message || err}`, "error");
          }
        }
      }

      try {
        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          Zotero.Zocrates.ZocratesItemPane.refresh();
        }
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", "");
        }
      } catch (e) {}
    } catch (topErr) {
      Zotero.debug("Zocrates: Error in startBatchIndex: " + topErr);
    }
  },

  _handleIndexSSEEvent(event, data, abortController) {
    if (!data) return;

    if (event === "batch_start") {
      let total = data.total || 1;
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: "indexing",
          converterLabel: "Vector Indexer",
          converterId: "vector_index",
          total: total,
          currentIndex: 1,
          processed: 0,
          skipped: 0,
          failed: 0,
          isStopping: abortController && abortController.signal.aborted,
          abort: () => {
            if (abortController) abortController.abort();
          },
        });
      }
    } else if (event === "item_start") {
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        let cur = Zotero.Zocrates.BatchProgress.get() || {};
        Zotero.Zocrates.BatchProgress.update({
          ...cur,
          status: "indexing",
          currentIndex: data.index || cur.currentIndex,
          currentTitle: data.title,
          currentKey: data.key,
          total: data.total || cur.total,
        });
      }
    } else if (event === "item_success") {
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        let cur = Zotero.Zocrates.BatchProgress.get() || {};
        Zotero.Zocrates.BatchProgress.update({
          ...cur,
          processed: (cur.processed || 0) + 1,
          currentIndex: data.index || cur.currentIndex,
          currentTitle: data.title,
        });
      }
      try {
        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          Zotero.Zocrates.ZocratesItemPane.refresh();
        }
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", data.key || "");
        }
      } catch (e) {}
    } else if (event === "item_error") {
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        let cur = Zotero.Zocrates.BatchProgress.get() || {};
        Zotero.Zocrates.BatchProgress.update({
          ...cur,
          failed: (cur.failed || 0) + 1,
        });
      }
      if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
        Zotero.Zocrates.Notifier.add(`[${data.index}/${data.total}] Indexing failed: ${data.title} (${data.error})`, "error");
      }
    } else if (event === "batch_complete") {
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          status: "completed",
          converterLabel: "Vector Indexer",
          converterId: "vector_index",
          processed: data.processed || 0,
          failed: data.failed || 0,
          total: data.total || 0,
          abort: null,
        });
      }
      let summaryText = `Batch indexing complete: ${data.processed || 0} indexed, ${data.failed || 0} failed.`;
      if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
        Zotero.Zocrates.Notifier.add(summaryText, data.failed > 0 ? "error" : "info");
      }
      try {
        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          Zotero.Zocrates.ZocratesItemPane.refresh();
        }
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", "");
        }
      } catch (e) {}
    }
  },
};

if (typeof Zotero !== "undefined") {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  Zotero.Zocrates.BatchConverter = BatchConverter;
}
if (typeof window !== "undefined") {
  window.BatchConverter = BatchConverter;
}
if (typeof globalThis !== "undefined") {
  globalThis.BatchConverter = BatchConverter;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = BatchConverter;
}

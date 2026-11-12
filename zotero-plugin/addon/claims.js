// Zocrates Claim Verification Inspector Standalone Window Script

(function() {
  let currentClaimsData = null;
  let activeStatusFilter = "all";
  let activePaperFilter = "";
  let activeSearchQuery = "";
  let activeSortField = "index";
  let activeSortOrder = "asc"; // 'asc' or 'desc'
  let allPassagesExpanded = false;
  let _isColumnResizing = false;
  let _justResizedColumn = false;

  function esc(s) {
    if (!s && s !== 0) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function sanitizeHTML(html) {
    if (!html || typeof html !== "string") return "";
    try {
      const openerSanitize = window.opener?.sanitizeHTML || window.top?.opener?.sanitizeHTML;
      if (typeof openerSanitize === "function") {
        return openerSanitize(html);
      }
      if (typeof DOMParser !== "undefined") {
        const parser = new DOMParser();
        const doc = parser.parseFromString(`<!DOCTYPE html><html><body>${html}</body></html>`, "text/html");
        const dangerous = doc.querySelectorAll("script, iframe, object, embed, link, meta, style");
        dangerous.forEach(el => el.remove());
        doc.querySelectorAll("*").forEach(el => {
          for (let i = el.attributes.length - 1; i >= 0; i--) {
            const attr = el.attributes[i].name.toLowerCase();
            if (attr.startsWith("on") || attr === "formaction" || attr === "id") {
              el.removeAttribute(el.attributes[i].name);
            }
          }
        });
        return doc.body.innerHTML;
      }
    } catch (_) {}
    return esc(html);
  }

  function getSidebarWindow() {
    try {
      if (currentClaimsData?._sidebarWindow && !currentClaimsData._sidebarWindow.closed) {
        return currentClaimsData._sidebarWindow;
      }
    } catch (_) {}

    try {
      const zot = (typeof Zotero !== "undefined" ? Zotero : null) ||
                  (typeof window !== "undefined" ? (window.Zotero || window.opener?.Zotero || window.top?.Zotero) : null);
      if (zot?.Zocrates?._sidebarWindow && !zot.Zocrates._sidebarWindow.closed) {
        return zot.Zocrates._sidebarWindow;
      }
    } catch (_) {}

    const opener = (typeof window !== "undefined" ? (window.opener || window.top?.opener || window.parent) : null);
    try {
      if (opener?.document) {
        const iframe = opener.document.querySelector("iframe[src*='sidebar.html']") ||
                       opener.document.getElementById("zocrates-sidebar-iframe");
        if (iframe?.contentWindow && !iframe.contentWindow.closed) {
          return iframe.contentWindow;
        }
      }
    } catch (_) {}

    try {
      if (opener && (typeof opener.openItemInReader === "function" || opener.document?.querySelector("#chat-messages"))) {
        return opener;
      }
    } catch (_) {}

    return opener || null;
  }

  function getBackendUrl() {
    try {
      const zot = (typeof Zotero !== "undefined" ? Zotero : null) ||
                  (typeof window !== "undefined" ? (window.Zotero || window.opener?.Zotero || window.top?.Zotero) : null);
      if (zot?.Zocrates?.getBackendUrl) {
        return zot.Zocrates.getBackendUrl();
      }
    } catch (_) {}
    try {
      const sidebarWin = getSidebarWindow();
      if (sidebarWin?.API_BASE) return sidebarWin.API_BASE;
      if (window.opener?.API_BASE) return window.opener.API_BASE;
      if (window.top?.opener?.API_BASE) return window.top.opener.API_BASE;
    } catch (_) {}
    return "http://127.0.0.1:8765";
  }

  function getAuthHeaders(extraHeaders = {}) {
    try {
      const zot = (typeof Zotero !== "undefined" ? Zotero : null) ||
                  (typeof window !== "undefined" ? (window.Zotero || window.opener?.Zotero || window.top?.Zotero) : null);
      if (zot?.Zocrates?.getAuthHeaders) {
        return zot.Zocrates.getAuthHeaders(extraHeaders);
      }
    } catch (_) {}
    try {
      const sidebarWin = getSidebarWindow();
      const openerFn = sidebarWin?.getAuthHeaders || window.opener?.getAuthHeaders || window.top?.opener?.getAuthHeaders;
      if (typeof openerFn === "function") {
        return openerFn(extraHeaders);
      }
    } catch (_) {}
    return Object.assign({}, extraHeaders);
  }

  function buildHighlightedPassage(sourceText, spans, supportingIndices) {
    if (!sourceText || !Array.isArray(spans) || spans.length === 0) {
      return sourceText || "";
    }
    const supSet = new Set((supportingIndices || []).map(Number));
    let output = "";
    let cursor = 0;

    for (let i = 0; i < spans.length; i++) {
      const span = spans[i];
      if (!span || typeof span !== "string") continue;
      const spanNum = i + 1;
      let idx = sourceText.indexOf(span, cursor);
      let matchLen = span.length;

      if (idx === -1) {
        const trimmed = span.trim();
        if (trimmed) {
          const trimIdx = sourceText.indexOf(trimmed, cursor);
          if (trimIdx !== -1) {
            idx = trimIdx;
            matchLen = trimmed.length;
          }
        }
      }

      if (idx === -1) {
        // Span cannot be located in sourceText at or after cursor;
        // skip without outputting or advancing cursor to prevent duplication/misalignment
        continue;
      }

      output += sourceText.substring(cursor, idx);
      const matchedText = sourceText.substring(idx, idx + matchLen);
      cursor = idx + matchLen;

      if (supSet.has(spanNum)) {
        const trimmed = matchedText.trim();
        if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
          output += matchedText.replace(/^(\s*\|\s*)/, "$1<span data-attr-highlight-tr=\"true\"></span>");
        } else {
          output += `<span class="sentence-attr-highlight">${matchedText}</span>`;
        }
      } else {
        output += matchedText;
      }
    }
    output += sourceText.substring(cursor);
    return output;
  }

  function syncSentenceAttribution(item, result) {
    item.sentence_attribution = result;

    // 1. Sync to currentClaimsData
    if (currentClaimsData && Array.isArray(currentClaimsData.assessments)) {
      const target = currentClaimsData.assessments.find(a =>
        (item.chunk_index && a.chunk_index === item.chunk_index && item.occurrence && a.occurrence === item.occurrence) ||
        (a.claim === item.claim && a.source_text === item.source_text)
      );
      if (target) {
        target.sentence_attribution = result;
      }
    }

    const sidebarWin = getSidebarWindow();

    // 2. Sync to opener's / sidebarWin's _zocratesCurrentClaimsData
    try {
      const targets = [
        sidebarWin?._zocratesCurrentClaimsData,
        window.opener?._zocratesCurrentClaimsData,
        window.top?.opener?._zocratesCurrentClaimsData,
      ];
      targets.forEach(cd => {
        if (cd && Array.isArray(cd.assessments)) {
          const match = cd.assessments.find(a =>
            (item.chunk_index && a.chunk_index === item.chunk_index && item.occurrence && a.occurrence === item.occurrence) ||
            (a.claim === item.claim && a.source_text === item.source_text)
          );
          if (match) {
            match.sentence_attribution = result;
          }
        }
      });
    } catch (_) {}

    // 3. Sync to message bubble._metadata on sidebarWin document
    try {
      const doc = sidebarWin?.document || window.opener?.document || window.top?.opener?.document;
      if (doc) {
        const bubbles = doc.querySelectorAll(".message-bubble");
        bubbles.forEach(b => {
          if (!b._metadata) return;
          const ver = b._metadata.citation_verification || b._metadata.citation_verification_telemetry;
          if (ver && Array.isArray(ver.assessments)) {
            const match = ver.assessments.find(a =>
              (item.chunk_index && a.chunk_index === item.chunk_index && item.occurrence && a.occurrence === item.occurrence) ||
              (a.claim === item.claim && a.source_text === item.source_text)
            );
            if (match) {
              match.sentence_attribution = result;

              // Incrementally add to parent bubble._metadata.turn_cost_usd and bubble._metadata.usage if present
              const newCost = Number(result?.cost_usd || 0.0);
              const usageObj = result?.usage || {};
              const addedTotal = Number(usageObj.total_tokens || (Number(usageObj.prompt_tokens || 0) + Number(usageObj.completion_tokens || 0)) || 0);

              const prevCost = Number(match._syncedCost || 0.0);
              const prevTotal = Number(match._syncedTotalTokens || 0);
              const deltaCost = Math.max(0, Number((newCost - prevCost).toFixed(6)));
              const deltaTotal = Math.max(0, addedTotal - prevTotal);

              if (deltaCost > 0 || deltaTotal > 0) {
                if (b._metadata.turn_cost_usd !== undefined && b._metadata.turn_cost_usd !== null) {
                  b._metadata.turn_cost_usd = Number((Number(b._metadata.turn_cost_usd) + deltaCost).toFixed(6));
                }
                if (b._metadata.usage && typeof b._metadata.usage === "object") {
                  b._metadata.usage.total_tokens = Number(b._metadata.usage.total_tokens || 0) + deltaTotal;
                  if (b._metadata.usage.cost_usd !== undefined && b._metadata.usage.cost_usd !== null) {
                    b._metadata.usage.cost_usd = Number((Number(b._metadata.usage.cost_usd) + deltaCost).toFixed(6));
                  }
                }
              }
              match._syncedCost = newCost;
              match._syncedTotalTokens = addedTotal;
            }
          }
        });
      }
    } catch (_) {}

    // 4. Sync to matching session message in sidebarWin.sessions if present
    try {
      const sessionsList = sidebarWin?.sessions || (typeof sidebarWin?.getSessions === "function" ? sidebarWin.getSessions() : null);
      if (Array.isArray(sessionsList)) {
        sessionsList.forEach(s => {
          if (!s || !Array.isArray(s.messages)) return;
          s.messages.forEach(m => {
            if (!m) return;
            const ver = m.citation_verification || m.metadata?.citation_verification || m.citation_verification_telemetry;
            if (ver && Array.isArray(ver.assessments)) {
              const match = ver.assessments.find(a =>
                (item.chunk_index && a.chunk_index === item.chunk_index && item.occurrence && a.occurrence === item.occurrence) ||
                (a.claim === item.claim && a.source_text === item.source_text)
              );
              if (match) {
                match.sentence_attribution = result;
              }
            }
          });
        });
      }
    } catch (_) {}

    updateAttributionHeaderStats();
  }

  function updateAttributionHeaderStats() {
    const badge = document.getElementById("badge-attr-cost");
    if (!badge) return;

    const assessments = currentClaimsData?.assessments;
    if (!Array.isArray(assessments)) {
      badge.style.display = "none";
      return;
    }

    let count = 0;
    let tokens = 0;
    let cost = 0.0;

    assessments.forEach(a => {
      const sa = a?.sentence_attribution;
      if (sa && sa.status === "ok") {
        count++;
        const u = sa.usage || {};
        const p = Number(u.prompt_tokens || 0);
        const c = Number(u.completion_tokens || 0);
        tokens += Number(u.total_tokens || (p + c) || 0);
        cost += Number(sa.cost_usd || 0.0);
      }
    });

    if (count > 0) {
      const costStr = cost < 0.001 ? cost.toFixed(5) : (cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2));
      badge.textContent = `🔍 Attribution: ${tokens.toLocaleString()} tokens${cost > 0 ? ` • $${costStr}` : " (Free)"}`;
      badge.title = `Sentence Attribution: ${count} evaluated\nTokens: ${tokens.toLocaleString()}\nExact Cost: $${cost.toFixed(6)}`;
      badge.style.display = "inline-flex";
    } else {
      badge.style.display = "none";
    }
  }

  function renderRichContent(rawText, isBlock = false) {
    if (!rawText && rawText !== 0) return "";
    let text = String(rawText).trim();

    // Strip artificial outer quotes so markdown and LaTeX formulas are not trapped
    if ((text.startsWith('"') && text.endsWith('"') && text.length >= 2) ||
        (text.startsWith('“') && text.endsWith('”') && text.length >= 2) ||
        (text.startsWith("'") && text.endsWith("'") && text.length >= 2)) {
      text = text.slice(1, -1).trim();
    }

    // 1. Extract block math $$...$$ and inline math $...$
    const placeholders = [];
    let processed = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, math) => {
      const ph = `@@BLOCKMATH_${placeholders.length}@@`;
      placeholders.push({ ph, math, type: 'block' });
      return ph;
    });

    processed = processed.replace(/(?<!\$)\$(?!\s)((?:\\\$|[^$])+?)(?<!\s)\$(?!\$)/g, (match, math) => {
      const ph = `@@INLINEMATH_${placeholders.length}@@`;
      placeholders.push({ ph, math, type: 'inline' });
      return ph;
    });

    // 2. Parse Markdown if marked is available
    let html = "";
    const markedLib = typeof marked !== "undefined" ? marked : (window.marked || window.opener?.marked);
    const containsTable = /\|[^\n\r]+\|/.test(processed);
    const useBlock = Boolean(isBlock || containsTable);

    if (markedLib) {
      try {
        if (useBlock && typeof markedLib.parse === "function") {
          html = markedLib.parse(processed);
        } else if (typeof markedLib.parseInline === "function") {
          html = markedLib.parseInline(processed);
        } else if (typeof markedLib.parse === "function") {
          html = markedLib.parse(processed);
        } else {
          html = esc(processed);
        }
      } catch (_) {
        html = esc(processed);
      }
    } else {
      html = esc(processed);
    }

    // 3. Render KaTeX math if katex is available
    const katexLib = typeof katex !== "undefined" ? katex : (window.katex || window.opener?.katex);
    placeholders.forEach(({ ph, math, type }) => {
      let renderedMath = "";
      if (katexLib && typeof katexLib.renderToString === "function") {
        try {
          renderedMath = katexLib.renderToString(math, {
            displayMode: type === 'block',
            throwOnError: false
          });
        } catch (_) {
          renderedMath = `<code>${esc(math)}</code>`;
        }
      } else {
        renderedMath = `<code>${esc(math)}</code>`;
      }
      html = html.replace(ph, renderedMath);
    });

    return sanitizeHTML(html);
  }

  function showToast(msg) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add("show");
    setTimeout(() => {
      toast.classList.remove("show");
    }, 2000);
  }

  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_) {
      return false;
    }
  }

  function formatAttributionNoteHtml(attr) {
    if (!attr) return "";
    const status = String(attr.status || "").toLowerCase();
    const unsupp = attr.unsupported_aspects ? String(attr.unsupported_aspects).trim() : "";
    const supportingIndices = Array.isArray(attr.supporting_indices) ? attr.supporting_indices : [];

    if (status === "error") {
      const errMsg = attr.error || attr.unsupported_aspects || "Service unavailable";
      return `<div class="sentence-attr-note error"><div>⚠️ Attribution Error: ${esc(errMsg)}</div></div>`;
    }
    if (supportingIndices.length > 0) {
      const count = supportingIndices.length;
      const cls = unsupp ? "sentence-attr-note partial" : "sentence-attr-note full";
      let html = `<div class="${cls}"><div>🎯 ${count} supporting sentence${count === 1 ? "" : "s"} highlighted above</div>`;
      if (unsupp) {
        html += `<div class="sentence-attr-missing"><strong>Not found in this passage:</strong> ${esc(unsupp)}</div>`;
      }
      html += `</div>`;
      return html;
    }
    let html = `<div class="sentence-attr-note none"><div>❌ No direct supporting sentences found in this passage</div>`;
    if (unsupp) {
      html += `<div class="sentence-attr-missing"><strong>Missing from passage:</strong> ${esc(unsupp)}</div>`;
    }
    html += `</div>`;
    return html;
  }

  function formatSourceAuthorYear(item) {
    let authorTag = "";
    if (Array.isArray(item.authors) && item.authors.length > 0) {
      const cleanAuthors = item.authors.map(a => String(a).split(",")[0].trim()).filter(Boolean);
      if (cleanAuthors.length === 1) authorTag = cleanAuthors[0];
      else if (cleanAuthors.length === 2) authorTag = cleanAuthors[0] + " & " + cleanAuthors[1];
      else if (cleanAuthors.length > 2) authorTag = cleanAuthors[0] + " et al.";
    } else if (typeof item.authors === "string" && item.authors.trim()) {
      authorTag = item.authors.trim();
    }

    if (!authorTag) {
      const title = item.title || "";
      authorTag = title ? (title.length > 28 ? title.slice(0, 26) + "…" : title) : "Unknown Source";
    }
    const yearTag = item.year ? ` (${item.year})` : "";
    return `${authorTag}${yearTag}`;
  }

  function resolveClaimsData(skipArguments = false) {
    if (!skipArguments) {
      try {
        if (window.arguments && window.arguments[0]) {
          return window.arguments[0];
        }
      } catch (e) {}
    }

    try {
      if (window.opener && window.opener._zocratesCurrentClaimsData) {
        return window.opener._zocratesCurrentClaimsData;
      }
      if (window.opener && window.opener.top && window.opener.top._zocratesCurrentClaimsData) {
        return window.opener.top._zocratesCurrentClaimsData;
      }
    } catch (e) {}

    try {
      const Zotero = window.Zotero || window.opener?.Zotero;
      if (Zotero?.Zocrates?._currentClaimsData) {
        return Zotero.Zocrates._currentClaimsData;
      }
    } catch (e) {}

    return { assessments: [], summary: {} };
  }

  function applyTheme() {
    let appliedTheme = null;

    try {
      if (currentClaimsData?.theme) {
        appliedTheme = currentClaimsData.theme;
      }
      if (!appliedTheme) {
        const data = resolveClaimsData();
        if (data?.theme) {
          appliedTheme = data.theme;
        }
      }
    } catch (e) {}

    if (!appliedTheme) {
      try {
        if (window.opener?.document?.body?.getAttribute("data-theme")) {
          appliedTheme = window.opener.document.body.getAttribute("data-theme");
        }
      } catch (e) {}
    }

    if (!appliedTheme) {
      try {
        const Zotero = window.Zotero || window.opener?.Zotero;
        if (Zotero?.Prefs) {
          const prefTheme = Zotero.Prefs.get("zocrates.theme");
          if (prefTheme && prefTheme !== "auto") {
            appliedTheme = prefTheme;
          } else {
            const zoteroTheme = Zotero.Prefs.get("extensions.zotero.theme") || "system";
            if (zoteroTheme === "dark") appliedTheme = "zotero-dark";
            else if (zoteroTheme === "light") appliedTheme = "zotero-light";
          }
        }
      } catch (e) {}
    }

    if (!appliedTheme) {
      const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches;
      appliedTheme = prefersDark ? "zotero-dark" : "zotero-light";
    }

    (document.body || document.documentElement)?.setAttribute("data-theme", appliedTheme);
  }

  function getFilteredAndSortedAssessments() {
    if (!currentClaimsData || !Array.isArray(currentClaimsData.assessments)) {
      return [];
    }

    const raw = currentClaimsData.assessments.map((item, originalIndex) => Object.assign({}, item, {
      _originalIndex: originalIndex + 1
    }));

    // 1. Status Filter
    let filtered = raw;
    if (activeStatusFilter !== "all") {
      filtered = filtered.filter(item => {
        if (activeStatusFilter === "KEPT") return item.verdict === "KEPT";
        if (activeStatusFilter === "DEMOTED") return item.verdict === "DEMOTED";
        if (activeStatusFilter === "FAIL_OPEN") return item.verdict === "FAIL_OPEN";
        return true;
      });
    }

    // 2. Paper Filter
    if (activePaperFilter) {
      filtered = filtered.filter(item => {
        const key = String(item.item_key || item.title || "");
        return key === activePaperFilter;
      });
    }

    // 3. Search Query
    if (activeSearchQuery) {
      const q = activeSearchQuery.toLowerCase().trim();
      filtered = filtered.filter(item => {
        const claim = String(item.claim || "").toLowerCase();
        const passage = String(item.source_text || "").toLowerCase();
        const title = String(item.title || "").toLowerCase();
        const section = String(item.section || "").toLowerCase();
        const authors = Array.isArray(item.authors) ? item.authors.join(" ").toLowerCase() : String(item.authors || "").toLowerCase();
        return claim.includes(q) || passage.includes(q) || title.includes(q) || section.includes(q) || authors.includes(q);
      });
    }

    // 4. Sort
    filtered.sort((a, b) => {
      let cmp = 0;
      if (activeSortField === "index") {
        cmp = a._originalIndex - b._originalIndex;
      } else if (activeSortField === "claim") {
        cmp = String(a.claim || "").localeCompare(String(b.claim || ""));
      } else if (activeSortField === "source") {
        const sa = formatSourceAuthorYear(a) + (a.section || "");
        const sb = formatSourceAuthorYear(b) + (b.section || "");
        cmp = sa.localeCompare(sb);
      } else if (activeSortField === "prob") {
        const pa = typeof a.support_probability === "number" ? a.support_probability : -1;
        const pb = typeof b.support_probability === "number" ? b.support_probability : -1;
        cmp = pa - pb;
      }
      return activeSortOrder === "asc" ? cmp : -cmp;
    });

    return filtered;
  }

  function renderHeaderStats(data) {
    const summary = data?.summary || {};
    const assessments = data?.assessments || [];

    const total = summary.batch_size !== undefined ? summary.batch_size : assessments.length;
    const kept = summary.kept !== undefined ? summary.kept : assessments.filter(a => a.verdict === "KEPT").length;
    const demoted = summary.demoted !== undefined ? summary.demoted : assessments.filter(a => a.verdict === "DEMOTED").length;
    const failOpen = summary.fail_open !== undefined ? summary.fail_open : assessments.filter(a => a.verdict === "FAIL_OPEN").length;

    // Model & latency badge
    const modelBadge = document.getElementById("claims-model-badge");
    if (modelBadge) {
      const model = data?.model || "decision-model";
      const lat = (summary.latency_ms !== undefined && summary.latency_ms !== null)
        ? summary.latency_ms
        : (data?.latency_ms !== undefined ? data.latency_ms : null);
      const latStr = lat !== null ? ` · ${Math.round(lat)}ms` : "";
      modelBadge.textContent = `${model}${latStr}`;
      modelBadge.title = `Evaluator model: ${model}${lat !== null ? ` (total latency: ${lat}ms)` : ""}`;
    }

    // Threshold badge
    const threshBadge = document.getElementById("claims-thresh-badge");
    if (threshBadge) {
      const thresh = data?.threshold !== undefined ? data.threshold : (summary.threshold !== undefined ? summary.threshold : 0.60);
      threshBadge.textContent = `Threshold p = ${Number(thresh).toFixed(3)}`;
      threshBadge.title = `Verification cutoff threshold: ${Number(thresh).toFixed(3)}`;
    }

    // Mean support badge
    const meanBadge = document.getElementById("claims-mean-badge");
    if (meanBadge) {
      if (summary.mean_support !== undefined && summary.mean_support !== null) {
        const stdPart = (summary.std_support !== undefined && summary.std_support !== null)
          ? ` ± ${Number(summary.std_support).toFixed(3)}`
          : "";
        meanBadge.textContent = `Mean p: ${Number(summary.mean_support).toFixed(3)}${stdPart}`;
        meanBadge.title = `Average supporting probability across evaluated claims`;
      } else {
        const validProbs = assessments
          .map(a => a.support_probability)
          .filter(p => typeof p === "number");
        if (validProbs.length > 0) {
          const mean = validProbs.reduce((a, b) => a + b, 0) / validProbs.length;
          meanBadge.textContent = `Mean p: ${mean.toFixed(3)}`;
        } else {
          meanBadge.textContent = `Mean p: --`;
        }
      }
    }

    // Counts badges
    const bTotal = document.getElementById("badge-total");
    if (bTotal) bTotal.textContent = `${total} Total`;

    // Update filter tab counts
    const tabAllCount = document.querySelector("#tab-all .tab-count");
    if (tabAllCount) tabAllCount.textContent = total;

    const tabVerCount = document.querySelector("#tab-verified .tab-count");
    if (tabVerCount) tabVerCount.textContent = kept;

    const tabRejCount = document.querySelector("#tab-rejected .tab-count");
    if (tabRejCount) tabRejCount.textContent = demoted;

    const tabFailCount = document.querySelector("#tab-fail-open .tab-count");
    if (tabFailCount) tabFailCount.textContent = failOpen;
  }

  function populatePaperFilter(assessments) {
    const select = document.getElementById("claims-paper-filter");
    const dropdownMenu = document.getElementById("claims-source-menu");
    const labelSpan = document.getElementById("claims-source-btn-label");
    const dropdownBtn = document.getElementById("claims-source-btn");

    const totalClaims = (assessments || []).length;
    const paperMap = new Map();
    (assessments || []).forEach(a => {
      const key = String(a.item_key || a.title || "Unknown");
      if (!paperMap.has(key)) {
        const label = formatSourceAuthorYear(a);
        const fullTitle = a.title || "";
        paperMap.set(key, { label, fullTitle, count: 0 });
      }
      paperMap.get(key).count += 1;
    });

    if (select) {
      select.innerHTML = "";
      const rootOpt = document.createElement("option");
      rootOpt.value = "";
      rootOpt.textContent = `All Sources (${totalClaims})`;
      select.appendChild(rootOpt);
      paperMap.forEach((meta, key) => {
        const opt = document.createElement("option");
        opt.value = key;
        opt.textContent = `${meta.label} (${meta.count})`;
        if (key === activePaperFilter) opt.selected = true;
        select.appendChild(opt);
      });
    }

    if (labelSpan) {
      if (!activePaperFilter) {
        labelSpan.textContent = `All Sources (${totalClaims})`;
      } else if (paperMap.has(activePaperFilter)) {
        const p = paperMap.get(activePaperFilter);
        labelSpan.textContent = `${p.label} (${p.count})`;
      } else {
        labelSpan.textContent = `All Sources (${totalClaims})`;
      }
    }

    if (dropdownMenu) {
      dropdownMenu.innerHTML = "";

      // 1. Root option: All Sources
      const rootItem = document.createElement("button");
      rootItem.type = "button";
      rootItem.className = "source-option-item" + (!activePaperFilter ? " selected" : "");
      rootItem.innerHTML = `
        <span class="source-option-check">${!activePaperFilter ? "✓" : ""}</span>
        <div class="source-option-content">
          <div class="source-option-header">
            <span class="source-option-title">All Sources</span>
            <span class="source-option-badge">${totalClaims} claims</span>
          </div>
          <div class="source-option-subtitle">Show claims evaluated across all papers in this response</div>
        </div>
      `;
      rootItem.addEventListener("click", () => {
        activePaperFilter = "";
        if (select) select.value = "";
        if (labelSpan) labelSpan.textContent = `All Sources (${totalClaims})`;
        dropdownMenu.classList.remove("show");
        dropdownBtn?.setAttribute("aria-expanded", "false");
        populatePaperFilter(currentClaimsData?.assessments);
        renderTableRows();
      });
      dropdownMenu.appendChild(rootItem);

      // 2. Per-paper options
      paperMap.forEach((meta, key) => {
        const isSelected = key === activePaperFilter;
        const itemBtn = document.createElement("button");
        itemBtn.type = "button";
        itemBtn.className = "source-option-item" + (isSelected ? " selected" : "");
        itemBtn.innerHTML = `
          <span class="source-option-check">${isSelected ? "✓" : ""}</span>
          <div class="source-option-content">
            <div class="source-option-header">
              <span class="source-option-title">${esc(meta.label)}</span>
              <span class="source-option-badge">${meta.count} claim${meta.count === 1 ? "" : "s"}</span>
            </div>
            ${meta.fullTitle ? `<div class="source-option-subtitle">${esc(meta.fullTitle)}</div>` : ""}
          </div>
        `;
        itemBtn.addEventListener("click", () => {
          activePaperFilter = key;
          if (select) select.value = key;
          if (labelSpan) labelSpan.textContent = `${meta.label} (${meta.count})`;
          dropdownMenu.classList.remove("show");
          dropdownBtn?.setAttribute("aria-expanded", "false");
          populatePaperFilter(currentClaimsData?.assessments);
          renderTableRows();
        });
        dropdownMenu.appendChild(itemBtn);
      });
    }
  }

  function renderTableRows() {
    const tbody = document.getElementById("claims-tbody");
    const emptyState = document.getElementById("claims-empty-state");
    if (!tbody) return;

    tbody.innerHTML = "";
    const items = getFilteredAndSortedAssessments();

    if (items.length === 0) {
      if (emptyState) emptyState.style.display = "block";
      return;
    }
    if (emptyState) emptyState.style.display = "none";

    const defaultThresh = currentClaimsData?.threshold !== undefined
      ? currentClaimsData.threshold
      : (currentClaimsData?.summary?.threshold !== undefined ? currentClaimsData.summary.threshold : 0.60);

    items.forEach(item => {
      const tr = document.createElement("tr");
      if (item.verdict === "DEMOTED") {
        tr.className = "row-rejected";
      }

      // 1. # Column (Clean sequential claim index only)
      const tdIdx = document.createElement("td");
      tdIdx.className = "col-idx";
      const idxCell = document.createElement("div");
      idxCell.className = "index-cell";

      const rowNum = document.createElement("span");
      rowNum.className = "row-num";
      rowNum.textContent = `${item._originalIndex}`;
      idxCell.appendChild(rowNum);

      tdIdx.appendChild(idxCell);
      tr.appendChild(tdIdx);

      // 2. Claim Column
      const tdClaim = document.createElement("td");
      tdClaim.className = "col-claim";

      const claimCell = document.createElement("div");
      claimCell.className = "claim-cell";

      const claimText = document.createElement("div");
      claimText.className = "claim-text";
      claimText.innerHTML = renderRichContent(item.claim || "No claim recorded");
      claimCell.appendChild(claimText);

      const copyClaimBtn = document.createElement("button");
      copyClaimBtn.className = "claim-copy-btn";
      copyClaimBtn.title = "Copy claim text to clipboard";
      copyClaimBtn.textContent = "📋";
      copyClaimBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const ok = await copyToClipboard(item.claim || "");
        if (ok) {
          copyClaimBtn.textContent = "✓";
          setTimeout(() => { copyClaimBtn.textContent = "📋"; }, 1200);
          showToast("Claim copied to clipboard");
        }
      });
      claimCell.appendChild(copyClaimBtn);

      tdClaim.appendChild(claimCell);
      tr.appendChild(tdClaim);

      // 3. Source & Section Column
      const tdSource = document.createElement("td");
      tdSource.className = "col-source";

      const sourceCell = document.createElement("div");
      sourceCell.className = "source-cell";

      const authorYearSpan = document.createElement("div");
      authorYearSpan.className = "source-author-year";
      authorYearSpan.textContent = formatSourceAuthorYear(item);
      if (item.title) authorYearSpan.title = item.title;
      sourceCell.appendChild(authorYearSpan);

      const sectionText = (item.section || "").trim();
      if (sectionText) {
        const sectionBadge = document.createElement("span");
        sectionBadge.className = "section-badge";
        sectionBadge.textContent = `§ ${sectionText}`;
        sectionBadge.title = `Section: ${sectionText}`;
        sourceCell.appendChild(sectionBadge);
      }

      const itemKey = item.item_key || "";
      const attKey = item.attachment_key || "";

      if (itemKey || attKey) {
        const actionsDiv = document.createElement("div");
        actionsDiv.className = "source-actions";

        const readerBtn = document.createElement("button");
        readerBtn.className = "source-action-btn";
        readerBtn.innerHTML = `📖 Open in Reader`;
        readerBtn.title = "Open paper at this section in Zotero PDF Reader";
        readerBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const targetKey = attKey || itemKey;
          const sidebarWin = getSidebarWindow();
          sidebarWin?.openItemInReader(targetKey, sectionText);
        });
        actionsDiv.appendChild(readerBtn);

        const libBtn = document.createElement("button");
        libBtn.className = "source-action-btn";
        libBtn.innerHTML = `📁 Select in Library`;
        libBtn.title = "Select paper in Zotero Library collection tree";
        libBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const sidebarWin = getSidebarWindow();
          sidebarWin?.selectItemInLibrary(itemKey || attKey);
        });
        actionsDiv.appendChild(libBtn);

        sourceCell.appendChild(actionsDiv);
      }

      tdSource.appendChild(sourceCell);
      tr.appendChild(tdSource);

      // 4. Supporting Text Column
      const tdPassage = document.createElement("td");
      tdPassage.className = "col-passage";

      const passageBox = document.createElement("div");
      passageBox.className = allPassagesExpanded ? "passage-box expanded" : "passage-box clamped";
      const textContent = (item.source_text || item.quote || item.text || "").trim();
      passageBox.title = "Click to toggle expanded view";

      function updatePassageDisplay() {
        if (!textContent) {
          passageBox.innerHTML = '<span class="empty-passage" style="font-style: italic; opacity: 0.6;">— (No passage captured) —</span>';
          return;
        }

        const attr = item.sentence_attribution;
        const showAttr = item._showSentenceAttribution !== false && attr && attr.status !== "error" && Array.isArray(attr.spans);

        if (showAttr) {
          const highlightedMd = buildHighlightedPassage(textContent, attr.spans, attr.supporting_indices || []);
          passageBox.innerHTML = renderRichContent(highlightedMd, true);
          // Highlight table rows matching marker
          passageBox.querySelectorAll("span[data-attr-highlight-tr]").forEach(marker => {
            const trEl = marker.closest("tr");
            if (trEl) trEl.classList.add("sentence-attr-highlight");
            marker.remove();
          });
        } else {
          passageBox.innerHTML = renderRichContent(textContent, true);
        }
      }

      // If attribution already exists, render highlighted immediately
      if (item.sentence_attribution && item._showSentenceAttribution === undefined) {
        item._showSentenceAttribution = true;
      }
      updatePassageDisplay();

      // Actions Bar beneath passageBox
      const actionsBar = document.createElement("div");
      actionsBar.className = "passage-actions-bar";

      const attrBtn = document.createElement("button");
      attrBtn.className = "btn-sentence-attr sentence-attr-btn";
      attrBtn.type = "button";

      const toggleBtn = document.createElement("button");
      toggleBtn.className = "btn-passage-toggle";
      toggleBtn.type = "button";
      toggleBtn.textContent = passageBox.classList.contains("expanded") ? "↕ Collapse" : "↕ Expand";
      toggleBtn.title = "Toggle expanded passage view";

      toggleBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (passageBox.classList.contains("clamped")) {
          passageBox.classList.remove("clamped");
          passageBox.classList.add("expanded");
          toggleBtn.textContent = "↕ Collapse";
        } else {
          passageBox.classList.remove("expanded");
          passageBox.classList.add("clamped");
          toggleBtn.textContent = "↕ Expand";
        }
      });

      passageBox.addEventListener("click", () => {
        if (passageBox.classList.contains("clamped")) {
          passageBox.classList.remove("clamped");
          passageBox.classList.add("expanded");
          toggleBtn.textContent = "↕ Collapse";
        } else {
          passageBox.classList.remove("expanded");
          passageBox.classList.add("clamped");
          toggleBtn.textContent = "↕ Expand";
        }
      });

      const noteWrap = document.createElement("div");
      noteWrap.className = "sentence-attr-note-wrap";

      function updateNoteDisplay() {
        noteWrap.innerHTML = "";
        const attr = item.sentence_attribution;
        if (!attr || item._showSentenceAttribution === false) {
          noteWrap.style.display = "none";
          return;
        }

        noteWrap.style.display = "block";
        noteWrap.innerHTML = formatAttributionNoteHtml(attr);
      }

      function updateBtnState() {
        if (!item.sentence_attribution) {
          attrBtn.textContent = "🔍 Obtain Sentence-Level Attribution";
          attrBtn.classList.remove("active");
          attrBtn.title = "Identify exact sentences supporting this claim using Nemotron-3.5-Lightning";
        } else if (item.sentence_attribution.status === "error") {
          attrBtn.textContent = "⚠️ Retry Sentence Attribution";
          attrBtn.classList.remove("active");
          attrBtn.title = `Attribution error: ${item.sentence_attribution.error || item.sentence_attribution.unsupported_aspects || "Click to retry"}`;
        } else if (item._showSentenceAttribution !== false) {
          attrBtn.textContent = "✨ Attribution: Shown";
          attrBtn.classList.add("active");
          attrBtn.title = "Click to toggle attribution highlights off";
        } else {
          attrBtn.textContent = "🔍 Attribution: Hidden";
          attrBtn.classList.remove("active");
          attrBtn.title = "Click to toggle attribution highlights on";
        }
      }

      updateBtnState();
      updateNoteDisplay();

      attrBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (item.sentence_attribution && item.sentence_attribution.status !== "error") {
          // Toggle on/off
          item._showSentenceAttribution = !item._showSentenceAttribution;
          updateBtnState();
          updatePassageDisplay();
          updateNoteDisplay();
          return;
        }

        if (!textContent) {
          showToast("No passage text available to evaluate attribution");
          return;
        }

        attrBtn.disabled = true;
        attrBtn.textContent = "⏳ Identifying supporting sentences…";

        try {
          const backendUrl = getBackendUrl();
          const authHeaders = getAuthHeaders({ "Content-Type": "application/json" });
          const payload = {
            claim: item.claim || "",
            source_text: textContent,
            session_id: currentClaimsData?.sessionId || currentClaimsData?.session_id || null,
            invocation_id: currentClaimsData?.invocationId || currentClaimsData?.invocation_id || null,
            chunk_index: item.chunk_index || 0,
            occurrence: item.occurrence || 0,
          };

          const res = await fetch(`${backendUrl}/citations/sentence_attribution`, {
            method: "POST",
            headers: authHeaders,
            body: JSON.stringify(payload),
          });

          if (!res.ok) {
            const errBody = await res.json().catch(() => ({}));
            throw new Error(errBody.detail || `Server returned ${res.status}`);
          }

          const result = await res.json();
          item.sentence_attribution = result;
          item._showSentenceAttribution = true;

          // Auto-expand passage
          passageBox.classList.remove("clamped");
          passageBox.classList.add("expanded");
          toggleBtn.textContent = "↕ Collapse";

          updateBtnState();
          updatePassageDisplay();
          updateNoteDisplay();

          syncSentenceAttribution(item, result);
        } catch (err) {
          console.error("Zocrates Claims: Sentence attribution error:", err);
          showToast(`Attribution failed: ${err.message || err}`);
          item.sentence_attribution = {
            status: "error",
            error: err.message || String(err),
          };
          item._showSentenceAttribution = true;
          updateBtnState();
          updatePassageDisplay();
          updateNoteDisplay();
        } finally {
          attrBtn.disabled = false;
        }
      });

      actionsBar.appendChild(attrBtn);
      actionsBar.appendChild(toggleBtn);
      tdPassage.appendChild(passageBox);
      tdPassage.appendChild(actionsBar);
      tdPassage.appendChild(noteWrap);
      tr.appendChild(tdPassage);

      // 5. Probability & Status Column
      const tdProb = document.createElement("td");
      tdProb.className = "col-prob";

      const probCell = document.createElement("div");
      probCell.className = "prob-status-cell";

      const probRow = document.createElement("div");
      probRow.className = "prob-val-row";

      const probNum = document.createElement("span");
      probNum.className = "prob-number";

      const hasProb = typeof item.support_probability === "number";
      const probVal = hasProb ? Number(item.support_probability) : null;
      const threshVal = item.threshold !== undefined ? Number(item.threshold) : Number(defaultThresh);

      if (hasProb) {
        probNum.textContent = `p = ${probVal.toFixed(3)}`;
        probNum.classList.add(probVal >= threshVal ? "high" : "low");
      } else {
        probNum.textContent = "p = N/A";
        probNum.classList.add("na");
      }
      probRow.appendChild(probNum);
      probCell.appendChild(probRow);

      const statusBadge = document.createElement("div");
      statusBadge.className = "status-badge";

      if (item.verdict === "KEPT" || (hasProb && probVal >= threshVal)) {
        statusBadge.classList.add("status-verified");
        statusBadge.textContent = "✅ Verified";
      } else if (item.verdict === "DEMOTED" || (hasProb && probVal < threshVal)) {
        statusBadge.classList.add("status-rejected");
        statusBadge.textContent = "❌ Rejected";
      } else {
        statusBadge.classList.add("status-unverified");
        statusBadge.textContent = "⚠️ Unverified";
      }
      probCell.appendChild(statusBadge);

      tdProb.appendChild(probCell);
      tr.appendChild(tdProb);

      tbody.appendChild(tr);
    });
  }

  function renderSortIndicators() {
    document.querySelectorAll("table.claims-table th.sortable").forEach(th => {
      const field = th.getAttribute("data-sort");
      const icon = th.querySelector(".sort-icon");
      if (field === activeSortField) {
        th.classList.add("sorted");
        if (icon) icon.textContent = activeSortOrder === "asc" ? "▲" : "▼";
      } else {
        th.classList.remove("sorted");
        if (icon) icon.textContent = "";
      }
    });
  }

  function renderAll() {
    if (!currentClaimsData) return;
    renderHeaderStats(currentClaimsData);
    populatePaperFilter(currentClaimsData.assessments);
    renderSortIndicators();
    renderTableRows();
  }

  function buildMarkdownExport() {
    const items = getFilteredAndSortedAssessments();
    if (items.length === 0) return "No claims evaluated.";

    const summary = currentClaimsData?.summary || {};
    const model = currentClaimsData?.model || "decision-model";
    const thresh = currentClaimsData?.threshold !== undefined ? currentClaimsData.threshold : 0.60;

    const lines = [
      `# Zocrates — Claim Verification Report`,
      ``,
      `- **Model**: \`${model}\``,
      `- **Threshold (τ)**: \`${Number(thresh).toFixed(3)}\``,
      `- **Evaluated Claims**: ${items.length}`,
      `- **Verified**: ${items.filter(i => i.verdict === "KEPT").length}`,
      `- **Rejected**: ${items.filter(i => i.verdict === "DEMOTED").length}`,
      `- **Unverified / Fail-open**: ${items.filter(i => i.verdict === "FAIL_OPEN").length}`,
      ``,
      `| # | Claim | Source | Section | Probability | Status |`,
      `|---|---|---|---|---|---|`,
    ];

    items.forEach(it => {
      const idx = `#${it._originalIndex}`;
      const claim = String(it.claim || "").replace(/\|/g, "\\|").replace(/\n/g, " ").trim();
      const source = formatSourceAuthorYear(it).replace(/\|/g, "\\|");
      const section = String(it.section || "").replace(/\|/g, "\\|");
      const prob = typeof it.support_probability === "number" ? it.support_probability.toFixed(3) : "N/A";
      const status = it.verdict === "KEPT" ? "✅ Verified" : (it.verdict === "DEMOTED" ? "❌ Rejected" : "⚠️ Unverified");
      lines.push(`| ${idx} | ${claim} | ${source} | ${section} | ${prob} | ${status} |`);
    });

    return lines.join("\n");
  }

  function buildJsonExport() {
    const items = getFilteredAndSortedAssessments();
    const payload = {
      model: currentClaimsData?.model || null,
      provider: currentClaimsData?.provider || null,
      threshold: currentClaimsData?.threshold ?? 0.60,
      summary: currentClaimsData?.summary || null,
      assessments: items
    };
    return JSON.stringify(payload, null, 2);
  }

  function initColumnResizing() {
    if (typeof document === "undefined" || !document.querySelector("table.claims-table")) {
      return;
    }
    const table = document.querySelector("table.claims-table");
    const ths = table.querySelectorAll("thead tr th");
    const container = document.getElementById("claims-table-container") || table.parentElement;

    let guide = container ? container.querySelector("#col-resize-guide") : null;
    if (!guide && container) {
      guide = document.createElement("div");
      guide.id = "col-resize-guide";
      guide.className = "col-resize-guide";
      container.appendChild(guide);
    }

    const MIN_COL_WIDTHS = [50, 140, 130, 160, 120];

    for (let i = 0; i <= ths.length - 2; i++) {
      const th = ths[i];
      let resizer = th.querySelector(".col-resizer");
      if (!resizer) {
        resizer = document.createElement("div");
        resizer.className = "col-resizer";
        resizer.setAttribute("data-col-index", String(i));
        resizer.title = "Drag to resize column (double-click to reset)";
        th.appendChild(resizer);
      }

      resizer.addEventListener("click", (e) => {
        e.stopPropagation();
      });

      resizer.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (ths.length >= 5) {
          ths[0].style.width = "50px";
          ths[1].style.width = "32%";
          ths[2].style.width = "22%";
          ths[3].style.width = "32%";
          ths[4].style.width = "140px";
        }
        table.style.tableLayout = "";
      });

      resizer.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();

        ths.forEach((t) => {
          t.style.width = t.getBoundingClientRect().width + "px";
        });
        table.style.tableLayout = "fixed";

        const colIdx = i;
        const colA = ths[colIdx];
        const colB = ths[colIdx + 1];
        const startX = e.clientX;
        const startWidthA = colA.getBoundingClientRect().width;
        const startWidthB = colB.getBoundingClientRect().width;
        const minA = MIN_COL_WIDTHS[colIdx] ?? 50;
        const minB = MIN_COL_WIDTHS[colIdx + 1] ?? 50;

        _isColumnResizing = true;
        resizer.classList.add("is-resizing");
        if (document.body) {
          document.body.classList.add("is-resizing-cols");
        }

        const updateGuidePosition = () => {
          if (!guide || !container) return;
          const containerRect = container.getBoundingClientRect();
          const leftPos = colA.getBoundingClientRect().right - containerRect.left + container.scrollLeft;
          guide.style.left = leftPos + "px";
          guide.style.top = container.scrollTop + "px";
          guide.style.height = container.clientHeight + "px";
          guide.style.display = "block";
        };

        updateGuidePosition();

        const onMouseMove = (moveEvent) => {
          if (moveEvent.buttons === 0) {
            onMouseUp();
            return;
          }
          const deltaX = moveEvent.clientX - startX;
          let newWidthA = startWidthA + deltaX;
          let newWidthB = startWidthB - deltaX;

          if (newWidthA < minA) {
            newWidthA = minA;
            newWidthB = startWidthA + startWidthB - minA;
          }
          if (newWidthB < minB) {
            newWidthB = minB;
            newWidthA = startWidthA + startWidthB - minB;
          }

          colA.style.width = newWidthA + "px";
          colB.style.width = newWidthB + "px";

          updateGuidePosition();
        };

        const onMouseUp = () => {
          window.removeEventListener("mousemove", onMouseMove);
          window.removeEventListener("mouseup", onMouseUp);
          window.removeEventListener("blur", onMouseUp);
          _isColumnResizing = false;
          _justResizedColumn = true;
          setTimeout(() => {
            _justResizedColumn = false;
          }, 100);
          resizer.classList.remove("is-resizing");
          if (document.body) {
            document.body.classList.remove("is-resizing-cols");
          }
          if (guide) {
            guide.style.display = "none";
          }
        };

        window.addEventListener("mousemove", onMouseMove);
        window.addEventListener("mouseup", onMouseUp);
        window.addEventListener("blur", onMouseUp);
      });
    }
  }

  function setupEventListeners() {
    // 1. Status Filter Tabs
    document.querySelectorAll(".filter-tab").forEach(tab => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".filter-tab").forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        activeStatusFilter = tab.getAttribute("data-filter") || "all";
        renderTableRows();
      });
    });

    // 2. Paper Filter Select & Custom Dropdown
    const paperSelect = document.getElementById("claims-paper-filter");
    if (paperSelect) {
      paperSelect.addEventListener("change", (e) => {
        activePaperFilter = e.target.value;
        renderTableRows();
      });
    }

    const sourceBtn = document.getElementById("claims-source-btn");
    const sourceMenu = document.getElementById("claims-source-menu");
    if (sourceBtn && sourceMenu) {
      sourceBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = sourceMenu.classList.contains("show");
        sourceMenu.classList.toggle("show");
        sourceBtn.setAttribute("aria-expanded", !isOpen ? "true" : "false");
      });

      document.addEventListener("click", (e) => {
        if (!sourceMenu.contains(e.target) && e.target !== sourceBtn) {
          sourceMenu.classList.remove("show");
          sourceBtn.setAttribute("aria-expanded", "false");
        }
      });
    }

    // 3. Search Input
    const searchInput = document.getElementById("claims-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        activeSearchQuery = e.target.value;
        renderTableRows();
      });
    }

    // 4. Expand / Collapse All Passages
    const toggleAllBtn = document.getElementById("btn-toggle-all-passages");
    if (toggleAllBtn) {
      toggleAllBtn.addEventListener("click", () => {
        allPassagesExpanded = !allPassagesExpanded;
        toggleAllBtn.textContent = allPassagesExpanded ? "↕ Collapse All" : "↕ Expand All";
        document.querySelectorAll(".passage-box").forEach(box => {
          if (allPassagesExpanded) {
            box.classList.remove("clamped");
            box.classList.add("expanded");
          } else {
            box.classList.remove("expanded");
            box.classList.add("clamped");
          }
        });
      });
    }

    // 5. Table Header Sort Controls
    document.querySelectorAll("table.claims-table th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        if (_isColumnResizing || _justResizedColumn) return;
        const field = th.getAttribute("data-sort");
        if (field === activeSortField) {
          activeSortOrder = activeSortOrder === "asc" ? "desc" : "asc";
        } else {
          activeSortField = field;
          activeSortOrder = (field === "prob") ? "desc" : "asc";
        }
        renderSortIndicators();
        renderTableRows();
      });
    });

    // 6. Copy Menu Dropdown Toggle
    const copyToggleBtn = document.getElementById("btn-copy-dropdown-toggle");
    const copyMenu = document.getElementById("copy-menu");
    if (copyToggleBtn && copyMenu) {
      copyToggleBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        copyMenu.classList.toggle("show");
      });

      document.addEventListener("click", (e) => {
        if (!copyMenu.contains(e.target) && e.target !== copyToggleBtn) {
          copyMenu.classList.remove("show");
        }
      });
    }

    // 7. Copy Markdown
    const copyMdBtn = document.getElementById("btn-copy-markdown");
    if (copyMdBtn) {
      copyMdBtn.addEventListener("click", async () => {
        if (copyMenu) copyMenu.classList.remove("show");
        const md = buildMarkdownExport();
        const ok = await copyToClipboard(md);
        if (ok) showToast("Markdown table copied to clipboard!");
      });
    }

    // 8. Copy JSON
    const copyJsonBtn = document.getElementById("btn-copy-json");
    if (copyJsonBtn) {
      copyJsonBtn.addEventListener("click", async () => {
        if (copyMenu) copyMenu.classList.remove("show");
        const jsonStr = buildJsonExport();
        const ok = await copyToClipboard(jsonStr);
        if (ok) showToast("JSON data copied to clipboard!");
      });
    }

    // 9. Column Resizing
    initColumnResizing();

    // Escape key to close
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        window.close();
      }
    });
  }

  // Window reuse and external updates
  window.updateClaimsData = function(data) {
    if (!data) return;
    currentClaimsData = data;
    try {
      if (window.arguments) {
        window.arguments[0] = data;
      }
    } catch (e) {}
    applyTheme();
    renderAll();
    updateAttributionHeaderStats();
  };

  // Re-check on focus if parent pushed newer data
  window.addEventListener("focus", () => {
    applyTheme();
    try {
      const data = resolveClaimsData(true);
      if (data && data !== currentClaimsData && data.assessments && data.assessments !== currentClaimsData?.assessments) {
        try {
          if (window.arguments) {
            window.arguments[0] = data;
          }
        } catch (e) {}
        currentClaimsData = data;
        renderAll();
      }
    } catch (e) {}
    updateAttributionHeaderStats();
  });

  window.matchMedia?.("(prefers-color-scheme: dark)")?.addEventListener("change", () => {
    applyTheme();
  });

  // Initial render
  function init() {
    applyTheme();
    setupEventListeners();
    const data = resolveClaimsData();
    currentClaimsData = data;
    renderAll();
    updateAttributionHeaderStats();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      buildHighlightedPassage,
      renderRichContent,
      sanitizeHTML,
      formatSourceAuthorYear,
      formatAttributionNoteHtml,
      syncSentenceAttribution,
      updateAttributionHeaderStats,
      getSidebarWindow,
      updateClaimsData: (typeof window !== "undefined" ? window.updateClaimsData : null),
      initColumnResizing,
      setupEventListeners,
      renderSortIndicators,
      resetSortingState: () => { activeSortField = "index"; activeSortOrder = "asc"; },
      getIsColumnResizing: () => _isColumnResizing,
      getJustResizedColumn: () => _justResizedColumn,
    };
  }
})();

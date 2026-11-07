// Zocrates Claim Verification Inspector Standalone Window Script

(function() {
  let currentClaimsData = null;
  let activeStatusFilter = "all";
  let activePaperFilter = "";
  let activeSearchQuery = "";
  let activeSortField = "index";
  let activeSortOrder = "asc"; // 'asc' or 'desc'
  let allPassagesExpanded = false;

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

  function renderRichContent(rawText) {
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
    if (markedLib) {
      try {
        if (typeof markedLib.parseInline === "function") {
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
      threshBadge.textContent = `τ = ${Number(thresh).toFixed(3)}`;
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

    const bVerified = document.getElementById("badge-verified");
    if (bVerified) bVerified.textContent = `${kept} Verified`;

    const bRejected = document.getElementById("badge-rejected");
    if (bRejected) bRejected.textContent = `${demoted} Rejected`;

    const bFailOpen = document.getElementById("badge-fail-open");
    if (bFailOpen) {
      bFailOpen.textContent = `${failOpen} Fail-open`;
      bFailOpen.style.display = failOpen > 0 ? "inline-flex" : "none";
    }

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
    if (!select) return;

    const savedVal = activePaperFilter;
    select.innerHTML = "";

    const paperMap = new Map();
    (assessments || []).forEach(a => {
      const key = String(a.item_key || a.title || "Unknown");
      if (!paperMap.has(key)) {
        const label = formatSourceAuthorYear(a);
        paperMap.set(key, { label, count: 0 });
      }
      paperMap.get(key).count += 1;
    });

    const rootOption = document.createElement("option");
    rootOption.value = "";
    rootOption.textContent = `All Sources (${paperMap.size})`;
    select.appendChild(rootOption);

    paperMap.forEach((meta, key) => {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = `${meta.label} (${meta.count})`;
      if (key === savedVal) {
        opt.selected = true;
      }
      select.appendChild(opt);
    });

    if (savedVal && !paperMap.has(savedVal)) {
      activePaperFilter = "";
      rootOption.selected = true;
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
      rowNum.textContent = `#${item._originalIndex}`;
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
        readerBtn.innerHTML = `📖 Reader`;
        readerBtn.title = "Open paper at this section in Zotero PDF Reader";
        readerBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const targetKey = attKey || itemKey;
          const opener = window.opener || window.top?.opener || window.parent;
          const fn = opener?.openItemInReader || window.top?.openItemInReader || window.parent?.openItemInReader;
          if (typeof fn === "function") {
            fn(targetKey, sectionText);
          } else {
            console.warn("Zocrates Claims: openItemInReader not found on opener window");
          }
        });
        actionsDiv.appendChild(readerBtn);

        const libBtn = document.createElement("button");
        libBtn.className = "source-action-btn";
        libBtn.innerHTML = `📁 Library`;
        libBtn.title = "Select paper in Zotero Library collection tree";
        libBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const opener = window.opener || window.top?.opener || window.parent;
          const fn = opener?.selectItemInLibrary || opener?.selectItemInZotero || window.top?.selectItemInLibrary || window.top?.selectItemInZotero;
          if (typeof fn === "function") {
            fn(itemKey || attKey);
          } else {
            console.warn("Zocrates Claims: selectItemInLibrary not found on opener window");
          }
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
      if (textContent) {
        passageBox.innerHTML = renderRichContent(textContent);
      } else {
        passageBox.innerHTML = '<span class="empty-passage" style="font-style: italic; opacity: 0.6;">— (No passage captured) —</span>';
      }
      passageBox.title = "Click to toggle expanded view";

      passageBox.addEventListener("click", () => {
        if (passageBox.classList.contains("clamped")) {
          passageBox.classList.remove("clamped");
          passageBox.classList.add("expanded");
        } else {
          passageBox.classList.remove("expanded");
          passageBox.classList.add("clamped");
        }
      });

      tdPassage.appendChild(passageBox);
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

      const threshNote = document.createElement("span");
      threshNote.className = "thresh-note";
      threshNote.textContent = `τ = ${threshVal.toFixed(3)}`;
      probCell.appendChild(threshNote);

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

    // 2. Paper Filter Select
    const paperSelect = document.getElementById("claims-paper-filter");
    if (paperSelect) {
      paperSelect.addEventListener("change", (e) => {
        activePaperFilter = e.target.value;
        renderTableRows();
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

    // 9. Close Button
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        window.close();
      });
    }

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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

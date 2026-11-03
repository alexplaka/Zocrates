// Zocrates Execution Trace Standalone Window Script

(function() {
  let currentTraceData = null;

  function esc(s) {
    if (!s && s !== 0) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function syntaxHighlightJson(jsonStr) {
    if (!jsonStr) return "";
    let out = "";
    let last = 0;
    const re = /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g;
    let m;
    while ((m = re.exec(jsonStr)) !== null) {
      out += esc(jsonStr.slice(last, m.index));
      const match = m[0];
      let cls = "color: #38bdf8;"; // number
      if (/^"/.test(match)) {
        if (/:$/.test(match)) {
          cls = "color: #93c5fd; font-weight: 600;"; // key
        } else {
          cls = "color: #4ade80;"; // string
        }
      } else if (/true|false/.test(match)) {
        cls = "color: #f472b6; font-weight: 600;"; // boolean
      } else if (/null/.test(match)) {
        cls = "color: #94a3b8; font-style: italic;"; // null
      }
      out += '<span style="' + cls + '">' + esc(match) + '</span>';
      last = m.index + match.length;
    }
    out += esc(jsonStr.slice(last));
    return out;
  }

  function formatAuthorName(role, rawAuthor) {
    if (role === "user" || rawAuthor === "User") return "User";
    let name = (rawAuthor || "").trim();
    if (name.startsWith("Zocrates/")) name = name.substring(9).trim();
    if (!name || name.toLowerCase() === "zocrates" || name.toLowerCase() === "agent") return "Dialogue Agent";
    name = name.replace(/sub[-_]?agent/gi, "agent");
    const normalized = name.toLowerCase().replace(/[-_]/g, " ").trim();
    if (normalized === "synthesizer agent" || normalized === "synthesizer" || normalized === "synthesis agent") return "Synthesis Agent";
    if (normalized === "search agent" || normalized === "search") return "Search Agent";
    if (normalized === "coordinator agent" || normalized === "coordinator" || normalized === "dialogue agent" || normalized === "dialogue") return "Dialogue Agent";
    let formatted = name.split(/[-_]+/).map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");
    formatted = formatted.replace(/\bSubagent\b/gi, "Agent").replace(/\bSynthesizer\b/gi, "Synthesis").replace(/\bCoordinator\b/gi, "Dialogue");
    return formatted || "Dialogue Agent";
  }

  function resolveTraceData(skipArguments = false) {
    if (!skipArguments) {
      try {
        if (window.arguments && window.arguments[0]) {
          return window.arguments[0];
        }
      } catch (e) {}
    }

    try {
      if (window.opener && window.opener._zocratesCurrentTraceData) {
        return window.opener._zocratesCurrentTraceData;
      }
      if (window.opener && window.opener.top && window.opener.top._zocratesCurrentTraceData) {
        return window.opener.top._zocratesCurrentTraceData;
      }
    } catch (e) {}

    try {
      const Zotero = window.Zotero || window.opener?.Zotero;
      if (Zotero?.Zocrates?._currentTraceData) {
        return Zotero.Zocrates._currentTraceData;
      }
    } catch (e) {}

    return { steps: [], metadata: {} };
  }

  function renderTrace(data) {
    currentTraceData = data;
    const root = document.getElementById("trace-root");
    if (!root) return;

    const steps = Array.isArray(data?.steps) ? data.steps : [];
    const meta = data?.metadata || {};

    const modelName = meta.model || "Dialogue Agent";
    const durationStr = meta.duration ? `${meta.duration}s` : "";
    const totalTokens = meta.usage?.total_tokens ? Number(meta.usage.total_tokens).toLocaleString() : null;

    let turnCost = 0.0;
    if (meta.turn_cost_usd !== undefined && meta.turn_cost_usd !== null) {
      turnCost = Number(meta.turn_cost_usd);
    } else if (meta.cost_usd !== undefined && meta.cost_usd !== null) {
      turnCost = Number(meta.cost_usd);
    } else if (meta.usage?.turn_cost_usd !== undefined && meta.usage?.turn_cost_usd !== null) {
      turnCost = Number(meta.usage.turn_cost_usd);
    } else if (meta.usage?.cost_usd !== undefined && meta.usage?.cost_usd !== null) {
      turnCost = Number(meta.usage.cost_usd);
    } else if (meta.metadata) {
      const sub = meta.metadata;
      if (sub.turn_cost_usd !== undefined && sub.turn_cost_usd !== null) turnCost = Number(sub.turn_cost_usd);
      else if (sub.cost_usd !== undefined && sub.cost_usd !== null) turnCost = Number(sub.cost_usd);
      else if (sub.usage?.turn_cost_usd !== undefined && sub.usage?.turn_cost_usd !== null) turnCost = Number(sub.usage.turn_cost_usd);
      else if (sub.usage?.cost_usd !== undefined && sub.usage?.cost_usd !== null) turnCost = Number(sub.usage.cost_usd);
    }
    if (isNaN(turnCost)) turnCost = 0.0;

    let costStr = "$0.00";
    if (turnCost > 0 && turnCost < 0.005) {
      costStr = "<$0.01";
    } else if (turnCost >= 0.005) {
      costStr = "$" + turnCost.toFixed(2);
    }
    const exactCostStr = "$" + turnCost.toFixed(6);

    let stepsHtml = "";
    if (steps.length === 0) {
      stepsHtml = `<div class="empty-state">No intermediate execution steps were recorded for this turn.</div>`;
    } else {
      steps.forEach((step, idx) => {
        const stepNum = idx + 1;
        const type = step.type || "step";
        const stepId = `step-${idx}`;

        let typeBadgeClass = "badge-call";
        let typeLabel = "Step";
        let icon = "⚙️";

        if (type === "thought") {
          typeBadgeClass = "badge-thought";
          typeLabel = "Thought";
          icon = "💭";
        } else if (type === "tool_call") {
          typeBadgeClass = "badge-call";
          typeLabel = "Tool Call";
          icon = "🔧";
        } else if (type === "tool_return") {
          typeBadgeClass = "badge-return";
          typeLabel = "Tool Return";
          icon = "📦";
        } else if (type === "agent_text") {
          typeBadgeClass = "badge-note";
          typeLabel = "Observation";
          icon = "📝";
        }

        const author = step.author ? formatAuthorName("assistant", step.author) : "";
        const stepName = step.name || "";

        let contentHtml = "";
        let preview = "";

        if (type === "thought") {
          const text = step.content || step.text || "";
          preview = text.replace(/\s+/g, " ").trim();
          if (preview.length > 70) preview = preview.substring(0, 70) + "...";
          contentHtml = `<div class="thought-body">${esc(text)}</div>`;
        } else if (type === "tool_call") {
          const args = step.args || {};
          const argsJson = JSON.stringify(args, null, 2);
          if (args.query) {
            preview = `query: "${String(args.query).substring(0, 50)}${String(args.query).length > 50 ? '...' : ''}"`;
          } else if (Object.keys(args).length > 0) {
            preview = Object.keys(args).join(", ");
          }
          contentHtml = `
            <div class="code-box">
              <div class="code-box-header">
                <span>Arguments</span>
              </div>
              <pre><code>${syntaxHighlightJson(argsJson)}</code></pre>
            </div>
          `;
        } else if (type === "tool_return") {
          let respStr = "";
          let isJson = false;
          try {
            if (typeof step.response === "object" && step.response !== null) {
              respStr = JSON.stringify(step.response, null, 2);
              isJson = true;
            } else {
              respStr = String(step.response || "");
              const trimmed = respStr.trim();
              if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
                try {
                  const parsed = JSON.parse(trimmed);
                  respStr = JSON.stringify(parsed, null, 2);
                  isJson = true;
                } catch (e) {
                  isJson = false;
                }
              }
            }
          } catch (e) {
            respStr = String(step.response || "");
          }

          const rawOneLine = respStr.replace(/\s+/g, " ").trim();
          preview = rawOneLine.length > 70 ? rawOneLine.substring(0, 70) + "..." : rawOneLine;

          const formattedCode = isJson ? syntaxHighlightJson(respStr) : esc(respStr);
          contentHtml = `
            <div class="code-box">
              <div class="code-box-header">
                <span>Response Payload</span>
              </div>
              <pre><code>${formattedCode}</code></pre>
            </div>
          `;
        } else if (type === "agent_text") {
          const text = step.text || "";
          preview = text.replace(/\s+/g, " ").trim();
          if (preview.length > 70) preview = preview.substring(0, 70) + "...";
          contentHtml = `<div class="note-body">${esc(text)}</div>`;
        }

        stepsHtml += `
          <div class="trace-card collapsed" id="${stepId}" data-type="${esc(type)}">
            <div class="trace-card-header">
              <div class="trace-card-header-left">
                <span class="trace-chevron">▶</span>
                <span class="step-num-badge">#${stepNum}</span>
                <span class="step-type-badge ${typeBadgeClass}">${icon} ${typeLabel}</span>
                ${author ? `<span class="step-author">${esc(author)}</span>` : ""}
                ${stepName ? `<span class="step-name">${esc(stepName)}</span>` : ""}
                ${preview ? `<span class="step-preview">${esc(preview)}</span>` : ""}
              </div>
            </div>
            <div class="trace-card-body">
              ${contentHtml}
            </div>
          </div>
        `;
      });
    }

    root.innerHTML = `
      <header>
        <div class="header-left">
          <h2 class="header-title">
            <span>⚡</span> Zocrates — Execution Trace
          </h2>
          <div class="header-stats">
            <span class="stat-badge">${esc(modelName)}</span>
            ${durationStr ? `<span class="stat-badge">⏱️ ${esc(durationStr)}</span>` : ""}
            ${totalTokens ? `<span class="stat-badge tokens">🔤 ${esc(totalTokens)} tokens</span>` : ""}
            <span class="stat-badge cost" title="Turn Cost (Exact: ${esc(exactCostStr)})">💵 ${esc(costStr)}</span>
            <span class="stat-badge">${steps.length} event${steps.length === 1 ? "" : "s"}</span>
          </div>
        </div>
        <div class="header-actions">
          <button id="btn-close-win" class="btn-close" title="Close Window (Esc)">Close</button>
        </div>
      </header>

      <div id="steps-container">
        ${stepsHtml}
      </div>
    `;

    // Event listeners
    document.getElementById("btn-close-win")?.addEventListener("click", () => {
      window.close();
    });

    // Per-card collapsible header click
    document.querySelectorAll(".trace-card-header").forEach(headerEl => {
      headerEl.addEventListener("click", () => {
        const card = headerEl.closest(".trace-card");
        if (card) {
          const isExpanded = card.classList.contains("expanded");
          if (isExpanded) {
            card.classList.remove("expanded");
            card.classList.add("collapsed");
          } else {
            card.classList.add("expanded");
            card.classList.remove("collapsed");
          }
        }
      });
    });
  }

  // Window-level escape key
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      window.close();
    }
  });

  function applyTheme() {
    let appliedTheme = null;

    // 1. Check explicit metadata theme passed from sidebar.js
    try {
      if (currentTraceData?.metadata?.theme) {
        appliedTheme = currentTraceData.metadata.theme;
      }
      if (!appliedTheme) {
        const data = resolveTraceData();
        if (data?.metadata?.theme) {
          appliedTheme = data.metadata.theme;
        }
      }
    } catch (e) {}

    // 2. Check window.opener document theme
    if (!appliedTheme) {
      try {
        if (window.opener?.document?.body?.getAttribute("data-theme")) {
          appliedTheme = window.opener.document.body.getAttribute("data-theme");
        }
      } catch (e) {}
    }

    // 3. Check Zotero preferences
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

    // 4. Fallback to system prefers-color-scheme
    if (!appliedTheme) {
      const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches;
      appliedTheme = prefersDark ? "zotero-dark" : "zotero-light";
    }

    (document.body || document.documentElement)?.setAttribute("data-theme", appliedTheme);
  }

  // Window reuse and external updates
  window.updateTraceData = function(data) {
    if (!data) return;
    currentTraceData = data;
    try {
      if (window.arguments) {
        window.arguments[0] = data;
      }
    } catch (e) {}
    applyTheme();
    renderTrace(data);
  };

  // Re-check on focus if parent pushed newer trace
  window.addEventListener("focus", () => {
    applyTheme();
    try {
      const data = resolveTraceData(true);
      if (data && data !== currentTraceData && data.steps && (data.steps !== currentTraceData?.steps || data.steps.length !== currentTraceData?.steps?.length)) {
        try {
          if (window.arguments) {
            window.arguments[0] = data;
          }
        } catch (e) {}
        renderTrace(data);
      }
    } catch (e) {}
  });

  window.matchMedia?.("(prefers-color-scheme: dark)")?.addEventListener("change", () => {
    applyTheme();
  });

  // Initial render
  function init() {
    applyTheme();
    const data = resolveTraceData();
    renderTrace(data);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

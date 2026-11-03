const THEME_PALETTES = {
  slate: { bgCard: "#1e293b", bgDark: "#0f172a", border: "#334155", textMain: "#f8fafc", textMuted: "#94a3b8", primary: "#6366f1", chipBg: "rgba(255, 255, 255, 0.06)" },
  amoled: { bgCard: "#0d0d0d", bgDark: "#000000", border: "#27272a", textMain: "#ffffff", textMuted: "#a1a1aa", primary: "#818cf8", chipBg: "rgba(255, 255, 255, 0.08)" },
  "zotero-dark": { bgCard: "#2d2d2d", bgDark: "#1e1e1e", border: "#3d3d3d", textMain: "#e3e3e3", textMuted: "#aaaaaa", primary: "#007acc", chipBg: "rgba(255, 255, 255, 0.06)" },
  "zotero-light": { bgCard: "#ffffff", bgDark: "#f9f9fb", border: "#e0e0e0", textMain: "#222222", textMuted: "#666666", primary: "#007acc", chipBg: "rgba(0, 0, 0, 0.05)" },
  sepia: { bgCard: "#fdfbf7", bgDark: "#f4ecd8", border: "#e4d5b7", textMain: "#433422", textMuted: "#8c7050", primary: "#c05c36", chipBg: "rgba(192, 92, 54, 0.08)" }
};

function getActiveThemePalette(targetDoc) {
  let theme = null;
  if (targetDoc && targetDoc.body && typeof targetDoc.body.getAttribute === "function") {
    theme = targetDoc.body.getAttribute("data-theme");
  }

  // If explicit theme attribute was found on body, normalize and return
  if (theme) {
    if (theme === "light") theme = "zotero-light";
    else if (theme === "dark") theme = "zotero-dark";
    if (THEME_PALETTES[theme]) {
      return { theme, colors: THEME_PALETTES[theme] };
    }
    return { theme: "slate", colors: THEME_PALETTES.slate };
  }

  // If no theme attribute on body, check Zotero.Prefs / system
  try {
    const zot = (typeof Zotero !== "undefined" ? Zotero : null) || (typeof window !== "undefined" && window.Zotero);
    if (zot && zot.Prefs) {
      let pref = zot.Prefs.get("zocrates.theme");
      if (pref && pref !== "auto" && THEME_PALETTES[pref]) {
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

  if (!theme || !THEME_PALETTES[theme]) {
    theme = "slate";
  }
  return { theme, colors: THEME_PALETTES[theme] };
}

var InfoModal = {
  THEME_PALETTES,
  getActiveThemePalette,

  show(options = {}) {
    let {
      window = null,
      title = "",
      subtitle = "",
      rows = [],
      buttons = null,
      wide = false,
      maxWidth = null,
      onClose = null
    } = options;

    let targetDoc = (window && window.document) ||
                    (typeof Zotero !== "undefined" && Zotero.getMainWindow && Zotero.getMainWindow().document) ||
                    (typeof document !== "undefined" ? document : null);
    if (!targetDoc) return;

    const XHTMLNS = "http://www.w3.org/1999/xhtml";

    let existing = targetDoc.getElementById("zocrates-info-modal-overlay");
    if (existing) {
      if (typeof existing._closeModal === "function") {
        existing._closeModal();
      } else {
        existing.remove();
      }
    }

    let overlay = targetDoc.createElementNS(XHTMLNS, "div");
    overlay.id = "zocrates-info-modal-overlay";
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

    const resolvedWidth = maxWidth ? String(maxWidth) : (wide ? "680px" : "440px");
    const resolvedMaxViewport = (wide || maxWidth) ? "92vw" : "90vw";
    let card = targetDoc.createElementNS(XHTMLNS, "div");
    card.className = "zocrates-info-modal-card";
    card.style.cssText = `background: var(--bg-card, #1e293b); color: var(--text-main, #f1f5f9); border: 1px solid var(--border, rgba(255, 255, 255, 0.15)); border-radius: 10px; width: ${resolvedWidth}; max-width: ${resolvedMaxViewport}; max-height: 85vh; display: flex; flex-direction: column; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; box-sizing: border-box; overflow: hidden;`;

    // Header
    let header = targetDoc.createElementNS(XHTMLNS, "div");
    header.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; padding: 12px 16px; border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.1)); background: var(--chip-bg, rgba(255, 255, 255, 0.03));";

    let headerTextContainer = targetDoc.createElementNS(XHTMLNS, "div");
    headerTextContainer.style.cssText = "display: flex; flex-direction: column; gap: 2px;";

    let titleEl = targetDoc.createElementNS(XHTMLNS, "div");
    titleEl.style.cssText = "font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px;";
    titleEl.textContent = title;
    headerTextContainer.appendChild(titleEl);

    if (subtitle) {
      let subEl = targetDoc.createElementNS(XHTMLNS, "div");
      subEl.style.cssText = "font-size: 11.5px; color: var(--text-muted, #94a3b8);";
      subEl.textContent = subtitle;
      headerTextContainer.appendChild(subEl);
    }

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

    let isClosed = false;
    let closeModal = () => {
      if (isClosed) return;
      isClosed = true;
      overlay.remove();
      targetDoc.removeEventListener("keydown", keyHandler);
      if (typeof onClose === "function") {
        try {
          onClose();
        } catch (_) {}
      }
    };
    overlay._closeModal = closeModal;
    targetDoc.addEventListener("keydown", keyHandler);

    closeBtn.addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeModal();
    });

    header.appendChild(closeBtn);
    card.appendChild(header);

    // Body
    let modalBody = targetDoc.createElementNS(XHTMLNS, "div");
    modalBody.style.cssText = "padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box; line-height: 1.6;";

    for (let row of rows) {
      if (!row) continue;
      if (typeof row === "object" && row.nodeType) {
        modalBody.appendChild(row);
      } else if (typeof row === "string") {
        let rowEl = targetDoc.createElementNS(XHTMLNS, "div");
        rowEl.style.cssText = "word-break: break-word;";
        rowEl.textContent = row;
        modalBody.appendChild(rowEl);
      } else if (typeof row === "object") {
        let rowEl = targetDoc.createElementNS(XHTMLNS, "div");
        rowEl.style.cssText = "word-break: break-word;";

        let bullet = targetDoc.createTextNode("• ");
        rowEl.appendChild(bullet);

        if (row.label) {
          let bold = targetDoc.createElementNS(XHTMLNS, "b");
          bold.style.cssText = "color: var(--text-main, #f1f5f9);";
          let labelText = row.label.endsWith(":") ? row.label + " " : row.label + ": ";
          bold.textContent = labelText;
          rowEl.appendChild(bold);
        }

        let valSpan = targetDoc.createElementNS(XHTMLNS, "span");
        if (row.valueStyle) {
          valSpan.style.cssText = row.valueStyle;
        } else {
          valSpan.style.cssText = "color: var(--text-muted, #94a3b8);";
        }
        valSpan.textContent = (row.value !== undefined && row.value !== null) ? String(row.value) : "";
        rowEl.appendChild(valSpan);

        modalBody.appendChild(rowEl);
      }
    }

    card.appendChild(modalBody);

    // Footer
    let footer = targetDoc.createElementNS(XHTMLNS, "div");
    footer.style.cssText = "display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border, rgba(255, 255, 255, 0.1)); background: var(--chip-bg, rgba(255, 255, 255, 0.02));";

    if (buttons && Array.isArray(buttons) && buttons.length > 0) {
      for (let btnConfig of buttons) {
        if (!btnConfig) continue;
        let btn = targetDoc.createElementNS(XHTMLNS, "button");
        btn.textContent = btnConfig.label || "OK";
        if (btnConfig.className) {
          btn.className = btnConfig.className;
        }

        let baseStyle = "padding: 6px 14px; font-size: 12px; font-weight: 500; cursor: pointer; border-radius: 5px; border: 1px solid var(--border, rgba(255, 255, 255, 0.2)); background: transparent; color: inherit; transition: opacity 0.15s;";
        if (btnConfig.primary) {
          baseStyle = "padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 5px; border: 1px solid var(--primary, #2563eb); background: var(--primary, #2563eb); color: #ffffff; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2); transition: opacity 0.15s;";
        }
        if (btnConfig.style) {
          baseStyle += " " + btnConfig.style;
        }
        btn.style.cssText = baseStyle;

        btn.addEventListener("click", async (e) => {
          let shouldClose = !btnConfig.keepOpen;
          if (typeof btnConfig.onClick === "function") {
            try {
              let res = await btnConfig.onClick(e, btn);
              if (res === false) {
                shouldClose = false;
              }
            } catch (err) {
              if (typeof Zotero !== "undefined" && Zotero.debug) {
                Zotero.debug("Zocrates: Error in modal button onClick handler: " + err);
              }
            }
          }
          if (shouldClose) {
            closeModal();
          }
        });

        footer.appendChild(btn);
      }
    } else {
      let footerCloseBtn = targetDoc.createElementNS(XHTMLNS, "button");
      footerCloseBtn.textContent = "Close";
      footerCloseBtn.style.cssText = "padding: 6px 14px; font-size: 12px; font-weight: 500; cursor: pointer; border-radius: 5px; border: 1px solid var(--border, rgba(255, 255, 255, 0.2)); background: transparent; color: inherit;";
      footerCloseBtn.addEventListener("click", closeModal);
      footer.appendChild(footerCloseBtn);
    }

    card.appendChild(footer);
    overlay.appendChild(card);

    let container = targetDoc.body || targetDoc.documentElement;
    container.appendChild(overlay);
    return overlay;
  }
};

if (typeof Zotero !== "undefined") {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  Zotero.Zocrates.InfoModal = InfoModal;
}
if (typeof window !== "undefined") {
  window.InfoModal = InfoModal;
}
if (typeof globalThis !== "undefined") {
  globalThis.InfoModal = InfoModal;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = InfoModal;
}

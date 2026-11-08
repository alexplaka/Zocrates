// Zocrates Sidebar Script
// Runs inside an iframe embedded in the Zotero main window.
// Accesses Zotero APIs via the parent chrome window.

try {

  // Access the Zotero global from the parent chrome window.
  let Zotero = window.Zotero
    || window.parent?.Zotero
    || window.top?.Zotero
    || window.opener?.Zotero
    || window.opener?.parent?.Zotero
    || window.opener?.top?.Zotero;

  if (!Zotero && typeof Components !== "undefined" && Components?.classes) {
    try {
      let wm = Components.classes["@mozilla.org/appshell/window-mediator;1"]
        .getService(Components.interfaces.nsIWindowMediator);
      let mainWin = wm.getMostRecentWindow("navigator:browser");
      Zotero = mainWin?.Zotero;
    } catch(e) {
      console.warn("Zocrates: Failed nsIWindowMediator resolution:", e);
    }
  }

  if (Zotero) {
    Zotero.debug("Zocrates: Sidebar JS connected to Zotero scope successfully");
  } else {
    console.error("Zocrates: Zotero global not found in window or parents");
  }

  // Single source of truth for backend URL resolution, installed by bootstrap.js.
  // Defensive fallback so nothing breaks if this script loads before bootstrap
  // startup completes.
  const BACKEND_URL = Zotero?.Zocrates?.getBackendUrl?.() || "http://127.0.0.1:8765";
  const API_BASE = BACKEND_URL;

  function getAuthToken() {
    try {
      const zot = (typeof resolveZotero === "function" ? resolveZotero() : null) || Zotero;
      return zot?.Zocrates?.getAuthToken?.() || zot?.Zocrates?._authToken || "";
    } catch (e) {
      return "";
    }
  }

  function getAuthHeaders(extraHeaders = {}) {
    try {
      const zot = (typeof resolveZotero === "function" ? resolveZotero() : null) || Zotero;
      if (zot?.Zocrates?.getAuthHeaders) {
        return zot.Zocrates.getAuthHeaders(extraHeaders);
      }
      const token = getAuthToken();
      if (token) {
        return { ...extraHeaders, Authorization: "Bearer " + token };
      }
    } catch (e) {}
    return { ...extraHeaders };
  }

  let rootURI = Zotero?.Zocrates?.rootURI || null;

  let isDetached = (window.parent === window || !window.parent?.document);

  function resolveActiveSessionId() {
    try {
      if (window.arguments && window.arguments[0] && window.arguments[0].initialSessionId) {
        const argSid = window.arguments[0].initialSessionId;
        if (argSid && !argSid.startsWith("session_")) return argSid;
      }
    } catch (e) {}

    try {
      const zot = resolveZotero();
      if (isDetached && zot?.Zocrates?._detachedInitialState?.currentSessionId) {
        const detachedSid = zot.Zocrates._detachedInitialState.currentSessionId;
        if (detachedSid && !detachedSid.startsWith("session_")) return detachedSid;
      }
      if (zot?.Zocrates?._activeSessionId && !zot.Zocrates._activeSessionId.startsWith("session_")) {
        return zot.Zocrates._activeSessionId;
      }
      const savedSid = zot?.Prefs?.get?.("extensions.zocrates.last_active_session_id", true);
      if (savedSid && typeof savedSid === "string" && !savedSid.startsWith("session_")) return savedSid;
    } catch (e) {}

    return "";
  }

  function setActiveSessionId(sid) {
    currentSessionId = sid || "";
    try {
      if (typeof selectedCandidateKeys !== "undefined") selectedCandidateKeys.clear();
      if (typeof importedCandidateKeys !== "undefined") importedCandidateKeys.clear();
    } catch (e) {}
    try {
      const zot = resolveZotero();
      if (zot?.Zocrates) {
        zot.Zocrates._activeSessionId = currentSessionId;
        if (zot.Zocrates._detachedInitialState) {
          zot.Zocrates._detachedInitialState.currentSessionId = currentSessionId;
        }
      }
      if (zot?.Prefs && currentSessionId && !currentSessionId.startsWith("session_")) {
        zot.Prefs.set("extensions.zocrates.last_active_session_id", currentSessionId, true);
      }
    } catch (e) {}
  }

  let currentSessionId = resolveActiveSessionId();
  let sessions = []; // Array of { id, title, messages }
  let _loadSessionsPromise = null; // Re-entrancy guard against concurrent session fetches
  let activeItemsList = []; // Holds array of { key, title, creators, year }
  let activeSnippetsList = []; // Holds array of snippet objects from ReaderContext
  let lastActiveKeysStr = "";
  let selectionListenerAttached = false;
  let contextMode = "papers"; // "papers", "collection", or "library"
  let isStickyMode = false;
  let expandedColIds = new Set(); // Tracks expanded collection IDs in the context menu tree

// Chat submission gate: false while the onboarding bootstrap progress card is
// active (backend provisioning / starting). Flips true exactly once in
// removeBootstrapProgressCard() — the single "backend usable now" choke point
// invoked from BOTH the bootstrap done event and the /status-backed
// checkBackendStatus success path. handleChatSubmit refuses to POST while it
// is false, so click and Ctrl/Cmd+Enter submits are inert during install.
let backendReadyForChat = false;

// UI Elements
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const dropdownStatusItem = document.getElementById("dropdown-status-item") || (statusText ? statusText.closest(".dropdown-status-item") : null);
const registrationPanel = document.getElementById("registration-panel");
const registerBtn = document.getElementById("register-btn");
const syncBtn = document.getElementById("sync-btn");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const chatMessages = document.getElementById("chat-messages");
const chipsContainer = document.getElementById("chips-container");
const clearAllChipsBtn = document.getElementById("clear-all-chips-btn");
const toggleStickyBtn = document.getElementById("toggle-sticky-btn");
const modelNameDisplay = document.getElementById("model-name-display");
let currentLlmModel = "google/gemini-3.8-flash";
const addContextBtn = document.getElementById("add-context-btn");
const contextOptionsDropdown = document.getElementById("context-options-dropdown");
const addCurrentViewOption = document.getElementById("add-current-view-option");
const addCurrentViewIcon = document.getElementById("add-current-view-icon");
const addCurrentViewText = document.getElementById("add-current-view-text");
const currentViewSeparator = document.getElementById("current-view-separator");
const addSelectedPapersOption = document.getElementById("add-selected-papers-option");
const addCollectionOption = document.getElementById("add-collection-option");
const collectionsSubmenu = document.getElementById("collections-submenu");
const addLibraryOption = document.getElementById("add-library-option");
let currentResolvedView = null; // { type: "collection", collections: [...] } or { type: "library", libInfo: { libraryID, name } }
const themeMenuOption = document.getElementById("theme-menu-option");
const themeMenuContainer = document.getElementById("theme-menu-container");
const themeOptionsDropdown = document.getElementById("theme-options-dropdown");
const fontSizeMenuOption = document.getElementById("font-size-menu-option");
const fontSizeMenuContainer = document.getElementById("font-size-menu-container");
const fontSizeOptionsDropdown = document.getElementById("font-size-options-dropdown");
const aboutMenuContainer = document.getElementById("about-menu-container");
const aboutMenuOption = document.getElementById("about-menu-option");
const aboutOptionsDropdown = document.getElementById("about-options-dropdown");
const aboutHardwareBtn = document.getElementById("about-hardware-btn");
const aboutAdvancedSettingsBtn = document.getElementById("about-advanced-settings-btn");
const aboutInfoBtn = document.getElementById("about-info-btn");
const aboutReportIssueBtn = document.getElementById("about-report-issue-btn");

const AGENT_DISPLAY_NAMES = {
  "coordinator_agent": "Zocrates",
  "coordinator": "Zocrates",
  "library_search_agent": "Library Search",
  "library_search": "Library Search",
  "library_synthesizer_agent": "Library Synthesis",
  "library_synthesizer": "Library Synthesis",
  "online_search_agent": "Online Discovery",
  "online_search": "Online Discovery",
  "online_synthesizer_agent": "Discovery Synthesis",
  "online_synthesizer": "Discovery Synthesis"
};

function isInternalWorkflowNode(author) {
  if (!author) return false;
  const a = author.toLowerCase();
  return a.endsWith("_node") || a.endsWith("_router") || a.endsWith("_checkpoint") || a.startsWith("checkpoint_") || a.includes("checkpoint") || a === "system";
}

function isIntermediateSearchStep(author) {
  if (!author) return false;
  const a = author.toLowerCase();
  return a.endsWith("_search_agent") || a === "library_search_agent";
}

// Footer Session Stats Elements
const sessionStatsBtn = document.getElementById("session-stats-btn");
const sessionStatsPopover = document.getElementById("session-stats-popover");
const statsTurnsCount = document.getElementById("stats-turns-count");
const statsCostVal = document.getElementById("stats-cost-val");
const statsTokensVal = document.getElementById("stats-tokens-val");
const statsPromptTokens = document.getElementById("stats-prompt-tokens");
const statsCandidatesTokens = document.getElementById("stats-candidates-tokens");
const statsCachedTokens = document.getElementById("stats-cached-tokens");
const statsCachedTokensItem = document.getElementById("stats-cached-tokens-item");
const statsBreakdownRow = document.getElementById("stats-breakdown-row");

let currentSessionCostUsd = 0.0;
let currentSessionTotalTokens = 0;
let currentSessionTurns = 0;
let currentSessionTokenBreakdown = { prompt: 0, candidates: 0, cached: 0 };

function formatCostUsd(cost) {
  if (cost === null || cost === undefined || isNaN(cost)) return "$0.00";
  const num = Number(cost);
  if (num <= 0) return "$0.00";
  if (num < 0.005) return "<$0.01";
  return "$" + num.toFixed(2);
}

function updateSessionStatsUI() {
  const roundedCost = formatCostUsd(currentSessionCostUsd);
  const exactCost = "$" + (currentSessionCostUsd || 0).toFixed(6);
  const turnsText = `${currentSessionTurns} turn${currentSessionTurns === 1 ? "" : "s"}`;
  const tokensText = Number(currentSessionTotalTokens || 0).toLocaleString();

  if (statsTurnsCount) statsTurnsCount.textContent = turnsText;
  if (statsCostVal) {
    statsCostVal.textContent = roundedCost;
    statsCostVal.title = `Exact: ${exactCost}`;
  }
  if (statsTokensVal) {
    statsTokensVal.textContent = tokensText;
    statsTokensVal.title = `${currentSessionTotalTokens} tokens`;
  }
  if (sessionStatsBtn) {
    sessionStatsBtn.title = `Session Usage: ${roundedCost} · ${tokensText} tokens · ${turnsText}`;
  }

  if (currentSessionTotalTokens > 0 && statsBreakdownRow) {
    statsBreakdownRow.style.display = "flex";
    if (statsPromptTokens) statsPromptTokens.textContent = `Input: ${Number(currentSessionTokenBreakdown.prompt || 0).toLocaleString()}`;
    if (statsCandidatesTokens) statsCandidatesTokens.textContent = `Output: ${Number(currentSessionTokenBreakdown.candidates || 0).toLocaleString()}`;
    if (currentSessionTokenBreakdown.cached > 0) {
      if (statsCachedTokensItem) statsCachedTokensItem.style.display = "block";
      if (statsCachedTokens) statsCachedTokens.textContent = `Cached: ${Number(currentSessionTokenBreakdown.cached || 0).toLocaleString()}`;
    } else if (statsCachedTokensItem) {
      statsCachedTokensItem.style.display = "none";
    }
  } else if (statsBreakdownRow) {
    statsBreakdownRow.style.display = "none";
  }
}

function computeTurnTokens(streamUsage) {
  const pTokens = Number(streamUsage?.prompt_token_count || streamUsage?.prompt_tokens || 0);
  const cTokens = Number(streamUsage?.candidates_token_count || streamUsage?.candidates_tokens || 0);
  let turnTokens = Number(streamUsage?.total_token_count || streamUsage?.total_tokens || 0);
  if (!turnTokens && (pTokens || cTokens)) {
    turnTokens = pTokens + cTokens;
  }
  return turnTokens;
}

function calculateSessionStats(sessionData) {
  const sessionObj = sessionData?.session || sessionData || {};
  const messages = Array.isArray(sessionObj.messages) ? sessionObj.messages : [];

  const backendTotalTokens = Number(sessionObj.total_tokens || sessionObj.session_metadata?.total_tokens || 0);
  const backendCost = Number(sessionObj.total_cost_usd || sessionObj.session_metadata?.total_cost_usd || 0.0);
  const backendTurns = Number(sessionObj.total_turns || sessionObj.session_metadata?.total_turns || 0);

  let turns = 0;
  let promptTokens = 0;
  let candTokens = 0;
  let cachedTokens = 0;
  let totalTokensFromMessages = 0;
  let costFromMessages = 0;

  messages.forEach(m => {
    const role = m.role || (m.sender === "user" ? "user" : "assistant");
    if (role === "assistant" && m.text && !m.text.startsWith("Hello! I am Zocrates")) {
      turns++;
    }
    if (m.usage) {
      const p = Number(m.usage.prompt_tokens || m.usage.prompt_token_count || 0);
      const c = Number(m.usage.candidates_tokens || m.usage.candidates_token_count || 0);
      const ca = Number(m.usage.cached_tokens || m.usage.cached_content_token_count || 0);
      let msgTokens = Number(m.usage.total_tokens || m.usage.total_token_count || 0);
      if (!msgTokens && (p || c)) {
        msgTokens = p + c;
      }
      promptTokens += p;
      candTokens += c;
      cachedTokens += ca;
      totalTokensFromMessages += msgTokens;
      costFromMessages += Number(m.turn_cost_usd || m.usage.cost_usd || 0.0);
    } else if (m.turn_cost_usd) {
      costFromMessages += Number(m.turn_cost_usd || 0.0);
    }
  });

  const totalTokens = backendTotalTokens > 0 ? backendTotalTokens : totalTokensFromMessages;
  const costUsd = backendCost > 0 ? backendCost : costFromMessages;
  const finalTurns = backendTurns > 0 ? backendTurns : turns;

  return {
    turns: finalTurns,
    totalTokens: totalTokens,
    costUsd: costUsd,
    totalTokensFromMessages,
    costFromMessages,
    turnsFromMessages: turns,
    tokenBreakdown: {
      prompt: promptTokens,
      candidates: candTokens,
      cached: cachedTokens,
    },
  };
}

// Sessions Drawer Elements
const menuToggleBtn = document.getElementById("menu-toggle-btn");
const closeDrawerBtn = document.getElementById("close-drawer-btn");
const drawerOverlay = document.getElementById("drawer-overlay");
const sessionsDrawer = document.getElementById("sessions-drawer");
const sessionsList = document.getElementById("sessions-list");
const newChatBtn = document.getElementById("new-chat-btn");
const sessionsSearchInput = document.getElementById("sessions-search-input");

// More Actions Dropdown Elements
const moreActionsBtn = document.getElementById("more-actions-btn");
const moreActionsDropdown = document.getElementById("more-actions-dropdown");

// Window Toggle Elements
const windowToggleBtn = document.getElementById("window-toggle-btn");
const detachIcon = document.getElementById("detach-icon");
const attachIcon = document.getElementById("attach-icon");

// Notifications Drawer Elements
const notificationToggleBtn = document.getElementById("notification-toggle-btn");
const notificationBadge = document.getElementById("notification-badge");
const notificationsDrawer = document.getElementById("notifications-drawer");
const closeNotificationsBtn = document.getElementById("close-notifications-btn");
const notificationsList = document.getElementById("notifications-list");
const clearNotificationsBtn = document.getElementById("clear-notifications-btn");

// Settings Gear Button Element
const settingsBtn = document.getElementById("settings-btn");

// Helper to open Zocrates Preferences Pane directly in Zotero settings
function openZocratesPreferences() {
  try {
    const zotero = resolveZotero();
    if (zotero) {
      if (typeof zotero.openPreferences === "function") {
        zotero.openPreferences("zocrates-preferences");
        return;
      }
      if (zotero.Utilities && zotero.Utilities.Internal && typeof zotero.Utilities.Internal.openPreferences === "function") {
        zotero.Utilities.Internal.openPreferences("zocrates-preferences");
        return;
      }
    }
    const win = (window.parent && window.parent.Services) ? window.parent : 
                (window.top && window.top.Services ? window.top : 
                (window.opener && window.opener.Services ? window.opener : (typeof Services !== "undefined" ? Services : null)));
    if (win && win.Services && win.Services.wm) {
      const mainWindow = win.Services.wm.getMostRecentWindow("navigator:browser") || win.Services.wm.getMostRecentWindow("zotero:main") || win.Services.wm.getMostRecentWindow(null);
      if (mainWindow) {
        if (mainWindow.Zotero && typeof mainWindow.Zotero.openPreferences === "function") {
          mainWindow.Zotero.openPreferences("zocrates-preferences");
          return;
        }
        if (typeof mainWindow.openPreferences === "function") {
          mainWindow.openPreferences("zocrates-preferences");
          return;
        }
        if (typeof mainWindow.openDialog === "function") {
          mainWindow.openDialog("chrome://zotero/content/preferences/preferences.xul", "zotero-preferences", "chrome,titlebar,toolbar,centerscreen", { pane: "zocrates-preferences" });
          return;
        }
      }
    }
    if (typeof window.openDialog === "function") {
      window.openDialog("chrome://zotero/content/preferences/preferences.xul", "zotero-preferences", "chrome,titlebar,toolbar,centerscreen", { pane: "zocrates-preferences" });
    }
  } catch (e) {
    console.error("Zocrates: Error opening preferences window:", e);
  }
}

if (settingsBtn) {
  settingsBtn.addEventListener("click", () => {
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    openZocratesPreferences();
  });
}

/**
 * Escapes HTML special characters to prevent XSS when inserting into DOM.
 * Use this for all user-generated or LLM-generated plain text.
 */
function escapeHTML(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Rewrites zocrates-img:// URIs to the local backend asset endpoint.
 *
 * Image Markdown emitted by the Synthesizer uses the custom scheme
 * `zocrates-img://{item_key}/images/{filename}`.  The Gecko renderer does
 * not understand this scheme, so we rewrite it to
 * `http://127.0.0.1:8765/assets/{item_key}/images/{filename}` before
 * injecting it into an <img> src attribute.
 *
 * Any other URI (http://, data:, etc.) is returned unchanged.
 */
function resolveAssetUrl(href) {
  if (typeof href === "string") {
    const token = getAuthToken();
    const qs = token ? "?token=" + encodeURIComponent(token) : "";
    if (href.startsWith("zocrates-img://")) {
      return BACKEND_URL + "/assets/" + href.slice("zocrates-img://".length) + qs;
    }
    let cleanHref = href.trim();
    if (cleanHref.startsWith("./")) {
      cleanHref = cleanHref.slice(2);
    }
    if (cleanHref.startsWith("images/")) {
      const activePaper = (typeof activeItemsList !== "undefined" && Array.isArray(activeItemsList))
        ? activeItemsList.find(p => p.key && p.type !== "collection" && p.type !== "library")
        : null;
      if (activePaper && activePaper.key) {
        return BACKEND_URL + "/assets/" + activePaper.key + "/" + cleanHref + qs;
      }
    }
  }
  return href;
}


/**
 * Allowlists for DOM sanitization to prevent Gecko SystemPrincipal privilege escalation
 * via Stored XSS while preserving legitimate markdown formatting, KaTeX MathML, and SVG.
 */
const DANGEROUS_TAGS = new Set([
  "script", "iframe", "object", "embed", "applet",
  "form", "input", "textarea", "select", "button",
  "meta", "link", "style", "base", "template", "portal",
  "frame", "frameset", "noscript"
]);

const ALLOWED_TAGS = new Set([
  // Text formatting
  "p", "span", "b", "i", "strong", "em", "u", "s", "strike", "del", "ins",
  "mark", "small", "sub", "sup", "abbr", "q", "cite", "dfn", "time",
  "kbd", "samp", "var", "br", "hr", "wbr",
  // Headings
  "h1", "h2", "h3", "h4", "h5", "h6",
  // Structural / block containers
  "div", "blockquote", "pre", "code", "section", "article", "header", "footer", "nav", "aside", "main",
  // Lists
  "ul", "ol", "li", "dl", "dt", "dd",
  // Tables
  "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
  // Links & images
  "a", "img",
  // Interactive / details & task lists
  "details", "summary", "input",
  // Figures & synthetic captions
  "figure", "figcaption", "synthetic_caption",
  // KaTeX MathML elements
  "math", "mrow", "mi", "mo", "mn", "msup", "msub", "msubsup", "mfrac",
  "mover", "munder", "munderover", "mtable", "mtr", "mtd", "mspace",
  "mtext", "annotation", "semantics", "mpadded", "mphantom", "menclose",
  "mstyle", "merror", "msqrt", "mroot", "mmultiscripts", "mprescripts", "none",
  // Inline SVG elements
  "svg", "path", "line", "rect", "circle", "ellipse", "polyline", "polygon",
  "g", "use", "defs", "marker", "text", "tspan"
]);

const GLOBAL_ALLOWED_ATTRS = new Set([
  "class", "style", "title", "dir", "lang", "tabindex"
]);

const TAG_ALLOWED_ATTRS = {
  a: new Set(["href", "target", "rel", "download"]),
  img: new Set(["src", "alt", "title", "loading", "width", "height"]),
  input: new Set(["type", "checked", "disabled"]),
  details: new Set(["open"]),
  th: new Set(["colspan", "rowspan", "headers", "scope", "align", "valign"]),
  td: new Set(["colspan", "rowspan", "headers", "scope", "align", "valign"]),
  ol: new Set(["start", "reversed", "type"]),
  li: new Set(["value"]),
  figure: new Set(["data-type"]),
  table: new Set(["border", "cellspacing", "cellpadding"]),
  // MathML attributes
  math: new Set(["display", "xmlns", "mathvariant", "mathsize", "mathcolor", "mathbackground"]),
  annotation: new Set(["encoding"]),
  mi: new Set(["mathvariant", "mathcolor", "mathbackground", "mathsize"]),
  mo: new Set(["fence", "separator", "stretchy", "symmetric", "largeop", "movablelimits", "form", "lspace", "rspace", "mathvariant", "mathcolor", "mathbackground"]),
  mn: new Set(["mathvariant", "mathcolor", "mathbackground", "mathsize"]),
  mtext: new Set(["mathvariant", "mathcolor", "mathbackground", "mathsize"]),
  mspace: new Set(["width", "height", "depth", "mathbackground"]),
  mfrac: new Set(["linethickness", "numalign", "denomalign", "bevelled", "mathbackground"]),
  menclose: new Set(["notation", "mathcolor", "mathbackground"]),
  mpadded: new Set(["width", "height", "depth", "lspace", "voffset", "mathbackground"]),
  mtable: new Set(["align", "rowalign", "columnalign", "rowlines", "columnlines", "frame", "framespacing", "equalrows", "equalcolumns", "displaystyle"]),
  mtr: new Set(["rowalign", "columnalign"]),
  mtd: new Set(["rowalign", "columnalign", "rowspan", "columnspan"]),
  mover: new Set(["accent", "align"]),
  munder: new Set(["accentunder", "align"]),
  munderover: new Set(["accent", "accentunder", "align"]),
  mstyle: new Set(["scriptlevel", "displaystyle", "mathcolor", "mathbackground"]),
  // SVG attributes (id allowed only on SVG elements for internal defs/marker references)
  svg: new Set(["id", "viewbox", "viewBox", "xmlns", "xmlns:xlink", "width", "height", "fill", "stroke", "preserveaspectratio", "preserveAspectRatio"]),
  defs: new Set(["id"]),
  path: new Set(["d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "fill-opacity", "stroke-opacity", "transform", "id"]),
  line: new Set(["x1", "y1", "x2", "y2", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "opacity", "stroke-opacity", "transform", "id"]),
  rect: new Set(["x", "y", "width", "height", "rx", "ry", "fill", "stroke", "stroke-width", "stroke-dasharray", "opacity", "fill-opacity", "stroke-opacity", "transform", "id"]),
  circle: new Set(["cx", "cy", "r", "fill", "stroke", "stroke-width", "stroke-dasharray", "opacity", "fill-opacity", "stroke-opacity", "transform", "id"]),
  ellipse: new Set(["cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width", "opacity", "transform", "id"]),
  polyline: new Set(["points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "transform", "id"]),
  polygon: new Set(["points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "transform", "id"]),
  g: new Set(["fill", "stroke", "stroke-width", "opacity", "transform", "id", "class"]),
  use: new Set(["href", "xlink:href", "x", "y", "width", "height", "transform", "id"]),
  marker: new Set(["id", "viewbox", "viewBox", "refx", "refX", "refy", "refY", "markerunits", "markerUnits", "markerwidth", "markerWidth", "markerheight", "markerHeight", "orient"]),
  text: new Set(["x", "y", "dx", "dy", "text-anchor", "font-family", "font-size", "font-weight", "fill", "transform", "id"]),
  tspan: new Set(["x", "y", "dx", "dy", "text-anchor", "font-family", "font-size", "font-weight", "fill", "id"])
};

/**
 * Validates whether a URL is safe for navigation links (<a href>).
 * Strictly restricts schemes to http:, https:, mailto:, and hash fragments.
 * Disallows data:, file:, javascript:, and relative file paths.
 */
function isSafeLinkUrl(url) {
  if (!url || typeof url !== "string") return false;
  const clean = url.trim().replace(/[\u0000-\u0020\u007F-\u009F]/g, "");
  if (!clean) return false;
  if (clean.startsWith("#")) return true;
  if (/^(?:javascript|vbscript|data|file|blob|chrome|resource|about):/i.test(clean)) {
    return false;
  }
  const colonIdx = clean.indexOf(":");
  if (colonIdx !== -1) {
    const scheme = clean.slice(0, colonIdx).toLowerCase();
    return scheme === "http" || scheme === "https" || scheme === "mailto";
  }
  return false;
}

/**
 * Validates whether a URL is safe against dangerous schemes (javascript:, vbscript:, etc.).
 * Permits http:, https:, mailto:, safe custom scheme zocrates-img:, safe image data URIs,
 * relative paths, and anchor fragments.
 */
function isSafeUrl(url) {
  if (!url || typeof url !== "string") return false;
  const clean = url.trim().replace(/[\u0000-\u0020\u007F-\u009F]/g, "");
  if (!clean) return false;

  if (/^(?:javascript|vbscript|data):/i.test(clean)) {
    if (/^data:image\/(?:png|jpeg|jpg|gif|webp|svg\+xml);base64,[a-z0-9+/=\s]+$/i.test(clean)) {
      return true;
    }
    return false;
  }

  if (clean.startsWith("#") || clean.startsWith("/") || clean.startsWith("./") || clean.startsWith("../") || clean.startsWith("images/")) {
    return true;
  }

  const colonIdx = clean.indexOf(":");
  if (colonIdx !== -1) {
    const scheme = clean.slice(0, colonIdx).toLowerCase();
    if (scheme === "http" || scheme === "https" || scheme === "mailto" || scheme === "zocrates-img") {
      return true;
    }
    return false;
  }

  return true;
}

/**
 * Robust, allowlist-based DOM sanitization using DOMParser in an inert text/html document.
 * Discards dangerous tags (script, iframe, form, etc.) with children.
 * Unwraps safe children of non-allowed tags.
 * Strips all on* event handlers and validates attributes, URLs, and styles.
 */
function sanitizeHTML(html) {
  if (!html || typeof html !== "string") return "";
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(`<!DOCTYPE html><html><body>${html}</body></html>`, "text/html");
    const body = doc.body;
    if (!body) return "";

    function sanitizeElement(node) {
      if (node.nodeType === 3) return; // Text node
      if (node.nodeType === 8) {
        node.remove(); // Strip HTML comments
        return;
      }
      if (node.nodeType !== 1) {
        node.remove();
        return;
      }

      const tagName = (node.tagName || node.nodeName || "").toLowerCase();

      // 1. Dangerous tag: discard completely with all children
      // Special exception: permit benign disabled task-list checkboxes: <input type="checkbox" disabled>
      if (tagName === "input") {
        const typeAttr = (node.getAttribute("type") || "").toLowerCase();
        const isDisabled = node.hasAttribute("disabled") || node.disabled;
        if (typeAttr !== "checkbox" || !isDisabled) {
          node.remove();
          return;
        }
      } else if (DANGEROUS_TAGS.has(tagName)) {
        node.remove();
        return;
      }

      // 2. Non-allowed non-dangerous tag: unwrap children so safe text/nodes are preserved
      if (!ALLOWED_TAGS.has(tagName)) {
        const parent = node.parentElement || node.parentNode;
        if (!parent) {
          node.remove();
          return;
        }
        const children = Array.from(node.childNodes || node.children || []);
        for (const child of children) {
          parent.insertBefore(child, node);
          sanitizeElement(child);
        }
        node.remove();
        return;
      }

      // 3. Allowed tag: sanitize attributes
      const attrNames = node.getAttributeNames ? node.getAttributeNames() : Object.keys(node.attributes || {});
      const allowedForTag = TAG_ALLOWED_ATTRS[tagName];

      for (const rawName of attrNames) {
        const lowerName = rawName.toLowerCase();

        // Strip any attribute starting with 'on'
        if (/^[oO][nN]/.test(lowerName)) {
          node.removeAttribute(rawName);
          continue;
        }

        const isDataAttr = /^data-[a-z0-9_\-]+$/i.test(lowerName);
        const isAriaAttr = /^aria-[a-z0-9_\-]+$/i.test(lowerName);
        const isGlobal = GLOBAL_ALLOWED_ATTRS.has(lowerName);
        const isTagSpecific = allowedForTag && allowedForTag.has(lowerName);

        if (!isDataAttr && !isAriaAttr && !isGlobal && !isTagSpecific) {
          node.removeAttribute(rawName);
          continue;
        }

        // Validate attribute values
        if (lowerName === "href" && tagName === "a") {
          const val = node.getAttribute(rawName);
          if (!isSafeLinkUrl(val)) {
            node.removeAttribute(rawName);
          } else if (node.getAttribute("target") === "_blank") {
            node.setAttribute("rel", "noopener noreferrer");
          }
        } else if (lowerName === "href" || lowerName === "src" || lowerName === "xlink:href") {
          const val = node.getAttribute(rawName);
          if (!isSafeUrl(val)) {
            node.removeAttribute(rawName);
          }
        } else if (lowerName === "style") {
          const val = node.getAttribute(rawName) || "";
          if (/expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding|behavior\s*:/i.test(val)) {
            node.removeAttribute(rawName);
          } else if (/url\s*\(/i.test(val)) {
            const urlMatches = val.match(/url\s*\(\s*['"]?([^'")]+)['"]?\s*\)/gi);
            if (urlMatches) {
              for (const m of urlMatches) {
                const innerUrl = m.replace(/^url\s*\(\s*['"]?|['"]?\s*\)$/gi, "");
                if (!isSafeUrl(innerUrl)) {
                  node.removeAttribute(rawName);
                  break;
                }
              }
            }
          }
        }
      }

      // 4. Recurse into children
      const children = Array.from(node.childNodes || node.children || []);
      for (const child of children) {
        sanitizeElement(child);
      }
    }

    const bodyChildren = Array.from(body.childNodes || body.children || []);
    for (const child of bodyChildren) {
      sanitizeElement(child);
    }

    return body.innerHTML;
  } catch (err) {
    console.error("Zocrates: sanitizeHTML failed:", err);
    return escapeHTML(html);
  }
}

/**
 * Defense-in-depth SVG sanitizer for Mermaid diagrams.
 * Parses SVG as image/svg+xml, strips <script> tags, on* event handlers,
 * javascript: links, and unsafe style expressions while preserving Mermaid
 * styling, <style> blocks, <defs>, <marker>, and path layout.
 */
function sanitizeMermaidSvg(svgCode) {
  if (!svgCode || typeof svgCode !== "string") return "";
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(svgCode, "image/svg+xml");
    const hasParserError = doc.getElementsByTagName ? doc.getElementsByTagName("parsererror").length > 0 : false;
    const root = hasParserError ? (parser.parseFromString(svgCode, "text/html").body) : (doc.documentElement || doc);
    if (!root) return "";

    const DANGEROUS_SVG_TAGS = new Set([
      "script", "iframe", "object", "embed", "applet", "form",
      "input", "button", "textarea", "select",
      "animate", "set", "animatetransform", "animatemotion", "handler", "listener"
    ]);

    function cleanSvgNode(node) {
      if (node.nodeType === 3) return; // Text
      if (node.nodeType === 8) {
        node.remove();
        return;
      }
      if (node.nodeType !== 1) {
        node.remove();
        return;
      }

      const tagName = (node.tagName || node.nodeName || "").toLowerCase();
      if (DANGEROUS_SVG_TAGS.has(tagName) || tagName.endsWith(":script")) {
        node.remove();
        return;
      }

      if (tagName === "style") {
        const text = node.textContent || "";
        if (/@import|javascript\s*:|vbscript\s*:|-moz-binding|expression\s*\(/i.test(text)) {
          node.remove();
          return;
        }
        if (/url\s*\(\s*['"]?(?!data:image\/|#)[^'")]+['"]?\s*\)/i.test(text)) {
          node.remove();
          return;
        }
      }

      const attrNames = node.getAttributeNames ? node.getAttributeNames() : Object.keys(node.attributes || {});
      for (const rawName of attrNames) {
        const lower = rawName.toLowerCase();
        if (/^[oO][nN]/.test(lower) || lower === "action" || lower === "formaction") {
          node.removeAttribute(rawName);
          continue;
        }
        if (lower === "href" || lower === "xlink:href" || lower === "src") {
          const val = node.getAttribute(rawName);
          if (val && !val.startsWith("#") && !isSafeUrl(val)) {
            node.removeAttribute(rawName);
          }
          continue;
        }
        if (lower === "to" || lower === "values") {
          const val = node.getAttribute(rawName) || "";
          if (/javascript\s*:|expression\s*\(/i.test(val)) {
            node.removeAttribute(rawName);
          }
          continue;
        }
        if (lower === "style") {
          const styleVal = node.getAttribute(rawName) || "";
          if (/expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding|behavior\s*:/i.test(styleVal)) {
            node.removeAttribute(rawName);
          }
        }
      }

      const children = Array.from(node.childNodes || node.children || []);
      for (const child of children) {
        cleanSvgNode(child);
      }
    }

    cleanSvgNode(root);

    const serializer = new XMLSerializer();
    let result = serializer.serializeToString(root);
    if (result.startsWith("<svg") && !result.includes("xmlns=")) {
      result = result.replace(/<svg\b/i, '<svg xmlns="http://www.w3.org/2000/svg"');
    }
    return result;
  } catch (err) {
    console.error("Zocrates: sanitizeMermaidSvg failed:", err);
    return "";
  }
}

// ---------------------------------------------------------------------------
// Stream control: AbortController for Stop button (H9)
// ---------------------------------------------------------------------------
let activeAbortController = null;
let isStreaming = false;

// ---------------------------------------------------------------------------
// Predictive next-prompt ghost text (suggested follow-up shown inside #chat-input)
// ---------------------------------------------------------------------------
let currentSuggestedPrompt = null;
let currentSuggestAbortController = null;

function setGhostSuggestion(prompt) {
  const overlay = document.getElementById("ghost-input-overlay");
  const textEl = document.getElementById("ghost-input-text");
  const wrapEl = overlay?.closest(".chat-input-wrap");
  const chatInputEl = document.getElementById("chat-input");

  if (!prompt || typeof prompt !== "string" || !prompt.trim()) {
    clearGhostSuggestion();
    return;
  }

  currentSuggestedPrompt = prompt.trim();
  if (textEl) {
    textEl.textContent = currentSuggestedPrompt;
    textEl.setAttribute("title", currentSuggestedPrompt);
  }

  if (overlay && chatInputEl && !chatInputEl.value.trim()) {
    overlay.style.display = "flex";
    if (wrapEl) wrapEl.classList.add("has-ghost");
  }
}

function clearGhostSuggestion() {
  if (currentSuggestAbortController) {
    currentSuggestAbortController.abort();
    currentSuggestAbortController = null;
  }
  currentSuggestedPrompt = null;
  const overlay = document.getElementById("ghost-input-overlay");
  const wrapEl = overlay?.closest(".chat-input-wrap");
  if (overlay) overlay.style.display = "none";
  if (wrapEl) wrapEl.classList.remove("has-ghost");
}

function updateGhostVisibility() {
  const overlay = document.getElementById("ghost-input-overlay");
  const wrapEl = overlay?.closest(".chat-input-wrap");
  const chatInputEl = document.getElementById("chat-input");
  if (!overlay || !chatInputEl) return;

  if (currentSuggestedPrompt && !chatInputEl.value.trim()) {
    overlay.style.display = "flex";
    if (wrapEl) wrapEl.classList.add("has-ghost");
  } else {
    overlay.style.display = "none";
    if (wrapEl) wrapEl.classList.remove("has-ghost");
  }
}

async function requestNextPromptSuggestion(sessionId, invocationId) {
  clearGhostSuggestion();
  if (!sessionId || !invocationId) return;

  const controller = new AbortController();
  currentSuggestAbortController = controller;
  try {
    const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/suggest_next_prompt`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ invocation_id: invocationId }),
      signal: controller.signal,
    });
    if (!res.ok) return;
    const data = await res.json();
    if (data.status === "success" && data.suggested_prompt) {
      const chatInputEl = document.getElementById("chat-input");
      if (currentSessionId === sessionId && chatInputEl && !chatInputEl.value.trim()) {
        setGhostSuggestion(data.suggested_prompt);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      console.debug("Zocrates: suggest_next_prompt skipped or failed:", e);
    }
  } finally {
    if (currentSuggestAbortController === controller) {
      currentSuggestAbortController = null;
    }
  }
}

// ---------------------------------------------------------------------------
// Backend Connection, Adaptive Single-Flight Heartbeat & Activity Gating
// ---------------------------------------------------------------------------
const BACKEND_CONNECTED_POLL_MS = 60000;
const BACKEND_BACKOFF_STEPS_MS = [3000, 5000, 10000, 30000];
const BACKEND_FOCUS_DEBOUNCE_MS = 2000;
const SELECTION_POLL_INTERVAL_MS = 400;

let _lastBackendOnline = null;
let _backendCheckInFlight = null;
let _backendPollTimer = null;
let _consecutiveBackendFailures = 0;
let _lastBackendCheckAt = 0;
let _backendPollingEnabled = false;
let _selectionPollTimer = null;
let _selectionPollingEnabled = false;
const _registeredActivityListeners = [];
const _registeredObsListeners = [];
let _sidebarVisibilityObserver = null;
let _activityListenersInstalled = false;

function resolveServices() {
  if (typeof Services !== "undefined" && Services) return Services;
  try {
    if (window.parent && window.parent.Services) return window.parent.Services;
    if (window.top && window.top.Services) return window.top.Services;
    if (window.opener && window.opener.Services) return window.opener.Services;
  } catch (e) {}
  return null;
}

function getDetachedWindow() {
  try {
    const zot = resolveZotero();
    if (zot?.Zocrates && typeof zot.Zocrates.getDetachedWindow === "function") {
      const win = zot.Zocrates.getDetachedWindow();
      return (win && !win.closed) ? win : null;
    }
  } catch (e) {}
  return null;
}

function getEmbeddedSidebarElement() {
  if (isDetached) return null;
  try {
    const hostDoc = (window.parent && window.parent !== window && window.parent.document)
      ? window.parent.document
      : ((window.top && window.top !== window && window.top.document) ? window.top.document : null);
    return hostDoc ? hostDoc.getElementById("zocrates-sidebar") : null;
  } catch (e) {
    return null;
  }
}

function isSidebarCollapsed() {
  if (isDetached) {
    return false;
  }
  const sidebarEl = getEmbeddedSidebarElement();
  return Boolean(sidebarEl && sidebarEl.hidden);
}

function hasZoteroActiveFocus() {
  try {
    if (typeof document !== "undefined" && document?.hidden === true) {
      return false;
    }
    const svc = resolveServices();
    const hasFocusService = Boolean(svc && svc.focus && "activeWindow" in svc.focus);
    if (hasFocusService) {
      if (svc.focus.activeWindow === null) {
        return false;
      }
      if (isDetached) {
        return true;
      }
    }
    const topWin = (window.top && window.top.document) ? window.top : null;
    if (topWin && topWin.document) {
      if (topWin.document.hidden === true) return false;
      if (typeof topWin.document.hasFocus === "function") {
        const topFocused = Boolean(topWin.document.hasFocus());
        const selfFocused = (typeof document !== "undefined" && typeof document?.hasFocus === "function")
          ? Boolean(document.hasFocus())
          : false;
        if (!topFocused && !selfFocused) {
          return false;
        }
      }
    } else if (typeof document !== "undefined" && document && typeof document.hasFocus === "function") {
      if (!(isDetached && hasFocusService) && !document.hasFocus()) {
        return false;
      }
    }
  } catch (e) {}
  return true;
}

function isPollingPaused() {
  if (!isDetached && getDetachedWindow()) {
    return true;
  }
  if (isSidebarCollapsed()) {
    return true;
  }
  if (!hasZoteroActiveFocus()) {
    return true;
  }
  return false;
}

function cancelBackendPollTimer() {
  if (_backendPollTimer !== null) {
    clearTimeout(_backendPollTimer);
    _backendPollTimer = null;
  }
}

function cancelSelectionPollTimer() {
  if (_selectionPollTimer !== null) {
    clearTimeout(_selectionPollTimer);
    _selectionPollTimer = null;
  }
}

function getNextBackendPollDelayMs() {
  if (_lastBackendOnline === true) {
    return BACKEND_CONNECTED_POLL_MS;
  }
  const idx = Math.min(Math.max(0, _consecutiveBackendFailures - 1), BACKEND_BACKOFF_STEPS_MS.length - 1);
  return BACKEND_BACKOFF_STEPS_MS[idx];
}

function scheduleNextBackendCheck() {
  cancelBackendPollTimer();
  if (!_backendPollingEnabled || isPollingPaused()) {
    return null;
  }
  const delayMs = getNextBackendPollDelayMs();
  _backendPollTimer = setTimeout(() => {
    _backendPollTimer = null;
    if (!_backendPollingEnabled || isPollingPaused()) {
      return;
    }
    checkBackendStatus();
  }, delayMs);
  return delayMs;
}

function scheduleNextSelectionCheck() {
  cancelSelectionPollTimer();
  if (!_selectionPollingEnabled || isPollingPaused()) {
    return false;
  }
  _selectionPollTimer = setTimeout(() => {
    _selectionPollTimer = null;
    if (!_selectionPollingEnabled || isPollingPaused()) {
      return;
    }
    checkActiveSelection();
    scheduleNextSelectionCheck();
  }, SELECTION_POLL_INTERVAL_MS);
  return true;
}

function syncPollingWithActivity({ triggerImmediate = false } = {}) {
  if (isPollingPaused()) {
    cancelBackendPollTimer();
    cancelSelectionPollTimer();
    return;
  }

  if (triggerImmediate) {
    checkActiveSelection();
  }
  if (_selectionPollingEnabled && _selectionPollTimer === null) {
    scheduleNextSelectionCheck();
  }

  if (_backendPollingEnabled) {
    const now = Date.now();
    if (triggerImmediate && !_backendCheckInFlight && (now - _lastBackendCheckAt >= BACKEND_FOCUS_DEBOUNCE_MS)) {
      checkBackendStatus();
    } else if (_backendPollTimer === null && !_backendCheckInFlight) {
      scheduleNextBackendCheck();
    }
  }
}

function updateBackendStatusTooltips(status) {
  const isOffline = status === "disconnected" || status === "offline";
  const isConnecting = status === "connecting";
  if (dropdownStatusItem) {
    if (isOffline) {
      dropdownStatusItem.title = "Local backend is offline. Click to reconnect.";
      dropdownStatusItem.setAttribute("role", "button");
      dropdownStatusItem.setAttribute("tabindex", "0");
      dropdownStatusItem.classList.add("is-offline");
    } else if (isConnecting) {
      dropdownStatusItem.title = "Connecting to local backend...";
      dropdownStatusItem.removeAttribute("role");
      dropdownStatusItem.removeAttribute("tabindex");
      dropdownStatusItem.classList.remove("is-offline");
    } else {
      dropdownStatusItem.title = "Local backend is connected.";
      dropdownStatusItem.removeAttribute("role");
      dropdownStatusItem.removeAttribute("tabindex");
      dropdownStatusItem.classList.remove("is-offline");
    }
  }
  const footerContainer = modelNameDisplay ? modelNameDisplay.parentElement : null;
  if (footerContainer) {
    if (isOffline) {
      footerContainer.classList.add("is-offline");
      footerContainer.title = "Local backend is offline. Click to reconnect.";
      footerContainer.setAttribute("role", "button");
      footerContainer.setAttribute("tabindex", "0");
    } else {
      footerContainer.classList.remove("is-offline");
      footerContainer.removeAttribute("role");
      footerContainer.removeAttribute("tabindex");
    }
  }
}

async function triggerManualBackendReconnect() {
  if (_backendCheckInFlight) return _backendCheckInFlight;
  statusDot.className = "status-dot connecting";
  statusText.textContent = "Local Backend: Connecting...";
  if (modelNameDisplay) modelNameDisplay.textContent = "Connecting...";
  updateBackendStatusTooltips("connecting");
  try { resolveZotero()?.Zocrates?.setBackendStatus?.("connecting", "Connecting..."); } catch (_) {}
  try { resolveZotero()?.Zocrates?.retryBackendLaunch?.(); } catch (_) {}
  cancelBackendPollTimer();
  return await checkBackendStatus();
}

if (dropdownStatusItem) {
  dropdownStatusItem.addEventListener("click", (e) => {
    e.stopPropagation();
    triggerManualBackendReconnect();
  });
  dropdownStatusItem.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      triggerManualBackendReconnect();
    }
  });
}
if (modelNameDisplay && modelNameDisplay.parentElement) {
  modelNameDisplay.parentElement.addEventListener("click", (e) => {
    if (_lastBackendOnline === false) {
      e.stopPropagation();
      triggerManualBackendReconnect();
    }
  });
  modelNameDisplay.parentElement.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && _lastBackendOnline === false) {
      e.preventDefault();
      e.stopPropagation();
      triggerManualBackendReconnect();
    }
  });
}

function handleBackendOfflinePush() {
  statusDot.className = "status-dot disconnected";
  statusText.textContent = "Local Backend: Offline";
  registrationPanel.classList.add("hidden");
  syncBtn.disabled = true;
  modelNameDisplay.textContent = "Offline";
  updateBackendStatusTooltips("disconnected");
  const shuttingDown = Boolean(resolveZotero()?.Zocrates?._shuttingDown);
  if (_lastBackendOnline === true && !shuttingDown) {
    addNotification("Zocrates backend disconnected or unreachable.", "error");
  }
  _lastBackendOnline = false;
  if (_consecutiveBackendFailures < 1) {
    _consecutiveBackendFailures = 1;
  }
  if (_backendPollingEnabled) {
    scheduleNextBackendCheck();
  }
}

function _addTrackedListener(target, type, fn) {
  if (!target || typeof target.addEventListener !== "function") return;
  try {
    target.addEventListener(type, fn);
    _registeredActivityListeners.push({ target, type, fn });
  } catch (e) {}
}

function _addTrackedObsObserver(obsService, observer, topic) {
  if (!obsService || typeof obsService.addObserver !== "function") return;
  try {
    obsService.addObserver(observer, topic, false);
    _registeredObsListeners.push({ obsService, observer, topic });
  } catch (e) {}
}

function installActivityListeners() {
  if (_activityListenersInstalled) return;
  _activityListenersInstalled = true;

  const onActivate = () => syncPollingWithActivity({ triggerImmediate: true });
  const onDeactivate = () => syncPollingWithActivity({ triggerImmediate: false });
  const onVisibilityChange = () => syncPollingWithActivity({ triggerImmediate: !isPollingPaused() });

  const hostWindows = new Set();
  try {
    if (window.top && window.top !== window && typeof window.top.addEventListener === "function") {
      hostWindows.add(window.top);
    }
  } catch (e) {}
  try {
    if (window.parent && window.parent !== window && typeof window.parent.addEventListener === "function") {
      hostWindows.add(window.parent);
    }
  } catch (e) {}
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    hostWindows.add(window);
  }
  if (isDetached) {
    try {
      const svc = resolveServices();
      const mainWin = svc?.wm?.getMostRecentWindow?.("navigator:browser") || window.opener;
      if (mainWin && mainWin !== window && !mainWin.closed && typeof mainWin.addEventListener === "function") {
        hostWindows.add(mainWin);
      }
    } catch (e) {}
  }

  for (const targetWin of hostWindows) {
    _addTrackedListener(targetWin, "focus", onActivate);
    _addTrackedListener(targetWin, "activate", onActivate);
    _addTrackedListener(targetWin, "blur", onDeactivate);
    _addTrackedListener(targetWin, "deactivate", onDeactivate);
    try {
      if (targetWin.document && typeof targetWin.document.addEventListener === "function") {
        _addTrackedListener(targetWin.document, "visibilitychange", onVisibilityChange);
      }
    } catch (e) {}
  }

  if (!isDetached) {
    const sidebarEl = getEmbeddedSidebarElement();
    const MutObs = (typeof MutationObserver !== "undefined" && MutationObserver)
      || window.parent?.MutationObserver
      || window.top?.MutationObserver;
    if (sidebarEl && typeof MutObs === "function") {
      try {
        _sidebarVisibilityObserver = new MutObs(() => {
          syncPollingWithActivity({ triggerImmediate: !isPollingPaused() });
        });
        _sidebarVisibilityObserver.observe(sidebarEl, {
          attributes: true,
          attributeFilter: ["hidden", "collapsed"],
        });
      } catch (e) {
        _sidebarVisibilityObserver = null;
      }
    }
  }

  const svc = resolveServices();
  if (svc?.obs) {
    _addTrackedObsObserver(
      svc.obs,
      { observe: () => syncPollingWithActivity({ triggerImmediate: !isPollingPaused() }) },
      "zocrates-sidebar-visibility-changed"
    );
    _addTrackedObsObserver(
      svc.obs,
      {
        observe: (_subject, _topic, data) => {
          try {
            const payload = data ? JSON.parse(data) : null;
            if (!payload || payload.status === "offline") {
              handleBackendOfflinePush();
            }
          } catch (_) {
            handleBackendOfflinePush();
          }
        },
      },
      "zocrates-backend-status-changed"
    );
  }
}

function cleanupActivityListeners() {
  cancelBackendPollTimer();
  cancelSelectionPollTimer();

  const listeners = _registeredActivityListeners.splice(0, _registeredActivityListeners.length);
  for (const entry of listeners) {
    try {
      entry.target.removeEventListener(entry.type, entry.fn);
    } catch (e) {}
  }

  if (_sidebarVisibilityObserver) {
    try {
      _sidebarVisibilityObserver.disconnect();
    } catch (e) {}
    _sidebarVisibilityObserver = null;
  }

  const obsEntries = _registeredObsListeners.splice(0, _registeredObsListeners.length);
  for (const entry of obsEntries) {
    try {
      entry.obsService.removeObserver(entry.observer, entry.topic);
    } catch (e) {}
  }

  _activityListenersInstalled = false;
}

function checkBackendStatus() {
  if (_backendCheckInFlight) {
    return _backendCheckInFlight;
  }
  _lastBackendCheckAt = Date.now();
  _backendCheckInFlight = (async () => {
    try {
      if (_lastBackendOnline === null) {
        statusDot.className = "status-dot connecting";
        statusText.textContent = "Local Backend: Connecting...";
        updateBackendStatusTooltips("connecting");
        try { resolveZotero()?.Zocrates?.setBackendStatus?.("connecting", "Connecting..."); } catch (_) {}
      }

      const res = await fetch(`${BACKEND_URL}/health`, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) throw new Error("Server error");
      
      const data = await res.json();
      if (data.status === "ready") {
        statusDot.className = "status-dot connected";
        statusText.textContent = "Local Backend: Connected";
        updateBackendStatusTooltips("connected");
        try { resolveZotero()?.Zocrates?.setBackendStatus?.("connected", "Connected"); } catch (_) {}
        registrationPanel.classList.add("hidden");
        // The backend just answered a /health probe: dissolve the Phase 3
        // onboarding bootstrap card if it is still showing.
        removeBootstrapProgressCard();
        syncBtn.disabled = false;
        if (data.model) {
          updateDisplayedModelName(data.model, data.llm_provider);
        }
        if (_lastBackendOnline === false) {
          addNotification("Zocrates backend reconnected.", "success");
        }
        if (_lastBackendOnline === false || sessions.length === 0 || (sessions.length === 1 && sessions[0].id && sessions[0].id.startsWith("session_"))) {
          loadSessions();
        }
        _lastBackendOnline = true;
        _consecutiveBackendFailures = 0;
      } else {
        // Backend is online but not registered. Auto-register in the background.
        await autoRegister();
        if (_lastBackendOnline === true) {
          _consecutiveBackendFailures = 0;
        } else {
          _consecutiveBackendFailures++;
        }
      }
    } catch (err) {
      // Self-heal: if the namespace helper now resolves a DIFFERENT URL than the
      // one captured at load (e.g. the handshake set activePort after this
      // iframe loaded and the done event was missed or its reload failed),
      // reload so every URL consumer re-resolves instead of showing Offline.
      try {
        const zot = resolveZotero();
        const liveUrl = zot?.Zocrates?.getBackendUrl?.();
        if (liveUrl && liveUrl !== BACKEND_URL) {
          try { Zotero.debug("Zocrates sidebar: backend URL drifted (" + BACKEND_URL + " -> " + liveUrl + "); reloading sidebar"); } catch (e) {}
          window.location.reload();
          return;
        }
      } catch (e) {}
      statusDot.className = "status-dot disconnected";
      statusText.textContent = "Local Backend: Offline";
      try { resolveZotero()?.Zocrates?.setBackendStatus?.("offline", "Offline"); } catch (_) {}
      registrationPanel.classList.add("hidden");
      syncBtn.disabled = true;
      modelNameDisplay.textContent = "Offline";
      updateBackendStatusTooltips("disconnected");
      if (_lastBackendOnline === true) {
        addNotification("Zocrates backend disconnected or unreachable.", "error");
      }
      _lastBackendOnline = false;
      _consecutiveBackendFailures++;
    } finally {
      _backendCheckInFlight = null;
      if (_backendPollingEnabled) {
        scheduleNextBackendCheck();
      }
    }
  })();
  return _backendCheckInFlight;
}

function resolvePoliteEmail() {
  try {
    const zot = (typeof resolveZotero === "function" ? resolveZotero() : null)
      || (typeof window !== "undefined" && window.Zotero ? window.Zotero : null)
      || (typeof global !== "undefined" && global.Zotero ? global.Zotero : null)
      || (typeof globalThis !== "undefined" && globalThis.Zotero ? globalThis.Zotero : null)
      || (typeof Zotero !== "undefined" ? Zotero : null);
    let cfgEmail = zot?.Prefs?.get?.("extensions.zocrates.scholarPoliteEmail", true);
    if (cfgEmail && typeof cfgEmail === "string" && cfgEmail.includes("@")) {
      return cfgEmail.trim();
    }
    let syncUsername = zot?.Prefs?.get?.("sync.server.username");
    if (syncUsername && typeof syncUsername === "string" && syncUsername.includes("@")) {
      return syncUsername.trim();
    }
  } catch (e) {
    console.warn("Zocrates: Error resolving polite email:", e);
  }
  return "";
}

function saveStoredApiKey(keyName, apiKey) {
  if (!keyName) return;
  const zot = (typeof resolveZotero === "function" ? resolveZotero() : null)
    || (typeof window !== "undefined" && window.Zotero ? window.Zotero : null)
    || (typeof global !== "undefined" && global.Zotero ? global.Zotero : null)
    || (typeof globalThis !== "undefined" && globalThis.Zotero ? globalThis.Zotero : null)
    || (typeof Zotero !== "undefined" ? Zotero : null);

  try {
    if (zot?.Zocrates?.saveStoredApiKey && typeof zot.Zocrates.saveStoredApiKey === "function") {
      return zot.Zocrates.saveStoredApiKey(keyName, apiKey);
    }
  } catch (e) {}
}

function loadStoredApiKey(keyName) {
  if (!keyName) return "";
  const zot = (typeof resolveZotero === "function" ? resolveZotero() : null)
    || (typeof window !== "undefined" && window.Zotero ? window.Zotero : null)
    || (typeof global !== "undefined" && global.Zotero ? global.Zotero : null)
    || (typeof globalThis !== "undefined" && globalThis.Zotero ? globalThis.Zotero : null)
    || (typeof Zotero !== "undefined" ? Zotero : null);

  // If bootstrap exported loadStoredApiKey on Zotero.Zocrates, delegate to it
  try {
    if (zot?.Zocrates?.loadStoredApiKey && typeof zot.Zocrates.loadStoredApiKey === "function") {
      const k = zot.Zocrates.loadStoredApiKey(keyName);
      if (k && typeof k === "string" && k.trim()) return k.trim();
    }
  } catch (e) {}

  // Fall back to direct Zotero.Prefs inspection if available
  try {
    if (zot?.Prefs && typeof zot.Prefs.get === "function") {
      let targets = [keyName];
      if (keyName.includes("openrouter") || keyName.includes("openRouter")) {
        targets = ["openrouter_api_key", "openRouterApiKey"];
      } else if (keyName.includes("datalab") || keyName.includes("dataLab")) {
        targets = ["datalab_api_key", "datalabApiKey"];
      } else if (keyName.includes("openalex") || keyName.includes("openAlex")) {
        targets = ["openalex_api_key", "openAlexApiKey"];
      } else if (keyName.includes("gemini") || keyName.includes("cloud")) {
        targets = ["cloudApiKey", "gemini_api_key"];
      }
      for (const t of targets) {
        let v = zot.Prefs.get("extensions.zocrates." + t, true) || zot.Prefs.get("extensions.zotero.zocrates." + t, true);
        if (v && typeof v === "string" && v.trim()) return v.trim();
      }
    }
  } catch (e) {}

  return "";
}

async function autoRegister() {
  if (!Zotero) {
    statusDot.className = "status-dot connected";
    statusText.textContent = "Local Backend: Needs Registration";
    try { resolveZotero()?.Zocrates?.setBackendStatus?.("connected", "Needs Registration"); } catch (_) {}
    registrationPanel.classList.remove("hidden");
    syncBtn.disabled = true;
    return;
  }

  const profileDir = Zotero.DataDirectory.dir;
  const politeEmail = resolvePoliteEmail();
  const openrouterApiKey = loadStoredApiKey("openrouter_api_key");
  const datalabApiKey = loadStoredApiKey("datalab_api_key");
  const openalexApiKey = loadStoredApiKey("openalex_api_key");
  try {
    statusText.textContent = "Local Backend: Auto-registering...";
    try { resolveZotero()?.Zocrates?.setBackendStatus?.("connecting", "Auto-registering..."); } catch (_) {}
    const res = await fetch(`${BACKEND_URL}/register`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        zotero_data_dir: profileDir,
        scholar_polite_email: politeEmail || undefined,
        openrouter_api_key: openrouterApiKey || undefined,
        datalab_api_key: datalabApiKey || undefined,
        openalex_api_key: openalexApiKey || undefined,
      })
    });
    if (res.ok) {
      statusDot.className = "status-dot connected";
      statusText.textContent = "Local Backend: Connected";
      try { resolveZotero()?.Zocrates?.setBackendStatus?.("connected", "Connected"); } catch (_) {}
      registrationPanel.classList.add("hidden");
      syncBtn.disabled = false;
      if (_lastBackendOnline === false) {
        addNotification("Zocrates backend reconnected.", "success");
      }
      if (_lastBackendOnline === false || sessions.length === 0 || (sessions.length === 1 && sessions[0].id && sessions[0].id.startsWith("session_"))) {
        loadSessions();
      }
      _lastBackendOnline = true;
    } else {
      let detail = "";
      try { detail = (await res.text()).slice(0, 300); } catch (e) {}
      try { Zotero.debug("Zocrates sidebar: auto-register failed (HTTP " + res.status + "): " + detail); } catch (e) {}
      throw new Error("Registration failed" + (detail ? ": " + detail : ""));
    }
  } catch (err) {
    console.error("Auto-registration failed:", err);
    try { Zotero.debug("Zocrates sidebar: auto-register exception: " + (err && err.message ? err.message : err)); } catch (e) {}
    statusDot.className = "status-dot connected";
    statusText.textContent = "Local Backend: Needs Registration";
    try { resolveZotero()?.Zocrates?.setBackendStatus?.("connected", "Needs Registration"); } catch (_) {}
    registrationPanel.classList.remove("hidden");
    syncBtn.disabled = true;
    addNotification("Auto-registration with Zocrates backend failed: " + (err.message || err), "error");
  }
}

registerBtn.addEventListener("click", async () => {
  if (!Zotero) {
    addNotification("Zotero instance not detected.");
    return;
  }

  const profileDir = Zotero.DataDirectory.dir;
  const politeEmail = resolvePoliteEmail();
  const openrouterApiKey = loadStoredApiKey("openrouter_api_key");
  const datalabApiKey = loadStoredApiKey("datalab_api_key");
  const openalexApiKey = loadStoredApiKey("openalex_api_key");
  try {
    statusText.textContent = "Local Backend: Registering...";
    const res = await fetch(`${BACKEND_URL}/register`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        zotero_data_dir: profileDir,
        scholar_polite_email: politeEmail || undefined,
        openrouter_api_key: openrouterApiKey || undefined,
        datalab_api_key: datalabApiKey || undefined,
        openalex_api_key: openalexApiKey || undefined,
      })
    });
    const data = await res.json();
    if (res.ok) {
      addNotification("Zocrates backend linked successfully!", "success");
      checkBackendStatus();
    } else {
      addNotification("Registration failed: " + data.detail, "error");
    }
  } catch (err) {
    addNotification("Failed to connect to backend: " + err.message, "error");
  }
});

syncBtn.addEventListener("click", async () => {
  if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
  if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
  if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
  try {
    statusText.textContent = "Updating Library Index...";
    const res = await fetch(`${BACKEND_URL}/sync`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({})
    });
    if (res.ok) {
      addNotification("Library index update requested successfully!", "success");
    } else {
      const data = await res.json();
      addNotification("Library index update failed: " + data.detail, "error");
    }
  } catch (err) {
    addNotification("Failed to trigger library index update: " + err.message, "error");
  } finally {
    checkBackendStatus();
  }
});

// Track explicitly removed items' keys
let removedKeys = new Set();

// Debug helper — logs to console
function setDebug(msg) {
  if (msg) console.log("[Zocrates Debug]", msg);
}

function renderActiveChips() {
  try {
    if (!chipsContainer) {
      setDebug("❌ chipsContainer element not found in DOM");
      console.error("Zocrates: chipsContainer is null");
      return;
    }

    chipsContainer.innerHTML = "";
    if (typeof document !== "undefined" && document.querySelectorAll) {
      document.querySelectorAll(".snippet-hover-preview").forEach(p => p.remove());
    }

    activeItemsList.forEach(paper => {
      const chip = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      chip.className = "paper-chip";
      
      let bg = "rgba(99, 102, 241, 0.2)";
      let border = "rgba(99, 102, 241, 0.4)";
      let icon = "📄";
      
      if (paper.type === "library") {
        bg = "rgba(234, 179, 8, 0.2)";
        border = "rgba(234, 179, 8, 0.4)";
        icon = "📚";
      } else if (paper.type === "collection") {
        bg = "rgba(16, 185, 129, 0.2)";
        border = "rgba(16, 185, 129, 0.4)";
        icon = "📁";
      } else if (paper.type === "note") {
        bg = "rgba(168, 85, 247, 0.2)";
        border = "rgba(168, 85, 247, 0.4)";
        icon = "📝";
      }

      chip.style.background = bg;
      chip.style.borderColor = border;

      // Icon span
      const iconSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      iconSpan.className = "chip-icon";
      iconSpan.textContent = icon;
      chip.appendChild(iconSpan);

      // Title span
      const titleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      titleSpan.className = "chip-text";
      titleSpan.title = paper.title;
      titleSpan.textContent = paper.title;
      chip.appendChild(titleSpan);

      // Close button
      const closeBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
      closeBtn.className = "chip-remove";
      closeBtn.title = "Remove context";
      closeBtn.textContent = "✕";

      closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        removedKeys.add(paper.key);
        activeItemsList = activeItemsList.filter(p => p.key !== paper.key);
        
        if (paper.type === "collection" || paper.type === "library") {
          const hasCollections = activeItemsList.some(p => p.type === "collection");
          const hasLibrary = activeItemsList.some(p => p.type === "library");
          if (!hasCollections && !hasLibrary) {
            contextMode = "papers";
            lastActiveKeysStr = "";
            const items = readSelectedItemsNow();
            applySelection(items, true);
          } else {
            renderActiveChips();
          }
        } else {
          renderActiveChips();
        }
      });

      chip.appendChild(closeBtn);
      chipsContainer.appendChild(chip);
    });

    // Snippet chips
    activeSnippetsList.forEach(snippet => {
      const chip = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      chip.className = "paper-chip snippet-chip " + (snippet.type === "image" ? "snippet-image-chip" : "snippet-text-chip");
      chip.dataset.snippetId = snippet.id;
      chip.setAttribute("data-snippet-id", snippet.id);

      // Format short author citation for snippet tag
      let authorTag = (snippet.author_str || snippet.authorStr || "").trim();
      if (!authorTag && Array.isArray(snippet.authors) && snippet.authors.length > 0) {
        if (snippet.authors.length === 1) authorTag = snippet.authors[0];
        else if (snippet.authors.length === 2) authorTag = snippet.authors[0] + " & " + snippet.authors[1];
        else authorTag = snippet.authors[0] + " et al.";
      }
      if (!authorTag) {
        const rawTitle = snippet.paper_title || snippet.paperTitle || "Paper";
        authorTag = rawTitle.length > 20 ? rawTitle.slice(0, 19) + "…" : rawTitle;
      }
      const pageStr = snippet.page ? " (p." + snippet.page + ")" : "";

      if (snippet.type === "image") {
        const thumbImg = document.createElementNS("http://www.w3.org/1999/xhtml", "img");
        thumbImg.className = "snippet-thumbnail";
        thumbImg.src = snippet.thumbnail_data || snippet.thumbnailData || snippet.image_data || snippet.imageData || "";
        thumbImg.alt = "Area preview";
        chip.appendChild(thumbImg);

        const textSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        textSpan.className = "chip-text";
        const strongTag = document.createElementNS("http://www.w3.org/1999/xhtml", "strong");
        strongTag.className = "snippet-paper-tag";
        strongTag.textContent = authorTag + pageStr + ":";
        textSpan.appendChild(strongTag);
        textSpan.appendChild(document.createTextNode(" Area Crop"));
        chip.appendChild(textSpan);

        // Hover preview popover for image snippets
        let previewPopup = null;
        chip.addEventListener("mouseenter", () => {
          const fullImage = snippet.image_data || snippet.imageData;
          if (!fullImage) return;
          previewPopup = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
          previewPopup.className = "snippet-hover-preview";
          const popImg = document.createElementNS("http://www.w3.org/1999/xhtml", "img");
          popImg.src = fullImage;
          previewPopup.appendChild(popImg);
          const popLbl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
          popLbl.className = "snippet-preview-label";
          const paperLabel = (snippet.paper_title || snippet.paperTitle || "Paper");
          popLbl.textContent = (authorTag ? authorTag + " · " : "") + paperLabel + (snippet.page ? " · Page " + snippet.page : "");
          previewPopup.appendChild(popLbl);

          if (typeof chip.getBoundingClientRect === "function") {
            const rect = chip.getBoundingClientRect();
            const viewportH = (typeof window !== "undefined" && window.innerHeight) || 800;
            previewPopup.style.left = Math.max(8, rect.left) + "px";
            previewPopup.style.bottom = Math.max(8, viewportH - rect.top + 6) + "px";
          }
          if (document.body) {
            document.body.appendChild(previewPopup);
          } else {
            chip.appendChild(previewPopup);
          }
        });
        chip.addEventListener("mouseleave", () => {
          if (previewPopup) {
            previewPopup.remove();
            previewPopup = null;
          }
        });
      } else {
        // Text snippet: High-clarity vector document icon with visible lines
        const iconSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        iconSpan.className = "chip-icon snippet-icon";
        const svgNS = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(svgNS, "svg");
        svg.setAttribute("viewBox", "0 0 16 16");
        svg.setAttribute("width", "13");
        svg.setAttribute("height", "13");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "1.3");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.style.display = "block";

        const docPath = document.createElementNS(svgNS, "path");
        docPath.setAttribute("d", "M3.5 1.5h6l3 3v10a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1z");
        svg.appendChild(docPath);

        const foldPath = document.createElementNS(svgNS, "polyline");
        foldPath.setAttribute("points", "9.5 1.5 9.5 4.5 12.5 4.5");
        svg.appendChild(foldPath);

        const line1 = document.createElementNS(svgNS, "line");
        line1.setAttribute("x1", "5.5"); line1.setAttribute("y1", "7"); line1.setAttribute("x2", "10.5"); line1.setAttribute("y2", "7");
        svg.appendChild(line1);

        const line2 = document.createElementNS(svgNS, "line");
        line2.setAttribute("x1", "5.5"); line2.setAttribute("y1", "9.5"); line2.setAttribute("x2", "10.5"); line2.setAttribute("y2", "9.5");
        svg.appendChild(line2);

        const line3 = document.createElementNS(svgNS, "line");
        line3.setAttribute("x1", "5.5"); line3.setAttribute("y1", "12"); line3.setAttribute("x2", "8.5"); line3.setAttribute("y2", "12");
        svg.appendChild(line3);

        iconSpan.appendChild(svg);
        chip.appendChild(iconSpan);

        const textSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        textSpan.className = "chip-text";
        const fullQuote = (snippet.text || "").trim();
        const truncQuote = fullQuote.length > 40 ? fullQuote.slice(0, 38) + "…" : fullQuote;
        const strongTag = document.createElementNS("http://www.w3.org/1999/xhtml", "strong");
        strongTag.className = "snippet-paper-tag";
        strongTag.textContent = authorTag + pageStr + ":";
        textSpan.appendChild(strongTag);
        textSpan.appendChild(document.createTextNode(' "' + truncQuote + '"'));
        const paperLabel = (snippet.paper_title || snippet.paperTitle || "Paper");
        textSpan.title = (authorTag ? authorTag + " · " : "") + paperLabel + pageStr + '\n"' + fullQuote + '"';
        chip.appendChild(textSpan);
      }

      // Close button
      const closeBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
      closeBtn.className = "chip-remove";
      closeBtn.title = "Remove snippet";
      closeBtn.textContent = "✕";
      closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        activeSnippetsList = activeSnippetsList.filter(s => s.id !== snippet.id);
        renderActiveChips();
      });
      chip.appendChild(closeBtn);
      chipsContainer.appendChild(chip);
    });

    // Toggle visibility of clear-all-chips-btn based on activeItemsList length (only show when 2 or more chips are present)
    const totalChips = activeItemsList.length + activeSnippetsList.length;
    if (clearAllChipsBtn) {
      if (totalChips < 2) {
        clearAllChipsBtn.classList.add("hidden");
      } else {
        clearAllChipsBtn.classList.remove("hidden");
      }
    }

    setDebug("✓ " + totalChips + " chip(s) rendered");
    syncOpenGraphTabIfPresent();
  } catch (err) {
    setDebug("❌ renderActiveChips error: " + err);
    console.error("Zocrates: renderActiveChips error:", err);
  }
}

// ---------------------------------------------------------------------------
// Network Graph Visualizer Sync
// ---------------------------------------------------------------------------

function syncOpenGraphTabIfPresent() {
  try {
    const zot = resolveZotero();
    let mainWin = null;
    if (zot && typeof zot.getMainWindow === "function") {
      mainWin = zot.getMainWindow();
    }
    if (!mainWin) {
      mainWin = window.parent?.Zotero ? window.parent : (window.top?.Zotero ? window.top : null);
    }
    if (!mainWin && typeof Services !== "undefined" && Services.wm) {
      mainWin = Services.wm.getMostRecentWindow("navigator:browser") || Services.wm.getMostRecentWindow("zotero:main");
    }

    if (!mainWin || !mainWin.Zotero_Tabs) return;

    const existingTab = Array.isArray(mainWin.Zotero_Tabs._tabs)
      ? mainWin.Zotero_Tabs._tabs.find(t => t && (t.id === "zocrates-network-graph-tab" || t.type === "zocrates_graph"))
      : null;

    if (!existingTab) return;

    const container = mainWin.document.getElementById(existingTab.id);
    const iframe = container ? container.querySelector("iframe") : null;
    if (!iframe || !iframe.contentWindow) return;

    const cw = iframe.contentWindow;
    if (cw.isLockedToFullLibrary || cw.isUserLockedToFullLibrary) {
      return;
    }

    if (typeof cw.loadGraph !== "function") return;

    const paperChips = activeItemsList.filter(item => (item.type === "item" || item.type === "paper" || !item.type) && item.key);
    const collectionChips = activeItemsList.filter(item => item.type === "collection");

    const paramsObj = {};
    if (paperChips.length === 1) {
      paramsObj.center_key = paperChips[0].key;
      paramsObj.depth = "2";
    } else if (paperChips.length > 1) {
      paramsObj.center_keys = paperChips.map(p => p.key).join(",");
      paramsObj.depth = "1";
    } else if (collectionChips.length >= 1) {
      paramsObj.collection_keys = collectionChips.map(c => c.key).join(",");
    }

    const paramsKey = JSON.stringify(paramsObj);
    if (cw._syncedParamsKey === paramsKey) {
      return;
    }
    cw._syncedParamsKey = paramsKey;

    cw.loadGraph(paramsObj);
  } catch (err) {
    console.warn("Zocrates: syncOpenGraphTabIfPresent error:", err);
  }
}

// ---------------------------------------------------------------------------
// Snippet Handling & Integration
// ---------------------------------------------------------------------------

function handleIncomingSnippet(snippet) {
  if (!snippet) return;
  // 1. Auto-switch context mode to "papers" and clear any existing collection or library chips
  if (contextMode !== "papers") {
    contextMode = "papers";
    activeItemsList = activeItemsList.filter(item => item.type !== "library" && item.type !== "collection");
    if (typeof updateContextDropdownUI === "function") updateContextDropdownUI();
  } else {
    activeItemsList = activeItemsList.filter(item => item.type !== "library" && item.type !== "collection");
  }

  // 2. Add to activeSnippetsList (deduplicate by snippet.id)
  const alreadyInList = activeSnippetsList.some(s => s.id === snippet.id);
  if (!alreadyInList) {
    activeSnippetsList.push(snippet);
  }
  // 3. Re-render chips
  renderActiveChips();
}

try {
  const zot = resolveZotero();
  if (zot?.Zocrates?.snippetSubscribers) {
    zot.Zocrates.snippetSubscribers.add(handleIncomingSnippet);
    window.addEventListener("unload", () => {
      try {
        zot.Zocrates.snippetSubscribers.delete(handleIncomingSnippet);
      } catch (e) {}
    });
  }
} catch (e) {
  console.warn("Zocrates: Error subscribing to snippetSubscribers:", e);
}

function handleSnippetUpdate(updatedData) {
  if (!updatedData) return;
  const targetKey = updatedData.annotation_id || updatedData.annotationId;
  const targetItemId = updatedData.annotation_item_id || updatedData.annotationItemId;
  if (!targetKey && !targetItemId) return;
  let modified = false;
  for (let snip of activeSnippetsList) {
    if (snip.type === "image") {
      let isMatch = (targetKey && (snip.annotation_id === targetKey || snip.annotationId === targetKey)) ||
                    (targetItemId && (snip.annotation_item_id === targetItemId || snip.annotationItemId === targetItemId || String(snip.annotation_id) === String(targetItemId)));
      if (isMatch) {
        snip.image_data = updatedData.image_data || updatedData.imageData;
        snip.imageData = updatedData.imageData || updatedData.image_data;
        if (updatedData.thumbnail_data || updatedData.thumbnailData) {
          snip.thumbnail_data = updatedData.thumbnail_data || updatedData.thumbnailData;
          snip.thumbnailData = updatedData.thumbnailData || updatedData.thumbnail_data;
        }
        if (updatedData.width) snip.width = updatedData.width;
        if (updatedData.height) snip.height = updatedData.height;
        modified = true;
      }
    }
  }
  if (modified) {
    renderActiveChips();
  }
}

try {
  const zot = resolveZotero();
  if (zot?.Zocrates?.updateSnippetSubscribers) {
    zot.Zocrates.updateSnippetSubscribers.add(handleSnippetUpdate);
    window.addEventListener("unload", () => {
      try {
        zot.Zocrates.updateSnippetSubscribers.delete(handleSnippetUpdate);
      } catch (e) {}
    });
  }
} catch (e) {
  console.warn("Zocrates: Error subscribing to updateSnippetSubscribers:", e);
}

// ---------------------------------------------------------------------------
// Phase 7 Onboarding: stage machine state (declared BEFORE the bootstrap
// progress section so the replayed done snapshot below never hits a TDZ on
// these bindings).
// ---------------------------------------------------------------------------

// Phase 7: defensive IOUtils resolution — mirrors the established pattern in
// ensureSessionsDir (bare global, then parent/top frame fallbacks). Resolving
// only the bare global would silently degrade the entire onboarding layer to
// "legacy" in sidebar contexts where the global is not exposed.
function resolveOnboardingIO() {
  return (typeof IOUtils !== "undefined" ? IOUtils :
         (window.parent && window.parent.IOUtils ? window.parent.IOUtils :
         (window.top && window.top.IOUtils ? window.top.IOUtils : null)));
}

function onboardingStateIoAvailable() {
  try {
    const z = resolveZotero();
    return !!(resolveOnboardingIO() && z && z.DataDirectory && z.DataDirectory.dir);
  } catch (e) {
    return false;
  }
}

// Phase 7 stage machine state. `null` = legacy (no state file) or unresolved.
let onboardingStage = null;
let onboardingStateMeta = null;       // last read/written state-file payload
let _onboardingStatePending = onboardingStateIoAvailable();
let _pendingBootstrapDone = false;    // done event that raced the state read
let _onboardingStreamAbort = null;    // AbortController for POST /sync/stream
let _onboardingStreamActive = false;
let _onboardingFinished = false;      // one-shot guard per PROCESS run
let _onboardingWizardRefs = null;     // live wizard field references

// ---------------------------------------------------------------------------
// Phase 3 Onboarding: backend bootstrap progress card
// ---------------------------------------------------------------------------
// Non-blocking progress card rendered in the chat message area while the
// backend launch orchestration (bootstrap.js) is provisioning / starting the
// backend. Driven by the Zotero.Zocrates bootstrap progress event bus and
// dissolved once the ready event fires (or the first /status-backed
// checkBackendStatus success). All DOM is built via createElementNS with
// textContent only (XSS-safe); styling uses the shared 5-theme variables.

const BOOTSTRAP_STEP_KEYS = ["preparing", "python", "engine", "starting"];
const BOOTSTRAP_STEP_LABELS = {
  preparing: "Preparing package manager…",
  python: "Installing Python 3.13 runtime…",
  engine: "Installing Zocrates AI engine & models…",
  starting: "Starting backend server…",
};
const BOOTSTRAP_STEP_WIDTHS = {
  preparing: "15%",
  python: "45%",
  engine: "75%",
  starting: "92%",
};

function removeBootstrapProgressCard() {
  // Phase 7 gate composition: while onboarding is mid-flow (CONFIGURE wizard
  // or PROCESS conversion), this routine must NOT dissolve the card nor lift
  // the chat gate — the 10s /status poll would otherwise dismiss the wizard
  // on a fresh profile with pending onboarding. The mount-time state read is
  // also honored: while it is pending the stage is not yet known, so lifting
  // the gate here would race a warm-start CONFIGURE resume on a slow disk.
  // The gate flips only when the card is actually removable: stage READY, or
  // legacy/no-state users (same behavior as before Phase 7).
  if (isOnboardingActive() || _onboardingStatePending) return;
  const existing = document.getElementById("bootstrap-progress-card");
  if (existing) existing.remove();
  // Backend usable now: re-enable chat submission exactly once. Guarded so
  // the every-10s checkBackendStatus success path cannot clobber the
  // streaming stop-mode title ("Stop generation") on the Send button.
  if (backendReadyForChat) return;
  backendReadyForChat = true;
  sendBtn.disabled = false;
  sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
}

function applyBootstrapProgressStateToCard(evt) {
  const isError = evt && evt.state === "error";
  const stepIndex = evt && evt.stepKey ? BOOTSTRAP_STEP_KEYS.indexOf(evt.stepKey) : -1;
  const failedIndex = isError && evt.detail && evt.detail.failedStep
    ? Math.min(BOOTSTRAP_STEP_KEYS.indexOf(evt.detail.failedStep), 2)
    : -1;

  // Row 3 doubles as "Starting backend server…" in the final stage.
  const engineLabel = document.getElementById("bootstrap-step-label-engine");
  if (engineLabel) {
    engineLabel.textContent = evt && evt.stepKey === "starting"
      ? BOOTSTRAP_STEP_LABELS.starting
      : BOOTSTRAP_STEP_LABELS.engine;
  }

  const rows = [
    { key: "preparing", idx: 0 },
    { key: "python", idx: 1 },
    { key: "engine", idx: 2 },
  ];
  for (const row of rows) {
    const el = document.getElementById("bootstrap-step-" + row.key);
    if (!el) continue;
    el.classList.toggle("active", !isError && stepIndex === row.idx);
    el.classList.toggle("done", stepIndex > row.idx);
    el.classList.toggle("error", failedIndex === row.idx);
  }

  const fill = document.getElementById("bootstrap-progress-fill");
  if (fill) {
    fill.style.width = (!isError && stepIndex >= 0)
      ? (BOOTSTRAP_STEP_WIDTHS[evt.stepKey] || "50%")
      : "100%";
  }

  const errBox = document.getElementById("bootstrap-card-error");
  if (errBox) {
    if (isError) {
      errBox.classList.remove("hidden");
      const msg = document.getElementById("bootstrap-error-message");
      if (msg) {
        msg.textContent = evt.detail && evt.detail.message
          ? String(evt.detail.message)
          : "Backend setup failed. Please try again.";
      }
    } else {
      errBox.classList.add("hidden");
    }
  }
}

function renderOrUpdateBootstrapProgressCard(evt) {
  let card = document.getElementById("bootstrap-progress-card");
  if (!card) {
    if (evt && evt.state === "active" && evt.stepKey === "starting" &&
        (getEffectiveOnboardingStage() === "ready" || evt.detail?.isProvisioning === false)) {
      if (sendBtn) {
        sendBtn.disabled = true;
        sendBtn.title = "Connecting to Zocrates backend…";
      }
      return;
    }
    if (!chatMessages) return;
    card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    card.id = "bootstrap-progress-card";
    card.className = "bootstrap-card";

    const title = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    title.className = "bootstrap-card-title";
    title.textContent = "Setting up Zocrates AI Engine…";
    card.appendChild(title);

    const bar = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    bar.className = "bootstrap-progress-bar";
    const fill = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    fill.className = "bootstrap-progress-fill";
    fill.id = "bootstrap-progress-fill";
    bar.appendChild(fill);
    card.appendChild(bar);

    const steps = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    steps.className = "bootstrap-steps";
    for (const key of ["preparing", "python", "engine"]) {
      const row = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      row.className = "bootstrap-step";
      row.id = "bootstrap-step-" + key;
      const dot = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      dot.className = "bootstrap-step-dot";
      const label = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      label.className = "bootstrap-step-label";
      label.id = "bootstrap-step-label-" + key;
      label.textContent = BOOTSTRAP_STEP_LABELS[key];
      row.appendChild(dot);
      row.appendChild(label);
      steps.appendChild(row);
    }
    card.appendChild(steps);

    const errBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    errBox.className = "bootstrap-card-error hidden";
    errBox.id = "bootstrap-card-error";
    const msg = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    msg.className = "bootstrap-error-message";
    msg.id = "bootstrap-error-message";
    const retryBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    retryBtn.className = "bootstrap-retry-btn";
    retryBtn.textContent = "Retry";
    retryBtn.addEventListener("click", () => {
      try {
        const zot = resolveZotero();
        if (zot?.Zocrates?.retryBackendLaunch) zot.Zocrates.retryBackendLaunch();
      } catch (e) {
        console.warn("Zocrates: bootstrap retry failed:", e);
      }
    });
    errBox.appendChild(msg);
    errBox.appendChild(retryBtn);
    card.appendChild(errBox);

    // P1-3: the card moves OUT of #chatMessages into the dedicated
    // #onboarding-container so loadActiveSessionMessages(), session-list
    // clicks, and New Chat can never destroy it. Fallback keeps the old host
    // if the container is missing (defensive against older sidebar.html).
    let host = document.getElementById("onboarding-container") || chatMessages;
    host.appendChild(card);

    // Install-tail affordance: the card no longer dissolves on done. An
    // inactive "Next" footer button is shown so the follow-up CONFIGURE step
    // is expected; markBootstrapInstallDone() activates it.
    const footer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    footer.className = "onboarding-footer";
    const nextBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    nextBtn.id = "onboarding-next-btn";
    nextBtn.className = "onboarding-next-btn";
    nextBtn.disabled = true;
    nextBtn.textContent = "Next";
    nextBtn.addEventListener("click", enterOnboardingConfigure);
    footer.appendChild(nextBtn);
    card.appendChild(footer);
  }
  applyBootstrapProgressStateToCard(evt);
  // Bootstrap in progress (active or error state): keep chat submission
  // inert until the backend is ready (see handleChatSubmit).
  sendBtn.disabled = true;
  if (evt && evt.stepKey === "starting") {
    sendBtn.title = "Connecting to Zocrates backend…";
  } else {
    sendBtn.title = "Zocrates is starting — the AI engine is installing…";
  }
}

function handleBootstrapProgressEvent(evt) {
  if (!evt) return;
  if (evt.state === "done") {
    // Phase 7: `done` is now stage-dependent. The bootstrap done event NEVER
    // mutates the onboarding stage; it only signals "backend usable".
    handleOnboardingBootstrapDone();
    return;
  }
  if (evt.state === "active" || evt.state === "error") {
    renderOrUpdateBootstrapProgressCard(evt);
  }
}

// Phase 7: stage-dependent handling of the bootstrap `done` event.
//   - no onboarding state (legacy user)   → dissolve card, lift gate
//   - stage "configure" (fresh install)   → ✓ rows, activate "Next" → wizard
//   - stage "process" (restart recovery)  → PROCESS view + re-open stream
//   - stage "ready"                       → dissolve card, lift gate
function handleOnboardingBootstrapDone() {
  try {
    const zot = resolveZotero();
    const liveUrl = zot?.Zocrates?.getBackendUrl?.();
    try { Zotero.debug("Zocrates sidebar: bootstrap done; live URL " + liveUrl + " vs captured " + BACKEND_URL); } catch (e) {}
    if (liveUrl && liveUrl !== BACKEND_URL) {
      // Port drift (e.g. this iframe mounted during a failed launch and a
      // later Retry resolved a different port): reload so every URL consumer
      // re-resolves. The replayed done snapshot on the reloaded iframe takes
      // the stage-specific path below (no reload loop).
      window.location.reload();
      return;
    }
  } catch (e) {
    try { Zotero.debug("Zocrates sidebar: done-handler URL compare failed: " + e); } catch (e2) {}
  }
  if (_onboardingStatePending) {
    // Mount-time state read still in flight: defer this done event; the
    // stage flow re-dispatches it once the stage is known.
    _pendingBootstrapDone = true;
    return;
  }
  const stage = getEffectiveOnboardingStage();
  if (stage === "configure" || stage === "process") {
    // INSTALL tail: ✓ marks + "Next" activation only. The bootstrap done
    // event NEVER advances the stage — the wizard opens on the Next click
    // (fresh install) or the PROCESS view resumes via restart recovery.
    markBootstrapInstallDone();
    if (stage === "process") {
      enterOnboardingProcess();
    } else if (!document.getElementById("bootstrap-progress-card")) {
      // Warm-start mid-CONFIGURE resume: the backend attached without a
      // fresh provisioning, so no install card exists — open the wizard.
      enterOnboardingConfigure();
    }
    return;
  }
  // Legacy user (no state file) or stage already "ready": dissolve the card
  // and lift the chat gate exactly as before Phase 7.
  removeBootstrapProgressCard();
  loadSessions();
}

try {
  const zot = resolveZotero();
  if (zot?.Zocrates?.addBootstrapProgressListener) {
    const unsubBootstrapProgress = zot.Zocrates.addBootstrapProgressListener(handleBootstrapProgressEvent);
    window.addEventListener("unload", () => {
      try { unsubBootstrapProgress(); } catch (e) {}
    });
    // Replay the current bootstrap state (e.g. the sidebar mounted after an
    // error state, or the iframe was reloaded mid-provisioning).
    const snapshot = zot.Zocrates.getBootstrapProgressState?.();
    if (snapshot) handleBootstrapProgressEvent(snapshot);
  }
} catch (e) {
  console.warn("Zocrates: Error subscribing to bootstrap progress:", e);
}

// ---------------------------------------------------------------------------
// Phase 7 Onboarding: stage machine (CONFIGURE wizard + PROCESS conversion)
// ---------------------------------------------------------------------------
// Driven by <ZoteroDataDir>/zocrates-data/onboarding_state.json (seeded by
// bootstrap.js right after a fresh provisioning succeeds) plus the process-
// wide onboarding stage bus. File absence = pre-Phase-7 (legacy) user: no
// card, chat gate lifts via the existing /status path. The backend never
// reads this file — it is a sidebar-only contract. Stage transitions:
//   configure → process (wizard Finish/Skip) → ready (stream done/abort).

const ONBOARDING_VALID_STAGES = ["configure", "process", "ready"];
const ONBOARDING_STATE_FILENAME = "onboarding_state.json";

function getOnboardingStateFilePath() {
  try {
    const z = resolveZotero();
    const dir = z && z.DataDirectory && z.DataDirectory.dir;
    if (!dir) return null;
    const sep = String(dir).includes("\\") ? "\\" : "/";
    return dir + sep + "zocrates-data" + sep + ONBOARDING_STATE_FILENAME;
  } catch (e) {
    return null;
  }
}

function onboardingVenvPresent() {
  // Corrupt-state-file micro-rule discriminator: onboarding_state.json was
  // only ever seeded right after a fresh provisioning, so a corrupt file on a
  // profile that HAS the managed .venv means an interrupted Phase 7 install →
  // fail open to the card-visible CONFIGURE state. No .venv → stale artifact
  // → treat as legacy.
  try {
    const io = resolveOnboardingIO();
    if (!io) return Promise.resolve(false);
    const z = resolveZotero();
    const dir = z && z.DataDirectory && z.DataDirectory.dir;
    if (!dir) return Promise.resolve(false);
    const sep = String(dir).includes("\\") ? "\\" : "/";
    return Promise.resolve(io.exists(dir + sep + "zocrates-data" + sep + ".venv"));
  } catch (e) {
    return Promise.resolve(false);
  }
}

function isOnboardingActive() {
  return onboardingStage === "configure" || onboardingStage === "process";
}

function getEffectiveOnboardingStage() {
  // While the mount-time state read is unresolved, behave exactly like a
  // legacy profile (pre-Phase-7 semantics): the poll guard and the gate must
  // not wait on disk I/O.
  return onboardingStage;
}

function setOnboardingStage(stage) {
  onboardingStage = ONBOARDING_VALID_STAGES.includes(stage) ? stage : null;
  try {
    const z = resolveZotero();
    // Never broadcast null: a legacy/failed-write window mounting must not
    // eject a PROCESS peer from its active flow (plan §3 multi-window bus).
    if (onboardingStage !== null && z?.Zocrates?.notifyOnboardingStage) {
      z.Zocrates.notifyOnboardingStage(onboardingStage);
    }
  } catch (e) {}
}

async function readOnboardingStateFile() {
  const path = getOnboardingStateFilePath();
  const io = resolveOnboardingIO();
  if (!path || !io) {
    return { stage: null, meta: null, corrupt: false };
  }
  try {
    const raw = await io.readUTF8(path);
    const meta = JSON.parse(raw);
    const stage = ONBOARDING_VALID_STAGES.includes(meta && meta.stage) ? meta.stage : null;
    return { stage, meta: meta || null, corrupt: false };
  } catch (e) {
    if (e && (e.name === "NotFoundError" || /exists|No such file/i.test(String((e && e.message) || e)))) {
      return { stage: null, meta: null, corrupt: false };
    }
    return { stage: null, meta: null, corrupt: true };
  }
}

async function writeOnboardingState(stage, extra = {}) {
  // Sidebar-owned transitions (configure → process → ready) + bookkeeping.
  // Atomic temp+rename write, same pattern as the bootstrap seed. Bootstrap
  // owns the single seed write; sidebar owns everything after it.
  const path = getOnboardingStateFilePath();
  const io = resolveOnboardingIO();
  if (!path || !io) return false;
  const prev = onboardingStateMeta || {};
  const nowIso = new Date().toISOString();
  const next = {
    version: 1,
    stage,
    seeded_at: prev.seeded_at || nowIso,
    config_done_at: prev.config_done_at || null,
    process_done_at: prev.process_done_at || null,
    process_aborted: Boolean(prev.process_aborted),
  };
  if (stage === "process" && !next.config_done_at) next.config_done_at = nowIso;
  if (stage === "ready") {
    if (!next.process_done_at) next.process_done_at = nowIso;
    if (extra.process_aborted) next.process_aborted = true;
  }
  if (extra.preset_applied) next.preset_applied = extra.preset_applied;
  next.stage = stage;
  try {
    const tmpPath = path + ".tmp";
    await io.writeUTF8(tmpPath, JSON.stringify(next, null, 2) + "\n");
    await io.move(tmpPath, path);
    onboardingStateMeta = next;
    return true;
  } catch (e) {
    console.warn("Zocrates: onboarding state write failed:", e);
    return false;
  }
}

function isBackendReadyForOnboardingResume() {
  try {
    const z = resolveZotero();
    return !!(z && z.Zocrates && z.Zocrates._backendReady);
  } catch (e) {
    return false;
  }
}

function isValidSessionId(sid) {
  return Boolean(sid && typeof sid === "string" && !sid.startsWith("session_"));
}

function hasExistingChatSessions() {
  if (Array.isArray(sessions)) {
    const realSessions = sessions.filter((s) => s && isValidSessionId(s.id));
    // If any session has an actual conversation title (not the initial default "New Chat").
    if (realSessions.some((s) => s.title && s.title !== "New Chat")) {
      return true;
    }
    // If any session has loaded message items.
    if (realSessions.some((s) => Array.isArray(s.messages) && s.messages.length > 0)) {
      return true;
    }
  }
  // If the rendered DOM contains actual user chat bubbles.
  const chatBox = document.getElementById("chat-messages");
  if (chatBox && chatBox.querySelector(".message-wrapper.user")) {
    return true;
  }
  return false;
}

async function profileHasExistingArtifacts(metaOverride) {
  const io = resolveOnboardingIO();
  const z = resolveZotero();
  const dir = z && z.DataDirectory && z.DataDirectory.dir;
  if (!io || !dir || typeof io.stat !== "function") return false;
  const meta = arguments.length > 0
    ? metaOverride
    : ((await readOnboardingStateFile()).meta || onboardingStateMeta);
  const seededAtRaw = meta && meta.seeded_at;
  if (!seededAtRaw) return false;
  const seededMs = new Date(seededAtRaw).getTime();
  if (!Number.isFinite(seededMs)) return false;
  const cutoffMs = seededMs - 1000;
  const sep = String(dir).includes("\\") ? "\\" : "/";
  const candidates = [
    dir + sep + "zocrates-data" + sep + "zocrates_config.json",
    dir + sep + "zocrates-data" + sep + "sessions.db",
  ];
  for (const candidatePath of candidates) {
    try {
      if (!(await io.exists(candidatePath))) continue;
      const info = await io.stat(candidatePath);
      if (!info) continue;
      const mtime = typeof info.lastModified === "number" ? info.lastModified : NaN;
      const btime = (typeof info.creationTime === "number" && info.creationTime > 0) ? info.creationTime : NaN;
      const artifactMs = Number.isFinite(btime) && Number.isFinite(mtime)
        ? Math.min(btime, mtime)
        : (Number.isFinite(mtime) ? mtime : btime);
      if (Number.isFinite(artifactMs) && artifactMs < cutoffMs) {
        return true;
      }
    } catch (e) {}
  }
  return false;
}

function clearOnboardingContainerCards() {
  const onboardingHost = document.getElementById("onboarding-container");
  if (onboardingHost) {
    while (onboardingHost.firstChild) {
      onboardingHost.removeChild(onboardingHost.firstChild);
    }
  }
  for (const id of ["onboarding-wizard-card", "onboarding-process-card"]) {
    const node = document.getElementById(id);
    if (node) {
      try { node.remove(); } catch (e) {}
    }
  }
  _onboardingWizardRefs = null;
  _onboardingProcessCardRefs = null;
}

async function maybeAutoGraduateOnboardingFromSessions() {
  if (onboardingStage === "configure" && hasExistingChatSessions()) {
    onboardingStage = "ready";
    clearOnboardingContainerCards();
    cleanupOnboardingResources();
    await writeOnboardingState("ready");
    setOnboardingStage("ready");
    removeBootstrapProgressCard();
    return true;
  }
  return false;
}

async function initOnboardingStateFlow() {
  // Mount-time read (restart recovery / reload parity). Re-runnable from
  // tests with a stubbed IOUtils + Zotero.DataDirectory.
  _onboardingStatePending = onboardingStateIoAvailable();
  if (!_onboardingStatePending) {
    onboardingStage = null;
    onboardingStateMeta = null;
    return;
  }
  try {
    const result = await readOnboardingStateFile();
    onboardingStateMeta = result.meta;
    if (result.corrupt) {
      onboardingStage = (await onboardingVenvPresent()) ? "configure" : null;
    } else {
      onboardingStage = result.stage;
    }
    let autoGraduated = false;
    if (onboardingStage === "configure") {
      if (hasExistingChatSessions() || (await profileHasExistingArtifacts(onboardingStateMeta))) {
        onboardingStage = "ready";
        await writeOnboardingState("ready");
        autoGraduated = true;
      }
    }
    setOnboardingStage(onboardingStage);
    if (autoGraduated) {
      clearOnboardingContainerCards();
      cleanupOnboardingResources();
      _onboardingStatePending = false;
      if (isBackendReadyForOnboardingResume()) {
        removeBootstrapProgressCard();
      }
    }
    if (_pendingBootstrapDone) {
      _pendingBootstrapDone = false;
      _onboardingStatePending = false;
      handleOnboardingBootstrapDone();
    } else if (onboardingStage === "process" && isBackendReadyForOnboardingResume()) {
      // Restart recovery: the backend is already up, so no fresh done event
      // is coming — resume the PROCESS view (and its stream) directly.
      enterOnboardingProcess();
    }
  } catch (e) {
    console.warn("Zocrates: onboarding state read failed:", e);
    onboardingStage = null;
  } finally {
    _onboardingStatePending = false;
  }
}

function markBootstrapInstallDone() {
  for (const key of BOOTSTRAP_STEP_KEYS) {
    const el = document.getElementById("bootstrap-step-" + key);
    if (el) {
      el.classList.remove("active", "error");
      el.classList.add("done");
    }
  }
  const fill = document.getElementById("bootstrap-progress-fill");
  if (fill) fill.style.width = "100%";
  const errBox = document.getElementById("bootstrap-card-error");
  if (errBox) errBox.classList.add("hidden");
  const nextBtn = document.getElementById("onboarding-next-btn");
  if (nextBtn) nextBtn.disabled = false;
  // Chat stays gated until READY (see handleChatSubmit / the poll guard).
  sendBtn.disabled = true;
  sendBtn.title = "Finish onboarding to start chatting";
}

// ---------------------------------------------------------------------------
// Phase 7 CONFIGURE wizard — preset definitions (source of truth:
// artifacts/plans/model_pricing_tiers_plan.md; do not invent values)
// ---------------------------------------------------------------------------
// Presets are pure UX sugar: they populate wizard fields. ONLY Finish
// persists anything, and Offline is a manual path (no canonical JSON) that
// switches the wizard into local mode.
const ONBOARDING_PRESETS = {
  recommended: {
    name: "Recommended \u2b50",
    description: "Best research quality, zero data retention on conversion; uses paid OpenRouter models.",
    fields: {
      provider: "openrouter_llm",
      model: "openrouter/~z-ai/glm-flash-latest",
      embedMode: "cloud",
      cloudEmbeddingModel: "openrouter/voyageai/voyage-4",
      vlmChoice: "openrouter",
      vlmModel: "openrouter/deepseek/deepseek-v4.1-flash",
      vlmBackupModel: "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
      vlmMaxImagesPerDoc: 100,
    },
    llmConfig: {
      override_agent_models: {
        coordinator: { model: null, thinking_level: "medium" },
        library_search: { model: null, thinking_level: "low" },
        library_synthesizer: { model: "openrouter/google/gemini-3.8-flash", thinking_level: "high" },
        online_search: { model: null, thinking_level: "low" },
        online_synthesizer: { model: "openrouter/google/gemini-3.8-flash", thinking_level: "high" },
      },
      reranker_model: "qwen/qwen3-reranker-8b",
      citation_verification_provider: "openrouter",
      citation_verification_model: "~typesafe/jev-latest",
      enable_predictive_ghost_input: true,
      predictive_prompt_model: "openrouter/~deepseek/deepseek-flash-latest",
      compaction_summarizer_model: "openrouter/~deepseek/deepseek-flash-latest",
      adaptive_compaction_decision_model: "inception/mercury-decide",
    },
  },
  free: {
    name: "Free \u26a0 Experimental",
    description: "\u26a0 Not recommended for daily use: free-model quota limits can drop calls during deep research \u2014 light testing only.",
    warning: true,
    fields: {
      provider: "openrouter_llm",
      model: "nvidia/nemotron-3.5-lightning:free",
      embedMode: "cloud",
      cloudEmbeddingModel: "openrouter/nvidia/nemotron-3-embed-1b:free",
      vlmChoice: "openrouter",
      vlmModel: "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
      vlmBackupModel: "openrouter/nvidia/nemotron-3.5-super-vl:free",
      vlmMaxImagesPerDoc: 3,
    },
    llmConfig: {
      override_agent_models: {
        coordinator: { model: null, thinking_level: "medium" },
        library_search: { model: null, thinking_level: "low" },
        library_synthesizer: { model: "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", thinking_level: "high" },
        online_search: { model: null, thinking_level: "low" },
        online_synthesizer: { model: "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", thinking_level: "high" },
      },
      citation_verification_provider: "local_laya",
      citation_verification_model: "laya",
      citation_verification_enabled: false,
      reranker_model: "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
      enable_predictive_ghost_input: false,
      enable_adaptive_compaction: false,
    },
  },
  offline: {
    name: "Offline / Privacy",
    description: "Everything on-machine; requires Ollama installed and running. Not a preset \u2014 a manual path: you type your local model names.",
    manual: true,
    fields: {
      provider: "ollama",
      model: "",
      embedMode: "local",
      cloudEmbeddingModel: "",
      vlmChoice: "disable",
      vlmModel: "",
      vlmMaxImagesPerDoc: 5,
    },
    llmConfig: {
      citation_verification_provider: "local_laya",
      citation_verification_auto_start_local: true,
      citation_verification_enabled: true,
      enable_adaptive_compaction: false,
      enable_predictive_ghost_input: false,
    },
    layaFlow: true,
  },
};

function mapPresetToWizardFields(presetKey) {
  // Pure preset → wizard-field mapping (incl. Free/Offline flag flips).
  const def = (typeof presetKey === "object" && presetKey !== null) ? presetKey : ONBOARDING_PRESETS[presetKey];
  if (!def) return null;
  return {
    provider: def.fields.provider,
    model: def.fields.model,
    baseUrlVisible: def.fields.provider !== "openrouter_llm",
    embedMode: def.fields.embedMode,
    cloudEmbeddingModel: def.fields.cloudEmbeddingModel,
    cloudEmbeddingRowVisible: def.fields.embedMode === "cloud",
    vlmChoice: def.fields.vlmChoice,
    vlmModel: def.fields.vlmModel,
    vlmBackupModel: def.fields.vlmBackupModel || "",
    vlmMaxImagesPerDoc: (def.fields && def.fields.vlmMaxImagesPerDoc) !== undefined ? def.fields.vlmMaxImagesPerDoc : 100,
    vlmModelRowVisible: def.fields.vlmChoice !== "disable",
    layaRowVisible: Boolean(def.layaFlow),
    warning: Boolean(def.warning),
  };
}

function buildOnboardingFinishPayloads(selection, presetDef) {
  // PURE mapping: wizard selections → the existing write endpoints.
  //   - POST /config (schemas/llm_request.py::ConfigRequest):
  //     provider/model/base_url/key/override_agent_models + feature flags +
  //     profile/email. Evidence-backed accepted keys only.
  //   - POST /rag/model  (EmbeddingModelChangeRequest): mode + cloud slug.
  //   - POST /rag/converter (ConverterChangeRequest): converter + VLM keys.
  // Returns { llmConfig, ragModel, ragConverter, layaFlow }.
  presetDef = presetDef || (selection && ONBOARDING_PRESETS[selection.preset]) || ONBOARDING_PRESETS.recommended;
  const isLocalProvider = selection.provider !== "openrouter_llm";

  const llmConfig = {};
  if (selection.provider) llmConfig.llm_provider = selection.provider;
  if (selection.model) llmConfig.llm_model = selection.model;
  if (isLocalProvider && selection.baseUrl) llmConfig.llm_base_url = selection.baseUrl;
  if (selection.openrouterKey) llmConfig.openrouter_api_key = selection.openrouterKey;
  if (!presetDef.manual) {
    llmConfig.override_agent_models = presetDef.llmConfig.override_agent_models;
  } else {
    // Offline path: reset every role to inherit the local main model — the
    // shipped defaults pin the synthesizers to OpenRouter-hosted Gemini,
    // which contradicts "everything on-machine" (P2-6).
    llmConfig.override_agent_models = {
      coordinator: { model: null, thinking_level: "medium" },
      library_search: { model: null, thinking_level: "low" },
      library_synthesizer: { model: null, thinking_level: "high" },
      online_search: { model: null, thinking_level: "low" },
      online_synthesizer: { model: null, thinking_level: "high" },
    };
  }
  for (const key of [
    "reranker_model",
    "citation_verification_provider",
    "citation_verification_model",
    "citation_verification_auto_start_local",
    "citation_verification_enabled",
    "enable_predictive_ghost_input",
    "enable_adaptive_compaction",
    "predictive_prompt_model",
    "compaction_summarizer_model",
    "adaptive_compaction_decision_model",
  ]) {
    if (presetDef.llmConfig[key] !== undefined) llmConfig[key] = presetDef.llmConfig[key];
  }
  if (selection.profile && selection.profile.trim()) {
    llmConfig.user_research_profile = selection.profile.trim();
  }
  if (selection.email && selection.email.trim()) {
    llmConfig.scholar_polite_email = selection.email.trim();
  }

  const ragModel = { mode: selection.embedMode === "cloud" ? "cloud" : "local" };
  if (ragModel.mode === "cloud" && selection.cloudEmbeddingModel) {
    ragModel.model_name = selection.cloudEmbeddingModel;
  }

  const ragConverter = { document_converter_id: selection.converterId || "markitdown" };
  const vlmChoice = selection.vlmChoice || (presetDef.fields && presetDef.fields.vlmChoice) || "disable";
  if (vlmChoice === "disable") {
    ragConverter.enable_vlm_captioning = false;
  } else if (vlmChoice === "openrouter") {
    ragConverter.enable_vlm_captioning = true;
    ragConverter.vlm_captioning_provider = "openrouter";
    if (selection.vlmModel || (presetDef.fields && presetDef.fields.vlmModel)) {
      ragConverter.vlm_captioning_model = selection.vlmModel || presetDef.fields.vlmModel;
    }
    const backupModel = selection.vlmBackupModel || (presetDef.fields && presetDef.fields.vlmBackupModel) || "";
    if (backupModel) ragConverter.vlm_captioning_backup_model = backupModel;
  } else {
    ragConverter.enable_vlm_captioning = true;
    ragConverter.vlm_captioning_provider = "ollama";
    if (selection.vlmModel) {
      ragConverter.vlm_captioning_local_model = selection.vlmModel;
    }
  }

  const maxImages = (selection && selection.vlmMaxImagesPerDoc !== undefined)
    ? selection.vlmMaxImagesPerDoc
    : (presetDef && presetDef.fields && presetDef.fields.vlmMaxImagesPerDoc !== undefined)
      ? presetDef.fields.vlmMaxImagesPerDoc
      : 100;
  ragConverter.vlm_max_images_per_doc = maxImages;

  return {
    llmConfig,
    ragModel,
    ragConverter,
    layaFlow: Boolean(presetDef.layaFlow) || (selection.provider === "ollama" && llmConfig.citation_verification_provider === "local_laya"),
  };
}

// ---------------------------------------------------------------------------
// Phase 7 CONFIGURE wizard — single scrolling panel (plan D2) with per-section
// helper text. XSS-safe: createElementNS/textContent only, no innerHTML with
// untrusted data; styling rides the shared 5-theme CSS variables.
// ---------------------------------------------------------------------------

function enterOnboardingConfigure() {
  if (document.getElementById("onboarding-wizard-card")) return;
  if (isDetached && (isValidSessionId(currentSessionId) || isValidSessionId(resolveActiveSessionId()))) {
    onboardingStage = "ready";
    removeBootstrapProgressCard();
    return;
  }
  renderOnboardingWizardCard();
}

function renderOnboardingWizardCard() {
  const host = document.getElementById("onboarding-container");
  if (!host) return;
  const oldBootstrap = document.getElementById("bootstrap-progress-card");
  if (oldBootstrap) oldBootstrap.remove();
  const oldProcess = document.getElementById("onboarding-process-card");
  if (oldProcess) oldProcess.remove();
  const oldWizard = document.getElementById("onboarding-wizard-card");
  if (oldWizard) oldWizard.remove();

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "onboarding-card";
  card.id = "onboarding-wizard-card";
  host.appendChild(card);

  const w = {};
  _onboardingWizardRefs = w;

  const el = (parent, tag, cls, text) => {
    const node = document.createElementNS("http://www.w3.org/1999/xhtml", tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  };
  const labeledField = (parent, label, helper) => {
    const field = el(parent, "div", "onboarding-field");
    el(field, "span", "onboarding-field-label", label);
    if (helper) el(field, "div", "onboarding-helper", helper);
    return field;
  };

  el(card, "div", "onboarding-card-title", "Configure Zocrates");
  el(card, "div", "onboarding-card-subtitle",
    "Pick a preset or tune each section below. Untouched sections keep the recommended defaults.");

  const wizard = el(card, "div", "onboarding-wizard");

  // --- Presets (top of the wizard, plan §5.1) ---
  const presetSection = el(wizard, "div", "onboarding-section");
  el(presetSection, "div", "onboarding-section-title", "Presets");
  el(presetSection, "div", "onboarding-helper",
    "Presets pre-fill every field below: nothing is saved until you finish. Pick \u201cOffline\u201d to configure on-machine privacy step by step.");
  const presetCards = el(presetSection, "div", "onboarding-preset-cards");
  for (const key of ["recommended", "free", "offline"]) {
    const def = ONBOARDING_PRESETS[key];
    const cardBtn = el(presetCards, "button", "onboarding-preset-card");
    cardBtn.type = "button";
    cardBtn.dataset.presetKey = key;
    el(cardBtn, "span", "onboarding-preset-name", def.name);
    el(cardBtn, "span", "onboarding-preset-desc" + (def.warning ? " warning" : ""), def.description);
    cardBtn.addEventListener("click", () => applyOnboardingPreset(key));
    (w.presetCards = w.presetCards || []).push(cardBtn);
  }
  w.presetWarning = el(presetSection, "div", "onboarding-helper warning");
  w.presetWarning.style.display = "none";

  // --- AI Engine ---
  const aiSection = el(wizard, "div", "onboarding-section");
  el(aiSection, "div", "onboarding-section-title", "AI Engine");
  el(aiSection, "div", "onboarding-helper",
    "OpenRouter is the default engine. An API key is required even for :free models. Choose Ollama/vLLM/OpenAI-compatible to run a local server instead (its base URL is needed then).");
  const providerField = labeledField(aiSection, "Provider");
  const providerSelect = el(providerField, "select", "onboarding-select");
  for (const [val, label] of [
    ["openrouter_llm", "OpenRouter"],
    ["ollama", "Ollama (local)"],
    ["vllm", "vLLM (local)"],
    ["openai_compatible", "OpenAI-compatible endpoint"],
  ]) {
    const opt = el(providerSelect, "option", null, label);
    opt.value = val;
  }
  providerSelect.addEventListener("change", () => {
    if (w.baseUrlRow) w.baseUrlRow.style.display = providerSelect.value === "openrouter_llm" ? "none" : "flex";
  });
  w.providerSelect = providerSelect;

  const modelField = labeledField(aiSection, "Main model",
    "Model slug sent to the provider (OpenRouter presets use openrouter-prefixed slugs; local providers take their own model names).");
  const modelInput = el(modelField, "input", "onboarding-input");
  modelInput.type = "text";
  modelInput.placeholder = "openrouter/~z-ai/glm-flash-latest";
  w.modelInput = modelInput;

  const baseUrlRow = labeledField(aiSection, "Base URL",
    "e.g. http://localhost:11434 for Ollama.");
  const baseUrlInput = el(baseUrlRow, "input", "onboarding-input");
  baseUrlInput.type = "text";
  baseUrlInput.placeholder = "http://localhost:11434";
  baseUrlRow.style.display = "none";
  w.baseUrlRow = baseUrlRow;
  w.baseUrlInput = baseUrlInput;

  const keyField = labeledField(aiSection, "OpenRouter API key");
  const keyHelper = el(keyField, "div", "onboarding-helper");
  keyHelper.appendChild(document.createTextNode(
    "Stored securely in Zotero. Required for all OpenRouter models, including :free ones. Create an account at "
  ));
  const openrouterLink = el(keyHelper, "a", "onboarding-link", "https://openrouter.ai/");
  openrouterLink.href = "https://openrouter.ai/";
  openrouterLink.target = "_blank";
  keyHelper.appendChild(document.createTextNode(" and create an API key."));
  const keyInput = el(keyField, "input", "onboarding-input");
  keyInput.type = "password";
  keyInput.placeholder = "sk-or-…";
  w.openrouterKey = keyInput;

  const testRow = el(aiSection, "div", "onboarding-inline-row");
  const testBtn = el(testRow, "button", "onboarding-secondary-btn", "Test connection");
  testBtn.type = "button";
  testBtn.addEventListener("click", () => testOnboardingConnection(w));
  w.testBtn = testBtn;
  w.testStatus = el(aiSection, "div", "onboarding-test-status");

  // --- Embeddings ---
  const embSection = el(wizard, "div", "onboarding-section");
  el(embSection, "div", "onboarding-section-title", "Embeddings");
  el(embSection, "div", "onboarding-helper",
    "\u26a0 This choice is cheapest to make now: switching local \u2194 cloud later requires re-indexing your library. Local = private + slower first index; Cloud = faster, data goes to the embedding provider.");
  const embMode = el(embSection, "div", "onboarding-embed-mode");
  const localLabel = el(embMode, "label");
  const embedLocal = el(localLabel, "input");
  embedLocal.type = "radio";
  embedLocal.name = "onboarding-embed-mode";
  embedLocal.value = "local";
  localLabel.appendChild(document.createTextNode(" Local (Jina v5 \u2014 private)"));
  const cloudLabel = el(embMode, "label");
  const embedCloud = el(cloudLabel, "input");
  embedCloud.type = "radio";
  embedCloud.name = "onboarding-embed-mode";
  embedCloud.value = "cloud";
  cloudLabel.appendChild(document.createTextNode(" Cloud (OpenRouter-hosted)"));
  const cloudRow = labeledField(embSection, "Cloud embedding model",
    "OpenRouter embedding slug (the OpenRouter key above is reused). The vector dimension is auto-resolved per model.");
  const cloudModelInput = el(cloudRow, "input", "onboarding-input");
  cloudModelInput.type = "text";
  cloudModelInput.placeholder = "openrouter/voyageai/voyage-4";
  w.cloudEmbeddingRow = cloudRow;
  w.cloudEmbeddingModel = cloudModelInput;
  w.dimensionLine = el(embSection, "div", "onboarding-hardware-line");
  const syncEmbedModeUi = () => {
    cloudRow.style.display = embedCloud.checked ? "flex" : "none";
  };
  embedLocal.addEventListener("change", syncEmbedModeUi);
  embedCloud.addEventListener("change", syncEmbedModeUi);
  w.embedLocal = embedLocal;
  w.embedCloud = embedCloud;

  // --- VLM captioning ---
  const vlmSection = el(wizard, "div", "onboarding-section");
  el(vlmSection, "div", "onboarding-section-title", "Figure captions (VLM)");
  el(vlmSection, "div", "onboarding-helper",
    "Vision captioning describes figures inside PDFs so they become searchable. Fully optional and fail-open: captioning problems never block indexing.");
  const vlmChoices = el(vlmSection, "div", "onboarding-embed-mode");
  const vlmDisableLabel = el(vlmChoices, "label");
  const vlmDisable = el(vlmDisableLabel, "input");
  vlmDisable.type = "radio";
  vlmDisable.name = "onboarding-vlm";
  vlmDisable.value = "disable";
  vlmDisableLabel.appendChild(document.createTextNode(" Disable"));
  const vlmOrLabel = el(vlmChoices, "label");
  const vlmOpenrouter = el(vlmOrLabel, "input");
  vlmOpenrouter.type = "radio";
  vlmOpenrouter.name = "onboarding-vlm";
  vlmOpenrouter.value = "openrouter";
  vlmOrLabel.appendChild(document.createTextNode(" OpenRouter vision model"));
  const vlmOlLabel = el(vlmChoices, "label");
  const vlmOllama = el(vlmOlLabel, "input");
  vlmOllama.type = "radio";
  vlmOllama.name = "onboarding-vlm";
  vlmOllama.value = "ollama";
  vlmOlLabel.appendChild(document.createTextNode(" Ollama vision model"));
  const vlmModelRow = labeledField(vlmSection, "Vision model",
    "OpenRouter slug (e.g. openrouter/deepseek/deepseek-v4.1-flash) or your Ollama vision model name.");
  const vlmModelInput = el(vlmModelRow, "input", "onboarding-input");
  vlmModelInput.type = "text";
  w.vlmModelRow = vlmModelRow;
  w.vlmModel = vlmModelInput;
  const syncVlmUi = () => {
    vlmModelRow.style.display = vlmDisable.checked ? "none" : "flex";
    if (vlmOllama.checked) {
      w.vlmModelLabel.textContent = "Ollama vision model";
    } else {
      w.vlmModelLabel.textContent = "OpenRouter vision model";
    }
  };
  w.vlmModelLabel = vlmModelRow.querySelector("span.onboarding-field-label");
  vlmDisable.addEventListener("change", syncVlmUi);
  vlmOpenrouter.addEventListener("change", syncVlmUi);
  vlmOllama.addEventListener("change", syncVlmUi);
  w.vlmDisable = vlmDisable;
  w.vlmOpenrouter = vlmOpenrouter;
  w.vlmOllama = vlmOllama;

  // --- Converter ---
  const convSection = el(wizard, "div", "onboarding-section");
  el(convSection, "div", "onboarding-section-title", "Document conversion");
  w.hardwareLine = el(convSection, "div", "onboarding-hardware-line", "Detecting hardware\u2026");
  el(convSection, "div", "onboarding-helper",
    "markitdown is fast with lower quality; Docling runs locally with higher quality and a heavier first index. Datalab (cloud OCR, highest quality) can be opted into later in Preferences.");
  const convChoices = el(convSection, "div", "onboarding-embed-mode");
  const mdLabel = el(convChoices, "label");
  const converterMd = el(mdLabel, "input");
  converterMd.type = "radio";
  converterMd.name = "onboarding-converter";
  converterMd.value = "markitdown";
  mdLabel.appendChild(document.createTextNode(" markitdown (fast)"));
  const dlLabel = el(convChoices, "label");
  const converterDocling = el(dlLabel, "input");
  converterDocling.type = "radio";
  converterDocling.name = "onboarding-converter";
  converterDocling.value = "docling_local";
  dlLabel.appendChild(document.createTextNode(" Docling (local, higher quality)"));
  w.converterMd = converterMd;
  w.converterDocling = converterDocling;

  // --- Research profile ---
  const profileSection = el(wizard, "div", "onboarding-section");
  el(profileSection, "div", "onboarding-section-title", "Research profile (optional)");
  el(profileSection, "div", "onboarding-helper",
    "Describe your field and how you want answers shaped (max 1,000 characters). Skippable.");
  const profileField = el(profileSection, "div", "onboarding-field");
  const profileArea = el(profileField, "textarea", "onboarding-textarea");
  profileArea.maxLength = 1000;
  profileArea.rows = 3;
  profileArea.placeholder = "e.g. PhD student in computational biology studying protein folding; prefer concise answers.";
  w.profile = profileArea;
  w.profileCounter = el(profileField, "div", "onboarding-profile-counter", "0 / 1000");
  profileArea.addEventListener("input", () => {
    w.profileCounter.textContent = `${profileArea.value.length} / 1000`;
  });

  // --- Scholar email ---
  const emailSection = el(wizard, "div", "onboarding-section");
  el(emailSection, "div", "onboarding-section-title", "Scholar email (optional)");
  el(emailSection, "div", "onboarding-helper",
    "Contact email used for the OpenAlex polite pool (higher scholarly API rate limits). Purely optional.");
  const emailField = el(emailSection, "div", "onboarding-field");
  const emailInput = el(emailField, "input", "onboarding-input");
  emailInput.type = "email";
  emailInput.placeholder = "you@university.edu";
  w.email = emailInput;

  // --- Offline: local decision tool (Laya) note ---
  const layaRow = el(wizard, "div", "onboarding-section");
  el(layaRow, "div", "onboarding-section-title", "Local citation verification (Laya)");
  el(layaRow, "div", "onboarding-helper",
    "Offline mode verifies citations with the local Laya decision tool. On Finish, Zocrates checks its status and installs/starts it if needed \u2014 entirely fail-open.");
  w.layaStatus = el(layaRow, "div", "onboarding-test-status");
  w.layaRow = layaRow;
  layaRow.style.display = "none";

  // --- Footer ---
  const footer = el(card, "div", "onboarding-wizard-footer");
  w.skipBtn = el(footer, "button", "onboarding-secondary-btn", "Skip \u2014 use defaults");
  w.skipBtn.type = "button";
  const saveBtn = el(footer, "button", "onboarding-primary-btn", "Save & start indexing");
  saveBtn.type = "button";
  saveBtn.disabled = false;
  w.saveBtn = saveBtn;
  w.status = el(card, "div", "onboarding-wizard-status");
  saveBtn.addEventListener("click", () => finishOnboardingConfigure(false));
  w.skipBtn.addEventListener("click", () => finishOnboardingConfigure(true));

  // Recommended pre-selected, per plan §5.1.
  applyOnboardingPreset("recommended");
  loadOnboardingWizardDefaults(w);
}

function applyOnboardingPreset(key) {
  const mapping = mapPresetToWizardFields(key);
  const w = _onboardingWizardRefs;
  if (!mapping || !w) return;
  w.activePresetKey = key;
  for (const cardBtn of w.presetCards || []) {
    cardBtn.classList.toggle("selected", cardBtn.dataset.presetKey === key);
  }
  if (w.providerSelect) w.providerSelect.value = mapping.provider;
  if (w.modelInput) w.modelInput.value = mapping.model || "";
  if (w.baseUrlRow) w.baseUrlRow.style.display = mapping.baseUrlVisible ? "flex" : "none";
  if (mapping.embedMode === "cloud") {
    if (w.embedCloud) w.embedCloud.checked = true;
  } else if (w.embedLocal) {
    w.embedLocal.checked = true;
  }
  if (w.cloudEmbeddingModel) w.cloudEmbeddingModel.value = mapping.cloudEmbeddingModel || "";
  if (w.cloudEmbeddingRow) w.cloudEmbeddingRow.style.display = mapping.cloudEmbeddingRowVisible ? "flex" : "none";
  if (w.vlmDisable && w.vlmOpenrouter && w.vlmOllama) {
    if (mapping.vlmChoice === "openrouter") w.vlmOpenrouter.checked = true;
    else if (mapping.vlmChoice === "ollama") w.vlmOllama.checked = true;
    else w.vlmDisable.checked = true;
  }
  if (w.vlmModel) w.vlmModel.value = mapping.vlmModel || "";
  if (w.vlmModelRow) w.vlmModelRow.style.display = mapping.vlmModelRowVisible ? "flex" : "none";
  if (w.layaRow) w.layaRow.style.display = mapping.layaRowVisible ? "flex" : "none";
  if (w.presetWarning) {
    if (mapping.warning) {
      w.presetWarning.textContent = "\u26a0 Free tier conserves quota for lighter use: predictive ghost input and adaptive compaction are disabled on this tier.";
      w.presetWarning.style.display = "block";
    } else {
      w.presetWarning.style.display = "none";
    }
  }
}

function setOnboardingStatus(node, text, isFail) {
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("fail", Boolean(isFail));
  node.classList.toggle("ok", !isFail && /success|connected/i.test(text));
}

async function testOnboardingConnection(w) {
  if (!w) return;
  setOnboardingStatus(w.testStatus, "Testing\u2026");
  try {
    const payload = {
      llm_provider: w.providerSelect ? w.providerSelect.value : undefined,
      llm_model: w.modelInput ? w.modelInput.value.trim() || undefined : undefined,
      llm_base_url: w.baseUrlInput && w.baseUrlInput.value.trim() ? w.baseUrlInput.value.trim() : undefined,
      openrouter_api_key: w.openrouterKey && w.openrouterKey.value.trim() ? w.openrouterKey.value.trim() : undefined,
    };
    const res = await fetch(`${BACKEND_URL}/llm/test`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    const ok = Boolean(res.ok && data && (data.status === "ok" || data.status === "success" || data.ok === true || data.connected === true));
    let text = "";
    if (ok) {
      const rawMsg = data && data.message ? String(data.message) : "";
      text = rawMsg ? (/success|connected/i.test(rawMsg) ? rawMsg : `Connection successful (${rawMsg})`) : "Connection successful";
    } else {
      let detail = data && (data.message || data.detail);
      if (detail && typeof detail !== "string") {
        try { detail = JSON.stringify(detail); } catch (_) { detail = String(detail); }
      }
      text = detail ? ("Test failed: " + detail) : ("Test failed (" + res.status + ")");
    }
    setOnboardingStatus(w.testStatus, text, !ok);
  } catch (e) {
    setOnboardingStatus(w.testStatus, "Test failed: " + (e && e.message ? e.message : e), true);
  }
}

async function loadOnboardingWizardDefaults(w) {
  // Hardware summary + read-only embedding dimension + converter preselect.
  try {
    const res = await fetch(`${BACKEND_URL}/status`, {
      headers: getAuthHeaders(),
    });
    if (!res.ok) return;
    const data = await res.json();
    const hw = data && data.hardware || {};
    const emb = data && data.embedding_info || {};
    const hwBits = [];
    if (hw.ram_gb !== undefined) hwBits.push(`${Math.round(hw.ram_gb)} GB RAM`);
    if (hw.gpu_name) hwBits.push(hw.gpu_name);
    if (w.dimensionLine) {
      w.dimensionLine.textContent = `Active embedding: ${emb.active_model || emb.mode || "local"} · dimension ${emb.dimension != null ? emb.dimension : "(auto on first embed)"}`;
    }
    if (w.hardwareLine) {
      w.hardwareLine.textContent = hwBits.length ? `Detected: ${hwBits.join(" \u00b7 ")}` : "Hardware detection unavailable.";
    }
    // /status nests the docling assessment under hardware (fast_api_app.py),
    // matching the pre-existing reader (hw.docling_assessment).
    const assessment = (data && data.hardware && data.hardware.docling_assessment) || null;
    if (assessment && w.converterDocling && w.converterMd) {
      if (assessment.recommended_mode) {
        (assessment.is_gpu_accelerated ? w.converterDocling : w.converterMd).checked = true;
      }
      if (assessment.reason) {
        w.hardwareLine.textContent = `${w.hardwareLine.textContent} — ${assessment.reason}`;
      }
    }
  } catch (e) {
    console.warn("Zocrates: onboarding wizard defaults fetch failed:", e);
  }
}

async function finishOnboardingConfigure(skip) {
  const w = _onboardingWizardRefs;
  const status = w && w.status;
  try {
    if (w) {
      if (w.saveBtn) w.saveBtn.disabled = true;
      if (w.skipBtn) w.skipBtn.disabled = true;
    }
    if (!skip) {
      const selection = collectOnboardingSelections();
      if (selection.openrouterKey) {
        saveStoredApiKey("openrouter_api_key", selection.openrouterKey);
      }
      const payloads = buildOnboardingFinishPayloads(selection);
      if (status) setOnboardingStatus(status, "Saving AI engine settings\u2026");
      const llmRes = await fetch(`${BACKEND_URL}/config`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payloads.llmConfig),
      });
      if (!llmRes.ok) throw new Error(`AI engine settings failed (${llmRes.status})`);
      if (status) setOnboardingStatus(status, "Switching embeddings\u2026");
      const ragRes = await fetch(`${BACKEND_URL}/rag/model`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payloads.ragModel),
      });
      if (!ragRes.ok) throw new Error(`Embedding switch failed (${ragRes.status})`);
      if (status) setOnboardingStatus(status, "Setting converter & captions\u2026");
      const convRes = await fetch(`${BACKEND_URL}/rag/converter`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payloads.ragConverter),
      });
      if (!convRes.ok) throw new Error(`Converter update failed (${convRes.status})`);
      if (payloads.layaFlow) {
        if (status) setOnboardingStatus(status, "Setting up local citation verification (Laya)\u2026");
        await ensureOnboardingLayaReady(status);
      }
    }
    const extra = {};
    if (!skip && w && w.activePresetKey) extra.preset_applied = w.activePresetKey;
    await writeOnboardingState("process", extra);
    setOnboardingStage("process");
    enterOnboardingProcess();
  } catch (e) {
    if (status) setOnboardingStatus(status, "Save failed: " + (e && e.message ? e.message : e), true);
    if (w) {
      if (w.saveBtn) w.saveBtn.disabled = false;
      if (w.skipBtn) w.skipBtn.disabled = false;
    }
  }
}

async function ensureOnboardingLayaReady(status) {
  // Offline path: status → install → start via the existing decision-tool
  // endpoints. Fail-open: any error only surfaces in the wizard status line.
  try {
    // The status route is registered with @app.get (fast_api_app.py) — POST
    // would 405 and silently skip the install branch (P1-4).
    const stRes = await fetch(`${BACKEND_URL}/llm/decision_tool/status`, {
      headers: getAuthHeaders(),
    });
    const st = stRes.ok ? await stRes.json() : null;
    if (st && st.server_running) {
      if (status) setOnboardingStatus(status, "Local decision tool already running.");
      return true;
    }
    if (st && !st.tool_installed) {
      if (status) setOnboardingStatus(status, "Installing local decision tool (one-time download)\u2026");
      const instRes = await fetch(`${BACKEND_URL}/llm/decision_tool/install`, { method: "POST", headers: getAuthHeaders() });
      if (!instRes.ok) console.warn("Zocrates: Laya install failed (" + instRes.status + "); continuing (fail-open)");
    }
    const startRes = await fetch(`${BACKEND_URL}/llm/decision_tool/start`, { method: "POST", headers: getAuthHeaders() });
    if (!startRes.ok) console.warn("Zocrates: Laya start failed (" + startRes.status + "); continuing (fail-open)");
    return true;
  } catch (e) {
    console.warn("Zocrates: Laya setup failed (fail-open):", e);
    return false;
  }
}

function collectOnboardingSelections() {
  const w = _onboardingWizardRefs || {};
  const vlmChoice = w.vlmDisable && w.vlmDisable.checked ? "disable"
    : (w.vlmOllama && w.vlmOllama.checked ? "ollama" : "openrouter");
  return {
    preset: w.activePresetKey || null,
    provider: w.providerSelect ? w.providerSelect.value : "openrouter_llm",
    model: w.modelInput ? w.modelInput.value.trim() : "",
    baseUrl: w.baseUrlInput ? w.baseUrlInput.value.trim() : "",
    openrouterKey: w.openrouterKey ? w.openrouterKey.value.trim() : "",
    embedMode: w.embedCloud && w.embedCloud.checked ? "cloud" : "local",
    cloudEmbeddingModel: w.cloudEmbeddingModel ? w.cloudEmbeddingModel.value.trim() : "",
    converterId: w.converterDocling && w.converterDocling.checked ? "docling_local" : "markitdown",
    vlmChoice,
    vlmModel: w.vlmModel ? w.vlmModel.value.trim() : "",
    vlmMaxImagesPerDoc: (w.vlmMaxImagesInput ? (parseInt(w.vlmMaxImagesInput.value, 10) || undefined) : undefined),
    profile: w.profile ? w.profile.value : "",
    email: w.email ? w.email.value.trim() : "",
  };
}

// ---------------------------------------------------------------------------
// Phase 7 PROCESS stage — first full-library conversion (plan §6)
// ---------------------------------------------------------------------------
// Streams POST /sync/stream (SSE over a fetch POST, same wire pattern as the
// batch converter: "event: <name>" + "data: <json>"). Terminal events:
// done | aborted | auto_aborted | error. Chat unlocks ONLY at READY — the
// gate flip lives in removeBootstrapProgressCard() (gate composition D1),
// which stays a no-op while the stage is configure/process.

const ONBOARDING_SYNC_TERMINAL_EVENTS = ["done", "aborted", "auto_aborted", "error"];

let _onboardingProcessCardRefs = null; // live PROCESS card element refs
let _onboardingAbortIntent = null;     // "adjust" | "stop" | null (user clicked)
let _onboardingAbortFallbackTimer = null; // follower-window self-finalize timer

function formatOnboardingElapsed(ms) {
  const totalSec = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  if (mins <= 0) return secs + "s";
  return mins + "m " + (secs < 10 ? "0" : "") + secs + "s";
}

function subscribeOnboardingStageBus() {
  // Multi-window broadcast (plan §3): every sidebar iframe replays the last
  // stage on mount and follows stage writes from other windows. Writes stay
  // single-owner; this listener only applies foreign stages.
  try {
    const zot = resolveZotero();
    if (!zot || !zot.Zocrates || typeof zot.Zocrates.addOnboardingStageListener !== "function") return;
    const unsub = zot.Zocrates.addOnboardingStageListener((stage) => {
      if (stage === getEffectiveOnboardingStage()) return; // this window wrote it
      handleOnboardingStageEvent(stage);
    });
    window.addEventListener("unload", () => { try { unsub(); } catch (e) {} });
    const snapshot = zot.Zocrates.getOnboardingStageState && zot.Zocrates.getOnboardingStageState();
    if (snapshot && snapshot !== getEffectiveOnboardingStage()) handleOnboardingStageEvent(snapshot);
  } catch (e) {
    console.warn("Zocrates: Error subscribing to onboarding stage bus:", e);
  }
}

function handleOnboardingStageEvent(stage) {
  if (stage === "ready") {
    // Terminal broadcast from another window (or a late seed replay): tear
    // the local flow down; the chat gate lifts via removeBootstrapProgressCard().
    if (_onboardingAbortFallbackTimer) {
      try { clearTimeout(_onboardingAbortFallbackTimer); } catch (e) {}
      _onboardingAbortFallbackTimer = null;
    }
    onboardingStage = "ready";
    onboardingStateMeta = null;
    if (_onboardingStreamAbort) { try { _onboardingStreamAbort.abort(); } catch (e) {} }
    _onboardingStreamAbort = null;
    _onboardingStreamActive = false;
    _onboardingFinished = true;
    const wiz = document.getElementById("onboarding-wizard-card");
    if (wiz) wiz.remove();
    const proc = document.getElementById("onboarding-process-card");
    if (proc) proc.remove();
    removeBootstrapProgressCard();
    return;
  }
  if (stage === "configure" || stage === "process") {
    if (onboardingStage === "ready") return; // READY is terminal; do not downgrade
    if (_onboardingAbortFallbackTimer) {
      try { clearTimeout(_onboardingAbortFallbackTimer); } catch (e) {}
      _onboardingAbortFallbackTimer = null;
    }
    onboardingStage = stage;
    if (stage === "process") {
      enterOnboardingProcess();
    } else if (isBackendReadyForOnboardingResume() && !document.getElementById("bootstrap-progress-card")) {
      // Only auto-enter the wizard once the backend is actually usable AND no
      // install card is showing. The seed broadcast arrives mid-provisioning
      // (before spawn/handshake/done) — entering here would destroy the
      // INSTALL progress card and fire /status into a dead port. The done
      // path (markBootstrapInstallDone) activates "Next" instead; a mid-
      // CONFIGURE resume (warm-start, no card) opens the wizard directly.
      enterOnboardingConfigure();
    }
    return;
  }
  // Unrecognized or null stage: ignore — do not eject an active flow.
}

function enterOnboardingProcess() {
  if (isDetached && (isValidSessionId(currentSessionId) || isValidSessionId(resolveActiveSessionId()))) {
    onboardingStage = "ready";
    removeBootstrapProgressCard();
    return;
  }
  if (getEffectiveOnboardingStage() !== "process") return;
  if (_onboardingStreamActive) return; // already streaming (idempotent re-entry)
  renderOnboardingProcessCard();
  openOnboardingSyncStream();
}

function renderOnboardingProcessCard() {
  const host = document.getElementById("onboarding-container");
  if (!host) return;
  const oldWizard = document.getElementById("onboarding-wizard-card");
  if (oldWizard) oldWizard.remove();
  const oldCard = document.getElementById("onboarding-process-card");
  if (oldCard) oldCard.remove();

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "onboarding-card";
  card.id = "onboarding-process-card";
  host.appendChild(card);

  const el = (parent, tag, cls, text) => {
    const node = document.createElementNS("http://www.w3.org/1999/xhtml", tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  };

  el(card, "div", "onboarding-card-title", "Indexing your library");
  el(card, "div", "onboarding-card-subtitle",
    "First-time conversion of every item in your library for local search. " +
    "This runs in the background \u2014 you can stop and adjust settings at any time.");

  const progress = el(card, "div", "onboarding-process-progress");
  const bar = el(progress, "div", "onboarding-process-bar");
  const fill = el(bar, "div", "onboarding-process-fill");
  const countsLine = el(progress, "div", "onboarding-process-line", "Preparing\u2026");
  const titleLine = el(progress, "div", "onboarding-process-current");
  titleLine.style.display = "none";
  const attachedLine = el(progress, "div", "onboarding-process-line onboarding-process-attached");
  attachedLine.style.display = "none";

  const warning = el(card, "div", "onboarding-process-line onboarding-process-warning");
  warning.style.display = "none";
  const errorBox = el(card, "div", "onboarding-process-error");
  errorBox.style.display = "none";
  const errorText = el(errorBox, "div", "onboarding-process-error-text");
  const retryRow = el(errorBox, "div", "onboarding-abort-row");
  const retryBtn = el(retryRow, "button", "onboarding-secondary-btn", "Retry indexing");
  retryBtn.type = "button";
  retryBtn.addEventListener("click", () => {
    if (warning) warning.style.display = "none";
    if (errorBox) errorBox.style.display = "none";
    openOnboardingSyncStream();
  });

  const abortRow = el(card, "div", "onboarding-abort-row");
  const adjustBtn = el(abortRow, "button", "onboarding-secondary-btn",
    "Stop & adjust settings");
  adjustBtn.type = "button";
  adjustBtn.title = "Stop the conversion and go back to the settings wizard. Already-indexed items are skipped on the next run.";
  adjustBtn.addEventListener("click", () => requestOnboardingAbort("adjust"));
  const stopBtn = el(abortRow, "button", "onboarding-abort-btn danger", "Stop indexing");
  stopBtn.type = "button";
  stopBtn.title = "Keep the partial index and finish onboarding. You can resume any time via the Sync entry point.";
  stopBtn.addEventListener("click", () => requestOnboardingAbort("stop"));

  _onboardingProcessCardRefs = {
    card,
    fill,
    countsLine,
    titleLine,
    attachedLine,
    warning,
    errorBox,
    errorText,
    retryRow,
    retryBtn,
    abortRow,
    adjustBtn,
    stopBtn,
  };
}

function updateOnboardingProcessProgress(data) {
  const refs = _onboardingProcessCardRefs;
  if (!refs) return;
  const processed = Number(data && data.processed) || 0;
  const skipped = Number(data && data.skipped) || 0;
  const errors = Number(data && data.errors) || 0;
  const total = Number(data && data.total) || 0;
  const elapsedMs = Number(data && data.elapsed_ms) || 0;
  const attempted = processed + skipped + errors;
  if (refs.fill) {
    refs.fill.style.width = total > 0
      ? Math.min(100, Math.round((attempted / total) * 100)) + "%"
      : "8%"; // total unknown yet: indeterminate placeholder
  }
  if (refs.countsLine) {
    const elapsed = elapsedMs > 0 ? formatOnboardingElapsed(elapsedMs) : "";
    refs.countsLine.textContent = (processed + skipped) + " / " + (total > 0 ? String(total) : "\u2026")
      + " items \u00b7 " + errors + " failed" + (elapsed ? " \u00b7 " + elapsed : "");
  }
  if (refs.titleLine) {
    const title = data && data.current_title ? String(data.current_title) : "";
    refs.titleLine.textContent = title ? "Now indexing: " + title : "";
    refs.titleLine.style.display = title ? "block" : "none";
  }
  if (refs.attachedLine && data && typeof data.attached === "boolean") {
    refs.attachedLine.style.display = data.attached ? "block" : "none";
    if (data.attached) {
      refs.attachedLine.textContent = "Another Zocrates window is already running this conversion \u2014 showing its live progress.";
    }
  }
}

function showOnboardingProcessError(message) {
  const refs = _onboardingProcessCardRefs;
  if (!refs) return;
  if (refs.errorText) refs.errorText.textContent = String(message || "Indexing failed.");
  if (refs.errorBox) refs.errorBox.style.display = "block";
  if (refs.warning) refs.warning.style.display = "none";
}

function showOnboardingProcessWarning(message) {
  const refs = _onboardingProcessCardRefs;
  if (!refs || !refs.warning) return;
  refs.warning.textContent = String(message || "");
  refs.warning.style.display = "block";
  if (refs.errorBox) refs.errorBox.style.display = "none";
}

function _currentOnboardingSyncGeneration() {
  // Shared (multi-window) generation counter bumped by every
  // openOnboardingSyncStream call. Used by the follower fallback timer to
  // detect that a NEW indexing run started anywhere (retry, restart recovery
  // in a fresh window) — a stale timer must never tear down a newer run.
  try {
    const z = resolveZotero();
    return (z && z.Zocrates && z.Zocrates._onboardingSyncGeneration) || 0;
  } catch (e) {
    return 0;
  }
}

function _finalizeFollowerAbortFallback(armedGeneration, data) {
  // 20s fallback body for the single-owner abort: self-finalize as
  // aborted_stop ONLY if the initiating window never resolved the flow AND
  // no newer run started anywhere (generation guard, P2 finding 2).
  if (_currentOnboardingSyncGeneration() !== armedGeneration) return;
  if (getEffectiveOnboardingStage() === "process" && _onboardingProcessCardRefs) {
    _onboardingFinished = true;
    finishOnboardingProcessing("aborted_stop", data);
  }
}

async function requestOnboardingAbort(intent) {
  // Two-path abort (plan §6.3). "adjust" returns to the wizard (converter
  // switchable); "stop" keeps the partial index and finishes onboarding.
  // Fail-open: the endpoint returns no_active_job when nothing is running —
  // the flow then finalizes directly from the local state.
  _onboardingAbortIntent = intent;
  const refs = _onboardingProcessCardRefs;
  if (refs) {
    if (refs.adjustBtn) refs.adjustBtn.disabled = true;
    if (refs.stopBtn) refs.stopBtn.disabled = true;
    if (refs.errorBox) refs.errorBox.style.display = "none";
    if (refs.countsLine) {
      refs.countsLine.textContent = intent === "adjust"
        ? "Stopping\u2026 returning to settings"
        : "Stopping\u2026 finishing onboarding with the partial index";
    }
  }
  try {
    await fetch(`${BACKEND_URL}/sync/abort`, { method: "POST", headers: getAuthHeaders() });
  } catch (e) {
    // Fail-open: a network error here must not wedge the flow; the stream's
    // own terminal event (or the no-stream fallback below) finalizes.
  }
  if (!_onboardingStreamActive) {
    // No live stream (e.g. the error/auto-abort terminal state where the
    // one-shot _onboardingFinished guard is already set): finalize DIRECTLY
    // instead of routing through handleOnboardingSyncEvent — its terminal
    // guard would early-return and leave both abort buttons disabled forever
    // in exactly the failure states where abort matters (P1-3).
    // Race guard: the backend may broadcast the "aborted" terminal to the
    // stream subscriber CONCURRENTLY with the POST response — the terminal
    // path then already consumed _onboardingAbortIntent and finalized with
    // the correct outcome. Re-finalizing here would map the consumed (null)
    // intent to aborted_stop and wipe the wizard the user asked for.
    if (_onboardingAbortIntent === null) return;
    const finalIntent = _onboardingAbortIntent;
    _onboardingAbortIntent = null;
    _onboardingFinished = true;
    await finishOnboardingProcessing(finalIntent === "adjust" ? "aborted_adjust" : "aborted_stop", null);
  }
}

async function openOnboardingSyncStream() {
  if (_onboardingStreamActive) return;
  _onboardingFinished = false;
  _onboardingAbortIntent = null;
  // Hardening: a leftover follower fallback timer must never tear down a NEW
  // run started by this open (retry / restart recovery) — clear it here and
  // bump the shared generation so OTHER windows' stale timers skip too.
  if (_onboardingAbortFallbackTimer) {
    try { clearTimeout(_onboardingAbortFallbackTimer); } catch (e) {}
    _onboardingAbortFallbackTimer = null;
  }
  try {
    const z = resolveZotero();
    if (z && z.Zocrates) {
      z.Zocrates._onboardingSyncGeneration = ((z.Zocrates._onboardingSyncGeneration || 0) + 1);
    }
  } catch (e) {}
  const abort = (typeof AbortController !== "undefined") ? new AbortController() : null;
  _onboardingStreamAbort = abort;
  _onboardingStreamActive = true;
  try {
    const res = await fetch(`${BACKEND_URL}/sync/stream`, {
      method: "POST",
      headers: getAuthHeaders({ "Accept": "text/event-stream" }),
      signal: abort ? abort.signal : undefined,
    });
    if (!res.ok || !res.body || typeof res.body.getReader !== "function") {
      throw new Error(`Indexing endpoint returned HTTP ${res.status}`);
    }
    await readOnboardingSyncStream(res);
  } catch (err) {
    if (err && (err.name === "AbortError" || err.name === "NS_ERROR_ABORT")) {
      // Reader abort: page unload, stage-bus teardown, or session reload —
      // the backend job keeps running for other subscribers.
      _onboardingStreamActive = false;
      return;
    }
    _onboardingStreamActive = false;
    if (!_onboardingFinished) {
      handleOnboardingSyncEvent("error", { error: (err && err.message) ? String(err.message) : "Indexing request failed." });
    }
  }
}

async function readOnboardingSyncStream(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = null;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const rawLine of lines) {
        const line = rawLine.replace(/\r$/, "");
        if (line.startsWith("event:")) {
          currentEvent = line.substring(6).trim() || "message";
          continue;
        }
        if (!line.startsWith("data:")) continue;
        const dataStr = line.substring(5).trim();
        let data = null;
        try { data = dataStr ? JSON.parse(dataStr) : null; } catch (e) { data = null; }
        handleOnboardingSyncEvent(currentEvent || "message", data);
        currentEvent = null;
      }
    }
    if (!_onboardingFinished) {
      // Stream closed without a terminal event (backend restart, dropped
      // connection): surface as a recoverable error state.
      handleOnboardingSyncEvent("error", { error: "Connection to the indexing job was lost." });
    }
  } finally {
    _onboardingStreamActive = false;
  }
}

function handleOnboardingSyncEvent(eventName, data) {
  // SSE state machine step. Progress/start events update the card; terminal
  // events are one-shot per PROCESS run (guarded by _onboardingFinished).
  if (!ONBOARDING_SYNC_TERMINAL_EVENTS.includes(eventName)) {
    if (eventName === "sync_start" || eventName === "progress") {
      updateOnboardingProcessProgress(data);
    }
    return eventName;
  }
  if (_onboardingFinished) return eventName; // terminal already handled
  _onboardingFinished = true;
  _onboardingStreamActive = false;
  if (eventName === "done") {
    finishOnboardingProcessing("done", data);
  } else if (eventName === "aborted") {
    if (_onboardingAbortIntent === null) {
      // Single-owner finalization (P2-8): this window did not initiate the
      // abort — the initiating window owns the intent and will broadcast the
      // resulting stage ("ready" or "configure") via the bus, which resolves
      // this card. Self-finalize as aborted_stop only if the owner vanishes.
      const refs = _onboardingProcessCardRefs;
      if (refs && refs.countsLine) {
        refs.countsLine.textContent = "Stopped from another window\u2026";
      }
      const armedGeneration = _currentOnboardingSyncGeneration();
      _onboardingAbortFallbackTimer = setTimeout(() => {
        _onboardingAbortFallbackTimer = null;
        _finalizeFollowerAbortFallback(armedGeneration, data);
      }, 20000);
    } else {
      const intent = _onboardingAbortIntent;
      _onboardingAbortIntent = null;
      finishOnboardingProcessing(intent === "adjust" ? "aborted_adjust" : "aborted_stop", data);
    }
  } else if (eventName === "auto_aborted") {
    finishOnboardingProcessing("auto_aborted", data);
  } else {
    finishOnboardingProcessing("error", data);
  }
  return eventName;
}

async function finishOnboardingProcessing(outcome, data) {
  if (_onboardingAbortFallbackTimer) {
    try { clearTimeout(_onboardingAbortFallbackTimer); } catch (e) {}
    _onboardingAbortFallbackTimer = null;
  }
  switch (outcome) {
    case "done": {
      await writeOnboardingState("ready");
      setOnboardingStage("ready");
      try { addNotification("Library indexing complete \u2014 Zocrates is ready.", "success"); } catch (e) {}
      cleanupOnboardingProcessCard();
      removeBootstrapProgressCard();
      break;
    }
    case "aborted_stop": {
      await writeOnboardingState("ready", { process_aborted: true });
      setOnboardingStage("ready");
      try {
        addNotification("Indexing stopped early \u2014 the search index is partial. Resume any time via the Sync menu entry.", "info");
      } catch (e) {}
      cleanupOnboardingProcessCard();
      removeBootstrapProgressCard();
      break;
    }
    case "aborted_adjust": {
      _onboardingAbortIntent = null;
      await writeOnboardingState("configure");
      setOnboardingStage("configure");
      enterOnboardingConfigure();
      break;
    }
    case "auto_aborted": {
      // Poison-item auto-abort (backend consecutive-failure threshold):
      // offer Retry (idempotent resume) and the two stop paths.
      showOnboardingProcessWarning(
        "Indexing stopped automatically after repeated conversion failures on one or more items. " +
        "You can retry (already-indexed items are skipped), adjust settings, or stop here with a partial index."
      );
      break;
    }
    default: {
      const detail = data && data.error ? String(data.error) : "Indexing failed. You can retry (already-indexed items are skipped), adjust settings, or stop with a partial index.";
      showOnboardingProcessError(detail);
      break;
    }
  }
}

function cleanupOnboardingProcessCard() {
  const refs = _onboardingProcessCardRefs;
  if (refs && refs.card) {
    try { refs.card.remove(); } catch (e) {}
  }
  _onboardingProcessCardRefs = null;
}

function cleanupOnboardingResources() {
  // Lifecycle cleanup (plan §9). NO-OP while onboarding is active: the
  // dedicated #onboarding-container cards are NOT children of #chat-messages
  // and must survive session loads, New Chat, and chat cleanup — this only
  // tidies stale cards once the flow has resolved (it does NOT abort an
  // active SSE reader; the stream is owned by the PROCESS flow itself).
  if (isOnboardingActive()) return false;
  if (_onboardingStreamAbort) {
    try { _onboardingStreamAbort.abort(); } catch (e) {}
  }
  _onboardingStreamAbort = null;
  _onboardingStreamActive = false;
  _onboardingFinished = false;
  _onboardingProcessCardRefs = null;
  _onboardingWizardRefs = null;
  let removed = false;
  for (const id of ["onboarding-wizard-card", "onboarding-process-card"]) {
    const node = document.getElementById(id);
    if (node) { try { node.remove(); } catch (e) {} removed = true; }
  }
  return removed;
}

// ---------------------------------------------------------------------------
// Selection Detection — Event-driven (primary) + polling (fallback)
// ---------------------------------------------------------------------------

/**
 * Maps a raw Zotero item object to our chip data shape.
 * Each field is accessed individually so one bad call doesn't kill the whole item.
 */
function mapItemToChipData(item) {
  let title = "(no title)";
  let authorStr = "";
  let year = "";
  let mapErr = null;
  let type = "item";

  try {
    if (item.isNote && item.isNote()) {
      type = "note";
      const rawNote = item.getNote?.() || item.getField?.('note') || "";
      const cleanText = rawNote.replace(/<[^>]*>/g, "").trim();
      if (cleanText) {
        title = cleanText.split("\n")[0].substring(0, 50);
      } else {
        title = "Standalone Note";
      }
    } else {
      title = item.getDisplayTitle?.()
           || item.getField?.('title')
           || item.getField?.('shortTitle')
           || "(no title)";
    }
  } catch (e) { mapErr = "title:" + e; }

  try {
    const creators = item.getCreatorsJSON?.() ?? item.getCreators?.() ?? [];
    authorStr = creators.map(c => c.lastName || c.name || "").filter(Boolean).join(", ");
  } catch (e) { mapErr = (mapErr ? mapErr + "; " : "") + "creators:" + e; }

  try {
    const date = item.getField?.('date') || "";
    year = date ? String(date).substring(0, 4) : "";
  } catch (e) { mapErr = (mapErr ? mapErr + "; " : "") + "date:" + e; }

  if (mapErr) {
    setDebug("❌ map error: " + mapErr);
    console.error("Zocrates: item map error:", mapErr);
  }

  return { key: item.key, title, authorStr, year, type };
}

/**
 * Given a list of raw Zotero items from getSelectedItems(),
 * update activeItemsList and re-render chips.
 */
function applySelection(rawItems, force = false) {
  if (contextMode !== "papers" && !force) return;

  const regularItems = (rawItems || []).filter(item => {
    try {
      if (item.isRegularItem()) return true;
      if (item.isAttachment && item.isAttachment() && !item.parentItem) return true;
      if (item.isNote && item.isNote() && !item.parentItem) return true;
      return false;
    } catch(e) { return false; }
  });

  const keysStr = regularItems.map(i => i.key).sort().join(",");

  if (isStickyMode && !force) {
    if (keysStr === lastActiveKeysStr) return;
    lastActiveKeysStr = keysStr;

    if (regularItems.length === 0) return;

    const existingKeys = new Set(activeItemsList.map(i => i.key));
    let addedAny = false;

    for (let item of regularItems) {
      if (!item.key) continue;

      // If paper was previously removed via ✕, re-selecting it in Zotero re-adds it
      if (removedKeys.has(item.key)) {
        removedKeys.delete(item.key);
      }

      if (!existingKeys.has(item.key)) {
        activeItemsList.push(mapItemToChipData(item));
        existingKeys.add(item.key);
        addedAny = true;
      }
    }

    if (addedAny) {
      console.log("Zocrates: sticky applySelection →", activeItemsList.length, "chips");
      setDebug("✓ " + activeItemsList.length + " chip(s) accumulated (sticky)");
      renderActiveChips();
    }
    return;
  }

  // Non-sticky mode (default)
  if (!force && keysStr === lastActiveKeysStr) return; // no change

  // Reset removedKeys if selection set changed substantially
  const currentKeys = new Set(regularItems.map(i => i.key));
  let isSubset = true;
  for (let k of removedKeys) {
    if (!currentKeys.has(k)) { isSubset = false; break; }
  }
  if (!isSubset || lastActiveKeysStr === "") removedKeys.clear();

  lastActiveKeysStr = keysStr;

  const visibleItems = regularItems.filter(item => !removedKeys.has(item.key));
  const paperChips = visibleItems.map(mapItemToChipData);

  // Preserve parent paper chips for active snippets so they remain anchored
  const snippetParentKeys = new Set(
    (activeSnippetsList || [])
      .map(s => s.parent_key || s.parentKey || s.item_key || s.itemKey)
      .filter(Boolean)
  );
  const preservedSnippetParents = activeItemsList.filter(
    item => snippetParentKeys.has(item.key) && !paperChips.some(p => p.key === item.key) && !removedKeys.has(item.key)
  );

  activeItemsList = [...paperChips, ...preservedSnippetParents];

  console.log("Zocrates: applySelection →", activeItemsList.length, "chips, keys:", keysStr);
  setDebug(activeItemsList.length > 0 ? "✓ " + activeItemsList.length + " chip(s) captured" : "");
  renderActiveChips();
}

/**
 * Reads selected items from the pane right now.
 * Returns [] if the pane is unavailable or the selection is empty.
 */
function readSelectedItemsNow() {
  if (!Zotero) return [];
  try {
    const pane = resolveZoteroPane();
    if (!pane) return [];
    // Try itemsView first (more direct), then the pane wrapper
    const items = pane.itemsView?.getSelectedItems?.() || pane.getSelectedItems?.() || [];
    
    // Map attachments to parent items and de-duplicate by key
    const mappedItems = [];
    const seenKeys = new Set();
    
    for (let item of items) {
      if (!item || (!item.key && !item.isItem)) continue;
      if (typeof item.isRegularItem === "function" && !item.isRegularItem()
          && !(item.isAttachment && item.isAttachment())
          && !(item.isNote && item.isNote())) {
        continue;
      }
      let targetItem = item;
      try {
        if (item.isAttachment && item.isAttachment()) {
          const parent = item.parentItem;
          if (parent) targetItem = parent;
        } else if (item.isNote && item.isNote()) {
          const parent = item.parentItem;
          if (parent) targetItem = parent;
        }
      } catch (err) {
        console.warn("Zocrates: Failed to resolve parent item:", err);
      }
      
      const key = targetItem.key;
      if (key) {
        if (!seenKeys.has(key)) {
          seenKeys.add(key);
          mappedItems.push(targetItem);
        }
      } else {
        // Fallback for items without key (e.g. unsaved/special items)
        mappedItems.push(targetItem);
      }
    }
    
    return mappedItems;
  } catch(e) {
    console.warn("Zocrates: readSelectedItemsNow error:", e);
    return [];
  }
}

let clickListenerAttached = false;
let _treeClickHandler = null; // Store reference for cleanup (H13)

/**
 * Attaches a click listener to the parent Zotero library tree element.
 * Whenever the user clicks the list, we assume they are initiating a selection update
 * and clear any manual chip closures (removedKeys), allowing the clicked item to reappear.
 */
function setupTreeClickListener() {
  if (clickListenerAttached) return;
  try {
    const parentDoc = window.parent?.document;
    if (!parentDoc) return;
    const treeEl = parentDoc.getElementById("zotero-items-tree");
    if (!treeEl) return;

    _treeClickHandler = () => {
      console.log("Zocrates: library tree clicked, resetting manual closes");
      if (!isStickyMode) {
        removedKeys.clear();
      }
      lastActiveKeysStr = ""; // Force poll to re-read and apply selection
    };
    treeEl.addEventListener("click", _treeClickHandler);
    clickListenerAttached = true;
    console.log("Zocrates: Tree click listener attached successfully");
  } catch (e) {
    console.error("Zocrates: tree click listener setup error:", e);
  }
}

/**
 * High-frequency selection check. Reads selected items from Zotero and applies them.
 * Using document.hasFocus() ensures we do not clear selections when focus is in our sidebar.
 */
function checkActiveSelection() {
  if (!Zotero) return;

  // Try to attach the click listener to Zotero's tree if not done yet
  if (!clickListenerAttached) setupTreeClickListener();

  // If in collection or library mode, do not auto-update active items
  if (contextMode !== "papers") return;

  try {
    const items = readSelectedItemsNow();
    let regularItems = items.filter(item => {
      try {
        if (item.isRegularItem()) return true;
        if (item.isAttachment && item.isAttachment() && !item.parentItem) return true;
        if (item.isNote && item.isNote() && !item.parentItem) return true;
        return false;
      } catch(e) { return false; }
    });
    const keysStr = regularItems.map(i => i.key).sort().join(",");

    if (keysStr !== lastActiveKeysStr) {
      if (items.length > 0) {
        // Selection updated in the library pane
        applySelection(items);
      } else {
        // Selection is reported as empty.
        if (isStickyMode) {
          // In sticky mode, retain accumulated chips when selection is empty
          lastActiveKeysStr = "";
        } else {
          // We only clear our active chips if the user is NOT actively typing in the sidebar,
          // and we do NOT have a custom context active (collection or library).
          const hasCustomContext = activeItemsList.some(item => item.type === "collection" || item.type === "library");
          const sidebarFocused = typeof document !== "undefined" && typeof document?.hasFocus === "function"
            ? document.hasFocus()
            : false;
          if (!hasCustomContext && !sidebarFocused) {
            activeItemsList = [];
            removedKeys.clear();
            renderActiveChips();
          }
          lastActiveKeysStr = "";
        }
      }
    }
  } catch(e) {
    console.error("Zocrates: checkActiveSelection error:", e);
  }
}

// ---------------------------------------------------------------------------
// Pane resolver — finds the ZoteroPane from inside the iframe
// ---------------------------------------------------------------------------
function resolveZoteroPane() {
  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  try {
    if (typeof window !== "undefined") {
      if (window.parent && window.parent !== window && window.parent.ZoteroPane) {
        return window.parent.ZoteroPane;
      }
      if (window.top && window.top !== window && window.top.ZoteroPane) {
        return window.top.ZoteroPane;
      }
      if (window.ZoteroPane) {
        return window.ZoteroPane;
      }
    }
  } catch (e) {}

  try {
    const mainWin = zot?.getMainWindow?.();
    if (mainWin && mainWin.ZoteroPane) {
      return mainWin.ZoteroPane;
    }
  } catch (e) {}

  try {
    if (typeof Components !== "undefined" && Components.classes) {
      const wm = Components.classes["@mozilla.org/appshell/window-mediator;1"]
        .getService(Components.interfaces.nsIWindowMediator);
      const enumerator = wm.getEnumerator("navigator:browser");
      while (enumerator.hasMoreElements()) {
        const win = enumerator.getNext();
        if (win.ZoteroPane) {
          return win.ZoteroPane;
        }
      }
    }
  } catch (e) {}

  try {
    const pane = zot?.getActiveZoteroPane?.();
    if (pane) {
      return pane;
    }
  } catch (e) {}

  return null;
}

function resolveActiveLibraryID(pane) {
  try {
    if (pane?.getSelectedLibraryIDs) {
      const ids = pane.getSelectedLibraryIDs();
      if (Array.isArray(ids) && ids.length > 0) return ids[0];
    }
  } catch (e) {}
  try {
    if (pane?.getSelectedLibraryID) {
      return pane.getSelectedLibraryID();
    }
  } catch (e) {}
  return Zotero?.Libraries?.userLibraryID;
}

function resolveSelectedCollections(pane) {
  if (!pane) return [];
  try {
    if (typeof pane.getSelectedCollections === "function") {
      const cols = pane.getSelectedCollections();
      if (Array.isArray(cols)) return cols.filter(Boolean);
    }
  } catch (e) {
    console.warn("Zocrates: getSelectedCollections error:", e);
  }

  try {
    if (typeof pane.getSelectedCollection === "function") {
      const col = pane.getSelectedCollection();
      if (col && typeof col === "object") return [col];
    }
  } catch (e) {
    console.warn("Zocrates: getSelectedCollection error:", e);
  }

  return [];
}

function resolveActiveLibraryInfo(pane) {
  const libraryID = resolveActiveLibraryID(pane);
  let name = "My Library";
  try {
    if (libraryID && libraryID !== Zotero?.Libraries?.userLibraryID) {
      name = Zotero?.Libraries?.get?.(libraryID)?.name || Zotero?.Libraries?.getName?.(libraryID) || "Library";
    }
  } catch (e) {
    console.warn("Zocrates: resolveActiveLibraryInfo error:", e);
  }
  return { libraryID, name };
}

function selectItemInZotero(itemKey) {
  if (!itemKey) return;
  try {
    const zot = resolveZotero();
    if (!zot) {
      console.warn("Zocrates: Zotero object not found for selectItemInZotero");
      return;
    }

    const pane = (typeof zot.getActiveZoteroPane === "function" ? zot.getActiveZoteroPane() : null) ||
                 resolveZoteroPane() ||
                 window.parent?.ZoteroPane ||
                 window.top?.ZoteroPane;

    let item = null;
    if (zot.Items && typeof zot.Items.getByLibraryAndKey === "function") {
      try {
        const userLibID = zot.Libraries?.userLibraryID;
        if (userLibID !== undefined) {
          item = zot.Items.getByLibraryAndKey(userLibID, itemKey);
        }
      } catch (_) {}
      if (!item && zot.Libraries?.getAll) {
        try {
          for (const lib of zot.Libraries.getAll()) {
            const cand = zot.Items.getByLibraryAndKey(lib.id, itemKey);
            if (cand) {
              item = cand;
              break;
            }
          }
        } catch (_) {}
      }
    }

    const performSelection = (targetItem) => {
      if (!targetItem || !pane) return;
      let finalItem = targetItem;
      if (finalItem.isAttachment?.() && finalItem.parentItemID) {
        try {
          const parentItem = zot.Items.get(finalItem.parentItemID);
          if (parentItem) finalItem = parentItem;
        } catch (_) {}
      }
      if (typeof pane.selectItem === "function") {
        pane.selectItem(finalItem.id, false, true);
      } else if (typeof pane.selectItems === "function") {
        pane.selectItems([finalItem.id]);
      }
    };

    if (item) {
      performSelection(item);
    } else if (zot.Items && typeof zot.Items.getByLibraryAndKeyAsync === "function") {
      const userLibID = zot.Libraries?.userLibraryID;
      zot.Items.getByLibraryAndKeyAsync(userLibID, itemKey).then(cand => {
        if (cand) {
          performSelection(cand);
        }
      }).catch(() => {});
    }
  } catch (err) {
    console.warn("Zocrates: selectItemInZotero error:", err);
  }
}

// Module-level in-flight tracker to coalesce duplicate clicks and avoid race conditions
const _activeReaderNavigations = new Map();

function isSyntheticOrTitle(heading, documentTitle = "") {
  if (!heading || typeof heading !== "string") return true;
  const clean = heading.trim().replace(/^§\s*/, "").replace(/[*`_]/g, "").replace(/\$([^$]*)\$/g, "$1").trim();
  if (!clean) return true;
  const lower = clean.toLowerCase();
  if (
    lower === "frontmatter" ||
    lower === "full document" ||
    lower === "abstract" ||
    /^(?:figure|table)\s+\d/i.test(clean)
  ) {
    return true;
  }
  if (documentTitle && normalizeForComparison(clean) === normalizeForComparison(documentTitle)) {
    return true;
  }
  return false;
}

function extractLeafHeading(rawSection, documentTitle = "") {
  if (!rawSection || typeof rawSection !== "string") return { leaf: "", parents: [] };
  let clean = rawSection.trim();
  // Strip leading section symbols and Markdown formatting
  clean = clean.replace(/^§\s*/, "").replace(/[*`_]/g, "").replace(/\$([^$]*)\$/g, "$1");

  // Take the last component of a hierarchy breadcrumb
  let parents = [];
  if (clean.includes(" > ")) {
    const parts = clean.split(" > ").map(p => p.trim()).filter(Boolean);
    clean = parts.pop() || "";
    parents = parts;
  }
  clean = clean.replace(/^§\s*/, "").trim();

  // Ignore synthetic labels or headings equal to document title
  if (isSyntheticOrTitle(clean, documentTitle)) {
    return { leaf: "", parents: [] };
  }

  return { leaf: clean, parents };
}

function normalizeForComparison(str) {
  if (!str) return "";
  return str
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "") // strip combining diacritics
    .replace(/[\^\{\}\[\]\+_\\]/g, "") // strip LaTeX punctuation and formatting characters
    .replace(/[^\p{L}\p{N}\s]/gu, " ") // Unicode-aware letter/number filtering
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function parseHeadingParts(rawTitle) {
  const clean = (rawTitle || "").trim().replace(/^§\s*/, "");
  if (!clean) return { numbering: null, core: "" };

  // Case 1: The entire heading is purely a section number (e.g. "3.2.2", "III", "Appendix B", "B.1")
  const pureNumMatch = clean.match(/^(?:[Aa]ppendix\s+[A-Z]|[IVXLCDM]+(?:[\.\-][\dA-Za-z]+)*|\d+(?:[\.\-][\dA-Za-z]+)*|[A-Z](?:[\.\-][\dA-Za-z]+)+)$/);
  if (pureNumMatch) {
    return {
      numbering: clean.toLowerCase().replace(/[^a-z0-9\.\-]/g, "").replace(/[\.\-]+$/, ""),
      core: ""
    };
  }

  // Case 2: Numbering prefix followed by title.
  // Match Arabic (3.2.2), uppercase Roman (III, IV.B), or uppercase Lettered (Appendix B, B.1, A.)
  // Explicitly case-sensitive to avoid matching common words like "Mix of Experts" or "A Survey"
  const numMatch = clean.match(/^((?:[Aa]ppendix\s+[A-Z]|[IVXLCDM]+(?:[\.\-][\dA-Za-z]+)*|\d+(?:[\.\-][\dA-Za-z]+)*|[A-Z](?:[\.\-][\dA-Za-z]+)+)[\.\:\s\-\)]+|[A-Z][\.\:\-\)][\.\:\s\-\)]*)(.*)$/);
  if (numMatch) {
    return {
      numbering: numMatch[1].toLowerCase().replace(/[^a-z0-9\.\-]/g, "").replace(/[\.\-]+$/, ""),
      core: normalizeForComparison(numMatch[2])
    };
  }

  return {
    numbering: null,
    core: normalizeForComparison(clean)
  };
}

function findOutlineDestination(outlineNodes, targetHeading, parentHeadings = []) {
  if (!Array.isArray(outlineNodes) || outlineNodes.length === 0 || !targetHeading) return null;

  const targetParts = parseHeadingParts(targetHeading);
  const targetCore = targetParts.core;
  if (!targetCore && !targetParts.numbering) return null;

  const targetParentsNorm = parentHeadings.map(p => normalizeForComparison(p)).filter(Boolean);
  const candidates = [];

  function traverse(nodes, parentChain = []) {
    for (const node of nodes) {
      if (!node) continue;
      const rawTitle = node.title || "";
      const targetLocation = node.location || (node.dest ? { dest: node.dest } : (node.pageIndex !== undefined ? { pageIndex: node.pageIndex } : null));

      if (rawTitle && targetLocation) {
        const nodeParts = parseHeadingParts(rawTitle);
        let score = 0;

        // Tier 1: Exact core title match + exact numbering match (Highest confidence: 100)
        if (targetParts.numbering && nodeParts.numbering && targetParts.numbering === nodeParts.numbering) {
          if (targetCore && nodeParts.core && targetCore === nodeParts.core) {
            score = 100;
          } else if (!targetCore || !nodeParts.core) {
            score = 80;
          } else {
            score = -1; // Number matches but title differs significantly -> reject
          }
        }
        // Tier 2: Exact core title match (Score: 70)
        // If BOTH have numbering but they conflict (e.g. "3.2 Results" vs "4.2 Results"), reject
        else if (targetCore && nodeParts.core && targetCore === nodeParts.core) {
          if (targetParts.numbering && nodeParts.numbering && targetParts.numbering !== nodeParts.numbering) {
            score = -1;
          } else {
            score = 70;
          }
        }
        // Tier 3: Token-boundary core containment for multi-word titles (min 8 chars) (Score: 40)
        else if (targetCore && nodeParts.core && targetCore.length >= 8 && nodeParts.core.length >= 8) {
          const coreRegex = new RegExp(`\\b${escapeRegex(targetCore)}\\b`, "i");
          const nodeRegex = new RegExp(`\\b${escapeRegex(nodeParts.core)}\\b`, "i");
          if (coreRegex.test(nodeParts.core) || nodeRegex.test(targetCore)) {
            if (targetParts.numbering && nodeParts.numbering && targetParts.numbering !== nodeParts.numbering) {
              score = -1;
            } else {
              score = 40;
            }
          }
        }

        // Parent chain corroboration bonus (+10 per matching parent)
        if (score > 0 && targetParentsNorm.length > 0 && parentChain.length > 0) {
          for (const p of parentChain) {
            const pNorm = normalizeForComparison(p);
            if (targetParentsNorm.some(tp => tp === pNorm || pNorm.includes(tp))) {
              score += 10;
            }
          }
        }

        if (score >= 40) {
          candidates.push({ node, targetLocation, score, title: rawTitle });
        }
      }

      if (Array.isArray(node.items) && node.items.length > 0) {
        traverse(node.items, [...parentChain, rawTitle]);
      }
    }
  }

  traverse(outlineNodes, []);

  if (candidates.length === 0) return null;

  // Sort descending by score
  candidates.sort((a, b) => b.score - a.score);

  // If the top score is tied between two distinct destinations, reject to prevent jumping to wrong section
  if (candidates.length > 1 && candidates[0].score === candidates[1].score) {
    if (JSON.stringify(candidates[0].targetLocation) !== JSON.stringify(candidates[1].targetLocation)) {
      return null;
    }
  }

  return candidates[0].targetLocation;
}

function findHtmlSnapshotHeading(doc, targetHeading) {
  if (!doc || !targetHeading) return null;

  const targetParts = parseHeadingParts(targetHeading);
  const targetCore = targetParts.core;
  if (!targetCore && !targetParts.numbering) return null;

  let headings = [];
  try {
    const rawHeadings = doc.querySelectorAll("h1, h2, h3, h4, h5, h6, [role='heading']");
    if (rawHeadings && rawHeadings.length > 0) {
      headings = Array.from(rawHeadings);
    }
  } catch (_) {}

  // Fallback for mock DOM or environments where comma-separated selector isn't supported
  if (headings.length === 0) {
    const selectors = ["h1", "h2", "h3", "h4", "h5", "h6", "[role='heading']"];
    for (const sel of selectors) {
      try {
        const found = doc.querySelectorAll(sel);
        if (found && found.length > 0) {
          headings.push(...Array.from(found));
        }
      } catch (_) {}
    }
  }

  if (headings.length === 0) return null;

  let bestEl = null;
  let bestScore = -1;

  for (const el of headings) {
    const rawText = el.textContent || "";
    const nodeParts = parseHeadingParts(rawText);
    let score = 0;

    if (targetParts.numbering && nodeParts.numbering && targetParts.numbering === nodeParts.numbering) {
      if (targetCore && nodeParts.core && targetCore === nodeParts.core) {
        score = 100;
      } else if (!targetCore || !nodeParts.core) {
        score = 80;
      } else {
        score = -1;
      }
    } else if (targetCore && nodeParts.core && targetCore === nodeParts.core) {
      if (targetParts.numbering && nodeParts.numbering && targetParts.numbering !== nodeParts.numbering) {
        score = -1;
      } else {
        score = 70;
      }
    } else if (targetCore && nodeParts.core && targetCore.length >= 8 && nodeParts.core.length >= 8) {
      const coreRegex = new RegExp(`\\b${escapeRegex(targetCore)}\\b`, "i");
      const nodeRegex = new RegExp(`\\b${escapeRegex(nodeParts.core)}\\b`, "i");
      if (coreRegex.test(nodeParts.core) || nodeRegex.test(targetCore)) {
        if (targetParts.numbering && nodeParts.numbering && targetParts.numbering !== nodeParts.numbering) {
          score = -1;
        } else {
          score = 40;
        }
      }
    }

    if (score > bestScore) {
      bestScore = score;
      bestEl = el;
    }
  }

  return bestScore >= 40 ? bestEl : null;
}

function openItemInReader(attachmentKey, rawSection) {
  if (!attachmentKey) return;

  const coalescingKey = `${attachmentKey}|${rawSection || ""}`;
  // Debounce/coalesce concurrent clicks on the same attachment and section
  if (_activeReaderNavigations.has(coalescingKey)) {
    return _activeReaderNavigations.get(coalescingKey);
  }

  const navPromise = (async () => {
    try {
      const zot = resolveZotero();
      if (!zot) {
        console.warn("Zocrates: Zotero object not found for openItemInReader");
        return;
      }
      const reader = zot.Reader || window.top?.Zotero?.Reader || window.parent?.Zotero?.Reader;
      if (!reader || typeof reader.open !== "function") {
        console.warn("Zocrates: Zotero.Reader.open not available");
        return;
      }

      // 1. Resolve Zotero attachment item
      let item = null;
      if (zot.Items && typeof zot.Items.getByLibraryAndKey === "function") {
        try {
          const userLibID = zot.Libraries?.userLibraryID;
          if (userLibID !== undefined) {
            item = zot.Items.getByLibraryAndKey(userLibID, attachmentKey);
          }
        } catch (_) {}
        if (!item && zot.Libraries?.getAll) {
          try {
            for (const lib of zot.Libraries.getAll()) {
              const cand = zot.Items.getByLibraryAndKey(lib.id, attachmentKey);
              if (cand) {
                item = cand;
                break;
              }
            }
          } catch (_) {}
        }
      }

      let targetItemID = null;
      let targetAttachment = null;
      if (item) {
        if (!item.isAttachment?.() && typeof item.getAttachments === "function") {
          const attIDs = item.getAttachments();
          if (attIDs && attIDs.length > 0) {
            targetItemID = attIDs[0];
            targetAttachment = zot.Items?.get?.(targetItemID);
          }
        } else {
          targetItemID = item.id;
          targetAttachment = item;
        }
      }

      if (!targetItemID) return;

      let isPdf = Boolean(
        targetAttachment?.attachmentReaderType === "pdf" ||
        targetAttachment?.attachmentContentType === "application/pdf" ||
        (targetAttachment?.attachmentFilename && targetAttachment.attachmentFilename.toLowerCase().endsWith(".pdf")) ||
        (targetAttachment?.contentType === "application/pdf")
      );

      let isSnapshot = Boolean(
        targetAttachment?.attachmentReaderType === "snapshot" ||
        targetAttachment?.attachmentContentType === "text/html" ||
        (targetAttachment?.attachmentFilename && (targetAttachment.attachmentFilename.toLowerCase().endsWith(".html") || targetAttachment.attachmentFilename.toLowerCase().endsWith(".htm"))) ||
        (targetAttachment?.contentType === "text/html")
      );

      // 2. Open or focus reader tab
      const readerInstance = await reader.open(targetItemID);
      if (!readerInstance) return;

      const docTitle = item?.getField?.("title") || item?.getDisplayTitle?.() || "";
      const { leaf, parents } = extractLeafHeading(rawSection, docTitle);
      if (!leaf) return;

      // 3. Overall timeout deadline (3.5 seconds)
      const deadlineMs = 3500;
      const startTime = Date.now();

      // =====================================================================
      // BRANCH A: PDF Attachment Navigation via Native Outline
      // =====================================================================
      if (isPdf) {
        let outline = null;

        while (Date.now() - startTime < deadlineMs) {
          const internalReader = readerInstance._internalReader || readerInstance;

          // Attempt 1: Zotero's cached internal outline
          if (internalReader?._state?.outline && Array.isArray(internalReader._state.outline) && internalReader._state.outline.length > 0) {
            outline = internalReader._state.outline;
            break;
          }

          // Attempt 2: PDFViewerApplication.pdfDocument.getOutline2() then getOutline()
          const iframeWin = internalReader?._primaryView?._iframeWindow;
          const pdfViewerApp = iframeWin?.wrappedJSObject?.PDFViewerApplication || iframeWin?.PDFViewerApplication;
          const pdfDoc = pdfViewerApp?.pdfDocument;

          if (pdfDoc) {
            if (typeof pdfDoc.getOutline2 === "function") {
              try {
                const docOutline2 = await pdfDoc.getOutline2();
                if (Array.isArray(docOutline2) && docOutline2.length > 0) {
                  outline = docOutline2;
                  break;
                }
              } catch (_) {}
            }
            if (typeof pdfDoc.getOutline === "function") {
              try {
                const docOutline = await pdfDoc.getOutline();
                if (Array.isArray(docOutline) && docOutline.length > 0) {
                  outline = docOutline;
                  break;
                }
              } catch (_) {}
            }
          }

          await new Promise(resolve => setTimeout(resolve, 120));
        }

        if (!outline || !Array.isArray(outline) || outline.length === 0) return;

        let targetLocation = findOutlineDestination(outline, leaf, parents);
        if (!targetLocation && parents && parents.length > 0) {
          for (let i = parents.length - 1; i >= 0; i--) {
            const parentHeading = parents[i];
            if (isSyntheticOrTitle(parentHeading, docTitle)) continue;
            const ancestorParents = parents.slice(0, i);
            targetLocation = findOutlineDestination(outline, parentHeading, ancestorParents);
            if (targetLocation) break;
          }
        }
        if (!targetLocation) return;

        const internalReader = readerInstance._internalReader || readerInstance;
        // Execute destination jump with a bounded timeout race to prevent stuck promises
        let navTimer = null;
        const navRemainingMs = Math.max(1000, deadlineMs - (Date.now() - startTime));
        const navTimeoutPromise = new Promise(resolve => {
          navTimer = setTimeout(resolve, navRemainingMs);
        });

        const jumpPromise = (async () => {
          if (typeof internalReader.navigate === "function") {
            return await internalReader.navigate(targetLocation);
          } else if (typeof readerInstance.navigate === "function") {
            return await readerInstance.navigate(targetLocation);
          } else {
            const iframeWin = internalReader?._primaryView?._iframeWindow;
            const pdfViewerApp = iframeWin?.wrappedJSObject?.PDFViewerApplication || iframeWin?.PDFViewerApplication;
            const linkService = pdfViewerApp?.pdfLinkService;

            if (linkService && typeof linkService.goToDestination === "function" && targetLocation?.dest) {
              return await linkService.goToDestination(targetLocation.dest);
            }
          }
        })();
        // Guard against late rejection if timeout wins
        jumpPromise.catch(() => {});

        try {
          await Promise.race([jumpPromise, navTimeoutPromise]);
        } finally {
          if (navTimer) clearTimeout(navTimer);
        }
      }

      // =====================================================================
      // BRANCH B: HTML Web Snapshot Navigation via DOM scrollIntoView
      // =====================================================================
      else if (isSnapshot) {
        while (Date.now() - startTime < deadlineMs) {
          const internalReader = readerInstance._internalReader || readerInstance;
          const iframeDoc = internalReader?._primaryView?._iframeWindow?.document ||
                            internalReader?._iframeWindow?.document ||
                            internalReader?.document;

          if (iframeDoc && iframeDoc.readyState === "complete") {
            const matchedEl = findHtmlSnapshotHeading(iframeDoc, leaf);
            if (matchedEl && typeof matchedEl.scrollIntoView === "function") {
              matchedEl.scrollIntoView({ behavior: "smooth", block: "start" });
            }
            break; // Stop polling once document is complete
          }

          await new Promise(resolve => setTimeout(resolve, 120));
        }
      }
    } catch (err) {
      console.warn("Zocrates: openItemInReader navigation error:", err);
    }
  })();

  _activeReaderNavigations.set(coalescingKey, navPromise);
  navPromise.finally(() => {
    _activeReaderNavigations.delete(coalescingKey);
  });
  return navPromise;
}


// ---------------------------------------------------------------------------
// Markdown Formatter (with local image display support)
// ---------------------------------------------------------------------------

function htmlToXhtml(htmlString) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(`<div>${htmlString}</div>`, "text/html");
    const wrapper = doc.body.firstChild;
    const serializer = new XMLSerializer();
    let result = "";
    for (let i = 0; i < wrapper.childNodes.length; i++) {
      result += serializer.serializeToString(wrapper.childNodes[i]);
    }
    // Restore explicit SVG namespace if stripped by XMLSerializer
    result = result.replace(/<svg(?![^>]*\bxmlns=)/gi, '<svg xmlns="http://www.w3.org/2000/svg"');
    // Restore camelCase SVG attributes lost during DOMParser text/html parsing
    result = result.replace(/\bviewbox=/gi, 'viewBox=');
    result = result.replace(/\bpreserveaspectratio=/gi, 'preserveAspectRatio=');
    return result;
  } catch (e) {
    console.error("Zocrates: htmlToXhtml serialization error:", e);
    return htmlString;
  }
}

// ---------------------------------------------------------------------------
// Mermaid Diagram Rendering & Lazy Loader
// ---------------------------------------------------------------------------

let _mermaidLoadingPromise = null;
let _mermaidDiagramCounter = 0;
let _autoRenderMermaid = true;

function getAutoRenderMermaidPref() {
  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  if (zot && zot.Prefs) {
    const val = zot.Prefs.get("extensions.zocrates.autoRenderMermaid", true);
    if (val !== undefined && val !== null) {
      return Boolean(val);
    }
  }
  return _autoRenderMermaid;
}

function getMermaidThemeConfig() {
  const appliedTheme = document.body ? (document.body.getAttribute("data-theme") || "slate") : "slate";

  if (appliedTheme === "amoled") {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
      fontFamily: "var(--font-family-sans, system-ui, sans-serif)",
      themeVariables: {
        darkMode: true,
        background: "#0d0d0d",
        mainBkg: "#0d0d0d",
        primaryColor: "#1e1e1e",
        primaryTextColor: "#ffffff",
        primaryBorderColor: "#27272a",
        lineColor: "#818cf8",
        secondaryColor: "#141414",
        tertiaryColor: "#1a1a1a",
        edgeLabelBackground: "#0d0d0d"
      }
    };
  } else if (appliedTheme === "slate") {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
      fontFamily: "var(--font-family-sans, system-ui, sans-serif)",
      themeVariables: {
        darkMode: true,
        background: "#1e293b",
        mainBkg: "#1e293b",
        primaryColor: "#334155",
        primaryTextColor: "#f8fafc",
        primaryBorderColor: "#475569",
        lineColor: "#818cf8",
        secondaryColor: "#0f172a",
        tertiaryColor: "#1e293b",
        edgeLabelBackground: "#1e293b"
      }
    };
  } else if (appliedTheme === "zotero-dark") {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
      fontFamily: "var(--font-family-sans, system-ui, sans-serif)",
      themeVariables: {
        darkMode: true,
        background: "#2d2d2d",
        mainBkg: "#2d2d2d",
        primaryColor: "#3a3a3a",
        primaryTextColor: "#e3e3e3",
        primaryBorderColor: "#4d4d4d",
        lineColor: "#007acc",
        secondaryColor: "#1e1e1e",
        tertiaryColor: "#2d2d2d",
        edgeLabelBackground: "#2d2d2d"
      }
    };
  } else if (appliedTheme === "sepia") {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "default",
      fontFamily: "var(--font-family-sans, system-ui, sans-serif)",
      themeVariables: {
        darkMode: false,
        background: "#fdfbf7",
        mainBkg: "#fdfbf7",
        primaryColor: "#f4ecd8",
        primaryTextColor: "#433422",
        primaryBorderColor: "#e4d5b7",
        lineColor: "#c05c36",
        secondaryColor: "#eae0c9",
        tertiaryColor: "#fdfbf7",
        edgeLabelBackground: "#fdfbf7"
      }
    };
  } else {
    // zotero-light / default
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "default",
      fontFamily: "var(--font-family-sans, system-ui, sans-serif)",
      themeVariables: {
        darkMode: false,
        background: "#ffffff",
        mainBkg: "#ffffff",
        primaryColor: "#f3f4f6",
        primaryTextColor: "#1f2937",
        primaryBorderColor: "#e5e7eb",
        lineColor: "#007acc",
        secondaryColor: "#f9fafb",
        tertiaryColor: "#ffffff",
        edgeLabelBackground: "#ffffff"
      }
    };
  }
}

function initMermaid() {
  if (typeof window.mermaid !== "undefined") {
    try {
      window.mermaid.initialize(getMermaidThemeConfig());
    } catch (e) {
      console.warn("Zocrates: failed to initialize mermaid configuration:", e);
    }
  }
}

function ensureMermaidLoaded() {
  if (typeof window.mermaid !== "undefined") {
    return Promise.resolve(window.mermaid);
  }
  if (_mermaidLoadingPromise) {
    return _mermaidLoadingPromise;
  }
  _mermaidLoadingPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "lib/mermaid.min.js";
    script.onload = () => {
      initMermaid();
      resolve(window.mermaid);
    };
    script.onerror = (err) => {
      _mermaidLoadingPromise = null;
      console.error("Zocrates: Failed to load local mermaid.min.js:", err);
      reject(new Error("Failed to load local mermaid.min.js"));
    };
    document.head.appendChild(script);
  });
  return _mermaidLoadingPromise;
}

/**
 * Decorates complete message mermaid code blocks with a "Render diagram" / "Show code" toggle button.
 * During streaming, this is not invoked so mermaid blocks remain plain syntax-highlighted code blocks.
 * If auto-render is enabled (default true), diagrams are rendered automatically upon decoration.
 */
function decorateMermaidCodeBlocks(container) {
  if (!container) return;
  const wrappers = container.querySelectorAll(".code-block-wrapper");
  let foundMermaid = false;

  wrappers.forEach(wrapper => {
    const langLabel = wrapper.querySelector(".code-lang-label");
    const lang = langLabel ? langLabel.textContent.trim().toLowerCase() : "";
    const isMermaid = lang === "mermaid" || wrapper.querySelector("code.mermaid, code.hljs.mermaid") !== null;
    if (!isMermaid) return;

    foundMermaid = true;
    const header = wrapper.querySelector(".code-block-header");
    if (!header) return;

    // Cache the pristine code text on the wrapper
    const codeEl = wrapper.querySelector("pre code");
    if (codeEl && !wrapper._rawMermaidCode) {
      let codeText = codeEl.textContent.trim();
      codeText = codeText
        .replace(/--&gt;/g, "-->")
        .replace(/==&gt;/g, "==>")
        .replace(/-\.&gt;/g, "-.->")
        .replace(/&gt;/g, ">")
        .replace(/&lt;/g, "<");
      wrapper._rawMermaidCode = codeText;
    }

    let toggleBtn = header.querySelector(".mermaid-toggle-btn");
    if (!toggleBtn) {
      toggleBtn = document.createElement("button");
      toggleBtn.setAttribute("class", "mermaid-toggle-btn");
      toggleBtn.setAttribute("type", "button");
      toggleBtn.setAttribute("title", "Render diagram");
      toggleBtn.textContent = "Render diagram";

      const copyBtn = header.querySelector(".code-copy-btn");
      if (copyBtn) {
        header.insertBefore(toggleBtn, copyBtn);
      } else {
        header.appendChild(toggleBtn);
      }
    }

    // Auto-render if enabled and not already rendered, currently busy, or marked with syntax error
    const autoRender = getAutoRenderMermaidPref();
    const existingContainer = wrapper.querySelector(".mermaid-diagram-container");
    const isRendered = existingContainer && existingContainer.querySelector("svg");
    const hasError = wrapper.querySelector(".mermaid-error-banner");
    if (autoRender && !isRendered && !hasError && !wrapper._isMermaidBusy) {
      toggleMermaidDiagram(toggleBtn);
    }
  });

  // Lazy-load library when a mermaid block is encountered so it is preloaded
  if (foundMermaid) {
    ensureMermaidLoaded().catch(() => {});
  }
}

/**
 * Toggles between raw Mermaid code block and rendered SVG diagram.
 */
async function toggleMermaidDiagram(btn) {
  const wrapper = btn.closest(".code-block-wrapper");
  if (!wrapper || wrapper._isMermaidBusy) return;

  const preEl = wrapper.querySelector("pre");
  let container = wrapper.querySelector(".mermaid-diagram-container");
  const isShowingDiagram = container && container.style.display !== "none";

  if (isShowingDiagram) {
    // Switch back to code view
    container.style.display = "none";
    if (preEl) preEl.style.display = "";
    btn.textContent = "Render diagram";
    btn.title = "Render diagram";
    return;
  }

  const currentAppliedTheme = document.body.getAttribute("data-theme") || "slate";

  // If container already exists and has rendered SVG with matching theme, just show it
  if (container && container.querySelector("svg") && container.dataset.mermaidTheme === currentAppliedTheme) {
    if (preEl) preEl.style.display = "none";
    container.style.display = "flex";
    btn.textContent = "Show code";
    btn.title = "Show code";
    return;
  }

  // Need to render diagram
  wrapper._isMermaidBusy = true;
  btn.disabled = true;
  btn.textContent = "Rendering...";
  btn.title = "Rendering...";

  // Remove any previous error banner
  const prevError = wrapper.querySelector(".mermaid-error-banner");
  if (prevError) prevError.remove();

  const codeEl = wrapper.querySelector("pre code");
  let rawCode = wrapper._rawMermaidCode || (codeEl ? codeEl.textContent.trim() : "");
  rawCode = rawCode
    .replace(/--&gt;/g, "-->")
    .replace(/==&gt;/g, "==>")
    .replace(/-\.&gt;/g, "-.->")
    .replace(/&gt;/g, ">")
    .replace(/&lt;/g, "<");
  wrapper._rawMermaidCode = rawCode;

  const diagramId = "zocrates-mermaid-" + (++_mermaidDiagramCounter) + "-" + Date.now();

  try {
    await ensureMermaidLoaded();
    initMermaid();

    const renderResult = await window.mermaid.render(diagramId, rawCode);
    const svgCode = (typeof renderResult === "object" && renderResult.svg) ? renderResult.svg : renderResult;

    if (!container) {
      container = document.createElement("div");
      container.className = "mermaid-diagram-container";
      wrapper.appendChild(container);
    }

    // Explanatory comment:
    // The rendered SVG is sanitized through sanitizeMermaidSvg() to strip <script> tags,
    // on* handlers, and javascript: links while preserving Mermaid's styling, defs, and paths.
    container.innerHTML = sanitizeMermaidSvg(svgCode);
    container.dataset.mermaidTheme = currentAppliedTheme;
    container.style.display = "flex";
    if (preEl) preEl.style.display = "none";

    if (typeof renderResult.bindFunctions === "function") {
      renderResult.bindFunctions(container);
    }

    btn.textContent = "Show code";
    btn.title = "Show code";
  } catch (err) {
    console.error("Zocrates: Mermaid render failed:", err);
    // Clean up any stray error elements created by Mermaid in document
    const strayEl = document.getElementById(diagramId) || document.getElementById("d" + diagramId);
    if (strayEl) strayEl.remove();

    if (preEl) preEl.style.display = "";
    if (container) container.style.display = "none";

    let errorBanner = wrapper.querySelector(".mermaid-error-banner");
    if (!errorBanner) {
      errorBanner = document.createElement("div");
      errorBanner.className = "mermaid-error-banner";
      if (preEl) {
        wrapper.insertBefore(errorBanner, preEl);
      } else {
        wrapper.appendChild(errorBanner);
      }
    }
    const diagMsg = err && err.message ? err.message.split("\n")[0] : "unable to render";
    errorBanner.textContent = `Diagram syntax error (${diagMsg}). Showing code.`;
    btn.textContent = "Render diagram";
    btn.title = "Render diagram";
  } finally {
    btn.disabled = false;
    wrapper._isMermaidBusy = false;
  }
}

/**
 * Re-renders any currently visible Mermaid diagrams when the active theme changes.
 */
function updateMermaidThemes() {
  if (typeof window.mermaid === "undefined") return;
  initMermaid();

  const activeContainers = document.querySelectorAll(".mermaid-diagram-container");
  activeContainers.forEach(async (container) => {
    if (container.style.display === "none") return;
    const wrapper = container.closest(".code-block-wrapper");
    if (!wrapper || !wrapper._rawMermaidCode) return;

    let codeToRender = (wrapper._rawMermaidCode || "")
      .replace(/--&gt;/g, "-->")
      .replace(/==&gt;/g, "==>")
      .replace(/-\.&gt;/g, "-.->")
      .replace(/&gt;/g, ">")
      .replace(/&lt;/g, "<");

    const diagramId = "zocrates-mermaid-" + (++_mermaidDiagramCounter) + "-" + Date.now();
    try {
      const renderResult = await window.mermaid.render(diagramId, codeToRender);
      const svgCode = (typeof renderResult === "object" && renderResult.svg) ? renderResult.svg : renderResult;
      container.innerHTML = sanitizeMermaidSvg(svgCode);
      container.dataset.mermaidTheme = document.body.getAttribute("data-theme") || "slate";
      if (typeof renderResult.bindFunctions === "function") {
        renderResult.bindFunctions(container);
      }
    } catch (e) {
      console.warn("Zocrates: failed to re-render diagram on theme change:", e);
      const strayEl = document.getElementById(diagramId) || document.getElementById("d" + diagramId);
      if (strayEl) strayEl.remove();
    }
  });
}

/**
 * Post-processes a container element to inject copy buttons into code block
 * headers. The entire button (including SVG icons) is built via createElementNS
 * to avoid the htmlToXhtml() DOMParser round-trip, which corrupts SVG namespaces
 * and can strip or mangle interactive elements in Gecko's strict XHTML parser.
 * This mirrors how the working message-level action buttons are constructed.
 */
function fixCodeBlockCopyButtons(container) {
  if (!container) return;
  const SVGNS = "http://www.w3.org/2000/svg";
  const XHTMLNS = "http://www.w3.org/1999/xhtml";

  const COPY_PATH = "M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z";
  const CHECK_PATH = "M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z";

  function createSvgIcon(pathD, className, style) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "13");
    svg.setAttribute("height", "13");
    if (className) svg.setAttribute("class", className);
    if (style) svg.setAttribute("style", style);
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", pathD);
    svg.appendChild(path);
    return svg;
  }

  // Find all code block headers that don't already have a copy button
  const headers = container.querySelectorAll(".code-block-header");
  headers.forEach(header => {
    if (header.querySelector(".code-copy-btn")) return; // already has a button

    const btn = document.createElementNS(XHTMLNS, "button");
    btn.setAttribute("class", "code-copy-btn");
    btn.setAttribute("title", "Copy code");
    btn.setAttribute("type", "button");

    btn.appendChild(createSvgIcon(COPY_PATH, "copy-icon"));
    btn.appendChild(createSvgIcon(CHECK_PATH, "check-icon hidden", "color: #22c55e;"));

    header.appendChild(btn);
  });
}

/**
 * Graceful degradation for bundle asset images: when the backend is offline
 * (or the asset is gone), replace the broken <img> with a styled chip showing
 * the alt text instead of Gecko's broken-image placeholder.  on* attributes
 * cannot go in renderer strings (sanitizeHTML strips them), so error
 * listeners are attached post-insertion — same pattern as
 * fixCodeBlockCopyButtons().  The error event fires asynchronously after
 * insertion, so attaching here always catches the failure.
 */
function decorateAssetImages(rootEl) {
  if (!rootEl) return;
  const XHTMLNS = "http://www.w3.org/1999/xhtml";
  const imgs = rootEl.querySelectorAll("img");
  for (const img of imgs) {
    const src = img.getAttribute("src") || "";
    if (!src.includes("/assets/")) continue;
    img.addEventListener("error", () => {
      // Guard against re-rendered messages: node may already be detached.
      if (!img.parentNode) return;
      const alt = (img.getAttribute("alt") || "").trim();
      const chip = document.createElementNS(XHTMLNS, "div");
      chip.className = "asset-unavailable";
      chip.textContent = "🖼 Image unavailable (backend offline)" + (alt ? " — " + alt : "");
      img.replaceWith(chip);
    }, { once: true });
  }
}

async function copyTextToClipboard(text) {
  // Keep in sync with addon/preferences.js::copyTextToClipboard
  if (text === null || text === undefined || text === "") {
    return false;
  }
  const textToCopy = String(text);

  // 1. Modern navigator.clipboard API
  const nav = (typeof window !== "undefined" && window.navigator) || (typeof navigator !== "undefined" ? navigator : null);
  if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
    try {
      await nav.clipboard.writeText(textToCopy);
      return true;
    } catch (e) {
      // Fall through to secondary clipboard tiers
    }
  }

  // 2. Zotero XPCOM / internal utility
  const zot = (typeof resolveZotero === "function" ? resolveZotero() : null) ||
              (typeof Zotero !== "undefined" ? Zotero : (typeof window !== "undefined" && window.Zotero ? window.Zotero : null));
  if (
    zot &&
    zot.Utilities &&
    zot.Utilities.Internal &&
    typeof zot.Utilities.Internal.copyTextToClipboard === "function"
  ) {
    try {
      zot.Utilities.Internal.copyTextToClipboard(textToCopy);
      return true;
    } catch (e) {
      // Fall through to DOM fallback
    }
  }

  // 3. Hidden <textarea> + document.execCommand("copy") fallback
  if (typeof document !== "undefined" && typeof document.createElement === "function") {
    const container = document.body || document.documentElement;
    if (container && typeof document.execCommand === "function") {
      let textArea = null;
      try {
        textArea = document.createElement("textarea");
        textArea.value = textToCopy;
        textArea.style.position = "fixed";
        textArea.style.top = "-9999px";
        textArea.style.left = "-9999px";
        textArea.setAttribute("aria-hidden", "true");
        container.appendChild(textArea);
        if (typeof textArea.select === "function") {
          textArea.select();
        }
        const successful = document.execCommand("copy");
        if (successful !== false) {
          return true;
        }
      } catch (e) {
        // execCommand failed
      } finally {
        if (textArea && textArea.parentNode) {
          textArea.parentNode.removeChild(textArea);
        }
      }
    }
  }

  if (typeof console !== "undefined" && typeof console.warn === "function") {
    console.warn("Zocrates: clipboard copy failed across all tiers");
  }
  return false;
}

window.copyCodeBlock = async function(btn) {
  try {
    const wrapper = btn.closest(".code-block-wrapper");
    if (!wrapper) return;
    const codeEl = wrapper.querySelector("code");
    const textToCopy = wrapper._rawMermaidCode || (codeEl ? (codeEl.innerText || codeEl.textContent) : "");

    const copied = await copyTextToClipboard(textToCopy);
    if (!copied) {
      if (typeof console !== "undefined" && typeof console.warn === "function") {
        console.warn("Zocrates: failed to copy code block to clipboard");
      }
      return;
    }

    const copyIcon = btn.querySelector(".copy-icon");
    const checkIcon = btn.querySelector(".check-icon");

    if (copyIcon && checkIcon) {
      copyIcon.classList.add("hidden");
      checkIcon.classList.remove("hidden");
      setTimeout(() => {
        copyIcon.classList.remove("hidden");
        checkIcon.classList.add("hidden");
      }, 1500);
    }
  } catch (err) {
    console.error("Failed to copy code block:", err);
  }
};

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".code-copy-btn");
  if (!btn) return;
  e.stopPropagation();
  window.copyCodeBlock(btn);
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".mermaid-toggle-btn");
  if (!btn) return;
  e.stopPropagation();
  toggleMermaidDiagram(btn);
});

// Open hyperlinks in the system browser (Gecko iframe does not navigate on anchor click)
document.addEventListener("click", (e) => {
  const anchor = e.target.closest("a[href]");
  if (!anchor) return;
  const href = anchor.getAttribute("href");
  if (!href || href.startsWith("#")) return;
  e.preventDefault();
  e.stopPropagation();
  if (!isSafeLinkUrl(href)) return;
  try {
    const zot = resolveZotero();
    if (zot && typeof zot.launchURL === "function") {
      zot.launchURL(href);
      return;
    }
  } catch (_) {}
  // Fallback: locate a Services.io handle from the parent chrome context
  try {
    const win = window.parent || window.top || window.opener;
    if (win && win.Services && win.Services.io) {
      const uri = win.Services.io.newURI(href);
      const svc = win.Cc["@mozilla.org/uriloader/external-protocol-service;1"]
                      .getService(win.Ci.nsIExternalProtocolService);
      svc.loadURI(uri);
    }
  } catch (_) {}
});

function formatMarkdown(text, citationPlaceholders = []) {
  if (!text) return "";

  const placeholders = [];
  
  // 1. Extract block math: $$ ... $$
  let processedText = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, math) => {
    const placeholder = `@@BLOCKMATH_${placeholders.length}@@`;
    placeholders.push({
      placeholder: placeholder,
      type: 'block',
      math: math
    });
    return placeholder;
  });

  // 2. Extract inline math: $ ... $ (with strict formatting checks to prevent false positives like currency)
  processedText = processedText.replace(/(?<!\$)\$(?!\s)((?:\\\$|[^$])+?)(?<!\s)\$(?!\$)/g, (match, math) => {
    const placeholder = `@@INLINEMATH_${placeholders.length}@@`;
    placeholders.push({
      placeholder: placeholder,
      type: 'inline',
      math: math
    });
    return placeholder;
  });

  // Configure marked if loaded (once)
  if (typeof window.marked !== 'undefined' && !window._markedConfigured) {
    const renderer = new window.marked.Renderer();
    renderer.image = ({ href, title, text }) => {
      const safeSrc = isSafeUrl(href) ? escapeHTML(resolveAssetUrl(href)) : "";
      const safeAlt = escapeHTML(text || "");
      const safeTitle = escapeHTML(title || "");
      return `<img src="${safeSrc}" alt="${safeAlt}" title="${safeTitle}" loading="lazy" style="max-width: 100%; height: auto; border-radius: 6px; margin: 12px 0; display: block; border: 1px solid var(--border);" />`;
    };
    renderer.code = (token) => {
      let codeText = (typeof token === 'object' && token.text !== undefined) ? token.text : (token || "");
      let rawLang = (typeof token === 'object' && token.lang !== undefined) ? token.lang : (arguments[1] || "");
      let language = (rawLang || "").trim().toLowerCase();

      // Common language alias resolution
      const langAliases = {
        "py": "python",
        "python3": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "sh": "bash",
        "shell": "bash",
        "zsh": "bash",
        "c++": "cpp",
        "yml": "yaml",
        "html": "xml"
      };
      if (langAliases[language]) {
        language = langAliases[language];
      }

      let highlightedCode = "";
      let detectedLang = language;

      if (typeof window.hljs !== 'undefined') {
        if (language && window.hljs.getLanguage(language)) {
          try {
            highlightedCode = window.hljs.highlight(codeText, { language }).value;
          } catch (e) {
            highlightedCode = escapeHTML(codeText);
          }
        } else if (language) {
          highlightedCode = escapeHTML(codeText);
        } else {
          try {
            const autoRes = window.hljs.highlightAuto(codeText);
            if (autoRes && autoRes.relevance >= 3 && autoRes.language) {
              highlightedCode = autoRes.value;
              detectedLang = autoRes.language;
            } else {
              highlightedCode = escapeHTML(codeText);
              detectedLang = "code";
            }
          } catch (e) {
            highlightedCode = escapeHTML(codeText);
            detectedLang = "code";
          }
        }
      } else {
        highlightedCode = escapeHTML(codeText);
      }

      const displayLang = detectedLang ? detectedLang.toLowerCase() : "code";

      // NOTE: The copy button is NOT included in this template because the
      // htmlToXhtml() round-trip (DOMParser text/html → XMLSerializer) corrupts
      // both SVG namespaces and interactive elements in Gecko's strict XHTML
      // parser. Instead, fixCodeBlockCopyButtons() constructs the entire button
      // with createElementNS after innerHTML is set.
      return `<div class="code-block-wrapper">` +
        `<div class="code-block-header">` +
          `<span class="code-lang-label">${escapeHTML(displayLang)}</span>` +
        `</div>` +
        `<pre><code class="hljs ${escapeHTML(displayLang)}">${highlightedCode}</code></pre>` +
      `</div>`;
    };

    const figureBlockExtension = {
      name: 'figure',
      level: 'block',
      start(src) {
        const match = src.match(/<figure\b/i);
        return match ? match.index : -1;
      },
      tokenizer(src) {
        const match = src.match(/^<figure\b([^>]*)>([\s\S]*?)<\/figure>/i);
        if (match) {
          return {
            type: 'figure',
            raw: match[0],
            attrs: match[1] || '',
            innerContent: match[2].trim(),
          };
        }
      },
      renderer(token) {
        const innerHtml = window.marked.parse(token.innerContent);
        return `<figure${token.attrs}>${innerHtml}</figure>`;
      }
    };

    window.marked.use({
      renderer: renderer,
      extensions: [figureBlockExtension],
      gfm: true,
      breaks: true
    });
    window._markedConfigured = true;
  }

  // 3. Render Markdown using marked
  let html = "";
  if (typeof window.marked !== 'undefined') {
    html = window.marked.parse(processedText);
  } else {
    html = processedText.replace(/\n/g, "<br />");
  }

  // 4. Render Math and replace placeholders
  for (const item of placeholders) {
    let mathHtml;
    try {
      if (typeof window.katex !== 'undefined') {
        mathHtml = window.katex.renderToString(item.math, {
          displayMode: item.type === 'block',
          throwOnError: true
        });
      } else {
        throw new Error("KaTeX is not defined");
      }
    } catch (err) {
      console.warn("KaTeX render failed for:", item.math, err);
      const delimiter = item.type === 'block' ? '$$' : '$';
      mathHtml = delimiter + escapeHTML(item.math) + delimiter;
    }
    // Safe replace using a function return to avoid regex sequence issues (like $& replacement)
    html = html.replace(item.placeholder, () => mathHtml);
  }

  // 5. Restore citation placeholders
  if (Array.isArray(citationPlaceholders) && citationPlaceholders.length > 0) {
    for (let i = 0; i < citationPlaceholders.length; i++) {
      html = html.replaceAll(`@@ZOCRATESCITATION${i}@@`, () => citationPlaceholders[i]);
    }
  }

  // 6. Sanitize and convert to valid XHTML
  return sanitizeHTML(htmlToXhtml(html));
}

function formatMessageContent(text, metadata = null, isUser = false) {
  if (!text) return "";
  let processed = text;

  // Strip redundant trailing reference/source/recommendation sections since candidate cards display them
  if (!isUser && metadata && metadata.candidates && metadata.candidates.length > 0) {
    processed = processed.replace(/(?:\n{1,3}|^\s*)#{1,3}\s*(?:References|Cited Sources|Sources Consulted|Recommended Papers for Import)[\s\S]*$/i, "").trim();
  }

  const citations = (metadata && Array.isArray(metadata.citations)) ? metadata.citations : null;
  const citationPlaceholders = [];

  // 1. Mask fenced code blocks and inline code to prevent citation, header, and escaping collisions
  const codeBlocks = [];
  processed = processed.replace(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g, (match) => {
    const ph = `@@ZOCRATES_CODE_MASK_${codeBlocks.length}@@`;
    codeBlocks.push(match);
    return ph;
  });

  // 2. User messages: escape raw HTML in prose (outside code blocks) so tags
  // like <img> or <script> display as literal text while markdown
  // formatting (**bold**, etc.) and code blocks still work.
  if (isUser) {
    processed = processed.replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  const citationRegex = /\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】/g;

  if (citations && citations.length > 0) {
    const totalOccurrencesInText = {};
    const countRegex = new RegExp(citationRegex.source, citationRegex.flags);
    let countMatch;
    while ((countMatch = countRegex.exec(processed)) !== null) {
      const raw = countMatch[1] || countMatch[2] || "";
      const parts = raw.split(/[,;]/).map(s => s.trim()).filter(Boolean);
      const seenInCountToken = new Set();
      for (const part of parts) {
        const idx = parseInt(part, 10);
        if (isNaN(idx) || seenInCountToken.has(idx)) continue;
        seenInCountToken.add(idx);
        totalOccurrencesInText[idx] = (totalOccurrencesInText[idx] || 0) + 1;
      }
    }

    const occCounters = {};
    // 2. Scan and replace citation tokens: [1], [1, 2], [1; 2], [Chunk 1], [E 1], 【1】
    processed = processed.replace(citationRegex, (match, p1, p2) => {
      const raw = p1 || p2 || "";
      const parts = raw.split(/[,;]/).map(s => s.trim()).filter(Boolean);
      const pills = [];
      const seenInToken = new Set();

      for (const part of parts) {
        const idx = parseInt(part, 10);
        if (isNaN(idx) || seenInToken.has(idx)) continue;
        seenInToken.add(idx);

        const c = citations.find(item => item.index === idx);
        if (c) {
          const seenCount = (occCounters[idx] || 0) + 1;
          occCounters[idx] = seenCount;

          const allOccs = Array.isArray(c.occurrences) ? c.occurrences : [];
          const survivingOccs = allOccs.filter(o => o.survived !== false);
          const hasRejectedOccs = allOccs.some(o => o.survived === false);

          let occRec = null;
          if (hasRejectedOccs && (totalOccurrencesInText[idx] || 0) > survivingOccs.length) {
            occRec = allOccs.find(o => o.occurrence === seenCount) || allOccs[seenCount - 1] || null;
            if (!occRec || occRec.survived === false) continue;
          } else {
            if (allOccs.length > 0 && seenCount > survivingOccs.length) continue;
            occRec = allOccs.length > 0 ? (survivingOccs[seenCount - 1] || null) : null;
          }

          const canonicalOcc = (occRec && typeof occRec.occurrence === "number")
            ? occRec.occurrence
            : seenCount;
          const occProb = (occRec && occRec.support_probability !== undefined && occRec.support_probability !== null)
            ? occRec.support_probability
            : (allOccs.length === 0 ? c.support_probability : null);

          const auth = c.authors ? c.authors.trim() : "";
          const yr = c.year ? String(c.year).trim() : "";
          let label = "";
          if (auth && yr) {
            label = `${auth} (${yr})`;
          } else if (auth) {
            label = auth;
          } else if (yr) {
            label = `(${yr})`;
          } else {
            let t = c.title ? String(c.title).trim() : `[${idx}]`;
            if (t.length > 15) {
              t = t.substring(0, 15).trim() + "…";
            }
            label = t;
          }

          const sec = c.section ? String(c.section).trim() : "General Context";
          const numProb = (occProb !== undefined && occProb !== null)
            ? parseFloat(occProb)
            : null;
          const probStr = (numProb !== null && Number.isFinite(numProb))
            ? numProb.toFixed(2)
            : "N/A";
          let cleanTitle = c.title ? String(c.title).trim() : "Untitled Document";
          if (cleanTitle.length > 55) {
            cleanTitle = cleanTitle.substring(0, 55).trim() + "…";
          }

          const tooltip = `Title: ${cleanTitle}\nSection: ${sec}\nSupport probability: ${probStr}`;
          const escapedTooltip = escapeHTML(tooltip).replace(/\r?\n/g, "&#10;");
          pills.push(`<span class="citation-pill" data-citation-idx="${idx}" data-citation-occ="${canonicalOcc}" title="${escapedTooltip}">${escapeHTML(label)}</span>`);
        }
      }

      if (pills.length === 0) {
        return "";
      }
      const pillGroup = `<span class="citation-pill-group">${pills.join(", ")}</span>`;
      const ph = `@@ZOCRATESCITATION${citationPlaceholders.length}@@`;
      citationPlaceholders.push(pillGroup);
      return ph;
    });
  } else if ((metadata && Array.isArray(metadata.references) && metadata.references.length > 0) || (metadata && metadata.citation_verification)) {
    // Defense-in-depth: scrub any lingering bracket citation tokens if no citations were kept
    processed = processed.replace(new RegExp(`[ \\t]*${citationRegex.source}[ \\t]*([.,;!?])?`, "g"), (m, p1, p2, punct) => punct || "");
  }

  // Ensure Cited Sources and the considered-sources drawer are rendered if references metadata exists
  const refs = (metadata && Array.isArray(metadata.references)) ? metadata.references : null;
  const alreadyHasSources = processed.includes("# Cited Sources") || processed.includes('<details class="considered-sources">');
  if (refs && refs.length > 0 && !alreadyHasSources) {
    const tier1 = refs.filter(r => r.tier === 1 || r.tier === "1" || !r.tier);
    const tier2 = refs.filter(r => r.tier === 2 || r.tier === "2");
    let refMd = "";
    if (tier1.length > 0) {
      refMd += "\n\n# Cited Sources\n";
      tier1.forEach((r) => {
        const auth = r.authors ? String(r.authors).trim() : "";
        const yr = r.year ? String(r.year).trim() : "";
        let prefix = "";
        if (auth && yr) prefix = `**${escapeHTML(auth)} (${escapeHTML(yr)})**. `;
        else if (auth) prefix = `**${escapeHTML(auth)}**. `;
        else if (yr) prefix = `**(${escapeHTML(yr)})**. `;
        const title = r.title ? `*${escapeHTML(r.title)}*.` : "*Untitled Document*.";
        const pub = r.publication ? ` ${escapeHTML(r.publication)}.` : "";
        const key = r.item_key || "";
        const link = key ? `<span class="zotero-source-link" data-item-key="${escapeHTML(key)}">${prefix}${title}${pub}</span>` : `${prefix}${title}${pub}`;
        refMd += `- ${link}\n`;
      });
    }

    if (tier2.length > 0) {
      const cnt = tier2.length;
      const plural = cnt === 1 ? "Source" : "Sources";
      // Note: no heading above this drawer — the summary itself is the
      // leading content ("N Uncited Sources Considered by the Agent").
      const prefixNl = tier1.length > 0 ? "\n" : "\n\n";
      refMd += `${prefixNl}<details class="considered-sources">\n  <summary>${cnt} Uncited ${plural} Considered by the Agent</summary>\n  <ul>\n`;
      tier2.forEach(r => {
        const auth = r.authors ? String(r.authors).trim() : "";
        const yr = r.year ? String(r.year).trim() : "";
        let prefix = "";
        if (auth && yr) prefix = `<strong>${escapeHTML(auth)} (${escapeHTML(yr)})</strong>. `;
        else if (auth) prefix = `<strong>${escapeHTML(auth)}</strong>. `;
        else if (yr) prefix = `<strong>(${escapeHTML(yr)})</strong>. `;
        const title = r.title ? `<em>${escapeHTML(r.title)}</em>.` : "<em>Untitled Document</em>.";
        const pub = r.publication ? ` ${escapeHTML(r.publication)}.` : "";
        const key = r.item_key || "";
        const link = key ? `<span class="zotero-source-link" data-item-key="${escapeHTML(key)}">${prefix}${title}${pub}</span>` : `${prefix}${title}${pub}`;
        refMd += `    <li>${link}</li>\n`;
      });
      refMd += `  </ul>\n</details>\n`;
    }
    if (refMd) {
      processed += refMd;
    }
  }

  // Wrap Cited Sources and any subsequent sources sections in .zocrates-sources-section (while code blocks are masked)
  if ((processed.includes("# Cited Sources") || processed.includes('<details class="considered-sources">')) && !processed.includes('<div class="zocrates-sources-section">')) {
    let sourcesIdx = -1;
    const citedIdx = processed.indexOf("# Cited Sources");
    const detailsIdx = processed.indexOf('<details class="considered-sources">');
    if (citedIdx !== -1 && detailsIdx !== -1) {
      sourcesIdx = Math.min(citedIdx, detailsIdx);
    } else if (citedIdx !== -1) {
      sourcesIdx = citedIdx;
    } else {
      sourcesIdx = detailsIdx;
    }

    if (sourcesIdx !== -1) {
      const mainBody = processed.substring(0, sourcesIdx);
      const sourcesBody = processed.substring(sourcesIdx);
      processed = `${mainBody}\n\n<div class="zocrates-sources-section">\n\n${sourcesBody.trim()}\n\n</div>`;
    }
  }

  // 3. Restore code blocks
  for (let i = 0; i < codeBlocks.length; i++) {
    processed = processed.replace(`@@ZOCRATES_CODE_MASK_${i}@@`, codeBlocks[i]);
  }

  return formatMarkdown(processed, citationPlaceholders);
}

let activeEvidencePopover = null;

function closeEvidencePopover() {
  if (activeEvidencePopover) {
    activeEvidencePopover.remove();
    activeEvidencePopover = null;
  }
}

function showEvidencePopover(pill, citation) {
  if (activeEvidencePopover) {
    const isSamePill = (activeEvidencePopover._targetPill === pill);
    closeEvidencePopover();
    if (isSamePill) return;
  }

  const XHTMLNS = "http://www.w3.org/1999/xhtml";
  const popover = document.createElementNS(XHTMLNS, "div");
  popover.className = "evidence-popover";
  popover._targetPill = pill;

  const title = citation.title ? String(citation.title).trim() : "Untitled Document";
  const pub = citation.publication ? String(citation.publication).trim() : "";
  const auth = citation.authors ? citation.authors.trim() : "";
  const yr = citation.year ? String(citation.year).trim() : "";
  let authYear = "";
  if (auth && yr) {
    authYear = `${auth} (${yr})`;
  } else if (auth) {
    authYear = auth;
  } else if (yr) {
    authYear = `(${yr})`;
  }

  const sec = citation.section ? String(citation.section).trim() : "";
  const quote = citation.quote ? String(citation.quote).trim() : "";
  const attKey = citation.attachment_key || citation.item_key || "";
  const itemKey = citation.item_key || "";

  // 1. Header Row (Title + Maximize Button)
  const headerDiv = document.createElementNS(XHTMLNS, "div");
  headerDiv.className = "evidence-popover-header";

  const titleDiv = document.createElementNS(XHTMLNS, "div");
  titleDiv.className = "evidence-popover-title";
  titleDiv.textContent = title;
  headerDiv.appendChild(titleDiv);

  const maximizeBtn = document.createElementNS(XHTMLNS, "button");
  maximizeBtn.className = "evidence-popover-maximize-btn";
  maximizeBtn.setAttribute("type", "button");
  maximizeBtn.setAttribute("title", "Maximize in sidebar");
  maximizeBtn.textContent = "⤢";

  function toggleMaximize() {
    const isMax = popover.classList.contains("is-maximized");
    if (!isMax) {
      popover._prevGeometry = {
        left: popover.style.left,
        top: popover.style.top,
        width: popover.style.width,
        height: popover.style.height,
        maxHeight: popover.style.maxHeight,
      };
      popover.classList.add("is-maximized");
      maximizeBtn.textContent = "⤡";
      maximizeBtn.setAttribute("title", "Restore size");
      const bodyEl = popover.querySelector(".evidence-popover-body");
      if (bodyEl) {
        bodyEl.style.maxHeight = "none";
      }
    } else {
      popover.classList.remove("is-maximized");
      maximizeBtn.textContent = "⤢";
      maximizeBtn.setAttribute("title", "Maximize in sidebar");
      if (popover._prevGeometry) {
        popover.style.left = popover._prevGeometry.left;
        popover.style.top = popover._prevGeometry.top;
        popover.style.width = popover._prevGeometry.width;
        popover.style.height = popover._prevGeometry.height;
        popover.style.maxHeight = popover._prevGeometry.maxHeight;
      }
    }
  }

  maximizeBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleMaximize();
  });

  maximizeBtn.addEventListener("dblclick", (e) => {
    e.stopPropagation();
  });

  headerDiv.addEventListener("dblclick", (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleMaximize();
  });

  headerDiv.appendChild(maximizeBtn);
  popover.appendChild(headerDiv);

  // 2. Metadata (Authors, Year, Pub)
  if (authYear || pub) {
    const metaDiv = document.createElementNS(XHTMLNS, "div");
    metaDiv.className = "evidence-popover-meta";
    if (authYear) {
      const authSpan = document.createElementNS(XHTMLNS, "span");
      authSpan.className = "evidence-popover-author-year";
      authSpan.textContent = authYear;
      metaDiv.appendChild(authSpan);
    }
    if (pub) {
      const pubSpan = document.createElementNS(XHTMLNS, "span");
      pubSpan.className = "evidence-popover-pub";
      pubSpan.textContent = (authYear ? " • " : "") + pub;
      metaDiv.appendChild(pubSpan);
    }
    popover.appendChild(metaDiv);
  }

  // 3. Badges row (Section badge & Decision Support badge)
  let supportBadge = null;
  const occ = parseInt(pill.getAttribute("data-citation-occ"), 10);
  let targetProb = citation.support_probability;
  if (Number.isFinite(occ) && Array.isArray(citation.occurrences)) {
    const occRecord = citation.occurrences.find(o => o.occurrence === occ);
    if (occRecord) {
      targetProb = (occRecord.support_probability !== undefined && occRecord.support_probability !== null)
        ? occRecord.support_probability
        : null;
    } else {
      const occRecords = citation.occurrences.filter(o => o.survived !== false);
      if (occRecords[occ - 1] && occRecords[occ - 1].support_probability !== undefined && occRecords[occ - 1].support_probability !== null) {
        targetProb = occRecords[occ - 1].support_probability;
      }
    }
  }

  if (targetProb !== undefined && targetProb !== null) {
    const numProb = parseFloat(targetProb);
    if (Number.isFinite(numProb) && numProb >= 0.0 && numProb <= 1.0) {
      const pFormatted = numProb.toFixed(2);
      const badge = document.createElementNS(XHTMLNS, "div");
      if (numProb >= 0.80) {
        badge.className = "evidence-support-badge high";
        badge.textContent = `✓ Support Probability: ${pFormatted}`;
      } else if (numProb >= 0.50) {
        badge.className = "evidence-support-badge moderate";
        badge.textContent = `ⓘ Support Probability: ${pFormatted}`;
      } else {
        badge.className = "evidence-support-badge low";
        badge.textContent = `Support Probability: ${pFormatted}`;
      }
      badge.setAttribute("title", `Calibrated support probability: ${pFormatted} (evaluated by decision model)`);
      supportBadge = badge;
    }
  }

  if (sec || supportBadge) {
    const badgesRow = document.createElementNS(XHTMLNS, "div");
    badgesRow.className = "evidence-popover-badges-row";
    if (sec) {
      const secDiv = document.createElementNS(XHTMLNS, "div");
      secDiv.className = "evidence-popover-section-badge";
      const formattedSec = formatMarkdown(`§ ${sec}`).trim().replace(/^<p>([\s\S]*?)<\/p>$/i, "$1").trim();
      secDiv.innerHTML = formattedSec;
      badgesRow.appendChild(secDiv);
    }
    if (supportBadge) {
      badgesRow.appendChild(supportBadge);
    }
    popover.appendChild(badgesRow);
  }

  // 4. Quote Body
  if (quote) {
    const bodyDiv = document.createElementNS(XHTMLNS, "div");
    bodyDiv.className = "evidence-popover-body";
    const quoteDiv = document.createElementNS(XHTMLNS, "div");
    quoteDiv.className = "evidence-popover-quote markdown-body";
    quoteDiv.innerHTML = formatMarkdown(quote);
    bodyDiv.appendChild(quoteDiv);
    popover.appendChild(bodyDiv);
  }

  // 5. Footer action buttons
  const footerDiv = document.createElementNS(XHTMLNS, "div");
  footerDiv.className = "evidence-popover-footer";

  if (attKey) {
    const readerBtn = document.createElementNS(XHTMLNS, "button");
    readerBtn.className = "evidence-popover-btn popover-open-reader-btn";
    readerBtn.setAttribute("data-attachment-key", attKey);
    if (sec) {
      readerBtn.setAttribute("data-section", sec);
    }
    readerBtn.setAttribute("type", "button");
    readerBtn.textContent = "Open in Reader";
    footerDiv.appendChild(readerBtn);
  }

  if (itemKey) {
    const selectBtn = document.createElementNS(XHTMLNS, "button");
    selectBtn.className = "evidence-popover-btn popover-select-item-btn";
    selectBtn.setAttribute("data-item-key", itemKey);
    selectBtn.setAttribute("type", "button");
    selectBtn.textContent = "Select in Library";
    footerDiv.appendChild(selectBtn);
  }

  popover.appendChild(footerDiv);

  // 6. Multi-directional resizers
  const resizerDirections = ["n", "s", "e", "w", "nw", "ne", "sw", "se"];
  for (const dir of resizerDirections) {
    const resizer = document.createElementNS(XHTMLNS, "div");
    resizer.className = `evidence-popover-resizer resizer-${dir}`;
    resizer.setAttribute("data-direction", dir);
    resizer.setAttribute("title", "Drag to resize");

    resizer.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();

      popover._isResizing = true;
      const startX = e.clientX;
      const startY = e.clientY;
      const rect = popover.getBoundingClientRect();
      const startLeft = parseFloat(popover.style.left) || rect.left || popover.offsetLeft || 0;
      const startTop = parseFloat(popover.style.top) || rect.top || popover.offsetTop || 0;
      const startWidth = parseFloat(popover.style.width) || popover.offsetWidth || rect.width || 320;
      const startHeight = parseFloat(popover.style.height) || popover.offsetHeight || rect.height || 220;

      const minWidth = 260;
      const minHeight = 180;
      const margin = 10;

      const onMouseMove = (moveEvt) => {
        if (popover.classList.contains("is-maximized")) {
          popover.classList.remove("is-maximized");
          maximizeBtn.textContent = "⤢";
          maximizeBtn.setAttribute("title", "Maximize in sidebar");
        }

        const deltaX = moveEvt.clientX - startX;
        const deltaY = moveEvt.clientY - startY;

        let newWidth = startWidth;
        let newLeft = startLeft;
        let newHeight = startHeight;
        let newTop = startTop;

        if (dir.includes("e")) {
          const maxW = Math.max(minWidth, window.innerWidth - startLeft - margin);
          newWidth = Math.min(maxW, Math.max(minWidth, startWidth + deltaX));
          popover.style.width = newWidth + "px";
        } else if (dir.includes("w")) {
          newLeft = Math.max(margin, Math.min(startLeft + startWidth - minWidth, startLeft + deltaX));
          newWidth = startWidth + (startLeft - newLeft);
          popover.style.left = newLeft + "px";
          popover.style.width = newWidth + "px";
        }

        if (dir.includes("s")) {
          const maxH = Math.max(minHeight, window.innerHeight - startTop - margin);
          newHeight = Math.min(maxH, Math.max(minHeight, startHeight + deltaY));
          popover.style.height = newHeight + "px";
          popover.style.maxHeight = newHeight + "px";
        } else if (dir.includes("n")) {
          newTop = Math.max(margin, Math.min(startTop + startHeight - minHeight, startTop + deltaY));
          newHeight = startHeight + (startTop - newTop);
          popover.style.top = newTop + "px";
          popover.style.height = newHeight + "px";
          popover.style.maxHeight = newHeight + "px";
        }

        const bodyEl = popover.querySelector(".evidence-popover-body");
        if (bodyEl) {
          bodyEl.style.maxHeight = "none";
        }
      };

      const onMouseUp = () => {
        popover._isResizing = false;
        popover._justResized = true;
        setTimeout(() => {
          if (popover) popover._justResized = false;
        }, 100);
        window.removeEventListener("mousemove", onMouseMove, true);
        window.removeEventListener("mouseup", onMouseUp, true);
      };

      window.addEventListener("mousemove", onMouseMove, true);
      window.addEventListener("mouseup", onMouseUp, true);
    });

    popover.appendChild(resizer);
  }

  document.body.appendChild(popover);

  // Position calculation and viewport clamping
  const margin = 10;
  const pillRect = pill.getBoundingClientRect();
  let left = pillRect.left;

  if (window.innerWidth < 420) {
    const popoverWidth = Math.max(260, window.innerWidth - 2 * margin);
    popover.style.width = popoverWidth + "px";
    left = margin;
  } else {
    const popoverWidth = 320;
    if (left + popoverWidth > window.innerWidth - margin) {
      left = Math.max(margin, window.innerWidth - popoverWidth - margin);
    }
    if (left < margin) left = margin;
  }

  const spaceBelow = window.innerHeight - pillRect.bottom - margin - 6;
  const spaceAbove = pillRect.top - margin - 6;
  const popoverHeight = popover.offsetHeight || 220;

  let top;
  let maxHeight;

  if (spaceBelow >= 180 || spaceBelow >= spaceAbove) {
    top = pillRect.bottom + 6;
    maxHeight = Math.min(340, Math.max(120, spaceBelow));
  } else {
    maxHeight = Math.min(340, Math.max(120, spaceAbove));
    top = Math.max(margin, pillRect.top - Math.min(popoverHeight, maxHeight) - 6);
  }

  popover.style.maxHeight = maxHeight + "px";
  popover.style.left = left + "px";
  popover.style.top = top + "px";
  activeEvidencePopover = popover;
}

// ---------------------------------------------------------------------------
// Chat Interface
// ---------------------------------------------------------------------------

function formatAuthorName(role, rawAuthor) {
  if (role === "user" || rawAuthor === "User") return "User";
  let name = (rawAuthor || "").trim();
  if (name.startsWith("Zocrates/")) {
    name = name.substring(9).trim();
  }
  const low = name.toLowerCase();
  if (AGENT_DISPLAY_NAMES[low]) {
    return AGENT_DISPLAY_NAMES[low];
  }
  if (!name || low === "zocrates" || low === "agent") {
    return "Zocrates";
  }

  // Remove any references to subagent / sub-agent (users should only see agents)
  name = name.replace(/sub[-_]?agent/gi, "agent");

  // Specific canonical mappings
  const normalized = name.toLowerCase().replace(/[-_]/g, " ").trim();
  const slug = name.toLowerCase().replace(/\s+/g, "_");
  if (AGENT_DISPLAY_NAMES[slug]) {
    return AGENT_DISPLAY_NAMES[slug];
  }
  if (normalized === "library synthesizer agent" || normalized === "library synthesizer" || normalized === "synthesizer") {
    return "Library Synthesis";
  }
  if (normalized === "library search agent" || normalized === "library search" || normalized === "search") {
    return "Library Search";
  }
  if (normalized === "coordinator agent" || normalized === "coordinator" ||
      isInternalWorkflowNode(name)) {
    return "Zocrates";
  }

  let formatted = name
    .split(/[-_]+/)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");

  formatted = formatted.replace(/\bSubagent\b/gi, "Agent");
  return formatted || "Zocrates";
}

function formatMarkdownForNote(text) {
  if (!text) return "";

  const placeholders = [];
  
  // 1. Extract block math: $$ ... $$
  let processedText = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, math) => {
    const placeholder = `@@BLOCKMATH_${placeholders.length}@@`;
    placeholders.push({
      placeholder: placeholder,
      type: 'block',
      math: math
    });
    return placeholder;
  });

  // 2. Extract inline math: $ ... $
  processedText = processedText.replace(/(?<!\$)\$(?!\s)((?:\\\$|[^$])+?)(?<!\s)\$(?!\$)/g, (match, math) => {
    const placeholder = `@@INLINEMATH_${placeholders.length}@@`;
    placeholders.push({
      placeholder: placeholder,
      type: 'inline',
      math: math
    });
    return placeholder;
  });

  let html = "";
  if (typeof window.marked !== 'undefined') {
    const noteRenderer = new window.marked.Renderer();
    noteRenderer.image = ({ href, title, text }) => {
      const safeSrc = isSafeUrl(href) ? escapeHTML(resolveAssetUrl(href)) : "";
      const safeAlt = escapeHTML(text || "");
      const safeTitle = escapeHTML(title || "");
      return `<img src="${safeSrc}" alt="${safeAlt}" title="${safeTitle}" loading="lazy" style="max-width: 100%; height: auto; border-radius: 6px; margin: 12px 0; display: block;" />`;
    };
    noteRenderer.code = (token) => {
      let codeText = (typeof token === 'object' && token.text !== undefined) ? token.text : (token || "");
      let rawLang = (typeof token === 'object' && token.lang !== undefined) ? token.lang : "";
      let language = (rawLang || "").trim().toLowerCase();

      const langAliases = {
        "py": "python",
        "python3": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "sh": "bash",
        "shell": "bash",
        "zsh": "bash",
        "c++": "cpp",
        "yml": "yaml",
        "html": "xml"
      };
      if (langAliases[language]) {
        language = langAliases[language];
      }

      let highlightedCode = "";
      let detectedLang = language;

      if (typeof window.hljs !== 'undefined') {
        if (language && window.hljs.getLanguage(language)) {
          try {
            highlightedCode = window.hljs.highlight(codeText, { language }).value;
          } catch (e) {
            highlightedCode = escapeHTML(codeText);
          }
        } else if (language) {
          highlightedCode = escapeHTML(codeText);
        } else {
          try {
            const autoRes = window.hljs.highlightAuto(codeText);
            if (autoRes && autoRes.relevance >= 3 && autoRes.language) {
              highlightedCode = autoRes.value;
              detectedLang = autoRes.language;
            } else {
              highlightedCode = escapeHTML(codeText);
              detectedLang = "code";
            }
          } catch (e) {
            highlightedCode = escapeHTML(codeText);
            detectedLang = "code";
          }
        }
      } else {
        highlightedCode = escapeHTML(codeText);
      }

      const displayLang = detectedLang ? detectedLang.toLowerCase() : "code";

      return `<div class="code-block-wrapper">` +
        `<div class="code-block-header">` +
          `<span class="code-lang-label">${escapeHTML(displayLang)}</span>` +
        `</div>` +
        `<pre><code class="hljs ${escapeHTML(displayLang)}">${highlightedCode}</code></pre>` +
      `</div>`;
    };

    html = window.marked.parse(processedText, { renderer: noteRenderer, gfm: true, breaks: true });
  } else {
    html = processedText.replace(/\n/g, "<br />");
  }

  // Replace math placeholders with clean HTML code elements preserving delimiters for Zotero notes
  for (const item of placeholders) {
    let noteMathHtml = "";
    if (item.type === 'block') {
      noteMathHtml = `<pre class="note-math-block"><code>$$\n${escapeHTML(item.math)}\n$$</code></pre>`;
    } else {
      noteMathHtml = `<code class="note-math-inline">$${escapeHTML(item.math)}$</code>`;
    }
    html = html.replace(item.placeholder, () => noteMathHtml);
  }

  return sanitizeHTML(htmlToXhtml(html));
}

/**
 * Creates and saves a note item in Zotero containing the specified text.
 * If targetParentKey is provided, attaches the note as a child note to the parent paper item.
 */
/**
 * Creates and saves a note item in Zotero containing the specified text.
 * - targetPaperKeys: array of Zotero paper item keys to link via addRelatedItem()
 * - targetCollectionKeys: array of collection keys/names/IDs to add note to via addToCollection()
 */
/**
 * Extracts a concise message title from the message body.
 * Used for titling Zotero notes saved from individual messages.
 */
function extractMessageTitle(text, role = "assistant") {
  if (!text || typeof text !== "string") {
    return role === "user" ? "User Query" : "Response";
  }

  const cleanText = text.trim();
  if (!cleanText) {
    return role === "user" ? "User Query" : "Response";
  }

  // Priority 1: First markdown heading (# Header, ## Header, etc.)
  const headingMatch = cleanText.match(/^#{1,6}\s+(.+)$/m);
  if (headingMatch && headingMatch[1]) {
    let heading = headingMatch[1]
      .replace(/\s*#+\s*$/, "") // strip closing markdown hashes like '## Title ##'
      .replace(/[*_`]/g, "")    // strip bold, italic, code markdown
      .replace(/[:\s]+$/, "")
      .trim();
    if (heading.length > 0) {
      return heading.length > 80 ? heading.substring(0, 77) + "..." : heading;
    }
  }

  // Priority 2: First non-empty line starting with bold text (**Header**)
  const lines = cleanText.split("\n").map(l => l.trim()).filter(Boolean);
  for (const line of lines) {
    const boldMatch = line.match(/^\*\*([^*]+)\*\*[:\s]*(.*)$/);
    if (boldMatch && boldMatch[1]) {
      let boldHeader = boldMatch[1]
        .replace(/[*_`]/g, "")
        .replace(/[:\s]+$/, "")
        .trim();
      if (boldHeader.length > 0) {
        return boldHeader.length > 80 ? boldHeader.substring(0, 77) + "..." : boldHeader;
      }
    }
  }

  // Priority 3: First line fallback (strip markdown and links, truncate to 60 chars)
  if (lines.length > 0) {
    let firstLine = lines[0]
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/\s*#+\s*$/, "")
      .replace(/[*_`]/g, "")
      .trim();
    if (firstLine.length > 0) {
      return firstLine.length > 60 ? firstLine.substring(0, 57) + "..." : firstLine;
    }
  }

  return role === "user" ? "User Query" : "Response";
}

/**
 * Creates and saves a note item in Zotero containing the specified text.
 * - targetPaperKey: single Zotero paper item key to set as parentID (attaches note under paper)
 * - targetCollectionKey: single collection key/name/ID to add note to via addToCollection()
 */
async function saveZoteroNote(noteText, noteTitle = "Zocrates Note", targetPaperKey = null, targetCollectionKey = null, tags = []) {
  if (!Zotero) {
    throw new Error("Zotero application scope is unavailable");
  }

  const pane = resolveZoteroPane();
  const libraryID = resolveActiveLibraryID(pane);

  // Instantiate a new Zotero Item of type 'note'
  const item = new Zotero.Item('note');
  if (libraryID) {
    item.libraryID = libraryID;
  }

  // Format HTML for static Zotero notes
  const formattedHtml = formatMarkdownForNote(noteText);
  const fullNoteHtml = `<h2>${escapeHTML(noteTitle)}</h2>\n${formattedHtml}`;
  item.setNote(fullNoteHtml);

  // 1. If targetPaperKey is specified, set parentID so the note appears directly UNDER the paper!
  let parentPaperTitle = null;
  if (targetPaperKey) {
    try {
      let parentItem = null;
      if (Zotero.Items?.getByLibraryAndKey) {
        parentItem = Zotero.Items.getByLibraryAndKey(libraryID, targetPaperKey);
      }
      if (!parentItem && Zotero.Items?.get) {
        const allItems = Zotero.Items.getAll?.(libraryID) || [];
        parentItem = allItems.find(i => i.key === targetPaperKey);
      }
      if (parentItem && parentItem.id) {
        item.parentID = parentItem.id;
        parentPaperTitle = parentItem.getField?.('title') || parentItem.title || null;
      }
    } catch (err) {
      console.warn("Zocrates: Failed to locate paper parent item:", targetPaperKey, err);
    }
  }

  // 2. If targetCollectionKey is specified, attach note to that collection/subcollection
  let addedToCollectionName = null;
  if (targetCollectionKey) {
    try {
      let colObj = null;
      if (Zotero.Collections?.getByLibraryAndKey) {
        colObj = Zotero.Collections.getByLibraryAndKey(libraryID, targetCollectionKey);
      }
      if (!colObj && (typeof targetCollectionKey === "number" || /^\d+$/.test(targetCollectionKey)) && Zotero.Collections?.get) {
        colObj = Zotero.Collections.get(Number(targetCollectionKey));
      }
      if (!colObj && Zotero.Collections?.getByLibrary) {
        const collections = Zotero.Collections.getByLibrary(libraryID) || [];
        colObj = collections.find(c => c.key === targetCollectionKey || String(c.id) === String(targetCollectionKey) || c.name === targetCollectionKey);
      }
      if (colObj && colObj.id) {
        item.addToCollection(colObj.id);
        addedToCollectionName = colObj.name || null;
      }
    } catch (err) {
      console.warn("Zocrates: Failed to attach note to collection:", targetCollectionKey, err);
    }
  }

  // Add tags if provided
  if (Array.isArray(tags)) {
    tags.forEach(t => {
      if (t && typeof item.addTag === "function") {
        item.addTag(t);
      }
    });
  }

  // Save the note item to Zotero DB
  if (typeof item.saveTx === "function") {
    await item.saveTx();
  } else if (typeof item.save === "function") {
    await item.save();
  } else {
    throw new Error("Zotero item save method unavailable");
  }

  return { item, parentPaperTitle, addedToCollectionName };
}

/**
 * Triggers the Save Note flow.
 * Presents a single-click popover menu with Standalone Note option, papers in context,
 * and an expandable collection tree for subcollections.
 */
function triggerSaveNoteFlow(anchorBtn, noteText, noteTitle, tags = []) {
  // Remove any existing save-note-popover
  document.querySelectorAll(".save-note-popover").forEach(el => el.remove());

  const paperChips = activeItemsList.filter(item => (item.type === "item" || item.type === "paper" || !item.type) && item.key);
  const collectionChips = activeItemsList.filter(item => item.type === "collection");
  const hasLibraryChip = activeItemsList.some(item => item.type === "library");

  // If context is "library" mode or no specific paper/collection chips exist, save directly as standalone note
  if (contextMode === "library" || hasLibraryChip || (paperChips.length === 0 && collectionChips.length === 0)) {
    executeSaveNote(anchorBtn, noteText, noteTitle, null, null, tags);
    return;
  }

  // Create popover menu container
  const popover = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  popover.className = "save-note-popover";

  // Option 1: Standalone Note (Always top item)
  const standaloneBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  standaloneBtn.type = "button";
  standaloneBtn.className = "popover-item";
  standaloneBtn.innerHTML = `<span style="font-size:12px;">📝</span> <span class="item-text">Standalone Note (My Library)</span>`;
  standaloneBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.remove();
    executeSaveNote(anchorBtn, noteText, noteTitle, null, null, tags);
  });
  popover.appendChild(standaloneBtn);

  // Section 2: Papers in Context (if paper chips exist)
  if (paperChips.length > 0) {
    const divider = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    divider.className = "popover-divider";
    popover.appendChild(divider);

    const secHeader = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    secHeader.className = "popover-section-header";
    secHeader.textContent = "Attach to paper:";
    popover.appendChild(secHeader);

    paperChips.forEach(paper => {
      const paperBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
      paperBtn.type = "button";
      paperBtn.className = "popover-item";
      paperBtn.title = "Attach note directly under: " + paper.title;
      paperBtn.innerHTML = `<span style="font-size:12px;">📄</span> <span class="item-text">${escapeHTML(paper.title)}</span>`;
      paperBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        popover.remove();
        executeSaveNote(anchorBtn, noteText, noteTitle, paper.key, null, tags);
      });
      popover.appendChild(paperBtn);
    });
  }

  // Section 3: Collections & Subcollections in Context (if collection chips exist)
  if (collectionChips.length > 0) {
    const divider = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    divider.className = "popover-divider";
    popover.appendChild(divider);

    const secHeader = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    secHeader.className = "popover-section-header";
    secHeader.textContent = "Save to collection:";
    popover.appendChild(secHeader);

    const treeContainer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    treeContainer.className = "tree-container";

    try {
      const pane = resolveZoteroPane();
      const libraryID = resolveActiveLibraryID(pane);
      const rawCollections = fetchAllCollectionsInLibrary(libraryID);

      const colMap = new Map();
      rawCollections.forEach(col => colMap.set(col.id, col));

      const parentIdsWithChildren = new Set();
      rawCollections.forEach(col => {
        const pId = getParentId(col, colMap);
        if (pId) parentIdsWithChildren.add(pId);
      });

      const enrichedCollections = rawCollections.map(col => {
        const info = getPathAndDepth(col, colMap);
        const pId = getParentId(col, colMap);
        return {
          col: col,
          id: col.id,
          key: col.key,
          name: col.name,
          parentId: pId,
          hasChildren: parentIdsWithChildren.has(col.id),
          fullPath: info.fullPath,
          depth: info.depth
        };
      });

      // Filter collections to include collections in context and all their subcollection descendants
      const contextColIds = new Set(
        enrichedCollections
          .filter(c => collectionChips.some(chip => chip.collectionKey === c.key || chip.collectionId === c.id || chip.key === c.name || chip.key === c.key))
          .map(c => c.id)
      );

      const allowedColIds = new Set(contextColIds);
      let addedMore = true;
      while (addedMore) {
        addedMore = false;
        for (const c of enrichedCollections) {
          if (!allowedColIds.has(c.id) && c.parentId && allowedColIds.has(c.parentId)) {
            allowedColIds.add(c.id);
            addedMore = true;
          }
        }
      }

      const visibleCollections = enrichedCollections.filter(c => allowedColIds.has(c.id));
      visibleCollections.sort((a, b) => a.fullPath.localeCompare(b.fullPath));

      visibleCollections.forEach(colInfo => {
        const col = colInfo.col;
        const colItem = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        colItem.className = "popover-item";
        colItem.style.paddingLeft = "4px";
        colItem.title = colInfo.fullPath;

        const folderIcon = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        folderIcon.style.cssText = "font-size:12px;flex-shrink:0;";
        folderIcon.textContent = colInfo.depth > 0 ? "📂" : "📁";
        colItem.appendChild(folderIcon);

        const nameSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        nameSpan.className = "item-text";
        nameSpan.textContent = colInfo.fullPath;
        colItem.appendChild(nameSpan);

        colItem.addEventListener("click", (e) => {
          e.stopPropagation();
          popover.remove();
          executeSaveNote(anchorBtn, noteText, noteTitle, null, col.key || col.id, tags);
        });

        treeContainer.appendChild(colItem);
      });
    } catch (err) {
      console.error("Zocrates: Error populating popover collection tree:", err);
    }

    popover.appendChild(treeContainer);
  }

  document.body.appendChild(popover);

  // Position relative to anchorBtn
  const rect = anchorBtn.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();

  let top = rect.bottom + 4;
  if (top + popoverRect.height > window.innerHeight - 10) {
    top = Math.max(10, rect.top - popoverRect.height - 4);
  }
  let left = Math.min(rect.left, window.innerWidth - popoverRect.width - 10);
  left = Math.max(10, left);

  popover.style.top = top + "px";
  popover.style.left = left + "px";

  const closeHandler = (e) => {
    if (!popover.contains(e.target) && e.target !== anchorBtn) {
      popover.remove();
      document.removeEventListener("click", closeHandler);
    }
  };
  setTimeout(() => document.addEventListener("click", closeHandler), 10);
}

async function executeSaveNote(anchorBtn, noteText, noteTitle, targetPaperKey = null, targetCollectionKey = null, tags = []) {
  try {
    anchorBtn.disabled = true;
    const result = await saveZoteroNote(noteText, noteTitle, targetPaperKey, targetCollectionKey, tags);

    const noteIcon = anchorBtn.querySelector(".note-icon");
    const checkIcon = anchorBtn.querySelector(".check-icon");
    if (noteIcon && checkIcon) {
      noteIcon.classList.add("hidden");
      checkIcon.classList.remove("hidden");
      setTimeout(() => {
        noteIcon.classList.remove("hidden");
        checkIcon.classList.add("hidden");
        anchorBtn.disabled = false;
      }, 2000);
    }

    if (result.parentPaperTitle) {
      addNotification(`Saved "${noteTitle}" attached directly under "${result.parentPaperTitle}"!`, "success");
    } else if (result.addedToCollectionName) {
      addNotification(`Saved "${noteTitle}" to collection "${result.addedToCollectionName}"!`, "success");
    } else {
      addNotification(`Saved "${noteTitle}" as a standalone note in main Zotero library!`, "success");
    }
  } catch (err) {
    console.error("Zocrates: Failed to save Zotero note:", err);
    addNotification("Failed to save note: " + err.message, "error");
    anchorBtn.disabled = false;
  }
}

function getZoteroUsername() {
  try {
    const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zot) {
      if (zot.User && zot.User.name) return zot.User.name;
      if (zot.Users && zot.Users.getCurrentUser?.()?.name) return zot.Users.getCurrentUser().name;
      if (zot.Prefs && zot.Prefs.get) {
        const syncUsername = zot.Prefs.get("sync.server.username");
        if (syncUsername && typeof syncUsername === "string" && syncUsername.trim()) {
          return syncUsername.trim();
        }
      }
    }
  } catch (e) {
    console.warn("Zocrates: Failed to retrieve Zotero account username:", e);
  }
  return "User";
}

function openMessageInfoModal(meta) {
  if (!meta) return;
  document.querySelectorAll(".info-modal-overlay").forEach(el => el.remove());
  if (document._zocratesInfoEscHandler) {
    document.removeEventListener("keydown", document._zocratesInfoEscHandler);
    document._zocratesInfoEscHandler = null;
  }

  const overlay = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  overlay.className = "modal-overlay info-modal-overlay";

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "modal-card";
  card.style.maxWidth = "440px";
  card.style.width = "100%";

  const header = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  header.className = "modal-header";

  const title = document.createElementNS("http://www.w3.org/1999/xhtml", "h3");
  title.className = "modal-title";
  title.textContent = "Message Information";

  const closeBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  closeBtn.className = "modal-close-btn";
  closeBtn.textContent = "✕";
  closeBtn.title = "Close (Esc)";

  function closeModal() {
    overlay.remove();
    if (document._zocratesInfoEscHandler) {
      document.removeEventListener("keydown", document._zocratesInfoEscHandler);
      document._zocratesInfoEscHandler = null;
    }
  }

  function handleEsc(e) {
    if (e.key === "Escape") closeModal();
  }

  document._zocratesInfoEscHandler = handleEsc;
  closeBtn.addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener("keydown", handleEsc);

  header.appendChild(title);
  header.appendChild(closeBtn);

  const body = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  body.className = "modal-body";

  const table = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  table.className = "info-meta-table";

  function renderRows(currentMeta) {
    table.innerHTML = "";

    function addRow(label, valueContent, isNode = false) {
      if (!valueContent && valueContent !== 0) return;
      const row = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      row.className = "info-meta-row";

      const labelEl = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      labelEl.className = "info-meta-label";
      labelEl.textContent = label;

      const valEl = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      valEl.className = "info-meta-value";
      if (isNode) {
        valEl.appendChild(valueContent);
      } else {
        valEl.textContent = String(valueContent);
      }

      row.appendChild(labelEl);
      row.appendChild(valEl);
      table.appendChild(row);
    }

    const role = currentMeta.role || (currentMeta.author === "user" || currentMeta.author === "User" ? "user" : "assistant");
    const authorDisplay = role === "user" ? (getZoteroUsername() || "User") : formatAuthorName(role, currentMeta.author);
    addRow("Author / Role", authorDisplay);

    const agentModels = currentMeta.agent_models || currentMeta.usage?.agent_models || null;
    let filteredAgentModels = {};
    if (agentModels && typeof agentModels === "object") {
      for (const [k, v] of Object.entries(agentModels)) {
        if (!isInternalWorkflowNode(k) && k.toLowerCase() !== "user" && v) {
          filteredAgentModels[k] = v;
        }
      }
    }

    const distinctModels = new Set(Object.values(filteredAgentModels));
    if (distinctModels.size > 1) {
      const modelsBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      modelsBox.style.cssText = "display:flex;flex-direction:column;gap:4px;align-items:flex-end;";
      const roleTitles = {
        coordinator_agent: "Zocrates",
        coordinator: "Zocrates",
        library_search_agent: "Library Search",
        library_search: "Library Search",
        library_synthesizer_agent: "Library Synthesis",
        library_synthesizer: "Library Synthesis",
        online_search_agent: "Online Discovery",
        online_search: "Online Discovery",
        online_synthesizer_agent: "Discovery Synthesis",
        online_synthesizer: "Discovery Synthesis"
      };
      for (const [agentKey, modelVal] of Object.entries(filteredAgentModels)) {
        const roleLabel = roleTitles[agentKey] || formatAuthorName("assistant", agentKey);
        const rowItem = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        rowItem.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11px;";
        const roleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        roleSpan.style.cssText = "color:var(--text-muted);font-size:10.5px;";
        roleSpan.textContent = roleLabel + ":";
        const pill = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        pill.className = "info-meta-pill";
        pill.textContent = modelVal;
        pill.title = modelVal;
        rowItem.appendChild(roleSpan);
        rowItem.appendChild(pill);
        modelsBox.appendChild(rowItem);
      }
      addRow("Agent Models", modelsBox, true);
    } else {
      const singleModel = distinctModels.size === 1
        ? Array.from(distinctModels)[0]
        : (currentMeta.model || Object.values(filteredAgentModels)[0] || null);
      if (singleModel) {
        const modelPill = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        modelPill.className = "info-meta-pill";
        modelPill.textContent = singleModel;
        addRow("Model", modelPill, true);
      }
    }

    if (currentMeta.timestamp) {
      let timeStr = currentMeta.timestamp;
      if (typeof currentMeta.timestamp === "number") {
        const ms = currentMeta.timestamp < 1e11 ? currentMeta.timestamp * 1000 : currentMeta.timestamp;
        timeStr = new Date(ms).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
      }
      addRow("Timestamp", timeStr);
    }

    if (currentMeta.duration !== null && currentMeta.duration !== undefined) {
      addRow("Duration", `${currentMeta.duration}s`);
    }

    if (currentMeta.finish_reason) {
      addRow("Finish Reason", currentMeta.finish_reason);
    }

    // Turn Cost (rounded to nearest cent, with exact in tooltip)
    const turnCost = currentMeta.turn_cost_usd !== undefined && currentMeta.turn_cost_usd !== null
      ? currentMeta.turn_cost_usd
      : (currentMeta.usage?.turn_cost_usd !== undefined && currentMeta.usage?.turn_cost_usd !== null
        ? currentMeta.usage.turn_cost_usd
        : (currentMeta.usage?.cost_usd !== undefined && currentMeta.usage?.cost_usd !== null
          ? currentMeta.usage.cost_usd
          : null));

    if (turnCost !== null && turnCost !== undefined) {
      const costBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      costBox.style.cssText = "display:flex;flex-direction:column;align-items:flex-end;";
      const costVal = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      costVal.style.cssText = "font-family:monospace;font-size:11.5px;font-weight:600;color:var(--text-main);";
      costVal.textContent = formatCostUsd(turnCost);
      costVal.title = `Exact: $${Number(turnCost).toFixed(6)}`;
      costBox.appendChild(costVal);
      addRow("Turn Cost", costBox, true);
    }

    if (currentMeta.usage) {
      const u = currentMeta.usage;
      const tokens = [];
      if (u.prompt_tokens) tokens.push(`Input: ${Number(u.prompt_tokens).toLocaleString()}`);
      if (u.candidates_tokens) tokens.push(`Output: ${Number(u.candidates_tokens).toLocaleString()}`);
      if (u.thoughts_tokens) tokens.push(`Reasoning: ${Number(u.thoughts_tokens).toLocaleString()}`);
      if (u.cached_tokens) tokens.push(`Cached: ${Number(u.cached_tokens).toLocaleString()}`);
      if (u.total_tokens) tokens.push(`Total: ${Number(u.total_tokens).toLocaleString()}`);

      if (tokens.length > 0) {
        const usageBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        usageBox.style.cssText = "display:flex;flex-direction:column;gap:2px;align-items:flex-end;";
        tokens.forEach(t => {
          const item = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
          item.style.cssText = "font-size:11px;font-family:monospace;";
          item.textContent = t;
          usageBox.appendChild(item);
        });
        addRow("Token Usage", usageBox, true);
      }
    }

    const verif = currentMeta.citation_verification && typeof currentMeta.citation_verification === "object" ? currentMeta.citation_verification : null;
    const rawCitations = Array.isArray(currentMeta.citations) ? currentMeta.citations : [];
    const hasVerificationTelemetry = Boolean(verif && (verif.summary || (Array.isArray(verif.assessments) && verif.assessments.length > 0)));
    const hasNumericCitationProbs = rawCitations.some(c =>
      typeof c?.support_probability === "number" ||
      (Array.isArray(c?.occurrences) && c.occurrences.some(o => typeof o?.support_probability === "number"))
    );

    if (hasVerificationTelemetry || hasNumericCitationProbs) {
      const citations = rawCitations;
      const summary = verif?.summary || {};
      const assessments = Array.isArray(verif?.assessments) ? verif.assessments : [];

      const provider = verif?.provider || "openrouter";
      const model = verif?.model || (provider === "local_laya" ? "laya" : null) || currentMeta.model || "Decision Model";
      const latency = typeof summary.latency_ms === "number" ? summary.latency_ms : (typeof verif?.latency_ms === "number" ? verif.latency_ms : null);
      const threshold = typeof verif?.threshold === "number"
        ? verif.threshold
        : (typeof summary.threshold === "number" ? summary.threshold : 0.60);

      let probs = assessments
        .map(a => a.support_probability)
        .filter(p => typeof p === "number" && !isNaN(p));
      if (probs.length === 0 && citations.length > 0) {
        probs = citations
          .flatMap(c => (c.occurrences && c.occurrences.length > 0 ? c.occurrences : [{ support_probability: c.support_probability }]))
          .map(o => o?.support_probability)
          .filter(p => typeof p === "number" && !isNaN(p));
      }

      const total = summary.batch_size !== undefined
        ? summary.batch_size
        : (assessments.length > 0 ? assessments.length : (probs.length > 0 ? probs.length : citations.length));

      let kept = summary.kept;
      if (kept === undefined) {
        if (assessments.length > 0) {
          kept = assessments.filter(a => a.verdict === "KEPT" || (typeof a.support_probability === "number" && a.support_probability >= threshold)).length;
        } else if (citations.length > 0) {
          const occs = citations.flatMap(c => c.occurrences || (c.support_probability !== undefined ? [{ support_probability: c.support_probability, survived: c.support_probability >= threshold }] : []));
          kept = occs.filter(o => o.survived || (typeof o.support_probability === "number" && o.support_probability >= threshold)).length;
        } else {
          kept = 0;
        }
      }

      let demoted = summary.demoted;
      if (demoted === undefined) {
        if (assessments.length > 0) {
          demoted = assessments.filter(a => a.verdict === "DEMOTED" || (typeof a.support_probability === "number" && a.support_probability < threshold)).length;
        } else if (citations.length > 0) {
          const occs = citations.flatMap(c => c.occurrences || (c.support_probability !== undefined ? [{ support_probability: c.support_probability, survived: c.support_probability >= threshold }] : []));
          demoted = occs.filter(o => o.survived === false || (typeof o.support_probability === "number" && o.support_probability < threshold)).length;
        } else {
          demoted = 0;
        }
      }

      let failOpen = summary.fail_open;
      if (failOpen === undefined) {
        if (assessments.length > 0) {
          failOpen = assessments.filter(a => a.verdict === "FAIL_OPEN" || a.support_probability === null || a.support_probability === undefined).length;
        } else {
          failOpen = 0;
        }
      }

      let meanVal = summary.mean_support !== undefined && summary.mean_support !== null
        ? summary.mean_support
        : (probs.length > 0 ? probs.reduce((a, b) => a + b, 0) / probs.length : null);

      let stdVal = summary.std_support !== undefined && summary.std_support !== null
        ? summary.std_support
        : (probs.length >= 2
            ? Math.sqrt(probs.reduce((sum, p) => sum + Math.pow(p - meanVal, 2), 0) / (probs.length - 1))
            : (probs.length === 1 ? 0 : null));

      let meanPText = "Mean p: N/A";
      const numProbs = probs.length;
      if (numProbs > 0 && meanVal !== null && meanVal !== undefined) {
        const mClamped = Math.max(0, Math.min(1, Number(meanVal))).toFixed(3);
        if (numProbs === 1) {
          meanPText = `Mean p: ${mClamped} ± N/A (std)`;
        } else if (numProbs >= 2 && stdVal !== null && stdVal !== undefined) {
          const sClamped = Math.max(0, Math.min(1, Number(stdVal))).toFixed(3);
          meanPText = `Mean p: ${mClamped} ± ${sClamped} (std)`;
        } else {
          meanPText = `Mean p: ${mClamped} ± N/A (std)`;
        }
      }

      const row = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      row.className = "info-meta-row citation-support-row";

      const titleRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      titleRow.className = "citation-support-title-row";

      const labelEl = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      labelEl.className = "info-meta-label";
      labelEl.textContent = "Citation Support";

      const pill = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      pill.className = "info-meta-pill citation-support-pill";
      const latencyStr = typeof latency === "number" ? `${Math.round(latency)}ms` : "";
      pill.textContent = latencyStr ? `${model} · ${latencyStr}` : model;
      pill.title = `${model}${latencyStr ? ` (${latencyStr})` : ""}`;

      titleRow.appendChild(labelEl);
      titleRow.appendChild(pill);
      row.appendChild(titleRow);

      const valEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      valEl.className = "info-meta-value citation-support-value";

      const threshFmt = Number(threshold).toFixed(3);

      const statsBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      statsBox.className = "citation-support-stats";

      const meanEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      meanEl.className = "citation-support-mean";
      meanEl.textContent = meanPText;
      statsBox.appendChild(meanEl);

      const countsBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      countsBox.className = "citation-support-counts";
      countsBox.textContent = `${total} claims evaluated: ${kept} verified (p ≥ ${threshFmt}), ${demoted} rejected (p < ${threshFmt})${failOpen > 0 ? `, ${failOpen} unverified` : ""}`;
      statsBox.appendChild(countsBox);

      if (Array.isArray(verif?.assessments)) {
        let saCount = 0;
        let saTokens = 0;
        let saCost = 0.0;
        verif.assessments.forEach(a => {
          const sa = a?.sentence_attribution;
          if (sa && sa.status === "ok") {
            saCount++;
            const u = sa.usage || {};
            const p = Number(u.prompt_tokens || 0);
            const c = Number(u.completion_tokens || 0);
            saTokens += Number(u.total_tokens || (p + c) || 0);
            saCost += Number(sa.cost_usd || 0.0);
          }
        });
        if (saCount > 0) {
          const saRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
          saRow.className = "citation-support-attribution";
          saRow.textContent = `Sentence Attribution: ${saCount} evaluated (${saTokens.toLocaleString()} tokens${saCost > 0 ? ` • $${saCost.toFixed(5)}` : " • Free"})`;
          statsBox.appendChild(saRow);
        }
      }

      valEl.appendChild(statsBox);

      row.appendChild(valEl);
      table.appendChild(row);
    }

    if (currentMeta.active_context_items && Array.isArray(currentMeta.active_context_items) && currentMeta.active_context_items.length > 0) {
      const list = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      list.className = "info-meta-chips-list";
      currentMeta.active_context_items.forEach(ci => {
        const chipEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        chipEl.className = "info-meta-chip-item";
        let title = typeof ci === "string" ? ci : (ci.title || ci.name || ci.key || "Item");
        let icon = "📄";
        if (typeof ci === "object" && ci) {
          if (ci.type === "collection" || ci.collectionKey) icon = "📁";
          else if (ci.type === "library" || ci.key === "all") icon = "📚";
        }
        chipEl.textContent = `${icon} ${title}`;
        chipEl.title = title;
        list.appendChild(chipEl);
      });
      addRow("Context Items", list, true);
    }

    if (currentMeta.snippets && Array.isArray(currentMeta.snippets) && currentMeta.snippets.length > 0) {
      const list = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      list.className = "info-meta-chips-list";
      currentMeta.snippets.forEach(snip => {
        const chipEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        chipEl.className = "info-meta-chip-item";

        const isImage = snip.type === "image";
        const icon = isImage ? "🖼️" : "📄";

        let authorTag = (snip.author_str || snip.authorStr || "").trim();
        if (!authorTag && Array.isArray(snip.authors) && snip.authors.length > 0) {
          if (snip.authors.length === 1) authorTag = snip.authors[0];
          else if (snip.authors.length === 2) authorTag = snip.authors[0] + " & " + snip.authors[1];
          else authorTag = snip.authors[0] + " et al.";
        }
        const paperLabel = snip.paper_title || snip.paperTitle || "Paper";
        if (!authorTag) {
          authorTag = paperLabel.length > 20 ? paperLabel.slice(0, 19) + "…" : paperLabel;
        }

        const pageStr = snip.page ? ` (p.${snip.page})` : "";
        let excerpt = isImage ? "Area Crop" : "";
        if (!isImage) {
          const rawQuote = (snip.text || "").trim();
          const truncQuote = rawQuote.length > 35 ? rawQuote.slice(0, 33) + "…" : rawQuote;
          excerpt = truncQuote ? `"${truncQuote}"` : "Excerpt";
        }

        chipEl.textContent = `${icon} ${authorTag}${pageStr}: ${excerpt}`;

        let tooltip = (authorTag ? `${authorTag} · ` : "") + paperLabel;
        if (snip.page) tooltip += ` · Page ${snip.page}`;
        if (snip.publication) tooltip += ` · ${snip.publication}`;
        if (snip.year) tooltip += ` (${snip.year})`;
        if (!isImage && snip.text) {
          tooltip += `\n\n"${snip.text.trim()}"`;
        } else if (isImage) {
          tooltip += `\n\n[Area Crop]`;
        }
        chipEl.title = tooltip;

        list.appendChild(chipEl);
      });
      addRow("Focal Snippets", list, true);
    }

    if (currentMeta.intermediate_steps && Array.isArray(currentMeta.intermediate_steps) && currentMeta.intermediate_steps.length > 0) {
      const traceBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      traceBox.style.cssText = "display:flex;align-items:center;justify-content:flex-end;gap:6px;width:100%;";
      const traceCount = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      traceCount.style.cssText = "font-size:11px;color:var(--text-muted);";
      traceCount.textContent = `${currentMeta.intermediate_steps.length} steps`;
      const viewBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
      viewBtn.className = "modal-btn-cancel";
      viewBtn.style.cssText = "font-size:10.5px;padding:2px 8px;cursor:pointer;background:rgba(255,255,255,0.08);border:1px solid var(--border);border-radius:4px;color:var(--text-main);";
      viewBtn.textContent = "View Trace";
      viewBtn.addEventListener("click", () => {
        openTraceWindow(currentMeta.intermediate_steps, currentMeta);
      });
      traceBox.appendChild(traceCount);
      traceBox.appendChild(viewBtn);
      addRow("Execution Trace", traceBox, true);
    }

    if (currentMeta.event_id || currentMeta.invocation_id) {
      const idsBox = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      idsBox.style.cssText = "display:flex;flex-direction:column;gap:3px;align-items:flex-end;width:100%;";
      if (currentMeta.event_id) {
        const eid = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        eid.style.cssText = "font-size:10px;font-family:monospace;color:var(--text-muted);word-break:break-all;user-select:all;text-align:right;line-height:1.35;";
        eid.textContent = `Event: ${currentMeta.event_id}`;
        eid.title = currentMeta.event_id;
        idsBox.appendChild(eid);
      }
      if (currentMeta.invocation_id) {
        const iid = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        iid.style.cssText = "font-size:10px;font-family:monospace;color:var(--text-muted);word-break:break-all;user-select:all;text-align:right;line-height:1.35;";
        iid.textContent = `Invocation: ${currentMeta.invocation_id}`;
        iid.title = currentMeta.invocation_id;
        idsBox.appendChild(iid);
      }
      addRow("Identifiers", idsBox, true);
    }
  }

  renderRows(meta);

  // Asynchronously fetch fresh session DB record if token usage or model is missing
  if (currentSessionId && (!meta.usage || !meta.model)) {
    fetch(`${API_BASE}/sessions/${currentSessionId}`, { headers: getAuthHeaders() })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data || !data.session || !Array.isArray(data.session.messages)) return;
        const matchingMsg = data.session.messages.find(m =>
          (meta.event_id && m.id === meta.event_id) ||
          (meta.text && m.text && m.text.substring(0, 60) === meta.text.substring(0, 60))
        );
        if (matchingMsg) {
          if (matchingMsg.model) meta.model = matchingMsg.model;
          if (matchingMsg.usage) meta.usage = matchingMsg.usage;
          if (matchingMsg.turn_cost_usd !== undefined) meta.turn_cost_usd = matchingMsg.turn_cost_usd;
          if (matchingMsg.agent_models) meta.agent_models = matchingMsg.agent_models;
          if (matchingMsg.finish_reason) meta.finish_reason = matchingMsg.finish_reason;
          if (matchingMsg.active_context_items) meta.active_context_items = matchingMsg.active_context_items;
          if (matchingMsg.snippets) meta.snippets = matchingMsg.snippets;
          if (matchingMsg.intermediate_steps) meta.intermediate_steps = matchingMsg.intermediate_steps;
          if (matchingMsg.id) meta.event_id = matchingMsg.id;
          if (matchingMsg.invocation_id) meta.invocation_id = matchingMsg.invocation_id;
          if (matchingMsg.citation_verification) meta.citation_verification = matchingMsg.citation_verification;
          if (matchingMsg.citations) meta.citations = matchingMsg.citations;
          renderRows(meta);
        }
      })
      .catch(err => console.warn("Zocrates: Error fetching session DB details:", err));
  }

  body.appendChild(table);

  const footer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  footer.className = "modal-footer";
  footer.style.cssText = "display:flex;justify-content:flex-end;margin-top:8px;";

  const okBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  okBtn.className = "modal-btn-cancel";
  okBtn.textContent = "Close";
  okBtn.addEventListener("click", closeModal);

  footer.appendChild(okBtn);

  card.appendChild(header);
  card.appendChild(body);
  card.appendChild(footer);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

function openTraceWindow(intermediateSteps, metadata = {}) {
  const steps = Array.isArray(intermediateSteps) ? intermediateSteps : [];
  const meta = Object.assign({}, metadata || {}, {
    theme: document.body.getAttribute("data-theme") || "slate"
  });

  try {
    if (window.top) window.top._zocratesCurrentTraceData = { steps, metadata: meta };
    window._zocratesCurrentTraceData = { steps, metadata: meta };
    const Zotero = window.Zotero || window.top?.Zotero;
    if (Zotero?.Zocrates) Zotero.Zocrates._currentTraceData = { steps, metadata: meta };
  } catch (e) {}

  try {
    const mainWin = window.top || window.parent || window;
    if (mainWin && typeof mainWin.openDialog === "function") {
      const win = mainWin.openDialog(
        "chrome://zocrates/content/trace.xhtml",
        "zocrates-trace-window",
        "chrome,centerscreen,resizable=yes,width=980,height=720",
        { steps, metadata: meta }
      );
      if (win) {
        win.updateTraceData?.({ steps, metadata: meta });
        win.focus?.();
        return;
      }
      throw new Error("Dialog window could not be opened");
    }
    throw new Error("openDialog is not available on parent window");
  } catch (err) {
    console.error("Zocrates: Failed to open execution trace window:", err);
    if (typeof addNotification === "function") {
      addNotification("Could not open execution trace window: " + (err?.message || err));
    }
  }
}

function appendIntermediateStepsBubbleDOM(steps = [], metadata = {}, isStreaming = false) {
  const bubbleDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  bubbleDiv.className = "intermediate-steps-bubble collapsed";

  const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerDiv.className = "intermediate-steps-header";

  const headerLeft = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerLeft.className = "intermediate-steps-header-left";

  const chevronSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  chevronSpan.className = "intermediate-steps-chevron";
  chevronSpan.textContent = "▶";

  const iconSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  iconSpan.className = isStreaming ? "intermediate-steps-icon intermediate-steps-spinner" : "intermediate-steps-icon";
  iconSpan.textContent = "⚙️";

  const labelSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  labelSpan.className = "intermediate-steps-label";

  function getStepLabel(count, toolCalls) {
    if (count === 0) return "Processing query...";
    if (toolCalls > 0) {
      return `Execution trace (${count} step${count === 1 ? '' : 's'}, ${toolCalls} tool call${toolCalls === 1 ? '' : 's'})`;
    }
    return `Execution trace (${count} step${count === 1 ? '' : 's'})`;
  }

  const initialToolCalls = (steps || []).filter(s => s.type === "tool_call").length;
  labelSpan.textContent = isStreaming
    ? "Searching library & synthesizing evidence..."
    : getStepLabel((steps || []).length, initialToolCalls);

  headerLeft.appendChild(chevronSpan);
  headerLeft.appendChild(iconSpan);
  headerLeft.appendChild(labelSpan);
  headerDiv.appendChild(headerLeft);

  const traceBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  traceBtn.className = isStreaming ? "intermediate-steps-trace-btn hidden" : "intermediate-steps-trace-btn";
  traceBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:12px;height:12px;fill:currentColor;flex-shrink:0;"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg> <span>View Trace</span>`;
  traceBtn.title = "Open Full Execution Trace";
  traceBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openTraceWindow(bubbleDiv._steps, bubbleDiv._metadata);
  });
  headerDiv.appendChild(traceBtn);

  bubbleDiv.appendChild(headerDiv);

  const bodyDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  bodyDiv.className = "intermediate-steps-body";
  bubbleDiv._isStreaming = isStreaming;
  bubbleDiv._bodyDiv = bodyDiv;

  function renderBodyContent(currentSteps, currentMeta) {
    bodyDiv.innerHTML = "";
    if (!currentSteps || currentSteps.length === 0) {
      const emptyEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      emptyEl.className = "intermediate-step-item";
      emptyEl.style.fontStyle = "italic";
      emptyEl.textContent = isStreaming ? "Executing steps in background..." : "No intermediate steps recorded.";
      bodyDiv.appendChild(emptyEl);
    } else {
      const displaySteps = currentSteps;
      displaySteps.forEach(s => {
        const itemEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        itemEl.className = "intermediate-step-item";

        const icon = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        icon.className = "intermediate-step-item-icon";
        let ic = "⚙️";
        let desc = "";

        if (s.type === "thought") {
          ic = "💭";
          const auth = s.author || "Agent";
          const snippet = (s.content || s.text || "").substring(0, 100);
          desc = `${auth}: ${snippet}...`;
        } else if (s.type === "tool_call") {
          ic = "🔧";
          const auth = s.author || "Search Agent";
          desc = `${auth}: Called ${s.name || 'tool'}`;
        } else if (s.type === "tool_return") {
          ic = "📥";
          desc = `Returned results for ${s.name || 'tool'}`;
        } else if (s.type === "agent_text") {
          ic = "💬";
          const auth = s.author || "Agent";
          desc = `${auth}: ${(s.text || "").substring(0, 100)}...`;
        }
        icon.textContent = ic;

        const descSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        descSpan.className = "intermediate-step-item-desc";
        descSpan.textContent = desc;

        itemEl.appendChild(icon);
        itemEl.appendChild(descSpan);
        bodyDiv.appendChild(itemEl);
      });
    }

    if (bubbleDiv._isStreaming) {
      bodyDiv.scrollTop = bodyDiv.scrollHeight;
      requestAnimationFrame(() => {
        bodyDiv.scrollTop = bodyDiv.scrollHeight;
      });
    }
  }

  renderBodyContent(steps, metadata);
  bubbleDiv.appendChild(bodyDiv);

  bubbleDiv._steps = steps || [];
  bubbleDiv._metadata = metadata || {};
  bubbleDiv._labelSpan = labelSpan;
  bubbleDiv._iconSpan = iconSpan;
  bubbleDiv._traceBtn = traceBtn;
  bubbleDiv._renderBody = renderBodyContent;

  headerDiv.addEventListener("click", () => {
    bubbleDiv.classList.toggle("collapsed");
    if (!bubbleDiv.classList.contains("collapsed")) {
      renderBodyContent(bubbleDiv._steps, bubbleDiv._metadata);
      if (bubbleDiv._isStreaming) {
        bodyDiv.scrollTop = bodyDiv.scrollHeight;
        requestAnimationFrame(() => {
          bodyDiv.scrollTop = bodyDiv.scrollHeight;
        });
        if (chatMessages) {
          chatMessages.scrollTop = chatMessages.scrollHeight;
        }
      }
    }
  });

  chatMessages.appendChild(bubbleDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return bubbleDiv;
}

function updateIntermediateStepsBubbleProgress(bubbleDiv, steps, statusText) {
  if (!bubbleDiv) return;
  bubbleDiv._steps = steps || [];
  if (bubbleDiv._labelSpan && statusText) {
    bubbleDiv._labelSpan.textContent = statusText;
  }
  // Only re-render the body if the user has manually expanded the bubble mid-stream
  if (!bubbleDiv.classList.contains("collapsed") && bubbleDiv._renderBody) {
    bubbleDiv._renderBody(bubbleDiv._steps, bubbleDiv._metadata);
  }
  if (chatMessages) {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }
}

function finalizeIntermediateStepsBubble(bubbleDiv, steps, metadata) {
  if (!bubbleDiv) return;
  bubbleDiv._isStreaming = false;
  bubbleDiv._steps = steps || [];
  bubbleDiv._metadata = metadata || {};

  if (!steps || steps.length === 0) {
    bubbleDiv.remove();
    return;
  }

  bubbleDiv.classList.add("collapsed");

  if (bubbleDiv._traceBtn) {
    bubbleDiv._traceBtn.classList.remove("hidden");
  }

  if (bubbleDiv._iconSpan) {
    bubbleDiv._iconSpan.className = "intermediate-steps-icon";
    bubbleDiv._iconSpan.textContent = "✓";
  }

  const toolCalls = steps.filter(s => s.type === "tool_call").length;
  if (bubbleDiv._labelSpan) {
    if (toolCalls > 0) {
      bubbleDiv._labelSpan.textContent = `Execution trace (${steps.length} steps, ${toolCalls} tool call${toolCalls === 1 ? '' : 's'})`;
    } else {
      bubbleDiv._labelSpan.textContent = `Execution trace (${steps.length} step${steps.length === 1 ? '' : 's'})`;
    }
  }

  if (bubbleDiv._renderBody) {
    bubbleDiv._renderBody(steps, metadata);
  }
}

function renderHitlCheckpointCard(container, hitlData, onResumeCallback) {
  if (!container) return null;

  // Remove existing HITL card if present in container
  const existing = container.querySelector(".hitl-checkpoint-card");
  if (existing) existing.remove();

  const cardDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  cardDiv.className = "hitl-checkpoint-card";

  const payload = (hitlData && hitlData.payload) || {};
  const extNum = payload.extension_number || 1;
  const maxExt = payload.max_extensions || 2;

  // Header
  const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerDiv.className = "hitl-card-header";

  const iconSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  iconSpan.className = "hitl-header-icon";
  iconSpan.textContent = "⚡";

  const titleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  titleSpan.className = "hitl-header-title";
  titleSpan.textContent = `Deep Research Checkpoint (Extension ${extNum} of ${maxExt})`;

  headerDiv.appendChild(iconSpan);
  headerDiv.appendChild(titleSpan);
  cardDiv.appendChild(headerDiv);

  // Trigger reason indicator (if present)
  if (payload.trigger_reason) {
    const triggerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    triggerDiv.className = "hitl-trigger-reason";

    const titleDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    titleDiv.className = "hitl-trigger-title";

    const details = payload.trigger_details || {};
    const calls = details.tool_calls_used != null ? details.tool_calls_used : payload.current_calls;
    const budget = details.tool_budget != null ? details.tool_budget : null;
    const words = details.words_used != null ? details.words_used : payload.current_words;
    const ceiling = details.word_ceiling != null ? details.word_ceiling : null;

    if (payload.trigger_reason === "agent_request") {
      titleDiv.textContent = "🤖 Requested by search agent";
      triggerDiv.appendChild(titleDiv);

      if (payload.agent_justification && String(payload.agent_justification).trim()) {
        const justDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        justDiv.className = "hitl-trigger-justification";
        const justText = String(payload.agent_justification).trim();
        justDiv.textContent = justText.length > 200 ? justText.slice(0, 200) + "..." : justText;
        triggerDiv.appendChild(justDiv);
      }
    } else if (payload.trigger_reason === "tool_budget_exhausted") {
      if (calls != null && budget != null) {
        titleDiv.textContent = `🛠 Retrieval tool budget exhausted (${calls}/${budget} calls used)`;
      } else {
        titleDiv.textContent = "🛠 Retrieval tool budget exhausted";
      }
      triggerDiv.appendChild(titleDiv);
    } else if (payload.trigger_reason === "word_ceiling_approached") {
      if (words != null && ceiling != null) {
        const wordsStr = (typeof words === "number" || !isNaN(Number(words))) ? Number(words).toLocaleString() : words;
        const ceilingStr = (typeof ceiling === "number" || !isNaN(Number(ceiling))) ? Number(ceiling).toLocaleString() : ceiling;
        titleDiv.textContent = `📄 Evidence volume approaching limit (~${wordsStr}/${ceilingStr} words)`;
      } else {
        titleDiv.textContent = "📄 Evidence volume approaching word limit";
      }
      triggerDiv.appendChild(titleDiv);
    } else {
      titleDiv.textContent = `⚡ Checkpoint triggered (${payload.trigger_reason})`;
      triggerDiv.appendChild(titleDiv);
    }

    cardDiv.appendChild(triggerDiv);
  }

  // Preliminary Evidence Summary (if available)
  if (payload.covered_summary && String(payload.covered_summary).trim()) {
    const summarySec = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    summarySec.className = "hitl-summary-section";

    const title = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    title.className = "hitl-section-title";
    title.textContent = "Preliminary Evidence Covered";

    const content = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    content.className = "hitl-summary-content";
    content.textContent = String(payload.covered_summary).trim();

    summarySec.appendChild(title);
    summarySec.appendChild(content);
    cardDiv.appendChild(summarySec);
  }

  // Unresolved Gaps (if available)
  if (Array.isArray(payload.missing_aspects) && payload.missing_aspects.length > 0) {
    const gapsSec = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    gapsSec.className = "hitl-gaps-section";

    const title = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    title.className = "hitl-section-title";
    title.textContent = "Unresolved Literature Gaps";

    const ul = document.createElementNS("http://www.w3.org/1999/xhtml", "ul");
    ul.className = "hitl-gaps-list";

    payload.missing_aspects.forEach(gap => {
      if (gap && String(gap).trim()) {
        const li = document.createElementNS("http://www.w3.org/1999/xhtml", "li");
        li.textContent = String(gap).trim();
        ul.appendChild(li);
      }
    });

    if (ul.childNodes.length > 0) {
      gapsSec.appendChild(title);
      gapsSec.appendChild(ul);
      cardDiv.appendChild(gapsSec);
    }
  }

  // Steering Guidance input
  const guidanceWrapper = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  guidanceWrapper.className = "hitl-guidance-wrapper";

  const guidanceInput = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
  guidanceInput.type = "text";
  guidanceInput.className = "hitl-guidance-input";
  guidanceInput.placeholder = "Optional steering guidance (e.g., Focus on Section 5 benchmarks)...";
  guidanceWrapper.appendChild(guidanceInput);
  cardDiv.appendChild(guidanceWrapper);

  // Actions Container & Buttons
  const actionsDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionsDiv.className = "hitl-actions-container";

  const grantBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  grantBtn.className = "hitl-btn hitl-btn-grant";
  grantBtn.innerHTML = "<span>➕ Grant Deep Research Extension</span>";

  const synthBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  synthBtn.className = "hitl-btn hitl-btn-synth";
  synthBtn.innerHTML = "<span>⚡ Synthesize Response with Existing Evidence</span>";

  actionsDiv.appendChild(grantBtn);
  actionsDiv.appendChild(synthBtn);
  cardDiv.appendChild(actionsDiv);

  grantBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    grantBtn.disabled = true;
    synthBtn.disabled = true;
    guidanceInput.disabled = true;
    grantBtn.innerHTML = "<span class='hitl-spinner'>⏳</span> <span>Extending Deep Research...</span>";
    const guidance = (guidanceInput.value || "").trim();
    if (onResumeCallback) {
      onResumeCallback({ action: "extend_budget", guidance: guidance || null });
    }
  });

  synthBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    grantBtn.disabled = true;
    synthBtn.disabled = true;
    guidanceInput.disabled = true;
    synthBtn.innerHTML = "<span class='hitl-spinner'>⏳</span> <span>Synthesizing Response...</span>";
    if (onResumeCallback) {
      onResumeCallback({ action: "synthesize" });
    }
  });

  container.appendChild(cardDiv);
  if (chatMessages) {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }
  return cardDiv;
}

/**
 * Detects whether a message contains citations or reference sections.
 */
function hasMessageCitations(text, metadata = null) {
  if (metadata) {
    if (Array.isArray(metadata.candidates) && metadata.candidates.length > 0) return false;
    if (metadata.author && typeof metadata.author === "string" && metadata.author.toLowerCase().includes("online")) return false;
    if (Array.isArray(metadata.citations) && metadata.citations.length > 0) return true;
    if (Array.isArray(metadata.references) && metadata.references.length > 0) return true;
  }
  if (!text || typeof text !== "string") return false;

  // Mask code blocks to avoid false positives like arr[1] or # Cited Sources in code
  const strippedCode = text.replace(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g, "");
  if (strippedCode.includes("# Cited Sources")) return true;

  const citationRegex = /\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】/;
  return citationRegex.test(strippedCode);
}

/**
 * Strips citation bracket tokens and sources sections to produce clean text for copying.
 */
function stripCitationsFromText(text) {
  if (!text || typeof text !== "string") return "";
  let processed = text;

  // 1. Mask code blocks first to protect array indexing, syntax brackets, and sample headers
  const codeBlocks = [];
  processed = processed.replace(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g, (match) => {
    const ph = `@@ZOCRATES_CODE_MASK_${codeBlocks.length}@@`;
    codeBlocks.push(match);
    return ph;
  });

  // 2. Remove sources section on masked text: # Cited Sources or <div class="zocrates-sources-section"> and everything after
  const sourcesMatch = processed.search(/(?:<div class="zocrates-sources-section"|\n{1,3}#{1,3}\s*Cited Sources)/i);
  if (sourcesMatch !== -1) {
    processed = processed.substring(0, sourcesMatch);
  }

  // 3. Remove citation tokens: [1], [1, 2], [1; 2], [Chunk 1], [E 1], 【1】
  const citationPattern = /[ \t]*(\[(?:E\s?|Chunk\s?)?\d{1,2}(?:\s*[,;]\s*\d{1,2})*\]|【\d{1,2}(?:\s*[,;]\s*\d{1,2})*】)/g;
  processed = processed.replace(citationPattern, "");

  // Clean trailing spaces before punctuation resulting from removed citation tokens
  processed = processed.replace(/[ \t]+([.,;:!?])/g, "$1");

  // 4. Restore code blocks
  for (let i = 0; i < codeBlocks.length; i++) {
    processed = processed.replace(`@@ZOCRATES_CODE_MASK_${i}@@`, codeBlocks[i]);
  }

  return processed.trim();
}

/**
 * Builds clean Markdown for references (Cited Sources & considered-sources drawer) without HTML tags.
 */
function buildReferencesMarkdown(references) {
  if (!references || !Array.isArray(references) || references.length === 0) return "";
  const tier1 = references.filter(r => r.tier === 1 || r.tier === "1" || !r.tier);
  const tier2 = references.filter(r => r.tier === 2 || r.tier === "2");
  let md = "\n\n# Cited Sources\n";
  tier1.forEach(r => {
    const auth = r.authors ? String(r.authors).trim() : "";
    const yr = r.year ? String(r.year).trim() : "";
    let prefix = "";
    if (auth && yr) prefix = `**${auth} (${yr})**. `;
    else if (auth) prefix = `**${auth}**. `;
    else if (yr) prefix = `**(${yr})**. `;
    const title = r.title ? `*${r.title}*.` : "*Untitled Document*.";
    const pub = r.publication ? ` ${r.publication}.` : "";
    md += `- ${prefix}${title}${pub}\n`;
  });

  if (tier2.length > 0) {
    const plural = tier2.length === 1 ? "Source" : "Sources";
    md += `\n**${tier2.length} Uncited ${plural} Considered by the Agent**\n`;
    tier2.forEach(r => {
      const auth = r.authors ? String(r.authors).trim() : "";
      const yr = r.year ? String(r.year).trim() : "";
      let prefix = "";
      if (auth && yr) prefix = `**${auth} (${yr})**. `;
      else if (auth) prefix = `**${auth}**. `;
      else if (yr) prefix = `**(${yr})**. `;
      const title = r.title ? `*${r.title}*.` : "*Untitled Document*.";
      const pub = r.publication ? ` ${r.publication}.` : "";
      md += `- ${prefix}${title}${pub}\n`;
    });
  }
  return md;
}

/**
 * Returns formatted Markdown content for clipboard copying or saving as note.
 * - When citations are hidden: returns clean prose without bracketed citations or sources.
 * - When citations are visible: returns prose with author-year citations (e.g. (Watson & Crick, 1953))
 *   and the full references section in clean Markdown.
 */
function getMessageTextForCopy(msgDiv, wrapperDiv) {
  const currentText = (msgDiv && msgDiv._rawText !== undefined) ? msgDiv._rawText : (msgDiv ? (msgDiv.innerText || msgDiv.textContent) : "");
  const isCitationsHidden = wrapperDiv && wrapperDiv.classList.contains("citations-hidden");

  if (isCitationsHidden) {
    return stripCitationsFromText(currentText);
  }

  const metadata = (msgDiv && msgDiv._metadata) || (wrapperDiv && wrapperDiv._metadata) || null;
  const citations = (metadata && Array.isArray(metadata.citations)) ? metadata.citations : ((msgDiv && msgDiv._citations) || null);
  const references = (metadata && Array.isArray(metadata.references)) ? metadata.references : null;

  let processed = currentText;

  // 1. Separate sources section if already present in text
  let sourcesPart = "";
  const sourcesMatch = processed.search(/(?:<div class="zocrates-sources-section"|\n{1,3}#{1,3}\s*Cited Sources)/i);
  if (sourcesMatch !== -1) {
    sourcesPart = processed.substring(sourcesMatch);
    processed = processed.substring(0, sourcesMatch);
  }

  // 2. Format in-text citations as Author-Year (e.g. (Watson & Crick, 1953))
  if (citations && citations.length > 0) {
    const codeBlocks = [];
    processed = processed.replace(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g, (match) => {
      const ph = `@@ZOCRATES_CODE_MASK_${codeBlocks.length}@@`;
      codeBlocks.push(match);
      return ph;
    });

    const citationPattern = /\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】/g;
    processed = processed.replace(citationPattern, (match, p1, p2) => {
      const raw = p1 || p2 || "";
      const parts = raw.split(/[,;]/).map(s => s.trim()).filter(Boolean);
      const labels = [];
      for (const part of parts) {
        const idx = parseInt(part, 10);
        if (isNaN(idx)) continue;
        const c = citations.find(item => item.index === idx);
        if (c) {
          const auth = c.authors ? c.authors.trim() : "";
          const yr = c.year ? String(c.year).trim() : "";
          if (auth && yr) {
            labels.push(`${auth}, ${yr}`);
          } else if (auth) {
            labels.push(auth);
          } else if (yr) {
            labels.push(yr);
          } else if (c.title) {
            labels.push(c.title);
          } else {
            labels.push(`[${idx}]`);
          }
        }
      }
      if (labels.length === 0) return "";
      return `(${labels.join("; ")})`;
    });

    for (let i = 0; i < codeBlocks.length; i++) {
      processed = processed.replace(`@@ZOCRATES_CODE_MASK_${i}@@`, codeBlocks[i]);
    }
  }

  // 3. Append clean references section
  if (references && references.length > 0) {
    processed = processed.trim() + buildReferencesMarkdown(references);
  } else if (sourcesPart) {
    let cleanSources = sourcesPart
      .replace(/<div class="zocrates-sources-section">/gi, "")
      .replace(/<\/div>/gi, "")
      .replace(/<span class="zotero-source-link"[^>]*>([\s\S]*?)<\/span>/gi, "$1")
      .replace(/<span[^>]*>/gi, "")
      .replace(/<\/span>/gi, "")
      .replace(/<details class="considered-sources">[\s\S]*?<summary>(.*?)<\/summary>/gi, "\n**$1**\n")
      .replace(/<\/?(?:details|summary|ul)>/gi, "")
      .replace(/<li>([\s\S]*?)<\/li>/gi, "- $1")
      .trim();
    processed = processed.trim() + "\n\n" + cleanSources;
  }

  return processed.trim();
}

/**
 * Creates the toggle button for hiding/showing citations on an assistant message.
 */
function createCitationToggleBtn(wrapperDiv) {
  const toggleBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  toggleBtn.className = "citation-toggle-btn";
  toggleBtn.setAttribute("type", "button");

  const isCurrentlyHidden = wrapperDiv.classList.contains("citations-hidden");
  toggleBtn.setAttribute("title", isCurrentlyHidden ? "Show citations" : "Hide citations");
  if (isCurrentlyHidden) {
    toggleBtn.classList.add("muted");
  }

  toggleBtn.innerHTML = `
    <svg class="citation-toggle-icon icon-visible ${isCurrentlyHidden ? 'hidden' : ''}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="currentColor" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg>
    <svg class="citation-toggle-icon icon-hidden ${isCurrentlyHidden ? '' : 'hidden'}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="currentColor" d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.44-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/></svg>
    <span class="citation-toggle-label">${isCurrentlyHidden ? "Citations: Hidden" : "Citations: Visible"}</span>
  `;

  toggleBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    closeEvidencePopover();
    const isHidden = wrapperDiv.classList.toggle("citations-hidden");
    const iconVis = toggleBtn.querySelector(".icon-visible");
    const iconHid = toggleBtn.querySelector(".icon-hidden");
    const label = toggleBtn.querySelector(".citation-toggle-label");
    if (isHidden) {
      toggleBtn.classList.add("muted");
      toggleBtn.setAttribute("title", "Show citations");
      if (label) label.textContent = "Citations: Hidden";
      if (iconVis) iconVis.classList.add("hidden");
      if (iconHid) iconHid.classList.remove("hidden");
    } else {
      toggleBtn.classList.remove("muted");
      toggleBtn.setAttribute("title", "Hide citations");
      if (label) label.textContent = "Citations: Visible";
      if (iconVis) iconVis.classList.remove("hidden");
      if (iconHid) iconHid.classList.add("hidden");
    }
  });

  return toggleBtn;
}

/**
 * Ensures the citation toggle button exists on an assistant message wrapper.
 */
function ensureCitationToggleBtn(wrapperDiv) {
  if (!wrapperDiv) return;
  const bubble = wrapperDiv.querySelector(".message-bubble");
  const rawText = bubble ? (bubble._rawText !== undefined ? bubble._rawText : (bubble.textContent || "")) : "";
  const metadata = (bubble && bubble._metadata) || wrapperDiv._metadata || null;
  if (!hasMessageCitations(rawText, metadata)) {
    const existingToggle = wrapperDiv.querySelector(".citation-toggle-btn");
    if (existingToggle) existingToggle.remove();
    return;
  }
  let leftContainer = wrapperDiv.querySelector(".message-actions-left");
  if (!leftContainer) {
    const actionsDiv = wrapperDiv.querySelector(".message-actions");
    if (!actionsDiv) return;
    leftContainer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    leftContainer.className = "message-actions-left";
    actionsDiv.insertBefore(leftContainer, actionsDiv.firstChild);
  }
  if (leftContainer.querySelector(".citation-toggle-btn")) return;
  const toggleBtn = createCitationToggleBtn(wrapperDiv);
  const existingClaimsBtn = leftContainer.querySelector(".claims-inspector-btn");
  if (existingClaimsBtn) {
    leftContainer.insertBefore(toggleBtn, existingClaimsBtn);
  } else {
    leftContainer.appendChild(toggleBtn);
  }
}

/**
 * Resolves an item selection in the Zotero library tree.
 */
function selectItemInLibrary(itemKey) {
  return selectItemInZotero(itemKey);
}

/**
 * Builds normalized data payload for the dedicated Claim Verification Inspector window.
 * Reconciles verification assessments, summaries, and citations metadata.
 */
function buildClaimsInspectorData(meta) {
  if (!meta) {
    return {
      assessments: [],
      summary: { batch_size: 0, kept: 0, demoted: 0, fail_open: 0, mean_support: null, std_support: null, threshold: 0.60, latency_ms: null },
      model: "decision-model",
      provider: "openrouter",
      threshold: 0.60,
      latency_ms: null,
      theme: (typeof document !== "undefined" && document.body?.getAttribute?.("data-theme")) || "slate",
      sessionId: (typeof currentSessionId !== "undefined" ? currentSessionId : null) || (typeof resolveActiveSessionId === "function" ? resolveActiveSessionId() : null),
      invocationId: null
    };
  }

  const ver = meta.citation_verification || meta.citation_verification_telemetry || null;
  const citations = Array.isArray(meta.citations) ? meta.citations : [];
  let rows = [];

  if (ver && Array.isArray(ver.assessments) && ver.assessments.length > 0) {
    // Clone assessments to prevent mutating caller metadata
    rows = ver.assessments.map(a => Object.assign({}, a));

    // Cross-reference with citations to backfill any missing item_key, attachment_key, or citation metadata
    if (citations.length > 0) {
      rows.forEach(a => {
        const c = citations.find(item =>
          (item.index !== undefined && item.index === a.chunk_index) ||
          (item.chunk_index !== undefined && item.chunk_index === a.chunk_index)
        );
        if (c) {
          if (!a.item_key && c.item_key) a.item_key = String(c.item_key);
          if (!a.attachment_key && c.attachment_key) a.attachment_key = c.attachment_key;
          if (!a.title && c.title) a.title = c.title;
          if ((!a.authors || a.authors.length === 0) && c.authors) a.authors = c.authors;
          if (!a.year && c.year) a.year = c.year;
          if (!a.section && c.section) a.section = c.section;
          if (!a.source_text && (c.quote || c.text)) a.source_text = c.quote || c.text;
        }
      });
    }

    // Cross-reference with references to backfill any demoted chunks not present in citations
    const references = Array.isArray(meta.references) ? meta.references : [];
    if (references.length > 0) {
      rows.forEach(a => {
        if (!a.item_key || !a.title) {
          const r = references.find(ref =>
            (a.item_key && String(ref.item_key || ref.key) === String(a.item_key)) ||
            (a.title && ref.title && ref.title.toLowerCase() === a.title.toLowerCase())
          );
          if (r) {
            if (!a.item_key && (r.item_key || r.key)) a.item_key = String(r.item_key || r.key);
            if (!a.attachment_key && r.attachment_key) a.attachment_key = r.attachment_key;
            if (!a.title && r.title) a.title = r.title;
            if ((!a.authors || a.authors.length === 0) && r.authors) a.authors = r.authors;
            if (!a.year && r.year) a.year = r.year;
          }
        }
      });
    }

    // Fallback for single-source turns or active context items: reconcile any assessments with missing title/authors
    const knownTitles = Array.from(new Set(rows.map(r => r.title).filter(Boolean)));
    const knownAuthors = rows.find(r => Array.isArray(r.authors) && r.authors.length > 0)?.authors || null;
    const knownYear = rows.find(r => r.year)?.year || null;
    const knownKey = rows.find(r => r.item_key)?.item_key || null;
    const activeItem = (meta.active_context_items && meta.active_context_items[0]) || null;

    const fallbackTitle = knownTitles.length === 1 ? knownTitles[0] : (citations[0]?.title || references[0]?.title || activeItem?.title || "");
    const fallbackAuthors = knownAuthors || citations[0]?.authors || references[0]?.authors || activeItem?.creators || activeItem?.authors || [];
    const fallbackYear = knownYear || citations[0]?.year || references[0]?.year || activeItem?.year || "";
    const fallbackKey = knownKey || citations[0]?.item_key || citations[0]?.key || references[0]?.item_key || references[0]?.key || activeItem?.item_key || activeItem?.key || "";

    rows.forEach(a => {
      if (!a.title && fallbackTitle) a.title = fallbackTitle;
      if ((!a.authors || a.authors.length === 0) && fallbackAuthors && fallbackAuthors.length > 0) a.authors = fallbackAuthors;
      if (!a.year && fallbackYear) a.year = fallbackYear;
      if (!a.item_key && fallbackKey) a.item_key = String(fallbackKey);
    });
  } else if (citations.length > 0) {
    // Construct rows from citation occurrences when raw assessments array is absent
    citations.forEach(c => {
      const cIdx = c.index !== undefined ? c.index : (c.chunk_index !== undefined ? c.chunk_index : 1);
      const occurrences = (Array.isArray(c.occurrences) && c.occurrences.length > 0)
        ? c.occurrences
        : (c.support_probability !== undefined
            ? [{ occurrence: 1, support_probability: c.support_probability, survived: c.support_probability >= (c.threshold !== undefined ? c.threshold : 0.60) }]
            : []);

      if (occurrences.length > 0) {
        occurrences.forEach(occ => {
          const prob = occ.support_probability !== undefined ? occ.support_probability : (c.support_probability !== undefined ? c.support_probability : null);
          const thresh = occ.threshold !== undefined ? occ.threshold : (c.threshold !== undefined ? c.threshold : 0.60);
          let verdict = occ.verdict;
          if (!verdict) {
            if (prob === null || prob === undefined) verdict = "FAIL_OPEN";
            else if (occ.survived === true || prob >= thresh) verdict = "KEPT";
            else verdict = "DEMOTED";
          }
          rows.push({
            chunk_index: cIdx,
            occurrence: occ.occurrence || 1,
            claim: occ.claim || c.claim || "Citation claim",
            source_text: occ.source_text || c.quote || c.text || "",
            title: c.title || "",
            authors: c.authors || [],
            year: c.year || "",
            section: c.section || "",
            item_key: c.item_key ? String(c.item_key) : "",
            attachment_key: c.attachment_key || null,
            support_probability: prob,
            threshold: thresh,
            verdict: verdict,
            latency_ms: occ.latency_ms || null,
            sentence_attribution: occ.sentence_attribution || c.sentence_attribution || null
          });
        });
      }
    });
  }

  // Determine or compute summary stats
  let summary = (ver && ver.summary) ? Object.assign({}, ver.summary) : null;
  const threshold = (ver && ver.threshold !== undefined)
    ? ver.threshold
    : (summary?.threshold !== undefined ? summary.threshold : (rows[0]?.threshold ?? 0.60));

  if (!summary || summary.batch_size === undefined) {
    const kept = rows.filter(r => r.verdict === "KEPT").length;
    const demoted = rows.filter(r => r.verdict === "DEMOTED").length;
    const failOpen = rows.filter(r => r.verdict === "FAIL_OPEN" || r.support_probability === null || r.support_probability === undefined).length;
    const validProbs = rows.map(r => r.support_probability).filter(p => typeof p === "number");
    const mean = validProbs.length > 0 ? Number((validProbs.reduce((a, b) => a + b, 0) / validProbs.length).toFixed(3)) : null;
    let std = null;
    if (validProbs.length >= 2) {
      const vMean = validProbs.reduce((a, b) => a + b, 0) / validProbs.length;
      std = Number(Math.sqrt(validProbs.reduce((acc, p) => acc + Math.pow(p - vMean, 2), 0) / (validProbs.length - 1)).toFixed(3));
    } else if (validProbs.length === 1) {
      std = 0.0;
    }

    summary = {
      batch_size: rows.length,
      kept,
      demoted,
      fail_open: failOpen,
      mean_support: mean,
      std_support: std,
      threshold,
      latency_ms: ver?.latency_ms ?? null
    };
  }

  return {
    assessments: rows,
    summary,
    model: ver?.model || meta.model || "decision-model",
    provider: ver?.provider || "openrouter",
    threshold,
    latency_ms: (ver && ver.latency_ms !== undefined) ? ver.latency_ms : (summary?.latency_ms ?? null),
    theme: meta.theme || (typeof document !== "undefined" && document.body?.getAttribute?.("data-theme")) || "slate",
    sessionId: meta.sessionId || meta.session_id || (typeof currentSessionId !== "undefined" ? currentSessionId : null) || (typeof resolveActiveSessionId === "function" ? resolveActiveSessionId() : null),
    invocationId: meta.invocation_id || meta.invocationId || (meta.citation_verification && meta.citation_verification.invocation_id) || null
  };
}

/**
 * Opens the standalone Claim Verification Inspector dialog window.
 */
function openClaimsInspectorWindow(meta = {}) {
  const payload = buildClaimsInspectorData(meta);
  payload.theme = (typeof document !== "undefined" && document.body?.getAttribute?.("data-theme")) || payload.theme || "slate";
  payload._sidebarWindow = window;

  if (typeof Zotero !== "undefined" && Zotero.Zocrates) {
    Zotero.Zocrates._sidebarWindow = window;
  }

  try {
    if (window.top) window.top._zocratesCurrentClaimsData = payload;
    window._zocratesCurrentClaimsData = payload;
    const Zotero = window.Zotero || window.top?.Zotero;
    if (Zotero?.Zocrates) Zotero.Zocrates._currentClaimsData = payload;
  } catch (e) {}

  try {
    const mainWin = window.top || window.parent || window;
    if (mainWin && typeof mainWin.openDialog === "function") {
      const win = mainWin.openDialog(
        "chrome://zocrates/content/claims.html",
        "zocrates-claims-window",
        "chrome,centerscreen,resizable=yes,width=1180,height=760",
        payload
      );
      if (win) {
        win.updateClaimsData?.(payload);
        win.focus?.();
        return;
      }
      throw new Error("Dialog window could not be opened");
    }
    throw new Error("openDialog is not available on parent window");
  } catch (err) {
    console.error("Zocrates: Failed to open claim verification inspector window:", err);
    if (typeof addNotification === "function") {
      addNotification("Could not open claim verification inspector: " + (err?.message || err));
    }
  }
}

/**
 * Creates the Claims Inspector action button for an assistant message.
 */
function createClaimsInspectorBtn(wrapperDiv) {
  const bubble = wrapperDiv ? wrapperDiv.querySelector(".message-bubble") : null;
  const meta = (bubble && bubble._metadata) || (wrapperDiv && wrapperDiv._metadata) || (wrapperDiv?.querySelector(".info-btn")?._metadata) || {};
  const data = buildClaimsInspectorData(meta);
  const summary = data.summary || {};
  const total = summary.batch_size !== undefined ? summary.batch_size : data.assessments.length;
  const verified = summary.kept !== undefined ? summary.kept : data.assessments.filter(a => a.verdict === "KEPT").length;
  const demoted = summary.demoted !== undefined ? summary.demoted : data.assessments.filter(a => a.verdict === "DEMOTED").length;

  const btn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  btn.className = "claims-inspector-btn";
  btn.setAttribute("type", "button");
  btn.textContent = "🛡️ Claim Verification Inspector";
  btn.setAttribute(
    "title",
    total > 0
      ? `Inspect claim verification table (${verified} verified, ${demoted} rejected of ${total} total)`
      : "Inspect claim verification table"
  );

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const currentMeta = (bubble && bubble._metadata) || (wrapperDiv && wrapperDiv._metadata) || (wrapperDiv?.querySelector(".info-btn")?._metadata) || meta;
    openClaimsInspectorWindow(currentMeta);
  });

  return btn;
}

/**
 * Ensures the Claims Inspector action button exists in .message-actions-left next to .citation-toggle-btn.
 */
function ensureClaimsInspectorBtn(wrapperDiv) {
  if (!wrapperDiv) return;
  const bubble = wrapperDiv.querySelector(".message-bubble");
  const meta = (bubble && bubble._metadata) || wrapperDiv._metadata || (wrapperDiv.querySelector(".info-btn")?._metadata) || null;

  const data = buildClaimsInspectorData(meta);
  if (!data || !data.assessments || data.assessments.length === 0) {
    const existing = wrapperDiv.querySelector(".claims-inspector-btn");
    if (existing) existing.remove();
    return;
  }

  let leftContainer = wrapperDiv.querySelector(".message-actions-left");
  if (!leftContainer) {
    const actionsDiv = wrapperDiv.querySelector(".message-actions");
    if (!actionsDiv) return;
    leftContainer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    leftContainer.className = "message-actions-left";
    actionsDiv.insertBefore(leftContainer, actionsDiv.firstChild);
  }

  const summary = data.summary || {};
  const total = summary.batch_size !== undefined ? summary.batch_size : data.assessments.length;
  const verified = summary.kept !== undefined ? summary.kept : data.assessments.filter(a => a.verdict === "KEPT").length;
  const demoted = summary.demoted !== undefined ? summary.demoted : data.assessments.filter(a => a.verdict === "DEMOTED").length;
  const btnLabel = "🛡️ Claim Verification Inspector";
  const btnTitle = total > 0
    ? `Inspect claim verification table (${verified} verified, ${demoted} rejected of ${total} total)`
    : "Inspect claim verification table";

  const existingBtn = leftContainer.querySelector(".claims-inspector-btn");
  if (existingBtn) {
    existingBtn.textContent = btnLabel;
    existingBtn.setAttribute("title", btnTitle);
    return;
  }

  const btn = createClaimsInspectorBtn(wrapperDiv);
  const citationToggle = leftContainer.querySelector(".citation-toggle-btn");
  if (citationToggle && citationToggle.nextSibling) {
    leftContainer.insertBefore(btn, citationToggle.nextSibling);
  } else {
    leftContainer.appendChild(btn);
  }
}

// ---------------------------------------------------------------------------
// Phase 5: Candidate Literature Discovery & Batch Import UI
// ---------------------------------------------------------------------------
const selectedCandidateKeys = new Set();
const importedCandidateKeys = new Set();

function getScholarImport() {
  if (typeof ScholarImport !== "undefined" && ScholarImport) return ScholarImport;
  const zot = resolveZotero();
  if (zot?.Zocrates?.ScholarImport) return zot.Zocrates.ScholarImport;
  if (typeof window !== "undefined") {
    if (window.ScholarImport) return window.ScholarImport;
    if (window.parent?.Zotero?.Zocrates?.ScholarImport) return window.parent.Zotero.Zocrates.ScholarImport;
    if (window.top?.Zotero?.Zocrates?.ScholarImport) return window.top.Zotero.Zocrates.ScholarImport;
  }
  if (typeof globalThis !== "undefined" && globalThis.ScholarImport) return globalThis.ScholarImport;
  return null;
}

function getCandidateKey(candidate, fallbackIndex = 0) {
  if (!candidate) return "cand_" + fallbackIndex;
  if (candidate.candidate_id) return String(candidate.candidate_id);
  if (candidate.doi) return "doi:" + String(candidate.doi).trim().toLowerCase();
  if (candidate.arxiv_id) return "arxiv:" + String(candidate.arxiv_id).trim().toLowerCase();
  if (candidate.openalex_id) return String(candidate.openalex_id).trim();
  if (candidate.title) return "title:" + String(candidate.title).trim().toLowerCase();
  return "cand_" + fallbackIndex;
}

function formatCandidateAuthors(authors) {
  if (!authors || !Array.isArray(authors) || authors.length === 0) return "Unknown authors";
  const first = authors[0];
  let firstLastName = "";
  if (typeof first === "object" && first !== null) {
    firstLastName = first.lastName || first.name || String(first);
  } else if (typeof first === "string") {
    const trimmed = first.trim();
    if (trimmed.includes(",")) {
      firstLastName = trimmed.split(",")[0].trim();
    } else {
      const parts = trimmed.split(/\s+/);
      firstLastName = parts[parts.length - 1];
    }
  }
  if (!firstLastName) firstLastName = "Unknown";
  if (authors.length === 1) return firstLastName;
  if (authors.length === 2) {
    const second = authors[1];
    let secondLastName = "";
    if (typeof second === "object" && second !== null) {
      secondLastName = second.lastName || second.name || String(second);
    } else if (typeof second === "string") {
      const trimmed = second.trim();
      if (trimmed.includes(",")) {
        secondLastName = trimmed.split(",")[0].trim();
      } else {
        const parts = trimmed.split(/\s+/);
        secondLastName = parts[parts.length - 1];
      }
    }
    return secondLastName ? `${firstLastName} & ${secondLastName}` : `${firstLastName} et al.`;
  }
  return `${firstLastName} et al.`;
}

function formatCandidateMetaLine(c) {
  if (!c) return "";
  const parts = [];
  const authorsStr = formatCandidateAuthors(c.authors);
  if (authorsStr) parts.push(authorsStr);

  if (c.arxiv_id) {
    parts.push(`arXiv:${c.arxiv_id}`);
  } else if (c.venue || c.publication) {
    parts.push(c.venue || c.publication);
  } else if (c.doi) {
    parts.push(`DOI: ${c.doi}`);
  }

  if (c.year !== undefined && c.year !== null && String(c.year).trim() !== "") {
    parts.push(String(c.year).trim());
  }

  const citations = typeof c.citation_count === "number"
    ? c.citation_count
    : (typeof c.citations === "number"
        ? c.citations
        : (c.citation_count !== undefined && c.citation_count !== null
            ? Number(c.citation_count)
            : (c.citations !== undefined && c.citations !== null ? Number(c.citations) : null)));
  if (citations !== null && !isNaN(citations)) {
    parts.push(`(${citations} citation${citations === 1 ? "" : "s"})`);
  }

  return parts.join(" · ");
}

function populateCandidateCollectionSelect(selectEl, libraryID) {
  if (!selectEl) return;
  selectEl.innerHTML = "";
  if (selectEl.options) selectEl.options.length = 0;

  const rootOption = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
  rootOption.value = "";
  rootOption.textContent = "📁 My Library (Root)";
  selectEl.appendChild(rootOption);

  const newOption = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
  newOption.value = "__new__";
  newOption.textContent = "➕ New Collection...";
  selectEl.appendChild(newOption);

  let libID = libraryID;
  if (libID === undefined || libID === null) {
    try {
      const pane = resolveZoteroPane();
      libID = resolveActiveLibraryID(pane);
    } catch (e) {}
  }
  if (libID === undefined || libID === null) {
    libID = Zotero?.Libraries?.userLibraryID || 1;
  }

  let rawCollections = [];
  try {
    rawCollections = fetchAllCollectionsInLibrary(libID);
  } catch (e) {
    console.warn("Zocrates: Error fetching collections for candidate dropdown:", e);
  }

  // Filter strictly to collections belonging to libID (prevents group library collection leakage)
  if (rawCollections && rawCollections.length > 0) {
    rawCollections = rawCollections.filter(col => {
      if (col && col.libraryID !== undefined && col.libraryID !== null) {
        return col.libraryID === libID;
      }
      return true;
    });
  }

  if (rawCollections && rawCollections.length > 0) {
    const colMap = new Map();
    rawCollections.forEach(col => {
      if (col && col.id) colMap.set(col.id, col);
    });

    const childrenMap = new Map();
    const rootCols = [];

    rawCollections.forEach(col => {
      const pId = getParentId(col, colMap);
      if (pId && colMap.has(pId)) {
        if (!childrenMap.has(pId)) childrenMap.set(pId, []);
        childrenMap.get(pId).push(col);
      } else {
        rootCols.push(col);
      }
    });

    rootCols.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    for (const kids of childrenMap.values()) {
      kids.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    }

    function appendBranch(cols, prefix) {
      for (let i = 0; i < cols.length; i++) {
        const col = cols[i];
        const isLast = (i === cols.length - 1);
        const branch = prefix ? (isLast ? "└─ " : "├─ ") : "";
        const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        opt.value = String(col.id);
        opt.textContent = `${prefix}${branch}${col.name}`;
        selectEl.appendChild(opt);

        const children = childrenMap.get(col.id) || [];
        if (children.length > 0) {
          const nextPrefix = prefix + (isLast ? "   " : "│  ");
          appendBranch(children, nextPrefix);
        }
      }
    }

    appendBranch(rootCols, "");
  }

  // Pre-select active collection if available
  try {
    const pane = resolveZoteroPane();
    const selectedCols = resolveSelectedCollections(pane);
    if (selectedCols && selectedCols.length > 0 && selectedCols[0]?.id) {
      const activeColId = String(selectedCols[0].id);
      if (selectEl.querySelector(`option[value="${activeColId}"]`)) {
        selectEl.value = activeColId;
      }
    }
  } catch (e) {
    console.warn("Zocrates: Error pre-selecting active collection:", e);
  }
}

function renderCandidatePapersGroup(wrapperDiv, candidates, invocationId) {
  if (!wrapperDiv || !Array.isArray(candidates) || candidates.length === 0) return null;

  // Guard against duplicate rendering in the same wrapper
  const existing = wrapperDiv.querySelector(".candidate-papers-group");
  if (existing) return existing;

  const groupDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  groupDiv.className = "candidate-papers-group";
  if (invocationId) {
    groupDiv.dataset.invocationId = invocationId;
  }

  // Header
  const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerDiv.className = "candidate-header";

  const titleEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  titleEl.className = "candidate-header-title";
  titleEl.textContent = `Candidate Papers for Import (${candidates.length} novel paper${candidates.length === 1 ? "" : "s"} found)`;
  headerDiv.appendChild(titleEl);

  // Collection bar
  const collectionBar = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  collectionBar.className = "candidate-collection-bar";

  const collectionLabel = document.createElementNS("http://www.w3.org/1999/xhtml", "label");
  collectionLabel.className = "candidate-collection-label";
  collectionLabel.textContent = "📁 Target Collection:";

  const collectionSelect = document.createElementNS("http://www.w3.org/1999/xhtml", "select");
  collectionSelect.className = "candidate-collection-select";
  populateCandidateCollectionSelect(collectionSelect);

  collectionBar.appendChild(collectionLabel);
  collectionBar.appendChild(collectionSelect);
  headerDiv.appendChild(collectionBar);

  // New collection input row
  const newCollectionRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  newCollectionRow.className = "candidate-new-collection-row hidden";

  const newCollectionInput = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
  newCollectionInput.type = "text";
  newCollectionInput.className = "candidate-new-collection-input";
  newCollectionInput.setAttribute("placeholder", "Enter new collection name...");
  newCollectionRow.appendChild(newCollectionInput);
  headerDiv.appendChild(newCollectionRow);

  collectionSelect.addEventListener("change", () => {
    if (collectionSelect.value === "__new__") {
      newCollectionRow.classList.remove("hidden");
      if (typeof newCollectionInput.focus === "function") {
        newCollectionInput.focus();
      }
    } else {
      newCollectionRow.classList.add("hidden");
      newCollectionInput.value = "";
    }
  });

  // Action bar
  const actionBar = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionBar.className = "candidate-action-bar";

  const selectAllLabel = document.createElementNS("http://www.w3.org/1999/xhtml", "label");
  selectAllLabel.className = "candidate-select-all-label";

  const selectAllCheckbox = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
  selectAllCheckbox.type = "checkbox";
  selectAllCheckbox.className = "candidate-select-all-checkbox";

  const selectAllText = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  selectAllText.textContent = "Select All";

  selectAllLabel.appendChild(selectAllCheckbox);
  selectAllLabel.appendChild(selectAllText);

  const importBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  importBtn.className = "candidate-import-btn";
  importBtn.disabled = true;
  importBtn.textContent = "⬇ Import Selected (0)";

  actionBar.appendChild(selectAllLabel);
  actionBar.appendChild(importBtn);
  headerDiv.appendChild(actionBar);
  groupDiv.appendChild(headerDiv);

  // Candidate cards list
  const cardsList = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  cardsList.className = "candidate-cards-list";

  candidates.forEach((c, idx) => {
    const candIdx = idx + 1;
    const candKey = getCandidateKey(c, candIdx);
    const isImported = importedCandidateKeys.has(candKey) || Boolean(c.in_library);

    const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    card.className = "candidate-paper-card" + (isImported ? " imported" : "");
    card.dataset.candidateKey = candKey;

    // Top row: checkbox + title
    const topRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    topRow.className = "candidate-card-top";

    const checkbox = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
    checkbox.type = "checkbox";
    checkbox.className = "candidate-select-checkbox";
    checkbox.dataset.candidateKey = candKey;

    if (isImported) {
      checkbox.disabled = true;
      checkbox.checked = false;
    } else {
      checkbox.checked = selectedCandidateKeys.has(candKey);
    }

    const titleRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    titleRow.className = "candidate-card-title-row";

    const indexSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
    indexSpan.className = "candidate-card-index";
    indexSpan.textContent = `${candIdx}.`;

    const titleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
    titleSpan.className = "candidate-card-title";
    titleSpan.textContent = c.title || "Untitled";

    titleRow.appendChild(indexSpan);
    titleRow.appendChild(titleSpan);
    topRow.appendChild(checkbox);
    topRow.appendChild(titleRow);
    card.appendChild(topRow);

    // Metadata line
    const metaLine = formatCandidateMetaLine(c);
    if (metaLine) {
      const metaEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      metaEl.className = "candidate-card-meta";
      metaEl.textContent = metaLine;
      card.appendChild(metaEl);
    }

    // Badges container
    const badgesDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    badgesDiv.className = "candidate-card-badges";

    const isOa = Boolean(c.is_oa || c.oa_pdf_url);
    const oaBadge = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
    oaBadge.className = "candidate-badge " + (isOa ? "oa-available" : "oa-none");
    oaBadge.textContent = isOa ? "PDF Available" : "Metadata Only";
    badgesDiv.appendChild(oaBadge);

    if (isImported) {
      const impBadge = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      impBadge.className = "candidate-badge imported";
      impBadge.textContent = "✓ Imported";
      badgesDiv.appendChild(impBadge);
    }
    card.appendChild(badgesDiv);

    // Rationale
    const rationaleText = c.tldr || c.abstract;
    if (rationaleText && String(rationaleText).trim()) {
      const ratEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      ratEl.className = "candidate-card-rationale";

      const strongEl = document.createElementNS("http://www.w3.org/1999/xhtml", "strong");
      strongEl.textContent = "Recommendation rationale: ";

      const textNode = document.createTextNode(String(rationaleText).trim());
      ratEl.appendChild(strongEl);
      ratEl.appendChild(textNode);
      card.appendChild(ratEl);
    }

    // Expandable Abstract Drawer
    const abstractDrawer = document.createElementNS("http://www.w3.org/1999/xhtml", "details");
    abstractDrawer.className = "candidate-abstract-drawer";

    const abstractSummary = document.createElementNS("http://www.w3.org/1999/xhtml", "summary");
    abstractSummary.className = "candidate-abstract-summary";
    abstractSummary.textContent = "Abstract";
    abstractDrawer.appendChild(abstractSummary);

    const abstractBody = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    abstractBody.className = "candidate-abstract-text";

    const rawAbs = (typeof c.abstract === "string" ? c.abstract : (typeof c.abstractNote === "string" ? c.abstractNote : ""));
    const absText = rawAbs.trim();
    if (absText) {
      abstractBody.textContent = absText;
    } else {
      const emptySpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      emptySpan.className = "candidate-abstract-empty";
      emptySpan.textContent = "No abstract available for this publication.";
      abstractBody.appendChild(emptySpan);
    }
    abstractDrawer.appendChild(abstractBody);
    card.appendChild(abstractDrawer);

    cardsList.appendChild(card);
  });

  groupDiv.appendChild(cardsList);

  let isImporting = false;

  function updateSelectionState() {
    const cardCheckboxes = Array.from(cardsList.querySelectorAll(".candidate-select-checkbox:not(:disabled)"));
    const selectedCount = cardCheckboxes.filter(cb => cb.checked).length;

    importBtn.textContent = `⬇ Import Selected (${selectedCount})`;
    importBtn.disabled = (selectedCount === 0 || isImporting);

    if (cardCheckboxes.length === 0) {
      selectAllCheckbox.checked = false;
      selectAllCheckbox.indeterminate = false;
      selectAllCheckbox.disabled = true;
    } else if (selectedCount === 0) {
      selectAllCheckbox.checked = false;
      selectAllCheckbox.indeterminate = false;
      selectAllCheckbox.disabled = false;
    } else if (selectedCount === cardCheckboxes.length) {
      selectAllCheckbox.checked = true;
      selectAllCheckbox.indeterminate = false;
      selectAllCheckbox.disabled = false;
    } else {
      selectAllCheckbox.checked = false;
      selectAllCheckbox.indeterminate = true;
      selectAllCheckbox.disabled = false;
    }
  }

  // Card checkbox changes
  cardsList.addEventListener("change", (e) => {
    const cb = e.target.closest(".candidate-select-checkbox");
    if (!cb || cb.disabled) return;
    const key = cb.dataset.candidateKey;
    if (cb.checked) {
      selectedCandidateKeys.add(key);
    } else {
      selectedCandidateKeys.delete(key);
    }
    updateSelectionState();
  });

  // Select all checkbox
  selectAllCheckbox.addEventListener("change", () => {
    const shouldCheck = selectAllCheckbox.checked;
    const cardCheckboxes = Array.from(cardsList.querySelectorAll(".candidate-select-checkbox:not(:disabled)"));
    cardCheckboxes.forEach(cb => {
      cb.checked = shouldCheck;
      const key = cb.dataset.candidateKey;
      if (shouldCheck) {
        selectedCandidateKeys.add(key);
      } else {
        selectedCandidateKeys.delete(key);
      }
    });
    updateSelectionState();
  });

  async function executeImport(selectedItems, targetColId, newColName = null) {
    if (isImporting || !selectedItems || selectedItems.length === 0) return;
    isImporting = true;
    importBtn.disabled = true;
    importBtn.textContent = "⏳ Staging PDFs...";

    try {
      if (targetColId === "__new__" || newColName) {
        const colName = (newColName || (newCollectionInput ? newCollectionInput.value : "") || "").trim();
        if (!colName) {
          addNotification("Please enter a name for the new collection, or choose an existing collection.", "warning");
          if (newCollectionInput && typeof newCollectionInput.focus === "function") newCollectionInput.focus();
          isImporting = false;
          updateSelectionState();
          return;
        }

        let libID = null;
        try {
          const pane = resolveZoteroPane();
          libID = resolveActiveLibraryID(pane);
        } catch (e) {}
        if (libID === undefined || libID === null) {
          const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
          libID = zot?.Libraries?.userLibraryID || 1;
        }

        const zot = (typeof Zotero !== "undefined" && Zotero.Collection) ? Zotero : (resolveZotero() || (typeof globalThis !== "undefined" ? globalThis.Zotero : null));
        if (zot && zot.Collection) {
          let col = new zot.Collection();
          col.name = colName;
          col.libraryID = libID;
          targetColId = await col.saveTx();
        } else {
          console.warn("Zocrates: Zotero.Collection not available for creating collection");
          addNotification("Could not create new collection: Zotero.Collection API unavailable.", "error");
          isImporting = false;
          updateSelectionState();
          return;
        }

        populateCandidateCollectionSelect(collectionSelect, libID);
        if (targetColId) {
          collectionSelect.value = String(targetColId);
        }
        newCollectionRow.classList.add("hidden");
      }

      const prepRes = await fetch(`${API_BASE}/discover/prepare_batch`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ candidates: selectedItems })
      });

      if (!prepRes.ok) {
        let errDetail = prepRes.statusText;
        try {
          const errJson = await prepRes.json();
          if (errJson && errJson.detail) errDetail = errJson.detail;
        } catch (e) {}
        throw new Error(errDetail);
      }

      const prepData = await prepRes.json();

      if (prepData.skipped_duplicates && prepData.skipped_duplicates.length > 0) {
        addNotification(`Skipped ${prepData.skipped_duplicates.length} duplicate paper(s) already in your library.`, "info");
      }

      importBtn.textContent = "📥 Importing to Zotero...";
      const scholarImport = getScholarImport();
      if (!scholarImport) {
        throw new Error("ScholarImport module not available in Zotero scope");
      }

      const importResult = await scholarImport.importBatch({
        stagedItems: prepData.staged_items,
        batchToken: prepData.batch_token,
        targetCollectionId: targetColId,
        window: (typeof window !== "undefined" ? window : null)
      });

      const failedKeys = new Set();
      if (importResult.failedItems && Array.isArray(importResult.failedItems)) {
        importResult.failedItems.forEach(fi => {
          const fc = fi.candidate || fi;
          const fk = fc.candidate_id || fc.title || "";
          if (fk) failedKeys.add(fk);
        });
      }

      const itemsMap = new Map();
      if (importResult.importedItems && Array.isArray(importResult.importedItems)) {
        importResult.importedItems.forEach((res) => {
          const cid = res.candidateId || (res.item && res.item.getField && res.item.getField("title")) || "";
          if (cid) itemsMap.set(cid, res);
        });
      }

      let countWithoutPdf = 0;

      // Mark imported cards in-place
      selectedItems.forEach((cand, i) => {
        const k = getCandidateKey(cand, i + 1);
        const candId = cand.candidate_id || cand.title || "";
        if (candId && failedKeys.has(candId)) {
          // Do not mark card as imported if this candidate failed
          return;
        }

        importedCandidateKeys.add(k);
        selectedCandidateKeys.delete(k);

        const card = cardsList.querySelector(`.candidate-paper-card[data-candidate-key="${k}"]`);
        if (card) {
          card.classList.add("imported");
          const cb = card.querySelector(".candidate-select-checkbox");
          if (cb) {
            cb.checked = false;
            cb.disabled = true;
          }

          const res = itemsMap.get(cand.candidate_id) || itemsMap.get(cand.title);
          const hasAtt = res && Boolean(res.attachmentKey);
          const paperUrl = (res && res.oaUrl) || cand.oa_pdf_url || cand.landing_page_url || cand.url || "";

          const badgesEl = card.querySelector(".candidate-card-badges");
          if (badgesEl && !badgesEl.querySelector(".candidate-badge.imported")) {
            const impBadge = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
            if (hasAtt) {
              impBadge.className = "candidate-badge imported";
              impBadge.textContent = "✓ Imported";
            } else {
              impBadge.className = "candidate-badge imported-no-pdf";
              impBadge.textContent = "✓ Imported (No PDF)";
              impBadge.title = "Metadata saved, but PDF download was blocked by repository anti-bot protection (Cloudflare).";
              countWithoutPdf++;
            }
            badgesEl.appendChild(impBadge);
          }

          if (!hasAtt && paperUrl) {
            const metaEl = card.querySelector(".candidate-card-meta") || card;
            if (!metaEl.querySelector(".candidate-open-repo-link")) {
              const openLink = document.createElementNS("http://www.w3.org/1999/xhtml", "a");
              openLink.className = "candidate-open-repo-link";
              openLink.href = paperUrl;
              openLink.textContent = "🔗 Open Paper URL";
              openLink.title = "Open open-access repository page in browser to save PDF directly";
              openLink.addEventListener("click", (e) => {
                e.preventDefault();
                try {
                  const zot = resolveZotero();
                  if (zot && typeof zot.launchURL === "function") {
                    zot.launchURL(paperUrl);
                  } else if (typeof window !== "undefined" && typeof window.open === "function") {
                    window.open(paperUrl, "_blank");
                  }
                } catch (err) {}
              });
              metaEl.appendChild(openLink);
            }
          }
        }
      });

      const impCount = importResult.importedCount || 0;
      const attCount = importResult.attachmentCount || 0;
      if (countWithoutPdf > 0) {
        addNotification(`Imported ${impCount} paper(s) (${attCount} PDF(s) attached). ${countWithoutPdf} repository URL(s) require manual download due to Cloudflare anti-bot protection.`, "warning");
      } else {
        addNotification(`Imported ${impCount} paper(s) (${attCount} PDF(s) attached)!`, "success");
      }

      if (importResult.failedItems && importResult.failedItems.length > 0) {
        console.error("Zocrates: Literature import failures:", importResult.failedItems);
        const firstErr = importResult.failedItems[0].error || "";
        addNotification(`Warning: ${importResult.failedItems.length} paper(s) failed during import${firstErr ? `: ${firstErr}` : ""}.`, "warning");
      }
    } catch (err) {
      console.error("Zocrates: Literature import error:", err);
      addNotification("Literature import failed: " + (err.message || err), "error");
    } finally {
      isImporting = false;
      updateSelectionState();
    }
  }

  // Import button click
  importBtn.addEventListener("click", () => {
    if (isImporting) return;

    const selectedItems = [];
    candidates.forEach((cand, i) => {
      const k = getCandidateKey(cand, i + 1);
      if (selectedCandidateKeys.has(k) && !importedCandidateKeys.has(k)) {
        selectedItems.push(cand);
      }
    });

    if (selectedItems.length === 0) return;

    let targetColId = collectionSelect.value ? collectionSelect.value : null;
    let targetColName = "📁 My Library (Root)";
    let newColName = "";

    if (collectionSelect.value === "__new__") {
      newColName = (newCollectionInput.value || "").trim();
      if (!newColName) {
        addNotification("Please enter a name for the new collection, or choose an existing collection.", "warning");
        if (typeof newCollectionInput.focus === "function") {
          newCollectionInput.focus();
        }
        return;
      }
      targetColName = `📁 New Collection: "${newColName}"`;
    } else if (collectionSelect.selectedIndex >= 0 && collectionSelect.options[collectionSelect.selectedIndex]) {
      targetColName = collectionSelect.options[collectionSelect.selectedIndex].textContent || targetColName;
    }

    const oaCount = selectedItems.filter(c => c.is_oa || c.oa_pdf_url).length;

    const modal = getInfoModal();
    if (!modal) {
      executeImport(selectedItems, targetColId, newColName);
      return;
    }

    const titlesListEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    titlesListEl.style.cssText = "max-height: 140px; overflow-y: auto; padding: 6px 10px; background: var(--chip-bg, rgba(0,0,0,0.15)); border-radius: 6px; font-size: 11.5px; display: flex; flex-direction: column; gap: 4px; border: 1px solid var(--border, rgba(255,255,255,0.08));";
    selectedItems.forEach((cand, idx) => {
      const pEl = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      pEl.textContent = `${idx + 1}. ${cand.title || "Untitled"} (${cand.year || 'n.d.'})${cand.is_oa || cand.oa_pdf_url ? ' [PDF]' : ''}`;
      titlesListEl.appendChild(pEl);
    });

    modal.show({
      window: (typeof window !== "undefined" ? window : null),
      title: "Confirm Literature Import",
      subtitle: "Review the papers to stage and import into your Zotero library.",
      rows: [
        { label: "Destination", value: targetColName },
        { label: "Total Papers", value: String(selectedItems.length) },
        { label: "Open Access PDFs", value: `${oaCount} of ${selectedItems.length} available` },
        titlesListEl
      ],
      buttons: [
        {
          label: "Cancel",
          onClick: () => {}
        },
        {
          label: "Confirm & Import",
          primary: true,
          onClick: () => {
            setTimeout(() => {
              executeImport(selectedItems, targetColId, newColName);
            }, 0);
            return true;
          }
        }
      ]
    });
  });

  groupDiv._updateSelectionState = updateSelectionState;
  updateSelectionState();

  // Asynchronously synchronize card states with the user's live Zotero library
  syncCandidatesWithLibrary(groupDiv, candidates);

  const actionsDiv = wrapperDiv.querySelector(".message-actions");
  if (actionsDiv) {
    wrapperDiv.insertBefore(groupDiv, actionsDiv);
  } else {
    wrapperDiv.appendChild(groupDiv);
  }

  return groupDiv;
}

async function syncCandidatesWithLibrary(groupDiv, candidates) {
  if (!groupDiv || !Array.isArray(candidates) || candidates.length === 0) return;

  const zot = resolveZotero();
  if (!zot) return;

  let libraryID = 1;
  try {
    libraryID = (zot.Libraries && zot.Libraries.userLibraryID) || 1;
  } catch (e) {}

  let anyUpdated = false;

  for (let i = 0; i < candidates.length; i++) {
    const cand = candidates[i];
    if (!cand) continue;

    const k = getCandidateKey(cand, i + 1);
    const card = groupDiv.querySelector(`.candidate-paper-card[data-candidate-key="${k}"]`);
    if (!card) continue;

    // If card is already marked imported, skip
    if (card.classList.contains("imported") && card.querySelector(".candidate-badge.imported")) {
      continue;
    }

    try {
      let itemIDs = [];
      let rawDoi = (cand.doi || cand.DOI || "").trim();
      let cleanDoi = rawDoi;
      if (cleanDoi && zot.Utilities && typeof zot.Utilities.cleanDOI === "function") {
        cleanDoi = zot.Utilities.cleanDOI(cleanDoi) || rawDoi;
      }

      // 1. Search by DOI
      if (cleanDoi && typeof zot.Search === "function") {
        try {
          const s = new zot.Search();
          s.libraryID = libraryID;
          s.addCondition("DOI", "is", cleanDoi);
          itemIDs = await s.search();
        } catch (sErr) {}
      }

      // 2. Search by arXiv ID if preprint
      if ((!itemIDs || itemIDs.length === 0) && cand.arxiv_id && typeof zot.Search === "function") {
        try {
          const s = new zot.Search();
          s.libraryID = libraryID;
          s.addCondition("extra", "contains", cand.arxiv_id);
          itemIDs = await s.search();
        } catch (sErr) {}
      }

      // 3. Search by Title
      if ((!itemIDs || itemIDs.length === 0) && cand.title && typeof zot.Search === "function") {
        try {
          const s = new zot.Search();
          s.libraryID = libraryID;
          s.addCondition("title", "is", cand.title.trim());
          itemIDs = await s.search();
        } catch (sErr) {}
      }

      if (itemIDs && itemIDs.length > 0) {
        // Paper is already present in library!
        importedCandidateKeys.add(k);
        selectedCandidateKeys.delete(k);
        card.classList.add("imported");

        const cb = card.querySelector(".candidate-select-checkbox");
        if (cb) {
          cb.checked = false;
          cb.disabled = true;
        }

        // Check if item has a PDF attachment
        let hasPdf = false;
        try {
          if (zot.Items && typeof zot.Items.getAsync === "function") {
            const zotItem = await zot.Items.getAsync(itemIDs[0]);
            if (zotItem) {
              const attIDs = (typeof zotItem.getAttachments === "function") ? zotItem.getAttachments() : [];
              for (let aID of attIDs) {
                const att = await zot.Items.getAsync(aID);
                if (att && (
                  (typeof att.isPDFAttachment === "function" && att.isPDFAttachment()) ||
                  att.attachmentContentType === "application/pdf" ||
                  (att.attachmentFilename && att.attachmentFilename.toLowerCase().endsWith(".pdf"))
                )) {
                  hasPdf = true;
                  break;
                }
              }
            }
          }
        } catch (attErr) {}

        const badgesEl = card.querySelector(".candidate-card-badges");
        if (badgesEl) {
          let impBadge = badgesEl.querySelector(".candidate-badge.imported");
          if (!impBadge) {
            impBadge = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
            badgesEl.appendChild(impBadge);
          }
          if (hasPdf) {
            impBadge.className = "candidate-badge imported";
            impBadge.textContent = "✓ Imported";
          } else {
            impBadge.className = "candidate-badge imported-no-pdf";
            impBadge.textContent = "✓ Imported (No PDF)";
            impBadge.title = "Paper metadata present in library, but no PDF attachment found.";
          }
        }

        const paperUrl = cand.oa_pdf_url || cand.landing_page_url || cand.url || "";
        if (!hasPdf && paperUrl) {
          const metaEl = card.querySelector(".candidate-card-meta") || card;
          if (!metaEl.querySelector(".candidate-open-repo-link")) {
            const openLink = document.createElementNS("http://www.w3.org/1999/xhtml", "a");
            openLink.className = "candidate-open-repo-link";
            openLink.href = paperUrl;
            openLink.textContent = "🔗 Open Paper URL";
            openLink.title = "Open open-access repository page in browser to save PDF directly";
            openLink.addEventListener("click", (e) => {
              e.preventDefault();
              try {
                if (typeof zot.launchURL === "function") {
                  zot.launchURL(paperUrl);
                } else if (typeof window !== "undefined" && typeof window.open === "function") {
                  window.open(paperUrl, "_blank");
                }
              } catch (err) {}
            });
            metaEl.appendChild(openLink);
          }
        }

        anyUpdated = true;
      }
    } catch (checkErr) {
      console.warn("Zocrates: Error syncing candidate card with library:", checkErr);
    }
  }

  if (anyUpdated) {
    const updateSelectionStateFn = groupDiv._updateSelectionState;
    if (typeof updateSelectionStateFn === "function") {
      updateSelectionStateFn();
    }
  }
}

function appendMessageDOM(role, text, isMarkdown = true, timestamp = null, author = null, llmModel = null, duration = null, metadata = null) {
  const formattedTimestamp = timestamp || new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const wrapperDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  wrapperDiv.className = `message-wrapper ${role}`;
  wrapperDiv.dataset.timestamp = formattedTimestamp;
  if (metadata && metadata.invocation_id) {
    wrapperDiv.dataset.invocationId = metadata.invocation_id;
  }

  // Header Row (Icon Badge + Author Name)
  const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerDiv.className = "message-header";

  const iconSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  iconSpan.className = "message-header-icon";
  if (role === "user") {
    iconSpan.textContent = "👤";
  } else {
    const imgLogo = document.createElementNS("http://www.w3.org/1999/xhtml", "img");
    imgLogo.src = "icon.png";
    imgLogo.alt = "Zocrates";
    imgLogo.style.cssText = "width:15px;height:15px;border-radius:3px;vertical-align:middle;object-fit:contain;flex-shrink:0;";
    iconSpan.appendChild(imgLogo);
  }

  const authorSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  authorSpan.className = "message-header-author";
  authorSpan.textContent = role === "user" ? getZoteroUsername() : formatAuthorName(role, author);

  headerDiv.appendChild(iconSpan);
  headerDiv.appendChild(authorSpan);
  wrapperDiv.appendChild(headerDiv);

  const msgDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  msgDiv.className = `message ${role}`;
  const safeText = text != null ? String(text) : "";
  msgDiv._rawText = safeText;
  msgDiv._citations = (metadata && Array.isArray(metadata.citations)) ? metadata.citations : null;
  msgDiv._metadata = metadata;
  wrapperDiv._citations = msgDiv._citations;
  wrapperDiv._metadata = metadata;
  if (isMarkdown) {
    msgDiv.innerHTML = formatMessageContent(safeText, metadata, role === "user");
    fixCodeBlockCopyButtons(msgDiv);
    decorateAssetImages(msgDiv);
    if (!isStreaming) {
      decorateMermaidCodeBlocks(msgDiv);
    }
  } else {
    msgDiv.textContent = safeText;
  }
  wrapperDiv.appendChild(msgDiv);

  // Message Action Bar (Copy + Info + Rerun icons under bubble)
  const actionsDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionsDiv.className = "message-actions";

  const actionsLeftDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionsLeftDiv.className = "message-actions-left";

  const actionsRightDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionsRightDiv.className = "message-actions-right";

  actionsDiv.appendChild(actionsLeftDiv);
  actionsDiv.appendChild(actionsRightDiv);

  if (role === "assistant" && hasMessageCitations(safeText, metadata)) {
    const toggleBtn = createCitationToggleBtn(wrapperDiv);
    actionsLeftDiv.appendChild(toggleBtn);
  }

  const infoAuthor = formatAuthorName(role, author);
  const formattedTime = timestamp || new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const modelToDisplay = role === "assistant" ? (llmModel || currentLlmModel || (modelNameDisplay ? modelNameDisplay.textContent : "") || "google/gemini-3.8-flash") : null;
  const durationToDisplay = (role === "assistant" && duration !== null && duration !== undefined) ? duration : null;

  // Rerun prompt button (for User messages - first icon of the 4 icons)
  if (role === "user") {
    const rerunBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    rerunBtn.className = "message-action-btn rerun-btn";
    rerunBtn.setAttribute("title", "Rerun or edit prompt");
    rerunBtn.innerHTML = `
      <svg class="rerun-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
    `;

    rerunBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      triggerRerunFlow(rerunBtn, wrapperDiv, text);
    });

    actionsRightDiv.appendChild(rerunBtn);
  }

  // Optional Report Issue button (strictly opt-in: rendered ONLY on explicit error bubbles)
  if (metadata && metadata.isError === true) {
    wrapperDiv.classList.add("has-error-report");
    const reportBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    reportBtn.setAttribute("type", "button");
    reportBtn.className = "message-action-btn error-report-action-btn";
    reportBtn.setAttribute("title", "Generate error & diagnostic report");
    reportBtn.textContent = "🛠️ Report Issue...";
    const reportErrorMsg = metadata.errorMessage || safeText;
    const reportInvId = metadata.invocationId || metadata.invocation_id || null;
    reportBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openDiagnosticReportModal({
        errorMessage: reportErrorMsg,
        sessionId: currentSessionId || resolveActiveSessionId() || null,
        invocationId: reportInvId,
      });
    });
    actionsRightDiv.appendChild(reportBtn);
  }

  // Save as Zotero Note button (for both User and Assistant messages - placed immediately to the left of Copy)
  const saveNoteBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  saveNoteBtn.className = "message-action-btn save-note-btn";
  saveNoteBtn.setAttribute("title", "Save as Zotero Note (indexed by Zocrates)");
  saveNoteBtn.innerHTML = `
    <svg class="note-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>
    <svg class="check-icon hidden" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="color: #22c55e;"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
  `;

  saveNoteBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const contentToSave = getMessageTextForCopy(msgDiv, wrapperDiv);
    const msgTitle = extractMessageTitle(contentToSave, role);
    const noteTitle = `Zocrates Message: ${msgTitle}`;
    triggerSaveNoteFlow(saveNoteBtn, contentToSave, noteTitle);
  });

  actionsRightDiv.appendChild(saveNoteBtn);

  // Copy button
  const copyBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  copyBtn.className = "message-action-btn copy-btn";
  copyBtn.setAttribute("title", "Copy message");
  copyBtn.innerHTML = `
    <svg class="copy-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
    <svg class="check-icon hidden" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="color: #22c55e;"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
  `;

  copyBtn.addEventListener("click", async () => {
    try {
      const contentToCopy = getMessageTextForCopy(msgDiv, wrapperDiv);
      const copied = await copyTextToClipboard(contentToCopy);
      if (!copied) {
        if (typeof console !== "undefined" && typeof console.warn === "function") {
          console.warn("Zocrates: failed to copy message to clipboard");
        }
        return;
      }
      const copyIcon = copyBtn.querySelector(".copy-icon");
      const checkIcon = copyBtn.querySelector(".check-icon");
      if (copyIcon && checkIcon) {
        copyIcon.classList.add("hidden");
        checkIcon.classList.remove("hidden");
        setTimeout(() => {
          copyIcon.classList.remove("hidden");
          checkIcon.classList.add("hidden");
        }, 1500);
      }
    } catch (err) {
      console.error("Failed to copy message:", err);
    }
  });

  actionsRightDiv.appendChild(copyBtn);

  // Info button
  const infoBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  infoBtn.className = "message-action-btn info-btn";
  infoBtn.setAttribute("role", "button");
  infoBtn.setAttribute("tabindex", "0");
  const titleAttr = `Author: ${infoAuthor}${modelToDisplay ? `\nModel: ${modelToDisplay}` : ''}${durationToDisplay !== null ? `\nDuration: ${durationToDisplay}s` : ''}\nCompleted: ${formattedTime}`;
  infoBtn.setAttribute("title", titleAttr);
  infoBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>`;

  const messageMeta = {
    role,
    author: author || (role === "user" ? "User" : "Coordinator Agent"),
    formattedAuthor: infoAuthor,
    model: modelToDisplay,
    timestamp: formattedTime,
    duration: durationToDisplay,
    usage: metadata?.usage || null,
    turn_cost_usd: metadata?.turn_cost_usd !== undefined ? metadata.turn_cost_usd : (metadata?.usage?.cost_usd ?? null),
    agent_models: metadata?.agent_models || metadata?.usage?.agent_models || null,
    finish_reason: metadata?.finish_reason || null,
    active_context_items: metadata?.active_context_items || null,
    snippets: metadata?.snippets || null,
    event_id: metadata?.event_id || metadata?.id || null,
    invocation_id: metadata?.invocation_id || null,
    intermediate_steps: metadata?.intermediate_steps || [],
    text: text,
    citations: metadata?.citations || null,
    references: metadata?.references || null,
    candidates: metadata?.candidates || null,
    citation_verification: metadata?.citation_verification || metadata?.citation_verification_telemetry || null
  };
  infoBtn._metadata = messageMeta;
  wrapperDiv._metadata = messageMeta;
  msgDiv._metadata = messageMeta;

  infoBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openMessageInfoModal(infoBtn._metadata);
  });
  infoBtn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      openMessageInfoModal(infoBtn._metadata);
    }
  });

  actionsRightDiv.appendChild(infoBtn);
  if (metadata && Array.isArray(metadata.candidates) && metadata.candidates.length > 0) {
    renderCandidatePapersGroup(wrapperDiv, metadata.candidates, metadata.invocation_id);
  }
  wrapperDiv.appendChild(actionsDiv);

  if (role === "assistant") {
    ensureClaimsInspectorBtn(wrapperDiv);
  }

  chatMessages.appendChild(wrapperDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return msgDiv;
}

function appendThoughtBubbleDOM(author, thought) {
  const thoughtDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  thoughtDiv.className = "thought-bubble collapsed";

  const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  headerDiv.className = "thought-header";

  const titleDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  titleDiv.className = "thought-title";
  const formattedAuthor = formatAuthorName("assistant", author);
  let labelText = formattedAuthor + " internal thoughts";
  if (author === "System" && thought.startsWith("Calling tool:")) {
    labelText = thought;
  }
  titleDiv.textContent = labelText;
  headerDiv.appendChild(titleDiv);

  const chevronSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  chevronSpan.className = "thought-chevron";
  chevronSpan.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:14px;height:14px;fill:currentColor;"><path d="M7 10l5 5 5-5z"/></svg>`;
  headerDiv.appendChild(chevronSpan);

  thoughtDiv.appendChild(headerDiv);

  const contentDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  contentDiv.className = "thought-content";
  contentDiv.textContent = thought;
  thoughtDiv.appendChild(contentDiv);

  headerDiv.addEventListener("click", () => {
    thoughtDiv.classList.toggle("collapsed");
  });

  chatMessages.appendChild(thoughtDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return thoughtDiv;
}

function appendMessage(role, text, isMarkdown = true, timestamp = null, author = null, llmModel = null, duration = null, metadata = null) {
  const element = appendMessageDOM(role, text, isMarkdown, timestamp, author, llmModel, duration, metadata);
  
  // Save to active session storage
  const session = sessions.find(s => s.id === currentSessionId);
  if (session) {
    session.messages.push({ role, text, isMarkdown, timestamp, author, llm_model: llmModel, duration, metadata });
  }
  return element;
}

function appendThoughtBubble(author, thought) {
  const element = appendThoughtBubbleDOM(author, thought);
  
  // Save to active session storage
  const session = sessions.find(s => s.id === currentSessionId);
  if (session) {
    session.messages.push({ role: "assistant", text: thought, thought: thought, author: author });
  }
  return element;
}

function triggerRerunFlow(anchorBtn, wrapperDiv, promptText) {
  if (isStreaming) return;

  document.querySelectorAll(".rerun-popover").forEach(el => el.remove());

  const popover = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  popover.className = "rerun-popover dropdown-menu";
  popover.style.cssText = "position:fixed;z-index:10000;background:var(--bg-card,#ffffff);border:1px solid var(--border,#e0e0e0);border-radius:8px;padding:4px;box-shadow:0 8px 25px rgba(0,0,0,0.3);min-width:160px;white-space:nowrap;";

  const resendBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  resendBtn.className = "dropdown-item";
  resendBtn.style.cssText = "display:flex;align-items:center;gap:6px;width:100%;padding:7px 10px;font-size:12px;color:var(--text-main);background:transparent;border:none;border-radius:4px;cursor:pointer;text-align:left;";
  resendBtn.innerHTML = `<span style="font-size:12px;">🔄</span> <span>Resend</span>`;
  resendBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.remove();
    confirmAndExecuteRerun(wrapperDiv, promptText);
  });
  popover.appendChild(resendBtn);

  const editBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  editBtn.className = "dropdown-item";
  editBtn.style.cssText = "display:flex;align-items:center;gap:6px;width:100%;padding:7px 10px;font-size:12px;color:var(--text-main);background:transparent;border:none;border-radius:4px;cursor:pointer;text-align:left;";
  editBtn.innerHTML = `<span style="font-size:12px;">✏️</span> <span>Edit and resend</span>`;
  editBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.remove();
    enableInlineEditing(wrapperDiv, promptText);
  });
  popover.appendChild(editBtn);

  document.body.appendChild(popover);

  const rect = anchorBtn.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();

  let top = rect.bottom + 4;
  if (top + popoverRect.height > window.innerHeight - 10) {
    top = Math.max(10, rect.top - popoverRect.height - 4);
  }
  let left = Math.min(rect.left, window.innerWidth - popoverRect.width - 10);
  left = Math.max(10, left);

  popover.style.top = top + "px";
  popover.style.left = left + "px";

  const closeHandler = (e) => {
    if (!popover.contains(e.target) && e.target !== anchorBtn) {
      popover.remove();
      document.removeEventListener("click", closeHandler);
    }
  };
  setTimeout(() => document.addEventListener("click", closeHandler), 10);
}

function normalizeRestoredActiveItem(item) {
  if (!item || typeof item !== "object") return item;
  const resolvedKey = item.key || item.item_key || item.itemKey || "";
  return {
    ...item,
    key: resolvedKey,
    item_key: item.item_key || resolvedKey,
    itemKey: item.itemKey || resolvedKey,
    type: item.type || "item"
  };
}

function normalizeRestoredSnippet(s) {
  if (!s || typeof s !== "object") return s;
  const itemKey = s.item_key || s.itemKey;
  const parentKey = s.parent_key || s.parentKey;
  const paperTitle = s.paper_title || s.paperTitle;
  const authorStr = s.author_str || s.authorStr;
  const imageData = s.image_data || s.imageData;
  const thumbnailData = s.thumbnail_data || s.thumbnailData;
  const mimeType = s.mime_type || s.mimeType || "image/jpeg";
  const annotationId = s.annotation_id || s.annotationId;
  const annotationItemId = s.annotation_item_id || s.annotationItemId;

  return {
    ...s,
    item_key: itemKey,
    itemKey: itemKey,
    parent_key: parentKey,
    parentKey: parentKey,
    paper_title: paperTitle,
    paperTitle: paperTitle,
    author_str: authorStr,
    authorStr: authorStr,
    image_data: imageData,
    imageData: imageData,
    thumbnail_data: thumbnailData,
    thumbnailData: thumbnailData,
    mime_type: mimeType,
    mimeType: mimeType,
    annotation_id: annotationId,
    annotationId: annotationId,
    annotation_item_id: annotationItemId,
    annotationItemId: annotationItemId
  };
}

function enableInlineEditing(wrapperDiv, currentText) {
  const msgDiv = wrapperDiv.querySelector(".message.user");
  if (!msgDiv) return;

  const session = currentSessionId ? sessions.find(s => s.id === currentSessionId) : null;
  const msgTimestamp = wrapperDiv.dataset.timestamp;
  let targetIndex = -1;
  if (session && msgTimestamp) {
    targetIndex = session.messages.findIndex(m => m.timestamp === msgTimestamp && (m.role === "user" || m.sender === "user"));
  }
  if (session && targetIndex === -1) {
    const allWrappers = Array.from(chatMessages.querySelectorAll(".message-wrapper"));
    const wrapperIndex = allWrappers.indexOf(wrapperDiv);
    if (wrapperIndex !== -1 && wrapperIndex < session.messages.length) {
      targetIndex = wrapperIndex;
    }
  }

  const infoBtnMeta = wrapperDiv.querySelector(".info-btn")?._metadata;
  const targetMsg = (targetIndex !== -1 && session?.messages[targetIndex]) || infoBtnMeta || {};

  activeItemsList = targetMsg.active_context_items
    ? targetMsg.active_context_items.map(normalizeRestoredActiveItem)
    : [];
  activeSnippetsList = targetMsg.snippets
    ? targetMsg.snippets.map(normalizeRestoredSnippet)
    : [];
  if (activeSnippetsList.length > 0) {
    contextMode = "papers";
  } else if (activeItemsList.some(i => i.type === "collection")) {
    contextMode = "collection";
  } else if (activeItemsList.some(i => i.type === "library")) {
    contextMode = "library";
  } else if (activeItemsList.length > 0) {
    contextMode = "papers";
  }
  renderActiveChips();

  const originalHTML = msgDiv.innerHTML;

  msgDiv.classList.add("inline-editing");
  msgDiv.innerHTML = "";

  const editContainer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  editContainer.className = "inline-edit-container";
  editContainer.style.cssText = "display:flex;flex-direction:column;gap:8px;width:100%;";

  const textarea = document.createElementNS("http://www.w3.org/1999/xhtml", "textarea");
  textarea.className = "inline-edit-textarea";
  textarea.value = currentText;
  textarea.style.cssText = "width:100%;min-height:60px;max-height:200px;padding:8px 10px;border-radius:6px;border:1px solid var(--primary, #007acc);background:var(--bg-card, #ffffff);color:var(--text-main, #222222);font-family:inherit;font-size:13px;line-height:1.4;resize:vertical;outline:none;box-sizing:border-box;";

  const btnRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  btnRow.style.cssText = "display:flex;justify-content:flex-end;gap:8px;";

  const cancelBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  cancelBtn.className = "inline-edit-btn cancel-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.style.cssText = "padding:5px 12px;font-size:12px;border-radius:4px;border:1px solid var(--border, #e0e0e0);background:transparent;color:var(--text-main);cursor:pointer;";
  cancelBtn.addEventListener("click", () => {
    msgDiv.classList.remove("inline-editing");
    msgDiv.innerHTML = originalHTML;
  });

  const saveResendBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  saveResendBtn.className = "inline-edit-btn resend-btn";
  saveResendBtn.textContent = "Resend";
  saveResendBtn.style.cssText = "padding:5px 12px;font-size:12px;border-radius:4px;border:none;background:var(--primary, #007acc);color:#ffffff;font-weight:600;cursor:pointer;";
  saveResendBtn.addEventListener("click", () => {
    const updatedText = textarea.value.trim();
    if (!updatedText) return;
    confirmAndExecuteRerun(wrapperDiv, updatedText);
  });

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(saveResendBtn);

  editContainer.appendChild(textarea);
  editContainer.appendChild(btnRow);
  msgDiv.appendChild(editContainer);

  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

function confirmAndExecuteRerun(wrapperDiv, newPromptText) {
  if (isStreaming) return;

  showWarningModal(
    "Delete Subsequent Messages?",
    "Rerunning this prompt will permanently delete all subsequent responses in this conversation. Are you sure you want to proceed?",
    "Delete & Resend",
    async () => {
      await executeRerun(wrapperDiv, newPromptText);
    }
  );
}

function showWarningModal(title, bodyMessage, confirmBtnText, onConfirm) {
  document.querySelectorAll(".warning-modal-overlay").forEach(el => el.remove());

  const overlay = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  overlay.className = "warning-modal-overlay";
  overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:20000;background:rgba(0,0,0,0.5);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;padding:16px;";

  const modal = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  modal.className = "warning-modal-card";
  modal.style.cssText = "background:var(--bg-card, #ffffff);border:1px solid var(--border, #e0e0e0);border-radius:12px;padding:20px;max-width:340px;width:100%;box-shadow:0 10px 25px rgba(0,0,0,0.3);color:var(--text-main, #222222);display:flex;flex-direction:column;gap:14px;";

  const header = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  header.style.cssText = "display:flex;align-items:center;gap:10px;";
  header.innerHTML = `
    <span style="font-size:20px;">⚠️</span>
    <span style="font-weight:700;font-size:15px;color:var(--text-main);">${escapeHTML(title)}</span>
  `;

  const content = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  content.style.cssText = "font-size:13px;color:var(--text-muted);line-height:1.5;";
  content.textContent = bodyMessage;

  const btnRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  btnRow.style.cssText = "display:flex;justify-content:flex-end;gap:10px;margin-top:6px;";

  const cancelBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  cancelBtn.className = "modal-btn modal-cancel-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.style.cssText = "padding:7px 14px;font-size:12.5px;border-radius:6px;border:1px solid var(--border, #e0e0e0);background:transparent;color:var(--text-main);cursor:pointer;font-weight:500;";
  cancelBtn.addEventListener("click", () => overlay.remove());

  const confirmBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  confirmBtn.className = "modal-btn modal-confirm-btn";
  confirmBtn.textContent = confirmBtnText;
  confirmBtn.style.cssText = "padding:7px 14px;font-size:12.5px;border-radius:6px;border:none;background:#ef4444;color:#ffffff;cursor:pointer;font-weight:600;";
  confirmBtn.addEventListener("click", () => {
    overlay.remove();
    if (typeof onConfirm === "function") onConfirm();
  });

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(confirmBtn);

  modal.appendChild(header);
  modal.appendChild(content);
  modal.appendChild(btnRow);
  overlay.appendChild(modal);

  document.body.appendChild(overlay);
}

async function executeRerun(wrapperDiv, newPromptText) {
  if (!currentSessionId) return;
  const session = sessions.find(s => s.id === currentSessionId);
  if (!session) return;

  const msgTimestamp = wrapperDiv.dataset.timestamp;
  let targetInvocationId = wrapperDiv.dataset.invocationId || null;
  let targetIndex = -1;

  if (msgTimestamp) {
    targetIndex = session.messages.findIndex(m => m.timestamp === msgTimestamp && (m.role === "user" || m.sender === "user"));
  }

  if (targetIndex === -1) {
    const allWrappers = Array.from(chatMessages.querySelectorAll(".message-wrapper"));
    const wrapperIndex = allWrappers.indexOf(wrapperDiv);
    if (wrapperIndex !== -1 && wrapperIndex < session.messages.length) {
      targetIndex = wrapperIndex;
    }
  }

  const infoBtnMeta = wrapperDiv.querySelector(".info-btn")?._metadata;
  const targetMsg = (targetIndex !== -1 && session.messages[targetIndex]) || infoBtnMeta || {};
  const origActiveContextItems = targetMsg.active_context_items;
  const origSnippets = targetMsg.snippets;

  if (targetIndex !== -1 && !targetInvocationId) {
    targetInvocationId = session.messages[targetIndex]?.invocation_id;
    if (!targetInvocationId && session.messages[targetIndex + 1]?.invocation_id) {
      targetInvocationId = session.messages[targetIndex + 1].invocation_id;
    }
  }

  if (!targetInvocationId) {
    try {
      const sessRes = await fetch(`${API_BASE}/sessions/${currentSessionId}`, {
        headers: getAuthHeaders(),
      });
      if (sessRes.ok) {
        const sessData = await sessRes.json();
        const serverMessages = sessData.session?.messages || [];
        let serverUserMsg = null;
        if (msgTimestamp) {
          serverUserMsg = serverMessages.find(m => m.timestamp === msgTimestamp && (m.role === "user" || m.sender === "user"));
        }
        if (!serverUserMsg && targetIndex !== -1 && targetIndex < serverMessages.length) {
          const candidate = serverMessages[targetIndex];
          if (candidate && (candidate.role === "user" || candidate.sender === "user")) {
            serverUserMsg = candidate;
          }
        }
        if (!serverUserMsg) {
          const userMsgs = serverMessages.filter(m => m.role === "user" || m.sender === "user");
          if (userMsgs.length > 0) {
            serverUserMsg = userMsgs[userMsgs.length - 1];
          }
        }
        if (serverUserMsg && serverUserMsg.invocation_id) {
          targetInvocationId = serverUserMsg.invocation_id;
          wrapperDiv.dataset.invocationId = targetInvocationId;
          if (targetIndex !== -1 && session.messages[targetIndex]) {
            session.messages[targetIndex].invocation_id = targetInvocationId;
          }
        }
      }
    } catch (fetchErr) {
      console.warn("Zocrates: Could not fetch session to resolve invocation_id:", fetchErr);
    }
  }

  if (targetInvocationId) {
    try {
      const rewindRes = await fetch(`${API_BASE}/sessions/${currentSessionId}/rewind`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ invocation_id: targetInvocationId })
      });
      if (!rewindRes.ok) {
        if (rewindRes.status === 404) {
          console.info("Zocrates: Rewind API returned 404 (turn not recorded server-side or already rewound); proceeding.");
        } else {
          const errData = await rewindRes.json().catch(() => ({}));
          console.error("Zocrates: Rewind API returned non-OK status:", errData);
          addNotification("Failed to rewind conversation history: " + (errData.detail || rewindRes.statusText), "error");
          return;
        }
      }
    } catch (err) {
      console.error("Zocrates: Error calling rewind API:", err);
      addNotification("Network error while rewinding conversation history: " + err.message, "error");
      return;
    }

    await loadActiveSessionMessages();
  } else {
    // targetInvocationId is not present (e.g. server never received the request)
    // Perform clean local truncate:
    // Remove wrapperDiv and all subsequent message wrappers from chatMessages.
    let currentEl = wrapperDiv;
    while (currentEl) {
      const nextEl = currentEl.nextElementSibling;
      currentEl.remove();
      currentEl = nextEl;
    }
    if (targetIndex !== -1) {
      session.messages.splice(targetIndex);
    }
  }

  // Restore target user message's active context items and snippets
  activeItemsList = origActiveContextItems
    ? origActiveContextItems.map(normalizeRestoredActiveItem)
    : [];
  activeSnippetsList = origSnippets
    ? origSnippets.map(normalizeRestoredSnippet)
    : [];
  if (activeSnippetsList.length > 0) {
    contextMode = "papers";
  } else if (activeItemsList.some(i => i.type === "collection")) {
    contextMode = "collection";
  } else if (activeItemsList.some(i => i.type === "library")) {
    contextMode = "library";
  } else if (activeItemsList.length > 0) {
    contextMode = "papers";
  }
  renderActiveChips();

  const userTimestamp = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const activeChips = [...activeItemsList];
  const userMeta = {
    role: "user",
    author: "User",
    timestamp: userTimestamp,
    active_context_items: activeChips,
    snippets: activeSnippetsList && activeSnippetsList.length > 0 ? [...activeSnippetsList] : undefined,
    text: newPromptText
  };
  appendMessageDOM("user", newPromptText, true, userTimestamp, "User", null, null, userMeta);

  const updatedSession = sessions.find(s => s.id === currentSessionId);
  if (updatedSession) {
    updatedSession.messages.push(userMeta);
  }

  await streamDialogueResponse(newPromptText);
}

async function handleChatSubmit() {
  // Choke point for ALL submission paths (Send button click and the
  // Ctrl/Cmd+Enter keydown): refuse to POST while the onboarding bootstrap
  // is still provisioning/starting the backend.
  if (!backendReadyForChat) {
    const isInstalling = document.getElementById("bootstrap-progress-card") !== null;
    const msg = isInstalling
      ? "Zocrates is still starting — the AI engine is installing. Please wait for setup to finish."
      : "Zocrates is still starting — connecting to the local backend. Please wait a moment.";
    addNotification(msg, "info");
    return;
  }
  const prompt = chatInput.value.trim();
  if (!prompt) return;

  if (isStreaming) return;

  clearGhostSuggestion();
  cleanupOnboardingResources();
  chatInput.value = "";
  chatInput.style.height = "46px";

  if (!currentSessionId) {
    await createNewSession("New Chat");
  }

  const userTimestamp = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const activeChips = [...activeItemsList];
  const userMeta = {
    role: "user",
    author: "User",
    timestamp: userTimestamp,
    active_context_items: activeChips,
    snippets: activeSnippetsList && activeSnippetsList.length > 0 ? [...activeSnippetsList] : undefined,
    text: prompt
  };
  appendMessageDOM("user", prompt, true, userTimestamp, "User", null, null, userMeta);

  const session = sessions.find(s => s.id === currentSessionId);
  if (session) {
    session.messages.push({
      sender: "user",
      role: "user",
      text: prompt,
      isMarkdown: true,
      timestamp: userTimestamp,
      author: "User",
      active_context_items: activeChips,
      snippets: activeSnippetsList && activeSnippetsList.length > 0 ? [...activeSnippetsList] : undefined
    });
    if (session.title === "New Chat") {
      session.title = prompt.substring(0, 24) + (prompt.length > 24 ? "..." : "");
      renderSessionsList();
    }
  }

  await streamDialogueResponse(prompt);
}

function finalizeChatStream(streamCtx, isAborted = false) {
  if (!streamCtx) return;

  // Remove any remaining interactive HITL checkpoint card from stepsBubble
  if (streamCtx.stepsBubble) {
    const card = streamCtx.stepsBubble.querySelector(".hitl-checkpoint-card");
    if (card) card.remove();
  }

  const requestEndTime = Date.now();
  const startTime = streamCtx.requestStartTime || requestEndTime;
  const durationSec = parseFloat(((requestEndTime - startTime) / 1000).toFixed(2));
  const completionTime = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const finalModel = streamCtx.streamModel || currentLlmModel || (modelNameDisplay ? modelNameDisplay.textContent : "") || "google/gemini-3.8-flash";
  const turnCost = Number(streamCtx.streamUsage?.turn_cost_usd !== undefined ? streamCtx.streamUsage.turn_cost_usd : (streamCtx.streamUsage?.cost_usd ?? 0.0));
  const turnAgentModels = streamCtx.streamUsage?.agent_models || null;

  finalizeIntermediateStepsBubble(streamCtx.stepsBubble, streamCtx.liveSteps, {
    model: finalModel,
    duration: durationSec,
    usage: streamCtx.streamUsage,
    turn_cost_usd: turnCost,
    agent_models: turnAgentModels
  });

  if (!isAborted && streamCtx.streamEnrichedText) {
    streamCtx.currentResponseText = streamCtx.streamEnrichedText;
  }

  const responseTextToRender = isAborted && streamCtx.currentResponseText
    ? streamCtx.currentResponseText + "\n\n*[Generation stopped by user]*"
    : streamCtx.currentResponseText;

  if (!streamCtx.assistantBubble && streamCtx.currentResponseText) {
    const initialModel = streamCtx.streamModel || streamCtx.usedModel;
    streamCtx.assistantBubble = appendMessageDOM("assistant", responseTextToRender, true, null, streamCtx.authorName, initialModel, null, {
      author: streamCtx.authorName,
      model: initialModel,
      active_context_items: streamCtx.payloadItems,
      usage: streamCtx.streamUsage,
      finish_reason: streamCtx.streamFinishReason,
      event_id: streamCtx.streamEventId,
      intermediate_steps: streamCtx.liveSteps,
      citations: streamCtx.streamCitations,
      references: streamCtx.streamReferences,
      candidates: streamCtx.streamCandidates,
      citation_verification: streamCtx.streamCitationVerification,
    });
    if (streamCtx.stepsBubble && streamCtx.assistantBubble && streamCtx.assistantBubble.parentElement) {
      streamCtx.assistantBubble.parentElement.insertBefore(streamCtx.stepsBubble, streamCtx.assistantBubble);
    }
  }

  // Final render with full content (ensures last chunk is displayed)
  if (streamCtx.assistantBubble && streamCtx.currentResponseText) {
    streamCtx.assistantBubble._rawText = responseTextToRender;
    const finalMeta = {
      citations: streamCtx.streamCitations,
      references: streamCtx.streamReferences,
      candidates: streamCtx.streamCandidates,
      citation_verification: streamCtx.streamCitationVerification
    };
    if (streamCtx.streamCitations) {
      streamCtx.assistantBubble._citations = streamCtx.streamCitations;
    }
    streamCtx.assistantBubble._metadata = finalMeta;
    if (streamCtx.assistantBubble.parentElement) {
      streamCtx.assistantBubble.parentElement._citations = streamCtx.streamCitations;
      streamCtx.assistantBubble.parentElement._metadata = finalMeta;
    }
    streamCtx.assistantBubble.innerHTML = formatMessageContent(responseTextToRender, finalMeta);
    fixCodeBlockCopyButtons(streamCtx.assistantBubble);
    decorateAssetImages(streamCtx.assistantBubble);
    decorateMermaidCodeBlocks(streamCtx.assistantBubble);
    if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
    if (streamCtx.assistantBubble.parentElement) {
      const wrapperDiv = streamCtx.assistantBubble.parentElement;
      if (hasMessageCitations(responseTextToRender, finalMeta)) {
        ensureCitationToggleBtn(wrapperDiv);
      } else {
        const existingToggle = wrapperDiv.querySelector(".citation-toggle-btn");
        if (existingToggle) existingToggle.remove();
      }
      ensureClaimsInspectorBtn(wrapperDiv);
      if (finalMeta.candidates && Array.isArray(finalMeta.candidates) && finalMeta.candidates.length > 0) {
        renderCandidatePapersGroup(wrapperDiv, finalMeta.candidates, streamCtx.streamInvocationId);
      }
      const infoBtn = wrapperDiv.querySelector(".info-btn");
      if (infoBtn) {
        const formattedAuthor = formatAuthorName("assistant", streamCtx.authorName);
        infoBtn.setAttribute("title", `Author: ${formattedAuthor}\nModel: ${finalModel}\nDuration: ${durationSec}s\nCompleted: ${completionTime}`);
        if (infoBtn._metadata) {
          infoBtn._metadata.author = streamCtx.authorName;
          infoBtn._metadata.formattedAuthor = formattedAuthor;
          infoBtn._metadata.model = finalModel;
          infoBtn._metadata.duration = durationSec;
          infoBtn._metadata.timestamp = completionTime;
          infoBtn._metadata.usage = streamCtx.streamUsage || infoBtn._metadata.usage;
          infoBtn._metadata.turn_cost_usd = turnCost;
          infoBtn._metadata.agent_models = turnAgentModels;
          infoBtn._metadata.finish_reason = streamCtx.streamFinishReason || infoBtn._metadata.finish_reason;
          infoBtn._metadata.active_context_items = streamCtx.payloadItems || infoBtn._metadata.active_context_items;
          infoBtn._metadata.event_id = streamCtx.streamEventId || infoBtn._metadata.event_id;
          infoBtn._metadata.intermediate_steps = streamCtx.liveSteps;
          infoBtn._metadata.text = responseTextToRender;
          infoBtn._metadata.citations = streamCtx.streamCitations;
          infoBtn._metadata.references = streamCtx.streamReferences;
          infoBtn._metadata.candidates = streamCtx.streamCandidates;
          infoBtn._metadata.citation_verification = streamCtx.streamCitationVerification;
        }
      }
    }
  }

  // Stream finished — save the assistant response (and intermediate steps) to session history
  if (streamCtx.session && streamCtx.currentResponseText) {
    const lastUserMsg = streamCtx.session.messages.slice().reverse().find(m => m.role === "user" || m.sender === "user");
    if (lastUserMsg && !lastUserMsg.invocation_id && streamCtx.streamInvocationId) {
      lastUserMsg.invocation_id = streamCtx.streamInvocationId;
    }
    const allUserWrappers = chatMessages.querySelectorAll(".message-wrapper.user");
    if (allUserWrappers.length > 0) {
      const lastUserWrapper = allUserWrappers[allUserWrappers.length - 1];
      if (lastUserWrapper && !lastUserWrapper.dataset.invocationId && streamCtx.streamInvocationId) {
        lastUserWrapper.dataset.invocationId = streamCtx.streamInvocationId;
      }
    }

    const existingMsg = streamCtx.session.messages.find(m => m.invocation_id && m.invocation_id === streamCtx.streamInvocationId && (m.role === "assistant" || m.sender === "assistant"));
    if (existingMsg) {
      existingMsg.text = responseTextToRender;
      existingMsg.timestamp = completionTime;
      existingMsg.llm_model = finalModel;
      existingMsg.model = finalModel;
      existingMsg.duration = durationSec;
      existingMsg.usage = streamCtx.streamUsage;
      existingMsg.turn_cost_usd = turnCost;
      existingMsg.agent_models = turnAgentModels;
      existingMsg.finish_reason = streamCtx.streamFinishReason;
      existingMsg.active_context_items = streamCtx.payloadItems;
      existingMsg.event_id = streamCtx.streamEventId;
      existingMsg.intermediate_steps = streamCtx.liveSteps;
      existingMsg.citations = streamCtx.streamCitations;
      existingMsg.references = streamCtx.streamReferences;
      existingMsg.candidates = streamCtx.streamCandidates;
      existingMsg.citation_verification = streamCtx.streamCitationVerification;
    } else {
      streamCtx.session.messages.push({
        role: "assistant",
        text: responseTextToRender,
        isMarkdown: true,
        author: streamCtx.authorName,
        timestamp: completionTime,
        llm_model: finalModel,
        model: finalModel,
        duration: durationSec,
        usage: streamCtx.streamUsage,
        turn_cost_usd: turnCost,
        agent_models: turnAgentModels,
        finish_reason: streamCtx.streamFinishReason,
        active_context_items: streamCtx.payloadItems,
        event_id: streamCtx.streamEventId,
        invocation_id: streamCtx.streamInvocationId,
        intermediate_steps: streamCtx.liveSteps,
        citations: streamCtx.streamCitations,
        references: streamCtx.streamReferences,
        candidates: streamCtx.streamCandidates,
        citation_verification: streamCtx.streamCitationVerification
      });
    }
  }

  // Update session accumulator and footer stats
  const pTokens = Number(streamCtx.streamUsage?.prompt_token_count || streamCtx.streamUsage?.prompt_tokens || 0);
  const cTokens = Number(streamCtx.streamUsage?.candidates_token_count || streamCtx.streamUsage?.candidates_tokens || 0);
  const caTokens = Number(streamCtx.streamUsage?.cached_content_token_count || streamCtx.streamUsage?.cached_tokens || 0);
  const turnTokens = computeTurnTokens(streamCtx.streamUsage);
  currentSessionTurns++;
  currentSessionTotalTokens += turnTokens;
  currentSessionCostUsd += turnCost;
  currentSessionTokenBreakdown.prompt += pTokens;
  currentSessionTokenBreakdown.candidates += cTokens;
  currentSessionTokenBreakdown.cached += caTokens;
  updateSessionStatsUI();

  // Background next-prompt prediction: never blocks stream finalization
  if (!isAborted && streamCtx.streamInvocationId) {
    requestNextPromptSuggestion(currentSessionId, streamCtx.streamInvocationId);
  }

  // Auto-clear activeSnippetsList only upon successful (non-aborted) completion
  if (!isAborted && activeSnippetsList && activeSnippetsList.length > 0) {
    activeSnippetsList = [];
    renderActiveChips();
  }
}

async function runChatStreamReader(res, streamCtx) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const RENDER_INTERVAL_MS = 50;
  let lastRenderTime = 0;

  function scheduleRender() {
    const now = Date.now();
    if (now - lastRenderTime >= RENDER_INTERVAL_MS) {
      lastRenderTime = now;
      if (streamCtx.assistantBubble) {
        streamCtx.assistantBubble._rawText = streamCtx.currentResponseText;
        if (streamCtx.streamCitations) {
          streamCtx.assistantBubble._citations = streamCtx.streamCitations;
        }
        const streamMeta = {
          citations: streamCtx.streamCitations,
          references: streamCtx.streamReferences
        };
        streamCtx.assistantBubble._metadata = streamMeta;
        if (streamCtx.assistantBubble.parentElement) {
          streamCtx.assistantBubble.parentElement._citations = streamCtx.streamCitations;
          streamCtx.assistantBubble.parentElement._metadata = streamMeta;
        }
        streamCtx.assistantBubble.innerHTML = formatMessageContent(streamCtx.currentResponseText, streamMeta);
        fixCodeBlockCopyButtons(streamCtx.assistantBubble);
        decorateAssetImages(streamCtx.assistantBubble);
        if (streamCtx.assistantBubble.parentElement) {
          if (hasMessageCitations(streamCtx.currentResponseText, streamMeta)) {
            ensureCitationToggleBtn(streamCtx.assistantBubble.parentElement);
          }
          ensureClaimsInspectorBtn(streamCtx.assistantBubble.parentElement);
        }
        if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
      }
    }
  }

  const findActiveStep = (steps, type, author, eventId) => {
    if (!steps || steps.length === 0) return null;
    if (eventId) {
      for (let i = steps.length - 1; i >= 0; i--) {
        const s = steps[i];
        if (s.eventId === eventId && s.type === type && (!author || s.author === author)) {
          return s;
        }
      }
    }
    const last = steps[steps.length - 1];
    if (last && last.type === type && (!author || last.author === author)) {
      return last;
    }
    return null;
  };

  const updateInvocationIdAssociation = (invId) => {
    if (!invId || streamCtx.streamInvocationId === invId) return;
    streamCtx.streamInvocationId = invId;
    if (streamCtx.session && Array.isArray(streamCtx.session.messages)) {
      const lastUserMsg = streamCtx.session.messages.slice().reverse().find(m => m.role === "user" || m.sender === "user");
      if (lastUserMsg) {
        lastUserMsg.invocation_id = invId;
      }
    }
    if (chatMessages) {
      const allUserWrappers = chatMessages.querySelectorAll(".message-wrapper.user");
      if (allUserWrappers.length > 0) {
        const lastUserWrapper = allUserWrappers[allUserWrappers.length - 1];
        if (lastUserWrapper) {
          lastUserWrapper.dataset.invocationId = invId;
        }
      }
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const rawJson = line.substring(6).trim();
        if (!rawJson) continue;

        try {
          const data = JSON.parse(rawJson);

          if (data.notice) {
            addNotification(data.notice, "info");
            continue;
          }

          if (data.invocation_id) {
            updateInvocationIdAssociation(data.invocation_id);
          }

          if (data.error) {
            if (streamCtx.stepsBubble && !streamCtx.assistantBubble) {
              streamCtx.stepsBubble.remove();
            }
            const errText = "Error: " + data.error;
            const errInvId = data.invocation_id || streamCtx.streamInvocationId || null;
            const errMeta = {
              isError: true,
              errorMessage: String(data.error),
              invocationId: errInvId,
              invocation_id: errInvId,
            };
            appendMessageDOM("assistant", errText, true, null, null, null, null, errMeta);
            if (streamCtx.session) {
              streamCtx.session.messages.push({
                role: "assistant",
                text: errText,
                isError: true,
                errorMessage: String(data.error),
                invocation_id: errInvId,
                metadata: errMeta,
              });
            }
            continue;
          }

          // Handle HITL request_input event
          if (data.event_type === "request_input") {
            streamCtx.isInterrupted = true;
            if (data.invocation_id) streamCtx.streamInvocationId = data.invocation_id;
            if (data.id) streamCtx.streamEventId = data.id;

            addNotification("⚡ Deep Research Checkpoint: User input needed to extend retrieval.", "info");

            updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, "⚡ Action Required: Deep Research Checkpoint");
            if (streamCtx.stepsBubble && streamCtx.stepsBubble._iconSpan) {
              streamCtx.stepsBubble._iconSpan.className = "intermediate-steps-icon";
              streamCtx.stepsBubble._iconSpan.textContent = "⚡";
            }

            renderHitlCheckpointCard(streamCtx.stepsBubble, data, async (responsePayload) => {
              await resumeDialogueResponse(
                data.invocation_id || streamCtx.streamInvocationId,
                data.interrupt_id,
                responsePayload,
                streamCtx
              );
            });

            isStreaming = false;
            sendBtn.classList.remove("stop-mode");
            sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
            return;
          }

          if (data.author) {
            if (!isInternalWorkflowNode(data.author)) {
              streamCtx.authorName = data.author;
            }
          }
          if (data.model_version) streamCtx.streamModel = data.model_version;
          if (data.usage_metadata) streamCtx.streamUsage = data.usage_metadata;
          if (data.finish_reason) streamCtx.streamFinishReason = data.finish_reason;
          if (data.id) streamCtx.streamEventId = data.id;
          if (data.invocation_id) streamCtx.streamInvocationId = data.invocation_id;
          if (data.citations) streamCtx.streamCitations = data.citations;
          if (data.references) streamCtx.streamReferences = data.references;
          if (data.metadata?.citations) streamCtx.streamCitations = data.metadata.citations;
          if (data.metadata?.references) streamCtx.streamReferences = data.metadata.references;
          if (data.metadata?.candidates) streamCtx.streamCandidates = data.metadata.candidates;
          if (data.candidates) streamCtx.streamCandidates = data.candidates;
          if (data.citation_verification) streamCtx.streamCitationVerification = data.citation_verification;
          if (data.metadata?.citation_verification) streamCtx.streamCitationVerification = data.metadata.citation_verification;
          if (data.enriched_text) streamCtx.streamEnrichedText = data.enriched_text;
          if (data.metadata?.enriched_text) streamCtx.streamEnrichedText = data.metadata.enriched_text;

          if (data.parts && data.parts.length > 0) {
            for (let partIdx = 0; partIdx < data.parts.length; partIdx++) {
              const part = data.parts[partIdx];
              if (part.thought) {
                const thAuthor = formatAuthorName("assistant", data.author);
                const activeThought = findActiveStep(streamCtx.liveSteps, "thought", thAuthor, data.id);
                if (activeThought) {
                  if (data.partial) {
                    activeThought.content += part.thought;
                  } else {
                    activeThought.content = part.thought;
                  }
                  if (data.id && !activeThought.eventId) {
                    activeThought.eventId = data.id;
                  }
                } else {
                  streamCtx.liveSteps.push({
                    type: "thought",
                    author: thAuthor,
                    content: part.thought,
                    eventId: data.id || null,
                    timestamp: Date.now()
                  });
                }
                updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, `${thAuthor} thinking...`);
              }

              if (part.function_call) {
                // Suppress raw adk_request_input tool call so internal JSON schema never leaks
                if (part.function_call.name !== "adk_request_input") {
                  if (!data.partial) {
                    const fc = part.function_call;
                    const fcAuthor = formatAuthorName("assistant", data.author);
                    let existingCall = null;
                    if (fc.id) {
                      for (let i = streamCtx.liveSteps.length - 1; i >= 0; i--) {
                        const s = streamCtx.liveSteps[i];
                        if (s.type === "tool_call" && s.callId === fc.id) {
                          existingCall = s;
                          break;
                        }
                      }
                    } else if (data.id) {
                      for (let i = streamCtx.liveSteps.length - 1; i >= 0; i--) {
                        const s = streamCtx.liveSteps[i];
                        if (s.eventId === data.id && s.partIndex === partIdx && s.type === "tool_call" && s.name === fc.name) {
                          existingCall = s;
                          break;
                        }
                      }
                    }
                    if (!existingCall) {
                      for (let i = streamCtx.liveSteps.length - 1; i >= 0; i--) {
                        const s = streamCtx.liveSteps[i];
                        if (s.type === "tool_return") break;
                        if (s.type === "tool_call" && s.name === fc.name && JSON.stringify(s.args || {}) === JSON.stringify(fc.args || {})) {
                          existingCall = s;
                          break;
                        }
                      }
                    }

                    if (existingCall) {
                      if (fc.args && (!existingCall.args || Object.keys(existingCall.args).length === 0)) {
                        existingCall.args = fc.args;
                      }
                      if (data.id && !existingCall.eventId) {
                        existingCall.eventId = data.id;
                      }
                      if (fc.id && !existingCall.callId) {
                        existingCall.callId = fc.id;
                      }
                    } else {
                      streamCtx.liveSteps.push({
                        type: "tool_call",
                        author: fcAuthor,
                        name: fc.name,
                        args: fc.args,
                        callId: fc.id || null,
                        eventId: data.id || null,
                        partIndex: partIdx,
                        timestamp: Date.now()
                      });
                    }
                    updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, `Calling ${fc.name}...`);
                  } else if (part.function_call.name) {
                    updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, `Calling ${part.function_call.name}...`);
                  }
                }
              }

              if (part.function_response) {
                if (part.function_response.name !== "adk_request_input") {
                  const fr = part.function_response;
                  let existingReturn = null;
                  if (fr.id) {
                    for (let i = streamCtx.liveSteps.length - 1; i >= 0; i--) {
                      const s = streamCtx.liveSteps[i];
                      if (s.type === "tool_return" && s.callId === fr.id) {
                        existingReturn = s;
                        break;
                      }
                    }
                  } else if (data.id) {
                    for (let i = streamCtx.liveSteps.length - 1; i >= 0; i--) {
                      const s = streamCtx.liveSteps[i];
                      if (s.eventId === data.id && s.partIndex === partIdx && s.type === "tool_return" && s.name === fr.name) {
                        existingReturn = s;
                        break;
                      }
                    }
                  }
                  if (!existingReturn) {
                    streamCtx.liveSteps.push({
                      type: "tool_return",
                      name: fr.name,
                      response: fr.response,
                      callId: fr.id || null,
                      eventId: data.id || null,
                      partIndex: partIdx,
                      timestamp: Date.now()
                    });
                  }
                  updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, `Received response from ${fr.name}`);
                }
              }

              if (part.text) {
                const rawAuthor = (data.author || "").toLowerCase();
                if (isIntermediateSearchStep(rawAuthor)) {
                  const txtAuthor = formatAuthorName("assistant", data.author);
                  const activeText = findActiveStep(streamCtx.liveSteps, "agent_text", txtAuthor, data.id);
                  if (activeText) {
                    if (data.partial) {
                      activeText.text += part.text;
                    } else {
                      activeText.text = part.text;
                    }
                    if (data.id && !activeText.eventId) {
                      activeText.eventId = data.id;
                    }
                  } else {
                    streamCtx.liveSteps.push({
                      type: "agent_text",
                      author: txtAuthor,
                      text: part.text,
                      eventId: data.id || null,
                      timestamp: Date.now()
                    });
                  }
                  updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, part.text.substring(0, 100) + "...");
                } else {
                  if (data.partial) {
                    if (data.id) streamCtx.streamedEventIds.add(data.id);
                    streamCtx.currentResponseText += part.text;
                  } else {
                    if (data.id && streamCtx.streamedEventIds.has(data.id)) {
                      streamCtx.currentResponseText = part.text;
                      streamCtx.streamedEventIds.delete(data.id);
                    } else {
                      streamCtx.currentResponseText += part.text;
                    }
                  }
                  if (data.author) {
                    if (!isInternalWorkflowNode(data.author)) {
                      streamCtx.authorName = data.author;
                    }
                  }
                  if (!streamCtx.assistantBubble) {
                    const initialModel = streamCtx.streamModel || streamCtx.usedModel;
                    streamCtx.assistantBubble = appendMessageDOM("assistant", streamCtx.currentResponseText, true, null, streamCtx.authorName, initialModel, null, {
                      author: streamCtx.authorName,
                      model: initialModel,
                      active_context_items: streamCtx.payloadItems,
                      usage: streamCtx.streamUsage,
                      finish_reason: streamCtx.streamFinishReason,
                      event_id: streamCtx.streamEventId,
                      intermediate_steps: streamCtx.liveSteps
                    });
                    if (streamCtx.stepsBubble && streamCtx.assistantBubble && streamCtx.assistantBubble.parentElement) {
                      streamCtx.assistantBubble.parentElement.insertBefore(streamCtx.stepsBubble, streamCtx.assistantBubble);
                    }
                  } else {
                    scheduleRender();
                  }
                }
              }
            }
          }
        } catch (e) {
          console.error("SSE parse error:", e);
        }
      }
    }
  }

  // Handle final buffer
  if (buffer.trim()) {
    const remainingLines = buffer.split("\n");
    for (const line of remainingLines) {
      if (line.startsWith("data: ")) {
        const rawJson = line.substring(6).trim();
        if (!rawJson) continue;
        try {
          const data = JSON.parse(rawJson);
          if (data.notice) {
            addNotification(data.notice, "info");
            continue;
          }
          if (data.invocation_id) {
            updateInvocationIdAssociation(data.invocation_id);
          }
          if (data.id) streamCtx.streamEventId = data.id;
          if (data.citations) streamCtx.streamCitations = data.citations;
          if (data.references) streamCtx.streamReferences = data.references;
          if (data.metadata?.citations) streamCtx.streamCitations = data.metadata.citations;
          if (data.metadata?.references) streamCtx.streamReferences = data.metadata.references;
          if (data.metadata?.candidates) streamCtx.streamCandidates = data.metadata.candidates;
          if (data.candidates) streamCtx.streamCandidates = data.candidates;
          if (data.citation_verification) streamCtx.streamCitationVerification = data.citation_verification;
          if (data.metadata?.citation_verification) streamCtx.streamCitationVerification = data.metadata.citation_verification;
          if (data.enriched_text) streamCtx.streamEnrichedText = data.enriched_text;
          if (data.metadata?.enriched_text) streamCtx.streamEnrichedText = data.metadata.enriched_text;
          if (data.error) {
            if (streamCtx.stepsBubble && !streamCtx.assistantBubble) {
              streamCtx.stepsBubble.remove();
            }
            const errText = "Error: " + data.error;
            const errInvId = data.invocation_id || streamCtx.streamInvocationId || null;
            const errMeta = {
              isError: true,
              errorMessage: String(data.error),
              invocationId: errInvId,
              invocation_id: errInvId,
            };
            appendMessageDOM("assistant", errText, true, null, null, null, null, errMeta);
            if (streamCtx.session) {
              streamCtx.session.messages.push({
                role: "assistant",
                text: errText,
                isError: true,
                errorMessage: String(data.error),
                invocation_id: errInvId,
                metadata: errMeta,
              });
            }
            continue;
          }
          if (data.event_type === "request_input") {
            streamCtx.isInterrupted = true;
            updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, "⚡ Action Required: Deep Research Checkpoint");
            if (streamCtx.stepsBubble && streamCtx.stepsBubble._iconSpan) {
              streamCtx.stepsBubble._iconSpan.className = "intermediate-steps-icon";
              streamCtx.stepsBubble._iconSpan.textContent = "⚡";
            }
            renderHitlCheckpointCard(streamCtx.stepsBubble, data, async (responsePayload) => {
              await resumeDialogueResponse(
                data.invocation_id || streamCtx.streamInvocationId,
                data.interrupt_id,
                responsePayload,
                streamCtx
              );
            });
            isStreaming = false;
            sendBtn.classList.remove("stop-mode");
            sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
            return;
          }
          if (data.parts && data.parts.length > 0) {
            for (const part of data.parts) {
              if (part.text) {
                const rawAuthor = (data.author || "").toLowerCase();
                if (!isIntermediateSearchStep(rawAuthor)) {
                  if (data.partial) {
                    if (data.id) streamCtx.streamedEventIds.add(data.id);
                    streamCtx.currentResponseText += part.text;
                  } else {
                    if (data.id && streamCtx.streamedEventIds.has(data.id)) {
                      streamCtx.currentResponseText = part.text;
                      streamCtx.streamedEventIds.delete(data.id);
                    } else {
                      streamCtx.currentResponseText += part.text;
                    }
                  }
                }
              }
            }
          }
        } catch (e) {
          console.error("SSE parse error (final buffer):", e);
        }
      }
    }
  }

  // If we reached here without an interrupt, finalize!
  if (!streamCtx.isInterrupted) {
    finalizeChatStream(streamCtx);
  }
}

async function resumeDialogueResponse(invocationId, interruptId, responsePayload, streamCtx) {
  if (!currentSessionId || isStreaming) return;

  activeAbortController = new AbortController();
  isStreaming = true;
  streamCtx.isInterrupted = false;
  sendBtn.classList.add("stop-mode");
  sendBtn.title = "Stop generation";

  const actionText = responsePayload.action === "extend_budget"
    ? "Extending deep research capacity..."
    : "Synthesizing response with existing evidence...";

  updateIntermediateStepsBubbleProgress(streamCtx.stepsBubble, streamCtx.liveSteps, actionText);
  if (streamCtx.stepsBubble && streamCtx.stepsBubble._iconSpan) {
    streamCtx.stepsBubble._iconSpan.className = "intermediate-steps-icon intermediate-steps-spinner";
    streamCtx.stepsBubble._iconSpan.textContent = "⚙️";
  }

  try {
    const res = await fetch(`${BACKEND_URL}/chat/resume`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        session_id: currentSessionId,
        invocation_id: invocationId,
        interrupt_id: interruptId,
        response: responsePayload
      }),
      signal: activeAbortController.signal
    });

    if (!res.ok) {
      let errDetail = res.statusText;
      try {
        const errJson = await res.json();
        if (errJson && errJson.detail) {
          errDetail = (typeof errJson.detail === "object" && errJson.detail !== null)
            ? JSON.stringify(errJson.detail)
            : String(errJson.detail);
        }
      } catch (e) {}
      throw new Error(errDetail);
    }

    streamCtx.isInterrupted = false;
    await runChatStreamReader(res, streamCtx);
  } catch (err) {
    streamCtx.isInterrupted = false;
    if (err.name === "AbortError") {
      finalizeChatStream(streamCtx, true);
    } else {
      console.error("Resume stream error:", err);
      addNotification("Resume research failed: " + (err.message || err), "error");
      const errTime = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
      const errText = "Resume Error: " + err.message;
      const errInvId = invocationId || streamCtx.streamInvocationId || null;
      const errMeta = {
        isError: true,
        errorMessage: errText,
        invocationId: errInvId,
        invocation_id: errInvId,
      };
      appendMessageDOM("assistant", errText, false, errTime, "System", null, null, errMeta);
      if (streamCtx.session) {
        streamCtx.session.messages.push({
          role: "assistant",
          text: errText,
          timestamp: errTime,
          author: "System",
          isError: true,
          errorMessage: errText,
          invocation_id: errInvId,
          metadata: errMeta,
        });
      }
    }
  } finally {
    if (!streamCtx.isInterrupted) {
      isStreaming = false;
      activeAbortController = null;
      sendBtn.classList.remove("stop-mode");
      sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
    }
  }
}

async function streamDialogueResponse(prompt) {
  if (!prompt || isStreaming) return;

  const session = sessions.find(s => s.id === currentSessionId);

  // Single thin intermediate steps bubble (Option A)
  const stepsBubble = appendIntermediateStepsBubbleDOM([], null, true);
  const liveSteps = [];
  if (chatMessages) {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  // Prepare payload mapping activeItemsList
  const payloadItems = activeItemsList.map(item => ({
    key: item.key || item.item_key || item.itemKey,
    title: item.title,
    type: item.type || "item"
  }));

  const payloadSnippets = (activeSnippetsList || []).map(s => ({
    id: s.id,
    type: s.type,
    item_key: s.item_key || s.itemKey,
    itemKey: s.itemKey || s.item_key,
    parent_key: s.parent_key || s.parentKey,
    parentKey: s.parentKey || s.parent_key,
    paper_title: s.paper_title || s.paperTitle,
    paperTitle: s.paperTitle || s.paper_title,
    author_str: s.author_str || s.authorStr,
    authorStr: s.authorStr || s.author_str,
    authors: s.authors,
    page: s.page,
    text: s.text,
    image_data: s.image_data || s.imageData,
    imageData: s.imageData || s.image_data,
    mime_type: s.mime_type || "image/jpeg"
  }));

  // Set up AbortController for Stop button (H9)
  activeAbortController = new AbortController();
  isStreaming = true;

  // Transform send button to stop button
  sendBtn.classList.add("stop-mode");
  sendBtn.title = "Stop generation";

  const requestStartTime = Date.now();
  let usedModel = currentLlmModel || (modelNameDisplay ? modelNameDisplay.textContent : "") || "google/gemini-3.8-flash";

  const streamCtx = {
    assistantBubble: null,
    currentResponseText: "",
    streamedEventIds: new Set(),
    authorName: "Dialogue Agent",
    streamModel: null,
    streamUsage: null,
    streamFinishReason: null,
    streamEventId: null,
    streamInvocationId: null,
    stepsBubble: stepsBubble,
    liveSteps: liveSteps,
    payloadItems: payloadItems,
    payloadSnippets: payloadSnippets,
    requestStartTime: requestStartTime,
    usedModel: usedModel,
    session: session,
    isInterrupted: false,
    streamCitations: null,
    streamReferences: null,
    streamCandidates: null,
    streamCitationVerification: null,
    streamEnrichedText: null
  };

  try {
    const res = await fetch(`${BACKEND_URL}/chat`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        prompt: prompt,
        session_id: currentSessionId,
        active_items: payloadItems,
        snippets: payloadSnippets.length > 0 ? payloadSnippets : undefined
      }),
      signal: activeAbortController.signal
    });

    if (!res.ok) {
      let errDetail = res.statusText;
      try {
        const errJson = await res.json();
        if (errJson && errJson.detail) {
          errDetail = (typeof errJson.detail === "object" && errJson.detail !== null)
            ? JSON.stringify(errJson.detail)
            : String(errJson.detail);
        }
      } catch (e) {}
      if (res.status === 409) {
        throw new Error(errDetail || "Session has a pending research extension request. Please respond to the checkpoint above before starting a new request.");
      }
      throw new Error("Chat service failed: " + errDetail);
    }

    await runChatStreamReader(res, streamCtx);
  } catch (err) {
    if (err.name === "AbortError") {
      finalizeChatStream(streamCtx, true);
    } else {
      if (stepsBubble && !streamCtx.assistantBubble) stepsBubble.remove();
      const errTime = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
      const errText = "System Error: " + err.message;
      const errInvId = streamCtx.streamInvocationId || null;
      const errMeta = {
        isError: true,
        errorMessage: errText,
        invocationId: errInvId,
        invocation_id: errInvId,
      };
      appendMessageDOM("assistant", errText, false, errTime, "System", null, null, errMeta);
      if (session) {
        session.messages.push({
          role: "assistant",
          text: errText,
          timestamp: errTime,
          author: "System",
          isError: true,
          errorMessage: errText,
          invocation_id: errInvId,
          metadata: errMeta,
        });
      }
      const errMsg = (err.message || String(err)).toLowerCase();
      if (errMsg.includes("401") || errMsg.includes("403") || errMsg.includes("unauthorized") || errMsg.includes("api key")) {
        addNotification("API authentication failed. Please verify your API key in Settings.", "error");
      } else if (errMsg.includes("429") || errMsg.includes("rate limit")) {
        addNotification("Rate limit exceeded. Please wait or select a different model.", "error");
      } else {
        addNotification("Chat error: " + (err.message || err), "error");
      }
    }
  } finally {
    if (!streamCtx.isInterrupted) {
      isStreaming = false;
      activeAbortController = null;
      sendBtn.classList.remove("stop-mode");
      sendBtn.title = "Send Message (Ctrl/Cmd + Enter)";
    }
  }
}

sendBtn.addEventListener("click", () => {
  if (isStreaming && activeAbortController) {
    // Stop mode: abort the active stream
    clearGhostSuggestion();
    activeAbortController.abort();
  } else {
    handleChatSubmit();
  }
});
chatInput.addEventListener("keydown", (e) => {
  // Accept ghost suggestion on Tab (Shift+Tab preserved) or Right-Arrow at caret start
  const isTabAccept = e.key === "Tab" && !e.shiftKey;
  const isArrowAccept = e.key === "ArrowRight" && chatInput.selectionStart === 0 && chatInput.selectionEnd === 0;
  if ((isTabAccept || isArrowAccept) && currentSuggestedPrompt && !chatInput.value.trim()) {
    e.preventDefault();
    chatInput.value = currentSuggestedPrompt;
    clearGhostSuggestion();
    chatInput.dispatchEvent(new Event("input"));
    chatInput.focus();
    return;
  }

  // Dismiss on Escape (no preventDefault so other Escape handlers still run)
  if (e.key === "Escape" && currentSuggestedPrompt) {
    clearGhostSuggestion();
    return;
  }

  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    handleChatSubmit();
  }
});

chatInput.addEventListener("input", function() {
  updateGhostVisibility();
  const selectionStart = this.selectionStart;
  const selectionEnd = this.selectionEnd;

  this.style.height = "46px";
  const newHeight = this.scrollHeight;

  if (newHeight > 46) {
    this.style.height = newHeight + "px";
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  this.setSelectionRange(selectionStart, selectionEnd);
});

// Add context menu: toggles context options popup menu
addContextBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const isHidden = contextOptionsDropdown.classList.contains("hidden");
  contextOptionsDropdown.classList.toggle("hidden");
  
  // Reset keyboard focus state
  clearMenuFocus();
  focusedMenuType = "main";
  focusedMenuIndex = -1;

  if (isHidden) {
    updateContextDropdownUI();
  }
});

// Theme management
let currentThemeSetting = "auto"; // "auto", "slate", "amoled"

function applyTheme() {
  let appliedTheme = "slate"; // default fallback is our Slate Dark
  
  if (currentThemeSetting === "auto") {
    let zoteroTheme = "system";
    try {
      const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
      if (zot && zot.Prefs) {
        zoteroTheme = zot.Prefs.get("extensions.zotero.theme") || "system";
      }
    } catch (e) {
      console.warn("Zocrates: Failed to read theme pref:", e);
    }
    
    if (zoteroTheme === "system") {
      let prefersDark = false;
      try {
        if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
          prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        }
      } catch (e) {}
      appliedTheme = prefersDark ? "zotero-dark" : "zotero-light";
    } else if (zoteroTheme === "dark") {
      appliedTheme = "zotero-dark";
    } else {
      appliedTheme = "zotero-light";
    }
  } else if (currentThemeSetting === "slate") {
    appliedTheme = "slate";
  } else if (currentThemeSetting === "amoled") {
    appliedTheme = "amoled";
  } else if (currentThemeSetting === "sepia") {
    appliedTheme = "sepia";
  }
  
  document.body.setAttribute("data-theme", appliedTheme);


  const themeOptionBtns = document.querySelectorAll(".theme-option-btn");
  themeOptionBtns.forEach(btn => {
    btn.setAttribute("data-selected", btn.getAttribute("data-theme") === currentThemeSetting ? "true" : "false");
  });

  updateMermaidThemes();
}

function loadSavedTheme() {
  let changed = false;
  try {
    let savedTheme = null;
    const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zot && zot.Prefs) {
      savedTheme = zot.Prefs.get("zocrates.theme");
    }
    if (!savedTheme) {
      savedTheme = localStorage.getItem("zocrates_theme");
    }
    if (savedTheme && ["auto", "slate", "amoled", "sepia"].includes(savedTheme)) {
      if (savedTheme !== currentThemeSetting) {
        currentThemeSetting = savedTheme;
        changed = true;
      }
    }
  } catch (e) {}
  if (changed || !document.body || !document.body.getAttribute("data-theme")) {
    applyTheme();
  }
}

// Font size management
let currentFontSizeSetting = "default"; // "small", "default", "large", "xlarge"

function applyFontSize() {
  const allowedSizes = ["small", "default", "large", "xlarge"];
  if (!allowedSizes.includes(currentFontSizeSetting)) {
    currentFontSizeSetting = "default";
  }

  if (document.body) {
    document.body.setAttribute("data-font-size", currentFontSizeSetting);
  }

  const fontSizeOptionBtns = document.querySelectorAll(".font-size-option-btn");
  fontSizeOptionBtns.forEach(btn => {
    btn.setAttribute("data-selected", btn.getAttribute("data-font-size") === currentFontSizeSetting ? "true" : "false");
  });

  if (chatInput) {
    chatInput.dispatchEvent(new Event("input"));
  }
}

function loadSavedFontSize() {
  let changed = false;
  try {
    let savedSize = null;
    const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zot && zot.Prefs) {
      savedSize = zot.Prefs.get("zocrates.fontSize") || zot.Prefs.get("extensions.zotero.zocrates.fontSize");
    }
    if (!savedSize) {
      savedSize = localStorage.getItem("zocrates_font_size");
    }
    if (savedSize && ["small", "default", "large", "xlarge"].includes(savedSize)) {
      if (savedSize !== currentFontSizeSetting) {
        currentFontSizeSetting = savedSize;
        changed = true;
      }
    }
  } catch (e) {}
  if (changed || !document.body || !document.body.getAttribute("data-font-size")) {
    applyFontSize();
  }
}

// H5: Capture observer ID for cleanup on unload
let themePrefsObserverId = null;
let zocratesThemeObserverId = null;
let zocratesFontSizeObserverId = null;
let llmPrefsObserverId = null;
let llmTimestampObserverId = null;
let autoRenderMermaidObserverId = null;

function formatModelLabel(modelName, provider = null) {
  if (!modelName) return "Connecting...";

  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  let p = provider || (zot && zot.Prefs ? zot.Prefs.get("zocrates.llm_provider") : null);

  let rawModel = modelName;
  if (rawModel.startsWith("openrouter/")) {
    rawModel = rawModel.substring("openrouter/".length);
    if (!p) p = "openrouter_llm";
  } else if (rawModel.startsWith("ollama_chat/")) {
    rawModel = rawModel.substring("ollama_chat/".length);
    if (!p) p = "ollama";
  } else if (rawModel.startsWith("ollama/")) {
    rawModel = rawModel.substring("ollama/".length);
    if (!p) p = "ollama";
  } else if (rawModel.startsWith("openai/")) {
    rawModel = rawModel.substring("openai/".length);
  }

  let prefix = "";
  if (p === "openrouter_llm" || modelName.startsWith("openrouter/")) {
    prefix = "OpenRouter: ";
  } else if (p === "ollama" || modelName.startsWith("ollama/") || modelName.startsWith("ollama_chat/")) {
    prefix = "Ollama: ";
  } else if (p === "vllm") {
    prefix = "vLLM: ";
  } else if (p === "openai_compatible") {
    prefix = "Custom OpenAI: ";
  }

  return `${prefix}${rawModel}`;
}

function updateDisplayedModelName(modelName, provider = null) {
  if (!modelName || !modelNameDisplay) return;
  currentLlmModel = modelName;
  const label = formatModelLabel(modelName, provider);
  modelNameDisplay.textContent = label;
  modelNameDisplay.title = label;
  if (modelNameDisplay.parentElement) {
    modelNameDisplay.parentElement.title = label;
  }
}

try {
  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  if (zot && zot.Prefs) {
    themePrefsObserverId = zot.Prefs.registerObserver("extensions.zotero.theme", () => {
      if (currentThemeSetting === "auto") {
        applyTheme();
      }
    });
    zocratesThemeObserverId = zot.Prefs.registerObserver("zocrates.theme", () => {
      loadSavedTheme();
    });
    zocratesFontSizeObserverId = zot.Prefs.registerObserver("zocrates.fontSize", () => {
      loadSavedFontSize();
    });
    llmPrefsObserverId = zot.Prefs.registerObserver("zocrates.llm_model", () => {
      const savedModel = zot.Prefs.get("zocrates.llm_model");
      if (savedModel) updateDisplayedModelName(savedModel);
      checkBackendStatus();
    });
    llmTimestampObserverId = zot.Prefs.registerObserver("zocrates.llm_config_timestamp", () => {
      const savedModel = zot.Prefs.get("zocrates.llm_model");
      if (savedModel) updateDisplayedModelName(savedModel);
      checkBackendStatus();
    });
    autoRenderMermaidObserverId = zot.Prefs.registerObserver("extensions.zocrates.autoRenderMermaid", () => {
      const pref = zot.Prefs.get("extensions.zocrates.autoRenderMermaid", true);
      if (pref !== undefined && pref !== null) {
        _autoRenderMermaid = Boolean(pref);
      }
    });
  }

  const observerObj = {
    observe: function(subject, topic, data) {
      if (data) {
        updateDisplayedModelName(data);
      } else {
        const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
        if (zot && zot.Prefs) {
          const m = zot.Prefs.get("zocrates.llm_model");
          if (m) updateDisplayedModelName(m);
        }
      }
      checkBackendStatus();
    }
  };

  if (typeof Services !== "undefined" && Services.obs) {
    Services.obs.addObserver(observerObj, "zocrates-llm-config-updated", false);
  }
  const win = (window.parent && window.parent.Services) ? window.parent : (window.top && window.top.Services ? window.top : window);
  if (win && win.Services && win.Services.obs) {
    win.Services.obs.addObserver(observerObj, "zocrates-llm-config-updated", false);
  }
} catch (e) {
  console.warn("Zocrates: Failed to register observers:", e);
}

// Watch for system prefers-color-scheme changes
if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
  try {
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    if (mediaQuery && typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", () => {
        if (currentThemeSetting === "auto") {
          applyTheme();
        }
      });
    }
  } catch (e) {}
}

// Flyout submenu helpers for More Actions dropdown
function positionFlyoutSubmenu(flyoutDropdown, container) {
  if (!flyoutDropdown) return;
  flyoutDropdown.style.left = "";
  flyoutDropdown.style.right = "100%";
  const rect = flyoutDropdown.getBoundingClientRect();
  if (rect.left < 4 && container) {
    const containerRect = container.getBoundingClientRect();
    flyoutDropdown.style.right = "auto";
    flyoutDropdown.style.left = (4 - containerRect.left) + "px";
  }
}

function positionThemeSubmenu() {
  positionFlyoutSubmenu(themeOptionsDropdown, themeMenuContainer);
}

function positionFontSizeSubmenu() {
  positionFlyoutSubmenu(fontSizeOptionsDropdown, fontSizeMenuContainer);
}

function positionAboutSubmenu() {
  positionFlyoutSubmenu(aboutOptionsDropdown, aboutMenuContainer);
}

let hideThemeSubmenuTimeout = null;
let hideFontSizeSubmenuTimeout = null;
let hideAboutSubmenuTimeout = null;

function clearFlyoutTimeouts() {
  if (hideThemeSubmenuTimeout) {
    clearTimeout(hideThemeSubmenuTimeout);
    hideThemeSubmenuTimeout = null;
  }
  if (hideFontSizeSubmenuTimeout) {
    clearTimeout(hideFontSizeSubmenuTimeout);
    hideFontSizeSubmenuTimeout = null;
  }
  if (hideAboutSubmenuTimeout) {
    clearTimeout(hideAboutSubmenuTimeout);
    hideAboutSubmenuTimeout = null;
  }
}

function hideAllFlyoutSubmenus() {
  clearFlyoutTimeouts();
  if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
  if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
  if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
}

if (themeMenuOption) {
  themeMenuOption.addEventListener("mouseenter", () => {
    clearFlyoutTimeouts();
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) {
      themeOptionsDropdown.classList.remove("hidden");
      positionThemeSubmenu();
    }
  });

  themeMenuOption.addEventListener("click", (e) => {
    e.stopPropagation();
    clearFlyoutTimeouts();
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) {
      const isHidden = themeOptionsDropdown.classList.toggle("hidden");
      if (!isHidden) {
        positionThemeSubmenu();
      }
    }
  });
}

if (themeMenuContainer) {
  themeMenuContainer.addEventListener("mouseleave", () => {
    if (hideThemeSubmenuTimeout) clearTimeout(hideThemeSubmenuTimeout);
    hideThemeSubmenuTimeout = setTimeout(() => {
      if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    }, 400); // 400ms buffer to cross the gap
  });

  themeMenuContainer.addEventListener("mouseenter", () => {
    if (hideThemeSubmenuTimeout) {
      clearTimeout(hideThemeSubmenuTimeout);
      hideThemeSubmenuTimeout = null;
    }
  });
}

if (themeOptionsDropdown) {
  themeOptionsDropdown.addEventListener("mouseenter", () => {
    if (hideThemeSubmenuTimeout) {
      clearTimeout(hideThemeSubmenuTimeout);
      hideThemeSubmenuTimeout = null;
    }
  });
}

if (aboutMenuOption) {
  aboutMenuOption.addEventListener("mouseenter", () => {
    clearFlyoutTimeouts();
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) {
      aboutOptionsDropdown.classList.remove("hidden");
      positionAboutSubmenu();
    }
  });

  aboutMenuOption.addEventListener("click", (e) => {
    e.stopPropagation();
    clearFlyoutTimeouts();
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) {
      const isHidden = aboutOptionsDropdown.classList.toggle("hidden");
      if (!isHidden) {
        positionAboutSubmenu();
      }
    }
  });
}

if (aboutMenuContainer) {
  aboutMenuContainer.addEventListener("mouseleave", () => {
    if (hideAboutSubmenuTimeout) clearTimeout(hideAboutSubmenuTimeout);
    hideAboutSubmenuTimeout = setTimeout(() => {
      if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    }, 400);
  });

  aboutMenuContainer.addEventListener("mouseenter", () => {
    if (hideAboutSubmenuTimeout) {
      clearTimeout(hideAboutSubmenuTimeout);
      hideAboutSubmenuTimeout = null;
    }
  });
}

if (aboutOptionsDropdown) {
  aboutOptionsDropdown.addEventListener("mouseenter", () => {
    if (hideAboutSubmenuTimeout) {
      clearTimeout(hideAboutSubmenuTimeout);
      hideAboutSubmenuTimeout = null;
    }
  });
}

const moreActionsSiblingBtns = [
  document.getElementById("network-graph-menu-btn"),
  document.getElementById("library-conversion-status-menu-btn"),
  syncBtn,
  settingsBtn
];
moreActionsSiblingBtns.forEach(btn => {
  if (btn) {
    btn.addEventListener("mouseenter", () => {
      hideAllFlyoutSubmenus();
    });
  }
});

if (fontSizeMenuOption) {
  fontSizeMenuOption.addEventListener("mouseenter", () => {
    clearFlyoutTimeouts();
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) {
      fontSizeOptionsDropdown.classList.remove("hidden");
      positionFontSizeSubmenu();
    }
  });

  fontSizeMenuOption.addEventListener("click", (e) => {
    e.stopPropagation();
    clearFlyoutTimeouts();
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) {
      const isHidden = fontSizeOptionsDropdown.classList.toggle("hidden");
      if (!isHidden) {
        positionFontSizeSubmenu();
      }
    }
  });
}

if (fontSizeMenuContainer) {
  fontSizeMenuContainer.addEventListener("mouseleave", () => {
    if (hideFontSizeSubmenuTimeout) clearTimeout(hideFontSizeSubmenuTimeout);
    hideFontSizeSubmenuTimeout = setTimeout(() => {
      if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    }, 400);
  });

  fontSizeMenuContainer.addEventListener("mouseenter", () => {
    if (hideFontSizeSubmenuTimeout) {
      clearTimeout(hideFontSizeSubmenuTimeout);
      hideFontSizeSubmenuTimeout = null;
    }
  });
}

if (fontSizeOptionsDropdown) {
  fontSizeOptionsDropdown.addEventListener("mouseenter", () => {
    if (hideFontSizeSubmenuTimeout) {
      clearTimeout(hideFontSizeSubmenuTimeout);
      hideFontSizeSubmenuTimeout = null;
    }
  });
}

const themeOptionBtns = document.querySelectorAll(".theme-option-btn");
themeOptionBtns.forEach(btn => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const newThemeSetting = btn.getAttribute("data-theme");
    if (newThemeSetting) {
      currentThemeSetting = newThemeSetting;
      try {
        localStorage.setItem("zocrates_theme", currentThemeSetting);
      } catch (err) {}
      try {
        const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
        if (zot && zot.Prefs) {
          zot.Prefs.set("zocrates.theme", currentThemeSetting);
        }
      } catch (err) {}
      applyTheme();
    }
    hideAllFlyoutSubmenus();
    if (moreActionsDropdown) {
      moreActionsDropdown.classList.add("hidden");
    }
  });
});

const fontSizeOptionBtns = document.querySelectorAll(".font-size-option-btn");
fontSizeOptionBtns.forEach(btn => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const newFontSize = btn.getAttribute("data-font-size");
    if (newFontSize) {
      currentFontSizeSetting = newFontSize;
      try {
        localStorage.setItem("zocrates_font_size", currentFontSizeSetting);
      } catch (err) {}
      try {
        const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
        if (zot && zot.Prefs) {
          zot.Prefs.set("zocrates.fontSize", currentFontSizeSetting);
        }
      } catch (err) {}
      applyFontSize();
    }
    hideAllFlyoutSubmenus();
    if (moreActionsDropdown) {
      moreActionsDropdown.classList.add("hidden");
    }
  });
});

// ---------------------------------------------------------------------------
// About Submenu Action Handlers
// ---------------------------------------------------------------------------
function getInfoModal() {
  if (typeof InfoModal !== "undefined" && InfoModal) return InfoModal;
  const zot = resolveZotero();
  if (zot && zot.Zocrates && zot.Zocrates.InfoModal) return zot.Zocrates.InfoModal;
  if (window.InfoModal) return window.InfoModal;
  if (window.parent && window.parent.Zotero && window.parent.Zotero.Zocrates && window.parent.Zotero.Zocrates.InfoModal) {
    return window.parent.Zotero.Zocrates.InfoModal;
  }
  if (window.top && window.top.Zotero && window.top.Zotero.Zocrates && window.top.Zotero.Zocrates.InfoModal) {
    return window.top.Zotero.Zocrates.InfoModal;
  }
  return null;
}

if (aboutHardwareBtn) {
  aboutHardwareBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    hideAllFlyoutSubmenus();

    const modal = getInfoModal();
    if (!modal) {
      console.warn("Zocrates: InfoModal not available");
      return;
    }

    try {
      const res = await fetch(`${BACKEND_URL}/status`, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const hw = data.hardware || {};

      const rows = [
        { label: "System RAM", value: hw.ram_gb ? `${hw.ram_gb} GB` : (hw.ram || "Unknown") },
        { label: "GPU Vendor", value: hw.gpu_vendor || "None" },
        { label: "GPU Name", value: hw.gpu_name || "None" },
        { label: "VRAM", value: hw.vram_gb ? `${hw.vram_gb} GB` : (hw.vram || "N/A") }
      ];

      if (hw.docling_assessment) {
        let assessmentVal = "";
        if (typeof hw.docling_assessment === "string") {
          assessmentVal = hw.docling_assessment;
        } else if (hw.docling_assessment && hw.docling_assessment.reason) {
          assessmentVal = hw.docling_assessment.reason;
        } else {
          assessmentVal = JSON.stringify(hw.docling_assessment);
        }
        rows.push({ label: "Docling Assessment", value: assessmentVal });
      }

      modal.show({
        window,
        title: "Detected Hardware",
        subtitle: "Host system hardware capabilities detected by local backend.",
        rows
      });
    } catch (err) {
      modal.show({
        window,
        title: "Detected Hardware",
        subtitle: "Host system hardware capabilities detected by local backend.",
        rows: [
          { label: "Status", value: "Offline / Unable to connect to backend", valueStyle: "color: #ef4444;" },
          { label: "Backend URL", value: BACKEND_URL },
          { label: "Error", value: err.message || String(err), valueStyle: "color: #ef4444;" }
        ]
      });
    }
  });
}

if (aboutAdvancedSettingsBtn) {
  aboutAdvancedSettingsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    hideAllFlyoutSubmenus();

    const modal = getInfoModal();
    if (!modal) {
      console.warn("Zocrates: InfoModal not available");
      return;
    }

    const zot = resolveZotero();
    const dataDir = (zot && zot.DataDirectory && zot.DataDirectory.dir) ? zot.DataDirectory.dir : "<Zotero Data Directory>";
    const configPath = `${dataDir}/zocrates-data/zocrates_config.json`;

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "margin-top: 6px; display: flex; justify-content: flex-start;";
    const revealBtn = document.createElement("button");
    revealBtn.textContent = "Reveal in File Manager";
    revealBtn.style.cssText = "padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 5px; border: 1px solid var(--zotero-accent-color, #6366f1); background: var(--zotero-accent-color, #6366f1); color: #ffffff;";
    revealBtn.addEventListener("click", () => {
      try {
        if (zot && zot.File && typeof zot.File.pathToFile === "function") {
          let file = zot.File.pathToFile(configPath);
          if (file.exists()) {
            file.reveal();
            return;
          }
          let parentDir = zot.File.pathToFile(`${dataDir}/zocrates-data`);
          if (parentDir.exists()) {
            parentDir.reveal();
            return;
          }
        }
      } catch (err) {
        console.warn("Zocrates: Error revealing config file:", err);
      }
    });
    btnRow.appendChild(revealBtn);

    modal.show({
      window,
      title: "Advanced Settings",
      subtitle: "Custom JSON configuration for agents, models, and retrieval",
      rows: [
        { label: "Overview", value: "Advanced settings (per-agent models, thinking effort levels, and custom endpoints) can be customized in zocrates_config.json and take effect the next time a message is sent." },
        { label: "Config Location", value: configPath, valueStyle: "font-family: monospace; word-break: break-all; font-size: 11px;" },
        { label: "Caution", value: "⚠️ Warning: Manually editing zocrates_config.json requires strict JSON syntax. Invalid JSON or unsupported keys will prevent configuration from loading properly. Always make a backup before editing.", valueStyle: "color: #f59e0b; font-weight: 500;" },
        btnRow
      ]
    });
  });
}

if (aboutInfoBtn) {
  aboutInfoBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    hideAllFlyoutSubmenus();

    const modal = getInfoModal();
    if (!modal) {
      console.warn("Zocrates: InfoModal not available");
      return;
    }

    const zot = resolveZotero();
    const manifestUrl = (zot && zot.Zocrates && zot.Zocrates.rootURI)
      ? `${zot.Zocrates.rootURI}manifest.json`
      : "manifest.json";

    const manifestPromise = fetch(manifestUrl)
      .then(r => r.json())
      .catch(() => ({ version: "0.1.0" }));

    const statusPromise = fetch(`${BACKEND_URL}/status`, { headers: getAuthHeaders() })
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null);

    const [manifestData, statusData] = await Promise.all([manifestPromise, statusPromise]);

    const rawVersion = (manifestData && manifestData.version) ? manifestData.version : "0.1.0";
    const pluginVersion = rawVersion.startsWith("v") ? rawVersion : `v${rawVersion}`;

    const zoteroVersion = (zot && zot.version)
      ? zot.version
      : ((typeof Zotero !== "undefined" && Zotero.version) ? Zotero.version : "Unknown");

    const isConnected = !!statusData;
    const backendStatusStr = `${BACKEND_URL} (${isConnected ? "Connected" : "Offline"})`;

    const activeLlm = (statusData && statusData.model) || "Unknown";
    const activeEmbeddings = (statusData && statusData.embedding_info && statusData.embedding_info.active_model) || "Unknown";

    modal.show({
      window,
      title: "Zocrates Info",
      subtitle: "Zocrates AI Research Assistant for Zotero",
      rows: [
        { label: "Plugin Version", value: pluginVersion },
        { label: "Zotero Version", value: zoteroVersion },
        { label: "Local Backend", value: backendStatusStr, valueStyle: isConnected ? "color: #10b981;" : "color: #ef4444;" },
        { label: "Active LLM", value: activeLlm },
        { label: "Active Embeddings", value: activeEmbeddings }
      ]
    });
  });
}

function launchExternalUrl(targetUrl) {
  const zot = resolveZotero();
  if (zot && typeof zot.launchURL === "function") {
    zot.launchURL(targetUrl);
    return;
  }
  try {
    const win = window.parent || window.top || window.opener;
    if (win && win.Services && win.Services.io && win.Cc && win.Ci) {
      const uri = win.Services.io.newURI(targetUrl);
      const svc = win.Cc["@mozilla.org/uriloader/external-protocol-service;1"]
                      .getService(win.Ci.nsIExternalProtocolService);
      svc.loadURI(uri);
      return;
    }
  } catch (_) {}
  if (typeof window !== "undefined" && typeof window.open === "function") {
    window.open(targetUrl, "_blank");
  }
}

let _cachedExtensionVersion = null;

async function resolveExtensionManifestVersion() {
  if (_cachedExtensionVersion) return _cachedExtensionVersion;
  try {
    const zot = resolveZotero();
    const manifestUrl = (zot && zot.Zocrates && zot.Zocrates.rootURI)
      ? `${zot.Zocrates.rootURI}manifest.json`
      : "manifest.json";
    const res = await fetch(manifestUrl);
    if (res && res.ok) {
      const data = await res.json();
      if (data && data.version) {
        _cachedExtensionVersion = String(data.version);
        return _cachedExtensionVersion;
      }
    }
  } catch (_) {}
  return "0.1.0";
}

function sanitizeDiagnosticTextClient(rawText) {
  if (rawText === null || rawText === undefined) return "";
  let out = String(rawText);
  if (!out) return "";

  try {
    const liveToken = getAuthToken();
    if (liveToken && liveToken.length >= 4 && out.includes(liveToken)) {
      out = out.split(liveToken).join("[REDACTED_AUTH_TOKEN]");
    }
  } catch (_) {}

  try {
    const zot = resolveZotero();
    const dataDir = zot?.DataDirectory?.dir;
    if (dataDir && String(dataDir).length > 2) {
      const cleanNative = String(dataDir).replace(/[\\/]+$/, "");
      const cleanSlash = cleanNative.replace(/\\/g, "/");
      const cleanEscaped = cleanNative.replace(/\\/g, "\\\\");
      for (const variant of [cleanEscaped, cleanNative, cleanSlash]) {
        if (variant && variant.length > 2 && out.includes(variant)) {
          out = out.split(variant).join("<ZOTERO_DATA_DIR>");
        }
      }
    }
  } catch (_) {}

  out = out.replace(/sk-or-v1-[A-Za-z0-9_-]{8,}/g, "[REDACTED_OPENROUTER_KEY]");
  out = out.replace(/\bsk-[A-Za-z0-9_-]{16,}/g, "[REDACTED_API_KEY]");
  out = out.replace(/(\bBearer\s+)(?!\[REDACTED)([^\s"',;)}\]]+)/gi, "$1[REDACTED_TOKEN]");
  out = out.replace(/(\bX-Zocrates-Auth\s*[:=]\s*)(?!\[REDACTED)([^\s"',;)}\]]+)/gi, "$1[REDACTED_AUTH_TOKEN]");
  out = out.replace(/(\bX-Api-Key\s*[:=]\s*)(?!\[REDACTED)([^\s"',;)}\]]+)/gi, "$1[REDACTED_API_KEY]");
  out = out.replace(/([?&]?(?:token|api_key|apikey|access_token|auth_token)=)(?!\[REDACTED)([^\s&"',;)}\]]+)/gi, "$1[REDACTED_TOKEN]");
  out = out.replace(/(https?:\/\/)([^/\s:@]+:[^/\s@]+)@/gi, "$1[REDACTED_CREDENTIALS]@");
  out = out.replace(/\bmailto:[^\s"',;>)\]]+/gi, "mailto:[REDACTED_EMAIL]");
  out = out.replace(/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, "[REDACTED_EMAIL]");

  out = out.replace(/(?<![A-Za-z0-9_-])\/var\/home\/[^/\s"':;<>|\\]+/g, "<HOME>");
  out = out.replace(/(?<![A-Za-z0-9_-])\/home\/[^/\s"':;<>|\\]+/g, "<HOME>");
  out = out.replace(/(?<![A-Za-z0-9_-])\/Users\/[^/\s"':;<>|\\]+/g, "<HOME>");
  out = out.replace(/(?<![A-Za-z0-9_-])[A-Za-z]:\\\\Users\\\\[^\\\s"':;<>|/]+/g, "<HOME>");
  out = out.replace(/(?<![A-Za-z0-9_-])[A-Za-z]:\\Users\\[^\\\s"':;<>|/]+/g, "<HOME>");
  out = out.replace(/(?<![A-Za-z0-9_-])[A-Za-z]:\/Users\/[^/\s"':;<>|\\]+/g, "<HOME>");
  out = out.replace(/(?<![\\/\w:])\\\\\\\\[A-Za-z0-9._-]+\\\\[A-Za-z0-9._$-]+/g, "<UNC_PATH>");
  out = out.replace(/(?<![\\/\w:])\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+/g, "<UNC_PATH>");

  return out;
}

async function collectFrontendDiagnosticContext({ sessionId = null, includePrompts = false } = {}) {
  const zot = resolveZotero();
  const extVersion = await resolveExtensionManifestVersion();
  const zoteroVersion = (zot && zot.version)
    ? String(zot.version)
    : ((typeof Zotero !== "undefined" && Zotero.version) ? String(Zotero.version) : "Unknown");
  const zoteroPlatform = (zot && zot.platform)
    ? String(zot.platform)
    : ((typeof navigator !== "undefined" && navigator.platform) ? String(navigator.platform) : "Unknown");

  let rawNotifs = [];
  try {
    if (zot?.Zocrates?.Notifier && typeof zot.Zocrates.Notifier.getAll === "function") {
      rawNotifs = zot.Zocrates.Notifier.getAll();
    } else if (Array.isArray(notifications)) {
      rawNotifs = notifications;
    }
  } catch (_) {}

  const recentNotifs = (Array.isArray(rawNotifs) ? rawNotifs : []).slice(0, 5).map((n) => {
    let ts = "";
    try {
      if (n?.timestamp) {
        const d = new Date(n.timestamp);
        if (!isNaN(d.getTime())) ts = d.toISOString();
      }
    } catch (_) {}
    return {
      type: String(n?.type || "info"),
      text: sanitizeDiagnosticTextClient(String(n?.text || "")),
      timestamp: ts,
    };
  });

  const chips = (Array.isArray(activeItemsList) ? activeItemsList : []).slice(0, 25).map((item) => ({
    key: String(item?.key || item?.item_key || item?.itemKey || ""),
    title: String(item?.title || ""),
  }));

  let lastUserPrompt = null;
  if (includePrompts && sessionId && Array.isArray(sessions)) {
    const sessObj = sessions.find((s) => s && s.id === sessionId);
    if (sessObj && Array.isArray(sessObj.messages)) {
      for (let i = sessObj.messages.length - 1; i >= 0; i--) {
        const m = sessObj.messages[i];
        if (m && (m.role === "user" || m.sender === "user") && m.text) {
          lastUserPrompt = sanitizeDiagnosticTextClient(m.text);
          break;
        }
      }
    }
  }

  const backendConn = _lastBackendOnline === true
    ? "Connected"
    : (_lastBackendOnline === false ? "Offline" : "Unknown");

  return {
    zotero_version: zoteroVersion,
    zotero_platform: zoteroPlatform,
    extension_version: extVersion,
    window_mode: isDetached ? "detached" : "sidebar",
    backend_connection: backendConn,
    heartbeat_latency_ms: null,
    notifications: recentNotifs,
    active_items: chips,
    last_user_prompt: lastUserPrompt,
  };
}

function buildOfflineDiagnosticMarkdown({
  errorMessage = null,
  sessionId = null,
  invocationId = null,
  toggles = {},
  clientCtx = {},
  fetchError = null,
} = {}) {
  const includePrompts = Boolean(toggles.includePrompts);
  const includeItemTitles = Boolean(toggles.includeItemTitles);
  const includeLogs = Boolean(toggles.includeLogs);

  const generatedAt = new Date().toISOString();
  const safeErr = errorMessage
    ? sanitizeDiagnosticTextClient(errorMessage)
    : "No specific error message attached (manual report).";
  const safeBackendErr = fetchError
    ? sanitizeDiagnosticTextClient(fetchError.message || String(fetchError))
    : "Backend unreachable";

  const chips = Array.isArray(clientCtx.active_items) ? clientCtx.active_items : [];
  const formattedChips = chips.map((c) => {
    const safeKey = sanitizeDiagnosticTextClient(c.key || "unknown").slice(0, 32);
    const titlePart = includeItemTitles
      ? sanitizeDiagnosticTextClient(c.title || "Untitled")
      : "<Title omitted>";
    return `[Key: ${safeKey}] ${titlePart}`;
  });

  const lines = [
    "## Zocrates Diagnostic & Error Report (Client Fallback — Backend Unreachable)",
    "",
    `- **Generated**: \`${generatedAt}\``,
    `- **Zocrates Extension Version**: \`${sanitizeDiagnosticTextClient(clientCtx.extension_version || "0.1.0")}\``,
    `- **Zotero Version**: \`${sanitizeDiagnosticTextClient(clientCtx.zotero_version || "Unknown")}\` (\`${sanitizeDiagnosticTextClient(clientCtx.zotero_platform || "Unknown")}\`)`,
    `- **Window Mode**: \`${sanitizeDiagnosticTextClient(clientCtx.window_mode || "sidebar")}\``,
    `- **Backend Status**: \`Offline / Unreachable\` (\`${safeBackendErr}\`)`,
    "",
    "### Error Summary",
    "```text",
    safeErr,
    "```",
    "",
    "<details>",
    "<summary><b>Turn & Context Details</b></summary>",
    "",
    `- **Session ID**: \`${sanitizeDiagnosticTextClient(sessionId || "None")}\``,
    `- **Invocation ID**: \`${sanitizeDiagnosticTextClient(invocationId || "None")}\``,
  ];

  if (formattedChips.length > 0) {
    lines.push("- **Context Chips**:");
    for (const ch of formattedChips) {
      lines.push(`  - \`${ch}\``);
    }
  } else {
    lines.push("- **Context Chips**: `None`");
  }

  if (includePrompts) {
    lines.push(
      "",
      "**User Prompt**:",
      "```text",
      sanitizeDiagnosticTextClient(clientCtx.last_user_prompt || "<Unavailable offline>"),
      "```"
    );
  } else {
    lines.push("- **Prompt & Response**: `<User prompt and assistant message text omitted for privacy>`");
  }

  lines.push("", "</details>", "", "<details>", "<summary><b>Frontend Notifications (Last 5)</b></summary>", "");
  const notifs = Array.isArray(clientCtx.notifications) ? clientCtx.notifications.slice(0, 5) : [];
  if (notifs.length > 0) {
    for (const n of notifs) {
      let nText = sanitizeDiagnosticTextClient(n.text || "");
      if (!includeItemTitles) {
        nText = nText.replace(/((?:⚡\s*)?(?:Re-converting|Converting)(?:\s+new)?(?:\s+[^:]*)?:\s*)(.+?)(\s+with\s+)/g, "$1<Title omitted>$3");
        nText = nText.replace(/(Indexing failed:\s*)(.+?)(\s+\()/g, "$1<Title omitted>$3");
        nText = nText.replace(/"([^"\n\r]{2,})"/g, '"<Title omitted>"');
      }
      lines.push(`- \`${sanitizeDiagnosticTextClient(n.type || "info")}\`: ${n.timestamp ? `[${n.timestamp}] ` : ""}${nText}`);
    }
  } else {
    lines.push("_No recent notifications._");
  }
  lines.push("", "</details>", "", "<details>", "<summary><b>Backend Log Tail</b></summary>", "", "```text");
  if (includeLogs) {
    lines.push(`<Backend log tail unavailable: backend unreachable (${safeBackendErr})>`);
  } else {
    lines.push("<Backend log tail omitted (enable via checkbox if needed for debugging)>");
  }
  lines.push("```", "", "</details>", "");

  let report = lines.join("\n");
  const maxBytes = 50 * 1024;
  const marker = "\n... [truncated to maintain 50 KB report ceiling]\n";
  if (typeof TextEncoder !== "undefined") {
    const enc = new TextEncoder();
    const bytes = enc.encode(report);
    if (bytes.length > maxBytes) {
      const markerBytes = enc.encode(marker);
      const dec = new TextDecoder("utf-8", { fatal: false });
      report = dec.decode(bytes.slice(0, maxBytes - markerBytes.length)) + marker;
    }
  } else if (report.length > maxBytes) {
    report = report.slice(0, maxBytes - marker.length) + marker;
  }
  return report;
}

async function saveDiagnosticReportToDisk(markdownText) {
  const datePart = new Date().toISOString().slice(0, 10);
  const defaultFilename = `zocrates-error-report-${datePart}.md`;
  const zot = resolveZotero();
  const io = resolveOnboardingIO();

  // Tier 1: Zotero / Gecko FilePicker module + IOUtils.writeUTF8
  let FPClass = (typeof FilePicker !== "undefined" && FilePicker) || zot?.FilePicker || null;
  if (!FPClass && typeof ChromeUtils !== "undefined" && typeof ChromeUtils.importESModule === "function") {
    try {
      FPClass = ChromeUtils.importESModule("chrome://zotero/content/modules/filePicker.mjs").FilePicker;
    } catch (_) {}
  }

  if (FPClass) {
    const fp = new FPClass();
    fp.init((zot && typeof zot.getMainWindow === "function" ? zot.getMainWindow() : null) || window, "Save Zocrates Diagnostic Report", fp.modeSave);
    if (typeof fp.appendFilter === "function") {
      fp.appendFilter("Markdown Files (*.md)", "*.md");
    }
    fp.defaultString = defaultFilename;
    fp.defaultExtension = "md";
    const rv = await fp.show();
    if (rv === fp.returnOK || rv === fp.returnReplace) {
      const target = fp.file;
      const targetPath = typeof target === "string" ? target : target?.path;
      if (targetPath) {
        if (io && typeof io.writeUTF8 === "function") {
          await io.writeUTF8(targetPath, markdownText);
          return true;
        }
        if (zot?.File && typeof zot.File.putContentsAsync === "function") {
          await zot.File.putContentsAsync(targetPath, markdownText);
          return true;
        }
      }
    }
    return false;
  }

  // Tier 2: XPCOM nsIFilePicker fallback
  if (typeof Components !== "undefined" && Components?.classes && Components?.interfaces) {
    try {
      const nsIFP = Components.interfaces.nsIFilePicker;
      const fp = Components.classes["@mozilla.org/filepicker;1"].createInstance(nsIFP);
      fp.init((zot && typeof zot.getMainWindow === "function" ? zot.getMainWindow() : null) || window, "Save Zocrates Diagnostic Report", nsIFP.modeSave);
      fp.appendFilter("Markdown Files (*.md)", "*.md");
      fp.defaultString = defaultFilename;
      fp.defaultExtension = "md";
      const rv = await new Promise((resolve) => fp.open(resolve));
      if (rv === nsIFP.returnOK || rv === nsIFP.returnReplace) {
        const filePath = fp.file?.path;
        if (filePath) {
          if (io && typeof io.writeUTF8 === "function") {
            await io.writeUTF8(filePath, markdownText);
            return true;
          }
          if (zot?.File && typeof zot.File.putContentsAsync === "function") {
            await zot.File.putContentsAsync(filePath, markdownText);
            return true;
          }
        }
      }
      return false;
    } catch (_) {}
  }

  // Tier 3: Standard HTML5 Blob download fallback
  if (typeof Blob !== "undefined" && typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
    const blob = new Blob([markdownText], { type: "text/markdown;charset=utf-8" });
    const blobUrl = URL.createObjectURL(blob);
    const link = document.createElementNS("http://www.w3.org/1999/xhtml", "a");
    link.setAttribute("href", blobUrl);
    link.setAttribute("download", defaultFilename);
    link.style.cssText = "display:none;";
    document.body.appendChild(link);
    if (typeof link.click === "function") link.click();
    link.remove();
    setTimeout(() => {
      try { URL.revokeObjectURL(blobUrl); } catch (_) {}
    }, 1000);
    return true;
  }

  return false;
}

async function openDiagnosticReportModal(options = {}) {
  const modal = getInfoModal();
  if (!modal) {
    console.warn("Zocrates: InfoModal not available for diagnostic report");
    return null;
  }

  const {
    errorMessage = null,
    sessionId = null,
    invocationId = null,
  } = options || {};

  const effectiveSessionId = sessionId || currentSessionId || resolveActiveSessionId() || null;
  const XHTMLNS = "http://www.w3.org/1999/xhtml";

  const controlsBox = document.createElementNS(XHTMLNS, "div");
  controlsBox.className = "diagnostic-privacy-controls";

  const privacyTitle = document.createElementNS(XHTMLNS, "div");
  privacyTitle.className = "diagnostic-privacy-title";
  privacyTitle.textContent = "Optional Context (All OFF by default for privacy — API keys, emails & paths are always scrubbed):";
  controlsBox.appendChild(privacyTitle);

  function createToggleRow(className, labelText) {
    const label = document.createElementNS(XHTMLNS, "label");
    label.className = "diagnostic-privacy-toggle";
    const checkbox = document.createElementNS(XHTMLNS, "input");
    checkbox.setAttribute("type", "checkbox");
    checkbox.className = className;
    checkbox.checked = false;
    const span = document.createElementNS(XHTMLNS, "span");
    span.textContent = labelText;
    label.appendChild(checkbox);
    label.appendChild(span);
    controlsBox.appendChild(label);
    return checkbox;
  }

  const promptsToggle = createToggleRow(
    "diagnostic-toggle-prompts",
    "Include research prompts and message text"
  );
  const titlesToggle = createToggleRow(
    "diagnostic-toggle-titles",
    "Include research paper titles"
  );
  const logsToggle = createToggleRow(
    "diagnostic-toggle-logs",
    "Include backend log tail"
  );

  const previewTextarea = document.createElementNS(XHTMLNS, "textarea");
  previewTextarea.className = "diagnostic-preview-textarea";
  previewTextarea.setAttribute("readonly", "readonly");
  previewTextarea.readOnly = true;

  let currentReportText = "Generating sanitized diagnostic report...";
  const setPreviewContent = (val) => {
    currentReportText = String(val || "");
    previewTextarea.value = currentReportText;
    previewTextarea.textContent = currentReportText;
  };
  setPreviewContent(currentReportText);

  let debounceTimer = null;
  let requestSeq = 0;
  let modalClosed = false;

  async function refreshDiagnosticPreview() {
    const seq = ++requestSeq;
    const toggles = {
      includePrompts: Boolean(promptsToggle.checked),
      includeItemTitles: Boolean(titlesToggle.checked),
      includeLogs: Boolean(logsToggle.checked),
    };

    let clientCtx = {};
    try {
      clientCtx = await collectFrontendDiagnosticContext({
        sessionId: effectiveSessionId,
        includePrompts: toggles.includePrompts,
      });

      const t0 = Date.now();
      const res = await fetch(`${BACKEND_URL}/diagnostics/report`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          session_id: effectiveSessionId,
          invocation_id: invocationId || null,
          error_message: errorMessage || null,
          include_prompts: toggles.includePrompts,
          include_item_titles: toggles.includeItemTitles,
          include_logs: toggles.includeLogs,
          log_tail_lines: 250,
          frontend_metadata: {
            ...clientCtx,
            heartbeat_latency_ms: clientCtx.heartbeat_latency_ms,
          },
        }),
      });
      const elapsed = Date.now() - t0;
      if (!res || !res.ok) {
        throw new Error(res ? `HTTP ${res.status}` : "Network error");
      }
      const data = await res.json();
      if (modalClosed || seq !== requestSeq) return;
      if (data && typeof data.markdown === "string") {
        setPreviewContent(data.markdown);
      } else {
        throw new Error("Invalid diagnostic response payload");
      }
      void elapsed;
    } catch (fetchErr) {
      if (modalClosed || seq !== requestSeq) return;
      const fallbackMd = buildOfflineDiagnosticMarkdown({
        errorMessage,
        sessionId: effectiveSessionId,
        invocationId,
        toggles,
        clientCtx,
        fetchError: fetchErr,
      });
      setPreviewContent(fallbackMd);
    }
  }

  const scheduleRefresh = () => {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      refreshDiagnosticPreview();
    }, 150);
  };

  promptsToggle.addEventListener("change", scheduleRefresh);
  titlesToggle.addEventListener("change", scheduleRefresh);
  logsToggle.addEventListener("change", scheduleRefresh);

  modal.show({
    window,
    wide: true,
    maxWidth: "680px",
    title: "Zocrates Diagnostic & Error Report",
    subtitle: "Inspect the exact sanitized Markdown below before copying or saving.",
    rows: [controlsBox, previewTextarea],
    onClose: () => {
      modalClosed = true;
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
    },
    buttons: [
      {
        label: "📋 Copy to Clipboard",
        className: "diagnostic-copy-btn",
        primary: true,
        keepOpen: true,
        onClick: async (e, btn) => {
          const copied = await copyTextToClipboard(currentReportText);
          const targetBtn = btn || e?.currentTarget;
          if (copied && targetBtn) {
            const origLabel = "📋 Copy to Clipboard";
            targetBtn.textContent = "✓ Copied!";
            setTimeout(() => {
              if (!modalClosed) targetBtn.textContent = origLabel;
            }, 1500);
          }
          return false;
        },
      },
      {
        label: "💾 Save Report (.md)",
        className: "diagnostic-save-btn",
        keepOpen: true,
        onClick: async (e, btn) => {
          try {
            const saved = await saveDiagnosticReportToDisk(currentReportText);
            const targetBtn = btn || e?.currentTarget;
            if (saved && targetBtn) {
              const origLabel = "💾 Save Report (.md)";
              targetBtn.textContent = "✓ Saved!";
              setTimeout(() => {
                if (!modalClosed) targetBtn.textContent = origLabel;
              }, 1500);
            }
          } catch (saveErr) {
            console.warn("Zocrates: Failed saving diagnostic report:", saveErr);
          }
          return false;
        },
      },
      {
        label: "🐙 Open GitHub Issues",
        className: "diagnostic-github-btn",
        keepOpen: true,
        onClick: () => {
          launchExternalUrl("https://github.com/alexplaka/Zocrates/issues/new");
          return false;
        },
      },
      {
        label: "Close",
        className: "diagnostic-close-btn",
        onClick: () => true,
      },
    ],
  });

  await refreshDiagnosticPreview();
  return previewTextarea;
}

if (aboutReportIssueBtn) {
  aboutReportIssueBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    hideAllFlyoutSubmenus();

    openDiagnosticReportModal({
      sessionId: currentSessionId || resolveActiveSessionId() || null,
    });
  });
}

// Mode management (legacy no-op)
function loadSavedMode() {}

// Session Stats Icon Hover / Press Handlers
if (sessionStatsBtn && sessionStatsPopover) {
  let isStatsPinned = false;

  function showStatsPopover() {
    sessionStatsPopover.classList.remove("hidden");
    sessionStatsBtn.classList.add("active");
  }

  function hideStatsPopover() {
    if (!isStatsPinned) {
      sessionStatsPopover.classList.add("hidden");
      sessionStatsBtn.classList.remove("active");
    }
  }

  sessionStatsBtn.addEventListener("mouseenter", () => {
    showStatsPopover();
  });

  sessionStatsBtn.addEventListener("mouseleave", () => {
    hideStatsPopover();
  });

  sessionStatsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    isStatsPinned = !isStatsPinned;
    if (isStatsPinned) {
      showStatsPopover();
    } else {
      sessionStatsPopover.classList.add("hidden");
      sessionStatsBtn.classList.remove("active");
    }
  });

  sessionStatsBtn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      isStatsPinned = !isStatsPinned;
      if (isStatsPinned) {
        showStatsPopover();
      } else {
        sessionStatsPopover.classList.add("hidden");
        sessionStatsBtn.classList.remove("active");
      }
    } else if (e.key === "Escape") {
      isStatsPinned = false;
      sessionStatsPopover.classList.add("hidden");
      sessionStatsBtn.classList.remove("active");
    }
  });

  document.addEventListener("click", (e) => {
    if (!sessionStatsPopover.contains(e.target) && !sessionStatsBtn.contains(e.target)) {
      isStatsPinned = false;
      sessionStatsPopover.classList.add("hidden");
      sessionStatsBtn.classList.remove("active");
    }
  });
}

// Close context options dropdown on click outside
document.addEventListener("click", (e) => {
  if (contextOptionsDropdown && !addContextBtn.contains(e.target) && !contextOptionsDropdown.contains(e.target)) {
    contextOptionsDropdown.classList.add("hidden");
    if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
    clearMenuFocus();
    focusedMenuType = "main";
    focusedMenuIndex = -1;
  }
  if (themeOptionsDropdown && themeMenuContainer && !themeMenuContainer.contains(e.target)) {
    themeOptionsDropdown.classList.add("hidden");
  }
  if (aboutOptionsDropdown && aboutMenuContainer && !aboutMenuContainer.contains(e.target)) {
    aboutOptionsDropdown.classList.add("hidden");
  }
});

// Close context menus when sidebar loses focus or tab visibility changes
window.addEventListener("blur", () => {
  if (contextOptionsDropdown && !contextOptionsDropdown.classList.contains("hidden")) {
    contextOptionsDropdown.classList.add("hidden");
  }
  if (collectionsSubmenu && !collectionsSubmenu.classList.contains("hidden")) {
    collectionsSubmenu.classList.add("hidden");
  }
  hideAllFlyoutSubmenus();
  if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
  clearMenuFocus();
  focusedMenuType = "main";
  focusedMenuIndex = -1;
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (contextOptionsDropdown && !contextOptionsDropdown.classList.contains("hidden")) {
      contextOptionsDropdown.classList.add("hidden");
    }
    if (collectionsSubmenu && !collectionsSubmenu.classList.contains("hidden")) {
      collectionsSubmenu.classList.add("hidden");
    }
    hideAllFlyoutSubmenus();
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    clearMenuFocus();
    focusedMenuType = "main";
    focusedMenuIndex = -1;
  }
});

// Sticky Mode Toggle click listener
if (toggleStickyBtn) {
  toggleStickyBtn.addEventListener("click", () => {
    isStickyMode = !isStickyMode;
    if (isStickyMode) {
      toggleStickyBtn.classList.add("active");
      toggleStickyBtn.setAttribute("title", "Unpin context (sticky mode active)");
    } else {
      toggleStickyBtn.classList.remove("active");
      toggleStickyBtn.setAttribute("title", "Sticky mode (pin selected items/papers)");
      // Keep accumulated chips visible until the user changes selection in Zotero.
      // Set lastActiveKeysStr to current Zotero selection so background polling doesn't immediately replace chips.
      const items = readSelectedItemsNow();
      let regularItems = items.filter(item => {
        try {
          if (item.isRegularItem()) return true;
          if (item.isAttachment && item.isAttachment() && !item.parentItem) return true;
          if (item.isNote && item.isNote() && !item.parentItem) return true;
          return false;
        } catch(e) { return false; }
      });
      lastActiveKeysStr = regularItems.map(i => i.key).sort().join(",");
    }
  });
}

// Clear All Chips click listener
if (clearAllChipsBtn) {
  clearAllChipsBtn.addEventListener("click", () => {
    const currentActive = [...activeItemsList];
    activeItemsList = [];
    activeSnippetsList = [];
    contextMode = "papers";
    if (isStickyMode) {
      removedKeys.clear();
      lastActiveKeysStr = "";
    } else {
      // Add all currently visible chips to removedKeys
      currentActive.forEach(item => {
        if (item.key) removedKeys.add(item.key);
      });

      // Also add all currently selected papers' keys to removedKeys so they don't reappear immediately
      const items = readSelectedItemsNow();
      items.forEach(item => {
        if (item.key) removedKeys.add(item.key);
      });

      lastActiveKeysStr = items.map(i => i.key).sort().join(",");
    }
    renderActiveChips();
  });
}

function updateContextDropdownUI() {
  if (!addSelectedPapersOption || !addCollectionOption || !addLibraryOption) return;

  // Clear all highlights
  if (addCurrentViewOption) addCurrentViewOption.removeAttribute("data-selected");
  addSelectedPapersOption.removeAttribute("data-selected");
  addCollectionOption.removeAttribute("data-selected");
  addLibraryOption.removeAttribute("data-selected");

  // Evaluate active view in Zotero's left pane on-demand
  const pane = resolveZoteroPane();
  const selectedCols = resolveSelectedCollections(pane);

  const FOLDER_ICON_PATH = "M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z";
  const LIBRARY_ICON_PATH = "M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H8V4h12v12z";

  function setDynamicIcon(pathD) {
    if (!addCurrentViewIcon) return;
    addCurrentViewIcon.innerHTML = "";
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.style.cssText = "width:16px;height:16px;fill:currentColor;";
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", pathD);
    svg.appendChild(path);
    addCurrentViewIcon.appendChild(svg);
  }

  if (selectedCols.length > 0) {
    currentResolvedView = { type: "collection", collections: selectedCols };
    if (addCurrentViewOption) {
      addCurrentViewOption.style.display = "flex";
      if (selectedCols.length === 1) {
        const colName = selectedCols[0].name || "Collection";
        if (addCurrentViewText) addCurrentViewText.textContent = `Add Current Collection: "${colName}"`;
        addCurrentViewOption.title = colName;
      } else {
        if (addCurrentViewText) addCurrentViewText.textContent = `Add Current Collections (${selectedCols.length} selected)`;
        addCurrentViewOption.title = selectedCols.map(c => c.name).join(", ");
      }
      setDynamicIcon(FOLDER_ICON_PATH);

      // Check if all selected collections are already in activeItemsList
      const allActive = contextMode === "collection" && selectedCols.every(c =>
        activeItemsList.some(p => p.type === "collection" && (p.collectionKey === c.key || p.collectionId === c.id || p.key === c.name || p.key === c.key))
      );
      if (allActive) {
        addCurrentViewOption.setAttribute("data-selected", "true");
      }
    }
    if (currentViewSeparator) currentViewSeparator.style.display = "block";
  } else {
    const libInfo = resolveActiveLibraryInfo(pane);
    currentResolvedView = { type: "library", libInfo };
    if (addCurrentViewOption) {
      addCurrentViewOption.style.display = "flex";
      if (addCurrentViewText) addCurrentViewText.textContent = `Add Current Library ("${libInfo.name}")`;
      addCurrentViewOption.title = libInfo.name;
      setDynamicIcon(LIBRARY_ICON_PATH);

      if (contextMode === "library" && activeItemsList.some(p => p.type === "library")) {
        addCurrentViewOption.setAttribute("data-selected", "true");
      }
    }
    if (currentViewSeparator) currentViewSeparator.style.display = "block";
  }

  if (contextMode === "papers") {
    addSelectedPapersOption.setAttribute("data-selected", "true");
  } else if (contextMode === "library") {
    addLibraryOption.setAttribute("data-selected", "true");
  } else if (contextMode === "collection") {
    addCollectionOption.setAttribute("data-selected", "true");
  }

  // Populate collections list in submenu
  populateCollectionsSubmenu();
}

/**
 * Recursively fetches all collections (top-level and subcollections) in the specified library.
 */
function fetchAllCollectionsInLibrary(libraryID) {
  const colMap = new Map();
  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);

  function processCollection(col) {
    if (!col || !col.id) return;
    if (colMap.has(col.id)) return;

    colMap.set(col.id, col);

    let childCols = [];
    try {
      if (typeof col.getChildCollections === "function") {
        childCols = col.getChildCollections() || [];
      } else if (typeof col.getCollections === "function") {
        childCols = col.getCollections() || [];
      } else if (zot?.Collections && zot.Collections.getByParent) {
        childCols = zot.Collections.getByParent(col.id) || [];
      }
    } catch (err) {
      console.warn("Zocrates: Error fetching subcollections for col", col.id, err);
    }

    for (let child of childCols) {
      let childObj = typeof child === "object" ? child : (zot?.Collections?.get?.(child) || null);
      if (childObj) {
        processCollection(childObj);
      }
    }
  }

  let initial = [];
  try {
    if (zot?.Collections?.getByLibrary) {
      initial = zot.Collections.getByLibrary(libraryID, false) || [];
      if (initial.length === 0) {
        initial = zot.Collections.getByLibrary(libraryID, true) || [];
      }
    }
  } catch (e) {}

  try {
    if (zot?.Collections?.getByParent) {
      const rootCols = zot.Collections.getByParent(false) || zot.Collections.getByParent(null) || [];
      initial = [...initial, ...rootCols];
    }
  } catch (e) {}

  initial.forEach(c => processCollection(c));

  return Array.from(colMap.values());
}

/**
 * Returns the parent collection ID if present.
 */
function getParentId(col, colMap) {
  if (col.parentID && colMap.has(col.parentID)) return col.parentID;
  if (col.parentCollection && col.parentCollection.id && colMap.has(col.parentCollection.id)) return col.parentCollection.id;
  if (col.parentKey) {
    for (let c of colMap.values()) {
      if (c.key === col.parentKey) return c.id;
    }
  }
  return null;
}

/**
 * Calculates the full parent path (e.g. "Parent / Subcollection") and nesting depth.
 */
function getPathAndDepth(col, colMap) {
  const pathParts = [col.name];
  let curr = col;
  let depth = 0;
  const visited = new Set([col.id]);

  while (curr) {
    let parent = null;
    try {
      if (curr.parentCollection) {
        parent = curr.parentCollection;
      } else if (curr.parentID && colMap.has(curr.parentID)) {
        parent = colMap.get(curr.parentID);
      } else if (curr.parentKey) {
        for (let c of colMap.values()) {
          if (c.key === curr.parentKey) {
            parent = c;
            break;
          }
        }
      } else if (curr.parentID && Zotero.Collections?.get) {
        parent = Zotero.Collections.get(curr.parentID);
      }
    } catch (e) {}

    if (parent && parent.id && !visited.has(parent.id)) {
      visited.add(parent.id);
      pathParts.unshift(parent.name);
      depth++;
      curr = parent;
    } else {
      break;
    }
  }

  return {
    fullPath: pathParts.join(" / "),
    depth: depth
  };
}

function populateCollectionsSubmenu() {
  if (!collectionsSubmenu) return;
  collectionsSubmenu.innerHTML = "";

  try {
    const pane = resolveZoteroPane();
    const libraryID = resolveActiveLibraryID(pane);
    
    // Recursively fetch all collections and nested subcollections
    const rawCollections = fetchAllCollectionsInLibrary(libraryID);
    if (rawCollections.length === 0) {
      const placeholder = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      placeholder.style.cssText = "color:#94a3b8;font-size:11.5px;padding:8px 14px;text-align:center;font-style:italic;";
      placeholder.textContent = "No collections found";
      collectionsSubmenu.appendChild(placeholder);
      return;
    }

    // Build map by collection ID & parent mapping
    const colMap = new Map();
    rawCollections.forEach(col => colMap.set(col.id, col));

    const parentIdsWithChildren = new Set();
    rawCollections.forEach(col => {
      const pId = getParentId(col, colMap);
      if (pId) parentIdsWithChildren.add(pId);
    });

    // Calculate full path, nesting depth, and parent ID for each collection
    const enrichedCollections = rawCollections.map(col => {
      const info = getPathAndDepth(col, colMap);
      const pId = getParentId(col, colMap);
      return {
        col: col,
        id: col.id,
        parentId: pId,
        hasChildren: parentIdsWithChildren.has(col.id),
        name: col.name,
        fullPath: info.fullPath,
        depth: info.depth
      };
    });

    // Sort hierarchically by fullPath
    enrichedCollections.sort((a, b) => a.fullPath.localeCompare(b.fullPath));

    const itemDOMMap = new Map();

    function updateTreeVisibility() {
      enrichedCollections.forEach(info => {
        const el = itemDOMMap.get(info.id);
        if (!el) return;

        if (info.depth === 0) {
          el.style.display = "flex";
        } else {
          // Check if all ancestor parents are expanded
          let isVisible = true;
          let currPId = info.parentId;
          while (currPId && colMap.has(currPId)) {
            if (!expandedColIds.has(currPId)) {
              isVisible = false;
              break;
            }
            const parentObj = colMap.get(currPId);
            currPId = parentObj ? getParentId(parentObj, colMap) : null;
          }
          el.style.display = isVisible ? "flex" : "none";
        }

        const toggleEl = el.querySelector(".tree-toggle-btn");
        if (toggleEl) {
          const isExp = expandedColIds.has(info.id);
          toggleEl.textContent = isExp ? "▼" : "▶";
          toggleEl.title = isExp ? "Collapse subcollections" : "Expand subcollections";
        }
      });
    }

    enrichedCollections.forEach(colInfo => {
      const col = colInfo.col;
      const item = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
      item.className = "dropdown-item";
      
      const indentPx = 8 + (colInfo.depth * 12);
      item.style.cssText = `display:flex;align-items:center;gap:5px;width:100%;padding:6px 12px 6px ${indentPx}px;box-sizing:border-box;font-size:12px;cursor:pointer;`;
      item.title = colInfo.fullPath;
      itemDOMMap.set(colInfo.id, item);
      
      const isActive = (contextMode === "collection" && activeItemsList.some(p => p.type === "collection" && (p.collectionKey === col.key || p.collectionId === col.id || p.key === col.name || p.key === col.key)));
      if (isActive) {
        item.setAttribute("data-selected", "true");
      }

      // Expand / Collapse toggle caret button (if has children) or spacer
      if (colInfo.hasChildren) {
        const toggleBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
        toggleBtn.className = "tree-toggle-btn";
        toggleBtn.style.cssText = "background:transparent;border:none;color:var(--text-muted);cursor:pointer;width:14px;height:14px;padding:0;display:flex;align-items:center;justify-content:center;font-size:9px;opacity:0.8;flex-shrink:0;";
        toggleBtn.textContent = expandedColIds.has(colInfo.id) ? "▼" : "▶";
        toggleBtn.title = expandedColIds.has(colInfo.id) ? "Collapse subcollections" : "Expand subcollections";

        toggleBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          if (expandedColIds.has(colInfo.id)) {
            expandedColIds.delete(colInfo.id);
          } else {
            expandedColIds.add(colInfo.id);
          }
          updateTreeVisibility();
        });

        item.appendChild(toggleBtn);
      } else {
        const spacer = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        spacer.style.cssText = "width:14px;height:14px;flex-shrink:0;display:inline-block;";
        item.appendChild(spacer);
      }

      // Checkbox element
      const checkbox = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
      checkbox.type = "checkbox";
      checkbox.checked = isActive;
      checkbox.style.cssText = "cursor:pointer;width:14px;height:14px;accent-color:#4f46e5;flex-shrink:0;margin:0;";
      item.appendChild(checkbox);

      // Folder icon
      const folderIcon = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      folderIcon.style.cssText = "font-size:12px;opacity:0.85;flex-shrink:0;";
      folderIcon.textContent = colInfo.depth > 0 ? "📂" : "📁";
      item.appendChild(folderIcon);

      // Name label
      const nameSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
      nameSpan.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;flex-grow:1;";
      nameSpan.textContent = col.name;
      item.appendChild(nameSpan);

      item.addEventListener("click", (e) => {
        e.stopPropagation();
        
        // Ignore clicks directly on tree toggle button
        if (e.target.closest && e.target.closest(".tree-toggle-btn")) {
          return;
        }

        // If the click was not directly on the checkbox, toggle it programmatically.
        if (e.target !== checkbox) {
          checkbox.checked = !checkbox.checked;
          checkbox.dispatchEvent(new Event("change"));
        }
      });

      // Change listener on the checkbox (handles all state updates)
      checkbox.addEventListener("change", (e) => {
        e.stopPropagation();
        const willBeActive = checkbox.checked;

        if (willBeActive) {
          item.setAttribute("data-selected", "true");
          
          if (contextMode !== "collection") {
            contextMode = "collection";
            removedKeys.clear();
            activeSnippetsList = [];
            activeItemsList = [];
          }
          
          const isColActive = activeItemsList.some(p => p.type === "collection" && (p.collectionKey === col.key || p.collectionId === col.id || p.key === col.name));
          if (!isColActive) {
            activeItemsList.push({
              key: col.key || col.name,
              collectionKey: col.key,
              collectionId: col.id,
              name: col.name,
              title: "Collection: " + colInfo.fullPath,
              type: "collection"
            });
          }
        } else {
          item.removeAttribute("data-selected");
          activeItemsList = activeItemsList.filter(p => !(p.type === "collection" && (p.collectionKey === col.key || p.collectionId === col.id || p.key === col.name)));

          const hasCollections = activeItemsList.some(p => p.type === "collection");
          if (!hasCollections) {
            contextMode = "papers";
            lastActiveKeysStr = "";
            const items = readSelectedItemsNow();
            applySelection(items, true);
          }
        }

        renderActiveChips();
      });

      collectionsSubmenu.appendChild(item);
    });

    // Apply initial collapsed visibility (subcollections depth > 0 hidden by default)
    updateTreeVisibility();
  } catch (err) {
    console.error("Failed to populate collections submenu:", err);
  }
}

// Hover/click behaviors for the Collections submenu with hover delay
let hideSubmenuTimeout = null;

if (addCollectionOption) {
  addCollectionOption.addEventListener("mouseenter", () => {
    if (hideSubmenuTimeout) {
      clearTimeout(hideSubmenuTimeout);
      hideSubmenuTimeout = null;
    }
    if (collectionsSubmenu) collectionsSubmenu.classList.remove("hidden");
  });
  addCollectionOption.addEventListener("click", (e) => {
    e.stopPropagation();
    if (collectionsSubmenu) {
      if (hideSubmenuTimeout) {
        clearTimeout(hideSubmenuTimeout);
        hideSubmenuTimeout = null;
      }
      collectionsSubmenu.classList.toggle("hidden");
    }
  });
}

const collectionsMenuContainer = document.getElementById("collections-menu-container");
if (collectionsMenuContainer) {
  collectionsMenuContainer.addEventListener("mouseleave", () => {
    if (hideSubmenuTimeout) clearTimeout(hideSubmenuTimeout);
    hideSubmenuTimeout = setTimeout(() => {
      if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
    }, 400); // 400ms buffer to cross the gap
  });
  collectionsMenuContainer.addEventListener("mouseenter", () => {
    if (hideSubmenuTimeout) {
      clearTimeout(hideSubmenuTimeout);
      hideSubmenuTimeout = null;
    }
  });
}

if (collectionsSubmenu) {
  collectionsSubmenu.addEventListener("mouseenter", () => {
    if (hideSubmenuTimeout) {
      clearTimeout(hideSubmenuTimeout);
      hideSubmenuTimeout = null;
    }
  });
}

// Add Current View (Collection or Library) Option
if (addCurrentViewOption) {
  addCurrentViewOption.addEventListener("click", () => {
    contextOptionsDropdown.classList.add("hidden");
    if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
    clearMenuFocus();
    focusedMenuType = "main";
    focusedMenuIndex = -1;

    if (!currentResolvedView) return;

    if (currentResolvedView.type === "collection" && currentResolvedView.collections?.length > 0) {
      contextMode = "collection";
      removedKeys.clear();
      activeSnippetsList = [];
      activeItemsList = [];

      const cols = currentResolvedView.collections;
      for (const col of cols) {
        const isColActive = activeItemsList.some(p => p.type === "collection" && (p.collectionKey === col.key || p.collectionId === col.id || p.key === col.name));
        if (!isColActive) {
          const colTitle = col.name ? "Collection: " + col.name : "Collection";
          activeItemsList.push({
            key: col.key || col.name,
            collectionKey: col.key,
            collectionId: col.id,
            name: col.name,
            title: colTitle,
            type: "collection"
          });
        }

        // Ancestor expansion: ensure parent collections are expanded so populateCollectionsSubmenu displays them checked
        try {
          let currParentId = col.parentID || col.parentCollection?.id;
          while (currParentId) {
            expandedColIds.add(currParentId);
            const parentCol = Zotero?.Collections?.get?.(currParentId);
            currParentId = parentCol?.parentID || parentCol?.parentCollection?.id || null;
          }
        } catch (err) {
          console.warn("Zocrates: Error expanding ancestor collections:", err);
        }
      }

      renderActiveChips();
    } else if (currentResolvedView.type === "library") {
      contextMode = "library";
      removedKeys.clear();
      activeSnippetsList = [];
      activeItemsList = [];
      const libName = currentResolvedView.libInfo?.name || "My Library";
      activeItemsList.push({
        key: "all",
        title: libName,
        type: "library"
      });
      renderActiveChips();
    }
  });
}

// Add Selected Papers Option
addSelectedPapersOption.addEventListener("click", () => {
  const wasVisible = contextOptionsDropdown && !contextOptionsDropdown.classList.contains("hidden");
  contextOptionsDropdown.classList.add("hidden");
  if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
  clearMenuFocus();
  focusedMenuType = "main";
  focusedMenuIndex = -1;

  if (!wasVisible) return;

  contextMode = "papers";
  const items = readSelectedItemsNow();
  if (items.length > 0) {
    removedKeys.clear();
    lastActiveKeysStr = ""; // Force applySelection to re-run
    applySelection(items, true);
  } else {
    addNotification("To include research papers, select them in Zotero's library pane first.");
  }
});

// Add Entire Library Option
addLibraryOption.addEventListener("click", () => {
  contextOptionsDropdown.classList.add("hidden");
  if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
  clearMenuFocus();
  focusedMenuType = "main";
  focusedMenuIndex = -1;
  contextMode = "library";
  removedKeys.clear();
  activeSnippetsList = [];
  const pane = resolveZoteroPane();
  const libInfo = resolveActiveLibraryInfo(pane);
  const libName = libInfo?.name || "My Library";
  activeItemsList = [{
    key: "all",
    title: libName,
    type: "library"
  }];
  renderActiveChips();
});


// ---------------------------------------------------------------------------
// Sessions Management
// ---------------------------------------------------------------------------

function resolveZotero() {
  if (typeof window !== "undefined" && window.Zotero) return window.Zotero;
  if (typeof globalThis !== "undefined" && globalThis.Zotero) return globalThis.Zotero;
  if (typeof Zotero !== "undefined" && Zotero) return Zotero;
  try {
    if (window.parent && window.parent.Zotero) return window.parent.Zotero;
  } catch (e) {}
  try {
    if (window.top && window.top.Zotero) return window.top.Zotero;
  } catch (e) {}
  try {
    if (window.opener && window.opener.Zotero) return window.opener.Zotero;
    if (window.opener && window.opener.parent && window.opener.parent.Zotero) return window.opener.parent.Zotero;
    if (window.opener && window.opener.top && window.opener.top.Zotero) return window.opener.top.Zotero;
  } catch (e) {}
  try {
    const win = (window.parent && window.parent.Services) ? window.parent : 
                (window.top && window.top.Services ? window.top : 
                (window.opener && window.opener.Services ? window.opener : (typeof Services !== "undefined" ? Services : null)));
    if (win && win.Services && win.Services.wm) {
      let mainWin = win.Services.wm.getMostRecentWindow("navigator:browser") || win.Services.wm.getMostRecentWindow("zotero:main") || win.Services.wm.getMostRecentWindow(null);
      if (mainWin && mainWin.Zotero) return mainWin.Zotero;
      const enumWins = win.Services.wm.getEnumerator(null);
      while (enumWins.hasMoreElements()) {
        const w = enumWins.getNext();
        if (w && w.Zotero) return w.Zotero;
      }
    }
  } catch (e) {}
  return null;
}

function getSessionsDirPath() {
  const zoteroObj = resolveZotero();
  if (!zoteroObj) return null;

  try {
    if (zoteroObj.DataDirectory && zoteroObj.DataDirectory.dir) {
      const dir = zoteroObj.DataDirectory.dir;
      let rootPath = "";
      if (typeof dir === "string" && dir.trim()) {
        rootPath = dir.trim();
      } else if (dir && dir.path) {
        rootPath = dir.path;
      }
      if (rootPath) {
        return rootPath + (rootPath.endsWith("/") ? "" : "/") + "zocrates-data/sessions";
      }
    }
  } catch (e) {
    console.error("Zocrates: Failed to resolve DataDirectory sessions path:", e);
  }
  return null;
}

function getSessionsNsIFile(pathStr) {
  if (!pathStr) return null;
  try {
    if (typeof Components !== "undefined" && Components.classes && Components.classes["@mozilla.org/file/local;1"]) {
      const f = Components.classes["@mozilla.org/file/local;1"].createInstance(Components.interfaces.nsIFile);
      f.initWithPath(pathStr);
      return f;
    }
  } catch (e) {}
  return null;
}

async function ensureSessionsDir(dirPath) {
  const io = (typeof IOUtils !== "undefined" ? IOUtils :
             (window.parent && window.parent.IOUtils ? window.parent.IOUtils :
             (window.top && window.top.IOUtils ? window.top.IOUtils : null)));
  if (io && typeof io.makeDirectory === "function") {
    try {
      await io.makeDirectory(dirPath, { ignoreExisting: true });
      return;
    } catch (e) {}
  }
  try {
    if (typeof Components !== "undefined" && Components.classes) {
      const f = Components.classes["@mozilla.org/file/local;1"].createInstance(Components.interfaces.nsIFile);
      f.initWithPath(dirPath);
      if (!f.exists()) {
        f.create(Components.interfaces.nsIFile.DIRECTORY_TYPE, 0755);
      }
    }
  } catch (e) {}
}

function getSessionFilename(session) {
  if (session && session.filename) return session.filename;
  if (!session) return null;
  const dateObj = session.created_at ? new Date(session.created_at) : new Date();
  const ts = dateObj.getFullYear() +
    String(dateObj.getMonth() + 1).padStart(2, '0') +
    String(dateObj.getDate()).padStart(2, '0') + "_" +
    String(dateObj.getHours()).padStart(2, '0') +
    String(dateObj.getMinutes()).padStart(2, '0') +
    String(dateObj.getSeconds()).padStart(2, '0');
  session.filename = `${ts}_${session.id}.json`;
  return session.filename;
}

async function saveSessionFile(session) {
  const dirPath = getSessionsDirPath();
  if (!dirPath || !session) return;

  await ensureSessionsDir(dirPath);

  const nowIso = new Date().toISOString();
  if (!session.created_at) {
    session.created_at = nowIso;
  }
  session.updated_at = nowIso;
  getSessionFilename(session);

  const sessionData = {
    id: session.id,
    title: session.title,
    filename: session.filename,
    created_at: session.created_at,
    updated_at: session.updated_at,
    messages: (session.messages || []).map(m => {
      const role = m.role || (m.sender === "user" ? "user" : "assistant");
      const { sender, ...rest } = m;
      return { role, ...rest };
    })
  };

  const filePath = dirPath + "/" + session.filename;
  const jsonStr = JSON.stringify(sessionData, null, 2);

  console.log("Zocrates: Writing individual session file to:", filePath);

  const io = (typeof IOUtils !== "undefined" ? IOUtils :
             (window.parent && window.parent.IOUtils ? window.parent.IOUtils :
             (window.top && window.top.IOUtils ? window.top.IOUtils : null)));

  if (io && typeof io.writeUTF8 === "function") {
    try {
      await io.writeUTF8(filePath, jsonStr);
      console.log("Zocrates: Successfully saved session via IOUtils to:", filePath);
      return;
    } catch (e) {
      console.warn("Zocrates: IOUtils writeUTF8 failed for session file:", e);
    }
  }

  const zoteroObj = resolveZotero();
  if (zoteroObj && zoteroObj.File) {
    try {
      if (typeof zoteroObj.File.putContentsAsync === "function") {
        await zoteroObj.File.putContentsAsync(filePath, jsonStr);
        console.log("Zocrates: Saved session via putContentsAsync to:", filePath);
        return;
      }
    } catch (e) {}

    try {
      if (typeof zoteroObj.File.putContents === "function") {
        const nsIFile = getSessionsNsIFile(filePath);
        if (nsIFile) zoteroObj.File.putContents(nsIFile, jsonStr);
        else zoteroObj.File.putContents(filePath, jsonStr);
        console.log("Zocrates: Saved session via putContents to:", filePath);
        return;
      }
    } catch (e) {}
  }
}

async function saveSessionsIndex() {
  const dirPath = getSessionsDirPath();
  if (!dirPath) return;

  await ensureSessionsDir(dirPath);
  const filePath = dirPath + "/index.json";

  const manifest = {
    currentSessionId: currentSessionId,
    sessions: sessions.map(s => ({
      id: s.id,
      title: s.title,
      filename: getSessionFilename(s),
      created_at: s.created_at || new Date().toISOString(),
      updated_at: s.updated_at || new Date().toISOString()
    }))
  };

  const jsonStr = JSON.stringify(manifest, null, 2);
  console.log("Zocrates: Writing sessions index to:", filePath);

  const io = (typeof IOUtils !== "undefined" ? IOUtils :
             (window.parent && window.parent.IOUtils ? window.parent.IOUtils :
             (window.top && window.top.IOUtils ? window.top.IOUtils : null)));

  if (io && typeof io.writeUTF8 === "function") {
    try {
      await io.writeUTF8(filePath, jsonStr);
      console.log("Zocrates: Saved index.json via IOUtils to:", filePath);
      return;
    } catch (e) {}
  }

  const zoteroObj = resolveZotero();
  if (zoteroObj && zoteroObj.File) {
    try {
      if (typeof zoteroObj.File.putContentsAsync === "function") {
        await zoteroObj.File.putContentsAsync(filePath, jsonStr);
        return;
      }
      if (typeof zoteroObj.File.putContents === "function") {
        const nsIFile = getSessionsNsIFile(filePath);
        if (nsIFile) zoteroObj.File.putContents(nsIFile, jsonStr);
        else zoteroObj.File.putContents(filePath, jsonStr);
        return;
      }
    } catch (e) {}
  }
}

function getStoredSessionTitles() {
  try {
    const raw = localStorage.getItem("zocrates_session_titles");
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    }
  } catch (e) {}
  try {
    const zot = resolveZotero();
    const raw = zot?.Prefs?.get?.("extensions.zocrates.session_titles", true);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    }
  } catch (e) {}
  return {};
}

function saveStoredSessionTitle(sessionId, title) {
  if (!sessionId) return;
  const titles = getStoredSessionTitles();
  if (title) {
    titles[sessionId] = title;
  } else {
    delete titles[sessionId];
  }
  try {
    localStorage.setItem("zocrates_session_titles", JSON.stringify(titles));
  } catch (e) {}
  try {
    const zot = resolveZotero();
    zot?.Prefs?.set?.("extensions.zocrates.session_titles", JSON.stringify(titles), true);
  } catch (e) {}
}

async function loadSessions() {
  // In-flight guard: prevent concurrent execution on warm starts
  if (_loadSessionsPromise) return _loadSessionsPromise;
  if (isStreaming) return Promise.resolve();

  _loadSessionsPromise = (async () => {
    try {
      const storedTitles = getStoredSessionTitles();
      try {
        const res = await fetch(`${API_BASE}/sessions`, {
          headers: getAuthHeaders(),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.sessions && Array.isArray(data.sessions)) {
            sessions = data.sessions.map(s => {
              const existing = sessions.find(e => e.id === s.id);
              const resolvedTitle = storedTitles[s.id]
                || (existing?.renamed && existing.title)
                || s.title
                || existing?.title
                || "New Chat";
              return {
                id: s.id,
                title: resolvedTitle,
                renamed: Boolean(storedTitles[s.id] || existing?.renamed),
                created_at: s.created_at,
                updated_at: s.updated_at,
                messages: existing?.messages || []
              };
            });
            const savedTarget = resolveActiveSessionId();
            if (savedTarget && sessions.some(s => s.id === savedTarget)) {
              setActiveSessionId(savedTarget);
            } else if (sessions.length > 0) {
              // Fall back to most recent session
              setActiveSessionId(sessions[sessions.length - 1].id);
            }
            await maybeAutoGraduateOnboardingFromSessions();
          }
        }
      } catch (e) {
        console.warn("Zocrates: Error fetching sessions from backend:", e);
      }

      if (sessions.length === 0) {
        await createNewSession("New Chat");
      } else {
        renderSessionsList();
        await loadActiveSessionMessages();
        await maybeAutoGraduateOnboardingFromSessions();
      }
    } finally {
      _loadSessionsPromise = null;
    }
  })();

  return _loadSessionsPromise;
}

async function createNewSession(title = "New Chat") {
  clearGhostSuggestion();
  cleanupOnboardingResources();
  activeSnippetsList = [];
  renderActiveChips();
  let newSession = null;
  try {
    const res = await fetch(`${API_BASE}/sessions`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ title: title })
    });
    if (res.ok) {
      const data = await res.json();
      if (data.session) {
        newSession = {
          id: data.session.id,
          title: data.session.title || title,
          created_at: data.session.created_at || new Date().toISOString(),
          messages: []
        };
      }
    }
  } catch (e) {
    console.warn("Zocrates: Failed to create session via API:", e);
  }

  if (!newSession) {
    const id = "session_" + Math.random().toString(36).substring(2, 11);
    const nowIso = new Date().toISOString();
    newSession = {
      id: id,
      title: title,
      created_at: nowIso,
      updated_at: nowIso,
      messages: []
    };
  }

  sessions.push(newSession);
  setActiveSessionId(newSession.id);
  if (sessionsSearchInput) {
    sessionsSearchInput.value = "";
  }
  renderSessionsList();
  await loadActiveSessionMessages();
}

async function loadActiveSessionMessages() {
  clearGhostSuggestion();
  cleanupOnboardingResources();
  activeSnippetsList = [];
  renderActiveChips();
  chatMessages.innerHTML = "";
  currentSessionCostUsd = 0.0;
  currentSessionTotalTokens = 0;
  currentSessionTurns = 0;
  currentSessionTokenBreakdown = { prompt: 0, candidates: 0, cached: 0 };
  updateSessionStatsUI();

  if (!currentSessionId) return;

  const session = sessions.find(s => s.id === currentSessionId);
  if (!session) return;

  try {
    const res = await fetch(`${API_BASE}/sessions/${currentSessionId}`, {
      headers: getAuthHeaders(),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.session) {
        if (Array.isArray(data.session.messages)) {
          session.messages = data.session.messages;
        }
        if (data.session.pending_hitl) {
          session.pending_hitl = data.session.pending_hitl;
        } else {
          session.pending_hitl = null;
        }
        const storedTitles = getStoredSessionTitles();
        if (storedTitles[session.id]) {
          session.title = storedTitles[session.id];
          session.renamed = true;
        } else if (data.session.title && data.session.title !== "New Chat" && !session.renamed) {
          session.title = data.session.title;
        }

        const stats = calculateSessionStats(data);
        currentSessionTotalTokens = stats.totalTokens;
        currentSessionCostUsd = stats.costUsd;
        currentSessionTurns = stats.turns;
        currentSessionTokenBreakdown = stats.tokenBreakdown;
        updateSessionStatsUI();
      }
    }
  } catch (e) {
    console.warn("Zocrates: Failed to load session messages from API:", e);
  }

  if (session.messages.length === 0) {
    appendWelcomeMessage();
  } else {
    session.messages.forEach((msg, idx) => {
      const role = msg.role || (msg.sender === "user" ? "user" : "assistant");
      const isMarkdown = msg.isMarkdown !== false;
      const turnCost = msg.turn_cost_usd !== undefined ? msg.turn_cost_usd : (msg.usage?.cost_usd ?? null);
      const agentModels = msg.agent_models || msg.usage?.agent_models || null;
      const meta = {
        role: role,
        author: msg.author,
        model: msg.model || msg.llm_model,
        usage: msg.usage,
        turn_cost_usd: turnCost,
        agent_models: agentModels,
        finish_reason: msg.finish_reason,
        active_context_items: msg.active_context_items,
        snippets: msg.snippets || null,
        event_id: msg.event_id || msg.id,
        invocation_id: msg.invocation_id,
        duration: msg.duration,
        timestamp: msg.timestamp,
        intermediate_steps: msg.intermediate_steps || [],
        text: msg.text,
        citations: msg.citations,
        references: msg.references,
        candidates: msg.candidates || msg.metadata?.candidates || null,
        citation_verification: msg.citation_verification || msg.metadata?.citation_verification || null,
        isError: Boolean(msg.isError || msg.metadata?.isError),
        errorMessage: msg.errorMessage || msg.metadata?.errorMessage || null,
        invocationId: msg.invocationId || msg.invocation_id || msg.metadata?.invocationId || msg.metadata?.invocation_id || null
      };

      const msgDiv = appendMessageDOM(role, msg.text, isMarkdown, msg.timestamp, msg.author, msg.model || msg.llm_model, msg.duration, meta);

      if (role === "assistant") {
        let bubbleDiv = null;
        if (msg.intermediate_steps && msg.intermediate_steps.length > 0) {
          bubbleDiv = appendIntermediateStepsBubbleDOM(msg.intermediate_steps, meta);
        } else if (msg.thought) {
          bubbleDiv = appendIntermediateStepsBubbleDOM([
            { type: "thought", author: formatAuthorName("assistant", msg.author), content: msg.thought }
          ], meta);
        }
        if (bubbleDiv && msgDiv && msgDiv.parentElement) {
          msgDiv.parentElement.insertBefore(bubbleDiv, msgDiv);
        }

        const isLastMsg = (idx === session.messages.length - 1);
        const hitl = msg.pending_hitl || (isLastMsg && session.pending_hitl);
        if (hitl && isLastMsg) {
          if (!bubbleDiv) {
            bubbleDiv = appendIntermediateStepsBubbleDOM(msg.intermediate_steps || [], meta);
            if (msgDiv && msgDiv.parentElement) {
              msgDiv.parentElement.insertBefore(bubbleDiv, msgDiv);
            }
          }
          if (bubbleDiv) {
            updateIntermediateStepsBubbleProgress(bubbleDiv, msg.intermediate_steps || [], "⚡ Action Required: Deep Research Checkpoint");
            if (bubbleDiv._iconSpan) {
              bubbleDiv._iconSpan.className = "intermediate-steps-icon";
              bubbleDiv._iconSpan.textContent = "⚡";
            }
            const reloadCtx = {
              assistantBubble: msgDiv,
              currentResponseText: msg.text || "",
              streamedEventIds: new Set(),
              authorName: msg.author || "Dialogue Agent",
              streamModel: msg.model || null,
              streamUsage: msg.usage || null,
              streamFinishReason: null,
              streamEventId: msg.id || null,
              streamInvocationId: hitl.invocation_id || msg.invocation_id || null,
              stepsBubble: bubbleDiv,
              liveSteps: msg.intermediate_steps || [],
              payloadItems: msg.active_context_items || [],
              requestStartTime: Date.now(),
              usedModel: msg.model || "google/gemini-3.8-flash",
              session: session,
              isInterrupted: true
            };
            renderHitlCheckpointCard(bubbleDiv, hitl, async (responsePayload) => {
              await resumeDialogueResponse(
                hitl.invocation_id,
                hitl.interrupt_id,
                responsePayload,
                reloadCtx
              );
            });
          }
        }
      }
    });
  }
}

function appendWelcomeMessage() {
  appendMessageDOM(
    "assistant",
    "Hello! I am Zocrates, your research assistant. Ask me questions about your papers or library, or ask me to discover new literature and preprints online to import into Zotero.",
    true,
    null,
    "Zocrates"
  );
}

async function deleteSession(sessionId) {
  const index = sessions.findIndex(s => s.id === sessionId);
  if (index === -1) return;

  saveStoredSessionTitle(sessionId, null);

  try {
    const res = await fetch(`${API_BASE}/sessions/${sessionId}`, {
      method: "DELETE",
      headers: getAuthHeaders(),
    });
    if (!res.ok) {
      addNotification("Warning: Could not delete session from server: " + res.statusText, "error");
    }
  } catch (e) {
    console.warn("Zocrates: Failed to delete session from API:", e);
    addNotification("Warning: Could not delete session from server: " + e.message, "error");
  }

  sessions.splice(index, 1);

  if (currentSessionId === sessionId) {
    if (sessions.length > 0) {
      setActiveSessionId(sessions[sessions.length - 1].id);
    } else {
      await createNewSession("New Chat");
      return;
    }
  }

  renderSessionsList();
  await loadActiveSessionMessages();
}


/**
 * Formats an entire conversation/session as Markdown string for Zotero note export.
 * Strictly includes only User messages and final response messages.
 */
async function saveSessionAsNoteText(session) {
  if (!session) {
    throw new Error("No session provided.");
  }

  let messages = session.messages;
  if (!messages || messages.length === 0) {
    try {
      const res = await fetch(`${API_BASE}/sessions/${session.id}`, {
        headers: getAuthHeaders(),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.session && Array.isArray(data.session.messages)) {
          messages = data.session.messages;
          session.messages = messages;
        }
      }
    } catch (e) {
      console.warn("Zocrates: Error fetching session messages for note export:", e);
    }
  }

  if (!messages || messages.length === 0) {
    throw new Error("No messages found in this session.");
  }

  const cleanEntries = [];
  for (const msg of messages) {
    const isUser = msg.role === "user" || msg.sender === "user" || msg.author === "user";
    const isAssistant = (msg.role === "assistant" || msg.role === "model" || msg.sender === "assistant" || msg.sender === "model") &&
                        Boolean(msg.text && msg.text !== msg.thought) &&
                        !msg.is_intermediate && !msg.intermediate &&
                        msg.author !== "Observation" && msg.author !== "Search Agent" && msg.author !== "search_subagent";

    if (isUser) {
      const body = (msg.text || "").trim();
      if (body) {
        cleanEntries.push({ role: "User", text: body });
      }
    } else if (isAssistant) {
      const body = (msg.text || "").trim();
      if (body) {
        cleanEntries.push({ role: "Zocrates", text: body });
      }
    }
  }

  if (cleanEntries.length === 0) {
    throw new Error("No messages found in this session.");
  }

  const dateStr = new Date().toLocaleDateString();

  let markdownLines = [`*Saved from Zocrates on ${dateStr}*`, ""];

  cleanEntries.forEach(entry => {
    markdownLines.push(`### ${entry.role}`);
    markdownLines.push(entry.text);
    markdownLines.push("");
    if (entry.role === "Zocrates") {
      markdownLines.push("---");
      markdownLines.push("");
    }
  });

  return markdownLines.join("\n");
}

function openRenameSessionModal(session) {
  // Remove existing rename modals if any
  document.querySelectorAll(".rename-modal-overlay").forEach(el => el.remove());

  const overlay = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  overlay.className = "modal-overlay rename-modal-overlay";

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "modal-card";

  // Header
  const header = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  header.className = "modal-header";
  
  const title = document.createElementNS("http://www.w3.org/1999/xhtml", "h3");
  title.className = "modal-title";
  title.textContent = "Rename Session";
  header.appendChild(title);

  const closeBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  closeBtn.className = "modal-close-btn";
  closeBtn.textContent = "✕";
  closeBtn.title = "Cancel";
  closeBtn.addEventListener("click", () => overlay.remove());
  header.appendChild(closeBtn);
  card.appendChild(header);

  // Body
  const body = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  body.className = "modal-body";

  const input = document.createElementNS("http://www.w3.org/1999/xhtml", "input");
  input.type = "text";
  input.className = "modal-input";
  input.maxLength = 80;
  input.value = session.title || "";
  body.appendChild(input);
  card.appendChild(body);

  // Footer
  const footer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  footer.className = "modal-footer";

  const counter = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  counter.className = "modal-char-counter";

  function updateCounter() {
    const remaining = 80 - input.value.length;
    counter.textContent = `${remaining} character${remaining === 1 ? '' : 's'} remaining`;
    if (remaining <= 0) {
      counter.style.color = "#ef4444";
    } else if (remaining <= 15) {
      counter.style.color = "#f59e0b";
    } else {
      counter.style.color = "";
    }
  }

  updateCounter();
  input.addEventListener("input", updateCounter);

  footer.appendChild(counter);

  const actions = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actions.className = "modal-actions";

  const cancelBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  cancelBtn.className = "modal-btn-cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => overlay.remove());

  const saveBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  saveBtn.className = "modal-btn-save";
  saveBtn.textContent = "Save";

  function saveAndClose() {
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== session.title) {
      session.title = newTitle.substring(0, 80);
      session.renamed = true;
      saveStoredSessionTitle(session.id, session.title);
      renderSessionsList();
    }
    overlay.remove();
  }

  saveBtn.addEventListener("click", saveAndClose);
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  footer.appendChild(actions);
  card.appendChild(footer);

  overlay.appendChild(card);

  // Event handlers
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      saveAndClose();
    } else if (e.key === "Escape") {
      e.preventDefault();
      overlay.remove();
    }
  });

  document.body.appendChild(overlay);
  input.focus();
  input.select();
}

function openDeleteSessionModal(session) {
  if (!session) return;

  // Remove existing delete modals if any
  document.querySelectorAll(".delete-modal-overlay").forEach(el => el.remove());

  const overlay = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  overlay.className = "modal-overlay delete-modal-overlay";

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "modal-card";

  // Header
  const header = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  header.className = "modal-header";

  const title = document.createElementNS("http://www.w3.org/1999/xhtml", "h3");
  title.className = "modal-title";
  title.textContent = "Delete Session";
  header.appendChild(title);

  const closeBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  closeBtn.className = "modal-close-btn";
  closeBtn.textContent = "✕";
  closeBtn.title = "Cancel";
  closeBtn.addEventListener("click", () => overlay.remove());
  header.appendChild(closeBtn);
  card.appendChild(header);

  // Body
  const body = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  body.className = "modal-body";

  const promptText = document.createElementNS("http://www.w3.org/1999/xhtml", "p");
  promptText.style.cssText = "font-size: 13px; line-height: 1.5; color: var(--text-main); margin: 0; word-break: break-word;";
  promptText.textContent = `Are you sure you want to delete "${session.title || 'this session'}"? This action cannot be undone.`;
  body.appendChild(promptText);
  card.appendChild(body);

  // Footer
  const footer = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  footer.className = "modal-footer";
  footer.style.justifyContent = "flex-end";

  const actions = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actions.className = "modal-actions";

  const cancelBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  cancelBtn.className = "modal-btn modal-btn-secondary modal-btn-cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => overlay.remove());

  const confirmDeleteBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
  confirmDeleteBtn.className = "modal-btn modal-btn-delete";
  confirmDeleteBtn.style.cssText = "background: #ef4444; color: #ffffff; border: none; padding: 6px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; transition: background-color 0.15s;";
  confirmDeleteBtn.textContent = "Delete";
  confirmDeleteBtn.addEventListener("mouseenter", () => {
    confirmDeleteBtn.style.background = "#dc2626";
  });
  confirmDeleteBtn.addEventListener("mouseleave", () => {
    confirmDeleteBtn.style.background = "#ef4444";
  });
  confirmDeleteBtn.addEventListener("click", async () => {
    overlay.remove();
    await deleteSession(session.id);
  });

  actions.appendChild(cancelBtn);
  actions.appendChild(confirmDeleteBtn);
  footer.appendChild(actions);
  card.appendChild(footer);

  overlay.appendChild(card);

  // Event handlers
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  overlay.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      overlay.remove();
    }
  });

  document.body.appendChild(overlay);
  cancelBtn.focus();
}

function renderSessionsList() {
  const searchInputEl = document.getElementById("sessions-search-input") || sessionsSearchInput;
  const searchQuery = (searchInputEl?.value || "").toLowerCase().trim();
  sessionsList.innerHTML = "";

  const filteredSessions = sessions.filter(session => {
    if (!searchQuery) return true;
    return (session.title || "").toLowerCase().includes(searchQuery);
  });

  [...filteredSessions].reverse().forEach(session => {
    const item = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    item.className = "session-item" + (session.id === currentSessionId ? " active" : "");
    item.setAttribute("role", "button");
    item.setAttribute("tabindex", "0");
    item.title = session.title;

    // C1: Use textContent instead of innerHTML for user-derived session titles
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("style", "width: 14px; height: 14px; fill: currentColor; opacity: 0.7;");
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", "M20 2H4c-1.1 0-1.99.9-1.99 2L2 22l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 9h12v2H6V9zm8 5H6v-2h8v2zm4-6H6V6h12v2z");
    svg.appendChild(path);
    item.appendChild(svg);

    const titleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
    titleSpan.textContent = session.title;
    item.appendChild(titleSpan);

    // Rename session button on hover
    const renameBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    renameBtn.className = "session-rename-btn";
    renameBtn.setAttribute("title", "Rename session");
    renameBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:12px; height:12px; fill:currentColor;"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>`;

    renameBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openRenameSessionModal(session);
    });

    item.appendChild(renameBtn);

    // Save session as Zotero Note button
    const saveNoteBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    saveNoteBtn.className = "session-save-btn";
    saveNoteBtn.setAttribute("title", "Save session as Zotero note");
    saveNoteBtn.innerHTML = `
      <svg class="note-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:13px; height:13px; fill:currentColor;"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>
      <svg class="check-icon hidden" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="width:13px; height:13px; fill:currentColor; color:#22c55e;"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
    `;

    saveNoteBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const fullMd = await saveSessionAsNoteText(session);
        const sessionName = (session && session.title) ? session.title : "Untitled Session";
        const noteTitle = `Zocrates Session: ${sessionName}`;
        triggerSaveNoteFlow(saveNoteBtn, fullMd, noteTitle, ["Zocrates session log"]);
      } catch (err) {
        console.error("Zocrates: Failed to save session note:", err);
        addNotification("Failed to save session note: " + err.message, "error");
      }
    });

    item.appendChild(saveNoteBtn);

    // Delete session button on hover
    const deleteBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    deleteBtn.className = "session-delete-btn";
    deleteBtn.setAttribute("title", "Delete session");
    deleteBtn.textContent = "✕";

    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openDeleteSessionModal(session);
    });

    item.appendChild(deleteBtn);

    item.addEventListener("click", () => {
      setActiveSessionId(session.id);
      renderSessionsList();
      loadActiveSessionMessages();
      closeDrawer();
    });

    item.addEventListener("keydown", (e) => {
      if (e.target !== item) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setActiveSessionId(session.id);
        renderSessionsList();
        loadActiveSessionMessages();
        closeDrawer();
      }
    });

    sessionsList.appendChild(item);
  });
}

// Drawer Toggle Handlers
menuToggleBtn.addEventListener("click", async (e) => {
  e.stopPropagation();
  closeNotificationsDrawer();
  sessionsDrawer.classList.remove("hidden");
  sessionsDrawer.classList.add("active");
  drawerOverlay.classList.remove("hidden");
  renderSessionsList();
  if (sessionsSearchInput) {
    sessionsSearchInput.focus();
  }

  // Refresh sessions from backend so all existing sessions and titles are up to date
  try {
    const res = await fetch(`${API_BASE}/sessions`, {
      headers: getAuthHeaders(),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.sessions && Array.isArray(data.sessions)) {
        const storedTitles = getStoredSessionTitles();
        sessions = data.sessions.map(s => {
          const existing = sessions.find(e => e.id === s.id);
          const resolvedTitle = storedTitles[s.id]
            || (existing?.renamed && existing.title)
            || s.title
            || existing?.title
            || "New Chat";
          return {
            id: s.id,
            title: resolvedTitle,
            renamed: Boolean(storedTitles[s.id] || existing?.renamed),
            created_at: s.created_at,
            updated_at: s.updated_at,
            messages: existing?.messages || []
          };
        });
        renderSessionsList();
      }
    }
  } catch (err) {
    console.warn("Zocrates: Error refreshing sessions drawer:", err);
  }
});

function closeDrawer() {
  sessionsDrawer.classList.remove("active");
  sessionsDrawer.classList.add("hidden");
  closeNotificationsDrawer();
  drawerOverlay.classList.add("hidden");
}

closeDrawerBtn.addEventListener("click", closeDrawer);
drawerOverlay.addEventListener("click", closeDrawer);

// New Chat Button
newChatBtn.addEventListener("click", () => {
  createNewSession("New Chat");
  closeDrawer();
});

// Sessions Search Input Handler
const searchInputEl = document.getElementById("sessions-search-input") || sessionsSearchInput;
if (searchInputEl) {
  searchInputEl.addEventListener("input", () => {
    renderSessionsList();
  });
  searchInputEl.addEventListener("keyup", () => {
    renderSessionsList();
  });
}

// Notifications Drawer Handlers
notificationToggleBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  sessionsDrawer.classList.remove("active");
  sessionsDrawer.classList.add("hidden");
  
  notificationsDrawer.classList.remove("hidden");
  notificationsDrawer.classList.add("active");
  drawerOverlay.classList.remove("hidden");
  
  notificationBadge.classList.add("hidden");
  renderNotifications();
});

function closeNotificationsDrawer() {
  notificationsDrawer.classList.remove("active");
  notificationsDrawer.classList.add("hidden");
}

closeNotificationsBtn.addEventListener("click", () => {
  closeNotificationsDrawer();
  drawerOverlay.classList.add("hidden");
});

clearNotificationsBtn.addEventListener("click", clearNotifications);

// Dropdown Toggle Handlers
moreActionsBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const isHidden = moreActionsDropdown.classList.toggle("hidden");
  if (isHidden) {
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
  }
});

document.addEventListener("click", (e) => {
  if (!moreActionsBtn.contains(e.target) && !moreActionsDropdown.contains(e.target)) {
    moreActionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
  }
});

// ---------------------------------------------------------------------------
// Notifications & Batch Progress Logic
// ---------------------------------------------------------------------------
let notifications = [];
let currentBatchJob = null;

function renderBatchProgress(job) {
  currentBatchJob = job;
  const container = document.getElementById("batch-progress-container");
  if (!container) return;

  if (!job) {
    container.innerHTML = "";
    container.classList.add("hidden");
    return;
  }

  container.classList.remove("hidden");
  container.innerHTML = "";

  const card = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  card.className = "batch-job-card";

  const header = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  header.className = "batch-job-header";

  const isSingle = !!(job.isSingleItem || job.total === 1);

  const titleSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  titleSpan.className = "batch-job-title";
  titleSpan.textContent = isSingle
    ? `⚡ Converting: ${job.converterLabel || "Document"}`
    : `⚡ Batch: ${job.converterLabel || "Conversion"}`;
  header.appendChild(titleSpan);

  const statusBadge = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
  statusBadge.className = `batch-job-badge status-${job.status}`;
  if (job.status === "completed") {
    statusBadge.textContent = "Completed";
  } else if (job.isStopping) {
    statusBadge.textContent = "Stopping...";
  } else if (job.status === "stopped") {
    statusBadge.textContent = "Stopped";
  } else if (job.status === "error") {
    statusBadge.textContent = "Error";
  } else {
    statusBadge.textContent = "Active";
  }
  header.appendChild(statusBadge);
  card.appendChild(header);

  // Progress Bar
  const total = job.total || 1;
  let pct = 0;
  if (job.status === "completed") {
    pct = 100;
  } else if (isSingle && job.status === "converting") {
    pct = 50;
  } else if (isSingle && job.status === "indexing") {
    pct = 75;
  } else if (job.status === "converting") {
    pct = Math.round((((job.currentIndex || 1) - 1) / total) * 100);
  } else if (job.status === "indexing") {
    pct = Math.round((((job.currentIndex || 1) - 0.5) / total) * 100);
  } else {
    pct = Math.round((((job.processed || 0) + (job.skipped || 0)) / total) * 100);
  }
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;

  const track = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  track.className = "batch-progress-track";

  const fill = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  fill.className = "batch-progress-bar";
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  card.appendChild(track);

  // Status detail text
  const detail = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  detail.className = "batch-job-detail";
  
  if (job.status === "completed") {
    detail.textContent = isSingle
      ? `Finished: ${job.currentTitle || "document"}`
      : `Finished ${job.processed || 0} doc(s) (${job.skipped || 0} skipped, ${job.failed || 0} failed).`;
  } else if (job.status === "stopped") {
    detail.textContent = isSingle
      ? `Stopped by user: ${job.currentTitle || "document"}`
      : `Stopped by user. (${job.processed || 0}/${total} converted)`;
  } else if (job.status === "error") {
    detail.textContent = `Error: ${job.error || (isSingle ? "Conversion failed" : "Batch failed")}`;
  } else {
    let prefix = job.status === "converting" ? "Converting" : (job.status === "indexing" ? "Indexing" : "Processing");
    let currentDoc = job.currentTitle || "document";
    detail.textContent = isSingle
      ? `${prefix}: ${currentDoc}`
      : `[${job.currentIndex || 1}/${total}] ${prefix}: ${currentDoc}`;
  }
  card.appendChild(detail);

  // Action Buttons row
  const actionsRow = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
  actionsRow.className = "batch-job-actions";

  if (job.status !== "completed" && job.status !== "stopped" && job.status !== "error") {
    if (typeof job.abort === "function") {
      const stopBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
      stopBtn.className = "batch-job-stop-btn";
      stopBtn.textContent = job.isStopping ? "Stopping..." : "⏹ Stop Conversion";
      stopBtn.disabled = !!job.isStopping;
      stopBtn.addEventListener("click", () => {
        const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
        if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
          zoteroGlobal.Zocrates.BatchProgress.stop();
        }
      });
      actionsRow.appendChild(stopBtn);
    }
  } else {
    const dismissBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    dismissBtn.className = "batch-job-dismiss-btn";
    dismissBtn.textContent = "Dismiss";
    dismissBtn.addEventListener("click", () => {
      const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
      if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
        zoteroGlobal.Zocrates.BatchProgress.clear();
      }
    });
    actionsRow.appendChild(dismissBtn);
  }

  if (actionsRow.childNodes.length > 0) {
    card.appendChild(actionsRow);
  }
  container.appendChild(card);

  // While running, keep notification badge lit
  if (job.status !== "completed" && job.status !== "stopped" && job.status !== "error") {
    if (notificationBadge) notificationBadge.classList.remove("hidden");
  }
}

function loadNotifications() {
  try {
    const data = localStorage.getItem("zocrates_notifications");
    notifications = data ? JSON.parse(data) : [];
  } catch (e) {
    notifications = [];
  }
  try {
    const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier && typeof zoteroGlobal.Zocrates.Notifier.getAll === "function") {
      let hubNotifs = zoteroGlobal.Zocrates.Notifier.getAll();
      if (Array.isArray(hubNotifs) && hubNotifs.length > 0) {
        let existingIds = new Set(notifications.map(n => n.id));
        for (let hn of hubNotifs) {
          if (!existingIds.has(hn.id)) {
            notifications.push(hn);
            existingIds.add(hn.id);
          }
        }
        notifications.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
        if (notifications.length > 20) notifications.length = 20;
        saveNotifications();
      }
    }
  } catch (e) {
    console.debug("Zocrates: Error syncing with Notifier.getAll():", e);
  }
  renderNotifications();
  if (notifications.length > 0 && (notificationsDrawer.classList.contains("hidden") || !notificationsDrawer.classList.contains("active"))) {
    notificationBadge.classList.remove("hidden");
  }
}

// Storage and Notifier event hooks
window.addEventListener("storage", (e) => {
  if (e.key === "zocrates_notifications") {
    loadNotifications();
  }
});

try {
  const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier) {
    zoteroGlobal.Zocrates.Notifier.subscribe((notif) => {
      if (notif && notif.id) {
        if (!notifications.some(n => n.id === notif.id)) {
          notifications.unshift(notif);
          if (notifications.length > 20) notifications.length = 20;
          saveNotifications();
        }
        renderNotifications();
        if (notificationsDrawer.classList.contains("hidden") || !notificationsDrawer.classList.contains("active")) {
          notificationBadge.classList.remove("hidden");
        }
      } else {
        loadNotifications();
        if (notifications.length === 0 && notificationBadge) {
          notificationBadge.classList.add("hidden");
        }
      }
    });
  }

  if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
    zoteroGlobal.Zocrates.BatchProgress.subscribe((job) => {
      renderBatchProgress(job);
    });
  }
} catch (e) {
  console.debug("Zocrates: Could not attach to Notifier or BatchProgress hub:", e);
}

function saveNotifications() {
  try {
    localStorage.setItem("zocrates_notifications", JSON.stringify(notifications));
  } catch (e) {}
}

function addNotification(text, type = "info") {
  try {
    const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier && typeof zoteroGlobal.Zocrates.Notifier.add === "function") {
      return zoteroGlobal.Zocrates.Notifier.add(text, type);
    }
  } catch (e) {}

  const notif = {
    id: "notif_" + Date.now() + "_" + Math.random().toString(36).substring(2, 6),
    text: String(text),
    type: type,
    timestamp: Date.now()
  };
  notifications.unshift(notif); // Newest first
  if (notifications.length > 20) notifications.length = 20; // M1: Cap at 20
  saveNotifications();
  renderNotifications();
  
  if (notificationsDrawer.classList.contains("hidden") || !notificationsDrawer.classList.contains("active")) {
    notificationBadge.classList.remove("hidden");
  }
  return notif.id;
}

function deleteNotification(id) {
  notifications = notifications.filter(n => n.id !== id);
  saveNotifications();
  try {
    const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier && typeof zoteroGlobal.Zocrates.Notifier.delete === "function") {
      zoteroGlobal.Zocrates.Notifier.delete(id);
    }
  } catch (e) {}
  renderNotifications();
  if (notifications.length === 0) {
    notificationBadge.classList.add("hidden");
  }
}

function renderNotifications() {
  notificationsList.innerHTML = "";
  if (notifications.length === 0) {
    const empty = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    empty.className = "no-notifications";
    empty.textContent = "No notifications yet";
    notificationsList.appendChild(empty);
    return;
  }

  notifications.forEach(notif => {
    const div = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    div.className = "notification-item" + (notif.type ? ` notification-${notif.type}` : "");
    
    const date = new Date(notif.timestamp);
    const timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const dateStr = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
    
    // C1: Use textContent for notification text to prevent XSS
    const headerDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    headerDiv.className = "notification-header";
    const timeSpan = document.createElementNS("http://www.w3.org/1999/xhtml", "span");
    timeSpan.className = "notification-time";
    timeSpan.textContent = dateStr + " " + timeStr;
    headerDiv.appendChild(timeSpan);

    const actionsDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    actionsDiv.className = "notification-actions";

    const copyBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    copyBtn.className = "notification-copy-btn";
    copyBtn.title = "Copy notification";
    copyBtn.setAttribute("title", "Copy notification");
    copyBtn.innerHTML = `
      <svg class="copy-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
      <svg class="check-icon hidden" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" style="color: #22c55e;"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
    `;
    copyBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const copied = await copyTextToClipboard(notif.text);
        if (!copied) return;
        const copyIcon = copyBtn.querySelector(".copy-icon");
        const checkIcon = copyBtn.querySelector(".check-icon");
        if (copyIcon && checkIcon) {
          copyIcon.classList.add("hidden");
          checkIcon.classList.remove("hidden");
          copyBtn.title = "Copied!";
          copyBtn.setAttribute("title", "Copied!");
          setTimeout(() => {
            copyIcon.classList.remove("hidden");
            checkIcon.classList.add("hidden");
            copyBtn.title = "Copy notification";
            copyBtn.setAttribute("title", "Copy notification");
          }, 1500);
        }
      } catch (err) {
        if (typeof console !== "undefined" && typeof console.warn === "function") {
          console.warn("Zocrates: Failed to copy notification text:", err);
        }
      }
    });
    actionsDiv.appendChild(copyBtn);

    const deleteBtn = document.createElementNS("http://www.w3.org/1999/xhtml", "button");
    deleteBtn.className = "notification-delete-btn";
    deleteBtn.textContent = "✕";
    deleteBtn.title = "Delete notification";
    deleteBtn.setAttribute("title", "Delete notification");
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteNotification(notif.id);
    });
    actionsDiv.appendChild(deleteBtn);

    headerDiv.appendChild(actionsDiv);

    div.appendChild(headerDiv);

    const textDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    textDiv.className = "notification-text";
    textDiv.textContent = notif.text;
    div.appendChild(textDiv);
    notificationsList.appendChild(div);
  });
}

function clearNotifications() {
  notifications = [];
  saveNotifications();
  try {
    const zoteroGlobal = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier && typeof zoteroGlobal.Zocrates.Notifier.clear === "function") {
      zoteroGlobal.Zocrates.Notifier.clear();
    }
  } catch (e) {}
  renderNotifications();
  notificationBadge.classList.add("hidden");
}

// Library Conversion Status Menu Item Handler
const libConversionStatusMenuBtn = document.getElementById("library-conversion-status-menu-btn");
if (libConversionStatusMenuBtn) {
  libConversionStatusMenuBtn.addEventListener("click", () => {
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    const zoteroGlobal = resolveZotero();
    if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchConverter) {
      zoteroGlobal.Zocrates.BatchConverter.checkLibraryConversionStatus(window);
    }
  });
}

// Paper Relations Network Graph Visualizer Launcher
function openNetworkGraphTab(customParams = null) {
  const paperChips = activeItemsList.filter(item => (item.type === "item" || item.type === "paper" || !item.type) && item.key);
  const collectionChips = activeItemsList.filter(item => item.type === "collection");

  const queryParams = new URLSearchParams();
  if (customParams && customParams.center_keys) {
    queryParams.set("center_keys", customParams.center_keys);
    if (customParams.depth) queryParams.set("depth", String(customParams.depth));
  } else if (customParams && customParams.center_key) {
    queryParams.set("center_key", customParams.center_key);
    if (customParams.depth) queryParams.set("depth", String(customParams.depth));
  } else if (customParams && customParams.collection_keys) {
    queryParams.set("collection_keys", customParams.collection_keys);
  } else if (customParams && customParams.collection_key) {
    queryParams.set("collection_key", customParams.collection_key);
  } else if (paperChips.length === 1) {
    queryParams.set("center_key", paperChips[0].key);
    queryParams.set("depth", "2");
  } else if (paperChips.length > 1) {
    queryParams.set("center_keys", paperChips.map(p => p.key).join(","));
    queryParams.set("depth", "1");
  } else if (collectionChips.length >= 1) {
    queryParams.set("collection_keys", collectionChips.map(c => c.key).join(","));
  }

  const queryString = queryParams.toString();
  const graphUrl = `chrome://zocrates/content/graph.xhtml${queryString ? "?" + queryString : ""}`;

  try {
    const zot = resolveZotero();
    let mainWin = null;
    if (zot && typeof zot.getMainWindow === "function") {
      mainWin = zot.getMainWindow();
    }
    if (!mainWin) {
      mainWin = window.parent?.Zotero ? window.parent : (window.top?.Zotero ? window.top : null);
    }
    if (!mainWin && typeof Services !== "undefined" && Services.wm) {
      mainWin = Services.wm.getMostRecentWindow("navigator:browser") || Services.wm.getMostRecentWindow("zotero:main");
    }

    if (mainWin && mainWin.Zotero_Tabs && typeof mainWin.Zotero_Tabs.add === "function") {
      try {
        if (!mainWin.Zotero_Tabs.tabHooks) {
          mainWin.Zotero_Tabs.tabHooks = {};
        }
        if (!mainWin.Zotero_Tabs.tabHooks.restoreState) {
          mainWin.Zotero_Tabs.tabHooks.restoreState = {};
        }
        if (!mainWin.Zotero_Tabs.tabHooks.restoreState["zocrates_graph"]) {
          mainWin.Zotero_Tabs.tabHooks.restoreState["zocrates_graph"] = async (tab) => {
            let attempts = 0;
            const pollContainer = () => {
              try {
                if (!mainWin || mainWin.closed) return;
                let container = mainWin.document.getElementById(tab.id);
                if (container) {
                  let url = tab.data?.url || "chrome://zocrates/content/graph.xhtml";
                  let iframe = container.querySelector("iframe");
                  if (!iframe) {
                    iframe = mainWin.document.createElement("iframe");
                    iframe.setAttribute("src", url);
                    iframe.setAttribute("flex", "1");
                    iframe.style.border = "none";
                    iframe.style.width = "100%";
                    iframe.style.height = "100%";
                    iframe.style.flex = "1";
                    container.style.width = "100%";
                    container.style.height = "100%";
                    container.style.display = "flex";
                    container.style.flexDirection = "column";
                    container.appendChild(iframe);
                  }
                } else if (++attempts < 10) {
                  mainWin.setTimeout(pollContainer, 200);
                }
              } catch (e) {
                console.warn("Zocrates: network graph restore error:", e);
              }
            };
            mainWin.setTimeout(pollContainer, 100);
            return { itemID: null };
          };
        }

        const existingTab = Array.isArray(mainWin.Zotero_Tabs._tabs)
          ? mainWin.Zotero_Tabs._tabs.find(t => t && (t.id === "zocrates-network-graph-tab" || t.type === "zocrates_graph"))
          : null;

        if (existingTab) {
          mainWin.Zotero_Tabs.select(existingTab.id);
          existingTab.data = { url: graphUrl };
          const container = mainWin.document.getElementById(existingTab.id);
          const iframe = container ? container.querySelector("iframe") : null;
          if (iframe) {
            try {
              if (typeof iframe.contentWindow?.loadGraph === "function") {
                iframe.contentWindow.loadGraph(Object.fromEntries(queryParams));
              } else {
                iframe.src = graphUrl;
              }
            } catch (frameErr) {
              iframe.src = graphUrl;
            }
          } else if (container) {
            const newIframe = mainWin.document.createElement("iframe");
            newIframe.setAttribute("src", graphUrl);
            newIframe.setAttribute("flex", "1");
            newIframe.style.border = "none";
            newIframe.style.width = "100%";
            newIframe.style.height = "100%";
            newIframe.style.flex = "1";
            container.style.width = "100%";
            container.style.height = "100%";
            container.style.display = "flex";
            container.style.flexDirection = "column";
            container.appendChild(newIframe);
          }
          return;
        }

        const tabResult = mainWin.Zotero_Tabs.add({
          id: "zocrates-network-graph-tab",
          type: "zocrates_graph",
          title: "Network Graph",
          data: { url: graphUrl },
          select: true,
        });
        const container = tabResult?.container || mainWin.document.getElementById("zocrates-network-graph-tab");
        if (container) {
          const iframe = mainWin.document.createElement("iframe");
          iframe.setAttribute("src", graphUrl);
          iframe.setAttribute("flex", "1");
          iframe.style.border = "none";
          iframe.style.width = "100%";
          iframe.style.height = "100%";
          iframe.style.flex = "1";
          container.style.width = "100%";
          container.style.height = "100%";
          container.style.display = "flex";
          container.style.flexDirection = "column";
          container.appendChild(iframe);
          return;
        } else {
          console.warn("Zocrates: Zotero_Tabs.add succeeded but container not found");
          return;
        }
      } catch (tabErr) {
        console.warn("Zocrates: Zotero_Tabs.add failed, falling back to launchURL:", tabErr);
      }
    }
    const token = getAuthToken();
    const tokenParam = token ? `token=${encodeURIComponent(token)}` : "";
    const fullQuery = [queryString, tokenParam].filter(Boolean).join("&");
    if (zot && typeof zot.launchURL === "function") {
      const externalUrl = `${BACKEND_URL}/graph/view${fullQuery ? "?" + fullQuery : ""}`;
      zot.launchURL(externalUrl);
      return;
    }
  } catch (err) {
    console.warn("Zocrates: Error opening network graph in Zotero_Tabs:", err);
  }

  const token = getAuthToken();
  const tokenParam = token ? `token=${encodeURIComponent(token)}` : "";
  const fullQuery = [queryString, tokenParam].filter(Boolean).join("&");
  const externalUrl = `${BACKEND_URL}/graph/view${fullQuery ? "?" + fullQuery : ""}`;
  window.open(externalUrl, "_blank");
}

const networkGraphMenuBtn = document.getElementById("network-graph-menu-btn");
if (networkGraphMenuBtn) {
  networkGraphMenuBtn.addEventListener("click", () => {
    if (moreActionsDropdown) moreActionsDropdown.classList.add("hidden");
    if (themeOptionsDropdown) themeOptionsDropdown.classList.add("hidden");
    if (fontSizeOptionsDropdown) fontSizeOptionsDropdown.classList.add("hidden");
    if (aboutOptionsDropdown) aboutOptionsDropdown.classList.add("hidden");
    openNetworkGraphTab();
  });
}

function snapshotDetachedState() {
  return {
    sessionId: currentSessionId,
    contextMode,
    isStickyMode,
    theme: currentThemeSetting,
    fontSize: currentFontSizeSetting,
    activeItemsList: activeItemsList ? [...activeItemsList] : [],
    activeSnippetsList: activeSnippetsList ? [...activeSnippetsList] : [],
    inputDraft: chatInput ? chatInput.value : ""
  };
}

if (windowToggleBtn) {
  if (isDetached) {
    // Show attach icon, hide detach icon
    detachIcon.classList.add("hidden");
    attachIcon.classList.remove("hidden");
    windowToggleBtn.title = "Reattach to Zotero";
  } else {
    // Show detach icon, hide attach icon
    detachIcon.classList.remove("hidden");
    attachIcon.classList.add("hidden");
    windowToggleBtn.title = "Detach Sidebar";
  }

  windowToggleBtn.addEventListener("click", () => {
    if (isDetached) {
      // Reattach: Restore embedded sidebar, and close standalone window
      try {
        const zot = resolveZotero();
        if (zot?.Zocrates) {
          zot.Zocrates._activeSessionId = currentSessionId;
          delete zot.Zocrates._detachedInitialState;
        }

        let wm = Components.classes["@mozilla.org/appshell/window-mediator;1"]
          .getService(Components.interfaces.nsIWindowMediator);
        let mainWin = wm.getMostRecentWindow("navigator:browser");
        if (mainWin) {
          let sidebar = mainWin.document.getElementById("zocrates-sidebar");
          let splitter = mainWin.document.getElementById("zocrates-splitter");
          let toggleBtn = mainWin.document.getElementById("zocrates-toolbar-btn");
          if (sidebar) {
            sidebar.hidden = false;
            if (splitter) splitter.hidden = false;
            if (toggleBtn) toggleBtn.setAttribute("data-active", "true");

            let iframe = sidebar.querySelector("#zocrates-iframe") || sidebar.querySelector("iframe");
            if (iframe && iframe.contentWindow && typeof iframe.contentWindow.syncFromDetachedState === "function") {
              iframe.contentWindow.syncFromDetachedState(snapshotDetachedState());
            }
          }
        }
      } catch (err) {
        console.error("Zocrates: failed to restore sidebar:", err);
      }
      window.close();
    } else {
      // Detach: Open standalone window, and hide embedded sidebar
      try {
        const doc = window.parent?.document;
        const sidebar = doc?.getElementById("zocrates-sidebar");
        let targetWidth = 450;
        let targetHeight = 750;
        if (sidebar) {
          const rect = sidebar.getBoundingClientRect();
          if (rect.width > 200) targetWidth = Math.round(rect.width);
          if (rect.height > 200) targetHeight = Math.round(rect.height);
        } else if (window.innerWidth && window.innerHeight) {
          if (window.innerWidth > 200) targetWidth = Math.round(window.innerWidth);
          if (window.innerHeight > 200) targetHeight = Math.round(window.innerHeight);
        }

        const activeSessionTarget = currentSessionId || resolveActiveSessionId();
        setActiveSessionId(activeSessionTarget);

        const detachPayload = {
          initialSessionId: activeSessionTarget,
          initialWidth: targetWidth,
          initialHeight: targetHeight,
          theme: currentThemeSetting,
          activeItemsList: activeItemsList ? [...activeItemsList] : [],
          activeSnippetsList: activeSnippetsList ? [...activeSnippetsList] : [],
          contextMode,
          isStickyMode,
          inputDraft: chatInput ? chatInput.value : ""
        };

        const zot = resolveZotero();
        if (zot?.Zocrates) {
          zot.Zocrates._activeSessionId = activeSessionTarget;
          zot.Zocrates._detachedInitialState = {
            currentSessionId: activeSessionTarget,
            initialWidth: targetWidth,
            initialHeight: targetHeight,
            theme: currentThemeSetting,
            activeItemsList: activeItemsList ? [...activeItemsList] : [],
            activeSnippetsList: activeSnippetsList ? [...activeSnippetsList] : [],
            contextMode,
            isStickyMode,
            inputDraft: chatInput ? chatInput.value : ""
          };
        }

        const openFn = (window.parent && typeof window.parent.openDialog === "function")
          ? window.parent.openDialog.bind(window.parent)
          : (window.openDialog ? window.openDialog.bind(window) : (window.parent?.open || window.open).bind(window.parent || window));

        const features = `chrome,width=${targetWidth},height=${targetHeight},resizable,centerscreen`;

        let win = openFn(
          "chrome://zocrates/content/sidebar.html",
          "zocrates-detached",
          features,
          detachPayload
        );
        if (win) {
          win.isZocratesDetached = true;
        }

        const splitter = doc?.getElementById("zocrates-splitter");
        const toggleBtn = doc?.getElementById("zocrates-toolbar-btn");
        if (sidebar) {
          sidebar.hidden = true;
          if (splitter) splitter.hidden = true;
          if (toggleBtn) toggleBtn.setAttribute("data-active", "true");
        }
        syncPollingWithActivity({ triggerImmediate: false });
      } catch (err) {
        console.error("Zocrates: failed to open detached window:", err);
        addNotification("Failed to detach window: " + err.message);
      }
    }
  });
}

function syncFromDetachedState(state) {
  if (!state) return;
  const VALID_THEMES = ["auto", "slate", "amoled", "sepia"];
  if (state.theme && VALID_THEMES.includes(state.theme)) {
    if (state.theme !== currentThemeSetting || !document.body || !document.body.getAttribute("data-theme")) {
      currentThemeSetting = state.theme;
      try {
        localStorage.setItem("zocrates_theme", currentThemeSetting);
      } catch (e) {}
      applyTheme();
    }
  } else {
    loadSavedTheme();
  }
  const VALID_FONT_SIZES = ["small", "default", "large", "xlarge"];
  if (state.fontSize && VALID_FONT_SIZES.includes(state.fontSize)) {
    if (state.fontSize !== currentFontSizeSetting || !document.body || !document.body.getAttribute("data-font-size")) {
      currentFontSizeSetting = state.fontSize;
      try {
        localStorage.setItem("zocrates_font_size", currentFontSizeSetting);
      } catch (e) {}
      applyFontSize();
    }
  } else {
    loadSavedFontSize();
  }
  if (state.sessionId) {
    setActiveSessionId(state.sessionId);
    renderSessionsList();
    if (typeof loadActiveSessionMessages === "function") {
      try {
        const p = loadActiveSessionMessages();
        if (p && typeof p.catch === "function") {
          p.catch(() => {});
        }
      } catch (e) {}
    }
  }
  if (state.contextMode) contextMode = state.contextMode;
  if (state.isStickyMode !== undefined) {
    isStickyMode = !!state.isStickyMode;
    if (toggleStickyBtn) {
      if (isStickyMode) {
        toggleStickyBtn.classList.add("active");
        toggleStickyBtn.setAttribute("title", "Unpin context (sticky mode active)");
      } else {
        toggleStickyBtn.classList.remove("active");
        toggleStickyBtn.setAttribute("title", "Sticky mode (pin selected items/papers)");
      }
    }
  }
  if (Array.isArray(state.activeItemsList)) {
    activeItemsList = [...state.activeItemsList];
    renderActiveChips();
  }
  if (Array.isArray(state.activeSnippetsList)) {
    activeSnippetsList = [...state.activeSnippetsList];
    renderActiveChips();
  }
  if (state.inputDraft !== undefined && chatInput) {
    chatInput.value = state.inputDraft;
  }
  syncPollingWithActivity({ triggerImmediate: false });
}
window.syncFromDetachedState = syncFromDetachedState;

window.addEventListener("focus", () => {
  loadSavedTheme();
  loadSavedFontSize();
  if (!isDetached) {
    try {
      const zot = resolveZotero();
      const activeId = zot?.Zocrates?._activeSessionId;
      const needsRefresh = zot?.Zocrates?._detachedNeedsRefresh || zot?.Zocrates?._detachedState || (activeId && activeId !== currentSessionId);
      if (needsRefresh) {
        if (zot?.Zocrates) {
          zot.Zocrates._detachedNeedsRefresh = false;
        }
        if (zot?.Zocrates?._detachedState) {
          const detachedState = zot.Zocrates._detachedState;
          delete zot.Zocrates._detachedState;
          if (typeof window.syncFromDetachedState === "function") {
            window.syncFromDetachedState(detachedState);
          }
        } else {
          if (activeId) {
            setActiveSessionId(activeId);
          }
          renderSessionsList();
          loadActiveSessionMessages();
        }
      }
    } catch (e) {}
  }
});

// Register Zotero Notifier observer to keep collections list updated
let collectionsNotifierID = null;
try {
  if (Zotero && Zotero.Notifier) {
    const collectionsObserver = {
      notify: function (event, type, ids, extraData) {
        if (type === 'collection') {
          console.log("Zocrates: collection change detected via notifier:", event, ids);
          populateCollectionsSubmenu();

          // If the currently active collection was deleted, fall back to papers mode
          if (event === 'delete' && contextMode === "collection") {
            const activeColChip = activeItemsList.find(p => p.type === "collection");
            if (activeColChip) {
              const pane = resolveZoteroPane();
              const libraryID = resolveActiveLibraryID(pane);
              const collections = fetchAllCollectionsInLibrary(libraryID);
              const exists = collections.some(col => "Collection: " + col.name === activeColChip.title);
              if (!exists) {
                console.log("Zocrates: Active collection deleted, reverting to papers mode");
                contextMode = "papers";
                lastActiveKeysStr = "";
                const items = readSelectedItemsNow();
                applySelection(items, true);
                addNotification("The active collection context was deleted. Reverted to papers mode.");
              }
            }
          }
        }
      }
    };
    collectionsNotifierID = Zotero.Notifier.registerObserver(collectionsObserver, ['collection'], 'Zocrates', 10);
    console.log("Zocrates: Registered observer for collection events");
  }
} catch (e) {
  console.error("Zocrates: Failed to register collection observer:", e);
}

function handleBeforeUnload() {
  cleanupActivityListeners();
  if (isDetached) {
    try {
      const zot = resolveZotero();
      if (zot?.Zocrates) {
        zot.Zocrates._detachedNeedsRefresh = true;
        zot.Zocrates._activeSessionId = currentSessionId;
        zot.Zocrates._detachedState = snapshotDetachedState();
      }
    } catch (e) {}
  }
}

window.addEventListener("beforeunload", handleBeforeUnload);

// Clean up on unload (H5, H13)
window.addEventListener("unload", () => {
  cleanupActivityListeners();
  if (isDetached) {
    try {
      const zot = resolveZotero();
      if (zot?.Zocrates) {
        zot.Zocrates._activeSessionId = currentSessionId;
        zot.Zocrates._detachedNeedsRefresh = true;
        if (!zot.Zocrates._detachedState) {
          zot.Zocrates._detachedState = snapshotDetachedState();
        }
      }
    } catch (e) {}
  }

  // Onboarding lifecycle cleanup (plan §9): aborts any open SSE reader and
  // clears onboarding DOM. NO-OP while onboarding is active (the dedicated
  // #onboarding-container cards must survive session loads); this only tidies
  // stale cards once the flow has resolved.
  cleanupOnboardingResources();

  // Clean up collection notifier
  if (collectionsNotifierID) {
    const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
    if (zot && zot.Notifier) {
      try {
        zot.Notifier.unregisterObserver(collectionsNotifierID);
        console.log("Zocrates: Unregistered collection observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister collection observer:", e);
      }
    }
  }

  // H5: Clean up preferences observers
  const zot = resolveZotero() || (typeof Zotero !== "undefined" ? Zotero : null);
  if (zot && zot.Prefs) {
    if (themePrefsObserverId) {
      try {
        zot.Prefs.unregisterObserver(themePrefsObserverId);
        console.log("Zocrates: Unregistered theme prefs observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister theme observer:", e);
      }
    }
    if (zocratesThemeObserverId) {
      try {
        zot.Prefs.unregisterObserver(zocratesThemeObserverId);
        console.log("Zocrates: Unregistered zocrates theme observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister zocrates theme observer:", e);
      }
    }
    if (zocratesFontSizeObserverId) {
      try {
        zot.Prefs.unregisterObserver(zocratesFontSizeObserverId);
        console.log("Zocrates: Unregistered zocrates font size observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister zocrates font size observer:", e);
      }
    }
    if (llmPrefsObserverId) {
      try {
        zot.Prefs.unregisterObserver(llmPrefsObserverId);
        console.log("Zocrates: Unregistered llm model observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister llm model observer:", e);
      }
    }
    if (llmTimestampObserverId) {
      try {
        zot.Prefs.unregisterObserver(llmTimestampObserverId);
        console.log("Zocrates: Unregistered llm timestamp observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister llm timestamp observer:", e);
      }
    }
    if (autoRenderMermaidObserverId) {
      try {
        zot.Prefs.unregisterObserver(autoRenderMermaidObserverId);
        console.log("Zocrates: Unregistered autoRenderMermaid observer successfully");
      } catch(e) {
        console.error("Zocrates: Failed to unregister autoRenderMermaid observer:", e);
      }
    }
  }

  // H13: Clean up tree click listener on parent window
  if (_treeClickHandler) {
    try {
      const parentDoc = window.parent?.document;
      if (parentDoc) {
        const treeEl = parentDoc.getElementById("zotero-items-tree");
        if (treeEl) treeEl.removeEventListener("click", _treeClickHandler);
      }
      console.log("Zocrates: Removed tree click listener successfully");
    } catch(e) {
      console.error("Zocrates: Failed to remove tree click listener:", e);
    }
  }
});

function restoreDetachedInitialState() {
  if (!isDetached) return;
  try {
    const zot = resolveZotero();
    const st = (window.arguments && window.arguments[0]) || zot?.Zocrates?._detachedInitialState;
    if (st) {
      if (st.initialWidth && st.initialHeight) {
        try {
          window.resizeTo(st.initialWidth, st.initialHeight);
        } catch (e) {}
      }
      const targetSid = st.initialSessionId || st.currentSessionId;
      if (targetSid) {
        setActiveSessionId(targetSid);
        clearOnboardingContainerCards();
      }
      if (st.theme) {
        currentThemeSetting = st.theme;
        try {
          localStorage.setItem("zocrates_theme", currentThemeSetting);
        } catch (e) {}
        applyTheme();
      }
      if (st.fontSize) {
        currentFontSizeSetting = st.fontSize;
        try {
          localStorage.setItem("zocrates_font_size", currentFontSizeSetting);
        } catch (e) {}
        applyFontSize();
      }
      if (st.contextMode) contextMode = st.contextMode;
      if (st.isStickyMode !== undefined) {
        isStickyMode = !!st.isStickyMode;
        if (toggleStickyBtn) {
          if (isStickyMode) {
            toggleStickyBtn.classList.add("active");
            toggleStickyBtn.setAttribute("title", "Unpin context (sticky mode active)");
          } else {
            toggleStickyBtn.classList.remove("active");
            toggleStickyBtn.setAttribute("title", "Sticky mode (pin selected items/papers)");
          }
        }
      }
      if (Array.isArray(st.activeItemsList) && st.activeItemsList.length > 0) {
        activeItemsList = [...st.activeItemsList];
        renderActiveChips();
      }
      if (Array.isArray(st.activeSnippetsList) && st.activeSnippetsList.length > 0) {
        activeSnippetsList = [...st.activeSnippetsList];
        renderActiveChips();
      }
      if (st.inputDraft && chatInput) {
        chatInput.value = st.inputDraft;
      }
    }
  } catch (e) {
    console.warn("Zocrates: Error restoring detached state:", e);
  }
}

// Initializations
if (isDetached) {
  restoreDetachedInitialState();
}

if (typeof module === "undefined") {
  installActivityListeners();
  _backendPollingEnabled = true;
  loadSessions();
  loadNotifications();
  checkBackendStatus();
  // Phase 7 onboarding: mount-time state read (restart recovery / reload
  // parity). Replay and subscribe the multi-window stage bus AFTER the
  // read so the process-wide snapshot is the authoritative seed.
  initOnboardingStateFlow().then(() => {
    subscribeOnboardingStageBus();
  }).catch((e) => {
    console.warn("Zocrates: Error during onboarding state init:", e);
    subscribeOnboardingStageBus();
  });
}

// ---------------------------------------------------------------------------
// Keyboard Navigation for dropdown menus
// ---------------------------------------------------------------------------
let focusedMenuType = "main"; // "main" or "submenu"
let focusedMenuIndex = -1;

function clearMenuFocus() {
  const allItems = document.querySelectorAll(".dropdown-item");
  allItems.forEach(item => item.classList.remove("keyboard-focused"));
}

function getVisibleMenuItems() {
  const items = [
    document.getElementById("add-current-view-option"),
    document.getElementById("add-selected-papers-option"),
    document.getElementById("add-collection-option"),
    document.getElementById("add-library-option")
  ].filter(el => el && el.style.display !== "none");
  return items;
}

function getSubmenuItems() {
  if (!collectionsSubmenu || collectionsSubmenu.classList.contains("hidden")) {
    return [];
  }
  return Array.from(collectionsSubmenu.querySelectorAll(".dropdown-item"));
}

function updateMenuFocus(items) {
  clearMenuFocus();
  const target = items[focusedMenuIndex];
  if (target) {
    target.classList.add("keyboard-focused");
    target.scrollIntoView({ block: "nearest" });
  }
}

function handleMenuKeydown(e) {
  const isMainMenuOpen = !contextOptionsDropdown.classList.contains("hidden");
  if (!isMainMenuOpen) return;

  const key = e.key;
  if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Enter", "Escape"].includes(key)) {
    return;
  }

  e.preventDefault();

  const mainItems = getVisibleMenuItems();
  const submenuItems = getSubmenuItems();

  if (key === "Escape") {
    contextOptionsDropdown.classList.add("hidden");
    if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
    clearMenuFocus();
    focusedMenuType = "main";
    focusedMenuIndex = -1;
    chatInput.focus();
    return;
  }

  if (focusedMenuType === "main") {
    if (key === "ArrowDown") {
      focusedMenuIndex = (focusedMenuIndex + 1) % mainItems.length;
      updateMenuFocus(mainItems);
    } else if (key === "ArrowUp") {
      if (focusedMenuIndex <= 0) {
        focusedMenuIndex = mainItems.length - 1;
      } else {
        focusedMenuIndex--;
      }
      updateMenuFocus(mainItems);
    } else if (key === "ArrowRight" || key === "Enter") {
      const activeItem = mainItems[focusedMenuIndex];
      if (activeItem && activeItem.id === "add-collection-option") {
        if (collectionsSubmenu) {
          collectionsSubmenu.classList.remove("hidden");
          populateCollectionsSubmenu();
          const nextItems = getSubmenuItems();
          if (nextItems.length > 0) {
            focusedMenuType = "submenu";
            focusedMenuIndex = 0;
            updateMenuFocus(nextItems);
          }
        }
      } else if (key === "Enter" && activeItem) {
        activeItem.click();
      }
    }
  } else if (focusedMenuType === "submenu") {
    if (key === "ArrowDown") {
      focusedMenuIndex = (focusedMenuIndex + 1) % submenuItems.length;
      updateMenuFocus(submenuItems);
    } else if (key === "ArrowUp") {
      if (focusedMenuIndex <= 0) {
        focusedMenuIndex = submenuItems.length - 1;
      } else {
        focusedMenuIndex--;
      }
      updateMenuFocus(submenuItems);
    } else if (key === "ArrowLeft") {
      if (collectionsSubmenu) collectionsSubmenu.classList.add("hidden");
      focusedMenuType = "main";
      focusedMenuIndex = mainItems.findIndex(item => item.id === "add-collection-option");
      updateMenuFocus(mainItems);
    } else if (key === "Enter") {
      const activeItem = submenuItems[focusedMenuIndex];
      if (activeItem) {
        activeItem.click();
      }
    }
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && activeEvidencePopover) {
    closeEvidencePopover();
  }
  handleMenuKeydown(e);
});

// ---------------------------------------------------------------------------
// In-Text Citation Pills & Evidence Popover Event Delegation
// ---------------------------------------------------------------------------
document.addEventListener("click", (e) => {
  const pill = e.target.closest(".citation-pill");
  if (pill) {
    e.stopPropagation();
    const idx = parseInt(pill.dataset.citationIdx, 10);
    const wrapper = pill.closest(".message-wrapper");
    const msgDiv = wrapper?.querySelector(".message");
    const citations = msgDiv?._citations || wrapper?._citations;
    const citation = citations?.find(c => c.index === idx);
    if (citation) {
      showEvidencePopover(pill, citation);
    }
    return;
  }

  const sourceLink = e.target.closest(".zotero-source-link");
  if (sourceLink) {
    e.stopPropagation();
    const key = sourceLink.dataset.itemKey || sourceLink.getAttribute("data-item-key");
    selectItemInZotero(key);
    return;
  }

  const popoverReaderBtn = e.target.closest(".popover-open-reader-btn");
  if (popoverReaderBtn) {
    e.stopPropagation();
    const attKey = popoverReaderBtn.dataset.attachmentKey || popoverReaderBtn.getAttribute("data-attachment-key");
    const section = popoverReaderBtn.dataset.section || popoverReaderBtn.getAttribute("data-section");
    openItemInReader(attKey, section);
    return;
  }

  const popoverSelectBtn = e.target.closest(".popover-select-item-btn");
  if (popoverSelectBtn) {
    e.stopPropagation();
    const itemKey = popoverSelectBtn.dataset.itemKey || popoverSelectBtn.getAttribute("data-item-key");
    selectItemInZotero(itemKey);
    return;
  }

  if (activeEvidencePopover && !e.target.closest(".evidence-popover")) {
    if (activeEvidencePopover._isResizing || activeEvidencePopover._justResized) {
      return;
    }
    closeEvidencePopover();
  }
});

window.addEventListener("resize", closeEvidencePopover);
window.addEventListener("scroll", (e) => {
  if (activeEvidencePopover && e.target && (activeEvidencePopover === e.target || activeEvidencePopover.contains(e.target))) {
    return;
  }
  closeEvidencePopover();
}, true);

// Load theme and mode on startup
if (typeof module === "undefined") {
  loadSavedTheme();
  loadSavedFontSize();
  loadSavedMode();

  // Set up activity-gated selection checker polling at 400ms
  _selectionPollingEnabled = true;
  if (!isPollingPaused()) {
    checkActiveSelection();
    scheduleNextSelectionCheck();
  }
}

if (typeof window !== "undefined") {
  window.buildClaimsInspectorData = buildClaimsInspectorData;
  window.openClaimsInspectorWindow = openClaimsInspectorWindow;
  window.createClaimsInspectorBtn = createClaimsInspectorBtn;
  window.ensureClaimsInspectorBtn = ensureClaimsInspectorBtn;
  window.selectItemInLibrary = selectItemInLibrary;
  window.openItemInReader = openItemInReader;
  try {
    Object.defineProperty(window, "sessions", {
      get: () => sessions,
      set: (val) => { sessions = val; },
      configurable: true,
      enumerable: true
    });
  } catch (_) {
    window.sessions = sessions;
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    buildClaimsInspectorData,
    openClaimsInspectorWindow,
    createClaimsInspectorBtn,
    ensureClaimsInspectorBtn,
    selectItemInLibrary,
    sanitizeHTML,
    isSafeUrl,
    isSafeLinkUrl,
    sanitizeMermaidSvg,
    appendWelcomeMessage,
    hasMessageCitations,
    ensureCitationToggleBtn,
    populateCandidateCollectionSelect,
    renderCandidatePapersGroup,
    formatCandidateAuthors,
    formatCandidateMetaLine,
    getCandidateKey,
    selectedCandidateKeys,
    importedCandidateKeys,
    resolvePoliteEmail,
    loadStoredApiKey,
    saveStoredApiKey,
    autoRegister,
    formatMessageContent,
    syncCandidatesWithLibrary,
    setGhostSuggestion,
    clearGhostSuggestion,
    updateGhostVisibility,
    showEvidencePopover,
    closeEvidencePopover,
    extractLeafHeading,
    isSyntheticOrTitle,
    normalizeForComparison,
    parseHeadingParts,
    findOutlineDestination,
    findHtmlSnapshotHeading,
    openItemInReader,
    stripCitationsFromText,
    buildReferencesMarkdown,
    getMessageTextForCopy,
    copyTextToClipboard,
    mapItemToChipData,
    renderActiveChips,
    resolveActiveLibraryInfo,
    resolveSelectedCollections,
    renderHitlCheckpointCard,
    appendIntermediateStepsBubbleDOM,
    updateIntermediateStepsBubbleProgress,
    finalizeIntermediateStepsBubble,
    decorateMermaidCodeBlocks,
    calculateSessionStats,
    computeTurnTokens,
    formatCostUsd,
    extractMessageTitle,
    formatMarkdown,
    appendMessageDOM,
    formatMarkdownForNote,
    formatModelLabel,
    isInternalWorkflowNode,
    isIntermediateSearchStep,
    applySelection,
    handleIncomingSnippet,
    handleSnippetUpdate,
    getActiveSnippetsList: () => activeSnippetsList,
    setActiveSnippetsList: (list) => { activeSnippetsList = list; },
    getActiveItemsList: () => activeItemsList,
    setActiveItemsList: (list) => { activeItemsList = list; },
    getContextMode: () => contextMode,
    setContextMode: (mode) => { contextMode = mode; },
    finalizeChatStream,
    handleChatSubmit,
    getBackendReadyForChat: () => backendReadyForChat,
    setBackendReadyForChat: (v) => { backendReadyForChat = v; },
    removeBootstrapProgressCard,
    renderOrUpdateBootstrapProgressCard,
    handleBootstrapProgressEvent,
    handleOnboardingBootstrapDone,
    checkBackendStatus,
    loadSessions,
    setActiveSessionId,
    resolveActiveSessionId,
    createNewSession,
    streamDialogueResponse,
    openMessageInfoModal,
    openDiagnosticReportModal,
    sanitizeDiagnosticTextClient,
    buildOfflineDiagnosticMarkdown,
    saveDiagnosticReportToDisk,
    executeRerun,
    enableInlineEditing,
    normalizeRestoredActiveItem,
    normalizeRestoredSnippet,
    appendMessageDOM,
    renderNotifications,
    addNotification,
    deleteNotification,
    clearNotifications,
    getNotifications: () => notifications,
    setNotifications: (list) => { notifications = list; },
    getSessions: () => sessions,
    setSessions: (list) => { sessions = list; },
    getCurrentSessionId: () => currentSessionId,
    setCurrentSessionId: (sid) => { currentSessionId = sid; },
    // Phase 7 onboarding internals (test surface)
    ONBOARDING_PRESETS,
    mapPresetToWizardFields,
    buildOnboardingFinishPayloads,
    getEffectiveOnboardingStage,
    setOnboardingStage,
    isOnboardingActive,
    getOnboardingStateFilePath,
    onboardingVenvPresent,
    profileHasExistingArtifacts,
    readOnboardingStateFile,
    writeOnboardingState,
    initOnboardingStateFlow,
    handleOnboardingStageEvent,
    subscribeOnboardingStageBus,
    enterOnboardingConfigure,
    enterOnboardingProcess,
    testOnboardingConnection,
    readOnboardingSyncStream,
    finalizeFollowerAbortFallback: _finalizeFollowerAbortFallback,
    getOnboardingSyncGeneration: _currentOnboardingSyncGeneration,
    getOnboardingFlowState: () => ({
      finished: _onboardingFinished,
      streamActive: _onboardingStreamActive,
      abortIntent: _onboardingAbortIntent,
      abortFallbackTimer: _onboardingAbortFallbackTimer !== null,
    }),
    handleOnboardingSyncEvent,
    finishOnboardingProcessing,
    requestOnboardingAbort,
    cleanupOnboardingResources,
    ensureOnboardingLayaReady,
    updateOnboardingProcessProgress,
    getOnboardingProcessCardRefs: () => _onboardingProcessCardRefs,
    setOnboardingProcessCardRefs: (refs) => { _onboardingProcessCardRefs = refs; },
    getOnboardingStatePending: () => _onboardingStatePending,
    setOnboardingStatePending: (v) => { _onboardingStatePending = Boolean(v); },
    getOnboardingWizardRefs: () => _onboardingWizardRefs,
    getOnboardingAbortIntent: () => _onboardingAbortIntent,
    setOnboardingAbortIntent: (v) => { _onboardingAbortIntent = v; },
    resolveZotero,
    setZotero: (z) => { Zotero = z; },
    getZotero: () => Zotero,
    loadSavedTheme,
    applyTheme,
    getCurrentThemeSetting: () => currentThemeSetting,
    setCurrentThemeSetting: (t) => { currentThemeSetting = t; },
    loadSavedFontSize,
    applyFontSize,
    getCurrentFontSizeSetting: () => currentFontSizeSetting,
    setCurrentFontSizeSetting: (s) => { currentFontSizeSetting = s; },
    syncFromDetachedState,
    restoreDetachedInitialState,
    getAutoRenderMermaidPref,
    getZoteroUsername,
    snapshotDetachedState,
    handleBeforeUnload,
    getIsDetached: () => isDetached,
    setIsDetached: (v) => { isDetached = Boolean(v); },
    checkActiveSelection,
    getDetachedWindow,
    isSidebarCollapsed,
    hasZoteroActiveFocus,
    isPollingPaused,
    triggerManualBackendReconnect,
    updateBackendStatusTooltips,
    scheduleNextBackendCheck,
    scheduleNextSelectionCheck,
    syncPollingWithActivity,
    installActivityListeners,
    cleanupActivityListeners,
    setBackendPollingEnabled: (v) => {
      _backendPollingEnabled = Boolean(v);
      if (!_backendPollingEnabled) cancelBackendPollTimer();
    },
    setSelectionPollingEnabled: (v) => {
      _selectionPollingEnabled = Boolean(v);
      if (!_selectionPollingEnabled) cancelSelectionPollTimer();
    },
    setLastBackendCheckAtForTests: (ts) => { _lastBackendCheckAt = Number(ts) || 0; },
    getPollingDebugState: () => ({
      backendPollTimerActive: _backendPollTimer !== null,
      selectionPollTimerActive: _selectionPollTimer !== null,
      backendCheckInFlight: _backendCheckInFlight !== null,
      consecutiveBackendFailures: _consecutiveBackendFailures,
      lastBackendOnline: _lastBackendOnline,
      lastBackendCheckAt: _lastBackendCheckAt,
      registeredActivityListenerCount: _registeredActivityListeners.length,
      registeredObsListenerCount: _registeredObsListeners.length,
    }),
    resetPollingStateForTests: () => {
      cleanupActivityListeners();
      _backendPollingEnabled = false;
      _selectionPollingEnabled = false;
      _consecutiveBackendFailures = 0;
      _lastBackendOnline = null;
      _lastBackendCheckAt = 0;
    },
  };
}

} catch (loadError) {
  if (typeof alert === "function") {
    alert("Zocrates Sidebar Load Error: " + loadError + "\nStack: " + loadError.stack);
  } else {
    console.error("Zocrates Sidebar Load Error:", loadError);
  }
}

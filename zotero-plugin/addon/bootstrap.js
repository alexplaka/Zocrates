// Copyright 2026 Google LLC
// Licensed under the Apache License, Version 2.0

var rootURI;
var chromeHandle;

function install(data, reason) {
  // No-op
}

function startup(data, reason) {
  rootURI = data.rootURI;

  // Register chrome content so sidebar.html/css/js load via chrome:// URIs
  var aomStartup = Cc["@mozilla.org/addons/addon-manager-startup;1"]
    .getService(Ci.amIAddonManagerStartup);
  var manifestURI = Services.io.newURI(rootURI + "manifest.json");
  chromeHandle = aomStartup.registerChrome(manifestURI, [
    ["content", "zocrates", "./"],
    ["locale", "zocrates", "en-US", "locale/en-US/"],
  ]);

  if (Zotero.ftl && typeof Zotero.ftl.addResourceIds === "function") {
    try {
      Zotero.ftl.addResourceIds(["zocrates.ftl"]);
    } catch (e) {
      Zotero.debug("Zocrates: Error adding FTL resource ID: " + e);
    }
  }

  // Expose shared state on the Zotero global
  Zotero.Zocrates = {
    rootURI,
    version: data.version,
    loadStoredApiKey,
    saveStoredApiKey,
    getDetachedWindow,
    getWindowsArchitecture,
    isVcRedistInstalledWindows,
    VC_REDIST_DOC_URL,
  };
  Zotero.Zocrates._backendStatus = { status: "connecting", label: "Connecting..." };
  Zotero.Zocrates.setBackendStatus = setBackendStatus;
  Zotero.Zocrates.getWindowsArchitecture = getWindowsArchitecture;
  Zotero.Zocrates.isVcRedistInstalledWindows = isVcRedistInstalledWindows;
  Zotero.Zocrates.VC_REDIST_DOC_URL = VC_REDIST_DOC_URL;

  // Single Source of Truth for backend URL resolution. The backend binds loopback
  // only. Resolution order:
  //   0. Zotero.Zocrates.activePort — port confirmed by the Phase 3 launch
  //      handshake (Subprocess launcher spawn or warm-start probe) and
  //      persisted in the backend.port sidecar file;
  //   1. "backend_port" in <ZoteroDataDir>/zocrates-data/zocrates_config.json;
  //   2. Zotero pref "extensions.zocrates.backendPort";
  //   3. 8765.
  Zotero.Zocrates.activePort = null;
  Zotero.Zocrates._cachedBackendUrl = null;
  Zotero.Zocrates._cachedBackendPort = null;
  Zotero.Zocrates.setActivePort = function(port) {
    if (!Number.isInteger(port) || port <= 0) return;
    if (Zotero.Zocrates.activePort === port && Zotero.Zocrates._cachedBackendUrl) return;
    Zotero.Zocrates.activePort = port;
    // Invalidate any URL/port cached before the handshake confirmed readiness.
    Zotero.Zocrates._cachedBackendUrl = null;
    Zotero.Zocrates._cachedBackendPort = null;
  };
  Zotero.Zocrates.getBackendUrl = function() {
    // Tier 0: handshake-confirmed port always wins (Phase 3 activePort).
    if (Number.isInteger(Zotero.Zocrates.activePort) && Zotero.Zocrates.activePort > 0) {
      if (!Zotero.Zocrates._cachedBackendUrl) {
        Zotero.Zocrates._cachedBackendUrl = "http://127.0.0.1:" + Zotero.Zocrates.activePort;
        Zotero.Zocrates._cachedBackendPort = Zotero.Zocrates.activePort;
      }
      return Zotero.Zocrates._cachedBackendUrl;
    }
    if (Zotero.Zocrates._cachedBackendUrl) return Zotero.Zocrates._cachedBackendUrl;
    let port = null;
    // 1. Authoritative port written by the backend into zocrates_config.json
    try {
      const configFile = Zotero.File.pathToFile(
        Zotero.DataDirectory.dir + "/zocrates-data/zocrates_config.json"
      );
      if (configFile.exists()) {
        const config = JSON.parse(Zotero.File.getContents(configFile));
        if (config && Number.isInteger(config.backend_port) && config.backend_port > 0) {
          port = config.backend_port;
        }
      }
    } catch (e) {
      Zotero.debug("Zocrates: Could not read backend_port from zocrates_config.json: " + e);
    }
    // 2. Zotero pref (bootstrap-time fallback before the config file exists)
    if (port == null) {
      try {
        const prefPort = Zotero.Prefs.get("extensions.zocrates.backendPort", true);
        if (prefPort) port = Number(prefPort);
      } catch (e) {}
    }
    // 3. Deterministic candidate port computed at bootstrap (env > config >
    //    pref > offset). This lets getBackendUrl() resolve the deterministic
    //    port (e.g. 8784) even before the handshake confirms the port, so
    //    pre-handshake sidebar pings never hit the hardcoded default.
    if (port == null) {
      const candidatePort = Zotero.Zocrates._candidatePort;
      if (Number.isInteger(candidatePort) && candidatePort > 0) port = candidatePort;
    }
    // 4. Hardcoded fallback
    if (!port) port = 8765;
    Zotero.Zocrates._cachedBackendUrl = "http://127.0.0.1:" + port;
    Zotero.Zocrates._cachedBackendPort = port;
    return Zotero.Zocrates._cachedBackendUrl;
  };
  Zotero.Zocrates.getBackendPort = function() {
    // Resolve via getBackendUrl() so the cached integer port is populated, then
    // return it directly (URL re-parsing would mis-report schemes where the
    // URL parser normalizes the port away, e.g. :80/:443).
    try {
      Zotero.Zocrates.getBackendUrl();
    } catch (e) {}
    if (Number.isInteger(Zotero.Zocrates._cachedBackendPort) && Zotero.Zocrates._cachedBackendPort > 0) {
      return Zotero.Zocrates._cachedBackendPort;
    }
    return 8765;
  };
  Zotero.Zocrates.snippetSubscribers = new Set();
  Zotero.Zocrates.updateSnippetSubscribers = new Set();
  Zotero.Zocrates.updateSnippet = function(updatedData) {
    if (Zotero.Zocrates.updateSnippetSubscribers) {
      for (const sub of Zotero.Zocrates.updateSnippetSubscribers) {
        try { sub(updatedData); } catch (e) { Zotero.debug("Zocrates: snippet update subscriber error: " + e); }
      }
    }
  };

  // Phase 3 onboarding bootstrap progress bus (mirrors snippetSubscribers).
  Zotero.Zocrates._bootstrapProgressSubscribers = new Set();
  Zotero.Zocrates._bootstrapState = null;
  Zotero.Zocrates.notifyBootstrapProgress = function(stepKey, detail) {
    const event = {
      stepKey: stepKey || null,
      state: (detail && detail.state) || "active",
      detail: detail || null,
      updatedAt: Date.now(),
    };
    // Track the most recently reported (non-error) step so a launch failure can
    // highlight the correct error row in the onboarding card.
    if (stepKey && event.state !== "error") {
      Zotero.Zocrates._lastBootstrapStepKey = stepKey;
    }
    Zotero.Zocrates._bootstrapState = event;
    for (const sub of Zotero.Zocrates._bootstrapProgressSubscribers) {
      try { sub(event); } catch (e) { Zotero.debug("Zocrates: bootstrap progress subscriber error: " + e); }
    }
  };
  Zotero.Zocrates.addBootstrapProgressListener = function(cb) {
    Zotero.Zocrates._bootstrapProgressSubscribers.add(cb);
    return () => { Zotero.Zocrates._bootstrapProgressSubscribers.delete(cb); };
  };
  Zotero.Zocrates.getBootstrapProgressState = function() {
    return Zotero.Zocrates._bootstrapState;
  };
  Zotero.Zocrates.retryBackendLaunch = function() {
    try {
      Zotero.Zocrates._bootstrapSettled = false;
      Zotero.Zocrates._bootstrapState = null;
      // Reset the in-flight flag BEFORE relaunching: if a previous provisioning
      // attempt is still winding down (or failed while leaving the flag set),
      // the retry must never be silently dropped by the launch-in-flight guard.
      Zotero.Zocrates._backendLaunchInFlight = false;
      if (!Number.isInteger(Zotero.Zocrates.activePort) || Zotero.Zocrates.activePort <= 0) {
        Zotero.Zocrates._cachedBackendUrl = null;
        Zotero.Zocrates._cachedBackendPort = null;
      }
      launchBackendIfNeeded();
    } catch (e) {
      Zotero.debug("Zocrates: retryBackendLaunch error: " + e);
    }
  };
  Zotero.Zocrates.openSidebar = function() {
    let detachedWin = getDetachedWindow();
    if (detachedWin && !detachedWin.closed) {
      detachedWin.focus();
      return;
    }
    let wm = Services.wm.getMostRecentWindow("navigator:browser");
    if (!wm) return;
    let sidebar = wm.document.getElementById("zocrates-sidebar");
    let splitter = wm.document.getElementById("zocrates-splitter");
    if (sidebar && sidebar.hidden) {
      sidebar.hidden = false;
      if (splitter) splitter.hidden = false;
      let btn = wm.document.getElementById("zocrates-toolbar-btn");
      if (btn) btn.setAttribute("data-active", "true");
      try {
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-sidebar-visibility-changed", "visible");
        }
      } catch (eVis) {}
    }
  };
  Zotero.Zocrates.addSnippetToContext = function(snippet) {
    if (Zotero.Zocrates.snippetSubscribers) {
      for (const sub of Zotero.Zocrates.snippetSubscribers) {
        try { sub(snippet); } catch (e) { Zotero.debug("Zocrates: snippet subscriber error: " + e); }
      }
    }
    try {
      Zotero.Zocrates.openSidebar();
    } catch (e) {}
  };

  // Local Authentication Token Manager for Zocrates backend
  initAuthManager(Zotero);

  // Notification Hub for Zocrates
  initNotifier(Zotero);

  // Batch Progress Hub for active batch conversions
  initBatchProgress(Zotero);

  // Load item pane section helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/itemPane.js", Zotero.Zocrates);
    if (Zotero.Zocrates.ZocratesItemPane) {
      Zotero.Zocrates.ZocratesItemPane.register();
    }
  } catch (e) {
    Zotero.debug("Zocrates: Error loading itemPane.js: " + e);
  }

  // Load batch converter helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/batchConverter.js", Zotero.Zocrates);
  } catch (e) {
    Zotero.debug("Zocrates: Error loading batchConverter.js: " + e);
  }

  // Load info modal helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/infoModal.js", Zotero.Zocrates);
  } catch (e) {
    Zotero.debug("Zocrates: Error loading infoModal.js: " + e);
  }

  // Load auto converter helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/autoConverter.js", Zotero.Zocrates);
    if (Zotero.Zocrates.AutoConverter) {
      Zotero.Zocrates.AutoConverter.init();
    }
  } catch (e) {
    Zotero.debug("Zocrates: Error loading autoConverter.js: " + e);
  }

  // Load scholar import helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/scholarImport.js", Zotero.Zocrates);
  } catch (e) {
    Zotero.debug("Zocrates: Error loading scholarImport.js: " + e);
  }

  // Load reader context helper
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/readerContext.js", Zotero.Zocrates);
    if (Zotero.Zocrates.ReaderContext) {
      Zotero.Zocrates.ReaderContext.init();
    }
  } catch (e) {
    Zotero.debug("Zocrates: Error loading readerContext.js: " + e);
  }

  // Phase 7 onboarding stage bus (multi-window broadcast + state seed). See
  // initOnboardingStageBus / seedOnboardingStateFile below.
  initOnboardingStageBus(Zotero);

  // Load deterministic port offset helper (Python/JS parity formula)
  try {
    Services.scriptloader.loadSubScript(rootURI + "lib/portOffset.js", Zotero.Zocrates);
  } catch (e) {
    Zotero.debug("Zocrates: Error loading portOffset.js: " + e);
  }

  // Phase 3: managed backend launch orchestration (uv bootstrap + Subprocess
  // launcher + /status handshake). Sidebar injection is deferred until backend
  // readiness (see maybeInjectSidebar / markBackendReady); a failed launch
  // mounts the sidebar showing the onboarding error card with Retry.
  try {
    launchBackendIfNeeded().catch(function(e) {
      if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("offline", "Launch Failed");
      Zotero.debug("Zocrates: backend launch orchestration failed: " + e);
    });
  } catch (e) {
    if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("offline", "Launch Failed");
    Zotero.debug("Zocrates: Error starting backend launch orchestration: " + e);
  }

  // Register Preference Pane for Zotero Application Settings
  if (Zotero.PreferencePanes && typeof Zotero.PreferencePanes.register === "function") {
    Zotero.PreferencePanes.register({
      pluginID: "zocrates@alexplaka.dev",
      src: "preferences.xhtml",
      scripts: ["preferences.js"],
      id: "zocrates-preferences",
      label: "Zocrates",
      image: "chrome://zocrates/content/icons/icon-toggle.png",
    });
  }

  // Add sidebar to all existing main windows
  let wm = Services.wm.getEnumerator("navigator:browser");
  while (wm.hasMoreElements()) {
    let win = wm.getNext();
    addToWindow(win);
  }
}

async function shutdown(data, reason) {
  // Destroy AutoConverter
  if (Zotero.Zocrates && Zotero.Zocrates.AutoConverter) {
    try {
      Zotero.Zocrates.AutoConverter.destroy();
    } catch (e) {
      Zotero.debug("Zocrates: Error destroying AutoConverter: " + e);
    }
  }

  // Destroy ReaderContext
  if (Zotero.Zocrates && Zotero.Zocrates.ReaderContext) {
    try {
      Zotero.Zocrates.ReaderContext.destroy();
    } catch (e) {
      Zotero.debug("Zocrates: Error destroying ReaderContext: " + e);
    }
  }

  if (Zotero.Zocrates && Zotero.Zocrates.snippetSubscribers) {
    Zotero.Zocrates.snippetSubscribers.clear();
  }

  if (Zotero.Zocrates && Zotero.Zocrates.updateSnippetSubscribers) {
    Zotero.Zocrates.updateSnippetSubscribers.clear();
  }

  if (Zotero.Zocrates && Zotero.Zocrates._bootstrapProgressSubscribers) {
    Zotero.Zocrates._bootstrapProgressSubscribers.clear();
  }

  if (Zotero.Zocrates && Zotero.Zocrates._pendingSidebarWindows) {
    Zotero.Zocrates._pendingSidebarWindows.clear();
  }

  // Phase 3: best-effort backend shutdown (graceful /shutdown POST +
  // proc.wait() grace + kill). Awaited so Zotero quit does not race the
  // teardown of the backend we spawned.
  try {
    await shutdownBackendProcess();
  } catch (e) {
    Zotero.debug("Zocrates: Error during backend shutdown: " + e);
  }

  // Unregister ItemPane section
  if (Zotero.Zocrates && Zotero.Zocrates.ZocratesItemPane) {
    try {
      Zotero.Zocrates.ZocratesItemPane.unregister();
    } catch (e) {
      Zotero.debug("Zocrates: Error unregistering ItemPane: " + e);
    }
  }

  // Remove sidebar from all main windows
  let wm = Services.wm.getEnumerator("navigator:browser");
  while (wm.hasMoreElements()) {
    let win = wm.getNext();
    removeFromWindow(win);
  }

  delete Zotero.Zocrates;

  if (Zotero.ftl && typeof Zotero.ftl.removeResourceIds === "function") {
    try {
      Zotero.ftl.removeResourceIds(["zocrates.ftl"]);
    } catch (e) {}
  }

  if (chromeHandle) {
    chromeHandle.destruct();
    chromeHandle = null;
  }
}

function uninstall(data, reason) {
  // No-op
}

// Window hooks called by Zotero 7+ when main windows load/unload
function onMainWindowLoad({ window }) {
  addToWindow(window);
}

function onMainWindowUnload({ window }) {
  removeFromWindow(window);
}

// ---------------------------------------------------------------------------
// UI Injection
// ---------------------------------------------------------------------------

function addToWindow(window) {
  let doc = window.document;

  // ---- Menu item (toggle) ----
  let menuPopup = doc.getElementById("menu_ToolsPopup");
  if (menuPopup && !doc.getElementById("zocrates-menuitem")) {
    let menuitem = doc.createXULElement("menuitem");
    menuitem.id = "zocrates-menuitem";
    menuitem.setAttribute("label", "Toggle Zocrates Sidebar");
    menuitem.addEventListener("command", () => {
      toggleSidebar(window);
    });
    menuPopup.appendChild(menuitem);
  }

  // ---- Toolbar toggle button ----
  injectToolbarButton(window);

  // ---- Context menu in Library items pane ----
  injectItemContextMenu(window);

  // ---- Inject sidebar panel into the main window layout ----
  // Mounted IMMEDIATELY (not gated on backend readiness) so the onboarding
  // progress card inside the sidebar is live during first-run provisioning;
  // the chat surface stays unusable until the handshake completes (the card
  // overlays it, and the error card + Retry appear if the launch fails). On
  // readiness the done event reloads the iframe if the resolved port differs.
  maybeInjectSidebar(window);

  // ---- Tab session restore hook for network graph ----
  if (window.Zotero_Tabs && window.Zotero_Tabs.tabHooks) {
    if (!window.Zotero_Tabs.tabHooks.restoreState) {
      window.Zotero_Tabs.tabHooks.restoreState = {};
    }
    window.Zotero_Tabs.tabHooks.restoreState["zocrates_graph"] = async (tab) => {
      let attempts = 0;
      const pollContainer = () => {
        try {
          if (!window || window.closed) return;
          let container = window.document.getElementById(tab.id);
          if (container) {
            let url = tab.data?.url || "chrome://zocrates/content/graph.xhtml";
            let iframe = container.querySelector("iframe");
            if (!iframe) {
              iframe = window.document.createElement("iframe");
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
            window.setTimeout(pollContainer, 200);
          }
        } catch (e) {
          Zotero.debug("Zocrates: network graph restore error: " + e);
        }
      };
      window.setTimeout(pollContainer, 100);
      return { itemID: null };
    };
  }
}

function removeFromWindow(window) {
  let doc = window.document;

  let itemMenu = doc.getElementById("zotero-itemmenu");
  let zocratesMenu = doc.getElementById("zocrates-itemmenu");
  if (itemMenu && zocratesMenu && zocratesMenu._onPopupShowing) {
    itemMenu.removeEventListener("popupshowing", zocratesMenu._onPopupShowing);
  }

  for (let id of [
    "zocrates-menuitem",
    "zocrates-itemmenu",
    "zocrates-toolbar-btn",
    "zocrates-splitter",
    "zocrates-sidebar",
    "zocrates-toolbar-style",
  ]) {
    let el = doc.getElementById(id);
    if (el) el.remove();
  }
}

// ---------------------------------------------------------------------------
// Backend Status Manager
// ---------------------------------------------------------------------------

function setBackendStatus(status, label) {
  if (!status) status = "offline";
  if (!label) {
    if (status === "connected") label = "Connected";
    else if (status === "connecting") label = "Connecting...";
    else label = "Offline";
  }
  const payload = { status, label };
  if (typeof Zotero !== "undefined" && Zotero && Zotero.Zocrates) {
    Zotero.Zocrates._backendStatus = payload;
  }
  try {
    let windows = (typeof Zotero.getMainWindows === "function")
      ? Zotero.getMainWindows()
      : [];
    if ((!windows || windows.length === 0) && typeof Services !== "undefined" && Services.wm && typeof Services.wm.getEnumerator === "function") {
      let wm = Services.wm.getEnumerator("navigator:browser");
      let list = [];
      while (wm && wm.hasMoreElements()) {
        list.push(wm.getNext());
      }
      if (list.length > 0) windows = list;
    }
    for (const win of windows) {
      if (!win || !win.document) continue;
      const btn = win.document.getElementById("zocrates-toolbar-btn");
      if (!btn) continue;
      btn.setAttribute("data-status", status);
      const dot = btn.querySelector(".zocrates-status-dot");
      if (dot) dot.setAttribute("data-status", status);
      btn.setAttribute("tooltiptext", `Toggle Zocrates Sidebar (Local Backend: ${label})`);
    }
  } catch (e) {
    try { Zotero.debug("Zocrates: setBackendStatus error: " + e); } catch (_) {}
  }
}

if (typeof Zotero !== "undefined" && Zotero) {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  if (!Zotero.Zocrates._backendStatus) {
    Zotero.Zocrates._backendStatus = { status: "connecting", label: "Connecting..." };
  }
  Zotero.Zocrates.setBackendStatus = setBackendStatus;
}

// ---------------------------------------------------------------------------
// Toolbar Button
// ---------------------------------------------------------------------------

function injectToolbarButton(window) {
  try {
    let doc = window.document;
    if (doc.getElementById("zocrates-toolbar-btn")) return;

    Zotero.debug("Zocrates: injectToolbarButton called");

    // Find the top bar tabs toolbar container (holds the Sync button on the right)
    let toolbar = doc.getElementById("zotero-tabs-toolbar");
    if (!toolbar) {
      Zotero.debug("Zocrates: zotero-tabs-toolbar not found, trying fallback to item tree toolbar");
      toolbar = doc.getElementById("zotero-toolbar-item-tree")
        || doc.getElementById("zotero-toolbar")
        || doc.getElementById("zotero-tb-main")
        || doc.querySelector("toolbar");
    }
    if (!toolbar) {
      Zotero.debug("Zocrates: No toolbar container found!");
      return;
    }

    Zotero.debug("Zocrates: Injecting button into toolbar: " + toolbar.id);

    // Inject CSS for the button
    if (!doc.getElementById("zocrates-toolbar-style")) {
      let style = doc.createElement("style");
      style.id = "zocrates-toolbar-style";
      style.textContent = `
        #zocrates-toolbar-btn {
          appearance: none;
          -moz-appearance: none;
          background: transparent;
          border: 1px solid transparent;
          border-radius: 4px;
          padding: 3px 5px;
          margin: 0 4px;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          opacity: 0.85;
          transition: opacity 0.15s, background-color 0.15s;
          position: relative !important;
        }
        #zocrates-toolbar-btn:hover {
          opacity: 1;
          background-color: rgba(0, 0, 0, 0.08);
        }
        #zocrates-toolbar-btn[data-active="true"] {
          opacity: 1;
          background-color: rgba(99, 102, 241, 0.15);
          border-color: rgba(99, 102, 241, 0.3);
        }
        #zocrates-toolbar-btn .toolbarbutton-icon {
          width: 18px !important;
          height: 18px !important;
          border-radius: 3px !important;
          display: block !important;
          opacity: 1 !important;
        }
        .zocrates-status-dot {
          position: absolute !important;
          top: 2px !important;
          right: 2px !important;
          width: 6px !important;
          height: 6px !important;
          border-radius: 50% !important;
          pointer-events: none !important;
          box-sizing: border-box !important;
          background-color: #94a3b8 !important;
          box-shadow: 0 0 0 1px rgba(128, 128, 128, 0.35) !important;
          transition: background-color 0.2s ease, opacity 0.2s ease !important;
          z-index: 10 !important;
        }
        .zocrates-status-dot[data-status="connected"],
        #zocrates-toolbar-btn[data-status="connected"] .zocrates-status-dot {
          background-color: #10b981 !important;
        }
        .zocrates-status-dot[data-status="connecting"],
        #zocrates-toolbar-btn[data-status="connecting"] .zocrates-status-dot {
          background-color: #f59e0b !important;
          animation: zocrates-status-pulse 1.5s infinite !important;
        }
        .zocrates-status-dot[data-status="offline"],
        #zocrates-toolbar-btn[data-status="offline"] .zocrates-status-dot,
        .zocrates-status-dot[data-status="disconnected"],
        #zocrates-toolbar-btn[data-status="disconnected"] .zocrates-status-dot {
          background-color: #ef4444 !important;
        }
        @keyframes zocrates-status-pulse {
          0% { opacity: 0.4; }
          50% { opacity: 1; }
          100% { opacity: 0.4; }
        }
        /* Dark mode support */
        @media (prefers-color-scheme: dark) {
          #zocrates-toolbar-btn:hover {
            background-color: rgba(255, 255, 255, 0.1);
          }
          #zocrates-toolbar-btn[data-active="true"] {
            background-color: rgba(99, 102, 241, 0.3);
            border-color: rgba(99, 102, 241, 0.5);
          }
          .zocrates-status-dot {
            box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.6) !important;
          }
        }
      `;
      let targetRoot = doc.documentElement || doc.head || doc.body;
      if (targetRoot) targetRoot.appendChild(style);
    }

    let currentStatus = (Zotero.Zocrates && Zotero.Zocrates._backendStatus) || { status: "connecting", label: "Connecting..." };

    let btn = (typeof doc.createXULElement === "function") ? doc.createXULElement("toolbarbutton") : doc.createElement("toolbarbutton");
    btn.id = "zocrates-toolbar-btn";
    btn.className = "zotero-tb-button";
    btn.setAttribute("data-status", currentStatus.status);
    btn.setAttribute("tooltiptext", `Toggle Zocrates Sidebar (Local Backend: ${currentStatus.label})`);
    btn.setAttribute("data-active", "true");
    btn.setAttribute("image", "chrome://zocrates/content/icons/icon-toggle.png");

    btn.addEventListener("command", (e) => {
      e.stopPropagation();
      toggleSidebar(window);
      let detachedWin = getDetachedWindow();
      let sidebar = doc.getElementById("zocrates-sidebar");
      btn.setAttribute("data-active", detachedWin || (sidebar && !sidebar.hidden) ? "true" : "false");
    });

    // Insert next to the Sync button if found, otherwise append to toolbar.
    // btn is inserted with 0 children so MozToolbarbutton.connectedCallback()
    // renders the <image class="toolbarbutton-icon"> anonymous content cleanly.
    let syncBtn = doc.getElementById("zotero-tb-sync");
    if (syncBtn && syncBtn.parentNode === toolbar) {
      toolbar.insertBefore(btn, syncBtn);
      Zotero.debug("Zocrates: Button injected next to Sync button");
    } else {
      toolbar.appendChild(btn);
      Zotero.debug("Zocrates: Button appended to toolbar");
    }

    let dot = (typeof doc.createXULElement === "function") ? doc.createXULElement("box") : doc.createElement("box");
    dot.className = "zocrates-status-dot";
    dot.setAttribute("data-status", currentStatus.status);
    btn.appendChild(dot);
  } catch (err) {
    try { Zotero.debug("Zocrates: Error injecting toolbar button: " + err); } catch (_) {}
  }
}

// ---------------------------------------------------------------------------
// Context Menu Injection (zotero-itemmenu)
// ---------------------------------------------------------------------------

function injectItemContextMenu(window) {
  let doc = window.document;
  let itemMenu = doc.getElementById("zotero-itemmenu");
  if (!itemMenu || doc.getElementById("zocrates-itemmenu")) return;

  let menu = doc.createXULElement("menu");
  menu.id = "zocrates-itemmenu";
  menu.setAttribute("label", "Zocrates");
  menu.setAttribute("class", "menu-iconic");
  menu.setAttribute("image", "chrome://zocrates/content/icons/icon-16.png");

  let popup = doc.createXULElement("menupopup");
  popup.id = "zocrates-itemmenu-menupopup";

  // 1. Datalab OCR
  let miDatalab = doc.createXULElement("menuitem");
  miDatalab.id = "zocrates-itemmenu-datalab";
  miDatalab.setAttribute("label", "Convert with Datalab (Cloud)...");
  miDatalab.addEventListener("command", () => {
    let items = window.ZoteroPane ? window.ZoteroPane.getSelectedItems() : [];
    if (Zotero.Zocrates && Zotero.Zocrates.BatchConverter) {
      Zotero.Zocrates.BatchConverter.startBatchConvert(items, "datalab", { window });
    }
  });
  popup.appendChild(miDatalab);

  // 2. Docling Local
  let miDocling = doc.createXULElement("menuitem");
  miDocling.id = "zocrates-itemmenu-docling";
  miDocling.setAttribute("label", "Convert with Docling (Local)...");
  miDocling.addEventListener("command", () => {
    let items = window.ZoteroPane ? window.ZoteroPane.getSelectedItems() : [];
    if (Zotero.Zocrates && Zotero.Zocrates.BatchConverter) {
      Zotero.Zocrates.BatchConverter.startBatchConvert(items, "docling_local", { window });
    }
  });
  popup.appendChild(miDocling);

  // 3. MarkItDown
  let miMarkitdown = doc.createXULElement("menuitem");
  miMarkitdown.id = "zocrates-itemmenu-markitdown";
  miMarkitdown.setAttribute("label", "Convert with MarkItDown (Local)...");
  miMarkitdown.addEventListener("command", () => {
    let items = window.ZoteroPane ? window.ZoteroPane.getSelectedItems() : [];
    if (Zotero.Zocrates && Zotero.Zocrates.BatchConverter) {
      Zotero.Zocrates.BatchConverter.startBatchConvert(items, "markitdown", { window });
    }
  });
  popup.appendChild(miMarkitdown);

  // Separator
  let sep = doc.createXULElement("menuseparator");
  popup.appendChild(sep);

  // 4. Check Conversion Status
  let miStatus = doc.createXULElement("menuitem");
  miStatus.id = "zocrates-itemmenu-status";
  miStatus.setAttribute("label", "Check Conversion Status...");
  miStatus.addEventListener("command", () => {
    let items = window.ZoteroPane ? window.ZoteroPane.getSelectedItems() : [];
    if (Zotero.Zocrates && Zotero.Zocrates.BatchConverter) {
      Zotero.Zocrates.BatchConverter.checkConversionStatus(items, window);
    }
  });
  popup.appendChild(miStatus);

  menu.appendChild(popup);

  // Toggle visibility on popupshowing
  let onPopupShowing = () => {
    let items = window.ZoteroPane ? window.ZoteroPane.getSelectedItems() : [];
    let hasValidSelection = items && items.length > 0 && items.some(item => {
      if (!item) return false;
      if (typeof item.isAttachment === "function" && item.isAttachment()) return true;
      if (typeof item.isRegularItem === "function" && item.isRegularItem()) return true;
      return false;
    });
    menu.hidden = !hasValidSelection;
  };
  itemMenu.addEventListener("popupshowing", onPopupShowing);
  menu._onPopupShowing = onPopupShowing;

  itemMenu.appendChild(menu);
}

// ---------------------------------------------------------------------------
// Sidebar Injection
// ---------------------------------------------------------------------------

function injectSidebar(window) {
  let doc = window.document;

  // Already injected?
  if (doc.getElementById("zocrates-sidebar")) return;

  // Find the main horizontal layout that holds the three-pane view.
  // We append after #zotero-context-pane (the rightmost pane) so our
  // sidebar appears on the far right of the Zotero window.
  let container = findLayoutContainer(doc);
  if (!container) {
    Zotero.debug("Zocrates: Could not find layout container for sidebar injection.");
    return;
  }

  // ---- Splitter (resize handle between Zotero content and our sidebar) ----
  let splitter = doc.createXULElement("splitter");
  splitter.id = "zocrates-splitter";
  splitter.setAttribute("resizebefore", "closest");
  splitter.setAttribute("resizeafter", "closest");
  splitter.style.cssText = "width: 4px; cursor: ew-resize; background-color: var(--zotero-border-color, var(--zotero-pane-border-color, var(--theme-border-color, var(--border-color, ThreeDShadow)))); border: none;";

  // ---- Sidebar container ----
  let sidebarBox = doc.createXULElement("vbox");
  sidebarBox.id = "zocrates-sidebar";
  sidebarBox.style.cssText = "min-width: 280px; max-width: 50vw;";

  function applySidebarWidth() {
    try {
      let maxWidth = Math.round(window.innerWidth * 0.5);
      let width = Math.round(window.innerWidth / 3);
      width = Math.max(280, Math.min(maxWidth, width));
      Zotero.debug("Zocrates: applySidebarWidth setting style.width to " + width);
      sidebarBox.style.width = width + "px";
    } catch (e) {
      Zotero.debug("Zocrates: applySidebarWidth error: " + e);
    }
  }

  // Delay execution by 800ms to allow Zotero's async window restorer to complete
  if (doc.readyState === "complete" && window.innerWidth > 100) {
    window.setTimeout(applySidebarWidth, 800);
  } else {
    window.addEventListener("load", function onLoad() {
      window.removeEventListener("load", onLoad);
      window.setTimeout(applySidebarWidth, 800);
    });
  }

  // ---- Iframe hosting sidebar.html ----
  let iframe = doc.createElement("iframe");
  iframe.id = "zocrates-iframe";
  iframe.setAttribute("src", "chrome://zocrates/content/sidebar.html");
  iframe.setAttribute("flex", "1");
  iframe.style.cssText = "border: none; width: 100%; height: 100%;";

  sidebarBox.appendChild(iframe);
  container.appendChild(splitter);
  container.appendChild(sidebarBox);
}

/**
 * Locate the horizontal box container that holds Zotero's three-pane layout.
 * We try multiple selectors for robustness across Zotero versions.
 */
function findLayoutContainer(doc) {
  // Strategy 1: Find #zotero-context-pane (right details pane) and use its parent
  let contextPane = doc.getElementById("zotero-context-pane");
  if (contextPane && contextPane.parentNode) {
    return contextPane.parentNode;
  }

  // Strategy 2: Find #zotero-items-pane and use its parent
  let itemsPane = doc.getElementById("zotero-items-pane");
  if (itemsPane && itemsPane.parentNode) {
    return itemsPane.parentNode;
  }

  // Strategy 3: Find the main-pane container
  let mainPane = doc.getElementById("main-pane");
  if (mainPane) {
    // Look for the first hbox child that contains the pane layout
    let hbox = mainPane.querySelector("hbox");
    if (hbox) return hbox;
    return mainPane;
  }

  return null;
}

// ---------------------------------------------------------------------------
// Toggle Visibility
// ---------------------------------------------------------------------------

function getDetachedWindow() {
  if (typeof Services === "undefined" || !Services.wm || typeof Services.wm.getEnumerator !== "function") {
    return null;
  }
  let wm = Services.wm.getEnumerator(null);
  while (wm.hasMoreElements()) {
    let win = wm.getNext();
    try {
      if (win && !win.closed && (
          win.isZocratesDetached || 
          (win.location && win.location.href && 
           win.location.href.startsWith("chrome://zocrates/content/sidebar.html") && 
           win.parent === win))) {
        return win;
      }
    } catch(e) {}
  }
  return null;
}

function toggleSidebar(window) {
  let doc = window.document;
  let sidebar = doc.getElementById("zocrates-sidebar");
  let splitter = doc.getElementById("zocrates-splitter");
  if (!sidebar || !splitter) return;

  // If a detached window exists, focus it instead of showing the embedded sidebar
  let detachedWin = getDetachedWindow();
  if (detachedWin) {
    try {
      detachedWin.focus();
    } catch (e) {
      Zotero.debug("Zocrates: failed to focus detached window: " + e);
    }
    sidebar.hidden = true;
    splitter.hidden = true;
    try {
      if (typeof Services !== "undefined" && Services.obs) {
        Services.obs.notifyObservers(null, "zocrates-sidebar-visibility-changed", "hidden");
      }
    } catch (eVis) {}
    return;
  }

  let isVisible = !sidebar.hidden;
  sidebar.hidden = isVisible;
  splitter.hidden = isVisible;
  try {
    if (typeof Services !== "undefined" && Services.obs) {
      Services.obs.notifyObservers(null, "zocrates-sidebar-visibility-changed", sidebar.hidden ? "hidden" : "visible");
    }
  } catch (eVis) {}

  if (!isVisible) {
    try {
      if (Zotero?.Zocrates?._detachedNeedsRefresh || Zotero?.Zocrates?._detachedState) {
        let iframe = sidebar.querySelector("#zocrates-iframe") || sidebar.querySelector("iframe");
        if (iframe && iframe.contentWindow && typeof iframe.contentWindow.syncFromDetachedState === "function") {
          Zotero.Zocrates._detachedNeedsRefresh = false;
          if (Zotero.Zocrates._detachedState) {
            const detachedState = Zotero.Zocrates._detachedState;
            delete Zotero.Zocrates._detachedState;
            iframe.contentWindow.syncFromDetachedState(detachedState);
          } else {
            iframe.contentWindow.syncFromDetachedState({
              sessionId: Zotero.Zocrates._activeSessionId
            });
          }
        }
      }
    } catch (e) {
      Zotero.debug("Zocrates: toggleSidebar sync error: " + e);
    }
  }
}

function generateAuthToken() {
  try {
    const cryptoObj = (typeof crypto !== "undefined" && crypto) ||
      (typeof globalThis !== "undefined" && globalThis.crypto);
    if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
      const bytes = new Uint8Array(32);
      cryptoObj.getRandomValues(bytes);
      return Array.from(bytes, function(b) { return b.toString(16).padStart(2, "0"); }).join("");
    }
  } catch (e) {}
  try {
    if (typeof Cc !== "undefined" && typeof Ci !== "undefined") {
      const rng = Cc["@mozilla.org/security/random-generator;1"].createInstance(Ci.nsIRandomGenerator);
      const bytes = rng.generateRandomBytes(32);
      return Array.from(bytes, function(b) { return b.toString(16).padStart(2, "0"); }).join("");
    }
  } catch (e) {}
  throw new Error("Cryptographic random number generator is unavailable in this runtime");
}

function readAuthTokenFromSidecarSync(targetZotero) {
  const zotero = targetZotero || (typeof Zotero !== "undefined" ? Zotero : null);
  try {
    if (zotero && zotero.DataDirectory && zotero.DataDirectory.dir && zotero.File && typeof zotero.File.pathToFile === "function") {
      const baseDir = String(zotero.DataDirectory.dir).replace(/[\\/]+$/, "");
      const sep = baseDir.indexOf("\\") !== -1 ? "\\" : "/";
      const authFile = zotero.File.pathToFile(baseDir + sep + "zocrates-data" + sep + "backend.auth");
      if (authFile && typeof authFile.exists === "function" && authFile.exists()) {
        const raw = zotero.File.getContents(authFile);
        if (raw && typeof raw === "string" && raw.trim()) {
          return raw.trim();
        }
      }
    }
  } catch (e) {
    if (zotero && zotero.debug) zotero.debug("Zocrates: Could not read backend.auth synchronously: " + e);
  }
  return null;
}

function initAuthManager(targetZotero) {
  const zotero = targetZotero || (typeof Zotero !== "undefined" ? Zotero : null);
  if (!zotero) return null;
  if (!zotero.Zocrates) zotero.Zocrates = {};

  const existingSidecar = readAuthTokenFromSidecarSync(zotero);
  zotero.Zocrates._authToken = existingSidecar || zotero.Zocrates._authToken || generateAuthToken();

  zotero.Zocrates.getAuthToken = function() {
    if (zotero.Zocrates._authToken) {
      return zotero.Zocrates._authToken;
    }
    const sidecarToken = readAuthTokenFromSidecarSync(zotero);
    if (sidecarToken) {
      zotero.Zocrates._authToken = sidecarToken;
      return sidecarToken;
    }
    zotero.Zocrates._authToken = generateAuthToken();
    return zotero.Zocrates._authToken;
  };

  zotero.Zocrates.getAuthHeaders = function(headers = {}) {
    const merged = (headers && typeof headers === "object") ? { ...headers } : {};
    const token = zotero.Zocrates.getAuthToken();
    if (token) {
      merged["Authorization"] = "Bearer " + token;
    }
    return merged;
  };

  return zotero.Zocrates;
}

function initNotifier(targetZotero) {
  const zotero = targetZotero || (typeof Zotero !== "undefined" ? Zotero : null);
  const notifier = {
    _subscribers: new Set(),
    _notifications: [],
    subscribe(callback) {
      this._subscribers.add(callback);
      return () => {
        this._subscribers.delete(callback);
      };
    },
    getAll() {
      return this._notifications.slice();
    },
    clear() {
      this._notifications = [];
      try {
        if (typeof localStorage !== "undefined") {
          localStorage.removeItem("zocrates_notifications");
        }
      } catch (e) {}
    },
    delete(id) {
      this._notifications = this._notifications.filter((n) => n.id !== id);
      try {
        if (typeof localStorage !== "undefined") {
          localStorage.setItem("zocrates_notifications", JSON.stringify(this._notifications));
        }
      } catch (e) {}
      for (let cb of this._subscribers) {
        try {
          cb(null);
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error notifying subscriber on delete: " + e);
        }
      }
      try {
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-notification-deleted", id);
        }
      } catch (e) {}
    },
    add(text, type = "info") {
      let notif = {
        id: "notif_" + Date.now() + "_" + Math.random().toString(36).substring(2, 6),
        text: String(text),
        type: type,
        timestamp: Date.now(),
      };
      this._notifications.unshift(notif);
      if (this._notifications.length > 20) this._notifications.length = 20;

      try {
        if (typeof localStorage !== "undefined") {
          let raw = localStorage.getItem("zocrates_notifications");
          let list = raw ? JSON.parse(raw) : [];
          list.unshift(notif);
          if (list.length > 20) list.length = 20;
          localStorage.setItem("zocrates_notifications", JSON.stringify(list));
        }
      } catch (e) {
        if (zotero && zotero.debug) zotero.debug("Zocrates: Error persisting notification: " + e);
      }
      for (let cb of this._subscribers) {
        try {
          cb(notif);
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error notifying subscriber: " + e);
        }
      }
      try {
        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-notification-added", JSON.stringify(notif));
        }
      } catch (e) {}
      return notif.id;
    },
  };
  if (zotero) {
    if (!zotero.Zocrates) zotero.Zocrates = {};
    zotero.Zocrates.Notifier = notifier;
  }
  return notifier;
}

function initBatchProgress(targetZotero) {
  const zotero = targetZotero || (typeof Zotero !== "undefined" ? Zotero : null);
  const batchProgress = {
    _activeJob: null,
    _subscribers: new Set(),
    subscribe(callback) {
      this._subscribers.add(callback);
      if (this._activeJob) {
        try {
          callback(this._activeJob);
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error in initial BatchProgress subscription: " + e);
        }
      }
      return () => {
        this._subscribers.delete(callback);
      };
    },
    get() {
      return this._activeJob;
    },
    update(jobState) {
      this._activeJob = jobState;
      for (let cb of this._subscribers) {
        try {
          cb(this._activeJob);
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error notifying BatchProgress subscriber: " + e);
        }
      }
    },
    stop() {
      if (this._activeJob && typeof this._activeJob.abort === "function") {
        try {
          this._activeJob.abort();
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error aborting active batch job: " + e);
        }
      }
      if (this._activeJob) {
        this.update({
          ...this._activeJob,
          isStopping: true,
          statusText: "Stopping after current document...",
        });
      }
    },
    clear() {
      this._activeJob = null;
      for (let cb of this._subscribers) {
        try {
          cb(null);
        } catch (e) {
          if (zotero && zotero.debug) zotero.debug("Zocrates: Error notifying BatchProgress clear: " + e);
        }
      }
    },
  };
  if (zotero) {
    if (!zotero.Zocrates) zotero.Zocrates = {};
    zotero.Zocrates.BatchProgress = batchProgress;
  }
  return batchProgress;
}

// ---------------------------------------------------------------------------
// Phase 7 Onboarding: stage bus + state-file seeding
// ---------------------------------------------------------------------------
// Multi-window onboarding stage broadcast (mirrors the bootstrap progress bus):
// notifyOnboardingStage(stage) fans a stage change out to every sidebar iframe
// (main + detached windows) so N windows render ONE consistent card state.
// Stage writes stay single-owner (the window where the user acted); other
// windows replay the last stage on mount via getOnboardingStageState().
function initOnboardingStageBus(targetZotero) {
  const zotero = targetZotero || (typeof Zotero !== "undefined" ? Zotero : null);
  if (zotero) {
    if (!zotero.Zocrates) zotero.Zocrates = {};
    zotero.Zocrates._onboardingStageSubscribers = new Set();
    zotero.Zocrates._onboardingStage = null;
    zotero.Zocrates.notifyOnboardingStage = function(stage) {
      const normalized = (stage === "configure" || stage === "process" || stage === "ready") ? stage : null;
      zotero.Zocrates._onboardingStage = normalized;
      for (const sub of zotero.Zocrates._onboardingStageSubscribers) {
        try { sub(normalized); } catch (e) {
          if (zotero.debug) zotero.debug("Zocrates: onboarding stage subscriber error: " + e);
        }
      }
    };
    zotero.Zocrates.addOnboardingStageListener = function(cb) {
      zotero.Zocrates._onboardingStageSubscribers.add(cb);
      return () => { zotero.Zocrates._onboardingStageSubscribers.delete(cb); };
    };
    zotero.Zocrates.getOnboardingStageState = function() {
      return zotero.Zocrates._onboardingStage;
    };
  }
  return zotero && zotero.Zocrates ? zotero.Zocrates : null;
}

// Canonical seed payload for onboarding_state.json (plan §3). "seeded_at" is
// the provisioning-completion timestamp; the remaining bookkeeping fields stay
// null until the sidebar wizard advances the flow.
function buildOnboardingSeedPayload(seededAtIso) {
  return {
    version: 1,
    stage: "configure",
    seeded_at: seededAtIso,
    config_done_at: null,
    process_done_at: null,
    process_aborted: false,
  };
}

async function hasExistingProfileArtifacts(zocratesDataDir, sep) {
  if (!zocratesDataDir || typeof IOUtils === "undefined") return false;
  const pathSep = sep || (String(zocratesDataDir).indexOf("\\") !== -1 ? "\\" : "/");
  return (
    (await IOUtils.exists(zocratesDataDir + pathSep + "zocrates_config.json")) ||
    (await IOUtils.exists(zocratesDataDir + pathSep + "sessions.db"))
  );
}

// Seed onboarding_state.json (stage: "configure") immediately after a fresh
// provisioning succeeds. Only provisionEnvironment() knows provisioning ran,
// so this is the fresh-install discriminator: never written on warm-start
// attach, and skipped if zocrates_config.json or sessions.db already exists
// (an existing profile undergoing .venv provisioning is not a fresh install).
// Atomic temp+rename via IOUtils (same pattern as the backend.port /
// backend.pid sidecars); the write is skipped when the file already exists so
// an in-flight configure/process/ready stage is never clobbered. Best-effort:
// a failed seed merely means the affected install behaves as a legacy user.
async function seedOnboardingStateFile(zocratesDataDir, sep) {
  const z = Zotero.Zocrates;
  try {
    if (!zocratesDataDir || typeof IOUtils === "undefined") return false;
    const pathSep = sep || (String(zocratesDataDir).indexOf("\\") !== -1 ? "\\" : "/");
    const finalPath = zocratesDataDir + pathSep + "onboarding_state.json";
    if (await IOUtils.exists(finalPath)) return false;
    if (await hasExistingProfileArtifacts(zocratesDataDir, pathSep)) return false;
    const payload = JSON.stringify(buildOnboardingSeedPayload(new Date().toISOString()), null, 2) + "\n";
    const tmpPath = zocratesDataDir + pathSep + "onboarding_state.json.tmp";
    await IOUtils.writeUTF8(tmpPath, payload);
    // Atomic rename; noOverwrite guards against a concurrent seed writer.
    await IOUtils.move(tmpPath, finalPath, { noOverwrite: true });
    if (z && z.notifyOnboardingStage) z.notifyOnboardingStage("configure");
    return true;
  } catch (e) {
    if (z) z._seedOnboardingFailed = true;
    if (zoteroLogSafe()) Zotero.debug("Zocrates: onboarding state seed write failed (treating install as legacy): " + e);
    return false;
  }
}

function zoteroLogSafe() {
  try { return typeof Zotero !== "undefined" && Zotero && Zotero.debug; } catch (e) { return false; }
}

// ---------------------------------------------------------------------------
// Phase 3: Managed Backend Launch Orchestration
// ---------------------------------------------------------------------------
// Bootstraps a managed Python virtual environment under
// <ZoteroDataDir>/zocrates-data/.venv (via Astral's uv), spawns the backend
// with Subprocess.sys.mjs, and gates sidebar injection on a /status handshake.
// Runtime verification inside Zotero is impossible offline: every fallible step
// is defensively try/caught and logged via Zotero.debug — never throw into
// Zotero startup.

function isWindowsOs() {
  try {
    if (Services.appinfo && Services.appinfo.OS === "WINNT") return true;
  } catch (e) {}
  try {
    if (Zotero && Zotero.isWin) return true;
  } catch (e) {}
  return false;
}

function zocratesPathJoin(dir, name) {
  const base = String(dir || "");
  const sep = base.indexOf("\\") !== -1 ? "\\" : "/";
  return base + sep + name;
}

function getZocratesLaunchPaths() {
  const baseDir = String(Zotero.DataDirectory.dir || "").replace(/[\\/]+$/, "");
  if (!baseDir) throw new Error("Zotero.DataDirectory.dir is unavailable");
  const sep = baseDir.indexOf("\\") !== -1 ? "\\" : "/";
  const zocratesDataDir = baseDir + sep + "zocrates-data";
  return { baseDir, sep, zocratesDataDir };
}

let _subprocessModule = null;
function getSubprocess() {
  if (_subprocessModule) return _subprocessModule;
  if (typeof Subprocess !== "undefined") return Subprocess;
  if (typeof globalThis !== "undefined" && globalThis.Subprocess) return globalThis.Subprocess;
  try {
    _subprocessModule = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs"
    ).Subprocess;
  } catch (e) {
    Zotero.debug("Zocrates: Failed to import Subprocess.sys.mjs: " + e);
  }
  return _subprocessModule;
}

function setSubprocessForTesting(mod) {
  _subprocessModule = mod;
}

async function ensureZocratesDataDir() {
  try {
    const { zocratesDataDir } = getZocratesLaunchPaths();
    if (!(await IOUtils.exists(zocratesDataDir))) {
      await IOUtils.makeDirectory(zocratesDataDir, { ignoreExisting: true, createAncestors: true });
    }
    return zocratesDataDir;
  } catch (e) {
    Zotero.debug("Zocrates: ensureZocratesDataDir failed: " + e);
    return null;
  }
}

async function probeBackendStatus(port, timeoutMs) {
  return (await probePortState(port, timeoutMs)) === "attached";
}

// Classify a port: "attached" = a Zocrates backend bound to OUR profile data
// dir; "foreign" = something else occupies the port (any non-200 response, or a
// 200 from a backend serving a different zotero_data_dir); "free" = connection
// refused / network error.
async function probePortState(port, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(function() { try { controller.abort(); } catch (e) {} }, timeoutMs || 500);
  try {
    // Note: unauthenticated /status returns only status and zotero_data_dir,
    // which is all probePortState needs. Do not send auth headers during
    // candidate port scanning to avoid leaking the token to foreign loopback listeners.
    const res = await fetch("http://127.0.0.1:" + port + "/status", {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res || res.status !== 200) {
      Zotero.debug("Zocrates: probe on port " + port + ": HTTP " + (res ? res.status : "no response") + " -> foreign");
      return "foreign";
    }
    try {
      const body = await res.json();
      const reported = body && body.zotero_data_dir ? String(body.zotero_data_dir) : null;
      if (!reported) return "foreign";
      const normalize = function(p) { return String(p).replace(/\\/g, "/").replace(/\/+$/, ""); };
      const { baseDir } = getZocratesLaunchPaths();
      if (normalize(reported) === normalize(baseDir)) return "attached";
      Zotero.debug("Zocrates: port " + port + " serves a backend for a DIFFERENT profile (reported " + reported + ", ours " + baseDir + ") -> foreign");
      return "foreign";
    } catch (e) {
      Zotero.debug("Zocrates: /status probe on port " + port + " returned unparseable body: " + e);
      return "foreign";
    }
  } catch (e) {
    return "free";
  } finally {
    clearTimeout(timer);
  }
}

async function computeCandidateBackendPort() {
  // Precedence mirrors app/config.py::_resolve_backend_port:
  //   env BACKEND_PORT (Zotero process env, when present)
  //   > zocrates_config.json "backend_port" (int > 0)
  //   > Zotero pref "extensions.zocrates.backendPort" (only when explicitly
  //     set by the user — the seeded 8765 default must not mask the offset)
  //   > deterministic offset (8765 + SHA-256 of the zocrates-data dir path).
  // The resolved candidate is stored on Zotero.Zocrates._candidatePort so
  // getBackendUrl() can resolve it even BEFORE the handshake confirms the
  // port (pre-handshake sidebar pings must target the deterministic port,
  // not the hardcoded 8765 default).
  let port = null;
  try {
    const envPort = Services.env && Services.env.get ? Services.env.get("BACKEND_PORT") : null;
    if (envPort) {
      const parsed = Number(envPort);
      if (Number.isInteger(parsed) && parsed > 0) port = parsed;
    }
  } catch (e) {}
  const { zocratesDataDir, sep } = getZocratesLaunchPaths();
  if (port == null) {
    try {
      const configFile = Zotero.File.pathToFile(zocratesDataDir + sep + "zocrates_config.json");
      if (configFile.exists()) {
        const config = JSON.parse(Zotero.File.getContents(configFile));
        if (config && Number.isInteger(config.backend_port) && config.backend_port > 0) {
          port = config.backend_port;
        }
      }
    } catch (e) {
      Zotero.debug("Zocrates: Could not read backend_port from zocrates_config.json: " + e);
    }
  }
  if (port == null) {
    try {
      const prefName = "extensions.zocrates.backendPort";
      if (Services.prefs.prefHasUserValue(prefName)) {
        const prefPort = Services.prefs.getIntPref(prefName);
        if (Number.isInteger(prefPort) && prefPort > 0) port = prefPort;
      }
    } catch (e) {}
  }
  if (port == null) {
    try {
      const portOffset = Zotero.Zocrates && Zotero.Zocrates.ZocratesPortOffset;
      if (portOffset) {
        const offsetPort = await portOffset.computePortFromPath(zocratesDataDir);
        if (Number.isInteger(offsetPort) && offsetPort > 0) port = offsetPort;
      }
    } catch (e) {
      Zotero.debug("Zocrates: deterministic port computation failed: " + e);
    }
  }
  if (port == null) port = 8765;
  if (Zotero.Zocrates) {
    Zotero.Zocrates._candidatePort = port;
    if (!Number.isInteger(Zotero.Zocrates.activePort) || Zotero.Zocrates.activePort <= 0) {
      Zotero.Zocrates._cachedBackendUrl = null;
      Zotero.Zocrates._cachedBackendPort = null;
    }
  }
  return port;
}

async function readPipeToString(pipe) {
  let out = "";
  if (!pipe) return out;
  while (true) {
    try {
      const chunk = await pipe.read();
      if (!chunk || chunk.byteLength === 0) break;
      out += new TextDecoder().decode(chunk);
    } catch (e) {
      break;
    }
  }
  return out;
}

async function searchExecutableInPath(command) {
  const win = isWindowsOs();
  let pathEnv = "";
  try {
    if (typeof Services !== "undefined" && Services.env && typeof Services.env.get === "function") {
      pathEnv = Services.env.get("PATH") || "";
    }
  } catch (e) {}
  if (!pathEnv) return null;
  const sep = win ? ";" : ":";
  const dirs = pathEnv.split(sep).map(function(d) { return d.trim(); }).filter(Boolean);
  const exts = win ? [".exe", ".cmd", ".bat", ""] : [""];
  for (const dir of dirs) {
    for (const ext of exts) {
      const candidateName = (ext && command.toLowerCase().endsWith(ext.toLowerCase())) ? command : (command + ext);
      const candPath = dir + (win ? "\\" : "/") + candidateName;
      try {
        if (typeof IOUtils !== "undefined" && typeof IOUtils.exists === "function" && (await IOUtils.exists(candPath))) {
          return candPath;
        }
      } catch (e) {}
    }
  }
  return null;
}

async function runProvisionCommand(label, command, args, workdir) {
  const subprocess = getSubprocess();
  if (!subprocess) throw new Error("Subprocess.sys.mjs is unavailable");

  let isAbsolute = (typeof PathUtils !== "undefined" && PathUtils.isAbsolute)
    ? PathUtils.isAbsolute(command)
    : (/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(command));
  if (!isAbsolute) {
    if (typeof subprocess.pathSearch === "function") {
      try {
        const searched = await subprocess.pathSearch(command);
        if (searched) {
          command = searched;
          isAbsolute = (typeof PathUtils !== "undefined" && PathUtils.isAbsolute)
            ? PathUtils.isAbsolute(command)
            : (/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(command));
        }
      } catch (e) {}
    }
    if (!isAbsolute) {
      try {
        const manualSearched = await searchExecutableInPath(command);
        if (manualSearched) {
          command = manualSearched;
          isAbsolute = true;
        }
      } catch (e) {}
    }
    if (isWindowsOs() && !isAbsolute) {
      try {
        const sysRoot = (typeof Services !== "undefined" && Services.env && typeof Services.env.get === "function" && Services.env.get("SystemRoot")) || "C:\\Windows";
        const cmdName = command.toLowerCase().endsWith(".exe") ? command : (command + ".exe");
        const sysCandidate = sysRoot + "\\System32\\" + cmdName;
        if (typeof IOUtils !== "undefined" && typeof IOUtils.exists === "function") {
          if (await IOUtils.exists(sysCandidate)) {
            command = sysCandidate;
            isAbsolute = true;
          }
        } else {
          command = sysCandidate;
          isAbsolute = true;
        }
      } catch (e) {}
    }
  }

  Zotero.debug("Zocrates: provisioning [" + label + "] " + command + " " + args.join(" "));
  const proc = await subprocess.call({
    command: command,
    arguments: args,
    workdir: workdir || undefined,
    stderr: "pipe",
  });
  const stdoutPromise = readPipeToString(proc.stdout);
  const stderrPromise = readPipeToString(proc.stderr);
  // proc.wait() resolves to a RESULT OBJECT ({exitCode, ...}) per the official
  // Subprocess docs; unwrap defensively so both object and legacy-number
  // shapes work. Comparing the object itself to 0 misjudged every successful
  // step as a failure ("exit code [object Object]").
  const waitResult = await proc.wait();
  const exitCode = (waitResult && typeof waitResult === "object")
    ? waitResult.exitCode
    : waitResult;
  const stdout = await stdoutPromise;
  const stderr = await stderrPromise;
  if (stdout) Zotero.debug("Zocrates: [" + label + "] stdout: " + stdout.slice(-4000));
  if (stderr) Zotero.debug("Zocrates: [" + label + "] stderr: " + stderr.slice(-4000));
  if (exitCode !== 0) {
    throw new Error(
      "Provisioning step '" + label + "' failed (exit code " + exitCode + ")" +
      (stderr ? ": " + stderr.slice(-500) : "")
    );
  }
  return { exitCode, stdout, stderr };
}

async function extractZipArchive(zipPath, targetDir, sep) {
  const hasClasses = (typeof Components !== "undefined" && Components.classes) || (typeof Cc !== "undefined" && Cc);
  const hasInterfaces = (typeof Components !== "undefined" && Components.interfaces) || (typeof Ci !== "undefined" && Ci);
  const zipReaderClass = hasClasses ? hasClasses["@mozilla.org/libjar/zip-reader;1"] : null;
  const zipReaderIface = hasInterfaces ? hasInterfaces.nsIZipReader : null;

  if (!zipReaderClass || !zipReaderIface) {
    throw new Error("nsIZipReader is not available");
  }

  if (!Zotero || !Zotero.File || typeof Zotero.File.pathToFile !== "function") {
    throw new Error("Zotero.File.pathToFile is not available");
  }

  const zipFile = Zotero.File.pathToFile(zipPath);
  if (!zipFile || !zipFile.exists()) {
    throw new Error("Zip archive not found: " + zipPath);
  }

  const zipReader = zipReaderClass.createInstance(zipReaderIface);
  let opened = false;
  try {
    zipReader.open(zipFile);
    opened = true;
    const entries = zipReader.findEntries(null);
    while (entries && (typeof entries.hasMore === "function" ? entries.hasMore() : (typeof entries.hasMoreElements === "function" ? entries.hasMoreElements() : false))) {
      const entryItem = typeof entries.getNext === "function" ? entries.getNext() : null;
      if (!entryItem) break;
      const entryName = typeof entryItem === "string" ? entryItem : (entryItem.name || entryItem.entryName || String(entryItem));
      const isDir = Boolean((entryItem && entryItem.isDirectory) || entryName.endsWith("/") || entryName.endsWith("\\"));
      if (isDir) {
        continue;
      }

      // Zip-Slip guard: sanitize/validate entry name (check for .., empty segments, or absolute paths)
      if (/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(entryName) || entryName.startsWith("/") || entryName.startsWith("\\")) {
        throw new Error("Zip-Slip guard: absolute path in zip entry: " + entryName);
      }
      const segments = entryName.split(/[\\/]/);
      if (segments.some(function(s) { return s === ".." || s === ""; })) {
        throw new Error("Zip-Slip guard: invalid segment in zip entry: " + entryName);
      }

      const destPath = targetDir + sep + segments.join(sep);
      const lastSepIdx = destPath.lastIndexOf(sep);
      const parentDir = lastSepIdx !== -1 ? destPath.slice(0, lastSepIdx) : targetDir;

      await IOUtils.makeDirectory(parentDir, { ignoreExisting: true, createAncestors: true });
      await IOUtils.remove(destPath, { ignoreAbsent: true });
      zipReader.extract(entryName, Zotero.File.pathToFile(destPath));
    }
  } finally {
    if (opened || (zipReader && typeof zipReader.close === "function")) {
      try {
        zipReader.close();
      } catch (e) {}
    }
  }
}

async function verifyUvBinary(targetPath) {
  const stat = await IOUtils.stat(targetPath);
  if (!stat || stat.size < 10 * 1024 * 1024) {
    throw new Error("uv binary verification failed (size " + (stat ? stat.size : "n/a") + " < 10MB)");
  }
  const head = await IOUtils.read(targetPath, { maxBytes: 4 });
  if (head && head.length >= 2) {
    const magic = Array.from(head.slice(0, 4))
      .map(function(b) { return b.toString(16).padStart(2, "0"); })
      .join(" ");
    // ELF (Linux), PE/MZ (Windows), Mach-O (macOS, both byte orders), FAT.
    const known = ["7f 45 4c 46", "4d 5a", "cf fa ed fe", "fe ed fa ce", "ca fe ba be", "ce fa ed fe"];
    if (!known.some(function(m) { return magic.startsWith(m); })) {
      throw new Error("uv binary verification failed: unexpected magic bytes " + magic);
    }
  }
}

async function downloadStandaloneUv(binDir, sep, targetPath) {
  const win = isWindowsOs();
  let triple = null;
  try {
    const abi = String(Services.appinfo.XPCOMABI || "");
    const arch = abi.split("-")[0];
    const machine = (arch === "aarch64" || arch === "arm64") ? "aarch64" : (arch === "x86" ? "i686" : "x86_64");
    if (win) {
      triple = machine === "aarch64" ? "aarch64-pc-windows-msvc" : "x86_64-pc-windows-msvc";
    } else if (Services.appinfo.OS === "Darwin") {
      triple = machine === "aarch64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
    } else {
      triple = machine + "-unknown-linux-gnu";
    }
  } catch (e) {
    Zotero.debug("Zocrates: Could not detect host OS/arch for uv download: " + e);
  }
  if (!triple) throw new Error("Cannot determine host OS/arch for uv download");

  const artifactName = "uv-" + triple + (win ? ".zip" : ".tar.gz");
  const url = "https://github.com/astral-sh/uv/releases/latest/download/" + artifactName;
  Zotero.debug("Zocrates: downloading standalone uv from " + url);

  await IOUtils.makeDirectory(binDir, { ignoreExisting: true, createAncestors: true });
  const archivePath = binDir + sep + artifactName;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status + " for " + url);
    const buf = await res.arrayBuffer();
    if (!buf || buf.byteLength < 1024 * 1024) {
      throw new Error("uv download suspiciously small: " + (buf ? buf.byteLength : 0) + " bytes");
    }
    await IOUtils.write(archivePath, new Uint8Array(buf));
  } catch (e) {
    Zotero.debug("Zocrates: uv download failed: " + e);
    throw new Error("uv download failed: " + (e && e.message ? e.message : e));
  }

  // Remove pre-existing stale targetPath before extraction
  try { await IOUtils.remove(targetPath, { ignoreAbsent: true }); } catch (e) {}

  const subprocess = getSubprocess();

  if (win) {
    let extracted = false;
    // Strategy 1: extractZipArchive(archivePath, binDir, sep)
    try {
      await extractZipArchive(archivePath, binDir, sep);
      const flatCandidate = binDir + sep + "uv.exe";
      const nestedCandidate = binDir + sep + "uv-" + triple + sep + "uv.exe";
      if ((await IOUtils.exists(flatCandidate)) || (await IOUtils.exists(nestedCandidate))) {
        extracted = true;
      } else {
        Zotero.debug("Zocrates: extractZipArchive completed but uv.exe not found; attempting external fallbacks");
      }
    } catch (zipErr) {
      Zotero.debug("Zocrates: extractZipArchive failed, falling back to tar: " + zipErr);
    }

    // Strategy 2: If Strategy 1 throws, fallback to tar.exe
    if (!extracted) {
      try {
        let tarBinary = "tar";
        if (subprocess && typeof subprocess.pathSearch === "function") {
          try {
            const searched = await subprocess.pathSearch("tar");
            if (searched) tarBinary = searched;
          } catch (e) {}
        }
        if (tarBinary === "tar") {
          const sysRoot = (typeof Services !== "undefined" && Services.env && typeof Services.env.get === "function" && Services.env.get("SystemRoot")) || "C:\\Windows";
          const sysTar = sysRoot + "\\System32\\tar.exe";
          try {
            if (await IOUtils.exists(sysTar)) tarBinary = sysTar;
          } catch (e) {}
        }
        await runProvisionCommand("extract uv archive (tar)", tarBinary, ["-xf", archivePath, "-C", binDir], null);
        extracted = true;
      } catch (tarErr) {
        Zotero.debug("Zocrates: tar fallback failed, falling back to powershell: " + tarErr);
      }
    }

    // Strategy 3: If Strategy 2 throws, fallback to powershell.exe
    if (!extracted) {
      let psBinary = "powershell";
      if (subprocess && typeof subprocess.pathSearch === "function") {
        try {
          const searched = await subprocess.pathSearch("powershell");
          if (searched) psBinary = searched;
        } catch (e) {}
      }
      if (psBinary === "powershell") {
        const sysRoot = (typeof Services !== "undefined" && Services.env && typeof Services.env.get === "function" && Services.env.get("SystemRoot")) || "C:\\Windows";
        const sysPs = sysRoot + "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
        try {
          if (await IOUtils.exists(sysPs)) psBinary = sysPs;
        } catch (e) {}
      }
      const safeArchivePath = archivePath.replace(/'/g, "''");
      const safeBinDir = binDir.replace(/'/g, "''");
      await runProvisionCommand(
        "extract uv archive (powershell)",
        psBinary,
        ["-NoProfile", "-NonInteractive", "-Command", "Expand-Archive -LiteralPath '" + safeArchivePath + "' -DestinationPath '" + safeBinDir + "' -Force"],
        null
      );
    }
  } else {
    // Unix: Resolve tar via subprocess.pathSearch("tar") or /usr/bin/tar or /bin/tar.
    let tarBinary = null;
    if (subprocess && typeof subprocess.pathSearch === "function") {
      try {
        const searched = await subprocess.pathSearch("tar");
        if (searched) tarBinary = searched;
      } catch (e) {}
    }
    if (!tarBinary) {
      for (const cand of ["/usr/bin/tar", "/bin/tar"]) {
        try {
          if (await IOUtils.exists(cand)) {
            tarBinary = cand;
            break;
          }
        } catch (e) {}
      }
    }
    if (!tarBinary) tarBinary = "tar";
    await runProvisionCommand("extract uv archive", tarBinary, ["-xzf", archivePath, "-C", binDir], null);
  }

  // Locate extracted binary:
  // Check in this order:
  // 1. Flat root in binDir: targetPath (binDir + sep + "uv" + (win ? ".exe" : ""))
  // 2. Nested triple folder: binDir + sep + "uv-" + triple + sep + "uv" + (win ? ".exe" : "")
  // 3. Subdirectories of binDir via IOUtils.getChildren(binDir).
  // If not found, throw error.
  let extractedBinary = null;
  const flatCandidate = binDir + sep + "uv" + (win ? ".exe" : "");
  try {
    if (await IOUtils.exists(flatCandidate)) {
      extractedBinary = flatCandidate;
    }
  } catch (e) {}

  if (!extractedBinary) {
    const nestedCandidate = binDir + sep + "uv-" + triple + sep + "uv" + (win ? ".exe" : "");
    try {
      if (await IOUtils.exists(nestedCandidate)) {
        extractedBinary = nestedCandidate;
      }
    } catch (e) {}
  }

  if (!extractedBinary) {
    try {
      const children = await IOUtils.getChildren(binDir);
      for (const child of children) {
        const candidate = String(child) + sep + "uv" + (win ? ".exe" : "");
        try {
          if (await IOUtils.exists(candidate)) {
            extractedBinary = candidate;
            break;
          }
        } catch (e) {}
      }
    } catch (e) {}
  }

  if (!extractedBinary || !(await IOUtils.exists(extractedBinary))) {
    throw new Error("extracted uv binary not found in " + binDir);
  }

  // Binary placement guard:
  const norm = function(p) { return String(p).replace(/\\/g, "/"); };
  const isSame = win
    ? norm(extractedBinary).toLowerCase() === norm(targetPath).toLowerCase()
    : norm(extractedBinary) === norm(targetPath);
  if (!isSame) {
    try { await IOUtils.remove(targetPath, { ignoreAbsent: true }); } catch (e) {}
    await IOUtils.move(extractedBinary, targetPath);
  }

  // Best-effort cleanup of the extraction scaffolding and archive.
  try { await IOUtils.remove(binDir + sep + "uv-" + triple, { recursive: true, ignoreAbsent: true }); } catch (e) {}
  try { await IOUtils.remove(archivePath, { ignoreAbsent: true }); } catch (e) {}

  if (!win) {
    try { await IOUtils.setPermissions(targetPath, 0o755); } catch (e) {
      Zotero.debug("Zocrates: chmod on uv binary failed: " + e);
    }
  }
  await verifyUvBinary(targetPath);
  Zotero.debug("Zocrates: standalone uv installed at " + targetPath);
}

async function resolveUvBinary(zocratesDataDir, sep) {
  // Tier 1 (spec order): system uv reachable via PATH.
  const subprocess = getSubprocess();
  if (subprocess) {
    try {
      let systemUv = null;
      if (typeof subprocess.pathSearch === "function") {
        try {
          systemUv = await subprocess.pathSearch("uv");
        } catch (e) {}
      }
      if (systemUv) {
        await runProvisionCommand("uv --version", systemUv, ["--version"], null);
        return systemUv;
      } else {
        await runProvisionCommand("uv --version", "uv", ["--version"], null);
        return "uv";
      }
    } catch (e) {
      Zotero.debug("Zocrates: system uv not usable: " + e);
    }
  }
  // Tier 2: previously downloaded standalone uv in the hermetic bin dir.
  const binDir = zocratesDataDir + sep + "bin";
  const bundledUv = binDir + sep + "uv" + (isWindowsOs() ? ".exe" : "");
  try {
    if (await IOUtils.exists(bundledUv)) return bundledUv;
  } catch (e) {}
  // Tier 3: common user locations (~/.local/bin/uv, ~/.cargo/bin/uv).
  try {
    const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
    for (const rel of [".local" + sep + "bin", ".cargo" + sep + "bin"]) {
      const candidate = home + sep + rel + sep + "uv" + (isWindowsOs() ? ".exe" : "");
      try { if (await IOUtils.exists(candidate)) return candidate; } catch (e) {}
    }
  } catch (e) {
    Zotero.debug("Zocrates: user uv location probe skipped: " + e);
  }
  // Tier 4: download the official standalone static uv binary.
  await downloadStandaloneUv(binDir, sep, bundledUv);
  return bundledUv;
}

function getVenvPythonPath(zocratesDataDir, sep) {
  return zocratesDataDir + sep + ".venv" + sep + (isWindowsOs() ? "Scripts" + sep + "python.exe" : "bin" + sep + "python");
}

// Handshake budget from zocrates_config.json "bootstrap_handshake_timeout_ms"
// (backend-seeded default 120000; the key is materialized per the Single
// Source of Truth rule). First boot can legitimately exceed 15s while the
// backend imports heavy ML dependencies and initializes services.
function resolveHandshakeTimeoutMs(zocratesDataDir, sep) {
  // Default 300000 (5 min): the FIRST boot compiles .pyc for the whole ML
  // stack (torch/chromadb/docling/litellm) and can exceed 120s on slow disks.
  try {
    const configFile = Zotero.File.pathToFile(zocratesDataDir + sep + "zocrates_config.json");
    if (configFile.exists()) {
      const config = JSON.parse(Zotero.File.getContents(configFile));
      if (config && Number.isInteger(config.bootstrap_handshake_timeout_ms) && config.bootstrap_handshake_timeout_ms > 0) {
        return config.bootstrap_handshake_timeout_ms;
      }
    }
  } catch (e) {
    Zotero.debug("Zocrates: could not read bootstrap_handshake_timeout_ms: " + e);
  }
  return 300000;
}

// Pure helper: convert a filesystem path to a percent-encoded file:// URL for
// `uv pip install -c` values. uv parses `-r`/`-c` values as requirement
// strings, which split on whitespace, so raw paths containing spaces (common
// in Zotero profile dirs, e.g. "/home/alex/Zotero prod-test/zocrates-data")
// break provisioning with os error 21. A percent-encoded file:// URL is
// parsed as a URL and survives the requirement parser intact.
function constraintsTargetToFileUrl(constraintsTarget) {
  const normalized = String(constraintsTarget).replace(/\\/g, "/");
  const prefixed = normalized.startsWith("/") ? "file://" + normalized : "file:///" + normalized;
  // encodeURI turns spaces into %20 but leaves "#" and "?" intact, which are
  // dangerous in file URLs (fragment/query separators), so encode them too.
  return encodeURI(prefixed).replace(/#/g, "%23").replace(/\?/g, "%3F");
}

// Pure helper: build the `uv pip install` argument list for the bundled
// wheel. The constraints target (lockfile-derived pins staged by
// local_deploy.sh at backend/constraints.txt) is appended only when it was
// actually materialized, so a legacy payload without constraints.txt falls
// back to open-range resolution at install time. The constraints path is
// passed as a percent-encoded file:// URL via constraintsTargetToFileUrl
// (uv's requirement-string parser splits `-r`/`-c` values on whitespace).
// Kept pure for unit testing (tests/js/test_pinned_provisioning.js).
function buildWheelInstallArgs(venvPython, wheelInstallTarget, constraintsTarget) {
  const args = ["pip", "install", "--python", venvPython, wheelInstallTarget];
  if (constraintsTarget) {
    args.push("--constraint", constraintsTargetToFileUrl(constraintsTarget));
  }
  return args;
}

// Pure helper: filter a list of child path strings down to stale wheel
// payloads left in the zocrates-data dir by provisioning — the unversioned
// `zocrates.whl` staging copy and any `zocrates-<version>-py3-none-any.whl`
// install target (wheels from PREVIOUS extension versions accumulate across
// upgrades). Matches ONLY on the basename and never touches anything else:
// constraints.txt, .venv, sessions.db, zocrates_config.json, uv binaries, and
// sidecars are all rejected. Kept pure for unit testing.
function filterStaleWheelPayloadPaths(childPaths) {
  const stale = [];
  for (const p of childPaths || []) {
    const basename = String(p).split(/[\\/]/).pop();
    if (basename === "zocrates.whl" || /^zocrates-.*\.whl$/.test(basename)) {
      stale.push(p);
    }
  }
  return stale;
}

// Best-effort sweep of stale wheel payload files. Once `uv pip install`
// succeeds the wheels are dead weight (~2-4 MB each; the .venv holds the
// installed package) so every successful install and every warm-start attach
// sweeps them. Entire body inside try/catch: must NEVER throw or block/fail
// provisioning or launch. constraints.txt is intentionally retained (small;
// documents the installed dependency matrix for drift debugging).
async function cleanupStaleWheelPayloads(zocratesDataDir) {
  try {
    // IOUtils.getChildren() returns absolute path STRINGS.
    const children = await IOUtils.getChildren(zocratesDataDir);
    const staleWheels = filterStaleWheelPayloadPaths(children);
    for (const wheelPath of staleWheels) {
      try {
        await IOUtils.remove(wheelPath, { ignoreAbsent: true });
        Zotero.debug("Zocrates: removed stale wheel payload: " + wheelPath);
      } catch (e) {
        Zotero.debug("Zocrates: stale wheel payload removal failed for " + wheelPath + ": " + e);
      }
    }
    Zotero.debug("Zocrates: wheel payload cleanup complete (" + staleWheels.length + " file(s) removed)");
  } catch (e) {
    Zotero.debug("Zocrates: wheel payload cleanup skipped: " + e);
  }
}

// Backend version marker: <zocrates-data>/.venv/.zocrates-version records the
// manifest version whose wheel was installed into the managed .venv. It is
// read back on warm starts so an extension update (new bundled wheel) can be
// upgraded incrementally without wiping the environment.
function getBackendVersionMarkerPath(zocratesDataDir, sep) {
  return zocratesDataDir + sep + ".venv" + sep + ".zocrates-version";
}

// Best-effort write of the backend version marker. Called only after a
// successful bundled-wheel install; a failed write degrades gracefully (the
// next launch re-runs the upgrade path, which is idempotent).
async function writeBackendVersionMarker(zocratesDataDir, sep, manifestVersion) {
  if (!manifestVersion) return;
  try {
    await IOUtils.write(getBackendVersionMarkerPath(zocratesDataDir, sep), new TextEncoder().encode(manifestVersion + "\n"));
    Zotero.debug("Zocrates: backend version marker written: " + manifestVersion);
  } catch (e) {
    Zotero.debug("Zocrates: backend version marker write failed (best-effort): " + e);
  }
}

// Read the backend version marker; null when missing/unreadable.
async function readBackendVersionMarker(zocratesDataDir, sep) {
  try {
    const markerPath = getBackendVersionMarkerPath(zocratesDataDir, sep);
    if (typeof IOUtils === "undefined" || !(await IOUtils.exists(markerPath))) return null;
    const value = String(await IOUtils.readUTF8(markerPath) || "").trim();
    return value || null;
  } catch (e) {
    Zotero.debug("Zocrates: backend version marker read failed: " + e);
    return null;
  }
}

// Backend wheel installer shared by initial provisioning (provisionEnvironment)
// and the incremental upgrade path (launchBackendIfNeeded). Fetches the
// bundled wheel (backend/zocrates.whl) from the installed .xpi, stages the
// unversioned + versioned install targets in the hermetic data dir, fetches
// the pinned constraints matrix, and installs it with `uv pip install
// --constraint`. On success writes the version marker (.venv/.zocrates-version)
// so future launches can detect upgrades. Returns true when the bundled wheel
// was installed, false when no wheel was found in the payload (provisioning
// then falls back to the dev source-dir pref). Throws on install failure.
async function installBundledWheel(zocratesDataDir, sep, uvPath, venvPython, manifestVersion) {
  const wheelTarget = zocratesDataDir + sep + "zocrates.whl";
  // uv refuses to install a wheel whose filename lacks a version, so the
  // install target carries the manifest version. local_deploy.sh enforces
  // that this stays in sync with the built wheel's pyproject version.
  let resolvedVersion = manifestVersion;
  if (!resolvedVersion) {
    // Fallback: re-read the manifest if startup data lacked a version.
    try {
      const res = await fetch(rootURI + "manifest.json");
      if (res && res.ok) {
        const manifest = await res.json();
        if (manifest && typeof manifest.version === "string" && manifest.version) {
          resolvedVersion = manifest.version;
        }
      }
    } catch (e) {}
    if (!resolvedVersion) {
      Zotero.debug("Zocrates: WARNING - manifest version unavailable; wheel install will be skipped");
    }
  }
  const wheelInstallTarget = resolvedVersion
    ? zocratesDataDir + sep + "zocrates-" + resolvedVersion + "-py3-none-any.whl"
    : null;
  let haveWheel = false;
  try {
    const res = await fetch(rootURI + "backend/zocrates.whl");
    if (res.ok) {
      const buf = await res.arrayBuffer();
      if (buf && buf.byteLength > 0) {
        await IOUtils.write(wheelTarget, new Uint8Array(buf));
        if (wheelInstallTarget) {
          await IOUtils.write(wheelInstallTarget, new Uint8Array(buf));
          haveWheel = true;
        } else {
          Zotero.debug("Zocrates: bundled wheel install skipped (manifest version unavailable for versioned install target)");
        }
      }
    }
  } catch (e) {
    Zotero.debug("Zocrates: bundled wheel fetch/write failed (dev source-dir pref remains the fallback): " + e);
  }
  if (!haveWheel) return false;
  // Phase 6 pinned provisioning: fetch the lockfile-derived constraints file
  // staged next to the wheel by local_deploy.sh (uv export at deploy time)
  // so the wheel install reproduces the exact tested dependency matrix
  // instead of resolving the wheel's open ranges against current PyPI.
  // A missing/stale constraints file must never block provisioning — on any
  // failure we install without --constraint (open-range fallback).
  let constraintsTarget = null;
  try {
    const res = await fetch(rootURI + "backend/constraints.txt");
    if (res.ok) {
      const buf = await res.arrayBuffer();
      if (buf && buf.byteLength > 0) {
        constraintsTarget = zocratesDataDir + sep + "constraints.txt";
        await IOUtils.write(constraintsTarget, new Uint8Array(buf));
      }
    }
  } catch (e) {
    constraintsTarget = null;
    Zotero.debug("Zocrates: constraints.txt fetch/write failed; wheel install will resolve open dependency ranges: " + e);
  }
  if (!constraintsTarget) {
    Zotero.debug("Zocrates: no constraints.txt in payload; wheel install falls back to open-range dependency resolution (legacy payload / dev install)");
  }
  await runProvisionCommand("uv pip install (wheel)", uvPath, buildWheelInstallArgs(venvPython, wheelInstallTarget, constraintsTarget), null);
  // Record the installed backend version so future launches can detect
  // upgrades (a missing/mismatched marker triggers the incremental wheel
  // upgrade path in launchBackendIfNeeded).
  await writeBackendVersionMarker(zocratesDataDir, sep, resolvedVersion);
  return true;
}

const VC_REDIST_DOC_URL = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170#latest-supported-redistributable-version";

function getWindowsArchitecture() {
  try {
    if (typeof Services !== "undefined" && Services.appinfo && Services.appinfo.XPCOMABI) {
      const abi = String(Services.appinfo.XPCOMABI).toLowerCase();
      const arch = abi.split("-")[0];
      if (arch === "aarch64" || arch === "arm64") return "arm64";
      if (arch === "x86") return "x86";
      return "x64";
    }
  } catch (e) {}
  return "x64";
}

async function isVcRedistInstalledWindows() {
  if (!isWindowsOs()) return true;
  try {
    let sysRoot = "C:\\Windows";
    try {
      if (typeof Services !== "undefined" && Services.env && typeof Services.env.get === "function") {
        const envSysRoot = Services.env.get("SystemRoot");
        if (envSysRoot) sysRoot = envSysRoot;
      }
    } catch (e) {}
    const dllPath = sysRoot.replace(/[\\/]+$/, "") + "\\System32\\vcruntime140_1.dll";
    if (typeof IOUtils !== "undefined" && typeof IOUtils.exists === "function") {
      return Boolean(await IOUtils.exists(dllPath));
    }
  } catch (e) {
    if (typeof Zotero !== "undefined" && Zotero.debug) {
      Zotero.debug("Zocrates: isVcRedistInstalledWindows check error: " + e);
    }
  }
  return false;
}

async function ensureVcRedistWindows(zocratesDataDir, sep) {
  if (!isWindowsOs() || (await isVcRedistInstalledWindows())) {
    return true;
  }

  const rawArch = getWindowsArchitecture();
  const archLabel = rawArch === "arm64" ? "ARM64" : (rawArch === "x86" ? "X86" : "X64");

  try {
    if (typeof Zotero !== "undefined" && Zotero.Zocrates && Zotero.Zocrates.Notifier && typeof Zotero.Zocrates.Notifier.add === "function") {
      Zotero.Zocrates.Notifier.add(
        "Microsoft Visual C++ 2015–2022 Redistributable (" + archLabel + ") is required on Windows. Please install it from " + VC_REDIST_DOC_URL,
        "warning"
      );
    }
  } catch (e) {}

  const err = new Error(
    "Microsoft Visual C++ 2015–2022 Redistributable (" + archLabel + ") is required on Windows to run the local Python engine. " +
    "Please visit " + VC_REDIST_DOC_URL + " and download and install the " + archLabel + " version."
  );
  err.code = "VCRUNTIME_MISSING";
  err.arch = archLabel;
  err.docUrl = VC_REDIST_DOC_URL;
  throw err;
}

async function provisionEnvironment(zocratesDataDir, sep) {
  // 3a. uv discovery (system PATH -> bundled -> official download)
  Zotero.Zocrates.notifyBootstrapProgress("preparing", { state: "active" });
  await ensureVcRedistWindows(zocratesDataDir, sep);
  const uvPath = await resolveUvBinary(zocratesDataDir, sep);
  // 3b. Python 3.13 standalone runtime
  Zotero.Zocrates.notifyBootstrapProgress("python", { state: "active" });
  await runProvisionCommand("uv python install 3.13", uvPath, ["python", "install", "3.13"], null);
  // 3c. Dedicated virtual environment
  const venvDir = zocratesDataDir + sep + ".venv";
  await runProvisionCommand("uv venv", uvPath, ["venv", "--python", "3.13", venvDir], null);
  // 3d. Backend install: bundled wheel if present, else dev source-dir pref.
  Zotero.Zocrates.notifyBootstrapProgress("engine", { state: "active" });
  const venvPython = getVenvPythonPath(zocratesDataDir, sep);
  const haveWheel = await installBundledWheel(zocratesDataDir, sep, uvPath, venvPython, Zotero.Zocrates.version);
  if (!haveWheel) {
    let sourceDir = null;
    try { sourceDir = Zotero.Prefs.get("extensions.zocrates.backendSourceDir", true); } catch (e) {}
    if (sourceDir && typeof sourceDir === "string" && sourceDir.length > 0) {
      await runProvisionCommand("uv pip install (dev source)", uvPath, ["pip", "install", "--python", venvPython, sourceDir], null);
    } else {
      throw new Error(
        "No backend payload available: bundled backend/zocrates.whl was not found in the installed .xpi " +
        "and Zotero pref 'extensions.zocrates.backendSourceDir' is not set."
      );
    }
  }
  // Post-install cleanup: the wheel payload files are dead weight once the
  // install succeeded — Retry after a later handshake failure never needs
  // them because provisionEnvironment re-fetches them from the XPI. Sweeping
  // here also covers the dev-source branch (stale wheels from earlier runs).
  await cleanupStaleWheelPayloads(zocratesDataDir);
  return venvPython;
}

function getLoginManager() {
  try {
    if (typeof Services !== "undefined") {
      if (Services.logins) return Services.logins;
      if (Services.passwords) return Services.passwords;
    }
  } catch (e) {}
  try {
    if (typeof Zotero !== "undefined" && Zotero.Services) {
      if (Zotero.Services.logins) return Zotero.Services.logins;
      if (Zotero.Services.passwords) return Zotero.Services.passwords;
    }
  } catch (e) {}
  try {
    if (typeof Cc !== "undefined" && typeof Ci !== "undefined" && Cc["@mozilla.org/login-manager;1"]) {
      return Cc["@mozilla.org/login-manager;1"]?.getService(Ci.nsILoginManager);
    }
  } catch (e) {}
  return null;
}

function getApiKeyTargets(keyName) {
  if (!keyName) return [];
  if (keyName === "openrouter_api_key" || keyName === "openRouterApiKey" || keyName.includes("openrouter") || keyName.includes("openRouter")) {
    return ["openrouter_api_key", "openRouterApiKey"];
  }
  if (keyName === "datalab_api_key" || keyName === "datalabApiKey" || keyName.includes("datalab")) {
    return ["datalab_api_key", "datalabApiKey"];
  }
  if (keyName === "openalex_api_key" || keyName === "openAlexApiKey" || keyName.includes("openalex") || keyName.includes("openAlex")) {
    return ["openalex_api_key", "openAlexApiKey"];
  }
  if (keyName.includes("gemini") || keyName === "cloudApiKey" || keyName.includes("geminiApiKey")) {
    return ["gemini_api_key", "cloudApiKey", "geminiApiKey"];
  }
  return [keyName];
}

function getApiKeyPrefKeys(keyName) {
  const targets = getApiKeyTargets(keyName);
  const keys = new Set();
  for (const t of targets) {
    keys.add("extensions.zocrates." + t);
    keys.add("extensions.zotero.zocrates." + t);
  }
  if (keyName && (keyName.includes("openrouter") || keyName.includes("openRouter"))) {
    keys.add("extensions.zocrates.openRouterApiKey");
    keys.add("extensions.zocrates.openrouter_api_key");
    keys.add("extensions.zotero.zocrates.openRouterApiKey");
    keys.add("extensions.zotero.zocrates.openrouter_api_key");
  }
  if (keyName && (keyName.includes("datalab") || keyName.includes("dataLab"))) {
    keys.add("extensions.zocrates.datalabApiKey");
    keys.add("extensions.zocrates.datalab_api_key");
    keys.add("extensions.zotero.zocrates.datalabApiKey");
    keys.add("extensions.zotero.zocrates.datalab_api_key");
  }
  if (keyName && (keyName.includes("openalex") || keyName.includes("openAlex"))) {
    keys.add("extensions.zocrates.openAlexApiKey");
    keys.add("extensions.zocrates.openalex_api_key");
    keys.add("extensions.zotero.zocrates.openAlexApiKey");
    keys.add("extensions.zotero.zocrates.openalex_api_key");
  }
  if (keyName && (keyName.includes("gemini") || keyName === "cloudApiKey" || keyName.includes("cloud"))) {
    keys.add("extensions.zocrates.cloudApiKey");
    keys.add("extensions.zotero.zocrates.cloudApiKey");
    keys.add("extensions.zocrates.gemini_api_key");
    keys.add("extensions.zotero.zocrates.gemini_api_key");
  }
  if (keyName) {
    keys.add("extensions.zocrates." + keyName);
    keys.add("extensions.zotero.zocrates." + keyName);
  }
  return Array.from(keys);
}

function saveStoredApiKey(keyName, apiKey) {
  if (!keyName) return false;
  const val = (apiKey || "").trim();
  const hostname = "chrome://zocrates";
  const targets = getApiKeyTargets(keyName);
  const prefKeys = getApiKeyPrefKeys(keyName);

  // 1. Always persist to Zotero.Prefs as guaranteed storage so keys are never lost across restarts/flatpak boundaries
  try {
    if (typeof Zotero !== "undefined" && Zotero.Prefs) {
      for (const pk of prefKeys) {
        if (val) {
          Zotero.Prefs.set(pk, val, true);
        } else {
          Zotero.Prefs.clear(pk, true);
        }
      }
    }
  } catch (e) {}

  // 2. Opportunistically store to Login Manager if available
  const loginMgr = getLoginManager();
  if (loginMgr) {
    try {
      if (typeof loginMgr.findLogins === "function") {
        for (const target of targets) {
          try {
            const logins = loginMgr.findLogins(hostname, null, target) || [];
            for (const l of logins) {
              if (typeof loginMgr.removeLogin === "function") {
                loginMgr.removeLogin(l);
              }
            }
          } catch (e) {}
        }
      }
      if (val) {
        const canonicalTarget = targets[0] || keyName;
        let loginInfo = null;
        if (typeof Cc !== "undefined" && typeof Ci !== "undefined" && Cc["@mozilla.org/login-manager/loginInfo;1"]) {
          loginInfo = Cc["@mozilla.org/login-manager/loginInfo;1"].createInstance(Ci.nsILoginInfo);
          loginInfo.init(hostname, null, canonicalTarget, canonicalTarget, val, "", "");
        } else if (typeof Components !== "undefined" && Components.Constructor) {
          const LoginInfo = Components.Constructor(
            "@mozilla.org/login-manager/loginInfo;1",
            "nsILoginInfo",
            "init"
          );
          loginInfo = new LoginInfo(hostname, null, canonicalTarget, canonicalTarget, val, "", "");
        } else {
          loginInfo = {
            hostname,
            formSubmitURL: null,
            httpRealm: canonicalTarget,
            username: canonicalTarget,
            password: val,
          };
        }
        if (loginInfo) {
          if (typeof loginMgr.addLogin === "function") {
            try { loginMgr.addLogin(loginInfo); } catch (e) {}
          } else if (typeof loginMgr.addLoginAsync === "function") {
            loginMgr.addLoginAsync(loginInfo).catch(() => {});
          }
        }
      }
    } catch (e) {}
  }
  return true;
}

function loadStoredApiKey(keyName) {
  if (!keyName) return "";
  const targets = getApiKeyTargets(keyName);
  const hostname = "chrome://zocrates";
  const loginMgr = getLoginManager();

  // 1. Check Login Manager first
  if (loginMgr) {
    try {
      if (typeof loginMgr.findLogins === "function") {
        for (const target of targets) {
          try {
            const logins = loginMgr.findLogins(hostname, null, target);
            if (logins && logins.length > 0 && logins[0].password) {
              const val = String(logins[0].password).trim();
              if (val) return val;
            }
          } catch (e) {}
        }
      }
    } catch (e) {
      if (typeof Zotero !== "undefined" && Zotero.debug) {
        Zotero.debug("Zocrates: Note reading API key from login manager: " + e);
      }
    }
  }

  // 2. Check Zotero.Prefs (guaranteed persistent store)
  try {
    if (typeof Zotero !== "undefined" && Zotero.Prefs && typeof Zotero.Prefs.get === "function") {
      const prefKeys = getApiKeyPrefKeys(keyName);
      for (const pk of prefKeys) {
        const v = Zotero.Prefs.get(pk, true);
        if (v && typeof v === "string" && v.trim()) {
          return v.trim();
        }
      }
    }
  } catch (e) {
    if (typeof Zotero !== "undefined" && Zotero.debug) {
      Zotero.debug("Zocrates: Error reading API key from Zotero.Prefs: " + e);
    }
  }

  return "";
}

if (typeof Zotero !== "undefined") {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  Zotero.Zocrates.loadStoredApiKey = loadStoredApiKey;
  Zotero.Zocrates.saveStoredApiKey = saveStoredApiKey;
}

async function spawnBackend(zocratesDataDir, port) {
  const subprocess = getSubprocess();
  if (!subprocess) throw new Error("Subprocess.sys.mjs is unavailable");
  const { baseDir, sep } = getZocratesLaunchPaths();
  const venvPython = getVenvPythonPath(zocratesDataDir, sep);
  let zoteroPid = 0;
  try { zoteroPid = Services.appinfo.processID; } catch (e) {}
  const authToken = (Zotero.Zocrates && typeof Zotero.Zocrates.getAuthToken === "function")
    ? Zotero.Zocrates.getAuthToken()
    : (Zotero.Zocrates && Zotero.Zocrates._authToken) || generateAuthToken();
  if (Zotero.Zocrates) Zotero.Zocrates._authToken = authToken;

  const openrouterKey = loadStoredApiKey("openrouter_api_key");
  const datalabKey = loadStoredApiKey("datalab_api_key");
  const openalexKey = loadStoredApiKey("openalex_api_key");

  const childEnv = {
    ZOTERO_DATA_DIR: baseDir,
    BACKEND_PORT: String(port),
    ZOTERO_PID: String(zoteroPid),
    ZOCRATES_AUTH_TOKEN: String(authToken),
  };
  if (openrouterKey && openrouterKey.trim()) {
    childEnv.OPENROUTER_API_KEY = openrouterKey.trim();
  }
  if (datalabKey && datalabKey.trim()) {
    childEnv.DATALAB_API_KEY = datalabKey.trim();
  }
  if (openalexKey && openalexKey.trim()) {
    childEnv.OPENALEX_API_KEY = openalexKey.trim();
  }

  Zotero.debug("Zocrates: spawning backend on port " + port + ": " + venvPython + " -m zocrates.fast_api_app");
  const proc = await subprocess.call({
    command: venvPython,
    arguments: ["-m", "zocrates.fast_api_app"],
    workdir: zocratesDataDir,
    environment: childEnv,
    // CRITICAL: per the Subprocess module docs, a bare `environment` object
    // REPLACES the child's entire environment. environmentAppend: true makes
    // these three variables APPEND to (and override) the inherited Zotero
    // environment instead — without it the backend would run with no PATH,
    // HOME, or XDG dirs, breaking HF cache, platformdirs, and tool subprocesses.
    environmentAppend: true,
    stderr: "pipe",
  });
  Zotero.Zocrates._backendProcess = proc;
  if (proc.stderr) {
    proc._zocratesStderrPromise = readPipeToString(proc.stderr);
    proc._zocratesStderrPromise
      .then(function(errOut) { if (errOut) Zotero.debug("Zocrates: backend stderr (tail): " + errOut.slice(-8000)); })
      .catch(function() {});
  }
  try {
    proc.wait().then(function(waitResult) {
      const exitCode = (waitResult && typeof waitResult === "object") ? waitResult.exitCode : waitResult;
      if (exitCode !== 0) Zotero.debug("Zocrates: backend process exited with code " + exitCode);
    }).catch(function() {});
  } catch (e) {}
  return proc;
}

async function waitForBackendHandshake(port, proc, timeoutMs) {
  // Poll /status every 500ms up to timeoutMs (default 120s, materialized in
  // zocrates_config.json as bootstrap_handshake_timeout_ms). Two failure
  // modes are distinguished:
  //   - process exits early -> fail FAST with the real exit code + stderr tail
  //   - process still alive past the budget -> fail with a still-starting note
  const maxElapsed = timeoutMs || 120000;
  const startedAt = Date.now();
  for (;;) {
    if (await probeBackendStatus(port, 500)) return true;
    const elapsed = Date.now() - startedAt;
    if (elapsed >= maxElapsed) {
      throw new Error("Backend did not become ready on port " + port + " within " + Math.round(maxElapsed / 1000) + "s (still starting or hung)");
    }
    if (proc) {
      // Fail fast when the child already died (wait() resolves to a result
      // object; see runProvisionCommand for the shape-unwrap rationale).
      const early = await Promise.race([
        proc.wait().then(function(waitResult) {
          const exitCode = (waitResult && typeof waitResult === "object") ? waitResult.exitCode : waitResult;
          return exitCode === null || exitCode === undefined ? null : { exitCode: exitCode };
        }),
        new Promise(function(resolve) { setTimeout(function() { resolve(null); }, 500); }),
      ]);
      if (early) {
        let stderrTail = "";
        try {
          if (proc._zocratesStderrPromise) {
            const errOut = await Promise.race([
              proc._zocratesStderrPromise,
              new Promise(function(resolve) { setTimeout(function() { resolve(""); }, 500); }),
            ]);
            stderrTail = errOut ? errOut.slice(-1000) : "";
          }
        } catch (e) {}
        throw new Error(
          "Backend process exited during handshake (port " + port + ", exit code " + early.exitCode + ")" +
          (stderrTail ? "; stderr: " + stderrTail : "")
        );
      }
    }
    await new Promise(function(resolve) { setTimeout(resolve, 500); });
  }
}

async function persistSidecars(zocratesDataDir, sep, port, proc) {
  // Best-effort sidecar persistence (backend.port / backend.pid / backend.auth).
  try {
    await IOUtils.writeUTF8(zocratesDataDir + sep + "backend.port", String(port) + "\n");
  } catch (e) {
    Zotero.debug("Zocrates: backend.port sidecar write failed: " + e);
  }
  try {
    let backendPid = null;
    try { backendPid = proc && proc.pid; } catch (e) {}
    if (backendPid) {
      await IOUtils.writeUTF8(zocratesDataDir + sep + "backend.pid", String(backendPid) + "\n");
    }
  } catch (e) {
    Zotero.debug("Zocrates: backend.pid sidecar write failed: " + e);
  }
  try {
    const authPath = zocratesDataDir + sep + "backend.auth";
    const token = (Zotero.Zocrates && typeof Zotero.Zocrates.getAuthToken === "function")
      ? Zotero.Zocrates.getAuthToken()
      : (Zotero.Zocrates && Zotero.Zocrates._authToken);
    if (token && !(await IOUtils.exists(authPath))) {
      await IOUtils.writeUTF8(authPath, String(token) + "\n");
      if (!isWindowsOs() && typeof IOUtils.setPermissions === "function") {
        try { await IOUtils.setPermissions(authPath, 0o600); } catch (ePerm) {}
      }
    }
  } catch (e) {
    Zotero.debug("Zocrates: backend.auth sidecar write failed: " + e);
  }
}

function mountSidebars() {
  const pending = Zotero.Zocrates._pendingSidebarWindows;
  if (!pending) return;
  Zotero.Zocrates._pendingSidebarWindows = new Set();
  for (const win of pending) {
    try {
      if (win && !win.closed) injectSidebar(win);
    } catch (e) {
      Zotero.debug("Zocrates: Error mounting deferred sidebar: " + e);
    }
  }
}

function attachBackendExitMonitor(proc) {
  if (!proc || typeof proc.wait !== "function") return;
  try {
    proc.wait().then(function(waitResult) {
      const exitCode = (waitResult && typeof waitResult === "object") ? waitResult.exitCode : waitResult;
      if (typeof Zotero === "undefined" || !Zotero || !Zotero.Zocrates) {
        return;
      }
      if (Zotero.Zocrates._backendProcess && Zotero.Zocrates._backendProcess !== proc) {
        return;
      }
      Zotero.Zocrates._backendProcess = null;
      Zotero.Zocrates._backendReady = false;
      if (Zotero.Zocrates.setBackendStatus) {
        Zotero.Zocrates.setBackendStatus("offline", "Offline");
      } else {
        setBackendStatus("offline", "Offline");
      }
      try {
        if (typeof Services !== "undefined" && Services.obs && typeof Services.obs.notifyObservers === "function") {
          Services.obs.notifyObservers(
            null,
            "zocrates-backend-status-changed",
            JSON.stringify({ status: "offline", label: "Offline", exitCode: exitCode ?? null })
          );
        }
      } catch (eObs) {
        try { Zotero.debug("Zocrates: backend exit observer notify error: " + eObs); } catch (_) {}
      }
    }).catch(function() {});
  } catch (e) {
    try { Zotero.debug("Zocrates: attachBackendExitMonitor error: " + e); } catch (_) {}
  }
}
function markBackendReady(port) {
  Zotero.Zocrates._backendReady = true;
  Zotero.Zocrates._bootstrapSettled = true;
  if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("connected", "Connected");
  try { Zotero.Zocrates.notifyBootstrapProgress("done", { state: "done", port: port }); } catch (e) {}
  mountSidebars();
}

function maybeInjectSidebar(window) {
  // Mount immediately: the onboarding progress card inside the sidebar must be
  // visible during provisioning (and the error card + Retry on failure). The
  // chat surface stays unusable until the handshake completes.
  try {
    injectSidebar(window);
  } catch (e) {
    Zotero.debug("Zocrates: Error injecting sidebar: " + e);
  }
}

async function launchBackendIfNeeded() {
  const z = Zotero.Zocrates;
  if (!z || z._backendReady) return;
  if (z._backendLaunchInFlight) return;
  z._backendLaunchInFlight = true;
  if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("connecting", "Starting...");
  z._lastBootstrapStepKey = "probe";
  let currentStep = "probe";
  let spawnedProc = null;
  try {
    // 1. Hermetic data dir.
    await ensureZocratesDataDir();
    const { zocratesDataDir, sep } = getZocratesLaunchPaths();
    try {
      const authPath = zocratesDataDir + sep + "backend.auth";
      if (typeof IOUtils !== "undefined" && await IOUtils.exists(authPath)) {
        const diskToken = (await IOUtils.readUTF8(authPath)).trim();
        if (diskToken) {
          z._authToken = diskToken;
        }
      } else if (!z._authToken) {
        z._authToken = generateAuthToken();
      }
    } catch (eAuth) {
      Zotero.debug("Zocrates: backend.auth read check skipped: " + eAuth);
    }
    // 2a. Dev-profile gate: "auto_spawn_backend" in zocrates_config.json
    //     controls whether Zotero may provision/spawn a managed backend.
    //     Developers running the backend from a terminal set it to false so
    //     Zotero attaches warm when the terminal server is up and stays
    //     offline (no Python download / .venv provisioning) otherwise.
    let autoSpawnBackend = true;
    try {
      const cfgFile = Zotero.File.pathToFile(zocratesDataDir + sep + "zocrates_config.json");
      if (cfgFile.exists()) {
        const cfg = JSON.parse(Zotero.File.getContents(cfgFile));
        if (cfg && typeof cfg.auto_spawn_backend === "boolean") {
          autoSpawnBackend = cfg.auto_spawn_backend;
        }
      }
    } catch (eCfg) {
      Zotero.debug("Zocrates: could not read auto_spawn_backend from zocrates_config.json: " + eCfg);
    }
    // 2b. Candidate port + warm-start probe (dev attach / Zotero restart
    //    resilience: never double-spawn against a running terminal backend).
    //    A port occupied by a FOREIGN service (200 from a different backend, or
    //    any non-Zocrates HTTP response) causes a forward step within the
    //    8765..8864 offset band, mirroring the backend's find_free_port().
    const basePort = await computeCandidateBackendPort();
    let port = null;
    let spawnPort = null;
    for (let candidate = basePort; candidate < basePort + 100; candidate++) {
      const state = await probePortState(candidate);
      if (state === "attached") { port = candidate; break; }
      if (state === "free") { spawnPort = candidate; break; }
      Zotero.debug("Zocrates: port " + candidate + " occupied by a foreign service; stepping forward");
    }
    if (port != null) {
      Zotero.debug("Zocrates: warm backend detected on port " + port + "; attaching without spawn");
      z.setActivePort(port);
      markBackendReady(port);
      // Sweep wheel payloads left by a PREVIOUS provisioning run (the venv
      // already existed, so provisioning was skipped this launch). Fire-and-
      // forget: the sweep is best-effort and must never delay readiness.
      cleanupStaleWheelPayloads(zocratesDataDir);
      return;
    }
    if (autoSpawnBackend === false) {
      // Dev profile: never provision or spawn. The user explicitly opted out
      // of auto-spawn; if the terminal backend was not running at launch,
      // remain offline (Retry re-runs this launch flow and will attach once
      // the developer has started their terminal server).
      Zotero.debug("Zocrates: auto_spawn_backend is false and no warm backend detected; remaining offline without provisioning");
      if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("offline", "Offline");
      z._bootstrapSettled = true;
      mountSidebars();
      return;
    }
    if (spawnPort == null) {
      throw new Error("No free port in range " + basePort + ".." + (basePort + 99) + " for zocrates-data dir " + zocratesDataDir);
    }
    port = spawnPort;
    // 3. Provision the managed .venv only when missing.
    const venvPython = getVenvPythonPath(zocratesDataDir, sep);
    let haveVenv = false;
    try { haveVenv = await IOUtils.exists(venvPython); } catch (e) {
      Zotero.debug("Zocrates: venv python existence check failed: " + e);
    }
    const isProvisioning = !haveVenv;
    if (!haveVenv) {
      let hadExistingProfile = false;
      try {
        hadExistingProfile = await hasExistingProfileArtifacts(zocratesDataDir, sep);
      } catch (e) {
        Zotero.debug("Zocrates: existing profile artifacts check failed: " + e);
      }
      try {
        await provisionEnvironment(zocratesDataDir, sep);
        // Phase 7: provisioning succeeded — seed onboarding_state.json so the
        // sidebar wizard opens in CONFIGURE, unless zocrates_config.json or
        // sessions.db already existed (an existing profile re-provisioning
        // .venv is not a fresh install).
        let skipOnboardingSeed = hadExistingProfile;
        if (!skipOnboardingSeed) {
          try {
            skipOnboardingSeed = await hasExistingProfileArtifacts(zocratesDataDir, sep);
          } catch (e) {
            Zotero.debug("Zocrates: post-provision profile artifacts check failed: " + e);
          }
        }
        if (!skipOnboardingSeed) {
          await seedOnboardingStateFile(zocratesDataDir, sep);
        }
      } catch (e) {
        // A failure AFTER "uv venv" (e.g. wheel install error) leaves a partial
        // .venv behind; the haveVenv gate would then make UI Retry a no-op that
        // repeats only spawn/handshake. Remove the partial venv so the next
        // Retry re-provisions from scratch. Best-effort: never mask the error.
        try {
          await IOUtils.remove(zocratesDataDir + sep + ".venv", { recursive: true, ignoreAbsent: true });
          Zotero.debug("Zocrates: removed partial .venv after provisioning failure (Retry will re-provision)");
        } catch (e2) {
          Zotero.debug("Zocrates: could not remove partial .venv: " + e2);
        }
        throw e;
      }
    } else {
      // Incremental backend upgrade: the .venv exists, but the installed
      // backend version (recorded in .venv/.zocrates-version) may predate
      // the current extension version. On any missing/mismatched marker,
      // install the newly bundled wheel in place so an extension update
      // upgrades the backend transparently on the next launch. Best-effort:
      // a failed upgrade never blocks the launch — the existing backend
      // keeps serving and the next launch retries.
      try {
        const manifestVersion = Zotero.Zocrates.version;
        if (manifestVersion) {
          const installedVersion = await readBackendVersionMarker(zocratesDataDir, sep);
          if (installedVersion !== manifestVersion) {
            Zotero.debug("Zocrates: backend upgrade needed (installed " + (installedVersion || "unknown") + " -> manifest " + manifestVersion + ")");
            currentStep = "engine";
            Zotero.Zocrates.notifyBootstrapProgress("engine", { state: "active", upgrading: true, from: installedVersion || null, to: manifestVersion });
            const uvPath = await resolveUvBinary(zocratesDataDir, sep);
            const installed = await installBundledWheel(zocratesDataDir, sep, uvPath, venvPython, manifestVersion);
            if (installed) {
              await cleanupStaleWheelPayloads(zocratesDataDir);
            }
          }
        }
      } catch (e) {
        Zotero.debug("Zocrates: incremental backend upgrade failed (continuing with existing .venv): " + e);
      }
    }
    // 4. Spawn + handshake.
    currentStep = "starting";
    Zotero.Zocrates.notifyBootstrapProgress("starting", { state: "active", isProvisioning: isProvisioning });
    const proc = await spawnBackend(zocratesDataDir, port);
    spawnedProc = proc;
    // waitForBackendHandshake throws directly on early process exit (with the
    // exit code + stderr tail) or on budget exhaustion; venv diagnostics are
    // appended in the outer catch.
    const handshakeTimeoutMs = resolveHandshakeTimeoutMs(zocratesDataDir, sep);
    await waitForBackendHandshake(port, proc, handshakeTimeoutMs);
    // 5. Sidecars + activePort, then mount the sidebar.
    await persistSidecars(zocratesDataDir, sep, port, proc);
    z.setActivePort(port);
    markBackendReady(port);
    attachBackendExitMonitor(proc);
  } catch (e) {
    if (Zotero.Zocrates && Zotero.Zocrates.setBackendStatus) Zotero.Zocrates.setBackendStatus("offline", "Launch Failed");
    // Kill the child we spawned so failed attempts don't accumulate sick
    // backends that hold ports and profile-dir locks (they answer /status as
    // "foreign" on the next attempt and force needless port stepping).
    if (spawnedProc) {
      try {
        spawnedProc.kill();
        Zotero.debug("Zocrates: killed spawned backend after launch failure");
      } catch (eKill) {
        Zotero.debug("Zocrates: failed to kill spawned backend: " + eKill);
      }
      spawnedProc = null;
      try { z._backendProcess = null; } catch (e2) {}
    }
    const message = String(e && e.message || e);
    Zotero.debug("Zocrates: backend launch failed: " + message + (e && e.stack ? "\n" + e.stack : ""));
    try {
      z._bootstrapSettled = true;
      z.notifyBootstrapProgress("error", {
        state: "error",
        message: message,
        failedStep: z._lastBootstrapStepKey || currentStep,
        code: e && e.code,
        arch: e && e.arch,
        docUrl: e && e.docUrl,
      });
    } catch (e2) {}
    // The onboarding error card lives in the sidebar chat area; mount the
    // sidebar (without marking the backend ready) so Retry is reachable.
    try { mountSidebars(); } catch (e3) {}
  } finally {
    if (Zotero.Zocrates) Zotero.Zocrates._backendLaunchInFlight = false;
  }
}

async function shutdownBackendProcess() {
  // Graceful shutdown: fire the /shutdown POST, then wait on proc.wait() up
  // to a 2.5s grace period for a clean exit before force-killing the stored
  // Subprocess handle. Fully try/caught: must never throw into Zotero quit.
  // We only tear down a backend WE spawned (_backendProcess != null): a dev
  // terminal backend we merely attached to keeps running (see doc section 3,
  // "Zero-Touch Developer Attachment").
  let port = null;
  let proc = null;
  try {
    if (Zotero && Zotero.Zocrates) {
      Zotero.Zocrates._shuttingDown = true;
    }
    proc = Zotero.Zocrates && Zotero.Zocrates._backendProcess;
    if (proc && Zotero.Zocrates && Number.isInteger(Zotero.Zocrates.activePort) && Zotero.Zocrates.activePort > 0) {
      port = Zotero.Zocrates.activePort;
    }
  } catch (e) {}
  if (port && Number.isInteger(port) && port > 0) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(function() { try { controller.abort(); } catch (e) {} }, 1500);
      const authHeaders = (Zotero.Zocrates && typeof Zotero.Zocrates.getAuthHeaders === "function")
        ? Zotero.Zocrates.getAuthHeaders()
        : {};
      fetch("http://127.0.0.1:" + port + "/shutdown", { method: "POST", headers: authHeaders, signal: controller.signal })
        .then(function() { clearTimeout(timer); })
        .catch(function() {});
    } catch (e) {
      Zotero.debug("Zocrates: /shutdown request error: " + e);
    }
  }
  if (proc) {
    // Wait up to 2.5s for a clean exit (proc.wait()) before force-killing.
    let cleanlyExited = false;
    try {
      const waitPromise = (typeof proc.wait === "function")
        ? proc.wait().then(function() { cleanlyExited = true; }).catch(function() {})
        : Promise.resolve();
      await Promise.race([
        waitPromise,
        new Promise(function(resolve) { setTimeout(resolve, 2500); }),
      ]);
    } catch (e) {}
    if (!cleanlyExited) {
      try {
        if (typeof proc.kill === "function") proc.kill();
        Zotero.debug("Zocrates: backend process killed after 2.5s shutdown grace");
      } catch (e) {}
    } else {
      Zotero.debug("Zocrates: backend process exited cleanly");
    }
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    generateAuthToken,
    initAuthManager,
    initNotifier,
    initBatchProgress,
    initOnboardingStageBus,
    buildOnboardingSeedPayload,
    seedOnboardingStateFile,
    buildWheelInstallArgs,
    constraintsTargetToFileUrl,
    filterStaleWheelPayloadPaths,
    getBackendVersionMarkerPath,
    writeBackendVersionMarker,
    readBackendVersionMarker,
    installBundledWheel,
    shutdownBackendProcess,
    loadStoredApiKey,
    saveStoredApiKey,
    spawnBackend,
    attachBackendExitMonitor,
    launchBackendIfNeeded,
    getDetachedWindow,
    setSubprocessForTesting,
    setBackendStatus,
    injectToolbarButton,
    extractZipArchive,
    runProvisionCommand,
    downloadStandaloneUv,
    resolveUvBinary,
    getWindowsArchitecture,
    isVcRedistInstalledWindows,
    ensureVcRedistWindows,
    VC_REDIST_DOC_URL,
  };
}

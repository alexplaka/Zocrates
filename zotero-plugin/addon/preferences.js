// Zocrates Preference Pane Logic

function initZocratesPreferences() {
  Zotero.debug("Zocrates: Preference Pane script initializing...");

  const modeSelect = document.getElementById("embedding-mode-select");
  const converterSelect = document.getElementById("document-converter-select");
  const converterInfoMessage = document.getElementById("converter-info-message");
  const converterWarning = document.getElementById("document-converter-warning");
  const converterStatusBadge = document.getElementById("document-converter-status-badge");
  const converterApplyBtn = document.getElementById("document-converter-apply-btn");
  const datalabKeyGroup = document.getElementById("datalab-key-group");
  const datalabApiKeyInput = document.getElementById("datalab-api-key-input");
  const toggleDatalabKeyBtn = document.getElementById("toggle-datalab-key-btn");
  const testDatalabKeyBtn = document.getElementById("test-datalab-key-btn");
  const datalabKeyStatusBadge = document.getElementById("datalab-key-status-badge");
  const datalabFallbackContainer = document.getElementById("datalab-fallback-container");
  const datalabFallbackMarkitdown = document.getElementById("datalab-fallback-markitdown");
  const datalabFallbackDocling = document.getElementById("datalab-fallback-docling");
  const datalabMergeCrossPageContainer = document.getElementById("datalab-merge-cross-page-container");
  const datalabMergeCrossPageCheckbox = document.getElementById("datalab-merge-cross-page-checkbox");
  const enableVlmCheckbox = document.getElementById("enable-vlm-captioning-checkbox");
  const vlmSettingsContainer = document.getElementById("vlm-captioning-settings-container");
  const vlmProviderOpenRouter = document.getElementById("vlm-provider-openrouter");
  const vlmProviderOllama = document.getElementById("vlm-provider-ollama");
  const vlmCloudModelGroup = document.getElementById("vlm-cloud-model-group");
  const vlmPrimaryModelSelect = document.getElementById("vlm-primary-model-select");
  const vlmPrimaryMetadataBox = document.getElementById("vlm-primary-model-metadata-box");
  const vlmBackupModelSelect = document.getElementById("vlm-backup-model-select");
  const vlmBackupMetadataBox = document.getElementById("vlm-backup-model-metadata-box");
  const vlmLocalModelGroup = document.getElementById("vlm-local-model-group");
  const vlmLocalModelInput = document.getElementById("vlm-local-model-input");
  const vlmMaxImagesInput = document.getElementById("vlm-max-images-input");
  const apiKeyInput = document.getElementById("cloud-api-key-input");
  const openRouterApiKeyInput = document.getElementById("openrouter-api-key-input");
  const toggleOpenRouterKeyBtn = document.getElementById("toggle-openrouter-key-btn");
  const testOpenRouterKeyBtn = document.getElementById("test-openrouter-key-btn");
  const openRouterKeyStatusBadge = document.getElementById("openrouter-key-status-badge");
  const enableCitationsCheckbox = document.getElementById("enable-citations-checkbox");
  const enableReferencesCheckbox = document.getElementById("enable-references-checkbox");
  const autoRenderMermaidCheckbox = document.getElementById("auto-render-mermaid-checkbox");
  const enableMcpServerCheckbox = document.getElementById("enable-mcp-server-checkbox");
  const enablePredictiveGhostInputCheckbox = document.getElementById("enable-predictive-ghost-input-checkbox");
  const scholarPoliteEmailInput = document.getElementById("scholar-polite-email-input");
  const openAlexApiKeyInput = document.getElementById("openalex-api-key-input");
  const toggleOpenAlexKeyBtn = document.getElementById("toggle-openalex-key-btn");
  const testOpenAlexKeyBtn = document.getElementById("test-openalex-key-btn");
  const openAlexKeyStatusBadge = document.getElementById("openalex-key-status-badge");
  const researchProfileTextarea = document.getElementById("zocrates-research-profile");
  const researchProfileCounter = document.getElementById("zocrates-research-profile-counter");
  const mcpConfigSnippet = document.getElementById("mcp-config-snippet");
  const copyMcpConfigBtn = document.getElementById("copy-mcp-config-btn");

  // Session Maintenance UI Elements
  const sessionRetentionDaysInput = document.getElementById("session-retention-days-input");
  const maxSavedSessionsInput = document.getElementById("max-saved-sessions-input");
  const pruneEmptySessionsCheckbox = document.getElementById("prune-empty-sessions-checkbox");
  const sessionAutoVacuumCheckbox = document.getElementById("session-auto-vacuum-checkbox");
  const runSessionMaintenanceBtn = document.getElementById("run-session-maintenance-btn");
  const sessionMaintenanceStatusBadge = document.getElementById("session-maintenance-status-badge");
  const sessionDbFileSizeEl = document.getElementById("session-db-file-size");
  const sessionDbTotalCountEl = document.getElementById("session-db-total-count");

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let val = bytes;
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx++;
    }
    return `${val.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
  }

  function updateSessionStorageStatus(stats) {
    if (!stats) return;
    if (sessionDbFileSizeEl && stats.size_bytes !== undefined) {
      sessionDbFileSizeEl.textContent = formatBytes(stats.size_bytes);
    }
    if (sessionDbTotalCountEl && stats.session_count !== undefined) {
      sessionDbTotalCountEl.textContent = String(stats.session_count);
    }
  }

  // LLM Provider UI Elements
  const llmProviderSelect = document.getElementById("llm-provider-select");
  const openrouterKeyGroup = document.getElementById("openrouter-key-group");
  const openrouterKeyMissingNotice = document.getElementById("openrouter-key-missing-notice");
  const llmBaseUrlGroup = document.getElementById("llm-base-url-group");
  const llmBaseUrlInput = document.getElementById("llm-base-url-input");
  const llmModelGroup = document.getElementById("llm-model-group");
  const llmModelSearchContainer = document.getElementById("llm-model-search-container");
  const llmModelSearchInput = document.getElementById("llm-model-search-input");
  const llmModelSelect = document.getElementById("llm-model-select");
  const llmModelMetadataBox = document.getElementById("llm-model-metadata-box");
  const llmModelCustomInput = document.getElementById("llm-model-custom-input");
  const llmModelCountBadge = document.getElementById("llm-model-count-badge");
  const llmFilterChipsContainer = document.getElementById("llm-filter-chips");
  const llmApplyBtn = document.getElementById("llm-apply-btn");
  const llmRefreshBtn = document.getElementById("llm-refresh-btn");
  const llmTestBtn = document.getElementById("llm-test-btn");
  const llmTestStatusBadge = document.getElementById("llm-test-status-badge");
  const llmUnsavedWarning = document.getElementById("llm-unsaved-warning");
  const embeddingUnsavedWarning = document.getElementById("embedding-unsaved-warning");
  const vllmGuidanceBox = document.getElementById("vllm-guidance-box");

  const embeddingKeyMissingNotice = document.getElementById("embedding-key-missing-notice");
  const embeddingModelGroup = document.getElementById("embedding-model-group");
  const embeddingModelSelect = document.getElementById("embedding-model-select") || document.getElementById("cloud-embedding-model-select");
  const embeddingModelMetadataBox = document.getElementById("embedding-model-metadata-box");
  const embeddingRefreshBtn = document.getElementById("embedding-refresh-btn") || document.getElementById("cloud-embedding-refresh-btn");
  const embeddingTestBtn = document.getElementById("embedding-test-btn");
  const embeddingTestStatusBadge = document.getElementById("embedding-test-status-badge");
  const embeddingApplyBtn = document.getElementById("embedding-apply-btn");

  // Citation Verification UI Elements
  const verificationCard = document.getElementById("citation-verification-card");
  const verificationEnabledToggle = document.getElementById("citation-verification-enabled-toggle");
  const verificationProviderSelect = document.getElementById("verification-provider-select");
  const verificationOpenrouterModelGroup = document.getElementById("verification-openrouter-model-group");
  const verificationModelSelect = document.getElementById("verification-model-select");
  const verificationModelMetadataBox = document.getElementById("verification-model-metadata-box");
  const refreshVerificationModelsBtn = document.getElementById("refresh-verification-models-btn");
  const verificationLocalUrlGroup = document.getElementById("verification-local-url-group");
  const verificationLocalUrlInput = document.getElementById("verification-local-url-input");
  const layaStatusPill = document.getElementById("laya-status-pill");
  const installLayaBtn = document.getElementById("install-laya-btn");
  const startLayaBtn = document.getElementById("start-laya-btn");
  const stopLayaBtn = document.getElementById("stop-laya-btn");
  const autoStartLayaToggle = document.getElementById("auto-start-laya-toggle");
  const verificationThresholdInput = document.getElementById("verification-threshold-input") || document.getElementById("verification-threshold-slider");
  const verificationThresholdValue = document.getElementById("verification-threshold-value");
  const verificationTestBtn = document.getElementById("verification-test-btn");
  const verificationTestStatus = document.getElementById("verification-test-status");
  const verificationApplyBtn = document.getElementById("verification-apply-btn");
  const verificationUnsavedWarning = document.getElementById("verification-unsaved-warning");

  let rawLlmModels = [];
  let rawEmbeddingModels = [];
  let rawDecisionModels = [];
  let _layaPollTimer = null;
  let _layaPollAttempts = 0;
  let rawDoclingAssessment = null;
  let activeChipFilter = "all";
  let savedLlmProvider = "openrouter_llm";
  let savedLlmBaseUrl = "";
  let savedLlmModel = "";
  let savedEmbeddingMode = "local";
  let savedEmbeddingModel = "jinaai/jina-embeddings-v5-text-nano";
  let savedConverterMode = "local";
  let savedConverterId = "markitdown";
  let savedDatalabApiKey = "";
  let savedDatalabFallback = "markitdown";
  let savedDatalabMergeCrossPage = false;
  let savedVlmEnable = true;
  let savedVlmProvider = "openrouter";
  let savedVlmCloudModel = "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free";
  let savedVlmBackupModel = "openrouter/qwen/qwen3.7-flash";
  let savedVlmLocalModel = "";
  let savedVlmMaxImages = 100;
  let savedVerificationEnabled = false;
  let savedVerificationProvider = "openrouter";
  let savedVerificationModel = "inception/mercury-decide";
  let savedVerificationThreshold = 0.60;
  let savedVerificationAutoStart = true;
  let savedProjectDir = "";
  let copyFeedbackTimeout = null;

  function getEffectiveDatalabFallback() {
    return (datalabFallbackDocling && datalabFallbackDocling.checked) ? "docling_local" : "markitdown";
  }

  function setDatalabFallback(val) {
    if (val === "docling_local" && datalabFallbackDocling) {
      datalabFallbackDocling.checked = true;
    } else if (datalabFallbackMarkitdown) {
      datalabFallbackMarkitdown.checked = true;
    }
  }

  function getEffectiveVlmProvider() {
    return (vlmProviderOllama && vlmProviderOllama.checked) ? "ollama" : "openrouter";
  }

  function setVlmProvider(provider) {
    if (provider === "ollama") {
      if (vlmProviderOllama) vlmProviderOllama.checked = true;
      if (vlmProviderOpenRouter) vlmProviderOpenRouter.checked = false;
    } else {
      if (vlmProviderOpenRouter) vlmProviderOpenRouter.checked = true;
      if (vlmProviderOllama) vlmProviderOllama.checked = false;
    }
  }

  function updateVlmUiVisibility() {
    const isEnabled = enableVlmCheckbox ? enableVlmCheckbox.checked : true;
    if (vlmSettingsContainer) {
      vlmSettingsContainer.style.display = isEnabled ? "flex" : "none";
    }
    const provider = getEffectiveVlmProvider();
    if (vlmCloudModelGroup) {
      vlmCloudModelGroup.style.display = (provider === "openrouter") ? "block" : "none";
    }
    if (vlmLocalModelGroup) {
      vlmLocalModelGroup.style.display = (provider === "ollama") ? "block" : "none";
    }
  }

  function modelsMatch(m1, m2) {
    if (!m1 && !m2) return true;
    if (!m1 || !m2) return false;
    const s1 = m1.trim();
    const s2 = m2.trim();
    if (s1 === s2) return true;
    const strip1 = s1.replace(/^openrouter\//, "");
    const strip2 = s2.replace(/^openrouter\//, "");
    return strip1 === strip2;
  }

  function getEffectiveLlmModel() {
    const provider = llmProviderSelect ? llmProviderSelect.value : "openrouter_llm";
    if (provider === "openrouter_llm") {
      return (llmModelSelect ? llmModelSelect.value : "").trim();
    }
    return (llmModelCustomInput ? llmModelCustomInput.value : "").trim();
  }

  function getEffectiveEmbeddingModel() {
    const mode = modeSelect ? modeSelect.value : "local";
    if (mode === "cloud") {
      return (embeddingModelSelect ? embeddingModelSelect.value : "").trim();
    }
    return (embeddingModelSelect ? embeddingModelSelect.value : "jinaai/jina-embeddings-v5-text-nano").trim();
  }

  function updateLlmDirtyState() {
    if (!llmProviderSelect || !llmApplyBtn) return;
    const currentProvider = llmProviderSelect.value;
    const currentModel = getEffectiveLlmModel();
    const requiresBaseUrl = (currentProvider === "ollama" || currentProvider === "vllm" || currentProvider === "openai_compatible");
    const currentBaseUrl = (requiresBaseUrl && llmBaseUrlInput) ? llmBaseUrlInput.value.trim() : "";

    const providerMatches = (currentProvider === savedLlmProvider);
    const modelMatches = modelsMatch(currentModel, savedLlmModel);
    const baseUrlMatches = !requiresBaseUrl || (currentBaseUrl === savedLlmBaseUrl);

    const isDirty = !providerMatches || !modelMatches || !baseUrlMatches;

    if (isDirty) {
      llmApplyBtn.removeAttribute("disabled");
      llmApplyBtn.disabled = false;
      if (llmUnsavedWarning) llmUnsavedWarning.style.display = "block";
      if (llmTestStatusBadge && llmTestStatusBadge.textContent.includes("applied")) {
        llmTestStatusBadge.style.display = "none";
      }
    } else {
      llmApplyBtn.setAttribute("disabled", "disabled");
      llmApplyBtn.disabled = true;
      if (llmUnsavedWarning) llmUnsavedWarning.style.display = "none";
    }
  }

  function updateEmbeddingDirtyState() {
    if (!modeSelect || !embeddingApplyBtn) return;
    const currentMode = modeSelect.value;
    const currentModel = getEffectiveEmbeddingModel();

    const modeMatches = (currentMode === savedEmbeddingMode);
    const modelMatches = modelsMatch(currentModel, savedEmbeddingModel);

    const isDirty = !modeMatches || !modelMatches;

    if (isDirty) {
      embeddingApplyBtn.removeAttribute("disabled");
      embeddingApplyBtn.disabled = false;
      if (embeddingUnsavedWarning) embeddingUnsavedWarning.style.display = "block";
      if (embeddingTestStatusBadge && embeddingTestStatusBadge.textContent.includes("applied")) {
        embeddingTestStatusBadge.style.display = "none";
      }
    } else {
      embeddingApplyBtn.setAttribute("disabled", "disabled");
      embeddingApplyBtn.disabled = true;
      if (embeddingUnsavedWarning) embeddingUnsavedWarning.style.display = "none";
    }
  }

  function updateVerificationDirtyState() {
    if (!verificationApplyBtn) return;
    const currentEnabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
    const currentProvider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
    const currentModel = verificationModelSelect ? verificationModelSelect.value : "inception/mercury-decide";
    const currentThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
    const currentAutoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;

    const enabledMatches = (currentEnabled === savedVerificationEnabled);
    const providerMatches = (currentProvider === savedVerificationProvider);
    const modelMatches = (currentModel === savedVerificationModel);
    const thresholdMatches = Math.abs(currentThreshold - savedVerificationThreshold) < 0.001;
    const autoStartMatches = (currentAutoStart === savedVerificationAutoStart);

    const isDirty = !enabledMatches || !providerMatches || !modelMatches || !thresholdMatches || !autoStartMatches;

    if (isDirty) {
      verificationApplyBtn.removeAttribute("disabled");
      verificationApplyBtn.disabled = false;
      if (verificationUnsavedWarning) verificationUnsavedWarning.style.display = "block";
      if (verificationTestStatus && verificationTestStatus.textContent.includes("applied")) {
        verificationTestStatus.style.display = "none";
      }
    } else {
      verificationApplyBtn.setAttribute("disabled", "disabled");
      verificationApplyBtn.disabled = true;
      if (verificationUnsavedWarning) verificationUnsavedWarning.style.display = "none";
    }
  }

  function updateConverterInfoMessage() {
    if (!converterInfoMessage) return;
    const currentId = converterSelect ? converterSelect.value : "markitdown";

    while (converterInfoMessage.firstChild) {
      converterInfoMessage.removeChild(converterInfoMessage.firstChild);
    }

    if (datalabKeyGroup) {
      if (currentId === "datalab") {
        datalabKeyGroup.style.display = "block";
      } else {
        datalabKeyGroup.style.display = "none";
      }
    }

    if (datalabFallbackContainer) {
      datalabFallbackContainer.style.display = (currentId === "datalab") ? "block" : "none";
    }

    if (datalabMergeCrossPageContainer) {
      datalabMergeCrossPageContainer.style.display = (currentId === "datalab") ? "block" : "none";
    }

    converterInfoMessage.style.display = "block";

    const callout = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    callout.style.fontSize = "11px";
    callout.style.color = "var(--zc-text-muted)";
    callout.style.lineHeight = "1.5";
    callout.style.padding = "8px 10px";
    callout.style.background = "var(--zc-input-bg)";
    callout.style.border = "1px solid var(--zc-input-border)";
    callout.style.borderRadius = "6px";
    callout.style.marginTop = "8px";

    const titleDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    titleDiv.style.fontWeight = "bold";

    const descDiv = document.createElementNS("http://www.w3.org/1999/xhtml", "div");

    if (currentId === "markitdown") {
      titleDiv.textContent = "⚡ Markitdown (Local):";
      descDiv.textContent = "Fast, lightweight local text & PDF converter. Best for standard documents with instant conversion and 0% GPU/cloud usage.";
    } else if (currentId === "docling_local") {
      const isGpu = rawDoclingAssessment && rawDoclingAssessment.is_gpu_accelerated;
      titleDiv.textContent = `${isGpu ? "⚡" : "⚙️"} Docling Hardware Profile:`;
      descDiv.textContent = (rawDoclingAssessment && rawDoclingAssessment.reason)
        ? rawDoclingAssessment.reason
        : "Probing system hardware...";
    } else if (currentId === "datalab") {
      titleDiv.textContent = "☁️ Datalab (Cloud):";
      descDiv.textContent = "Cloud multimodal document OCR with full LaTeX mathematical formula transcription ($...$ / $$...$$), complex table recognition, and VLM diagram descriptions.";
    }

    callout.appendChild(titleDiv);
    callout.appendChild(descDiv);
    converterInfoMessage.appendChild(callout);
  }

  function updateConverterDirtyState() {
    if (!converterSelect || !converterApplyBtn) return;
    const currentId = converterSelect.value;
    const currentKey = datalabApiKeyInput ? datalabApiKeyInput.value.trim() : "";
    const currentFallback = getEffectiveDatalabFallback();
    const currentMergeCrossPage = datalabMergeCrossPageCheckbox ? datalabMergeCrossPageCheckbox.checked : false;

    const currentVlmEnable = enableVlmCheckbox ? enableVlmCheckbox.checked : true;
    const currentVlmProvider = getEffectiveVlmProvider();
    const currentVlmPrimaryModel = (vlmPrimaryModelSelect ? vlmPrimaryModelSelect.value : "") || "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free";
    const currentVlmBackupModel = (vlmBackupModelSelect ? vlmBackupModelSelect.value : "") || "openrouter/qwen/qwen3.7-flash";
    const currentVlmLocalModel = vlmLocalModelInput ? vlmLocalModelInput.value.trim() : "";
    const currentVlmMaxImages = vlmMaxImagesInput ? (parseInt(vlmMaxImagesInput.value, 10) || 100) : 100;

    const isDirty = (
      currentId !== savedConverterId ||
      (datalabApiKeyInput && currentKey !== savedDatalabApiKey) ||
      (currentId === "datalab" && currentFallback !== savedDatalabFallback) ||
      (currentId === "datalab" && currentMergeCrossPage !== savedDatalabMergeCrossPage) ||
      (enableVlmCheckbox && currentVlmEnable !== savedVlmEnable) ||
      (currentVlmProvider !== savedVlmProvider) ||
      (currentVlmPrimaryModel !== savedVlmCloudModel) ||
      (currentVlmBackupModel !== savedVlmBackupModel) ||
      (currentVlmLocalModel !== savedVlmLocalModel) ||
      (currentVlmMaxImages !== savedVlmMaxImages)
    );

    if (isDirty) {
      converterApplyBtn.removeAttribute("disabled");
      converterApplyBtn.disabled = false;
      if (converterWarning) converterWarning.style.display = "block";
      if (converterStatusBadge) converterStatusBadge.style.display = "none";
    } else {
      converterApplyBtn.setAttribute("disabled", "disabled");
      converterApplyBtn.disabled = true;
      if (converterWarning) converterWarning.style.display = "none";
    }
  }

  const cloudEmbeddingGroup = document.getElementById("cloud-embedding-group");
  const cloudEmbeddingModelSelect = embeddingModelSelect;
  const cloudEmbeddingRefreshBtn = embeddingRefreshBtn;

  if (!modeSelect || !converterSelect) {
    Zotero.debug("Zocrates: Preference DOM elements not found yet, retrying...");
    setTimeout(initZocratesPreferences, 100);
    return;
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

  function getApiKeyPrefKeys(keyName, legacyPrefKey) {
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
    if (legacyPrefKey) {
      keys.add(legacyPrefKey);
    }
    return Array.from(keys);
  }

  function clearLegacyApiKeyPrefs(keyName, legacyPrefKey) {
    if (typeof Zotero !== "undefined" && Zotero.Prefs && typeof Zotero.Prefs.clear === "function") {
      const prefKeys = getApiKeyPrefKeys(keyName, legacyPrefKey);
      for (const pk of prefKeys) {
        try {
          Zotero.Prefs.clear(pk, true);
        } catch (e) {}
      }
    }
  }

  // Generalized secure API key storage via Zotero.Prefs and Login Manager
  function saveSecureApiKey(keyName, apiKey) {
    if (typeof Zotero !== "undefined" && Zotero.Zocrates && typeof Zotero.Zocrates.saveStoredApiKey === "function") {
      return Zotero.Zocrates.saveStoredApiKey(keyName, apiKey);
    }

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
      } catch (e) {
        if (typeof Zotero !== "undefined" && Zotero.debug) {
          Zotero.debug("Zocrates: Error writing login to login manager: " + e);
        }
      }
    }

    return true;
  }

  function loadSecureApiKey(keyName = "gemini_api_key", legacyPrefKey = "extensions.zocrates.cloudApiKey") {
    if (typeof Zotero !== "undefined" && Zotero.Zocrates && typeof Zotero.Zocrates.loadStoredApiKey === "function") {
      const k = Zotero.Zocrates.loadStoredApiKey(keyName);
      if (k) {
        return k;
      }
    }

    let keyVal = "";
    const hostname = "chrome://zocrates";
    const targets = getApiKeyTargets(keyName);
    const loginMgr = getLoginManager();

    // 1. Check Login Manager first
    if (loginMgr) {
      try {
        if (typeof loginMgr.findLogins === "function") {
          for (const target of targets) {
            try {
              const logins = loginMgr.findLogins(hostname, null, target);
              if (logins && logins.length > 0 && logins[0].password) {
                keyVal = String(logins[0].password).trim();
                if (keyVal) {
                  return keyVal;
                }
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
    if (typeof Zotero !== "undefined" && Zotero.Prefs && typeof Zotero.Prefs.get === "function") {
      try {
        const prefKeys = getApiKeyPrefKeys(keyName, legacyPrefKey);
        for (const prefName of prefKeys) {
          const v = Zotero.Prefs.get(prefName, true);
          if (v && typeof v === "string" && v.trim()) {
            keyVal = v.trim();
            break;
          }
        }
        if (keyVal) {
          return keyVal;
        }
      } catch (e) {
        if (typeof Zotero !== "undefined" && Zotero.debug) {
          Zotero.debug("Zocrates: Error reading API key from Zotero.Prefs: " + e);
        }
      }
    }

    return "";
  }

  // Load preferences from Zotero
  function loadPreferences() {
    try {
      if (typeof Zotero !== "undefined" && Zotero.Prefs) {
        savedEmbeddingMode = Zotero.Prefs.get("extensions.zocrates.embeddingMode", true) || "local";
        if (modeSelect) modeSelect.value = savedEmbeddingMode;
        savedEmbeddingModel = Zotero.Prefs.get("extensions.zocrates.embeddingModel", true) || "jinaai/jina-embeddings-v5-text-nano";

        let rawConv = Zotero.Prefs.get("extensions.zocrates.documentConverter", true) || "markitdown";
        if (rawConv === "docling") rawConv = "docling_local";
        savedConverterId = rawConv;
        savedConverterMode = (savedConverterId === "datalab") ? "cloud" : "local";

        if (converterSelect) {
          converterSelect.value = savedConverterId;
        }
        updateConverterInfoMessage();

        savedLlmProvider = Zotero.Prefs.get("zocrates.llm_provider", true) || "openrouter_llm";
        savedLlmModel = Zotero.Prefs.get("zocrates.llm_model", true) || "google/gemini-3.8-flash";
        if (llmProviderSelect) llmProviderSelect.value = savedLlmProvider;

        savedDatalabApiKey = loadSecureApiKey("datalab_api_key", "extensions.zocrates.datalabApiKey");
        if (datalabApiKeyInput) {
          datalabApiKeyInput.value = savedDatalabApiKey;
        }

        savedDatalabFallback = Zotero.Prefs.get("extensions.zocrates.datalabNonPdfFallback", true) || "markitdown";
        setDatalabFallback(savedDatalabFallback);

        const mergeCrossPagePref = Zotero.Prefs.get("extensions.zocrates.datalabMergeCrossPage", true);
        savedDatalabMergeCrossPage = Boolean(mergeCrossPagePref);
        if (datalabMergeCrossPageCheckbox) {
          datalabMergeCrossPageCheckbox.checked = savedDatalabMergeCrossPage;
        }

        savedVlmEnable = Zotero.Prefs.get("extensions.zocrates.enableVlmCaptioning", true);
        if (savedVlmEnable === undefined || savedVlmEnable === null) savedVlmEnable = true;
        if (enableVlmCheckbox) enableVlmCheckbox.checked = savedVlmEnable;

        savedVlmProvider = Zotero.Prefs.get("extensions.zocrates.vlmCaptioningProvider", true) || "openrouter";
        setVlmProvider(savedVlmProvider);

        savedVlmCloudModel = Zotero.Prefs.get("extensions.zocrates.vlmCaptioningModel", true) || "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free";
        savedVlmBackupModel = Zotero.Prefs.get("extensions.zocrates.vlmCaptioningBackupModel", true) || "openrouter/qwen/qwen3.7-flash";
        renderVlmModels(savedVlmCloudModel, savedVlmBackupModel);

        savedVlmLocalModel = Zotero.Prefs.get("extensions.zocrates.vlmCaptioningLocalModel", true) || "";
        if (vlmLocalModelInput) vlmLocalModelInput.value = savedVlmLocalModel;

        savedVlmMaxImages = Zotero.Prefs.get("extensions.zocrates.vlmMaxImagesPerDoc", true) || 100;
        if (vlmMaxImagesInput) vlmMaxImagesInput.value = savedVlmMaxImages;

        updateVlmUiVisibility();

        // Citations and references checkbox defaults
        const citationsEnabled = Zotero.Prefs.get("extensions.zocrates.citationsEnabled", true);
        if (enableCitationsCheckbox) {
          enableCitationsCheckbox.checked = (citationsEnabled === null || citationsEnabled === undefined) ? true : Boolean(citationsEnabled);
        }
        const referencesSectionEnabled = Zotero.Prefs.get("extensions.zocrates.referencesSectionEnabled", true);
        if (enableReferencesCheckbox) {
          enableReferencesCheckbox.checked = (referencesSectionEnabled === null || referencesSectionEnabled === undefined) ? true : Boolean(referencesSectionEnabled);
        }
        const autoRenderMermaid = Zotero.Prefs.get("extensions.zocrates.autoRenderMermaid", true);
        if (autoRenderMermaidCheckbox) {
          autoRenderMermaidCheckbox.checked = (autoRenderMermaid === null || autoRenderMermaid === undefined) ? true : Boolean(autoRenderMermaid);
        }
        const enableMcpServer = Zotero.Prefs.get("extensions.zocrates.enableMcpServer", true);
        if (enableMcpServerCheckbox) {
          enableMcpServerCheckbox.checked = (enableMcpServer === null || enableMcpServer === undefined) ? true : Boolean(enableMcpServer);
        }
        const enablePredictiveGhostInput = Zotero.Prefs.get("extensions.zocrates.enablePredictiveGhostInput", true);
        if (enablePredictiveGhostInputCheckbox) {
          enablePredictiveGhostInputCheckbox.checked = (enablePredictiveGhostInput === null || enablePredictiveGhostInput === undefined) ? true : Boolean(enablePredictiveGhostInput);
        }
        const scholarPoliteEmail = Zotero.Prefs.get("extensions.zocrates.scholarPoliteEmail", true);
        if (scholarPoliteEmailInput && scholarPoliteEmail !== undefined && scholarPoliteEmail !== null) {
          scholarPoliteEmailInput.value = scholarPoliteEmail;
        }
        const savedRetentionDays = Zotero.Prefs.get("extensions.zocrates.sessionRetentionDays", true);
        if (sessionRetentionDaysInput && savedRetentionDays !== undefined && savedRetentionDays !== null) {
          sessionRetentionDaysInput.value = savedRetentionDays;
        }
        const savedMaxSessions = Zotero.Prefs.get("extensions.zocrates.maxSavedSessions", true);
        if (maxSavedSessionsInput && savedMaxSessions !== undefined && savedMaxSessions !== null) {
          maxSavedSessionsInput.value = savedMaxSessions;
        }
        const savedPruneEmpty = Zotero.Prefs.get("extensions.zocrates.pruneEmptySessions", true);
        if (pruneEmptySessionsCheckbox) {
          pruneEmptySessionsCheckbox.checked = (savedPruneEmpty === null || savedPruneEmpty === undefined) ? true : Boolean(savedPruneEmpty);
        }
        const savedAutoVacuum = Zotero.Prefs.get("extensions.zocrates.sessionDbAutoVacuumOnStartup", true);
        if (sessionAutoVacuumCheckbox) {
          sessionAutoVacuumCheckbox.checked = (savedAutoVacuum === null || savedAutoVacuum === undefined) ? true : Boolean(savedAutoVacuum);
        }
        if (openAlexApiKeyInput) {
          openAlexApiKeyInput.value = loadSecureApiKey("openalex_api_key", "extensions.zocrates.openAlexApiKey");
        }

        // Citation Verification defaults
        const citationVerificationEnabled = Zotero.Prefs.get("extensions.zocrates.citationVerificationEnabled", true);
        if (verificationEnabledToggle && citationVerificationEnabled !== undefined && citationVerificationEnabled !== null) {
          verificationEnabledToggle.checked = Boolean(citationVerificationEnabled);
        }
        const citationVerificationProvider = Zotero.Prefs.get("extensions.zocrates.citationVerificationProvider", true);
        if (verificationProviderSelect && citationVerificationProvider) {
          verificationProviderSelect.value = citationVerificationProvider;
        }
        const citationVerificationModel = Zotero.Prefs.get("extensions.zocrates.citationVerificationModel", true);
        if (verificationModelSelect && citationVerificationModel && citationVerificationModel !== "laya") {
          verificationModelSelect.value = citationVerificationModel;
        }
        const citationVerificationThreshold = Zotero.Prefs.get("extensions.zocrates.citationVerificationThreshold", true);
        if (verificationThresholdInput && citationVerificationThreshold !== undefined && citationVerificationThreshold !== null) {
          verificationThresholdInput.value = Number(citationVerificationThreshold).toFixed(2);
          if (verificationThresholdValue) verificationThresholdValue.textContent = Number(citationVerificationThreshold).toFixed(2);
        }
        const citationVerificationLocalUrl = Zotero.Prefs.get("extensions.zocrates.citationVerificationLocalUrl", true);
        if (verificationLocalUrlInput && citationVerificationLocalUrl) {
          verificationLocalUrlInput.value = citationVerificationLocalUrl;
        }
        const citationVerificationAutoStartLocal = Zotero.Prefs.get("extensions.zocrates.citationVerificationAutoStartLocal", true);
        if (autoStartLayaToggle && citationVerificationAutoStartLocal !== undefined && citationVerificationAutoStartLocal !== null) {
          autoStartLayaToggle.checked = Boolean(citationVerificationAutoStartLocal);
        }
        savedVerificationEnabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
        savedVerificationProvider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
        savedVerificationModel = (citationVerificationModel && citationVerificationModel !== "laya") ? citationVerificationModel : (verificationModelSelect && verificationModelSelect.value && verificationModelSelect.value !== "laya" ? verificationModelSelect.value : "inception/mercury-decide");
        savedVerificationThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
        savedVerificationAutoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;
        updateVerificationUiVisibility();
        updateVerificationModelMetadata();

        if (apiKeyInput) {
          apiKeyInput.value = loadSecureApiKey("gemini_api_key", "extensions.zocrates.cloudApiKey");
        }
        if (openRouterApiKeyInput) {
          openRouterApiKeyInput.value = loadSecureApiKey("openrouter_api_key", "extensions.zocrates.openRouterApiKey");
        }
      }
      // NOTE: backend_port and settings are intentionally NOT written here.
      // loadPreferences() is purely read-only to prevent writing stale defaults to disk.
      // The backend is the single authoritative writer of zocrates_config.json.
      updateConverterDirtyState();
      updateLlmDirtyState();
      updateEmbeddingDirtyState();
      updateVerificationDirtyState();
      updateMcpConfigSnippet();
    } catch (e) {
      Zotero.debug("Zocrates: Could not load preferences: " + e);
    }
  }

  // Save preference to Zotero
  function savePreference(key, value) {
    try {
      if (typeof Zotero !== "undefined" && Zotero.Prefs) {
        Zotero.Prefs.set(`extensions.zocrates.${key}`, value, true);
      }
    } catch (e) {
      Zotero.debug(`Zocrates: Could not save preference ${key}: ` + e);
    }
  }

  // Single source of truth for backend URL resolution, installed by bootstrap.js
  function getBackendUrl() {
    try {
      return Zotero?.Zocrates?.getBackendUrl?.() || "http://127.0.0.1:8765";
    } catch (e) {
      return "http://127.0.0.1:8765";
    }
  }

  function getAuthToken() {
    try {
      return Zotero?.Zocrates?.getAuthToken?.() || Zotero?.Zocrates?._authToken || "";
    } catch (e) {
      return "";
    }
  }

  function getAuthHeaders(extraHeaders = {}) {
    try {
      if (Zotero?.Zocrates?.getAuthHeaders) {
        return Zotero.Zocrates.getAuthHeaders(extraHeaders);
      }
      const token = getAuthToken();
      if (token) {
        return { ...extraHeaders, Authorization: "Bearer " + token };
      }
    } catch (e) {}
    return { ...extraHeaders };
  }

  // Central authenticated helper to dispatch settings updates to backend.
  // The backend persists to zocrates_config.json under FileLock as the single authoritative writer.
  async function postLlmConfig(updates) {
    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/config`;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(updates),
      });
      if (!res.ok) {
        Zotero.debug(`Zocrates: Settings sync rejected with status: ${res.status}`);
      }
      return res;
    } catch (err) {
      Zotero.debug(`Zocrates: Failed to sync config with backend: ${err}`);
      return null;
    }
  }

  // Deprecated: direct filesystem writing to zocrates_config.json via IOUtils has been removed.
  // The backend is the single authoritative writer of zocrates_config.json under FileLock.
  // Retained for backward compatibility by routing updates to postLlmConfig without writing to disk directly.
  async function saveConfigJson(updates) {
    Zotero.debug("Zocrates: saveConfigJson is deprecated; routing updates to backend /config");
    try {
      if (!updates || typeof updates !== "object") return null;
      const safeUpdates = { ...updates };
      for (const k of Object.keys(safeUpdates)) {
        if (k.endsWith("_api_key") || k.endsWith("_secret") || k.includes("ApiKey")) {
          delete safeUpdates[k];
        }
      }
      return await postLlmConfig(safeUpdates);
    } catch (err) {
      Zotero.debug("Zocrates: Error in deprecated saveConfigJson: " + err);
      return null;
    }
  }

  // Fetch status from local Fast-API server and update UI
  async function fetchStatus() {
    const backendUrl = getBackendUrl();
    const statusUrl = `${backendUrl}/status`;

    try {
      const response = await fetch(statusUrl, {
        headers: getAuthHeaders(),
      });
      if (!response.ok) throw new Error("Status API returned error");
      const data = await response.json();

      // Update Hardware info
      if (data.hardware) {
        if (data.hardware.docling_assessment) {
          rawDoclingAssessment = data.hardware.docling_assessment;
        }
        updateConverterInfoMessage();
      }

      if (data.enable_vlm_captioning !== undefined) {
        savedVlmEnable = Boolean(data.enable_vlm_captioning);
        if (enableVlmCheckbox) enableVlmCheckbox.checked = savedVlmEnable;
      }
      if (data.auto_render_mermaid_diagrams !== undefined && autoRenderMermaidCheckbox) {
        autoRenderMermaidCheckbox.checked = Boolean(data.auto_render_mermaid_diagrams);
      }
      if (data.enable_mcp_server !== undefined && enableMcpServerCheckbox) {
        enableMcpServerCheckbox.checked = Boolean(data.enable_mcp_server);
      }
      if (data.enable_predictive_ghost_input !== undefined && enablePredictiveGhostInputCheckbox) {
        enablePredictiveGhostInputCheckbox.checked = Boolean(data.enable_predictive_ghost_input);
      }
      if (data.user_research_profile !== undefined && researchProfileTextarea) {
        researchProfileTextarea.value = data.user_research_profile || "";
        updateResearchProfileCounter();
      }
      if (data.scholar_polite_email !== undefined && scholarPoliteEmailInput) {
        scholarPoliteEmailInput.value = data.scholar_polite_email || "";
      }
      if (data.session_storage_stats) {
        updateSessionStorageStatus(data.session_storage_stats);
      }
      if (data.project_dir) {
        updateMcpConfigSnippet(data.project_dir, backendUrl);
      }
      if (data.citation_verification_enabled !== undefined && verificationEnabledToggle) {
        verificationEnabledToggle.checked = Boolean(data.citation_verification_enabled);
      }
      if (data.citation_verification_provider && verificationProviderSelect) {
        verificationProviderSelect.value = data.citation_verification_provider;
      }
      if (data.citation_verification_model && verificationModelSelect && data.citation_verification_model !== "laya") {
        verificationModelSelect.value = data.citation_verification_model;
      }
      if (data.citation_verification_threshold !== undefined && verificationThresholdInput) {
        verificationThresholdInput.value = Number(data.citation_verification_threshold).toFixed(2);
        if (verificationThresholdValue) verificationThresholdValue.textContent = Number(data.citation_verification_threshold).toFixed(2);
      }
      if (data.citation_verification_local_url && verificationLocalUrlInput) {
        verificationLocalUrlInput.value = data.citation_verification_local_url;
      }
      if (data.citation_verification_auto_start_local !== undefined && autoStartLayaToggle) {
        autoStartLayaToggle.checked = Boolean(data.citation_verification_auto_start_local);
      }
      savedVerificationEnabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
      savedVerificationProvider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
      savedVerificationModel = (data.citation_verification_model && data.citation_verification_model !== "laya")
        ? data.citation_verification_model
        : (verificationModelSelect && verificationModelSelect.value && verificationModelSelect.value !== "laya" ? verificationModelSelect.value : "inception/mercury-decide");
      savedVerificationThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
      savedVerificationAutoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;
      updateVerificationUiVisibility();
      updateVerificationDirtyState();
      updateVerificationModelMetadata();
      if (verificationProviderSelect && verificationProviderSelect.value === "local_laya") {
        checkLayaStatus();
      }
      if (data.vlm_captioning_provider) {
        savedVlmProvider = data.vlm_captioning_provider;
        setVlmProvider(savedVlmProvider);
      }
      if (data.vlm_captioning_model) {
        savedVlmCloudModel = data.vlm_captioning_model;
      }
      if (data.vlm_captioning_backup_model) {
        savedVlmBackupModel = data.vlm_captioning_backup_model;
      }
      renderVlmModels(savedVlmCloudModel, savedVlmBackupModel);
      if (data.vlm_captioning_local_model !== undefined) {
        savedVlmLocalModel = data.vlm_captioning_local_model || "";
        if (vlmLocalModelInput) vlmLocalModelInput.value = savedVlmLocalModel;
      }
      if (data.vlm_max_images_per_doc !== undefined) {
        savedVlmMaxImages = parseInt(data.vlm_max_images_per_doc, 10) || 100;
        if (vlmMaxImagesInput) vlmMaxImagesInput.value = savedVlmMaxImages;
      }
      if (data.datalab_merge_cross_page !== undefined) {
        savedDatalabMergeCrossPage = Boolean(data.datalab_merge_cross_page);
        if (datalabMergeCrossPageCheckbox) {
          datalabMergeCrossPageCheckbox.checked = savedDatalabMergeCrossPage;
        }
      }
      updateVlmUiVisibility();
      updateConverterDirtyState();
    } catch (err) {
      Zotero.debug("Zocrates: Error fetching status: " + err);
    }
  }

  // Fetch LLM configuration from FastAPI backend
  async function fetchLlmConfig() {
    const backendUrl = getBackendUrl();
    const configUrl = `${backendUrl}/config`;

    try {
      const res = await fetch(configUrl, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.status !== "success") return;

      if (llmProviderSelect && data.llm_provider) {
        llmProviderSelect.value = data.llm_provider;
        savedLlmProvider = data.llm_provider;
      }
      if (llmBaseUrlInput && data.llm_base_url) {
        llmBaseUrlInput.value = data.llm_base_url;
        savedLlmBaseUrl = data.llm_base_url.trim();
      } else {
        savedLlmBaseUrl = "";
      }

      if (data.llm_model) {
        savedLlmModel = data.llm_model.trim();
      }

      if (data.embedding_provider) {
        savedEmbeddingMode = (data.embedding_provider === "cloud") ? "cloud" : "local";
        if (modeSelect) modeSelect.value = savedEmbeddingMode;
      }
      if (data.embedding_model) {
        savedEmbeddingModel = data.embedding_model.trim();
      }

      if (enableCitationsCheckbox && data.citations_enabled !== undefined) {
        enableCitationsCheckbox.checked = Boolean(data.citations_enabled);
      }
      if (enableReferencesCheckbox && data.references_section_enabled !== undefined) {
        enableReferencesCheckbox.checked = Boolean(data.references_section_enabled);
      }
      if (autoRenderMermaidCheckbox && data.auto_render_mermaid_diagrams !== undefined) {
        autoRenderMermaidCheckbox.checked = Boolean(data.auto_render_mermaid_diagrams);
      }
      if (enableMcpServerCheckbox && data.enable_mcp_server !== undefined) {
        enableMcpServerCheckbox.checked = Boolean(data.enable_mcp_server);
      }
      if (enablePredictiveGhostInputCheckbox && data.enable_predictive_ghost_input !== undefined) {
        enablePredictiveGhostInputCheckbox.checked = Boolean(data.enable_predictive_ghost_input);
      }
      if (scholarPoliteEmailInput && data.scholar_polite_email !== undefined) {
        scholarPoliteEmailInput.value = data.scholar_polite_email || "";
      }
      if (sessionRetentionDaysInput && data.session_retention_days !== undefined) {
        sessionRetentionDaysInput.value = data.session_retention_days;
      }
      if (maxSavedSessionsInput && data.max_saved_sessions !== undefined) {
        maxSavedSessionsInput.value = data.max_saved_sessions;
      }
      if (pruneEmptySessionsCheckbox && data.prune_empty_sessions !== undefined) {
        pruneEmptySessionsCheckbox.checked = Boolean(data.prune_empty_sessions);
      }
      if (sessionAutoVacuumCheckbox && data.session_db_auto_vacuum_on_startup !== undefined) {
        sessionAutoVacuumCheckbox.checked = Boolean(data.session_db_auto_vacuum_on_startup);
      }
      if (data.session_storage_stats) {
        updateSessionStorageStatus(data.session_storage_stats);
      }

      if (verificationEnabledToggle && data.citation_verification_enabled !== undefined) {
        verificationEnabledToggle.checked = Boolean(data.citation_verification_enabled);
      }
      if (verificationProviderSelect && data.citation_verification_provider) {
        verificationProviderSelect.value = data.citation_verification_provider;
      }
      if (verificationModelSelect && data.citation_verification_model && data.citation_verification_model !== "laya") {
        verificationModelSelect.value = data.citation_verification_model;
      }
      if (verificationThresholdInput && data.citation_verification_threshold !== undefined) {
        verificationThresholdInput.value = Number(data.citation_verification_threshold).toFixed(2);
        if (verificationThresholdValue) verificationThresholdValue.textContent = Number(data.citation_verification_threshold).toFixed(2);
      }
      if (verificationLocalUrlInput && data.citation_verification_local_url) {
        verificationLocalUrlInput.value = data.citation_verification_local_url;
      }
      if (autoStartLayaToggle && data.citation_verification_auto_start_local !== undefined) {
        autoStartLayaToggle.checked = Boolean(data.citation_verification_auto_start_local);
      }
      savedVerificationEnabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
      savedVerificationProvider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
      savedVerificationModel = (data.citation_verification_model && data.citation_verification_model !== "laya")
        ? data.citation_verification_model
        : (verificationModelSelect && verificationModelSelect.value && verificationModelSelect.value !== "laya" ? verificationModelSelect.value : "inception/mercury-decide");
      savedVerificationThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
      savedVerificationAutoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;
      updateVerificationUiVisibility();

      updateLlmUiVisibility();
      updateEmbeddingUiVisibility();

      if (data.has_openrouter_key && openrouterKeyMissingNotice) {
        openrouterKeyMissingNotice.style.display = "none";
      }

      // If OpenRouter provider, fetch models catalog
      if (data.llm_provider === "openrouter_llm") {
        await fetchOpenRouterModels("llm", false, data.llm_model);
      } else if (llmModelCustomInput && data.llm_model) {
        llmModelCustomInput.value = data.llm_model;
      }

      if (savedEmbeddingMode === "cloud") {
        await fetchOpenRouterModels("embedding", false, data.embedding_model);
      }
      await fetchOpenRouterModels("decision", false, data.citation_verification_model !== "laya" ? data.citation_verification_model : null);
      updateVerificationModelMetadata();

      if (data.datalab_merge_cross_page !== undefined) {
        savedDatalabMergeCrossPage = Boolean(data.datalab_merge_cross_page);
        if (datalabMergeCrossPageCheckbox) {
          datalabMergeCrossPageCheckbox.checked = savedDatalabMergeCrossPage;
        }
        updateConverterDirtyState();
      }

      updateLlmDirtyState();
      updateEmbeddingDirtyState();
      updateVerificationDirtyState();
    } catch (err) {
      Zotero.debug("Zocrates: Error fetching LLM config: " + err);
    }
  }

  // Fetch converter configuration from FastAPI backend
  async function fetchConverterSettings() {
    const backendUrl = getBackendUrl();
    const configUrl = `${backendUrl}/converters`;

    try {
      const res = await fetch(configUrl, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.status !== "success") return;

      if (converterSelect && data.document_converter_id) {
        converterSelect.value = data.document_converter_id;
        savedConverterId = data.document_converter_id;
      }
      savedConverterMode = (savedConverterId === "datalab") ? "cloud" : "local";

      if (datalabApiKeyInput && !datalabApiKeyInput.value.trim() && data.has_datalab_key) {
        // Backend has key configured in memory or environment
        savedDatalabApiKey = savedDatalabApiKey || "";
      }

      if (data.datalab_non_pdf_fallback) {
        savedDatalabFallback = data.datalab_non_pdf_fallback;
        setDatalabFallback(savedDatalabFallback);
      }

      if (data.datalab_merge_cross_page !== undefined) {
        savedDatalabMergeCrossPage = Boolean(data.datalab_merge_cross_page);
        if (datalabMergeCrossPageCheckbox) {
          datalabMergeCrossPageCheckbox.checked = savedDatalabMergeCrossPage;
        }
      }

      updateConverterInfoMessage();
      updateConverterDirtyState();
    } catch (err) {
      Zotero.debug("Zocrates: Error fetching converter settings: " + err);
    }
  }

  // Update UI control visibility based on selected LLM Provider
  function updateLlmUiVisibility() {
    if (!llmProviderSelect) return;
    const provider = llmProviderSelect.value;
    const keyVal = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : "";

    if (openrouterKeyGroup) openrouterKeyGroup.style.display = "flex";

    if (provider === "openrouter_llm") {
      if (openrouterKeyMissingNotice) {
        openrouterKeyMissingNotice.style.display = keyVal ? "none" : "block";
      }
      if (llmBaseUrlGroup) llmBaseUrlGroup.style.display = "none";
      if (vllmGuidanceBox) vllmGuidanceBox.style.display = "none";
      if (llmFilterChipsContainer) llmFilterChipsContainer.style.display = "flex";
      if (llmModelSearchContainer) llmModelSearchContainer.style.display = "block";
      if (llmModelSelect) llmModelSelect.style.display = "block";
      if (llmModelCustomInput) llmModelCustomInput.style.display = "none";
      if (llmRefreshBtn) llmRefreshBtn.style.display = "inline-block";
    } else {
      if (openrouterKeyMissingNotice) openrouterKeyMissingNotice.style.display = "none";
      if (llmBaseUrlGroup) llmBaseUrlGroup.style.display = "flex";
      if (llmFilterChipsContainer) llmFilterChipsContainer.style.display = "none";
      if (llmModelSearchContainer) llmModelSearchContainer.style.display = "none";
      if (llmModelSelect) llmModelSelect.style.display = "none";
      if (llmModelMetadataBox) llmModelMetadataBox.style.display = "none";
      if (llmRefreshBtn) llmRefreshBtn.style.display = "none";
      if (llmModelCustomInput) {
        llmModelCustomInput.style.display = "block";
        llmModelCustomInput.placeholder = "Enter the model name";
      }

      if (provider === "ollama") {
        if (llmBaseUrlInput && !llmBaseUrlInput.value) llmBaseUrlInput.value = "http://localhost:11434";
        if (vllmGuidanceBox) vllmGuidanceBox.style.display = "none";
      } else if (provider === "vllm") {
        if (llmBaseUrlInput && !llmBaseUrlInput.value) llmBaseUrlInput.value = "http://localhost:8000/v1";
        if (vllmGuidanceBox) vllmGuidanceBox.style.display = "block";
      } else if (provider === "openai_compatible") {
        if (vllmGuidanceBox) vllmGuidanceBox.style.display = "none";
      }
    }

    updateEmbeddingUiVisibility();
  }

  function updateEmbeddingUiVisibility() {
    if (!modeSelect) return;
    const mode = modeSelect.value;
    const keyVal = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : "";

    if (mode === "cloud") {
      if (embeddingKeyMissingNotice) {
        embeddingKeyMissingNotice.style.display = keyVal ? "none" : "block";
      }
      if (embeddingRefreshBtn) embeddingRefreshBtn.style.display = "inline-block";

      if (rawEmbeddingModels && rawEmbeddingModels.length > 0) {
        renderEmbeddingModels();
      } else {
        fetchOpenRouterModels("embedding", false);
      }
    } else {
      if (embeddingKeyMissingNotice) embeddingKeyMissingNotice.style.display = "none";
      if (embeddingRefreshBtn) embeddingRefreshBtn.style.display = "none";
      if (embeddingModelMetadataBox) embeddingModelMetadataBox.style.display = "none";

      if (embeddingModelSelect) {
        embeddingModelSelect.innerHTML = "";
        const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        opt.value = "jinaai/jina-embeddings-v5-text-nano";
        opt.textContent = "jinaai/jina-embeddings-v5-text-nano (768d)";
        embeddingModelSelect.appendChild(opt);
        embeddingModelSelect.value = "jinaai/jina-embeddings-v5-text-nano";
      }
    }
  }

  function renderEmbeddingModels(activeModelToSelect = null) {
    if (!embeddingModelSelect || !rawEmbeddingModels) return;
    if (modeSelect && modeSelect.value !== "cloud") return;

    const currentSel = activeModelToSelect || embeddingModelSelect.value || "openrouter/nvidia/nemotron-3-embed-1b:free";
    embeddingModelSelect.innerHTML = "";

    rawEmbeddingModels.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    rawEmbeddingModels.forEach((m) => {
      const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
      opt.value = m.id;
      opt.textContent = m.name || m.id;
      embeddingModelSelect.appendChild(opt);
    });

    if (currentSel) {
      for (let i = 0; i < embeddingModelSelect.options.length; i++) {
        const val = embeddingModelSelect.options[i].value;
        if (val === currentSel || val === `openrouter/${currentSel}` || `openrouter/${val}` === currentSel) {
          embeddingModelSelect.selectedIndex = i;
          break;
        }
      }
    }
    updateEmbeddingModelMetadata();
    updateEmbeddingDirtyState();
  }

  function renderDecisionModels(activeModelToSelect = null) {
    if (!verificationModelSelect || !rawDecisionModels || rawDecisionModels.length === 0) return;

    let currentSel = activeModelToSelect || verificationModelSelect.value || "inception/mercury-decide";
    if (!currentSel || currentSel === "laya") {
      currentSel = "inception/mercury-decide";
    }
    verificationModelSelect.innerHTML = "";

    rawDecisionModels.sort((a, b) => (a.name || a.id || "").localeCompare(b.name || b.id || ""));
    rawDecisionModels.forEach((m) => {
      const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
      opt.value = m.id;
      let ctx = "Unknown";
      if (m.context_length) {
        ctx = m.context_length >= 1024 ? `${Math.round(m.context_length / 1024)}k` : `${m.context_length}`;
      }
      opt.textContent = `${m.name || m.id} (${ctx} Context)`;
      verificationModelSelect.appendChild(opt);
    });

    const opts = (verificationModelSelect.options && verificationModelSelect.options.length > 0)
      ? verificationModelSelect.options
      : verificationModelSelect.children;
    if (currentSel) {
      let matched = false;
      for (let i = 0; i < opts.length; i++) {
        const val = opts[i].value;
        if (val === currentSel || val.replace(/^openrouter\//, "") === currentSel.replace(/^openrouter\//, "")) {
          verificationModelSelect.selectedIndex = i;
          verificationModelSelect.value = val;
          matched = true;
          break;
        }
      }
      if (!matched && currentSel && currentSel !== "laya") {
        const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        opt.value = currentSel;
        opt.textContent = currentSel;
        verificationModelSelect.appendChild(opt);
        verificationModelSelect.value = currentSel;
      }
    }
    updateVerificationModelMetadata();
    updateVerificationDirtyState();
  }

  function renderModelMetadataBox(metadataBox, modsText, ctxText, priceText) {
    if (!metadataBox) return;
    metadataBox.innerHTML = "";
    const container = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    container.style.cssText = "font-size: 11px; color: var(--zc-text-muted); line-height: 1.5; padding: 8px 10px; background: var(--zc-input-bg); border: 1px solid var(--zc-input-border); border-radius: 6px; margin-top: 6px;";

    const row1 = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    const bold1 = document.createElementNS("http://www.w3.org/1999/xhtml", "b");
    bold1.textContent = modsText;
    row1.appendChild(bold1);
    container.appendChild(row1);

    const row2 = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    row2.textContent = ctxText;
    container.appendChild(row2);

    const row3 = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    row3.textContent = priceText;
    container.appendChild(row3);

    metadataBox.appendChild(container);
    metadataBox.style.display = "block";
  }

  function updateVerificationModelMetadata(selectedId = null) {
    if (!verificationModelMetadataBox) return;
    if (!verificationProviderSelect || verificationProviderSelect.value !== "openrouter") {
      verificationModelMetadataBox.style.display = "none";
      return;
    }
    if (!rawDecisionModels || rawDecisionModels.length === 0) {
      verificationModelMetadataBox.style.display = "none";
      return;
    }

    const currentId = selectedId || (verificationModelSelect ? verificationModelSelect.value : "");
    if (!currentId) {
      verificationModelMetadataBox.style.display = "none";
      return;
    }

    const model = rawDecisionModels.find((m) => {
      const mClean = (m.id || "").replace(/^openrouter\//, "");
      const cClean = (currentId || "").replace(/^openrouter\//, "");
      return m.id === currentId || mClean === cClean;
    });

    if (!model) {
      verificationModelMetadataBox.style.display = "none";
      return;
    }

    const arch = model.architecture || {};
    let inMods = model.input_modalities || arch.input_modalities || ["text"];
    if (Array.isArray(inMods)) inMods = inMods.join(", ");
    const modsText = `Modalities: ${inMods} ➔ decisions`;

    const ctxLen = model.context_length || 0;
    const ctxText = ctxLen ? `Context: ${ctxLen.toLocaleString()} tokens` : "Context: Unknown";

    const pricing = model.pricing || {};
    const promptPrice = parseFloat(pricing.prompt || 0) * 1000000;
    const requestPrice = parseFloat(pricing.request || 0);
    let priceText = "Pricing: Free";
    if (model.is_free || (promptPrice === 0 && requestPrice === 0)) {
      priceText = "Pricing: Free";
    } else {
      let parts = [];
      if (promptPrice > 0) {
        const pStr = promptPrice < 0.01 ? `$${promptPrice.toFixed(4)}` : `$${promptPrice.toFixed(2)}`;
        parts.push(`${pStr} / 1M input`);
      }
      if (requestPrice > 0) {
        const rStr = requestPrice < 0.001 ? `$${requestPrice.toFixed(4)}` : `$${requestPrice.toFixed(3)}`;
        parts.push(`${rStr} / request`);
      }
      priceText = `Pricing: ${parts.join(" · ") || "Free"}`;
    }

    renderModelMetadataBox(verificationModelMetadataBox, modsText, ctxText, priceText);
  }

  function updateEmbeddingModelMetadata(selectedId = null) {
    if (!embeddingModelMetadataBox) return;
    if (!modeSelect || modeSelect.value !== "cloud") {
      embeddingModelMetadataBox.style.display = "none";
      return;
    }

    const currentId = selectedId || (embeddingModelSelect ? embeddingModelSelect.value : "");
    if (!currentId || !rawEmbeddingModels || rawEmbeddingModels.length === 0) {
      embeddingModelMetadataBox.style.display = "none";
      return;
    }

    const model = rawEmbeddingModels.find((m) => {
      const mClean = (m.id || "").replace(/^openrouter\//, "");
      const cClean = (currentId || "").replace(/^openrouter\//, "");
      return m.id === currentId || mClean === cClean;
    });

    if (!model) {
      embeddingModelMetadataBox.style.display = "none";
      return;
    }

    const arch = model.architecture || {};
    let inMods = model.input_modalities || arch.input_modalities || ["text"];
    if (Array.isArray(inMods)) inMods = inMods.join(", ");
    const modsText = `Modalities: ${inMods} ➔ embedding`;

    const ctxLen = model.context_length || 0;
    const ctxText = ctxLen ? `Context: ${ctxLen.toLocaleString()} tokens` : "Context: Unknown";

    let priceText = "Pricing: Unknown";
    const pricing = model.pricing || {};
    if (model.is_free) {
      priceText = "Pricing: ⚡ Free";
    } else if (pricing.prompt) {
      const p = parseFloat(pricing.prompt) * 1000000;
      priceText = `Pricing: $${p.toFixed(3)} / 1M tokens`;
    }

    renderModelMetadataBox(embeddingModelMetadataBox, modsText, ctxText, priceText);
  }

  function getVendorName(model) {
    let id = (model.id || "").toLowerCase();
    if (id.startsWith("openrouter/")) {
      id = id.substring("openrouter/".length);
    }
    const name = (model.name || "").toLowerCase();
    if (id.startsWith("anthropic/") || name.includes("anthropic") || name.includes("claude")) return "Anthropic";
    if (id.startsWith("google/") || name.includes("google") || name.includes("gemma") || name.includes("gemini")) return "Google";
    if (id.startsWith("openai/") || name.includes("openai") || name.includes("gpt")) return "OpenAI";
    if (id.startsWith("deepseek/") || name.includes("deepseek")) return "DeepSeek";
    if (id.startsWith("meta-llama/") || id.startsWith("meta/") || name.includes("meta") || name.includes("llama")) return "Meta";
    if (id.startsWith("mistralai/") || name.includes("mistral") || name.includes("codestral")) return "Mistral AI";
    if (id.startsWith("qwen/") || name.includes("qwen")) return "Qwen";
    if (id.startsWith("nvidia/") || name.includes("nvidia") || name.includes("nemotron")) return "NVIDIA";
    if (id.startsWith("moonshotai/") || id.startsWith("moonshot/") || name.includes("moonshot")) return "MoonshotAI";
    if (id.startsWith("x-ai/") || id.startsWith("xai/") || name.includes("grok") || name.includes("xai") || name.includes("x.ai")) return "xAI";
    if (id.startsWith("zhipu/") || id.startsWith("z-ai/") || id.startsWith("zhipuai/") || name.includes("chatglm") || name.includes("z.ai") || name.includes("glm")) return "Z.ai";
    return "Other Providers";
  }

  function getCleanModelDisplayName(modelName, vendorName) {
    if (!modelName) return "";
    if (vendorName === "Other Providers") return modelName;

    const prefixRegex = new RegExp(`^${vendorName}:\\s*`, "i");
    let clean = modelName.replace(prefixRegex, "");
    clean = clean.replace(/^(anthropic|google|openai|deepseek|meta|meta-llama|mistral|mistralai|qwen|nvidia|moonshotai|moonshot|x-ai|xai|zhipu|zhipuai|z-ai):\s*/i, "");
    return clean;
  }

  function updateLlmModelMetadata(selectedId = null) {
    const metadataBox = document.getElementById("llm-model-metadata-box");
    if (!metadataBox) return;

    const currentId = selectedId || (llmModelSelect ? llmModelSelect.value : "");
    if (!currentId || !rawLlmModels || rawLlmModels.length === 0) {
      metadataBox.style.display = "none";
      return;
    }

    const model = rawLlmModels.find((m) =>
      m.id === currentId || m.id === `openrouter/${currentId}` || `openrouter/${m.id}` === currentId
    );

    if (!model) {
      metadataBox.style.display = "none";
      return;
    }

    // 1. Modalities (input -> output)
    const arch = model.architecture || {};
    let inMods = model.input_modalities || arch.input_modalities || ["text"];
    let outMods = model.output_modalities || arch.output_modalities || ["text"];
    if (Array.isArray(inMods)) inMods = inMods.join(", ");
    if (Array.isArray(outMods)) outMods = outMods.join(", ");
    const modsText = `Modalities: ${inMods} ➔ ${outMods}`;

    // 2. Context Length
    const ctxLen = model.context_length || 0;
    const ctxText = ctxLen ? `Context: ${ctxLen.toLocaleString()} tokens` : "Context: Unknown";

    // 3. Pricing (Prompt & Completion per 1M tokens, plus per-image price if present)
    const pricing = model.pricing || {};
    const promptPrice = parseFloat(pricing.prompt || 0) * 1000000;
    const completionPrice = parseFloat(pricing.completion || 0) * 1000000;
    const imagePrice = parseFloat(pricing.image || 0);

    let priceText = "Pricing: Free";
    if (model.is_free || (promptPrice === 0 && completionPrice === 0 && imagePrice === 0)) {
      priceText = "Pricing: Free";
    } else {
      let parts = [];
      if (promptPrice > 0) {
        const pStr = promptPrice < 0.01 ? `$${promptPrice.toFixed(4)}` : `$${promptPrice.toFixed(2)}`;
        parts.push(`${pStr} / 1M prompt`);
      }
      if (completionPrice > 0) {
        const cStr = completionPrice < 0.01 ? `$${completionPrice.toFixed(4)}` : `$${completionPrice.toFixed(2)}`;
        parts.push(`${cStr} / 1M completion`);
      }
      if (imagePrice > 0) {
        const iStr = imagePrice < 0.001 ? `$${imagePrice.toFixed(4)}` : `$${imagePrice.toFixed(3)}`;
        parts.push(`${iStr} / image`);
      }
      priceText = `Pricing: ${parts.join(" · ") || "Free"}`;
    }

    renderModelMetadataBox(metadataBox, modsText, ctxText, priceText);
  }

  function updateChipStyles() {
    const chipBtns = document.querySelectorAll("#llm-filter-chips .zc-chip-btn");
    chipBtns.forEach((btn) => {
      const filter = btn.getAttribute("data-filter") || "all";
      const isActive = filter === activeChipFilter;
      btn.style.padding = "4px 10px";
      btn.style.fontSize = "11px";
      btn.style.fontWeight = "600";
      btn.style.borderRadius = "12px";
      btn.style.cursor = "pointer";

      if (isActive) {
        btn.classList.add("zc-chip-active");
        if (filter === "free") {
          btn.style.background = "#10b981";
          btn.style.color = "#ffffff";
          btn.style.border = "1px solid #10b981";
        } else {
          btn.style.background = "var(--zc-accent)";
          btn.style.color = "#ffffff";
          btn.style.border = "1px solid var(--zc-accent)";
        }
      } else {
        btn.classList.remove("zc-chip-active");
        if (filter === "free") {
          btn.style.background = "var(--zc-input-bg)";
          btn.style.color = "#10b981";
          btn.style.border = "1px solid #10b981";
        } else {
          btn.style.background = "var(--zc-input-bg)";
          btn.style.color = "var(--zc-text-muted)";
          btn.style.border = "1px solid var(--zc-input-border)";
        }
      }
    });
  }

  function renderFilteredLlmModels(activeModelToSelect = null) {
    if (!llmModelSelect) return;
    if (llmProviderSelect && llmProviderSelect.value !== "openrouter_llm") return;

    const desiredModel = activeModelToSelect || llmModelSelect.value;
    const searchQuery = (llmModelSearchInput ? llmModelSearchInput.value : "").toLowerCase().trim();

    // 1. Strict Batch Model Filtering
    let filtered = rawLlmModels.filter((m) => {
      const idLower = (m.id || "").toLowerCase();
      const nameLower = (m.name || "").toLowerCase();
      if (idLower.includes("batch") || nameLower.includes("batch")) return false;
      return true;
    });

    // 2. Filter Chips Matching
    if (activeChipFilter === "free") {
      filtered = filtered.filter((m) => m.is_free || (m.id || "").endsWith(":free") || (m.id || "").includes(":free"));
    } else if (activeChipFilter !== "all") {
      filtered = filtered.filter((m) => getVendorName(m).toLowerCase() === activeChipFilter.toLowerCase());
    }

    // 3. Search Query Matching
    if (searchQuery) {
      filtered = filtered.filter((m) => {
        const nameLower = (m.name || "").toLowerCase();
        const idLower = (m.id || "").toLowerCase();
        if (searchQuery === "free") {
          return m.is_free || idLower.includes(":free") || nameLower.includes("free");
        }
        return nameLower.includes(searchQuery) || idLower.includes(searchQuery);
      });
    }

    if (llmModelCountBadge) {
      llmModelCountBadge.textContent = `${filtered.length} models available`;
    }

    llmModelSelect.innerHTML = "";
    if (filtered.length === 0) {
      const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
      opt.value = "";
      opt.textContent = "No models match search/filter";
      llmModelSelect.appendChild(opt);
      return;
    }

    // 4. Group by Vendor Optgroups and Sort Alphabetically
    const groups = {};
    const vendorOrder = ["Anthropic", "Google", "OpenAI", "DeepSeek", "Meta", "Mistral AI", "Qwen", "NVIDIA", "MoonshotAI", "xAI", "Z.ai", "Other Providers"];

    filtered.forEach((m) => {
      const v = getVendorName(m);
      if (!groups[v]) groups[v] = [];
      groups[v].push(m);
    });

    vendorOrder.forEach((vendor) => {
      if (!groups[vendor] || groups[vendor].length === 0) return;

      groups[vendor].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
      const optgroup = document.createElementNS("http://www.w3.org/1999/xhtml", "optgroup");
      optgroup.setAttribute("label", `${vendor} (${groups[vendor].length})`);

      groups[vendor].forEach((m) => {
        const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        opt.value = m.id;
        opt.textContent = getCleanModelDisplayName(m.name, vendor);
        optgroup.appendChild(opt);
      });

      llmModelSelect.appendChild(optgroup);
    });

    // 5. Restore Selected Option
    if (desiredModel) {
      let matched = false;
      for (let i = 0; i < llmModelSelect.options.length; i++) {
        const val = llmModelSelect.options[i].value;
        if (val === desiredModel || val === `openrouter/${desiredModel}` || `openrouter/${val}` === desiredModel) {
          llmModelSelect.selectedIndex = i;
          matched = true;
          break;
        }
      }
      if (!matched && llmModelSelect.options.length > 0) {
        llmModelSelect.selectedIndex = 0;
      }
    }

    updateLlmModelMetadata();
    updateLlmDirtyState();
  }

  function updateVlmModelMetadata(selectElem, metadataBox) {
    if (!selectElem || !metadataBox) return;
    const currentId = selectElem.value || "";
    if (!currentId || !rawLlmModels || rawLlmModels.length === 0) {
      metadataBox.style.display = "none";
      return;
    }

    const model = rawLlmModels.find((m) =>
      m.id === currentId || m.id === `openrouter/${currentId}` || `openrouter/${m.id}` === currentId
    );

    if (!model) {
      metadataBox.style.display = "none";
      return;
    }

    // 1. Modalities (input -> output)
    const arch = model.architecture || {};
    let inMods = model.input_modalities || arch.input_modalities || ["text", "image"];
    let outMods = model.output_modalities || arch.output_modalities || ["text"];
    if (Array.isArray(inMods)) inMods = inMods.join(", ");
    if (Array.isArray(outMods)) outMods = outMods.join(", ");
    const modsText = `Modalities: ${inMods} ➔ ${outMods}`;

    // 2. Context Length
    const ctxLen = model.context_length || 0;
    const ctxText = ctxLen ? `Context: ${ctxLen.toLocaleString()} tokens` : "Context: Unknown";

    // 3. Pricing (Prompt & Completion per 1M tokens, plus per-image price if present)
    const pricing = model.pricing || {};
    const promptPrice = parseFloat(pricing.prompt || 0) * 1000000;
    const completionPrice = parseFloat(pricing.completion || 0) * 1000000;
    const imagePrice = parseFloat(pricing.image || 0);

    let priceText = "Pricing: Free";
    if (model.is_free || (promptPrice === 0 && completionPrice === 0 && imagePrice === 0)) {
      priceText = "Pricing: Free";
    } else {
      let parts = [];
      if (promptPrice > 0) {
        const pStr = promptPrice < 0.01 ? `$${promptPrice.toFixed(4)}` : `$${promptPrice.toFixed(2)}`;
        parts.push(`${pStr} / 1M prompt`);
      }
      if (completionPrice > 0) {
        const cStr = completionPrice < 0.01 ? `$${completionPrice.toFixed(4)}` : `$${completionPrice.toFixed(2)}`;
        parts.push(`${cStr} / 1M completion`);
      }
      if (imagePrice > 0) {
        const iStr = imagePrice < 0.001 ? `$${imagePrice.toFixed(4)}` : `$${imagePrice.toFixed(3)}`;
        parts.push(`${iStr} / image`);
      }
      priceText = `Pricing: ${parts.join(" · ") || "Free"}`;
    }

    renderModelMetadataBox(metadataBox, modsText, ctxText, priceText);
  }

  function renderVlmDropdown(selectElem, metadataBox, desiredModel, defaultModel) {
    if (!selectElem) return;

    const targetModel = desiredModel || selectElem.value || defaultModel;

    while (selectElem.firstChild) {
      selectElem.removeChild(selectElem.firstChild);
    }

    // Filter rawLlmModels for vision models: text + image in input, text in output
    let visionModels = (rawLlmModels || []).filter((m) => {
      const arch = m.architecture || {};
      let inMods = m.input_modalities || arch.input_modalities || [];
      let outMods = m.output_modalities || arch.output_modalities || [];
      if ((!inMods || inMods.length === 0) && arch.modality) {
        const parts = arch.modality.split("->");
        inMods = parts[0].split("+").map((s) => s.trim());
        if (parts[1]) outMods = parts[1].split("+").map((s) => s.trim());
      }
      const idLower = (m.id || "").toLowerCase();
      const nameLower = (m.name || "").toLowerCase();
      if (idLower.includes("batch") || nameLower.includes("batch")) return false;
      const hasTextIn = inMods.includes("text");
      const hasImageIn = inMods.includes("image");
      const hasTextOut = outMods.length === 0 || outMods.includes("text");
      return hasTextIn && hasImageIn && hasTextOut;
    });

    // Ensure known default models exist even before network fetch returns
    const knownDefaultModels = [
      {
        id: "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        name: "NVIDIA: Nemotron-3 Nano Omni 30B Reasoning (free)",
        is_free: true,
        input_modalities: ["text", "image", "audio", "video"],
        output_modalities: ["text"],
      },
      {
        id: "openrouter/qwen/qwen3.7-flash",
        name: "Qwen: Qwen 3.7 Flash",
        is_free: false,
        input_modalities: ["text", "image", "video"],
        output_modalities: ["text"],
      },
      {
        id: "openrouter/google/gemma-4-26b-a4b-it:free",
        name: "Google: Gemma 4 26B A4B Instruct (free)",
        is_free: true,
        input_modalities: ["text", "image"],
        output_modalities: ["text"],
      },
    ];

    for (const dm of knownDefaultModels) {
      if (!visionModels.some((m) => m.id === dm.id || `openrouter/${m.id}` === dm.id)) {
        visionModels.push(dm);
      }
    }

    // Group by Vendor Optgroups and Sort Alphabetically
    const groups = {};
    const vendorOrder = ["Google", "NVIDIA", "Anthropic", "OpenAI", "Meta", "Qwen", "Mistral AI", "DeepSeek", "xAI", "MoonshotAI", "Z.ai", "Other Providers"];

    visionModels.forEach((m) => {
      const v = getVendorName(m);
      if (!groups[v]) groups[v] = [];
      groups[v].push(m);
    });

    vendorOrder.forEach((vendor) => {
      if (!groups[vendor] || groups[vendor].length === 0) return;

      groups[vendor].sort((a, b) => {
        if (a.is_free && !b.is_free) return -1;
        if (!a.is_free && b.is_free) return 1;
        return (a.name || "").localeCompare(b.name || "");
      });

      const optgroup = document.createElementNS("http://www.w3.org/1999/xhtml", "optgroup");
      optgroup.setAttribute("label", `${vendor} (${groups[vendor].length})`);

      groups[vendor].forEach((m) => {
        const opt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        opt.value = m.id;
        opt.textContent = getCleanModelDisplayName(m.name, vendor);
        optgroup.appendChild(opt);
      });

      selectElem.appendChild(optgroup);
    });

    // Select desired option or append if custom
    if (targetModel) {
      let matched = false;
      const targetClean = targetModel.replace(/^openrouter\//i, "").toLowerCase();
      for (let i = 0; i < selectElem.options.length; i++) {
        const val = selectElem.options[i].value;
        const valClean = (val || "").replace(/^openrouter\//i, "").toLowerCase();
        if (val === targetModel || valClean === targetClean) {
          selectElem.selectedIndex = i;
          matched = true;
          break;
        }
      }
      if (!matched && targetModel) {
        const customOpt = document.createElementNS("http://www.w3.org/1999/xhtml", "option");
        customOpt.value = targetModel;
        customOpt.textContent = targetModel.replace(/^openrouter\//i, "");
        selectElem.appendChild(customOpt);
        selectElem.value = targetModel;
      }
    }

    updateVlmModelMetadata(selectElem, metadataBox);
  }

  function renderVlmModels(desiredPrimary = null, desiredBackup = null) {
    const pri = desiredPrimary || (vlmPrimaryModelSelect ? vlmPrimaryModelSelect.value : savedVlmCloudModel);
    const bak = desiredBackup || (vlmBackupModelSelect ? vlmBackupModelSelect.value : savedVlmBackupModel);
    renderVlmDropdown(vlmPrimaryModelSelect, vlmPrimaryMetadataBox, pri, "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free");
    renderVlmDropdown(vlmBackupModelSelect, vlmBackupMetadataBox, bak, "openrouter/qwen/qwen3.7-flash");
  }

  // Fetch OpenRouter model catalog for dropdown population
  async function fetchOpenRouterModels(targetType = "llm", forceRefresh = false, activeModelToSelect = null) {
    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/llm/openrouter_models?target_type=${targetType}&refresh=${forceRefresh}`;

    try {
      const res = await fetch(url, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) return;
      const data = await res.json();
      if ((data.status !== "ok" && data.status !== "success") || !Array.isArray(data.models)) return;

      if (targetType === "embedding") {
        rawEmbeddingModels = data.models;
        renderEmbeddingModels(activeModelToSelect);
      } else if (targetType === "decision") {
        rawDecisionModels = data.models;
        renderDecisionModels(activeModelToSelect);
      } else {
        rawLlmModels = data.models;
        renderFilteredLlmModels(activeModelToSelect);
        renderVlmModels(savedVlmCloudModel, savedVlmBackupModel);
      }
    } catch (err) {
      Zotero.debug(`Zocrates: Error fetching OpenRouter ${targetType} models: ${err}`);
    }
  }

  // Save full LLM configuration to backend
  async function saveLlmConfig() {
    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/config`;

    const provider = llmProviderSelect ? llmProviderSelect.value : "openrouter_llm";
    const model = (provider === "openrouter_llm" ? (llmModelSelect ? llmModelSelect.value : "") : (llmModelCustomInput ? llmModelCustomInput.value : "")).trim();
    const baseUrl = llmBaseUrlInput ? llmBaseUrlInput.value.trim() : null;
    const openrouterKey = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : null;
    const embeddingProv = modeSelect ? modeSelect.value : "local";
    let embeddingModel = "jinaai/jina-embeddings-v5-text-nano";
    if (embeddingProv === "cloud") {
      embeddingModel = (embeddingModelSelect ? embeddingModelSelect.value : "") || "openrouter/nvidia/nemotron-3-embed-1b:free";
    }

    const payload = {
      llm_provider: provider,
      llm_model: model,
      llm_base_url: baseUrl,
      embedding_provider: embeddingProv,
      embedding_model: embeddingModel,
      openrouter_api_key: openrouterKey,
    };

    if (enableCitationsCheckbox) {
      payload.citations_enabled = enableCitationsCheckbox.checked;
    }
    if (enableReferencesCheckbox) {
      payload.references_section_enabled = enableReferencesCheckbox.checked;
    }
    if (autoRenderMermaidCheckbox) {
      payload.auto_render_mermaid_diagrams = autoRenderMermaidCheckbox.checked;
    }
    if (enableMcpServerCheckbox) {
      payload.enable_mcp_server = enableMcpServerCheckbox.checked;
    }
    if (enablePredictiveGhostInputCheckbox) {
      payload.enable_predictive_ghost_input = enablePredictiveGhostInputCheckbox.checked;
    }
    if (scholarPoliteEmailInput) {
      payload.scholar_polite_email = scholarPoliteEmailInput.value.trim();
    }
    if (openAlexApiKeyInput) {
      const openAlexKeyVal = openAlexApiKeyInput.value.trim();
      payload.openalex_api_key = openAlexKeyVal;
      saveSecureApiKey("openalex_api_key", openAlexKeyVal);
    }

    if (openrouterKey) {
      saveSecureApiKey("openrouter_api_key", openrouterKey);
    }

    try {
      const res = await postLlmConfig(payload);
      if (res && res.ok) {
        if (typeof Zotero !== "undefined" && Zotero.Prefs) {
          try {
            Zotero.Prefs.set("zocrates.llm_provider", provider);
            Zotero.Prefs.set("zocrates.llm_model", model);
            Zotero.Prefs.set("zocrates.llm_config_timestamp", Date.now());
            if (autoRenderMermaidCheckbox) {
              savePreference("autoRenderMermaid", autoRenderMermaidCheckbox.checked);
            }
            if (enableMcpServerCheckbox) {
              savePreference("enableMcpServer", enableMcpServerCheckbox.checked);
            }
            if (enablePredictiveGhostInputCheckbox) {
              savePreference("enablePredictiveGhostInput", enablePredictiveGhostInputCheckbox.checked);
            }
            if (scholarPoliteEmailInput) {
              savePreference("scholarPoliteEmail", scholarPoliteEmailInput.value.trim());
            }
          } catch (e) {}
        }
        try {
          const win = (window.parent && window.parent.Services) ? window.parent : (window.top && window.top.Services ? window.top : window);
          if (win && win.Services && win.Services.obs) {
            win.Services.obs.notifyObservers(null, "zocrates-llm-config-updated", model);
          }
        } catch (e) {}
        await fetchStatus();
      }
    } catch (err) {
      Zotero.debug("Zocrates: Error saving LLM config: " + err);
    }
  }

  // Test LLM Connection helper (Decoupled from saving config)
  async function testLlmConnection() {
    if (!llmTestStatusBadge) return;
    llmTestStatusBadge.style.display = "block";
    llmTestStatusBadge.textContent = "⏳ Testing...";
    llmTestStatusBadge.className = "zc-status-text zc-text-warning";

    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/llm/test`;

    const provider = llmProviderSelect ? llmProviderSelect.value : "openrouter_llm";
    let model = (provider === "openrouter_llm" ? (llmModelSelect ? llmModelSelect.value : "") : (llmModelCustomInput ? llmModelCustomInput.value : "")).trim();
    if (!model || model === "free" || model === "openrouter/free") {
      model = "google/gemini-3.8-flash";
    }
    const baseUrl = llmBaseUrlInput ? llmBaseUrlInput.value.trim() : null;
    const openrouterKey = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : null;

    const payload = {
      llm_provider: provider,
      llm_model: model,
      llm_base_url: baseUrl,
      openrouter_api_key: openrouterKey,
    };

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && data.status === "ok") {
        llmTestStatusBadge.textContent = `✓ ${data.message || "Connected!"}`;
        llmTestStatusBadge.className = "zc-status-text zc-text-success";
      } else {
        llmTestStatusBadge.textContent = `✗ Error: ${data.detail || data.message || "Connection failed"}`;
        llmTestStatusBadge.className = "zc-status-text zc-text-error";
      }
    } catch (err) {
      llmTestStatusBadge.textContent = "✗ Network Error (Server Offline)";
      llmTestStatusBadge.className = "zc-status-text zc-text-error";
    }
  }

  // Test Embedding Connection helper
  async function testEmbeddingConnection() {
    if (!embeddingTestStatusBadge) return;
    embeddingTestStatusBadge.style.display = "block";
    embeddingTestStatusBadge.textContent = "⏳ Testing...";
    embeddingTestStatusBadge.className = "zc-status-text zc-text-warning";

    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/llm/test_embedding`;

    const embeddingProv = modeSelect ? modeSelect.value : "local";
    let embeddingModel = "jinaai/jina-embeddings-v5-text-nano";
    if (embeddingProv === "cloud") {
      embeddingModel = (embeddingModelSelect ? embeddingModelSelect.value : "") || "openrouter/nvidia/nemotron-3-embed-1b:free";
    }
    const openrouterKey = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : null;

    const payload = {
      embedding_provider: embeddingProv,
      embedding_model: embeddingModel,
      openrouter_api_key: openrouterKey,
    };

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && data.status === "ok") {
        embeddingTestStatusBadge.textContent = `✓ ${data.message || "Connected!"}`;
        embeddingTestStatusBadge.className = "zc-status-text zc-text-success";
      } else {
        embeddingTestStatusBadge.textContent = `✗ Error: ${data.detail || data.message || "Connection failed"}`;
        embeddingTestStatusBadge.className = "zc-status-text zc-text-error";
      }
    } catch (err) {
      embeddingTestStatusBadge.textContent = "✗ Network Error (Server Offline)";
      embeddingTestStatusBadge.className = "zc-status-text zc-text-error";
    }
  }

  // Event Listeners for LLM Controls
  if (openRouterApiKeyInput) {
    openRouterApiKeyInput.addEventListener("input", () => {
      updateLlmDirtyState();
      updateEmbeddingDirtyState();
    });
    openRouterApiKeyInput.addEventListener("change", () => {
      updateLlmDirtyState();
      updateEmbeddingDirtyState();
    });
  }

  if (llmProviderSelect) {
    llmProviderSelect.addEventListener("change", () => {
      updateLlmUiVisibility();
      updateLlmDirtyState();
      if (llmProviderSelect.value === "openrouter_llm") {
        if (rawLlmModels.length === 0) {
          fetchOpenRouterModels("llm", false, savedLlmModel);
        } else {
          renderFilteredLlmModels();
        }
      }
    });
  }

  if (llmModelSearchInput) {
    llmModelSearchInput.addEventListener("input", () => {
      renderFilteredLlmModels();
      updateLlmDirtyState();
    });
  }

  if (llmFilterChipsContainer) {
    const chips = llmFilterChipsContainer.querySelectorAll(".zc-chip-btn");
    chips.forEach((chip) => {
      chip.addEventListener("click", () => {
        activeChipFilter = chip.getAttribute("data-filter") || "all";
        updateChipStyles();
        renderFilteredLlmModels();
        updateLlmDirtyState();
      });
    });
    updateChipStyles();
  }

  if (llmModelSelect) {
    llmModelSelect.addEventListener("change", () => {
      updateLlmModelMetadata();
      updateLlmDirtyState();
    });
  }

  if (llmModelCustomInput) {
    llmModelCustomInput.addEventListener("input", updateLlmDirtyState);
    llmModelCustomInput.addEventListener("change", updateLlmDirtyState);
  }

  if (llmBaseUrlInput) {
    llmBaseUrlInput.addEventListener("input", updateLlmDirtyState);
    llmBaseUrlInput.addEventListener("change", updateLlmDirtyState);
  }

  if (llmApplyBtn) {
    llmApplyBtn.addEventListener("click", async () => {
      if (llmApplyBtn.hasAttribute("disabled")) return;

      if (openRouterApiKeyInput && openRouterApiKeyInput.value.trim()) {
        const val = openRouterApiKeyInput.value.trim();
        saveSecureApiKey("openrouter_api_key", val);
      }
      await saveLlmConfig();
      if (llmProviderSelect && llmProviderSelect.value === "openrouter_llm") {
        await fetchOpenRouterModels("llm", true);
      }

      savedLlmProvider = llmProviderSelect ? llmProviderSelect.value : "openrouter_llm";
      savedLlmBaseUrl = llmBaseUrlInput ? llmBaseUrlInput.value.trim() : "";
      savedLlmModel = getEffectiveLlmModel();
      updateLlmDirtyState();

      if (llmTestStatusBadge) {
        llmTestStatusBadge.style.display = "block";
        llmTestStatusBadge.textContent = "✓ LLM settings applied successfully!";
        llmTestStatusBadge.className = "zc-status-text zc-text-success";
        setTimeout(() => {
          if (llmTestStatusBadge.textContent.includes("applied")) {
            llmTestStatusBadge.style.display = "none";
          }
        }, 3500);
      }
    });
  }

  if (llmRefreshBtn) {
    llmRefreshBtn.addEventListener("click", async () => {
      if (openRouterApiKeyInput && openRouterApiKeyInput.value.trim()) {
        saveSecureApiKey("openrouter_api_key", openRouterApiKeyInput.value.trim());
      }
      await saveLlmConfig();
      const currentModel = llmModelSelect ? llmModelSelect.value : null;
      await fetchOpenRouterModels("llm", true, currentModel);
      savedLlmModel = getEffectiveLlmModel();
      updateLlmDirtyState();
    });
  }

  if (llmTestBtn) {
    llmTestBtn.addEventListener("click", testLlmConnection);
  }

  if (modeSelect) {
    modeSelect.addEventListener("change", () => {
      updateEmbeddingUiVisibility();
      updateEmbeddingDirtyState();
      if (modeSelect.value === "cloud" && rawEmbeddingModels.length === 0) {
        fetchOpenRouterModels("embedding", false);
      }
    });
  }

  if (embeddingModelSelect) {
    embeddingModelSelect.addEventListener("change", () => {
      updateEmbeddingModelMetadata();
      updateEmbeddingDirtyState();
    });
  }

  if (embeddingRefreshBtn) {
    embeddingRefreshBtn.addEventListener("click", async () => {
      if (openRouterApiKeyInput && openRouterApiKeyInput.value.trim()) {
        saveSecureApiKey("openrouter_api_key", openRouterApiKeyInput.value.trim());
      }
      await saveLlmConfig();
      const currentModel = embeddingModelSelect ? embeddingModelSelect.value : null;
      await fetchOpenRouterModels("embedding", true, currentModel);
      savedEmbeddingModel = getEffectiveEmbeddingModel();
      updateEmbeddingDirtyState();
    });
  }

  if (embeddingTestBtn) {
    embeddingTestBtn.addEventListener("click", testEmbeddingConnection);
  }

  if (embeddingApplyBtn) {
    embeddingApplyBtn.addEventListener("click", async () => {
      if (embeddingApplyBtn.hasAttribute("disabled")) return;

      if (openRouterApiKeyInput && openRouterApiKeyInput.value.trim()) {
        saveSecureApiKey("openrouter_api_key", openRouterApiKeyInput.value.trim());
      }

      const currentMode = modeSelect ? modeSelect.value : "local";
      const currentModel = getEffectiveEmbeddingModel();

      savePreference("embeddingMode", currentMode);
      savePreference("embeddingModel", currentModel);

      await saveLlmConfig();
      await postConfigChange({ mode: currentMode, model_name: currentModel });

      if (modeSelect && modeSelect.value === "cloud") {
        await fetchOpenRouterModels("embedding", true);
      }

      savedEmbeddingMode = currentMode;
      savedEmbeddingModel = currentModel;
      updateEmbeddingDirtyState();

      if (embeddingTestStatusBadge) {
        embeddingTestStatusBadge.style.display = "block";
        embeddingTestStatusBadge.textContent = "✓ Embeddings settings applied successfully!";
        embeddingTestStatusBadge.className = "zc-status-text zc-text-success";
        setTimeout(() => {
          if (embeddingTestStatusBadge.textContent.includes("applied")) {
            embeddingTestStatusBadge.style.display = "none";
          }
        }, 3500);
      }
    });
  }
  async function postConfigChange(updates) {
    const backendUrl = getBackendUrl() + "/rag/model";

    try {
      const response = await fetch(backendUrl, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(updates)
      });
      if (response.ok) {
        await fetchStatus();
      }
    } catch (err) {
      Zotero.debug("Zocrates: Error posting preference changes: " + err);
    }
  }

  async function postConverterChange(updates) {
    const backendUrl = getBackendUrl() + "/rag/converter";

    try {
      const res = await fetch(backendUrl, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(updates)
      });
      if (!res.ok) {
        Zotero.debug(`Zocrates: Converter change rejected with status: ${res.status}`);
      }
      return res;
    } catch (err) {
      Zotero.debug("Zocrates: Error posting converter changes: " + err);
      return null;
    }
  }

  if (converterSelect) {
    converterSelect.addEventListener("change", () => {
      try {
        updateConverterInfoMessage();
      } catch (e) {
        Zotero.debug("Zocrates: Error updating converter info message: " + e);
      }
      updateConverterDirtyState();
    });
  }

  if (converterApplyBtn) {
    converterApplyBtn.addEventListener("click", async () => {
      if (converterApplyBtn.hasAttribute("disabled")) return;

      const newId = converterSelect ? converterSelect.value : "markitdown";
      const newMode = (newId === "datalab") ? "cloud" : "local";
      const newDatalabKey = datalabApiKeyInput ? datalabApiKeyInput.value.trim() : "";
      const newFallback = getEffectiveDatalabFallback();
      const newMergeCrossPage = datalabMergeCrossPageCheckbox ? datalabMergeCrossPageCheckbox.checked : false;
      const currentVlmEnable = enableVlmCheckbox ? enableVlmCheckbox.checked : true;
      const currentVlmProvider = getEffectiveVlmProvider();
      const currentVlmPrimaryModel = (vlmPrimaryModelSelect ? vlmPrimaryModelSelect.value : "") || "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free";
      const currentVlmBackupModel = (vlmBackupModelSelect ? vlmBackupModelSelect.value : "") || "openrouter/qwen/qwen3.7-flash";
      const currentVlmLocalModel = vlmLocalModelInput ? vlmLocalModelInput.value.trim() : "";
      const currentVlmMaxImages = vlmMaxImagesInput ? (parseInt(vlmMaxImagesInput.value, 10) || 100) : 100;

      savePreference("documentConverterMode", newMode);
      savePreference("documentConverter", newId);
      savePreference("datalabNonPdfFallback", newFallback);
      savePreference("datalabMergeCrossPage", newMergeCrossPage);
      savePreference("enableVlmCaptioning", currentVlmEnable);
      savePreference("vlmCaptioningProvider", currentVlmProvider);
      savePreference("vlmCaptioningModel", currentVlmPrimaryModel);
      savePreference("vlmCaptioningBackupModel", currentVlmBackupModel);
      savePreference("vlmCaptioningLocalModel", currentVlmLocalModel);
      savePreference("vlmMaxImagesPerDoc", currentVlmMaxImages);

      if (newDatalabKey) {
        saveSecureApiKey("datalab_api_key", newDatalabKey);
      }

      const res = await postConverterChange({
        document_converter_mode: newMode,
        document_converter_id: newId,
        converter_id: newId,
        datalab_api_key: newDatalabKey || undefined,
        datalab_non_pdf_fallback: newFallback,
        datalab_merge_cross_page: newMergeCrossPage,
        enable_vlm_captioning: currentVlmEnable,
        vlm_captioning_provider: currentVlmProvider,
        vlm_captioning_model: currentVlmPrimaryModel,
        vlm_captioning_backup_model: currentVlmBackupModel,
        vlm_captioning_local_model: currentVlmLocalModel,
        vlm_max_images_per_doc: currentVlmMaxImages,
      });

      if (!res || !res.ok) {
        if (converterStatusBadge) {
          converterStatusBadge.textContent = "⚠️ Failed to apply converter settings. Backend unreachable or returned error.";
          converterStatusBadge.style.display = "block";
        }
        return;
      }

      savedConverterMode = newMode;
      savedConverterId = newId;
      savedDatalabApiKey = newDatalabKey;
      savedDatalabFallback = newFallback;
      savedDatalabMergeCrossPage = newMergeCrossPage;
      savedVlmEnable = currentVlmEnable;
      savedVlmProvider = currentVlmProvider;
      savedVlmCloudModel = currentVlmPrimaryModel;
      savedVlmBackupModel = currentVlmBackupModel;
      savedVlmLocalModel = currentVlmLocalModel;
      savedVlmMaxImages = currentVlmMaxImages;
      updateConverterDirtyState();

      if (converterStatusBadge) {
        converterStatusBadge.textContent = "✓ Document converter settings applied successfully!";
        converterStatusBadge.style.display = "block";
        setTimeout(() => {
          if (converterStatusBadge && !converterApplyBtn.hasAttribute("disabled")) {
            // Still dirty
          } else if (converterStatusBadge) {
            converterStatusBadge.style.display = "none";
          }
        }, 3500);
      }
    });
  }

  async function syncDatalabApiKey() {
    if (!datalabApiKeyInput) return;
    const keyVal = datalabApiKeyInput.value.trim();
    saveSecureApiKey("datalab_api_key", keyVal);
    savedDatalabApiKey = keyVal;
    updateConverterDirtyState();
    await postLlmConfig({ datalab_api_key: keyVal });
  }

  if (datalabApiKeyInput) {
    datalabApiKeyInput.addEventListener("input", updateConverterDirtyState);
    datalabApiKeyInput.addEventListener("change", syncDatalabApiKey);
    datalabApiKeyInput.addEventListener("blur", syncDatalabApiKey);
  }

  if (datalabFallbackMarkitdown) {
    datalabFallbackMarkitdown.addEventListener("change", updateConverterDirtyState);
  }

  if (datalabFallbackDocling) {
    datalabFallbackDocling.addEventListener("change", updateConverterDirtyState);
  }

  if (datalabMergeCrossPageCheckbox) {
    datalabMergeCrossPageCheckbox.addEventListener("change", updateConverterDirtyState);
  }

  if (enableVlmCheckbox) {
    enableVlmCheckbox.addEventListener("change", () => {
      updateVlmUiVisibility();
      updateConverterDirtyState();
    });
  }

  if (vlmProviderOpenRouter) {
    vlmProviderOpenRouter.addEventListener("change", () => {
      updateVlmUiVisibility();
      updateConverterDirtyState();
    });
  }

  if (vlmProviderOllama) {
    vlmProviderOllama.addEventListener("change", () => {
      updateVlmUiVisibility();
      updateConverterDirtyState();
    });
  }

  if (vlmPrimaryModelSelect) {
    vlmPrimaryModelSelect.addEventListener("change", () => {
      updateVlmModelMetadata(vlmPrimaryModelSelect, vlmPrimaryMetadataBox);
      updateConverterDirtyState();
    });
  }

  if (vlmBackupModelSelect) {
    vlmBackupModelSelect.addEventListener("change", () => {
      updateVlmModelMetadata(vlmBackupModelSelect, vlmBackupMetadataBox);
      updateConverterDirtyState();
    });
  }

  if (vlmLocalModelInput) {
    vlmLocalModelInput.addEventListener("input", updateConverterDirtyState);
    vlmLocalModelInput.addEventListener("change", updateConverterDirtyState);
  }

  if (vlmMaxImagesInput) {
    vlmMaxImagesInput.addEventListener("input", updateConverterDirtyState);
    vlmMaxImagesInput.addEventListener("change", updateConverterDirtyState);
  }

  if (toggleDatalabKeyBtn && datalabApiKeyInput) {
    toggleDatalabKeyBtn.addEventListener("click", () => {
      if (datalabApiKeyInput.type === "password") {
        datalabApiKeyInput.type = "text";
        toggleDatalabKeyBtn.textContent = "Hide";
      } else {
        datalabApiKeyInput.type = "password";
        toggleDatalabKeyBtn.textContent = "Show";
      }
    });
  }

  if (testDatalabKeyBtn) {
    testDatalabKeyBtn.addEventListener("click", async () => {
      const backendUrl = getBackendUrl();
      const keyVal = datalabApiKeyInput ? datalabApiKeyInput.value.trim() : "";

      if (datalabKeyStatusBadge) {
        datalabKeyStatusBadge.style.display = "block";
        datalabKeyStatusBadge.textContent = "⚡ Testing Datalab API key...";
        datalabKeyStatusBadge.className = "zc-text-warning";
      }

      try {
        const res = await fetch(`${backendUrl}/converters/test-connection`, {
          method: "POST",
          headers: getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ converter_id: "datalab", datalab_api_key: keyVal }),
        });
        const data = await res.json();
        if (res.ok && (data.status === "ok" || data.status === "success")) {
          if (keyVal) {
            await syncDatalabApiKey();
          }
          if (datalabKeyStatusBadge) {
            datalabKeyStatusBadge.textContent = "✓ " + (data.message || "Connection test succeeded");
            datalabKeyStatusBadge.className = "zc-text-success";
          }
        } else {
          if (datalabKeyStatusBadge) {
            datalabKeyStatusBadge.textContent = "❌ " + (data.message || data.detail || "Connection failed");
            datalabKeyStatusBadge.className = "zc-text-error";
          }
        }
      } catch (err) {
        if (datalabKeyStatusBadge) {
          datalabKeyStatusBadge.textContent = "❌ Connection error: " + err;
          datalabKeyStatusBadge.className = "zc-text-error";
        }
      }
    });
  }

  function onApiKeyInput() {
    saveSecureApiKey("gemini_api_key", apiKeyInput.value);
    postConfigChange({ mode: modeSelect.value, cloud_api_key: apiKeyInput.value });
  }

  if (apiKeyInput) {
    apiKeyInput.addEventListener("input", onApiKeyInput);
    apiKeyInput.addEventListener("change", onApiKeyInput);
  }

  function onOpenRouterApiKeyInput() {
    if (!openRouterApiKeyInput) return;
    saveSecureApiKey("openrouter_api_key", openRouterApiKeyInput.value);
    postConfigChange({ openrouter_api_key: openRouterApiKeyInput.value });
  }

  if (openRouterApiKeyInput) {
    openRouterApiKeyInput.addEventListener("input", onOpenRouterApiKeyInput);
    openRouterApiKeyInput.addEventListener("change", onOpenRouterApiKeyInput);
  }

  const toggleGeminiKeyBtn = document.getElementById("toggle-gemini-key-btn") || document.getElementById("toggle-api-key-btn");
  if (toggleGeminiKeyBtn && apiKeyInput) {
    toggleGeminiKeyBtn.addEventListener("click", () => {
      if (apiKeyInput.type === "password") {
        apiKeyInput.type = "text";
        toggleGeminiKeyBtn.textContent = "Hide";
      } else {
        apiKeyInput.type = "password";
        toggleGeminiKeyBtn.textContent = "Show";
      }
    });
  }

  if (toggleOpenRouterKeyBtn && openRouterApiKeyInput) {
    toggleOpenRouterKeyBtn.addEventListener("click", () => {
      if (openRouterApiKeyInput.type === "password") {
        openRouterApiKeyInput.type = "text";
        toggleOpenRouterKeyBtn.textContent = "Hide";
      } else {
        openRouterApiKeyInput.type = "password";
        toggleOpenRouterKeyBtn.textContent = "Show";
      }
    });
  }

  if (testOpenRouterKeyBtn) {
    testOpenRouterKeyBtn.addEventListener("click", async () => {
      const backendUrl = getBackendUrl();
      const keyVal = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : "";

      if (!keyVal) {
        if (openRouterKeyStatusBadge) {
          openRouterKeyStatusBadge.style.display = "block";
          openRouterKeyStatusBadge.textContent = "⚠ Please enter an OpenRouter API key to test.";
          openRouterKeyStatusBadge.className = "zc-text-warning";
        }
        return;
      }

      if (openRouterKeyStatusBadge) {
        openRouterKeyStatusBadge.style.display = "block";
        openRouterKeyStatusBadge.textContent = "⚡ Testing OpenRouter API key...";
        openRouterKeyStatusBadge.className = "zc-text-warning";
      }

      try {
        const res = await fetch(`${backendUrl}/llm/test_openrouter_key`, {
          method: "POST",
          headers: getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ openrouter_api_key: keyVal }),
        });
        const data = await res.json();
        if (data.status === "success") {
          if (openRouterKeyStatusBadge) {
            openRouterKeyStatusBadge.textContent = "✓ " + data.message;
            openRouterKeyStatusBadge.className = "zc-text-success";
          }
        } else {
          if (openRouterKeyStatusBadge) {
            openRouterKeyStatusBadge.textContent = "❌ " + (data.message || data.detail || "Connection failed");
            openRouterKeyStatusBadge.className = "zc-text-error";
          }
        }
      } catch (err) {
        if (openRouterKeyStatusBadge) {
          openRouterKeyStatusBadge.textContent = "❌ Connection error: " + err;
          openRouterKeyStatusBadge.className = "zc-text-error";
        }
      }
    });
  }

  async function syncCitationsSettings() {
    const citationsVal = enableCitationsCheckbox ? enableCitationsCheckbox.checked : true;
    const referencesVal = enableReferencesCheckbox ? enableReferencesCheckbox.checked : true;
    const autoRenderMermaidVal = autoRenderMermaidCheckbox ? autoRenderMermaidCheckbox.checked : true;

    savePreference("citationsEnabled", citationsVal);
    savePreference("referencesSectionEnabled", referencesVal);
    savePreference("autoRenderMermaid", autoRenderMermaidVal);

    await postLlmConfig({
      citations_enabled: citationsVal,
      references_section_enabled: referencesVal,
      auto_render_mermaid_diagrams: autoRenderMermaidVal,
    });
  }

  if (enableCitationsCheckbox) {
    enableCitationsCheckbox.addEventListener("change", syncCitationsSettings);
  }
  if (enableReferencesCheckbox) {
    enableReferencesCheckbox.addEventListener("change", syncCitationsSettings);
  }
  if (autoRenderMermaidCheckbox) {
    autoRenderMermaidCheckbox.addEventListener("change", syncCitationsSettings);
  }

  async function syncPredictiveInputSettings() {
    const ghostVal = enablePredictiveGhostInputCheckbox ? enablePredictiveGhostInputCheckbox.checked : true;

    savePreference("enablePredictiveGhostInput", ghostVal);

    await postLlmConfig({
      enable_predictive_ghost_input: ghostVal,
    });
  }

  if (enablePredictiveGhostInputCheckbox) {
    enablePredictiveGhostInputCheckbox.addEventListener("change", syncPredictiveInputSettings);
  }

  async function syncMcpSettings() {
    const mcpVal = enableMcpServerCheckbox ? enableMcpServerCheckbox.checked : true;

    savePreference("enableMcpServer", mcpVal);

    await postLlmConfig({
      enable_mcp_server: mcpVal,
    });
  }

  if (enableMcpServerCheckbox) {
    enableMcpServerCheckbox.addEventListener("change", syncMcpSettings);
  }

  async function syncSessionMaintenanceSettings() {
    const retentionDays = sessionRetentionDaysInput ? Math.max(0, parseInt(sessionRetentionDaysInput.value, 10) || 0) : 180;
    const maxSessions = maxSavedSessionsInput ? Math.max(0, parseInt(maxSavedSessionsInput.value, 10) || 0) : 100;
    const pruneEmpty = pruneEmptySessionsCheckbox ? pruneEmptySessionsCheckbox.checked : true;
    const autoVacuum = sessionAutoVacuumCheckbox ? sessionAutoVacuumCheckbox.checked : true;

    savePreference("sessionRetentionDays", retentionDays);
    savePreference("maxSavedSessions", maxSessions);
    savePreference("pruneEmptySessions", pruneEmpty);
    savePreference("sessionDbAutoVacuumOnStartup", autoVacuum);

    await postLlmConfig({
      session_retention_days: retentionDays,
      max_saved_sessions: maxSessions,
      prune_empty_sessions: pruneEmpty,
      session_db_auto_vacuum_on_startup: autoVacuum,
    });
  }

  if (sessionRetentionDaysInput) {
    sessionRetentionDaysInput.addEventListener("change", syncSessionMaintenanceSettings);
  }
  if (maxSavedSessionsInput) {
    maxSavedSessionsInput.addEventListener("change", syncSessionMaintenanceSettings);
  }
  if (pruneEmptySessionsCheckbox) {
    pruneEmptySessionsCheckbox.addEventListener("change", syncSessionMaintenanceSettings);
  }
  if (sessionAutoVacuumCheckbox) {
    sessionAutoVacuumCheckbox.addEventListener("change", syncSessionMaintenanceSettings);
  }

  if (runSessionMaintenanceBtn) {
    runSessionMaintenanceBtn.addEventListener("click", async () => {
      runSessionMaintenanceBtn.disabled = true;
      if (sessionMaintenanceStatusBadge) {
        sessionMaintenanceStatusBadge.classList.remove("hidden");
        sessionMaintenanceStatusBadge.textContent = "Cleaning up...";
        sessionMaintenanceStatusBadge.style.color = "var(--zc-text-muted)";
      }

      const backendUrl = getBackendUrl();
      const url = `${backendUrl}/sessions/maintenance`;

      try {
        const res = await fetch(url, { method: "POST", headers: getAuthHeaders() });
        if (!res.ok) {
          throw new Error(`Server returned HTTP ${res.status}`);
        }
        const data = await res.json();
        const results = data.results || data;

        if (sessionMaintenanceStatusBadge) {
          if (results.skipped_reason === "chat_in_progress") {
            sessionMaintenanceStatusBadge.textContent = "Deferred: Chat turn currently active";
            sessionMaintenanceStatusBadge.style.color = "var(--zc-text-warning, #f59e0b)";
          } else {
            const prunedCount = (results.empty_sessions_pruned || 0) + (results.retention_sessions_pruned || 0);
            const reclaimed = results.reclaimed_bytes || 0;
            if (prunedCount > 0 || reclaimed > 0) {
              sessionMaintenanceStatusBadge.textContent = `Pruned ${prunedCount} session(s) (reclaimed ${formatBytes(reclaimed)})`;
            } else {
              sessionMaintenanceStatusBadge.textContent = "Database is healthy (0 pruned)";
            }
            sessionMaintenanceStatusBadge.style.color = "var(--zc-text-success, #10b981)";
          }
        }

        // Refresh live storage stats
        const statsRes = await fetch(`${backendUrl}/sessions/storage_stats`, {
          headers: getAuthHeaders(),
        });
        if (statsRes.ok) {
          const statsData = await statsRes.json();
          updateSessionStorageStatus(statsData);
        }
      } catch (err) {
        Zotero.debug("Zocrates: Error executing session maintenance: " + err);
        if (sessionMaintenanceStatusBadge) {
          sessionMaintenanceStatusBadge.textContent = "Clean up failed";
          sessionMaintenanceStatusBadge.style.color = "var(--zc-text-error, #ef4444)";
        }
      } finally {
        runSessionMaintenanceBtn.disabled = false;
        setTimeout(() => {
          if (sessionMaintenanceStatusBadge) {
            sessionMaintenanceStatusBadge.classList.add("hidden");
          }
        }, 5000);
      }
    });
  }

  function updateResearchProfileCounter() {
    if (!researchProfileTextarea || !researchProfileCounter) return;
    researchProfileCounter.textContent = `${researchProfileTextarea.value.length} / 1000 characters`;
  }

  let researchProfileSaveTimer = null;

  async function syncResearchProfileSettings() {
    if (researchProfileSaveTimer) {
      clearTimeout(researchProfileSaveTimer);
      researchProfileSaveTimer = null;
    }
    if (!researchProfileTextarea) return;
    const profileVal = researchProfileTextarea.value;

    await postLlmConfig({
      user_research_profile: profileVal,
    });
  }

  if (researchProfileTextarea) {
    researchProfileTextarea.addEventListener("input", () => {
      updateResearchProfileCounter();
      if (researchProfileSaveTimer) clearTimeout(researchProfileSaveTimer);
      researchProfileSaveTimer = setTimeout(syncResearchProfileSettings, 800);
    });
    researchProfileTextarea.addEventListener("change", syncResearchProfileSettings);
    researchProfileTextarea.addEventListener("blur", syncResearchProfileSettings);
    updateResearchProfileCounter();
  }

  async function syncLiteratureDiscoverySettings() {
    const emailVal = scholarPoliteEmailInput ? scholarPoliteEmailInput.value.trim() : "";
    const openAlexKeyVal = openAlexApiKeyInput ? openAlexApiKeyInput.value.trim() : "";

    savePreference("scholarPoliteEmail", emailVal);
    saveSecureApiKey("openalex_api_key", openAlexKeyVal);

    await postLlmConfig({
      scholar_polite_email: emailVal,
      openalex_api_key: openAlexKeyVal,
    });
  }

  if (scholarPoliteEmailInput) {
    scholarPoliteEmailInput.addEventListener("change", syncLiteratureDiscoverySettings);
  }
  if (openAlexApiKeyInput) {
    openAlexApiKeyInput.addEventListener("change", syncLiteratureDiscoverySettings);
  }

  if (toggleOpenAlexKeyBtn && openAlexApiKeyInput) {
    toggleOpenAlexKeyBtn.addEventListener("click", () => {
      if (openAlexApiKeyInput.type === "password") {
        openAlexApiKeyInput.type = "text";
        toggleOpenAlexKeyBtn.textContent = "Hide";
      } else {
        openAlexApiKeyInput.type = "password";
        toggleOpenAlexKeyBtn.textContent = "Show";
      }
    });
  }

  if (testOpenAlexKeyBtn) {
    testOpenAlexKeyBtn.addEventListener("click", async () => {
      const backendUrl = getBackendUrl();
      const keyVal = openAlexApiKeyInput ? openAlexApiKeyInput.value.trim() : "";

      if (!keyVal) {
        if (openAlexKeyStatusBadge) {
          openAlexKeyStatusBadge.style.display = "block";
          openAlexKeyStatusBadge.textContent = "ℹ No API key entered in field (keyless budget active)";
          openAlexKeyStatusBadge.className = "zc-text-muted";
        }
        return;
      }

      if (openAlexKeyStatusBadge) {
        openAlexKeyStatusBadge.style.display = "block";
        openAlexKeyStatusBadge.textContent = "⚡ Testing OpenAlex API key...";
        openAlexKeyStatusBadge.className = "zc-text-warning";
      }

      try {
        const res = await fetch(`${backendUrl}/discovery/test_openalex`, {
          method: "POST",
          headers: getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ openalex_api_key: keyVal }),
        });
        const data = await res.json();
        if (data.status === "success") {
          if (openAlexKeyStatusBadge) {
            openAlexKeyStatusBadge.textContent = "✓ " + data.message;
            openAlexKeyStatusBadge.className = "zc-text-success";
          }
        } else {
          if (openAlexKeyStatusBadge) {
            openAlexKeyStatusBadge.textContent = "❌ " + (data.message || data.detail || "Connection failed");
            openAlexKeyStatusBadge.className = "zc-text-error";
          }
        }
      } catch (err) {
        if (openAlexKeyStatusBadge) {
          openAlexKeyStatusBadge.textContent = "❌ Connection error: " + err;
          openAlexKeyStatusBadge.className = "zc-text-error";
        }
      }
    });
  }

  function updateMcpConfigSnippet(projectDir, backendUrlOverride) {
    if (!mcpConfigSnippet) return;
    if (projectDir) {
      savedProjectDir = projectDir;
    }
    let effectiveDir = projectDir || savedProjectDir;
    if (!effectiveDir && typeof Zotero !== "undefined" && Zotero.Prefs) {
      try {
        effectiveDir = Zotero.Prefs.get("extensions.zocrates.backendSourceDir", true) ||
                       Zotero.Prefs.get("extensions.zocrates.devSourceDir", true);
      } catch (e) {}
    }
    if (!effectiveDir) {
      effectiveDir = "/path/to/zocrates";
    }
    const backendUrl = backendUrlOverride || getBackendUrl();
    const authToken = getAuthToken();

    const config = {
      command: "uv",
      args: [
        "--directory",
        effectiveDir,
        "run",
        "python",
        "-m",
        "zocrates.mcp"
      ],
      env: {
        PYTHONUNBUFFERED: "1",
        ZOCRATES_BACKEND_URL: backendUrl,
        ZOCRATES_AUTH_TOKEN: authToken
      }
    };

    mcpConfigSnippet.textContent = `"zocrates": ${JSON.stringify(config, null, 2)}`;
  }

  if (copyMcpConfigBtn) {
    copyMcpConfigBtn.addEventListener("click", async () => {
      if (!mcpConfigSnippet) return;
      const snippetText = mcpConfigSnippet.textContent;
      try {
        const copied = await copyTextToClipboard(snippetText);
        if (!copied) {
          if (typeof console !== "undefined" && typeof console.warn === "function") {
            console.warn("Zocrates: failed to copy MCP config snippet to clipboard");
          }
          return;
        }
        if (copyFeedbackTimeout) {
          clearTimeout(copyFeedbackTimeout);
        }
        copyMcpConfigBtn.textContent = "✅ Copied!";
        copyFeedbackTimeout = setTimeout(() => {
          copyMcpConfigBtn.textContent = "📋 Copy Config";
          copyFeedbackTimeout = null;
        }, 2000);
      } catch (err) {
        if (typeof Zotero !== "undefined" && typeof Zotero.debug === "function") {
          Zotero.debug("Zocrates: Error copying MCP config: " + err);
        }
      }
    });
  }

  function updateVerificationUiVisibility() {
    const isLocal = verificationProviderSelect && verificationProviderSelect.value === "local_laya";
    if (verificationOpenrouterModelGroup) {
      verificationOpenrouterModelGroup.style.display = isLocal ? "none" : "block";
    }
    if (verificationLocalUrlGroup) {
      verificationLocalUrlGroup.style.display = isLocal ? "block" : "none";
    }
  }

  async function checkLayaStatus() {
    if (!layaStatusPill) return;

    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/llm/decision_tool/status`;

    try {
      const res = await fetch(url, {
        headers: getAuthHeaders(),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();

      if (data.server_running) {
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        _layaPollAttempts = 0;
        const activeDev = data.active_device || "online";
        const devLabel = activeDev.toLowerCase() === "cpu" ? "CPU" : activeDev;
        layaStatusPill.textContent = `● Running (${devLabel})`;
        layaStatusPill.className = "zc-badge zc-badge-online";
        if (installLayaBtn) installLayaBtn.style.display = "none";
        if (startLayaBtn) {
          startLayaBtn.style.display = "none";
          startLayaBtn.disabled = false;
        }
        if (stopLayaBtn) {
          stopLayaBtn.style.display = "inline-block";
          stopLayaBtn.disabled = false;
        }
      } else if (data.daemon_starting || data.daemon_managed) {
        layaStatusPill.textContent = "⏳ Starting server / loading model...";
        layaStatusPill.className = "zc-badge zc-badge-checking";
        if (startLayaBtn) {
          startLayaBtn.style.display = "inline-block";
          startLayaBtn.disabled = true;
        }
        if (installLayaBtn) installLayaBtn.style.display = "none";
        if (stopLayaBtn) stopLayaBtn.style.display = "none";
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        if (_layaPollAttempts < 25) {
          _layaPollAttempts++;
          _layaPollTimer = setTimeout(checkLayaStatus, 1500);
        } else {
          _layaPollAttempts = 0;
          layaStatusPill.textContent = "● Offline (startup slow/timed out)";
          layaStatusPill.className = "zc-badge zc-badge-offline";
          if (startLayaBtn) {
            startLayaBtn.style.display = "inline-block";
            startLayaBtn.disabled = false;
          }
          if (stopLayaBtn) stopLayaBtn.style.display = "none";
        }
      } else if (data.tool_installed) {
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        _layaPollAttempts = 0;
        layaStatusPill.textContent = "● Offline";
        layaStatusPill.className = "zc-badge zc-badge-offline";
        if (installLayaBtn) installLayaBtn.style.display = "none";
        if (startLayaBtn) {
          startLayaBtn.style.display = "inline-block";
          startLayaBtn.disabled = false;
        }
        if (stopLayaBtn) stopLayaBtn.style.display = "none";
      } else if (data.uv_available) {
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        _layaPollAttempts = 0;
        layaStatusPill.textContent = "○ Not Installed";
        layaStatusPill.className = "zc-badge";
        if (installLayaBtn) {
          installLayaBtn.style.display = "inline-block";
          installLayaBtn.disabled = false;
        }
        if (startLayaBtn) startLayaBtn.style.display = "none";
        if (stopLayaBtn) stopLayaBtn.style.display = "none";
      } else {
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        _layaPollAttempts = 0;
        layaStatusPill.textContent = "⚠ uv Not Found";
        layaStatusPill.className = "zc-badge zc-badge-offline";
        if (installLayaBtn) installLayaBtn.style.display = "none";
        if (startLayaBtn) startLayaBtn.style.display = "none";
        if (stopLayaBtn) stopLayaBtn.style.display = "none";
      }
    } catch (err) {
      if (_layaPollTimer) {
        clearTimeout(_layaPollTimer);
        _layaPollTimer = null;
      }
      _layaPollAttempts = 0;
      layaStatusPill.textContent = "● Offline";
      layaStatusPill.className = "zc-badge zc-badge-offline";
      if (startLayaBtn) {
        startLayaBtn.style.display = "inline-block";
        startLayaBtn.disabled = false;
      }
      if (stopLayaBtn) stopLayaBtn.style.display = "none";
    }
  }

  async function syncVerificationSettings() {
    const enabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
    const provider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
    const model = provider === "local_laya" ? "laya" : (verificationModelSelect && verificationModelSelect.value && verificationModelSelect.value !== "laya" ? verificationModelSelect.value : "inception/mercury-decide");
    const rawThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
    const threshold = Number.isFinite(rawThreshold) ? Math.min(0.95, Math.max(0.00, rawThreshold)) : 0.60;
    const autoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;

    savePreference("citationVerificationEnabled", enabled);
    savePreference("citationVerificationProvider", provider);
    savePreference("citationVerificationModel", model);
    savePreference("citationVerificationThreshold", threshold);
    savePreference("citationVerificationAutoStartLocal", autoStart);

    const configUpdates = {
      citation_verification_enabled: enabled,
      citation_verification_provider: provider,
      citation_verification_model: model,
      citation_verification_threshold: threshold,
      citation_verification_auto_start_local: autoStart,
    };

    if (verificationLocalUrlInput && verificationLocalUrlInput.value.trim()) {
      const localUrl = verificationLocalUrlInput.value.trim();
      savePreference("citationVerificationLocalUrl", localUrl);
      configUpdates.citation_verification_local_url = localUrl;
    }

    await postLlmConfig(configUpdates);
  }

  async function testDecisionModelConnection() {
    if (!verificationTestStatus) return;
    verificationTestStatus.style.display = "inline-block";
    verificationTestStatus.textContent = "⏳ Testing...";
    verificationTestStatus.className = "zc-status-text zc-text-warning";

    const backendUrl = getBackendUrl();
    const url = `${backendUrl}/llm/test_decision_model`;

    const provider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
    const model = provider === "local_laya" ? "laya" : (verificationModelSelect ? verificationModelSelect.value : "inception/mercury-decide");
    const localUrl = verificationLocalUrlInput ? verificationLocalUrlInput.value.trim() : null;
    const openrouterKey = openRouterApiKeyInput ? openRouterApiKeyInput.value.trim() : null;

    const payload = {
      provider: provider,
      model: model,
      local_url: localUrl,
      openrouter_api_key: openrouterKey,
    };

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && (data.status === "ok" || data.status === "success")) {
        verificationTestStatus.textContent = `✓ ${data.message || "Connected!"}`;
        verificationTestStatus.className = "zc-status-text zc-text-success";
        if (provider === "local_laya") {
          checkLayaStatus();
        }
      } else {
        verificationTestStatus.textContent = `✗ ${data.message || data.detail || "Verification test failed"}`;
        verificationTestStatus.className = "zc-status-text zc-text-error";
      }
    } catch (err) {
      verificationTestStatus.textContent = "✗ Network Error (Backend Server Offline)";
      verificationTestStatus.className = "zc-status-text zc-text-error";
    }
  }

  if (verificationEnabledToggle) {
    verificationEnabledToggle.addEventListener("change", updateVerificationDirtyState);
  }
  if (verificationProviderSelect) {
    verificationProviderSelect.addEventListener("change", () => {
      updateVerificationUiVisibility();
      updateVerificationDirtyState();
      updateVerificationModelMetadata();
      if (verificationProviderSelect.value === "local_laya") {
        checkLayaStatus();
      } else {
        if (_layaPollTimer) {
          clearTimeout(_layaPollTimer);
          _layaPollTimer = null;
        }
        _layaPollAttempts = 0;
      }
    });
  }
  if (verificationModelSelect) {
    verificationModelSelect.addEventListener("change", () => {
      updateVerificationDirtyState();
      updateVerificationModelMetadata();
    });
  }
  if (verificationLocalUrlInput) {
    verificationLocalUrlInput.addEventListener("change", () => {
      updateVerificationDirtyState();
      if (verificationProviderSelect && verificationProviderSelect.value === "local_laya") {
        checkLayaStatus();
      }
    });
  }
  if (autoStartLayaToggle) {
    autoStartLayaToggle.addEventListener("change", updateVerificationDirtyState);
  }
  if (verificationThresholdInput) {
    verificationThresholdInput.addEventListener("input", () => {
      let val = parseFloat(verificationThresholdInput.value);
      if (Number.isFinite(val)) {
        val = Math.min(0.95, Math.max(0.00, val));
        if (verificationThresholdValue) {
          verificationThresholdValue.textContent = val.toFixed(2);
        }
      }
      updateVerificationDirtyState();
    });
    verificationThresholdInput.addEventListener("change", () => {
      let val = parseFloat(verificationThresholdInput.value);
      if (Number.isFinite(val)) {
        val = Math.min(0.95, Math.max(0.00, val));
        verificationThresholdInput.value = val.toFixed(2);
      }
      updateVerificationDirtyState();
    });
  }
  if (refreshVerificationModelsBtn) {
    refreshVerificationModelsBtn.addEventListener("click", async () => {
      const currentModel = verificationModelSelect ? verificationModelSelect.value : null;
      await fetchOpenRouterModels("decision", true, currentModel);
      updateVerificationModelMetadata();
    });
  }
  if (verificationTestBtn) {
    verificationTestBtn.addEventListener("click", testDecisionModelConnection);
  }
  if (verificationApplyBtn) {
    verificationApplyBtn.addEventListener("click", async () => {
      if (verificationApplyBtn.hasAttribute("disabled") && verificationApplyBtn.disabled) return;
      verificationApplyBtn.disabled = true;

      await syncVerificationSettings();

      savedVerificationEnabled = verificationEnabledToggle ? verificationEnabledToggle.checked : false;
      savedVerificationProvider = verificationProviderSelect ? verificationProviderSelect.value : "openrouter";
      savedVerificationModel = verificationModelSelect ? verificationModelSelect.value : "inception/mercury-decide";
      savedVerificationThreshold = verificationThresholdInput ? parseFloat(verificationThresholdInput.value) : 0.60;
      savedVerificationAutoStart = autoStartLayaToggle ? autoStartLayaToggle.checked : true;

      updateVerificationDirtyState();

      if (verificationTestStatus) {
        verificationTestStatus.style.display = "inline-block";
        verificationTestStatus.textContent = "✓ Verification settings applied successfully!";
        verificationTestStatus.className = "zc-status-text zc-text-success";
        setTimeout(() => {
          if (verificationTestStatus.textContent.includes("applied")) {
            verificationTestStatus.style.display = "none";
          }
        }, 3500);
      }
    });
  }
  if (installLayaBtn) {
    installLayaBtn.addEventListener("click", async () => {
      installLayaBtn.disabled = true;
      if (layaStatusPill) {
        layaStatusPill.textContent = "⏳ Installing Laya (~1.6GB)...";
        layaStatusPill.className = "zc-badge zc-badge-checking";
      }
      const backendUrl = getBackendUrl();
      try {
        const res = await fetch(`${backendUrl}/llm/decision_tool/install`, { method: "POST", headers: getAuthHeaders() });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          await checkLayaStatus();
        } else {
          alert(`Laya installation failed: ${data.message || "Unknown error"}`);
          await checkLayaStatus();
        }
      } catch (err) {
        alert(`Installation request error: ${err.message}`);
        await checkLayaStatus();
      } finally {
        installLayaBtn.disabled = false;
      }
    });
  }
  if (startLayaBtn) {
    startLayaBtn.addEventListener("click", async () => {
      startLayaBtn.disabled = true;
      if (_layaPollTimer) {
        clearTimeout(_layaPollTimer);
        _layaPollTimer = null;
      }
      _layaPollAttempts = 0;
      if (layaStatusPill) {
        layaStatusPill.textContent = "⏳ Starting server...";
        layaStatusPill.className = "zc-badge zc-badge-checking";
      }
      const backendUrl = getBackendUrl();
      try {
        await fetch(`${backendUrl}/llm/decision_tool/start`, { method: "POST", headers: getAuthHeaders() });
      } catch (err) {
        Zotero.debug("Zocrates: Error starting Laya daemon: " + err);
      } finally {
        await checkLayaStatus();
      }
    });
  }
  if (stopLayaBtn) {
    stopLayaBtn.addEventListener("click", async () => {
      if (_layaPollTimer) {
        clearTimeout(_layaPollTimer);
        _layaPollTimer = null;
      }
      _layaPollAttempts = 0;
      stopLayaBtn.disabled = true;
      if (layaStatusPill) {
        layaStatusPill.textContent = "⏳ Stopping server...";
        layaStatusPill.className = "zc-badge zc-badge-checking";
      }
      const backendUrl = getBackendUrl();
      try {
        await fetch(`${backendUrl}/llm/decision_tool/stop`, { method: "POST", headers: getAuthHeaders() });
      } catch (err) {
        Zotero.debug("Zocrates: Error stopping Laya daemon: " + err);
      } finally {
        stopLayaBtn.disabled = false;
        await checkLayaStatus();
      }
    });
  }

  loadPreferences();
  fetchLlmConfig();
  fetchConverterSettings();
  fetchStatus();
  initDrawerAccordion();

  return {
    loadPreferences,
    postLlmConfig,
    saveConfigJson,
    postConverterChange,
    syncCitationsSettings,
    syncPredictiveInputSettings,
    syncMcpSettings,
    syncSessionMaintenanceSettings,
    syncLiteratureDiscoverySettings,
    syncDatalabApiKey,
    syncVerificationSettings,
    syncResearchProfileSettings,
    saveLlmConfig,
    fetchStatus,
    fetchLlmConfig,
    fetchConverterSettings,
    updateMcpConfigSnippet,
    copyTextToClipboard,
    updateLlmUiVisibility,
    updateVerificationModelMetadata,
    updateLlmModelMetadata,
    updateEmbeddingModelMetadata,
    updateVlmModelMetadata,
    fetchOpenRouterModels,
    renderModelMetadataBox,
    renderDecisionModels,
  };
}

async function copyTextToClipboard(text) {
  // Keep in sync with addon/sidebar.js::copyTextToClipboard
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
  const zot = typeof Zotero !== "undefined" ? Zotero : (typeof window !== "undefined" && window.Zotero ? window.Zotero : null);
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

function initDrawerAccordion() {
  const drawers = document.querySelectorAll(".zc-drawer");
  const expandCollapseBtn = document.getElementById("expand-collapse-all-btn");

  function updateExpandCollapseLabel() {
    if (!expandCollapseBtn) return;
    const allOpen = Array.from(drawers).every((d) => d.classList.contains("open"));
    expandCollapseBtn.textContent = allOpen ? "Collapse all" : "Expand all";
  }

  drawers.forEach((drawer) => {
    const header = drawer.querySelector(".zc-drawer-header");
    if (!header) return;
    header.addEventListener("click", () => {
      const isNowOpen = !drawer.classList.contains("open");
      drawer.classList.toggle("open", isNowOpen);
      header.setAttribute("aria-expanded", isNowOpen ? "true" : "false");
      if (isNowOpen) {
        const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        header.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion ? "auto" : "smooth" });
      }
      updateExpandCollapseLabel();
    });
  });

  if (expandCollapseBtn) {
    expandCollapseBtn.addEventListener("click", () => {
      const anyClosedDrawer = Array.from(drawers).some((d) => !d.classList.contains("open"));
      drawers.forEach((drawer) => {
        const header = drawer.querySelector(".zc-drawer-header");
        if (anyClosedDrawer) {
          drawer.classList.add("open");
          if (header) header.setAttribute("aria-expanded", "true");
        } else {
          drawer.classList.remove("open");
          if (header) header.setAttribute("aria-expanded", "false");
        }
      });
      expandCollapseBtn.textContent = anyClosedDrawer ? "Collapse all" : "Expand all";
    });
  }
}

// Execute initialization in browser / Zotero environment
if (typeof document !== "undefined" && typeof module === "undefined") {
  if (document.readyState === "complete" || document.readyState === "interactive") {
    initZocratesPreferences();
  } else if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("load", initZocratesPreferences);
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    initZocratesPreferences,
    initDrawerAccordion,
    copyTextToClipboard,
  };
}

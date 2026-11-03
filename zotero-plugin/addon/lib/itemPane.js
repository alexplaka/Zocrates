// Copyright 2026 Google LLC
// Licensed under the Apache License, Version 2.0

var ZocratesItemPane = {
  sectionId: "zocrates-converter-section",
  pluginID: "zocrates@alexplaka.dev",
  registeredID: null,
  _activePanes: new Map(), // Map<HTMLElement (body), { item, renderId }>
  _refreshTimer: null,
  _renderCounter: 0,
  _observer: null,

  _getObserver() {
    if (!this._observer) {
      this._observer = {
        observe: (subject, topic, data) => {
          if (topic === "zocrates-document-converted") {
            this.refresh();
          }
        },
      };
    }
    return this._observer;
  },

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

  register() {
    if (!Zotero.ItemPaneManager || typeof Zotero.ItemPaneManager.registerSection !== "function") {
      Zotero.debug("Zocrates: ItemPaneManager not supported in this Zotero version");
      return;
    }

    try {
      this.registeredID = Zotero.ItemPaneManager.registerSection({
        paneID: this.sectionId,
        pluginID: this.pluginID,
        header: {
          l10nID: "zocrates-section-header",
          icon: "chrome://zocrates/content/icons/icon-16.png",
        },
        sidenav: {
          l10nID: "zocrates-section-sidenav",
          icon: "chrome://zocrates/content/icons/icon-20.png",
        },
        onInit: ({ paneID }) => {
          Zotero.debug("Zocrates: ItemPane section initialized: " + paneID);
        },
        onItemChange: ({ item, setEnabled }) => {
          if (setEnabled) {
            setEnabled(!!item);
          }
        },
        onRender: this.onRender.bind(this),
        onUnmount: this.onUnmount.bind(this),
      });

      Zotero.debug("Zocrates: ItemPane section registered with ID: " + this.registeredID);

      // Register observer for document conversion events
      if (typeof Services !== "undefined" && Services.obs) {
        Services.obs.addObserver(this._getObserver(), "zocrates-document-converted", false);
      }
    } catch (e) {
      Zotero.debug("Zocrates: Error registering ItemPane section: " + e);
    }
  },

  unregister() {
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    this._activePanes.clear();

    // Unregister observer
    if (this._observer && typeof Services !== "undefined" && Services.obs) {
      try {
        Services.obs.removeObserver(this._observer, "zocrates-document-converted");
        this._observer = null;
      } catch (e) {}
    }

    let idToUnregister = this.registeredID || this.sectionId;
    if (Zotero.ItemPaneManager && typeof Zotero.ItemPaneManager.unregisterSection === "function") {
      try {
        Zotero.ItemPaneManager.unregisterSection(idToUnregister);
        Zotero.debug("Zocrates: ItemPane section unregistered: " + idToUnregister);
      } catch (e) {
        Zotero.debug("Zocrates: Error unregistering ItemPane section: " + e);
      }
    }
  },

  async getTargetAttachmentKey(item) {
    if (!item) return null;
    try {
      if (typeof item.isAttachment === "function" && item.isAttachment()) {
        return item.key;
      }
      if (typeof item.isRegularItem === "function" && item.isRegularItem()) {
        return item.key;
      }
    } catch (e) {
      Zotero.debug("Zocrates: Error resolving attachment key: " + e);
    }
    return null;
  },

  async onRender(context) {
    if (!context || !context.body || !context.body.isConnected) return;
    let { body, item } = context;

    // Track active mounted pane with a unique render ID to ignore stale async responses
    let renderId = ++this._renderCounter;
    this._activePanes.set(body, { item, renderId });

    let doc = body.ownerDocument;
    const XHTMLNS = "http://www.w3.org/1999/xhtml";

    let sectionEl = body.closest ? body.closest("collapsible-section") : body.parentElement;
    if (sectionEl) {
      sectionEl.setAttribute("label", "Zocrates: Attachment Conversion Status");
      sectionEl.label = "Zocrates: Attachment Conversion Status";
    }

    body.innerHTML = "";
    body.className = "zocrates-item-pane-container";
    body.style.cssText = "padding: 8px 12px; font-size: 12px; line-height: 1.4; color: var(--zotero-text-color, inherit);";

    let itemKey = await this.getTargetAttachmentKey(item);
    if (!body.isConnected || this._activePanes.get(body)?.renderId !== renderId) return;

    if (!itemKey) {
      let msg = doc.createElementNS(XHTMLNS, "div");
      msg.style.cssText = "color: var(--zotero-text-muted, #777); font-style: italic;";
      msg.textContent = "No convertible document attachment found for selected item.";
      body.appendChild(msg);
      return;
    }

    let loadingDiv = doc.createElementNS(XHTMLNS, "div");
    loadingDiv.className = "zocrates-loading";
    loadingDiv.style.cssText = "color: var(--zotero-text-muted, #777);";
    loadingDiv.textContent = "Loading converter status...";
    body.appendChild(loadingDiv);

    let fetchFn = (body.ownerGlobal && body.ownerGlobal.fetch) || (body.ownerDocument && body.ownerDocument.defaultView && body.ownerDocument.defaultView.fetch) || fetch;

    try {
      let serverUrl = this.getServerUrl();
      let authHeaders = this.getAuthHeaders();
      let [statusRes, convertersRes] = await Promise.all([
        fetchFn(`${serverUrl}/items/${itemKey}/status`, { headers: authHeaders }),
        fetchFn(`${serverUrl}/converters`, { headers: authHeaders })
      ]);

      if (!body.isConnected || this._activePanes.get(body)?.renderId !== renderId) return;

      if (!statusRes.ok) {
        let text = await statusRes.text().catch(() => statusRes.statusText);
        throw new Error(`Item status endpoint returned HTTP ${statusRes.status}: ${text}`);
      }
      if (!convertersRes.ok) {
        let text = await convertersRes.text().catch(() => convertersRes.statusText);
        throw new Error(`Converters endpoint returned HTTP ${convertersRes.status}: ${text}`);
      }

      let statusData = await statusRes.json();
      let convertersData = await convertersRes.json();

      if (!body.isConnected || this._activePanes.get(body)?.renderId !== renderId) return;

      this.renderWidgetContent(body, item, itemKey, statusData, convertersData.converters || []);
    } catch (err) {
      if (!body.isConnected || this._activePanes.get(body)?.renderId !== renderId) return;
      Zotero.debug("Zocrates: ItemPane render error: " + err);
      body.innerHTML = "";
      let errBox = doc.createElementNS(XHTMLNS, "div");
      errBox.style.cssText = "color: var(--zotero-text-error, #d97706); background: rgba(217, 119, 6, 0.1); padding: 8px; border-radius: 4px;";
      
      let errTitle = doc.createElementNS(XHTMLNS, "strong");
      errTitle.textContent = "Zocrates Server Disconnected";
      errBox.appendChild(errTitle);

      errBox.appendChild(doc.createElementNS(XHTMLNS, "br"));

      let errDetail = doc.createElementNS(XHTMLNS, "span");
      errDetail.style.cssText = "font-size: 11px;";
      errDetail.textContent = err.message || String(err);
      errBox.appendChild(errDetail);

      body.appendChild(errBox);
    }
  },

  renderWidgetContent(body, item, itemKey, statusData, availableConverters) {
    body.innerHTML = "";
    let doc = body.ownerDocument;
    const XHTMLNS = "http://www.w3.org/1999/xhtml";

    let attachments = statusData.attachments || [];
    if (attachments.length === 0) {
      let emptyMsg = doc.createElementNS(XHTMLNS, "div");
      emptyMsg.style.cssText = "color: var(--zotero-text-muted, #777); font-style: italic;";
      emptyMsg.textContent = "No convertible document attachment found.";
      body.appendChild(emptyMsg);
      return;
    }

    let selectedIndex = 0;

    let renderCurrent = () => {
      body.innerHTML = "";
      let att = attachments[selectedIndex];

      if (attachments.length > 1) {
        let attSelectBox = doc.createElementNS(XHTMLNS, "div");
        attSelectBox.style.cssText = "margin-bottom: 8px;";

        let attLabel = doc.createElementNS(XHTMLNS, "label");
        attLabel.textContent = `Attachment (${selectedIndex + 1} of ${attachments.length}):`;
        attLabel.style.cssText = "display: block; font-weight: 600; font-size: 11px; margin-bottom: 4px;";
        attSelectBox.appendChild(attLabel);

        let attSelect = doc.createElementNS(XHTMLNS, "select");
        attSelect.style.cssText = "width: 100%; padding: 4px; font-size: 11px; border-radius: 4px; border: 1px solid var(--zotero-border-color, #ccc); background: var(--zotero-background-color, #fff); color: var(--zotero-text-color, inherit);";

        attachments.forEach((a, idx) => {
          let opt = doc.createElementNS(XHTMLNS, "option");
          opt.value = idx;
          opt.textContent = `${a.filename || a.item_key} (${a.content_type || 'document'})`;
          if (idx === selectedIndex) opt.selected = true;
          attSelect.appendChild(opt);
        });

        attSelect.addEventListener("change", (e) => {
          selectedIndex = parseInt(e.target.value, 10);
          renderCurrent();
        });

        attSelectBox.appendChild(attSelect);
        body.appendChild(attSelectBox);
      }

      let targetKey = att.item_key;
      let meta = att.metadata || {};
      let isCached = att.is_cached;
      let isIndexed = att.is_indexed;
      let failedConverters = att.failed_converters || statusData.failed_converters || {};

      // Status Badge
      let badgeColor = isIndexed ? "#16a34a" : (isCached ? "#ca8a04" : "#6b7280");
      let badgeText = isIndexed ? "Ingested & Indexed" : (isCached ? "Cached (Pending Index)" : "Not Converted");

      let statusRow = doc.createElementNS(XHTMLNS, "div");
      statusRow.style.cssText = "margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between;";
      
      let statusLabel = doc.createElementNS(XHTMLNS, "span");
      statusLabel.style.cssText = "font-weight: 600;";
      statusLabel.textContent = "Status:";
      statusRow.appendChild(statusLabel);

      let badgeSpan = doc.createElementNS(XHTMLNS, "span");
      badgeSpan.style.cssText = `background-color: ${badgeColor}; color: #ffffff; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 500;`;
      badgeSpan.textContent = badgeText;
      statusRow.appendChild(badgeSpan);

      body.appendChild(statusRow);

      // Failure Alert Box if previous conversion failed
      if (failedConverters && Object.keys(failedConverters).length > 0) {
        let failedBox = doc.createElementNS(XHTMLNS, "div");
        failedBox.style.cssText = "background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 4px; padding: 6px 8px; font-size: 11px; color: #ef4444; margin-bottom: 8px; line-height: 1.4;";
        let failedLines = [];
        for (let [fConv, fInfo] of Object.entries(failedConverters)) {
          let rawMsg = (typeof fInfo === "object" && fInfo.error_message) ? fInfo.error_message : String(fInfo);
          if (rawMsg.length > 90) rawMsg = rawMsg.substring(0, 90) + "...";
          let activeEngine = meta.converter_id || "MarkItDown";
          failedLines.push(`⚠️ ${fConv} failed on this document: ${rawMsg}. Working bundle remains ${activeEngine}.`);
        }
        failedBox.textContent = failedLines.join("\n");
        body.appendChild(failedBox);
      }

      if (isCached && meta) {
        let metaTable = doc.createElementNS(XHTMLNS, "table");
        metaTable.style.cssText = "width: 100%; border-collapse: collapse; margin-bottom: 10px; font-size: 11px;";
        
        let convertedDate = meta.converted_at ? new Date(meta.converted_at).toLocaleString() : 'N/A';
        let indexedDate = meta.indexed_at ? new Date(meta.indexed_at).toLocaleString() : 'N/A';
        let hashTrunc = meta.source_hash ? meta.source_hash.substring(0, 12) + '...' : 'N/A';
        let charCount = meta.char_count ? meta.char_count.toLocaleString() : '0';

        let rowsData = [
          ["Engine:", `${meta.converter_id || 'unknown'} (v${meta.converter_version || '1.0'})`],
          ["Converted:", convertedDate],
          ["Indexed:", indexedDate],
          ["Char Count:", charCount],
          ["Source Hash:", hashTrunc]
        ];

        if (meta.parse_quality_score != null) {
          let scoreNum = parseFloat(meta.parse_quality_score);
          let scoreFormatted = isNaN(scoreNum) ? String(meta.parse_quality_score) : `${scoreNum.toFixed(2)} / 5.0`;
          rowsData.push(["Parse Quality:", scoreFormatted]);
        }
        if (meta.checkpoint_id) {
          let cpTrunc = meta.checkpoint_id.length > 12 ? meta.checkpoint_id.substring(0, 12) + "..." : meta.checkpoint_id;
          rowsData.push(["Checkpoint ID:", cpTrunc]);
        }

        rowsData.forEach(([label, val]) => {
          let tr = doc.createElementNS(XHTMLNS, "tr");
          
          let tdLabel = doc.createElementNS(XHTMLNS, "td");
          tdLabel.style.cssText = "color: var(--zotero-text-muted, #666); padding: 2px 0; width: 35%;";
          tdLabel.textContent = label;
          tr.appendChild(tdLabel);

          let tdVal = doc.createElementNS(XHTMLNS, "td");
          if (label === "Engine:") tdVal.style.cssText = "font-weight: 500;";
          if (label === "Parse Quality:") {
            let scoreNum = parseFloat(meta.parse_quality_score);
            let scoreColor = (!isNaN(scoreNum) && scoreNum >= 4.0) ? "#16a34a" : ((!isNaN(scoreNum) && scoreNum >= 3.0) ? "#ca8a04" : "#ef4444");
            tdVal.style.cssText = `font-weight: 600; color: ${scoreColor};`;
          }
          if (label === "Checkpoint ID:" && meta.checkpoint_id) {
            tdVal.setAttribute("title", meta.checkpoint_id);
            tdVal.style.cssText = "font-family: monospace; font-size: 10px;";
          }
          if (label === "Source Hash:") {
            tdVal.style.cssText = "font-family: monospace; font-size: 10px; display: flex; align-items: center; justify-content: space-between; gap: 4px;";
            
            let hashSpan = doc.createElementNS(XHTMLNS, "span");
            hashSpan.textContent = hashTrunc;
            if (meta.source_hash) hashSpan.setAttribute("title", meta.source_hash);
            tdVal.appendChild(hashSpan);

            if (meta.source_hash) {
              let copyBtn = doc.createElementNS(XHTMLNS, "button");
              copyBtn.textContent = "Copy";
              copyBtn.style.cssText = "padding: 1px 5px; font-size: 10px; cursor: pointer; border-radius: 3px; background: var(--zotero-button-background, rgba(99, 102, 241, 0.12)); color: var(--zotero-text-color, inherit); border: 1px solid var(--zotero-border-color, rgba(99, 102, 241, 0.3)); font-weight: 500;";
              
              copyBtn.addEventListener("click", (e) => {
                e.preventDefault();
                try {
                  if (typeof Zotero !== "undefined" && Zotero.Utilities && Zotero.Utilities.Internal && typeof Zotero.Utilities.Internal.copyTextToClipboard === "function") {
                    Zotero.Utilities.Internal.copyTextToClipboard(meta.source_hash);
                  } else if (navigator.clipboard) {
                    navigator.clipboard.writeText(meta.source_hash);
                  }
                  copyBtn.textContent = "Copied!";
                  setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
                } catch (err) {
                  alert("Full SHA-256 Hash:\n\n" + meta.source_hash);
                }
              });
              tdVal.appendChild(copyBtn);
            }
          } else {
            tdVal.textContent = val;
          }
          tr.appendChild(tdVal);

          metaTable.appendChild(tr);
        });

        body.appendChild(metaTable);

        // "View Markdown" button right under Status metadata
        let viewBtn = doc.createElementNS(XHTMLNS, "button");
        viewBtn.textContent = "View Markdown";
        viewBtn.style.cssText = "width: 100%; margin-top: 4px; margin-bottom: 8px; padding: 5px 8px; font-size: 11px; cursor: pointer; border-radius: 4px; background: var(--zotero-button-background, rgba(99, 102, 241, 0.12)); color: var(--zotero-text-color, inherit); border: 1px solid var(--zotero-border-color, rgba(99, 102, 241, 0.3)); font-weight: 500;";
        
        viewBtn.addEventListener("click", () => {
          try {
            let mdPath = (meta && meta.markdown_path) || "";
            if (!mdPath && typeof Zotero !== "undefined" && Zotero.DataDirectory && Zotero.DataDirectory.dir) {
              mdPath = Zotero.DataDirectory.dir + "/zocrates-data/corpus/" + targetKey + "/document.md";
            }
            if (mdPath && typeof Zotero !== "undefined" && typeof Zotero.launchFile === "function") {
              Zotero.launchFile(mdPath);
            } else if (mdPath) {
              alert("Markdown document path:\n" + mdPath);
            } else {
              alert("Markdown file path not found for target key: " + targetKey);
            }
          } catch (e) {
            alert("Failed to launch Markdown viewer: " + e.message);
          }
        });

        body.appendChild(viewBtn);
      }

      // Action Controls
      let controlsBox = doc.createElementNS(XHTMLNS, "div");
      controlsBox.style.cssText = "margin-top: 10px; border-top: 1px solid var(--zotero-border-color, #e5e7eb); padding-top: 10px;";

      let isPdf = (att.content_type === "application/pdf") || (att.filename && att.filename.toLowerCase().endsWith(".pdf"));
      let filteredConverters = availableConverters.filter(conv => {
        if (conv.id === "direct_read") return false;
        if (!isPdf && conv.id === "datalab") return false;
        return true;
      });

      if (filteredConverters.length > 0) {
        let selectLabel = doc.createElementNS(XHTMLNS, "label");
        selectLabel.textContent = "Re-convert with engine:";
        selectLabel.style.cssText = "display: block; font-size: 11px; font-weight: 500; margin-bottom: 4px; color: var(--zotero-text-color, inherit);";
        controlsBox.appendChild(selectLabel);

        let select = doc.createElementNS(XHTMLNS, "select");
        select.className = "zocrates-converter-select";
        select.style.cssText = "width: 100%; padding: 4px 6px; font-size: 11px; margin-bottom: 8px; border-radius: 4px; border: 1px solid var(--zotero-border-color, #ccc); background: var(--zotero-input-background, var(--zotero-background-color, transparent)); color: var(--zotero-input-color, var(--zotero-text-color, inherit));";

        let currentConverterId = meta ? meta.converter_id : null;
        let firstSelectableSet = false;

        const displayNameMap = {
          "markitdown": "MarkItDown (Local)",
          "docling_local": "Docling (Local)",
          "docling": "Docling (Local)",
          "datalab": "Datalab (Cloud)",
        };

        filteredConverters.forEach(conv => {
          let opt = doc.createElementNS(XHTMLNS, "option");
          opt.value = conv.id;
          let isCurrent = (currentConverterId === conv.id);
          let hasFailed = !!(failedConverters && (failedConverters[conv.id] || (conv.id === "docling_local" && failedConverters["docling"])));
          let failTag = hasFailed ? ' ⚠️ (Failed previously)' : '';
          let nameStr = displayNameMap[conv.id] || conv.id;
          let hasKey = conv.has_api_key !== false;

          if (!hasKey && conv.id === "datalab") {
            opt.textContent = `${nameStr} (v${conv.version}) [API Key Required]`;
            opt.disabled = true;
          } else {
            opt.textContent = `${nameStr} (v${conv.version})${conv.is_active ? ' [Active Global]' : ''}${isCurrent ? ' [Current]' : ''}${failTag}`;
            if (isCurrent) {
              opt.disabled = true;
            } else if (!firstSelectableSet) {
              opt.selected = true;
              firstSelectableSet = true;
            }
          }
          select.appendChild(opt);
        });

        // If no selectable converter alternative exists, disable select & button
        if (!firstSelectableSet) {
          select.disabled = true;
        }

        controlsBox.appendChild(select);

        let btnRow = doc.createElementNS(XHTMLNS, "div");
        btnRow.style.cssText = "display: flex; gap: 6px;";

        let convertBtn = doc.createElementNS(XHTMLNS, "button");
        convertBtn.textContent = "Re-convert Attachment";
        convertBtn.style.cssText = "flex: 1; padding: 4px 8px; font-size: 11px; cursor: pointer; border-radius: 4px; background: var(--zotero-button-background, rgba(99, 102, 241, 0.12)); color: var(--zotero-text-color, inherit); border: 1px solid var(--zotero-border-color, rgba(99, 102, 241, 0.3)); font-weight: 500;";
        if (!firstSelectableSet) {
          convertBtn.disabled = true;
        }
        
        convertBtn.addEventListener("click", async () => {
          convertBtn.disabled = true;
          let selectedEngine = select.value || "converter";
          let engineName = displayNameMap[select.value] || select.value;
          let docTitle = att.filename || (item && typeof item.getField === "function" && item.getField("title")) || targetKey;
          let jobId = "recon_" + targetKey + "_" + Date.now();
          let zoteroGlobal = (typeof Zotero !== "undefined") ? Zotero : (window.parent && window.parent.Zotero ? window.parent.Zotero : null);

          convertBtn.textContent = (selectedEngine === "docling_local" || selectedEngine === "docling")
            ? "Converting with Docling..."
            : `Converting with ${selectedEngine}...`;

          if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier) {
            zoteroGlobal.Zocrates.Notifier.add(`⚡ Re-converting: ${docTitle} with ${engineName}...`, "info");
          }

          if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
            zoteroGlobal.Zocrates.BatchProgress.update({
              id: jobId,
              isSingleItem: true,
              status: "converting",
              converterLabel: engineName,
              converterId: select.value,
              currentTitle: docTitle,
              currentIndex: 1,
              total: 1,
              processed: 0,
              skipped: 0,
              failed: 0,
              abort: null,
            });
          }

          try {
            let serverUrl = this.getServerUrl();
            let fetchFn = (body.ownerGlobal && body.ownerGlobal.fetch) || (body.ownerDocument && body.ownerDocument.defaultView && body.ownerDocument.defaultView.fetch) || fetch;
            let res = await fetchFn(`${serverUrl}/convert/${targetKey}`, {
              method: "POST",
              headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify({ converter_id: select.value, force: true })
            });
            if (!res.ok) {
              let errData = await res.json().catch(() => ({}));
              let detail = errData.detail;
              let errMsg = typeof detail === "object" ? (detail.message || JSON.stringify(detail)) : (detail || `HTTP ${res.status}`);
              throw new Error(errMsg);
            }

            let resData = await res.json().catch(() => ({}));
            let successMsg = `✅ Converted: ${docTitle} with ${engineName}`;

            try {
              if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier) {
                zoteroGlobal.Zocrates.Notifier.add(successMsg, "info");
              }
            } catch (err) {
              Zotero.debug("Zocrates: Error dispatching conversion success notification: " + err);
            }

            if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
              zoteroGlobal.Zocrates.BatchProgress.update({
                id: jobId,
                isSingleItem: true,
                status: "completed",
                converterLabel: engineName,
                converterId: select.value,
                currentTitle: docTitle,
                currentIndex: 1,
                total: 1,
                processed: 1,
                skipped: 0,
                failed: 0,
                abort: null,
              });
              setTimeout(() => {
                let cur = zoteroGlobal.Zocrates.BatchProgress.get();
                if (cur && cur.id === jobId) {
                  zoteroGlobal.Zocrates.BatchProgress.clear();
                }
              }, 4000);
            }

            try {
              if (typeof Services !== "undefined" && Services.obs) {
                Services.obs.notifyObservers(null, "zocrates-document-converted", targetKey);
              }
            } catch (obsErr) {
              Zotero.debug("Zocrates: Error notifying zocrates-document-converted observer: " + obsErr);
            }
          } catch (e) {
            let notifText = `Re-conversion failed for ${att.filename || targetKey} (${select.value}): ${e.message}`;
            try {
              if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.Notifier) {
                zoteroGlobal.Zocrates.Notifier.add(notifText, "error");
              }
            } catch (err) {
              Zotero.debug("Zocrates: Error dispatching conversion failure notification: " + err);
            }

            if (zoteroGlobal && zoteroGlobal.Zocrates && zoteroGlobal.Zocrates.BatchProgress) {
              zoteroGlobal.Zocrates.BatchProgress.update({
                id: jobId,
                isSingleItem: true,
                status: "error",
                converterLabel: engineName,
                converterId: select.value,
                currentTitle: docTitle,
                error: e.message || String(e),
                currentIndex: 1,
                total: 1,
                processed: 0,
                skipped: 0,
                failed: 1,
                abort: null,
              });
              setTimeout(() => {
                let cur = zoteroGlobal.Zocrates.BatchProgress.get();
                if (cur && cur.id === jobId) {
                  zoteroGlobal.Zocrates.BatchProgress.clear();
                }
              }, 4000);
            }

            convertBtn.disabled = false;
            convertBtn.textContent = "Re-convert Attachment";
          }
        });

        btnRow.appendChild(convertBtn);
        controlsBox.appendChild(btnRow);
        body.appendChild(controlsBox);
      }
    };

    renderCurrent();
  },

  onUnmount(context) {
    if (context && context.body) {
      this._activePanes.delete(context.body);
      context.body.innerHTML = "";
    }
  },

  refresh() {
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
    }
    this._refreshTimer = setTimeout(async () => {
      this._refreshTimer = null;
      let entries = Array.from(this._activePanes.entries());
      for (let [body, paneInfo] of entries) {
        try {
          if (!body || !body.isConnected) {
            this._activePanes.delete(body);
            continue;
          }
          let currentItem = paneInfo.item;
          if (
            currentItem &&
            currentItem.id &&
            typeof Zotero !== "undefined" &&
            Zotero.Items &&
            typeof Zotero.Items.getAsync === "function"
          ) {
            try {
              let latest = await Zotero.Items.getAsync(currentItem.id);
              if (latest) currentItem = latest;
            } catch (e) {}
          }
          await this.onRender({ body, item: currentItem });
        } catch (err) {
          Zotero.debug("Zocrates: Error refreshing ItemPane context: " + err);
        }
      }
    }, 200);
  }
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = ZocratesItemPane;
}


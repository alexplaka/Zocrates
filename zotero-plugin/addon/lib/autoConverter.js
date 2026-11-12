
if (typeof ChromeUtils !== "undefined" && typeof ChromeUtils.importGlobalProperties === "function") {
  try {
    ChromeUtils.importGlobalProperties(["fetch", "setTimeout", "clearTimeout"]);
  } catch (e) {}
} else if (typeof Components !== "undefined" && Components.utils && typeof Components.utils.importGlobalProperties === "function") {
  try {
    Components.utils.importGlobalProperties(["fetch", "setTimeout", "clearTimeout"]);
  } catch (e) {}
}

function _resolveAutoConverterFetch() {
  if (typeof globalThis !== "undefined" && typeof globalThis.fetch === "function") {
    return globalThis.fetch.bind(globalThis);
  }
  if (typeof fetch === "function") {
    return fetch;
  }
  let targetWin = typeof Zotero !== "undefined" && Zotero.getMainWindow ? Zotero.getMainWindow() : null;
  return (
    (targetWin && typeof targetWin.fetch === "function" && targetWin.fetch.bind(targetWin)) ||
    null
  );
}

function getConverterDisplayName(converterId) {
  switch (converterId) {
    case "datalab":
      return "Datalab (Cloud)";
    case "docling_local":
    case "docling":
      return "Docling (Local)";
    case "markitdown":
      return "MarkItDown (Local)";
    default:
      return converterId;
  }
}

function resolveItemTitle(attItem) {
  if (!attItem) return "Document";
  try {
    if (attItem.parentItem) {
      let parentTitle = attItem.parentItem.getField("title");
      if (parentTitle) return parentTitle;
    }
    let title = attItem.getField("title");
    if (title && title !== "PDF" && title !== "Snapshot") return title;
    let filename = attItem.getFilename ? attItem.getFilename() : null;
    if (filename) return filename;
  } catch (e) {}
  return attItem.key || "Document";
}

function isConvertibleAttachment(attItem) {
  if (!attItem || !attItem.isAttachment()) return false;
  let contentType = (attItem.attachmentContentType || "").toLowerCase();
  let filename = (attItem.getFilename ? attItem.getFilename() : "") || "";
  let ext = filename ? filename.split(".").pop().toLowerCase() : "";

  // If content type and filename are not yet resolved, allow the queue to verify via waitForAttachmentFile
  if (!contentType && !filename) {
    return true;
  }

  const convertibleTypes = [
    "application/pdf",
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ];
  const convertibleExts = [
    "pdf",
    "html",
    "htm",
    "xhtml",
    "txt",
    "md",
    "markdown",
    "docx",
    "doc",
    "pptx",
  ];

  if (convertibleTypes.includes(contentType) || convertibleExts.includes(ext)) {
    return true;
  }

  if (typeof attItem.isSnapshot === "function" && attItem.isSnapshot()) {
    return true;
  }

  return false;
}

async function waitForAttachmentFile(attItem, maxRetries = 10, delayMs = 1000) {
  for (let i = 0; i <= maxRetries; i++) {
    try {
      // 1. Try getFilePathAsync()
      if (typeof attItem.getFilePathAsync === "function") {
        let p = await attItem.getFilePathAsync();
        if (p) {
          if (typeof IOUtils !== "undefined" && IOUtils.exists) {
            let exists = await IOUtils.exists(p).catch(() => false);
            if (exists) {
              let stat = await IOUtils.stat(p).catch(() => null);
              if (stat && stat.size > 0) {
                let leafName = p.split(/[/\\]/).pop() || "";
                return { path: p, leafName: leafName, exists: true, fileSize: stat.size };
              }
            }
          } else if (typeof OS !== "undefined" && OS.File) {
            let exists = await OS.File.exists(p).catch(() => false);
            if (exists) {
              let stat = await OS.File.stat(p).catch(() => null);
              if (stat && stat.size > 0) {
                let leafName = p.split(/[/\\]/).pop() || "";
                return { path: p, leafName: leafName, exists: true, fileSize: stat.size };
              }
            }
          }
        }
      }

      // 2. Try getFilePath()
      if (typeof attItem.getFilePath === "function") {
        let p = attItem.getFilePath();
        if (p) {
          if (typeof IOUtils !== "undefined" && IOUtils.exists) {
            let exists = await IOUtils.exists(p).catch(() => false);
            if (exists) {
              let stat = await IOUtils.stat(p).catch(() => null);
              if (stat && stat.size > 0) {
                let leafName = p.split(/[/\\]/).pop() || "";
                return { path: p, leafName: leafName, exists: true, fileSize: stat.size };
              }
            }
          } else if (typeof OS !== "undefined" && OS.File) {
            let exists = await OS.File.exists(p).catch(() => false);
            if (exists) {
              let stat = await OS.File.stat(p).catch(() => null);
              if (stat && stat.size > 0) {
                let leafName = p.split(/[/\\]/).pop() || "";
                return { path: p, leafName: leafName, exists: true, fileSize: stat.size };
              }
            }
          }
        }
      }

      // 3. Try getFile() (XPCOM nsIFile)
      if (typeof attItem.getFile === "function") {
        let file = attItem.getFile();
        if (file && file.exists() && file.fileSize > 0) {
          return file;
        }
      }
    } catch (e) {}

    if (i < maxRetries) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  return null;
}

var AutoConverter = {
  _observerID: null,
  _queue: [],
  _inQueue: new Set(),
  _activeConversions: new Set(),
  _isProcessing: false,
  _convertedCache: new Set(),
  _idToKeyCache: new Map(),
  _suppressAll: false,
  _suppressKeys: new Set(),
  _suppressParentIDs: new Set(),

  suppressBatch(enabled) {
    this._suppressAll = !!enabled;
    if (!this._suppressAll) {
      this._suppressKeys.clear();
      this._suppressParentIDs.clear();
    }
  },

  suppressKey(key) {
    if (key) this._suppressKeys.add(String(key));
  },

  unsuppressKey(key) {
    if (key) this._suppressKeys.delete(String(key));
  },

  suppressParentID(id) {
    if (id !== undefined && id !== null) this._suppressParentIDs.add(id);
  },

  unsuppressParentID(id) {
    if (id !== undefined && id !== null) this._suppressParentIDs.delete(id);
  },

  clearSuppression() {
    this._suppressAll = false;
    this._suppressKeys.clear();
    this._suppressParentIDs.clear();
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

  init() {
    if (this._observerID) return;
    this._observerID = Zotero.Notifier.registerObserver(this, ["item"], "ZocratesAutoConverter", 10);
    Zotero.debug("Zocrates: AutoConverter initialized and observer registered.");
  },

  destroy() {
    if (this._observerID) {
      Zotero.Notifier.unregisterObserver(this._observerID);
      this._observerID = null;
    }
    this._queue = [];
    this._inQueue.clear();
    this._activeConversions.clear();
    this._convertedCache.clear();
    this._idToKeyCache.clear();
    this._suppressAll = false;
    this._suppressKeys.clear();
    this._suppressParentIDs.clear();
    this._isProcessing = false;
    Zotero.debug("Zocrates: AutoConverter destroyed.");
  },

  _scheduleItemAttachmentChecks(itemId) {
    [1500, 3500].forEach((delay) => {
      setTimeout(() => {
        try {
          if (this._suppressAll || this._suppressParentIDs.has(itemId)) return;
          let item = Zotero.Items.get(itemId);
          if (!item || item.deleted || !item.isRegularItem()) return;
          let attIDs = item.getAttachments();
          for (let attID of attIDs) {
            let att = Zotero.Items.get(attID);
            if (att && !att.deleted && att.isAttachment()) {
              if (this._suppressKeys.has(att.key) || (att.parentItemID && this._suppressParentIDs.has(att.parentItemID))) {
                continue;
              }
              this.enqueueItem(att);
            }
          }
        } catch (e) {
          Zotero.debug("Zocrates: AutoConverter delayed check error for id " + itemId + ": " + e);
        }
      }, delay);
    });

    // 30-second debounced check for attachment-free regular items (allowing Zotero sync or PDF find to finish)
    setTimeout(() => {
      try {
        if (this._suppressAll || this._suppressParentIDs.has(itemId)) return;
        // Skip check if Zotero is currently syncing
        if (typeof Zotero !== "undefined" && Zotero.Sync && Zotero.Sync.Runner && Zotero.Sync.Runner.syncInProgress) {
          return;
        }
        let item = Zotero.Items.get(itemId);
        if (!item || item.deleted || !item.isRegularItem()) return;
        let attIDs = item.getAttachments ? item.getAttachments() : [];
        if (!attIDs || attIDs.length === 0) {
          this.enqueueMetadataItem(item.key);
        }
      } catch (e) {
        Zotero.debug("Zocrates: AutoConverter 30s metadata check error for id " + itemId + ": " + e);
      }
    }, 30000);
  },

  notify(event, type, ids, extraData) {
    if (!ids || !ids.length) return;

    if (event === "add") {
      if (this._suppressAll) return;

      for (let id of ids) {
        try {
          let item = Zotero.Items.get(id);
          if (item && item.key) {
            this._idToKeyCache.set(id, item.key);
            if (typeof item.isRegularItem === "function" && item.isRegularItem()) {
              if (typeof item.getAttachments === "function") {
                let attIDs = item.getAttachments() || [];
                for (let attID of attIDs) {
                  let att = Zotero.Items.get(attID);
                  if (att && att.key) {
                    this._idToKeyCache.set(attID, att.key);
                  }
                }
              }
              if (typeof item.getNotes === "function") {
                let noteIDs = item.getNotes() || [];
                for (let noteID of noteIDs) {
                  let note = Zotero.Items.get(noteID);
                  if (note && note.key) {
                    this._idToKeyCache.set(noteID, note.key);
                  }
                }
              }
            }
          }
        } catch (e) {}
      }

      for (let id of ids) {
        try {
          if (this._suppressParentIDs.has(id)) continue;

          let item = Zotero.Items.get(id);
          if (!item || item.deleted) continue;

          if (item.isAttachment()) {
            if (this._suppressKeys.has(item.key) || (item.parentItemID && this._suppressParentIDs.has(item.parentItemID))) {
              continue;
            }
            this.enqueueItem(item);
          } else if (item.isRegularItem()) {
            let attIDs = item.getAttachments();
            for (let attID of attIDs) {
              let att = Zotero.Items.get(attID);
              if (att && !att.deleted && att.isAttachment()) {
                if (this._suppressKeys.has(att.key) || (att.parentItemID && this._suppressParentIDs.has(att.parentItemID))) {
                  continue;
                }
                this.enqueueItem(att);
              }
            }
            this._scheduleItemAttachmentChecks(id);
          }
        } catch (e) {
          Zotero.debug("Zocrates: AutoConverter notify error for id " + id + ": " + e);
        }
      }
      return;
    }

    if (event === "trash" || event === "delete") {
      let keys = new Set();
      for (let id of ids) {
        try {
          let item = Zotero.Items.get(id);

          // Restore-from-trash guard: Zotero fires "trash" for both trash and untrash.
          // When an item is restored / untrashed from trash, item.deleted is false.
          // Do NOT purge it; re-enqueue if it has convertible attachments.
          if (event === "trash" && item && !item.deleted) {
            if (item.isAttachment()) {
              this.enqueueItem(item);
            } else if (typeof item.isRegularItem === "function" && item.isRegularItem()) {
              if (typeof item.getAttachments === "function") {
                let attIDs = item.getAttachments() || [];
                for (let attID of attIDs) {
                  let att = Zotero.Items.get(attID);
                  if (att && !att.deleted && att.isAttachment()) {
                    this.enqueueItem(att);
                  }
                }
              }
              this._scheduleItemAttachmentChecks(id);
            }
            if (item.key) {
              this._idToKeyCache.set(id, item.key);
            }
            continue;
          }

          if (item) {
            if (item.key) keys.add(item.key);
            if (typeof item.isRegularItem === "function" && item.isRegularItem()) {
              if (typeof item.getAttachments === "function") {
                let attIDs = item.getAttachments() || [];
                for (let attID of attIDs) {
                  let att = Zotero.Items.get(attID);
                  if (att && att.key) {
                    keys.add(att.key);
                    this._idToKeyCache.set(attID, att.key);
                  } else if (this._idToKeyCache.has(attID)) {
                    keys.add(this._idToKeyCache.get(attID));
                  }
                }
              }
              if (typeof item.getNotes === "function") {
                let noteIDs = item.getNotes() || [];
                for (let noteID of noteIDs) {
                  let note = Zotero.Items.get(noteID);
                  if (note && note.key) {
                    keys.add(note.key);
                    this._idToKeyCache.set(noteID, note.key);
                  } else if (this._idToKeyCache.has(noteID)) {
                    keys.add(this._idToKeyCache.get(noteID));
                  }
                }
              }
            }
            if (item.key) {
              this._idToKeyCache.set(id, item.key);
            }
          } else if (this._idToKeyCache.has(id)) {
            keys.add(this._idToKeyCache.get(id));
            this._idToKeyCache.delete(id);
          }
        } catch (e) {
          Zotero.debug("Zocrates: AutoConverter trash/delete error for id " + id + ": " + e);
        }
      }

      if (keys.size > 0) {
        for (let k of keys) {
          this._convertedCache.delete(k);
        }
        let fetchFn = _resolveAutoConverterFetch();
        if (fetchFn) {
          let serverUrl = this.getServerUrl();
          fetchFn(`${serverUrl}/items/delete`, {
            method: "POST",
            headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ item_keys: Array.from(keys) }),
          }).catch((err) => {
            Zotero.debug("Zocrates: AutoConverter failed to purge deleted items: " + err);
          });
        }
      }
      return;
    }
  },

  enqueueItem(attItem) {
    if (!attItem || !isConvertibleAttachment(attItem)) return;
    if (this._suppressAll || this._suppressKeys.has(attItem.key) || (attItem.parentItemID && this._suppressParentIDs.has(attItem.parentItemID))) return;
    let key = attItem.key;
    if (this._inQueue.has(key) || this._activeConversions.has(key) || this._convertedCache.has(key)) {
      return;
    }

    this._inQueue.add(key);
    this._queue.push(key);
    Zotero.debug("Zocrates: AutoConverter enqueued item " + key);

    this.processQueue();
  },

  enqueueMetadataItem(key) {
    if (!key || this._suppressAll || this._suppressKeys.has(key)) return;
    if (this._inQueue.has(key) || this._activeConversions.has(key) || this._convertedCache.has(key)) {
      return;
    }
    this._inQueue.add(key);
    this._queue.push(key);
    Zotero.debug("Zocrates: AutoConverter enqueued metadata item " + key);
    this.processQueue();
  },

  async processQueue() {
    if (this._isProcessing) return;
    this._isProcessing = true;

    while (this._queue.length > 0) {
      let key = this._queue.shift();
      this._inQueue.delete(key);
      if (this._activeConversions.has(key) || this._convertedCache.has(key)) {
        continue;
      }
      this._activeConversions.add(key);
      this._convertedCache.add(key); // Mark converted immediately to prevent duplicate enqueues during slow network / conversion

      try {
        await this.convertSingleItem(key);
      } catch (err) {
        Zotero.debug("Zocrates: AutoConverter error processing " + key + ": " + err);
      } finally {
        this._activeConversions.delete(key);
      }
    }

    this._isProcessing = false;
  },

  async convertSingleItem(key) {
    let attItem = null;
    try {
      let userLibID = Zotero.Libraries.userLibraryID;
      attItem = Zotero.Items.getByLibraryAndKey(userLibID, key);
    } catch (e) {}

    if (!attItem) {
      let allLibs = Zotero.Libraries.getAll();
      for (let lib of allLibs) {
        try {
          let candidate = Zotero.Items.getByLibraryAndKey(lib.id, key);
          if (candidate) {
            attItem = candidate;
            break;
          }
        } catch (e) {}
      }
    }

    if (!attItem || attItem.deleted) return;

    if (typeof attItem.isRegularItem === "function" && attItem.isRegularItem()) {
      let atts = attItem.getAttachments ? attItem.getAttachments() : [];
      let hasConvertibleFile = false;
      for (let attID of atts) {
        let att = Zotero.Items.get(attID);
        if (att && !att.deleted && isConvertibleAttachment(att) && att.attachmentLinkMode !== 3) {
          hasConvertibleFile = true;
          break;
        }
      }
      if (!hasConvertibleFile) {
        let fetchFn = _resolveAutoConverterFetch();
        if (!fetchFn) return;
        if (Zotero.DB && typeof Zotero.DB.queryAsync === "function") {
          try {
            await Zotero.DB.queryAsync("PRAGMA wal_checkpoint(TRUNCATE)");
          } catch (e) {}
        }
        let serverUrl = this.getServerUrl();
        await fetchFn(`${serverUrl}/items/ingest`, {
          method: "POST",
          headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ item_keys: [key] }),
        }).catch((err) => {
          this._convertedCache.delete(key);
          Zotero.debug("Zocrates: AutoConverter failed to ingest metadata item " + key + ": " + err);
        });
        Zotero.debug("Zocrates: AutoConverter ingested metadata-only item " + key);
        setTimeout(() => this._convertedCache.delete(key), 60000);
      }
      return;
    }

    // Verify attachment file exists on disk & non-zero size with async retry backoff
    let file = await waitForAttachmentFile(attItem, 4, 500);
    if (!file) {
      Zotero.debug(`Zocrates: AutoConverter attachment ${key} file not ready on disk; skipping.`);
      return;
    }

    let fetchFn = _resolveAutoConverterFetch();
    if (!fetchFn) {
      Zotero.debug("Zocrates: AutoConverter fetchFn not available.");
      return;
    }

    let serverUrl = this.getServerUrl();
    let statusRes = await fetchFn(`${serverUrl}/status`, {
      headers: this.getAuthHeaders(),
    }).catch(() => null);
    if (!statusRes || !statusRes.ok) {
      Zotero.debug("Zocrates: AutoConverter backend offline or not ready; skipping auto-conversion for " + key);
      return;
    }

    let statusData = await statusRes.json().catch(() => ({}));
    if (statusData.status !== "ready") {
      return;
    }

    let activeConverterId = statusData.document_converter_id || "markitdown";
    let fallbackId = statusData.datalab_non_pdf_fallback || "markitdown";

    let contentType = (attItem.attachmentContentType || "").toLowerCase();
    let leafName = (file.leafName || (file.path ? file.path.split(/[/\\]/).pop() : "") || "").toLowerCase();
    let isPdf = contentType === "application/pdf" || leafName.endsWith(".pdf");
    let isHtml =
      contentType === "text/html" ||
      leafName.endsWith(".html") ||
      leafName.endsWith(".htm") ||
      (typeof attItem.isSnapshot === "function" && attItem.isSnapshot());

    let targetConverterId = activeConverterId;
    let usedFallback = false;

    if (activeConverterId === "datalab" && !isPdf) {
      targetConverterId = fallbackId;
      usedFallback = true;
    }

    let title = resolveItemTitle(attItem);
    let engineName = getConverterDisplayName(targetConverterId);
    let itemTypeDesc = isHtml ? "snapshot" : "document";
    let jobId = "auto_" + key + "_" + Date.now();

    let startNotifId = null;
    if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
      startNotifId = Zotero.Zocrates.Notifier.add(`⚡ Converting new ${itemTypeDesc}: ${title} with ${engineName}...`, "info");
    }

    if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
      Zotero.Zocrates.BatchProgress.update({
        id: jobId,
        isSingleItem: true,
        status: "converting",
        converterLabel: engineName,
        converterId: targetConverterId,
        currentTitle: title,
        currentIndex: 1,
        total: 1,
        processed: 0,
        skipped: 0,
        failed: 0,
        abort: null,
      });
    }

    try {
      if (typeof Zotero !== "undefined" && Zotero.DB && typeof Zotero.DB.queryAsync === "function") {
        try {
          await Zotero.DB.queryAsync("PRAGMA wal_checkpoint(TRUNCATE)");
        } catch (cpErr) {
          Zotero.debug("Zocrates: AutoConverter wal_checkpoint failed: " + cpErr);
        }
      }

      let res = await fetchFn(`${serverUrl}/convert/${key}`, {
        method: "POST",
        headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ converter_id: targetConverterId, force: false }),
      });

      let data = await res.json().catch(() => ({}));

      if (res.ok && data.status === "skipped") {
        this._convertedCache.add(key);
        setTimeout(() => {
          this._convertedCache.delete(key);
        }, 60000);
        Zotero.debug("Zocrates: AutoConverter item " + key + " already converted; skipped.");
        if (startNotifId && Zotero.Zocrates && Zotero.Zocrates.Notifier && typeof Zotero.Zocrates.Notifier.delete === "function") {
          Zotero.Zocrates.Notifier.delete(startNotifId);
        }
        if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
          let cur = Zotero.Zocrates.BatchProgress.get();
          if (cur && cur.id === jobId) {
            Zotero.Zocrates.BatchProgress.clear();
          }
        }
        return;
      }

      if (res.ok && data.status === "success") {
        this._convertedCache.add(key);
        setTimeout(() => {
          this._convertedCache.delete(key);
        }, 60000);

        let notifMsg = "";
        if (usedFallback) {
          notifMsg = `✅ Converted ${itemTypeDesc}: ${title} with ${engineName} [Datalab is PDF-only]`;
        } else {
          notifMsg = `✅ Converted new item: ${title} with ${engineName}`;
        }

        if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
          Zotero.Zocrates.Notifier.add(notifMsg, "success");
        }

        if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
          Zotero.Zocrates.BatchProgress.update({
            id: jobId,
            isSingleItem: true,
            status: "completed",
            converterLabel: engineName,
            converterId: targetConverterId,
            currentTitle: title,
            currentIndex: 1,
            total: 1,
            processed: 1,
            skipped: 0,
            failed: 0,
            abort: null,
          });
          setTimeout(() => {
            let cur = Zotero.Zocrates.BatchProgress.get();
            if (cur && cur.id === jobId) {
              Zotero.Zocrates.BatchProgress.clear();
            }
          }, 4000);
        }

        if (typeof Services !== "undefined" && Services.obs) {
          Services.obs.notifyObservers(null, "zocrates-document-converted", key);
        }

        if (
          Zotero.Zocrates &&
          Zotero.Zocrates.ZocratesItemPane &&
          typeof Zotero.Zocrates.ZocratesItemPane.refresh === "function"
        ) {
          try {
            Zotero.Zocrates.ZocratesItemPane.refresh();
          } catch (e) {
            Zotero.debug("Zocrates: Error refreshing ItemPane in AutoConverter: " + e);
          }
        }
      } else {
        let errMsg =
          (data.detail && (data.detail.message || data.detail.error)) ||
          data.detail ||
          data.message ||
          "Conversion failed";
        if (typeof errMsg === "object") errMsg = JSON.stringify(errMsg);
        let notifMsg = `⚠️ Auto-conversion failed for ${title}: ${errMsg}`;

        if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
          Zotero.Zocrates.Notifier.add(notifMsg, "error");
        }

        if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
          Zotero.Zocrates.BatchProgress.update({
            id: jobId,
            isSingleItem: true,
            status: "error",
            converterLabel: engineName,
            converterId: targetConverterId,
            currentTitle: title,
            error: errMsg,
            currentIndex: 1,
            total: 1,
            processed: 0,
            skipped: 0,
            failed: 1,
            abort: null,
          });
          setTimeout(() => {
            let cur = Zotero.Zocrates.BatchProgress.get();
            if (cur && cur.id === jobId) {
              Zotero.Zocrates.BatchProgress.clear();
            }
          }, 4000);
        }
      }
    } catch (err) {
      let errMsg = err.message || String(err);
      let notifMsg = `⚠️ Auto-conversion failed for ${title}: ${errMsg}`;
      if (Zotero.Zocrates && Zotero.Zocrates.Notifier) {
        Zotero.Zocrates.Notifier.add(notifMsg, "error");
      }
      if (Zotero.Zocrates && Zotero.Zocrates.BatchProgress) {
        Zotero.Zocrates.BatchProgress.update({
          id: jobId,
          isSingleItem: true,
          status: "error",
          converterLabel: engineName,
          converterId: targetConverterId,
          currentTitle: title,
          error: errMsg,
          currentIndex: 1,
          total: 1,
          processed: 0,
          skipped: 0,
          failed: 1,
          abort: null,
        });
        setTimeout(() => {
          let cur = Zotero.Zocrates.BatchProgress.get();
          if (cur && cur.id === jobId) {
            Zotero.Zocrates.BatchProgress.clear();
          }
        }, 4000);
      }
    }
  },
};

AutoConverter.isConvertibleAttachment = isConvertibleAttachment;
AutoConverter.resolveItemTitle = resolveItemTitle;
AutoConverter.getConverterDisplayName = getConverterDisplayName;

if (typeof Zotero !== "undefined") {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  Zotero.Zocrates.AutoConverter = AutoConverter;
}
if (typeof window !== "undefined") {
  window.AutoConverter = AutoConverter;
}
if (typeof globalThis !== "undefined") {
  globalThis.AutoConverter = AutoConverter;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = AutoConverter;
}

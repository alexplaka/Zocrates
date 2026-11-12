/**
 * scholarImport.js — In-Process Zotero Write-Back & Batch Conversion Hand-off
 * Part of Zocrates extension for Zotero.
 */

if (typeof ChromeUtils !== "undefined" && typeof ChromeUtils.importGlobalProperties === "function") {
  try {
    ChromeUtils.importGlobalProperties(["fetch"]);
  } catch (e) {}
} else if (typeof Components !== "undefined" && Components.utils && typeof Components.utils.importGlobalProperties === "function") {
  try {
    Components.utils.importGlobalProperties(["fetch"]);
  } catch (e) {}
}

function _resolveScholarFetch(win) {
  let targetWin = win || (typeof Zotero !== "undefined" && Zotero.getMainWindow ? Zotero.getMainWindow() : null);
  if (targetWin && typeof targetWin.fetch === "function") {
    return targetWin.fetch.bind(targetWin);
  }
  if (typeof globalThis !== "undefined" && typeof globalThis.fetch === "function") {
    return globalThis.fetch.bind(globalThis);
  }
  if (typeof fetch === "function") {
    return fetch;
  }
  return null;
}

function _resolveZotero(win) {
  if (typeof Zotero !== "undefined" && Zotero) return Zotero;
  if (typeof globalThis !== "undefined" && globalThis.Zotero) return globalThis.Zotero;
  let targetWin = win || (typeof window !== "undefined" ? window : null);
  if (targetWin) {
    if (targetWin.Zotero) return targetWin.Zotero;
    try {
      if (targetWin.parent && targetWin.parent.Zotero) return targetWin.parent.Zotero;
      if (targetWin.top && targetWin.top.Zotero) return targetWin.top.Zotero;
      if (targetWin.opener && targetWin.opener.Zotero) return targetWin.opener.Zotero;
      if (targetWin.opener?.parent?.Zotero) return targetWin.opener.parent.Zotero;
      if (targetWin.opener?.top?.Zotero) return targetWin.opener.top.Zotero;
    } catch (e) {}
  }
  try {
    const s = (typeof Services !== "undefined" && Services) || (targetWin && (targetWin.Services || targetWin.parent?.Services || targetWin.top?.Services));
    if (s && s.wm) {
      let mainWin = s.wm.getMostRecentWindow("navigator:browser") || s.wm.getMostRecentWindow("zotero:main") || s.wm.getMostRecentWindow(null);
      if (mainWin && mainWin.Zotero) return mainWin.Zotero;
    }
  } catch (e) {}
  return null;
}

var ScholarImport = {
  getServerUrl(win) {
    // Single source of truth installed by bootstrap.js, with defensive fallback.
    try {
      const zot = _resolveZotero(win);
      const url = zot?.Zocrates?.getBackendUrl?.();
      if (url) return url;
    } catch (e) {}
    return "http://127.0.0.1:8765";
  },

  getAuthHeaders(win, extraHeaders = {}) {
    try {
      const zot = _resolveZotero(win);
      if (zot?.Zocrates?.getAuthHeaders) {
        return zot.Zocrates.getAuthHeaders(extraHeaders);
      }
      const token = zot?.Zocrates?.getAuthToken?.() || zot?.Zocrates?._authToken;
      if (token) {
        return { ...extraHeaders, Authorization: "Bearer " + token };
      }
    } catch (e) {}
    return { ...extraHeaders };
  },

  formatCreators(authors) {
    if (!authors || !Array.isArray(authors)) return [];
    let creators = [];
    for (let raw of authors) {
      if (!raw) continue;
      if (typeof raw === "object") {
        if (raw.lastName || raw.firstName) {
          creators.push({
            firstName: raw.firstName || "",
            lastName: raw.lastName || "",
            creatorType: raw.creatorType || "author",
          });
        } else if (raw.name) {
          creators.push({
            name: String(raw.name),
            creatorType: raw.creatorType || "author",
            fieldMode: raw.fieldMode !== undefined ? raw.fieldMode : 1,
          });
        }
        continue;
      }
      let str = String(raw).trim();
      if (!str) continue;
      if (str.includes(",")) {
        let parts = str.split(",");
        let lastName = parts[0].trim();
        let firstName = parts.slice(1).join(",").trim();
        creators.push({ firstName, lastName, creatorType: "author" });
      } else {
        let parts = str.split(/\s+/);
        if (parts.length === 1) {
          creators.push({ name: str, creatorType: "author", fieldMode: 1 });
        } else {
          let lastName = parts.pop();
          let firstName = parts.join(" ");
          creators.push({ firstName, lastName, creatorType: "author" });
        }
      }
    }
    return creators;
  },

  setItemFieldSafe(item, fieldName, value, zotContext) {
    if (!item || !fieldName || value === undefined || value === null || value === "") return false;
    let strVal = String(value).trim();
    if (!strVal) return false;
    let zot = zotContext || _resolveZotero();
    try {
      if (zot && zot.ItemFields) {
        let fieldID = null;
        if (typeof zot.ItemFields.getID === "function") {
          fieldID = zot.ItemFields.getID(fieldName);
        }
        if (fieldID && typeof zot.ItemFields.isValidForType === "function" && item.itemTypeID) {
          if (!zot.ItemFields.isValidForType(fieldID, item.itemTypeID)) {
            return false;
          }
        }
      }
      item.setField(fieldName, strVal);
      return true;
    } catch (e) {
      try {
        item.setField(fieldName, strVal);
        return true;
      } catch (err) {
        if (zot && zot.debug) {
          zot.debug(`Zocrates: ScholarImport setItemFieldSafe failed for ${fieldName}: ${err}`);
        }
        return false;
      }
    }
  },

  async importPaper({ candidate, stagedPath, targetCollectionId, libraryID, window } = {}) {
    if (!candidate) {
      throw new Error("ScholarImport.importPaper: candidate is required");
    }

    let zot = _resolveZotero(window);
    if (!zot) {
      throw new Error("ScholarImport.importPaper: Zotero runtime is not accessible");
    }

    // Determine item type
    let isPreprint = Boolean(
      candidate.arxiv_id ||
      (candidate.venue && /arxiv|biorxiv|medrxiv|chemrxiv|ssrn|preprint/i.test(candidate.venue)) ||
      (candidate.publicationTitle && /arxiv|biorxiv|medrxiv|chemrxiv|ssrn|preprint/i.test(candidate.publicationTitle))
    );
    let itemType = "journalArticle";
    if (isPreprint) {
      let canPreprint = false;
      try {
        if (zot.ItemTypes) {
          if (typeof zot.ItemTypes.isValidForType === "function") {
            canPreprint = zot.ItemTypes.isValidForType("preprint");
          } else if (typeof zot.ItemTypes.getID === "function") {
            canPreprint = Boolean(zot.ItemTypes.getID("preprint"));
          }
        }
      } catch (e) {}
      if (canPreprint) {
        itemType = "preprint";
      }
    }

    let item = new zot.Item(itemType);
    if (libraryID !== undefined && libraryID !== null) {
      item.libraryID = libraryID;
    }

    // Safe field population
    this.setItemFieldSafe(item, "title", candidate.title, zot);
    this.setItemFieldSafe(
      item,
      "abstractNote",
      candidate.abstractNote || candidate.abstract || candidate.tldr,
      zot
    );

    let pub = candidate.publicationTitle || candidate.venue;
    if (pub) {
      let setPub = this.setItemFieldSafe(item, "publicationTitle", pub, zot);
      if (!setPub) {
        this.setItemFieldSafe(item, "repository", pub, zot);
      }
    }

    this.setItemFieldSafe(item, "date", candidate.date || candidate.year, zot);
    this.setItemFieldSafe(item, "DOI", candidate.DOI || candidate.doi, zot);
    if (candidate.arxiv_id) {
      this.setItemFieldSafe(item, "archiveID", "arXiv:" + candidate.arxiv_id, zot);
      if (itemType === "preprint") {
        this.setItemFieldSafe(item, "repository", "arXiv", zot);
      }
    }
    this.setItemFieldSafe(
      item,
      "url",
      candidate.oa_pdf_url || candidate.landing_page_url || candidate.url || candidate.openalex_id,
      zot
    );

    // Creators
    if (candidate.authors && Array.isArray(candidate.authors) && candidate.authors.length) {
      item.setCreators(this.formatCreators(candidate.authors));
    }

    // Automatic Tags (tagType = 1)
    if (candidate.keywords && Array.isArray(candidate.keywords)) {
      candidate.keywords.forEach((kw) => {
        if (kw) {
          try {
            item.addTag(String(kw).trim(), 1);
          } catch (tagErr) {}
        }
      });
    }

    // Target Collection
    if (
      targetCollectionId !== undefined &&
      targetCollectionId !== null &&
      targetCollectionId !== "root" &&
      targetCollectionId !== "null"
    ) {
      let colID = null;
      if (typeof targetCollectionId === "number") {
        colID = targetCollectionId;
      } else if (typeof targetCollectionId === "string") {
        let trimmed = targetCollectionId.trim();
        if (/^\d+$/.test(trimmed)) {
          colID = parseInt(trimmed, 10);
        } else {
          try {
            let libID = item.libraryID !== undefined && item.libraryID !== null
              ? item.libraryID
              : (zot.Libraries ? zot.Libraries.userLibraryID : 1);
            let col = null;
            if (zot.Collections && typeof zot.Collections.getByLibraryAndKey === "function") {
              col = zot.Collections.getByLibraryAndKey(libID, trimmed);
            }
            if (!col && zot.Collections && typeof zot.Collections.get === "function") {
              col = zot.Collections.get(trimmed);
            }
            if (col && col.id) {
              colID = col.id;
            }
          } catch (colErr) {
            if (zot.debug) {
              zot.debug("Zocrates: Error resolving collection key " + trimmed + ": " + colErr);
            }
          }
        }
      } else if (typeof targetCollectionId === "object" && targetCollectionId !== null) {
        if (targetCollectionId.id) {
          colID = targetCollectionId.id;
        }
      }
      if (colID && typeof item.addToCollection === "function") {
        try {
          item.addToCollection(colID);
        } catch (colAddErr) {
          if (zot.debug) {
            zot.debug("Zocrates: Failed to add item to collection " + colID + ": " + colAddErr);
          }
        }
      }
    }

    // Save item transactionally
    let itemID = await item.saveTx();

    // Import staged PDF attachment if present
    let attachmentKey = null;
    if (stagedPath && typeof stagedPath === "string" && stagedPath.trim().length > 0) {
      try {
        let att = await zot.Attachments.importFromFile({
          file: stagedPath.trim(),
          parentItemID: item.id || itemID,
          libraryID: item.libraryID,
          contentType: "application/pdf",
        });
        if (att && att.key) {
          attachmentKey = att.key;
        }
      } catch (attErr) {
        console.error("Zocrates: ScholarImport failed to attach staged PDF:", attErr, stagedPath);
        if (zot.debug) {
          zot.debug("Zocrates: ScholarImport failed to attach staged PDF: " + attErr);
        }
      }
    }

    // Fallback: If no staged PDF was provided (e.g. publisher blocked non-browser downloads),
    // let Zotero natively attempt to resolve the available file using Gecko's browser engine.
    if (!attachmentKey) {
      try {
        let resolvers = [];
        if (typeof zot.Attachments?.getFileResolvers === "function") {
          try {
            resolvers = zot.Attachments.getFileResolvers(item) || [];
          } catch (rErr) {}
        }

        let oaUrls = [];
        if (candidate.oa_pdf_url) oaUrls.push(candidate.oa_pdf_url);
        if (Array.isArray(candidate.oa_pdf_urls)) {
          candidate.oa_pdf_urls.forEach((u) => {
            if (u && !oaUrls.includes(u)) oaUrls.push(u);
          });
        }
        for (let oaUrl of oaUrls) {
          resolvers.unshift({
            url: oaUrl.toLowerCase().endsWith(".pdf") ? oaUrl : undefined,
            pageURL: oaUrl,
            accessMethod: "oa",
          });
        }

        if (resolvers.length > 0 && typeof zot.Attachments?.addFileFromURLs === "function") {
          let att = await zot.Attachments.addFileFromURLs(item, resolvers, { shouldDisplayCaptcha: true });
          if (att && att.key) {
            attachmentKey = att.key;
          }
        } else if (typeof zot.Attachments?.addAvailableFile === "function") {
          let att = await zot.Attachments.addAvailableFile(item);
          if (att && att.key) {
            attachmentKey = att.key;
          }
        }
      } catch (nativeErr) {
        if (zot.debug) {
          zot.debug("Zocrates: Native addFileFromURLs fallback failed: " + nativeErr);
        }
      }
    }

    let candId = candidate.candidate_id || candidate.id || "";
    let paperUrl = candidate.oa_pdf_url || candidate.landing_page_url || candidate.url || "";
    return {
      item,
      itemID,
      attachmentKey,
      candidateId: candId,
      hasOaUrl: Boolean(candidate.oa_pdf_url),
      oaUrl: paperUrl,
    };
  },

  async importBatch({ stagedItems = [], batchToken = null, targetCollectionId = null, window = null } = {}) {
    let zot = _resolveZotero(window);
    let libraryID = (zot && zot.Libraries && zot.Libraries.userLibraryID) || 1;

    let autoConv = (zot && zot.Zocrates && zot.Zocrates.AutoConverter) ||
                   (typeof AutoConverter !== "undefined" ? AutoConverter : null) ||
                   (typeof window !== "undefined" && window && (window.AutoConverter || window.parent?.Zotero?.Zocrates?.AutoConverter || window.top?.Zotero?.Zocrates?.AutoConverter));

    let createdAttachmentKeys = [];
    let importedItems = [];
    let failedItems = [];
    let suppressedKeys = [];
    let suppressedParentIDs = [];

    try {
      if (Array.isArray(stagedItems)) {
        for (let item of stagedItems) {
          if (!item) continue;
          let candidate = item.candidate || item.metadata || item;
          let stagedPath = item.staged_path || item.stagedPath || "";
          try {
            let res = await this.importPaper({
              candidate,
              stagedPath,
              targetCollectionId,
              libraryID,
              window,
            });
            if (res) {
              importedItems.push(res);
              if (res.itemID && autoConv && typeof autoConv.suppressParentID === "function") {
                autoConv.suppressParentID(res.itemID);
                suppressedParentIDs.push(res.itemID);
              }
              if (res.attachmentKey) {
                createdAttachmentKeys.push(res.attachmentKey);
                if (autoConv && typeof autoConv.suppressKey === "function") {
                  autoConv.suppressKey(res.attachmentKey);
                  suppressedKeys.push(res.attachmentKey);
                }
              }
            }
          } catch (itemErr) {
            failedItems.push({ candidate, error: String(itemErr) });
            console.error("Zocrates: Error importing paper in batch:", itemErr);
            if (zot && zot.debug) {
              zot.debug("Zocrates: Error importing paper in batch: " + itemErr);
            }
          }
        }
      }

      // Cleanup backend staging directory
      if (batchToken) {
        try {
          let fetchFn = _resolveScholarFetch(window);
          if (fetchFn) {
            let serverUrl = this.getServerUrl(window);
            let signal = (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function")
              ? AbortSignal.timeout(5000)
              : undefined;
            let cleanRes = await fetchFn(`${serverUrl}/discover/cleanup_batch`, {
              method: "POST",
              headers: this.getAuthHeaders(window, { "Content-Type": "application/json" }),
              body: JSON.stringify({ batch_token: batchToken }),
              signal,
            });
            if (!cleanRes.ok && zot && zot.debug) {
              zot.debug("Zocrates: Cleanup batch returned non-ok status: " + cleanRes.status);
            }
          }
        } catch (cleanErr) {
          if (zot && zot.debug) {
            zot.debug("Zocrates: Error cleaning up discovery staging: " + cleanErr);
          }
        }
      }

      // Collect regular items that had no OA PDF attachment
      let metadataOnlyKeys = [];
      for (let cand of importedItems) {
        if (!cand.attachmentKey && cand.item && cand.item.key) {
          metadataOnlyKeys.push(cand.item.key);
        }
      }

      // Checkpoint WAL so SQLite changes are immediately visible to backend readers
      if (zot && zot.DB && typeof zot.DB.queryAsync === "function") {
        try {
          await zot.DB.queryAsync("PRAGMA wal_checkpoint(TRUNCATE)");
        } catch (cpErr) {
          if (zot.debug) zot.debug("Zocrates: ScholarImport wal_checkpoint error: " + cpErr);
        }
      }

      if (metadataOnlyKeys.length > 0) {
        if (autoConv && autoConv._convertedCache) {
          for (let k of metadataOnlyKeys) {
            autoConv._convertedCache.add(k);
          }
        }
        try {
          let fetchFn = _resolveScholarFetch(window);
          if (fetchFn) {
            let serverUrl = this.getServerUrl(window);
            let signal = (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function")
              ? AbortSignal.timeout(10000)
              : undefined;
            await fetchFn(`${serverUrl}/items/ingest`, {
              method: "POST",
              headers: this.getAuthHeaders(window, { "Content-Type": "application/json" }),
              body: JSON.stringify({ item_keys: metadataOnlyKeys }),
              signal,
            });
          }
        } catch (ingestErr) {
          if (zot && zot.debug) {
            zot.debug("Zocrates: ScholarImport metadata ingest handoff failed: " + ingestErr);
          }
        }
      }

      // Hand off to BatchConverter if any attachments were imported
      if (createdAttachmentKeys.length > 0) {

        let activeConverterId = "markitdown";
        try {
          let fetchFn = _resolveScholarFetch(window);
          if (fetchFn) {
            let serverUrl = this.getServerUrl(window);
            let signal = (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function")
              ? AbortSignal.timeout(5000)
              : undefined;
            let statusRes = await fetchFn(`${serverUrl}/status`, {
              headers: this.getAuthHeaders(window),
              signal,
            });
            if (statusRes && statusRes.ok) {
              let statusData = await statusRes.json();
              if (statusData && statusData.document_converter_id) {
                activeConverterId = statusData.document_converter_id;
              }
            }
          }
        } catch (statusErr) {
          if (zot && zot.debug) {
            zot.debug("Zocrates: Error querying converter status, defaulting to markitdown: " + statusErr);
          }
        }

        let bc = (zot && zot.Zocrates && zot.Zocrates.BatchConverter) ||
                 (typeof BatchConverter !== "undefined" ? BatchConverter : null) ||
                 (typeof window !== "undefined" && window && (window.BatchConverter || window.parent?.Zotero?.Zocrates?.BatchConverter || window.top?.Zotero?.Zocrates?.BatchConverter));
        if (bc && typeof bc.startBatchConvert === "function") {
          await bc.startBatchConvert(createdAttachmentKeys, activeConverterId, { skipConfirm: true, window });
        }
      }
    } finally {
      if (autoConv) {
        for (let pid of suppressedParentIDs) {
          if (typeof autoConv.unsuppressParentID === "function") autoConv.unsuppressParentID(pid);
        }
        for (let key of suppressedKeys) {
          if (typeof autoConv.unsuppressKey === "function") autoConv.unsuppressKey(key);
        }
      }
    }

    return {
      importedCount: importedItems.length,
      attachmentCount: createdAttachmentKeys.length,
      importedItems,
      createdAttachmentKeys,
      failedItems,
    };
  },
};

if (typeof Zotero !== "undefined") {
  if (!Zotero.Zocrates) Zotero.Zocrates = {};
  Zotero.Zocrates.ScholarImport = ScholarImport;
}
if (typeof window !== "undefined") {
  window.ScholarImport = ScholarImport;
}
if (typeof globalThis !== "undefined") {
  globalThis.ScholarImport = ScholarImport;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = ScholarImport;
}

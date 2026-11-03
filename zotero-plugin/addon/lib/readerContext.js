/**
 * readerContext.js — Reader selection and annotation context capture for Zocrates.
 * Part of Zocrates extension for Zotero.
 */

const PLUGIN_ID = "zocrates@alexplaka.dev";

var ReaderContext = {
  PLUGIN_ID,
  _bound: false,
  _notifierId: null,

  init() {
    if (!this._bound) {
      this._handleViewContextMenu = this._handleViewContextMenu.bind(this);
      this._handleAnnotationContextMenu = this._handleAnnotationContextMenu.bind(this);
      this.notify = this.notify.bind(this);
      this.handleAnnotationModify = this.handleAnnotationModify.bind(this);
      this._bound = true;
    }
    if (typeof Zotero !== "undefined" && Zotero.Reader && typeof Zotero.Reader.registerEventListener === "function") {
      Zotero.Reader.registerEventListener("createViewContextMenu", this._handleViewContextMenu, PLUGIN_ID);
      Zotero.Reader.registerEventListener("createAnnotationContextMenu", this._handleAnnotationContextMenu, PLUGIN_ID);
    }
    if (typeof Zotero !== "undefined" && Zotero.Notifier && typeof Zotero.Notifier.registerObserver === "function") {
      this._notifierId = Zotero.Notifier.registerObserver(
        this,
        ["item"],
        "zocrates-area-resize-watcher"
      );
    }
  },

  destroy() {
    if (typeof Zotero !== "undefined" && Zotero.Reader && typeof Zotero.Reader.unregisterEventListener === "function") {
      Zotero.Reader.unregisterEventListener("createViewContextMenu", this._handleViewContextMenu);
      Zotero.Reader.unregisterEventListener("createAnnotationContextMenu", this._handleAnnotationContextMenu);
    }
    if (this._notifierId && typeof Zotero !== "undefined" && Zotero.Notifier && typeof Zotero.Notifier.unregisterObserver === "function") {
      Zotero.Notifier.unregisterObserver(this._notifierId);
      this._notifierId = null;
    }
  },

  notify(action, type, ids, extraData) {
    if (action !== "modify" || type !== "item" || !Array.isArray(ids)) return;
    try {
      if (typeof dump === "function") {
        dump("[Zocrates RC] notify: action=" + action + " type=" + type + " ids=" + JSON.stringify(ids) + "\n");
      }
    } catch (e) {}
    let promises = [];
    for (const id of ids) {
      promises.push(this.handleAnnotationModify(id));
    }
    return Promise.all(promises);
  },

  async handleAnnotationModify(id) {
    try {
      if (typeof Zotero === "undefined") return;
      let item = Zotero.Items?.get ? Zotero.Items.get(id) : null;
      if (!item && Zotero.Items?.getAsync) {
        try {
          item = await Zotero.Items.getAsync(id);
        } catch (e) {}
      }
      if (!item) return;

      // Check if item is an area/image annotation
      let isImageAnn = false;
      try {
        if (item.isAnnotation && item.isAnnotation() && item.annotationType === "image") isImageAnn = true;
      } catch (e) {}
      if (!isImageAnn) return;

      // Locate the open reader for this parent attachment if available
      let reader = (Zotero.Reader?._readers || []).find(r => r.itemID === item.parentID) ||
                   (typeof Zotero.Reader?.getByTabID === "function" && typeof Zotero_Tabs !== "undefined" ? Zotero.Reader.getByTabID(Zotero_Tabs.selectedID) : null);

      // Attempt to retrieve updated crop with retry loop to absorb reader canvas render / debounce
      let rawDataUrl = null;
      for (let attempt = 0; attempt < 5; attempt++) {
        // 1. Check in-memory reader state
        if (reader) {
          let stateAnns = reader._internalReader?._state?.annotations || reader._state?.annotations || [];
          let annMatch = stateAnns.find(a => a?.id === item.key);
          if (annMatch?.image) {
            rawDataUrl = annMatch.image;
            break;
          }
        }

        // 2. Check Zotero.Annotations.toJSON (disk cache)
        if (Zotero.Annotations?.toJSON) {
          try {
            let json = await Zotero.Annotations.toJSON(item);
            if (json && json.image) {
              rawDataUrl = json.image;
              break;
            }
          } catch (e) {}
        }

        if (attempt < 4) {
          await new Promise(r => setTimeout(r, 150));
        }
      }

      if (!rawDataUrl) {
        if (typeof Zotero !== "undefined") {
          Zotero.debug("Zocrates: Could not resolve updated image for annotation " + item.key);
        }
        return;
      }

      let compressed = await this.extractAndCompressAnnotationImage(reader, { id: item.key, image: rawDataUrl });
      let updatedPayload = {
        annotation_id: item.key,
        annotationId: item.key,
        annotation_item_id: item.id,
        annotationItemId: item.id,
        image_data: compressed.imageData,
        imageData: compressed.imageData,
        thumbnail_data: compressed.thumbnailData,
        thumbnailData: compressed.thumbnailData,
        width: compressed.width,
        height: compressed.height
      };
      if (typeof dump === "function") {
        dump("[Zocrates RC] Broadcasting updateSnippet for " + item.key + " (w=" + compressed.width + ", h=" + compressed.height + ")\n");
      }
      if (Zotero.Zocrates?.updateSnippet) {
        Zotero.Zocrates.updateSnippet(updatedPayload);
      }
    } catch (err) {
      if (typeof Zotero !== "undefined") {
        Zotero.debug("Zocrates: Error handling annotation modify for area resize: " + err);
      }
    }
  },

  _handleViewContextMenu(event) {
    let { reader, params, append } = event;
    if (!append) return;

    let selectedText = (
      reader._internalReader?._primaryView?._iframeWindow?.getSelection()?.toString() ||
      reader._internalReader?._lastView?._iframeWindow?.getSelection()?.toString() ||
      reader._iframeWindow?.getSelection()?.toString() ||
      reader._internalReader?._state?.primaryViewSelectionPopup?.annotation?.text ||
      reader._state?.primaryViewSelectionPopup?.annotation?.text ||
      params?.selectionText ||
      ""
    ).trim();

    if (selectedText) {
      append({
        label: Zotero.getString?.("zocrates-menu-add-context") || "Zocrates: Add to Context",
        onCommand: async () => {
          try {
            let pageIndex = params?.pageIndex ??
              reader._internalReader?._state?.primaryViewStats?.pageIndex ??
              reader._internalReader?._state?.primaryViewState?.pageIndex ??
              null;
            await this.captureTextSnippet(reader, selectedText, pageIndex);
          } catch (e) {
            if (typeof Zotero !== "undefined") {
              Zotero.debug("Zocrates: Error capturing text snippet from view context menu: " + e);
            }
          }
        }
      });
    }
  },

  _handleAnnotationContextMenu(event) {
    let { reader, params, append } = event;
    if (!append) return;

    let annotations = (reader._internalReader?._state?.annotations || reader._state?.annotations || [])
      .filter(a => params?.ids?.includes(a.id));

    let imageAnn = annotations.find(a => a.type === "image");
    if (imageAnn) {
      append({
        label: Zotero.getString?.("zocrates-menu-add-area-context") || "Zocrates: Add Area to Context",
        onCommand: async () => {
          try {
            await this.captureAreaSnippet(reader, imageAnn);
          } catch (e) {
            if (typeof Zotero !== "undefined") {
              Zotero.debug("Zocrates: Error capturing area snippet from annotation menu: " + e);
            }
          }
        }
      });
    }

    let textAnn = annotations.find(a => a.text && a.text.trim());
    if (textAnn && !imageAnn) {
      append({
        label: Zotero.getString?.("zocrates-menu-add-context") || "Zocrates: Add to Context",
        onCommand: async () => {
          try {
            await this.captureTextSnippet(reader, textAnn.text, textAnn.pageIndex);
          } catch (e) {
            if (typeof Zotero !== "undefined") {
              Zotero.debug("Zocrates: Error capturing text snippet from annotation menu: " + e);
            }
          }
        }
      });
    }
  },

  async captureTextSnippet(reader, text, pageIndex) {
    let paperMeta = this.resolveParentPaperMetadata(reader);
    let snippet = {
      id: "snip_" + Date.now() + "_" + Math.random().toString(36).slice(2, 6),
      type: "text",
      item_key: paperMeta.itemKey,
      itemKey: paperMeta.itemKey,
      parent_key: paperMeta.parentKey,
      parentKey: paperMeta.parentKey,
      paper_title: paperMeta.paperTitle,
      paperTitle: paperMeta.paperTitle,
      author_str: paperMeta.authorStr || "",
      authorStr: paperMeta.authorStr || "",
      authors: paperMeta.authors || [],
      page: pageIndex != null ? pageIndex + 1 : null,
      text: (text || "").trim()
    };
    if (typeof Zotero !== "undefined" && Zotero.Zocrates?.addSnippetToContext) {
      Zotero.Zocrates.addSnippetToContext(snippet);
    }
    return snippet;
  },

  async captureAreaSnippet(reader, annotation) {
    try {
      let compressed = await this.extractAndCompressAnnotationImage(reader, annotation);
      let paperMeta = this.resolveParentPaperMetadata(reader);
      let snippet = {
        id: "snip_" + Date.now() + "_" + Math.random().toString(36).slice(2, 6),
        type: "image",
        annotation_id: annotation?.id || annotation?.key || null,
        annotationId: annotation?.id || annotation?.key || null,
        annotation_item_id: annotation?.itemID || annotation?.id || null,
        annotationItemId: annotation?.itemID || annotation?.id || null,
        item_key: paperMeta.itemKey,
        itemKey: paperMeta.itemKey,
        parent_key: paperMeta.parentKey,
        parentKey: paperMeta.parentKey,
        paper_title: paperMeta.paperTitle,
        paperTitle: paperMeta.paperTitle,
        author_str: paperMeta.authorStr || "",
        authorStr: paperMeta.authorStr || "",
        authors: paperMeta.authors || [],
        page: annotation.pageIndex != null ? annotation.pageIndex + 1 : null,
        image_data: compressed.imageData,
        imageData: compressed.imageData,
        thumbnail_data: compressed.thumbnailData,
        thumbnailData: compressed.thumbnailData,
        width: compressed.width,
        height: compressed.height
      };
      if (typeof Zotero !== "undefined" && Zotero.Zocrates?.addSnippetToContext) {
        Zotero.Zocrates.addSnippetToContext(snippet);
      }
      return snippet;
    } catch (e) {
      if (typeof Zotero !== "undefined") {
        Zotero.debug("Zocrates: Error capturing area snippet: " + e);
      }
      throw e;
    }
  },

  async extractAndCompressAnnotationImage(reader, annotation, maxDim = 1200, quality = 0.85) {
    let rawDataUrl = annotation?.image;
    if (!rawDataUrl && annotation?.id && reader?._item?.libraryID) {
      try {
        let item = Zotero.Items?.getByLibraryAndKey?.(reader._item.libraryID, annotation.id);
        if (item && Zotero.Annotations?.toJSON) {
          let json = await Zotero.Annotations.toJSON(item);
          rawDataUrl = json?.image;
        }
      } catch (e) {
        if (typeof Zotero !== "undefined") {
          Zotero.debug("Zocrates: Error reading annotation image JSON: " + e);
        }
      }
    }
    if (!rawDataUrl && annotation?.id) {
      let stateAnns = reader?._internalReader?._state?.annotations || reader?._state?.annotations;
      if (Array.isArray(stateAnns)) {
        let match = stateAnns.find(a => a?.id === annotation.id);
        if (match && match.image) {
          rawDataUrl = match.image;
        }
      }
    }
    if (!rawDataUrl) {
      throw new Error("Could not extract image from area annotation.");
    }

    let doc = reader?._iframeWindow?.document ||
      (typeof Zotero !== "undefined" && Zotero.getMainWindow?.()?.document) ||
      (typeof document !== "undefined" ? document : null);
    if (!doc) {
      throw new Error("DOM document context unavailable for image compression.");
    }
    let ImageCtor = doc.defaultView?.Image || (typeof Image !== "undefined" ? Image : null);
    if (!ImageCtor) {
      throw new Error("Image constructor unavailable.");
    }

    return new Promise((resolve, reject) => {
      let img = new ImageCtor();
      img.onload = () => {
        try {
          let w = img.width;
          let h = img.height;
          if (w > maxDim || h > maxDim) {
            if (w > h) {
              h = Math.round((h * maxDim) / w);
              w = maxDim;
            } else {
              w = Math.round((w * maxDim) / h);
              h = maxDim;
            }
          }
          let canvas = doc.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          let ctx = canvas.getContext("2d");
          // White background to prevent transparent PNG rendering black in JPEG
          ctx.fillStyle = "#FFFFFF";
          ctx.fillRect(0, 0, w, h);
          ctx.drawImage(img, 0, 0, w, h);
          let compressedDataUrl = canvas.toDataURL("image/jpeg", quality);

          // 48px max thumbnail
          let thumbCanvas = doc.createElement("canvas");
          let thumbDim = 48;
          let origW = img.width || 1;
          let origH = img.height || 1;
          let scale = Math.min(thumbDim / origW, thumbDim / origH);
          thumbCanvas.width = Math.max(1, Math.round(origW * scale));
          thumbCanvas.height = Math.max(1, Math.round(origH * scale));
          let thumbCtx = thumbCanvas.getContext("2d");
          thumbCtx.fillStyle = "#FFFFFF";
          thumbCtx.fillRect(0, 0, thumbCanvas.width, thumbCanvas.height);
          thumbCtx.drawImage(img, 0, 0, thumbCanvas.width, thumbCanvas.height);
          let thumbnailDataUrl = thumbCanvas.toDataURL("image/jpeg", 0.7);

          resolve({
            imageData: compressedDataUrl,
            thumbnailData: thumbnailDataUrl,
            width: w,
            height: h
          });
        } catch (err) {
          reject(err);
        }
      };
      img.onerror = (err) => reject(new Error("Failed to decode annotation image: " + err));
      img.src = rawDataUrl;
    });
  },

  resolveParentPaperMetadata(reader) {
    let item = reader?._item;
    if (!item) {
      return { itemKey: null, parentKey: null, paperTitle: "Untitled", authorStr: "", authors: [] };
    }
    let parentKey = item.key;
    let targetItem = item;
    if (item.isAttachment?.() && item.parentItem) {
      targetItem = item.parentItem;
      parentKey = targetItem.key;
    }
    let paperTitle = targetItem.getField?.("title") || "Untitled";
    let authors = [];
    let authorStr = "";
    try {
      let creators = targetItem.getCreatorsJSON?.() || [];
      authors = creators.map(c => c.lastName || c.name || "").filter(Boolean);
      if (authors.length === 1) {
        authorStr = authors[0];
      } else if (authors.length === 2) {
        authorStr = authors[0] + " & " + authors[1];
      } else if (authors.length > 2) {
        authorStr = authors[0] + " et al.";
      }
    } catch (e) {}
    return {
      itemKey: item.key,
      parentKey: parentKey,
      paperTitle: paperTitle,
      authorStr: authorStr,
      authors: authors
    };
  }
};

if (typeof this !== "undefined") {
  this.ReaderContext = ReaderContext;
}
if (typeof Zotero !== "undefined" && Zotero.Zocrates) {
  Zotero.Zocrates.ReaderContext = ReaderContext;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    PLUGIN_ID,
    ReaderContext,
  };
}


/**
 * test_helpers.js — Shared test harness and mock environment for Zocrates front-end tests.
 */

const assert = require("assert");

let testsRun = 0;
let testsPassed = 0;

function test(name, fn) {
  testsRun++;
  try {
    fn();
    testsPassed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err);
    if (typeof process !== "undefined") {
      process.exitCode = 1;
    }
  }
}

async function testAsync(name, fn) {
  testsRun++;
  try {
    await fn();
    testsPassed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err);
    if (typeof process !== "undefined") {
      process.exitCode = 1;
    }
  }
}

function getTestStats() {
  return {
    run: testsRun,
    passed: testsPassed,
    failed: testsRun - testsPassed,
  };
}

function resetTestStats() {
  testsRun = 0;
  testsPassed = 0;
  if (typeof process !== "undefined") {
    process.exitCode = 0;
  }
}

function printTestSummary() {
  console.log(`\n========================================`);
  console.log(`Test Summary: ${testsPassed}/${testsRun} tests passed.`);
  console.log(`========================================\n`);
  return testsPassed === testsRun && testsRun > 0;
}

const VOID_ELEMENTS = new Set([
  "AREA", "BASE", "BR", "COL", "EMBED", "HR", "IMG", "INPUT", "LINK", "META", "PARAM", "SOURCE", "TRACK", "WBR"
]);

function decodeHTMLEntities(str) {
  if (!str) return "";
  return String(str)
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

function escapeHtmlText(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function serializeMockNode(node, isXml = false, parentTag = "") {
  if (!node) return "";
  if (typeof node === "string") return escapeHtmlText(node);
  if (node.nodeType === 3) {
    if (parentTag === "script" || parentTag === "style") {
      return node.textContent || "";
    }
    return escapeHtmlText(node.textContent);
  }
  if (node.nodeType === 8) return `<!--${node.textContent || ""}-->`;
  if (node.nodeType === 1) {
    const rawTag = node.tagName || node.nodeName || "div";
    const tagName = rawTag.toLowerCase();
    const isVoid = !isXml && VOID_ELEMENTS.has(rawTag.toUpperCase());

    let attrStr = "";
    if (node.attributes) {
      for (const k of Object.keys(node.attributes)) {
        const val = node.attributes[k];
        if (val !== undefined && val !== null) {
          attrStr += ` ${k}="${String(val).replace(/"/g, "&quot;")}"`;
        }
      }
    }

    if (isVoid) return `<${tagName}${attrStr}>`;

    let inner = "";
    if (node._innerHTML !== undefined) {
      inner = node._innerHTML;
    } else if (node.children && node.children.length > 0) {
      inner = node.children.map((c) => serializeMockNode(c, isXml, tagName)).join("");
    } else if (node._text !== undefined) {
      inner = (tagName === "script" || tagName === "style") ? node._text : escapeHtmlText(node._text);
    }

    if (isXml && !inner) {
      return `<${tagName}${attrStr} />`;
    }
    return `<${tagName}${attrStr}>${inner}</${tagName}>`;
  }
  return "";
}

function createMockTextNode(text) {
  const node = {
    nodeType: 3,
    nodeName: "#text",
    textContent: String(text !== undefined && text !== null ? text : ""),
    parentElement: null,
    children: [],
    get childNodes() {
      return [];
    },
    get parentNode() {
      return this.parentElement;
    },
    set parentNode(p) {
      this.parentElement = p;
    },
    remove() {
      if (this.parentElement) {
        const idx = this.parentElement.children.indexOf(this);
        if (idx !== -1) this.parentElement.children.splice(idx, 1);
        this.parentElement = null;
      }
    },
    replaceWith(...nodes) {
      if (!this.parentElement) return;
      const parent = this.parentElement;
      const idx = parent.children.indexOf(this);
      if (idx === -1) return;
      const flat = [];
      for (const n of nodes) {
        if (typeof n === "string") flat.push(createMockTextNode(n));
        else if (n) {
          n.parentElement = parent;
          flat.push(n);
        }
      }
      parent.children.splice(idx, 1, ...flat);
      this.parentElement = null;
    },
  };
  return node;
}

function parseHtmlFragment(htmlStr, isXml = false) {
  const root = createMockElement(isXml ? "root" : "div");
  const stack = [root];
  let cursor = 0;
  const len = htmlStr.length;

  while (cursor < len) {
    if (htmlStr.startsWith("<!--", cursor)) {
      const endIdx = htmlStr.indexOf("-->", cursor + 4);
      cursor = endIdx === -1 ? len : endIdx + 3;
      continue;
    }
    if (htmlStr.startsWith("<!DOCTYPE", cursor) || htmlStr.startsWith("<!doctype", cursor)) {
      const endIdx = htmlStr.indexOf(">", cursor + 9);
      cursor = endIdx === -1 ? len : endIdx + 1;
      continue;
    }
    const endTagMatch = htmlStr.slice(cursor).match(/^<\/([a-zA-Z0-9_\-:]+)\s*>/);
    if (endTagMatch) {
      const closeTagName = endTagMatch[1].toLowerCase();
      cursor += endTagMatch[0].length;
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName.toLowerCase() === closeTagName) {
          stack.splice(i);
          break;
        }
      }
      continue;
    }

    const startTagMatch = htmlStr.slice(cursor).match(/^<([a-zA-Z0-9_\-:]+)/);
    if (startTagMatch) {
      const tagName = startTagMatch[1];
      let tagStartPos = cursor + startTagMatch[0].length;
      let inQuote = null;
      let tagEndPos = -1;
      for (let i = tagStartPos; i < len; i++) {
        const ch = htmlStr[i];
        if (inQuote) {
          if (ch === inQuote) inQuote = null;
        } else {
          if (ch === '"' || ch === "'") inQuote = ch;
          else if (ch === ">") {
            tagEndPos = i;
            break;
          }
        }
      }
      if (tagEndPos === -1) {
        cursor = len;
        break;
      }

      let attrStr = htmlStr.slice(tagStartPos, tagEndPos);
      const isSelfClosingXml = attrStr.trimEnd().endsWith("/");
      if (isSelfClosingXml) attrStr = attrStr.trimEnd().slice(0, -1);

      // Handle attribute separators like <svg/onload=alert(1)> without clobbering / inside quotes
      let inQ = null;
      let cleanAttrStr = "";
      for (let i = 0; i < attrStr.length; i++) {
        const ch = attrStr[i];
        if (inQ) {
          if (ch === inQ) inQ = null;
          cleanAttrStr += ch;
        } else {
          if (ch === '"' || ch === "'") {
            inQ = ch;
            cleanAttrStr += ch;
          } else if (ch === "/" && i + 1 < attrStr.length && /[a-zA-Z]/.test(attrStr[i + 1])) {
            cleanAttrStr += " ";
          } else {
            cleanAttrStr += ch;
          }
        }
      }
      attrStr = cleanAttrStr;

      const el = createMockElement(tagName);
      const attrRegex = /([a-zA-Z0-9_\-:]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let aMatch;
      while ((aMatch = attrRegex.exec(attrStr)) !== null) {
        const aName = aMatch[1];
        if (aName === "/") continue;
        let aVal = "";
        if (aMatch[2] !== undefined) aVal = aMatch[2];
        else if (aMatch[3] !== undefined) aVal = aMatch[3];
        else if (aMatch[4] !== undefined) aVal = aMatch[4];
        el.setAttribute(aName, decodeHTMLEntities(aVal));
      }

      const parent = stack[stack.length - 1];
      el.parentElement = parent;
      parent.children.push(el);
      cursor = tagEndPos + 1;

      const lowerTag = tagName.toLowerCase();
      if (lowerTag === "script" || lowerTag === "style") {
        const closeRegex = new RegExp(`</${lowerTag}\\s*>`, "i");
        const closeMatch = htmlStr.slice(cursor).match(closeRegex);
        if (closeMatch) {
          const rawText = htmlStr.slice(cursor, cursor + closeMatch.index);
          const textNode = createMockTextNode(rawText);
          textNode.parentElement = el;
          el.children.push(textNode);
          cursor += closeMatch.index + closeMatch[0].length;
        } else {
          const rawText = htmlStr.slice(cursor);
          const textNode = createMockTextNode(rawText);
          textNode.parentElement = el;
          el.children.push(textNode);
          cursor = len;
        }
        continue;
      }

      const isVoid = !isXml && VOID_ELEMENTS.has(tagName.toUpperCase());
      if (!isSelfClosingXml && !isVoid) {
        stack.push(el);
      }
      continue;
    }

    const nextTagIdx = htmlStr.indexOf("<", cursor);
    const textChunk = nextTagIdx === -1 ? htmlStr.slice(cursor) : htmlStr.slice(cursor, nextTagIdx);
    if (textChunk.length > 0) {
      const textNode = createMockTextNode(decodeHTMLEntities(textChunk));
      const parent = stack[stack.length - 1];
      textNode.parentElement = parent;
      parent.children.push(textNode);
    }
    cursor = nextTagIdx === -1 ? len : nextTagIdx;
  }
  return root.children;
}

function createMockElement(tag, ns) {
  const attrsObj = {};
  Object.defineProperty(attrsObj, Symbol.iterator, {
    enumerable: false,
    configurable: true,
    value: function* () {
      for (const k of Object.keys(this)) {
        yield { name: k, value: this[k] };
      }
    },
  });

  const el = {
    tagName: (tag || "DIV").toUpperCase(),
    get nodeName() {
      return this.tagName;
    },
    nodeType: 1,
    parentElement: null,
    get parentNode() {
      return this.parentElement;
    },
    set parentNode(p) {
      this.parentElement = p;
    },
    ns,
    get namespaceURI() {
      return this.ns || "http://www.w3.org/1999/xhtml";
    },
    style: {
      cssText: "",
      display: "",
      width: "",
      height: "",
      maxHeight: "",
      background: "",
      borderColor: "",
      setProperty(k, v) {
        this[k] = String(v);
      },
      getPropertyValue(k) {
        return this[k] || "";
      },
      removeProperty(k) {
        delete this[k];
      },
    },
    children: [],
    attributes: attrsObj,
    dataset: {},
    classList: {
      _classes: new Set(),
      add(...cls) {
        cls.forEach((c) => c && c.split(/\s+/).forEach((x) => this._classes.add(x)));
      },
      remove(...cls) {
        cls.forEach((c) => c && c.split(/\s+/).forEach((x) => this._classes.delete(x)));
      },
      contains(c) {
        return this._classes.has(c);
      },
      toggle(c, force) {
        if (force !== undefined) {
          if (force) {
            this._classes.add(c);
            return true;
          }
          this._classes.delete(c);
          return false;
        }
        if (this._classes.has(c)) {
          this._classes.delete(c);
          return false;
        }
        this._classes.add(c);
        return true;
      },
    },
    _listeners: new Map(),
    disabled: false,
    checked: false,
    indeterminate: false,
    value: "",
    type: "text",
    placeholder: "",
    title: "",
    selectedIndex: 0,
    options: [],
    _focused: false,
    offsetWidth: 300,
    offsetHeight: 200,
    clientWidth: 300,
    clientHeight: 200,
    offsetLeft: 0,
    offsetTop: 0,
    scrollLeft: 0,
    scrollTop: 0,
    scrollHeight: 200,
    isConnected: true,
    focus() {
      this._focused = true;
    },
    blur() {
      this._focused = false;
    },
    getBoundingClientRect() {
      return {
        left: this.offsetLeft,
        top: this.offsetTop,
        right: this.offsetLeft + this.offsetWidth,
        bottom: this.offsetTop + this.offsetHeight,
        width: this.offsetWidth,
        height: this.offsetHeight,
      };
    },
    getContext(type) {
      return {
        measureText: (t) => ({ width: (t || "").length * 8 }),
        fillRect() {},
        clearRect() {},
        beginPath() {},
        arc() {},
        fill() {},
        stroke() {},
        moveTo() {},
        lineTo() {},
        save() {},
        restore() {},
        scale() {},
        translate() {},
        setLineDash() {},
        strokeText() {},
        fillText() {},
      };
    },
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
    get firstChild() {
      return this.children.length > 0 ? this.children[0] : null;
    },
    get lastChild() {
      return this.children.length > 0 ? this.children[this.children.length - 1] : null;
    },
    get childNodes() {
      return this.children;
    },
    get innerHTML() {
      if (this._innerHTML !== undefined) return this._innerHTML;
      return this.children.map((c) => serializeMockNode(c, false)).join("");
    },
    set innerHTML(val) {
      this._innerHTML = undefined;
      this._text = undefined;
      this.children = [];
      if (val) {
        const parsed = parseHtmlFragment(String(val), false);
        for (const child of parsed) {
          this.appendChild(child);
        }
      }
      if (this.tagName === "SELECT") {
        this.options = this.children.filter((c) => c.tagName === "OPTION");
      }
    },
    get outerHTML() {
      return serializeMockNode(this, false);
    },
    get className() {
      return Array.from(this.classList._classes).join(" ");
    },
    set className(val) {
      this.classList._classes = new Set(val ? val.split(/\s+/).filter(Boolean) : []);
    },
    get textContent() {
      if (this.children.length > 0) {
        return this.children.map((c) => (typeof c === "string" ? c : c.textContent || "")).join("");
      }
      if (this._text !== undefined) return this._text;
      return "";
    },
    set textContent(val) {
      this._text = String(val);
      this._innerHTML = undefined;
      this.children = [];
    },
    setAttribute(k, v) {
      this.attributes[k] = String(v);
      if (k === "class") {
        this.className = String(v);
      }
      if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, l) => l.toUpperCase());
        this.dataset[camel] = String(v);
      }
    },
    getAttribute(k) {
      if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, l) => l.toUpperCase());
        if (this.dataset[camel] !== undefined) return this.dataset[camel];
      }
      return this.attributes[k] !== undefined ? this.attributes[k] : null;
    },
    removeAttribute(k) {
      delete this.attributes[k];
      if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, l) => l.toUpperCase());
        delete this.dataset[camel];
      }
    },
    getAttributeNames() {
      return Object.keys(this.attributes);
    },
    hasAttribute(k) {
      return this.getAttribute(k) !== null;
    },
    replaceWith(...nodes) {
      if (!this.parentElement) return;
      const parent = this.parentElement;
      const idx = parent.children.indexOf(this);
      if (idx === -1) return;
      const flat = [];
      for (const n of nodes) {
        if (typeof n === "string") flat.push(createMockTextNode(n));
        else if (n) {
          n.parentElement = parent;
          flat.push(n);
        }
      }
      parent.children.splice(idx, 1, ...flat);
      this.parentElement = null;
    },
    getElementsByTagName(tag) {
      const lower = (tag || "").toLowerCase();
      const res = [];
      function walk(n) {
        if (!n || !n.children) return;
        for (const c of n.children) {
          if (c && c.nodeType === 1) {
            if (lower === "*" || (c.tagName || "").toLowerCase() === lower) {
              res.push(c);
            }
            walk(c);
          }
        }
      }
      walk(this);
      return res;
    },
    appendChild(child) {
      this._innerHTML = undefined;
      this._text = undefined;
      if (typeof child === "object" && child) {
        child.parentElement = this;
        this.children.push(child);
        if (this.tagName === "SELECT" && child.tagName === "OPTION") this.options.push(child);
      } else {
        const tn = createMockTextNode(child);
        tn.parentElement = this;
        this.children.push(tn);
      }
      return child;
    },
    removeChild(child) {
      this._innerHTML = undefined;
      this._text = undefined;
      const idx = this.children.indexOf(child);
      if (idx !== -1) {
        this.children.splice(idx, 1);
        child.parentElement = null;
      }
      if (this.tagName === "SELECT" && child.tagName === "OPTION") {
        const optIdx = this.options.indexOf(child);
        if (optIdx !== -1) this.options.splice(optIdx, 1);
      }
      return child;
    },
    insertBefore(newChild, refChild) {
      this._innerHTML = undefined;
      this._text = undefined;
      let nodeToInsert = newChild;
      if (typeof newChild !== "object" || !newChild) {
        nodeToInsert = createMockTextNode(newChild);
      }
      nodeToInsert.parentElement = this;
      const idx = this.children.indexOf(refChild);
      if (idx !== -1) this.children.splice(idx, 0, nodeToInsert);
      else this.children.push(nodeToInsert);
      return nodeToInsert;
    },
    remove() {
      if (this.parentElement) {
        const idx = this.parentElement.children.indexOf(this);
        if (idx !== -1) this.parentElement.children.splice(idx, 1);
        this.parentElement = null;
      }
    },
    addEventListener(evt, fn) {
      if (!this._listeners.has(evt)) this._listeners.set(evt, []);
      this._listeners.get(evt).push(fn);
    },
    removeEventListener(evt, fn) {
      if (this._listeners.has(evt)) {
        const arr = this._listeners.get(evt).filter((h) => h !== fn);
        this._listeners.set(evt, arr);
      }
    },
    dispatchEvent(evt) {
      const type = typeof evt === "string" ? evt : evt.type;
      const handlers = this._listeners.get(type) || [];
      const eventObj = typeof evt === "string" ? { type: evt, target: this, stopPropagation: () => {}, preventDefault: () => {} } : evt;
      if (!eventObj.target) eventObj.target = this;
      handlers.forEach((h) => h.call(this, eventObj));
      if (this.parentElement && typeof this.parentElement.dispatchEvent === "function") {
        this.parentElement.dispatchEvent(eventObj);
      }
    },
    querySelector(selector) {
      const all = this.querySelectorAll(selector);
      return all.length > 0 ? all[0] : null;
    },
    querySelectorAll(selector) {
      const parts = selector.trim().split(/\s+/).filter(Boolean);
      if (parts.length > 1) {
        let currentNodes = [this];
        for (const part of parts) {
          const nextNodes = [];
          for (const node of currentNodes) {
            nextNodes.push(...node.querySelectorAll(part));
          }
          currentNodes = nextNodes;
        }
        return currentNodes;
      }

      const res = [];
      function match(node, sel) {
        if (!node || typeof node !== "object") return false;
        sel = sel.trim();
        if (sel.startsWith("#")) return node.id === sel.slice(1);
        if (sel === ":not(:disabled)") return !node.disabled;
        if (sel.includes(":not(:disabled)")) {
          const base = sel.replace(":not(:disabled)", "");
          return match(node, base) && !node.disabled;
        }
        if (sel.includes(".") && !sel.startsWith(".")) {
          const dotIdx = sel.indexOf(".");
          const tagPart = sel.slice(0, dotIdx);
          const classPart = sel.slice(dotIdx);
          if (tagPart && (node.tagName || "").toLowerCase() !== tagPart.toLowerCase()) {
            return false;
          }
          return match(node, classPart);
        }
        if (sel.includes("[") && !sel.startsWith("[")) {
          const tagPart = sel.slice(0, sel.indexOf("["));
          const bracketPart = sel.slice(sel.indexOf("["));
          if (tagPart) {
            if (tagPart.startsWith(".")) {
              const classes = tagPart.slice(1).split(".").filter(Boolean);
              if (!classes.every((c) => node.classList && node.classList.contains(c))) return false;
            } else {
              if ((node.tagName || "").toLowerCase() !== tagPart.toLowerCase()) return false;
            }
          }
          return match(node, bracketPart);
        }
        if (sel.startsWith(".")) {
          const classes = sel.slice(1).split(".").filter(Boolean);
          return classes.every((c) => node.classList && node.classList.contains(c));
        }
        if (sel.startsWith("[") && sel.endsWith("]")) {
          const inner = sel.slice(1, -1);
          if (inner.includes("=")) {
            const [attr, rawVal] = inner.split("=");
            const val = rawVal.replace(/^["']|["']$/g, "");
            if (attr.startsWith("data-")) {
              const camel = attr.slice(5).replace(/-([a-z])/g, (_, l) => l.toUpperCase());
              return (node.dataset && node.dataset[camel] === val) || (node.getAttribute && node.getAttribute(attr) === val);
            }
            return (node.getAttribute && node.getAttribute(attr) === val) || node[attr] === val;
          }
          return (node.getAttribute && node.getAttribute(inner) !== null) || node[inner] !== undefined;
        }
        return (node.tagName || "").toLowerCase() === sel.toLowerCase();
      }
      function traverse(n) {
        if (!n || !Array.isArray(n.children)) return;
        for (let child of n.children) {
          if (typeof child !== "object" || !child) continue;
          if (match(child, selector)) res.push(child);
          traverse(child);
        }
      }
      traverse(this);
      return res;
    },
    closest(selector) {
      let cur = this;
      while (cur) {
        if (selector.startsWith(".") && cur.classList && cur.classList.contains(selector.slice(1))) return cur;
        if ((cur.tagName || "").toLowerCase() === selector.toLowerCase()) return cur;
        cur = cur.parentElement;
      }
      return null;
    },
  };
  return el;
}

function createMockZotero() {
  const prefs = new Map();
  const collections = new Map([
    [101, { id: 101, key: "COL123", name: "Deep Learning", parentID: null, getChildCollections: () => [] }],
  ]);
  const items = new Map();
  let nextId = 1;

  class MockItem {
    constructor(itemType) {
      this.itemType = itemType || "journalArticle";
      this.itemTypeID = itemType === "preprint" ? 2 : 1;
      this.fields = {};
      this.creators = [];
      this.tags = [];
      this.collections = [];
      this.id = null;
      this.key = "ITEM_" + Math.random().toString(36).substring(2, 8);
      this.parentItem = null;
      this._attachments = [];
    }
    setField(field, val) {
      if (field === "invalidField") throw new Error("Invalid field");
      this.fields[field] = val;
    }
    getField(field) {
      return this.fields[field];
    }
    getDisplayTitle() {
      return this.fields["title"] || this.fields["shortTitle"] || "(no title)";
    }
    getCreatorsJSON() {
      return this.creators;
    }
    getCreators() {
      return this.creators;
    }
    setCreators(creators) {
      this.creators = creators;
    }
    addTag(tag, type) {
      this.tags.push({ tag, type });
    }
    addToCollection(colId) {
      this.collections.push(colId);
    }
    async saveTx() {
      this.id = nextId++;
      items.set(this.id, this);
      return this.id;
    }
    isAttachment() {
      return this.itemType === "attachment";
    }
    isRegularItem() {
      return this.itemType !== "attachment" && this.itemType !== "note";
    }
    isNote() {
      return this.itemType === "note";
    }
    getAttachments() {
      return this._attachments.map((a) => a.id || a);
    }
    getFilename() {
      return this._filename || (this.fields["title"] ? `${this.fields["title"]}.pdf` : "document.pdf");
    }
  }

  class MockCollection {
    constructor() {
      this.id = nextId++;
      this.name = "";
      this.libraryID = 1;
      this.parentID = null;
    }
    async saveTx() {
      collections.set(this.id, this);
      return this.id;
    }
    getChildCollections() {
      return [];
    }
  }

  const zotero = {
    Prefs: {
      get(key, global) {
        if (!global && !key.startsWith("extensions.zotero.")) {
          return prefs.get("extensions.zotero." + key) ?? prefs.get(key);
        }
        return prefs.get(key);
      },
      set(key, val, global) {
        if (!global && !key.startsWith("extensions.zotero.")) {
          prefs.set("extensions.zotero." + key, val);
        }
        prefs.set(key, val);
      },
      clear(key, global) {
        if (!global && !key.startsWith("extensions.zotero.")) {
          prefs.delete("extensions.zotero." + key);
        } else {
          prefs.delete(key);
        }
      },
      registerObserver(branch, handler) {
        return 1;
      },
      unregisterObserver(id) {},
    },
    Item: MockItem,
    Collection: MockCollection,
    ItemFields: {
      getID(name) {
        return name === "invalidField" ? null : 10;
      },
      isValidForType(fieldId, typeId) {
        return fieldId !== 999;
      },
    },
    ItemTypes: {
      isValidForType(type) {
        return type === "preprint" || type === "journalArticle" || type === "note";
      },
      getID(type) {
        return type === "preprint" ? 2 : 1;
      },
    },
    Libraries: {
      userLibraryID: 1,
      get(id) {
        return { id, name: id === 1 ? "My Library" : "Other Library" };
      },
      getName(id) {
        return id === 1 ? "My Library" : "Other Library";
      },
    },
    Collections: {
      get(idOrKey) {
        if (typeof idOrKey === "number") return collections.get(idOrKey) || null;
        for (let col of collections.values()) {
          if (col.key === idOrKey) return col;
        }
        return null;
      },
      getByLibrary(libId) {
        return Array.from(collections.values());
      },
      getByLibraryAndKey(libId, key) {
        for (let col of collections.values()) {
          if (col.key === key) return col;
        }
        return null;
      },
    },
    Attachments: {
      imported: [],
      async importFromFile({ file, parentItemID, libraryID, contentType }) {
        const att = {
          id: nextId++,
          key: "ATT_" + Math.random().toString(36).substring(2, 8),
          file,
          parentItemID,
          libraryID,
          contentType: contentType || "application/pdf",
          isAttachment: () => true,
          isPDFAttachment: () => true,
        };
        this.imported.push(att);
        items.set(att.id, att);
        const parent = items.get(parentItemID);
        if (parent && parent._attachments) {
          parent._attachments.push(att);
        }
        return att;
      },
      async addAvailableFile(item) {
        if (item.getField("DOI") || item.getField("url")) {
          const att = {
            id: nextId++,
            key: "ATT_NATIVE_" + Math.random().toString(36).substring(2, 8),
            parentItemID: item.id,
            libraryID: item.libraryID,
            isAttachment: () => true,
            isPDFAttachment: () => true,
          };
          this.imported.push(att);
          items.set(att.id, att);
          if (item._attachments) item._attachments.push(att);
          return att;
        }
        return false;
      },
    },
    Notifier: {
      observers: [],
      registerObserver(obs, types, name, prio) {
        this.observers.push({ obs, types, name, prio });
        return 1;
      },
      unregisterObserver(id) {
        this.observers = [];
      },
    },
    Items: {
      get(id) {
        return items.get(id) || null;
      },
      async getAsync(id) {
        return items.get(id) || null;
      },
    },
    Search: function () {
      this.libraryID = 1;
      this.conditions = [];
      this.addCondition = (c, op, v) => {
        this.conditions.push({ c, op, v });
      };
      this.search = async () => [];
    },
    Utilities: {
      cleanDOI: (d) => d,
    },
    debug(msg) {},
    Zocrates: {},
    getActiveZoteroPane() {
      return null;
    },
    getMainWindow() {
      return { ZoteroPane: null };
    },
  };

  return zotero;
}

let singletonEnv = null;

function setupMockEnvironment() {
  if (singletonEnv) {
    singletonEnv.document.body.children = [];
    singletonEnv.document.body._innerHTML = undefined;
    singletonEnv.document.body._text = undefined;
    if (singletonEnv.elementsById.has("chat-messages")) {
      singletonEnv.elementsById.get("chat-messages").children = [];
      singletonEnv.elementsById.get("chat-messages")._innerHTML = undefined;
      singletonEnv.elementsById.get("chat-messages")._text = undefined;
    }
    if (singletonEnv.elementsById.has("chips-container")) {
      singletonEnv.elementsById.get("chips-container").children = [];
      singletonEnv.elementsById.get("chips-container")._innerHTML = undefined;
      singletonEnv.elementsById.get("chips-container")._text = undefined;
    }
    singletonEnv.elementsById.delete("zocrates-toolbar-btn");
    singletonEnv.elementsById.delete("zocrates-toolbar-style");
    if (!singletonEnv.document.createXULElement) {
      singletonEnv.document.createXULElement = (t) => createMockElement(t.replace(/^html:/, ""), "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul");
    }
    if (!singletonEnv.document.documentElement) {
      singletonEnv.document.documentElement = createMockElement("html");
    }
    if (global.localStorage && typeof global.localStorage.clear === "function") {
      global.localStorage.clear();
    }
    if (global.Services?.obs?.observers) {
      global.Services.obs.observers.clear();
    }
    return singletonEnv;
  }

  const elementsById = new Map();
  const DYNAMIC_IDS = new Set([
    "bootstrap-progress-card",
    "onboarding-wizard-card",
    "onboarding-process-card",
    "zocrates-toolbar-btn",
    "zocrates-toolbar-style",
    "zocrates-info-modal-overlay",
  ]);

  function findElementById(root, id) {
    if (!root || typeof root !== "object") return null;
    if (root.id === id) return root;
    if (Array.isArray(root.children)) {
      for (const child of root.children) {
        const found = findElementById(child, id);
        if (found) return found;
      }
    }
    return null;
  }

  const mockDoc = {
    createElement: (t) => createMockElement(t),
    createElementNS: (ns, t) => createMockElement(t, ns),
    createXULElement: (t) => createMockElement(t.replace(/^html:/, ""), "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul"),
    createTextNode: (t) => String(t),
    getElementById: (id) => {
      for (const root of elementsById.values()) {
        const found = findElementById(root, id);
        if (found) return found;
      }
      for (const root of [mockDoc.body, mockDoc.head]) {
        const found = findElementById(root, id);
        if (found) return found;
      }
      if (DYNAMIC_IDS.has(id)) {
        return null;
      }
      if (!elementsById.has(id)) {
        const tag = id.endsWith("-select") ? "select" : "div";
        const el = createMockElement(tag);
        el.id = id;
        elementsById.set(id, el);
      }
      return elementsById.get(id);
    },
    querySelector: (sel) => mockDoc.body.querySelector(sel),
    querySelectorAll: (sel) => mockDoc.body.querySelectorAll(sel),
    addEventListener: () => {},
    removeEventListener: () => {},
    head: createMockElement("head"),
    body: createMockElement("body"),
    documentElement: createMockElement("html"),
  };

  const winListeners = new Map();
  const mockWindow = {
    addEventListener: (evt, fn) => {
      if (!winListeners.has(evt)) winListeners.set(evt, []);
      winListeners.get(evt).push(fn);
    },
    removeEventListener: (evt, fn) => {
      if (winListeners.has(evt)) {
        winListeners.set(evt, winListeners.get(evt).filter((h) => h !== fn));
      }
    },
    dispatchEvent: (evt) => {
      const type = typeof evt === "string" ? evt : evt.type;
      const handlers = winListeners.get(type) || [];
      const eventObj = typeof evt === "string" ? { type: evt, target: mockWindow, preventDefault: () => {}, stopPropagation: () => {} } : evt;
      if (!eventObj.target) eventObj.target = mockWindow;
      handlers.slice().forEach((h) => h.call(mockWindow, eventObj));
    },
    location: { href: "", search: "" },
    document: mockDoc,
    innerWidth: 1024,
    innerHeight: 768,
    devicePixelRatio: 1,
    matchMedia: (query) => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
  };

  mockWindow.mermaid = {
    initialize: () => {},
    render: async (id, code) => ({ svg: `<svg id="${id}"></svg>` }),
  };

  // Load vendored marked parser if available
  try {
    const marked = require("../../zotero-plugin/addon/lib/marked.min.js");
    mockWindow.marked = marked;
    global.marked = marked;
  } catch (e) {}

  mockDoc.defaultView = mockWindow;

  global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  global.cancelAnimationFrame = (id) => clearTimeout(id);
  global.window = mockWindow;
  global.document = mockDoc;
  global.alert = () => {};

  // Mock localStorage
  const storageMap = new Map();
  global.localStorage = {
    getItem(k) {
      return storageMap.has(k) ? storageMap.get(k) : null;
    },
    setItem(k, v) {
      storageMap.set(k, String(v));
    },
    removeItem(k) {
      storageMap.delete(k);
    },
    clear() {
      storageMap.clear();
    },
  };
  mockWindow.localStorage = global.localStorage;

  // Mock Services
  const observersMap = new Map();
  global.Services = {
    obs: {
      observers: observersMap,
      addObserver(obs, topic) {
        if (!observersMap.has(topic)) observersMap.set(topic, new Set());
        observersMap.get(topic).add(obs);
      },
      removeObserver(obs, topic) {
        if (observersMap.has(topic)) {
          observersMap.get(topic).delete(obs);
        }
      },
      notifyObservers(subject, topic, data) {
        const set = observersMap.get(topic);
        if (set) {
          for (let obs of set) {
            try {
              if (typeof obs.observe === "function") {
                obs.observe(subject, topic, data);
              } else if (typeof obs === "function") {
                obs(subject, topic, data);
              }
            } catch (e) {}
          }
        }
      },
    },
    prompt: {
      confirm(win, title, text) {
        return true;
      },
    },
  };

  // Mock DOMParser & XMLSerializer
  global.DOMParser = class MockDOMParser {
    parseFromString(str, mimeType = "text/html") {
      const isXml = mimeType === "image/svg+xml" || mimeType === "application/xml" || mimeType === "text/xml";
      const nodes = parseHtmlFragment(String(str || ""), isXml);

      if (isXml) {
        const rootEl = nodes.find((n) => n.nodeType === 1) || createMockElement("svg");
        return {
          nodeType: 9,
          nodeName: "#document",
          documentElement: rootEl,
          firstChild: rootEl,
          childNodes: [rootEl],
          children: [rootEl],
          getElementsByTagName(tag) {
            const lower = (tag || "").toLowerCase();
            const res = [];
            if (lower === "*" || (rootEl.tagName || "").toLowerCase() === lower) res.push(rootEl);
            res.push(...rootEl.getElementsByTagName(tag));
            return res;
          },
          querySelector(sel) {
            return rootEl.querySelector(sel);
          },
          querySelectorAll(sel) {
            return rootEl.querySelectorAll(sel);
          },
        };
      }

      const htmlEl = nodes.find((n) => n.nodeType === 1 && (n.tagName || "").toLowerCase() === "html");
      let bodyEl = null;
      let headEl = null;

      if (htmlEl) {
        bodyEl = htmlEl.children.find((n) => n.nodeType === 1 && (n.tagName || "").toLowerCase() === "body");
        headEl = htmlEl.children.find((n) => n.nodeType === 1 && (n.tagName || "").toLowerCase() === "head");
      }

      if (!htmlEl) {
        const docHtml = createMockElement("html");
        headEl = createMockElement("head");
        bodyEl = createMockElement("body");
        docHtml.appendChild(headEl);
        docHtml.appendChild(bodyEl);
        for (const node of nodes) {
          bodyEl.appendChild(node);
        }
      } else {
        if (!bodyEl) {
          bodyEl = createMockElement("body");
          htmlEl.appendChild(bodyEl);
        }
        if (!headEl) {
          headEl = createMockElement("head");
          htmlEl.children.unshift(headEl);
        }
      }

      return {
        nodeType: 9,
        nodeName: "#document",
        documentElement: htmlEl || bodyEl.parentElement,
        head: headEl,
        body: bodyEl,
        getElementById(id) {
          return bodyEl.querySelector(`#${id}`) || (headEl && headEl.querySelector(`#${id}`)) || null;
        },
        getElementsByTagName(tag) {
          const lower = tag.toLowerCase();
          const res = [];
          if (lower === "body") res.push(bodyEl);
          if (lower === "head" && headEl) res.push(headEl);
          if (lower === "html" && (htmlEl || bodyEl.parentElement)) res.push(htmlEl || bodyEl.parentElement);
          res.push(...bodyEl.getElementsByTagName(tag));
          if (headEl) res.push(...headEl.getElementsByTagName(tag));
          return res;
        },
        querySelector(sel) {
          return bodyEl.querySelector(sel) || (headEl && headEl.querySelector(sel)) || null;
        },
        querySelectorAll(sel) {
          return [...bodyEl.querySelectorAll(sel), ...(headEl ? headEl.querySelectorAll(sel) : [])];
        },
      };
    }
  };
  global.window.DOMParser = global.DOMParser;

  global.XMLSerializer = class MockXMLSerializer {
    serializeToString(node) {
      if (!node) return "";
      if (node.nodeType === 9) {
        return serializeMockNode(node.documentElement || node.body, true);
      }
      return serializeMockNode(node, true);
    }
  };
  global.window.XMLSerializer = global.XMLSerializer;

  const mockZotero = createMockZotero();
  global.Zotero = mockZotero;
  global.window.Zotero = mockZotero;

  singletonEnv = {
    window: mockWindow,
    document: mockDoc,
    zotero: mockZotero,
    elementsById,
  };

  return singletonEnv;
}

module.exports = {
  test,
  testAsync,
  getTestStats,
  resetTestStats,
  printTestSummary,
  createMockElement,
  createMockTextNode,
  createMockZotero,
  setupMockEnvironment,
};

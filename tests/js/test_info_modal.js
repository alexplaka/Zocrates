/**
 * test_info_modal.js — Tests for infoModal.js module.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: InfoModal ---");
  const env = setupMockEnvironment();
  const InfoModal = require("../../zotero-plugin/addon/lib/infoModal.js");

  test("renders action buttons with primary button styling (#2563eb) and executes click handlers", async () => {
    const createdElements = [];
    const listeners = new Map();

    const mockDocument = {
      getElementById: () => null,
      createElementNS: (ns, tag) => {
        const el = {
          tagName: tag,
          style: {
            setProperty(k, v) { this[k] = String(v); },
            getPropertyValue(k) { return this[k] || ""; },
          },
          attributes: {},
          setAttribute(k, v) { this.attributes[k] = String(v); },
          getAttribute(k) { return this.attributes[k] !== undefined ? this.attributes[k] : null; },
          children: [],
          appendChild(child) {
            this.children.push(child);
          },
          addEventListener(evt, handler) {
            listeners.set(evt + "_" + (this.textContent || this.id || tag), handler);
          },
          remove() {
            this.removed = true;
          },
        };
        createdElements.push(el);
        return el;
      },
      addEventListener: () => {},
      removeEventListener: () => {},
      body: {
        appendChild(child) {},
      },
    };

    let importClicked = false;
    let cancelClicked = false;

    InfoModal.show({
      window: { document: mockDocument },
      title: "Confirm Import",
      rows: ["3 papers selected"],
      buttons: [
        {
          label: "Cancel",
          onClick: () => {
            cancelClicked = true;
          },
        },
        {
          label: "Confirm & Import",
          primary: true,
          onClick: () => {
            importClicked = true;
          },
        },
      ],
    });

    const buttons = createdElements.filter((el) => el.tagName === "button");
    const confirmBtn = buttons.find((b) => b.textContent === "Confirm & Import");
    const cancelBtn = buttons.find((b) => b.textContent === "Cancel");

    assert.ok(confirmBtn, "Confirm & Import button should be rendered");
    assert.ok(cancelBtn, "Cancel button should be rendered");
    assert.ok(
      confirmBtn.style.cssText.includes("#2563eb"),
      "Primary button must have #2563eb background styling"
    );

    // Click cancel
    const cancelHandler = listeners.get("click_Cancel");
    assert.ok(cancelHandler, "Click handler for Cancel should be registered");
    await cancelHandler({ stopPropagation: () => {} });
    assert.strictEqual(cancelClicked, true, "Cancel onClick handler should be executed");

    // Click confirm
    const clickHandler = listeners.get("click_Confirm & Import");
    assert.ok(clickHandler, "Click handler must be registered on button");
    await clickHandler({ stopPropagation: () => {} });
    assert.strictEqual(importClicked, true, "onClick handler should be executed");
  });

  test("closes modal overlay on close button click or overlay background click", () => {
    let overlayRemoved = false;
    let createdElements = [];

    const mockDocument = {
      getElementById: () => null,
      createElementNS: (ns, tag) => {
        const el = createMockElement(tag, ns);
        createdElements.push(el);
        return el;
      },
      addEventListener: () => {},
      removeEventListener: () => {},
      body: {
        appendChild(child) {},
      },
    };

    InfoModal.show({
      window: { document: mockDocument },
      title: "Test Modal",
    });

    const overlay = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
    assert.ok(overlay, "Overlay should be created");
    overlay.remove = () => {
      overlayRemoved = true;
    };

    const closeBtn = createdElements.find((el) => el.tagName === "BUTTON" && el.textContent === "✕");
    assert.ok(closeBtn, "Close button ✕ should be rendered");

    // Click close button
    closeBtn.dispatchEvent("click");
    assert.strictEqual(overlayRemoved, true, "Overlay should be removed when close button is clicked");
  });

  test("theme conformity: sets --bg-card and data-theme according to targetDoc.body data-theme", () => {
    let createdElements = [];

    const createDocWithTheme = (themeName) => {
      createdElements = [];
      const body = createMockElement("body");
      if (themeName) {
        body.setAttribute("data-theme", themeName);
      }
      return {
        getElementById: () => null,
        createElementNS: (ns, tag) => {
          const el = createMockElement(tag, ns);
          createdElements.push(el);
          return el;
        },
        addEventListener: () => {},
        removeEventListener: () => {},
        body: body,
      };
    };

    // Test all 5 themes
    const expectedThemes = {
      slate: { bgCard: "#1e293b", chipBg: "rgba(255, 255, 255, 0.06)" },
      amoled: { bgCard: "#0d0d0d", chipBg: "rgba(255, 255, 255, 0.08)" },
      "zotero-dark": { bgCard: "#2d2d2d", chipBg: "rgba(255, 255, 255, 0.06)" },
      "zotero-light": { bgCard: "#ffffff", chipBg: "rgba(0, 0, 0, 0.05)" },
      sepia: { bgCard: "#fdfbf7", chipBg: "rgba(192, 92, 54, 0.08)" },
    };

    for (const [tName, exp] of Object.entries(expectedThemes)) {
      InfoModal.show({
        window: { document: createDocWithTheme(tName) },
        title: `${tName} Theme Modal`,
      });
      const o = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
      assert.ok(o, `Overlay created for ${tName}`);
      assert.strictEqual(o.getAttribute("data-theme"), tName);
      assert.strictEqual(o.style.getPropertyValue("--bg-card") || o.style["--bg-card"], exp.bgCard);
      assert.strictEqual(o.style.getPropertyValue("--chip-bg") || o.style["--chip-bg"], exp.chipBg);
    }

    // Normalization of "light" -> "zotero-light"
    InfoModal.show({
      window: { document: createDocWithTheme("light") },
      title: "Light Theme Modal",
    });
    let o = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
    assert.strictEqual(o.getAttribute("data-theme"), "zotero-light");
    assert.strictEqual(o.style.getPropertyValue("--bg-card") || o.style["--bg-card"], "#ffffff");

    // Normalization of unknown theme -> "slate"
    InfoModal.show({
      window: { document: createDocWithTheme("unknown_custom_theme") },
      title: "Unknown Theme Modal",
    });
    o = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
    assert.strictEqual(o.getAttribute("data-theme"), "slate");
    assert.strictEqual(o.style.getPropertyValue("--bg-card") || o.style["--bg-card"], "#1e293b");

    // Fallback to Zotero.Prefs when body has no data-theme
    const origPrefs = global.Zotero?.Prefs;
    try {
      global.Zotero = global.Zotero || {};
      global.Zotero.Prefs = {
        get(key) {
          if (key === "zocrates.theme") return "sepia";
          return null;
        },
      };
      InfoModal.show({
        window: { document: createDocWithTheme(null) },
        title: "Prefs Theme Modal",
      });
      o = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
      assert.strictEqual(o.getAttribute("data-theme"), "sepia");
      assert.strictEqual(o.style.getPropertyValue("--bg-card") || o.style["--bg-card"], "#fdfbf7");
    } finally {
      if (origPrefs) global.Zotero.Prefs = origPrefs;
    }
  });

  await testAsync("supports wide/maxWidth options and keeps modal open when button onClick returns false", async () => {
    const createdElements = [];
    const mockDocument = {
      getElementById: () => null,
      createElementNS: (ns, tag) => {
        const el = createMockElement(tag, ns);
        createdElements.push(el);
        return el;
      },
      addEventListener: () => {},
      removeEventListener: () => {},
      body: createMockElement("body"),
    };

    let closedCount = 0;
    let copyClicks = 0;

    InfoModal.show({
      window: { document: mockDocument },
      wide: true,
      title: "Wide Diagnostic Modal",
      rows: ["Preview row"],
      onClose: () => {
        closedCount++;
      },
      buttons: [
        {
          label: "Copy",
          primary: true,
          onClick: () => {
            copyClicks++;
            return false;
          },
        },
        {
          label: "Close",
          onClick: () => true,
        },
      ],
    });

    const overlay = createdElements.find((el) => el.id === "zocrates-info-modal-overlay");
    const card = createdElements.find((el) => el.className === "zocrates-info-modal-card");
    assert.ok(overlay, "Overlay must exist");
    assert.ok(card, "Modal card must exist");
    assert.ok(card.style.cssText.includes("width: 680px"), "wide: true must apply 680px card width");

    let overlayRemoved = false;
    overlay.remove = () => {
      overlayRemoved = true;
    };

    const copyBtn = createdElements.find((el) => el.tagName === "BUTTON" && el.textContent === "Copy");
    const closeActionBtn = createdElements.find((el) => el.tagName === "BUTTON" && el.textContent === "Close");
    assert.ok(copyBtn, "Copy button must be rendered");
    assert.ok(closeActionBtn, "Close button must be rendered");

    copyBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(copyClicks, 1, "Copy handler should execute");
    assert.strictEqual(overlayRemoved, false, "Returning false must NOT close the modal");
    assert.strictEqual(closedCount, 0, "onClose must not run when modal stays open");

    closeActionBtn.dispatchEvent("click");
    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(overlayRemoved, true, "Returning true must close the modal");
    assert.strictEqual(closedCount, 1, "onClose callback must fire once on dismissal");
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

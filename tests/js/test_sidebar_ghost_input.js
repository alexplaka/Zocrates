/**
 * test_sidebar_ghost_input.js — Tests for predictive next-prompt ghost input in sidebar.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Predictive Next-Prompt Ghost Input ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  const ghostChatInput = env.document.getElementById("chat-input");
  const ghostOverlay = env.document.getElementById("ghost-input-overlay");
  const ghostText = env.document.getElementById("ghost-input-text");

  ghostChatInput.setSelectionRange = () => {};
  ghostChatInput.dispatchEvent = function (evt) {
    const handlers = this._listeners.get(evt.type) || [];
    handlers.forEach((h) => h.call(this, evt));
  };

  const ghostWrap = createMockElement("div");
  ghostWrap.classList.add("chat-input-wrap");
  ghostWrap.appendChild(ghostOverlay);

  const SUGGESTION = "Compare Medusa heads against our library draft models";

  function resetGhostDom() {
    sidebar.clearGhostSuggestion();
    ghostChatInput.value = "";
  }

  function fireKeydown(init) {
    let prevented = false;
    const evt = Object.assign(
      {
        type: "keydown",
        shiftKey: false,
        preventDefault() {
          prevented = true;
        },
      },
      init
    );
    ghostChatInput.dispatchEvent(evt);
    return prevented;
  }

  test("setGhostSuggestion shows overlay and suppresses placeholder when input is empty", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    assert.strictEqual(ghostOverlay.style.display, "flex");
    assert.strictEqual(ghostText.textContent, SUGGESTION);
    assert.ok(ghostWrap.classList.contains("has-ghost"));
  });

  test("updateGhostVisibility hides overlay when textarea has text and restores it when emptied", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    ghostChatInput.value = "typing";
    sidebar.updateGhostVisibility();
    assert.strictEqual(ghostOverlay.style.display, "none");
    assert.strictEqual(ghostWrap.classList.contains("has-ghost"), false);

    ghostChatInput.value = "";
    sidebar.updateGhostVisibility();
    assert.strictEqual(ghostOverlay.style.display, "flex");
    assert.ok(ghostWrap.classList.contains("has-ghost"));
  });

  test("Tab accepts the ghost suggestion into the textarea and clears the overlay", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    const prevented = fireKeydown({ key: "Tab" });
    assert.strictEqual(prevented, true);
    assert.strictEqual(ghostChatInput.value, SUGGESTION);
    assert.strictEqual(ghostOverlay.style.display, "none");
    assert.strictEqual(ghostWrap.classList.contains("has-ghost"), false);
  });

  test("ArrowRight at caret start accepts the suggestion", () => {
    resetGhostDom();
    ghostChatInput.selectionStart = 0;
    ghostChatInput.selectionEnd = 0;
    sidebar.setGhostSuggestion(SUGGESTION);
    assert.strictEqual(fireKeydown({ key: "ArrowRight" }), true);
    assert.strictEqual(ghostChatInput.value, SUGGESTION);
  });

  test("Shift+Tab passes through without accepting the suggestion", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    const prevented = fireKeydown({ key: "Tab", shiftKey: true });
    assert.strictEqual(prevented, false);
    assert.strictEqual(ghostChatInput.value, "");
    assert.strictEqual(ghostOverlay.style.display, "flex");
  });

  test("Tab does not accept when the textarea already has text", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    ghostChatInput.value = "my own prompt";
    const prevented = fireKeydown({ key: "Tab" });
    assert.strictEqual(prevented, false);
    assert.strictEqual(ghostChatInput.value, "my own prompt");
  });

  test("Escape dismisses the suggestion without calling preventDefault", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    const prevented = fireKeydown({ key: "Escape" });
    assert.strictEqual(prevented, false);
    assert.strictEqual(ghostOverlay.style.display, "none");
    assert.strictEqual(ghostChatInput.value, "");
  });

  test("clearGhostSuggestion disables later Tab acceptance (submit / session-switch cleanup)", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    sidebar.clearGhostSuggestion();
    assert.strictEqual(ghostOverlay.style.display, "none");
    assert.strictEqual(fireKeydown({ key: "Tab" }), false);
    assert.strictEqual(ghostChatInput.value, "");
  });

  test("setGhostSuggestion with blank input clears any existing suggestion", () => {
    resetGhostDom();
    sidebar.setGhostSuggestion(SUGGESTION);
    sidebar.setGhostSuggestion("   ");
    assert.strictEqual(ghostOverlay.style.display, "none");
    assert.strictEqual(fireKeydown({ key: "Tab" }), false);
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

/**
 * test_sidebar_mermaid.js — Tests for Mermaid diagram decoration and toggling in sidebar.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar Mermaid Blocks ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  test("decorateMermaidCodeBlocks: injects toggle button, preserves copy button, and unescapes raw code", () => {
    const container = createMockElement("div");

    // Construct mock code block wrapper for mermaid
    const wrapper = createMockElement("div");
    wrapper.classList.add("code-block-wrapper");

    const header = createMockElement("div");
    header.classList.add("code-block-header");

    const langLabel = createMockElement("span");
    langLabel.classList.add("code-lang-label");
    langLabel.textContent = "mermaid";
    header.appendChild(langLabel);

    const copyBtn = createMockElement("button");
    copyBtn.classList.add("code-copy-btn");
    copyBtn.textContent = "Copy";
    header.appendChild(copyBtn);

    wrapper.appendChild(header);

    const pre = createMockElement("pre");
    const code = createMockElement("code");
    code.textContent = "graph TD\n  A[Input] --&gt; B[Retrieval]\n  B ==&gt; C[Synthesis]";
    pre.appendChild(code);
    wrapper.appendChild(pre);

    container.appendChild(wrapper);

    // Decorate container
    sidebar.decorateMermaidCodeBlocks(container);

    // 1. Verify toggle button injected before copy button
    const toggleBtn = header.querySelector(".mermaid-toggle-btn");
    assert.ok(toggleBtn, "Toggle button should be injected into header");
    assert.strictEqual(toggleBtn.getAttribute("type"), "button");

    // Copy button still exists
    assert.ok(header.querySelector(".code-copy-btn"), "Copy button must be preserved");

    // 2. Verify raw code preservation and HTML entity unescaping
    assert.ok(wrapper._rawMermaidCode, "wrapper._rawMermaidCode should be cached");
    assert.ok(wrapper._rawMermaidCode.includes("A[Input] --> B[Retrieval]"), "Entity --&gt; must be unescaped to -->");
    assert.ok(wrapper._rawMermaidCode.includes("B ==> C[Synthesis]"), "Entity ==&gt; must be unescaped to ==>");
  });

  test("decorateMermaidCodeBlocks: ignores non-mermaid code blocks", () => {
    const container = createMockElement("div");

    const wrapper = createMockElement("div");
    wrapper.classList.add("code-block-wrapper");

    const header = createMockElement("div");
    header.classList.add("code-block-header");

    const langLabel = createMockElement("span");
    langLabel.classList.add("code-lang-label");
    langLabel.textContent = "python";
    header.appendChild(langLabel);

    wrapper.appendChild(header);

    const pre = createMockElement("pre");
    const code = createMockElement("code");
    code.textContent = "def hello(): pass";
    pre.appendChild(code);
    wrapper.appendChild(pre);

    container.appendChild(wrapper);

    sidebar.decorateMermaidCodeBlocks(container);

    assert.strictEqual(header.querySelector(".mermaid-toggle-btn"), null);
    assert.strictEqual(wrapper._rawMermaidCode, undefined);
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

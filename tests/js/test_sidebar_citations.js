/**
 * test_sidebar_citations.js — Tests for citations and evidence popover in sidebar.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar Citations & Evidence Peeks ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. hasMessageCitations
  test("hasMessageCitations: detects brackets and cited sources, suppresses for discovery responses", () => {
    const textWithCitations = "Here is some text citing paper [1] and [2] and # Cited Sources.";

    // Online discovery suppression
    assert.strictEqual(sidebar.hasMessageCitations(textWithCitations, { candidates: [{ candidate_id: "c1" }] }), false);
    assert.strictEqual(sidebar.hasMessageCitations(textWithCitations, { author: "online_search_agent" }), false);
    assert.strictEqual(sidebar.hasMessageCitations(textWithCitations, { author: "Online Synthesizer" }), false);

    // Citations present
    assert.strictEqual(sidebar.hasMessageCitations(textWithCitations, null), true);
    assert.strictEqual(sidebar.hasMessageCitations("Claims supported by evidence.", { citations: [{ index: 1 }] }), true);
    assert.strictEqual(sidebar.hasMessageCitations("Considered literature.", { references: [{ title: "Ref" }] }), true);

    // No citations
    assert.strictEqual(sidebar.hasMessageCitations("Plain answer without any citations or sources.", null), false);
  });

  // 2. ensureCitationToggleBtn
  test("ensureCitationToggleBtn: manages citation toggle button presence based on citation metadata", () => {
    const wrapper = createMockElement("div");
    wrapper.className = "message-wrapper assistant";
    const bubble = createMockElement("div");
    bubble.className = "message-bubble";
    bubble._rawText = "Online discovery text [1]";
    bubble._metadata = { candidates: [{ candidate_id: "cand1" }] };
    wrapper.appendChild(bubble);

    const actionsDiv = createMockElement("div");
    actionsDiv.className = "message-actions";
    const actionsLeft = createMockElement("div");
    actionsLeft.className = "message-actions-left";
    const existingToggle = createMockElement("button");
    existingToggle.className = "citation-toggle-btn";
    actionsLeft.appendChild(existingToggle);
    actionsDiv.appendChild(actionsLeft);
    wrapper.appendChild(actionsDiv);

    // Suppressed when discovery candidates present
    assert.ok(wrapper.querySelector(".citation-toggle-btn"));
    sidebar.ensureCitationToggleBtn(wrapper);
    assert.strictEqual(wrapper.querySelector(".citation-toggle-btn"), null);

    // Added when normal citations present
    bubble._metadata = { citations: [{ index: 1 }] };
    sidebar.ensureCitationToggleBtn(wrapper);
    assert.ok(wrapper.querySelector(".citation-toggle-btn"), "Toggle button should be restored for cited message");
  });

  // 3. stripCitationsFromText
  test("stripCitationsFromText: removes bracket tokens and sources section while protecting code", () => {
    const raw = "Result shows 95% accuracy [1, 2] and [Chunk 3].\n\n```python\nval = arr[1]\n```\n\n# Cited Sources\n- [1] Paper A";
    const clean = sidebar.stripCitationsFromText(raw);

    assert.ok(clean.includes("Result shows 95% accuracy"));
    assert.ok(!clean.includes("[1, 2]"));
    assert.ok(!clean.includes("[Chunk 3]"));
    assert.ok(!clean.includes("# Cited Sources"));
    assert.ok(clean.includes("val = arr[1]"), "Code block brackets must be preserved");
  });

  // 4. buildReferencesMarkdown
  test("buildReferencesMarkdown: partitions Tier 1 Cited Sources and Tier 2 Considered Sources", () => {
    const refs = [
      {
        authors: "He & Zhang",
        year: 2016,
        title: "Deep Residual Learning",
        publication: "CVPR",
        tier: 1,
      },
      {
        authors: "Vaswani et al.",
        year: 2017,
        title: "Attention Is All You Need",
        publication: "NeurIPS",
        tier: 2,
      },
    ];

    const md = sidebar.buildReferencesMarkdown(refs);

    assert.ok(md.includes("# Cited Sources"));
    assert.ok(md.includes("- **He & Zhang (2016)**. *Deep Residual Learning*. CVPR."));
    assert.ok(md.includes("**1 Uncited Source Considered by the Agent**"));
    assert.ok(md.includes("- **Vaswani et al. (2017)**. *Attention Is All You Need*. NeurIPS."));
  });

  // 5. showEvidencePopover & closeEvidencePopover
  test("showEvidencePopover: constructs popover DOM with breadcrumbs, calibrated badges, and action buttons", () => {
    sidebar.closeEvidencePopover();

    const pill = createMockElement("span");
    pill.setAttribute("data-citation-occ", "1");

    // Case A: High support probability (>= 0.80)
    const citationHigh = {
      title: "FlashAttention-2",
      authors: "Dao",
      year: 2023,
      publication: "ICLR",
      section: "Hardware Benchmarks",
      quote: "Achieved 2x speedup on A100 GPUs.",
      attachment_key: "ATT_FA2",
      item_key: "ITEM_FA2",
      support_probability: 0.92,
    };

    sidebar.showEvidencePopover(pill, citationHigh);
    const popover = env.document.body.querySelector(".evidence-popover");
    assert.ok(popover, "Popover should be appended to document.body");

    // Header title
    assert.strictEqual(popover.querySelector(".evidence-popover-title").textContent, "FlashAttention-2");

    // Meta
    assert.strictEqual(popover.querySelector(".evidence-popover-author-year").textContent, "Dao (2023)");

    // Section badge
    assert.strictEqual(popover.querySelector(".evidence-popover-section-badge").textContent, "§ Hardware Benchmarks");

    // Support score badge (high)
    const highBadge = popover.querySelector(".evidence-support-badge.high");
    assert.ok(highBadge);
    assert.strictEqual(highBadge.textContent, "✓ Support Probability: 0.92");

    // Quote
    assert.ok(popover.querySelector(".evidence-popover-quote").textContent.includes("Achieved 2x speedup"));

    // Action buttons
    const readerBtn = popover.querySelector(".popover-open-reader-btn");
    assert.ok(readerBtn);
    assert.strictEqual(readerBtn.textContent, "Open in Reader");
    assert.strictEqual(readerBtn.getAttribute("data-attachment-key"), "ATT_FA2");

    const selectBtn = popover.querySelector(".popover-select-item-btn");
    assert.ok(selectBtn);
    assert.strictEqual(selectBtn.textContent, "Select in Library");
    assert.strictEqual(selectBtn.getAttribute("data-item-key"), "ITEM_FA2");

    // Close
    sidebar.closeEvidencePopover();
    assert.strictEqual(env.document.body.querySelector(".evidence-popover"), null);

    // Case B: Moderate support probability (0.50 - 0.80)
    const citationMod = {
      title: "Moderate Paper",
      support_probability: 0.65,
    };
    sidebar.showEvidencePopover(pill, citationMod);
    const popoverMod = env.document.body.querySelector(".evidence-popover");
    assert.ok(popoverMod.querySelector(".evidence-support-badge.moderate"));
    assert.strictEqual(popoverMod.querySelector(".evidence-support-badge.moderate").textContent, "ⓘ Support Probability: 0.65");
    sidebar.closeEvidencePopover();

    // Case C: Low support probability (< 0.50)
    const citationLow = {
      title: "Low Support Paper",
      support_probability: 0.35,
    };
    sidebar.showEvidencePopover(pill, citationLow);
    const popoverLow = env.document.body.querySelector(".evidence-popover");
    assert.ok(popoverLow.querySelector(".evidence-support-badge.low"));
    assert.strictEqual(popoverLow.querySelector(".evidence-support-badge.low").textContent, "Support Probability: 0.35");
    sidebar.closeEvidencePopover();
  });

  // 6. Citation pills inside Markdown tables
  test("formatMessageContent: renders citation pills inside markdown tables without breaking cells, unescaped spans, or pipe splitting", () => {
    const tableMd = [
      "| Model | Architecture | Benchmark |",
      "| --- | --- | --- |",
      "| ResNet-50 | Residual learning [1] | 92.5% [2] |",
      "| Transformer | Self-attention [2, 3] | 94.1% [1] |",
    ].join("\n");

    const metadata = {
      citations: [
        {
          index: 1,
          title: "Deep Residual Learning | CVPR 2016",
          authors: "He et al.",
          year: 2016,
          section: "Architecture & ResNet Details",
          support_probability: 0.95,
        },
        {
          index: 2,
          title: "Attention Is All You Need | NeurIPS 2017",
          authors: "Vaswani et al.",
          year: 2017,
          section: "Model Architecture",
          support_probability: 0.88,
        },
        {
          index: 3,
          title: "BERT: Pre-training of Deep Bidirectional Transformers",
          authors: "Devlin et al.",
          year: 2019,
          section: "Pre-training BERT",
          support_probability: 0.76,
        },
      ],
    };

    const renderedHtml = sidebar.formatMessageContent(tableMd, metadata);

    // 1. Table structure must remain intact
    assert.ok(renderedHtml.includes("<table"), "Output should contain <table> element");
    assert.ok(renderedHtml.includes("</table>"), "Output should contain </table> element");
    assert.ok(renderedHtml.includes("<th>Model</th>"), "Table header Model should exist");
    assert.ok(renderedHtml.includes("<th>Architecture</th>"), "Table header Architecture should exist");
    assert.ok(renderedHtml.includes("<th>Benchmark</th>"), "Table header Benchmark should exist");

    // 2. No escaped HTML spans (<span must NOT become &lt;span)
    assert.ok(!renderedHtml.includes("&lt;span"), "Output must not contain escaped &lt;span");
    assert.ok(!renderedHtml.includes("&lt;/span&gt;"), "Output must not contain escaped &lt;/span&gt;");

    // 3. Spans are properly present as live HTML elements
    assert.ok(renderedHtml.includes('<span class="citation-pill" data-citation-idx="1"'), "Citation pill 1 should be present");
    assert.ok(renderedHtml.includes('<span class="citation-pill" data-citation-idx="2"'), "Citation pill 2 should be present");
    assert.ok(renderedHtml.includes('<span class="citation-pill" data-citation-idx="3"'), "Citation pill 3 should be present");

    // 4. Pipes in titles (e.g. '| CVPR 2016' or '| NeurIPS 2017') must not split table cells
    assert.ok(renderedHtml.includes("Deep Residual Learning | CVPR 2016"), "Title with pipe should be preserved in tooltip attribute");
    assert.ok(renderedHtml.includes("Attention Is All You Need | NeurIPS 2017"), "Second title with pipe should be preserved in tooltip attribute");

    // 5. Tooltip newlines must be encoded as &#10;, not literal newlines
    assert.ok(renderedHtml.includes("&#10;Section:"), "Tooltip newline must be encoded as &#10;");
    assert.ok(renderedHtml.includes("&#10;Support probability:"), "Tooltip newline must be encoded as &#10;");
    // Ensure no raw unescaped newlines inside the title attribute
    const pillTitleMatches = renderedHtml.match(/title="([^"]*)"/g) || [];
    assert.ok(pillTitleMatches.length >= 4, "Should find title attributes for citation pills");
    for (const match of pillTitleMatches) {
      assert.ok(!match.includes("\n"), "Title attribute must not contain raw unencoded newlines");
      assert.ok(!match.includes("\r"), "Title attribute must not contain raw unencoded carriage returns");
    }
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

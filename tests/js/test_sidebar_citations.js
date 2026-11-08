/**
 * test_sidebar_citations.js — Tests for citations and evidence popover in sidebar.js.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

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

    // Header row and title
    const headerRow = popover.querySelector(".evidence-popover-header");
    assert.ok(headerRow, "Header row should exist");
    assert.strictEqual(popover.querySelector(".evidence-popover-title").textContent, "FlashAttention-2");

    const maxBtn = headerRow.querySelector(".evidence-popover-maximize-btn");
    assert.ok(maxBtn, "Maximize button should exist");
    assert.strictEqual(maxBtn.textContent, "⤢");
    assert.strictEqual(maxBtn.getAttribute("title"), "Maximize in sidebar");

    // 8 Resizer elements
    const resizers = popover.querySelectorAll(".evidence-popover-resizer");
    assert.strictEqual(resizers.length, 8, "Should create 8 resizers");
    const directions = ["n", "s", "e", "w", "nw", "ne", "sw", "se"];
    directions.forEach((dir) => {
      const resizerEl = popover.querySelector(`.evidence-popover-resizer.resizer-${dir}`);
      assert.ok(resizerEl, `Resizer for direction ${dir} should exist`);
      assert.strictEqual(resizerEl.getAttribute("data-direction"), dir);
    });

    // Maximize button click toggles is-maximized
    maxBtn.dispatchEvent("click");
    assert.ok(popover.classList.contains("is-maximized"), "Clicking maximize button should add is-maximized");
    assert.strictEqual(maxBtn.textContent, "⤡");
    assert.strictEqual(maxBtn.getAttribute("title"), "Restore size");
    assert.ok(popover._prevGeometry, "Should store previous geometry");

    // Restore via maximize button click
    maxBtn.dispatchEvent("click");
    assert.strictEqual(popover.classList.contains("is-maximized"), false, "Clicking restore button should remove is-maximized");
    assert.strictEqual(maxBtn.textContent, "⤢");
    assert.strictEqual(maxBtn.getAttribute("title"), "Maximize in sidebar");

    // Header double-click toggles is-maximized
    headerRow.dispatchEvent("dblclick");
    assert.ok(popover.classList.contains("is-maximized"), "Double-clicking header should maximize");
    headerRow.dispatchEvent("dblclick");
    assert.strictEqual(popover.classList.contains("is-maximized"), false, "Double-clicking header again should restore");

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
    assert.strictEqual(readerBtn.getAttribute("data-section"), "Hardware Benchmarks");

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

    // Case D: Markdown and LaTeX math in section badge renders formatted HTML in innerHTML
    const prevKatex = env.window.katex;
    env.window.katex = {
      renderToString: (math) => `<span class="katex">${math}</span>`,
    };
    try {
      const citationMath = {
        title: "Complexity Analysis",
        section: "Analysis of **$O(N)$** Bounds",
        support_probability: 0.85,
      };
      sidebar.showEvidencePopover(pill, citationMath);
      const popoverMath = env.document.body.querySelector(".evidence-popover");
      const secBadge = popoverMath.querySelector(".evidence-popover-section-badge");
      assert.ok(secBadge, "Section badge should exist");
      assert.strictEqual(
        secBadge.innerHTML,
        "§ Analysis of <strong><span class=\"katex\">O(N)</span></strong> Bounds",
        "Section badge should render formatted markdown and math in secDiv.innerHTML"
      );
      assert.strictEqual(secBadge.textContent, "§ Analysis of O(N) Bounds");
      sidebar.closeEvidencePopover();

      // Case E: Plain section, script tags, and XSS sanitization
      const citationXss = {
        title: "Security Test",
        section: "Methods <script>alert(1)</script> <img src=x onerror=alert(1)> > Implementation",
      };
      sidebar.showEvidencePopover(pill, citationXss);
      const popoverXss = env.document.body.querySelector(".evidence-popover");
      const secBadgeXss = popoverXss.querySelector(".evidence-popover-section-badge");
      assert.ok(secBadgeXss, "Section badge should exist");
      assert.ok(!secBadgeXss.innerHTML.includes("<script"), "Script tags must be stripped");
      assert.ok(!secBadgeXss.innerHTML.includes("onerror"), "Event handlers must be sanitized");
      assert.ok(secBadgeXss.textContent.startsWith("§ Methods"), "Prefix § must be present");
      assert.ok(secBadgeXss.textContent.includes("Implementation"), "Section title text must be preserved");
      sidebar.closeEvidencePopover();
    } finally {
      env.window.katex = prevKatex;
    }
  });

  // 6. Smart placement in narrow sidebars (< 420px)
  test("showEvidencePopover: smart placement in narrow sidebars (< 420px)", () => {
    sidebar.closeEvidencePopover();
    const pill = createMockElement("span");
    pill.offsetLeft = 50;
    pill.offsetWidth = 20;
    const citation = { title: "Narrow Sidebar Test" };

    const origInnerWidth = env.window.innerWidth;
    try {
      // 1. Sidebar width 360px (< 420): width = Math.max(260, 360 - 20) = 340px, left = 10px
      env.window.innerWidth = 360;
      sidebar.showEvidencePopover(pill, citation);
      let popover = env.document.body.querySelector(".evidence-popover");
      assert.ok(popover);
      assert.strictEqual(popover.style.width, "340px");
      assert.strictEqual(popover.style.left, "10px");
      sidebar.closeEvidencePopover();

      // 2. Very narrow sidebar 270px: width = Math.max(260, 270 - 20) = 260px (minWidth clamp)
      env.window.innerWidth = 270;
      sidebar.showEvidencePopover(pill, citation);
      popover = env.document.body.querySelector(".evidence-popover");
      assert.ok(popover);
      assert.strictEqual(popover.style.width, "260px");
      assert.strictEqual(popover.style.left, "10px");
      sidebar.closeEvidencePopover();

      // 3. Normal sidebar width 800px (>= 420): standard clamped positioning
      env.window.innerWidth = 800;
      sidebar.showEvidencePopover(pill, citation);
      popover = env.document.body.querySelector(".evidence-popover");
      assert.ok(popover);
      assert.strictEqual(popover.style.left, "50px");
      sidebar.closeEvidencePopover();
    } finally {
      env.window.innerWidth = origInnerWidth;
      sidebar.closeEvidencePopover();
    }
  });

  // 7. Multi-directional resizers and dragging
  test("showEvidencePopover: multi-directional resizers resize popover and clear maximized state on drag", () => {
    sidebar.closeEvidencePopover();
    const pill = createMockElement("span");
    pill.offsetLeft = 40;
    const citation = { title: "Resize Test", quote: "Testing multi-directional resize." };

    sidebar.showEvidencePopover(pill, citation);
    const popover = env.document.body.querySelector(".evidence-popover");
    assert.ok(popover);

    const maxBtn = popover.querySelector(".evidence-popover-maximize-btn");
    const origLeft = popover.style.left;
    const origTop = popover.style.top;
    const origWidth = popover.style.width;
    const origHeight = popover.style.height;

    // Maximize first
    maxBtn.dispatchEvent("click");
    assert.ok(popover.classList.contains("is-maximized"));

    // Toggle back (restore)
    maxBtn.dispatchEvent("click");
    assert.strictEqual(popover.classList.contains("is-maximized"), false);
    assert.strictEqual(popover.style.left, origLeft, "Restored left must match pre-maximize left");
    assert.strictEqual(popover.style.top, origTop, "Restored top must match pre-maximize top");
    assert.strictEqual(popover.style.width, origWidth, "Restored width must match pre-maximize width");

    // Maximize again for the drag test
    maxBtn.dispatchEvent("click");
    assert.ok(popover.classList.contains("is-maximized"));

    // 1. Start drag on resizer-se: should clear maximized state and update width/height
    const seResizer = popover.querySelector(".evidence-popover-resizer.resizer-se");
    assert.ok(seResizer);
    seResizer.dispatchEvent({
      type: "mousedown",
      clientX: 100,
      clientY: 100,
      preventDefault() {},
      stopPropagation() {},
    });

    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 150,
      clientY: 160,
    });

    assert.strictEqual(popover.classList.contains("is-maximized"), false, "Manual drag should clear is-maximized");
    assert.strictEqual(maxBtn.textContent, "⤢", "Maximize button icon should reset to unmaximized");
    assert.strictEqual(maxBtn.getAttribute("title"), "Maximize in sidebar");
    assert.strictEqual(popover.style.width, "350px"); // startWidth (300) + 50
    assert.strictEqual(popover.style.height, "260px"); // startHeight (200) + 60
    assert.strictEqual(popover.style.maxHeight, "260px");

    env.window.dispatchEvent({ type: "mouseup" });

    // 2. Drag west resizer (resizer-w): shrinks left and grows width
    const wResizer = popover.querySelector(".evidence-popover-resizer.resizer-w");
    assert.ok(wResizer);
    wResizer.dispatchEvent({
      type: "mousedown",
      clientX: 100,
      clientY: 100,
      preventDefault() {},
      stopPropagation() {},
    });

    // Move left by 20px -> left should become startLeft (40) - 20 = 20px, width grows by 20px (from 350 to 370)
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 80,
      clientY: 100,
    });

    assert.strictEqual(popover.style.left, "20px");
    assert.strictEqual(popover.style.width, "370px");

    env.window.dispatchEvent({ type: "mouseup" });

    // 3. Drag north resizer (resizer-n): shrinks top and grows height
    const nResizer = popover.querySelector(".evidence-popover-resizer.resizer-n");
    assert.ok(nResizer);
    nResizer.dispatchEvent({
      type: "mousedown",
      clientX: 100,
      clientY: 100,
      preventDefault() {},
      stopPropagation() {},
    });

    const currTop = parseFloat(popover.style.top);
    // Move up by 30px
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 100,
      clientY: 70,
    });

    assert.strictEqual(popover.style.top, `${Math.max(10, currTop - 30)}px`);
    assert.strictEqual(popover.style.height, "290px"); // startHeight (260) + 30
    assert.strictEqual(popover.style.maxHeight, "290px");

    env.window.dispatchEvent({ type: "mouseup" });

    // 4. Enforce minWidth (260) and minHeight (180) clamps
    seResizer.dispatchEvent({
      type: "mousedown",
      clientX: 100,
      clientY: 100,
      preventDefault() {},
      stopPropagation() {},
    });

    // Try to shrink width and height by 500px (beyond minWidth 260 and minHeight 180)
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: -400,
      clientY: -400,
    });

    assert.strictEqual(popover.style.width, "260px", "Width must not shrink below minWidth 260");
    assert.strictEqual(popover.style.height, "180px", "Height must not shrink below minHeight 180");

    env.window.dispatchEvent({ type: "mouseup" });
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

  // 7. extractLeafHeading & normalizeForComparison
  test("extractLeafHeading: extracts leaf headings, breadcrumbs, strips symbols, and rejects synthetic labels", () => {
    // Single heading
    const single = sidebar.extractLeafHeading("3.2.2 Multi-Head Attention");
    assert.strictEqual(single.leaf, "3.2.2 Multi-Head Attention");
    assert.deepStrictEqual(single.parents, []);

    // Breadcrumbs
    const crumbs = sidebar.extractLeafHeading("A > B > C");
    assert.strictEqual(crumbs.leaf, "C");
    assert.deepStrictEqual(crumbs.parents, ["A", "B"]);

    const deepCrumbs = sidebar.extractLeafHeading("Attention Is All You Need > 3 Model Architecture > 3.2 Attention > 3.2.2 Multi-Head Attention");
    assert.strictEqual(deepCrumbs.leaf, "3.2.2 Multi-Head Attention");
    assert.deepStrictEqual(deepCrumbs.parents, ["Attention Is All You Need", "3 Model Architecture", "3.2 Attention"]);

    // Section symbol
    const secSym = sidebar.extractLeafHeading("§ 4.1");
    assert.strictEqual(secSym.leaf, "4.1");
    assert.deepStrictEqual(secSym.parents, []);

    const secSymWithCrumbs = sidebar.extractLeafHeading("Paper > § 4.1 Setup");
    assert.strictEqual(secSymWithCrumbs.leaf, "4.1 Setup");
    assert.deepStrictEqual(secSymWithCrumbs.parents, ["Paper"]);

    // Markdown symbols and math formatting
    const formatted = sidebar.extractLeafHeading("§ *4.2* `$W_i^Q$` `Projections`");
    assert.strictEqual(formatted.leaf, "4.2 Wi^Q Projections");

    const mathHeading = sidebar.extractLeafHeading("A leaky integrator of $[Ca^{2+}]$ signals");
    assert.strictEqual(mathHeading.leaf, "A leaky integrator of [Ca^{2+}] signals");

    // Real sections starting with Table / Figure should NOT be rejected
    assert.strictEqual(sidebar.extractLeafHeading("Table Detection Methods").leaf, "Table Detection Methods");
    assert.strictEqual(sidebar.extractLeafHeading("Figure Generation Algorithms").leaf, "Figure Generation Algorithms");

    // Synthetic or document-level labels return empty leaf
    assert.deepStrictEqual(sidebar.extractLeafHeading("Frontmatter"), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading("Full Document"), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading("Abstract"), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading("Figure 3: Attention diagram"), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading("Table 1: BLEU scores"), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading(""), { leaf: "", parents: [] });
    assert.deepStrictEqual(sidebar.extractLeafHeading(null), { leaf: "", parents: [] });

    // Document title match returns empty leaf
    const docTitleMatch = sidebar.extractLeafHeading("Deep Residual Learning for Image Recognition", "Deep Residual Learning for Image Recognition");
    assert.deepStrictEqual(docTitleMatch, { leaf: "", parents: [] });

    // isSyntheticOrTitle
    assert.strictEqual(sidebar.isSyntheticOrTitle("Frontmatter"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Full Document"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Abstract"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Figure 1: Attention diagram"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Table 2: BLEU score comparison"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Attention Is All You Need", "Attention Is All You Need"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("attention is all you need", "Attention Is All You Need"), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle("§ 3.2.2 Multi-Head Attention", "Attention Is All You Need"), false);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Table Detection Methods"), false);
    assert.strictEqual(sidebar.isSyntheticOrTitle("Figure Generation Algorithms"), false);
    assert.strictEqual(sidebar.isSyntheticOrTitle(""), true);
    assert.strictEqual(sidebar.isSyntheticOrTitle(null), true);

    // normalizeForComparison
    assert.strictEqual(sidebar.normalizeForComparison("Café naïve résumé"), "cafe naive resume");
    assert.strictEqual(sidebar.normalizeForComparison("Multi-Head  Attention: [2023]"), "multi head attention 2023");
    assert.strictEqual(sidebar.normalizeForComparison("Multi-Head Attention"), "multi head attention");
    assert.strictEqual(sidebar.normalizeForComparison("Multi Head Attention"), "multi head attention");
    assert.strictEqual(sidebar.normalizeForComparison("Self-Attention"), "self attention");
    assert.strictEqual(sidebar.normalizeForComparison("Self Attention"), "self attention");
    assert.strictEqual(
      sidebar.normalizeForComparison("Self-Attention"),
      sidebar.normalizeForComparison("Self Attention")
    );
    assert.strictEqual(sidebar.normalizeForComparison("A leaky integrator of [Ca^{2+}] signals"), "a leaky integrator of ca2 signals");
    assert.strictEqual(sidebar.normalizeForComparison("A leaky integrator of Ca2+ signals"), "a leaky integrator of ca2 signals");
    assert.strictEqual(
      sidebar.normalizeForComparison("A leaky integrator of [Ca^{2+}] signals"),
      sidebar.normalizeForComparison("A leaky integrator of Ca2+ signals")
    );
    assert.strictEqual(sidebar.normalizeForComparison(""), "");
    assert.strictEqual(sidebar.normalizeForComparison(null), "");
  });

  // 8. parseHeadingParts
  test("parseHeadingParts: isolates numbering and core titles, preserving unnumbered words via case sensitivity", () => {
    // Numbered
    const numbered = sidebar.parseHeadingParts("3.2.2 Multi-Head Attention");
    assert.strictEqual(numbered.numbering, "3.2.2");
    assert.strictEqual(numbered.core, "multi head attention");

    // Roman
    const roman = sidebar.parseHeadingParts("III. Methodology");
    assert.strictEqual(roman.numbering, "iii");
    assert.strictEqual(roman.core, "methodology");

    // Appendix
    const appendix = sidebar.parseHeadingParts("Appendix B. Implementation");
    assert.strictEqual(appendix.numbering, "appendixb");
    assert.strictEqual(appendix.core, "implementation");

    // Unnumbered
    const unnumbered = sidebar.parseHeadingParts("Discussion");
    assert.strictEqual(unnumbered.numbering, null);
    assert.strictEqual(unnumbered.core, "discussion");

    // Pure numbering
    const pureNum = sidebar.parseHeadingParts("3.2.2");
    assert.strictEqual(pureNum.numbering, "3.2.2");
    assert.strictEqual(pureNum.core, "");

    const pureRoman = sidebar.parseHeadingParts("IV");
    assert.strictEqual(pureRoman.numbering, "iv");
    assert.strictEqual(pureRoman.core, "");

    // Case-sensitive guard: words like "Mix of Experts" or "A Survey" must NOT have their first word falsely stripped as numbering
    const moe = sidebar.parseHeadingParts("Mix of Experts");
    assert.strictEqual(moe.numbering, null);
    assert.strictEqual(moe.core, "mix of experts");

    const survey = sidebar.parseHeadingParts("A Survey");
    assert.strictEqual(survey.numbering, null);
    assert.strictEqual(survey.core, "a survey");

    const simple = sidebar.parseHeadingParts("A Simple Baseline for Speech");
    assert.strictEqual(simple.numbering, null);
    assert.strictEqual(simple.core, "a simple baseline for speech");

    // Explicit lettered numbering with punctuation is recognized
    const letteredWithDot = sidebar.parseHeadingParts("A. Survey of Methods");
    assert.strictEqual(letteredWithDot.numbering, "a");
    assert.strictEqual(letteredWithDot.core, "survey of methods");

    const subLettered = sidebar.parseHeadingParts("B.1 Proof of Theorem");
    assert.strictEqual(subLettered.numbering, "b.1");
    assert.strictEqual(subLettered.core, "proof of theorem");
  });

  // 9. findOutlineDestination
  test("findOutlineDestination: traverses PDF outline with scoring, prefix matching, conflict rejection, and parent disambiguation", () => {
    const mockOutline = [
      { title: "1. Introduction", dest: "dest-intro" },
      {
        title: "2. Related Work",
        dest: "dest-related",
        items: [
          { title: "2.1 Traditional Methods", dest: "dest-traditional" },
          { title: "2.2 Deep Learning", dest: "dest-dl" },
        ],
      },
      {
        title: "3. Methodology",
        dest: "dest-method",
        items: [
          {
            title: "3.1 Overview",
            dest: "dest-overview",
            items: [
              { title: "3.1.1 Problem Formulation", dest: "dest-formulation" },
            ],
          },
          { title: "3.2 Multi-Head Attention", dest: "dest-mha" },
          { title: "Data Processing", dest: "dest-method-dp" },
        ],
      },
      {
        title: "4. Experiments",
        dest: "dest-exp",
        items: [
          { title: "4.1 Setup", dest: "dest-setup" },
          { title: "4.2 Results", dest: "dest-results-4.2" },
          { title: "Data Processing", dest: "dest-exp-dp" },
        ],
      },
      { title: "Discussion and Future Work", dest: "dest-discussion" },
      { title: "Appendix A. Proofs", dest: "named.dest.appendix" },
    ];

    // Flat and nested outline exact match
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "1. Introduction"), { dest: "dest-intro" });
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "3.2 Multi-Head Attention"), { dest: "dest-mha" });
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "3.1.1 Problem Formulation"), { dest: "dest-formulation" });

    // Numbered prefix match
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "3.2"), { dest: "dest-mha" });
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "2.1"), { dest: "dest-traditional" });

    // Numbering conflict rejection (3.2 Results vs 4.2 Results rejected)
    assert.strictEqual(sidebar.findOutlineDestination(mockOutline, "3.2 Results"), null);

    // Parent chain disambiguation: duplicate "Data Processing" disambiguated by parent
    const methodDp = sidebar.findOutlineDestination(mockOutline, "Data Processing", ["3. Methodology"]);
    assert.deepStrictEqual(methodDp, { dest: "dest-method-dp" });

    const expDp = sidebar.findOutlineDestination(mockOutline, "Data Processing", ["4. Experiments"]);
    assert.deepStrictEqual(expDp, { dest: "dest-exp-dp" });

    // Tied scores across distinct destinations returns null (safe fail-open)
    const tiedDp = sidebar.findOutlineDestination(mockOutline, "Data Processing", []);
    assert.strictEqual(tiedDp, null);

    // Named destination string preserved intact
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "Appendix A. Proofs"), { dest: "named.dest.appendix" });

    // Token-boundary core containment (min 8 chars)
    assert.deepStrictEqual(sidebar.findOutlineDestination(mockOutline, "Discussion and Future"), { dest: "dest-discussion" });

    // Unmatched returns null
    assert.strictEqual(sidebar.findOutlineDestination(mockOutline, "Nonexistent Section"), null);
    assert.strictEqual(sidebar.findOutlineDestination([], "1. Introduction"), null);
    assert.strictEqual(sidebar.findOutlineDestination(null, "1. Introduction"), null);
  });

  // 9b. findOutlineDestination: synthesized outline schema
  test("findOutlineDestination: accepts Zotero synthesized outline schema with location and no dest", () => {
    const synthOutline = [
      {
        title: "1 Introduction",
        location: { position: { pageIndex: 0, rects: [[10, 20, 30, 40]] } },
      },
      {
        title: "2 Methodology",
        location: { position: { pageIndex: 2, rects: [[15, 25, 35, 45]] } },
        items: [
          {
            title: "2.1 Signal Integration",
            location: { position: { pageIndex: 3, rects: [[5, 10, 50, 60]] } },
          },
        ],
      },
      {
        title: "3 Direct Page Index",
        pageIndex: 7,
      },
    ];

    assert.deepStrictEqual(
      sidebar.findOutlineDestination(synthOutline, "1 Introduction"),
      { position: { pageIndex: 0, rects: [[10, 20, 30, 40]] } }
    );
    assert.deepStrictEqual(
      sidebar.findOutlineDestination(synthOutline, "2.1 Signal Integration"),
      { position: { pageIndex: 3, rects: [[5, 10, 50, 60]] } }
    );
    assert.deepStrictEqual(
      sidebar.findOutlineDestination(synthOutline, "3 Direct Page Index"),
      { pageIndex: 7 }
    );
  });

  // 9d. findOutlineDestination: location with dest schema
  test("findOutlineDestination: correctly handles location with dest object schema", () => {
    const outlineWithLocationDest = [
      {
        title: "4. Experimental Setup",
        location: { dest: "named.dest.exp" },
      },
    ];

    assert.deepStrictEqual(
      sidebar.findOutlineDestination(outlineWithLocationDest, "4. Experimental Setup"),
      { dest: "named.dest.exp" }
    );
  });

  // 9c. findOutlineDestination: math heading unwrap and normalization
  test("findOutlineDestination: matches unwrapped LaTeX math heading against outline node", () => {
    const outlineWithCa2 = [
      {
        title: "A leaky integrator of Ca2+ signals",
        location: { position: { pageIndex: 4, rects: [[20, 40, 60, 80]] } },
      },
    ];

    const { leaf } = sidebar.extractLeafHeading("A leaky integrator of $[Ca^{2+}]$ signals");
    assert.strictEqual(leaf, "A leaky integrator of [Ca^{2+}] signals");

    const match = sidebar.findOutlineDestination(outlineWithCa2, leaf);
    assert.deepStrictEqual(match, { position: { pageIndex: 4, rects: [[20, 40, 60, 80]] } });
  });

  // 10. findHtmlSnapshotHeading
  test("findHtmlSnapshotHeading: locates matching semantic heading in HTML snapshot DOM and rejects conflicts", () => {
    const doc = createMockElement("div");

    const h1 = createMockElement("h1");
    h1.textContent = "A Survey of Language Models";
    doc.appendChild(h1);

    const h2_1 = createMockElement("h2");
    h2_1.textContent = "1. Introduction";
    doc.appendChild(h2_1);

    const h2_2 = createMockElement("h2");
    h2_2.textContent = "2. Architecture Overview";
    doc.appendChild(h2_2);

    const h3_2_1 = createMockElement("h3");
    h3_2_1.textContent = "2.1 Attention Layers";
    doc.appendChild(h3_2_1);

    const h2_3 = createMockElement("h2");
    h2_3.textContent = "3.2 Results";
    doc.appendChild(h2_3);

    const roleHeading = createMockElement("div");
    roleHeading.setAttribute("role", "heading");
    roleHeading.textContent = "Discussion & Conclusions";
    doc.appendChild(roleHeading);

    // Matches <h2> in mock DOM
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, "2. Architecture Overview"), h2_2);
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, "2.1 Attention Layers"), h3_2_1);

    // Matches unnumbered and role=heading
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, "Discussion & Conclusions"), roleHeading);

    // Rejects conflicting numbering
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, "4.2 Results"), null);

    // Returns null if unmatched
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, "Nonexistent Heading"), null);
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(null, "1. Introduction"), null);
    assert.strictEqual(sidebar.findHtmlSnapshotHeading(doc, ""), null);
  });

  // 11. openItemInReader & click delegation
  await testAsync("openItemInReader: coalesces duplicate clicks, queries PDF outline, and jumps to section destination", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;
    let goToDestinationCalledWith = null;
    let readerOpenedWithID = null;

    try {
      const mockOutline = [
        { title: "1. Introduction", dest: "dest-intro" },
        { title: "2. Architecture", dest: "dest-arch" },
        { title: "3.2 Multi-Head Attention", dest: "dest-mha" },
      ];

      const mockInternalReader = {
        _state: { outline: mockOutline },
        _primaryView: {
          _iframeWindow: {
            PDFViewerApplication: {
              pdfLinkService: {
                goToDestination: async (dest) => {
                  goToDestinationCalledWith = dest;
                },
              },
            },
          },
        },
      };

      const mockItem = {
        id: 991,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = {
        open: async (id) => {
          readerOpenedWithID = id;
          return mockInternalReader;
        },
      };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = (libId, key) => (key === "ATT_TEST" ? mockItem : null);

      // Concurrent double-click coalescing
      const p1 = sidebar.openItemInReader("ATT_TEST", "§ 3.2 Multi-Head Attention");
      const p2 = sidebar.openItemInReader("ATT_TEST", "§ 3.2 Multi-Head Attention");
      assert.strictEqual(p1, p2, "Concurrent calls for same key and section must return the same coalesced promise");

      await p1;

      assert.strictEqual(readerOpenedWithID, 991);
      assert.strictEqual(goToDestinationCalledWith, "dest-mha");

      // Fail-open for synthetic section
      goToDestinationCalledWith = null;
      await sidebar.openItemInReader("ATT_TEST", "Frontmatter");
      assert.strictEqual(goToDestinationCalledWith, null, "Synthetic headings must not trigger outline jump");

      // Verify unresolvable key does not leave dead promise in coalescing map
      await sidebar.openItemInReader("NON_EXISTENT_KEY", "§ 1 Introduction");
      // Subsequent call for real key must proceed normally and navigate
      goToDestinationCalledWith = null;
      await sidebar.openItemInReader("ATT_TEST", "§ 3.2 Multi-Head Attention");
      assert.strictEqual(goToDestinationCalledWith, "dest-mha", "Subsequent navigation must succeed after early-return key");
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 12. openItemInReader: hierarchical parent breadcrumb fallback
  await testAsync("openItemInReader: falls back to parent breadcrumb hierarchy when leaf heading is absent in shallow outline", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;
    let navigatedWith = null;

    try {
      const shallowOutline = [
        {
          title: "Introduction",
          location: { position: { pageIndex: 0, rects: [[0, 0, 100, 100]] } },
        },
        {
          title: "Results",
          location: { position: { pageIndex: 5, rects: [[100, 200, 300, 400]] } },
        },
      ];

      const mockInternalReader = {
        _state: { outline: shallowOutline },
        navigate: async (targetLoc) => {
          navigatedWith = targetLoc;
        },
      };

      const mockItem = {
        id: 995,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = { open: async () => mockInternalReader };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = () => mockItem;

      // Query "Results > Leaky Integrator" against outline containing only "Results"
      await sidebar.openItemInReader("ATT_SHALLOW", "Results > Leaky Integrator");

      assert.deepStrictEqual(
        navigatedWith,
        { position: { pageIndex: 5, rects: [[100, 200, 300, 400]] } },
        "Should navigate to parent 'Results' location when child 'Leaky Integrator' is missing in outline"
      );
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 13. openItemInReader: outline polling checks pdfDoc.getOutline2() before getOutline()
  await testAsync("openItemInReader: queries pdfDoc.getOutline2() before getOutline() when internal state is empty", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;
    let navigatedWith = null;
    let getOutline2Called = false;
    let getOutlineCalled = false;

    try {
      const synthOutline = [
        {
          title: "Synthesized Section",
          location: { position: { pageIndex: 2, rects: [[1, 2, 3, 4]] } },
        },
      ];

      const mockInternalReader = {
        _state: { outline: null },
        _primaryView: {
          _iframeWindow: {
            PDFViewerApplication: {
              pdfDocument: {
                getOutline2: async () => {
                  getOutline2Called = true;
                  return synthOutline;
                },
                getOutline: async () => {
                  getOutlineCalled = true;
                  return [{ title: "Legacy Section", dest: "legacy-dest" }];
                },
              },
            },
          },
        },
        navigate: async (loc) => {
          navigatedWith = loc;
        },
      };

      const mockItem = {
        id: 996,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = { open: async () => mockInternalReader };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = () => mockItem;

      await sidebar.openItemInReader("ATT_SYNTH", "Synthesized Section");

      assert.strictEqual(getOutline2Called, true, "pdfDoc.getOutline2() must be checked first");
      assert.strictEqual(getOutlineCalled, false, "pdfDoc.getOutline() must not be called when getOutline2() succeeds");
      assert.deepStrictEqual(navigatedWith, { position: { pageIndex: 2, rects: [[1, 2, 3, 4]] } });
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 12b. openItemInReader: parent fallback skips synthetic labels and documentTitle
  await testAsync("openItemInReader: parent fallback skips synthetic labels and headings equal to documentTitle", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;
    let navigatedWith = null;

    try {
      const mockOutline = [
        {
          title: "Attention Is All You Need",
          location: { position: { pageIndex: 0, rects: [[0, 0, 100, 100]] } },
        },
        {
          title: "Frontmatter",
          location: { position: { pageIndex: 0, rects: [[0, 0, 100, 100]] } },
        },
        {
          title: "Abstract",
          location: { position: { pageIndex: 1, rects: [[0, 0, 100, 100]] } },
        },
        {
          title: "Figure 1: Architecture",
          location: { position: { pageIndex: 2, rects: [[0, 0, 100, 100]] } },
        },
        {
          title: "3 Model Architecture",
          location: { position: { pageIndex: 3, rects: [[10, 20, 30, 40]] } },
        },
      ];

      const mockInternalReader = {
        _state: { outline: mockOutline },
        navigate: async (targetLoc) => {
          navigatedWith = targetLoc;
        },
      };

      const mockItem = {
        id: 998,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
        getField: (field) => (field === "title" ? "Attention Is All You Need" : ""),
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = { open: async () => mockInternalReader };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = () => mockItem;

      // Case 1: Breadcrumb contains Title > Frontmatter > Abstract > Missing Section
      // All parents are synthetic or equal to docTitle, so none should be matched.
      await sidebar.openItemInReader("ATT_SYNTH_PARENTS", "Attention Is All You Need > Frontmatter > Abstract > Missing Subheading");
      assert.strictEqual(navigatedWith, null, "Parent fallback must skip Frontmatter, Abstract, and document title");

      // Case 2: Breadcrumb contains Title > 3 Model Architecture > Missing Subheading
      // Parent "3 Model Architecture" is valid and should be matched, skipping Title.
      navigatedWith = null;
      await sidebar.openItemInReader("ATT_SYNTH_PARENTS", "Attention Is All You Need > 3 Model Architecture > Missing Subheading");
      assert.deepStrictEqual(
        navigatedWith,
        { position: { pageIndex: 3, rects: [[10, 20, 30, 40]] } },
        "Parent fallback should navigate to '3 Model Architecture' while skipping document title"
      );
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 13b. openItemInReader: getOutline2 returning empty or null gracefully falls back to getOutline()
  await testAsync("openItemInReader: getOutline2 returning empty or null gracefully falls back to getOutline()", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;
    let navigatedWith = null;
    let getOutline2CallCount = 0;
    let getOutlineCallCount = 0;

    try {
      const legacyOutline = [
        {
          title: "Legacy Section",
          location: { position: { pageIndex: 1, rects: [[5, 6, 7, 8]] } },
        },
      ];

      const mockInternalReader = {
        _state: { outline: null },
        _primaryView: {
          _iframeWindow: {
            PDFViewerApplication: {
              pdfDocument: {
                getOutline2: async () => {
                  getOutline2CallCount++;
                  return null; // Empty or null returned by getOutline2
                },
                getOutline: async () => {
                  getOutlineCallCount++;
                  return legacyOutline;
                },
              },
            },
          },
        },
        navigate: async (loc) => {
          navigatedWith = loc;
        },
      };

      const mockItem = {
        id: 997,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = { open: async () => mockInternalReader };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = () => mockItem;

      await sidebar.openItemInReader("ATT_FALLBACK_OUTLINE", "Legacy Section");

      assert.strictEqual(getOutline2CallCount, 1, "getOutline2() should be attempted first");
      assert.strictEqual(getOutlineCallCount, 1, "getOutline() should be called when getOutline2() returns null/empty");
      assert.deepStrictEqual(navigatedWith, { position: { pageIndex: 1, rects: [[5, 6, 7, 8]] } });
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 14. openItemInReader: bounded timeout race prevents hanging host navigate from permanently blocking
  await testAsync("openItemInReader: bounded timeout race prevents hanging host navigate from permanently blocking", async () => {
    const origReader = env.window.Zotero?.Reader;
    const origGetByLib = env.window.Zotero?.Items?.getByLibraryAndKey;

    try {
      const mockOutline = [
        { title: "Hanging Section", dest: "dest-hang" },
      ];

      let navigateCalled = false;
      const mockInternalReader = {
        _state: { outline: mockOutline },
        navigate: () => {
          navigateCalled = true;
          return new Promise(() => {}); // never resolves
        },
      };

      const mockItem = {
        id: 999,
        isAttachment: () => true,
        attachmentReaderType: "pdf",
        attachmentContentType: "application/pdf",
      };

      if (!env.window.Zotero) env.window.Zotero = {};
      env.window.Zotero.Reader = { open: async () => mockInternalReader };
      if (!env.window.Zotero.Items) env.window.Zotero.Items = {};
      env.window.Zotero.Items.getByLibraryAndKey = () => mockItem;

      // First call: navigate hangs, but openItemInReader resolves due to timeout race
      await sidebar.openItemInReader("ATT_HANG", "Hanging Section");
      assert.strictEqual(navigateCalled, true);

      // Now verify that after timeout, coalescing map has cleared and subsequent call is not stuck
      let secondNavigateCalled = false;
      mockInternalReader.navigate = async () => {
        secondNavigateCalled = true;
      };

      await sidebar.openItemInReader("ATT_HANG", "Hanging Section");
      assert.strictEqual(secondNavigateCalled, true, "Subsequent call must proceed after timeout cleared hanging navigation");
    } finally {
      if (origReader !== undefined) {
        env.window.Zotero.Reader = origReader;
      } else {
        delete env.window.Zotero.Reader;
      }
      if (origGetByLib !== undefined) {
        env.window.Zotero.Items.getByLibraryAndKey = origGetByLib;
      } else if (env.window.Zotero?.Items) {
        delete env.window.Zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 12. formatMessageContent: enriched_text vs raw unpruned text with canonical data-citation-occ
  test("formatMessageContent: handles enriched_text with surviving occurrences and canonical data-citation-occ", () => {
    // Occurrence 1 was rejected by verification (support_probability: 0.20, survived: false)
    // Occurrence 2 survived (support_probability: 0.92, survived: true)
    const metadata = {
      citations: [
        {
          index: 1,
          title: "Twins Study",
          authors: "Jonsson et al.",
          year: 2021,
          section: "Methods",
          occurrences: [
            { occurrence: 1, survived: false, support_probability: 0.20 },
            { occurrence: 2, survived: true, support_probability: 0.92 },
          ],
        },
      ],
      references: [
        { title: "Twins Study", authors: "Jonsson et al.", year: 2021, tier: 1 },
      ],
    };

    // Case A: enriched_text (where occurrence 1 was already removed from prose)
    const enrichedText = "Only the second claim was verified [1].";
    const renderedEnriched = sidebar.formatMessageContent(enrichedText, metadata);

    assert.ok(renderedEnriched.includes('data-citation-idx="1"'));
    assert.ok(renderedEnriched.includes('data-citation-occ="2"'), "Should attach canonical occurrence 2 to pill");
    assert.ok(renderedEnriched.includes("Support probability: 0.92"), "Tooltip should show surviving occurrence probability");

    // Case B: showEvidencePopover with this pill resolves canonical occurrence 2
    sidebar.closeEvidencePopover();
    const pill = createMockElement("span");
    pill.setAttribute("data-citation-occ", "2");
    sidebar.showEvidencePopover(pill, metadata.citations[0]);
    const popover = env.document.body.querySelector(".evidence-popover");
    assert.ok(popover, "Evidence popover should render");
    const badge = popover.querySelector(".evidence-support-badge.high");
    assert.ok(badge, "High support badge should be rendered for canonical occurrence 2");
    assert.strictEqual(badge.textContent, "✓ Support Probability: 0.92");
    sidebar.closeEvidencePopover();
  });

  test("formatMessageContent: handles raw unpruned text fallback and canonical data-citation-occ", () => {
    const metadata = {
      citations: [
        {
          index: 1,
          title: "Twins Study",
          authors: "Jonsson et al.",
          year: 2021,
          section: "Methods",
          occurrences: [
            { occurrence: 1, survived: false, support_probability: 0.20 },
            { occurrence: 2, survived: true, support_probability: 0.92 },
          ],
        },
      ],
      references: [
        { title: "Twins Study", authors: "Jonsson et al.", year: 2021, tier: 1 },
      ],
    };

    // Raw unpruned text containing both occurrences in text
    const rawText = "First ungrounded claim [1]. Second verified claim [1].";
    const renderedRaw = sidebar.formatMessageContent(rawText, metadata);

    // First [1] must NOT have a citation pill
    assert.ok(!renderedRaw.includes("First ungrounded claim <span"), "First ungrounded claim must not have citation pill");
    // Second [1] must have a citation pill with canonical occ 2
    assert.ok(renderedRaw.includes('data-citation-idx="1"'));
    assert.ok(renderedRaw.includes('data-citation-occ="2"'), "Surviving occurrence must have canonical occ 2");
    assert.ok(renderedRaw.includes("Support probability: 0.92"));

    // Verify only ONE pill is rendered total
    const pillMatches = renderedRaw.match(/class="citation-pill"/g) || [];
    assert.strictEqual(pillMatches.length, 1, "Only the surviving occurrence should be rendered as a pill");
  });

  test("formatMessageContent: deduplicates repeated numbers within the same bracket token", () => {
    const metadata = {
      citations: [
        {
          index: 1,
          title: "Paper A",
          authors: "Author A",
          year: 2021,
          support_probability: 0.85,
        },
      ],
    };

    const text = "Duplicate citation in token [1, 1].";
    const rendered = sidebar.formatMessageContent(text, metadata);

    const pillMatches = rendered.match(/data-citation-idx="1"/g) || [];
    assert.strictEqual(pillMatches.length, 1, "Token [1, 1] must only render one citation pill for index 1");
  });

  test("formatMessageContent: strips lingering citation tokens when citations is empty and citation_verification is present", () => {
    const metadata = {
      citations: [],
      citation_verification: {
        model: "inception/mercury-decide:free",
        summary: { batch_size: 1, kept: 0, demoted: 1 },
      },
    };

    const text = "Claim that failed verification [1].";
    const rendered = sidebar.formatMessageContent(text, metadata);
    assert.ok(!rendered.includes("[1]"), "Bracket [1] should be stripped when citation verification demoted all citations");
    assert.ok(rendered.includes("Claim that failed verification."), "Prose should be preserved");
  });

  test("finalizeChatStream: updates rawText with streamEnrichedText and verifies copy / citation toggle parity", () => {
    const wrapper = createMockElement("div");
    wrapper.className = "message-wrapper assistant";
    const bubble = createMockElement("div");
    bubble.className = "message-bubble";
    wrapper.appendChild(bubble);

    const actionsDiv = createMockElement("div");
    actionsDiv.className = "message-actions";
    const actionsLeft = createMockElement("div");
    actionsLeft.className = "message-actions-left";
    actionsDiv.appendChild(actionsLeft);
    wrapper.appendChild(actionsDiv);

    env.document.getElementById("chat-messages").appendChild(wrapper);

    const rawStreamingText = "Raw streaming text with unverified citation [1].";
    const verifiedEnrichedText = "Clean verified text without unverified citation.\n\n# Cited Sources\n- **Jonsson (2021)**. *Twins Study*.";

    const streamCtx = {
      assistantBubble: bubble,
      currentResponseText: rawStreamingText,
      streamEnrichedText: verifiedEnrichedText,
      streamCitations: [],
      streamReferences: [
        { title: "Twins Study", authors: "Jonsson", year: 2021, tier: 1 },
      ],
      streamCitationVerification: { model: "inception/mercury-decide:free" },
      streamModel: "google/gemini-2.5-flash",
      streamUsage: { prompt_tokens: 100, candidates_tokens: 50, total_tokens: 150 },
      streamFinishReason: "stop",
      streamEventId: "evt-final-test",
      streamInvocationId: "inv-final-test",
      stepsBubble: null,
      liveSteps: [],
      payloadItems: [],
      requestStartTime: Date.now() - 1000,
      session: { messages: [] },
    };

    sidebar.finalizeChatStream(streamCtx, false);

    // 1. Bubble raw text and currentResponseText should match streamEnrichedText
    assert.strictEqual(streamCtx.currentResponseText, verifiedEnrichedText, "streamCtx.currentResponseText must be updated to streamEnrichedText");
    assert.strictEqual(bubble._rawText, verifiedEnrichedText, "bubble._rawText must store verifiedEnrichedText");
    assert.ok(bubble.innerHTML.includes("Clean verified text"), "Rendered innerHTML must reflect verified text");
    assert.ok(!bubble.innerHTML.includes("[1]"), "Lingering [1] should be stripped");

    // 2. Session messages must store verifiedEnrichedText
    assert.strictEqual(streamCtx.session.messages.length, 1);
    assert.strictEqual(streamCtx.session.messages[0].text, verifiedEnrichedText);

    // 3. stripCitationsFromText preserves clean prose for copy-to-clipboard
    const copiedCleanText = sidebar.stripCitationsFromText(bubble._rawText);
    assert.strictEqual(copiedCleanText, "Clean verified text without unverified citation.");

    // 4. Citation toggle functionality
    assert.ok(sidebar.hasMessageCitations(bubble._rawText, bubble._metadata));
    sidebar.ensureCitationToggleBtn(wrapper);
    const toggleBtn = wrapper.querySelector(".citation-toggle-btn");
    assert.ok(toggleBtn, "Citation toggle button should be present");

    // Toggle off: citations hidden
    toggleBtn.dispatchEvent("click");
    assert.ok(wrapper.classList.contains("citations-hidden"));
    assert.ok(toggleBtn.classList.contains("muted"));

    // Toggle on: citations restored
    toggleBtn.dispatchEvent("click");
    assert.strictEqual(wrapper.classList.contains("citations-hidden"), false);
    assert.strictEqual(toggleBtn.classList.contains("muted"), false);

    wrapper.remove();
  });

  // 27. buildClaimsInspectorData: passes session_id and handles sentence_attribution
  test("buildClaimsInspectorData: passes session_id and handles sentence_attribution", () => {
    const metaWithSid = {
      sessionId: "sess_custom_123",
      invocationId: "inv_turn_456",
      citation_verification: {
        model: "inception/mercury-decide:free",
        assessments: [
          {
            chunk_index: 1,
            occurrence: 1,
            claim: "Sentence level claim A",
            source_text: "Passage containing supporting evidence.",
            support_probability: 0.92,
            verdict: "KEPT",
            sentence_attribution: {
              status: "ok",
              supporting_indices: [1],
              spans: ["Passage containing supporting evidence."],
              unsupported_aspects: "",
              model: "nvidia/nemotron-3.5-lightning:free",
            },
          },
        ],
      },
    };

    const data = sidebar.buildClaimsInspectorData(metaWithSid);
    assert.strictEqual(data.sessionId, "sess_custom_123");
    assert.strictEqual(data.invocationId, "inv_turn_456");
    assert.strictEqual(data.assessments.length, 1);
    assert.ok(data.assessments[0].sentence_attribution, "sentence_attribution must be preserved");
    assert.strictEqual(data.assessments[0].sentence_attribution.support_level, undefined, "support_level must not be present");
    assert.deepStrictEqual(data.assessments[0].sentence_attribution.supporting_indices, [1]);

    // Test fallback from currentSessionId when meta.sessionId is omitted
    sidebar.setCurrentSessionId?.("sess_active_789");
    const metaNoSid = {
      invocation_id: "inv_999",
      citation_verification: {
        assessments: [
          {
            chunk_index: 1,
            occurrence: 1,
            claim: "Claim without sid",
            source_text: "Passage",
            support_probability: 0.88,
          },
        ],
      },
    };
    const dataActive = sidebar.buildClaimsInspectorData(metaNoSid);
    assert.strictEqual(dataActive.sessionId, "sess_active_789");
    assert.strictEqual(dataActive.invocationId, "inv_999");

    // Test nested invocation_id in citation_verification
    const metaVerInv = {
      citation_verification: {
        invocation_id: "inv_nested_ver_42",
        assessments: [],
      },
    };
    const dataVerInv = sidebar.buildClaimsInspectorData(metaVerInv);
    assert.strictEqual(dataVerInv.invocationId, "inv_nested_ver_42", "Must resolve invocation_id from citation_verification");

    // Test citation occurrence fallback preserves sentence_attribution
    const metaCitationOcc = {
      sessionId: "sess_occ_1",
      citations: [
        {
          index: 1,
          claim: "Citation claim",
          occurrences: [
            {
              occurrence: 1,
              support_probability: 0.85,
              sentence_attribution: {
                status: "ok",
                supporting_indices: [2],
                unsupported_aspects: "missing date",
              },
            },
          ],
        },
      ],
    };
    const dataOcc = sidebar.buildClaimsInspectorData(metaCitationOcc);
    assert.strictEqual(dataOcc.sessionId, "sess_occ_1");
    assert.strictEqual(dataOcc.assessments.length, 1);
    assert.strictEqual(dataOcc.assessments[0].sentence_attribution.support_level, undefined);
    assert.deepStrictEqual(dataOcc.assessments[0].sentence_attribution.supporting_indices, [2]);
    assert.strictEqual(dataOcc.assessments[0].sentence_attribution.unsupported_aspects, "missing date");
  });

  // 28. buildHighlightedPassage: prose span wrapping
  test("buildHighlightedPassage: wraps supporting prose spans in sentence-attr-highlight", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");
    const sourceText = "Deep learning architectures have evolved rapidly. Attention mechanisms enable effective sequence modeling. Residual connections mitigate gradient vanishing.";
    const spans = [
      "Deep learning architectures have evolved rapidly.",
      "Attention mechanisms enable effective sequence modeling.",
      "Residual connections mitigate gradient vanishing.",
    ];
    // Span index 2 (1-based: spanNum 2) is supporting
    const highlighted = claims.buildHighlightedPassage(sourceText, spans, [2]);

    assert.ok(
      highlighted.includes('<span class="sentence-attr-highlight">Attention mechanisms enable effective sequence modeling.</span>'),
      "Supporting span must be wrapped in sentence-attr-highlight"
    );
    assert.ok(
      highlighted.startsWith("Deep learning architectures have evolved rapidly. "),
      "Unsupporting span 1 must not be wrapped"
    );
    assert.ok(
      highlighted.endsWith(" Residual connections mitigate gradient vanishing."),
      "Unsupporting span 3 must not be wrapped"
    );
  });

  // 29. buildHighlightedPassage: table row highlighting marker
  test("buildHighlightedPassage: injects data-attr-highlight-tr marker into table row spans", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");
    const tableText = [
      "| Model | Accuracy | F1 |",
      "|---|---|---|",
      "| Baseline | 76.2% | 0.74 |",
      "| Proposed | 84.9% | 0.83 |",
    ].join("\n");

    const spans = [
      "| Baseline | 76.2% | 0.74 |",
      "| Proposed | 84.9% | 0.83 |",
    ];

    // Span 2 (Proposed) is supporting
    const highlighted = claims.buildHighlightedPassage(tableText, spans, [2]);

    assert.ok(
      highlighted.includes('| <span data-attr-highlight-tr="true"></span>Proposed | 84.9% | 0.83 |'),
      "Supporting table row must contain data-attr-highlight-tr marker"
    );
    assert.ok(
      !highlighted.includes('<span class="sentence-attr-highlight">| Proposed'),
      "Table row must NOT be wrapped in generic inline span"
    );
    assert.ok(
      highlighted.includes("| Baseline | 76.2% | 0.74 |"),
      "Non-supporting row must remain unchanged without marker"
    );
  });

  // 30. buildHighlightedPassage: empty, null, and missing spans fallback
  test("buildHighlightedPassage: handles empty, null, and missing spans without duplicating text or misaligning cursor", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");
    const sourceText = "Sentence one. Sentence two. Sentence three.";

    // A. Empty / null fallbacks
    assert.strictEqual(claims.buildHighlightedPassage("", ["Span"], [1]), "");
    assert.strictEqual(claims.buildHighlightedPassage(sourceText, [], [1]), sourceText);
    assert.strictEqual(claims.buildHighlightedPassage(sourceText, null, [1]), sourceText);
    assert.strictEqual(claims.buildHighlightedPassage(null, ["Span"], [1]), "");

    // B. Missing span (idx === -1) between valid spans
    const spansWithMissing = [
      "Sentence one.",
      "Sentence phantom that does not exist in source text.",
      "Sentence three.",
    ];
    // Supporting spans 1 and 3 (spanNum 1 and 3)
    const result = claims.buildHighlightedPassage(sourceText, spansWithMissing, [1, 3]);

    assert.ok(
      result.includes('<span class="sentence-attr-highlight">Sentence one.</span>'),
      "First span must be highlighted"
    );
    assert.ok(
      result.includes(" Sentence two. "),
      "Middle unmentioned sentence must be preserved verbatim once"
    );
    assert.ok(
      result.includes('<span class="sentence-attr-highlight">Sentence three.</span>'),
      "Third span must be highlighted despite missing intermediate span"
    );
    assert.ok(
      !result.includes("phantom"),
      "Phantom missing span must not be injected into output"
    );
    // Crucial: ensure Sentence two is not duplicated
    const countSentenceTwo = (result.match(/Sentence two\./g) || []).length;
    assert.strictEqual(countSentenceTwo, 1, "Sentence two must appear exactly once (no duplication)");
  });

  // 31. renderRichContent: isBlock produces table rows with sentence-attr-highlight
  test("renderRichContent: produces table rows with sentence-attr-highlight when isBlock is true", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");
    const tableText = [
      "| Model | Score |",
      "|---|---|",
      "| Baseline | 70% |",
      "| Proposed | 85% |",
    ].join("\n");

    const spans = [
      "| Baseline | 70% |",
      "| Proposed | 85% |",
    ];

    // Highlight row 2
    const highlightedMd = claims.buildHighlightedPassage(tableText, spans, [2]);
    assert.ok(highlightedMd.includes('data-attr-highlight-tr="true"'));

    // Render with isBlock = true
    const html = claims.renderRichContent(highlightedMd, true);
    assert.ok(html.includes("<table"), "Output must contain <table> element");
    assert.ok(html.includes("<tr"), "Output must contain <tr> elements");

    // Mount to DOM element and execute closest('tr') post-processing as done in updatePassageDisplay
    const passageBox = createMockElement("div");
    passageBox.innerHTML = html;

    passageBox.querySelectorAll("span[data-attr-highlight-tr]").forEach((marker) => {
      const trEl = marker.closest("tr");
      if (trEl) trEl.classList.add("sentence-attr-highlight");
      marker.remove();
    });

    const trs = passageBox.querySelectorAll("tr");
    assert.strictEqual(trs.length, 3, "Expected 1 header tr + 2 data trs");

    // tr 0: header, tr 1: Baseline (not highlighted), tr 2: Proposed (highlighted)
    assert.strictEqual(trs[0].classList.contains("sentence-attr-highlight"), false);
    assert.strictEqual(trs[1].classList.contains("sentence-attr-highlight"), false);
    assert.strictEqual(trs[2].classList.contains("sentence-attr-highlight"), true, "Target row must have sentence-attr-highlight class");

    // Marker span must have been removed
    const remainingMarkers = passageBox.querySelectorAll("span[data-attr-highlight-tr]");
    assert.strictEqual(remainingMarkers.length, 0, "All data-attr-highlight-tr markers must be cleaned up");
  });

  // 32. sentence_attribution: formatAttributionNoteHtml renders error, supporting, and unsupported notes
  test("sentence_attribution: formatAttributionNoteHtml renders distinct structures without support_level", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");

    function renderNote(item) {
      const noteWrap = createMockElement("div");
      noteWrap.className = "sentence-attr-note-wrap";
      const attr = item.sentence_attribution;
      if (!attr || item._showSentenceAttribution === false) {
        noteWrap.style.display = "none";
        return noteWrap;
      }
      noteWrap.style.display = "block";
      noteWrap.innerHTML = claims.formatAttributionNoteHtml(attr);
      return noteWrap;
    }

    // A: Error status
    const itemError = {
      claim: "Testing error",
      sentence_attribution: {
        status: "error",
        error: "OpenRouter rate limit reached",
        supporting_indices: [],
      },
      _showSentenceAttribution: true,
    };
    const wrapError = renderNote(itemError);
    const errEl = wrapError.querySelector(".sentence-attr-note.error");
    assert.ok(errEl, "Must render .sentence-attr-note.error");
    assert.ok(errEl.textContent.includes("⚠️ Attribution Error: OpenRouter rate limit reached"));
    assert.ok(!wrapError.textContent.includes("No direct supporting sentences"));

    // B: Supporting sentences > 0, fully substantiated
    const itemFull = {
      claim: "Accuracy is 95%",
      sentence_attribution: {
        status: "ok",
        supporting_indices: [1, 2],
        unsupported_aspects: "",
      },
      _showSentenceAttribution: true,
    };
    const wrapFull = renderNote(itemFull);
    const fullEl = wrapFull.querySelector(".sentence-attr-note.full");
    assert.ok(fullEl, "Must render .sentence-attr-note.full");
    assert.ok(fullEl.textContent.includes("🎯 2 supporting sentences highlighted above"));
    assert.strictEqual(wrapFull.querySelector(".sentence-attr-missing"), null);

    // C: Supporting sentences > 0, partial (unsupported aspects present)
    const itemPartial = {
      claim: "Concentration is 20 uM at 37C",
      sentence_attribution: {
        status: "ok",
        supporting_indices: [1],
        unsupported_aspects: "temperature 37C is not mentioned",
      },
      _showSentenceAttribution: true,
    };
    const wrapPartial = renderNote(itemPartial);
    const partEl = wrapPartial.querySelector(".sentence-attr-note.partial");
    assert.ok(partEl, "Must render .sentence-attr-note.partial");
    assert.ok(partEl.textContent.includes("🎯 1 supporting sentence highlighted above"));
    const missingEl = wrapPartial.querySelector(".sentence-attr-missing");
    assert.ok(missingEl, "Must render .sentence-attr-missing");
    assert.ok(missingEl.innerHTML.includes("<strong>Not found in this passage:</strong>"));
    assert.ok(missingEl.textContent.includes("temperature 37C is not mentioned"));

    // D: Supporting sentences === 0, missing explanation present
    const itemNone = {
      claim: "Model uses Pepke et al ODE solver",
      sentence_attribution: {
        status: "ok",
        supporting_indices: [],
        unsupported_aspects: "Passage discusses general kinetics but does not name Pepke et al.",
      },
      _showSentenceAttribution: true,
    };
    const wrapNone = renderNote(itemNone);
    const noneEl = wrapNone.querySelector(".sentence-attr-note.none");
    assert.ok(noneEl, "Must render .sentence-attr-note.none");
    assert.ok(noneEl.textContent.includes("❌ No direct supporting sentences found in this passage"));
    const noneMissingEl = wrapNone.querySelector(".sentence-attr-missing");
    assert.ok(noneMissingEl, "Must render .sentence-attr-missing");
    assert.ok(noneMissingEl.innerHTML.includes("<strong>Missing from passage:</strong>"));
    assert.ok(noneMissingEl.textContent.includes("Passage discusses general kinetics"));
  });

  // 33. claims.js: getSidebarWindow resolution and syncSentenceAttribution
  test("claims.js: getSidebarWindow and syncSentenceAttribution update DOM and sessions", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");

    const mockBubble = createMockElement("div");
    mockBubble.className = "message-bubble";
    mockBubble._metadata = {
      turn_cost_usd: 0.005,
      usage: { prompt_tokens: 500, candidates_tokens: 100, total_tokens: 600, cost_usd: 0.005 },
      citation_verification: {
        assessments: [
          {
            chunk_index: 1,
            occurrence: 1,
            claim: "Claim A",
            source_text: "Passage text",
            sentence_attribution: null,
          },
        ],
      },
    };

    const mockDoc = {
      querySelectorAll: (sel) => {
        if (sel === ".message-bubble") return [mockBubble];
        return [];
      },
    };

    const mockSession = {
      id: "sess_test",
      messages: [
        {
          role: "assistant",
          citation_verification: {
            assessments: [
              {
                chunk_index: 1,
                occurrence: 1,
                claim: "Claim A",
                source_text: "Passage text",
                sentence_attribution: null,
              },
            ],
          },
        },
      ],
    };

    let openedInReader = null;
    let selectedInLibrary = null;

    const mockSidebarWindow = {
      document: mockDoc,
      sessions: [mockSession],
      openItemInReader: (key, section) => { openedInReader = { key, section }; },
      selectItemInLibrary: (key) => { selectedInLibrary = key; },
      closed: false,
    };

    // Update window.Zotero mock for test
    const origZot = global.Zotero;
    const origWinZot = typeof window !== "undefined" ? window.Zotero : undefined;
    const mockZot = {
      Zocrates: {
        _sidebarWindow: mockSidebarWindow,
      },
    };
    global.Zotero = mockZot;
    if (typeof window !== "undefined") window.Zotero = mockZot;

    try {
      const resolvedWin = claims.getSidebarWindow();
      assert.strictEqual(resolvedWin, mockSidebarWindow, "Must resolve sidebar window from Zotero.Zocrates._sidebarWindow");

      // Test reader and library methods on resolved window
      resolvedWin.openItemInReader("ATT123", "Introduction");
      assert.deepStrictEqual(openedInReader, { key: "ATT123", section: "Introduction" });
      resolvedWin.selectItemInLibrary("ITEM456");
      assert.strictEqual(selectedInLibrary, "ITEM456");

      // Test syncSentenceAttribution updates bubble._metadata and session message
      const targetItem = {
        chunk_index: 1,
        occurrence: 1,
        claim: "Claim A",
        source_text: "Passage text",
      };
      const attrResult = {
        status: "ok",
        supporting_indices: [1],
        spans: ["Passage text"],
        unsupported_aspects: "",
        model: "nvidia/nemotron-3.5-lightning:free",
        latency_ms: 120,
        usage: { prompt_tokens: 100, completion_tokens: 25, total_tokens: 125 },
        cost_usd: 0.00015,
      };

      claims.syncSentenceAttribution(targetItem, attrResult);

      // Verify item updated
      assert.strictEqual(targetItem.sentence_attribution, attrResult);

      // Verify bubble._metadata updated
      const bubbleAssessment = mockBubble._metadata.citation_verification.assessments[0];
      assert.strictEqual(bubbleAssessment.sentence_attribution, attrResult, "bubble assessment must have sentence_attribution");
      assert.strictEqual(mockBubble._metadata.turn_cost_usd, 0.00515);
      assert.strictEqual(mockBubble._metadata.usage.prompt_tokens, 500);
      assert.strictEqual(mockBubble._metadata.usage.candidates_tokens, 100);
      assert.strictEqual(mockBubble._metadata.usage.total_tokens, 725);
      assert.strictEqual(mockBubble._metadata.usage.cost_usd, 0.00515);

      // Verify _syncedCost and _syncedTotalTokens tracked
      assert.strictEqual(bubbleAssessment._syncedCost, 0.00015);
      assert.strictEqual(bubbleAssessment._syncedTotalTokens, 125);

      // Idempotency: re-syncing with identical result does NOT double-increment
      claims.syncSentenceAttribution(targetItem, attrResult);
      assert.strictEqual(mockBubble._metadata.turn_cost_usd, 0.00515, "Re-sync must not double-count cost");
      assert.strictEqual(mockBubble._metadata.usage.total_tokens, 725, "Re-sync must not double-count tokens");

      // Verify mockSession message updated
      const sessAssessment = mockSession.messages[0].citation_verification.assessments[0];
      assert.strictEqual(sessAssessment.sentence_attribution, attrResult, "session message assessment must have sentence_attribution");
    } finally {
      global.Zotero = origZot;
      if (typeof window !== "undefined") window.Zotero = origWinZot;
    }
  });

  // 34. claims.js: updateAttributionHeaderStats calculation and DOM updates
  test("claims.js: updateAttributionHeaderStats calculation and DOM updates", () => {
    const claims = require("../../zotero-plugin/addon/claims.js");
    let badge = document.getElementById("badge-attr-cost");
    if (!badge) {
      badge = createMockElement("span");
      badge.id = "badge-attr-cost";
      document.body.appendChild(badge);
    }

    // 1. When count === 0 or assessments empty -> display none
    claims.updateClaimsData({
      assessments: [
        { claim: "Claim 1", sentence_attribution: null },
      ],
    });
    assert.strictEqual(badge.style.display, "none", "updateClaimsData must auto-refresh header stats");

    // 1b. When status !== 'ok' even if supporting_indices exists -> display none
    claims.updateClaimsData({
      assessments: [
        {
          claim: "Claim 1",
          sentence_attribution: {
            status: "error",
            supporting_indices: [1],
            cost_usd: 0.0001,
            usage: { total_tokens: 100 },
          },
        },
      ],
    });
    assert.strictEqual(badge.style.display, "none", "Non-ok status must not be counted even with supporting_indices");

    // 2. When count > 0 and free (cost_usd === 0)
    claims.updateClaimsData({
      assessments: [
        {
          claim: "Claim 1",
          sentence_attribution: {
            status: "ok",
            cost_usd: 0.0,
            usage: { prompt_tokens: 150, completion_tokens: 50, total_tokens: 200 },
          },
        },
      ],
    });
    claims.updateAttributionHeaderStats();
    assert.strictEqual(badge.style.display, "inline-flex");
    assert.ok(badge.textContent.includes("200 tokens"));
    assert.ok(badge.textContent.includes("(Free)"));
    assert.ok(badge.title.includes("Sentence Attribution: 1 evaluated"));
    assert.ok(badge.title.includes("Exact Cost: $0.000000"));

    // 3. When count > 0 and cost_usd > 0
    claims.updateClaimsData({
      assessments: [
        {
          claim: "Claim 1",
          sentence_attribution: {
            status: "ok",
            cost_usd: 0.00012,
            usage: { prompt_tokens: 200, completion_tokens: 40, total_tokens: 240 },
          },
        },
        {
          claim: "Claim 2",
          sentence_attribution: {
            status: "ok",
            cost_usd: 0.00018,
            usage: { prompt_tokens: 300, completion_tokens: 60, total_tokens: 360 },
          },
        },
      ],
    });
    claims.updateAttributionHeaderStats();
    assert.strictEqual(badge.style.display, "inline-flex");
    assert.ok(badge.textContent.includes("600 tokens"));
    assert.ok(badge.textContent.includes("$"));
    assert.ok(badge.title.includes("Sentence Attribution: 2 evaluated"));
    assert.ok(badge.title.includes("Tokens: 600"));
    assert.ok(badge.title.includes("Exact Cost: $0.000300"));
  });

  // 35. sidebar.js: openMessageInfoModal renders sentence attribution row
  test("sidebar.js: openMessageInfoModal renders sentence attribution token count and cost row", () => {
    // 1. With completed sentence attribution and cost > 0
    const metaWithCost = {
      role: "assistant",
      author: "Zocrates",
      text: "Synthesized text [1].",
      citation_verification: {
        provider: "openrouter",
        model: "inception/mercury-decide",
        threshold: 0.60,
        summary: { batch_size: 1, kept: 1, demoted: 0, fail_open: 0, mean_support: 0.85 },
        assessments: [
          {
            claim: "Claim A.",
            support_probability: 0.85,
            verdict: "KEPT",
            sentence_attribution: {
              status: "ok",
              cost_usd: 0.00015,
              usage: { prompt_tokens: 100, completion_tokens: 25, total_tokens: 125 },
            },
          },
        ],
      },
    };

    try {
      sidebar.openMessageInfoModal(metaWithCost);
      const overlay = document.querySelector(".info-modal-overlay");
      assert.ok(overlay, "Modal overlay must exist");
      const citRow = overlay.querySelector(".citation-support-row");
      assert.ok(citRow, "Citation Support row must exist");
      const saBox = citRow.querySelector(".citation-support-attribution");
      assert.ok(saBox, "Sentence attribution row must exist");
      assert.strictEqual(saBox.textContent, "Sentence Attribution: 1 evaluated (125 tokens • $0.00015)");
    } finally {
      const leftover = document.querySelector(".info-modal-overlay");
      if (leftover) leftover.remove();
    }

    // 2. With completed sentence attribution and cost === 0 (Free)
    const metaFree = {
      role: "assistant",
      author: "Zocrates",
      text: "Synthesized text [1].",
      citation_verification: {
        provider: "openrouter",
        model: "inception/mercury-decide",
        threshold: 0.60,
        summary: { batch_size: 1, kept: 1, demoted: 0, fail_open: 0, mean_support: 0.85 },
        assessments: [
          {
            claim: "Claim A.",
            support_probability: 0.85,
            verdict: "KEPT",
            sentence_attribution: {
              status: "ok",
              cost_usd: 0.0,
              usage: { prompt_tokens: 80, completion_tokens: 20, total_tokens: 100 },
            },
          },
        ],
      },
    };

    try {
      sidebar.openMessageInfoModal(metaFree);
      const overlay = document.querySelector(".info-modal-overlay");
      const citRow = overlay.querySelector(".citation-support-row");
      const saBox = citRow.querySelector(".citation-support-attribution");
      assert.ok(saBox, "Sentence attribution row must exist for free attribution");
      assert.strictEqual(saBox.textContent, "Sentence Attribution: 1 evaluated (100 tokens • Free)");
    } finally {
      const leftover = document.querySelector(".info-modal-overlay");
      if (leftover) leftover.remove();
    }

    // 3. Without sentence attribution -> row should NOT exist
    const metaNoSa = {
      role: "assistant",
      author: "Zocrates",
      text: "Synthesized text [1].",
      citation_verification: {
        provider: "openrouter",
        model: "inception/mercury-decide",
        threshold: 0.60,
        summary: { batch_size: 1, kept: 1, demoted: 0, fail_open: 0, mean_support: 0.85 },
        assessments: [
          {
            claim: "Claim A.",
            support_probability: 0.85,
            verdict: "KEPT",
          },
        ],
      },
    };

    try {
      sidebar.openMessageInfoModal(metaNoSa);
      const overlay = document.querySelector(".info-modal-overlay");
      const citRow = overlay.querySelector(".citation-support-row");
      const saBox = citRow.querySelector(".citation-support-attribution");
      assert.strictEqual(saBox, null, "Sentence attribution row must not exist when no assessments have sentence attribution");
    } finally {
      const leftover = document.querySelector(".info-modal-overlay");
      if (leftover) leftover.remove();
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

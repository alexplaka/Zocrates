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
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

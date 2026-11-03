/**
 * test_sidebar_discovery.js — Tests for online discovery components in sidebar.js.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar Online Discovery ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. Target Collection Dropdown Tree Builder
  test("populateCandidateCollectionSelect: populates default root option <option value=\"\">📁 My Library (Root)</option>", () => {
    const selectEl = createMockElement("select");
    sidebar.populateCandidateCollectionSelect(selectEl, 1);

    assert.ok(selectEl.options.length >= 1, "Should have at least root option");
    assert.strictEqual(selectEl.options[0].value, "");
    assert.strictEqual(selectEl.options[0].textContent, "📁 My Library (Root)");
  });

  test("populateCandidateCollectionSelect: builds indented Unicode tree hierarchy for nested collections", () => {
    const mockAttention = { id: 30, name: "Attention", parentID: 20, getChildCollections: () => [] };
    const mockTransformers = { id: 20, name: "Transformers", parentID: 10, getChildCollections: () => [mockAttention] };
    const mockVision = { id: 40, name: "Vision", parentID: 10, getChildCollections: () => [] };
    const mockML = { id: 10, name: "ML", parentID: null, getChildCollections: () => [mockTransformers, mockVision] };

    const mockCols = [mockML, mockTransformers, mockAttention, mockVision];
    env.zotero.Collections.getByLibrary = () => [mockML];
    env.zotero.Collections.get = (id) => mockCols.find((c) => c.id === id) || null;

    const selectEl = createMockElement("select");
    sidebar.populateCandidateCollectionSelect(selectEl, 1);

    assert.strictEqual(selectEl.options[0].value, "");
    assert.strictEqual(selectEl.options[0].textContent, "📁 My Library (Root)");

    const optTexts = selectEl.options.map((o) => o.textContent);
    assert.ok(optTexts.some((t) => t.includes("ML")), "Should include root collection ML");
    assert.ok(optTexts.some((t) => t.includes("├─ Transformers") || t.includes("└─ Transformers")), "Should include branched Transformers child");
    assert.ok(optTexts.some((t) => t.includes("Attention")), "Should include grandchild Attention");
  });

  test("populateCandidateCollectionSelect: pre-selects active collection from library pane if available", () => {
    const mockCol = { id: 101, name: "Deep Learning", parentID: null, getChildCollections: () => [] };
    env.zotero.Collections.getByLibrary = () => [mockCol];
    env.zotero.Collections.get = () => mockCol;
    const mockPane = {
      getSelectedCollections: () => [{ id: 101, name: "Deep Learning" }],
    };
    env.zotero.getMainWindow = () => ({ ZoteroPane: mockPane });
    env.zotero.getActiveZoteroPane = () => mockPane;
    global.window.top = { ZoteroPane: mockPane };

    const selectEl = createMockElement("select");
    sidebar.populateCandidateCollectionSelect(selectEl, 1);

    assert.strictEqual(selectEl.value, "101", "Active collection ID 101 should be pre-selected");
  });

  // 2. Candidate Cards Group DOM Construction & Selection Tracking
  test("renderCandidatePapersGroup: constructs group with header, select, select all, and import button", () => {
    sidebar.selectedCandidateKeys.clear();
    sidebar.importedCandidateKeys.clear();

    const wrapper = createMockElement("div");
    const testCandidates = [
      {
        candidate_id: "c1",
        title: "Speculative Decoding via Cascades",
        year: 2024,
        authors: ["Alice Chen", "Bob Smith"],
        arxiv_id: "2401.12345",
        citation_count: 42,
        is_oa: true,
        oa_pdf_url: "https://arxiv.org/pdf/2401.12345.pdf",
        tldr: "Introduces early-exit draft cascades.",
      },
      {
        candidate_id: "c2",
        title: "Fast Attention Benchmarks",
        year: 2023,
        authors: ["Dave Lee", "Eve Miller", "Frank Wong"],
        venue: "IEEE Trans",
        doi: "10.1109/test",
        citation_count: 118,
        is_oa: false,
        oa_pdf_url: null,
        abstract: "Comprehensive benchmark of memory bandwidth bottlenecks.",
      },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates, "inv_phase5_test");
    assert.ok(group, "Group element should be created");
    assert.strictEqual(group.dataset.invocationId, "inv_phase5_test");

    const headerTitle = group.querySelector(".candidate-header-title");
    assert.ok(headerTitle.textContent.includes("2 novel papers found"));

    const collectionSelect = group.querySelector(".candidate-collection-select");
    assert.ok(collectionSelect, "Target collection select should be rendered");

    const selectAllCheckbox = group.querySelector(".candidate-select-all-checkbox");
    assert.ok(selectAllCheckbox, "Select All checkbox should be rendered");
    assert.strictEqual(selectAllCheckbox.checked, false);

    const importBtn = group.querySelector(".candidate-import-btn");
    assert.ok(importBtn, "Import button should be rendered");
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (0)");
    assert.strictEqual(importBtn.disabled, true);

    const cards = group.querySelectorAll(".candidate-paper-card");
    assert.strictEqual(cards.length, 2);

    // Card 1 assertions
    const card1 = cards[0];
    assert.strictEqual(card1.dataset.candidateKey, "c1");
    assert.strictEqual(card1.querySelector(".candidate-card-index").textContent, "1.");
    assert.strictEqual(card1.querySelector(".candidate-card-title").textContent, "Speculative Decoding via Cascades");
    assert.ok(!card1.querySelector(".candidate-card-title").textContent.includes("(2024)"));
    assert.strictEqual(card1.querySelector(".candidate-card-meta").textContent, "Chen & Smith · arXiv:2401.12345 · 2024 · (42 citations)");
    assert.ok(card1.querySelector(".candidate-badge.oa-available"));
    assert.strictEqual(card1.querySelector(".candidate-badge.oa-available").textContent, "PDF Available");

    // Card 2 assertions
    const card2 = cards[1];
    assert.strictEqual(card2.dataset.candidateKey, "c2");
    assert.strictEqual(card2.querySelector(".candidate-card-title").textContent, "Fast Attention Benchmarks");
    assert.strictEqual(card2.querySelector(".candidate-card-meta").textContent, "Lee et al. · IEEE Trans · 2023 · (118 citations)");
    assert.ok(card2.querySelector(".candidate-badge.oa-none"));
    assert.strictEqual(card2.querySelector(".candidate-badge.oa-none").textContent, "Metadata Only");
  });

  test("selection tracking: updates selectedCandidateKeys, dynamic count, and enables button", () => {
    sidebar.selectedCandidateKeys.clear();
    sidebar.importedCandidateKeys.clear();

    const wrapper = createMockElement("div");
    const testCandidates = [
      { candidate_id: "cand_a", title: "Paper Alpha", year: 2024 },
      { candidate_id: "cand_b", title: "Paper Beta", year: 2023 },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates, "inv_select_test");
    const cb1 = group.querySelectorAll(".candidate-select-checkbox")[0];
    const importBtn = group.querySelector(".candidate-import-btn");

    assert.strictEqual(importBtn.disabled, true);
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (0)");

    // Check candidate Alpha
    cb1.checked = true;
    cb1.dispatchEvent("change");

    assert.strictEqual(sidebar.selectedCandidateKeys.has("cand_a"), true);
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (1)");
    assert.strictEqual(importBtn.disabled, false);

    // Uncheck candidate Alpha
    cb1.checked = false;
    cb1.dispatchEvent("change");

    assert.strictEqual(sidebar.selectedCandidateKeys.has("cand_a"), false);
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (0)");
    assert.strictEqual(importBtn.disabled, true);
  });

  // 3. Select All / Deselect All
  test("Select All: selects all non-disabled cards and updates count to total novel papers", () => {
    sidebar.selectedCandidateKeys.clear();
    sidebar.importedCandidateKeys.clear();

    const wrapper = createMockElement("div");
    const testCandidates = [
      { candidate_id: "p1", title: "Paper 1", year: 2024 },
      { candidate_id: "p2", title: "Paper 2", year: 2024 },
      { candidate_id: "p3", title: "Paper 3", year: 2024 },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);
    const selectAllCheckbox = group.querySelector(".candidate-select-all-checkbox");
    const importBtn = group.querySelector(".candidate-import-btn");

    selectAllCheckbox.checked = true;
    selectAllCheckbox.dispatchEvent("change");

    assert.strictEqual(sidebar.selectedCandidateKeys.size, 3);
    assert.strictEqual(sidebar.selectedCandidateKeys.has("p1"), true);
    assert.strictEqual(sidebar.selectedCandidateKeys.has("p2"), true);
    assert.strictEqual(sidebar.selectedCandidateKeys.has("p3"), true);
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (3)");
    assert.strictEqual(importBtn.disabled, false);

    selectAllCheckbox.checked = false;
    selectAllCheckbox.dispatchEvent("change");

    assert.strictEqual(sidebar.selectedCandidateKeys.size, 0);
    assert.strictEqual(importBtn.textContent, "⬇ Import Selected (0)");
    assert.strictEqual(importBtn.disabled, true);
  });

  test("Select All: partial card selection marks Select All as indeterminate", () => {
    sidebar.selectedCandidateKeys.clear();
    sidebar.importedCandidateKeys.clear();

    const wrapper = createMockElement("div");
    const testCandidates = [
      { candidate_id: "p1", title: "Paper 1", year: 2024 },
      { candidate_id: "p2", title: "Paper 2", year: 2024 },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);
    const selectAllCheckbox = group.querySelector(".candidate-select-all-checkbox");
    const cb1 = group.querySelectorAll(".candidate-select-checkbox")[0];

    cb1.checked = true;
    cb1.dispatchEvent("change");

    assert.strictEqual(selectAllCheckbox.checked, false);
    assert.strictEqual(selectAllCheckbox.indeterminate, true, "Select All should be indeterminate when 1 of 2 is checked");
  });

  // 4. Post-Import In-Place Badge Update
  test("post-import badges: marks imported cards with .imported class, disabled checkbox, and [✓ Imported] badge", () => {
    sidebar.selectedCandidateKeys.clear();
    sidebar.importedCandidateKeys.clear();
    sidebar.importedCandidateKeys.add("already_imp_1");

    const wrapper = createMockElement("div");
    const testCandidates = [
      { candidate_id: "already_imp_1", title: "Imported Paper", year: 2023, is_oa: true },
      { candidate_id: "novel_1", title: "Novel Paper", year: 2024, is_oa: false },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);
    const cards = group.querySelectorAll(".candidate-paper-card");

    const impCard = cards[0];
    assert.ok(impCard.classList.contains("imported"));
    const impCb = impCard.querySelector(".candidate-select-checkbox");
    assert.strictEqual(impCb.disabled, true);
    assert.strictEqual(impCb.checked, false);

    const impBadge = impCard.querySelector(".candidate-badge.imported");
    assert.ok(impBadge);
    assert.strictEqual(impBadge.textContent, "✓ Imported");

    const novelCard = cards[1];
    assert.strictEqual(novelCard.classList.contains("imported"), false);
    assert.strictEqual(novelCard.querySelector(".candidate-select-checkbox").disabled, false);
  });

  test("duplicate rendering guard prevents multiple .candidate-papers-group in same wrapper", () => {
    const wrapper = createMockElement("div");
    const testCandidates = [{ candidate_id: "p1", title: "Paper 1", year: 2024 }];

    const group1 = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);
    const group2 = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);

    assert.strictEqual(group1, group2, "Should return existing group instance");
    assert.strictEqual(wrapper.querySelectorAll(".candidate-papers-group").length, 1);
  });

  test("appendWelcomeMessage formats discovery exploration prompt", () => {
    const chatMsgs = env.document.getElementById("chat-messages");
    const prevCount = chatMsgs.children.length;

    sidebar.appendWelcomeMessage();

    assert.ok(chatMsgs.children.length > prevCount, "Should append message to chat-messages");
    const lastWrapper = chatMsgs.children[chatMsgs.children.length - 1];
    const msgEl = lastWrapper.querySelector(".message");
    assert.ok(msgEl._rawText.includes("discover new literature and preprints online to import into Zotero"));
  });

  // 5. Polite Etiquette Auto-Detection
  test("resolvePoliteEmail: resolves configured or synced email", () => {
    env.zotero.Prefs.set("extensions.zocrates.scholarPoliteEmail", "");
    env.zotero.Prefs.set("sync.server.username", "");
    assert.strictEqual(sidebar.resolvePoliteEmail(), "");

    env.zotero.Prefs.set("extensions.zocrates.scholarPoliteEmail", "researcher@example.edu");
    assert.strictEqual(sidebar.resolvePoliteEmail(), "researcher@example.edu");

    env.zotero.Prefs.set("extensions.zocrates.scholarPoliteEmail", "");
    env.zotero.Prefs.set("sync.server.username", "user@university.edu");
    assert.strictEqual(sidebar.resolvePoliteEmail(), "user@university.edu");

    // Invalid email in sync username
    env.zotero.Prefs.set("sync.server.username", "plain_username");
    assert.strictEqual(sidebar.resolvePoliteEmail(), "");
  });

  // 6. Metadata formatting & Abstract drawers
  test("formatCandidateMetaLine formats author · venue/arXiv · YEAR · (N citations)", () => {
    const c1 = {
      authors: ["Alice Chen", "Bob Smith"],
      arxiv_id: "2401.12345",
      year: 2024,
      citation_count: 42,
    };
    assert.strictEqual(sidebar.formatCandidateMetaLine(c1), "Chen & Smith · arXiv:2401.12345 · 2024 · (42 citations)");

    const c2 = {
      authors: ["Alice Chen"],
      venue: "Nature",
      year: 2023,
      citation_count: 1,
    };
    assert.strictEqual(sidebar.formatCandidateMetaLine(c2), "Chen · Nature · 2023 · (1 citation)");
  });

  test("expandable abstract drawer handles abstract, abstractNote, and empty fallback", () => {
    const wrapper = createMockElement("div");
    const testCandidates = [
      { candidate_id: "a1", title: "Paper with abstract", abstract: "This is the primary abstract." },
      { candidate_id: "a2", title: "Paper with abstractNote", abstractNote: "This is an abstractNote." },
      { candidate_id: "a3", title: "Paper with empty abstract", abstract: "   ", abstractNote: "" },
    ];

    const group = sidebar.renderCandidatePapersGroup(wrapper, testCandidates);
    const cards = group.querySelectorAll(".candidate-paper-card");

    const d1 = cards[0].querySelector(".candidate-abstract-drawer");
    assert.strictEqual(d1.querySelector(".candidate-abstract-text").textContent, "This is the primary abstract.");

    const d2 = cards[1].querySelector(".candidate-abstract-drawer");
    assert.strictEqual(d2.querySelector(".candidate-abstract-text").textContent, "This is an abstractNote.");

    const d3 = cards[2].querySelector(".candidate-abstract-drawer");
    assert.strictEqual(d3.querySelector(".candidate-abstract-empty").textContent, "No abstract available for this publication.");
  });

  // 7. Library sync
  await testAsync("syncCandidatesWithLibrary detects existing items and marks cards on session reload", async () => {
    const mockZot = {
      Libraries: { userLibraryID: 1 },
      Utilities: { cleanDOI: (d) => d },
      Search: function () {
        this.libraryID = 1;
        this.conditions = [];
        this.addCondition = (c, op, v) => {
          this.conditions.push({ c, op, v });
        };
        this.search = async () => {
          const cond = this.conditions[0];
          if (cond && cond.c === "DOI" && cond.v === "10.1000/182") return [101];
          if (cond && cond.c === "DOI" && cond.v === "10.1000/183") return [102];
          return [];
        };
      },
      Items: {
        getAsync: async (id) => {
          if (id === 101) return { getAttachments: () => [201] };
          if (id === 201) return { isPDFAttachment: () => true, attachmentContentType: "application/pdf" };
          if (id === 102) return { getAttachments: () => [] };
          return null;
        },
      },
    };

    const savedZot = global.Zotero;
    global.Zotero = mockZot;
    global.window.Zotero = mockZot;

    try {
      const cands = [
        { candidate_id: "c1", title: "Paper With Attached PDF", doi: "10.1000/182", year: 2024, oa_pdf_url: "https://example.org/p1.pdf" },
        { candidate_id: "c2", title: "Paper Without PDF", doi: "10.1000/183", year: 2023, oa_pdf_url: "https://example.org/p2" },
        { candidate_id: "c3", title: "Un-imported Novel Paper", doi: "10.1000/999", year: 2022 },
      ];

      const wrapper = createMockElement("div");
      const group = sidebar.renderCandidatePapersGroup(wrapper, cands, "inv_test_reload");
      const cards = group.querySelectorAll(".candidate-paper-card");

      await sidebar.syncCandidatesWithLibrary(group, cands);

      assert.strictEqual(cards[0].classList.contains("imported"), true);
      assert.strictEqual(cards[0].querySelector(".candidate-badge.imported").textContent, "✓ Imported");

      assert.strictEqual(cards[1].classList.contains("imported"), true);
      assert.strictEqual(cards[1].querySelector(".candidate-badge.imported-no-pdf").textContent, "✓ Imported (No PDF)");

      assert.strictEqual(cards[2].classList.contains("imported"), false);
    } finally {
      global.Zotero = savedZot;
      global.window.Zotero = savedZot;
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

/**
 * test_sidebar_context_chips.js — Tests for context chips and library/collection resolution in sidebar.js.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar Context Chips ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. mapItemToChipData
  test("mapItemToChipData: extracts metadata for regular items, multi-author, preprints, and missing years", () => {
    // Regular item with full metadata
    const regularItem = {
      key: "REG_1",
      getField: (f) => (f === "title" ? "Attention Mechanisms" : f === "date" ? "2023-05-12" : null),
      getCreatorsJSON: () => [{ lastName: "Vaswani" }, { lastName: "Shazeer" }],
      isNote: () => false,
      isAttachment: () => false,
    };
    const chip1 = sidebar.mapItemToChipData(regularItem);
    assert.strictEqual(chip1.key, "REG_1");
    assert.strictEqual(chip1.title, "Attention Mechanisms");
    assert.strictEqual(chip1.authorStr, "Vaswani, Shazeer");
    assert.strictEqual(chip1.year, "2023");
    assert.strictEqual(chip1.type, "item");

    // Item with missing year
    const noYearItem = {
      key: "NO_YR",
      getField: (f) => (f === "title" ? "Undated Document" : null),
      getCreatorsJSON: () => [{ lastName: "Anonymous" }],
      isNote: () => false,
    };
    const chip2 = sidebar.mapItemToChipData(noYearItem);
    assert.strictEqual(chip2.year, "");
    assert.strictEqual(chip2.authorStr, "Anonymous");

    // Standalone note item
    const noteItem = {
      key: "NOTE_1",
      isNote: () => true,
      getNote: () => "<h1>Research Notes on LoRA</h1>\n<p>Detailed notes...</p>",
    };
    const chip3 = sidebar.mapItemToChipData(noteItem);
    assert.strictEqual(chip3.type, "note");
    assert.strictEqual(chip3.title, "Research Notes on LoRA");
  });

  // 2. renderActiveChips & Chip removal
  test("renderActiveChips: renders DOM chips and handles chip removal button click", () => {
    const chipsContainer = env.document.getElementById("chips-container");
    assert.ok(chipsContainer, "chips-container must be present");

    // Sync active items list
    global.window.syncFromDetachedState({
      activeItemsList: [
        { key: "PAPER_A", title: "Paper Alpha", type: "item" },
        { key: "COL_B", title: "ML Collection", type: "collection" },
      ],
    });

    const renderedChips = chipsContainer.querySelectorAll(".paper-chip");
    assert.strictEqual(renderedChips.length, 2);

    const chipA = renderedChips[0];
    assert.strictEqual(chipA.querySelector(".chip-icon").textContent, "📄");
    assert.strictEqual(chipA.querySelector(".chip-text").textContent, "Paper Alpha");

    const chipB = renderedChips[1];
    assert.strictEqual(chipB.querySelector(".chip-icon").textContent, "📁");
    assert.strictEqual(chipB.querySelector(".chip-text").textContent, "ML Collection");

    // Remove chip A via close button
    const removeBtnA = chipA.querySelector(".chip-remove");
    assert.ok(removeBtnA);
    removeBtnA.dispatchEvent("click");

    // After removing chip A, only chip B should remain in DOM
    sidebar.renderActiveChips();
    const remainingChips = chipsContainer.querySelectorAll(".paper-chip");
    assert.strictEqual(remainingChips.length, 1);
    assert.strictEqual(remainingChips[0].querySelector(".chip-text").textContent, "ML Collection");
  });

  // 3. resolveSelectedCollections
  test("resolveSelectedCollections: handles getSelectedCollections and getSelectedCollection API variants", () => {
    // Array API
    const mockPaneArray = {
      getSelectedCollections: () => [{ id: 1, name: "Col 1" }, { id: 2, name: "Col 2" }],
    };
    const cols1 = sidebar.resolveSelectedCollections(mockPaneArray);
    assert.strictEqual(cols1.length, 2);
    assert.strictEqual(cols1[0].name, "Col 1");

    // Single item fallback
    const mockPaneSingle = {
      getSelectedCollection: () => ({ id: 3, name: "Col Single" }),
    };
    const cols2 = sidebar.resolveSelectedCollections(mockPaneSingle);
    assert.strictEqual(cols2.length, 1);
    assert.strictEqual(cols2[0].name, "Col Single");

    // Null or empty pane
    assert.deepStrictEqual(sidebar.resolveSelectedCollections(null), []);
  });

  // 4. resolveActiveLibraryInfo
  test("resolveActiveLibraryInfo: resolves user library and group libraries", () => {
    // User library (ID 1)
    const mockPaneUser = {
      getSelectedLibraryID: () => 1,
    };
    const infoUser = sidebar.resolveActiveLibraryInfo(mockPaneUser);
    assert.strictEqual(infoUser.libraryID, 1);
    assert.strictEqual(infoUser.name, "My Library");

    // Group library (ID 5)
    env.zotero.Libraries.get = (id) => ({ id, name: "Robotics Lab" });
    env.zotero.Libraries.getName = (id) => "Robotics Lab";
    const mockPaneGroup = {
      getSelectedLibraryID: () => 5,
    };
    const infoGroup = sidebar.resolveActiveLibraryInfo(mockPaneGroup);
    assert.strictEqual(infoGroup.libraryID, 5);
    assert.strictEqual(infoGroup.name, "Robotics Lab");
  });

  // 5. handleIncomingSnippet switching contextMode to "papers" and adding snippet without parent paper chip
  test("handleIncomingSnippet: switches contextMode to 'papers' and adds snippet without polluting activeItemsList", () => {
    // Start with a collection chip in collection mode
    sidebar.setActiveItemsList([{ key: "COL_OLD", type: "collection", title: "Old Collection" }]);
    sidebar.setActiveSnippetsList([]);
    sidebar.setContextMode("collection");
    assert.strictEqual(sidebar.getContextMode(), "collection");

    const originalGetByLibraryAndKey = env.zotero.Items?.getByLibraryAndKey;
    env.zotero.Items = env.zotero.Items || {};
    env.zotero.Items.getByLibraryAndKey = (libId, key) => {
      if (key === "PARENT_PAPER_1") {
        return {
          key: "PARENT_PAPER_1",
          getField: (f) => (f === "title" ? "Parent Research Paper" : f === "date" ? "2024-01-01" : null),
          getCreatorsJSON: () => [{ lastName: "Smith" }],
          isNote: () => false,
          isAttachment: () => false,
        };
      }
      return null;
    };

    try {
      const snippet = {
        id: "snip_test_1",
        type: "text",
        parentKey: "PARENT_PAPER_1",
        paperTitle: "Parent Research Paper",
        authorStr: "Smith",
        text: "Important quotation from the paper.",
      };
      sidebar.handleIncomingSnippet(snippet);

      // Context mode switched to papers
      assert.strictEqual(sidebar.getContextMode(), "papers");

      // Collection chip was cleared and parent paper was NOT added to activeItemsList (no clutter)
      const items = sidebar.getActiveItemsList();
      assert.strictEqual(items.length, 0, "activeItemsList should not have parent paper chip auto-added");

      // Snippet added to activeSnippetsList
      const snippets = sidebar.getActiveSnippetsList();
      assert.strictEqual(snippets.length, 1);
      assert.strictEqual(snippets[0].id, "snip_test_1");

      // Add a second snippet from another paper
      sidebar.handleIncomingSnippet({
        id: "snip_test_2",
        type: "text",
        parent_key: "UNKNOWN_PAPER",
        paper_title: "Fallback Title",
        text: "Another excerpt from unknown paper.",
      });
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 2);
      assert.strictEqual(sidebar.getActiveItemsList().length, 0);

      // Test library mode transition clears library chip
      sidebar.setContextMode("library");
      sidebar.setActiveItemsList([{ key: "all", type: "library", title: "My Library" }]);
      sidebar.handleIncomingSnippet({
        id: "snip_test_lib",
        type: "text",
        parentKey: "PARENT_PAPER_1",
        paperTitle: "Parent Research Paper",
        text: "Library transition excerpt",
      });
      assert.strictEqual(sidebar.getContextMode(), "papers");
      const itemsLib = sidebar.getActiveItemsList();
      assert.strictEqual(itemsLib.some(i => i.type === "library"), false, "Library chip must be cleared");
      assert.strictEqual(itemsLib.length, 0, "No paper chips auto-added");
    } finally {
      if (originalGetByLibraryAndKey) {
        env.zotero.Items.getByLibraryAndKey = originalGetByLibraryAndKey;
      } else {
        delete env.zotero.Items.getByLibraryAndKey;
      }
    }
  });

  // 6. handleIncomingSnippet adding text snippet and image snippet
  test("handleIncomingSnippet: adds text snippet and image snippet with deduplication", () => {
    sidebar.setActiveItemsList([]);
    sidebar.setActiveSnippetsList([]);

    const textSnippet = {
      id: "snip_text_1",
      type: "text",
      parentKey: "PAPER_1",
      paperTitle: "Attention Is All You Need",
      page: 3,
      text: "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
    };

    const imageSnippet = {
      id: "snip_img_1",
      type: "image",
      parentKey: "PAPER_1",
      paperTitle: "Attention Is All You Need",
      page: 5,
      image_data: "data:image/png;base64,FULL_IMG_DATA",
      thumbnail_data: "data:image/png;base64,THUMB_IMG_DATA",
    };

    sidebar.handleIncomingSnippet(textSnippet);
    sidebar.handleIncomingSnippet(imageSnippet);

    const snippets = sidebar.getActiveSnippetsList();
    assert.strictEqual(snippets.length, 2);
    assert.strictEqual(snippets[0].id, "snip_text_1");
    assert.strictEqual(snippets[0].type, "text");
    assert.strictEqual(snippets[1].id, "snip_img_1");
    assert.strictEqual(snippets[1].type, "image");

    // Deduplication by snippet.id
    sidebar.handleIncomingSnippet(textSnippet);
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 2);
  });

  // 7. renderActiveChips rendering .snippet-text-chip and .snippet-image-chip
  test("renderActiveChips: renders .snippet-text-chip and .snippet-image-chip with hover preview", () => {
    const chipsContainer = env.document.getElementById("chips-container");
    assert.ok(chipsContainer, "chips-container must be present");

    sidebar.setActiveItemsList([]);
    sidebar.setActiveSnippetsList([
      {
        id: "snip_text_1",
        type: "text",
        parentKey: "PAPER_1",
        paperTitle: "Attention Is All You Need",
        authorStr: "Vaswani et al.",
        page: 3,
        text: "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
      },
      {
        id: "snip_img_1",
        type: "image",
        parentKey: "PAPER_1",
        paperTitle: "Attention Is All You Need",
        authorStr: "Vaswani et al.",
        page: 5,
        image_data: "data:image/png;base64,FULL_IMG_DATA",
        thumbnail_data: "data:image/png;base64,THUMB_IMG_DATA",
      },
    ]);

    sidebar.renderActiveChips();

    const snippetChips = chipsContainer.querySelectorAll(".snippet-chip");
    assert.strictEqual(snippetChips.length, 2);

    // Text snippet chip
    const textChip = chipsContainer.querySelector(".snippet-text-chip");
    assert.ok(textChip, ".snippet-text-chip must be rendered");
    assert.strictEqual(textChip.dataset.snippetId, "snip_text_1");
    assert.ok(textChip.querySelector(".snippet-icon svg"), "SVG icon must be rendered on text snippet chip");
    assert.strictEqual(textChip.querySelector(".snippet-paper-tag").textContent, "Vaswani et al. (p.3):");
    assert.ok(textChip.querySelector(".chip-text").textContent.includes("The dominant sequence transduction mod…"));
    assert.ok(textChip.querySelector(".chip-text").title.includes("Vaswani et al. · Attention Is All You Need"));
    assert.ok(textChip.querySelector(".chip-text").title.includes("The dominant sequence transduction models are based"));

    // Image snippet chip
    const imgChip = chipsContainer.querySelector(".snippet-image-chip");
    assert.ok(imgChip, ".snippet-image-chip must be rendered");
    assert.strictEqual(imgChip.dataset.snippetId, "snip_img_1");
    const thumb = imgChip.querySelector(".snippet-thumbnail");
    assert.ok(thumb, ".snippet-thumbnail must be present");
    assert.strictEqual(thumb.src, "data:image/png;base64,THUMB_IMG_DATA");
    assert.strictEqual(imgChip.querySelector(".snippet-paper-tag").textContent, "Vaswani et al. (p.5):");
    assert.ok(imgChip.querySelector(".chip-text").textContent.includes("Area Crop"));

    // Hover preview popover on mouseenter / mouseleave
    imgChip.dispatchEvent("mouseenter");
    const hoverPreview = env.document.body.querySelector(".snippet-hover-preview") || imgChip.querySelector(".snippet-hover-preview");
    assert.ok(hoverPreview, ".snippet-hover-preview must be rendered on mouseenter");
    assert.strictEqual(hoverPreview.querySelector("img").src, "data:image/png;base64,FULL_IMG_DATA");
    assert.strictEqual(hoverPreview.querySelector(".snippet-preview-label").textContent, "Vaswani et al. · Attention Is All You Need · Page 5");

    imgChip.dispatchEvent("mouseleave");
    assert.strictEqual(env.document.body.querySelector(".snippet-hover-preview") || imgChip.querySelector(".snippet-hover-preview"), null, "Hover preview must be removed on mouseleave");
  });

  // 8. close button on snippet removing the snippet
  test("renderActiveChips: close button on snippet removes the snippet", () => {
    const chipsContainer = env.document.getElementById("chips-container");
    sidebar.setActiveItemsList([]);
    sidebar.setActiveSnippetsList([
      { id: "s1", type: "text", text: "First snippet" },
      { id: "s2", type: "text", text: "Second snippet" },
    ]);
    sidebar.renderActiveChips();

    const chipsBefore = chipsContainer.querySelectorAll(".snippet-chip");
    assert.strictEqual(chipsBefore.length, 2);

    // Remove first snippet
    const closeBtn = chipsBefore[0].querySelector(".chip-remove");
    assert.ok(closeBtn, "Close button must exist on snippet chip");
    closeBtn.dispatchEvent("click");

    assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);
    assert.strictEqual(sidebar.getActiveSnippetsList()[0].id, "s2");

    const chipsAfter = chipsContainer.querySelectorAll(".snippet-chip");
    assert.strictEqual(chipsAfter.length, 1);
    assert.strictEqual(chipsAfter[0].dataset.snippetId, "s2");
  });

  // 9. clearAllChipsBtn clearing both activeItemsList and activeSnippetsList
  test("clearAllChipsBtn: clears both activeItemsList and activeSnippetsList", () => {
    const chipsContainer = env.document.getElementById("chips-container");
    const clearAllChipsBtn = env.document.getElementById("clear-all-chips-btn");
    assert.ok(clearAllChipsBtn, "clear-all-chips-btn must be present");

    sidebar.setActiveItemsList([
      { key: "ITEM_1", title: "Paper One", type: "item" },
    ]);
    sidebar.setActiveSnippetsList([
      { id: "SNIP_1", type: "text", text: "Snippet text" },
    ]);
    sidebar.renderActiveChips();

    // Total chips = 2, so clearAllChipsBtn should not be hidden
    assert.strictEqual(clearAllChipsBtn.classList.contains("hidden"), false);
    assert.strictEqual(chipsContainer.querySelectorAll(".paper-chip").length, 2);

    // Click clear-all-chips-btn
    clearAllChipsBtn.dispatchEvent("click");

    assert.strictEqual(sidebar.getActiveItemsList().length, 0);
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 0);
    assert.strictEqual(chipsContainer.querySelectorAll(".paper-chip").length, 0);
    assert.strictEqual(clearAllChipsBtn.classList.contains("hidden"), true);
  });

  // 10. finalizeChatStream clearing snippets when !isAborted and retaining when isAborted=true
  test("finalizeChatStream: clears snippets when !isAborted and retains when isAborted=true", () => {
    sidebar.setActiveSnippetsList([
      { id: "snip_abort_test", type: "text", text: "Retained snippet" },
    ]);
    sidebar.renderActiveChips();
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);

    // Aborted stream (isAborted = true) -> snippets must be retained
    sidebar.finalizeChatStream({ stepsBubble: null, session: null }, true);
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 1, "Snippets must be retained when chat is aborted");

    // Successful stream (isAborted = false) -> snippets must be cleared
    sidebar.finalizeChatStream({ stepsBubble: null, session: null }, false);
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 0, "Snippets must be cleared on successful stream completion");
  });

  // 11. syncFromDetachedState restoring activeSnippetsList
  test("syncFromDetachedState: restores activeSnippetsList and updates chips", () => {
    sidebar.setActiveSnippetsList([]);
    global.window.syncFromDetachedState({
      activeSnippetsList: [
        { id: "snip_detached", type: "text", text: "Detached text", paperTitle: "Detached Paper" },
      ],
    });
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);
    assert.strictEqual(sidebar.getActiveSnippetsList()[0].id, "snip_detached");
    const chipsContainer = env.document.getElementById("chips-container");
    assert.strictEqual(chipsContainer.querySelectorAll(".snippet-chip").length, 1);
  });

  // 12. createNewSession clearing activeSnippetsList
  await testAsync("createNewSession: clears activeSnippetsList and re-renders chips", async () => {
    const origFetch = global.fetch;
    global.fetch = async () => ({
      ok: true,
      statusText: "OK",
      json: async () => ({ session: { id: "mock_s_1", title: "New Research Chat" } })
    });
    try {
      sidebar.setActiveSnippetsList([{ id: "s_session", type: "text", text: "Session text" }]);
      sidebar.renderActiveChips();
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);

      await sidebar.createNewSession("New Research Chat");
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 0);
    } finally {
      global.fetch = origFetch;
    }
  });

  // 13. streamDialogueResponse payload mapping for snippets
  await testAsync("streamDialogueResponse: maps activeSnippetsList into fetch POST payload", async () => {
    let capturedBody = null;
    const origFetch = global.fetch;
    global.fetch = async (url, opts) => {
      if (url.includes("/chat")) {
        capturedBody = JSON.parse(opts.body);
        return {
          ok: true,
          statusText: "OK",
          body: {
            getReader: () => ({
              read: async () => ({ done: true, value: new Uint8Array() }),
            }),
          },
        };
      }
      return { ok: true, statusText: "OK", json: async () => ({}) };
    };

    try {
      sidebar.setActiveItemsList([{ key: "ITEM_PAYLOAD", title: "Payload Paper", type: "item" }]);
      sidebar.setActiveSnippetsList([
        {
          id: "snip_payload_1",
          type: "text",
          item_key: "ATT_1",
          parent_key: "ITEM_PAYLOAD",
          paper_title: "Payload Paper",
          page: 2,
          text: "Retrieved snippet text",
        },
      ]);

      await sidebar.streamDialogueResponse("Analyze this snippet");

      assert.ok(capturedBody, "POST /chat must have been called");
      assert.strictEqual(capturedBody.prompt, "Analyze this snippet");
      assert.ok(Array.isArray(capturedBody.snippets), "Payload must contain snippets array");
      assert.strictEqual(capturedBody.snippets.length, 1);
      assert.strictEqual(capturedBody.snippets[0].id, "snip_payload_1");
      assert.strictEqual(capturedBody.snippets[0].type, "text");
      assert.strictEqual(capturedBody.snippets[0].item_key, "ATT_1");
      assert.strictEqual(capturedBody.snippets[0].parent_key, "ITEM_PAYLOAD");
      assert.strictEqual(capturedBody.snippets[0].text, "Retrieved snippet text");
      assert.strictEqual(capturedBody.snippets[0].page, 2);

      // Successfully finished stream should clear activeSnippetsList
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 0);
    } finally {
      global.fetch = origFetch;
    }
  });

  // 14. applySelection preserves parent paper chip in non-sticky mode when snippet is active
  test("applySelection: preserves snippet parent paper chip across selection polling in non-sticky mode", () => {
    sidebar.setActiveSnippetsList([
      { id: "snip_keep_1", type: "text", text: "Some excerpt", parent_key: "PARENT_ANCHOR", paper_title: "Anchored Paper" }
    ]);
    sidebar.setActiveItemsList([
      { key: "PARENT_ANCHOR", title: "Anchored Paper", authorStr: "", year: "", type: "item" }
    ]);
    sidebar.renderActiveChips();

    // User clicks another paper in library tree (NEW_SELECTION)
    const newItems = [
      { key: "NEW_SELECTION", title: "Newly Clicked Paper", isRegularItem: () => true, getField: () => "Newly Clicked Paper", getCreatorsJSON: () => [] }
    ];
    sidebar.applySelection(newItems, false);

    const activeKeys = sidebar.getActiveItemsList().map(i => i.key);
    assert.ok(activeKeys.includes("NEW_SELECTION"), "Must include newly selected item");
    assert.ok(activeKeys.includes("PARENT_ANCHOR"), "Must preserve parent anchor while snippet is active");
  });

  // 15. handleSnippetUpdate updates matching image snippet in activeSnippetsList in-place and triggers chip re-render
  test("handleSnippetUpdate: updates matching image snippet in activeSnippetsList in-place and triggers chip re-render", () => {
    const chipsContainer = env.document.getElementById("chips-container");
    assert.ok(chipsContainer, "chips-container must be present");

    const initialImageSnippet = {
      id: "snip_img_target",
      type: "image",
      annotation_id: "ANN_RESIZE_KEY",
      annotationId: "ANN_RESIZE_KEY",
      parentKey: "PAPER_1",
      paperTitle: "Attention Is All You Need",
      page: 4,
      image_data: "data:image/jpeg;base64,ORIGINAL_FULL_IMG",
      imageData: "data:image/jpeg;base64,ORIGINAL_FULL_IMG",
      thumbnail_data: "data:image/jpeg;base64,ORIGINAL_THUMB_IMG",
      thumbnailData: "data:image/jpeg;base64,ORIGINAL_THUMB_IMG",
      width: 400,
      height: 300,
    };

    const textSnippet = {
      id: "snip_txt_other",
      type: "text",
      annotation_id: "ANN_RESIZE_KEY",
      annotationId: "ANN_RESIZE_KEY",
      parentKey: "PAPER_1",
      paperTitle: "Attention Is All You Need",
      page: 2,
      text: "Untouched text snippet",
    };

    const otherImageSnippet = {
      id: "snip_img_other",
      type: "image",
      annotation_id: "ANN_OTHER_KEY",
      annotationId: "ANN_OTHER_KEY",
      parentKey: "PAPER_1",
      paperTitle: "Attention Is All You Need",
      page: 6,
      image_data: "data:image/jpeg;base64,OTHER_IMG",
      imageData: "data:image/jpeg;base64,OTHER_IMG",
      thumbnail_data: "data:image/jpeg;base64,OTHER_THUMB",
      thumbnailData: "data:image/jpeg;base64,OTHER_THUMB",
      width: 200,
      height: 200,
    };

    sidebar.setActiveItemsList([]);
    sidebar.setActiveSnippetsList([initialImageSnippet, textSnippet, otherImageSnippet]);
    sidebar.renderActiveChips();

    // Verify initial thumbnail
    const initialThumb = chipsContainer.querySelector(`[data-snippet-id="snip_img_target"] .snippet-thumbnail`);
    assert.ok(initialThumb);
    assert.strictEqual(initialThumb.src, "data:image/jpeg;base64,ORIGINAL_THUMB_IMG");

    // Perform update
    const updatePayload = {
      annotation_id: "ANN_RESIZE_KEY",
      image_data: "data:image/jpeg;base64,NEW_FULL_IMG",
      imageData: "data:image/jpeg;base64,NEW_FULL_IMG",
      thumbnail_data: "data:image/jpeg;base64,NEW_THUMB_IMG",
      thumbnailData: "data:image/jpeg;base64,NEW_THUMB_IMG",
      width: 800,
      height: 600,
    };

    sidebar.handleSnippetUpdate(updatePayload);

    // 1. Snippet updated in-place in activeSnippetsList
    const snippets = sidebar.getActiveSnippetsList();
    const updated = snippets.find(s => s.id === "snip_img_target");
    assert.ok(updated);
    assert.strictEqual(updated.image_data, "data:image/jpeg;base64,NEW_FULL_IMG");
    assert.strictEqual(updated.imageData, "data:image/jpeg;base64,NEW_FULL_IMG");
    assert.strictEqual(updated.thumbnail_data, "data:image/jpeg;base64,NEW_THUMB_IMG");
    assert.strictEqual(updated.thumbnailData, "data:image/jpeg;base64,NEW_THUMB_IMG");
    assert.strictEqual(updated.width, 800);
    assert.strictEqual(updated.height, 600);

    // 2. Untouched snippets unchanged
    const textSnip = snippets.find(s => s.id === "snip_txt_other");
    assert.strictEqual(textSnip.text, "Untouched text snippet");
    const otherImg = snippets.find(s => s.id === "snip_img_other");
    assert.strictEqual(otherImg.width, 200);
    assert.strictEqual(otherImg.thumbnail_data, "data:image/jpeg;base64,OTHER_THUMB");

    // 3. Re-rendered DOM chip has updated thumbnail
    const updatedThumb = chipsContainer.querySelector(`[data-snippet-id="snip_img_target"] .snippet-thumbnail`);
    assert.ok(updatedThumb);
    assert.strictEqual(updatedThumb.src, "data:image/jpeg;base64,NEW_THUMB_IMG");

    // 4. Hover preview uses updated full image
    const targetChip = chipsContainer.querySelector(`[data-snippet-id="snip_img_target"]`);
    targetChip.dispatchEvent("mouseenter");
    const hoverPreview = env.document.body.querySelector(".snippet-hover-preview") || targetChip.querySelector(".snippet-hover-preview");
    assert.ok(hoverPreview);
    assert.strictEqual(hoverPreview.querySelector("img").src, "data:image/jpeg;base64,NEW_FULL_IMG");
    targetChip.dispatchEvent("mouseleave");

    // 5. Safe handling of invalid / non-matching payload
    sidebar.handleSnippetUpdate(null);
    sidebar.handleSnippetUpdate({});
    sidebar.handleSnippetUpdate({ annotation_id: "NON_EXISTENT_KEY" });
  });

  // 17. Switching to library or collection mode clears activeSnippetsList
  test("context mode switch to library/collection: clears activeSnippetsList", () => {
    sidebar.setContextMode("papers");
    sidebar.setActiveItemsList([{ key: "PAPER_1", type: "item", title: "Paper 1" }]);
    sidebar.setActiveSnippetsList([{ id: "snip_active", type: "text", text: "Some excerpt" }]);

    // Trigger Add Library option click
    const addLibraryOption = env.document.getElementById("add-library-option");
    assert.ok(addLibraryOption);
    addLibraryOption.dispatchEvent("click");

    assert.strictEqual(sidebar.getContextMode(), "library");
    assert.strictEqual(sidebar.getActiveSnippetsList().length, 0, "activeSnippetsList must be cleared when switching to library mode");
    assert.strictEqual(sidebar.getActiveItemsList().length, 1);
    assert.strictEqual(sidebar.getActiveItemsList()[0].type, "library");
  });

  // 18. openMessageInfoModal renders Focal Snippets chips
  test("openMessageInfoModal: renders Focal Snippets chips", () => {
    const meta = {
      role: "user",
      author: "User",
      text: "Explain this figure and quote",
      snippets: [
        {
          id: "snip_txt_1",
          type: "text",
          author_str: "Vaswani et al.",
          paper_title: "Attention Is All You Need",
          page: 3,
          text: "Self-attention mechanism computes representations of a single sequence.",
          publication: "NeurIPS",
          year: 2017,
        },
        {
          id: "snip_img_2",
          type: "image",
          author_str: "He et al.",
          paper_title: "Deep Residual Learning",
          page: 5,
          image_data: "data:image/jpeg;base64,AREA_DATA",
          year: 2016,
        },
      ],
    };

    sidebar.openMessageInfoModal(meta);

    const overlay = env.document.querySelector(".info-modal-overlay");
    assert.ok(overlay, "Modal overlay must exist in DOM");

    const rows = Array.from(overlay.querySelectorAll(".info-meta-row"));
    const focalRow = rows.find(r => r.querySelector(".info-meta-label")?.textContent === "Focal Snippets");
    assert.ok(focalRow, "Focal Snippets row must be rendered in modal");

    const chipsList = focalRow.querySelector(".info-meta-chips-list");
    assert.ok(chipsList, ".info-meta-chips-list must be present in row value");

    const chips = Array.from(chipsList.querySelectorAll(".info-meta-chip-item"));
    assert.strictEqual(chips.length, 2, "Must render exactly 2 focal snippet chips");

    // Text snippet chip assertions
    const textChip = chips[0];
    assert.ok(textChip.textContent.includes("📄"), "Text snippet must include document icon");
    assert.ok(textChip.textContent.includes("Vaswani et al."), "Text snippet must include author tag");
    assert.ok(textChip.textContent.includes("(p.3)"), "Text snippet must include page string");
    assert.ok(textChip.textContent.includes("Self-attention"), "Text snippet must include excerpt quote");
    assert.ok(textChip.title.includes("Vaswani et al."), "Tooltip must include author");
    assert.ok(textChip.title.includes("Attention Is All You Need"), "Tooltip must include paper title");
    assert.ok(textChip.title.includes("Page 3"), "Tooltip must include page number");
    assert.ok(textChip.title.includes("NeurIPS"), "Tooltip must include publication details");

    // Image snippet chip assertions
    const imgChip = chips[1];
    assert.ok(imgChip.textContent.includes("🖼️"), "Image snippet must include image icon");
    assert.ok(imgChip.textContent.includes("He et al."), "Image snippet must include author tag");
    assert.ok(imgChip.textContent.includes("(p.5)"), "Image snippet must include page string");
    assert.ok(imgChip.textContent.includes("Area Crop"), "Image snippet must indicate Area Crop");
    assert.ok(imgChip.title.includes("He et al."), "Tooltip must include author");
    assert.ok(imgChip.title.includes("Deep Residual Learning"), "Tooltip must include paper title");
    assert.ok(imgChip.title.includes("Page 5"), "Tooltip must include page number");

    // Close modal
    overlay.remove();
  });

  // 19. executeRerun & enableInlineEditing restore target message context and snippets
  await testAsync("executeRerun: restores target message's original active_context_items and snippets to activeItemsList and activeSnippetsList", async () => {
    const origFetch = global.fetch;
    const testSessionId = "sess_rerun_test";

    const originalContextItems = [
      { key: "ITEM_RERUN_1", title: "Rerun Paper Alpha", type: "item" },
    ];
    const originalSnippets = [
      {
        id: "snip_rerun_1",
        type: "text",
        item_key: "ATT_RERUN_1",
        parent_key: "ITEM_RERUN_1",
        paper_title: "Rerun Paper Alpha",
        page: 4,
        text: "Restored snippet text",
      },
    ];

    let capturedPayload = null;
    global.fetch = async (url, opts) => {
      if (url.includes("/rewind")) {
        return { ok: true, status: 200, json: async () => ({ status: "success" }) };
      }
      if (url.includes(`/sessions/${testSessionId}`)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            session: {
              id: testSessionId,
              messages: [],
            },
          }),
        };
      }
      if (url.includes("/chat")) {
        capturedPayload = JSON.parse(opts.body);
        return {
          ok: true,
          statusText: "OK",
          body: {
            getReader: () => ({
              read: async () => ({ done: true, value: new Uint8Array() }),
            }),
          },
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    try {
      sidebar.setCurrentSessionId(testSessionId);
      const mockSession = {
        id: testSessionId,
        messages: [
          {
            role: "user",
            sender: "user",
            text: "Initial user question",
            timestamp: "10/10/2026, 12:00:00 PM",
            invocation_id: "inv-rerun-turn-1",
            active_context_items: originalContextItems,
            snippets: originalSnippets,
          },
          {
            role: "assistant",
            sender: "assistant",
            text: "Assistant answer",
            timestamp: "10/10/2026, 12:00:05 PM",
            invocation_id: "inv-rerun-turn-1",
          },
        ],
      };
      sidebar.setSessions([mockSession]);

      // Create message wrapper DOM simulating user message in chat
      const chatMessages = env.document.getElementById("chat-messages");
      const wrapperDiv = env.document.createElement("div");
      wrapperDiv.className = "message-wrapper user";
      wrapperDiv.dataset.timestamp = "10/10/2026, 12:00:00 PM";
      wrapperDiv.dataset.invocationId = "inv-rerun-turn-1";

      const msgDiv = env.document.createElement("div");
      msgDiv.className = "message user";
      msgDiv.textContent = "Initial user question";
      wrapperDiv.appendChild(msgDiv);

      const infoBtn = env.document.createElement("div");
      infoBtn.className = "message-action-btn info-btn";
      infoBtn._metadata = {
        role: "user",
        text: "Initial user question",
        active_context_items: originalContextItems,
        snippets: originalSnippets,
      };
      wrapperDiv.appendChild(infoBtn);
      chatMessages.appendChild(wrapperDiv);

      // Verify enableInlineEditing restores items, snippets, and sets contextMode="papers"
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);
      sidebar.setContextMode("library");

      sidebar.enableInlineEditing(wrapperDiv, "Initial user question");

      assert.strictEqual(sidebar.getContextMode(), "papers", "enableInlineEditing must switch contextMode to 'papers' when snippets exist");
      assert.strictEqual(sidebar.getActiveItemsList().length, 1);
      assert.strictEqual(sidebar.getActiveItemsList()[0].key, "ITEM_RERUN_1");
      assert.strictEqual(sidebar.getActiveSnippetsList().length, 1);
      assert.strictEqual(sidebar.getActiveSnippetsList()[0].id, "snip_rerun_1");

      // Verify executeRerun restores items, snippets, and sends them to backend
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);
      sidebar.setContextMode("library");

      await sidebar.executeRerun(wrapperDiv, "Updated rerun question");

      // Check captured payload in streamDialogueResponse
      assert.ok(capturedPayload, "executeRerun must stream updated prompt");
      assert.strictEqual(capturedPayload.prompt, "Updated rerun question");
      assert.ok(Array.isArray(capturedPayload.snippets), "Payload must contain restored snippets");
      assert.strictEqual(capturedPayload.snippets.length, 1);
      assert.strictEqual(capturedPayload.snippets[0].id, "snip_rerun_1");
      assert.strictEqual(capturedPayload.snippets[0].text, "Restored snippet text");

      // Check activeItemsList retained restored item
      assert.strictEqual(sidebar.getActiveItemsList().length, 1);
      assert.strictEqual(sidebar.getActiveItemsList()[0].key, "ITEM_RERUN_1");
    } finally {
      global.fetch = origFetch;
    }
  });

  // 20. executeRerun and enableInlineEditing normalize item_key/itemKey and snippets aliases
  await testAsync("executeRerun & enableInlineEditing: normalizes restored items with item_key to produce payload with key and populated aliases", async () => {
    const origFetch = global.fetch;
    const testSessionId = "sess_normalization_test";

    const contextItemsWithItemKeyOnly = [
      { item_key: "KEY_FROM_ITEM_KEY", title: "Item Key Paper", type: "item" },
    ];
    const snippetsWithAliases = [
      {
        id: "snip_alias_1",
        type: "text",
        item_key: "ATT_KEY_1",
        parent_key: "KEY_FROM_ITEM_KEY",
        paper_title: "Aliased Title",
        author_str: "Author A",
        text: "Snippet body",
      },
    ];

    let capturedPayload = null;
    global.fetch = async (url, opts) => {
      if (url.includes("/rewind")) {
        return { ok: true, status: 200, json: async () => ({ status: "success" }) };
      }
      if (url.includes(`/sessions/${testSessionId}`)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ session: { id: testSessionId, messages: [] } }),
        };
      }
      if (url.includes("/chat")) {
        capturedPayload = JSON.parse(opts.body);
        return {
          ok: true,
          statusText: "OK",
          body: {
            getReader: () => ({
              read: async () => ({ done: true, value: new Uint8Array() }),
            }),
          },
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    try {
      sidebar.setCurrentSessionId(testSessionId);
      const mockSession = {
        id: testSessionId,
        messages: [
          {
            role: "user",
            sender: "user",
            text: "Question with item_key only",
            timestamp: "10/10/2026, 01:00:00 PM",
            invocation_id: "inv-norm-1",
            active_context_items: contextItemsWithItemKeyOnly,
            snippets: snippetsWithAliases,
          },
        ],
      };
      sidebar.setSessions([mockSession]);

      const chatMessages = env.document.getElementById("chat-messages");
      const wrapperDiv = env.document.createElement("div");
      wrapperDiv.className = "message-wrapper user";
      wrapperDiv.dataset.timestamp = "10/10/2026, 01:00:00 PM";
      wrapperDiv.dataset.invocationId = "inv-norm-1";

      const msgDiv = env.document.createElement("div");
      msgDiv.className = "message user";
      msgDiv.textContent = "Question with item_key only";
      wrapperDiv.appendChild(msgDiv);

      const infoBtn = env.document.createElement("div");
      infoBtn.className = "message-action-btn info-btn";
      infoBtn._metadata = {
        role: "user",
        text: "Question with item_key only",
        active_context_items: contextItemsWithItemKeyOnly,
        snippets: snippetsWithAliases,
      };
      wrapperDiv.appendChild(infoBtn);
      chatMessages.appendChild(wrapperDiv);

      // 1. Test enableInlineEditing normalization
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);
      sidebar.enableInlineEditing(wrapperDiv, "Question with item_key only");

      const inlineItems = sidebar.getActiveItemsList();
      assert.strictEqual(inlineItems.length, 1);
      assert.strictEqual(inlineItems[0].key, "KEY_FROM_ITEM_KEY", "enableInlineEditing must guarantee key from item_key");

      const inlineSnippets = sidebar.getActiveSnippetsList();
      assert.strictEqual(inlineSnippets.length, 1);
      assert.strictEqual(inlineSnippets[0].itemKey, "ATT_KEY_1", "Snippet camelCase itemKey must be populated");
      assert.strictEqual(inlineSnippets[0].parentKey, "KEY_FROM_ITEM_KEY", "Snippet camelCase parentKey must be populated");
      assert.strictEqual(inlineSnippets[0].paperTitle, "Aliased Title", "Snippet camelCase paperTitle must be populated");
      assert.strictEqual(inlineSnippets[0].authorStr, "Author A", "Snippet camelCase authorStr must be populated");

      // 2. Test executeRerun normalization and payload generation
      sidebar.setActiveItemsList([]);
      sidebar.setActiveSnippetsList([]);

      await sidebar.executeRerun(wrapperDiv, "Rerun question with item_key");

      assert.ok(capturedPayload, "executeRerun must stream payload to /chat");
      assert.ok(Array.isArray(capturedPayload.active_items), "Payload active_items must be array");
      assert.strictEqual(capturedPayload.active_items.length, 1);
      assert.strictEqual(
        capturedPayload.active_items[0].key,
        "KEY_FROM_ITEM_KEY",
        "Payload active_items must have 'key' populated when restored from 'item_key'"
      );
      assert.ok(Array.isArray(capturedPayload.snippets), "Payload snippets must be array");
      assert.strictEqual(capturedPayload.snippets[0].item_key, "ATT_KEY_1");
      assert.strictEqual(capturedPayload.snippets[0].itemKey, "ATT_KEY_1");
    } finally {
      global.fetch = origFetch;
    }
  });

  // 21. streamDialogueResponse error handling serializes object/array details without [object Object]
  await testAsync("streamDialogueResponse: error handling serializes object or array detail without [object Object]", async () => {
    const origFetch = global.fetch;
    const testSessionId = "sess_err_test";

    const errorDetailsToTest = [
      {
        name: "Array detail from validation error",
        status: 422,
        body: {
          detail: [
            { loc: ["body", "active_items", 0, "key"], msg: "Field required", type: "missing" },
          ],
        },
      },
      {
        name: "Object detail error",
        status: 400,
        body: {
          detail: { error_code: "INVALID_SELECTION", reason: "Item not found" },
        },
      },
    ];

    try {
      sidebar.setCurrentSessionId(testSessionId);

      for (const tc of errorDetailsToTest) {
        const mockSession = {
          id: testSessionId,
          messages: [],
        };
        sidebar.setSessions([mockSession]);

        global.fetch = async () => ({
          ok: false,
          status: tc.status,
          statusText: "Unprocessable Entity",
          json: async () => tc.body,
        });

        await sidebar.streamDialogueResponse("Test error prompt");

        const lastMsg = mockSession.messages[mockSession.messages.length - 1];
        assert.ok(lastMsg, "Error message should be appended to session");
        assert.ok(
          !lastMsg.text.includes("[object Object]"),
          `Error message for '${tc.name}' must NOT contain '[object Object]', got: ${lastMsg.text}`
        );
        assert.ok(
          lastMsg.text.includes(JSON.stringify(tc.body.detail)),
          `Error message must contain stringified detail, got: ${lastMsg.text}`
        );
      }
    } finally {
      global.fetch = origFetch;
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

/**
 * test_claims_column_resizing.js — Unit test suite for adjustable column widths in claims table.
 */

const assert = require("assert");
const { test, testAsync, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

function createMockClaimsTableDOM(env) {
  env.document.body.children = [];
  const container = createMockElement("div");
  container.id = "claims-table-container";
  container.scrollLeft = 0;
  container.scrollTop = 0;
  container.offsetLeft = 10;
  container.offsetTop = 0;
  container.offsetWidth = 800;
  container.clientHeight = 500;

  const table = createMockElement("table");
  table.className = "claims-table";
  table.offsetWidth = 800;

  const thead = createMockElement("thead");
  const tr = createMockElement("tr");

  const colClasses = ["col-idx", "col-claim", "col-source", "col-passage", "col-prob"];
  const colSorts = ["index", "claim", "source", null, "prob"];
  const colInitialWidths = [50, 250, 180, 220, 140];
  const ths = [];

  for (let i = 0; i < 5; i++) {
    const th = createMockElement("th");
    th.className = colClasses[i] + (colSorts[i] ? " sortable" : "") + (i === 0 ? " sorted" : "");
    if (colSorts[i]) {
      th.setAttribute("data-sort", colSorts[i]);
      const icon = createMockElement("span");
      icon.className = "sort-icon";
      th.appendChild(icon);
    }
    th.offsetWidth = colInitialWidths[i];
    th.offsetLeft = colInitialWidths.slice(0, i).reduce((a, b) => a + b, 0);
    ths.push(th);
    tr.appendChild(th);
  }

  thead.appendChild(tr);
  table.appendChild(thead);

  const tbody = createMockElement("tbody");
  tbody.id = "claims-tbody";
  table.appendChild(tbody);

  container.appendChild(table);

  env.document.body.appendChild(container);
  env.elementsById.set("claims-table-container", container);
  env.elementsById.set("claims-tbody", tbody);

  return { container, table, ths };
}

async function runTests() {
  console.log("\n--- Suite: Claims Column Resizing ---");
  const env = setupMockEnvironment();
  const claimsModule = require("../../zotero-plugin/addon/claims.js");

  test("initColumnResizing: attaches col-resizer handles to columns 0 through 3, omitting last column", () => {
    const { container, table, ths } = createMockClaimsTableDOM(env);

    claimsModule.initColumnResizing();

    for (let i = 0; i < 4; i++) {
      const resizer = ths[i].querySelector(".col-resizer");
      assert.ok(resizer, `Column ${i} must have a .col-resizer handle`);
      assert.strictEqual(resizer.getAttribute("data-col-index"), String(i));
      assert.strictEqual(resizer.title, "Drag to resize column (double-click to reset)");
    }

    const lastResizer = ths[4].querySelector(".col-resizer");
    assert.strictEqual(lastResizer, null, "Last column (.col-prob) must not have a resizer handle");
  });

  test("initColumnResizing: creates single col-resize-guide in container", () => {
    const { container } = createMockClaimsTableDOM(env);

    claimsModule.initColumnResizing();

    const guide = container.querySelector("#col-resize-guide");
    assert.ok(guide, "Guide line element #col-resize-guide must be present in container");
    assert.ok(guide.classList.contains("col-resize-guide"), "Guide line must have class .col-resize-guide");
  });

  test("col-resizer click: stops propagation to avoid triggering sort on column", () => {
    const { table, ths } = createMockClaimsTableDOM(env);
    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");
    assert.ok(resizer);

    let stopped = false;
    resizer.dispatchEvent({
      type: "click",
      stopPropagation: () => { stopped = true; },
      preventDefault: () => {},
    });

    assert.strictEqual(stopped, true, "Click on resizer must call stopPropagation()");
  });

  test("col-resizer dblclick: resets column widths to default shares and clears tableLayout", () => {
    const { table, ths } = createMockClaimsTableDOM(env);
    claimsModule.initColumnResizing();

    ths[0].style.width = "80px";
    ths[1].style.width = "400px";
    ths[2].style.width = "100px";
    ths[3].style.width = "100px";
    ths[4].style.width = "120px";
    table.style.tableLayout = "fixed";

    const resizer = ths[1].querySelector(".col-resizer");
    let prevented = false;
    let stopped = false;
    resizer.dispatchEvent({
      type: "dblclick",
      preventDefault: () => { prevented = true; },
      stopPropagation: () => { stopped = true; },
    });

    assert.strictEqual(prevented, true, "dblclick must call preventDefault()");
    assert.strictEqual(stopped, true, "dblclick must call stopPropagation()");
    assert.strictEqual(ths[0].style.width, "50px");
    assert.strictEqual(ths[1].style.width, "32%");
    assert.strictEqual(ths[2].style.width, "22%");
    assert.strictEqual(ths[3].style.width, "32%");
    assert.strictEqual(ths[4].style.width, "140px");
    assert.strictEqual(table.style.tableLayout, "");
  });

  test("col-resizer mousedown: locks layout to fixed and initializes resize state", () => {
    const { container, table, ths } = createMockClaimsTableDOM(env);
    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");
    let prevented = false;
    let stopped = false;

    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 300,
      preventDefault: () => { prevented = true; },
      stopPropagation: () => { stopped = true; },
    });

    assert.strictEqual(prevented, true);
    assert.strictEqual(stopped, true);
    assert.strictEqual(table.style.tableLayout, "fixed");
    assert.strictEqual(claimsModule.getIsColumnResizing(), true);
    assert.ok(resizer.classList.contains("is-resizing"));
    assert.ok(env.document.body.classList.contains("is-resizing-cols"));

    const guide = container.querySelector("#col-resize-guide");
    assert.strictEqual(guide.style.display, "block");
    assert.strictEqual(guide.style.top, container.scrollTop + "px");
    assert.strictEqual(guide.style.height, container.clientHeight + "px");

    // Clean up with mouseup
    env.window.dispatchEvent({ type: "mouseup" });
    assert.strictEqual(claimsModule.getIsColumnResizing(), false);
  });

  test("col-resizer mousedown: snapshots column widths fresh on every mousedown even if tableLayout was already fixed", () => {
    const { table, ths } = createMockClaimsTableDOM(env);
    table.style.tableLayout = "fixed";
    ths[0].offsetWidth = 75;
    ths[1].offsetWidth = 225;

    claimsModule.initColumnResizing();

    const resizer = ths[0].querySelector(".col-resizer");
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 75,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    assert.strictEqual(ths[0].style.width, "75px", "Must snapshot updated width even if tableLayout was already fixed");
    assert.strictEqual(ths[1].style.width, "225px");
    env.window.dispatchEvent({ type: "mouseup" });
  });

  test("col-resizer guide position: updateGuidePosition sets top, left, and height matching container scroll and dimensions", () => {
    const { container, ths } = createMockClaimsTableDOM(env);
    container.scrollTop = 35;
    container.scrollLeft = 12;
    container.clientHeight = 450;

    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 300,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    const guide = container.querySelector("#col-resize-guide");
    assert.strictEqual(guide.style.display, "block");
    assert.strictEqual(guide.style.top, "35px");
    assert.strictEqual(guide.style.height, "450px");
    const containerRect = container.getBoundingClientRect();
    const expectedLeft = ths[1].getBoundingClientRect().right - containerRect.left + container.scrollLeft;
    assert.strictEqual(guide.style.left, expectedLeft + "px");

    env.window.dispatchEvent({ type: "mouseup" });
  });

  test("dragging with mousemove: clamps width to minimum floors (50px for col 0, 140px for col 1, 130px for col 2)", () => {
    const { container, table, ths } = createMockClaimsTableDOM(env);
    // col 1 initial width 250px, col 2 initial width 180px, sum = 430px
    ths[1].offsetWidth = 250;
    ths[2].offsetWidth = 180;

    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");

    // Start drag at x = 300
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 300,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    // Move left by 200px (target newWidthA = 250 - 200 = 50px, below minA 140px)
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 100,
    });

    // Clamped: col 1 should be 140px, col 2 should take remainder 430 - 140 = 290px
    assert.strictEqual(ths[1].style.width, "140px");
    assert.strictEqual(ths[2].style.width, "290px");

    // Move right by 200px (target newWidthB = 180 - 200 = -20px, below minB 130px)
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 500,
    });

    // Clamped: col 2 should be 130px, col 1 should take remainder 430 - 130 = 300px
    assert.strictEqual(ths[2].style.width, "130px");
    assert.strictEqual(ths[1].style.width, "300px");

    // Finish resize
    env.window.dispatchEvent({ type: "mouseup" });
    assert.strictEqual(claimsModule.getIsColumnResizing(), false);
    assert.strictEqual(claimsModule.getJustResizedColumn(), true);

    // Also test col 0 clamp to MIN_COL_WIDTHS[0] = 50px
    ths[0].offsetWidth = 50;
    ths[1].offsetWidth = 250;
    const resizer0 = ths[0].querySelector(".col-resizer");
    resizer0.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 50,
      preventDefault: () => {},
      stopPropagation: () => {},
    });
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 20, // target newWidthA = 50 - 30 = 20px, below minA 50px
    });
    assert.strictEqual(ths[0].style.width, "50px");
    assert.strictEqual(ths[1].style.width, "250px");
    env.window.dispatchEvent({ type: "mouseup" });
  });

  test("col-resizer cleanup: window blur event aborts resize and cleans up state", () => {
    const { container, ths } = createMockClaimsTableDOM(env);
    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 300,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    assert.strictEqual(claimsModule.getIsColumnResizing(), true);
    const guide = container.querySelector("#col-resize-guide");
    assert.strictEqual(guide.style.display, "block");
    assert.strictEqual(resizer.classList.contains("is-resizing"), true);
    assert.ok(env.document.body.classList.contains("is-resizing-cols"));

    // Trigger blur on window
    env.window.dispatchEvent({ type: "blur" });

    assert.strictEqual(claimsModule.getIsColumnResizing(), false);
    assert.strictEqual(guide.style.display, "none");
    assert.strictEqual(resizer.classList.contains("is-resizing"), false);
    assert.strictEqual(env.document.body.classList.contains("is-resizing-cols"), false);
  });

  test("col-resizer cleanup: mousemove with buttons === 0 triggers early mouseup cleanup", () => {
    const { container, ths } = createMockClaimsTableDOM(env);
    claimsModule.initColumnResizing();

    const resizer = ths[1].querySelector(".col-resizer");
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 300,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    assert.strictEqual(claimsModule.getIsColumnResizing(), true);
    const guide = container.querySelector("#col-resize-guide");
    assert.strictEqual(guide.style.display, "block");

    // Mousemove with buttons === 0 indicates button was released outside window
    env.window.dispatchEvent({
      type: "mousemove",
      clientX: 350,
      buttons: 0,
    });

    assert.strictEqual(claimsModule.getIsColumnResizing(), false);
    assert.strictEqual(guide.style.display, "none");
    assert.strictEqual(resizer.classList.contains("is-resizing"), false);
    assert.strictEqual(env.document.body.classList.contains("is-resizing-cols"), false);
  });

  await testAsync("sorting guard: sortable th click does not sort when resizing or just resized, observing th.classList.contains('sorted')", async () => {
    const { ths } = createMockClaimsTableDOM(env);
    if (claimsModule.resetSortingState) {
      claimsModule.resetSortingState();
    }
    claimsModule.initColumnResizing();
    claimsModule.setupEventListeners();
    claimsModule.renderSortIndicators();

    // Verify initial state: column 0 has .sorted, column 1 does not
    assert.strictEqual(ths[0].classList.contains("sorted"), true, "Column 0 should initially be sorted");
    assert.strictEqual(ths[1].classList.contains("sorted"), false, "Column 1 should not initially be sorted");

    const resizer = ths[1].querySelector(".col-resizer");
    resizer.dispatchEvent({
      type: "mousedown",
      button: 0,
      clientX: 200,
      preventDefault: () => {},
      stopPropagation: () => {},
    });

    assert.strictEqual(claimsModule.getIsColumnResizing(), true);

    // Click sortable th during resize -> sorting must be suppressed
    ths[1].dispatchEvent({ type: "click" });
    assert.strictEqual(ths[1].classList.contains("sorted"), false, "Column 1 must not gain .sorted while isColumnResizing is true");
    assert.strictEqual(ths[0].classList.contains("sorted"), true, "Column 0 must remain sorted");

    // Mouse up sets justResizedColumn = true
    env.window.dispatchEvent({ type: "mouseup" });
    assert.strictEqual(claimsModule.getJustResizedColumn(), true);

    // Click sortable th immediately after resize -> still suppressed by justResizedColumn guard
    ths[1].dispatchEvent({ type: "click" });
    assert.strictEqual(ths[1].classList.contains("sorted"), false, "Column 1 must not gain .sorted while justResizedColumn is true");
    assert.strictEqual(ths[0].classList.contains("sorted"), true, "Column 0 must remain sorted");

    // Wait for the 100ms cooldown to expire
    await new Promise((resolve) => setTimeout(resolve, 120));
    assert.strictEqual(claimsModule.getJustResizedColumn(), false);

    // Click sortable th after cooldown -> now sorts column 1!
    ths[1].dispatchEvent({ type: "click" });
    assert.strictEqual(ths[1].classList.contains("sorted"), true, "Column 1 must be sorted after cooldown expires");
    assert.strictEqual(ths[0].classList.contains("sorted"), false, "Column 0 must no longer be sorted");
  });
}

module.exports = runTests;

if (require.main === module) {
  runTests().then(() => {
    const stats = getTestStats();
    if (stats.failed > 0) {
      process.exit(1);
    }
  });
}

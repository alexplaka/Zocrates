/**
 * test_graph_algorithms.js — Tests for network visualizer math and community detection in graph.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Graph Visualizer Math & Community Detection ---");
  setupMockEnvironment();
  const graph = require("../../zotero-plugin/addon/graph.js");

  // 1. detectCommunities (Label Propagation Algorithm)
  test("detectCommunities: partitions disconnected components into distinct clusters", () => {
    // Two disconnected triangles: (1-2-3) and (4-5-6)
    const nodes = [
      { id: "1" }, { id: "2" }, { id: "3" },
      { id: "4" }, { id: "5" }, { id: "6" },
    ];
    const links = [
      { source: "1", target: "2", score: 0.9, relation_type: "human_curated" },
      { source: "2", target: "3", score: 0.9, relation_type: "human_curated" },
      { source: "3", target: "1", score: 0.9, relation_type: "human_curated" },
      { source: "4", target: "5", score: 0.9, relation_type: "human_curated" },
      { source: "5", target: "6", score: 0.9, relation_type: "human_curated" },
      { source: "6", target: "4", score: 0.9, relation_type: "human_curated" },
    ];

    graph.detectCommunities(nodes, links, 10);

    // Nodes 1, 2, 3 should all share the same community
    assert.strictEqual(nodes[0].community, nodes[1].community);
    assert.strictEqual(nodes[1].community, nodes[2].community);

    // Nodes 4, 5, 6 should all share the same community
    assert.strictEqual(nodes[3].community, nodes[4].community);
    assert.strictEqual(nodes[4].community, nodes[5].community);

    // The two triangles must have different communities
    assert.notStrictEqual(nodes[0].community, nodes[3].community);
  });

  // 2. seedMacroClusterLayout
  test("seedMacroClusterLayout: anchors center node at origin and assigns cluster/orphan coordinates", () => {
    const nodes = [
      { id: "c", is_center: true, degree: 5, community: 0 },
      { id: "m1", is_center: false, degree: 2, community: 0 },
      { id: "m2", is_center: false, degree: 2, community: 0 },
      { id: "orphan1", is_center: false, degree: 0, community: 1 },
    ];

    graph.seedMacroClusterLayout(nodes, true);

    // Center node anchored at 0, 0 with pinned = true
    const center = nodes.find((n) => n.id === "c");
    assert.strictEqual(center.x, 0);
    assert.strictEqual(center.y, 0);
    assert.strictEqual(center.pinned, true);

    // Community cluster members get valid numeric coordinates
    const m1 = nodes.find((n) => n.id === "m1");
    assert.ok(typeof m1.x === "number" && !isNaN(m1.x));
    assert.ok(typeof m1.y === "number" && !isNaN(m1.y));
    assert.ok(typeof m1.anchorX === "number");

    // Orphan node placed on outer ring
    const orphan = nodes.find((n) => n.id === "orphan1");
    assert.ok(typeof orphan.x === "number" && !isNaN(orphan.x));
    assert.ok(typeof orphan.y === "number" && !isNaN(orphan.y));
    const distFromOrigin = Math.sqrt(orphan.x * orphan.x + orphan.y * orphan.y);
    assert.ok(distFromOrigin > 200, "Orphan must be placed on outer concentric ring");
  });

  // 3. getLinkWeight
  test("getLinkWeight: computes relation-weighted score multipliers", () => {
    assert.strictEqual(graph.getLinkWeight({ relation_type: "both", score: 0.8 }), 4.0); // 5.0 * 0.8
    assert.strictEqual(graph.getLinkWeight({ relation_type: "curated", score: 0.5 }), 2.0); // 4.0 * 0.5
    assert.strictEqual(graph.getLinkWeight({ relation_type: "hybrid", score: 0.5 }), 1.0); // 2.0 * 0.5
    assert.strictEqual(graph.getLinkWeight({ relation_type: "tags", score: 1.0 }), 1.0); // 1.0 * 1.0
    // Default score 1.0
    assert.strictEqual(graph.getLinkWeight({ relation_type: "human_curated_and_hybrid_search" }), 5.0);
  });

  // 4. getNodeRadius
  test("getNodeRadius: scales radius monotonically with degree between 7 and 24", () => {
    assert.strictEqual(graph.getNodeRadius(0), 7);
    assert.strictEqual(graph.getNodeRadius(1), 9);
    assert.strictEqual(graph.getNodeRadius(5), 17);
    assert.strictEqual(graph.getNodeRadius(10), 24); // capped at 24
    assert.strictEqual(graph.getNodeRadius(50), 24); // capped at 24
  });

  // 5. getEdgeThickness
  test("getEdgeThickness: scales line width between 1.5 and 5.0 based on score", () => {
    assert.strictEqual(graph.getEdgeThickness(0.5), 1.5);
    assert.strictEqual(graph.getEdgeThickness(0.2), 1.5); // clamped min at 0.5
    assert.strictEqual(graph.getEdgeThickness(0.75), 3.25);
    assert.strictEqual(graph.getEdgeThickness(1.0), 5.0);
  });

  // 6. getEdgeColor
  test("getEdgeColor: maps relation types to theme colors", () => {
    const cBoth = graph.getEdgeColor("human_curated_and_hybrid_search");
    const cCurated = graph.getEdgeColor("human_curated");
    const cHybrid = graph.getEdgeColor("hybrid_search");
    const cTags = graph.getEdgeColor("shared_tags");

    assert.ok(cBoth.startsWith("#"));
    assert.ok(cCurated.startsWith("#"));
    assert.ok(cHybrid.startsWith("#"));
    assert.ok(cTags.startsWith("#"));
  });

  // 7. screenToWorld & worldToScreen
  test("screenToWorld & worldToScreen: provides reversible coordinate transformations", () => {
    if (typeof graph._setPanZoom === "function") {
      graph._setPanZoom(150, 200, 1.5);
    }

    const screenX = 300;
    const screenY = 450;
    const world = graph.screenToWorld(screenX, screenY);
    const roundTrip = graph.worldToScreen(world.x, world.y);

    assert.ok(Math.abs(roundTrip.x - screenX) < 1e-6);
    assert.ok(Math.abs(roundTrip.y - screenY) < 1e-6);
  });

  // 8. findNodeAt
  test("findNodeAt: detects node within radius + 4 bounding circle", () => {
    const testNodes = [
      { id: "nodeA", x: 100, y: 100, radius: 10 },
      { id: "nodeB", x: 300, y: 300, radius: 15 },
    ];
    if (typeof graph._setActiveNodes === "function") {
      graph._setActiveNodes(testNodes);
    }

    // Direct hit on nodeA
    assert.strictEqual(graph.findNodeAt(100, 100)?.id, "nodeA");

    // Hit inside tolerance (distance = 12 <= 14)
    assert.strictEqual(graph.findNodeAt(110, 100)?.id, "nodeA");

    // Outside tolerance of nodeA (distance = 25 > 14)
    assert.strictEqual(graph.findNodeAt(125, 100), null);

    // Direct hit on nodeB
    assert.strictEqual(graph.findNodeAt(305, 305)?.id, "nodeB");
  });

  // 9. getShortCitation
  test("getShortCitation: formats 1-author, 2-author, multi-author et al., and years", () => {
    // Single author
    assert.strictEqual(
      graph.getShortCitation({ authors: ["Alice Smith"], year: 2020 }),
      "Smith (2020)"
    );

    // Two authors
    assert.strictEqual(
      graph.getShortCitation({ authors: ["Alice Smith", "Bob Jones"], year: "2021" }),
      "Smith & Jones (2021)"
    );

    // Three+ authors (et al.)
    assert.strictEqual(
      graph.getShortCitation({ authors: ["Alice Smith", "Bob Jones", "Charlie Day"], year: 2022 }),
      "Smith et al. (2022)"
    );

    // Missing year
    assert.strictEqual(
      graph.getShortCitation({ authors: ["Alice Smith", "Bob Jones"] }),
      "Smith & Jones"
    );

    // Missing authors
    assert.strictEqual(
      graph.getShortCitation({ year: 2024 }),
      "(2024)"
    );
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

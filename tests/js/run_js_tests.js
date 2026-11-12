/**
 * run_js_tests.js — Aggregated test runner orchestrating all front-end test modules.
 */

const { getTestStats, resetTestStats, printTestSummary } = require("./test_helpers.js");

const testSuites = [
  { name: "ScholarImport", runner: require("./test_scholar_import.js") },
  { name: "AutoConverter", runner: require("./test_auto_converter.js") },
  { name: "BatchConverter", runner: require("./test_batch_converter.js") },
  { name: "InfoModal", runner: require("./test_info_modal.js") },
  { name: "ItemPane", runner: require("./test_item_pane.js") },
  { name: "BootstrapHubs", runner: require("./test_bootstrap_hubs.js") },
  { name: "PinnedProvisioning", runner: require("./test_pinned_provisioning.js") },
  { name: "WheelCleanup", runner: require("./test_wheel_cleanup.js") },
  { name: "OnboardingWizard", runner: require("./test_onboarding_wizard.js") },
  { name: "PortParity", runner: require("./test_port_parity.js") },
  { name: "SidebarDiscovery", runner: require("./test_sidebar_discovery.js") },
  { name: "SidebarCitations", runner: require("./test_sidebar_citations.js") },
  { name: "SidebarContextChips", runner: require("./test_sidebar_context_chips.js") },
  { name: "SidebarHitlAndSteps", runner: require("./test_sidebar_hitl_and_steps.js") },
  { name: "SidebarMermaid", runner: require("./test_sidebar_mermaid.js") },
  { name: "SidebarUtilities", runner: require("./test_sidebar_utilities.js") },
  { name: "SidebarGhostInput", runner: require("./test_sidebar_ghost_input.js") },
  { name: "GraphAlgorithms", runner: require("./test_graph_algorithms.js") },
  { name: "ReaderContext", runner: require("./test_reader_context.js") },
  { name: "XssSanitization", runner: require("./test_xss_sanitization.js") },
  { name: "Preferences", runner: require("./test_preferences.js") },
  { name: "ClaimsColumnResizing", runner: require("./test_claims_column_resizing.js") },
];

async function runAllSuites() {
  console.log("========================================================");
  console.log("Starting Zocrates Front-End JavaScript Test Suite (22 modules)");
  console.log("========================================================");

  resetTestStats();

  for (const suite of testSuites) {
    try {
      await suite.runner();
    } catch (err) {
      console.error(`\nSuite [${suite.name}] encountered fatal execution error:`, err);
      process.exitCode = 1;
    }
  }

  const allPassed = printTestSummary();
  if (!allPassed) {
    process.exit(1);
  }
}

runAllSuites().catch((err) => {
  console.error("Fatal runner failure:", err);
  process.exit(1);
});

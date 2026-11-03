/**
 * test_port_parity.js — Cross-language Python/JS deterministic port parity.
 *
 * Pins the Phase 3 port offset formula:
 *   8765 + (first 8 bytes of SHA-256(UTF-8(path)) as big-endian uint64 % 100)
 * against the same integer asserted by tests/unit/test_port_allocation.py.
 */

const assert = require("assert");
const crypto = require("crypto");
const { testAsync, getTestStats, printTestSummary } = require("./test_helpers.js");

const ZocratesPortOffset = require("../../zotero-plugin/addon/lib/portOffset.js");

const PINNED_VECTOR_DIR = "/home/alex/Zotero";
const PINNED_VECTOR_PORT = 8832;

async function runTests() {
  console.log("\n--- Suite: Port Parity (Python <-> JS deterministic offset) ---");

  await testAsync("PortOffset: pinned parity vector matches app/config.py (8832)", async () => {
    const port = await ZocratesPortOffset.computePortFromPath(PINNED_VECTOR_DIR);
    assert.strictEqual(port, PINNED_VECTOR_PORT);
  });

  await testAsync("PortOffset: independent Node SHA-256 recompute agrees", async () => {
    const digest = crypto.createHash("sha256").update(PINNED_VECTOR_DIR, "utf8").digest();
    const expected = 8765 + Number(digest.readBigUInt64BE(0) % 100n);
    assert.strictEqual(expected, PINNED_VECTOR_PORT);
    const port = await ZocratesPortOffset.computePortFromPath(PINNED_VECTOR_DIR);
    assert.strictEqual(port, expected);
  });

  await testAsync("PortOffset: formula holds for alternate path shapes", async () => {
    for (const dir of [
      "/home/alex/Zotero/zocrates-data",
      "/home/alex/Zotero/Zotero test-user/zocrates-data",
      "C:\\Users\\Researcher\\Zotero\\zocrates-data",
    ]) {
      const digest = crypto.createHash("sha256").update(dir.replace(/[\\/]+$/, ""), "utf8").digest();
      const expected = 8765 + Number(digest.readBigUInt64BE(0) % 100n);
      const port = await ZocratesPortOffset.computePortFromPath(dir);
      assert.strictEqual(port, expected, dir);
    }
  });

  await testAsync("PortOffset: zocrates-data join is separator-aware and trailing-slash normalized", () => {
    assert.strictEqual(
      ZocratesPortOffset.computeZocratesDataDir("/home/alex/Zotero"),
      "/home/alex/Zotero/zocrates-data"
    );
    assert.strictEqual(
      ZocratesPortOffset.computeZocratesDataDir("/home/alex/Zotero/"),
      "/home/alex/Zotero/zocrates-data"
    );
    assert.strictEqual(
      ZocratesPortOffset.computeZocratesDataDir("C:\\Users\\R\\Zotero\\"),
      "C:\\Users\\R\\Zotero\\zocrates-data"
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

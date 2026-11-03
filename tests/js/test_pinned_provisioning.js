/**
 * test_pinned_provisioning.js — Tests for the Phase 6 pinned-provisioning
 * wheel install argument helpers in bootstrap.js (buildWheelInstallArgs and
 * constraintsTargetToFileUrl).
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Pinned Provisioning (buildWheelInstallArgs) ---");
  setupMockEnvironment();
  const { buildWheelInstallArgs, constraintsTargetToFileUrl } = require("../../zotero-plugin/addon/bootstrap.js");

  const venvPython = "/data/zocrates-data/.venv/bin/python";
  const wheelInstallTarget = "/data/zocrates-data/zocrates-1.0.0-py3-none-any.whl";
  const constraintsTarget = "/data/zocrates-data/constraints.txt";
  const constraintsFileUrl = "file:///data/zocrates-data/constraints.txt";

  test("buildWheelInstallArgs: without constraintsTarget returns bare wheel install args (no --constraint)", () => {
    const args = buildWheelInstallArgs(venvPython, wheelInstallTarget);
    assert.deepStrictEqual(args, ["pip", "install", "--python", venvPython, wheelInstallTarget]);
  });

  test("buildWheelInstallArgs: with constraintsTarget appends --constraint <file:// URL>", () => {
    const args = buildWheelInstallArgs(venvPython, wheelInstallTarget, constraintsTarget);
    assert.deepStrictEqual(args, [
      "pip", "install", "--python", venvPython, wheelInstallTarget,
      "--constraint", constraintsFileUrl,
    ]);
  });

  test("buildWheelInstallArgs: null constraintsTarget treated as absent", () => {
    const args = buildWheelInstallArgs(venvPython, wheelInstallTarget, null);
    assert.deepStrictEqual(args, ["pip", "install", "--python", venvPython, wheelInstallTarget]);
  });

  test("buildWheelInstallArgs: undefined constraintsTarget treated as absent", () => {
    const args = buildWheelInstallArgs(venvPython, wheelInstallTarget, undefined);
    assert.deepStrictEqual(args, ["pip", "install", "--python", venvPython, wheelInstallTarget]);
  });

  test("buildWheelInstallArgs: empty-string constraintsTarget treated as absent (falsy fallback)", () => {
    const args = buildWheelInstallArgs(venvPython, wheelInstallTarget, "");
    assert.deepStrictEqual(args, ["pip", "install", "--python", venvPython, wheelInstallTarget]);
  });

  test("buildWheelInstallArgs: pure function — does not mutate inputs across calls", () => {
    const baseline = ["pip", "install", "--python", venvPython, wheelInstallTarget];
    const withConstraint = buildWheelInstallArgs(venvPython, wheelInstallTarget, constraintsTarget);
    const withoutConstraint = buildWheelInstallArgs(venvPython, wheelInstallTarget, "");
    assert.deepStrictEqual(withoutConstraint, baseline);
    assert.deepStrictEqual(withConstraint, baseline.concat(["--constraint", constraintsFileUrl]));
    assert.notDeepStrictEqual(withConstraint, withoutConstraint);
  });

  console.log("\n--- Suite: Pinned Provisioning (constraintsTargetToFileUrl) ---");

  test("constraintsTargetToFileUrl: space-free POSIX path round-trips with file:// prefix", () => {
    assert.strictEqual(
      constraintsTargetToFileUrl("/data/zocrates-data/constraints.txt"),
      "file:///data/zocrates-data/constraints.txt",
    );
  });

  test("constraintsTargetToFileUrl: path with space percent-encodes as %20 (Zotero prod-test profile)", () => {
    assert.strictEqual(
      constraintsTargetToFileUrl("/home/alex/Zotero prod-test/zocrates-data/constraints.txt"),
      "file:///home/alex/Zotero%20prod-test/zocrates-data/constraints.txt",
    );
  });

  test("constraintsTargetToFileUrl: Windows backslash path normalized to forward slashes with drive prefix", () => {
    assert.strictEqual(
      constraintsTargetToFileUrl("C:\\Users\\Al Ex\\zocrates-data\\constraints.txt"),
      "file:///C:/Users/Al%20Ex/zocrates-data/constraints.txt",
    );
  });

  test("constraintsTargetToFileUrl: '#' and '?' are explicitly encoded as %23 / %3F", () => {
    assert.strictEqual(
      constraintsTargetToFileUrl("/data/zoc rates#frag/q?ery/constraints.txt"),
      "file:///data/zoc%20rates%23frag/q%3Fery/constraints.txt",
    );
  });

  test("constraintsTargetToFileUrl: pure function — does not mutate input across calls", () => {
    const input = "/data/zoc rates/constraints.txt";
    const first = constraintsTargetToFileUrl(input);
    const second = constraintsTargetToFileUrl(input);
    assert.strictEqual(first, "file:///data/zoc%20rates/constraints.txt");
    assert.strictEqual(first, second);
    assert.strictEqual(input, "/data/zoc rates/constraints.txt");
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;
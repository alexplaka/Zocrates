/**
 * test_sidebar_hitl_and_steps.js — Tests for HITL checkpoint card and ReAct steps in sidebar.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, createMockElement, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: Sidebar HITL Checkpoints & ReAct Steps ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. renderHitlCheckpointCard
  test("renderHitlCheckpointCard: renders checkpoint card with summary, gaps, guidance, and grant button", () => {
    const container = createMockElement("div");
    let resumeAction = null;

    const hitlData = {
      payload: {
        extension_number: 1,
        max_extensions: 2,
        covered_summary: "Covered foundational LLM latency analyses.",
        missing_aspects: [
          "Speculative decoding draft head benchmarks",
          "Edge device quantization memory constraints",
        ],
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, (action) => {
      resumeAction = action;
    });

    assert.ok(card, "Card element should be created");
    assert.strictEqual(container.querySelector(".hitl-checkpoint-card"), card);

    // Title
    assert.strictEqual(
      card.querySelector(".hitl-header-title").textContent,
      "Deep Research Checkpoint (Extension 1 of 2)"
    );

    // Summary
    assert.strictEqual(
      card.querySelector(".hitl-summary-content").textContent,
      "Covered foundational LLM latency analyses."
    );

    // Gaps list
    const gaps = card.querySelectorAll(".hitl-gaps-list li");
    assert.strictEqual(gaps.length, 2);
    assert.strictEqual(gaps[0].textContent, "Speculative decoding draft head benchmarks");
    assert.strictEqual(gaps[1].textContent, "Edge device quantization memory constraints");

    // Guidance input & Grant extension click (+5 calls / +3750 words / +5 rerank chunks)
    const guidanceInput = card.querySelector(".hitl-guidance-input");
    guidanceInput.value = "Focus on Section 5 GPU numbers";

    const grantBtn = card.querySelector(".hitl-btn-grant");
    assert.ok(grantBtn);
    grantBtn.dispatchEvent("click");

    assert.ok(resumeAction, "onResumeCallback should be called");
    assert.strictEqual(resumeAction.action, "extend_budget");
    assert.strictEqual(resumeAction.guidance, "Focus on Section 5 GPU numbers");
    assert.strictEqual(grantBtn.disabled, true);
  });

  test("renderHitlCheckpointCard: early synthesis button triggers synthesize action callback", () => {
    const container = createMockElement("div");
    let resumeAction = null;

    const hitlData = {
      payload: {
        extension_number: 2,
        max_extensions: 2,
        covered_summary: "Adequate coverage gathered.",
        missing_aspects: [],
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, (action) => {
      resumeAction = action;
    });

    const synthBtn = card.querySelector(".hitl-btn-synth");
    assert.ok(synthBtn);
    synthBtn.dispatchEvent("click");

    assert.ok(resumeAction);
    assert.strictEqual(resumeAction.action, "synthesize");
    assert.strictEqual(synthBtn.disabled, true);
  });

  test("renderHitlCheckpointCard: renders trigger reason for agent_request with justification truncation", () => {
    const container = createMockElement("div");
    const longJustification = "A".repeat(250);
    const hitlData = {
      payload: {
        trigger_reason: "agent_request",
        agent_justification: longJustification,
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, () => {});
    const triggerEl = card.querySelector(".hitl-trigger-reason");
    assert.ok(triggerEl, "Trigger reason container should be rendered");

    const titleEl = triggerEl.querySelector(".hitl-trigger-title");
    assert.strictEqual(titleEl.textContent, "🤖 Requested by search agent");

    const justEl = triggerEl.querySelector(".hitl-trigger-justification");
    assert.ok(justEl, "Justification element should be rendered");
    assert.strictEqual(justEl.textContent, "A".repeat(200) + "...");
  });

  test("renderHitlCheckpointCard: renders trigger reason for tool_budget_exhausted with formatted calls", () => {
    const container = createMockElement("div");
    const hitlData = {
      payload: {
        trigger_reason: "tool_budget_exhausted",
        trigger_details: {
          tool_calls_used: 10,
          tool_budget: 10,
        },
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, () => {});
    const triggerEl = card.querySelector(".hitl-trigger-reason");
    assert.ok(triggerEl);
    const titleEl = triggerEl.querySelector(".hitl-trigger-title");
    assert.strictEqual(titleEl.textContent, "🛠 Retrieval tool budget exhausted (10/10 calls used)");
    assert.strictEqual(triggerEl.querySelector(".hitl-trigger-justification"), null);
  });

  test("renderHitlCheckpointCard: renders trigger reason for word_ceiling_approached with formatted words", () => {
    const container = createMockElement("div");
    const hitlData = {
      payload: {
        trigger_reason: "word_ceiling_approached",
        trigger_details: {
          words_used: 6200,
          word_ceiling: 7500,
        },
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, () => {});
    const triggerEl = card.querySelector(".hitl-trigger-reason");
    assert.ok(triggerEl);
    const titleEl = triggerEl.querySelector(".hitl-trigger-title");
    assert.strictEqual(
      titleEl.textContent,
      `📄 Evidence volume approaching limit (~${(6200).toLocaleString()}/${(7500).toLocaleString()} words)`
    );
  });

  test("renderHitlCheckpointCard: omits trigger reason when not present in payload", () => {
    const container = createMockElement("div");
    const hitlData = {
      payload: {
        extension_number: 1,
      },
    };

    const card = sidebar.renderHitlCheckpointCard(container, hitlData, () => {});
    assert.strictEqual(card.querySelector(".hitl-trigger-reason"), null);
  });

  // 2. ReAct intermediate steps
  test("intermediate steps: lifecycle (append -> update progress -> finalize)", () => {
    const chatMessages = env.document.getElementById("chat-messages");

    const initialSteps = [
      { type: "thought", author: "Search Agent", text: "Planning retrieval queries..." },
      { type: "tool_call", author: "Search Agent", name: "search_corpus" },
    ];

    // 1. Append bubble (streaming)
    const bubble = sidebar.appendIntermediateStepsBubbleDOM(initialSteps, { query: "test" }, true);
    assert.ok(bubble);
    assert.ok(bubble.classList.contains("intermediate-steps-bubble"));
    assert.strictEqual(bubble._isStreaming, true);

    const iconSpan = bubble.querySelector(".intermediate-steps-icon");
    assert.ok(iconSpan.classList.contains("intermediate-steps-spinner"));
    assert.strictEqual(bubble.querySelector(".intermediate-steps-label").textContent, "Searching library & synthesizing evidence...");

    // 2. Update progress
    const updatedSteps = [
      ...initialSteps,
      { type: "tool_return", name: "search_corpus" },
      { type: "thought", author: "Search Agent", text: "Reading retrieved chunks..." },
    ];
    sidebar.updateIntermediateStepsBubbleProgress(bubble, updatedSteps, "Reranking retrieved evidence (step 3 of 5)...");
    assert.strictEqual(bubble.querySelector(".intermediate-steps-label").textContent, "Reranking retrieved evidence (step 3 of 5)...");

    // 3. Finalize bubble
    sidebar.finalizeIntermediateStepsBubble(bubble, updatedSteps, { query: "test", finished: true });
    assert.strictEqual(bubble._isStreaming, false);
    assert.ok(bubble.classList.contains("collapsed"));
    assert.strictEqual(bubble.querySelector(".intermediate-steps-icon").textContent, "✓");
    assert.ok(!bubble.querySelector(".intermediate-steps-icon").classList.contains("intermediate-steps-spinner"));
    assert.ok(bubble.querySelector(".intermediate-steps-label").textContent.includes("Execution trace (4 steps, 1 tool call)"));
    assert.ok(!bubble.querySelector(".intermediate-steps-trace-btn").classList.contains("hidden"));
  });

  test("intermediate steps: truncates thought and agent_text trace items to 100 characters", () => {
    const longThought = "A".repeat(150);
    const longAgentText = "B".repeat(150);

    const steps = [
      { type: "thought", author: "Search Agent", text: longThought },
      { type: "agent_text", author: "Synthesizer", text: longAgentText },
    ];

    const bubble = sidebar.appendIntermediateStepsBubbleDOM(steps, {}, false);
    const itemDescs = bubble.querySelectorAll(".intermediate-step-item-desc");
    assert.strictEqual(itemDescs.length, 2);

    // Thought snippet is 100 chars followed by "..."
    const expectedThought = `Search Agent: ${"A".repeat(100)}...`;
    assert.strictEqual(itemDescs[0].textContent, expectedThought);

    // Agent text snippet is 100 chars followed by "..."
    const expectedAgentText = `Synthesizer: ${"B".repeat(100)}...`;
    assert.strictEqual(itemDescs[1].textContent, expectedAgentText);
  });
}

if (require.main === module) {
  runTests().then(() => {
    printTestSummary();
    if (getTestStats().failed > 0) process.exit(1);
  });
}

module.exports = runTests;

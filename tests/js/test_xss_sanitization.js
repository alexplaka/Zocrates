/**
 * test_xss_sanitization.js — Rigorous security and sanitization tests for Gecko SystemPrincipal
 * Privilege Escalation and Stored XSS prevention in sidebar.js.
 */

const assert = require("assert");
const { test, getTestStats, printTestSummary, setupMockEnvironment } = require("./test_helpers.js");

async function runTests() {
  console.log("\n--- Suite: XSS Sanitization & Gecko Privilege Escalation ---");
  const env = setupMockEnvironment();
  const sidebar = require("../../zotero-plugin/addon/sidebar.js");

  // 1. URL Safety Validation (isSafeUrl & isSafeLinkUrl)
  test("isSafeUrl: allows safe protocols, relative paths, and safe image data URIs", () => {
    assert.strictEqual(sidebar.isSafeUrl("https://example.com/paper.pdf"), true);
    assert.strictEqual(sidebar.isSafeUrl("http://127.0.0.1:8765/assets/K1/images/f1.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("mailto:scholar@example.edu"), true);
    assert.strictEqual(sidebar.isSafeUrl("zocrates-img://ABC12345/images/figure1.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("/assets/local.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("./images/figure1.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("../images/figure1.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("images/figure1.png"), true);
    assert.strictEqual(sidebar.isSafeUrl("#heading-1"), true);
    assert.strictEqual(sidebar.isSafeUrl("data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="), true);
    assert.strictEqual(sidebar.isSafeUrl("data:image/jpeg;base64,/9j/4AAQSkZJRg=="), true);
    assert.strictEqual(sidebar.isSafeUrl("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="), true);
  });

  test("isSafeLinkUrl: strictly restricts <a href> to http, https, mailto, and hash anchors", () => {
    assert.strictEqual(sidebar.isSafeLinkUrl("https://example.com/paper.pdf"), true);
    assert.strictEqual(sidebar.isSafeLinkUrl("http://example.com/"), true);
    assert.strictEqual(sidebar.isSafeLinkUrl("mailto:user@example.com"), true);
    assert.strictEqual(sidebar.isSafeLinkUrl("#section-2"), true);

    // Disallowed for general navigation links
    assert.strictEqual(sidebar.isSafeLinkUrl("data:image/png;base64,iVBORw=="), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("file:///etc/passwd"), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("blob:http://example.com/123"), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("javascript:alert(1)"), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("/relative/path"), false);
    assert.strictEqual(sidebar.isSafeLinkUrl("images/fig1.png"), false);
  });

  test("isSafeUrl: blocks dangerous schemes, non-image data URIs, and control characters", () => {
    assert.strictEqual(sidebar.isSafeUrl("javascript:alert(1)"), false);
    assert.strictEqual(sidebar.isSafeUrl("javascript:alert(document.domain)"), false);
    assert.strictEqual(sidebar.isSafeUrl("JAVASCRIPT:alert(1)"), false);
    assert.strictEqual(sidebar.isSafeUrl("  javascript:alert(1)  "), false);
    assert.strictEqual(sidebar.isSafeUrl("java\tscript:alert(1)"), false);
    assert.strictEqual(sidebar.isSafeUrl("java\0script:alert(1)"), false);
    assert.strictEqual(sidebar.isSafeUrl("vbscript:msgbox(1)"), false);
    assert.strictEqual(sidebar.isSafeUrl("data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="), false);
    assert.strictEqual(sidebar.isSafeUrl("data:application/javascript;base64,YWxlcnQoMSk="), false);
    assert.strictEqual(sidebar.isSafeUrl("data:image/svg+xml,<svg onload=alert(1)>"), false); // Non-base64 SVG data URI
    assert.strictEqual(sidebar.isSafeUrl("file:///etc/passwd"), false);
    assert.strictEqual(sidebar.isSafeUrl("blob:http://example.com/uuid"), false);
    assert.strictEqual(sidebar.isSafeUrl(""), false);
    assert.strictEqual(sidebar.isSafeUrl(null), false);
    assert.strictEqual(sidebar.isSafeUrl(undefined), false);
  });

  // 2. DOM Sanitization (sanitizeHTML)
  test("sanitizeHTML: removes <script> and dangerous tags completely with children", () => {
    const input = '<div>Safe text<script>alert(1)</script><script src="http://evil.com/x.js"></script></div>';
    const clean = sidebar.sanitizeHTML(input);
    assert.ok(!clean.includes("<script"), "Must not contain <script");
    assert.ok(!clean.includes("alert(1)"), "Must not contain script body");
    assert.ok(!clean.includes("evil.com"), "Must not contain script src");
    assert.ok(clean.includes("Safe text"), "Must preserve safe sibling text");
  });

  test("sanitizeHTML: strips id attribute from HTML elements to prevent DOM/window clobbering", () => {
    const input = '<div id="chat-input"><span id="mermaid">Text</span></div>';
    const clean = sidebar.sanitizeHTML(input);
    assert.ok(!clean.includes('id="chat-input"'), "Must strip id from div");
    assert.ok(!clean.includes('id="mermaid"'), "Must strip id from span");
    assert.ok(clean.includes("Text"), "Must preserve inner text");
  });

  test("sanitizeHTML: allows task list checkboxes but strips interactive inputs", () => {
    const taskList = '<ul><li><input type="checkbox" disabled checked> Done item</li><li><input type="checkbox" disabled> Todo item</li></ul>';
    const cleanTask = sidebar.sanitizeHTML(taskList);
    assert.ok(cleanTask.includes('<input'), "Must preserve task-list checkbox");
    assert.ok(cleanTask.includes('type="checkbox"'), "Must preserve type=checkbox");
    assert.ok(cleanTask.includes('disabled'), "Must preserve disabled attribute");

    const textInput = '<form><input type="text" name="steal" value="secret"></form>';
    const cleanText = sidebar.sanitizeHTML(textInput);
    assert.ok(!cleanText.includes('<input'), "Must remove interactive text input");
    assert.ok(!cleanText.includes('<form'), "Must remove form element");
  });

  test("sanitizeHTML: strips <svg/onload=alert(1)> (attribute separator without whitespace)", () => {
    const payload = '<svg/onload=alert(1)>';
    const clean = sidebar.sanitizeHTML(payload);
    assert.ok(!clean.includes("onload"), "Must strip onload handler even without whitespace");
    assert.ok(!clean.includes("alert"), "Must strip alert expression");
  });

  test("sanitizeHTML: strips all on* event handler attributes", () => {
    const input = '<img src="valid.png" onerror="alert(1)" onclick="steal()" onmouseover="boom()" onLoad="run()">';
    const clean = sidebar.sanitizeHTML(input);
    assert.ok(!clean.includes("onerror"), "Must strip onerror");
    assert.ok(!clean.includes("onclick"), "Must strip onclick");
    assert.ok(!clean.includes("onmouseover"), "Must strip onmouseover");
    assert.ok(!clean.includes("onload"), "Must strip onload");
    assert.ok(!clean.includes("alert(1)"), "Must strip handler code");
    assert.ok(clean.includes('src="valid.png"'), "Must preserve valid src");
  });

  test("sanitizeHTML: removes javascript: and dangerous data: in href while preserving link text", () => {
    const jsLink = '<a href="javascript:alert(1)">Click Me</a>';
    const cleanJs = sidebar.sanitizeHTML(jsLink);
    assert.ok(!cleanJs.includes("javascript:"), "Must strip javascript: href");
    assert.ok(cleanJs.includes("Click Me"), "Must preserve link text");

    const dataLink = '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">Malicious Link</a>';
    const cleanData = sidebar.sanitizeHTML(dataLink);
    assert.ok(!cleanData.includes("data:text/html"), "Must strip dangerous data: href");
    assert.ok(cleanData.includes("Malicious Link"), "Must preserve link text");
  });

  test("sanitizeHTML: blocks dangerous expressions and javascript in style attributes", () => {
    const expr = '<div style="background: expression(alert(1)); color: red;">Test</div>';
    const cleanExpr = sidebar.sanitizeHTML(expr);
    assert.ok(!cleanExpr.includes("expression"), "Must strip style with expression(...)");

    const jsStyle = '<div style="background: url(javascript:alert(1)); font-size: 12px;">Test</div>';
    const cleanJs = sidebar.sanitizeHTML(jsStyle);
    assert.ok(!cleanJs.includes("javascript:"), "Must strip style with url(javascript:...)");

    const safeStyle = '<div style="color: blue; font-weight: bold;">Safe</div>';
    const cleanSafe = sidebar.sanitizeHTML(safeStyle);
    assert.ok(cleanSafe.includes("style="), "Must preserve safe styles");
    assert.ok(cleanSafe.includes("color: blue"), "Must preserve safe style declarations");
  });

  test("sanitizeHTML: unwraps children of non-allowed non-dangerous tags", () => {
    const input = '<custom-widget><p>Inner paragraph with <b>bold</b> text</p></custom-widget>';
    const clean = sidebar.sanitizeHTML(input);
    assert.ok(!clean.includes("<custom-widget"), "Must remove non-allowed tag");
    assert.ok(!clean.includes("</custom-widget>"), "Must remove non-allowed close tag");
    assert.ok(clean.includes("<p>Inner paragraph with <b>bold</b> text</p>"), "Must preserve children");
  });

  test("sanitizeHTML: preserves legitimate markdown, figures, MathML, and citation pills", () => {
    const fullContent = [
      '<h1>Header 1</h1>',
      '<p>Paragraph with <strong>bold</strong>, <em>italic</em>, and <code>inline code</code>.</p>',
      '<ul><li>List item 1</li><li>List item 2</li></ul>',
      '<table><thead><tr><th>Col 1</th><th>Col 2</th></tr></thead><tbody><tr><td>Data 1</td><td>Data 2</td></tr></tbody></table>',
      '<pre><code class="hljs python">def test(): pass</code></pre>',
      '<figure data-type="image"><img src="images/fig1.png" alt="Figure 1" /><synthetic_caption>Visual description</synthetic_caption></figure>',
      '<details open><summary>Summary</summary><p>Expanded text</p></details>',
      '<span class="citation-pill" data-citation-idx="1" data-citation-occ="0" title="Source: Test">1</span>',
      '<span class="katex"><math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow></math></span>',
    ].join("\n");

    const clean = sidebar.sanitizeHTML(fullContent);
    assert.ok(clean.includes("<h1>Header 1</h1>"), "Must preserve h1");
    assert.ok(clean.includes("<strong>bold</strong>"), "Must preserve strong");
    assert.ok(clean.includes("<em>italic</em>"), "Must preserve em");
    assert.ok(clean.includes("<code>inline code</code>"), "Must preserve code");
    assert.ok(clean.includes("<th>Col 1</th>"), "Must preserve table headers");
    assert.ok(clean.includes("<td>Data 1</td>"), "Must preserve table data");
    assert.ok(clean.includes("<figure data-type=\"image\">"), "Must preserve figure with data-type");
    assert.ok(clean.includes("<synthetic_caption>Visual description</synthetic_caption>"), "Must preserve synthetic_caption");
    assert.ok(clean.includes("<details open"), "Must preserve details with open attribute");
    assert.ok(clean.includes("class=\"citation-pill\""), "Must preserve citation pill class");
    assert.ok(clean.includes("data-citation-idx=\"1\""), "Must preserve data-citation-idx");
    assert.ok(clean.includes("<math"), "Must preserve MathML math element");
    assert.ok(clean.includes("<mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow>"), "Must preserve MathML content");
  });

  // 3. Markdown Image Alt Escaping
  test("renderer.image: escapes quotes and injected attributes in alt and title", () => {
    // Test formatMarkdown with an image having attribute-injection attempt in alt and title
    const md = '![img" onerror=alert(1)](images/fig.png "title\\" onmouseover=steal()")';
    const rendered = sidebar.formatMarkdown(md);

    // Quotes must be escaped as &quot; so they cannot break out of the alt/title attributes
    assert.ok(!rendered.includes('alt="img" onerror='), "Quote must not break out of alt attribute");
    assert.ok(!rendered.includes('title="title" onmouseover='), "Quote must not break out of title attribute");
    assert.ok(rendered.includes('alt="img&quot; onerror=alert(1)"'), "Alt text quotes must be HTML escaped");
    assert.ok(rendered.includes('title="title&quot; onmouseover=steal()"'), "Title text quotes must be HTML escaped");

    // When parsed into DOM, img must have no onerror or onmouseover attributes
    const parser = new DOMParser();
    const doc = parser.parseFromString(rendered, "text/html");
    const img = doc.querySelector("img");
    assert.ok(img, "Must render an <img> tag");
    assert.strictEqual(img.getAttribute("onerror"), null, "img must not have an onerror attribute");
    assert.strictEqual(img.getAttribute("onmouseover"), null, "img must not have an onmouseover attribute");
    assert.strictEqual(img.getAttribute("alt"), 'img" onerror=alert(1)', "Alt text should preserve original content");
  });

  test("renderer.image: blocks javascript: href in markdown images", () => {
    const md = '![malicious image](javascript:alert(1))';
    const rendered = sidebar.formatMarkdown(md);

    assert.ok(!rendered.includes('src="javascript:alert(1)"'), "Must not emit javascript: src in img");
    assert.ok(!rendered.includes('src="http://127.0.0.1:8765/assets/javascript:alert(1)"'), "Must not resolve javascript: URI to asset");
  });

  // 4. KaTeX Error Fallback Escaping
  test("formatMarkdown: KaTeX parse error gracefully falls back with escaped HTML", () => {
    // An invalid LaTeX expression containing embedded HTML/scripts
    const malformedMath = '$\\frac{1}{0 <script>alert(1)</script>}$';
    const rendered = sidebar.formatMarkdown(malformedMath);

    assert.ok(!rendered.includes("<script"), "Fallback math must not contain unescaped <script");
    assert.ok(!rendered.includes("</script>"), "Fallback math must not contain unescaped </script>");
    assert.ok(rendered.includes("&lt;script&gt;alert(1)&lt;/script&gt;"), "Script tag in formula must be HTML escaped");
  });

  // 5. Mermaid SVG Sanitization
  test("sanitizeMermaidSvg: strips <script> tags and on* handlers while preserving diagram structure", () => {
    const dangerousSvg = [
      '<svg id="zocrates-mermaid-1" class="mermaid" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">',
      '  <style>.node { fill: #2563eb; } .edgePath { stroke: #64748b; }</style>',
      '  <defs><marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b" /></marker></defs>',
      '  <g class="nodes" onload="alert(1)">',
      '    <g class="node" transform="translate(50, 50)" onclick="stealCookie()">',
      '      <rect x="-40" y="-20" width="80" height="40" rx="5" ry="5" />',
      '      <text x="0" y="0" text-anchor="middle">Input Node</text>',
      '    </g>',
      '  </g>',
      '  <script>alert("pwned")</script>',
      '  <animate attributeName="href" values="javascript:alert(3)" />',
      '  <set attributeName="onmouseover" to="alert(4)" />',
      '  <a xlink:href="javascript:alert(2)"><text x="10" y="10">Link</text></a>',
      '</svg>',
    ].join("\n");

    const cleanSvg = sidebar.sanitizeMermaidSvg(dangerousSvg);

    // Security assertions
    assert.ok(!cleanSvg.includes("<script"), "Must completely remove <script> elements");
    assert.ok(!cleanSvg.includes("alert(\"pwned\")"), "Must remove script payload");
    assert.ok(!cleanSvg.includes("onload="), "Must strip onload attribute from <g>");
    assert.ok(!cleanSvg.includes("onclick="), "Must strip onclick attribute from <g>");
    assert.ok(!cleanSvg.includes("stealCookie"), "Must remove onclick payload");
    assert.ok(!cleanSvg.includes("javascript:alert(2)"), "Must strip javascript: from xlink:href");
    assert.ok(!cleanSvg.includes("<animate"), "Must remove SMIL <animate> elements");
    assert.ok(!cleanSvg.includes("<set"), "Must remove SMIL <set> elements");

    // Malicious style inside SVG
    const svgWithBadStyle = '<svg><style>@import "http://evil.com/x.css"; .x { color: red; }</style><rect width="10" height="10"/></svg>';
    const cleanBadStyle = sidebar.sanitizeMermaidSvg(svgWithBadStyle);
    assert.ok(!cleanBadStyle.includes("@import"), "Must strip style with @import");
    assert.ok(cleanBadStyle.includes("<rect"), "Must preserve safe elements");

    // Mermaid integrity assertions
    assert.ok(cleanSvg.includes("<style>"), "Must preserve Mermaid <style> block");
    assert.ok(cleanSvg.includes(".node { fill: #2563eb; }"), "Must preserve Mermaid CSS styling");
    assert.ok(cleanSvg.includes("<defs>"), "Must preserve <defs>");
    assert.ok(cleanSvg.includes("<marker"), "Must preserve <marker>");
    assert.ok(cleanSvg.includes("<path"), "Must preserve marker path");
    assert.ok(cleanSvg.includes("<rect"), "Must preserve node rect");
    assert.ok(cleanSvg.includes("Input Node"), "Must preserve node text content");
    assert.ok(cleanSvg.includes("viewBox=\"0 0 100 100\""), "Must preserve viewBox");
  });
}

module.exports = runTests;

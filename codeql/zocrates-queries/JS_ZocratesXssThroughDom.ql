/**
 * @name DOM text reinterpreted as HTML (Zocrates sanitizer-aware)
 * @description Reinterpreting text from the DOM as HTML can lead to a cross-site
 *              scripting vulnerability. This query is a fork of the standard
 *              `js/xss-through-dom` query with one additional, narrowly-scoped
 *              barrier: arguments passed into Zocrates' own sanitization
 *              boundaries — `sanitizeHTMLForRendering` (allowlist HTML
 *              sanitizer composed with the XHTML namespace converter) and
 *              `sanitizeMermaidSvg` (SVG allowlist sanitizer). These functions
 *              parse untrusted content into inert DOMParser documents as their
 *              designed purpose, and every parse is followed by allowlist
 *              sanitization inside the same function before any result is
 *              rendered into a live document.
 *
 *              The stock query recognizes third-party sanitizers (DOMPurify,
 *              sanitize-html, he, validator, ...) and home-made sanitizers by
 *              name across call boundaries, but cannot model flows that end at
 *              a parse *inside* a custom sanitizer. This fork closes that gap
 *              for exactly two documented functions; any other unsanitized
 *              caller of `sanitizeHTML` or `htmlToXhtml` still produces alerts.
 * @kind path-problem
 * @problem.severity warning
 * @security-severity 7.8
 * @precision high
 * @id js/zocrates-xss-through-dom
 * @tags security
 *       external/cwe/cwe-079
 *       external/cwe/cwe-116
 */

import javascript
import semmle.javascript.security.dataflow.XssThroughDomCustomizations::XssThroughDom
import semmle.javascript.security.dataflow.DomBasedXssCustomizations
import semmle.javascript.security.dataflow.UnsafeJQueryPluginCustomizations::UnsafeJQueryPlugin as UnsafeJQuery
import semmle.javascript.security.dataflow.Xss::Shared as Shared

/**
 * Calls to Zocrates' own sanitization-boundary functions. Arguments passed to
 * these functions are handled by allowlist sanitization (inert DOMParser parse
 * + tag/attribute allowlisting) inside the callee.
 */
class ZocratesSanitizerBoundary extends DataFlow::Node {
  ZocratesSanitizerBoundary() {
    exists(DataFlow::CallNode call |
      call.getCalleeName() in ["sanitizeHTMLForRendering", "sanitizeMermaidSvg"] and
      this = call.getArgument(0)
    )
  }
}

module ZocratesXssThroughDomConfig implements DataFlow::ConfigSig {
  predicate isSource(DataFlow::Node source) { source instanceof Source }

  predicate isSink(DataFlow::Node sink) { sink instanceof DomBasedXss::Sink }

  predicate isBarrier(DataFlow::Node node) {
    node instanceof DomBasedXss::Sanitizer or
    DomBasedXss::isOptionallySanitizedNode(node) or
    node = DataFlow::MakeBarrierGuard<BarrierGuard>::getABarrierNode() or
    node = DataFlow::MakeBarrierGuard<UnsafeJQuery::BarrierGuard>::getABarrierNode() or
    node = Shared::BarrierGuard::getABarrierNode() or
    // Zocrates-specific sanitization boundary (see ZocratesSanitizerBoundary).
    node instanceof ZocratesSanitizerBoundary
  }

  predicate isAdditionalFlowStep(DataFlow::Node node1, DataFlow::Node node2) {
    node2 = DataFlow::globalVarRef("URL").getAMemberCall("createObjectURL") and
    node1 = node2.(DataFlow::InvokeNode).getArgument(0)
  }

  predicate observeDiffInformedIncrementalMode() { any() }
}

module ZocratesXssThroughDomFlow = TaintTracking::Global<ZocratesXssThroughDomConfig>;

import ZocratesXssThroughDomFlow::PathGraph

/**
 * Holds if the `source,sink` pair should not be reported.
 */
bindingset[source, sink]
predicate isIgnoredSourceSinkPair(Source source, DomBasedXss::Sink sink) {
  source.(DomPropertySource).getPropertyName() = "src" and
  sink instanceof DomBasedXss::WriteUrlSink
}

from ZocratesXssThroughDomFlow::PathNode source, ZocratesXssThroughDomFlow::PathNode sink
where
  ZocratesXssThroughDomFlow::flowPath(source, sink) and
  not isIgnoredSourceSinkPair(source.getNode(), sink.getNode())
select sink.getNode(), source, sink,
  "$@ is reinterpreted as HTML without escaping meta-characters.", source.getNode(), "DOM text"

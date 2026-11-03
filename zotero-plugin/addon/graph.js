// Pure Vanilla JS + Canvas 2D Force-Directed Network Graph Visualizer
    (function() {
      function getBackendUrl() {
        // Delegate to the single source of truth installed by bootstrap.js; keep the
        // previous inline resolution as a defensive fallback.
        try {
          const delegated = resolveZotero()?.Zocrates?.getBackendUrl?.();
          if (delegated) return delegated;
        } catch (e) {}
        try {
          const zot = resolveZotero();
          const port = zot?.Prefs?.get?.("extensions.zocrates.backendPort", true) || 8765;
          return `http://127.0.0.1:${port}`;
        } catch (e) {
          return "http://127.0.0.1:8765";
        }
      }
      const BACKEND_URL = getBackendUrl();

      function getAuthHeaders(extraHeaders = {}) {
        try {
          const zot = resolveZotero();
          if (zot?.Zocrates?.getAuthHeaders) {
            return zot.Zocrates.getAuthHeaders(extraHeaders);
          }
          const token = zot?.Zocrates?.getAuthToken?.() || zot?.Zocrates?._authToken;
          if (token) {
            return { ...extraHeaders, Authorization: "Bearer " + token };
          }
        } catch (e) {}
        return { ...extraHeaders };
      }

      // Theme Palette Definitions
      const THEME_COLORS = {
        "slate": {
          background: "#0f172a",
          nodeDefault: "#0284c7",
          nodeCenter: "#818cf8",
          nodePinned: "#e11d48",
          nodeBorder: "#f8fafc",
          textDefault: "#f8fafc",
          textMuted: "#94a3b8",
          edgeCurated: "#a855f7",
          edgeHybrid: "#38bdf8",
          edgeBoth: "#10b981",
          edgeTags: "#f59e0b",
          textShadow: "rgba(15, 23, 42, 0.95)"
        },
        "amoled": {
          background: "#000000",
          nodeDefault: "#0284c7",
          nodeCenter: "#818cf8",
          nodePinned: "#e11d48",
          nodeBorder: "#ffffff",
          textDefault: "#ffffff",
          textMuted: "#a1a1aa",
          edgeCurated: "#a855f7",
          edgeHybrid: "#38bdf8",
          edgeBoth: "#10b981",
          edgeTags: "#f59e0b",
          textShadow: "rgba(0, 0, 0, 0.95)"
        },
        "zotero-dark": {
          background: "#1e1e1e",
          nodeDefault: "#0284c7",
          nodeCenter: "#818cf8",
          nodePinned: "#e11d48",
          nodeBorder: "#e3e3e3",
          textDefault: "#e3e3e3",
          textMuted: "#aaaaaa",
          edgeCurated: "#a855f7",
          edgeHybrid: "#38bdf8",
          edgeBoth: "#10b981",
          edgeTags: "#f59e0b",
          textShadow: "rgba(30, 30, 30, 0.95)"
        },
        "zotero-light": {
          background: "#f9f9fb",
          nodeDefault: "#0284c7",
          nodeCenter: "#6366f1",
          nodePinned: "#dc2626",
          nodeBorder: "#ffffff",
          textDefault: "#1e293b",
          textMuted: "#64748b",
          edgeCurated: "#9333ea",
          edgeHybrid: "#0284c7",
          edgeBoth: "#059669",
          edgeTags: "#d97706",
          textShadow: "rgba(249, 249, 251, 0.95)"
        },
        "sepia": {
          background: "#f4ecd8",
          nodeDefault: "#c05c36",
          nodeCenter: "#7c3aed",
          nodePinned: "#b91c1c",
          nodeBorder: "#fdfbf7",
          textDefault: "#433422",
          textMuted: "#8c7050",
          edgeCurated: "#8b5cf6",
          edgeHybrid: "#0284c7",
          edgeBoth: "#059669",
          edgeTags: "#d97706",
          textShadow: "rgba(244, 236, 216, 0.95)"
        }
      };

      const COLORS = { ...THEME_COLORS["slate"] };
      let currentActiveTheme = "slate";

      function getActiveTheme() {
        const zot = resolveZotero();
        let prefTheme = null;
        if (zot?.Prefs) {
          try {
            prefTheme = zot.Prefs.get("zocrates.theme");
          } catch (_) {}
        }
        if (!prefTheme) {
          try {
            if (typeof localStorage !== "undefined") {
              prefTheme = localStorage.getItem("zocrates_theme");
            }
          } catch (_) {}
        }
        if (prefTheme && prefTheme !== "auto" && THEME_COLORS[prefTheme]) {
          return prefTheme;
        }

        // If auto or unset, check parent window or opener
        try {
          const parentTheme = window.parent?.document?.body?.getAttribute("data-theme");
          if (parentTheme && THEME_COLORS[parentTheme]) return parentTheme;
        } catch (_) {}
        try {
          const openerTheme = window.opener?.document?.body?.getAttribute("data-theme");
          if (openerTheme && THEME_COLORS[openerTheme]) return openerTheme;
        } catch (_) {}

        // Check Zotero extensions.zotero.theme pref
        let zoteroTheme = "system";
        if (zot?.Prefs) {
          try {
            zoteroTheme = zot.Prefs.get("extensions.zotero.theme") || "system";
          } catch (_) {}
        }
        if (zoteroTheme === "dark") return "zotero-dark";
        if (zoteroTheme === "light") return "zotero-light";

        const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches;
        return prefersDark ? "zotero-dark" : "zotero-light";
      }

      function applyTheme(theme) {
        const activeTheme = theme || getActiveTheme() || "slate";
        currentActiveTheme = activeTheme;
        document.body.setAttribute("data-theme", activeTheme);
        const palette = THEME_COLORS[activeTheme] || THEME_COLORS["slate"];
        Object.assign(COLORS, palette);
        requestRender();
      }

      function initThemeListeners() {
        applyTheme();

        const zot = resolveZotero();
        if (zot?.Prefs?.registerObserver) {
          try {
            const obs1 = zot.Prefs.registerObserver("zocrates.theme", () => applyTheme());
            const obs2 = zot.Prefs.registerObserver("extensions.zotero.theme", () => applyTheme());
            window.addEventListener("unload", () => {
              try {
                if (obs1) zot.Prefs.unregisterObserver(obs1);
                if (obs2) zot.Prefs.unregisterObserver(obs2);
              } catch (_) {}
            });
          } catch (e) {
            console.warn("Zocrates: Error registering theme observer in graph:", e);
          }
        }

        if (window.matchMedia) {
          try {
            const mq = window.matchMedia("(prefers-color-scheme: dark)");
            mq.addEventListener("change", () => applyTheme());
          } catch (_) {}
        }
      }

      // Community Color Palette
      const COMMUNITY_COLORS = [
        "#6366f1", "#06b6d4", "#10b981", "#f59e0b", "#ec4899",
        "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6", "#84cc16"
      ];

      function getCommunityColor(communityId) {
        if (communityId === undefined || communityId === null || communityId < 0) {
          return COLORS.nodeDefault;
        }
        return COMMUNITY_COLORS[communityId % COMMUNITY_COLORS.length];
      }

      // State
      let rawNodes = [];
      let rawLinks = [];
      let activeNodes = [];
      let activeLinks = [];
      let nodesMap = new Map();
      let hoveredNode = null;
      let selectedNode = null;
      let selectedClusterId = null;
      let draggedNode = null;
      let alpha = 1.0;
      let currentDepth = 2;
      let communityMeta = {};

      // Camera
      let panX = 0;
      let panY = 0;
      let zoom = 1.0;
      let isPanning = false;
      let panStartX = 0;
      let panStartY = 0;
      let hasDraggedSinceMouseDown = false;
      let animTarget = null;

      // Filter settings
      let minScore = 0.50;
      let includeCurated = true;
      let includeHybrid = true;
      let includeTags = true;
      let searchQuery = "";

      // DOM Elements
      const canvas = document.getElementById("graph-canvas");
      const ctx = canvas.getContext("2d");
      const loadingOverlay = document.getElementById("loading-overlay");
      const depthSlider = document.getElementById("depth-slider");
      const depthVal = document.getElementById("depth-val");
      const btnFitScreen = document.getElementById("btn-fit-screen");
      const btnToggleScope = document.getElementById("btn-toggle-scope");
      const searchInput = document.getElementById("search-input");
      const btnClearSearch = document.getElementById("btn-clear-search");
      const confidenceSlider = document.getElementById("confidence-slider");
      const minScoreVal = document.getElementById("min-score-val");
      const chkCurated = document.getElementById("chk-curated");
      const chkHybrid = document.getElementById("chk-hybrid");
      const chkTags = document.getElementById("chk-tags");
      const statsBadge = document.getElementById("stats-badge");
      const tooltip = document.getElementById("graph-tooltip");
      const tooltipTitle = document.getElementById("tooltip-title");
      const tooltipMeta = document.getElementById("tooltip-meta");
      const tooltipConnections = document.getElementById("tooltip-connections");
      const inspectorDrawer = document.getElementById("inspector-drawer");
      const btnCloseInspector = document.getElementById("btn-close-inspector");
      const inspectorTitle = document.getElementById("inspector-title");
      const inspectorClusterCard = document.getElementById("inspector-cluster-card");
      const inspectorClusterSection = document.getElementById("inspector-cluster-section");
      const inspectorYearVal = document.getElementById("inspector-year-val");
      const inspectorYearPill = document.getElementById("inspector-year-pill");
      const inspectorTypeVal = document.getElementById("inspector-type-val");
      const inspectorDegreeVal = document.getElementById("inspector-degree-val");
      const inspectorKeywords = document.getElementById("inspector-keywords");
      const inspectorBibSection = document.getElementById("inspector-bib-section");
      const inspectorAuthors = document.getElementById("inspector-authors");
      const inspectorPublication = document.getElementById("inspector-publication");
      const inspectorAbstractSection = document.getElementById("inspector-abstract-section");
      const inspectorAbstract = document.getElementById("inspector-abstract");
      const inspectorConnections = document.getElementById("inspector-connections");
      const inspectorConnCount = document.getElementById("inspector-conn-count");
      const btnSelectInZotero = document.getElementById("btn-select-in-zotero");

      let lastScopedParams = null;
      let isLockedToFullLibrary = false;
      window.isLockedToFullLibrary = false;
      window.isUserLockedToFullLibrary = false;

      function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
      }

      function resolveZotero() {
        if (typeof Zotero !== "undefined" && Zotero) return Zotero;
        try {
          if (window.parent && window.parent.Zotero) return window.parent.Zotero;
        } catch (e) {}
        try {
          if (window.top && window.top.Zotero) return window.top.Zotero;
        } catch (e) {}
        return null;
      }

      function selectItemInZotero(itemKey) {
        if (!itemKey) return;
        try {
          const zot = resolveZotero();
          if (!zot) {
            console.warn("Zocrates: Zotero object not found for selectItemInZotero");
            return;
          }

          const pane = (typeof zot.getActiveZoteroPane === "function" ? zot.getActiveZoteroPane() : null) ||
                       window.parent?.ZoteroPane ||
                       window.top?.ZoteroPane;

          let item = null;
          if (zot.Items && typeof zot.Items.getByLibraryAndKey === "function") {
            try {
              const userLibID = zot.Libraries?.userLibraryID;
              if (userLibID !== undefined) {
                item = zot.Items.getByLibraryAndKey(userLibID, itemKey);
              }
            } catch (_) {}
            if (!item && zot.Libraries?.getAll) {
              try {
                for (const lib of zot.Libraries.getAll()) {
                  const cand = zot.Items.getByLibraryAndKey(lib.id, itemKey);
                  if (cand) {
                    item = cand;
                    break;
                  }
                }
              } catch (_) {}
            }
          }

          const target = item ? item.id : itemKey;
          if (pane && typeof pane.selectItem === "function") {
            pane.selectItem(target, false, true);
          } else if (pane && typeof pane.selectItems === "function") {
            pane.selectItems([target]);
          }
        } catch (err) {
          console.warn("Zocrates: selectItemInZotero error:", err);
        }
      }

      // Responsive Canvas Setup
      function resizeCanvas() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * dpr;
        canvas.height = window.innerHeight * dpr;
        canvas.style.width = window.innerWidth + "px";
        canvas.style.height = window.innerHeight + "px";
        requestRender();
      }
      window.addEventListener("resize", resizeCanvas);

      // Coordinate Transformations
      function screenToWorld(sx, sy) {
        return {
          x: (sx - panX) / zoom,
          y: (sy - panY) / zoom
        };
      }

      function worldToScreen(wx, wy) {
        return {
          x: wx * zoom + panX,
          y: wy * zoom + panY
        };
      }

      function getEdgeColor(relType) {
        if (relType === "human_curated_and_hybrid_search") return COLORS.edgeBoth;
        if (relType === "human_curated") return COLORS.edgeCurated;
        if (relType === "hybrid_search") return COLORS.edgeHybrid;
        if (relType === "shared_tags") return COLORS.edgeTags;
        return COLORS.edgeHybrid;
      }

      function getEdgeThickness(score) {
        const clamped = Math.max(0.5, Math.min(1.0, score || 0.5));
        return 1.5 + ((clamped - 0.5) / 0.5) * 3.5;
      }

      function getNodeRadius(degree) {
        return Math.max(7, Math.min(24, 7 + 2 * (degree || 0)));
      }

      function getShortCitation(node) {
        if (!node) return "";
        let authors = node.authors || node.creators;
        if (typeof authors === "string") {
          try {
            const parsed = JSON.parse(authors);
            if (Array.isArray(parsed)) authors = parsed;
            else authors = [authors];
          } catch (_) {
            authors = authors.split(";").map(s => s.trim()).filter(Boolean);
          }
        }
        function extractAuthorLastName(raw) {
          if (!raw) return "";
          const first = String(raw).replace(/\s+et\s+al\.?$/i, "").trim();
          if (first.includes(",")) {
            return first.split(",")[0].trim();
          }
          const parts = first.split(/\s+/).filter(Boolean);
          if (parts.length > 1 && /^[A-Za-z]\.?$/.test(parts[parts.length - 1])) {
            return parts[0];
          }
          return parts[parts.length - 1] || first;
        }

        let authorStr = "";
        if (Array.isArray(authors) && authors.length > 0) {
          const rawFirst = String(authors[0] || "").trim();
          if (rawFirst) {
            const hadEtAl = /\bet\s+al\.?$/i.test(rawFirst);
            const firstLastName = extractAuthorLastName(rawFirst);
            if (firstLastName) {
              if (authors.length === 1) {
                authorStr = firstLastName + (hadEtAl ? " et al." : "");
              } else if (authors.length === 2 && !hadEtAl) {
                const secondLastName = extractAuthorLastName(authors[1]);
                authorStr = secondLastName ? `${firstLastName} & ${secondLastName}` : `${firstLastName} et al.`;
              } else {
                authorStr = `${firstLastName} et al.`;
              }
            }
          }
        }

        let yearStr = "";
        if (node.year !== undefined && node.year !== null) {
          const yStr = String(node.year).trim();
          if (yStr && yStr !== "N/A" && yStr !== "null" && yStr !== "undefined") {
            const match = yStr.match(/\b(19\d\d|20\d\d)\b/);
            yearStr = match ? match[1] : yStr;
          }
        }

        if (authorStr && yearStr) {
          return `${authorStr} (${yearStr})`;
        } else if (authorStr) {
          return authorStr;
        } else if (yearStr) {
          return `(${yearStr})`;
        }
        return "";
      }

      // Edge weight calculation for Weighted Label Propagation Algorithm
      // Edge weights: curated = 4.0, both = 5.0, hybrid = 2.0, tags = 1.0, multiplied by link.score
      function getLinkWeight(link) {
        let baseWeight = 2.0;
        const rel = link.relation_type || "";
        if (rel === "human_curated_and_hybrid_search" || rel === "both") {
          baseWeight = 5.0;
        } else if (rel === "human_curated" || rel === "curated") {
          baseWeight = 4.0;
        } else if (rel === "hybrid_search" || rel === "hybrid") {
          baseWeight = 2.0;
        } else if (rel === "shared_tags" || rel === "tags") {
          baseWeight = 1.0;
        }
        const score = (link.score !== undefined && link.score !== null) ? Number(link.score) : 1.0;
        return baseWeight * score;
      }

      // Community Detection via Weighted Label Propagation Algorithm (LPA)
      function detectCommunities(nodes, links, numIterations = 5) {
        if (!nodes || nodes.length === 0) return;

        // Adjacency list with edge weights
        const adj = new Map();
        nodes.forEach(n => adj.set(n.id, []));

        links.forEach(link => {
          const w = getLinkWeight(link);
          if (adj.has(link.source) && adj.has(link.target)) {
            adj.get(link.source).push({ target: link.target, weight: w });
            adj.get(link.target).push({ target: link.source, weight: w });
          }
        });

        // 1. Assign each node initial community ID
        const community = new Map();
        nodes.forEach((n, idx) => {
          community.set(n.id, idx);
        });

        // 2. Run iterations to converge into communities
        for (let iter = 0; iter < numIterations; iter++) {
          for (let i = 0; i < nodes.length; i++) {
            const node = nodes[i];
            const neighbors = adj.get(node.id) || [];
            if (neighbors.length === 0) continue;

            const weightByCommunity = new Map();
            for (let j = 0; j < neighbors.length; j++) {
              const edge = neighbors[j];
              const c = community.get(edge.target);
              weightByCommunity.set(c, (weightByCommunity.get(c) || 0) + edge.weight);
            }

            let bestComm = community.get(node.id);
            let maxWeight = -1;
            for (const [c, w] of weightByCommunity.entries()) {
              if (w > maxWeight) {
                maxWeight = w;
                bestComm = c;
              }
            }
            community.set(node.id, bestComm);
          }
        }

        // 3. Count cluster sizes and sort descending
        const clusterCounts = new Map();
        nodes.forEach(n => {
          const c = community.get(n.id);
          clusterCounts.set(c, (clusterCounts.get(c) || 0) + 1);
        });

        const sortedClusters = Array.from(clusterCounts.keys()).sort((a, b) => {
          return clusterCounts.get(b) - clusterCounts.get(a);
        });

        const commIdMap = new Map();
        sortedClusters.forEach((oldId, newId) => {
          commIdMap.set(oldId, newId);
        });

        nodes.forEach(n => {
          const oldC = community.get(n.id);
          const newC = commIdMap.get(oldC);
          n.community = newC;
          n.communitySize = clusterCounts.get(oldC) || 1;
        });

        // 4. Derive cluster topic metadata for inspector drawer
        communityMeta = {};
        const STOP_WORDS = new Set([
          "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
          "by", "from", "up", "about", "into", "over", "after", "is", "are", "was", "were",
          "be", "been", "being", "have", "has", "had", "do", "does", "did", "can", "could",
          "will", "would", "shall", "should", "may", "might", "must", "that", "which", "who",
          "this", "these", "those", "using", "based", "approach", "study", "analysis", "system",
          "method", "paper", "towards", "via", "through", "need"
        ]);

        sortedClusters.forEach((oldId, newId) => {
          const members = nodes.filter(n => n.community === newId);
          const count = members.length;
          const color = getCommunityColor(newId);

          // Keyword frequencies
          const kwFreq = new Map();
          const kwDisplay = new Map();

          members.forEach(node => {
            let kws = node.keywords || [];
            if (typeof kws === "string") {
              try {
                kws = JSON.parse(kws);
              } catch (_) {
                kws = kws.split(",");
              }
            }
            if (Array.isArray(kws)) {
              kws.forEach(k => {
                const raw = String(k || "").trim();
                if (!raw) return;
                const norm = raw.toLowerCase();
                kwFreq.set(norm, (kwFreq.get(norm) || 0) + 1);
                if (!kwDisplay.has(norm)) {
                  const cap = raw.split(/\s+/).map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");
                  kwDisplay.set(norm, cap);
                }
              });
            }
          });

          let topic = "";
          if (kwFreq.size > 0) {
            const sortedKw = Array.from(kwFreq.entries()).sort((a, b) => b[1] - a[1]);
            const topKws = sortedKw.slice(0, 2).map(([norm]) => kwDisplay.get(norm));
            topic = topKws.join(", ");
          } else {
            // Fallback: top title keywords
            const titleWordFreq = new Map();
            members.forEach(node => {
              const title = String(node.title || "");
              const words = title.toLowerCase().replace(/[^a-z0-9\s-]/g, " ").split(/\s+/);
              words.forEach(w => {
                if (w.length >= 4 && !STOP_WORDS.has(w)) {
                  titleWordFreq.set(w, (titleWordFreq.get(w) || 0) + 1);
                }
              });
            });

            if (titleWordFreq.size > 0) {
              const sortedWords = Array.from(titleWordFreq.entries()).sort((a, b) => b[1] - a[1]);
              const topWords = sortedWords.slice(0, 2).map(([w]) => w.charAt(0).toUpperCase() + w.slice(1));
              topic = topWords.join(", ");
            } else {
              topic = `Cluster ${newId + 1}`;
            }
          }

          communityMeta[newId] = {
            topic: topic || `Cluster ${newId + 1}`,
            count,
            color
          };
        });
      }
      const runWeightedLPA = detectCommunities;

      // Macro-Cluster Spatial Seeding
      // Place discovered community centroids along a macro ring; member nodes around centroid with jitter;
      // orphan nodes (degree 0) in an outer concentric ring.
      function seedMacroClusterLayout(nodes, singleCenter) {
        if (!nodes || nodes.length === 0) return;

        const orphans = [];
        const communities = new Map();

        nodes.forEach(n => {
          if (singleCenter && n.is_center) {
            n.anchorX = 0;
            n.anchorY = 0;
            n.x = 0;
            n.y = 0;
            n.pinned = true;
            return;
          }
          if ((n.degree || 0) === 0) {
            orphans.push(n);
          } else {
            const c = n.community !== undefined ? n.community : 0;
            if (!communities.has(c)) communities.set(c, []);
            communities.get(c).push(n);
          }
        });

        const commKeys = Array.from(communities.keys());
        const numClusters = commKeys.length;
        const macroRadius = Math.max(260, 45 * Math.sqrt(nodes.length));

        commKeys.forEach((cId, idx) => {
          const angle = (idx / Math.max(1, numClusters)) * 2 * Math.PI;
          const cx = numClusters === 1 ? 0 : Math.cos(angle) * macroRadius;
          const cy = numClusters === 1 ? 0 : Math.sin(angle) * macroRadius;

          const members = communities.get(cId);
          const memberCount = members.length;
          members.forEach((node, mIdx) => {
            node.anchorX = cx;
            node.anchorY = cy;
            const memberAngle = (mIdx / Math.max(1, memberCount)) * 2 * Math.PI + ((mIdx % 3) - 1) * 0.25;
            const jitterDist = 35 + ((mIdx * 37) % 55);
            node.x = cx + Math.cos(memberAngle) * jitterDist;
            node.y = cy + Math.sin(memberAngle) * jitterDist;
            node.vx = 0;
            node.vy = 0;
          });
        });

        // Position orphan nodes (degree 0) on outer concentric ring
        const orphanRadius = macroRadius + 180 + Math.min(100, orphans.length * 2);
        const orphanCount = orphans.length;
        orphans.forEach((node, idx) => {
          const angle = (idx / Math.max(1, orphanCount)) * 2 * Math.PI;
          const ox = Math.cos(angle) * orphanRadius;
          const oy = Math.sin(angle) * orphanRadius;
          node.anchorX = ox;
          node.anchorY = oy;
          node.x = ox;
          node.y = oy;
          node.vx = 0;
          node.vy = 0;
        });
      }

      // Breadth-first search to find all nodes reachable within maxDepth hops from startNode
      function getSpotlightNodeIds(startNode, maxDepth) {
        if (!startNode) return null;
        const visited = new Set();
        visited.add(startNode.id);
        const queue = [{ id: startNode.id, depth: 0 }];

        const adj = new Map();
        for (let i = 0; i < activeLinks.length; i++) {
          const l = activeLinks[i];
          if (!adj.has(l.source)) adj.set(l.source, []);
          if (!adj.has(l.target)) adj.set(l.target, []);
          adj.get(l.source).push(l.target);
          adj.get(l.target).push(l.source);
        }

        while (queue.length > 0) {
          const current = queue.shift();
          if (current.depth >= maxDepth) continue;
          const neighbors = adj.get(current.id) || [];
          for (let i = 0; i < neighbors.length; i++) {
            const nId = neighbors[i];
            if (!visited.has(nId)) {
              visited.add(nId);
              queue.push({ id: nId, depth: current.depth + 1 });
            }
          }
        }

        return visited;
      }

      // Physics Simulation Step
      function updatePhysics() {
        if (activeNodes.length === 0 || alpha < 0.003) return;

        const kRepulsion = 4000;
        const damping = 0.85;
        const kClusterCohesion = 0.035;

        const n = activeNodes.length;

        // Dynamic community centroids for cluster cohesion
        const communityCentroids = new Map();
        for (let i = 0; i < n; i++) {
          const u = activeNodes[i];
          if (u.community !== undefined && u.degree > 0) {
            let c = communityCentroids.get(u.community);
            if (!c) {
              c = { sumX: 0, sumY: 0, count: 0 };
              communityCentroids.set(u.community, c);
            }
            c.sumX += u.x;
            c.sumY += u.y;
            c.count += 1;
          }
        }

        // Pairwise node repulsion
        for (let i = 0; i < n; i++) {
          const u = activeNodes[i];
          for (let j = i + 1; j < n; j++) {
            const v = activeNodes[j];
            const dx = v.x - u.x;
            const dy = v.y - u.y;
            const distSq = dx * dx + dy * dy;
            if (distSq === 0 || distSq > 600000) continue;
            const dist = Math.sqrt(distSq);
            const force = (kRepulsion / (distSq + 400)) * alpha;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            if (!u.pinned) { u.vx -= fx; u.vy -= fy; }
            if (!v.pinned) { v.vx += fx; v.vy += fy; }
          }
        }

        // Spring forces on active links
        // Intra-cluster link distance d ≈ 75px, inter-cluster d ≈ 180px
        for (let i = 0; i < activeLinks.length; i++) {
          const link = activeLinks[i];
          const u = nodesMap.get(link.source);
          const v = nodesMap.get(link.target);
          if (!u || !v) continue;
          const dx = v.x - u.x;
          const dy = v.y - u.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const isSameCluster = (u.community !== undefined && v.community !== undefined && u.community === v.community);
          const restLength = isSameCluster ? 65 : 280;
          const kSpring = (isSameCluster ? 0.045 : 0.005) * alpha;
          const displacement = dist - restLength;
          const force = displacement * kSpring;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          if (!u.pinned) { u.vx += fx; u.vy += fy; }
          if (!v.pinned) { v.vx -= fx; v.vy -= fy; }
        }

        // Cluster cohesion force, anchor attraction, and velocity integration
        for (let i = 0; i < n; i++) {
          const u = activeNodes[i];
          if (!u.pinned) {
            // Cluster cohesion: gently attract nodes toward dynamic community centroid (0.035 * alpha)
            if (u.community !== undefined && u.degree > 0) {
              const c = communityCentroids.get(u.community);
              if (c && c.count > 1) {
                const cx = c.sumX / c.count;
                const cy = c.sumY / c.count;
                u.vx += (cx - u.x) * kClusterCohesion * alpha;
                u.vy += (cy - u.y) * kClusterCohesion * alpha;
              }
            }

            // Anchor attraction to keep clusters in assigned spatial sectors
            if (u.anchorX !== undefined) {
              u.vx += (u.anchorX - u.x) * 0.025 * alpha;
              u.vy += (u.anchorY - u.y) * 0.025 * alpha;
            }

            u.vx *= damping;
            u.vy *= damping;
            u.x += u.vx;
            u.y += u.vy;
          }
        }

        alpha *= 0.992;
      }

      function wakeSimulation() {
        alpha = Math.max(alpha, 0.45);
        requestRender();
      }

      // Filter Nodes and Links based on UI controls
      function applyFilters() {
        const query = searchQuery.trim().toLowerCase();

        // 1. Filter links based on minScore and relation types
        activeLinks = rawLinks.filter(link => {
          if (link.score < minScore) return false;
          if (link.relation_type === "human_curated" && !includeCurated) return false;
          if (link.relation_type === "shared_tags" && !includeTags) return false;
          if (link.relation_type === "hybrid_search" && !includeHybrid) return false;
          if (link.relation_type === "human_curated_and_hybrid_search" && !includeCurated && !includeHybrid) return false;
          return true;
        });

        // 2. Recompute dynamic degrees
        const counts = {};
        rawNodes.forEach(n => { counts[n.id] = 0; });
        activeLinks.forEach(link => {
          counts[link.source] = (counts[link.source] || 0) + 1;
          counts[link.target] = (counts[link.target] || 0) + 1;
        });

        rawNodes.forEach(n => {
          n.degree = counts[n.id] || 0;
          n.radius = getNodeRadius(n.degree);
          // Search match flag
          if (query) {
            const titleMatch = (n.title || "").toLowerCase().includes(query);
            const keyMatch = (n.id || "").toLowerCase().includes(query);
            const kwMatch = (n.keywords || []).some(k => k.toLowerCase().includes(query));
            const authorMatch = (n.authors || []).some(a => a.toLowerCase().includes(query));
            n.searchMatch = titleMatch || keyMatch || kwMatch || authorMatch;
          } else {
            n.searchMatch = true;
          }
        });

        activeNodes = rawNodes;
        const centerCount = rawNodes.filter(n => n.is_center).length;
        const centerText = centerCount > 1 ? ` (${centerCount} centers)` : "";
        statsBadge.textContent = `${activeNodes.length} papers${centerText} • ${activeLinks.length} connections`;
        wakeSimulation();
      }

      // Smooth Camera Animation Loop
      function updateCameraAnimation() {
        if (!animTarget) return;
        animTarget.progress += 0.08;
        const t = Math.min(1.0, animTarget.progress);
        const ease = 1 - Math.pow(1 - t, 3);
        panX = animTarget.startPanX + (animTarget.targetPanX - animTarget.startPanX) * ease;
        panY = animTarget.startPanY + (animTarget.targetPanY - animTarget.startPanY) * ease;
        zoom = animTarget.startZoom + (animTarget.targetZoom - animTarget.startZoom) * ease;
        if (t >= 1.0) animTarget = null;
      }

      function centerOnNode(node) {
        const targetZoom = Math.min(Math.max(zoom, 1.1), 2.2);
        animTarget = {
          startPanX: panX,
          startPanY: panY,
          startZoom: zoom,
          targetPanX: window.innerWidth / 2 - node.x * targetZoom,
          targetPanY: window.innerHeight / 2 - node.y * targetZoom,
          targetZoom: targetZoom,
          progress: 0
        };
        wakeSimulation();
      }

      function focusCluster(communityId) {
        if (communityId === undefined || communityId === null || communityId < 0) return;
        const members = activeNodes.filter(n => n.community === communityId);
        if (members.length === 0) return;

        selectedClusterId = communityId;

        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        members.forEach(n => {
          if (typeof n.x === "number" && !isNaN(n.x) && typeof n.y === "number" && !isNaN(n.y)) {
            const r = n.radius || 10;
            minX = Math.min(minX, n.x - r);
            maxX = Math.max(maxX, n.x + r);
            minY = Math.min(minY, n.y - r);
            maxY = Math.max(maxY, n.y + r);
          }
        });

        if (!isFinite(minX) || !isFinite(maxX) || !isFinite(minY) || !isFinite(maxY)) {
          requestRender();
          return;
        }

        const w = Math.max(maxX - minX, 100);
        const h = Math.max(maxY - minY, 100);
        const padding = 140;
        const drawerOpen = inspectorDrawer && inspectorDrawer.classList.contains("open");
        const drawerW = (drawerOpen && window.innerWidth > 650) ? 380 : 0;
        const availW = Math.max(window.innerWidth - drawerW - padding, 200);
        const availH = Math.max(window.innerHeight - padding, 200);
        const targetZoom = Math.min(Math.max(Math.min(availW / w, availH / h), 0.3), 2.2);
        const targetCenterX = (minX + maxX) / 2;
        const targetCenterY = (minY + maxY) / 2;
        const viewCenterX = (window.innerWidth - drawerW) / 2;
        const viewCenterY = window.innerHeight / 2;

        animTarget = {
          startPanX: panX,
          startPanY: panY,
          startZoom: zoom,
          targetPanX: viewCenterX - targetCenterX * targetZoom,
          targetPanY: viewCenterY - targetCenterY * targetZoom,
          targetZoom: targetZoom,
          progress: 0
        };
        wakeSimulation();
      }

      function fitScreen() {
        selectedClusterId = null;
        if (activeNodes.length === 0) return;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        activeNodes.forEach(n => {
          minX = Math.min(minX, n.x - n.radius);
          maxX = Math.max(maxX, n.x + n.radius);
          minY = Math.min(minY, n.y - n.radius);
          maxY = Math.max(maxY, n.y + n.radius);
        });

        const w = Math.max(maxX - minX, 100);
        const h = Math.max(maxY - minY, 100);
        const padding = 120;
        const availW = Math.max(window.innerWidth - padding, 200);
        const availH = Math.max(window.innerHeight - padding, 200);
        const targetZoom = Math.min(Math.max(Math.min(availW / w, availH / h), 0.25), 2.0);
        const targetCenterX = (minX + maxX) / 2;
        const targetCenterY = (minY + maxY) / 2;

        animTarget = {
          startPanX: panX,
          startPanY: panY,
          startZoom: zoom,
          targetPanX: window.innerWidth / 2 - targetCenterX * targetZoom,
          targetPanY: window.innerHeight / 2 - targetCenterY * targetZoom,
          targetZoom: targetZoom,
          progress: 0
        };
        wakeSimulation();
      }

      // Drawing Routine
      let renderPending = false;
      function requestRender() {
        if (!renderPending) {
          renderPending = true;
          requestAnimationFrame(render);
        }
      }

      function render() {
        renderPending = false;
        updatePhysics();
        updateCameraAnimation();

        const dpr = window.devicePixelRatio || 1;
        ctx.save();
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = COLORS.background;
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        // Apply camera transformation
        ctx.scale(dpr, dpr);
        ctx.translate(panX, panY);
        ctx.scale(zoom, zoom);

        // Spotlight mode set when selectedClusterId or selectedNode is active
        let spotlightNodeIds = null;
        if (selectedClusterId !== null && selectedClusterId !== undefined) {
          const members = activeNodes.filter(n => n.community === selectedClusterId);
          spotlightNodeIds = new Set(members.map(m => m.id));
        } else if (selectedNode) {
          spotlightNodeIds = getSpotlightNodeIds(selectedNode, currentDepth);
        }

        // Find hovered 1-hop neighborhood if no selected node and no cluster spotlight
        const hoveredNeighbors = new Set();
        if (hoveredNode && !selectedNode && selectedClusterId === null) {
          hoveredNeighbors.add(hoveredNode.id);
          activeLinks.forEach(link => {
            if (link.source === hoveredNode.id) hoveredNeighbors.add(link.target);
            if (link.target === hoveredNode.id) hoveredNeighbors.add(link.source);
          });
        }

        // Draw Links
        for (let i = 0; i < activeLinks.length; i++) {
          const link = activeLinks[i];
          const u = nodesMap.get(link.source);
          const v = nodesMap.get(link.target);
          if (!u || !v) continue;

          let alphaVal = 0.75;
          let drawThickness = getEdgeThickness(link.score);

          if (spotlightNodeIds) {
            // Highlighted links (incident to spotlight set): 100% opacity, stroke width 2.5px
            // Non-highlighted links: 4% opacity
            const isSpotlightLink = spotlightNodeIds.has(link.source) && spotlightNodeIds.has(link.target);
            if (isSpotlightLink) {
              alphaVal = 1.0;
              drawThickness = 2.5;
            } else {
              alphaVal = 0.04;
            }
          } else if (hoveredNode) {
            if (link.source === hoveredNode.id || link.target === hoveredNode.id) {
              alphaVal = 1.0;
              drawThickness = drawThickness * 1.5;
            } else {
              alphaVal = 0.12;
            }
          } else if (searchQuery.trim() && (!u.searchMatch || !v.searchMatch)) {
            alphaVal = 0.15;
          }

          ctx.save();
          ctx.beginPath();
          ctx.moveTo(u.x, u.y);
          ctx.lineTo(v.x, v.y);
          ctx.strokeStyle = getEdgeColor(link.relation_type);
          ctx.globalAlpha = alphaVal;
          ctx.lineWidth = drawThickness;
          ctx.lineCap = "round";
          ctx.stroke();
          ctx.restore();
        }

        // Draw Nodes
        for (let i = 0; i < activeNodes.length; i++) {
          const node = activeNodes[i];

          let alphaVal = 1.0;
          let drawRadius = node.radius;
          let showLabel = false;

          if (spotlightNodeIds) {
            // Highlighted nodes: 100% opacity, standard radius, crisp labels
            // Non-highlighted nodes: 12% opacity, smaller radius, labels hidden
            if (spotlightNodeIds.has(node.id)) {
              alphaVal = 1.0;
              drawRadius = node.radius;
              showLabel = true;
            } else {
              alphaVal = 0.12;
              drawRadius = Math.max(3, node.radius * 0.7);
              showLabel = false;
            }
          } else {
            if (hoveredNode) {
              alphaVal = hoveredNeighbors.has(node.id) ? 1.0 : 0.15;
            } else if (searchQuery.trim() && !node.searchMatch) {
              alphaVal = 0.18;
            }

            // Semantic Zoom & Degree-Based Label Culling (Level of Detail)
            if ((hoveredNode && hoveredNeighbors.has(node.id)) || (searchQuery.trim() && node.searchMatch)) {
              showLabel = true;
            } else {
              if (zoom < 0.65) {
                showLabel = (node.degree >= 3 || Boolean(node.is_center));
              } else if (zoom < 1.05) {
                showLabel = (node.degree >= 1 || Boolean(node.is_center));
              } else {
                showLabel = true;
              }
            }
          }

          ctx.save();
          ctx.globalAlpha = alphaVal;

          // Glowing Halo for Center Node
          if (node.is_center) {
            ctx.beginPath();
            ctx.arc(node.x, node.y, drawRadius + 7, 0, Math.PI * 2);
            ctx.fillStyle = "rgba(129, 140, 248, 0.22)";
            ctx.fill();
            ctx.strokeStyle = "rgba(129, 140, 248, 0.7)";
            ctx.lineWidth = 2;
            ctx.stroke();
          }

          // Glowing Selection Ring
          if (selectedNode && selectedNode.id === node.id) {
            ctx.beginPath();
            ctx.arc(node.x, node.y, drawRadius + 5, 0, Math.PI * 2);
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = 2.5;
            ctx.stroke();
          }

          // Search Match Highlight Ring
          if (searchQuery.trim() && node.searchMatch && !hoveredNode && !selectedNode && selectedClusterId === null) {
            ctx.beginPath();
            ctx.arc(node.x, node.y, drawRadius + 4, 0, Math.PI * 2);
            ctx.strokeStyle = "#facc15";
            ctx.lineWidth = 2;
            ctx.stroke();
          }

          // Node Circle Body
          // Default nodes inherit their community color so topic clusters are immediately recognizable.
          // Center / pinned nodes retain their special styling.
          ctx.beginPath();
          ctx.arc(node.x, node.y, drawRadius, 0, Math.PI * 2);
          if (node.is_center) {
            ctx.fillStyle = COLORS.nodeCenter;
          } else if (node.pinned) {
            ctx.fillStyle = COLORS.nodePinned;
          } else if (node.community !== undefined && node.community !== null && node.community >= 0) {
            ctx.fillStyle = getCommunityColor(node.community);
          } else {
            ctx.fillStyle = COLORS.nodeDefault;
          }
          ctx.fill();

          ctx.strokeStyle = COLORS.nodeBorder;
          ctx.lineWidth = 1.5;
          ctx.stroke();

          // Node Title Label (crisp labels when highlighted, hidden when non-highlighted in spotlight)
          if (showLabel) {
            const isCrisp = (spotlightNodeIds && spotlightNodeIds.has(node.id)) || Boolean(node.is_center) || (hoveredNode && hoveredNeighbors.has(node.id));
            const citation = getShortCitation(node);

            // Text shadow for high contrast and legibility over canvas edges and backgrounds
            ctx.shadowColor = COLORS.textShadow || "rgba(15, 23, 42, 0.95)";
            ctx.shadowBlur = 4;
            ctx.textAlign = "center";
            ctx.textBaseline = "top";

            const maxChars = isCrisp ? 40 : 24;
            let displayTitle = node.title || node.id || "";
            if (displayTitle.length > maxChars) {
              displayTitle = displayTitle.slice(0, maxChars) + "...";
            }

            if (citation) {
              // Line 1 (citation): 600 10px system-ui, sans-serif, color COLORS.textMuted or community accent
              ctx.font = "600 10px system-ui, -apple-system, sans-serif";
              const commColor = (node.community !== undefined && node.community !== null && node.community >= 0) ? getCommunityColor(node.community) : null;
              ctx.fillStyle = commColor || COLORS.textMuted;
              ctx.fillText(citation, node.x, node.y + drawRadius + 5);

              // Line 2 (compact title): 10px system-ui, sans-serif, color COLORS.textDefault, truncated at 24 chars with ... (or up to 40 chars/crisp when selected/hovered/spotlighted)
              ctx.font = `${isCrisp ? "600 10px" : "10px"} system-ui, -apple-system, sans-serif`;
              ctx.fillStyle = COLORS.textDefault;
              ctx.fillText(displayTitle, node.x, node.y + drawRadius + 18);
            } else {
              // If citation does not exist: draw compact title directly below node circle
              ctx.font = `${isCrisp ? "600 11px" : "10px"} system-ui, -apple-system, sans-serif`;
              ctx.fillStyle = COLORS.textDefault;
              ctx.fillText(displayTitle, node.x, node.y + drawRadius + 5);
            }

            ctx.shadowBlur = 0;
            ctx.shadowColor = "transparent";
          }

          ctx.restore();
        }


        ctx.restore();

        // Continue animation frame if physics or camera active
        if (alpha >= 0.003 || animTarget) {
          requestRender();
        }
      }

      // Inspector Drawer Logic
      function openInspector(node) {
        selectedNode = node;
        selectedClusterId = null;
        inspectorTitle.textContent = node.title || node.id;
        
        // Year Display: If present and valid, show Year: ${node.year}. If not, hide pill.
        const yearStr = (node.year && String(node.year).trim() !== "N/A") ? String(node.year).trim() : "";
        if (inspectorYearPill) {
          if (yearStr) {
            inspectorYearVal.textContent = yearStr;
            inspectorYearPill.style.display = "inline-flex";
          } else {
            inspectorYearPill.style.display = "none";
          }
        }

        inspectorTypeVal.textContent = node.type || "Document";
        inspectorDegreeVal.textContent = node.degree || 0;

        // Populate Topical Cluster Section
        if (inspectorClusterCard) {
          inspectorClusterCard.innerHTML = "";

          const hasCommunity = (node.community !== undefined && node.community !== null && node.community >= 0);
          const members = hasCommunity
            ? activeNodes.filter(n => n.community === node.community)
            : [];

          if (members.length >= 2) {
            const meta = communityMeta[node.community] || {};
            const topicName = meta.topic || `Cluster ${node.community + 1}`;
            const commColor = getCommunityColor(node.community);

            const header = document.createElement("div");
            header.className = "cluster-header";

            const titleRow = document.createElement("div");
            titleRow.className = "cluster-title-row";

            const dot = document.createElement("span");
            dot.className = "cluster-dot";
            dot.style.backgroundColor = commColor;

            const topicSpan = document.createElement("span");
            topicSpan.className = "cluster-topic-name";
            topicSpan.textContent = topicName;
            topicSpan.title = topicName;

            titleRow.appendChild(dot);
            titleRow.appendChild(topicSpan);

            const metaRow = document.createElement("div");
            metaRow.className = "cluster-meta-row";

            const subtitle = document.createElement("span");
            subtitle.style.fontSize = "11px";
            subtitle.style.color = "var(--text-muted, #94a3b8)";
            subtitle.textContent = `${members.length} papers in library`;

            const btnFocus = document.createElement("button");
            btnFocus.type = "button";
            btnFocus.className = "btn-focus-cluster";
            btnFocus.innerHTML = `
              <svg xmlns="http://www.w3.org/2000/svg" style="width:11px;height:11px;fill:currentColor;" viewBox="0 0 24 24"><path d="M12 8c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4zm8.94 3A8.994 8.994 0 0 0 13 3.06V1h-2v2.06A8.994 8.994 0 0 0 3.06 11H1v2h2.06A8.994 8.994 0 0 0 11 20.94V23h2v-2.06A8.994 8.994 0 0 0 20.94 13H23v-2h-2.06zM12 19c-3.87 0-7-3.13-7-7s3.13-7 7-7 7 3.13 7 7-3.13 7-7 7z"/></svg>
              <span>Focus Cluster in Graph</span>
            `;
            btnFocus.title = "Highlight and fit all members of this cluster in graph";
            btnFocus.addEventListener("click", (e) => {
              e.stopPropagation();
              focusCluster(node.community);
            });

            metaRow.appendChild(subtitle);
            metaRow.appendChild(btnFocus);

            header.appendChild(titleRow);
            header.appendChild(metaRow);
            inspectorClusterCard.appendChild(header);

            const membersLabel = document.createElement("div");
            membersLabel.className = "cluster-members-label";
            membersLabel.textContent = "Papers in this cluster:";
            inspectorClusterCard.appendChild(membersLabel);

            const membersList = document.createElement("div");
            membersList.className = "cluster-members-list";

            const sortedMembers = [...members].sort((a, b) => {
              if (a.id === node.id) return -1;
              if (b.id === node.id) return 1;
              return (b.degree || 0) - (a.degree || 0);
            });

            sortedMembers.forEach(member => {
              const item = document.createElement("div");
              const isCurrent = (member.id === node.id);
              item.className = "cluster-member-item" + (isCurrent ? " current" : "");

              const titleEl = document.createElement("div");
              titleEl.className = "cluster-member-title";
              const citation = getShortCitation(member);
              const memberTitle = member.title || member.id;
              const fullText = citation ? `${citation} — ${memberTitle}` : memberTitle;
              titleEl.textContent = fullText;
              titleEl.title = fullText;
              item.appendChild(titleEl);

              if (isCurrent) {
                const badge = document.createElement("span");
                badge.className = "cluster-member-tag";
                badge.textContent = "Current Paper";
                item.appendChild(badge);
              } else {
                item.addEventListener("click", () => {
                  openInspector(member);
                  centerOnNode(member);
                });
              }

              membersList.appendChild(item);
            });

            inspectorClusterCard.appendChild(membersList);
          } else {
            // Isolated paper (orphan / 1 paper)
            const emptyState = document.createElement("div");
            emptyState.className = "cluster-empty-state";

            const titleRow = document.createElement("div");
            titleRow.className = "cluster-title-row";

            const dot = document.createElement("span");
            dot.className = "cluster-dot";
            dot.style.backgroundColor = "var(--text-muted, #94a3b8)";
            dot.style.opacity = "0.5";

            const titleSpan = document.createElement("span");
            titleSpan.className = "cluster-topic-name";
            titleSpan.style.color = "var(--text-muted, #94a3b8)";
            titleSpan.textContent = "Isolated Paper";

            titleRow.appendChild(dot);
            titleRow.appendChild(titleSpan);

            const subtitle = document.createElement("div");
            subtitle.style.fontSize = "11px";
            subtitle.style.color = "var(--text-muted, #94a3b8)";
            subtitle.style.paddingLeft = "18px";
            subtitle.textContent = "No connected cluster peers found in library.";

            emptyState.appendChild(titleRow);
            emptyState.appendChild(subtitle);

            inspectorClusterCard.appendChild(emptyState);
          }
        }

        // Populate Bibliographic Info
        if (node.authors && node.authors.length > 0) {
          inspectorAuthors.textContent = node.authors.join(", ");
          inspectorAuthors.style.display = "block";
          inspectorBibSection.style.display = "block";
        } else {
          inspectorAuthors.textContent = "";
          inspectorAuthors.style.display = "none";
        }

        if (node.publication) {
          inspectorPublication.textContent = node.publication;
          inspectorPublication.style.display = "block";
          inspectorBibSection.style.display = "block";
        } else {
          inspectorPublication.textContent = "";
          inspectorPublication.style.display = "none";
        }

        if (!node.authors?.length && !node.publication) {
          inspectorBibSection.style.display = "none";
        }

        // Populate Abstract
        if (node.abstract && String(node.abstract).trim()) {
          inspectorAbstract.textContent = String(node.abstract).trim();
          inspectorAbstractSection.style.display = "block";
        } else {
          inspectorAbstract.textContent = "";
          inspectorAbstractSection.style.display = "none";
        }

        // Populate keywords
        inspectorKeywords.innerHTML = "";
        if (node.keywords && node.keywords.length > 0) {
          node.keywords.forEach(kw => {
            const pill = document.createElement("span");
            pill.className = "tag-pill";
            pill.textContent = kw;
            inspectorKeywords.appendChild(pill);
          });
        } else {
          inspectorKeywords.innerHTML = '<span style="font-size:11px;color:#64748b;">No domain keywords available</span>';
        }

        // Find connected papers
        inspectorConnections.innerHTML = "";
        const incidentLinks = activeLinks.filter(l => l.source === node.id || l.target === node.id);
        inspectorConnCount.textContent = incidentLinks.length;

        if (incidentLinks.length > 0) {
          // Sort by score descending
          incidentLinks.sort((a, b) => b.score - a.score);
          incidentLinks.forEach(link => {
            const targetId = (link.source === node.id) ? link.target : link.source;
            const targetNode = nodesMap.get(targetId);
            if (!targetNode) return;

            const card = document.createElement("div");
            card.className = "connection-card";

            let badgeClass = "badge-hybrid";
            let badgeText = "Hybrid";
            if (link.relation_type === "human_curated_and_hybrid_search") {
              badgeClass = "badge-both";
              badgeText = "Curated + Hybrid";
            } else if (link.relation_type === "human_curated") {
              badgeClass = "badge-curated";
              badgeText = "Curated";
            } else if (link.relation_type === "shared_tags") {
              badgeClass = "badge-tags";
              badgeText = "Shared Tags";
            }

            const pctScore = Math.round((link.score || 0) * 100);

            const safeTitle = escapeHtml(targetNode.title || targetId);
            const safeType = escapeHtml(targetNode.type || "Paper");
            const safeYear = targetNode.year ? escapeHtml(targetNode.year) + " • " : "";
            const safeEvidence = link.evidence ? escapeHtml(link.evidence) : "";

            card.innerHTML = `
              <div class="connection-card-header">
                <span class="relation-badge ${badgeClass}">${badgeText}</span>
                <span style="font-size:11px;font-weight:700;color:var(--accent, #38bdf8);">${pctScore}%</span>
              </div>
              <div class="connection-card-title">${safeTitle}</div>
              <div class="connection-card-meta">
                <span>${safeYear}${safeType}</span>
              </div>
              ${safeEvidence ? `<div class="connection-evidence">${safeEvidence}</div>` : ""}
            `;

            card.addEventListener("click", () => {
              openInspector(targetNode);
              centerOnNode(targetNode);
            });

            inspectorConnections.appendChild(card);
          });
        } else {
          inspectorConnections.innerHTML = '<span style="font-size:11px;color:#64748b;">No active connections at current score threshold</span>';
        }

        inspectorDrawer.classList.add("open");
        requestRender();
      }

      btnCloseInspector.addEventListener("click", () => {
        inspectorDrawer.classList.remove("open");
        selectedNode = null;
        selectedClusterId = null;
        requestRender();
      });

      if (btnSelectInZotero) {
        btnSelectInZotero.addEventListener("click", () => {
          if (selectedNode && selectedNode.id) {
            selectItemInZotero(selectedNode.parent_item_key || selectedNode.id);
          }
        });
      }

      // Mouse & Gesture Canvas Interaction Handlers
      function findNodeAt(worldX, worldY) {
        for (let i = activeNodes.length - 1; i >= 0; i--) {
          const n = activeNodes[i];
          const dx = worldX - n.x;
          const dy = worldY - n.y;
          if (dx * dx + dy * dy <= (n.radius + 4) * (n.radius + 4)) {
            return n;
          }
        }
        return null;
      }

      let mouseDownPos = { x: 0, y: 0 };
      canvas.addEventListener("mousedown", (e) => {
        const rect = canvas.getBoundingClientRect();
        const worldPos = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
        mouseDownPos = { x: e.clientX, y: e.clientY };
        hasDraggedSinceMouseDown = false;

        const clickedNode = findNodeAt(worldPos.x, worldPos.y);
        if (clickedNode) {
          draggedNode = clickedNode;
          draggedNode.pinned = true;
          canvas.classList.add("dragging-node");
          wakeSimulation();
        } else {
          isPanning = true;
          panStartX = e.clientX - panX;
          panStartY = e.clientY - panY;
          canvas.classList.add("panning");
        }
      });

      window.addEventListener("mousemove", (e) => {
        const rect = canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        if (Math.hypot(e.clientX - mouseDownPos.x, e.clientY - mouseDownPos.y) > 4) {
          hasDraggedSinceMouseDown = true;
        }

        if (draggedNode) {
          const worldPos = screenToWorld(mouseX, mouseY);
          draggedNode.x = worldPos.x;
          draggedNode.y = worldPos.y;
          wakeSimulation();
          return;
        }

        if (isPanning) {
          panX = e.clientX - panStartX;
          panY = e.clientY - panStartY;
          requestRender();
          return;
        }

        // Hover test
        const worldPos = screenToWorld(mouseX, mouseY);
        const node = findNodeAt(worldPos.x, worldPos.y);
        if (node !== hoveredNode) {
          hoveredNode = node;
          if (hoveredNode) {
            const firstAuthor = (hoveredNode.authors && hoveredNode.authors.length > 0) ? `${hoveredNode.authors[0]} • ` : "";
            const yearStr = hoveredNode.year ? `${hoveredNode.year} • ` : "";
            tooltipTitle.textContent = hoveredNode.title || hoveredNode.id;
            tooltipMeta.textContent = `${firstAuthor}${yearStr}${hoveredNode.type || "Document"}`;
            tooltipConnections.textContent = `${hoveredNode.degree} connection${hoveredNode.degree === 1 ? "" : "s"}`;
            tooltip.style.display = "block";
          } else {
            tooltip.style.display = "none";
          }
          requestRender();
        }

        if (hoveredNode) {
          // Keep tooltip within window bounds
          const tipX = Math.min(e.clientX + 16, window.innerWidth - 300);
          const tipY = Math.min(e.clientY + 16, window.innerHeight - 100);
          tooltip.style.left = tipX + "px";
          tooltip.style.top = tipY + "px";
        }
      });

      window.addEventListener("mouseup", (e) => {
        if (draggedNode) {
          canvas.classList.remove("dragging-node");
          draggedNode = null;
        }
        if (isPanning) {
          canvas.classList.remove("panning");
          isPanning = false;
        }

        // If it was a clean click without significant drag
        if (!hasDraggedSinceMouseDown) {
          const rect = canvas.getBoundingClientRect();
          const worldPos = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
          const clickedNode = findNodeAt(worldPos.x, worldPos.y);
          if (clickedNode) {
            openInspector(clickedNode);
          } else {
            // Clicked canvas background: restore all nodes and links to full opacity
            selectedNode = null;
            selectedClusterId = null;
            inspectorDrawer.classList.remove("open");
            requestRender();
          }
        }
      });

      // Cursor-anchored Zoom (Wheel)
      canvas.addEventListener("wheel", (e) => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        const worldX = (mouseX - panX) / zoom;
        const worldY = (mouseY - panY) / zoom;

        const zoomFactor = e.deltaY < 0 ? 1.12 : 0.89;
        const newZoom = Math.min(Math.max(zoom * zoomFactor, 0.15), 5.0);

        panX = mouseX - worldX * newZoom;
        panY = mouseY - worldY * newZoom;
        zoom = newZoom;
        requestRender();
      }, { passive: false });

      // Double Click Handlers: unpin node & center on it, or fit screen
      canvas.addEventListener("dblclick", (e) => {
        const rect = canvas.getBoundingClientRect();
        const worldPos = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
        const clickedNode = findNodeAt(worldPos.x, worldPos.y);
        if (clickedNode) {
          // Unpin and smoothly center
          clickedNode.pinned = false;
          openInspector(clickedNode);
          centerOnNode(clickedNode);
        } else {
          selectedClusterId = null;
          fitScreen();
        }
      });

      // Toolbar Controls Handlers
      if (depthSlider) {
        depthSlider.addEventListener("input", (e) => {
          currentDepth = parseInt(e.target.value, 10) || 2;
          if (depthVal) depthVal.textContent = String(currentDepth);

          const isEgoSubgraph = Boolean(lastScopedParams?.center_key || lastScopedParams?.center_keys) && !isLockedToFullLibrary;
          if (isEgoSubgraph) {
            lastScopedParams.depth = String(currentDepth);
            lastScopedParams.min_score = minScore.toFixed(2);
            loadGraphData(lastScopedParams);
          } else {
            // Full library: if selectedNode is present, re-render to update spotlight neighborhood
            requestRender();
          }
        });
      }

      btnFitScreen.addEventListener("click", fitScreen);

      if (btnToggleScope) {
        btnToggleScope.addEventListener("click", () => {
          if (btnToggleScope.textContent.includes("Full Library")) {
            isLockedToFullLibrary = true;
            window.isLockedToFullLibrary = true;
            window.isUserLockedToFullLibrary = true;
            loadGraphData({});
          } else if (btnToggleScope.textContent.includes("Focus View")) {
            isLockedToFullLibrary = false;
            window.isLockedToFullLibrary = false;
            window.isUserLockedToFullLibrary = false;
            if (lastScopedParams) {
              loadGraphData(lastScopedParams);
            }
          }
        });
      }

      // Search Filter
      searchInput.addEventListener("input", (e) => {
        searchQuery = e.target.value;
        applyFilters();
      });

      btnClearSearch.addEventListener("click", () => {
        searchInput.value = "";
        searchQuery = "";
        applyFilters();
        searchInput.focus();
      });

      // Confidence Slider
      confidenceSlider.addEventListener("input", (e) => {
        minScore = parseFloat(e.target.value);
        minScoreVal.textContent = minScore.toFixed(2);
        applyFilters();
      });

      // Edge Type Checkboxes
      chkCurated.addEventListener("change", (e) => {
        includeCurated = e.target.checked;
        applyFilters();
      });

      chkHybrid.addEventListener("change", (e) => {
        includeHybrid = e.target.checked;
        applyFilters();
      });

      chkTags.addEventListener("change", (e) => {
        includeTags = e.target.checked;
        applyFilters();
      });

      // Initialize Graph from Backend
      async function loadGraphData(customParams = null) {
        let centerKey = null;
        let centerKeys = null;
        let collectionKey = null;
        let collectionKeys = null;
        let depth = "2";
        let initialMinScore = "0.50";

        if (customParams instanceof URLSearchParams) {
          centerKey = customParams.get("center_key");
          centerKeys = customParams.get("center_keys");
          collectionKey = customParams.get("collection_key");
          collectionKeys = customParams.get("collection_keys");
          depth = customParams.get("depth") || "2";
          initialMinScore = customParams.get("min_score") || "0.50";
        } else if (customParams && typeof customParams === "object") {
          centerKey = customParams.center_key || null;
          centerKeys = customParams.center_keys || null;
          collectionKey = customParams.collection_key || null;
          collectionKeys = customParams.collection_keys || null;
          depth = customParams.depth ? String(customParams.depth) : "2";
          initialMinScore = customParams.min_score ? String(customParams.min_score) : "0.50";
        } else {
          const urlParams = new URLSearchParams(window.location.search);
          centerKey = urlParams.get("center_key");
          centerKeys = urlParams.get("center_keys");
          collectionKey = urlParams.get("collection_key");
          collectionKeys = urlParams.get("collection_keys");
          depth = urlParams.get("depth") || "2";
          initialMinScore = urlParams.get("min_score") || "0.50";
        }

        currentDepth = parseInt(depth, 10) || 2;
        if (depthSlider) depthSlider.value = String(currentDepth);
        if (depthVal) depthVal.textContent = String(currentDepth);

        minScore = parseFloat(initialMinScore) || 0.50;
        confidenceSlider.value = initialMinScore;
        minScoreVal.textContent = minScore.toFixed(2);

        const isScoped = Boolean(centerKey || centerKeys || collectionKey || collectionKeys);

        if (isScoped) {
          lastScopedParams = {
            center_key: centerKey,
            center_keys: centerKeys,
            collection_key: collectionKey,
            collection_keys: collectionKeys,
            depth: depth,
            min_score: initialMinScore
          };
          Object.keys(lastScopedParams).forEach(k => {
            if (!lastScopedParams[k]) delete lastScopedParams[k];
          });

          if (btnToggleScope) {
            btnToggleScope.style.display = "inline-flex";
            btnToggleScope.textContent = "🌐 Full Library";
          }
          isLockedToFullLibrary = false;
          window.isLockedToFullLibrary = false;
          window.isUserLockedToFullLibrary = false;
        } else {
          if (lastScopedParams && btnToggleScope) {
            btnToggleScope.style.display = "inline-flex";
            btnToggleScope.textContent = "🎯 Focus View";
          } else if (btnToggleScope) {
            btnToggleScope.style.display = "none";
          }
        }

        const apiParams = new URLSearchParams();
        if (centerKey) apiParams.set("center_key", centerKey);
        if (centerKeys) apiParams.set("center_keys", centerKeys);
        if (collectionKey) apiParams.set("collection_key", collectionKey);
        if (collectionKeys) apiParams.set("collection_keys", collectionKeys);
        apiParams.set("depth", depth);
        apiParams.set("min_score", "0.50"); // Load full candidate set for client slider responsiveness
        apiParams.set("include_curated", "true");
        apiParams.set("include_tags", "true");

        loadingOverlay.innerHTML = `
          <div class="spinner"></div>
          <div style="font-size: 14px; color: #94a3b8;">Loading Knowledge Graph...</div>
        `;
        loadingOverlay.style.display = "flex";
        loadingOverlay.style.opacity = "1";

        try {
          const resp = await fetch(`${BACKEND_URL}/graph/data?${apiParams.toString()}`, {
            headers: getAuthHeaders(),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const data = await resp.json();

          const centerNodesList = (data.nodes || []).filter(n => n.is_center);
          const singleCenter = centerNodesList.length === 1;

          rawNodes = (data.nodes || []).map((n) => {
            const isSingleCenter = singleCenter && n.is_center;
            return {
              ...n,
              x: 0,
              y: 0,
              vx: 0,
              vy: 0,
              pinned: Boolean(isSingleCenter),
              radius: getNodeRadius(n.degree),
              searchMatch: true
            };
          });

          rawLinks = data.links || [];
          nodesMap = new Map();
          rawNodes.forEach(n => nodesMap.set(n.id, n));

          // 1. Initial degree calculation on rawLinks
          const counts = {};
          rawNodes.forEach(n => { counts[n.id] = 0; });
          rawLinks.forEach(link => {
            counts[link.source] = (counts[link.source] || 0) + 1;
            counts[link.target] = (counts[link.target] || 0) + 1;
          });
          rawNodes.forEach(n => {
            n.degree = counts[n.id] || 0;
            n.radius = getNodeRadius(n.degree);
          });

          // 2. Discover communities using Weighted LPA
          detectCommunities(rawNodes, rawLinks, 5);

          // 3. Seed macro cluster spatial layout
          seedMacroClusterLayout(rawNodes, singleCenter);

          selectedNode = null;
          selectedClusterId = null;
          hoveredNode = null;
          draggedNode = null;
          inspectorDrawer.classList.remove("open");

          // Set initial camera
          panX = window.innerWidth / 2;
          panY = window.innerHeight / 2;
          zoom = 1.0;

          applyFilters();

          // Hide loading overlay
          loadingOverlay.style.opacity = "0";
          setTimeout(() => { loadingOverlay.style.display = "none"; }, 320);

          // If exactly 1 center node is present, open inspector drawer for it.
          // If multiple center nodes exist, display network stats badge with center count
          // and keep drawer closed until user clicks a node.
          const centerNodes = rawNodes.filter(n => n.is_center);
          if (centerNodes.length === 1) {
            openInspector(centerNodes[0]);
          } else if (centerNodes.length > 1) {
            statsBadge.textContent = `${activeNodes.length} papers (${centerNodes.length} centers) • ${activeLinks.length} connections`;
          }

          fitScreen();
        } catch (err) {
          console.error("Failed to load graph data:", err);
          const safeErrMsg = escapeHtml(err.message || "Network Error");
          loadingOverlay.innerHTML = `
            <div style="font-size: 16px; font-weight:600; color: #ef4444; margin-bottom:8px;">Failed to Load Graph</div>
            <div style="font-size: 13px; color: #94a3b8;">${safeErrMsg}</div>
          `;
        }
      }

      // Expose for external calls
      window.loadGraph = function(params) {
        loadGraphData(params);
      };
      window.selectItemInZotero = selectItemInZotero;
      window.applyTheme = applyTheme;
      window.getActiveTheme = getActiveTheme;

      // Startup
      if (typeof module === "undefined") {
        initThemeListeners();
        resizeCanvas();
        loadGraphData();
      }

      if (typeof module !== "undefined" && module.exports) {
        module.exports = {
          detectCommunities,
          seedMacroClusterLayout,
          getLinkWeight,
          getNodeRadius,
          getEdgeThickness,
          getEdgeColor,
          screenToWorld,
          worldToScreen,
          findNodeAt,
          getShortCitation,
          _setActiveNodes: (nodes) => { activeNodes = nodes; },
          _setPanZoom: (px, py, z) => { panX = px; panY = py; zoom = z; },
        };
      }
    })();

/* ==========================================================================
   Zocrates Official Showcase Website — Interactive Controller (app.js)
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  /* ------------------------------------------------------------------------
     1. Live 5-Theme Switcher (Custom Polished Popover)
     ------------------------------------------------------------------------ */
  const THEME_NAMES = {
    'slate': 'Slate',
    'amoled': 'AMOLED',
    'zotero-dark': 'Zotero Dark',
    'zotero-light': 'Zotero Light',
    'sepia': 'Sepia'
  };

  const themeDockTrigger = document.getElementById('theme-dock-trigger');
  const themeDockCurrent = document.getElementById('theme-dock-current');
  const themeMenuPopover = document.getElementById('theme-menu-popover');
  const themeMenuItems = document.querySelectorAll('.theme-menu-item');

  function safeGetTheme() {
    try {
      return localStorage.getItem('zocrates-site-theme') || 'slate';
    } catch {
      return 'slate';
    }
  }

  function safeSetTheme(key) {
    try {
      localStorage.setItem('zocrates-site-theme', key);
    } catch {
      // Storage unavailable or blocked
    }
  }

  const savedTheme = safeGetTheme();

  function applyTheme(themeKey) {
    const key = THEME_NAMES[themeKey] ? themeKey : 'slate';
    document.body.setAttribute('data-theme', key);
    safeSetTheme(key);

    if (themeDockCurrent) {
      themeDockCurrent.textContent = THEME_NAMES[key] || 'Slate';
    }

    themeMenuItems.forEach((item) => {
      item.classList.toggle('active', item.dataset.theme === key);
    });

    if (window._relationsGraphReady && typeof drawRelationsGraph === 'function') {
      drawRelationsGraph();
    }
  }

  // Initial theme application
  applyTheme(savedTheme);

  if (themeDockTrigger && themeMenuPopover) {
    themeDockTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = themeMenuPopover.classList.toggle('open');
      themeDockTrigger.setAttribute('aria-expanded', String(isOpen));
    });

    themeMenuItems.forEach((item) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        applyTheme(item.dataset.theme);
        themeMenuPopover.classList.remove('open');
        themeDockTrigger.setAttribute('aria-expanded', 'false');
      });
    });

    document.addEventListener('click', (e) => {
      if (!themeMenuPopover.contains(e.target) && !themeDockTrigger.contains(e.target)) {
        themeMenuPopover.classList.remove('open');
        themeDockTrigger.setAttribute('aria-expanded', 'false');
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && themeMenuPopover.classList.contains('open')) {
        themeMenuPopover.classList.remove('open');
        themeDockTrigger.setAttribute('aria-expanded', 'false');
        themeDockTrigger.focus();
      }
    });
  }

  /* ------------------------------------------------------------------------
     2. Hero Split-Pane Workbench: Citation Pills & Evidence Peek Popover
     ------------------------------------------------------------------------ */
  const CITATION_DATA = {
    '1': {
      title: 'Vaswani et al. (2017) — Attention Is All You Need',
      section: '§ 3.2.2 Multi-Head Attention',
      sectionBadge: '📑 Outline: § 3.2.2',
      confidence: 'Support Probability: 0.94',
      quote: '"Multi-head attention allows the model to jointly attend to information from different representation subspaces at different positions. With a single attention head, averaging inhibits this."',
      targetSectionTitle: '3.2.2 Multi-Head Attention'
    },
    '2': {
      title: 'Dao et al. (2022) — FlashAttention: Fast and Memory-Efficient Exact Attention',
      section: '§ 2.1 IO-Aware Tiling & Softmax Recomputation',
      sectionBadge: '📑 Outline: § 2.1',
      confidence: 'Support Probability: 0.91',
      quote: '"By splitting Q, K, V into blocks and computing exact softmax increments in fast SRAM, FlashAttention avoids materializing the N×N attention matrix in slow HBM."',
      targetSectionTitle: '2.1 IO-Aware Tiling'
    },
    '3': {
      title: 'Beltagy et al. (2020) — Longformer: The Long-Document Transformer',
      section: '§ 3.1 Sliding Window & Dilated Attention',
      sectionBadge: '📑 Outline: § 3.1',
      confidence: 'Support Probability: 0.87',
      quote: '"Combining local windowed attention with task-motivated global attention scales linearly O(N) with sequence length while preserving full-context capacity on key tokens."',
      targetSectionTitle: '3.1 Sliding Window Attention'
    }
  };

  const pillButtons = document.querySelectorAll('.hero-citation-pill');
  const popoverTitle = document.getElementById('popover-doc-title');
  const popoverConfidence = document.getElementById('popover-confidence');
  const popoverSection = document.getElementById('popover-section');
  const popoverQuote = document.getElementById('popover-quote');
  const openInReaderBtn = document.getElementById('btn-open-in-reader');
  const selectInLibBtn = document.getElementById('btn-select-in-library');
  const popoverStatus = document.getElementById('popover-action-status');
  const readerStatusLabel = document.getElementById('reader-status-label');
  const outlineBadge = document.getElementById('reader-outline-badge');
  const paperTab = document.getElementById('tab-active-paper');
  const paperChip = document.getElementById('chip-active-paper');
  const readerPane = document.getElementById('zotero-reader-pane');

  let currentCitationKey = '1';
  let openInReaderTimeout = null;
  let selectInLibTimeout = null;

  function updateEvidencePopover(key) {
    const item = CITATION_DATA[key];
    if (!item) return;
    currentCitationKey = key;

    pillButtons.forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.cite === key);
    });

    if (popoverTitle) popoverTitle.textContent = item.title;
    if (popoverConfidence) popoverConfidence.textContent = item.confidence;
    if (popoverSection) popoverSection.textContent = item.section;
    if (popoverQuote) popoverQuote.textContent = item.quote;
    if (outlineBadge && item.sectionBadge) {
      outlineBadge.textContent = item.sectionBadge;
    }
    const headingTitle = document.querySelector('#pdf-section-heading .heading-title');
    if (headingTitle && item.targetSectionTitle) {
      headingTitle.textContent = item.targetSectionTitle;
    }
  }

  pillButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      updateEvidencePopover(btn.dataset.cite);
    });
  });

  if (openInReaderBtn) {
    openInReaderBtn.addEventListener('click', () => {
      clearTimeout(openInReaderTimeout);
      const origText = '📖 Open in Reader';
      const targetHeading = document.getElementById('pdf-section-heading');
      const item = CITATION_DATA[currentCitationKey] || CITATION_DATA['1'];
      const sectionNum = item.section?.split(' ')[1] || '§ 3.2.2';

      // 1. In-place button feedback on the right
      openInReaderBtn.innerHTML = '✓ Opened at Section ◀';
      openInReaderBtn.style.color = 'var(--success)';
      openInReaderBtn.style.borderColor = 'var(--success)';

      if (popoverStatus) {
        popoverStatus.textContent = '◀ Jumped to section via PDF Outline';
        popoverStatus.classList.add('show');
      }

      // 2. High-visibility pulse on section heading & reader pane (highlight-free prose)
      if (targetHeading) {
        targetHeading.classList.remove('flash-active');
        void targetHeading.offsetWidth; // force reflow
        targetHeading.classList.add('flash-active');
      }
      if (readerPane) {
        readerPane.classList.remove('flash-pane');
        void readerPane.offsetWidth;
        readerPane.classList.add('flash-pane');
      }
      if (outlineBadge) {
        outlineBadge.classList.add('active-jump');
      }

      // 3. Update reader status bar (single-line, zero wrap)
      if (readerStatusLabel) {
        readerStatusLabel.textContent = `✓ Jumped to ${sectionNum} via PDF Outline`;
        readerStatusLabel.style.color = 'var(--success)';
      }

      openInReaderTimeout = setTimeout(() => {
        openInReaderBtn.innerHTML = origText;
        openInReaderBtn.style.color = '';
        openInReaderBtn.style.borderColor = '';
        if (popoverStatus) popoverStatus.classList.remove('show');
        if (targetHeading) targetHeading.classList.remove('flash-active');
        if (readerPane) readerPane.classList.remove('flash-pane');
        if (outlineBadge) outlineBadge.classList.remove('active-jump');
        if (readerStatusLabel) {
          readerStatusLabel.textContent = '📖 Zotero PDF Reader • Page 4 of 15';
          readerStatusLabel.style.color = '';
        }
      }, 2600);
    });
  }

  if (selectInLibBtn) {
    selectInLibBtn.addEventListener('click', () => {
      clearTimeout(selectInLibTimeout);
      const origText = '🗂️ Select in Library';
      selectInLibBtn.innerHTML = '✓ Selected in Library ◀';
      selectInLibBtn.style.color = 'var(--accent)';
      selectInLibBtn.style.borderColor = 'var(--accent)';

      if (popoverStatus) {
        popoverStatus.textContent = '◀ Paper focused in Zotero library';
        popoverStatus.classList.add('show');
      }

      if (paperTab) {
        paperTab.classList.remove('flash-tab');
        void paperTab.offsetWidth;
        paperTab.classList.add('flash-tab');
      }
      if (paperChip) {
        paperChip.classList.remove('flash-chip');
        void paperChip.offsetWidth;
        paperChip.classList.add('flash-chip');
      }

      selectInLibTimeout = setTimeout(() => {
        selectInLibBtn.innerHTML = origText;
        selectInLibBtn.style.color = '';
        selectInLibBtn.style.borderColor = '';
        if (popoverStatus) popoverStatus.classList.remove('show');
        if (paperTab) paperTab.classList.remove('flash-tab');
        if (paperChip) paperChip.classList.remove('flash-chip');
      }, 2600);
    });
  }

  const saveAsNoteBtn = document.getElementById('btn-save-as-note');
  if (saveAsNoteBtn) {
    saveAsNoteBtn.addEventListener('click', () => {
      const origText = saveAsNoteBtn.textContent;
      saveAsNoteBtn.textContent = '✓ Saved as Zotero Note!';
      saveAsNoteBtn.style.color = 'var(--success)';
      setTimeout(() => {
        saveAsNoteBtn.textContent = origText;
        saveAsNoteBtn.style.color = '';
      }, 2600);
    });
  }

  const copyMsgBtn = document.getElementById('btn-copy-msg');
  if (copyMsgBtn) {
    copyMsgBtn.addEventListener('click', () => {
      const orig = copyMsgBtn.textContent;
      copyMsgBtn.textContent = '✓ Copied!';
      copyMsgBtn.style.color = 'var(--accent)';
      setTimeout(() => {
        copyMsgBtn.textContent = orig;
        copyMsgBtn.style.color = '';
      }, 2000);
    });
  }

  /* Citations Visibility Toggle & Claim Verification Inspector Modal */
  const toggleCitationsBtn = document.getElementById('btn-toggle-citations');
  const heroAssistantBubble = document.querySelector('.chat-bubble-assistant');
  if (toggleCitationsBtn && heroAssistantBubble) {
    let citationsVisible = true;
    toggleCitationsBtn.addEventListener('click', () => {
      citationsVisible = !citationsVisible;
      heroAssistantBubble.classList.toggle('citations-hidden', !citationsVisible);
      if (citationsVisible) {
        toggleCitationsBtn.innerHTML = '👁️ Citations: Visible';
        toggleCitationsBtn.classList.add('active-pill');
        toggleCitationsBtn.classList.remove('muted-pill');
      } else {
        toggleCitationsBtn.innerHTML = '👁️ Citations: Hidden';
        toggleCitationsBtn.classList.remove('active-pill');
        toggleCitationsBtn.classList.add('muted-pill');
      }
    });
  }

  const claimsModal = document.getElementById('claims-inspector-modal');
  const openClaimsBtn = document.getElementById('btn-open-claims-inspector');
  const closeClaimsBtn = document.getElementById('claims-modal-close-btn');
  const claimsFilterSelect = document.getElementById('claims-filter-select');
  const claimsTableBody = document.getElementById('claims-table-body');

  function openClaimsInspector() {
    if (claimsModal) {
      claimsModal.classList.add('open');
      claimsModal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    }
  }

  function closeClaimsInspector() {
    if (claimsModal) {
      claimsModal.classList.remove('open');
      claimsModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
  }

  if (openClaimsBtn) {
    openClaimsBtn.addEventListener('click', openClaimsInspector);
  }
  if (closeClaimsBtn) {
    closeClaimsBtn.addEventListener('click', closeClaimsInspector);
  }
  if (claimsModal) {
    claimsModal.addEventListener('click', (e) => {
      if (e.target === claimsModal) {
        closeClaimsInspector();
      }
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && claimsModal && claimsModal.classList.contains('open')) {
      closeClaimsInspector();
    }
  });

  if (claimsFilterSelect && claimsTableBody) {
    claimsFilterSelect.addEventListener('change', () => {
      const selectedSource = claimsFilterSelect.value;
      const rows = claimsTableBody.querySelectorAll('tr');
      rows.forEach((row) => {
        if (selectedSource === 'all' || row.dataset.source === selectedSource) {
          row.style.display = '';
        } else {
          row.style.display = 'none';
        }
      });
    });
  }

  const msgInfoBtn = document.getElementById('btn-msg-info');
  if (msgInfoBtn) {
    msgInfoBtn.addEventListener('click', () => {
      const orig = msgInfoBtn.innerHTML;
      msgInfoBtn.innerHTML = '✓ 3 verified (p ≥ 0.600)';
      msgInfoBtn.style.color = 'var(--success)';
      setTimeout(() => {
        msgInfoBtn.innerHTML = orig;
        msgInfoBtn.style.color = '';
      }, 2600);
    });
  }

  /* ------------------------------------------------------------------------
     3. Hero Predictive Ghost Text Input Demo ([Tab ⇥] Acceptance)
     ------------------------------------------------------------------------ */
  const ghostBar = document.getElementById('hero-ghost-bar');
  const ghostText = document.getElementById('hero-ghost-text');
  const ghostBadge = document.getElementById('hero-ghost-badge');
  const originalGhostText = ghostText ? ghostText.textContent : '';
  let ghostAccepted = false;

  function toggleGhostAccept() {
    if (!ghostText || !ghostBadge) return;
    ghostAccepted = !ghostAccepted;
    if (ghostAccepted) {
      ghostText.classList.add('accepted');
      ghostBadge.textContent = 'Ready to Send ↵ (Click to Reset)';
    } else {
      ghostText.classList.remove('accepted');
      ghostText.textContent = originalGhostText;
      ghostBadge.textContent = 'Accept [Tab ⇥]';
    }
  }

  if (ghostBar) {
    ghostBar.addEventListener('click', toggleGhostAccept);
    ghostBar.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Tab') {
        e.preventDefault();
        toggleGhostAccept();
      }
    });
  }

  /* ------------------------------------------------------------------------
     4. Three Flexible Ways to Work — Mode Switcher & OKF Bundle Explorer
     ------------------------------------------------------------------------ */
  const modeTabBtns = document.querySelectorAll('.mode-tab-btn');
  const modePanels = document.querySelectorAll('.mode-panel');

  modeTabBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const targetMode = btn.dataset.mode;
      modeTabBtns.forEach((b) => b.classList.toggle('active', b === btn));
      modePanels.forEach((panel) => {
        panel.classList.toggle('active', panel.id === `mode-panel-${targetMode}`);
      });
    });
  });

  const BUNDLE_PREVIEWS = {
    'index.md': `# Root Knowledge Base Catalog (index.md)
Total Converted Bundles: 184 papers | 42 Zotero notes
- [Vaswani et al., 2017] Attention Is All You Need
  -> ./A8F92K1L/document.md
- [Dao et al., 2022] FlashAttention: Fast and Memory-Efficient...
  -> ./B3K91M4P/document.md
- [Jumper et al., 2021] Highly accurate protein structure...
  -> ./C9Q24X8W/document.md`,
    'AGENTS.md': `# Zocrates OKF v0.2 Navigation Protocol for Coding Agents
1. Inspect \`index.md\` to locate relevant attachment keys.
2. Read \`<item_key>/toc.md\` or \`toc.json\` first for section line offsets (s0..sN) & figure/table catalogs (f1..fN, t1..tN).
3. Read targeted line ranges in \`<item_key>/document.md\` and traverse \`item_relations.md\` for 1-hop corroborated papers.`,
    'document.md': `---
title: "Attention Is All You Need"
authors: ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"]
year: "2017"
publication: "Advances in Neural Information Processing Systems (NeurIPS)"
doi: "10.48550/arXiv.1706.03762"
source_pdf: "../../../storage/A8F92K1L/vaswani_2017.pdf"
---
## 3.2.2 Multi-Head Attention
Instead of performing a single attention function with d_model-dimensional keys...`,
    'toc.json': `{
  "sections": [
    { "id": "s0", "title": "Frontmatter & Abstract", "level": 1, "start_line": 1, "end_line": 38 },
    { "id": "s3", "title": "3. Model Architecture", "level": 1, "start_line": 112, "end_line": 265 }
  ],
  "figures": [
    { "id": "f1", "caption": "Figure 1: The Transformer - model architecture.", "line": 128 }
  ],
  "tables": [
    { "id": "t1", "caption": "Table 1: Maximum path lengths, per-layer complexity...", "line": 241 }
  ]
}`,
    'item_relations.md': `# Verified Literature Neighborhood (Evidence-Gated)
1. Dao et al. (2022) — FlashAttention [Key: B3K91M4P]
   • Corroboration: Semantic Match + Keyphrases
     ("multi-head attention", "self-attention complexity")
2. Beltagy et al. (2020) — Longformer [Key: K7M19P3X]
   • Corroboration: Zotero Manual Link + Vector Match (0.86)`
  };

  const treeItems = document.querySelectorAll('.tree-item');
  const treePreviewBox = document.getElementById('tree-preview-content');

  treeItems.forEach((item) => {
    item.addEventListener('click', () => {
      const fileKey = item.dataset.file;
      treeItems.forEach((el) => el.classList.toggle('active', el === item));
      if (treePreviewBox && BUNDLE_PREVIEWS[fileKey]) {
        treePreviewBox.textContent = BUNDLE_PREVIEWS[fileKey];
      }
    });
  });

  /* ------------------------------------------------------------------------
     5. Bento Widget 1: Citation Mode & Clean Manuscript Export Switcher
     ------------------------------------------------------------------------ */
  const citeModeBtns = document.querySelectorAll('.cite-mode-btn');
  const citeModeDisplay = document.getElementById('cite-mode-display');

  const CITE_MODE_HTML = {
    'pills': `
      <p style="font-size:0.86rem; color:var(--text-main);">
        Exact tiling algorithms eliminate redundant memory reads and writes during attention forward and backward passes
        <span class="citation-pill">Dao et al. (2022)</span>, achieving a 3.2× speedup over standard baselines
        <span class="citation-pill">Dao (2023)</span>.
      </p>
      <div style="font-size:0.75rem; color:var(--success); display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
        <span>✓ Claim Verification: Both assertions verified against source papers</span>
        <span>• If a paper does not support a claim, the unsupported citation is automatically pruned</span>
      </div>
    `,
    'inspector': `
      <div style="display:flex; flex-direction:column; gap:0.6rem;">
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.5rem;">
          <span style="font-size:0.78rem; font-weight:700; color:var(--text-main);">
            🛡️ Claim Verification Inspector Preview (2/2 Claims Verified)
          </span>
          <button type="button" class="btn btn-primary btn-sm open-full-inspector-btn" style="padding:0.25rem 0.55rem; font-size:0.74rem;">
            🔍 Open Full Inspector Window
          </button>
        </div>
        <div class="claims-table-wrap" style="max-height:220px; overflow-y:auto;">
          <table class="claims-table" style="font-size:0.77rem;">
            <thead>
              <tr>
                <th style="width:28px;">#</th>
                <th>Claim Sentence</th>
                <th>Source &amp; Section</th>
                <th style="text-align:center;">Prob (p)</th>
                <th style="text-align:center;">Verdict</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><span class="claim-idx-badge">#1</span></td>
                <td>Exact tiling algorithms eliminate redundant memory reads and writes during attention passes.</td>
                <td>Dao et al. (2022)<br/><span class="claim-section-tag">§ 2 FlashAttention</span></td>
                <td style="text-align:center;"><span class="claim-prob-val">0.938</span></td>
                <td style="text-align:center;"><span class="claim-verdict-badge verified">✓ VERIFIED</span></td>
              </tr>
              <tr>
                <td><span class="claim-idx-badge">#2</span></td>
                <td>Achieving a 3.2× speedup over standard attention baselines on A100 GPUs.</td>
                <td>Dao (2023)<br/><span class="claim-section-tag">§ 4 Empirical Evaluation</span></td>
                <td style="text-align:center;"><span class="claim-prob-val">0.912</span></td>
                <td style="text-align:center;"><span class="claim-verdict-badge verified">✓ VERIFIED</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div style="font-size:0.73rem; color:var(--text-muted); display:flex; justify-content:space-between; flex-wrap:wrap;">
          <span>Model: ~typesafe/jev-latest (Threshold τ = 0.600)</span>
          <span style="color:var(--success); font-weight:600;">Mean p: 0.925 ± 0.018</span>
        </div>
      </div>
    `,
    'manuscript': `
      <p style="font-size:0.86rem; color:var(--text-main);">
        Exact tiling algorithms eliminate redundant memory reads and writes during attention forward and backward passes (Dao et al., 2022), achieving a 3.2× speedup over standard baselines (Dao, 2023).
      </p>
      <div style="font-size:0.74rem; color:var(--text-muted); border-top:1px solid var(--border); padding-top:0.45rem;">
        <strong>Auto-Formatted References on Copy (APA):</strong><br/>
        • Dao, T., Fu, D. Y., Ermon, S., Rudra, A., &amp; Ré, C. (2022). FlashAttention: Fast and memory-efficient exact attention.<br/>
        • Dao, T. (2023). FlashAttention-2: Faster attention with better parallelism and work partitioning.
      </div>
    `,
    'clean': `
      <p style="font-size:0.86rem; color:var(--text-main);">
        Exact tiling algorithms eliminate redundant memory reads and writes during attention forward and backward passes, achieving a 3.2× speedup over standard baselines.
      </p>
      <div style="font-size:0.75rem; color:var(--accent);">
        ✓ Distraction-Free Reading: In-text citations hidden for seamless reading and clean copy-pasting.
      </div>
    `
  };

  citeModeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.citemode;
      citeModeBtns.forEach((b) => b.classList.toggle('active', b === btn));
      if (citeModeDisplay && CITE_MODE_HTML[mode]) {
        citeModeDisplay.innerHTML = CITE_MODE_HTML[mode];
      }
    });
  });

  if (citeModeDisplay) {
    citeModeDisplay.addEventListener('click', (e) => {
      if (e.target && e.target.closest('.open-full-inspector-btn')) {
        openClaimsInspector();
      }
    });
  }

  /* ------------------------------------------------------------------------
     6. Bento Widget 2: Online Discovery & 1-Click Import Simulator
     ------------------------------------------------------------------------ */
  const candidateItems = document.querySelectorAll('.candidate-card-item');
  const importBtn = document.getElementById('btn-simulate-import');
  const importStatus = document.getElementById('import-sim-status');

  candidateItems.forEach((card) => {
    card.addEventListener('click', () => {
      card.classList.toggle('checked');
      const checkbox = card.querySelector('input[type="checkbox"]');
      if (checkbox) checkbox.checked = card.classList.contains('checked');
    });
  });

  if (importBtn) {
    importBtn.addEventListener('click', () => {
      const selectedCount = document.querySelectorAll('.candidate-card-item.checked').length;
      if (selectedCount === 0) {
        if (importStatus) {
          importStatus.textContent = 'Select at least one novel candidate card above to import.';
          importStatus.style.color = 'var(--warning)';
        }
        return;
      }
      importBtn.disabled = true;
      importBtn.textContent = 'Staging OA PDFs (%PDF- verified)...';
      if (importStatus) {
        importStatus.textContent = `Downloading ${selectedCount} Open Access PDF(s) via Unpaywall & writing items into Zotero collection "Efficient Transformers"...`;
        importStatus.style.color = 'var(--accent)';
      }
      setTimeout(() => {
        importBtn.textContent = `✓ Imported ${selectedCount} Paper(s) into Zotero!`;
        if (importStatus) {
          importStatus.textContent = `✓ Atomic write-back complete! Auto-converter queued ${selectedCount} bundle(s) for instant chat readiness.`;
          importStatus.style.color = 'var(--success)';
        }
        setTimeout(() => {
          importBtn.disabled = false;
          importBtn.textContent = 'Import Selected to Zotero';
        }, 3200);
      }, 900);
    });
  }

  /* ------------------------------------------------------------------------
     Bento Widget 4: User-Curated Long-Term Memory via Native Zotero Notes
     ------------------------------------------------------------------------ */
  const btnSaveNoteSim = document.getElementById('btn-save-note-sim');
  const btnSaveSessionSim = document.getElementById('btn-save-session-sim');
  const noteSimStatus = document.getElementById('note-sim-status');

  if (btnSaveNoteSim && noteSimStatus) {
    btnSaveNoteSim.addEventListener('click', () => {
      btnSaveNoteSim.disabled = true;
      btnSaveNoteSim.textContent = 'Saving Note…';
      setTimeout(() => {
        btnSaveNoteSim.disabled = false;
        btnSaveNoteSim.textContent = '💾 Save Message as Note';
        noteSimStatus.textContent = '✓ Created child note under "Vaswani et al. (2017)"! Indexed into local vector & FTS5 search—ready for future chat queries.';
        noteSimStatus.style.color = 'var(--success)';
      }, 500);
    });
  }

  if (btnSaveSessionSim && noteSimStatus) {
    btnSaveSessionSim.addEventListener('click', () => {
      btnSaveSessionSim.disabled = true;
      btnSaveSessionSim.textContent = 'Saving Session…';
      setTimeout(() => {
        btnSaveSessionSim.disabled = false;
        btnSaveSessionSim.textContent = '📚 Save Full Session as Note';
        noteSimStatus.textContent = '✓ Saved entire research session as standalone Zotero note! Automatically re-indexed for future cross-session recall.';
        noteSimStatus.style.color = 'var(--accent)';
      }, 500);
    });
  }

  /* ------------------------------------------------------------------------
     7. Bento Widget 3: Interactive HTML5 Canvas 2D Paper Relations Graph
     ------------------------------------------------------------------------ */
  const canvas = document.getElementById('relations-canvas');
  const graphInspectorText = document.getElementById('graph-inspector-text');

  const GRAPH_NODES = [
    // Cluster 1: Efficient Attention (Indigo)
    { id: 0, label: 'Vaswani 2017', cluster: 'Cluster A: Attention Architectures', x: 0.24, y: 0.34, r: 9, color: '#6366f1', info: 'Vaswani et al. (2017) • 3 verified edges • Keyphrases: "multi-head attention", "scaled dot-product" • 100% Deterministic Local Algorithm' },
    { id: 1, label: 'Dao 2022', cluster: 'Cluster A: Attention Architectures', x: 0.38, y: 0.24, r: 8, color: '#6366f1', info: 'Dao et al. (2022) • 3 verified edges • Keyphrases: "IO-aware tiling", "HBM bandwidth" • 100% Deterministic Local Algorithm' },
    { id: 2, label: 'Beltagy 2020', cluster: 'Cluster A: Attention Architectures', x: 0.16, y: 0.66, r: 7, color: '#6366f1', info: 'Beltagy et al. (2020) • 2 verified edges • Keyphrases: "sliding window attention", "linear scaling" • 100% Deterministic Local Algorithm' },
    { id: 3, label: 'Dao 2023', cluster: 'Cluster A: Attention Architectures', x: 0.39, y: 0.62, r: 8, color: '#6366f1', info: 'Dao (2023) FlashAttention-2 • 3 verified edges • Keyphrases: "warp partitioning", "sequence parallelism" • 100% Deterministic Local Algorithm' },
    // Cluster 2: Protein Folding (Cyan)
    { id: 4, label: 'Jumper 2021', cluster: 'Cluster B: Biomolecular Folding', x: 0.66, y: 0.32, r: 9, color: '#06b6d4', info: 'Jumper et al. (2021) AlphaFold2 • 2 verified edges • Keyphrases: "evoformer", "invariant point attention" • 100% Deterministic Local Algorithm' },
    { id: 5, label: 'Abramson 2024', cluster: 'Cluster B: Biomolecular Folding', x: 0.78, y: 0.56, r: 8, color: '#06b6d4', info: 'Abramson et al. (2024) AlphaFold3 • 2 verified edges • Keyphrases: "diffusion module", "pairformer" • 100% Deterministic Local Algorithm' },
    { id: 6, label: 'Lin 2023 (ESM-2)', cluster: 'Cluster B: Biomolecular Folding', x: 0.58, y: 0.68, r: 7, color: '#06b6d4', info: 'Lin et al. (2023) ESM-2 • 2 verified edges • Keyphrases: "protein language model", "contact prediction" • 100% Deterministic Local Algorithm' },
    // Unconnected Papers (Emerald / Muted Slate)
    { id: 7, label: 'Carnot 1824 (Unconnected)', cluster: 'Unconnected Paper (0 Spurious Edges)', x: 0.89, y: 0.22, r: 6, color: '#10b981', orphan: true, info: 'Unconnected Paper: Classical Thermodynamics (1824) • 0 edges — Evidence Gate blocked weak uncorroborated links! Computed 100% locally with deterministic algorithms.' },
    { id: 8, label: 'Coral Reefs 2021 (Unconnected)', cluster: 'Unconnected Paper (0 Spurious Edges)', x: 0.90, y: 0.78, r: 6, color: '#10b981', orphan: true, info: 'Unconnected Paper: Marine Ecology Survey (2021) • 0 edges — Deterministic local algorithms never hallucinate links just to fill a graph.' }
  ];

  const GRAPH_EDGES = [
    [0, 1], [0, 2], [0, 3], [1, 3], [2, 3],
    [4, 5], [4, 6], [5, 6],
    [0, 4] // Cross-cluster bridge (Attention -> Evoformer)
  ];

  let selectedNodeId = 0;

  function drawRelationsGraph() {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    const isLight = ['zotero-light', 'sepia'].includes(document.body.getAttribute('data-theme'));

    ctx.clearRect(0, 0, w, h);

    // Draw Edges
    GRAPH_EDGES.forEach(([i, j]) => {
      const n1 = GRAPH_NODES[i];
      const n2 = GRAPH_NODES[j];
      const isHighlighted = selectedNodeId === i || selectedNodeId === j;
      ctx.beginPath();
      ctx.moveTo(n1.x * w, n1.y * h);
      ctx.lineTo(n2.x * w, n2.y * h);
      ctx.strokeStyle = isHighlighted
        ? 'rgba(99, 102, 241, 0.85)'
        : (isLight ? 'rgba(100, 116, 139, 0.28)' : 'rgba(148, 163, 184, 0.22)');
      ctx.lineWidth = isHighlighted ? 2.2 : 1.2;
      ctx.stroke();
    });

    // Draw Nodes
    GRAPH_NODES.forEach((node) => {
      const nx = node.x * w;
      const ny = node.y * h;
      const isSel = node.id === selectedNodeId;

      if (isSel) {
        ctx.beginPath();
        ctx.arc(nx, ny, node.r + 6, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(99, 102, 241, 0.25)';
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(nx, ny, node.r, 0, Math.PI * 2);
      ctx.fillStyle = node.color;
      ctx.fill();
      ctx.lineWidth = isSel ? 2.5 : 1.5;
      ctx.strokeStyle = isLight ? '#ffffff' : '#0f172a';
      ctx.stroke();

      ctx.font = `${isSel ? '700' : '500'} 11px Inter, sans-serif`;
      ctx.fillStyle = isLight ? '#1e293b' : '#e2e8f0';
      ctx.textAlign = 'center';
      ctx.fillText(node.label, nx, ny - node.r - 6);
    });
  }

  window._relationsGraphReady = true;

  if (canvas) {
    drawRelationsGraph();
    window.addEventListener('resize', drawRelationsGraph);

    canvas.addEventListener('click', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const w = rect.width;
      const h = rect.height;

      for (const node of GRAPH_NODES) {
        const dx = mx - node.x * w;
        const dy = my - node.y * h;
        if (Math.sqrt(dx * dx + dy * dy) <= node.r + 10) {
          selectedNodeId = node.id;
          if (graphInspectorText) {
            graphInspectorText.textContent = node.info;
          }
          drawRelationsGraph();
          break;
        }
      }
    });
  }

  /* ------------------------------------------------------------------------
     8. Bento Widget 4: Deep Research Investigation Checkpoint Demo
     ------------------------------------------------------------------------ */
  const hitlExtendBtn = document.getElementById('btn-hitl-extend');
  const hitlSynthBtn = document.getElementById('btn-hitl-synthesize');
  const hitlStatusBox = document.getElementById('hitl-budget-status');

  if (hitlExtendBtn && hitlStatusBox) {
    hitlExtendBtn.addEventListener('click', () => {
      hitlStatusBox.innerHTML = `
        <strong style="color:var(--success);">✓ Investigation Extended:</strong>
        Broadening search to connected papers and conference proceedings to resolve open questions...
      `;
    });
  }

  if (hitlSynthBtn && hitlStatusBox) {
    hitlSynthBtn.addEventListener('click', () => {
      hitlStatusBox.innerHTML = `
        <strong style="color:var(--accent);">✓ Synthesizing Evidence:</strong>
        Cross-checking all identified findings against source passages and preparing comprehensive response...
      `;
    });
  }

  /* ------------------------------------------------------------------------
     9. Bento Widget 5: Mermaid Diagram Code / Visual Toggle
     ------------------------------------------------------------------------ */
  const mermaidToggleBtn = document.getElementById('btn-toggle-mermaid');
  const mermaidVisualView = document.getElementById('mermaid-visual-view');
  const mermaidCodeView = document.getElementById('mermaid-code-view');
  let showingMermaidCode = false;

  if (mermaidToggleBtn && mermaidVisualView && mermaidCodeView) {
    mermaidToggleBtn.addEventListener('click', () => {
      showingMermaidCode = !showingMermaidCode;
      mermaidVisualView.style.display = showingMermaidCode ? 'none' : 'flex';
      mermaidCodeView.style.display = showingMermaidCode ? 'block' : 'none';
      mermaidToggleBtn.textContent = showingMermaidCode ? 'Render Diagram' : 'Show Raw Mermaid Code';
    });
  }

  /* ------------------------------------------------------------------------
     10. Value Matrix Search Filter
     ------------------------------------------------------------------------ */
  const matrixSearch = document.getElementById('matrix-search');
  const matrixRows = document.querySelectorAll('#value-matrix-tbody tr');

  if (matrixSearch) {
    matrixSearch.addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      matrixRows.forEach((row) => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(q) ? '' : 'none';
      });
    });
  }
});

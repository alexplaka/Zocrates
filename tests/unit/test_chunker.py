"""Unit tests for the Markdown chunking pipeline."""

from zocrates.rag.chunker import chunk_markdown


def test_chunk_markdown_header_splitting():
    """Verify that H1/H2/H3 headers produce correctly structured ChunkResult objects.

    Asserts the heading path, text content, chunk index, and document title
    for each of the four expected chunks produced from a three-level header
    document.
    """
    text = (
        "# Introduction\n"
        "This is the introduction section.\n"
        "\n"
        "## Background\n"
        "Some details about background.\n"
        "### Detail level 3\n"
        "Deep detail.\n"
        "\n"
        "# Another Section\n"
        "Content of section 2."
    )

    chunks = chunk_markdown(
        text, max_chars=1000, overlap_chars=0, document_title="Test Title"
    )

    assert len(chunks) == 4

    assert chunks[0].heading_str == "Introduction"
    assert "This is the introduction section" in chunks[0].text
    assert chunks[0].chunk_index == 0
    assert chunks[0].document_title == "Test Title"

    assert chunks[1].heading_str == "Introduction > Background"
    assert "Some details about background" in chunks[1].text
    assert chunks[1].chunk_index == 1
    assert chunks[1].document_title == "Test Title"

    assert chunks[2].heading_str == "Introduction > Background > Detail level 3"
    assert "Deep detail" in chunks[2].text
    assert chunks[2].chunk_index == 2
    assert chunks[2].document_title == "Test Title"

    assert chunks[3].heading_str == "Another Section"
    assert "Content of section 2" in chunks[3].text
    assert chunks[3].chunk_index == 3
    assert chunks[3].document_title == "Test Title"


def test_chunk_markdown_recursive_splitting():
    """Verify that long flat text with no headers is split into size-bounded chunks.

    Asserts that more than one chunk is produced and that every chunk stays
    within the specified character limit.
    """
    # Long text with no headers
    text = "Word " * 500  # 2500 chars

    chunks = chunk_markdown(text, max_chars=100, overlap_chars=10)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 100
        assert chunk.heading_path == []


def test_chunk_markdown_code_block_protection():
    """Verify that ``#``-prefixed lines inside fenced code blocks are not treated as headers.

    Asserts that only the two real ATX headers produce section boundaries and
    that the raw fenced code block content is preserved verbatim in the chunk
    text.
    """
    text = (
        "# Real Header\n"
        "Here is some text before code.\n"
        "\n"
        "```python\n"
        "# This is a comment inside code block\n"
        "def foo():\n"
        "    ## This is not a header\n"
        "    pass\n"
        "```\n"
        "\n"
        "~~~bash\n"
        "### Not a header either\n"
        "echo 'hello'\n"
        "~~~\n"
        "\n"
        "## Second Real Header\n"
        "After code block."
    )

    chunks = chunk_markdown(
        text, max_chars=1000, overlap_chars=0, document_title="Code Block Test"
    )

    assert len(chunks) == 2

    assert chunks[0].heading_str == "Real Header"
    assert "# This is a comment inside code block" in chunks[0].text
    assert "## This is not a header" in chunks[0].text
    assert "### Not a header either" in chunks[0].text
    assert "```python" in chunks[0].text
    assert "~~~bash" in chunks[0].text

    assert chunks[1].heading_str == "Real Header > Second Real Header"
    assert "After code block." in chunks[1].text


def test_chunk_markdown_figure_and_table_atomicity():
    """Verify that <figure> blocks for images and tables are kept intact without paragraph splitting."""
    text = (
        "# Results\n\n"
        "Here are the findings:\n\n"
        '<figure data-type="image" data-id="figure-001">\n\n'
        "![Figure 1](file:///path/to/img.png)\n\n"
        "**Figure 1.** Detailed mutation spectrum caption spanning multiple sentences.\n\n"
        "</figure>\n\n"
        "Now consider the tabular data:\n\n"
        '<figure data-type="table" data-id="table-001" data-csv="tables/table_000.csv">\n\n'
        "**Table 1.** Summary statistics.\n\n"
        "| Col1 | Col2 |\n"
        "|---|---|\n"
        "| A | B |\n\n"
        "<!-- artifacts: tables/table_000.csv -->\n"
        "</figure>\n\n"
        "Conclusion paragraph."
    )

    chunks = chunk_markdown(text, max_chars=400, overlap_chars=0)
    figure_chunks = [c for c in chunks if c.chunk_type == "figure"]
    table_chunks = [c for c in chunks if c.chunk_type == "table"]

    assert len(figure_chunks) == 1
    assert "![Figure 1]" in figure_chunks[0].text
    assert "**Figure 1.** Detailed mutation spectrum" in figure_chunks[0].text
    assert figure_chunks[0].chunk_type == "figure"

    assert len(table_chunks) == 1
    assert "**Table 1.** Summary statistics." in table_chunks[0].text
    assert "| Col1 | Col2 |" in table_chunks[0].text
    assert table_chunks[0].chunk_type == "table"


def test_determine_chunk_type_taxonomy():
    """Verify classification of frontmatter, figure, table, equation, code, and text chunks."""
    from zocrates.rag.chunker import determine_chunk_type

    fm_text = "---\ntype: journalArticle\ntitle: Paper Title\nauthors:\n  - Author Name\n---"
    assert determine_chunk_type(fm_text) == "frontmatter"

    fig_text = '<figure data-type="image">\n![Figure](path)\nCaption\n</figure>'
    assert determine_chunk_type(fig_text) == "figure"

    tbl_text = '<figure data-type="table">\n| A | B |\n|---|---|\n| 1 | 2 |\n</figure>'
    assert determine_chunk_type(tbl_text) == "table"

    raw_tbl = "| X | Y |\n|---|---|\n| 10 | 20 |"
    assert determine_chunk_type(raw_tbl) == "table"

    code_text = "```python\nprint('hello')\n```"
    assert determine_chunk_type(code_text) == "code"

    eq_text = "We formulate the Hamiltonian as:\n$$\n\\hat{H} = \\sum_i \\frac{p_i^2}{2m} + V(x)\n$$\nwhere p is momentum."
    assert determine_chunk_type(eq_text) == "equation"

    plain_text = "This is a normal narrative paragraph explaining the methodology."
    assert determine_chunk_type(plain_text) == "text"


def test_figure_tag_stripping_and_prose_stitching():
    """Verify that outer <figure> tags are stripped from chunk text and narrative prose is stitched."""
    text = (
        "## Section\n\n"
        "Paragraph before figure explaining the concept.\n\n"
        '<figure data-type="image">\n\n'
        "![Figure 1](images/fig1.png)\n\n"
        "<synthetic_caption>\nVLM description\n</synthetic_caption>\n\n"
        "**Figure 1.** Author caption.\n\n"
        "</figure>\n\n"
        "Paragraph after figure continuing the discussion."
    )

    chunks = chunk_markdown(text, max_chars=4000, overlap_chars=0)
    assert len(chunks) == 2

    # Chunk 0: Stitched prose (both paragraphs merged)
    prose_chunk = chunks[0]
    assert prose_chunk.chunk_type == "text"
    assert "Paragraph before figure" in prose_chunk.text
    assert "Paragraph after figure" in prose_chunk.text
    assert "<figure" not in prose_chunk.text
    assert "</figure>" not in prose_chunk.text

    # Chunk 1: Isolated figure chunk with outer tags stripped
    fig_chunk = chunks[1]
    assert fig_chunk.chunk_type == "figure"
    assert "![Figure 1](images/fig1.png)" in fig_chunk.text
    assert "**Figure 1.** Author caption." in fig_chunk.text
    assert "<figure" not in fig_chunk.text
    assert "</figure>" not in fig_chunk.text


def test_anchor_interleaving_order():
    """Verify that start-of-section, middle, and end-of-section figures maintain document order."""
    text = (
        "# Analysis\n\n"
        '<figure data-type="image">\n![Fig 0](img0.png)\n**Fig 0.** Start fig.\n</figure>\n\n'
        "Introduction paragraph here.\n\n"
        '<figure data-type="table">\n**Table 1.** Mid table.\n| A |\n|---|\n| 1 |\n</figure>\n\n'
        "Middle narrative paragraph.\n\n"
        '<figure data-type="image">\n![Fig 2](img2.png)\n**Fig 2.** End fig.\n</figure>'
    )

    chunks = chunk_markdown(text, max_chars=4000, overlap_chars=0)
    assert len(chunks) == 4

    # Order: Fig 0 (start) -> Stitched Prose -> Table 1 (mid) -> Fig 2 (end)
    assert chunks[0].chunk_type == "figure"
    assert "![Fig 0]" in chunks[0].text

    assert chunks[1].chunk_type == "text"
    assert "Introduction paragraph here." in chunks[1].text
    assert "Middle narrative paragraph." in chunks[1].text

    assert chunks[2].chunk_type == "table"
    assert "**Table 1.** Mid table." in chunks[2].text

    assert chunks[3].chunk_type == "figure"
    assert "![Fig 2]" in chunks[3].text


def test_table_with_cell_image_classification():
    """Verify that a table containing an image thumbnail in a cell is classified as 'table'."""
    text = (
        "# Results\n\n"
        '<figure data-type="table">\n\n'
        "**Table 8.** Case study.\n\n"
        "| Inputs | Model Output | Ground Truth |\n"
        "| --- | --- | --- |\n"
        "| ![Protein structure](images/protein.jpg) [O58736] | Predicted activity | True activity |\n\n"
        "</figure>"
    )

    chunks = chunk_markdown(text, max_chars=4000, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "table"
    assert "**Table 8.** Case study." in chunks[0].text
    assert "![Protein structure]" in chunks[0].text
    assert "<figure" not in chunks[0].text


def test_code_block_with_table_or_image_syntax_classified_as_code():
    """Verify that a code block illustrating markdown tables or images is classified as 'code'."""
    from zocrates.rag.chunker import determine_chunk_type

    code_with_table = (
        "```markdown\n"
        "# Markdown table example\n"
        "| Name | Age |\n"
        "| --- | --- |\n"
        "| Alice | 30 |\n"
        "```"
    )
    assert determine_chunk_type(code_with_table) == "code"

    code_with_image = (
        "```python\n"
        "# Print markdown image link\n"
        "print('![Image](https://example.com/img.png)')\n"
        "```"
    )
    assert determine_chunk_type(code_with_image) == "code"


def test_section_with_only_figures_produces_no_empty_prose():
    """Verify that a section containing only figures produces no empty text chunks."""
    text = (
        "## Architecture Overview\n\n"
        '<figure data-type="image">\n![Arch](arch.png)\n**Fig A.** System overview.\n</figure>\n\n'
        '<figure data-type="image">\n![Flow](flow.png)\n**Fig B.** Workflow.\n</figure>'
    )

    chunks = chunk_markdown(text, max_chars=4000, overlap_chars=0)
    assert len(chunks) == 2
    assert chunks[0].chunk_type == "figure"
    assert "![Arch]" in chunks[0].text
    assert chunks[1].chunk_type == "figure"
    assert "![Flow]" in chunks[1].text


def test_real_world_qnvrfyqm_document_fixture():
    """Verify chunking and classification on the permanent QNVRFYQM document fixture."""
    from pathlib import Path

    fixture_path = (
        Path(__file__).parent.parent / "fixtures" / "documents" / "QNVRFYQM_document.md"
    )
    assert fixture_path.exists(), f"Fixture file missing: {fixture_path}"

    text = fixture_path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text)

    # 1. Total chunk count and sequential 0-indexed ordering
    assert len(chunks) == 32
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    # 2. No chunk text should contain <figure or </figure>
    for c in chunks:
        assert "<figure" not in c.text, f"Chunk {c.chunk_index} contains <figure"
        assert "</figure>" not in c.text, f"Chunk {c.chunk_index} contains </figure>"

    # 3. No chunk should contain 3 or more consecutive newlines (clean whitespace)
    for c in chunks:
        assert "\n\n\n" not in c.text, f"Chunk {c.chunk_index} contains redundant blank lines"

    # 4. Verify all Tables (Table 1 through Table 8) are extracted and classified as 'table'
    for table_num in range(1, 9):
        pattern = f"**Table {table_num}.**"
        matching_chunks = [c for c in chunks if pattern in c.text]
        assert len(matching_chunks) == 1, f"Expected exactly 1 chunk for {pattern}, found {len(matching_chunks)}"
        assert matching_chunks[0].chunk_type == "table", f"Table {table_num} misclassified as {matching_chunks[0].chunk_type}"

    # Specifically verify Table 8 with cell thumbnail is pure table
    t8_chunks = [c for c in chunks if "**Table 8.**" in c.text]
    assert len(t8_chunks) == 1
    assert t8_chunks[0].chunk_type == "table"
    assert "![Protein structure diagram]" in t8_chunks[0].text

    # 5. Verify all Figures (Fig. 1, Fig. 2, Fig. 3) are extracted and classified as 'figure'
    for fig_num in range(1, 4):
        pattern = f"**Fig. {fig_num}.**"
        matching_chunks = [c for c in chunks if pattern in c.text]
        assert len(matching_chunks) == 1, f"Expected exactly 1 chunk for {pattern}, found {len(matching_chunks)}"
        assert matching_chunks[0].chunk_type == "figure", f"Figure {fig_num} misclassified as {matching_chunks[0].chunk_type}"
        assert f"![Fig. {fig_num}]" in matching_chunks[0].text

    # 6. Verify display math sections (ProteinMPNN equations) are classified as 'equation'
    eq_chunks = [c for c in chunks if c.chunk_type == "equation"]
    assert len(eq_chunks) >= 2
    for eq_c in eq_chunks:
        assert "$$" in eq_c.text or r"\[" in eq_c.text

    # 7. Verify uninterrupted Introduction narrative paragraphs are classified as 'text'
    intro_chunks = [
        c for c in chunks
        if "Introduction" in c.heading_path and c.chunk_type == "text"
    ]
    assert len(intro_chunks) >= 2
    # Verify the stitched sentence continuity across Figure 1 inside the same intro chunk
    assert "reducing the models' effectiveness in" in intro_chunks[0].text
    assert "handling complex protein-related questions" in intro_chunks[0].text


def test_consecutive_figures_leave_no_multi_blank_line_gaps():
    """Verify that multiple consecutive figures do not leave large blank gaps in stitched prose."""
    text = (
        "# Method\n\n"
        "Prose before consecutive figures.\n\n"
        '<figure data-type="table">\n**Table 1.**\n| A |\n|---|\n| 1 |\n</figure>\n\n'
        '<figure data-type="table">\n**Table 2.**\n| B |\n|---|\n| 2 |\n</figure>\n\n'
        '<figure data-type="table">\n**Table 3.**\n| C |\n|---|\n| 3 |\n</figure>\n\n'
        "Prose immediately after consecutive figures."
    )

    chunks = chunk_markdown(text, max_chars=4000, overlap_chars=0)
    prose_chunks = [c for c in chunks if c.chunk_type == "text"]
    assert len(prose_chunks) == 1
    assert "\n\n\n" not in prose_chunks[0].text
    assert prose_chunks[0].text == "Prose before consecutive figures.\n\nProse immediately after consecutive figures."


def test_real_world_arsfjkpa_docling_document_fixture():
    """Verify chunking and classification on the permanent ARSFJKPA Docling document fixture."""
    from pathlib import Path

    fixture_path = (
        Path(__file__).parent.parent / "fixtures" / "documents" / "ARSFJKPA_document.md"
    )
    assert fixture_path.exists(), f"Fixture file missing: {fixture_path}"

    text = fixture_path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text)

    # 1. Total chunk count and monotonic 0-indexing
    assert len(chunks) == 59
    assert [c.chunk_index for c in chunks] == list(range(59))

    # 2. No chunk text should contain <figure or </figure>
    for c in chunks:
        assert "<figure" not in c.text, f"Chunk {c.chunk_index} contains <figure"
        assert "</figure>" not in c.text, f"Chunk {c.chunk_index} contains </figure>"

    # 3. No chunk should contain 3 or more consecutive newlines (clean whitespace)
    for c in chunks:
        assert "\n\n\n" not in c.text, f"Chunk {c.chunk_index} contains redundant blank lines"

    # 4. Exact figure count (20) with chunk_type == "figure"
    figure_chunks = [c for c in chunks if c.chunk_type == "figure"]
    assert len(figure_chunks) == 20
    for fig_c in figure_chunks:
        assert "![" in fig_c.text or "images/" in fig_c.text or "picture_" in fig_c.text

    # 5. Exact table count (2) with chunk_type == "table" containing markdown table syntax
    table_chunks = [c for c in chunks if c.chunk_type == "table"]
    assert len(table_chunks) == 2
    for tbl_c in table_chunks:
        assert "|" in tbl_c.text and "\n|---" in tbl_c.text

    # 6. Frontmatter chunk count (1) and exact text chunk count (36)
    fm_chunks = [c for c in chunks if c.chunk_type == "frontmatter"]
    assert len(fm_chunks) == 1
    assert chunks[0].chunk_type == "frontmatter"
    assert chunks[0].text.startswith("---\n")
    assert chunks[0].text.endswith("---")
    assert "Differences between germline genomes of monozygotic twins" in chunks[0].text

    text_chunks = [c for c in chunks if c.chunk_type == "text"]
    assert len(text_chunks) == 36
    for txt_c in text_chunks:
        assert len(txt_c.text) <= 4000

    # 7. Section heading propagation and narrative prose stitching in Results and Discussion
    results_text = [c for c in text_chunks if "Results" in c.heading_path]
    assert len(results_text) == 7
    for rc in results_text:
        assert rc.heading_str == "Results"

    discussion_text = [c for c in text_chunks if "Discussion" in c.heading_path]
    assert len(discussion_text) == 2
    for dc in discussion_text:
        assert dc.heading_str == "Discussion"


def test_figure_with_synthetic_caption_vlm_metadata():
    """Verify that figure blocks with synthetic captions extract metadata and format clean Visual Summary."""
    text = (
        "# Results\n\n"
        "Narrative before figure.\n\n"
        '<figure data-type="image">\n'
        "![Figure 2](file:///home/alex/zocrates-data/corpus/ABC/images/page_2_fig_1.png)\n"
        "**Figure 2.** Training and validation curves.\n"
        '<synthetic_caption data-source="vlm_remote" data-model="openrouter/google/gemma-4-26b-a4b-it:free">\n'
        "**Type**: Multi-panel Line Plot.\n"
        "**Key Elements**: Left: Epochs (0-100) vs Loss. Right: Validation Accuracy.\n"
        "**Takeaway**: Proposed model converges 30% faster.\n"
        "</synthetic_caption>\n"
        "</figure>\n\n"
        "Narrative after figure."
    )

    chunks = chunk_markdown(text)
    assert len(chunks) == 2

    # Chunk 0 is stitched narrative
    assert chunks[0].chunk_type == "text"
    assert "Narrative before figure." in chunks[0].text
    assert "Narrative after figure." in chunks[0].text

    # Chunk 1 is figure chunk
    fig_chunk = chunks[1]
    assert fig_chunk.chunk_type == "figure"
    assert fig_chunk.image_path == "images/page_2_fig_1.png"
    assert fig_chunk.caption_source == "vlm_remote"
    assert fig_chunk.vlm_model == "openrouter/google/gemma-4-26b-a4b-it:free"

    # Verify no raw HTML tags in indexed text
    assert "<figure" not in fig_chunk.text
    assert "</figure>" not in fig_chunk.text
    assert "<synthetic_caption" not in fig_chunk.text
    assert "</synthetic_caption>" not in fig_chunk.text
    assert "[Visual Summary]:" in fig_chunk.text
    assert "**Type**: Multi-panel Line Plot." in fig_chunk.text
    assert "**Figure 2.** Training and validation curves." in fig_chunk.text


def test_figure_synthetic_caption_heuristic_classification():
    """Verify heuristic classification of caption_source when data-source attribute is omitted."""
    vlm_text = (
        '<figure data-type="image">\n'
        "![Figure 1](images/fig1.png)\n"
        "<synthetic_caption>\n"
        "**Type**: Architecture Diagram.\n"
        "**Key Elements**: Encoder and Decoder blocks.\n"
        "**Takeaway**: Transformer pipeline.\n"
        "</synthetic_caption>\n"
        "</figure>"
    )
    chunks_vlm = chunk_markdown(vlm_text)
    assert len(chunks_vlm) == 1
    assert chunks_vlm[0].caption_source == "vlm"
    assert chunks_vlm[0].image_path == "images/fig1.png"

    datalab_text = (
        '<figure data-type="image">\n'
        "![Figure 1](images/fig1.png)\n"
        "<synthetic_caption>\n"
        "A photo of the laboratory experimental apparatus showing test tubes and sensors.\n"
        "</synthetic_caption>\n"
        "</figure>"
    )
    chunks_dl = chunk_markdown(datalab_text)
    assert len(chunks_dl) == 1
    assert chunks_dl[0].caption_source == "datalab"
    assert chunks_dl[0].image_path == "images/fig1.png"


def test_figure_html_img_tag_and_relative_path_extraction():
    """Verify extraction of image path from HTML img tags inside figure blocks."""
    html_fig_text = (
        '<figure data-type="image">\n'
        '<img src="images/experiment_setup.png" alt="Experimental Setup" />\n'
        "<figcaption>Figure 3: System diagram.</figcaption>\n"
        "</figure>"
    )
    chunks = chunk_markdown(html_fig_text)
    assert len(chunks) == 1
    fig_chunk = chunks[0]
    assert fig_chunk.chunk_type == "figure"
    assert fig_chunk.image_path == "images/experiment_setup.png"
    assert "<figcaption" not in fig_chunk.text
    assert "</figcaption>" not in fig_chunk.text
    assert "<figure" not in fig_chunk.text
    assert "</figure>" not in fig_chunk.text
    assert "Figure 3: System diagram." in fig_chunk.text


def test_figure_synthetic_caption_latex_backslashes():
    """Verify that synthetic captions containing arbitrary LaTeX backslashes do not raise PatternError."""
    latex_caption = (
        "**Type**: Physics schematic diagram.\n"
        "**Key Elements**: The interface displays $\\lambda_1 / \\lambda_2$, "
        "fractions $\\frac{a}{b}$, delimiters $\\left( x \\right)$, "
        "greek symbols $\\alpha$, $\\beta$, $\\gamma$, and backreferences \\1 \\2 \\g<0>.\n"
        "**Takeaway**: Wave optics interference governed by $\\Delta x = \\frac{\\lambda D}{d}$."
    )
    text = (
        "# Wave Optics\n\n"
        "Introduction to wave interference.\n\n"
        '<figure data-type="image">\n'
        "![Figure 1](images/double_slit.png)\n"
        "**Figure 1.** Double slit interference setup.\n"
        '<synthetic_caption data-source="vlm_remote" data-model="openrouter/nvidia/nemotron-3.5-super-vl:free">\n'
        f"{latex_caption}\n"
        "</synthetic_caption>\n"
        "</figure>\n\n"
        "Concluding discussion on optical path differences."
    )

    chunks = chunk_markdown(text)
    assert len(chunks) == 2

    # Chunk 0 is narrative text
    assert chunks[0].chunk_type == "text"
    assert "Introduction to wave interference." in chunks[0].text
    assert "Concluding discussion on optical path differences." in chunks[0].text

    # Chunk 1 is figure chunk
    fig_chunk = chunks[1]
    assert fig_chunk.chunk_type == "figure"
    assert fig_chunk.image_path == "images/double_slit.png"
    assert fig_chunk.caption_source == "vlm_remote"
    assert fig_chunk.vlm_model == "openrouter/nvidia/nemotron-3.5-super-vl:free"
    assert "[Visual Summary]:" in fig_chunk.text
    assert "$\\lambda_1 / \\lambda_2$" in fig_chunk.text
    assert "\\frac{a}{b}" in fig_chunk.text
    assert "\\left( x \\right)" in fig_chunk.text
    assert "\\alpha" in fig_chunk.text
    assert "\\beta" in fig_chunk.text
    assert "\\1 \\2 \\g<0>" in fig_chunk.text
    assert "<synthetic_caption" not in fig_chunk.text
    assert "</synthetic_caption>" not in fig_chunk.text


def test_chunk_markdown_frontmatter_isolated_chunk():
    """Verify that leading YAML frontmatter is isolated into chunk 0 of type 'frontmatter' without body text."""
    markdown = (
        "---\n"
        "type: journalArticle\n"
        "title: Attention Is All You Need\n"
        "authors:\n"
        "  - Vaswani, Ashish\n"
        "  - Shazeer, Noam\n"
        "year: 2017\n"
        "publication: NeurIPS\n"
        "doi: 10.48550/arXiv.1706.03762\n"
        "---\n\n"
        "# Introduction\n\n"
        "Recurrent neural networks, long short-term memory and gated recurrent neural networks "
        "have been firmly established as state of the art approaches.\n\n"
        "## Model Architecture\n\n"
        "The Transformer follows this overall architecture using stacked self-attention."
    )

    chunks = chunk_markdown(
        markdown,
        max_chars=1000,
        overlap_chars=0,
        document_title="Attention Is All You Need",
    )

    assert len(chunks) == 3

    # Chunk 0: Pure frontmatter
    c0 = chunks[0]
    assert c0.chunk_type == "frontmatter"
    assert c0.chunk_index == 0
    assert c0.heading_path == []
    assert c0.heading_str == ""
    assert c0.document_title == "Attention Is All You Need"
    assert "type: journalArticle" in c0.text
    assert "authors:" in c0.text
    assert "Vaswani, Ashish" in c0.text
    assert "doi: 10.48550/arXiv.1706.03762" in c0.text
    # Ensure NO document body text is in chunk 0
    assert "Introduction" not in c0.text
    assert "Recurrent neural networks" not in c0.text
    assert "Model Architecture" not in c0.text

    # Chunk 1: Introduction section (no frontmatter)
    c1 = chunks[1]
    assert c1.chunk_type == "text"
    assert c1.chunk_index == 1
    assert c1.heading_str == "Introduction"
    assert "Recurrent neural networks" in c1.text
    assert "type: journalArticle" not in c1.text
    assert "---" not in c1.text

    # Chunk 2: Model Architecture section
    c2 = chunks[2]
    assert c2.chunk_type == "text"
    assert c2.chunk_index == 2
    assert c2.heading_str == "Introduction > Model Architecture"
    assert "The Transformer follows this overall architecture" in c2.text


def test_chunk_markdown_only_frontmatter():
    """Verify that a document containing only frontmatter produces a single chunk at index 0."""
    markdown = "---\ntype: webpage\ntitle: Standalone Metadata\n---"
    chunks = chunk_markdown(markdown)
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "frontmatter"
    assert chunks[0].chunk_index == 0
    assert "type: webpage" in chunks[0].text


def test_chunk_markdown_frontmatter_with_hyphens_in_content():
    """Verify frontmatter delimiter regex handles values containing triple hyphens."""
    markdown = (
        "---\n"
        "type: document\n"
        "title: Title with --- hyphens inside\n"
        "---\n\n"
        "# Content\n"
        "Main body text."
    )
    chunks = chunk_markdown(markdown)
    assert len(chunks) == 2
    assert chunks[0].chunk_type == "frontmatter"
    assert "Title with --- hyphens inside" in chunks[0].text
    assert chunks[1].chunk_type == "text"
    assert "Main body text." in chunks[1].text


def test_chunk_markdown_line_numbers_aligned_with_toc():
    """Verify that chunker line_start and line_end are exactly aligned with TOC generator."""
    from zocrates.rag.toc_generator import generate_toc

    markdown = (
        "---\n"
        "title: Line Number Test Paper\n"
        "authors:\n"
        "  - Jane Doe\n"
        "---\n"
        "\n"
        "# Introduction\n"
        "This is the first paragraph of introduction.\n"
        "It spans across line eight.\n"
        "\n"
        "## Methodology\n"
        "Detailed methodology explanation.\n"
        "\n"
        '<figure data-type="image">\n'
        "![Figure 1](images/model.png)\n"
        "<synthetic_caption>\n"
        "Neural network architecture overview.\n"
        "</synthetic_caption>\n"
        "**Figure 1.** System model.\n"
        "</figure>\n"
        "\n"
        "Continuing methodology after figure.\n"
    )

    toc_json, _ = generate_toc(markdown, title="Line Number Test Paper")
    chunks = chunk_markdown(markdown, max_chars=2000, overlap_chars=0)

    # Verify Frontmatter chunk (chunk 0) matches s0 in ToC
    s0 = next(s for s in toc_json["sections"] if s["id"] == "s0")
    assert chunks[0].chunk_type == "frontmatter"
    assert chunks[0].line_start == s0["start_line"]
    assert chunks[0].line_end == s0["end_line"]

    # Verify Figure chunk matches f1 in ToC
    f1 = toc_json["figures"][0]
    fig_chunk = next(c for c in chunks if c.chunk_type == "figure")
    assert fig_chunk.line_start == f1["start_line"]
    assert fig_chunk.line_end == f1["end_line"]

    # Verify all chunks have line_start <= line_end
    for c in chunks:
        assert c.line_start >= 1
        assert c.line_end >= c.line_start









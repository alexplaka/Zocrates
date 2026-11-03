"""Unit tests for Phase 3 Paper Relations Network Graph API and Visualizer."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.rag.document_processor import _META_SCHEMA


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


@pytest.fixture
def meta_db_setup(tmp_path: Path):
    """Create a temporary conversion_meta.db SQLite database with schema."""
    db_path = tmp_path / "conversion_meta.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_META_SCHEMA)
    return db_path, tmp_path


class TestGraphAPI:
    """Test suite for GET /graph/data and GET /graph/view."""

    def test_graph_data_empty(self, client) -> None:
        """When no data exists or db is not found, returns empty nodes and links."""
        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=None):
            res = client.get("/graph/data")
            assert res.status_code == 200
            data = res.json()
            assert data == {"nodes": [], "links": []}

    def test_graph_data_global(self, client, meta_db_setup) -> None:
        """Verify global graph data returns nodes, deduplicated undirected links, degrees, and parsed keywords."""
        db_path, cache_dir = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            # Seed nodes
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count,
                    keywords
                ) VALUES
                ('KEY_A', 'Autonomous AI Agents', 'markitdown', '1.0', '/path/a.pdf', 'pdf', 'journalArticle', '2026-01-01', 'hashA', 5000, ?),
                ('KEY_B', 'Loop Engineering Systems', 'markitdown', '1.0', '/path/b.pdf', 'pdf', 'conferencePaper', '2026-01-02', 'hashB', 4500, ?),
                ('KEY_C', 'Theoretical Economics', 'markitdown', '1.0', '/path/c.pdf', 'pdf', 'journalArticle', '2026-01-03', 'hashC', 3000, ?)
                """,
                (
                    json.dumps(["autonomous agents", "coding loops"]),
                    json.dumps(["loop engineering", "prompt loops"]),
                    json.dumps(["fair division", "mixed manna"]),
                ),
            )

            # Seed bidirectional edges
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('KEY_A', 'KEY_B', 0.88, 'hybrid_search', ?, '2026-01-01T00:00:00'),
                ('KEY_B', 'KEY_A', 0.88, 'hybrid_search', ?, '2026-01-01T00:00:00'),
                ('KEY_B', 'KEY_C', 0.65, 'shared_tags', ?, '2026-01-01T00:00:00'),
                ('KEY_C', 'KEY_B', 0.65, 'shared_tags', ?, '2026-01-01T00:00:00')
                """,
                (
                    json.dumps({"raw_rrf_score": 0.0861, "matched_chunks": 4, "retrieval_methods": ["dense", "bm25"]}),
                    json.dumps({"raw_rrf_score": 0.0861, "matched_chunks": 4, "retrieval_methods": ["dense", "bm25"]}),
                    json.dumps({"source": "zotero_itemTags", "matched_tags": ["systems"]}),
                    json.dumps({"source": "zotero_itemTags", "matched_tags": ["systems"]}),
                ),
            )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            res = client.get("/graph/data")
            assert res.status_code == 200
            data = res.json()

            nodes = data["nodes"]
            links = data["links"]

            assert len(nodes) == 3
            node_map = {n["id"]: n for n in nodes}

            # Check parsed keywords & degrees
            assert node_map["KEY_A"]["keywords"] == ["autonomous agents", "coding loops"]
            assert node_map["KEY_A"]["degree"] == 1
            assert node_map["KEY_B"]["keywords"] == ["loop engineering", "prompt loops"]
            assert node_map["KEY_B"]["degree"] == 2
            assert node_map["KEY_C"]["keywords"] == ["fair division", "mixed manna"]
            assert node_map["KEY_C"]["degree"] == 1

            # Check deduplicated undirected links
            assert len(links) == 2
            for link in links:
                # Undirected uniqueness invariant: source < target
                assert link["source"] < link["target"]

            link_ab = next(l for l in links if l["source"] == "KEY_A" and l["target"] == "KEY_B")
            assert link_ab["score"] == 0.88
            assert link_ab["raw_rrf"] == 0.0861
            assert link_ab["relation_type"] == "hybrid_search"
            assert "Hybrid search match" in link_ab["evidence"]

            link_bc = next(l for l in links if l["source"] == "KEY_B" and l["target"] == "KEY_C")
            assert link_bc["score"] == 0.65
            assert link_bc["relation_type"] == "shared_tags"
            assert "Shared tags" in link_bc["evidence"]

    def test_graph_data_center_key_ego(self, client, meta_db_setup) -> None:
        """Verify BFS traversal starting at center_key up to depth hops."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            # Seed 4 nodes: A - B - C - D linear chain
            for k in ["KEY_A", "KEY_B", "KEY_C", "KEY_D"]:
                conn.execute(
                    """
                    INSERT INTO conversion_meta (
                        item_key, title, converter_id, converter_version, source_path,
                        content_type, converted_at, source_hash, char_count
                    ) VALUES (?, ?, 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                    """,
                    (k, f"Title {k}"),
                )

            # A <-> B <-> C <-> D
            edges = [
                ("KEY_A", "KEY_B", 0.9),
                ("KEY_B", "KEY_A", 0.9),
                ("KEY_B", "KEY_C", 0.8),
                ("KEY_C", "KEY_B", 0.8),
                ("KEY_C", "KEY_D", 0.7),
                ("KEY_D", "KEY_C", 0.7),
            ]
            for s, t, sc in edges:
                conn.execute(
                    """
                    INSERT INTO item_relations (
                        source_key, target_key, score, relation_type, match_evidence, updated_at
                    ) VALUES (?, ?, ?, 'hybrid_search', '{}', '2026-01-01T00:00:00')
                    """,
                    (s, t, sc),
                )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            # Depth 1 from KEY_A should reach KEY_A and KEY_B only
            res_d1 = client.get("/graph/data?center_key=KEY_A&depth=1")
            assert res_d1.status_code == 200
            d1_data = res_d1.json()
            d1_node_ids = {n["id"] for n in d1_data["nodes"]}
            assert d1_node_ids == {"KEY_A", "KEY_B"}
            assert len(d1_data["links"]) == 1

            center_node = next(n for n in d1_data["nodes"] if n["id"] == "KEY_A")
            assert center_node["is_center"] is True
            other_node = next(n for n in d1_data["nodes"] if n["id"] == "KEY_B")
            assert other_node["is_center"] is False

            # Depth 2 from KEY_A should reach KEY_A, KEY_B, KEY_C
            res_d2 = client.get("/graph/data?center_key=KEY_A&depth=2")
            assert res_d2.status_code == 200
            d2_data = res_d2.json()
            d2_node_ids = {n["id"] for n in d2_data["nodes"]}
            assert d2_node_ids == {"KEY_A", "KEY_B", "KEY_C"}
            assert len(d2_data["links"]) == 2

    def test_graph_data_filters(self, client, meta_db_setup) -> None:
        """Verify min_score, include_curated, and include_tags filtering."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            for k in ["KEY_1", "KEY_2", "KEY_3", "KEY_4"]:
                conn.execute(
                    """
                    INSERT INTO conversion_meta (
                        item_key, title, converter_id, converter_version, source_path,
                        content_type, converted_at, source_hash, char_count
                    ) VALUES (?, ?, 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                    """,
                    (k, f"Title {k}"),
                )

            # KEY_1 <-> KEY_2: human_curated (score 1.0)
            # KEY_2 <-> KEY_3: hybrid_search (score 0.72)
            # KEY_3 <-> KEY_4: shared_tags (score 0.55)
            edges = [
                ("KEY_1", "KEY_2", 1.0, "human_curated"),
                ("KEY_2", "KEY_1", 1.0, "human_curated"),
                ("KEY_2", "KEY_3", 0.72, "hybrid_search"),
                ("KEY_3", "KEY_2", 0.72, "hybrid_search"),
                ("KEY_3", "KEY_4", 0.55, "shared_tags"),
                ("KEY_4", "KEY_3", 0.55, "shared_tags"),
            ]
            for s, t, sc, rt in edges:
                conn.execute(
                    """
                    INSERT INTO item_relations (
                        source_key, target_key, score, relation_type, match_evidence, updated_at
                    ) VALUES (?, ?, ?, ?, '{}', '2026-01-01T00:00:00')
                    """,
                    (s, t, sc, rt),
                )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            # Test min_score threshold: min_score=0.70 should exclude shared_tags (0.55)
            res_score = client.get("/graph/data?min_score=0.70")
            assert res_score.status_code == 200
            score_links = res_score.json()["links"]
            assert len(score_links) == 2
            rel_types = {l["relation_type"] for l in score_links}
            assert rel_types == {"human_curated", "hybrid_search"}

            # Test include_curated=false: should exclude human_curated
            res_no_curated = client.get("/graph/data?include_curated=false&min_score=0.50")
            assert res_no_curated.status_code == 200
            no_curated_links = res_no_curated.json()["links"]
            assert len(no_curated_links) == 2
            assert all(l["relation_type"] != "human_curated" for l in no_curated_links)

            # Test include_tags=false: should exclude shared_tags
            res_no_tags = client.get("/graph/data?include_tags=false&min_score=0.50")
            assert res_no_tags.status_code == 200
            no_tags_links = res_no_tags.json()["links"]
            assert len(no_tags_links) == 2
            assert all(l["relation_type"] != "shared_tags" for l in no_tags_links)

    def test_graph_data_collection_filter(self, client, meta_db_setup) -> None:
        """Verify collection_key restricts returned nodes and links to that collection."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, converted_at, source_hash, char_count
                ) VALUES
                ('KEY_IN_1', 'In Col 1', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h1', 1000),
                ('KEY_IN_2', 'In Col 2', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h2', 1000),
                ('KEY_OUT', 'Outside Col', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h3', 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('KEY_IN_1', 'KEY_IN_2', 0.9, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_IN_2', 'KEY_IN_1', 0.9, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_IN_2', 'KEY_OUT', 0.85, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_OUT', 'KEY_IN_2', 0.85, 'hybrid_search', '{}', '2026-01-01T00:00:00')
                """
            )

        mock_syncer = MagicMock()
        mock_syncer.db.get_item_keys_in_collection.return_value = ["KEY_IN_1", "KEY_IN_2"]

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path), \
             patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/graph/data?collection_key=COL_123")
            assert res.status_code == 200
            data = res.json()
            node_ids = {n["id"] for n in data["nodes"]}
            assert node_ids == {"KEY_IN_1", "KEY_IN_2"}
            assert len(data["links"]) == 1
            assert data["links"][0]["source"] == "KEY_IN_1"
            assert data["links"][0]["target"] == "KEY_IN_2"

    def test_graph_view_html(self, client) -> None:
        """Verify GET /graph/view returns 200 OK and valid HTML containing canvas and inspector DOM."""
        res = client.get("/graph/view")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        html_text = res.text
        assert '<canvas id="graph-canvas"' in html_text
        assert 'id="inspector-drawer"' in html_text
        assert 'id="toolbar"' in html_text
        assert 'id="depth-slider"' in html_text
        assert 'id="inspector-cluster-section"' in html_text
        assert 'id="inspector-cluster-card"' in html_text
        assert 'id="inspector-cluster-pill"' not in html_text
        assert 'id="btn-toggle-sim"' not in html_text
        assert 'id="confidence-slider"' in html_text
        assert 'id="inspector-bib-section"' in html_text
        assert 'id="inspector-abstract-section"' in html_text

    def test_graph_data_precedence_coalescing(self, client, meta_db_setup) -> None:
        """Verify strict precedence in edge coalescing:
        human_curated_and_hybrid_search (4) > human_curated (3) > hybrid_search (2) > shared_tags (1).
        """
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, converted_at, source_hash, char_count
                ) VALUES
                ('NODE_A', 'Paper A', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000),
                ('NODE_B', 'Paper B', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000),
                ('NODE_C', 'Paper C', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                """
            )
            # A <-> B has both human_curated (1.0) and human_curated_and_hybrid_search (1.0) -> dual must win
            # B <-> C has shared_tags (0.70) and hybrid_search (0.55) -> hybrid_search must win
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('NODE_A', 'NODE_B', 1.0, 'human_curated', '{}', 'now'),
                ('NODE_A', 'NODE_B', 1.0, 'human_curated_and_hybrid_search', '{}', 'now'),
                ('NODE_B', 'NODE_C', 0.70, 'shared_tags', '{}', 'now'),
                ('NODE_B', 'NODE_C', 0.55, 'hybrid_search', '{}', 'now')
                """
            )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            res = client.get("/graph/data?min_score=0.50")
            assert res.status_code == 200
            links = res.json()["links"]
            assert len(links) == 2

            link_ab = next(l for l in links if l["source"] == "NODE_A" and l["target"] == "NODE_B")
            assert link_ab["relation_type"] == "human_curated_and_hybrid_search"
            assert link_ab["score"] == 1.0

            link_bc = next(l for l in links if l["source"] == "NODE_B" and l["target"] == "NODE_C")
            assert link_bc["relation_type"] == "hybrid_search"
            assert link_bc["score"] == 0.55

    def test_graph_data_include_curated_dual_exclusion(self, client, meta_db_setup) -> None:
        """Verify include_curated=false excludes both human_curated and human_curated_and_hybrid_search."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, converted_at, source_hash, char_count
                ) VALUES
                ('P1', 'Paper 1', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000),
                ('P2', 'Paper 2', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000),
                ('P3', 'Paper 3', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('P1', 'P2', 1.0, 'human_curated_and_hybrid_search', '{}', 'now'),
                ('P2', 'P3', 0.85, 'hybrid_search', '{}', 'now')
                """
            )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            res = client.get("/graph/data?include_curated=false")
            assert res.status_code == 200
            links = res.json()["links"]
            assert len(links) == 1
            assert links[0]["source"] == "P2"
            assert links[0]["target"] == "P3"
            assert links[0]["relation_type"] == "hybrid_search"

    def test_graph_data_parent_item_key_resolution_for_center(self, client, meta_db_setup) -> None:
        """Verify center_key supplied as parent_item_key resolves to the attachment node."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, parent_item_key, converted_at, source_hash, char_count
                ) VALUES
                ('ATT_1', 'Attachment 1', 'markitdown', '1.0', '/path', 'pdf', 'PARENT_1', '2026-01-01', 'h', 1000),
                ('ATT_2', 'Attachment 2', 'markitdown', '1.0', '/path', 'pdf', 'PARENT_2', '2026-01-01', 'h', 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('ATT_1', 'ATT_2', 0.80, 'hybrid_search', '{}', 'now')
                """
            )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            res = client.get("/graph/data?center_key=PARENT_1&depth=1")
            assert res.status_code == 200
            data = res.json()
            nodes = data["nodes"]
            assert len(nodes) == 2
            center_node = next(n for n in nodes if n["id"] == "ATT_1")
            assert center_node["is_center"] is True
            assert center_node["parent_item_key"] == "PARENT_1"

    def test_graph_data_bibliographic_metadata_and_abstract(self, client, meta_db_setup) -> None:
        """Verify authors, publication, and abstract excerpt are parsed into node objects."""
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, abstract, converted_at, source_hash, char_count
                ) VALUES
                ('BIB_DOC', 'Grand Research Paper', 'markitdown', '1.0', '/path', 'pdf', 'This paper studies graph architectures.', '2026-01-01', 'h', 1000)
                """
            )

        mock_item = MagicMock()
        mock_item.year = "2025"
        mock_item.creators = ["Alice Smith", "Bob Jones"]
        mock_item.publication = "Nature Communications"
        mock_item.abstract = "This paper studies graph architectures."

        mock_syncer = MagicMock()
        mock_syncer.db.get_item_by_key.return_value = mock_item

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path), \
             patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/graph/data")
            assert res.status_code == 200
            nodes = res.json()["nodes"]
            assert len(nodes) == 1
            node = nodes[0]
            assert node["year"] == 2025
            assert node["authors"] == ["Alice Smith", "Bob Jones"]
            assert node["publication"] == "Nature Communications"
            assert "This paper studies graph architectures." in node["abstract"]

    def test_graph_data_multi_center_keys(self, client, meta_db_setup) -> None:
        """Verify multi-source BFS starting from multiple center keys.
        Both center keys have is_center=True, their neighborhoods are unioned,
        and connecting bridges between them are included.
        """
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            # Seed linear chain: KEY_A - KEY_B - KEY_C - KEY_D - KEY_E
            for k in ["KEY_A", "KEY_B", "KEY_C", "KEY_D", "KEY_E"]:
                conn.execute(
                    """
                    INSERT INTO conversion_meta (
                        item_key, title, converter_id, converter_version, source_path,
                        content_type, converted_at, source_hash, char_count
                    ) VALUES (?, ?, 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                    """,
                    (k, f"Title {k}"),
                )

            edges = [
                ("KEY_A", "KEY_B", 0.9),
                ("KEY_B", "KEY_A", 0.9),
                ("KEY_B", "KEY_C", 0.85),
                ("KEY_C", "KEY_B", 0.85),
                ("KEY_C", "KEY_D", 0.8),
                ("KEY_D", "KEY_C", 0.8),
                ("KEY_D", "KEY_E", 0.75),
                ("KEY_E", "KEY_D", 0.75),
            ]
            for s, t, sc in edges:
                conn.execute(
                    """
                    INSERT INTO item_relations (
                        source_key, target_key, score, relation_type, match_evidence, updated_at
                    ) VALUES (?, ?, ?, 'hybrid_search', '{}', '2026-01-01T00:00:00')
                    """,
                    (s, t, sc),
                )

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path):
            # center_keys=KEY_A,KEY_C with depth=1
            # KEY_A depth 0: KEY_A, depth 1: KEY_B
            # KEY_C depth 0: KEY_C, depth 1: KEY_B, KEY_D
            # KEY_E is at depth 2 from KEY_C (distance 4 from KEY_A), so it must NOT be visited
            res = client.get("/graph/data?center_keys=KEY_A,KEY_C&depth=1")
            assert res.status_code == 200
            data = res.json()

            nodes = data["nodes"]
            links = data["links"]

            node_ids = {n["id"] for n in nodes}
            assert node_ids == {"KEY_A", "KEY_B", "KEY_C", "KEY_D"}
            assert "KEY_E" not in node_ids

            node_map = {n["id"]: n for n in nodes}
            # Both centers get is_center = True
            assert node_map["KEY_A"]["is_center"] is True
            assert node_map["KEY_C"]["is_center"] is True
            # Non-center nodes get is_center = False
            assert node_map["KEY_B"]["is_center"] is False
            assert node_map["KEY_D"]["is_center"] is False

            # Connecting bridge KEY_B links KEY_A and KEY_C
            link_pairs = {(l["source"], l["target"]) for l in links}
            assert ("KEY_A", "KEY_B") in link_pairs
            assert ("KEY_B", "KEY_C") in link_pairs
            assert ("KEY_C", "KEY_D") in link_pairs
            # Link to KEY_E is excluded
            assert not any("KEY_E" in (l["source"], l["target"]) for l in links)

    def test_graph_data_multi_collection_keys(self, client, meta_db_setup) -> None:
        """Verify multi-collection filtering enforces a strict collection boundary
        containing items from either collection and strictly internal links.
        """
        db_path, _ = meta_db_setup

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, converter_id, converter_version, source_path,
                    content_type, converted_at, source_hash, char_count
                ) VALUES
                ('KEY_COL1_1', 'In Col 1 Paper 1', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h1', 1000),
                ('KEY_COL1_2', 'In Col 1 Paper 2', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h2', 1000),
                ('KEY_COL2_1', 'In Col 2 Paper 1', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h3', 1000),
                ('KEY_OUT', 'Outside Any Col', 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h4', 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (
                    source_key, target_key, score, relation_type, match_evidence, updated_at
                ) VALUES
                ('KEY_COL1_1', 'KEY_COL1_2', 0.9, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_COL1_2', 'KEY_COL1_1', 0.9, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_COL1_2', 'KEY_COL2_1', 0.85, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_COL2_1', 'KEY_COL1_2', 0.85, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_COL2_1', 'KEY_OUT', 0.80, 'hybrid_search', '{}', '2026-01-01T00:00:00'),
                ('KEY_OUT', 'KEY_COL2_1', 0.80, 'hybrid_search', '{}', '2026-01-01T00:00:00')
                """
            )

        mock_syncer = MagicMock()
        def mock_get_item_keys(collection_key: str):
            if collection_key == "COL_1":
                return ["KEY_COL1_1", "KEY_COL1_2"]
            elif collection_key == "COL_2":
                return ["KEY_COL2_1"]
            return []
        mock_syncer.db.get_item_keys_in_collection.side_effect = mock_get_item_keys

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path), \
             patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/graph/data?collection_keys=COL_1,COL_2")
            assert res.status_code == 200
            data = res.json()

            node_ids = {n["id"] for n in data["nodes"]}
            assert node_ids == {"KEY_COL1_1", "KEY_COL1_2", "KEY_COL2_1"}
            assert "KEY_OUT" not in node_ids

            links = data["links"]
            assert len(links) == 2
            link_pairs = {(l["source"], l["target"]) for l in links}
            assert ("KEY_COL1_1", "KEY_COL1_2") in link_pairs
            assert ("KEY_COL1_2", "KEY_COL2_1") in link_pairs
            # Strict boundary: edge connecting KEY_COL2_1 to KEY_OUT must NOT be present
            assert not any("KEY_OUT" in (l["source"], l["target"]) for l in links)

    def test_graph_data_parent_key_priority_date_regex_and_full_abstract(self, client, meta_db_setup) -> None:
        """Verify:
        1. p_key is looked up first before i_key in ZoteroDB.
        2. Year is parsed from item_obj.date via regex when item_obj.year is missing.
        3. Full abstract (>400 chars) is preserved without truncation and prefers ZoteroDB over fallback.
        """
        db_path, _ = meta_db_setup

        long_zotero_abstract = "Zotero abstract start " + ("x" * 500) + " end."

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (
                    item_key, title, parent_item_key, converter_id, converter_version, source_path,
                    content_type, abstract, converted_at, source_hash, char_count
                ) VALUES
                ('ATTACH_KEY', 'Attachment Title', 'PARENT_KEY', 'markitdown', '1.0', '/path',
                 'pdf', 'Fallback DB abstract', '2026-01-01', 'h', 1000)
                """
            )

        # Mock parent item returned when looking up PARENT_KEY
        mock_parent_item = MagicMock()
        mock_parent_item.year = None
        mock_parent_item.date = "Published on May 15, 2024 in London"
        mock_parent_item.creators = ["Jane Doe"]
        mock_parent_item.publication = "Science Journal"
        mock_parent_item.abstract = long_zotero_abstract

        # Mock attach item (should not be used if parent item exists)
        mock_attach_item = MagicMock()
        mock_attach_item.year = "1999"
        mock_attach_item.date = "1999-01-01"
        mock_attach_item.abstract = "Wrong attach abstract"

        mock_syncer = MagicMock()
        def mock_get_item(key: str):
            if key == "PARENT_KEY":
                return mock_parent_item
            if key == "ATTACH_KEY":
                return mock_attach_item
            return None
        mock_syncer.db.get_item_by_key.side_effect = mock_get_item

        with patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path), \
             patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/graph/data")
            assert res.status_code == 200
            nodes = res.json()["nodes"]
            assert len(nodes) == 1
            node = nodes[0]

            # 1. p_key was looked up first: parent metadata is present
            assert node["authors"] == ["Jane Doe"]
            assert node["publication"] == "Science Journal"

            # 2. Year parsed from mock_parent_item.date via regex since year is None
            assert node["year"] == 2024

            # 3. Full abstract preserved (>400 chars, no truncation with '...')
            assert node["abstract"] == long_zotero_abstract
            assert len(node["abstract"]) > 500

    def test_graph_recompute_unregistered(self, client) -> None:
        """When syncer is not registered, POST /graph/recompute returns 400."""
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.post("/graph/recompute", json={"item_keys": ["KEY_1"]})
            assert res.status_code == 400
            assert "Zocrates backend is not registered" in res.json()["detail"]

    def test_graph_recompute_with_specified_keys(self, client) -> None:
        """Verify POST /graph/recompute evaluates only specified keys."""
        mock_syncer = MagicMock()
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app.evaluate_and_persist_edges") as mock_eval:
            mock_eval.return_value = []
            res = client.post("/graph/recompute", json={"item_keys": ["KEY_A", "KEY_B"]})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["recomputed_count"] == 2
            assert mock_eval.call_count == 2
            mock_eval.assert_any_call("KEY_A", mock_syncer, True)
            mock_eval.assert_any_call("KEY_B", mock_syncer, True)

    def test_graph_recompute_all_keys(self, client, meta_db_setup) -> None:
        """When item_keys is omitted, recomputes all keys from conversion_meta.db."""
        db_path, _ = meta_db_setup
        with sqlite3.connect(str(db_path)) as conn:
            for k in ["KEY_1", "KEY_2", "KEY_3"]:
                conn.execute(
                    """
                    INSERT INTO conversion_meta (
                        item_key, title, converter_id, converter_version, source_path,
                        content_type, converted_at, source_hash, char_count
                    ) VALUES (?, ?, 'markitdown', '1.0', '/path', 'pdf', '2026-01-01', 'h', 1000)
                    """,
                    (k, f"Title {k}"),
                )

        mock_syncer = MagicMock()
        mock_syncer.processor.get_all_meta_keys.return_value = {"KEY_1", "KEY_2", "KEY_3"}

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer), \
             patch("zocrates.fast_api_app._resolve_conversion_meta_db_path", return_value=db_path), \
             patch("zocrates.fast_api_app.evaluate_and_persist_edges") as mock_eval:
            mock_eval.return_value = []
            # Call with no body
            res = client.post("/graph/recompute")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert data["recomputed_count"] == 3
            assert mock_eval.call_count == 3



"""
Scholarly literature discovery and batch import package for Zocrates.
"""

from zocrates.discovery.dedup import Deduplicator, normalize_arxiv_id, normalize_doi, normalize_title
from zocrates.discovery.engine import DiscoveryEngine, get_default_discovery_engine
from zocrates.discovery.models import CandidatePaper, StagedItem
from zocrates.discovery.providers.openalex import OpenAlexProvider
from zocrates.discovery.staging import (
    cleanup_stale_staging_directories,
    download_and_stage_pdf,
    get_staging_root,
)

__all__ = [
    "CandidatePaper",
    "StagedItem",
    "DiscoveryEngine",
    "get_default_discovery_engine",
    "Deduplicator",
    "OpenAlexProvider",
    "get_staging_root",
    "cleanup_stale_staging_directories",
    "download_and_stage_pdf",
    "normalize_doi",
    "normalize_arxiv_id",
    "normalize_title",
]

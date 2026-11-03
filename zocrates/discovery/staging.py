"""
Temporary download staging, SSRF validation, byte capping, and cleanup for candidate PDF papers.
"""

import hashlib
import logging
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any
import urllib.parse

import httpx

from zocrates.discovery.models import CandidatePaper

logger = logging.getLogger(__name__)

MAX_PDF_BYTES: int = 100 * 1024 * 1024  # 100 MB
PDF_MAGIC_BYTES: bytes = b"%PDF-"
DEFAULT_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=30.0)


def get_staging_root() -> Path:
    """Return the root directory for temporary discovery batch staging.

    Prefers <zocrates-data>/staging/ (which is within user home, accessible to sandboxed
    Flatpak Zotero instances having filesystem=home permissions), falling back to
    ~/.zocrates/staging, and finally system temp.
    """
    try:
        from zocrates.config import get_zocrates_data_dir

        root = get_zocrates_data_dir() / "staging"
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception:
        pass

    try:
        root = Path.home() / ".zocrates" / "staging"
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception:
        pass

    root = Path(tempfile.gettempdir()) / "zocrates_staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_stale_staging_directories(max_age_seconds: int = 7200) -> int:
    """
    Remove staging subdirectories older than `max_age_seconds` (default: 2 hours).

    Args:
        max_age_seconds: Age threshold in seconds.

    Returns:
        Number of directories removed.
    """
    root = get_staging_root()
    if not root.exists():
        return 0

    removed = 0
    now = time.time()
    for item in root.iterdir():
        if item.is_dir():
            try:
                mtime = item.stat().st_mtime
                if (now - mtime) > max_age_seconds:
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
            except Exception as e:
                logger.debug("Failed removing stale staging directory %s: %s", item, e)
    return removed


def extract_pdf_url_from_html(html: str, base_url: str) -> str | None:
    """
    Extract a direct PDF URL from an HTML landing page using standard scholarly metadata tags
    (Highwire/Google Scholar, Open Access Signposting, EPrints, and DSpace).
    """
    if not html:
        return None

    # 1. Google Scholar / Highwire meta tag: <meta name="citation_pdf_url" content="...">
    m = re.search(
        r"""<meta\s+[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']""",
        html,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"""<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']""",
            html,
            re.IGNORECASE,
        )

    # 2. Open Access Signposting / Link relations: <link rel="item" type="application/pdf" href="...">
    if not m:
        m = re.search(
            r"""<link\s+[^>]*type=["\']application/pdf["\'][^>]*href=["\']([^"\']+)["\']""",
            html,
            re.IGNORECASE,
        )
    if not m:
        m = re.search(
            r"""<link\s+[^>]*href=["\']([^"\']+)["\'][^>]*type=["\']application/pdf["\']""",
            html,
            re.IGNORECASE,
        )

    # 3. EPrints document URL: <meta name="eprints.document_url" content="...">
    if not m:
        m = re.search(
            r"""<meta\s+[^>]*name=["\']eprints\.document_url["\'][^>]*content=["\']([^"\']+)["\']""",
            html,
            re.IGNORECASE,
        )

    # 4. Embedded DSpace 7 REST bitstream API link: /server/api/core/bitstreams/{uuid}/content
    if not m:
        m_dspace = re.search(r"""/server/api/core/bitstreams/([0-9a-fA-F\-]{36})/content""", html)
        if m_dspace:
            return urllib.parse.urljoin(base_url, m_dspace.group(0))

    if m:
        raw_url = m.group(1).strip()
        # DSpace 7 SPA rewrite: /bitstreams/{uuid}/download -> /server/api/core/bitstreams/{uuid}/content
        if "/bitstreams/" in raw_url and raw_url.endswith("/download"):
            raw_url = re.sub(
                r"/bitstreams/([0-9a-fA-F\-]{36})/download",
                r"/server/api/core/bitstreams/\1/content",
                raw_url,
            )
        return urllib.parse.urljoin(base_url, raw_url)

    return None


def extract_pdf_from_link_header(link_header: str, base_url: str) -> str | None:
    """Extract a direct PDF URL from HTTP Link header (Signposting RFC 8288)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if "application/pdf" in part and ("rel=\"item\"" in part or "rel=\"alternate\"" in part or "rel=item" in part):
            m = re.search(r"""<([^>]+)>""", part)
            if m:
                raw_url = m.group(1).strip()
                if "/bitstreams/" in raw_url and raw_url.endswith("/download"):
                    raw_url = re.sub(
                        r"/bitstreams/([0-9a-fA-F\-]{36})/download",
                        r"/server/api/core/bitstreams/\1/content",
                        raw_url,
                    )
                return urllib.parse.urljoin(base_url, raw_url)
    return None


async def resolve_dspace7_pdf(
    client: httpx.AsyncClient,
    page_url: str,
    html: str,
    initial_url: str = "",
) -> str | None:
    """Resolve a direct bitstream PDF URL from a modern DSpace 7 Angular SPA landing page.

    DSpace 7 returns a lightweight JavaScript SPA shell (<ds-app>) whose primary
    PDF bitstreams are not present in raw HTML meta tags, but accessible via the
    native DSpace 7 REST API (/server/api/core/items/{uuid}/bundles).
    """
    if "dspace" not in html.lower() and "<ds-app" not in html.lower():
        return None

    parsed = urllib.parse.urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Check if the redirected or current URL contains a DSpace item/publication UUID
    bundles_href: str | None = None
    m_uuid = re.search(r"/(?:entities/[a-zA-Z0-9_\-]+|items|item)/([0-9a-fA-F\-]{36})", page_url)
    if m_uuid:
        item_uuid = m_uuid.group(1)
        bundles_href = f"{origin}/server/api/core/items/{item_uuid}/bundles"
    else:
        # 2. Check if initial or final URL contains a handle path, e.g. handle/10344/10509
        for u in (initial_url, page_url):
            m_handle = re.search(r"handle/([0-9a-zA-Z\.\_\-]+/[0-9a-zA-Z\.\_\-]+)", u)
            if m_handle:
                pid_url = f"{origin}/server/api/pid/find?id={m_handle.group(1)}"
                try:
                    r = await client.get(pid_url, timeout=10)
                    if r.status_code == 200:
                        bundles_href = r.json().get("_links", {}).get("bundles", {}).get("href")
                        if bundles_href:
                            break
                except Exception:
                    pass

    if not bundles_href:
        return None

    try:
        b_res = await client.get(bundles_href, timeout=10)
        if b_res.status_code != 200:
            return None
        b_data = b_res.json()
        bundles = b_data.get("_embedded", {}).get("bundles", [])
        original_bundles = [b for b in bundles if b.get("name") == "ORIGINAL"]
        target_bundles = original_bundles if original_bundles else bundles

        for bundle in target_bundles:
            bs_href = bundle.get("_links", {}).get("bitstreams", {}).get("href")
            if not bs_href:
                continue
            bs_res = await client.get(bs_href, timeout=10)
            if bs_res.status_code != 200:
                continue
            bs_data = bs_res.json()
            for bs in bs_data.get("_embedded", {}).get("bitstreams", []):
                name = bs.get("name", "").lower()
                mime = (bs.get("format") or {}).get("mimetype", "").lower() if isinstance(bs.get("format"), dict) else ""
                if name.endswith(".pdf") or "pdf" in mime:
                    content_href = bs.get("_links", {}).get("content", {}).get("href")
                    if content_href:
                        return urllib.parse.urljoin(origin, content_href)
    except Exception as exc:
        logger.debug("DSpace 7 REST bitstream resolution failed for %s: %s", page_url, exc)

    return None


async def _stream_to_file(
    client: httpx.AsyncClient,
    initial_url: str,
    dest_path: Path,
    allow_html_landing: bool = True,
) -> str | None:
    """Stream remote URL to local destination path with hop-by-hop HTTPS validation, byte capping, and magic byte checks."""
    curr_url = initial_url
    max_redirects = 5

    try:
        for _ in range(max_redirects + 1):
            parsed = urllib.parse.urlparse(curr_url)
            if parsed.scheme.lower() != "https":
                if parsed.scheme.lower() == "http":
                    curr_url = "https://" + curr_url[7:]
                    parsed = urllib.parse.urlparse(curr_url)
                else:
                    logger.warning("SSRF Protection: Blocked non-https URL or redirect: %s", curr_url)
                    return None

            hostname = (parsed.hostname or "").lower()
            if hostname in ("localhost", "127.0.0.1", "::1", "169.254.169.254") or hostname.startswith("192.168.") or hostname.startswith("10."):
                logger.warning("SSRF Protection: Blocked request to private/local host %s", hostname)
                return None

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            }
            async with client.stream("GET", curr_url, headers=headers, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    loc = response.headers.get("Location")
                    if not loc:
                        logger.warning("Redirect status %d with no Location header for %s", response.status_code, curr_url)
                        return None
                    next_url = urllib.parse.urljoin(curr_url, loc)
                    curr_url = next_url
                    continue

                if response.status_code != 200:
                    logger.debug("PDF download failed with status %d for %s", response.status_code, curr_url)
                    return None

                link_header = response.headers.get("Link")
                candidate_pdf_from_header = (
                    extract_pdf_from_link_header(link_header, curr_url)
                    if allow_html_landing
                    else None
                )

                total_bytes = 0
                header_checked = False
                header_buffer = bytearray()
                is_html_candidate = False
                html_buf = bytearray()

                with open(dest_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PDF_BYTES:
                            logger.warning("PDF download exceeded %d bytes limit for %s", MAX_PDF_BYTES, curr_url)
                            raise ValueError(f"Byte cap exceeded ({total_bytes} > {MAX_PDF_BYTES})")

                        if not header_checked:
                            header_buffer.extend(chunk)
                            if len(header_buffer) >= 5:
                                if header_buffer.startswith(PDF_MAGIC_BYTES):
                                    header_checked = True
                                    f.write(header_buffer)
                                elif allow_html_landing:
                                    content_type = response.headers.get("content-type", "").lower()
                                    if (
                                        candidate_pdf_from_header
                                        or ("html" in content_type)
                                        or header_buffer.lstrip().startswith(
                                            (b"<!doc", b"<!DOC", b"<html", b"<HTML", b"<?xml", b"<head", b"<body")
                                        )
                                    ):
                                        is_html_candidate = True
                                        html_buf.extend(header_buffer)
                                        header_checked = True
                                    else:
                                        logger.warning(
                                            "File magic bytes mismatch for %s: expected %r, got %r",
                                            curr_url,
                                            PDF_MAGIC_BYTES,
                                            bytes(header_buffer[:5]),
                                        )
                                        raise ValueError("Invalid PDF magic bytes")
                                else:
                                    logger.warning(
                                        "File magic bytes mismatch for %s: expected %r, got %r",
                                        curr_url,
                                        PDF_MAGIC_BYTES,
                                        bytes(header_buffer[:5]),
                                    )
                                    raise ValueError("Invalid PDF magic bytes")
                        elif is_html_candidate:
                            html_buf.extend(chunk)
                            if len(html_buf) >= 524288:
                                break
                        else:
                            f.write(chunk)

                if is_html_candidate:
                    if dest_path.exists():
                        try:
                            dest_path.unlink()
                        except OSError:
                            pass
                    pdf_target_url = candidate_pdf_from_header
                    html_text = ""
                    if not pdf_target_url:
                        html_text = html_buf.decode("utf-8", errors="replace")
                        pdf_target_url = extract_pdf_url_from_html(html_text, curr_url)
                    if not pdf_target_url:
                        pdf_target_url = await resolve_dspace7_pdf(
                            client, curr_url, html_text, initial_url=initial_url
                        )
                    if pdf_target_url:
                        logger.info(
                            "Extracted direct PDF URL %s from HTML landing page %s",
                            pdf_target_url,
                            curr_url,
                        )
                        return await _stream_to_file(
                            client=client,
                            initial_url=pdf_target_url,
                            dest_path=dest_path,
                            allow_html_landing=False,
                        )
                    logger.warning("No PDF URL found in HTML landing page %s", curr_url)
                    raise ValueError("Invalid PDF magic bytes")

                if not header_checked:
                    logger.warning("File too short (< 5 bytes) for %s", curr_url)
                    raise ValueError("File too short")

                return str(dest_path.resolve())

        logger.warning("Too many redirects (> %d) for %s", max_redirects, initial_url)
        return None
    except Exception as exc:
        logger.debug("Download and stage failed for %s: %s", curr_url, exc)
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        return None


async def download_and_stage_pdf(
    candidate: CandidatePaper,
    staging_dir: Path,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """
    Download and stage an Open Access PDF for a candidate paper.

    Enforces:
      - SSRF Protection: strictly requires https:// URL scheme.
      - Safe Filename: sanitizes candidate_id against directory traversal.
      - Directory Containment: ensures target path is strictly within staging_dir.
      - Magic Byte Verification: first 5 bytes must equal b"%PDF-".
      - Streaming Byte Cap: aborts if cumulative bytes exceed MAX_PDF_BYTES (100 MB).

    Args:
        candidate: CandidatePaper instance.
        staging_dir: Target batch directory Path.
        client: Optional existing httpx.AsyncClient for connection reuse.

    Returns:
        Absolute file path string on success, or None on failure/non-PDF.
    """
    urls_to_try: list[str] = []
    if candidate.oa_pdf_url and isinstance(candidate.oa_pdf_url, str):
        urls_to_try.append(candidate.oa_pdf_url.strip())
    for alt in getattr(candidate, "oa_pdf_urls", []):
        if alt and isinstance(alt, str):
            clean_alt = alt.strip()
            if clean_alt and clean_alt not in urls_to_try:
                urls_to_try.append(clean_alt)

    if not urls_to_try:
        return None

    raw_id = candidate.candidate_id.strip() if candidate.candidate_id else "candidate"
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", raw_id)
    if not safe_id:
        safe_id = "candidate"
    id_hash = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:8]
    filename = f"{safe_id}_{id_hash}.pdf"

    resolved_staging_dir = Path(staging_dir).resolve()
    dest_path = (resolved_staging_dir / filename).resolve()

    try:
        if not dest_path.is_relative_to(resolved_staging_dir) or dest_path == resolved_staging_dir:
            logger.warning("Path traversal attempt in filename %r", filename)
            return None
    except AttributeError:
        if resolved_staging_dir not in dest_path.parents:
            return None

    resolved_staging_dir.mkdir(parents=True, exist_ok=True)

    async def _try_download(c: httpx.AsyncClient) -> str | None:
        """Attempt downloading candidate PDF across candidate URLs sequentially.

        Args:
            c: Asynchronous HTTP client instance to use for downloads.

        Returns:
            Absolute file path string if downloaded successfully, or None.
        """
        for raw_url in urls_to_try:
            url = raw_url
            if url.lower().startswith("http://"):
                url = "https://" + url[7:]
            elif not url.lower().startswith("https://"):
                logger.warning(
                    "SSRF Protection: Rejected non-https URL %r for candidate %s",
                    url,
                    candidate.candidate_id,
                )
                continue

            res = await _stream_to_file(c, url, dest_path)
            if res and Path(res).exists() and Path(res).stat().st_size > 0:
                return res
        return None

    if client is not None:
        return await _try_download(client)
    else:
        async with httpx.AsyncClient(timeout=DEFAULT_DOWNLOAD_TIMEOUT, follow_redirects=True) as own_client:
            return await _try_download(own_client)

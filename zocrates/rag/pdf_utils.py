"""PDF processing utilities for Zocrates."""

import shutil
import subprocess
from pathlib import Path


def count_pdf_pages(pdf_path: Path) -> int:
    """Estimate page count of a PDF file using pypdf, pdfimages, or byte regex fallback.

    Args:
        pdf_path: Path to the PDF file on disk.

    Returns:
        Integer page count (>= 1 for existing PDF files).
    """
    if not pdf_path.exists() or not pdf_path.is_file():
        return 0

    # 1. Try pypdf if installed
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception:
        pass

    # 2. Try system tool pdfimages
    if shutil.which("pdfimages"):
        try:
            res = subprocess.run(
                ["pdfimages", "-list", str(pdf_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                pages: set[int] = set()
                for line in res.stdout.strip().splitlines():
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        pages.add(int(parts[0]))
                if pages:
                    return max(pages)
        except Exception:
            pass

    # 3. Fast byte regex fallback
    try:
        content = pdf_path.read_bytes()
        import re

        match = re.search(rb"/Count\s+(\d+)", content)
        if match:
            return int(match.group(1))
    except Exception:
        pass

    return 1

"""Sources the documented-leak set (H3) may never use: everything in the Kaggle leak catalog.

The catalog was read while the rules were developed, kl01 and kl13 included even though no case was
built from them. A source is excluded when its URL is one the catalog lists (a host page or a
source), when it is on kaggle.com at all (Kaggle is not one of the registered source kinds), or when
a catalog competition's name is the title of the candidate. URLs are compared without scheme case,
`www.`, query string, fragment or trailing slash.

Usage: python -m bench.docleaks.exclusions URL_OR_TITLE ...
"""

from __future__ import annotations

import csv
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
CATALOG = "bench/kaggle/leak-catalog.csv"


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}"


def _rows(root: Path) -> list[dict[str, str]]:
    with open(root / CATALOG, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def catalog_urls(root: Path = ROOT) -> set[str]:
    """Every URL the catalog lists (host pages and sources), normalised."""
    found = set()
    for row in _rows(root):
        for url in re.split(r"[;\s]+", f"{row['host_url']};{row['source_urls']}"):
            if url.startswith("http"):
                found.add(normalize_url(url))
    return found


def catalog_titles(root: Path = ROOT) -> set[str]:
    return {row["competition"].strip().lower() for row in _rows(root) if row["competition"]}


def digest(root: Path = ROOT) -> str:
    return hashlib.sha256((root / CATALOG).read_bytes()).hexdigest()


def row_count(root: Path = ROOT) -> int:
    return len(_rows(root))


def is_excluded(source: str, root: Path = ROOT) -> bool:
    """Whether a URL or a title names something in the Kaggle catalog (or is on kaggle.com)."""
    text = source.strip()
    if text.lower().startswith("http"):
        url = normalize_url(text)
        return url.split("/")[0].endswith("kaggle.com") or any(
            url == known or url.startswith(known + "/") for known in catalog_urls(root)
        )
    return text.lower() in catalog_titles(root)


def main(argv: list[str] | None = None) -> int:
    names = sys.argv[1:] if argv is None else argv
    hits = [n for n in names if is_excluded(n)]
    for name in hits:
        print(f"{name}: excluded (Kaggle leak catalog)")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
core.html — HTML → clean main text via trafilatura.

Also exposes BeautifulSoup-based link extraction. Every HTML parse path in
the package goes through HTMLExtractor.
"""
from __future__ import annotations
import trafilatura
from bs4 import BeautifulSoup


class HTMLExtractor:
    """Extract clean main-content text and outbound links from HTML."""

    @staticmethod
    def extract_text(html_bytes: bytes, url: str) -> str:
        """Clean article-style text using trafilatura (boilerplate removed)."""
        try:
            html = html_bytes.decode("utf-8", errors="replace")
        except Exception:
            html = html_bytes.decode("latin-1", errors="replace")

        # Trafilatura = best-in-class main-content extractor
        txt = trafilatura.extract(
            html,
            url=url,
            include_tables=True,
            include_links=False,
            favor_recall=True,
            no_fallback=False,
        )
        if txt and len(txt.strip()) > 80:
            return txt.strip()

        # Fallback: BeautifulSoup strip of scripts/styles
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def extract_title(html_bytes: bytes) -> str:
        try:
            soup = BeautifulSoup(html_bytes, "lxml")
        except Exception:
            soup = BeautifulSoup(html_bytes, "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:200]
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)[:200]
        return ""

    @staticmethod
    def extract_links(html_bytes: bytes, base_url: str) -> list[dict[str, str]]:
        """All <a href> links, absolutized and deduped."""
        try:
            soup = BeautifulSoup(html_bytes, "lxml")
        except Exception:
            soup = BeautifulSoup(html_bytes, "html.parser")

        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("javascript:", "mailto:", "#")):
                continue
            full = urljoin(base_url, href)
            if full in seen:
                continue
            seen.add(full)
            text = a.get_text(strip=True)[:200] or "(no text)"
            out.append({
                "url": full,
                "text": text,
                "is_pdf": full.lower().endswith(".pdf") or "/pdf/" in full.lower(),
                "same_domain": urlparse(full).netloc == urlparse(base_url).netloc,
            })
        return out


# ════════════════════════════════════════════════════════════════════════════
#  PDF EXTRACTOR  — pdfminer primary, pypdf fallback
# ════════════════════════════════════════════════════════════════════════════

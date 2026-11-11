"""Download arXiv PDFs and extract text with Poppler."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_TEXT_BYTES = 10 * 1024 * 1024
CACHE_TTL_SECONDS = 3600
CACHE_SCHEMA = 1
_ALLOWED_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_MODERN_ID = re.compile(r"(?P<year>\d{2})(?P<month>0[1-9]|1[0-2])\.(?P<number>\d{4,5})(?:v[1-9]\d*)?\Z", re.ASCII)
_LEGACY_ID = re.compile(r"[a-z][A-Za-z.-]*/(?P<year>\d{2})(?P<month>0[1-9]|1[0-2])(?P<number>\d{3})(?:v[1-9]\d*)?\Z", re.ASCII)
_RATE_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


class SourceError(Exception):
    """A source could not be checked; this is never a negative quote result."""

    def __init__(self, message: str, code: str = "source_unavailable", status: int = 502):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Paper:
    arxiv_id: str
    source_url: str
    pages: tuple[str, ...]
    sha256: str
    fetched_at: str
    cached: bool = False


def parse_arxiv_id(value: str) -> str:
    """Accept an identifier or a trusted arXiv abstract/PDF/HTML URL."""
    if not isinstance(value, str):
        raise SourceError("arxiv_id must be a string.", "invalid_arxiv_id", 400)
    identifier = value.strip()
    if identifier.lower().startswith("arxiv:"):
        identifier = identifier[6:].strip()
    if "://" in identifier:
        try:
            url = urlsplit(identifier)
            if (url.scheme not in {"http", "https"}
                    or url.hostname not in _ALLOWED_HOSTS
                    or url.username is not None or url.password is not None
                    or url.port is not None or url.query or url.fragment):
                raise ValueError("Untrusted URL")
            prefix, _, rest = url.path.lstrip("/").partition("/")
            if prefix not in {"abs", "pdf", "html"}:
                raise ValueError("Unsupported arXiv path")
            identifier = rest
            if prefix == "pdf" and identifier.endswith(".pdf"):
                identifier = identifier[:-4]
        except ValueError:
            raise SourceError("Use an arXiv identifier or an arxiv.org abs/pdf/html URL.", "invalid_arxiv_id", 400) from None
    modern = _MODERN_ID.fullmatch(identifier)
    legacy = _LEGACY_ID.fullmatch(identifier)
    if modern:
        year, month = int(modern["year"]), int(modern["month"])
        valid = (year > 7 or (year == 7 and month >= 4))
        valid = valid and len(modern["number"]) == (4 if year < 15 else 5)
        valid = valid and int(modern["number"]) > 0
    elif legacy:
        year, month = int(legacy["year"]), int(legacy["month"])
        valid = (year >= 91 or year <= 6 or (year == 7 and month <= 3))
        valid = valid and int(legacy["number"]) > 0
    else:
        valid = False
    if not valid:
        raise SourceError("Invalid arXiv identifier.", "invalid_arxiv_id", 400)
    return identifier


def _safe_download_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS
                and parsed.username is None and parsed.password is None
                and parsed.port is None and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


class _ArxivRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_download_url(newurl):
            raise SourceError("arXiv redirected outside the permitted HTTPS hosts.", "unsafe_redirect", 502)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pace_request() -> None:
    global _LAST_REQUEST
    with _RATE_LOCK:
        remaining = 3.0 - (time.monotonic() - _LAST_REQUEST)
        if remaining > 0:
            time.sleep(remaining)
        _LAST_REQUEST = time.monotonic()


class ArxivSource:
    """Fetch PDFs with an optional local text cache."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

    def fetch(self, arxiv_id: str) -> Paper:
        identifier = parse_arxiv_id(arxiv_id)
        cached = self._read_cache(identifier)
        if cached is not None:
            return cached
        url = "https://arxiv.org/pdf/" + identifier
        data = self._download(url)
        paper = self._make_paper(data, identifier, url)
        self._write_cache(paper)
        return paper

    def from_pdf(self, path) -> Paper:
        """Read an explicitly provided local PDF (never exposed by the HTTP API)."""
        try:
            source_path = Path(path).expanduser().resolve(strict=True)
            if not source_path.is_file():
                raise OSError("not a regular file")
            with source_path.open("rb") as stream:
                data = stream.read(MAX_PDF_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise SourceError("Cannot read local PDF: " + str(exc), "local_pdf_unavailable", 400) from exc
        return self._make_paper(data, "local", str(source_path))

    @staticmethod
    def _check_pdf(data: bytes) -> None:
        if len(data) > MAX_PDF_BYTES:
            raise SourceError("PDF exceeds the 30 MiB size limit.", "pdf_too_large", 413)
        if not data.startswith(b"%PDF-"):
            raise SourceError("The source did not return a PDF.", "invalid_pdf", 502)

    def _download(self, url: str) -> bytes:
        request = Request(url, headers={
            "User-Agent": "QuoteVerifier/0.1 (deterministic scholarly citation checker)",
            "Accept": "application/pdf",
            "Accept-Encoding": "identity",
        })
        _pace_request()
        started = time.monotonic()
        try:
            with build_opener(_ArxivRedirectHandler()).open(request, timeout=30) as response:
                if not _safe_download_url(response.geturl()):
                    raise SourceError("Unsafe PDF response URL.", "unsafe_redirect", 502)
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_PDF_BYTES:
                    raise SourceError("PDF exceeds the 30 MiB size limit.", "pdf_too_large", 413)
                data = bytearray()
                while True:
                    if time.monotonic() - started > 60:
                        raise SourceError("Timed out downloading the PDF.", "source_timeout", 504)
                    chunk = response.read(min(65536, MAX_PDF_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_PDF_BYTES:
                        raise SourceError("PDF exceeds the 30 MiB size limit.", "pdf_too_large", 413)
                if length and length.isascii() and length.isdigit() and len(data) != int(length):
                    raise SourceError("arXiv returned an incomplete PDF download.", "incomplete_download", 502)
        except HTTPError as exc:
            if exc.code == 404:
                raise SourceError("No PDF found for this arXiv identifier/version.", "paper_not_found", 404) from exc
            if exc.code == 429:
                raise SourceError("arXiv rate limited the request; retry later.", "source_rate_limited", 503) from exc
            raise SourceError("arXiv returned HTTP " + str(exc.code) + ".", "source_unavailable", 502) from exc
        except (TimeoutError, URLError, OSError, HTTPException) as exc:
            raise SourceError("Could not download the arXiv PDF: " + str(exc), "source_unavailable", 502) from exc
        result = bytes(data)
        self._check_pdf(result)
        return result

    def _make_paper(self, data: bytes, identifier: str, url: str) -> Paper:
        self._check_pdf(data)
        pages = self._extract(data)
        return Paper(identifier, url, pages, hashlib.sha256(data).hexdigest(),
                     datetime.now(timezone.utc).isoformat())

    @staticmethod
    def _extract(data: bytes) -> tuple[str, ...]:
        binary = shutil.which("pdftotext")
        if binary is None:
            raise SourceError("Install Poppler (pdftotext) to extract PDF text.", "extractor_missing", 503)
        try:
            with tempfile.TemporaryDirectory(prefix="quote-verifier-") as temporary:
                pdf_path = Path(temporary) / "paper.pdf"
                text_path = Path(temporary) / "paper.txt"
                pdf_path.write_bytes(data)
                result = subprocess.run(
                    [binary, "-enc", "UTF-8", "-eol", "unix", str(pdf_path), str(text_path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=30, check=False,
                )
                if result.returncode != 0 or not text_path.exists():
                    raise SourceError("Poppler could not extract this PDF.", "extraction_failed", 422)
                with text_path.open("rb") as stream:
                    output = stream.read(MAX_TEXT_BYTES + 1)
                if len(output) > MAX_TEXT_BYTES:
                    raise SourceError("Extracted PDF text exceeds the size limit.", "text_too_large", 413)
                text = output.decode("utf-8", errors="strict")
        except subprocess.TimeoutExpired as exc:
            raise SourceError("PDF text extraction timed out.", "extraction_timeout", 504) from exc
        except (OSError, UnicodeError) as exc:
            raise SourceError("Could not extract readable PDF text.", "extraction_failed", 422) from exc
        pages = text.split("\f")
        if len(pages) > 1 and not pages[-1].strip():
            pages.pop()
        if not pages or any(not page.strip() for page in pages):
            raise SourceError(
                "At least one PDF page has no extractable text; this document cannot be checked reliably.",
                "incomplete_text", 422,
            )
        return tuple(pages)

    def _cache_path(self, identifier: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha256(identifier.encode("utf-8")).hexdigest() + ".json")

    def _read_cache(self, identifier: str) -> Optional[Paper]:
        path = self._cache_path(identifier)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != CACHE_SCHEMA:
                return None
            fields = value["paper"]
            if fields["arxiv_id"] != identifier or fields["source_url"] != "https://arxiv.org/pdf/" + identifier:
                return None
            if not re.fullmatch(r"[0-9a-f]{64}", fields["sha256"]):
                return None
            pages = fields["pages"]
            if not isinstance(pages, list) or not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
                return None
            fetched = datetime.fromisoformat(fields["fetched_at"])
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if age < 0 or (not re.search(r"v[1-9]\d*\Z", identifier) and age > CACHE_TTL_SECONDS):
                return None
            return Paper(identifier, fields["source_url"], tuple(pages), fields["sha256"], fields["fetched_at"], True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _write_cache(self, paper: Paper) -> None:
        path = self._cache_path(paper.arxiv_id)
        if path is None:
            return
        temporary_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                json.dump({"schema": CACHE_SCHEMA, "paper": asdict(replace(paper, cached=False))}, temporary, ensure_ascii=False)
            temporary_path.replace(path)
        except OSError:
            # A read-only/full cache must not turn a successfully checked source into an error.
            pass
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

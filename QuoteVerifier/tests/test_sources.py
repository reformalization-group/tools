from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from quote_verifier.sources import (
    ArxivSource, CACHE_SCHEMA, Paper, SourceError, _ArxivRedirectHandler,
    parse_arxiv_id,
)


PDF = b"%PDF-1.4\nfixture"


class IdentifierTests(unittest.TestCase):
    def test_supported_forms(self):
        for value, expected in [
            ("1706.03762v7", "1706.03762v7"),
            ("arXiv:0704.0001", "0704.0001"),
            ("hep-th/9711200v3", "hep-th/9711200v3"),
            ("math.GT/0309136", "math.GT/0309136"),
            ("https://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://arxiv.org/pdf/hep-th/9711200v1.pdf", "hep-th/9711200v1"),
            ("http://export.arxiv.org/html/2402.08954v1", "2402.08954v1"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_arxiv_id(value), expected)

    def test_rejects_untrusted_or_malformed_identifiers(self):
        for value in [None, "", "arxiv:", "1700.03762", "1713.03762", "1706.00000",
                      "1706.03762v0", "1706.03762v01", "0703.0001", "1706.0376",
                      "1501.1234", "hep-th/0812001", "hep-th/9700001",
                      "２６０１.００００１", "2601.00001v１",
                      "https://arxiv.org.evil.test/pdf/1706.03762",
                      "https://evil.test/pdf/1706.03762",
                      "file:///1706.03762", "https://user@arxiv.org/pdf/1706.03762",
                      "https://arxiv.org:443/pdf/1706.03762",
                      "https://arxiv.org/pdf/1706.03762?download=1",
                      "https://arxiv.org/abs/1706.03762#x",
                      "https://arxiv.org/src/1706.03762", "../../etc/passwd"]:
            with self.subTest(value=value), self.assertRaises(SourceError) as caught:
                parse_arxiv_id(value)
            self.assertEqual(caught.exception.status, 400)


class RetrievalTests(unittest.TestCase):
    def test_http_404_is_an_error(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://arxiv.org/pdf/1706.03762v99", 404, "not found", {}, None)
        with patch("quote_verifier.sources.build_opener", return_value=opener), patch("quote_verifier.sources._pace_request"):
            with self.assertRaises(SourceError) as caught:
                ArxivSource().fetch("1706.03762v99")
        self.assertEqual(caught.exception.code, "paper_not_found")

    def test_download_only_pdf(self):
        response = io.BytesIO(b"<html>Temporarily unavailable</html>")
        response.headers = {}
        response.geturl = lambda: "https://arxiv.org/pdf/1706.03762"
        opener = Mock()
        opener.open.return_value = response
        with patch("quote_verifier.sources.build_opener", return_value=opener), patch("quote_verifier.sources._pace_request"):
            with self.assertRaises(SourceError) as caught:
                ArxivSource().fetch("1706.03762")
        self.assertEqual(caught.exception.code, "invalid_pdf")

    def test_download_is_bounded_even_without_content_length(self):
        response = io.BytesIO(PDF + b"x" * 100)
        response.headers = {}
        response.geturl = lambda: "https://arxiv.org/pdf/1706.03762"
        opener = Mock()
        opener.open.return_value = response
        with patch("quote_verifier.sources.build_opener", return_value=opener), patch("quote_verifier.sources._pace_request"), patch("quote_verifier.sources.MAX_PDF_BYTES", 40):
            with self.assertRaises(SourceError) as caught:
                ArxivSource().fetch("1706.03762")
        self.assertEqual(caught.exception.code, "pdf_too_large")

    def test_truncated_download_is_not_checked(self):
        response = io.BytesIO(PDF)
        response.headers = {"Content-Length": str(len(PDF) + 100)}
        response.geturl = lambda: "https://arxiv.org/pdf/1706.03762"
        opener = Mock()
        opener.open.return_value = response
        with patch("quote_verifier.sources.build_opener", return_value=opener), patch("quote_verifier.sources._pace_request"):
            with self.assertRaises(SourceError) as caught:
                ArxivSource().fetch("1706.03762")
        self.assertEqual(caught.exception.code, "incomplete_download")

    def test_redirect_rejects_external_and_insecure_targets(self):
        handler = _ArxivRedirectHandler()
        for target in ["https://localhost/paper", "http://arxiv.org/pdf/1706.03762", "https://arxiv.org:8443/pdf/1706.03762", "https://arxiv.org/pdf/1706.03762?url=http://localhost"]:
            with self.subTest(target=target), self.assertRaises(SourceError):
                handler.redirect_request(None, None, 302, "found", {}, target)

    def test_downloaded_source_provenance(self):
        response = io.BytesIO(PDF)
        response.headers = {"Content-Length": str(len(PDF))}
        response.geturl = lambda: "https://arxiv.org/pdf/hep-th/9711200v3"
        opener = Mock()
        opener.open.return_value = response
        with patch("quote_verifier.sources.build_opener", return_value=opener), patch("quote_verifier.sources._pace_request"), patch.object(ArxivSource, "_extract", return_value=("Page one", "Page two")):
            paper = ArxivSource().fetch("hep-th/9711200v3")
        self.assertEqual(paper.arxiv_id, "hep-th/9711200v3")
        self.assertEqual(len(paper.sha256), 64)
        self.assertEqual(paper.pages, ("Page one", "Page two"))
        self.assertFalse(paper.cached)
        self.assertIsNotNone(datetime.fromisoformat(paper.fetched_at).tzinfo)

    def test_local_pdf_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pdf"
            path.write_bytes(PDF)
            with patch.object(ArxivSource, "_extract", return_value=("Local paper",)):
                paper = ArxivSource().from_pdf(path)
        self.assertEqual(paper.arxiv_id, "local")
        self.assertEqual(paper.source_url, str(path.resolve()))

    def test_local_missing_file_is_explicit_error(self):
        with self.assertRaises(SourceError) as caught:
            ArxivSource().from_pdf("/definitely/missing/quote-verifier.pdf")
        self.assertEqual(caught.exception.code, "local_pdf_unavailable")


class ExtractionTests(unittest.TestCase):
    def extract_stub(self, output):
        def run(command, **kwargs):
            Path(command[-1]).write_bytes(output)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with patch("quote_verifier.sources.shutil.which", return_value="/bin/pdftotext"), patch("quote_verifier.sources.subprocess.run", side_effect=run):
            return ArxivSource._extract(PDF)

    def test_page_boundaries_preserved_without_trailing_phantom_page(self):
        self.assertEqual(self.extract_stub(b"First page\n\fSecond page\n\f"), ("First page\n", "Second page\n"))

    def test_empty_or_partly_scanned_document_is_error(self):
        for output in [b"", b"\f", b"Text\f\f", b"Text\f \n\fLast page\f"]:
            with self.subTest(output=output), self.assertRaises(SourceError) as caught:
                self.extract_stub(output)
            self.assertEqual(caught.exception.code, "incomplete_text")

    def test_missing_poppler_is_explicit(self):
        with patch("quote_verifier.sources.shutil.which", return_value=None), self.assertRaises(SourceError) as caught:
            ArxivSource._extract(PDF)
        self.assertEqual(caught.exception.code, "extractor_missing")

    def test_extraction_timeout_is_explicit(self):
        with patch("quote_verifier.sources.shutil.which", return_value="/bin/pdftotext"), patch("quote_verifier.sources.subprocess.run", side_effect=subprocess.TimeoutExpired("pdftotext", 30)), self.assertRaises(SourceError) as caught:
            ArxivSource._extract(PDF)
        self.assertEqual(caught.exception.code, "extraction_timeout")

    def test_extraction_size_is_bounded(self):
        with patch("quote_verifier.sources.MAX_TEXT_BYTES", 5), self.assertRaises(SourceError) as caught:
            self.extract_stub(b"much longer text")
        self.assertEqual(caught.exception.code, "text_too_large")


class CacheTests(unittest.TestCase):
    def test_versioned_document_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ArxivSource(Path(directory))
            with patch.object(source, "_download", return_value=PDF) as download, patch.object(source, "_extract", return_value=("Paper body",)):
                first = source.fetch("1706.03762v7")
                second = source.fetch("https://arxiv.org/abs/1706.03762v7")
                different_version = source.fetch("1706.03762v6")
            self.assertEqual(download.call_count, 2)
            self.assertTrue(second.cached)
            self.assertFalse(different_version.cached)
            self.assertEqual(first.sha256, second.sha256)

    def test_unversioned_cache_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ArxivSource(Path(directory))
            old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            source._write_cache(Paper("1706.03762", "https://arxiv.org/pdf/1706.03762", ("Old text",), "a" * 64, old_time))
            with patch.object(source, "_download", return_value=PDF) as download, patch.object(source, "_extract", return_value=("New text",)):
                paper = source.fetch("1706.03762")
            download.assert_called_once()
            self.assertEqual(paper.pages, ("New text",))

    def test_corrupt_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ArxivSource(Path(directory))
            source._cache_path("1706.03762v7").write_text("not json")
            with patch.object(source, "_download", return_value=PDF) as download, patch.object(source, "_extract", return_value=("Paper body",)):
                source.fetch("1706.03762v7")
            download.assert_called_once()

    def test_cache_source_identity_cannot_be_swapped(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ArxivSource(Path(directory))
            value = {"schema": CACHE_SCHEMA, "paper": {
                "arxiv_id": "1706.03762v7", "source_url": "https://evil.test/paper",
                "sha256": "a" * 64, "pages": ["forged"], "fetched_at": datetime.now(timezone.utc).isoformat(),
            }}
            source._cache_path("1706.03762v7").write_text(json.dumps(value))
            self.assertIsNone(source._read_cache("1706.03762v7"))


if __name__ == "__main__":
    unittest.main()

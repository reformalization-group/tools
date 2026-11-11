import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from quote_verifier.cli import main
from quote_verifier.sources import SourceError


class CliTests(unittest.TestCase):
    def run_cli(self, args, results=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("quote_verifier.cli.Verifier") as factory:
            verifier = factory.return_value
            verifier.verify.side_effect = results
            verifier.verify_pdf.side_effect = results
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(args)
        return code, stdout.getvalue(), stderr.getvalue(), verifier

    @staticmethod
    def result(found):
        result = Mock()
        result.to_dict.return_value = {"found": found, "arxiv_id": "1706.03762v1", "mode": "normalized"}
        return result

    def test_check_true_and_false_have_distinct_exit_codes(self):
        for found, expected in ((True, 0), (False, 1)):
            code, out, err, verifier = self.run_cli(
                ["check", "1706.03762v1", "--quote", "Exact sentence."], [self.result(found)]
            )
            self.assertEqual(code, expected)
            self.assertIs(json.loads(out), found)
            self.assertEqual(err, "")
            verifier.verify.assert_called_once_with("1706.03762v1", "Exact sentence.", mode="normalized")

    def test_details_and_quote_file_preserve_input(self):
        with tempfile.TemporaryDirectory() as directory:
            quote = Path(directory) / "quote.txt"
            quote.write_text("A sentence.\n", encoding="utf-8")
            code, out, err, verifier = self.run_cli(
                ["check", "1706.03762v1", "--quote-file", str(quote), "--mode", "verbatim", "--details"],
                [self.result(True)],
            )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["found"])
        self.assertEqual(err, "")
        verifier.verify.assert_called_once_with("1706.03762v1", "A sentence.\n", mode="verbatim")

    def test_local_pdf_uses_explicit_local_interface(self):
        code, out, err, verifier = self.run_cli(
            ["check-pdf", "paper.pdf", "--quote", "Local quote."], [self.result(True)]
        )
        self.assertEqual((code, out, err), (0, "true\n", ""))
        verifier.verify_pdf.assert_called_once_with("paper.pdf", "Local quote.", mode="normalized")
        verifier.verify.assert_not_called()

    def test_source_error_is_not_false(self):
        code, out, err, _ = self.run_cli(
            ["check", "1706.03762v1", "--quote", "A quote"],
            [SourceError("Offline", code="source_unavailable", status=502)],
        )
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "source_unavailable")

    def test_argument_errors_are_json(self):
        code, out, err, verifier = self.run_cli(["check", "1706.03762v1"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("error", json.loads(err))
        verifier.verify.assert_not_called()

    def test_lint_validates_entire_manifest_before_fetching(self):
        for bad in ({"arxiv_id": "1706.03762v1", "quote": "Q", "line": True},
                    {"arxiv_id": "http://localhost/private", "quote": "Q"},
                    {"arxiv_id": "1706.03762v1", "quote": " "},
                    {"arxiv_id": "1706.03762v1", "quote": "Q", "mode": "fuzzy"}):
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "references.json"
                manifest.write_text(json.dumps([{"arxiv_id": "1706.03762v1", "quote": "Fine"}, bad]))
                code, out, err, verifier = self.run_cli(["lint", str(manifest)])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("entry 2", json.loads(err)["error"]["message"])
            verifier.verify.assert_not_called()

    def test_lint_continues_and_reports_errors_missing_and_found(self):
        records = [{"arxiv_id": "1706.03762v1", "quote": str(i), "name": "ref" + str(i),
                    "file": "Physlib/Test.lean", "line": i + 1} for i in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "references.json"
            manifest.write_text(json.dumps(records))
            code, out, err, verifier = self.run_cli(
                ["lint", str(manifest)],
                [self.result(True), SourceError("Unavailable"), self.result(False)],
            )
        self.assertEqual((code, err), (2, ""))
        report = json.loads(out)
        self.assertEqual(report["summary"], {"checked": 3, "found": 1, "missing": 1, "errors": 1})
        self.assertEqual(verifier.verify.call_count, 3)
        self.assertEqual(report["results"][0]["file"], "Physlib/Test.lean")
        self.assertIsNone(report["results"][1]["found"])
        self.assertEqual(report["results"][2]["line"], 3)

    def test_lint_exit_codes(self):
        for found, expected in ((True, 0), (False, 1)):
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "references.json"
                manifest.write_text(json.dumps([{"arxiv_id": "1706.03762v1", "quote": "Q"}]))
                code, out, err, _ = self.run_cli(["lint", str(manifest)], [self.result(found)])
            self.assertEqual(code, expected)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["summary"]["checked"], 1)

    def test_serve_uses_validated_settings(self):
        with patch("quote_verifier.server.serve") as serve:
            code, out, err, verifier = self.run_cli(["serve", "--port", "8766"])
        self.assertEqual((code, out, err), (0, "", ""))
        serve.assert_called_once_with("127.0.0.1", 8766, verifier)
        code, out, err, _ = self.run_cli(["serve", "--port", "99999"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Port", json.loads(err)["error"]["message"])


if __name__ == "__main__":
    unittest.main()

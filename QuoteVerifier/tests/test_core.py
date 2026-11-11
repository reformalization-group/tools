import unittest
from unittest.mock import Mock

from quote_verifier.core import Verifier, match_paper, normalize
from quote_verifier.sources import Paper, SourceError


def paper(*pages):
    return Paper("1706.03762v1", "https://arxiv.org/pdf/1706.03762v1", tuple(pages),
                 "a" * 64, "2026-11-11T00:00:00+00:00")


class MatchingTests(unittest.TestCase):
    def test_verbatim_evidence(self):
        result = match_paper(paper("intro", "A conserved charge is constant."), "charge is constant")
        self.assertTrue(result.found)
        self.assertEqual(result.match["kind"], "verbatim")
        self.assertEqual(result.match["page_start"], 2)
        self.assertEqual(result.match["page_end"], 2)
        text = "intro\fA conserved charge is constant."
        self.assertEqual(text[result.match["start"]:result.match["end"]], result.match["text"])
        self.assertEqual(result.to_dict()["sha256"], "a" * 64)

    def test_whitespace_and_ligatures(self):
        result = match_paper(paper("The eﬀective\n\tﬁeld\u00a0theory."), "The effective field theory.")
        self.assertTrue(result.found)
        self.assertEqual(result.match["kind"], "normalized")
        self.assertEqual(normalize(result.match["text"]), "The effective field theory.")

    def test_verbatim_preserves_newlines(self):
        self.assertFalse(match_paper(paper("The\nfield"), "The field", "verbatim").found)
        self.assertTrue(match_paper(paper("The\nfield"), "The\nfield", "verbatim").found)

    def test_cross_page_evidence(self):
        result = match_paper(paper("A conserved", "charge is constant."), "conserved charge")
        self.assertTrue(result.found)
        self.assertEqual((result.match["page_start"], result.match["page_end"]), (1, 2))
        self.assertFalse(match_paper(paper("some", "thing"), "something").found)

    def test_missing_is_false(self):
        result = match_paper(paper("A conserved charge is constant."), "A conserved charge is not constant.")
        self.assertFalse(result.found)
        self.assertIsNone(result.match)

    def test_no_semantic_or_mathematical_rewriting(self):
        for source, quote in [("Mass", "mass"), ("x²", "x2"), ("x−y", "x-y"),
                              ("mass, charge", "mass charge"), ("not conserved", "is conserved"),
                              ("multi-\nparticle", "multiparticle"), ("“word”", '"word"')]:
            with self.subTest(source=source, quote=quote):
                self.assertFalse(match_paper(paper(source), quote).found)

    def test_no_partial_ligature_match(self):
        self.assertFalse(match_paper(paper("ﬃ"), "fi").found)
        # A rejected partial-ligature occurrence must not hide a later valid one.
        result = match_paper(paper("ﬀ i, then f\n i"), "f i")
        self.assertTrue(result.found)
        self.assertEqual(result.match["text"], "f\n i")

    def test_invalid_quote_does_not_fetch(self):
        source = Mock()
        verifier = Verifier(source)
        for quote in [None, False, [], "", " \n ", "a" * 20001, "a\0b"]:
            with self.subTest(quote=str(quote)[:20]), self.assertRaises(ValueError):
                verifier.verify("1706.03762v1", quote)
        with self.assertRaises(ValueError):
            verifier.verify("1706.03762v1", "quote", mode="fuzzy")
        source.fetch.assert_not_called()

    def test_source_errors_never_become_false(self):
        source = Mock()
        source.fetch.side_effect = SourceError("Cannot read paper")
        with self.assertRaises(SourceError):
            Verifier(source).verify("1706.03762v1", "a quote")


if __name__ == "__main__":
    unittest.main()

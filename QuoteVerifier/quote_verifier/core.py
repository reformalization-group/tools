"""Conservative substring matching with traceable PDF text offsets."""

from bisect import bisect_right
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from .sources import ArxivSource, Paper

MAX_QUOTE_LENGTH = 20_000
MODES = ("normalized", "verbatim")
# Deliberately avoid NFKC: it would equate x² and x2, among other changes.
LIGATURES = str.maketrans({
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})


def validate_quote(quote: str, mode: str = "normalized") -> None:
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("quote must be a nonempty string")
    if len(quote) > MAX_QUOTE_LENGTH:
        raise ValueError("quote exceeds the 20000-character limit")
    if any(ord(char) < 32 and char not in "\t\r\n\f" for char in quote):
        raise ValueError("quote contains unsupported control characters")
    if mode not in MODES:
        raise ValueError("mode must be 'normalized' or 'verbatim'")


def normalize(text: str) -> str:
    """Collapse whitespace and expand seven typographic Latin ligatures only."""
    return " ".join(text.translate(LIGATURES).split())


def _normalized_with_offsets(text: str) -> Tuple[str, list, list]:
    chars, starts, ends = [], [], []
    for offset, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] == " ":
                ends[-1] = offset + 1
            elif chars:
                chars.append(" ")
                starts.append(offset)
                ends.append(offset + 1)
        else:
            for expanded in char.translate(LIGATURES):
                chars.append(expanded)
                starts.append(offset)
                ends.append(offset + 1)
    if chars and chars[-1] == " ":
        chars.pop()
        starts.pop()
        ends.pop()
    return "".join(chars), starts, ends


@dataclass(frozen=True)
class VerificationResult:
    found: bool
    arxiv_id: str
    mode: str
    source_url: str
    sha256: str
    fetched_at: str
    cached: bool
    match: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def match_paper(paper: Paper, quote: str, mode: str = "normalized") -> VerificationResult:
    """Search extracted reading-order text; offsets index pages joined by form feed."""
    validate_quote(quote, mode)
    text = "\f".join(paper.pages)
    start = text.find(quote)
    end = start + len(quote)
    kind = "verbatim"
    if start < 0 and mode == "normalized":
        needle = normalize(quote)
        # Most checks need no offset map. Build it only when a match exists.
        normalized = normalize(text)
        normalized_start = normalized.find(needle)
        if normalized_start >= 0:
            _, starts, ends = _normalized_with_offsets(text)
            while normalized_start >= 0:
                start = starts[normalized_start]
                end = ends[normalized_start + len(needle) - 1]
                kind = "normalized"
                # Never match just part of an expanded ligature (e.g. 'f' in 'ﬁ').
                if normalize(text[start:end]) == needle:
                    break
                start = -1
                normalized_start = normalized.find(needle, normalized_start + 1)

    match = None
    if start >= 0:
        page_starts = [0]
        for page in paper.pages[:-1]:
            page_starts.append(page_starts[-1] + len(page) + 1)
        match = {
            "kind": kind,
            "page_start": bisect_right(page_starts, start),
            "page_end": bisect_right(page_starts, end - 1),
            "start": start,
            "end": end,
            "text": text[start:end],
            "context": text[max(0, start - 100):min(len(text), end + 100)],
        }
    return VerificationResult(
        found=match is not None, arxiv_id=paper.arxiv_id, mode=mode,
        source_url=paper.source_url, sha256=paper.sha256, fetched_at=paper.fetched_at,
        cached=paper.cached, match=match,
    )


class Verifier:
    def __init__(self, source=None):
        self.source = source if source is not None else ArxivSource()

    def verify(self, arxiv_id: str, quote: str, mode: str = "normalized") -> VerificationResult:
        validate_quote(quote, mode)
        return match_paper(self.source.fetch(arxiv_id), quote, mode)

    def verify_pdf(self, path, quote: str, mode: str = "normalized") -> VerificationResult:
        validate_quote(quote, mode)
        return match_paper(self.source.from_pdf(path), quote, mode)

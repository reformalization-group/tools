"""Command-line checks and a JSON-manifest linter suitable for CI."""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .core import Verifier, validate_quote
from .sources import ArxivSource, SourceError, parse_arxiv_id


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parser():
    parser = _Parser(description="Verify exact quotes against arXiv papers.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, argument in (("check", "arxiv_id"), ("check-pdf", "pdf")):
        check = commands.add_parser(command)
        check.add_argument(argument)
        quote = check.add_mutually_exclusive_group(required=True)
        quote.add_argument("--quote", help="Exact quoted text.")
        quote.add_argument("--quote-file", type=Path, help="UTF-8 file containing the quote.")
        check.add_argument("--mode", choices=("normalized", "verbatim"), default="normalized")
        check.add_argument("--details", action="store_true", help="Include the matched text and page number.")
        check.add_argument("--cache-dir", type=Path)
    lint = commands.add_parser("lint", help="Check a JSON array of citation records.")
    lint.add_argument("manifest", type=Path)
    lint.add_argument("--cache-dir", type=Path)
    serve = commands.add_parser("serve", help="Run the local HTTP API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--cache-dir", type=Path)
    return parser


def _read_utf8(path, max_bytes):
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("{} exceeds the {} byte input limit".format(path, max_bytes))
    return data.decode("utf-8")


def _load_manifest(path):
    records = json.loads(_read_utf8(path, 10 * 1024 * 1024))
    if not isinstance(records, list):
        raise ValueError("Manifest must be a JSON array of citation records")
    allowed = {"arxiv_id", "quote", "mode", "name", "file", "line"}
    validated = []
    for index, record in enumerate(records):
        label = "Manifest entry {}".format(index + 1)
        if not isinstance(record, dict):
            raise ValueError(label + " must be an object")
        unknown = set(record) - allowed
        if unknown:
            raise ValueError(label + " has unknown fields: " + ", ".join(sorted(unknown)))
        if not isinstance(record.get("arxiv_id"), str):
            raise ValueError(label + " needs a string arxiv_id")
        try:
            arxiv_id = parse_arxiv_id(record["arxiv_id"])
        except (ValueError, SourceError) as exc:
            raise ValueError(label + ": " + str(exc)) from exc
        mode = record.get("mode", "normalized")
        try:
            validate_quote(record.get("quote"), mode)
        except ValueError as exc:
            raise ValueError(label + ": " + str(exc)) from exc
        for key in ("name", "file"):
            if key in record and (not isinstance(record[key], str) or not record[key].strip()):
                raise ValueError(label + " " + key + " must be a nonempty string")
        if "line" in record and (type(record["line"]) is not int or record["line"] < 1):
            raise ValueError(label + " line must be a positive integer")
        validated.append(dict(record, arxiv_id=arxiv_id, mode=mode))
    return validated


def _write_json(value, stream=None):
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout)


def _error(exc):
    return {"code": getattr(exc, "code", "invalid_input"), "message": str(exc)}


def _lint(records, verifier):
    results = []
    summary = {"checked": len(records), "found": 0, "missing": 0, "errors": 0}
    for index, record in enumerate(records):
        item = {key: record[key] for key in ("name", "file", "line") if key in record}
        item["index"] = index
        try:
            result = verifier.verify(record["arxiv_id"], record["quote"], mode=record["mode"])
            item.update(result.to_dict())
            summary["found" if item["found"] else "missing"] += 1
        except (SourceError, ValueError, OSError) as exc:
            item.update(arxiv_id=record["arxiv_id"], mode=record["mode"], found=None, error=_error(exc))
            summary["errors"] += 1
        results.append(item)
    _write_json({"results": results, "summary": summary})
    return 2 if summary["errors"] else (1 if summary["missing"] else 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Exit 0 for success, 1 for a missing quote, or 2 for input/source errors."""
    try:
        args = _parser().parse_args(argv)
        # Validate the whole manifest before the first network operation.
        records = _load_manifest(args.manifest) if args.command == "lint" else None
        if args.command == "serve" and not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        verifier = Verifier(source=ArxivSource(cache_dir=args.cache_dir))
        if args.command == "lint":
            return _lint(records, verifier)
        if args.command == "serve":
            from .server import serve

            serve(args.host, args.port, verifier)
            return 0
        quote = args.quote if args.quote is not None else _read_utf8(args.quote_file, 1024 * 1024)
        if args.command == "check":
            result = verifier.verify(args.arxiv_id, quote, mode=args.mode)
        else:
            result = verifier.verify_pdf(args.pdf, quote, mode=args.mode)
        output = result.to_dict()
        _write_json(output if args.details else output["found"])
        return 0 if output["found"] else 1
    except (SourceError, ValueError, OSError) as exc:
        _write_json({"error": _error(exc)}, stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

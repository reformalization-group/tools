# Paper quote verifier

Check whether an exact quote appears in an arXiv paper. Includes a Python API,
CLI, local HTTP server, and reference linter. No AI model or API key is used.

Requires Python 3.9+ and Poppler's `pdftotext`: `brew install poppler` on macOS,
or `sudo apt-get install poppler-utils` on Debian/Ubuntu. No Python dependencies.

## Usage

Run from this directory:

```sh
python3 -m quote_verifier check 1706.03762v1 --quote 'Attention Is All You Need'
# true
python3 -m quote_verifier check-pdf paper.pdf --quote-file quote.txt
python3 -m quote_verifier serve --cache-dir .cache
```

The server defaults to `127.0.0.1:8765`:

```sh
curl --fail-with-body http://127.0.0.1:8765/verify \
  -H 'Content-Type: application/json' \
  -d '{"arxiv_id":"1706.03762v1","quote":"Attention Is All You Need"}'
```

`check --details` and `POST /verify/details` return the result with PDF page
numbers, matched text, context, source URL, PDF hash, and fetch time.
Python callers can use `Verifier().verify(arxiv_id, quote).found` after
`from quote_verifier import Verifier`.

Modern and legacy arXiv IDs and arxiv.org URLs are accepted. Use versioned IDs
for repeatable checks. `--cache-dir` enables disk caching; versioned entries
persist and unversioned entries expire after an hour. Delete the cache to refetch.

## Reference linter

```sh
python3 -m quote_verifier lint examples/references.json --cache-dir .cache
```

The manifest is a JSON array:

```json
[{"arxiv_id": "1706.03762v1", "quote": "Attention Is All You Need"}]
```

Optional fields: `name`, `file`, `line` (one-based), and `mode`. File and line
label the output; they do not cause Lean files to be parsed. Output contains
per-entry `results` and a `summary`. CLI exits: **0** all found, **1** missing
quote, **2** invalid input or retrieval/extraction error.

Use the manifest to integrate this checker with projects such as Physlib.

## Matching

The default `normalized` mode is case-sensitive, collapses whitespace, and
expands Latin printing ligatures (`ﬁ` → `fi`). Punctuation, hyphens, and mathematical
symbols are preserved. `--mode verbatim` (HTTP `"mode":"verbatim"`) requires
literal extracted characters, including whitespace.

`false` means no match in the extracted PDF text. Equations, scans, columns,
and line-end hyphenation can cause missed matches; there is no OCR. Pages with
no extractable text cause an error. Presence does not establish scientific truth
or support for a formal statement. Retrieval/extraction failures return HTTP
errors, never `false`; manifest failures have `found: null` and an `error`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests run offline and include HTTP checks on a temporary loopback port.

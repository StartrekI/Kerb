"""Ingest any scraper's export -- the wedge.

Someone already holding 15,000 rows from gosom, Apify or Outscraper can get
value here in under a minute, with no install beyond this one and no scraping
at all. What they see is what their own filter missed: the businesses renting
a booking page that `website IS NULL` silently discarded, the art galleries
matching a painting search, the apartment blocks matching a roofing search.

Known layouts are auto-detected by header signature. Anything else maps by
hand once and the mapping is saved as a reusable profile.

Nothing is discarded: unmapped columns are preserved in `extras`, because a
field we ignore today is a signal someone writes next month and re-fetching
to recover it costs far more than storing it.
"""

from __future__ import annotations

import csv
import itertools
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..models import Business, SourceQuery
from . import source

# Google's place id, as embedded in every /maps/place/ URL.
CID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
CID_PARAM_RE = re.compile(r"[?&]cid=(\d+)")

# Canonical field -> the column names different tools use for it. Order is
# priority: the first header present wins.
ALIASES: Dict[str, List[str]] = {
    "cid":          ["cid", "place_id", "placeid", "google_id", "fid", "data_id"],
    "name":         ["name", "title", "business_name", "businessname", "company"],
    "category":     ["category", "categories", "type", "main_category", "primary_category"],
    "address":      ["address", "full_address", "formatted_address", "street_address"],
    "phone":        ["phone", "phone_number", "telephone", "international_phone_number"],
    "website":      ["website", "site", "web_site", "url", "domain"],
    "booking_url":  ["booking_url", "reservation_url", "book_online", "reserve_url"],
    "rating":       ["rating", "review_rating", "stars", "average_rating", "score"],
    "review_count": ["review_count", "reviews", "reviews_count", "user_ratings_total",
                     "number_of_reviews", "review_number"],
    "lat":          ["lat", "latitude"],
    "lng":          ["lng", "lon", "long", "longitude"],
    "status_raw":   ["status", "business_status", "permanently_closed", "state"],
    "maps_url":     ["maps_url", "link", "google_maps_url", "url_maps", "google_url"],
}

# Header signatures that identify a known producer, for auto-mapping.
PROFILES = {
    "gosom":      {"title", "category", "review_count", "cid"},
    "apify":      {"title", "categoryName", "totalScore", "placeId"},
    "outscraper": {"name", "full_address", "reviews", "place_id"},
}


def detect_profile(headers: List[str]) -> Optional[str]:
    have = {h.strip() for h in headers}
    lower = {h.lower() for h in have}
    for name, need in PROFILES.items():
        if {n.lower() for n in need} <= lower:
            return name
    return None


def build_mapping(headers: List[str],
                  override: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """canonical field -> actual header, resolved by alias priority.

    Headers are coerced defensively: a ragged row can put a None among them,
    and a header row is not guaranteed to be strings at all.
    """
    lower = {str(h).lower().strip(): h for h in headers if h is not None}
    mapping: Dict[str, str] = {}
    for canon, names in ALIASES.items():
        for candidate in names:
            if candidate in lower:
                mapping[canon] = lower[candidate]
                break
    mapping.update(override or {})
    return mapping


def _num(v, cast=float):
    """Parse a number out of whatever a scraper wrote.

    Always goes via float, even when an int is wanted. Exports disagree about
    formatting -- gosom writes `142`, Apify and Outscraper commonly write
    `142.0` -- and `int("142.0")` raises. That silently produced
    review_count=None, which then failed every `reviews >= N` filter and threw
    away good businesses for a formatting difference.
    """
    if v in (None, ""):
        return None
    try:
        n = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    try:
        return cast(n)
    except (TypeError, ValueError, OverflowError):
        return None


def extract_cid(row: Dict[str, Any], mapping: Dict[str, str]) -> Optional[str]:
    """Identity, in order of trustworthiness.

    A row with no derivable id is dropped rather than given a synthetic key:
    a record we cannot match to anything is worse than a missing one, because
    it will silently duplicate the next time the same business appears.
    """
    raw = str(row.get(mapping.get("cid", ""), "") or "").strip()
    if raw and raw.lower() not in ("none", "nan", "null"):
        return raw.lower()

    url = str(row.get(mapping.get("maps_url", ""), "") or "")
    m = CID_RE.search(url) or CID_PARAM_RE.search(url)
    if m:
        return m.group(1).lower()
    return None


def decode(raw: bytes) -> str:
    """Bytes to text, handling what spreadsheets actually produce.

    Excel writes UTF-8 *with a BOM* by default, and "Unicode Text" is UTF-16.
    Decoding a BOM'd file as plain utf-8 leaves a zero-width \\ufeff glued to
    the first header, so `cid` became `\\ufeffcid`, matched no alias, and every
    single row was dropped as having no identity -- a silent, total failure on
    a perfectly good file.
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _encoding(path: Path) -> str:
    """Peek at the first bytes only, so a 2GB file is not read to pick a codec."""
    with path.open("rb") as fh:
        head = fh.read(4)
    return "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"


def _first_char(path: Path, encoding: str) -> str:
    with path.open(encoding=encoding, errors="replace") as fh:
        while True:
            ch = fh.read(1)
            if not ch:
                return ""
            if not ch.isspace():
                return ch


def read_rows(path: Path) -> Iterator[Dict[str, Any]]:
    """CSV, JSON array, or JSONL -- decided by content, not by extension.

    Every read goes through the path guard. Checking here rather than in a
    config validator means a future campaign key that names a file is covered
    without anyone remembering to add it.

    CSV and JSONL stream a line at a time. Only JSON has to be held whole,
    because a JSON document cannot be parsed incrementally without a streaming
    parser, and adding one to read a format nobody exports millions of rows in
    is not worth the dependency.
    """
    from ..paths import check as _check
    path = _check(path)
    encoding = _encoding(path)
    lead = _first_char(path, encoding)

    if lead == "":
        return
    if lead == "{":
        # Three different files start with a brace, and only the line count
        # separates them cheaply:
        #   many lines, first parses    -> JSONL, stream it
        #   many lines, first does not  -> pretty-printed wrapper, parse whole
        #   one line                    -> compact wrapper, or a lone record
        with path.open(encoding=encoding, errors="replace") as fh:
            first = fh.readline()
            more = any(line.strip() for line in fh)
        try:
            doc = json.loads(first)
        except json.JSONDecodeError:
            yield from _wrapped_document(path, encoding)
            return
        if more:
            yield from _jsonl(path, encoding)
            return
        # A single line that parsed. It is a wrapper if some value is a list of
        # records; otherwise it is one record. Checking the parsed object beats
        # guessing from the text -- a compact {"results":[...]} is a perfectly
        # valid single JSONL line, and treating it as one yielded the wrapper
        # instead of the rows inside it.
        if isinstance(doc, dict):
            for value in doc.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    yield from value
                    return
            yield doc
        return
    if lead == "[":
        with path.open(encoding=encoding, errors="replace") as fh:
            for row in json.load(fh):
                if isinstance(row, dict):
                    yield row
        return

    yield from _csv_rows(path, encoding)


# Python's csv refuses any field over 128KB. Real exports carry a pasted
# description or a serialised review blob that exceeds it, and the whole run
# died on one row. Raised to something no legitimate field reaches, but still
# bounded -- sys.maxsize would let one malformed file eat all the memory.
MAX_FIELD = 16 * 1024 * 1024
try:
    csv.field_size_limit(MAX_FIELD)
except (OverflowError, ValueError):              # 32-bit platforms
    csv.field_size_limit(2 ** 30)

# DictReader puts columns beyond the header under a key of None, so a single
# ragged row (an unescaped comma, which is endemic in scraper exports) put None
# among the header names and every downstream `.lower()` raised.
EXTRA_KEY = "_extra_columns"


def _clean_lines(fh) -> Iterator[str]:
    """Strip NUL bytes, which csv rejects outright with 'line contains NUL'.

    Stray NULs turn up in exports written by tools that mishandle encodings.
    One of them anywhere in a 200,000-row file used to abort the entire run.
    """
    for line in fh:
        yield line.replace("\0", "") if "\0" in line else line


def _csv_rows(path: Path, encoding: str) -> Iterator[Dict[str, Any]]:
    """Stream a CSV, surviving the rows that are malformed.

    A bad row costs that row. Aborting the file loses the other 199,999.
    """
    with path.open(encoding=encoding, errors="replace", newline="") as fh:
        reader = csv.DictReader(_clean_lines(fh), restkey=EXTRA_KEY, restval=None)
        bad = 0
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error:
                bad += 1
                if bad > 1000:                   # the file is not a CSV
                    break
                continue
            yield row


def _jsonl(path: Path, encoding: str) -> Iterator[Dict[str, Any]]:
    with path.open(encoding=encoding, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _wrapped_document(path: Path, encoding: str) -> Iterator[Dict[str, Any]]:
    """{"results": [...]} and friends -- plenty of tools export this."""
    with path.open(encoding=encoding, errors="replace") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        return
    for value in doc.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            yield from value
            return
    yield doc                                  # a single-record document



def inspect(path: str) -> Dict[str, Any]:
    """What the UI shows before importing: shape, detected profile, mapping."""
    p = Path(path).expanduser()
    # Counts every row but holds one at a time: inspecting a 2GB export must
    # not need 2GB of memory.
    headers: List[str] = []
    mapping: Dict[str, str] = {}
    total = missing_cid = 0
    for row in read_rows(p):
        if not headers:
            headers = [h for h in row.keys() if h is not None and h != EXTRA_KEY]
            mapping = build_mapping(headers)
        total += 1
        if not extract_cid(row, mapping):
            missing_cid += 1
    return {
        "path": str(p),
        "rows": total,
        "columns": headers,
        "detected_profile": detect_profile(headers),
        "mapping": mapping,
        "unmapped_columns": [h for h in headers if h not in mapping.values()],
        "rows_without_identity": missing_cid,
        "warning": ("%d rows have no cid or Maps URL and will be skipped -- "
                    "identity cannot be invented" % missing_cid) if missing_cid else None,
    }


@source(id="csv", label="Import a file",
        description="Any scraper's CSV / JSON / JSONL export",
        legal_note="Uses data you already hold. Kerb fetches nothing.",
        takes="path")
def csv_source(q: SourceQuery) -> Iterator[Business]:
    if not q.path:
        raise ValueError("the csv source needs a path")
    p = Path(q.path).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)

    # Streamed, not materialised. The mapping only needs the first row's keys,
    # so peek at one and keep going -- holding a multi-million-row export in
    # memory to read its header is a needless way to run a machine out of RAM.
    stream = read_rows(p)
    try:
        first = next(stream)
    except StopIteration:
        return
    headers = [h for h in first.keys() if h is not None and h != EXTRA_KEY]
    mapping = build_mapping(headers, q.options.get("mapping"))
    profile = detect_profile(headers) or "custom"

    emitted = 0
    for row in itertools.chain([first], stream):
        cid = extract_cid(row, mapping)
        if not cid:
            continue
        def col(field):
            key = mapping.get(field)
            v = row.get(key) if key else None
            return None if v in ("", None) else v

        yield Business(
            cid=cid,
            name=str(col("name") or "").strip(),
            category=col("category"),
            address=col("address"),
            phone=col("phone"),
            website=col("website"),
            booking_url=col("booking_url"),
            rating=_num(col("rating")),
            review_count=_num(col("review_count"), int),
            lat=_num(col("lat")),
            lng=_num(col("lng")),
            status_raw=col("status_raw"),
            source="csv:%s" % profile,
            # str(k) so a ragged row's None key cannot poison the record.
            extras={str(k): v for k, v in row.items() if v not in ("", None)},
        )
        emitted += 1
        if q.limit and emitted >= q.limit:
            return



#!/usr/bin/env python3
"""Normalize a book-source package so Legado and Readori's validator see exactly the same sources.

* accepts a JSON array, a wrapper object (``sources`` / ``bookSources`` / ``list`` / ``data``) or a single
  source object, the same shapes ``validator.validate_source_packages.load_json_file`` accepts;
* strips a UTF-8 BOM, drops non-object entries;
* de-duplicates by ``bookSourceUrl`` (stripped), keeping the first record - the Readori validator groups by URL
  and Legado's database keys sources by URL, so duplicates would only skew the comparison;
* applies ``--offset`` / ``--limit`` here (once), so the Legado run and the validator run need no slicing flags.

Book-source JSON is untrusted data: it is only parsed and re-serialized, never executed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

WRAPPER_KEYS = ("sources", "bookSources", "list", "data")


def extract_sources(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in WRAPPER_KEYS:
            inner = data.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
        if data.get("bookSourceUrl") or data.get("bookSourceName"):
            return [data]
    return []


def load_sources(paths: list[Path]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        sources.extend(extract_sources(data))
    return sources


def prepare(
    sources: list[dict[str, Any]],
    *,
    offset: int = 0,
    limit: int = 0,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        url = str(source.get("bookSourceUrl") or "").strip()
        if not url:
            continue
        if dedupe:
            if url in seen:
                continue
            seen.add(url)
        selected.append(source)
    selected = selected[max(0, offset):]
    if limit and limit > 0:
        selected = selected[:limit]
    return selected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, action="append", required=True, help="source JSON; repeatable")
    parser.add_argument("--output", type=Path, required=True, help="normalized JSON array to write")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--no-dedupe", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = load_sources(args.input)
    except (OSError, ValueError) as error:
        print(f"[PREPARE-SOURCES] ERROR: {error}", file=sys.stderr)
        return 2
    prepared = prepare(sources, offset=args.offset, limit=args.limit, dedupe=not args.no_dedupe)
    if not prepared:
        print("[PREPARE-SOURCES] ERROR: no book sources found in the input", file=sys.stderr)
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"[PREPARE-SOURCES] read={len(sources)} written={len(prepared)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

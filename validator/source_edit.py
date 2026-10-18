"""R146 WP-B: book source editing, structured diff, undo / redo and export.

Pure standard library apart from the optional ``app_profiles`` import (used only to decode
rule objects that were stored as JSON strings).  No network, no file I/O.

Public API
----------
``SourceDocument``            patch / undo / redo / diff / changes for one source
``stamp_fixed_comment``       ``// Fixed: ...`` comment + ``lastUpdateTime``
``export_sources``            ordered, cleaned export (optionally only changed / only passed)
``dumps_sources``             ``json.dumps(..., ensure_ascii=False, indent=2)`` of the above
``build_fix_report``          ``readori.fix-report/1`` document

All timestamps use America/Los_Angeles through ``zoneinfo`` (DST is applied by the system
time zone database, never computed by hand).
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # package import (CLI / PyInstaller) or top-level import (scripts / tests)
    from . import app_profiles  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only outside the package
    import app_profiles  # type: ignore[no-redef]

LA_TIMEZONE_KEY = "America/Los_Angeles"
FIXED_PREFIX = "// Fixed: "
ERROR_PREFIX = "// Error: "
INTERNAL_KEY = "__readoriValidation"
REPORT_SCHEMA = "readori.fix-report/1"
TOOL_NAME = "readori-source-validator R146"
UNDO_LIMIT = 200
RULE_CONVERTED_NOTE = "字符串规则已转为对象"

_RULE_KEYS = tuple(app_profiles.RULE_SHORTHAND_FIELD.keys())
_MISSING = object()


class PatchConflict(Exception):
    """``apply(..., check_old=True)`` found a current value different from the patch's ``old``."""


# ---------------------------------------------------------------------------
# Time (America/Los_Angeles)
# ---------------------------------------------------------------------------

def la_zone() -> ZoneInfo | None:
    """America/Los_Angeles from the system tz database; ``None`` when the host has no tz data
    (Windows without the ``tzdata`` package).  Callers fall back to local time instead of guessing DST."""
    try:
        return ZoneInfo(LA_TIMEZONE_KEY)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _as_la(now: datetime | None) -> tuple[datetime, str]:
    """Return ``(datetime, label)``.  Naive ``now`` is taken as Los Angeles wall time."""
    zone = la_zone()
    if zone is None:
        base = now if now is not None else datetime.now(timezone.utc)
        local = base.astimezone()
        offset = local.strftime("%z")
        label = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local"
        return local, f"{label}(local)"
    if now is None:
        return datetime.now(zone), LA_TIMEZONE_KEY
    if now.tzinfo is None:
        return now.replace(tzinfo=zone), LA_TIMEZONE_KEY
    return now.astimezone(zone), LA_TIMEZONE_KEY


def format_la_time(now: datetime | None = None) -> str:
    """``YYYY-MM-DD HH:mm America/Los_Angeles`` (comment stamp format)."""
    local, label = _as_la(now)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {label}"


def la_iso(now: datetime | None = None) -> str:
    """ISO-8601 with the offset in force in Los Angeles at that instant (``-07:00`` / ``-08:00``)."""
    local, _ = _as_la(now)
    return local.isoformat(timespec="seconds")


def _epoch_ms(now: datetime | None) -> int:
    if now is None:
        return int(time.time() * 1000)
    if now.tzinfo is None:
        zone = la_zone()
        now = now.replace(tzinfo=zone) if zone is not None else now.astimezone()
    return int(now.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _split_path(path: str) -> list[str]:
    parts = [p for p in str(path or "").split(".")]
    if not parts or any(p == "" for p in parts):
        raise ValueError(f"invalid path: {path!r}")
    return parts


def _decode_rule_object(key: str, value: Any) -> dict[str, Any] | None:
    """Rule value as a dict when it is a string that the App decodes into an object, else ``None``."""
    if key not in _RULE_KEYS or not isinstance(value, str):
        return None
    decoded = app_profiles._decode_rule_value(key, value)
    return copy.deepcopy(decoded) if isinstance(decoded, dict) else None


def _child(container: Any, segment: str) -> Any:
    if isinstance(container, dict):
        return container.get(segment, _MISSING)
    if isinstance(container, list) and segment.lstrip("-").isdigit():
        index = int(segment)
        return container[index] if -len(container) <= index < len(container) else _MISSING
    return _MISSING


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path; a rule stored as a JSON string is read as its decoded object."""
    parts = _split_path(path)
    current: Any = obj
    for i, segment in enumerate(parts):
        if i == 1 and isinstance(current, str):
            decoded = _decode_rule_object(parts[0], current)
            if decoded is not None:
                current = decoded
        current = _child(current, segment)
        if current is _MISSING:
            return default
    return current


def _ensure_container(root: dict[str, Any], parts: list[str]) -> tuple[Any, bool]:
    """Walk to the parent container of ``parts``, creating dicts and converting a string rule to an
    object on the way.  Returns ``(parent, converted_rule)``."""
    converted = False
    current: Any = root
    for i, segment in enumerate(parts[:-1]):
        nxt = _child(current, segment)
        if i == 0 and isinstance(nxt, str):
            decoded = _decode_rule_object(segment, nxt)
            if decoded is not None:
                current[segment] = decoded
                nxt = decoded
                converted = True
        if nxt is _MISSING or nxt is None:
            if isinstance(current, dict):
                current[segment] = {}
                nxt = current[segment]
            else:
                raise KeyError(".".join(parts[: i + 1]))
        if not isinstance(nxt, (dict, list)):
            raise TypeError(f"cannot descend into {'.'.join(parts[: i + 1])!r}: not an object")
        current = nxt
    return current, converted


def _set_in(parent: Any, segment: str, value: Any) -> None:
    if isinstance(parent, dict):
        parent[segment] = value
    elif isinstance(parent, list) and segment.lstrip("-").isdigit():
        parent[int(segment)] = value
    else:
        raise TypeError(f"cannot set {segment!r} on {type(parent).__name__}")


def _delete_in(parent: Any, segment: str) -> None:
    if isinstance(parent, dict):
        parent.pop(segment, None)
    elif isinstance(parent, list) and segment.lstrip("-").isdigit():
        index = int(segment)
        if -len(parent) <= index < len(parent):
            del parent[index]


# ---------------------------------------------------------------------------
# Structured diff
# ---------------------------------------------------------------------------

def _leaf_equal(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                              and not isinstance(a, bool) and not isinstance(b, bool) and a == b)


def diff_values(old: Any, new: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Recursive dict comparison; lists and scalars are compared whole.  A missing side is ``None``."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[dict[str, Any]] = []
        for key in list(old.keys()) + [k for k in new.keys() if k not in old]:
            path = f"{prefix}.{key}" if prefix else str(key)
            o = old.get(key, _MISSING)
            n = new.get(key, _MISSING)
            if o is not _MISSING and n is not _MISSING:
                if key in _RULE_KEYS and not prefix and isinstance(o, str) and isinstance(n, dict):
                    decoded = _decode_rule_object(key, o)
                    if decoded is not None:
                        out.append({"path": path, "note": RULE_CONVERTED_NOTE})
                        o = decoded
                out.extend(diff_values(o, n, path))
            else:
                ov = None if o is _MISSING else o
                nv = None if n is _MISSING else n
                out.append({"path": path, "old": copy.deepcopy(ov), "new": copy.deepcopy(nv)})
        return out
    if _leaf_equal(old, new):
        return []
    return [{"path": prefix, "old": copy.deepcopy(old), "new": copy.deepcopy(new)}]


def _dump_sorted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# SourceDocument
# ---------------------------------------------------------------------------

class SourceDocument:
    """One book source being edited.

    ``original`` is never modified.  Every ``set`` / ``apply`` is one undo unit.  A new edit
    clears the redo stack.  ``changes()`` lists the edits currently in force.
    """

    def __init__(self, src: dict[str, Any]) -> None:
        self.original: dict[str, Any] = copy.deepcopy(src)
        self.working: dict[str, Any] = copy.deepcopy(src)
        self._undo: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []  # (working before, change records)
        self._redo: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []  # (working after, change records)
        self._changes: list[dict[str, Any]] = []
        # Validation snapshots, filled by the caller (validate_one results) for reports / only_passed.
        self.before_result: dict[str, Any] | None = None
        self.after_result: dict[str, Any] | None = None

    # -- reading ---------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        return copy.deepcopy(get_path(self.working, path, default))

    def get_original(self, path: str, default: Any = None) -> Any:
        return copy.deepcopy(get_path(self.original, path, default))

    # -- editing ---------------------------------------------------------
    def set(self, path: str, value: Any, *, reason: str = "", origin: str = "manual") -> list[dict[str, Any]]:
        """Set one path.  Returns the patch that was applied (empty when nothing changed)."""
        old = get_path(self.working, path, _MISSING)
        if old is not _MISSING and _leaf_equal(old, value):
            return []
        patch = [{"op": "set", "path": path, "old": None if old is _MISSING else copy.deepcopy(old),
                  "new": copy.deepcopy(value)}]
        self.apply(patch, reason=reason, origin=origin)
        return patch

    def delete(self, path: str, *, reason: str = "", origin: str = "manual") -> list[dict[str, Any]]:
        old = get_path(self.working, path, _MISSING)
        if old is _MISSING:
            return []
        patch = [{"op": "delete", "path": path, "old": copy.deepcopy(old), "new": None}]
        self.apply(patch, reason=reason, origin=origin)
        return patch

    def apply(self, patch: Iterable[dict[str, Any]], *, reason: str = "", origin: str = "manual",
              check_old: bool = False) -> None:
        """Apply a list of ``{"op": "set"|"delete", "path", "old", "new"}`` as ONE undo unit.

        Atomic: a failing op leaves the document untouched.  ``check_old=True`` raises
        ``PatchConflict`` when an op's ``old`` differs from the current value (stale suggestion).
        """
        ops = [dict(op) for op in patch]
        if not ops:
            return
        snapshot_before = copy.deepcopy(self.working)
        records: list[dict[str, Any]] = []
        try:
            for op in ops:
                kind = op.get("op", "set")
                path = str(op.get("path") or "")
                parts = _split_path(path)
                current = get_path(self.working, path, _MISSING)
                if check_old and "old" in op:
                    expected = op["old"]
                    actual = None if current is _MISSING else current
                    if not _leaf_equal(expected, actual):
                        raise PatchConflict(f"{path}: expected {expected!r}, found {actual!r}")
                parent, _converted = _ensure_container(self.working, parts)
                old_value = None if current is _MISSING else copy.deepcopy(current)
                if kind == "delete":
                    _delete_in(parent, parts[-1])
                    new_value = None
                elif kind == "set":
                    new_value = copy.deepcopy(op.get("new"))
                    _set_in(parent, parts[-1], new_value)
                else:
                    raise ValueError(f"unsupported patch op: {kind!r}")
                records.append({"path": path, "old": old_value, "new": new_value,
                                "reason": reason, "origin": origin})
        except Exception:
            self.working = snapshot_before
            raise
        self._undo.append((snapshot_before, records))
        if len(self._undo) > UNDO_LIMIT:
            del self._undo[0]
        self._redo.clear()
        self._changes.extend(records)

    def undo(self) -> bool:
        if not self._undo:
            return False
        before, records = self._undo.pop()
        self._redo.append((copy.deepcopy(self.working), records))
        self.working = before
        del self._changes[len(self._changes) - len(records):]
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        after, records = self._redo.pop()
        self._undo.append((copy.deepcopy(self.working), records))
        self.working = after
        self._changes.extend(records)
        return True

    def reset(self) -> None:
        """Back to the original source (clears undo / redo / changes)."""
        self.working = copy.deepcopy(self.original)
        self._undo.clear()
        self._redo.clear()
        self._changes.clear()

    # -- inspection ------------------------------------------------------
    def diff(self) -> list[dict[str, Any]]:
        """``[{"path","old","new"}]`` plus ``{"path","note"}`` where a string rule became an object."""
        return diff_values(self.original, self.working)

    def unified_diff(self) -> str:
        lines = difflib.unified_diff(
            _dump_sorted(self.original).splitlines(),
            _dump_sorted(self.working).splitlines(),
            fromfile="original", tofile="working", lineterm="",
        )
        return "\n".join(lines)

    def changes(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._changes)

    @property
    def changed(self) -> bool:
        return self.working != self.original

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def change_codes(self) -> list[str]:
        """Suggestion codes behind the current changes (``suggestion:F5`` -> ``F5``); ``manual`` for hand edits."""
        return _codes_from_changes(self._changes)

    def to_entry(self) -> dict[str, Any]:
        """Entry for ``build_fix_report`` / ``export_sources``."""
        return {
            "original": copy.deepcopy(self.original),
            "working": copy.deepcopy(self.working),
            "changes": self.changes(),
            "before": copy.deepcopy(self.before_result),
            "after": copy.deepcopy(self.after_result),
            "codes": self.change_codes(),
        }


def _codes_from_changes(changes: Iterable[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for change in changes:
        origin = str(change.get("origin") or "manual")
        code = origin.split(":", 1)[1] if origin.startswith("suggestion:") and ":" in origin else "manual"
        if code and code not in codes:
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# // Fixed: comment
# ---------------------------------------------------------------------------

def fixed_comment_block(change_codes: Iterable[str] = (), reasons: Iterable[str] = (),
                        now: datetime | None = None) -> str:
    codes = [str(c) for c in change_codes if str(c)] or ["manual"]
    texts: list[str] = []
    for reason in reasons:
        text = re.sub(r"\s+", " ", str(reason or "")).strip()
        if text and text not in texts:
            texts.append(text)
    block = f"{FIXED_PREFIX}{format_la_time(now)} {','.join(codes)}"
    if texts:
        block += ": " + "; ".join(texts)
    return block


def stamp_fixed_comment(src: dict[str, Any], change_codes: Iterable[str] = (), now: datetime | None = None, *,
                        reasons: Iterable[str] = (), passed: bool = False) -> dict[str, Any]:
    """Rewrite ``bookSourceComment`` and refresh ``lastUpdateTime`` (in place; returns ``src``).

    Blocks are separated by a blank line (``"\\n\\n"``).  Old ``// Fixed: `` blocks are dropped
    (never stacked); when the fixed source now passes (``passed=True``) the ``// Error: `` block
    left by the batch validator is dropped as well.  The new block goes first.
    """
    raw = src.get("bookSourceComment")
    existing = raw if isinstance(raw, str) else ""
    kept: list[str] = []
    for block in existing.split("\n\n") if existing else []:
        head = block.lstrip()
        if head.startswith(FIXED_PREFIX):
            continue
        if passed and head.startswith(ERROR_PREFIX):
            continue
        if not block.strip():
            continue
        kept.append(block)
    new_block = fixed_comment_block(change_codes, reasons, now)
    src["bookSourceComment"] = "\n\n".join([new_block] + kept)
    src["lastUpdateTime"] = _epoch_ms(now)
    return src


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _entry_of(item: Any) -> dict[str, Any]:
    if isinstance(item, SourceDocument):
        return item.to_entry()
    if isinstance(item, dict):
        return item
    raise TypeError(f"expected SourceDocument or entry dict, got {type(item).__name__}")


def _entry_changed(entry: dict[str, Any]) -> bool:
    working = entry.get("working")
    original = entry.get("original")
    if isinstance(original, dict) and isinstance(working, dict):
        return working != original
    return bool(entry.get("changes"))


def _entry_passed(entry: dict[str, Any]) -> bool:
    after = entry.get("after")
    if isinstance(after, dict):
        return bool(after.get("passed")) or after.get("verdict") in ("pass", "passed")
    return False


def _entry_reasons(entry: dict[str, Any]) -> list[str]:
    return [str(c.get("reason") or "") for c in entry.get("changes") or [] if isinstance(c, dict)]


def _entry_codes(entry: dict[str, Any]) -> list[str]:
    codes = entry.get("codes")
    if codes:
        return [str(c) for c in codes]
    return _codes_from_changes(c for c in entry.get("changes") or [] if isinstance(c, dict))


def export_sources(docs: Iterable[Any], *, only_changed: bool = False, only_passed: bool = False,
                   keep_source_url: bool = False, stamp: bool = False,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Cleaned working sources, in input order.

    * key order of the original is kept; new keys are appended;
    * ``__readoriValidation`` is removed; no validation tag is written (``customTag`` /
      ``bookSourceGroup`` stay exactly as edited);
    * ``keep_source_url`` restores the original ``bookSourceUrl`` (a domain-migration fix then
      does not create a new source in the App);
    * ``stamp`` adds the ``// Fixed:`` comment + ``lastUpdateTime`` to changed sources.

    ``docs`` items are ``SourceDocument`` objects or entry dicts (``working`` / ``original`` /
    ``changes`` / ``after`` / ``codes``).
    """
    out: list[dict[str, Any]] = []
    for item in docs:
        entry = _entry_of(item)
        changed = _entry_changed(entry)
        if only_changed and not changed:
            continue
        if only_passed and not _entry_passed(entry):
            continue
        source = copy.deepcopy(entry.get("working") or {})
        source.pop(INTERNAL_KEY, None)
        original = entry.get("original")
        if keep_source_url and isinstance(original, dict) and "bookSourceUrl" in original:
            source["bookSourceUrl"] = original["bookSourceUrl"]
        if stamp and changed:
            stamp_fixed_comment(source, _entry_codes(entry), now, reasons=_entry_reasons(entry),
                                passed=_entry_passed(entry))
        out.append(source)
    return out


def dumps_sources(sources: list[dict[str, Any]]) -> str:
    return json.dumps(sources, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# fix-report
# ---------------------------------------------------------------------------

def _result_summary(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "verdict": result.get("verdict"),
        "failureReason": result.get("failureReason"),
        "stages": copy.deepcopy(result.get("stages")),
        "responseTimeMs": result.get("responseTimeMs"),
    }


def build_fix_report(entries: Iterable[Any], *, app_version: str = "1.1.1",
                     now: datetime | None = None, keep_source_url: bool = False) -> dict[str, Any]:
    """``readori.fix-report/1``.  ``bookSourceUrl`` is the new (working) value and
    ``originalBookSourceUrl`` the old one, so a domain migration can be mapped back."""
    sources: list[dict[str, Any]] = []
    for item in entries:
        entry = _entry_of(item)
        working = entry.get("working") if isinstance(entry.get("working"), dict) else {}
        original = entry.get("original") if isinstance(entry.get("original"), dict) else working
        new_url = working.get("bookSourceUrl", "")
        old_url = original.get("bookSourceUrl", new_url)
        if keep_source_url:
            new_url = old_url
        changes = [
            {
                "path": c.get("path"), "old": c.get("old"), "new": c.get("new"),
                "reason": c.get("reason", ""), "origin": c.get("origin", "manual"),
            }
            for c in entry.get("changes") or [] if isinstance(c, dict)
        ]
        comment = entry.get("comment")
        if comment is None:
            comment = fixed_comment_block(_entry_codes(entry), _entry_reasons(entry), now) if changes else ""
        sources.append({
            "bookSourceUrl": new_url,
            "originalBookSourceUrl": old_url,
            "bookSourceName": working.get("bookSourceName", original.get("bookSourceName", "")),
            "changes": changes,
            "before": _result_summary(entry.get("before")),
            "after": _result_summary(entry.get("after")),
            "comment": comment,
        })
    return {
        "schema": REPORT_SCHEMA,
        "generatedAt": la_iso(now),
        "appVersion": app_version,
        "tool": TOOL_NAME,
        "sources": sources,
    }

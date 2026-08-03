#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["chardet>=5,<6", "binaryornot"]
# ///
"""
encoding_guard.py - Preserve file encoding and line endings when Claude Code edits files.

Strategy:
    1. PreToolUse (Read):  detect encoding + line endings, convert to UTF-8 so Claude reads correctly
    2. PreToolUse (Edit):  file already UTF-8 (cache exists), skip
    3. PostToolUse (Edit): restore original encoding + line endings from session cache
    4. Stop / SessionEnd / SessionStart(resume) (restore-all):
       restore any remaining files — turn cleanup, exit backstop, and
       crash-residue healing before the resumed session acts

Recovery commands (user-invoked CLI, not hooks):
    scan / scan-current           read-only report of cache records
    restore-current               restore orphaned files under the cwd
    prune-current                 delete defunct records under the cwd

Cache layout:
    <tempdir>/.cc_encoding_cache/<session_id>/<sha256(normalized_path)[:16]>.json

Each session gets its own cache directory. Hooks operate strictly on their
own session's records; only the user-invoked recovery commands may act on
other sessions' records. Stale EMPTY session dirs (>24h) are removed on
startup — record-bearing dirs are never auto-deleted.
Stdin is read as binary (sys.stdin.buffer) to avoid Windows codepage issues.
"""

# Read stdin immediately before any heavy imports (Windows stdin reliability).
# Hook modes only: CC feeds hooks a pipe that closes. In the recovery CLI
# modes stdin may be an interactive terminal, where a blind read() would
# block forever.
import sys
_raw_stdin = b""
if len(sys.argv) > 1 and sys.argv[1] in ("pre", "post", "restore-all"):
    _raw_stdin = sys.stdin.buffer.read()

import contextlib
import json
import os
import hashlib
import tempfile
import time

CACHE_ROOT = os.path.join(tempfile.gettempdir(), ".cc_encoding_cache")
STALE_HOURS = 24

RESTORE_ENCODINGS = {
    "gb2312", "gbk", "gb18030",
    "big5", "big5hkscs",
    "euc-tw",
    "shift_jis", "euc-jp", "iso-2022-jp",
    "euc-kr",
    "windows-1252", "iso-8859-1",
    "windows-1251",
}

ENCODING_ALIASES = {
    "gb2312": "gbk",
    "iso-8859-1": "windows-1252",
}

# Encodings whose chardet detection (>=0.5 confidence + name-in-RESTORE) is
# reliable enough to skip the binaryornot pre-check. binaryornot's decision
# tree false-positives mixed-content files in two patterns:
#   - short CJK files (<~500B Shift_JIS/EUC-JP/EUC-KR/Big5/GB18030)
#   - mixed Cyrillic + ASCII files (e.g., source code with CP1251 comments)
# For these encodings chardet's structural validators are strict enough on
# their own; real binaries cannot reach the confidence threshold.
STRUCTURAL_TRUSTED = {
    "gbk", "gb18030",
    "big5", "big5hkscs",
    "shiftjis", "eucjp", "euckr", "iso2022jp",
    "windows1251",
}


def _strip_enc(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "")


def _log(msg: str):
    sys.stderr.write(f"encoding_guard: {msg}\n")


def normalize_encoding(enc: str) -> str:
    """Map chardet output to a Python codec name.

    Apply ENCODING_ALIASES safety mappings (gb2312 -> gbk, iso-8859-1 ->
    windows-1252) when applicable; otherwise preserve chardet's original
    name verbatim. Returning the stripped form (e.g. "windows1251" without
    its dash) breaks codec lookup since Python's codec registry recognizes
    "windows-1251" / "cp1251" but NOT "windows1251".
    """
    stripped = _strip_enc(enc)
    for orig, alias in ENCODING_ALIASES.items():
        if stripped == _strip_enc(orig):
            return alias
    return enc


def normalize_path(path: str) -> str:
    """Normalize path for consistent cache keys across pre/post and platforms."""
    return os.path.normcase(os.path.normpath(path))


def file_hash(path: str) -> str:
    return hashlib.sha256(normalize_path(path).encode()).hexdigest()[:16]


def sanitize_session_id(session_id: str) -> str:
    """Prevent path traversal from untrusted session_id."""
    return session_id.replace(os.sep, "_").replace("/", "_").replace("\\", "_")


def session_dir(session_id: str) -> str:
    return os.path.join(CACHE_ROOT, sanitize_session_id(session_id))


def cache_path(session_id: str, path: str) -> str:
    return os.path.join(session_dir(session_id), file_hash(path) + ".json")


def cleanup_stale_sessions(current_session_id: str):
    """Remove EMPTY stale session dirs only. Never touches another session's records.

    An orphaned record is the only copy of "this file on disk should be
    re-encoded back" — deleting it is irreversible information loss, and
    restoring it automatically can override a user who has since accepted
    the UTF-8 state. Cross-session records are therefore acted on only by
    the user, via the scan/restore-current/prune-current commands.
    """
    if not os.path.exists(CACHE_ROOT):
        return
    current_dir_name = sanitize_session_id(current_session_id)
    now = time.time()
    try:
        for name in os.listdir(CACHE_ROOT):
            if name == current_dir_name:
                continue
            sid_dir = os.path.join(CACHE_ROOT, name)
            if os.path.isdir(sid_dir):
                age = now - os.path.getmtime(sid_dir)
                if age > STALE_HOURS * 3600 and not os.listdir(sid_dir):
                    with contextlib.suppress(OSError):
                        os.rmdir(sid_dir)
    except OSError:
        pass


def detect_encoding(path: str) -> tuple[str, float]:
    try:
        import chardet
    except ImportError:
        try:
            from charset_normalizer import from_path
            result = from_path(path).best()
            if result is None:
                return ("utf-8", 0.0)
            return (str(result.encoding), 1.0)
        except ImportError:
            return ("utf-8", 0.0)

    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        return ("utf-8", 1.0)

    det = chardet.detect(raw)
    return (det.get("encoding") or "utf-8", det.get("confidence") or 0.0)


def detect_line_ending(raw: bytes) -> str:
    """Detect dominant line ending style from raw bytes."""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return "crlf" if crlf > lf else "lf"


def normalize_line_endings(data: bytes, target: str) -> bytes:
    """Normalize all line endings to target style."""
    # First unify to LF
    unified = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if target == "crlf":
        return unified.replace(b"\n", b"\r\n")
    return unified


def atomic_write(path: str, data: bytes):
    """Write data to file atomically via temp file + replace."""
    dir_path = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".encoding_guard_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_cache(session_id: str, path: str, encoding: str,
               confidence: float, line_ending: str):
    sd = session_dir(session_id)
    os.makedirs(sd, exist_ok=True)
    with open(cache_path(session_id, path), "w") as f:
        json.dump({
            "path": path,
            "encoding": encoding,
            "confidence": confidence,
            "line_ending": line_ending,
            "timestamp": time.time(),
        }, f)


def load_cache(session_id: str, path: str) -> dict | None:
    cp = cache_path(session_id, path)
    if not os.path.exists(cp):
        return None
    try:
        with open(cp, "r") as f:
            data = json.load(f)
        # Validate required fields and types
        if (not isinstance(data, dict)
                or not isinstance(data.get("encoding"), str)
                or data.get("line_ending") not in ("lf", "crlf")
                or _strip_enc(data["encoding"]) not in _RESTORE_SET):
            _log(f"corrupt cache for {path}, removing")
            with contextlib.suppress(OSError):
                os.unlink(cp)
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        _log(f"failed to read cache for {path}: {e}")
        with contextlib.suppress(OSError):
            os.unlink(cp)
        return None


def delete_cache(session_id: str, path: str) -> bool:
    """Delete cache file. Returns True if successfully deleted or already absent."""
    cp = cache_path(session_id, path)
    try:
        os.unlink(cp)
    except FileNotFoundError:
        pass  # already gone, that's fine
    except OSError as e:
        _log(f"failed to delete cache {cp}: {e}")
        return False
    # Clean up empty session dir
    sd = session_dir(session_id)
    try:
        if os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)
    except OSError:
        pass
    return True


def convert_file(path: str, from_enc: str, to_enc: str,
                 target_eol: str | None = None) -> bool:
    """Convert file encoding (and optionally line endings) in one atomic write."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        content = raw.decode(from_enc)
        encoded = content.encode(to_enc)
        if target_eol:
            encoded = normalize_line_endings(encoded, target_eol)
        atomic_write(path, encoded)
        return True
    except (UnicodeDecodeError, UnicodeEncodeError, LookupError) as e:
        _log(f"convert {from_enc}->{to_enc} failed for {path}: {e}")
        return False
    except OSError as e:
        _log(f"file I/O failed for {path}: {e}")
        return False


def extract_file_path(hook_json: dict) -> str | None:
    ti = hook_json.get("tool_input", {})
    return ti.get("file_path")


_RESTORE_SET = {_strip_enc(e) for e in RESTORE_ENCODINGS}


def handle_pre(hook_json: dict):
    session_id = hook_json.get("session_id", "unknown")
    path = extract_file_path(hook_json)
    if not path or not os.path.isfile(path):
        return

    # Cache exists from a previous Read in this session?
    cached = load_cache(session_id, path)
    if cached:
        # Verify file is still UTF-8 (cache might be stale if delete_cache failed)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            # Can't read file (locked, permissions) — keep cache, don't guess
            _log(f"cannot read {path} for cache validation: {e}")
            return
        try:
            raw.decode("utf-8")
            return  # file is valid UTF-8 as expected, skip
        except UnicodeDecodeError:
            pass
        # File is not valid UTF-8 — stale cache from failed delete
        _log(f"stale cache for {path} (file is not UTF-8), removing")
        delete_cache(session_id, path)
        # Fall through to re-convert

    encoding, confidence = detect_encoding(path)
    norm = normalize_encoding(encoding)

    if _strip_enc(norm) not in _RESTORE_SET:
        return

    if confidence < 0.5:
        _log(f"skipping {path} — confidence too low ({confidence:.2f}) for {norm}")
        return

    # Trust chardet for STRUCTURAL_TRUSTED encodings (CJK + Cyrillic) where
    # the confidence + name gate above is already enforced. binaryornot
    # false-positives short J/K files and mixed Cyrillic+ASCII files; for
    # these encodings chardet's structural validators are strict enough on
    # their own. Other single-byte encodings (Windows-1252, ISO-8859-1) still
    # go through binaryornot since they cannot structurally rule out binary.
    if _strip_enc(norm) not in STRUCTURAL_TRUSTED:
        try:
            from binaryornot.check import is_binary
            if is_binary(path):
                return
        except Exception as e:
            _log(f"binary check failed for {path}: {e}")
            return

    with open(path, "rb") as f:
        raw = f.read()
    line_ending = detect_line_ending(raw)

    if convert_file(path, norm, "utf-8"):
        save_cache(session_id, path, norm, confidence, line_ending)
        _log(f"[{session_id[:8]}] converted {path} {norm}->utf-8 eol={line_ending} "
             f"(confidence={confidence:.2f})")


def handle_post(hook_json: dict | None):
    if not hook_json:
        return
    session_id = hook_json.get("session_id", "unknown")
    path = extract_file_path(hook_json)
    if not path or not os.path.isfile(path):
        return

    cached = load_cache(session_id, path)
    if not cached:
        return

    orig_enc = cached["encoding"]
    orig_eol = cached.get("line_ending", "lf")

    if convert_file(path, "utf-8", orig_enc, target_eol=orig_eol):
        if delete_cache(session_id, path):
            _log(f"[{session_id[:8]}] restored {path} utf-8->{orig_enc} eol={orig_eol}")
        else:
            _log(f"[{session_id[:8]}] restored {path} but cache delete failed — "
                 f"stale cache will be cleaned on next pre")
    else:
        _log(f"[{session_id[:8]}] restore FAILED for {path}, "
             f"file remains UTF-8, cache preserved for retry")


def handle_restore_all(hook_json: dict | None):
    """Restore every file still in the session cache (e.g. Read-only without Edit)."""
    if not hook_json:
        return
    session_id = hook_json.get("session_id", "unknown")
    sd = session_dir(session_id)
    if not os.path.isdir(sd):
        return
    try:
        entries = os.listdir(sd)
    except OSError as e:
        _log(f"cannot list session dir {sd}: {e}")
        return
    for name in entries:
        if not name.endswith(".json"):
            continue
        cp = os.path.join(sd, name)
        # Read just enough to recover the path; load_cache does the full validation.
        try:
            with open(cp, "r") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _log(f"failed to read {cp}: {e}")
            with contextlib.suppress(OSError):
                os.unlink(cp)
            continue
        path = raw.get("path") if isinstance(raw, dict) else None
        if not isinstance(path, str) or not path:
            with contextlib.suppress(OSError):
                os.unlink(cp)
            continue
        cached = load_cache(session_id, path)
        if cached is None:
            # load_cache already removed the corrupt entry
            continue
        if not os.path.isfile(path):
            delete_cache(session_id, path)
            continue
        orig_enc = cached["encoding"]
        orig_eol = cached["line_ending"]
        if convert_file(path, "utf-8", orig_enc, target_eol=orig_eol):
            delete_cache(session_id, path)
            _log(f"[{session_id[:8]}] stop-restored {path} utf-8->{orig_enc} eol={orig_eol}")
        else:
            _log(f"[{session_id[:8]}] stop-restore FAILED for {path}, cache preserved")
    # Remove session dir if every entry was cleaned up via os.unlink
    # (delete_cache paths already handle this, but orphan paths above don't).
    with contextlib.suppress(OSError):
        if os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)


# ---------------------------------------------------------------------------
# Recovery commands: scan / scan-current / restore-current / prune-current
#
# User-invoked CLI modes, not hooks. These are the only code paths allowed
# to look at OTHER sessions' cache records; hooks operate strictly on their
# own session_id. Discovery is record-driven: only CACHE_ROOT/*/​*.json is
# read — user project files are never crawled.
# ---------------------------------------------------------------------------

RECENT_SECONDS = 3600  # session dirs younger than this may belong to a live session


def classify_record(record_file: str) -> tuple[str, dict | None, str]:
    """Classify one cache record by on-disk facts.

    "moot"       — provably worthless: unreadable/invalid record, target
                   file missing, or file no longer valid UTF-8 (already
                   restored, reverted, or rewritten). Safe to delete.
    "actionable" — target file exists and is valid UTF-8: a restore is
                   possible. No encode-back test here — a failing
                   re-encode is discovered and reported by restore
                   itself (convert_file keeps the record on failure).
    """
    try:
        with open(record_file, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return ("moot", None, f"unreadable record: {e}")
    if (not isinstance(data, dict)
            or not isinstance(data.get("path"), str)
            or not data["path"]
            or not isinstance(data.get("encoding"), str)
            or data.get("line_ending") not in ("lf", "crlf")
            or _strip_enc(data["encoding"]) not in _RESTORE_SET):
        return ("moot", data if isinstance(data, dict) else None, "invalid record fields")
    path = data["path"]
    if not os.path.isfile(path):
        return ("moot", data, "target file no longer exists")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        # Cannot prove anything about a file we cannot read — keep the record.
        return ("actionable", data, f"file unreadable right now ({e}); record kept")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return ("moot", data, "file is no longer UTF-8 (already restored or rewritten)")
    return ("actionable", data, "file is UTF-8; original encoding on record")


def _own_session_dir_name() -> str | None:
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return sanitize_session_id(sid) if sid else None


def _path_under(child: str, parent: str) -> bool:
    child_n = normalize_path(os.path.abspath(child))
    parent_n = normalize_path(os.path.abspath(parent))
    return child_n == parent_n or child_n.startswith(parent_n + os.sep)


def collect_records(scope_cwd: str | None) -> list[dict]:
    """Classify every record under CACHE_ROOT.

    scope_cwd filters to records whose target path lies under that
    directory. Records without a readable path can never match a scope
    filter — they surface only in the global scan.
    """
    rows = []
    if not os.path.isdir(CACHE_ROOT):
        return rows
    now = time.time()
    for name in sorted(os.listdir(CACHE_ROOT)):
        sid_dir = os.path.join(CACHE_ROOT, name)
        if not os.path.isdir(sid_dir):
            continue
        try:
            age_s = int(now - os.path.getmtime(sid_dir))
        except OSError:
            age_s = None
        try:
            entries = sorted(os.listdir(sid_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".json"):
                continue
            record_file = os.path.join(sid_dir, entry)
            category, record, detail = classify_record(record_file)
            path = record.get("path") if isinstance(record, dict) else None
            if scope_cwd is not None:
                if not (isinstance(path, str) and path and _path_under(path, scope_cwd)):
                    continue
            try:
                record_age_s = int(now - os.path.getmtime(record_file))
            except OSError:
                record_age_s = None
            rows.append({
                "session": name,
                "session_age_seconds": age_s,
                "record_age_seconds": record_age_s,
                "record_file": record_file,
                "category": category,
                "path": path if isinstance(path, str) else None,
                "encoding": record.get("encoding") if isinstance(record, dict) else None,
                "line_ending": record.get("line_ending") if isinstance(record, dict) else None,
                "detail": detail,
            })
    return rows


def _guard_reason(row: dict, include_all: bool) -> str | None:
    """Why this record must not be modified right now, or None.

    Liveness is judged per RECORD (its file mtime), not per session dir:
    a live session's in-flight record was written seconds ago, while an
    orphan record is old by definition. Directory mtime would self-lock —
    our own restore/prune bump the dir mtime by unlinking siblings.
    """
    own = _own_session_dir_name()
    if own is not None and row["session"] == own:
        return "own live session (CLAUDE_CODE_SESSION_ID)"
    age_s = row["record_age_seconds"]
    if not include_all and age_s is not None and age_s < RECENT_SECONDS:
        return (f"record written {age_s // 60}m ago — possibly a live "
                f"session; pass --all to override")
    return None


def _age_str(age_s) -> str:
    if age_s is None:
        return "age unknown"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    if age_s < 86400:
        return f"{age_s // 3600}h ago"
    return f"{age_s // 86400}d ago"


def _cleanup_empty_dirs(sessions: set[str]) -> None:
    for name in sessions:
        sid_dir = os.path.join(CACHE_ROOT, name)
        with contextlib.suppress(OSError):
            if os.path.isdir(sid_dir) and not os.listdir(sid_dir):
                os.rmdir(sid_dir)


def handle_scan(scope_cwd: str | None, as_json: bool) -> int:
    """Read-only report. Never modifies records or files."""
    rows = collect_records(scope_cwd)
    actionable = [r for r in rows if r["category"] == "actionable"]
    moot = [r for r in rows if r["category"] == "moot"]
    if as_json:
        print(json.dumps({
            "cache_root": CACHE_ROOT,
            "scope": scope_cwd,
            "own_session": _own_session_dir_name(),
            "summary": {"actionable": len(actionable), "moot": len(moot)},
            "records": rows,
        }, ensure_ascii=False, indent=2))
        return 0
    where = f" for files under {scope_cwd}" if scope_cwd else " (all sessions)"
    if not rows:
        print(f"encoding-guard: no cache records found{where}. Clean.")
        return 0
    print(f"encoding-guard: {len(actionable)} restorable, {len(moot)} defunct record(s){where}\n")
    by_session: dict[str, list[dict]] = {}
    for r in rows:
        by_session.setdefault(r["session"], []).append(r)
    own = _own_session_dir_name()
    for name, group in by_session.items():
        tags = [_age_str(group[0]["session_age_seconds"])]
        fresh_records = [r for r in group
                         if r["record_age_seconds"] is not None
                         and r["record_age_seconds"] < RECENT_SECONDS]
        if own is not None and name == own:
            tags.append("THIS session")
        elif fresh_records:
            tags.append("possibly live")
        print(f"session {name} ({', '.join(tags)}):")
        for r in group:
            mark = "RESTORABLE" if r["category"] == "actionable" else "defunct"
            target = r["path"] or r["record_file"]
            enc = f" [{r['encoding']}]" if r["encoding"] else ""
            print(f"  {mark:>10}  {target}{enc} — {r['detail']}")
        print()
    if scope_cwd is not None:
        if actionable:
            print("To restore these files to their original encoding, run this "
                  "command again with mode 'restore-current'.")
        if moot:
            print("To remove defunct records, run mode 'prune-current'.")
    else:
        print("Note: restore/prune operate per-project — cd into the project "
              "and use restore-current / prune-current.")
    return 0


def _match_paths_filter(row: dict, paths_filter: list[str] | None) -> bool:
    if not paths_filter:
        return True
    if not row["path"]:
        return False
    target = normalize_path(os.path.abspath(row["path"]))
    return any(normalize_path(os.path.abspath(p)) == target for p in paths_filter)


def handle_restore_current(paths_filter: list[str] | None, include_all: bool) -> int:
    """Restore actionable records whose target lies under the current directory."""
    cwd = os.getcwd()
    rows = collect_records(cwd)
    restored = failed = skipped = 0
    touched_sessions: set[str] = set()
    for row in rows:
        if row["category"] != "actionable" or not _match_paths_filter(row, paths_filter):
            continue
        reason = _guard_reason(row, include_all)
        if reason is not None:
            print(f"SKIP     {row['path']} — {reason}")
            skipped += 1
            continue
        enc = row["encoding"]
        eol = row["line_ending"] or "lf"
        if convert_file(row["path"], "utf-8", enc, target_eol=eol):
            with contextlib.suppress(OSError):
                os.unlink(row["record_file"])
            touched_sessions.add(row["session"])
            print(f"RESTORED {row['path']} utf-8->{enc} eol={eol}")
            restored += 1
        else:
            print(f"FAILED   {row['path']} — could not re-encode to {enc}; "
                  f"record kept (file may have been edited as UTF-8)")
            failed += 1
    _cleanup_empty_dirs(touched_sessions)
    print(f"\nrestore-current: {restored} restored, {failed} failed, {skipped} skipped")
    return 0 if failed == 0 else 1


def handle_prune_current(paths_filter: list[str] | None, include_all: bool,
                         discard_actionable: bool) -> int:
    """Delete defunct records under the current directory.

    Actionable records are deleted only with --discard (the user accepts
    the current UTF-8 state and wants the reminder gone).
    """
    cwd = os.getcwd()
    rows = collect_records(cwd)
    pruned = skipped = 0
    touched_sessions: set[str] = set()
    for row in rows:
        if not _match_paths_filter(row, paths_filter):
            continue
        if row["category"] == "actionable" and not discard_actionable:
            continue
        reason = _guard_reason(row, include_all)
        if reason is not None:
            print(f"SKIP   {row['path'] or row['record_file']} — {reason}")
            skipped += 1
            continue
        with contextlib.suppress(OSError):
            os.unlink(row["record_file"])
        touched_sessions.add(row["session"])
        label = "discarded" if row["category"] == "actionable" else "pruned"
        print(f"{label.upper():>9}  {row['path'] or row['record_file']} — {row['detail']}")
        pruned += 1
    _cleanup_empty_dirs(touched_sessions)
    print(f"\nprune-current: {pruned} removed, {skipped} skipped")
    return 0


def run_recovery_cli(mode: str, argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog=f"encoding_guard.py {mode}")
    if mode in ("scan", "scan-current"):
        ap.add_argument("--json", action="store_true",
                        help="machine-readable output")
    else:
        ap.add_argument("--path", action="append", default=[],
                        help="only this file (repeatable)")
        ap.add_argument("--all", action="store_true",
                        help="include recently-active session dirs")
        if mode == "prune-current":
            ap.add_argument("--discard", action="store_true",
                            help="also delete restorable records (accept current UTF-8 state)")
    args = ap.parse_args(argv)
    if mode == "scan":
        return handle_scan(None, args.json)
    if mode == "scan-current":
        return handle_scan(os.getcwd(), args.json)
    if mode == "restore-current":
        return handle_restore_current(args.path or None, args.all)
    return handle_prune_current(args.path or None, args.all, args.discard)


HOOK_MODES = ("pre", "post", "restore-all")
RECOVERY_MODES = ("scan", "scan-current", "restore-current", "prune-current")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in HOOK_MODES + RECOVERY_MODES:
        _log("Usage: encoding_guard.py "
             "<pre|post|restore-all|scan|scan-current|restore-current|prune-current>")
        sys.exit(0)  # never block Claude Code

    mode = sys.argv[1]

    if mode in RECOVERY_MODES:
        # User-invoked CLI, not a hook: normal exit codes, output on stdout.
        sys.exit(run_recovery_cli(mode, sys.argv[2:]))

    hook_json = None
    try:
        text = _raw_stdin.decode("utf-8")
        if text.strip():
            hook_json = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _log(f"stdin parse failed ({mode}): {e}")
    except Exception as e:
        _log(f"unexpected stdin error ({mode}): {e}")

    try:
        if mode == "pre":
            cleanup_stale_sessions(
                hook_json.get("session_id", "") if hook_json else ""
            )
            if hook_json is None:
                sys.exit(0)
            handle_pre(hook_json)
        elif mode == "post":
            handle_post(hook_json)
        else:
            handle_restore_all(hook_json)
    except Exception as e:
        _log(f"unhandled error ({mode}): {e}")
        # Never block Claude Code
        sys.exit(0)


if __name__ == "__main__":
    main()

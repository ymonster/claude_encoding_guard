#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["chardet>=7,<8"]
# ///
"""
encoding_guard.py (chardet 7.x experimental) - Preserve file encoding and
line endings when Claude Code edits files.

This is the chardet7-preview branch. Behavioral parity with main was verified
via _backup/pr_test_harness.py (88/89 fixtures PASS — 73 original + 16
Central European; the one miss is a 50-byte cp1250 fixture conservatively
skipped). Differences from main:

    * chardet pinned to >=7,<8 (vs >=5,<6 on main)
    * binaryornot dependency dropped — chardet 7.x has built-in binary
      detection at pipeline stage 5 (encoding=None for non-text)
    * detect_safely() trusts the stage 5 binary cut + RESTORE_ENCODINGS
      allowlist + a 0.10 noise floor; no 0.30/0.60/gap thresholds because
      7.x confidence is cosine-similarity based (geometric, not Bayesian)
      and GB18030 confidence is structurally pinned to 0.14-0.25
    * ASCII short-circuit added — 7.x reports pure-ASCII input as
      Windows-1252(1.00); we skip these explicitly
    * STRUCTURAL_TRUSTED bypass removed — stage 5 obviates it
    * RESTORE_ENCODINGS extended with cp932 / CP949 / GB18030 (7.x's
      default names for 5.x's shift_jis / euc-kr / gbk-gb2312)

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
    prune-current                 delete obsolete records under the cwd

Each session gets its own cache directory. Hooks operate strictly on their
own session's records; only the user-invoked recovery commands may act on
other sessions' records. Stale EMPTY session dirs (>24h) are removed on
startup — record-bearing dirs are never auto-deleted.
Stdin is read as binary (sys.stdin.buffer) to avoid Windows codepage issues.

See TECHNICAL.md "chardet 7.x Migration Findings" for the empirical
rationale and corner-case catalog.
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
LOG_FILE = os.path.join(CACHE_ROOT, "encoding_guard.log")
LOG_MAX_BYTES = 512 * 1024

# 7.x-aware allowlist. Includes both 7.x-default names (cp932, CP949, GB18030)
# and 5.x-style names (shift_jis, euc-kr, gbk, gb2312) defensively.
RESTORE_ENCODINGS = {
    # CJK
    "GB18030", "gb18030", "gbk", "gb2312",
    "Big5", "big5", "Big5-HKSCS", "big5hkscs", "EUC-TW", "euc-tw",
    "cp932", "Shift_JIS", "shift_jis",
    "EUC-JP", "euc-jp", "ISO-2022-JP", "iso-2022-jp",
    "CP949", "cp949", "EUC-KR", "euc-kr",
    # Single-byte Latin
    "Windows-1252", "windows-1252", "ISO-8859-1", "iso-8859-1",
    # Cyrillic
    "windows-1251", "Windows-1251",
    # Central European
    "cp1250", "windows-1250", "Windows-1250",
}

ENCODING_ALIASES = {
    "gb2312": "gbk",
    "iso-8859-1": "windows-1252",
}


def _strip_enc(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "")


def _log(msg: str):
    sys.stderr.write(f"encoding_guard: {msg}\n")


def _flog(event: str):
    """Append one line to the forensic log (CACHE_ROOT/encoding_guard.log).

    Hook stderr is captured into transcripts, but operations that run in
    process-shutdown windows (e.g. SessionEnd restore-all after the
    terminal is closed) never reach a transcript — this file is the
    durable trail for every file mutation. Best-effort: never raises.
    """
    try:
        os.makedirs(CACHE_ROOT, exist_ok=True)
        try:
            if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE + ".1")
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {event}\n")
    except OSError:
        pass


def normalize_encoding(enc: str) -> str:
    stripped = _strip_enc(enc)
    for orig, alias in ENCODING_ALIASES.items():
        if stripped == _strip_enc(orig):
            return alias
    return enc


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def file_hash(path: str) -> str:
    return hashlib.sha256(normalize_path(path).encode()).hexdigest()[:16]


def sanitize_session_id(session_id: str) -> str:
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


_RESTORE_SET = {_strip_enc(e) for e in RESTORE_ENCODINGS}


def detect_safely(data: bytes) -> tuple[str | None, str]:
    """7.x detection (v2: 方向 3+4 混合).

    Strategy — trust chardet 7.x's own filtering layers:
      1. Stage 5 binary detection: encoding=None means binary (verified hard:
         even with ignore_threshold=True, real binaries return None at
         confidence 0.95-1.00). This is our absolute binary cut.
      2. encoding_era=MODERN_WEB | LEGACY_ISO keeps web-relevant encodings
         plus the ISO-8859 family (no LEGACY_MAC / DOS / MAINFRAME noise).
         LEGACY_ISO must be present: without it a genuine ISO-8859-2 file
         top-detects as its near-twin cp1250 and gets wrongly converted.
      3. prefer_superset=False so legacy ISO encodings win under their own
         names and fall out at the RESTORE_ENCODINGS allowlist. With True,
         chardet folds iso8859-2 -> cp1250 and iso8859-5 -> windows-1251
         (lossy: the layouts differ), reintroducing the misconversion.
      4. RESTORE_ENCODINGS allowlist filter on top result.
      5. Floor at conf >= 0.10 to drop noise candidates (cp1006/IBM855-class).

    No 0.30/0.60/gap thresholds: GB18030's bigram inventory is the largest
    in 7.x's model so its cosine similarity confidence is geometrically
    pinned to 0.14-0.25 regardless of input length. Demanding higher
    confidence excludes valid GB18030 of any size. Binary safety still
    holds because stage 5 returns encoding=None unconditionally for
    non-textual bytes.
    """
    if not data:
        return (None, "empty file")

    # All-ASCII short-circuit. chardet 7.x reports any pure-ASCII content as
    # Windows-1252(1.00) (since ASCII is a cp1252 subset and the bigram model
    # has no positive ASCII signal). Converting these as cp1252 is a no-op
    # round-trip but still mutates the file and writes a cache entry — skip.
    if all(b < 0x80 for b in data):
        return (None, "all-ASCII (already utf-8 compatible)")

    try:
        import chardet
        from chardet import EncodingEra
    except ImportError as e:
        return (None, f"chardet 7.x import failed: {e}")

    # detect() === run_pipeline()[0] + prefer_superset/compat_names processing.
    # detect_all() adds an `ignore_threshold` knob and returns the full list,
    # but we don't read results[1:] for any cross-candidate logic, so detect()
    # is the precise tool: same shaping params, no wasted to_dict / no
    # threshold filtering (matches our prior ignore_threshold=True).
    try:
        top = chardet.detect(
            data,
            encoding_era=EncodingEra.MODERN_WEB | EncodingEra.LEGACY_ISO,
            prefer_superset=False,
        )
    except Exception as e:
        return (None, f"detect raised: {e}")

    top_enc = top.get("encoding")
    top_conf = top.get("confidence", 0.0)

    # Hard binary cut (stage 5)
    if top_enc is None:
        return (None, f"binary (stage 5 conf={top_conf:.2f})")

    # Already UTF-8 → no work to do
    if _strip_enc(top_enc) == "utf8":
        return (None, f"already utf-8 (conf={top_conf:.2f})")

    # Allowlist filter
    if _strip_enc(top_enc) not in _RESTORE_SET:
        return (None, f"top {top_enc!r} ({top_conf:.2f}) not in RESTORE_ENCODINGS")

    # Noise floor — keep cp1006/IBM855-class flukes out
    if top_conf < 0.10:
        return (None, f"top {top_enc!r} conf={top_conf:.2f} below noise floor 0.10")

    return (top_enc, f"conf={top_conf:.2f}")


def detect_line_ending(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return "crlf" if crlf > lf else "lf"


def normalize_line_endings(data: bytes, target: str) -> bytes:
    unified = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if target == "crlf":
        return unified.replace(b"\n", b"\r\n")
    return unified


def atomic_write(path: str, data: bytes):
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
    cp = cache_path(session_id, path)
    try:
        os.unlink(cp)
    except FileNotFoundError:
        pass
    except OSError as e:
        _log(f"failed to delete cache {cp}: {e}")
        return False
    sd = session_dir(session_id)
    try:
        if os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)
    except OSError:
        pass
    return True


def convert_file(path: str, from_enc: str, to_enc: str,
                 target_eol: str | None = None) -> bool:
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


def handle_pre(hook_json: dict):
    session_id = hook_json.get("session_id", "unknown")
    path = extract_file_path(hook_json)
    if not path or not os.path.isfile(path):
        return

    # Cache self-heal (same as 5.x)
    cached = load_cache(session_id, path)
    if cached:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            _log(f"cannot read {path} for cache validation: {e}")
            return
        try:
            raw.decode("utf-8")
            return  # already converted
        except UnicodeDecodeError:
            pass
        _log(f"stale cache for {path} (file is not UTF-8), removing")
        delete_cache(session_id, path)

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        _log(f"cannot read {path}: {e}")
        return

    enc, reason = detect_safely(raw)
    if enc is None:
        if reason and "already utf-8" not in reason:
            _log(f"skip {path}: {reason}")
        return

    norm = normalize_encoding(enc)

    if _strip_enc(norm) not in _RESTORE_SET:
        _log(f"skip {path}: normalized {norm!r} not in RESTORE_ENCODINGS (post-alias)")
        return

    line_ending = detect_line_ending(raw)

    if convert_file(path, norm, "utf-8"):
        save_cache(session_id, path, norm, 1.0, line_ending)
        _log(f"[{session_id[:8]}] converted {path} {norm}->utf-8 eol={line_ending} ({reason})")
        _flog(f"[{session_id[:8]}] pre converted {path} {norm}->utf-8 "
              f"eol={line_ending} ({reason})")


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
        _flog(f"[{session_id[:8]}] post restored {path} utf-8->{orig_enc} eol={orig_eol}")
        if delete_cache(session_id, path):
            _log(f"[{session_id[:8]}] restored {path} utf-8->{orig_enc} eol={orig_eol}")
        else:
            _log(f"[{session_id[:8]}] restored {path} but cache delete failed")
    else:
        _log(f"[{session_id[:8]}] restore FAILED for {path}, "
             f"file remains UTF-8, cache preserved for retry")
        _flog(f"[{session_id[:8]}] post restore FAILED {path} (target {orig_enc})")


def handle_restore_all(hook_json: dict | None):
    """Restore every file still in the session cache (e.g. Read-only without Edit).

    Registered for Stop, SessionEnd, and SessionStart(resume) — the
    triggering event is taken from stdin for the forensic log.
    """
    if not hook_json:
        return
    session_id = hook_json.get("session_id", "unknown")
    trigger = hook_json.get("hook_event_name") or "Stop"
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
            continue
        if not os.path.isfile(path):
            delete_cache(session_id, path)
            continue
        orig_enc = cached["encoding"]
        orig_eol = cached["line_ending"]
        if convert_file(path, "utf-8", orig_enc, target_eol=orig_eol):
            delete_cache(session_id, path)
            _log(f"[{session_id[:8]}] restore-all[{trigger}] restored {path} "
                 f"utf-8->{orig_enc} eol={orig_eol}")
            _flog(f"[{session_id[:8]}] restore-all[{trigger}] restored {path} "
                  f"utf-8->{orig_enc} eol={orig_eol}")
        else:
            _log(f"[{session_id[:8]}] restore-all[{trigger}] FAILED for {path}, "
                 f"cache preserved")
            _flog(f"[{session_id[:8]}] restore-all[{trigger}] FAILED {path} "
                  f"(target {orig_enc})")
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

def classify_record(record_file: str) -> tuple[str, dict | None, str]:
    """Classify one cache record by on-disk facts.

    "obsolete"       — provably worthless: unreadable/invalid record, target
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
        return ("obsolete", None, f"unreadable record: {e}")
    if (not isinstance(data, dict)
            or not isinstance(data.get("path"), str)
            or not data["path"]
            or not isinstance(data.get("encoding"), str)
            or data.get("line_ending") not in ("lf", "crlf")
            or _strip_enc(data["encoding"]) not in _RESTORE_SET):
        return ("obsolete", data if isinstance(data, dict) else None, "invalid record fields")
    path = data["path"]
    if not os.path.isfile(path):
        return ("obsolete", data, "target file no longer exists")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        # Cannot prove anything about a file we cannot read — keep the record.
        return ("actionable", data, f"file unreadable right now ({e}); record kept")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return ("obsolete", data, "file is no longer UTF-8 (already restored or rewritten)")
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


def _guard_reason(row: dict) -> str | None:
    """Why this record must not be modified right now, or None.

    The only guard is the session's own records (a known fact via
    CLAUDE_CODE_SESSION_ID — they are in-flight and will be handled by the
    Stop/SessionEnd hooks). Records of OTHER possibly-live sessions are
    deliberately not second-guessed: concurrent sessions on the same
    project are declared unsupported in the README, and scan/restore/prune
    only ever run on the user's explicit request.
    """
    own = _own_session_dir_name()
    if own is not None and row["session"] == own:
        return "own live session (CLAUDE_CODE_SESSION_ID)"
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
    obsolete = [r for r in rows if r["category"] == "obsolete"]
    if as_json:
        print(json.dumps({
            "cache_root": CACHE_ROOT,
            "scope": scope_cwd,
            "own_session": _own_session_dir_name(),
            "summary": {"actionable": len(actionable), "obsolete": len(obsolete)},
            "records": rows,
        }, ensure_ascii=False, indent=2))
        return 0
    where = f" for files under {scope_cwd}" if scope_cwd else " (all sessions)"
    if not rows:
        print(f"encoding-guard: no cache records found{where}. Clean.")
        return 0
    print(f"encoding-guard: {len(actionable)} restorable, {len(obsolete)} obsolete record(s){where}\n")
    by_session: dict[str, list[dict]] = {}
    for r in rows:
        by_session.setdefault(r["session"], []).append(r)
    own = _own_session_dir_name()
    for name, group in by_session.items():
        tags = [_age_str(group[0]["session_age_seconds"])]
        if own is not None and name == own:
            tags.append("THIS session")
        print(f"session {name} ({', '.join(tags)}):")
        for r in group:
            mark = "RESTORABLE" if r["category"] == "actionable" else "obsolete"
            target = r["path"] or r["record_file"]
            enc = f" [{r['encoding']}]" if r["encoding"] else ""
            print(f"  {mark:>10}  {target}{enc} — {r['detail']}")
        print()
    if scope_cwd is not None:
        if actionable:
            print("To restore these files to their original encoding, run this "
                  "command again with mode 'restore-current'.")
        if obsolete:
            print("To remove obsolete records, run mode 'prune-current'.")
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


def handle_restore_current(paths_filter: list[str] | None) -> int:
    """Restore actionable records whose target lies under the current directory."""
    cwd = os.getcwd()
    rows = collect_records(cwd)
    restored = failed = skipped = 0
    touched_sessions: set[str] = set()
    for row in rows:
        if row["category"] != "actionable" or not _match_paths_filter(row, paths_filter):
            continue
        reason = _guard_reason(row)
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
            _flog(f"[cli] restore-current restored {row['path']} utf-8->{enc} "
                  f"(session {row['session'][:8]})")
            restored += 1
        else:
            print(f"FAILED   {row['path']} — could not re-encode to {enc}; "
                  f"record kept (file may have been edited as UTF-8)")
            _flog(f"[cli] restore-current FAILED {row['path']} (target {enc})")
            failed += 1
    _cleanup_empty_dirs(touched_sessions)
    print(f"\nrestore-current: {restored} restored, {failed} failed, {skipped} skipped")
    return 0 if failed == 0 else 1


def handle_prune_current(paths_filter: list[str] | None,
                         discard_actionable: bool) -> int:
    """Delete obsolete records under the current directory.

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
        reason = _guard_reason(row)
        if reason is not None:
            print(f"SKIP   {row['path'] or row['record_file']} — {reason}")
            skipped += 1
            continue
        with contextlib.suppress(OSError):
            os.unlink(row["record_file"])
        touched_sessions.add(row["session"])
        label = "discarded" if row["category"] == "actionable" else "pruned"
        print(f"{label.upper():>9}  {row['path'] or row['record_file']} — {row['detail']}")
        _flog(f"[cli] prune-current {label} {row['path'] or row['record_file']} "
              f"({row['detail']})")
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
        if mode == "prune-current":
            ap.add_argument("--discard", action="store_true",
                            help="also delete restorable records (accept current UTF-8 state)")
    args = ap.parse_args(argv)
    if mode == "scan":
        return handle_scan(None, args.json)
    if mode == "scan-current":
        return handle_scan(os.getcwd(), args.json)
    if mode == "restore-current":
        return handle_restore_current(args.path or None)
    return handle_prune_current(args.path or None, args.discard)


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
        sys.exit(0)


if __name__ == "__main__":
    main()

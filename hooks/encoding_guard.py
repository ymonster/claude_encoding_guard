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
    4. Stop (restore-all): restore any remaining files that were read but never edited

Cache layout:
    <tempdir>/.cc_encoding_cache/<session_id>/<sha256(normalized_path)[:16]>.json

Each session gets its own cache directory. Stale sessions (>24h) are cleaned up on startup.
Stdin is read as binary (sys.stdin.buffer) to avoid Windows codepage issues.
"""

# Read stdin immediately before any heavy imports (Windows stdin reliability)
import sys
_raw_stdin = sys.stdin.buffer.read()

import contextlib
import json
import os
import hashlib
import shutil
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
}

ENCODING_ALIASES = {
    "gb2312": "gbk",
    "iso-8859-1": "windows-1252",
}

# CJK encodings whose chardet detection (>=0.9 confidence) is reliable enough
# to skip the binaryornot pre-check. binaryornot's decision tree false-positives
# short Shift_JIS / EUC-JP / EUC-KR / Big5 / GB18030 files (<~500B) as binary;
# chardet's structural validators for these encodings have strict byte-range
# rules that real binaries cannot satisfy at high confidence.
CJK_TRUSTED = {
    "gbk", "gb18030",
    "big5", "big5hkscs",
    "shiftjis", "eucjp", "euckr", "iso2022jp",
}


def _strip_enc(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "")


def _log(msg: str):
    sys.stderr.write(f"encoding_guard: {msg}\n")


def normalize_encoding(enc: str) -> str:
    stripped = _strip_enc(enc)
    for orig, alias in ENCODING_ALIASES.items():
        if stripped == _strip_enc(orig):
            return alias
    return stripped


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
    """Remove session cache directories older than STALE_HOURS, skipping current session."""
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
                if age > STALE_HOURS * 3600:
                    shutil.rmtree(sid_dir, ignore_errors=True)
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

    # Trust chardet for CJK (encoding name + the >= 0.5 confidence gate above
    # is already enforced). binaryornot false-positives short J/K files; for
    # CJK encodings chardet's structural validators are strict enough on their
    # own. Non-CJK encodings (Windows-1252, ISO-8859-1) still go through
    # binaryornot since those single-byte encodings cannot rule out binary.
    if _strip_enc(norm) not in CJK_TRUSTED:
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
        _log(f"[{session_id[:8]}] converted {path} {norm}→utf-8 eol={line_ending} "
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
            _log(f"[{session_id[:8]}] restored {path} utf-8→{orig_enc} eol={orig_eol}")
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
            _log(f"[{session_id[:8]}] stop-restored {path} utf-8→{orig_enc} eol={orig_eol}")
        else:
            _log(f"[{session_id[:8]}] stop-restore FAILED for {path}, cache preserved")
    # Remove session dir if every entry was cleaned up via os.unlink
    # (delete_cache paths already handle this, but orphan paths above don't).
    with contextlib.suppress(OSError):
        if os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pre", "post", "restore-all"):
        _log("Usage: encoding_guard.py <pre|post|restore-all>")
        sys.exit(0)  # never block Claude Code

    mode = sys.argv[1]

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

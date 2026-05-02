#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["chardet>=7,<8"]
# ///
"""
encoding_guard.py (chardet 7.x experimental) - Preserve file encoding and
line endings when Claude Code edits files.

This is the chardet7-preview branch. Behavioral parity with main was verified
via _backup/pr_test_harness.py (73/73 fixtures PASS). Differences from main:

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

See TECHNICAL.md "chardet 7.x Migration Findings" for the empirical
rationale and corner-case catalog.
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
}

ENCODING_ALIASES = {
    "gb2312": "gbk",
    "iso-8859-1": "windows-1252",
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


_RESTORE_SET = {_strip_enc(e) for e in RESTORE_ENCODINGS}


def detect_safely(data: bytes) -> tuple[str | None, str]:
    """7.x detection (v2: 方向 3+4 混合).

    Strategy — trust chardet 7.x's own filtering layers:
      1. Stage 5 binary detection: encoding=None means binary (verified hard:
         even with ignore_threshold=True, real binaries return None at
         confidence 0.95-1.00). This is our absolute binary cut.
      2. encoding_era=MODERN_WEB shrinks the candidate space to web-relevant
         encodings (no LEGACY_MAC / DOS / MAINFRAME noise).
      3. prefer_superset=True collapses superset/subset siblings (CP949
         absorbs EUC-KR; otherwise short EUC-KR files tie at gap=0).
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
            encoding_era=EncodingEra.MODERN_WEB,
            prefer_superset=True,
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

    return (top_enc, "")


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
        _log(f"[{session_id[:8]}] converted {path} {norm}→utf-8 eol={line_ending}")


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
            _log(f"[{session_id[:8]}] restored {path} but cache delete failed")
    else:
        _log(f"[{session_id[:8]}] restore FAILED for {path}, file remains UTF-8")


def handle_restore_all(hook_json: dict | None):
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
            _log(f"[{session_id[:8]}] stop-restored {path} utf-8→{orig_enc}")
        else:
            _log(f"[{session_id[:8]}] stop-restore FAILED for {path}")
    with contextlib.suppress(OSError):
        if os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pre", "post", "restore-all"):
        _log("Usage: encoding_guard.py <pre|post|restore-all>")
        sys.exit(0)

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
        sys.exit(0)


if __name__ == "__main__":
    main()

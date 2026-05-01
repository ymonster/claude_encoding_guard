# Technical Notes

Implementation details, platform quirks, and edge cases encountered during development.

## Platform: Windows

### stdin Pipe and `uv run`

Claude Code passes hook input as JSON on stdin. On Windows, two issues compound:

1. **`uv run` (without `--script`) consumes PostToolUse stdin.** The project sync phase closes or drains the stdin pipe before the Python script starts. PreToolUse works because uv's first-run environment setup completes before stdin is read. Workaround: `uv run --script` skips project discovery entirely.

2. **Python defaults to GBK codepage for stdin.** Claude Code sends UTF-8 JSON, but `sys.stdin.read()` decodes via the system codepage (GBK on Chinese Windows). Characters outside GBK range cause `UnicodeDecodeError`. Workaround: `sys.stdin.buffer.read().decode("utf-8")` — binary read then explicit UTF-8 decode.

3. **Stdin must be read before heavy imports.** Some imports or uv initialization may interfere with the pipe. The script reads `sys.stdin.buffer.read()` on the very first line after `import sys`.

### `python3` Command

On Windows, `python3` is typically a Microsoft Store stub (exit code 49, opens Store). `uv run --script` bypasses this entirely — it manages its own Python discovery.

### CRLF Line Endings

Claude Code's Edit tool may convert CRLF to LF ([#38887](https://github.com/anthropics/claude-code/issues/38887)). The hook detects the dominant line ending style before conversion and restores it after. Implementation: all line endings are unified to `\n` first, then expanded to `\r\n` if the original was CRLF. This correctly handles mixed-EOL files.

### File Locking

Windows antivirus or indexing services may temporarily lock files. The hook handles this in two places:
- `atomic_write()`: if `os.replace()` fails, the temp file is cleaned up and the original file is preserved.
- `delete_cache()`: returns `False` on failure; the Post handler logs a warning and the Pre handler will detect the stale cache on next invocation.

## Encoding Detection

### chardet Version Sensitivity

chardet 7.x is a complete rewrite (Mar 2026) with a cosine-similarity bigram-model scoring system. Its confidence values are **not directly comparable** to chardet 5.x: same content gives very different numbers, and the calibration varies by encoding (driven by bigram inventory size in each model).

| Encoding | chardet 5.2.0 confidence | chardet 7.4.3 confidence (typical, 5KB sample) |
|---|---|---|
| GBK / GB2312 | 0.99 | 0.59–0.67 |
| Big5 | 0.99 | **0.37–0.40** (large bigram inventory geometry, see chardet design doc) |
| GB18030 | 0.99 | 0.14–0.25 |
| EUC-JP | 0.99 | 0.83–0.91 |
| Shift_JIS | 0.99 | 0.83 (returned as `cp932`, not `SHIFT_JIS`) |
| EUC-KR | 0.99 | 0.85 (returned as `CP949`, not `EUC-KR`) |

Other behavioral changes in 7.x that block a drop-in upgrade:
- **Built-in binary detection** at pipeline stage 5 returns `encoding=None` for binaries (could replace binaryornot)
- **Default encoding names changed** for some codecs — `shift_jis` → `cp932`, `euc-kr` → `CP949` (deliberate for the latter; arguably a missing `_COMPAT_NAMES` mapping for the former)
- **`max_bytes`, `encoding_era`, `include_encodings`, `prefer_superset`, `compat_names`** — new tuning parameters

Pinned to `>=5,<6` via PEP 723 inline metadata until a deliberate 7.x migration redesigns the encoding-set, threshold, and binary-check layers together. The `uv run --script` creates an isolated environment — the pinned version does not affect the user's project dependencies.

### Binary File Misidentification

Tested with real `.lib` and `.dll` files:

| File | chardet result | Would trigger hook? |
|------|---------------|-------------------|
| `iconv.lib` (3KB) | Windows-1252, 0.73 | Yes — **dangerous** |
| `pthreadVC2.dll` (55KB) | KOI8-R, 0.61 | Yes — **dangerous** |
| `gwschedule_32_d.lib` (4KB) | Windows-1252, 0.73 | Yes — **dangerous** |
| `dscompress.dll` (119KB) | None, 0.00 | No |

Windows-1252 is a single-byte encoding that can decode almost any byte sequence without error. Without binary detection, the hook would "successfully" convert binary files, and Claude's subsequent edit would corrupt them irreversibly.

[binaryornot](https://github.com/binaryornot/binaryornot) uses a trained decision tree with 24 features including CJK encoding validity checks, Shannon entropy, magic signatures, and null byte ratios. It correctly identifies all tested binary files but false-positives in two patterns:

- **Short CJK files** (<500 bytes Shift_JIS / EUC-JP / EUC-KR / Big5 / GB18030, observed at 50–240 bytes in PoC).
- **Mixed Cyrillic + ASCII files** — e.g., source code with CP1251 comments. Empirically a 115-byte C source with a single CP1251 comment line is flagged binary even though `chardet` returns `windows-1251` at 0.99 confidence.

To resolve this without losing binaryornot's protection on Windows-1252 binaries, the Pre hook uses a structural-trust short-circuit:

```python
encoding, confidence = chardet.detect(...)
if _strip_enc(norm) not in _RESTORE_SET:        # outside our supported set: skip
    return
if confidence < 0.5:                             # confidence floor: skip
    return
if _strip_enc(norm) not in STRUCTURAL_TRUSTED:   # only run binaryornot if not trusted
    if is_binary(path):
        return
```

`STRUCTURAL_TRUSTED = {gbk, gb18030, big5, big5hkscs, shiftjis, eucjp, euckr, iso2022jp, windows1251}`. These encodings have strict structural rules (multi-byte lead/trail ranges for CJK; characteristic Cyrillic byte distribution for cp1251) that chardet's probers verify directly — a real binary cannot satisfy them across hundreds of bytes at confidence ≥ 0.5. binaryornot remains the binary check for Windows-1252 / ISO-8859-1, where chardet alone cannot rule out binary (single-byte Latin encodings can decode any bytes).

Validated on 100+ fixtures (our PoC + binaryornot's own test set + Cyrillic 8-size matrix in `_backup/fixtures/cyrillic_sizes/`): all supported text files correctly converted with byte-identical round-trip, all binaries correctly rejected, zero corruption.

### Encoding Aliases

chardet reports `GB2312` but Python's `gb2312` codec is stricter than `gbk`. Real-world files labeled GB2312 often contain GBK-range characters. Mapping to the superset (`gbk`) avoids `UnicodeEncodeError` during restore.

Similarly, `ISO-8859-1` → `Windows-1252` follows the HTML specification's behavior. The 0x80-0x9F range in real files almost always contains CP1252 characters (€, curly quotes, em dash), not C1 control characters.

### Codec Name Preservation

`normalize_encoding` returns chardet's name verbatim for the non-aliased path:

```python
def normalize_encoding(enc: str) -> str:
    stripped = _strip_enc(enc)
    for orig, alias in ENCODING_ALIASES.items():
        if stripped == _strip_enc(orig):
            return alias
    return enc                  # NOT _strip_enc(enc)
```

Returning the dash-stripped form (`"windows1251"`, `"windows1252"`, `"iso88591"`) breaks codec lookup. Python's codec normalizer replaces `-` with `_` but cannot recover a missing separator: `windows-1251` / `windows_1251` / `cp1251` all resolve to the cp1251 codec, but `windows1251` raises `LookupError`.

The aliased path (gb2312 → gbk, iso-8859-1 → windows-1252) was unaffected because the alias value is returned verbatim with its dashes intact. Direct chardet hits on `windows-1252` silently failed in `convert_file` until [#3](https://github.com/ymonster/claude_encoding_guard/pull/3) added windows-1251 to RESTORE_ENCODINGS and exposed the same pattern across every Cyrillic test size — surfacing the latent bug for both encodings simultaneously.

## Cache Design

### Why Session Isolation?

Without session isolation:
```
Session A: Read → convert GBK→UTF-8 → cache
Session B: Read → sees cache exists → skip (uses A's cache)
Session A: Edit → Post restore → delete cache
Session B: Edit → Post → no cache → cannot restore!
```

With session isolation (`<tmpdir>/.cc_encoding_cache/<session_id>/`), each session manages its own cache independently.

### Stale Cache Self-Healing

Pre hook validates cache on every access:
1. Cache exists for this file in this session?
2. Can the file be decoded as UTF-8? (`raw.decode("utf-8")`)
   - **Yes** → file is in converted state, cache is valid, skip
   - **No** → file was already restored (stale cache from failed delete), remove cache, re-convert

**Edge case**: Windows-1252 files whose original bytes are valid UTF-8 (e.g., `C2 A9` = `©` in UTF-8, `Â©` in Windows-1252). The stale cache won't self-heal because the file passes the UTF-8 decode check. This requires both `delete_cache` failure AND the original bytes to be coincidentally valid UTF-8 — a narrow edge case that doesn't affect CJK encodings.

### Cleanup

- Session directories older than 24 hours are removed on each Pre hook invocation
- Current session is always skipped during cleanup (compared by sanitized session_id)
- Empty session directories are removed after the last cache file is deleted

## Why Read-Time Conversion?

### Failed Approach: Edit-Time Conversion

The initial design converted files in PreToolUse for Edit only:
1. Pre(Edit): detect GBK → convert to UTF-8 → cache
2. Claude edits
3. Post(Edit): UTF-8 → GBK

This failed because Claude Code v2.1.90+ silently accepts file modifications from hooks without re-reading content. Claude's Edit uses its in-memory content from the last Read (which was garbled GBK-as-UTF-8), writes it back, and the file gets corrupted with U+FFFD.

### Failed Approach: Deny + Re-Read

The `permissionDecision: "deny"` mechanism was tried to force Claude to re-Read after conversion. This failed because:
1. Claude retries the Edit without actually re-Reading (uses stale in-memory content)
2. PostToolUse fires for denied edits too, prematurely consuming the cache
3. The file bounces between GBK and UTF-8 in a loop

### Working Approach: Read-Time Conversion

Converting at Read time ensures Claude's first in-memory view is already correct UTF-8. The Pre hook matcher includes `Read|Edit|Write` for Pre (convert before any file access) and `Edit|Write` for Post (restore after modification).

### Stop Hook: Read-Without-Edit Recovery

Post only fires after Edit/Write. If Claude reads a file but never edits it in that turn, the file is left in UTF-8 form and the cache entry is never consumed. To recover, a `Stop` hook runs `encoding_guard.py restore-all` at the end of every Claude turn: it enumerates the session cache directory (Stop stdin has no `tool_input.file_path`) and restores each still-cached file through the same `convert_file` helper that `handle_post` uses. Cache validation goes through `load_cache` so the Stop path applies the same `_RESTORE_SET` / `line_ending` / type checks as Post. (Approach contributed by [@lbresler](https://github.com/lbresler) in [#2](https://github.com/ymonster/claude_encoding_guard/pull/2).)

**Accepted trade-off — multi-session race.** When two CC instances operate on the same file, the restore triggered by one session's Stop can overwrite a pending Edit in another session. Session-isolated caches prevent most cross-contamination, but a narrow race window remains. Concurrent CC use is rare enough that this trade-off is accepted in exchange for Read-without-Edit recovery in the common single-session case.

## Atomic File Writes

All file writes use `atomic_write()`:
1. Create temp file in the same directory (`tempfile.mkstemp`)
2. Write data, `flush()`, `os.fsync()`
3. `os.replace()` atomically replaces the original

If step 2 or 3 fails, the temp file is cleaned up and the original file is preserved. This prevents data loss from disk-full errors, process interruption, or permission issues.

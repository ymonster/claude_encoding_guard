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

**On this branch (`chardet7-preview`)**, chardet is pinned to `>=7,<8`. The migration was driven by an empirically validated detect-and-trust strategy — see "chardet 7.x Migration Findings" below. The `uv run --script` creates an isolated environment — the pinned version does not affect the user's project dependencies.

### Binary File Misidentification (historical, applies to `main`)

This section describes the binary-detection design on the `main` branch (chardet 5.x + binaryornot). On `chardet7-preview` it has been replaced by chardet 7.x's pipeline stage 5 — see "chardet 7.x Migration Findings → Why no manual binary check" below.

Tested with real `.lib` and `.dll` files on chardet 5.x:

| File | chardet 5.x result | Would trigger hook? |
|------|---------------|-------------------|
| `iconv.lib` (3KB) | Windows-1252, 0.73 | Yes — **dangerous** |
| `pthreadVC2.dll` (55KB) | KOI8-R, 0.61 | Yes — **dangerous** |
| `gwschedule_32_d.lib` (4KB) | Windows-1252, 0.73 | Yes — **dangerous** |
| `dscompress.dll` (119KB) | None, 0.00 | No |

Windows-1252 is a single-byte encoding that can decode almost any byte sequence without error. Without binary detection, the hook would "successfully" convert binary files, and Claude's subsequent edit would corrupt them irreversibly.

On `main`, [binaryornot](https://github.com/binaryornot/binaryornot) handles this with a 24-feature decision tree, plus a `STRUCTURAL_TRUSTED` short-circuit to bypass binaryornot's known false positives on short CJK files and mixed Cyrillic+ASCII content. Both layers are removed on this branch because chardet 7.x's stage 5 returns `encoding=None` directly for the same inputs, with no false positives observed.

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

## chardet 7.x Migration Findings

This branch (`chardet7-preview`) was prototyped to evaluate replacing chardet 5.x + binaryornot with chardet 7.x's built-in pipeline. The harness (`_backup/pr_test_harness.py`) reports 73/73 fixtures PASS — same as `main`'s 5.x baseline. What follows is the empirical rationale for the design choices that got there.

### Detection Strategy (`detect_safely`)

```python
# 1. Empty input → utf-8 (handled before this function)
# 2. Pure-ASCII short-circuit (all bytes < 0x80) → skip
# 3. chardet.detect(data,
#                   encoding_era=EncodingEra.MODERN_WEB,
#                   prefer_superset=True)
# 4. encoding == None         → binary, skip (stage 5 cut)
# 5. encoding == "utf-8"      → already converted, skip
# 6. _strip_enc not in RESTORE→ unsupported encoding, skip
# 7. confidence < 0.10        → noise, skip
# 8. otherwise → return encoding
```

No `>= 0.30 / >= 0.60 / gap >= 0.10` thresholds, no STRUCTURAL_TRUSTED bypass, no binaryornot. The 0.10 floor is purely to drop `cp1006`/`IBM855`-class flukes; everything else is delegated to chardet 7.x's own filtering layers.

### Why no high confidence threshold?

7.x's confidence is **cosine similarity** in the bigram-frequency space, not a calibrated probability. Empirically (5KB samples):

| Encoding | 7.x confidence | Why |
|---|---|---|
| GB18030 | 0.14–0.25 | Largest bigram inventory in the model — geometrically pinned low |
| Big5 | 0.37–0.40 | Second-largest bigram inventory |
| GBK | 0.59–0.67 | Smaller inventory, higher score |
| EUC-JP / Shift_JIS / EUC-KR / Cyrillic / cp1252 | 0.6–0.95 | Smaller inventories or distinctive byte patterns |

A 0.30 floor would silently skip every GB18030 file regardless of length. A 0.60 floor would also drop Big5. The fix is not "raise the threshold" — it's "trust the structural layers that come before scoring."

### Why no manual binary check?

chardet 7.x's pipeline stage 5 returns `encoding=None` for non-text input at confidence 0.95–1.00, and this verdict is independent of `ignore_threshold`. Empirically across PoC fixtures (`bin_random_*`, `bin_png`, `bin_jpeg`, `bin_pe_head`, `bin_gzip`, `bin_adversarial_cjk`):

| Binary fixture | Result |
|---|---|
| 200B–5KB random bytes | None(0.95) |
| Minimal PNG / JPEG / PE / gzip | None(1.00) |
| Adversarial: random + GBK chunk | None(0.95) |

No false positives. binaryornot's known false positives on `_backup/cp1251_test/test.c` (115B mixed CP1251 source) and short Shift_JIS/EUC-JP files do not occur with chardet 7.x.

### Why `EncodingEra.MODERN_WEB`?

chardet 7.x ships 99 encoding probers spread across MODERN_WEB / LEGACY_ISO / LEGACY_MAC / LEGACY_REGIONAL / DOS / MAINFRAME eras. Restricting to MODERN_WEB shrinks the candidate space to web-relevant encodings (UTF-*, Windows-125x, CP874, KOI8-*, CJK multibyte) without losing anything we support.

### Why `prefer_superset=True`?

For superset/subset sibling pairs, raw 7.x can return both at identical confidence (e.g., on a 49-byte EUC-KR fixture: `[CP949(0.58), EUC-KR(0.58)]`). With `prefer_superset=True` the subset is collapsed onto its superset, eliminating tied gaps that would break a gap-based heuristic. We don't use a gap heuristic any more, but the collapse also gives us cleaner Python codec semantics: `cp949` is a strict superset of `euc-kr`, so its codec round-trip is more permissive.

### Why an explicit ASCII short-circuit?

chardet 7.x reports any pure-ASCII content as `Windows-1252(1.00)` because ASCII is a cp1252 subset and the bigram model has no positive ASCII signal. Converting these as cp1252 is a no-op round-trip but still mutates the file and writes a cache entry, breaking the "ASCII files should be invisible to the hook" expectation. A 5-line `all(b < 0x80 for b in data)` check upstream of `chardet.detect` skips them cleanly. Setting `no_match_encoding="ascii"` does **not** help — that parameter is for "no candidate survived," not for "ASCII matched cp1252 perfectly."

### Codec name conventions

7.x's `compat_names=True` default still returns chardet 7.x canonical names for some codecs:

| 5.x name (chardet) | 7.x name (chardet 7.4.3, default) | Python codec status |
|---|---|---|
| `gbk` / `gb2312` (per-input) | `GB18030` (always, regardless of input) | strict superset, accepts both |
| `shift_jis` | `cp932` | strict superset |
| `euc-kr` | `CP949` | strict superset |
| `Big5`, `Big5-HKSCS`, `EUC-JP`, `ISO-2022-JP`, `Windows-125x`, `ISO-8859-*` | unchanged | — |

We rely on the fact that Python's codec registry accepts the 7.x names directly, and a superset codec correctly round-trips bytes that were originally encoded under its subset codec. So we add `cp932`, `CP949`, `GB18030` to RESTORE_ENCODINGS and never need to remap names back to IANA originals.

### `EUC-TW` not supported by chardet 7.x

`EUC-TW` exists in our `RESTORE_ENCODINGS` set as a defensive holdover, but chardet 7.x has no EUC-TW prober. Passing it via `include_encodings=...` raises `Unknown encoding 'EUC-TW'`. Real files in EUC-TW are rare; we keep the entry so a 5.x fallback still recognizes it, but on this branch chardet will never return it.

### Why `detect()` not `detect_all()`?

Reading chardet's source: `detect()` is `run_pipeline(...)[0].to_dict()` plus optional `prefer_superset` / `compat_names` post-processing. `detect_all()` returns the full list and adds an `ignore_threshold` knob. Since our strategy doesn't read `results[1:]` for any cross-candidate logic and we want the equivalent of `ignore_threshold=True` (let our own 0.10 floor decide), `detect()` is the precise tool — same shaping params, no wasted `to_dict` work, intent is clear at the call site.

### License

chardet 7.x is **0BSD**-licensed (chardet 5.x was LGPL-2.1+). The plugin itself remains MIT. With `binaryornot` (BSD-3-Clause) dropped on this branch, runtime dependencies are now uniformly permissive — no LGPL-style "user must be able to swap library" obligation, even though `uv run --script` already satisfied it.

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

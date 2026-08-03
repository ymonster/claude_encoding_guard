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

- Only **empty** session directories older than 24 hours are removed on each Pre hook invocation — record-bearing directories are never auto-deleted (see Crash Recovery Architecture below)
- Current session is always skipped during cleanup (compared by sanitized session_id)
- Empty session directories are removed after the last cache file is deleted

## Crash Recovery Architecture

If the CC process dies mid-session (crash, kill, terminal closed), converted files stay UTF-8 on disk and their cache records become orphans. The recovery design rests on one ownership principle and splits into four layers.

### Ownership principle: records are keyed by session, and hooks never cross that line

A cache record is the only copy of the fact "this file on disk should be re-encoded back". Two automatic policies for *another* session's stale records were considered and rejected:

- **Auto-delete after 24h** (the original behavior): irreversible information loss — after deletion not even a manual recovery is possible.
- **Restore-before-delete**: falsified during design review. Its guard conditions ("file is valid UTF-8" + "re-encodes cleanly") are *also* satisfied when the user has deliberately accepted the UTF-8 state (converted the project, changed build flags). An automatic restore would silently override that intent.

Since neither automatic action is safe on stale cross-session state, the rule is: **hooks operate strictly on their own session's records; acting on another session's records is reserved for the user** via the explicit recovery commands. (Removing *empty* foreign dirs stays in the Pre-hook janitor — an empty dir carries no user data.)

### Four defense layers

| Layer | Trigger | Covers |
|---|---|---|
| `Stop` → restore-all | every completed turn | files read but not edited this turn (original PR #2 semantic) |
| `SessionEnd` → restore-all | session termination | turns interrupted with Esc (Stop does not fire on user interrupts — docs-confirmed) |
| `SessionStart(resume)` → restore-all | resuming a session | crash residue, healed **before** the resumed session acts |
| `scan` / `restore-current` / `prune-current` | user-invoked CLI | orphans from crashed sessions that are never resumed |

Why SessionStart matters even though the first turn's Stop would eventually heal: during that first turn the disk is still UTF-8, and the user's first instruction after a crash may be `git commit` — which would permanently record converted files. Healing at session start closes that window. At SessionStart no turn is in flight, so any record present is by definition residue; restoring immediately has no ambiguity.

**SessionStart matcher must be exactly `resume`.** Never `compact`: auto-compact can fire in the middle of a long turn, and a restore-all at that point converts files back to their original encoding while Claude still holds the UTF-8 view mid-turn — recreating the stale-baseline death spiral documented in "Stale-detection baseline timing" above. (`startup`/`clear` sessions have a fresh session_id and an empty cache dir, so registering them would be a no-op.)

Resume keeps the session ID on all three paths — CLI `--resume`, CLI `--continue` (documented), and the TUI `/resume` picker (verified empirically 2026-08-02: hook logs show identical session_id and transcript across an exit/resume boundary). That is what makes the SessionStart layer reach crash residue at all.

### Recovery commands

`scan` / `scan-current` are read-only reports over `CACHE_ROOT/*/​*.json` (discovery is record-driven — user project files are never crawled). Each record is classified by on-disk facts only:

- **moot** — provably worthless: unreadable/invalid record, target file missing, or target no longer valid UTF-8 (already restored or rewritten). Safe to prune.
- **actionable** — target exists and is valid UTF-8: restore is possible. Deliberately no encode-back test at scan time; a content drift that no longer fits the original encoding is discovered and reported by `restore-current` itself (`convert_file` fails gracefully and keeps the record).

`restore-current` / `prune-current` act only on records whose target lies under the cwd — there is intentionally **no global restore**: batch-restoring across projects is a decision no user can responsibly make in one keypress. Guards on both: the running session's own dir is excluded via `CLAUDE_CODE_SESSION_ID`, and records written within the last hour are skipped unless `--all` (a live parallel session's in-flight records look identical to orphans; the user is the only reliable liveness oracle). The liveness heuristic is judged per record file, not per session dir — restore/prune unlink sibling records and would bump the dir mtime, locking the user out of their own follow-up command.

**The modes are internal machinery**: the single user-facing surface is `/encoding-guard:scan`, which drives them. Direct invocation is a debugging/development affordance:

```bash
uv run --script hooks/encoding_guard.py scan [--json]              # read-only, all sessions & paths
uv run --script hooks/encoding_guard.py scan-current [--json]      # read-only, targets under cwd
uv run --script hooks/encoding_guard.py restore-current [--path FILE]... [--all]
uv run --script hooks/encoding_guard.py prune-current [--path FILE]... [--all] [--discard]
```

(`--json` machine-readable output; `--path` repeatable single-file filter; `--all` overrides the fresh-record guard; `--discard` extends prune to restorable records. When run from an installed plugin, the script lives under the plugin cache root — `~/.claude/plugins/cache/...` — which is exactly why end-user docs point at the slash command instead.) `prune-current` deletes moot records; actionable ones only with `--discard` (user accepts the UTF-8 state).

The `/encoding-guard:scan` slash command wraps this flow: inline `!`-preprocessing runs `scan-current --json` deterministically, then the command guides an AskUserQuestion (restore all / discard / pick per file / not now). Scan output is treated as untrusted data in that flow — only whitelisted-encoding names and normalized paths reach the option labels.

### Forensic log

Hook stderr is captured into transcripts (`hook_success` attachments) in current CC versions, but operations executed in process-shutdown windows never get there — empirically, closing the terminal window on Windows delivers `CTRL_CLOSE_EVENT`, CC runs the SessionEnd hook inside the grace period, the restore succeeds, and no transcript entry survives to prove it. Every conversion/restore/prune is therefore also appended to `CACHE_ROOT/encoding_guard.log` (best-effort, size-capped with a single rotation) tagged with its trigger: `pre converted`, `post restored`, `restore-all[Stop|SessionEnd|SessionStart] restored`, `[cli] restore-current` / `prune-current`. This is the only durable evidence trail for the shutdown-window cases.

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

## Hook Behavior Reference

Empirically verified against Claude Code 2.1.132 via an instrumented log-only hook in a separate playground project. Documented here as a stable reference for future hook design (read-only redirect, raw-byte backup, ACL gate, etc.).

### Hook stdin schemas (observed shapes)

`PreToolUse` for `Read`:
```json
{
  "session_id": "...",
  "transcript_path": "C:\\Users\\...\\<session_id>.jsonl",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Read",
  "tool_input": {"file_path": "..."},
  "tool_use_id": "toolu_..."
}
```

`PostToolUse` for `Read`:
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PostToolUse",
  "tool_name": "Read",
  "tool_input": {"file_path": "..."},
  "tool_response": {
    "type": "text",
    "file": {
      "filePath": "...",
      "content": "...",
      "numLines": 2,
      "startLine": 1,
      "totalLines": 2
    }
  },
  "tool_use_id": "toolu_...",
  "duration_ms": 6
}
```

`PreToolUse` for `Edit`:
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "...",
    "old_string": "...",
    "new_string": "...",
    "replace_all": false
  },
  "tool_use_id": "toolu_..."
}
```

`PostToolUse` for `Edit` (on success only — failures bypass the hook chain; see "Client-side validation" below):
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PostToolUse",
  "tool_name": "Edit",
  "tool_input": {"file_path": "...", "old_string": "...", "new_string": "...", "replace_all": false},
  "tool_response": {
    "filePath": "...",
    "oldString": "...",
    "newString": "...",
    "originalFile": "<full file content before Edit>",
    "structuredPatch": [
      {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
       "lines": ["-old", "+new"]}
    ],
    "userModified": false,
    "replaceAll": false
  },
  "tool_use_id": "toolu_...",
  "duration_ms": 21
}
```

Note the case mismatch: `tool_input` uses `snake_case` (`file_path`, `old_string`) while `tool_response` uses `camelCase` (`filePath`, `oldString`). This pattern holds for both `Edit` and `Write`.

`PreToolUse` for `Write`:
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Write",
  "tool_input": {"file_path": "...", "content": "..."},
  "tool_use_id": "toolu_..."
}
```

`PostToolUse` for `Write` (on success — `type` is `"create"` for a new file, `"update"` when overwriting an existing file):
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "PostToolUse",
  "tool_name": "Write",
  "tool_input": {"file_path": "...", "content": "..."},
  "tool_response": {
    "type": "create",
    "filePath": "...",
    "content": "<written content>",
    "structuredPatch": [],
    "originalFile": null,
    "userModified": false
  },
  "tool_use_id": "toolu_...",
  "duration_ms": 14
}
```

For `Write` overwriting an existing file (`type: "update"`), `originalFile` is the file's full content before the overwrite and `structuredPatch` contains the diff.

`Stop`:
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "Stop",
  "stop_hook_active": false,
  "last_assistant_message": "..."
}
```

`tool_use_id` is the natural correlation key between a Pre and its matching Post invocation. The hook also has `CLAUDE_PROJECT_DIR` set as an env var (project-relative `cwd` for the running CC instance). `duration_ms` appears in `Post` only.

### `updatedInput` semantics — "swap engine, keep facade"

When `PreToolUse` returns `hookSpecificOutput.updatedInput` together with `permissionDecision: "allow"`, the modified input takes effect for the actual tool execution but Claude is not informed of the change. Verified for `Read` with `file_path` redirect:

| Channel | Sees | Notes |
|---|---|---|
| Pre stdin | **original** input | Pre's chance to decide whether/how to modify |
| Tool execution | **updated** input | What CC actually runs |
| Post stdin (`tool_input`) | **updated** input | Reflects the post-modification state |
| Post stdin (`tool_response`) | result of running on **updated** input | e.g., content of the redirected file |
| Transcript `tool_use.input` (Claude's record) | **original** input | Claude believes it called what it asked for |
| `tool_result.content` returned to Claude | result of running on **updated** input | Claude sees the resulting bytes |

So a hook can transparently re-route or normalize a tool call without polluting Claude's mental model of what it did. The only signal Claude can pick up is content-level: if Claude reads the same path twice and sees different bytes, it can infer "something changed" but not "the path was redirected."

The empirical setup that produced this: `PreToolUse(Read("orig.txt"))` returning `updatedInput.file_path = "redirect.txt"` resulted in CC reading `redirect.txt`'s content, Claude receiving that content, but Claude's transcript `tool_use.input.file_path` still showing `orig.txt`. Both `Pre` and `Post` hook stdin showed exactly what the columns above describe.

**Caveat — `updatedInput` cannot redirect Edit/Write on existing files**: although the redirect mechanics work end-to-end for `Read`, the same redirect on `Edit`/`Write`-existing fails *before* the hook is invoked, due to the `readFiles`-tracker asymmetry described in "Asymmetric `readFiles` tracking" below. As of CC 2.1.132, `updatedInput` is effective only for tools whose client-side preconditions are either absent (e.g., `Read`, `Bash`) or already satisfied independently of the redirect (e.g., new-file `Write`).

### Version requirements

`hookSpecificOutput.updatedInput` (with `permissionDecision: "allow"`) is supported since at least Claude Code **2.1.0**. The 2.1.0 CHANGELOG entry is a fix for an `ask`-decision edge case, indicating the feature predates 2.1.0; the modern `hookSpecificOutput` schema has been stable since at least early 2.1.x. The plugin's existing reliance on `${CLAUDE_PLUGIN_ROOT}` and stdin JSON shape already implies `≥ 2.1.0` so adding `updatedInput` use does not raise the floor in practice.

### Edit vs Write semantic differences

| Dimension | `Edit` | `Write` |
|---|---|---|
| File existence | Must exist | May not exist (creates new) or exist (overwrites) |
| Read precondition | Must have been Read in current session before Edit (else CC fails the call before any hook fires) | Only when target exists: must have been Read in current session (Write tool prompt v2.1.120+ says "or the call will fail"). New-file creation has no Read precondition. |
| Modification mode | Surgical: replace `old_string` with `new_string` (optionally all occurrences via `replace_all`) | Full: replace entire content |
| Client-side fail-fast paths | `old_string` not found / matches multiple times; stale-detection (file content changed externally since Read); Read precondition unsatisfied | Stale-detection on existing target; Read precondition unsatisfied for existing target |
| `tool_response.type` field | (none) | `"create"` (new file) or `"update"` (overwrote existing) |
| Typical `tool_input` fields | `{file_path, old_string, new_string, replace_all}` | `{file_path, content}` |

For an encoding-handling plugin: `Edit` and `Write` on **existing** non-UTF-8 files share the same handling path (detect encoding, convert, do the operation, restore). `Write` on a **non-existent** file is a special case — there is no original encoding to detect, and the new file is naturally created as UTF-8. `Bash` tool calls that write to files (`sed -i`, `>`, `>>`, `vim`, etc.) bypass the `Edit`/`Write` hook chain entirely and are outside any encoding plugin's reach (see "Read-first invariant" below).

### Client-side validation (preempts the hook chain)

CC performs a series of validations on the model's tool call **before** invoking the `PreToolUse` hook chain. If any validation fails, CC generates an error `tool_result` for the model and **never fires `PreToolUse` or `PostToolUse`**. The TUI still renders an `Update(file)` / `Write(file)` line followed by `⎿  Error editing file` (or a more specific message) so the user sees something happened, but no hook log entry is produced and no `updatedInput` redirect is possible at that point.

Empirically observed validations on `Edit` / `Write`:

| Validation | Trigger | TUI display | LLM error indication | Hook fires? |
|---|---|---|---|---|
| `old_string` content check (Edit) | `old_string` doesn't appear in current file content | `Update(file) ⎿ Error editing file` | LLM concludes "string not present" from the response | ✗ |
| Stale-detection (content hash) | File content changed externally since the last Read in this session — even within the same session, even between two prompts in the same session | Same | "file has been modified externally; must re-Read" | ✗ |
| Read precondition | `Edit`/`Write`-existing on a file not in the session's `readFiles` set | `Update(file) ⎿ File must be read first` / `Write(file) ⎿ Error writing file` | "File must be read first" / "File has not been read yet" | ✗ |

All three validations have been **directly observed** to skip the hook chain. The `old_string` and stale paths were verified by forcing the model to issue an `Edit` whose preconditions cannot be satisfied (`old_string` literally not in the file; or rewriting the file content externally between Read and Edit). The Read-precondition path was verified by the dual-redirect experiment described in "Asymmetric `readFiles` tracking" below.

Important consequence for hook plugins: a hook **cannot** intercept these failures, cannot inspect the failed call, and cannot compensate. From the hook's perspective, the call simply never happened. This rules out any design where the hook would `updatedInput`-redirect the file path *after* CC has already validated against the original path's content — by the time the hook would see the call, CC has already short-circuited it.

The above is about CC's *internal* validation. A tool that **passes** validation, fires `PreToolUse`, then errors during its own execution (e.g., disk full, encoding write error) is a separate case — whether `PostToolUse` fires for that path is still an open empirical gap.

### Asymmetric `readFiles` tracking

Inside CC there is a session-scoped set of "files that have been Read this session", used by the Read-precondition check above. Empirically, **this set is populated and queried using two different paths when a `PreToolUse` hook redirects via `updatedInput.file_path`**:

- **Read** populates the set using the **post-redirect** path (the path the tool actually executed against).
- **Edit** / **Write** Read-precondition checks the set using the **pre-redirect** path (the path the model passed in `tool_input`, before any hook intervention).

These two paths are different exactly when a hook redirects, so the two ends never align, and `updatedInput` cannot transparently rescue an Edit/Write that targets the original (pre-redirect) path.

Controlled experiment (Claude Code 2.1.132):

- Fixture: `orig.txt = "ORIG_PHYSICAL"`, `redirect.txt = "REDIRECTED_VIA_HOOK"` (distinct contents).
- Single `PreToolUse` hook with matcher `*`: when `tool_input.file_path` basename equals `orig.txt`, return `updatedInput.file_path = ".../redirect.txt"`. The same redirect is structurally applied to `Read`, `Edit`, and `Write`.
- Plugin disabled — only the test hook is active.

Step-by-step observation:

1. `Read(orig.txt)` — redirect fires; tool reads `redirect.txt`; Claude receives `"REDIRECTED_VIA_HOOK"`; `pre_Read` and `post_Read` hook stdin both produced; transcript records `Read(orig.txt)`. The session's `readFiles` now contains `redirect.txt` (not `orig.txt`).
2. `Edit(orig.txt, ...)` — CC client-side check asks "is `orig.txt ∈ readFiles`?" Answer: No (only `redirect.txt` is in the set). CC fails fast with `File must be read first` and **never invokes `PreToolUse` for the Edit**. The redirect-on-Edit branch is unreachable. No `pre_Edit` log entry is produced. The same holds for `Write(orig.txt)` — fails fast with `Error writing file` / "File has not been read yet" before the Write hook can redirect.
3. The model self-corrects by re-issuing `Read(orig.txt)`. The hook redirects again; `readFiles` still does not gain `orig.txt`. The loop is infinite from the model's perspective. The model eventually verifies via `Bash` (`Get-Content` / `cat`) and detects the inconsistency between Read content and physical content, concluding aloud that "the read was redirected by a hook."

The hook author has no way to break this asymmetry from inside the hook: by the time `PreToolUse` for Edit/Write would run, the precondition has already failed and the hook is not invoked. Even adding more redirect entries to the same hook (matcher widened to all of `Read|Edit|Write`) does not help.

**Why this matters architecturally**: any "don't touch the original on disk" plugin design — where Pre creates a UTF-8 sibling and Pre redirects all reads/writes to it — is **structurally infeasible** under the current CC validation order, regardless of how clever the hook is. This is the single empirical finding that conclusively rules out the "Solution 2" family for `claude_encoding_guard`.

If CC were to (a) run `PreToolUse` before the precondition checks, or (b) populate `readFiles` with both pre- and post-redirect paths on a Read redirect, the asymmetry would close. An issue has been drafted upstream (TODO: link once filed).

### Stale-detection baseline timing — empirical pinning

The stale-detection baseline (the content snapshot that subsequent Edit/Write are compared against) is locked at the moment the Read tool **completes**, i.e., **before** the `PostToolUse(Read)` hook runs. Mutations performed inside `Post(Read)` do **not** enter the baseline; they show up as "drift" from the baseline that any later Edit/Write will fail-fast against.

Two empirical confirmations:

**Phase C (ASCII fixture, single Read + Write, "different mutation content"):**
- fixture `baseline_test.txt = "PRE_PHYSICAL"`
- Hook: `Pre(Read)` no-op; `Post(Read)` overwrites disk to `"POST_MODIFIED"`
- prompt 1: Read — tool returns `"PRE_PHYSICAL"`, Post mutates disk to `"POST_MODIFIED"`
- prompt 2: Write (without re-Read) — fails fast with "file has been modified externally"; no `pre_Write` hook entry. Confirms: baseline was `"PRE_PHYSICAL"` (the content tool Read saw, before Post ran), not `"POST_MODIFIED"` (the post-Post disk state).

**Phase D (real GBK fixture, simulates Solution 1 / "Post(Read) restore"):**
- fixture `baseline_test_gbk.txt` = `"你好世界_PRE_PHYSICAL_GBK_中文测试_价格行情数据_2026\n"` written as GBK (53 bytes)
- Hook: `Pre(Read)` GBK → UTF-8 (53 → 67 bytes); `Post(Read)` UTF-8 → GBK (67 → 53 bytes) — i.e., a faithful simulation of Solution 1's "convert in Pre, restore in Post"
- prompt 1: Read — pre_convert + post_restore both fire; Claude receives UTF-8 text correctly; disk ends as GBK
- prompt 2: Edit "中文测试" → "测试改名" — model issues Edit, CC fail-fasts at stale-detection (baseline = UTF-8 at tool-Read completion; current = GBK after Post restore). Model self-recovers by re-Read; another round of pre_convert + post_restore; another Edit; another stale-detection failure; loop.

The model's own diagnosis (transcribed verbatim from the Phase D `last_assistant_message`):

> "文件在每次 Read 之后立即被外部进程修改，导致 Edit 总是被拒绝……继续重试是无效的——根本原因是文件状态在 Read 后立刻发生变化，而非我的操作问题。" — "The file is being modified by an external process immediately after each Read, so Edit is consistently rejected. Continuing to retry is futile — the root cause is that the file state changes right after Read, not anything wrong with my operation."

The model arrived at this conclusion from purely observable signals (multiple Read+Edit attempts all failing the same way) without being told the hook's behavior. This is the strongest empirical confirmation: **Solution 1 ("Pre(Read) convert + Post(Read) restore") is dead in the real GBK scenario** — not just slow due to extra Read calls, but a hard infinite loop with no escape, because Pre and Post mutate the file in opposite directions and the baseline is locked between them.

The implication for `claude_encoding_guard` is final:

- **There is no way under current CC mechanics to produce a "perfect" encoding-guard plugin** — one that keeps the on-disk file in its original encoding at all times outside the brief tool execution window. Any hook that mutates the file outside of a tool-execution window either (i) trips stale-detection on subsequent Edit/Write (Solution 1's death mode), or (ii) reaches the disk only after Edit/Write has already failed the precondition (Solution 2's death mode).
- The plugin's current architecture — Pre(Read) convert + Post(Edit) restore + Stop restore-all — is the **only** viable design under CC's current validation order, at the cost of a "turn-length exposure window" during which the on-disk content is UTF-8 and visible to external observers (IDE watchers, build daemons, and most painfully: in-turn `Bash` compile commands that pick up the UTF-8 file under a system codepage that expects the original encoding).
- Closing the exposure window requires either an upstream CC change (running `PreToolUse` before precondition checks, or extending the baseline-update protocol to include `PostToolUse` hook mutations), or accepting the trade-off and documenting it as a known limitation.

### Read-first invariant and stale-detection

CC enforces "must have Read this file in the current session before Edit/Write-existing" as a hard rule at two layers:

1. **Read precondition (membership test)**: a session-scoped set of `readFiles`. `Edit`/`Write`-existing checks membership; missing → fail-fast (no hook). The Write tool prompt says "or the call will fail" (v2.1.120+ confirmed). Membership is also granted by `sed -n` / `cat` via `Bash` (CHANGELOG: "Changed Edit to work on files viewed via Bash with sed -n or cat, without requiring a separate Read call first").

2. **Stale-detection (content-hash baseline)**: at Read tool completion, CC records a content-level baseline (not just `mtime`). On the next `Edit`/`Write` of the same path, CC compares current content against the recorded baseline; if different → "file has been modified externally" error, model is told to re-Read. The CHANGELOG entry `Fixed false 'file modified' errors on Windows when cloud sync tools, antivirus scanners, or Git touch file timestamps without changing content` (v2.1.7) reverse-confirms the baseline is content-hash, not pure-`mtime`. Empirically: `touch` (mtime-only change) does not trigger; rewriting the file content does.

Known violations of the Read-first invariant / corner cases where it does not protect us:

- **Historical bugs (now fixed in CHANGELOG)**:
  - Session resume losing `readFiles` set: "Fixed bug with resuming where previously created files needed to be read again before writing".
  - `-p` mode `@`-mentioned files not counting as Read: "Fixed bug with `-p` mode where @-mentioned files needed to be read again before writing".
- **`Bash` bypass (uncloseable from a hook plugin)**: any `Bash` invocation that writes a file (`sed -i`, redirections like `>`/`>>`, in-Bash editors, etc.) flows through the `Bash` tool, not `Edit`/`Write`. The encoding hook on `Read|Edit|Write` is not fired. This is the dominant remaining case where an encoding plugin cannot help.
- **Explicit "don't re-Read" instructions to the model**: the model may go ahead and emit an `Edit` tool call without re-reading. CC's client-side validation fail-fasts at the `old_string` / stale-detection / Read-precondition layer; the file is unchanged, but the hook does not fire either. The user-visible failure is harmless to file integrity, but the plugin is blind to it.
- **Subagents (Plan/Explore/Task)**: not yet empirically verified — see "Open empirical gaps".
- **Across a `compact` boundary**: it is not yet verified whether `readFiles` membership persists across compact; if not, the first `Edit` post-compact would fail Read precondition until a re-Read.

Implication for an encoding-conversion plugin: any conversion that changes the on-disk content **between a Read and the next Edit on the same path** will trip stale-detection and force the model into a re-Read loop. This is a hard constraint on hook lifecycle design, not just a performance trade-off.

### Failure-mode safety: how the original file is protected

**(a) In-place convert + reverse — current main + chardet7-preview architecture**: Pre converts the original file in place to UTF-8, Post converts back. If the tool fails without modifying content, the round-trip is byte-identical. Safety relies on a mathematical equivalence (`encode(decode(bytes)) == bytes`). Pre's `convert_file` aborts on `UnicodeDecodeError` so a wrong-encoding detection is caught before any write. The original file's `mtime` changes twice per Read/Edit cycle, which is visible to external watchers (IDEs, build daemons).

**(b) Redirect to temp + sync-back on success — evaluated and shelved**: The original design idea was to have `Pre(Read)` create a fresh UTF-8 sibling and `updatedInput`-redirect the path; the original would never be touched on disk. `Post(Edit)` would sync back to the original only on tool success; on failure, the temp would be discarded and the original would remain literally byte-identical with unchanged `mtime`. Safety would have been *structural* (Pre never writes the original) rather than mathematical. External watchers would have seen at most one `mtime` bump per successful Edit/Write — a much shorter and tighter exposure window than (a).

This design was discovered to be **structurally infeasible** under current CC behavior — see "Client-side validation" and "Asymmetric `readFiles` tracking" above. Briefly: `Read(orig)` populates the `readFiles` tracker with the post-redirect path, but `Edit(orig)` / `Write(orig)` validates against the pre-redirect path. Because Edit/Write fail fast *before* `PreToolUse` is invoked, the hook cannot apply a symmetric redirect to rescue. This rules out the entire family of "don't touch the original on disk" hooks for any tool with a Read-precondition. The plugin therefore stays with architecture (a).

Under (a), a failed tool — `Edit` with non-matching `old_string`, `Write` with disk error, etc. — still leaves the original file's content intact, because (1) failures of the `old_string`, stale-detection, and Read-precondition kinds short-circuit at CC's client-side validation layer (no `PostToolUse` fires, no Edit/Write reaches disk), and (2) any in-tool failure that does reach disk is caught by `convert_file`'s atomic-write guard. The remaining gap in (a) is the small "exposure window" — on-disk content is UTF-8 between `Pre(Read)` and `Stop`, observable to external file watchers / build processes that act inside that window.

### Open empirical gaps

These have not been verified by experiment yet (deferred until needed):

- Whether `PostToolUse` fires when the tool itself errors out **after** passing client-side validation (e.g., `Edit` hits a disk error mid-write). The "client-side fail-fast" path is closed (no `Post` fires); the in-tool error path is still open. No failed-`Edit`/`Write` `tool_response` has been observed because every observed failure was a validation fail-fast that bypassed the hook entirely.
- `Pre`/`Post` behavior under subagents (Plan/Explore/Task) — whether hooks fire identically, attenuate, or are skipped for subagent-issued tool calls.
- Whether `compact` (auto-compact or user-triggered) preserves the `readFiles` membership across the boundary — if not, the first `Edit` after compact would fail Read precondition until a re-Read.
- Edit/Write `tool_response` shape for `replace_all: true` matching multiple sites — whether `structuredPatch` enumerates all hunks or merges them.
- Suspected fail-fast on `Read` for large files invoked **without** `offset`/`limit`: in one observed run a `pre_Read` log entry was produced for a ~54 KB file with no matching `post_Read`, after which the model retried with explicit `offset`/`limit` and succeeded (both `pre`/`post` produced). This pattern resembles the other client-side fail-fasts but has not been isolated as a single-variable test.
- **`PostToolUse.updatedToolOutput` interaction with subsequent client-side validations (v2.1.121+ feature, all tools)**: when a `PostToolUse(Read)` hook returns `updatedToolOutput` to replace `tool_response.file.content` (e.g. give Claude a UTF-8 decoded view of an on-disk GBK file), what content does CC use for the next `Edit`/`Write`'s client-side checks?
  - (A) the on-disk bytes (Claude-visible content is decoupled from validations) → Edit's `old_string` check against UTF-8 string vs disk GBK bytes will fail-fast, locking out Edit just like the Solution 2 dead end
  - (B) the post-hook Claude-visible content → Edit's `old_string` matches, and the entire "Read-path-without-disk-mutation" architecture becomes viable for an encoding-conversion plugin
  - This is the single unknown gating whether `claude_encoding_guard`'s "no exposure window on Read path" redesign is possible. PoC plan in `_backup/reviews/phase_e_poc_plan.md`. Related: CC Read tool's behavior when reading non-UTF-8 bytes (does the tool fail, return mojibake with U+FFFD, or pass bytes through?) — `PostToolUse` cannot rescue a `Read` that errors out before producing a normal `tool_response`.
- Whether `Read` actually invokes `PostToolUse` when the file content is not valid UTF-8 — required prerequisite for the above. If `Read` short-circuits (similar to Edit/Write fail-fast), `updatedToolOutput` for encoding rescue is structurally impossible.

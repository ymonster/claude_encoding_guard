# claude_encoding_guard (chardet 7.x experimental)

> [!WARNING]
> **This is the `chardet7-preview` experimental branch.** Stable releases live on [`main`](https://github.com/ymonster/claude_encoding_guard/tree/main). Use this branch only for testing the chardet 7.x migration or contributing feedback.

Preserve non-UTF-8 file encodings and line endings when Claude Code edits your files.

[中文文档](README_CN.md)

## What's different from `main`

| Aspect | `main` (5.x) | `chardet7-preview` |
|---|---|---|
| chardet version | `>=5,<6` (LGPL-2.1+) | `>=7,<8` (0BSD) |
| `binaryornot` dependency | required | **dropped** — chardet 7.x stage 5 binary detection replaces it |
| Confidence threshold | `>= 0.5` per-encoding | `>= 0.10` noise floor (7.x cosine-similarity scoring is geometric, not Bayesian) |
| ASCII handling | natural via chardet 5.x | explicit short-circuit (7.x reports ASCII as Windows-1252) |
| `STRUCTURAL_TRUSTED` bypass | yes (binaryornot has FPs on short CJK / mixed Cyrillic) | not needed (stage 5 has no comparable FPs) |
| Codec name conventions | `gbk` / `shift_jis` / `euc-kr` | `GB18030` / `cp932` / `CP949` (superset codecs, more permissive round-trip) |
| Test harness result (89 fixtures incl. Central European) | 79/89 (10 known 5.x misdetections) | 88/89 (one 50-byte cp1250 fixture conservatively skipped) |

See [TECHNICAL.md](TECHNICAL.md) → "chardet 7.x Migration Findings" for the empirical rationale and corner-case catalog.

## Install (experimental branch)

The Claude Code plugin marketplace doesn't support specifying a branch when installing from a GitHub source, so manual setup is required:

```bash
git clone -b chardet7-preview https://github.com/ymonster/claude_encoding_guard
```

Then point your project's `.claude/settings.local.json` at the cloned `hooks/encoding_guard.py`:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Read|Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py pre"
      }]
    }],
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py post"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py restore-all"
      }]
    }]
  }
}
```

## The Problem

Claude Code's Edit/Write tools always output UTF-8 with LF line endings. When editing files in GBK, Big5, Shift_JIS, or other legacy encodings, the original encoding is silently destroyed. CRLF line endings are also lost on Windows. The official response is ["not planned"](https://github.com/anthropics/claude-code/issues/12203).

Related issues: [#6485](https://github.com/anthropics/claude-code/issues/6485), [#7134](https://github.com/anthropics/claude-code/issues/7134), [#28523](https://github.com/anthropics/claude-code/issues/28523), [#38887](https://github.com/anthropics/claude-code/issues/38887).

## How It Works

```
PreToolUse (Read)                       PostToolUse (Edit/Write)
    │                                        │
    ├─ ASCII short-circuit                   ├─ read cached encoding + line ending
    ├─ detect encoding (chardet 7.x)         ├─ convert UTF-8 → original encoding
    │   ├─ stage 5 binary detection          ├─ normalize line endings to original
    │   ├─ era=MODERN_WEB|LEGACY_ISO         ├─ delete session cache
    │   └─ prefer_superset=False             └─ done
    ├─ detect line ending (CRLF/LF)
    ├─ convert original → UTF-8
    ├─ save session cache
    └─ Claude reads correct content
```

Conversion happens at **Read time**, before Claude Code loads the file into memory. This is critical — Claude Code interprets non-UTF-8 bytes as UTF-8, replacing invalid sequences with U+FFFD (irreversible). By converting to UTF-8 first, Claude sees correct content and edits cleanly.

## Features

- **Encoding preservation**: GBK, GB2312, GB18030, Big5, Big5-HKSCS, EUC-TW, Shift_JIS, EUC-JP, ISO-2022-JP, EUC-KR, Windows-1252, Windows-1251 (Cyrillic), Windows-1250 (Central European), ISO-8859-1
- **Line ending preservation**: CRLF restored after Claude Code converts to LF
- **Binary file protection**: chardet 7.x's built-in stage 5 binary detection (always on)
- **Session isolation**: Multiple Claude Code sessions won't interfere with each other
- **Zero configuration**: Works out of the box

### Verify

After install, Read any non-UTF-8 file — Chinese characters should display correctly instead of garbled text.

## Design Decisions

- **Read-time conversion**: Claude Code v2.1.90+ silently accepts hook-modified files without re-reading content. Edit-time conversion results in U+FFFD corruption. Converting at Read time ensures Claude's first in-memory view is correct.
- **`uv run --script`**: Plain `uv run` triggers project sync which closes the stdin pipe on Windows. `--script` skips project discovery.
- **chardet 7.x**: Confidence is recalibrated to cosine-similarity scoring (no longer "how confident I am" but "how hard the detection method was"). GB18030 confidence is geometrically pinned to 0.14–0.25 regardless of input length, so we use a 0.10 noise floor + allowlist filter rather than a high confidence threshold. See [TECHNICAL.md](TECHNICAL.md) for details.
- **Binary detection**: chardet 7.x's pipeline stage 5 returns `encoding=None` for non-text inputs at confidence 0.95–1.00, replacing the binaryornot decision tree used on `main`. No false positives observed across the test fixture set.
- **Encoding aliases**: GB2312 → GBK (byte-compatible superset), ISO-8859-1 → Windows-1252 (industry practice).
- **Session-isolated cache**: `<tmpdir>/.cc_encoding_cache/<session_id>/` — no cross-session interference. Hooks act strictly on their own session's records; stale **empty** session dirs (>24h) are auto-removed, record-bearing dirs never are (see Crash Recovery).

## Known Limitations

- **Recommended: use under version control as a fallback for binary misedits.** We do our best to avoid editing binary files through multiple defensive layers (and in practice Claude Code rarely tries to edit a binary file anyway), but stray cases are always possible. We considered using local `git stash` as an automatic fallback but didn't want to interfere with the user's own git workflow (polluting stash list, reflog, etc.). So **strongly recommended: use this plugin under git or another version control tool**, so any misidentification can be safely rolled back.

- **Windows-1252 stale cache edge case.** If cache deletion fails (e.g., antivirus lock) and the file's original Windows-1252 bytes happen to be valid UTF-8, the stale cache won't self-heal. This requires two unlikely conditions to coincide and doesn't affect CJK encodings (GBK/Big5/Shift_JIS bytes are not valid UTF-8).

- **Mixed line endings.** Files with both CRLF and LF are normalized to the dominant style.

- **Claude Code assumes absolute paths.** This plugin relies on Claude Code providing absolute file paths in hook stdin JSON, which is the observed behavior. Symbolic links or junctions pointing to the same file may produce different cache keys.

- **Concurrent Claude Code instances on the same file.** If two CC instances edit or read the same file at the same time, one session's Stop restore can overwrite another session's pending Edit. Session-isolated caches prevent most cross-contamination, but a race window remains. Concurrent use is rare enough that this trade-off is accepted in exchange for Read-without-Edit recovery. The same applies to `/encoding-guard:scan`: it runs only when you ask, and it cannot tell whether another window's session is still alive — close other Claude Code instances working in the same project before using it.

## Crash Recovery

If Claude Code is killed mid-session (crash, terminal closed), files can be left in their temporary UTF-8 state. Four layers deal with this:

| Layer | When | What it does |
|---|---|---|
| `Stop` restore-all | end of every turn | restores files read but never edited this turn |
| `SessionEnd` restore-all | session exits | backstop — also covers turns interrupted with Esc |
| `SessionStart(resume)` restore-all | resuming a session | heals crash residue before anything else runs in the resumed session |
| `/encoding-guard:scan` | you run it | finds records left by crashed sessions you never resumed |

The first three are built in — resuming a crashed session (CLI `--resume` / `-c` or the TUI `/resume` picker all keep the session ID) is enough to recover. But if a crashed session is never resumed, files may be left in their UTF-8 state and cache records may be left behind. For the current directory, run `/encoding-guard:scan` to find them.

Every conversion and restore is written to a log, including the hook event that triggered it. The log lives at `<tmpdir>/.cc_encoding_cache/encoding_guard.log`, where `<tmpdir>` is the system temp directory: usually `C:\Users\<name>\AppData\Local\Temp` (`%TEMP%`) on Windows, `/tmp` on Linux/macOS. To confirm the location on your machine, run `python -c "import tempfile; print(tempfile.gettempdir())"`.

## The `/encoding-guard:scan` Command

This is currently the only command the plugin provides:

```
/encoding-guard:scan
```

It inspects only the plugin's own cache records (it never crawls your project files), finding files **under the current directory** that were left in their UTF-8-converted state, plus leftover cache records, and reports or asks. Records fall into two classes:

- **Restorable** — the file still exists and is still UTF-8; the record can convert it back to its original encoding
- **Obsolete** — the file was deleted, is already back in its original encoding, or the record itself is corrupt. Nothing can be done with these; they are cleaned up silently (only the cache record is deleted)

If everything is clean it says so and stops. When restorable files are found, it shows the list (with each record's age) and asks:

- **Restore all** — convert every file back to its original encoding
- **Discard all records** — keep the files as UTF-8 and delete the records (the plugin's cache records). Note: the record is the only place that remembers what encoding a file originally had — once deleted, restore is permanently impossible
- **Pick per file** — two checklists side by side: **Restore** (check what to restore) and **Discard** (check what to give up). Checked in neither = left alone for this run; the record stays and a later run will ask again. Checked in both = treated as restore
- **Not now** — touch nothing

Restore results are reported file by file. A failed restore (typically the file was edited while UTF-8 and now holds characters the original encoding cannot represent — e.g. an emoji in a GBK file) shows the reason and keeps the record — the remaining options are discarding the record, or fixing the content by hand and trying again.

Safety rules: records belonging to the session you're in are skipped automatically (its own hooks handle them). Each record is shown with its age — a hint for spotting another session that may still be working on files under the same path.

The command is driven by a few script modes (`scan` / `scan-current` / `restore-current` / `prune-current`) — internal implementation, documented in [TECHNICAL.md](TECHNICAL.md) for debugging and development.

## Advanced Configuration

To limit the hook to specific file extensions, use the `if` field in your project's `.claude/settings.local.json` (Claude Code v2.1.85+):

```json
{
  "matcher": "Read|Edit|Write",
  "if": "tool_input.file_path MATCHES '\\.(?:h|c|cpp|txt|xml|csv|ini)$'",
  "hooks": [...]
}
```

Without `if`, the hook runs on all file operations. The performance cost is minimal — chardet skips UTF-8/ASCII files almost instantly.

## Technical Details

See [TECHNICAL.md](TECHNICAL.md) for implementation details including platform quirks (Windows stdin, codepage, CRLF), chardet version sensitivity, binary detection rationale, cache design, and the evolution of the conversion strategy.

## Acknowledgments

Thanks to [@lbresler](https://github.com/lbresler) for [#1](https://github.com/ymonster/claude_encoding_guard/pull/1) (alias-dash mismatch fix for ISO-8859-1 / GB2312) and [#2](https://github.com/ymonster/claude_encoding_guard/pull/2) (`handle_restore_all` + Stop hook recovery for Read-without-Edit).

Thanks to [@Pacman766](https://github.com/Pacman766) for [#3](https://github.com/ymonster/claude_encoding_guard/pull/3) (Windows-1251 / CP1251 support for Cyrillic text).

Thanks to [@albert-polak](https://github.com/albert-polak) for [#4](https://github.com/ymonster/claude_encoding_guard/pull/4) (Windows-1250 / CP1250 support for Central European text).

## License

MIT

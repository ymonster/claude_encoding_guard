# claude_encoding_guard

Preserve non-UTF-8 file encodings and line endings when Claude Code edits your files.

[中文文档](README_CN.md)

## The Problem

Claude Code's Edit/Write tools always output UTF-8 with LF line endings. When editing files in GBK, Big5, Shift_JIS, or other legacy encodings, the original encoding is silently destroyed. CRLF line endings are also lost on Windows. The official response is ["not planned"](https://github.com/anthropics/claude-code/issues/12203).

Related issues: [#6485](https://github.com/anthropics/claude-code/issues/6485), [#7134](https://github.com/anthropics/claude-code/issues/7134), [#28523](https://github.com/anthropics/claude-code/issues/28523), [#38887](https://github.com/anthropics/claude-code/issues/38887).

## How It Works

```
PreToolUse (Read)                       PostToolUse (Edit/Write)
    │                                        │
    ├─ binary check (binaryornot)            ├─ read cached encoding + line ending
    ├─ detect encoding (chardet 5.x)         ├─ convert UTF-8 → original encoding
    ├─ detect line ending (CRLF/LF)          ├─ normalize line endings to original
    ├─ convert original → UTF-8              ├─ delete session cache
    ├─ save session cache                    └─ done
    └─ Claude reads correct content
```

Conversion happens at **Read time**, before Claude Code loads the file into memory. This is critical — Claude Code interprets non-UTF-8 bytes as UTF-8, replacing invalid sequences with U+FFFD (irreversible). By converting to UTF-8 first, Claude sees correct content and edits cleanly.

## Features

- **Encoding preservation**: GBK, GB2312, GB18030, Big5, Big5-HKSCS, EUC-TW, Shift_JIS, EUC-JP, ISO-2022-JP, EUC-KR, Windows-1252, Windows-1251 (Cyrillic), ISO-8859-1
- **Line ending preservation**: CRLF restored after Claude Code converts to LF
- **Binary file protection**: Prevents chardet from misidentifying binary files (always on, not configurable)
- **Session isolation**: Multiple Claude Code sessions won't interfere with each other
- **Zero configuration**: Works out of the box

## Install

### Prerequisites

- [uv](https://docs.astral.sh/uv/) — required. Handles Python dependencies automatically via PEP 723 inline metadata. No manual `pip install` needed.

### As Plugin

```
/plugin marketplace add ymonster/claude_encoding_guard
/plugin install encoding-guard
```

### Verify

After installation, Read any non-UTF-8 file — Chinese characters should display correctly instead of garbled text.

### Experimental: chardet 7.x branch

A migration to chardet 7.x is being prototyped on the [`chardet7-preview`](https://github.com/ymonster/claude_encoding_guard/tree/chardet7-preview) branch. It drops the `binaryornot` dependency (chardet 7.x has built-in binary detection) and uses 0BSD-licensed chardet (vs LGPL on 5.x). Not recommended for daily use yet — see the branch's README for status and install instructions.

## Design Decisions

- **Read-time conversion**: Claude Code v2.1.90+ silently accepts hook-modified files without re-reading content. Edit-time conversion results in U+FFFD corruption. Converting at Read time ensures Claude's first in-memory view is correct.
- **`uv run --script`**: Plain `uv run` triggers project sync which closes the stdin pipe on Windows. `--script` skips project discovery.
- **chardet 5.x**: Version 7.x reduced CJK detection confidence from 0.99 to 0.40 — below the safety threshold. Pinned via PEP 723 in an isolated environment.
- **Binary detection**: chardet misidentifies some binary files as Windows-1252 (confidence 0.73). [binaryornot](https://github.com/binaryornot/binaryornot) filters these out. Always on, not configurable.
- **Encoding aliases**: GB2312 → GBK (byte-compatible superset), ISO-8859-1 → Windows-1252 (industry practice).
- **Session-isolated cache**: `<tmpdir>/.cc_encoding_cache/<session_id>/` — no cross-session interference. Hooks act strictly on their own session's records; stale **empty** session dirs (>24h) are auto-removed, record-bearing dirs never are (see Crash Recovery).

## Known Limitations

- **Recommended: use under version control as a fallback for binary misedits.** We do our best to avoid editing binary files through multiple defensive layers (and in practice Claude Code rarely tries to edit a binary file anyway), but stray cases are always possible. We considered using local `git stash` as an automatic fallback but didn't want to interfere with the user's own git workflow (polluting stash list, reflog, etc.). So **strongly recommended: use this plugin under git or another version control tool**, so any misidentification can be safely rolled back.

- **Windows-1252 stale cache edge case.** If cache deletion fails (e.g., antivirus lock) and the file's original Windows-1252 bytes happen to be valid UTF-8, the stale cache won't self-heal. This requires two unlikely conditions to coincide and doesn't affect CJK encodings (GBK/Big5/Shift_JIS bytes are not valid UTF-8).

- **Mixed line endings.** Files with both CRLF and LF are normalized to the dominant style.

- **Claude Code assumes absolute paths.** This plugin relies on Claude Code providing absolute file paths in hook stdin JSON, which is the observed behavior. Symbolic links or junctions pointing to the same file may produce different cache keys.

- **Concurrent Claude Code instances on the same file.** If two CC instances edit or read the same file at the same time, one session's Stop restore can overwrite another session's pending Edit. Session-isolated caches prevent most cross-contamination, but a race window remains. Concurrent use is rare enough that this trade-off is accepted in exchange for Read-without-Edit recovery. The recovery commands additionally skip records written within the last hour unless `--all` is passed.

## Crash Recovery

If Claude Code is killed mid-session (crash, terminal closed), files can be left in their temporary UTF-8 state. Four layers deal with this:

| Layer | When | What it does |
|---|---|---|
| `Stop` restore-all | end of every turn | restores files read but never edited this turn |
| `SessionEnd` restore-all | session exits | backstop — also covers turns interrupted with Esc |
| `SessionStart(resume)` restore-all | resuming a session | heals crash residue before anything else runs in the resumed session |
| `/encoding-guard:scan` | you run it | finds records left by crashed sessions you never resumed |

The first three are automatic — resuming a crashed session (CLI `--resume` / `-c` or the TUI `/resume` picker all keep the session ID) is enough to self-heal. The only case needing you: a crashed session you never resume. Handle it with the recovery commands below — interactively via `/encoding-guard:scan`, or from a terminal.

Every conversion and restore is also appended to a forensic log at `<tmpdir>/.cc_encoding_cache/encoding_guard.log`, including the hook event that triggered it — useful because operations running in process-shutdown windows (e.g. `SessionEnd` after the terminal is closed) leave no other trace.

## Recovery Commands

All recovery commands work off the plugin's own cache records (`<tmpdir>/.cc_encoding_cache/<session_id>/*.json`) — they never crawl your project files. Scope at a glance:

| Command | Scope | Modifies |
|---|---|---|
| `scan` | **every** record from **every** session, any path | nothing (read-only report) |
| `scan-current` | only records whose target file lies under the **current directory** | nothing (read-only report) |
| `restore-current` | restorable records under the **current directory** | converts files back to their original encoding, consumes their records |
| `prune-current` | records under the **current directory** | deletes defunct records; with `--discard` also deletes restorable ones (keep files as UTF-8) |

```bash
uv run --script <plugin>/hooks/encoding_guard.py scan [--json]
uv run --script <plugin>/hooks/encoding_guard.py scan-current [--json]
uv run --script <plugin>/hooks/encoding_guard.py restore-current [--path FILE]... [--all]
uv run --script <plugin>/hooks/encoding_guard.py prune-current [--path FILE]... [--all] [--discard]
```

- Records are classified by on-disk facts: **restorable** (target exists and is valid UTF-8) or **defunct** (record invalid, target gone, or target no longer UTF-8 — i.e. already restored or rewritten). Only provably defunct records are ever auto-deleted.
- There is deliberately **no global restore** — restoring is a per-project decision; `cd` into each project and run `restore-current` there. The global `scan` exists for a read-only overview.
- Guards on the mutating commands: records of the running session are never touched (identified via `CLAUDE_CODE_SESSION_ID`), and records written within the last hour are skipped unless `--all` is passed — a fresh record most likely belongs to a live session in another window.
- `/encoding-guard:scan` inside Claude Code runs `scan-current` under the hood and walks you through the choices interactively (restore all / discard / pick per file).

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

## License

MIT

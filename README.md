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

## Design Decisions

- **Read-time conversion**: Claude Code v2.1.90+ silently accepts hook-modified files without re-reading content. Edit-time conversion results in U+FFFD corruption. Converting at Read time ensures Claude's first in-memory view is correct.
- **`uv run --script`**: Plain `uv run` triggers project sync which closes the stdin pipe on Windows. `--script` skips project discovery.
- **chardet 5.x**: Version 7.x reduced CJK detection confidence from 0.99 to 0.40 — below the safety threshold. Pinned via PEP 723 in an isolated environment.
- **Binary detection**: chardet misidentifies some binary files as Windows-1252 (confidence 0.73). [binaryornot](https://github.com/binaryornot/binaryornot) filters these out. Always on, not configurable.
- **Encoding aliases**: GB2312 → GBK (byte-compatible superset), ISO-8859-1 → Windows-1252 (industry practice).
- **Session-isolated cache**: `<tmpdir>/.cc_encoding_cache/<session_id>/` — no cross-session interference. Stale sessions (>24h) auto-cleaned.

## Known Limitations

- **Windows-1252 stale cache edge case.** If cache deletion fails (e.g., antivirus lock) and the file's original Windows-1252 bytes happen to be valid UTF-8, the stale cache won't self-heal. This requires two unlikely conditions to coincide and doesn't affect CJK encodings (GBK/Big5/Shift_JIS bytes are not valid UTF-8).

- **Mixed line endings.** Files with both CRLF and LF are normalized to the dominant style.

- **Claude Code assumes absolute paths.** This plugin relies on Claude Code providing absolute file paths in hook stdin JSON, which is the observed behavior. Symbolic links or junctions pointing to the same file may produce different cache keys.

- **Concurrent Claude Code instances on the same file.** If two CC instances edit or read the same file at the same time, one session's Stop restore can overwrite another session's pending Edit. Session-isolated caches prevent most cross-contamination, but a race window remains. Concurrent use is rare enough that this trade-off is accepted in exchange for Read-without-Edit recovery.

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

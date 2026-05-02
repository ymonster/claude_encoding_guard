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
| Test harness result | 73/73 fixtures PASS | 73/73 fixtures PASS |

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
    │   ├─ era=MODERN_WEB candidates         ├─ delete session cache
    │   └─ prefer_superset=True              └─ done
    ├─ detect line ending (CRLF/LF)
    ├─ convert original → UTF-8
    ├─ save session cache
    └─ Claude reads correct content
```

Conversion happens at **Read time**, before Claude Code loads the file into memory. This is critical — Claude Code interprets non-UTF-8 bytes as UTF-8, replacing invalid sequences with U+FFFD (irreversible). By converting to UTF-8 first, Claude sees correct content and edits cleanly.

## Features

- **Encoding preservation**: GBK, GB2312, GB18030, Big5, Big5-HKSCS, EUC-TW, Shift_JIS, EUC-JP, ISO-2022-JP, EUC-KR, Windows-1252, Windows-1251 (Cyrillic), ISO-8859-1
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

# Native Share ZIP Import — cross-repo contract (v1, 2026-11-02)

Scope: P0 of「Local Bridge × WeChat Provider × Companion 微信原生分享集成」.
Enables: user-selected WeChat merged-forward ZIP → WeChat Provider parse → Companion preview →
user selects range → Agent suggestion (still user-triggered).

Non-goals in v1: macOS Share Extension (P1), batch merge (P1.5), structured real-time read (P2),
automatic save, automatic model call, any WeChat modification or relogin.

## Repos / branches (this round)

| Repo | Branch | Base |
|---|---|---|
| `eaopen/wechat-provider` | `feat/native-share-import` | `dev` (includes the local-excerpt tools Companion already calls) |
| `CoBeing/companion` | `feat/native-share-import` | `main` |
| `eaopen/local-bridge` | `docs/native-share-contract` | `dev` (docs only; **no P0 code changes**) |

Each repo keeps its existing checkout untouched: development happens on the feature branch only, and
no protected local work is overwritten.

## Data flow

```
Companion UI (user picks / drops .zip)
  → Companion Rust stages bytes into its private app-data dir (0600), keeps path in memory only
  → Companion Rust mints a one-time grant file in Provider's grant dir (path binding, 0600, TTL)
  → Companion Rust → Local Bridge mcp_provider::call("wechat", "wechat_native_export_preview", ...)
  → Provider IPC daemon / stdio server: validates grant, deletes grant, opens ZIP, parses TXT
  → returns bounded preview JSON (no persistence of chat history)
  → Companion shows counts/participants/messages; user checks a range (≤ 7800 UTF-8 bytes)
  → existing desktop_chat_run → Agent → editable/copyable drafts (no sending)
Optional, only when user clicks "保存整份聊天到本机索引":
  → second grant → wechat_native_export_save → Provider existing SQLite (source = native_share)
```

The **only** file path that the Provider ever opens comes from a grant file written by a same-user
native process after an explicit user gesture. No MCP tool accepts a filesystem path or file bytes,
so neither a web page nor an Agent can use the provider to read arbitrary local files.

## Provider data directory

`default_db_path().parent()` (e.g. `~/Library/Application Support/wechat-provider`).
Grant dir: `<parent>/native-export-grants/`, created mode `0700`, never symlinked.

## Grant file (written by Companion, consumed by Provider)

- File name: `<token>.json` where token = 32 lowercase hex chars.
- Written mode `0600` (no group/other bits), owner = current uid, created “now”.
- Content (single line JSON, ≤ 2048 bytes):

```json
{"version":1,"token":"<32 hex>","path":"/absolute/path/to/chat.zip","purpose":"preview"}
```

`purpose` ∈ `preview` | `save`. Provider MUST validate and then unlink the grant before parsing
(one-shot, even on failure; Companion re-mints for retry).

Provider validation (fail closed, stable error codes below):
- filename is 32 hex + `.json`; `lstat` regular file (no symlink), same uid, `mode & 0o077 == 0`,
  `mtime` within 600 s;
- JSON version 1, `token` matches filename, `purpose` matches the tool being called;
- `path` is absolute, has no NUL, ends with `.zip` (case-insensitive), `lstat` is a regular file
  (no symlink), same uid, size ≤ `max_archive_bytes`.
- Any failure → delete grant (if safe) then raise `ValueError("native_export_grant_invalid")`.

## ZIP validation + parsing (WeChat Provider only)

Use Python stdlib `zipfile` on the validated path; do **not** add a ZIP dependency.

Hard limits (all must be enforced before parsing; fail closed with `native_export_limit_exceeded`
or `native_export_archive_invalid`):
- archive size ≤ 25 165 824 bytes (24 MiB), entry count ≤ 2 000, total uncompressed ≤ 268 435 456
  bytes (256 MiB), single entry uncompressed ≤ 33 554 432 bytes (32 MiB),
  compression ratio (total uncompressed / compressed) ≤ 200, max path length ≤ 512 bytes.
- Reject encrypted entries (`flag_bits & 0x1`), compression methods other than 0 and 8, ZIP64,
  multi-disk, duplicate names after NFC + lowercase normalization, absolute paths,
  `..`/`.`/empty components, backslashes, `:` in any component, control characters,
  symlink/hardlink/other non-regular file type bits, directory entries with non-zero size.
- Read every entry with a bounded read; let `zipfile` verify CRC (bad CRC → archive invalid).
- On any invalid archive the **user's original file is untouched** (never moved/renamed/deleted).

TXT discovery:
- Prefer the entry whose basename is exactly `聊天记录.txt`.
- Otherwise pick the `.txt` entry (≤ 16 MiB uncompressed) that yields the most parsed records.
- No parseable TXT → `native_export_parse_failed` (preview returns error; original file kept).

Record format (WeChat macOS merged-forward export, same as WeChatBridge reference):
- File may start with UTF-8 BOM; CRLF normalized to LF.
- Each record: `·<sender>` line, then `yyyy年M月d日 HH:mm` line, then body lines until the next
  `·`+date pair. First record must start at offset 0 after BOM/trim → otherwise parse fails.
- Multi-line text preserved verbatim (no collapsing of internal blank lines, trailing whitespace
  trimmed only at record boundaries).
- Sender is a display name, never a stable WeChat identity.
- Unparsed trailing bytes / malformed blocks are reported, never guessed.

Timezone (no silent conversion):
- The TXT timestamp has no zone. `wechat_native_export_preview(..., timezone_offset_minutes)`:
  - explicit int in `-840..=840`: records get `sent_at` = ISO-8601 **with explicit offset**
    (e.g. `2026-09-05T08:05:00+08:00`) and `sent_at_local` = `2026-09-05T08:05`;
    `export.timezone = {"applied": true, "offset_minutes": <int>}`.
  - `null`/absent: `sent_at = null`, `sent_at_local` kept, `timezone.applied = false`, plus a
    warning string. Never assume the system zone inside the provider.
- Companion sends `-new Date().getTimezoneOffset()` from the UI at preview time.

Attachments:
- Every non-directory, non-TXT entry is an attachment: `{name, kind, bytes}` where
  `kind` ∈ `image|video|audio|document|archive|other` from the extension only (no magic guessing).
- Cap `attachments` returned to 50 entries (report `attachment_count` fully).
- Message → attachment relation: only exact match of a message body line to the entry name or
  basename adds that attachment name to `attachment_refs`; otherwise no relation (never guess).
- Attachment bytes are **not** copied into the Provider DB (P0); metadata only.

## Provider MCP tools (exact names)

Tools are `<provider>_<tool>` from Companion (`wechat_native_export_preview`, etc.).

### `wechat_native_export_preview`
Args: `grant_token: str`, `timezone_offset_minutes: int | null = null`,
`offset: int = 0`, `limit: int = 200`.

Limits: `limit` 1..200, `offset` 0..100000, `offset+limit` ≤ `max_messages` (20000).

Returns (JSON object, no `error` key on success):

```json
{
  "ok": true,
  "source": "native_share",
  "export": {
    "contract_version": 1,
    "session_id": "native_share:<32 hex>",
    "fingerprint": "sha256:<64 hex>",
    "label": "<archive TXT/entry label, ≤120 chars, display only>",
    "participants": ["..."],
    "participant_count": 3,
    "message_count": 42,
    "attachment_count": 2,
    "attachments": [{"name":"images/photo.png","kind":"image","bytes":32}],
    "time_range_local": {"start":"2026-09-05T08:05","end":"2026-09-05T08:06"},
    "timezone": {"applied": true, "offset_minutes": 480},
    "coverage": {"transcript_found": true, "format": "native_txt",
                 "parsed_records": 42, "unparsed_tail_bytes": 0, "complete": true},
    "already_saved": false
  },
  "messages": [
    {"index":0,"source_id":"native:<32 hex>","sender":"甲",
     "sent_at":"2026-09-05T08:05:00+08:00","sent_at_local":"2026-09-05T08:05",
     "text":"你好\n第二行","attachment_refs":["images/photo.png"]}
  ],
  "window": {"offset":0,"limit":200,"returned":42,"has_more":false,"next_offset":null},
  "warnings": []
}
```

Identity rules:
- `fingerprint` = sha256 over the normalized parsed records (sender + local timestamp literal +
  text, `\x1e`-separated). Same ZIP bytes re-imported → same fingerprint → idempotent.
- `session_id` = `native_share:` + first 32 hex of fingerprint. It is a **share-scoped** id, NOT a
  WeChat stable conversation id. Distinct groups with the same display name never merge, and two
  different exports of the same chat are two different sessions (no silent fusion).
- `source_id` = `native:` + 32 hex of sha256(sender + `\x1e` + local timestamp + `\x1e` + text +
  `\x1e` + occurrence-index-among-identical-tuples). Re-import of the same ZIP yields identical
  ids → duplicate counting instead of duplication.

Bounded payload: `messages` total UTF-8 text ≤ 262 144 bytes per response; if a single message
exceeds 32 000 UTF-8 bytes the preview fails with `native_export_limit_exceeded` (no silent
truncation). When the window exceeds the byte cap, return fewer messages with
`window.has_more = true` and `window.next_offset` set.

### `wechat_native_export_save`
Args: `grant_token: str`, `expected_fingerprint: str` (`sha256:<hex>`).

- Re-parse the granted ZIP; require `fingerprint == expected_fingerprint`
  (else `native_export_changed`, nothing imported — TOCTOU guard).
- Import through the **existing** `Store.import_messages` transaction (no second index DB):
  rows `{chat_id: session_id, source_id, sender, sent_at, text, source: "native_share"}`.
- Insert/update one metadata row in new table `native_exports` (same SQLite file):

```sql
CREATE TABLE IF NOT EXISTS native_exports (
  session_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  message_count INTEGER NOT NULL,
  participant_count INTEGER NOT NULL,
  attachment_count INTEGER NOT NULL,
  timezone_offset_minutes INTEGER,
  source_zip_name TEXT,
  imported_at TEXT NOT NULL
);
```

- Idempotent: second save of the same fingerprint → `imported: 0`, `duplicates: <n>`,
  `already_imported: true` (not an error).
- Returns:

```json
{"ok":true,"saved":true,"source":"native_share","session_id":"native_share:<32 hex>",
 "fingerprint":"sha256:<64 hex>","imported":42,"duplicates":0,"already_imported":false,
 "message_count":42,"attachment_count":2,"live_read":false,"complete":false}
```

### `wechat_native_export_archive` (reserved, NOT exposed in P0)
Documented internal contract only: would write Markdown/attachments to a user-chosen directory
(P1.5). `Service.archive_native_export` exists and raises/reports `not_implemented`; no MCP tool.

### Error codes (raised as `ValueError("<code>")`, no private paths or chat text)
`native_export_grant_invalid`, `native_export_archive_invalid`, `native_export_limit_exceeded`,
`native_export_parse_failed`, `native_export_changed`, `native_export_unsupported`.
(Existing codes `invalid_argument`, `reader_unavailable` unchanged.)

## Provider status additions

```json
"capabilities": {"native_export_import": true, "native_export_contract_version": 1, ...},
"native_export": {
  "contract_version": 1,
  "enabled": true,
  "grant_dir": "/Users/<user>/Library/Application Support/wechat-provider/native-export-grants",
  "grant_ttl_seconds": 600,
  "max_archive_bytes": 25165824,
  "max_messages": 20000,
  "max_preview_messages": 200,
  "max_preview_bytes": 262144,
  "timezone_offset_required_for_absolute_time": true
}
```

`Store.list_chats(local_index)` rows for native sessions: `title` = `native_exports.label`;
`source_kind` = `"native_share"`; `native_share_only` = true when all rows of that chat are
`native_share`. Never reported as `structured_db` or live history.

IPC whitelist (`local_ipc.METHODS`) must include `native_export_preview`, `native_export_save`
(and keep `save_excerpt`, `forget_saved_excerpts`).

CLI (developer/acceptance convenience, same validation):
`wechat-provider preview-native-export <grant_token> [--timezone-offset-minutes N]`,
`wechat-provider save-native-export <grant_token> --fingerprint <sha256:...>`,
`wechat-provider grant-native-export <path.zip> --purpose preview|save --grant-dir <dir>`
(the last one only for tests/acceptance; product path is Companion minting the grant).

## Companion ↔ Provider mapping (Companion Rust)

`native_export.rs`:
- `desktop_chat_native_export_stage` (raw IPC body = ZIP bytes, header
  `x-companion-export-name` = percent-encoded UTF-8 file name, optional):
  caps 24 MiB, writes `<app_local_data_dir>/native-imports/<uuid>.zip` mode 0600, replaces any
  previous staging file, returns `{staged:true, bytes, name}`.
  Errors: `文件过大…`, `请选择微信合并转发导出的 ZIP 文件`.
- `desktop_chat_native_export_preview(offset, limit, timezone_offset_minutes)`:
  - requires staged file; requires provider `wechat` registered and
    `native_export_contract_version == 1` (status via `mcp_provider::call`);
  - validates `grant_dir` from status: absolute, no `..`, ends with `native-export-grants`,
    under `$HOME`, existing directory;
  - mints grant (0600) then calls `wechat_native_export_preview`; validates response:
    `source == "native_share"`, messages ≤ requested limit, `export.session_id` prefix,
    `fingerprint` shape, message fields types, total text bytes ≤ 262144;
    returns the validated object unchanged (minus nothing) to the UI.
- `desktop_chat_native_export_save(expected_fingerprint)`: re-mints grant with purpose `save`,
  calls `wechat_native_export_save`, validates `saved == true`, `source == "native_share"`,
  numeric `imported`/`duplicates`, `fingerprint` echo matches.
- `desktop_chat_native_export_clear`: delete staged file + in-memory session state.
- Pure helpers (unit-tested without Tauri): `stage_bytes`, `mint_grant`, `validate_preview`,
  `grant_dir_ok`, error mapping (`provider error code → actionable Chinese, no raw text`).
- No new crates. No send, no automatic save/model call. Chat text never written to logs.

`chat_copilot.rs`: add `native_share` to `CONTEXT_SOURCE_KINDS` so
`context_source: "wechat_native_share"` is accepted as a bounded data label.

`chat_source.rs::status_view`: pass through
`native_export_supported` (bool), `native_export_grant_dir` (string|null),
`native_export_max_archive_bytes` / `_max_preview_messages` / `_max_preview_bytes` (numbers|null).

## Companion UI (ui/native-export.mjs + index.html + app.css)

Placement: directly under the existing「导入微信聊天文字」block, before the existing
「选择本机已有的聊天片段」details.

Elements (ids fixed for tests):
- `chat-native-import` (button), `chat-native-file` (hidden `input[type=file]`
  accept `.zip,application/zip`), `chat-native-drop` (drop zone, also clickable),
  `chat-native-status` (role=status).
- Preview block `chat-native-preview` (hidden until preview):
  `chat-native-summary` (label · participants · messages · attachments · time range · source note),
  `chat-native-participants`, `chat-native-messages` (checkbox list),
  `chat-native-more` (load next window; hidden when no `has_more`),
  `chat-native-bytes` (selected UTF-8 byte count vs 7800),
  `chat-native-select-recent` / `chat-native-select-all` / `chat-native-select-none`,
  `chat-native-apply` (放入对话), `chat-native-save` (optional 保存整份聊天到本机索引, inside
  a `<details>`), `chat-native-clear`.

Behavior:
- File selection/drop only. Reject non-`.zip` / > 24 MiB with explicit text (no truncation).
  `readFile` injectable for node tests (`async file => new Uint8Array(await file.arrayBuffer())`),
  default implementation used in the app.
- Stage → preview page 0 → render. `加载更多消息` appends the next window (still in memory only).
- Participant/message/attachment counts come only from provider numbers; never fabricate
  message counts for unparsed content (`coverage.complete === false` is surfaced).
- Selection: bytes = UTF-8 bytes of `sender：text` lines joined by `\n`; over 7800 → apply
  disabled + explicit "请减少勾选的消息，不会自动截断" message (never change the 7800 limit).
- Apply calls `onUse({text, context_source:"wechat_native_share", thread_label: label})`.
  No model call, no save, no send at this step.
- Save button explicitly confirms provider path (`desktop_chat_native_export_save`) and reports
  imported/duplicates; idempotent re-save message when `already_imported`.
- Cancelled picker = no-op (no error, no clearing of current preview/textarea).
- Provider unavailable / not registered / contract mismatch → actionable Chinese message and
  keep manual paste / clipboard path working.
- Every new file staged or applied marks existing AI drafts stale: `mountChat` prepends/refreshes
  a single banner `chat-results-stale` ("右侧建议基于上一轮片段，尚未按新导入内容更新") and never
  silently presents old drafts as updated.
- Text/chat content is untrusted data: never HTML-injected (use textContent only).

## Acceptance tests (P0)

Provider (pytest): ZIP safety matrix (traversal, symlink, bomb/ratio, encrypted, bad CRC,
unsupported method, duplicate names, size/count limits), parser (single/multi, CJK/EN/emoji,
multiline, duplicate identical messages, same display name different groups), timezone
(explicit offset, missing offset), attachments, save idempotency, TOCTOU fingerprint mismatch,
grant expiry/reuse mode, list_chats provenance, IPC whitelist, MCP stdio integration.

Companion Rust: command validation, staging caps/cleanup, grant minting (mode/path/expiry),
preview receipt validation (source/fingerprint/limits/bytes), error mapping, status passthrough,
`wechat_native_share` context source accepted.

Companion UI (node --test): picker cancel, non-zip, oversize, preview render counts, pagination,
selection bytes over/under limit (no truncation), apply fills context, save receipt, stale banner,
provider/agent unavailable, untrusted text not injected as HTML.

Synthetic E2E: `scripts/e2e-native-share.py` (Companion repo) builds ZIPs in a temp dir, runs the
real Provider over MCP stdio through Bridge's `mcp_provider`, previews/saves, and optionally runs a
real Agent through `agent_options::run_local` when `COMPANION_E2E_AGENT=1`. Never touches WeChat,
its databases, permissions, or the user's installed Provider copy (isolated via
`LOCAL_BRIDGE_DATA_DIR`).

## Explicit limits (must be reported honestly)

- Only the WeChat macOS merged-forward ZIP text format is verified; other exporters/versions are
  not claimed. Windows share entry is not implemented (P1 interface only).
- Attachments are metadata only in P0 (no extraction, no note links).
- Client-side XML/crypto not touched; no WeChat login or database access in any code path.
- Synthetic ZIPs prove the pipeline; they do **not** prove a real WeChat share was captured.

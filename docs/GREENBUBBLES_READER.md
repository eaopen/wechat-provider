# Optional GreenBubbles Reader

One Provider, independent reader and native sender. No additional Python dependencies or plugin framework. The reader invokes an already installed GreenBubbles CLI with an explicitly selected query profile; it never runs acquisition, account discovery, signing, GUI activation, sending or full-history export.

## Owner configuration

Set these in the **local Provider process environment**, not an Agent prompt:

- `WECHAT_PROVIDER_GB_EXECUTABLE`: absolute path to the installed CLI (not greenbubbles-acquire).
- `WECHAT_PROVIDER_GB_PROFILE`: explicit existing query profile name.
- `WECHAT_PROVIDER_GB_SOURCE_ID`: expected `source.identity` for that authorized source/profile.

The profile must already reference an authorized decrypted database, snapshot or existing credential. The owner obtains the expected identity from a separately authorized `greenbubbles source status --profile <name>` query; Provider never guesses an account or captures a key. Keep private profile/credential permissions in accordance with GreenBubbles. Missing setup disables structured reads. Status checks executable/config presence only, does not execute the reader, and always reports `verified=false`; availability is not a successful account query.

## Existing six-tool interface

- `wechat_list_chats(limit, source="structured_db", cursor?)`
- `wechat_get_messages(chat_id, limit, source="structured_db", cursor?)`
- `wechat_search(query, chat_id?, limit, source="structured_db", cursor?)`

Defaults are unchanged: list/get use desktop_ui, search uses local_index. Cursor is accepted only for structured_db. Explicit structured queries have no automatic fallback and do not populate the local index. Native sending and its ledger are unchanged; there is no GreenBubbles sender or automatic backend switching.

`chat_id` for structured_db is a stable conversation ID from the returned list, **not** a display name. Duplicate display names remain separate. Database IDs do not verify a GUI recipient and must never be mapped to send targets by name alone. Response rows retain message ID, sender ID and label, UTC timestamp, text/non-text distinction and truncation. Source namespace/identity/mode, coverage, warnings, consistency, next_cursor and has_more are retained. `complete=false` and `live_read=false` avoid implying complete history or GUI acceptance. Database source freshness is described by its mode and consistency, not these UI flags.

Messages in each page are returned oldest first for Chat Copilot; pagination still uses the upstream newest-first opaque cursor. Fetching another page goes further back, so callers must place that older page before the current one. Non-text messages are explicitly marked and not materialized. Search responses contain snippets, not guaranteed full messages. Unmatched identity/schema/chat, excess rows, invalid cursor, timeout and output beyond 8 MiB fail closed. Search text goes over stdin, never command arguments; raw stderr is discarded. The executable/profile are trusted local owner configuration, not arbitrary caller-supplied commands.

## Validation (2026-10-30)

Original handoff patch was not found in public repository branches, releases, issue links or public search. This is an independently implemented adapter against Issue #6 and merged Bridge #47, not a claimed application of that unavailable artifact.

- 29 repository unit tests pass (7 Reader tests), including malformed replies, source/identity mismatch, duplicate names, non-text, cursor, timeout/output bounds, search stdin, no GUI and no second index copy.
- Existing six-tool real MCP import/replay smoke passes.
- Actual GreenBubbles CLI at `69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b` + temporary synthetic plaintext database + actual Provider MCP: conversations, two distinct message pages, search and empty local-index-after-query pass. No installed app, private data or credentials involved.
- Bridge dev `08e4d8d` consumes actual Reader responses through real MCP: stable ID, source identity, message order and non-live flag pass; 5 chat-source core tests and 16 desktop UI tests pass. No additional Bridge change is required.
- Real encrypted WeChat 4.1.15 access, persistent owner configuration, native sending and a custom extension remain unaccepted. The earlier synthetic encrypted GreenBubbles-only tests are separate evidence, not an integrated live-account result.

Mac login protection remains in effect. This supplies P0 structured reading and capability reporting; Issue #6 remains open for real authorized-data acceptance and a concrete owner-provided extension.

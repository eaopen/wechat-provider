# WeChat Provider architecture, current state and acceptance

## Design principle

One local MCP Server, one own SQLite, two small platform adapters. No OpenRPA,
cloud account, arbitrary OS-command runner, WeChat private-protocol login, or
application-specific Local Bridge dependency.

```
Codex / Claude / local Agent
   ├─ directly: wechat-provider serve (stdio MCP)
   └─ optionally: Local Bridge (once general registration ships)
                 └── installed WeChat Provider (same stdio MCP)
   └── SQLite: indexed message snapshots, FTS5, send ledger
         ├── Mac: wxauto-mac -> installed WeChat.app
         └── Win: pyweixin -> installed Weixin.exe
```

## Real integration already coded (not yet real-world certified)

- `pyweixin.Messages.dump_recent_sessions/pull_messages/send_messages_to_friend`
- `wxauto_mac.WeChat.GetSessionList/GetChatHistory/SendToFriend`
- Local SQLite search/import with account partition
- Send ledger: queued -> running -> submitted / failed / uncertain
- Cross-process OS file lock around all desktop operations; never blindly retry uncertain tasks.
- Fixed-action RPA subprocess to absorb third-party stdout/stderr (which may contain chat text) rather than corrupt MCP stdio.
- MCP stdio endpoint with six documented tools.

The dependency methods were matched against the upstream source, not guessed. Existing
WeChat versions, UI tree visibility, local permissions and duplicate-name resolution
remain real-device unknowns.

## Concrete P0 acceptance, device by device

1. In the intended Windows 11 / macOS machine, check OS permission, installed WeChat version,
   active account login and optional package installation. Run `wechat-provider status`.
2. On a chat you control (e.g., File Transfer Assistant), list sessions; retrieve at most 10 UI
   messages; run `wechat_search` for one term; confirm it only reports indexed data.
3. Submit a short text to that same controlled chat with a stable idempotency key. Operator
   checks the intended chat's sent bubble and, when possible, recipient-side arrival.
4. Re-send with exactly the same idempotency key and confirm no duplicate send occurs.
5. Simulate a failed/blocked UI operation; ensure the task is `uncertain` if a click might
   have occurred and no blind automatic replay is attempted.
6. Repeat the same path on the second platform. Report the exact WeChat version, OS version,
   account UI-tree availability, results and observed limitations.

Until item 3 is validated, return `submitted` rather than `verified`.

## Next, only when demanded by verified use

- Reliable recipient identity mapping beyond mutable display names, with duplicate-name handling.
- Mac UI source compatibility for current WeChat 4.x; fix small adapter or substitute platform native workflow.
- Windows UI tree fallback if account/WeChat version cannot expose it.
- Read-only offline SQLCipher snapshot extraction: evaluate wechat-exporter/Wechat-MCP on Mac
  and wechat-local-analytics on Windows; never decrypt or resign client without explicit grant.
  Normalize stable IDs, timestamp, sender, media pointers, then import incrementally to our DB.
- Actual Local Bridge MCP registration when Local Bridge provides general installed provider routing.

## Out of scope

No account harvesting, group marketing, automatic friend adds, private-protocol login,
cloud sync, automatic use of arbitrary downloaded prompts/commands, or building
generic RPA orchestration. Never store login passwords, SQLCipher keys or raw process
memory dumps in GitHub or debug logs.

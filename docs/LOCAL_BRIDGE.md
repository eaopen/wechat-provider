# Integration with Local Bridge

WeChat Provider is a standalone, locally installed communication capability.

```
Agent / MCP client ──► wechat-provider serve (stdio)
Agent / Web client ──► Local Bridge ──► [future user-installed MCP registration] ──► Provider
```

## Ownership

- **WeChat Provider:** user-authorized local chat reads, its own SQLite index, desktop RPA, recipient checks, send ledger and statuses.
- **Local Bridge:** discover/register callable capabilities, route requests within its own caller authorization model, and coordinate cross-application tasks. It does not inspect WeChat's DB schema, impersonate a WeChat user, or implement send logic.
- **Etech/other applications:** remain independently authorized; Provider must not contain Etech-specific business rules.

**Current state:** Local Bridge 0.1.0 has a read-only MCP prototype; it does not yet offer general installed-provider registration. Thus this repository works independently today. Do not claim automatic Bridge integration is shipped.

## Future registration contract (proposal)

Advertise a fixed, locally installed executable and explicit stdio arguments:
`wechat-provider serve`. Announce six MCP tools and side-effecting classifications; `wechat_send_message` is a write operation. Respect the user's local connector permissions. Do not accept arbitrary execution paths or commands from a webpage or chat message.

When Local Bridge exposes actual user-approved provider registration, add a small setup example here rather than linking its runtime into this project.

Message send outcomes are `queued/running/submitted/uncertain/failed`; only separately evidenced receipt checks may be marked `verified`. Never automatically interpret `submitted` as delivered.

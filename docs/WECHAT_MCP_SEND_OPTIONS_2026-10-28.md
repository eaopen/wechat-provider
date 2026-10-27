# WeChat MCP & message sending implementation options (2026-10-28)

**Status: code/source research only.** No new candidate listed here has passed real WeChat GUI send acceptance on the target Windows or macOS devices. This file is not an endorsement of third-party installation packages, paid activations or unapproved key extraction.

## Problem, not framework

The [WeChat Provider](../README.md) must read/search messages locally and send to a verified intended peer or group through the interactive desktop app, on **both Windows and macOS**. It must work as its own stdio MCP service without Local Bridge; Bridge may register it once generic registration ships. Reuse specialized existing tools rather than build a general RPA platform.

**Two fundamentally different channels:**

- **Desktop personal-WeChat RPA:** use the signed-in real app; can interact with ordinary personal chats/groups visible to the account. Requires a compatible UI tree, active unlocked desktop and careful recipient verification.
- **Weixin ClawBot/iLink Bot channel:** supported by [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin), receives new Bot-directed messages and replies with a prior conversation context token. It **does not** expose the user's full personal contact list, ordinary chat archives, or arbitrary personal outbound group messages. It is not a drop-in replacement for personal-account desktop RPA.

## Windows options

| Source | What is actually reusable | Compatibility / cost / concerns | Decision |
|---|---|---|---|
| [Hello-Mr-Crab/pywechat (`pyweixin`)](https://github.com/Hello-Mr-Crab/pywechat) | Direct Python `Messages.send_messages_to_friend`, `pull_messages`, session & contacts UIA methods | Requires usable Windows UI tree; WeChat 4.1.6+ claimed; depends on account & client build | **P0 existing default** |
| [RungePei/wechat-mcp](https://github.com/RungePei/wechat-mcp) | Ready MCP tools: dated history, send text/files, contacts, status; time-parser, quotation-title compatibility workaround, serial UI locking and stdout handling | **Same pyweixin backend**, not an independent fallback. Project claims Windows 11 + WeChat 4.1.15.56 test; source newer but small | **P0 reference: reuse specific fixes** |
| [cluic/wxauto](https://github.com/cluic/wxauto) + [wxauto-mcp](https://github.com/cluic/wxauto-mcp) | **Independent UIA implementation**, sends text/images/files, reads sessions/new messages; ready ~27-tool MCP wrapper | Official [install guide](https://docs.wxauto.org/docs/install.html) says free `wxauto4` supports up to **WeChat 4.1.8.107**; `wxautox4` needs activation. Version/license test required | **P0 fallback research if pyweixin fails**, not new hard dependency |
| [LeoMusk/wechat-rpa-bot-skill](https://github.com/LeoMusk/wechat-rpa-bot-skill) | Installed local binary `yoko_rpa_mcp.exe` exposes loopback HTTP MCP/REST for sending, files, voice, contacts and jobs | GitHub contains Skill/docs, not underlying native backend source; activation/installation model to be separately assessed. Primarily an installed product, not an SDK | Product benchmark / user-selected optional connector, **not vendored source** |
| [pmhw/WinAutoWx](https://github.com/pmhw/WinAutoWx) | Small Python+pywinauto Windows message-send MCP | Small, old codebase; less 4.1 compatibility evidence | Low priority |
| [LinnnnYue/weixin-mcp-server](https://github.com/LinnnnYue/weixin-mcp-server) | 4.1+ pyweixin wrapper for send/read/file | Vendors upstream implementation and exposes `wechat_run_command` for arbitrary system commands. Do not expose unsafe tool to Agents | Not a default dependency |

**Key point:** more pyweixin wrappers do not create independent RPA compatibility. Test wxauto only if needed; do not prebuild a dynamic backend registry.

## macOS options

| Source | Reusable capability | Weakness / verification | Decision |
|---|---|---|---|
| [BiboyQG/WeChat-MCP](https://github.com/BiboyQG/WeChat-MCP) | PyPI `wechat-mcp-server`; native AX + screenshot read, exact-name search, `reply_to_messages_by_chat` using AX text field + synthetic Return | Requires Python 3.12+, Accessibility; uses UI automation, not proof of actual delivery. Last repository push observed Mar 2026 | **P0 test candidate**, current wxauto-mac need not stay default |
| [dustin573/wechat-mcp](https://github.com/dustin573/wechat-mcp) | Native AX, non-mutating `list_chats`, sidebar navigation (avoids global search), duplicate display-name flag, read overlap/dedupe, sender OCR. Can send via AX | Mac WeChat 4.x source, few commits; validate accessibility identifiers on actual build; send implementation is similar to BiboyQG | **P0 study/compare navigation + read**, potential Mac backend |
| [BearyRadish/wechat-macos-automation](https://github.com/BearyRadish/wechat-macos-automation) | Real-world Mac 4.1.x AppleScript/cliclick text+file UI sends, locks, outbox, errors, recipient verification techniques; updated Oct 2026 | Script-driven and installation-specific window geometry; recent Mac 4.1.60 screenshot/overlay issues documented by author; not drop-in MCP | **P0 reference for send verification & file sending** |
| [S7arFish/wxauto-mcp-mac](https://github.com/S7arFish/wxauto-mcp-mac) | Ready five-tool AppleScript/AX MCP, send current chat, read/unread | Uses Cmd+F and picks first search match, fragile when WeChat behavior changes or names duplicate; last pushed Jun 2026 | Backup for simple local test only |
| [PatrickHua/wxauto-mac](https://github.com/PatrickHua/wxauto-mac) | `SendToFriend`, `GetSessionList`, `GetChatHistory` Python API already wrapped in our MacAdapter | Older project (last push Jun 2025) with weak evidence for current 4.1.x; uses AppleScript to navigate names. Do not assume newest compatibility | **Current provisional adapter**, compare/replace based on real failure |

BiboyQG and dustin573 share some implementation approaches and identical low-level send code; do **not** count them as fully independent fallback engines. The best fallback may instead be BearyRadish's different send sequence and verification, but do not copy hard-coded coordinates.

## Separate official Bot channel (optional, not a personal-chat shortcut)

- [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin): Tencent-maintained OpenClaw channel, QR bind, getUpdates, sendMessage, media, multiple accounts. Public changelog includes **2.4.10 (2026-10-10)**. Source LICENSE is MIT. Requires OpenClaw runtime; connects to Bot conversations, not a complete personal-WeChat desktop API.
- [bkmashiro/weixin-mcp](https://github.com/bkmashiro/weixin-mcp): standalone MCP based on iLink Bot protocol. Source `src/contacts.ts` builds contact IDs **only from messages that reached the bot** and stores the context token. Therefore `contacts` does not mean the user's full personal WeChat contact book. Supports Bot conversations/media.
- [jimingyuan7/wechat-mcp](https://github.com/jimingyuan7/wechat-mcp): smaller standalone MCP port of Tencent's Bot protocol, very little activity; use as concept reference, not preferred upstream.

**Decision:** Evaluate a **separate optional Bot channel** only when the use case is "users message the AI bot / bot replies". Prefer official Tencent implementation over copy/paste of a private protocol implementation. Do not put OpenClaw dependencies into this project's base install. Never claim that iLink can send to arbitrary personal contacts, mass-message normal groups or access historical personal chats.

## Local database readers are not senders

- [robbin/wechat-exporter](https://github.com/robbin/wechat-exporter) (Mac): **implementation guide / Skill, not an already running decryptor**. Requires authorized, version-specific key handling; strictly separate from UI send path.
- [xerlxzx/Wechat-MCP](https://github.com/xerlxzx/Wechat-MCP) (Mac): read-only archive/search/MCP; documented compatibility scope includes specific macOS / WeChat builds.
- [davids1896/wechat-local-analytics](https://github.com/davids1896/wechat-local-analytics) (Windows): Windows 4.x read-only offline snapshot/JSON export candidate; independent DLL and key requirements to validate.
- [sjzar/chatlog](https://github.com/sjzar/chatlog): historical local-chat research background; do not assume current 4.x full compatibility or use a scraped copy of now-inaccessible tools.
- [lich0821/WeChatFerry](https://github.com/lich0821/WeChatFerry): Hook approach with published compatibility concentrated on WeChat 3.9, client modification and version/policy risk; **not selected for personal WeChat 4.x MVP**.

## Highest-leverage next steps — no architecture expansion

1. **Mac:** compare the exact installed WeChat build with `BiboyQG` and `dustin573` AX selectors on File Transfer Assistant; if current wxauto-mac fails, repair/replace only `adapters/macos.py` (and worker methods). Borrow BearyRadish send verification. Do not send to an arbitrary group as smoke test. Track in [#2](https://github.com/eaopen/wechat-provider/issues/2).
2. **Windows:** keep pyweixin and try `RungePei`-documented title compatibility and dated history. If current account UI tree unusable, test `wxauto4` / activated Plus on **the exact installed build**, and compare with an actual message; do not assume alternate UIA bypasses UI-tree restrictions. Track in [#1](https://github.com/eaopen/wechat-provider/issues/1).
3. **Read:** add normalized local JSONL / consistent SQLite snapshots if actual use needs historical full-text search. We already support JSONL in the shared SQLite index. Track in [#3](https://github.com/eaopen/wechat-provider/issues/3).
4. **Bot:** keep optional until there is a request for "Bot conversation" rather than operating user's own contacts; if adopted, create an independent Provider/channel and use the official Tencent plugin.

### Success gate

Record `OS + actual WeChat build + backend version`, confirm one session read, one targeted short message to a self-controlled chat, independently verify the intended recipient and sent bubble, verify same idempotency key does not resend, then report `submitted` vs independently observed `verified`. All target-platform live sends remain **unverified** until tested; never upgrade project status based on README claims or synthetic CI alone.

## Sources (reviewed 2026-10-28)

- https://github.com/Tencent/openclaw-weixin/blob/main/CHANGELOG.md
- https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md
- https://docs.wxauto.org/docs/install.html
- https://github.com/RungePei/wechat-mcp
- https://github.com/BiboyQG/WeChat-MCP
- https://github.com/dustin573/wechat-mcp
- https://github.com/BearyRadish/wechat-macos-automation
- https://github.com/bkmashiro/weixin-mcp
- https://github.com/LeoMusk/wechat-rpa-bot-skill

Note: all compatibility notes are grounded in upstream docs/code or publisher-provided tests, not independent certification; licenses and access may change.

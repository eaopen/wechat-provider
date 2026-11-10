# Unified WeChat Provider / Vault merge — 2026-11-10

## Scope

This merge integrates the user-facing native-share ZIP import and the storage / independent WeChat Vault APIs onto the latest `dev` baseline, preserving the latest native-import safety fixes. Git graph contains both `origin/dev` at `2beadb7` and the pre-existing `feat/vault-share-integration-p1` at `1b51f90`.

- One local Provider maintains the account-scoped message index, native share exports, file inventory, SHA-256 archive jobs, and optional authorized readers.
- Companion continues through the existing Local Bridge / MCP proxy; the independent Vault uses the same-user Unix RPC. This does **not** authorize automatic Agent calls or sending.
- Native ZIP importer maintains one-time grants, bounded ZIP validation, user-confirmed save, timezone semantics, source provenance and idempotence.
- New per-account native share metadata supports two local accounts importing the same ZIP without mixing labels or changing stored timezones. Existing `native_exports` records remain intact, and the new supporting metadata is initialized on Store open.
- Vault archive accepts explicitly selected files, verifies copies, preserves originals, and does not claim official WeChat backup restorability.
- WeFlow/ChatLab ingestion remains optional. No third-party library is silently installed; no WeChat login, key retrieval or macOS TCC changes are performed here.

## Local validation (Tokyo isolated checkout)

- `PYTHONPATH=src ~/.pyenv/shims/python3.12 -m unittest discover -s tests -q`: **199 passed, 0 failed**.
- `PYTHONPATH=src ~/.pyenv/shims/python3.12 tests/smoke_mcp.py`: **PASS**, real CLI/MCP with synthetic data, eleven tools.
- `PYTHONPATH=src ~/.pyenv/shims/python3.12 -m compileall -q src`: **PASS**.
- Merge conflict markers removed; `git diff --check` to be verified before commit.
- Full `ruff` not available on Tokyo host in the current offline Python environment; do not describe it as passing.

All tests use disposable synthetic sources. They do not establish full historical coverage, real WeChat Share Sheet compatibility, app signing, independent Helper FDA/TCC ownership, NAS data verification, or real account end-to-end UI acceptance.

## Safe runtime rollout and rollback

1. Do **not** mutate or swap the currently running Shanghai Mac Provider from the remote branch alone. Wait until local access is restored and confirm no archive/index worker is running.
2. Preserve the old executable, virtualenv, runtime configuration, socket path, Bridge proxy, and the account binding. Make a **consistent** SQLite backup with SQLite's backup API while the service is quiescent; never blindly copy a live WAL database.
3. Launch the unified build against a disposable index and read-only synthetic files first. Verify `vault_contract_version=1`, `capabilities.native_export_import=true`, `storage_governance`, the eleven MCP tools, and the same account/source identity over IPC and MCP.
4. Validate Companion two-batch collect → per-batch preview → explicitly saved ZIP → Vault search; no automatic Agent call.
5. Only then test the installed preview during an idle period, with a rollback plan. Check old data compatibility and new account-scoped metadata, and do not assume the independent Helper's TCC/FDA has been verified.
6. If any step fails, revert **only the preview's runtime/proxy** to the prior version and restore the consistent index snapshot if necessary. Do not reset WeChat, SIP, TCC, or delete source files.
7. Independently verify NAS connectivity and actual file hashes before considering any local cleanup. NAS failure should leave original data untouched.

**Not a production release:** keep the unified Provider PR in draft until user-authorized real WeChat and NAS acceptance.

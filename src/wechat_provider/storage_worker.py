"""Fixed inventory worker; never loads GUI adapters or a message Reader."""

import argparse
import os
from pathlib import Path

from .storage_inventory import StorageInventory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manager = StorageInventory(Path(args.directory), args.account)
    try:
        manager.run(args.job)
    except Exception:  # noqa: BLE001 -- isolate untrusted filenames and native errors at worker boundary
        # Do not log paths, filename content or native error text.
        with manager._connect(args.job) as db:
            db.execute("UPDATE job SET status='failed'")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

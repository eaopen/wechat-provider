"""Bounded normalized exports only; never open private WeChat databases or acquire keys."""
from datetime import datetime, timezone
import json
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000


def read_jsonl(path: Path) -> tuple[list[dict], dict]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("export exceeds 8 MiB; split into smaller batches")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ValueError("export is not UTF-8") from None
    rows = []
    counts = {"read": 0, "skipped": 0}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        counts["read"] += 1
        if counts["read"] > MAX_ROWS:
            raise ValueError("export exceeds 10000 records")
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError()
            for key in ("chat_id", "source_id", "text", "sender", "sent_at", "source"):
                value = obj.get(key)
                if value is not None and (not isinstance(value, str) or "\0" in value):
                    raise ValueError()
            if not all((obj.get(k) or "").strip() for k in ("chat_id", "source_id", "text")):
                counts["skipped"] += 1
                continue
            for key, limit in (("chat_id", 200), ("source_id", 4096), ("text", 32000),
                               ("sender", 500), ("source", 200)):
                if len((obj.get(key) or "").encode("utf-8")) > limit:
                    raise ValueError()
            stamp = obj.get("sent_at") or None
            if stamp:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.utcoffset() is None:
                    raise ValueError()
                stamp = parsed.astimezone(timezone.utc).isoformat()
            rows.append({"chat_id": obj["chat_id"].strip(), "source_id": obj["source_id"].strip(),
                         "text": obj["text"], "sender": obj.get("sender"), "sent_at": stamp,
                         "source": obj.get("source") or "jsonl"})
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"invalid export record at line {number}; nothing imported") from None
    return rows, counts

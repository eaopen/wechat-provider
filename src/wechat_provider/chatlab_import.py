"""Bounded ChatLab text imports; no media downloads or WeChat database access."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .greenbubbles_reader import bounded
from .structured_import import MAX_BYTES, MAX_ROWS


def index_chat_id(source_identity, session_id):
    identity = json.dumps([source_identity, session_id], ensure_ascii=False).encode()
    return "chatlab:" + hashlib.sha256(identity).hexdigest()


def normalize_document(data, source_identity, session_id, owner_id=None):
    """A caller binds private-chat IDs explicitly; a display name is never an ID."""
    try:
        if not bounded(source_identity, 512) or not bounded(session_id, 200):
            raise ValueError()
        if data["chatlab"]["version"] != "0.0.2":
            raise ValueError()
        meta, messages = data["meta"], data["messages"]
        if (meta["platform"] not in ("im", "wechat")
                or meta["type"] not in ("group", "private")
                or not bounded(meta["name"], 2000)
                or not bounded(meta["ownerId"], 200)
                or (owner_id is not None and meta["ownerId"] != owner_id)
                or (meta["type"] == "group" and meta.get("groupId") != session_id)
                or not isinstance(messages, list) or len(messages) > MAX_ROWS):
            raise ValueError()
        conversation = {
            "chat_id": index_chat_id(source_identity, session_id), "title": meta["name"],
            "source_namespace": "chatlab", "source_identity": source_identity,
            "conversation_id": session_id, "owner_id": meta["ownerId"],
        }
        rows, skipped = [], 0
        for message in messages:
            if type(message["type"]) is not int:
                raise ValueError()
            if message["type"] != 0:
                skipped += 1
                continue
            # Missing stable IDs fail closed: never hash text/time and merge real messages.
            if (not bounded(message["platformMessageId"], 4096)
                    or not bounded(message["sender"], 500)
                    or not bounded(message["content"], 32000)
                    or type(message["timestamp"]) is not int or message["timestamp"] < 0):
                raise ValueError()
            rows.append({
                "chat_id": conversation["chat_id"],
                "source_id": "chatlab:" + message["platformMessageId"],
                "text": message["content"], "sender": message["sender"],
                "sent_at": datetime.fromtimestamp(message["timestamp"], timezone.utc).isoformat(),
                "source": "chatlab",
            })
        return rows, conversation, {"read": len(messages), "skipped_non_text": skipped}
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise ValueError("ChatLab identity/schema/message rejected; nothing imported") from None


def import_chatlab(service, path: Path, source_identity, session_id, owner_id=None):
    """JSON document or JSONL of complete ChatLab page documents, one transaction."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        text = raw.decode("utf-8")
        if path.suffix.lower() == ".jsonl":
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            documents = [json.loads(text)]
        rows, conversations = [], []
        counts = {"read": 0, "skipped_non_text": 0}
        for document in documents:
            batch, conversation, tally = normalize_document(
                document, source_identity, session_id, owner_id,
            )
            rows.extend(batch)
            conversations.append(conversation)
            for key in counts:
                counts[key] += tally[key]
            if counts["read"] > MAX_ROWS:
                raise ValueError()
        result = service.store.import_messages(service.account_key, rows, conversations)
        return {**counts, **result, "complete": False}
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("ChatLab import rejected; check format, identity and batch limits") from None

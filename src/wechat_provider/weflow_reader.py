"""Opt-in WeFlow HTTP reader. Only two fixed GET routes, literal loopback, no proxies."""

import base64
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .chatlab_import import index_chat_id, normalize_document
from .greenbubbles_reader import bounded
from .models import AdapterUnavailable
from .structured_import import MAX_BYTES


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WeFlowReader:
    backend = "weflow"

    def __init__(self, base_url="", token="", source_identity="", owner_id=""):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.source_identity = source_identity
        self.owner_id = owner_id

    @classmethod
    def from_env(cls):
        return cls(*(os.environ.get("WECHAT_PROVIDER_WEFLOW_" + key, "")
                     for key in ("URL", "TOKEN", "SOURCE_ID", "OWNER_ID")))

    def status(self):
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1")
                     and url.port is not None and 1 <= url.port <= 65535
                     and not url.username and not url.password and not url.path
                     and not url.query and not url.fragment
                     and bounded(self._token, 8192)
                     and all(33 <= ord(c) <= 126 for c in self._token)
                     and bounded(self.source_identity, 512) and bounded(self.owner_id, 200))
        except ValueError:
            valid = False
        return {"backend": self.backend, "configured": bool(valid), "available": bool(valid),
                "verified": False, "id_kind": "stable_conversation_id",
                "source_identity": self.source_identity if valid else None,
                "warning": "Configuration only; no server or real-account verification."}

    def _get(self, session_id=None, **params):
        if not self.status()["available"]:
            raise AdapterUnavailable("WeFlow not configured; no initialization attempted")
        path = "/api/v1/sessions"
        if session_id is not None:
            if not bounded(session_id, 200) or session_id in (".", ".."):
                raise ValueError("invalid stable conversation ID")
            path += "/" + quote(session_id, safe="") + "/messages"
        request = Request(self.base_url + path + "?" + urlencode(params),
                          headers={"Authorization": "Bearer " + self._token,
                                   "Accept": "application/json"}, method="GET")
        try:
            # Never honor HTTP_PROXY or follow Location with a bearer credential.
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
                if (len(raw) > MAX_BYTES
                        or response.headers.get_content_type() != "application/json"):
                    raise ValueError()
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("success") is False:
                    raise ValueError()
                return data
        except HTTPError as error:
            error.close()
            raise AdapterUnavailable("WeFlow HTTP request rejected; no fallback attempted") from None
        except (URLError, OSError, ValueError, RecursionError):
            raise AdapterUnavailable("WeFlow unavailable or invalid response; no fallback attempted") from None

    def _window(self, session_id, cursor=None):
        window = {"source_identity": self.source_identity, "owner_id": self.owner_id,
                  "session_id": session_id, "since": 0, "offset": 0, "end": int(time.time())}
        if cursor is not None:
            try:
                if not bounded(cursor, 4096):
                    raise ValueError()
                saved = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (set(saved) != set(window)
                        or any(saved[k] != window[k] for k in
                               ("source_identity", "owner_id", "session_id"))
                        or any(type(saved[k]) is not int or saved[k] < 0
                               for k in ("since", "offset", "end"))
                        or saved["since"] > saved["end"] or saved["end"] > int(time.time())):
                    raise ValueError()
                window = saved
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ValueError("invalid or differently bound WeFlow cursor") from None
        return window

    def page(self, session_id, limit=100, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        if not bounded(session_id, 200):
            raise ValueError("stable conversation ID required")
        window = self._window(session_id, cursor)
        data = self._get(session_id, **{key: window[key] for key in ("since", "end", "offset")},
                         limit=limit)
        try:
            rows, conversation, counts = normalize_document(
                data, self.source_identity, session_id, self.owner_id,
            )
        except ValueError:
            raise AdapterUnavailable("WeFlow identity/schema/message rejected") from None
        try:
            sync = data["sync"]
            if (type(sync["hasMore"]) is not bool or counts["read"] > limit
                    or type(sync["watermark"]) is not int or sync["watermark"] < 0
                    or any(type(m["timestamp"]) is not int
                           or not window["since"] <= m["timestamp"] <= window["end"]
                           for m in data["messages"])):
                raise ValueError()
            next_window = {**window, "offset": window["offset"] + counts["read"]}
            if sync["hasMore"] and (not counts["read"]
                                      or sync.get("nextOffset") != next_window["offset"]):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AdapterUnavailable("WeFlow pagination/schema rejected; checkpoint unchanged") from None
        cursor_value = base64.urlsafe_b64encode(json.dumps(next_window).encode()).decode()
        return {"rows": rows, "conversation": conversation, **counts,
                "has_more": sync["hasMore"], "next_cursor": cursor_value,
                "window": window}

    def query(self, operation, limit=30, chat_id=None, cursor=None, query=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        common = {"source": "structured_db", "source_namespace": "weflow",
                  "source_identity": self.source_identity, "id_kind": "stable_conversation_id",
                  "live_read": False, "complete": False, "coverage_complete": False}
        if operation == "conversations.list":
            if cursor is not None:
                raise ValueError("WeFlow sessions API does not support pagination cursors")
            data = self._get(format="chatlab", limit=limit)
            try:
                if not isinstance(data["sessions"], list) or len(data["sessions"]) > limit:
                    raise ValueError()
                chats, seen = [], set()
                for item in data["sessions"]:
                    if (not bounded(item["id"], 200) or not bounded(item["name"], 2000)
                            or item["id"] in seen):
                        raise ValueError()
                    seen.add(item["id"])
                    chats.append({"chat_id": item["id"], "conversation_id": item["id"],
                                  "title": item["name"], "id_kind": "stable_conversation_id"})
                return {**common, "chats": chats, "has_more": None, "next_cursor": None}
            except (KeyError, TypeError, ValueError):
                raise AdapterUnavailable("WeFlow sessions response rejected") from None
        if operation != "messages.list":
            raise AdapterUnavailable("WeFlow supports list reads only; sync explicitly for local search")
        page = self.page(chat_id, limit, cursor)
        return {**common, "chat_id": chat_id,
                "messages": [{**row, "chat_id": chat_id, "conversation_id": chat_id,
                              "message_id": row["source_id"][8:], "sender_id": row["sender"],
                              "source": "structured_db"} for row in page["rows"]],
                "has_more": page["has_more"],
                "next_cursor": page["next_cursor"] if page["has_more"] else None,
                "skipped_non_text": page["skipped_non_text"]}


def sync_weflow(service, reader, session_id, max_pages=10, page_size=100):
    """Explicit bounded pull. Each page and its resume checkpoint commit together."""
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be 1-100")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("page_size must be 1-100")
    if not bounded(session_id, 200):
        raise ValueError("stable conversation ID required")
    if not reader.status()["available"]:
        raise AdapterUnavailable("WeFlow not configured")
    chat_id = index_chat_id(reader.source_identity, session_id)
    previous = service.store.get_import_cursor(service.account_key, chat_id)
    cursor = None
    if previous:
        if previous["base_url"] != reader.base_url or previous["owner_id"] != reader.owner_id:
            raise ValueError("WeFlow source binding changed; use a distinct source identity")
        cursor = previous["cursor"]
        if previous["window_exhausted"]:
            window = reader._window(session_id, cursor)
            window.update(since=max(0, window["end"] - 1), end=int(time.time()), offset=0)
            cursor = base64.urlsafe_b64encode(json.dumps(window).encode()).decode()
    totals = {"pages": 0, "read": 0, "skipped_non_text": 0, "imported": 0, "duplicates": 0}
    for _ in range(max_pages):
        page = reader.page(session_id, page_size, cursor)
        checkpoint = {"chat_id": chat_id, "cursor": page["next_cursor"],
                      "base_url": reader.base_url, "owner_id": reader.owner_id,
                      "window_exhausted": not page["has_more"]}
        result = service.store.import_messages(service.account_key, page["rows"],
                                              [page["conversation"]], checkpoint, previous)
        totals["pages"] += 1
        for key in ("read", "skipped_non_text"):
            totals[key] += page[key]
        for key in ("imported", "duplicates"):
            totals[key] += result[key]
        previous, cursor = checkpoint, page["next_cursor"]
        if not page["has_more"]:
            break
    return {**totals, "chat_id": chat_id, "has_more": page["has_more"], "complete": False,
            "window_exhausted": not page["has_more"]}

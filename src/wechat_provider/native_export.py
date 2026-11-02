"""P0 native-share ZIP import: one-shot grant, stdlib ZIP validation, TXT parser.

The Provider never accepts a filesystem path or file bytes from an MCP caller. A
same-user native process (Companion) writes a single-use grant file into the
Provider's data directory after an explicit user gesture; this module validates
that grant, unlinks it, and only then opens the archive with stdlib
:mod:`zipfile`. Nothing is extracted to disk, entry names are never joined to a
path, and no shell/eval is used.

Wire contract: ``docs/NATIVE_SHARE_CONTRACT_V1.md`` (contract version 1).
The parser is verified only against the synthetic WeChat macOS merged-forward
TXT shape; it does not read WeChat databases, login state or permissions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 1
SOURCE_KIND = "native_share"
GRANT_DIR_NAME = "native-export-grants"
GRANT_TTL_SECONDS = 600
MAX_GRANT_BYTES = 2048
MAX_ARCHIVE_BYTES = 25_165_824  # 24 MiB
MAX_ENTRIES = 2_000
MAX_TOTAL_UNCOMPRESSED = 268_435_456  # 256 MiB
MAX_ENTRY_UNCOMPRESSED = 33_554_432  # 32 MiB
MAX_COMPRESSION_RATIO = 200
MAX_PATH_BYTES = 512
MAX_MESSAGES = 20_000
MAX_PREVIEW_MESSAGES = 200
MAX_PREVIEW_BYTES = 262_144
MAX_MESSAGE_BYTES = 32_000
MAX_ATTACHMENTS = 50
MAX_PARTICIPANTS = 2_000
MAX_TXT_BYTES = 16 * 1024 * 1024
MAX_LABEL_CHARS = 120
MAX_SENDER_CHARS = 512
MAX_ARCHIVE_NAME_CHARS = 200
MAX_OFFSET = 100_000

ERROR_GRANT_INVALID = "native_export_grant_invalid"
ERROR_ARCHIVE_INVALID = "native_export_archive_invalid"
ERROR_LIMIT = "native_export_limit_exceeded"
ERROR_PARSE_FAILED = "native_export_parse_failed"
ERROR_CHANGED = "native_export_changed"
ERROR_UNSUPPORTED = "native_export_unsupported"
ERROR_INVALID_ARGUMENT = "invalid_argument"

# Stable, documented codes that may cross the same-user IPC boundary. The IPC
# layer otherwise maps ValueError to the generic `invalid_argument`; these codes
# are needed so Companion can tell a corrupt archive from "too large" without
# reflecting any path or chat text.
IPC_ERROR_CODES = frozenset({
    ERROR_GRANT_INVALID, ERROR_ARCHIVE_INVALID, ERROR_LIMIT,
    ERROR_PARSE_FAILED, ERROR_CHANGED, ERROR_UNSUPPORTED,
})

TOKEN_RE = re.compile(r"[0-9a-f]{32}")
FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
DATE_LINE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日 (\d{1,2}):(\d{2})")

FIELD_SEPARATOR = "\x1e"
TRANSCRIPT_BASENAME = "聊天记录.txt"
ATTACHMENT_KINDS = (
    (("png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "tif", "tiff", "svg"), "image"),
    (("mp4", "mov", "m4v", "avi", "mkv", "wmv", "webm", "3gp"), "video"),
    (("mp3", "wav", "m4a", "aac", "flac", "ogg", "amr", "silk"), "audio"),
    (("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "md", "csv", "rtf",
      "pages", "numbers", "key"), "document"),
    (("zip", "rar", "7z", "tar", "gz", "bz2", "xz"), "archive"),
)


def _current_uid() -> int | None:
    """os.getuid is POSIX-only; Windows (unsupported in P0) must fail closed, not crash."""
    getter = getattr(os, "getuid", None)
    return getter() if getter is not None else None


def _owner_ok(info: os.stat_result) -> bool:
    uid = _current_uid()
    return uid is None or info.st_uid == uid


def _clean_text(value: str) -> str:
    """Drop control characters, never return NUL; used only for display labels."""
    return "".join(ch for ch in value if ch == "\n" or (ord(ch) >= 0x20 and ord(ch) != 0x7F))


def _clean_label(value: str, limit: int) -> str:
    return _clean_text(value).strip()[:limit].strip()


def archive_basename(path: Path) -> str:
    try:
        name = Path(os.fspath(path)).name
    except (TypeError, ValueError):
        return ""
    return _clean_label(name, MAX_ARCHIVE_NAME_CHARS)


# ---------------------------------------------------------------------------
# One-time grant file: written by a same-user native process, consumed here.
# ---------------------------------------------------------------------------


def default_grant_dir(db_path: Path) -> Path:
    return Path(db_path).parent / GRANT_DIR_NAME


def ensure_grant_dir(value: Path) -> Path:
    """Return a real 0700 directory owned by us; never accept a symlink."""
    path = Path(value)
    try:
        if path.is_symlink():
            raise ValueError(ERROR_GRANT_INVALID)
        if not path.exists():
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise ValueError(ERROR_GRANT_INVALID) from None
    try:
        info = path.lstat()
        parent_info = path.parent.lstat()
    except OSError:
        raise ValueError(ERROR_GRANT_INVALID) from None
    if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or not _owner_ok(info)):
        raise ValueError(ERROR_GRANT_INVALID)
    if stat.S_IMODE(info.st_mode) & 0o077:
        # Tightening our own directory is safe; a foreign owner was rejected above.
        try:
            os.chmod(path, 0o700)
            info = path.lstat()
        except OSError:
            raise ValueError(ERROR_GRANT_INVALID) from None
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(ERROR_GRANT_INVALID)
    return path


def _safe_unlink(directory: Path, path: Path) -> None:
    """Unlink a grant without ever following a symlink or touching a foreign file."""
    try:
        info = path.lstat()
    except OSError:
        return
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return
    if not stat.S_ISLNK(info.st_mode) and not _owner_ok(info):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def validate_zip_path(value: Any) -> Path:
    """Grant-bound ZIP path: absolute, regular, same-uid, not a symlink, bounded."""
    if type(value) is not str or not value or "\0" in value:
        raise ValueError(ERROR_GRANT_INVALID)
    path = Path(value)
    try:
        if not path.is_absolute() or not value.lower().endswith(".zip"):
            raise ValueError(ERROR_GRANT_INVALID)
        info = path.lstat()
    except OSError:
        raise ValueError(ERROR_GRANT_INVALID) from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or not _owner_ok(info) or info.st_size > MAX_ARCHIVE_BYTES):
        raise ValueError(ERROR_GRANT_INVALID)
    return path


def mint_grant(grant_dir: Path, zip_path: Any, purpose: str) -> dict[str, Any]:
    """Test/acceptance helper mirroring Companion's grant writer (0600, one-shot)."""
    if purpose not in {"preview", "save"}:
        raise ValueError(ERROR_GRANT_INVALID)
    directory = ensure_grant_dir(grant_dir)
    target_zip = validate_zip_path(os.path.abspath(os.fspath(zip_path)))
    for _ in range(8):
        token = secrets.token_hex(16)
        payload = json.dumps(
            {"version": CONTRACT_VERSION, "token": token, "path": str(target_zip),
             "purpose": purpose},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_GRANT_BYTES:
            raise ValueError(ERROR_GRANT_INVALID)
        grant_file = directory / f"{token}.json"
        try:
            descriptor = os.open(
                grant_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            continue
        except OSError:
            raise ValueError(ERROR_GRANT_INVALID) from None
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return {"grant_token": token, "grant_path": str(grant_file),
                "purpose": purpose, "expires_in_seconds": GRANT_TTL_SECONDS}
    raise ValueError(ERROR_GRANT_INVALID)


def consume_grant(grant_dir: Path, token: Any, purpose: str) -> Path:
    """Validate and unlink one grant file; returns the bound ZIP path.

    The grant is removed on every outcome that reaches the file (one-shot), even
    when validation later fails, and before the ZIP is parsed.
    """
    if type(token) is not str or not TOKEN_RE.fullmatch(token) or purpose not in {"preview", "save"}:
        raise ValueError(ERROR_GRANT_INVALID)
    directory = ensure_grant_dir(grant_dir)
    grant_file = directory / f"{token}.json"
    try:
        descriptor = os.open(grant_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _safe_unlink(directory, grant_file)
        raise ValueError(ERROR_GRANT_INVALID) from None
    raw = b""
    try:
        info = os.fstat(descriptor)
        valid = (
            stat.S_ISREG(info.st_mode)
            and _owner_ok(info)
            and not (stat.S_IMODE(info.st_mode) & 0o077)
            and info.st_size <= MAX_GRANT_BYTES
            and abs(time.time() - info.st_mtime) <= GRANT_TTL_SECONDS
        )
        if not valid:
            raise ValueError(ERROR_GRANT_INVALID)
        chunks: list[bytes] = []
        remaining = MAX_GRANT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_GRANT_BYTES:
            raise ValueError(ERROR_GRANT_INVALID)
    finally:
        os.close(descriptor)
        _safe_unlink(directory, grant_file)
    try:
        grant_file.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise ValueError(ERROR_GRANT_INVALID) from None
    else:
        # Refuse to parse if the one-shot grant could not be consumed.
        raise ValueError(ERROR_GRANT_INVALID)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError(ERROR_GRANT_INVALID) from None
    try:
        if type(payload["version"]) is not int or payload["version"] != CONTRACT_VERSION:
            raise ValueError(ERROR_GRANT_INVALID)
        if payload["token"] != token or payload["purpose"] != purpose:
            raise ValueError(ERROR_GRANT_INVALID)
        path = payload["path"]
    except (TypeError, KeyError):
        raise ValueError(ERROR_GRANT_INVALID) from None
    return validate_zip_path(path)


# ---------------------------------------------------------------------------
# Archive inspection (stdlib zipfile only; validate before any parsing).
# ---------------------------------------------------------------------------


@dataclass
class ArchiveEntry:
    info: zipfile.ZipInfo
    name: str
    basename: str
    is_dir: bool
    size: int
    compress_type: int


@dataclass
class Attachment:
    name: str
    basename: str
    kind: str
    bytes: int


@dataclass
class Record:
    sender: str
    local_time: str
    text: str
    source_id: str
    attachment_refs: tuple[str, ...] = ()


@dataclass
class NativeExport:
    archive_name: str
    label: str
    transcript_name: str
    records: list[Record]
    participants: list[str]
    attachments: list[Attachment]
    fingerprint: str
    session_id: str
    unparsed_tail_bytes: int = 0


class _BadArchive(Exception):
    """Internal marker so our own size bound is not confused with zipfile errors."""


def _open_zip_handle(zip_path: Path) -> Any:
    try:
        descriptor = os.open(zip_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        raise ValueError(ERROR_ARCHIVE_INVALID) from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or not _owner_ok(info)
                or info.st_size <= 0 or info.st_size > MAX_ARCHIVE_BYTES):
            raise ValueError(ERROR_LIMIT if info.st_size > MAX_ARCHIVE_BYTES
                             else ERROR_ARCHIVE_INVALID)
        return os.fdopen(descriptor, "rb")
    except (OSError, ValueError) as error:
        os.close(descriptor)
        if isinstance(error, ValueError):
            raise
        raise ValueError(ERROR_ARCHIVE_INVALID) from None


def _open_zip(handle: Any) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(handle)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError,
            RuntimeError, OSError, EOFError, ValueError, struct.error):
        raise ValueError(ERROR_ARCHIVE_INVALID) from None


def _has_zip64_extra_fields(extra: bytes) -> bool:
    offset = 0
    while offset + 4 <= len(extra):
        try:
            tag, length = struct.unpack("<HH", extra[offset:offset + 4])
        except struct.error:
            return False
        if tag == 0x0001:
            return True
        offset += 4 + length
    return False


def _local_header_extra(handle: Any, info: zipfile.ZipInfo) -> bytes:
    """Read one local file header only to look for ZIP64 markers (bounded)."""
    try:
        handle.seek(max(0, info.header_offset))
        header = handle.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            return b""
        name_length, extra_length = struct.unpack("<HH", header[26:30])
        if name_length + extra_length > 128 * 1024:
            return b""
        if header[18:22] == b"\xff\xff\xff\xff" or header[22:26] == b"\xff\xff\xff\xff":
            return b"\x01\x00\x00\x00"  # sentinel sizes imply a ZIP64 local header
        handle.seek(info.header_offset + 30 + name_length)
        return handle.read(min(extra_length, 64 * 1024))
    except OSError:
        return b""


def _reject_zip64(handle: Any, infos: list[zipfile.ZipInfo]) -> None:
    """ZIP64 is not supported in P0; reject before reading entry data."""
    if any(_has_zip64_extra_fields(info.extra or b"") for info in infos):
        raise ValueError(ERROR_UNSUPPORTED)
    if any(info.file_size == 0xFFFFFFFF or info.compress_size == 0xFFFFFFFF
           or info.header_offset == 0xFFFFFFFF for info in infos):
        raise ValueError(ERROR_UNSUPPORTED)
    if any(_has_zip64_extra_fields(_local_header_extra(handle, info)) for info in infos):
        raise ValueError(ERROR_UNSUPPORTED)
    try:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        tail_length = min(size, 65536 + 32)
        handle.seek(size - tail_length, os.SEEK_SET)
        tail = handle.read(tail_length)
    except OSError:
        raise ValueError(ERROR_ARCHIVE_INVALID) from None
    position = tail.rfind(b"PK\x05\x06")
    if position >= 20 and tail[position - 20:position - 16] == b"PK\x06\x07":
        raise ValueError(ERROR_UNSUPPORTED)
    if position >= 0 and position + 22 <= len(tail):
        signature, disk, disk_start = struct.unpack("<4sHH", tail[position:position + 8])
        if signature == b"PK\x05\x06" and (disk != 0 or disk_start != 0):
            raise ValueError(ERROR_ARCHIVE_INVALID)  # multi-disk archive


def _normalized(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _valid_entry_name(info: zipfile.ZipInfo) -> str:
    """Reject traversal, absolute paths, control characters and file-type tricks."""
    raw_names = {info.filename, info.orig_filename}
    for raw in raw_names:
        if not raw or "\0" in raw:
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if "\\" in raw:
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if raw.startswith("/"):
            raise ValueError(ERROR_ARCHIVE_INVALID)
    name = _normalized(info.filename)
    if not name or len(name.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError(ERROR_LIMIT if name else ERROR_ARCHIVE_INVALID)
    is_dir = info.is_dir()
    trimmed = name[:-1] if is_dir and name.endswith("/") else name
    components = trimmed.split("/")
    for component in components:
        if component in {"", ".", ".."}:
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if ":" in component or "\0" in component:
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in component):
            raise ValueError(ERROR_ARCHIVE_INVALID)
    if info.flag_bits & 0x41:
        raise ValueError(ERROR_ARCHIVE_INVALID)  # encrypted / strong encryption
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError(ERROR_ARCHIVE_INVALID)
    file_type = (info.external_attr >> 16) & 0o170000
    if is_dir:
        if file_type not in (0, stat.S_IFDIR):
            raise ValueError(ERROR_ARCHIVE_INVALID)
        if info.file_size != 0:
            raise ValueError(ERROR_ARCHIVE_INVALID)
    else:
        if file_type not in (0, stat.S_IFREG):
            raise ValueError(ERROR_ARCHIVE_INVALID)  # symlink/hardlink/other type
        if info.file_size > MAX_ENTRY_UNCOMPRESSED:
            raise ValueError(ERROR_LIMIT)
    return name


def _validate_entries(infos: list[zipfile.ZipInfo]) -> list[ArchiveEntry]:
    if len(infos) > MAX_ENTRIES:
        raise ValueError(ERROR_LIMIT)
    entries: list[ArchiveEntry] = []
    seen: set[str] = set()
    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        name = _valid_entry_name(info)
        key = name.lower()
        if key in seen:
            raise ValueError(ERROR_ARCHIVE_INVALID)
        seen.add(key)
        is_dir = info.is_dir()
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            raise ValueError(ERROR_LIMIT)
        entries.append(ArchiveEntry(
            info=info, name=name, basename=name.rsplit("/", 1)[-1] if not is_dir else name,
            is_dir=is_dir, size=info.file_size, compress_type=info.compress_type,
        ))
    if total_uncompressed > 0 and (
            total_compressed <= 0
            or total_uncompressed / total_compressed > MAX_COMPRESSION_RATIO):
        raise ValueError(ERROR_LIMIT)
    return entries


def _read_entry(archive: zipfile.ZipFile, entry: ArchiveEntry, capture: bool) -> bytes:
    """Bounded read of one entry; zipfile verifies CRC at end of stream."""
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(entry.info) as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > entry.size or total > MAX_ENTRY_UNCOMPRESSED:
                    raise _BadArchive
                if capture:
                    chunks.append(chunk)
    except _BadArchive:
        raise ValueError(ERROR_ARCHIVE_INVALID) from None
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError,
            RuntimeError, OSError, EOFError, ValueError, struct.error):
        raise ValueError(ERROR_ARCHIVE_INVALID) from None
    if total != entry.size:
        raise ValueError(ERROR_ARCHIVE_INVALID)
    return b"".join(chunks)


def _attachment_kind(name: str) -> str:
    basename = name.rsplit("/", 1)[-1]
    extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    for extensions, kind in ATTACHMENT_KINDS:
        if extension in extensions:
            return kind
    return "other"


def _is_txt_entry(entry: ArchiveEntry) -> bool:
    return not entry.is_dir and entry.name.lower().endswith(".txt")


def _attachment_metadata(entries: list[ArchiveEntry]) -> list[Attachment]:
    attachments: list[Attachment] = []
    for entry in entries:
        if entry.is_dir or _is_txt_entry(entry):
            continue
        attachments.append(Attachment(
            name=entry.name, basename=entry.basename,
            kind=_attachment_kind(entry.name), bytes=entry.size,
        ))
    return attachments


# ---------------------------------------------------------------------------
# WeChat macOS merged-forward TXT parser.
# ---------------------------------------------------------------------------


def _parse_stamp(value: str) -> datetime | None:
    match = DATE_LINE_RE.fullmatch(value)
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _local_stamp(value: str) -> str | None:
    parsed = _parse_stamp(value)
    if parsed is None:
        return None
    return f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}T{parsed.hour:02d}:{parsed.minute:02d}"


def parse_transcript(payload: bytes) -> tuple[list[Record], int]:
    """Parse the merged-forward TXT bytes; fail closed on malformed structure."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(ERROR_PARSE_FAILED) from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip()
    if not text.startswith("·"):
        raise ValueError(ERROR_PARSE_FAILED)
    lines = text.split("\n")
    starts = [
        index for index in range(len(lines) - 1)
        if lines[index].startswith("·") and _parse_stamp(lines[index + 1]) is not None
    ]
    if not starts or starts[0] != 0:
        raise ValueError(ERROR_PARSE_FAILED)
    occurrences: dict[tuple[str, str, str], int] = {}
    records: list[Record] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        sender = lines[start][1:].strip()
        if (not sender or len(sender) > MAX_SENDER_CHARS or "\0" in sender
                or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in sender)):
            raise ValueError(ERROR_PARSE_FAILED)
        stamp = _local_stamp(lines[start + 1])
        if stamp is None:  # defensive: the start-pair scan already validated it
            raise ValueError(ERROR_PARSE_FAILED)
        body = "\n".join(lines[start + 2:end]).rstrip()
        if "\0" in body:
            raise ValueError(ERROR_PARSE_FAILED)
        identity = (sender, stamp, body)
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        material = FIELD_SEPARATOR.join((sender, stamp, body, str(occurrence)))
        source_id = "native:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        records.append(Record(sender=sender, local_time=stamp, text=body, source_id=source_id))
        if len(records) > MAX_MESSAGES:
            raise ValueError(ERROR_LIMIT)
    return records, 0


def _bind_attachment_refs(records: list[Record], attachments: list[Attachment]) -> None:
    by_line: dict[str, list[str]] = {}
    for attachment in attachments:
        for candidate in (attachment.name, attachment.basename):
            if not candidate:
                continue
            existing = by_line.setdefault(candidate, [])
            if attachment.name not in existing:
                existing.append(attachment.name)
    for record in records:
        refs: list[str] = []
        seen: set[str] = set()
        for line in record.text.split("\n"):
            for name in by_line.get(line, ()):
                if name not in seen:
                    seen.add(name)
                    refs.append(name)
        record.attachment_refs = tuple(refs[:MAX_ATTACHMENTS])


def _fingerprint(records: list[Record]) -> str:
    material = FIELD_SEPARATOR.join(
        field for record in records for field in (record.sender, record.local_time, record.text)
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _archive_label(zip_path: Path, transcript_name: str) -> str:
    try:
        label = _clean_label(Path(os.fspath(zip_path)).stem, MAX_LABEL_CHARS)
    except (TypeError, ValueError):
        label = ""
    if not label:
        label = _clean_label(transcript_name.rsplit("/", 1)[-1], MAX_LABEL_CHARS)
    return label or "native_share_export"


def _pick_transcript(
    archive: zipfile.ZipFile, entries: list[ArchiveEntry],
) -> tuple[list[Record], str]:
    candidates = [entry for entry in entries if _is_txt_entry(entry)]
    if not candidates:
        raise ValueError(ERROR_PARSE_FAILED)
    preferred = [entry for entry in candidates if entry.basename == TRANSCRIPT_BASENAME]
    if preferred:
        chosen = preferred[0]
        if chosen.size > MAX_TXT_BYTES:
            raise ValueError(ERROR_LIMIT)
        payload = _read_entry(archive, chosen, capture=True)
        records, _ = parse_transcript(payload)
        return records, chosen.name
    best: tuple[list[Record], str] | None = None
    for entry in candidates:
        if entry.size > MAX_TXT_BYTES:
            continue
        payload = _read_entry(archive, entry, capture=True)
        try:
            records, _ = parse_transcript(payload)
        except ValueError as error:
            if str(error) == ERROR_PARSE_FAILED:
                continue
            raise
        if best is None or len(records) > len(best[0]):
            best = (records, entry.name)
    if best is None:
        raise ValueError(ERROR_PARSE_FAILED)
    return best


def load_native_export(zip_path: Path) -> NativeExport:
    """Validate + parse one grant-bound ZIP; raises contract error codes only."""
    archive_name = archive_basename(zip_path)
    handle = _open_zip_handle(zip_path)
    try:
        archive = _open_zip(handle)
        try:
            infos = archive.infolist()
            entries = _validate_entries(infos)
            _reject_zip64(handle, infos)
            for entry in entries:
                if not entry.is_dir:
                    _read_entry(archive, entry, capture=False)
            attachments = _attachment_metadata(entries)
            records, transcript_name = _pick_transcript(archive, entries)
        finally:
            archive.close()
    finally:
        handle.close()
    participants: list[str] = []
    seen_senders: set[str] = set()
    for record in records:
        if record.sender not in seen_senders:
            seen_senders.add(record.sender)
            participants.append(record.sender)
    if len(participants) > MAX_PARTICIPANTS:
        raise ValueError(ERROR_LIMIT)
    _bind_attachment_refs(records, attachments)
    fingerprint = _fingerprint(records)
    return NativeExport(
        archive_name=archive_name,
        label=_archive_label(zip_path, transcript_name),
        transcript_name=transcript_name,
        records=records,
        participants=participants,
        attachments=attachments,
        fingerprint=fingerprint,
        session_id="native_share:" + fingerprint[len("sha256:"):][:32],
    )


# ---------------------------------------------------------------------------
# Bounded preview rendering (timezone never assumed).
# ---------------------------------------------------------------------------


def apply_timezone(local_time: str, offset_minutes: int) -> str:
    zone = timezone(timedelta(minutes=offset_minutes))
    parsed = datetime.fromisoformat(local_time).replace(tzinfo=zone)
    return parsed.isoformat(timespec="seconds")


def render_preview(
    export: NativeExport,
    *,
    timezone_offset_minutes: int | None,
    offset: int,
    limit: int,
    already_saved: bool,
) -> dict[str, Any]:
    warnings: list[str] = []
    if timezone_offset_minutes is None:
        timezone_field: dict[str, Any] = {"applied": False}
        warnings.append("未提供时区偏移：sent_at 为空，仅保留本地时间；不会假设本机时区。")
    else:
        timezone_field = {"applied": True, "offset_minutes": timezone_offset_minutes}
    for record in export.records:
        if len(record.text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(ERROR_LIMIT)
    messages: list[dict[str, Any]] = []
    used_bytes = 0
    end = min(len(export.records), offset + limit)
    for index in range(offset, end):
        record = export.records[index]
        text_bytes = len(record.text.encode("utf-8"))
        if used_bytes + text_bytes > MAX_PREVIEW_BYTES:
            break
        used_bytes += text_bytes
        messages.append({
            "index": index,
            "source_id": record.source_id,
            "sender": record.sender,
            "sent_at": (apply_timezone(record.local_time, timezone_offset_minutes)
                        if timezone_offset_minutes is not None else None),
            "sent_at_local": record.local_time,
            "text": record.text,
            "attachment_refs": list(record.attachment_refs),
        })
    returned = len(messages)
    has_more = offset + returned < len(export.records)
    stamps = sorted(record.local_time for record in export.records)
    time_range = {"start": stamps[0], "end": stamps[-1]} if stamps else None
    attachments = [
        {"name": item.name, "kind": item.kind, "bytes": item.bytes}
        for item in export.attachments[:MAX_ATTACHMENTS]
    ]
    return {
        "ok": True,
        "source": SOURCE_KIND,
        "export": {
            "contract_version": CONTRACT_VERSION,
            "session_id": export.session_id,
            "fingerprint": export.fingerprint,
            "label": export.label,
            "participants": list(export.participants),
            "participant_count": len(export.participants),
            "message_count": len(export.records),
            "attachment_count": len(export.attachments),
            "attachments": attachments,
            "time_range_local": time_range,
            "timezone": timezone_field,
            "coverage": {
                "transcript_found": True,
                "format": "native_txt",
                "parsed_records": len(export.records),
                "unparsed_tail_bytes": export.unparsed_tail_bytes,
                "complete": True,
            },
            "already_saved": already_saved,
        },
        "messages": messages,
        "window": {
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "has_more": has_more,
            "next_offset": offset + returned if has_more else None,
        },
        "warnings": warnings,
    }

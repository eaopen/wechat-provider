"""P0 native-share import tests: grants, ZIP safety, parser, preview, save.

Everything here is synthetic: no WeChat databases, login state or permissions are
touched, and no real chat text is used.
"""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_provider import native_export as ne
from wechat_provider.local_ipc import METHODS, handle, receive, send
from wechat_provider.service import Service
from wechat_provider.store import Store

TRANSCRIPT = "聊天记录.txt"


def record(sender, stamp, body):
    return f"·{sender}\n{stamp}\n{body}\n\n"


def transcript(*items):
    return "".join(record(*item) for item in items).encode("utf-8")


def build_zip(path: Path, entries, method=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression=method) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


class AdapterStub:
    def status(self):
        return {"ready_for_attempt": False, "platform": "synthetic"}


class NativeExportBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self.store = Store(self.root / "wechat.db")
        self.service = Service(self.store, Mock(), "fixture-account")
        self.grant_dir = self.service.grant_dir
        self._counter = 0

    def make_zip(self, entries, method=zipfile.ZIP_DEFLATED, name=None, subdir="") -> Path:
        self._counter += 1
        folder = self.root / subdir if subdir else self.root
        folder.mkdir(parents=True, exist_ok=True)
        stem = name or "export-" + str(self._counter)
        path = folder / (stem if stem.endswith(".zip") else stem + ".zip")
        return build_zip(path, entries, method)

    def sample_zip(self, **kwargs) -> Path:
        return self.make_zip([
            (TRANSCRIPT, transcript(
                ("张三", "2026年9月5日 08:05", "早上好 ☕️\n今天有点忙"),
                ("我", "2026年9月5日 08:06", "好的"),
                ("张三", "2026年9月5日 08:07", "OK 👌"),
            )),
        ], **kwargs)

    def mint(self, zip_path, purpose="preview") -> str:
        return ne.mint_grant(self.grant_dir, zip_path, purpose)["grant_token"]

    def preview(self, zip_path, **kwargs):
        return self.service.preview_native_export(self.mint(zip_path), **kwargs)

    def save(self, zip_path, fingerprint):
        return self.service.save_native_export(self.mint(zip_path, "save"), fingerprint)

    def expect_error(self, zip_path, code, **kwargs) -> None:
        with self.assertRaises(ValueError) as caught:
            self.preview(zip_path, **kwargs)
        self.assertEqual(str(caught.exception), code)


class GrantHandshakeTest(NativeExportBase):
    def test_grant_is_consumed_before_parse_even_when_archive_invalid(self):
        bad = self.make_zip([("x.bin", b"no transcript")])
        token = self.mint(bad)
        grant_file = self.grant_dir / f"{token}.json"
        self.assertTrue(grant_file.is_file())
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(token)
        self.assertIn(str(caught.exception), {ne.ERROR_PARSE_FAILED, ne.ERROR_ARCHIVE_INVALID})
        self.assertFalse(grant_file.exists())
        # The user's original file is never moved/renamed/deleted.
        self.assertTrue(bad.is_file())

    def test_grant_reuse_expiry_mode_and_purpose(self):
        good = self.sample_zip()
        token = self.mint(good)
        self.service.preview_native_export(token)
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(token)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

        expired = self.mint(good)
        grant_file = self.grant_dir / f"{expired}.json"
        old = time.time() - (ne.GRANT_TTL_SECONDS + 60)
        os.utime(grant_file, (old, old))
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(expired)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)
        self.assertFalse(grant_file.exists())

        wide = self.mint(good)
        grant_file = self.grant_dir / f"{wide}.json"
        os.chmod(grant_file, 0o640)
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(wide)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)
        self.assertFalse(grant_file.exists())

        purpose = self.mint(good, "save")
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(purpose)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

    def test_tampered_grant_content_is_rejected_and_removed(self):
        good = self.sample_zip()
        cases = [
            {"version": 2},
            {"token": "0" * 32},
            {"purpose": "save"},
            {"path": "relative/path.zip"},
            {"path": "/tmp/not-a-zip.txt"},
            {"path": str(self.root / "missing.zip")},
        ]
        for change in cases:
            with self.subTest(change=list(change)):
                token = self.mint(good)
                grant_file = self.grant_dir / f"{token}.json"
                payload = json.loads(grant_file.read_text(encoding="utf-8"))
                payload.update(change)
                grant_file.write_text(json.dumps(payload), encoding="utf-8")
                os.chmod(grant_file, 0o600)
                with self.assertRaises(ValueError) as caught:
                    self.service.preview_native_export(token)
                self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)
                self.assertFalse(grant_file.exists())

    def test_symlinked_grant_file_and_directory_are_rejected(self):
        good = self.sample_zip()
        token = self.mint(good)
        real = self.grant_dir / f"{token}.json"
        target = self.root / "elsewhere.json"
        target.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        real.unlink()
        real.symlink_to(target)
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(token)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)
        self.assertFalse(real.exists())
        self.assertTrue(target.is_file())

        linked = self.root / "linked-grants"
        linked.symlink_to(self.grant_dir)
        service = Service(self.store, AdapterStub(), "fixture-account", grant_dir=linked)
        with self.assertRaises(ValueError) as caught:
            service.preview_native_export(self.mint(good))
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

    def test_grant_file_size_limit_and_zip_path_checks(self):
        good = self.sample_zip()
        token = self.mint(good)
        grant_file = self.grant_dir / f"{token}.json"
        grant_file.write_bytes(b"{" + b" " * (ne.MAX_GRANT_BYTES + 1) + b"}")
        os.chmod(grant_file, 0o600)
        with self.assertRaises(ValueError) as caught:
            self.service.preview_native_export(token)
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

        symlinked_zip = self.root / "symlinked.zip"
        symlinked_zip.symlink_to(good)
        with self.assertRaises(ValueError) as caught:
            ne.mint_grant(self.grant_dir, symlinked_zip, "preview")
        self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

        with patch.object(ne, "MAX_ARCHIVE_BYTES", 10):
            with self.assertRaises(ValueError) as caught:
                ne.mint_grant(self.grant_dir, good, "preview")
            self.assertEqual(str(caught.exception), ne.ERROR_GRANT_INVALID)

    def test_grant_dir_is_created_private_and_tightened(self):
        exposed = self.root / "exposed-grants"
        exposed.mkdir(mode=0o755, parents=True)
        service = Service(self.store, AdapterStub(), "fixture-account", grant_dir=exposed)
        native = service.status()["native_export"]
        self.assertTrue(native["enabled"])
        self.assertEqual(stat.S_IMODE(exposed.lstat().st_mode), 0o700)

    def test_unsafe_grant_dir_reports_disabled_status(self):
        linked = self.root / "status-linked"
        linked.symlink_to(self.grant_dir)
        service = Service(self.store, AdapterStub(), "fixture-account", grant_dir=linked)
        native = service.status()["native_export"]
        self.assertFalse(native["enabled"])
        self.assertEqual(native["grant_dir"], str(linked))


class ZipSafetyTest(NativeExportBase):
    def test_path_traversal_absolute_and_component_tricks_are_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        for name in ("../evil.txt", "a/../../evil.png", "/etc/passwd.png",
                     "a\\b.png", "a:b.png", "a/./b.png", "a//b.png",
                     "a\x01b.png", "a\x7fb.png"):
            with self.subTest(name=name):
                path = self.make_zip([(TRANSCRIPT, base), (name, b"x")])
                self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

    def test_duplicate_names_after_nfc_lowercase_are_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        for first, second in (("a.png", "A.PNG"), ("图.png", "图.PNG")):
            with self.subTest(names=(first, second)):
                path = self.make_zip([(TRANSCRIPT, base), (first, b"x"), (second, b"y")])
                self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)
        # NFC/NFD spellings of the same name collapse to one normalized name.
        path = self.make_zip([(TRANSCRIPT, base), ("café.png", b"x"), ("cafe\u0301.png", b"y")])
        self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

    def test_symlink_and_non_regular_entries_are_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        link = zipfile.ZipInfo("link.png")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = self.make_zip([(TRANSCRIPT, base), (link, "/etc/passwd")])
        self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

        directory_with_data = zipfile.ZipInfo("images/")
        path = self.make_zip([(TRANSCRIPT, base), (directory_with_data, b"not empty")])
        self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

    def test_encrypted_unsupported_method_and_zip64_are_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        encrypted = self.make_zip([(TRANSCRIPT, base)])
        raw = bytearray(encrypted.read_bytes())
        # zipfile's writer clears flag_bits, so set the encryption bit in both
        # the local file header and the central directory by hand.
        raw[6:8] = (int.from_bytes(raw[6:8], "little") | 0x1).to_bytes(2, "little")
        central = raw.rfind(b"PK\x01\x02")
        raw[central + 8:central + 10] = (
            int.from_bytes(raw[central + 8:central + 10], "little") | 0x1).to_bytes(2, "little")
        encrypted.write_bytes(bytes(raw))
        self.expect_error(encrypted, ne.ERROR_ARCHIVE_INVALID)

        bzip2 = self.make_zip([(TRANSCRIPT, base)], method=zipfile.ZIP_BZIP2)
        self.expect_error(bzip2, ne.ERROR_ARCHIVE_INVALID)

        zip64 = self.make_zip([(TRANSCRIPT, base)])
        with zipfile.ZipFile(zip64, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo(TRANSCRIPT)
            with archive.open(info, "w", force_zip64=True) as stream:
                stream.write(base)
        self.expect_error(zip64, ne.ERROR_UNSUPPORTED)

    def test_multi_disk_marker_is_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        path = self.make_zip([(TRANSCRIPT, base)])
        raw = bytearray(path.read_bytes())
        index = raw.rfind(b"PK\x05\x06")
        raw[index + 4:index + 8] = (1).to_bytes(2, "little") * 2
        path.write_bytes(bytes(raw))
        self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

    def test_bad_crc_and_garbage_payload_are_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        path = self.make_zip([(TRANSCRIPT, base), ("x.bin", b"payload")],
                             method=zipfile.ZIP_STORED)
        raw = bytearray(path.read_bytes())
        index = raw.rfind(b"payload")
        self.assertGreaterEqual(index, 0)
        raw[index] ^= 0xFF
        path.write_bytes(bytes(raw))
        self.expect_error(path, ne.ERROR_ARCHIVE_INVALID)

        garbage = self.root / "garbage.zip"
        garbage.write_bytes(b"not a zip archive")
        self.expect_error(garbage, ne.ERROR_ARCHIVE_INVALID)

    def test_compression_bomb_is_rejected(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        path = self.make_zip([(TRANSCRIPT, base), ("zero.bin", b"\0" * (6 * 1024 * 1024))])
        self.expect_error(path, ne.ERROR_LIMIT)

    def test_size_count_and_path_limits(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        with patch.object(ne, "MAX_ENTRIES", 2):
            path = self.make_zip([(TRANSCRIPT, base), ("a.png", b"x"), ("b.png", b"y")])
            self.expect_error(path, ne.ERROR_LIMIT)
        with patch.object(ne, "MAX_TOTAL_UNCOMPRESSED", 10):
            path = self.make_zip([(TRANSCRIPT, base), (TRANSCRIPT + ".png", b"x" * 20)])
            self.expect_error(path, ne.ERROR_LIMIT)
        with patch.object(ne, "MAX_ENTRY_UNCOMPRESSED", 10):
            path = self.make_zip([(TRANSCRIPT, base)])
            self.expect_error(path, ne.ERROR_LIMIT)
        path = self.make_zip([(TRANSCRIPT, base), ("a" * 511 + ".png", b"x")])
        self.expect_error(path, ne.ERROR_LIMIT)

    def test_invalid_archives_never_modify_the_original_file(self):
        base = transcript(("甲", "2026年9月5日 08:05", "hi"))
        cases = []
        traversal = self.make_zip([(TRANSCRIPT, base), ("../x.png", b"x")])
        cases.append(traversal)
        bomb = self.make_zip([(TRANSCRIPT, base), ("zero.bin", b"\0" * (6 * 1024 * 1024))])
        cases.append(bomb)
        garbage = self.root / "garbage.zip"
        garbage.write_bytes(b"definitely not a zip")
        cases.append(garbage)
        before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                  for path in cases}
        for path in cases:
            with self.assertRaises(ValueError):
                self.preview(path)
        after = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                 for path in cases}
        self.assertEqual(before, after)


class TranscriptDiscoveryAndParserTest(NativeExportBase):
    def test_prefers_exact_transcript_name_and_falls_back_to_richest_txt(self):
        preferred = self.make_zip([
            ("other.txt", transcript(("甲", "2026年9月5日 08:05", "a"),
                                     ("乙", "2026年9月5日 08:06", "b"),
                                     ("丙", "2026年9月5日 08:07", "c"))),
            (TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "only"))),
        ])
        self.assertEqual(self.preview(preferred)["export"]["message_count"], 1)

        fallback = self.make_zip([
            ("a.txt", transcript(("甲", "2026年9月5日 08:05", "one"))),
            ("b.TXT", transcript(("甲", "2026年9月5日 08:05", "one"),
                                  ("乙", "2026年9月5日 08:06", "two"),
                                  ("丙", "2026年9月5日 08:07", "three"))),
        ])
        self.assertEqual(self.preview(fallback)["export"]["message_count"], 3)

    def test_missing_or_undecodable_transcript_fails(self):
        no_txt = self.make_zip([("photo.png", b"x")])
        self.expect_error(no_txt, ne.ERROR_PARSE_FAILED)
        undecodable = self.make_zip([(TRANSCRIPT, b"\xff\xfe\x00\x00garbage")])
        self.expect_error(undecodable, ne.ERROR_PARSE_FAILED)
        # A broken fallback candidate is skipped when another candidate parses.
        mixed = self.make_zip([
            ("broken.txt", b"\xff\xfe\x00\x00"),
            ("good.txt", transcript(("甲", "2026年9月5日 08:05", "ok"))),
        ])
        self.assertEqual(self.preview(mixed)["export"]["message_count"], 1)

    def test_oversized_preferred_transcript_is_a_limit_error(self):
        with patch.object(ne, "MAX_TXT_BYTES", 5):
            path = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "hi")))])
            self.expect_error(path, ne.ERROR_LIMIT)

    def test_parser_preserves_text_and_identifies_messages(self):
        path = self.make_zip([
            (TRANSCRIPT, transcript(
                ("甲", "2026年9月5日 08:05", "第一行\n\n第三行  "),
                ("乙 English", "2026年9月5日 08:06", "emoji 🙂 and ascii"),
                ("甲", "2026年9月5日 08:07", "重复"),
                ("甲", "2026年9月5日 08:07", "重复"),
            )),
        ])
        data = self.preview(path, timezone_offset_minutes=480)
        export = data["export"]
        self.assertEqual(export["message_count"], 4)
        self.assertEqual(export["participant_count"], 2)
        self.assertEqual(export["participants"], ["甲", "乙 English"])
        self.assertEqual(export["coverage"]["format"], "native_txt")
        self.assertTrue(export["coverage"]["transcript_found"])
        self.assertEqual(export["coverage"]["unparsed_tail_bytes"], 0)
        messages = data["messages"]
        self.assertEqual(messages[0]["text"], "第一行\n\n第三行")
        self.assertIn("🙂", messages[1]["text"])
        # Identical tuples are both kept, with distinct source ids.
        self.assertEqual([item["text"] for item in messages[2:]], ["重复", "重复"])
        self.assertEqual(len({item["source_id"] for item in messages}), 4)
        self.assertTrue(all(item["source_id"].startswith("native:") for item in messages))
        self.assertEqual(len({item["source_id"] for item in messages}), 4)

    def test_bom_crlf_and_padded_hour_are_normalized(self):
        body = transcript(("甲", "2026年9月5日 8:05", "第一行\n第二行"))
        path = self.make_zip([(TRANSCRIPT, b"\xef\xbb\xbf" + body.replace(b"\n", b"\r\n"))])
        data = self.preview(path)
        self.assertEqual(data["messages"][0]["text"], "第一行\n第二行")
        self.assertEqual(data["messages"][0]["sent_at_local"], "2026-09-05T08:05")

    def test_malformed_prefix_or_invalid_date_fails_without_guessing(self):
        prefixed = self.make_zip([
            (TRANSCRIPT, b"\xe8\xaf\xb4\xe6\x98\x8e\n" + transcript(("甲", "2026年9月5日 08:05", "hi"))),
        ])
        self.expect_error(prefixed, ne.ERROR_PARSE_FAILED)
        invalid_date = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年2月30日 08:05", "hi")))])
        self.expect_error(invalid_date, ne.ERROR_PARSE_FAILED)
        empty = self.make_zip([(TRANSCRIPT, b"")])
        self.expect_error(empty, ne.ERROR_PARSE_FAILED)

    def test_name_length_and_control_character_limits(self):
        long_sender = self.make_zip([(TRANSCRIPT, transcript(("x" * 513, "2026年9月5日 08:05", "hi")))])
        self.expect_error(long_sender, ne.ERROR_PARSE_FAILED)
        nul_body = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "bad\0body")))])
        self.expect_error(nul_body, ne.ERROR_PARSE_FAILED)
        with patch.object(ne, "MAX_PARTICIPANTS", 1):
            many_senders = self.make_zip([(TRANSCRIPT, transcript(
                ("甲", "2026年9月5日 08:05", "a"),
                ("乙", "2026年9月5日 08:06", "b"),
            ))])
            self.expect_error(many_senders, ne.ERROR_LIMIT)

    def test_same_display_names_in_distinct_groups_stay_distinct(self):
        first = self.make_zip([
            (TRANSCRIPT, transcript(("同名", "2026年9月5日 08:05", "群一内容"))),
        ], name="群一.zip")
        second = self.make_zip([
            (TRANSCRIPT, transcript(("同名", "2026年9月5日 08:05", "群二内容"))),
        ], name="群二.zip")
        one = self.preview(first)["export"]
        two = self.preview(second)["export"]
        self.assertNotEqual(one["session_id"], two["session_id"])
        self.assertNotEqual(one["fingerprint"], two["fingerprint"])
        self.assertEqual(one["participant_count"], 1)
        self.assertEqual(two["participant_count"], 1)
        saved_one = self.save(first, one["fingerprint"])
        saved_two = self.save(second, two["fingerprint"])
        rows = {row["chat_id"]: row for row in self.service.list_chats(source="local_index")["chats"]}
        self.assertEqual(rows[saved_one["session_id"]]["title"], "群一")
        self.assertEqual(rows[saved_two["session_id"]]["title"], "群二")
        self.assertNotEqual(saved_one["session_id"], saved_two["session_id"])

    def test_fingerprint_is_deterministic_and_share_scoped(self):
        path = self.sample_zip()
        first = self.preview(path)["export"]
        second = self.preview(path)["export"]
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["session_id"], "native_share:" + first["fingerprint"][len("sha256:"):][:32])
        other = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "不同")))])
        self.assertNotEqual(first["fingerprint"], self.preview(other)["export"]["fingerprint"])


class TimezoneTest(NativeExportBase):
    def test_explicit_offsets_are_applied_without_system_assumptions(self):
        path = self.sample_zip()
        for offset, suffix in ((480, "+08:00"), (330, "+05:30"), (-300, "-05:00"), (0, "+00:00")):
            with self.subTest(offset=offset):
                data = self.preview(path, timezone_offset_minutes=offset)
                message = data["messages"][0]
                self.assertEqual(message["sent_at"], "2026-09-05T08:05:00" + suffix)
                self.assertEqual(message["sent_at_local"], "2026-09-05T08:05")
                self.assertEqual(data["export"]["timezone"],
                                 {"applied": True, "offset_minutes": offset})

    def test_missing_offset_keeps_local_time_only_with_warning(self):
        data = self.preview(self.sample_zip())
        message = data["messages"][0]
        self.assertIsNone(message["sent_at"])
        self.assertEqual(message["sent_at_local"], "2026-09-05T08:05")
        self.assertEqual(data["export"]["timezone"], {"applied": False})
        self.assertTrue(data["warnings"])
        self.assertNotIn("\0", " ".join(data["warnings"]))

    def test_invalid_offsets_are_argument_errors(self):
        path = self.sample_zip()
        for value in (True, 841, -841, 480.0, "480"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.preview(path, timezone_offset_minutes=value)
                self.assertEqual(str(caught.exception), ne.ERROR_INVALID_ARGUMENT)


class AttachmentTest(NativeExportBase):
    def test_attachment_metadata_kinds_and_count(self):
        path = self.make_zip([
            (TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "files"))),
            ("images/photo.png", b"png-bytes"),
            ("media/clip.mp4", b"mp4-bytes"),
            ("audio/voice.mp3", b"mp3"),
            ("docs/report.pdf", b"pdf"),
            ("bundle/archive.zip", b"zip"),
            ("misc/blob.bin", b"bin"),
            ("nested/", b""),
        ])
        export = self.preview(path)["export"]
        self.assertEqual(export["attachment_count"], 6)
        kinds = {item["name"]: item["kind"] for item in export["attachments"]}
        self.assertEqual(kinds["images/photo.png"], "image")
        self.assertEqual(kinds["media/clip.mp4"], "video")
        self.assertEqual(kinds["audio/voice.mp3"], "audio")
        self.assertEqual(kinds["docs/report.pdf"], "document")
        self.assertEqual(kinds["bundle/archive.zip"], "archive")
        self.assertEqual(kinds["misc/blob.bin"], "other")
        first = export["attachments"][0]
        self.assertEqual(first["bytes"], len(b"png-bytes"))

    def test_attachment_list_is_capped_but_count_is_honest(self):
        entries = [(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "many")))]
        entries += [(f"files/{index}.png", b"x") for index in range(51)]
        export = self.preview(self.make_zip(entries))["export"]
        self.assertEqual(export["attachment_count"], 51)
        self.assertEqual(len(export["attachments"]), ne.MAX_ATTACHMENTS)

    def test_attachment_refs_only_on_exact_line_match(self):
        path = self.make_zip([
            (TRANSCRIPT, transcript(
                ("甲", "2026年9月5日 08:05", "images/photo.png"),
                ("甲", "2026年9月5日 08:06", "photo.png"),
                ("甲", "2026年9月5日 08:07", "see images/photo.png here"),
                ("甲", "2026年9月5日 08:08", "line one\nphoto.png"),
            )),
            ("images/photo.png", b"png"),
        ])
        messages = self.preview(path)["messages"]
        self.assertEqual(messages[0]["attachment_refs"], ["images/photo.png"])
        self.assertEqual(messages[1]["attachment_refs"], ["images/photo.png"])
        self.assertEqual(messages[2]["attachment_refs"], [])
        self.assertEqual(messages[3]["attachment_refs"], ["images/photo.png"])


class PreviewWindowTest(NativeExportBase):
    def many_messages_zip(self, count=6):
        return self.make_zip([(TRANSCRIPT, transcript(*[
            ("甲", f"2026年9月5日 08:{index:02d}", f"消息 {index}") for index in range(count)
        ]))])

    def test_window_pagination_and_indexes(self):
        path = self.many_messages_zip()
        first = self.preview(path, offset=0, limit=2)
        self.assertEqual([item["index"] for item in first["messages"]], [0, 1])
        self.assertEqual(first["window"],
                         {"offset": 0, "limit": 2, "returned": 2, "has_more": True, "next_offset": 2})
        second = self.preview(path, offset=2, limit=2)
        self.assertEqual(second["window"]["next_offset"], 4)
        last = self.preview(path, offset=4, limit=2)
        self.assertEqual(last["window"]["has_more"], False)
        self.assertIsNone(last["window"]["next_offset"])
        self.assertEqual(last["export"]["fingerprint"], first["export"]["fingerprint"])

    def test_window_argument_limits(self):
        path = self.many_messages_zip()
        for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1},
                       {"offset": 100001}, {"offset": 19999, "limit": 200},
                       {"offset": 7, "limit": 2}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError) as caught:
                    self.preview(path, **kwargs)
                self.assertEqual(str(caught.exception), ne.ERROR_INVALID_ARGUMENT)

    def test_byte_cap_returns_fewer_messages_without_truncation(self):
        path = self.make_zip([(TRANSCRIPT, transcript(
            ("甲", "2026年9月5日 08:05", "x" * 60),
            ("甲", "2026年9月5日 08:06", "y" * 60),
        ))])
        with patch.object(ne, "MAX_PREVIEW_BYTES", 100):
            data = self.preview(path, limit=2)
        self.assertEqual(data["window"]["returned"], 1)
        self.assertEqual(data["window"]["limit"], 2)
        self.assertTrue(data["window"]["has_more"])
        self.assertEqual(data["window"]["next_offset"], 1)
        self.assertEqual(data["messages"][0]["text"], "x" * 60)

    def test_oversized_single_message_is_not_silently_truncated(self):
        with patch.object(ne, "MAX_MESSAGE_BYTES", 10):
            path = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "x" * 20)))])
            self.expect_error(path, ne.ERROR_LIMIT)
        big = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "x" * 32_001)))])
        self.expect_error(big, ne.ERROR_LIMIT)

    def test_label_is_bounded_display_text(self):
        path = self.make_zip([
            (TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "hi"))),
        ], name=("l" * 196) + ".zip")
        label = self.preview(path)["export"]["label"]
        self.assertEqual(len(label), ne.MAX_LABEL_CHARS)
        self.assertTrue(label)


class SaveAndProvenanceTest(NativeExportBase):
    def test_save_imports_idempotently_into_existing_sqlite(self):
        path = self.sample_zip()
        preview = self.preview(path, timezone_offset_minutes=480)["export"]
        fingerprint = preview["fingerprint"]
        first = self.save(path, fingerprint)
        self.assertEqual(first["imported"], 3)
        self.assertEqual(first["duplicates"], 0)
        self.assertFalse(first["already_imported"])
        self.assertFalse(first["live_read"])
        self.assertFalse(first["complete"])
        self.assertEqual(first["session_id"], preview["session_id"])
        self.assertEqual(first["fingerprint"], fingerprint)
        rows = self.store.get_messages("fixture-account", preview["session_id"], 10)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["source"] for row in rows}, {ne.SOURCE_KIND})
        self.assertTrue(all(row["sent_at"] is None for row in rows))
        self.assertEqual(len(self.store.get_messages("other-account", preview["session_id"])), 0)

        metadata = self.store.get_native_export(fingerprint)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["label"], "export-1")
        self.assertEqual(metadata["message_count"], 3)
        self.assertEqual(metadata["participant_count"], 2)
        self.assertEqual(metadata["attachment_count"], 0)
        self.assertIsNone(metadata["timezone_offset_minutes"])
        self.assertEqual(metadata["source_zip_name"], path.name)

        second = self.save(path, fingerprint)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 3)
        self.assertTrue(second["already_imported"])
        self.assertEqual(len(self.store.get_messages("fixture-account", preview["session_id"])), 3)

    def test_fingerprint_mismatch_imports_nothing(self):
        path = self.sample_zip()
        fingerprint = self.preview(path)["export"]["fingerprint"]
        changed = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "改过的内容")))])
        with self.assertRaises(ValueError) as caught:
            self.save(changed, fingerprint)
        self.assertEqual(str(caught.exception), ne.ERROR_CHANGED)
        self.assertEqual(self.service.list_chats(source="local_index")["chats"], [])
        self.assertIsNone(self.store.get_native_export(fingerprint))

    def test_save_argument_validation(self):
        for token, fingerprint in (("bad-token", "sha256:" + "0" * 64),
                                   ("0" * 32, "sha256:" + "A" * 64),
                                   ("0" * 32, "not-a-fingerprint")):
            with self.subTest(token=token, fingerprint=fingerprint):
                with self.assertRaises(ValueError) as caught:
                    self.service.save_native_export(token, fingerprint)
                self.assertEqual(str(caught.exception), ne.ERROR_INVALID_ARGUMENT)

    def test_archive_reserved_operation_is_not_implemented(self):
        with self.assertRaises(ValueError) as caught:
            self.service.archive_native_export("/tmp/anything")
        self.assertEqual(str(caught.exception), ne.ERROR_UNSUPPORTED)

    def test_list_chats_reports_native_provenance_and_respects_other_sources(self):
        path = self.sample_zip()
        preview = self.preview(path)["export"]
        saved = self.save(path, preview["fingerprint"])
        session_id = saved["session_id"]
        rows = {row["chat_id"]: row for row in self.service.list_chats(source="local_index")["chats"]}
        row = rows[session_id]
        self.assertEqual(row["title"], "export-1")
        self.assertEqual(row["source_kind"], "native_share")
        self.assertTrue(row["native_share_only"])
        self.assertFalse(row["manual_excerpt_only"])
        self.assertEqual(row["message_count"], 3)
        history = self.service.get_messages(session_id, 10, source="local_index")
        self.assertFalse(history["live_read"])
        self.assertEqual({message["source"] for message in history["messages"]}, {ne.SOURCE_KIND})

        self.store.add_messages("fixture-account", session_id, [
            {"source_id": "jsonl-1", "text": "独立导入", "source": "jsonl"}])
        mixed = {row["chat_id"]: row for row in self.service.list_chats(source="local_index")["chats"]}
        self.assertEqual(mixed[session_id]["source_kind"], "mixed")
        self.assertFalse(mixed[session_id]["native_share_only"])
        self.assertEqual(mixed[session_id]["title"], session_id)

        self.service.save_excerpt("手动标签", "我：手动保存")
        manual = {row["chat_id"]: row for row in self.service.list_chats(source="local_index")["chats"]}
        self.assertEqual(manual["手动标签"]["source_kind"], "manual_excerpt")
        self.assertTrue(manual["手动标签"]["manual_excerpt_only"])

    def test_preview_does_not_persist_chat_content(self):
        path = self.sample_zip()
        export = self.preview(path)["export"]
        self.assertEqual(self.store.get_messages("fixture-account", export["session_id"]), [])
        self.assertIsNone(self.store.get_native_export(export["fingerprint"]))
        self.assertEqual(self.service.list_chats(source="local_index")["chats"], [])

    def test_save_is_metadata_only_and_writes_no_attachment_files(self):
        path = self.make_zip([
            (TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "images/photo.png"))),
            ("images/photo.png", b"png-bytes"),
        ])
        export = self.preview(path)["export"]
        before = {item.name for item in self.root.iterdir()}
        saved = self.save(path, export["fingerprint"])
        after = {item.name for item in self.root.iterdir()}
        self.assertEqual(before, after)
        metadata = self.store.get_native_export(export["fingerprint"])
        self.assertEqual(metadata["attachment_count"], 1)
        messages = self.store.get_messages("fixture-account", saved["session_id"], 10)
        self.assertEqual([message["text"] for message in messages], ["images/photo.png"])

    def test_already_saved_flag_on_preview(self):
        path = self.sample_zip()
        fingerprint = self.preview(path)["export"]["fingerprint"]
        self.assertFalse(self.preview(path)["export"]["already_saved"])
        self.save(path, fingerprint)
        self.assertTrue(self.preview(path)["export"]["already_saved"])


class StatusAndIpcTest(NativeExportBase):
    def test_status_contract_fields(self):
        service = Service(self.store, AdapterStub(), "fixture-account")
        status = service.status()
        self.assertTrue(status["capabilities"]["native_export_import"])
        self.assertEqual(status["capabilities"]["native_export_contract_version"], 1)
        native = status["native_export"]
        self.assertEqual(native["contract_version"], 1)
        self.assertTrue(native["enabled"])
        self.assertEqual(native["grant_dir"], str(service.grant_dir))
        self.assertEqual(native["grant_ttl_seconds"], 600)
        self.assertEqual(native["max_archive_bytes"], 25_165_824)
        self.assertEqual(native["max_messages"], 20_000)
        self.assertEqual(native["max_preview_messages"], 200)
        self.assertEqual(native["max_preview_bytes"], 262_144)
        self.assertTrue(native["timezone_offset_required_for_absolute_time"])
        self.assertEqual(stat.S_IMODE(service.grant_dir.lstat().st_mode), 0o700)

    def test_ipc_whitelist_contains_native_methods_and_keeps_existing_ones(self):
        for method in ("native_export_preview", "native_export_save",
                       "save_excerpt", "forget_saved_excerpts", "status"):
            self.assertIn(method, METHODS)
        self.assertTrue(hasattr(self.service, "native_export_preview"))
        self.assertTrue(hasattr(self.service, "native_export_save"))

    def test_ipc_round_trip_uses_whitelisted_methods(self):
        path = self.sample_zip()
        token = self.mint(path)
        responses = {}

        def call(request):
            client, server = socket.socketpair()

            def worker():
                with server:
                    handle(server, self.service)

            thread = threading.Thread(target=worker)
            thread.start()
            with client:
                send(client, request)
                responses["value"] = receive(client)
            thread.join(5)

        call({"version": 1, "method": "native_export_preview",
              "args": [token], "kwargs": {"timezone_offset_minutes": 480, "limit": 2}})
        result = responses["value"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["export"]["message_count"], 3)
        self.assertEqual(len(result["result"]["messages"]), 2)
        save_token = self.mint(path, "save")
        call({"version": 1, "method": "native_export_save", "args": [],
              "kwargs": {"grant_token": save_token,
                         "expected_fingerprint": result["result"]["export"]["fingerprint"]}})
        saved = responses["value"]
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["result"]["imported"], 3)

    def test_ipc_exposes_only_stable_native_export_error_codes(self):
        """Companion needs to tell a corrupt ZIP from a bad request without raw text."""
        responses = {}

        def call(request):
            client, server = socket.socketpair()

            def worker():
                with server:
                    handle(server, self.service)

            thread = threading.Thread(target=worker)
            thread.start()
            with client:
                send(client, request)
                responses["value"] = receive(client)
            thread.join(5)

        call({"version": 1, "method": "native_export_preview",
              "args": ["f" * 32], "kwargs": {}})
        self.assertFalse(responses["value"]["ok"])
        self.assertEqual(responses["value"]["error"], "native_export_grant_invalid")

        # A generic validation error must stay the bounded `invalid_argument`.
        call({"version": 1, "method": "native_export_preview",
              "args": [self.mint(self.sample_zip())], "kwargs": {"limit": 0}})
        self.assertFalse(responses["value"]["ok"])
        self.assertEqual(responses["value"]["error"], "invalid_argument")


class CliTest(NativeExportBase):
    def run_cli(self, *args, env=None):
        root = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "WECHAT_PROVIDER_DB": str(self.root / "wechat.db"),
            "WECHAT_PROVIDER_READER": "unconfigured",
            "WECHAT_PROVIDER_ACCOUNT": "default",
            **(env or {}),
        }
        return subprocess.run([sys.executable, "-m", "wechat_provider.cli", *args],
                              capture_output=True, text=True, timeout=60, env=environment,
                              check=False)

    def test_cli_grant_preview_save_flow(self):
        path = self.sample_zip()
        grant = self.run_cli("grant-native-export", str(path), "--purpose", "preview",
                             "--grant-dir", str(self.grant_dir))
        self.assertEqual(grant.returncode, 0, grant.stderr)
        token = json.loads(grant.stdout)["grant_token"]
        self.assertEqual(len(token), 32)

        preview = self.run_cli("preview-native-export", token,
                               "--timezone-offset-minutes", "480", "--limit", "2")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        data = json.loads(preview.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["export"]["message_count"], 3)
        self.assertEqual(data["messages"][0]["sent_at"], "2026-09-05T08:05:00+08:00")

        grant = self.run_cli("grant-native-export", str(path), "--purpose", "save",
                             "--grant-dir", str(self.grant_dir))
        token = json.loads(grant.stdout)["grant_token"]
        save = self.run_cli("save-native-export", token,
                            "--fingerprint", data["export"]["fingerprint"])
        self.assertEqual(save.returncode, 0, save.stderr)
        receipt = json.loads(save.stdout)
        self.assertEqual((receipt["imported"], receipt["duplicates"]), (3, 0))
        self.assertFalse(receipt["already_imported"])

        stored = Store(self.root / "wechat.db")
        self.assertEqual(len(stored.get_messages("default", receipt["session_id"], 10)), 3)

    def test_cli_rejects_unsafe_archive_and_bad_grant(self):
        unsafe = self.make_zip([(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "hi"))),
                                ("../evil.png", b"x")])
        grant = self.run_cli("grant-native-export", str(unsafe), "--purpose", "preview",
                             "--grant-dir", str(self.grant_dir))
        token = json.loads(grant.stdout)["grant_token"]
        preview = self.run_cli("preview-native-export", token)
        self.assertEqual(preview.returncode, 2)
        self.assertIn(ne.ERROR_ARCHIVE_INVALID, preview.stderr)
        replay = self.run_cli("preview-native-export", token)
        self.assertEqual(replay.returncode, 2)
        self.assertIn(ne.ERROR_GRANT_INVALID, replay.stderr)


if __name__ == "__main__":
    unittest.main()

"""Offline proofs for the no-certificate helper build/configure path.

These tests never touch WeChat, TCC, the login state, or the user's Applications
directory. Cross-platform cases mock swiftc/codesign; the macOS case really compiles
and ad-hoc signs into a temporary directory (no certificate identity involved).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "wechat_provider_build_provider", ROOT / "macos" / "build_provider.py")
build_provider = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("wechat_provider_build_provider", build_provider)
_SPEC.loader.exec_module(build_provider)

BuildError = build_provider.BuildError


class FakeCommands:
    """Record build commands and fake `swiftc -o <helper>` output creation."""

    def __init__(self):
        self.commands: list[list[str]] = []

    def __call__(self, command, check=False, capture_output=True, text=True):
        self.commands.append(list(command))
        if command[0] == "fake-swiftc":
            target = Path(command[command.index("-o") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-helper-binary")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @property
    def codesign_commands(self):
        return [command for command in self.commands if command[0] == "fake-codesign"]


def valid_runtime(root: Path) -> dict:
    return {
        "python": "/absolute/venv/bin/python",
        "socket": str(root / "ipc" / "provider.sock"),
        "environment": {"WECHAT_PROVIDER_READER": "unconfigured"},
    }


def load_plist(path: Path) -> dict:
    return plistlib.loads(Path(path).read_bytes())


@unittest.skipUnless(os.name == "posix", "POSIX permission bits are asserted here")
class BuildShapesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.runtime_path = self.root / "helper" / "runtime.json"
        self.output = self.root / "out"
        self.fake = FakeCommands()

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **kwargs):
        with patch.object(build_provider.subprocess, "run", self.fake):
            return build_provider.build(
                kwargs.pop("runtime", valid_runtime(self.root)), self.output,
                swiftc="fake-swiftc", codesign="fake-codesign",
                runtime_path=kwargs.pop("runtime_path", self.runtime_path), **kwargs)

    def test_default_build_is_ad_hoc_without_certificate(self):
        summary = self.build()
        self.assertEqual(summary["mode"], "ad-hoc")
        self.assertFalse(summary["installed"])
        self.assertFalse(summary["tcc_changed"])
        app = self.output / build_provider.APP_NAME
        info = load_plist(app / "Contents" / "Info.plist")
        self.assertEqual(info["CFBundleIdentifier"], build_provider.BUNDLE_IDENTIFIER)
        self.assertEqual(info[build_provider.RUNTIME_PATH_KEY], str(self.runtime_path))
        # No certificate name ever reaches codesign: default identity is '-' (ad-hoc).
        sign_commands = self.fake.codesign_commands
        self.assertEqual([command[command.index("--sign") + 1] for command in sign_commands
                          if "--sign" in command], ["-"])
        self.assertTrue(any("--verify" in command and "--strict" in command
                            for command in sign_commands))
        # External runtime exists, is private, and no runtime JSON is bundled.
        self.assertTrue(self.runtime_path.is_file())
        self.assertEqual(self.runtime_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runtime_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(self.runtime_path.read_text())["python"],
                         "/absolute/venv/bin/python")
        self.assertFalse((app / "Contents" / "Resources" / "runtime.json").exists())
        self.assertEqual(app.parent, self.output)

    def test_ad_hoc_launch_agent_has_no_team_identifier_association(self):
        self.build()
        agent = load_plist(self.output / f"{build_provider.BUNDLE_IDENTIFIER}.plist")
        self.assertNotIn("AssociatedBundleIdentifiers", agent)
        self.assertEqual(agent["Label"], build_provider.BUNDLE_IDENTIFIER)
        self.assertIn(str(self.output / build_provider.APP_NAME / "Contents" / "MacOS" /
                          build_provider.EXECUTABLE_NAME), agent["ProgramArguments"])

    def test_certificate_mode_is_optional_and_keeps_association(self):
        summary = self.build(sign="Developer ID Application: Optional Distribution")
        self.assertEqual(summary["mode"], "certificate")
        agent = load_plist(self.output / f"{build_provider.BUNDLE_IDENTIFIER}.plist")
        self.assertEqual(agent["AssociatedBundleIdentifiers"],
                         [build_provider.BUNDLE_IDENTIFIER])
        signed = [command[command.index("--sign") + 1] for command
                  in self.fake.codesign_commands if "--sign" in command]
        self.assertEqual(signed, ["Developer ID Application: Optional Distribution"])

    def test_existing_app_is_never_overwritten_by_default(self):
        app = self.output / build_provider.APP_NAME
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "marker").write_text("keep")
        with self.assertRaisesRegex(BuildError, "refusing to overwrite or re-sign"):
            self.build()
        self.assertEqual(self.fake.commands, [])
        self.assertTrue((app / "Contents" / "marker").is_file())

    def test_force_rebuild_replaces_bundle(self):
        app = self.output / build_provider.APP_NAME
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "old-marker").write_text("old")
        self.build(force=True)
        self.assertFalse((app / "Contents" / "old-marker").exists())
        self.assertTrue((app / "Contents" / "MacOS" / build_provider.EXECUTABLE_NAME).is_file())

    def test_runtime_config_must_live_outside_bundle(self):
        inside = self.output / build_provider.APP_NAME / "Contents" / "runtime.json"
        with self.assertRaisesRegex(BuildError, "outside the app bundle"):
            self.build(runtime_path=inside)
        self.assertEqual(self.fake.commands, [])

    def test_configure_only_rewrites_runtime_without_rebuilding_helper(self):
        self.build()
        app = self.output / build_provider.APP_NAME
        helper = app / "Contents" / "MacOS" / build_provider.EXECUTABLE_NAME
        before = hashlib.sha256(helper.read_bytes()).hexdigest()
        self.assertEqual(self.fake.commands[-1][0], "fake-codesign")
        previous_commands = len(self.fake.commands)
        new_runtime = valid_runtime(self.root)
        new_runtime["environment"] = {"WECHAT_PROVIDER_READER": "wechat_mcp",
                                      "WECHAT_PROVIDER_WM_PYTHON": "/optional/sdk/python"}
        summary = build_provider.configure(new_runtime, self.output)
        self.assertEqual(len(self.fake.commands), previous_commands)  # no compile/sign
        self.assertFalse(summary["helper_rebuilt"])
        self.assertFalse(summary["helper_resigned"])
        self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), before)
        self.assertEqual(json.loads(self.runtime_path.read_text())["environment"],
                         new_runtime["environment"])

    def test_failed_force_rebuild_keeps_existing_app(self):
        self.build()
        app = self.output / build_provider.APP_NAME
        helper = app / "Contents" / "MacOS" / build_provider.EXECUTABLE_NAME
        before = hashlib.sha256(helper.read_bytes()).hexdigest()
        marker = app / "Contents" / "keep-marker"
        marker.write_text("old helper must survive a failed --force rebuild")

        def failing(command, check=False, capture_output=True, text=True):
            self.fake.commands.append(list(command))
            if command[0] == "fake-codesign" and "--sign" in command:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="simulated signing failure")
            if command[0] == "fake-swiftc":
                target = Path(command[command.index("-o") + 1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"new-helper")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (patch.object(build_provider.subprocess, "run", failing),
              self.assertRaisesRegex(BuildError, "command failed")):
            build_provider.build(valid_runtime(self.root), self.output, force=True,
                                 swiftc="fake-swiftc", codesign="fake-codesign",
                                 runtime_path=self.runtime_path)
        self.assertTrue(marker.is_file())  # previous bundle restored, not destroyed
        self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), before)

    def test_interrupted_previous_bundle_is_recovered_not_deleted(self):
        previous = self.output / f".{build_provider.APP_NAME}.previous"
        (previous / "Contents").mkdir(parents=True)
        (previous / "Contents" / "marker").write_text("recovered bundle")
        # A recovered bundle is an existing app: it must not be silently rebuilt.
        with self.assertRaisesRegex(BuildError, "refusing to overwrite or re-sign"):
            self.build()
        self.assertEqual(self.fake.commands, [])
        restored = self.output / build_provider.APP_NAME / "Contents" / "marker"
        self.assertTrue(restored.is_file())
        self.assertFalse(previous.exists())

    def test_failed_launch_agent_write_restores_previous_app(self):
        self.build()
        app = self.output / build_provider.APP_NAME
        helper = app / "Contents" / "MacOS" / build_provider.EXECUTABLE_NAME
        before = hashlib.sha256(helper.read_bytes()).hexdigest()
        marker = app / "Contents" / "keep-marker"
        marker.write_text("previous app must be restored")
        original_write = Path.write_bytes

        def flaky(path, data):
            if path.name == f"{build_provider.BUNDLE_IDENTIFIER}.plist":
                raise OSError(28, "No space left on device")
            return original_write(path, data)

        with (patch.object(Path, "write_bytes", flaky),
              self.assertRaisesRegex(BuildError, "build failed while writing files")):
            self.build(force=True)
        self.assertTrue(marker.is_file())
        self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), before)
        self.assertFalse((self.output / f".{build_provider.APP_NAME}.previous").exists())
        self.assertEqual(list(self.output.glob(".wechat-provider-build-*")), [])

    def test_output_path_that_is_a_file_reports_build_error(self):
        self.output.write_text("not a directory")
        with self.assertRaisesRegex(BuildError, "cannot create output directory"):
            self.build()
        self.assertEqual(self.fake.commands, [])

    def test_configure_only_rejects_build_only_flags(self):
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text(json.dumps(valid_runtime(self.root)))
        for extra in (["--force"], ["--sign", "Some Identity"],
                      ["--swiftc", "/nonexistent/swiftc"]):
            with (contextlib.redirect_stderr(io.StringIO()) as stderr,
                  self.assertRaises(SystemExit) as caught):
                build_provider.main(["--runtime", str(runtime_file), "--output",
                                     str(self.output), "--configure-only", *extra])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("only apply to a build", stderr.getvalue())

    def test_configure_output_does_not_claim_a_launch_agent(self):
        self.build()
        runtime_file = self.root / "runtime.json"
        runtime_file.write_text(json.dumps(valid_runtime(self.root)))
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            build_provider.main(["--runtime", str(runtime_file), "--output",
                                 str(self.output), "--configure-only"])
        output = stdout.getvalue()
        self.assertIn("were not touched", output)
        self.assertNotIn("Review the generated LaunchAgent", output)

    def test_configure_requires_installed_app(self):
        with self.assertRaisesRegex(BuildError, "not found; build it once"):
            build_provider.configure(valid_runtime(self.root), self.output)

    def test_configure_refuses_runtime_path_change(self):
        self.build()
        other = self.root / "other" / "runtime.json"
        with self.assertRaisesRegex(BuildError, "requires rebuilding the helper"):
            build_provider.configure(valid_runtime(self.root), self.output,
                                     runtime_path=other)

    def test_validate_runtime_rejects_unsafe_configuration(self):
        cases = {
            "relative python": {"python": "venv/bin/python",
                                "socket": str(self.root / "p.sock"), "environment": {}},
            "relative socket": {"python": "/abs/python", "socket": "ipc/p.sock",
                                "environment": {}},
            "unknown environment key": {"python": "/abs/python",
                                        "socket": str(self.root / "p.sock"),
                                        "environment": {"DYLD_INSERT_LIBRARIES": "/tmp/x"}},
            "non-string environment value": {"python": "/abs/python",
                                             "socket": str(self.root / "p.sock"),
                                             "environment": {"WECHAT_PROVIDER_ACCOUNT": 7}},
            "relative environment path": {
                "python": "/abs/python", "socket": str(self.root / "p.sock"),
                "environment": {"WECHAT_PROVIDER_DB": "relative/index.db"}},
            "long socket": {"python": "/abs/python",
                            "socket": "/" + "x" * build_provider.MAX_SOCKET_PATH_BYTES,
                            "environment": {}},
            "not an object": ["python"],
        }
        for name, runtime in cases.items():
            with self.subTest(name), self.assertRaises(BuildError):
                build_provider.validate_runtime(runtime)

    def test_private_directory_is_tightened(self):
        directory = self.root / "broad"
        directory.mkdir()
        directory.chmod(0o755)
        runtime = valid_runtime(self.root)
        build_provider.write_runtime(directory / "runtime.json", runtime)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "runtime.json").stat().st_mode & 0o777, 0o600)

    def test_default_runtime_path_is_outside_app_and_repo(self):
        path = build_provider.default_runtime_path(Path("/Users/example"))
        self.assertEqual(path, Path("/Users/example/Library/Application Support/"
                                    "wechat-provider/helper/runtime.json"))
        self.assertNotIn("WeChat Provider.app", str(path))


class NoCertificateRequirementTest(unittest.TestCase):
    def test_build_module_never_requires_a_certificate_argument(self):
        source = (ROOT / "macos" / "build_provider.py").read_text()
        self.assertNotIn("required=True, help='Stable codesign certificate", source)
        # Default identity is explicitly ad-hoc.
        self.assertIn('identity = "-" if sign is None else sign', source)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc")
                         and shutil.which("codesign"),
                         "requires macOS with Swift and codesign")
    def test_real_ad_hoc_build_and_config_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            output = root / "out"
            runtime_path = root / "helper" / "runtime.json"
            socket = root / "ipc" / "provider.sock"
            runtime = {"python": "/usr/bin/true", "socket": str(socket),
                       "environment": {"WECHAT_PROVIDER_READER": "unconfigured"}}
            summary = build_provider.build(runtime, output, runtime_path=runtime_path)
            self.assertEqual(summary["mode"], "ad-hoc")
            app = output / build_provider.APP_NAME
            helper = app / "Contents" / "MacOS" / build_provider.EXECUTABLE_NAME
            self.assertTrue(helper.is_file())
            signature = subprocess.run(["codesign", "-dv", str(app)],
                                       capture_output=True, text=True, check=False)
            self.assertIn("Signature=adhoc", signature.stderr + signature.stdout)
            self.assertEqual(subprocess.run(["codesign", "--verify", "--strict", str(app)],
                                            capture_output=True, check=False).returncode, 0)
            self.assertEqual(runtime_path.stat().st_mode & 0o777, 0o600)
            # Changing Python/reader config must not rebuild or re-sign the helper.
            before = hashlib.sha256(helper.read_bytes()).hexdigest()
            changed = {"python": "/usr/bin/true", "socket": str(socket),
                       "environment": {"WECHAT_PROVIDER_READER": "wechat_mcp"}}
            build_provider.configure(changed, output)
            self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), before)
            self.assertEqual(json.loads(runtime_path.read_text())["environment"],
                             changed["environment"])
            # Replacing an existing helper requires an explicit new identity decision.
            with self.assertRaisesRegex(BuildError, "refusing to overwrite or re-sign"):
                build_provider.build(runtime, output, runtime_path=runtime_path)
            # Positive control: the custom Info.plist path is used and the daemon
            # command is fixed (fake Python records its argv and exits 0).
            argv_file = root / "helper-argv.txt"
            fake_python = root / "fake-python"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > " + str(argv_file) + "\n")
            fake_python.chmod(0o755)
            control = {"python": str(fake_python), "socket": str(socket),
                       "environment": {"WECHAT_PROVIDER_READER": "unconfigured"}}
            build_provider.write_runtime(runtime_path, control)
            completed = subprocess.run([str(helper)], capture_output=True, timeout=15,
                                       check=False)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(argv_file.read_text().splitlines(),
                             ["-m", "wechat_provider.cli", "daemon", "--socket", str(socket)])
            # The helper itself also rejects environment keys outside the allowlist.
            runtime_path.write_text(json.dumps({
                "python": str(fake_python), "socket": str(socket),
                "environment": {"DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib"}}))
            runtime_path.chmod(0o600)
            self.assertEqual(subprocess.run([str(helper)], capture_output=True,
                                            timeout=15, check=False).returncode, 2)
            # NUL must be rejected for every allowlisted value, not only path keys.
            runtime_path.write_text(json.dumps({
                "python": str(fake_python), "socket": str(socket),
                "environment": {"WECHAT_PROVIDER_ACCOUNT": "bad\u0000value"}}))
            runtime_path.chmod(0o600)
            self.assertEqual(subprocess.run([str(helper)], capture_output=True,
                                            timeout=15, check=False).returncode, 2)
            # Helper refuses unsafe external config before starting any daemon.
            runtime_path.chmod(0o644)
            rejected = subprocess.run([str(helper)], capture_output=True, check=False)
            self.assertEqual(rejected.returncode, 2)
            runtime_path.unlink()
            missing = subprocess.run([str(helper)], capture_output=True, check=False)
            self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()

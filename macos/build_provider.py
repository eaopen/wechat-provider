"""Build/configure a dedicated local Provider helper; no signing certificate required.

Default mode builds an ad-hoc signed ``WeChat Provider.app`` (``codesign --sign -``,
not a certificate or Apple Developer identity).  Its runtime configuration lives
outside the bundle, so Python Provider / Wechat-MCP SDK / reader environment changes
never require recompiling or re-signing the helper.  Certificate signing is optional
for a future distribution build.

This script only writes reviewable artifacts: it never installs or enables the
LaunchAgent, never grants permissions, and never changes TCC or WeChat data.
Ad-hoc code identity changes whenever the helper is rebuilt (Apple TN3127), so a
rebuild may require the user to grant file access again; that is never promised.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

APP_NAME = "WeChat Provider.app"
EXECUTABLE_NAME = "WeChatProvider"
BUNDLE_IDENTIFIER = "com.eaopen.wechat-provider"
RUNTIME_PATH_KEY = "WCProviderRuntimePath"

# Only these fixed provider/reader settings may reach the daemon environment.
ALLOWED_ENVIRONMENT = frozenset({
    "WECHAT_PROVIDER_READER", "WECHAT_PROVIDER_WM_PYTHON", "WECHAT_PROVIDER_WM_STATE_DIR",
    "WECHAT_PROVIDER_WM_SOURCE_ID", "WECHAT_PROVIDER_GB_EXECUTABLE",
    "WECHAT_PROVIDER_GB_PROFILE", "WECHAT_PROVIDER_GB_SOURCE_ID", "WECHAT_PROVIDER_DB",
    "WECHAT_PROVIDER_ACCOUNT",
})
# Values of these keys, when present, must be absolute paths.
PATH_ENVIRONMENT = frozenset({
    "WECHAT_PROVIDER_WM_PYTHON", "WECHAT_PROVIDER_WM_STATE_DIR",
    "WECHAT_PROVIDER_GB_EXECUTABLE", "WECHAT_PROVIDER_DB",
})
MAX_SOCKET_PATH_BYTES = 103  # macOS sockaddr_un.sun_path is 104 bytes including NUL


class BuildError(Exception):
    """A reviewable, user-facing build/configuration failure."""


def default_runtime_path(home: Path | None = None) -> Path:
    """External config path: user-private, outside the app bundle and repo."""
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Application Support" / "wechat-provider" / "helper" / "runtime.json"


def _absolute(value: Path) -> Path:
    """Normalize an absolute user path; allow standard firmlinks (e.g. /var -> /private/var)."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BuildError(f"path must be absolute: {value}")
    path = Path(os.path.abspath(path))
    if path.is_symlink():
        raise BuildError(f"refusing to use a symlinked path: {path}")
    # Resolve only existing ancestors (macOS /var is a firmlink); keep the final
    # component as given so we never silently write through a final symlink.
    return path.parent.resolve() / path.name


def _private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise BuildError(f"runtime config directory is not a real directory: {directory}")
    if hasattr(os, "getuid"):  # POSIX only; the helper itself is macOS-only.
        info = directory.stat()
        if info.st_uid != os.getuid():
            raise BuildError(f"runtime config directory is not owned by the current user: {directory}")
        if info.st_mode & 0o077:
            directory.chmod(0o700)


def validate_runtime(runtime: object) -> dict:
    """Validate and normalize the fixed runtime JSON (no secrets, no arbitrary env)."""
    if not isinstance(runtime, dict):
        raise BuildError("runtime configuration must be a JSON object")
    python, socket = runtime.get("python"), runtime.get("socket")
    for name, value in (("python", python), ("socket", socket)):
        if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
            raise BuildError(f"runtime '{name}' must be an absolute path")
    try:
        socket_bytes = len(socket.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise BuildError("runtime 'socket' must be valid UTF-8") from error
    if socket_bytes > MAX_SOCKET_PATH_BYTES:
        raise BuildError(
            f"runtime 'socket' is too long for a macOS Unix socket "
            f"({socket_bytes} > {MAX_SOCKET_PATH_BYTES} bytes)")
    environment = runtime.get("environment", {})
    if not isinstance(environment, dict):
        raise BuildError("runtime 'environment' must be a JSON object")
    normalized = {}
    for key, value in environment.items():
        if key not in ALLOWED_ENVIRONMENT:
            raise BuildError(f"runtime environment key is not allowed: {key}")
        if not isinstance(value, str) or "\0" in value:
            raise BuildError(f"runtime environment value must be a string: {key}")
        if key in PATH_ENVIRONMENT and value and not value.startswith("/"):
            raise BuildError(f"runtime environment path is not absolute: {key}")
        normalized[key] = value
    return {"python": python, "socket": socket, "environment": normalized}


def load_runtime(source: Path) -> dict:
    try:
        return validate_runtime(json.loads(Path(source).expanduser().read_text()))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read runtime configuration: {error}") from error


def write_runtime(runtime_path: Path, runtime: dict) -> Path:
    """Atomically write user-private external config (mode 0600) without touching the app."""
    runtime = validate_runtime(runtime)
    path = _absolute(runtime_path)
    if path.is_symlink():
        raise BuildError(f"refusing to replace a symlinked runtime configuration: {path}")
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(runtime, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise BuildError(f"required command not found: {command[0]}") from error
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()[:1000]
        raise BuildError(f"command failed ({completed.returncode}): {command[0]}: {detail}")
    return completed


def build(runtime: dict, output: Path, *, sign: str | None = None,
          runtime_path: Path | None = None, force: bool = False,
          swiftc: str = "swiftc", codesign: str = "codesign",
          source: Path | None = None) -> dict:
    """Build the reviewable ad-hoc (default) or certificate-signed app + plist."""
    runtime = validate_runtime(runtime)
    output_path = Path(os.path.abspath(Path(output).expanduser()))
    app = _absolute(output_path / APP_NAME)
    if app.is_symlink():
        raise BuildError(f"refusing to replace a symlinked app bundle: {app}")
    if app.exists() and not app.is_dir():
        raise BuildError(f"app path exists but is not a bundle directory: {app}")
    if app.exists() and not force:
        raise BuildError(
            "existing WeChat Provider.app; refusing to overwrite or re-sign it. "
            "A rebuild changes its ad-hoc code identity/CDHash; macOS may ask the user to "
            "grant file access again, and that is not guaranteed. Pass --force only if you "
            "accept a new helper identity.")
    config_path = (_absolute(runtime_path) if runtime_path is not None
                   else _absolute(default_runtime_path()))
    if config_path == app or config_path.is_relative_to(app):
        raise BuildError("runtime configuration must live outside the app bundle")
    if sign is not None and not sign.strip():
        raise BuildError("empty signing identity; omit --sign for ad-hoc or pass a real identity")

    if app.exists():  # only reachable with explicit --force
        shutil.rmtree(app)
    try:
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BUNDLE_IDENTIFIER,
            "CFBundleName": "WeChat Provider",
            "CFBundleExecutable": EXECUTABLE_NAME,
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "CFBundleShortVersionString": "1.0",
            "LSUIElement": True,
            RUNTIME_PATH_KEY: str(config_path),
        }))
        swift_source = Path(source) if source is not None else Path(__file__).with_name("ProviderMain.swift")
        executable = contents / "MacOS" / EXECUTABLE_NAME
        _run([swiftc, str(swift_source), "-o", str(executable)])
        identity = "-" if sign is None else sign
        _run([codesign, "--force", "--sign", identity, str(app)])
        _run([codesign, "--verify", "--strict", str(app)])
        write_runtime(config_path, runtime)

        agent = {
            "Label": BUNDLE_IDENTIFIER,
            "ProgramArguments": [str(executable)],
            "RunAtLoad": True,
            "ThrottleInterval": 30,
            "KeepAlive": {"SuccessfulExit": False},
        }
        if sign is not None:
            # Team-identifier based association is only meaningful for certificate builds.
            agent["AssociatedBundleIdentifiers"] = [BUNDLE_IDENTIFIER]
        agent_path = output_path / f"{BUNDLE_IDENTIFIER}.plist"
        agent_path.write_bytes(plistlib.dumps(agent))
    except Exception:
        # Never leave a half-built bundle that looks installable.
        shutil.rmtree(app, ignore_errors=True)
        raise
    return {
        "mode": "ad-hoc" if sign is None else "certificate",
        "app": str(app),
        "runtime": str(config_path),
        "launch_agent": str(agent_path),
        "helper_rebuilt": True,
        "installed": False,
        "tcc_changed": False,
    }


def configure(runtime: dict, output: Path, *, runtime_path: Path | None = None) -> dict:
    """Update external runtime config only; never rebuild or re-sign the helper."""
    app = _absolute(Path(os.path.abspath(Path(output).expanduser())) / APP_NAME)
    info_path = app / "Contents" / "Info.plist"
    if not app.is_dir() or not info_path.is_file():
        raise BuildError("installed WeChat Provider.app not found; build it once before configuring")
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise BuildError(f"cannot read installed app Info.plist: {error}") from error
    configured = info.get(RUNTIME_PATH_KEY)
    if not isinstance(configured, str) or not configured.startswith("/"):
        raise BuildError(
            "installed app has no external runtime configuration; rebuild it once with this script")
    if runtime_path is not None and _absolute(runtime_path) != _absolute(configured):
        raise BuildError("changing the runtime configuration path requires rebuilding the helper")
    runtime = validate_runtime(runtime)
    path = write_runtime(configured, runtime)
    return {
        "mode": "configured",
        "app": str(app),
        "runtime": str(path),
        "helper_rebuilt": False,
        "helper_resigned": False,
        "tcc_changed": False,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build an ad-hoc (default, no certificate) macOS Provider helper, or "
                    "update only its external runtime configuration.")
    parser.add_argument("--runtime", type=Path, required=True,
                        help="Private JSON: python, socket, environment")
    parser.add_argument("--output", type=Path, required=True,
                        help="Directory containing (or to contain) 'WeChat Provider.app'")
    parser.add_argument("--configure-only", action="store_true",
                        help="Rewrite external runtime JSON only; never rebuild/re-sign the app")
    parser.add_argument("--sign", default=None,
                        help="Optional codesign identity for distribution; default is ad-hoc")
    parser.add_argument("--runtime-path", type=Path, default=None,
                        help="Override external runtime JSON path (absolute, outside the app)")
    parser.add_argument("--force", action="store_true",
                        help="Allow replacing an existing app (changes code identity; may require "
                             "granting file access again)")
    parser.add_argument("--swiftc", default="swiftc")
    parser.add_argument("--codesign", default="codesign")
    args = parser.parse_args(argv)
    try:
        runtime = load_runtime(args.runtime)
        if args.configure_only:
            summary = configure(runtime, args.output, runtime_path=args.runtime_path)
        else:
            summary = build(runtime, args.output, sign=args.sign,
                            runtime_path=args.runtime_path, force=args.force,
                            swiftc=args.swiftc, codesign=args.codesign)
    except BuildError as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Review the generated LaunchAgent. This script did not install, enable, or "
          "authorize anything and did not change TCC.")


if __name__ == "__main__":
    main()

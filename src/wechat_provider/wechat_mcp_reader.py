"""Explicit existing Wechat-MCP SDK; no setup, archive refresh or GUI calls."""
import os
from pathlib import Path
from .greenbubbles_reader import GreenBubblesReader


class WechatMCPReader(GreenBubblesReader):
    backend = "wechat_mcp"
    schema = "wechat-provider.query.v1"

    def __init__(self, executable, state_dir, source_identity):
        super().__init__(executable, "bound", source_identity, timeout=20)
        self.state_dir = state_dir

    def status(self):
        result = super().status()
        state = Path(self.state_dir)
        valid = bool(self.state_dir and state.is_absolute() and
                     all((state / name).is_file() for name in ("config.json", "keys.json", "identity.json")))
        result["configured"] = result["configured"] and valid
        result["available"] = result["installed"] and result["configured"]
        return result

    def _run(self, args, query=None):
        worker = str(Path(__file__).with_name("wechat_mcp_worker.py"))
        return super()._run([worker, "--state-dir", self.state_dir, *args], query)


def configured_reader():
    selected = os.environ.get("WECHAT_PROVIDER_READER", "greenbubbles")
    if selected == "wechat_mcp":
        return WechatMCPReader(os.environ.get("WECHAT_PROVIDER_WM_PYTHON", ""),
                              os.environ.get("WECHAT_PROVIDER_WM_STATE_DIR", ""),
                              os.environ.get("WECHAT_PROVIDER_WM_SOURCE_ID", ""))
    if selected != "greenbubbles":
        return GreenBubblesReader()  # unknown configuration never falls back to a live reader
    return GreenBubblesReader.from_env()

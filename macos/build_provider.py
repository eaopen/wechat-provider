"""Build a dedicated signed app plus reviewable LaunchAgent; does not install/enable it."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--runtime', type=Path, required=True, help='Private JSON: python, socket, environment')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--sign', required=True, help='Stable codesign certificate identity; no ad-hoc identity')
a = parser.parse_args()
if a.sign == '-':
    parser.error('Ad-hoc signing cannot establish the stable production identity')
runtime = json.loads(a.runtime.read_text())
allowed = {'WECHAT_PROVIDER_READER', 'WECHAT_PROVIDER_WM_PYTHON', 'WECHAT_PROVIDER_WM_STATE_DIR',
           'WECHAT_PROVIDER_WM_SOURCE_ID', 'WECHAT_PROVIDER_GB_EXECUTABLE',
           'WECHAT_PROVIDER_GB_PROFILE', 'WECHAT_PROVIDER_GB_SOURCE_ID', 'WECHAT_PROVIDER_DB',
           'WECHAT_PROVIDER_ACCOUNT'}
if (not Path(runtime['python']).is_absolute() or not Path(runtime['socket']).is_absolute()
        or set(runtime['environment']) - allowed):
    parser.error('Invalid fixed runtime configuration')
app = a.output.resolve() / 'WeChat Provider.app'
contents = app / 'Contents'
(contents/'MacOS').mkdir(parents=True)
(contents/'Resources').mkdir()
identifier = 'com.eaopen.wechat-provider'
(contents/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier,
    'CFBundleName': 'WeChat Provider', 'CFBundleExecutable': 'WeChatProvider',
    'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True}))
(contents/'Resources/runtime.json').write_text(json.dumps(runtime))
subprocess.run(['swiftc', str(Path(__file__).with_name('ProviderMain.swift')), '-o',
                str(contents/'MacOS/WeChatProvider')], check=True)
subprocess.run(['codesign', '--force', '--sign', a.sign, str(app)], check=True)
subprocess.run(['codesign', '--verify', '--strict', str(app)], check=True)
agent = {'Label': identifier, 'ProgramArguments': [str(contents/'MacOS/WeChatProvider')],
         'AssociatedBundleIdentifiers': [identifier], 'RunAtLoad': True,
         'ThrottleInterval': 30, 'KeepAlive': {'SuccessfulExit': False}}
(a.output/'com.eaopen.wechat-provider.plist').write_bytes(plistlib.dumps(agent))
print('Built signed Provider and LaunchAgent; installation and TCC acceptance pending')

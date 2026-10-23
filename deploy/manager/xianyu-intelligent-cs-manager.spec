# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile build for the privileged systemd manager."""

import ast
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
VERSION_FILE = ROOT / "backend/version.py"


def build_version():
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"), filename=str(VERSION_FILE))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                return value
    raise ValueError("backend/version.py has no literal VERSION")


BUILD_VERSION = build_version()
BUNDLE_FILES = (
    "backend/account_storage.py",
    "backend/db.py",
    "backend/docker_update_protocol.py",
    "backend/platform_update.py",
    "backend/runtime_settings.py",
    "backend/standalone_runtime.py",
    "backend/update_maintenance.py",
    "backend/update_progress.py",
    "backend/version.py",
    "deploy/updater/updater.py",
    "deploy/update-signing.pub",
    "deploy/systemd/xianyu-intelligent-cs.service",
    "deploy/systemd/xianyu-intelligent-cs-consumer.service",
    "deploy/systemd/xianyu-intelligent-cs-updater.service",
    "deploy/systemd/xianyu-intelligent-cs-updater.path",
    "deploy/xianyu-intelligent-cs-bot-logrotate.conf",
)
datas = [(str(ROOT / relative), str(Path(relative).parent)) for relative in BUNDLE_FILES]
hiddenimports = [
    "deploy.updater.updater",
    "backend.version",
    "requests",
    "cryptography",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
]

analysis = Analysis(
    [str(ROOT / "deploy/manager/__main__.py")],
    pathex=[str(ROOT), str(ROOT / "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="xianyu-intelligent-cs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
# BUILD_VERSION is deliberately read from backend/version.py at spec evaluation;
# release automation may use it to label the onefile artifact directory.

"""Fixed identities and filesystem locations for the systemd manager."""

from __future__ import annotations

from pathlib import Path


PRODUCT = "xianyu-intelligent-cs"
MANAGER_PROTOCOL = 1
UPDATER_PROTOCOL = 1

INSTALL_ROOT = Path("/opt/xianyu-intelligent-cs")
RELEASES_DIR = INSTALL_ROOT / "releases"
CURRENT_LINK = INSTALL_ROOT / "current"
MANAGER_ROOT = INSTALL_ROOT / "manager"
MANAGER_RELEASES_DIR = MANAGER_ROOT / "releases"
MANAGER_CURRENT_LINK = MANAGER_ROOT / "current"
MANAGER_INSTALL_PATH = Path("/usr/local/bin/xianyu-intelligent-cs")
RUNTIME_DIR = INSTALL_ROOT / "runtime"
UPDATER_BUNDLE_ROOT = INSTALL_ROOT / "updater"
STATE_DIR = Path("/var/lib/xianyu-intelligent-cs")
UPDATE_QUEUE_DIR = Path("/var/lib/xianyu-intelligent-cs-updates")
UPDATER_STATE_DIR = Path("/var/lib/xianyu-intelligent-cs-updater")
PUBLIC_KEY_RELATIVE = Path("deploy/update-signing.pub")
SYSTEMCTL_BINARY = Path("/usr/bin/systemctl")

API_SERVICE = "xianyu-intelligent-cs.service"
CONSUMER_SERVICE = "xianyu-intelligent-cs-consumer.service"
UPDATER_SERVICE = "xianyu-intelligent-cs-updater.service"
UPDATER_PATH_UNIT = "xianyu-intelligent-cs-updater.path"
APPLICATION_UNITS = (API_SERVICE, CONSUMER_SERVICE)
MANAGED_UNITS = APPLICATION_UNITS + (UPDATER_SERVICE, UPDATER_PATH_UNIT)

ROOT_COMMANDS = frozenset({
    "install",
    "start",
    "stop",
    "restart",
    "internal:consume-intent",
    "internal:initialize",
    "internal:import-baseline",
    "internal:resume",
})

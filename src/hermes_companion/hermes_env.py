"""Read the minimum existing Hermes settings needed by the companion."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True, repr=False)
class HermesEnvironment:
    target_user_id: str = field(repr=False)
    api_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "HermesEnvironment(target_user_id=<redacted>, api_key=<redacted>)"


def load_hermes_environment(path: str | Path) -> HermesEnvironment:
    """Load the allowlisted Weixin target and MiMo key without echoing either."""
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise FileNotFoundError("Hermes environment file is not available")
    selected: dict[str, str] = {}
    wanted = {"WEIXIN_HOME_CHANNEL", "WEIXIN_ALLOWED_USERS", "XIAOMI_API_KEY"}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted:
            continue
        try:
            parts = shlex.split(raw_value.strip(), comments=False, posix=True)
        except ValueError:
            continue
        selected[key] = parts[0] if parts else ""

    target = selected.get("WEIXIN_HOME_CHANNEL", "").strip()
    allowed = {
        value.strip()
        for value in selected.get("WEIXIN_ALLOWED_USERS", "").split(",")
        if value.strip()
    }
    if not target or target not in allowed:
        raise ValueError("configured target is not in the Weixin allowlist")
    api_key = selected.get("XIAOMI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Xiaomi MiMo API key is missing from the Hermes environment")
    return HermesEnvironment(target_user_id=target, api_key=api_key)

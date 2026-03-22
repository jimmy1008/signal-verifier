"""
設定檔載入
"""

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str = "config/config.yaml") -> dict[str, Any]:
    """載入 YAML 設定檔"""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"找不到設定檔 {path}，請從 config/config.example.yaml 複製並填入你的設定"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

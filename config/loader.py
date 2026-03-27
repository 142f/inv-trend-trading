from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from config.models import Settings


def load_settings(path: Optional[str] = None) -> Settings:
    load_dotenv()
    cfg_path = Path(path or "config/settings.yaml")
    if not cfg_path.exists():
        return Settings()
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    settings = Settings.from_dict(data)
    return settings


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)

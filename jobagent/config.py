"""Configuration loading. Everything private lives under DATA_DIR."""
from __future__ import annotations

import copy
import os
import shutil
import threading
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
DATA_DIR = Path(os.environ.get("JOBAGENT_DATA", ROOT / "data")).resolve()
CONFIG_PATH = DATA_DIR / "config.yaml"
DEFAULT_PATH = PKG_DIR / "default_config.yaml"
COVER_TEMPLATE_PATH = DATA_DIR / "cover_letter.txt"

MODES = ("auto_strong", "approve_all", "off")

_lock = threading.Lock()
_cache: dict = {"mtime": None, "cfg": None}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "screenshots").mkdir(exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    if not CONFIG_PATH.exists():
        shutil.copyfile(DEFAULT_PATH, CONFIG_PATH)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _normalize(cfg: dict) -> dict:
    # YAML 1.1 reads an unquoted `off` as the boolean False; accept that as "off".
    if cfg.get("apply", {}).get("mode") is False:
        cfg["apply"]["mode"] = "off"
    return cfg


def load_config() -> dict:
    ensure_data_dir()
    with _lock:
        mtime = CONFIG_PATH.stat().st_mtime_ns
        if _cache["mtime"] != mtime:
            defaults = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
            user = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _cache["cfg"] = _normalize(_merge(defaults, user))
            _cache["mtime"] = mtime
        return copy.deepcopy(_cache["cfg"])


def config_text() -> str:
    ensure_data_dir()
    return CONFIG_PATH.read_text(encoding="utf-8")


def validate_config_text(text: str) -> tuple[str | None, dict | None]:
    """Return (error, parsed). error is None when the text is acceptable."""
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return f"That is not valid YAML: {exc}", None
    if not isinstance(parsed, dict):
        return "The config must be a YAML mapping (key: value pairs).", None
    merged = _normalize(_merge(yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8")), parsed))

    for key in ("titles", "skills", "nice_to_have", "exclude_title", "exclude_description", "locations",
                "remote_regions"):
        if not isinstance(merged["search"].get(key), list):
            return f"search.{key} must be a list.", None
    for key in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"):
        if not isinstance(merged["sources"].get(key), list):
            return f"sources.{key} must be a list.", None
    if merged["apply"].get("mode") not in MODES:
        return f"apply.mode must be one of: {', '.join(MODES)}.", None
    for section, key in (
        ("search", "min_score"), ("search", "auto_apply_score"), ("search", "max_age_days"),
        ("apply", "daily_limit"), ("apply", "max_per_company"), ("apply", "delay_seconds"),
        ("tracking", "ghosted_after_days"), ("schedule", "search_every_minutes"),
        ("schedule", "inbox_every_minutes"), ("server", "port"),
    ):
        try:
            int(merged[section][key])
        except (TypeError, ValueError):
            return f"{section}.{key} must be a number.", None
    return None, parsed


def save_config_text(text: str) -> str | None:
    error, _ = validate_config_text(text)
    if error:
        return error
    ensure_data_dir()
    CONFIG_PATH.write_text(text, encoding="utf-8")
    return None


def resume_path() -> Path | None:
    for ext in ("pdf", "docx", "doc"):
        path = DATA_DIR / f"resume.{ext}"
        if path.exists():
            return path
    return None

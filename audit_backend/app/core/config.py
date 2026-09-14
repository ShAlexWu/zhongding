"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # app/core/config.py -> backend/
PROJECT_ROOT = BACKEND_DIR.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Never logs values."""
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    rules_path: Path
    vlm_base_url: str
    vlm_model: str
    dashscope_api_key: str
    vlm_dry_run: bool
    png_cache_dir: Path
    upload_dir: Path = Path("/tmp/zhongji-audit-uploads")
    trademark_vlm_provider: str = "dashscope"
    codex_cli_path: str = "codex"
    codex_model: str = "gpt-5.6-sol"
    standards_live_lookup: bool = True
    standards_timeout_s: float = 6.0
    page_render_dpi: int = 150
    png_max_long_edge: int = 2200
    vlm_concurrency: int = 3
    vlm_temperature: float = 0.0
    vlm_max_attempts: int = 3  # 1 initial + 2 retries
    vlm_retry_backoff_s: list[int] = field(default_factory=lambda: [3, 9])
    vlm_timeout_s: float = 330.0  # direct PDF first byte may take up to 300s
    api_prefix: str = "/api/v1"
    app_version: str = "1.0.0"


def load_settings() -> Settings:
    _load_dotenv()
    data_dir = Path(os.environ.get("DATA_DIR", str(PROJECT_ROOT / "api_json")))
    db_path = Path(os.environ.get("DB_PATH", str(BACKEND_DIR / "app.db")))
    png_cache_dir = Path(os.environ.get("PNG_CACHE_DIR", str(BACKEND_DIR / ".png_cache")))
    upload_dir = Path(os.environ.get("UPLOAD_DIR", str(BACKEND_DIR / "uploads")))
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        upload_dir=upload_dir,
        rules_path=BACKEND_DIR / "app" / "data" / "rules_40.json",
        vlm_base_url=os.environ.get(
            "VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        vlm_model=os.environ.get("VLM_MODEL", "qwen3.8-flash"),
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        vlm_dry_run=os.environ.get("VLM_DRY_RUN", "") in ("1", "true", "TRUE", "yes"),
        png_cache_dir=png_cache_dir,
        trademark_vlm_provider=os.environ.get("TRADEMARK_VLM_PROVIDER", "dashscope").strip().lower(),
        codex_cli_path=os.environ.get("CODEX_CLI_PATH", "codex").strip() or "codex",
        codex_model=os.environ.get("CODEX_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol",
        standards_live_lookup=os.environ.get("STANDARDS_LIVE_LOOKUP", "1")
        not in ("0", "false", "FALSE", "no"),
        standards_timeout_s=float(os.environ.get("STANDARDS_TIMEOUT_S", "6")),
        vlm_timeout_s=float(os.environ.get("VLM_TIMEOUT_S", "330")),
    )


settings: Settings = load_settings()

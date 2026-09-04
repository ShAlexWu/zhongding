"""Pytest fixtures: isolated temp DB + env for VLM dry-run."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
_tmpdir = tempfile.mkdtemp(prefix="zhongji_test_")
DATA_DIR = Path(_tmpdir) / "sample"
with zipfile.ZipFile(BACKEND_DIR.parent / "局部图片" / "密封件" / "zhongji-v4-audit-sample.zip") as archive:
    archive.extractall(DATA_DIR)
os.environ["DB_PATH"] = str(Path(_tmpdir) / "test.db")
os.environ["PNG_CACHE_DIR"] = str(Path(_tmpdir) / "png")
os.environ["UPLOAD_DIR"] = str(Path(_tmpdir) / "uploads")
os.environ["VLM_DRY_RUN"] = "1"
os.environ["DATA_DIR"] = str(DATA_DIR)
# Set (not pop) the key: an empty value occupies the env slot so that
# config._load_dotenv cannot re-inject the real key from backend/.env
# (dotenv only fills keys that are absent). Real server runs are unaffected.
os.environ["DASHSCOPE_API_KEY"] = ""


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture(scope="session")
def real_api_path(data_dir: Path) -> Path:
    return data_dir / "000A22G1G_总装配_21A-00_api.json"


@pytest.fixture(scope="session")
def real_spatial_path(data_dir: Path) -> Path:
    return data_dir / "000A22G1G_总装配_21A-00_spatial.json"


@pytest.fixture(scope="session")
def real_docx_path(data_dir: Path) -> Path:
    return data_dir / "CIMC20-21A.docx"


@pytest.fixture(scope="session")
def real_pdf_path(data_dir: Path) -> Path:
    return data_dir / "000A22G1G_总装配_21A-00_api.pdf"

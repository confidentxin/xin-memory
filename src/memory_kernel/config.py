from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

EMBED_DIM = 1536


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _load_env_files() -> None:
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    # src/memory_kernel/config.py -> repo root
    if len(here.parents) >= 3:
        candidates.append(here.parents[2] / ".env")
    for path in candidates:
        load_dotenv(path)


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str
    openai_base_url: str
    openai_extract_model: str
    openai_embed_model: str
    api_host: str
    api_port: int
    embed_dim: int = EMBED_DIM

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key.strip())


def load_settings() -> Settings:
    _load_env_files()
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://memory:memory@127.0.0.1:54329/memory",
        ),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_extract_model=os.getenv("OPENAI_EXTRACT_MODEL", "gpt-4o-mini"),
        openai_embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
        api_host=os.getenv("MEMORY_API_HOST", "127.0.0.1"),
        api_port=int(os.getenv("MEMORY_API_PORT", "8741")),
    )


def sql_paths() -> list[Path]:
    directory = Path(__file__).resolve().parent / "sql"
    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise FileNotFoundError(f"找不到 schema 目录: {directory}")
    return paths


def sql_path() -> Path:
    return sql_paths()[0]

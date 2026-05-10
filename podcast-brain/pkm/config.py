from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class PodcastIndexConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    user_agent: str = "podcast-brain/0.1"
    max_episodes_per_request: int = 1000


class InboxConfig(BaseModel):
    default_watch_dir: str = "data/inbox"
    file_settle_seconds: float = 2.0


class IngestConfig(BaseModel):
    podcastindex: PodcastIndexConfig = Field(default_factory=PodcastIndexConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)


class PathsConfig(BaseModel):
    audio_dir: str = "data/audio"
    transcripts_dir: str = "data/transcripts"
    graph_dir: str = "data/graph.kuzu"
    db_path: str = "data/jobs.db"
    vault_dir: str = "vault"


class ComputeConfig(BaseModel):
    whisper_backend: str = "auto"
    whisper_model: str = "large-v3"
    torch_device: str = "auto"
    serialize_models: str = "auto"


class BacklogConfig(BaseModel):
    max_episodes_per_day: int = 5
    strategy: str = "oldest_first"
    per_show_daily_cap: int = 2


class ExtractConfig(BaseModel):
    backend: str = "local"
    local_model: str = "qwen2.5:14b-instruct-q4_K_M"
    local_endpoint: str = "http://localhost:11434"
    json_mode: str = "json_schema"
    spot_check_pct: int = 0
    claude_model: str = "claude-haiku-4-5-20251001"


class NotionConfig(BaseModel):
    enabled: bool = False
    token: str = "${NOTION_TOKEN}"
    episodes_db_id: str = ""
    concepts_db_id: str = ""
    weekly_parent_page_id: str = ""
    sync_mode: str = "push"


class BudgetConfig(BaseModel):
    monthly_cap_usd: float = 20.0
    warn_at_pct: int = 80
    summarize_model: str = "claude-sonnet-4-6"


class TranscribeConfig(BaseModel):
    diarize: bool = False
    hf_token: str = ""
    language: str = ""
    summary_lang: str = "source"


class SummarizeConfig(BaseModel):
    episode_summary_words: int = 600
    weekly_digest_words: int = 1200
    exclude_banter_from_digest: bool = True


class ChunkerConfig(BaseModel):
    target_seconds: int = 180
    overlap_seconds: int = 15


class Config(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    compute: ComputeConfig = Field(default_factory=ComputeConfig)
    backlog: BacklogConfig = Field(default_factory=BacklogConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)


# Config is not cached: callers re-read at runtime so edits take effect without restart.
def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = Path("config.toml")
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)

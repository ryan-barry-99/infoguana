from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: Path = Path("./data/infoguana.db")
    host: str = "0.0.0.0"
    port: int = 8789
    mcp_secret: str = "change-me-lan-shared-secret"
    claude_bin: str = "claude"
    classify_model: str = "claude-haiku-4-5"
    embed_model: str = "BAAI/bge-small-en-v1.5"

    backup_dir: Path = Path("./backups")
    backup_interval_hours: float = 0.25
    backup_retain: int = 30
    # Optional off-box sync target — e.g. an NFS mount where backups are
    # mirrored after rotation. Leave unset to disable.
    nas_sync_path: Path | None = None

    attachments_dir: Path = Path("./data/attachments")
    attachment_max_bytes: int = 15 * 1024 * 1024  # 15 MiB
    classify_image_max_px: int = 1280  # longest side before sending to claude

    # GitHub integration for the chat UI.
    # - github_read_token: single PAT used for ALL read operations (issues,
    #   PRs, comments). Should be a PAT on the user's personal account with
    #   broad read scope; what the PAT can see is what the chat can see.
    # - github_bot_tokens: map of project name -> PAT used when posting
    #   comments from that project's chat. The project key matches the infoguana's
    #   `chat.project` value. Missing key => writes refuse with a clear error.
    #   Env form is a JSON string, e.g.:
    #     INFOGUANA_GITHUB_BOT_TOKENS='{"my-project":"ghp_xxx"}'
    github_read_token: str | None = None
    github_bot_tokens: dict[str, str] = {}

    # Read-only filesystem access for infoguana MCP clients (infoguana-chat agent and
    # any Claude Code session connected to the infoguana MCP server). Colon-
    # separated list of absolute root directories the agent may read under.
    # Any path that, after resolution, does not lie under one of these roots
    # is refused. Denylist for secrets / .git / *.sqlite is hardcoded in
    # app/fs_access.py.
    #
    # Env form: INFOGUANA_FS_ALLOWLIST=/root/code:/root/docs
    fs_allowlist: list[Path] = [Path("/root/code")]
    fs_read_max_bytes: int = 500 * 1024  # 500 KiB hard cap per read

    @field_validator("fs_allowlist", mode="before")
    @classmethod
    def _split_fs_allowlist(cls, v):
        # Accept colon-separated string from env, list from code/JSON.
        if isinstance(v, str):
            return [Path(p) for p in v.split(":") if p.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INFOGUANA_",
        extra="ignore",
    )


settings = Settings()

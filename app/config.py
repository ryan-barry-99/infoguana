from pathlib import Path
from typing import Annotated
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    db_path: Path = Path("./data/infoguana.db")
    host: str = "0.0.0.0"
    port: int = 8789
    mcp_secret: str = "change-me-lan-shared-secret"
    claude_bin: str = "claude"
    classify_model: str = "claude-haiku-4-5"
    embed_model: str = "BAAI/bge-small-en-v1.5"

    # Classification backend. Set classify_base_url to an OpenAI-compatible
    # /v1 endpoint (LM Studio, Ollama, OpenAI, vLLM) to classify over HTTP
    # instead of shelling out to the Claude CLI — the CLI is only present on
    # machines running Claude Code, so a Codex-only or headless install has
    # no classifier without this. Set classify_model to a model the endpoint
    # actually serves (e.g. 'gemma-2-9b-it'), since the default names a
    # Claude model. Leave the URL unset to keep the CLI path.
    #
    #   INFOGUANA_CLASSIFY_BASE_URL=http://127.0.0.1:1234/v1
    #   INFOGUANA_CLASSIFY_MODEL=gemma-2-9b-it
    classify_base_url: str | None = None
    classify_api_key: str | None = None  # unset is fine for local servers
    classify_timeout: float = 180.0

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
    #   comments from that project's chat. The project key matches infoguana's
    #   `chat.project` value. Missing key => writes refuse with a clear error.
    #   Env form is a JSON string, e.g.:
    #     INFOGUANA_GITHUB_BOT_TOKENS='{"my-project":"ghp_xxx"}'
    github_read_token: str | None = None
    github_bot_tokens: dict[str, str] = {}

    @field_validator("github_bot_tokens", mode="before")
    @classmethod
    def _empty_to_empty_dict(cls, v):
        # docker-compose passes INFOGUANA_GITHUB_BOT_TOKENS as "" when the
        # user hasn't set it. pydantic-settings would then try to json.loads("")
        # and crash. Treat empty/whitespace as the default {}.
        if isinstance(v, str) and not v.strip():
            return {}
        return v

    # Read-only filesystem access for infoguana MCP clients (infoguana-chat agent and
    # any Claude Code session connected to infoguana MCP server). Colon-
    # separated list of absolute root directories the agent may read under.
    # Any path that, after resolution, does not lie under one of these roots
    # is refused. Denylist for secrets / .git / *.sqlite is hardcoded in
    # app/fs_access.py.
    #
    # Env form: INFOGUANA_FS_ALLOWLIST=/root/code:/root/docs
    # NoDecode tells pydantic-settings not to JSON-decode the env value — the
    # validator below splits the colon-separated string itself. Without
    # NoDecode, the env-var source would crash on `json.loads("/root/code")`
    # before the validator gets a chance to run.
    #
    # Empty by default, which disables the read/list/grep tools entirely: an
    # empty allowlist has no root for a path to resolve under, so every call
    # is refused. The alternative — shipping a default root — would mean a
    # fresh install exposes one operator's layout to any MCP caller holding
    # the bearer token, without that ever being an explicit decision.
    fs_allowlist: Annotated[list[Path], NoDecode] = []
    fs_read_max_bytes: int = 500 * 1024  # 500 KiB hard cap per read

    @field_validator("fs_allowlist", mode="before")
    @classmethod
    def _split_fs_allowlist(cls, v):
        # Accept colon-separated string from env, list from code/JSON.
        if isinstance(v, str):
            return [Path(p) for p in v.split(":") if p.strip()]
        return v

    # Extra Host/Origin values permitted to reach the MCP endpoint, beyond
    # loopback — comma-separated, ':*' wildcards the port. Set this when
    # clients reach the server by LAN or tailnet IP rather than localhost.
    # Leaving it unset disables the DNS-rebinding checks outright, which
    # _transport_security has to do explicitly — the SDK would otherwise
    # auto-enable a loopback-only allowlist and refuse LAN clients 421.
    # Setting it turns protection on with loopback + these hosts.
    #
    #   INFOGUANA_MCP_ALLOWED_HOSTS=10.0.0.5:*,infoguana.tailnet.ts.net
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = []

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_mcp_allowed_hosts(cls, v):
        # Comma-separated from env (':' is taken by host:port), list from code.
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INFOGUANA_",
        extra="ignore",
    )


settings = Settings()

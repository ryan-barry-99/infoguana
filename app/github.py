"""Thin GitHub REST client used by the infoguana chat MCP tools.

Two auth scopes:
- Reads use a single personal PAT (`settings.github_read_token`). What that
  PAT can see = what the chat can see.
- Writes use a project-scoped bot PAT (`settings.github_bot_tokens[project]`),
  so each project's chat posts comments as its own bot identity rather than
  as the user.

We deliberately don't wrap PyGithub — the surface we need is tiny and
httpx keeps the dep graph small. Errors bubble up as GitHubError with a
short human-readable message so MCP tool results render cleanly in the UI.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings


log = logging.getLogger(__name__)


_gh_token_cache: Optional[str] = None


def _gh_cli_token() -> Optional[str]:
    """Fallback read-token source: shell `gh auth token` once per process and
    cache the result. Lets the infoguana read with the user's existing gh CLI
    credentials when INFOGUANA_GITHUB_READ_TOKEN isn't explicitly configured."""
    global _gh_token_cache
    if _gh_token_cache is not None:
        return _gh_token_cache or None
    try:
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.info("gh auth token unavailable: %s", e)
        _gh_token_cache = ""
        return None
    tok = (r.stdout or "").strip()
    _gh_token_cache = tok
    return tok or None


API_BASE = "https://api.github.com"
UA = "infoguana-chat/1.0"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(Exception):
    """Raised for any non-2xx GitHub response or config error. Message is
    suitable for rendering back to the agent as a tool result."""


def _check_repo(repo: str) -> str:
    if not REPO_RE.match(repo or ""):
        raise GitHubError(f"invalid repo '{repo}' — expected 'owner/name'")
    return repo


def _resolve_token(raw: Optional[str]) -> Optional[str]:
    """A token value that starts with '/' is treated as a path to a file
    containing the token. Lets a PAT be shared between processes without
    duplicating the secret into .env and without it being world-readable."""
    if not raw:
        return None
    if raw.startswith("/"):
        p = Path(raw)
        if not p.is_file():
            log.warning("GitHub token file %s does not exist", p)
            return None
        try:
            return p.read_text().strip() or None
        except OSError as e:
            log.warning("failed to read GitHub token file %s: %s", p, e)
            return None
    return raw.strip()


def _read_headers() -> dict[str, str]:
    token = _resolve_token(settings.github_read_token) or _gh_cli_token()
    if not token:
        raise GitHubError(
            "no GitHub read token available — set INFOGUANA_GITHUB_READ_TOKEN or "
            "authenticate the root user's gh CLI (`gh auth login`)"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }


def _write_headers(bot_project: str) -> dict[str, str]:
    raw = settings.github_bot_tokens.get(bot_project or "")
    token = _resolve_token(raw) if raw else None
    if not token:
        configured = ", ".join(sorted(settings.github_bot_tokens)) or "<none>"
        raise GitHubError(
            f"no bot PAT configured for project '{bot_project}' "
            f"(INFOGUANA_GITHUB_BOT_TOKENS keys: {configured})"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }


def _get(path: str, params: Optional[dict] = None) -> httpx.Response:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{API_BASE}{path}", headers=_read_headers(), params=params)
    if r.status_code >= 400:
        raise GitHubError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
    return r


def _post(path: str, bot_project: str, json_body: dict) -> httpx.Response:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{API_BASE}{path}", headers=_write_headers(bot_project),
                   json=json_body)
    if r.status_code >= 400:
        raise GitHubError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
    return r


# ---------- shapes ----------

def _issue_dict(raw: dict) -> dict:
    """Compact an issue/PR payload down to the fields a chat actually needs."""
    user = (raw.get("user") or {}).get("login")
    labels = [l.get("name") for l in raw.get("labels") or [] if l.get("name")]
    return {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "author": user,
        "labels": labels,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "body": raw.get("body") or "",
        "comments": raw.get("comments"),
        "html_url": raw.get("html_url"),
        "is_pr": "pull_request" in raw,
    }


def _comment_dict(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "author": (raw.get("user") or {}).get("login"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "body": raw.get("body") or "",
        "html_url": raw.get("html_url"),
    }


def _review_comment_dict(raw: dict) -> dict:
    d = _comment_dict(raw)
    d["path"] = raw.get("path")
    d["line"] = raw.get("line") or raw.get("original_line")
    d["diff_hunk"] = raw.get("diff_hunk")
    return d


# ---------- reads ----------

def get_issue(repo: str, number: int) -> dict:
    _check_repo(repo)
    r = _get(f"/repos/{repo}/issues/{number}")
    return _issue_dict(r.json())


def list_issue_comments(repo: str, number: int, limit: int = 50) -> list[dict]:
    _check_repo(repo)
    r = _get(f"/repos/{repo}/issues/{number}/comments",
             params={"per_page": min(max(limit, 1), 100)})
    return [_comment_dict(c) for c in r.json()[:limit]]


def list_issues(repo: str, state: str = "open",
                labels: Optional[str] = None, limit: int = 20) -> list[dict]:
    _check_repo(repo)
    if state not in {"open", "closed", "all"}:
        raise GitHubError(f"invalid state '{state}' — expected open|closed|all")
    params: dict = {"state": state, "per_page": min(max(limit, 1), 100)}
    if labels:
        params["labels"] = labels
    r = _get(f"/repos/{repo}/issues", params=params)
    # /issues returns PRs too; filter unless the caller explicitly wants them.
    issues = [i for i in r.json() if "pull_request" not in i]
    return [_issue_dict(i) for i in issues[:limit]]


def get_pr(repo: str, number: int) -> dict:
    _check_repo(repo)
    r = _get(f"/repos/{repo}/pulls/{number}")
    raw = r.json()
    base = _issue_dict(raw)
    base.update({
        "draft": raw.get("draft"),
        "merged": raw.get("merged"),
        "mergeable_state": raw.get("mergeable_state"),
        "head": (raw.get("head") or {}).get("ref"),
        "base_ref": (raw.get("base") or {}).get("ref"),
        "additions": raw.get("additions"),
        "deletions": raw.get("deletions"),
        "changed_files": raw.get("changed_files"),
    })
    return base


def list_pr_comments(repo: str, number: int, limit: int = 50) -> dict:
    """Return both issue-style comments on the PR and inline review comments."""
    _check_repo(repo)
    per_page = min(max(limit, 1), 100)
    issue_r = _get(f"/repos/{repo}/issues/{number}/comments",
                   params={"per_page": per_page})
    review_r = _get(f"/repos/{repo}/pulls/{number}/comments",
                    params={"per_page": per_page})
    return {
        "conversation": [_comment_dict(c) for c in issue_r.json()[:limit]],
        "review": [_review_comment_dict(c) for c in review_r.json()[:limit]],
    }


# ---------- writes ----------

def post_issue_comment(repo: str, number: int, body: str,
                       bot_project: str) -> dict:
    _check_repo(repo)
    if not (body or "").strip():
        raise GitHubError("comment body is empty")
    r = _post(f"/repos/{repo}/issues/{number}/comments", bot_project,
              {"body": body})
    return _comment_dict(r.json())


def create_issue(repo: str, title: str, body: str, bot_project: str,
                 labels: Optional[list[str]] = None) -> dict:
    _check_repo(repo)
    if not (title or "").strip():
        raise GitHubError("issue title is empty")
    payload: dict = {"title": title, "body": body or ""}
    if labels:
        payload["labels"] = [l for l in labels if l]
    r = _post(f"/repos/{repo}/issues", bot_project, payload)
    return _issue_dict(r.json())

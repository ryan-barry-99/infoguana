"""Invariants in `docker-compose.yml` that only a container would otherwise catch.

Both checks here are regressions that shipped: a schema older Compose could
not parse, and a port variable used for two incompatible purposes. The CI
docker job builds and boots the real thing, which is the authoritative check;
these exist because that job takes minutes and these take milliseconds, and a
misrendered port is cheap to reintroduce.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

CONTAINER_PORT = 8789


@pytest.fixture(scope="module")
def service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["infoguana"]


def test_env_file_uses_the_short_form(service: dict) -> None:
    """The `path:`/`required:` long form needs Compose v2.24+.

    Older Compose rejects the whole file rather than ignoring the key, so the
    long form is not a graceful degradation — it is a hard refusal to start.
    """
    entries = service["env_file"]
    assert entries == [".env"], f"expected the v1-compatible short form, got {entries!r}"


def test_the_container_port_is_fixed_not_substituted(service: dict) -> None:
    """`INFOGUANA_PORT` is the host side of the mapping, nothing else.

    Substituting it into the container's own environment moved the listener
    while the mapping's target and the healthcheck stayed on 8789, so any
    value but the default published a port with nothing behind it.
    """
    assert str(service["environment"]["INFOGUANA_PORT"]) == str(CONTAINER_PORT)


def test_mapping_target_and_healthcheck_agree_with_the_listener(service: dict) -> None:
    """All three references to the in-container port must name the same one."""
    targets = [str(m).rsplit(":", 1)[-1] for m in service["ports"]]
    assert targets == [str(CONTAINER_PORT)], f"ports target {targets}"
    assert f"localhost:{CONTAINER_PORT}/healthz" in " ".join(service["healthcheck"]["test"])

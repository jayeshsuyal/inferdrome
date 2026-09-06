"""The single local Docker target admitted by manual-host preparation.

No context discovery, daemon probe, or configurable remote/rootless mode. All
read and lifecycle commands use this same explicit host and empty config.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

LOCAL_DOCKER_HOST = "unix:///var/run/docker.sock"
DOCKER_CONFIG_FILE = "config.json"
EMPTY_DOCKER_CONFIG = b"{}"


def docker_target(config_directory: str) -> dict[str, str]:
    return {
        "schema_version": "inferdrome.manual-host-docker-target.v1",
        "host": LOCAL_DOCKER_HOST,
        "config_directory": config_directory,
        "config_file": DOCKER_CONFIG_FILE,
        "context_selection": "DISABLED_EXPLICIT_LOCAL_HOST",
        "ambient_routing": "CLEARED_BEFORE_DOCKER",
        "compose_env_file": "DISABLED",
    }


def docker_argv(config_directory: str, *arguments: str) -> list[str]:
    return [
        "docker",
        "--host",
        LOCAL_DOCKER_HOST,
        "--config",
        config_directory,
        *arguments,
    ]


def docker_environment(
    config_directory: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove selectors/TLS/plugin overrides, including Compose .env loading.

    Explicit CLI options bind the target as well; pinned environment values
    also cover the Compose plugin's child-process boundary.
    """
    source = os.environ if environment is None else environment
    result = {
        key: value
        for key, value in source.items()
        if not key.startswith(("DOCKER_", "COMPOSE_"))
    }
    result.update(
        {
            "DOCKER_HOST": LOCAL_DOCKER_HOST,
            "DOCKER_CONFIG": config_directory,
            "COMPOSE_DISABLE_ENV_FILE": "1",
        }
    )
    return result

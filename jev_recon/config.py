"""Configuration: API key, base URL, model, and a tiny ``.env`` reader.

No dotenv dependency: the format is four lines of Python and one less package.
"""

from __future__ import annotations

import os

from .jev import DEFAULT_BASE_URL, DEFAULT_MODEL

API_KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL_ENV = "TYPESAFE_BASE_URL"
MODEL_ENV = "TYPESAFE_DEFAULT_MODEL"

#: Where the default ``.env`` is looked for, in order: the working directory
#: (so a pipe from any recon directory works), the project itself, then XDG.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_NAME = ".env"


def env_search_paths(path: str = DEFAULT_ENV_NAME) -> list[str]:
    """The files ``load_env`` will try. A named file is never second-guessed."""
    if path != DEFAULT_ENV_NAME:
        return [path]
    return [
        os.path.join(os.getcwd(), DEFAULT_ENV_NAME),
        os.path.join(PROJECT_ROOT, DEFAULT_ENV_NAME),
        os.path.join(os.path.expanduser("~"), ".config", "jev-recon", DEFAULT_ENV_NAME),
    ]


def load_env(path: str = DEFAULT_ENV_NAME) -> tuple[int, list[str]]:
    """Load ``KEY=VALUE`` pairs into ``os.environ`` without overriding real env.

    Returns ``(variables_set, paths_tried)``. A missing file is not an error.
    """
    tried: list[str] = []
    loaded = 0
    for candidate in env_search_paths(path):
        tried.append(candidate)
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded += 1
        break
    return loaded, tried


def resolve(api_key: str | None, base_url: str | None, model: str | None) -> tuple[str, str, str]:
    key = api_key or os.environ.get(API_KEY_ENV, "")
    url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    name = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    return key, url.rstrip("/"), name

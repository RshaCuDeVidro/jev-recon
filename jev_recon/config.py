"""Configuration: API key, base URL, model, and a tiny ``.env`` reader.

No dotenv dependency: the format is four lines of Python and one less package.
"""

from __future__ import annotations

import os

from .jev import DEFAULT_BASE_URL, DEFAULT_MODEL

API_KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL_ENV = "TYPESAFE_BASE_URL"
MODEL_ENV = "TYPESAFE_DEFAULT_MODEL"


def load_env(path: str = ".env") -> int:
    """Load ``KEY=VALUE`` pairs into ``os.environ`` without overriding real env.

    Returns the number of variables set. Missing file is not an error.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0

    loaded = 0
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
    return loaded


def resolve(api_key: str | None, base_url: str | None, model: str | None) -> tuple[str, str, str]:
    key = api_key or os.environ.get(API_KEY_ENV, "")
    url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    name = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    return key, url.rstrip("/"), name

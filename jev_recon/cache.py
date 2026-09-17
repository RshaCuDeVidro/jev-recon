"""Response cache keyed by the exact request body.

Re-ranking is the whole point of this tool, and it never needs the API: the
probabilities for a given (model, signals, candidates) are already in hand. With
``--cache`` a second run with different weights or a different threshold costs
nothing and returns instantly.

The key is a hash of the request body as sent, so a hit is only possible when
the state, the questions and the model are all identical. Delete the file to
force fresh answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile


class ResponseCache:
    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self.hits = 0
        self.writes = 0
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, json.JSONDecodeError):
            self.data = {}

    @staticmethod
    def key(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> dict | None:
        response = self.data.get(key)
        if response is None:
            return None
        self.hits += 1
        return response

    def put(self, key: str, response: dict) -> None:
        self.data[key] = response
        self.writes += 1
        self.dirty = True

    def save(self) -> None:
        """Atomic write, so a killed run cannot leave a half-written cache."""
        if not self.dirty:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError:
            os.unlink(tmp)
            raise

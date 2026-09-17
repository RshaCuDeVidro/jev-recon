"""Async Jev client: batching, bounded concurrency, retries, rate limits.

There is no "batch endpoint" in the TypeSafe API. Batching happens *inside* one
``POST /v1/systemone`` call: the documented way to score many items is to put
them all in ``state`` and ask one question per item, in the same request. All
questions in a request are evaluated in parallel, so 20 candidates scored on 7
signals costs one round trip and 140 questions instead of 20 round trips.

That gives two independent levers, and this client uses both:

* ``batch_size``   - candidates per HTTP request (fewer, fuller requests)
* ``concurrency``  - HTTP requests in flight at once (asyncio + a semaphore)

Documented limits this module respects (docs.typesafe.ai, jev-1.13):

* 64k tokens for all of ``state`` + ``questions``
* 32k tokens for ``state`` + the longest single question
* ``429`` on the tokens/second or requests/minute limit, ``529`` when overloaded
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from .signals import DEFAULT_SIGNAL_ORDER, build_request

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
ENDPOINT = "/v1/systemone"

#: statuses worth retrying, from the API reference plus the usual suspects
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529})
#: statuses that must not be retried
FATAL_STATUS = frozenset({400, 401, 403, 404, 413, 422})

#: price per 1M input tokens, output tokens are free (models page, jev-1.13)
PRICE_PER_MTOK_INPUT = 0.042


class JevError(RuntimeError):
    """Base class for every failure this client surfaces."""


class JevAuthError(JevError):
    """401: missing or invalid API key."""


class JevRequestError(JevError):
    """A request the server refused (validation, permissions, not found)."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:400]}")


class JevUnavailable(JevError):
    """Retries exhausted (rate limit, overload, network, timeout)."""


@dataclass
class RunStats:
    requests_sent: int = 0
    requests_ok: int = 0
    retries: int = 0
    retries_by_status: dict = field(default_factory=dict)
    batches_planned: int = 0
    batches_failed: int = 0
    adaptive_splits: int = 0
    rate_limit_pauses: int = 0
    paused_seconds: float = 0.0
    cache_hits: int = 0
    cache_writes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models_seen: set = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 2)

    @property
    def estimated_cost_usd(self) -> float:
        return round(self.input_tokens / 1_000_000 * PRICE_PER_MTOK_INPUT, 6)

    def as_dict(self) -> dict:
        return {
            "requests_sent": self.requests_sent,
            "requests_ok": self.requests_ok,
            "retries": self.retries,
            "retries_by_status": dict(sorted(self.retries_by_status.items())),
            "batches_planned": self.batches_planned,
            "batches_failed": self.batches_failed,
            "adaptive_splits": self.adaptive_splits,
            "rate_limit_pauses": self.rate_limit_pauses,
            "paused_seconds": round(self.paused_seconds, 2),
            "cache_hits": self.cache_hits,
            "cache_writes": self.cache_writes,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "models_seen": sorted(self.models_seen),
            "estimated_cost_usd": self.estimated_cost_usd,
            "wall_seconds": self.seconds,
        }


@dataclass
class BatchOutcome:
    batch_id: int
    hostnames: list[str]
    signals: list[dict] = field(default_factory=list)
    relative_pick: list[float] = field(default_factory=list)
    batch_yield: float | None = None
    batch_yield_confidence: float | None = None
    incomplete: list[bool] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def estimate_tokens(text: str) -> int:
    """Cheap token estimate. English is ~4 chars per token; the state is names.

    Deliberately conservative: it only ever has to stop us from building a
    request that the API would reject.
    """
    return max(1, len(text) // 3)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        delta = (parsedate_to_datetime(value) - parsedate_to_datetime(
            time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        )).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


class JevClient:
    """Client for ``POST /v1/systemone`` with bounded concurrency and retries."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_initial: float = 0.5,
        backoff_max: float = 20.0,
        backoff_jitter: float = 0.25,
        respect_retry_after: bool = True,
        max_questions_per_request: int = 220,
        max_request_tokens: int = 24_000,
        signals: tuple[str, ...] = DEFAULT_SIGNAL_ORDER,
        cache=None,
        on_event=None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.backoff_jitter = backoff_jitter
        self.respect_retry_after = respect_retry_after
        self.max_questions_per_request = max_questions_per_request
        self.max_request_tokens = max_request_tokens
        self.signals = signals
        self.cache = cache
        self.on_event = on_event or (lambda *_a, **_k: None)
        self.stats = RunStats()
        #: epoch at which all workers are allowed to talk to the API again
        self._resume_at = 0.0
        self._pause_lock = asyncio.Lock()

    # -- planning ---------------------------------------------------------

    def effective_batch_size(self, requested: int, sample_hostnames: list[str]) -> int:
        """Shrink the batch to fit the documented per-request limits."""
        by_questions = max(
            1, self.max_questions_per_request // (len(self.signals) + 2)
        )
        size = max(1, min(requested, by_questions))
        while size > 1:
            probe = [f"candidates[{i}]" for i in range(size)]
            skeleton = json.dumps(
                {
                    "state": {"candidates": probe * 12},  # rough per-item cost
                    "questions": {q: {} for q in probe},
                }
            )
            if estimate_tokens(skeleton) <= self.max_request_tokens:
                break
            size -= 1
        if size < requested:
            self.on_event(
                "plan",
                f"batch size reduced from {requested} to {size} to stay inside "
                f"{self.max_request_tokens} tokens / "
                f"{self.max_questions_per_request} questions per request",
            )
        return size

    def split_batches(self, candidates: list, batch_size: int) -> list[list]:
        return [
            candidates[i : i + batch_size]
            for i in range(0, len(candidates), batch_size)
        ]

    # -- transport --------------------------------------------------------

    async def _sleep_until_resumed(self) -> None:
        delay = self._resume_at - time.monotonic()
        if delay > 0:
            self.stats.paused_seconds += delay
            await asyncio.sleep(delay)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.backoff_max * 4)
        raw = min(self.backoff_max, self.backoff_initial * (2 ** attempt))
        return max(0.0, raw * (1.0 - random.random() * self.backoff_jitter))

    async def _post(
        self, client: httpx.AsyncClient, payload: dict, batch_id: int
    ) -> tuple[dict, bool]:
        """POST one request, retrying per the documented backoff guidance.

        Returns ``(response, served_from_cache)``. A cached response is not
        billed, so the caller must not count its tokens.
        """
        body = json.dumps(payload)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        cache_key = None
        if self.cache is not None:
            cache_key = self.cache.key_for(payload)
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.stats.cache_hits += 1
                self.stats.requests_ok += 1
                self.on_event("cache", f"batch {batch_id}: served from cache")
                return cached, True
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            await self._sleep_until_resumed()
            try:
                self.stats.requests_sent += 1
                response = await client.post(
                    f"{self.base_url}{ENDPOINT}", content=body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = self._backoff(attempt, None)
                self._note_retry("network", attempt, delay, batch_id)
                await asyncio.sleep(delay)
                continue

            status = response.status_code
            if status == 200:
                try:
                    parsed = response.json()
                except ValueError as exc:
                    last_error = JevUnavailable(f"200 with a non-JSON body: {exc}")
                    if attempt >= self.max_retries:
                        break
                    delay = self._backoff(attempt, None)
                    self._note_retry("bad-body", attempt, delay, batch_id)
                    await asyncio.sleep(delay)
                    continue
                self.stats.requests_ok += 1
                if cache_key is not None:
                    self.cache.put(cache_key, parsed)
                    self.stats.cache_writes += 1
                return parsed, False

            if status == 401:
                raise JevAuthError(
                    "401 from the API: TYPESAFE_API_KEY is missing, wrong, or revoked."
                )
            if status == 422:
                # A validation error will not fix itself on retry, but it does
                # tell us the batch was too ambitious: the caller splits it.
                raise JevRequestError(status, response.text)
            if status in RETRYABLE_STATUS:
                last_error = JevUnavailable(f"HTTP {status}: {response.text[:200]}")
                self.stats.retries_by_status[status] = (
                    self.stats.retries_by_status.get(status, 0) + 1
                )
                if attempt >= self.max_retries:
                    break
                retry_after = (
                    parse_retry_after(response.headers.get("retry-after"))
                    if self.respect_retry_after
                    else None
                )
                delay = self._backoff(attempt, retry_after)
                if status == 429:
                    async with self._pause_lock:
                        self._resume_at = max(self._resume_at, time.monotonic() + delay)
                    self.stats.rate_limit_pauses += 1
                    self.on_event(
                        "429",
                        f"rate limited on batch {batch_id}: pausing every worker "
                        f"{delay:.1f}s"
                        + (" (retry-after honoured)" if retry_after is not None else ""),
                    )
                else:
                    self._note_retry(status, attempt, delay, batch_id)
                await asyncio.sleep(delay)
                continue
            if status in FATAL_STATUS:
                raise JevRequestError(status, response.text)
            last_error = JevUnavailable(f"HTTP {status}: {response.text[:200]}")
            if attempt >= self.max_retries:
                break
            delay = self._backoff(attempt, None)
            self._note_retry(status, attempt, delay, batch_id)
            await asyncio.sleep(delay)

        raise JevUnavailable(
            f"batch {batch_id} failed after {self.max_retries + 1} attempts: {last_error}"
        )

    def _note_retry(self, reason, attempt: int, delay: float, batch_id: int) -> None:
        self.stats.retries += 1
        self.on_event(
            "retry",
            f"batch {batch_id}: attempt {attempt + 1} failed ({reason}), "
            f"retrying in {delay:.2f}s",
        )

    # -- response parsing -------------------------------------------------

    def parse_batch(
        self, payload: dict, response: dict, batch_id: int, from_cache: bool = False
    ) -> BatchOutcome:
        hostnames = [c["hostname"] for c in payload["state"]["candidates"]]
        outcome = BatchOutcome(batch_id=batch_id, hostnames=hostnames)
        answers = response.get("answers") or {}
        if not isinstance(answers, dict):
            outcome.error = "response carried no `answers` map"
            return outcome

        model = response.get("model")
        if model:
            self.stats.models_seen.add(str(model))
        # A response served from the cache was already billed by the run that
        # stored it, so it must not be counted again here.
        usage = {} if from_cache else (response.get("usage") or {})
        self.stats.input_tokens += int(usage.get("input_tokens") or 0)
        self.stats.output_tokens += int(usage.get("output_tokens") or 0)

        count = len(hostnames)
        probabilities: dict = {}
        pick = answers.get("batch::top_pick") or {}
        if isinstance(pick.get("probabilities"), dict):
            probabilities = pick["probabilities"]

        for index in range(count):
            signals: dict[str, float | None] = {}
            incomplete = False
            for name in self.signals:
                answer = answers.get(f"c{index}::{name}")
                value = None
                if isinstance(answer, dict) and answer.get("type") == "noul":
                    try:
                        value = float(answer["noul"])
                    except (TypeError, ValueError, KeyError):
                        value = None
                if value is None:
                    incomplete = True
                signals[name] = value
            outcome.signals.append(signals)
            outcome.incomplete.append(incomplete)
            outcome.relative_pick.append(
                float(probabilities.get(f"candidates[{index}]", 0.0) or 0.0)
            )

        answer = answers.get("batch::research_yield") or {}
        if answer.get("type") == "score":
            try:
                outcome.batch_yield = float(answer["score"])
            except (TypeError, ValueError, KeyError):
                outcome.batch_yield = None
            outcome.batch_yield_confidence = answer.get("confidence")
        return outcome

    # -- the pipeline -----------------------------------------------------

    async def analyze(
        self,
        candidates: list,
        *,
        batch_size: int = 20,
        concurrency: int = 8,
        dry_run: bool = False,
        strict: bool = False,
    ) -> list[BatchOutcome]:
        """Score every candidate, ``batch_size`` per request, ``concurrency`` at a time."""
        if not candidates:
            return []
        sample = [c.hostname for c in candidates[:batch_size]]
        size = self.effective_batch_size(batch_size, sample)
        batches = self.split_batches(candidates, size)
        self.stats.batches_planned = len(batches)

        if dry_run:
            return [self._dry_batch(i, b) for i, b in enumerate(batches)]

        semaphore = asyncio.Semaphore(max(1, concurrency))
        limits = httpx.Limits(
            max_connections=max(4, concurrency),
            max_keepalive_connections=max(2, concurrency // 2),
        )
        timeout = httpx.Timeout(self.timeout, connect=min(15.0, self.timeout))

        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            async def run(index: int, batch: list) -> BatchOutcome:
                async with semaphore:
                    return await self._run_batch(
                        client, index, batch, strict=strict, depth=0
                    )

            return list(
                await asyncio.gather(*(run(i, b) for i, b in enumerate(batches)))
            )

    async def _run_batch(
        self,
        client: httpx.AsyncClient,
        batch_id: int,
        batch: list,
        *,
        strict: bool,
        depth: int,
    ) -> BatchOutcome:
        payload = build_request(batch, self.model, self.signals)
        try:
            response, from_cache = await self._post(client, payload, batch_id)
        except JevRequestError as exc:
            # 422 means the request did not validate. Most common cause in
            # practice: too many questions in one request. Split and retry once
            # we have a smaller batch; never silently drop the candidates.
            if exc.status == 422 and depth < 3 and len(batch) > 1:
                self.stats.adaptive_splits += 1
                self.on_event(
                    "split",
                    f"batch {batch_id}: HTTP 422 on {len(batch)} candidates, "
                    f"splitting in half and retrying",
                )
                midpoint = len(batch) // 2 or 1
                left, right = batch[:midpoint], batch[midpoint:]
                pairs = [(left, 0), (right, midpoint)] if right else [(left, 0)]
                results = []
                for part, offset in pairs:
                    outcome = await self._run_batch(
                        client,
                        batch_id,
                        part,
                        strict=strict,
                        depth=depth + 1,
                    )
                    outcome.batch_id = batch_id
                    results.append(outcome)
                merged = _merge_outcomes(batch_id, results)
                return merged
            outcome = BatchOutcome(
                batch_id=batch_id,
                hostnames=[c.hostname for c in batch],
                signals=[{} for _ in batch],
                relative_pick=[0.0 for _ in batch],
                incomplete=[True for _ in batch],
                error=str(exc),
            )
            self.stats.batches_failed += 1
            if strict:
                raise
            self.on_event("error", f"batch {batch_id}: {exc}")
            return outcome
        except JevAuthError:
            raise
        except JevUnavailable as exc:
            outcome = BatchOutcome(
                batch_id=batch_id,
                hostnames=[c.hostname for c in batch],
                signals=[{} for _ in batch],
                relative_pick=[0.0 for _ in batch],
                incomplete=[True for _ in batch],
                error=str(exc),
            )
            self.stats.batches_failed += 1
            if strict:
                raise
            self.on_event("error", f"batch {batch_id}: {exc}")
            return outcome

        outcome = self.parse_batch(payload, response, batch_id, from_cache)
        if not outcome.ok:
            self.stats.batches_failed += 1
            if strict:
                raise JevUnavailable(outcome.error or "empty response")
        self.on_event("batch_done", batch_id)
        return outcome

    def _dry_batch(self, batch_id: int, batch: list) -> BatchOutcome:
        payload = build_request(batch, self.model, self.signals)
        questions = payload["questions"]
        self.stats.input_tokens += estimate_tokens(json.dumps(payload))
        outcome = BatchOutcome(
            batch_id=batch_id,
            hostnames=[c.hostname for c in batch],
            signals=[{} for _ in batch],
            relative_pick=[0.0 for _ in batch],
            incomplete=[True for _ in batch],
        )
        outcome.dry_payload = payload  # type: ignore[attr-defined]
        outcome.dry_questions = len(questions)  # type: ignore[attr-defined]
        return outcome


def _merge_outcomes(batch_id: int, parts: list[BatchOutcome]) -> BatchOutcome:
    merged = BatchOutcome(batch_id=batch_id, hostnames=[], signals=[], relative_pick=[])
    merged.incomplete = []
    errors = []
    for part in parts:
        merged.hostnames.extend(part.hostnames)
        merged.signals.extend(part.signals)
        merged.relative_pick.extend(part.relative_pick)
        merged.incomplete.extend(part.incomplete)
        if not part.ok and part.error:
            errors.append(part.error)
        merged.batch_yield = part.batch_yield or merged.batch_yield
        merged.batch_yield_confidence = (
            part.batch_yield_confidence or merged.batch_yield_confidence
        )
    merged.error = "; ".join(errors) or None
    return merged

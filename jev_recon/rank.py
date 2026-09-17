"""Composite scoring and ranking, in Python.

This is the part that must not be delegated to a model. Jev returns one
independent probability per signal; the weights, the arithmetic and the sort
order are ours, reviewable, and changeable without touching a prompt.
"""

from __future__ import annotations

import json

from .preprocess import name_shape

#: Short signal names map onto the question ids used in the request.
SIGNAL_ALIASES = {
    "production": "likely_production",
    "sensitive": "likely_sensitive",
    "internal": "likely_internal",
    "staging": "likely_staging",
    "admin": "likely_admin",
    "api": "likely_api",
    "devops": "likely_devops",
    "interesting": "interesting_for_security_research",
    "relative_pick": "relative_pick",   # the batch-level Choice, a tie-breaker
}

#: The default formula. Weights are normalised over the signals actually
#: present, so they behave as documented as long as they sum to 1.
DEFAULT_WEIGHTS: dict[str, float] = {
    "production": 0.25,
    "sensitive": 0.25,
    "admin": 0.15,
    "api": 0.15,
    "interesting": 0.20,
}


def parse_weights(spec: str | None) -> dict[str, float]:
    """Accept ``--weights`` as JSON or as ``production=0.3,staging=-0.1``.

    Both short names (``production``) and full question ids
    (``likely_production``) are accepted; the result is keyed by short name, the
    namespace the ranking works in.
    """
    if not spec:
        return dict(DEFAULT_WEIGHTS)
    spec = spec.strip()
    try:
        if spec.startswith("{"):
            raw = json.loads(spec)
        else:
            raw = {}
            for chunk in spec.split(","):
                if not chunk.strip():
                    continue
                key, _, value = chunk.partition("=")
                raw[key.strip()] = float(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"could not parse --weights: {exc}") from exc

    weights: dict[str, float] = {}
    for key, value in raw.items():
        name = normalize_signal_name(key)
        if name is None:
            known = ", ".join(sorted(SIGNAL_ALIASES))
            raise ValueError(f"unknown weight '{key}'. Known signals: {known}")
        weights[name] = float(value)
    if not weights:
        raise ValueError("no weights given")
    if not any(v for v in weights.values()):
        raise ValueError("all weights are zero")
    return weights


#: full question id -> short name, so ``--weights`` accepts either
LONG_TO_SHORT = {long_name: short for short, long_name in SIGNAL_ALIASES.items()}


def normalize_signal_name(name: str) -> str | None:
    key = name.strip().lower()
    if key in SIGNAL_ALIASES:
        return key
    if key in LONG_TO_SHORT:
        return LONG_TO_SHORT[key]
    if key.startswith("likely_") and key.removeprefix("likely_") in SIGNAL_ALIASES:
        return key.removeprefix("likely_")
    return None


def score_namespace(signals: dict, relative_pick: float | None = None) -> dict:
    """Flatten one asset's signals into the namespace weights are written against."""
    namespace: dict[str, float | None] = {}
    for short, long_name in SIGNAL_ALIASES.items():
        if short == "relative_pick":
            continue
        namespace[short] = signals.get(long_name)
    if relative_pick is not None:
        namespace["relative_pick"] = relative_pick
    return namespace


def compute_priority(
    namespace: dict[str, float | None], weights: dict[str, float]
) -> tuple[float | None, dict[str, float], list[str]]:
    """Weighted sum of the signals that answered, over the full weight budget.

    The denominator is the sum of *all* configured weights, not just the weights
    of the signals that came back. A missing signal therefore lowers the score
    (by exactly its weight) instead of being renormalised away. That is
    deliberate: an asset whose signals we could not measure must never look more
    interesting than one we measured fully. Missing signals are reported in
    ``missing`` and flagged as ``incomplete`` on the asset.

    Returns ``(priority, used_weights, missing_signals)``. ``priority`` is
    ``None`` only when not a single weighted signal came back, which happens
    when a whole batch failed.
    """
    present = {
        name: value
        for name, value in namespace.items()
        if name in weights and value is not None
    }
    missing = sorted(name for name in weights if namespace.get(name) is None)
    denominator = sum(abs(w) for w in weights.values())
    if not present or denominator == 0:
        return None, {}, missing

    total = 0.0
    used: dict[str, float] = {}
    for name, value in present.items():
        used[name] = round(weights[name] / denominator, 4)
        total += weights[name] * value
    priority = total / denominator
    return round(max(0.0, min(1.0, priority)), 3), used, missing


#: signal -> (floor, human phrase). Why an asset ranked where it did, derived
#: from the numbers, never generated by a model. Ordered by the weight each
#: signal actually carried, so the top reason is the one that moved the score.
REASON_RULES = (
    ("admin", 0.60, "admin or management surface"),
    ("devops", 0.60, "build or deploy infrastructure"),
    ("sensitive", 0.60, "guards sensitive data or functionality"),
    ("internal", 0.60, "employee or partner facing, not public"),
    ("production", 0.60, "live production system"),
    ("api", 0.60, "machine-facing API surface"),
    ("staging", 0.60, "non-production environment"),
    ("interesting", 0.60, "worth manual analysis on its own"),
)


def explain(asset: dict) -> list[str]:
    """The reasons a human can read, built from the probabilities and the facts."""
    namespace = score_namespace(asset.get("signals") or {}, asset.get("relative_pick"))
    weights = asset.get("weights_used") or DEFAULT_WEIGHTS
    scored: list[tuple[float, str]] = []
    for short, floor, phrase in REASON_RULES:
        value = namespace.get(short)
        if value is not None and value >= floor:
            scored.append(
                (weights.get(short, 0.0) * value, f"{short} {value:.2f}  {phrase}")
            )
    reasons = [text for _, text in sorted(scored, key=lambda pair: -pair[0])]

    facts: list[str] = []
    pre = asset.get("pre") or {}
    if pre.get("env_token"):
        facts.append(f"code: {pre['env_token']} environment token")
    labels = pre.get("privileged_labels") or []
    if labels:
        facts.append(f"code: known labels {', '.join(labels[:4])}")
    meta = asset.get("metadata") or {}
    if meta.get("http_status") in (401, 403):
        facts.append(f"evidence: gated, HTTP {meta['http_status']}")
    if meta.get("title"):
        facts.append(f"evidence: title \"{str(meta['title'])[:48]}\"")
    if meta.get("technologies"):
        tech = meta["technologies"]
        facts.append(f"evidence: {', '.join(map(str, tech[:4]))}")
    if pre.get("tracking_namespace"):
        facts.append("code: third-party tracking namespace")
    if asset.get("incomplete"):
        facts.append("signals incomplete")
    return reasons + facts


#: Facts that mean "this is not the target's own surface". Each one multiplies
#: the priority by the penalty, in code, because a mechanical rule belongs in
#: code and not in a prompt.
#:
#: What the measurement actually says, after getting it wrong once: on a 300 host
#: set, top-30 membership made the criteria text look useless (7 of 15 decoys
#: promoted before and after the edit), because the affected hosts were already
#: far below the cut, so the metric could not see a demotion. Priority tells the
#: truth. On six hosts whose names reproduce the real case (a tracking namespace
#: plus an api label), the criteria took the decoys from 0.46, 0.46 and 0.44 down
#: to 0.12, 0.12 and 0.11 while the genuine api hosts stayed at 0.62, 0.56 and
#: 0.56. So the prompt does the work for this class, and this penalty is
#: insurance: it halves what the model already demotes (0.12 to 0.06, no effect
#: at any sane threshold) and it catches the case where some future model scores
#: such a name high, which is exactly what happened on the real list.
PENALTY_FACTS = ("tracking_namespace",)


def apply_penalties(priority: float | None, pre: dict, penalty: float) -> float | None:
    """Demote facts that disqualify an asset, without touching anything else."""
    if priority is None or penalty >= 1.0:
        return priority
    for fact in PENALTY_FACTS:
        if pre.get(fact):
            priority *= penalty
    return round(priority, 3)


def build_assets(outcomes: list, candidates_by_host: dict, weights: dict,
                 tracking_penalty: float = 0.5) -> list[dict]:
    """Turn batch outcomes into ranked-ready asset records."""
    assets: list[dict] = []
    for outcome in outcomes:
        for position, hostname in enumerate(outcome.hostnames):
            signals = (
                outcome.signals[position] if position < len(outcome.signals) else {}
            )
            relative_pick = (
                outcome.relative_pick[position]
                if position < len(outcome.relative_pick)
                else 0.0
            )
            namespace = score_namespace(signals, relative_pick)
            priority, used_weights, missing = compute_priority(namespace, weights)
            candidate = candidates_by_host.get(hostname)
            pre = candidate.pre if candidate else {}
            priority = apply_penalties(priority, pre, tracking_penalty)
            record = {
                "hostname": hostname,
                "priority": priority,
                "signals": {
                    name: (None if value is None else round(value, 3))
                    for name, value in signals.items()
                },
                "relative_pick": round(relative_pick, 3),
                "weights_used": used_weights,
                "missing_signals": missing,
                "shape": (
                    candidate.shape
                    if candidate is not None and candidate.shape
                    else name_shape(hostname, tuple(hostname.split(".")))
                ),
                "batch": {
                    "id": outcome.batch_id,
                    "research_yield": (
                        None
                        if outcome.batch_yield is None
                        else round(outcome.batch_yield, 3)
                    ),
                    "yield_confidence": outcome.batch_yield_confidence,
                    "batch_error": outcome.error,
                },
                "pre": pre,
                "metadata": candidate.meta if candidate else {},
                "incomplete": (
                    outcome.incomplete[position]
                    if position < len(outcome.incomplete)
                    else True
                ),
            }
            record["reasons"] = explain(record)
            assets.append(record)
    return sort_assets(assets)


def sort_assets(assets: list[dict]) -> list[dict]:
    """Priority first, then the batch-level relative pressure, then the name."""
    return sorted(
        assets,
        key=lambda a: (
            a["priority"] is None,
            -(a["priority"] or 0.0),
            -a["relative_pick"],
            a["hostname"],
        ),
    )


def select(assets: list[dict], threshold: float) -> list[dict]:
    return [
        asset
        for asset in assets
        if asset["priority"] is not None and asset["priority"] >= threshold
    ]


def diversify(assets: list[dict], max_per_shape: int) -> tuple[list[dict], int]:
    """Keep the best few of each service, not every regional copy of it.

    ``us-central-1..8.api.acme.com`` is one service: paying manual analysis for the
    fourth region adds nothing. Assets arrive already sorted, so the first of a
    shape is its highest-priority member and the rest are marked as copies.
    Returns ``(kept, dropped)``; every asset is annotated either way.
    """
    seen: dict[str, int] = {}
    kept: list[dict] = []
    for asset in assets:
        shape = asset.get("shape") or asset["hostname"]
        seen[shape] = seen.get(shape, 0) + 1
        asset["same_shape_count"] = seen[shape]
        asset["shape_rank"] = seen[shape]
        if max_per_shape <= 0 or seen[shape] <= max_per_shape:
            kept.append(asset)
        else:
            asset["suppressed_by_shape"] = True
    return kept, len(assets) - len(kept)


def summary(assets: list[dict], threshold: float) -> dict:
    scored = [a for a in assets if a["priority"] is not None]
    incomplete = [a for a in assets if a["incomplete"]]
    buckets = {name: 0 for name in SIGNAL_ALIASES}
    for asset in scored:
        for short, long_name in SIGNAL_ALIASES.items():
            value = asset["signals"].get(long_name)
            if value is not None and value >= 0.5:
                buckets[short] = buckets.get(short, 0) + 1
    return {
        "assets": len(assets),
        "scored": len(scored),
        "high_interest": sum(
            1 for a in scored if (a["priority"] or 0.0) >= threshold
        ),
        "unscored": len(assets) - len(scored),
        "incomplete": len(incomplete),
        "threshold": threshold,
        "signal_hits_over_0.5": buckets,
    }

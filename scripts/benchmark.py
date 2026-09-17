#!/usr/bin/env python3
"""Rank a labelled set five ways and report precision/recall at the top 10%.

Methods compared:

    random              control
    name heuristic      preprocess.pre_rank, tokens of the hostname only
    evidence keywords   the naive regex a person writes first (title + tech)
    jev, names only     seven Noul questions per asset, no probe data
    jev, names+evidence same questions, with http_status/title/tech in the state

What this measures, and what it does not: the gold label comes from HTTP
evidence, so the last two lines and the keyword baseline are being scored on
material that is in the family of the label. This answers "does the pipeline
recover what the evidence says is there", not "does it find real bugs". The
`subtle` tier is where it gets interesting, because there the evidence is
ambiguous and a keyword list has nothing to grab.

    python scripts/benchmark.py --bench bench/ [--cache bench/cache.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, os.path.dirname(HERE)):        # scripts/ and the repo root
    if path not in sys.path:
        sys.path.insert(0, path)

from make_benchmark import KEYWORDS  # noqa: E402

from jev_recon.cache import ResponseCache  # noqa: E402
from jev_recon.config import load_env  # noqa: E402
from jev_recon.jev import JevClient  # noqa: E402
from jev_recon.preprocess import ParserOptions, prepare  # noqa: E402
from jev_recon.rank import DEFAULT_WEIGHTS, build_assets  # noqa: E402

KS = (0.05, 0.10, 0.20)


def load_bench(directory: str) -> tuple[list[dict], dict[str, str]]:
    with open(os.path.join(directory, "hosts.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    with open(os.path.join(directory, "labels.json"), encoding="utf-8") as fh:
        labels = json.load(fh)
    return rows, labels


def keyword_score(row: dict) -> float:
    haystack = f"{row.get('title', '')} {' '.join(map(str, row.get('technologies') or []))}"
    haystack = haystack.lower()
    return float(sum(1 for word in KEYWORDS if word in haystack))


def rank_by(hosts: list[str], scores: dict[str, float]) -> list[str]:
    return sorted(hosts, key=lambda h: (-scores.get(h, 0.0), h))


def metrics(ranking: list[str], labels: dict[str, str], k: float,
            tier: str | None = None) -> dict:
    cutoff = max(1, int(len(ranking) * k))
    top = ranking[:cutoff]
    if tier is None:
        gold = {h for h, t in labels.items() if t != "boring"}
    else:
        gold = {h for h, t in labels.items() if t == tier}
    hits = len(set(top) & gold)
    return {
        "k": k,
        "cutoff": cutoff,
        "gold": len(gold),
        "hits": hits,
        "precision": round(hits / cutoff, 3),
        "recall": round(hits / len(gold), 3) if gold else 0.0,
    }


def jev_scores(rows: list[dict], with_evidence: bool, cache_path: str | None,
               batch_size: int, concurrency: int) -> dict[str, float]:
    meta = {} if not with_evidence else {
        row["hostname"]: {
            "http_status": row["http_status"], "title": row["title"],
            "technologies": row["technologies"], "server": row["server"],
        }
        for row in rows
    }
    hosts = [row["hostname"] for row in rows]
    candidates = prepare(
        hosts, ParserOptions(), metadata=meta, limit=0
    ).candidates
    cache = ResponseCache(cache_path) if cache_path else None
    client = JevClient(
        api_key=os.environ.get("TYPESAFE_API_KEY", "cache-only"),
        cache=cache, on_event=lambda *_a, **_k: None,
    )
    outcomes = asyncio.run(client.analyze(
        candidates, batch_size=batch_size, concurrency=concurrency
    ))
    if cache is not None:
        cache.save()
    assets = build_assets(outcomes, {c.hostname: c for c in candidates},
                          DEFAULT_WEIGHTS)
    scores = {a["hostname"]: (a["priority"] or 0.0) for a in assets}
    usage = client.stats.as_dict()
    print(f"  jev{' + evidence' if with_evidence else '          '}: "
          f"{usage['requests_sent']} requests sent, {usage['cache_hits']} cache hits, "
          f"{usage['input_tokens']:,} tokens, ${usage['estimated_cost_usd']:.4f}, "
          f"{usage['wall_seconds']}s", file=sys.stderr)
    return scores


def table(results: dict, method_names: list[str]) -> str:
    lines = []
    header = f"{'method':<20}" + "".join(f"{'P@'+str(int(k*100))+'%':>8}{'R@'+str(int(k*100))+'%':>8}" for k in KS)
    lines.append(header)
    lines.append("-" * len(header))
    for name in method_names:
        row = results["overall"][name]
        cells = "".join(f"{row[k]['precision']:>8.3f}{row[k]['recall']:>8.3f}" for k in KS)
        lines.append(f"{name:<20}{cells}")
    lines.append("")
    lines.append("por tier (P@10%, R@10%)")
    for tier in ("obvious", "subtle"):
        lines.append(f"  {tier}")
        for name in method_names:
            row = results["tiers"][tier][name][0.10]
            lines.append(f"    {name:<18}P {row['precision']:.3f}   R {row['recall']:.3f}"
                         f"   ({row['hits']}/{row['gold']} alvos de {row['cutoff']} posicoes)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bench", default="bench")
    parser.add_argument("--cache", default=None,
                        help="one cache file; the two jev conditions have different keys")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--out", default=None, help="write results as JSON")
    args = parser.parse_args()

    load_env()
    rows, labels = load_bench(args.bench)
    hosts = [row["hostname"] for row in rows]
    n_gold = sum(1 for t in labels.values() if t != "boring")
    print(f"{len(hosts)} hosts, {n_gold} com evidencia de superficie privilegiada\n")

    rng = random.Random(args.seed)
    random_scores = {h: rng.random() for h in hosts}

    parsed = prepare(hosts, ParserOptions()).candidates
    if len(parsed) != len(hosts):
        raise SystemExit(
            f"refusing to benchmark: the local filter kept {len(parsed)} of "
            f"{len(hosts)} hosts, so the comparison would be measuring the "
            "filter, not the ranking. Check the parent domains in the set "
            "(reserved TLDs like .example/.test/.invalid are dropped)."
        )
    name_scores = {c.hostname: c.pre["pre_rank"] for c in parsed}
    name_scores = {h: max(0.0, v + 5.0) / 20.0 for h, v in name_scores.items()}

    evidence_scores = {row["hostname"]: keyword_score(row) for row in rows}

    print("rodando o jev (2 condicoes)...", file=sys.stderr)
    jev_names = jev_scores(rows, False, args.cache, args.batch_size, args.concurrency)
    jev_evidence = jev_scores(rows, True, args.cache, args.batch_size, args.concurrency)

    methods = {
        "random": random_scores,
        "name heuristic": name_scores,
        "evidence keywords": evidence_scores,
        "jev (names)": jev_names,
        "jev (names+evidence)": jev_evidence,
    }
    rankings = {name: rank_by(hosts, scores) for name, scores in methods.items()}

    results = {"overall": {}, "tiers": {"obvious": {}, "subtle": {}}}
    for name, ranking in rankings.items():
        results["overall"][name] = {k: metrics(ranking, labels, k) for k in KS}
        for tier in results["tiers"]:
            results["tiers"][tier][name] = {0.10: metrics(ranking, labels, 0.10, tier)}

    print()
    print(table(results, list(methods)))
    print()
    print("nota: o gold vem da evidencia HTTP, entao 'evidence keywords' e as duas "
          "linhas do jev sao avaliadas em material da mesma familia do rotulo.")
    print("      isso mede se o pipeline recupera o que a evidencia diz, nao se "
          "acha bug real. o tier 'subtle' e onde o regex nao tem o que pegar.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"n_hosts": len(hosts), "n_gold": n_gold, **results}, fh, indent=1)
        print(f"\nresultados em {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

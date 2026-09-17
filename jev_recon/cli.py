"""jev-recon command line entry point.

Pipeline: subdomains -> local filters -> candidates -> Jev (many tiny questions,
batched) -> probabilities -> ranking in Python -> interesting.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config
from .cache import ResponseCache
from .jev import JevAuthError, JevClient, JevError
from .preprocess import (
    STATE_META_FIELDS,
    ParserOptions,
    load_input,
    load_metadata,
    prepare,
)
from .rank import (
    build_assets,
    parse_weights,
    select,
    summary,
)
from .signals import DEFAULT_SIGNAL_ORDER, SIGNALS

SIGNAL_SHORT = {
    "production": "likely_production",
    "sensitive": "likely_sensitive",
    "internal": "likely_internal",
    "staging": "likely_staging",
    "admin": "likely_admin",
    "api": "likely_api",
    "interesting": "interesting_for_security_research",
}
SIGNAL_HEADER = {
    "production": "prod", "sensitive": "sens", "internal": "intl",
    "staging": "stag", "admin": "admin", "api": "api", "interesting": "inter",
}
BAR = "─" * 36


# ---------------------------------------------------------------------------
# logging: events to stderr, report to stdout, so `> out.txt` stays clean
# ---------------------------------------------------------------------------


class Reporter:
    def __init__(self, quiet: bool = False, total_batches: int = 0):
        self.quiet = quiet
        self.total = max(1, total_batches)
        self.done = 0
        self.errors: list[str] = []
        self.tty = sys.stderr.isatty()
        self._width = 0

    def event(self, kind: str, message: str = "") -> None:
        if kind == "batch_done":
            self.done += 1
            if self.tty and not self.quiet:
                line = f"  jev: {self.done}/{self.total} requests done"
                pad = max(0, self._width - len(line))
                sys.stderr.write("\r" + line + " " * pad)
                sys.stderr.flush()
                self._width = len(line)
            elif self.done % max(1, self.total // 10) == 0 and not self.quiet:
                print(
                    f"  jev: {self.done}/{self.total} requests done",
                    file=sys.stderr,
                    flush=True,
                )
            return
        if self.quiet:
            return
        if kind in {"error", "split", "429"}:
            self.errors.append(message)
        prefix = {"retry": "  retry:", "429": "  rate-limit:", "split": "  split:",
                  "error": "  error:", "plan": "  plan:"}.get(kind, f"  {kind}:")
        print(f"{prefix} {message}", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.tty and not self.quiet and self.done:
            sys.stderr.write("\r" + " " * (self._width + 8) + "\r")
            sys.stderr.flush()


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------


def resolve_meta_fields(spec: str | None, send_all: bool) -> frozenset[str]:
    """Which metadata keys reach the state. Jev is explicit: filter first."""
    if send_all:
        return frozenset(STATE_META_FIELDS | {"*"})
    if not spec:
        return STATE_META_FIELDS
    chosen = {token.strip() for token in spec.replace(" ", "").split(",") if token.strip()}
    if not chosen:
        raise ValueError("--meta-fields was empty")
    return frozenset(chosen)


def resolve_signals(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return DEFAULT_SIGNAL_ORDER
    chosen: list[str] = []
    for token in spec.replace(" ", "").split(","):
        if not token:
            continue
        long_name = SIGNAL_SHORT.get(token, token)
        if long_name not in SIGNALS:
            raise ValueError(
                f"unknown signal '{token}'. Choose from: "
                + ", ".join(sorted(SIGNAL_SHORT))
                + " (or the full names: "
                + ", ".join(DEFAULT_SIGNAL_ORDER)
                + ")"
            )
        if long_name not in chosen:
            chosen.append(long_name)
    if not chosen:
        raise ValueError("--signals was empty")
    return tuple(chosen)


# ---------------------------------------------------------------------------
# reporting helpers
# ---------------------------------------------------------------------------


def print_funnel(
    total_lines: int,
    report,
    candidates: int,
    batches: int,
    batch_size: int,
    concurrency: int,
    high_interest: int,
    dry_run: bool,
    elapsed: float,
    threshold: float = 0.0,
) -> None:
    print(f"{total_lines} subdomains")
    print("        ↓")
    dropped = sum(report.dropped.values())
    detail = ", ".join(f"{k} {v}" for k, v in sorted(report.dropped.items()) if v)
    suffix = f"  ({dropped} removed: {detail})" if dropped else ""
    print(f"{candidates} candidates{suffix}")
    print("        ↓")
    mode = "dry run, nothing sent" if dry_run else f"batch {batch_size} · concurrency {concurrency}"
    print(f"Jev analysis  ({batches} requests · {mode} · {elapsed:.1f}s)")
    print("        ↓")
    print(f"{high_interest} high-interest assets  (priority >= {threshold:.2f})")
    print()


def print_top(assets: list[dict]) -> None:
    print("TOP ASSETS")
    print(BAR)
    for asset in assets:
        if asset["priority"] is None:
            continue
        print(f"{asset['priority']:.2f}  {asset['hostname']}")
    print()


def print_explain(assets: list[dict], explain: int) -> None:
    if explain <= 0:
        return
    print(f"SIGNALS (first {explain})")
    print(BAR)
    header = " " * 22 + "".join(f"{SIGNAL_HEADER[n]:>8}" for n in SIGNAL_SHORT)
    print(header)
    for asset in assets[:explain]:
        if asset["priority"] is None:
            continue
        cells = ""
        for short, long_name in SIGNAL_SHORT.items():
            value = asset["signals"].get(long_name)
            cells += f"{'  -  ':>8}" if value is None else f"{value:>8.2f}"
        print(f"{asset['priority']:.2f}  {asset['hostname'][:17]:<17}{cells}")
    print()


def print_footer(stats: dict, summary_dict: dict, signals: tuple[str, ...]) -> None:
    print("RUN")
    print(BAR)
    print(f"  signals        {', '.join(signals)}")
    print(f"  scored         {summary_dict['scored']}/{summary_dict['assets']}"
          f"   incomplete {summary_dict['incomplete']}"
          f"   unscored {summary_dict['unscored']}")
    print(f"  requests       {stats['requests_sent']} sent"
          f"   retries {stats['retries']}   rate-limit pauses {stats['rate_limit_pauses']}")
    if stats["cache_hits"] or stats["cache_writes"]:
        print(f"  cache          {stats['cache_hits']} hits"
              f"   {stats['cache_writes']} stored")
    print(f"  tokens         in {stats['input_tokens']:,}  out {stats['output_tokens']:,}"
          f"   est. cost ${stats['estimated_cost_usd']:.4f}"
          f"   ({', '.join(stats['models_seen']) or 'no response yet'})")
    print()


def print_dry_run(plan: dict, payload: dict | None) -> None:
    print("DRY RUN — nothing was sent to the API")
    print(BAR)
    print(f"  requests planned      {plan['requests']}")
    print(f"  candidates per request{plan['batch_size']:>7}")
    print(f"  questions per request {plan['questions_per_request']}")
    print(f"  concurrency           {plan['concurrency']}")
    print(f"  est. input tokens     {plan['estimated_input_tokens']:,}")
    print(f"  est. cost             ${plan['estimated_cost_usd']:.4f}"
          f"   (@ ${plan['price_per_mtok']}/Mtok, output free)")
    if payload:
        preview = json.dumps(payload, indent=2)
        if len(preview) > 2600:
            preview = preview[:2600] + "\n  … (truncated)"
        print()
        print("FIRST REQUEST")
        print(BAR)
        print(preview)
    print()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    if args.input in {"-", "/dev/stdin"} and sys.stdin.isatty():
        print(
            "no input: pass a file, or pipe one in "
            "(e.g. subfinder -d alvo -silent | jev-recon)",
            file=sys.stderr,
        )
        return 2
    lines, meta = load_input(args.input)
    if args.meta:
        meta = {**meta, **load_metadata(args.meta)}

    opts = ParserOptions(
        allow_ip=args.allow_ip,
        allow_underscore=args.allow_underscore,
        drop_throwaway=args.drop_throwaway,
        max_per_parent=args.max_per_parent,
        state_meta_fields=resolve_meta_fields(args.meta_fields, args.meta_all),
    )
    report = prepare(lines, opts, metadata=meta, limit=args.limit)
    candidates = report.candidates
    if not candidates:
        print("No candidate survived local filtering. Nothing to ask Jev.", file=sys.stderr)
        return 0

    weights = parse_weights(args.weights)
    signals = resolve_signals(args.signals)
    reporter = Reporter(quiet=args.quiet)
    cache = ResponseCache(args.cache) if args.cache else None

    client = JevClient(
        api_key=args.api_key or "",
        base_url=args.base_url,
        model=args.model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_questions_per_request=args.max_questions_per_request,
        max_request_tokens=args.max_request_tokens,
        signals=signals,
        cache=cache,
        on_event=reporter.event,
    )
    batch_size = client.effective_batch_size(args.batch_size, [c.hostname for c in candidates[: args.batch_size]])
    batches = client.split_batches(candidates, batch_size)
    reporter.total = max(1, len(batches))

    if args.dry_run:
        payload = None
        if batches:
            from .signals import build_request

            payload = build_request(batches[0], client.model, signals)
        plan = {
            "requests": len(batches),
            "batch_size": batch_size,
            "questions_per_request": len(signals) * batch_size + 2,
            "concurrency": args.concurrency,
            "estimated_input_tokens": estimate_payload_tokens(batches, signals, client.model),
            "price_per_mtok": 0.042,
        }
        plan["estimated_cost_usd"] = round(
            plan["estimated_input_tokens"] / 1_000_000 * plan["price_per_mtok"], 5
        )
        print_funnel(
            len(lines), report, len(candidates), len(batches), batch_size,
            args.concurrency, 0, True, time.monotonic() - started,
            args.threshold,
        )
        print_dry_run(plan, payload)
        return 0

    if not args.api_key and not os.environ.get(config.API_KEY_ENV):
        print(
            f"No API key. Put {config.API_KEY_ENV}=... in one of:\n  "
            + "\n  ".join(args.env_tried)
            + "\nor export it. Get one at https://console.typesafe.ai/settings/keys",
            file=sys.stderr,
        )
        return 2

    try:
        outcomes = await client.analyze(
            candidates,
            batch_size=batch_size,
            concurrency=args.concurrency,
            strict=args.strict,
        )
    except JevAuthError as exc:
        reporter.finish()
        print(f"fatal: {exc}", file=sys.stderr)
        return 2
    finally:
        reporter.finish()
        if cache is not None:
            cache.save()

    assets = build_assets(outcomes, {c.hostname: c for c in candidates}, weights)
    high_interest = select(assets, args.threshold)
    stats = client.stats.as_dict()
    summary_dict = summary(assets, args.threshold)

    elapsed = time.monotonic() - started
    print_funnel(
        len(lines), report, len(candidates), len(batches), batch_size,
        args.concurrency, len(high_interest), False, elapsed, args.threshold,
    )
    print_top(high_interest[: args.top] if args.top else high_interest)
    print_explain(high_interest if args.explain else [], args.explain)
    print_footer(stats, summary_dict, signals)
    if not high_interest and summary_dict["scored"]:
        best = next(a for a in assets if a["priority"] is not None)
        print(
            f"note: no asset reached --threshold {args.threshold:.2f}. "
            f"Top score is {best['priority']:.2f} ({best['hostname']}). "
            "Real Jev scores sit lower than the mock's, so pick the cut from the "
            f"data: try --threshold {max(0.0, best['priority'] - 0.05):.2f}, "
            f"or --all-output to keep every asset.",
            file=sys.stderr,
        )
    if stats["batches_failed"]:
        print(
            f"WARNING: {stats['batches_failed']} of {stats['batches_planned']} "
            f"requests failed. Those {summary_dict['unscored']} assets have "
            '"priority": null and "incomplete": true; they are dropped from '
            f"{args.output} unless you also pass --all-output.",
            file=sys.stderr,
        )

    write_outputs(args, assets, high_interest, stats, report, summary_dict, len(lines),
                  batch_size, len(batches), client.model)

    if stats["batches_failed"]:
        return 1
    return 0


def estimate_payload_tokens(batches: list, signals: tuple[str, ...], model: str) -> int:
    from .jev import estimate_tokens
    from .signals import build_request

    total = 0
    for batch in batches[: min(len(batches), 25)]:
        total += estimate_tokens(json.dumps(build_request(batch, model, signals)))
    sampled = min(len(batches), 25)
    return int(total / max(1, sampled) * len(batches))


def write_outputs(args, assets, high_interest, stats, report, summary_dict,
                  total_lines, batch_size, batches, model) -> None:
    write_json(args.output, high_interest)
    if args.all_output:
        write_json(args.all_output, assets)
    if args.report:
        write_json(
            args.report,
            {
                "tool": "jev-recon",
                "version": __version__,
                "model_requested": model,
                "input": {
                    "lines": total_lines,
                    "candidates": len(assets),
                    "preprocess": report.as_dict(),
                },
                "batching": {
                    "batch_size": batch_size,
                    "requests": batches,
                    "concurrency": args.concurrency,
                },
                "signals": list(SIGNALS),
                "weights": parse_weights(args.weights),
                "summary": summary_dict,
                "usage": stats,
                "batch_errors": sorted(
                    {
                        (a["batch"]["batch_error"] or "")
                        for a in assets
                        if a["batch"]["batch_error"]
                    }
                ),
            },
        )


def write_json(path: str, payload) -> None:
    target = Path(path)
    if target.parent and str(target.parent) != ".":
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-recon",
        description=(
            "Triage a large subdomain list with Jev: many tiny semantic "
            "questions per asset, ranking composed in Python."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="?", default="-",
                        help="hosts.txt, assets.json, assets.jsonl, or - to read stdin "
                             "(default, so you can pipe from subfinder)")
    parser.add_argument("--output", default="interesting.json",
                        help="high-interest assets, ranked")
    parser.add_argument("--all-output", default=None,
                        help="optional file with every analyzed asset")
    parser.add_argument("--report", default=None,
                        help="optional run report (counts, usage, cost, errors)")
    parser.add_argument("--threshold", type=float, default=0.55,
                        help="priority at or above which an asset is high-interest")
    parser.add_argument("--top", type=int, default=25, help="rows printed in TOP ASSETS")
    parser.add_argument("--explain", type=int, default=0,
                        help="print the per-signal table for the first N assets")
    parser.add_argument("--signals", default=None,
                        help="comma list: production,sensitive,internal,staging,admin,api,interesting")
    parser.add_argument("--weights", default=None,
                        help='weights, e.g. \'{"production":0.25,"sensitive":0.25,'
                             '"admin":0.15,"api":0.15,"interesting":0.2}\' or k=v pairs')
    parser.add_argument("--model", default=None, help="default: $TYPESAFE_DEFAULT_MODEL or jev-latest")
    parser.add_argument("--base-url", default=None, help="default: $TYPESAFE_BASE_URL")
    parser.add_argument("--api-key", default=None, help="default: $TYPESAFE_API_KEY")
    parser.add_argument("--env-file", default=".env", help=".env to load, if present")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="candidates per request (shrunk if it would exceed limits)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="requests in flight at once")
    parser.add_argument("--max-questions-per-request", type=int, default=220)
    parser.add_argument("--max-request-tokens", type=int, default=24000,
                        help="documented cap is 32k for state + longest question")
    parser.add_argument("--timeout", type=float, default=60.0, help="per request, seconds")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="analyze only the top N candidates")
    parser.add_argument("--max-per-parent", type=int, default=0,
                        help="cap candidates per registered parent domain (0 = off)")
    parser.add_argument("--drop-throwaway", action="store_true",
                        help="drop dev/test/qa names instead of only annotating them")
    parser.add_argument("--allow-ip", action="store_true", help="keep IP literals")
    parser.add_argument("--allow-underscore", action="store_true",
                        help="keep labels with underscores")
    parser.add_argument("--meta", default=None, help="JSON/JSONL metadata keyed by hostname")
    parser.add_argument("--meta-fields", default=None,
                        help="comma list of metadata keys to put in the state "
                             "(default: the decision-relevant ones)")
    parser.add_argument("--meta-all", action="store_true",
                        help="send every metadata field to Jev, noise included")
    parser.add_argument("--cache", default=None,
                        help="JSON file with responses, keyed by request body. "
                             "Re-ranking with new weights then costs nothing")
    parser.add_argument("--strict", action="store_true", help="abort on the first failed request")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the request plan and exit without calling the API")
    parser.add_argument("--quiet", action="store_true", help="no progress or event log")
    parser.add_argument("--version", action="version", version=f"jev-recon {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _, args.env_tried = config.load_env(args.env_file)
    args.api_key, args.base_url, args.model = config.resolve(
        args.api_key, args.base_url, args.model
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (JevError, ValueError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

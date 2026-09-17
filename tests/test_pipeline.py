"""End-to-end tests against the mock TypeSafe API (scripts/mock_typesafe_server.py).

These cover the parts that only show up when real HTTP is involved: batching
arithmetic, bounded concurrency, retry on 429/5xx, adaptive splitting on 422,
and the CLI writing its output files.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from mock_typesafe_server import serve  # noqa: E402

from jev_recon.cli import main  # noqa: E402
from jev_recon.jev import JevAuthError, JevClient  # noqa: E402
from jev_recon.preprocess import prepare  # noqa: E402
from jev_recon.rank import DEFAULT_WEIGHTS, build_assets, select  # noqa: E402

API_KEY = "test-key-0123456789abcdef"
HOSTS = [
    "admin-api.example.com", "vpn.example.com", "api.internal.example.com",
    "dashboard.example.com", "jenkins.example.com", "grafana.example.com",
    "dev-api.example.com", "test.example.com", "qa.example.com",
    "static.cdn.example.com", "www.example.com", "images.example.com",
]


class MockServer:
    """Run the mock API in a thread on an ephemeral port."""

    def __init__(self, **kwargs):
        self.httpd = serve(0, "127.0.0.1", **kwargs)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def client_for(server, **kwargs):
    kwargs.setdefault("max_retries", 3)
    kwargs.setdefault("backoff_initial", 0.01)
    kwargs.setdefault("backoff_max", 0.05)
    return JevClient(API_KEY, base_url=server.base_url, model="jev-latest", **kwargs)


def run(coro):
    return asyncio.run(coro)


class TestBatching(unittest.TestCase):
    def test_one_request_per_batch_and_every_candidate_scored(self):
        candidates = prepare(HOSTS).candidates
        with MockServer() as server:
            client = client_for(server)
            outcomes = run(client.analyze(candidates, batch_size=5, concurrency=3))

        self.assertEqual(len(outcomes), 3)                      # 12 candidates / 5
        self.assertEqual(client.stats.requests_sent, 3)         # 3 HTTP calls, not 12
        self.assertEqual([len(o.hostnames) for o in outcomes], [5, 5, 2])
        scored = {
            host: signals
            for outcome in outcomes
            for host, signals in zip(outcome.hostnames, outcome.signals)
        }
        self.assertEqual(set(scored), set(HOSTS))
        for signals in scored.values():
            self.assertEqual(len(signals), 7)
            self.assertTrue(all(v is not None for v in signals.values()))
        self.assertEqual(client.stats.batches_failed, 0)
        self.assertIn("jev-1.13.0", client.stats.models_seen)

    def test_requests_are_not_serialised(self):
        """12 batches at concurrency 12 with per-request latency finish fast."""
        candidates = prepare(HOSTS).candidates
        with MockServer(latency=0.2) as server:
            client = client_for(server)
            outcomes = run(client.analyze(candidates, batch_size=1, concurrency=12))
        self.assertEqual(len(outcomes), 12)
        # serial would be >= 2.4s; the limit is generous to survive slow CI
        self.assertLess(client.stats.seconds, 1.6)

    def test_batch_size_is_capped_by_the_question_limit(self):
        candidates = prepare([f"h{i}.example.com" for i in range(30)]).candidates
        with MockServer() as server:
            client = client_for(server, max_questions_per_request=30)
            size = client.effective_batch_size(20, ["a.example.com"])
        self.assertEqual(size, 30 // (7 + 2))                   # 3 candidates


class TestRetries(unittest.TestCase):
    def test_429_is_retried_and_the_retry_after_header_is_honoured(self):
        candidates = prepare(HOSTS).candidates
        with MockServer(rate_limit_every=2, retry_after=0.1) as server:
            client = client_for(server)
            outcomes = run(client.analyze(candidates, batch_size=4, concurrency=2))

        self.assertTrue(all(o.ok for o in outcomes))
        self.assertGreater(client.stats.retries_by_status.get(429, 0), 0)
        self.assertGreaterEqual(client.stats.rate_limit_pauses, 1)
        self.assertTrue(any("429" in str(k) for k in client.stats.retries_by_status))

    def test_5xx_is_retried(self):
        candidates = prepare(HOSTS).candidates
        with MockServer(fail_every=3) as server:
            client = client_for(server)
            outcomes = run(client.analyze(candidates, batch_size=4, concurrency=1))
        self.assertTrue(all(o.ok for o in outcomes))
        self.assertEqual(client.stats.retries_by_status.get(503), 1)

    def test_422_splits_the_batch_instead_of_dropping_it(self):
        candidates = prepare(HOSTS).candidates
        with MockServer(reject_over_questions=40) as server:
            client = client_for(server)
            outcomes = run(client.analyze(candidates, batch_size=10, concurrency=2))

        self.assertGreater(client.stats.adaptive_splits, 0)
        self.assertEqual(client.stats.batches_failed, 0)
        scored = [h for o in outcomes for h in o.hostnames]
        self.assertEqual(sorted(scored), sorted(HOSTS))
        for outcome in outcomes:
            for signals in outcome.signals:
                self.assertTrue(all(v is not None for v in signals.values()))

    def test_a_bad_key_fails_loudly(self):
        candidates = prepare(["api.example.com"]).candidates
        with MockServer() as server:
            client = JevClient("short", base_url=server.base_url, max_retries=0)
            with self.assertRaises(JevAuthError):
                run(client.analyze(candidates, batch_size=1))

    def test_exhausted_retries_are_reported_not_hidden(self):
        candidates = prepare(HOSTS).candidates
        with MockServer(fail_every=1) as server:
            client = client_for(server, max_retries=1)
            outcomes = run(client.analyze(candidates, batch_size=4, concurrency=2))
        self.assertEqual(client.stats.batches_failed, len(outcomes))
        self.assertTrue(all(o.error for o in outcomes))
        assets = build_assets(outcomes, {h: None for h in HOSTS}, DEFAULT_WEIGHTS)
        self.assertEqual(len(assets), len(HOSTS))          # nothing silently vanishes
        self.assertTrue(all(a["priority"] is None for a in assets))
        self.assertEqual(select(assets, 0.5), [])


class TestCli(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.input = os.path.join(self.tmp.name, "subdomains.txt")
        with open(self.input, "w") as fh:
            fh.write("\n".join(HOSTS + ["", "foo.example.com", "1.2.3.4"]) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_run_writes_ranked_json(self):
        out = os.path.join(self.tmp.name, "interesting.json")
        report = os.path.join(self.tmp.name, "report.json")
        with MockServer() as server:
            code = main([
                self.input, "--output", out, "--report", report,
                "--base-url", server.base_url, "--api-key", API_KEY,
                "--batch-size", "6", "--concurrency", "3",
                "--threshold", "0.5", "--env-file", os.path.join(self.tmp.name, "nope.env"),
            ])
        self.assertEqual(code, 0)
        payload = json.loads(open(out).read())
        self.assertTrue(payload)
        self.assertEqual(set(payload[0]), {
            "hostname", "priority", "signals", "relative_pick", "weights_used",
            "missing_signals", "batch", "pre", "metadata", "incomplete",
        })
        priorities = [row["priority"] for row in payload]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        self.assertTrue(all(p >= 0.5 for p in priorities))

        # the mock scores privileged names higher, so the funnel should agree
        top = payload[0]["hostname"]
        self.assertIn(top, {"admin-api.example.com", "api.internal.example.com",
                            "vpn.example.com", "jenkins.example.com",
                            "grafana.example.com", "dashboard.example.com"})
        stats = json.loads(open(report).read())
        self.assertEqual(stats["batching"]["requests"], 2)
        self.assertEqual(stats["signals"][0], "likely_production")
        self.assertEqual(stats["usage"]["batches_failed"], 0)
        self.assertEqual(stats["weights"], DEFAULT_WEIGHTS)

    def test_custom_weights_and_signals_change_the_ranking(self):
        out = os.path.join(self.tmp.name, "out.json")
        with MockServer() as server:
            code = main([
                self.input, "--output", out, "--base-url", server.base_url,
                "--api-key", API_KEY, "--threshold", "0.0", "--top", "0",
                "--weights", "staging=1,production=-0.2",
                "--signals", "production,staging",
                "--env-file", os.path.join(self.tmp.name, "nope.env"),
            ])
        self.assertEqual(code, 0)
        payload = json.loads(open(out).read())
        self.assertIn(payload[0]["hostname"], {
            "dev-api.example.com", "test.example.com", "qa.example.com",
        })
        self.assertEqual(set(payload[0]["weights_used"]), {"staging", "production"})
        top_three = {row["hostname"] for row in payload[:3]}
        self.assertTrue(
            top_three <= {"dev-api.example.com", "test.example.com",
                          "qa.example.com", "static.cdn.example.com"},
            msg=f"staging weight should dominate, got {top_three}",
        )

    def test_dry_run_sends_nothing(self):
        out = os.path.join(self.tmp.name, "never.json")
        code = main([
            self.input, "--output", out, "--dry-run", "--batch-size", "4",
            "--env-file", os.path.join(self.tmp.name, "nope.env"),
        ])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(out))

    def test_missing_key_is_a_fatal_error(self):
        env = dict(os.environ)
        os.environ.pop("TYPESAFE_API_KEY", None)
        try:
            code = main([
                self.input, "--base-url", "http://127.0.0.1:1",
                "--env-file", os.path.join(self.tmp.name, "nope.env"),
            ])
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

"""Ranking tests: the arithmetic that decides the order must be plain code."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  (repo root and scripts/ on sys.path)

from jev_recon.rank import (
    DEFAULT_WEIGHTS,
    build_assets,
    compute_priority,
    diversify,
    explain,
    parse_weights,
    score_namespace,
    select,
    sort_assets,
    summary,
)
from jev_recon.preprocess import name_shape, tracking_namespace  # noqa: E402
from jev_recon.preprocess import ParserOptions, prepare  # noqa: E402


class FakeCandidate:
    def __init__(self, hostname):
        self.hostname = hostname
        self.pre = {"env_token": None}
        self.meta = {}
        self.shape = name_shape(hostname, tuple(hostname.split(".")))


class FakeOutcome:
    def __init__(self, hostnames, signals, relative_pick=None, error=None):
        self.batch_id = 0
        self.hostnames = hostnames
        self.signals = signals
        self.relative_pick = relative_pick or [0.0] * len(hostnames)
        self.incomplete = [False] * len(hostnames)
        self.batch_yield = 1.0
        self.batch_yield_confidence = 0.8
        self.error = error


class TestWeights(unittest.TestCase):
    def test_default_is_the_documented_formula(self):
        self.assertEqual(
            DEFAULT_WEIGHTS,
            {"production": 0.25, "sensitive": 0.25, "admin": 0.15,
             "api": 0.15, "interesting": 0.20},
        )

    def test_parse_json_and_pairs_and_long_names(self):
        self.assertEqual(parse_weights('{"production": 1}'), {"production": 1.0})
        parsed = parse_weights("production=0.5,staging=-0.5")
        self.assertEqual(parsed, {"production": 0.5, "staging": -0.5})
        self.assertEqual(parse_weights("likely_admin=1"), {"admin": 1.0})
        self.assertEqual(
            parse_weights("interesting_for_security_research=2"),
            {"interesting": 2.0},
        )

    def test_unknown_signal_and_empty_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_weights("nonsense=1")
        with self.assertRaises(ValueError):
            parse_weights("production=0")


class TestPriority(unittest.TestCase):
    def test_matches_hand_computed_formula(self):
        signals = {
            "likely_production": 0.98,
            "likely_sensitive": 0.96,
            "likely_admin": 0.93,
            "likely_api": 0.97,
            "interesting_for_security_research": 0.95,
        }
        expected = (
            0.98 * 0.25 + 0.96 * 0.25 + 0.93 * 0.15 + 0.97 * 0.15 + 0.95 * 0.20
        )
        priority, used, missing = compute_priority(
            score_namespace(signals), DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(priority, round(expected, 3), places=3)
        self.assertEqual(missing, [])
        self.assertAlmostEqual(sum(used.values()), 1.0, places=3)

    def test_unweighted_signals_are_ignored(self):
        base = {"likely_production": 0.5, "likely_sensitive": 0.5,
                "likely_admin": 0.5, "likely_api": 0.5,
                "interesting_for_security_research": 0.5}
        noisy = dict(base, likely_internal=0.99, likely_staging=0.99)
        self.assertEqual(
            compute_priority(score_namespace(base), DEFAULT_WEIGHTS)[0],
            compute_priority(score_namespace(noisy), DEFAULT_WEIGHTS)[0],
        )

    def test_missing_signals_lower_the_score_and_never_inflate_it(self):
        """Unmeasured signals must not make an asset look better than it is."""
        full = {"likely_production": 0.9, "likely_sensitive": 0.9,
                "likely_admin": 0.9, "likely_api": 0.9,
                "interesting_for_security_research": 0.9}
        complete = compute_priority(score_namespace(full), DEFAULT_WEIGHTS)[0]
        partial = dict(full)
        partial["interesting_for_security_research"] = None
        priority, used, missing = compute_priority(
            score_namespace(partial), DEFAULT_WEIGHTS
        )
        self.assertAlmostEqual(complete, 0.9, places=3)
        # 0.9 * (1 - 0.20), because the 0.20 weight stays in the denominator
        self.assertAlmostEqual(priority, 0.72, places=3)
        self.assertLess(priority, complete)
        self.assertEqual(missing, ["interesting"])
        self.assertAlmostEqual(sum(used.values()), 0.8, places=3)

    def test_all_missing_is_none_not_zero(self):
        priority, used, missing = compute_priority(
            score_namespace({}), DEFAULT_WEIGHTS
        )
        self.assertIsNone(priority)
        self.assertEqual(used, {})
        self.assertEqual(len(missing), 5)

    def test_negative_weight_penalises_staging(self):
        weights = parse_weights("production=1,staging=-1")
        prod = compute_priority(
            score_namespace({"likely_production": 1.0, "likely_staging": 0.0}),
            weights,
        )[0]
        stage = compute_priority(
            score_namespace({"likely_production": 0.0, "likely_staging": 1.0}),
            weights,
        )[0]
        self.assertGreater(prod, stage)
        self.assertGreaterEqual(stage, 0.0)

    def test_result_never_leaves_zero_to_one(self):
        weights = parse_weights("production=3")
        self.assertEqual(
            compute_priority(score_namespace({"likely_production": 1.0}), weights)[0],
            1.0,
        )
        self.assertEqual(
            compute_priority(score_namespace({"likely_production": 0.0}), weights)[0],
            0.0,
        )


class TestAssets(unittest.TestCase):
    def make(self):
        outcome = FakeOutcome(
            hostnames=["admin-api.example.com", "static.example.com"],
            signals=[
                {"likely_production": 0.98, "likely_sensitive": 0.96,
                 "likely_admin": 0.93, "likely_api": 0.97,
                 "interesting_for_security_research": 0.95,
                 "likely_internal": 0.3, "likely_staging": 0.05},
                {"likely_production": 0.4, "likely_sensitive": 0.1,
                 "likely_admin": 0.05, "likely_api": 0.02,
                 "interesting_for_security_research": 0.12,
                 "likely_internal": 0.02, "likely_staging": 0.4},
            ],
            relative_pick=[0.81, 0.03],
        )
        candidates = {h: FakeCandidate(h) for h in outcome.hostnames}
        return build_assets([outcome], candidates, DEFAULT_WEIGHTS)

    def test_assets_are_ranked_and_shaped_like_the_contract(self):
        assets = self.make()
        top = assets[0]
        self.assertEqual(top["hostname"], "admin-api.example.com")
        self.assertGreater(top["priority"], assets[1]["priority"])
        self.assertEqual(set(top["signals"]), {
            "likely_production", "likely_sensitive", "likely_internal",
            "likely_staging", "likely_admin", "likely_api",
            "interesting_for_security_research",
        })
        self.assertEqual(top["batch"]["id"], 0)
        self.assertEqual(top["incomplete"], False)
        self.assertTrue(top["reasons"])

    def test_reasons_are_ordered_by_what_moved_the_score(self):
        asset = {
            "signals": {"likely_production": 0.98, "likely_sensitive": 0.96,
                        "likely_admin": 0.93, "likely_api": 0.97,
                        "interesting_for_security_research": 0.95},
            "relative_pick": 0.2, "weights_used": DEFAULT_WEIGHTS,
            "metadata": {"http_status": 403, "title": "Jenkins",
                         "technologies": ["Jenkins", "nginx"]},
            "pre": {"env_token": None, "privileged_labels": ["jenkins"]},
        }
        reasons = explain(asset)
        # production and sensitive carry 0.25 each, so they lead; api at 0.15
        self.assertTrue(reasons[0].startswith("production 0.98"))
        self.assertIn("sensitive 0.96", reasons[1])
        self.assertTrue(any(r.startswith("evidence: gated, HTTP 403") for r in reasons))
        self.assertTrue(any("Jenkins" in r for r in reasons))
        self.assertTrue(any(r.startswith("code: known labels jenkins") for r in reasons))

    def test_reasons_stay_quiet_when_nothing_crossed_the_floor(self):
        asset = {
            "signals": {"likely_admin": 0.2, "likely_api": 0.1},
            "relative_pick": 0.0, "weights_used": DEFAULT_WEIGHTS,
            "metadata": {}, "pre": {},
        }
        self.assertEqual(explain(asset), [])

    def test_select_and_summary(self):
        assets = self.make()
        high = select(assets, 0.7)
        self.assertEqual([a["hostname"] for a in high], ["admin-api.example.com"])
        stats = summary(assets, 0.7)
        self.assertEqual(stats["assets"], 2)
        self.assertEqual(stats["high_interest"], 1)
        self.assertEqual(stats["signal_hits_over_0.5"]["production"], 1)

    def test_failed_batch_keeps_its_assets_visible(self):
        outcome = FakeOutcome(["a.example.com"], [{}], error="HTTP 503")
        assets = build_assets([outcome], {"a.example.com": FakeCandidate("a.example.com")},
                              DEFAULT_WEIGHTS)
        self.assertEqual(len(assets), 1)
        self.assertIsNone(assets[0]["priority"])
        self.assertEqual(assets[0]["batch"]["batch_error"], "HTTP 503")

    def test_sort_is_stable_on_ties(self):
        assets = [
            {"priority": 0.5, "relative_pick": 0.1, "hostname": "b.example.com"},
            {"priority": 0.5, "relative_pick": 0.2, "hostname": "a.example.com"},
            {"priority": None, "relative_pick": 0.9, "hostname": "z.example.com"},
        ]
        order = [a["hostname"] for a in sort_assets(assets)]
        self.assertEqual(order, ["a.example.com", "b.example.com", "z.example.com"])


class TestShapeDiversity(unittest.TestCase):
    """One service with 13 regional names must not eat 13 output slots."""

    def test_regions_versions_counters_and_hashes_collapse(self):
        cases = {
            "us-central-3.api.acme.com": "api.acme.com",
            "us-central-4.api.acme.com": "api.acme.com",
            "eu-west-1.api.acme.com": "api.acme.com",
            "api.acme.com": "api.acme.com",
            "api.widget-v2.acme.com": "api.widget.acme.com",
            "widget-3p5-0621.us-east-1.api.acme.com": "api.widget.acme.com",
            "17db54672c72a9ba.cdn.example.com": "cdn.example.com",
            "web01.example.com": "web01.example.com",
            "db01.example.com": "db01.example.com",
        }
        for host, expected in cases.items():
            self.assertEqual(name_shape(host, tuple(host.split("."))), expected, msg=host)

    def test_environment_tokens_are_not_collapsed(self):
        """dev-api and api are different surfaces, not copies."""
        self.assertNotEqual(
            name_shape("us-central-1.api.acme.com", ("us-central-1", "api", "x", "ai")),
            name_shape("staging-cm-api.acme.com", ("staging-cm-api", "x", "ai")),
        )

    def test_diversify_keeps_the_best_and_marks_the_copies(self):
        assets = [
            {"hostname": "us-central-3.api.acme.com", "priority": 0.7, "shape": "api.acme.com",
             "relative_pick": 0.1, "incomplete": False, "signals": {}, "batch": {}},
            {"hostname": "us-central-4.api.acme.com", "priority": 0.68, "shape": "api.acme.com",
             "relative_pick": 0.1, "incomplete": False, "signals": {}, "batch": {}},
            {"hostname": "us-central-5.api.acme.com", "priority": 0.66, "shape": "api.acme.com",
             "relative_pick": 0.1, "incomplete": False, "signals": {}, "batch": {}},
            {"hostname": "auth.acme.com", "priority": 0.65, "shape": "auth.acme.com",
             "relative_pick": 0.1, "incomplete": False, "signals": {}, "batch": {}},
        ]
        kept, dropped = diversify(assets, 2)
        self.assertEqual([a["hostname"] for a in kept],
                         ["us-central-3.api.acme.com", "us-central-4.api.acme.com", "auth.acme.com"])
        self.assertEqual(dropped, 1)
        self.assertEqual(assets[0]["shape_rank"], 1)
        self.assertEqual(assets[1]["same_shape_count"], 2)
        self.assertNotIn("suppressed_by_shape", assets[1])
        self.assertTrue(assets[2]["suppressed_by_shape"])

    def test_zero_disables_diversity_but_still_annotates(self):
        assets = [
            {"hostname": f"r{i}.api.acme.com", "priority": 0.7 - i / 100, "shape": "api.acme.com",
             "relative_pick": 0.0, "incomplete": False, "signals": {}, "batch": {}}
            for i in range(4)
        ]
        kept, dropped = diversify(assets, 0)
        self.assertEqual(len(kept), 4)
        self.assertEqual(dropped, 0)
        self.assertEqual([a["shape_rank"] for a in assets], [1, 2, 3, 4])


class TestTrackingNamespace(unittest.TestCase):
    """Third-party delivery infra wears the target's domain and a fancy label."""

    def test_two_vocabularies_meeting_is_what_makes_it_tracking(self):
        pos = ["click.c.email.api.acme.com", "click.email.acme.com",
               "c.email.acme.com", "track.links.mail.acme.com",
               "url.ct.mailer.acme.com", "email.click.sso.acme.com"]
        neg = ["mail.acme.com", "smtp.acme.com", "email.acme.com",
               "api.acme.com", "click.acme.com", "admin.email.acme.com",
               "links.acme.com"]
        for host in pos:
            self.assertTrue(tracking_namespace(tuple(host.split("."))), msg=host)
        for host in neg:
            self.assertFalse(tracking_namespace(tuple(host.split("."))), msg=host)

    def test_the_fact_is_computed_in_code_and_shipped_in_pre(self):
        report = prepare(["click.c.email.api.acme.com", "mail.acme.com"],
                         ParserOptions())
        facts = {c.hostname: c.pre["tracking_namespace"] for c in report.candidates}
        self.assertTrue(facts["click.c.email.api.acme.com"])
        self.assertFalse(facts["mail.acme.com"])

    def test_penalty_demotes_the_decoy_and_says_why(self):
        """The criteria text did not move these (7/15 before and after); the
        rule in code does, which is where a mechanical rule belongs."""
        hosts = ["click.c.email.api.acme.com", "api.acme.com"]
        candidates = {h: FakeCandidate(h) for h in hosts}
        for candidate in candidates.values():
            candidate.pre["tracking_namespace"] = tracking_namespace(
                tuple(candidate.hostname.split("."))
            )
        same = {"likely_api": 0.9, "likely_sensitive": 0.8}
        outcome = FakeOutcome(hosts, [dict(same), dict(same)])

        by_host = {a["hostname"]: a for a in build_assets(
            [outcome], candidates, DEFAULT_WEIGHTS)}
        decoy, real = by_host[hosts[0]], by_host[hosts[1]]
        self.assertEqual(decoy["priority"], round(real["priority"] * 0.5, 3))
        self.assertIn("code: third-party tracking namespace", decoy["reasons"])

        off = {a["hostname"]: a["priority"] for a in build_assets(
            [outcome], candidates, DEFAULT_WEIGHTS, tracking_penalty=1.0)}
        self.assertEqual(off[hosts[0]], off[hosts[1]])


if __name__ == "__main__":
    unittest.main()

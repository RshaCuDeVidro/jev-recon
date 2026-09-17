"""Local stage tests: what gets kept, what gets dropped, what gets annotated."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from jev_recon.preprocess import (
    NOISE_LABELS,
    ParserOptions,
    load_metadata,
    normalize_host,
    prepare,
    pre_rank,
)


class TestNormalize(unittest.TestCase):
    def test_strips_scheme_path_port_user_and_case(self):
        cases = {
            "https://API.Example.COM/v1/users": "api.example.com",
            "https://vpn.example.com:8443/login": "vpn.example.com",
            "mail.example.com:25": "mail.example.com",
            "user@mail.example.com": "mail.example.com",
            "*.wild.example.com": "wild.example.com",
            "portal.example.com.": "portal.example.com",
            "UPPER.CASE.example.com": "upper.case.example.com",
            "db.example.com # primary": "db.example.com",
            "api.example.com  1.2.3.4": "api.example.com",
            "": None,
            "# comment": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_host(raw), expected, msg=raw)


class TestPrepare(unittest.TestCase):
    def test_funnel_counts(self):
        lines = [
            "api.example.com",
            "api.example.com",          # duplicate
            "API.example.com",          # duplicate after normalisation
            "foo.example.com",          # placeholder label
            "1.2.3.4",                  # ip literal
            "localhost",                # not a hostname
            "under_score.example.com",  # invalid syntax
            "host.example.invalid",     # reserved suffix
            "dev-api.example.com",      # kept, annotated
            "*.wild.example.com",       # wildcard, kept as wild.example.com
            "x.example.com",            # junk label x? no: single-char label is fine
        ]
        report = prepare(lines)
        hosts = [c.hostname for c in report.candidates]
        self.assertIn("api.example.com", hosts)
        self.assertIn("dev-api.example.com", hosts)
        self.assertIn("wild.example.com", hosts)
        self.assertNotIn("foo.example.com", hosts)
        self.assertNotIn("1.2.3.4", hosts)
        self.assertNotIn("host.example.invalid", hosts)
        self.assertEqual(report.dupes, 2)
        self.assertEqual(report.dropped["ip_literal"], 1)
        self.assertEqual(report.dropped["not_a_hostname"], 1)
        self.assertEqual(report.dropped["invalid_syntax"], 1)
        self.assertEqual(report.dropped["reserved_suffix"], 1)
        self.assertEqual(report.dropped["placeholder_label"], 1)

    def test_throwaway_is_annotated_not_dropped_by_default(self):
        report = prepare(["test-api.example.com", "dev-api.example.com"])
        self.assertEqual(len(report.candidates), 2)
        by_host = {c.hostname: c for c in report.candidates}
        self.assertEqual(by_host["test-api.example.com"].pre["env_token"], "test")
        self.assertIn("api", by_host["test-api.example.com"].pre["privileged_labels"])
        self.assertEqual(by_host["dev-api.example.com"].pre["name_tokens"], ["dev", "api"])
        self.assertEqual(by_host["dev-api.example.com"].pre["env_token"], "development")

    def test_drop_throwaway_flag_removes_them(self):
        report = prepare(["test-api.example.com", "api.example.com"],
                         ParserOptions(drop_throwaway=True))
        self.assertEqual([c.hostname for c in report.candidates], ["api.example.com"])
        self.assertEqual(report.dropped["throwaway_only"], 1)

    def test_metadata_is_merged_without_shadowing_core_keys(self):
        report = prepare(
            ["dev-api.example.com"],
            metadata={"dev-api.example.com": {
                "resolved_ips": ["1.2.3.4"], "http_status": 200,
                "title": "Admin Portal", "server": "nginx",
                "ports": [443, 8443], "technologies": ["nginx", "FastAPI"],
                "hostname": "should-not-shadow",
            }},
        )
        state = report.candidates[0].as_state()
        self.assertEqual(state["hostname"], "dev-api.example.com")
        self.assertEqual(state["labels"], ["dev-api", "example", "com"])
        self.assertEqual(state["subdomain_depth"], 3)
        self.assertEqual(state["code_extracted"]["name_tokens"], ["dev", "api"])
        self.assertEqual(state["title"], "Admin Portal")
        self.assertEqual(state["technologies"], ["nginx", "FastAPI"])
        self.assertIn("code_extracted", state)

    def test_max_per_parent_keeps_the_interesting_ones(self):
        lines = [f"www{i}.corp.com" for i in range(10)]
        lines += ["admin.corp.com", "api.corp.com", "vpn.corp.com"]
        report = prepare(lines, ParserOptions(max_per_parent=4))
        kept = [c.hostname for c in report.candidates]
        self.assertEqual(len(kept), 4)
        self.assertIn("admin.corp.com", kept)
        self.assertIn("api.corp.com", kept)
        self.assertEqual(report.dropped["capped_per_parent"], 9)

    def test_pre_rank_rewards_privileged_and_punishes_noise(self):
        good = prepare(["admin-api.corp.com"]).candidates[0].pre
        meh = prepare(["static1.cdn.corp.com"]).candidates[0].pre
        self.assertGreater(pre_rank(good), pre_rank(meh))
        self.assertTrue(set(meh["noise_labels"]) & NOISE_LABELS)

    def test_limit_keeps_highest_pre_rank(self):
        report = prepare(
            ["static.corp.com", "admin.corp.com", "api.corp.com"], limit=2
        )
        kept = {c.hostname for c in report.candidates}
        self.assertEqual(kept, {"admin.corp.com", "api.corp.com"})
        self.assertEqual(report.dropped["limit"], 1)

    def test_candidate_ids_follow_output_order(self):
        report = prepare(["api.corp.com", "admin.corp.com"])
        self.assertEqual(report.candidates[0].cid, "c00000")
        self.assertEqual(report.candidates[1].cid, "c00001")


class TestMetadataLoader(unittest.TestCase):
    def test_json_object_jsonl_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            as_object = os.path.join(tmp, "m1.json")
            with open(as_object, "w") as fh:
                json.dump({"api.example.com": {"http_status": 200}}, fh)
            self.assertEqual(
                load_metadata(as_object)["api.example.com"]["http_status"], 200
            )

            as_jsonl = os.path.join(tmp, "m2.jsonl")
            with open(as_jsonl, "w") as fh:
                fh.write('{"hostname": "api.example.com", "server": "nginx"}\n')
                fh.write('{"hostname": "dev.example.com", "server": "gunicorn"}\n')
            meta = load_metadata(as_jsonl)
            self.assertEqual(meta["api.example.com"]["server"], "nginx")
            self.assertEqual(meta["dev.example.com"]["server"], "gunicorn")

            as_list = os.path.join(tmp, "m3.json")
            with open(as_list, "w") as fh:
                json.dump([{"hostname": "vpn.example.com", "http_status": 401}], fh)
            self.assertEqual(
                load_metadata(as_list)["vpn.example.com"]["http_status"], 401
            )


if __name__ == "__main__":
    unittest.main()

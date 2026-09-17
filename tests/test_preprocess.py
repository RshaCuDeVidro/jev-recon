"""Local stage tests: what gets kept, what gets dropped, what gets annotated."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401  (repo root and scripts/ on sys.path)

from jev_recon.config import env_search_paths, load_env  # noqa: E402
from jev_recon.preprocess import (  # noqa: E402
    NOISE_LABELS,
    STATE_META_FIELDS,
    ParserOptions,
    canonical_meta,
    load_input,
    load_metadata,
    normalize_host,
    prepare,
    pre_rank,
    rows_to_input,
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


class TestToolPipelineInput(unittest.TestCase):
    """subfinder and httpx -json must feed in without a translation step."""

    def test_subfinder_lines_and_stdin(self):
        import io
        import sys as _sys

        original = _sys.stdin
        _sys.stdin = io.StringIO("api.example.com\nadmin.example.com\n")
        try:
            lines, meta = load_input("-")
        finally:
            _sys.stdin = original
        self.assertEqual(lines, ["api.example.com", "admin.example.com"])
        self.assertEqual(meta, {})

    def test_httpx_json_lines_are_normalised(self):
        row = {
            "timestamp": "2026-09-17T10:00:00Z",
            "port": "443",
            "url": "https://admin.corp.example.com/login",
            "input": "admin.corp.example.com",
            "status_code": 200,
            "title": "Admin Portal",
            "tech": ["nginx", "FastAPI"],
            "webserver": "nginx/1.24",
            "host_ip": "203.0.113.10",
            "cdn_name": "cloudflare",
        }
        lines, meta = rows_to_input([row])
        self.assertEqual(lines, ["admin.corp.example.com"])
        enriched = meta["admin.corp.example.com"]
        self.assertEqual(enriched["http_status"], 200)
        self.assertEqual(enriched["technologies"], ["nginx", "FastAPI"])
        self.assertEqual(enriched["server"], "nginx/1.24")
        self.assertEqual(enriched["resolved_ips"], ["203.0.113.10"])
        self.assertEqual(enriched["ports"], ["443"])
        self.assertEqual(enriched["title"], "Admin Portal")
        self.assertEqual(enriched["cdn"], "cloudflare")
        self.assertNotIn("input", enriched)
        self.assertNotIn("url", enriched["technologies"])

    def test_scalar_fields_become_lists_and_survive_enrichment(self):
        meta = canonical_meta({"host": "a.b.com", "ip": "1.1.1.1,2.2.2.2",
                               "port": 8443, "cname": "x.cdn.net"})
        self.assertEqual(meta["resolved_ips"], ["1.1.1.1", "2.2.2.2"])
        self.assertEqual(meta["ports"], ["8443"])
        self.assertEqual(meta["cname"], ["x.cdn.net"])

    def test_unknown_keys_pass_through_untouched(self):
        meta = canonical_meta({"hostname": "a.b.com", "weird_field": {"x": 1}})
        self.assertEqual(meta["weird_field"], {"x": 1})

    def test_jsonl_file_and_bad_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.jsonl")
            with open(path, "w") as fh:
                fh.write(json.dumps({"host": "api.example.com", "status_code": 401}) + "\n")
                fh.write(json.dumps({"host": "dev.example.com", "tech": "gunicorn"}) + "\n")
            lines, meta = load_input(path)
            self.assertEqual(sorted(lines), ["api.example.com", "dev.example.com"])
            self.assertEqual(meta["api.example.com"]["http_status"], 401)
            self.assertEqual(meta["dev.example.com"]["technologies"], ["gunicorn"])

            broken = os.path.join(tmp, "broken.jsonl")
            with open(broken, "w") as fh:
                fh.write("{oops\n")
            with self.assertRaises(ValueError):
                load_input(broken)

    def test_two_rows_per_host_merge_without_erasing(self):
        """httpx writes one row per port; the redirect row has no title."""
        rows = [
            {"host": "a.example.com", "port": "80", "status_code": 308,
             "location": "https://a.example.com/"},
            {"host": "a.example.com", "port": "443", "status_code": 200,
             "title": "Login", "tech": ["nginx"]},
        ]
        lines, meta = rows_to_input(rows)
        self.assertEqual(lines, ["a.example.com", "a.example.com"])
        self.assertEqual(meta["a.example.com"]["http_status"], 200)
        self.assertEqual(meta["a.example.com"]["title"], "Login")
        self.assertEqual(meta["a.example.com"]["redirect_to"], "https://a.example.com/")

    def test_only_decision_relevant_metadata_is_kept(self):
        """httpx noise is dropped by default: it distracts the model and bloats the file."""
        noisy = {
            "resolved_ips": ["203.0.113.10"], "http_status": 200,
            "title": "Admin", "server": "nginx", "ports": ["443"],
            "technologies": ["nginx"], "timestamp": "2026-09-17T10:00:00Z",
            "resolvers": ["8.8.8.8:53"], "knowledgebase": {"PageType": "error"},
            "words": 5317, "content_type": "text/html",
        }
        report = prepare(["admin.corp.com"], metadata={"admin.corp.com": noisy})
        candidate = report.candidates[0]
        state = candidate.as_state()
        self.assertEqual(state["title"], "Admin")
        self.assertEqual(state["technologies"], ["nginx"])
        for noise in ("timestamp", "resolvers", "knowledgebase", "words",
                      "content_type"):
            self.assertNotIn(noise, state)
            self.assertNotIn(noise, candidate.meta)

    def test_meta_all_keeps_everything(self):
        noisy = {"title": "Admin", "words": 12, "resolvers": ["1.1.1.1:53"]}
        report = prepare(
            ["admin.corp.com"],
            ParserOptions(state_meta_fields=frozenset(STATE_META_FIELDS | {"*"})),
            metadata={"admin.corp.com": noisy},
        )
        state = report.candidates[0].as_state()
        self.assertEqual(state["words"], 12)
        self.assertEqual(state["resolvers"], ["1.1.1.1:53"])
        self.assertEqual(report.candidates[0].meta["words"], 12)

    def test_meta_fields_can_be_narrowed(self):
        report = prepare(
            ["admin.corp.com"],
            ParserOptions(state_meta_fields=frozenset({"title"})),
            metadata={"admin.corp.com": {"title": "Admin", "server": "nginx"}},
        )
        self.assertEqual(report.candidates[0].as_state()["title"], "Admin")
        self.assertNotIn("server", report.candidates[0].as_state())
        self.assertNotIn("server", report.candidates[0].meta)


class TestEnvDiscovery(unittest.TestCase):
    """A pipe from any directory must still find the key."""

    def test_named_file_is_never_second_guessed(self):
        self.assertEqual(env_search_paths("custom.env"), ["custom.env"])
        loaded, tried = load_env("/nonexistent/definitely-not-here.env")
        self.assertEqual(loaded, 0)
        self.assertEqual(tried, ["/nonexistent/definitely-not-here.env"])

    def test_default_search_covers_cwd_project_and_xdg(self):
        tried = env_search_paths()
        self.assertEqual(len(tried), 3)
        self.assertEqual(tried[0], os.path.join(os.getcwd(), ".env"))
        self.assertTrue(tried[1].endswith(".env"))
        self.assertIn(".config/jev-recon", tried[2].replace(os.sep, "/"))

    def test_load_env_reads_the_first_file_that_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as fh:
                fh.write("# comment\nexport JEVRECON_TEST_KEY=abc\nBROKEN LINE\n")
            original = os.environ.pop("JEVRECON_TEST_KEY", None)
            try:
                loaded, _ = load_env(path)
                self.assertEqual(loaded, 1)
                self.assertEqual(os.environ["JEVRECON_TEST_KEY"], "abc")
            finally:
                os.environ.pop("JEVRECON_TEST_KEY", None)
                if original is not None:
                    os.environ["JEVRECON_TEST_KEY"] = original


if __name__ == "__main__":
    unittest.main()

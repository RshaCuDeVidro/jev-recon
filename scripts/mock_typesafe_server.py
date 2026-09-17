#!/usr/bin/env python3
"""A stand-in for https://api.typesafe.ai/v1/systemone. Standard library only.

It answers the exact request/response shape documented at docs.typesafe.ai/api,
using deterministic heuristics over the hostnames, so the whole pipeline can be
demonstrated and tested without an API key. It is not a model: the numbers it
returns are computed from label tokens plus a hash-based jitter.

    python scripts/mock_typesafe_server.py --port 8712

Fault injection, for exercising the client's error paths:

    --rate-limit-every 7     every 7th request answers 429 with Retry-After: 1
    --reject-over-questions 60   requests with more questions get a 422
    --fail-every 5           every 5th request answers 503
    --latency 0.2            seconds of artificial delay per request
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRIVILEGED = {
    "admin", "administrator", "panel", "console", "dashboard", "backoffice",
    "manage", "management", "internal", "intranet", "vpn", "sso", "auth",
    "login", "oauth", "jenkins", "ci", "cd", "deploy", "argocd", "gitlab",
    "git", "registry", "k8s", "kubernetes", "rancher", "grafana", "kibana",
    "vault", "consul", "db", "database", "mysql", "postgres", "mongo", "redis",
    "elastic", "ssh", "bastion", "jump", "rdp", "sftp", "ftp", "smb",
    "exchange", "owa", "webmail", "mail", "smtp", "storage", "backup", "s3",
    "minio", "scada", "plc", "nvr", "camera", "printer", "radius", "firewall",
    "mgmt", "api", "apis", "gw", "graphql", "rpc", "internal-api",
}
SENSITIVE = {
    "admin", "billing", "payments", "finance", "hr", "crm", "erp", "secrets",
    "vault", "db", "database", "mysql", "postgres", "mongo", "redis", "elastic",
    "backup", "backups", "gitlab", "git", "s3", "minio", "storage", "sso",
    "auth", "ldap", "ad", "adfs", "keycloak", "jenkins", "ci", "k8s", "scada",
}
INTERNAL = {
    "internal", "intranet", "extranet", "vpn", "sso", "bastion", "jump", "rdp",
    "ldap", "ad", "adfs", "jenkins", "argocd", "grafana", "kibana", "zabbix",
    "nagios", "vault", "consul", "hr", "erp", "crm", "mgmt", "firewall",
    "printer", "printers", "wifi", "radius", "wiki", "confluence", "jira",
}
NOISE = {
    "www", "cdn", "static", "assets", "img", "images", "css", "js", "fonts",
    "media", "docs", "status", "analytics", "tracker", "ns", "ns1", "ns2", "mx",
    "spf", "dkim", "dmarc", "autodiscover", "verification", "blog",
}
ENV_TOKENS = {
    "prod": "production", "prd": "production", "production": "production",
    "live": "production", "dev": "development", "devel": "development",
    "develop": "development", "development": "development", "stg": "staging",
    "stage": "staging", "staging": "staging", "test": "test", "tests": "test",
    "testing": "test", "qa": "qa", "uat": "uat", "preprod": "preprod",
    "sandbox": "sandbox", "demo": "demo", "lab": "lab", "beta": "beta",
    "alpha": "alpha", "tmp": "temporary", "temp": "temporary",
}
API_LABELS = {"api", "apis", "gw", "graphql", "rpc", "rest", "soap", "webhook"}
ADMIN_LABELS = {
    "admin", "administrator", "panel", "cpanel", "console", "manage",
    "management", "dashboard", "backoffice", "jenkins", "argocd", "grafana",
    "kibana", "phpmyadmin", "adminer", "portainer", "rancher", "vault",
}

STOP = re.compile(r"[^a-z0-9]+")


def jitter(host: str, salt: str) -> float:
    digest = hashlib.sha1(f"{host}|{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def labels_of(host: str) -> list[str]:
    return [l for l in STOP.split(host.lower()) if l]


def score_candidate(host: str, meta: dict) -> dict[str, float]:
    labels = labels_of(host)
    env = next((ENV_TOKENS[l] for l in labels if l in ENV_TOKENS), None)
    privileged = [l for l in labels if l in PRIVILEGED]
    sensitive = [l for l in labels if l in SENSITIVE]
    internal = [l for l in labels if l in INTERNAL]
    noise = [l for l in labels if l in NOISE]
    admin = [l for l in labels if l in ADMIN_LABELS]
    api = [l for l in labels if l in API_LABELS] or (
        ["api"] if "api" in host.lower() else []
    )

    clamp = lambda v: round(min(0.99, max(0.01, v)), 3)

    production = 0.85
    if env in {"development", "staging", "test", "qa", "uat", "preprod", "sandbox",
               "demo", "lab", "beta", "alpha", "temporary"}:
        production = 0.12
    elif env == "production":
        production = 0.97
    if noise:
        production -= 0.05

    sensitive_v = 0.25 + 0.22 * len(sensitive) + 0.08 * len(privileged)
    internal_v = 0.15 + 0.35 * len(internal) + 0.05 * len(privileged)
    staging_v = 0.85 if env in {"development", "staging", "test", "qa", "uat",
                                "preprod", "sandbox", "demo", "lab", "alpha",
                                "beta", "temporary"} else 0.06
    admin_v = 0.08 + 0.85 * (1 if admin else 0) + 0.05 * len(privileged)
    api_v = 0.05 + 0.9 * (1 if api else 0)

    # metadata, if any, moves things the way it would in the real world
    if meta.get("title"):
        title = str(meta["title"]).lower()
        if any(w in title for w in ("admin", "panel", "dashboard", "console",
                                    "login", "sign in")):
            admin_v += 0.3
            sensitive_v += 0.2
        if "api" in title:
            api_v += 0.2
    if meta.get("http_status") in (401, 403):
        sensitive_v += 0.15
        admin_v += 0.1
    if meta.get("technologies"):
        techs = {str(t).lower() for t in meta["technologies"]}
        if techs & {"jenkins", "gitlab", "grafana", "kibana", "rabbitmq",
                    "elasticsearch", "phpmyadmin", "wordpress"}:
            sensitive_v += 0.15
            admin_v += 0.1

    interesting = (
        0.35 * production
        + 0.3 * sensitive_v
        + 0.15 * admin_v
        + 0.1 * api_v
        + 0.1 * (1.0 - staging_v)
        - 0.15 * len(noise)
    )

    j = lambda salt: (jitter(host, salt) - 0.5) * 0.06
    return {
        "likely_production": clamp(production + j("p")),
        "likely_sensitive": clamp(sensitive_v + j("s")),
        "likely_internal": clamp(internal_v + j("i")),
        "likely_staging": clamp(staging_v + j("t")),
        "likely_admin": clamp(admin_v + j("a")),
        "likely_api": clamp(api_v + j("ap")),
        "interesting_for_security_research": clamp(interesting + j("in")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-typesafe/0.1"
    cfg: argparse.Namespace
    counter = 0
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # quiet by default
        if self.cfg.verbose:
            sys.stderr.write("mock: " + fmt % args + "\n")

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, {"models": [
                {"name": "jev-latest", "description": "mock", "release_date": "2026-09-16"},
                {"name": "jev-1.13.0", "description": "mock", "release_date": "2026-09-16"},
            ]})
            return
        self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        if not self.path.startswith("/v1/systemone"):
            self._send(404, {"detail": "Not Found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 20:
            self._send(401, {"detail": "Invalid API key"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"detail": "malformed JSON"})
            return

        if self.cfg.latency:
            time.sleep(self.cfg.latency)

        with Handler.lock:
            Handler.counter += 1
            index = Handler.counter

        if self.cfg.rate_limit_every and index % self.cfg.rate_limit_every == 0:
            self._send(429, {"detail": "rate limit exceeded"},
                       {"Retry-After": str(self.cfg.retry_after)})
            return
        if self.cfg.fail_every and index % self.cfg.fail_every == 0:
            self._send(503, {"detail": "upstream unavailable"})
            return

        questions = payload.get("questions") or {}
        if self.cfg.reject_over_questions and len(questions) > self.cfg.reject_over_questions:
            self._send(422, {"detail": [{
                "loc": ["body", "questions"],
                "msg": f"too many questions ({len(questions)}): "
                       f"limit {self.cfg.reject_over_questions}",
            }]})
            return

        state = payload.get("state") or {}
        candidates = state.get("candidates") if isinstance(state, dict) else None
        if not candidates:
            self._send(422, {"detail": [{"loc": ["body", "state"],
                                         "msg": "no candidates"}]})
            return

        scored = [
            score_candidate(str(item.get("hostname", "")),
                            {k: v for k, v in item.items()
                             if k in ("title", "http_status", "server", "ports",
                                      "technologies", "resolved_ips")})
            for item in candidates
        ]
        answers: dict = {}
        for qi, item in enumerate(candidates):
            host = str(item.get("hostname", ""))
            for key, question in questions.items():
                prefix = f"c{qi}::"
                if not key.startswith(prefix):
                    continue
                name = key[len(prefix):]
                if question.get("type") != "noul" or name not in scored[qi]:
                    continue
                answers[key] = {"type": "noul", "noul": scored[qi][name]}

        # batch-level Choice: turn the interest scores into a distribution
        interest = [s["interesting_for_security_research"] for s in scored]
        exp = [pow(max(v, 0.01), 4) for v in interest]
        total = sum(exp) or 1.0
        probabilities = {
            f"candidates[{i}]": round(v / total, 4) for i, v in enumerate(exp)
        }
        if "batch::top_pick" in questions:
            answers["batch::top_pick"] = {
                "type": "choice",
                "choice": max(probabilities, key=probabilities.get),
                "probabilities": probabilities,
                "confidence": round(max(probabilities.values()), 3),
            }
        top = max(interest) if interest else 0.0
        yield_score = 0.0 if top < 0.45 else (1.0 if top < 0.7 else 2.0)
        if "batch::research_yield" in questions:
            answers["batch::research_yield"] = {
                "type": "score",
                "score": yield_score,
                "legend": {"0": "Little of interest", "1": "Mixed",
                           "2": "Several leads"},
                "probabilities": {"0": round(1 - yield_score / 2, 3),
                                  "1": 0.0,
                                  "2": round(yield_score / 2, 3)},
                "confidence": 0.82,
            }

        self._send(200, {
            "model": "jev-1.13.0",
            "answers": answers,
            "usage": {
                "input_tokens": max(1, length // 4),
                "output_tokens": len(questions) * 3,
            },
        })


def serve(port: int = 8712, host: str = "127.0.0.1", **kwargs) -> ThreadingHTTPServer:
    defaults = dict(
        verbose=False, latency=0.0, rate_limit_every=0, retry_after=1,
        fail_every=0, reject_over_questions=0,
    )
    defaults.update(kwargs)
    Handler.cfg = argparse.Namespace(**defaults)
    Handler.counter = 0
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8712)
    parser.add_argument("--latency", type=float, default=0.0)
    parser.add_argument("--rate-limit-every", type=int, default=0)
    parser.add_argument("--retry-after", type=int, default=1)
    parser.add_argument("--fail-every", type=int, default=0)
    parser.add_argument("--reject-over-questions", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    httpd = serve(
        args.port, args.host, verbose=args.verbose, latency=args.latency,
        rate_limit_every=args.rate_limit_every, retry_after=args.retry_after,
        fail_every=args.fail_every,
        reject_over_questions=args.reject_over_questions,
    )
    print(f"mock TypeSafe API on http://{args.host}:{args.port} "
          f"(POST /v1/systemone)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nmock: bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

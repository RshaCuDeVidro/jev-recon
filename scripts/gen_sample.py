#!/usr/bin/env python3
"""Generate a synthetic subdomain list, for demos and load testing.

    python scripts/gen_sample.py 50000 > subdomains.txt

The mix is deliberately messy: duplicates, placeholders, malformed lines, IPs,
wildcards, URLs and ports all appear, the way they do in a real enumeration
dump. Seeded, so the same size always produces the same file.
"""

from __future__ import annotations

import random
import sys

PARENTS = [
    "example.com", "acme-corp.net", "northwind.io", "globex.com.br",
    "initech.dev", "umbrella.co", "stark-industries.com", "wayne-enterprises.net",
    "hooli.com", "piedpiper.io", "soylent.dev", "cyberdyne.org",
    "tyrell-corp.com", "weyland.co", "massive-dynamic.io", "veridian.net",
    "zorg-industries.com", "bluth-company.dev", "vandelay.io", "dundermifflin.com",
]
APEX_SERVICES = [
    "www", "api", "app", "admin", "portal", "dashboard", "mail", "smtp", "imap",
    "webmail", "exchange", "owa", "vpn", "sso", "auth", "login", "oauth",
    "id", "accounts", "billing", "payments", "checkout", "stripe", "shop",
    "store", "cdn", "static", "assets", "img", "images", "media", "video",
    "docs", "help", "support", "status", "blog", "careers", "jobs", "about",
    "internal", "intranet", "extranet", "partner", "partners", "vendor",
    "jenkins", "ci", "cd", "build", "deploy", "argocd", "gitlab", "git",
    "github", "bitbucket", "registry", "docker", "k8s", "kubernetes",
    "rancher", "grafana", "kibana", "prometheus", "zabbix", "nagios", "sentry",
    "db", "database", "mysql", "postgres", "pgsql", "mongo", "redis",
    "elastic", "clickhouse", "sql", "ssh", "bastion", "jump", "rdp", "sftp",
    "ftp", "smb", "nas", "storage", "backup", "backups", "s3", "minio",
    "scada", "plc", "hmi", "camera", "cameras", "nvr", "dvr", "printer",
    "printers", "radius", "wifi", "firewall", "router", "switch", "mgmt",
    "panel", "cpanel", "console", "manage", "management", "backoffice",
    "wiki", "confluence", "jira", "jitsi", "meet", "teams", "zoom", "chat",
    "erp", "crm", "hr", "finance", "reports", "metrics", "analytics",
    "tracker", "telemetry", "webhooks", "gateway", "gw", "graphql", "rpc",
    "mobile", "android", "ios", "beta", "alpha", "staging", "stage", "test",
    "qa", "uat", "dev", "develop", "sandbox", "demo", "lab", "preprod",
    "legacy", "old", "v1", "v2", "v3", "sandbox2", "lab2",
]
ENV_PREFIXES = ["dev", "devel", "test", "qa", "uat", "stg", "stage", "staging",
                "preprod", "sandbox", "demo", "lab", "beta", "alpha", "old",
                "legacy", "tmp"]
JUNK = [
    "foo", "bar", "baz", "asdf", "dummy", "sample", "placeholder", "aaa",
    "xxx", "todo", "none", "nope", "na", "null",
]
MALFORMED = [
    "not a hostname at all", "   ", "# comment line", ";;", "--",
    "-leadinghyphen.example.com", "trailinghyphen-.example.com",
    "* .example.com", "single-label", "1.2.3.4", "10.0.0.12",
    "2001:db8::1", "http://admin.example.com", "https://api.example.com/v1/users",
    "https://vpn.example.com:8443/login", "mail.example.com:25",
    "UPPER.CASE.example.com", "under_score.example.com", "portal.example.com.",
    "host.example.invalid", "thing.example.test", "localhost",
    "user@mail.example.com", "db01.example.com:3306",
]
REGIONS = ["us", "eu", "br", "us-east-1", "eu-west-1", "sa-east-1", "ap-south-1"]


def generate(total: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    lines: list[str] = []
    while len(lines) < total:
        roll = rng.random()
        parent = rng.choice(PARENTS)
        if roll < 0.10:
            lines.append(rng.choice(MALFORMED))
        elif roll < 0.16:
            lines.append(f"{rng.choice(JUNK)}.{parent}")
        elif roll < 0.30:
            # a duplicate of something already emitted
            lines.append(rng.choice(lines) if lines else f"www.{parent}")
        elif roll < 0.50:
            # env-prefixed variant
            lines.append(f"{rng.choice(ENV_PREFIXES)}.{rng.choice(APEX_SERVICES)}.{parent}")
        elif roll < 0.62 and lines:
            # deep name built on an existing shallow host
            base = rng.choice(lines)
            if (base.count(".") == 2 and " " not in base
                    and not base.startswith(("*", "-", "#", "h", "1", "2"))):
                lines.append(f"{rng.choice(APEX_SERVICES)}.{base}")
                continue
            lines.append(f"{rng.choice(APEX_SERVICES)}.{parent}")
        elif roll < 0.74:
            lines.append(f"{rng.choice(APEX_SERVICES)}{rng.randint(1, 30)}.{parent}")
        elif roll < 0.84:
            lines.append(f"{rng.choice(REGIONS)}.{rng.choice(APEX_SERVICES)}.{parent}")
        else:
            lines.append(f"{rng.choice(APEX_SERVICES)}.{parent}")
    return lines[:total]


def main() -> int:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    out = generate(total, seed)
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

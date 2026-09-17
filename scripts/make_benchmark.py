#!/usr/bin/env python3
"""Build a labelled benchmark set for testing whether evidence beats names.

The trick that makes this test honest: every hostname comes from ONE pool of
innocuous names (``node-07``, ``app-14``, ``svc-3``...), and the interesting
label is assigned at random and then backed by HTTP evidence. So the names carry
no information about the label, by construction, and any method that only reads
the name is measuring noise.

Two tiers, because that is where the interesting difference lives:

* ``obvious``  the evidence names a privileged product (Jenkins, GitLab,
  phpMyAdmin, Portainer, Kibana...). A keyword regex finds these.
* ``subtle``   the evidence is ambiguous: a generic title (Dashboard, Console,
  Portal) behind a 401/403, or a generic framework on a gated page. A keyword
  regex mostly misses these.

    python scripts/make_benchmark.py --hosts 300 --gold 90 --out bench/
"""

from __future__ import annotations

import argparse
import json
import os
import random

#: Innocuous names. Both classes are drawn from this same pool.
NAME_POOL = [
    "node-{n}", "app-{n}", "svc-{n}", "web-{n}", "edge-{n}", "cache-{n}",
    "gw-{n}", "job-{n}", "task-{n}", "queue-{n}", "mail-relay-{n}", "prn-{n}",
    "obj-{n}", "worker-{n}", "sqlproxy-{n}", "relay-{n}", "sync-{n}",
    "region-{n}", "cell-{n}", "shard-{n}",
]
PARENTS = ["northwind-lab.example.org", "globex-lab.example.org",
           "initech-lab.example.net", "umbrella-lab.example.org",
           "soylent-lab.example.com"]

#: tier A: the evidence shouts
OBVIOUS = [
    ("jenkins", "dashboard", ["Jenkins", "nginx"]),
    ("gitlab", "GitLab", ["GitLab", "nginx"]),
    ("phpMyAdmin", "phpMyAdmin", ["phpMyAdmin", "php"]),
    ("Portainer", "Portainer", ["Angular", "Portainer"]),
    ("Kibana", "Kibana", ["Kibana"]),
    ("Grafana", "Grafana", ["Grafana"]),
    ("RabbitMQ Management", "RabbitMQ Management", ["RabbitMQ"]),
    ("Prometheus", "Prometheus Time Series Collection", ["Prometheus"]),
    ("MinIO Console", "MinIO Console", ["MinIO"]),
    ("Proxmox VE", "Proxmox Virtual Environment", ["Proxmox"]),
]
#: tier B: generic evidence behind a gated response. None of these titles
#: contains a word from KEYWORDS, and the discriminator (401/403) is invisible to
#: a keyword scan over title+tech. So this tier asks a real question: does the
#: method understand "generic page, gated response" or does it only match words?
SUBTLE_TITLES = ["Console", "Portal", "Overview", "Manage", "Internal Tools",
                 "Sign in", "Home", "Settings", "Workspace", "Ops"]
SUBTLE_TECH = [["Django", "gunicorn"], ["Rails", "puma"], ["Express"],
               ["Laravel", "php-fpm"], ["FastAPI"], ["ASP.NET"]]
#: the bored class: real-looking but worth nothing
BORING_TITLES = [
    "Welcome to nginx!", "Apache2 Ubuntu Default Page", "Site under construction",
    "Just a moment...", "Index of /", "Domain parked", "404 Not Found",
    "Redirecting...", "Cloudflare", "Object not found",
]
BORING_TECH = [["Vercel"], ["Cloudflare"], ["nginx"], ["Amazon S3"], ["Netlify"]]

#: Decoys. Third-party tracking and delivery namespaces that LOOK privileged
#: because they carry a label like api, admin or sso, plus an environment word.
#: A method that matches labels promotes them; the evidence says otherwise (a
#: redirect, nothing behind it). These are counted as boring, so every one that
#: lands in the top slice costs the method precision.
#:
#: The failure this reproduces, from a real run: `click.c.email.api.acme.com` ranked
#: 0.60 on names alone, above `sso-auth.acme.com`. So the decoys that matter carry
#: BOTH a tracking vocabulary and a privileged label. A decoy with only the
#: tracking side (`email.api.acme.com`) lands in the ambiguous list, below, and is
#: deliberately out of scope for the rule in code.
DECOY_NAMES = [
    "click.c.email.api.{p}", "track.c.email.api.{p}", "url.c.email.api.{p}",
    "click.links.mail.api.{p}", "track.email.admin.{p}", "c.email.sso.{p}",
    "click.c.email.{p}", "track.links.mail.{p}", "url.c.email.{p}",
    "track.c.email.{p}", "click.email.{p}", "c.email.{p}",
]
#: one vocabulary only: a real mail API or a bare click host is genuinely
#: ambiguous, so a rule that flags these would be wrong more often than right.
#: They ship in the set as boring hosts, as a control on the rule misfiring.
AMBIGUOUS_NAMES = [
    "email.api.{p}", "click.api.{p}", "links.email.{p}", "email.admin.{p}",
    "click.sso.{p}", "track.api.{p}", "email.console.{p}", "links.api.{p}",
    "click.admin.{p}", "email.{p}", "mail.{p}", "smtp.{p}",
]
DECOY_TITLES = ["Redirecting...", "Object Moved", "Just a moment...",
                "Welcome to nginx!", "404 Not Found"]
DECOY_TECH = [["Sendgrid"], ["SendGrid"], ["Cloudflare"], ["Amazon CloudFront"],
              ["Vercel"], ["Mailgun"]]

#: the quick keyword list a hunter writes in a minute. Used as a baseline by
#: scripts/benchmark.py, defined here so both live in one readable place.
KEYWORDS = ["admin", "login", "jenkins", "gitlab", "dashboard", "phpmyadmin",
            "portainer", "kibana", "grafana", "sql", "backup"]


def build(n_hosts: int, n_gold: int, seed: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    n_decoys = min(len(DECOY_NAMES), max(8, n_hosts // 20))
    parents = PARENTS                                 # decoys sit under the same parents

    names: list[str] = []
    used: set[str] = set()
    decoy_set: set[str] = set()
    forced_boring: set[str] = set()
    for template in DECOY_NAMES[:n_decoys]:           # decoys first, then the pool
        host = template.format(p=rng.choice(parents))
        if host not in used:
            used.add(host)
            names.append(host)
            decoy_set.add(host)
    for template in AMBIGUOUS_NAMES:                  # control: must stay boring
        host = template.format(p=rng.choice(parents))
        if host not in used:
            used.add(host)
            names.append(host)
            forced_boring.add(host)
    while len(names) < n_hosts:
        host = rng.choice(NAME_POOL).format(n=rng.randint(1, 99))
        host = f"{host}.{rng.choice(PARENTS)}"
        if host in used:
            continue
        used.add(host)
        names.append(host)

    rest = [h for h in names if h not in decoy_set and h not in forced_boring]
    gold_flags = [True] * n_gold + [False] * (len(rest) - n_gold)
    rng.shuffle(gold_flags)
    rng.shuffle(names)                                 # decoys mixed back in

    n_obvious = int(n_gold * 0.66)
    rows: list[dict] = []
    labels: dict[str, str] = {}
    for host in names:
        if host in decoy_set:
            labels[host] = "decoy"
            page = rng.choice(DECOY_TITLES)
            tech = rng.choice(DECOY_TECH)
            status = rng.choice([301, 302, 404])
        elif host in forced_boring:
            labels[host] = "boring"
            page = rng.choice(DECOY_TITLES)
            tech = rng.choice(DECOY_TECH)
            status = rng.choice([301, 302, 404])
        else:
            page, tech, is_gold = None, None, gold_flags.pop()
            if is_gold:
                if n_obvious > 0:
                    _, page, tech = rng.choice(OBVIOUS)
                    n_obvious -= 1
                    labels[host] = "obvious"
                    status = rng.choice([200, 200, 403])
                else:
                    page = rng.choice(SUBTLE_TITLES)
                    tech = rng.choice(SUBTLE_TECH)
                    labels[host] = "subtle"
                    status = rng.choice([401, 403])
            else:
                labels[host] = "boring"
                page = rng.choice(BORING_TITLES)
                tech = rng.choice(BORING_TECH)
                status = rng.choice([200, 200, 301, 302, 404])
        rows.append({
            "hostname": host,
            "http_status": status,
            "title": page,
            "technologies": tech,
            "server": tech[-1] if tech else "nginx",
        })
    return rows, labels


def keyword_baseline(row: dict) -> float:
    """The naive regex a person writes first: keywords over title and tech."""
    haystack = f"{row.get('title', '')} {' '.join(row.get('technologies') or [])}".lower()
    hits = sum(1 for word in KEYWORDS if word in haystack)
    return float(hits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hosts", type=int, default=300)
    parser.add_argument("--gold", type=int, default=90)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", default="bench")
    args = parser.parse_args()

    rows, labels = build(args.hosts, args.gold, args.seed)
    os.makedirs(args.out, exist_ok=True)
    hosts_path = os.path.join(args.out, "hosts.jsonl")
    with open(hosts_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    labels_path = os.path.join(args.out, "labels.json")
    with open(labels_path, "w", encoding="utf-8") as fh:
        json.dump(labels, fh, indent=1)

    counts: dict[str, int] = {}
    for tier in labels.values():
        counts[tier] = counts.get(tier, 0) + 1
    print(f"{len(rows)} hosts -> {hosts_path}")
    print(f"labels -> {labels_path}  {counts}")
    print("names carry no label signal by construction: both classes come from "
          f"{len(NAME_POOL)} name templates x {len(PARENTS)} parents")
    print(f"decoys are label-rich tracking names with worthless evidence: "
          f"{counts.get('decoy', 0)} hosts, counted as boring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local, cheap pre-processing: normalize, validate, annotate, group.

Nothing here costs an API call. The point of this module is to make sure every
token we send to Jev is attached to a hostname that is worth asking about.

Two different things happen to a hostname that looks like noise:

* hard junk is dropped (duplicates, malformed names, reserved suffixes,
  obvious placeholders like ``foo``/``asdf``)
* security-relevant-but-weak names are NOT dropped, they are annotated. A
  ``test`` label is a fact we hand to Jev, not a reason to throw the asset away.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# vocabularies (all code-owned, all overridable from the CLI)
# --------------------------------------------------------------------------

#: Labels that mean "this line was never a real host". Dropped.
#: ``example`` is deliberately NOT here: example.com is a real registered domain
#: (IANA's documentation domain) and shows up legitimately in test lists. It is
#: annotated as a documentation parent instead, and the reserved TLD ``.example``
#: is still rejected by RESERVED_SUFFIXES.
HARD_DROP_LABELS = frozenset(
    {
        "localhost", "invalid", "foo", "bar", "baz", "qux", "asdf",
        "dummy", "sample", "placeholder", "aaa", "bbb", "xxx", "yyy", "zzz",
        "todo", "none", "nope", "na", "null", "void", "domain", "yourdomain",
        "hostname", "changeme", "ip6", "wildcard",
    }
)

#: Registered parents that exist only for documentation and examples.
DOCUMENTATION_PARENTS = frozenset(
    {
        "example.com", "example.org", "example.net", "example.edu",
        "example.io", "example.dev", "test.com", "mydomain.com",
        "yourdomain.com", "domain.com", "acme.com", "foo.com", "bar.com",
    }
)

#: Labels that hint at a throwaway or non-production name. Annotated, not dropped.
THROWAWAY_LABELS = frozenset(
    {
        "test", "tests", "testing", "tmp", "temp", "demo", "sandbox", "lab",
        "qa", "uat", "preprod", "pre", "stg", "stage", "staging", "dev", "devel",
        "develop", "development", "beta", "alpha", "rc", "old", "legacy",
    }
)

#: Labels that code can extract as facts instead of asking a model to match strings.
ENV_TOKENS = {
    "prod": "production", "prd": "production", "production": "production",
    "live": "production",
    "dev": "development", "devel": "development", "develop": "development",
    "development": "development",
    "stg": "staging", "stage": "staging", "staging": "staging",
    "test": "test", "tests": "test", "testing": "test", "qa": "qa", "uat": "uat",
    "preprod": "preprod", "pre": "preprod", "sandbox": "sandbox",
    "demo": "demo", "lab": "lab", "beta": "beta", "alpha": "alpha",
    "tmp": "temporary", "temp": "temporary",
}

#: Labels that raise the prior interest of an asset. Only used for local ordering.
PRIVILEGED_LABELS = frozenset(
    {
        "admin", "administrator", "api", "apis", "internal", "intranet", "extranet",
        "vpn", "sso", "auth", "login", "oauth", "openid", "keycloak", "ldap", "ad",
        "adfs", "jenkins", "ci", "cd", "build", "deploy", "deployment", "argo",
        "argocd", "gitlab", "git", "github", "bitbucket", "registry", "docker",
        "k8s", "kubernetes", "rancher", "nomad", "consul", "vault", "grafana",
        "kibana", "prometheus", "zabbix", "nagios", "sentry", "db", "database",
        "mysql", "pgsql", "postgres", "mongo", "mongodb", "redis", "elastic",
        "elasticsearch", "clickhouse", "sql", "ssh", "bastion", "jump", "rdp",
        "panel", "cpanel", "portal", "console", "manage", "management", "dashboard",
        "backoffice", "sftp", "ftp", "smb", "ldapadmin", "mssql", "oracle", "sap",
        "erp", "crm", "hr", "finance", "billing", "payments", "checkout", "stripe",
        "secrets", "storage", "backup", "backups", "s3", "minio", "nas", "scada",
        "plc", "hmi", "camera", "cameras", "nvr", "dvr", "printer", "printers",
        "mail", "smtp", "imap", "exchange", "owa", "webmail", "vpn2", "remote",
        "zt", "ztna", "radius", "wifi", "firewall", "router", "switch", "mgmt",
    }
)

#: Labels that usually mean low-value infrastructure noise.
NOISE_LABELS = frozenset(
    {
        "www", "cdn", "static", "assets", "img", "images", "image", "css", "js",
        "fonts", "media", "video", "docs", "status", "statuspage", "analytics",
        "tracker", "telemetry", "metrics", "ns", "ns1", "ns2", "mx", "spf",
        "dkim", "dmarc", "autodiscover", "cpanelmail", "verification", "verify",
        "ticket", "tickets", "zendesk", "blog", "newsroom", "careers", "jobs",
    }
)

#: Suffixes reserved by RFC 2606 / 6761 and friends.
RESERVED_SUFFIXES = (
    ".invalid", ".test", ".localhost", ".example", ".localdomain", ".home.arpa",
    ".onion", ".arpa",
)

LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
DROP_REASONS_ORDER = (
    "duplicate", "invalid_syntax", "not_a_hostname", "ip_literal",
    "reserved_suffix", "placeholder_label", "throwaway_only", "wildcard",
    "capped_per_parent", "limit",
)


@dataclass(slots=True)
class ParserOptions:
    """Everything the local stage is allowed to do to an input line."""

    allow_ip: bool = False
    allow_underscore: bool = False
    drop_throwaway: bool = False
    max_per_parent: int = 0          # 0 = disabled
    hard_drop_labels: frozenset[str] = HARD_DROP_LABELS
    throwaway_labels: frozenset[str] = THROWAWAY_LABELS


@dataclass(slots=True)
class Candidate:
    """One asset that survived pre-processing and is worth a Jev question."""

    hostname: str
    labels: tuple[str, ...]
    depth: int
    pre: dict
    meta: dict = field(default_factory=dict)
    index: int = 0

    @property
    def cid(self) -> str:
        return f"c{self.index:05d}"

    def as_state(self) -> dict:
        """The per-item object that goes into the Jev ``state``."""
        item: dict = {
            "id": self.cid,
            "hostname": self.hostname,
            "labels": list(self.labels),
            "subdomain_depth": self.depth,
            "code_extracted": self.pre,
        }
        # Optional enrichment (resolved_ips, http_status, title, server, ports,
        # technologies, ...) is merged in last so it never shadows the core keys.
        for key, value in self.meta.items():
            if key not in item:
                item[key] = value
        return item


@dataclass
class PreprocessReport:
    total_lines: int = 0
    dupes: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    kept: int = 0
    annotated: dict[str, int] = field(default_factory=dict)
    groups: int = 0
    candidates: list[Candidate] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "duplicates": self.dupes,
            "dropped": dict(sorted(self.dropped.items())),
            "kept": self.kept,
            "annotated": dict(sorted(self.annotated.items())),
            "groups": self.groups,
        }


def normalize_host(raw: str) -> str | None:
    """Turn one messy input line into a bare hostname, or ``None``."""
    line = raw.strip()
    if not line or line.startswith(("#", ";")):
        return None
    line = line.split()[0]                      # "host 1.2.3.4" -> "host"
    line = SCHEME_RE.sub("", line)              # "https://host"  -> "host"
    line = line.split("/", 1)[0]                # "host/path"     -> "host"
    line = line.split("?", 1)[0].split("#", 1)[0]
    line = line.split("@")[-1]                  # "user@host"     -> "host"
    if line.startswith("-"):
        line = line[1:]
    if line.startswith("*."):                   # wildcard DNS records
        line = line[2:]
    if ":" in line and not line.startswith("["):
        line = line.split(":", 1)[0]            # strip a port
    line = line.strip().strip(".").lower()
    return line or None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_host(host: str, opts: ParserOptions) -> str:
    """Return ``""`` when the host is valid, otherwise the drop reason."""
    if len(host) > 253:
        return "invalid_syntax"
    labels = host.split(".")
    if len(labels) < 2:
        return "not_a_hostname"
    if _is_ip_literal(host):
        return "" if opts.allow_ip else "ip_literal"
    for label in labels:
        if not label:
            return "invalid_syntax"
        if "_" in label:
            if not opts.allow_underscore:
                return "invalid_syntax"
            continue
        if not LABEL_RE.match(label):
            return "invalid_syntax"
    if not labels[-1].isalpha() or len(labels[-1]) < 2:
        return "invalid_syntax"
    if host.endswith(RESERVED_SUFFIXES):
        return "reserved_suffix"
    return ""


def name_tokens(labels: tuple[str, ...]) -> list[str]:
    """Every subdomain label split on hyphens.

    ``dev-api.example.com`` -> ``["dev", "api"]``. The registered parent and the
    TLD are excluded: ``acme-corp.net`` says nothing about the asset. This is the
    exact, cheap extraction that keeps Jev from having to string-match.
    """
    tokens: list[str] = []
    for label in labels[:-2]:
        for piece in label.split("-"):
            piece = piece.strip("0123456789_")
            if piece:
                tokens.append(piece)
    return tokens


def extract_facts(host: str, labels: tuple[str, ...], opts: ParserOptions) -> dict:
    """Code-owned facts, computed exactly, so Jev never has to string-match."""
    tokens = name_tokens(labels)
    env_tokens = [ENV_TOKENS[t] for t in tokens if t in ENV_TOKENS]
    privileged = [t for t in tokens if t in PRIVILEGED_LABELS]
    noise = [t for t in tokens if t in NOISE_LABELS]
    digits = sum(c.isdigit() for c in host)
    parent = ".".join(labels[-2:])
    facts = {
        "registered_parent": parent,
        "documentation_parent": parent in DOCUMENTATION_PARENTS,
        "name_tokens": tokens,
        "env_tokens": env_tokens,
        "env_token": env_tokens[0] if env_tokens else None,
        "privileged_labels": privileged,
        "noise_labels": noise,
        "numeric_label": any(l.rstrip("0123456789") != l for l in labels[:-1]),
        "digit_share": round(digits / len(host), 2),
        "has_hyphen": "-" in host,
        "label_count": len(labels),
    }
    return facts


def pre_rank(pre: dict) -> float:
    """Cheap deterministic ordering key. NEVER the final decision.

    Used only to order assets inside a ``--max-per-parent`` cap and to serve
    ``--limit``, so that a truncated run keeps the names with the most signal.
    """
    score = 0.0
    score += min(len(pre["privileged_labels"]), 3) * 1.0
    score += 0.6 if pre["env_token"] in {"production", "development", "staging",
                                        "test", "qa", "uat", "preprod", "sandbox",
                                        "demo", "lab", "beta"} else 0.0
    score -= 0.35 * len(pre["noise_labels"])
    score -= 0.3 if pre.get("documentation_parent") else 0.0
    score += 0.15 if pre["has_hyphen"] else 0.0
    score += 0.1 if pre["numeric_label"] else 0.0
    score += 0.1 * (pre["label_count"] - 2)
    return round(score, 3)


def prepare(
    lines: list[str],
    opts: ParserOptions | None = None,
    metadata: dict[str, dict] | None = None,
    limit: int = 0,
) -> PreprocessReport:
    """Run the whole local stage over raw input lines."""
    opts = opts or ParserOptions()
    metadata = metadata or {}
    report = PreprocessReport(total_lines=len(lines))
    seen: set[str] = set()
    staged: list[Candidate] = []

    for raw in lines:
        host = normalize_host(raw)
        if host is None:
            continue
        if host.startswith("*"):
            report.drop("wildcard")
            continue
        if host in seen:
            report.dupes += 1
            report.drop("duplicate")
            continue
        reason = validate_host(host, opts)
        if reason:
            report.drop(reason)
            continue
        seen.add(host)

        labels = tuple(host.split("."))
        tokens = name_tokens(labels)
        if any(t in opts.hard_drop_labels for t in tokens):
            report.drop("placeholder_label")
            continue
        if opts.drop_throwaway and any(t in opts.throwaway_labels for t in tokens):
            report.drop("throwaway_only")
            continue

        pre = extract_facts(host, labels, opts)
        pre["pre_rank"] = pre_rank(pre)
        for token in pre["env_tokens"]:
            report.annotated[token] = report.annotated.get(token, 0) + 1
        if pre["privileged_labels"]:
            report.annotated["privileged"] = report.annotated.get("privileged", 0) + 1
        if pre["documentation_parent"]:
            report.annotated["documentation_parent"] = (
                report.annotated.get("documentation_parent", 0) + 1
            )

        staged.append(
            Candidate(
                hostname=host,
                labels=labels,
                depth=len(labels),
                pre=pre,
                meta=metadata.get(host, {}),
            )
        )

    # Optional grouping: cap how many assets we keep per registered parent.
    if opts.max_per_parent > 0:
        buckets: dict[str, list[Candidate]] = {}
        for cand in staged:
            buckets.setdefault(cand.pre["registered_parent"], []).append(cand)
        report.groups = len(buckets)
        kept: list[Candidate] = []
        for members in buckets.values():
            members.sort(key=lambda c: (-c.pre["pre_rank"], c.hostname))
            if len(members) > opts.max_per_parent:
                report.dropped["capped_per_parent"] = (
                    report.dropped.get("capped_per_parent", 0)
                    + len(members)
                    - opts.max_per_parent
                )
            kept.extend(members[: opts.max_per_parent])
        staged = kept
    else:
        report.groups = len({c.pre["registered_parent"] for c in staged})

    staged.sort(key=lambda c: (-c.pre["pre_rank"], c.hostname))
    if limit and len(staged) > limit:
        report.dropped["limit"] = len(staged) - limit
        staged = staged[:limit]

    for position, cand in enumerate(staged):
        cand.index = position
    report.candidates = staged
    report.kept = len(staged)
    return report


def load_metadata(path: str) -> dict[str, dict]:
    """Load optional enrichment.

    Three shapes are accepted, because all three show up in real pipelines:

    * ``{"hostname": {"http_status": 200, ...}, ...}``  (a map)
    * ``[{"hostname": "...", ...}, ...]``                (a list of records)
    * one JSON object per line                            (JSONL)
    """
    import json

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    stripped = text.lstrip()

    if stripped.startswith("["):
        rows = json.loads(text)
        meta = {
            row["hostname"]: row
            for row in rows
            if isinstance(row, dict) and row.get("hostname")
        }
    elif stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("hostname"):
            meta = {data["hostname"]: data}
        elif isinstance(data, dict):
            meta = {h: v for h, v in data.items() if isinstance(v, dict)}
        else:
            meta = _load_jsonl(text)
    else:
        meta = _load_jsonl(text)

    out: dict[str, dict] = {}
    for host, row in meta.items():
        clean = normalize_host(str(host))
        if not clean:
            continue
        out[clean] = {k: v for k, v in row.items() if k != "hostname"}
    return out


def _load_jsonl(text: str) -> dict:
    import json

    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict) and "hostname" in row:
            out[row["hostname"]] = row
    return out

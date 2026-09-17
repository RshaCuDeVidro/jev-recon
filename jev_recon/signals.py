"""The tiny semantic questions we ask Jev, and how a batch becomes one request.

Design rules taken from the TypeSafe docs:

* one judgment per question, phrased literally, no hidden multi-factor weighing
* every question names the part of ``state`` it is about, by path
* criteria are the same statement as the instruction, spelled out on both sides
* many questions travel in ONE request; they are evaluated in parallel
* nothing that code can compute exactly is asked of the model
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-candidate questions: one Noul per signal per candidate.
# ---------------------------------------------------------------------------

#: signal name -> (question, what counts as yes, what counts as no)
SIGNALS: dict[str, tuple[str, str, str]] = {
    "likely_production": (
        "Is `{path}` likely to be a production-facing system that serves real "
        "users, real customers, or live business traffic?",
        "The name and the metadata point at a live, customer-facing system: a "
        "production or region token, a bare api or app or www prefix, a real "
        "certificate and HTTP response, or traffic-serving technology.",
        "The name or the metadata points at a non-production environment "
        "(dev, test, qa, uat, staging, sandbox, demo, lab, preprod) or at an "
        "asset that plainly does not serve live traffic.",
    ),
    "likely_sensitive": (
        "Is `{path}` likely to carry or guard data or functionality that a "
        "company would classify as sensitive?",
        "Customer personal data, payment or billing data, credentials, secrets, "
        "session tokens, source code, internal documents, employee data, or "
        "control over production infrastructure. Count the metadata too: a "
        "technology naming a database, a queue, a secret store, a repository, "
        "or an object storage (Postgres, MySQL, Mongo, Redis, RabbitMQ, Vault, "
        "GitLab, MinIO, Elasticsearch), or an HTTP 401 or 403 that says "
        "something behind the wall requires a credential.",
        "Only public content, marketing pages, static assets, or functionality "
        "whose exposure would not by itself be a confidentiality problem.",
    ),
    "likely_internal": (
        "Is `{path}` likely used by employees, contractors, or internal services "
        "rather than by the general public?",
        "Internal tooling, intranet, corporate SSO, VPN, office networking, "
        "employee self-service, partner-only portals, or services that speak to "
        "other backend services rather than to the public internet.",
        "A public website, public API, or public-facing product surface that any "
        "anonymous visitor is expected to use.",
    ),
    "likely_staging": (
        "Is `{path}` part of a non-production environment?",
        "A development, test, qa, uat, staging, preprod, sandbox, demo, lab, or "
        "temporary environment, including names that carry those tokens or a "
        "copy of production naming with one of them substituted.",
        "A live production system, or an asset whose name and metadata give no "
        "indication of a pre-release or throwaway environment.",
    ),
    "likely_admin": (
        "Does `{path}` look like it exposes an administrative or privileged "
        "management interface?",
        "An admin console, control panel, dashboard, back office, CI/CD or "
        "deployment control, container or orchestration control, database or "
        "queue administration, or infrastructure management surface. Count the "
        "metadata too: a title or technology naming such a product (Jenkins, "
        "GitLab, Grafana, Kibana, Portainer, phpMyAdmin, MinIO, Proxmox, "
        "RabbitMQ, Prometheus), a title that is a generic management word "
        "(Console, Dashboard, Portal, Manage, Overview) on a page that answers "
        "HTTP 401 or 403, or a login form in front of such a service.",
        "A read-only public page, a product feature for end users, or a plain "
        "content or asset host with no management function.",
    ),
    "likely_api": (
        "Does `{path}` look like it exposes a machine-facing API rather than a "
        "human-facing page?",
        "REST, GraphQL, gRPC, SOAP, or webhook endpoints, API gateways, service "
        "meshes, or host names built from api, apis, gw, graphql, rpc, or a "
        "service-to-service naming scheme.",
        "A browser-facing page, a static file host, a mail or DNS record, or an "
        "asset whose metadata describes only HTML content.",
    ),
    # Off by default: costs one more question per asset. Add it with
    # --signals production,sensitive,admin,api,devops,interesting and weight it.
    "likely_devops": (
        "Does `{path}` look like build, deploy, or release infrastructure?",
        "CI/CD runners, build and artifact services, container registries, "
        "deployment controllers, configuration or secrets management used by "
        "pipelines, or a host name built from jenkins, ci, cd, build, deploy, "
        "argocd, registry, drone, tekton, or similar.",
        "An application, a data service, a human-facing page, or an asset with "
        "no pipeline role implied by its name or metadata.",
    ),
    "interesting_for_security_research": (
        "Based on its name and metadata alone, is `{path}` an asset worth "
        "spending expensive manual security analysis on?",
        "A researcher reading only this name and metadata would put it on the "
        "short list to probe by hand, because it looks reachable, privileged, "
        "data-adjacent, or unmaintained.",
        "A researcher reading only this name and metadata would skip it: it "
        "looks like infrastructure noise, a duplicated name, or a surface with "
        "nothing to find.",
    ),
}

DEFAULT_SIGNAL_ORDER = (
    "likely_production",
    "likely_sensitive",
    "likely_internal",
    "likely_staging",
    "likely_admin",
    "likely_api",
    "interesting_for_security_research",
)

NOUL_ANSWER_KEYS = ("noul",)

# ---------------------------------------------------------------------------
# Per-batch questions: one request carries a whole batch.
# ---------------------------------------------------------------------------

TOP_PICK_INSTRUCTIONS = (
    "Exactly one candidate in `candidates` is the best first target for "
    "manual, expensive security analysis. Which one? Judge from the names and "
    "metadata in `candidates` only. Answer with one candidate id, such as "
    "`candidates[3]`."
)

RESEARCH_YIELD_INSTRUCTIONS = (
    "How much does this batch of `candidates` contain at least one asset that "
    "deserves expensive manual security analysis?"
)

RESEARCH_YIELD_LEVELS = [
    "Little of interest: mostly noise, placeholders, duplicated names, or "
    "assets with no reachable or privileged surface.",
    "Mixed: one or two plausible leads mixed in with routine names.",
    "Several plausible leads: multiple candidates a researcher would want to "
    "probe by hand.",
]


def candidate_questions(index: int, signal_names: tuple[str, ...]) -> dict:
    """The Noul questions for one candidate, keyed ``c{i}::{signal}``."""
    path = f"candidates[{index}]"
    questions = {}
    for name in signal_names:
        question, yes, no = SIGNALS[name]
        questions[f"c{index}::{name}"] = {
            "type": "noul",
            "instructions": question.replace("{path}", path),
            "criteria": {"true": yes, "false": no},
        }
    return questions


def batch_questions(hostnames: list[str], signal_names: tuple[str, ...]) -> dict:
    """The full question map for one batch: per-candidate Nouls plus two batch gates."""
    questions: dict = {}
    for index in range(len(hostnames)):
        questions.update(candidate_questions(index, signal_names))
    # A Choice over the batch: relative pressure, useful as a tie-breaker.
    # Its probabilities always sum to 1, so it is never read as an absolute signal.
    questions["batch::top_pick"] = {
        "type": "choice",
        "instructions": TOP_PICK_INSTRUCTIONS,
        "criteria": {
            f"candidates[{i}]": host for i, host in enumerate(hostnames)
        },
    }
    # A Score for the batch as a whole: ordered levels, so it can gate the run.
    questions["batch::research_yield"] = {
        "type": "score",
        "instructions": RESEARCH_YIELD_INSTRUCTIONS,
        "criteria": list(RESEARCH_YIELD_LEVELS),
    }
    return questions


def build_request(
    candidates: list, model: str, signal_names: tuple[str, ...] = DEFAULT_SIGNAL_ORDER
) -> dict:
    """One complete ``POST /v1/systemone`` body for a batch of candidates."""
    return {
        "state": {"task": _TASK, "candidates": [c.as_state() for c in candidates]},
        "model": model,
        "questions": batch_questions(
            [c.hostname for c in candidates], signal_names
        ),
    }


_TASK = (
    "Each entry in `candidates` is a DNS name that a security team is triaging. "
    "The names are unrelated to each other; every question is about exactly one "
    "candidate, named in the question by its index. Fields under "
    "`code_extracted` were computed from the name by deterministic code, not by "
    "a model: `env_token` was matched against a fixed vocabulary, "
    "`privileged_labels` and `noise_labels` are lists of known tokens, "
    "`registered_parent` is the last two labels. Treat them as facts about the "
    "name."
)

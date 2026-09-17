# jev-recon

> Português: [README-pt.md](README-pt.md). Both files carry the same content.

Subdomain triage for security research, using **Jev** (TypeSafe AI) as a
probabilistic decision layer, and plain Python deciding the ranking.

The point is not to ask an LLM to "tell me which subdomain is interesting". The
point is to ask many small independent questions about each asset, turn the
answers into numbers, and compose the priority with arithmetic you control.

```
Traditional LLM:  "tell me which subdomain is interesting"     (text, unstable, expensive to iterate)
Jev:              "answer 7 yes/no questions about this asset" (one probability per question)
Python:           priority = weighted sum + sort                (yours, auditable, versioned)
```

---

## Layout

```
jev-recon/
├── jev_recon/
│   ├── cli.py            CLI, orchestration, terminal output
│   ├── config.py         .env, API key, base URL, model
│   ├── cache.py          response cache keyed by request hash (re-ranking is free)
│   ├── preprocess.py     cheap local filters, code-extracted facts, grouping
│   ├── signals.py        the questions (Choice / Score / Noul) and the request body
│   ├── jev.py            async HTTP client: batching, concurrency, retries
│   └── rank.py           weights, priority, per-service grouping, reasons, sorting
├── scripts/
│   ├── gen_sample.py             generates a large synthetic list for demos and load
│   ├── make_benchmark.py         labelled sets for the benchmark
│   ├── benchmark.py              Jev vs heuristics vs random (see BENCHMARK.md)
│   └── mock_typesafe_server.py   compatible fake API, for demos and tests without a key
├── tests/                62 tests (unittest, no extra dependencies)
├── examples/             input, output and logs from real runs
├── requirements.txt      httpx
├── pyproject.toml
├── BENCHMARK.md
├── README-pt.md          mesmo conteudo, em portugues
└── .env.example
```

Runtime dependency: **httpx**. No framework, no database, no frontend.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # or: .venv/bin/pip install -e .
cp .env.example .env                             # then fill in the key
```

`pip install -e .` puts the `jev-recon` console script inside `.venv/bin/`,
which is not on your PATH, so a bare `jev-recon` will not be found. Three ways
out, in order of convenience:

```bash
# 1. a launcher in ~/.local/bin (this is what is already set up on this machine)
printf '#!/bin/sh\nexec %s/.venv/bin/python -m jev_recon "$@"\n' "$PWD" > ~/.local/bin/jev-recon
chmod +x ~/.local/bin/jev-recon

# 2. activate the venv each session
source .venv/bin/activate

# 3. install nothing at all
.venv/bin/python -m jev_recon hosts.txt
```

`.env`:

```ini
TYPESAFE_API_KEY=ts_...
# optional
TYPESAFE_BASE_URL=https://api.typesafe.ai
TYPESAFE_DEFAULT_MODEL=jev-latest
```

Key at https://console.typesafe.ai/settings/keys

The `.env` file is looked up in this order: `.env` in the current directory,
`.env` in the project root, `~/.config/jev-recon/.env`. So you can call
`jev-recon` from inside any engagement folder and the key is still found. If you
name an explicit file (`--env-file x.env`), only that file is used, with no
fallback.

## With subfinder, httpx and nuclei

The main use case. `jev-recon` reads stdin when you pass `-` (which is already
the default), so it drops straight into the middle of a pipeline.

```bash
# 1. enumerate
subfinder -d target.com -silent | sort -u > hosts.txt

# 2. enrich (optional, but it changes the signal a lot)
httpx -silent -json -l hosts.txt -o probe.json \
      -status-code -title -tech-detect -web-server -ports 443,80,8080,8443

# 3. check the plan and the cost before spending anything (nothing is sent)
jev-recon hosts.txt --meta probe.json --dry-run

# 4. prioritize
jev-recon hosts.txt --meta probe.json --cache jev-cache.json \
    --all-output all.json --threshold 0.55 --output interesting.json
```

If you skip step 2, drop `--meta` from steps 3 and 4: `probe.json` only exists
after httpx runs. A `--meta probe.json` pointing at a file that is not there gets
an explicit message with the httpx command that creates it, and nothing is sent
to the API.

Same thing with no intermediate file, all through pipes:

```bash
subfinder -d target.com -silent | sort -u | jev-recon - --meta probe.json \
    --cache jev-cache.json --all-output all.json --output interesting.json
```

The `--all-output` on the first run is not decoration: read "Choosing the
threshold" below before cutting.

And whatever comes out of here feeds the expensive stage:

```bash
# only the high-interest ones, into nuclei
jq -r '.[].hostname' interesting.json | nuclei -l - -severity critical,high

# or back into httpx, now for screenshots of what matters
jq -r '.[].hostname' interesting.json | httpx -silent -screenshot

# or the top 20 to look at by hand
jq -r '.[:20][] | "\(.priority)  \(.hostname)"' interesting.json

# or the reasons, straight from the record
jq -r '.[:5][] | "\(.priority)  \(.hostname)\n    \(.reasons|join("\n    "))"' interesting.json
```

A real run on a domain of my own (`examples/subfinder-pipeline.txt`):

```
5 subdomains → 5 candidates → 1 request · 33.0s → 5 assets

0.29  reesxss.pwnd.blog       prod 0.43  sens 0.19  admin 0.08  inter 0.54
0.27  zimute.pwnd.blog        prod 0.62  sens 0.13  admin 0.06  inter 0.30
0.23  www.pwnd.blog           prod 0.58  sens 0.14  admin 0.05  inter 0.16

tokens in 7,262   est. cost $0.0003   (jev-1.13.0)
```

### Which parts of `httpx -json` reach Jev

`httpx -json` writes one object per line with around 25 fields, and most of them
are noise for the decision (`timestamp`, `resolvers`, `knowledgebase`, `method`,
`path`, `words`). The Jev docs are explicit: a state carrying material unrelated
to the question drags accuracy down. So the default keeps only what decides,
both in the state and in the output file:

```
resolved_ips  http_status  title  server  ports  technologies  scheme
final_url  redirect_to  cdn  cname  content_length  response_time  probe_failed
```

The names the tools actually use are translated automatically:
`status_code → http_status`, `tech → technologies`, `webserver → server`,
`host_ip`/`a`/`ip → resolved_ips`, `location → redirect_to`, `port → ports`.
Unknown fields pass through, nothing is dropped silently.

Control it with `--meta-fields title,server,http_status` to pick by hand, or
`--meta-all` to send everything, noise included, into both the state and the
output.

One line per port is handled too: httpx emits one line for port 80 and another
for port 443 of the same host, and the two are merged without an empty field
overwriting a filled one (the redirect line has no `title`).

### Choosing the threshold

`--threshold` defaults to 0.55, and it is not a sacred number. The real Jev
scale is lower than it looks. Measured across three different sets, with the
default weights:

```
privileged-looking names (admin-api, bastion, vpn, db, jenkins)   0.51 to 0.63
internal names after dropping the production weight               0.70 to 0.80
static sites with nothing privileged (www, landing, vercel)       0.13 to 0.29
```

So: run the first pass with `--all-output`, look at the distribution, and pick
the cut from the data. If nothing reaches the threshold, the tool says so
instead of handing you a silently empty file:

```
note: no asset reached --threshold 0.55. Top score is 0.28 (reesxss.pwnd.blog).
Real Jev scores sit lower than the mock's, so pick the cut from the data:
try --threshold 0.23, or --all-output to keep every asset.
```

A quick way to calibrate without spending on a huge list:

```bash
jev-recon hosts.txt --limit 300 --all-output calib.json --threshold 0.0
jq -r '[.[].priority] | "max \(max)  min \(min)  median \(sort | .[length/2|floor])"' calib.json
jq -r '[.[].priority] | sort | reverse | .[0:20] | @csv' calib.json
```

Afterwards, with `--cache`, re-cutting and re-weighting cost nothing.

## General usage

```bash
.venv/bin/python -m jev_recon subdomains.txt \
  --batch-size 20 \
  --concurrency 16 \
  --threshold 0.55 \
  --output interesting.json
```

Or, after `pip install -e .`, the console script:

```bash
jev-recon subdomains.txt --concurrency 20 --explain 10
```

### Flags

```
input / output
  -                      stdin (default), for pipes
  --output F             high-interest, sorted (default interesting.json)
  --all-output F         everything, with priority null for what failed
  --report F             counts, batching, weights, usage, cost, errors
  --threshold F          high-interest cut (default 0.55, see above)
  --top N / --explain N  rows in TOP ASSETS / the per-signal table
  --reasons N            why-tree, one block per asset

decision
  --signals a,b,c        subset of the signals; devops exists, off by default
  --weights ...          weights, JSON or k=v, negative weights allowed
  --meta F               per-host enrichment (httpx -json straight in)
  --meta-fields a,b      which metadata fields are kept
  --meta-all             keeps every metadata field, noise included
  --max-per-parent N     caps assets per registered domain
  --max-per-shape N      copies of the same service in the output (default 2, 0 disables)
  --tracking-penalty F   multiplies the priority of a third-party tracking or
                         delivery namespace (default 0.5, 1.0 disables)
  --drop-throwaway       drops dev/test/qa (default: keeps and annotates)
  --limit N              analyzes only the top N by pre_rank

execution
  --cache F              response cache; re-ranking then costs nothing
  --batch-size N         candidates per request (latency)
  --concurrency N        requests in flight (default 8)
  --max-retries N        attempts per request (default 4)
  --timeout F            per request, seconds
  --strict               abort on the first failure (default: continue and mark)
  --dry-run              plan and estimated cost, no API call
  --quiet                no progress, no event log
```

A real run (50,000 subdomains, 24,707 candidates, concurrency 16, against the
local mock from section 7, which is where the `usage` numbers come from):
`examples/console-output.txt`

```
50000 subdomains
        ↓
24707 candidates  (24609 removed: duplicate 20974, invalid_syntax 546, ...)
        ↓
Jev analysis  (1236 requests · batch 20 · concurrency 16 · 9.1s)
        ↓
6 high-interest assets

TOP ASSETS
────────────────────────────────────
0.84  admin.graphql.massive-dynamic.io
0.76  dashboard.graphql.acme-corp.net
0.76  graphql.kibana.stark-industries.com
0.76  grafana.rpc.piedpiper.io
0.74  jenkins.auth.massive-dynamic.io
0.74  jenkins.scada.veridian.net

RUN
────────────────────────────────────
  scored         24707/24707   incomplete 0   unscored 0
  requests       1236 sent   retries 0   rate-limit pauses 0
  tokens         in 26,076,079  out 526,263   est. cost $1.0952   (jev-1.13.0)
```

`--dry-run` builds the requests, counts how many are already in `--cache`, and shows
the plan without spending anything:

```
  requests planned      14
  candidates per request     20
  questions per request 142
  est. input tokens     374,065
  est. cost             $0.0157   (@ $0.042/Mtok, output free)
  already cached        1239 of 1239 requests   (this run would cost $0.0000)
```

That last line is the guard against the most expensive mistake this tool has made:
the cache key covers the state, the questions and the model, so editing one
question invalidates every request that carries it. On a 24,771 host list that is
a silent $1.30. Ask for the dry run first.

### Input

* `.txt`: one hostname per line. Tolerates garbage: `https://`, `:port`, `user@`,
  `*.` (wildcard), `#` comments, `host 1.2.3.4`, uppercase, IPs, URLs with paths.
* `-` (default): stdin, for `subfinder -silent | jev-recon -`.
* `.json`: list of strings, or list of objects with `hostname`/`host`/`input`/`url`.
* `.jsonl`: one object per line, in the `httpx -json` shape.
* `--meta probe.json`: enrichment keyed by hostname (`resolved_ips`, `http_status`,
  `title`, `server`, `ports`, `technologies`), using either the httpx keys or the
  canonical ones. See `examples/metadata.sample.json`. If the input is already
  JSON with those fields, they are picked up automatically.

### Output

`interesting.json` (assets with `priority >= --threshold`, sorted):

```json
[
  {
    "hostname": "admin-api.example.com",
    "priority": 0.94,
    "signals": {
      "likely_production": 0.98,
      "likely_sensitive": 0.96,
      "likely_internal": 0.32,
      "likely_staging": 0.08,
      "likely_admin": 0.93,
      "likely_api": 0.97,
      "interesting_for_security_research": 0.95
    },
    "reasons": [
      "api 0.97  machine-facing API surface",
      "sensitive 0.96  guards sensitive data or functionality",
      "production 0.98  live production system",
      "code: privileged labels admin, api",
      "evidence: gated, HTTP 403",
      "evidence: title \"Jenkins\"",
      "evidence: Jenkins, nginx"
    ],
    "relative_pick": 0.215,
    "weights_used": {"production": 0.25, "sensitive": 0.25, "admin": 0.15, "api": 0.15, "interesting": 0.2},
    "missing_signals": [],
    "shape": "admin.api.example.com",
    "shape_rank": 1,
    "same_shape_count": 1,
    "batch": {"id": 45, "research_yield": 2.0, "yield_confidence": 0.82, "batch_error": null},
    "pre": {"name_tokens": ["admin", "api"], "env_token": null, "privileged_labels": ["admin", "api"], "pre_rank": 2.6},
    "metadata": {},
    "incomplete": false
  }
]
```

* `signals`: the per-question probabilities (Noul), 0 to 1. `null` when the
  answer did not arrive.
* `reasons`: why this asset ranked where it did. Built in Python from the
  probabilities and the facts, never generated by a model. The order is the
  contribution each signal actually made (`weight x value`), so the first line is
  what moved the score. Evidence lines are prefixed with `code:` or `evidence:`.
* `relative_pick`: the batch-level `Choice` question. Choice probabilities sum to
  1, so this is **relative pressure inside the batch**, used only as a
  tie-breaker. It is never read as an absolute signal.
* `batch.research_yield`: the batch-level `Score` question (0, 1 or 2) with the
  confidence of the answer itself. Usable as a gate: a batch with nothing
  interesting does not need expensive analysis afterwards.
* `pre`: facts computed in code (name tokens, detected environment, privileged
  labels), not by a model.
* `incomplete`: `true` when some question went unanswered.

Extras: `--all-output all.json` (everything, not just the high-interest ones),
`--report report.json` (counts, preprocess, batching, weights, usage, errors).

### Cache: re-ranking is not paid twice

```bash
jev-recon list.txt --cache jev-cache.json --output v1.json
jev-recon list.txt --cache jev-cache.json \
  --weights 'sensitive=0.4,admin=0.3,interesting=0.3' --output v2.json
```

The cache key is the hash of the request body (model + state + questions), so a
hit only happens when the request is identical. The second run above finishes in
under a second with `requests 0 sent   cache 3 hits   est. cost $0.0000`, and the
ranking changes only because of the weights. Answers served from the cache are
**not** added to `input_tokens`, because they were already billed when they
arrived. Delete the file to force fresh answers.

One consequence worth knowing: the criteria text is part of the key. Improve a
question and every cached answer for that signal is invalidated, so the next run
pays again.

## 1. Local preprocessing (before spending API budget)

`preprocess.py`, all offline:

| action | how |
| --- | --- |
| normalize | strips scheme, path, query, port, `user@`, `*.`, trailing dot, lowercases |
| deduplicate | a set, after normalizing (`API.x.com` == `api.x.com`) |
| validate | label 1 to 63 chars, no leading or trailing hyphen, alphabetic TLD, total <= 253 |
| drop junk | IP literal, single-label hostname, reserved suffix (`.invalid`, `.test`), wildcard, placeholder labels (`foo`, `asdf`, `dummy`) |
| annotate (not drop) | environment token (`dev`, `test`, `qa`), privileged label (`admin`, `api`, `vpn`), noise label (`cdn`, `static`), depth, `pre_rank` |
| group | `--max-per-parent N` caps how many assets stay per registered domain, keeping the highest `pre_rank` |

An important design point: **`test` and `dev` are not dropped**, they are
annotated. Broken staging assets are exactly where bug bounty tends to pay. Only
names that were never a real host are thrown away.

`pre_rank` is the local heuristic (a sum of known tokens). It orders within a cap
and feeds `--limit`. It is never the final decision.

## 2. What is sent to Jev

One request per batch. State and questions, in the format documented for
`POST /v1/systemone`:

```json
{
  "model": "jev-latest",
  "state": {
    "task": "Each entry in `candidates` is a DNS name ...",
    "candidates": [
      {
        "id": "c00005",
        "hostname": "dev-api.example.com",
        "labels": ["dev-api", "example", "com"],
        "subdomain_depth": 3,
        "code_extracted": {
          "registered_parent": "example.com",
          "name_tokens": ["dev", "api"],
          "env_token": "development",
          "privileged_labels": ["api"],
          "noise_labels": [],
          "pre_rank": 1.15
        },
        "http_status": 403,
        "title": "403 Forbidden",
        "server": "gunicorn",
        "technologies": ["Python", "FastAPI"]
      }
    ]
  },
  "questions": {
    "c0::likely_admin": {
      "type": "noul",
      "instructions": "Does `candidates[0]` look like it exposes an administrative or privileged management interface?",
      "criteria": {"true": "...", "false": "..."}
    },
    "batch::top_pick": {
      "type": "choice",
      "instructions": "Exactly one candidate in `candidates` is the best first target ...",
      "criteria": {"candidates[0]": "dev-api.example.com", "candidates[1]": "..."}
    },
    "batch::research_yield": {
      "type": "score",
      "instructions": "How much does this batch of `candidates` contain at least one asset ...",
      "criteria": ["Little of interest ...", "Mixed ...", "Several plausible leads ..."]
    }
  }
}
```

Each candidate generates 7 `Noul` questions (one per signal), plus 2 batch
questions. A request with 20 candidates carries 142 questions.

The three types are used where each one fits:

* **Noul** for the per-asset signals. The value is absolute and independent:
  seven nouls can all come back low, and that is information. (Noul has no
  separate `confidence`, by design of Jev itself: the probability is the signal.)
* **Choice** for "which one in this batch would you look at first". Probabilities
  sum to 1, so it works as a relative ranking and tie-breaker.
* **Score** for the batch yield, with ordered levels and a `confidence`, used as
  a gate.

Every question is literal and carries `criteria` on both sides (`true` and
`false`) stating what counts as yes and what counts as no. This follows the docs
("write the exact condition in the instructions"), because Jev answers the
question you wrote, not the one you meant.

### `likely_devops`, off by default

A bonus signal exists for build and release infrastructure (CI/CD, registries,
artifact stores, deployment controllers), triggered by names such as `jenkins`,
`ci`, `build`, `deploy`, `argocd`, `drone`, `tekton`. It is not in the default
set because it costs one more question per asset, about 12% more tokens. Turn it
on and weight it:

```bash
jev-recon hosts.txt --signals production,admin,devops \
    --weights 'admin=0.4,devops=0.4,production=0.2' --output infra.json
```

## 3. Batching and concurrency (how it is implemented)

There is no batch endpoint in the TypeSafe API. **Batching happens inside a
single request**: you put several items in the `state` and ask one question per
item, in the same call. All questions in a request are evaluated in parallel, so
20 candidates x 7 signals cost 1 round trip and 142 questions, not 20 round
trips.

That gives two independent knobs:

| knob | controls | effect |
| --- | --- | --- |
| `--batch-size` | candidates per request | fewer requests, lower latency; too large a batch dilutes the model's attention |
| `--concurrency` | requests in flight (asyncio + `Semaphore`) | throughput; 20 already saturates the requests-per-minute rate limit |

Implementation in `jev.py`:

1. `effective_batch_size()` shrinks the batch, before any call, to fit the
   documented limits (`--max-questions-per-request`, default 220, and
   `--max-request-tokens`, default 24,000, against the documented ceiling of 32k
   for `state` plus the longest question and 64k overall).
2. `split_batches()` slices the candidates.
3. `asyncio.gather` plus `asyncio.Semaphore(concurrency)`, with one shared
   `httpx.AsyncClient` and `Limits(max_connections=concurrency)`.
4. No batch is dropped silently: a failed batch produces assets with
   `priority: null`, `incomplete: true` and `batch.batch_error` filled in, and
   the process exits with code 1.

Five hundred thousand simultaneous requests are never fired. The ceiling is
`--concurrency`, default 8.

**Where the money goes** (measured, `--dry-run`): in a request with 20
candidates, `state` is 2,422 tokens (121 per candidate) and `questions` is 18,753
tokens (132 per question). The `criteria` text dominates, and it is resent once
per candidate, because each question is about one item. Practical conclusion:
about 1,050 tokens per asset, roughly $0.000044 per asset at $0.042/Mtok of input
(output is free), which is about $1.10 per 25,000 assets. Lowering `--signals`
cuts the cost proportionally (3 signals instead of 7 is about 45% of the cost).
Raising `--batch-size` barely changes cost; it is a latency knob.

## 4. Ranking in Python

`rank.py`, with no model involved:

```python
priority = (
    production * 0.25 +
    sensitive  * 0.25 +
    admin      * 0.15 +
    api        * 0.15 +
    interesting* 0.20
)
```

* Denominator is the sum of all configured weights. A signal that did not arrive
  **drags the score down** instead of being renormalized away: an unmeasured
  asset must not look more interesting than a measured one.
* Weights are configurable and normalized: `--weights 'production=0.3,staging=-0.1'`.
  A negative weight penalizes staging, which the default does not do (the default
  ignores `internal` and `staging`, exactly as in the formula above).
* Sorting: priority, then the batch `relative_pick`, then name.
* `--threshold` decides what becomes "high-interest" (default 0.55).

### Why an asset ranked where it did

`reasons` is computed in code from the numbers and the facts:

```
WHY (top 2)
────────────────────────────────────
0.94  jenkins.api.corp.com
      ├─ admin 0.99  admin or management surface
      ├─ devops 0.93  build or deploy infrastructure
      ├─ production 0.86  live production system
      └─ code: known labels jenkins, api
0.59  grafana.corp.com
      ├─ admin 0.96  admin or management surface
      ├─ production 0.86  live production system
      └─ code: known labels grafana
```

Signals appear when they cross 0.60, ordered by `weight x value`, so the top line
is the reason that moved the score. Then the facts: the environment token, the
privileged labels, a gated `HTTP 401/403`, the page title, the detected
technologies. An asset with nothing above the floor prints
`no signal above the floor` instead of an invented reason.

### One service repeated across regions does not eat the list

Enumeration produces copies: `us-central-1.api.acme.com` through
`us-central-8.api.acme.com`, `eu-west-1.api.acme.com`, `us-west-1.api.acme.com`. These are
**one** service. Spending manual analysis on the fourth region after seeing the
first yields nothing, so the output groups by *shape*: the name with region,
version, counter and hash tokens removed.

```
us-central-3.api.acme.com        -> api.acme.com
eu-west-1.api.acme.com           -> api.acme.com
api.widget-v2.acme.com             -> api.widget.acme.com
widget-3p5-0621.us-east-1.api.acme.com -> api.widget.acme.com
```

Environment tokens stay: `dev-api` and `api` remain different surfaces, not
copies. `--max-per-shape` (default 2) keeps the best N of each shape in the
output and marks the rest with `suppressed_by_shape`, `shape_rank` and
`same_shape_count`. `--max-per-shape 0` disables it. Every asset stays in
`--all-output`, and the funnel reports how many copies were held back.

Measured on 195 hosts of a real target:

```
before:  49 high-interest, with 12 of the top 30 being *.api.acme.com (one per region)
after:   38 high-interest (11 copies held back)

top shapes in output: 2 api.widget.acme.com · 2 api.acme.com · 2 api.embedding.fte5.models.acme.com
suppressed:           eu-west-1.api · us-west-1.api · us-central-1..8.api  (copy #3 to #11)
```

The shape is an exact string heuristic, done in code, and deliberately
conservative: it removes obvious copies (region, version, counter, hash) but does
not try to guess semantic equivalence (`sso-auth` and `sso` stay different
shapes). Deciding whether two names are the same service is a semantic judgment,
and that belongs to the model or to you, not to a regex.

### What the default weights do to real data

Measured on a real sample: `likely_production` comes back **low** (0.24 to 0.48)
for internal infrastructure, because the question is literal about serving real
users or customers, and a bastion or an internal Postgres serves no customer.
With the default weights this caps the top of the list around 0.63 and pushes
exactly those assets down:

```
0.63  us-east-1.argocd.globex.com.br     prod 0.39  sens 0.80  admin 0.89
0.62  eu-west-1.auth.globex.com.br       prod 0.38  sens 0.84  admin 0.81
0.60  eu-west-1.bastion.tyrell-corp.com  prod 0.27  sens 0.91  admin 0.86
```

If your target is an internal panel rather than a public surface, take weight
away from `production` and put it on `sensitive` and `admin`:

```
--weights 'sensitive=0.35,admin=0.30,api=0.10,interesting=0.25'

0.79  eu-west-1.bastion.tyrell-corp.com  prod 0.26  sens 0.91  admin 0.88
0.75  ap-south-1.db.tyrell-corp.com      prod 0.24  sens 0.88  admin 0.81
0.74  us-east-1.argocd.globex.com.br     prod 0.46  sens 0.81  admin 0.88
0.73  us-east-1.rdp.acme-corp.net        prod 0.40  sens 0.85  admin 0.81
```

With `--cache` the second pass costs nothing and finishes in under a second, so
calibrating weights is cheap: run once, re-weight as many times as you like.

## 5. Errors and rate limits

No SDK dependency: the HTTP client implements what the docs recommend ("retry
with exponential backoff").

| situation | handling |
| --- | --- |
| `429 Too Many Requests` | exponential backoff with jitter, honoring `retry-after` (seconds or HTTP date). The pause is **global**: a shared `_resume_at` makes every worker wait, otherwise concurrency recreates the 429 |
| `529 Overloaded` / `5xx` / `408` / timeout / network error | retry with backoff up to `--max-retries` (default 4) |
| `422 Unprocessable Entity` | batch too large or invalid question. The client **splits the batch in half and retries** (up to 3 levels deep) instead of losing candidates |
| `401` | fatal error, with a message pointing at `TYPESAFE_API_KEY`; exit 2 |
| batch failed for good | assets come out with `priority: null`, `incomplete: true`, `batch_error`, a warning on stderr, exit 1. `--strict` aborts on the first failure |
| missing `answers`, or an unexpected `type` | treated as a `null` signal, not as zero |
| no request at all | `--dry-run` shows the full plan and never calls the API |

Real evidence of the error paths is in `examples/fault-injection.txt`: 429
honoring `retry-after`, 503 with retry, 422 splitting batches, 297 splits, and
still 1,983/1,983 assets scored with 0 batches lost.

## 6. Known limits of Jev (and how the project handles them)

From the jaggedness page of `jev-1.13`, applied here:

* **Literal reading**: explicit criteria on both sides of every Noul.
* **It does not count, it does not do math**: no "how many subdomains have X".
  Recursion, sorting and weights stay in code.
* **Context rot**: the `state` carries only what is needed (name, labels, facts,
  metadata you supplied). `--batch-size` controls how much unrelated material
  travels with it: a smaller batch tends to more precision, a larger one to less
  latency. Test it on your own data.
* **Adversarial content**: `title` and other fields come from outside and are
  data, not instructions. Jev does not treat state as hostile by default; if you
  run this against third-party assets, treat `title`/`technologies` as untrusted
  input and validate the result before acting.
* **Indirection**: each question is one decision, and points at its path in the
  state (`candidates[3]`) instead of hiding several judgments in one question.
* The holistic question (`interesting_for_security_research`) is kept on request,
  but the signal that carries the decision is the composition of the others: it
  is the question a traditional LLM would answer, and it is here to be compared.

## 7. Does the semantic ranking beat a keyword list?

`BENCHMARK.md`, with `scripts/make_benchmark.py` and `scripts/benchmark.py`. Five
methods (random, name heuristic, evidence keyword regex, Jev on names, Jev on
names plus evidence) scored with precision and recall at the top 5, 10 and 20
percent, on 300 hosts whose names carry no information about the label by
construction, averaged over three label draws:

```
method                   P@10%   R@10%   |   P@20%   R@20%
random                   0.333   0.111   |   0.311   0.207
name heuristic           0.278   0.093   |   0.295   0.196
evidence keywords        0.978   0.326   |   0.689   0.459
jev (names)              0.200   0.067   |   0.233   0.156
jev (names+evidence)     0.967   0.322   |   0.956   0.637
```

* Names alone land at chance, which is the point of the design: with no evidence
  there is no signal to extract, whatever the method.
* At the top 10% the keyword regex is slightly ahead (it maximizes precision by
  only ever finding the obvious). At the top 20% it collapses to 0.689 while Jev
  holds 0.956, with recall 0.637 against 0.459. The word list runs out of things
  to find at around 30% coverage; Jev keeps finding.
* On the tier where the evidence is ambiguous (a generic title behind a 401/403)
  the regex scores zero by construction, and Jev only gets there after the
  criteria explicitly describe that condition. Full write-up, including the
  negative result and what the benchmark does not prove, in `BENCHMARK.md`.

## 8. Tests and demo without an API key

```bash
.venv/bin/python -m unittest discover -s tests     # 62 tests, no extra dependencies
.venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest -q
```

The suite runs the same under pytest and unittest, from any directory and without
installing the package (`tests/_bootstrap.py` puts the repo root and `scripts/`
on `sys.path`; unittest does not read `conftest.py` and bare pytest does not add
the working directory).

The tests cover preprocessing, the ranking arithmetic, and end to end against the
mock: one request per batch, concurrency (12 batches with artificial latency
overlap in flight), 429 with `retry-after`, 503 with retry, 422 splitting a
batch, an invalid key, a dead endpoint (exit 1, assets preserved as
`incomplete`), custom weights, `--dry-run`, the output files, missing `--meta`
and input files, and the threshold warning.

`scripts/mock_typesafe_server.py` answers in the documented format, with fault
injection:

```bash
# terminal 1
.venv/bin/python scripts/mock_typesafe_server.py --port 8712

# terminal 2
.venv/bin/python scripts/gen_sample.py 50000 > subdomains.txt
.venv/bin/python -m jev_recon subdomains.txt \
  --base-url http://127.0.0.1:8712 --api-key mock-key-0123456789abcdef \
  --threshold 0.55 --explain 8
```

The mock is not a model: it scores by name tokens and a hash. It exists to
exercise the whole pipeline (and the failures) with no key and no cost. The
`usage` numbers shown in the demos come from the mock's own accounting; in
production, read `usage` from the real response and the `model` that answered
(the `jev-latest` alias moves between versions, and the code records which one
answered in `--report`).

Injection flags: `--rate-limit-every N`, `--retry-after S`, `--fail-every N`,
`--reject-over-questions N`, `--latency S`.

## 9. References

* API: https://docs.typesafe.ai/api
* Primitives (Choice, Score, Noul): https://docs.typesafe.ai/primitives
* State: https://docs.typesafe.ai/concepts/state
* Composite scoring: https://docs.typesafe.ai/patterns/composite-scoring
* Speculative fan-out: https://docs.typesafe.ai/patterns/fan-out
* Re-ranking: https://docs.typesafe.ai/cookbooks/rerank_typesafe
* Batching / parallel questions: https://docs.typesafe.ai/cookbooks/parallel_questions
* Jev 1.13 jaggedness: https://docs.typesafe.ai/model-jaggedness/jev-1.13
* Models and limits: https://docs.typesafe.ai/models

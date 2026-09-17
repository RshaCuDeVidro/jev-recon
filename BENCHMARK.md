# Benchmark: does the semantic ranking add value over a heuristic?

`scripts/make_benchmark.py` and `scripts/benchmark.py`. The question they answer:
when names say nothing, only evidence (HTTP response, title, tech) separates what
matters, and **how much** does each method recover.

## Design

300 hosts. Every name comes from the **same** pool of innocuous templates
(`node-07`, `svc-3`, `cache-12`, `sqlproxy-66`), and the class is assigned at
random and then backed by evidence. In other words the name carries no
information about the label, by construction, so a method that only reads the
name is measuring noise. That is what makes the test honest instead of theatre.

Two tiers of "interesting", because that is where the difference shows up:

| tier | what it is | does a regex find it? |
| --- | --- | --- |
| `obvious` | the evidence names a privileged product: Jenkins, GitLab, phpMyAdmin, Portainer, Kibana, Grafana, MinIO, Proxmox, RabbitMQ, Prometheus | yes |
| `subtle` | a generic title (`Console`, `Portal`, `Overview`, `Manage`) behind an HTTP 401/403. No title contains a word from the keyword list, and the status is invisible to a regex over title plus tech | no |

Five methods: `random` (control), `name heuristic` (`preprocess.pre_rank`, name
tokens only), `evidence keywords` (the naive regex a person writes first),
`jev (names)` and `jev (names+evidence)`.

## Result (average of 3 sets, 300 hosts each, 90 carrying privileged evidence)

```
method                   P@10%   R@10%   |   P@20%   R@20%
----------------------------------------------------------
random                   0.333   0.111   |   0.311   0.207
name heuristic           0.278   0.093   |   0.295   0.196
evidence keywords        0.978   0.326   |   0.689   0.459
jev (names)              0.200   0.067   |   0.233   0.156
jev (names+evidence)     0.967   0.322   |   0.956   0.637
```

What can be claimed from this:

1. **Names alone are a coin flip.** `jev (names)` sits at the level of `random`,
   and it has to, because the set was built that way. That is not a failure of
   the model: with no evidence there is no signal to extract. Anyone selling
   "rank subdomains by name" is selling noise with a method's face on it.
2. **With evidence the gain is real and grows with the cut.** At P@10% the two
   tie (0.967 against 0.978), but at P@20% the regex drops to 0.689 while Jev
   holds 0.956, with recall 0.637 against 0.459. In plain terms: the keyword list
   exhausts what it can find at around 30% of the targets, and Jev keeps finding
   after that.
3. **On the subtle tier a regex scores zero.** By construction there is no word
   to match. Jev also did badly at first (2.3 of 31, below random), and the
   reason is the documented jaggedness: it answers the question you wrote. The
   `criteria` talked about names and never mentioned a gated response. After
   writing the actual condition ("a title that is a generic management word on a
   page that answers HTTP 401 or 403"), it went to 4.7 of 31, against 2.7 for
   random and 0.0 for the regex:

```
subtle tier, hits in the top 30 (out of 31 targets)
random                2.7  ->  2.7
evidence keywords     0.0  ->  0.0
jev (names)           2.7  ->  2.3
jev (names+evidence)  2.3  ->  4.7
```

4. **The honest cost of trading one thing for another:** with the longer
   `criteria`, recall on the obvious tier fell from 0.441 to 0.413. Sharpening
   one question can displace another. The composite at P@20% improved, but the
   oscillation is real, and it is why measurement exists.

## A metric that lied, and the decoy class

The benchmark measures promotion into a top slice. When a rule only *demotes*
something, that metric is blind to it: a host pushed from rank 81 to rank 295 is
still outside the top 30, before and after. Measured that way, the criteria edit
that excludes third-party tracking namespaces looked useless (7 of 15 decoys
promoted before the edit, 7 of 15 after).

Priority tells the truth. Six hosts, names that reproduce the real failure
(`click.c.email.api.acme.com` ranked 0.60 on a real list, above `sso-auth.acme.com`),
run twice, once against the criteria before the edit and once after:

```
host                                      criteria before   criteria after
api.banco-ficticio.com.br                            0.61             0.62
api.ficticia-industria.com                           0.56             0.56
api.grupo-fantasia.com.br                            0.56             0.56
click.c.email.api.ficticia-industria.com             0.49             0.15
click.c.email.api.banco-ficticio.com.br              0.46             0.12
track.c.email.api.grupo-fantasia.com.br              0.44             0.11
```

The genuine API hosts do not move. The decoys lose two thirds of their score.
So for this class the criteria text does the work, and the deterministic
`tracking_namespace` penalty (`--tracking-penalty`, default 0.5) is insurance: it
halves something the model already demotes, which changes nothing at any sane
threshold, and it catches the case where a model version scores such a name high.

Lesson worth keeping: a promotion metric cannot evaluate a demotion rule. The
decoys also have to sit near the cut, or the slice never sees them move.

## What this benchmark does NOT prove

* The label comes from HTTP evidence, so `evidence keywords` and both Jev lines
  are scored on material in the same family as the label. This measures "does the
  pipeline recover what the evidence says is there", **not** "does it find a real
  vulnerability".
* The set is synthetic and the `subtle` tier is an explicit hypothesis of mine
  about what a human would consider interesting. If that hypothesis is wrong, the
  tier number is wrong with it.
* On real data there is no label. There, the only thing measurable is agreement
  and disagreement between methods, plus inspecting the cases where they diverge.
  What this script provides is the size of the effect when a label does exist.

## Reproduce

```bash
.venv/bin/python scripts/make_benchmark.py --hosts 300 --gold 90 --seed 11 --out bench-11/
.venv/bin/python scripts/benchmark.py --bench bench-11/ --cache bench-11/cache.json \
    --out bench-11/results.json
```

Each set costs about $0.027 (two conditions, 15 requests each). The script
refuses to run when the local filter drops any host, so a comparison never
degrades into a measurement of the filter.

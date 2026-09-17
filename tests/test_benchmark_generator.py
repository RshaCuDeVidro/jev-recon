"""The decoy generator has to keep producing the class that actually failed.

On a real list, `click.c.email.api.acme.com` ranked 0.60 on names alone, above
`sso-auth.acme.com`. A decoy that only carries the tracking side does not reproduce
that: the model rejects it on its own, measured at 0.13 to 0.21. And a name that
is genuinely one-sided must stay boring, or the control measures nothing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

from make_benchmark import AMBIGUOUS_NAMES, build                # noqa: E402
from jev_recon.preprocess import tracking_namespace              # noqa: E402

PRIVILEGED = {"api", "admin", "sso", "console"}
SEEDS = (31, 41, 99)


class TestDecoySet(unittest.TestCase):
    def test_every_decoy_is_the_dangerous_combination(self):
        """Both vocabularies and a privileged label: tracking alone is not it."""
        for seed in SEEDS:
            _, labels = build(300, 90, seed)
            decoys = [h for h, tier in labels.items() if tier == "decoy"]
            self.assertTrue(decoys)
            for host in decoys:
                labels_set = set(host.split("."))
                self.assertTrue(tracking_namespace(tuple(host.split("."))), host)
                self.assertTrue(labels_set & PRIVILEGED, host)

    def test_one_sided_names_stay_boring_and_do_not_trip_the_rule(self):
        prefixes = tuple(t.split("{")[0].rstrip(".") for t in AMBIGUOUS_NAMES)
        for seed in SEEDS:
            _, labels = build(300, 90, seed)
            control = [h for h, tier in labels.items()
                       if tier == "boring" and h.startswith(prefixes)]
            self.assertTrue(control, "the set lost its ambiguous control")
            for host in control:
                self.assertFalse(tracking_namespace(tuple(host.split("."))), host)

    def test_labels_cover_every_host_exactly_once(self):
        for seed in SEEDS:
            rows, labels = build(300, 90, seed)
            self.assertEqual(len(rows), len(labels))
            self.assertEqual({h for h in labels if labels[h] == "obvious"} | 
                             {h for h, t in labels.items() if t == "subtle"} |
                             {h for h, t in labels.items() if t == "decoy"} |
                             {h for h, t in labels.items() if t == "boring"},
                             {r["hostname"] for r in rows})


if __name__ == "__main__":
    unittest.main()

"""The snapshot crawl (crawl.py) as a test: every response from every
entry point within Discord's limits and the copy rules, every page
within its component count from the spec, and the text renders equal
to the goldens in snapshots/. After an intended change, rewrite them
with `python3 tests/settingsui/crawl.py --update` and review the diff.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_crawl.py"""
import difflib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402

RESULTS = {}


def results():
    if not RESULTS:
        RESULTS.update(crawl.crawl_all())
    return RESULTS


class Crawl(unittest.TestCase):
    def test_no_problems(self):
        bad = [p for c in results().values() for p in c.problems]
        self.assertEqual(bad, [], "\n".join(bad[:40]))

    def test_every_entry_point_reached_its_pages(self):
        res = results()
        self.assertEqual(set(res), {e[0] for e in crawl.entries()})
        self.assertGreater(len(res["owner_channel"].pages), 300)
        self.assertGreater(len(res["owner_dm"].pages), 300)
        self.assertGreater(len(res["admin_panel"].pages), 8)
        seen = {s for c in res.values() for s, _n, _p in c.screens}
        for screen in crawl.SPEC_COUNTS:
            self.assertIn(screen, seen, f"no {screen} page was crawled")

    def test_spec_component_counts(self):
        over = []
        for c in results().values():
            for screen, n, path in c.screens:
                if screen and n > crawl.SPEC_COUNTS[screen]:
                    over.append(f"{path}: {screen} has {n} components "
                                f"(spec: {crawl.SPEC_COUNTS[screen]})")
        self.assertEqual(over, [], "\n".join(over[:20]))

    def test_goldens(self):
        diffs = []
        for name, c in results().items():
            path = os.path.join(crawl.SNAPSHOTS, f"{name}.txt")
            got = crawl.golden_text(c) + "\n"
            try:
                with open(path) as f:
                    want = f.read()
            except OSError:
                diffs.append(f"missing golden {path}")
                continue
            if got != want:
                diffs.append(f"{name}:\n" + "\n".join(list(
                    difflib.unified_diff(want.splitlines(),
                                         got.splitlines(), "golden", "now",
                                         lineterm="", n=1))[:40]))
        self.assertEqual(diffs, [], "\n\n".join(diffs) + "\n\n(After an "
                         "intended change: python3 tests/settingsui/"
                         "crawl.py --update)")


if __name__ == "__main__":
    unittest.main(verbosity=1)

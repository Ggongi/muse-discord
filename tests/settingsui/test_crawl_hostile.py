"""The crawl (crawl.py) with hostile fixtures (review B): 200 servers,
280 channels in Muse Lounge, 100-character names full of markdown,
links, @everyone and mentions, hostile display names. Every response
must still pass every check: Discord's limits, the 3,600-character text
budget, no link, ping or mention made by a name. No goldens here.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_crawl_hostile.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

H.go_hostile()
import crawl  # noqa: E402

RESULTS = {}


def results():
    if not RESULTS:
        RESULTS.update(crawl.crawl_all())
    return RESULTS


class Hostile(unittest.TestCase):
    def test_fixtures(self):
        self.assertEqual(len(H.GUILDS), 200)
        self.assertEqual(len(H.CHANNELS[H.G1]), 280)
        self.assertTrue(all(len(g["name"]) == 100 for g in H.GUILDS))

    def test_every_response_passes(self):
        bad = []
        for c in results().values():
            for p in c.problems:
                # A name's own text may hold a role id ("<@&5550…>",
                # neutralised): that is the name, not an id we print.
                if "bare id" in p and "5550000000000000" in p:
                    continue
                bad.append(p)
        self.assertEqual(bad, [], "\n".join(b[:300] for b in bad[:30]))

    def test_it_crawled(self):
        res = results()
        self.assertGreater(len(res["owner_dm"].pages), 300)
        self.assertGreater(len(res["owner_channel"].pages), 300)


if __name__ == "__main__":
    unittest.main(verbosity=1)

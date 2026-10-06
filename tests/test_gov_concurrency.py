"""验证数据库级并发不变量：并发授权最多一个版本有效、回调不重复计数。"""

import threading
import unittest

from science_strategy_foundation.errors import ConflictError

from _gov_helpers import GovScenario


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])

    def tearDown(self):
        self.ctx.close()

    def _prepare_approved_proposal(self, proposal_id: str):
        self.gov.submit_proposal(request_id=f"sub-{proposal_id}", actor_id="rep2",
                                 proposal_id=proposal_id, dataset_id="obs-A",
                                 dataset_version=1, purpose="研究")
        self.gov.cast_ballot(request_id=f"b1-{proposal_id}", actor_id="rep1",
                             proposal_id=proposal_id, vote="yes")
        self.gov.cast_ballot(request_id=f"b2-{proposal_id}", actor_id="rep2",
                             proposal_id=proposal_id, vote="yes")

    def test_concurrent_grants_only_one_active(self):
        self._prepare_approved_proposal("P1")
        self._prepare_approved_proposal("P2")
        outcomes: list[str] = []
        lock = threading.Lock()

        def resolve(proposal_id: str, request_id: str):
            try:
                self.gov.resolve_proposal(request_id=request_id, actor_id="sec1",
                                          proposal_id=proposal_id)
                result = "ok"
            except ConflictError:
                result = "conflict"
            with lock:
                outcomes.append(result)

        t1 = threading.Thread(target=resolve, args=("P1", "res-P1"))
        t2 = threading.Thread(target=resolve, args=("P2", "res-P2"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(2, len(outcomes))
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(1, outcomes.count("conflict"))
        active = [row for row in self.gov.grant_timeline("m2", "obs-A")
                  if row["status"] == "active"]
        self.assertEqual(1, len(active))

    def test_concurrent_identical_callbacks_counted_once(self):
        self._prepare_approved_proposal("P1")
        self.gov.resolve_proposal(request_id="res-P1", actor_id="sec1", proposal_id="P1")
        grant_id = [r for r in self.gov.grant_timeline("m2", "obs-A")
                    if r["status"] == "active"][0]["grant_id"]
        outcomes: list[str] = []
        lock = threading.Lock()

        def callback(rid: str):
            try:
                self.gov.register_download_callback(
                    request_id=rid, actor_id="sec1", callback_id="dup-cb",
                    grant_id=grant_id)
                result = "ok"
            except ConflictError:
                result = "conflict"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=callback, args=(f"cb-{i}",)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(4, outcomes.count("conflict"))
        self.assertEqual(1, len(self.gov.list_downloads()))


if __name__ == "__main__":
    unittest.main()

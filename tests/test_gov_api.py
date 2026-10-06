"""治理领域的 HTTP 路由测试。"""

import json
import unittest

from science_strategy_foundation.api import route
from science_strategy_foundation.gov_service import GovernanceService

from _gov_helpers import GovScenario


class GovApiTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.service = self.ctx.service
        self.gov = self.ctx.gov

    def tearDown(self):
        self.ctx.close()

    def _call(self, method, path, body=None, actor="sec1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, governance=self.gov)

    def _approve(self, proposal_id="prop1"):
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        status, _ = self._call("POST", "/gov/proposals", {
            "request_id": "sub1", "proposal_id": proposal_id, "dataset_id": "obs-A",
            "dataset_version": 1, "purpose": "气候研究"}, actor="rep2")
        self.assertEqual(201, status)
        self._call("POST", "/gov/ballots", {"request_id": "b1", "proposal_id": proposal_id,
                                            "vote": "yes"}, actor="rep1")
        self._call("POST", "/gov/ballots", {"request_id": "b2", "proposal_id": proposal_id,
                                            "vote": "yes"}, actor="rep2")
        status, body = self._call("POST", "/gov/proposals/resolve",
                                  {"request_id": "res1", "proposal_id": proposal_id})
        self.assertEqual(201, status)
        return body

    def test_member_registration_endpoint(self):
        status, body = self._call("POST", "/gov/members", {
            "request_id": "m9", "member_id": "m9", "kind": "institution",
            "name": "某大学", "weight": 1.5})
        self.assertEqual(201, status)
        self.assertEqual("m9", body["resource_id"])

    def test_full_flow_and_explain_endpoint(self):
        self._approve()
        status, body = self._call("GET", "/gov/access-explain?member_id=m2&dataset_id=obs-A",
                                  actor="rep2")
        self.assertEqual(200, status)
        self.assertTrue(body["allowed"])
        self.assertEqual("access_allowed", body["reason"])
        gates = {gate["gate"]: gate["passed"] for gate in body["gates"]}
        self.assertTrue(gates["resolution_approved"])
        # 证据链指向提交时章程与快照
        self.assertEqual("charter:1", body["evidence"]["baseline_at_submission"])
        self.assertIn("snapshot_hash", body["evidence"])

    def test_partner_cannot_query_other_member_explanation(self):
        self._approve()
        status, body = self._call("GET", "/gov/access-explain?member_id=m1&dataset_id=obs-A",
                                  actor="rep2")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

    def test_suspend_blocks_and_resume_restores_via_api(self):
        self._approve()
        status, _ = self._call("POST", "/gov/grants/suspend", {
            "request_id": "s1", "member_id": "m2", "dataset_id": "obs-A", "reason": "核查"})
        self.assertEqual(201, status)
        status, body = self._call("GET", "/gov/access-explain?member_id=m2&dataset_id=obs-A",
                                  actor="rep2")
        self.assertFalse(body["allowed"])
        status, _ = self._call("POST", "/gov/grants/resume", {
            "request_id": "r1", "member_id": "m2", "dataset_id": "obs-A", "reason": "解除"})
        self.assertEqual(201, status)
        status, body = self._call("GET", "/gov/access-explain?member_id=m2&dataset_id=obs-A",
                                  actor="rep2")
        self.assertTrue(body["allowed"])

    def test_duplicate_ballot_api_returns_conflict(self):
        self._approve = self._approve  # noqa
        # 第二个提案用于重复投票场景
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1"])
        self._call("POST", "/gov/proposals", {
            "request_id": "subX", "proposal_id": "propX", "dataset_id": "obs-A",
            "dataset_version": 1, "purpose": "x"}, actor="rep1")
        self._call("POST", "/gov/ballots", {"request_id": "bx1", "proposal_id": "propX",
                                            "vote": "yes"}, actor="rep1")
        status, body = self._call("POST", "/gov/ballots", {
            "request_id": "bx2", "proposal_id": "propX", "vote": "no"}, actor="rep1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"])

    def test_unfulfilled_endpoint_and_audit_chain(self):
        self.ctx.commitment("c1", "rep1", "m1", 100)
        status, body = self._call("GET", "/gov/unfulfilled")
        self.assertEqual(200, status)
        self.assertTrue(any(item["member_id"] == "m1" for item in body["items"]))
        status, body = route(self.service, "GET", "/health", None,
                             governance=self.gov)
        self.assertEqual(200, status)
        self.assertTrue(body["audit_valid"])

    def test_idempotent_replay_returns_200(self):
        payload = {"request_id": "m9", "member_id": "m9", "kind": "institution", "name": "某大学"}
        first_status, _ = self._call("POST", "/gov/members", payload)
        replay_status, body = self._call("POST", "/gov/members", payload)
        self.assertEqual(201, first_status)
        self.assertEqual(200, replay_status)
        self.assertTrue(body["replayed"])


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta, timezone

from science_strategy_foundation.api import route
from science_strategy_foundation.clock import MutableClock
from science_strategy_foundation.governance import GovernanceService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class GovernanceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(T0)
        self.service = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.clock)
        self.service.register_organization(
            request_id="org-sec", actor_id="bootstrap", organization_id="sec", name="秘书处")
        self.service.register_actor(
            request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin-1",
            display_name="管理员", role="admin", organization_id="sec")
        self.service.register_actor(
            request_id="actor-sec", actor_id="admin-1", new_actor_id="sec-1",
            display_name="干事", role="secretariat", organization_id="sec")
        self.service.register_organization(
            request_id="org-a", actor_id="admin-1", organization_id="org-a", name="甲国")
        self.service.register_actor(
            request_id="actor-a", actor_id="admin-1", new_actor_id="rep-a",
            display_name="代表甲", role="representative", organization_id="org-a")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="sec-1"):
        return route(self.service, "POST", path, body,
                     {"X-Actor-Id": actor}, governance=self.gov)

    def _get(self, path, actor="sec-1"):
        return route(self.service, "GET", path, None,
                     {"X-Actor-Id": actor}, governance=self.gov)

    def _seed(self):
        self._post("/governance/charters", {
            "request_id": "c1",
            "rules": {"embargo_days": {"restricted": 30}, "access_min_fulfillment": 0.5,
                      "embargo_access_fulfillment": 1.0, "third_party_transfer": "allowed"},
        })
        self._post("/governance/members", {
            "request_id": "m1", "member_id": "ma", "name": "甲国", "kind": "country",
            "organization_id": "org-a", "voting_weight": 1.0,
        })
        due = (T0 + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        self._post("/governance/commitments", {
            "request_id": "com1", "member_id": "ma", "kind": "funding",
            "amount": 100, "unit": "kUSD", "due_at": due,
        })
        self._post("/governance/contributions", {
            "request_id": "pay1", "member_id": "ma", "kind": "funding", "amount": 100,
        })
        self._post("/governance/datasets", {
            "request_id": "d1", "dataset_id": "ds-1", "title": "观测数据集",
        })
        self._post("/governance/dataset-versions", {
            "request_id": "dv1", "dataset_id": "ds-1", "sensitivity": "restricted",
        })

    def test_full_flow_over_http(self):
        self._seed()
        status, proposal = self._post("/governance/proposals", {
            "request_id": "p1", "dataset_id": "ds-1", "purpose": "研究",
        }, actor="rep-a")
        self.assertEqual(201, status)
        self.assertEqual("approved", proposal["status"])
        status, replay = self._post("/governance/proposals", {
            "request_id": "p1", "dataset_id": "ds-1", "purpose": "研究",
        }, actor="rep-a")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, access = self._get("/governance/access?member_id=ma&dataset_id=ds-1",
                                   actor="rep-a")
        self.assertEqual(200, status)
        self.assertTrue(access["allowed"])
        status, standing = self._get("/governance/member-standing?member_id=ma")
        self.assertEqual(200, status)
        self.assertEqual(1.0, standing["fulfillment_ratio"])
        status, usage = self._get("/governance/dataset-usage?dataset_id=ds-1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(usage["licenses"]))

    def test_governance_route_requires_service(self):
        status, payload = route(self.service, "GET", "/governance/access?member_id=ma&dataset_id=ds-1",
                                None, {"X-Actor-Id": "sec-1"})
        self.assertEqual(404, status)

    def test_unknown_governance_route(self):
        status, payload = self._get("/governance/unknown")
        self.assertEqual(404, status)

    def test_permission_denied_maps_to_403(self):
        self._seed()
        status, payload = self._post("/governance/charters", {
            "request_id": "c2", "rules": {},
        }, actor="rep-a")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_missing_query_param_is_400(self):
        status, payload = self._get("/governance/access?member_id=ma")
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()

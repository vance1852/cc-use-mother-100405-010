import unittest
from datetime import datetime, timedelta, timezone

from science_strategy_foundation.clock import MutableClock
from science_strategy_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from science_strategy_foundation.governance import GovernanceService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

RULES = {
    "quorum_fraction": 0.5,
    "embargo_days": {"restricted": 30, "sensitive": 90},
    "access_min_fulfillment": 0.5,
    "embargo_access_fulfillment": 1.0,
    "authorship_min_share": 0.2,
    "third_party_transfer": "resolution",
    "license_days": 365,
    "sensitive_requires_resolution": True,
}


def at(days=0, hours=0):
    return (T0 + timedelta(days=days, hours=hours)).isoformat().replace("+00:00", "Z")


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(T0)
        self.foundation = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.clock)
        self.foundation.register_organization(
            request_id="org-sec", actor_id="bootstrap", organization_id="sec", name="秘书处")
        self.foundation.register_actor(
            request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin-1",
            display_name="管理员", role="admin", organization_id="sec")
        self.foundation.register_actor(
            request_id="actor-sec", actor_id="admin-1", new_actor_id="sec-1",
            display_name="秘书处干事", role="secretariat", organization_id="sec")
        for key in ("a", "b", "c"):
            self.foundation.register_organization(
                request_id=f"org-{key}", actor_id="admin-1",
                organization_id=f"org-{key}", name=f"机构{key}")
            self.foundation.register_actor(
                request_id=f"actor-{key}", actor_id="admin-1", new_actor_id=f"rep-{key}",
                display_name=f"代表{key}", role="representative", organization_id=f"org-{key}")
        self.charter = self.gov.create_charter(
            request_id="charter-1", actor_id="sec-1", rules=RULES)
        self.gov.register_member(request_id="mem-a", actor_id="sec-1", member_id="ma",
                                 name="甲国", kind="country", organization_id="org-a",
                                 voting_weight=2.0)
        self.gov.register_member(request_id="mem-b", actor_id="sec-1", member_id="mb",
                                 name="乙机构", kind="institution", organization_id="org-b",
                                 voting_weight=1.0)
        self.gov.register_member(request_id="mem-c", actor_id="sec-1", member_id="mc",
                                 name="丙机构", kind="institution", organization_id="org-c",
                                 voting_weight=1.0)

    def tearDown(self):
        self.database.close()

    # ---- 准备工具 -------------------------------------------------------

    def _commit(self, member, kind, amount, req=None, due=None):
        return self.gov.record_commitment(
            request_id=req or f"com-{member}-{kind}-{amount}-{id(self)}",
            actor_id="sec-1", member_id=member, kind=kind, amount=amount,
            unit="unit", due_at=due or at(days=10))

    def _pay(self, member, kind, amount, req=None):
        return self.gov.record_contribution(
            request_id=req or f"pay-{member}-{kind}-{amount}-{id(self)}",
            actor_id="sec-1", member_id=member, kind=kind, amount=amount)

    def _dataset(self, dataset_id="ds-1", sensitivity="restricted"):
        self.gov.register_dataset(request_id=f"reg-{dataset_id}", actor_id="sec-1",
                                  dataset_id=dataset_id, title="观测数据集")
        return self.gov.publish_version(request_id=f"pub-{dataset_id}-v1", actor_id="sec-1",
                                        dataset_id=dataset_id, sensitivity=sensitivity)

    def _fullfill(self, member):
        self._commit(member, "funding", 100, req=f"com-{member}-full")
        self._pay(member, "funding", 100, req=f"pay-{member}-full")

    def _proposal(self, rep, dataset_id="ds-1", req=None, **kwargs):
        return self.gov.submit_proposal(
            request_id=req or f"prop-{rep}-{id(self)}", actor_id=rep,
            dataset_id=dataset_id, purpose="科学研究", **kwargs)

    def _close_after_votes(self, res_id, votes, req_prefix):
        for index, (rep, choice) in enumerate(votes):
            self.gov.cast_vote(request_id=f"{req_prefix}-v{index}", actor_id=rep,
                               resolution_id=res_id, choice=choice)
        self.clock.advance(days=2)
        return self.gov.close_resolution(request_id=f"{req_prefix}-close", actor_id="sec-1",
                                         resolution_id=res_id)

    # ---- 章程 -----------------------------------------------------------

    def test_first_charter_effective_second_draft(self):
        self.assertEqual("effective", self.charter["status"])
        second = self.gov.create_charter(request_id="charter-2", actor_id="sec-1",
                                         rules={**RULES, "access_min_fulfillment": 0.6})
        self.assertEqual("draft", second["status"])
        self.assertEqual(2, second["version"])

    def test_charter_rules_validated(self):
        with self.assertRaises(ValidationError):
            self.gov.create_charter(request_id="bad-charter", actor_id="sec-1",
                                    rules={"quorum_fraction": 2})

    def test_charter_amendment_only_forward(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-old")
        self.assertEqual("approved", proposal["status"])
        second = self.gov.create_charter(request_id="charter-2", actor_id="sec-1",
                                         rules={**RULES, "access_min_fulfillment": 0.9})
        res = self.gov.open_resolution(
            request_id="res-amend", actor_id="sec-1", kind="amend_charter",
            subject={"charter_id": second["charter_id"]}, closes_at=at(days=1))
        closed = self._close_after_votes(
            res["resolution_id"], [("rep-a", "yes"), ("rep-b", "yes"), ("rep-c", "yes")],
            "amend")
        self.assertEqual("passed", closed["status"])
        old = self.gov.get_proposal(actor_id="sec-1", proposal_id=proposal["proposal_id"])
        self.assertEqual(1, old["charter_version"])
        self.assertEqual("approved", old["status"])
        license_view = self.gov.get_license(actor_id="sec-1", license_id=proposal["license_id"])
        self.assertEqual("active", license_view["status"])

    # ---- 提案与访问决定 ---------------------------------------------------

    def test_proposal_approved_grants_license_and_explanation(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        self.assertEqual("approved", proposal["status"])
        self.assertIsNotNone(proposal["license_id"])
        explain = self.gov.explain_access(actor_id="rep-a", member_id="ma", dataset_id="ds-1")
        self.assertTrue(explain["allowed"])
        self.assertTrue(any("授予" in reason for reason in explain["reasons"]))

    def test_embargo_denies_under_fulfilled_member(self):
        self._commit("mb", "funding", 100, req="com-b")
        self._pay("mb", "funding", 40, req="pay-b")
        self._dataset()
        proposal = self._proposal("rep-b", req="prop-b")
        self.assertEqual("denied", proposal["status"])
        rules = [check["rule"] for check in proposal["decision"]["checks"]
                 if not check["passed"]]
        self.assertIn("fulfillment_threshold", rules)
        self.assertIn("embargo_window", rules)
        explain = self.gov.explain_access(actor_id="rep-b", member_id="mb", dataset_id="ds-1")
        self.assertFalse(explain["allowed"])
        self.assertTrue(any("拒绝" in reason for reason in explain["reasons"]))

    def test_fully_paid_member_accesses_during_embargo(self):
        self._fullfill("ma")
        version = self._dataset()
        self.assertLess(T0.isoformat(), version["embargo_until"])
        proposal = self._proposal("rep-a", req="prop-a")
        self.assertEqual("approved", proposal["status"])

    def test_late_contribution_not_retroactive(self):
        self._commit("mb", "funding", 100, req="com-b")
        self._pay("mb", "funding", 40, req="pay-b-1")
        self._dataset()
        denied = self._proposal("rep-b", req="prop-b-1")
        self.assertEqual("denied", denied["status"])
        self.clock.advance(days=12)
        late = self._pay("mb", "funding", 60, req="pay-b-2")
        self.assertTrue(late["late"])
        old = self.gov.get_proposal(actor_id="sec-1", proposal_id=denied["proposal_id"])
        self.assertEqual("denied", old["status"])
        self.assertEqual(0.4, old["decision"]["snapshot"]["fulfillment_ratio"])
        approved = self._proposal("rep-b", req="prop-b-2")
        self.assertEqual("approved", approved["status"])

    def test_third_party_transfer_gated_by_resolution(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        self.assertEqual("pending", proposal["status"])
        self.assertIn("第三方转交需治理决议批准", proposal["decision"]["gates"])
        res = self.gov.open_resolution(
            request_id="res-tr", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        closed = self._close_after_votes(
            res["resolution_id"], [("rep-a", "yes"), ("rep-b", "yes"), ("rep-c", "yes")], "tr")
        self.assertEqual("passed", closed["status"])
        license_view = self.gov.get_license(actor_id="sec-1",
                                            license_id=closed["effect"]["license_id"])
        self.assertEqual("read+transfer", license_view["scope"])

    def test_transfer_prohibited_by_charter(self):
        # 单独服务验证 prohibited 策略直接拒绝转交提案。
        database = Database()
        clock = MutableClock(T0)
        foundation = DomainService(database, clock)
        gov = GovernanceService(database, clock)
        foundation.register_organization(request_id="org-sec-x", actor_id="bootstrap",
                                         organization_id="sec", name="秘书处")
        foundation.register_actor(request_id="actor-admin-x", actor_id="bootstrap",
                                  new_actor_id="admin-x", display_name="管理员",
                                  role="admin", organization_id="sec")
        foundation.register_actor(request_id="actor-sec-x", actor_id="admin-x",
                                  new_actor_id="sec-x", display_name="干事",
                                  role="secretariat", organization_id="sec")
        foundation.register_organization(request_id="org-x2", actor_id="admin-x",
                                         organization_id="org-x", name="机构")
        foundation.register_actor(request_id="actor-x2", actor_id="admin-x", new_actor_id="rep-x",
                                  display_name="代表", role="representative",
                                  organization_id="org-x")
        gov.create_charter(request_id="charter-x", actor_id="sec-x",
                           rules={**RULES, "third_party_transfer": "prohibited"})
        gov.register_member(request_id="mem-x", actor_id="sec-x", member_id="mx", name="某国",
                            kind="country", organization_id="org-x")
        gov.register_dataset(request_id="ds-x", actor_id="sec-x", dataset_id="dx", title="数据")
        gov.publish_version(request_id="dsv-x", actor_id="sec-x", dataset_id="dx",
                            sensitivity="internal")
        proposal = gov.submit_proposal(request_id="prop-x", actor_id="rep-x", dataset_id="dx",
                                       purpose="分析", third_party_transfer=True)
        self.assertEqual("denied", proposal["status"])
        self.assertIn("章程禁止向第三方转交", proposal["decision"]["reasons"])
        database.close()

    def test_sensitive_dataset_requires_resolution(self):
        self._fullfill("ma")
        self._dataset(sensitivity="sensitive")
        proposal = self._proposal("rep-a", req="prop-a")
        self.assertEqual("pending", proposal["status"])
        res = self.gov.open_resolution(
            request_id="res-sens", actor_id="sec-1", kind="approve_proposal",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        closed = self._close_after_votes(
            res["resolution_id"], [("rep-a", "yes"), ("rep-b", "yes"), ("rep-c", "yes")],
            "sens")
        self.assertEqual("passed", closed["status"])
        updated = self.gov.get_proposal(actor_id="sec-1", proposal_id=proposal["proposal_id"])
        self.assertEqual("approved", updated["status"])
        self.assertIsNotNone(updated["license_id"])

    def test_second_license_denied_while_active(self):
        self._fullfill("ma")
        self._dataset()
        first = self._proposal("rep-a", req="prop-1")
        self.assertEqual("approved", first["status"])
        second = self._proposal("rep-a", req="prop-2")
        self.assertEqual("denied", second["status"])
        self.assertIn("已存在有效许可，不能重复授权", second["decision"]["reasons"])
        with self.assertRaises(Exception):
            self.database.connection.execute(
                "INSERT INTO licenses(license_id,proposal_id,member_id,dataset_id,"
                "dataset_version,scope,status,revision,granted_at,expires_at,updated_at) "
                "VALUES('x','p','ma','ds-1',1,'read','active',1,'g','e','u')")

    def test_proposal_replay_returns_same_receipt(self):
        self._fullfill("ma")
        self._dataset()
        first = self._proposal("rep-a", req="prop-same")
        replay = self._proposal("rep-a", req="prop-same")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["proposal_id"], replay["proposal_id"])
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM proposals").fetchone()["c"]
        self.assertEqual(1, count)

    # ---- 决议与表决 -------------------------------------------------------

    def test_conflicted_representative_recused(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        self.gov.declare_conflict(request_id="conf", actor_id="rep-c",
                                  dataset_id="ds-1", reason="资助关系")
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        vote = self.gov.cast_vote(request_id="vote-c", actor_id="rep-c",
                                  resolution_id=res["resolution_id"], choice="yes")
        self.assertTrue(vote["recused"])
        closed = self._close_after_votes(
            res["resolution_id"], [("rep-a", "yes"), ("rep-b", "yes")], "close")
        self.assertEqual(3.0, closed["tally"]["participation_weight"])
        self.assertEqual(["mc"], closed["tally"]["recused_members"])

    def test_double_vote_not_double_counted(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        first = self.gov.cast_vote(request_id="v1", actor_id="rep-a",
                                   resolution_id=res["resolution_id"], choice="yes")
        again = self.gov.cast_vote(request_id="v2", actor_id="rep-a",
                                   resolution_id=res["resolution_id"], choice="yes")
        self.assertTrue(first["counted"])
        self.assertFalse(again["counted"])
        with self.assertRaises(ConflictError):
            self.gov.cast_vote(request_id="v3", actor_id="rep-a",
                               resolution_id=res["resolution_id"], choice="no")
        closed = self._close_after_votes(
            res["resolution_id"], [("rep-b", "yes"), ("rep-c", "yes")], "tally")
        self.assertEqual(4.0, closed["tally"]["yes_weight"])
        self.assertEqual(4.0, closed["tally"]["participation_weight"])

    def test_weight_change_does_not_affect_open_resolution(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        self.gov.update_weight(request_id="w-x", actor_id="sec-1", member_id="ma",
                               voting_weight=10.0)
        closed = self._close_after_votes(
            res["resolution_id"],
            [("rep-a", "yes"), ("rep-b", "no"), ("rep-c", "no")], "w")
        self.assertEqual(2.0, closed["tally"]["yes_weight"])
        self.assertEqual(2.0, closed["tally"]["no_weight"])
        self.assertEqual("failed", closed["status"])

    def test_quorum_failure_blocks_effect(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="suspend_license",
            subject={"license_id": license_id}, closes_at=at(days=1))
        closed = self._close_after_votes(res["resolution_id"], [("rep-c", "yes")], "q")
        self.assertEqual("failed", closed["status"])
        self.assertFalse(closed["tally"]["quorum_ok"])
        license_view = self.gov.get_license(actor_id="sec-1", license_id=license_id)
        self.assertEqual("active", license_view["status"])

    def test_close_before_deadline_requires_all_votes(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        self.gov.cast_vote(request_id="v1", actor_id="rep-a",
                           resolution_id=res["resolution_id"], choice="yes")
        with self.assertRaises(ConflictError):
            self.gov.close_resolution(request_id="early", actor_id="sec-1",
                                      resolution_id=res["resolution_id"])
        self.gov.cast_vote(request_id="v2", actor_id="rep-b",
                           resolution_id=res["resolution_id"], choice="yes")
        self.gov.cast_vote(request_id="v3", actor_id="rep-c",
                           resolution_id=res["resolution_id"], choice="yes")
        closed = self.gov.close_resolution(request_id="all-voted", actor_id="sec-1",
                                           resolution_id=res["resolution_id"])
        self.assertEqual("passed", closed["status"])

    def test_vote_after_deadline_rejected(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        self.clock.advance(days=2)
        with self.assertRaises(ConflictError):
            self.gov.cast_vote(request_id="late-vote", actor_id="rep-a",
                               resolution_id=res["resolution_id"], choice="yes")

    def test_withdrawn_member_cannot_vote(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        self.gov.withdraw_member(request_id="wd", actor_id="sec-1", member_id="ma")
        with self.assertRaises(PermissionDenied):
            self.gov.cast_vote(request_id="v", actor_id="rep-a",
                               resolution_id=res["resolution_id"], choice="yes")

    # ---- 许可、下载与更正 ---------------------------------------------------

    def test_download_callback_deduped(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        first = self.gov.record_download(request_id="d1", actor_id="rep-a",
                                         callback_id="cb-1", license_id=license_id,
                                         byte_count=100)
        same = self.gov.record_download(request_id="d2", actor_id="rep-a",
                                        callback_id="cb-1", license_id=license_id,
                                        byte_count=100)
        replay = self.gov.record_download(request_id="d1", actor_id="rep-a",
                                          callback_id="cb-1", license_id=license_id,
                                          byte_count=100)
        self.assertTrue(first["counted"])
        self.assertFalse(same["counted"])
        self.assertTrue(replay["replayed"])
        usage = self.gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        self.assertEqual(1, usage["downloads"]["count"])

    def test_suspension_blocks_download_and_resume_restores(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        self.gov.record_download(request_id="d1", actor_id="rep-a",
                                 callback_id="cb-1", license_id=license_id, byte_count=10)
        self.gov.suspend_license(request_id="sus", actor_id="sec-1",
                                 license_id=license_id, reason="数据质量核查")
        with self.assertRaises(PermissionDenied):
            self.gov.record_download(request_id="d2", actor_id="rep-a",
                                     callback_id="cb-2", license_id=license_id)
        self.gov.resume_license(request_id="res", actor_id="sec-1", license_id=license_id)
        again = self.gov.record_download(request_id="d3", actor_id="rep-a",
                                         callback_id="cb-3", license_id=license_id)
        self.assertTrue(again["counted"])
        usage = self.gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        self.assertEqual(2, usage["downloads"]["count"])

    def test_license_expiry_blocks_download(self):
        self.gov.create_charter(request_id="charter-short", actor_id="sec-1",
                                rules={**RULES, "license_days": 5})
        res = self.gov.open_resolution(
            request_id="res-amend", actor_id="sec-1", kind="amend_charter",
            subject={"charter_id": self.database.connection.execute(
                "SELECT charter_id FROM charter_versions WHERE version=2").fetchone()[0]},
            closes_at=at(days=1))
        self._close_after_votes(res["resolution_id"],
                                [("rep-a", "yes"), ("rep-b", "yes"), ("rep-c", "yes")], "a")
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        self.clock.advance(days=6)
        with self.assertRaises(PermissionDenied):
            self.gov.record_download(request_id="d1", actor_id="rep-a",
                                     callback_id="cb-1", license_id=license_id)
        explain = self.gov.explain_access(actor_id="sec-1", member_id="ma", dataset_id="ds-1")
        self.assertFalse(explain["allowed"])
        self.assertTrue(any("到期" in reason for reason in explain["reasons"]))

    def test_correction_supersedes_and_downloads_use_new_version(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        corrected = self.gov.correct_dataset(request_id="fix", actor_id="sec-1",
                                             dataset_id="ds-1", note="校准修正")
        self.assertEqual(2, corrected["version"])
        download = self.gov.record_download(request_id="d1", actor_id="rep-a",
                                            callback_id="cb-1", license_id=license_id)
        self.assertEqual(2, download["dataset_version"])
        usage = self.gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        statuses = {item["version"]: item["status"] for item in usage["versions"]}
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("active", statuses[2])

    def test_suspended_version_blocks_download(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        self.gov.suspend_version(request_id="sv", actor_id="sec-1", dataset_id="ds-1",
                                 version=1, reason="污染数据待核")
        with self.assertRaises(ConflictError):
            self.gov.record_download(request_id="d1", actor_id="rep-a",
                                     callback_id="cb-1", license_id=proposal["license_id"])

    def test_withdrawal_revokes_license_and_keeps_history(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        license_id = proposal["license_id"]
        self.gov.record_download(request_id="d1", actor_id="rep-a",
                                 callback_id="cb-1", license_id=license_id, byte_count=10)
        result = self.gov.withdraw_member(request_id="wd", actor_id="sec-1", member_id="ma")
        self.assertIn(license_id, result["revoked_licenses"])
        with self.assertRaises(PermissionDenied):
            self._proposal("rep-a", req="prop-a-2")
        explain = self.gov.explain_access(actor_id="sec-1", member_id="ma", dataset_id="ds-1")
        self.assertFalse(explain["allowed"])
        self.assertTrue(any("撤销" in reason for reason in explain["reasons"]))
        usage = self.gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        self.assertEqual(1, usage["downloads"]["count"])

    # ---- 署名与成果 -------------------------------------------------------

    def test_publication_attribution_snapshot(self):
        self._commit("ma", "funding", 100, req="com-a")
        self._pay("ma", "funding", 100, req="pay-a")
        self._commit("mb", "funding", 100, req="com-b")
        self._pay("mb", "funding", 100, req="pay-b")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        pub = self.gov.register_publication(request_id="pub-1", actor_id="rep-a",
                                            license_id=proposal["license_id"],
                                            title="归因研究")
        shares = {item["member_id"]: item for item in pub["attributions"]}
        self.assertEqual(0.5, shares["ma"]["share"])
        self.assertEqual(0.5, shares["mb"]["share"])
        self.assertEqual(0.0, shares["mc"]["share"])
        self.assertTrue(shares["ma"]["eligible"])
        self.assertFalse(shares["mc"]["eligible"])
        self._pay("mc", "funding", 1000, req="pay-c-late")
        stored = self.gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        again = {item["member_id"]: item
                 for item in stored["publications"][0]["attributions"]}
        self.assertEqual(0.0, again["mc"]["share"])

    def test_publication_rejected_for_revoked_license(self):
        self._fullfill("ma")
        self._dataset()
        proposal = self._proposal("rep-a", req="prop-a")
        self.gov.withdraw_member(request_id="wd", actor_id="sec-1", member_id="ma")
        with self.assertRaises(PermissionDenied):
            self.gov.register_publication(request_id="pub", actor_id="sec-1",
                                          license_id=proposal["license_id"], title="成果")

    # ---- 承诺与实缴追溯 ----------------------------------------------------

    def test_unfulfilled_commitment_and_late_payment(self):
        self._commit("mb", "funding", 100, req="com-b")
        self.clock.advance(days=11)
        standing = self.gov.member_standing(actor_id="sec-1", member_id="mb")
        self.assertEqual(1, len(standing["unfulfilled_commitments"]))
        self.assertEqual("unfulfilled", standing["commitments"][0]["status"])
        late = self._pay("mb", "funding", 100, req="pay-b")
        self.assertTrue(late["late"])
        standing = self.gov.member_standing(actor_id="sec-1", member_id="mb")
        self.assertEqual([], standing["unfulfilled_commitments"])
        self.assertEqual(1.0, standing["fulfillment_ratio"])
        self.assertTrue(standing["commitments"][0]["has_late_contribution"])

    def test_instrument_slot_completion_counts_contribution(self):
        self._commit("ma", "instrument_time", 10, req="com-a-inst")
        self.gov.register_instrument(request_id="ins", actor_id="sec-1",
                                     instrument_id="ins-1", name="观测仪")
        slot = self.gov.schedule_slot(request_id="slot", actor_id="sec-1",
                                      instrument_id="ins-1", member_id="ma",
                                      starts_at=at(hours=1), ends_at=at(hours=11))
        done = self.gov.complete_slot(request_id="done", actor_id="sec-1",
                                      slot_id=slot["slot_id"])
        self.assertEqual(10, done["hours"])
        standing = self.gov.member_standing(actor_id="sec-1", member_id="ma")
        self.assertEqual(10, standing["kinds"]["instrument_time"]["contributed"])
        with self.assertRaises(ConflictError):
            self.gov.complete_slot(request_id="done-2", actor_id="sec-1",
                                   slot_id=slot["slot_id"])

    def test_instrument_slot_overlap_rejected(self):
        self.gov.register_instrument(request_id="ins", actor_id="sec-1",
                                     instrument_id="ins-1", name="观测仪")
        self.gov.schedule_slot(request_id="s1", actor_id="sec-1", instrument_id="ins-1",
                               member_id="ma", starts_at=at(hours=1), ends_at=at(hours=5))
        with self.assertRaises(ConflictError):
            self.gov.schedule_slot(request_id="s2", actor_id="sec-1", instrument_id="ins-1",
                                   member_id="mb", starts_at=at(hours=4), ends_at=at(hours=8))
        ok = self.gov.schedule_slot(request_id="s3", actor_id="sec-1", instrument_id="ins-1",
                                    member_id="mb", starts_at=at(hours=5), ends_at=at(hours=8))
        self.assertFalse(ok["replayed"])

    # ---- 权限 --------------------------------------------------------------

    def test_representative_cannot_manage_charter(self):
        with self.assertRaises(PermissionDenied):
            self.gov.create_charter(request_id="x", actor_id="rep-a", rules=RULES)

    def test_secretariat_cannot_vote(self):
        self._dataset()
        proposal = self._proposal("rep-c", req="prop-c", third_party_transfer=True)
        res = self.gov.open_resolution(
            request_id="res", actor_id="sec-1", kind="approve_transfer",
            subject={"proposal_id": proposal["proposal_id"]}, closes_at=at(days=1))
        with self.assertRaises(PermissionDenied):
            self.gov.cast_vote(request_id="v", actor_id="sec-1",
                               resolution_id=res["resolution_id"], choice="yes")

    def test_representative_cannot_view_other_member(self):
        self._fullfill("ma")
        self._dataset()
        self._proposal("rep-a", req="prop-a")
        with self.assertRaises(PermissionDenied):
            self.gov.explain_access(actor_id="rep-b", member_id="ma", dataset_id="ds-1")

    def test_proposal_requires_effective_charter(self):
        database = Database()
        clock = MutableClock(T0)
        foundation = DomainService(database, clock)
        gov = GovernanceService(database, clock)
        foundation.register_organization(request_id="org-sec-x", actor_id="bootstrap",
                                         organization_id="sec", name="秘书处")
        foundation.register_actor(request_id="actor-admin-x", actor_id="bootstrap",
                                  new_actor_id="admin-x", display_name="管理员",
                                  role="admin", organization_id="sec")
        foundation.register_actor(request_id="actor-sec-x", actor_id="admin-x",
                                  new_actor_id="sec-x", display_name="干事",
                                  role="secretariat", organization_id="sec")
        foundation.register_organization(request_id="org-x2", actor_id="admin-x",
                                         organization_id="org-x", name="机构")
        foundation.register_actor(request_id="actor-x2", actor_id="admin-x", new_actor_id="rep-x",
                                  display_name="代表", role="representative",
                                  organization_id="org-x")
        gov.register_member(request_id="mem-x", actor_id="sec-x", member_id="mx", name="某国",
                            kind="country", organization_id="org-x")
        gov.register_dataset(request_id="ds-x", actor_id="sec-x", dataset_id="dx", title="数据")
        gov.publish_version(request_id="dsv-x", actor_id="sec-x", dataset_id="dx",
                            sensitivity="internal")
        with self.assertRaises(ValidationError):
            gov.submit_proposal(request_id="prop-x", actor_id="rep-x", dataset_id="dx",
                                purpose="分析")
        database.close()


if __name__ == "__main__":
    unittest.main()

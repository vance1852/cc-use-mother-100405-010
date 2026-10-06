"""治理领域核心不变量测试。"""

import unittest
from datetime import datetime, timezone

from science_strategy_foundation.errors import ConflictError, PermissionDenied, ValidationError
from science_strategy_foundation.service import DomainService

from _gov_helpers import GovScenario


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov
        self.clock = self.ctx.clock

    def tearDown(self):
        self.ctx.close()

    def _eligibility(self, proposal_id="prop1"):
        detail = self.gov.proposal_detail(proposal_id)
        return {m["member_id"]: m for m in detail["snapshot"]["members"]}

    def test_eligibility_computed_from_contributions_at_submission(self):
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.commitment("c3", "rep3", "m3", 100, kind="calibration")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 60, "2026-08-01T00:00:00Z")  # 60% 达标
        # m3 分文未缴
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        members = self._eligibility()
        self.assertTrue(members["m1"]["eligible"])
        self.assertTrue(members["m2"]["eligible"])
        self.assertFalse(members["m3"]["eligible"])
        self.assertIn("fulfillment_below_threshold", members["m3"]["ineligible_reasons"])

    def test_late_contribution_does_not_change_existing_snapshot(self):
        self.ctx.commitment("c3", "rep3", "m3", 100, kind="calibration")
        self.ctx.dataset()
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        before = self._eligibility()["m3"]
        self.assertFalse(before["eligible"])
        # 迟交发生在提案之后
        self.clock.set(datetime(2026, 12, 15, tzinfo=timezone.utc))
        self.ctx.pay("p3late", "rep3", "c3", 100, "2026-12-10T00:00:00Z")
        after = self._eligibility()["m3"]
        self.assertEqual(before, after)

    def test_weight_change_after_submission_does_not_alter_resolution(self):
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        # 甲国(权重2)赞成、乙国(权重1)反对：3 票中赞成权重 2，2/3 通过
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="no")
        # 秘书处事后把甲国权重改为 100，不影响定影结果
        self.gov.set_member_weight(request_id="w1", actor_id="sec1", member_id="m1", weight=100)
        receipt = self.gov.resolve_proposal(request_id="res1", actor_id="sec1", proposal_id="prop1")
        detail = self.gov.proposal_detail("prop1")
        self.assertEqual("approved", detail["resolution"]["outcome"])
        self.assertEqual(2.0, detail["resolution"]["yes_weight"])
        self.assertTrue(receipt.resource_id)

    def test_applicant_below_threshold_cannot_submit(self):
        # m2 实缴 40%，低于 50% 门槛：自己提交提案被拒
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p2", "rep2", "c2", 40, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        with self.assertRaises(PermissionDenied) as caught:
            self.gov.submit_proposal(request_id="prop-bad", actor_id="rep2",
                                     proposal_id="prop-bad", dataset_id="obs-A",
                                     dataset_version=1, purpose="研究")
        self.assertIn("fulfillment_below_threshold", str(caught.exception))

    def test_new_baseline_does_not_retroactively_change_pending_proposal(self):        # 甲国实缴 100%，乙国 60%：在阈值 0.5 的旧章程下都合格
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 60, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        # 之后新章程把门槛提高到 0.8
        self.clock.set(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.gov.register_baseline(
            request_id="charter-v2", actor_id="sec1", baseline_id="charter", version=2,
            payload={"voting": {"quorum_ratio": 0.5, "approval_ratio": 0.66},
                     "eligibility": {"min_fulfillment_ratio": 0.8},
                     "data": {"allow_third_party_transfer": True}})
        members = self._eligibility()
        self.assertTrue(members["m2"]["eligible"])  # 仍按提交时的 v1
        self.assertEqual("charter:1",
                         self.gov.proposal_detail("prop1")["baseline"])


class VotingTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov
        for cid, rep, mid, kind, paid in (
                ("c1", "rep1", "m1", "funding", 100),
                ("c2", "rep2", "m2", "instrument_time", 100),
                ("c3", "rep3", "m3", "calibration", 0)):
            self.ctx.commitment(cid, rep, mid, 100, kind=kind)
            if paid:
                self.ctx.pay(f"p-{cid}", rep, cid, paid, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")

    def tearDown(self):
        self.ctx.close()

    def test_duplicate_ballot_not_counted(self):
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        with self.assertRaises(ConflictError):
            self.gov.cast_ballot(request_id="b1dup", actor_id="rep1", proposal_id="prop1",
                                 vote="no")

    def test_ineligible_member_cannot_vote(self):
        with self.assertRaises(PermissionDenied) as caught:
            self.gov.cast_ballot(request_id="b3", actor_id="rep3", proposal_id="prop1",
                                 vote="yes")
        self.assertIn("fulfillment_below_threshold", str(caught.exception))

    def test_conflicted_representative_must_recuse(self):
        # m2 在提案提交后才与数据拥有方 m1 产生利益冲突，投票时刻仍须回避
        conn = self.ctx.database.connection
        conn.execute("UPDATE gov_members SET conflict_orgs='[\"m1\"]' WHERE member_id='m2'")
        with self.assertRaises(PermissionDenied):
            self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1",
                                 vote="yes")

    def test_quorum_failure_rejects(self):
        # 合格权重为 m1(2)+m2(1)=3，仅 m1 投票：cast 2/3 < 0.5？2/3>=0.5 法定人数满足。
        # 改为只有 m2(1) 投票：1/3 < 0.5 法定人数不足
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="yes")
        self.gov.resolve_proposal(request_id="res1", actor_id="sec1", proposal_id="prop1")
        detail = self.gov.proposal_detail("prop1")
        self.assertEqual("rejected", detail["resolution"]["outcome"])
        self.assertFalse(detail["resolution"]["quorum_met"])

    def test_abstention_counts_toward_quorum_but_not_approval(self):
        # m1 赞成(2)，m2 弃权(1)：法定人数 3/3 满足，赞成比例 2/2 通过
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="abstain")
        self.gov.resolve_proposal(request_id="res1", actor_id="sec1", proposal_id="prop1")
        detail = self.gov.proposal_detail("prop1")
        self.assertTrue(detail["resolution"]["quorum_met"])
        self.assertEqual(1.0, detail["resolution"]["abstain_weight"])
        self.assertEqual("approved", detail["resolution"]["outcome"])

    def test_resolution_is_idempotent_and_cannot_be_redecided(self):
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="yes")
        first = self.gov.resolve_proposal(request_id="res1", actor_id="sec1", proposal_id="prop1")
        replay = self.gov.resolve_proposal(request_id="res1", actor_id="sec1", proposal_id="prop1")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        # 决议后再投票被拒绝
        with self.assertRaises(ConflictError):
            self.gov.cast_ballot(request_id="b-late", actor_id="rep1", proposal_id="prop1",
                                 vote="no")
        # 但同一投票请求在决议后因网络重试重投：回放原回执而不是报错
        replay_ballot = self.gov.cast_ballot(request_id="b1", actor_id="rep1",
                                             proposal_id="prop1", vote="yes")
        self.assertTrue(replay_ballot.replayed)


class GrantLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov
        self.clock = self.ctx.clock
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="yes")
        self.resolution = self.gov.resolve_proposal(
            request_id="res1", actor_id="sec1", proposal_id="prop1")

    def tearDown(self):
        self.ctx.close()

    def test_embargo_blocks_non_producer_but_allows_producer(self):
        # m2 是登记的产出方，禁运期内可访问
        explanation = self.gov.explain_access("m2", "obs-A")
        self.assertTrue(explanation["allowed"])
        embargo_gate = next(g for g in explanation["gates"] if g["gate"] == "embargo_window")
        self.assertTrue(embargo_gate["passed"])
        self.assertFalse(embargo_gate["detail"]["lifted"])

    def test_embargo_blocks_after_it_lifts_for_everyone_via_time(self):
        # 一个未登记为产出方的成员 m1 是 owner（自动算产出方）。
        # 构造新数据集，m3 非产出方：先批准再跨禁运
        self.ctx.commitment("c3", "rep3", "m3", 100, kind="calibration")
        self.ctx.pay("p3", "rep3", "c3", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(1, dataset_id="obs-B", producers=["m1"],
                         embargo_until="2026-12-01T00:00:00Z")
        self.gov.submit_proposal(request_id="propB", actor_id="rep3", proposal_id="propB",
                                 dataset_id="obs-B", dataset_version=1, purpose="研究")
        self.gov.cast_ballot(request_id="bb1", actor_id="rep1", proposal_id="propB", vote="yes")
        self.gov.cast_ballot(request_id="bb2", actor_id="rep2", proposal_id="propB", vote="yes")
        self.gov.cast_ballot(request_id="bb3", actor_id="rep3", proposal_id="propB", vote="yes")
        self.gov.resolve_proposal(request_id="resB", actor_id="sec1", proposal_id="propB")
        during = self.gov.explain_access("m3", "obs-B")
        self.assertFalse(during["allowed"])
        self.assertEqual("denied_embargo_window", during["reason"])
        self.clock.set(datetime(2027, 1, 1, tzinfo=timezone.utc))
        after = self.gov.explain_access("m3", "obs-B")
        self.assertTrue(after["allowed"])

    def test_suspend_and_resume_are_subsequent_events(self):
        self.gov.suspend_grant(request_id="s1", actor_id="sec1", member_id="m2",
                               dataset_id="obs-A", reason="调查")
        suspended = self.gov.explain_access("m2", "obs-A")
        self.assertFalse(suspended["allowed"])
        self.assertEqual("terminated_grant_suspended", suspended["reason"])
        self.gov.resume_grant(request_id="r1", actor_id="sec1", member_id="m2",
                              dataset_id="obs-A", reason="解除")
        self.assertTrue(self.gov.explain_access("m2", "obs-A")["allowed"])
        timeline = self.gov.grant_timeline("m2", "obs-A")
        events = [row["event"] for row in timeline]
        self.assertEqual(["granted", "suspended", "resumed"], events)

    def test_withdrawal_revokes_grant_without_editing_history(self):
        granted_rows = [r for r in self.gov.grant_timeline("m2", "obs-A")
                        if r["event"] == "granted"]
        self.assertEqual(1, len(granted_rows))
        self.gov.withdraw_member(request_id="w1", actor_id="sec1", member_id="m2",
                                 reason="退出")
        explanation = self.gov.explain_access("m2", "obs-A")
        self.assertFalse(explanation["allowed"])
        self.assertEqual("terminated_member_withdrawal", explanation["reason"])
        # 历史行仍然存在且未被删除
        events = [r["event"] for r in self.gov.grant_timeline("m2", "obs-A")]
        self.assertIn("granted", events)
        self.assertIn("revoked_withdrawal", events)

    def test_data_correction_supersedes_only_old_version_grant(self):
        self.ctx.dataset(2, producers=["m1", "m2"], corrected_of=1,
                         embargo_until="2026-09-01T00:00:00Z")
        explanation = self.gov.explain_access("m2", "obs-A")
        # 最新许可事件指向 v1 且为 superseded
        self.assertEqual(1, explanation["evidence"]["latest_grant_event"]["dataset_version"])
        self.assertEqual("superseded", explanation["evidence"]["latest_grant_event"]["event"])
        self.assertFalse(explanation["allowed"])

    def test_only_one_active_grant_version_per_member_dataset(self):
        # m2 已对 obs-A 有有效许可；再提一个针对同数据集的提案必须冲突
        self.gov.submit_proposal(request_id="prop2", actor_id="rep2", proposal_id="prop2",
                                 dataset_id="obs-A", dataset_version=1, purpose="另一研究")
        self.gov.cast_ballot(request_id="b21", actor_id="rep1", proposal_id="prop2", vote="yes")
        self.gov.cast_ballot(request_id="b22", actor_id="rep2", proposal_id="prop2", vote="yes")
        with self.assertRaises(ConflictError):
            self.gov.resolve_proposal(request_id="res2", actor_id="sec1", proposal_id="prop2")


class CallbackAndPublicationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov
        self.ctx.commitment("c1", "rep1", "m1", 100)
        self.ctx.commitment("c2", "rep2", "m2", 100, kind="instrument_time")
        self.ctx.pay("p1", "rep1", "c1", 100, "2026-08-01T00:00:00Z")
        self.ctx.pay("p2", "rep2", "c2", 100, "2026-08-01T00:00:00Z")
        self.ctx.dataset(producers=["m1", "m2"])
        self.gov.submit_proposal(request_id="prop1", actor_id="rep2", proposal_id="prop1",
                                 dataset_id="obs-A", dataset_version=1, purpose="研究")
        self.gov.cast_ballot(request_id="b1", actor_id="rep1", proposal_id="prop1", vote="yes")
        self.gov.cast_ballot(request_id="b2", actor_id="rep2", proposal_id="prop1", vote="yes")
        self.gov.resolve_proposal(request_id="res1", actor_id="sec1",
                                  proposal_id="prop1")
        # 许可编号从许可账本取得（决议回执的 resource_id 是决议编号）
        timeline = self.gov.grant_timeline("m2", "obs-A")
        self.grant_id = timeline[-1]["grant_id"]
        self.assertEqual("granted", timeline[-1]["event"])

    def tearDown(self):
        self.ctx.close()

    def test_duplicate_download_callback_counted_once(self):
        self.gov.register_download_callback(
            request_id="cb1", actor_id="sec1", callback_id="cb-1", grant_id=self.grant_id)
        with self.assertRaises(ConflictError):
            self.gov.register_download_callback(
                request_id="cb2", actor_id="sec1", callback_id="cb-1",
                grant_id=self.grant_id)
        self.assertEqual(1, len(self.gov.list_downloads()))

    def test_publication_authorship_ordered_by_snapshot_contribution(self):
        self.gov.record_publication(
            request_id="pub1", actor_id="sec1", publication_id="pub-1",
            grant_id=self.grant_id, title="成果论文")
        publications = self.gov.list_publications(self.grant_id)
        self.assertEqual(1, len(publications))
        author_ids = [author["member_id"] for author in publications[0]["authorship"]]
        # m1 实缴 100 排第一，m2 实缴 100 同为满分（member_id 次序），m3 无实缴不署名
        self.assertEqual("m1", author_ids[0])
        self.assertIn("m2", author_ids)
        self.assertNotIn("m3", author_ids)


class ExplainAndStaffTest(unittest.TestCase):
    def setUp(self):
        self.ctx = GovScenario()
        self.gov = self.ctx.gov

    def tearDown(self):
        self.ctx.close()

    def test_partner_can_only_explain_own_access(self):
        with self.assertRaises(PermissionDenied):
            self.gov.explain_access("m1", "obs-A", actor_id="rep2")
        # 秘书处可查任意成员
        self.ctx.dataset(producers=["m1"])
        explanation = self.gov.explain_access("m1", "obs-A", actor_id="sec1")
        self.assertIn("gates", explanation)

    def test_unfulfilled_commitments_visible_only_to_secretariat(self):
        with self.assertRaises(PermissionDenied):
            self.gov.unfulfilled_commitments(actor_id="rep1")
        self.ctx.commitment("c1", "rep1", "m1", 100)
        items = self.gov.unfulfilled_commitments(actor_id="sec1")
        self.assertEqual(1, len(items))
        self.assertEqual(100.0, items[0]["shortfall"])

    def test_third_party_transfer_blocked_when_baseline_forbids(self):
        # 新建一个禁止转交的章程系列
        self.gov.register_baseline(
            request_id="strict-v1", actor_id="sec1", baseline_id="strict", version=1,
            payload={"voting": {"quorum_ratio": 0.5, "approval_ratio": 0.5},
                     "eligibility": {"min_fulfillment_ratio": 0.0},
                     "data": {"allow_third_party_transfer": False}},
            effective_at="2027-01-01T00:00:00Z")
        self.ctx.clock.set(datetime(2027, 6, 1, tzinfo=timezone.utc))
        self.ctx.dataset(producers=["m1"])
        with self.assertRaises(PermissionDenied):
            self.gov.submit_proposal(
                request_id="propX", actor_id="rep2", proposal_id="propX",
                dataset_id="obs-A", dataset_version=1, purpose="转交",
                third_party_transfer=True)

    def test_sensitive_dataset_cannot_be_transferred_even_if_baseline_allows(self):
        self.ctx.dataset(1, sensitivity="sensitive", producers=["m1"])
        with self.assertRaises(PermissionDenied):
            self.gov.submit_proposal(
                request_id="propS", actor_id="rep2", proposal_id="propS",
                dataset_id="obs-A", dataset_version=1, purpose="敏感转交",
                third_party_transfer=True)


if __name__ == "__main__":
    unittest.main()

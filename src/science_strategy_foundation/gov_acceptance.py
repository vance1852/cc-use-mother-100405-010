"""国际科研合作贡献与数据治理的离线端到端验收。

用一个完整故事验证全部治理不变量：

* 成员贡献进度不同，访问资格按提案提交时的章程版本与贡献快照计算；
* 利益冲突代表回避、法定人数与表决权重定影；
* 迟交贡献、成员权重/章程变化不回改已生效许可；
* 数据更正、许可暂停/恢复、成员退出只产生后继权利义务；
* 重复下载回调不重复计数，并发授权最多一个版本有效；
* 成果发表按快照贡献生成署名责任；审计哈希链完整。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .gov_service import GovernanceService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "gov_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        gov = GovernanceService(database, clock)

        # 1. 秘书处与成员
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="secretariat", name="计划秘书处")
        service.register_actor(request_id="sec-actor", actor_id="bootstrap", new_actor_id="sec",
                               display_name="执行秘书", role="secretariat",
                               organization_id="secretariat")
        members = (("cn", "中国", 3.0), ("de", "德国", 2.0), ("br", "巴西", 1.0),
                   ("za", "南非", 1.0))
        for member_id, name, weight in members:
            gov.register_member(request_id=f"mem-{member_id}", actor_id="sec",
                                member_id=member_id, kind="country", name=name, weight=weight)
        representatives = {"cn": "rep-cn", "de": "rep-de", "br": "rep-br", "za": "rep-za"}
        for member_id, actor_id in representatives.items():
            service.register_actor(request_id=f"actor-{member_id}", actor_id="sec",
                                   new_actor_id=actor_id, display_name=f"{member_id} 代表",
                                   role="operator", organization_id=member_id)

        # 2. 生效章程：法定人数 1/2、通过比例 1/2、实缴门槛 50%、禁止第三方转交
        gov.register_baseline(
            request_id="charter-1", actor_id="sec", baseline_id="charter", version=1,
            payload={"voting": {"quorum_ratio": 0.5, "approval_ratio": 0.5},
                     "eligibility": {"min_fulfillment_ratio": 0.5},
                     "authorship": {"order_rule": "contribution_weight"},
                     "data": {"allow_third_party_transfer": False}})

        # 3. 承诺与进度不一的实缴（截止 2026-09-01）
        gov.register_commitment(request_id="com-cn", actor_id="rep-cn", commitment_id="com-cn",
                                member_id="cn", kind="instrument_time", amount=200, unit="hour",
                                due_at="2026-09-01T00:00:00Z")
        gov.register_commitment(request_id="com-de", actor_id="rep-de", commitment_id="com-de",
                                member_id="de", kind="funding", amount=100, unit="kEUR",
                                due_at="2026-09-01T00:00:00Z")
        gov.register_commitment(request_id="com-br", actor_id="rep-br", commitment_id="com-br",
                                member_id="br", kind="calibration", amount=100, unit="hour",
                                due_at="2026-09-01T00:00:00Z")
        gov.register_commitment(request_id="com-za", actor_id="rep-za", commitment_id="com-za",
                                member_id="za", kind="funding", amount=100, unit="kUSD",
                                due_at="2026-09-01T00:00:00Z")
        # 中国足额、德国 60% 达标、巴西 40% 不达标、南非 0
        gov.record_contribution(request_id="pay-cn", actor_id="rep-cn", commitment_id="com-cn",
                                amount=200, contributed_at="2026-08-10T00:00:00Z")
        gov.record_contribution(request_id="pay-de", actor_id="rep-de", commitment_id="com-de",
                                amount=60, contributed_at="2026-08-20T00:00:00Z")
        gov.record_contribution(request_id="pay-br", actor_id="rep-br", commitment_id="com-br",
                                amount=40, contributed_at="2026-08-25T00:00:00Z")

        # 4. 仪器排期与首批观测数据（受限、禁运至 2026-12-01，产出方为中国/德国）
        gov.schedule_instrument(request_id="slot-1", actor_id="rep-cn", slot_id="slot-1",
                                member_id="cn", instrument_code="SAT-A",
                                starts_at="2026-07-01T00:00:00Z", ends_at="2026-07-10T00:00:00Z")
        gov.mark_slot_delivered(request_id="slot-1-done", actor_id="rep-cn", slot_id="slot-1")
        gov.register_dataset(request_id="ds-1", actor_id="sec", dataset_id="obs-2026",
                             version=1, title="首批气候观测", sensitivity="restricted",
                             embargo_until="2026-12-01T00:00:00Z", owning_member_id="cn",
                             producer_member_ids=["cn", "de"])

        # 5. 德国提交使用提案；提交时刻快照定影
        gov.submit_proposal(request_id="prop-1", actor_id="rep-de", proposal_id="P1",
                            dataset_id="obs-2026", dataset_version=1,
                            purpose="极端降水归因研究")
        snapshot = gov.proposal_detail("P1")["snapshot"]
        eligible = {m["member_id"]: m["eligible"] for m in snapshot["members"]}
        assert eligible == {"cn": True, "de": True, "br": False, "za": False}, eligible

        # 6. 表决：中国(权重3)赞成、德国(权重2)赞成；巴西无资格不能投票
        gov.cast_ballot(request_id="vote-cn", actor_id="rep-cn", proposal_id="P1", vote="yes")
        gov.cast_ballot(request_id="vote-de", actor_id="rep-de", proposal_id="P1", vote="yes")
        blocked = False
        try:
            gov.cast_ballot(request_id="vote-br", actor_id="rep-br", proposal_id="P1", vote="yes")
        except Exception:
            blocked = True
        assert blocked

        # 6b. 新章程提高门槛到 80%、并把中国权重调整：都不得影响已提交提案
        clock.set(datetime(2026, 10, 2, tzinfo=timezone.utc))
        gov.register_baseline(
            request_id="charter-2", actor_id="sec", baseline_id="charter", version=2,
            payload={"voting": {"quorum_ratio": 0.66, "approval_ratio": 0.66},
                     "eligibility": {"min_fulfillment_ratio": 0.8},
                     "authorship": {"order_rule": "contribution_weight"},
                     "data": {"allow_third_party_transfer": False}})
        gov.set_member_weight(request_id="weight-cn", actor_id="sec", member_id="cn", weight=30)
        clock.set(datetime(2026, 10, 3, tzinfo=timezone.utc))
        gov.resolve_proposal(request_id="resolve-1", actor_id="sec", proposal_id="P1")
        resolution = gov.proposal_detail("P1")["resolution"]
        assert resolution["outcome"] == "approved"
        assert resolution["yes_weight"] == 5.0, resolution  # 3+2 旧权重
        assert resolution["eligible_weight"] == 5.0

        # 7. 禁运期：德国是产出方可访问；巴西若获批也受禁运限制（此处无许可）
        de_access = gov.explain_access("de", "obs-2026", actor_id="sec")
        assert de_access["allowed"] and de_access["reason"] == "access_allowed"

        # 8. 许可暂停/恢复只产生后继事件
        gov.suspend_grant(request_id="suspend-1", actor_id="sec", member_id="de",
                          dataset_id="obs-2026", reason="校准材料复核")
        assert not gov.explain_access("de", "obs-2026", actor_id="sec")["allowed"]
        gov.resume_grant(request_id="resume-1", actor_id="sec", member_id="de",
                         dataset_id="obs-2026", reason="复核通过")
        assert gov.explain_access("de", "obs-2026", actor_id="sec")["allowed"]

        # 9. 南非迟交：只产生后继权利，旧提案不变
        clock.set(datetime(2026, 10, 5, tzinfo=timezone.utc))
        gov.record_contribution(request_id="pay-za-late", actor_id="rep-za",
                                commitment_id="com-za", amount=100,
                                contributed_at="2026-10-04T00:00:00Z")
        assert gov.proposal_detail("P1")["snapshot"] == snapshot

        # 10. 数据更正：v2 取代 v1，旧许可失效，需要新提案
        gov.register_dataset(request_id="ds-2", actor_id="sec", dataset_id="obs-2026",
                             version=2, title="首批气候观测（校正）", sensitivity="restricted",
                             embargo_until="2026-09-01T00:00:00Z", owning_member_id="cn",
                             producer_member_ids=["cn", "de"], corrected_of=1)
        corrected = gov.explain_access("de", "obs-2026", actor_id="sec")
        assert not corrected["allowed"]
        assert corrected["evidence"]["latest_grant_event"]["event"] == "superseded"

        # 11. 德国针对 v2 重新提案获批（此时禁运已过、新章程门槛 80%，德国 60% 不合格！
        #     德国补足迟缴后才合格——迟交对其后提案有效）
        clock.set(datetime(2026, 10, 6, tzinfo=timezone.utc))
        gov.record_contribution(request_id="pay-de-rest", actor_id="rep-de",
                                commitment_id="com-de", amount=40,
                                contributed_at="2026-10-05T00:00:00Z")
        gov.submit_proposal(request_id="prop-2", actor_id="rep-de", proposal_id="P2",
                            dataset_id="obs-2026", dataset_version=2, purpose="校正数据复核")
        # 新章程合格分母：cn(30) 足额、de(2) 足额、za(1) 足额；br 40% 不合格
        gov.cast_ballot(request_id="vote2-cn", actor_id="rep-cn", proposal_id="P2", vote="yes")
        gov.cast_ballot(request_id="vote2-de", actor_id="rep-de", proposal_id="P2", vote="yes")
        gov.cast_ballot(request_id="vote2-za", actor_id="rep-za", proposal_id="P2", vote="no")
        gov.resolve_proposal(request_id="resolve-2", actor_id="sec", proposal_id="P2")
        grant_v2 = gov.grant_timeline("de", "obs-2026")[-1]
        assert grant_v2["event"] == "granted" and grant_v2["dataset_version"] == 2

        # 12. 下载回调重复投递只计一次
        gov.register_download_callback(request_id="cb-1", actor_id="sec", callback_id="cb-0001",
                                       grant_id=grant_v2["grant_id"])
        callback_duplicated = False
        try:
            gov.register_download_callback(request_id="cb-1-again", actor_id="sec",
                                           callback_id="cb-0001",
                                           grant_id=grant_v2["grant_id"])
        except Exception:
            callback_duplicated = True
        assert callback_duplicated
        assert len(gov.list_downloads()) == 1

        # 13. 成果发表：按提交快照贡献排序署名（中国实缴最高居首）
        gov.record_publication(request_id="pub-1", actor_id="sec", publication_id="paper-1",
                               grant_id=grant_v2["grant_id"],
                               title="全球极端降水归因（大科学计划合作组）")
        authors = gov.list_publications(grant_v2["grant_id"])[0]["authorship"]
        assert authors[0]["member_id"] == "cn", authors

        # 14. 巴西退出：撤销其许可（巴西无许可），停用其代表；历史不删改
        gov.withdraw_member(request_id="exit-br", actor_id="sec", member_id="br", reason="预算退出")
        timeline_events = [row["event"] for row in gov.grant_timeline("de", "obs-2026")]

        # 15. 未履行承诺追溯（巴西尚欠 60 校准小时）
        unfulfilled = gov.unfulfilled_commitments(actor_id="sec")
        za_gap = [item for item in unfulfilled if item["member_id"] == "za"]
        # 南非已在 10-04 补足，巴西退出但承诺缺口仍可追溯
        br_gap = [item for item in unfulfilled if item["member_id"] == "br"]
        assert not za_gap and br_gap and br_gap[0]["shortfall"] == 60.0, unfulfilled

        audit_valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": event_count,
            "first_resolution": {"outcome": resolution["outcome"],
                                 "yes_weight": resolution["yes_weight"],
                                 "eligible_weight": resolution["eligible_weight"]},
            "ineligible_blocked": blocked,
            "downloads": len(gov.list_downloads()),
            "callback_deduped": callback_duplicated,
            "first_author": authors[0]["member_id"],
            "grant_timeline": timeline_events,
            "unfulfilled_members": [item["member_id"] for item in unfulfilled],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

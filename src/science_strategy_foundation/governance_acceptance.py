"""运行国际科研合作贡献与数据治理服务的离线端到端验收。

场景覆盖：章程生效与修订、成员登记、承诺与实缴（含迟交）、仪器排期、
数据集版本与禁运、提案决定快照、利益冲突回避、决议表决、许可授予/暂停、
下载回调幂等、数据更正、成员退出、成果发表署名与权限解释。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import MutableClock
from .errors import PermissionDenied
from .governance import GovernanceService
from .service import DomainService
from .storage import Database

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


def run() -> dict[str, object]:
    """执行完整治理链并返回核对结果。"""

    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "governance-acceptance.sqlite3")
        clock = MutableClock(T0)
        foundation = DomainService(database, clock)
        gov = GovernanceService(database, clock)

        # 组织与操作者：秘书处 + 三个成员机构。
        foundation.register_organization(request_id="org-sec", actor_id="bootstrap",
                                         organization_id="sec", name="计划秘书处")
        foundation.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                  new_actor_id="admin-1", display_name="系统管理员",
                                  role="admin", organization_id="sec")
        foundation.register_actor(request_id="actor-sec", actor_id="admin-1",
                                  new_actor_id="sec-1", display_name="秘书处干事",
                                  role="secretariat", organization_id="sec")
        for key, label in (("a", "甲"), ("b", "乙"), ("c", "丙")):
            foundation.register_organization(request_id=f"org-{key}", actor_id="admin-1",
                                             organization_id=f"org-{key}", name=f"成员机构{label}")
            foundation.register_actor(request_id=f"actor-{key}", actor_id="admin-1",
                                      new_actor_id=f"rep-{key}", display_name=f"代表{label}",
                                      role="representative", organization_id=f"org-{key}")

        # 章程 v1 立即生效；成员甲(权重2)、乙(权重1)、丙(权重1)。
        charter1 = gov.create_charter(request_id="charter-1", actor_id="sec-1", rules=RULES)
        checks["charter_v1_effective"] = charter1["status"] == "effective"
        gov.register_member(request_id="mem-a", actor_id="sec-1", member_id="ma",
                            name="甲国", kind="country", organization_id="org-a",
                            voting_weight=2.0)
        gov.register_member(request_id="mem-b", actor_id="sec-1", member_id="mb",
                            name="乙机构", kind="institution", organization_id="org-b",
                            voting_weight=1.0)
        gov.register_member(request_id="mem-c", actor_id="sec-1", member_id="mc",
                            name="丙机构", kind="institution", organization_id="org-c",
                            voting_weight=1.0)

        # 承诺：甲经费100+仪器10小时，乙经费100，均 T+10 到期。
        due = (T0 + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        gov.record_commitment(request_id="com-a-fund", actor_id="sec-1", member_id="ma",
                              kind="funding", amount=100, unit="kUSD", due_at=due)
        gov.record_commitment(request_id="com-a-inst", actor_id="sec-1", member_id="ma",
                              kind="instrument_time", amount=10, unit="hour", due_at=due)
        gov.record_commitment(request_id="com-b-fund", actor_id="sec-1", member_id="mb",
                              kind="funding", amount=100, unit="kUSD", due_at=due)

        # 甲按时实缴经费并完成仪器时段；乙先实缴 40%。
        clock.advance(days=1)
        gov.record_contribution(request_id="pay-a-1", actor_id="sec-1", member_id="ma",
                                kind="funding", amount=100)
        gov.register_instrument(request_id="ins-1", actor_id="sec-1",
                                instrument_id="ins-1", name="高原辐射观测仪")
        start = (clock.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        end = (clock.now() + timedelta(hours=11)).isoformat().replace("+00:00", "Z")
        slot = gov.schedule_slot(request_id="slot-1", actor_id="sec-1", instrument_id="ins-1",
                                 member_id="ma", starts_at=start, ends_at=end)
        clock.advance(days=1)
        done = gov.complete_slot(request_id="slot-1-done", actor_id="sec-1",
                                 slot_id=slot["slot_id"])
        checks["slot_hours_contributed"] = done["hours"] == 10
        gov.record_contribution(request_id="pay-b-1", actor_id="sec-1", member_id="mb",
                                kind="funding", amount=40)

        # 数据集 DS-1 首版本 restricted，禁运 30 天。
        gov.register_dataset(request_id="ds-1", actor_id="sec-1", dataset_id="ds-1",
                             title="首批气候观测数据集")
        version1 = gov.publish_version(request_id="ds-1-v1", actor_id="sec-1",
                                       dataset_id="ds-1", sensitivity="restricted")

        # 禁运期内：甲足额实缴获批许可；乙完成率不足被拒。
        clock.advance(days=1)
        prop_a = gov.submit_proposal(request_id="prop-a", actor_id="rep-a",
                                     dataset_id="ds-1", purpose="气候变化归因分析")
        checks["member_a_approved_in_embargo"] = prop_a["status"] == "approved"
        prop_b1 = gov.submit_proposal(request_id="prop-b-1", actor_id="rep-b",
                                      dataset_id="ds-1", purpose="区域能源评估")
        checks["member_b_denied_in_embargo"] = prop_b1["status"] == "denied"
        denied_snapshot = prop_b1["decision"]["snapshot"]["fulfillment_ratio"]

        # 丙无承诺但请求第三方转交：进入待决，需治理决议。
        prop_c = gov.submit_proposal(request_id="prop-c", actor_id="rep-c",
                                     dataset_id="ds-1", purpose="联合企业再分析",
                                     third_party_transfer=True)
        checks["transfer_proposal_pending"] = prop_c["status"] == "pending"

        # 丙申报利益冲突；决议表决时丙回避，甲乙赞成，决议通过。
        gov.declare_conflict(request_id="conf-c", actor_id="rep-c",
                             dataset_id="ds-1", reason="与接收第三方存在资助关系")
        closes = (clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        res1 = gov.open_resolution(request_id="res-1", actor_id="sec-1",
                                   kind="approve_transfer",
                                   subject={"proposal_id": prop_c["proposal_id"]},
                                   closes_at=closes)
        gov.cast_vote(request_id="vote-a-1", actor_id="rep-a",
                      resolution_id=res1["resolution_id"], choice="yes")
        gov.cast_vote(request_id="vote-b-1", actor_id="rep-b",
                      resolution_id=res1["resolution_id"], choice="yes")
        vote_c = gov.cast_vote(request_id="vote-c-1", actor_id="rep-c",
                               resolution_id=res1["resolution_id"], choice="yes")
        checks["conflicted_vote_recused"] = vote_c["recused"] and not vote_c["counted"] is False
        clock.advance(days=1, hours=1)
        closed1 = gov.close_resolution(request_id="res-1-close", actor_id="sec-1",
                                       resolution_id=res1["resolution_id"])
        checks["transfer_resolution_passed"] = closed1["status"] == "passed"
        checks["recused_weight_not_counted"] = (
            closed1["tally"]["participation_weight"] == 3.0
            and closed1["tally"]["recused_members"] == ["mc"])
        license_c = gov.get_license(actor_id="sec-1", license_id=closed1["effect"]["license_id"])
        checks["transfer_scope_granted"] = license_c["scope"] == "read+transfer"

        # 乙迟交剩余经费：只产生后继权利，旧拒绝决定不变，新提案获批。
        clock.advance(days=9)
        late = gov.record_contribution(request_id="pay-b-2", actor_id="sec-1", member_id="mb",
                                       kind="funding", amount=60)
        checks["late_contribution_flagged"] = late["late"] is True
        old = gov.get_proposal(actor_id="sec-1", proposal_id=prop_b1["proposal_id"])
        checks["old_denial_unchanged"] = (
            old["status"] == "denied"
            and old["decision"]["snapshot"]["fulfillment_ratio"] == denied_snapshot)
        prop_b2 = gov.submit_proposal(request_id="prop-b-2", actor_id="rep-b",
                                      dataset_id="ds-1", purpose="区域能源评估")
        checks["member_b_approved_after_late_payment"] = prop_b2["status"] == "approved"

        # 下载回调幂等：同一回调编号重复上报只计一次。
        license_a = prop_a["license_id"]
        dl1 = gov.record_download(request_id="dl-1", actor_id="rep-a",
                                  callback_id="cb-0001", license_id=license_a, byte_count=1024)
        dl2 = gov.record_download(request_id="dl-2", actor_id="rep-a",
                                  callback_id="cb-0001", license_id=license_a, byte_count=1024)
        checks["download_callback_deduped"] = dl1["counted"] and not dl2["counted"]

        # 数据更正产生 v2，旧版本被取代，后续下载使用新版本。
        gov.correct_dataset(request_id="ds-1-fix", actor_id="sec-1", dataset_id="ds-1",
                            note="修正温度校准系数")
        dl3 = gov.record_download(request_id="dl-3", actor_id="rep-a",
                                  callback_id="cb-0002", license_id=license_a, byte_count=2048)
        checks["download_uses_corrected_version"] = dl3["dataset_version"] == 2

        # 乙的许可被决议暂停后下载被拒，历史下载保留。
        license_b = prop_b2["license_id"]
        gov.record_download(request_id="dl-4", actor_id="rep-b",
                            callback_id="cb-0003", license_id=license_b, byte_count=512)
        res2 = gov.open_resolution(request_id="res-2", actor_id="sec-1",
                                   kind="suspend_license", subject={"license_id": license_b},
                                   closes_at=(clock.now() + timedelta(hours=1))
                                   .isoformat().replace("+00:00", "Z"))
        gov.cast_vote(request_id="vote-a-2", actor_id="rep-a",
                      resolution_id=res2["resolution_id"], choice="yes")
        gov.cast_vote(request_id="vote-c-2", actor_id="rep-c",
                      resolution_id=res2["resolution_id"], choice="yes")
        clock.advance(hours=2)
        closed2 = gov.close_resolution(request_id="res-2-close", actor_id="sec-1",
                                       resolution_id=res2["resolution_id"])
        checks["suspend_resolution_passed"] = closed2["status"] == "passed"
        blocked = False
        try:
            gov.record_download(request_id="dl-5", actor_id="rep-b",
                                callback_id="cb-0004", license_id=license_b, byte_count=64)
        except PermissionDenied:
            blocked = True
        checks["suspended_license_blocks_download"] = blocked

        # 甲发表成果：署名份额按实缴计算，甲乙达标，丙不达标。
        pub = gov.register_publication(request_id="pub-1", actor_id="rep-a",
                                       license_id=license_a, title="首批观测归因研究")
        shares = {item["member_id"]: item for item in pub["attributions"]}
        checks["attribution_shares"] = (
            shares["ma"]["eligible"] and shares["mb"]["eligible"]
            and not shares["mc"]["eligible"])

        # 丙退出：许可被撤销，解释接口说明原因；历史记录保留。
        gov.withdraw_member(request_id="wd-c", actor_id="sec-1", member_id="mc")
        explain_c = gov.explain_access(actor_id="sec-1", member_id="mc", dataset_id="ds-1")
        checks["withdrawn_member_explained"] = (
            not explain_c["allowed"]
            and any("撤销" in reason for reason in explain_c["reasons"]))
        explain_a = gov.explain_access(actor_id="rep-a", member_id="ma", dataset_id="ds-1")
        checks["member_a_still_allowed"] = explain_a["allowed"]

        # 秘书处追溯：乙的实缴完成率与历史下载、甲的署名责任可核。
        standing_b = gov.member_standing(actor_id="sec-1", member_id="mb")
        checks["member_b_fulfilled_late"] = (
            standing_b["fulfillment_ratio"] == 1.0
            and standing_b["unfulfilled_commitments"] == [])
        usage = gov.dataset_usage(actor_id="sec-1", dataset_id="ds-1")
        checks["usage_traceable"] = (
            usage["downloads"]["count"] == 3 and len(usage["publications"]) == 1)

        # 章程修订经决议生效，旧提案决定仍引用 v1。
        charter2 = gov.create_charter(request_id="charter-2", actor_id="sec-1",
                                      rules={**RULES, "access_min_fulfillment": 0.6})
        checks["charter_v2_draft"] = charter2["status"] == "draft"
        res3 = gov.open_resolution(request_id="res-3", actor_id="sec-1", kind="amend_charter",
                                   subject={"charter_id": charter2["charter_id"]},
                                   closes_at=(clock.now() + timedelta(hours=1))
                                   .isoformat().replace("+00:00", "Z"))
        gov.cast_vote(request_id="vote-a-3", actor_id="rep-a",
                      resolution_id=res3["resolution_id"], choice="yes")
        gov.cast_vote(request_id="vote-b-3", actor_id="rep-b",
                      resolution_id=res3["resolution_id"], choice="yes")
        clock.advance(hours=2)
        closed3 = gov.close_resolution(request_id="res-3-close", actor_id="sec-1",
                                       resolution_id=res3["resolution_id"])
        checks["charter_v2_activated"] = (
            closed3["status"] == "passed" and closed3["effect"]["applied"])
        checks["old_proposal_kept_charter_v1"] = old["charter_version"] == 1

        valid, event_count = foundation.verify_audit()
        checks["audit_valid"] = valid
        result: dict[str, object] = {
            "status": "ok" if all(checks.values()) else "failed",
            "checks": checks, "audit_events": event_count, "audit_valid": valid,
            "embargo_until_v1": version1["embargo_until"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

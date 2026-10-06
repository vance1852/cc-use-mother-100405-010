"""治理规则的纯函数实现。

这一层不读写数据库、不产生副作用：给定"提案提交时定影"的章程与贡献快照，
任何人在任何时候都能复算出相同的资格、法定人数、表决结果与访问状态。
服务层负责在正确的时刻截取快照并持久化，后续权重或章程变化不会进入这里。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# 时间全部使用同构的 UTC ISO-8601 文本，可直接按字典序比较。


def fulfillment_ratio(committed_due: float, paid: float) -> float:
    """计算截止某时点已到期承诺的实缴比例，无到期承诺视为 1.0。"""

    if committed_due <= 0:
        return 1.0
    return round(min(1.0, paid / committed_due), 6)


def is_conflicted(member: dict[str, Any], dataset_owner_id: str, applicant_id: str) -> bool:
    """判断代表是否对该数据集或申请方存在利益冲突，必须回避表决。"""

    conflict_orgs = set(member.get("conflict_orgs") or [])
    return dataset_owner_id in conflict_orgs or applicant_id in conflict_orgs


def build_member_snapshot(*, member: dict[str, Any], paid: float, committed_due: float,
                          baseline: dict[str, Any], dataset_owner_id: str,
                          applicant_id: str) -> dict[str, Any]:
    """为单个成员生成提交时刻的贡献与资格快照条目。"""

    threshold = float(baseline.get("eligibility", {}).get("min_fulfillment_ratio", 0.0))
    ratio = fulfillment_ratio(committed_due, paid)
    active = member.get("status") == "active"
    conflict = is_conflicted(member, dataset_owner_id, applicant_id)
    eligible = active and ratio + 1e-9 >= threshold and not conflict
    return {
        "member_id": member["member_id"],
        "status": member.get("status"),
        "weight": float(member.get("weight", 1.0)),
        "committed_due": round(committed_due, 6),
        "paid": round(paid, 6),
        "fulfillment_ratio": ratio,
        "conflict": conflict,
        "eligible": eligible,
        "ineligible_reasons": _reasons(active, ratio, threshold, conflict),
    }


def _reasons(active: bool, ratio: float, threshold: float, conflict: bool) -> list[str]:
    reasons: list[str] = []
    if not active:
        reasons.append("member_not_active")
    if ratio + 1e-9 < threshold:
        reasons.append("fulfillment_below_threshold")
    if conflict:
        reasons.append("conflict_recusal")
    return reasons


def summarize_snapshot(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """汇总全部成员快照，给出表决分母。"""

    eligible_entries = [entry for entry in entries if entry["eligible"]]
    return {
        "members": list(entries),
        "eligible_member_ids": [entry["member_id"] for entry in eligible_entries],
        "eligible_weight": round(sum(entry["weight"] for entry in eligible_entries), 6),
    }


def tally_ballots(ballots: Iterable[dict[str, Any]], snapshot: dict[str, Any],
                  baseline: dict[str, Any]) -> dict[str, Any]:
    """按提交快照定影的权重与法定人数规则统计表决。

    选票中的 weight 在投票时从快照复制；之后成员权重变化不影响本结果。
    回避代表不进入分母，弃权票计入法定人数但不计入通过比例。
    """

    voting_rules = baseline.get("voting", {})
    quorum_ratio = float(voting_rules.get("quorum_ratio", 0.5))
    approval_ratio = float(voting_rules.get("approval_ratio", 0.5))

    by_member = {entry["member_id"]: entry for entry in snapshot["members"]}
    yes_weight = no_weight = abstain_weight = 0.0
    present_members: list[str] = []
    for ballot in ballots:
        entry = by_member.get(ballot["member_id"])
        if entry is None or not entry["eligible"]:
            # 快照中无资格的票不产生任何效力
            continue
        weight = float(ballot["weight"])
        present_members.append(ballot["member_id"])
        if ballot["vote"] == "yes":
            yes_weight += weight
        elif ballot["vote"] == "no":
            no_weight += weight
        else:
            abstain_weight += weight

    eligible_weight = float(snapshot["eligible_weight"])
    cast_weight = yes_weight + no_weight + abstain_weight
    quorum_met = eligible_weight > 0 and cast_weight / eligible_weight + 1e-9 >= quorum_ratio
    decided_weight = yes_weight + no_weight
    approved = quorum_met and decided_weight > 0 and yes_weight / decided_weight + 1e-9 >= approval_ratio
    return {
        "outcome": "approved" if approved else "rejected",
        "quorum_met": quorum_met,
        "yes_weight": round(yes_weight, 6),
        "no_weight": round(no_weight, 6),
        "abstain_weight": round(abstain_weight, 6),
        "cast_weight": round(cast_weight, 6),
        "eligible_weight": round(eligible_weight, 6),
        "present_member_ids": present_members,
    }


def embargo_open(dataset: dict[str, Any], at: str) -> bool:
    """判断禁运窗口在给定时刻是否已经结束。"""

    return at >= dataset["embargo_until"]


def access_gates(*, resolution: dict[str, Any] | None, applicant_snapshot: dict[str, Any] | None,
                member_active: bool, dataset: dict[str, Any], at: str,
                applicant_is_contributor: bool, grant_event: str | None) -> dict[str, Any]:
    """逐项解释某成员此刻为何拥有或失去数据访问权。

    决议与资格来自提案提交时定影的快照；禁运是随时间评估的窗口条件；
    成员退出、许可暂停、版本取代则是许可账本上的后继事件。
    """

    gates: list[dict[str, Any]] = []

    approved = bool(resolution and resolution["outcome"] == "approved" and resolution["quorum_met"])
    gates.append({"gate": "resolution_approved", "passed": approved,
                  "detail": None if resolution is None else {"outcome": resolution["outcome"],
                                                             "quorum_met": resolution["quorum_met"]}})

    eligible = bool(applicant_snapshot and applicant_snapshot["eligible"])
    gates.append({"gate": "contribution_eligibility_at_submission", "passed": eligible,
                  "detail": applicant_snapshot})

    gates.append({"gate": "member_active", "passed": member_active})

    embargo_lifted = embargo_open(dataset, at)
    embargo_pass = embargo_lifted or applicant_is_contributor
    gates.append({"gate": "embargo_window", "passed": embargo_pass,
                  "detail": {"embargo_until": dataset["embargo_until"], "at": at,
                             "lifted": embargo_lifted, "applicant_is_contributor": applicant_is_contributor,
                             "sensitivity": dataset["sensitivity"]}})

    grant_alive = grant_event == "granted" or grant_event == "resumed"
    gates.append({"gate": "grant_effective", "passed": grant_alive,
                  "detail": {"latest_event": grant_event}})

    allowed = all(gate["passed"] for gate in gates)
    if allowed:
        reason = "access_allowed"
    elif not approved:
        reason = "denied_no_approved_resolution"
    elif not eligible:
        reason = "denied_ineligible_at_submission"
    elif not member_active:
        reason = "terminated_member_withdrawal"
    elif not embargo_pass:
        reason = "denied_embargo_window"
    else:
        reason = f"terminated_grant_{grant_event or 'missing'}"
    return {"allowed": allowed, "reason": reason, "gates": gates}


def order_authors(contributors: Sequence[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """按章程署名规则排序署名责任：默认按实缴贡献权重降序。"""

    ordering = rules.get("order_rule", "contribution_weight")
    items = list(contributors)
    if ordering == "contribution_weight":
        items.sort(key=lambda item: (-float(item.get("paid", 0.0)), item["member_id"]))
    return items

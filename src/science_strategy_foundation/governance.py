"""国际科研合作贡献与数据治理服务。

把成员身份、国家或机构承诺、实缴贡献、仪器排期、数据集版本、敏感级别、
禁运窗口、使用提案、署名规则和治理决议串成可审计的权利来源。

时间语义：
- 访问决定按提案提交时有效的章程与贡献快照计算，快照随提案持久化；
- 章程修订、法定人数与表决权重变化只向后生效，不倒改已生效许可；
- 迟交贡献、成员退出、数据更正、许可暂停与成果发表只产生后继权利与义务。

幂等与并发：
- 所有写接口按 request_id 幂等，重复请求返回原回执；
- 表决按 (resolution_id, member_id) 去重，下载回调按 callback_id 去重；
- 同一成员在同一数据集上最多一个有效许可（部分唯一索引 + IMMEDIATE 事务），
  许可状态迁移携带 revision 乐观并发检查。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

SENSITIVITY_LEVELS = ("public", "internal", "restricted", "sensitive")
COMMITMENT_KINDS = ("instrument_time", "funding", "calibration_data")
MEMBER_KINDS = ("country", "institution")
RESOLUTION_KINDS = (
    "amend_charter",
    "suspend_license",
    "resume_license",
    "approve_proposal",
    "approve_transfer",
    "admit_member",
)
TRANSFER_POLICIES = ("prohibited", "resolution", "allowed")
VOTE_CHOICES = ("yes", "no", "abstain")
SECRETARIAT_ROLES = ("secretariat", "admin")
REPRESENTATIVE_ROLES = ("representative",)
READER_ROLES = SECRETARIAT_ROLES + ("auditor",)

DEFAULT_RULES: dict[str, Any] = {
    "quorum_fraction": 0.5,
    "embargo_days": {"public": 0, "internal": 0, "restricted": 180, "sensitive": 365},
    "access_min_fulfillment": 0.5,
    "embargo_access_fulfillment": 1.0,
    "authorship_min_share": 0.1,
    "third_party_transfer": "prohibited",
    "license_days": 365,
    "sensitive_requires_resolution": True,
    "require_contributions": False,
}


def _fmt(value: datetime) -> str:
    """把时间规范化为定宽 UTC 文本，保证可字典序比较。"""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_time(value: Any, field: str) -> datetime:
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _fraction(value: Any, field: str, lower_open: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是数字")
    number = float(value)
    if lower_open:
        valid = 0.0 < number <= 1.0
    else:
        valid = 0.0 <= number <= 1.0
    if not valid:
        raise ValidationError(f"{field} 超出允许区间")
    return number


def _merge_rules(raw: Any) -> dict[str, Any]:
    """合并并校验章程规则。"""

    if not isinstance(raw, dict):
        raise ValidationError("rules 必须是对象")
    rules = {**DEFAULT_RULES, **raw}
    embargo_raw = rules.get("embargo_days")
    if not isinstance(embargo_raw, dict):
        raise ValidationError("embargo_days 必须是对象")
    embargo: dict[str, int] = dict(DEFAULT_RULES["embargo_days"])
    for level, days in embargo_raw.items():
        if level not in SENSITIVITY_LEVELS:
            raise ValidationError(f"未知敏感级别: {level}")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
            raise ValidationError("embargo_days 必须是非负天数")
        embargo[level] = int(days)
    rules["embargo_days"] = embargo
    rules["quorum_fraction"] = _fraction(rules["quorum_fraction"], "quorum_fraction", lower_open=True)
    for key in ("access_min_fulfillment", "embargo_access_fulfillment", "authorship_min_share"):
        rules[key] = _fraction(rules[key], key)
    license_days = rules["license_days"]
    if isinstance(license_days, bool) or not isinstance(license_days, (int, float)) or int(license_days) <= 0:
        raise ValidationError("license_days 必须是正整数")
    rules["license_days"] = int(license_days)
    if rules["third_party_transfer"] not in TRANSFER_POLICIES:
        raise ValidationError("third_party_transfer 策略无效")
    for key in ("sensitive_requires_resolution", "require_contributions"):
        rules[key] = bool(rules[key])
    return rules


class GovernanceService:
    """协调成员、贡献、数据、提案、决议与许可的治理规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---- 基础工具 -----------------------------------------------------

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now(self) -> str:
        return _fmt(self.clock.now())

    def _identifier(self, value: Any, field: str) -> str:
        text = str(value).strip()
        if not IDENTIFIER.fullmatch(text):
            raise ValidationError(f"{field} 格式无效")
        return text

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        text = str(value).strip()
        if not text or len(text) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return text

    def _positive_number(self, value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise ValidationError(f"{field} 必须是正数")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数字") from exc
        if not number > 0:
            raise ValidationError(f"{field} 必须是正数")
        return number

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, response["resource_type"],
             response["resource_id"], canonical_json(response), self._now()),
        )
        response["replayed"] = False
        return response

    def _member_row(self, connection, member_id: str):
        row = connection.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
        if row is None:
            raise NotFoundError("成员不存在")
        return row

    def _member_for_actor(self, connection, actor):
        row = connection.execute(
            "SELECT * FROM members WHERE organization_id=? ORDER BY joined_at, member_id LIMIT 1",
            (actor["organization_id"],),
        ).fetchone()
        if row is None:
            raise PermissionDenied("操作者所属机构不是计划成员")
        return row

    def _require_can_view(self, connection, actor, member_id: str) -> None:
        if actor["role"] in READER_ROLES:
            return
        if actor["role"] in REPRESENTATIVE_ROLES:
            member = self._member_row(connection, member_id)
            if member["organization_id"] == actor["organization_id"]:
                return
        raise PermissionDenied("不能查看其他成员的资料")

    def _effective_charter(self, connection, at: datetime) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM charter_versions WHERE status='effective' AND effective_from<=? "
            "ORDER BY version DESC LIMIT 1",
            (_fmt(at),),
        ).fetchone()
        if row is None:
            return None
        return {"charter_id": row["charter_id"], "version": row["version"],
                "rules": json.loads(row["rules_json"]), "effective_from": row["effective_from"]}

    # ---- 贡献与承诺 ----------------------------------------------------

    def _standing(self, connection, member_id: str, at: datetime,
                  include_commitments: bool = False) -> dict[str, Any]:
        committed = {kind: 0.0 for kind in COMMITMENT_KINDS}
        contributed = {kind: 0.0 for kind in COMMITMENT_KINDS}
        for row in connection.execute(
                "SELECT kind, amount FROM commitments WHERE member_id=?", (member_id,)):
            committed[row["kind"]] += row["amount"]
        for row in connection.execute(
                "SELECT kind, amount FROM contributions WHERE member_id=?", (member_id,)):
            contributed[row["kind"]] += row["amount"]
        kinds: dict[str, dict[str, float]] = {}
        ratios = []
        for kind in COMMITMENT_KINDS:
            ratio = contributed[kind] / committed[kind] if committed[kind] > 0 else 1.0
            ratios.append(ratio)
            kinds[kind] = {"committed": committed[kind], "contributed": contributed[kind],
                           "ratio": round(ratio, 6)}
        result: dict[str, Any] = {
            "kinds": kinds,
            "fulfillment_ratio": round(min(ratios), 6),
            "total_committed": sum(committed.values()),
            "total_contributed": sum(contributed.values()),
        }
        if include_commitments:
            items = []
            unfulfilled = []
            for row in connection.execute(
                    "SELECT * FROM commitments WHERE member_id=? ORDER BY created_at, commitment_id",
                    (member_id,)):
                got = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM contributions WHERE commitment_id=?",
                    (row["commitment_id"],),
                ).fetchone()["total"]
                if got >= row["amount"]:
                    status = "fulfilled"
                elif _parse_time(row["due_at"], "due_at") < at:
                    status = "unfulfilled"
                else:
                    status = "open"
                if status == "unfulfilled":
                    unfulfilled.append(row["commitment_id"])
                late = connection.execute(
                    "SELECT 1 FROM contributions WHERE commitment_id=? AND late=1 LIMIT 1",
                    (row["commitment_id"],),
                ).fetchone()
                items.append({"commitment_id": row["commitment_id"], "kind": row["kind"],
                              "amount": row["amount"], "contributed": got, "unit": row["unit"],
                              "due_at": row["due_at"], "status": status,
                              "has_late_contribution": late is not None})
            result["commitments"] = items
            result["unfulfilled_commitments"] = unfulfilled
        return result

    # ---- 章程 ----------------------------------------------------------

    def create_charter(self, *, request_id: str, actor_id: str, rules: dict[str, Any],
                       effective_from: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "rules": rules, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            merged = _merge_rules(rules)
            effective_dt = _parse_time(effective_from, "effective_from") if effective_from else self._now_dt()

            def create() -> dict[str, Any]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM charter_versions"
                ).fetchone()["v"] + 1
                has_effective = connection.execute(
                    "SELECT 1 FROM charter_versions WHERE status='effective'"
                ).fetchone()
                status = "draft" if has_effective else "effective"
                charter_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO charter_versions(charter_id,version,rules_json,status,effective_from,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (charter_id, version, canonical_json(merged), status, _fmt(effective_dt),
                     actor["actor_id"], self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="charter.created",
                             resource_type="charter", resource_id=charter_id,
                             detail={"version": version, "status": status, "rules": merged},
                             occurred_at=self._now())
                return {"resource_type": "charter", "resource_id": charter_id,
                        "charter_id": charter_id, "version": version, "status": status,
                        "rules": merged}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.create_charter", payload=payload, create=create)

    # ---- 成员 ----------------------------------------------------------

    def register_member(self, *, request_id: str, actor_id: str, member_id: str, name: str,
                        kind: str, organization_id: str, voting_weight: float = 1.0) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "member_id": member_id, "name": name, "kind": kind,
                   "organization_id": organization_id, "voting_weight": voting_weight}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            member_id = self._identifier(member_id, "member_id")
            name = self._text(name, "name")
            if kind not in MEMBER_KINDS:
                raise ValidationError("成员类型无效")
            weight = self._positive_number(voting_weight, "voting_weight")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> dict[str, Any]:
                try:
                    connection.execute(
                        "INSERT INTO members(member_id,organization_id,name,kind,voting_weight,status,"
                        "joined_at) VALUES(?,?,?,?,?,'active',?)",
                        (member_id, organization_id, name, kind, weight, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("成员编号已经存在") from exc
                connection.execute(
                    "INSERT INTO member_weight_events(member_id,weight,effective_at) VALUES(?,?,?)",
                    (member_id, weight, self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="member.registered",
                             resource_type="member", resource_id=member_id,
                             detail={"name": name, "kind": kind, "organization_id": organization_id,
                                     "voting_weight": weight}, occurred_at=self._now())
                return {"resource_type": "member", "resource_id": member_id,
                        "member_id": member_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.register_member", payload=payload, create=create)

    def withdraw_member(self, *, request_id: str, actor_id: str, member_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "member_id": member_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            member = self._member_row(connection, member_id)
            if member["status"] != "active":
                raise ConflictError("成员已退出")

            def create() -> dict[str, Any]:
                connection.execute("UPDATE members SET status='withdrawn', withdrawn_at=? WHERE member_id=?",
                                   (self._now(), member_id))
                revoked = []
                for lic in connection.execute(
                        "SELECT * FROM licenses WHERE member_id=? AND status IN ('active','suspended')",
                        (member_id,)).fetchall():
                    connection.execute(
                        "UPDATE licenses SET status='revoked', revision=revision+1,"
                        " status_note='member_withdrawn', updated_at=? WHERE license_id=? AND revision=?",
                        (self._now(), lic["license_id"], lic["revision"]),
                    )
                    append_event(connection, actor_id=actor["actor_id"], action="license.revoked",
                                 resource_type="license", resource_id=lic["license_id"],
                                 detail={"reason": "member_withdrawn", "member_id": member_id},
                                 occurred_at=self._now())
                    revoked.append(lic["license_id"])
                append_event(connection, actor_id=actor["actor_id"], action="member.withdrawn",
                             resource_type="member", resource_id=member_id,
                             detail={"revoked_licenses": revoked}, occurred_at=self._now())
                return {"resource_type": "member", "resource_id": member_id,
                        "member_id": member_id, "status": "withdrawn",
                        "revoked_licenses": revoked}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.withdraw_member", payload=payload, create=create)

    def update_weight(self, *, request_id: str, actor_id: str, member_id: str,
                      voting_weight: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "member_id": member_id, "voting_weight": voting_weight}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            member = self._member_row(connection, member_id)
            weight = self._positive_number(voting_weight, "voting_weight")

            def create() -> dict[str, Any]:
                old = member["voting_weight"]
                connection.execute("UPDATE members SET voting_weight=? WHERE member_id=?",
                                   (weight, member_id))
                connection.execute(
                    "INSERT INTO member_weight_events(member_id,weight,effective_at) VALUES(?,?,?)",
                    (member_id, weight, self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="member.weight_updated",
                             resource_type="member", resource_id=member_id,
                             detail={"old_weight": old, "new_weight": weight,
                                     "note": "只向后生效，已开启的决议使用开启时快照"},
                             occurred_at=self._now())
                return {"resource_type": "member", "resource_id": member_id,
                        "member_id": member_id, "voting_weight": weight}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.update_weight", payload=payload, create=create)

    # ---- 承诺与实缴 ----------------------------------------------------

    def record_commitment(self, *, request_id: str, actor_id: str, member_id: str, kind: str,
                          amount: float, unit: str, due_at: str,
                          commitment_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "member_id": member_id, "kind": kind, "amount": amount,
                   "unit": unit, "due_at": due_at, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            member = self._member_row(connection, member_id)
            if member["status"] != "active":
                raise ConflictError("成员已退出，不能新增承诺")
            if kind not in COMMITMENT_KINDS:
                raise ValidationError("承诺类别无效")
            amount = self._positive_number(amount, "amount")
            unit = self._text(unit, "unit", 40)
            due_dt = _parse_time(due_at, "due_at")
            now = self._now_dt()
            charter = self._effective_charter(connection, now)
            if charter is None:
                raise ValidationError("章程尚未生效，不能登记承诺")
            if commitment_id is not None:
                commitment_id = self._identifier(commitment_id, "commitment_id")

            def create() -> dict[str, Any]:
                cid = commitment_id or uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,member_id,kind,amount,unit,due_at,"
                        "charter_version,status,created_at) VALUES(?,?,?,?,?,?,?,'open',?)",
                        (cid, member_id, kind, amount, unit, _fmt(due_dt),
                         charter["version"], self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="commitment.recorded",
                             resource_type="commitment", resource_id=cid,
                             detail={"member_id": member_id, "kind": kind, "amount": amount,
                                     "unit": unit, "due_at": _fmt(due_dt),
                                     "charter_version": charter["version"]},
                             occurred_at=self._now())
                return {"resource_type": "commitment", "resource_id": cid,
                        "commitment_id": cid, "charter_version": charter["version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.record_commitment", payload=payload, create=create)

    def _find_open_commitment(self, connection, member_id: str, kind: str):
        return connection.execute(
            "SELECT * FROM commitments WHERE member_id=? AND kind=? AND status='open' "
            "ORDER BY due_at, commitment_id LIMIT 1",
            (member_id, kind),
        ).fetchone()

    def _apply_contribution(self, connection, *, actor_id: str, member_id: str, kind: str,
                            amount: float, at: datetime, commitment,
                            source: dict[str, Any]) -> dict[str, Any]:
        """登记一笔实缴并推进承诺状态，返回贡献信息。"""

        late = False
        commitment_id = None
        if commitment is not None:
            commitment_id = commitment["commitment_id"]
            late = at > _parse_time(commitment["due_at"], "due_at")
        contribution_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO contributions(contribution_id,commitment_id,member_id,kind,amount,late,"
            "contributed_at) VALUES(?,?,?,?,?,?,?)",
            (contribution_id, commitment_id, member_id, kind, amount, 1 if late else 0, _fmt(at)),
        )
        fulfilled = False
        if commitment is not None:
            got = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM contributions WHERE commitment_id=?",
                (commitment_id,),
            ).fetchone()["total"]
            if got >= commitment["amount"]:
                connection.execute("UPDATE commitments SET status='fulfilled' WHERE commitment_id=?",
                                   (commitment_id,))
                fulfilled = True
        append_event(connection, actor_id=actor_id, action="contribution.recorded",
                     resource_type="contribution", resource_id=contribution_id,
                     detail={"member_id": member_id, "kind": kind, "amount": amount,
                             "commitment_id": commitment_id, "late": late,
                             "commitment_fulfilled": fulfilled, "source": source},
                     occurred_at=self._now())
        return {"contribution_id": contribution_id, "commitment_id": commitment_id,
                "late": late, "commitment_fulfilled": fulfilled}

    def record_contribution(self, *, request_id: str, actor_id: str, member_id: str, kind: str,
                            amount: float, commitment_id: str | None = None,
                            contributed_at: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "member_id": member_id, "kind": kind, "amount": amount,
                   "commitment_id": commitment_id, "contributed_at": contributed_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            self._member_row(connection, member_id)
            if kind not in COMMITMENT_KINDS:
                raise ValidationError("贡献类别无效")
            amount = self._positive_number(amount, "amount")
            at = _parse_time(contributed_at, "contributed_at") if contributed_at else self._now_dt()
            commitment = None
            if commitment_id is not None:
                commitment = connection.execute(
                    "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
                ).fetchone()
                if commitment is None:
                    raise NotFoundError("承诺不存在")
                if commitment["member_id"] != member_id or commitment["kind"] != kind:
                    raise ValidationError("承诺与成员或类别不匹配")
            else:
                commitment = self._find_open_commitment(connection, member_id, kind)

            def create() -> dict[str, Any]:
                info = self._apply_contribution(
                    connection, actor_id=actor["actor_id"], member_id=member_id, kind=kind,
                    amount=amount, at=at, commitment=commitment, source={"origin": "reported"})
                return {"resource_type": "contribution", "resource_id": info["contribution_id"],
                        **info}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.record_contribution", payload=payload, create=create)

    # ---- 仪器排期 ------------------------------------------------------

    def register_instrument(self, *, request_id: str, actor_id: str, instrument_id: str,
                            name: str, site_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "instrument_id": instrument_id, "name": name,
                   "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            instrument_id = self._identifier(instrument_id, "instrument_id")
            name = self._text(name, "name")
            if site_id is not None and connection.execute(
                    "SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create() -> dict[str, Any]:
                try:
                    connection.execute(
                        "INSERT INTO instruments(instrument_id,name,site_id,created_at) VALUES(?,?,?,?)",
                        (instrument_id, name, site_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("仪器编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="instrument.registered",
                             resource_type="instrument", resource_id=instrument_id,
                             detail={"name": name, "site_id": site_id}, occurred_at=self._now())
                return {"resource_type": "instrument", "resource_id": instrument_id,
                        "instrument_id": instrument_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.register_instrument", payload=payload, create=create)

    def schedule_slot(self, *, request_id: str, actor_id: str, instrument_id: str, member_id: str,
                      starts_at: str, ends_at: str, slot_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "instrument_id": instrument_id, "member_id": member_id,
                   "starts_at": starts_at, "ends_at": ends_at, "slot_id": slot_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            if connection.execute("SELECT 1 FROM instruments WHERE instrument_id=?",
                                  (instrument_id,)).fetchone() is None:
                raise NotFoundError("仪器不存在")
            member = self._member_row(connection, member_id)
            if member["status"] != "active":
                raise ConflictError("成员已退出，不能排期")
            start = _parse_time(starts_at, "starts_at")
            end = _parse_time(ends_at, "ends_at")
            if not start < end:
                raise ValidationError("时段起止无效")
            if slot_id is not None:
                slot_id = self._identifier(slot_id, "slot_id")

            def create() -> dict[str, Any]:
                overlap = connection.execute(
                    "SELECT slot_id FROM instrument_slots WHERE instrument_id=? "
                    "AND status IN ('scheduled','completed') AND NOT (ends_at<=? OR starts_at>=?)",
                    (instrument_id, _fmt(start), _fmt(end)),
                ).fetchone()
                if overlap:
                    raise ConflictError("仪器时段与既有排期重叠")
                sid = slot_id or uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO instrument_slots(slot_id,instrument_id,member_id,starts_at,ends_at,"
                        "status,created_at) VALUES(?,?,?,?,?,'scheduled',?)",
                        (sid, instrument_id, member_id, _fmt(start), _fmt(end), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("时段编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="slot.scheduled",
                             resource_type="instrument_slot", resource_id=sid,
                             detail={"instrument_id": instrument_id, "member_id": member_id,
                                     "starts_at": _fmt(start), "ends_at": _fmt(end)},
                             occurred_at=self._now())
                return {"resource_type": "instrument_slot", "resource_id": sid, "slot_id": sid}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.schedule_slot", payload=payload, create=create)

    def complete_slot(self, *, request_id: str, actor_id: str, slot_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "slot_id": slot_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            slot = connection.execute("SELECT * FROM instrument_slots WHERE slot_id=?",
                                      (slot_id,)).fetchone()
            if slot is None:
                raise NotFoundError("时段不存在")

            def create() -> dict[str, Any]:
                fresh = connection.execute("SELECT * FROM instrument_slots WHERE slot_id=?",
                                           (slot_id,)).fetchone()
                if fresh["status"] == "completed":
                    raise ConflictError("时段已完成，不能重复计数")
                if fresh["status"] == "cancelled":
                    raise ConflictError("时段已取消")
                connection.execute("UPDATE instrument_slots SET status='completed' WHERE slot_id=?",
                                   (slot_id,))
                start = _parse_time(fresh["starts_at"], "starts_at")
                end = _parse_time(fresh["ends_at"], "ends_at")
                hours = round((end - start).total_seconds() / 3600.0, 6)
                info = None
                if hours > 0:
                    commitment = self._find_open_commitment(connection, fresh["member_id"],
                                                            "instrument_time")
                    info = self._apply_contribution(
                        connection, actor_id=actor["actor_id"], member_id=fresh["member_id"],
                        kind="instrument_time", amount=hours, at=self._now_dt(),
                        commitment=commitment, source={"origin": "instrument_slot", "slot_id": slot_id})
                append_event(connection, actor_id=actor["actor_id"], action="slot.completed",
                             resource_type="instrument_slot", resource_id=slot_id,
                             detail={"instrument_id": fresh["instrument_id"],
                                     "member_id": fresh["member_id"], "hours": hours},
                             occurred_at=self._now())
                return {"resource_type": "instrument_slot", "resource_id": slot_id,
                        "slot_id": slot_id, "hours": hours,
                        "contribution_id": info["contribution_id"] if info else None}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.complete_slot", payload=payload, create=create)

    def cancel_slot(self, *, request_id: str, actor_id: str, slot_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "slot_id": slot_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            slot = connection.execute("SELECT * FROM instrument_slots WHERE slot_id=?",
                                      (slot_id,)).fetchone()
            if slot is None:
                raise NotFoundError("时段不存在")

            def create() -> dict[str, Any]:
                fresh = connection.execute("SELECT * FROM instrument_slots WHERE slot_id=?",
                                           (slot_id,)).fetchone()
                if fresh["status"] != "scheduled":
                    raise ConflictError("只有已排期未执行的时段可以取消")
                connection.execute("UPDATE instrument_slots SET status='cancelled' WHERE slot_id=?",
                                   (slot_id,))
                append_event(connection, actor_id=actor["actor_id"], action="slot.cancelled",
                             resource_type="instrument_slot", resource_id=slot_id,
                             detail={"instrument_id": fresh["instrument_id"]},
                             occurred_at=self._now())
                return {"resource_type": "instrument_slot", "resource_id": slot_id,
                        "slot_id": slot_id, "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.cancel_slot", payload=payload, create=create)

    # ---- 数据集与版本 --------------------------------------------------

    def register_dataset(self, *, request_id: str, actor_id: str, dataset_id: str,
                         title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            dataset_id = self._identifier(dataset_id, "dataset_id")
            title = self._text(title, "title")

            def create() -> dict[str, Any]:
                try:
                    connection.execute("INSERT INTO datasets(dataset_id,title,created_at) VALUES(?,?,?)",
                                       (dataset_id, title, self._now()))
                except Exception as exc:
                    raise ConflictError("数据集编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="dataset.registered",
                             resource_type="dataset", resource_id=dataset_id,
                             detail={"title": title}, occurred_at=self._now())
                return {"resource_type": "dataset", "resource_id": dataset_id,
                        "dataset_id": dataset_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.register_dataset", payload=payload, create=create)

    def _default_embargo(self, connection, sensitivity: str, at: datetime) -> datetime:
        charter = self._effective_charter(connection, at)
        days = charter["rules"]["embargo_days"].get(sensitivity, 0) if charter else 0
        return at + timedelta(days=days)

    def publish_version(self, *, request_id: str, actor_id: str, dataset_id: str, sensitivity: str,
                        embargo_until: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "sensitivity": sensitivity,
                   "embargo_until": embargo_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            if connection.execute("SELECT 1 FROM datasets WHERE dataset_id=?",
                                  (dataset_id,)).fetchone() is None:
                raise NotFoundError("数据集不存在")
            if sensitivity not in SENSITIVITY_LEVELS:
                raise ValidationError("敏感级别无效")
            now = self._now_dt()
            embargo_dt = (_parse_time(embargo_until, "embargo_until") if embargo_until
                          else self._default_embargo(connection, sensitivity, now))

            def create() -> dict[str, Any]:
                count = connection.execute(
                    "SELECT COUNT(*) AS c FROM dataset_versions WHERE dataset_id=?", (dataset_id,)
                ).fetchone()["c"]
                if count:
                    raise ConflictError("首版本已存在，后续请使用更正接口")
                connection.execute(
                    "INSERT INTO dataset_versions(dataset_id,version,sensitivity,embargo_until,status,"
                    "supersedes,note,created_at) VALUES(?,?,?,?,'active',NULL,NULL,?)",
                    (dataset_id, 1, sensitivity, _fmt(embargo_dt), self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="dataset_version.published",
                             resource_type="dataset_version", resource_id=f"{dataset_id}#1",
                             detail={"dataset_id": dataset_id, "version": 1,
                                     "sensitivity": sensitivity, "embargo_until": _fmt(embargo_dt)},
                             occurred_at=self._now())
                return {"resource_type": "dataset_version", "resource_id": f"{dataset_id}#1",
                        "dataset_id": dataset_id, "version": 1,
                        "embargo_until": _fmt(embargo_dt)}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.publish_version", payload=payload, create=create)

    def correct_dataset(self, *, request_id: str, actor_id: str, dataset_id: str, note: str,
                        sensitivity: str | None = None,
                        embargo_until: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "note": note,
                   "sensitivity": sensitivity, "embargo_until": embargo_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            if connection.execute("SELECT 1 FROM datasets WHERE dataset_id=?",
                                  (dataset_id,)).fetchone() is None:
                raise NotFoundError("数据集不存在")
            note = self._text(note, "note", 500)
            now = self._now_dt()

            def create() -> dict[str, Any]:
                current = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                    (dataset_id,),
                ).fetchone()
                if current is None:
                    raise ValidationError("数据集尚未发布版本")
                new_sensitivity = sensitivity or current["sensitivity"]
                if new_sensitivity not in SENSITIVITY_LEVELS:
                    raise ValidationError("敏感级别无效")
                embargo_dt = (_parse_time(embargo_until, "embargo_until") if embargo_until
                              else self._default_embargo(connection, new_sensitivity, now))
                new_version = current["version"] + 1
                connection.execute(
                    "UPDATE dataset_versions SET status='superseded' WHERE dataset_id=? AND version=?",
                    (dataset_id, current["version"]),
                )
                connection.execute(
                    "INSERT INTO dataset_versions(dataset_id,version,sensitivity,embargo_until,status,"
                    "supersedes,note,created_at) VALUES(?,?,?,?,'active',?,?,?)",
                    (dataset_id, new_version, new_sensitivity, _fmt(embargo_dt),
                     current["version"], note, self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="dataset_version.corrected",
                             resource_type="dataset_version", resource_id=f"{dataset_id}#{new_version}",
                             detail={"dataset_id": dataset_id, "from_version": current["version"],
                                     "to_version": new_version, "note": note,
                                     "sensitivity": new_sensitivity,
                                     "embargo_until": _fmt(embargo_dt)},
                             occurred_at=self._now())
                return {"resource_type": "dataset_version",
                        "resource_id": f"{dataset_id}#{new_version}",
                        "dataset_id": dataset_id, "version": new_version,
                        "supersedes": current["version"], "embargo_until": _fmt(embargo_dt)}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.correct_dataset", payload=payload, create=create)

    def suspend_version(self, *, request_id: str, actor_id: str, dataset_id: str, version: int,
                        reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "version": version,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            reason = self._text(reason, "reason", 500)
            row = connection.execute(
                "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
                (dataset_id, version),
            ).fetchone()
            if row is None:
                raise NotFoundError("数据版本不存在")

            def create() -> dict[str, Any]:
                fresh = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
                    (dataset_id, version),
                ).fetchone()
                if fresh["status"] != "active":
                    raise ConflictError("只有有效版本可以暂停")
                connection.execute(
                    "UPDATE dataset_versions SET status='suspended' WHERE dataset_id=? AND version=?",
                    (dataset_id, version),
                )
                append_event(connection, actor_id=actor["actor_id"], action="dataset_version.suspended",
                             resource_type="dataset_version", resource_id=f"{dataset_id}#{version}",
                             detail={"reason": reason}, occurred_at=self._now())
                return {"resource_type": "dataset_version", "resource_id": f"{dataset_id}#{version}",
                        "dataset_id": dataset_id, "version": version, "status": "suspended"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.suspend_version", payload=payload, create=create)

    # ---- 使用提案与访问决定 ---------------------------------------------

    def _evaluate(self, connection, *, member, version_row, charter: dict[str, Any],
                  standing: dict[str, Any], third_party_requested: bool,
                  at: datetime) -> dict[str, Any]:
        rules = charter["rules"]
        checks: list[dict[str, Any]] = []
        reasons: list[str] = []
        gates: list[str] = []

        def check(rule: str, passed: bool, **detail: Any) -> bool:
            checks.append({"rule": rule, "passed": bool(passed), **detail})
            return passed

        dataset_id = version_row["dataset_id"]
        if not check("dataset_version_active", version_row["status"] == "active",
                     version=version_row["version"], status=version_row["status"]):
            reasons.append("数据版本不可用（已更正或暂停）")
        fulfillment = standing["fulfillment_ratio"]
        if not check("fulfillment_threshold", fulfillment >= rules["access_min_fulfillment"],
                     required=rules["access_min_fulfillment"], actual=fulfillment):
            reasons.append("实缴贡献完成率低于章程要求")
        if rules["require_contributions"]:
            if not check("has_contributions", standing["total_contributed"] > 0,
                         actual=standing["total_contributed"]):
                reasons.append("尚无实缴贡献记录")
        embargo_until = _parse_time(version_row["embargo_until"], "embargo_until")
        if at < embargo_until:
            if not check("embargo_window", fulfillment >= rules["embargo_access_fulfillment"],
                         embargo_until=version_row["embargo_until"],
                         required=rules["embargo_access_fulfillment"], actual=fulfillment):
                reasons.append("禁运期内实缴完成率未达到提前访问要求")
        else:
            check("embargo_window", True, embargo_until=version_row["embargo_until"],
                  in_embargo=False)
        policy = rules["third_party_transfer"]
        if third_party_requested:
            if policy == "prohibited":
                check("third_party_transfer", False, policy=policy)
                reasons.append("章程禁止向第三方转交")
            elif policy == "resolution":
                check("third_party_transfer", False, policy=policy)
                gates.append("第三方转交需治理决议批准")
            else:
                check("third_party_transfer", True, policy=policy)
        else:
            check("third_party_transfer", True, policy=policy, requested=False)
        if version_row["sensitivity"] == "sensitive" and rules["sensitive_requires_resolution"]:
            check("sensitive_resolution", False, sensitivity="sensitive")
            gates.append("敏感级数据需治理决议批准")
        else:
            check("sensitive_resolution", True, sensitivity=version_row["sensitivity"])
        existing = connection.execute(
            "SELECT license_id, status FROM licenses WHERE member_id=? AND dataset_id=? "
            "AND status IN ('active','suspended')",
            (member["member_id"], dataset_id),
        ).fetchone()
        if not check("no_existing_license", existing is None,
                     existing_license_id=existing["license_id"] if existing else None):
            reasons.append("已存在有效许可，不能重复授权")
        if reasons:
            outcome = "denied"
        elif gates:
            outcome = "pending"
        else:
            outcome = "approved"
        return {"charter_version": charter["version"], "evaluated_at": _fmt(at),
                "snapshot": standing, "checks": checks, "outcome": outcome,
                "reasons": reasons, "gates": gates}

    def _expire_licenses(self, connection, at: datetime, *, member_id: str | None = None,
                         dataset_id: str | None = None) -> None:
        query = "SELECT * FROM licenses WHERE status='active'"
        params: list[Any] = []
        if member_id:
            query += " AND member_id=?"
            params.append(member_id)
        if dataset_id:
            query += " AND dataset_id=?"
            params.append(dataset_id)
        for lic in connection.execute(query, params).fetchall():
            if _parse_time(lic["expires_at"], "expires_at") <= at:
                connection.execute(
                    "UPDATE licenses SET status='expired', revision=revision+1,"
                    " status_note='expired', updated_at=? WHERE license_id=? AND revision=?",
                    (self._now(), lic["license_id"], lic["revision"]),
                )
                append_event(connection, actor_id="system", action="license.expired",
                             resource_type="license", resource_id=lic["license_id"],
                             detail={"member_id": lic["member_id"], "dataset_id": lic["dataset_id"],
                                     "expires_at": lic["expires_at"]},
                             occurred_at=self._now())

    def _grant_license(self, connection, *, proposal_id: str, member_id: str, dataset_id: str,
                       dataset_version: int, scope: str, license_days: int, at: datetime,
                       actor_id: str, resolution_id: str | None = None) -> str:
        existing = connection.execute(
            "SELECT license_id FROM licenses WHERE member_id=? AND dataset_id=? "
            "AND status IN ('active','suspended')",
            (member_id, dataset_id),
        ).fetchone()
        if existing:
            raise ConflictError("该成员在此数据集上已存在有效许可")
        license_id = uuid.uuid4().hex
        expires = at + timedelta(days=int(license_days))
        try:
            connection.execute(
                "INSERT INTO licenses(license_id,proposal_id,member_id,dataset_id,dataset_version,"
                "scope,status,revision,status_note,granted_at,expires_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'active',1,NULL,?,?,?)",
                (license_id, proposal_id, member_id, dataset_id, dataset_version, scope,
                 _fmt(at), _fmt(expires), self._now()),
            )
        except Exception as exc:
            raise ConflictError("该成员在此数据集上已存在有效许可") from exc
        append_event(connection, actor_id=actor_id, action="license.granted",
                     resource_type="license", resource_id=license_id,
                     detail={"proposal_id": proposal_id, "member_id": member_id,
                             "dataset_id": dataset_id, "dataset_version": dataset_version,
                             "scope": scope, "resolution_id": resolution_id,
                             "expires_at": _fmt(expires)},
                     occurred_at=self._now())
        return license_id

    def submit_proposal(self, *, request_id: str, actor_id: str, dataset_id: str, purpose: str,
                        dataset_version: int | None = None, third_party_transfer: bool = False,
                        proposal_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "purpose": purpose,
                   "dataset_version": dataset_version, "third_party_transfer": third_party_transfer,
                   "proposal_id": proposal_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REPRESENTATIVE_ROLES)
            member = self._member_for_actor(connection, actor)
            if member["status"] != "active":
                raise PermissionDenied("成员已退出，不能提交新提案")
            if connection.execute("SELECT 1 FROM datasets WHERE dataset_id=?",
                                  (dataset_id,)).fetchone() is None:
                raise NotFoundError("数据集不存在")
            if dataset_version is not None:
                version_row = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
                    (dataset_id, dataset_version),
                ).fetchone()
                if version_row is None:
                    raise NotFoundError("数据版本不存在")
            else:
                version_row = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                    (dataset_id,),
                ).fetchone()
                if version_row is None:
                    raise ValidationError("数据集尚未发布版本")
            purpose = self._text(purpose, "purpose", 500)
            now = self._now_dt()
            charter = self._effective_charter(connection, now)
            if charter is None:
                raise ValidationError("章程尚未生效，不能提交提案")
            self._expire_licenses(connection, now, member_id=member["member_id"],
                                  dataset_id=dataset_id)
            standing = self._standing(connection, member["member_id"], now)
            transfer = bool(third_party_transfer)
            decision = self._evaluate(connection, member=member, version_row=version_row,
                                      charter=charter, standing=standing,
                                      third_party_requested=transfer, at=now)
            snapshot = {"charter_version": charter["version"], "charter_rules": charter["rules"],
                        "standing": standing}
            if proposal_id is not None:
                proposal_id = self._identifier(proposal_id, "proposal_id")

            def create() -> dict[str, Any]:
                pid = proposal_id or uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO proposals(proposal_id,member_id,dataset_id,dataset_version,"
                        "purpose,third_party_transfer,submitted_at,charter_version,snapshot_json,"
                        "status,decision_json,license_id,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                        (pid, member["member_id"], dataset_id, version_row["version"], purpose,
                         1 if transfer else 0, self._now(), charter["version"],
                         canonical_json(snapshot), decision["outcome"],
                         canonical_json(decision)),
                    )
                except Exception as exc:
                    raise ConflictError("提案编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="proposal.submitted",
                             resource_type="proposal", resource_id=pid,
                             detail={"member_id": member["member_id"], "dataset_id": dataset_id,
                                     "dataset_version": version_row["version"],
                                     "outcome": decision["outcome"],
                                     "charter_version": charter["version"]},
                             occurred_at=self._now())
                license_id = None
                if decision["outcome"] == "approved":
                    scope = "read+transfer" if transfer else "read"
                    license_id = self._grant_license(
                        connection, proposal_id=pid, member_id=member["member_id"],
                        dataset_id=dataset_id, dataset_version=version_row["version"],
                        scope=scope, license_days=charter["rules"]["license_days"],
                        at=now, actor_id=actor["actor_id"])
                    connection.execute(
                        "UPDATE proposals SET license_id=?, decided_at=? WHERE proposal_id=?",
                        (license_id, self._now(), pid),
                    )
                return {"resource_type": "proposal", "resource_id": pid, "proposal_id": pid,
                        "status": decision["outcome"], "decision": decision,
                        "license_id": license_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.submit_proposal", payload=payload, create=create)

    # ---- 治理决议 ------------------------------------------------------

    def _subject_dataset(self, connection, resolution) -> str | None:
        subject = json.loads(resolution["subject_json"])
        kind = resolution["kind"]
        if kind in ("suspend_license", "resume_license"):
            row = connection.execute("SELECT dataset_id FROM licenses WHERE license_id=?",
                                     (subject.get("license_id"),)).fetchone()
            return row["dataset_id"] if row else None
        if kind in ("approve_proposal", "approve_transfer"):
            row = connection.execute("SELECT dataset_id FROM proposals WHERE proposal_id=?",
                                     (subject.get("proposal_id"),)).fetchone()
            return row["dataset_id"] if row else None
        return None

    def _validate_subject(self, connection, kind: str, subject: Any) -> dict[str, Any]:
        if not isinstance(subject, dict):
            raise ValidationError("subject 必须是对象")
        if kind == "amend_charter":
            charter_id = subject.get("charter_id")
            row = connection.execute("SELECT * FROM charter_versions WHERE charter_id=?",
                                     (charter_id,)).fetchone()
            if row is None:
                raise NotFoundError("章程版本不存在")
            if row["status"] != "draft":
                raise ConflictError("只有草案状态的章程可以提交表决")
        elif kind in ("suspend_license", "resume_license"):
            row = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                     (subject.get("license_id"),)).fetchone()
            if row is None:
                raise NotFoundError("许可不存在")
            expected = "active" if kind == "suspend_license" else "suspended"
            if row["status"] != expected:
                raise ConflictError("许可当前状态不支持该决议")
        elif kind in ("approve_proposal", "approve_transfer"):
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                     (subject.get("proposal_id"),)).fetchone()
            if row is None:
                raise NotFoundError("提案不存在")
            if row["status"] != "pending":
                raise ConflictError("提案不在待决状态")
        elif kind == "admit_member":
            row = connection.execute("SELECT * FROM members WHERE member_id=?",
                                     (subject.get("member_id"),)).fetchone()
            if row is None:
                raise NotFoundError("成员不存在")
            if row["status"] != "withdrawn":
                raise ConflictError("成员不在退出状态")
        return subject

    def open_resolution(self, *, request_id: str, actor_id: str, kind: str,
                        subject: dict[str, Any], closes_at: str,
                        resolution_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "kind": kind, "subject": subject,
                   "closes_at": closes_at, "resolution_id": resolution_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            if kind not in RESOLUTION_KINDS:
                raise ValidationError("决议类别无效")
            now = self._now_dt()
            closes = _parse_time(closes_at, "closes_at")
            if closes <= now:
                raise ValidationError("截止时间必须晚于当前时间")
            charter = self._effective_charter(connection, now)
            if charter is None:
                raise ValidationError("章程尚未生效，不能开启决议")
            subject = self._validate_subject(connection, kind, subject)
            weights = {row["member_id"]: row["voting_weight"] for row in connection.execute(
                "SELECT member_id, voting_weight FROM members WHERE status='active'")}
            if not weights:
                raise ValidationError("没有可表决成员")
            if resolution_id is not None:
                resolution_id = self._identifier(resolution_id, "resolution_id")

            def create() -> dict[str, Any]:
                rid = resolution_id or uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO resolutions(resolution_id,kind,subject_json,opened_at,closes_at,"
                        "quorum_fraction,weights_json,status,tally_json,decided_at) "
                        "VALUES(?,?,?,?,?,?,?,'open',NULL,NULL)",
                        (rid, kind, canonical_json(subject), self._now(), _fmt(closes),
                         charter["rules"]["quorum_fraction"], canonical_json(weights)),
                    )
                except Exception as exc:
                    raise ConflictError("决议编号已经存在") from exc
                append_event(connection, actor_id=actor["actor_id"], action="resolution.opened",
                             resource_type="resolution", resource_id=rid,
                             detail={"kind": kind, "subject": subject,
                                     "quorum_fraction": charter["rules"]["quorum_fraction"],
                                     "weights": weights, "closes_at": _fmt(closes),
                                     "charter_version": charter["version"]},
                             occurred_at=self._now())
                return {"resource_type": "resolution", "resource_id": rid,
                        "resolution_id": rid, "status": "open",
                        "quorum_fraction": charter["rules"]["quorum_fraction"],
                        "weights": weights}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.open_resolution", payload=payload, create=create)

    def _has_conflict(self, connection, member_id: str, resolution) -> bool:
        dataset_id = self._subject_dataset(connection, resolution)
        row = connection.execute(
            "SELECT 1 FROM conflict_declarations WHERE member_id=? "
            "AND (resolution_id=? OR (dataset_id IS NOT NULL AND dataset_id=?)) LIMIT 1",
            (member_id, resolution["resolution_id"], dataset_id or ""),
        ).fetchone()
        return row is not None

    def declare_conflict(self, *, request_id: str, actor_id: str, reason: str,
                         resolution_id: str | None = None, dataset_id: str | None = None,
                         member_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "reason": reason, "resolution_id": resolution_id,
                   "dataset_id": dataset_id, "member_id": member_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor["role"] in REPRESENTATIVE_ROLES:
                member = self._member_for_actor(connection, actor)
                member_id = member["member_id"]
            elif actor["role"] in SECRETARIAT_ROLES:
                if not member_id:
                    raise ValidationError("member_id 不能为空")
                self._member_row(connection, member_id)
            else:
                raise PermissionDenied("当前角色不能申报利益冲突")
            reason = self._text(reason, "reason", 500)
            if not resolution_id and not dataset_id:
                raise ValidationError("必须指定 resolution_id 或 dataset_id")
            if resolution_id and connection.execute(
                    "SELECT 1 FROM resolutions WHERE resolution_id=?",
                    (resolution_id,)).fetchone() is None:
                raise NotFoundError("决议不存在")
            if dataset_id and connection.execute(
                    "SELECT 1 FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone() is None:
                raise NotFoundError("数据集不存在")

            def create() -> dict[str, Any]:
                cursor = connection.execute(
                    "INSERT INTO conflict_declarations(member_id,resolution_id,dataset_id,reason,"
                    "declared_by,declared_at) VALUES(?,?,?,?,?,?)",
                    (member_id, resolution_id, dataset_id, reason, actor["actor_id"], self._now()),
                )
                declaration_id = str(cursor.lastrowid)
                append_event(connection, actor_id=actor["actor_id"], action="conflict.declared",
                             resource_type="conflict_declaration", resource_id=declaration_id,
                             detail={"member_id": member_id, "resolution_id": resolution_id,
                                     "dataset_id": dataset_id, "reason": reason},
                             occurred_at=self._now())
                return {"resource_type": "conflict_declaration", "resource_id": declaration_id,
                        "declaration_id": declaration_id, "member_id": member_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.declare_conflict", payload=payload, create=create)

    def cast_vote(self, *, request_id: str, actor_id: str, resolution_id: str,
                  choice: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "resolution_id": resolution_id, "choice": choice}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REPRESENTATIVE_ROLES)
            member = self._member_for_actor(connection, actor)
            resolution = connection.execute("SELECT * FROM resolutions WHERE resolution_id=?",
                                            (resolution_id,)).fetchone()
            if resolution is None:
                raise NotFoundError("决议不存在")
            if resolution["status"] != "open":
                raise ConflictError("决议已关闭")
            now = self._now_dt()
            if now >= _parse_time(resolution["closes_at"], "closes_at"):
                raise ConflictError("表决已截止")
            weights = json.loads(resolution["weights_json"])
            if member["member_id"] not in weights:
                raise PermissionDenied("该成员在决议开启时不具备表决权")
            if member["status"] != "active":
                raise PermissionDenied("成员已退出，不能表决")
            if choice not in VOTE_CHOICES:
                raise ValidationError("表决选项无效")
            recused = self._has_conflict(connection, member["member_id"], resolution)

            def create() -> dict[str, Any]:
                existing = connection.execute(
                    "SELECT * FROM resolution_votes WHERE resolution_id=? AND member_id=?",
                    (resolution_id, member["member_id"]),
                ).fetchone()
                if existing:
                    if existing["choice"] != choice:
                        raise ConflictError("该成员已投出不同选择，不能重复表决")
                    return {"resource_type": "resolution_vote",
                            "resource_id": f"{resolution_id}:{member['member_id']}",
                            "resolution_id": resolution_id, "member_id": member["member_id"],
                            "choice": existing["choice"], "recused": bool(existing["recused"]),
                            "counted": False}
                connection.execute(
                    "INSERT INTO resolution_votes(resolution_id,member_id,actor_id,choice,recused,"
                    "cast_at) VALUES(?,?,?,?,?,?)",
                    (resolution_id, member["member_id"], actor["actor_id"], choice,
                     1 if recused else 0, self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="resolution.vote_cast",
                             resource_type="resolution", resource_id=resolution_id,
                             detail={"member_id": member["member_id"], "choice": choice,
                                     "recused": recused},
                             occurred_at=self._now())
                return {"resource_type": "resolution_vote",
                        "resource_id": f"{resolution_id}:{member['member_id']}",
                        "resolution_id": resolution_id, "member_id": member["member_id"],
                        "choice": choice, "recused": recused, "counted": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.cast_vote", payload=payload, create=create)

    def _tally(self, connection, resolution) -> dict[str, Any]:
        weights = json.loads(resolution["weights_json"])
        votes = connection.execute(
            "SELECT * FROM resolution_votes WHERE resolution_id=?", (resolution["resolution_id"],)
        ).fetchall()
        counted = [v for v in votes if not v["recused"]]
        recused_members = [v["member_id"] for v in votes if v["recused"]]
        participation = sum(weights.get(v["member_id"], 0.0) for v in counted)
        yes = sum(weights.get(v["member_id"], 0.0) for v in counted if v["choice"] == "yes")
        no = sum(weights.get(v["member_id"], 0.0) for v in counted if v["choice"] == "no")
        abstain = sum(weights.get(v["member_id"], 0.0) for v in counted if v["choice"] == "abstain")
        total = sum(weights.values())
        quorum_ok = total > 0 and participation >= resolution["quorum_fraction"] * total
        return {"total_weight": total, "participation_weight": participation,
                "yes_weight": yes, "no_weight": no, "abstain_weight": abstain,
                "recused_members": recused_members, "quorum_fraction": resolution["quorum_fraction"],
                "quorum_ok": quorum_ok, "passed": quorum_ok and yes > no}

    def _apply_resolution(self, connection, resolution, actor, at: datetime) -> dict[str, Any]:
        subject = json.loads(resolution["subject_json"])
        kind = resolution["kind"]
        rid = resolution["resolution_id"]
        if kind == "amend_charter":
            charter = connection.execute("SELECT * FROM charter_versions WHERE charter_id=?",
                                         (subject.get("charter_id"),)).fetchone()
            if charter is None or charter["status"] != "draft":
                return {"applied": False, "note": "章程版本已不是草案"}
            connection.execute("UPDATE charter_versions SET status='superseded' WHERE status='effective'")
            connection.execute(
                "UPDATE charter_versions SET status='effective', effective_from=? WHERE charter_id=?",
                (_fmt(at), charter["charter_id"]),
            )
            append_event(connection, actor_id=actor["actor_id"], action="charter.activated",
                         resource_type="charter", resource_id=charter["charter_id"],
                         detail={"version": charter["version"], "resolution_id": rid},
                         occurred_at=self._now())
            return {"applied": True, "charter_id": charter["charter_id"],
                    "version": charter["version"]}
        if kind == "suspend_license":
            lic = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                     (subject.get("license_id"),)).fetchone()
            if lic is None or lic["status"] != "active":
                return {"applied": False, "note": "许可不在有效状态"}
            connection.execute(
                "UPDATE licenses SET status='suspended', revision=revision+1, status_note=?,"
                " updated_at=? WHERE license_id=? AND revision=?",
                (f"resolution:{rid}", self._now(), lic["license_id"], lic["revision"]),
            )
            append_event(connection, actor_id=actor["actor_id"], action="license.suspended",
                         resource_type="license", resource_id=lic["license_id"],
                         detail={"resolution_id": rid, "member_id": lic["member_id"]},
                         occurred_at=self._now())
            return {"applied": True, "license_id": lic["license_id"]}
        if kind == "resume_license":
            lic = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                     (subject.get("license_id"),)).fetchone()
            if lic is None or lic["status"] != "suspended":
                return {"applied": False, "note": "许可不在暂停状态"}
            conflict = connection.execute(
                "SELECT license_id FROM licenses WHERE member_id=? AND dataset_id=? "
                "AND status='active' AND license_id<>?",
                (lic["member_id"], lic["dataset_id"], lic["license_id"]),
            ).fetchone()
            if conflict:
                return {"applied": False, "note": "已存在其他有效许可，不能恢复"}
            connection.execute(
                "UPDATE licenses SET status='active', revision=revision+1, status_note=NULL,"
                " updated_at=? WHERE license_id=? AND revision=?",
                (self._now(), lic["license_id"], lic["revision"]),
            )
            append_event(connection, actor_id=actor["actor_id"], action="license.resumed",
                         resource_type="license", resource_id=lic["license_id"],
                         detail={"resolution_id": rid, "member_id": lic["member_id"]},
                         occurred_at=self._now())
            return {"applied": True, "license_id": lic["license_id"]}
        if kind in ("approve_proposal", "approve_transfer"):
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                          (subject.get("proposal_id"),)).fetchone()
            if proposal is None or proposal["status"] != "pending":
                return {"applied": False, "note": "提案不在待决状态"}
            snapshot = json.loads(proposal["snapshot_json"])
            license_days = snapshot["charter_rules"]["license_days"]
            self._expire_licenses(connection, at, member_id=proposal["member_id"],
                                  dataset_id=proposal["dataset_id"])
            existing = connection.execute(
                "SELECT license_id FROM licenses WHERE member_id=? AND dataset_id=? "
                "AND status IN ('active','suspended')",
                (proposal["member_id"], proposal["dataset_id"]),
            ).fetchone()
            if existing:
                license_id = existing["license_id"]
            else:
                scope = "read+transfer" if proposal["third_party_transfer"] else "read"
                license_id = self._grant_license(
                    connection, proposal_id=proposal["proposal_id"],
                    member_id=proposal["member_id"], dataset_id=proposal["dataset_id"],
                    dataset_version=proposal["dataset_version"], scope=scope,
                    license_days=license_days, at=at, actor_id=actor["actor_id"],
                    resolution_id=rid)
            decision = json.loads(proposal["decision_json"])
            decision["outcome"] = "approved"
            decision["resolution_id"] = rid
            decision["reasons"] = []
            decision["gates"] = []
            connection.execute(
                "UPDATE proposals SET status='approved', license_id=?, decided_at=?,"
                " decision_json=? WHERE proposal_id=?",
                (license_id, self._now(), canonical_json(decision), proposal["proposal_id"]),
            )
            append_event(connection, actor_id=actor["actor_id"], action="proposal.approved",
                         resource_type="proposal", resource_id=proposal["proposal_id"],
                         detail={"resolution_id": rid, "license_id": license_id},
                         occurred_at=self._now())
            return {"applied": True, "proposal_id": proposal["proposal_id"],
                    "license_id": license_id}
        if kind == "admit_member":
            member = connection.execute("SELECT * FROM members WHERE member_id=?",
                                        (subject.get("member_id"),)).fetchone()
            if member is None or member["status"] != "withdrawn":
                return {"applied": False, "note": "成员不在退出状态"}
            connection.execute(
                "UPDATE members SET status='active', withdrawn_at=NULL WHERE member_id=?",
                (member["member_id"],),
            )
            append_event(connection, actor_id=actor["actor_id"], action="member.readmitted",
                         resource_type="member", resource_id=member["member_id"],
                         detail={"resolution_id": rid}, occurred_at=self._now())
            return {"applied": True, "member_id": member["member_id"]}
        return {"applied": False, "note": "未知决议类别"}

    def close_resolution(self, *, request_id: str, actor_id: str,
                         resolution_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "resolution_id": resolution_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            resolution = connection.execute("SELECT * FROM resolutions WHERE resolution_id=?",
                                            (resolution_id,)).fetchone()
            if resolution is None:
                raise NotFoundError("决议不存在")

            def create() -> dict[str, Any]:
                fresh = connection.execute("SELECT * FROM resolutions WHERE resolution_id=?",
                                           (resolution_id,)).fetchone()
                if fresh["status"] != "open":
                    raise ConflictError("决议已关闭")
                now = self._now_dt()
                weights = json.loads(fresh["weights_json"])
                voted = {row["member_id"] for row in connection.execute(
                    "SELECT member_id FROM resolution_votes WHERE resolution_id=?",
                    (resolution_id,))}
                if now < _parse_time(fresh["closes_at"], "closes_at") and not set(weights) <= voted:
                    raise ConflictError("尚未到截止时间且表决未完结")
                tally = self._tally(connection, fresh)
                status = "passed" if tally["passed"] else "failed"
                connection.execute(
                    "UPDATE resolutions SET status=?, tally_json=?, decided_at=? WHERE resolution_id=?",
                    (status, canonical_json(tally), self._now(), resolution_id),
                )
                effect = None
                if status == "passed":
                    effect = self._apply_resolution(connection, fresh, actor, now)
                append_event(connection, actor_id=actor["actor_id"], action="resolution.closed",
                             resource_type="resolution", resource_id=resolution_id,
                             detail={"status": status, "tally": tally, "effect": effect},
                             occurred_at=self._now())
                return {"resource_type": "resolution", "resource_id": resolution_id,
                        "resolution_id": resolution_id, "status": status,
                        "tally": tally, "effect": effect}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.close_resolution", payload=payload, create=create)

    # ---- 许可直接管理 ----------------------------------------------------

    def _set_license_status(self, connection, actor, license_id: str, expected: str,
                            target: str, note: str | None, action: str) -> dict[str, Any]:
        lic = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                 (license_id,)).fetchone()
        if lic is None:
            raise NotFoundError("许可不存在")

        def create() -> dict[str, Any]:
            fresh = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                       (license_id,)).fetchone()
            if fresh["status"] != expected:
                raise ConflictError("许可当前状态不支持该操作")
            if target == "active":
                conflict = connection.execute(
                    "SELECT license_id FROM licenses WHERE member_id=? AND dataset_id=? "
                    "AND status='active' AND license_id<>?",
                    (fresh["member_id"], fresh["dataset_id"], license_id),
                ).fetchone()
                if conflict:
                    raise ConflictError("已存在其他有效许可，不能恢复")
            updated = connection.execute(
                "UPDATE licenses SET status=?, revision=revision+1, status_note=?, updated_at=? "
                "WHERE license_id=? AND revision=?",
                (target, note, self._now(), license_id, fresh["revision"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("许可状态已变化，请重试")
            append_event(connection, actor_id=actor["actor_id"], action=action,
                         resource_type="license", resource_id=license_id,
                         detail={"member_id": fresh["member_id"], "dataset_id": fresh["dataset_id"],
                                 "note": note},
                         occurred_at=self._now())
            return {"resource_type": "license", "resource_id": license_id,
                    "license_id": license_id, "status": target}

        return create()

    def suspend_license(self, *, request_id: str, actor_id: str, license_id: str,
                        reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "license_id": license_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            reason = self._text(reason, "reason", 500)
            result = self._set_license_status(connection, actor, license_id, "active",
                                              "suspended", reason, "license.suspended")
            return self._idempotent(connection, request_id=request_id,
                                    action="governance.suspend_license", payload=payload,
                                    create=lambda: result)

    def resume_license(self, *, request_id: str, actor_id: str,
                       license_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "license_id": license_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARIAT_ROLES)
            result = self._set_license_status(connection, actor, license_id, "suspended",
                                              "active", None, "license.resumed")
            return self._idempotent(connection, request_id=request_id,
                                    action="governance.resume_license", payload=payload,
                                    create=lambda: result)

    # ---- 下载回调 ------------------------------------------------------

    def record_download(self, *, request_id: str, actor_id: str, callback_id: str,
                        license_id: str, byte_count: int = 0) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "callback_id": callback_id, "license_id": license_id,
                   "byte_count": byte_count}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            lic = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                     (license_id,)).fetchone()
            if lic is None:
                raise NotFoundError("许可不存在")
            member = self._member_row(connection, lic["member_id"])
            if actor["role"] in REPRESENTATIVE_ROLES:
                if actor["organization_id"] != member["organization_id"]:
                    raise PermissionDenied("不能记录其他成员的下载")
            elif actor["role"] not in SECRETARIAT_ROLES:
                raise PermissionDenied("当前角色不能记录下载")
            callback_id = self._identifier(callback_id, "callback_id")
            if isinstance(byte_count, bool):
                raise ValidationError("byte_count 必须是非负整数")
            try:
                bytes_value = int(byte_count)
            except (TypeError, ValueError) as exc:
                raise ValidationError("byte_count 必须是非负整数") from exc
            if bytes_value < 0:
                raise ValidationError("byte_count 必须是非负整数")

            def create() -> dict[str, Any]:
                duplicate = connection.execute(
                    "SELECT * FROM download_events WHERE callback_id=?", (callback_id,)
                ).fetchone()
                if duplicate:
                    if duplicate["license_id"] != license_id:
                        raise ConflictError("回调编号已被其他许可使用")
                    return {"resource_type": "download", "resource_id": callback_id,
                            "callback_id": callback_id, "license_id": license_id,
                            "dataset_id": duplicate["dataset_id"],
                            "dataset_version": duplicate["dataset_version"], "counted": False}
                now = self._now_dt()
                self._expire_licenses(connection, now, member_id=lic["member_id"],
                                      dataset_id=lic["dataset_id"])
                fresh = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                           (license_id,)).fetchone()
                if fresh["status"] != "active":
                    raise PermissionDenied(f"许可状态为 {fresh['status']}，不能下载")
                version = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                    (lic["dataset_id"],),
                ).fetchone()
                if version is None or version["status"] != "active":
                    raise ConflictError("当前数据版本不可用")
                charter = self._effective_charter(connection, now)
                embargo_until = _parse_time(version["embargo_until"], "embargo_until")
                if charter and now < embargo_until:
                    standing = self._standing(connection, lic["member_id"], now)
                    required = charter["rules"]["embargo_access_fulfillment"]
                    if standing["fulfillment_ratio"] < required:
                        raise PermissionDenied("禁运期内实缴贡献不足，暂缓下载")
                connection.execute(
                    "INSERT INTO download_events(callback_id,license_id,member_id,dataset_id,"
                    "dataset_version,bytes,occurred_at) VALUES(?,?,?,?,?,?,?)",
                    (callback_id, license_id, lic["member_id"], lic["dataset_id"],
                     version["version"], bytes_value, self._now()),
                )
                append_event(connection, actor_id=actor["actor_id"], action="download.recorded",
                             resource_type="download", resource_id=callback_id,
                             detail={"license_id": license_id, "member_id": lic["member_id"],
                                     "dataset_id": lic["dataset_id"],
                                     "dataset_version": version["version"], "bytes": bytes_value},
                             occurred_at=self._now())
                return {"resource_type": "download", "resource_id": callback_id,
                        "callback_id": callback_id, "license_id": license_id,
                        "dataset_id": lic["dataset_id"], "dataset_version": version["version"],
                        "counted": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.record_download", payload=payload, create=create)

    # ---- 成果发表与署名 --------------------------------------------------

    def _attribution(self, connection, rules: dict[str, Any]) -> list[dict[str, Any]]:
        totals = {kind: 0.0 for kind in COMMITMENT_KINDS}
        per_member: dict[str, dict[str, float]] = {}
        for row in connection.execute(
                "SELECT member_id, kind, SUM(amount) AS total FROM contributions GROUP BY member_id, kind"):
            totals[row["kind"]] += row["total"]
            per_member.setdefault(row["member_id"], {kind: 0.0 for kind in COMMITMENT_KINDS})
            per_member[row["member_id"]][row["kind"]] += row["total"]
        result = []
        for member in connection.execute("SELECT * FROM members ORDER BY joined_at, member_id"):
            own = per_member.get(member["member_id"],
                                 {kind: 0.0 for kind in COMMITMENT_KINDS})
            ratios = [own[kind] / totals[kind] for kind in COMMITMENT_KINDS if totals[kind] > 0]
            share = round(sum(ratios) / len(ratios), 6) if ratios else 0.0
            result.append({"member_id": member["member_id"], "share": share,
                           "eligible": share >= rules["authorship_min_share"]})
        return result

    def register_publication(self, *, request_id: str, actor_id: str, license_id: str,
                             title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "license_id": license_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            lic = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                     (license_id,)).fetchone()
            if lic is None:
                raise NotFoundError("许可不存在")
            member = self._member_row(connection, lic["member_id"])
            if actor["role"] in REPRESENTATIVE_ROLES:
                if actor["organization_id"] != member["organization_id"]:
                    raise PermissionDenied("不能为其他成员登记成果")
            elif actor["role"] not in SECRETARIAT_ROLES:
                raise PermissionDenied("当前角色不能登记成果")
            if lic["status"] == "revoked":
                raise PermissionDenied("许可已撤销，不能登记成果")
            title = self._text(title, "title", 300)

            def create() -> dict[str, Any]:
                now = self._now_dt()
                charter = self._effective_charter(connection, now)
                if charter is None:
                    raise ValidationError("章程尚未生效，不能计算署名资格")
                attributions = self._attribution(connection, charter["rules"])
                publication_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO publications(publication_id,license_id,member_id,dataset_id,title,"
                    "published_at) VALUES(?,?,?,?,?,?)",
                    (publication_id, license_id, lic["member_id"], lic["dataset_id"], title,
                     self._now()),
                )
                for item in attributions:
                    connection.execute(
                        "INSERT INTO publication_attributions(publication_id,member_id,share,"
                        "eligible) VALUES(?,?,?,?)",
                        (publication_id, item["member_id"], item["share"],
                         1 if item["eligible"] else 0),
                    )
                append_event(connection, actor_id=actor["actor_id"], action="publication.registered",
                             resource_type="publication", resource_id=publication_id,
                             detail={"license_id": license_id, "member_id": lic["member_id"],
                                     "dataset_id": lic["dataset_id"], "title": title,
                                     "charter_version": charter["version"],
                                     "attributions": attributions},
                             occurred_at=self._now())
                return {"resource_type": "publication", "resource_id": publication_id,
                        "publication_id": publication_id, "dataset_id": lic["dataset_id"],
                        "attributions": attributions}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.register_publication", payload=payload,
                                    create=create)

    # ---- 查询与解释 ------------------------------------------------------

    def _proposal_view(self, row) -> dict[str, Any]:
        return {"proposal_id": row["proposal_id"], "member_id": row["member_id"],
                "dataset_id": row["dataset_id"], "dataset_version": row["dataset_version"],
                "purpose": row["purpose"], "third_party_transfer": bool(row["third_party_transfer"]),
                "submitted_at": row["submitted_at"], "charter_version": row["charter_version"],
                "status": row["status"], "decision": json.loads(row["decision_json"]),
                "license_id": row["license_id"], "decided_at": row["decided_at"]}

    def get_proposal(self, *, actor_id: str, proposal_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                 (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError("提案不存在")
        self._require_can_view(connection, actor, row["member_id"])
        return self._proposal_view(row)

    def get_license(self, *, actor_id: str, license_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = connection.execute("SELECT * FROM licenses WHERE license_id=?",
                                 (license_id,)).fetchone()
        if row is None:
            raise NotFoundError("许可不存在")
        self._require_can_view(connection, actor, row["member_id"])
        effective = row["status"]
        if effective == "active" and _parse_time(row["expires_at"], "expires_at") <= self._now_dt():
            effective = "expired"
        return {"license_id": row["license_id"], "proposal_id": row["proposal_id"],
                "member_id": row["member_id"], "dataset_id": row["dataset_id"],
                "dataset_version": row["dataset_version"], "scope": row["scope"],
                "status": effective, "stored_status": row["status"], "revision": row["revision"],
                "status_note": row["status_note"], "granted_at": row["granted_at"],
                "expires_at": row["expires_at"], "updated_at": row["updated_at"]}

    def get_resolution(self, *, actor_id: str, resolution_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        row = connection.execute("SELECT * FROM resolutions WHERE resolution_id=?",
                                 (resolution_id,)).fetchone()
        if row is None:
            raise NotFoundError("决议不存在")
        votes = [{"member_id": v["member_id"], "choice": v["choice"],
                  "recused": bool(v["recused"]), "cast_at": v["cast_at"]}
                 for v in connection.execute(
                     "SELECT * FROM resolution_votes WHERE resolution_id=? ORDER BY cast_at",
                     (resolution_id,))]
        return {"resolution_id": row["resolution_id"], "kind": row["kind"],
                "subject": json.loads(row["subject_json"]), "opened_at": row["opened_at"],
                "closes_at": row["closes_at"], "quorum_fraction": row["quorum_fraction"],
                "weights": json.loads(row["weights_json"]), "status": row["status"],
                "tally": json.loads(row["tally_json"]) if row["tally_json"] else None,
                "decided_at": row["decided_at"], "votes": votes}

    def explain_access(self, *, actor_id: str, member_id: str,
                       dataset_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_can_view(connection, actor, member_id)
        member = self._member_row(connection, member_id)
        dataset = connection.execute("SELECT * FROM datasets WHERE dataset_id=?",
                                     (dataset_id,)).fetchone()
        if dataset is None:
            raise NotFoundError("数据集不存在")
        now = self._now_dt()
        lic = connection.execute(
            "SELECT * FROM licenses WHERE member_id=? AND dataset_id=? "
            "ORDER BY granted_at DESC, license_id DESC LIMIT 1",
            (member_id, dataset_id),
        ).fetchone()
        proposal = connection.execute(
            "SELECT * FROM proposals WHERE member_id=? AND dataset_id=? "
            "ORDER BY submitted_at DESC, proposal_id DESC LIMIT 1",
            (member_id, dataset_id),
        ).fetchone()
        version = connection.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
        reasons: list[str] = []
        allowed = False
        license_view = None
        if lic is not None:
            effective = lic["status"]
            if effective == "active" and _parse_time(lic["expires_at"], "expires_at") <= now:
                effective = "expired"
            license_view = {"license_id": lic["license_id"], "status": effective,
                            "stored_status": lic["status"], "scope": lic["scope"],
                            "granted_at": lic["granted_at"], "expires_at": lic["expires_at"],
                            "dataset_version": lic["dataset_version"],
                            "status_note": lic["status_note"]}
            if effective == "active":
                allowed = True
                reasons.append(
                    f"许可 {lic['license_id']} 依据提案 {lic['proposal_id']} 授予，范围 {lic['scope']}")
                if version is None or version["status"] != "active":
                    allowed = False
                    reasons.append("当前数据版本不可用，下载暂停")
                elif now < _parse_time(version["embargo_until"], "embargo_until"):
                    charter = self._effective_charter(connection, now)
                    if charter is not None:
                        standing = self._standing(connection, member_id, now)
                        required = charter["rules"]["embargo_access_fulfillment"]
                        if standing["fulfillment_ratio"] < required:
                            allowed = False
                            reasons.append(
                                f"禁运期至 {version['embargo_until']}，当前实缴完成率 "
                                f"{standing['fulfillment_ratio']} 低于要求 {required}")
            elif effective == "suspended":
                reasons.append(f"许可自 {lic['updated_at']} 起暂停（{lic['status_note']}）")
            elif effective == "revoked":
                reasons.append(f"许可已于 {lic['updated_at']} 撤销（{lic['status_note']}）")
            elif effective == "expired":
                reasons.append(f"许可已于 {lic['expires_at']} 到期")
        else:
            if proposal is not None:
                decision = json.loads(proposal["decision_json"])
                if proposal["status"] == "denied":
                    reasons.append("提案被拒绝：" + "；".join(decision.get("reasons", [])))
                elif proposal["status"] == "pending":
                    reasons.append("提案待治理决议：" + "；".join(decision.get("gates", [])))
                else:
                    reasons.append("提案已批准但许可尚未生成")
            else:
                reasons.append("尚未提交使用提案")
        if member["status"] == "withdrawn":
            reasons.append("成员已退出；退出前已产生的下载与署名记录仍然有效")
        if lic is not None and version is not None and lic["dataset_version"] < version["version"]:
            reasons.append(f"数据已更正至 v{version['version']}，后续下载使用新版本")
        proposal_view = None
        if proposal is not None:
            decision = json.loads(proposal["decision_json"])
            proposal_view = {"proposal_id": proposal["proposal_id"],
                             "status": proposal["status"],
                             "submitted_at": proposal["submitted_at"],
                             "charter_version": proposal["charter_version"],
                             "decision": decision}
        return {"member_id": member_id, "dataset_id": dataset_id, "allowed": allowed,
                "reasons": reasons, "license": license_view, "proposal": proposal_view,
                "current_version": ({"version": version["version"], "status": version["status"],
                                     "sensitivity": version["sensitivity"],
                                     "embargo_until": version["embargo_until"]}
                                    if version else None)}

    def member_standing(self, *, actor_id: str, member_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_can_view(connection, actor, member_id)
        member = self._member_row(connection, member_id)
        standing = self._standing(connection, member_id, self._now_dt(),
                                  include_commitments=True)
        return {"member_id": member_id, "member_status": member["status"],
                "voting_weight": member["voting_weight"], **standing}

    def dataset_usage(self, *, actor_id: str, dataset_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *READER_ROLES)
        dataset = connection.execute("SELECT * FROM datasets WHERE dataset_id=?",
                                     (dataset_id,)).fetchone()
        if dataset is None:
            raise NotFoundError("数据集不存在")
        versions = [{"version": row["version"], "sensitivity": row["sensitivity"],
                     "embargo_until": row["embargo_until"], "status": row["status"],
                     "supersedes": row["supersedes"], "note": row["note"]}
                    for row in connection.execute(
                        "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version",
                        (dataset_id,))]
        licenses = [{"license_id": row["license_id"], "member_id": row["member_id"],
                     "status": row["status"], "scope": row["scope"],
                     "granted_at": row["granted_at"], "expires_at": row["expires_at"]}
                    for row in connection.execute(
                        "SELECT * FROM licenses WHERE dataset_id=? ORDER BY granted_at",
                        (dataset_id,))]
        download_rows = connection.execute(
            "SELECT member_id, COUNT(*) AS count, SUM(bytes) AS bytes FROM download_events "
            "WHERE dataset_id=? GROUP BY member_id", (dataset_id,)).fetchall()
        by_member = {row["member_id"]: {"count": row["count"], "bytes": row["bytes"] or 0}
                     for row in download_rows}
        total_count = sum(item["count"] for item in by_member.values())
        total_bytes = sum(item["bytes"] for item in by_member.values())
        publications = []
        for pub in connection.execute(
                "SELECT * FROM publications WHERE dataset_id=? ORDER BY published_at",
                (dataset_id,)):
            attributions = [{"member_id": a["member_id"], "share": a["share"],
                             "eligible": bool(a["eligible"])}
                            for a in connection.execute(
                                "SELECT * FROM publication_attributions WHERE publication_id=? "
                                "ORDER BY member_id", (pub["publication_id"],))]
            publications.append({"publication_id": pub["publication_id"],
                                 "member_id": pub["member_id"], "title": pub["title"],
                                 "published_at": pub["published_at"],
                                 "attributions": attributions})
        return {"dataset_id": dataset_id, "title": dataset["title"], "versions": versions,
                "licenses": licenses,
                "downloads": {"count": total_count, "bytes": total_bytes, "by_member": by_member},
                "publications": publications}

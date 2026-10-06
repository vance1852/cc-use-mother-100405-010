"""国际科研合作贡献与数据治理领域服务。

把成员身份、国家/机构承诺、实缴贡献、仪器排期、数据集版本、敏感级别、
禁运窗口、使用提案、署名规则与治理决议串成可审计的权利来源：

* 访问资格按提案 *提交时* 有效的章程版本与贡献快照计算，之后章程修订、
  权重调整、迟交贡献、成员退出都不会回改已经生效的许可；
* 许可只通过只追加的事件账本变化（授予/暂停/恢复/退出撤销/版本取代/到期）；
* 所有写操作幂等，重复表决与重复下载回调不会重复计数；
* 部分唯一索引保证同一成员对同一数据集任意时刻最多一个有效许可版本。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from . import governance
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
MEMBER_KINDS = frozenset({"country", "institution"})
COMMITMENT_KINDS = frozenset({"instrument_time", "funding", "calibration"})
SENSITIVITIES = frozenset({"public", "internal", "restricted", "sensitive"})
VOTES = frozenset({"yes", "no", "abstain"})
STAFF_ROLES = frozenset({"secretariat", "admin"})


class GovernanceService:
    """治理领域的应用服务，所有方法都在短事务内执行。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _amount(self, value: Any, field: str, *, positive: bool = False) -> float:
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是非负数字") from exc
        if amount != amount or amount == float("inf") or amount < 0 or (positive and amount <= 0):
            raise ValidationError(f"{field} 必须是{'正' if positive else '非负'}有限数字")
        return amount

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_staff(self, actor: Actor) -> None:
        if actor.role not in STAFF_ROLES:
            raise PermissionDenied("该动作只有秘书处可以执行")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _replay_if_seen(self, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """事务前的快速重放：状态已变化时，相同重试仍返回原回执。

        这只是优化路径；权威的冲突与插入检查仍在持锁事务内完成。
        """

        try:
            normalized = self._id(request_id, "request_id")
        except ValidationError:
            return None
        row = self.database.connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (normalized,)
        ).fetchone()
        if row and row["action"] == action and row["payload_hash"] == digest(payload):
            return WriteReceipt(normalized, row["resource_type"], row["resource_id"], True)
        return None

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _member(self, connection, member_id: str):
        row = connection.execute("SELECT * FROM gov_members WHERE member_id=?", (member_id,)).fetchone()
        if row is None:
            raise NotFoundError("成员不存在")
        return row

    def _dataset(self, connection, dataset_id: str, version: int):
        row = connection.execute(
            "SELECT * FROM gov_datasets WHERE dataset_id=? AND version=?", (dataset_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("数据集版本不存在")
        return row

    def _baseline_in_force(self, connection, at: str):
        # 取生效时间不晚于 at 的最近一个生效章程；不同章程系列之间以生效时刻定先后
        return connection.execute(
            "SELECT * FROM gov_baselines WHERE status='effective' AND effective_at<=? "
            "ORDER BY effective_at DESC, version DESC LIMIT 1",
            (at,),
        ).fetchone()

    # ------------------------------------------------------------------
    # 成员与权重
    # ------------------------------------------------------------------

    def register_member(self, *, request_id: str, actor_id: str, member_id: str,
                        kind: str, name: str, weight: float = 1.0,
                        conflict_orgs: list[str] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "member_id": member_id, "kind": kind, "name": name,
                   "weight": weight, "conflict_orgs": conflict_orgs or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            member_id = self._id(member_id, "member_id")
            name = self._text(name, "name")
            if kind not in MEMBER_KINDS:
                raise ValidationError("kind 必须是 country 或 institution")
            weight = self._amount(weight, "weight", positive=True)
            conflict_orgs = conflict_orgs or []
            if not isinstance(conflict_orgs, list):
                raise ValidationError("conflict_orgs 必须是数组")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    # 成员同时作为组织，供代表 actor 挂靠
                    connection.execute(
                        "INSERT INTO organizations(organization_id,name,created_at) VALUES(?,?,?)",
                        (member_id, name, self._now()),
                    )
                    connection.execute(
                        "INSERT INTO gov_members(member_id,kind,name,status,weight,conflict_orgs,created_at) "
                        "VALUES(?,?,?,'active',?,?,?)",
                        (member_id, kind, name, weight, canonical_json(conflict_orgs), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("成员编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="gov.member_registered",
                            resource_type="gov_member", resource_id=member_id,
                            detail={"kind": kind, "name": name, "weight": weight,
                                    "conflict_orgs": conflict_orgs})
                return "gov_member", member_id, {"member_id": member_id}

            return self._idempotent(connection, request_id=request_id, action="register_member",
                                    payload=payload, create=create)

    def set_member_weight(self, *, request_id: str, actor_id: str, member_id: str,
                          weight: float) -> WriteReceipt:
        """调整表决权重。只影响之后的提案快照，不影响已生效决议。"""

        payload = {"actor_id": actor_id, "member_id": member_id, "weight": weight}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            member_id = self._id(member_id, "member_id")
            weight = self._amount(weight, "weight", positive=True)
            self._member(connection, member_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE gov_members SET weight=? WHERE member_id=?",
                                   (weight, member_id))
                self._audit(connection, actor_id=actor_id, action="gov.member_weight_changed",
                            resource_type="gov_member", resource_id=member_id,
                            detail={"weight": weight, "retroactive": False})
                return "gov_member", member_id, {"member_id": member_id, "weight": weight}

            return self._idempotent(connection, request_id=request_id, action="set_member_weight",
                                    payload=payload, create=create)

    def withdraw_member(self, *, request_id: str, actor_id: str, member_id: str,
                        reason: str = "") -> WriteReceipt:
        """成员退出：只产生后继效力——停用成员并撤销其尚未结束的许可。"""

        payload = {"actor_id": actor_id, "member_id": member_id, "reason": reason}
        replay = self._replay_if_seen(request_id, "withdraw_member", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            member_id = self._id(member_id, "member_id")
            self._member(connection, member_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                result = connection.execute(
                    "UPDATE gov_members SET status='withdrawn', withdrawn_at=? "
                    "WHERE member_id=? AND status='active'",
                    (now, member_id),
                )
                if result.rowcount == 0:
                    raise ConflictError("成员已经退出")
                connection.execute(
                    "UPDATE actors SET active=0 WHERE organization_id=?", (member_id,)
                )
                revoked = self._close_active_grants(connection, member_id=member_id,
                                                    event="revoked_withdrawal",
                                                    reason=reason or "成员退出", now=now)
                self._audit(connection, actor_id=actor_id, action="gov.member_withdrawn",
                            resource_type="gov_member", resource_id=member_id,
                            detail={"reason": reason, "revoked_grants": revoked})
                return "gov_member", member_id, {"member_id": member_id, "revoked_grants": revoked}

            return self._idempotent(connection, request_id=request_id, action="withdraw_member",
                                    payload=payload, create=create)

    def _close_active_grants(self, connection, *, member_id: str, event: str,
                             reason: str, now: str) -> int:
        rows = connection.execute(
            "SELECT grant_id FROM gov_access_grants WHERE member_id=? AND status='active'",
            (member_id,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE gov_access_grants SET status='inactive' WHERE grant_id=?",
                (row["grant_id"],),
            )
            connection.execute(
                "INSERT INTO gov_access_grants(grant_id,proposal_id,member_id,dataset_id,"
                "dataset_version,event,scope_json,status,reason,created_at) "
                "SELECT ?,proposal_id,member_id,dataset_id,dataset_version,?,scope_json,'inactive',?,? "
                "FROM gov_access_grants WHERE grant_id=?",
                (uuid.uuid4().hex, event, reason, now, row["grant_id"]),
            )
        return len(rows)

    # ------------------------------------------------------------------
    # 章程版本
    # ------------------------------------------------------------------

    def register_baseline(self, *, request_id: str, actor_id: str, baseline_id: str,
                          version: int, payload: dict[str, Any],
                          effective_at: str | None = None) -> WriteReceipt:
        payload = dict(payload)
        wrapped = {"actor_id": actor_id, "baseline_id": baseline_id, "version": version,
                   "payload": payload, "effective_at": effective_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            baseline_id = self._id(baseline_id, "baseline_id")
            version = int(version)
            if version < 1:
                raise ValidationError("version 必须从 1 开始")
            self._validate_baseline(payload)
            effective_at = effective_at or self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO gov_baselines(baseline_id,version,status,payload_json,"
                        "payload_hash,created_by,created_at,effective_at) VALUES(?,?,?,?,?,?,?,?)",
                        (baseline_id, version, "effective", canonical_json(payload), digest(payload),
                         actor_id, now, effective_at),
                    )
                except Exception as exc:
                    raise ConflictError("章程版本已经存在") from exc
                # 同一系列中生效时间不晚于本版的旧版本标记为被取代
                connection.execute(
                    "UPDATE gov_baselines SET status='superseded' "
                    "WHERE baseline_id=? AND version<>? AND status='effective' AND effective_at<=?",
                    (baseline_id, version, effective_at),
                )
                self._audit(connection, actor_id=actor_id, action="gov.baseline_registered",
                            resource_type="gov_baseline", resource_id=f"{baseline_id}:{version}",
                            detail={"payload_hash": digest(payload), "effective_at": effective_at})
                return "gov_baseline", f"{baseline_id}:{version}", {
                    "baseline_id": baseline_id, "version": version}

            return self._idempotent(connection, request_id=request_id, action="register_baseline",
                                    payload=wrapped, create=create)

    def _validate_baseline(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("章程内容必须是对象")
        voting = payload.setdefault("voting", {})
        for field in ("quorum_ratio", "approval_ratio"):
            value = float(voting.get(field, 0.5))
            if not 0.0 < value <= 1.0:
                raise ValidationError(f"voting.{field} 必须在 (0,1] 区间")
            voting[field] = value
        eligibility = payload.setdefault("eligibility", {})
        ratio = float(eligibility.get("min_fulfillment_ratio", 0.0))
        if not 0.0 <= ratio <= 1.0:
            raise ValidationError("eligibility.min_fulfillment_ratio 必须在 [0,1] 区间")
        eligibility["min_fulfillment_ratio"] = ratio
        payload.setdefault("authorship", {}).setdefault("order_rule", "contribution_weight")
        payload.setdefault("data", {}).setdefault("allow_third_party_transfer", False)
        return payload

    # ------------------------------------------------------------------
    # 承诺、实缴贡献与仪器排期
    # ------------------------------------------------------------------

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            member_id: str, kind: str, amount: float, unit: str,
                            due_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "member_id": member_id,
                   "kind": kind, "amount": amount, "unit": unit, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment_id = self._id(commitment_id, "commitment_id")
            member_id = self._id(member_id, "member_id")
            unit = self._text(unit, "unit", 40)
            due_at = self._text(due_at, "due_at", 40)
            amount = self._amount(amount, "amount", positive=True)
            if kind not in COMMITMENT_KINDS:
                raise ValidationError("kind 必须是 instrument_time/funding/calibration")
            self._member(connection, member_id)
            # 成员代表可为自己登记承诺，秘书处可代登记
            if actor.role not in STAFF_ROLES and actor.organization_id != member_id:
                raise PermissionDenied("不能为其他成员登记承诺")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO gov_commitments(commitment_id,member_id,kind,amount,unit,"
                        "due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                        (commitment_id, member_id, kind, amount, unit, due_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="gov.commitment_registered",
                            resource_type="gov_commitment", resource_id=commitment_id,
                            detail={"member_id": member_id, "kind": kind, "amount": amount,
                                    "unit": unit, "due_at": due_at})
                return "gov_commitment", commitment_id, {"commitment_id": commitment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_commitment", payload=payload, create=create)

    def record_contribution(self, *, request_id: str, actor_id: str, commitment_id: str,
                            amount: float, contributed_at: str | None = None) -> WriteReceipt:
        """登记一笔实缴贡献。迟交只新增一行，不改变任何既有快照。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "amount": amount,
                   "contributed_at": contributed_at}
        replay = self._replay_if_seen(request_id, "record_contribution", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment_id = self._id(commitment_id, "commitment_id")
            amount = self._amount(amount, "amount", positive=True)
            row = connection.execute(
                "SELECT * FROM gov_commitments WHERE commitment_id=?", (commitment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("承诺不存在")
            if actor.role not in STAFF_ROLES and actor.organization_id != row["member_id"]:
                raise PermissionDenied("只能登记本成员的实缴贡献")
            contributed_at = contributed_at or self._now()
            contribution_id = uuid.uuid4().hex
            late = contributed_at > row["due_at"]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO gov_contributions(contribution_id,commitment_id,member_id,kind,"
                    "amount,contributed_at,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (contribution_id, commitment_id, row["member_id"], row["kind"], amount,
                     contributed_at, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="gov.contribution_recorded",
                            resource_type="gov_contribution", resource_id=contribution_id,
                            detail={"commitment_id": commitment_id, "member_id": row["member_id"],
                                    "kind": row["kind"], "amount": amount,
                                    "contributed_at": contributed_at, "late": late})
                return "gov_contribution", contribution_id, {"contribution_id": contribution_id,
                                                              "late": late}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_contribution", payload=payload, create=create)

    def schedule_instrument(self, *, request_id: str, actor_id: str, slot_id: str,
                            member_id: str, instrument_code: str, starts_at: str,
                            ends_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "slot_id": slot_id, "member_id": member_id,
                   "instrument_code": instrument_code, "starts_at": starts_at, "ends_at": ends_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            slot_id = self._id(slot_id, "slot_id")
            member_id = self._id(member_id, "member_id")
            instrument_code = self._text(instrument_code, "instrument_code", 80)
            starts_at = self._text(starts_at, "starts_at", 40)
            ends_at = self._text(ends_at, "ends_at", 40)
            if ends_at <= starts_at:
                raise ValidationError("结束时间必须晚于开始时间")
            self._member(connection, member_id)
            if actor.role not in STAFF_ROLES and actor.organization_id != member_id:
                raise PermissionDenied("不能为其他成员登记仪器排期")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO gov_instrument_slots(slot_id,member_id,instrument_code,"
                        "starts_at,ends_at,delivered,created_at) VALUES(?,?,?,?,?,0,?)",
                        (slot_id, member_id, instrument_code, starts_at, ends_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("排期编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="gov.instrument_scheduled",
                            resource_type="gov_instrument_slot", resource_id=slot_id,
                            detail={"member_id": member_id, "instrument_code": instrument_code,
                                    "starts_at": starts_at, "ends_at": ends_at})
                return "gov_instrument_slot", slot_id, {"slot_id": slot_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="schedule_instrument", payload=payload, create=create)

    def mark_slot_delivered(self, *, request_id: str, actor_id: str, slot_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "slot_id": slot_id}
        replay = self._replay_if_seen(request_id, "mark_slot_delivered", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            slot_id = self._id(slot_id, "slot_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                slot = connection.execute(
                    "SELECT * FROM gov_instrument_slots WHERE slot_id=?", (slot_id,)
                ).fetchone()
                if slot is None:
                    raise NotFoundError("排期不存在")
                if actor.role not in STAFF_ROLES and actor.organization_id != slot["member_id"]:
                    raise PermissionDenied("只能确认本成员的仪器交付")
                result = connection.execute(
                    "UPDATE gov_instrument_slots SET delivered=1 WHERE slot_id=? AND delivered=0",
                    (slot_id,),
                )
                if result.rowcount == 0:
                    raise ConflictError("排期已标记交付")
                self._audit(connection, actor_id=actor_id, action="gov.instrument_delivered",
                            resource_type="gov_instrument_slot", resource_id=slot_id,
                            detail={"delivered": True})
                return "gov_instrument_slot", slot_id, {"slot_id": slot_id, "delivered": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_slot_delivered", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 数据集版本、敏感级别与更正
    # ------------------------------------------------------------------

    def register_dataset(self, *, request_id: str, actor_id: str, dataset_id: str,
                         version: int, title: str, sensitivity: str, embargo_until: str,
                         owning_member_id: str, payload: dict[str, Any] | None = None,
                         corrected_of: int | None = None,
                         producer_member_ids: list[str] | None = None) -> WriteReceipt:
        payload = payload or {}
        producer_member_ids = producer_member_ids or []
        wrapped = {"actor_id": actor_id, "dataset_id": dataset_id, "version": version,
                   "title": title, "sensitivity": sensitivity, "embargo_until": embargo_until,
                   "owning_member_id": owning_member_id, "payload": payload,
                   "corrected_of": corrected_of, "producer_member_ids": producer_member_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            dataset_id = self._id(dataset_id, "dataset_id")
            version = int(version)
            if version < 1:
                raise ValidationError("version 必须从 1 开始")
            title = self._text(title, "title")
            if sensitivity not in SENSITIVITIES:
                raise ValidationError("sensitivity 不合法")
            embargo_until = self._text(embargo_until, "embargo_until", 40)
            self._member(connection, owning_member_id)
            if not isinstance(producer_member_ids, list):
                raise ValidationError("producer_member_ids 必须是数组")
            for producer_id in producer_member_ids:
                self._member(connection, str(producer_id))
            producers = sorted({owning_member_id, *producer_member_ids})
            if actor.role not in STAFF_ROLES and actor.organization_id != owning_member_id:
                raise PermissionDenied("只能由拥有成员或秘书处登记数据集")

            def create() -> tuple[str, str, dict[str, Any]]:
                data_hash = digest(payload)
                try:
                    connection.execute(
                        "INSERT INTO gov_datasets(dataset_id,version,title,sensitivity,"
                        "embargo_until,owning_member_id,producers_json,payload_hash,corrected_of,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (dataset_id, version, title, sensitivity, embargo_until,
                         owning_member_id, canonical_json(producers), data_hash, corrected_of,
                         self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("数据集版本已经存在") from exc
                superseded = 0
                if corrected_of is not None:
                    updated = connection.execute(
                        "UPDATE gov_datasets SET superseded_by=? "
                        "WHERE dataset_id=? AND version=? AND superseded_by IS NULL",
                        (version, dataset_id, corrected_of),
                    )
                    if updated.rowcount == 0:
                        raise NotFoundError("被更正的数据集版本不存在")
                    # 旧版本上的有效许可随版本取代而失效，新版本需要新提案
                    rows = connection.execute(
                        "SELECT g.grant_id FROM gov_access_grants g WHERE g.dataset_id=? "
                        "AND g.dataset_version=? AND g.status='active'",
                        (dataset_id, corrected_of),
                    ).fetchall()
                    for row in rows:
                        connection.execute(
                            "UPDATE gov_access_grants SET status='inactive' WHERE grant_id=?",
                            (row["grant_id"],),
                        )
                        connection.execute(
                            "INSERT INTO gov_access_grants(grant_id,proposal_id,member_id,"
                            "dataset_id,dataset_version,event,scope_json,status,reason,created_at) "
                            "SELECT ?,proposal_id,member_id,dataset_id,dataset_version,'superseded',"
                            "scope_json,'inactive',?,? FROM gov_access_grants WHERE grant_id=?",
                            (uuid.uuid4().hex, f"数据集更正至版本 {version}", self._now(),
                             row["grant_id"]),
                        )
                    superseded = len(rows)
                self._audit(connection, actor_id=actor_id, action="gov.dataset_registered",
                            resource_type="gov_dataset", resource_id=f"{dataset_id}:{version}",
                            detail={"sensitivity": sensitivity, "embargo_until": embargo_until,
                                    "owner": owning_member_id, "producers": producers,
                                    "corrected_of": corrected_of,
                                    "payload_hash": data_hash, "superseded_grants": superseded})
                return "gov_dataset", f"{dataset_id}:{version}", {
                    "dataset_id": dataset_id, "version": version, "superseded_grants": superseded}

            return self._idempotent(connection, request_id=request_id, action="register_dataset",
                                    payload=wrapped, create=create)

    # ------------------------------------------------------------------
    # 提案与提交时快照
    # ------------------------------------------------------------------

    def submit_proposal(self, *, request_id: str, actor_id: str, proposal_id: str,
                        dataset_id: str, dataset_version: int, purpose: str,
                        scope: dict[str, Any] | None = None,
                        third_party_transfer: bool = False,
                        applicant_member_id: str | None = None) -> WriteReceipt:
        scope = scope or {}
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "dataset_id": dataset_id,
                   "dataset_version": dataset_version, "purpose": purpose, "scope": scope,
                   "third_party_transfer": third_party_transfer,
                   "applicant_member_id": applicant_member_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            proposal_id = self._id(proposal_id, "proposal_id")
            dataset_id = self._id(dataset_id, "dataset_id")
            dataset_version = int(dataset_version)
            purpose = self._text(purpose, "purpose", 2000)
            if not isinstance(scope, dict):
                raise ValidationError("scope 必须是对象")
            applicant_member_id = applicant_member_id or actor.organization_id
            applicant = self._member(connection, applicant_member_id)
            if applicant["status"] != "active":
                raise PermissionDenied("已退出成员不能提交提案")
            if actor.role not in STAFF_ROLES and actor.organization_id != applicant_member_id:
                raise PermissionDenied("只能代表本成员提交提案")
            dataset = self._dataset(connection, dataset_id, dataset_version)
            if dataset["superseded_by"] is not None:
                raise ConflictError("该数据集版本已被更正取代，请针对新版本提交")
            now = self._now()
            baseline = self._baseline_in_force(connection, now)
            if baseline is None:
                raise ValidationError("当前没有生效的章程版本")
            baseline_payload = json.loads(baseline["payload_json"])
            if third_party_transfer and not baseline_payload["data"].get(
                    "allow_third_party_transfer", False):
                raise PermissionDenied("提交时有效的章程禁止向第三方转交")
            if third_party_transfer and dataset["sensitivity"] == "sensitive":
                raise PermissionDenied("敏感级别为 sensitive 的数据集一律不得向第三方转交")
            now = self._now()
            # 快照在插入前构建，用于校验提案人自身资格，避免产生"出生即失效"的许可
            snapshot = self._build_snapshot(connection, baseline_payload=baseline_payload,
                                            dataset_owner_id=dataset["owning_member_id"],
                                            applicant_id=applicant_member_id, at=now)
            applicant_entry = next(
                item for item in snapshot["members"]
                if item["member_id"] == applicant_member_id)
            applicant_access_reasons = [
                reason for reason in applicant_entry["ineligible_reasons"]
                if reason != "conflict_recusal"]
            if applicant_access_reasons:
                raise PermissionDenied(
                    "提案成员按提交时贡献快照不具备使用资格："
                    + ",".join(applicant_access_reasons))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO gov_proposals(proposal_id,applicant_member_id,purpose,"
                        "dataset_id,dataset_version,requested_scope_json,third_party_transfer,"
                        "status,baseline_id,baseline_version,snapshot_json,snapshot_hash,"
                        "submitted_at) VALUES(?,?,?,?,?,?,?,'submitted',?,?,?,?,?)",
                        (proposal_id, applicant_member_id, purpose, dataset_id, dataset_version,
                         canonical_json(scope), 1 if third_party_transfer else 0,
                         baseline["baseline_id"], baseline["version"],
                         canonical_json(snapshot), digest(snapshot), now),
                    )
                except Exception as exc:
                    raise ConflictError("提案编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="gov.proposal_submitted",
                            resource_type="gov_proposal", resource_id=proposal_id,
                            detail={"dataset": f"{dataset_id}:{dataset_version}",
                                    "applicant": applicant_member_id,
                                    "baseline": f"{baseline['baseline_id']}:{baseline['version']}",
                                    "snapshot_hash": digest(snapshot)})
                return "gov_proposal", proposal_id, {
                    "proposal_id": proposal_id, "snapshot_hash": digest(snapshot),
                    "baseline": f"{baseline['baseline_id']}:{baseline['version']}"}

            return self._idempotent(connection, request_id=request_id, action="submit_proposal",
                                    payload=payload, create=create)

    def _build_snapshot(self, connection, *, baseline_payload: dict[str, Any],
                        dataset_owner_id: str, applicant_id: str, at: str) -> dict[str, Any]:
        entries = []
        members = connection.execute("SELECT * FROM gov_members ORDER BY member_id").fetchall()
        for member in members:
            committed_due = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM gov_commitments "
                "WHERE member_id=? AND due_at<=?",
                (member["member_id"], at),
            ).fetchone()["total"]
            paid = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM gov_contributions "
                "WHERE member_id=? AND contributed_at<=?",
                (member["member_id"], at),
            ).fetchone()["total"]
            entries.append(governance.build_member_snapshot(
                member={"member_id": member["member_id"], "status": member["status"],
                        "weight": member["weight"],
                        "conflict_orgs": json.loads(member["conflict_orgs"])},
                paid=paid, committed_due=committed_due, baseline=baseline_payload,
                dataset_owner_id=dataset_owner_id, applicant_id=applicant_id))
        snapshot = governance.summarize_snapshot(entries)
        snapshot["at"] = at
        snapshot["baseline_rules"] = {
            "eligibility": baseline_payload.get("eligibility", {}),
            "voting": baseline_payload.get("voting", {}),
        }
        return snapshot

    # ------------------------------------------------------------------
    # 表决与决议
    # ------------------------------------------------------------------

    def cast_ballot(self, *, request_id: str, actor_id: str, proposal_id: str,
                    vote: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "vote": vote}
        replay = self._replay_if_seen(request_id, "cast_ballot", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            proposal_id = self._id(proposal_id, "proposal_id")
            if vote not in VOTES:
                raise ValidationError("vote 必须是 yes/no/abstain")
            proposal = connection.execute(
                "SELECT * FROM gov_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise NotFoundError("提案不存在")
            if proposal["status"] != "submitted":
                raise ConflictError("提案已经形成决议，不能再投票")
            member_id = actor.organization_id
            # 利益冲突回避在投票时刻实时判定：即使提交快照后才产生冲突，也必须回避
            voter = self._member(connection, member_id)
            dataset = self._dataset(connection, proposal["dataset_id"],
                                    proposal["dataset_version"])
            live_conflict = governance.is_conflicted(
                {"conflict_orgs": json.loads(voter["conflict_orgs"] or "[]")},
                dataset["owning_member_id"], proposal["applicant_member_id"])
            snapshot = json.loads(proposal["snapshot_json"])
            entry = next((item for item in snapshot["members"]
                          if item["member_id"] == member_id), None)
            if entry is None:
                raise NotFoundError("表决成员不在快照中")
            if live_conflict or entry["conflict"]:
                raise PermissionDenied("存在利益冲突，该代表必须回避表决")
            if not entry["eligible"]:
                raise PermissionDenied(
                    "按提交时快照，该成员不具备表决资格：" + ",".join(entry["ineligible_reasons"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                ballot_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO gov_ballots(ballot_id,proposal_id,member_id,vote,weight,"
                        "eligible,conflict_recused,cast_at) VALUES(?,?,?,?,?,1,0,?)",
                        (ballot_id, proposal_id, member_id, vote, entry["weight"], self._now()),
                    )
                except Exception as exc:
                    # UNIQUE(proposal_id, member_id)：重复表决不重复计数
                    raise ConflictError("该成员已经投票，重复表决不会重复计数") from exc
                self._audit(connection, actor_id=actor_id, action="gov.ballot_cast",
                            resource_type="gov_ballot", resource_id=ballot_id,
                            detail={"proposal_id": proposal_id, "member_id": member_id,
                                    "vote": vote, "weight": entry["weight"]})
                return "gov_ballot", ballot_id, {"ballot_id": ballot_id}

            return self._idempotent(connection, request_id=request_id, action="cast_ballot",
                                    payload=payload, create=create)

    def resolve_proposal(self, *, request_id: str, actor_id: str,
                         proposal_id: str) -> WriteReceipt:
        """形成治理决议；通过则在许可账本上授予访问。结果一经定影不可倒改。"""

        payload = {"actor_id": actor_id, "proposal_id": proposal_id}
        replay = self._replay_if_seen(request_id, "resolve_proposal", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            proposal_id = self._id(proposal_id, "proposal_id")
            proposal = connection.execute(
                "SELECT * FROM gov_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise NotFoundError("提案不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if proposal["status"] != "submitted":
                    raise ConflictError("提案已经形成决议")
                baseline = connection.execute(
                    "SELECT * FROM gov_baselines WHERE baseline_id=? AND version=?",
                    (proposal["baseline_id"], proposal["baseline_version"]),
                ).fetchone()
                baseline_payload = json.loads(baseline["payload_json"])
                snapshot = json.loads(proposal["snapshot_json"])
                ballots = [{"member_id": row["member_id"], "vote": row["vote"],
                            "weight": row["weight"]}
                           for row in connection.execute(
                               "SELECT * FROM gov_ballots WHERE proposal_id=?", (proposal_id,))]
                result = governance.tally_ballots(ballots, snapshot, baseline_payload)
                now = self._now()
                resolution_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO gov_resolutions(resolution_id,proposal_id,outcome,quorum_met,"
                    "yes_weight,no_weight,abstain_weight,eligible_weight,detail_json,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (resolution_id, proposal_id, result["outcome"],
                     1 if result["quorum_met"] else 0, result["yes_weight"], result["no_weight"],
                     result["abstain_weight"], result["eligible_weight"],
                     canonical_json(result), now),
                )
                connection.execute(
                    "UPDATE gov_proposals SET status='decided', decided_at=? WHERE proposal_id=?",
                    (now, proposal_id),
                )
                grant_id = None
                if result["outcome"] == "approved":
                    target_dataset = self._dataset(connection, proposal["dataset_id"],
                                                   proposal["dataset_version"])
                    if target_dataset["superseded_by"] is not None:
                        # 表决期间数据集已被更正：不能再向失效版本授权，须针对新版本重新提案
                        raise ConflictError(
                            f"数据集版本 {proposal['dataset_id']}:"
                            f"{proposal['dataset_version']} 已在表决期间被更正取代，请针对新版本重新提案")
                    grant_id = self._insert_grant(
                        connection, proposal=proposal, snapshot=snapshot, reason="治理决议通过",
                        event="granted", now=now)
                self._audit(connection, actor_id=actor_id, action="gov.resolution_decided",
                            resource_type="gov_resolution", resource_id=resolution_id,
                            detail={"proposal_id": proposal_id, **result, "grant_id": grant_id})
                return "gov_resolution", resolution_id, {
                    "resolution_id": resolution_id, "outcome": result["outcome"],
                    "quorum_met": result["quorum_met"], "grant_id": grant_id}

            return self._idempotent(connection, request_id=request_id, action="resolve_proposal",
                                    payload=payload, create=create)

    def _insert_grant(self, connection, *, proposal, snapshot: dict[str, Any], reason: str,
                      event: str, now: str) -> str:
        scope = {
            "requested_scope": json.loads(proposal["requested_scope_json"]),
            "third_party_transfer": bool(proposal["third_party_transfer"]),
        }
        grant_id = uuid.uuid4().hex
        try:
            connection.execute(
                "INSERT INTO gov_access_grants(grant_id,proposal_id,member_id,dataset_id,"
                "dataset_version,event,scope_json,status,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?, 'active',?,?)",
                (grant_id, proposal["proposal_id"], proposal["applicant_member_id"],
                 proposal["dataset_id"], proposal["dataset_version"], event,
                 canonical_json(scope), reason, now),
            )
        except Exception as exc:
            # 部分唯一索引：同一成员对同一数据集已有有效许可（含其他版本）
            raise ConflictError("该成员对该数据集已有有效许可版本，并发授权最多一个版本有效") from exc
        return grant_id

    # ------------------------------------------------------------------
    # 许可暂停/恢复（后继事件）
    # ------------------------------------------------------------------

    def _change_grant(self, *, request_id: str, actor_id: str, action: str, event_label: str,
                      member_id: str, dataset_id: str, reason: str, new_status: str,
                      new_event: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "member_id": member_id, "dataset_id": dataset_id,
                   "reason": reason, "action": action}
        replay = self._replay_if_seen(request_id, action, payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            member_id = self._id(member_id, "member_id")
            dataset_id = self._id(dataset_id, "dataset_id")
            reason = self._text(reason, "reason", 500)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM gov_access_grants WHERE member_id=? AND dataset_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (member_id, dataset_id),
                ).fetchone()
                if latest is None:
                    raise NotFoundError("不存在该成员对该数据集的许可")
                if action == "suspend" and latest["status"] != "active":
                    raise ConflictError("许可当前不是有效状态，无法暂停")
                if action == "resume" and latest["status"] != "inactive":
                    raise ConflictError("许可当前有效，无需恢复")
                if action == "resume" and latest["event"] not in ("suspended",):
                    raise ConflictError("只有被暂停的许可可以恢复；退出或版本取代需重新申请")
                if latest["status"] == "active":
                    connection.execute(
                        "UPDATE gov_access_grants SET status='inactive' WHERE grant_id=?",
                        (latest["grant_id"],),
                    )
                grant_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO gov_access_grants(grant_id,proposal_id,member_id,dataset_id,"
                        "dataset_version,event,scope_json,status,reason,created_at) "
                        "SELECT ?,proposal_id,member_id,dataset_id,dataset_version,?,scope_json,?,?,? "
                        "FROM gov_access_grants WHERE grant_id=?",
                        (grant_id, new_event, new_status, reason, now, latest["grant_id"]),
                    )
                except Exception as exc:
                    raise ConflictError("并发授权冲突：该数据集已存在有效版本") from exc
                self._audit(connection, actor_id=actor_id, action=event_label,
                            resource_type="gov_access_grant", resource_id=grant_id,
                            detail={"member_id": member_id, "dataset_id": dataset_id,
                                    "previous_grant_id": latest["grant_id"], "reason": reason})
                return "gov_access_grant", grant_id, {"grant_id": grant_id, "status": new_status}

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    def suspend_grant(self, *, request_id: str, actor_id: str, member_id: str,
                      dataset_id: str, reason: str) -> WriteReceipt:
        return self._change_grant(request_id=request_id, actor_id=actor_id, action="suspend",
                                  event_label="gov.grant_suspended", member_id=member_id,
                                  dataset_id=dataset_id, reason=reason,
                                  new_status="inactive", new_event="suspended")

    def resume_grant(self, *, request_id: str, actor_id: str, member_id: str,
                     dataset_id: str, reason: str) -> WriteReceipt:
        return self._change_grant(request_id=request_id, actor_id=actor_id, action="resume",
                                  event_label="gov.grant_resumed", member_id=member_id,
                                  dataset_id=dataset_id, reason=reason,
                                  new_status="active", new_event="resumed")

    # ------------------------------------------------------------------
    # 下载回调与成果发表
    # ------------------------------------------------------------------

    def register_download_callback(self, *, request_id: str, actor_id: str, callback_id: str,
                                   grant_id: str, occurred_at: str | None = None) -> WriteReceipt:
        """数据平台下载回调。同一 callback_id 重复投递只计一次。"""

        payload = {"actor_id": actor_id, "callback_id": callback_id, "grant_id": grant_id,
                   "occurred_at": occurred_at}
        replay = self._replay_if_seen(request_id, "download_callback", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
            callback_id = self._id(callback_id, "callback_id")
            grant_id = self._id(grant_id, "grant_id")
            grant = connection.execute(
                "SELECT * FROM gov_access_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise NotFoundError("许可不存在")
            occurred_at = occurred_at or self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO gov_download_callbacks(callback_id,grant_id,dataset_id,"
                        "dataset_version,occurred_at,recorded_at) VALUES(?,?,?,?,?,?)",
                        (callback_id, grant_id, grant["dataset_id"], grant["dataset_version"],
                         occurred_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("回调已经处理，重复下载回调不会重复计数") from exc
                self._audit(connection, actor_id=actor_id, action="gov.download_callback",
                            resource_type="gov_download_callback", resource_id=callback_id,
                            detail={"grant_id": grant_id, "dataset": f"{grant['dataset_id']}:"
                                                                     f"{grant['dataset_version']}",
                                    "occurred_at": occurred_at})
                return "gov_download_callback", callback_id, {"callback_id": callback_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="download_callback", payload=payload, create=create)

    def record_publication(self, *, request_id: str, actor_id: str, publication_id: str,
                           grant_id: str, title: str,
                           published_at: str | None = None) -> WriteReceipt:
        """登记成果发表：按提案快照中的贡献生成署名责任，只产生后继义务。"""

        payload = {"actor_id": actor_id, "publication_id": publication_id, "grant_id": grant_id,
                   "title": title, "published_at": published_at}
        replay = self._replay_if_seen(request_id, "record_publication", payload)
        if replay is not None:
            return replay
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            publication_id = self._id(publication_id, "publication_id")
            title = self._text(title, "title")
            grant_id = self._id(grant_id, "grant_id")
            grant = connection.execute(
                "SELECT * FROM gov_access_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise NotFoundError("许可不存在")
            if actor.role not in STAFF_ROLES and actor.organization_id != grant["member_id"]:
                raise PermissionDenied("只能登记本成员成果的发表")
            proposal = connection.execute(
                "SELECT * FROM gov_proposals WHERE proposal_id=?", (grant["proposal_id"],)
            ).fetchone()
            baseline = connection.execute(
                "SELECT * FROM gov_baselines WHERE baseline_id=? AND version=?",
                (proposal["baseline_id"], proposal["baseline_version"]),
            ).fetchone()
            rules = json.loads(baseline["payload_json"]).get("authorship", {})
            snapshot = json.loads(proposal["snapshot_json"])
            dataset = self._dataset(connection, grant["dataset_id"], grant["dataset_version"])
            published_at = published_at or self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                contributors = [
                    {"member_id": entry["member_id"], "paid": entry["paid"],
                     "role": "data_owner" if entry["member_id"] == dataset["owning_member_id"]
                     else ("contributor" if entry["paid"] > 0 else "acknowledged_member")}
                    for entry in snapshot["members"]
                    if entry["paid"] > 0 or entry["member_id"] == dataset["owning_member_id"]
                ]
                authorship = governance.order_authors(contributors, rules)
                try:
                    connection.execute(
                        "INSERT INTO gov_publications(publication_id,grant_id,title,authorship_json,"
                        "published_at,recorded_at) VALUES(?,?,?,?,?,?)",
                        (publication_id, grant_id, title, canonical_json(authorship),
                         published_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("成果编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="gov.publication_recorded",
                            resource_type="gov_publication", resource_id=publication_id,
                            detail={"grant_id": grant_id, "title": title,
                                    "authorship": authorship})
                return "gov_publication", publication_id, {
                    "publication_id": publication_id, "authorship": authorship}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_publication", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 合作方解释与秘书处追溯
    # ------------------------------------------------------------------

    def explain_access(self, member_id: str, dataset_id: str, at: str | None = None,
                       actor_id: str | None = None) -> dict[str, Any]:
        """返回某成员对某数据集当前访问权的完整证据链与逐项门禁解释。

        合作方经此接口只能查询自己成员身份的权限；秘书处可查询任意成员。
        """

        at = at or self._now()
        connection = self.database.connection
        if actor_id:
            actor = self._actor(connection, actor_id)
            if actor.role not in STAFF_ROLES and actor.organization_id != member_id:
                raise PermissionDenied("合作方只能查询本成员的权限解释")
        member = connection.execute(
            "SELECT * FROM gov_members WHERE member_id=?", (member_id,)
        ).fetchone()
        if member is None:
            raise NotFoundError("成员不存在")
        latest = connection.execute(
            "SELECT * FROM gov_access_grants WHERE member_id=? AND dataset_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (member_id, dataset_id),
        ).fetchone()
        proposal = resolution = applicant_entry = dataset_row = None
        contributor = False
        if latest is not None:
            proposal = connection.execute(
                "SELECT * FROM gov_proposals WHERE proposal_id=?", (latest["proposal_id"],)
            ).fetchone()
            dataset_row = self._dataset(connection, dataset_id, latest["dataset_version"])
            res_row = connection.execute(
                "SELECT * FROM gov_resolutions WHERE proposal_id=?", (latest["proposal_id"],)
            ).fetchone()
            if res_row:
                resolution = {"outcome": res_row["outcome"], "quorum_met": bool(res_row["quorum_met"]),
                              "resolution_id": res_row["resolution_id"]}
            snapshot = json.loads(proposal["snapshot_json"])
            applicant_entry = next(
                (item for item in snapshot["members"]
                 if item["member_id"] == proposal["applicant_member_id"]), None)
            producers = set(json.loads(dataset_row["producers_json"] or "[]"))
            contributor = proposal["applicant_member_id"] in producers
        else:
            dataset_row = connection.execute(
                "SELECT * FROM gov_datasets WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                (dataset_id,),
            ).fetchone()
            if dataset_row is None:
                raise NotFoundError("数据集不存在")
        dataset = {"dataset_id": dataset_id, "version": dataset_row["version"],
                   "sensitivity": dataset_row["sensitivity"],
                   "embargo_until": dataset_row["embargo_until"],
                   "owning_member_id": dataset_row["owning_member_id"],
                   "producers": json.loads(dataset_row["producers_json"] or "[]")}
        decision = governance.access_gates(
            resolution=resolution, applicant_snapshot=applicant_entry,
            member_active=member["status"] == "active", dataset=dataset, at=at,
            applicant_is_contributor=contributor,
            grant_event=latest["event"] if latest else None)
        evidence: dict[str, Any] = {
            "member_id": member_id, "dataset": dataset, "at": at,
            "latest_grant_event": None if latest is None else {
                "grant_id": latest["grant_id"], "event": latest["event"],
                "status": latest["status"], "reason": latest["reason"],
                "created_at": latest["created_at"], "dataset_version": latest["dataset_version"]},
        }
        if proposal is not None:
            evidence["proposal_id"] = proposal["proposal_id"]
            evidence["baseline_at_submission"] = f"{proposal['baseline_id']}:" \
                                                 f"{proposal['baseline_version']}"
            evidence["snapshot_hash"] = proposal["snapshot_hash"]
            evidence["submitted_at"] = proposal["submitted_at"]
            evidence["resolution"] = resolution
        return {**decision, "evidence": evidence}

    def grant_timeline(self, member_id: str | None = None,
                       dataset_id: str | None = None,
                       actor_id: str | None = None) -> list[dict[str, Any]]:
        if actor_id:
            actor = self._actor(self.database.connection, actor_id)
            if actor.role not in STAFF_ROLES:
                # 合作方只能追溯本成员的许可事件
                member_id = actor.organization_id
        sql = "SELECT * FROM gov_access_grants"
        clauses = []
        params: list[Any] = []
        if member_id:
            clauses.append("member_id=?")
            params.append(member_id)
        if dataset_id:
            clauses.append("dataset_id=?")
            params.append(dataset_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, rowid"
        return [{"grant_id": row["grant_id"], "proposal_id": row["proposal_id"],
                 "member_id": row["member_id"], "dataset_id": row["dataset_id"],
                 "dataset_version": row["dataset_version"], "event": row["event"],
                 "status": row["status"], "reason": row["reason"],
                 "scope": json.loads(row["scope_json"]), "created_at": row["created_at"]}
                for row in self.database.connection.execute(sql, params)]

    def unfulfilled_commitments(self, at: str | None = None,
                                actor_id: str | None = None) -> list[dict[str, Any]]:
        """秘书处追溯未履行承诺：列出已到期但实缴不足的承诺。"""

        at = at or self._now()
        connection = self.database.connection
        if actor_id:
            actor = self._actor(connection, actor_id)
            self._require_staff(actor)
        rows = self.database.connection.execute(
            "SELECT c.commitment_id,c.member_id,c.kind,c.amount,c.unit,c.due_at,"
            "COALESCE((SELECT SUM(amount) FROM gov_contributions p WHERE p.commitment_id=c.commitment_id"
            " AND p.contributed_at<=?),0) AS paid FROM gov_commitments c WHERE c.due_at<=?",
            (at, at),
        ).fetchall()
        items = []
        for row in rows:
            shortfall = round(row["amount"] - row["paid"], 6)
            if shortfall > 1e-9:
                items.append({"commitment_id": row["commitment_id"], "member_id": row["member_id"],
                              "kind": row["kind"], "amount": row["amount"], "paid": row["paid"],
                              "shortfall": shortfall, "unit": row["unit"], "due_at": row["due_at"]})
        return items

    def proposal_detail(self, proposal_id: str, actor_id: str | None = None) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM gov_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("提案不存在")
        if actor_id:
            actor = self._actor(self.database.connection, actor_id)
            if actor.role not in STAFF_ROLES and actor.organization_id != row["applicant_member_id"]:
                raise PermissionDenied("只能查看本成员提交的提案")
        ballots = [{"member_id": item["member_id"], "vote": item["vote"], "weight": item["weight"],
                    "cast_at": item["cast_at"]}
                   for item in self.database.connection.execute(
                       "SELECT * FROM gov_ballots WHERE proposal_id=? ORDER BY cast_at",
                       (proposal_id,))]
        resolution = self.database.connection.execute(
            "SELECT * FROM gov_resolutions WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return {
            "proposal_id": row["proposal_id"], "applicant_member_id": row["applicant_member_id"],
            "dataset": f"{row['dataset_id']}:{row['dataset_version']}", "purpose": row["purpose"],
            "scope": json.loads(row["requested_scope_json"]),
            "third_party_transfer": bool(row["third_party_transfer"]),
            "status": row["status"],
            "baseline": f"{row['baseline_id']}:{row['baseline_version']}",
            "snapshot": json.loads(row["snapshot_json"]), "snapshot_hash": row["snapshot_hash"],
            "submitted_at": row["submitted_at"], "decided_at": row["decided_at"],
            "ballots": ballots,
            "resolution": None if resolution is None else json.loads(resolution["detail_json"]),
        }

    def list_publications(self, grant_id: str | None = None) -> list[dict[str, Any]]:
        """秘书处追溯成果署名责任。"""

        connection = self.database.connection
        if grant_id:
            rows = connection.execute(
                "SELECT * FROM gov_publications WHERE grant_id=? ORDER BY published_at",
                (grant_id,)).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM gov_publications ORDER BY published_at").fetchall()
        return [{"publication_id": row["publication_id"], "grant_id": row["grant_id"],
                 "title": row["title"], "authorship": json.loads(row["authorship_json"]),
                 "published_at": row["published_at"]} for row in rows]

    def list_downloads(self, grant_id: str | None = None,
                       actor_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        if actor_id:
            actor = self._actor(connection, actor_id)
            if actor.role not in STAFF_ROLES:
                # 合作方只能看到本成员许可的下载记录
                rows = connection.execute(
                    "SELECT c.* FROM gov_download_callbacks c JOIN gov_access_grants g "
                    "ON g.grant_id=c.grant_id WHERE g.member_id=? ORDER BY c.recorded_at",
                    (actor.organization_id,)).fetchall()
                return [{"callback_id": row["callback_id"], "grant_id": row["grant_id"],
                         "dataset": f"{row['dataset_id']}:{row['dataset_version']}",
                         "occurred_at": row["occurred_at"], "recorded_at": row["recorded_at"]}
                        for row in rows]
        if grant_id:
            rows = self.database.connection.execute(
                "SELECT * FROM gov_download_callbacks WHERE grant_id=? ORDER BY recorded_at",
                (grant_id,)).fetchall()
        else:
            rows = self.database.connection.execute(
                "SELECT * FROM gov_download_callbacks ORDER BY recorded_at").fetchall()
        return [{"callback_id": row["callback_id"], "grant_id": row["grant_id"],
                 "dataset": f"{row['dataset_id']}:{row['dataset_version']}",
                 "occurred_at": row["occurred_at"], "recorded_at": row["recorded_at"]}
                for row in rows]

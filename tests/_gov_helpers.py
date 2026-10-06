"""治理领域测试共用的引导装置。"""

from __future__ import annotations

from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.gov_service import GovernanceService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class GovScenario:
    """搭建一个三成员、一章程、一个数据集的标准治理场景。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.database = Database()
        self.clock = FixedClock(start or datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.clock)
        self._bootstrap()

    def _bootstrap(self) -> None:
        self.service.register_organization(
            request_id="org-sec", actor_id="bootstrap", organization_id="sec", name="秘书处")
        self.service.register_actor(
            request_id="actor-sec", actor_id="bootstrap", new_actor_id="sec1",
            display_name="秘书长", role="secretariat", organization_id="sec")
        for member_id, name, weight in (("m1", "甲国", 2.0), ("m2", "乙国", 1.0),
                                        ("m3", "丙国", 1.0)):
            self.gov.register_member(
                request_id=f"mem-{member_id}", actor_id="sec1", member_id=member_id,
                kind="country", name=name, weight=weight)
        for actor_id, name, member_id in (("rep1", "甲代表", "m1"),
                                          ("rep2", "乙代表", "m2"),
                                          ("rep3", "丙代表", "m3")):
            self.service.register_actor(
                request_id=f"actor-{actor_id}", actor_id="sec1", new_actor_id=actor_id,
                display_name=name, role="operator", organization_id=member_id)
        self.gov.register_baseline(
            request_id="charter-v1", actor_id="sec1", baseline_id="charter", version=1,
            payload={
                "voting": {"quorum_ratio": 0.5, "approval_ratio": 0.5},
                "eligibility": {"min_fulfillment_ratio": 0.5},
                "authorship": {"order_rule": "contribution_weight"},
                "data": {"allow_third_party_transfer": True},
            })

    def commitment(self, cid: str, rep: str, member: str, amount: float,
                   kind: str = "funding", due_at: str = "2026-09-01T00:00:00Z") -> None:
        self.gov.register_commitment(
            request_id=f"cmt-{cid}", actor_id=rep, commitment_id=cid, member_id=member,
            kind=kind, amount=amount, unit="u", due_at=due_at)

    def pay(self, rid: str, rep: str, cid: str, amount: float, at: str) -> None:
        self.gov.record_contribution(
            request_id=rid, actor_id=rep, commitment_id=cid, amount=amount, contributed_at=at)

    def dataset(self, version: int = 1, *, embargo_until: str = "2026-12-01T00:00:00Z",
                sensitivity: str = "restricted", owner: str = "m1",
                producers: list[str] | None = None, corrected_of: int | None = None,
                dataset_id: str = "obs-A") -> None:
        self.gov.register_dataset(
            request_id=f"ds-{dataset_id}-v{version}-{corrected_of or ''}", actor_id="sec1",
            dataset_id=dataset_id, version=version, title=f"观测数据 v{version}",
            sensitivity=sensitivity, embargo_until=embargo_until, owning_member_id=owner,
            producer_member_ids=producers, corrected_of=corrected_of)

    def close(self) -> None:
        self.database.close()

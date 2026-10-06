"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);

-- ============================================================
-- 国际科研合作贡献与数据治理领域
-- ============================================================

-- 成员：国家或机构。
-- 约定：登记成员时同时在 organizations 表写入同编号行，
-- 这样成员代表直接复用 actors.organization_id 作为其代表的成员身份。
CREATE TABLE IF NOT EXISTS gov_members (
    member_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('country','institution')),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight > 0),
    conflict_orgs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);

-- 章程版本（表决规则、署名规则、数据治理默认约定）
CREATE TABLE IF NOT EXISTS gov_baselines (
    baseline_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','effective','superseded')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    PRIMARY KEY (baseline_id, version)
);

-- 承诺：成员承诺提供仪器时间 / 经费 / 校准资料
CREATE TABLE IF NOT EXISTS gov_commitments (
    commitment_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    kind TEXT NOT NULL CHECK(kind IN ('instrument_time','funding','calibration')),
    amount REAL NOT NULL CHECK(amount >= 0),
    unit TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 实缴贡献事件：迟交只产生新行，不回改既有快照
CREATE TABLE IF NOT EXISTS gov_contributions (
    contribution_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES gov_commitments(commitment_id),
    member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    kind TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    contributed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gov_contrib_member ON gov_contributions(member_id, contributed_at);

-- 仪器排期
CREATE TABLE IF NOT EXISTS gov_instrument_slots (
    slot_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    instrument_code TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

-- 数据集版本（含敏感级别与禁运窗口）
CREATE TABLE IF NOT EXISTS gov_datasets (
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public','internal','restricted','sensitive')),
    embargo_until TEXT NOT NULL,
    owning_member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    superseded_by TEXT,
    producers_json TEXT NOT NULL DEFAULT '[]',
    payload_hash TEXT NOT NULL,
    corrected_of TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version)
);

-- 使用提案
CREATE TABLE IF NOT EXISTS gov_proposals (
    proposal_id TEXT PRIMARY KEY,
    applicant_member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    purpose TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    requested_scope_json TEXT NOT NULL,
    third_party_transfer INTEGER NOT NULL DEFAULT 0 CHECK(third_party_transfer IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('submitted','decided','withdrawn')),
    baseline_id TEXT NOT NULL,
    baseline_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (baseline_id, baseline_version) REFERENCES gov_baselines(baseline_id, version)
);

-- 治理表决票（每提案每成员唯一，重复计票被拒绝）
CREATE TABLE IF NOT EXISTS gov_ballots (
    ballot_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES gov_proposals(proposal_id),
    member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    vote TEXT NOT NULL CHECK(vote IN ('yes','no','abstain')),
    weight REAL NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
    conflict_recused INTEGER NOT NULL DEFAULT 0 CHECK(conflict_recused IN (0,1)),
    cast_at TEXT NOT NULL,
    UNIQUE(proposal_id, member_id)
);

-- 治理决议（法定人数按提交快照计算，结果定影）
CREATE TABLE IF NOT EXISTS gov_resolutions (
    resolution_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES gov_proposals(proposal_id),
    outcome TEXT NOT NULL CHECK(outcome IN ('approved','rejected')),
    quorum_met INTEGER NOT NULL CHECK(quorum_met IN (0,1)),
    yes_weight REAL NOT NULL,
    no_weight REAL NOT NULL,
    abstain_weight REAL NOT NULL,
    eligible_weight REAL NOT NULL,
    detail_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- 访问许可事件账本：只追加，绝不回改
CREATE TABLE IF NOT EXISTS gov_access_grants (
    grant_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES gov_proposals(proposal_id),
    member_id TEXT NOT NULL REFERENCES gov_members(member_id),
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('granted','superseded','suspended','resumed','revoked_withdrawal','expired')),
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','inactive')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_member_dataset ON gov_access_grants(member_id, dataset_id, created_at);
-- 同一成员与数据集任意时刻最多一个有效许可版本
CREATE UNIQUE INDEX IF NOT EXISTS idx_grant_single_active
    ON gov_access_grants(member_id, dataset_id) WHERE status = 'active';

-- 下载回调（回调幂等：同一回调键只计一次）
CREATE TABLE IF NOT EXISTS gov_download_callbacks (
    callback_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES gov_access_grants(grant_id),
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

-- 成果发表（只产生后继署名义务，不改变既有许可）
CREATE TABLE IF NOT EXISTS gov_publications (
    publication_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES gov_access_grants(grant_id),
    title TEXT NOT NULL,
    authorship_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        # 多线程 HTTP 服务共用同一连接：写事务串行化，避免事务状态交错；
        # 业务级唯一约束（如单有效许可、回调唯一）仍由数据库索引保证。
        self._write_lock = threading.RLock()
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。多线程写入串行执行。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()

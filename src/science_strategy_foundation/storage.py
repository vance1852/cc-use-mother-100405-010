"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
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
CREATE TABLE IF NOT EXISTS charter_versions (
    charter_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    rules_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'effective', 'superseded')),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('country', 'institution')),
    voting_weight REAL NOT NULL CHECK(voting_weight > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
    joined_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS member_weight_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    weight REAL NOT NULL,
    effective_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    kind TEXT NOT NULL CHECK(kind IN ('instrument_time', 'funding', 'calibration_data')),
    amount REAL NOT NULL CHECK(amount > 0),
    unit TEXT NOT NULL,
    due_at TEXT NOT NULL,
    charter_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'fulfilled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contributions (
    contribution_id TEXT PRIMARY KEY,
    commitment_id TEXT REFERENCES commitments(commitment_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    kind TEXT NOT NULL CHECK(kind IN ('instrument_time', 'funding', 'calibration_data')),
    amount REAL NOT NULL CHECK(amount > 0),
    late INTEGER NOT NULL CHECK(late IN (0, 1)),
    contributed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_id TEXT REFERENCES sites(site_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instrument_slots (
    slot_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'completed', 'cancelled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public', 'internal', 'restricted', 'sensitive')),
    embargo_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'suspended')),
    supersedes INTEGER,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version)
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    dataset_version INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    third_party_transfer INTEGER NOT NULL CHECK(third_party_transfer IN (0, 1)),
    submitted_at TEXT NOT NULL,
    charter_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
    decision_json TEXT NOT NULL,
    license_id TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('amend_charter', 'suspend_license', 'resume_license',
                                     'approve_proposal', 'approve_transfer', 'admit_member')),
    subject_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    quorum_fraction REAL NOT NULL,
    weights_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'passed', 'failed')),
    tally_json TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS resolution_votes (
    resolution_id TEXT NOT NULL REFERENCES resolutions(resolution_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    actor_id TEXT NOT NULL,
    choice TEXT NOT NULL CHECK(choice IN ('yes', 'no', 'abstain')),
    recused INTEGER NOT NULL CHECK(recused IN (0, 1)),
    cast_at TEXT NOT NULL,
    PRIMARY KEY (resolution_id, member_id)
);
CREATE TABLE IF NOT EXISTS conflict_declarations (
    declaration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    resolution_id TEXT,
    dataset_id TEXT,
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    dataset_version INTEGER NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('read', 'read+transfer')),
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended', 'revoked', 'expired')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status_note TEXT,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS licenses_one_active
    ON licenses(member_id, dataset_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS download_events (
    callback_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    dataset_version INTEGER NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    title TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_attributions (
    publication_id TEXT NOT NULL REFERENCES publications(publication_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    share REAL NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    PRIMARY KEY (publication_id, member_id)
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
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

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

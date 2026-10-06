"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .gov_service import GovernanceService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, governance: GovernanceService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    governance = governance or GovernanceService(service.database, service.clock)
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def q(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------------------
        # 国际科研合作贡献与数据治理
        # ------------------------------------------------------------
        gov_post = {
            "/gov/members": governance.register_member,
            "/gov/members/weight": governance.set_member_weight,
            "/gov/members/withdraw": governance.withdraw_member,
            "/gov/baselines": governance.register_baseline,
            "/gov/commitments": governance.register_commitment,
            "/gov/contributions": governance.record_contribution,
            "/gov/instrument-slots": governance.schedule_instrument,
            "/gov/instrument-slots/delivered": governance.mark_slot_delivered,
            "/gov/datasets": governance.register_dataset,
            "/gov/proposals": governance.submit_proposal,
            "/gov/ballots": governance.cast_ballot,
            "/gov/proposals/resolve": governance.resolve_proposal,
            "/gov/grants/suspend": governance.suspend_grant,
            "/gov/grants/resume": governance.resume_grant,
            "/gov/download-callbacks": governance.register_download_callback,
            "/gov/publications": governance.record_publication,
        }
        if method == "POST" and parsed.path in gov_post:
            receipt = gov_post[parsed.path](actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/gov/access-explain":
            member_id = q("member_id")
            dataset_id = q("dataset_id")
            if not member_id or not dataset_id:
                raise ValidationError("member_id 与 dataset_id 不能为空")
            return 200, governance.explain_access(member_id, dataset_id, q("at") or None,
                                                  actor_id=actor_id or None)
        if method == "GET" and parsed.path == "/gov/grant-timeline":
            return 200, {"items": governance.grant_timeline(q("member_id") or None,
                                                            q("dataset_id") or None,
                                                            actor_id=actor_id or None)}
        if method == "GET" and parsed.path == "/gov/unfulfilled":
            return 200, {"items": governance.unfulfilled_commitments(
                q("at") or None, actor_id=actor_id or None)}
        if method == "GET" and parsed.path == "/gov/downloads":
            return 200, {"items": governance.list_downloads(q("grant_id") or None,
                                                            actor_id=actor_id or None)}
        if method == "GET" and parsed.path == "/gov/publications":
            return 200, {"items": governance.list_publications(q("grant_id") or None)}
        if method == "GET" and parsed.path.startswith("/gov/proposals/"):
            proposal_id = parsed.path.rsplit("/", 1)[-1]
            return 200, governance.proposal_detail(proposal_id, actor_id=actor_id or None)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    governance: GovernanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                governance=self.governance)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动科技战略协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    service = DomainService(database)
    Handler.service = service
    Handler.governance = GovernanceService(database, service.clock)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

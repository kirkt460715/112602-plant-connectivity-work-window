"""业务承诺协调服务端（回环监听，不接真实设备）。

维护、生产（车间）、备用值守三类角色通过同一服务接续下一次窗口，
各自只能看到自己仍承诺的范围与阻塞条件。链路与工位回执均由本地联调端给出。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from urllib.parse import urlsplit

from coordinator import Coordinator
from feed_client import FeedUnavailable, ReceiptClient

ROLES = {"maint", "mill", "support"}


class CoordinatorService(ThreadingHTTPServer):
    def __init__(self, port, client: ReceiptClient | None = None, register: dict | None = None):
        self.coordinator = Coordinator(receipt_client=client)
        if register is not None:
            self.coordinator.load_register(register)
        elif client is not None:
            self.coordinator.load_register(client.fetch_register())
        self.state_lock = RLock()
        super().__init__(("127.0.0.1", port), ServiceHandler)

    def ingest_feed(self) -> dict:
        """从联调端拉取全部到达事实并顺序应用。"""
        client = self.coordinator.client
        if client is None:
            raise FeedUnavailable("未配置联调端")
        applied = []
        with self.state_lock:
            for event in client.iter_intake():
                applied.append(self.coordinator.apply_event(event))
        return {"applied": applied, "count": len(applied)}


class ServiceHandler(BaseHTTPRequestHandler):
    server: CoordinatorService

    # -- 基础收发 -----------------------------------------------------------

    def _json(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid_json"})
            return None

    def _role(self):
        role = self.headers.get("X-Role", "")
        if role not in ROLES:
            self._json(403, {"error": "unknown_role", "allowed": sorted(ROLES)})
            return None
        return role

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, {"status": "ok"})
        role = self._role()
        if role is None:
            return
        coord = self.server.coordinator
        if path == "/windows":
            return self._json(200, {"windows": [
                {"id": w.id, "scope": list(w.scope), "period": list(w.period),
                 "status": w.status, "blockers": w.blockers,
                 "slots_requested": w.slots_requested}
                for w in coord.windows.values()]})
        if path == "/orders":
            return self._json(200, {"orders": [
                {"ref": o.ref, "mill": o.mill, "class": o.cls,
                 "direction": o.direction, "status": o.status, "history": o.history}
                for o in coord.orders.values()]})
        if path == "/deliveries":
            return self._json(200, {"deliveries": coord.deliveries})
        if path.startswith("/view/"):
            return self._view(path, role)
        self._json(404, {"error": "not_found"})

    def _view(self, path, role):
        coord = self.server.coordinator
        parts = [p for p in path.split("/") if p]
        if parts[1] == "maint":
            if role != "maint":
                return self._json(403, {"error": "role_forbidden"})
            return self._json(200, coord.view_for("maint"))
        if parts[1] == "support":
            if role != "support":
                return self._json(403, {"error": "role_forbidden"})
            return self._json(200, coord.view_for("support"))
        if parts[1] == "mill":
            if role != "mill":
                return self._json(403, {"error": "role_forbidden"})
            if len(parts) < 3:
                return self._json(400, {"error": "mill_required"})
            return self._json(200, coord.view_for("mill", parts[2]))
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        role = self._role()
        if role is None:
            return
        body = self._body()
        if body is None:
            return
        path = urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            with self.server.state_lock:
                self._route_post(parts, body, role)
        except FeedUnavailable as exc:
            self._json(502, {"error": "feed_unavailable", "detail": str(exc)})
        except (KeyError, ValueError) as exc:
            self._json(400, {"error": "bad_request", "detail": str(exc)})

    def _route_post(self, parts, body, role):
        coord = self.server.coordinator
        # 联调端事实灌入（维护侧触发的同步动作）
        if parts == ["events", "ingest"]:
            if role != "maint":
                return self._json(403, {"error": "role_forbidden"})
            return self._json(200, self.server.ingest_feed())
        if parts[:1] == ["events"] and len(parts) == 2:
            # 单条本地联调事件直发，便于测试
            return self._json(200, coord.apply_event(body))
        # 窗口批准 / 延长 / 恢复
        if len(parts) == 3 and parts[0] == "windows":
            win_id, action = parts[1], parts[2]
            if win_id not in coord.windows:
                return self._json(404, {"error": "window_not_found"})
            if action == "approve":
                if role != "maint":
                    return self._json(403, {"error": "role_forbidden"})
                return self._json(200, coord.approve_window(win_id, body["at"]))
            if action == "extend":
                if role != "maint":
                    return self._json(403, {"error": "role_forbidden"})
                return self._json(200, coord.extend_window(win_id, body["extend_to"], body["at"]))
            if action == "restore":
                if role != "maint":
                    return self._json(403, {"error": "role_forbidden"})
                return self._json(200, coord.report_restore(
                    win_id, body["restored_at"], body.get("ready_classes", []), body["at"]))
        # 车间承诺 / 撤回 / 接回
        if len(parts) == 3 and parts[0] == "mills":
            mill, action = parts[1], parts[2]
            if role != "mill":
                return self._json(403, {"error": "role_forbidden"})
            if action == "pledge":
                pledge = coord.pledge(
                    mill, body["allow"], body["valid_until"], body["at"],
                    mode=body.get("mode", "stop"), direction=body.get("direction"))
                return self._json(200, {"pledge": pledge.__dict__})
            if action == "retract":
                return self._json(200, {"retracted": coord.retract(
                    mill, body["at"], body.get("direction"))})
            if action == "takeback":
                return self._json(200, coord.takeback(
                    mill, body["classes"], body["at"], body.get("direction")))
        self._json(404, {"error": "not_found"})

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=8801, type=int)
    parser.add_argument("--feed-port", default=8765, type=int,
                        help="本地联调端（partner_feed）端口")
    parser.add_argument("--no-feed", action="store_true",
                        help="不连接联调端，台账等通过事件/接口另行装载")
    args = parser.parse_args()
    client = None if args.no_feed else ReceiptClient(port=args.feed_port)
    with CoordinatorService(args.port, client=client) as service:
        print(f"协调服务端 http://127.0.0.1:{service.server_port}", flush=True)
        service.serve_forever()


if __name__ == "__main__":
    main()

"""协调服务端端到端测试：真实拉起 partner_feed 与 CoordinatorService（均回环）。"""

from http.client import HTTPConnection
import json
from threading import Thread
import unittest

from partner_feed import PartnerFeed
from feed_client import ReceiptClient
from service import CoordinatorService


class ServiceEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.feed = PartnerFeed(0)
        self.addCleanup(self.feed.server_close)
        feed_thread = Thread(target=self.feed.serve_forever,
                             kwargs={"poll_interval": 0.02}, daemon=True)
        feed_thread.start()
        self.addCleanup(feed_thread.join, 2)
        self.addCleanup(self.feed.shutdown)

        client = ReceiptClient(port=self.feed.server_port, retries=4, backoff=0.02)
        self.service = CoordinatorService(0, client=client)
        self.addCleanup(self.service.server_close)
        svc_thread = Thread(target=self.service.serve_forever,
                            kwargs={"poll_interval": 0.02}, daemon=True)
        svc_thread.start()
        self.addCleanup(svc_thread.join, 2)
        self.addCleanup(self.service.shutdown)
        self.port = self.service.server_port

    def call(self, method, path, body=None, role="maint"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            headers = {"X-Role": role}
            payload = None
            if body is not None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_register_loaded_from_feed(self):
        coord = self.service.coordinator
        self.assertEqual(coord.reserve["capacity"], 3)
        self.assertEqual(set(coord.windows), {"m-1", "m-2"})

    def test_role_enforcement(self):
        self.assertEqual(self.call("GET", "/view/support", role="maint")[0], 403)
        self.assertEqual(self.call("GET", "/view/mill/mill-1", role="support")[0], 403)
        self.assertEqual(self.call("GET", "/view/maint", role="unknown")[0], 403)

    def test_full_window_flow_with_ingest_and_delayed_receipt(self):
        # 1) 车间承诺停接 weigh（生产侧），批准 m-1
        status, _ = self.call("POST", "/mills/mill-1/pledge",
                              {"allow": ["weigh"], "valid_until": 90, "at": 50}, role="mill")
        self.assertEqual(status, 200)
        status, approval = self.call("POST", "/windows/m-1/approve", {"at": 55})
        self.assertEqual((status, approval["approved"]), (200, True))

        # 2) 从联调端灌入样例事件：备份受理/认领、延长、撤回、恢复
        status, ingest = self.call("POST", "/events/ingest", {})
        self.assertEqual(status, 200)
        self.assertEqual(ingest["count"], 5)
        coord = self.service.coordinator
        # 延长生效，且送达台账保留
        self.assertEqual(coord.windows["m-1"].period, [60, 112])
        self.assertEqual(len(coord.deliveries), 5)
        # o-9 已领走开工、o-10 仍在队列
        self.assertEqual(coord.orders["o-9"].status, "backup_started")
        self.assertEqual(coord.orders["o-10"].status, "routed_backup")

        # 3) 恢复回报：链路经联调端核实（restore-report 非延迟），但接回仍待车间确认
        self.assertTrue(coord.windows["m-1"].link_evidence["link_restored"])
        status, support = self.call("GET", "/view/support", role="support")
        self.assertEqual(status, 200)
        self.assertIn("takeback_pending", json.dumps(support))

        # 4) 车间逐类接回 weigh：已开工单留给班组，未领走的回主端
        status, takeback = self.call("POST", "/mills/mill-1/takeback",
                                     {"classes": ["weigh"], "at": 100}, role="mill")
        self.assertEqual(status, 200)
        self.assertEqual(takeback["left_with_crew"], ["o-9"])
        self.assertEqual(takeback["rerouted_to_primary"], ["o-10"])

        # 5) 工位回执 work-status/o-9 首查 503，客户端退避后取到证据
        evidence = coord.verify_work_status("o-9")
        self.assertEqual(evidence["owner"], "backup-night")

        # 6) 班组完工后，备用值守无阻塞可撤
        coord.complete_order("o-9", at=110)
        status, support = self.call("GET", "/view/support", role="support")
        self.assertEqual(support["withdraw_blockers"], [])

    def test_concurrent_capacity_blocked_over_http(self):
        self.call("POST", "/mills/mill-1/pledge",
                  {"allow": ["weigh"], "valid_until": 120, "at": 55, "mode": "backup"},
                  role="mill")
        self.call("POST", "/mills/mill-2/pledge",
                  {"allow": ["weigh"], "valid_until": 125, "at": 55, "mode": "backup"},
                  role="mill")
        self.assertTrue(self.call("POST", "/windows/m-1/approve", {"at": 55})[1]["approved"])
        status, second = self.call("POST", "/windows/m-2/approve", {"at": 55})
        self.assertFalse(second["approved"])
        self.assertTrue(any(b["type"] == "reserve_capacity_exceeded"
                            for b in second["blockers"]))

    def test_deliveries_remain_queryable_after_extension(self):
        self.call("POST", "/events/ingest", {})
        status, data = self.call("GET", "/deliveries")
        self.assertEqual(status, 200)
        refs = data["deliveries"][1]["facts"]["accepted"]
        self.assertEqual(refs, ["o-9", "o-10"])


if __name__ == "__main__":
    unittest.main()

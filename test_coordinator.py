"""协调引擎的业务规则测试（不依赖网络，回执客户端用桩件）。"""

import unittest

from coordinator import Coordinator, OrderStage, Pledge


REGISTER = {
    "shifts": {
        "mill-1": [[0, 90], [90, 180]],
        "mill-2": [[0, 125]],
    },
    "work_classes": {"mill-1": ["cut", "weigh"], "mill-2": ["weigh"]},
    "reserve": {"desk": "backup-2", "capacity": 3, "allowed": ["weigh"]},
    "changes": [
        {"id": "m-1", "scope": ["mill-1"], "period": [60, 85], "slots_requested": 2},
        {"id": "m-2", "scope": ["mill-2"], "period": [72, 120], "slots_requested": 2},
    ],
}


class FakeClient:
    def __init__(self, receipts=None):
        self.receipts = receipts or {}

    def fetch_receipt(self, action, desk, ref):
        return self.receipts.get((action, desk, ref))


def build():
    coord = Coordinator(receipt_client=FakeClient({
        ("restore-report", "network-desk", "m-1"): {"link_restored": True,
                                                     "business_accepted": ["cut"]},
        ("work-status", "backup-2", "o-9"): {"started": True, "owner": "backup-night"},
    }))
    coord.load_register(REGISTER)
    return coord


class PledgeAndShiftTest(unittest.TestCase):
    def test_pledge_unknown_class_rejected(self):
        coord = build()
        with self.assertRaises(ValueError):
            coord.pledge("mill-2", ["cut"], 90, at=55)

    def test_pledge_does_not_carry_into_next_shift(self):
        # mill-1 本班 0-90；即使承诺停接，下一班开始后不得沿用
        coord = build()
        coord.pledge("mill-1", ["weigh"], valid_until=120, at=55)
        self.assertEqual(len(coord.active_pledges("mill-1", 80)), 1)
        expired = coord.advance(95)
        self.assertEqual(len(expired), 1)
        self.assertEqual(coord.active_pledges("mill-1", 95), [])
        # 新一班需要重新承诺
        coord.pledge("mill-1", ["weigh"], valid_until=180, at=95)
        self.assertEqual(len(coord.active_pledges("mill-1", 100)), 1)

    def test_retract_then_late_restore_does_not_reconfirm(self):
        # 车间先撤回同意；维护方恢复回报更晚，也不能替车间确认
        coord = build()
        coord.pledge("mill-1", ["weigh"], valid_until=120, at=55)
        coord.retract("mill-1", at=91)
        self.assertEqual(coord.active_pledges("mill-1", 91), [])
        result = coord.report_restore("m-1", restored_at=93, ready_classes=["cut"], at=97)
        self.assertTrue(result["link_verified"])          # 经办仍可查明链路恢复
        self.assertEqual(coord.active_pledges("mill-1", 97), [])  # 但未替车间确认
        self.assertTrue(any(b["type"] == "no_active_pledge"
                            for b in coord.windows["m-1"].blockers))


class ApprovalAndCapacityTest(unittest.TestCase):
    def test_approval_needs_active_pledge(self):
        coord = build()
        result = coord.approve_window("m-1", at=55)
        self.assertFalse(result["approved"])
        self.assertTrue(any(b["type"] == "no_active_pledge" for b in result["blockers"]))

    def test_backup_class_must_be_allowed_by_reserve(self):
        coord = build()
        # cut 不在应急队列 allowed（只允许 weigh）
        coord.pledge("mill-1", ["cut"], valid_until=120, at=55, mode="backup")
        result = coord.approve_window("m-1", at=55)
        self.assertTrue(any(b["type"] == "reserve_class_not_allowed" for b in result["blockers"]))

    def test_separately_ok_but_not_concurrent_capacity(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], 120, at=55, mode="backup")
        coord.pledge("mill-2", ["weigh"], 125, at=55, mode="backup")
        # 时段 60-85 与 72-120 重叠，各申请 2 槽，容量 3 => 单独可行，并发被拒
        self.assertTrue(coord.approve_window("m-1", at=55)["approved"])
        second = coord.approve_window("m-2", at=55)
        self.assertFalse(second["approved"])
        blocker = next(b for b in second["blockers"] if b["type"] == "reserve_capacity_exceeded")
        self.assertEqual((blocker["slots_total"], blocker["capacity"]), (4, 3))
        self.assertEqual(blocker["overlap"], [72, 85])

    def test_non_overlapping_windows_each_approved(self):
        coord = build()
        coord.windows["m-2"].period = [90, 110]      # 与 m-1(60-85) 不重叠
        coord.pledge("mill-1", ["weigh"], 90, at=55, mode="backup")
        coord.pledge("mill-2", ["weigh"], 125, at=55, mode="backup")
        self.assertTrue(coord.approve_window("m-1", at=55)["approved"])
        self.assertTrue(coord.approve_window("m-2", at=55)["approved"])


class ExtendAndDeliveryTest(unittest.TestCase):
    def test_extend_reevaluates_changed_mills_and_keeps_deliveries(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], valid_until=90, at=55)   # 只覆盖到 90
        coord.note_delivery({"sequence": 1, "at": 70, "source": "backup",
                             "facts": {"accepted": ["o-1"]}})
        coord.backup_accept(["o-1"], "mill-1", "weigh", None, 70)
        result = coord.extend_window("m-1", extend_to=112, at=76)
        self.assertEqual(coord.windows["m-1"].period, [60, 112])
        self.assertTrue(any(c["mill"] == "mill-1" for c in result["reevaluated"]))
        # 原本已送达的工单保持可查
        self.assertEqual(result["deliveries_kept"], 1)
        self.assertEqual(coord.deliveries[0]["facts"]["accepted"], ["o-1"])
        self.assertIn("o-1", coord.orders)

    def test_extend_within_pledge_coverage_flags_nothing(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], valid_until=120, at=55)
        result = coord.extend_window("m-1", extend_to=90, at=76)
        self.assertEqual(result["reevaluated"], [])


class TakebackAndRestoreTest(unittest.TestCase):
    def _setup_orders(self, coord):
        # o-9 已被备用端领走开工；o-10 仍在队列未领走
        coord.backup_accept(["o-9", "o-10"], "mill-1", "weigh", None, at=70)
        coord.backup_claim(["o-9"], at=72)
        self.assertEqual(coord.orders["o-9"].status, OrderStage.BACKUP_STARTED)
        self.assertEqual(coord.orders["o-10"].status, OrderStage.ROUTED_BACKUP)

    def test_restore_alone_does_not_withdraw_backup(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], 120, at=55, mode="backup")
        self._setup_orders(coord)
        result = coord.report_restore("m-1", 93, ["cut"], at=97)
        self.assertTrue(result["link_verified"])
        support = coord.view_for("support")
        # 备用仍不能撤：接回未确认
        self.assertTrue(any(b["type"] == "takeback_pending" for b in support["withdraw_blockers"]))
        # 队列里的工单状态没有被链路恢复自动改变
        self.assertEqual(coord.orders["o-10"].status, OrderStage.ROUTED_BACKUP)

    def test_takeback_per_class_started_stays_unclaimed_reroutes(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], 120, at=55, mode="backup")
        self._setup_orders(coord)
        coord.report_restore("m-1", 93, ["weigh"], at=97)
        result = coord.takeback("mill-1", ["weigh"], at=100)
        self.assertEqual(result["left_with_crew"], ["o-9"])       # 已开工单留给在场班组
        self.assertEqual(result["rerouted_to_primary"], ["o-10"])  # 未领走回主端
        self.assertEqual(coord.orders["o-9"].status, OrderStage.BACKUP_STARTED)
        coord.complete_order("o-9", at=110)
        self.assertEqual(coord.orders["o-9"].status, OrderStage.COMPLETED)
        # 接回完成且班组完工后，备用值守无阻塞，可以撤
        support = coord.view_for("support")
        self.assertEqual(support["withdraw_blockers"], [])

    def test_work_status_verified_through_feed(self):
        coord = build()
        self.assertEqual(coord.verify_work_status("o-9")["owner"], "backup-night")
        self.assertIsNone(coord.verify_work_status("missing"))


class RoleViewTest(unittest.TestCase):
    def test_each_role_sees_own_commitments_and_blockers(self):
        coord = build()
        coord.pledge("mill-1", ["weigh"], 90, at=55, mode="stop")
        mill_view = coord.view_for("mill", "mill-1")
        self.assertEqual(mill_view["active_pledges"][0]["classes"], ["weigh"])
        # weigh 停接，cut 仍可接
        self.assertEqual(mill_view["can_accept_now"], ["cut"])
        maint_view = coord.view_for("maint")
        self.assertEqual({w["id"] for w in maint_view["windows"]}, {"m-1", "m-2"})
        support = coord.view_for("support")
        self.assertEqual((support["reserve_desk"], support["capacity"]), ("backup-2", 3))

    def test_apply_event_ingest_matches_sample_narrative(self):
        coord = build()
        events = [
            {"sequence": 1, "at": 50, "source": "production",
             "facts": {"mill": "mill-1", "approved_until": 90, "allow": ["weigh"]}},
            {"sequence": 2, "at": 70, "source": "backup",
             "facts": {"accepted": ["o-9", "o-10"], "claimed": ["o-9"],
                       "mill": "mill-1", "class": "weigh"}},
            {"sequence": 3, "at": 76, "source": "maint",
             "facts": {"id": "m-1", "extend_to": 112}},
            {"sequence": 4, "at": 91, "source": "production",
             "facts": {"mill": "mill-1", "retract_previous": True}},
            {"sequence": 5, "at": 97, "source": "support",
             "facts": {"restored_at": 93, "link": "m-1", "ready_classes": ["cut"]}},
        ]
        outcomes = [coord.apply_event(e) for e in events]
        self.assertEqual([o["handled"] for o in outcomes],
                         ["pledge", "backup", "extend", "retract", "restore"])
        self.assertEqual(coord.orders["o-9"].status, OrderStage.BACKUP_STARTED)
        self.assertEqual(coord.orders["o-10"].status, OrderStage.ROUTED_BACKUP)
        self.assertEqual(coord.windows["m-1"].period, [60, 112])
        self.assertEqual(coord.active_pledges("mill-1", 97), [])
        self.assertTrue(coord.windows["m-1"].link_evidence["link_restored"])


if __name__ == "__main__":
    unittest.main()

"""维护窗口的业务承诺协调引擎（纯逻辑，不接真实设备）。

时间一律使用班表相对分钟（逻辑时钟），由到达事实的 ``at`` 与指令显式携带。
引擎只做协调判断：链路与工位回执事实由本地联调端（partner_feed）提供，
引擎通过 :class:`ReceiptClient` 拉取，不自行办理对端业务。
"""

from dataclasses import dataclass, field
from itertools import count


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Pledge:
    """车间（生产侧）对维护窗口作出的承诺：停接或托底。"""

    mill: str
    classes: tuple                      # 承诺涉及的工单类别
    valid_until: int                    # 承诺失效分钟（班表边界/批准时限）
    created_at: int
    mode: str = "stop"                  # stop=停接，backup=托底
    direction: str | None = None
    status: str = "active"              # active / expired / retracted
    history: list = field(default_factory=list)

    def covers(self, moment: int, cls: str, direction: str | None) -> bool:
        return (
            self.status == "active"
            and self.created_at <= moment < self.valid_until
            and cls in self.classes
            and (self.direction or direction) in (None, direction)
        )


@dataclass
class Order:
    """工单。送达事实不可变，状态随协调结论推进。"""

    ref: str
    mill: str
    cls: str
    direction: str | None
    delivered_at: int
    status: str = "primary_pending"     # 见 OrderStage
    history: list = field(default_factory=list)

    def record(self, moment: int, new_status: str, note: str) -> None:
        self.history.append({"at": moment, "from": self.status, "to": new_status, "note": note})
        self.status = new_status


class OrderStage:
    PRIMARY_PENDING = "primary_pending"      # 主端待受理
    ROUTED_BACKUP = "routed_backup"          # 已送入应急队列、尚未领走
    BACKUP_STARTED = "backup_started"        # 备用端已开始执行（在场班组接手）
    PRIMARY_RESUMED = "primary_resumed"      # 未领走工单按接回结果回主端
    COMPLETED = "completed"


@dataclass
class Window:
    """一次维护窗口。"""

    id: str
    scope: tuple
    period: list                          # [start, end]，延长只改 end
    slots_requested: int
    classes: tuple = ()                   # 影响的业务类别（由承诺推导/登记）
    directions: tuple = ()
    status: str = "proposed"              # proposed/approved/active/recovered/closed
    approved_at: int | None = None
    extended: bool = False
    restore: dict | None = None           # 维护方恢复回报
    link_evidence: dict | None = None     # 联调端核实到的链路回执
    taken_back: dict = field(default_factory=dict)   # class -> {at, direction}
    reeval_notes: list = field(default_factory=list)
    blockers: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 协调引擎
# ---------------------------------------------------------------------------


class Coordinator:
    def __init__(self, receipt_client=None, clock=0):
        self.client = receipt_client            # 可为空：纯单元测试不依赖联调端
        self.clock = clock
        self.shifts = {}                        # mill -> [[start, end], ...]
        self.mill_classes = {}                  # mill -> [class, ...]
        self.reserve = None                     # {desk, capacity, allowed}
        self.windows = {}                       # id -> Window
        self.pledges = []                       # Pledge 列表（含历史）
        self.orders = {}                        # ref -> Order
        self.deliveries = []                    # 不可变送达台账
        self._seq = count(1)

    # -- 台账装载 -----------------------------------------------------------

    def load_register(self, register: dict) -> None:
        self.shifts = {k: [tuple(p) for p in v] for k, v in register["shifts"].items()}
        self.mill_classes = {k: list(v) for k, v in register["work_classes"].items()}
        self.reserve = dict(register["reserve"])
        for change in register["changes"]:
            win = Window(
                id=change["id"],
                scope=tuple(change["scope"]),
                period=list(change["period"]),
                slots_requested=change["slots_requested"],
            )
            self.windows[win.id] = win

    # -- 时间推进（跨班失效） ----------------------------------------------

    def advance(self, moment: int) -> list:
        """把逻辑时钟推到 ``moment``，处理跨班失效，返回本次失效的承诺摘要。"""
        expired = []
        for pledge in self.pledges:
            if pledge.status != "active":
                continue
            boundary = self._shift_boundary_after(pledge.mill, pledge.created_at)
            deadline = min(pledge.valid_until, boundary) if boundary else pledge.valid_until
            if moment >= deadline:
                pledge.status = "expired"
                pledge.history.append({"at": moment, "reason": "shift_or_approval_expired"})
                expired.append({"mill": pledge.mill, "classes": list(pledge.classes),
                                "direction": pledge.direction, "at": moment})
        self.clock = max(self.clock, moment)
        return expired

    def _shift_boundary_after(self, mill: str, moment: int) -> int | None:
        """该 mill 在 ``moment`` 之后的第一个下班/接班边界。"""
        boundaries = [end for start, end in self.shifts.get(mill, []) if start <= moment < end]
        return min(boundaries) if boundaries else None

    def _pledge_valid(self, pledge: Pledge, moment: int) -> bool:
        if pledge.status != "active":
            return False
        boundary = self._shift_boundary_after(pledge.mill, pledge.created_at)
        deadline = min(pledge.valid_until, boundary) if boundary else pledge.valid_until
        return moment < deadline

    # -- 车间承诺 -----------------------------------------------------------

    def pledge(self, mill, allow, valid_until, at, mode="stop", direction=None) -> Pledge:
        """登记车间承诺。同一 mill+direction 的旧承诺被撤回（不沿用）。"""
        self.advance(at)
        if mill not in self.mill_classes:
            raise ValueError(f"unknown mill {mill}")
        unknown = [c for c in allow if c not in self.mill_classes[mill]]
        if unknown:
            raise ValueError(f"mill {mill} 不承接类别 {unknown}")
        for old in self.pledges:
            if (old.mill == mill and old.direction == direction
                    and old.status == "active" and old.classes == tuple(allow)):
                old.status = "retracted"
                old.history.append({"at": at, "reason": "replaced_by_new_pledge"})
        p = Pledge(mill=mill, classes=tuple(allow), valid_until=valid_until,
                   created_at=at, mode=mode, direction=direction)
        self.pledges.append(p)
        self._recompute_window_readiness()
        return p

    def retract(self, mill: str, at: int, direction: str | None = None) -> int:
        """车间撤回同意（生产侧 retract_previous）。返回撤回条数。"""
        self.advance(at)
        n = 0
        for p in self.pledges:
            if p.mill == mill and p.status == "active" and (direction is None or p.direction == direction):
                p.status = "retracted"
                p.history.append({"at": at, "reason": "mill_retracted"})
                n += 1
        self._recompute_window_readiness()
        return n

    def active_pledges(self, mill: str, moment: int):
        return [p for p in self.pledges if p.mill == mill and self._pledge_valid(p, moment)]

    # -- 窗口批准与并发容量核对 --------------------------------------------

    def approve_window(self, win_id: str, at: int) -> dict:
        """园区批准维护窗口。单独可行不代表可并发：核对同一应急队列容量。"""
        self.advance(at)
        win = self.windows[win_id]
        blockers = self._approval_blockers(win, at)
        if blockers:
            win.blockers = blockers
            return {"approved": False, "window": win_id, "blockers": blockers}
        win.status = "approved"
        win.approved_at = at
        win.blockers = []
        self._recompute_window_readiness()
        return {"approved": True, "window": win_id}

    def _approval_blockers(self, win: Window, at: int) -> list:
        blockers = []
        # 1) 受影响车间在窗口时段内必须有覆盖该时段的有效承诺，
        #    托底类别还必须在应急队列允许范围内
        for mill in win.scope:
            covered_classes, covered_dirs = set(), set()
            for p in self.pledges:
                if p.mill != mill or p.status != "active":
                    continue
                covers_window = p.created_at <= win.period[0] and p.valid_until >= win.period[1]
                if not covers_window:
                    continue
                if p.mode == "backup":
                    bad = [c for c in p.classes if c not in self.reserve["allowed"]]
                    if bad:
                        blockers.append({"type": "reserve_class_not_allowed",
                                         "mill": mill, "classes": bad})
                covered_classes.update(p.classes)
                if p.direction:
                    covered_dirs.add(p.direction)
            missing = [c for c in self.mill_classes.get(mill, []) if c not in covered_classes]
            if not covered_classes:
                # 没有任何覆盖窗口时段的有效承诺
                blockers.append({"type": "no_active_pledge", "mill": mill,
                                 "classes": list(self.mill_classes.get(mill, []))})
            win.classes = tuple(sorted(covered_classes))
            win.directions = tuple(sorted(covered_dirs))
        # 2) 应急队列容量：与所有已批准/在批窗口做时段重叠核对
        for other in self.windows.values():
            if other.id == win.id or other.status not in ("approved", "active"):
                continue
            if self._overlap(win.period, other.period):
                total = win.slots_requested + other.slots_requested
                if total > self.reserve["capacity"]:
                    blockers.append({
                        "type": "reserve_capacity_exceeded",
                        "with": other.id,
                        "slots_total": total,
                        "capacity": self.reserve["capacity"],
                        "overlap": self._overlap_range(win.period, other.period),
                    })
        return blockers

    @staticmethod
    def _overlap(a, b):
        return a[0] < b[1] and b[0] < a[1]

    @staticmethod
    def _overlap_range(a, b):
        return [max(a[0], b[0]), min(a[1], b[1])]

    # -- 窗口延长：只重新判断有变化的车间/方向 ------------------------------

    def extend_window(self, win_id: str, extend_to: int, at: int) -> dict:
        self.advance(at)
        win = self.windows[win_id]
        if extend_to <= win.period[1]:
            raise ValueError("延长时间必须晚于当前结束时间")
        old_end = win.period[1]
        old_overlap = {w.id for w in self.windows.values()
                       if w.id != win.id and self._overlap(win.period, w.period)}
        win.period[1] = extend_to
        win.extended = True

        changed = []
        # 只判断受延长段影响的车间与方向：承诺/班表边界不再覆盖新结束时刻的
        boundary_at_old = {mill: self._shift_boundary_after(mill, old_end) for mill in win.scope}
        for mill in win.scope:
            for p in self.active_pledges(mill, at):
                boundary = boundary_at_old.get(mill)
                uncovered = (
                    p.valid_until < extend_to
                    or (boundary is not None and boundary < extend_to)
                )
                if uncovered:
                    changed.append({"mill": mill, "direction": p.direction,
                                    "classes": list(p.classes),
                                    "reason": "pledge_does_not_cover_extension"})
        new_overlap = {w.id for w in self.windows.values()
                       if w.id != win.id and self._overlap(win.period, w.period)}
        for added in sorted(new_overlap - old_overlap):
            changed.append({"mill": None, "direction": None,
                            "classes": [], "reason": f"new_overlap_with:{added}"})
        win.reeval_notes.append({"at": at, "extended_to": extend_to, "changed": changed})
        self._recompute_window_readiness()
        # 已完整送达的工单保持可查：deliveries 不做任何删除
        return {"window": win_id, "extended_to": extend_to, "reevaluated": changed,
                "deliveries_kept": len(self.deliveries)}

    # -- 送达事实与工单（来自联调端 intake，不可变） ------------------------

    def note_delivery(self, event: dict) -> None:
        facts = event["facts"]
        self.deliveries.append({"sequence": event["sequence"], "at": event["at"],
                                "source": event["source"], "facts": facts})
        self.clock = max(self.clock, event["at"])

    def backup_accept(self, refs, mill, cls, direction, at) -> None:
        for ref in refs:
            order = self.orders.get(ref)
            if order is None:
                order = Order(ref=ref, mill=mill, cls=cls, direction=direction, delivered_at=at)
                self.orders[ref] = order
            if order.status == OrderStage.PRIMARY_PENDING:
                order.record(at, OrderStage.ROUTED_BACKUP, "送入应急队列，尚未领走")

    def backup_claim(self, refs, at) -> None:
        for ref in refs:
            order = self._require_order(ref)
            if order.status == OrderStage.ROUTED_BACKUP:
                order.record(at, OrderStage.BACKUP_STARTED, "备用端工位已领走开始执行")

    def complete_order(self, ref: str, at: int) -> None:
        order = self._require_order(ref)
        order.record(at, OrderStage.COMPLETED, "在场班组按原工单完成")

    def _require_order(self, ref: str) -> Order:
        if ref not in self.orders:
            raise KeyError(f"unknown order {ref}")
        return self.orders[ref]

    # -- 恢复回报与回执核实 -------------------------------------------------

    def report_restore(self, win_id: str, restored_at: int, ready_classes, at: int) -> dict:
        """维护方报恢复。链路正常只是必要条件，不能替车间确认接回。"""
        self.advance(at)
        win = self.windows[win_id]
        win.restore = {"restored_at": restored_at, "ready_classes": list(ready_classes),
                       "reported_at": at}
        # 向联调端独立核实链路回执（失败不阻塞登记，只标注未核实）
        evidence = None
        if self.client is not None:
            evidence = self.client.fetch_receipt("restore-report", "network-desk", win_id)
        win.link_evidence = evidence
        self._recompute_window_readiness()
        return {"window": win_id, "restore": win.restore,
                "link_verified": bool(evidence and evidence.get("link_restored")),
                "note": "链路恢复不等于业务接回，接回须车间逐类确认"}

    def verify_work_status(self, ref: str) -> dict | None:
        """通过联调端工位回执核实备用端执行事实。"""
        if self.client is None:
            return None
        return self.client.fetch_receipt("work-status", self.reserve["desk"], ref)

    # -- 车间逐类接回 -------------------------------------------------------

    def takeback(self, mill: str, classes, at: int, direction: str | None = None) -> dict:
        """车间逐类确认接回。已开工单留给在场班组，未领走的按接回结果处理。"""
        self.advance(at)
        affected_windows = [w for w in self.windows.values() if mill in w.scope
                            and w.status in ("proposed", "approved", "active", "recovered")]
        if not affected_windows:
            raise ValueError(f"mill {mill} 没有处于可接回阶段的窗口")
        for win in affected_windows:
            if win.restore is None:
                win.blockers.append({"type": "restore_not_reported", "at": at})
        rerouted, left_in_place = [], []
        for cls in classes:
            for win in affected_windows:
                win.taken_back[cls] = {"at": at, "direction": direction}
            for order in self.orders.values():
                if order.mill != mill or order.cls != cls:
                    continue
                if direction is not None and order.direction != direction:
                    continue
                if order.status == OrderStage.BACKUP_STARTED:
                    # 已在备用端开始执行：按原工单留给在场班组完成
                    left_in_place.append(order.ref)
                elif order.status == OrderStage.ROUTED_BACKUP:
                    order.record(at, OrderStage.PRIMARY_RESUMED, "未领走，按车间接回结果回主端")
                    rerouted.append(order.ref)
        if all(w.taken_back for w in affected_windows):
            for win in affected_windows:
                if win.restore:
                    win.status = "recovered"
        self._recompute_window_readiness()
        return {"mill": mill, "classes": list(classes),
                "rerouted_to_primary": rerouted, "left_with_crew": left_in_place}

    def _window_classes(self, win: Window) -> set:
        """窗口涉及的类别：批准推导的类别，回退到在其车间出现的工单类别。"""
        classes = set(win.classes)
        for order in self.orders.values():
            if order.mill in win.scope:
                classes.add(order.cls)
        return classes

    # -- 就绪度重算（承诺/容量变化后统一刷新阻塞条件） ----------------------

    def _recompute_window_readiness(self) -> None:
        for win in self.windows.values():
            if win.status in ("closed",):
                continue
            blockers = []
            for mill in win.scope:
                if not self.active_pledges(mill, min(self.clock, win.period[0])):
                    blockers.append({"type": "no_active_pledge", "mill": mill})
            for other in self.windows.values():
                if other.id != win.id and other.status in ("approved", "active") \
                        and self._overlap(win.period, other.period):
                    total = win.slots_requested + other.slots_requested
                    if total > self.reserve["capacity"]:
                        blockers.append({"type": "reserve_capacity_exceeded", "with": other.id})
            # 恢复后仍未接回的类别是撤备用的阻塞条件
            if win.restore:
                pending = sorted(self._window_classes(win) - set(win.taken_back))
                if pending:
                    blockers.append({"type": "takeback_pending", "classes": pending})
            win.blockers = blockers

    # -- 角色视图：各自看到仍承诺的范围与阻塞条件 ----------------------------

    def view_for(self, role: str, mill: str | None = None) -> dict:
        moment = self.clock
        if role == "maint":
            return {"role": "maint", "windows": [
                {"id": w.id, "scope": list(w.scope), "period": list(w.period),
                 "status": w.status, "blockers": w.blockers,
                 "restore": w.restore, "link_evidence": w.link_evidence,
                 "taken_back": w.taken_back, "extensions": w.reeval_notes}
                for w in self.windows.values()]}
        if role == "mill":
            if mill is None:
                raise ValueError("mill 视图需要指定 mill")
            return {"role": "mill", "mill": mill, "now": moment,
                    "active_pledges": [
                        {"classes": list(p.classes), "mode": p.mode,
                         "direction": p.direction, "valid_until": p.valid_until}
                        for p in self.active_pledges(mill, moment)],
                    "can_accept_now": self._mill_can_accept(mill, moment),
                    "orders": [{"ref": o.ref, "class": o.cls, "status": o.status}
                               for o in self.orders.values() if o.mill == mill]}
        if role == "support":
            queued = [o.ref for o in self.orders.values()
                      if o.status in (OrderStage.ROUTED_BACKUP, OrderStage.BACKUP_STARTED)]
            claimed = [o.ref for o in self.orders.values()
                       if o.status == OrderStage.BACKUP_STARTED]
            occupancy = len(queued)
            pending_takeback = {}
            for w in self.windows.values():
                if w.restore:
                    pending = sorted(self._window_classes(w) - set(w.taken_back))
                    if pending:
                        pending_takeback[w.id] = pending
            return {"role": "support", "reserve_desk": self.reserve["desk"],
                    "capacity": self.reserve["capacity"], "occupancy": occupancy,
                    "queued": queued, "claimed_by_crew": claimed,
                    "withdraw_blockers": self._support_blockers(claimed, pending_takeback),
                    "pending_takeback": pending_takeback}
        raise ValueError(f"unknown role {role}")

    def _mill_can_accept(self, mill: str, moment: int) -> list:
        """车间当前还能接的类别：未被有效停接承诺覆盖的类别。"""
        stopped = set()
        for p in self.active_pledges(mill, moment):
            if p.mode == "stop":
                stopped.update(p.classes)
        return [c for c in self.mill_classes.get(mill, []) if c not in stopped]

    @staticmethod
    def _support_blockers(claimed, pending_takeback) -> list:
        blockers = []
        if claimed:
            blockers.append({"type": "crew_still_working", "orders": claimed})
        if pending_takeback:
            blockers.append({"type": "takeback_pending", "windows": pending_takeback})
        return blockers

    # -- 联调端 intake 事件统一入口 -----------------------------------------

    def apply_event(self, event: dict) -> dict:
        self.note_delivery(event)
        at = event["at"]
        facts = event["facts"]
        source = event["source"]
        if source == "production":
            if facts.get("retract_previous"):
                return {"handled": "retract", "count": self.retract(facts["mill"], at,
                                                                     facts.get("direction"))}
            pledge = self.pledge(facts["mill"], facts["allow"],
                                 facts["approved_until"], at,
                                 mode=facts.get("mode", "stop"),
                                 direction=facts.get("direction"))
            return {"handled": "pledge",
                    "pledge": {"mill": pledge.mill, "classes": list(pledge.classes),
                               "mode": pledge.mode, "direction": pledge.direction,
                               "valid_until": pledge.valid_until, "status": pledge.status}}
        if source == "backup":
            mill = facts.get("mill", "mill-1")
            cls = facts.get("class", "weigh")
            direction = facts.get("direction")
            self.backup_accept(facts.get("accepted", []), mill, cls, direction, at)
            self.backup_claim(facts.get("claimed", []), at)
            return {"handled": "backup", "accepted": facts.get("accepted", []),
                    "claimed": facts.get("claimed", [])}
        if source == "maint":
            return {"handled": "extend", **self.extend_window(facts["id"], facts["extend_to"], at)}
        if source == "support":
            return {"handled": "restore",
                    **self.report_restore(facts["link"], facts["restored_at"],
                                          facts.get("ready_classes", []), at)}
        return {"handled": "ignored", "source": source}

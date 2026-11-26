# 园区专网维护的业务托底与交还

工单和网络均使用合成标识，维护范围为业务类别与现场工位。时段使用班表相对分钟。联调数据不连接真实专网。

所有资料供本地联调，为合成记录。`samples/register.json` 是基础台账，`samples/intake.json` 是送达的业务事实，`samples/receipts.json` 是对端查询合同。

## 本地资料端

使用 Python 3.11.2 标准库。服务只监听回环地址。

```sh
python3 -m unittest -v
python3 partner_feed.py --port 8765
```

- `GET /register` 读取台账。
- `GET /intake?since=0&limit=50` 读取一页到达记录，将返回的 `cursor` 传给下一次查询的 `since`。消息含 `sequence`、`at`、`source`、`facts`，其中 `at` 是收到记录的时间，业务发生时间可在 `facts` 中明确报告。
- `GET /receipt?action=操作&desk=经办方&ref=编号` 以三项值匹配样例。对端返回 `evidence` 作为独立事实。标记 `delayed` 的样例首次查询返回 503，后续可以读取回执。未知编号返回 404。联调端不会自行办理业务操作。

通过 `--samples` 指定另一组同名资料可换样例。临时不可查的状态按联调进程保存。

## 业务承诺协调服务端

`coordinator.py` 是纯逻辑协调引擎，`feed_client.py` 只从上述联调端拉取到达事实与回执，`service.py` 是仅监听回环地址的 HTTP 服务。链路与工位回执都来自本地联调端，不接真实设备，也不替对端办理业务。

```sh
python3 partner_feed.py --port 8765
python3 service.py --port 8801 --feed-port 8765
python3 -m unittest -v
```

所有写接口按 `X-Role` 区分 `maint`（维护）、`mill`（车间/生产）、`support`（备用值守），越权返回 403；时间一律由请求体的 `at`（班表相对分钟）携带。

### 接口

- `POST /events/ingest`（maint）：从联调端分页拉取 `intake` 全部事件并顺序应用；`POST /events/{n}` 可直发单条联调事件。
- `POST /mills/{mill}/pledge`（mill）：车间承诺 `stop`（停接）或 `backup`（托底），带 `allow` 类别、`valid_until`、可选 `direction`。同车间同方向重复承诺会撤回旧承诺，不沿用。
- `POST /mills/{mill}/retract`（mill）：撤回同意。
- `POST /windows/{id}/approve`（maint）：园区批准，核对承诺覆盖、应急队列允许类别与并发容量。
- `POST /windows/{id}/extend`（maint）：延长窗口，只重新判断受影响的车间/方向，已送达台账不删除。
- `POST /windows/{id}/restore`（maint）：维护方恢复回报，同时向联调端核实 `restore-report` 回执；链路恢复不等于业务接回。
- `POST /mills/{mill}/takeback`（mill）：车间逐类确认接回。
- `GET /view/maint`、`GET /view/support`、`GET /view/mill/{mill}`：各角色只看自己仍承诺的范围与阻塞条件。
- `GET /windows`、`GET /orders`、`GET /deliveries`：窗口、工单（含状态变迁历史）、不可变送达台账。

### 规则落点

- **跨班不沿用**：承诺同时受 `valid_until` 与班表边界约束，`advance` 到下一班即置 `expired`，新一班须重新承诺。
- **托底容量与约定类别**：批准时托底类别必须在台账 `reserve.allowed` 内；两个窗口时段重叠且 `slots_requested` 之和超过 `capacity` 时，即使各自单独可行，并发批准也会被 `reserve_capacity_exceeded` 阻塞。
- **延长重判**：只把承诺时限或班表边界不再覆盖新结束时刻的车间/方向列入 `reevaluated`，并标注新产生的窗口重叠；`deliveries` 与已建工单保持可查。
- **恢复不能撤备用**：`restore` 只登记并核实链路，工单状态不自动变化；`support` 视图持续给出 `takeback_pending` 与班组仍在执行的 `crew_still_working` 阻塞。
- **逐类接回**：`backup_started` 的工单按原工单留给在场班组（完工走 `complete_order`），`routed_backup` 未领走的工单按接回结果转 `primary_resumed`。
- **晚到的恢复回报**：车间已撤回后，维护经办仍可通过联调端回执查明链路恢复，但服务端不会据此替车间重建承诺。

回执查询对 503 自动退避重试、对 404 返回空证据，由引擎标注为未核实而非伪造结论。

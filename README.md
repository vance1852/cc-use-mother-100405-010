# 执行国际大科学数据治理协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础上，项目实现了**国际科研合作贡献与数据治理服务**（`governance` / `gov_service`），把成员身份、国家或机构承诺、实缴贡献、仪器排期、数据集版本、敏感级别、禁运窗口、使用提案、署名规则与治理决议串成一条可审计的权利来源链。

## 治理服务如何做决定

- **提交时快照定影**：使用提案提交的一刻，系统截取当时有效的章程版本与全体成员贡献快照（计算哈希并随提案持久化）。表决资格、法定人数分母、表决权重全部按该快照计算；此后章程修订、权重调整、迟交贡献、成员退出都**不会回改**已生效的决议与许可。
- **利益冲突回避**：在投票时刻实时判定（即使冲突在提案提交后才产生），命中数据拥有方或申请方的代表必须回避，且不进入法定人数分母。
- **只追加的许可账本**：授予、暂停、恢复、退出撤销、版本被更正取代、到期都写为带原因与时间的新事件；历史行永不修改。
- **后继权利义务**：迟交贡献只影响之后的新提案；数据更正确生新版本并使旧版本许可失效；许可暂停可恢复，而退出与版本取代必须重新提案；成果发表只生成后继署名责任。
- **并发与重复**：每提案每成员一票（数据库唯一约束）、下载回调按回调键只计一次；部分唯一索引保证同一成员对同一数据集任意时刻最多一个有效许可版本。写事务在多线程下串行化。
- **可解释、可追溯**：合作方经 `GET /gov/access-explain` 可看到自己"为何拥有/失去"某项权限的逐项门禁与证据（决议、章程版本、快照哈希、禁运、许可事件）；秘书处可经许可时间线、下载记录、成果署名和未履行承诺接口追溯每次数据使用与承诺。

### 主要写接口（`POST`，均以 `request_id` 幂等，`X-Actor-Id` 标识操作者）

`/gov/members`、`/gov/members/weight`、`/gov/members/withdraw`、`/gov/baselines`、
`/gov/commitments`、`/gov/contributions`、`/gov/instrument-slots`、
`/gov/instrument-slots/delivered`、`/gov/datasets`、`/gov/proposals`、`/gov/ballots`、
`/gov/proposals/resolve`、`/gov/grants/suspend`、`/gov/grants/resume`、
`/gov/download-callbacks`、`/gov/publications`。

### 主要读接口（`GET`）

- `/gov/access-explain?member_id=&dataset_id=&at=`：权限逐项门禁与证据链（合作方仅能查本人所在成员）。
- `/gov/proposals/{id}`：提案、定影快照、票与决议。
- `/gov/grant-timeline`、`/gov/downloads`、`/gov/publications`、`/gov/unfulfilled`：秘书处追溯。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `governance.py`：资格、回避、计票、禁运与访问门禁的**纯函数**规则（给定快照任何人可复算）；
  - `gov_service.py`：治理应用服务（快照定影、表决决议、许可账本、解释与追溯）；
  - `gov_acceptance.py`：治理端到端离线验收。
- `tests/`：基础规则、事务边界、接口路由、治理不变量、并发互斥和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

治理领域另有独立验收：

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.gov_acceptance
```

该命令在临时库中演完一个完整故事（贡献不均的成员、提案快照定影、回避表决、权重与章程变更不倒改、禁运、暂停/恢复、迟交、数据更正、成员退出、回调去重、署名排序），并校验审计哈希链。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

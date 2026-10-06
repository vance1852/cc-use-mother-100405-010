# 执行国际大科学数据治理协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础之上，项目内置了面向气候与能源国际大科学计划的**贡献与数据治理服务**（`governance.py`），把成员身份、国家或机构承诺、实缴贡献、仪器排期、数据集版本、敏感级别、禁运窗口、使用提案、署名规则和治理决议串成可审计的权利来源。

## 治理语义

- **快照决定**：访问决定按提案提交时有效的章程版本与贡献快照计算，快照随提案持久化，迟交贡献、章程修订不会倒改既有决定与许可；
- **回避与表决**：代表可申报利益冲突，涉及冲突的表决自动回避且不计入票数；法定人数与表决权重在决议开启时快照，后续变化只向后生效；
- **后继权利**：成员退出、数据更正、许可暂停、成果发表只产生后继权利与义务，历史下载与署名记录保持有效；
- **幂等与并发**：所有写接口按 `request_id` 幂等；表决按 `(resolution_id, member_id)` 去重；下载回调按 `callback_id` 去重；同一成员在同一数据集上最多一个有效许可（部分唯一索引 + IMMEDIATE 事务 + revision 乐观并发）；
- **可解释性**：合作方经 `GET /governance/access?member_id=…&dataset_id=…` 获知为何拥有或失去某项权限；秘书处经 `GET /governance/member-standing`、`GET /governance/dataset-usage` 追溯每次数据使用、署名责任和未履行承诺。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、国际科研合作治理服务、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、治理规则和端到端验收测试。

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
PYTHONPATH=src python3 -m science_strategy_foundation.governance_acceptance
```

基础验收在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链；治理验收执行完整的章程—承诺—实缴—排期—数据集—提案—决议—许可—下载—署名—退出链路并核对全部时序与幂等规则。两者成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

治理接口位于 `/governance/` 前缀下，写入端点包括 `charters`、`members`、`commitments`、`contributions`、`instrument-slots`、`dataset-versions`、`proposals`、`resolutions`、`votes`、`downloads`、`publications` 等；查询端点包括 `access`（权限解释）、`member-standing`（承诺履行）、`dataset-usage`（使用追溯）、`proposal`、`license`、`resolution`。

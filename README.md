# vector-clock-audit

离线巡检终端在断网期间各自记录事件，本服务对汇总上来的事件批次做**向量时钟因果审计**：
校验批次是否构成完整、无循环的因果历史；对合法批次生成**最早可执行的有序因果层**，
给出唯一、可复核的离线合并次序。

零第三方依赖（纯 Python 标准库），离线环境即可构建与运行。

## 运行

```bash
# 宿主机端口由 APP_PORT 控制（默认 8000），容器内固定监听 8000
APP_PORT=9000 docker compose up -d --build

curl http://127.0.0.1:9000/healthz
# {"status": "ok"}
```

本地无 Docker 时：`python3 -m app.server`（环境变量 `HOST`/`PORT` 可覆盖监听地址）。

## 一次性验证

```bash
./verify.sh        # 可用 APP_PORT 覆盖宿主机端口
```

`verify.sh` 会校验 Compose 配置、构建镜像、启动服务并等待 `/healthz` 报告健康，
随后在服务健康后依次完成**代码测试**（容器内单元测试）、**构建检查**（字节码编译 +
镜像检查）与**接口冒烟**（容器内冒烟 + 经 `APP_PORT` 映射的宿主机侧健康检查），
全部通过以退出码 `0` 结束，任一步失败以非零退出码结束并自动清理堆栈。

## API

### `POST /api/events/audit`

请求（JSON，正文 ≤ 1 MiB）：

```json
{
  "participants": ["edge-a", "edge-b"],
  "events": [
    {"id": "a1", "participant": "edge-a", "seq": 1,
     "clock": {"edge-a": 1, "edge-b": 0}},
    {"id": "b1", "participant": "edge-b", "seq": 1,
     "clock": {"edge-a": 1, "edge-b": 1}}
  ]
}
```

约束：

- `participants`：2–32 个唯一非空字符串；
- `events`：1–2000 个事件，次序任意（输入排列不影响结果）；
- 每个事件：`id` 批次内唯一；`participant` 必须已声明；`seq` 为从 1 开始的本地序号；
  `clock` 必须恰好覆盖全部参与方且各分量为非负整数；
- 每名参与方的序号必须连续（1..n，无缺号、无重复）；
- 事件自身时钟分量必须等于其序号；
- 同一参与方的后继时钟逐分量不得回退；
- 事件声明看到另一方第 k 个事件（`clock[q] = k > 0`）时，该事件必须存在，
  且其时钟必须被当前事件逐分量覆盖并至少一项严格更小（严格支配）；
- 依赖关系不得成环。

成功响应（`200`）：

```json
{
  "event_count": 2,
  "layer_count": 2,
  "layers": [
    {"index": 0, "event_ids": ["a1"]},
    {"index": 1, "event_ids": ["b1"]}
  ],
  "merge_order": ["a1", "b1"],
  "concurrent_pairs": 0
}
```

- `layers`：按向量时钟严格支配关系生成的最早可执行因果层（事件层号 =
  其全部因果前驱的最大层号 + 1，无前驱者为第 0 层）；层内按
  （参与方, 序号, id）排序；
- `merge_order`：各层依次拼接得到的唯一离线合并次序，是可复核的拓扑序；
- `concurrent_pairs`：互不构成因果先后（并发）的事件对数。

### 错误响应

所有校验失败返回稳定错误码与相关事件 id：

```json
{
  "error": {
    "code": "SEQUENCE_GAP",
    "message": "...",
    "event_ids": ["a1", "a3"],
    "details": {}
  }
}
```

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 400 | `MALFORMED_JSON` | 正文不是合法 JSON |
| 411 | `LENGTH_REQUIRED` | 缺少 Content-Length |
| 413 | `PAYLOAD_TOO_LARGE` | 正文超过 1 MiB |
| 422 | `INVALID_SCHEMA` | 顶层结构或字段缺失/类型错误 |
| 422 | `INVALID_PARTICIPANT` | 参与方不是非空字符串 |
| 422 | `PARTICIPANT_COUNT_OUT_OF_RANGE` | 参与方数量不在 2–32（越界） |
| 422 | `DUPLICATE_PARTICIPANT` | 参与方重复 |
| 422 | `EVENT_COUNT_OUT_OF_RANGE` | 事件数量不在 1–2000（越界） |
| 422 | `INVALID_EVENT_SCHEMA` | 事件字段缺失或类型错误 |
| 422 | `DUPLICATE_EVENT_ID` | 事件 id 重复 |
| 422 | `UNKNOWN_PARTICIPANT` | 事件属于未声明的参与方 |
| 422 | `INVALID_SEQUENCE` | 序号不是 ≥1 的整数（越界） |
| 422 | `INVALID_CLOCK` | 时钟未恰好覆盖全部参与方或分量非非负整数 |
| 422 | `CLOCK_SELF_MISMATCH` | 自身分量不等于序号 |
| 422 | `SEQUENCE_GAP` | 参与方序号缺号/重复（缺号） |
| 422 | `CLOCK_REGRESSION` | 同一参与方后继时钟回退（回退） |
| 422 | `FUTURE_REFERENCE` | 引用了批次中尚不存在的事件（未来引用） |
| 422 | `CIRCULAR_DEPENDENCY` | 依赖关系成环（循环依赖） |
| 422 | `DOMINATION_VIOLATION` | 被引用事件的时钟未被逐分量覆盖并严格更小 |
| 404 | `NOT_FOUND` | 未知路径 |
| 405 | `METHOD_NOT_ALLOWED` | 方法不允许 |

同一类别存在多处违规时，`event_ids` 汇总全部相关事件 id（排序去重），
`details` 给出可复核的结构化细节（如回退对、悬空引用、环的组成）。

## 实现要点

- 合法批次中，事件 `a` 因果先于 `b` 当且仅当 `a.seq <= b.clock[a.participant]`
  （与向量时钟严格支配等价），因此分层与并发对计数均为
  O(事件数 × 参与方数)，2000 事件 / 32 参与方的批次约 100 ms 完成；
- 环检测使用迭代式 Tarjan SCC，所有大小 > 1 的强连通分量即为循环依赖；
- 校验按固定类别顺序进行，类别内汇总全部违规并排序输出，保证结果与输入排列无关。

## 目录

```
app/audit.py     核心：校验管线、因果分层、并发对计数
app/server.py    HTTP 服务（stdlib）：/healthz 与 /api/events/audit
tests/           单元测试（含 O(n^2) 暴力对拍与随机性质测试）与冒烟脚本
Dockerfile       零依赖镜像（python:3.12-slim，非 root 运行，含 HEALTHCHECK）
docker-compose.yml  APP_PORT 控制宿主机端口，/healthz 纳入健康检查
verify.sh        一次性验证：构建 → 健康等待 → 测试/构建检查/冒烟 → 退出码
```

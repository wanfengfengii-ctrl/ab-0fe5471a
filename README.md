# Vector-Clock Causal Audit Service

离线巡检终端在断网期间各自记录事件，本服务对汇总后的批次校验向量时钟因果历史的
完整性与无环性，并为合法批次生成唯一、可复核的离线合并次序。

## 接口

### `GET /healthz`

健康检查，返回 `200 {"status":"ok"}`。已纳入 Dockerfile `HEALTHCHECK` 与 Compose
`healthcheck`。

### `POST /api/events/audit`

请求体（JSON，≤ 1 MiB）：

```json
{
  "participants": ["alpha", "beta"],
  "events": [
    {"id": "a1", "participant": "alpha", "seq": 1, "clock": {"alpha": 1, "beta": 0}},
    {"id": "b1", "participant": "beta",  "seq": 1, "clock": {"alpha": 0, "beta": 1}},
    {"id": "a2", "participant": "alpha", "seq": 2, "clock": {"alpha": 2, "beta": 1}}
  ]
}
```

约束：

- `participants`：2–32 个唯一、非空名称。
- `events`：1–2000 个事件，可以任意顺序提交；输入排列不会改变结果。
- 每个事件：`id` 唯一；`participant` 必须已声明；`seq` 为该参与方从 1 开始的本地
  序号；`clock` 必须恰好覆盖全部参与方且分量为非负整数。
- 每名参与方的序号必须连续（1..n，无缺号、无重复）。
- 事件自身时钟分量必须等于其序号；同一参与方后继事件的时钟不得回退。
- 事件声明看到另一方第 k 个事件（`clock[q] = k ≥ 1`）时，该事件必须存在，且其
  时钟必须被当前事件逐分量覆盖并至少一项严格更小。
- 声明的依赖关系（程序序 + 引用）不得成环。

合法批次返回 `200`：

```json
{
  "eventCount": 3,
  "layerCount": 2,
  "layers": [
    {"index": 0, "eventIds": ["a1", "b1"]},
    {"index": 1, "eventIds": ["a2"]}
  ],
  "order": ["a1", "b1", "a2"],
  "concurrentPairs": 1
}
```

- `layers`：按向量时钟严格支配关系得到的最早可执行因果层（Kahn 分层拓扑，
  每层内的事件互为并发）；层内按参与方、序号、id 排序。
- `order`：各层依次展开得到的唯一离线合并次序。
- `concurrentPairs`：因果上不可比较（互不支配）的事件对数。

非法批次返回稳定错误码与相关事件 id：

```json
{"error": {"code": "SEQUENCE_GAP", "message": "...", "eventIds": ["e1", "e3"]}}
```

| HTTP | code | 含义 |
|------|------|------|
| 413 | `PAYLOAD_TOO_LARGE` | 正文超过 1 MiB |
| 400 | `MALFORMED_JSON` | JSON 语法错误或多余数据 |
| 422 | `INVALID_REQUEST` | 顶层不是 JSON 对象 |
| 422 | `INVALID_PARTICIPANT` / `PARTICIPANT_COUNT_OUT_OF_RANGE` / `DUPLICATE_PARTICIPANT` | 参与方声明非法（越界） |
| 422 | `INVALID_EVENT` / `EVENT_COUNT_OUT_OF_RANGE` | 事件结构非法 / 数量越界 |
| 422 | `UNKNOWN_PARTICIPANT` | 事件属于未声明的参与方 |
| 422 | `DUPLICATE_EVENT_ID` | 事件 id 重复 |
| 422 | `INVALID_SEQUENCE` | 序号非正整数（越界） |
| 422 | `SEQUENCE_GAP` | 参与方序号缺号/不连续 |
| 422 | `INVALID_CLOCK` | 时钟未覆盖全部参与方或分量越界 |
| 422 | `CLOCK_OWN_MISMATCH` | 自身分量不等于序号 |
| 422 | `CLOCK_REGRESSION` | 同参与方后继时钟回退 |
| 422 | `FUTURE_REFERENCE` | 引用了批次中不存在（尚未发生）的事件 |
| 422 | `CYCLE_DETECTED` | 声明的依赖成环 |
| 422 | `CAUSALITY_VIOLATION` | 被引用事件的时钟未被严格支配 |

## 运行

```bash
APP_PORT=8080 docker compose up -d --build   # 宿主机端口由 APP_PORT 决定
curl http://127.0.0.1:8080/healthz
```

## 一次性验证

```bash
./verify.sh        # 可用 APP_PORT 覆盖端口
```

`verify.sh` 依次完成：Compose 配置校验、Docker 内 gofmt/vet/单元测试、镜像构建、
等待服务健康、接口冒烟（合法批次、排列不变性、各类 422/413/400 拒绝），并以退出码
报告结果（0 为通过）。

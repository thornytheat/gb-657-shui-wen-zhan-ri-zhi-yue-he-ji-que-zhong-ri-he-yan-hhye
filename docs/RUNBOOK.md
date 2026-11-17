# 运行手册 hydrocalendar

## 1 启动

```bash
cd projects/gb-657
docker compose up -d --build
curl -sS http://localhost:18216/api/health
```

页面：<http://localhost:18216/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18216；SQLite 落在命名卷 `hydrocalendar-data` 的 `/data/hydrocalendar.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18216/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18216/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18216/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列、关账补账。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

### 关账 / 回读 / 补账

```bash
# 0. 跑样例（关账补账样例跨 2026-01 ~ 2026-04）
curl -sS -X POST http://localhost:18216/api/runs -d '{"sample":"readings_ledger.csv"}'
# 1. 看账本版本与开放月
curl -sS http://localhost:18216/api/ledger | python3 -m json.tool
# 2. 关账 2026-01（决定必须带当前版本号，过期决定 409）
curl -sS -X POST http://localhost:18216/api/ledger/close \
  -d '{"month":"2026-01","expected_version":0}'
# 3. 回读：补读行并入基准批次重算最新应收（原批次不动）
curl -sS -X POST http://localhost:18216/api/ledger/reread \
  -d '{"readings":[{"meter_id":"M0002","register":"TOTAL","read_at":"2026-02-01T00:00:00+08:00","reading":"1001.0","unit":"kWh"}]}'
# 4. 补账：已关账差额记到开放月 2026-04（正补收、负退补，按原价）
curl -sS -X POST http://localhost:18216/api/ledger/supplement \
  -d '{"reread_id":"rr-…","target_month":"2026-04","expected_version":1}'
# 5. 落账中断后续跑（crash_after 是测试钩子，生产不传）
curl -sS -X POST http://localhost:18216/api/ledger/supplement/resume \
  -d '{"request_id":"sr-…"}'
# 6. 撤销补账：整批反向分录，不删旧账
curl -sS -X POST http://localhost:18216/api/ledger/reverse \
  -d '{"request_id":"sr-…","expected_version":2}'
# 7. 核对：原账 + 净补账 = 最新应收
curl -sS http://localhost:18216/api/ledger/reconcile | python3 -m json.tool
```

要点：同一 `request_id` 重试不重复入账；同一（补读， 目标月）只允许一个未撤销的补账批次；有进行中落账的月不能关账；撤销后才能对同一（补读， 目标月）重新补账。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 409 `E_LEDGER_STALE` | 关账/补账/撤销决定基于的账本版本已过期 | 重新 GET `/api/ledger` 取版本后再提交 |
| 409 `E_MONTH_CLOSED` | 目标月已关账 | 差额只能记入开放月 |
| 409 `E_POSTING_IN_FLIGHT` | 该月有进行中的落账 | 先 `/api/ledger/supplement/resume` 续跑完成 |
| 409 `E_RUN_NOT_DONE` | 落账未完成就撤销 | 先续跑再撤销 |
| 409 `E_RUN_ACTIVE` | 同一（补读， 目标月）已有补账批次 | 如需调整先撤销原批次 |
| 422 `E_REREAD_INVALID` | 补读行未通过契约/质量校验 | 按 detail.rows 的原因码修正 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HYDROCALENDAR_DB` | `/data/hydrocalendar.sqlite` | SQLite 路径 |
| `HYDROCALENDAR_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。

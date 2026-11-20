# 税则税号分组供数 API

新版税则拆并税号后，进口额同比要按**同一组商品**计算。本服务用 FastAPI + SQLite 实现，
并在首版对照生效后提供核验单登记/撤销与凭证签发：稽核员交接一组进口额时，可凭最少张
有效核验单拼出该组当年的完整税号覆盖与合计。

1. `PUT /base` 先固定相邻两年各税号进口额（整数分）；存在对照版本后底表即锁定（409）。
2. `POST /mappings` 按递增版号提交**完整**新旧税号对照。同号也按年份区分——未变税号
   也要以 `(code, code)` 显式出现，两侧分别对应底表两个年份的税号集合。
3. 经对照**直接或间接**相连的税号归为一组（二部图连通分量）。
4. `GET /summary` 现行汇总：组号、两侧税号、各年合计。
5. 改版时成员相同的组**沿用组号和金额**（不重算），只有新组才读取底表金额明细；
   组号单调递增、绝不复用。
6. `GET /changes/{version}` 按版取供数变更：首版给全量；后续给被替换的版号、
   应撤下的组号、应写入的汇总。变更与汇总在同一事务生效，落 SQLite，重启后仍可取。
7. 对照漏掉底表税号或引用未知税号 → 422 **整版拒收**，原供数仍可查询。
8. 同版重传原对照 → 200 返回当时的变更（幂等）；同版改内容 → 409 冲突；
   版号不高于现行版且不存在 → 409。
9. 并发改版串行提交，差分以**实际替换的上一版**为准（提交时在读已加写锁的事务内
   重取现行版号）。
10. **首版对照生效后**开放核验单登记/撤销（`/forms`）：每单填写唯一单号、年份、
    税号清单和合计金额（整数分），合计须与底表相符，否则 422 整单拒收；撤销为
    软删除，原单保留并带 `revoked_at`。
11. 凭证签发（`/vouchers`）传所见对照版、组号、年份和至多 20 个已登记单号：
    从**有效单**中选**最少张**，每张整用、税号互不重叠且恰好覆盖该组当年成员；
    拼不齐返回 422（提交单中含已撤销单导致拼不齐返回 409）。选单期间可继续换版、撤单。
12. 写入凭证时在写事务内重读：所见版已被替换或入选单已撤销 → 409 冲突且不生成凭证；
    未入选单撤销不影响签发。凭号可查签发时版号、组号、年份、所用单号和合计。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，SQLite 落在命名卷 `tariff-data`（`/data/app.db`），
重启后版本与变更保留。

### 本地

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # DB_PATH 默认 ./data/app.db
python -m pytest tests/ -q             # 跑测试
```

## 在 Swagger 上演示改版和取数

打开 <http://localhost:8000/docs>（`/` 会自动跳转），依次 “Try it out”：

1. **底表 / `PUT /base`** —— 固定 2023/2024 两年进口额：

   ```json
   {"amounts":[
     {"year":2023,"code":"A","amount_cents":100},
     {"year":2023,"code":"B","amount_cents":200},
     {"year":2023,"code":"C","amount_cents":300},
     {"year":2023,"code":"D","amount_cents":400},
     {"year":2024,"code":"A","amount_cents":110},
     {"year":2024,"code":"B","amount_cents":210},
     {"year":2024,"code":"C","amount_cents":310},
     {"year":2024,"code":"D","amount_cents":410}]}
   ```

2. **对照版本 / `POST /mappings`** —— v1 把 C、D 并成一组：

   ```json
   {"version":1,"entries":[
     {"old_code":"A","new_code":"A"},
     {"old_code":"B","new_code":"B"},
     {"old_code":"C","new_code":"C"},
     {"old_code":"C","new_code":"D"},
     {"old_code":"D","new_code":"D"}]}
   ```

   返回 201：首版全量，组 3 = `{C,D | C,D}`，合计 700 / 720 分。

3. **供数 / `GET /summary`** —— 现行 3 个组：1 `{A}`、2 `{B}`、3 `{C,D}`。
4. **`POST /mappings`** —— v2 拆组（四个单税号恒等对照），返回 201：
   `replaced_version=1`、`removed_group_ids=[3]`、应写入组 4 `{C}`、组 5 `{D}`；
   组 1、2 成员未变，沿用组号和金额，不出现在变更里。
5. **供数 / `GET /changes/{version}`** —— 分别取 1、2 两版变更核对。
6. **旧版重传** —— 再以原内容 `POST /mappings` version=1 → 200 返回当时的全量变更；
   把 entries 改掉再传 version=1 → 409。
7. **拒收** —— version=3 漏掉 `D` → 422 列出 `missing_old_codes`/`missing_new_codes`，
   `GET /summary` 仍是 v2。
8. **核验单 / `POST /forms`** —— 首版生效后登记（合计必须等于底表金额之和）：

   ```json
   {"form_no":"F-C","year":2023,"tax_codes":["C"],"total_cents":300}
   {"form_no":"F-D","year":2023,"tax_codes":["D"],"total_cents":400}
   {"form_no":"F-CD","year":2023,"tax_codes":["C","D"],"total_cents":700}
   {"form_no":"F-CD24","year":2024,"tax_codes":["C","D"],"total_cents":720}
   ```

   再传一个金额不符的单（如 `total_cents=999`）→ 422 且带 `expected_total_cents`；
   单号重复 → 409。
9. **预演 / `POST /vouchers/preview`** —— 传所见版、组号、年份和候选单号，返回最少张
   选单结果但不写凭证：

   ```json
   {"observed_version":1,"group_id":3,"year":2023,
    "form_nos":["F-C","F-D","F-CD"]}
   ```

   返回 `selected_form_nos=["F-CD"]`（一张优于两张）。只给拼不齐的候选 → 422。
10. **签发 / `POST /vouchers`** —— 同样的请求体正式签发，返回凭号 `V000001`、
    签发版号、所用单号与合计；`GET /vouchers/V000001` 可回查全部内容。
11. **撤单/签发交错** —— `POST /forms/F-C/revoke` 撤销未入选单后，仍可用 `F-CD`
    签发（响应里 `ignored_revoked_form_nos` 列出被忽略的撤销单）；再撤销入选所需的
    `F-CD` 后签发 → 409 且 `GET /vouchers` 中无新凭证。
12. **换版冲突** —— 提交 v2 拆组后，仍以 `observed_version=1` 签发 → 409/422 且不落
    凭证；改用现行版号与现存组号后才能签发。

也可以用脚本一键演示同样流程：`./scripts/demo.sh`（需服务已启动）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/base` | 固定相邻两年底表（整数分）；有版本后 409 |
| GET | `/base` | 查看底表 |
| POST | `/mappings` | 提交完整对照；201 新版 / 200 同版重传 / 409 冲突 / 422 拒收 |
| GET | `/mappings/{version}` | 查看某版对照内容 |
| GET | `/versions` | 版本列表与现行版号 |
| GET | `/summary` | 现行汇总：组号、两侧税号、各年合计 |
| GET | `/changes/{version}` | 按版取供数变更（首版全量） |
| POST | `/forms` | 登记核验单；金额与底表不符/税号未知/重复 → 422，单号重复 → 409 |
| POST | `/forms/{form_no}/revoke` | 撤销核验单（软删除，原单保留；重复撤销幂等） |
| GET | `/forms` | 核验单列表，可按 `status=active|revoked` 过滤 |
| GET | `/forms/{form_no}` | 按单号查核验单（含已撤销） |
| POST | `/vouchers/preview` | 预演最少张精确覆盖选单，不写凭证 |
| POST | `/vouchers` | 签发凭证；版被换/入选单被撤 → 409，拼不齐 → 422 |
| GET | `/vouchers` | 已签发凭证列表 |
| GET | `/vouchers/{voucher_no}` | 按凭号查版号、组号、年份、所用单号、合计 |

变更记录结构：

```json
{"version": 2, "replaced_version": 1,
 "removed_group_ids": [3],
 "summaries": [{"group_id": 4, "old_codes": ["C"], "new_codes": ["C"],
                "old_total_cents": 300, "new_total_cents": 310}]}
```

## 设计要点

- **分组**：对照边构成新旧两侧的二部图，连通分量即组；组成员哈希（两侧税号集合）
  决定组身份。
- **沿用**：改版时按成员哈希比对上一版组，命中则沿用组号与库存合计，不触碰底表；
  未命中的新组才 `SUM` 底表金额，组号取自持久化计数器（单调、不复用）。
- **一致性与并发**：写路径全程互斥锁 + `BEGIN IMMEDIATE`；版本行、变更行、现行汇总、
  计数器在同一事务提交，`replaced_version` 在事务内重取，故并发改版以实际替换的
  上一版为准。
- **幂等/冲突**：每版保存对照内容哈希；同版同哈希直接回放当时存的变更记录，
  不同哈希 409。
- **持久化**：`versions` / `changes` / `current_groups` / `meta` / `forms` / `vouchers`
  全在 SQLite 文件中，重启后按版取变更、现行汇总、重传幂等、核验单与凭证全部保留；
  凭号取自持久化计数器（`V000001` 起，单调不复用）。
- **核验单**：登记在写事务内核对底表——年份须属于底表两年、税号存在且互不重复、
  合计等于底表金额之和；撤销只把状态置为 `revoked` 并记录时间，行不删除。
- **精确覆盖选单**：把每张候选单视为该年税号集合的一个子集，用 DFS 求**最小精确
  覆盖**（分支于一个未覆盖代表税号，剔除与已选重叠的单）；先按张数最少、再按单号
  字典序决胜，结果确定。候选仅限有效单，撤销单在进入选择前即被剔除。
- **签发一致性**：选择与写入共用同一把进程锁；写入在 `BEGIN IMMEDIATE` 事务内重读
  现行版号与入选单状态，任一失效即回滚并返回 409，绝不生成半成品凭证。
- **冲突分级**：所见版被替换、入选单在写入前被撤 → 409（调用方刷新后可重试）；
  组号不存在、年份/单号非法、有效单本身拼不齐 → 422（数据不满足签发条件）。

## 目录

```
app/
  main.py      # FastAPI 路由与 App 工厂（DB_PATH 环境变量）
  store.py     # SQLite 持久化 + 分组/差分/校验/精确覆盖选单领域逻辑
  schemas.py   # Pydantic 模型（含核验单、凭证）
tests/test_api.py            # 底表/对照/供数基线测试
tests/test_forms_vouchers.py # 核验单登记撤销与凭证签发测试（含并发、重启）
scripts/demo.sh
Dockerfile / docker-compose.yml
requirements.txt / requirements-dev.txt
```

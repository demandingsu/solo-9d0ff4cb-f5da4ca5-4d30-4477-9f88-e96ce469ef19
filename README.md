# Library Journal Access API

高校图书馆期刊可访问性判定服务。把出版商/数据库商提供的期刊覆盖表按**资源包**整理进库，
检索系统用「查询日 + ISSN」查出**当日实际可打开的出版日期段**及其来源包；电子资源排障员
还可按**请求号**录入读者的文章访问尝试，系统逐条核对实际结果与馆藏授权是否相符。

技术栈：FastAPI + SQLite（WAL 模式），Python 标准库 `sqlite3`，无 ORM。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI，可直接操作）
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 测试：pip install pytest httpx && python -m pytest tests/ -v
```

SQLite 数据文件位置由环境变量 `DB_PATH` 决定（Compose 中为 `/data/library.db`，挂在命名卷上）。

## 概念与规则

- **资源包 package**：有名称、授权起止日（`license_start`/`license_end`，闭区间）、
  启用/停用标志。只有**查询日当天处于授权期内且未停用**的包才参与判定。
- **覆盖快照 coverage snapshot**：按包整体上传，每条记录 = ISSN + 出版日期范围 +
  最近禁访月数 `embargo_months`。`PUT .../coverage` 是**全量替换**，在一个事务里
  删旧插新。
- **移动禁访墙**：禁访边界随查询日移动。查询日为 `D`、禁访 `M` 个月时，该条覆盖
  可访区间 = `[start_date, min(end_date, D − M个月)]`；区间为空则该条当日不可访。
  月份相减按日历月，日数超出目标月则截到月末（如 3 月 31 日 − 1 月 = 2 月 28 日）。
- **拆段**：多个包覆盖同一期刊时，把可访区间按「来源包集合」切分——集合发生变化的
  边界处拆段，每段列出支撑它的全部来源包。停用某包只是把它从参与集合中移除，
  其他包的覆盖行原样保留，重叠段的依据不丢。
- **一致性**：每次成功写入（建包、改授权、换快照、启停用、录访问记录）在同一事务里把全局
  `data_version` 加一；写入失败整体回滚，读者继续看到旧版本。每次查询在**单个读
  事务**内完成（WAL 快照读），响应里的 `data_version` 就是本次答案实际依据的提交
  版本——并发写不会把新旧来源混进同一个答案。

## 访问核对（access verification）

- **录入** `POST /access-attempts`：按请求号 `request_no` 录入访问日 `access_date`、
  ISSN、文章出版日 `article_pub_date`、尝试的资源包 `package_id` 与结果
  `outcome`（`success` / `refused`）。请求号唯一：
  - **同号同内容重传**：接受但只记一次，响应 `duplicate: true`，不产生新版本；
  - **同号异内容重传**：`409` 拒收，响应体 `differing_fields` 指出哪些字段不一致，
    原记录原样保留；
  - 资源包不存在返回 `404`；ISSN/结果枚举非法返回 `422`。
- **核对** `GET /verifications?start_date=...&end_date=...`：按**访问日**闭区间逐条
  返回核对结果，每条包含：
  - 尝试包的**应有权限** `expected_access` 及无权限原因（停用 / 授权未生效 / 授权过期 /
    无此刊覆盖 / 出版日在覆盖范围外 / 移动禁访墙内），原因同时给机器码与中文说明；
  - **实际结果** `observed_outcome` 与核对结论 `match_result`：`consistent`（成功且
    应有权）、`expected_denial`（拒绝且本就无权）、`wrongful_denial`（应有权却被拒）、
    `unexpected_success`（无权却成功），不符时给中文 `mismatch_reason_text`；
  - **替代包** `alternative_packages`：同一访问日、同一文章**其他可服务的包**（附命中
    的覆盖行与禁访截止日）；
  - **全馆失权** `library_wide_access_loss`：仅当被拒、无任何替代包**且**尝试包本应有权
    时才为真——单包拒绝只要另有可访包就不算全馆失权；本就无权限的拒绝（禁访等）不算失权；
  - **依据** `basis`：尝试包的授权窗口、授权是否覆盖访问日、该 ISSN 的全部覆盖行
    （含每行按访问日算出的禁访截止日、是否命中文中出版日、是否被禁访挡住）及命中行。
- **重新判定**：核对结论不入库、每次查询按**当前已提交状态**现场重算。因此覆盖或授权被
  追溯更正、迟到记录补入后，历史记录在下次核对时自动按最新状态改判；原始尝试记录永不修改。
- **同版本保证**：一次核对在单个读事务（WAL 快照）内完成，窗口内每条记录的记录内容、
  判定依据与结论必然来自同一个 `data_version`；写入失败回滚后，旧版本数据照常可查。
- 原始录入可经 `GET /access-attempts`（支持访问日区间、请求号过滤）查看。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/packages` | 登记资源包（名称 + 授权起止日） |
| GET | `/packages` / `/packages/{id}` | 查看包（含覆盖行数） |
| PUT | `/packages/{id}/license` | 修改授权起止日（事务提交） |
| PUT | `/packages/{id}/coverage` | 全量替换该包覆盖快照（原子） |
| GET | `/packages/{id}/coverage` | 查看该包当前快照 |
| POST | `/packages/{id}/deactivate` / `activate` | 停用 / 重新启用 |
| GET | `/access?issn=...&date=...` | 查询可访出版日期段及来源包 |
| POST | `/access-attempts` | 按请求号录入一次访问尝试（同号同内容幂等、异内容 409） |
| GET | `/access-attempts` | 查看原始访问记录（可按访问日区间 / 请求号过滤） |
| GET | `/verifications?start_date=...&end_date=...` | 按访问日区间逐条核对实际结果与授权 |

ISSN 接受 `0028-0836` / `00280836` / 末位 `X`（大小写均可），库内统一存 `XXXX-XXXX`
大写形式；只校验格式，不校验校验位（出版商表格常有脏数据）。

### 示例

```bash
# 1. 登记两个包
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"Elsevier-ScienceDirect","license_start":"2024-01-01","license_end":"2026-12-31"}'
curl -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d '{"name":"JSTOR-Archive","license_start":"2024-01-01","license_end":"2026-12-31"}'

# 2. 上传覆盖快照（同一 ISSN，禁访月数不同）
curl -X PUT localhost:8000/packages/1/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2010-01-01","end_date":"2026-12-31","embargo_months":0}]}'
curl -X PUT localhost:8000/packages/2/coverage -H 'Content-Type: application/json' \
  -d '{"entries":[{"issn":"0028-0836","start_date":"2015-01-01","end_date":"2026-12-31","embargo_months":12}]}'

# 3. 查询
curl 'localhost:8000/access?issn=0028-0836&date=2026-11-18'
```

响应（注意禁访墙随查询日移动、来源变化处拆段）：

```json
{
  "issn": "0028-0836",
  "date": "2026-11-18",
  "data_version": 4,
  "segments": [
    {"start": "2010-01-01", "end": "2014-12-31", "sources": ["Elsevier-ScienceDirect"]},
    {"start": "2015-01-01", "end": "2025-11-18", "sources": ["Elsevier-ScienceDirect", "JSTOR-Archive"]},
    {"start": "2025-11-19", "end": "2026-11-18", "sources": ["Elsevier-ScienceDirect"]}
  ]
}
```

### 访问核对示例

```bash
# 录入一次被拒绝的访问（读者尝试包 1，文章出版于 2020-03-01）
curl -X POST localhost:8000/access-attempts -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-1","access_date":"2026-11-10","issn":"0028-0836",
       "article_pub_date":"2020-03-01","package_id":1,"outcome":"refused"}'

# 同号同内容重传 -> 201 且 duplicate=true；改成 success 再传 -> 409 + differing_fields
# 按访问日区间核对
curl 'localhost:8000/verifications?start_date=2026-11-01&end_date=2026-11-30'
```

核对响应（节选；`basis` 给出授权窗口与逐行禁访截止日作为判定依据）：

```json
{
  "data_version": 7,
  "count": 1,
  "items": [{
    "request_no": "REQ-1",
    "observed_outcome": "refused",
    "expected_access": true,
    "match_result": "wrongful_denial",
    "mismatch_reason_text": "该包对此文章应有权限，但实际被拒绝",
    "alternative_packages": [{"package_id": 2, "name": "JSTOR-Archive", "...": "..."}],
    "library_wide_access_loss": false,
    "basis": {"license_covers_access_date": true, "coverage_rows": ["..."], "...": "..."}
  }]
}
```

## 项目结构

```
app/
  main.py     FastAPI 路由与事务边界（含访问录入与核对路由）
  db.py       SQLite 连接、schema（含 access_attempts 表）、读/写事务、版本计数
  logic.py    纯函数：月份相减、禁访墙、区间拆段、单包权限判定、不符分类、替代包与全馆失权
  schemas.py  Pydantic 请求/响应模型（含 ISSN 规范化）
tests/
  test_logic.py        拆段、禁访墙与核对判定纯函数单测
  test_api.py          原 API 集成测试（重叠覆盖、失败回滚、并发一致性）
  test_verification.py 访问录入/核对集成测试（多包、补报、追溯更正、幂等与并发）
Dockerfile / docker-compose.yml / requirements.txt
```

## 验证情况（如实说明）

**已实现并验证**（`pytest` 45 项全过：原 16 项 + 新增 29 项；uvicorn 实跑 curl 全流程冒烟）：

原有能力：

- 按包上传全量覆盖快照、登记/修改授权起止日；
- 只采用查询当日有授权且未停用的包（授权前/后查询返回空）；
- 禁访边界随查询日移动（含月末截断、整段被禁访为空）；
- 多包重叠覆盖按来源集合变化拆段，不同禁访月数也正确拆段；
- 停用包后其他包的覆盖依据保留，重新启用后恢复；
- 非法快照/授权修改 422 整体拒绝，`data_version` 不变、旧数据照常服务；
- 并发读写期间每个响应的 `data_version` 与其内容严格对应，未观察到新旧混用。

访问核对新增能力：

- 按请求号录入访问日、ISSN、文章出版日、资源包与成功/拒绝；同号同内容重传只记一次
  （`duplicate=true`、不增版本），同号异内容 409 拒收并列出差异字段，原记录不受影响；
- 按访问日闭区间逐条核对，输出尝试包的应有权限、七种无权限原因（机器码 + 中文）、
  实际结果、四类核对结论与中文不符原因，以及完整判定依据（授权窗口、覆盖行、逐行禁访
  截止日、命中行）；
- 多包场景：其他包可服务时列入 `alternative_packages`，单包拒绝不判为全馆失权；
  被拒且无替代且尝试包本应有权才标 `library_wide_access_loss=true`；本就无权限的拒绝
  （禁访、未授权、未收录）不算失权；
- 补报与更正：先于覆盖数据录入的迟到记录、授权/覆盖追溯更正后，历史记录在下次核对时
  按当前已提交状态自动改判（测试覆盖授权更正、覆盖窗口更正、迟到补报三类）；
- 同版本：核对在单个 WAL 读事务内完成，窗口内记录、依据、结论同属一个 `data_version`；
  异内容拒收、非法快照等写入失败回滚后，旧数据与旧版本照常可查（有并发读写测试）；
- `/docs`（Swagger UI）可直接录入访问尝试并发起核对，`/openapi.json` 可用。

**未验证 / 限制**：

- 本环境无 Docker 守护进程（`docker` 命令不存在），`Dockerfile` / `docker-compose.yml`
  经静态核对：本次未引入新依赖（仍为 `fastapi==0.115.6` / `uvicorn==0.32.1`）、新代码均在
  `app/` 内、新表用 `CREATE TABLE IF NOT EXISTS` 自动建表（兼容已有命名卷里的旧库），
  但未实际 `docker compose up --build` 构建运行；
- SQLite 单写者，适合馆内单实例部署；多实例水平扩展需换外部数据库；
- ISSN 只校验格式不校验校验位；接口未做鉴权（内网部署假设）；
- 核对结论实时重算、不持久化，极大窗口（数万条以上）的查询耗时未做压测。

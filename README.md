# Library Journal Access API

高校图书馆期刊可访问性判定服务。把出版商/数据库商提供的期刊覆盖表按**资源包**整理进库，
检索系统用「查询日 + ISSN」查出**当日实际可打开的出版日期段**及其来源包。

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
- **一致性**：每次成功写入（建包、改授权、换快照、启停用）在同一事务里把全局
  `data_version` 加一；写入失败整体回滚，读者继续看到旧版本。每次查询在**单个读
  事务**内完成（WAL 快照读），响应里的 `data_version` 就是本次答案实际依据的提交
  版本——并发写不会把新旧来源混进同一个答案。

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

## 项目结构

```
app/
  main.py     FastAPI 路由与事务边界
  db.py       SQLite 连接、schema、读/写事务、版本计数
  logic.py    纯函数：月份相减、禁访墙、区间拆段合并
  schemas.py  Pydantic 请求/响应模型（含 ISSN 规范化）
tests/
  test_logic.py  拆段与禁访墙单测
  test_api.py    API 集成测试（重叠覆盖、失败回滚、并发一致性）
Dockerfile / docker-compose.yml / requirements.txt
```

## 验证情况（如实说明）

**已实现并验证**（`pytest` 16 项全过 + uvicorn 实跑 curl 冒烟）：

- 按包上传全量覆盖快照、登记/修改授权起止日；
- 只采用查询当日有授权且未停用的包（授权前/后查询返回空）；
- 禁访边界随查询日移动（含月末截断、整段被禁访为空）；
- 多包重叠覆盖按来源集合变化拆段，不同禁访月数也正确拆段；
- 停用包后其他包的覆盖依据保留，重新启用后恢复；
- 非法快照（日期倒置、ISSN 格式错）整体 422 拒绝，`data_version` 不变、旧数据照常服务；
- 非法授权修改（起晚于止）422 拒绝，旧授权继续生效；
- 并发读写在替换快照期间，每个响应的 `data_version` 与其段内容严格对应，未观察到新旧混用；
- `/docs`（Swagger UI）与 `/openapi.json` 可用。

**未验证 / 限制**：

- 本环境无 Docker，`Dockerfile` / `docker-compose.yml` 经静态核对（基础镜像、依赖版本与本地
  实测一致的 `fastapi==0.115.6` / `uvicorn==0.32.1`、启动命令与本地实跑相同），但未实际构建运行；
- SQLite 单写者，适合馆内单实例部署；多实例水平扩展需换外部数据库；
- ISSN 只校验格式不校验校验位；覆盖快照上传未做鉴权（内网部署假设）。

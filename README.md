# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 维度映射解析链

维度映射、多维下钻、聚合配置共用同一条解析链（`backend/app/services/dimensionmap_chain.py`），
不再各自实现“取第一条规则”的逻辑：

```text
原始维度字段记录 ──┐
                   ├─► 规则归一化/优先级解析 ─► 标准维度映射数据集 ─┬─► 多维下钻分层结果
映射规则（含优先级）┘        （每值唯一命中）        （带版本与命中规则） └─► 聚合目标粒度结果
                                            │
                                            └─► 映射版本（规则快照 + 回算统计，可重放）
```

规则优先级（`映射优先级`）与值转换逻辑（`值转换逻辑`）、默认取值（`默认取值`）、
最近更新时间一律完整落库，不再只保存三个必填字段。

### 冲突判定

同一原始维度值可能命中多条规则，解析链只选出一条：

1. 具体映射（精确 / 前缀 / 正则 / 值转换）优先于默认映射 —— **默认映射永远不会覆盖更具体的映射**，
   即使默认规则的数字优先级更大；
2. 同为具体映射时，按 `映射优先级` 数值从高到低；
3. 优先级相同再按匹配精度（精确 > 正则 > 前缀 > 值转换）、最近更新时间倒序、规则编号确定唯一结果。

每次解析都会在数据集行的 `候选规则` 里保留全部命中候选与落选原因，若同级候选给出的标准维度值
不一致会标记 `同级冲突`，需要业务按代价权衡后调整优先级（接口返回 `冲突处理策略` 说明）。

### 版本与事务

- 每次规则登记 / 修改 / 生效 / 废弃都会生成新的映射版本，`dimensionmap_versions` 里同一时刻
  只有一个 `状态=当前` 的版本；并发启停由仓库事务锁串行化。
- 写规则、生成版本、重建标准维度数据集、回算下钻与聚合在**同一个事务**里完成；
  任何一步写入或提交失败整体回滚，不会暴露残缺版本或与来源记录不一致的数据集。
- 旧映射变更会触发受影响的下载分层结果与聚合结果回算，两条下游结果都只读取当前版本数据集。
- `GET /api/dimensionmap/versions/{n}/replay` 可按历史版本规则快照重放解析，核对历史映射可追溯。

### 相关接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/dimensionmap/summary` | 规则数、当前版本、数据集/默认命中/同级冲突核对 |
| `GET /api/dimensionmap/dataset` | 标准维度映射数据集，含命中规则、规则版本、映射版本、来源记录 |
| `POST /api/dimensionmap/resolve` | 给定原始维度名与值，返回唯一命中规则与全部候选的落选原因 |
| `GET /api/dimensionmap/versions` | 映射版本列表（规则快照 + 回算统计） |
| `PUT /api/dimensionmap/{id}` | 修改规则并完整保存转换逻辑/默认值/优先级/更新时间 |
| `GET /api/drilldown/results` | 分层结果，原始维度名与标准维度名分列，带映射版本 |
| `POST /api/drilldown/{id}/refresh` | 按当前映射版本刷新单条路径分层结果 |
| `GET /api/aggregate/results` | 目标粒度结果，目标维度键唯一，带命中规则集合 |
| `GET /api/aggregate/{id}/detail` | 聚合详情 + 记录数重复/丢失核对 |
| `GET /api/aggregate/mapping-link` | 聚合页与映射版本联动核对 |

### 测试

```bash
cd backend
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests -q
```

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

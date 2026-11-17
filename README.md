# cc-retain-lab

CC_Codex 二期（26.11）**数据工程**产品线：**留存裁撤台**（FastAPI + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**分区留存策略版本 → 到期扫描裁决 → 归档或销毁执行 → 策略与实物分区对账**

与现有六线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取）。  
也不碰外仓常见指纹（Feed 归并 / 作业编排 / CDC 游标 / 质检签发 / 贴源-明细-汇总）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数仓与数据湖（分区留存 / 裁撤闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | 原生 JS 单页（全中文），青灰底 + 铜强调，顶栏导航（非绿侧栏克隆） |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 现行留存策略（主题域 / 保留天 / 到期动作）  
2. 实物分区列表（在线 / 归档 / 销毁）  
3. 按基准日到期扫描 → 生成待执行裁决  
4. 单条裁决执行（归档写地址 / 销毁清零）  
5. 策略与实物对账报告  

预留切口（未做，供后续功能题）：策略换版生效窗、重裁作废、批量失败闭互斥、跨域依赖冻结。

## 归档回灌（已实现）

把已归档分区一次性切回在线单态，链路固定一条：

**同库事务内快照 → 对照临时副本核对 → 升主轨**

- **只走一条路径**：接口只接受 `mode=对照临时副本`（默认），传「直接升主轨」等其它值直接 409，
  避免两条路径让失败语义与对账口径分叉。
- **快照语义**：回灌前把 `storage_state / archive_uri / row_count / 影子标记` 记进
  `reinject_runs`（`snapshot_*` 字段与 `precheck` JSON），失败也落一条 `ok=0` 的审计。
- **原子性**：快照、对照副本、升主轨、清理 `meta` 里的 `shadow:<id>`、作废原归档裁决
  全在同一个 `BEGIN IMMEDIATE` 事务里；任一步失败整笔退回，业务表回到回灌前，
  不存在「像在线又像已归档」的中间态。
- **双影专拦**：在线却仍带归档地址、或残留归档影子标记、或已归档却缺归档地址，
  判定为双影。扫描不为双影开新裁决，执行拒绝双影分区，对账把双影单列 `dual_count`，
  不混进在线数也不当普通错位；回灌默认拒绝双影，需显式 `allow_dual_repair=true` 才归一。
- **双写拦截**：回灌先抢 `reinject_locks` 分区锁（带过期时间），主事务再拿库级写锁。
  两名补跑同时回灌同一分区时，后到者拿到锁后重读分区态，已在线单态即 409，
  只落一笔成功回灌。

页面入口：实物分区 →「详情」看映像判定与回灌链路；到期扫描顶部有双影专拦卡片；
分区对账有实时对账队列（错位 / 双影 / 归档痕迹未收尾）与含 `dual_count` 的报告。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/partitions` | 含 `image` / `display_state` / `shadow_state`，可按 `storage_state=双影` 过滤 |
| GET | `/api/partitions/{id}` | 分区详情：映像判定、最近裁决、回灌快照链路 |
| GET | `/api/partitions/images/duals` | 双影分区清单 |
| POST | `/api/partitions/{id}/reinject` | body `{mode, allow_dual_repair}`；失败返回 409 + 稳定 `code` |
| GET | `/api/reinject-runs` | 回灌审计流水 |
| GET | `/api/reconcile-queue` | 对账队列即时视图（错位 / 双影 / 归档痕迹） |

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-retain-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6500/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6500**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`

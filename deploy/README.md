# SilvaEngine Gateway — Banyan 生产数据面部署（P0）

> P0 最小交付：网关容器 + postgres + neo4j + redis，se-configdata 读真实云端
> DynamoDB。nginx 统一入口（TLS / 前端反代）归 P1；任务/限流共享后端归 P2。
> 容器级操作（compose up、镜像构建）由使用者执行——本仓交付物为配置与脚本。

## 1. 架构

```
浏览器/前端
    │  /beta/core/banyan/{function}（API-GW 路径契约）
    ▼
gateway (FastAPI, uvicorn :8000)
    ├─ BanyanPathNormalizer   剥 /{stage}/{area} 前缀 → 双契约并存
    ├─ BanyanAuthorizerBridge perm_engine.PermAuthorizer 真实鉴权（401/403）
    ├─ FlexJWTMiddleware      非 Banyan 域（本网关自有 /auth 等）走 local/Cognito
    └─ 路由分发 → 12 引擎进程内 dispatch_graphql
         │
         ├─ postgres:16  关系型/审计/遥测（三池：postgres_main/audit/telemetry）
         ├─ neo4j:5      图数据 + 向量（唯一存储，铁律二）
         ├─ redis:7      瞬态令牌/验证码
         └─ DynamoDB（云端 se-configdata，启动时只读）
                └─ 跨引擎互调：httpx 池回环（BANYAN_LOOPBACK_BASE_URL）
                   引擎服务令牌经鉴权桥验证——与前端调用同一路径，无内部旁路
```

## 2. 前置条件与 .env

镜像：`GATEWAY_IMAGE` 必须是含 12 引擎 + 框架的扩展镜像（P1 由
`docker-silvaengine-gateway` 仓构建；PYTHONPATH 布局
`/app/vendor:/app:/app/modules/<12 引擎仓根>`，与 api-runtime 已验证形态一致）。

AWS：默认读真实云端 `se-configdata`（`beta_core_banyan` 记录，**只读**），
凭证需有该表读权限；完全离线用文末 DDB Local 变体。

在 `deploy/` 下创建 `.env`（不要提交）：

```dotenv
# --- 网关镜像 ---
GATEWAY_IMAGE=silvaengine-gateway-banyan:latest

# --- DynamoDB（云端 se-configdata，只读）---
region_name=us-west-2
aws_access_key_id=<AWS_KEY_WITH_DDB_READ>
aws_secret_access_key=<AWS_SECRET>

# --- 数据面基础设施（与 se-configdata 记录值保持一致）---
POSTGRES_USER=banyan
POSTGRES_PASSWORD=<PG_PASSWORD>
POSTGRES_DB=banyan
NEO4J_AUTH=neo4j/<NEO4J_PASSWORD>
REDIS_PASSWORD=<REDIS_PASSWORD>

# --- 可选覆盖（默认见 docker-compose.yml environment 段）---
# ENDPOINT_ID=banyan
# ADAPTER_STAGE=beta
# ADAPTER_AREA=core
# BANYAN_LOOPBACK_BASE_URL=http://127.0.0.1:8000/beta/core/banyan
# SE_CONFIGDATA_TABLE=se-configdata
# SE_CONFIGDATA_ENDPOINT_URL=http://ddb-local:8000   # DDB Local 变体
```

## 3. 启动

```bash
cd deploy
docker compose up -d
docker compose ps          # 等 gateway healthy
curl http://127.0.0.1:8000/health
```

启动日志应出现：`se-configdata setting loaded: setting_id=beta_core_banyan`
与 `Pool bootstrap: framework pools created`——两者缺失说明
配置叠加或池引导未生效，先排查再继续。

## 4. se-configdata 记录

`env/se-configdata.example.json` 是**占位模板**（无真实密钥），记录形态：
`(setting_id, variable) → value` 每变量一行；`plugins` 为列表，
池配置位于 `plugins[0]["config"][<pool>]["settings"]`。

权威变量集合是 staging 真实记录——用
`banyan/scripts/probe_seconfig.py`（AWS profile `ideabosque-dev`）核对后再
填充/裁剪模板；`httpx_*` 池的 `base_url` 会被 `BANYAN_LOOPBACK_BASE_URL`
启动时统一改写为回环地址，无需在记录里预改。

灌表（仅 DDB Local 变体需要）：

```bash
python scripts/seed_seconfig.py --file env/se-configdata.local.json \
    --endpoint-url http://127.0.0.1:8001 --apply
```

默认 dry-run，`--apply` 才写。含真实密钥的文件严禁提交任何仓库。

## 5. P0 验收步骤（使用者执行）

1. `docker compose ps` 全部 healthy；`curl /health` 200。
2. 前端契约：`POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql`
   （`part_id` 头 + Banyan JWT）登录 mutation 返回 token。
3. 鉴权负向：错误 token → 401；有效 token 但无角色权限 → 403。
4. 回环链路：触发一次跨引擎调用（如 invokeAgent → httpx_agent 池），网关
   日志可见同一请求二次进入鉴权桥（服务令牌）且业务结果正常。
5. x-api-key 护栏：带 `x-api-key` 头调用一次，debug 日志出现
   `x-api-key present: True`（P0 仅记录存在性，不校验）。
6. 12 引擎 pytest（banyan 仓，引擎侧零改动回归）。

## 6. P0 已知边界

- **x-api-key**：透传不校验（Lambda 链由 API-GW usage plan 强制），存在性
  日志护栏已加；校验归 P1 评审。
- **WebSocket**：鉴权桥为 BaseHTTPMiddleware，WS 升级请求不经其拦截
  （Starlette 行为）；WS 路由的鉴权策略归 P1 评审。
- **nginx/TLS/前端反代**：P1；**任务/限流共享后端**（多 worker）：P2。
- 单进程部署（uvicorn 单 worker，ConnectionManager MVP 约束）；多 worker
  前置依赖 P2 的任务/限流后端改造。
- staging se-configdata 的真实记录值（尤其 `httpx_llm` 等池形态）以
  probe 实测为准；发现与模板不一致时以记录为准修模板。

## 7. bind-mount 变体（免镜像构建的容器验收）

P0 容器验收可在网关镜像构建前先行：`docker-compose.bindmount.yml` Overlay
复用 base 的 gateway 服务定义，仅覆写为纯 `python:3.12-slim` + 运行面全部
bind-mount 注入（源文件修改后容器重启即生效，与 api-runtime 服务器部署
模式一致）。**P1 镜像仍是生产交付物**；两条路径共用同一份
`requirements-bindmount.txt` 与同一 PYTHONPATH 布局，验收结论可互相迁移。

挂载点全部平铺在 `/app`（容器 rootfs 目录）下、互不嵌套——嵌套 bind-mount
会在宿主机源目录内创建挂载点，污染源仓的 git 状态。

### 7.1 .env 增补

在 §2 的 `.env` 基础上追加（compose 路径变量全部 `:?` 守卫，缺一即报错）：

```dotenv
# --- bind-mount 变体（Overlay 模式）---
# 必填占位：base 文件里的 ${GATEWAY_IMAGE:?} 守卫在 Overlay 模式下仍会
# 插值求值（image 虽被覆写，变量仍必须存在）
GATEWAY_IMAGE=bindmount-placeholder

# --- 运行面源路径（本机示例，按实际工作区调整）---
GATEWAY_PACKAGE_DIR=/Users/<you>/Workspace/ideabosque/silvaengine_gateway/silvaengine_gateway
BANYAN_MODULES_DIR=/Users/<you>/Workspace/ideabosque/banyan/modules
SILVAENGINE_BASE_DIR=/Users/<you>/Workspace/ideabosque/silvaengine_base
SILVAENGINE_UTILITY_DIR=/Users/<you>/Workspace/ideabosque/silvaengine_utility
SILVAENGINE_CONNECTIONS_DIR=/Users/<you>/Workspace/ideabosque/silvaengine_connections
# 框架仓给 repo 根或包目录均可 —— 启动探测脚本自动适配两种布局

# vendor 快照（constants / definitions / dynamodb_base 三个包，api-runtime
# 已验证形态）
VENDOR_DIR=/Users/<you>/Workspace/docker/api-runtime/vendor

# --- 可选 ---
# PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/   # 默认即此
# SILVAENGINE_CACHE_TTL=300
```

### 7.2 启动

```bash
cd deploy
docker compose -f docker-compose.yml -f docker-compose.bindmount.yml up -d
docker compose -f docker-compose.yml -f docker-compose.bindmount.yml ps
docker compose -f docker-compose.yml -f docker-compose.bindmount.yml logs -f gateway
```

需 Docker Compose v2（environment / healthcheck 与 base 按映射逐键合并，
`SETTING_SOURCE` / `ENDPOINT_ID` / AWS 凭据等 env 注入全部继承）。

首次启动 pip 全量安装 `requirements-bindmount.txt`（约 1–3 分钟，取决于
镜像源），healthcheck 的 start_period 已放宽到 300s；后续重启命中 pip-cache
命名卷秒级完成。日志出现
`[entrypoint] silvaengine_base: repo-root layout detected, PYTHONPATH += /app/silvaengine_base`
说明双布局探测按预期生效。

### 7.3 验收与切换

- **验收**：§5 六步验收清单不变——bind-mount 或镜像任一变体通过即认可。
- **切回生产镜像模式**：`docker compose down`（不加 `-v`，数据卷保留）后，
  单文件 `docker compose up -d` 启动即可，bindmount Overlay 不参与。
- **源码修改热更**：网关 / 引擎 / 框架源文件在宿主机修改后，
  `docker compose restart gateway` 即生效（bind-mount 直读宿主机路径）。

## 8. 一键部署脚本 deploy.sh（模式 A 全自动）

`deploy.sh` 把 §2/§4/§7 的手工步骤（`.env` 生成、种子 JSON 渲染、源码树校验、
端口预检、DDB Local 启动与初始化、五服务 up、健康与关键日志验证）封装为
幂等一键流程。目标服务器只需预装 Docker（含 Compose v2 插件），无需
Python / jq / awscli；每阶段有状态输出，失败给出具体原因与排查建议后安全
退出，修复后直接重跑，整体回滚 `bash deploy.sh down`。

### 8.1 服务器准备（一次性）

源码树按本仓相对布局同步（`deploy/` 的父目录须是网关仓根）：

```bash
# 三框架仓与 banyan/modules 同在 ideabosque/ 下随本条同步
rsync -a --exclude .git --exclude .venv --exclude node_modules \
    --exclude __pycache__ <本机>/ideabosque/ server:/srv/ideabosque/
# vendor 快照（探测顺序：工作区根下 docker/api-runtime/vendor）
rsync -a --exclude .git <本机>/Workspace/docker/api-runtime/vendor \
    server:/srv/Workspace/docker/api-runtime/
```

### 8.2 命令

| 命令 | 作用 |
| --- | --- |
| `bash deploy.sh` | 部署/更新（幂等，重复执行安全） |
| `bash deploy.sh status` | 容器状态 + 健康检查 + 关键日志核查 |
| `bash deploy.sh down` | 停止并清理（数据卷保留） |
| `bash deploy.sh down -v` | 停止并连数据卷一起删除 |
| `bash deploy.sh --restart` | 部署后重启 gateway（改源码/种子 JSON 后） |
| `bash deploy.sh --force-env` | 重新生成 .env 与种子 JSON（密码会变） |
| `bash deploy.sh --dry-run` | 只跑环境检测/配置生成/校验/端口预检，不起容器 |
| `bash deploy.sh --self-test` | 内置纯逻辑自检（不碰 docker） |

环境变量（首次生成 `.env` 前生效）：`TENANT_PART_ID`（默认 nestaging）、
`GATEWAY_PACKAGE_DIR` / `BANYAN_MODULES_DIR` / `SILVAENGINE_BASE_DIR` /
`SILVAENGINE_UTILITY_DIR` / `SILVAENGINE_CONNECTIONS_DIR` / `VENDOR_DIR`
（源路径覆盖，优先于自动探测）；`GATEWAY_WAIT_TIMEOUT`（健康等待上限，
默认 600s）随时可设。

### 8.3 设计要点

- 密钥自动生成：PG / Neo4j / Redis 密码各 16 位随机，JWT_SECRET 与 x-api-key
  各 32 位随机（后两者只写入种子 JSON）；`.env` 与种子 JSON 均 600 权限，
  `deploy/.gitignore` 已排除，勿提交。
- 四态状态机：`.env`/JSON 都在→复用；都无→生成；仅 `.env`→补渲染；
  仅 JSON（孤儿态）→报错（防密码不一致）。配置值强制纯字母数字（sed
  渲染防注入），改密码后删 JSON 重跑。
- 幂等与自愈：重复运行 `.env`/JSON 内容不变；每次重灌 DDB Local 种子，
  `-inMemory` 重建后自动恢复。`--force-env` 为显式重新生成入口。
- 与 §7 手动路径同一 compose project（目录名 deploy），容器/卷/网络互通。
- base compose 若已手动取消注释 ddb-local，请注释回去（脚本会警告端口冲突）。
- 公网防火墙只放行 8000；8001/5432/6379/7474/7687 仅供本机调试。
- 12 引擎 pytest 仍在开发机 banyan 仓执行（服务器无 Python）。
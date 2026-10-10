#!/usr/bin/env bash
# =============================================================================
# SilvaEngine Gateway — Banyan 一键部署脚本（模式 A：DynamoDB Local 全离线）
# =============================================================================
# 目标服务器只需预装 Docker（含 Compose v2 插件），无需 Python / jq / awscli。
# 本脚本把 deploy/README.md 的手工路径（§2/§4/§7）封装为幂等的一键流程：
#
#   阶段 1  环境检测        docker / 守护进程可达 / compose v2
#   阶段 2  配置与源码校验  .env 与种子 JSON 生成/复用（四态状态机）+
#                          网关包 / 12 引擎 / vendor / 三框架源码树校验
#   阶段 3  端口预检        8000/8001/5432/6379/7474/7687（区分占用者）
#   阶段 4  启动 ddb-local  DynamoDB Local（-inMemory -sharedDb）
#   阶段 5  ddb-init        建表 → 灌种子（覆盖写，幂等）→ 行数核验
#   阶段 6  启动五服务      ddb-local postgres neo4j redis gateway
#   阶段 7  健康与日志验证  4 容器 healthy + /health 探活 + 两条关键启动日志
#   阶段 8  摘要            地址 / 租户 / 验证步骤 / 常用操作
#
# 用法：
#   bash deploy.sh                # 部署/更新（幂等，可重复执行）
#   bash deploy.sh status         # 状态与健康检查
#   bash deploy.sh down [-v]      # 停止清理（-v 连数据卷一起删除）
#   bash deploy.sh --restart      # 部署后重启 gateway（改源码/JSON 后）
#   bash deploy.sh --force-env    # 重新生成 .env 与种子 JSON（密码会变）
#   bash deploy.sh --dry-run      # 只跑阶段 1-3（生成 .env/JSON，不起容器）
#   bash deploy.sh --self-test    # 内置纯逻辑自检（不碰 docker）
#
# 首次生成 .env 前可用的环境变量覆盖：
#   TENANT_PART_ID（默认 nestaging）、GATEWAY_PACKAGE_DIR、BANYAN_MODULES_DIR、
#   SILVAENGINE_BASE_DIR、SILVAENGINE_UTILITY_DIR、SILVAENGINE_CONNECTIONS_DIR、
#   VENDOR_DIR；GATEWAY_WAIT_TIMEOUT（健康等待上限秒数，默认 600）随时可设。
#
# 模式边界：本脚本只做模式 A（DDB Local 全离线）；读真实云端 DynamoDB 的
# 模式 B 走 deploy/README.md §2/§4 手动路径。
#
# 幂等性：重复运行复用 .env 与种子 JSON（内容不变），每次重灌 DDB Local
# 种子（-inMemory 重建自愈）。任何阶段失败安全退出并给出原因；修复后直接
# 重跑即可，整体回滚：bash deploy.sh down。
#
# 兼容性：bash 3.2+（macOS 自带版本可用），无 Python / 关联数组依赖。
# =============================================================================
set -Eeuo pipefail
umask 077
export LC_ALL=C

DEPLOY_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DEPLOY_DIR"
REPO_ROOT=$(cd "$DEPLOY_DIR/.." && pwd)
WS_DIR=$(cd "$REPO_ROOT/.." && pwd)
UP_DIR=$(cd "$WS_DIR/.." && pwd)

ENV_FILE="$DEPLOY_DIR/.env"
SEED_DIR="$DEPLOY_DIR/env"
EXAMPLE_JSON="$SEED_DIR/se-configdata.example.json"
SEED_JSON="$SEED_DIR/se-configdata.local.json"

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.bindmount.yml -f docker-compose.ddb.yml)
UP_SERVICES=(ddb-local postgres neo4j redis gateway)
HOST_PORTS=(8000 8001 5432 6379 7474 7687)
ENGINE_REPOS=(agent_engine capability_engine knowledge_engine llm_engine \
  memory_engine merchant_engine monitor_engine orchestration_engine \
  perm_engine prompt_engine setting_engine user_engine)
VENDOR_PKGS=(silvaengine_constants silvaengine_definitions silvaengine_dynamodb_base)
FRAMEWORK_PKGS=(silvaengine_base silvaengine_utility silvaengine_connections)
REQUIRED_LOGS=("se-configdata setting loaded: setting_id=" \
  "Pool bootstrap: framework pools created")
OPTIONAL_LOGS=("repo-root layout detected" "Rewrote [0-9]+ httpx pool base_url")

REQUIRED_ENV_KEYS=(GATEWAY_IMAGE TENANT_PART_ID POSTGRES_USER POSTGRES_PASSWORD \
  POSTGRES_DB NEO4J_AUTH REDIS_PASSWORD SE_CONFIGDATA_ENDPOINT_URL \
  GATEWAY_PACKAGE_DIR BANYAN_MODULES_DIR SILVAENGINE_BASE_DIR \
  SILVAENGINE_UTILITY_DIR SILVAENGINE_CONNECTIONS_DIR VENDOR_DIR)

WAIT_TIMEOUT="${GATEWAY_WAIT_TIMEOUT:-600}"
FORCE_ENV=0
DRY_RUN=0
RESTART=0
VOLUMES=0
MODE="up"

CURRENT_STAGE="0"
STAGE_DESC="初始化"
STAGE_HINT=""

log() { printf '[deploy.sh] %s\n' "$*"; }

set_stage() {
  CURRENT_STAGE="$1"
  STAGE_DESC="$2"
  STAGE_HINT="$3"
  log "── 阶段 $1/8：$2"
}

die() {
  printf '\n[deploy.sh] X 阶段 %s（%s）失败：%s\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$*" >&2
  if [ -n "$STAGE_HINT" ]; then
    printf '[deploy.sh] 排查建议：%s\n' "$STAGE_HINT" >&2
  fi
  printf '[deploy.sh] 本脚本幂等——修复后可直接重跑；整体回滚：bash deploy.sh down（-v 连数据卷）\n' >&2
  exit 1
}

on_error() {
  local code=$?
  printf '\n[deploy.sh] X 阶段 %s（%s）内部异常（退出码 %s，行 %s）。\n' \
    "$CURRENT_STAGE" "$STAGE_DESC" "$code" "${BASH_LINENO[0]:-?}" >&2
  if [ -n "$STAGE_HINT" ]; then
    printf '[deploy.sh] 排查建议：%s\n' "$STAGE_HINT" >&2
  fi
  printf '[deploy.sh] 本脚本幂等——修复后可直接重跑；整体回滚：bash deploy.sh down（-v 连数据卷）\n' >&2
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<'USAGE'
SilvaEngine Gateway — Banyan 一键部署（模式 A：DynamoDB Local 全离线）

用法: bash deploy.sh [命令] [选项]

命令:
  (默认)      部署/更新（幂等，可重复执行）
  status      容器状态 + 健康检查 + 关键日志核查
  down        停止并清理（数据卷保留）；down -v 连数据卷一起删除

选项:
  --restart   部署后重启 gateway（修改源码/种子 JSON 后使用）
  --force-env 删除并重新生成 .env 与种子 JSON（密码会变更）
  --dry-run   只跑阶段 1-3（环境检测/配置生成/校验/端口预检），不起容器
  --self-test 内置纯逻辑自检（不碰 docker）
  -h, --help  显示本帮助

环境变量（首次生成 .env 前生效，路径类覆盖探测结果）:
  TENANT_PART_ID            租户 part_id（默认 nestaging）
  GATEWAY_PACKAGE_DIR       网关包目录（仓根下的 silvaengine_gateway/）
  BANYAN_MODULES_DIR        Banyan 12 引擎目录（banyan/modules）
  SILVAENGINE_BASE_DIR      框架仓 silvaengine_base
  SILVAENGINE_UTILITY_DIR   框架仓 silvaengine_utility
  SILVAENGINE_CONNECTIONS_DIR 框架仓 silvaengine_connections
  VENDOR_DIR                vendor 快照目录（constants/definitions/dynamodb_base）
  GATEWAY_WAIT_TIMEOUT      健康等待上限秒数（默认 600，首次启动 pip 安装较慢）
USAGE
}

# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------

dc() { docker compose "${COMPOSE_FILES[@]}" "$@"; }

# 随机字母数字串（urandom | tr | head；管道尾 || true 防 SIGPIPE × pipefail）
rand_alnum() {
  local n="$1" out
  out=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$n" || true)
  printf '%s' "$out"
}

# sed 替换串转义（\ / & ；本脚本用 | 作分隔符，仍按通用规则转义）
sed_escape() {
  printf '%s' "$1" | sed 's/[\\/&]/\\&/g' || true
}

# 依序返回第一个存在的候选路径；全不存在则返回非零
first_existing() {
  local c
  for c in "$@"; do
    if [ -e "$c" ]; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

# 从 .env 读键值（剥离成对引号；缺失返回空串）
env_get() {
  local key="$1" line val
  line=$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null || true)
  val="${line#*=}"
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}

# 端口被占用返回 0；空闲返回非零。优先 timeout 限时（无 timeout 的 macOS 直连，
# 127.0.0.1 上未监听端口会立即收到 RST，不会悬挂）
port_busy() {
  local port="$1"
  if command -v timeout >/dev/null 2>&1; then
    if timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" >/dev/null 2>&1; then
      return 0
    fi
  else
    if bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

# ---------------------------------------------------------------------------
# 阶段 2：配置生成与校验
# ---------------------------------------------------------------------------

# 把 .env 全量读入 shell 变量（渲染种子 JSON 与后续阶段使用）
load_env_values() {
  TENANT_PART_ID=$(env_get TENANT_PART_ID)
  PG_USER=$(env_get POSTGRES_USER)
  PG_PASSWORD=$(env_get POSTGRES_PASSWORD)
  PG_DB=$(env_get POSTGRES_DB)
  NEO4J_AUTH=$(env_get NEO4J_AUTH)
  NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}"
  REDIS_PASSWORD=$(env_get REDIS_PASSWORD)
  GATEWAY_PACKAGE_DIR=$(env_get GATEWAY_PACKAGE_DIR)
  BANYAN_MODULES_DIR=$(env_get BANYAN_MODULES_DIR)
  SILVAENGINE_BASE_DIR=$(env_get SILVAENGINE_BASE_DIR)
  SILVAENGINE_UTILITY_DIR=$(env_get SILVAENGINE_UTILITY_DIR)
  SILVAENGINE_CONNECTIONS_DIR=$(env_get SILVAENGINE_CONNECTIONS_DIR)
  VENDOR_DIR=$(env_get VENDOR_DIR)
}

# 配置值必须纯字母数字：本脚本用 sed 渲染种子 JSON，放宽字符集会引入
# 注入/转义问题；含特殊字符的密码请自行改用模式 B 手动灌表
assert_alnum() {
  local name="$1" val="$2"
  if [ -z "$val" ]; then
    die "配置值校验失败：$name 为空（.env）"
  fi
  case "$val" in
    *[!A-Za-z0-9]*)
      die "配置值校验失败：$name 含非字母数字字符。种子 JSON 由 sed 渲染，为避免转义问题，请把 .env 中该值改为纯字母数字，删除 env/se-configdata.local.json 后重跑（或 --force-env 重新生成）"
      ;;
  esac
}

# 首次部署：探测源码路径 + 生成随机密码 → .env
generate_env() {
  local part_id pg_pw neo4j_pw redis_pw tmp_f

  # 路径发现：环境变量覆盖优先，否则按本仓相对布局探测
  # （deploy/.. = 网关仓根；仓根/.. = ideabosque 工作区；再上一级为工作区根）
  if [ -z "${GATEWAY_PACKAGE_DIR:-}" ]; then
    if ! GATEWAY_PACKAGE_DIR=$(first_existing "$REPO_ROOT/silvaengine_gateway"); then
      GATEWAY_PACKAGE_DIR=""
    fi
  fi
  if [ -z "${BANYAN_MODULES_DIR:-}" ]; then
    if ! BANYAN_MODULES_DIR=$(first_existing "$WS_DIR/banyan/modules"); then
      BANYAN_MODULES_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_BASE_DIR:-}" ]; then
    if ! SILVAENGINE_BASE_DIR=$(first_existing "$WS_DIR/silvaengine_base"); then
      SILVAENGINE_BASE_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_UTILITY_DIR:-}" ]; then
    if ! SILVAENGINE_UTILITY_DIR=$(first_existing "$WS_DIR/silvaengine_utility"); then
      SILVAENGINE_UTILITY_DIR=""
    fi
  fi
  if [ -z "${SILVAENGINE_CONNECTIONS_DIR:-}" ]; then
    if ! SILVAENGINE_CONNECTIONS_DIR=$(first_existing "$WS_DIR/silvaengine_connections"); then
      SILVAENGINE_CONNECTIONS_DIR=""
    fi
  fi
  if [ -z "${VENDOR_DIR:-}" ]; then
    if ! VENDOR_DIR=$(first_existing "$UP_DIR/docker/api-runtime/vendor"); then
      VENDOR_DIR=""
    fi
  fi

  part_id="${TENANT_PART_ID:-nestaging}"
  pg_pw=$(rand_alnum 16)
  neo4j_pw=$(rand_alnum 16)
  redis_pw=$(rand_alnum 16)

  tmp_f="$ENV_FILE.tmp.$$"
  cat > "$tmp_f" <<EOF
# SilvaEngine Gateway deploy/.env —— 由 deploy.sh 自动生成，勿提交（含密码）。
# 修改后直接重跑 bash deploy.sh 即可生效；键说明见 deploy/README.md §2/§7.1。

# --- 网关（bind-mount 模式占位值；base 的 \${GATEWAY_IMAGE:?} 守卫要求非空）---
GATEWAY_IMAGE=bindmount-placeholder

# --- 租户 ---
TENANT_PART_ID=${part_id}

# --- 数据面基础设施（密码随机生成，纯字母数字）---
POSTGRES_USER=banyan
POSTGRES_PASSWORD=${pg_pw}
POSTGRES_DB=banyan
NEO4J_AUTH=neo4j/${neo4j_pw}
REDIS_PASSWORD=${redis_pw}

# --- se-configdata：模式 A 读 DDB Local（离线）---
SE_CONFIGDATA_ENDPOINT_URL=http://ddb-local:8000
# boto3 对 DDB Local 仍需签名凭据（假凭据即可，DDB Local 不校验）
region_name=us-west-2
aws_access_key_id=local
aws_secret_access_key=local

# --- bind-mount 源路径（自动探测，可手工修正）---
GATEWAY_PACKAGE_DIR=${GATEWAY_PACKAGE_DIR}
BANYAN_MODULES_DIR=${BANYAN_MODULES_DIR}
SILVAENGINE_BASE_DIR=${SILVAENGINE_BASE_DIR}
SILVAENGINE_UTILITY_DIR=${SILVAENGINE_UTILITY_DIR}
SILVAENGINE_CONNECTIONS_DIR=${SILVAENGINE_CONNECTIONS_DIR}
VENDOR_DIR=${VENDOR_DIR}

# --- 可选覆盖（取消注释生效）---
# PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# SILVAENGINE_CACHE_TTL=300
# BANYAN_LOOPBACK_BASE_URL=http://127.0.0.1:8000/beta/core/banyan
EOF
  mv -f "$tmp_f" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "已生成 .env（随机密码 16 位；JWT/x-api-key 密钥仅在种子 JSON 中）"
}

# 按 .env 渲染种子 JSON：替换 8 个占位符 + 删 _comment 行 + 渲染后核验
render_seed_json() {
  local jwt api_key sed_script tmp_f

  assert_alnum "TENANT_PART_ID" "$TENANT_PART_ID"
  assert_alnum "POSTGRES_USER" "$PG_USER"
  assert_alnum "POSTGRES_DB" "$PG_DB"
  assert_alnum "POSTGRES_PASSWORD" "$PG_PASSWORD"
  assert_alnum "NEO4J_PASSWORD" "$NEO4J_PASSWORD"
  assert_alnum "REDIS_PASSWORD" "$REDIS_PASSWORD"
  jwt=$(rand_alnum 32)
  api_key=$(rand_alnum 32)
  assert_alnum "JWT_SECRET" "$jwt"
  assert_alnum "ENV_API_KEY" "$api_key"

  sed_script=$(mktemp) || die "mktemp 失败"
  tmp_f=$(mktemp) || { rm -f "$sed_script"; die "mktemp 失败"; }
  {
    printf 's|<TENANT_PART_ID>|%s|g\n' "$(sed_escape "$TENANT_PART_ID")"
    printf 's|<REPLACE_WITH_BANYAN_JWT_SECRET>|%s|g\n' "$(sed_escape "$jwt")"
    printf 's|<NEO4J_PASSWORD>|%s|g\n' "$(sed_escape "$NEO4J_PASSWORD")"
    printf 's|<REDIS_PASSWORD>|%s|g\n' "$(sed_escape "$REDIS_PASSWORD")"
    printf 's|<POSTGRES_DB>|%s|g\n' "$(sed_escape "$PG_DB")"
    printf 's|<POSTGRES_USER>|%s|g\n' "$(sed_escape "$PG_USER")"
    printf 's|<POSTGRES_PASSWORD>|%s|g\n' "$(sed_escape "$PG_PASSWORD")"
    printf 's|<ENV_API_KEY>|%s|g\n' "$(sed_escape "$api_key")"
    printf '/^[[:space:]]*"_comment"[[:space:]]*:/d\n'
  } > "$sed_script"
  sed -f "$sed_script" "$EXAMPLE_JSON" > "$tmp_f" \
    || { rm -f "$sed_script" "$tmp_f"; die "种子 JSON 渲染失败（sed 异常）"; }
  rm -f "$sed_script"

  if grep -q '<[A-Z][A-Z0-9_]*>' "$tmp_f"; then
    rm -f "$tmp_f"
    die "种子 JSON 仍残留未替换占位符——env/se-configdata.example.json 可能被改动过"
  fi
  if grep -q '"_comment"' "$tmp_f"; then
    rm -f "$tmp_f"
    die "种子 JSON 仍含 _comment 键（会被当垃圾变量灌入 DDB）"
  fi
  mv -f "$tmp_f" "$SEED_JSON"
  chmod 600 "$SEED_JSON"
  log "已渲染 env/se-configdata.local.json（含随机 JWT_SECRET 与 x-api-key）"
}

# 必填键非空校验（收集全部缺失后一次报错）
validate_env_keys() {
  local missing=() k v
  for k in "${REQUIRED_ENV_KEYS[@]}"; do
    v=$(env_get "$k")
    if [ -z "$v" ]; then
      missing+=("$k")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die ".env 缺少必填键：${missing[*]}。手工补全，或删除 .env 与 env/se-configdata.local.json 后重跑自动生成（或 --force-env）"
  fi
}

fw_layout_ok() {
  if [ -f "$1/__init__.py" ]; then return 0; fi
  if [ -f "$1/$2/__init__.py" ]; then return 0; fi
  return 1
}

# 源码树校验（收集全部问题后一次报错，便于一次性修复）
validate_source_paths() {
  local problems=() f d

  for f in __init__.py app.py auth/middleware.py; do
    if [ ! -f "$GATEWAY_PACKAGE_DIR/$f" ]; then
      problems+=("网关包缺文件：${GATEWAY_PACKAGE_DIR}/$f（.env: GATEWAY_PACKAGE_DIR）")
    fi
  done
  for d in "${ENGINE_REPOS[@]}"; do
    if [ ! -d "$BANYAN_MODULES_DIR/$d" ]; then
      problems+=("Banyan 引擎目录缺失：${BANYAN_MODULES_DIR}/$d（.env: BANYAN_MODULES_DIR）")
    fi
  done
  for d in "${VENDOR_PKGS[@]}"; do
    if [ ! -f "$VENDOR_DIR/$d/__init__.py" ]; then
      problems+=("vendor 包缺失：${VENDOR_DIR}/$d（.env: VENDOR_DIR）")
    fi
  done
  if ! fw_layout_ok "$SILVAENGINE_BASE_DIR" silvaengine_base; then
    problems+=("框架仓布局无法识别：$SILVAENGINE_BASE_DIR（.env: SILVAENGINE_BASE_DIR）")
  fi
  if ! fw_layout_ok "$SILVAENGINE_UTILITY_DIR" silvaengine_utility; then
    problems+=("框架仓布局无法识别：$SILVAENGINE_UTILITY_DIR（.env: SILVAENGINE_UTILITY_DIR）")
  fi
  if ! fw_layout_ok "$SILVAENGINE_CONNECTIONS_DIR" silvaengine_connections; then
    problems+=("框架仓布局无法识别：$SILVAENGINE_CONNECTIONS_DIR（.env: SILVAENGINE_CONNECTIONS_DIR）")
  fi
  if [ ! -f "$DEPLOY_DIR/requirements-bindmount.txt" ]; then
    problems+=("缺少 deploy/requirements-bindmount.txt")
  fi
  if [ ! -f "$EXAMPLE_JSON" ]; then
    problems+=("缺少模板 env/se-configdata.example.json")
  fi

  if [ "${#problems[@]}" -gt 0 ]; then
    printf '[deploy.sh] 源码树校验未通过（%s 项）：\n' "${#problems[@]}" >&2
    for d in "${problems[@]}"; do
      printf '  - %s\n' "$d" >&2
    done
    die "源码树不完整——按上方清单补齐后重跑；源路径可用环境变量覆盖（见 -h）"
  fi

  # base compose 的 ddb-local 若被手动取消注释，会与 Overlay 的端口映射冲突
  if grep -q '^[[:space:]]*ddb-local:' "$DEPLOY_DIR/docker-compose.yml"; then
    printf '[deploy.sh] 警告：docker-compose.yml 中 ddb-local 已取消注释，可能与 docker-compose.ddb.yml 的端口映射冲突；建议保持注释（Overlay 已提供该服务）\n' >&2
  fi
  log "源码树校验通过：网关包 + 12 引擎 + vendor 3 包 + 3 框架"
}

# 四态状态机：复用 / 生成 / 仅 .env 补渲染 / 孤儿报错
ensure_config() {
  if [ "$FORCE_ENV" = "1" ]; then
    log "--force-env：删除并重新生成 .env 与种子 JSON（密码将变更）"
    rm -f "$ENV_FILE" "$SEED_JSON"
  fi

  local have_env=0 have_json=0
  if [ -f "$ENV_FILE" ]; then have_env=1; fi
  if [ -f "$SEED_JSON" ]; then have_json=1; fi

  if [ "$have_env" = "1" ] && [ "$have_json" = "1" ]; then
    log "复用现有配置：.env + env/se-configdata.local.json"
    load_env_values
  elif [ "$have_env" = "0" ] && [ "$have_json" = "0" ]; then
    log "首次部署：自动生成 .env（随机密码）并渲染种子 JSON"
    generate_env
    load_env_values
    render_seed_json
  elif [ "$have_env" = "1" ]; then
    log "种子 JSON 缺失——按现有 .env 重新渲染（密码/JWT 会重生成）"
    load_env_values
    render_seed_json
  else
    die "孤儿状态：env/se-configdata.local.json 存在但 .env 缺失（两者密码会不一致）。请恢复 .env，或两者都删除后重跑（或 --force-env 重新生成全部密码）"
  fi

  validate_env_keys
  validate_source_paths
}

# ---------------------------------------------------------------------------
# 阶段 1/3：环境与端口
# ---------------------------------------------------------------------------

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    die "未找到 docker 命令——请先安装 Docker：https://docs.docker.com/engine/install/"
  fi
  local info_out cv
  if ! info_out=$(docker info 2>&1); then
    printf '%s\n' "$info_out" >&2
    if printf '%s' "$info_out" | grep -qi 'permission denied'; then
      die "Docker 守护进程连接被拒（permission denied）——当前用户不在 docker 组。修复：sudo usermod -aG docker \$(id -un)，然后重新登录会话"
    fi
    die "Docker 守护进程未运行或连接失败——请启动 Docker（Linux: systemctl start docker；macOS: 打开 Docker Desktop）"
  fi
  cv=$(docker compose version --short 2>&1 || true)
  case "$cv" in
    v2*|2*) ;;
    *)
      die "需要 Docker Compose v2（当前：${cv:-未检出}）——请安装 docker-compose-plugin：https://docs.docker.com/compose/migrate/"
      ;;
  esac
  log "Docker 可用，Compose 版本：$cv"
}

check_ports() {
  local p holder
  for p in "${HOST_PORTS[@]}"; do
    if port_busy "$p"; then
      holder=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
        | grep ":$p->" | head -1 | cut -d' ' -f1 || true)
      if printf '%s' "$holder" | grep -q '^silvaengine-gateway'; then
        log "端口 $p 被本部署容器（$holder）占用——重复运行场景，继续"
      elif [ -n "$holder" ]; then
        die "端口 $p 被其他容器（$holder）占用——请停止该容器或调整端口映射"
      else
        die "端口 $p 被宿主机进程占用——排查：lsof -i :$p 或 ss -ltnp | grep :$p"
      fi
    fi
  done
  log "端口预检通过：${HOST_PORTS[*]}"
}

# ---------------------------------------------------------------------------
# 阶段 7：健康与日志
# ---------------------------------------------------------------------------

wait_healthy() {
  local deadline now all_ok st c line
  deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  log "等待容器健康（最长 ${WAIT_TIMEOUT}s；首次启动容器内 pip 安装约 1-3 分钟）"
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      printf '%s\n' '── gateway 最近日志（docker logs --tail 60）──' >&2
      docker logs --tail 60 silvaengine-gateway 2>&1 || true
      die "健康检查超时（${WAIT_TIMEOUT}s）。可用 GATEWAY_WAIT_TIMEOUT 环境变量延长等待"
    fi
    all_ok=1
    line=""
    for c in silvaengine-gateway-postgres silvaengine-gateway-neo4j \
      silvaengine-gateway-redis silvaengine-gateway; do
      st=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || true)
      line="$line ${c#silvaengine-gateway-}=${st:-none}"
      if [ "$st" != "healthy" ]; then
        all_ok=0
      fi
    done
    st=$(docker inspect -f '{{.State.Running}}' silvaengine-gateway-ddb-local 2>/dev/null || true)
    line="$line ddb-local=${st:-none}"
    if [ "$st" != "true" ]; then
      all_ok=0
    fi
    if [ "$all_ok" = "1" ]; then
      log "全部容器健康：$line"
      return 0
    fi
    printf '  等待中（剩余 %ss）：%s\n' "$(( deadline - now ))" "$line"
    sleep 5
  done
}

verify_gateway() {
  local out
  if ! out=$(dc exec -T gateway python -c \
    "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10); print('health=' + str(r.status))" 2>&1); then
    die "网关 /health 探活失败（容器内 urllib）"
  fi
  log "网关探活：$out"
}

# $1 = required（部署时缺失即失败）| info（status 时仅告警）
# 扫全量容器日志而非尾部窗口：启动日志可达数百行（含引擎迁移回溯），
# 固定 tail 窗口会把头部的两条启动标记挤出视野（实测踩坑）；
# "容器当前健康" 已由 wait_healthy 在前保证，全量扫描不会误放行崩溃循环。
check_log() {
  local logs miss=() pat
  logs=$(docker logs silvaengine-gateway 2>&1 || true)
  # herestring 而非管道：pipefail 下 grep -q 匹配即退会让上游 printf 吃 SIGPIPE
  # （141 → if! 判真 → 误报缺失）；启动日志 > 64KB 管道缓冲时必触发（实测踩坑）
  for pat in "${REQUIRED_LOGS[@]}"; do
    if ! grep -qF "$pat" <<< "$logs"; then
      miss+=("$pat")
    fi
  done
  if [ "${#miss[@]}" -gt 0 ]; then
    for pat in "${miss[@]}"; do
      printf '[deploy.sh] 缺失关键日志：%s\n' "$pat" >&2
    done
    if [ "$1" = "required" ]; then
      die "网关启动关键日志缺失——se-configdata 叠加或池引导未生效；完整日志：docker logs silvaengine-gateway"
    fi
  fi
  for pat in "${OPTIONAL_LOGS[@]}"; do
    if ! grep -qE "$pat" <<< "$logs"; then
      printf '[deploy.sh] 提示：可选日志缺失（%s）——repo-root 布局未检出或池回环改写未发生，非预期时请检查挂载路径与 BANYAN_LOOPBACK_BASE_URL\n' "$pat" >&2
    fi
  done
}

# ---------------------------------------------------------------------------
# 阶段 8：摘要
# ---------------------------------------------------------------------------

print_summary() {
  cat <<EOF

============================================================
 SilvaEngine Gateway（Banyan 数据面）部署完成
============================================================
  网关地址      : http://<服务器IP>:8000  （本机探活 curl http://127.0.0.1:8000/health）
  GraphQL 入口  : POST http://<服务器IP>:8000/beta/core/banyan/<engine>_engine_graphql
  租户 part_id  : ${TENANT_PART_ID}
  生成文件      : .env（数据面密码，600 权限，勿提交）
                  env/se-configdata.local.json（JWT/x-api-key 等，600 权限，勿提交）
  数据面        : postgres / neo4j / redis + DynamoDB Local（127.0.0.1:8001 仅本机）

验证步骤（README §5 六步验收）：
  curl -sS http://127.0.0.1:8000/health
  # 登录 mutation 骨架（携带 part_id 头 + Banyan JWT）：
  # curl -sS -X POST http://127.0.0.1:8000/beta/core/banyan/user_engine_graphql \\
  #   -H 'content-type: application/json' -H 'part_id: ${TENANT_PART_ID}' \\
  #   -d '{"query": "mutation { ... }"}'
  # 12 引擎 pytest 在开发机 banyan 仓执行（服务器无 Python）

常用操作：
  bash deploy.sh status          # 状态/健康/关键日志
  bash deploy.sh down            # 停止（数据卷保留）
  bash deploy.sh down -v         # 停止并删除数据卷
  bash deploy.sh --restart       # 改源码/种子 JSON 后重启网关
  bash deploy.sh --force-env     # 重新生成全部密码
  # 跟踪日志（三文件 Overlay）：
  # docker compose -f docker-compose.yml -f docker-compose.bindmount.yml \\
  #   -f docker-compose.ddb.yml logs -f gateway

安全提醒：公网防火墙只放行 8000；8001/5432/6379/7474/7687 仅供本机调试。
============================================================
EOF
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

cmd_up() {
  set_stage 1 "环境检测（Docker / Compose v2）" \
    "安装 Docker：https://docs.docker.com/engine/install/；权限：sudo usermod -aG docker \$(id -un) 后重新登录；Compose v2：安装 docker-compose-plugin"
  check_docker

  set_stage 2 "配置生成与源码树校验" \
    "源路径可用环境变量覆盖（bash deploy.sh -h 查看清单）；.env 改动后重跑即生效；--force-env 重新生成"
  ensure_config

  set_stage 3 "端口预检（${HOST_PORTS[*]}）" \
    "占用排查：lsof -i :<port>（macOS）/ ss -ltnp | grep :<port>（Linux）"
  check_ports

  if [ "$DRY_RUN" = "1" ]; then
    log "--dry-run 完成：环境/配置/端口全部通过，未启动任何容器"
    return 0
  fi

  set_stage 4 "启动 DynamoDB Local" \
    "镜像 amazon/dynamodb-local 不走 DOCKER_MIRROR_PREFIX（非 Docker Hub library 镜像），直连拉取失败时可先 docker pull amazon/dynamodb-local:latest 再重跑"
  dc up -d ddb-local

  set_stage 5 "初始化 se-configdata（建表/灌种子/核验）" \
    "失败多为种子 JSON 渲染或容器网络问题；详情：docker compose -f docker-compose.yml -f docker-compose.bindmount.yml -f docker-compose.ddb.yml logs ddb-init；重跑幂等"
  dc run --rm ddb-init

  set_stage 6 "启动网关与数据面（五服务）" \
    "镜像拉取失败检查网络/DOCKER_MIRROR_PREFIX；bind-mount 源路径以 .env 为准；pip 慢可设 PIP_INDEX_URL"
  dc up -d "${UP_SERVICES[@]}"
  if [ "$RESTART" = "1" ]; then
    log "--restart：重启 gateway"
    dc restart gateway
  fi

  set_stage 7 "健康检查与启动验证" \
    "看报错：docker logs silvaengine-gateway；首次启动 pip 安装较慢，可用 GATEWAY_WAIT_TIMEOUT=900 bash deploy.sh 延长等待"
  wait_healthy
  verify_gateway
  check_log required

  set_stage 8 "输出摘要" ""
  print_summary
}

cmd_status() {
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 .env——请先运行 bash deploy.sh 完成部署"
  fi
  set_stage S "状态检查" "docker compose ... ps -a 查看全部容器"
  dc ps
  local bad=0 c st
  for c in silvaengine-gateway-postgres silvaengine-gateway-neo4j \
    silvaengine-gateway-redis silvaengine-gateway; do
    st=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || true)
    printf '  %-34s %s\n' "$c" "${st:-未运行}"
    if [ "$st" != "healthy" ]; then
      bad=1
    fi
  done
  st=$(docker inspect -f '{{.State.Running}}' silvaengine-gateway-ddb-local 2>/dev/null || true)
  printf '  %-34s %s\n' "silvaengine-gateway-ddb-local" "${st:-未运行}"
  if [ "$st" != "true" ]; then
    bad=1
  fi
  check_log info
  if [ "$bad" = "1" ]; then
    log "存在未健康/未运行的容器（上方状态）"
    exit 1
  fi
  log "全部容器健康"
}

cmd_down() {
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 .env——如需清理残留容器/卷，请手工执行 docker compose down（三 -f 文件见 README §8.2）"
  fi
  set_stage D "停止并清理" "compose down 失败时可先 docker ps 查看占用"
  if [ "$VOLUMES" = "1" ]; then
    dc down -v
    log "已停止全部容器并删除数据卷（postgres/neo4j/redis 数据已清空）"
  else
    dc down
    log "已停止全部容器（数据卷保留；彻底清理：bash deploy.sh down -v）"
  fi
}

# ---------------------------------------------------------------------------
# 自检（纯逻辑，不碰 docker；在 mktemp 目录中运行生成/渲染）
# ---------------------------------------------------------------------------

cmd_self_test() {
  local tmp
  tmp=$(mktemp -d) || return 1
  local saved_env="$ENV_FILE" saved_seed="$SEED_JSON"
  local pass=0 fail=0

  expect_eq() {
    local desc="${1:-}" actual="${2:-}" expected="${3:-}"
    if [ "$actual" = "$expected" ]; then
      printf '  PASS: %s\n' "$desc"
      pass=$((pass + 1))
    else
      printf '  FAIL: %s —— 期望 [%s] 实际 [%s]\n' "$desc" "$expected" "$actual"
      fail=$((fail + 1))
    fi
  }

  ENV_FILE="$tmp/.env"
  SEED_JSON="$tmp/se-configdata.local.json"

  # rand_alnum
  local r1 r2
  r1=$(rand_alnum 16)
  r2=$(rand_alnum 16)
  expect_eq "rand_alnum 长度 16" "${#r1}" "16"
  expect_eq "rand_alnum 纯字母数字" \
    "$(printf '%s' "$r1" | grep -c '^[A-Za-z0-9]*$' || true)" "1"
  expect_eq "rand_alnum 两次不同" "$([ "$r1" != "$r2" ] && echo 1 || true)" "1"

  # sed_escape
  expect_eq "sed_escape 转义 \\ / &" "$(sed_escape 'a/b\c&d')" 'a\/b\\c\&d'

  # env_get（临时切到独立 .env 再切回）
  local saved_env2="$ENV_FILE"
  ENV_FILE="$tmp/envget"
  printf 'K1="va lue"\nK2=plain\n' > "$ENV_FILE"
  expect_eq "env_get 剥双引号" "$(env_get K1)" "va lue"
  expect_eq "env_get 普通值" "$(env_get K2)" "plain"
  expect_eq "env_get 缺失为空" "$(env_get K3)" ""
  ENV_FILE="$saved_env2"

  # generate_env → .env 断言
  generate_env
  local pw_check
  pw_check=$(env_get POSTGRES_PASSWORD)
  expect_eq "GATEWAY_IMAGE 占位值" "$(env_get GATEWAY_IMAGE)" "bindmount-placeholder"
  expect_eq "SE_CONFIGDATA_ENDPOINT_URL 指向 ddb-local" \
    "$(env_get SE_CONFIGDATA_ENDPOINT_URL)" "http://ddb-local:8000"
  expect_eq "POSTGRES_PASSWORD 16 位" "${#pw_check}" "16"
  expect_eq "POSTGRES_PASSWORD 纯字母数字" \
    "$(printf '%s' "$pw_check" | grep -c '^[A-Za-z0-9]*$' || true)" "1"
  expect_eq "NEO4J_AUTH 形态 neo4j/<16>" \
    "$(env_get NEO4J_AUTH | grep -c '^neo4j/[A-Za-z0-9]\{16\}$' || true)" "1"
  expect_eq "AWS 假凭据已写入" "$(env_get aws_access_key_id)" "local"
  if [ -z "${TENANT_PART_ID:-}" ]; then
    expect_eq "TENANT_PART_ID 默认 nestaging" "$(env_get TENANT_PART_ID)" "nestaging"
  fi
  expect_eq ".env 权限 600" \
    "$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || true)" "600"

  # load_env_values 回读
  load_env_values
  expect_eq "load_env_values 回读 PG 密码一致" "$PG_PASSWORD" "$(env_get POSTGRES_PASSWORD)"
  expect_eq "NEO4J_PASSWORD 解析 16 位" "${#NEO4J_PASSWORD}" "16"

  # render_seed_json（用仓内真实模板）
  render_seed_json
  expect_eq "种子 JSON 无残留占位符" "$(grep -c '<[A-Z][A-Z0-9_]*>' "$SEED_JSON" || true)" "0"
  expect_eq "种子 JSON 无 _comment" "$(grep -c '_comment' "$SEED_JSON" || true)" "0"
  expect_eq "种子 JSON 注入 PG 库名（3 处池）" \
    "$(grep -c "\"database\": \"$(env_get POSTGRES_DB)\"" "$SEED_JSON" || true)" "3"
  expect_eq "种子 JSON 注入 PG 密码（3 处池）" \
    "$(grep -c "\"password\": \"$(env_get POSTGRES_PASSWORD)\"" "$SEED_JSON" || true)" "3"
  expect_eq "种子 JSON initialize_tables 保留" \
    "$(grep -c 'initialize_tables": true' "$SEED_JSON" || true)" "1"
  expect_eq "种子 JSON part_id 已注入" \
    "$(grep -c "\"part_id\": \"$(env_get TENANT_PART_ID)\"" "$SEED_JSON" || true)" "1"

  # port_busy 空闲端口判定
  local free_p="" p
  for p in 39201 39211 39221 39231 39241 39251 39261 39271 39281 39291; do
    if ! port_busy "$p"; then
      free_p="$p"
      break
    fi
  done
  if [ -n "$free_p" ]; then
    if port_busy "$free_p"; then
      expect_eq "port_busy 空闲端口($free_p)判定非忙" "busy" "free"
    else
      expect_eq "port_busy 空闲端口($free_p)判定非忙" "free" "free"
    fi
  else
    printf '  （跳过 port_busy 测试：未找到空闲端口）\n'
  fi

  ENV_FILE="$saved_env"
  SEED_JSON="$saved_seed"
  rm -rf "$tmp"

  if [ "$fail" -gt 0 ]; then
    printf 'self-test FAILED（%s/%s 通过）\n' "$pass" "$((pass + fail))"
    return 1
  fi
  printf 'self-test OK（%s/%s 全部通过）\n' "$pass" "$pass"
  return 0
}

# ---------------------------------------------------------------------------
# 参数解析与分发
# ---------------------------------------------------------------------------

for arg in "$@"; do
  case "$arg" in
    up|deploy) MODE="up" ;;
    status) MODE="status" ;;
    down) MODE="down" ;;
    --volumes|-v) VOLUMES=1 ;;
    --restart) RESTART=1 ;;
    --force-env) FORCE_ENV=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --self-test) MODE="selftest" ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf '未知参数：%s\n' "$arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ "$MODE" != "down" ] && [ "$VOLUMES" = "1" ]; then
  printf '错误：-v/--volumes 仅可与 down 组合\n' >&2
  exit 1
fi

log "SilvaEngine Gateway 一键部署（模式 A：DynamoDB Local 全离线）"

case "$MODE" in
  selftest)
    if cmd_self_test; then exit 0; else exit 1; fi
    ;;
  status) cmd_status ;;
  down) cmd_down ;;
  up) cmd_up ;;
esac
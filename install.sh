#!/usr/bin/env bash
# iol2dcm 安装脚本
#
#   sudo bash install.sh                          # 逐项问，回车用默认值
#   sudo bash install.sh --mdb /storage/x.MDB \
#        --pacs pacs.example.org:4242 --institution 示例医院   # 静默装完
#   sudo bash install.sh --pacs pacs.example.org:4242 --update-config   # 只改 PACS
#   sudo bash install.sh --help
#
# 本脚本负责三件事：
#   1. 生成 /opt/iol2dcm/config.yaml（按现场参数打补丁，保留原有中文注释）
#   2. 生成 /etc/systemd/system/iol2dcm.service（路径跟着 APP 变量走，不写死）
#   3. 装依赖 → 自检 → enable + 启动
#
# 幂等：重复运行只更新代码与服务，不覆盖现场改过的 config.yaml。
set -euo pipefail

# ---------------------------------------------------------------- 路径
APP=/opt/iol2dcm                 # 程序目录（也是 config.yaml 与常数表的家）
VAR=/var/lib/iol2dcm             # state.json
LOG=/var/log/iol2dcm.log
UNIT=/etc/systemd/system/iol2dcm.service
LOGROT=/etc/logrotate.d/iol2dcm

CONFIG="$APP/config.yaml"        # ★ 必须在 APP 根：constants_file 相对路径靠它
CONSTJSON="$APP/iolcon_constants.json"
VENV="$APP/venv"
PY="$VENV/bin/python"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 现场默认值（可用环境变量覆盖）
DEFAULT_MDB="${IOL2DCM_DEFAULT_MDB:-/storage/iolmaster/IOLData/IolmXP.MDB}"
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT="4242"
DEFAULT_AET="ORTHANC"
DEFAULT_INST=""
DEFAULT_INTERVAL="60"

# ---------------------------------------------------------------- 参数
OPT_MDB="" ; OPT_HOST="" ; OPT_PORT="" ; OPT_AET=""
OPT_INST="" ; OPT_INTERVAL=""
declare -A GIVEN=()          # 用户显式给了哪些参数
ASSUME_YES=0
FORCE_CONFIG=0
UPDATE_CONFIG=0
DO_START=1
SKIP_CONSTANTS=0
CONST_MAX_AGE=30            # 已有常数表超过这么多天就顺手拉一次新的

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "$*"; }
note() { echo "  -> $*"; }
warn() { echo "  ⚠ $*" >&2; }

split_pacs() {
  # "host:port" / "host" → OPT_HOST / OPT_PORT。IPv6 用 [::1]:4242。
  local raw="$1"
  [[ -n "$raw" ]] || die "--pacs 不能为空"
  if [[ "$raw" == \[*\]:* ]]; then
    OPT_HOST="${raw%:*}" ; OPT_HOST="${OPT_HOST#[}"; OPT_HOST="${OPT_HOST%]}"
    OPT_PORT="${raw##*:}"
  elif [[ "$raw" == *:* ]]; then
    OPT_HOST="${raw%:*}"
    OPT_PORT="${raw##*:}"
  else
    OPT_HOST="$raw"
    OPT_PORT="$DEFAULT_PORT"
  fi
  [[ -n "$OPT_HOST" ]] || die "--pacs 里缺少主机名: $raw"
  [[ "$OPT_PORT" =~ ^[0-9]+$ ]] || die "--pacs 端口不是数字: $raw"
}

usage() {
  cat <<'USAGE'
iol2dcm 安装脚本

用法
  sudo bash install.sh [选项]

选项
  --mdb PATH           IolmXP.MDB 路径
  --pacs HOST[:PORT]   DICOM 目标，端口默认 4242
  --aet TITLE          目标 AE Title，默认 ORTHANC
  --institution NAME   报告单页眉机构名（留空则不印院名）
  --interval SEC       MDB 轮询间隔，默认 60

  --update-config      只把上面给的参数写进已有 config.yaml，其余不动
  --force-config       丢弃现有 config.yaml（先备份），按参数重建
  -y, --yes            不提问，缺的项用默认值
  --no-start           装好但不启动服务

不带任何选项时逐项提问，回车即用默认值。只给部分选项时，只问剩下的。
--update-config 与 --force-config 互斥；两者都不给时，已存在的 config.yaml 原样保留。

  --skip-constants     装的时候不去 iolcon.org 拉常数表（用已有的）

生成的文件
  /opt/iol2dcm/config.yaml                 型号清单 / 口径 / 数据源 / 推送目标
  /opt/iol2dcm/iolcon_constants.json       IOLCon 常数表（安装时拉取）
  /etc/systemd/system/iol2dcm.service      主服务
  /etc/logrotate.d/iol2dcm                 日志轮转

PACS 目标只认 config.yaml 的 push 段，一处配置，不会「改了不生效」。
USAGE
}

need_val() { [[ $# -ge 2 ]] || die "$1 需要一个值（见 --help）"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mdb)           need_val "$@"; OPT_MDB="$2";        GIVEN[mdb]=1;      shift 2 ;;
    --mdb=*)         OPT_MDB="${1#*=}";                  GIVEN[mdb]=1;      shift ;;
    --pacs)          need_val "$@"; GIVEN[pacs]=1;       split_pacs "$2";   shift 2 ;;
    --pacs=*)        GIVEN[pacs]=1;                      split_pacs "${1#*=}"; shift ;;
    --aet)           need_val "$@"; OPT_AET="$2";        GIVEN[aet]=1;      shift 2 ;;
    --aet=*)         OPT_AET="${1#*=}";                  GIVEN[aet]=1;      shift ;;
    --institution)   need_val "$@"; OPT_INST="$2";       GIVEN[inst]=1;     shift 2 ;;
    --institution=*) OPT_INST="${1#*=}";                 GIVEN[inst]=1;     shift ;;
    --interval)      need_val "$@"; OPT_INTERVAL="$2";   GIVEN[interval]=1; shift 2 ;;
    --interval=*)    OPT_INTERVAL="${1#*=}";             GIVEN[interval]=1; shift ;;
    --update-config) UPDATE_CONFIG=1; shift ;;
    --force-config)  FORCE_CONFIG=1;  shift ;;
    -y|--yes)        ASSUME_YES=1;    shift ;;
    --no-start)       DO_START=0;       shift ;;
    --skip-constants) SKIP_CONSTANTS=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) die "未知参数: $1（见 --help）" ;;
  esac
done

[[ $FORCE_CONFIG -eq 1 && $UPDATE_CONFIG -eq 1 ]] \
  && die "--force-config 与 --update-config 不能同时用"

# ---------------------------------------------------------------- 前置检查
[[ $EUID -eq 0 ]] || die "请用 root 跑：sudo bash $0"

# 从 APP 内部跑会把源码自己删掉（下面有 rm -rf "$APP/$pkg"）
case "$SRC" in
  "$APP"|"$APP"/*) die "别在 $APP 里运行 install.sh —— 会把这个目录当作源码删掉。请回到仓库目录运行。" ;;
esac

command -v systemctl >/dev/null 2>&1 \
  || die "找不到 systemctl，这台机器没有 systemd，本安装脚本不适用"

# 随后要复制到 $APP 的东西，缺一样就在这里停 —— 不要等到 cp 时才静默漏掉
for f in pipeline.py config.yaml pyproject.toml README.md DISCLAIMER.md LICENSE \
         tools/patch_config.py; do
  [[ -f "$SRC/$f" ]] || die "源码不完整，缺文件: $SRC/$f"
done
for d in core sources formulas render dicomlib tools; do
  [[ -d "$SRC/$d" ]] || die "源码不完整，缺目录: $SRC/$d"
done

command -v python3 >/dev/null 2>&1 || die "找不到 python3"

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 版本太低（$PYVER），需要 3.9 或更高"

systemd_ver() {
  local v
  v="$(systemctl --version 2>/dev/null | head -1 | awk '{print $2}')"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}
SYSD_VER="$(systemd_ver)"

# ---------------------------------------------------------------- 决定配置策略
# fresh: 生成新的   keep: 原样保留   patch: 只改指定字段   rebuild: 备份后重建
if [[ ! -f "$CONFIG" ]]; then
  CFG_MODE=fresh
elif [[ $FORCE_CONFIG -eq 1 ]]; then
  CFG_MODE=rebuild
elif [[ $UPDATE_CONFIG -eq 1 ]]; then
  CFG_MODE="patch"
  [[ ${#GIVEN[@]} -gt 0 ]] || die "--update-config 需要至少给一个参数，否则没东西可改"
else
  CFG_MODE=keep
fi

# ---------------------------------------------------------------- 收集参数
# 只有真要写 config 时才问；keep 模式一个都不问。
PROMPT=0
if [[ "$CFG_MODE" == fresh || "$CFG_MODE" == rebuild ]]; then
  PROMPT=1
fi
if [[ $ASSUME_YES -eq 1 ]] || [[ ! -t 0 ]]; then
  PROMPT=0
fi

ask() {
  # ask <提示> <默认值>；值走 stdout，提示走 stderr（这样能放进 $(...)
  #
  # ★ 显式 return 0：set -e 下，函数返回非 0 会让调用处的
  #   'v="$(ask ...)"' 被判为失败并直接退出整个安装脚本。
  local prompt="$1" def="$2" ans=""
  printf '  %s' "$prompt" >&2
  if [[ -n "$def" ]]; then
    printf ' [%s]' "$def" >&2
  fi
  printf ': ' >&2
  IFS= read -r ans || true
  if [[ -z "$ans" ]]; then
    ans="$def"
  fi
  printf '%s' "$ans"
  return 0
}

if [[ $PROMPT -eq 1 ]]; then
  echo "== 现场参数（回车用默认值）=="

  if [[ -z "${GIVEN[mdb]:-}" ]]; then
    tries=0
    while :; do
      OPT_MDB="$(ask "MDB 路径（IolmXP.MDB）" "${OPT_MDB:-$DEFAULT_MDB}")"
      if [[ -f "$OPT_MDB" ]]; then
        break
      fi
      warn "文件不存在：$OPT_MDB"
      tries=$((tries + 1))
      if [[ $tries -ge 2 ]]; then
        warn "仍按此路径继续 —— 装完自检会因「库结构」失败而不启动服务。"
        break
      fi
      echo "    重新输入，或回车沿用（Ctrl-C 退出）" >&2
    done
  fi

  if [[ -z "${GIVEN[pacs]:-}" ]]; then
    raw="$(ask "PACS 目标 HOST:PORT" "${OPT_HOST:-$DEFAULT_HOST}:${OPT_PORT:-$DEFAULT_PORT}")"
    split_pacs "$raw"
  fi
  if [[ -z "${GIVEN[aet]:-}" ]]; then
    OPT_AET="$(ask "AE Title" "${OPT_AET:-$DEFAULT_AET}")"
  fi
  if [[ -z "${GIVEN[inst]:-}" ]]; then
    OPT_INST="$(ask "机构名（留空不印院名）" "${OPT_INST:-$DEFAULT_INST}")"
  fi
  if [[ -z "${GIVEN[interval]:-}" ]]; then
    OPT_INTERVAL="$(ask "轮询间隔（秒）" "${OPT_INTERVAL:-$DEFAULT_INTERVAL}")"
  fi
  echo
fi

# 缺的项落回默认值
OPT_MDB="${OPT_MDB:-$DEFAULT_MDB}"
OPT_HOST="${OPT_HOST:-$DEFAULT_HOST}"
OPT_PORT="${OPT_PORT:-$DEFAULT_PORT}"
OPT_AET="${OPT_AET:-$DEFAULT_AET}"
OPT_INTERVAL="${OPT_INTERVAL:-$DEFAULT_INTERVAL}"

[[ "$OPT_PORT" =~ ^[0-9]+$ ]] || die "端口不是数字: $OPT_PORT"
[[ "$OPT_INTERVAL" =~ ^[0-9]+$ ]] || die "轮询间隔不是数字: $OPT_INTERVAL"

# ---------------------------------------------------------------- 1 目录
echo "== 1/8 目录 =="
mkdir -p "$APP" "$VAR"
# ★ VAR 单独给 0700：里面的 state.json 存患者姓名与生日，
#   目录可列就等于泄漏了「哪天有哪个病人」。APP 是代码，0755 无妨。
chmod 0755 "$APP"
chmod 0700 "$VAR"
note "$APP  $VAR"

# ---------------------------------------------------------------- 2 系统依赖
echo "== 2/8 系统依赖 =="
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq || warn "apt-get update 失败，继续（可能用的是本地缓存）"
  apt-get install -y -qq python3-venv fonts-noto-cjk fonts-liberation \
    || warn "apt 装包失败，若自检报「中文字体」再手工装 fonts-noto-cjk"
  note "python3-venv / fonts-noto-cjk / fonts-liberation"
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm --needed python noto-fonts-cjk ttf-liberation || warn "pacman 装包失败"
  note "python / noto-fonts-cjk / ttf-liberation"
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3-pip google-noto-sans-cjk-fonts liberation-sans-fonts || warn "dnf 装包失败"
  note "python3-pip / noto-cjk / liberation"
else
  warn "未识别的包管理器。请自行确认：python3-venv、中文字体（Noto CJK）"
fi

# ---------------------------------------------------------------- 3 代码
echo "== 3/8 代码 =="
cp -f "$SRC/pipeline.py" "$APP"/
for pkg in core sources formulas render dicomlib tools; do
  [[ -n "$APP" && "$APP" != "/" ]] || die "APP 变量异常: '$APP'"
  rm -rf "${APP:?}/$pkg"
  cp -r "$SRC/$pkg" "$APP/$pkg"
done
if [[ -d "$SRC/docs" ]]; then
  # ★ 先删后拷：cp -r 到已存在的目标目录会嵌一层，重装几次就成 docs/docs/docs
  rm -rf "${APP:?}/docs"
  cp -r "$SRC/docs" "$APP/docs"
fi
find "$APP" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
# pyproject.toml 的 license = { file = "LICENSE" } 依赖这个文件：
# 少了它，构建出的包 license 字段会静默为空（不报错）。
cp -f "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/DISCLAIMER.md" "$SRC/LICENSE" "$APP"/

note "代码已复制到 $APP"

# ---------------------------------------------------------------- 4 venv
echo "== 4/8 Python 环境 =="
if [[ ! -x "$PY" ]]; then
  if ! out="$(python3 -m venv "$VENV" 2>&1)"; then
    echo "$out" | tail -5 >&2
    die "建 venv 失败。Debian/Ubuntu 需要：apt-get install python3-venv"
  fi
  note "已建 venv: $VENV"
else
  note "venv 已存在"
fi

PIPLOG="$(mktemp)"
trap 'rm -f "$PIPLOG"' EXIT

if "$PY" -m pip install -q --disable-pip-version-check \
      -i https://pypi.tuna.tsinghua.edu.cn/simple -e "$APP" >"$PIPLOG" 2>&1; then
  note "依赖已装（清华镜像）"
elif "$PY" -m pip install -q --disable-pip-version-check -e "$APP" >"$PIPLOG" 2>&1; then
  note "依赖已装（官方源）"
else
  echo "  !! 依赖安装失败，pip 日志末尾：" >&2
  tail -20 "$PIPLOG" >&2
  die "依赖装不上，服务不会启动。检查网络或代理后重跑。"
fi

# ---------------------------------------------------------------- 5 常数表
echo "== 5/8 常数表 =="

# 常数表不随仓库分发：IOLCon 用户条款 8.1 只允许为自身计算目的使用数据，
# 禁止再分发。所以每次部署都现场拉，拉不到就用机器上已有的。
# 检查逻辑：够新就不拉（省一次外部请求），太旧/缺失就拉。
const_age_days() {
  # 输出常数表的天数年龄；文件不存在输出空
  [[ -f "$CONSTJSON" ]] || return 0
  local mt now
  mt="$(stat -c %Y "$CONSTJSON" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  echo $(( (now - mt) / 86400 ))
}

HAVE_CONST=0
if [[ -f "$CONSTJSON" ]]; then
  HAVE_CONST=1
fi

if [[ $SKIP_CONSTANTS -eq 1 ]]; then
  if [[ $HAVE_CONST -eq 1 ]]; then
    note "跳过拉取（--skip-constants），用现有常数表（$(const_age_days) 天前）"
  else
    warn "跳过拉取（--skip-constants），但本机没有常数表"
  fi
else
  AGE="$(const_age_days)"
  if [[ $HAVE_CONST -eq 1 && -n "$AGE" && "$AGE" -lt $CONST_MAX_AGE ]]; then
    note "常数表 ${AGE} 天前生成，够新，跳过拉取"
  else
    [[ $HAVE_CONST -eq 1 ]] && note "常数表 ${AGE} 天前生成，拉一次新的"
    [[ $HAVE_CONST -eq 0 ]] && note "本机没有常数表，从 iolcon.org 拉取"
    # constants 子命令失败不会破坏已有文件（atomic_write + 款数骤减保护）
    CLOG="$(mktemp)"
    if "$PY" "$APP/pipeline.py" constants -o "$CONSTJSON" --timeout 120 >"$CLOG" 2>&1; then
      tail -3 "$CLOG" | sed 's/^/     /'
      HAVE_CONST=1
    else
      echo "  !! 拉取失败：" >&2
      tail -5 "$CLOG" | sed 's/^/     /' >&2
      if [[ $HAVE_CONST -eq 1 ]]; then
        warn "沿用现有的常数表（$(const_age_days) 天前）"
      fi
    fi
    rm -f "$CLOG"
  fi
fi

if [[ $HAVE_CONST -eq 0 ]]; then
  warn "本机没有常数表，管线无法计算。三种办法任选："
  echo "     1) 联网后重跑：       sudo bash $0" >&2
  echo "     2) 拷入已有的表：     install -m 0600 <iolcon_constants.json> $CONSTJSON" >&2
  echo "     3) 手工下载 XML 解析：$PY $APP/pipeline.py constants --from-xml <文件>" >&2
  die "没有常数表，拒绝安装（装了也跑不起来）"
fi
chmod 0600 "$CONSTJSON"

# ---------------------------------------------------------------- 6 config.yaml
echo "== 6/8 配置文件 =="

case "$CFG_MODE" in
  keep)
    note "$CONFIG 已存在，保留现场配置"
    if [[ ${#GIVEN[@]} -gt 0 ]]; then
      warn "本次给的参数没有写入。要改配置加 --update-config，要重建加 --force-config"
    fi
    ;;
  *)
    if [[ "$CFG_MODE" == rebuild ]]; then
      bak="$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
      cp -f "$CONFIG" "$bak"
      note "已备份旧配置 → $bak"
    fi
    # fresh / rebuild 从模板重新铺一份；patch 原地改
    if [[ "$CFG_MODE" != patch ]]; then
      install -m 0644 "$SRC/config.yaml" "$CONFIG"
      note "已从模板生成 $CONFIG"
    fi

    # 只把「用户显式给的 + fresh/rebuild 需要填的」交给补丁工具
    PATCH_ARGS=()
    if [[ "$CFG_MODE" == patch ]]; then
      if [[ -n "${GIVEN[mdb]:-}" ]]; then
        PATCH_ARGS+=("mdb_path=$OPT_MDB")
      fi
      if [[ -n "${GIVEN[pacs]:-}" ]]; then
        PATCH_ARGS+=("push.dicom_host=$OPT_HOST" "push.dicom_port=$OPT_PORT")
      fi
      if [[ -n "${GIVEN[aet]:-}" ]]; then
        PATCH_ARGS+=("push.ae_title=$OPT_AET")
      fi
      if [[ -n "${GIVEN[inst]:-}" ]]; then
        PATCH_ARGS+=("report.institution=$OPT_INST")
      fi
      if [[ -n "${GIVEN[interval]:-}" ]]; then
        PATCH_ARGS+=("watcher.interval_seconds=$OPT_INTERVAL")
      fi
    else
      PATCH_ARGS+=(
        "mdb_path=$OPT_MDB"
        "push.dicom_host=$OPT_HOST"
        "push.dicom_port=$OPT_PORT"
        "push.ae_title=$OPT_AET"
        "report.institution=$OPT_INST"
        "watcher.interval_seconds=$OPT_INTERVAL"
      )
    fi

    if [[ ${#PATCH_ARGS[@]} -gt 0 ]]; then
      python3 "$APP/tools/patch_config.py" "$CONFIG" "${PATCH_ARGS[@]}" \
        || die "写 config.yaml 失败，未做任何修改"
      # 只报本次真正动过的字段；没动的不提，否则会报出跟文件里不一样的默认值
      if [[ "$CFG_MODE" != patch ]]; then
        note "MDB    : $OPT_MDB"
        note "PACS   : $OPT_HOST:$OPT_PORT  AE=$OPT_AET"
        note "机构名 : ${OPT_INST:-（空，不印院名）}"
        note "轮询   : 每 $OPT_INTERVAL 秒"
      else
        if [[ -n "${GIVEN[mdb]:-}" ]]; then
          note "MDB    : $OPT_MDB"
        fi
        if [[ -n "${GIVEN[pacs]:-}" ]]; then
          note "PACS   : $OPT_HOST:$OPT_PORT"
        fi
        if [[ -n "${GIVEN[aet]:-}" ]]; then
          note "AE     : $OPT_AET"
        fi
        if [[ -n "${GIVEN[inst]:-}" ]]; then
          note "机构名 : ${OPT_INST:-（空，不印院名）}"
        fi
        if [[ -n "${GIVEN[interval]:-}" ]]; then
          note "轮询   : 每 $OPT_INTERVAL 秒"
        fi
      fi
    fi
    ;;
esac

# ---- 提前验证 config.yaml 的 lenses ----
# 型号写错（差一个字母、同名歧义、缺常数）在这里就报，别等到第 8 步自检 ——
# 那时代码和 systemd 都装完了，回滚更麻烦。
# 这一步只读，不联网：常数表刚拉过，就在 $CONSTJSON。
if [[ "$HAVE_CONST" -eq 1 ]]; then
  VLOG="$(mktemp)"
  if "$PY" "$APP/pipeline.py" lenses --config "$CONFIG" >"$VLOG" 2>&1; then
    N_LENSES="$(sed -n 's/^上表 \([0-9]*\) 款.*/\1/p' "$VLOG" | head -1)"
    note "config.yaml 的 lenses 可解析（上表 ${N_LENSES:-?} 款）"
    # 有缺公式的型号就点出来（不阻止安装，但现场该知道）
    if grep -q '⚠ 缺' "$VLOG"; then
      warn "有型号常数不齐，对应格子会显示 —："
      grep '⚠ 缺' "$VLOG" | sed 's/^/     /' >&2
    fi
  else
    echo "  ✗ config.yaml 的 lenses 解析失败：" >&2
    sed 's/^/     /' "$VLOG" >&2
    echo "" >&2
    echo "  改 $CONFIG 的 lenses 段后重跑本脚本。查型号用（本地，毫秒级）：" >&2
    echo "     $PY $APP/pipeline.py constants --show <关键词> --local" >&2
    rm -f "$VLOG"
    die "型号配置有误，未继续安装"
  fi
  rm -f "$VLOG"
fi

# 常数表占位：没有就给条明确出路
if [[ "$HAVE_CONST" -eq 0 ]]; then
  warn "缺常数表，服务无法计算。生成后重跑本脚本："
  echo "     cd $APP && $PY pipeline.py constants" >&2
fi

# ---------------------------------------------------------------- 7 systemd
echo "== 7/8 systemd =="

# 生成 unit：路径全部来自变量，改 APP 不用同步改第二个文件
UNIT_LINES=(
  "[Unit]"
  "Description=iol2dcm — IOLMaster 晶体单自动生成与推送"
  "Documentation=file://$APP/README.md"
  # ★ 开机时 PACS（Orthanc）往往比本管线上线慢几十秒到几分钟。管线自己会在
  #   原地守候并反复探测（见 config.yaml 的 watchdog 段），所以这里**不需要**
  #   把对端单元列进 After=/Wants= —— 那会变成硬依赖（单元不存在则服务起
  #   不来），而且换 PACS 还得改第二次配置。
  "After=network-online.target local-fs.target"
  "Wants=network-online.target"
  ""
  "[Service]"
  "Type=simple"
  "WorkingDirectory=$APP"
  "Environment=IOL2DCM_CONFIG=$CONFIG"
  "ExecStart=$PY $APP/pipeline.py"
  "Restart=always"
  "RestartSec=30"
  ""
  "# 2C/2G 的小机器，别让它吃满"
  "Nice=10"
  "IOSchedulingClass=idle"
)
# MemoryMax 要 systemd ≥ 231
if [[ "$SYSD_VER" -ge 231 ]]; then
  UNIT_LINES+=("MemoryMax=800M")
fi
# append: 要 systemd ≥ 240；更老的版本退回 journald
GOTO_JOURNAL=0
if [[ "$SYSD_VER" -ge 240 ]]; then
  UNIT_LINES+=("StandardOutput=append:$LOG" "StandardError=append:$LOG")
else
  GOTO_JOURNAL=1
fi
UNIT_LINES+=("" "[Install]" "WantedBy=multi-user.target")

printf '%s\n' "${UNIT_LINES[@]}" > "$UNIT"
chmod 0644 "$UNIT"
note "已生成 $UNIT（systemd $SYSD_VER）"

# ---- 清理常数表定时任务 ----
# 常数表只在安装时拉一次，不挂定时器。若机器上残留 iol2dcm-constants.*
# （会半夜拉表并重启服务），这里显式停掉并删除。
CLEANED=0
for u in iol2dcm-constants.timer iol2dcm-constants.service; do
  if [[ -f "/etc/systemd/system/$u" ]]; then
    systemctl disable --now "$u" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$u"
    CLEANED=1
  fi
done
if [[ $CLEANED -eq 1 ]]; then
  note "已清理残留的常数表定时任务"
fi

# 日志轮转：unit 用 append: 一直持有句柄，所以必须 copytruncate，
# 否则 rename 之后服务继续往已删除的 inode 里写，日志实际丢失。
# 权限走 create 0640 —— 日志含患者身份信息，不能按默认 0644 落盘。
if [[ $GOTO_JOURNAL -eq 0 ]]; then
  # create 0640：日志行里带患者姓名与检查日期，不能按默认 0644 落盘
  cat > "$LOGROT" <<ROTEOF
$LOG {
    weekly
    rotate 4
    size 5M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0640 root root
}
ROTEOF
  chmod 0644 "$LOGROT"
  note "已生成 $LOGROT（每周/5M 轮转，保留 4 份）"
else
  rm -f "$LOGROT"
  note "systemd 版本较老，日志走 journald：journalctl -u iol2dcm -f"
fi

systemctl daemon-reload

# ---------------------------------------------------------------- 8 自检 + 启动
echo "== 8/8 自检 =="

# --soft-push：PACS 现在不通只警告，不拦安装。
# 管线是 watch 常驻：启动后原地守候，每 watchdog.probe_seconds 探测一次
# （TCP + C-ECHO），通了立刻补推积压。PACS 后起来不会漏单。
if "$PY" "$APP/pipeline.py" check --config "$CONFIG" --soft-push; then
  CHECK_OK=1
else
  CHECK_OK=0
fi

if [[ $CHECK_OK -eq 1 ]]; then
  systemctl enable iol2dcm.service >/dev/null 2>&1
  if [[ $DO_START -eq 1 ]]; then
    systemctl restart iol2dcm.service
    note "服务已启动并设为开机自启"
  else
    note "已设为开机自启（--no-start，未启动）"
  fi
else
  warn "自检未通过，服务未启动。修好后再执行："
  echo "     systemctl enable --now iol2dcm" >&2
  echo "     重跑本脚本也行：sudo bash $0 --update-config ..." >&2
fi

# ---------------------------------------------------------------- 收尾
if [[ "$GOTO_JOURNAL" -eq 1 ]]; then
  LOGCMD="journalctl -u iol2dcm -f"
else
  LOGCMD="tail -f $LOG"
fi

cat <<DONE

────────────────────────────────────────────────────────────
安装完成。

  状态    : systemctl status iol2dcm
  日志    : $LOGCMD
  停止    : systemctl stop iol2dcm

配置文件（唯一的配置源，没有 env 文件）
  $CONFIG

  换晶体型号  → lenses 段
  换 PACS     → push 段
  改 MDB 路径 → mdb_path

  改完 systemctl restart iol2dcm 即可生效，不用重装。
  也可以用 install.sh 改单个字段：
    sudo bash $0 --pacs pacs.example.org:4242 --update-config

运维子命令
  P=$PY
  \$P $APP/pipeline.py check      # 自检
  \$P $APP/pipeline.py lenses     # 看型号与常数
  \$P $APP/pipeline.py render --patient 张三 --date 20250101 --outdir /tmp/out
  \$P $APP/pipeline.py run --limit 5

常数表（IOLCon，只在安装时拉一次，不会自动更新）
  \$P $APP/pipeline.py constants       # 想更新时手工跑，然后 restart 主服务
  systemctl restart iol2dcm            # 新常数要重启才生效

⚠️  适用范围：本项目仅对 Carl Zeiss IOLMaster 500（软件 V. 5.5）测试过。
    IOLMaster 700 及其他型号/厂商设备未经测试，不做任何可用性保证。
    换了机型必须重新做 DISCLAIMER.md 里列的四项核对。
────────────────────────────────────────────────────────────
DONE

if [[ $CHECK_OK -eq 0 ]]; then
  exit 1
fi

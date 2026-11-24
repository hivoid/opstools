#!/usr/bin/env bash
#
# fo - fzf 选文件并用指定或默认程序打开
#
# 依赖: bash >= 4, fzf
# 候选: fd / fdfind / find (按序回退)
# 打开: 显式 OPENER -> 扩展名映射表 -> xdg-open(Linux)/open(macOS)
#
# 用法: fo [OPENER] [-d DIR] [选项] [-- FZF_OPTIONS...]
#
# 配置: ${XDG_CONFIG_HOME:-~/.config}/fo/opener.conf
#   pdf = okular
#   txt = kate
#   png = gwenview
#   # 值按空白拆分为命令与参数: pdf = okular --unique
#   # 前缀控制运行方式:  "!" = 前台,  "&" = 脱钩
#   md = !nvim
#   pdf = &okular
#
#   不支持含空格的单个参数; 不支持行尾注释
#
# 运行方式判定 (auto 默认):
#   映射表前缀  >  -t/-x 显式开关  >  内置 TUI 名单
#   >  无显示服务器  >  默认脱钩
#
# todo: 增加 -z 参数使用 zoxide query 确定目录，做为 -d 的快捷操作

set -euo pipefail

# ---------- 常量 ----------
PROG="${0##*/}"
[[ -z "$PROG" || "$PROG" == "bash" || "$PROG" == "-bash" ]] && PROG="fo"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fo"
OPENER_CONF="${FO_OPENER_CONF:-$CONFIG_DIR/opener.conf}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/fo"
LAST_DIR_FILE="$CACHE_DIR/last_dir"
HISTORY_FILE="$CACHE_DIR/history.log"

# ---------- 状态 ----------
dir=""
opener=""
use_last_dir=0
include_hidden=0
no_ignore=0
multi=0
preview=0
dry_run=0
run_mode="auto" # auto | fg | detach
tmp_sel=""
fzf_status=0
declare -a fzf_extra=()
declare -a finder_args=()
declare -a selected=()
declare -A TUI_SET=() # 内置 TUI 程序名单

# ---------- 辅助 ----------
die() {
  echo "$PROG: $*" >&2
  exit 1
}
warn() { echo "$PROG: 警告: $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

cleanup() { [[ -n "$tmp_sel" ]] && rm -f "$tmp_sel" || true; }
trap cleanup EXIT

# best-effort 写缓存; $1=路径 $2=内容 $3=append|overwrite
# 目录不可写只警告不中断(只读 FS / 无 HOME 也可用)
cache_write() {
  local path="$1" content="$2" mode="${3:-append}"
  mkdir -p "$CACHE_DIR" 2>/dev/null || {
    warn "无法创建缓存目录 $CACHE_DIR"
    return 0
  }
  if [[ "$mode" == "overwrite" ]]; then
    printf '%s\n' "$content" >"$path" 2>/dev/null || warn "无法写入 $path"
  else
    printf '%s\n' "$content" >>"$path" 2>/dev/null || warn "无法写入 $path"
  fi
  return 0
}

usage() {
  cat <<EOF
用法: $PROG [OPENER] [-d DIR] [选项] [-- FZF_OPTIONS...]

选项:
  -d DIR           在 DIR 下搜索文件（默认当前目录；与 --last 同给时 -d 优先）
  -l, --last       使用上次 -d 的目录
  -m, --multi      允许 fzf 多选
  -p, --preview    启用内置预览（bat/chafa/viu/pdftotext 自动检测降级）
  -H, --hidden     包含隐藏文件
  -I, --no-ignore  不遵循 .gitignore（仅 fd/fdfind 有效）
  -t, --foreground 打开器全部在前台运行（阻塞至退出，多选时逐个顺序打开）
  -x, --detach     打开器全部脱钩运行（不占终端，终端关闭不影响）
  -n, --dry-run    打印将执行的命令；仍会启动 fzf 供选择，但不实际打开，
                   也不写 last_dir 和历史
  -h, --help       显示本帮助
  --               其后参数原样传给 fzf（如与 -p 的 --preview 冲突，以 -- 后的为准）

环境变量:
  FO_OPENER_CONF   覆盖打开器映射表路径
  默认: \${XDG_CONFIG_HOME:-~/.config}/fo/opener.conf

运行方式（默认 auto，自动判定）:
  1. 映射表前缀覆盖单个程序:   "!" = 前台,  "&" = 脱钩
       md = !nvim
       pdf = &okular
  2. -t / -x 全局强制覆盖
  3. 内置 TUI 名单（nvim/vim/less/htop/emacs 等）→ 前台
  4. 无显示服务器（SSH/纯 TTY）→ 前台
  5. 其余（GUI 程序）→ 脱钩

备注:
  - -H 在 find 回退时无额外效果（find 默认包含隐藏文件）；
    -I 在 find 回退时被忽略并警告。
  - 映射表不支持行尾注释；值按空白拆分为命令与参数，
    不支持含空格的单个参数。
  - OPENER 与 -d 顺序无关；只允许一个位置参数 OPENER。
EOF
  exit 0
}

# 检测 dir 是否含隐藏组件（. 或 ./ 开头的相对路径不算）
# 返回 0 表示含隐藏组件，1 表示不含
dir_has_hidden_component() {
  local d="$1"
  while [[ "$d" == ./* || "$d" == ../* ]]; do
    if [[ "$d" == ./* ]]; then d="${d#./}"; else d="${d#../}"; fi
  done
  [[ -z "$d" || "$d" == "." || "$d" == ".." ]] && return 1
  [[ "$d" == .?* || "$d" == */.?* ]]
}

# ---------- 内置 TUI 名单 ----------
# 一个平铺列表, 多行排列仅为可读; 无分类语义, 增删随意
# 第一段: 合法打开器 —— 能裸传文件参数, 按常用度降序
# 第二段: 兜底程序 —— 不收文件参数, 仅为误配置时防脱钩卡死
TUI_LIST=(
  # ===== 合法打开器 =====
  nvim vim vi nano emacs micro less more
  helix hx kak vis amp jed joe mg ne ee
  most lv pg man info pinfo
  w3m lynx links elinks browsh
  vifm ranger mc nnn yazi lf
  # ===== 兜底 (不收文件参数) =====
  top htop btop iotop glances atop dstat watch powertop s-tui
  lazygit tig gitui
  fzf dialog whiptail ncdu
  ssh mosh telnet nmtui
  ncmpcpp cmus mocp alsamixer pulsemixer
  iftop nethogs iptraf wavemon vnstat
  weechat irssi mutt neomutt alot aerc
  psql mysql sqlite3 mongosh redis-cli pgcli mycli litecli usql
  python ipython node irb ghci lua R
  fff clifm
)

init_tui_set() {
  local p
  for p in "${TUI_LIST[@]}"; do
    TUI_SET["$p"]=1
  done
}

init_tui_set

# 判定单个打开器是否该前台运行; $1 = 命令名（不含前缀）
should_run_fg() {
  case "$run_mode" in
  fg) return 0 ;;
  detach) return 1 ;;
  esac
  # 内置 TUI 名单
  [[ -n "${TUI_SET[$1]:-}" ]] && return 0
  # 无显示服务器 → GUI 起不来, 前台更合理
  [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]] && return 0
  # 默认脱钩
  return 1
}

# ---------- 打开器映射表 ----------
lookup_opener_by_ext() {
  local ext="${1,,}" # 小写比较 (bash >= 4)
  [[ -f "$OPENER_CONF" ]] || return 0
  local line key val
  while IFS= read -r line; do
    [[ -z "${line//[[:space:]]/}" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    [[ "$ext" == "${key,,}" ]] && {
      echo "$val"
      return 0
    }
  done <"$OPENER_CONF"
  return 0
}

default_opener() {
  if have xdg-open; then
    echo xdg-open
  elif have open; then
    echo open
  else return 1; fi
}

resolve_opener_for() {
  local file="$1"
  if [[ -n "$opener" ]]; then
    echo "$opener"
    return 0
  fi
  # 只有当最后一个 '.' 出现在文件名主体中才视为扩展名（排除点文件）
  local base="${file##*/}"
  local ext="${base##*.}"
  if [[ "$ext" != "$base" && "$base" != .* ]]; then
    local mapped
    mapped="$(lookup_opener_by_ext "$ext" || true)"
    [[ -n "$mapped" ]] && {
      echo "$mapped"
      return 0
    }
  fi
  default_opener
}

# ---------- 候选列表（直接填充全局 finder_args） ----------
build_finder() {
  finder_args=()
  local fd_bin=""
  if have fd; then
    fd_bin=fd
  elif have fdfind; then
    fd_bin=fdfind
  fi
  if [[ -n "$fd_bin" ]]; then
    finder_args=("$fd_bin" --type f)
    ((include_hidden)) && finder_args+=(--hidden)
    ((no_ignore)) && finder_args+=(--no-ignore)
    finder_args+=(. "$dir")
  elif have find; then
    finder_args=(find "$dir" -type f)
    if ((include_hidden)); then
      : # find 默认包含隐藏文件
    elif dir_has_hidden_component "$dir"; then
      # dir 本身含隐藏组件: '*/.*' 会误伤 dir 下的所有文件,
      # 跳过过滤以避免"在 ~/.config 下什么都搜不到"
      warn "find 回退: dir 含隐藏组件, 已跳过隐藏过滤以避免漏掉文件"
    else
      finder_args+=(-not -path '*/.*')
    fi
    ((no_ignore)) && warn "find 回退: --no-ignore 无等效项, 该选项被忽略"
  else
    return 1
  fi
  return 0
}

# ---------- 内置预览（逐级检测工具） ----------
# 注意: 本函数仅拼装内置的硬编码命令, 不接受用户输入拼接,
# 因此不对 $viewer/$img/$pdf 做 shell 转义; 若将来支持自定义预览命令,
# 需改为 argv 数组或对每个分支 printf %q, 以免 case 脚本被破坏。
build_preview_cmd() {
  local viewer="head -n 300 {} 2>/dev/null"
  have bat && viewer="bat --color=always --line-range=:300 {}"

  local img=""
  have chafa && img="chafa {}"
  [[ -z "$img" ]] && have viu && img="viu {}"

  local pdf=""
  have pdftotext && pdf="pdftotext -l 1 {} - 2>/dev/null"

  if have file && [[ -n "$img$pdf" ]]; then
    local branches=""
    [[ -n "$img" ]] && branches+="image/*) $img ;; "
    [[ -n "$pdf" ]] && branches+="application/pdf) $pdf ;; "
    printf 'case $(file -b --mime-type {} 2>/dev/null) in %s*) %s ;; esac' \
      "$branches" "$viewer"
  else
    printf '%s' "$viewer"
  fi
}

# ---------- 运行打开器 ----------
# $@ = 完整命令（打开器 + 参数 + 文件）
# 映射表前缀: "!" = 前台, "&" = 脱钩; 其余按 should_run_fg 判定
# 前台: 阻塞至退出, 继承当前终端 (TUI 程序必需)
# 脱钩: setsid/disown + 全 fd 隔离, 终端关闭不影响 (GUI 程序)
run_opener() {
  local fg=0
  if [[ "$1" == "!" ]]; then
    fg=1
    shift
  elif [[ "$1" == "&" ]]; then
    shift
  else
    should_run_fg "$1" && fg=1
  fi
  local -a cmd=("$@")

  if ((dry_run)); then
    printf 'would run (%s): ' "$([[ $fg ]] && echo fg || echo detach)"
    printf '%q ' "${cmd[@]}"
    printf '\n'
    return 0
  fi

  if ((fg)); then
    local rc=0
    "${cmd[@]}" || rc=$?
    ((rc != 0)) && warn "打开器退出码 $rc: ${cmd[0]}"
    return 0
  fi

  if have setsid; then
    setsid -f "${cmd[@]}" </dev/null >/dev/null 2>&1 ||
      warn "打开器启动返回非零: ${cmd[0]}"
  else
    "${cmd[@]}" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
}

# ---------- 版本守卫（${var,,} / declare -A 需要 bash >= 4） ----------
[[ -n "${BASH_VERSINFO:-}" ]] || {
  echo "$PROG: 需要 bash 运行" >&2
  exit 1
}
((BASH_VERSINFO[0] >= 4)) || die "需要 bash >= 4 (当前 ${BASH_VERSION})"

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
  -d)
    [[ $# -ge 2 ]] || die "-d 需要参数"
    dir="$2"
    shift 2
    ;;
  -l | --last)
    use_last_dir=1
    shift
    ;;
  -m | --multi)
    multi=1
    shift
    ;;
  -p | --preview)
    preview=1
    shift
    ;;
  -H | --hidden)
    include_hidden=1
    shift
    ;;
  -I | --no-ignore)
    no_ignore=1
    shift
    ;;
  -t | --foreground)
    run_mode="fg"
    shift
    ;;
  -x | --detach)
    run_mode="detach"
    shift
    ;;
  -n | --dry-run)
    dry_run=1
    shift
    ;;
  -h | --help) usage ;;
  --)
    shift
    fzf_extra=("$@")
    break
    ;;
  -*) die "未知选项: $1" ;;
  *)
    if [[ -z "$opener" ]]; then
      opener="$1"
    else die "只允许一个位置参数 OPENER (多余: $1)"; fi
    shift
    ;;
  esac
done

# ---------- 目录确定（显式 -d 优先于 --last） ----------
if ((use_last_dir)); then
  if [[ -z "$dir" ]]; then
    [[ -s "$LAST_DIR_FILE" ]] || die "--last 指定但无上次目录记录"
    IFS= read -r dir <"$LAST_DIR_FILE" # 只取首行
  else
    warn "-d 与 --last 同时给出, 使用显式 -d"
  fi
fi
[[ -z "$dir" ]] && dir="."
[[ -d "$dir" ]] || die "目录不存在或不是目录: $dir"

# ---------- 依赖检查 ----------
have fzf || die "未找到 fzf"
build_finder || die "未找到 fd / fdfind / find, 无法生成候选列表"

# 记录本次目录（覆盖写, best-effort）
((dry_run)) || cache_write "$LAST_DIR_FILE" "$dir" overwrite

# ---------- fzf 参数 ----------
declare -a fzf_args=(fzf)
((multi)) && fzf_args+=(--multi)
if ((preview)); then
  fzf_args+=(--preview "$(build_preview_cmd)")
fi
((${#fzf_extra[@]} > 0)) && fzf_args+=("${fzf_extra[@]}") # 先计数再展开

# ---------- dry-run 打印 pipeline ----------
if ((dry_run)); then
  printf 'would run: '
  printf '%q ' "${finder_args[@]}"
  printf '| '
  printf '%q ' "${fzf_args[@]}"
  printf '\n'
fi

# ---------- 执行 fzf ----------
# 统一走临时文件, mapfile 不吃管道退出码; 捕获前关 pipefail 防 SIGPIPE 误杀
tmp_sel="$(mktemp "${TMPDIR:-/tmp}/fo.XXXXXX")" || die "无法创建临时文件 (TMPDIR=${TMPDIR:-/tmp})"
set +e
set +o pipefail
"${finder_args[@]}" | "${fzf_args[@]}" >"$tmp_sel"
fzf_status=$?
set -o pipefail
set -e
mapfile -t selected <"$tmp_sel"

# ---------- 退出码分类 ----------
if ((fzf_status == 1 || fzf_status == 130)); then
  exit 0 # 用户取消 / 无匹配: 正常结束
elif ((fzf_status != 0)); then
  die "fzf 异常退出 (码 $fzf_status)" # 真实故障: 显式报错
fi
((${#selected[@]} == 0)) && exit 0

# ---------- 校验显式打开器 ----------
if [[ -n "$opener" ]]; then
  have "$opener" || die "打开器不存在: $opener"
fi

# ---------- 逐个打开 ----------
for file in "${selected[@]}"; do
  [[ -n "$file" ]] || continue
  real_opener="$(resolve_opener_for "$file")" ||
    die "无法确定打开器(未指定 OPENER 且无默认可用)"
  [[ -n "$real_opener" ]] || die "无法确定打开器"

  # 拆分为 argv, 支持映射表带参数与前缀 ("!"=前台, "&"=脱钩)
  declare -a opener_cmd=()
  read -r -a opener_cmd <<<"$real_opener"

  # 剥掉前缀后校验命令存在
  local_probe="${opener_cmd[0]}"
  if [[ "$local_probe" == "!" || "$local_probe" == "&" ]]; then
    local_probe="${opener_cmd[1]:-}"
    [[ -n "$local_probe" ]] || die "映射表值缺少命令: $real_opener"
  fi
  have "$local_probe" || die "打开器不存在: $local_probe"

  ((dry_run)) || cache_write "$HISTORY_FILE" \
    "$(date '+%Y-%m-%d %H:%M:%S')"$'\t'"$real_opener"$'\t'"$file"
  run_opener "${opener_cmd[@]}" "$file"
done

exit 0

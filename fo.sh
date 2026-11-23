#!/usr/bin/env bash
#
# fo - fzf 选文件并用指定或默认程序打开
#
# 用法:
#   fo [OPENER] [-d DIR] [OPTIONS] [-- FZF_OPTIONS...]
#
# 示例:
#   fo okular
#   fo okular -d ~/.config
#   fo -d ~/.config
#   fo -- --multi --preview 'bat --color=always {}'
#
set -euo pipefail

# ---------- 常量 ----------
PROG="fo"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fo"
OPENER_CONF="$CONFIG_DIR/opener.conf"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/fo"
LAST_DIR_FILE="$CACHE_DIR/last_dir"
HISTORY_FILE="$CACHE_DIR/history.log"

# ---------- 默认值 ----------
dir=""
opener=""
use_last_dir=0
include_hidden=0
no_ignore=0
dry_run=0
multi=0
declare -a fzf_extra=()

# ---------- 辅助函数 ----------
die() {
  echo "$PROG: $*" >&2
  exit 1
}

usage() {
  cat <<EOF
用法: $PROG [OPENER] [-d DIR] [OPTIONS] [-- FZF_OPTIONS...]

选项:
  -d DIR          在 DIR 下搜索文件（默认当前目录）
  -l, --last      使用上次 -d 的目录
  -H, --hidden    包含隐藏文件（传给 fd）
  -I, --no-ignore 不遵循 .gitignore（传给 fd）
  -m, --multi     允许 fzf 多选
  -n, --dry-run   只打印将执行的命令，不实际打开
  -h, --help      显示本帮助
  --              其后参数原样传给 fzf

环境变量:
  FO_OPENER_CONF  自定义打开器映射配置文件路径
EOF
  exit 0
}

# 检查命令是否存在
have() { command -v "$1" >/dev/null 2>&1; }

# 解析打开器映射表: 输出与扩展名匹配的打开器，找不到则输出空
lookup_opener_by_ext() {
  local ext="$1" conf="${FO_OPENER_CONF:-$OPENER_CONF}"
  [[ -f "$conf" ]] || return 0
  local line key val
  while IFS= read -r line; do
    # 跳过空行和注释
    [[ -z "${line// /}" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    # 去空白
    key="$(echo "$key" | tr -d '[:space:]')"
    val="$(echo "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ "${ext,,}" == "${key,,}" ]]; then
      echo "$val"
      return 0
    fi
  done <"$conf"
  return 0
}

# 选择默认打开器（按平台）
default_opener() {
  if have xdg-open; then
    echo xdg-open
  elif have open; then
    echo open
  else
    return 1
  fi
}

# 生成候选列表命令（不执行，仅打印/构建）
build_finder_cmd() {
  local d="$1"
  local -a args=()
  if have fd; then
    args=(fd --type f)
    ((include_hidden)) && args+=(--hidden)
    ((no_ignore)) && args+=(--no-ignore)
    args+=(. "$d")
  elif have find; then
    args=(find "$d" -type f)
  else
    return 1
  fi
  printf '%s\0' "${args[@]}"
}

# 构建 fzf 命令
build_fzf_cmd() {
  local -a args=(fzf)
  ((multi)) && args+=(--multi)
  args+=("${fzf_extra[@]}")
  printf '%s\0' "${args[@]}"
}

# 脱钩运行
detach_run() {
  local -a cmd=("$@")
  if ((dry_run)); then
    printf 'would run: '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    return 0
  fi
  if have setsid; then
    setsid -f "${cmd[@]}" >/dev/null 2>&1
  else
    "${cmd[@]}" >/dev/null 2>&1 &
    disown
  fi
}

# 记录历史
log_history() {
  local opener="$1" file="$2"
  ((dry_run)) && return 0
  mkdir -p "$CACHE_DIR"
  printf '%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$opener" "$file" >>"$HISTORY_FILE"
}

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
  -H | --hidden)
    include_hidden=1
    shift
    ;;
  -I | --no-ignore)
    no_ignore=1
    shift
    ;;
  -m | --multi)
    multi=1
    shift
    ;;
  -n | --dry-run)
    dry_run=1
    shift
    ;;
  -h | --help)
    usage
    ;;
  --)
    shift
    fzf_extra=("$@")
    break
    ;;
  -*)
    die "未知选项: $1"
    ;;
  *)
    if [[ -z "$opener" ]]; then
      opener="$1"
    else
      die "多余的位置参数: $1"
    fi
    shift
    ;;
  esac
done

# ---------- 目录确定 ----------
if ((use_last_dir)); then
  if [[ -f "$LAST_DIR_FILE" ]]; then
    dir="$(<"$LAST_DIR_FILE")"
  else
    die "--last 指定但无上次目录记录"
  fi
fi

if [[ -z "$dir" ]]; then
  dir="."
fi

[[ -d "$dir" ]] || die "目录不存在或不是目录: $dir"

# 记录本次目录（供 --last 使用）
if ((! dry_run)); then
  mkdir -p "$CACHE_DIR"
  printf '%s\n' "$dir" >"$LAST_DIR_FILE"
fi

# ---------- 依赖检查 ----------
have fzf || die "未找到 fzf"
if ! have fd && ! have find; then
  die "未找到 fd 或 find，无法生成候选列表"
fi

# ---------- 生成候选并交给 fzf ----------
declare -a finder_args=()
# 通过 NUL 分隔安全读取
while IFS= read -r -d '' item; do
  finder_args+=("$item")
done < <(build_finder_cmd "$dir") || true

if [[ ${#finder_args[@]} -eq 0 ]]; then
  die "无法构建候选列表命令"
fi

declare -a fzf_args=()
while IFS= read -r -d '' item; do
  fzf_args+=("$item")
done < <(build_fzf_cmd) || true

if ((dry_run)); then
  printf 'would run: '
  printf '%q ' "${finder_args[@]}"
  printf '| '
  printf '%q ' "${fzf_args[@]}"
  printf '\n'
fi

# 执行 fzf，捕获输出（支持多行）
declare -a selected=()
if ((multi)); then
  mapfile -t selected < <("${finder_args[@]}" | "${fzf_args[@]}" || true)
else
  out="$("${finder_args[@]}" | "${fzf_args[@]}" || true)"
  [[ -n "$out" ]] && selected=("$out")
fi

# 用户取消
if [[ ${#selected[@]} -eq 0 ]]; then
  exit 0
fi

# ---------- 确定打开器 ----------
# 若未显式指定，先查扩展名映射，再回退默认
resolve_opener_for() {
  local file="$1"
  if [[ -n "$opener" ]]; then
    echo "$opener"
    return 0
  fi
  local ext="${file##*.}"
  if [[ "$ext" != "$file" ]]; then
    local mapped
    mapped="$(lookup_opener_by_ext "$ext")"
    if [[ -n "$mapped" ]]; then
      echo "$mapped"
      return 0
    fi
  fi
  default_opener
}

# 校验显式指定的打开器存在
if [[ -n "$opener" ]]; then
  have "$opener" || die "打开器不存在: $opener"
fi

# ---------- 逐个打开 ----------
for file in "${selected[@]}"; do
  [[ -n "$file" ]] || continue
  real_opener="$(resolve_opener_for "$file")" || die "无法确定打开器（未指定且无默认可用）"
  have "$real_opener" || die "打开器不存在: $real_opener"
  log_history "$real_opener" "$file"
  detach_run "$real_opener" "$file"
done

exit 0

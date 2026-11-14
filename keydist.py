#!/usr/bin/env python3
"""
keydist — 增量分发 secrets 到各消费端配置文件（.env / JSON / 整文件），
并执行 post 事件。

- TOML 仅作清单。secret 值由类型天然区分：裸字符串/数字 = 字面量
  （布尔渲染为 true/false）；表 = 引用。结构化写法不存在可拼错的 scheme
  前缀，引用写错必然硬报错（fail-closed）：
    { env = "VAR_NAME" }                  进程环境变量
    { envfile = "PATH", key = "KEY" }     dotenv 格式文件中的键（单键文件可省略 key）
    { file = "PATH" }                     整文件内容作为值（去末尾换行，适合私钥）
    { chain = [引用或字面量, ...] }        回退链：逐个尝试，首个成功者生效
    { concat = [引用或字面量, ...] }       拼接：全部元素依次连接，任一失败整体失败
    { pipe = { cmd = "CMD", input = ... } }
                                          外部命令处理：input（可省略，与 secret
                                          值同构）写入 stdin，stdout（去末尾换行）
                                          为值；命令须确定性且无副作用（dry-run
                                          下同样执行）
  任意引用表可加 transform = [op, ...]（闭集，按序应用）：
    trim / lower / upper / b64 / hex / sha256 / sha512 / urlenc
- 目标 format 决定写入形态（env / json / raw）：
    env（默认）  按 style 类别适配各消费端 .env 格式：shell / dotenv /
                systemd / docker
    json        键值合并进顶层 JSON object：现存文件必须是合法 object，
                否则该目标失败；幂等比较基于解析后相等（仅排版不同不算
                变化，不触发 post）；值恒写为 JSON 字符串
    raw         单个 secret 即整个文件内容，须显式 format = "raw"；值
                原样写出（UTF-8）、不追加换行
- 仅在文件内容实际变化时触发 post 事件
- 每次运行确保目标文件权限为 600
- 清单查找顺序：--config/-c 显式路径 > $XDG_CONFIG_HOME/keydist/keydist.toml
  （默认 ~/.config/keydist/keydist.toml）> ~/.local/etc/keydist.toml（旧默认）
  > /etc/keydist/keydist.toml
- --dry-run：打印将发生变化的 unified diff，不写文件、不执行 post
- 清单在处理任何目标之前整体校验：读取失败、TOML 语法错误、结构与类型
  错误一次性全部报告（fail-fast），绝不写到一半才失败
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse

DEFAULT_FORMAT = "env"
DEFAULT_STYLE = "shell"
FORMATS = ("env", "json", "raw")
REF_KINDS = ("env", "envfile", "file", "chain", "concat", "pipe")
_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPORT_PREFIX_RE = re.compile(r"^export\s+")  # dotenv 行首 export 前缀
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")    # 未加引号值的行内注释
TRANSFORM_OPS = ("trim", "lower", "upper", "b64", "hex", "sha256", "sha512", "urlenc")
# pipe secret 与 post 事件的外部命令统一超时（秒）
_SUBPROC_TIMEOUT = 30

# ---------------------------------------------------------------------------
# 终端着色：stdout/stderr 均为 TTY 且未设 NO_COLOR 时启用；纯 ANSI，零依赖
# ---------------------------------------------------------------------------

COLOR = (os.environ.get("NO_COLOR") is None
         and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
         and hasattr(sys.stderr, "isatty") and sys.stderr.isatty())


def _c(code: str, s: str) -> str:
    """按 ANSI 转义码包裹字符串；未启用着色（COLOR 为假）时原样返回。"""
    return f"\x1b[{code}m{s}\x1b[0m" if COLOR else s


def bold(s: str) -> str:
    """加粗文本（标题、目标名、清单路径）。"""
    return _c("1", s)


def red(s: str) -> str:
    """红色文本（错误信息、失败统计）。"""
    return _c("31", s)


def green(s: str) -> str:
    """绿色文本（成功状态、diff 新增行）。"""
    return _c("32", s)


def yellow(s: str) -> str:
    """黄色文本（警告、dry-run 提示、diff 删除行）。"""
    return _c("33", s)


def cyan(s: str) -> str:
    """青色文本（diff 头/post 标记等中性高亮）。"""
    return _c("36", s)


def dim(s: str) -> str:
    """暗淡文本（次要信息，如 style 来源、跳过原因）。"""
    return _c("2", s)

# ---------------------------------------------------------------------------
# secret 引用解析
# ---------------------------------------------------------------------------

def parse_env_text(text: str) -> dict[str, str]:
    """极简 dotenv 解析：支持 export 前缀、单双引号、转义、整行注释、空行；
    未加引号的值按 dotenv 惯例剥离行内注释（# 前须有空白）。"""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT_PREFIX_RE.sub("", line)
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            q, inner = val[0], val[1:-1]
            if q == '"':
                inner = (inner.replace("\\\\", "\x00").replace('\\"', '"')
                              .replace("\\n", "\n").replace("\\r", "\r")
                              .replace("\x00", "\\"))
            else:
                inner = inner.replace("'\\''", "'")
            val = inner
        else:
            # 未加引号的值剥离行内注释（# 前须有空白，避免误伤无空格的 #）
            val = _INLINE_COMMENT_RE.sub("", val)
        pairs[key] = val
    return pairs


def _resolve_env(name: object) -> tuple[str, None] | tuple[None, str]:
    """{ env = "NAME" }：进程环境变量。"""
    if not isinstance(name, str) or not _VAR_NAME_RE.fullmatch(name):
        return None, f"invalid env var name: {name!r}"
    v = os.environ.get(name)
    if v is None:
        return None, f"environment variable not set: {name}"
    return v, None


def _resolve_envfile(t: dict) -> tuple[str, None] | tuple[None, str]:
    """{ envfile = "PATH", key = "KEY" }：dotenv 文件中的键（单键文件可省略 key）。"""
    path_str = t["envfile"]
    if not isinstance(path_str, str) or not path_str:
        return None, f"envfile path must be a non-empty string: {path_str!r}"
    key = t.get("key")
    if key is not None and not isinstance(key, str):
        return None, f"envfile key must be a string: {key!r}"
    path = pathlib.Path(os.path.expanduser(path_str))
    if not path.exists():
        return None, f"envfile not found: {path}"
    try:
        pairs = parse_env_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        # 存在但不可读/是目录/非 UTF-8：转错误消息，而非裸 traceback
        return None, f"cannot read envfile {path}: {e}"
    if key is not None:
        if key not in pairs:
            return None, f"key {key!r} not found in {path}"
        return pairs[key], None
    if len(pairs) == 1:
        return next(iter(pairs.values())), None
    if not pairs:
        return None, f"envfile {path} contains no key=value pairs"
    return None, f"envfile {path} has {len(pairs)} keys; specify key = \"...\" explicitly"


def _resolve_file(t: dict) -> tuple[str, None] | tuple[None, str]:
    """{ file = "PATH" }：整文件内容（去末尾换行，适合私钥）。"""
    path_str = t["file"]
    if not isinstance(path_str, str) or not path_str:
        return None, f"file path must be a non-empty string: {path_str!r}"
    path = pathlib.Path(os.path.expanduser(path_str))
    if not path.exists():
        return None, f"file not found: {path}"
    try:
        return path.read_text(encoding="utf-8").rstrip("\n"), None
    except (OSError, UnicodeDecodeError) as e:
        return None, f"cannot read file {path}: {e}"


def _resolve_chain(items: object) -> tuple[str, None] | tuple[None, str]:
    """{ chain = [...] }：逐元素尝试，首个成功者生效。
    元素与 secret 值同构（字面量或引用表，允许嵌套 chain）。"""
    if not isinstance(items, list) or not items:
        return None, "chain must be a non-empty array"
    errs: list[str] = []
    for i, item in enumerate(items):
        v, e = resolve_secret(item)
        if v is not None:  # 互斥不变式：值非 None 即成功（Pyright 可收窄 v）
            return v, None
        errs.append(f"chain[{i}]: {e}")
    return None, "all fallbacks failed -> " + "; ".join(errs)


def _resolve_concat(items: object) -> tuple[str, None] | tuple[None, str]:
    """{ concat = [...] }：全部元素按序拼接。元素与 secret 值同构（字面量
    或引用表，允许嵌套 concat / chain）；任一元素失败则整体失败（fail-closed）。"""
    if not isinstance(items, list) or not items:
        return None, "concat must be a non-empty array"
    parts: list[str] = []
    for i, item in enumerate(items):
        v, e = resolve_secret(item)
        if v is None:  # 互斥不变式：值为 None 即失败（Pyright 可收窄 v）
            return None, f"concat[{i}]: {e}"
        parts.append(v)
    return "".join(parts), None


def _resolve_pipe(t: dict) -> tuple[str, None] | tuple[None, str]:
    """{ pipe = { cmd = "CMD", input = <引用或字面量>? } }：外部命令处理。
    input（可选）写入 stdin，stdout（去末尾换行）为值；非零退出即失败。
    命令须确定性且无副作用——dry-run 下同样执行（否则无法计算 diff）。"""
    spec = t["pipe"]
    if not isinstance(spec, dict):
        return None, f"pipe must be a table: {spec!r}"
    extra = set(spec) - {"cmd", "input"}
    if extra:
        return None, f"unknown key(s) in pipe: {', '.join(sorted(extra))}"
    cmd = spec.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return None, f"pipe requires a non-empty cmd: {cmd!r}"
    stdin_text: str | None = None
    if "input" in spec:
        v, e = resolve_secret(spec["input"])
        if v is None:
            return None, f"pipe input: {e}"
        stdin_text = v
    try:
        # input 恒传字符串：None 会让子进程继承父进程 stdin（可能挂在 TTY
        # 上）；显式 UTF-8 解码，输出不随运行环境 locale 漂移
        result = subprocess.run(
            shlex.split(cmd),
            input=stdin_text if stdin_text is not None else "",
            capture_output=True, encoding="utf-8",
            timeout=_SUBPROC_TIMEOUT, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, f"pipe cmd failed: {e}"
    if result.returncode != 0:
        return None, f"pipe cmd exited {result.returncode}: {result.stderr.strip()}"
    return result.stdout.rstrip("\n"), None


def _resolve_table(t: dict) -> tuple[str, None] | tuple[None, str]:
    """引用表：env / envfile(+key) / file / chain / concat / pipe 恰选其一，
    未知或冲突键一律报错；可选 transform 在解析成功后按序应用。"""
    present = [k for k in REF_KINDS if k in t]
    if not present:
        return None, "reference table must set one of: " + ", ".join(REF_KINDS)
    if len(present) > 1:
        return None, ("ambiguous reference: only one of "
                      + ", ".join(repr(k) for k in present) + " may be set")
    kind = present[0]
    allowed = {kind, "transform"} | ({"key"} if kind == "envfile" else set())
    extra = set(t) - allowed
    if extra:
        return None, f"unknown key(s) in reference: {', '.join(sorted(extra))}"
    if kind == "env":
        value, err = _resolve_env(t["env"])
    elif kind == "envfile":
        value, err = _resolve_envfile(t)
    elif kind == "file":
        value, err = _resolve_file(t)
    elif kind == "chain":
        value, err = _resolve_chain(t["chain"])
    elif kind == "concat":
        value, err = _resolve_concat(t["concat"])
    else:
        value, err = _resolve_pipe(t)
    if value is None:  # 互斥不变式：值失败时 err 必非 None，守卫仅为类型收窄
        if err is None:
            return None, f"{kind}: resolve failed without error"
        return None, err
    if "transform" in t:
        return apply_transform(value, t["transform"])
    return value, None


def resolve_secret(value: object) -> tuple[str, None] | tuple[None, str]:
    """裸字符串/数字 = 字面量（布尔渲染为 true/false），由类型天然区分、
    无前缀可拼错；表 = 引用。返回 (值, 错误)，两者互斥。"""
    if isinstance(value, str):
        return value, None
    if isinstance(value, bool):
        return ("true" if value else "false"), None
    if isinstance(value, (int, float)):
        return str(value), None
    if isinstance(value, dict):
        return _resolve_table(value)
    return None, (f"unsupported secret type: {type(value).__name__} "
                  "(expected literal or reference table)")


def apply_transform(value: str, ops: object) -> tuple[str, None] | tuple[None, str]:
    """对值按序应用 transform 白名单中的确定性变换；ops 必须为字符串数组。
    未知 op 一律硬报错并列出合法值——与引用键的 fail-closed 语义一致。"""
    if not isinstance(ops, list) or not all(isinstance(op, str) for op in ops):
        return None, f"transform must be an array of strings: {ops!r}"
    for op in ops:
        if op == "trim":
            value = value.strip()
        elif op == "lower":
            value = value.lower()
        elif op == "upper":
            value = value.upper()
        elif op == "b64":
            value = base64.b64encode(value.encode()).decode("ascii")
        elif op == "hex":
            value = value.encode().hex()
        elif op == "sha256":
            value = hashlib.sha256(value.encode()).hexdigest()
        elif op == "sha512":
            value = hashlib.sha512(value.encode()).hexdigest()
        elif op == "urlenc":
            value = urllib.parse.quote(value, safe="")
        else:
            return None, f"unknown transform op {op!r} (valid: {', '.join(TRANSFORM_OPS)})"
    return value, None

# ---------------------------------------------------------------------------
# 值格式化（各消费端 .env 解析语义不同，保持上一版设计）
# ---------------------------------------------------------------------------

def q_shell(v: str) -> str:
    """POSIX 单引号：内部纯字面量。兼容 shell source、node/python/Go/Ruby
    dotenv、systemd、compose——最大兼容超集，故作为默认类别。"""
    return "'" + v.replace("'", "'\\''") + "'"


def q_double(v: str) -> str:
    """双引号 + 反斜杠转义（\\n 展开为换行）。适用 node/python dotenv、systemd。"""
    out = v.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\r", "\\r").replace("\n", "\\n")
    return '"' + out + '"'


def q_docker(v: str) -> str:
    """docker run --env-file：不剥引号、无转义机制，只能裸值。"""
    return v


STYLES: dict[str, dict] = {
    "shell":   {"allow_export": True,  "quote": q_shell,  "desc": "POSIX shell / source"},
    "dotenv":  {"allow_export": True,  "quote": q_double, "desc": "node/python/Go/Ruby dotenv"},
    "systemd": {"allow_export": False, "quote": q_double, "desc": "systemd EnvironmentFile"},
    "docker":  {"allow_export": False, "quote": q_docker, "desc": "docker run --env-file"},
}


def validate_value(style: str, key: str, value: str) -> tuple[list[str], list[str]]:
    """按目标 style 校验值能否被对应消费端正确解析。
    返回 (错误列表, 警告列表)：错误表示必然解析失败（如 docker 不支持
    转义），警告表示工具间语义有歧义（如引号与 '#' 的处理），需人工确认。"""
    errors: list[str] = []
    warns: list[str] = []
    if style == "docker":
        if "\n" in value or "\r" in value:
            errors.append(f"{key}: docker env-file value must not contain newlines (no escaping)")
        if value != value.strip():
            errors.append(f"{key}: docker env-file value must not have leading/trailing whitespace")
        if '"' in value or "'" in value:
            warns.append(f"{key}: docker run keeps quotes literal while compose strips them; verify the consumer")
        if re.search(r"(?<!\S)#", value):
            warns.append(f"{key}: a standalone '#' in the value starts an inline comment under compose")
    elif style == "systemd" and ("\n" in value or "\r" in value):
        errors.append(f"{key}: systemd EnvironmentFile does not support multi-line values")
    return errors, warns


def format_env_line(key: str, value: str, style: str, prefix: str = "") -> str:
    """按 style 渲染一行 KEY=value；style 不允许 export 前缀时忽略传入的 prefix。"""
    st = STYLES[style]
    if not st["allow_export"]:
        prefix = ""
    return f"{prefix}{key}={st['quote'](value)}"

# ---------------------------------------------------------------------------
# format / style 解析（explicit > auto > default）
# ---------------------------------------------------------------------------

_STYLE_HINTS: list[tuple[str, str]] = [
    ("systemd", r"systemctl|systemd|\.service\b"),
    ("docker",  r"docker|compose|podman|container"),
    ("dotenv",  r"\bnode\b|npm|pnpm|yarn|pm2|next|nuxt|vite|jest"),
]


def resolve_style(name: str, target: dict) -> tuple[str, str]:
    """确定目标的 .env 格式：显式 style > 目标名/路径/post 关键词自动推断
    > 默认 shell。返回 (style, 来源)，来源为 "explicit" / "auto" / "default"；
    显式指定了未知 style 时抛 ValueError。"""
    explicit = target.get("style")
    if explicit:
        if explicit not in STYLES:
            raise ValueError(f"unknown style {explicit!r} (valid: {', '.join(STYLES)})")
        return explicit, "explicit"
    hay = " ".join([name, str(target.get("path", "")), str(target.get("post", ""))]).lower()
    for style, pattern in _STYLE_HINTS:
        if re.search(pattern, hay):
            return style, "auto"
    return DEFAULT_STYLE, "default"


def infer_format(path_str: str) -> str:
    """按路径后缀推断 format：.json → json，其余一律 env——只认强信号。
    raw 无稳定路径特征，永不推断，必须显式 format = "raw"。"""
    return "json" if path_str.endswith(".json") else DEFAULT_FORMAT


def resolve_format(target: dict) -> tuple[str, str]:
    """确定目标 format：显式 format > 路径后缀推断 > 默认 env。
    返回 (format, 来源)，来源为 "explicit" / "auto" / "default"；
    显式指定了未知 format 时抛 ValueError（与 resolve_style 同款纵深防御，
    清单预校验正常已在更早处拦截）。"""
    explicit = target.get("format")
    if explicit:
        if explicit not in FORMATS:
            raise ValueError(f"unknown format {explicit!r} (valid: {', '.join(FORMATS)})")
        return explicit, "explicit"
    if infer_format(str(target.get("path", ""))) == "json":
        return "json", "auto"
    return DEFAULT_FORMAT, "default"

# ---------------------------------------------------------------------------
# 目标文件渲染：env 增量更新（保持上一版设计，含重复键清理）/ json / raw
# ---------------------------------------------------------------------------

def render_env_file(path: pathlib.Path, updates: dict[str, str], style: str) -> tuple[str, str, list[str]]:
    """计算更新后的完整内容（不做任何写入，供 dry-run 复用）。
    返回 (原内容, 新内容, 警告)。现存文件按 UTF-8 读取，解码失败向上抛
    （调用方转为该目标失败，与全文件的显式编码约定一致）。"""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    replaced: set[str] = set()
    dropped: list[str] = []
    new_lines: list[str] = []

    # 每键预编译一次匹配 pattern，避免逐行重复构造正则
    key_patterns = [(key, re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}(?=\s*=)"))
                    for key in updates]
    for line in lines:
        hit = None
        for key, pattern in key_patterns:
            if pattern.match(line):
                hit = key
                break
        if hit is None:
            new_lines.append(line)
            continue
        key = hit
        if key in replaced:
            dropped.append(key)
            continue
        m = re.match(r"^(\s*(?:export\s+)?)", line)
        prefix = m.group(1) if m and STYLES[style]["allow_export"] else ""
        new_lines.append(format_env_line(key, updates[key], style, prefix))
        replaced.add(key)

    for key, value in updates.items():
        if key not in replaced:
            new_lines.append(format_env_line(key, value, style))

    new_content = "\n".join(new_lines) + ("\n" if new_lines else "")
    warns = []
    if dropped:
        warns.append("removed stale duplicate key(s): " + ", ".join(sorted(set(dropped))))
    return original, new_content, warns


class TargetDataError(Exception):
    """目标现存文件内容不合法（如非法 JSON、顶层非 object）：该目标整体
    失败、不写入。env 渲染可逐行容忍垃圾内容，json 不行——fail-closed 下
    唯一安全的动作就是不写。"""


def render_json_file(path: pathlib.Path,
                     updates: dict[str, str]) -> tuple[str, str, bool, list[str]]:
    """计算 JSON 目标更新后的完整内容（不做任何写入，供 dry-run 复用）。
    返回 (原内容, 新内容, 是否变化, 警告)。

    - 现存文件必须是合法 JSON object，否则抛 TargetDataError（整目标失败）
    - 是否变化基于解析后相等而非文本：仅排版不同（缩进/紧凑）不算变化，
      不重写、不触发 post——否则一次格式归一化会让 post 永远触发，与
      非确定性 pipe 同款的不收敛病
    - 值恒写为 JSON 字符串（工具的值模型是 str，非字符串原值被同名键
      覆盖）；顶层只合并一层键，不支持嵌套路径
    - dict 保序：已存在的键留在原位，新键追加在末尾，与 env 渲染器一致
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original.strip():
        try:
            doc = json.loads(original)
        except json.JSONDecodeError as e:
            raise TargetDataError(f"{path}: not valid JSON: {e}") from e
        if not isinstance(doc, dict):
            raise TargetDataError(f"{path}: top-level JSON value must be "
                                  f"an object, got {type(doc).__name__}")
    else:
        doc = {}
    new_doc = {**doc, **updates}
    changed = new_doc != doc
    new_content = json.dumps(new_doc, indent=2, ensure_ascii=False) + "\n"
    return original, new_content, changed, []


def render_raw_file(path: pathlib.Path,
                    value: str) -> tuple[str, str, bool, list[str]]:
    """计算 raw 目标更新后的完整内容（不做任何写入，供 dry-run 复用）：
    值即整个文件内容，逐字原样写出、不追加换行（需要尾随换行时由清单用
    concat 显式拼）。值模型是 str，以 UTF-8 写出——与工具整体的显式编码
    约定一致，不支持二进制。返回 (原内容, 新内容, 是否变化, 警告)。"""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    return original, value, value != original, []


def write_target_file(path: pathlib.Path, content: str) -> None:
    """以 0600 原子写入目标文件（UTF-8，适用于全部 format 的最终落盘）：
    先写同目录临时文件再 os.replace 替换。

    避免截断写入中途崩溃留下残缺的凭证文件；替换后的目标文件权限
    为 0600，也不存在“首次创建时短暂宽权限”的窗口。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def diff_text(path: pathlib.Path, old: str, new: str, indent: str = "    ") -> str:
    """两份内容的 unified diff（整体缩进、按行着色，无结尾换行的行补齐）。"""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{path} (current)", tofile=f"{path} (proposed)",
    )
    out = []
    for line in lines:
        body = indent + (line if line.endswith("\n") else line + "\n")
        if line.startswith(("---", "+++", "@@")):
            body = cyan(body)
        elif line.startswith("+"):
            body = green(body)
        elif line.startswith("-"):
            body = red(body)
        out.append(body)
    return "".join(out)

# ---------------------------------------------------------------------------
# 命令行与清单查找
# ---------------------------------------------------------------------------

MANIFEST_SEARCH_PATH: tuple[pathlib.Path, ...] = (
    pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    / "keydist" / "keydist.toml",
    pathlib.Path.home() / ".local/etc/keydist.toml",  # 旧默认路径，保留兼容
    pathlib.Path("/etc/keydist/keydist.toml"),
)


class ManifestError(Exception):
    """清单读取 / TOML 语法 / 结构校验失败：在处理任何目标之前统一报告。"""


def _validate_manifest(data: dict) -> list[str]:
    """清单结构校验（纯类型检查，不触碰文件系统、不解析 secret 值）。
    一次性收集全部问题后返回，而非遇到首个即停。"""
    errors: list[str] = []
    extra = set(data) - {"secrets", "targets"}
    if extra:
        errors.append("unknown top-level key(s): "
                      + ", ".join(sorted(repr(k) for k in extra))
                      + " (valid: secrets, targets)")
    secrets = data.get("secrets", {})
    if not isinstance(secrets, dict):
        errors.append(f"[secrets] must be a table, got {type(secrets).__name__}")
        secrets = {}
    else:
        for name, value in secrets.items():
            if not isinstance(value, (str, int, float, bool, dict)):
                errors.append(f"secrets.{name}: unsupported type "
                              f"{type(value).__name__} "
                              "(expected literal or reference table)")
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        errors.append(f"[targets] must be a table, got {type(targets).__name__}")
        return errors
    for name, target in targets.items():
        prefix = f"targets.{name}"
        if not isinstance(target, dict):
            errors.append(f"{prefix} must be a table, got {type(target).__name__}")
            continue
        for field in ("format", "style", "post", "secret"):
            if field in target and not isinstance(target[field], str):
                errors.append(f"{prefix}.{field} must be a string, "
                              f"got {type(target[field]).__name__}")
        path = target.get("path")
        if path is None:
            errors.append(f"{prefix}: missing required key 'path'")
        elif not isinstance(path, str) or not path:
            errors.append(f"{prefix}: path must be a non-empty string, "
                          f"got {type(path).__name__}")
        fmt = target.get("format")
        if isinstance(fmt, str) and fmt not in FORMATS:
            errors.append(f"{prefix}.format: unknown format {fmt!r} "
                          f"(valid: {', '.join(FORMATS)})")
        style = target.get("style")
        # 空串同样拒绝：静默回退 auto 推断违背 fail-closed
        if isinstance(style, str) and style not in STYLES:
            errors.append(f"{prefix}.style: unknown style {style!r} "
                          f"(valid: {', '.join(STYLES)})")
        # 交叉字段按生效 format（显式 > 后缀推断）校验；显式 format 非法时
        # 按推断继续，让其余字段的错误仍一次报全（与整体报告语义一致）
        eff = fmt if fmt in FORMATS else infer_format(
            path if isinstance(path, str) else "")
        if eff != "env" and "style" in target:
            errors.append(f"{prefix}: style only applies to format env, "
                          f"not {eff!r}")
        if eff == "raw":
            if "vars" in target:
                errors.append(f"{prefix}: raw takes 'secret', not 'vars'")
            if "secret" not in target:
                errors.append(f"{prefix}: raw requires 'secret'")
        elif "secret" in target:
            errors.append(f"{prefix}: 'secret' only applies to format raw, "
                          f"not {eff!r}")
        extra = set(target) - {"path", "format", "style", "post", "vars", "secret"}
        if extra:
            errors.append(f"{prefix}: unknown key(s): " + ", ".join(sorted(extra)))
        if eff == "raw":
            secret = target.get("secret")
            if isinstance(secret, str) and secret not in secrets:
                errors.append(f"{prefix}.secret: secret {secret!r} "
                              "not defined in [secrets]")
            continue  # raw 无 vars；后续 vars 校验不适用
        vars_ = target.get("vars", {})
        if not isinstance(vars_, dict):
            errors.append(f"{prefix}.vars must be a table, "
                          f"got {type(vars_).__name__}")
            continue
        for var, src in vars_.items():
            # env 键名受环境变量命名约束；json 键是任意 JSON 字符串，不检查
            if eff == "env" and (not isinstance(var, str)
                                 or not _VAR_NAME_RE.fullmatch(var)):
                errors.append(f"{prefix}.vars: invalid env var name {var!r}")
            if not isinstance(src, str):
                errors.append(f"{prefix}.vars.{var}: secret name must be a "
                              f"string, got {type(src).__name__}")
            elif src not in secrets:
                errors.append(f"{prefix}.vars.{var}: secret {src!r} "
                              "not defined in [secrets]")
    return errors


def load_manifest(path: pathlib.Path) -> dict:
    """读取并校验清单：文件不可读、TOML 语法错误与结构类型错误都在处理
    任何目标之前统一发现（fail-fast），避免写了一半才失败。校验失败抛
    ManifestError，消息一次性列出全部结构问题。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    except UnicodeDecodeError as e:
        # TOML 规定 UTF-8：解码发生在 read_text 内，必须在此捕获并归入
        # invalid TOML，而非裸 UnicodeDecodeError traceback
        raise ManifestError(f"invalid TOML in {path}: {e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"invalid TOML in {path}: {e}") from e
    problems = _validate_manifest(data)
    if problems:
        raise ManifestError(f"{len(problems)} manifest problem(s):\n  "
                            + "\n  ".join(problems))
    return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数：-c/--config 指定清单路径，-n/--dry-run 只预览变更。"""
    p = argparse.ArgumentParser(
        prog="keydist",
        description="把清单里的 secrets 增量分发到各消费端配置文件"
                    "（.env / JSON / 整文件）。",
    )
    p.add_argument("-c", "--config", metavar="PATH",
                   help="清单文件路径（默认按查找顺序取第一个存在的）")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="只打印将要发生的变更（unified diff），不写文件、不执行 post")
    return p.parse_args(argv)


def find_manifest(explicit: str | None) -> pathlib.Path:
    """--config 显式路径优先（必须存在）；否则取查找顺序中第一个存在的清单。"""
    if explicit:
        p = pathlib.Path(os.path.expanduser(explicit))
        if not p.exists():
            print(f"{red('error:')} manifest not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    for p in MANIFEST_SEARCH_PATH:
        if p.exists():
            return p
    searched = "\n  ".join(map(str, MANIFEST_SEARCH_PATH))
    print(f"{red('error:')} manifest not found; searched:\n  {searched}\n"
          "use --config PATH to specify one", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _run_post(post: str | None, changed: bool, dry_run: bool) -> str | None:
    """执行目标 post 事件：未配置（含纯空白，避免 shlex.split 得空命令列表）
    或内容未变化时跳过；dry-run 仅提示将运行。
    返回失败消息（命令启动失败或非零退出），成功或跳过返回 None。"""
    if not post or not post.strip():
        return None
    if not changed:
        print(dim("  post: skipped (no changes)"))
        return None
    if dry_run:
        print(f"  {cyan('post:')} {yellow('would run:')} {post}")
        return None
    print(f"  {cyan('post:')} {post}")
    try:
        result = subprocess.run(
            shlex.split(post), capture_output=True, encoding="utf-8",
            timeout=_SUBPROC_TIMEOUT, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        msg = f"post failed: {e}"
        print(f"  {red('error:')} {msg}", file=sys.stderr)
        return msg
    if result.returncode != 0:
        msg = f"post exited {result.returncode}: {result.stderr.strip()}"
        print(f"  {red('error:')} {msg}", file=sys.stderr)
        return msg
    print(f"  {cyan('post:')} {green('ok')}")
    return None


def _resolve_target_updates(target: dict, secrets: dict, style: str | None,
                            seen_values: dict[str, str],
                            ) -> tuple[dict[str, str], list[str], list[str]]:
    """解析目标的全部 vars 引用 → (updates, errors, warns)。

    清单预校验已保证 src 在 [secrets] 中有定义；运行期失败（env 未设、
    文件缺失、pipe 出错等）与 style 值校验问题都记入 errors，调用方须在
    errors 非空时放弃整个目标（fail-closed，绝不写入残缺值）。style 为
    None（env 之外的 format，如 json，没有引号方言）时跳过值校验。同一
    secret 被多处引用而两次解析结果不同（非确定性 pipe？）记入 warns。"""
    updates: dict[str, str] = {}
    errors: list[str] = []
    warns: list[str] = []
    for var, src in target.get("vars", {}).items():
        value, err = resolve_secret(secrets[src])
        if value is None:  # 互斥不变式：值为 None 即失败，此时 err 必有信息
            errors.append(f"secret {src!r} unresolved: {err}")
            continue
        if src not in seen_values:
            seen_values[src] = value
        elif seen_values[src] != value:
            warns.append(f"secret {src!r} resolved differently this run "
                         "(non-deterministic pipe?); such targets never "
                         "converge and post would fire on every run")
        if style is not None:  # 值校验是 env style 专属（引号/换行语义）
            errs, ws = validate_value(style, var, value)
            errors.extend(errs)
            warns.extend(ws)
        updates[var] = value
    return updates, errors, warns


def _process_target(name: str, target: dict, secrets: dict, dry_run: bool,
                    seen_values: dict[str, str]) -> tuple[str | None, list[str]]:
    """处理单个目标：解析 format（env 再解析 style）→ 解析全部 vars
    （raw 则解析单个 secret）并校验值 → 按 format 渲染写入 → 按需触发
    post。返回 (结果状态, 失败消息列表)：状态为 "updated" / "unchanged" /
    "would update"，目标失败时为 None。

    清单预校验已保证 path 非空、format/style 合法、vars/secret 引用的
    secret 有定义；运行期仍可能失败的是 secret 解析（env 未设、文件缺失、
    pipe 出错等）、值校验（仅 env style，如 docker 不支持多行值）、现存
    文件内容不合法（json，TargetDataError）与目标文件 IO（父目录创建
    失败、目标不可读或被目录占用、写替换失败）——一律转为目标失败而非
    traceback。"""
    path = pathlib.Path(os.path.expanduser(target["path"]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg = f"cannot create target directory {path.parent}: {e}"
        print(f"{red('error:')} {bold(name)}: {msg}", file=sys.stderr)
        return None, [msg]

    try:
        fmt, fmt_src = resolve_format(target)
    except ValueError as e:
        print(f"{red('error:')} {bold(name)}: {e}", file=sys.stderr)
        return None, [str(e)]
    style: str | None = None
    if fmt == "env":
        try:
            style, style_src = resolve_style(name, target)
        except ValueError as e:
            print(f"{red('error:')} {bold(name)}: {e}", file=sys.stderr)
            return None, [str(e)]
        print(bold(name) + dim(f" ({style}, {style_src})"))
    else:
        print(bold(name) + dim(f" ({fmt}, {fmt_src})"))

    errors: list[str] = []
    warns: list[str] = []
    updates: dict[str, str] = {}
    raw_value: str | None = None
    if fmt == "raw":
        src = target["secret"]
        value, err = resolve_secret(secrets[src])
        if value is None:  # 互斥不变式：值为 None 即失败，此时 err 必有信息
            errors.append(f"secret {src!r} unresolved: {err}")
        else:
            if src not in seen_values:
                seen_values[src] = value
            elif seen_values[src] != value:
                warns.append(f"secret {src!r} resolved differently this run "
                             "(non-deterministic pipe?); such targets never "
                             "converge and post would fire on every run")
            raw_value = value
    else:
        updates, errors, warns = _resolve_target_updates(
            target, secrets, style, seen_values)

    if errors:  # fail-closed：任一 secret 出错即放弃整个目标，绝不写入残缺值
        for e in errors:
            print(f"  {red('error:')} {e}", file=sys.stderr)
        return None, errors

    try:
        if fmt == "json":
            original, new_content, changed, render_warns = render_json_file(path, updates)
        elif fmt == "raw":
            # errors 为空保证 raw_value 已成功解析；断言仅为类型收窄
            assert raw_value is not None
            original, new_content, changed, render_warns = render_raw_file(path, raw_value)
        else:
            # fmt == "env" 时 style 必已解析成功；断言仅为类型收窄
            assert style is not None
            original, new_content, render_warns = render_env_file(path, updates, style)
            changed = new_content != original
        if dry_run:
            state = "would update" if changed else "unchanged"
        else:
            if changed:
                write_target_file(path, new_content)
            elif path.exists():
                path.chmod(0o600)  # 内容未变也确保权限为 0600
            state = "updated" if changed else "unchanged"
    except TargetDataError as e:
        # 现存目标文件内容不合法（非法 JSON / 顶层非 object）：不写入，
        # 整目标失败——env 可逐行容忍垃圾内容，json 不能
        msg = str(e)
        print(f"  {red('error:')} {msg}", file=sys.stderr)
        return None, [msg]
    except (OSError, UnicodeDecodeError) as e:
        # 目标不可读/是目录/写替换失败（权限、磁盘满）/现存文件非 UTF-8：
        # 统一转为目标失败计入汇总；write_target_file 是原子替换，失败时
        # 原文件保持完整，post 不会在残缺状态上触发
        msg = f"io error on {path}: {e}"
        print(f"  {red('error:')} {msg}", file=sys.stderr)
        return None, [msg]
    state_color = {"updated": green, "would update": yellow}.get(state, dim)
    print(f"  {state_color(f'{state:<14}')}{path}")  # 先对齐后着色，避免转义码破坏列宽
    if dry_run and changed:
        sys.stdout.write(diff_text(path, original, new_content))
    for w in warns + render_warns:
        print(f"  {yellow('warn:')} {w}", file=sys.stderr)

    post_error = _run_post(target.get("post"), changed, dry_run)
    return state, ([] if post_error is None else [post_error])


def _dispatch(args: argparse.Namespace) -> None:
    """加载并整体校验清单（读失败/语法/结构问题先于一切目标报告）→ 逐目标
    分发到 _process_target（format/style 解析、secret 解析与校验、按 format
    渲染写入、post 事件）→ 汇总结果。任一 secret 失败即放弃整个目标
    （fail-closed，绝不写入残缺值）；存在失败时以退出码 1 结束。"""
    master = find_manifest(args.config)

    try:
        mode = master.stat().st_mode & 0o777
    except OSError:
        pass  # 找到后又被移除等竞态：跳过权限检查，load_manifest 会统一报告
    else:
        if mode & 0o077:
            print(f"{yellow('warning:')} {master} is too permissive ({oct(mode)}); "
                  "it may contain secret references — chmod 600 recommended",
                  file=sys.stderr)

    try:
        data = load_manifest(master)
    except ManifestError as e:
        print(f"{red('error:')} {e}", file=sys.stderr)
        sys.exit(1)
    secrets = data.get("secrets", {})
    targets = data.get("targets", {})

    if not targets:
        print("warning: no targets defined", file=sys.stderr)
        return

    print(f"keydist: manifest {bold(str(master))} ({len(targets)} target(s))")
    if args.dry_run:
        print(yellow(bold("keydist: DRY-RUN — nothing will be written, no command executed")))

    failures: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    seen_values: dict[str, str] = {}  # secret 名 → 首次解析值；检测非确定性 pipe

    for i, (name, target) in enumerate(targets.items()):
        if i:
            print()  # 目标块之间空一行，便于扫读
        state, errors = _process_target(name, target, secrets,
                                        args.dry_run, seen_values)
        if state is not None:
            counts[state] = counts.get(state, 0) + 1
        failures.extend((name, e) for e in errors)

    states = ("would update", "unchanged") if args.dry_run else ("updated", "unchanged")
    tally = ", ".join(f"{counts.get(s, 0)} {s}" for s in states)
    verdict = "dry-run complete" if args.dry_run else "done"
    if failures:
        print(f"\n{red(f'{len(failures)} failure(s):')}", file=sys.stderr)
        for name, msg in failures:
            print(f"  - {bold(name)}: {msg}", file=sys.stderr)
        print(red(f"{verdict}: {len(targets)} target(s): {tally}; {len(failures)} failed"),
              file=sys.stderr)
        sys.exit(1)
    print(green(f"{verdict}: {len(targets)} target(s): {tally}"))


def main() -> None:
    """入口：分发到 _dispatch 并兜底信号类异常——Ctrl-C 以 130 退出，
    下游提前关闭管道（EPIPE）时静默以 1 退出（与 dly.py 行为一致）。"""
    args = parse_args()
    try:
        _dispatch(args)
    except BrokenPipeError:
        # 下游提前关闭管道（如 keydist -n | head）：把 stdout 指向 devnull，
        # 防止解释器退出刷新缓冲区时再次抛 BrokenPipeError；静默退出
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass  # stdout 无可用 fd（如被替换为 StringIO，io.UnsupportedOperation）
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)  # 惯例 128+SIGINT；终端已回显 ^C，无需额外输出


if __name__ == "__main__":
    main()

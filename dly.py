#!/usr/bin/env python3
"""dly - 每日笔记与任务笔记命令行工具。

完整的用法与选项说明见 _USAGE（模块加载时拼回 __doc__，保持单一来源）。"""

from __future__ import annotations

import calendar
import fnmatch
import os
import pydoc
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# ============================== 配置项 ==============================

# 普通笔记目录，同时也是 global.md 的存放目录
NOTES_DIR: Path = Path.home() / "Documents/notes/daily"

# 任务笔记目录
# None 表示任务笔记与普通笔记共用 NOTES_DIR
# 也可配置为独立目录，例如：Path.home() / "Documents/notes/tasks"
TASK_DIR: Path | None = None

# 编辑器命令，目标文件路径由脚本追加到参数末尾
EDITOR_CMD: list[str] = ["nvim"]

# 外部查看命令
# None 表示使用 Python 标准库 pydoc.pager 作为默认查看方案
# Linux/macOS 可配置：["less", "-R"] 或 ["more"]
# Windows 可配置：["more.com"] 或 ["cmd", "/c", "more"]
VIEWER_CMD: list[str] | None = None

# ============================== 日期解析 ==============================

# 偏移表达式：+1、-3d、+2w、+6m、-1y 等；单位可省略，缺省为天
_RELATIVE_RE = re.compile(r"^([+-])([0-9]+)([dwmy]?)$")


def _make_date(year: int, month: int, day: int, expr: str) -> date:
    """构造并校验日期；非法日期统一抛出 ValueError（“无效日期: <表达式>”）。"""
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError(f"无效日期: {expr}") from None


def _add_months(d: date, months: int) -> date:
    """按日历月加减，保持“日”；目标月不存在该日时截断到目标月最后一天。"""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    last_day = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(d.day, last_day))


def _add_years(d: date, years: int) -> date:
    """按日历年加减，保持月、日；非闰年的 2 月 29 日截断为 2 月 28 日。"""
    year = d.year + years
    if d.month == 2 and d.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, d.month, d.day)


def parse_date_expr(expr: str, today: date) -> date:
    """解析基准日期表达式并返回对应日期。

    匹配：空字符串（今天）、DD、MMDD、YYMMDD（解释为 20YY）、YYYYMMDD、
    YYYY-MM-DD、YYYY/MM/DD、YYYY.MM.DD。偏移（±N[dwmy]?）不是基准
    表达式，由 _apply_offset 在基准日期上逐个应用。无法解析或日期非法
    时抛出 ValueError，消息为“无效日期: <表达式>”。
    """
    if expr == "":
        return today

    # 数字模式仅接受 ASCII 数字（[0-9]，与 _RELATIVE_RE 的写法一致），
    # 拒绝全角数字等 Unicode 十进制字符
    # 1-2 位数字：DD，使用当前年和当前月
    if re.fullmatch(r"[0-9]{1,2}", expr):
        return _make_date(today.year, today.month, int(expr), expr)

    # 3-4 位数字：MMDD，左补零到 4 位，年份取当前年
    if re.fullmatch(r"[0-9]{3,4}", expr):
        mmdd = expr.zfill(4)
        return _make_date(today.year, int(mmdd[:2]), int(mmdd[2:]), expr)

    # 6 位数字：YYMMDD，解释为 20YYMMDD
    if re.fullmatch(r"[0-9]{6}", expr):
        year = 2000 + int(expr[:2])
        return _make_date(year, int(expr[2:4]), int(expr[4:]), expr)

    # 8 位数字：YYYYMMDD
    if re.fullmatch(r"[0-9]{8}", expr):
        return _make_date(int(expr[:4]), int(expr[4:6]), int(expr[6:]), expr)

    # 完整日期：三种分隔符（-、/、.），要求前后使用同一分隔符
    m = re.fullmatch(r"([0-9]{4})([-/.])([0-9]{2})\2([0-9]{2})", expr)
    if m is not None:
        return _make_date(int(m.group(1)), int(m.group(3)), int(m.group(4)), expr)

    raise ValueError(f"无效日期: {expr}")


def _apply_offset(d: date, expr: str) -> date:
    """在 d 上应用一个偏移表达式（±N[dwmy]?），返回新日期。

    月、年加减沿用日历截断规则：目标月不存在该日时截断到月末，
    非闰年的 2 月 29 日截断为 2 月 28 日；结果超出 date 表示范围时
    由底层抛出 ValueError/OverflowError。expr 不匹配偏移文法时按
    无效日期抛出 ValueError（_split_date_args 已先行校验，此为兜底）。
    """
    m = _RELATIVE_RE.match(expr)
    if m is None:
        raise ValueError(f"无效日期: {expr}")
    sign = -1 if m.group(1) == "-" else 1
    n = sign * int(m.group(2))
    unit = m.group(3)
    if unit in ("", "d"):
        return d + timedelta(days=n)
    if unit == "w":
        return d + timedelta(weeks=n)
    if unit == "m":
        return _add_months(d, n)
    return _add_years(d, n)  # unit == "y"


# ============================== 目录与路径生成 ==============================

# 全局普通笔记文件名
_GLOBAL_NOTE_NAME = "global.md"
# 全局任务笔记文件名
_GLOBAL_TASK_NAME = "global.task.md"


class NotesDirError(Exception):
    """笔记/任务目录不存在或不可读，消息形如“笔记目录不存在: <路径>”。"""


def _normal_dir() -> Path:
    """返回普通笔记目录。"""
    return NOTES_DIR


def _task_dir() -> Path:
    """返回任务笔记目录；TASK_DIR 为 None 时与普通目录共用。"""
    return TASK_DIR if TASK_DIR is not None else NOTES_DIR


def _date_stem(d: date) -> str:
    """生成完整四位年份的 YYYYMMDD 日期串。"""
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


def normal_note_path(d: date) -> Path:
    """返回普通日期笔记路径：普通目录/YYYYMMDD.md。"""
    return _normal_dir() / f"{_date_stem(d)}.md"


def task_note_path(d: date) -> Path:
    """返回日期任务笔记路径：任务目录/YYYYMMDD.task.md。"""
    return _task_dir() / f"{_date_stem(d)}.task.md"


def global_note_path() -> Path:
    """返回全局普通笔记路径：普通目录/global.md。"""
    return _normal_dir() / _GLOBAL_NOTE_NAME


def global_task_path() -> Path:
    """返回全局任务笔记路径：任务目录/global.task.md。"""
    return _task_dir() / _GLOBAL_TASK_NAME


# ============================== 列表 ==============================

# 普通日期笔记文件名：YYYYMMDD.md，捕获 8 位日期串
_DATE_NOTE_RE = re.compile(r"(\d{8})\.md")
# 日期任务笔记文件名：YYYYMMDD.task.md，捕获 8 位日期串
_DATE_TASK_RE = re.compile(r"(\d{8})\.task\.md")

# 视为 glob 通配符的字符；pattern 含其中之一时按 glob 匹配，否则按子串匹配
_GLOB_META_CHARS = "*?["


def _ensure_dir(dir_path: Path, label: str) -> None:
    """目录不存在时抛出 NotesDirError；label 为“笔记目录”或“任务目录”。"""
    if not dir_path.is_dir():
        raise NotesDirError(f"{label}不存在: {dir_path}")


def _valid_date_stem(stem: str) -> bool:
    """校验 8 位日期串是否为合法日期（如 20261301 非法）。"""
    try:
        date(int(stem[:4]), int(stem[4:6]), int(stem[6:]))
    except ValueError:
        return False
    return True


def _collect_names(dir_path: Path, kinds: set[str]) -> set[str]:
    """只扫描目录一次，收集符合命名规则且日期合法的笔记 basename。

    kinds 含 "normal" 时收集 global.md 与 YYYYMMDD.md；
    kinds 含 "task" 时收集 global.task.md 与 YYYYMMDD.task.md。
    只收集普通文件，不收集子目录；不符合命名规则的文件不收集。
    """
    global_names: set[str] = set()
    date_res: list[re.Pattern[str]] = []
    if "normal" in kinds:
        global_names.add(_GLOBAL_NOTE_NAME)
        date_res.append(_DATE_NOTE_RE)
    if "task" in kinds:
        global_names.add(_GLOBAL_TASK_NAME)
        date_res.append(_DATE_TASK_RE)

    names: set[str] = set()
    try:
        entries = list(dir_path.iterdir())
    except OSError as exc:
        # 目录存在但不可读（如权限 000）：转为 NotesDirError 走统一错误出口
        raise NotesDirError(f"目录不可读: {dir_path}: {exc}") from exc
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if name in global_names:
            names.add(name)
            continue
        for date_re in date_res:
            m = date_re.fullmatch(name)
            if m is not None and _valid_date_stem(m.group(1)):
                names.add(name)
                break
    return names


def _match_name(name: str, pattern: str) -> bool:
    """按 FR-5.3 筛选 basename：pattern 不含通配符时按 *pattern* 子串匹配。"""
    if any(ch in pattern for ch in _GLOB_META_CHARS):
        return fnmatch.fnmatchcase(name, pattern)
    return pattern in name


def list_notes(list_type: str, pattern: str) -> list[str]:
    """按类型列出并筛选笔记文件，返回按文件名字典序逆序排序的 basename 列表。

    list_type: "normal" 普通类型 / "task" 任务类型 / "all" 全部类型。
    pattern: glob 模式；不含 * ? [ 时自动转换为 *pattern* 子串匹配。
    所需目录不存在或不可读时抛出 NotesDirError；无匹配时返回空列表，
    由 main 输出“没有匹配的文件: <pattern>”并以退出码 1 结束。
    """
    normal_d = _normal_dir()
    task_d = _task_dir()
    if list_type == "normal":
        _ensure_dir(normal_d, "笔记目录")
        names = _collect_names(normal_d, {"normal"})
    elif list_type == "task":
        _ensure_dir(task_d, "任务目录")
        names = _collect_names(task_d, {"task"})
    elif list_type == "all":
        if normal_d == task_d:
            # 目录相同：只扫描一次，分别按两类规则收集，合并后自然去重
            _ensure_dir(normal_d, "笔记目录")
            names = _collect_names(normal_d, {"normal", "task"})
        else:
            # 目录不同：分别扫描两个目录后合并
            _ensure_dir(normal_d, "笔记目录")
            _ensure_dir(task_d, "任务目录")
            names = _collect_names(normal_d, {"normal"}) | _collect_names(task_d, {"task"})
    else:
        raise ValueError(f"未知列表类型: {list_type}")
    return sorted((n for n in names if _match_name(n, pattern)), reverse=True)


# ============================== 编辑 / 查看 ==============================


def _run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str | None]:
    """启动外部命令（继承 stdin/stdout/stderr），返回 (退出码, 启动失败原因)。

    启动失败（OSError）不抛异常，返回 (1, "CMD: 说明")，由调用方按场景
    补提示前缀（编辑器/查看器/子 shell）；正常结束返回 (子进程退出码, None)。
    """
    try:
        if cwd is None:
            result = subprocess.run(cmd)
        else:
            result = subprocess.run(cmd, cwd=cwd)
    except OSError as exc:
        return 1, f"{cmd}: {exc}"
    return result.returncode, None


def run_editor(path: Path) -> int:
    """使用 EDITOR_CMD 编辑目标文件，返回编辑器退出码。

    调用形式为 EDITOR_CMD + [str(path)]，继承 stdin/stdout/stderr；
    文件不存在不报错，是否创建交由编辑器决定。编辑器命令启动失败时
    向 stderr 输出错误信息，返回 1。
    """
    code, err = _run_cmd([*EDITOR_CMD, str(path)])
    if err is not None:
        print(f"编辑器启动失败: {err}", file=sys.stderr)
    return code


def view_file(path: Path) -> int:
    """查看目标文件内容，返回退出码。

    VIEWER_CMD 为 None 时读取文件内容（UTF-8，解码错误用 replace）后
    调用标准库 pydoc.pager；否则调用外部查看命令 VIEWER_CMD + [str(path)]，
    继承 stdin/stdout/stderr，其退出码作为返回值。查看命令启动失败时
    向 stderr 输出错误信息，返回 1。
    """
    if VIEWER_CMD is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"无法读取文件: {path}: {exc}", file=sys.stderr)
            return 1
        pydoc.pager(text)
        return 0
    code, err = _run_cmd([*VIEWER_CMD, str(path)])
    if err is not None:
        print(f"查看命令启动失败: {err}", file=sys.stderr)
    return code


# ============================== CLI 参数解析与入口 ==============================

# 选项与关键字。手写解析、不使用 argparse，避免 -1、+2w、2026-11-05 被误判为选项
_VIEW_FLAGS: tuple[str, ...] = ("-v", "--view")
_PRINT_FLAGS: tuple[str, ...] = ("-p", "--print")
_HELP_FLAGS: tuple[str, ...] = ("-h", "--help")
_TASK_WORDS: tuple[str, ...] = ("task", "t")
_GLOBAL_WORDS: tuple[str, ...] = ("global", "g")
_LIST_FLAGS: tuple[str, ...] = ("-l", "--list")
_ALL_FLAGS: tuple[str, ...] = ("-a", "--all")
# cd 子命令关键字
_CD_WORD: str = "cd"
# pattern 缺省值
_DEFAULT_PATTERN: str = "*"

# 用法说明，用法错误时随错误信息一起输出到 stderr；-h/--help 将其输出到 stdout
_USAGE = """\
用法：
    dly [打开选项] [日期表达式] [偏移…]
    dly task|t [打开选项] [日期表达式] [偏移…]
    dly global|g [打开选项]
    dly global|g task|t [打开选项]
    dly task|t global|g [打开选项]
    dly -l|--list [-a|--all] [pattern]
    dly task|t -l|--list [pattern]
    dly cd [task|t] [-p]
    dly task|t cd [-p]

打开选项：
    -v | --view     仅查看文件内容，不进入编辑
    -p | --print    输出真实文件路径，不编辑不查看
    -h | --help     显示用法说明后退出

cd 子命令：
    在笔记/任务目录中启动交互式子 shell，exit 退出后回到原目录；
    加 -p 不进入，仅输出目录路径，供 cd $(dly cd -p) 使用

日期表达式：
    （空）         今天
    DD             当前年、当前月的第 DD 日
    MMDD           当前年的指定月日
    YYMMDD         20YY 年的指定月日
    YYYYMMDD       完整日期
    YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD    完整日期

偏移参数（可多个组合，从左到右逐个应用；基准缺省为今天）：
    +N / -N[d]     ±N 天
    +Nw / -Nw      ±N 周
    +Nm / -Nm      ±N 个日历月（目标月不存在该日时截断到月末）
    +Ny / -Ny      ±N 个日历年（非闰年的 2 月 29 日截断为 2 月 28 日）

    例：dly 1101 +3m -2d   今年 11 月 1 日 3 个月后的前 2 天
        dly +3m -2d        今天 3 个月后的前 2 天
"""

# 模块 docstring 的用法段与 _USAGE 同源：加载时整体重建 __doc__，避免两处文案漂移
__doc__ = f"""dly - 每日笔记与任务笔记命令行工具。

{_USAGE}
当前状态：日期与偏移组合解析、路径生成、列表筛选、编辑器/查看器调用、写入权限检测、cd 子命令与 CLI 参数解析均已实现。
"""


class UsageError(Exception):
    """用法错误；main 捕获后输出错误信息与用法说明到 stderr，退出码 1。"""


class DateError(Exception):
    """无效日期，消息形如“无效日期: <表达式>”，退出码 1。"""


def _strip_open_flags(args: list[str]) -> tuple[list[str], bool, bool]:
    """按 PRD 5.2 剥离至多一个 -v/--view 与至多一个 -p/--print。

    两者可出现在任意位置；重复出现或同时使用按用法错误处理，
    返回 (剩余参数, 是否查看模式, 是否打印路径模式)。
    """
    rest: list[str] = []
    view = False
    print_path = False
    for arg in args:
        if arg in _VIEW_FLAGS:
            if view:
                raise UsageError("重复使用 -v/--view")
            view = True
        elif arg in _PRINT_FLAGS:
            if print_path:
                raise UsageError("重复使用 -p/--print")
            print_path = True
        else:
            rest.append(arg)
    if view and print_path:
        raise UsageError("-v/--view 与 -p/--print 不能同时使用")
    return rest, view, print_path


def _parse_keywords(rest: list[str]) -> tuple[bool, bool, list[str]]:
    """解析前导关键字 task|t 与 global|g（各最多出现一次，顺序不限）。

    task|t global|g 与 global|g task|t 两种顺序均识别为全局任务笔记，
    返回 (是否任务, 是否全局, 剩余参数)。
    """
    is_task = False
    is_global = False
    idx = 0
    for arg in rest[:2]:
        if arg in _TASK_WORDS and not is_task:
            is_task = True
            idx += 1
        elif arg in _GLOBAL_WORDS and not is_global:
            is_global = True
            idx += 1
        else:
            break
    return is_task, is_global, rest[idx:]


def _looks_like_date_expr(expr: str) -> bool:
    """判断表达式是否匹配某种基准日期格式（不校验日期值合法性）。

    用于区分“格式不符”（按用法错误处理）与“格式相符但日期非法”
    （按无效日期处理，如 20261301）；偏移文法由 _RELATIVE_RE 单独判断。
    """
    if re.fullmatch(r"[0-9]{1,2}|[0-9]{3,4}|[0-9]{6}|[0-9]{8}", expr) is not None:
        return True
    return re.fullmatch(r"[0-9]{4}[-/.][0-9]{2}[-/.][0-9]{2}", expr) is not None


def _split_date_args(tail: list[str]) -> tuple[str, list[str]]:
    """把位置参数拆分为 (基准日期表达式, 偏移参数列表) 并校验文法。

    首个参数是偏移（±N[dwmy]?）时基准缺省（空串 = 今天）且全部参数
    都必须是偏移；否则首个参数为基准日期表达式，其后只能是偏移。
    基准格式不符、或偏移位置出现其他参数时按用法错误抛出 UsageError。
    """
    if not tail:
        return "", []
    if _RELATIVE_RE.match(tail[0]) is not None:
        base_expr, offsets = "", tail
    else:
        if not _looks_like_date_expr(tail[0]):
            raise UsageError(f"未知参数: {tail[0]}")
        base_expr, offsets = tail[0], tail[1:]
    for expr in offsets:
        if _RELATIVE_RE.match(expr) is None:
            raise UsageError(f"未知参数: {expr}")
    return base_expr, offsets


def _resolve_date_args(tail: list[str]) -> date:
    """解析“[基准日期表达式] 偏移…”组合，返回最终日期。

    基准缺省为今天；偏移从左到右逐个在前一步结果上应用，每步独立
    按日历截断（如 0131 +1m 得 0228，再 -1d 得 0227）。基准格式相符
    但日期非法、或偏移把日期推出 date 表示范围时按无效日期抛出
    DateError，消息带上出错的那个表达式。
    """
    base_expr, offsets = _split_date_args(tail)
    try:
        d = parse_date_expr(base_expr, date.today())
    except (ValueError, OverflowError):
        raise DateError(f"无效日期: {base_expr}") from None
    for expr in offsets:
        try:
            d = _apply_offset(d, expr)
        except (ValueError, OverflowError):
            raise DateError(f"无效日期: {expr}") from None
    return d


def _dir_label(target: Path) -> str:
    """返回目标文件所属目录的提示标签：“笔记目录”或“任务目录”。

    仅当任务目录独立配置（TASK_DIR 非 None）且目标位于任务目录时才使用
    “任务目录”；两目录共用时一律视为“笔记目录”。
    """
    if TASK_DIR is not None and target.parent == _task_dir():
        return "任务目录"
    return "笔记目录"


def _dir_writable(dir_path: Path) -> bool:
    """判断目录是否可写：仅用 os.access 检查权限位。"""
    return os.access(dir_path, os.W_OK)


def _file_writable(path: Path) -> bool:
    """判断已存在文件是否可写：仅用 os.access 检查权限位。"""
    return os.access(path, os.W_OK)


def _writable_error(path: Path) -> str | None:
    """编辑前检测写入权限，返回错误提示；均可写时返回 None。

    编辑需要在笔记/任务目录创建或更新文件，因此要求所在目录可写；
    目标文件已存在时还要求文件本身可写，均以 os.access 检查权限位。
    """
    if not _dir_writable(path.parent):
        return f"{_dir_label(path)}不可写: {path.parent}"
    if path.is_file() and not _file_writable(path):
        return f"文件不可写: {path}"
    return None


def _open_note(path: Path, view: bool, print_path: bool) -> int:
    """编辑、查看或打印目标笔记路径，返回退出码。

    所在目录不存在时抛出 NotesDirError（编辑、查看与打印路径均要求
    目录存在）；打印路径模式仅输出真实路径、不要求文件已存在；
    查看模式要求文件已存在；编辑模式要求目标不是已存在的目录，并经
    _writable_error 检测写入权限，不可写时输出提示并返回 1。是否创建
    文件交由编辑器决定。
    """
    if not path.parent.is_dir():
        raise NotesDirError(f"{_dir_label(path)}不存在: {path.parent}")
    if print_path:
        print(path)
        return 0
    if view:
        if not path.is_file():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 1
        return view_file(path)
    if path.is_dir():
        print(f"无法编辑目录: {path}", file=sys.stderr)
        return 1
    error = _writable_error(path)
    if error is not None:
        print(error, file=sys.stderr)
        return 1
    return run_editor(path)


def _run_list(rest: list[str], base: str) -> int:
    """执行列表命令并输出结果，返回退出码。

    base 为 "normal"（dly -l / dly global -l）或 "task"（dly task -l）。
    """
    # 列表模式不允许 -v / --view 与 -p / --print
    for arg in rest:
        if arg in _VIEW_FLAGS:
            raise UsageError("列表模式不支持 -v/--view")
        if arg in _PRINT_FLAGS:
            raise UsageError("列表模式不支持 -p/--print")
    if base == "task":
        # 任务列表形式：dly task -l [pattern]，不支持 -a/--all
        if len(rest) > 1:
            raise UsageError(f"未知参数: {rest[1]}")
        if rest and rest[0] in _ALL_FLAGS:
            raise UsageError("任务列表不支持 -a/--all")
        if rest and rest[0].startswith("-"):
            raise UsageError(f"未知参数: {rest[0]}")
        list_type = "task"
        pattern = rest[0] if rest else _DEFAULT_PATTERN
    else:
        # 普通列表形式：dly -l [-a|--all] [pattern]
        pending = list(rest)
        all_types = False
        if pending and pending[0] in _ALL_FLAGS:
            all_types = True
            pending = pending[1:]
        if pending and pending[0] in _ALL_FLAGS:
            raise UsageError("重复使用 -a/--all")
        if len(pending) > 1:
            raise UsageError(f"未知参数: {pending[1]}")
        if pending and pending[0].startswith("-"):
            raise UsageError(f"未知参数: {pending[0]}")
        list_type = "all" if all_types else "normal"
        pattern = pending[0] if pending else _DEFAULT_PATTERN
    names = list_notes(list_type, pattern)
    if not names:
        print(f"没有匹配的文件: {pattern}", file=sys.stderr)
        return 1
    for name in names:
        print(name)
    return 0


def _parse_cd_args(rest: list[str], is_task: bool = False) -> tuple[bool, bool]:
    """解析 cd 子命令的剩余参数，返回 (是否任务目录, 是否仅输出路径)。

    is_task 供 dly task|t cd 形式传入（task 关键字已确定任务目录），
    此时再出现 task|t 按未知参数拒绝。允许至多一个 -p/--print；
    出现其他参数或重复参数时按用法错误处理。
    """
    print_path = False
    for arg in rest:
        if arg in _PRINT_FLAGS:
            if print_path:
                raise UsageError("重复使用 -p/--print")
            print_path = True
        elif arg in _TASK_WORDS and not is_task:
            is_task = True
        else:
            raise UsageError(f"未知参数: {arg}")
    return is_task, print_path


def _cd_shell_cmd() -> list[str]:
    """返回用于进入目录的交互式 shell 命令。

    SHELL/COMSPEC 须为单个可执行文件路径（不含参数）；未设置或为空串
    时回退到默认 shell。"""
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    return [os.environ.get("SHELL") or "/bin/sh"]


def _run_cd(is_task: bool, print_path: bool) -> int:
    """执行 cd 子命令，返回退出码。

    默认在目标目录启动交互式子 shell（chezmoi cd 风格）：父 shell 的
    工作目录不受影响，exit 退出子 shell 后回到原目录，退出码取自
    子 shell。print_path 为 True 时不进入，仅输出目录路径，供
    cd $(dly cd -p) 或 shell 包装函数使用。is_task 为 True 时目标为
    任务目录（TASK_DIR 为 None 时与笔记目录相同）；目录不存在时
    沿用 NotesDirError 提示。
    """
    target = _task_dir() if is_task else _normal_dir()
    label = "任务目录" if is_task and TASK_DIR is not None else "笔记目录"
    _ensure_dir(target, label)
    if print_path:
        print(target)
        return 0
    cmd = _cd_shell_cmd()
    code, err = _run_cmd(cmd, cwd=target)
    if err is not None:
        print(f"子 shell 启动失败: {err}", file=sys.stderr)
    return code


def _run(args: list[str]) -> int:
    """按 PRD 第 4、5 节解析参数并分发执行，返回退出码。"""
    # ---- 帮助选项：任意位置出现 -h/--help 即输出用法说明并以退出码 0 结束 ----
    if any(arg in _HELP_FLAGS for arg in args):
        print(_USAGE, end="")
        return 0
    # ---- 列表模式识别（优先于打开选项剥离；列表模式不允许 -v/-p）----
    if args and args[0] in _LIST_FLAGS:
        return _run_list(args[1:], "normal")
    if len(args) >= 2 and args[0] in _TASK_WORDS and args[1] in _LIST_FLAGS:
        return _run_list(args[2:], "task")
    if len(args) >= 2 and args[0] in _GLOBAL_WORDS and args[1] in _LIST_FLAGS:
        # dly global -l / dly g -l：等同普通列表形式
        return _run_list(args[2:], "normal")

    # ---- cd 子命令识别（优先于打开选项剥离；cd 自行解析 task|t 与 -p）----
    if args and args[0] == _CD_WORD:
        # dly cd [task|t] [-p]
        return _run_cd(*_parse_cd_args(args[1:]))
    if len(args) >= 2 and args[0] in _TASK_WORDS and args[1] == _CD_WORD:
        # dly task cd [-p] / dly t cd [-p]：task 关键字已确定任务目录
        return _run_cd(*_parse_cd_args(args[2:], is_task=True))

    # ---- 打开 / 查看 / 打印路径模式：剥离至多一个 -v 与 -p 后按形式解析 ----
    rest, view, print_path = _strip_open_flags(args)

    if not rest:
        # 空参数：普通今天
        return _open_note(normal_note_path(date.today()), view, print_path)

    # 前导关键字：task|t 与 global|g 顺序不限，统一解析
    is_task, is_global, tail = _parse_keywords(rest)

    if is_global:
        # global|g [task|t]：全局普通/任务笔记；global 之后不接受日期表达式与偏移
        if tail:
            raise UsageError(f"未知参数: {tail[0]}")
        return _open_note(global_task_path() if is_task else global_note_path(),
                          view, print_path)

    if is_task:
        # task|t [日期表达式] [偏移…]：任务今天、任务指定日期或再组合偏移
        return _open_note(task_note_path(_resolve_date_args(tail)), view, print_path)

    # 无关键字：[日期表达式] [偏移…]
    return _open_note(normal_note_path(_resolve_date_args(rest)), view, print_path)


def main(argv: list[str]) -> int:
    """解析命令行参数并分发执行，返回进程退出码。

    -h/--help 在任意位置出现时向 stdout 输出用法说明并返回 0；用法错误
    时向 stderr 输出错误信息与用法说明并返回 1；无效日期、目录不存在、
    文件不存在等错误仅输出对应错误信息并返回 1；Ctrl-C 返回 130，
    下游提前关闭管道（EPIPE）时静默返回 1。
    """
    try:
        return _run(list(argv))
    except UsageError as exc:
        print(exc, file=sys.stderr)
        print(_USAGE, file=sys.stderr, end="")
        return 1
    except (DateError, NotesDirError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except BrokenPipeError:
        # 下游提前关闭管道（如 dly -l | head）：把 stdout 指向 devnull，
        # 防止解释器退出刷新缓冲区时再次抛 BrokenPipeError；静默返回 1
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass  # stdout 无可用 fd（如被替换为 StringIO，io.UnsupportedOperation）
        return 1
    except KeyboardInterrupt:
        return 130  # 惯例 128+SIGINT；终端已回显 ^C，无需额外输出


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

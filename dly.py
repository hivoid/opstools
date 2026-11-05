#!/usr/bin/env python3
"""dly - 每日笔记与任务笔记命令行工具。

用法：
    dly [打开选项] [日期表达式]
    dly task|t [打开选项] [日期表达式]
    dly global|g [打开选项]
    dly global|g task|t [打开选项]
    dly -l|--list [-a|--all] [pattern]
    dly task|t -l|--list [pattern]

打开选项：
    -v | --view    仅查看文件内容，不进入编辑

日期表达式：
    （空）         今天
    +N / -N[d]     相对今天 ±N 天
    +Nw / -Nw      相对今天 ±N 周
    +Nm / -Nm      相对今天 ±N 个日历月（目标月不存在该日时截断到月末）
    +Ny / -Ny      相对今天 ±N 个日历年（非闰年的 2 月 29 日截断为 2 月 28 日）
    DD             当前年、当前月的第 DD 日
    MMDD           当前年的指定月日
    YYMMDD         20YY 年的指定月日
    YYYYMMDD       完整日期
    YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD    完整日期

当前状态：已实现日期解析、路径生成与列表筛选；CLI 参数解析与编辑/查看操作为待实现的骨架。
"""

from __future__ import annotations

import calendar
import fnmatch
import re
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

# 相对日期表达式：+1、-3d、+2w、+6m、-1y 等；单位可省略，缺省为天
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
    """解析日期表达式并返回对应日期。

    按 PRD 第 7 节顺序匹配：空字符串、相对日期（±N[dwmy]?）、DD、MMDD、
    YYMMDD（解释为 20YY）、YYYYMMDD、YYYY-MM-DD、YYYY/MM/DD、YYYY.MM.DD。
    无法解析或日期非法时抛出 ValueError，消息为“无效日期: <表达式>”。
    """
    if expr == "":
        return today

    # 相对日期：±N[dwmy]?；N=0 时按零偏移返回今天
    m = _RELATIVE_RE.match(expr)
    if m is not None:
        sign = -1 if m.group(1) == "-" else 1
        n = sign * int(m.group(2))
        unit = m.group(3)
        if unit in ("", "d"):
            return today + timedelta(days=n)
        if unit == "w":
            return today + timedelta(weeks=n)
        if unit == "m":
            return _add_months(today, n)
        return _add_years(today, n)  # unit == "y"

    # 1-2 位数字：DD，使用当前年和当前月
    if re.fullmatch(r"\d{1,2}", expr):
        return _make_date(today.year, today.month, int(expr), expr)

    # 3-4 位数字：MMDD，左补零到 4 位，年份取当前年
    if re.fullmatch(r"\d{3,4}", expr):
        mmdd = expr.zfill(4)
        return _make_date(today.year, int(mmdd[:2]), int(mmdd[2:]), expr)

    # 6 位数字：YYMMDD，解释为 20YYMMDD
    if re.fullmatch(r"\d{6}", expr):
        year = 2000 + int(expr[:2])
        return _make_date(year, int(expr[2:4]), int(expr[4:]), expr)

    # 8 位数字：YYYYMMDD
    if re.fullmatch(r"\d{8}", expr):
        return _make_date(int(expr[:4]), int(expr[4:6]), int(expr[6:]), expr)

    # 完整日期：三种分隔符（-、/、.），要求前后使用同一分隔符
    m = re.fullmatch(r"(\d{4})([-/.])(\d{2})\2(\d{2})", expr)
    if m is not None:
        return _make_date(int(m.group(1)), int(m.group(3)), int(m.group(4)), expr)

    raise ValueError(f"无效日期: {expr}")


# ============================== 目录与路径生成 ==============================

# 全局普通笔记文件名
_GLOBAL_NOTE_NAME = "global.md"
# 全局任务笔记文件名
_GLOBAL_TASK_NAME = "global.task.md"


class NotesDirError(Exception):
    """笔记目录或任务目录不存在，消息形如“笔记目录不存在: <路径>”。"""


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
    for entry in dir_path.iterdir():
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
    所需目录不存在时抛出 NotesDirError；无匹配时返回空列表，
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


def run_editor(path: Path) -> int:
    """使用 EDITOR_CMD 编辑目标文件，返回编辑器退出码。（待实现）"""
    raise NotImplementedError("run_editor 待实现")


def view_file(path: Path) -> int:
    """使用 VIEWER_CMD 或 pydoc.pager 查看目标文件内容，返回退出码。（待实现）"""
    raise NotImplementedError("view_file 待实现")


# ============================== 入口 ==============================


def main(argv: list[str]) -> int:
    """解析命令行参数并分发到对应功能，返回进程退出码。（待实现）"""
    raise NotImplementedError("main 待实现")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

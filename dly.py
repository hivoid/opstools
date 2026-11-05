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

当前状态：仅实现日期解析模块；CLI 参数解析与文件操作为待实现的骨架。
"""

from __future__ import annotations

import calendar
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


# ============================== 路径生成 ==============================


def normal_note_path(d: date) -> Path:
    """返回普通日期笔记路径：普通目录/YYYYMMDD.md。（待实现）"""
    raise NotImplementedError("normal_note_path 待实现")


def task_note_path(d: date) -> Path:
    """返回日期任务笔记路径：任务目录/YYYYMMDD.task.md。（待实现）"""
    raise NotImplementedError("task_note_path 待实现")


def global_note_path() -> Path:
    """返回全局普通笔记路径：普通目录/global.md。（待实现）"""
    raise NotImplementedError("global_note_path 待实现")


def global_task_path() -> Path:
    """返回全局任务笔记路径：任务目录/global.task.md。（待实现）"""
    raise NotImplementedError("global_task_path 待实现")


# ============================== 列表 / 编辑 / 查看 ==============================


def list_notes(list_type: str, pattern: str) -> list[str]:
    """列出并筛选笔记文件，返回按文件名字典序逆序排序的 basename 列表。（待实现）"""
    raise NotImplementedError("list_notes 待实现")


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

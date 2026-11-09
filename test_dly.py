#!/usr/bin/env python3
"""dly.py 的单元测试，覆盖 PRD 第 9 节验收用例。

运行方式：
    python3 -m unittest test_dly.py -v
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import dly

# 固定“今天”，避免测试依赖运行日期
TODAY = date(2026, 11, 5)

# 共用目录场景下各类型列表的期望输出（字典序逆序）
EXPECTED_NORMAL = ["global.md", "20261105.md", "20251231.md"]
EXPECTED_TASK = ["global.task.md", "20261105.task.md", "20261001.task.md"]
EXPECTED_ALL = [
    "global.task.md",
    "global.md",
    "20261105.task.md",
    "20261105.md",
    "20261001.task.md",
    "20251231.md",
]


class ParseDateExprTests(unittest.TestCase):
    """PRD 第 7 节：日期表达式解析。"""

    def test_empty_is_today(self):
        self.assertEqual(dly.parse_date_expr("", TODAY), TODAY)

    def test_offsets_are_not_base_expressions(self):
        # 偏移不再是独立日期表达式；基准缺省今天与偏移组合由 CLI 层解析
        for expr in ("+1", "-3d", "+2w", "+6m", "-1y", "+0", "-0"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_dd_uses_current_month(self):
        self.assertEqual(dly.parse_date_expr("5", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("05", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("13", TODAY), date(2026, 11, 13))

    def test_mmdd_left_padded(self):
        self.assertEqual(dly.parse_date_expr("1105", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("101", TODAY), date(2026, 1, 1))
        self.assertEqual(dly.parse_date_expr("0101", TODAY), date(2026, 1, 1))

    def test_yymmdd_is_20yy(self):
        self.assertEqual(dly.parse_date_expr("110312", TODAY), date(2011, 3, 12))
        self.assertEqual(dly.parse_date_expr("991231", TODAY), date(2099, 12, 31))

    def test_yyyymmdd_full_year(self):
        self.assertEqual(dly.parse_date_expr("20261105", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("19991231", TODAY), date(1999, 12, 31))
        self.assertEqual(dly.parse_date_expr("21000101", TODAY), date(2100, 1, 1))

    def test_full_date_separators(self):
        for expr in ("2026-11-05", "2026/11/05", "2026.11.05"):
            with self.subTest(expr=expr):
                self.assertEqual(dly.parse_date_expr(expr, TODAY), TODAY)

    def test_invalid_calendar_dates(self):
        for expr in ("20261301", "20260230", "20261131", "2026-13-01", "2026.00.10", "32"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_mixed_separators_rejected(self):
        for expr in ("2026-11/05", "2026/11-05", "2026.11-05"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_unrecognized_formats(self):
        for expr in ("n", "foo", "1e5", "2026-1-5", "26-11-05", "++1", "+", "-1x", "2D"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_error_message_contains_expr(self):
        with self.assertRaisesRegex(ValueError, "^无效日期: 20261301$"):
            dly.parse_date_expr("20261301", TODAY)


class _FixedDate(date):
    """today() 固定返回 TODAY 的 date 子类，供 patch dly.date 使用。"""

    @classmethod
    def today(cls) -> date:
        return TODAY


class FixedTodayMixin:
    """把 dly 模块内的 date 替换为 _FixedDate，使“今天”可确定断言。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(dly, "date", _FixedDate)
        patcher.start()
        self.addCleanup(patcher.stop)


class OffsetApplyTests(unittest.TestCase):
    """偏移表达式 _apply_offset：单位、日历截断与逐步语义。"""

    def test_days_weeks_and_zero(self):
        d = date(2026, 11, 5)
        self.assertEqual(dly._apply_offset(d, "+3"), date(2026, 11, 8))
        self.assertEqual(dly._apply_offset(d, "-3d"), date(2026, 11, 2))
        self.assertEqual(dly._apply_offset(d, "+2w"), date(2026, 11, 19))
        self.assertEqual(dly._apply_offset(d, "-1w"), date(2026, 10, 29))
        self.assertEqual(dly._apply_offset(d, "+0"), d)
        self.assertEqual(dly._apply_offset(d, "-0"), d)

    def test_months(self):
        self.assertEqual(dly._apply_offset(date(2026, 11, 5), "+1m"), date(2026, 12, 5))
        self.assertEqual(dly._apply_offset(date(2026, 11, 5), "-1m"), date(2026, 10, 5))
        self.assertEqual(dly._apply_offset(date(2026, 11, 5), "+2m"), date(2027, 1, 5))
        self.assertEqual(dly._apply_offset(date(2026, 11, 5), "-11m"), date(2025, 12, 5))

    def test_months_clamp_to_month_end(self):
        # 目标月不存在该日时截断到目标月最后一天
        self.assertEqual(dly._apply_offset(date(2026, 1, 31), "+1m"), date(2026, 2, 28))
        self.assertEqual(dly._apply_offset(date(2026, 3, 31), "+1m"), date(2026, 4, 30))
        self.assertEqual(dly._apply_offset(date(2026, 3, 31), "-1m"), date(2026, 2, 28))

    def test_years_leap_day(self):
        # 非闰年的 2 月 29 日截断为 2 月 28 日，闰年保持 2 月 29 日
        self.assertEqual(dly._apply_offset(date(2024, 2, 29), "+1y"), date(2025, 2, 28))
        self.assertEqual(dly._apply_offset(date(2024, 2, 29), "-1y"), date(2023, 2, 28))
        self.assertEqual(dly._apply_offset(date(2024, 2, 29), "+4y"), date(2028, 2, 29))

    def test_invalid_offset_raises(self):
        for expr in ("1", "1d", "+", "+1x", "-1D"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly._apply_offset(date(2026, 11, 5), expr)


class DateChainTests(FixedTodayMixin, unittest.TestCase):
    """“[基准日期表达式] 偏移…”组合解析：拆分、从左到右应用与错误分类。"""

    def test_empty_is_today(self):
        self.assertEqual(dly._resolve_date_args([]), TODAY)

    def test_base_only(self):
        self.assertEqual(dly._resolve_date_args(["1101"]), date(2026, 11, 1))

    def test_base_with_offsets(self):
        # 1101 +3m -2d：(2026-11-01 +3m → 2027-02-01) -2d = 2027-01-30
        self.assertEqual(dly._resolve_date_args(["1101", "+3m", "-2d"]),
                         date(2027, 1, 30))

    def test_offsets_only_base_defaults_to_today(self):
        # +3m -2d：今天(2026-11-05) 3 个月后的前 2 天
        self.assertEqual(dly._resolve_date_args(["+3m", "-2d"]), date(2027, 2, 3))

    def test_single_offset_is_today_plus_offset(self):
        # +1m：今天(2026-11-05) +1 个日历月 = 2026-12-05
        self.assertEqual(dly._resolve_date_args(["+1m"]), date(2026, 12, 5))

    def test_offsets_apply_left_to_right_with_per_step_clamp(self):
        # 每步先落在截断后的真实日期上，因此顺序不同结果不同
        self.assertEqual(dly._resolve_date_args(["0131", "+1m", "-1d"]), date(2026, 2, 27))
        self.assertEqual(dly._resolve_date_args(["0131", "-1d", "+1m"]), date(2026, 2, 28))

    def test_offsets_may_repeat_and_mix(self):
        self.assertEqual(dly._resolve_date_args(["1105", "+1m", "+1m"]), date(2027, 1, 5))
        self.assertEqual(dly._resolve_date_args(["+0", "-0", "+1d"]), date(2026, 11, 6))

    def test_second_non_offset_rejected(self):
        for tail in (["1101", "1105"], ["1101", "3d"], ["1101", "+1d", "extra"],
                     ["foo"], ["foo", "+1d"], ["+1m", "foo"]):
            with self.subTest(tail=tail):
                with self.assertRaisesRegex(dly.UsageError, "未知参数"):
                    dly._resolve_date_args(tail)

    def test_invalid_base_reports_base_expr(self):
        with self.assertRaisesRegex(dly.DateError, "^无效日期: 20261301$"):
            dly._resolve_date_args(["20261301", "+1d"])

    def test_offset_overflow_reports_offset_expr(self):
        with self.assertRaisesRegex(dly.DateError, "^无效日期: \\+1d$"):
            dly._resolve_date_args(["9999-12-31", "+1d"])

    def test_offset_underflow_reports_offset_expr(self):
        with self.assertRaisesRegex(dly.DateError, "^无效日期: -1d$"):
            dly._resolve_date_args(["0001-01-01", "-1d"])


class NotesDirTestBase(unittest.TestCase):
    """提供临时笔记目录、配置 patch 与 CLI 调用辅助的测试基类。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.notes_dir = self.base / "daily"
        self.tasks_dir = self.base / "tasks"
        self.notes_dir.mkdir()
        self._make_files(self.notes_dir)
        self._patch_dir("NOTES_DIR", self.notes_dir)
        self._patch_dir("TASK_DIR", None)

    def _patch_dir(self, name: str, path: Path | None) -> None:
        """测试期间替换 dly 模块级目录配置，结束后自动恢复。"""
        patcher = mock.patch.object(dly, name, path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *args: str) -> tuple[int, str, str]:
        """调用 dly.main 并捕获 stdout/stderr，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dly.main(list(args))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _make_files(dir_path: Path) -> None:
        """创建覆盖命名规则边界的文件集合。"""
        for name in (
            "global.md",
            "global.task.md",
            "20261105.md",
            "20261105.task.md",
            "20261001.task.md",
            "20251231.md",
            "20261301.md",   # 非法日期，不应列出
            "20261105.txt",  # 后缀不符，不应列出
        ):
            (dir_path / name).write_text("", encoding="utf-8")
        (dir_path / "subdir").mkdir()  # 子目录，不应列出


class PathTests(NotesDirTestBase):
    """PRD 第 3 节：路径生成（TASK_DIR 为 None 与独立目录两种情况）。"""

    def test_paths_shared_dir(self):
        # TASK_DIR 为 None 时任务路径与普通路径共用 NOTES_DIR
        d5 = date(2026, 11, 5)
        self.assertEqual(dly.normal_note_path(d5), self.notes_dir / "20261105.md")
        self.assertEqual(dly.task_note_path(d5), self.notes_dir / "20261105.task.md")
        self.assertEqual(dly.global_note_path(), self.notes_dir / "global.md")
        self.assertEqual(dly.global_task_path(), self.notes_dir / "global.task.md")

    def test_paths_separate_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # 仅生成路径，不要求目录存在
        d5 = date(2026, 11, 5)
        self.assertEqual(dly.normal_note_path(d5), self.notes_dir / "20261105.md")
        self.assertEqual(dly.task_note_path(d5), self.tasks_dir / "20261105.task.md")
        self.assertEqual(dly.global_note_path(), self.notes_dir / "global.md")
        self.assertEqual(dly.global_task_path(), self.tasks_dir / "global.task.md")

    def test_full_year_padding(self):
        self.assertEqual(dly.normal_note_path(date(999, 1, 2)).name, "09990102.md")
        self.assertEqual(dly.task_note_path(date(2100, 1, 1)).name, "21000101.task.md")

    def test_task_dir_defaults_to_notes_dir(self):
        self.assertEqual(dly._task_dir(), dly.NOTES_DIR)


class ListNotesTests(NotesDirTestBase):
    """PRD FR-5：list_notes 类型、筛选与错误。"""

    def test_normal_type(self):
        self.assertEqual(dly.list_notes("normal", "*"), EXPECTED_NORMAL)

    def test_task_type(self):
        self.assertEqual(dly.list_notes("task", "*"), EXPECTED_TASK)

    def test_all_type_dedup(self):
        self.assertEqual(dly.list_notes("all", "*"), EXPECTED_ALL)

    def test_substring_pattern(self):
        self.assertEqual(dly.list_notes("normal", "2026"), ["20261105.md"])
        self.assertEqual(dly.list_notes("task", ".task"), EXPECTED_TASK)

    def test_glob_pattern(self):
        self.assertEqual(dly.list_notes("task", "*01.task.md"), ["20261001.task.md"])
        self.assertEqual(dly.list_notes("all", "20261105.*"), ["20261105.task.md", "20261105.md"])

    def test_invalid_and_foreign_files_excluded(self):
        names = dly.list_notes("all", "*")
        self.assertNotIn("20261301.md", names)
        self.assertNotIn("20261105.txt", names)
        self.assertNotIn("subdir", names)

    def test_no_match_returns_empty(self):
        self.assertEqual(dly.list_notes("normal", "zzz"), [])

    def test_missing_notes_dir(self):
        self._patch_dir("NOTES_DIR", self.base / "missing-daily")
        with self.assertRaisesRegex(dly.NotesDirError, "笔记目录不存在"):
            dly.list_notes("normal", "*")

    def test_missing_task_dir_separate(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        with self.assertRaisesRegex(dly.NotesDirError, "任务目录不存在"):
            dly.list_notes("task", "*")

    def test_all_type_separate_dirs(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        (self.tasks_dir / "20260101.task.md").write_text("", encoding="utf-8")
        (self.tasks_dir / "global.md").write_text("", encoding="utf-8")  # 任务目录中的普通命名文件不算任务类型
        # 目录分离后：普通类型仅来自 notes_dir，任务类型仅来自 tasks_dir；
        # notes_dir 中的任务命名文件与 tasks_dir 中的 global.md 均不属于任何类型
        self.assertEqual(
            dly.list_notes("all", "*"),
            ["global.md", "20261105.md", "20260101.task.md", "20251231.md"],
        )


class RunEditorAndViewFileTests(NotesDirTestBase):
    """PRD FR-4 / FR-6：编辑器与查看器调用。"""

    def test_run_editor_invokes_cmd_with_path(self):
        with mock.patch.object(dly, "EDITOR_CMD", ["fake-editor"]):
            with mock.patch.object(dly, "subprocess") as sp:
                sp.run.return_value = mock.Mock(returncode=3)
                code = dly.run_editor(self.notes_dir / "a.md")
        self.assertEqual(code, 3)
        sp.run.assert_called_once_with(["fake-editor", str(self.notes_dir / "a.md")])

    def test_run_editor_start_failure(self):
        with mock.patch.object(dly, "EDITOR_CMD", ["dly-no-such-editor"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.run_editor(self.notes_dir / "a.md")
        self.assertEqual(code, 1)
        self.assertIn("编辑器启动失败", err.getvalue())

    def test_view_file_uses_pydoc_pager(self):
        target = self.notes_dir / "20261105.md"
        target.write_text("第一行\n", encoding="utf-8")
        with mock.patch.object(dly, "pydoc") as pydoc_mock:
            code = dly.view_file(target)
        self.assertEqual(code, 0)
        pydoc_mock.pager.assert_called_once_with("第一行\n")

    def test_view_file_replaces_invalid_utf8(self):
        target = self.notes_dir / "20261105.md"
        target.write_bytes(b"\xff\xfe tail")
        with mock.patch.object(dly, "pydoc") as pydoc_mock:
            code = dly.view_file(target)
        self.assertEqual(code, 0)
        self.assertIn("tail", pydoc_mock.pager.call_args.args[0])

    def test_view_file_external_cmd(self):
        target = self.notes_dir / "global.md"
        target.write_text("hello dly", encoding="utf-8")
        with mock.patch.object(dly, "VIEWER_CMD", ["cat"]):
            # 子进程继承真实的 fd 1，redirect_stdout 捕获不到；需在 fd 级别重定向
            with tempfile.TemporaryFile("w+") as tmp_out:
                saved_fd = os.dup(1)
                try:
                    os.dup2(tmp_out.fileno(), 1)
                    code = dly.view_file(target)
                finally:
                    os.dup2(saved_fd, 1)
                    os.close(saved_fd)
                    tmp_out.seek(0)
                    captured = tmp_out.read()
        self.assertEqual(code, 0)
        self.assertEqual(captured, "hello dly")

    def test_view_file_external_cmd_invokes_with_path(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["less", "-R"]):
            with mock.patch.object(dly, "subprocess") as sp:
                sp.run.return_value = mock.Mock(returncode=2)
                code = dly.view_file(self.notes_dir / "a.md")
        self.assertEqual(code, 2)
        sp.run.assert_called_once_with(["less", "-R", str(self.notes_dir / "a.md")])

    def test_view_file_external_start_failure(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["dly-no-such-viewer"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.view_file(self.notes_dir / "a.md")
        self.assertEqual(code, 1)
        self.assertIn("查看命令启动失败", err.getvalue())

    def test_view_file_external_exit_code_passthrough(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["cat"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.view_file(self.base / "no-such-file.md")
        self.assertEqual(code, 1)


class CliListTests(NotesDirTestBase):
    """PRD 4.2 / 9.2：列表模式 CLI。"""

    def test_list_normal(self):
        code, out, err = self._run("-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_NORMAL)
        self.assertEqual(err, "")

    def test_list_all_flag(self):
        for flag in ("-a", "--all"):
            with self.subTest(flag=flag):
                code, out, err = self._run("-l", flag)
                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines(), EXPECTED_ALL)

    def test_list_all_with_pattern(self):
        code, out, _ = self._run("-l", "--all", "2026")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.task.md", "20261105.md", "20261001.task.md"])

    def test_list_normal_with_pattern(self):
        code, out, _ = self._run("-l", "2026")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.md"])

    def test_task_list(self):
        code, out, _ = self._run("task", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_TASK)

    def test_task_list_short_flag_and_substring(self):
        code, out, _ = self._run("t", "-l", ".task")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_TASK)

    def test_task_list_glob(self):
        code, out, _ = self._run("task", "-l", "*01.task.md")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261001.task.md"])

    def test_global_list_is_normal_type(self):
        code, out, _ = self._run("global", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_NORMAL)

    def test_list_no_match(self):
        code, out, err = self._run("-l", "nomatch")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: nomatch", err)

    def test_list_all_no_match(self):
        code, _, err = self._run("-l", "-a", "zzz")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: zzz", err)

    def test_list_with_view_rejected(self):
        for args in (("-l", "-v"), ("-l", "--view"), ("task", "-l", "-v"), ("task", "-l", "p", "-v")):
            with self.subTest(args=args):
                code, _, _ = self._run(*args)
                self.assertEqual(code, 1)

    def test_task_list_rejects_all(self):
        code, _, err = self._run("task", "-l", "-a")
        self.assertEqual(code, 1)
        self.assertIn("-a", err)

    def test_duplicate_all_rejected(self):
        code, _, err = self._run("-l", "-a", "-a")
        self.assertEqual(code, 1)
        self.assertIn("-a", err)

    def test_task_list_separate_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        (self.tasks_dir / "20261105.task.md").write_text("", encoding="utf-8")
        code, out, _ = self._run("task", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.task.md"])

    def test_task_list_missing_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        code, _, err = self._run("task", "-l")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不存在: {self.tasks_dir}", err)


class CliOpenTests(NotesDirTestBase):
    """PRD 4.1 / 9.1：打开与查看模式的参数路由。"""

    def test_edit_today(self):
        expected = dly.normal_note_path(date.today())
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run()
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_edit_expr_routes_to_normal_note(self):
        # 仅基准表达式；偏移组合见 CliDateChainTests
        for expr in ("5", "1105", "110312", "19991231", "21000101", "2026-11-05",
                     "2026/11/05", "2026.11.05"):
            with self.subTest(expr=expr):
                expected = dly.normal_note_path(dly.parse_date_expr(expr, date.today()))
                with mock.patch.object(dly, "run_editor", return_value=0) as editor:
                    code, _, _ = self._run(expr)
                self.assertEqual(code, 0)
                editor.assert_called_once_with(expected)

    def test_edit_task_today_and_expr(self):
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("t")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(dly.task_note_path(date.today()))
        expected = dly.task_note_path(date(2026, 11, 5))
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("task", "2026-11-05")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_edit_global_normal_and_task_both_orders(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()  # 编辑模式同样要求任务目录存在（PRD 第 2 节）
        cases = (
            (("global",), self.notes_dir / "global.md"),
            (("g",), self.notes_dir / "global.md"),
            (("global", "task"), self.tasks_dir / "global.task.md"),
            (("g", "t"), self.tasks_dir / "global.task.md"),
            (("task", "global"), self.tasks_dir / "global.task.md"),
            (("t", "g"), self.tasks_dir / "global.task.md"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                with mock.patch.object(dly, "run_editor", return_value=0) as editor:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                editor.assert_called_once_with(expected)

    def test_view_routes_to_view_file(self):
        target = self.notes_dir / "20261105.md"  # _make_files 已创建
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("-v", "20261105")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_flag_any_position(self):
        target = self.notes_dir / "20261105.md"
        for args in (("-v", "2026-11-05"), ("2026-11-05", "-v")):
            with self.subTest(args=args):
                with mock.patch.object(dly, "view_file", return_value=0) as viewer:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                viewer.assert_called_once_with(target)

    def test_view_global(self):
        target = self.notes_dir / "global.md"
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("g", "-v")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_global_task_both_orders(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        target = self.tasks_dir / "global.task.md"
        target.write_text("", encoding="utf-8")
        for args in (("g", "t", "-v"), ("task", "global", "-v")):
            with self.subTest(args=args):
                with mock.patch.object(dly, "view_file", return_value=0) as viewer:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                viewer.assert_called_once_with(target)

    def test_view_task_date_separate_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        target = self.tasks_dir / "20261105.task.md"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("t", "-v", "2026-11-05")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_today_missing_file(self):
        target = dly.normal_note_path(date.today())
        target.unlink(missing_ok=True)  # 确保目标不存在，验证“文件不存在”路径
        code, _, err = self._run("-v")
        self.assertEqual(code, 1)
        self.assertIn(f"文件不存在: {target}", err)

    def test_view_task_today_missing_file(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        code, _, err = self._run("t", "-v")
        self.assertEqual(code, 1)
        self.assertIn("文件不存在", err)

    def test_editor_exit_code_passthrough(self):
        with mock.patch.object(dly, "run_editor", return_value=7):
            code, _, _ = self._run("global")
        self.assertEqual(code, 7)

    def test_open_missing_notes_dir(self):
        missing = self.base / "missing-daily"
        self._patch_dir("NOTES_DIR", missing)
        for args in ([], ["task"], ["global"], ["g", "t"]):
            with self.subTest(args=args):
                code, _, err = self._run(*args)
                self.assertEqual(code, 1)
                self.assertIn(f"笔记目录不存在: {missing}", err)

    def test_open_missing_task_dir_separate(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建；notes_dir 存在
        with mock.patch.object(dly, "run_editor") as editor:
            code, _, err = self._run("task")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不存在: {self.tasks_dir}", err)
        editor.assert_not_called()

    def test_open_global_task_missing_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        code, _, err = self._run("global", "task")
        self.assertEqual(code, 1)
        self.assertIn("任务目录不存在", err)


class CdTests(NotesDirTestBase):
    """cd 子命令：默认在目标目录启动子 shell；-v 仅输出目录路径。"""

    def test_cd_launches_shell_in_target_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        cases = (
            (("cd",), self.notes_dir),
            (("cd", "t"), self.tasks_dir),
            (("task", "cd"), self.tasks_dir),
        )
        for args, target in cases:
            with self.subTest(args=args):
                with mock.patch.dict(os.environ, {"SHELL": "/bin/dly-fake-shell"}):
                    with mock.patch.object(dly.subprocess, "run",
                                           return_value=mock.Mock(returncode=0)) as run:
                        code, out, err = self._run(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, "")
                self.assertEqual(err, "")
                run.assert_called_once_with(["/bin/dly-fake-shell"], cwd=target)

    def test_cd_shell_defaults_to_sh_without_shell_env(self):
        env = {k: v for k, v in os.environ.items() if k != "SHELL"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(dly.subprocess, "run",
                                   return_value=mock.Mock(returncode=0)) as run:
                code, _, _ = self._run("cd")
        self.assertEqual(code, 0)
        run.assert_called_once_with(["/bin/sh"], cwd=self.notes_dir)

    def test_cd_shell_uses_comspec_on_windows(self):
        with mock.patch.object(dly.os, "name", "nt"):
            with mock.patch.dict(os.environ, {"COMSPEC": "cmd.exe"}):
                with mock.patch.object(dly.subprocess, "run",
                                       return_value=mock.Mock(returncode=0)) as run:
                    code, _, _ = self._run("cd")
        self.assertEqual(code, 0)
        run.assert_called_once_with(["cmd.exe"], cwd=self.notes_dir)

    def test_cd_shell_exit_code_passthrough(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/dly-fake-shell"}):
            with mock.patch.object(dly.subprocess, "run",
                                   return_value=mock.Mock(returncode=5)):
                code, _, _ = self._run("cd")
        self.assertEqual(code, 5)

    def test_cd_shell_start_failure(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/dly-fake-shell"}):
            with mock.patch.object(dly.subprocess, "run",
                                   side_effect=OSError(2, "No such file")):
                code, _, err = self._run("cd")
        self.assertEqual(code, 1)
        self.assertIn("子 shell 启动失败", err)

    def test_cd_v_prints_notes_dir(self):
        for args in (("cd", "-v"), ("cd", "--view")):
            with self.subTest(args=args):
                code, out, err = self._run(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, f"{self.notes_dir}\n")
                self.assertEqual(err, "")

    def test_cd_v_task_forms_print_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        for args in (("cd", "t", "-v"), ("cd", "-v", "task"),
                     ("task", "cd", "-v"), ("t", "cd", "--view")):
            with self.subTest(args=args):
                code, out, _ = self._run(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, f"{self.tasks_dir}\n")

    def test_cd_v_shared_dir_falls_back_to_notes_dir(self):
        # TASK_DIR 为 None 时任务目录与笔记目录共用
        for args in (("cd", "t", "-v"), ("task", "cd", "-v")):
            with self.subTest(args=args):
                code, out, _ = self._run(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, f"{self.notes_dir}\n")

    def test_cd_v_missing_notes_dir(self):
        missing = self.base / "missing-daily"
        self._patch_dir("NOTES_DIR", missing)
        code, _, err = self._run("cd", "-v")
        self.assertEqual(code, 1)
        self.assertIn(f"笔记目录不存在: {missing}", err)

    def test_cd_v_missing_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        code, _, err = self._run("cd", "task", "-v")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不存在: {self.tasks_dir}", err)

    def test_cd_rejects_unknown_args(self):
        for args in (("cd", "global"), ("cd", "t", "extra"),
                     ("task", "cd", "extra"), ("cd", "-v", "-v"),
                     ("cd", "-v", "t", "t")):
            with self.subTest(args=args):
                code, _, err = self._run(*args)
                self.assertEqual(code, 1)
                self.assertIn("用法", err)


class CliDateChainTests(FixedTodayMixin, NotesDirTestBase):
    """组合偏移的 CLI 路由：打开/查看/任务形式、global 拒绝与列表边界。"""

    def test_edit_base_with_offsets(self):
        # 1101 +3m -2d：(2026-11-01 +3m → 2027-02-01) -2d = 2027-01-30
        expected = self.notes_dir / "20270130.md"
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("1101", "+3m", "-2d")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_edit_offsets_only(self):
        # +3m -2d：今天(2026-11-05) 3 个月后的前 2 天
        expected = self.notes_dir / "20270203.md"
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("+3m", "-2d")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_task_base_with_offsets(self):
        # t 1101 +1m：2026-11-01 +1m = 2026-12-01
        expected = self.notes_dir / "20261201.task.md"
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("t", "1101", "+1m")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_view_base_with_offsets(self):
        target = self.notes_dir / "20270130.md"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("-v", "1101", "+3m", "-2d")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_global_rejects_offsets(self):
        code, _, err = self._run("global", "+3m")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: +3m", err)

    def test_list_mode_treats_offset_as_pattern(self):
        # 列表模式不解析偏移：+1m 仅是文件名 pattern
        code, _, err = self._run("-l", "+1m")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: +1m", err)

    def test_task_list_treats_offset_as_pattern(self):
        code, _, err = self._run("task", "-l", "+1m")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: +1m", err)

    def test_edit_invalid_base_with_offset(self):
        code, _, err = self._run("20261301", "+1d")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: 20261301", err)
        self.assertNotIn("用法", err)

    def test_edit_offset_overflow(self):
        code, _, err = self._run("9999-12-31", "+1d")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: +1d", err)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                 "root 用户不受文件权限限制")
class WritePermissionTests(NotesDirTestBase):
    """编辑模式的写入权限检测（NOTES_DIR / TASK_DIR 与目标文件）。"""

    def _make_readonly(self, path: Path) -> None:
        """将路径设为只读，测试结束后自动恢复权限以便清理临时目录。"""
        path.chmod(0o500 if path.is_dir() else 0o400)
        self.addCleanup(path.chmod, 0o700 if path.is_dir() else 0o600)

    def test_writable_error_returns_none_when_writable(self):
        self.assertIsNone(dly._writable_error(self.notes_dir / "20261105.md"))
        self.assertIsNone(dly._writable_error(self.notes_dir / "not-exist.md"))

    def test_writable_error_readonly_notes_dir(self):
        self._make_readonly(self.notes_dir)
        self.assertEqual(
            dly._writable_error(self.notes_dir / "20261105.md"),
            f"笔记目录不可写: {self.notes_dir}",
        )

    def test_writable_error_readonly_existing_file(self):
        target = self.notes_dir / "20261105.md"
        self._make_readonly(target)
        self.assertEqual(dly._writable_error(target), f"文件不可写: {target}")

    def test_dir_writable_true_when_writable(self):
        self.assertTrue(dly._dir_writable(self.notes_dir))

    def test_dir_writable_false_when_access_denied(self):
        # 模拟 os.access 判定目录不可写
        with mock.patch.object(dly.os, "access", return_value=False):
            self.assertFalse(dly._dir_writable(self.notes_dir))

    def test_file_writable_false_when_access_denied(self):
        # 模拟 os.access 判定文件不可写
        target = self.notes_dir / "20261105.md"
        with mock.patch.object(dly.os, "access", return_value=False):
            self.assertFalse(dly._file_writable(target))

    def test_edit_readonly_notes_dir(self):
        self._make_readonly(self.notes_dir)
        with mock.patch.object(dly, "run_editor") as editor:
            code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn(f"笔记目录不可写: {self.notes_dir}", err)
        editor.assert_not_called()

    def test_edit_readonly_existing_file(self):
        target = self.notes_dir / "20261105.md"
        self._make_readonly(target)
        with mock.patch.object(dly, "run_editor") as editor:
            code, _, err = self._run("20261105")
        self.assertEqual(code, 1)
        self.assertIn(f"文件不可写: {target}", err)
        editor.assert_not_called()

    def test_edit_readonly_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        self._make_readonly(self.tasks_dir)
        with mock.patch.object(dly, "run_editor") as editor:
            code, _, err = self._run("task")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不可写: {self.tasks_dir}", err)
        editor.assert_not_called()

    def test_view_readonly_file_allowed(self):
        # 查看模式只读，不应受写入权限影响
        target = self.notes_dir / "20261105.md"
        self._make_readonly(target)
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("-v", "20261105")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)


class HelpTests(NotesDirTestBase):
    """-h/--help：任意位置出现即输出用法说明并以退出码 0 结束。"""

    def setUp(self) -> None:
        super().setUp()
        # 全类替换 run_editor，既隔离外部编辑器，也便于断言未被调用
        patcher = mock.patch.object(dly, "run_editor", return_value=0)
        self.editor = patcher.start()
        self.addCleanup(patcher.stop)

    def test_help_prints_usage_to_stdout(self):
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                code, out, err = self._run(flag)
                self.assertEqual(code, 0)
                self.assertEqual(out, dly._USAGE)
                self.assertEqual(err, "")
                self.editor.assert_not_called()

    def test_help_any_position_wins(self):
        # -h/--help 优先于所有模式识别与用法校验（含重复 -v 等）
        for args in (("-l", "-h"), ("cd", "-h"), ("task", "-h"),
                     ("-v", "20261105", "-h"), ("1101", "+3m", "--help"),
                     ("-v", "-v", "-h")):
            with self.subTest(args=args):
                code, out, err = self._run(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, dly._USAGE)
                self.assertEqual(err, "")
                self.editor.assert_not_called()

    def test_help_works_without_notes_dir(self):
        # 帮助不依赖目录存在：目录缺失时 -h 依旧输出用法说明
        self._patch_dir("NOTES_DIR", self.base / "missing-daily")
        code, out, err = self._run("-h")
        self.assertEqual(code, 0)
        self.assertEqual(out, dly._USAGE)
        self.assertEqual(err, "")


class CliErrorTests(NotesDirTestBase):
    """PRD 9.3：CLI 用法错误用例。"""

    def test_unknown_arg_n(self):
        code, _, err = self._run("n")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: n", err)
        self.assertIn("用法", err)

    def test_unknown_short_task_flag(self):
        code, _, err = self._run("-t")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: -t", err)

    def test_unknown_long_task_flag(self):
        code, _, err = self._run("--task")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: --task", err)

    def test_unknown_type_flag(self):
        code, _, err = self._run("--type", "task")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: --type", err)

    def test_global_with_extra_arg(self):
        code, _, err = self._run("global", "foo")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: foo", err)

    def test_duplicate_view_flag(self):
        code, _, err = self._run("-v", "-v")
        self.assertEqual(code, 1)
        self.assertIn("-v", err)

    def test_invalid_date_cli(self):
        code, _, err = self._run("20261301")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: 20261301", err)
        self.assertNotIn("用法", err)  # 无效日期只输出错误信息，不带用法说明

    def test_invalid_date_in_task(self):
        code, _, err = self._run("task", "20261302")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: 20261302", err)

    def test_date_then_extra_arg(self):
        code, _, err = self._run("2026-11-05", "extra")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: extra", err)

    def test_usage_error_prints_usage(self):
        code, _, err = self._run("-v", "-v")
        self.assertEqual(code, 1)
        self.assertIn("用法：", err)
        self.assertIn("dly task|t -l|--list [pattern]", err)


if __name__ == "__main__":
    unittest.main()

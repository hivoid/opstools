#!/usr/bin/env python3
"""keydist 单元测试。

覆盖面：
- parse_env_text：极简 dotenv 解析（export 前缀、引号、转义、注释）
- secret 引用：env / envfile / file / chain / concat / pipe 与字面量
- transform 闭集与 fail-closed 报错
- 各 style 的引号、校验（错误 vs 警告）与行格式化
- style 解析优先级（explicit > auto > default）
- render_env_file 增量更新（替换、追加、重复键清理、前缀保留）
- write_env_file 权限、diff_text、find_manifest、parse_args
- main() 端到端：dry-run、post 触发/跳过、fail-closed 与退出码
"""

import io
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import keydist


# ---------------------------------------------------------------------------
# 公共基类
# ---------------------------------------------------------------------------

class TempDirBase(unittest.TestCase):
    """提供临时目录并在测试结束后清理的基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

# ---------------------------------------------------------------------------
# 着色
# ---------------------------------------------------------------------------

class ColorTests(unittest.TestCase):
    """COLOR 开关：为假时原样返回；为真时包裹对应 ANSI 码。"""

    def test_disabled_returns_plain_text(self):
        with mock.patch.object(keydist, "COLOR", False):
            for fn in (keydist.bold, keydist.red, keydist.green,
                       keydist.yellow, keydist.cyan, keydist.dim):
                self.assertEqual(fn("x"), "x")

    def test_enabled_wraps_ansi_codes(self):
        expected = {
            keydist.bold: "\x1b[1m", keydist.red: "\x1b[31m",
            keydist.green: "\x1b[32m", keydist.yellow: "\x1b[33m",
            keydist.cyan: "\x1b[36m", keydist.dim: "\x1b[2m",
        }
        with mock.patch.object(keydist, "COLOR", True):
            for fn, code in expected.items():
                self.assertEqual(fn("x"), f"{code}x\x1b[0m")

# ---------------------------------------------------------------------------
# dotenv 解析
# ---------------------------------------------------------------------------

class ParseEnvTextTests(unittest.TestCase):
    """parse_env_text：export 前缀、引号、转义、注释与空行。"""

    def test_basic_pairs(self):
        self.assertEqual(keydist.parse_env_text("A=1\nB=2"), {"A": "1", "B": "2"})

    def test_export_prefix_stripped(self):
        self.assertEqual(keydist.parse_env_text("export A=1"), {"A": "1"})

    def test_comments_and_blank_lines_ignored(self):
        text = "# comment\n\n   # indented comment\nA=1\n"
        self.assertEqual(keydist.parse_env_text(text), {"A": "1"})

    def test_double_quotes_unescape(self):
        text = r'A="line1\nline2 \"q\" \\ end"'
        self.assertEqual(keydist.parse_env_text(text),
                         {"A": 'line1\nline2 "q" \\ end'})

    def test_single_quotes_are_literal(self):
        self.assertEqual(keydist.parse_env_text("A='raw\\nvalue'"),
                         {"A": "raw\\nvalue"})

    def test_single_quote_escaped_shell_style(self):
        # 'it'\''s' → it's
        self.assertEqual(keydist.parse_env_text("A='it'\\''s'"), {"A": "it's"})

    def test_unquoted_value_trimmed(self):
        self.assertEqual(keydist.parse_env_text("A =   spaced  "), {"A": "spaced"})

    def test_line_without_equals_skipped(self):
        self.assertEqual(keydist.parse_env_text("justtext\nA=1"), {"A": "1"})

    def test_duplicate_key_last_wins(self):
        self.assertEqual(keydist.parse_env_text("A=1\nA=2"), {"A": "2"})

    def test_empty_text(self):
        self.assertEqual(keydist.parse_env_text(""), {})

# ---------------------------------------------------------------------------
# 引用解析：env / envfile / file
# ---------------------------------------------------------------------------

class ResolveEnvTests(unittest.TestCase):
    """_resolve_env：{ env = "NAME" } 进程环境变量引用。"""

    def test_reads_from_environ(self):
        with mock.patch.dict(os.environ, {"MY_SECRET": "v"}, clear=True):
            self.assertEqual(keydist._resolve_env("MY_SECRET"), ("v", None))

    def test_unset_var_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            value, err = keydist._resolve_env("NOPE_NOT_SET")
            self.assertIsNone(value)
            self.assertIn("not set", err)

    def test_invalid_name_rejected(self):
        for bad in ("1ABC", "A-B", "", "A B", "A.B"):
            value, err = keydist._resolve_env(bad)
            self.assertIsNone(value)
            self.assertIn("invalid env var name", err)

    def test_non_string_name_rejected(self):
        value, err = keydist._resolve_env(123)
        self.assertIsNone(value)
        self.assertIn("invalid env var name", err)


class ResolveEnvfileTests(TempDirBase):
    """_resolve_envfile：{ envfile = "PATH", key = "KEY" } dotenv 文件引用。"""

    def _write(self, text: str) -> pathlib.Path:
        p = self.tmp / "creds.env"
        p.write_text(text)
        return p

    def test_explicit_key(self):
        p = self._write("# note\nexport USER=alice\nTOKEN=tok123\n")
        self.assertEqual(keydist._resolve_envfile({"envfile": str(p), "key": "TOKEN"}),
                         ("tok123", None))

    def test_single_key_file_without_key(self):
        p = self._write("TOKEN=tok123\n")
        self.assertEqual(keydist._resolve_envfile({"envfile": str(p)}),
                         ("tok123", None))

    def test_single_key_file_with_key(self):
        p = self._write("TOKEN=tok123\n")
        self.assertEqual(keydist._resolve_envfile({"envfile": str(p), "key": "TOKEN"}),
                         ("tok123", None))

    def test_multi_key_without_key_fails(self):
        p = self._write("A=1\nB=2\n")
        value, err = keydist._resolve_envfile({"envfile": str(p)})
        self.assertIsNone(value)
        self.assertIn("specify key", err)

    def test_empty_file_fails(self):
        p = self._write("")
        value, err = keydist._resolve_envfile({"envfile": str(p)})
        self.assertIsNone(value)
        self.assertIn("no key=value pairs", err)

    def test_missing_file_fails(self):
        value, err = keydist._resolve_envfile({"envfile": str(self.tmp / "nope.env")})
        self.assertIsNone(value)
        self.assertIn("not found", err)

    def test_key_not_in_file_fails(self):
        p = self._write("A=1\n")
        value, err = keydist._resolve_envfile({"envfile": str(p), "key": "B"})
        self.assertIsNone(value)
        self.assertIn("not found in", err)

    def test_invalid_path_type_fails(self):
        value, err = keydist._resolve_envfile({"envfile": 42})
        self.assertIsNone(value)
        self.assertIn("must be a non-empty string", err)

    def test_invalid_key_type_fails(self):
        p = self._write("A=1\n")
        value, err = keydist._resolve_envfile({"envfile": str(p), "key": 3})
        self.assertIsNone(value)
        self.assertIn("must be a string", err)

    def test_tilde_expansion(self):
        # ~ 展开后不存在的路径应得到 not found，而非崩溃
        value, err = keydist._resolve_envfile(
            {"envfile": "~/keydist-definitely-missing.env"})
        self.assertIsNone(value)
        self.assertIn("not found", err)


class ResolveFileTests(TempDirBase):
    """_resolve_file：{ file = "PATH" } 整文件内容引用（去末尾换行）。"""

    def test_strips_trailing_newline(self):
        p = self.tmp / "key.pem"
        p.write_text("-----BEGIN-----\nabc\n-----END-----\n")
        self.assertEqual(keydist._resolve_file({"file": str(p)}),
                         ("-----BEGIN-----\nabc\n-----END-----", None))

    def test_strips_all_trailing_newlines(self):
        p = self.tmp / "x.txt"
        p.write_text("v\n\n\n")
        self.assertEqual(keydist._resolve_file({"file": str(p)}), ("v", None))

    def test_keeps_internal_newlines(self):
        p = self.tmp / "x.txt"
        p.write_text("a\nb\n")
        self.assertEqual(keydist._resolve_file({"file": str(p)}), ("a\nb", None))

    def test_missing_file_fails(self):
        value, err = keydist._resolve_file({"file": str(self.tmp / "nope")})
        self.assertIsNone(value)
        self.assertIn("not found", err)

    def test_invalid_path_type_fails(self):
        value, err = keydist._resolve_file({"file": None})
        self.assertIsNone(value)
        self.assertIn("must be a non-empty string", err)

# ---------------------------------------------------------------------------
# 引用解析：chain / concat / pipe
# ---------------------------------------------------------------------------

class ResolveChainTests(TempDirBase):
    """_resolve_chain：逐个回退，首个成功者生效；全失败聚合错误。"""

    def test_first_element_wins(self):
        self.assertEqual(keydist._resolve_chain(["first", "second"]),
                         ("first", None))

    def test_falls_back_after_failure(self):
        p = self.tmp / "c.env"
        p.write_text("K=v\n")
        items = [{"env": "KEYDIST_UNSET_GUARD"},
                 {"envfile": str(p), "key": "K"}]
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keydist._resolve_chain(items), ("v", None))

    def test_nested_chain(self):
        items = [{"chain": [{"env": "KEYDIST_UNSET_GUARD"},
                            "inner-literal"]}]
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keydist._resolve_chain(items), ("inner-literal", None))

    def test_all_fail_aggregates_errors(self):
        items = [{"env": "KEYDIST_UNSET_A"}, {"env": "KEYDIST_UNSET_B"}]
        with mock.patch.dict(os.environ, {}, clear=True):
            value, err = keydist._resolve_chain(items)
        self.assertIsNone(value)
        self.assertIn("all fallbacks failed", err)
        self.assertIn("chain[0]", err)
        self.assertIn("chain[1]", err)

    def test_empty_string_literal_is_a_success(self):
        # 空串字面量解析成功（值非 None），不再回退
        self.assertEqual(keydist._resolve_chain(["", "x"]), ("", None))

    def test_non_list_rejected(self):
        value, err = keydist._resolve_chain("x")
        self.assertIsNone(value)
        self.assertIn("must be a non-empty array", err)

    def test_empty_array_rejected(self):
        value, err = keydist._resolve_chain([])
        self.assertIsNone(value)
        self.assertIn("must be a non-empty array", err)


class ResolveConcatTests(TempDirBase):
    """_resolve_concat：全部按序拼接；任一元素失败则整体失败。"""

    def test_concat_literals_and_refs(self):
        env = {"KEYDIST_PART": "world"}
        parts = ["hello ", {"env": "KEYDIST_PART"}, "!", 1, True]
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(keydist._resolve_concat(parts), ("hello world!1true", None))

    def test_nested_concat(self):
        parts = [{"concat": ["a", "b"]}, "c"]
        self.assertEqual(keydist._resolve_concat(parts), ("abc", None))

    def test_any_failure_fails_whole(self):
        parts = ["ok ", {"env": "KEYDIST_UNSET_GUARD"}]
        with mock.patch.dict(os.environ, {}, clear=True):
            value, err = keydist._resolve_concat(parts)
        self.assertIsNone(value)
        self.assertIn("concat[1]", err)

    def test_non_list_rejected(self):
        value, err = keydist._resolve_concat(5)
        self.assertIsNone(value)
        self.assertIn("must be a non-empty array", err)


class ResolvePipeTests(TempDirBase):
    """_resolve_pipe：外部命令处理；stdout 去末尾换行为值，非零退出即失败。"""

    def test_stdout_becomes_value_with_trailing_newline_stripped(self):
        self.assertEqual(keydist._resolve_pipe({"pipe": {"cmd": "echo hello"}}),
                         ("hello", None))

    def test_input_written_to_stdin(self):
        spec = {"pipe": {"cmd": "cat", "input": "through-pipe"}}
        self.assertEqual(keydist._resolve_pipe(spec), ("through-pipe", None))

    def test_input_may_be_reference(self):
        spec = {"pipe": {"cmd": "cat", "input": {"env": "KEYDIST_IN"}}}
        with mock.patch.dict(os.environ, {"KEYDIST_IN": "ref-input"}):
            self.assertEqual(keydist._resolve_pipe(spec), ("ref-input", None))

    def test_input_failure_propagates(self):
        spec = {"pipe": {"cmd": "cat", "input": {"env": "KEYDIST_UNSET_GUARD"}}}
        with mock.patch.dict(os.environ, {}, clear=True):
            value, err = keydist._resolve_pipe(spec)
        self.assertIsNone(value)
        self.assertIn("pipe input", err)

    def test_nonzero_exit_fails_with_stderr(self):
        spec = {"pipe": {"cmd": "sh -c 'echo boom >&2; exit 3'"}}
        value, err = keydist._resolve_pipe(spec)
        self.assertIsNone(value)
        self.assertIn("exited 3", err)
        self.assertIn("boom", err)

    def test_missing_cmd_rejected(self):
        value, err = keydist._resolve_pipe({"pipe": {}})
        self.assertIsNone(value)
        self.assertIn("non-empty cmd", err)

    def test_blank_cmd_rejected(self):
        value, err = keydist._resolve_pipe({"pipe": {"cmd": "   "}})
        self.assertIsNone(value)
        self.assertIn("non-empty cmd", err)

    def test_non_string_cmd_rejected(self):
        value, err = keydist._resolve_pipe({"pipe": {"cmd": 7}})
        self.assertIsNone(value)
        self.assertIn("non-empty cmd", err)

    def test_pipe_not_a_table_rejected(self):
        value, err = keydist._resolve_pipe({"pipe": "echo hi"})
        self.assertIsNone(value)
        self.assertIn("must be a table", err)

    def test_unknown_keys_rejected(self):
        value, err = keydist._resolve_pipe({"pipe": {"cmd": "cat", "extra": 1}})
        self.assertIsNone(value)
        self.assertIn("unknown key", err)
        self.assertIn("extra", err)

    def test_cmd_not_found_fails(self):
        value, err = keydist._resolve_pipe(
            {"pipe": {"cmd": "keydist-no-such-cmd-xyz"}})
        self.assertIsNone(value)
        self.assertIn("pipe cmd failed", err)

# ---------------------------------------------------------------------------
# 引用表与字面量分发
# ---------------------------------------------------------------------------

class ResolveTableTests(TempDirBase):
    """_resolve_table：引用键恰选其一，未知/冲突键拒绝，transform 按序应用。"""

    def test_no_kind_rejected(self):
        value, err = keydist._resolve_table({})
        self.assertIsNone(value)
        self.assertIn("must set one of", err)

    def test_ambiguous_kinds_rejected(self):
        value, err = keydist._resolve_table({"env": "A", "file": "x"})
        self.assertIsNone(value)
        self.assertIn("ambiguous", err)
        self.assertIn("'env'", err)
        self.assertIn("'file'", err)

    def test_unknown_keys_rejected(self):
        value, err = keydist._resolve_table({"env": "PATH", "typo": 1})
        self.assertIsNone(value)
        self.assertIn("unknown key", err)

    def test_envfile_may_carry_key(self):
        p = self.tmp / "one.env"
        p.write_text("K=v\n")
        self.assertEqual(keydist._resolve_table({"envfile": str(p), "key": "K"}),
                         ("v", None))

    def test_key_on_other_kind_rejected(self):
        value, err = keydist._resolve_table({"env": "PATH", "key": "K"})
        self.assertIsNone(value)
        self.assertIn("unknown key", err)

    def test_kind_error_propagates(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            value, err = keydist._resolve_table({"env": "KEYDIST_UNSET_GUARD"})
        self.assertIsNone(value)
        self.assertIn("not set", err)

    def test_transform_applied_after_resolution(self):
        with mock.patch.dict(os.environ, {"KEYDIST_T": "  Mixed  "}, clear=True):
            value, err = keydist._resolve_table(
                {"env": "KEYDIST_T", "transform": ["trim", "upper"]})
        self.assertEqual((value, err), ("MIXED", None))

    def test_transform_failure_propagates(self):
        value, err = keydist._resolve_table({"env": "PATH", "transform": ["nope"]})
        self.assertIsNone(value)
        self.assertIn("unknown transform op", err)


class ResolveSecretTests(unittest.TestCase):
    """resolve_secret：裸标量 = 字面量（布尔渲染 true/false），表 = 引用。"""

    def test_string_literal(self):
        self.assertEqual(keydist.resolve_secret("abc"), ("abc", None))

    def test_int_and_float_literals(self):
        self.assertEqual(keydist.resolve_secret(42), ("42", None))
        self.assertEqual(keydist.resolve_secret(1.5), ("1.5", None))

    def test_bool_renders_true_false(self):
        self.assertEqual(keydist.resolve_secret(True), ("true", None))
        self.assertEqual(keydist.resolve_secret(False), ("false", None))

    def test_table_delegates_to_reference(self):
        with mock.patch.dict(os.environ, {"KEYDIST_S": "from-env"}, clear=True):
            self.assertEqual(keydist.resolve_secret({"env": "KEYDIST_S"}),
                             ("from-env", None))

    def test_table_error_passthrough(self):
        value, err = keydist.resolve_secret({"nope": 1})
        self.assertIsNone(value)
        self.assertIn("must set one of", err)

    def test_unsupported_type_rejected(self):
        for bad in ([1, 2], None):
            value, err = keydist.resolve_secret(bad)
            self.assertIsNone(value)
            self.assertIn("unsupported secret type", err)

# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

class ApplyTransformTests(unittest.TestCase):
    """apply_transform：闭集确定性变换按序应用；未知 op 硬报错。"""

    def test_trim_lower_upper(self):
        self.assertEqual(keydist.apply_transform("  AbC  ", ["trim", "lower"]),
                         ("abc", None))
        self.assertEqual(keydist.apply_transform("  AbC  ", ["trim", "upper"]),
                         ("ABC", None))

    def test_ops_apply_in_order(self):
        self.assertEqual(keydist.apply_transform("ab", ["upper", "b64"]), ("QUI=", None))

    def test_b64(self):
        self.assertEqual(keydist.apply_transform("hello", ["b64"]), ("aGVsbG8=", None))

    def test_hex(self):
        self.assertEqual(keydist.apply_transform("AB", ["hex"]), ("4142", None))

    def test_sha256(self):
        self.assertEqual(
            keydist.apply_transform("abc", ["sha256"]),
            ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", None))

    def test_sha512(self):
        self.assertEqual(
            keydist.apply_transform("abc", ["sha512"]),
            ("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
             "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f", None))

    def test_urlenc(self):
        self.assertEqual(keydist.apply_transform("a b&c", ["urlenc"]),
                         ("a%20b%26c", None))

    def test_empty_ops_return_value(self):
        self.assertEqual(keydist.apply_transform("v", []), ("v", None))

    def test_unknown_op_rejected_and_lists_valid(self):
        value, err = keydist.apply_transform("v", ["rot13"])
        self.assertIsNone(value)
        self.assertIn("unknown transform op 'rot13'", err)
        self.assertIn("sha256", err)

    def test_non_list_rejected(self):
        value, err = keydist.apply_transform("v", "trim")
        self.assertIsNone(value)
        self.assertIn("must be an array of strings", err)

    def test_non_string_op_rejected(self):
        value, err = keydist.apply_transform("v", ["trim", 1])
        self.assertIsNone(value)
        self.assertIn("must be an array of strings", err)

# ---------------------------------------------------------------------------
# 引号与 style 校验
# ---------------------------------------------------------------------------

class QuoteTests(unittest.TestCase):
    """三种引号函数：shell 单引号、dotenv 双引号转义、docker 裸值。"""

    def test_q_shell_escapes_single_quotes(self):
        self.assertEqual(keydist.q_shell("it's"), "'it'\\''s'")

    def test_q_shell_plain(self):
        self.assertEqual(keydist.q_shell("v"), "'v'")

    def test_q_double_escapes_specials(self):
        self.assertEqual(keydist.q_double('a"b\\c\nd'), '"a\\"b\\\\c\\nd"')

    def test_q_docker_verbatim(self):
        self.assertEqual(keydist.q_docker('raw "v"'), 'raw "v"')


class ValidateValueTests(unittest.TestCase):
    """validate_value：错误 = 必然解析失败；警告 = 工具间语义歧义。"""

    def test_shell_values_pass(self):
        errors, warns = keydist.validate_value("shell", "K", "any\nvalue \"x\" # y")
        self.assertEqual((errors, warns), ([], []))

    def test_docker_newline_is_error(self):
        errors, _ = keydist.validate_value("docker", "K", "a\nb")
        self.assertTrue(any("must not contain newlines" in e for e in errors))

    def test_docker_carriage_return_is_error(self):
        errors, _ = keydist.validate_value("docker", "K", "a\rb")
        self.assertTrue(any("must not contain newlines" in e for e in errors))

    def test_docker_edge_whitespace_is_error(self):
        errors, _ = keydist.validate_value("docker", "K", " padded ")
        self.assertTrue(any("leading/trailing whitespace" in e for e in errors))

    def test_docker_quotes_warn_but_not_error(self):
        errors, warns = keydist.validate_value("docker", "K", "it's")
        self.assertEqual(errors, [])
        self.assertTrue(any("keeps quotes literal" in w for w in warns))

    def test_docker_standalone_hash_warns(self):
        errors, warns = keydist.validate_value("docker", "K", "a # b")
        self.assertEqual(errors, [])
        self.assertTrue(any("inline comment" in w for w in warns))

    def test_docker_embedded_hash_no_warning(self):
        _, warns = keydist.validate_value("docker", "K", "a#b")
        self.assertEqual(warns, [])

    def test_systemd_newline_is_error(self):
        errors, _ = keydist.validate_value("systemd", "K", "a\nb")
        self.assertTrue(any("does not support multi-line" in e for e in errors))

    def test_systemd_single_line_passes(self):
        errors, warns = keydist.validate_value("systemd", "K", "plain")
        self.assertEqual((errors, warns), ([], []))


class FormatEnvLineTests(unittest.TestCase):
    """format_env_line：export 前缀仅作用于允许的 style。"""

    def test_shell_keeps_prefix(self):
        self.assertEqual(keydist.format_env_line("K", "v", "shell", "export "),
                         "export K='v'")

    def test_shell_without_prefix(self):
        self.assertEqual(keydist.format_env_line("K", "v", "shell"), "K='v'")

    def test_docker_ignores_prefix(self):
        self.assertEqual(keydist.format_env_line("K", "v", "docker", "export "), "K=v")

    def test_systemd_ignores_prefix(self):
        self.assertEqual(keydist.format_env_line("K", "v", "systemd", "export "),
                         'K="v"')

    def test_dotenv_uses_double_quotes(self):
        self.assertEqual(keydist.format_env_line("K", "a\nb", "dotenv"), 'K="a\\nb"')

# ---------------------------------------------------------------------------
# style 解析
# ---------------------------------------------------------------------------

class ResolveStyleTests(unittest.TestCase):
    """resolve_style：显式 > 目标名/路径/post 关键词自动推断 > 默认 shell。"""

    def test_explicit_style(self):
        self.assertEqual(keydist.resolve_style("svc", {"style": "docker"}),
                         ("docker", "explicit"))

    def test_explicit_unknown_style_raises(self):
        with self.assertRaises(ValueError):
            keydist.resolve_style("svc", {"style": "ini"})

    def test_empty_explicit_style_falls_through(self):
        self.assertEqual(keydist.resolve_style("plain", {"style": ""}),
                         ("shell", "default"))

    def test_auto_systemd_by_name(self):
        self.assertEqual(keydist.resolve_style("my.service.env", {}),
                         ("systemd", "auto"))

    def test_auto_docker_by_path(self):
        self.assertEqual(keydist.resolve_style("app", {"path": "/srv/compose/.env"}),
                         ("docker", "auto"))

    def test_auto_dotenv_by_post_keyword(self):
        self.assertEqual(keydist.resolve_style("app", {"post": "pm2 reload"}),
                         ("dotenv", "auto"))

    def test_default_when_no_hint(self):
        self.assertEqual(keydist.resolve_style("plainapp", {"path": "/x/y.env"}),
                         ("shell", "default"))

    def test_systemd_hint_wins_over_docker(self):
        # _STYLE_HINTS 按序匹配，systemd 在 docker 之前
        self.assertEqual(keydist.resolve_style("docker-systemd-thing", {}),
                         ("systemd", "auto"))

# ---------------------------------------------------------------------------
# env 渲染与写入
# ---------------------------------------------------------------------------

class RenderEnvFileTests(TempDirBase):
    """render_env_file：增量替换、保留无关行、重复键清理、追加新键。"""

    def test_new_file(self):
        original, new, warns = keydist.render_env_file(
            self.tmp / "new.env", {"A": "1"}, "shell")
        self.assertEqual(original, "")
        self.assertEqual(new, "A='1'\n")
        self.assertEqual(warns, [])

    def test_replaces_existing_and_keeps_others(self):
        p = self.tmp / "a.env"
        p.write_text("# header\nA=old\nB=keep\n")
        _, new, warns = keydist.render_env_file(p, {"A": "new"}, "shell")
        self.assertEqual(new, "# header\nA='new'\nB=keep\n")
        self.assertEqual(warns, [])

    def test_preserves_export_prefix(self):
        p = self.tmp / "a.env"
        p.write_text("export A=old\n")
        _, new, _ = keydist.render_env_file(p, {"A": "new"}, "shell")
        self.assertEqual(new, "export A='new'\n")

    def test_docker_style_drops_export_prefix(self):
        p = self.tmp / "a.env"
        p.write_text("export A=old\n")
        _, new, _ = keydist.render_env_file(p, {"A": "new"}, "docker")
        self.assertEqual(new, "A=new\n")

    def test_indented_line_prefix_preserved(self):
        p = self.tmp / "a.env"
        p.write_text("  A = old\n")
        _, new, _ = keydist.render_env_file(p, {"A": "new"}, "shell")
        self.assertEqual(new, "  A='new'\n")

    def test_duplicate_keys_dropped_with_warning(self):
        p = self.tmp / "a.env"
        p.write_text("A=1\nA=2\n")
        _, new, warns = keydist.render_env_file(p, {"A": "new"}, "shell")
        self.assertEqual(new, "A='new'\n")
        self.assertEqual(warns, ["removed stale duplicate key(s): A"])

    def test_duplicate_keys_with_export_mixed(self):
        p = self.tmp / "a.env"
        p.write_text("A=1\nexport A=2\n")
        _, new, warns = keydist.render_env_file(p, {"A": "new"}, "shell")
        self.assertEqual(new, "A='new'\n")
        self.assertEqual(warns, ["removed stale duplicate key(s): A"])

    def test_similar_prefix_key_not_matched(self):
        # FOO 不得误伤 FOO_BAR
        p = self.tmp / "a.env"
        p.write_text("FOO_BAR=x\nFOO=old\n")
        _, new, warns = keydist.render_env_file(p, {"FOO": "new"}, "shell")
        self.assertEqual(new, "FOO_BAR=x\nFOO='new'\n")
        self.assertEqual(warns, [])

    def test_new_key_appended(self):
        p = self.tmp / "a.env"
        p.write_text("A=old\n")
        _, new, _ = keydist.render_env_file(p, {"A": "old", "B": "2"}, "shell")
        self.assertEqual(new, "A='old'\nB='2'\n")

    def test_quotes_follow_style(self):
        p = self.tmp / "a.env"
        _, new, _ = keydist.render_env_file(p, {"A": 'x"y'}, "dotenv")
        self.assertEqual(new, 'A="x\\"y"\n')

    def test_no_updates_preserves_content(self):
        p = self.tmp / "a.env"
        p.write_text("A=1\n# keep\n")
        _, new, warns = keydist.render_env_file(p, {}, "shell")
        self.assertEqual(new, "A=1\n# keep\n")
        self.assertEqual(warns, [])


class WriteEnvFileTests(TempDirBase):
    """write_env_file：0600 创建/截断写入。"""

    def test_creates_with_0600(self):
        p = self.tmp / "new.env"
        keydist.write_env_file(p, "A=1\n")
        self.assertTrue(p.exists())
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.read_text(), "A=1\n")

    def test_overwrites_and_repermissions(self):
        p = self.tmp / "a.env"
        p.write_text("OLD\n")
        p.chmod(0o644)
        keydist.write_env_file(p, "A=1\n")
        self.assertEqual(p.read_text(), "A=1\n")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_truncates_longer_previous_content(self):
        p = self.tmp / "a.env"
        p.write_text("x" * 1000)
        keydist.write_env_file(p, "short")
        self.assertEqual(p.read_text(), "short")

# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

class DiffTextTests(unittest.TestCase):
    """diff_text：unified diff 骨架、缩进与着色开关。"""

    OLD = "A=1\nB=2\n"
    NEW = "A=1\nB=3\nC=4\n"

    def setUp(self) -> None:
        patcher = mock.patch.object(keydist, "COLOR", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_contains_headers_context_and_changes(self):
        out = keydist.diff_text(pathlib.Path("x.env"), self.OLD, self.NEW)
        self.assertIn("--- x.env (current)", out)
        self.assertIn("+++ x.env (proposed)", out)
        self.assertIn("@@", out)
        self.assertIn(" A=1\n", out)      # 上下文行
        self.assertIn("-B=2\n", out)
        self.assertIn("+B=3\n", out)
        self.assertIn("+C=4\n", out)

    def test_lines_indented_and_terminated(self):
        out = keydist.diff_text(pathlib.Path("x.env"), self.OLD, self.NEW, indent="  ")
        for line in out.splitlines(keepends=True):
            self.assertTrue(line.endswith("\n"), repr(line))
            self.assertTrue(line.startswith("  "), repr(line))

    def test_color_enabled_wraps_lines(self):
        with mock.patch.object(keydist, "COLOR", True):
            out = keydist.diff_text(pathlib.Path("x.env"), self.OLD, self.NEW)
        self.assertIn("\x1b[36m", out)  # 头部/@@ 行：青色
        self.assertIn("\x1b[32m", out)  # + 行：绿色
        self.assertIn("\x1b[31m", out)  # - 行：红色

# ---------------------------------------------------------------------------
# 清单查找与 CLI 参数
# ---------------------------------------------------------------------------

class FindManifestTests(TempDirBase):
    """find_manifest：显式路径优先且必须存在；否则按查找顺序取第一个。"""

    def test_explicit_path_returned(self):
        p = self.tmp / "m.toml"
        p.write_text("[targets]\n")
        self.assertEqual(keydist.find_manifest(str(p)), p)

    def test_explicit_missing_exits_1(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
            keydist.find_manifest(str(self.tmp / "nope.toml"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_explicit_tilde_path_missing_exits(self):
        with self.assertRaises(SystemExit):
            keydist.find_manifest("~/keydist-definitely-missing.toml")

    def test_search_order_first_existing(self):
        first = self.tmp / "first.toml"
        second = self.tmp / "second.toml"
        first.write_text("a")
        second.write_text("b")
        with mock.patch.object(keydist, "MANIFEST_SEARCH_PATH", (first, second)):
            self.assertEqual(keydist.find_manifest(None), first)

    def test_search_order_skips_missing(self):
        second = self.tmp / "second.toml"
        second.write_text("b")
        with mock.patch.object(keydist, "MANIFEST_SEARCH_PATH",
                               (self.tmp / "missing.toml", second)):
            self.assertEqual(keydist.find_manifest(None), second)

    def test_none_found_exits_with_search_list(self):
        stderr = io.StringIO()
        with mock.patch.object(keydist, "MANIFEST_SEARCH_PATH",
                               (self.tmp / "m1.toml", self.tmp / "m2.toml")):
            with redirect_stderr(stderr), self.assertRaises(SystemExit):
                keydist.find_manifest(None)
        self.assertIn("m1.toml", stderr.getvalue())
        self.assertIn("m2.toml", stderr.getvalue())
        self.assertIn("--config", stderr.getvalue())


class ParseArgsTests(unittest.TestCase):
    """parse_args：-c/--config 与 -n/--dry-run。"""

    def test_defaults(self):
        ns = keydist.parse_args([])
        self.assertIsNone(ns.config)
        self.assertFalse(ns.dry_run)

    def test_config_short_and_long(self):
        self.assertEqual(keydist.parse_args(["-c", "a.toml"]).config, "a.toml")
        self.assertEqual(keydist.parse_args(["--config", "b.toml"]).config, "b.toml")

    def test_dry_run_flags(self):
        self.assertTrue(keydist.parse_args(["-n"]).dry_run)
        self.assertTrue(keydist.parse_args(["--dry-run"]).dry_run)

# ---------------------------------------------------------------------------
# main() 端到端
# ---------------------------------------------------------------------------

class MainTestBase(TempDirBase):
    """端到端运行 main()：临时清单 + argv/查找路径 patch + 输出捕获。"""

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.tmp / "keydist.toml"
        self.env_path = self.tmp / "out" / "app.env"
        self.post_marker = self.tmp / "post-ran"
        self.write_manifest(self.default_manifest())
        # 把清单查找限定到临时目录，避免读到机器上的真实配置
        patcher = mock.patch.object(keydist, "MANIFEST_SEARCH_PATH", (self.manifest,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def default_manifest(self, secrets: str = 'alpha = "secret-value"',
                         style: str = "shell", post: str | None = None) -> str:
        """生成默认清单文本；post 为 None 表示 touch 标记文件，"" 表示不配置。"""
        if post is None:
            post = f"touch {self.post_marker}"
        post_line = f"post = {post!r}\n" if post else ""
        return (
            f"[secrets]\n{secrets}\n\n"
            "[targets.app]\n"
            f"path = {str(self.env_path)!r}\n"
            f'style = "{style}"\n'
            f"{post_line}"
            "\n[targets.app.vars]\n"
            'APP_TOKEN = "alpha"\n'
        )

    def write_manifest(self, body: str) -> None:
        self.manifest.write_text(body)
        self.manifest.chmod(0o600)

    def run_main(self, *args: str) -> tuple[int, str, str]:
        """运行 main()，捕获输出并关闭着色；返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(keydist, "COLOR", False):
            with mock.patch.object(sys, "argv", ["keydist", *args]):
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        keydist.main()
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()


class MainRunTests(MainTestBase):
    """main() 正常路径：写入、0600 权限、post 触发与跳过。"""

    def test_first_run_writes_file_with_0600_and_runs_post(self):
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("APP_TOKEN='secret-value'", self.env_path.read_text())
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.post_marker.exists())
        self.assertIn("updated", out)
        self.assertIn("1 updated", out)
        self.assertIn("1 target(s)", out)
        self.assertIn("done", out)

    def test_second_run_unchanged_skips_post(self):
        self.run_main()
        self.post_marker.unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertFalse(self.post_marker.exists())
        self.assertIn("unchanged", out)
        self.assertIn("1 unchanged", out)
        self.assertIn("post: skipped (no changes)", out)

    def test_value_change_triggers_post_again(self):
        self.run_main()
        self.post_marker.unlink()
        self.write_manifest(self.default_manifest(secrets='alpha = "rotated"'))
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.post_marker.exists())
        self.assertIn("APP_TOKEN='rotated'", self.env_path.read_text())

    def test_parent_dirs_created(self):
        self.assertFalse(self.env_path.parent.exists())
        self.run_main()
        self.assertTrue(self.env_path.parent.is_dir())

    def test_permissions_repaired_when_content_unchanged(self):
        self.run_main()
        self.env_path.chmod(0o644)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_key_cleanup_warns(self):
        self.env_path.parent.mkdir(parents=True)
        self.env_path.write_text("APP_TOKEN=old\nAPP_TOKEN=old2\n")
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.env_path.read_text().count("APP_TOKEN"), 1)
        self.assertIn("removed stale duplicate key", err)

    def test_multiple_targets_summarized(self):
        second = self.tmp / "second.env"
        body = self.default_manifest() + (
            "\n[targets.second]\n"
            f"path = {str(second)!r}\n"
            "\n[targets.second.vars]\n"
            'OTHER = "alpha"\n'
        )
        self.write_manifest(body)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("2 target(s)", out)
        self.assertIn("2 updated", out)
        self.assertTrue(second.exists())

    def test_nondeterministic_secret_warns(self):
        secrets = 'alpha = { pipe = { cmd = "sh -c \'echo $$\'" } }\n'
        targets = ""
        for i in range(2):
            path = str(self.tmp / f"t{i}.env")
            targets += (
                f"[targets.t{i}]\n"
                f"path = {path!r}\n"
                'style = "shell"\n'
                "\n"
                f"[targets.t{i}.vars]\n"
                'V = "alpha"\n\n'
            )
        self.write_manifest(f"[secrets]\n{secrets}\n{targets}")
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("resolved differently", err)


class MainDryRunTests(MainTestBase):
    """main() --dry-run：打印 diff，不写文件、不改权限、不执行 post。"""

    def test_dry_run_does_not_write_or_post(self):
        code, out, err = self.run_main("-n")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.env_path.exists())
        self.assertFalse(self.post_marker.exists())
        self.assertIn("DRY-RUN", out)
        self.assertIn("would update", out)
        self.assertIn("+++ ", out)
        self.assertIn("dry-run complete", out)

    def test_dry_run_shows_diff_against_existing_file(self):
        self.env_path.parent.mkdir(parents=True)
        self.env_path.write_text("APP_TOKEN=old\nKEEP=1\n")
        code, out, err = self.run_main("-n")
        self.assertEqual(code, 0, err)
        self.assertIn("-APP_TOKEN=old", out)
        self.assertIn("+APP_TOKEN='secret-value'", out)
        self.assertIn(" KEEP=1", out)
        self.assertEqual(self.env_path.read_text(), "APP_TOKEN=old\nKEEP=1\n")

    def test_dry_run_unchanged_target_no_diff(self):
        self.env_path.parent.mkdir(parents=True)
        self.env_path.write_text("APP_TOKEN='secret-value'\n")
        code, out, err = self.run_main("-n")
        self.assertEqual(code, 0, err)
        self.assertIn("unchanged", out)
        self.assertNotIn("+++", out)

    def test_dry_run_leaves_permissions_alone(self):
        self.env_path.parent.mkdir(parents=True)
        self.env_path.write_text("APP_TOKEN='secret-value'\n")
        self.env_path.chmod(0o644)
        code, out, err = self.run_main("-n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o644)

    def test_dry_run_executes_pipe_secret_for_diff(self):
        # pipe 须确定性且无副作用：dry-run 下同样执行才能计算 diff
        self.write_manifest(self.default_manifest(
            secrets='alpha = { pipe = { cmd = "echo from-pipe" } }'))
        code, out, err = self.run_main("-n")
        self.assertEqual(code, 0, err)
        self.assertIn("from-pipe", out)
        self.assertFalse(self.env_path.exists())


class MainFailureTests(MainTestBase):
    """main() 失败路径：fail-closed 不写残缺值、汇总失败、退出码 1。"""

    def test_undefined_secret_fails_target_without_writing(self):
        self.write_manifest(self.default_manifest(secrets='other = "x"'))
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(self.env_path.exists())
        self.assertFalse(self.post_marker.exists())
        self.assertIn("not defined in [secrets]", err)
        self.assertIn("1 failed", err)
        self.assertIn("- app", err)

    def test_unresolved_secret_fails_target(self):
        self.write_manifest(self.default_manifest(
            secrets='alpha = { env = "KEYDIST_UNSET_GUARD" }'))
        with mock.patch.dict(os.environ):
            os.environ.pop("KEYDIST_UNSET_GUARD", None)
            code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(self.env_path.exists())
        self.assertFalse(self.post_marker.exists())
        self.assertIn("unresolved", err)
        self.assertIn("not set", err)

    def test_validation_error_fails_target(self):
        body = self.default_manifest(secrets='alpha = "line1\\nline2"',
                                     style="docker", post="")
        self.write_manifest(body)
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(self.env_path.exists())
        self.assertIn("must not contain newlines", err)

    def test_missing_path_key_reported(self):
        body = (
            '[secrets]\nalpha = "v"\n\n'
            "[targets.broken]\n"
            'style = "shell"\n'
            "\n[targets.broken.vars]\n"
            'A = "alpha"\n'
        )
        self.write_manifest(body)
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("missing required key 'path'", err)

    def test_unknown_style_reported(self):
        self.write_manifest(self.default_manifest(style="ini"))
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(self.env_path.exists())
        self.assertIn("unknown style", err)

    def test_post_failure_counts_as_failure(self):
        self.write_manifest(self.default_manifest(post="false"))
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(self.env_path.exists())  # 文件已写，post 失败单独计入
        self.assertFalse(self.post_marker.exists())
        self.assertIn("post exited", err)


class MainCliTests(MainTestBase):
    """main() 入口：清单查找、--config、权限警告与空清单。"""

    def test_missing_manifest_exits_1(self):
        self.manifest.unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("manifest not found", err)

    def test_config_flag_selects_manifest(self):
        alt = self.tmp / "alt.toml"
        alt.write_text(self.default_manifest(secrets='alpha = "alt-value"'))
        alt.chmod(0o600)
        code, out, err = self.run_main("-c", str(alt))
        self.assertEqual(code, 0, err)
        self.assertIn("APP_TOKEN='alt-value'", self.env_path.read_text())

    def test_permissive_manifest_warns(self):
        self.manifest.chmod(0o644)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("too permissive", err)
        self.assertIn("chmod 600", err)

    def test_no_targets_defined_warns(self):
        self.write_manifest("[secrets]\nalpha = 'v'\n")
        code, out, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("no targets defined", err)
        self.assertNotIn("done", out)


if __name__ == "__main__":
    unittest.main()

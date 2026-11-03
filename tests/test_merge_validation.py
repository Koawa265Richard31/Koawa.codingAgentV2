"""F8 合并边界拒绝面：``validate_merge_request`` 对不可信合并请求的每一种拒绝。

夹具原则：每个用例只把请求在一个维度上弄坏，其余部分（digest、schema、路径、
限额）保持有效。``MergeRejectionControlTest`` 证明同一套夹具在未被篡改时确实会被
接受，所以每条拒绝都能归因到被改动的那一个字段，而不是夹具本身不合法。

接口按工作流交给实现者的签名编写：``MergeError(code)``/``.code``、``MergeRequest``、
``MergeLimits``、``canonical_patch_digest``、``validate_merge_request``。规范没有
钉死错误码的少数别名写法（反斜杠、``./``、分量尾点、盘符、NUL、空路径、孤立
surrogate）只断言"必被拒绝，且错误码属于合并词汇表"，理由写在各用例旁。

每次拒绝后还核对沙箱快照：校验是只读的，被拒绝的请求不得在文件系统上留下痕迹。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from koawa_agent_v2.workspace.merge import (
    MergeError,
    MergeLimits,
    MergeRequest,
    canonical_patch_digest,
    validate_merge_request,
)


BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OLD_TEXT = b"value = 1\n"
OLD_SHA256 = hashlib.sha256(OLD_TEXT).hexdigest()

DIGEST_MISMATCH = "merge_digest_mismatch"
OUTSIDE = "merge_path_outside_candidate"
PROTECTED = "merge_path_protected"
INVALID = "invalid_merge_patch"
TOO_LARGE = "merge_too_large"


def _add(path: Any, content: str = "print('hi')\n") -> dict[str, Any]:
    """协议意义上完整的 ADD（editing/protocol.py:370 的精确字段集）。"""
    return {
        "operation": "add",
        "path": path,
        "content": content,
        "newline": "lf",
        "utf8_bom": False,
    }


def _update(path: Any) -> dict[str, Any]:
    """协议意义上完整的 UPDATE：base 为 OLD_TEXT，把第 1 行改掉。"""
    return {
        "operation": "update",
        "path": path,
        "base_sha256": OLD_SHA256,
        "hunks": [
            {"old_start": 1, "old_lines": ["value = 1"], "new_lines": ["value = 2"]}
        ],
    }


def _delete(path: Any) -> dict[str, Any]:
    return {"operation": "delete", "path": path, "base_sha256": OLD_SHA256}


_OPERATIONS = (("add", _add), ("update", _update), ("delete", _delete))


def _document(*changes: Any) -> dict[str, Any]:
    return {"schema_version": 1, "changes": list(changes)}


def _canonical_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _spec_digest(value: Any) -> str:
    """按规范公式独立计算 digest；非 object 文档用它（被测函数只声明接受 dict）。"""
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def _raw_digest(text: str) -> str:
    """无法规范化的 patch_json 用的 digest：传输文本本身的 sha256，形状合法。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _signed(document: dict[str, Any]) -> tuple[str, str]:
    """诚实签名者：规范文本 + 被测 canonical_patch_digest。"""
    return _canonical_text(document), canonical_patch_digest(document)


def _snapshot(root: Path) -> dict[str, str | None]:
    """沙箱快照：相对路径 -> 文件内容 sha256（目录记为 None）。"""
    entries: dict[str, str | None] = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames:
            entries[os.path.relpath(os.path.join(directory, name), root)] = None
        for name in filenames:
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                entries[os.path.relpath(path, root)] = hashlib.sha256(
                    handle.read()
                ).hexdigest()
    return entries


def _make_directory_link(link: Path, target: Path) -> None:
    """建目录 symlink；Windows 无权限（WinError 1314）时退回 junction，都不行就跳过。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, AttributeError, OSError):
            pass
    raise unittest.SkipTest("cannot create a directory symlink or junction here")


def _remove_directory_link(link: Path) -> None:
    """只删链接本身；Windows 上目录 symlink 与 junction 都按目录项删除，不碰目标。"""
    try:
        if os.name == "nt":
            os.rmdir(link)
        else:
            os.unlink(link)
    except FileNotFoundError:
        pass


class _MergeRejectionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="merge-reject-")
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = Path(self._tmp.name)
        self.candidate_root = self.sandbox / "candidate"
        self.candidate_root.mkdir()
        self.outside_dir = self.sandbox / "outside"
        self.outside_dir.mkdir()
        self.agent_id = uuid4()
        self.run_id = uuid4()

    def _request(self, patch_json: str, digest: str) -> MergeRequest:
        return MergeRequest(
            source_agent_id=self.agent_id,
            run_id=self.run_id,
            base_commit=BASE_COMMIT,
            patch_digest=digest,
            patch_json=patch_json,
        )

    def _seed(self, relative: str, data: bytes = OLD_TEXT) -> None:
        target = self.candidate_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def assertRejected(
        self,
        expected: str | Iterable[str],
        *,
        patch_json: str,
        digest: str,
        protected_patterns: tuple[str, ...] = (),
        limits: MergeLimits | None = None,
    ) -> MergeError:
        codes = {expected} if isinstance(expected, str) else set(expected)
        before = _snapshot(self.sandbox)
        with self.assertRaises(
            MergeError, msg=f"expected rejection with one of {sorted(codes)}"
        ) as caught:
            # 请求在 with 内构造：实现若在 MergeRequest 构造期就拒绝，同样计入。
            validate_merge_request(
                self._request(patch_json, digest),
                candidate_root=self.candidate_root,
                protected_patterns=protected_patterns,
                limits=limits,
            )
        error = caught.exception
        self.assertIn(
            error.code,
            codes,
            f"code {error.code!r} is not one of {sorted(codes)}",
        )
        self.assertEqual(
            _snapshot(self.sandbox),
            before,
            "a rejected request must not touch the filesystem",
        )
        return error

    def assertDocumentRejected(
        self, expected: str | Iterable[str], document: dict[str, Any], **kwargs: Any
    ) -> MergeError:
        patch_json, digest = _signed(document)
        return self.assertRejected(
            expected, patch_json=patch_json, digest=digest, **kwargs
        )


class MergeDigestRejectionTest(_MergeRejectionCase):
    """digest 绑定规范化后的文档内容；任何不一致都是 merge_digest_mismatch。"""

    def test_tampered_digest_is_rejected(self) -> None:
        patch_json, digest = _signed(_document(_add("src/hello.py")))
        flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        for label, forged in (("one_hex_digit_flipped", flipped), ("all_zero", "0" * 64)):
            with self.subTest(label):
                self.assertNotEqual(forged, digest)
                self.assertRejected(
                    DIGEST_MISMATCH, patch_json=patch_json, digest=forged
                )

    def test_payload_changed_after_signing_is_rejected(self) -> None:
        # 只改 digest 不够：digest 必须真的绑定内容，改载荷保留原 digest 也要拒。
        _, digest = _signed(_document(_add("src/hello.py")))
        for label, tampered in (
            ("content", _document(_add("src/hello.py", content="print('pwned')\n"))),
            ("path", _document(_add("src/other.py"))),
            ("extra_change", _document(_add("src/hello.py"), _add("src/extra.py"))),
        ):
            with self.subTest(label):
                self.assertRejected(
                    DIGEST_MISMATCH,
                    patch_json=_canonical_text(tampered),
                    digest=digest,
                )

    def test_digest_of_raw_non_canonical_text_is_rejected(self) -> None:
        # digest 定义在规范化 JSON（sort_keys、紧凑分隔符、UTF-8）上，
        # 不是传输文本的 sha256；拿原文哈希冒充必须失败。
        document = _document(_add("src/hello.py"))
        raw = json.dumps(document, indent=2)
        raw_digest = _raw_digest(raw)
        self.assertNotEqual(
            raw_digest,
            canonical_patch_digest(document),
            "precondition: the raw text must differ from the canonical form",
        )
        self.assertRejected(DIGEST_MISMATCH, patch_json=raw, digest=raw_digest)


class MergeDocumentShapeRejectionTest(_MergeRejectionCase):
    """文档解析与结构：一律 invalid_merge_patch，且不得泄漏原始异常。"""

    def test_patch_json_that_is_not_json_is_rejected(self) -> None:
        for label, text in (
            ("empty", ""),
            ("prose", "not json"),
            ("truncated", '{"schema_version": 1, "changes": ['),
            ("single_quoted", "{'schema_version': 1, 'changes': []}"),
            (
                "trailing_garbage",
                _canonical_text(_document(_add("src/a.py"))) + " trailing",
            ),
        ):
            with self.subTest(label):
                self.assertRejected(INVALID, patch_json=text, digest=_raw_digest(text))

    def test_pathologically_nested_json_is_rejected(self) -> None:
        # json.loads 对深层嵌套抛 RecursionError 而不是 JSONDecodeError；
        # editing/protocol.py:192 同样把它归为文档无效。
        depth = 100_000
        text = "[" * depth + "]" * depth
        self.assertRejected(INVALID, patch_json=text, digest=_raw_digest(text))

    def test_lone_surrogate_escape_is_rejected(self) -> None:
        # "\ud800" 是合法 JSON 转义，但解析出的字符串没有 UTF-8 编码，规范 digest
        # 无从计算。要求得到合并错误而不是 UnicodeEncodeError；归为文档无效或
        # digest 不符都可接受。digest 取传输文本的哈希，模拟"尽力签名"的对手。
        text = _canonical_text(_document(_add("src/PLACEHOLDER.py"))).replace(
            "PLACEHOLDER", "\\ud800"
        )
        self.assertIn("\\ud800", text)
        self.assertRejected(
            {INVALID, DIGEST_MISMATCH}, patch_json=text, digest=_raw_digest(text)
        )

    def test_top_level_value_that_is_not_an_object_is_rejected(self) -> None:
        for value in ([], None, 1, "patch", [_document(_add("src/a.py"))]):
            with self.subTest(value=value):
                self.assertRejected(
                    INVALID,
                    patch_json=_canonical_text(value),
                    digest=_spec_digest(value),
                )

    def test_schema_version_other_than_integer_one_is_rejected(self) -> None:
        # Python 里 True == 1 且 1.0 == 1；editing/protocol.py:197-198 要求非 bool 的 int。
        for label, version in (
            ("two", 2),
            ("zero", 0),
            ("string", "1"),
            ("null", None),
            ("bool_true", True),
            ("float_one", 1.0),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(
                    INVALID,
                    {"schema_version": version, "changes": [_add("src/a.py")]},
                )
        with self.subTest("missing"):
            self.assertDocumentRejected(INVALID, {"changes": [_add("src/a.py")]})

    def test_empty_changes_is_rejected(self) -> None:
        self.assertDocumentRejected(INVALID, _document())

    def test_changes_that_is_not_a_list_is_rejected(self) -> None:
        for label, document in (
            ("missing", {"schema_version": 1}),
            ("null", {"schema_version": 1, "changes": None}),
            ("empty_object", {"schema_version": 1, "changes": {}}),
            (
                "object",
                {"schema_version": 1, "changes": {"src/a.py": _add("src/a.py")}},
            ),
            ("string", {"schema_version": 1, "changes": "src/a.py"}),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(INVALID, document)

    def test_malformed_change_entries_are_rejected(self) -> None:
        missing_path = _add("src/a.py")
        del missing_path["path"]
        for label, change in (
            ("string_entry", "src/a.py"),
            ("null_entry", None),
            ("list_entry", ["src/a.py"]),
            ("missing_path", missing_path),
            ("integer_path", _add(123)),
            ("list_path", _add(["src/a.py"])),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(INVALID, _document(change))

    def test_duplicate_paths_are_rejected(self) -> None:
        for label, changes in (
            ("identical", (_add("src/a.py"), _add("src/a.py", content="other\n"))),
            # editing/protocol.py:210-215 按 casefold 去重；大小写不敏感的文件系统上
            # 这两条改的是同一个文件。
            ("case_variant", (_add("src/a.py"), _add("SRC/A.py"))),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(INVALID, _document(*changes))
        with self.subTest("dot_segment_alias"):
            # "./src/a.py" 与 "src/a.py" 指向同一文件：当作重复，或直接拒绝非规范
            # 写法都可以，唯独不能两条都放行。
            self.assertDocumentRejected(
                {INVALID, OUTSIDE}, _document(_add("src/a.py"), _add("./src/a.py"))
            )

    def test_update_and_delete_of_the_same_file_is_rejected(self) -> None:
        self._seed("src/a.py")
        self.assertDocumentRejected(
            INVALID, _document(_update("src/a.py"), _delete("src/a.py"))
        )


class MergePathEscapeRejectionTest(_MergeRejectionCase):
    """路径必须是落在候选根内部的相对路径：绝对、含 ".."、解析后在外都拒绝。

    补丁文档与平台无关，路径按仓库路径边界的同一套规则解释
    （tools/workspace.py:533-571）："\\" 与 "/" 都是分隔符，盘符、根、控制字符、
    ".." 分量一律非法。用 PurePosixPath 判绝对、再用宿主 Path 拼接解析会产生
    解析差异：Windows 上同盘的 "C:/..." 与 "C:x" 会被 joinpath 重新挂到根下。
    """

    def test_parent_traversal_is_rejected(self) -> None:
        for name, build in _OPERATIONS:
            with self.subTest(operation=name):
                self.assertDocumentRejected(OUTSIDE, _document(build("../outside.txt")))
        self.assertFalse((self.sandbox / "outside.txt").exists())

    def test_any_dotdot_segment_is_rejected(self) -> None:
        # 规范：含 ".." 即拒绝，哪怕规范化后仍落在根内（"a/../b.txt"）。
        for path in (
            "a/../../outside.txt",
            "src/../../outside.txt",
            "./../outside.txt",
            "..",
            "a/../b.txt",
        ):
            with self.subTest(path=path):
                self.assertDocumentRejected(OUTSIDE, _document(_add(path)))

    def test_traversal_in_a_later_change_is_rejected(self) -> None:
        self.assertDocumentRejected(
            OUTSIDE, _document(_add("src/ok.py"), _add("../outside.txt"))
        )

    def test_absolute_path_is_rejected(self) -> None:
        absolute = (self.outside_dir / "abs.txt").resolve().as_posix()
        for name, build in _OPERATIONS:
            with self.subTest(operation=name):
                self.assertDocumentRejected(OUTSIDE, _document(build(absolute)))
        self.assertFalse((self.outside_dir / "abs.txt").exists())

    def test_absolute_or_rooted_path_is_rejected_even_inside_candidate(self) -> None:
        # 绝对路径本身就违规，不看它指向哪里。Windows 上不带盘符的 "/x" 不被
        # Path.is_absolute() 视为绝对，拼到根上还会落回根内——典型绕过点。
        inside = (self.candidate_root.resolve() / "inside.txt").as_posix()
        rooted = os.path.splitdrive(inside)[1]
        for label, path in (
            ("absolute_inside", inside),
            ("rooted_inside", rooted),
            ("rooted_outside", "/outside.txt"),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(OUTSIDE, _document(_add(path)))

    def test_backslash_and_drive_syntax_cannot_escape(self) -> None:
        # 补丁文档与平台无关；仓库路径边界在所有平台都把 "\\" 当分隔符、拒绝盘符
        # 与根（tools/workspace.py:544-551）。规范没给这类写法单列错误码，只要求
        # 必被拒绝。盘符取候选根自己的盘，免得同盘的盘符相对路径"碰巧"落回根内。
        drive = os.path.splitdrive(str(self.candidate_root.resolve()))[0] or "C:"
        for path in (
            "..\\outside.txt",
            "a\\..\\..\\outside.txt",
            "\\outside.txt",
            f"{drive}outside.txt",
            f"{drive}\\outside.txt",
        ):
            with self.subTest(path=path):
                self.assertDocumentRejected({OUTSIDE, INVALID}, _document(_add(path)))

    def test_link_inside_candidate_pointing_outside_is_rejected(self) -> None:
        # 链接目标故意选成名字以候选根名开头的兄弟目录：只做字符串前缀比较、
        # 或不解析链接就判定"在根内"的实现都会在这里放行。
        sibling = self.sandbox / f"{self.candidate_root.name}-evil"
        sibling.mkdir()
        link = self.candidate_root / "link"
        _make_directory_link(link, sibling)
        self.addCleanup(_remove_directory_link, link)
        self.assertDocumentRejected(OUTSIDE, _document(_add("link/payload.txt")))
        self.assertFalse((sibling / "payload.txt").exists())

    def test_degenerate_paths_are_rejected(self) -> None:
        # NUL、空串、"."、"./" 都不是候选根内的文件（"" 与 "." 解析后就是根本身）。
        # 规范没单列错误码，接受这两个码之一；不能放行，也不能漏出 ValueError。
        for label, path in (
            ("nul_byte", "src/a\x00.py"),
            ("empty", ""),
            ("dot", "."),
            ("dot_slash", "./"),
        ):
            with self.subTest(label):
                self.assertDocumentRejected({INVALID, OUTSIDE}, _document(_add(path)))


class MergeProtectedPathRejectionTest(_MergeRejectionCase):
    """protected_patterns：以 / 结尾的目录前缀、含 / 的精确路径、裸词按文件名前缀。

    匹配必须作用在规范化后的路径上（分隔符、"."、空分量折叠后；参照
    tools/workspace.py:533-571），否则同一文件换个写法就绕过保护。
    """

    def test_protected_directory_prefix_is_rejected(self) -> None:
        self._seed("tests/test_x.py")
        for name, build in _OPERATIONS:
            path = "tests/test_new.py" if name == "add" else "tests/test_x.py"
            with self.subTest(operation=name):
                self.assertDocumentRejected(
                    PROTECTED,
                    _document(build(path)),
                    protected_patterns=("tests/",),
                )

    def test_protected_directory_prefix_covers_nested_paths(self) -> None:
        self.assertDocumentRejected(
            PROTECTED,
            _document(_add("tests/unit/deep/test_y.py")),
            protected_patterns=("tests/",),
        )

    def test_exact_and_bare_word_patterns_are_rejected(self) -> None:
        for label, pattern, path in (
            ("exact_path", "src/app/config.py", "src/app/config.py"),
            ("bare_word_same_basename", ".env", ".env"),
            ("bare_word_basename_prefix", ".env", "deploy/.env.production"),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(
                    PROTECTED, _document(_add(path)), protected_patterns=(pattern,)
                )

    def test_every_pattern_and_every_change_is_checked(self) -> None:
        with self.subTest("second_pattern_matches"):
            self.assertDocumentRejected(
                PROTECTED,
                _document(_add("tests/a.py")),
                protected_patterns=("docs/", "tests/"),
            )
        with self.subTest("second_change_matches"):
            self.assertDocumentRejected(
                PROTECTED,
                _document(_add("src/ok.py"), _add("tests/b.py")),
                protected_patterns=("tests/",),
            )

    def test_case_variant_does_not_bypass_protection(self) -> None:
        # 现有 apply_patch 策略闸门按 casefold 匹配（editing/tools.py:103-119）；
        # 大小写不敏感的文件系统上 "Tests/" 就是 "tests/"。
        for path in ("Tests/test_x.py", "TESTS/test_x.py"):
            with self.subTest(path=path):
                self.assertDocumentRejected(
                    PROTECTED, _document(_add(path)), protected_patterns=("tests/",)
                )

    def test_path_aliases_do_not_bypass_protection(self) -> None:
        # 这些写法与受保护文件是同一个文件（Windows 还会去掉分量末尾的点）。
        # 规范化后命中 -> merge_path_protected；直接拒绝非规范写法也可以；唯独不能放行。
        for pattern, path in (
            ("tests/", "./tests/test_x.py"),
            ("tests/", "tests\\test_x.py"),
            ("tests/", "tests./test_x.py"),
            ("src/app/config.py", "src/./app/config.py"),
            ("src/app/config.py", "src//app/config.py"),
            ("src/app/config.py", "src/app/config.py."),
            (".env", "deploy/.env/"),
        ):
            with self.subTest(path=path):
                self.assertDocumentRejected(
                    {PROTECTED, OUTSIDE, INVALID},
                    _document(_add(path)),
                    protected_patterns=(pattern,),
                )


class MergeSizeLimitRejectionTest(_MergeRejectionCase):
    """MergeLimits 的每一项在校验侧都要重新执行：请求本身不可信。"""

    def test_file_count_above_max_files_is_rejected(self) -> None:
        for limit in (1, 2):
            with self.subTest(max_files=limit):
                changes = [_add(f"src/f{index}.py") for index in range(limit + 1)]
                self.assertDocumentRejected(
                    TOO_LARGE,
                    _document(*changes),
                    limits=MergeLimits(max_files=limit),
                )

    def test_default_limits_apply_when_limits_is_none(self) -> None:
        count = MergeLimits().max_files + 1
        changes = [_add(f"src/f{index:04d}.py") for index in range(count)]
        self.assertDocumentRejected(TOO_LARGE, _document(*changes))

    def test_total_bytes_above_max_total_bytes_is_rejected(self) -> None:
        limits = MergeLimits(max_total_bytes=1024)
        for label, changes in (
            ("single_file", [_add("src/big.py", content="x" * 4095 + "\n")]),
            (
                # 每个 512 字节都低于上限，合计 1536 才越界：上限是总量不是单文件。
                "sum_of_small_files",
                [
                    _add(f"src/part{index}.py", content="y" * 511 + "\n")
                    for index in range(3)
                ],
            ),
            (
                # 601 个字符、1201 个 UTF-8 字节：按字符计数会漏判。
                "utf8_bytes_not_characters",
                [_add("src/accent.py", content="é" * 600 + "\n")],
            ),
        ):
            with self.subTest(label):
                self.assertDocumentRejected(
                    TOO_LARGE, _document(*changes), limits=limits
                )

    def test_patch_json_longer_than_max_patch_json_chars_is_rejected(self) -> None:
        # 请求不可信：导出端的上限若不在校验端重放，手工构造的请求就能绕过它。
        # 20 个小文件：内容合计 40 字节，远低于任何字节上限；只有 JSON 长度越界。
        document = _document(
            *(_add(f"src/f{index:02d}.py", content="x\n") for index in range(20))
        )
        patch_json, digest = _signed(document)
        limits = MergeLimits(max_patch_json_chars=1024)
        self.assertGreater(len(patch_json), limits.max_patch_json_chars)
        self.assertRejected(
            TOO_LARGE, patch_json=patch_json, digest=digest, limits=limits
        )


class MergeRejectionControlTest(_MergeRejectionCase):
    """对照组：证明上面的拒绝来自被改动的字段，而不是夹具本身。"""

    def test_unmodified_fixture_request_is_accepted(self) -> None:
        self._seed("src/existing.py")
        self._seed("src/obsolete.py")
        document = _document(
            _add("src/hello.py"),
            _update("src/existing.py"),
            _delete("src/obsolete.py"),
        )
        patch_json, digest = _signed(document)
        before = _snapshot(self.sandbox)
        # max_files=3 恰好等于文件数：边界值本身不算"超过"。
        for limits in (None, MergeLimits(max_files=3)):
            with self.subTest(limits=limits):
                result = validate_merge_request(
                    self._request(patch_json, digest),
                    candidate_root=self.candidate_root,
                    protected_patterns=("tests/", ".env", "src/app/config.py"),
                    limits=limits,
                )
                self.assertIsNone(result)
        self.assertEqual(_snapshot(self.sandbox), before)

    def test_canonical_patch_digest_follows_the_documented_formula(self) -> None:
        # 所有 digest 用例都依赖这个公式；含非 ASCII 以覆盖 ensure_ascii=False。
        document = _document(
            _add("src/naïve.py", content="café\n"), _delete("src/obsolete.py")
        )
        self.assertEqual(canonical_patch_digest(document), _spec_digest(document))

    def test_error_carries_only_the_fixed_code(self) -> None:
        # 错误码与内容无关：不可信路径文本不得出现在异常的 code/str/repr 里。
        marker = "ZZUNTRUSTEDMARKER"
        for label, expected, document, patterns in (
            ("traversal", OUTSIDE, _document(_add(f"../{marker}.txt")), ()),
            (
                "protected",
                PROTECTED,
                _document(_add(f"tests/{marker}.py")),
                ("tests/",),
            ),
            (
                "duplicate",
                INVALID,
                _document(_add(f"src/{marker}.py"), _add(f"src/{marker}.py")),
                (),
            ),
        ):
            with self.subTest(label):
                error = self.assertDocumentRejected(
                    expected, document, protected_patterns=patterns
                )
                self.assertEqual(error.code, expected)
                self.assertNotIn(marker, str(error))
                self.assertNotIn(marker, repr(error))


if __name__ == "__main__":
    unittest.main()

"""segments.py 单元测试：段数组 / CQ 码解析、渲染、媒体注册表、提取器。

畸形输入一律不抛异常（防御式解析约定）。
"""

from __future__ import annotations

import pytest

from onebot_plugin.segments import (
    MediaRegistry,
    at_targets,
    extract_reply_id,
    parse_segments,
    render_segments,
    render_text,
)


# ---------------------------------------------------------------------------
# 段数组解析
# ---------------------------------------------------------------------------

class TestParseArray:
    def test_basic_types(self):
        segments = parse_segments([
            {"type": "text", "data": {"text": "你好"}},
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "face", "data": {"id": "5"}},
            {"type": "reply", "data": {"id": "999"}},
            {"type": "image", "data": {"url": "https://e.com/a.jpg", "file": "a.jpg",
                                        "file_size": 1024}},
            {"type": "record", "data": {"url": "https://e.com/r.mp3"}},
            {"type": "video", "data": {"file": "v.mp4"}},
            {"type": "file", "data": {"name": "报告.pdf", "file_id": "f1",
                                       "file_size": "2048"}},
        ])
        assert [seg.type for seg in segments] == [
            "text", "at", "face", "reply", "image", "record", "video", "file"]
        assert segments[0].raw_text == "你好"
        assert segments[1].data["qq"] == "10001"
        assert segments[3].data["id"] == "999"
        assert segments[7].data["file_id"] == "f1"

    def test_at_with_name(self):
        (seg,) = parse_segments([{"type": "at", "data": {"qq": "10001", "name": "张三"}}])
        assert seg.data["name"] == "张三"

    def test_string_items_tolerated_as_text(self):
        segments = parse_segments(["纯文本", {"type": "face", "data": {"id": 1}}])
        assert segments[0].type == "text"
        assert segments[0].raw_text == "纯文本"
        assert segments[1].type == "face"

    def test_malformed_items_dropped(self):
        segments = parse_segments([
            None, 42, {"type": ""}, {"data": {"text": "no type"}},
            {"type": "text", "data": "not-a-dict"},
            {"type": "text", "data": {"text": "ok"}},
        ])
        # None/42/空 type 丢弃；data 非字典按空处理（text 段保留）
        assert [seg.type for seg in segments] == ["text", "text"]
        assert segments[0].raw_text == ""  # data 非 dict → 空 raw_text，不抛异常

    def test_missing_data_defaults_empty(self):
        (seg,) = parse_segments([{"type": "at"}])
        assert seg.data == {}

    def test_unknown_type_preserved(self):
        (seg,) = parse_segments([{"type": "json", "data": {"data": "{}"}}])
        assert seg.type == "json"
        assert not seg.is_known


# ---------------------------------------------------------------------------
# CQ 码字符串解析与转义还原
# ---------------------------------------------------------------------------

class TestParseCQString:
    def test_plain_text(self):
        segments = parse_segments("就一句普通话")
        assert len(segments) == 1
        assert segments[0].type == "text"
        assert segments[0].raw_text == "就一句普通话"

    def test_single_code(self):
        segments = parse_segments("[CQ:image,file=abc.jpg,url=https://e.com/a.jpg]")
        assert len(segments) == 1
        assert segments[0].type == "image"
        assert segments[0].data == {"file": "abc.jpg", "url": "https://e.com/a.jpg"}

    def test_text_around_codes(self):
        segments = parse_segments("看[CQ:face,id=5]这个")
        assert [seg.type for seg in segments] == ["text", "face", "text"]
        assert segments[0].raw_text == "看"
        assert segments[2].raw_text == "这个"

    def test_text_escapes_restored(self):
        segments = parse_segments("数组写法 arr&#91;0&#93; 与 & 符号")
        assert segments[0].raw_text == "数组写法 arr[0] 与 & 符号"

    def test_param_escapes_restored(self):
        segments = parse_segments("[CQ:image,file=a&#44;b.jpg]")
        assert segments[0].data["file"] == "a,b.jpg"

    def test_literal_escape_sequence_survives(self):
        # 字面 "&#91;" 的转义写法是 "&amp;#91;"（& 最后还原，不被二次还原）
        segments = parse_segments("写字面转义 &amp;#91;")
        assert segments[0].raw_text == "写字面转义 &#91;"
        # 裸 "&#91;" 是 "[" 的转义形态，正常还原
        segments = parse_segments("数组 arr&#91;0&#93;")
        assert segments[0].raw_text == "数组 arr[0]"

    def test_no_value_param(self):
        segments = parse_segments("[CQ:at,qq=10001][CQ:image]")
        assert segments[1].type == "image"
        assert segments[1].data == {}

    def test_mixed_array_and_string_forms(self):
        # 段数组里嵌字符串成员 + CQ 码字符串本体，两种形态都可达
        array = parse_segments(["前缀", {"type": "at", "data": {"qq": "1"}}])
        string = parse_segments("前缀[CQ:at,qq=1]")
        assert [s.type for s in array] == [s.type for s in string] == ["text", "at"]


# ---------------------------------------------------------------------------
# 防御式解析
# ---------------------------------------------------------------------------

class TestDefensiveParsing:
    @pytest.mark.parametrize("raw", [None, 123, 4.5, {"type": "text"}, [], True])
    def test_non_string_non_list_inputs(self, raw):
        # dict/bool 等一律空列表，绝不抛异常
        assert parse_segments(raw) == []

    def test_cq_string_with_stray_brackets(self):
        # 未成对的 [ 不构成 CQ 码，按普通文本保留
        segments = parse_segments("看这个 [ 不是码")
        assert len(segments) == 1
        assert segments[0].raw_text == "看这个 [ 不是码"

    def test_code_with_space_in_params_is_not_a_code(self):
        segments = parse_segments("[CQ:image, file=x.jpg]")
        # ", file=x.jpg" 中 "[^\[\]]*" 允许空格——宽容解析为 image 段
        assert segments[-1].type == "image"


# ---------------------------------------------------------------------------
# 渲染与媒体注册表
# ---------------------------------------------------------------------------

class TestRender:
    def test_text_and_at_and_face(self):
        registry = MediaRegistry()
        text = render_segments(parse_segments([
            {"type": "text", "data": {"text": "hi "}},
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "face", "data": {"id": "5"}},
        ]), registry)
        assert text == "hi @10001[表情:5]"
        assert len(registry) == 0

    def test_at_prefers_name(self):
        registry = MediaRegistry()
        assert render_text(parse_segments(
            [{"type": "at", "data": {"qq": "1", "name": "张三"}}])[0], registry) == "@张三"

    def test_at_empty_falls_back_to_bare_mention(self):
        registry = MediaRegistry()
        assert render_text(parse_segments([{"type": "at", "data": {}}])[0], registry) == "@"

    def test_reply_renders_empty(self):
        registry = MediaRegistry()
        assert render_text(parse_segments(
            [{"type": "reply", "data": {"id": "9"}}])[0], registry) == ""

    def test_media_markers_register_refs(self):
        registry = MediaRegistry()
        text = render_segments(parse_segments([
            {"type": "image", "data": {"url": "https://e.com/a.jpg", "file": "a.jpg",
                                        "file_size": 100}},
            {"type": "image", "data": {"url": "https://e.com/b.jpg"}},
            {"type": "record", "data": {"url": "https://e.com/r.mp3"}},
            {"type": "video", "data": {"file": "v.mp4"}},
        ]), registry)
        assert text == "[图片:m1][图片:m2][语音:m3][视频:m4]"
        assert len(registry) == 4
        ref = registry.get("m1")
        assert ref.type == "image"
        assert ref.url == "https://e.com/a.jpg"
        assert ref.file == "a.jpg"
        assert ref.file_size == 100
        assert registry.get("m3").type == "record"
        assert registry.get("m4").file == "v.mp4"
        assert "m1" in registry and "m9" not in registry
        assert [r.media_id for r in registry.all()] == ["m1", "m2", "m3", "m4"]

    def test_file_marker_with_name(self):
        registry = MediaRegistry()
        text = render_segments(parse_segments([
            {"type": "file", "data": {"name": "报告.pdf", "file_id": "f1",
                                       "file_size": "2048"}},
        ]), registry)
        assert text == "[文件:报告.pdf:m1]"
        ref = registry.get("m1")
        assert ref.name == "报告.pdf"
        assert ref.file == "f1"  # file_id 兜底进 file 侧
        assert ref.file_size == 2048  # 数字字符串也接受

    def test_file_marker_without_name(self):
        registry = MediaRegistry()
        text = render_segments(parse_segments(
            [{"type": "file", "data": {"file_id": "f2"}}])[0:1], registry)
        assert text == "[文件:m1]"
        assert registry.get("m1").name == ""  # 无名时空串

    def test_file_name_fallback_keys(self):
        for key in ("name", "file_name", "filename"):
            registry = MediaRegistry()
            text = render_text(
                parse_segments([{"type": "file", "data": {key: f"n-{key}"}}])[0], registry)
            assert text == f"[文件:n-{key}:m1]", key

    def test_unknown_segment_placeholder(self):
        registry = MediaRegistry()
        text = render_segments(parse_segments([
            {"type": "json", "data": {"data": "{}"}},
            {"type": "forward", "data": {"id": "x"}},
        ]), registry)
        assert text == "[json][forward]"
        assert len(registry) == 0

    def test_media_size_defensive(self):
        registry = MediaRegistry()
        render_text(parse_segments(
            [{"type": "image", "data": {"file_size": "abc"}}])[0], registry)
        assert registry.get("m1").file_size is None
        render_text(parse_segments(
            [{"type": "image", "data": {"size": True}}])[0], registry)
        assert registry.get("m2").file_size is None  # bool 不当尺寸

    def test_text_segment_without_raw_text_uses_data(self):
        # 防御分支：raw_text 为 None 的 text 段（非解析路径构造）回退读 data
        from onebot_plugin.segments import Segment
        registry = MediaRegistry()
        seg = Segment("text", {"text": "回退"})
        assert seg.raw_text is None
        assert render_text(seg, registry) == "回退"


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------

class TestExtractors:
    def test_extract_reply_id(self):
        segments = parse_segments([
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "哈哈"}},
        ])
        assert extract_reply_id(segments) == "42"

    def test_extract_reply_id_missing(self):
        assert extract_reply_id(parse_segments("没引用")) is None
        assert extract_reply_id(parse_segments(
            [{"type": "reply", "data": {}}])) is None
        assert extract_reply_id(parse_segments(
            [{"type": "reply", "data": {"id": "  "}}])) is None

    def test_extract_reply_id_takes_first(self):
        segments = parse_segments([
            {"type": "reply", "data": {"id": "1"}},
            {"type": "reply", "data": {"id": "2"}},
        ])
        assert extract_reply_id(segments) == "1"

    def test_at_targets_dedup_and_order(self):
        segments = parse_segments([
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "at", "data": {"qq": "10002"}},
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "at", "data": {"qq": "all"}},
        ])
        assert at_targets(segments) == ["10001", "10002", "all"]

    def test_at_targets_empty(self):
        assert at_targets(parse_segments("没有 at")) == []

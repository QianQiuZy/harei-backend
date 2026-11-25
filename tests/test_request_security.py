from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import Request
from openpyxl import Workbook, load_workbook

from app.core.rate_limit import client_ip
from app.services.music_workbook import (
    IMPORT_HEADERS,
    IMPORT_SHEET,
    build_performance_template,
    parse_performance_workbook,
)


@pytest.mark.parametrize(
    ("peer", "forwarded", "edge", "trusted_edge", "expected"),
    [
        ("192.0.2.1", "192.0.2.10", "192.0.2.20", "", "192.0.2.1"),
        ("127.0.0.1", "192.0.2.10, 192.0.2.11", "192.0.2.20", "", "192.0.2.11"),
        ("127.0.0.1", "192.0.2.10, ::1", "", "", "192.0.2.10"),
        ("127.0.0.1", "192.0.2.10", "192.0.2.20", "127.0.0.1", "192.0.2.20"),
        ("127.0.0.1", "unspecified", "", "", "127.0.0.1"),
        ("::ffff:192.0.2.1", "", "", "", "192.0.2.1"),
        ("2001:db8:0:0:0:0:0:1", "", "", "", "2001:db8::1"),
    ],
)
def test_client_ip(peer, forwarded, edge, trusted_edge, expected):
    settings = SimpleNamespace(
        trusted_proxy_hosts_list=["127.0.0.1", "::1"],
        trusted_edgeone_proxy_hosts=trusted_edge,
    )
    request = Request(
        {
            "type": "http",
            "client": (peer, 443),
            "headers": [
                (b"x-forwarded-for", forwarded.encode()),
                (b"eo-connecting-ip", edge.encode()),
            ],
        }
    )
    with patch("app.core.rate_limit.get_settings", return_value=settings):
        assert client_ip(request) == expected


def test_box_upload_preserves_image_type_for_preview(tmp_path, monkeypatch):
    from PIL import Image
    from app.api import box

    for name in ("ORIGINAL_DIR", "JPG_DIR", "THUMB_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setattr(box, name, directory)
    for fmt, media_type, suffix in (("GIF", "image/gif", ".gif"), ("PNG", "image/png", ".png")):
        contents = BytesIO()
        Image.new("RGB", (3, 2), "red").save(contents, format=fmt)
        original, jpg, thumb = box.process_uploaded_image(contents.getvalue(), fmt.lower())
        assert original.suffix == suffix
        assert (original == jpg == thumb) is (fmt == "GIF")
        # Old installations may have stored a normal photo under a generic filename.
        legacy = original.rename(original.with_suffix(".bin"))
        response = box.image_response(legacy)
        assert response.media_type == media_type
        assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [2, 3])
async def test_compressed_live_batch_keeps_message_order(version):
    import json
    import zlib
    from unittest.mock import Mock

    import brotli
    from blivedm.clients.ws_base import HEADER_STRUCT, Operation, WebSocketClientBase

    def packet(body, encoding):
        return HEADER_STRUCT.pack(HEADER_STRUCT.size + len(body), HEADER_STRUCT.size,
                                  encoding, Operation.SEND_MSG_REPLY, 1) + body
    import asyncio
    client = WebSocketClientBase(session=Mock(loop=asyncio.get_running_loop()))
    client._handle_command = Mock()
    commands = [{"cmd": "FIRST"}, {"cmd": "SECOND"}]
    data = b"".join(packet(json.dumps(item).encode(), 0) for item in commands)
    compressed = (zlib.compress if version == 2 else brotli.compress)(data)
    await client._parse_ws_message(packet(compressed, version))
    assert [call.args[0] for call in client._handle_command.call_args_list] == commands


def test_music_workbook_keeps_user_strings_as_text_and_imports_valid_rows():
    contents = build_performance_template(
        [SimpleNamespace(title="=1+1", artist="测试", status="active")]
    )
    book = load_workbook(contents)
    assert book["歌曲列表"]["A2"].data_type == "s"
    book[IMPORT_SHEET].append(
        ["=1+1", "2026-11-25", "直播", "https://example.test/song"]
    )
    book[IMPORT_SHEET]["A2"].data_type = "s"
    output = BytesIO()
    book.save(output)
    book.close()
    rows, issues = parse_performance_workbook(output.getvalue())
    assert not issues and rows[0].song_title == "=1+1"


def test_music_workbook_rejects_unexpected_dimensions():
    book = Workbook()
    book.active.title = IMPORT_SHEET
    book.active.append((*IMPORT_HEADERS, "多余列"))
    output = BytesIO()
    book.save(output)
    book.close()
    rows, issues = parse_performance_workbook(output.getvalue())
    assert not rows and issues[0].code == "INVALID_SIZE"

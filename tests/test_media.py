"""媒体层测试：media_upload / media_fetch / 落盘去重 / 超限拒绝。

覆盖：
- media_upload -> media_ack：blob 落盘，返回 media_id
- 同内容重复上传 -> sha256 去重（同一份磁盘、同一 media_id）
- media_fetch -> media_fetch_ack：返回正确 bytes 与 filename
- 超 20MB -> 拒绝（ok=False），不落盘
- media_id 不存在 -> media_fetch_ack ok=False
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest

from astrbot_plugin_txrui.txrui_client import (
    MAX_MEDIA_BYTES,
    MediaStore,
    TxruiConnection,
    TxruiWSServer,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True

    async def recv(self):
        raise RuntimeError("eof")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _server(tmp_path):
    return TxruiWSServer(
        host="127.0.0.1",
        port=0,
        token="tok",
        connector_id="txrui_001",
        media_dir=str(tmp_path / "media"),
    )


def _conn(ws=None):
    return TxruiConnection(
        ws or _FakeWS(),
        connector_id="txrui_001",
        account_ids="wx_001",
        capabilities={"text": True},
    )


def _upload_msg(data: bytes, client_seq="m1", filename="a.png", mime="image/png"):
    return {
        "type": "media_upload",
        "client_seq": client_seq,
        "filename": filename,
        "mime": mime,
        "size": len(data),
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


class TestMediaUpload:
    def test_upload_acks_and_persists(self, tmp_path):
        """media_upload -> media_ack，blob 落盘。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            data = b"\x89PNG-bytes-here"
            await server._handle_media_upload(conn, _upload_msg(data))
            ack = conn.ws.sent[-1]
            assert ack["type"] == "media_ack"
            assert ack["client_seq"] == "m1"
            assert ack["ok"] is True
            assert ack["media_id"]
            assert ack["size"] == len(data)

            entry = server.media_store.get(ack["media_id"])
            assert entry is not None
            assert Path(entry.path).is_file()
            assert Path(entry.path).read_bytes() == data

        _run(scenario())

    def test_same_content_is_deduplicated(self, tmp_path):
        """同内容重复上传 -> 同一 media_id、只占一份磁盘。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            data = b"same-content-bytes"
            await server._handle_media_upload(conn, _upload_msg(data, "m1"))
            first = conn.ws.sent[-1]
            await server._handle_media_upload(conn, _upload_msg(data, "m2"))
            second = conn.ws.sent[-1]
            assert first["media_id"] == second["media_id"]

            blobs = list(Path(server.media_store.root).rglob("*"))
            files = [p for p in blobs if p.is_file()]
            assert len(files) == 1

        _run(scenario())

    def test_oversize_is_rejected(self, tmp_path):
        """超过 20MB -> 拒绝，不落盘。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            # 造一个超过上限的 base64（不真的解码全部字节也要保证超限）
            big = b"\x00" * (MAX_MEDIA_BYTES + 1024)
            await server._handle_media_upload(conn, _upload_msg(big, "m1"))
            ack = conn.ws.sent[-1]
            assert ack["ok"] is False
            assert ack["error"] == "too_large"
            assert ack["media_id"] == ""
            files = [p for p in Path(server.media_store.root).rglob("*") if p.is_file()]
            assert files == []

        _run(scenario())

    def test_empty_media_is_rejected(self, tmp_path):
        """空内容 -> 拒绝。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            await server._handle_media_upload(conn, _upload_msg(b"", "m1"))
            ack = conn.ws.sent[-1]
            assert ack["ok"] is False
            assert ack["error"] == "empty_media"

        _run(scenario())


class TestMediaFetch:
    def test_fetch_returns_correct_bytes(self, tmp_path):
        """media_fetch -> media_fetch_ack，返回正确 bytes 与 filename。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            data = b"binary-payload-\x00\x01\x02"
            await server._handle_media_upload(
                conn, _upload_msg(data, "m1", filename="pic.png", mime="image/png")
            )
            media_id = conn.ws.sent[-1]["media_id"]

            await server._handle_media_fetch(
                conn, {"type": "media_fetch", "client_seq": "f1", "media_id": media_id}
            )
            ack = conn.ws.sent[-1]
            assert ack["type"] == "media_fetch_ack"
            assert ack["client_seq"] == "f1"
            assert ack["ok"] is True
            assert ack["media_id"] == media_id
            assert ack["filename"] == "pic.png"
            assert ack["size"] == len(data)
            assert base64.b64decode(ack["data_b64"]) == data

        _run(scenario())

    def test_fetch_unknown_media_id_fails(self, tmp_path):
        """media_id 不存在 -> ok=False，不返回数据。"""

        async def scenario():
            server = _server(tmp_path)
            conn = _conn()
            await server._handle_media_fetch(
                conn, {"type": "media_fetch", "client_seq": "f1", "media_id": "nope"}
            )
            ack = conn.ws.sent[-1]
            assert ack["ok"] is False
            assert ack["error"] == "media_not_found"
            assert ack["data_b64"] == ""

        _run(scenario())


class TestMediaStore:
    def test_put_is_idempotent_by_content(self, tmp_path):
        """MediaStore.put 同内容返回同一 media_id。"""
        store = MediaStore(tmp_path / "m")
        a = store.put(b"payload", filename="x.bin")
        b = store.put(b"payload", filename="y.bin")
        assert a.media_id == b.media_id
        assert a.path == b.path

    def test_get_recovers_from_disk(self, tmp_path):
        """索引丢失后按内容寻址路径回捞。"""
        store = MediaStore(tmp_path / "m")
        entry = store.put(b"payload", filename="x.bin", mime="application/octet-stream")
        # 模拟进程重启：新建 store（索引为空）
        store2 = MediaStore(tmp_path / "m")
        recovered = store2.get(entry.media_id)
        assert recovered is not None
        assert recovered.size == len(b"payload")

    def test_missing_media_returns_none(self, tmp_path):
        """不存在的 media_id 返回 None。"""
        store = MediaStore(tmp_path / "m")
        assert store.get("doesnotexist") is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

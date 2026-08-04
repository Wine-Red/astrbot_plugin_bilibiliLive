"""danmaku_client 的协议解析与 WBI 签名单元自测（无需网络与 astrbot 环境）。

运行方式: python tests/test_danmaku_protocol.py
"""
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from danmaku_client import (  # noqa: E402
    DanmakuClient,
    LiveStats,
    OP_MESSAGE,
    _get_mixin_key,
    sign_wbi,
)


def pack(op, body, ver=1):
    """构造协议帧（与 DanmakuClient._pack 等价，便于测试）。"""
    return struct.pack(">IHHII", 16 + len(body), 16, ver, op, 1) + body


def make_client():
    return DanmakuClient("123", 456, "测试UP主")


def test_wbi_test_vector():
    """使用 bilibili WBI 文档的官方测试向量验证签名算法。"""
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    mixin = _get_mixin_key(img_key, sub_key)
    assert mixin == "ea1db124af3c7062474693fa704f4ff8", f"mixin key 错误: {mixin}"

    params = sign_wbi(
        {"foo": "114", "bar": "514", "zab": 1919810},
        img_key,
        sub_key,
        wts=1702204169,
    )
    assert params["wts"] == 1702204169
    assert params["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4", params["w_rid"]
    # 签名基于不含 w_rid 的排序参数串（w_rid 为签名后追加，不参与排序）
    signed_query = urlencode(
        dict(sorted((k, v) for k, v in params.items() if k != "w_rid"))
    )
    assert signed_query == "bar=514&foo=114&wts=1702204169&zab=1919810", signed_query


def test_online_rank_count():
    """ONLINE_RANK_COUNT 消息更新同接（真实在线人数）与峰值。"""
    client = make_client()
    frame = pack(
        OP_MESSAGE,
        json.dumps({"cmd": "ONLINE_RANK_COUNT", "data": {"count": 5339, "online_count": 5339}}).encode(),
        ver=0,
    )
    client._handle_binary(frame)
    assert client.stats.online == 5339
    assert client.stats.peak_online == 5339
    # 后续更低的同接不降低峰值
    client._handle_binary(
        pack(
            OP_MESSAGE,
            json.dumps({"cmd": "ONLINE_RANK_COUNT", "data": {"count": 4000, "online_count": 4000}}).encode(),
            ver=0,
        )
    )
    assert client.stats.online == 4000
    assert client.stats.peak_online == 5339
    # 无效值忽略
    client._handle_binary(
        pack(
            OP_MESSAGE,
            json.dumps({"cmd": "ONLINE_RANK_COUNT", "data": {"count": 0}}).encode(),
            ver=0,
        )
    )
    assert client.stats.online == 4000


def test_message_accounting():
    """礼物/舰长/SC 的全额口径累计规则。"""
    client = make_client()
    msgs = [
        {"cmd": "SEND_GIFT", "data": {"coin_type": "gold", "price": 1000}},  # 1元
        {"cmd": "SEND_GIFT", "data": {"coin_type": "silver", "price": 1000}},  # 免费礼物不计
        {"cmd": "COMBO_SEND", "data": {"coin_type": "gold", "price": 5000}},  # 连击汇总不重复计
        {"cmd": "SEND_GIFT", "data": {"coin_type": "gold", "price": 100, "num": 6}},  # 6×0.1元=0.6元
        {"cmd": "GUARD_BUY", "data": {"price": 198000, "gift_name": "舰长"}},  # 198元
        {"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": 50}},  # SC 单位是元
    ]
    data = b"".join(
        pack(OP_MESSAGE, json.dumps(m, separators=(",", ":")).encode(), ver=0)
        for m in msgs
    )
    client._handle_binary(data)
    expected = 1 + 0.6 + 198 + 50  # 礼物 1 元 + 批量 0.6 元 + 舰长 198 元 + SC 50 元
    assert abs(client.stats.total_amount - expected) < 1e-9, client.stats.total_amount


def test_zlib_packets():
    """protover=2 的 zlib 压缩消息包，解压后可能包含多个子包。"""
    client = make_client()
    inner1 = pack(
        OP_MESSAGE,
        json.dumps({"cmd": "SEND_GIFT", "data": {"coin_type": "gold", "price": 5000}}).encode(),
        ver=0,
    )
    inner2 = pack(
        OP_MESSAGE,
        json.dumps({"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": 30}}).encode(),
        ver=0,
    )
    compressed = zlib.compress(inner1 + inner2)
    frame = pack(OP_MESSAGE, compressed, ver=2)
    client._handle_binary(frame)
    expected = 5.0 + 30  # 礼物 5元 + SC 30元
    assert abs(client.stats.total_amount - expected) < 1e-9, client.stats.total_amount


def test_multiple_frames_in_one_packet():
    """一段数据里连续拼接多个帧（WebSocket 分片到达的场景）。"""
    client = make_client()
    frames = [
        pack(
            OP_MESSAGE,
            json.dumps({"cmd": "SEND_GIFT", "data": {"coin_type": "gold", "price": 2000}}).encode(),
            ver=0,
        ),
        pack(
            OP_MESSAGE,
            json.dumps({"cmd": "ONLINE_RANK_COUNT", "data": {"count": 777}}).encode(),
            ver=0,
        ),
        pack(
            OP_MESSAGE,
            json.dumps({"cmd": "SEND_GIFT", "data": {"coin_type": "gold", "price": 2000}}).encode(),
            ver=0,
        ),
    ]
    client._handle_binary(b"".join(frames))
    assert abs(client.stats.total_amount - 4.0) < 1e-9  # 2×2元
    assert client.stats.online == 777


def test_stats_roundtrip():
    """LiveStats 序列化/反序列化往返。"""
    s = LiveStats("1", 456, "名字", total_amount=12.5, online=10, peak_online=99)
    s2 = LiveStats.from_dict(s.to_dict())
    assert s2 == s
    # 兼容旧状态文件（字段缺失或含旧字段）
    s3 = LiveStats.from_dict({"uid": "1", "room_id": 456, "danmaku_count": 5})
    assert s3.total_amount == 0 and s3.uname == ""


def main():
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n全部 {len(tests)} 个测试通过")


if __name__ == "__main__":
    main()

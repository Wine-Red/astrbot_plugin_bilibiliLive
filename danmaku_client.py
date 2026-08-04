"""B 站直播弹幕 WebSocket 客户端：实时统计直播间收入 / 在线人数。

协议要点（B 站直播信息流协议，以实测为准）：
- 通过 getDanmuInfo 接口（需 WBI 签名 + buvid3 cookie）获取 wss 节点与鉴权 token
- 连接后 5 秒内发送鉴权包（op=7）即可开始接收消息；注意：不要再发送空正文包
  （部分文档提到发送 op=7 空包开启推送，实测 2026 年服务端收到后立即断开连接）
- 每 30 秒发送心跳包（op=2）保持连接，心跳回包不携带有效数据（人气值已变为固定占位值）
- 真实在线人数（同接）由 ONLINE_RANK_COUNT 消息实时推送，也可用高能榜接口
  getOnlineGoldRank 的 onlineNum 字段校验/兜底

统计口径（全额口径，即观众付费总额）：
- 收入（元）= 金瓜子礼物（SEND_GIFT，1000 金瓜子=1 元）+ 上舰（GUARD_BUY）+ SC（SUPER_CHAT_MESSAGE，单位直接是元）
- 开播前（连接建立前）的历史收入由高能榜贡献值初始化补齐（贡献值→元）
- 游客模式 uid=0，无需登录 cookie
"""
import asyncio
import hashlib
import json
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 独立测试时无 astrbot 环境
    import logging
    logger = logging.getLogger("bili_danmaku")

# B 站 WBI 签名使用的固定字符重排表（mixinKeyEncTab）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
RANK_INFO_URL = "https://api.live.bilibili.com/xlive/general-interface/v1/rank/getOnlineGoldRank"

# 协议操作码
OP_HEARTBEAT = 2        # 心跳包（上行）
OP_MESSAGE = 5          # 消息包（下行）
OP_AUTH = 7             # 鉴权包（上行）
OP_AUTH_REPLY = 8       # 鉴权回包（下行）

# 协议版本
PROTO_VER_PLAIN = 0     # 明文
PROTO_VER_ZLIB = 2      # zlib 压缩（避免 brotli 依赖，保持零新增依赖）

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# WBI 密钥与 buvid3 的进程内缓存（密钥每日轮换，缓存 8 小时足够）
_wbi_cache: Dict[str, Any] = {}
_buvid3_cache: str = ""


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """按固定重排表生成 mixin key（取重排后前 32 字符）。"""
    orig = img_key + sub_key
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(
    params: Dict[str, Any],
    img_key: str,
    sub_key: str,
    wts: Optional[int] = None,
) -> Dict[str, Any]:
    """对请求参数做 WBI 签名，返回追加了 wts 与 w_rid 的参数表。

    算法：参数按 key 排序后 urlencode，再拼接 mixin key 取 md5 作为 w_rid。
    """
    signed = dict(params)
    signed["wts"] = int(wts) if wts is not None else int(time.time())
    signed = dict(sorted(signed.items()))
    query = urlencode(signed)
    w_rid = hashlib.md5((query + _get_mixin_key(img_key, sub_key)).encode()).hexdigest()
    signed["w_rid"] = w_rid
    return signed


def _build_headers(buvid3: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://live.bilibili.com",
        "Referer": "https://live.bilibili.com/",
    }
    if buvid3:
        headers["Cookie"] = f"buvid3={buvid3}"
    return headers


async def get_buvid3(session: aiohttp.ClientSession) -> str:
    """获取 buvid3 cookie（WBI 接口的前置条件），优先复用已有值。"""
    global _buvid3_cache
    if _buvid3_cache:
        return _buvid3_cache
    for cookie in session.cookie_jar:
        if cookie.key == "buvid3" and cookie.value:
            _buvid3_cache = cookie.value
            return _buvid3_cache
    try:
        async with session.get(SPI_URL, headers=_build_headers()) as resp:
            body = await resp.json()
        b_3 = ((body.get("data") or {}).get("b_3") or "").strip()
        if b_3:
            _buvid3_cache = b_3
    except Exception as e:
        logger.error(f"获取 buvid3 失败: {e}")
    return _buvid3_cache


async def get_wbi_keys(
    session: aiohttp.ClientSession, buvid3: str = ""
) -> tuple:
    """从 nav 接口获取 WBI 的 img_key / sub_key（取图片文件名，每日轮换）。"""
    now = time.time()
    if _wbi_cache.get("expire", 0) > now:
        return _wbi_cache["img_key"], _wbi_cache["sub_key"]
    async with session.get(NAV_URL, headers=_build_headers(buvid3)) as resp:
        body = await resp.json()
    wbi_img = ((body.get("data") or {}).get("wbi_img") or {})
    img_url = wbi_img.get("img_url") or ""
    sub_url = wbi_img.get("sub_url") or ""
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise RuntimeError("获取 WBI 密钥失败（nav 接口未返回 wbi_img）")
    _wbi_cache.update(img_key=img_key, sub_key=sub_key, expire=now + 3600 * 8)
    return img_key, sub_key


async def get_danmu_info(
    session: aiohttp.ClientSession, room_id: int
) -> Dict[str, Any]:
    """获取弹幕服务器节点与鉴权 token，返回 {"host_list": [...], "token": "..."}。"""
    buvid3 = await get_buvid3(session)
    img_key, sub_key = await get_wbi_keys(session, buvid3)
    params = sign_wbi({"id": room_id, "type": 0}, img_key, sub_key)
    async with session.get(
        DANMU_INFO_URL, params=params, headers=_build_headers(buvid3)
    ) as resp:
        body = await resp.json()
    if body.get("code") != 0:
        # B 站风控策略可能调整，签名失败时回退为无签名请求再试一次
        async with session.get(
            DANMU_INFO_URL,
            params={"id": room_id, "type": 0},
            headers=_build_headers(buvid3),
        ) as resp2:
            body = await resp2.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"getDanmuInfo 失败: code={body.get('code')}, "
            f"message={body.get('message', '未知错误')}"
        )
    data = body.get("data") or {}
    host_list = data.get("host_list") or []
    if not host_list:
        raise RuntimeError("getDanmuInfo 未返回弹幕服务器节点")
    return {"host_list": host_list, "token": data.get("token") or ""}


async def get_online_num(
    session: aiohttp.ClientSession, room_id: int, ruid: int
) -> Optional[int]:
    """获取直播间真实在线人数（同接），来自高能榜接口 onlineNum 字段。"""
    try:
        params = {"ruid": ruid, "roomId": room_id, "page": 1, "pageSize": 1}
        async with session.get(
            RANK_INFO_URL, params=params, headers=_build_headers()
        ) as resp:
            body = await resp.json()
        if body.get("code") != 0:
            logger.warning(
                f"获取直播间 {room_id} 在线人数失败: code={body.get('code')}, "
                f"message={body.get('message', '未知错误')}"
            )
            return None
        online = (body.get("data") or {}).get("onlineNum")
        if isinstance(online, (int, float)):
            return int(online)
    except Exception as e:
        logger.error(f"获取直播间 {room_id} 在线人数失败: {e}")
    return None


async def get_gongxian_amount(
    session: aiohttp.ClientSession, room_id: int, ruid: int, max_pages: int = 50
) -> float:
    """获取高能榜累计贡献值（元），用于开播时初始化历史流水。

    贡献值单位换算：1 元 = 10 贡献值（1 贡献值 = 100 金瓜子）。
    与弹幕流脚本同口径：跳过 <=20 贡献值（2 元）的小额项，避免噪音。
    """
    total = 0
    for page in range(1, max_pages + 1):
        try:
            params = {"ruid": ruid, "roomId": room_id, "page": page, "pageSize": 50}
            async with session.get(
                RANK_INFO_URL, params=params, headers=_build_headers()
            ) as resp:
                body = await resp.json()
            if body.get("code") != 0:
                break
            items = (body.get("data") or {}).get("OnlineRankItem") or []
        except Exception as e:
            logger.error(f"获取直播间 {room_id} 高能榜第 {page} 页失败: {e}")
            break
        if not items:
            break
        for item in items:
            score = int(item.get("score") or 0)
            if score > 20:
                total += score
        # 首项已低于阈值说明后续贡献更低，可以提前结束
        if len(items) < 50 or int(items[0].get("score") or 0) < 20:
            break
    return total / 10.0


@dataclass
class LiveStats:
    """单场直播的实时统计（本场收入 / 在线人数）。"""

    uid: str
    room_id: int
    uname: str = ""
    total_amount: float = 0.0  # 本场收入（元）：金瓜子礼物 + 上舰 + SC，全额口径
    online: int = 0            # 最近一次同接（真实在线人数）
    peak_online: int = 0       # 本场同接峰值

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "room_id": self.room_id,
            "uname": self.uname,
            "total_amount": self.total_amount,
            "online": self.online,
            "peak_online": self.peak_online,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LiveStats":
        return cls(
            uid=str(data.get("uid", "")),
            room_id=int(data.get("room_id", 0) or 0),
            uname=str(data.get("uname", "")),
            total_amount=float(data.get("total_amount", 0) or 0),
            online=int(data.get("online", 0) or 0),
            peak_online=int(data.get("peak_online", 0) or 0),
        )


class DanmakuClient:
    """单个直播间的弹幕 WebSocket 客户端。

    启动后持续接收消息并累计到 self.stats，断线自动按指数退避重连，
    直到外部调用 stop() 为止。统计跨重连保留。
    """

    def __init__(
        self,
        uid: str,
        room_id: int,
        uname: str = "",
        stats: Optional[LiveStats] = None,
    ):
        self.uid = str(uid)
        self.room_id = int(room_id)
        self.uname = uname
        self.stats = stats or LiveStats(
            uid=str(uid), room_id=int(room_id), uname=uname
        )
        self.connected = False  # 当前是否已连上弹幕服务器
        self._stopped = False
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task

    def start(self) -> asyncio.Task:
        """启动后台接收任务（重复调用为幂等）。"""
        if self._task and not self._task.done():
            return self._task
        self._stopped = False
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self):
        """停止接收任务并释放连接与会话。"""
        self._stopped = True
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # 使用独立会话，避免与插件主 HTTP 轮询互相挤占连接池
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=4, limit_per_host=2),
            )
        return self._session

    async def _run(self):
        """主循环：获取节点 -> 连接监听 -> 断线后按指数退避重连。"""
        retry = 0
        while not self._stopped:
            try:
                session = await self._ensure_session()
                danmu_info = await get_danmu_info(session, self.room_id)
                await self._connect_and_listen(session, danmu_info)
                if self._stopped:
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"UID {self.uid}（{self.uname}）弹幕通道异常: {e}")
            if self._stopped:
                break
            retry += 1
            delay = min(300, 5 * (2 ** min(retry - 1, 5)))
            logger.info(f"UID {self.uid} 弹幕通道将在 {delay}s 后重连（第 {retry} 次）")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
        self.connected = False
        self._ws = None

    async def _connect_and_listen(self, session, danmu_info: Dict[str, Any]):
        """依次尝试各 wss 节点，连接成功后完成鉴权、心跳与收包。"""
        ws = None
        last_error: Optional[Exception] = None
        for host in danmu_info["host_list"][:3]:
            url = f"wss://{host.get('host')}:{host.get('wss_port', 443)}/sub"
            try:
                ws = await session.ws_connect(url, headers=_build_headers())
                break
            except Exception as e:
                last_error = e
                logger.warning(f"连接弹幕服务器 {url} 失败: {e}")
        if ws is None:
            raise last_error or RuntimeError("无法连接弹幕服务器")
        self._ws = ws
        self.connected = True
        try:
            # 1) 鉴权包（需在连接后 5 秒内发送），随后直接进入心跳+收包循环。
            # 注意：不要发送文档中提到的 op=7 空正文包，实测服务端收到后会立即断开连接。
            auth_body = json.dumps(
                {
                    "uid": 0,  # 游客模式
                    "roomid": self.room_id,
                    "protover": PROTO_VER_ZLIB,
                    "platform": "web",
                    "type": 2,
                    "key": danmu_info.get("token") or "",
                },
                separators=(",", ":"),
            ).encode()
            await ws.send_bytes(self._pack(OP_AUTH, auth_body))
            # 2) 等待鉴权回包（op=8），超时也继续（部分节点不回包也能收到消息）
            await self._wait_auth_reply(ws)
            if self._stopped:
                return
            # 3) 心跳 + 收包循环
            await self._listen(ws)
        finally:
            self.connected = False
            self._ws = None
            if not ws.closed:
                await ws.close()

    async def _wait_auth_reply(self, ws, timeout: float = 5.0):
        """等待鉴权回包（op=8），超时则继续（部分节点不回包也能收到消息）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    ws.receive(), timeout=max(0.1, deadline - time.time())
                )
            except asyncio.TimeoutError:
                return
            if msg.type == aiohttp.WSMsgType.BINARY:
                for frame in self._iter_frames(msg.data):
                    parsed = self._parse_frame(frame)
                    if parsed is None:
                        continue
                    op, _ver, _body = parsed
                    if op == OP_AUTH_REPLY:
                        return
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.ERROR,
            ):
                raise RuntimeError("鉴权前连接被关闭")

    async def _listen(self, ws):
        """收包循环：每 30 秒发送心跳包，心跳回包更新人气值。

        按“距上次心跳的剩余时间”计时，避免高弹幕量直播间因消息不断
        而迟迟不发心跳，导致超过 60 秒被服务端强制断开。
        """
        last_heartbeat = time.time()
        while not self._stopped:
            remaining = max(1.0, 30 - (time.time() - last_heartbeat))
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                try:
                    await ws.send_bytes(self._pack(OP_HEARTBEAT, b"[object Object]"))
                    last_heartbeat = time.time()
                except Exception:
                    return
                continue
            if msg.type == aiohttp.WSMsgType.BINARY:
                self._handle_binary(msg.data)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.ERROR,
            ):
                return

    @staticmethod
    def _pack(op: int, body: bytes) -> bytes:
        """构造协议数据包：16 字节包头（总长/头长/版本/操作码/序号）+ 正文。"""
        return struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1) + body

    @staticmethod
    def _iter_frames(data: bytes):
        """把一段数据按 16 字节包头切成完整帧序列。"""
        offset = 0
        n = len(data)
        while offset + 16 <= n:
            total_len = struct.unpack(">I", data[offset : offset + 4])[0]
            if total_len < 16 or offset + total_len > n:
                break
            yield data[offset : offset + total_len]
            offset += total_len

    @staticmethod
    def _parse_frame(frame: bytes):
        """解析单帧，返回 (op, ver, body)；zlib 压缩时先解压正文。"""
        if len(frame) < 16:
            return None
        total_len, header_len, ver, op, _seq = struct.unpack(">IHHII", frame[:16])
        if total_len < header_len or len(frame) < total_len:
            return None
        body = frame[header_len:total_len]
        if ver == PROTO_VER_ZLIB:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                return None
        return op, ver, body

    def _handle_binary(self, data: bytes):
        """处理一段二进制数据：切帧 -> 解析 -> 更新统计。"""
        for frame in self._iter_frames(data):
            parsed = self._parse_frame(frame)
            if parsed is None:
                continue
            op, ver, body = parsed
            if op != OP_MESSAGE:
                continue
            if ver == PROTO_VER_ZLIB:
                # 解压后的数据可能包含多个子包
                for sub_frame in self._iter_frames(body):
                    sub = self._parse_frame(sub_frame)
                    if sub is not None and sub[0] == OP_MESSAGE:
                        self._handle_message(sub[2])
            else:
                self._handle_message(body)

    def _handle_message(self, body: bytes):
        """解析 op=5 消息正文并累计到本场统计。"""
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict):
            return
        cmd = obj.get("cmd") or ""
        if cmd.startswith("SEND_GIFT"):
            data = obj.get("data") or {}
            # 连击时每个礼物都会单独下发一条 SEND_GIFT（num 通常为 1），
            # COMBO_SEND 仅为汇总消息，若再计入会重复计算，因此忽略。
            if data.get("coin_type") == "gold":  # 银瓜子为免费礼物，不计入
                self.stats.total_amount += (
                    float(data.get("price") or 0)
                    * float(data.get("num") or 1)
                    / 1000.0
                )
        elif cmd.startswith("GUARD_BUY"):
            data = obj.get("data") or {}
            self.stats.total_amount += float(data.get("price") or 0) / 1000.0
        elif cmd.startswith("SUPER_CHAT_MESSAGE"):
            data = obj.get("data") or {}
            self.stats.total_amount += float(data.get("price") or 0)  # SC 单位是元
        elif cmd == "ONLINE_RANK_COUNT":
            # 真实在线人数（同接）实时推送，data.count 与 online_count 同值
            data = obj.get("data") or {}
            value = data.get("online_count") or data.get("count")
            if isinstance(value, (int, float)):
                self._update_online(int(value))

    def _update_online(self, online: int):
        if online <= 0:
            return
        self.stats.online = online
        if online > self.stats.peak_online:
            self.stats.peak_online = online

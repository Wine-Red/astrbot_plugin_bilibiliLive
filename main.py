import asyncio
import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 兼容旧版 AstrBot
    get_astrbot_data_path = None

@register("bili_live_notice", "Wine-Red", "监控 B 站 UP 主直播状态，并在开播、关播或直播信息变更时向当前会话发送通知。", "1.0.0", "https://github.com/Wine-Red/astrbot_plugin_bilibiliLive")
class BiliLiveNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.check_interval = int(self.config.get("check_interval", 60))
        self.max_monitors = int(self.config.get("max_monitors", 50))
        
        self.live_status_cache: Dict[str, int] = {}  # 缓存直播状态
        self.live_start_times: Dict[str, float] = {} # 记录开播时间
        self.live_meta_cache: Dict[str, Dict[str, str]] = {}  # 记录最近一次抓取到的标题/封面/分区
        self.live_peak_online: Dict[str, int] = {}  # 记录本场人气值峰值
        self.uid_error_counts: Dict[str, int] = {}
        self.uid_skip_until: Dict[str, float] = {}
        self.online_query_skip_until = 0.0
        self.online_query_warn_at = 0.0
        
        self.current_interval = self.check_interval
        self._last_rate_limited = False
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.monitor_task = None
        self.session = None
        
        # 状态文件路径，遵循 AstrBot 的插件数据目录规范。
        self.state_file = self._get_data_dir() / "monitor_state.json"
        
        # 启动初始化任务
        asyncio.create_task(self.initialize())
        
    def _get_data_dir(self) -> Path:
        plugin_name = getattr(self, "name", "bili_live_notice")
        if get_astrbot_data_path:
            base = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        else:
            base = Path(os.path.expanduser("~")) / ".astrbot" / "plugin_data" / plugin_name
        base.mkdir(parents=True, exist_ok=True)
        return base

    @staticmethod
    def _default_status() -> Dict[str, Any]:
        return {"live_status": 0, "room_id": 0, "title": "", "uname": ""}

    @staticmethod
    def _normalize_text(value: Any, default: str = "") -> str:
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    @staticmethod
    def _is_empty_status(status_info: Dict[str, Any]) -> bool:
        return (not status_info.get("uname")) and int(status_info.get("room_id", 0) or 0) == 0

    @staticmethod
    def _build_bilibili_headers(content_type: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://live.bilibili.com",
            "Referer": "https://live.bilibili.com/",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _get_uid_arg(event: AstrMessageEvent) -> Optional[str]:
        args = event.message_str.strip().split()
        if len(args) < 2:
            return None
        uid = args[1].strip()
        if not uid.isdigit():
            return ""
        return uid

    @staticmethod
    def _extract_live_start_time(status_info: Dict[str, Any]) -> Optional[float]:
        live_time = status_info.get("live_time")
        if live_time in (None, "", 0, "0"):
            return None

        if isinstance(live_time, (int, float)):
            return float(live_time) if live_time > 0 else None

        if isinstance(live_time, str):
            raw_value = live_time.strip()
            if not raw_value:
                return None

            if raw_value.isdigit():
                timestamp = int(raw_value)
                return float(timestamp) if timestamp > 0 else None

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.datetime.strptime(raw_value, fmt).timestamp()
                except ValueError:
                    continue

        return None

    @classmethod
    def _extract_status_meta(cls, status_info: Dict[str, Any]) -> Dict[str, str]:
        return {
            "title": cls._normalize_text(status_info.get("title"), "无标题"),
            "area_name": cls._normalize_text(status_info.get("area_name"), "未知"),
            # 变更监控只使用更稳定的封面字段，避免 keyframe 高频刷新导致误报。
            "cover_url": cls._normalize_text(
                status_info.get("cover_from_user") or status_info.get("user_cover"),
                "",
            ),
        }

    @staticmethod
    def _format_change_value(value: str) -> str:
        text = str(value).strip()
        return text if text else "未设置"

    async def _broadcast_meta_changes(
        self,
        uid: str,
        status_info: Dict[str, Any],
        sessions: list,
        previous_meta: Dict[str, str],
        current_meta: Dict[str, str],
        is_live: bool,
    ):
        event_prefix = "" if is_live else "offline_"
        if previous_meta.get("title", "") != current_meta["title"]:
            await self.broadcast_event(
                uid,
                status_info,
                sessions,
                event_type=f"{event_prefix}title_change",
                old_value=previous_meta.get("title", ""),
                new_value=current_meta["title"],
            )
        if previous_meta.get("area_name", "") != current_meta["area_name"]:
            await self.broadcast_event(
                uid,
                status_info,
                sessions,
                event_type=f"{event_prefix}area_change",
                old_value=previous_meta.get("area_name", ""),
                new_value=current_meta["area_name"],
            )
        if previous_meta.get("cover_url", "") != current_meta["cover_url"]:
            await self.broadcast_event(
                uid,
                status_info,
                sessions,
                event_type=f"{event_prefix}cover_change",
                old_value=previous_meta.get("cover_url", ""),
                new_value=current_meta["cover_url"],
            )

    async def ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=10, limit_per_host=5)
            )
            logger.info("HTTP会话已创建")
        
    async def initialize(self):
        async with self._init_lock:
            if self._initialized: return
            try:
                logger.info("正在初始化B站开播监测插件...")
                await self.ensure_session()
                self.load_state()
                
                if not self.monitor_task or self.monitor_task.done():
                    self.monitor_task = asyncio.create_task(self.monitor_live_status())
                    logger.info("监控任务已启动")
                
                self._initialized = True
                logger.info("B站开播监测插件初始化完成")
            except Exception as e:
                logger.error(f"插件初始化失败: {e}")
                await self._cleanup_resources()
                raise

    def load_state(self):
        try:
            if self.state_file.exists():
                with self.state_file.open('r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.live_status_cache = data.get('live_status_cache', {})
                    self.live_start_times = data.get('live_start_times', {})
                    self.live_meta_cache = data.get('live_meta_cache', {})
                    self.live_peak_online = data.get('live_peak_online', {})
        except Exception as e:
            logger.error(f"加载状态文件失败: {e}")

    def save_state(self):
        try:
            data = {
                'live_status_cache': self.live_status_cache,
                'live_start_times': self.live_start_times,
                'live_meta_cache': self.live_meta_cache,
                'live_peak_online': self.live_peak_online,
            }
            with self.state_file.open('w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态文件失败: {e}")

    def save_config(self):
        if hasattr(self.config, 'save_config'):
            try:
                self.config.save_config()
            except Exception as e:
                logger.error(f"保存配置失败: {e}")
        self.save_state()

    def _get_sessions_list(self) -> list:
        sessions = self.config.get("sessions", [])
        if isinstance(sessions, list):
            return sessions
        try:
            self.config["sessions"] = []
        except Exception:
            pass
        return []

    def get_all_monitored_uids(self) -> Set[str]:
        uids = set()
        sessions = self.config.get("sessions", [])
        if isinstance(sessions, list):
            for session_config in sessions:
                if isinstance(session_config, dict):
                    session_uids = session_config.get("uids", [])
                    if isinstance(session_uids, list):
                        uids.update([str(u) for u in session_uids])
        return uids

    async def get_live_status(self, uid: str) -> Dict:
        try:
            batch = await self.get_live_status_batch([uid])
            if uid in batch:
                return batch[uid]
        except Exception as e:
            logger.error(f"获取UID {uid} 直播状态失败: {e}")
        return self._default_status()
    
    async def get_live_status_batch(self, uids: list) -> Dict[str, Dict]:
        result_map: Dict[str, Dict] = {}
        if not uids:
            return result_map
        try:
            await self.ensure_session()
            url = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
            data = {"uids": [int(u) for u in uids]}
            headers = self._build_bilibili_headers("application/json")
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.post(url, json=data, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    body = await response.json()
                    if body.get("code") == 0:
                        self._last_rate_limited = False
                        data_obj = body.get("data") or {}
                        if isinstance(data_obj, dict):
                            for u in uids:
                                key = str(u)
                                user_data = data_obj.get(key)
                                if user_data:
                                    result_map[key] = user_data
                        elif isinstance(data_obj, list):
                            for entry in data_obj:
                                uid_val = str(entry.get("uid") or entry.get("mid") or "")
                                if uid_val:
                                    result_map[uid_val] = entry
                    else:
                        logger.warning(f"B站API返回错误码: {body.get('code')}, 消息: {body.get('message', '未知错误')}")
                elif response.status in (429, 412, 414):
                    self._last_rate_limited = True
                    logger.warning(f"B站API请求受限，状态码: {response.status}")
                else:
                    logger.warning(f"B站API请求失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"批量获取直播状态失败: {e}")
        finally:
            for u in uids:
                if str(u) not in result_map:
                    result_map[str(u)] = self._default_status()
        return result_map

    async def get_room_online(self, room_id: int) -> Optional[int]:
        if not room_id:
            return None
        now = time.time()
        if now < self.online_query_skip_until:
            return None
        try:
            await self.ensure_session()
            url = "https://api.live.bilibili.com/room/v1/Room/get_info"
            headers = self._build_bilibili_headers()
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.get(
                url,
                params={"room_id": room_id},
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    if response.status in (412, 429, 414):
                        self._last_rate_limited = True
                        self.online_query_skip_until = now + max(300, self.check_interval * 3)
                        if now >= self.online_query_warn_at:
                            logger.warning(
                                f"获取直播间人气值请求受限，状态码: {response.status}，将在 {int(self.online_query_skip_until - now)} 秒后重试"
                            )
                            self.online_query_warn_at = now + 300
                        return None
                    logger.warning(f"获取直播间 {room_id} 人气值失败，状态码: {response.status}")
                    return None
                body = await response.json()
                if body.get("code") != 0:
                    logger.warning(
                        f"获取直播间 {room_id} 人气值失败，错误码: {body.get('code')}, 消息: {body.get('message', '未知错误')}"
                    )
                    return None
                data = body.get("data") or {}
                online = data.get("online")
                if isinstance(online, (int, float)):
                    return int(online)
                if isinstance(online, str) and online.isdigit():
                    return int(online)
                return None
        except Exception as e:
            logger.error(f"获取直播间 {room_id} 人气值失败: {e}")
        return None

    async def monitor_live_status(self):
        consecutive_errors = 0
        max_consecutive_errors = 5
        while True:
            try:
                all_uids = list(self.get_all_monitored_uids())
                if not all_uids:
                    await asyncio.sleep(self.check_interval)
                    continue
                
                now = time.time()
                uids_to_query = [uid for uid in all_uids if self.uid_skip_until.get(uid, 0) <= now]
                
                status_map = {}
                # 分块请求，每组40个
                for i in range(0, len(uids_to_query), 40):
                    batch = uids_to_query[i:i+40]
                    batch_result = await self.get_live_status_batch(batch)
                    status_map.update(batch_result)
                
                # 获取当前所有会话配置 (列表)
                sessions = self._get_sessions_list()
                
                for uid in uids_to_query:
                    current_status = status_map.get(uid, self._default_status())
                    is_empty = self._is_empty_status(current_status)

                    if is_empty:
                        cnt = self.uid_error_counts.get(uid, 0) + 1
                        self.uid_error_counts[uid] = cnt
                        self.uid_skip_until[uid] = now + min(300, 30 * cnt)
                        continue

                    self.uid_error_counts.pop(uid, None)
                    self.uid_skip_until.pop(uid, None)

                    live_status = current_status.get("live_status", 0)
                    previous_status = self.live_status_cache.get(uid, 0)
                    current_meta = self._extract_status_meta(current_status)
                    room_id = int(current_status.get("room_id", 0) or 0)
                    
                    if live_status == 1 and previous_status != 1:
                        self.live_start_times[uid] = self._extract_live_start_time(current_status) or now
                        self.live_meta_cache[uid] = current_meta
                        self.live_status_cache[uid] = live_status
                        online = await self.get_room_online(room_id)
                        if online is not None:
                            self.live_peak_online[uid] = online
                        await self.broadcast_event(uid, current_status, sessions, event_type="live")
                    
                    elif previous_status == 1 and live_status != 1:
                        live_start_time = self.live_start_times.pop(uid, None)
                        peak_online = self.live_peak_online.pop(uid, None)
                        self.live_meta_cache[uid] = current_meta
                        self.live_status_cache[uid] = live_status
                        await self.broadcast_event(
                            uid,
                            current_status,
                            sessions,
                            event_type="end",
                            live_start_time=live_start_time,
                            peak_online=peak_online,
                        )
                        
                    elif live_status == 1 and previous_status == 1:
                        # 状态未改变，仅更新缓存避免异常回退
                        self.live_status_cache[uid] = live_status
                        if uid not in self.live_start_times:
                            self.live_start_times[uid] = self._extract_live_start_time(current_status) or now
                        previous_meta = self.live_meta_cache.get(uid)
                        if previous_meta:
                            await self._broadcast_meta_changes(
                                uid, current_status, sessions, previous_meta, current_meta, is_live=True
                            )
                        self.live_meta_cache[uid] = current_meta
                        online = await self.get_room_online(room_id)
                        if online is not None:
                            self.live_peak_online[uid] = max(self.live_peak_online.get(uid, 0), online)
                        
                    elif live_status != 1 and previous_status != 1:
                        # 关播状态下仍可检测标题、封面、分区等资料变更
                        self.live_status_cache[uid] = live_status
                        previous_meta = self.live_meta_cache.get(uid)
                        if previous_meta:
                            await self._broadcast_meta_changes(
                                uid, current_status, sessions, previous_meta, current_meta, is_live=False
                            )
                        self.live_meta_cache[uid] = current_meta
                
                self.save_state()
                consecutive_errors = 0
                
                await asyncio.sleep(self.current_interval)
                if self._last_rate_limited:
                    self.current_interval = min(300, max(self.check_interval, int(self.current_interval * 2)))
                else:
                    self.current_interval = max(self.check_interval, int(self.current_interval * 0.75))
                
            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"监控任务出错: {e}")
                if consecutive_errors >= max_consecutive_errors:
                    await asyncio.sleep(min(300, 60 * consecutive_errors))
                else:
                    await asyncio.sleep(self.current_interval)

    async def broadcast_event(
        self,
        uid: str,
        status_info: Dict,
        sessions: list,
        event_type: str,
        live_start_time: Optional[float] = None,
        peak_online: Optional[int] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
    ):
        uname = status_info.get("uname", "未知UP主")
        title = status_info.get("title", "无标题")
        room_id = status_info.get("room_id", 0)
        area_name = status_info.get("area_name", "未知")
        cover_url = (
            status_info.get("cover_from_user")
            or status_info.get("user_cover")
            or status_info.get("keyframe")
            or ""
        )
        
        for session_config in sessions:
            if not isinstance(session_config, dict):
                continue
            session_id = session_config.get("session_id")
            if not session_id:
                continue
                
            session_uids = [str(u) for u in session_config.get("uids", [])]
            if uid not in session_uids:
                continue
            session_uids = [str(u) for u in session_config.get("uids", []) if str(u).strip()]
            if uid not in session_uids:
                continue
            
            try:
                if event_type == "live" and session_config.get("enable_notifications", True):
                    chain = MessageChain().message(f"🔴 {uname} 开播了！\n")
                    if cover_url:
                        chain.url_image(cover_url)
                    chain.message(f"标题: {title}\n分区: {area_name}\n直播间: https://live.bilibili.com/{room_id}")
                    await self.context.send_message(session_id, chain)
                
                elif (
                    event_type == "end"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_end_notifications", True)
                ):
                    start_time = live_start_time if live_start_time is not None else self.live_start_times.get(uid, 0)
                    duration_str = "未知"
                    peak_online_str = "未知"
                    if start_time > 0:
                        duration = int(time.time() - start_time)
                        hours = duration // 3600
                        minutes = (duration % 3600) // 60
                        if hours > 0:
                            duration_str = f"{hours}小时{minutes}分钟"
                        else:
                            duration_str = f"{minutes}分钟"
                    if isinstance(peak_online, int) and peak_online >= 0:
                        peak_online_str = str(peak_online)
                            
                    chain = MessageChain().message(f"⚫ {uname} 已结束直播\n")
                    if cover_url:
                        chain.url_image(cover_url)
                    chain.message(f"直播时长: {duration_str}\n本场人气值: {peak_online_str}")
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "title_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_title_change_notifications", True)
                ):
                    chain = MessageChain().message(f"📝 {uname} 修改了直播标题\n")
                    chain.message(
                        f"旧标题: {self._format_change_value(old_value or '')}\n"
                        f"新标题: {self._format_change_value(new_value or '')}\n"
                        f"直播间: https://live.bilibili.com/{room_id}"
                    )
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "area_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_area_change_notifications", True)
                ):
                    chain = MessageChain().message(f"🧭 {uname} 切换了直播分区\n")
                    if cover_url:
                        chain.url_image(cover_url)
                    chain.message(
                        f"旧分区: {self._format_change_value(old_value or '')}\n"
                        f"新分区: {self._format_change_value(new_value or '')}\n"
                        f"当前标题: {title}\n"
                        f"直播间: https://live.bilibili.com/{room_id}"
                    )
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "cover_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_cover_change_notifications", True)
                ):
                    chain = MessageChain().message(f"🖼️ {uname} 更换了直播封面\n")
                    if new_value:
                        chain.url_image(new_value)
                    chain.message(f"当前标题: {title}\n直播间: https://live.bilibili.com/{room_id}")
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "offline_title_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_offline_title_change_notifications", False)
                ):
                    chain = MessageChain().message(f"📝 {uname} 在关播状态下修改了标题\n")
                    chain.message(
                        f"旧标题: {self._format_change_value(old_value or '')}\n"
                        f"新标题: {self._format_change_value(new_value or '')}\n"
                        f"直播间: https://live.bilibili.com/{room_id}"
                    )
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "offline_area_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_offline_area_change_notifications", False)
                ):
                    chain = MessageChain().message(f"🧭 {uname} 在关播状态下调整了分区\n")
                    chain.message(
                        f"旧分区: {self._format_change_value(old_value or '')}\n"
                        f"新分区: {self._format_change_value(new_value or '')}\n"
                        f"当前标题: {title}\n"
                        f"直播间: https://live.bilibili.com/{room_id}"
                    )
                    await self.context.send_message(session_id, chain)

                elif (
                    event_type == "offline_cover_change"
                    and session_config.get("enable_notifications", True)
                    and session_config.get("enable_offline_cover_change_notifications", False)
                ):
                    chain = MessageChain().message(f"🖼️ {uname} 在关播状态下更换了封面\n")
                    if new_value:
                        chain.url_image(new_value)
                    chain.message(f"当前标题: {title}\n直播间: https://live.bilibili.com/{room_id}")
                    await self.context.send_message(session_id, chain)
                    
            except Exception as e:
                logger.error(f"发送通知失败 (会话: {session_id}, UP主: {uname}): {e}")

    def get_session_config(self, session_id: str) -> Optional[Dict]:
        sessions = self.config.get("sessions", [])
        if not isinstance(sessions, list):
            return None
        for session_config in sessions:
            if isinstance(session_config, dict) and session_config.get("session_id") == session_id:
                return session_config
        return None

    @filter.command("监控列表")
    async def list_monitors(self, event: AstrMessageEvent):
        """查看当前会话正在监控的全部 UP 主。"""
        session_id = event.unified_msg_origin
        session_config = self.get_session_config(session_id)
        uids = [str(u) for u in (session_config or {}).get("uids", [])]
        
        if not uids:
            yield event.plain_result("📝 当前会话没有监控任何UP主")
            return
        
        message = "📝 当前会话监控列表:\n"
        for uid in uids:
            status_info = await self.get_live_status(uid)
            uname = status_info.get("uname", "未知UP主")
            live_status = "🔴 直播中" if status_info.get("live_status") == 1 else "⚫ 未开播"
            message += f"• {uname}(UID:{uid}) - {live_status}\n"
        
        yield event.plain_result(message.strip())

    @filter.command("检查直播")
    async def check_live(self, event: AstrMessageEvent):
        """手动查询一个 UID 的当前直播状态。"""
        uid = self._get_uid_arg(event)
        if uid is None:
            yield event.plain_result("❌ 使用方法: /检查直播 <UID>")
            return
        if uid == "":
            yield event.plain_result("❌ UID必须是数字")
            return

        status_info = await self.get_live_status(uid)
        if not status_info.get("uname"):
            yield event.plain_result(f"❌ 未找到UID为 {uid} 的UP主")
            return
            
        uname = status_info.get("uname", "未知UP主")
        if status_info.get("live_status") == 1:
            title = status_info.get("title", "无标题")
            room_id = status_info.get("room_id", 0)
            area_name = status_info.get("area_name", "未知")
            cover_url = status_info.get("cover_from_user") or status_info.get("keyframe") or ""
            
            chain = MessageChain().message(f"🔴 {uname} 正在直播\n")
            if cover_url:
                chain.url_image(cover_url)
            chain.message(f"标题: {title}\n分区: {area_name}\n直播间: https://live.bilibili.com/{room_id}")
            yield event.chain_result(chain)
        else:
            yield event.plain_result(f"⚫ {uname} 当前未开播")

    @filter.command("插件状态")
    async def plugin_status(self, event: AstrMessageEvent):
        """显示插件运行状态和当前监控规模。"""
        all_uids = self.get_all_monitored_uids()
        sessions = self.config.get("sessions", [])
        sessions_count = len(sessions) if isinstance(sessions, list) else 0
        
        message = "🔧 插件运行状态:\n"
        message += f"• HTTP会话: {'✅ 正常' if self.session and not self.session.closed else '❌ 异常'}\n"
        message += f"• 监控任务: {'✅ 运行中' if self.monitor_task and not self.monitor_task.done() else '❌ 已停止'}\n"
        message += f"• 监控配置: 共 {sessions_count} 个会话, {len(all_uids)} 个去重UP主\n"
        
        yield event.plain_result(message)

    async def _cleanup_resources(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def terminate(self):
        logger.info("正在停止B站开播监测插件...")
        self.save_config()
        await self._cleanup_resources()
        logger.info("B站开播监测插件已完全停止")

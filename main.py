import asyncio
import aiohttp
import json
import os
import time
from typing import Dict, Set
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

@register("bili_live_notice", "Wine-Red", "B站UP主开播监测插件", "1.0.0", "https://github.com/Wine-Red/astrbot_plugin_bilibiliLive")
class BiliLiveNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.check_interval = int(self.config.get("check_interval", 60))
        self.max_monitors = int(self.config.get("max_monitors", 50))
        
        self.live_status_cache: Dict[str, int] = {}  # 缓存直播状态
        self.live_start_times: Dict[str, float] = {} # 记录开播时间
        self.uid_error_counts: Dict[str, int] = {}
        self.uid_skip_until: Dict[str, float] = {}
        
        self.current_interval = self.check_interval
        self._last_rate_limited = False
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.monitor_task = None
        self.session = None
        
        # 状态文件路径
        self.state_file = os.path.join(self._get_data_dir(), "monitor_state.json")
        
        # 启动初始化任务
        asyncio.create_task(self.initialize())
        
    def _get_data_dir(self) -> str:
        base = os.path.join(os.path.expanduser("~"), ".astrbot", "bili_live_notice")
        os.makedirs(base, exist_ok=True)
        return base

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
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.live_status_cache = data.get('live_status_cache', {})
                    self.live_start_times = data.get('live_start_times', {})
        except Exception as e:
            logger.error(f"加载状态文件失败: {e}")

    def save_state(self):
        try:
            data = {
                'live_status_cache': self.live_status_cache,
                'live_start_times': self.live_start_times
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
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
        sessions = self._get_sessions_list()
        for session_config in sessions:
            if not isinstance(session_config, dict):
                continue
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
        return {"live_status": 0, "room_id": 0, "title": "", "uname": ""}
    
    async def get_live_status_batch(self, uids: list) -> Dict[str, Dict]:
        result_map: Dict[str, Dict] = {}
        if not uids:
            return result_map
        try:
            await self.ensure_session()
            url = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
            data = {"uids": [int(u) for u in uids]}
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
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
                    result_map[str(u)] = {"live_status": 0, "room_id": 0, "title": "", "uname": ""}
        return result_map

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
                
                # 获取当前所有会话配置
                sessions = self._get_sessions_list()
                
                for uid in uids_to_query:
                    current_status = status_map.get(uid, {})
                    live_status = current_status.get("live_status", 0)
                    previous_status = self.live_status_cache.get(uid, 0)
                    
                    if live_status == 1 and previous_status != 1:
                        self.live_start_times[uid] = now
                        await self.broadcast_event(uid, current_status, sessions, event_type="live")
                    
                    if previous_status == 1 and live_status != 1:
                        await self.broadcast_event(uid, current_status, sessions, event_type="end")
                        self.live_start_times.pop(uid, None)
                    
                    self.live_status_cache[uid] = live_status
                    
                    is_empty = (not current_status.get("uname")) and current_status.get("room_id", 0) == 0
                    if is_empty:
                        cnt = self.uid_error_counts.get(uid, 0) + 1
                        self.uid_error_counts[uid] = cnt
                        self.uid_skip_until[uid] = now + min(300, 30 * cnt)
                    else:
                        self.uid_error_counts.pop(uid, None)
                        self.uid_skip_until.pop(uid, None)
                
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

    async def broadcast_event(self, uid: str, status_info: Dict, sessions: list, event_type: str):
        uname = status_info.get("uname", "未知UP主")
        title = status_info.get("title", "无标题")
        room_id = status_info.get("room_id", 0)
        area_name = status_info.get("area_name", "未知")
        cover_url = status_info.get("cover_from_user") or status_info.get("keyframe") or ""
        
        for session_config in sessions:
            if not isinstance(session_config, dict):
                continue
            session_id = str(session_config.get("session_id", "")).strip()
            if not session_id:
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
                    start_time = self.live_start_times.get(uid, 0)
                    duration_str = "未知"
                    if start_time > 0:
                        duration = int(time.time() - start_time)
                        hours = duration // 3600
                        minutes = (duration % 3600) // 60
                        if hours > 0:
                            duration_str = f"{hours}小时{minutes}分钟"
                        else:
                            duration_str = f"{minutes}分钟"
                            
                    chain = MessageChain().message(f"⚫ {uname} 已结束直播\n")
                    if cover_url:
                        chain.url_image(cover_url)
                    chain.message(f"直播时长: {duration_str}")
                    await self.context.send_message(session_id, chain)
                    
            except Exception as e:
                logger.error(f"发送通知失败 (会话: {session_id}, UP主: {uname}): {e}")

    def get_session_config(self, session_id: str) -> Dict:
        session_id = str(session_id).strip()
        sessions = self._get_sessions_list()
        for session_config in sessions:
            if not isinstance(session_config, dict):
                continue
            if str(session_config.get("session_id", "")).strip() == session_id:
                session_config.setdefault("uids", [])
                if not isinstance(session_config.get("uids"), list):
                    session_config["uids"] = []
                session_config.setdefault("enable_notifications", True)
                session_config.setdefault("enable_end_notifications", True)
                return session_config

        new_session_config = {
            "session_id": session_id,
            "uids": [],
            "enable_notifications": True,
            "enable_end_notifications": True,
        }
        sessions.append(new_session_config)
        try:
            self.config["sessions"] = sessions
        except Exception:
            pass
        return new_session_config

    def cleanup_unmonitored_uids(self):
        all_uids = self.get_all_monitored_uids()
        keys_to_remove = [uid for uid in self.live_status_cache.keys() if uid not in all_uids]
        for uid in keys_to_remove:
            self.live_status_cache.pop(uid, None)
            self.live_start_times.pop(uid, None)
            self.uid_error_counts.pop(uid, None)
            self.uid_skip_until.pop(uid, None)

    @filter.command("添加监控")
    async def add_monitor(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("❌ 使用方法: /添加监控 <UID>")
            return
        
        uid = args[1]
        if not uid.isdigit():
            yield event.plain_result("❌ UID必须是数字")
            return
            
        session_id = event.unified_msg_origin
        session_config = self.get_session_config(session_id)
        
        uids = [str(u) for u in session_config.get("uids", [])]
        if uid in uids:
            yield event.plain_result("❌ 该UP主已在当前会话的监控列表中")
            return
            
        if len(uids) >= self.max_monitors:
            yield event.plain_result(f"❌ 当前会话监控数量已达上限({self.max_monitors})")
            return
            
        status_info = await self.get_live_status(uid)
        if not status_info.get("uname"):
            yield event.plain_result(f"❌ 未找到UID为 {uid} 的UP主")
            return
            
        if "uids" not in session_config:
            session_config["uids"] = []
        session_config["uids"].append(str(uid))
        
        self.live_status_cache[uid] = status_info.get("live_status", 0)
        if status_info.get("live_status") == 1:
            self.live_start_times[uid] = time.time()
            
        self.save_config()
        uname = status_info.get("uname", "未知UP主")
        yield event.plain_result(f"✅ 已在当前会话添加 {uname}(UID:{uid}) 到监控列表")

    @filter.command("移除监控")
    async def remove_monitor(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("❌ 使用方法: /移除监控 <UID>")
            return
        
        uid = args[1]
        session_id = event.unified_msg_origin
        session_config = self.get_session_config(session_id)
        
        uids = [str(u) for u in session_config.get("uids", [])]
        if uid in uids:
            session_config["uids"] = [str(u) for u in session_config.get("uids", []) if str(u) != str(uid)]
            
            self.cleanup_unmonitored_uids()
            self.save_config()
            yield event.plain_result(f"✅ 已在当前会话移除UID {uid} 的监控")
        else:
            yield event.plain_result(f"❌ UID {uid} 不在当前会话的监控列表中")

    @filter.command("监控列表")
    async def list_monitors(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        session_config = self.get_session_config(session_id)
        uids = [str(u) for u in session_config.get("uids", [])]
        
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
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("❌ 使用方法: /检查直播 <UID>")
            return
        
        uid = args[1]
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
        all_uids = self.get_all_monitored_uids()
        sessions_count = len(self._get_sessions_list())
        
        message = "🔧 插件运行状态:\n"
        message += f"• HTTP会话: {'✅ 正常' if self.session and not self.session.closed else '❌ 异常'}\n"
        message += f"• 监控任务: {'✅ 运行中' if self.monitor_task and not self.monitor_task.done() else '❌ 已停止'}\n"
        message += f"• 监控配置: 共 {sessions_count} 个会话, {len(all_uids)} 个去重UP主\n"
        
        yield event.plain_result(message)

    @filter.command("开启通知")
    async def enable_notify_cmd(self, event: AstrMessageEvent):
        session_config = self.get_session_config(event.unified_msg_origin)
        session_config["enable_notifications"] = True
        self.save_config()
        yield event.plain_result("✅ 当前会话已开启开播与关播通知")

    @filter.command("关闭通知")
    async def disable_notify_cmd(self, event: AstrMessageEvent):
        session_config = self.get_session_config(event.unified_msg_origin)
        session_config["enable_notifications"] = False
        self.save_config()
        yield event.plain_result("✅ 当前会话已关闭所有通知")

    @filter.command("开启关播通知")
    async def enable_end_notify_cmd(self, event: AstrMessageEvent):
        session_config = self.get_session_config(event.unified_msg_origin)
        session_config["enable_end_notifications"] = True
        self.save_config()
        yield event.plain_result("✅ 当前会话已开启关播通知")

    @filter.command("关闭关播通知")
    async def disable_end_notify_cmd(self, event: AstrMessageEvent):
        session_config = self.get_session_config(event.unified_msg_origin)
        session_config["enable_end_notifications"] = False
        self.save_config()
        yield event.plain_result("✅ 当前会话已关闭关播通知")

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

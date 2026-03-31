# B站直播监控插件改进计划

## 1. 需求摘要
基于现有 `astrbot_plugin_bilibiliLive` 插件进行以下改进：
1. **消息格式修改**：将开播、关播通知改为图文混排的特定格式，确保在QQ中正常换行显示，并增加关播时的“直播时长”统计。
2. **会话隔离与配置页面集成**：将监控的UP主及通知开关从全局改为按会话（群聊/私聊）隔离。同时在 AstrBot 插件配置页面中提供可视化的会话配置编辑项。
3. **漏洞修复**：修复 Bilibili API 请求数限制、JSON 解析容错、残留状态导致的内存泄漏等问题。
4. **仓库信息转移**：修改插件元数据，使其与原仓库解绑，转移至个人仓库 `https://github.com/Wine-Red/astrbot_plugin_bilibiliLive`。

## 2. 现状分析
- **消息格式**：目前只发送纯文本消息，未使用 `MessageChain` 的图片发送能力，且无开播时长统计。
- **状态存储**：目前配置与运行状态（如 `live_status_cache`、`monitored_uids`）混合存储在 `~/.astrbot/bili_live_notice/monitor_config.json` 中。
- **配置系统**：目前的 `_conf_schema.json` 仅包含全局设置，未利用 AstrBot 提供的 `dict` / `template_schema` 实现复杂对象的 Web UI 配置。
- **漏洞情况**：
  1. API 批量请求未分块（B站API对单次请求的UID数量有隐性限制）。
  2. B站 API 偶发返回 `data: null` 时会引发类型判断异常。
  3. UID被移除时，错误计数和跳过标记字典（`uid_error_counts`, `uid_skip_until`）未清理，存在轻微内存泄漏。
- **元数据**：`metadata.yaml` 和 `main.py` 仍指向原作者 `Binbim` 和原仓库。

## 3. 拟定更改方案

### 3.1 消息样式与时长统计
- **目标文件**：`main.py`
- **修改内容**：
  - 在 `monitor_live_status` 中，当状态由 `0` 变 `1` 时，记录当前时间戳至 `self.live_start_times[uid]`。
  - 重构 `send_live_notification`：使用 `MessageChain().message("...").url_image(cover_url).message("...")` 拼接消息，其中 `cover_url` 取自 API 返回的 `cover_from_user` 或 `keyframe`。确保在纯文本中加入 `\n` 进行换行。
  - 重构 `send_end_notification`：计算当前时间与 `start_time` 的差值得出直播时长，格式化为 `X小时Y分钟`，使用相同的图文混排格式发送关播消息。

### 3.2 会话隔离与 Web UI 配置
- **目标文件**：`_conf_schema.json`, `main.py`
- **修改内容**：
  - 更新 `_conf_schema.json`，引入 `sessions` 字段（类型 `dict`），并设置 `template_schema`（包含 `uids`, `enable_notifications`, `enable_end_notifications`），使用户能在 AstrBot 面板直接为每个会话ID配置参数。
  - 在 `main.py` 中，移除全局的 `monitored_uids`，改为读取 `self.config.get("sessions", {})`。
  - 修改所有的指令处理函数（`/添加监控`, `/开启通知` 等），使它们操作当前会话 (`event.unified_msg_origin`) 的配置，并尝试调用 `self.config.save_config()` 保存到 AstrBot 原生配置系统。
  - 拆分运行状态：将 `live_status_cache` 和 `live_start_times` 单独存入 `~/.astrbot/bili_live_notice/monitor_state.json`，与用户配置彻底分离。

### 3.3 漏洞修复与稳定性优化
- **目标文件**：`main.py`
- **修改内容**：
  - **API分块**：在 `monitor_live_status` 和 `get_live_status_batch` 中，将 UIDs 列表按每组 40 个进行分块并发请求，防止请求 URI 过长或触发 B 站 412/414 错误。
  - **数据容错**：优化 `body.get("data")` 的处理，防止其为 `None` 时引发异常。
  - **内存清理**：在执行 `/移除监控` 后，同步 `pop` 掉 `uid_error_counts`、`uid_skip_until` 和 `live_start_times` 中的相关记录。

### 3.4 仓库所有权转移
- **目标文件**：`metadata.yaml`, `main.py`
- **修改内容**：
  - `metadata.yaml`：将 `author` 改为 `Wine-Red`，`repo` 改为 `https://github.com/Wine-Red/astrbot_plugin_bilibiliLive`。
  - `main.py`：修改 `@register` 装饰器参数，与新仓库信息保持一致。

## 4. 验证步骤
1. 检查配置页面：确保能在 AstrBot 配置面板中看到按会话隔离的监控列表及通知开关，并能正常保存。
2. 检查指令隔离：在不同的会话（如两个不同群聊）中执行 `/添加监控` 和 `/开启通知`，验证配置互相独立。
3. 检查消息格式：手动或等待触发一次开播和关播事件，确认包含正确的文本换行、直播封面，以及关播时的时长统计。
4. 检查后台任务：确认 API 分块机制和容错机制运行正常，控制台无报错。
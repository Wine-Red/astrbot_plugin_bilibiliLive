# B站开播监控

一个面向 AstrBot 的 B 站直播状态监控插件。

插件会按会话维度读取插件配置中的监控列表，定时检查指定 UID 的直播状态，并在开播、关播或直播资料发生变化时向当前会话发送通知。

## 功能概览

- 支持群聊和私聊分别维护独立监控列表
- 支持开播通知、关播通知
- 支持直播中标题、分区、封面变更通知
- 支持关播状态下标题、分区、封面变更通知
- 支持批量轮询 UID，降低请求次数
- 支持限流退避和异常 UID 暂时跳过
- 支持自动保存监控状态，插件重启后继续生效

## 兼容要求

- AstrBot `>=4.10.4,<5`
- Python 依赖：`aiohttp>=3.8.0`

## 安装

将插件目录放入 AstrBot 插件目录后重启或重载插件即可。

当前插件关键文件如下：

- `metadata.yaml`：插件元数据
- `main.py`：插件入口
- `_conf_schema.json`：WebUI 配置结构
- `requirements.txt`：依赖声明

## 使用方式

监控对象和通知开关统一通过插件配置维护，命令只保留查看类能力。

```text
/监控列表
/检查直播 <UID>
/插件状态
```

示例：

```text
/监控列表
/检查直播 123456
```

## 配置说明

插件主要配置项如下：

- `check_interval`：轮询间隔，单位秒，默认 `60`
- `max_monitors`：单会话最多监控的 UID 数量，默认 `50`
- `sessions`：会话级配置列表，需要在插件配置中维护

每个会话配置包含以下字段：

- `session_id`：会话唯一标识
- `uids`：当前会话监控的 UID 列表
- `enable_notifications`：通知总开关
- `enable_end_notifications`：关播通知开关
- `enable_title_change_notifications`：直播中标题变更通知
- `enable_cover_change_notifications`：直播中封面变更通知
- `enable_area_change_notifications`：直播中分区变更通知
- `enable_offline_title_change_notifications`：关播标题变更通知
- `enable_offline_cover_change_notifications`：关播封面变更通知
- `enable_offline_area_change_notifications`：关播分区变更通知

推荐直接在插件配置中维护 `sessions`。插件不会再通过命令自动创建或修改会话配置。

## 配置监控列表

在插件配置的 `sessions` 中添加会话模板即可启用监控。

示例：

```json
{
  "session_id": "default:GroupMessage:123456",
  "uids": ["123456", "789012"],
  "enable_notifications": true,
  "enable_end_notifications": true,
  "enable_title_change_notifications": true,
  "enable_cover_change_notifications": false,
  "enable_area_change_notifications": true,
  "enable_offline_title_change_notifications": false,
  "enable_offline_cover_change_notifications": false,
  "enable_offline_area_change_notifications": false
}
```

其中：

- `session_id` 是目标群聊或私聊的会话标识
- `uids` 是该会话要监控的主播 UID 列表
- 其余布尔字段用于控制不同通知类型的开关

## 通知内容

插件会根据状态变化发送以下类型的消息：

- 开播通知：包含主播名、标题、分区、直播间链接
- 关播通知：包含直播时长和本场人气值
- 标题变更通知：包含旧标题和新标题
- 分区变更通知：包含旧分区和新分区
- 封面变更通知：附带最新封面

## 关于“本场人气值”

关播通知中的“本场人气值”来自 B 站公开接口返回的 `online` 字段。

这个值更适合作为平台展示的人气值参考，不建议将其理解为严格意义上的“真实同时在线人数”。

## 数据存储

插件运行状态不会写入源码目录，而是保存到 AstrBot 数据目录：

```text
data/plugin_data/bili_live_notice/monitor_state.json
```

保存内容包括：

- 最近一次直播状态缓存
- 开播时间缓存
- 标题/分区/封面缓存
- 本场人气值缓存

## UID 获取方式

打开 B 站用户主页，例如：

```text
https://space.bilibili.com/123456
```

其中数字部分就是 UID。

## 开发说明

- 插件入口类为 `BiliLiveNoticePlugin`
- 所有命令和监控逻辑集中在 `main.py`
- 插件使用 AstrBot 标准数据目录保存状态
- 插件对 B 站接口限流做了退避处理，避免短时间内频繁重试

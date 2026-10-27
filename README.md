# 手机操作上下文 · AstrBot 插件

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.27%2C%3C5-4B8BBE.svg)
![Windows](https://img.shields.io/badge/Windows-AstrBot_host-0078D4.svg)
![Android 14](https://img.shields.io/badge/Android-14-3DDC84.svg)

把手机操作外包给一个无身份、无记忆的受限执行器。原始 adb 命令、控件树和屏幕内容留在磁盘，只有一段结构化摘要回到主对话，避免操作流水挤占聊天上下文。

当前版本 0.2.0。实验性项目，只针对小黑盒 App 与一台固定安卓设备，尚未做通用化。

需要 AstrBot 以 sandbox 运行时接入 Shipyard Neo 沙箱，且沙箱内能连到目标设备。仅安装插件无法使用。

## 功能

- 查询沙箱工作区容量、手机连接状态、前台是否为允许应用。
- 只读观察小黑盒控件树，同时读取 `text` 和 `content-desc`，过滤装饰节点并给出是否截断。
- 按控件树执行一次点击、滑动、返回、回桌面、启动应用、输入冻结文本；同一趟内可重复观察与动作。
- 截图作为工具调用，图片交给执行器自身判断；调用方只收到路径和字节数。
- 发帖一步完成：核对类型、填写标题正文、点击发布、处理配图确认框、读回详情页。
- 每次任务落盘 `timeline.jsonl` 与 `result.json`，按任务 ID 可追溯。

## 与直接调用工具的区别

| | 主对话直接执行 | 本插件 |
|---|---|---|
| 谁决定每一步 | 主对话逐条决定 | 独立执行模型在固定能力集内选择 |
| 进入对话的内容 | 命令原文、标准输出、错误 | 最终摘要、动作回执、页面分类 |
| 可用范围 | 沙箱内全部工具 | 固定的 12 项能力 |
| 额外校验 | 无 | 前台应用、捕获前后一致、目标必须在当前控件树、文字由宿主冻结 |

插件不是权限边界。主对话本身保留完整 shell，插件只负责隔离噪声与记录证据。

## 环境要求

| 组件 | 要求 |
|---|---|
| AstrBot | 4.27 及以上、低于 5.0 |
| 运行时 | `computer_use_runtime` 为 `sandbox`，`booter` 为 `shipyard_neo` |
| 父会话权限 | 具备 `astrbot_execute_shell`，否则拒绝执行 |
| 沙箱 | 已安装 adb，设备地址固定为 `172.22.0.2:5555` |
| 设备 | Android 14，已登录小黑盒，包名 `com.max.xiaoheihe` |
| 输入法 | 默认输入法为 `com.android.adbkeyboard/.AdbIME` |

adb 路径优先取 `/workspace/.local/adbdl/platform-tools/adb`，缺失时回退 `/usr/bin/adb`。

## 安装与配置

将仓库放入 AstrBot 的 `data/plugins/astrbot_plugin_operation_context/`，在插件面板填写配置并重载。

| 配置项 | 用途 |
|---|---|
| `allowed_capabilities` | 允许的能力列表，留空全部拒绝 |
| `max_steps` | 执行规划轮数上限，1 至 24，默认 10 |
| `tool_call_timeout` | 单个能力超时秒数，1 至 60 |
| `executor_provider` | 执行器使用的模型 ID；留空跟随当前聊天模型 |

`allowed_capabilities` 可填：

```
sandbox_status  phone_status  phone_app_scope  phone_observe
phone_tap  phone_swipe  phone_back  phone_home  phone_start_app
phone_screenshot  phone_input  post_submit
```

`post_submit` 必须单独使用，不能与其他能力并列。

## 使用

通过 AstrBot 对话调用 `operation_context_run`，给出目标和允许的能力。

| 请求示例 | 操作 |
|---|---|
| 看看现在手机停在哪个页面 | 只读观察并返回页面分类 |
| 点进第一条帖子，往下滑一屏，再观察 | 多步导航 |
| 在评论框输入这段话 | 使用主对话提供的原文输入 |
| 发一条帖子，标题和正文如下 | 一步完成填写与提交 |

### 能力

| 类别 | 能力 |
|---|---|
| 查询 | `sandbox_status`、`phone_status`、`phone_app_scope` |
| 观察 | `phone_observe`、`phone_screenshot` |
| 动作 | `phone_tap`、`phone_swipe`、`phone_back`、`phone_home`、`phone_start_app`、`phone_input` |
| 发布 | `post_submit` |

### 返回结构

```json
{
  "status": "observations_returned",
  "rounds": 4,
  "action_count": 1,
  "final": {"capability": "phone_observe", "activity": "...", "screen": "post_editor",
            "source": "uiautomator", "text_available": true, "node_count": 29,
            "truncated": false, "tainted": null},
  "expect_text_found": null,
  "actions_taken": [{"capability": "phone_tap", "result": {"action": "tap", "bounds": [0, 873, 720, 965]}}],
  "raw_record": "...\\tasks\\<task_id>"
}
```

`screen` 取值：`post_editor`、`post_detail`、`post_with_comment_box`、`home`、`other`。控件树不返回给主对话。

### 使用限制

- 只支持小黑盒包名，其他前台一律拒绝。
- uiautomator 失败时观察退回系统视图流，此时没有文字也没有可用 id。动作脚本依赖 uiautomator，这类页面上的点击和输入会失败，只能靠截图判断。
- `post_submit` 已改为自动填写标题正文，但尚未在真机完整跑通一次成功发布。
- 评论没有专属能力，仍由执行器按控件树自由操作，比发帖松。
- 没有跨通道的手机互斥：主对话直接跑 adb 时插件不知情。
- 状态未知一律不重试，需要人工或主对话先观察确认。
- 尚未完成干净环境部署与长期无人看护验收。

## 安全与隐私

不提供 QQ 能力，不发送任何对外消息；所有对外文字必须由主对话提供并冻结。页面文本在送入执行器前做提示词注入检测，命中时文本被屏蔽并标记 `tainted`。

控件树、命令输出和截图路径写入 `data/plugin_data/astrbot_plugin_operation_context/tasks/`，不进入聊天上下文。

## 常见问题

| 问题 | 检查项 |
|---|---|
| 返回 `capability_not_authorized` | `allowed_capabilities` 是否包含该能力 |
| 返回 `parent_shell_permission_required` | 父会话是否具备 `astrbot_execute_shell` |
| 返回 `phone_outside_allowed_app` | 前台是否仍为小黑盒 |
| 返回 `hierarchy_unavailable` | 页面是否持续动画，改用截图 |
| 返回 `observe_first` | 先调用 `phone_observe` |
| 返回 `protected_control` | 该控件被禁止点击，例如发帖页的类型标题 |
| 发帖返回 `unknown` | 已尝试但未确认，先观察再决定，不要直接重发 |

## 致谢

提示词注入检测复用 [astrbot_plugin_antipromptinjector](https://github.com/oyxning/astrbot_plugin_antipromptinjector) 的 PTD 核心，来源与许可见 `threat_detector.py` 文件头。

感谢 AstrBot 提供的插件框架与工具调用接口。

## 许可

[GNU AGPLv3](LICENSE)。`threat_detector.py` 来自 AGPL-3.0 的第三方插件，按原样保留。

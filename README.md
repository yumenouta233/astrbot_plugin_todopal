# AstrBot Plugin: TodoPal

TodoPal 是一个面向 AstrBot 的待办管理插件，当前版本采用“能力层 + 命令层 + 工具层”的统一架构，支持显式命令交互与主系统 LLM Tool 调用。

## 当前架构

### 1) 命令入口层（显式前缀）
- 仅拦截以下前缀：`todo`、`add`、`done`、`fix`、`check`、`del/delete/rm`
- 避免全量消息拦截带来的系统对话割裂问题
- `todo` 作为智能入口，会先做意图识别（add/done/fix/delete/check/cancel）

### 2) 服务能力层（统一业务语义）
- `check`：读取指定日期待办
- `add`：解析并生成待办项（支持仅解析不落库）
- `done`：按序号或内容匹配完成待办
- `fix`：按序号修改待办内容
- `delete`：按序号或内容匹配删除待办
- 每个服务结果都返回统一结构：`ok / action / error / message / data`

### 3) 工具暴露层（LLM Tool）
- `todo_check`
- `todo_add`
- `todo_done`
- `todo_fix`
- `todo_delete`
- 主系统可在自然对话中调用工具，插件负责提供结构化能力结果

### 4) 存储层（本地 JSON）
- 按 `platform / user / year / month / date` 分层存储
- 单日文件内维护 `pending/done` 状态、创建时间、更新时间等字段
- 用户注册信息同时记录 `origin/provider_id`，供主动提醒与主动总结任务复用

### 5) 主动消息可靠性
- 主动提醒/总结优先走 LLM 生成文案，失败时自动回退到模板文案，不中断发送
- 发送接口增加兼容调用路径，适配不同 AstrBot 版本下 `send_message` 参数差异

### 6) 提醒调度模式
- 默认优先接入主系统 `future_task` 托管循环提醒，任务按用户维度自动创建与更新
- 若主系统调度接口不可用或手动关闭托管模式，则自动回退到插件本地循环提醒

## 使用方式

### 显式命令
- `todo <自然语言>`：智能入口，自动识别意图
- `add <自然语言>`：新增待办（先预览，回复“确认”后保存）
- `done <序号或内容>`：标记完成（支持批量）
- `fix <序号> <新内容>`：修改待办
- `del/delete/rm <序号或内容>`：删除待办（支持批量）
- `check [今天|明天|后天|YYYY-MM-DD|M月D日]`：查看待办

### 交互确认
- 当识别到新增待办时，插件会先返回结构化预览
- 回复“确认”执行保存，回复“取消”放弃本次操作

## LLM Tool 接口说明

### `todo_check(date="")`
- 输入：可选日期字符串
- 输出：包含日期、数量、事项列表与统一 `message`

### `todo_add(content, date="", time="")`
- 输入：待办内容，或显式日期/时间
- 输出：新增数量、日期分布、事项列表与统一 `message`

### `todo_done(selector, date="")`
- 输入：序号/关键词选择器
- 输出：更新条目、更新数量与统一 `message`

### `todo_fix(index, content, date="")`
- 输入：待办序号与新内容
- 输出：更新项详情与统一 `message`

### `todo_delete(selector, date="")`
- 输入：序号/关键词选择器
- 输出：删除数量、删除项详情与统一 `message`

## 日期与人格

- `check` 与智能 `todo` 的查看意图支持今天/明天/后天/明确日期
- 人格回复支持两类模式：
  - 人格化单条回复
  - 人格开场 + 结构化清单（用于查看与预览场景）

## 数据存储结构

```text
data/plugin_data/todopal/
├── {platform}/
│   └── {user_id}/
│       └── {year}/
│           └── {month}/
│               └── {date}.json
```

```json
[
  {
    "id": "20260318-abc123",
    "date": "2026-03-18",
    "time": "15:00",
    "content": "给谭老师发邮件",
    "status": "pending",
    "created_at": "2026-03-18 10:00:00",
    "updated_at": "2026-03-18 10:00:00",
    "done_at": null,
    "source_text": "todo 明天下午3点给谭老师发邮件"
  }
]
```

## 配置建议

- 确保 AstrBot 已配置可用的 LLM Provider（用于智能解析与人格回复）
- 在 WebUI 中按需设置：
  - 人格（`bot_persona` / `bot_persona_prompt`）
  - 主动提醒与总结开关、时间段、间隔
  - 提醒调度模式（`use_system_scheduler_for_reminder`，默认启用主系统托管）
  - 提醒间隔字段使用 `reminder_interval_minutes`（分钟）
  - 自定义触发词配置（用于兼容旧行为配置）
- 触发过一次命令或工具调用后，插件会自动刷新用户的 `origin/provider_id` 缓存，降低“有待办但不提醒”的概率
- 若仍未收到提醒，可先用 `check 今天` 刷新用户上下文，再观察 1~2 个提醒周期

## 安装

将本插件放入 AstrBot 插件目录并重启加载即可。

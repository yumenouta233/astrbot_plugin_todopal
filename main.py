from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from datetime import datetime
import re
import uuid

import json
import os
from pathlib import Path

try:
    from .llm_parser import parse_todo, analyze_intent
    from .storage import TodoStorage
    from .matcher import TodoMatcher
except ImportError:
    from llm_parser import parse_todo, analyze_intent
    from storage import TodoStorage
    from matcher import TodoMatcher

@register("todopal", "TodoPal", "TodoPal Plugin", "1.0.0")
class TodoPalPlugin(Star):
    """
    TodoPal plugin for AstrBot to manage todo items.
    """

    def __init__(self, context: Context):
        """
        Initialize the TodoPal plugin.

        Args:
            context: The AstrBot context.
        """
        super().__init__(context)
        self.storage = TodoStorage()
        # In-memory session state: {unified_msg_origin: {'state': str, 'todos': list, 'pending_date': str}}
        self.sessions = {}
        self.triggers = self._load_triggers()

    def _load_triggers(self):
        """Load trigger keywords from triggers.json"""
        try:
            # Assuming triggers.json is in the same directory as this file
            trigger_path = Path(__file__).parent / "triggers.json"
            if trigger_path.exists():
                with open(trigger_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("keywords", [])
        except Exception as e:
            logger.error(f"Failed to load triggers.json: {e}")
        
        # Default fallback
        return ["记", "待办", "任务", "做完", "完成", "修改", "改一下", "看看", "清单", "列表", "check", "add", "fix", "done"]

    def _format_preview(self, todos: list, include_confirm_prompt: bool = True) -> str:
        """
        Format todos for user confirmation or display.
        """
        grouped = {}
        for todo in todos:
            date = todo.get("date", "Unknown")
            if date not in grouped:
                grouped[date] = []
            grouped[date].append(todo)
        
        result_lines = []
        for date, items in grouped.items():
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                weekday_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
                weekday = weekday_map[dt.weekday()]
                date_header = f"{dt.year}年{dt.month}月{dt.day}日 星期{weekday}"
            except ValueError:
                date_header = date
            
            result_lines.append(date_header)
            result_lines.append("")
            
            for i, item in enumerate(items, 1):
                time = item.get("time")
                content = item.get("content", "")
                status = item.get("status", "pending")
                
                prefix = f"{time} " if time else ""
                check_mark = "✅ " if status == "done" else ""
                
                result_lines.append(f"{i}. {check_mark}{prefix}{content}")
            result_lines.append("")
        
        if include_confirm_prompt:
            result_lines.append("如果正确，请回复“确认”。")
        return "\n".join(result_lines)

    @filter.regex(r".*")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input.
        Supports:
        1. Explicit commands: 'todo', 'add', 'done', 'fix', 'check'
        2. Natural language with keywords (defined in triggers.json)
        """
        message_str = event.message_str.strip()
        if not message_str:
            return

        # 1. Check for explicit command prefixes
        explicit_match = re.match(r"^(todo|add|done|fix|check)\s*(.*)", message_str, re.IGNORECASE)
        
        if explicit_match:
            command_prefix = explicit_match.group(1).lower()
            todo_content = explicit_match.group(2).strip()
            # If explicit command, force smart mode for 'todo' or execute others directly
        else:
            # 2. Check for keywords in triggers
            # If no keyword matches, ignore this message
            if not any(keyword in message_str for keyword in self.triggers):
                return
            
            # If matched, treat as 'todo' command (smart agent)
            command_prefix = 'todo'
            todo_content = message_str

        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        # --- Handle 'check' command ---
        if command_prefix == 'check':
            yield await self._handle_check_command(event, platform, user_id)
            return

        if not todo_content:
            yield event.plain_result(f"请输入{command_prefix}的具体内容。")
            return

        # --- Handle 'done' command ---
        if command_prefix == 'done':
            yield await self._handle_done_command(event, platform, user_id, todo_content)
            return

        # --- Handle 'fix' command ---
        if command_prefix == 'fix':
            yield await self._handle_fix_command(event, platform, user_id, todo_content)
            return

        # --- Handle 'todo' and 'add' commands (require LLM) ---
        # Get LLM Provider ID
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        except TypeError:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception as e:
            logger.error(f"Failed to get provider ID: {e}")
            yield event.plain_result("无法获取当前的 LLM Provider ID，请检查配置。")
            return

        if not provider_id:
            yield event.plain_result("未配置 LLM Provider。")
            return

        # Smart handling for 'todo' command
        if command_prefix == 'todo':
            today = datetime.now().strftime("%Y-%m-%d")
            current_todos = self.storage.load_todos(platform, user_id, today)
            
            # Analyze intent
            intent_result = await analyze_intent(self.context, provider_id, todo_content, current_todos)
            
            if not intent_result or not intent_result.get('type'):
                yield event.plain_result("抱歉，我没太理解您的意思，请换个说法试试。")
                return
            
            intent_type = intent_result['type']
            payload = intent_result.get('payload')
            
            if intent_type == 'check':
                yield await self._handle_check_command(event, platform, user_id)
                return
                
            elif intent_type == 'done':
                if not payload:
                     yield event.plain_result("需要指定完成哪一项哦。")
                     return
                yield await self._handle_done_command(event, platform, user_id, str(payload))
                return
                
            elif intent_type == 'fix':
                if not payload:
                    yield event.plain_result("需要指定修改哪一项及新内容哦。")
                    return
                yield await self._handle_fix_command(event, platform, user_id, str(payload))
                return
                
            elif intent_type == 'add':
                # Proceed to existing logic for adding todos
                # Payload should be the list of todos
                if isinstance(payload, list):
                    todos = payload
                else:
                    # Fallback to old parser if payload is weird
                    todos = await parse_todo(self.context, provider_id, todo_content)
            
            elif intent_type == 'cancel':
                yield event.plain_result("好的，什么都不做。")
                return
                
            else:
                yield event.plain_result("抱歉，我没太理解您的意思。")
                return

        else:
            # 'add' command: always append
            todos = await parse_todo(self.context, provider_id, todo_content)

        if todos is None:
            yield event.plain_result("暂时没有稳定识别这条待办，请换一种更明确的表达方式。")
            return
        if not todos:
            yield event.plain_result("未能识别到任何待办事项。")
            return

        # For 'todo' (smart mode), we default to append unless we want to support overwrite logic explicitly.
        # Given the "Smart Agent" change, 'todo' implies natural language interaction which usually means "add to list".
        # However, to preserve "Overwrite" capability, we might need a specific trigger.
        # For now, let's make 'todo' (via intent 'add') default to 'append' to be safe, 
        # BUT if we want to support the old "Overwrite" behavior, we might need to ask.
        # Let's stick to 'append' for smart 'add' to avoid data loss.
        # If user really wants to overwrite, they might need to clear first (not supported yet) or we add a "clear" intent later.
        # Wait, the previous logic was: 'todo' = overwrite, 'add' = append.
        # Now 'todo' = smart agent. 'add' = append.
        # So we effectively removed "Overwrite" command unless we re-introduce it.
        # Let's change action_type to 'append' for both, unless we add logic.
        
        action_type = 'append' 
        
        # Store session state
        self.sessions[event.unified_msg_origin] = {
            'state': 'WAITING_CONFIRM',
            'action_type': action_type,
            'todos': todos,
            'source_text': todo_content,
            'platform': platform,
            'user_id': user_id
        }

        preview = self._format_preview(todos, include_confirm_prompt=True)
        
        if command_prefix == 'todo_old_overwrite_mode_disabled':
            # Check if data exists for today to warn user
            today = datetime.now().strftime("%Y-%m-%d")
            existing = self.storage.load_todos(platform, user_id, today)
            warning = ""
            if existing:
                warning = f"\n⚠️ **注意：这将覆盖您今天已有的 {len(existing)} 条待办！**"
            
            yield event.plain_result(f"【新建/覆盖模式】{warning}\n{preview}")
        else:
            yield event.plain_result(f"【追加模式】\n{preview}")

    @filter.regex(r"^(确认|取消)$")
    async def handle_confirmation(self, event: AstrMessageEvent):
        """
        Handle confirmation or choice selection.
        """
        session = self.sessions.get(event.unified_msg_origin)
        if not session:
            # Not in a session, ignore or let other plugins handle
            return 

        action = event.message_str.strip()
        state = session['state']
        todos = session['todos']
        platform = session['platform']
        user_id = session['user_id']
        source_text = session.get('source_text', '')

        if action == "取消":
            del self.sessions[event.unified_msg_origin]
            yield event.plain_result("已取消。")
            return

        if state == 'WAITING_CONFIRM':
            if action == "确认":
                mode = session.get('action_type', 'append')
                self._save_todos(platform, user_id, todos, source_text, mode=mode)
                del self.sessions[event.unified_msg_origin]
                yield event.plain_result("已保存待办事项。")
            else:
                yield event.plain_result("请回复“确认”或“取消”。")

    def _save_todos(self, platform, user_id, todos, source_text, mode='append'):
        # Group by date first
        grouped = {}
        for todo in todos:
            date = todo.get("date")
            if not date: continue
            if date not in grouped: grouped[date] = []
            grouped[date].append(todo)
        
        for date, items in grouped.items():
            # Enrich items
            for item in items:
                item['id'] = f"{date.replace('-', '')}-{uuid.uuid4().hex[:6]}"
                item['status'] = 'pending'
                item['created_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                item['updated_at'] = item['created_at']
                item['done_at'] = None
                item['source_text'] = source_text
            
            if mode == 'overwrite':
                self.storage.save_todos(platform, user_id, date, items)
            else:
                self.storage.append_todos(platform, user_id, date, items)

    async def _handle_done_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle marking todos as done using the 'done' prefix.
        Matches: "done 1, 2", "done 买菜"
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        todos = self.storage.load_todos(platform, user_id, today)
        if not todos:
            return event.plain_result(f"今天没有待办事项哦。")

        matched_indices = TodoMatcher.match_todos(todos, content)
        
        if not matched_indices:
            return event.plain_result("找不到对应的待办事项，请检查描述或序号是否准确。")

        updated_items = []
        for idx in matched_indices:
            if todos[idx]['status'] != 'done':
                self.storage.update_todo_status(platform, user_id, today, idx, 'done')
                updated_items.append(todos[idx]['content'])
        
        if not updated_items:
            return event.plain_result("所选的待办事项已经是完成状态啦。")

        # Reload to get the fresh state and format it
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        
        return event.plain_result(f"太棒了！已更新状态：\n\n{preview}")

    async def _handle_check_command(self, event: AstrMessageEvent, platform: str, user_id: str):
        """
        Handle 'check' command to view today's todos.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        todos = self.storage.load_todos(platform, user_id, today)
        
        if not todos:
            return event.plain_result("今天还没有待办事项哦。")
            
        preview = self._format_preview(todos, include_confirm_prompt=False)
        return event.plain_result(f"今日待办清单：\n\n{preview}")

    async def _handle_fix_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle 'fix' command to modify a specific todo item content.
        Format: fix 3 改成光电数据集会议
        """
        today = datetime.now().strftime("%Y-%m-%d")
        todos = self.storage.load_todos(platform, user_id, today)
        
        if not todos:
            return event.plain_result("今天没有待办事项，无法修改。")

        # Parse index and content
        match = re.match(r"^(\d+)\s*(.*)", content)
        if not match:
            return event.plain_result("格式错误。请使用：fix 序号 新内容\n例如：fix 3 改成光电数据集会议")
            
        idx = int(match.group(1)) - 1
        raw_new_content = match.group(2).strip()
        
        if not (0 <= idx < len(todos)):
            return event.plain_result(f"找不到第 {idx+1} 条待办。")
            
        if not raw_new_content:
            return event.plain_result("请输入新的待办内容。")

        # Simple cleanup: remove common prefixes like "改成", "变为"
        cleaned_content = re.sub(r"^(改成|变为|变成|是|为|:)\s*", "", raw_new_content).strip()
        
        updated_item = self.storage.update_todo_content(platform, user_id, today, idx, cleaned_content)
        
        if updated_item:
            # Show the updated list
            fresh_todos = self.storage.load_todos(platform, user_id, today)
            preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
            return event.plain_result(f"已修改第 {idx+1} 条：\n\n{preview}")
        else:
            return event.plain_result("修改失败，请重试。")

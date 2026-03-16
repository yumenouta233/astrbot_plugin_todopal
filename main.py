from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from datetime import datetime
import re
import uuid

try:
    from .llm_parser import parse_todo
    from .storage import TodoStorage
    from .matcher import TodoMatcher
except ImportError:
    from llm_parser import parse_todo
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

    @filter.regex(r"^(todo|add|done)\s+(.*)")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input starting with 'todo', 'add', or 'done'.
        """
        message_str = event.message_str
        match = re.match(r"^(todo|add|done)\s+(.*)", message_str, re.IGNORECASE)
        if not match:
             return

        command_prefix = match.group(1).lower()
        todo_content = match.group(2).strip()
        
        if not todo_content:
            yield event.plain_result(f"请输入{command_prefix}的具体内容。")
            return

        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        # --- Handle 'done' command ---
        if command_prefix == 'done':
            await self._handle_done_command(event, platform, user_id, todo_content)
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

        # Parse Logic
        todos = await parse_todo(self.context, provider_id, todo_content)

        if todos is None:
            yield event.plain_result("暂时没有稳定识别这条待办，请换一种更明确的表达方式。")
            return
        if not todos:
            yield event.plain_result("未能识别到任何待办事项。")
            return

        # 'todo' prefix means overwrite (new list), 'add' means append
        action_type = 'overwrite' if command_prefix == 'todo' else 'append'
        
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
        
        if command_prefix == 'todo':
            yield event.plain_result(f"【新建/覆盖模式】\n{preview}")
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
        
        # Determine target date: If user specified a date like "done 昨天 1", we could parse it,
        # but for simplicity and common use cases, we default to today's list.
        todos = self.storage.load_todos(platform, user_id, today)
        if not todos:
            yield event.plain_result(f"今天没有待办事项哦。")
            return

        matched_indices = TodoMatcher.match_todos(todos, content)
        
        if not matched_indices:
            yield event.plain_result("找不到对应的待办事项，请检查描述或序号是否准确。")
            return

        updated_items = []
        for idx in matched_indices:
            if todos[idx]['status'] != 'done':
                self.storage.update_todo_status(platform, user_id, today, idx, 'done')
                updated_items.append(todos[idx]['content'])
        
        if not updated_items:
            yield event.plain_result("所选的待办事项已经是完成状态啦。")
            return

        # Reload to get the fresh state and format it
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        
        yield event.plain_result(f"太棒了！已更新状态：\n\n{preview}")

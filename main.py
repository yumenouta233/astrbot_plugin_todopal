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

    def _format_preview(self, todos: list) -> str:
        """
        Format todos for user confirmation.
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
                prefix = f"{time} " if time else ""
                result_lines.append(f"{i}. {prefix}{content}")
            result_lines.append("")
        
        result_lines.append("如果正确，请回复“确认”。")
        return "\n".join(result_lines)

    @filter.regex(r"^todo\s+(.*)")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input starting with 'todo'.
        """
        message_str = event.message_str
        match = re.match(r"^todo\s+(.*)", message_str, re.IGNORECASE)
        if not match:
             return

        todo_content = match.group(1).strip()
        if not todo_content:
            yield event.plain_result("请输入待办事项内容。")
            return

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

        # Check existing data (Simplification: just check the first date found for append/overwrite logic)
        # In multi-date scenario, we might just ask confirmation and handle merge internally.
        # But requirement says: "如果今天对应日期已经有待办数据... 提示用户选择：追加/覆盖"
        
        # We'll check if ANY of the dates have existing data.
        dates_with_data = []
        user_id = event.get_sender_id()
        # Fix platform name extraction: platform_name might not exist on event directly
        # Try unified_msg_origin first: "platform:..."
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            # Fallback
            platform = "unknown"
        
        for todo in todos:
            d = todo.get("date")
            if d:
                existing = self.storage.load_todos(platform, user_id, d)
                if existing and d not in dates_with_data:
                    dates_with_data.append(d)
        
        # Store session state
        self.sessions[event.unified_msg_origin] = {
            'state': 'WAITING_CHOICE' if dates_with_data else 'WAITING_CONFIRM',
            'todos': todos,
            'source_text': todo_content,
            'platform': platform,
            'user_id': user_id
        }

        preview = self._format_preview(todos)
        
        if dates_with_data:
            # Append prompt for choice
            yield event.plain_result(f"{preview}\n\n检测到日期 {'、'.join(dates_with_data)} 已有待办。\n请回复“追加”或“覆盖”。")
        else:
            yield event.plain_result(preview)

    @filter.regex(r"^(确认|追加|覆盖|取消)$")
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
                self._save_todos(platform, user_id, todos, source_text, mode='append') # Default append for new files
                del self.sessions[event.unified_msg_origin]
                yield event.plain_result("已保存待办事项。")
            else:
                # Should not happen due to regex, but safe fallback
                yield event.plain_result("请回复“确认”或“取消”。")

        elif state == 'WAITING_CHOICE':
            if action in ["追加", "覆盖"]:
                mode = 'append' if action == "追加" else 'overwrite'
                self._save_todos(platform, user_id, todos, source_text, mode=mode)
                del self.sessions[event.unified_msg_origin]
                yield event.plain_result(f"已{action}待办事项。")
            else:
                yield event.plain_result("请回复“追加”、“覆盖”或“取消”。")

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

    @filter.regex(r"(?:第(\d+)个|(.*))(?:做完了|完成了|完成|已完成)")
    async def handle_completion(self, event: AstrMessageEvent):
        """
        Handle marking todos as done.
        Matches: "第2个做完了", "买菜做完了"
        """
        # This regex is a bit broad, need to be careful not to conflict
        # But for plugin specific logic it should be fine.
        msg = event.message_str.strip()
        
        # Assume we are operating on TODAY's todo list by default
        # or we could search recent files. For simplicity, requirement implies context but doesn't specify date.
        # "第2个做完了" implies a visible list. Usually users refer to today's list.
        today = datetime.now().strftime("%Y-%m-%d")
        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        todos = self.storage.load_todos(platform, user_id, today)
        if not todos:
            yield event.plain_result("今天没有待办事项哦。")
            return

        match = TodoMatcher.match_todo(todos, msg)
        idx, item = match
        
        if item:
            if item['status'] == 'done':
                 yield event.plain_result(f"“{item['content']}” 已经是完成状态啦。")
                 return
            
            self.storage.update_todo_status(platform, user_id, today, idx, 'done')
            yield event.plain_result(f"太棒了！已将“{item['content']}”标记为完成。")
        else:
            yield event.plain_result("找不到对应的待办事项，请检查描述是否准确。")

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from datetime import datetime
import re
import uuid

import json
import os
from pathlib import Path
import asyncio
import inspect
from datetime import datetime, timedelta
from astrbot.api.message_components import Plain

try:
    from .llm_parser import parse_todo, analyze_intent
    from .storage import TodoStorage
    from .matcher import TodoMatcher
except ImportError:
    from llm_parser import parse_todo, analyze_intent
    from storage import TodoStorage
    from matcher import TodoMatcher

@register("todopal", "TodoPal", "TodoPal Plugin", "1.12.7")
class TodoPalPlugin(Star):
    """
    TodoPal plugin for AstrBot to manage todo items.
    """

    def __init__(self, context: Context, config: dict = None):
        """
        Initialize the TodoPal plugin.

        Args:
            context: The AstrBot context.
        """
        super().__init__(context)
        self.storage = TodoStorage()
        # In-memory session state: {unified_msg_origin: {'state': str, 'todos': list, 'pending_date': str}}
        self.sessions = {}
        
        self.config = config or {}
        
        # We don't need triggers.json anymore if we have self.config
        # But we provide a fallback default
        self.triggers = self.config.get("custom_triggers", ["记", "待办", "任务", "做完", "完成", "修改", "改一下", "看看", "清单", "列表", "check", "add", "fix", "done"])
        
        # Start cron loop for proactive messaging
        self._cron_task = asyncio.create_task(self._cron_loop())
        self._scheduler_bootstrap_task = asyncio.create_task(self._bootstrap_scheduler_sync())
        self._last_rollover_date = ""
        self._last_summary_sent = {}
        self._last_send_error = ""

    async def terminate(self):
        task = getattr(self, "_cron_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        scheduler_task = getattr(self, "_scheduler_bootstrap_task", None)
        if scheduler_task and not scheduler_task.done():
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _normalize_hhmm(value: str, default: str) -> str:
        try:
            parts = str(value).split(":")
            if len(parts) != 2:
                return default
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return default
            return f"{hour:02d}:{minute:02d}"
        except Exception:
            return default

    @staticmethod
    def _resolve_reminder_interval_minutes(config: dict) -> int:
        raw_minutes = config.get("reminder_interval_minutes", None)
        if raw_minutes is not None:
            try:
                parsed = int(raw_minutes)
                return parsed if parsed > 0 else 1
            except Exception:
                return 1
        legacy_hours = config.get("reminder_interval", 2)
        try:
            parsed_hours = float(legacy_hours)
            minutes = int(parsed_hours * 60)
            return minutes if minutes > 0 else 1
        except Exception:
            return 120

    async def _get_provider_id_from_origin(self, origin: str):
        if not origin:
            return None
        try:
            return await self.context.get_current_chat_provider_id(umo=origin)
        except TypeError:
            return await self.context.get_current_chat_provider_id(origin)
        except Exception:
            try:
                return await self.context.get_current_chat_provider_id(origin)
            except Exception:
                return None

    async def _call_maybe_async(self, func, *args, **kwargs):
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _system_scheduler_enabled(self) -> bool:
        return bool(self.config.get("use_system_scheduler_for_reminder", False))

    def _get_future_task_methods(self):
        create_method = getattr(self.context, "create_future_task", None)
        delete_method = getattr(self.context, "delete_future_task", None)
        list_method = getattr(self.context, "list_future_tasks", None)
        if callable(create_method) and callable(delete_method):
            return create_method, delete_method, list_method
        tool_executor = self._get_tool_executor()
        if callable(tool_executor):
            async def create_proxy(**kwargs):
                return await self._call_tool_executor(tool_executor, "create_future_task", kwargs)

            async def delete_proxy(**kwargs):
                return await self._call_tool_executor(tool_executor, "delete_future_task", kwargs)

            async def list_proxy(**kwargs):
                return await self._call_tool_executor(tool_executor, "list_future_tasks", kwargs)
            return create_proxy, delete_proxy, list_proxy
        return create_method, delete_method, list_method

    def _future_task_available(self) -> bool:
        if not self._system_scheduler_enabled():
            return False
        create_method, delete_method, _ = self._get_future_task_methods()
        return callable(create_method) and callable(delete_method)

    def _get_tool_executor(self):
        candidates = []
        for name in ("call_tool", "invoke_tool", "execute_tool", "run_tool", "call_func_tool"):
            method = getattr(self.context, name, None)
            if callable(method):
                candidates.append(method)
        tool_manager = getattr(self.context, "func_tool_manager", None)
        if tool_manager is not None:
            for name in ("call_tool", "invoke_tool", "execute_tool", "run_tool", "call_func_tool"):
                method = getattr(tool_manager, name, None)
                if callable(method):
                    candidates.append(method)
        return candidates[0] if candidates else None

    async def _call_tool_executor(self, executor, tool_name: str, args: dict):
        payload = args or {}
        attempts = [
            ((), {"tool_name": tool_name, "args": payload}),
            ((), {"name": tool_name, "args": payload}),
            ((), {"tool_name": tool_name, "params": payload}),
            ((), {"name": tool_name, "params": payload}),
            ((tool_name, payload), {}),
            ((tool_name,), payload),
            ((tool_name,), {"args": payload}),
            ((tool_name,), {"params": payload})
        ]
        for pos_args, kw_args in attempts:
            try:
                return await self._call_maybe_async(executor, *pos_args, **kw_args)
            except TypeError:
                continue
            except Exception as e:
                logger.error(f"Tool executor failed for {tool_name}: {e}")
                continue
        raise RuntimeError(f"tool executor unsupported for {tool_name}")

    @staticmethod
    def _origin_session_id(origin: str) -> str:
        if not origin:
            return ""
        parts = str(origin).split(":")
        if len(parts) >= 3:
            return parts[-1]
        return str(origin)

    @staticmethod
    def _is_send_result_success(result) -> bool:
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        if isinstance(result, str):
            text = result.strip().lower()
            return "message sent to session" in text
        if isinstance(result, dict):
            for key in ("ok", "success", "succeed"):
                if key in result:
                    return bool(result.get(key))
            return True
        return True

    @staticmethod
    def _build_plain_message_result(text: str):
        result = type("TodoPalMessageResult", (), {})()
        result.chain = [Plain(text)]
        return result

    @staticmethod
    def _is_unfinished_todo(item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        return str(item.get("status", "pending")) in ("pending", "rolled_over")

    @staticmethod
    def _normalize_tag_name(name: str) -> str:
        return str(name or "").strip()

    def _default_tags(self) -> list:
        configured = self.config.get("todo_default_tags", ["工作", "生活", "自我提升"])
        if not isinstance(configured, list):
            configured = ["工作", "生活", "自我提升"]
        tags = []
        for value in configured:
            tag = self._normalize_tag_name(value)
            if tag and tag not in tags:
                tags.append(tag)
        if not tags:
            return ["工作", "生活", "自我提升"]
        return tags

    @staticmethod
    def _default_tag_emoji_map() -> dict:
        return {
            "工作": "💼",
            "生活": "🏠",
            "自我提升": "📚",
            "学习": "📚"
        }

    def _config_tag_meta(self) -> list:
        configured = self.config.get("todo_tag_meta", [])
        if not isinstance(configured, list):
            configured = []
        meta = []
        seen = set()
        for idx, raw in enumerate(configured, 1):
            name = ""
            emoji = ""
            if isinstance(raw, str):
                parts = re.split(r"[|｜]", raw, maxsplit=1)
                name = self._normalize_tag_name(parts[0] if parts else "")
                emoji = str(parts[1]).strip() if len(parts) > 1 else ""
            elif isinstance(raw, dict):
                name = self._normalize_tag_name(raw.get("name"))
                emoji = str(raw.get("emoji", "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            meta.append({"id": idx, "name": name, "emoji": emoji, "order": len(meta) + 1})
        defaults = self._default_tags()
        fallback_map = self._default_tag_emoji_map()
        for default_name in defaults:
            if default_name in seen:
                continue
            seen.add(default_name)
            meta.append({
                "id": len(meta) + 1,
                "name": default_name,
                "emoji": fallback_map.get(default_name, ""),
                "order": len(meta) + 1
            })
        return meta

    def _tag_emoji_map(self) -> dict:
        mapping = {}
        for item in self._config_tag_meta():
            name = str(item.get("name", "")).strip()
            emoji = str(item.get("emoji", "")).strip()
            if name:
                mapping[name] = emoji
        return mapping

    def _tag_order_map(self) -> dict:
        order = {}
        for idx, item in enumerate(self._config_tag_meta(), 1):
            name = str(item.get("name", "")).strip()
            if name and name not in order:
                order[name] = idx
        return order

    def _tag_display_prefix(self, tag_name: str, fallback_tag_id: int = 0) -> str:
        normalized = self._normalize_tag_name(tag_name)
        emoji_map = self._tag_emoji_map()
        emoji = emoji_map.get(normalized, "")
        if emoji:
            return f"{emoji} "
        if normalized:
            return f"{normalized} "
        if int(fallback_tag_id or 0) > 0:
            tags = self._default_tags()
            idx = int(fallback_tag_id) - 1
            if 0 <= idx < len(tags):
                fallback_name = tags[idx]
                fallback_emoji = emoji_map.get(fallback_name, "")
                if fallback_emoji:
                    return f"{fallback_emoji} "
                if fallback_name:
                    return f"{fallback_name} "
        return ""

    def _use_config_tags_only(self) -> bool:
        return bool(self.config.get("todo_use_config_tags_only", False))

    def _allow_tag_command_edit(self) -> bool:
        if self._use_config_tags_only():
            return False
        return bool(self.config.get("todo_allow_tag_command_edit", True))

    def _get_user_tags(self, platform: str, user_id: str) -> list:
        if self._use_config_tags_only():
            return self._default_tags()
        user = self.storage.get_user_info(platform, user_id)
        raw = user.get("todo_tags") if isinstance(user, dict) else None
        if isinstance(raw, list):
            tags = []
            for value in raw:
                tag = self._normalize_tag_name(value)
                if tag and tag not in tags:
                    tags.append(tag)
            if tags:
                return tags
        tags = self._default_tags()
        self.storage.update_user_info(platform, user_id, {"todo_tags": tags})
        return tags

    def _set_user_tags(self, platform: str, user_id: str, tags: list):
        if self._use_config_tags_only():
            return
        normalized = []
        for value in tags or []:
            tag = self._normalize_tag_name(value)
            if tag and tag not in normalized:
                normalized.append(tag)
        self.storage.update_user_info(platform, user_id, {"todo_tags": normalized})

    def _render_tag_list(self, tags: list) -> str:
        if not tags:
            return "暂无标签。"
        rows = []
        for idx, name in enumerate(tags, 1):
            prefix = self._tag_display_prefix(name, idx)
            rows.append(f"{idx}. {prefix}{name}" if prefix else f"{idx}. {name}")
        return "\n".join(rows)

    def _build_tag_assign_help(self, todos: list, tags: list) -> str:
        lines = []
        for idx, name in enumerate(tags, 1):
            prefix = self._tag_display_prefix(name, idx)
            lines.append(f"{idx}. {prefix}{name}" if prefix else f"{idx}. {name}")
        tag_list_text = "\n".join(lines) if lines else "暂无标签。"
        mode_line = "- 当前标签来源：配置页面（全局）\n" if self._use_config_tags_only() else ""
        return (
            "标签列表：\n"
            f"{tag_list_text}\n\n"
            "回复标签编排：\n"
            f"{mode_line}"
            f"- 共 {len(todos)} 条待办，请逐条填写标签编号\n"
            "- 数字=绑定标签，x=丢弃该条，0=保留但不打标签\n"
            "- 支持格式：1x3 或 1,x,3\n"
            "- 回复“确认”可直接全部保存（不分配标签）\n"
            "- 回复“取消”放弃本次新增"
        )

    def _parse_tag_assignment(self, text: str, todo_count: int, tag_count: int):
        raw = str(text or "").strip()
        if not raw:
            return None, "请输入标签编排，例如：1x3 或 1,x,3。"
        compact = raw.replace(" ", "")
        if re.fullmatch(r"[0-9xX]+", compact) and len(compact) == todo_count:
            tokens = list(compact)
        else:
            tokens = [token for token in re.split(r"[\s,，]+", raw) if token]
        if len(tokens) != todo_count:
            return None, f"编排项数量不匹配：需要 {todo_count} 项，收到 {len(tokens)} 项。"
        parsed = []
        for idx, token in enumerate(tokens, 1):
            value = token.lower()
            if value == "x":
                parsed.append(None)
                continue
            if not value.isdigit():
                return None, f"第 {idx} 项格式无效：{token}。请使用数字、x 或 0。"
            number = int(value)
            if number < 0 or number > tag_count:
                return None, f"第 {idx} 项标签序号越界：{number}。可选范围是 0-{tag_count}。"
            parsed.append(number)
        return parsed, ""

    @staticmethod
    def _apply_tag_assignment(todos: list, tags: list, parsed: list):
        selected = []
        dropped = 0
        for todo, choice in zip(todos, parsed):
            if choice is None:
                dropped += 1
                continue
            item = dict(todo)
            if choice == 0:
                item["tag_id"] = 0
                item["tag_name"] = ""
            else:
                item["tag_id"] = choice
                item["tag_name"] = tags[choice - 1]
            selected.append(item)
        return selected, dropped

    @staticmethod
    def _parse_hhmm_minutes(value: str):
        text = str(value or "").strip()
        matched = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
        if not matched:
            return None
        return int(matched.group(1)) * 60 + int(matched.group(2))

    @staticmethod
    def _format_minutes_hhmm(minutes: int) -> str:
        clamped = max(0, min(23 * 60 + 59, int(minutes)))
        return f"{clamped // 60:02d}:{clamped % 60:02d}"

    @staticmethod
    def _priority_level_text(score: int) -> str:
        value = int(score or 0)
        if value >= 18:
            return "高优"
        if value >= 10:
            return "中优"
        return "低优"

    def _resolve_check_view_mode(self, query_text: str = ""):
        text = str(query_text or "").strip()
        low = text.lower()
        explicit_raw = bool(re.search(r"(原始|原样|\braw\b)", text, re.IGNORECASE))
        explicit_plan = bool(re.search(r"(计划|安排|安排表|排程|时间线|标签)", text, re.IGNORECASE))
        cleaned = re.sub(r"(原始|原样|\braw\b|计划|安排表|安排|排程|时间线|标签)", " ", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if explicit_raw:
            return "raw", cleaned
        if explicit_plan:
            return "plan", cleaned
        configured = str(self.config.get("todo_check_default_mode", "plan") or "plan").strip().lower()
        mode = "plan" if configured not in ("raw", "original") else "raw"
        if low in ("", "今天", "明天", "后天"):
            return mode, cleaned
        return mode, cleaned

    @staticmethod
    def _rule_rank_unscheduled(content: str, status: str = "pending") -> tuple:
        text = str(content or "").lower()
        urgent_words = ("紧急", "马上", "立刻", "尽快", "截止", "ddl", "提交", "开会", "会议", "汇报", "回复")
        important_words = ("项目", "客户", "面试", "考试", "论文", "合同", "报销", "发布")
        light_words = ("整理", "学习", "阅读", "复盘", "看看", "收拾", "散步")
        urgency = 1 + sum(1 for w in urgent_words if w in text)
        importance = 1 + sum(1 for w in important_words if w in text)
        effort = 1 + sum(1 for w in light_words if w in text)
        if status == "rolled_over":
            urgency += 1
        score = urgency * 3 + importance * 2 - effort
        duration = 45 if effort >= 3 else 30
        return score, duration

    async def _llm_rank_unscheduled(self, event: AstrMessageEvent, target_date: str, items: list):
        if not bool(self.config.get("todo_llm_priority_enable", False)):
            return {}
        provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
        if not provider_id:
            return {}
        payload_items = []
        for idx, item in enumerate(items, 1):
            payload_items.append({
                "index": idx,
                "content": str(item.get("content", "")),
                "tag": str(item.get("tag_name", "")),
                "status": str(item.get("status", "pending"))
            })
        prompt = (
            "你是任务调度器。请仅返回 JSON 数组，每项包含 index, priority, duration_min。\n"
            f"日期：{target_date}\n"
            f"任务：{json.dumps(payload_items, ensure_ascii=False)}\n"
            "约束：priority 为 1-10，越大越优先；duration_min 为 15-120 的 5 分钟整数。\n"
            "不要输出解释，不要 markdown。"
        )
        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            text = self._extract_completion_text(resp)
            if not text:
                return {}
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            data = json.loads(stripped)
            result = {}
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    idx = int(row.get("index", 0))
                    pri = int(row.get("priority", 0))
                    dur = int(row.get("duration_min", 0))
                    if idx <= 0:
                        continue
                    pri = max(1, min(10, pri))
                    if dur <= 0:
                        dur = int(self.config.get("todo_default_flexible_duration_minutes", 30))
                    dur = max(15, min(120, dur))
                    dur = (dur // 5) * 5
                    result[idx - 1] = {"priority": pri, "duration": dur}
            return result
        except Exception:
            return {}

    async def _build_plan_result(self, event: AstrMessageEvent, target_date: str, todos: list):
        unfinished = [item for item in todos if self._is_unfinished_todo(item)]
        if not unfinished:
            return {
                "timeline": [],
                "backlog": [],
                "total": len(todos),
                "unfinished_count": 0,
                "fixed_count": 0
            }
        fixed_block = int(self.config.get("todo_fixed_task_block_minutes", 45) or 45)
        fixed_block = max(15, min(180, fixed_block))
        default_duration = int(self.config.get("todo_default_flexible_duration_minutes", 30) or 30)
        default_duration = max(15, min(120, default_duration))
        day_start = self._parse_hhmm_minutes(self._normalize_hhmm(str(self.config.get("todo_workday_start", "09:00")), "09:00"))
        day_end = self._parse_hhmm_minutes(self._normalize_hhmm(str(self.config.get("todo_workday_end", "22:00")), "22:00"))
        if day_start is None:
            day_start = 9 * 60
        if day_end is None:
            day_end = 22 * 60
        if day_end <= day_start:
            day_end = day_start + 8 * 60
        min_gap = int(self.config.get("todo_min_gap_minutes", 15) or 15)
        min_gap = max(10, min(60, min_gap))

        fixed = []
        flex = []
        for item in unfinished:
            minute = self._parse_hhmm_minutes(item.get("time"))
            copied = dict(item)
            copied["_minute"] = minute
            if minute is None:
                flex.append(copied)
            else:
                fixed.append(copied)

        tag_order = self._tag_order_map()
        fixed.sort(key=lambda x: (x.get("_minute", 0), tag_order.get(str(x.get("tag_name", "")).strip(), 999), str(x.get("content", ""))))

        llm_rank = await self._llm_rank_unscheduled(event, target_date, flex)
        ranked_flex = []
        for idx, item in enumerate(flex):
            if idx in llm_rank:
                pri = llm_rank[idx]["priority"] * 10
                dur = llm_rank[idx]["duration"]
            else:
                score, dur = self._rule_rank_unscheduled(item.get("content", ""), item.get("status", "pending"))
                pri = score
            ranked_flex.append((pri, dur if dur > 0 else default_duration, item))
        ranked_flex.sort(key=lambda t: (tag_order.get(str(t[2].get("tag_name", "")).strip(), 999), -t[0], str(t[2].get("content", ""))))

        occupied = []
        timeline = []
        for item in fixed:
            start = item.get("_minute")
            end = min(day_end, start + fixed_block)
            occupied.append((start, end))
            timeline.append({
                "kind": "fixed",
                "start": start,
                "end": end,
                "item": item
            })
        occupied.sort(key=lambda p: p[0])
        gaps = []
        cursor = day_start
        for start, end in occupied:
            if start - cursor >= min_gap:
                gaps.append([cursor, start])
            cursor = max(cursor, end)
        if day_end - cursor >= min_gap:
            gaps.append([cursor, day_end])

        backlog = []
        for pri, duration, item in ranked_flex:
            placed = False
            duration = max(min_gap, duration)
            for gap in gaps:
                gs, ge = gap[0], gap[1]
                if ge - gs >= duration:
                    start = gs
                    end = gs + duration
                    timeline.append({
                        "kind": "flex",
                        "start": start,
                        "end": end,
                        "item": item,
                        "priority": pri
                    })
                    gap[0] = end
                    placed = True
                    break
            if not placed:
                backlog.append({
                    "item": item,
                    "priority": pri,
                    "duration": duration
                })
        timeline.sort(key=lambda x: x["start"])
        return {
            "timeline": timeline,
            "backlog": backlog,
            "total": len(todos),
            "unfinished_count": len(unfinished),
            "fixed_count": len(fixed)
        }

    def _format_plan_preview(self, target_date: str, plan_result: dict) -> str:
        timeline = plan_result.get("timeline", []) or []
        backlog = plan_result.get("backlog", []) or []
        total = int(plan_result.get("total", 0))
        unfinished_count = int(plan_result.get("unfinished_count", 0))
        fixed_count = int(plan_result.get("fixed_count", 0))
        show_virtual_time = bool(self.config.get("todo_plan_show_virtual_time", False))
        show_priority_level = bool(self.config.get("todo_plan_show_priority_level", True))
        lines = [
            "今日执行建议",
            f"总计 {total} 项，未完成 {unfinished_count} 项，固定时段 {fixed_count} 项。"
        ]
        if not timeline and not backlog:
            lines.append("今天的待办都完成啦。")
            return "\n".join(lines)
        fixed_rows = [row for row in timeline if row.get("kind") == "fixed"]
        flex_rows = [row for row in timeline if row.get("kind") != "fixed"]
        tag_order = self._tag_order_map()
        default_tags = self._default_tags()
        def _merge_duplicate_rows(rows: list) -> list:
            merged = {}
            order = []
            for row in rows:
                item = row.get("item", {}) if isinstance(row, dict) else {}
                key = (
                    str(row.get("kind", "")).strip(),
                    str(item.get("tag_name", "")).strip(),
                    int(item.get("tag_id", 0) or 0),
                    str(item.get("time", "")).strip(),
                    " ".join(str(item.get("content", "")).strip().split()).lower()
                )
                if key not in merged:
                    copied = dict(row)
                    copied_item = dict(item)
                    copied["item"] = copied_item
                    copied["_merged_indices"] = [int(copied_item.get("index", 0) or 0)]
                    status = str(copied_item.get("status", "pending"))
                    copied["_has_rolled"] = status == "rolled_over"
                    copied["_has_pending"] = status == "pending"
                    merged[key] = copied
                    order.append(key)
                    continue
                target = merged[key]
                target_item = target.get("item", {})
                source_index = int(item.get("index", 0) or 0)
                if source_index > 0:
                    target.setdefault("_merged_indices", []).append(source_index)
                source_status = str(item.get("status", "pending"))
                if source_status == "rolled_over":
                    target["_has_rolled"] = True
                if source_status == "pending":
                    target["_has_pending"] = True
                if int(item.get("index", 0) or 0) < int(target_item.get("index", 0) or 0):
                    target_item["index"] = item.get("index", target_item.get("index", 0))
                if int(row.get("priority", 0) or 0) > int(target.get("priority", 0) or 0):
                    target["priority"] = row.get("priority", target.get("priority", 0))
            result = []
            for key in order:
                row = merged[key]
                idxs = sorted({i for i in row.get("_merged_indices", []) if i > 0})
                row["_merged_indices"] = idxs
                result.append(row)
            return result
        def _row_tag_name(row: dict) -> str:
            item = row.get("item", {}) if isinstance(row, dict) else {}
            name = str(item.get("tag_name", "")).strip()
            if name:
                return name
            tag_id = int(item.get("tag_id", 0) or 0)
            if tag_id > 0 and tag_id <= len(default_tags):
                return default_tags[tag_id - 1]
            return "未分类"
        original_timeline_count = len(fixed_rows) + len(flex_rows)
        fixed_rows = _merge_duplicate_rows(fixed_rows)
        flex_rows = _merge_duplicate_rows(flex_rows)
        merged_hidden_count = max(0, original_timeline_count - len(fixed_rows) - len(flex_rows))
        flex_rows.sort(key=lambda row: (tag_order.get(_row_tag_name(row), 999), -int(row.get("priority", 0)), str((row.get("item", {}) or {}).get("content", ""))))
        if fixed_rows:
            lines.append("")
            lines.append("固定时段任务：")
            for idx, row in enumerate(fixed_rows, 1):
                item = row.get("item", {})
                prefix = self._tag_display_prefix(item.get("tag_name", ""), item.get("tag_id", 0))
                content = str(item.get("content", ""))
                rollover_mark = "↪ " if bool(row.get("_has_rolled", False)) else ""
                start = self._format_minutes_hhmm(row.get("start", 0))
                serial = int(item.get("index", idx) or idx)
                lines.append(f"{serial}. ⏰ {start} {rollover_mark}{prefix}{content}".strip())
        if flex_rows:
            lines.append("")
            lines.append("优先任务队列：")
            current_tag = None
            for idx, row in enumerate(flex_rows, 1):
                item = row.get("item", {})
                tag_name = _row_tag_name(row)
                if tag_name != current_tag:
                    lines.append(f"{tag_name}：")
                    current_tag = tag_name
                serial = int(item.get("index", idx) or idx)
                prefix = self._tag_display_prefix(item.get("tag_name", ""), item.get("tag_id", 0))
                content = str(item.get("content", ""))
                rollover_mark = "↪ " if bool(row.get("_has_rolled", False)) else ""
                merged_note = "，含顺延" if bool(row.get("_has_rolled", False)) and bool(row.get("_has_pending", False)) else ""
                level_text = self._priority_level_text(row.get("priority", 0))
                if show_virtual_time:
                    start = self._format_minutes_hhmm(row.get("start", 0))
                    end = self._format_minutes_hhmm(row.get("end", row.get("start", 0)))
                    if show_priority_level:
                        lines.append(f"{serial}. {rollover_mark}{prefix}{content}（{start}-{end}，{level_text}{merged_note}）".strip())
                    else:
                        lines.append(f"{serial}. {rollover_mark}{prefix}{content}（{start}-{end}）".strip())
                else:
                    if show_priority_level:
                        lines.append(f"{serial}. {rollover_mark}{prefix}{content}（{level_text}{merged_note}）".strip())
                    else:
                        lines.append(f"{serial}. {rollover_mark}{prefix}{content}".strip())
        if backlog:
            lines.append("")
            lines.append("候补：")
            for idx, row in enumerate(backlog, 1):
                item = row.get("item", {})
                prefix = self._tag_display_prefix(item.get("tag_name", ""), item.get("tag_id", 0))
                content = str(item.get("content", ""))
                duration = int(row.get("duration", 0))
                level_text = self._priority_level_text(row.get("priority", 0))
                if show_priority_level:
                    lines.append(f"{idx}. {prefix}{content}（{level_text}，建议 {duration} 分钟）".strip())
                else:
                    lines.append(f"{idx}. {prefix}{content}（建议 {duration} 分钟）".strip())
        if merged_hidden_count > 0:
            lines.append("")
            lines.append(f"已合并重复项 {merged_hidden_count} 条（含顺延同内容）。")
        lines.append("")
        lines.append("回复“check 原始”可查看完整明细并使用精确序号操作，回复“check 今天”可重新生成建议。")
        return "\n".join(lines)

    async def _send_text_via_tool(self, origin: str, text: str) -> bool:
        plain_message = [{"type": "plain", "text": text}]
        session_id = self._origin_session_id(origin)
        direct_method = getattr(self.context, "send_message_to_user", None)
        if callable(direct_method):
            direct_payloads = [
                {"messages": plain_message, "unified_msg_origin": origin},
                {"messages": plain_message, "session_id": session_id},
                {"messages": plain_message}
            ]
            for payload in direct_payloads:
                try:
                    result = await self._call_maybe_async(direct_method, **payload)
                    if not self._is_send_result_success(result):
                        self._last_send_error = f"direct_send_message_to_user({list(payload.keys())}) returned non-success: {result}"
                        logger.debug(f"send_text_via_direct_method non-success: payload_keys={list(payload.keys())}, result={result}")
                        continue
                    logger.info(f"send_text_via_direct_method success: payload_keys={list(payload.keys())}, result={str(result)[:120]}")
                    return True
                except Exception as e:
                    self._last_send_error = f"direct_send_message_to_user({list(payload.keys())}): {e}"
                    logger.debug(f"send_text_via_direct_method failed: payload_keys={list(payload.keys())}, error={e}")
        tool_executor = self._get_tool_executor()
        if not callable(tool_executor):
            return False
        payloads = [
            {"messages": plain_message, "unified_msg_origin": origin},
            {"messages": plain_message, "session_id": session_id},
            {"messages": plain_message}
        ]
        for payload in payloads:
            try:
                result = await self._call_tool_executor(tool_executor, "send_message_to_user", payload)
                if not self._is_send_result_success(result):
                    self._last_send_error = f"tool_send_message_to_user({list(payload.keys())}) returned non-success: {result}"
                    logger.debug(f"send_text_via_tool non-success: payload_keys={list(payload.keys())}, result={result}")
                    continue
                logger.info(f"send_text_via_tool success: payload_keys={list(payload.keys())}, result={str(result)[:120]}")
                return True
            except Exception as e:
                self._last_send_error = f"tool_send_message_to_user({list(payload.keys())}): {e}"
                logger.debug(f"send_text_via_tool failed: payload_keys={list(payload.keys())}, error={e}")
                continue
        return False

    def _is_system_scheduler_active_for_user(self, user: dict) -> bool:
        if not self._future_task_available():
            return False
        if not isinstance(user, dict):
            return False
        scheduler = str(user.get("reminder_scheduler", "") or "")
        task_id = str(user.get("reminder_task_id", "") or "")
        return scheduler == "system" and bool(task_id)

    def _build_reminder_task_name(self, platform: str, user_id: str) -> str:
        return f"todopal_reminder_{platform}_{user_id}"

    def _build_reminder_signature(self, interval_minutes: int, start: str, end: str, origin: str) -> str:
        return f"{interval_minutes}|{start}|{end}|{origin}"

    @staticmethod
    def _build_cron_expression(interval_minutes: int) -> str:
        minutes = max(1, int(interval_minutes))
        if minutes < 60:
            return f"*/{minutes} * * * *"
        if minutes % 60 == 0:
            hours = max(1, minutes // 60)
            return f"0 */{hours} * * *"
        return f"*/{minutes} * * * *"

    @staticmethod
    def _extract_task_entries(raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("tasks", "items", "data", "result"):
                value = raw.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    nested = value.get("tasks") or value.get("items")
                    if isinstance(nested, list):
                        return nested
                    return [value]
            return [raw]
        return []

    @staticmethod
    def _task_id(task) -> str:
        if isinstance(task, dict):
            for key in ("task_id", "id", "uuid"):
                value = task.get(key)
                if isinstance(value, str) and value:
                    return value
            for key in ("task", "data", "result"):
                nested = task.get(key)
                if isinstance(nested, dict):
                    for nested_key in ("task_id", "id", "uuid"):
                        nested_value = nested.get(nested_key)
                        if isinstance(nested_value, str) and nested_value:
                            return nested_value
        return ""

    @staticmethod
    def _task_name(task) -> str:
        if isinstance(task, dict):
            value = task.get("name")
            if isinstance(value, str):
                return value
            nested = task.get("task")
            if isinstance(nested, dict):
                nested_name = nested.get("name")
                if isinstance(nested_name, str):
                    return nested_name
        return ""

    def _build_future_task_note(self, platform: str, user_id: str, start: str, end: str) -> str:
        return (
            "【TodoPal 定时提醒执行指令】"
            f"目标用户平台={platform}，用户ID={user_id}。"
            f"只在 {start}-{end} 时间段执行。"
            "先调用 todo_check(date='今天') 获取清单。"
            "如果存在未完成待办，调用 send_message_to_user 发送提醒。"
            "提醒文案：你今天还有待办未完成，记得处理一下。"
            "如果没有未完成待办，不发送消息。"
        )

    async def _list_future_tasks(self):
        _, _, list_method = self._get_future_task_methods()
        if not callable(list_method):
            return []
        try:
            result = await self._call_maybe_async(list_method)
            return self._extract_task_entries(result)
        except TypeError:
            try:
                result = await self._call_maybe_async(list_method, {})
                return self._extract_task_entries(result)
            except Exception:
                return []
        except Exception:
            return []

    async def _delete_future_task_by_id(self, task_id: str) -> bool:
        if not task_id:
            return False
        _, delete_method, _ = self._get_future_task_methods()
        if not callable(delete_method):
            return False
        attempts = [
            {"task_id": task_id},
            {"id": task_id},
            {"task_uuid": task_id}
        ]
        for payload in attempts:
            try:
                await self._call_maybe_async(delete_method, **payload)
                return True
            except TypeError:
                continue
            except Exception:
                continue
        try:
            await self._call_maybe_async(delete_method, task_id)
            return True
        except Exception:
            return False

    async def _create_future_task(self, name: str, note: str, cron_expression: str):
        create_method, _, _ = self._get_future_task_methods()
        if not callable(create_method):
            return None
        attempts = [
            {"name": name, "note": note, "cron_expression": cron_expression, "task_type": "active_agent", "run_once": False},
            {"name": name, "note": note, "cron_expression": cron_expression, "task_type": "active_agent"},
            {"name": name, "note": note, "cron_expression": cron_expression, "run_once": False},
            {"name": name, "note": note, "cron_expression": cron_expression},
            {"name": name, "note": note, "cron": cron_expression},
            {"task_name": name, "note": note, "cron_expression": cron_expression, "run_once": False},
            {"task_name": name, "note": note, "cron_expression": cron_expression, "task_type": "active_agent", "run_once": False}
        ]
        for payload in attempts:
            try:
                logger.info(f"Create future task attempt: name={name}, payload_keys={list(payload.keys())}")
                result = await self._call_maybe_async(create_method, **payload)
                logger.info(f"Create future task result type={type(result).__name__}")
                return result
            except TypeError:
                continue
            except Exception as e:
                logger.error(f"Create future task failed for {name}: {e}")
                continue
        return None

    async def _sync_user_reminder_task(self, platform: str, user_id: str, origin: str):
        if not platform or not user_id:
            return
        if not self._future_task_available():
            return
        user_info = self.storage.get_user_info(platform, user_id)
        interval_minutes = self._resolve_reminder_interval_minutes(self.config)
        start = self._normalize_hhmm(self.config.get("reminder_start", "09:00"), "09:00")
        end = self._normalize_hhmm(self.config.get("reminder_end", "22:00"), "22:00")
        enabled = bool(self.config.get("reminder_enable", False))
        task_name = self._build_reminder_task_name(platform, user_id)
        stored_task_id = str(user_info.get("reminder_task_id", "") or "")
        stored_signature = str(user_info.get("reminder_signature", "") or "")
        new_signature = self._build_reminder_signature(interval_minutes, start, end, origin or "")
        if not enabled:
            if stored_task_id:
                await self._delete_future_task_by_id(stored_task_id)
            self.storage.update_user_info(platform, user_id, {
                "reminder_task_id": "",
                "reminder_task_name": task_name,
                "reminder_signature": "",
                "reminder_scheduler": "system_disabled"
            })
            return
        if stored_task_id and stored_signature == new_signature:
            self.storage.update_user_info(platform, user_id, {
                "reminder_task_name": task_name,
                "reminder_scheduler": "system"
            })
            return
        existing_tasks = await self._list_future_tasks()
        old_task_ids = set()
        if stored_task_id:
            old_task_ids.add(stored_task_id)
        for task in existing_tasks:
            if self._task_name(task) == task_name:
                task_id = self._task_id(task)
                if task_id:
                    old_task_ids.add(task_id)
        cron_expression = self._build_cron_expression(interval_minutes)
        note = self._build_future_task_note(platform, user_id, start, end)
        created = await self._create_future_task(task_name, note, cron_expression)
        created_id = self._task_id(created)
        if not created_id and old_task_ids:
            for old_id in old_task_ids:
                await self._delete_future_task_by_id(old_id)
            created = await self._create_future_task(task_name, note, cron_expression)
            created_id = self._task_id(created)
        if created_id:
            for old_id in old_task_ids:
                if old_id != created_id:
                    await self._delete_future_task_by_id(old_id)
        self.storage.update_user_info(platform, user_id, {
            "reminder_task_id": created_id,
            "reminder_task_name": task_name,
            "reminder_signature": new_signature,
            "reminder_scheduler": "system" if created_id else "system_failed"
        })
        logger.info(f"Reminder task sync result: user={platform}/{user_id}, task_id={created_id or 'none'}, scheduler={'system' if created_id else 'local_fallback'}")

    async def _sync_all_users_reminder_tasks(self):
        if not self._future_task_available():
            return
        users = self.storage.get_all_users()
        for user in users:
            platform = user.get("platform")
            user_id = user.get("user_id")
            origin = user.get("origin", "")
            if not platform or not user_id:
                continue
            try:
                await self._sync_user_reminder_task(platform, user_id, origin)
            except Exception as e:
                logger.error(f"Reminder task sync failed for {platform}/{user_id}: {e}")

    async def _bootstrap_scheduler_sync(self):
        try:
            await asyncio.sleep(2)
            logger.info(f"TodoPal scheduler mode: {'system' if self._future_task_available() else 'local'}")
            await self._sync_all_users_reminder_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Scheduler bootstrap sync failed: {e}")

    async def _send_text_to_origin(self, origin: str, text: str) -> bool:
        if not origin or not text:
            self._last_send_error = "invalid payload"
            logger.warning(f"send_text_to_origin skipped: invalid payload origin={bool(origin)} text={bool(text)}")
            return False
        self._last_send_error = ""
        if await self._send_text_via_tool(origin, text):
            return True
        send_method = getattr(self.context, "send_message", None)
        if not callable(send_method):
            self._last_send_error = "context.send_message is not callable"
            logger.error("send_text_to_origin failed: context.send_message is not callable")
            return False
        try:
            await send_method(origin, text)
            return True
        except TypeError as e:
            self._last_send_error = f"send_message(origin,text) type error: {e}"
            logger.debug(f"send_text_to_origin attempt1(origin,text) type error: {e}")
            try:
                await send_method(umo=origin, message=text)
                return True
            except Exception as e2:
                self._last_send_error = f"send_message(umo,message=text) error: {e2}"
                logger.debug(f"send_text_to_origin attempt2(umo,message=text) failed: {e2}")
        except Exception as e:
            self._last_send_error = f"send_message(origin,text) error: {e}"
            logger.debug(f"send_text_to_origin attempt1(origin,text) failed: {e}")
        message_result = self._build_plain_message_result(text)
        try:
            await send_method(origin, message_result)
            return True
        except TypeError as e:
            self._last_send_error = f"send_message(origin,message_result) type error: {e}"
            logger.debug(f"send_text_to_origin attempt3(origin,message_result) type error: {e}")
            try:
                await send_method(umo=origin, message=message_result)
                return True
            except Exception as e2:
                self._last_send_error = f"send_message(umo,message=message_result) error: {e2}"
                logger.debug(f"send_text_to_origin attempt4(umo,message_result) failed: {e2}")
        except Exception as e:
            self._last_send_error = f"send_message(origin,message_result) error: {e}"
            logger.debug(f"send_text_to_origin attempt3(origin,message_result) failed: {e}")
        if not self._last_send_error:
            self._last_send_error = "all send paths exhausted"
        logger.error("send_text_to_origin failed: all send paths exhausted")
        return False

    @staticmethod
    def _build_fallback_summary_text(todos: list, completed: list, pending: list) -> str:
        total = len(todos)
        done_count = len(completed)
        pending_count = len(pending)
        return f"今日待办总结：共{total}项，已完成{done_count}项，未完成{pending_count}项。"

    @staticmethod
    def _build_fallback_reminder_text(pending: list) -> str:
        top_items = [str(t.get("content", "")).strip() for t in pending if str(t.get("content", "")).strip()]
        top_items = top_items[:3]
        if not top_items:
            return "你还有未完成的待办，记得处理一下。"
        return f"你还有{len(pending)}项待办未完成：{'；'.join(top_items)}。"

    def _resolve_reminder_text_mode(self) -> str:
        mode = str(self.config.get("reminder_text_mode", "template")).strip().lower()
        if mode == "llm":
            return "llm"
        return "template"

    def _subscription_required_for_reminder(self) -> bool:
        return bool(self.config.get("reminder_require_subscription", True))

    def _subscription_default_on(self) -> bool:
        return bool(self.config.get("reminder_subscription_default_on", False))

    def _is_user_reminder_subscribed(self, user: dict) -> bool:
        if not self._subscription_required_for_reminder():
            return True
        if not isinstance(user, dict):
            return False
        subscribed = user.get("reminder_subscribed", None)
        if subscribed is None:
            return self._subscription_default_on()
        return bool(subscribed)

    def _set_user_reminder_subscription(self, platform: str, user_id: str, subscribed: bool):
        self.storage.update_user_info(platform, user_id, {
            "reminder_subscribed": bool(subscribed)
        })

    def _render_reminder_template(self, pending: list) -> str:
        top_items = [str(t.get("content", "")).strip() for t in pending if str(t.get("content", "")).strip()]
        top_items = top_items[:3]
        default_template = "你今天还有{pending_count}项待办未完成：{pending_preview}。"
        template = str(self.config.get("reminder_template", default_template) or "").strip()
        if not template:
            template = default_template
        pending_preview = "；".join(top_items) if top_items else "记得处理今日待办"
        values = {
            "pending_count": str(len(pending)),
            "pending_preview": pending_preview,
            "top1": top_items[0] if len(top_items) >= 1 else "",
            "top2": top_items[1] if len(top_items) >= 2 else "",
            "top3": top_items[2] if len(top_items) >= 3 else ""
        }
        text = template
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text

    @staticmethod
    def _persona_text_from_data(persona_data):
        if not persona_data:
            return ""
        if isinstance(persona_data, str):
            return persona_data.strip()
        if isinstance(persona_data, dict):
            for key in ("prompt", "system_prompt", "content", "description", "text"):
                value = persona_data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            name = persona_data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
            return ""
        for key in ("prompt", "system_prompt", "content", "description", "text", "name"):
            value = getattr(persona_data, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _get_persona_instruction(self, persona: str, custom_prompt: str) -> str:
        if custom_prompt:
            return f"人格设定：\n{custom_prompt}\n"
        if not persona:
            return ""
        persona_text = ""
        get_persona = getattr(self.context, "get_persona", None)
        if callable(get_persona):
            try:
                persona_obj = get_persona(persona)
                if asyncio.iscoroutine(persona_obj):
                    persona_obj = await persona_obj
                persona_text = self._persona_text_from_data(persona_obj)
            except Exception as e:
                logger.debug(f"Get persona failed for {persona}: {e}")
        if persona_text:
            return f"人格设定：\n{persona_text}\n"
        logger.debug(f"Persona details not found for {persona}, fallback to id prompt")
        return f"人格设定ID：{persona}\n"

    @staticmethod
    def _normalize_date_str(date_text: str):
        if not date_text:
            return None
        txt = str(date_text).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        matched = re.match(r"^(\d{1,2})月(\d{1,2})日$", txt)
        if matched:
            now = datetime.now()
            month = int(matched.group(1))
            day = int(matched.group(2))
            try:
                return datetime(now.year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return None
        return None

    def _resolve_check_date(self, query_text: str = "", payload=None):
        payload_date = None
        if isinstance(payload, dict):
            payload_date = self._normalize_date_str(payload.get("date"))
        elif isinstance(payload, str):
            payload_date = self._normalize_date_str(payload)
        if payload_date:
            return payload_date
        text = (query_text or "").strip()
        now = datetime.now()
        if "后天" in text:
            return (now + timedelta(days=2)).strftime("%Y-%m-%d")
        if "明天" in text:
            return (now + timedelta(days=1)).strftime("%Y-%m-%d")
        if "今天" in text:
            return now.strftime("%Y-%m-%d")
        explicit = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if explicit:
            parsed = self._normalize_date_str(explicit.group(1))
            if parsed:
                return parsed
        md = re.search(r"(\d{1,2})月(\d{1,2})日", text)
        if md:
            parsed = self._normalize_date_str(f"{md.group(1)}月{md.group(2)}日")
            if parsed:
                return parsed
        return now.strftime("%Y-%m-%d")

    async def _reply_with_persona_prefix(self, event, lead_text: str, body_text: str):
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        merged_text = f"{lead_text}\n\n{body_text}" if body_text else lead_text
        if not persona and not custom_prompt:
            return event.plain_result(merged_text)
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
        if not provider_id:
            logger.debug("Persona prefix fallback: provider_id not found")
            return event.plain_result(merged_text)
        prompt = f"""
你是一个助手。请根据以下人格设定，生成一句简短开场白，语气自然，不超过30字。
{persona_instruction}
开场白意图：{lead_text}

只输出一句开场白，不要输出列表，不要加引号。
"""
        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            if resp and hasattr(resp, "completion_text") and resp.completion_text:
                prefix = resp.completion_text.strip().splitlines()[0].strip()
                if prefix:
                    return event.plain_result(f"{prefix}\n\n{body_text}" if body_text else prefix)
            logger.debug("Persona prefix fallback: empty llm response")
        except Exception as e:
            logger.error(f"Persona prefix failed: {e}")
        return event.plain_result(merged_text)

    @staticmethod
    def _extract_completion_text(response) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        text = getattr(response, "completion_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        return ""

    @staticmethod
    def _sanitize_intro_text(text: str) -> str:
        if not text:
            return ""
        one_line = re.sub(r"\s+", " ", str(text)).strip()
        one_line = one_line.replace("\n", " ").strip()
        if not one_line:
            return ""
        one_line = re.sub(r"^[\-\d\.\)\s]+", "", one_line).strip()
        if one_line.endswith(("?", "？")):
            one_line = one_line[:-1].rstrip() + "。"
        if len(one_line) > 60:
            one_line = one_line[:60].rstrip("，,;；。.!！？?") + "。"
        return one_line

    async def _iter_llm_stream_chunks(self, provider_id: str, prompt: str):
        method = getattr(self.context, "llm_generate", None)
        if not callable(method):
            return
        result = method(chat_provider_id=provider_id, prompt=prompt, stream=True)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        if hasattr(result, "__aiter__"):
            async for chunk in result:
                text = self._extract_completion_text(chunk)
                if text:
                    yield text
            return
        if hasattr(result, "__iter__") and not isinstance(result, (str, bytes, dict)):
            for chunk in result:
                text = self._extract_completion_text(chunk)
                if text:
                    yield text
            return
        text = self._extract_completion_text(result)
        if text:
            yield text

    async def _generate_check_intro_segments(self, event: AstrMessageEvent, title: str, todos: list):
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
        if not provider_id:
            return []

        prompt = f"""
你是一个待办助手。请先给用户一句自然回应，再由系统发送清单详情。
{persona_instruction}
场景：用户请求查看待办事项。
清单标题：{title}
待办数量：{len(todos)}

要求：
1. 只输出一句短句，不超过30字。
2. 不要提问，不要反问，不要输出列表。
3. 语气自然、贴合人格设定。
"""
        stream_text = ""
        try:
            async for piece in self._iter_llm_stream_chunks(provider_id, prompt):
                if piece:
                    stripped_piece = piece.strip()
                    if not stripped_piece:
                        continue
                    if stream_text and stripped_piece.startswith(stream_text):
                        stream_text = stripped_piece
                    elif stripped_piece.startswith(stream_text):
                        stream_text = stripped_piece
                    else:
                        stream_text += stripped_piece
            if stream_text:
                merged = self._sanitize_intro_text(stream_text)
                if merged:
                    segments = []
                    buf = ""
                    for ch in merged:
                        buf += ch
                        if ch in "，。！？；,.!?;" or len(buf) >= 18:
                            part = buf.strip()
                            if part:
                                segments.append(part)
                            buf = ""
                    tail = buf.strip()
                    if tail:
                        segments.append(tail)
                    if segments:
                        return segments
        except Exception as e:
            logger.debug(f"Stream intro failed, fallback to non-stream: {e}")

        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            final_text = self._sanitize_intro_text(self._extract_completion_text(resp))
            if final_text:
                return [final_text]
        except Exception as e:
            logger.debug(f"Non-stream intro failed: {e}")
        return []

    async def _cron_loop(self):
        """Background task to handle reminders and summaries."""
        logger.info("TodoPal cron loop started.")
        last_reminders = {}
        
        while True:
            try:
                now = datetime.now()
                current_time_str = now.strftime("%H:%M")
                today_str = now.strftime("%Y-%m-%d")
                summary_time = self._normalize_hhmm(self.config.get("summary_time", "23:00"), "23:00")
                reminder_start = self._normalize_hhmm(self.config.get("reminder_start", "09:00"), "09:00")
                reminder_end = self._normalize_hhmm(self.config.get("reminder_end", "22:00"), "22:00")
                reminder_interval_minutes = self._resolve_reminder_interval_minutes(self.config)
                
                if self.config.get("auto_rollover", True):
                    if today_str != self._last_rollover_date:
                        self._do_rollover(today_str)
                        self._last_rollover_date = today_str

                users = self.storage.get_all_users()
                for u in users:
                    platform = u.get("platform")
                    user_id = u.get("user_id")
                    origin = u.get("origin")
                    if not platform or not user_id or not origin:
                        continue
                    cached_provider_id = u.get("provider_id")
                    user_key = f"{platform}_{user_id}"
                    provider_id = cached_provider_id or await self._get_provider_id_from_origin(origin)
                    if provider_id and provider_id != cached_provider_id:
                        self.storage.register_user(platform, user_id, origin, provider_id)
                    
                    if self.config.get("summary_enable", True):
                        if current_time_str >= summary_time and self._last_summary_sent.get(user_key) != today_str:
                            sent = await self._send_proactive_summary(platform, user_id, origin, today_str, provider_id)
                            if sent:
                                self._last_summary_sent[user_key] = today_str
                    
                    if self.config.get("reminder_enable", False) and not self._is_system_scheduler_active_for_user(u) and self._is_user_reminder_subscribed(u):
                        if reminder_start <= current_time_str <= reminder_end:
                            last_time = last_reminders.get(user_key)
                            if not last_time or (now - last_time).total_seconds() >= reminder_interval_minutes * 60:
                                sent = await self._send_proactive_reminder(platform, user_id, origin, today_str, provider_id)
                                if sent:
                                    last_reminders[user_key] = now
                                else:
                                    logger.warning(f"Reminder attempted but not sent for {platform}/{user_id}")
                                    
            except Exception as e:
                logger.error(f"TodoPal cron loop error: {e}")
            
            await asyncio.sleep(60)

    def _do_rollover(self, today_str: str):
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        users = self.storage.get_all_users()
        for u in users:
            platform = u.get('platform')
            user_id = u.get('user_id')
            if not platform or not user_id:
                continue
            if self.storage.get_user_rollover_date(platform, user_id) == today_str:
                continue
            rolled = self.storage.rollover_pending_todos(platform, user_id, yesterday_str, today_str)
            self.storage.set_user_rollover_date(platform, user_id, today_str)
            if rolled > 0:
                logger.info(f"Rolled over {rolled} items for {user_id}")

    async def _send_proactive_summary(self, platform, user_id, origin, today_str, cached_provider_id=None) -> bool:
        todos = self.storage.load_todos(platform, user_id, today_str)
        if not todos:
            return False
            
        completed = [t for t in todos if t.get('status') == 'done']
        pending = [t for t in todos if self._is_unfinished_todo(t)]
        
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        
        prompt = f"""
{persona_instruction}
用户今天的待办事项总结：
- 共计 {len(todos)} 项
- 已完成 {len(completed)} 项
- 未完成 {len(pending)} 项
详情：
{[t.get('content') for t in todos]}

请生成一段总结性的话语，主动发给用户，语气要自然。不要返回JSON，直接返回要说的话。
"""
        logger.debug(f"Proactive summary prompt: {prompt}")
        provider_id = cached_provider_id or await self._get_provider_id_from_origin(origin)
        if provider_id:
            try:
                resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
                if resp and hasattr(resp, 'completion_text') and resp.completion_text:
                    msg = resp.completion_text.strip()
                    if msg:
                        sent = await self._send_text_to_origin(origin, msg)
                        if sent:
                            return True
            except Exception as e:
                logger.error(f"Proactive summary llm failed for {platform}/{user_id}: {e}")
        else:
            logger.debug(f"Summary provider unavailable for {platform}/{user_id}, fallback to template")
        fallback_text = self._build_fallback_summary_text(todos, completed, pending)
        return await self._send_text_to_origin(origin, fallback_text)

    async def _send_proactive_reminder(self, platform, user_id, origin, today_str, cached_provider_id=None) -> bool:
        todos = self.storage.load_todos(platform, user_id, today_str)
        pending = [t for t in todos if self._is_unfinished_todo(t)]
        
        if not pending:
            logger.debug(f"Reminder skipped: no pending todos for {platform}/{user_id} on {today_str}")
            return False
        if self._resolve_reminder_text_mode() != "llm":
            reminder_text = self._render_reminder_template(pending)
            return await self._send_text_to_origin(origin, reminder_text)
            
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        
        prompt = f"""
{persona_instruction}
用户还有 {len(pending)} 项待办未完成，分别是：
{[t.get('content') for t in pending]}

请生成一段简短的话语，主动提醒用户去完成任务，语气要自然。不要返回JSON，直接返回要说的话。
"""
        logger.debug(f"Proactive reminder prompt: {prompt}")
        provider_id = cached_provider_id or await self._get_provider_id_from_origin(origin)
        if provider_id:
            try:
                resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
                if resp and hasattr(resp, 'completion_text') and resp.completion_text:
                    msg = resp.completion_text.strip()
                    if msg:
                        sent = await self._send_text_to_origin(origin, msg)
                        if sent:
                            return True
            except Exception as e:
                logger.error(f"Proactive reminder llm failed for {platform}/{user_id}: {e}")
        else:
            logger.debug(f"Reminder provider unavailable for {platform}/{user_id}, fallback to template")
        fallback_text = self._render_reminder_template(pending)
        return await self._send_text_to_origin(origin, fallback_text)

    async def _reply_with_persona(self, event, plain_text: str):
        """Helper to reply with persona if configured, otherwise plain text."""
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        
        if not persona and not custom_prompt:
            return event.plain_result(plain_text)

        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)

        prompt = f"""
你是一个助手。请根据以下人格设定，将括号里的系统提示转化为符合人设的自然回复。
{persona_instruction}
系统提示：({plain_text})

请直接输出回复内容，不要加引号。
"""
        logger.debug(f"Reply persona prompt: {prompt}")
        try:
            provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
            if not provider_id:
                logger.debug("Persona fallback: provider_id not found")
                return event.plain_result(plain_text)
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            if resp and hasattr(resp, 'completion_text') and resp.completion_text:
                return event.plain_result(resp.completion_text)
            logger.debug("Persona fallback: empty llm response")
        except Exception as e:
            logger.error(f"Persona reply failed: {e}")
        
        return event.plain_result(plain_text)

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
                tag_name = str(item.get("tag_name", "")).strip()
                tag_id = int(item.get("tag_id", 0) or 0)
                
                prefix = f"{time} " if time else ""
                check_mark = "✅ " if status == "done" else ""
                rollover_mark = "↪ " if status == "rolled_over" else ""
                tag_prefix = self._tag_display_prefix(tag_name, tag_id)
                result_lines.append(f"{i}. {check_mark}{rollover_mark}{tag_prefix}{prefix}{content}")
            result_lines.append("")
        
        if include_confirm_prompt:
            result_lines.append("如果正确，请回复“确认”。")
        return "\n".join(result_lines)

    def _event_scope(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"
        return platform, user_id

    async def _register_event_user_context(self, event: AstrMessageEvent, platform: str, user_id: str):
        origin = getattr(event, "unified_msg_origin", "") or ""
        if not origin:
            return
        provider_id = await self._get_provider_id_from_origin(origin)
        self.storage.register_user(platform, user_id, origin, provider_id)
        if self._future_task_available():
            try:
                await self._sync_user_reminder_task(platform, user_id, origin)
            except Exception as e:
                logger.error(f"Reminder task sync failed for {platform}/{user_id}: {e}")

    def _resolve_date_input(self, date_text: str = "") -> str:
        if not date_text:
            return datetime.now().strftime("%Y-%m-%d")
        parsed = self._normalize_date_str(date_text)
        if parsed:
            return parsed
        return self._resolve_check_date(str(date_text), None)

    def _extract_date_hint_from_text(self, text: str):
        source = str(text or "").strip()
        if not source:
            return "", ""
        explicit = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", source)
        if explicit:
            raw = explicit.group(1)
            parsed = self._normalize_date_str(raw)
            cleaned = (source[:explicit.start()] + source[explicit.end():]).strip()
            return parsed or "", cleaned
        md = re.search(r"(\d{1,2}月\d{1,2}日)", source)
        if md:
            raw = md.group(1)
            parsed = self._normalize_date_str(raw)
            cleaned = (source[:md.start()] + source[md.end():]).strip()
            return parsed or "", cleaned
        for keyword in ("后天", "明天", "今天"):
            pos = source.find(keyword)
            if pos >= 0:
                cleaned = (source[:pos] + source[pos + len(keyword):]).strip()
                parsed = self._resolve_check_date(keyword, None)
                return parsed, cleaned
        return "", source

    @staticmethod
    def _has_explicit_date_expression(text: str) -> bool:
        source = (text or "").strip()
        if not source:
            return False
        patterns = [
            r"(今天|明天|后天)",
            r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
            r"\d{1,2}月\d{1,2}日",
            r"\b(today|tomorrow|day\s*after\s*tomorrow)\b"
        ]
        return any(re.search(pattern, source, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _has_explicit_time_expression(text: str) -> bool:
        source = (text or "").strip()
        if not source:
            return False
        patterns = [
            r"\b([01]?\d|2[0-3]):[0-5]\d\b",
            r"([01]?\d|2[0-3])点([0-5]?\d分?)?",
            r"(上午|中午|下午|晚上|凌晨|今晚|明早|明晚)",
            r"\b\d{1,2}(am|pm)\b"
        ]
        return any(re.search(pattern, source, re.IGNORECASE) for pattern in patterns)

    def _sanitize_parsed_todos(self, todos: list, source_text: str, explicit_time_text: str = "", explicit_date_text: str = "") -> list:
        allow_time = bool((explicit_time_text or "").strip()) or self._has_explicit_time_expression(source_text)
        explicit_date = self._normalize_date_str(str(explicit_date_text).strip()) if explicit_date_text else None
        allow_date = bool(explicit_date) or self._has_explicit_date_expression(source_text)
        default_date = explicit_date or datetime.now().strftime("%Y-%m-%d")
        normalized = []
        for todo in todos or []:
            item = dict(todo) if isinstance(todo, dict) else {}
            time_value = item.get("time")
            if isinstance(time_value, str):
                time_value = time_value.strip() or None
            parsed_date = self._normalize_date_str(str(item.get("date", "")).strip()) if item.get("date") else None
            if allow_date:
                item["date"] = parsed_date or default_date
            else:
                item["date"] = default_date
            if not allow_time:
                item["time"] = None
            else:
                item["time"] = time_value
            normalized.append(item)
        return normalized

    def _simple_items(self, todos: list):
        items = []
        for idx, todo in enumerate(todos, 1):
            items.append({
                "index": idx,
                "date": todo.get("date"),
                "time": todo.get("time"),
                "content": todo.get("content"),
                "status": todo.get("status", "pending"),
                "tag_id": todo.get("tag_id", 0),
                "tag_name": todo.get("tag_name", "")
            })
        return items

    @staticmethod
    def _service_message(action: str, ok: bool, error: str = "", **kwargs) -> str:
        if action == "add":
            if ok:
                return f"已识别 {kwargs.get('count', 0)} 项待办，确认后保存。"
            if error == "EMPTY_CONTENT":
                return "请输入待办内容。"
            if error == "NO_PROVIDER":
                return "未配置 LLM Provider。"
            return "未能识别到任何待办事项。"
        if action == "done":
            if ok:
                count = kwargs.get("updated_count", 0)
                if count == 0:
                    return "所选的待办事项已经是完成状态啦。"
                return f"已完成 {count} 项待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项哦。"
            if error == "NOT_FOUND":
                return "找不到对应的待办事项，请检查描述或序号是否准确。"
            return "处理失败，请重试。"
        if action == "undone":
            if ok:
                count = kwargs.get("updated_count", 0)
                if count == 0:
                    return "所选待办目前不是完成状态，无需撤销。"
                return f"已撤销完成 {count} 项待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项哦。"
            if error == "NOT_FOUND":
                return "找不到对应的待办事项，请检查描述或序号是否准确。"
            return "处理失败，请重试。"
        if action == "fix":
            if ok:
                return f"已修改第 {kwargs.get('index', 0)} 条待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项，无法修改。"
            if error == "INDEX_OUT_OF_RANGE":
                return f"找不到第 {kwargs.get('index', 0)} 条待办。"
            if error == "EMPTY_CONTENT":
                return "请输入新的待办内容。"
            return "修改失败，请重试。"
        if action == "check":
            return f"共 {kwargs.get('count', 0)} 项待办。"
        if action == "delete":
            if ok:
                count = kwargs.get("deleted_count", 0)
                return f"已删除 {count} 项待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项哦。"
            if error == "NOT_FOUND":
                return "找不到要删除的待办事项，请检查序号或关键词。"
            return "删除失败，请重试。"
        return "处理完成。"

    async def _service_check(self, platform: str, user_id: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        return {
            "ok": True,
            "action": "check",
            "date": target_date,
            "count": len(todos),
            "items": self._simple_items(todos),
            "message": self._service_message("check", True, count=len(todos))
        }

    async def _service_add(self, event: AstrMessageEvent, platform: str, user_id: str, content: str, date_text: str = "", time_text: str = "", persist: bool = True, parsed_todos: list = None):
        source_text = (content or "").strip()
        if not source_text:
            return {"ok": False, "action": "add", "error": "EMPTY_CONTENT", "message": self._service_message("add", False, "EMPTY_CONTENT")}
        todos = list(parsed_todos) if isinstance(parsed_todos, list) else []
        if not todos:
            target_date = self._resolve_date_input(date_text) if date_text else ""
            if target_date:
                todos = [{
                    "date": target_date,
                    "time": (time_text.strip() if time_text else None),
                    "content": source_text
                }]
            else:
                provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
                if not provider_id:
                    return {"ok": False, "action": "add", "error": "NO_PROVIDER", "message": self._service_message("add", False, "NO_PROVIDER")}
                todos = await parse_todo(self.context, provider_id, source_text)
                if not todos:
                    return {"ok": False, "action": "add", "error": "PARSE_FAILED", "message": self._service_message("add", False, "PARSE_FAILED")}
        todos = self._sanitize_parsed_todos(todos, source_text, time_text, date_text)
        if persist:
            self._save_todos(platform, user_id, todos, source_text, mode='append')
        grouped = {}
        for todo in todos:
            dt = todo.get("date") or datetime.now().strftime("%Y-%m-%d")
            grouped.setdefault(dt, 0)
            grouped[dt] += 1
        return {
            "ok": True,
            "action": "add",
            "added_count": len(todos),
            "dates": grouped,
            "items": todos,
            "message": self._service_message("add", True, count=len(todos))
        }

    async def _service_done(self, platform: str, user_id: str, selector: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "done", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("done", False, "EMPTY_LIST")}
        matched_indices = TodoMatcher.match_todos(todos, selector or "")
        if not matched_indices:
            return {"ok": False, "action": "done", "date": target_date, "error": "NOT_FOUND", "message": self._service_message("done", False, "NOT_FOUND")}
        updated = []
        for idx in matched_indices:
            if todos[idx].get("status") != "done":
                self.storage.update_todo_status(platform, user_id, target_date, idx, "done")
                updated.append(idx + 1)
        return {
            "ok": True,
            "action": "done",
            "date": target_date,
            "updated_indices": updated,
            "updated_count": len(updated),
            "message": self._service_message("done", True, updated_count=len(updated))
        }

    async def _service_undone(self, platform: str, user_id: str, selector: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "undone", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("undone", False, "EMPTY_LIST")}
        matched_indices = TodoMatcher.match_todos(todos, selector or "")
        if not matched_indices:
            return {"ok": False, "action": "undone", "date": target_date, "error": "NOT_FOUND", "message": self._service_message("undone", False, "NOT_FOUND")}
        updated = []
        for idx in matched_indices:
            if todos[idx].get("status") == "done":
                self.storage.update_todo_status(platform, user_id, target_date, idx, "pending")
                updated.append(idx + 1)
        return {
            "ok": True,
            "action": "undone",
            "date": target_date,
            "updated_indices": updated,
            "updated_count": len(updated),
            "message": self._service_message("undone", True, updated_count=len(updated))
        }

    async def _service_fix(self, platform: str, user_id: str, index: int, content: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "fix", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("fix", False, "EMPTY_LIST", index=index)}
        if index < 1 or index > len(todos):
            return {"ok": False, "action": "fix", "date": target_date, "error": "INDEX_OUT_OF_RANGE", "message": self._service_message("fix", False, "INDEX_OUT_OF_RANGE", index=index)}
        cleaned_content = re.sub(r"^(改成|变为|变成|是|为|:)\s*", "", (content or "").strip()).strip()
        if not cleaned_content:
            return {"ok": False, "action": "fix", "date": target_date, "error": "EMPTY_CONTENT", "message": self._service_message("fix", False, "EMPTY_CONTENT", index=index)}
        updated = self.storage.update_todo_content(platform, user_id, target_date, index - 1, cleaned_content)
        if not updated:
            return {"ok": False, "action": "fix", "date": target_date, "error": "UPDATE_FAILED", "message": self._service_message("fix", False, "UPDATE_FAILED", index=index)}
        return {
            "ok": True,
            "action": "fix",
            "date": target_date,
            "index": index,
            "item": {
                "index": index,
                "date": updated.get("date"),
                "time": updated.get("time"),
                "content": updated.get("content"),
                "status": updated.get("status", "pending")
            },
            "message": self._service_message("fix", True, index=index)
        }

    async def _service_delete(self, platform: str, user_id: str, selector: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "delete", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("delete", False, "EMPTY_LIST")}
        matched_indices = TodoMatcher.match_todos(todos, selector or "")
        if not matched_indices:
            return {"ok": False, "action": "delete", "date": target_date, "error": "NOT_FOUND", "message": self._service_message("delete", False, "NOT_FOUND")}
        delete_method = getattr(self.storage, "delete_todos", None)
        if callable(delete_method):
            deleted = delete_method(platform, user_id, target_date, matched_indices)
        else:
            valid_indices = sorted({idx for idx in matched_indices if 0 <= idx < len(todos)}, reverse=True)
            deleted = []
            for idx in valid_indices:
                item = dict(todos[idx])
                item["index"] = idx + 1
                deleted.append(item)
                todos.pop(idx)
            self.storage.save_todos(platform, user_id, target_date, todos)
            deleted.reverse()
        if not deleted:
            return {"ok": False, "action": "delete", "date": target_date, "error": "NOT_FOUND", "message": self._service_message("delete", False, "NOT_FOUND")}
        return {
            "ok": True,
            "action": "delete",
            "date": target_date,
            "deleted_count": len(deleted),
            "deleted_items": deleted,
            "message": self._service_message("delete", True, deleted_count=len(deleted))
        }

    def _tool_text_response(self, action: str, result: dict) -> str:
        if not isinstance(result, dict):
            return "处理完成。"
        if action == "check":
            if not result.get("ok"):
                return result.get("message", "获取待办失败，请重试。")
            items = result.get("items", []) or []
            date_text = result.get("date", "今天")
            if not items:
                return f"{date_text} 暂无待办。"
            lines = [f"{date_text} 待办共 {len(items)} 项："]
            for item in items[:12]:
                idx = item.get("index", 0)
                status = item.get("status", "pending")
                mark = "✅ " if status == "done" else ""
                rollover_mark = "↪ " if status == "rolled_over" else ""
                tag_name = str(item.get("tag_name", "")).strip()
                tag_id = int(item.get("tag_id", 0) or 0)
                tag_prefix = self._tag_display_prefix(tag_name, tag_id)
                tm = item.get("time")
                prefix = f"{tm} " if tm else ""
                content = item.get("content", "")
                lines.append(f"{idx}. {mark}{rollover_mark}{tag_prefix}{prefix}{content}")
            if len(items) > 12:
                lines.append(f"……其余 {len(items) - 12} 项请使用 check 查看完整清单。")
            return "\n".join(lines)
        if action == "add":
            if not result.get("ok"):
                return result.get("message", "新增失败，请重试。")
            return result.get("message", f"已新增 {result.get('added_count', 0)} 项待办。")
        if action == "done":
            return result.get("message", "处理完成。")
        if action == "undone":
            return result.get("message", "处理完成。")
        if action == "fix":
            return result.get("message", "处理完成。")
        if action == "delete":
            return result.get("message", "处理完成。")
        return result.get("message", "处理完成。")

    @filter.llm_tool(name="todo_check")
    async def todo_tool_check(self, event: AstrMessageEvent, date: str = ""):
        '''查询待办清单。

        Args:
            date(string): 日期，可为空，支持今天/明天/后天/YYYY-MM-DD/M月D日
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_check(platform, user_id, date)
        yield event.plain_result(self._tool_text_response("check", result))

    @filter.llm_tool(name="todo_add")
    async def todo_tool_add(self, event: AstrMessageEvent, content: str, date: str = "", time: str = ""):
        '''新增待办事项。

        Args:
            content(string): 待办原始内容
            date(string): 可选日期，支持YYYY-MM-DD或自然日期表达
            time(string): 可选时间，格式建议HH:MM
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_add(event, platform, user_id, content, date, time)
        yield event.plain_result(self._tool_text_response("add", result))

    @filter.llm_tool(name="todo_done")
    async def todo_tool_done(self, event: AstrMessageEvent, selector: str, date: str = ""):
        '''标记待办完成。

        Args:
            selector(string): 序号、序号列表或内容关键词
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_done(platform, user_id, selector, date)
        yield event.plain_result(self._tool_text_response("done", result))

    @filter.llm_tool(name="todo_undone")
    async def todo_tool_undone(self, event: AstrMessageEvent, selector: str, date: str = ""):
        '''撤销待办完成状态。

        Args:
            selector(string): 序号、序号列表或内容关键词
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_undone(platform, user_id, selector, date)
        yield event.plain_result(self._tool_text_response("undone", result))

    @filter.llm_tool(name="todo_fix")
    async def todo_tool_fix(self, event: AstrMessageEvent, index: int, content: str, date: str = ""):
        '''修改指定待办内容。

        Args:
            index(number): 待办序号，从1开始
            content(string): 新的待办内容
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_fix(platform, user_id, index, content, date)
        yield event.plain_result(self._tool_text_response("fix", result))

    @filter.llm_tool(name="todo_delete")
    async def todo_tool_delete(self, event: AstrMessageEvent, selector: str, date: str = ""):
        '''删除待办事项。

        Args:
            selector(string): 序号、序号列表或内容关键词
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        result = await self._service_delete(platform, user_id, selector, date)
        yield event.plain_result(self._tool_text_response("delete", result))

    @filter.regex(r"^(todo|add|done|undo|undone|撤销完成|取消完成|取消done|fix|check|del|delete|rm)\s*.*")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input.
        Supports:
        1. Explicit commands: 'todo', 'add', 'done', 'fix', 'check', 'del', 'delete', 'rm'
        2. Natural language with keywords (defined in triggers.json)
        """
        message_str = event.message_str.strip()
        if not message_str:
            return

        explicit_match = re.match(r"^(todo|add|done|undo|undone|撤销完成|取消完成|取消done|fix|check|del|delete|rm)\s*(.*)", message_str, re.IGNORECASE)
        if not explicit_match:
            return
        command_prefix = explicit_match.group(1).lower()
        if command_prefix in ("del", "delete", "rm"):
            command_prefix = "delete"
        if command_prefix in ("undo", "undone", "撤销完成", "取消完成", "取消done"):
            command_prefix = "undone"
        todo_content = explicit_match.group(2).strip()

        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        await self._register_event_user_context(event, platform, user_id)
        provider_id_for_user = await self._get_provider_id_from_origin(event.unified_msg_origin)

        if command_prefix == 'check':
            async for result in self._handle_check_command(event, platform, user_id, todo_content, None):
                yield result
            return

        if not todo_content:
            yield event.plain_result(f"请输入{command_prefix}的具体内容。")
            return

        if command_prefix == 'done':
            async for result in self._handle_done_command(event, platform, user_id, todo_content):
                yield result
            return
        if command_prefix == 'undone':
            async for result in self._handle_undone_command(event, platform, user_id, todo_content):
                yield result
            return

        if command_prefix == 'fix':
            async for result in self._handle_fix_command(event, platform, user_id, todo_content):
                yield result
            return

        if command_prefix == 'delete':
            async for result in self._handle_delete_command(event, platform, user_id, todo_content):
                yield result
            return

        provider_id = provider_id_for_user
        add_result = None
        if command_prefix == 'todo':
            if not provider_id:
                yield event.plain_result("未配置 LLM Provider。")
                return
            today = datetime.now().strftime("%Y-%m-%d")
            current_todos = self.storage.load_todos(platform, user_id, today)
            intent_result = await analyze_intent(self.context, provider_id, todo_content, current_todos)
            if not intent_result or not intent_result.get('type'):
                yield event.plain_result("未能识别你的操作意图，请换个说法试试。")
                return
            intent_type = intent_result['type']
            payload = intent_result.get('payload')
            undo_match = re.search(r"(撤销完成|取消完成|undo|undone|取消done)\s*(\d+(?:[\s,，、]+\d+)*)", todo_content, re.IGNORECASE)
            if undo_match:
                async for result in self._handle_undone_command(event, platform, user_id, undo_match.group(2).strip()):
                    yield result
                return
            if intent_type == 'check':
                async for result in self._handle_check_command(event, platform, user_id, todo_content, payload):
                    yield result
                return
            elif intent_type == 'done':
                if not payload:
                     yield event.plain_result("需要指定完成哪一项。")
                     return
                async for result in self._handle_done_command(event, platform, user_id, str(payload)):
                    yield result
                return
            elif intent_type == 'fix':
                if not payload:
                    yield event.plain_result("需要指定修改哪一项及新内容。")
                    return
                async for result in self._handle_fix_command(event, platform, user_id, str(payload)):
                    yield result
                return
            elif intent_type == 'delete':
                if not payload:
                    yield event.plain_result("需要指定删除哪一项。")
                    return
                async for result in self._handle_delete_command(event, platform, user_id, str(payload)):
                    yield result
                return
            elif intent_type == 'add':
                if isinstance(payload, list):
                    add_result = await self._service_add(event, platform, user_id, todo_content, persist=False, parsed_todos=payload)
                else:
                    add_result = await self._service_add(event, platform, user_id, todo_content, persist=False)
            elif intent_type == 'cancel':
                yield event.plain_result("已取消，不做任何变更。")
                return
            else:
                yield event.plain_result("未能识别你的操作意图。")
                return
        else:
            add_result = await self._service_add(event, platform, user_id, todo_content, persist=False)

        if not add_result or not add_result.get("ok"):
            fail_message = add_result.get("message") if isinstance(add_result, dict) else "未能识别到任何待办事项。"
            yield event.plain_result(fail_message)
            return
        todos = add_result.get("items") or []
        action_type = 'append' 
        user_tags = self._get_user_tags(platform, user_id)
        self.sessions[event.unified_msg_origin] = {
            'state': 'WAITING_TAG_ASSIGN',
            'action_type': action_type,
            'todos': todos,
            'user_tags': user_tags,
            'source_text': todo_content,
            'platform': platform,
            'user_id': user_id
        }

        preview = self._format_preview(todos, include_confirm_prompt=False)
        todo_count = len(todos)
        date_count = len({t.get("date") for t in todos if t.get("date")})
        if date_count > 1:
            lead_text = f"已整理 {todo_count} 项待办，覆盖 {date_count} 天，确认后保存。"
        else:
            lead_text = f"已整理 {todo_count} 项待办，确认后保存。"
        tag_help = self._build_tag_assign_help(todos, user_tags)
        yield event.plain_result(f"{lead_text}\n\n{preview}\n{tag_help}")

    @filter.regex(r"^(标签|tag)\s*.*")
    async def manage_tags(self, event: AstrMessageEvent):
        message = event.message_str.strip()
        if not message:
            return
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        tags = self._get_user_tags(platform, user_id)
        lower = message.lower()
        if message in ("标签", "标签列表") or lower in ("tag", "tag list", "tags", "tags list"):
            yield event.plain_result(f"当前标签：\n{self._render_tag_list(tags)}")
            return
        if not self._allow_tag_command_edit():
            yield event.plain_result("当前已启用“仅使用配置页面标签”，请在插件配置页修改标签列表。")
            return
        add_match = re.match(r"^(标签新增|tag\s+add)\s+(.+)$", message, re.IGNORECASE)
        if add_match:
            new_tag = self._normalize_tag_name(add_match.group(2))
            if not new_tag:
                yield event.plain_result("标签名不能为空。")
                return
            if new_tag in tags:
                yield event.plain_result(f"标签“{new_tag}”已存在。")
                return
            tags.append(new_tag)
            self._set_user_tags(platform, user_id, tags)
            yield event.plain_result(f"已新增标签：{new_tag}\n\n当前标签：\n{self._render_tag_list(tags)}")
            return
        del_match = re.match(r"^(标签删除|tag\s+del|tag\s+delete)\s+(\d+)$", message, re.IGNORECASE)
        if del_match:
            idx = int(del_match.group(2))
            if idx < 1 or idx > len(tags):
                yield event.plain_result(f"序号越界，可选范围 1-{len(tags)}。")
                return
            removed = tags.pop(idx - 1)
            self._set_user_tags(platform, user_id, tags)
            yield event.plain_result(f"已删除标签：{removed}\n\n当前标签：\n{self._render_tag_list(tags)}")
            return
        rename_match = re.match(r"^(标签改名|tag\s+rename)\s+(\d+)\s+(.+)$", message, re.IGNORECASE)
        if rename_match:
            idx = int(rename_match.group(2))
            if idx < 1 or idx > len(tags):
                yield event.plain_result(f"序号越界，可选范围 1-{len(tags)}。")
                return
            new_name = self._normalize_tag_name(rename_match.group(3))
            if not new_name:
                yield event.plain_result("新标签名不能为空。")
                return
            if new_name in tags and new_name != tags[idx - 1]:
                yield event.plain_result(f"标签“{new_name}”已存在。")
                return
            old_name = tags[idx - 1]
            tags[idx - 1] = new_name
            self._set_user_tags(platform, user_id, tags)
            yield event.plain_result(f"已将标签“{old_name}”改为“{new_name}”。\n\n当前标签：\n{self._render_tag_list(tags)}")
            return
        yield event.plain_result(
            "标签命令支持：\n"
            "- 标签列表 / tag list\n"
            "- 标签新增 名称 / tag add 名称\n"
            "- 标签删除 序号 / tag del 序号\n"
            "- 标签改名 序号 新名称 / tag rename 序号 新名称"
        )

    @filter.regex(r"^(sub|subscribe|订阅提醒|取消提醒|提醒订阅|提醒诊断)\s*.*")
    async def reminder_subscription(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        if not message_str:
            return
        logger.info(f"Reminder command received: {message_str} from {getattr(event, 'unified_msg_origin', '')}")
        lower_text = message_str.lower()
        action = ""
        if message_str.startswith("订阅提醒"):
            action = "on"
        elif message_str.startswith("取消提醒"):
            action = "off"
        elif message_str.startswith("提醒订阅"):
            action = "list"
        elif message_str.startswith("提醒诊断"):
            action = "debug"
        else:
            match = re.match(r"^(sub|subscribe)\s*(.*)$", lower_text, re.IGNORECASE)
            if not match:
                return
            arg = (match.group(2) or "").strip()
            if arg in ("on", "1", "true", "enable", "start", "开启", "开", "订阅"):
                action = "on"
            elif arg in ("off", "0", "false", "disable", "stop", "关闭", "关", "取消"):
                action = "off"
            elif arg in ("debug", "diag", "test", "诊断", "测试"):
                action = "debug"
            else:
                action = "list"
        platform, user_id = self._event_scope(event)
        await self._register_event_user_context(event, platform, user_id)
        if action == "on":
            self._set_user_reminder_subscription(platform, user_id, True)
            yield event.plain_result("已开启提醒订阅。后续将按配置时间自动推送。")
            return
        if action == "off":
            self._set_user_reminder_subscription(platform, user_id, False)
            yield event.plain_result("已关闭提醒订阅。")
            return
        if action == "debug":
            today_str = datetime.now().strftime("%Y-%m-%d")
            user = self.storage.get_user_info(platform, user_id)
            todos = self.storage.load_todos(platform, user_id, today_str)
            pending = [t for t in todos if self._is_unfinished_todo(t)]
            subscribed = self._is_user_reminder_subscribed(user)
            reminder_enable = bool(self.config.get("reminder_enable", False))
            system_scheduler_active = self._is_system_scheduler_active_for_user(user)
            now_hhmm = datetime.now().strftime("%H:%M")
            reminder_start = self._normalize_hhmm(self.config.get("reminder_start", "09:00"), "09:00")
            reminder_end = self._normalize_hhmm(self.config.get("reminder_end", "22:00"), "22:00")
            in_window = reminder_start <= now_hhmm <= reminder_end
            origin = getattr(event, "unified_msg_origin", "") or ""
            send_ok = await self._send_text_to_origin(origin, "TodoPal 提醒链路诊断：主动发送通道可用。")
            summary = (
                f"诊断结果\n"
                f"- reminder_enable: {reminder_enable}\n"
                f"- 订阅状态: {subscribed}\n"
                f"- 系统调度已接管: {system_scheduler_active}\n"
                f"- 当前时间窗口: {in_window} ({now_hhmm} in {reminder_start}-{reminder_end})\n"
                f"- 今日待办总数: {len(todos)}\n"
                f"- 今日待办未完成: {len(pending)}\n"
                f"- origin可用: {bool(origin)}\n"
                f"- 主动发送链路: {'可用' if send_ok else '失败'}\n"
                f"- 最近发送错误: {self._last_send_error or '无'}"
            )
            yield event.plain_result(summary)
            return
        user = self.storage.get_user_info(platform, user_id)
        subscribed = self._is_user_reminder_subscribed(user)
        mode = "订阅制" if self._subscription_required_for_reminder() else "全量推送"
        status = "已订阅" if subscribed else "未订阅"
        yield event.plain_result(f"当前提醒模式：{mode}，你的状态：{status}。")

    @filter.regex(r"^(确认|取消|[0-9xX,\s，]+)$")
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

        if state == 'WAITING_TAG_ASSIGN':
            mode = session.get('action_type', 'append')
            tags = session.get("user_tags") or self._get_user_tags(platform, user_id)
            if action == "确认":
                self._save_todos(platform, user_id, todos, source_text, mode=mode)
                del self.sessions[event.unified_msg_origin]
                yield event.plain_result("已保存待办事项。")
                return
            parsed, error = self._parse_tag_assignment(action, len(todos), len(tags))
            if error:
                yield event.plain_result(error)
                return
            selected_todos, dropped_count = self._apply_tag_assignment(todos, tags, parsed)
            del self.sessions[event.unified_msg_origin]
            if not selected_todos:
                yield event.plain_result("本次待办已全部丢弃。")
                return
            self._save_todos(platform, user_id, selected_todos, source_text, mode=mode)
            kept_count = len(selected_todos)
            dropped_text = f"，丢弃 {dropped_count} 项" if dropped_count > 0 else ""
            preview = self._format_preview(selected_todos, include_confirm_prompt=False)
            yield event.plain_result(f"已保存 {kept_count} 项待办{dropped_text}。\n\n{preview}")
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
                item['tag_id'] = int(item.get('tag_id', 0) or 0)
                item['tag_name'] = str(item.get('tag_name', '') or '')
            
            if mode == 'overwrite':
                self.storage.save_todos(platform, user_id, date, items)
            else:
                self.storage.append_todos(platform, user_id, date, items)

    async def _handle_done_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle marking todos as done using the 'done' prefix.
        Matches: "done 1, 2", "done 买菜"
        """
        result = await self._service_done(platform, user_id, content, "")
        if not result.get("ok"):
            yield event.plain_result(result.get("message", "处理失败，请重试。"))
            return
        if result.get("updated_count", 0) == 0:
            yield event.plain_result(result.get("message", "所选的待办事项已经是完成状态。"))
            return

        today = datetime.now().strftime("%Y-%m-%d")
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        yield event.plain_result(f"{result.get('message', '已更新状态。')}\n\n{preview}")

    async def _handle_undone_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        result = await self._service_undone(platform, user_id, content, "")
        if not result.get("ok"):
            yield event.plain_result(result.get("message", "处理失败，请重试。"))
            return
        if result.get("updated_count", 0) == 0:
            yield event.plain_result(result.get("message", "所选待办目前不是完成状态。"))
            return
        today = datetime.now().strftime("%Y-%m-%d")
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        yield event.plain_result(f"{result.get('message', '已更新状态。')}\n\n{preview}")

    async def _handle_check_command(self, event: AstrMessageEvent, platform: str, user_id: str, query_text: str = "", payload=None):
        view_mode, cleaned_query = self._resolve_check_view_mode(query_text)
        target_date = self._resolve_check_date(cleaned_query, payload)
        service_result = await self._service_check(platform, user_id, target_date)
        todos = []
        for item in service_result.get("items", []):
            todos.append({
                "index": item.get("index", 0),
                "date": item.get("date"),
                "time": item.get("time"),
                "content": item.get("content"),
                "status": item.get("status", "pending"),
                "tag_id": item.get("tag_id", 0),
                "tag_name": item.get("tag_name", "")
            })
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        after_tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        if target_date == today:
            title = "今日待办清单："
            empty_text = "今天还没有待办事项哦。"
        elif target_date == tomorrow:
            title = "明日待办清单："
            empty_text = "明天还没有待办事项哦。"
        elif target_date == after_tomorrow:
            title = "后日待办清单："
            empty_text = "后天还没有待办事项哦。"
        else:
            title = f"{target_date} 待办清单："
            empty_text = f"{target_date} 还没有待办事项哦。"
        if not todos:
            yield event.plain_result(empty_text)
            return
        if view_mode == "raw":
            raw_title = title.replace("待办清单", "原始清单")
            preview = self._format_preview(todos, include_confirm_prompt=False)
            yield event.plain_result(f"{raw_title}\n\n{preview}")
            return
        plan_result = await self._build_plan_result(event, target_date, todos)
        preview = self._format_plan_preview(target_date, plan_result)
        yield event.plain_result(preview)

    async def _handle_fix_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle 'fix' command to modify a specific todo item content.
        Format: fix 3 改成光电数据集会议
        """
        match = re.match(r"^(\d+)\s*(.*)", content)
        if not match:
            yield event.plain_result("格式错误。请使用：fix 序号 新内容\n例如：fix 3 改成光电数据集会议")
            return
            
        idx = int(match.group(1))
        raw_new_content = match.group(2).strip()
        if not raw_new_content:
            yield event.plain_result("请输入新的待办内容。")
            return

        result = await self._service_fix(platform, user_id, idx, raw_new_content, "")
        if not result.get("ok"):
            yield event.plain_result(result.get("message", "修改失败，请重试。"))
            return
        if result.get("ok"):
            today = datetime.now().strftime("%Y-%m-%d")
            fresh_todos = self.storage.load_todos(platform, user_id, today)
            preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
            yield event.plain_result(f"{result.get('message', f'已修改第 {idx} 条待办。')}\n\n{preview}")
        else:
            yield event.plain_result("修改失败，请重试。")

    async def _handle_delete_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        parsed_date, selector_text = self._extract_date_hint_from_text(content)
        selector = str(selector_text or "").strip()
        if not selector:
            yield event.plain_result("请提供要删除的序号或关键词，例如：del 明天 1")
            return
        result = await self._service_delete(platform, user_id, selector, parsed_date)
        if not result.get("ok"):
            yield event.plain_result(result.get("message", "删除失败，请重试。"))
            return
        target_date = result.get("date", datetime.now().strftime("%Y-%m-%d"))
        fresh_todos = self.storage.load_todos(platform, user_id, target_date)
        if not fresh_todos:
            yield event.plain_result(f"{result.get('message', '已删除待办。')}\n\n{target_date} 已没有待办事项。")
            return
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        yield event.plain_result(f"{result.get('message', '已删除待办。')}\n\n{preview}")

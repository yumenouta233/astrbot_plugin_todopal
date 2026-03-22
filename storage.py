import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Union
import uuid

logger = logging.getLogger("astrbot")

class TodoStorage:
    """
    Handles local JSON storage for TodoPal plugin.
    
    Directory structure:
    data/plugin_data/todopal/{platform}/{user_id}/{year}/{month}/{date}.json
    """

    def __init__(self, base_path: str = "data/plugin_data/todopal"):
        """
        Initialize TodoStorage.

        Args:
            base_path: Base directory for storage.
        """
        self.base_path = Path(base_path)
        self.users_file = self.base_path / "users.json"
        self._ensure_users_file()

    def _ensure_users_file(self):
        if not self.users_file.exists():
            self.ensure_directory(self.users_file)
            with open(self.users_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def register_user(self, platform: str, user_id: str, origin: str, provider_id: Optional[str] = None):
        """Register or update a user's unified message origin for proactive messaging."""
        try:
            users = self._load_users_data()
            key = f"{platform}_{user_id}"
            existing = users.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            merged = dict(existing)
            merged["platform"] = platform
            merged["user_id"] = user_id
            merged["origin"] = origin
            merged["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            merged["provider_id"] = provider_id or existing.get("provider_id", "")
            merged["last_rollover_date"] = existing.get("last_rollover_date", "")
            users[key] = merged
            self._save_users_data(users)
        except Exception as e:
            logger.error(f"Failed to register user {user_id}: {e}")

    def get_all_users(self) -> List[Dict]:
        """Get all registered users."""
        try:
            users = self._load_users_data()
            return list(users.values())
        except Exception as e:
            logger.error(f"Failed to read users: {e}")
            return []

    def _load_users_data(self) -> Dict:
        try:
            with open(self.users_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_users_data(self, users: Dict):
        with open(self.users_file, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)

    def get_user_rollover_date(self, platform: str, user_id: str) -> str:
        users = self._load_users_data()
        key = f"{platform}_{user_id}"
        user = users.get(key, {})
        if isinstance(user, dict):
            value = user.get("last_rollover_date", "")
            if isinstance(value, str):
                return value
        return ""

    def set_user_rollover_date(self, platform: str, user_id: str, rollover_date: str):
        users = self._load_users_data()
        key = f"{platform}_{user_id}"
        existing = users.get(key, {})
        if not isinstance(existing, dict):
            existing = {}
        existing["platform"] = existing.get("platform", platform)
        existing["user_id"] = existing.get("user_id", user_id)
        existing["last_rollover_date"] = rollover_date
        existing["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        users[key] = existing
        self._save_users_data(users)

    def get_user_info(self, platform: str, user_id: str) -> Dict:
        users = self._load_users_data()
        key = f"{platform}_{user_id}"
        data = users.get(key, {})
        if isinstance(data, dict):
            return dict(data)
        return {}

    def update_user_info(self, platform: str, user_id: str, fields: Dict):
        users = self._load_users_data()
        key = f"{platform}_{user_id}"
        existing = users.get(key, {})
        if not isinstance(existing, dict):
            existing = {}
        existing["platform"] = existing.get("platform", platform)
        existing["user_id"] = existing.get("user_id", user_id)
        for k, v in (fields or {}).items():
            existing[k] = v
        existing["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        users[key] = existing
        self._save_users_data(users)

    def _get_file_path(self, platform: str, user_id: str, date_str: str) -> Path:
        """
        Construct the file path for a specific user and date.

        Args:
            platform: Platform identifier (e.g., 'qq').
            user_id: User identifier.
            date_str: Date string in 'YYYY-MM-DD' format.

        Returns:
            Path object pointing to the JSON file.
        """
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(date_obj.year)
            month = f"{date_obj.month:02d}"
            
            # Sanitize platform and user_id to be safe for filenames
            safe_platform = "".join(c for c in platform if c.isalnum() or c in ('_', '-'))
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('_', '-'))
            
            return self.base_path / safe_platform / safe_user_id / year / month / f"{date_str}.json"
        except ValueError:
            # Fallback if date parsing fails, though date_str should come from verified source
            return self.base_path / "unknown" / f"{date_str}.json"

    def ensure_directory(self, file_path: Path):
        """
        Ensure the directory for the file exists.

        Args:
            file_path: Path to the file.
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)

    def load_todos(self, platform: str, user_id: str, date_str: str) -> List[Dict]:
        """
        Load todos for a specific date.

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.

        Returns:
            List of todo items (dicts). Returns empty list if file doesn't exist.
        """
        file_path = self._get_file_path(platform, user_id, date_str)
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    fixed = []
                    changed = False
                    id_to_index = {}
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        normalized = dict(item)
                        if normalized.get("date") != date_str:
                            normalized["date"] = date_str
                            changed = True
                        item_id = normalized.get("id")
                        if item_id and item_id in id_to_index:
                            changed = True
                            fixed[id_to_index[item_id]] = normalized
                        else:
                            if item_id:
                                id_to_index[item_id] = len(fixed)
                            fixed.append(normalized)
                    if changed:
                        self.save_todos(platform, user_id, date_str, fixed)
                    return fixed
                logger.warning(f"Data in {file_path} is not a list. Returning empty list.")
                return []
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load todos from {file_path}: {e}")
            return []

    def rollover_pending_todos(self, platform: str, user_id: str, from_date_str: str, to_date_str: str) -> int:
        """
        Move pending todos from one date to another. Returns number of rolled over items.
        """
        from_todos = self.load_todos(platform, user_id, from_date_str)
        if not from_todos:
            return 0

        target_todos = self.load_todos(platform, user_id, to_date_str)
        existing_rollover_sources = {
            t.get("rollover_source_id")
            for t in target_todos
            if isinstance(t, dict) and t.get("rollover_source_id")
        }
        existing_signatures = {
            self._todo_signature(t)
            for t in target_todos
            if isinstance(t, dict) and str(t.get("status", "pending")) in ("pending", "rolled_over", "done")
        }
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        legacy_removed = 0
        pending_items = []
        remain_from_todos = []
        for item in from_todos:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "pending"))
            item_id = str(item.get("id", "")).strip()
            if item_id and item_id in existing_rollover_sources and status in ("pending", "rolled_over"):
                legacy_removed += 1
                continue
            if status == "pending":
                pending_items.append(item)
                continue
            remain_from_todos.append(item)

        if not pending_items and legacy_removed == 0:
            return 0

        carry_items = []
        for item in pending_items:
            source_id = item.get("id", "")
            signature = self._todo_signature(item)
            if signature in existing_signatures:
                continue
            moved = dict(item)
            moved["date"] = to_date_str
            moved["status"] = "rolled_over"
            moved["updated_at"] = now_str
            moved["rollover_source_id"] = source_id or str(moved.get("rollover_source_id", "")).strip()
            moved["rollover_from_date"] = from_date_str
            carry_items.append(moved)
            existing_signatures.add(signature)

        if carry_items:
            self.append_todos(platform, user_id, to_date_str, carry_items)

        self.save_todos(platform, user_id, from_date_str, remain_from_todos)

        return len(carry_items)

    @staticmethod
    def _todo_signature(item: Dict) -> str:
        content = " ".join(str(item.get("content", "")).strip().split()).lower()
        time_text = str(item.get("time", "")).strip()
        tag_name = str(item.get("tag_name", "")).strip()
        tag_id = int(item.get("tag_id", 0) or 0)
        return f"{content}|{time_text}|{tag_name}|{tag_id}"

    def save_todos(self, platform: str, user_id: str, date_str: str, todos: List[Dict]):
        """
        Save todos for a specific date (overwrite existing).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.
            todos: List of todo items to save.
        """
        file_path = self._get_file_path(platform, user_id, date_str)
        self.ensure_directory(file_path)
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(todos, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(todos)} todos to {file_path}")
        except OSError as e:
            logger.error(f"Failed to save todos to {file_path}: {e}")

    def append_todos(self, platform: str, user_id: str, date_str: str, new_todos: List[Dict]) -> List[Dict]:
        """
        Append new todos to existing ones for a specific date.

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.
            new_todos: List of new todo items.

        Returns:
            The updated list of all todos.
        """
        current_todos = self.load_todos(platform, user_id, date_str)
        
        # Simple append. In a real app, you might check for duplicates.
        # Ensure IDs are unique? For simplicity, we assume generated IDs are unique enough
        # or we re-generate IDs if needed. Here we just append.
        
        updated_todos = current_todos + new_todos
        self.save_todos(platform, user_id, date_str, updated_todos)
        return updated_todos

    def update_todo_status(self, platform: str, user_id: str, date_str: str, todo_index: int, status: str) -> Optional[Dict]:
        """
        Update the status of a specific todo item by index (0-based).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string.
            todo_index: Index of the todo item in the list.
            status: New status (e.g., 'done').

        Returns:
            The updated todo item if successful, None otherwise.
        """
        todos = self.load_todos(platform, user_id, date_str)
        if 0 <= todo_index < len(todos):
            todos[todo_index]['status'] = status
            todos[todo_index]['updated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if status == 'done':
                todos[todo_index]['done_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.save_todos(platform, user_id, date_str, todos)
            return todos[todo_index]
        return None

    def update_todo_content(self, platform: str, user_id: str, date_str: str, todo_index: int, new_content: str) -> Optional[Dict]:
        """
        Update the content of a specific todo item by index (0-based).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string.
            todo_index: Index of the todo item in the list.
            new_content: New content string.

        Returns:
            The updated todo item if successful, None otherwise.
        """
        todos = self.load_todos(platform, user_id, date_str)
        if 0 <= todo_index < len(todos):
            todos[todo_index]['content'] = new_content
            todos[todo_index]['updated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Preserve source_text or update it? Maybe append modification note?
            # For simplicity, we keep source_text as original or update it to reflect modification.
            # Let's keep original source_text but maybe add a note if we had a field for it.
            # Here we just update content.
            
            self.save_todos(platform, user_id, date_str, todos)
            return todos[todo_index]
        return None

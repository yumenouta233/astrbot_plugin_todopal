import re
from typing import List, Dict, Optional, Tuple

class TodoMatcher:
    """
    Helper class to match user input against todo items.
    """

    @staticmethod
    def match_todos(todos: List[Dict], query: str) -> List[int]:
        """
        Find multiple todo items based on user query (e.g. after 'done' prefix).
        
        Supported queries:
        1. Comma/space separated indices: "1, 2, 3" or "1 2"
        2. "第X个", "第X条"
        3. Content matching: "买菜"

        Args:
            todos: List of todo items.
            query: User input string.

        Returns:
            List of matched indices (0-based).
        """
        if not todos or not query:
            return []

        matched_indices = set()
        
        # 1. Try to extract numbers (e.g., "1, 2", "1 2 3", "第1个", "第2条")
        # Find all digit sequences that might represent an index
        numbers = re.findall(r'\d+', query)
        for num_str in numbers:
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(todos):
                    matched_indices.add(idx)
            except ValueError:
                pass
                
        if matched_indices:
            return list(matched_indices)

        # 2. Try to match by content if no numbers were found
        clean_query = query.strip()
        if not clean_query:
            return []

        # Simple substring match
        for i, todo in enumerate(todos):
            if clean_query in todo['content']:
                matched_indices.add(i)
                
        return list(matched_indices)

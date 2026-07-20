"""
scratch_regex.py - Auto-generated documentation base.
"""

import re
_FILE_PATTERN = re.compile(
    r"(?:create|write|generate|make)\s+(?:an?\s+)?(?:empty\s+)?(?:[\w\-]+\s+)?(?:file|script)(?:\s+named|\s+called)?\s*['\"]?([\w./\\-]+)['\"]?",
    re.IGNORECASE,
)

tasks = [
    "Create an empty Python file named 'test_loop.py' in the current directory.",
    "create file test.py",
    "write a bash script called 'run.sh'",
    "generate an empty config file named config.json",
]
for t in tasks:
    m = _FILE_PATTERN.search(t)
    if m:
        print(f"Matched: {m.group(1)}")
    else:
        print(f"Failed: {t}")

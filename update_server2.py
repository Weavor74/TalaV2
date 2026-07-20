"""
update_server2.py - Auto-generated documentation base.
"""

import sys

with open("python/harness/server.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update the parser regex
old_parser_line = "tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)>(?:(.*?)</call:\\1>)?', generated_text, re.DOTALL)"
new_parser_line = "tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>)?$)', generated_text, re.DOTALL)"

if old_parser_line in content:
    content = content.replace(old_parser_line, new_parser_line)
else:
    print("Could not find old_parser_line")

# 2. Update the filter regex
old_filter_line = "clean_generated = re.sub(r'<call:([\\w\\.]+)([^>]*?)/?>', '', generated_text).strip()"
new_filter_line = "clean_generated = re.sub(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>)?$)', '', generated_text, flags=re.DOTALL).strip()"

if old_filter_line in content:
    content = content.replace(old_filter_line, new_filter_line)
else:
    print("Could not find old_filter_line")

with open("python/harness/server.py", "w", encoding="utf-8") as f:
    f.write(content)

print("server.py updated to handle unclosed tags!")

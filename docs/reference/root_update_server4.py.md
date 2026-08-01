# File: `update_server4.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import sys

with open("python/harness/server.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Fix the prompt generator
old_prompt = """                dyn_desc += "You also have access to the following dynamic tools. You MUST strictly follow the Usage syntax provided for each tool.\\n\""""
new_prompt = """                dyn_desc += "You also have access to the following dynamic tools. You MUST strictly follow the Usage syntax provided for each tool. DO NOT wrap your tool calls in markdown backticks (```). Output the raw XML tag directly.\\n\""""

if old_prompt in content:
    content = content.replace(old_prompt, new_prompt)
else:
    print("Warning: Could not find old_prompt")

# 2. Fix the regex parser
old_parser = "tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>)?$)', generated_text, re.DOTALL)"
new_parser = "tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>|$))', generated_text, re.DOTALL)"
if old_parser in content:
    content = content.replace(old_parser, new_parser)
else:
    print("Warning: Could not find old_parser")

# 3. Fix the filter regex
old_filter = "clean_generated = re.sub(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>)?$)', '', generated_text, flags=re.DOTALL).strip()"
new_filter = "clean_generated = re.sub(r'<call:([\\w\\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\\1>|$))', '', generated_text, flags=re.DOTALL).strip()"
if old_filter in content:
    content = content.replace(old_filter, new_filter)
else:
    print("Warning: Could not find old_filter")

with open("python/harness/server.py", "w", encoding="utf-8") as f:
    f.write(content)

print("server.py regex and prompt patched!")

```

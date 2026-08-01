# File: `update_server.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import sys

with open("python/harness/server.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update the system prompt instructions
old_prompt = """                dyn_desc = "\\n\\n[Agent Capability: Dynamic Registered Tools (ToolRegistry)]\\n"
                dyn_desc += "You also have access to the following dynamic tools. You MUST invoke them using the exact XML tag format `<call:tool_name param1=\\"value\\" param2=\\"value\\" />`.\\n"
                for idx, t in enumerate(dyn_tools, 1):"""

new_prompt = """                dyn_desc = "\\n\\n[Agent Capability: Dynamic Registered Tools (ToolRegistry)]\\n"
                dyn_desc += "You also have access to the following dynamic tools. You MUST invoke them using the exact XML tag format `<call:tool_name param1=\\"value\\" param2=\\"value\\" />`.\\n"
                dyn_desc += "If a parameter requires a large block of multiline text (like markdown or code), you MUST NOT use an attribute. Instead, place the multiline text inside the tag like this: `<call:tool_name param1=\\"value\\"> [large multiline text here] </call:tool_name>`\\n"
                for idx, t in enumerate(dyn_tools, 1):"""

content = content.replace(old_prompt, new_prompt)

# 2. Update the regex parser
old_parser = """        # Parse tool calls
        import re
        tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)/?>', generated_text)
        if not tool_match:
            all_responses.append(generated_text)
            break

        # Truncate any text generated after the call tag to prevent model hallucinations of responses
        generated_text = generated_text[:tool_match.end()]

        tool_name = tool_match.group(1)
        attr_str = tool_match.group(2)

        # Parse attributes
        attrs = {}
        pattern = r'(\\w+)\\s*=\\s*("[^"]*"|\\\'[^\\\']*\\\'|\\[[^\\]]*\\]|{[^}]*})'
        for match in re.finditer(pattern, attr_str):
            k, v = match.groups()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                attrs[k] = v[1:-1]
            else:
                try:
                    attrs[k] = json.loads(v.replace("'", '"'))
                except Exception:
                    attrs[k] = v"""

new_parser = """        # Parse tool calls
        import re
        tool_match = re.search(r'<call:([\\w\\.]+)([^>]*?)>(?:(.*?)</call:\\1>)?', generated_text, re.DOTALL)
        if not tool_match:
            all_responses.append(generated_text)
            break

        # Truncate any text generated after the call tag to prevent model hallucinations of responses
        generated_text = generated_text[:tool_match.end()]

        tool_name = tool_match.group(1)
        attr_str = tool_match.group(2)
        inner_content = tool_match.group(3)
        if attr_str.endswith('/'):
            attr_str = attr_str[:-1]

        # Parse attributes
        attrs = {}
        pattern = r'(\\w+)\\s*=\\s*("[^"]*"|\\\'[^\\\']*\\\'|\\[[^\\]]*\\]|{[^}]*})'
        for match in re.finditer(pattern, attr_str):
            k, v = match.groups()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                attrs[k] = v[1:-1]
            else:
                try:
                    attrs[k] = json.loads(v.replace("'", '"'))
                except Exception:
                    attrs[k] = v
                    
        if inner_content:
            inner_content = inner_content.strip()
            if tool_name == "generate_detailed_neural_node":
                attrs["detailed_markdown"] = inner_content
            elif tool_name == "write_workspace_file":
                attrs["content"] = inner_content
            elif tool_name == "submit_agent_goal":
                attrs["goal"] = inner_content
            elif "content" not in attrs:
                attrs["content"] = inner_content"""

content = content.replace(old_parser, new_parser)

with open("python/harness/server.py", "w", encoding="utf-8") as f:
    f.write(content)

print("server.py updated!")

```

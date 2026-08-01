# File: `update_server3.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import sys

with open("python/harness/server.py", "r", encoding="utf-8") as f:
    content = f.read()

old_block = """                dyn_desc = "\\n\\n[Agent Capability: Dynamic Registered Tools (ToolRegistry)]\\n"
                dyn_desc += "You also have access to the following dynamic tools. You MUST invoke them using the exact XML tag format `<call:tool_name param1=\\"value\\" param2=\\"value\\" />`.\\n"
                dyn_desc += "If a parameter requires a large block of multiline text (like markdown or code), you MUST NOT use an attribute. Instead, place the multiline text inside the tag like this: `<call:tool_name param1=\\"value\\"> [large multiline text here] </call:tool_name>`\\n"
                for idx, t in enumerate(dyn_tools, 1):
                    schema = t.get("schema", {})
                    props = schema.get("properties", {})
                    reqs = schema.get("required", [])
                    params_str = " ".join([f'{k}="value"' + (" (required)" if k in reqs else " (optional)") for k in props.keys()])
                    dyn_desc += f"{idx}. **{t['name']}**: {t['description']}\\n"
                    dyn_desc += f"   Usage: `<call:{t['name']} {params_str} />`\\n"
                tool_instructions += dyn_desc"""

new_block = """                dyn_desc = "\\n\\n[Agent Capability: Dynamic Registered Tools (ToolRegistry)]\\n"
                dyn_desc += "You also have access to the following dynamic tools. You MUST strictly follow the Usage syntax provided for each tool.\\n"
                for idx, t in enumerate(dyn_tools, 1):
                    schema = t.get("schema", {})
                    props = schema.get("properties", {})
                    reqs = schema.get("required", [])
                    
                    attr_params = []
                    inner_param = None
                    for k in props.keys():
                        if k in ("detailed_markdown", "content", "goal"):
                            inner_param = k
                        else:
                            attr_params.append(f'{k}="value"' + (" (required)" if k in reqs else " (optional)"))
                    
                    params_str = " ".join(attr_params)
                    dyn_desc += f"{idx}. **{t['name']}**: {t['description']}\\n"
                    if inner_param:
                        dyn_desc += f"   Usage: `<call:{t['name']} {params_str}>\\n[your {inner_param} here]\\n</call:{t['name']}>`\\n"
                    else:
                        dyn_desc += f"   Usage: `<call:{t['name']} {params_str} />`\\n"
                tool_instructions += dyn_desc"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open("python/harness/server.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("server.py updated with dynamic usage examples!")
else:
    print("Could not find old_block in server.py")

```

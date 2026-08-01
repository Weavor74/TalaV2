# File: `test_regex6.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import re

generated_text = """``` **Tool Call:** ``` ``` ``` <call:generate_detailed_neural_node node_id="function-get_system_metrics" title="Get System Metrics" node_type="function" execution_authority="0.8"> **Description:** This function collects and provides real-time metrics about the system, including CPU utilization, memory usage, disk space, and network traffic. **Inputs:** * None **Outputs:** * A dictionary containing the following metrics: * cpu_utilization * memory_usage * disk_space * network_traffic **Example Usage:** ``` <call:generate_detailed_neural_node node_id="function-get_system_metrics" title="Get System Metrics" node_type="function" execution_authority="0.8"> **Description:** This function collects and provides real-time metrics about the system, including CPU utilization, memory usage, disk space, and network traffic. **Inputs:** * None **Outputs:** * A dictionary containing the following metrics: * cpu_utilization * memory_usage * disk_space * network_traffic **Example Usage:** ``` </call:generate_detailed_neural_node> ``` </start_of_turn>"""

tool_match = re.search(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', generated_text, re.DOTALL)
if not tool_match:
    print("NO MATCH")
else:
    print("MATCH!")
    tool_name = tool_match.group(1)
    attr_str = tool_match.group(2)
    inner_content = tool_match.group(3)
    if attr_str.endswith('/'):
        attr_str = attr_str[:-1]

    # Parse attributes
    attrs = {}
    pattern = r'(\w+)\s*=\s*("[^"]*"|\'[^\']*\'|\[[^\]]*\]|{[^}]*})'
    for match in re.finditer(pattern, attr_str):
        k, v = match.groups()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            attrs[k] = v[1:-1]
        else:
            attrs[k] = v
            
    if inner_content:
        inner_content = inner_content.strip()
        if tool_name == "generate_detailed_neural_node":
            attrs["detailed_markdown"] = inner_content
            
    print("Attrs:", attrs)

```

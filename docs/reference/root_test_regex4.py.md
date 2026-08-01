# File: `test_regex4.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import re

text = """``` **Tool Call:** ``` ``` ``` <call:generate_detailed_neural_node node_id="function-get_system_metrics" title="Get System Metrics" node_type="function" execution_authority="0.8"> **Description:** This function collects and provides real-time metrics about the system, including CPU utilization, memory usage, disk space, and network traffic. **Inputs:** * None **Outputs:** * A dictionary containing the following metrics: * cpu_utilization * memory_usage * disk_space * network_traffic **Example Usage:** ``` <call:generate_detailed_neural_node node_id="function-get_system_metrics" title="Get System Metrics" node_type="function" execution_authority="0.8"> **Description:** This function collects and provides real-time metrics about the system, including CPU utilization, memory usage, disk space, and network traffic. **Inputs:** * None **Outputs:** * A dictionary containing the following metrics: * cpu_utilization * memory_usage * disk_space * network_traffic **Example Usage:** ``` </call:generate_detailed_neural_node> ``` </start_of_turn>"""

pattern = r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>)?$)'

m = re.search(pattern, text, re.DOTALL)
if m:
    print("MATCH!")
    print("Group 1:", m.group(1))
    print("Group 2:", m.group(2))
    print("Group 3:", m.group(3))
else:
    print("NO MATCH")

```

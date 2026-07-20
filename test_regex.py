"""
test_regex.py - Auto-generated documentation base.
"""

import re
tool_pattern = re.compile(r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))', re.DOTALL)
text = '<call:run_workflow_node action="start" node_id="workflow-node_builder">\n<call:write_to_history>\n'
match = tool_pattern.search(text)
print("Match 1:")
print(match.groups())

text2 = '<call:run_workflow_node action="start" node_id="workflow-node_builder" />\n<call:write_to_history>\n'
match2 = tool_pattern.search(text2)
print("\nMatch 2:")
print(match2.groups())

import re

text = """``` **Tool Call:** ``` ``` ``` <call:generate_detailed_neural_node node_id="test" title="test" node_type="function" execution_authority="0.8">
My Markdown
</call:generate_detailed_neural_node> ``` </start_of_turn>"""

# Using the improved regex
pattern = r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>|$))'

m = re.search(pattern, text, re.DOTALL)
if m:
    print("MATCH!")
    print("Group 1:", m.group(1))
    print("Group 2:", m.group(2))
    print("Group 3:", m.group(3))
else:
    print("NO MATCH")

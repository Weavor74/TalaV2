"""
test_regex3.py - Auto-generated documentation base.
"""

import re

# Case 1: Properly closed
text1 = """<call:tool a="b">
hello
</call:tool>"""

# Case 2: Unclosed (LLM forgot)
text2 = """<call:tool a="b">
hello
world"""

# Case 3: Self-closing
text3 = """<call:tool a="b" />"""

pattern = r'<call:([\w\.]+)([^>]*?)(?:/>|>(.*?)(?:</call:\1>)?$)'

for i, text in enumerate([text1, text2, text3]):
    m = re.search(pattern, text, re.DOTALL)
    print(f"--- Case {i+1} ---")
    if m:
        print("Match:", m.group(0))
        print("Name:", m.group(1))
        print("Attrs:", m.group(2))
        print("Inner:", m.group(3))
    else:
        print("NO MATCH")

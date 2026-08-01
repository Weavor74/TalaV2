# File: `test_regex2.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import re
pattern = r'(\w+)\s*=\s*("[^"]*"|\'[^\']*\'|\[[^\]]*\]|{[^}]*})'
attr_str = ' action="start" node_id="workflow-node_builder"'
attrs = {}
for match in re.finditer(pattern, attr_str):
    k, v = match.groups()
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        attrs[k] = v[1:-1]
    else:
        attrs[k] = v
print(attrs)

```

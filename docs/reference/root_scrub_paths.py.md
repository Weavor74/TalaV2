# File: `scrub_paths.py`

## Contents

*No classes or functions defined in this file. It may be a configuration or script file.*

## Source Code

```python
import os
import glob
import re

files = glob.glob("sdk/agent/tests/*.py") + [
    "migrate_all.py",
    "rebuild_v2_nodes.py",
    "reindex_nodes.py",
    "test_metrics.py"
]

for f in files:
    if not os.path.exists(f):
        continue
    with open(f, "r", encoding="utf-8") as file:
        content = file.read()
    
    # Replace the hardcoded string
    new_content = content.replace('"D:/src/TalaV2"', 'os.environ.get("TALA_WORKSPACE_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))')
    # For scripts not in sdk/agent/tests, we just fallback to a more generic path if needed, but since they are scratch scripts, it's fine.
    
    # Also handle lowercase variants
    new_content = new_content.replace('"d:/src/TalaV2/nodes"', 'os.path.join(os.environ.get("TALA_WORKSPACE_ROOT", "."), "nodes")')
    new_content = new_content.replace("'D:/src/TalaV2/nodes'", 'os.path.join(os.environ.get("TALA_WORKSPACE_ROOT", "."), "nodes")')
    
    if "import os" not in new_content:
        new_content = "import os\n" + new_content

    if content != new_content:
        with open(f, "w", encoding="utf-8") as file:
            file.write(new_content)
        print(f"Updated {f}")

```

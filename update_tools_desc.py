import sys

with open("sdk/agent/tools.py", "r", encoding="utf-8") as f:
    content = f.read()

old_desc = 'description="Generate a highly detailed HTML Neural Node from a structured markdown document. Use this for deep documentation (functions, personas, UI, APIs). It automatically handles the NN math and blockchain anchoring.",'

new_desc = '''description="""Generate a highly detailed HTML Neural Node from a structured markdown document. It automatically handles the NN math and blockchain anchoring.

CRITICAL: When generating Function Documentation, you MUST format your detailed_markdown EXACTLY using this layout:

# Function Doc: `[Function Name]`

## 1. Purpose
`[Function Name]` does this:
[Short description]

Use it when:
[When this function should be used]

Do not use it when:
[When this function should not be used]

## 2. Basic Info
| Field | Value |
|---|---|
| Function | `[Function Name]` |
| Type | `[Utility / Data / API / UI / Validation / Other]` |
| Input | `[Main input type]` |
| Output | `[Main output type]` |
| Changes State? | `[Yes / No]` |
| Can Fail? | `[Yes / No]` |

## 3. Inputs
| Name | Required | Type | Meaning |
|---|---|---|---|
| `[inputName]` | Yes | `[type]` | `[what it means]` |

## 4. Output
The function returns:
[Describe the output]
Example output:
[example output]

## 5. Behavior
1. Receive input.
2. Check that input is valid.
3. Do the main task.
4. Return the result.
5. Handle errors safely.

## 6. Errors
| Problem | What Should Happen |
|---|---|
| Missing input | [return error / return null / throw error] |

## 7. Side Effects
[Does not change anything outside itself] OR [Writes file / updates state / etc]

## 8. Safe Use Rules
- Validate input before use.
- Do not log secrets.
- Do not expose private data.
- Sanitize file paths, commands, queries, and HTML.
- Confirm permission before write, delete, send, or execute actions.

## 9. Example
Input:
[example]
Steps:
[what it does]
Output:
[example]

## 10. Test Checklist
- [ ] Works with valid input.
- [ ] Handles missing input.
- [ ] Handles bad input.
- [ ] Returns expected output.
- [ ] Handles errors safely.
- [ ] Does not create unwanted side effects.

## 11. Notes
[Any limits or assumptions]
""","""

if old_desc in content:
    content = content.replace(old_desc, new_desc)
    with open("sdk/agent/tools.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("tools.py updated successfully!")
else:
    print("Could not find old_desc in tools.py")

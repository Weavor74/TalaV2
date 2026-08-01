# File: `bump_version.py`

## Functions

### `def bump_version()`
*No docstring provided.*

## Source Code

```python
import os
import json
import re

def bump_version():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_json_path = os.path.join(root_dir, "package.json")
    pyproject_toml_path = os.path.join(root_dir, "pyproject.toml")
    public_dir = os.path.join(root_dir, "blockchain", "public")
    version_json_path = os.path.join(public_dir, "version.json")

    # Read current version from package.json
    with open(package_json_path, 'r', encoding='utf-8') as f:
        pkg_data = json.load(f)
    
    current_version = pkg_data.get('version', '0.1.0')
    parts = current_version.split('.')
    if len(parts) != 3:
        parts = ['0', '1', '0']
    
    # Bump patch version
    parts[2] = str(int(parts[2]) + 1)
    new_version = '.'.join(parts)
    print(f"Bumping version from {current_version} to {new_version}")

    # Update package.json
    pkg_data['version'] = new_version
    with open(package_json_path, 'w', encoding='utf-8') as f:
        json.dump(pkg_data, f, indent=2)

    # Update pyproject.toml
    if os.path.exists(pyproject_toml_path):
        with open(pyproject_toml_path, 'r', encoding='utf-8') as f:
            toml_content = f.read()
        
        toml_content = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{new_version}"', toml_content)
        
        with open(pyproject_toml_path, 'w', encoding='utf-8') as f:
            f.write(toml_content)

    # Write version.json
    os.makedirs(public_dir, exist_ok=True)
    version_data = {
        "version": new_version,
        "release_notes": "Minor updates and bug fixes.",
        "downloads": {
            "windows": "https://talacore.app/tala-windows.zip",
            "macos": "https://talacore.app/tala-macos.zip",
            "linux": "https://talacore.app/tala-linux.zip"
        }
    }
    with open(version_json_path, 'w', encoding='utf-8') as f:
        json.dump(version_data, f, indent=2)

    print(f"Version bumped to {new_version} and version.json generated.")

if __name__ == "__main__":
    bump_version()

```

# File: `build_bundles.py`

## Functions

### `def build_bundles()`
*No docstring provided.*

## Source Code

```python
import os
import zipfile
import fnmatch
import subprocess
import sys

def build_bundles():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # Bump version and generate version.json
    bump_script = os.path.join(root_dir, "scripts", "bump_version.py")
    subprocess.run([sys.executable, bump_script], check=True)
    
    public_dir = os.path.join(root_dir, "blockchain", "public")
    os.makedirs(public_dir, exist_ok=True)

    base_excludes = [
        ".git/*",
        ".venv/*",
        "node_modules/*",
        "data/*",
        "vault/*",
        ".env",
        "__pycache__/*",
        "*.pyc",
        "blockchain/public/*.zip",
        "*.log",
        "dist/*",
        ".tala_snapshots/*",
        "scratch/*"
    ]

    bundles = {
        "tala-windows.zip": {
            "exclude": [
                "install-macos.sh",
                "install-linux.sh",
                "scripts/start-tala.sh"
            ]
        },
        "tala-macos.zip": {
            "exclude": [
                "install-windows.ps1",
                "install-linux.sh",
                "scripts/start-tala.ps1"
            ]
        },
        "tala-linux.zip": {
            "exclude": [
                "install-windows.ps1",
                "install-macos.sh",
                "scripts/start-tala.ps1"
            ]
        }
    }

    for bundle_name, config in bundles.items():
        zip_path = os.path.join(public_dir, bundle_name)
        print(f"Building {bundle_name}...")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Apply dir exclusions
                rel_dir = os.path.relpath(dirpath, root_dir)
                if rel_dir == ".":
                    rel_dir = ""
                
                skip_dir = False
                for ex in base_excludes:
                    if ex.endswith("/*"):
                        dir_ex = ex[:-2]
                        if rel_dir == dir_ex or rel_dir.startswith(dir_ex + os.sep) or rel_dir.startswith(dir_ex + "/"):
                            skip_dir = True
                            break
                if skip_dir:
                    continue

                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(file_path, root_dir)
                    
                    # Normalization for matching
                    rel_path_fwd = rel_path.replace(os.sep, '/')
                    
                    # Global Excludes
                    skip_file = False
                    for ex in base_excludes:
                        if fnmatch.fnmatch(rel_path_fwd, ex):
                            skip_file = True
                            break
                    if skip_file:
                        continue
                        
                    # Bundle-specific Excludes
                    for ex in config["exclude"]:
                        if rel_path_fwd == ex:
                            skip_file = True
                            break
                    if skip_file:
                        continue

                    # Write file
                    zf.write(file_path, rel_path)
                    
        print(f"Created {zip_path}")

if __name__ == "__main__":
    build_bundles()

```

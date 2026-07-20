"""
test_sub_exec.py - Auto-generated documentation base.
"""

import asyncio
import sys

async def test_exec():
    print(f"Executing: {sys.executable} d:\\src\\TalaV2\\test_loop.py")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "d:\\src\\TalaV2\\test_loop.py",
        cwd="d:\\src\\TalaV2",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    print("STDOUT:", stdout.decode())
    print("STDERR:", stderr.decode())
    print("RETURN CODE:", proc.returncode)

asyncio.run(test_exec())

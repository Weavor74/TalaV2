import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sdk.agent.parser import parse_file
from sdk.agent.exceptions import InvalidNodeSchemaError

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/validate_node.py <path_to_node.html>")
        sys.exit(1)

    file_path = sys.argv[1]
    path = Path(file_path)

    if not path.exists():
        print(f"Error: File '{file_path}' does not exist.")
        sys.exit(1)

    try:
        node = parse_file(str(path))
        print("==================================================")
        print("        NEURAL NODE SCHEMA VALIDATION PASSED       ")
        print("==================================================")
        print(f"File:      {path.name}")
        print(f"Node ID:   {node.node_id}")
        print(f"Node Type: {node.node_type}")
        print(f"Execution Authority: {node.execution_authority}")
        print(f"Memory Authority:    {node.memory_authority}")
        print(f"Relevance Weight:    {node.weight_relevance}")
        print(f"State Keys:          {list(node.state_data.keys()) if node.state_data else 'None'}")
        print("==================================================")
        sys.exit(0)
    except InvalidNodeSchemaError as exc:
        print("==================================================")
        print("        NEURAL NODE SCHEMA VALIDATION FAILED       ")
        print("==================================================")
        print(f"File:      {path.name}")
        print(f"Error:     {exc.message}")
        if exc.missing_attributes:
            print(f"Missing attributes: {exc.missing_attributes}")
        if exc.offending_html:
            print("\nOffending HTML snippet:")
            print(exc.offending_html[:500])
        print("==================================================")
        sys.exit(2)
    except Exception as exc:
        print(f"Unexpected Error parsing file: {exc}")
        sys.exit(3)

if __name__ == "__main__":
    main()

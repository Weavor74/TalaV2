# File: `verify.py`

## Functions

### `def main()`
*No docstring provided.*

## Source Code

```python
import sys
from python.ledger.chain import LedgerChain

def main():
    chain = LedgerChain()
    print("Verifying Local Ledger Chain...")
    result = chain.verify_chain()
    
    if result["valid"]:
        print(f"SUCCESS: {result['message']}")
        sys.exit(0)
    else:
        print(f"ERROR: {result['message']}")
        sys.exit(1)

if __name__ == "__main__":
    main()

```

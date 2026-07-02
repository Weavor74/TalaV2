# Documentation Rules

## Man Page Format for Documentation
All function, class, and script documentation (docstrings and standalone docs) in this project must be formatted like a UNIX man page.

**CRITICAL DISTINCTION:**
- **Python Docstrings:** Use plain text Man Page formatting (as shown in the example below).
- **Standalone Documentation Pages:** Any generated documentation pages MUST be strictly valid **HTML Neural Nodes (HTML-NN)**. They should visually display the Man Page sections (`NAME`, `SYNOPSIS`, `DESCRIPTION`, etc.) using appropriate HTML tags, but they must fundamentally adhere to the Tala HTML-NN v2 schema (`<article>`, semantic tags, metadata).

When writing or updating documentation, use the following structure and headers:

- **NAME**: The name of the function/script and a very brief description.
- **SYNOPSIS**: The signature, usage, or command-line arguments.
- **DESCRIPTION**: Detailed explanation of what the function/script does. Explain arguments and options here.
- **AUTHOR**: (Optional) Who wrote it.
- **REPORTING BUGS**: (Optional) Where to report issues.
- **COPYRIGHT**: (Optional) License information.
- **SEE ALSO**: (Optional) Related functions, scripts, or documentation.
- **COLOPHON**: (Optional) Project meta-information.

### Example for a Python Function Docstring
```python
def example_function(arg1: str, arg2: int) -> bool:
    """
    NAME
           example_function - does a thing with strings and ints
           
    SYNOPSIS
           example_function(arg1: str, arg2: int) -> bool
           
    DESCRIPTION
           Detailed description of the function.
           
           - arg1: The string argument
           - arg2: The integer argument
           
    SEE ALSO
           other_function(1)
    """
    pass
```

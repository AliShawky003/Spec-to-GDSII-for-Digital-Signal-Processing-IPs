"""
Code cleaning utilities for removing markdown artifacts from LLM outputs
"""
import re

def clean_verilog_code(raw_code: str) -> str:
    """
    Remove markdown code blocks from Verilog/SystemVerilog code.
    Ensures file ends with newline (POSIX requirement for Verilator).
    Removes non-ASCII characters that cause encoding issues.

    Args:
        raw_code: Raw code string potentially containing markdown

    Returns:
        Cleaned code string with trailing newline
    """
    # Remove markdown code block markers
    cleaned = re.sub(r'```systemverilog|```verilog|```', '', raw_code)
    
    # Remove or replace non-ASCII Unicode characters that cause encoding issues
    # Keep only ASCII characters (0-127) to avoid 'charmap' codec errors on Windows
    cleaned = ''.join(char if ord(char) < 128 else ' ' for char in cleaned)
    
    cleaned = cleaned.strip()

    # Ensure file ends with newline (POSIX requirement)
    if not cleaned.endswith('\n'):
        cleaned += '\n'

    return cleaned


def clean_python_code(raw_code: str) -> str:
    """
    Remove markdown code blocks from Python code.

    Args:
        raw_code: Raw code string potentially containing markdown

    Returns:
        Cleaned code string
    """
    # Remove markdown code block markers
    cleaned = re.sub(r'```python|```', '', raw_code)
    return cleaned.strip()

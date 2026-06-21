"""
Linter Agent - Runs Verilator syntax checking on RTL
"""
import subprocess
from termcolor import colored
from config.settings import VERILATOR_LINT_FLAGS
from models.state import AgentState

def lint_node(state: AgentState):
    """
    Runs Verilator lint check on the generated RTL.
    Distinguishes between fatal errors and warnings.
    """
    # If max attempts already reached, don't lint - just pass through
    if state.get("status") == "max_attempts_reached":
        return {"status": "max_attempts_reached"}

    rtl_code = state["verilog_code"]
    specs = state["specs"]
    output_file = specs["project_settings"]["output_file"]

    print(colored("\n[Linter] Running Verilator syntax check...", "magenta"))

    try:
        result = subprocess.run(
            ["wsl", "verilator"] + VERILATOR_LINT_FLAGS + [output_file],
            capture_output=True,
            text=True,
            timeout=30
        )

        stderr_output = result.stderr

        # Check for tool availability issues
        if "command not found" in stderr_output:
            print(colored("[CRITICAL] Verilator not found in WSL.", "red"))
            return {"error_report": "Tool missing", "status": "failed"}

        # Check if there are actual errors (not just warnings)
        has_errors = "Error" in stderr_output
        has_warnings = "%Warning" in stderr_output

        # Pass if: clean exit OR only warnings (no fatal %Error marker)
        if result.returncode == 0 or not has_errors:
            # Either passed cleanly OR only has warnings (no %Error)
            if has_warnings or (result.returncode != 0 and stderr_output):
                print(colored("[Linter] Warnings/non-fatal issues found:", "yellow"))
                print(stderr_output)
                print(colored("[Linter] Proceeding despite warnings...", "cyan"))
            else:
                print("[Linter] Syntax Check Passed!")
            return {"error_report": "", "status": "rtl_verified"}
        else:
            # Has actual %Error markers - needs fixing
            print(colored("[Linter] Fatal Errors Found.", "red"))
            print(stderr_output)
            return {"error_report": stderr_output, "status": "processing"}

    except FileNotFoundError:
        return {"error_report": "Verilator not found", "status": "failed"}
    except subprocess.TimeoutExpired:
        return {"error_report": "Verilator timeout", "status": "failed"}
    except Exception as e:
        print(colored(f"[Linter] Unexpected error: {e}", "red"))
        return {"error_report": str(e), "status": "failed"}

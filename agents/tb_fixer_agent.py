"""
TB Fixer Agent - Fixes errors in testbench code
"""
import os
import re
from termcolor import colored
from config.settings import MODEL_NAME, API_KEY, MAX_TB_FIX_ATTEMPTS, USE_COMPACT_PROMPTS
from models.state import AgentState
from utils.code_cleaners import clean_python_code, clean_verilog_code
from utils.api_utils import call_llm_with_retry
from utils.error_retriever import retrieve_errors, format_error_context, describe_matches


from config.prompts_compact import (
        TB_FIXER_SYSTEM_PROMPT,
        TB_FIXER_USER_PROMPT,
        SV_TB_FIXER_SYSTEM_PROMPT,
        SV_TB_FIXER_USER_PROMPT,
        IVERILOG_CONSTRAINTS
    )


def resolve_tb_method_from_plan(plan: str) -> str:
    if not plan:
        return ""
    match = re.search(r'\bTB_LANGUAGE\s*=\s*([A-Za-z0-9_]+)', plan, re.IGNORECASE)
    if match:
        value = match.group(1)
        normalized = value.strip().lower()
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "python"
    match = re.search(r'\bVERIFICATION_METHOD\s*=\s*([A-Za-z0-9_]+)', plan, re.IGNORECASE)
    if match:
        value = match.group(1)
        normalized = value.strip().lower()
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "python"
    return ""


def resolve_tb_method(verif_specs: dict, plan: str = "") -> str:
    plan_method = resolve_tb_method_from_plan(plan)
    if plan_method:
        return plan_method

    method = verif_specs.get("testbench_settings", {}).get("method", "")
    sim_tool = verif_specs.get("verification_framework", {}).get("simulator", "")

    for value in (method, sim_tool):
        if not value:
            continue
        normalized = str(value).strip().lower()
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "python"

    return "systemverilog"

def resolve_rtl_module_name(specs: dict) -> str:
    rtl_file = specs.get("project_settings", {}).get("output_file", "")
    module_name = None
    if rtl_file:
        try:
            with open(rtl_file, 'r') as f:
                rtl_content = f.read()
            match = re.search(r'^\s*module\s+(\w+)', rtl_content, re.MULTILINE)
            if match:
                module_name = match.group(1)
        except Exception:
            module_name = None

    if not module_name:
        module_name = specs.get("module_definition", {}).get("module_name")
    if not module_name and rtl_file:
        module_name = os.path.splitext(os.path.basename(rtl_file))[0]
    return module_name or ""

def strip_duplicate_dut_module(tb_code: str, dut_module: str) -> str:
    if not tb_code or not dut_module:
        return tb_code
    modules = re.findall(r'^\s*module\s+(\w+)', tb_code, re.MULTILINE)
    if dut_module in modules and len(modules) > 1:
        pattern = re.compile(r'(?s)\bmodule\s+' + re.escape(dut_module) + r'\b.*?\bendmodule\b')
        tb_code = re.sub(pattern, '', tb_code).strip()
        if not tb_code.endswith('\n'):
            tb_code += '\n'
    return tb_code

def fix_signed_real_usage(tb_code: str) -> str:
    if not tb_code:
        return tb_code
    lines = []
    real_hint = re.compile(r"\d+\.\d|\$sin|\$cos|\$itor|\breal\b")
    for line in tb_code.splitlines():
        if "$signed(" in line and real_hint.search(line):
            line = line.replace("$signed(", "$rtoi(")
        lines.append(line)
    return "\n".join(lines) + ("\n" if tb_code.endswith("\n") else "")

def fix_sv_signed_conversions(tb_code: str) -> str:
    if not tb_code:
        return tb_code
    vector_names = ["rtl_outputs", "result_out", "input_samples", "input_vectors", "sample_in"]
    for name in vector_names:
        tb_code = re.sub(
            rf"\$itor\(\$rtoi\(\s*{name}\b",
            rf"$itor($signed({name}",
            tb_code
        )
        tb_code = re.sub(
            rf"\$rtoi\(\s*{name}\b",
            rf"$signed({name}",
            tb_code
        )
    return tb_code

def fix_sv_latency_compare(tb_code: str) -> str:
    if not tb_code:
        return tb_code
    lines = []
    for line in tb_code.splitlines():
        if re.search(r"\bif\s*\(\s*i\s*<\s*output_index\s*\)", line):
            line = re.sub(
                r"if\s*\(\s*i\s*<\s*output_index\s*\)",
                "if (i < output_index && i >= LATENCY)",
                line
            )
        if re.search(r"\bgolden_(output|real)\s*=\s*golden_outputs\s*\[\s*i\s*\]", line):
            line = re.sub(
                r"golden_outputs\s*\[\s*i\s*\]",
                "golden_outputs[i - LATENCY]",
                line
            )
        if "$display" in line and "Error=" in line and "error_percent" in line:
            line = line.replace("error_percent", "abs_error")
        lines.append(line)
    return "\n".join(lines) + ("\n" if tb_code.endswith("\n") else "")

def fix_sv_timeout_block(tb_code: str, latency: int) -> str:
    if not tb_code:
        return tb_code
    if "timeout_counter" not in tb_code or "max_timeout" not in tb_code:
        return tb_code

    flush_block = (
        "    // Flush remaining outputs for latency cycles\n"
        f"    for (i = 0; i < input_count + {latency}; i = i + 1) begin\n"
        "      @(posedge clk);\n"
        "      if (result_valid && output_index < input_count) begin\n"
        "        captured_outputs[output_index] = result_out;\n"
        "        output_index = output_index + 1;\n"
        "      end\n"
        "    end\n"
        "\n"
        "    if (output_index < input_count) begin\n"
        "      $display(\"FAIL: Missing outputs (got %0d of %0d)\", output_index, input_count);\n"
        "      $finish;\n"
        "    end\n"
    )

    pattern = re.compile(
        r"\s*// Wait for remaining outputs.*?^\s*if\s*\(timeout_counter[^)]*\)\s*begin.*?^\s*end\s*$",
        re.MULTILINE | re.DOTALL
    )
    updated = re.sub(pattern, "\n" + flush_block, tb_code, count=1)
    return updated if updated else tb_code

def extract_latency_from_plan(plan: str) -> int:
    match = re.search(r'LATENCY\s*[:=]?\s*(\d+)(?:\s*CYCLES?)?', plan, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r'Pipeline depth\s*[:=]?\s*(\d+)', plan, re.IGNORECASE)
    if match:
        return int(match.group(1))
    matches = re.findall(r'latency\D*(\d+)', plan, re.IGNORECASE)
    for m in matches:
        val = int(m)
        if val < 50:
            return val
    return 1

def tb_fixer_node(state: AgentState):
    """
    Attempts to fix testbench errors with minimal changes.
    If fix attempts exhausted, signals to regenerate TB.
    """
    current_tb = state.get("tb_code", "")
    error_report = state.get("error_report", "")
    sim_report = state.get("sim_report", "")
    fix_count = state.get("tb_fix_count", 0)
    verif_specs = state.get("verif_specs", {})
    specs = state.get("specs", {})
    plan = state.get("verif_plan") or state.get("design_plan", "")
    tb_method = resolve_tb_method(verif_specs, plan)
    is_systemverilog = tb_method == "systemverilog"

    # Check if max fix attempts reached
    if fix_count >= MAX_TB_FIX_ATTEMPTS:
        print(colored(f"\n[TB Fixer] Max fix attempts ({MAX_TB_FIX_ATTEMPTS}) reached. Regenerating TB...", "yellow"))
        return {
            "tb_fix_count": 0,  # Reset fix counter
            "status": "tb_needs_regen"
        }

    print(colored(f"\n[TB Fixer] Attempting to fix testbench errors... (Fix attempt {fix_count + 1}/{MAX_TB_FIX_ATTEMPTS})", "cyan"))

    # Combine error sources (could be from TB generation or simulation)
    error_feedback = error_report if error_report else sim_report
    diagnostic = ""

    if not is_systemverilog:
        diagnostic = "\n\n=== DIAGNOSTIC ANALYSIS ===\n"

        # Look for Python errors in the traceback
        python_errors = {
            "AttributeError": "SIGNAL NAME MISMATCH - Check DUT signal names match RTL interface",
            "AssertionError": "ASSERTION FAILED - Check comparison logic and expected vs actual values",
            "IndexError": "QUEUE UNDERFLOW - Not enough expected values in queue for RTL outputs",
            "TypeError": "TYPE MISMATCH - Check signal value conversion (int, str, LogicArray)",
            "KeyError": "SPECIFICATION LOOKUP FAILED - Check JSON specs for required fields",
            "ValueError": "INVALID VALUE - Check parameter ranges and conversions",
        }

        for error_type, guidance in python_errors.items():
            if error_type in error_feedback:
                diagnostic += f"PYTHON ERROR DETECTED: {error_type}\n"
                diagnostic += f"GUIDANCE: {guidance}\n"
                diagnostic += "ACTION: Read the full Python traceback above and fix the specific issue.\n"
                break

        # Check for latency issues
        if "latency" in error_feedback.lower() or "cycle" in error_feedback.lower():
            diagnostic += "ISSUE: Latency/pipeline synchronization problem\n"
            diagnostic += "FIX: The RTL has a fixed pipeline latency. The testbench must:\n"
            diagnostic += "  1. Track expected outputs in a deque with fixed delay\n"
            diagnostic += "  2. Only compare outputs after (LATENCY + 1) cycles from stimulus\n"
            diagnostic += "  3. Properly handle ready/valid handshaking with delays\n"

        # Check for scaling/fixed-point issues
        if "relative error" in error_feedback.lower() or "mismatch" in error_feedback.lower():
            mismatch_match = re.search(r"Golden=([\d\.\-e]+).*RTL=([\d\.\-e]+)", error_feedback)
            if mismatch_match:
                golden, rtl = float(mismatch_match.group(1)), float(mismatch_match.group(2))
                ratio = rtl / golden if golden != 0 else 0
                diagnostic += f"ISSUE: Numerical mismatch (Golden={golden:.6f}, RTL={rtl:.6f}, ratio={ratio:.6f})\n"
                if abs(ratio - 2.0) < 0.1:
                    diagnostic += "FIX: RTL output is ~2x golden. Check fixed-point scaling (missing >>1?)\n"
                elif abs(ratio - 0.5) < 0.1:
                    diagnostic += "FIX: RTL output is ~0.5x golden. Check if output needs left-shift\n"
                else:
                    diagnostic += "FIX: Check OUTPUT_SCALE_FACTOR calculation and fixed-point bit positioning\n"

        # Check for queue issues
        if "queue" in error_feedback.lower() and "empty" in error_feedback.lower():
            diagnostic += "ISSUE: Testbench queue underflow - RTL producing outputs faster than stimulus\n"
            diagnostic += "FIX: Likely a latency mismatch. Ensure checker waits for pipeline latency.\n"

        # Check for uninitialized RTL
        if "0" in error_feedback and "found" in error_feedback.lower():
            comparison_match = re.search(r"Found (\d+) mismatches.*Performed (\d+) comparisons", error_feedback)
            if comparison_match:
                mismatches = int(comparison_match.group(1))
                comparisons = int(comparison_match.group(2))
                failure_rate = (mismatches / comparisons * 100) if comparisons > 0 else 100
                if failure_rate > 70:
                    diagnostic += f"ISSUE: {failure_rate:.1f}% failures - likely RTL not computing correctly\n"
                    diagnostic += "FIX: This appears to be an RTL issue, not a testbench issue. Consider RTL regeneration.\n"

    # Error-memory retrieval for the TB fixer. Source is 'linter' for compile
    # errors (the common case here) and 'simulator' when we were routed from
    # a sim failure.
    topology     = ""
    filter_class = "any"
    topo_match = re.search(r'^TOPOLOGY=(.+)$', plan, re.MULTILINE) if plan else None
    if topo_match:
        topology = topo_match.group(1).strip()
    fc_match = re.search(r'^FILTER_CLASS=(.+)$', plan, re.MULTILINE) if plan else None
    if fc_match:
        filter_class = fc_match.group(1).strip()

    retrieval_source = "simulator" if state.get("status") == "sim_failed" else "linter"
    past_errors = retrieve_errors(
        raw_error=error_feedback,
        source=retrieval_source,
        topology=topology,
        filter_class=filter_class,
        top_k=2,
    )
    if past_errors:
        print(colored(
            f"[TB Fixer] Error memory hits: {describe_matches(past_errors)}",
            "cyan"
        ))
    else:
        print(colored("[TB Fixer] Error memory: no past matches", "yellow"))
    past_context = format_error_context(past_errors)

    if is_systemverilog:
        user_content = SV_TB_FIXER_USER_PROMPT.format(
            error_feedback=error_feedback,
            current_tb=current_tb,
            iverilog_constraints=IVERILOG_CONSTRAINTS
        )
        system_prompt = SV_TB_FIXER_SYSTEM_PROMPT
    else:
        user_content = TB_FIXER_USER_PROMPT.format(
            error_feedback=error_feedback + diagnostic,
            current_tb=current_tb,
        )
        system_prompt = TB_FIXER_SYSTEM_PROMPT

    if past_context:
        user_content = past_context + "\n" + user_content

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    try:
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        raw_code = response.choices[0].message.content

        # Write fixed code to file
        tb_filename = verif_specs.get("testbench_settings", {}).get("filename", "test_filter")

        # Use correct extension based on method
        if tb_method == "systemverilog":
            output_file = f"{tb_filename}.sv" if not tb_filename.endswith(('.sv', '.v')) else tb_filename
            fixed_code = clean_verilog_code(raw_code)
            fixed_code = fix_signed_real_usage(fixed_code)
            fixed_code = fix_sv_signed_conversions(fixed_code)
            fixed_code = fix_sv_latency_compare(fixed_code)
            dut_module = resolve_rtl_module_name(specs)
            fixed_code = strip_duplicate_dut_module(fixed_code, dut_module)
            latency = extract_latency_from_plan(plan)
            fixed_code = fix_sv_timeout_block(fixed_code, latency)
        else:
            output_file = f"{tb_filename}.py" if not tb_filename.endswith('.py') else tb_filename
            fixed_code = clean_python_code(raw_code)
        
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w') as f:
            f.write(fixed_code)
        print(colored(f"[TB Fixer] Fixed testbench saved to {output_file}", "green"))

        return {
            "tb_code": fixed_code,
            "tb_fix_count": fix_count + 1,
            "status": "tb_fixed",
            # Snapshot for the harvest hook — if the next sim succeeds,
            # the workflow pairs these with the new tb_code.
            "pre_fix_tb_code":  current_tb,
            "pre_fix_tb_error": error_feedback,
        }

    except Exception as e:
        print(colored(f"[TB Fixer] Error: {e}", "red"))
        return {
            "tb_fix_count": fix_count + 1,
            "status": "tb_fix_failed"
        }

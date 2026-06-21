"""
Testbench Generator Agent - Generates both cocotb and SystemVerilog testbenches
"""
import os
import re
import string
from termcolor import colored
from config.settings import MODEL_NAME, API_KEY, MAX_TB_ATTEMPTS, USE_COMPACT_PROMPTS
from models.state import AgentState
from utils.code_cleaners import clean_python_code, clean_verilog_code
from utils.api_utils import call_llm_with_retry


from config.prompts_compact import (
        COCOTB_SYSTEM_PROMPT,
        COCOTB_USER_PROMPT,
        COCOTB_REQUIREMENTS,
        COCOTB_ERROR_FEEDBACK,
        SV_TB_SYSTEM_PROMPT,
        FIR_SV_TB_USER_PROMPT,
        IIR_SV_TB_USER_PROMPT,
        IVERILOG_CONSTRAINTS
    )


def truncate_error_log(error_text: str, max_chars=2000) -> str:
    """Truncates massive error logs to prevent Context Window limits"""
    if not error_text: return ""
    if len(error_text) <= max_chars: return error_text
    return f"... [TRUNCATED] ...\n{error_text[-max_chars:]}"

class _SafeKey(str):
    def __format__(self, spec: str) -> str:
        if spec:
            return "{" + self + ":" + spec + "}"
        return "{" + self + "}"

class _SafeFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            if key in kwargs:
                return kwargs[key]
            return _SafeKey(key)
        return string.Formatter.get_value(self, key, args, kwargs)

_SAFE_FORMATTER = _SafeFormatter()

def safe_format(template: str, **kwargs) -> str:
    try:
        return _SAFE_FORMATTER.vformat(template, (), kwargs)
    except Exception:
        return template

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
    """Extracts pipeline latency from the architect's plan text"""
    # 1. Priority: Look for "LATENCY: X CYCLES" (Matches Architect Output)
    match = re.search(r'LATENCY\s*[:=]?\s*(\d+)(?:\s*CYCLES?)?', plan, re.IGNORECASE)
    if match: return int(match.group(1))
    
    # 2. Fallback: Look for "Pipeline depth: X"
    match = re.search(r'Pipeline depth\s*[:=]?\s*(\d+)', plan, re.IGNORECASE)
    if match: return int(match.group(1))

    # 3. Safety Net: Ignore numbers > 50 (Likely MHz or Hz)
    matches = re.findall(r'latency\D*(\d+)', plan, re.IGNORECASE)
    for m in matches:
        val = int(m)
        if val < 50: return val # A filter latency is rarely > 50 cycles
        
    return 1 # Default

def resolve_tb_method_from_plan(plan: str) -> str:
    if not plan:
        return ""
    match = re.search(r'\bTB_LANGUAGE\s*=\s*([A-Za-z0-9_]+)', plan, re.IGNORECASE)
    if match:
        value = match.group(1)
        normalized = value.strip().lower()
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "cocotb"
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"
    match = re.search(r'\bVERIFICATION_METHOD\s*=\s*([A-Za-z0-9_]+)', plan, re.IGNORECASE)
    if match:
        value = match.group(1)
        normalized = value.strip().lower()
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "cocotb"
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"
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
        if normalized in ["cocotb", "cocotbx", "python", "pyuvm"]:
            return "cocotb"
        if normalized in ["systemverilog", "sv", "verilog", "iverilog", "verilator"]:
            return "systemverilog"

    return "systemverilog"

def resolve_cocotb_sim(verif_specs: dict) -> str:
    methods = verif_specs.get("verification_methods", {})
    cocotb_cfg = methods.get("cocotb") or methods.get("cocotbx") or {}
    sim = cocotb_cfg.get("makefile_sim")
    if not sim:
        sim = verif_specs.get("verification_framework", {}).get("simulator")

    sim = sim or "icarus"
    sim = str(sim).strip().lower()
    if sim == "iverilog":
        sim = "icarus"
    return sim


def get_tb_plan(state: AgentState) -> str:
    return state.get("verif_plan") or state.get("design_plan", "")

def extract_filter_class(plan: str) -> str:
    """Extract filter class (FIR or IIR) from design plan."""
    if not plan:
        return "FIR"  # Default to FIR
    for line in plan.split('\n'):
        if line.startswith('FILTER_CLASS='):
            cls = line.split('=', 1)[1].strip().upper()
            return cls
    return "FIR"  # Default to FIR

def generate_cocotb_testbench(state: AgentState, tb_attempt: int, error_feedback: str, current_tb: str):
    """Generate a Python cocotb testbench for the RTL"""
    verif_specs = state["verif_specs"]
    plan = get_tb_plan(state)
    specs = state["specs"]
    
    # CRITICAL FIX 1: Extract Latency to inject into prompt
    latency = extract_latency_from_plan(plan)
    print(colored(f"[TB Generator] Detected Pipeline Latency: {latency}", "cyan"))

    # CRITICAL FIX 2: Format the Requirements string FIRST
    # The prompt has {pipeline_latency} inside COCOTB_REQUIREMENTS
    formatted_requirements = safe_format(COCOTB_REQUIREMENTS, pipeline_latency=latency)

    user_content = safe_format(
        COCOTB_USER_PROMPT,
        plan=plan,
        cocotb_requirements=formatted_requirements
    )

    if error_feedback and current_tb:
        # CRITICAL FIX 3: Truncate error log
        clean_error = truncate_error_log(error_feedback)
        user_content += COCOTB_ERROR_FEEDBACK.format(
            error_feedback=clean_error,
            current_tb=current_tb
        )

    messages = [
        {"role": "system", "content": COCOTB_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    try:
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        clean_code = clean_python_code(response.choices[0].message.content)
        if not clean_code.endswith('\n'):
            clean_code += '\n'

        # Also generate Makefile for cocotb
        makefile_content = generate_cocotb_makefile(verif_specs, state["specs"])

        return {
            "tb_code": clean_code,
            "tb_attempt_count": tb_attempt + 1,
            "status": "tb_generated",
            "makefile": makefile_content
        }

    except Exception as e:
        print(colored(f"[TB Generator] Error: {e}", "red"))
        return {"status": "failed"}


def generate_systemverilog_testbench(state: AgentState, tb_attempt: int, error_feedback: str, current_tb: str):
    """Generate a SystemVerilog testbench"""
    plan = get_tb_plan(state)
    specs = state["specs"]

    # Select prompt template based on filter class (FIR or IIR)
    filter_class = extract_filter_class(plan)
    if filter_class == "IIR":
        prompt_template = IIR_SV_TB_USER_PROMPT
        print(colored("[TB Generator] Using IIR SV testbench prompt", "cyan"))
    else:
        prompt_template = FIR_SV_TB_USER_PROMPT
        print(colored("[TB Generator] Using FIR SV testbench prompt", "cyan"))

    user_content = safe_format(
        prompt_template,
        plan=plan,
        iverilog_constraints=IVERILOG_CONSTRAINTS
    )

    if error_feedback and current_tb:
        clean_error = truncate_error_log(error_feedback)
        user_content += f"""

        ========== PREVIOUS TB FAILED ==========
        ERROR OUTPUT:
        {clean_error}

        PREVIOUS CODE:
        {current_tb}

        Fix the errors and return corrected testbench code.
        """

    messages = [
        {"role": "system", "content": SV_TB_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    try:
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        clean_code = clean_verilog_code(response.choices[0].message.content)
        clean_code = fix_signed_real_usage(clean_code)
        clean_code = fix_sv_signed_conversions(clean_code)
        clean_code = fix_sv_latency_compare(clean_code)
        dut_module = resolve_rtl_module_name(specs)
        clean_code = strip_duplicate_dut_module(clean_code, dut_module)
        latency = extract_latency_from_plan(plan)
        clean_code = fix_sv_timeout_block(clean_code, latency)
        if not clean_code.endswith('\n'):
            clean_code += '\n'

        return {
            "tb_code": clean_code,
            "tb_attempt_count": tb_attempt + 1,
            "status": "tb_generated"
        }

    except Exception as e:
        print(colored(f"[TB Generator] Error: {e}", "red"))
        return {"status": "failed"}


def generate_cocotb_makefile(verif_specs, rtl_specs):
    """Generate Makefile for cocotb simulation"""
    import re
    import os
    rtl_file = rtl_specs["project_settings"]["output_file"]
    tb_file_base = verif_specs["testbench_settings"].get("filename", "test_filter")

    module_name = None
    try:
        with open(rtl_file, 'r') as f:
            rtl_content = f.read()
        match = re.search(r'^\s*module\s+(\w+)', rtl_content, re.MULTILINE)
        if match:
            module_name = match.group(1)
            print(colored(f"[Makefile] Detected module name: {module_name}", "cyan"))
    except Exception:
        module_name = None

    if not module_name:
        module_name = (
            rtl_specs.get("module_definition", {}).get("module_name")
            or rtl_specs.get("project_settings", {}).get("module_name")
            or os.path.splitext(os.path.basename(rtl_file))[0]
        )

    # Extract test name
    test_module = os.path.basename(tb_file_base.replace(".py", ""))
    
    # Calculate relative paths
    rtl_dir = os.path.dirname(rtl_file) or "."
    makefile_dir = os.path.dirname(tb_file_base) or "."
    
    if rtl_dir == makefile_dir:
        rtl_rel_path = os.path.basename(rtl_file)
    else:
        try:
            rtl_rel_path = os.path.relpath(rtl_file, makefile_dir)
        except ValueError:
            # Fallback for Windows if on different drives
            rtl_rel_path = os.path.abspath(rtl_file)

    sim = resolve_cocotb_sim(verif_specs)
    makefile = f"""# Makefile for cocotb simulation

# Defaults
SIM ?= {sim}
TOPLEVEL_LANG ?= verilog

# RTL source files
VERILOG_SOURCES += {rtl_rel_path}

# Top-level module
TOPLEVEL = {module_name}

# Python test module
COCOTB_TEST_MODULES = {test_module}

# Include cocotb makefiles
include $(shell cocotb-config --makefiles)/Makefile.sim
"""
    return makefile


def generate_tb_node(state: AgentState):
    """
    Main testbench generation node - routes to cocotb or SystemVerilog based on specs
    """
    verif_specs = state["verif_specs"]
    plan = get_tb_plan(state)
    method = resolve_tb_method(verif_specs, plan)
    error_feedback = state.get("error_report") or state.get("sim_report", "")
    current_tb = state.get("tb_code", "")
    tb_attempt = state.get("tb_attempt_count", 0)

    print(f"\n[TB Generator] Generating {method} testbench... (Attempt {tb_attempt + 1})")

    if tb_attempt >= MAX_TB_ATTEMPTS:
        print(colored(f"[TB Generator] Max attempts ({MAX_TB_ATTEMPTS}) reached.", "yellow"))
        return {"status": "failed"}

    if method.lower() in ["cocotb", "cocotbx"]:
        return generate_cocotb_testbench(state, tb_attempt, error_feedback, current_tb)
    else:
        return generate_systemverilog_testbench(state, tb_attempt, error_feedback, current_tb)

"""
RTL Designer Agent - Generates SystemVerilog RTL code
"""
import re
import os
from termcolor import colored
from config.settings import MODEL_NAME, API_KEY, MAX_RTL_ATTEMPTS, USE_COMPACT_PROMPTS
from models.state import AgentState
from utils.code_cleaners import clean_verilog_code
from utils.api_utils import call_llm_with_retry

# Import prompts based on configuration
from config.prompts_compact import (
        RTL_DESIGNER_SYSTEM_PROMPT,
        FIR_RTL_DESIGNER_USER_PROMPT,
        IIR_RTL_DESIGNER_USER_PROMPT,
        IVERILOG_CONSTRAINTS,
        VERILATOR_CONSTRAINTS,
        RTL_ERROR_FEEDBACK_TEMPLATE,
        ERROR_PATTERNS
)
 

def _get_coeff_params(specs):
    """Extract coefficient width params from specs."""
    hw_specs = specs.get("hardware_specifications", {})
    try:
        coeff_width = int(hw_specs.get("coefficient_width"))
    except (TypeError, ValueError):
        return None, None, None, None
    coeff_frac_bits = max(coeff_width - 1, 0)
    coeff_scale = 1 << coeff_frac_bits
    max_coeff = (1 << (coeff_width - 1)) - 1
    min_coeff = -(1 << (coeff_width - 1))
    return coeff_width, coeff_scale, max_coeff, min_coeff


def _fix_coeff_assignments(code, coeffs, coeff_width, coeff_scale, max_coeff, min_coeff, prefix="coeff"):
    """Replace coefficient assignments in RTL code with correctly scaled values."""
    for idx, coeff in enumerate(coeffs):
        val = int(round(coeff * coeff_scale))
        val = max(min_coeff, min(max_coeff, val))

        assign_pat = rf"assign\s+{prefix}\[{idx}\]\s*=\s*[^;]+;"
        assign_repl = f"assign {prefix}[{idx}] = {coeff_width}'sd{val};"
        code, assign_count = re.subn(assign_pat, assign_repl, code)

        if assign_count == 0:
            init_pat = rf"{prefix}\[{idx}\]\s*=\s*[^;]+;"
            init_repl = f"{prefix}[{idx}] = {coeff_width}'sd{val};"
            code = re.sub(init_pat, init_repl, code)
    return code


def fix_rtl_scaling(code: str, specs: dict) -> str:
    if not code or not specs:
        return code

    math_model = specs.get("mathematical_model", {})
    # Support both new and legacy coefficient keys
    fwd_coeffs = (math_model.get("forward_coefficients")
                  or math_model.get("coefficients")
                  or [])
    fbk_coeffs = math_model.get("feedback_coefficients") or []

    coeff_width, coeff_scale, max_coeff, min_coeff = _get_coeff_params(specs)
    if not fwd_coeffs or not coeff_width:
        return code

    coeff_frac_bits = max(coeff_width - 1, 0)

    # Ensure COEFF_FRAC_BITS exists
    if "COEFF_FRAC_BITS" not in code:
        code = re.sub(
            r"localparam\s+\w*FRAC\w*\s*=\s*[^;]+;",
            "    localparam COEFF_FRAC_BITS = COEFF_WIDTH - 1;",
            code,
            count=1
        )
        code = re.sub(
            r"(parameter\s+COEFF_WIDTH\s*=\s*\d+\s*;)",
            r"\1\n    localparam COEFF_FRAC_BITS = COEFF_WIDTH - 1;",
            code,
            count=1
        )

    # Fix forward coefficient assignments
    code = _fix_coeff_assignments(code, fwd_coeffs, coeff_width, coeff_scale, max_coeff, min_coeff, "coeff")

    # Fix feedback coefficient assignments (IIR)
    if fbk_coeffs and len(fbk_coeffs) > 1:
        code = _fix_coeff_assignments(code, fbk_coeffs, coeff_width, coeff_scale, max_coeff, min_coeff, "feedback_coeff")

    # Fix output scaling if result_temp is derived directly from accum bits.
    if "result_temp" in code and "scaled_accum" not in code and "accum" in code:
        decl_pat = r"(\s*logic\s+signed\s+\[[^\]]+\]\s+result_temp\s*;)"
        code, decl_count = re.subn(
            decl_pat,
            r"\1\n    logic signed [ACC_WIDTH-1:0] scaled_accum;",
            code,
            count=1
        )

        assign_pat = r"assign\s+result_temp\s*=\s*[^;]*accum[^;]*;"
        if re.search(assign_pat, code):
            code = re.sub(assign_pat, "assign scaled_accum = accum >>> COEFF_FRAC_BITS;\n    assign result_temp = scaled_accum[DATA_WIDTH-1:0];", code, count=1)

    return code

def extract_filter_class(plan: str) -> str:
    """Extract filter class (FIR or IIR) from design plan."""
    if not plan:
        return "FIR"  # Default to FIR
    for line in plan.split('\n'):
        if line.startswith('FILTER_CLASS='):
            cls = line.split('=', 1)[1].strip().upper()
            return cls
    return "FIR"  # Default to FIR

def generate_rtl_node(state: AgentState):
    """
    Generates RTL code based on the architecture plan and specifications.
    Handles error feedback and retry logic.
    """
    specs = state["specs"]
    plan = state.get("design_plan", "No plan provided.")
    error_feedback = state.get("error_report", "")
    sim_feedback = state.get("sim_report", "")
    current_code = state.get("verilog_code", "")
    attempt = state.get("attempt_count", 0)
    verif_specs = state.get("verif_specs", {})

    # Check if max attempts reached BEFORE generating new code
    if attempt >= MAX_RTL_ATTEMPTS:
        print(colored(f"\n[Designer] Max attempts ({MAX_RTL_ATTEMPTS}) reached. Stopping.", "red", attrs=['bold']))
        return {
            "status": "max_attempts_reached"
        }

    print(f"\n[Designer] Generating RTL... (Attempt {attempt + 1})")

    # Select constraints based on simulation tool
    sim_tool = verif_specs.get("verification_framework", {}).get("simulator", "iverilog")
    if sim_tool == "verilator":
        constraints = VERILATOR_CONSTRAINTS
        print(colored("[Designer] Using Verilator constraints", "cyan"))
    else:
        constraints = IVERILOG_CONSTRAINTS
        print(colored("[Designer] Using Icarus Verilog constraints", "cyan"))

    # Select prompt template based on filter class (FIR or IIR)
    filter_class = extract_filter_class(plan)
    if filter_class == "IIR":
        prompt_template = IIR_RTL_DESIGNER_USER_PROMPT
        print(colored("[Designer] Using IIR RTL designer prompt", "cyan"))
    else:
        prompt_template = FIR_RTL_DESIGNER_USER_PROMPT
        print(colored("[Designer] Using FIR RTL designer prompt", "cyan"))

    # Build the user prompt - provide both constraint blocks so prompt formatting
    # never fails regardless of selected simulator.
    user_content = prompt_template.format(
        plan=plan,
        iverilog_constraints=IVERILOG_CONSTRAINTS,
        verilator_constraints=VERILATOR_CONSTRAINTS
    )

    # Add error feedback if this is a retry
    if error_feedback and current_code and (state["status"] == "processing" or state["status"] == "rtl_compile_error"):
        user_content += RTL_ERROR_FEEDBACK_TEMPLATE.format(
            error_feedback=error_feedback,
            current_code=current_code,
            error_patterns=ERROR_PATTERNS
        )

    # Add simulation feedback if available
    if sim_feedback and current_code and state["status"] == "sim_failed":
        user_content += f"""

        ========== SIMULATION FAILED - LOGIC ERROR ==========
        The RTL compiled successfully but simulation showed incorrect behavior.

        SIMULATION ERROR OUTPUT:
        {sim_feedback}

        PREVIOUS RTL CODE:
        {current_code}

        ANALYZE THE SIMULATION OUTPUT AND FIX THE LOGIC ERRORS.
        Common issues: incorrect pipeline delays, wrong coefficient values, sign handling errors.
        Return the corrected RTL code ONLY.
        """

    messages = [
        {"role": "system", "content": RTL_DESIGNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    try:
        response = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
        raw_code = response.choices[0].message.content
        clean_code = clean_verilog_code(raw_code)
        clean_code = fix_rtl_scaling(clean_code, specs)

        # Write to file
        output_file = specs["project_settings"]["output_file"]
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w') as f:
            f.write(clean_code)
        print(colored(f"[Designer] RTL code saved to {output_file}", "green"))

        return {
            "verilog_code": clean_code,
            "attempt_count": attempt + 1,
            "status": "rtl_generated"
        }

    except Exception as e:
        print(colored(f"[Designer] Error: {e}", "red"))
        return {"status": "failed"}
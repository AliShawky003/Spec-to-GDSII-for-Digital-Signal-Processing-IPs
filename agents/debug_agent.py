"""
Debug Agent - AI-powered diagnosis of simulation failures
Uses LLM to reason about RTL, testbench, and simulation output to identify root causes
"""
import re
from termcolor import colored
from models.state import AgentState
from config.settings import MODEL_NAME, API_KEY
from utils.api_utils import call_llm_with_retry
from utils.error_retriever import retrieve_errors, format_error_context, describe_matches


DEBUG_SYSTEM_PROMPT = """
You are an expert hardware debugging engineer specializing in digital signal processing filters.
You analyze simulation failures by examining:
  1. The RTL implementation (SystemVerilog code)
  2. The testbench and how it drives the DUT
  3. Actual vs expected output comparison

Your task is to diagnose what went wrong and recommend a fix.

ROOT CAUSE CATEGORIES:
  RTL Issues (requires RTL redesign):
    - Wrong filter topology implemented (e.g., direct_form instead of symmetric)
    - Incorrect pipeline latency
    - Coefficient quantization errors or wrong coefficient values
    - Accumulator overflow/underflow
    - Wrong bit extraction or output scaling
    - State machine or control logic errors
  
  Testbench Issues (requires TB adjustment):
    - Incorrect latency compensation (samples read at wrong time)
    - Wrong output scaling/interpretation
    - Incorrect stimulus (test vectors)
    - Timing issues (not waiting long enough after valid signal)

Provide your analysis in this exact format:
DIAGNOSIS: <one-line summary>
ROOT_CAUSE: <detailed explanation of what went wrong and why>
EVIDENCE: <specific observations from the output that support this diagnosis>
RECOMMENDATION: RTL or TB
"""

DEBUG_USER_PROMPT = """
Analyze this simulation failure:

DESIGN PLAN:
{design_plan}

VERIFICATION PLAN:
{verif_plan}

RTL CODE:
{rtl_code}

TESTBENCH CODE:
{tb_code}

SIMULATION OUTPUT:
{sim_output}

What is causing the mismatch? Is it an RTL problem or a TB problem? Check if it is a Latency problem too.
"""


def _extract_sample_data(output: str) -> dict:
    """Extract key statistics and sample data from simulation output."""
    data = {
        "total_samples": 0,
        "pass_rate": 0.0,
        "max_error": 0.0,
        "avg_error": 0.0,
        "sample_lines": []
    }
    
    # Extract statistics
    total_match = re.search(r"Total samples tested:\s+(\d+)", output)
    if total_match:
        data["total_samples"] = int(total_match.group(1))
    
    pass_rate_match = re.search(r"Pass rate:\s+([0-9.]+)%", output)
    if pass_rate_match:
        data["pass_rate"] = float(pass_rate_match.group(1))
    
    max_error_match = re.search(r"Max error:\s+([0-9.]+)%", output)
    if max_error_match:
        data["max_error"] = float(max_error_match.group(1))
    
    avg_error_match = re.search(r"Avg error:\s+([0-9.]+)%", output)
    if avg_error_match:
        data["avg_error"] = float(avg_error_match.group(1))
    
    # Extract first few sample lines
    sample_lines = re.findall(r"^\s*\d+\s+.+?✗FAIL", output, re.MULTILINE)
    data["sample_lines"] = sample_lines[:10]  # First 10 failures
    
    return data


def _generate_fix(original_code: str, analysis: str, code_type: str = "RTL",
                   past_context: str = "") -> str:
    """Ask the LLM to produce a fixed version of the given code.

    Args:
        original_code: the current RTL or TB source.
        analysis: full LLM analysis text including diagnosis/root cause.
        code_type: "RTL" or "TB" to tailor the prompt.
        past_context: pre-formatted error-memory hints from similar past bugs.

    Returns:
        The updated source code returned by the LLM.

    Raises:
        Exception if the LLM call fails or returns empty content.
    """
    prompt = ""
    if past_context:
        prompt += past_context + "\n\n"
    prompt += f"The simulation failed with the following analysis:\n\n{analysis}\n\n"
    if code_type.upper() == "RTL":
        prompt += "Here is the current RTL implementation that produced the failure:\n"
    else:
        prompt += "Here is the current testbench implementation that produced the failure:\n"
    prompt += original_code + "\n\n"
    prompt += "Please provide the corrected code only, with no additional explanation."

    messages = [
        {"role": "system", "content": "You are a hardware designer who fixes RTL and testbenches based on failure analysis."},
        {"role": "user", "content": prompt}
    ]

    resp = call_llm_with_retry(model=MODEL_NAME, messages=messages, api_key=API_KEY)
    fixed = resp.choices[0].message.content
    if not fixed.strip():
        raise Exception("LLM returned empty fix code")
    return fixed


def debug_node(state: AgentState):
    """
    Main debug node - uses AI to diagnose simulation failures.
    Takes RTL code, TB code, and sim output to reason about root cause.
    """
    # Only run if simulation failed
    if state.get("status") not in ["tb_error", "rtl_sim_error", "sim_failed"]:
        print(colored("[Debugger] Skipping - simulation did not fail.", "yellow"))
        return {"status": "debug_skipped"}
    
    rtl_code = state.get("verilog_code", "")
    tb_code = state.get("tb_code", "")
    sim_output = state.get("sim_report", "")
    specs = state.get("specs", {})
    design_plan = state.get("design_plan", "")
    verif_plan = state.get("verif_plan", "")
    
    if not rtl_code or not tb_code or not sim_output:
        print(colored("[Debugger] Missing context (RTL, TB, or sim output). Defaulting to TB fix.", "yellow"))
        return {
            "status": "tb_error",
            "recommended_action": "tb",
            "reason": "Insufficient debug context"
        }
    
    print(colored("\n[Debugger] Analyzing failure with AI reasoning...", "blue"))

    # Error-memory retrieval: look for similar past failures to pre-bias the
    # LLM's analysis. Uses the simulator source and topology/filter_class
    # from the design plan.
    topo_match = re.search(r'^TOPOLOGY=(.+)$', design_plan, re.MULTILINE)
    fc_match   = re.search(r'^FILTER_CLASS=(.+)$', design_plan, re.MULTILINE)
    topology     = topo_match.group(1).strip() if topo_match else ""
    filter_class = fc_match.group(1).strip() if fc_match else "any"
    past_errors = retrieve_errors(
        raw_error=sim_output,
        source="simulator",
        topology=topology,
        filter_class=filter_class,
        top_k=2,
    )
    if past_errors:
        print(colored(
            f"[Debugger] Error memory hits: {describe_matches(past_errors)}",
            "cyan"
        ))
    past_context = format_error_context(past_errors)

    # Use entire code/output (no truncation) so the LLM sees full context
    user_prompt = DEBUG_USER_PROMPT.format(
        design_plan=design_plan,
        verif_plan=verif_plan,
        rtl_code=rtl_code,
        tb_code=tb_code,
        sim_output=sim_output
    )
    if past_context:
        user_prompt = past_context + "\n" + user_prompt
    
    messages = [
        {"role": "system", "content": DEBUG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        response = call_llm_with_retry(
            model=MODEL_NAME,
            messages=messages,
            api_key=API_KEY
        )
        
        analysis = response.choices[0].message.content
        print(colored("\n[Debugger] LLM Analysis:", "cyan"))
        print(analysis)
        
        # Parse LLM response
        diagnosis = extract_diagnosis_line(analysis, "DIAGNOSIS:")
        root_cause = extract_diagnosis_line(analysis, "ROOT_CAUSE:")
        evidence = extract_diagnosis_line(analysis, "EVIDENCE:")
        recommendation = extract_diagnosis_line(analysis, "RECOMMENDATION:")
        
        # Determine routing
        recommended_action = "tb"
        if recommendation and "rtl" in recommendation.lower():
            recommended_action = "rtl"
        elif recommendation and "tb" in recommendation.lower():
            recommended_action = "tb"
        
        print(colored(f"\n[Debugger] Final recommendation: {recommended_action.upper()}", "cyan", attrs=["bold"]))

        # Attempt automatic fix for the chosen domain
        if recommended_action == "rtl":
            print(colored("[Debugger] Attempting automatic RTL fix...", "cyan"))
            try:
                fixed_code = _generate_fix(rtl_code, analysis, code_type="RTL", past_context=past_context)
                print(colored("[Debugger] RTL fix generated by AI. Updating state.", "green"))
                return {
                    "status": "rtl_fixed",
                    "verilog_code": fixed_code,
                    "recommended_action": "rtl",
                    "diagnosis": diagnosis,
                    "root_cause": root_cause,
                    "evidence": evidence,
                    "error_report": state.get("sim_report", "")
                }
            except Exception as exc:
                print(colored(f"[Debugger] RTL fix failed: {exc}", "red"))
                # fallback to routing
                return {
                    "status": "rtl_sim_error",
                    "recommended_action": "rtl",
                    "diagnosis": diagnosis,
                    "root_cause": root_cause,
                    "evidence": evidence,
                    "error_report": state.get("sim_report", "")
                }
        else:
            print(colored("[Debugger] Attempting automatic TB fix...", "cyan"))
            try:
                fixed_tb = _generate_fix(tb_code, analysis, code_type="TB", past_context=past_context)
                print(colored("[Debugger] TB fix generated by AI. Updating state.", "green"))
                return {
                    "status": "tb_fixed",
                    "tb_code": fixed_tb,
                    "recommended_action": "tb",
                    "diagnosis": diagnosis,
                    "root_cause": root_cause,
                    "evidence": evidence,
                    "error_report": state.get("sim_report", "")
                }
            except Exception as exc:
                print(colored(f"[Debugger] TB fix failed: {exc}", "red"))
                return {
                    "status": "tb_error",
                    "recommended_action": "tb",
                    "diagnosis": diagnosis,
                    "root_cause": root_cause,
                    "evidence": evidence,
                    "error_report": state.get("sim_report", "")
                }
    
    except Exception as e:
        print(colored(f"[Debugger] LLM call failed: {e}", "red"))
        print(colored("[Debugger] Attempting heuristic fallback...", "yellow"))
        # use simple classifier from simulator_agent to decide
        try:
            from agents.simulator_agent import classify_sv_failure
            classification = classify_sv_failure(sim_output, specs)
            print(colored(f"[Debugger] Heuristic classification: {classification.upper()}", "cyan"))
            recommended_action = "rtl" if classification == "rtl" else "tb"
            status = "rtl_sim_error" if recommended_action == "rtl" else "tb_error"
            return {
                "status": status,
                "recommended_action": recommended_action,
                "reason": f"LLM failed ({e}); heuristic routed to {recommended_action}"
            }
        except Exception:
            print(colored("[Debugger] Heuristic fallback also failed. Defaulting to TB fix.", "red"))
            return {
                "status": "tb_error",
                "recommended_action": "tb",
                "reason": f"Debug analysis failed: {str(e)}"
            }


def extract_diagnosis_line(text: str, key: str) -> str:
    """
    Extract a line from LLM response that starts with a key.
    E.g., extract_diagnosis_line(text, "DIAGNOSIS:") returns the diagnosis value.
    """
    pattern = re.escape(key) + r"\s*(.+?)(?=\n[A-Z_]+:|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""

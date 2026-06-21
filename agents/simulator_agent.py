"""
Simulator Agent - Runs simulations (iverilog for SV, make for cocotb)
"""
import subprocess
import os
import re
from collections import Counter
from termcolor import colored
from config.settings import DEFAULT_SIM_TIMEOUT, COCOTB_SIM_TIMEOUT
from models.state import AgentState


def extract_and_display_comparisons(output: str):
    """Extract comparison data from cocotb output and display side-by-side"""
    # Try to find comparison lines from the output
    # Pattern 1: Sample N: RTL=[value] Golden=[value] Error=[value]%
    pattern1 = r'Sample\s+(\d+):\s+RTL=([0-9.-]+)\s+Golden=([0-9.-]+)\s+(?:Relative )?Error=([0-9.-]+)%'
    matches = re.findall(pattern1, output)
    
    if matches:
        print(colored("\n" + "="*80, "cyan"))
        print(colored("  SAMPLE-BY-SAMPLE COMPARISON", "cyan"))
        print(colored("="*80, "cyan"))
        
        # Show header
        print(f"{'Sample':<10} {'RTL Output':<18} {'Golden Model':<18} {'Error %':<12} {'Status':<8}")
        print("-" * 80)
        
        # Show samples (limit to first 20 and last 5 to avoid too much output)
        all_matches = matches
        if len(all_matches) > 25:
            # Show first 10
            for sample, rtl, golden, error in all_matches[:10]:
                error_val = abs(float(error))
                status = colored("✓PASS", "green") if error_val < 0.5 else colored("✗FAIL", "red")
                print(f"{int(sample):<10} {float(rtl):<18.6f} {float(golden):<18.6f} {error_val:<12.4f} {status}")
            
            print(colored("...(skipped middle samples)...", "yellow"))
            
            # Show last 5
            for sample, rtl, golden, error in all_matches[-5:]:
                error_val = abs(float(error))
                status = colored("✓PASS", "green") if error_val < 0.5 else colored("✗FAIL", "red")
                print(f"{int(sample):<10} {float(rtl):<18.6f} {float(golden):<18.6f} {error_val:<12.4f} {status}")
        else:
            # Show all
            for sample, rtl, golden, error in all_matches:
                error_val = abs(float(error))
                status = colored("✓PASS", "green") if error_val < 0.5 else colored("✗FAIL", "red")
                print(f"{int(sample):<10} {float(rtl):<18.6f} {float(golden):<18.6f} {error_val:<12.4f} {status}")
        
        print("-" * 80)
        
        # Summary statistics
        errors = [abs(float(match[3])) for match in all_matches]
        print(colored("\nSTATISTICS:", "cyan"))
        print(f"  Total samples tested: {len(all_matches)}")
        print(f"  Max error:            {max(errors):.4f}%")
        print(f"  Min error:            {min(errors):.4f}%")
        print(f"  Avg error:            {sum(errors)/len(errors):.4f}%")
        # Use consistent 0.5% threshold for pass/fail throughout
        pass_count = len([e for e in errors if e < 0.5])
        fail_count = len(errors) - pass_count
        pass_rate = (pass_count / len(errors)) * 100
        print(f"  Pass rate:            {pass_rate:.1f}%")
        print(colored("="*80, "cyan") + "\n")
        return True
    
    # Pattern 2: Look for test summary
    summary_pattern = r'Tests passed:\s*(\d+)\s+Tests failed:\s*(\d+)'
    summary_match = re.search(summary_pattern, output)
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        print(colored(f"\n✓ Test Summary: {passed} passed, {failed} failed", "green"))
        return True
    
    return False

def _most_common_value(values):
    rounded = [round(val, 6) for val in values]
    counts = Counter(rounded)
    value, count = counts.most_common(1)[0]
    return value, count / len(rounded)

def classify_sv_failure(output: str, specs: dict) -> str:
    """
    Heuristic: classify SV sim failure as RTL or TB issue.
    Returns: "rtl" or "tb".
    """
    # 1. Parse all sample lines
    sample_pattern = r"Sample\s+\d+:\s+RTL=([0-9\.\-]+)\s+Golden=([0-9\.\-]+)\s+Error=([0-9\.\-]+)%"
    matches = re.findall(sample_pattern, output)
    
    total_samples = len(matches)
    if total_samples == 0:
        return "tb"  # No data means TB likely crashed or didn't run

    # 2. Calculate Failure Rate directly from data
    #    Use consistent 0.5% threshold (matches the pass rate calculation)
    fail_count = sum(1 for _, _, err in matches if float(err) >= 0.5)
    failure_rate = fail_count / total_samples

    print(colored(f"[Classifier] Failure Rate: {failure_rate*100:.1f}% ({fail_count}/{total_samples})", "cyan"))

    # 3. Apply the "85% Rule"
    #    High failure rate usually means RTL logic is fundamentally broken 
    #    (e.g., wrong coeffs, bad pipeline, overflow)
    if failure_rate >= 0.85:
        return "rtl"

    # 4. Check for "Stuck at Constant" (RTL issue)
    rtl_values = []
    for rtl_str, _, _ in matches:
        try:
            rtl_values.append(float(rtl_str))
        except ValueError:
            continue

    if len(rtl_values) >= 10:
        common_val, ratio = _most_common_value(rtl_values)
        if ratio >= 0.9: # 90% of outputs are identical
            print(colored(f"[Classifier] RTL Stuck at {common_val} ({ratio*100:.1f}%)", "cyan"))
            return "rtl"

    # Default to TB error (scaling issues, latency shifts usually have <100% fail rate)
    return "tb"
def run_cocotb_simulation(state: AgentState):
    """Run cocotb simulation using Makefile"""
    tb_code = state["tb_code"]
    verif_specs = state["verif_specs"]
    tb_filename_base = verif_specs["testbench_settings"].get("filename", "test_filter")

    # Add .py extension if not present
    if not tb_filename_base.endswith('.py'):
        tb_filename = tb_filename_base + '.py'
    else:
        tb_filename = tb_filename_base

    makefile_content = state.get("makefile", "")

    # Write Python testbench (with UTF-8 encoding to handle Unicode characters)
    os.makedirs(os.path.dirname(tb_filename), exist_ok=True)
    with open(tb_filename, 'w', encoding='utf-8') as f:
        f.write(tb_code)
    print(f"[Simulator] cocotb testbench saved to {tb_filename}")

    # Write Makefile to output folder (same directory as testbench)
    output_dir = os.path.dirname(tb_filename) or "."
    makefile_path = os.path.join(output_dir, "Makefile")
    with open(makefile_path, 'w', encoding='utf-8') as f:
        f.write(makefile_content)
    print(f"[Simulator] Makefile generated at {makefile_path}")

    # Run cocotb simulation from output directory
    print("[Simulator] Running cocotb simulation with: make")
    try:
        # Update environment to include ~/.local/bin for cocotb-config
        env = os.environ.copy()
        home_dir = os.path.expanduser("~")
        local_bin = os.path.join(home_dir, ".local", "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        
        # Force CocoTB to print FULL logs (don't suppress Python tracebacks)
        env["COCOTB_REDUCED_LOG_FMT"] = "0"
        env["COCOTB_LOG_LEVEL"] = "DEBUG"

        result = subprocess.run(
            ["wsl", "bash", "-lc", f"cd {output_dir} && make"],
            capture_output=True,
            text=True,
            timeout=COCOTB_SIM_TIMEOUT,
            env=env
        )

        full_output = result.stdout + "\n" + result.stderr

        # Detect pre-simulation compilation failures in the make/iverilog output
        compile_error_patterns = [
            "syntax error",
            "error: Invalid module item",
            "error: Malformed statement",
            ".sv: error",
            "iverilog: error",
            "Icarus Verilog error",
            "sorry:",
        ]
        if any(pat in full_output for pat in compile_error_patterns):
            print(colored("[Simulator] Compilation error detected during cocotb run!", "red"))
            # try to determine file origin
            error_file = None
            for line in full_output.splitlines():
                m = re.match(r'([^:]+):\d+:', line)
                if m:
                    error_file = m.group(1).strip()
                    break
            rtl_file = state.get("specs", {}).get("project_settings", {}).get("output_file", "")
            tb_file = verif_specs["testbench_settings"].get("filename", "")
            is_rtl_error = None
            if error_file:
                if tb_file and tb_file in error_file:
                    is_rtl_error = False
                elif rtl_file and rtl_file in error_file:
                    is_rtl_error = True
            elif rtl_file and rtl_file in full_output:
                is_rtl_error = True
            # route based on classification or assume RTL if unsure
            if is_rtl_error is None:
                print(colored("[Simulator] Unable to identify source of compile error; assuming RTL.", "yellow"))
                return {"sim_report": full_output, "status": "rtl_compile_error"}
            if is_rtl_error:
                return {"sim_report": full_output, "status": "rtl_sim_error"}
            else:
                return {"sim_report": full_output, "status": "tb_error"}

        # Check for RTL compilation errors first (these need RTL regeneration, not TB fixing)
        # Only flag actual Verilog syntax errors in .sv files, not generic "Error" in make output
        rtl_error_patterns = [
            "syntax error",
            "error: Invalid module item",
            "error: Malformed statement",
            "Unable to find the root module",
            ".sv: error",  # Verilog file errors (more specific than just .sv:)
            "iverilog: error",  # Verilog compiler errors
            "sorry:",  # Iverilog limitation error (e.g., "sorry: constant selects in always_* processes")
            "Icarus Verilog error",
            "A for statement must use the index",  # Iverilog warning about loop variables
        ]
        
        # Also check for RTL-generated output issues in cocotb
        rtl_output_error_patterns = [
            "Can't convert LogicArray to int: it contains non-0/1 values",  # RTL output is X/Z (uninitialized)
            "LogicArray contains unknown values",  # RTL not driving outputs
        ]
        
        has_rtl_error = any(pattern in full_output for pattern in rtl_error_patterns)
        has_rtl_output_error = any(pattern in full_output for pattern in rtl_output_error_patterns)

        if has_rtl_error or has_rtl_output_error:
            print(colored("[Simulator] RTL compilation error detected!", "red"))
            print(full_output[-2000:])
            return {"sim_report": full_output, "status": "rtl_sim_error"}  # Route to RTL regeneration

        # Check for test results
        if "TEST PASSED" in full_output or "TESTS=1 PASS=1 FAIL=0" in full_output:
            print(colored("[Simulator] Cocotb test PASSED!", "green"))
            # Try to display comparison data
            extract_and_display_comparisons(full_output)
            return {"sim_report": full_output, "status": "success"}
        elif "TEST FAILED" in full_output or "FAIL=" in full_output:
            print(colored("[Simulator] Cocotb test FAILED. Analyzing root cause...", "red"))
            
            # Analyze failure type: RTL bad vs TB bad
            error_summary = full_output[-5000:]  # Last 5000 chars for full traceback analysis
            
            # Look for specific Python errors that indicate TB issues
            python_error_patterns = [
                ("AttributeError", "signal name mismatch"),  # Wrong signal name
                ("AssertionError", "comparison failed"),  # Assertion in checker
                ("IndexError", "queue or list issue"),  # Queue underflow
                ("TypeError", "type mismatch"),  # Type error in comparison
                ("KeyError", "dictionary key missing"),  # Spec lookup issue
                ("ValueError", "invalid value"),  # Bad parameter
                ("Traceback", "python exception"),  # Any Python error
            ]
            
            has_python_error = any(pattern[0] in error_summary for pattern in python_error_patterns)
            
            # RTL Issues (route to designer)
            rtl_bad_indicators = [
                ("Empty queue with valid output", "queue is empty"),  # DUT producing output without stimulus
                ("All outputs stuck at max", "RTL overflow/stuck"),
                ("All outputs are zero", "RTL not operating"),
                ("NaN or Inf in output", "invalid calculation"),
            ]
            
            # TB Issues (route to TB fixer)
            tb_bad_indicators = [
                ("Latency mismatch", "cycle", "latency"),  # Output timing issue
                ("Scaling error", "relative error", "tol"),  # Fixed-point scale issue
                ("Queue underflow", "queue", "empty"),
                ("Systematic offset", "MISMATCH", "Golden"),  # Consistent error pattern
            ]
            
            is_rtl_issue = any(indicator[1] in error_summary.lower() for indicator in rtl_bad_indicators)
            is_tb_issue = any(all(ind.lower() in error_summary.lower() for ind in indicator[1:]) 
                             for indicator in tb_bad_indicators)
            
            # If we see Python traceback, always route to TB fixer (it's a TB logic issue)
            if has_python_error:
                print(colored("[Simulator] Python traceback detected → Routing to TB fixer", "yellow"))
                print(colored("[Simulator] Full error output:", "yellow"))
                print(error_summary[-2000:])
                return {"sim_report": full_output, "status": "tb_error"}
            
            # Extract mismatch count if available
            import re
            mismatch_match = re.search(r"Found (\d+) mismatches", error_summary)
            if mismatch_match:
                mismatch_count = int(mismatch_match.group(1))
                comparison_match = re.search(r"Performed (\d+) comparisons", error_summary)
                if comparison_match:
                    total_comparisons = int(comparison_match.group(1))
                    failure_rate = (mismatch_count / total_comparisons * 100) if total_comparisons > 0 else 100
                    
                    # If >50% fail, likely RTL issue; <15% likely TB scaling/latency
                    if failure_rate > 50:
                        print(colored(f"[Simulator] HIGH FAILURE RATE ({failure_rate:.1f}%) → RTL likely BAD", "red"))
                        is_rtl_issue = True
                    elif failure_rate < 15:
                        print(colored(f"[Simulator] LOW FAILURE RATE ({failure_rate:.1f}%) → TB minor issue (scaling/latency)", "yellow"))
                        is_tb_issue = True
            
            if is_rtl_issue and not is_tb_issue:
                print(colored("[Simulator] Routing to RTL designer (RTL issue detected)", "red"))
                print(error_summary[-1000:])
                return {"sim_report": full_output, "status": "rtl_sim_error"}
            else:
                print(colored("[Simulator] Routing to TB fixer (TB logic issue detected)", "yellow"))
                print(error_summary[-1000:])
                return {"sim_report": full_output, "status": "tb_error"}
        else:
            print(colored("[Simulator] Cocotb simulation completed with unclear result.", "yellow"))
            print(full_output[-1000:])
            return {"sim_report": full_output, "status": "tb_error"}

    except subprocess.TimeoutExpired:
        print(colored("[Simulator] Cocotb simulation timeout (180s limit exceeded).", "red"))
        print(colored("[Simulator] Suggestion: Test may have too many vectors. Reduce NUM_SAMPLES in TB.", "yellow"))
        return {"sim_report": "Timeout after 180s", "status": "tb_error"}
    except Exception as e:
        print(colored(f"[Simulator] Error running cocotb: {e}", "red"))
        return {"sim_report": str(e), "status": "tb_error"}


def run_systemverilog_simulation(state: AgentState):
    """Run iverilog simulation for SystemVerilog testbench"""
    tb_code = state["tb_code"]
    verif_specs = state["verif_specs"]
    specs = state.get("specs", {})
    tb_filename = verif_specs["testbench_settings"].get("filename", "testbench.sv")

    # Add .sv extension if not present
    if not tb_filename.endswith('.sv'):
        tb_filename = tb_filename + '.sv'

    # Write testbench (with UTF-8 encoding)
    os.makedirs(os.path.dirname(tb_filename), exist_ok=True)
    with open(tb_filename, 'w', encoding='utf-8') as f:
        f.write(tb_code)
    print(f"[Simulator] Testbench saved to {tb_filename}")

    # Compile with iverilog
    print("[Simulator] Compiling testbench with iverilog...")
    try:
        rtl_file = specs.get("project_settings", {}).get("output_file")
        compile_cmd = ["wsl", "iverilog", "-g2012", "-o", "sim.out", tb_filename]
        if rtl_file:
            compile_cmd.append(rtl_file)

        print(colored(f"[Simulator] Compile command: {' '.join(compile_cmd)}", "cyan"))
        compile_result = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if compile_result.returncode != 0:
            print(colored("[Simulator] Testbench compilation failed.", "red"))
            print(compile_result.stderr)
            
            # Check if error is in RTL file or TB file
            error_output = compile_result.stderr
            rtl_file = specs.get("project_settings", {}).get("output_file", "")
            tb_file = tb_filename
            
            # Extract filename from first error line (format: filename:line: error)
            # to determine which file the error is actually in
            error_lines = error_output.strip().split('\n')
            error_file = None
            if error_lines:
                # Extract filename from first error line
                first_error = error_lines[0]
                match = re.match(r'([^:]+):\d+:', first_error)
                if match:
                    error_file = match.group(1).strip()
            
            # Classify based on actual error file
            is_rtl_error = None  # None means uncertain
            if error_file:
                # Check if error file is the TB file
                if tb_file in error_file or error_file.endswith('test_filter.sv') or 'testbench' in error_file.lower():
                    is_rtl_error = False
                # Check if error file is the RTL file
                elif rtl_file and rtl_file in error_file:
                    is_rtl_error = True
            elif rtl_file and rtl_file in error_output:
                # Fallback: check if RTL filename appears in the error message
                is_rtl_error = True

            if is_rtl_error is None:
                # Couldn't determine the source of the compile error
                print(colored("[Simulator] Could not identify whether compile error is RTL or TB. Assuming RTL by default.", "yellow"))
                return {"error_report": compile_result.stderr, "status": "rtl_compile_error"}

            if is_rtl_error:
                print(colored("[Simulator] Error detected in RTL file → Routing to RTL regeneration", "red"))
                return {"error_report": compile_result.stderr, "status": "rtl_sim_error"}
            else:
                print(colored("[Simulator] Error detected in testbench → Routing to TB fixer", "yellow"))
                return {"error_report": compile_result.stderr, "status": "tb_error"}

        print(colored("[Simulator] Compilation successful.", "green"))
        print("[Simulator] Running simulation...")
        
        sim_result = subprocess.run(
            ["wsl", "vvp", "sim.out"],
            capture_output=True,
            text=True,
            timeout=30  # Reduced from DEFAULT_SIM_TIMEOUT (300s) to catch hangs faster
        )

        full_output = sim_result.stdout + "\n" + sim_result.stderr

        # Check for pass/fail - analyze actual test results first
        # Quick acceptance for testbenches that print: "Test complete: <N> errors"
        test_complete_match = re.search(r"Test complete:\s*(\d+)\s*errors", full_output, re.IGNORECASE)
        if test_complete_match:
            err_count = int(test_complete_match.group(1))
            # If the testbench reported zero errors, treat as success
            if err_count == 0:
                print(colored("[Simulator] Simulation PASSED! (Test complete: 0 errors)", "green"))
                # Attempt to display any comparisons if present
                extract_and_display_comparisons(full_output)
                return {"sim_report": full_output, "status": "success"}

        # Also support parsing of '[DATA] Sample N | RTL = ... | TB_Golden = ... | Diff = ...' logs
        # Convert bracketed DATA lines into a simple comparable pattern so existing logic can pick them up
        if "[DATA] Sample" in full_output:
            # Normalize to 'Sample N: RTL=<v> Golden=<v> Error=<d>%' lines when possible
            def _normalize_data_lines(text):
                def repl(m):
                    idx = m.group('idx')
                    rtl = m.group('rtl')
                    golden = m.group('gold')
                    diff = m.group('diff')
                    # Treat Diff as absolute error (not percent) — placeholder for display
                    return f"Sample {idx}: RTL={rtl} Golden={golden} Error={diff}"
                pattern = re.compile(r"\[DATA\]\s*Sample\s+(?P<idx>\d+)\s*\|\s*RTL\s*=\s*(?P<rtl>-?\d+)\s*\|\s*TB_Golden\s*=\s*(?P<gold>-?\d+)\s*\|\s*Diff\s*=\s*(?P<diff>-?\d+)")
                return pattern.sub(repl, text)
            full_output = _normalize_data_lines(full_output)
        # Try multiple formats to extract failure rate
        
        # Format 1: Native testbench format - "Sample N: RTL=... Golden=... Error=...%"
        sample_pattern = r"Sample\s+\d+:\s+RTL=([0-9\.\-]+)\s+Golden=([0-9\.\-]+)\s+Error=([0-9\.\-]+)%"
        matches = re.findall(sample_pattern, full_output)
        
        # Format 2: Table format with ✗FAIL indicator (more reliable)
        if not matches or len(matches) == 0:
            # Extract pass/fail from status column (✓PASS vs ✗FAIL)
            pass_count = len(re.findall(r'✓PASS', full_output))
            fail_count = len(re.findall(r'✗FAIL', full_output))
            total_samples = pass_count + fail_count
            
            if total_samples > 0:
                failure_rate = fail_count / total_samples
                matches = [(None, None, str(fail_count))]  # Dummy matches to trigger comparison logic
        
        # Format 3: Extract from STATISTICS section if available
        if (not matches or len(matches) == 0) and "Pass rate:" in full_output:
            pass_rate_match = re.search(r"Pass rate:\s+([\d.]+)%", full_output)
            if pass_rate_match:
                pass_rate = float(pass_rate_match.group(1))
                failure_rate = (100.0 - pass_rate) / 100.0
                matches = [(None, None, str(failure_rate))]  # Dummy match
        
        if matches:
            # We have comparison data - use actual pass rate
            if len(matches) == 1 and matches[0][0] is None:
                # From Format 2 or 3 - failure_rate already calculated above
                pass
            else:
                # From Format 1 - calculate from native format using 0.5% threshold
                fail_count = sum(1 for _, _, err in matches if float(err) >= 0.5)
                failure_rate = fail_count / len(matches)
            
            shown = extract_and_display_comparisons(full_output)
            
            # Critical: Extract and check the actual statistics from the output
            # The Pass rate line contains the ground truth
            pass_rate_search = re.search(r"Pass rate:\s+([0-9.]+)%", full_output)
            if pass_rate_search:
                actual_pass_rate = float(pass_rate_search.group(1))
                failure_rate = (100.0 - actual_pass_rate) / 100.0
            
            if failure_rate == 0.0:
                # 100% pass rate - success
                print(colored("[Simulator] Simulation PASSED! (0% failure rate)", "green"))
                return {"sim_report": full_output, "status": "success"}
            else:
                # Some failures detected
                print(colored(f"[Simulator] Simulation FAILED. ({failure_rate*100:.1f}% failure rate)", "red"))
                classification = classify_sv_failure(full_output, specs)
                if classification == "rtl":
                    print(colored("[Simulator] Likely RTL issue detected in SV output.", "red"))
                    return {"sim_report": full_output, "status": "rtl_sim_error"}
                return {"sim_report": full_output, "status": "tb_error"}
        
        # No comparison data - fall back to keyword-based detection
        # BUT: Check for ✗FAIL or explicit FAILED first (higher priority)
        # CRITICAL: If statistics show any errors, it's a fail
        max_error_search = re.search(r"Max error:\s+([0-9.]+)%", full_output)
        if max_error_search:
            max_error = float(max_error_search.group(1))
            if max_error > 0.0:
                print(colored(f"[Simulator] Simulation FAILED (max error {max_error:.2f}% detected).", "red"))
                shown = extract_and_display_comparisons(full_output)
                if not shown:
                    print(full_output[-1000:])
                classification = classify_sv_failure(full_output, specs)
                if classification == "rtl":
                    print(colored("[Simulator] Likely RTL issue detected in SV output.", "red"))
                    return {"sim_report": full_output, "status": "rtl_sim_error"}
                return {"sim_report": full_output, "status": "tb_error"}
        
        if "✗FAIL" in full_output or "FAIL" in full_output.upper() and "passed" not in full_output.lower():
            print(colored("[Simulator] Simulation FAILED (detected ✗FAIL or FAILED keyword).", "red"))
            shown = extract_and_display_comparisons(full_output)
            if not shown:
                print(full_output[-1000:])
            classification = classify_sv_failure(full_output, specs)
            if classification == "rtl":
                print(colored("[Simulator] Likely RTL issue detected in SV output.", "red"))
                return {"sim_report": full_output, "status": "rtl_sim_error"}
            return {"sim_report": full_output, "status": "tb_error"}
        elif "TEST PASSED" in full_output or ("PASS" in full_output.upper() and "✗FAIL" not in full_output):
            print(colored("[Simulator] Simulation PASSED!", "green"))
            shown = extract_and_display_comparisons(full_output)
            if not shown and full_output.strip():
                print(full_output[-2000:])
            return {"sim_report": full_output, "status": "success"}
        elif "ERROR" in full_output.upper():
            print(colored("[Simulator] Simulation encountered ERROR.", "red"))
            shown = extract_and_display_comparisons(full_output)
            if not shown:
                print(full_output[-1000:])
            return {"sim_report": full_output, "status": "tb_error"}
        else:
            print(colored("[Simulator] Simulation completed.", "yellow"))
            shown = extract_and_display_comparisons(full_output)
            if not shown:
                print(full_output[-1000:])
            return {"sim_report": full_output, "status": "success"}

    except subprocess.TimeoutExpired:
        print(colored("[Simulator] Simulation timeout (30s limit exceeded).", "red"))
        print(colored("[Simulator] DIAGNOSIS: The simulation entered an infinite loop or is hung.", "yellow"))
        print(colored("[Simulator] Common causes:", "yellow"))
        print(colored("  1. Testbench missing $finish or $stop statement", "yellow"))
        print(colored("  2. Infinite always loop (check for 'always #X clk = ~clk' is present)", "yellow"))
        print(colored("  3. DUT module interface mismatch (wrong signal names or widths)", "yellow"))
        print(colored("  4. Always @(posedge clk) waiting on signal that never transitions", "yellow"))
        print(colored("\n[Simulator] Routing to TB fixer for diagnostic fixes.", "yellow"))
        return {"sim_report": "Timeout after 30s - suspected infinite loop in simulation", "status": "tb_error"}
    except Exception as e:
        print(colored(f"[Simulator] Error: {e}", "red"))
        return {"sim_report": str(e), "status": "tb_error"}


def simulation_node(state: AgentState):
    """
    Main simulation node - routes to cocotb or iverilog based on verification method
    """
    verif_specs = state["verif_specs"]
    method = verif_specs.get("testbench_settings", {}).get("method", "systemverilog")

    print(colored(f"\n[Simulator] Starting {method} simulation...", "blue"))

    if method.lower() in ["cocotb", "cocotbx"]:
        return run_cocotb_simulation(state)
    else:
        return run_systemverilog_simulation(state)

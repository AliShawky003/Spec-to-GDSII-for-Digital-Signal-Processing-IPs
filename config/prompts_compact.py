"""
COMPACT AI Prompts - Reduced token consumption version
Use this instead of prompts.py to save ~70% tokens
"""

# ===== ARCHITECT AGENT PROMPTS =====

ARCHITECT_SYSTEM_PROMPT = "Concise DSP architect. Output two plans with exact keys. No markdown."

ARCHITECT_USER_PROMPT = """
Inputs:
FILTER_SPECS:
{specs}

(If ``hardware_specifications`` contains ``max_area_um2`` the architect should
consult the area dataset to pick a topology that satisfies the budget.  Dataset
entries may only cover a few tap counts; if the exact tap count is missing the
agent should infer a trend (e.g. linear scaling) to estimate area.)

VERIF_SPECS:
{verif_specs}

OUTPUT FORMAT (plain text, no markdown):
DESIGN_PLAN
STATUS=OK or STATUS=ERROR:<field>
MODULE=<name>
FILTER_CLASS=<FIR/IIR>
FILTER_TYPE=<lowpass/etc>
STRUCTURE=<direct_form/etc>
TOPOLOGY=<direct_form|symmetric_direct_form|transposed_direct_form|folded|biquad_df2t|biquad_df1|cascaded>FOLDING=YES/NO
SYMMETRIC=YES/NO
TAPS=<int>
ORDER=<int>
COEFF_FLOAT=[...]
COEFF_WIDTH=<int>
COEFF_FRAC_BITS=<int>
COEFF_FIXED=[signed decimal integers, e.g. -3,-3,-2,0,5,...179,...,5,0,-2,-3,-3]
DATA_WIDTH=<int>
ACC_WIDTH=<int>
CLOCK_MHZ=<int>
ACCUM_TYPE=<registered|combinational>
LATENCY=<int>
LATENCY_RULE=<text>
RESET=<reset_type>
INTERFACE=clk:<name>;rst_n:<name>;sample_in:<name>;sample_valid:<name>;result_out:<name>;result_valid:<name>
VALID_TIMING=result_valid = sample_valid delayed by LATENCY cycles
OUTPUT_SCALE=result_out = (accum >>> COEFF_FRAC_BITS) then truncate to DATA_WIDTH
IF FOLDING=YES, also output:
FOLD_FACTOR=<int>  (number of taps processed per output cycle, equals TAPS for full folding)
THROUGHPUT=1 sample every FOLD_FACTOR cycles
IF FILTER_CLASS=IIR, also output:
SECTIONS=<int>
FORWARD_COEFFS_FIXED=[b0,b1,...bM]
FEEDBACK_COEFFS_FIXED=[a1,a2,...aN]
FEEDBACK_SIGN_CONVENTION=<A_positive_subtract|B_signed_add>
STATE_VAR_WIDTH=<int>
SATURATION=YES
FORWARD_FRAC_BITS=<int>
FEEDBACK_FRAC_BITS=<int>

VERIFICATION_PLAN
STATUS=OK or STATUS=ERROR:<field>
TB_LANGUAGE=<systemverilog|cocotb>
SIMULATOR=<iverilog|verilator|...>
TESTBENCH_FILE=<path or base name>
RTL_MODULE=<name>
VECTOR_COUNT=<int>
VECTOR_COUNT_SPEC=<int or NA>
INPUT_PATTERNS=[...]
TAPS=<int>
ORDER=<int>
GOLDEN_MODEL=SHADOW_MODEL (Bit-Accurate Integer Math)
TOLERANCE_PERCENT=0.0
DATA_SCALE=2**(DATA_WIDTH-1)
CLOCK_PERIOD_NS=10
RESET_SEQ=assert reset low for 2 cycles
PRINT_FORMAT=Sample X: RTL=Y Golden=Z Error=E%
MISMATCH_POLICY=fail if any mismatch (Exact Match Required)
LATENCY=<int>   ← MUST be identical to DESIGN_PLAN LATENCY
DATA_WIDTH=<int>
ACC_WIDTH=<int>
COEFF_WIDTH=<int>
COEFF_FRAC_BITS=<int>
COEFF_FIXED=[signed decimal integers, verbatim from DESIGN_PLAN]
STRUCTURE=<direct_form/etc>
TOPOLOGY=<direct_form|symmetric_direct_form|transposed_direct_form|folded|...>FOLDING=YES/NO
SYMMETRIC=YES/NO
INTERFACE=clk:<name>;rst_n:<name>;sample_in:<name>;sample_valid:<name>;result_out:<name>;result_valid:<name>
VALID_TIMING=result_valid = sample_valid delayed by LATENCY cycles

IF FILTER_CLASS=IIR:
FORWARD_COEFFS_FIXED=[...]
FEEDBACK_COEFFS_FIXED=[...]
FEEDBACK_SIGN_CONVENTION=<A_positive_subtract|B_signed_add>
FORWARD_FRAC_BITS=<int>
FEEDBACK_FRAC_BITS=<int>

ARCHITECT RULES:
1. COEFF_FIXED GOLDEN SOURCE:
   - Express ALL coefficients as SIGNED DECIMAL INTEGERS only.
   - WRONG: [12'sb111111110101, ...] binary literals are ambiguous across agents.
   - RIGHT:  [-11, -10, -10, -2, 0, 5, 12, ...] decimal integers.
   - Both DESIGN_PLAN and VERIFICATION_PLAN must carry identical COEFF_FIXED lists.
   - Never let downstream agents re-derive or re-quantize coefficients independently.

2. LATENCY CALCULATION (YOU MUST COMPUTE THIS — SHOW YOUR WORKING):

   You are responsible for computing the correct LATENCY value based on TOPOLOGY.
   Show the arithmetic in the LATENCY_RULE field. Never leave it as a formula name.

   ── direct_form ──────────────────────────────────────────
   Pipeline: data_reg(1) + accum_comb(0) + pipe_depth + result_out(1)
   PIPE_DEPTH = ceil(log2(TAPS))
   LATENCY = 2 + ceil(log2(TAPS))
   Example: TAPS=33 → LATENCY = 2 + ceil(log2(33)) = 2 + 6 = 8
   LATENCY_RULE = "2 + ceil(log2(33)) = 8 (direct_form)"

   ── symmetric_direct_form ────────────────────────────────
   Pipeline: data_reg(1) + accum_comb(0) + pipe_depth + result_out(1)
   UNIQUE_TAPS = (TAPS+1)/2 for odd TAPS, TAPS/2 for even
   PIPE_DEPTH = ceil(log2(UNIQUE_TAPS))
   LATENCY = 2 + ceil(log2(UNIQUE_TAPS))
   Example: TAPS=41, UNIQUE=21 → LATENCY = 2 + ceil(log2(21)) = 2 + 5 = 7
   LATENCY_RULE = "2 + ceil(log2(21)) = 7 (symmetric_direct_form)"

   ── transposed_direct_form ───────────────────────────────
    Pipeline: data_reg(1) + s_array_regs(1) + result_out(1) = 3
    LATENCY = 3  (or 2 if ACCUM_TYPE=combinational removes result_out)
    NO pipe_depth scaling. The latency DOES NOT scale with TAPS.
    NEVER use TAPS + 1 — that is fundamentally wrong for transposed form.
    Example: TAPS=8 → LATENCY = 3
    LATENCY_RULE = "3 (transposed_direct_form fixed latency)"

    ── folded ───────────────────────────────────────────────
   Pipeline: data_reg(1) + FOLD_FACTOR MAC cycles + result_out(1)
   FOLD_FACTOR = TAPS (full folding: one multiplier reused TAPS times)
   LATENCY = TAPS + 2  (measured in clock cycles from first input to first output)
   THROUGHPUT = 1 output every TAPS cycles (not 1 per cycle)
   Example: TAPS=8 → LATENCY = 10, one output every 8 cycles
   LATENCY_RULE = "folded: TAPS+2 = 8+2 = 10, throughput=1/8"

   NOTE: LATENCY here means cycles from sample_in to result_out.
   The valid signal fires once every FOLD_FACTOR cycles, not every cycle.
   The TB must account for this — only VECTOR_COUNT/FOLD_FACTOR outputs are produced.
   

   ── biquad_df2t / biquad_df1 (IIR) ──────────────────────
   LATENCY = 1  (single output register, state updates combinationally)
   LATENCY_RULE = "IIR single section = 1"

   ── cascaded IIR ─────────────────────────────────────────
   LATENCY = SECTIONS * 1 = SECTIONS
   LATENCY_RULE = "cascaded: SECTIONS * 1 = N"

   MANDATORY SELF-CHECK BEFORE OUTPUTTING:
   [ ] transposed topology → LATENCY == 3 exactly?
   [ ] symmetric topology  → LATENCY == 2 + ceil(log2((TAPS+1)/2))?
   [ ] direct topology     → LATENCY == 2 + ceil(log2(TAPS))?
   [ ] IIR single section  → LATENCY == 1?
   [ ] VERIFICATION_PLAN LATENCY copied verbatim from DESIGN_PLAN?
   [ ] LATENCY_RULE shows actual arithmetic, not just the formula name?
   [ ] folded topology → LATENCY == TAPS + 2? FOLD_FACTOR == TAPS?
   If ANY box fails → fix before outputting.

   ACCUM_TYPE field:
   ACCUM_TYPE=registered   → base latency = data_reg(1) + accum(1) + result_out(1) = 3
   ACCUM_TYPE=combinational → base latency = data_reg(1) + result_out(1) = 2
   transposed_direct_form  → do NOT set BASE_LATENCY at all, LATENCY = TAPS+1

3. IIR TOPOLOGY SELECTION:
   - biquad_df2t is preferred for fixed-point (minimum state, low sensitivity).
   - biquad_df1 when separate input/output history is needed.
   - Cascaded sections for order > 2.
   - Always specify FEEDBACK_SIGN_CONVENTION so RTL and TB use the same polarity.

4. TOPOLOGY REGISTRY - set correct fields per class:
   FIR symmetric_direct_form:
     Multiplier count = (TAPS+1)/2 for odd TAPS, TAPS/2 for even.
     Store only unique coefficients [0..CENTER].

   FIR transposed_direct_form:
    LATENCY = 3 (HARD RULE — fixed latency, never TAPS+1)
    ACCUM_TYPE = registered
    s[k] registers ARE the delay line — TAPS of them, all always_ff.
    For TAPS=4: LATENCY=3. For TAPS=16: LATENCY=3.
    
    FIR folded:
     FOLD_FACTOR = TAPS (one multiplier, one accumulator, reused TAPS times per output)
     LATENCY = TAPS + 2 (HARD RULE)
     ACCUM_TYPE = registered
     THROUGHPUT = 1 output per FOLD_FACTOR cycles — TB must expect fewer outputs.
     ACC_WIDTH = DATA_WIDTH + COEFF_WIDTH + ceil(log2(TAPS)) + 1
     Area savings: ~TAPSx fewer multipliers vs direct_form.
     Use when area budget is tight and throughput reduction is acceptable.

   IIR any:
     SATURATION=YES always.
     FORWARD_FRAC_BITS and FEEDBACK_FRAC_BITS may differ - state both.

5. ACC_WIDTH SIZING RULE - MUST be set by architect, not guessed:
   ACC_WIDTH = DATA_WIDTH + COEFF_WIDTH + ceil(log2(N_multipliers)) + 1 (sign margin)

   N_multipliers by topology:
     direct_form:          N = TAPS
     symmetric_direct_form: N = (TAPS+1)/2  ← pre-add doubles input range, already accounted
     transposed_direct_form: N = TAPS
     folded: N = 1 (single multiplier, but accumulates TAPS times)
            ACC_WIDTH = DATA_WIDTH + COEFF_WIDTH + ceil(log2(TAPS)) + 1

   Examples:
     33-tap symmetric, DATA=16, COEFF=12: 16+12+ceil(log2(17))+1 = 34 bits
     41-tap symmetric, DATA=16, COEFF=12: 16+12+ceil(log2(21))+1 = 34 bits
     65-tap direct,    DATA=16, COEFF=12: 16+12+ceil(log2(65))+1 = 36 bits

   ACC_WIDTH must be IDENTICAL in DESIGN_PLAN, VERIFICATION_PLAN, RTL, and TB.
   A mismatch between TB and RTL ACC_WIDTH causes silent golden arithmetic errors.     
"""

# ===== RTL DESIGNER AGENT PROMPTS =====

RTL_DESIGNER_SYSTEM_PROMPT = "Expert SystemVerilog engineer. Output ONLY code, no explanations."

IVERILOG_CONSTRAINTS = """
1. MODULE STRUCTURE:
   - Module name MUST exactly match the filename.
   - Must end with a newline at EOF.
   - All parameters MUST have explicit default values (e.g., `parameter W=16;`).

2. VARIABLE SCOPE & ITERATORS (FATAL ERROR PREVENTION):
   - ALL variables and loop iterators (`integer i, k;`) MUST be declared at the top MODULE SCOPE.
   - STRICT ENFORCEMENT: You must declare DISTINCT variables for parallel processes.
   - Example: `integer i;` for sequential shift registers AND `integer k;` for combinatorial math.
   - NEVER reuse the same integer variable across different `always` blocks (prevents MULTIDRIVEN).

3. BIT EXTRACTION & SLICING (FATAL ERROR PREVENTION):
   - Icarus/Verilator STRICTLY PROHIBIT inline part-selects `(val >>> FRAC)[15:0]` AND bit-selects inside always blocks.
   - WRONG: `always_ff ... out <= accum[15:0];`
   - WRONG: `assign out = (val >>> FRAC)[15:0];`
   - RIGHT: `assign shifted = val >>> FRAC; assign temp = shifted[15:0]; always_ff ... out <= temp;`

4. ASSIGNMENTS & BLOCKS:
   - `always_ff` uses `<=` ONLY. `always_comb` uses `=` ONLY. Mixing causes X/Z outputs.
   - Reset ALL registers explicitly in the `if (!rst_n)` block, EXCEPT sequential arrays which must use genvar generate loops or Verilator lint pragmas (See FIR RTL Rule 10).
   - NO `x` or `z` initializations — use explicit values (e.g., `'0`).
   - Replicate widths MUST be a constant localparam (e.g., `localparam EXT = A-B; {EXT{1'b0}}`).

5. ARRAYS & CONSTANTS:
   - NO `localparam` arrays. Icarus requires `logic signed` arrays initialized inside an `initial` block.
  - WRONG: `localparam logic [7:0] c[0:1] = '{{1, 2}};`
   - RIGHT: `logic [7:0] c[0:1]; initial begin c[0]=1; c[1]=2; end`

6. LINTING & TIMING:
   - LATENCY = count `always_ff` stages only. `always_comb` = 0 cycles.
   - Unused signals: Add `/* verilator lint_off UNUSEDSIGNAL */` before declaration or use all bits.
   - Width matching: Use explicit intermediate signals for all truncations/expansions.
"""

VERILATOR_CONSTRAINTS = """
CRITICAL for Verilator:
1. Module name MUST match filename.
2. All parameters need values.
3. VARIABLE SCOPE (FATAL ERROR PREVENTION): ALL `integer` loop variables MUST be declared at the top module scope. 
   STRICT ENFORCEMENT: You must declare DISTINCT variables for parallel processes. 
   Example: `integer i;` for the shift register AND `integer k;` for the filter math. 
   NEVER reuse the same integer variable across different `always` blocks.
4. NO INLINE PART-SELECTS (FATAL ERROR PREVENTION): You CANNOT extract bits from an evaluated expression in parentheses. 
   WRONG: `assign out = (val >>> FRAC)[15:0];` -> SYNTAX ERROR unexpected '['
   RIGHT: `assign shifted = val >>> FRAC; assign out = shifted[15:0];`
5. UNUSED SIGNALS: add `/* verilator lint_off UNUSEDSIGNAL */` or use all bits.
6. Width matching: explicit truncation with intermediate signals only.
7. Newline at EOF.
8. Reset ALL registers in reset block.
9. NO x or z initialization - use explicit values.
10. LATENCY = always_ff stages only.
11. DO NOT USE THE SAME VARIABLE IN IN LOOPS IN MULTIPLE ALWAYS BLOCKS - causes MULTIDRIVEN errors.# Add to IVERILOG_CONSTRAINTS and VERILATOR_CONSTRAINTS:
12. ACCUMULATOR SATURATION: NEVER use raw bit-slicing (e.g., acc_out[15:0]) to reduce the width of an accumulator. You MUST implement saturation logic (check sign bits, clamp to max/min signed values) to prevent wrapping.
"""

FIR_RTL_DESIGNER_USER_PROMPT = """
Write SystemVerilog module:

Use ONLY this DESIGN_PLAN:
{plan}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1: LATENCY CALCULATION (CRITICAL PIPELINE MATH)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LATENCY in the plan is the TOTAL always_ff register stage count input→output.
always_comb stages count as ZERO.

HARD RULE FOR BASE LATENCY:
When building FIR filters, you MUST use `pipe_reg[0]` to capture the combinatorial accumulator output. Because `pipe_reg[0]` acts as the accumulator register, it is counted as part of PIPE_DEPTH. 

Therefore, the only registers OUTSIDE of the pipeline array are the input and output registers. 
You MUST define your latency parameters EXACTLY like this:

  localparam BASE_LATENCY = 2; // data_reg(1) + result_out(1)
  localparam PIPE_DEPTH = LATENCY - BASE_LATENCY;

WRONG: localparam BASE_LATENCY = 3; // Hallucinating an accum_reg that is actually pipe_reg[0]
RIGHT: localparam BASE_LATENCY = 2; // Accurately leaves the accumulation capture to pipe_reg[0]

PIPELINE OUTPUT SOURCE RULE:
  PIPE_DEPTH == 0: assign scaled_acc = accum >>> COEFF_FRAC_BITS
  PIPE_DEPTH >= 1: assign scaled_acc = pipe_reg[PIPE_DEPTH-1] >>> COEFF_FRAC_BITS
  NEVER read `accum` directly when PIPE_DEPTH > 0 - silently cuts latency.

SELF-CHECK: Count every always_ff stage input→output. Does data_reg(1) + pipe_reg(PIPE_DEPTH) + result_out(1) == LATENCY exactly?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2: TOPOLOGY-DRIVEN IMPLEMENTATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read STRUCTURE and TOPOLOGY from DESIGN_PLAN. Implement EXACTLY that topology.
TOPOLOGY OVERRIDE RULE:
TOPOLOGY field is the SOLE authority on which architecture to implement.
SYMMETRIC=YES means the coefficients happen to be symmetric — it does NOT
mean use symmetric_direct_form unless TOPOLOGY=symmetric_direct_form.

  TOPOLOGY=direct_form        → implement direct_form MAC, no pre-adders
                                 even if SYMMETRIC=YES
  TOPOLOGY=transposed_direct_form → implement transposed form, no pre-adders
                                 even if SYMMETRIC=YES
  TOPOLOGY=folded             → implement folded counter/MAC architecture
                                 even if SYMMETRIC=YES
  TOPOLOGY=symmetric → implement pre-adder symmetric form
                                 ONLY when this exact value is present

NEVER let SYMMETRIC=YES override TOPOLOGY. TOPOLOGY wins unconditionally.

──────────────────────────────────────────────
FIR: direct_form
──────────────────────────────────────────────
TOPOLOGY=direct_form means one multiplier per tap, all taps in parallel.
No pre-adders. No sum_pre. No CENTER_TAP. No symmetric pairing.
This applies even if SYMMETRIC=YES — TOPOLOGY wins unconditionally.

CORRECT combinational MAC (MANDATORY WIDTH CASTING & CONSTANT COEFFICIENTS):
  always_comb begin : mac_logic
      integer k; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
      reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod; // Exact 30-bit product width
      accum = '0;
      for (k = 0; k < TAPS; k = k + 1) begin
          // Use get_coeff(k) to force Yosys to see constants!
          prod = data_reg[k] * get_coeff(k); // Fast 16x14 multiply
          accum = accum + prod;           // 37-bit accumulation
      end
  end

WRONG — pre-adder is symmetric_direct_form, NOT direct_form:
  sum_pre[k] = data_reg[k] + data_reg[TAPS-1-k];  // ← FORBIDDEN in direct_form
  mult[k] = sum_pre[k] * coeffs[k];               // ← FORBIDDEN in direct_form

COEFFICIENT STORAGE: all TAPS coefficients, indexed 0..TAPS-1.
Do NOT store only half the coefficients even if they are symmetric.
Do NOT declare CENTER_TAP, sum_pre, or mult arrays for direct_form.

DATA SHIFT REGISTER — MUST be gated by sample_valid:
  always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) begin
          data_reg[0] <= '0; data_reg[1] <= '0; ... // unrolled
      end else if (sample_valid) begin
          data_reg[0] <= sample_in;
          data_reg[1] <= data_reg[0];
          ...
      end
  end

SELF-CHECK for direct_form:
  [ ] No sum_pre signal declared?
  [ ] No CENTER_TAP localparam declared?
  [ ] No pre-adder (data_reg[k] + data_reg[TAPS-1-k]) anywhere?
  [ ] coeffs array sized [0:TAPS-1] (all taps, not just half)?
  [ ] MAC loops over ALL taps: for(k=0; k<TAPS; k++)?
  [ ] data_reg shift gated by sample_valid?

──────────────────────────────────────────────
FIR: symmetric (SYMMETRIC=YES)
──────────────────────────────────────────────
  MANDATORY: Pre-add symmetric tap pairs — halves multiplier count.

  SIGNAL DECLARATIONS — MUST be at MODULE SCOPE, never inside always_comb:

  ODD TAPS (e.g. TAPS=17, CENTER_TAP=8):
    localparam CENTER_TAP = TAPS/2;  // = 8
    logic signed [DATA_WIDTH:0]  sum_pre [0:CENTER_TAP-1];  // 8 entries
    logic signed [ACC_WIDTH-1:0] mult    [0:CENTER_TAP];    // 9 entries (0..8 incl. center)

  EVEN TAPS (e.g. TAPS=16, CENTER_TAP=8):
    localparam CENTER_TAP = TAPS/2;  // = 8
    logic signed [DATA_WIDTH:0]  sum_pre [0:CENTER_TAP-1];  // 8 entries
    logic signed [ACC_WIDTH-1:0] mult    [0:CENTER_TAP-1];  // 8 entries (NO center tap)

  WRONG — declaring arrays inside always_comb:
    always_comb begin
      logic signed [DATA_WIDTH:0] sum_pre [...];  // ← ILLEGAL, causes silent width errors
      logic signed [ACC_WIDTH-1:0] mult [...];    // ← ILLEGAL
    end

  ALL combinational logic MUST be in a SINGLE always_comb block
  to prevent MULTIDRIVEN on iterator k:

        ODD TAPS always_comb (center tap exists, MANDATORY WIDTH CASTING & CONSTANT COEFFICIENTS):
  always_comb begin : mac_logic
      integer k; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
      reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod; // Exact 30-bit product width
      for (k = 0; k < CENTER_TAP; k = k + 1)
          sum_pre[k] = data_reg[k] + data_reg[TAPS-1-k];
      for (k = 0; k < CENTER_TAP; k = k + 1) begin
          prod = sum_pre[k] * get_coeff(k); // Use get_coeff(k) for constant!
          mult[k] = prod;
      end
      prod = data_reg[CENTER_TAP] * get_coeff(CENTER_TAP); // center tap only for ODD
      mult[CENTER_TAP] = prod;
      accum = '0;
      for (k = 0; k <= CENTER_TAP; k = k + 1)
          accum = accum + mult[k];
  end

        EVEN TAPS always_comb (NO center tap, MANDATORY WIDTH CASTING & CONSTANT COEFFICIENTS):
  always_comb begin : mac_logic
      integer k; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
      reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod; // Exact 30-bit product width
      for (k = 0; k < CENTER_TAP; k = k + 1)
          sum_pre[k] = data_reg[k] + data_reg[TAPS-1-k];
      for (k = 0; k < CENTER_TAP; k = k + 1) begin
          prod = sum_pre[k] * get_coeff(k); // Use get_coeff(k) for constant!
          mult[k] = prod;
      end
      // NO mult[CENTER_TAP] line — even TAPS has no center tap
      accum = '0;
      for (k = 0; k < CENTER_TAP; k = k + 1)  // strictly < not <=
          accum = accum + mult[k];
  end

  ⚠️ NEVER split these into separate always_comb blocks —
     causes MULTIDRIVEN on iterator variable k.
  ⚠️ NEVER add mult[CENTER_TAP] for even TAPS — doubles the contribution
     of data_reg[CENTER_TAP] and produces wrong output from sample TAPS/2 onward.

  Multiplier count: (TAPS+1)/2 for odd TAPS, TAPS/2 for even.
  Store only unique coefficients [0..CENTER_TAP-1] for even, [0..CENTER_TAP] for odd.

  DEAD SIGNAL CHECK:
    Verify always_comb writes to sum_pre (not sum_sym or any other name).
    A correctly-sized but unused signal is a failed fix, not a working fix.

──────────────────────────────────────────────
FIR: transposed
──────────────────────────────────────────────
LATENCY OVERRIDE (CRITICAL FOR TRANSPOSED FORM):
The general BASE_LATENCY=2 rule DOES NOT APPLY to transposed form.
Transposed form has exactly 3 register stages: data_in_reg(1) + s[k](1) + result_out(1).
You MUST define parameters EXACTLY like this for transposed form, IGNORING the LATENCY value from the plan:
  localparam LATENCY      = 3; // FORCE LATENCY to 3 for transposed!
  localparam BASE_LATENCY = 3;
  localparam PIPE_DEPTH   = 0; // ALWAYS 0 for transposed
NEVER instantiate a `pipe_reg` array for transposed form. 
Adding a pipe_reg adds a 4th cycle, breaking the result_valid alignment.
Using a LATENCY value > 3 without adding pipe_reg will cause the valid_sr to be too long, delaying result_valid and breaking the TB checker.

The inter-stage delays s[k] MUST be always_ff registers.
This is the most common transposed form bug — putting s[k] in always_comb
makes the filter a scalar multiplier, not a FIR.

INPUT REGISTER RULE (CRITICAL):
Transposed form broadcasts the input to ALL multipliers in parallel.
Therefore, it ONLY needs a SINGLE input register, NOT a shift register array.
WRONG: logic signed [DATA_WIDTH-1:0] data_reg [0:TAPS-1]; // Wastes logic
RIGHT: (* max_fanout = 16 *) logic signed [DATA_WIDTH-1:0] data_in_reg; // Single register, buffered for ASIC timing
MANDATORY: The module port list MUST declare rst_n with the max_fanout attribute to prevent -55ns recovery violations:
  input wire clk,
  (* max_fanout = 50 *) input wire rst_n,
CORRECT signal flow — every s[k] is always_ff (MANDATORY GENERATE BLOCK):
   // CRITICAL: s array MUST be ACC_WIDTH to prevent overflow during accumulation.
   // DATA_WIDTH + COEFF_WIDTH only holds the product, NOT the sum of products.
   logic signed [ACC_WIDTH-1:0] s [0:TAPS-1];

   // CRITICAL: MUST use a generate block and the get_coeff() function.
   // This forces Yosys to see the coefficients as constants, resulting in
   // small, fast constant-multipliers instead of massive variable multipliers.
      genvar g;
   generate
     for (g = 0; g < TAPS; g = g + 1) begin : gen_tap
       wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod; // Exact 30-bit product width
       
       // Constant-coefficient multiplication
       assign prod = data_in_reg * get_coeff(g);
       
      // Reset normally. The (* max_fanout = 50 *) on the port fixes ASIC timing.
       always_ff @(posedge clk or negedge rst_n) begin
         if (!rst_n) begin
           s[g] <= '0;
         end else begin
           if (g == TAPS-1) begin
             s[g] <= prod;
           end else begin
             s[g] <= prod + s[g+1];
           end
         end
       end

CORRECT OUTPUT LOGIC (NO PIPE_REG):
  // scaled_acc MUST come directly from s[0], NOT from a pipe_reg!
  assign scaled_acc = s[0] >>> COEFF_FRAC_BITS;

VALID SIGNAL PIPELINE FOR TRANSPOSED (LATENCY=3):
Because LATENCY is forced to 3, you MUST use exactly this 3-stage valid pipeline to match the data path exactly:
  logic [1:0] valid_sr;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) valid_sr <= '0;
    else        valid_sr <= {{valid_sr[0], sample_valid}};
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) result_valid <= 1'b0;
    else        result_valid <= valid_sr[1];
  end

SELF-CHECK for transposed form:
 [ ] BASE_LATENCY == 3 and PIPE_DEPTH == 0?
 [ ] NO pipe_reg array instantiated?
 [ ] scaled_acc sourced directly from s[0]?
 [ ] s array driven by a generate block using the get_coeff(g) function?
 [ ] s[g] always_ff block omits `negedge rst_n` and `if (!rst_n)` to prevent high-fanout reset timing violations?
 [ ] Multiplier output (prod) is exactly DATA_WIDTH+COEFF_WIDTH bits?
 [ ] s array width is ACC_WIDTH (NOT DATA_WIDTH + COEFF_WIDTH)?
 [ ] Only ONE input register declared (data_in_reg), NOT an array?
 [ ] No always_comb block computing s[k]?
 [ ] LATENCY == 3?
 [ ] NO ACC_WIDTH casts inside the multiplication? (Causes massive ASIC multipliers)

──────────────────────────────────────────────
FIR: folded
──────────────────────────────────────────────
One multiplier and one accumulator, reused TAPS times per output sample.
A counter sequences through all TAPS coefficients before asserting result_valid.
Throughput = 1 output every TAPS cycles. LATENCY = TAPS + 2.

SIGNALS REQUIRED:
  logic signed [DATA_WIDTH-1:0]  data_reg  [0:TAPS-1];  // shift register
  logic signed [ACC_WIDTH-1:0]   accum_reg;              // running accumulator
  logic [$clog2(TAPS)-1:0]       tap_cnt;                // 0..TAPS-1 counter
  logic                          mac_valid;              // high when tap_cnt wraps
  logic                          ready;                  // high when accepting new samples
  logic signed [ACC_WIDTH-1:0]   scaled_acc;
  logic signed [DATA_WIDTH-1:0]  result_temp;

CORRECT structure:
  // Handshake: ready when idle
  assign ready = (tap_cnt == 0);

    // Stage 1: data shift register — advances only when ready and sample_valid
  always_ff @(posedge clk or negedge rst_n) begin : shift_logic
    integer i; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
    if (!rst_n) begin
      /* verilator lint_off BLKLOOPINIT */
      for (i = 0; i < TAPS; i = i + 1) data_reg[i] <= '0;
      /* verilator lint_on BLKLOOPINIT */
    end else if (sample_valid && ready) begin
      data_reg[0] <= sample_in;
      for (i = 1; i < TAPS; i = i + 1) data_reg[i] <= data_reg[i-1];
    end
  end

  // Stage 2: tap counter — starts on valid sample, free-runs until done
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tap_cnt <= '0;
    end else if (sample_valid && ready) begin
      tap_cnt <= 1; // Start computation
    end else if (tap_cnt != 0) begin
      if (tap_cnt == TAPS-1) tap_cnt <= '0;
      else                   tap_cnt <= tap_cnt + 1;
    end
  end

    // Stage 3: MAC — multiply-accumulate one tap per cycle (MANDATORY WIDTH CASTING)
  // CRITICAL: Accumulator MUST be gated by (sample_valid && ready) to prevent 
  // corruption when idle. Do NOT load accum_reg if sample_valid is low.
  always_ff @(posedge clk or negedge rst_n) begin : mac_logic
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod; // Exact 30-bit product width
    if (!rst_n) begin
      accum_reg <= '0;
      mac_valid <= 1'b0;
    end else begin
      mac_valid <= (tap_cnt == TAPS-1);  // pulses when last tap computed
      if (tap_cnt == 0 && sample_valid) begin
        prod = sample_in * coeffs[0];             // first tap: use sample_in directly
        accum_reg <= prod;
      end else if (tap_cnt != 0) begin
        prod = data_reg[tap_cnt] * coeffs[tap_cnt]; // accumulate
        accum_reg <= accum_reg + prod;
      end
    end
  end

  // Output scaling and SATURATION (MANDATORY)
  // NEVER use raw bit-slicing or parameterized part-selects on an accumulator. 
  // Icarus Verilog does not support parameterized part-selects in always_comb.
  // Use mathematical comparison instead.
  localparam signed [ACC_WIDTH-1:0] SAT_MAX = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [ACC_WIDTH-1:0] SAT_MIN = -(1 << (DATA_WIDTH-1));

  assign scaled_acc = accum_reg >>> COEFF_FRAC_BITS;
  
    assign scaled_acc = accum_reg >>> COEFF_FRAC_BITS;
  
  // CRITICAL ICARUS FIX: Use continuous assign for bit-slicing to prevent crashes.
  wire sat_pos_bit = ~scaled_acc[ACC_WIDTH-1] & (|scaled_acc[ACC_WIDTH-2:DATA_WIDTH-1]);
  wire sat_neg_bit = scaled_acc[ACC_WIDTH-1] & (~(&scaled_acc[ACC_WIDTH-2:DATA_WIDTH-1]));
  wire [DATA_WIDTH-1:0] scaled_acc_dw = scaled_acc[DATA_WIDTH-1:0];
  localparam signed [DATA_WIDTH-1:0] SAT_MAX_DW = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [DATA_WIDTH-1:0] SAT_MIN_DW = -(1 << (DATA_WIDTH-1));

  always @(*) begin
    if (sat_pos_bit)
      result_temp = SAT_MAX_DW;
    else if (sat_neg_bit)
      result_temp = SAT_MIN_DW;
    else
      result_temp = scaled_acc_dw;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) result_out <= '0;
    else if (mac_valid) result_out <= result_temp;
  end

  // Valid: result_valid pulses one cycle after mac_valid
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) result_valid <= 1'b0;
    else        result_valid <= mac_valid;
  end

CRITICAL RULES:
  [ ] Output a `ready` signal assigned to (tap_cnt == 0) AND add it to module ports.
  [ ] data_reg shifts ONLY when sample_valid && ready.
  [ ] tap_cnt starts at 1 when sample_valid && ready, free-runs to TAPS-1 then back to 0.
  [ ] accum_reg LOADS (not adds) on tap_cnt == 0 ONLY IF sample_valid is HIGH.
      If sample_valid is low and tap_cnt is 0, accum_reg must hold its value (prevent idle corruption).
  [ ] NON-BLOCKING HAZARD: At tap_cnt==0, MAC MUST use sample_in (NOT data_reg[0]).
  [ ] mac_valid pulses when tap_cnt == TAPS-1.
  [ ] MAC uses ACC_WIDTH'(signed'(...)) casts.
  [ ] Output MUST use mathematical saturation logic, NOT raw bit-slicing.

SELF-CHECK for folded:
  [ ] LATENCY == TAPS + 2?
  [ ] tap_cnt wraps at TAPS-1 → 0?
  [ ] data_reg shift gated by (sample_valid && ready)?
  [ ] accum_reg clears (not accumulates) when tap_cnt == 0?
  [ ] MAC at tap_cnt==0 uses sample_in directly, NOT data_reg[0]?
      (non-blocking hazard: data_reg[0] is stale on the same edge it's written)
  [ ] result_valid fires once every TAPS cycles, not every cycle?
  [ ] No pipe_reg array declared?
  [ ] ready signal declared and added to module port list?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3: COEFFICIENT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COEFF_FIXED values from the plan are ALREADY fixed-point integers:
  coeff_int = round(coeff_float * 2^COEFF_FRAC_BITS)

Use AS-IS. NEVER multiply by 2^FRAC_BITS again.
  WRONG: coeffs[k] = COEFF_FIXED[k] * (2**COEFF_FRAC_BITS)  ← 2048x too large
  RIGHT: coeffs[k] = COEFF_FIXED[k]

CRITICAL: You are strictly forbidden from writing 12'sb... binary literals. You MUST use signed decimal format (e.g., -12'sd3 or simply -3).

ICARUS VERILOG ARRAY CONSTRAINT & SYNTHESIS CONSTANT RULE (CRITICAL):
Icarus Verilog does NOT support `localparam` arrays. Furthermore, declaring `logic` arrays 
initialized in `initial` blocks causes Yosys to synthesize variable flip-flops instead of 
constant multipliers, destroying ASIC timing and area.
To satisfy both Icarus and Yosys, you MUST declare coefficients inside a `function` using 
a `case` statement, and unroll the filter taps using a `generate` block.

EXCEPTION FOR FOLDED TOPOLOGY:
Because Folded topology uses a variable counter (`tap_cnt`) to index coefficients, you CANNOT 
use the `get_coeff()` function (Yosys would build a massive multiplexer). 
For Folded topology ONLY, you MUST declare coefficients as a `logic signed` array initialized 
in an `initial` block. Yosys will correctly infer this as a ROM block.
  logic signed [COEFF_WIDTH-1:0] coeffs [0:TAPS-1];
  initial begin coeffs[0] = ...; end

  
MANDATORY COEFFICIENT FUNCTION PATTERN:
  function signed [COEFF_WIDTH-1:0] get_coeff;
    input integer index;
    begin
      case (index)
        0: get_coeff = 14'sd6;   // Use COEFF_WIDTH'sdVALUE format
        1: get_coeff = 14'sd7;
        // ... all coefficients ...
        default: get_coeff = 0;
      endcase
    end
  endfunction

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4: GENERATE BLOCKS FOR CONDITIONAL PIPELINE STAGES & SATURATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEVER use runtime if(PARAM > N) inside always_ff to guard array accesses.
Verilator checks array bounds statically - causes SELRANGE even if never taken.

GENERATE BLOCK SCOPING RULE (FATAL ERROR PREVENTION):
Any signal referenced outside a generate block MUST be declared at MODULE scope.
Signals declared inside generate blocks are invisible outside them.
Hierarchical paths like gen_pipe.result_temp do NOT work in Verilator.

WRONG — signal declared inside generate, referenced outside:
  generate
    if (PIPE_DEPTH > 0) begin : gen_pipe
      logic signed [ACC_WIDTH-1:0] pipe_reg [...]; // ← scoped to gen_pipe
      assign scaled_acc = pipe_reg[...];           // ← invisible outside
    end
  endgenerate
  always_ff ... result_out <= gen_pipe.result_temp; // ← FATAL ERROR

RIGHT — declare at module scope, drive from inside generate:
  // Module scope
  logic signed [ACC_WIDTH-1:0] scaled_acc;
  logic signed [DATA_WIDTH-1:0] result_temp;

  generate
    if (PIPE_DEPTH > 0) begin : gen_pipe
      logic signed [ACC_WIDTH-1:0] pipe_reg [0:PIPE_DEPTH-1];

        always_ff @(posedge clk or negedge rst_n) begin : pipe_logic
        integer i; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
        if (!rst_n) begin
          pipe_reg[0] <= '0;
          // For PIPE_DEPTH > 1: unroll manually or use genvar loop.
          // NEVER use: for(i=0; i<PIPE_DEPTH; i++) pipe_reg[i] <= '0;
          // That causes Verilator BLKLOOPINIT fatal error.
        end else begin
          pipe_reg[0] <= accum;
          for (i = 1; i < PIPE_DEPTH; i = i + 1) pipe_reg[i] <= pipe_reg[i-1];
        end
      end

      assign scaled_acc  = pipe_reg[PIPE_DEPTH-1] >>> COEFF_FRAC_BITS;

    end else begin : gen_no_pipe

      assign scaled_acc  = accum >>> COEFF_FRAC_BITS;

    end
  endgenerate

    // SATURATION LOGIC (MANDATORY FOR ALL FIR TOPOLOGIES)
  // CRITICAL ICARUS FIX: Icarus Verilog CRASHES if you use parameterized part-selects 
  // OR variable indexes (like scaled_acc[i]) inside always_comb or always @*.
  // You MUST use continuous assign (wire) for ALL bit-slicing and reduction operations.
  localparam signed [DATA_WIDTH-1:0] SAT_MAX_DW = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [DATA_WIDTH-1:0] SAT_MIN_DW = -(1 << (DATA_WIDTH-1));
    logic signed [DATA_WIDTH-1:0] result_temp; // ← Declared as logic!
    logic signed [ACC_WIDTH-1:0] result_temp_ext;

  wire sat_pos_bit = ~scaled_acc[ACC_WIDTH-1] & (|scaled_acc[ACC_WIDTH-2:DATA_WIDTH-1]);
  wire sat_neg_bit = scaled_acc[ACC_WIDTH-1] & (~(&scaled_acc[ACC_WIDTH-2:DATA_WIDTH-1]));
  wire [DATA_WIDTH-1:0] scaled_acc_dw = scaled_acc[DATA_WIDTH-1:0];

  always @(*) begin
    if (sat_pos_bit)
      result_temp = SAT_MAX_DW;
    else if (sat_neg_bit)
      result_temp = SAT_MIN_DW;
    else
      result_temp = scaled_acc_dw;
  end

  // Output register uses module-scope result_temp directly — no hierarchical path
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) result_out <= '0;
    else        result_out <= result_temp;
  end

RULES:
- pipe_reg may be declared inside gen_pipe (it is only used inside that block)
- scaled_acc and result_temp MUST be at module scope (used in always_ff outside generate)
- Use if/else generate, never two separate if generates driving the same signal
- PIPE_DEPTH == 0 case MUST be the else branch — not a second separate generate if
- Output MUST use mathematical saturation logic, NOT raw bit-slicing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5: VALID SIGNAL PIPELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IF TOPOLOGY=folded — DO NOT use valid_pipe. Use this instead:

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) result_valid <= 1'b0;
    else        result_valid <= mac_valid;
  end

  WHY: sample_valid pulses for only 1 cycle. A long shift register fires
  result_valid based on time elapsed since sample_valid, not based on
  whether the MAC has actually finished. mac_valid is the only correct
  source — it fires exactly when accum_reg holds the complete sum.
  Do NOT declare valid_pipe at all for folded topology.

FOR ALL OTHER TOPOLOGIES (EXCEPT TRANSPOSED) — result_valid must be delayed exactly LATENCY cycles.
  CRITICAL ICARUS BUG PREVENTION: Icarus Verilog CRASHES if you use parameterized expressions 
  like `valid_sr[LATENCY-2]` or `valid_sr[LATENCY-3:0]` inside `always_ff` or `always_comb`.
  You MUST use explicit hardcoded indexes based on the LATENCY value!

  IF LATENCY == 2:
    logic [0:0] valid_sr;
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) valid_sr <= '0;
      else        valid_sr <= sample_valid;
    end
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) result_valid <= 1'b0;
      else        result_valid <= valid_sr[0];
    end

  IF LATENCY == 3:
    logic [1:0] valid_sr;
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) valid_sr <= '0;
      else        valid_sr <= {{valid_sr[0], sample_valid}};
    end
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) result_valid <= 1'b0;
      else        result_valid <= valid_sr[1];
    end

  IF LATENCY == 4:
    logic [2:0] valid_sr;
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) valid_sr <= '0;
      else        valid_sr <= {{valid_sr[1:0], sample_valid}};
    end
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) result_valid <= 1'b0;
      else        result_valid <= valid_sr[2];
    end

  IF LATENCY >= 5:
    logic [LATENCY-2:0] valid_sr;
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) valid_sr <= '0;
      else        valid_sr <= {{valid_sr[LATENCY-3:0], sample_valid}};
    end
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n) result_valid <= 1'b0;
      else        result_valid <= valid_sr[LATENCY-2];
    end

valid_pipe MUST be ungated after valid_pipe[0].
Do NOT gate intermediate valid_pipe stages with sample_valid.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6: CRITICAL DESIGN RULES (ALL TOPOLOGIES)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Module name MUST match DESIGN_PLAN MODULE field exactly.
2. ALL signals declared at MODULE SCOPE - never inside always blocks.
3. BIT EXTRACTION via assign BEFORE always_ff - never inline:
     assign shifted    = wide_sig >>> FRAC_BITS;
     assign result_temp = shifted[DATA_WIDTH-1:0];
     always_ff ... result_out <= result_temp;
4. always_ff uses <= ONLY. always_comb uses = ONLY. Never mix.
5. LOOPS IN ALWAYS_FF:
   Loops are ALLOWED in the clocked (else) branch of always_ff (e.g., for shift registers).
   Loops are FORBIDDEN in the reset (if !rst_n) branch — see Rule 10 for Verilator BLKLOOPINIT.
   Do not unroll shift register assignments; use: for (i = 1; i < TAPS; i = i + 1) data_reg[i] <= data_reg[i-1];
6. No multiple drivers: never assign a DUT output wire from TB side.
7. UNCONDITIONAL DATA PIPELINES: Do NOT gate intermediate data registers (like accum or pipe_reg) with sample_valid. Data must shift through the pipeline unconditionally. Only data_reg[0] and valid_pipe[0] should care about the sample_valid input.
8. UNIQUE ITERATORS (FATAL MULTIDRIVEN PREVENTION FOR SYNTHESIS):
SystemVerilog processes run in PARALLEL. If you use a single module-scope integer (e.g., `integer i;`) in multiple `always_ff` or `always_comb` blocks, Yosys/LibreLane will throw a MULTIDRIVEN error.
FIX: You MUST declare loop iterators locally INSIDE the `always_ff` or `always_comb` block.
Example: `always_ff ... begin : block_name integer i; ... end`
NEVER declare `integer i;` or `integer k;` at module scope.9. NON-BLOCKING READ-BEFORE-WRITE HAZARD (FOLDED TOPOLOGY):
   When two always_ff blocks execute on the same clock edge, non-blocking
   assignments (<=) write AFTER all reads. If Block A writes signal X and
   Block B reads signal X on the same edge, Block B sees the OLD value.
   
   In folded FIR: the shift register writes data_reg[0] <= sample_in, but
   the MAC reads data_reg[0] on the same edge → gets stale value.
   FIX: MAC must use sample_in directly for tap 0, bypassing data_reg[0].
   This is the #1 most common folded FIR bug.
10. ARRAY RESET LOOP PROHIBITION & INITIALIZATION (VERILATOR FATAL / SYNTHESIS):
    NEVER use a for loop with <= inside an always_ff reset branch to reset a sequential array.
    Verilator rejects this with BLKLOOPINIT fatal error. 
    NEVER use `initial` blocks for sequential state registers (like data_reg) — they are ignored by synthesis and fail internal resets.
    `initial` blocks are ONLY allowed for ROM arrays (like `coeffs`).
    EXCEPTION: If an always_ff block intentionally omits negedge rst_n(like the transposed s[g] array) to avoid high-fanout reset timing violations, you MUST use aninitialblock to initialize the array to 0 for simulation.

    RIGHT option A — genvar generate loop (SYNTHESIS & VERILATOR SAFE):
      genvar g;
      generate
        for (g = 0; g < TAPS; g = g + 1) begin : gen_arr_reset
          always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) data_reg[g] <= '0;
            else        data_reg[g] <= next_value[g]; // clocked logic here
          end
        end
      endgenerate

    RIGHT option B — Verilator Lint Pragma (if genvar is too complex for the specific block):
            always_ff @(posedge clk or negedge rst_n) begin : reset_block
        integer i; // LOCAL ITERATOR (prevents Yosys/LibreLane MULTIDRIVEN)
        if (!rst_n) begin
          /* verilator lint_off BLKLOOPINIT */
          for (i = 0; i < TAPS; i = i + 1) data_reg[i] <= '0;
          /* verilator lint_on BLKLOOPINIT */
        end else begin
          // clocked logic here
        end
      end

    The for loop in the ELSE (clocked) branch is FINE and allowed by Verilator.
    Only the reset (if !rst_n) branch requires special handling.
11. MULTIPLICATION WIDTH BLOAT PREVENTION (CRITICAL ASIC TIMING RULE):
    In SystemVerilog, `accum = accum + (a * b)` can silently truncate the product.
    DO NOT cast operands to ACC_WIDTH before multiplication — this forces the synthesizer to build a massive, slow 37x37 multiplier!
    RIGHT — assign to an intermediate product variable sized exactly to DATA_WIDTH + COEFF_WIDTH:
      reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod;
      prod = data_reg[k] * coeffs[k]; // 16x14 multiply -> 30-bit result
      accum = accum + prod;           // 37-bit accumulation
      
    ALWAYS cast multiplication operands to the target accumulator width if the 
    natural product width is smaller than the accumulator.
12. ICARUS PARAMETERIZED BIT-SELECT BUG:
    Icarus Verilog does NOT support parameterized part-selects (e.g., `[MSB:LSB]` where MSB/LSB are localparams) 
    inside `always_comb` or `always @*`. It throws "constant selects in always_* processes are not currently supported".
    To perform saturation, NEVER use parameterized bit slicing like `scaled_acc[ACC_WIDTH-1:DATA_WIDTH-1]`.
    Instead, use mathematical comparison against SAT_MAX and SAT_MIN localparams.

13. ASIC RESET FANOUT TIMING RULE (CRITICAL):
    High-fanout reset trees destroy ASIC timing. Resetting 2400 flip-flops causes -55ns recovery violations.
    To fix this, you MUST add the synthesis attribute `(* max_fanout = 50 *)` to the `rst_n` input port.
    This forces Yosys to build a buffer tree for reset automatically.
    Then, you MUST reset ALL pipeline registers (s, pipe_reg, data_reg) normally using `negedge rst_n`.
    NEVER use `initial` blocks for sequential state registers — they cause cocotb X-propagation crashes. 

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 7: PRE-OUTPUT SELF-CHECK LIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Before outputting, verify ALL boxes:

LATENCY:
[ ] Counted every always_ff stage input→output. Total == LATENCY?
[ ] IF transposed: LATENCY == 3? s[k] driven by always_ff? s[k] is ACC_WIDTH?
[ ] IF direct/symmetric: ACCUM_TYPE matches plan (registered=base 3, combinational=base 2)?
[ ] IF direct/symmetric: scaled_acc sources from pipe_reg[PIPE_DEPTH-1], NOT accum directly?
[ ] IF folded: LATENCY == TAPS + 2? tap_cnt wraps at TAPS-1? accum_reg clears at tap_cnt==0?
[ ] IF folded: MAC uses sample_in (NOT data_reg[0]) when tap_cnt==0? (non-blocking hazard)
[ ] IF folded: result_valid <= mac_valid (single FF)? No valid_pipe declared?
[ ] IF folded: ready signal is in the port list?

SYMMETRIC FIR:
[ ] sum_pre declared at MODULE SCOPE (never inside always_comb)?
[ ] mult declared at MODULE SCOPE (never inside always_comb)?
[ ] sum_pre declared as [DATA_WIDTH:0] — 17 bits for 16-bit data?
[ ] always_comb writes to sum_pre (not a differently-named signal)?
[ ] EVEN TAPS: mult sized [0:CENTER_TAP-1]? NO mult[CENTER_TAP] line? accumulate with k < CENTER_TAP?
[ ] ODD TAPS: mult sized [0:CENTER_TAP]? mult[CENTER_TAP] line present? accumulate with k <= CENTER_TAP?
[ ] Multiplier count == TAPS/2 for even TAPS, (TAPS+1)/2 for odd TAPS?
[ ] MAC uses ACC_WIDTH'(signed'(...)) casts on all multiplier inputs?

DIRECT FORM SPECIFIC:
[ ] IF TOPOLOGY=direct_form: No sum_pre, no CENTER_TAP, no pre-adder?
[ ] IF TOPOLOGY=direct_form: coeffs[0:TAPS-1] stores all TAPS coefficients?
[ ] IF TOPOLOGY=direct_form: MAC iterates over all TAPS (not TAPS/2)?
[ ] IF TOPOLOGY=direct_form: MAC uses ACC_WIDTH'(signed'(...)) casts?

COEFFICIENTS:
[ ] Coefficients copied AS-IS from COEFF_FIXED (not re-scaled)?
[ ] Coefficients declared as `logic signed` arrays, NOT `localparam` arrays (Icarus limitation)?

GENERAL:
[ ] valid_pipe depth == LATENCY-1 (before final output register)?
[ ] generate blocks used for all param-conditional pipeline stages?
[ ] No blocking = inside any always_ff?
[ ] All signals declared at module scope?
[ ] Bit extraction via assign intermediate only?
[ ] No multiple drivers on any signal?
[ ] Write the parameters in Decimal form not Binary form, did you do this?
[ ] scaled_acc and result_temp declared at module scope (not inside generate)?
[ ] generate uses if/else not two separate if blocks when driving shared signals?
[ ] No hierarchical paths (gen_X.signal_name) anywhere in output register or assigns?
[ ] No for loop with <= inside always_ff reset branch (if !rst_n)?
    Use genvar generate or verilator lint pragma instead — Verilator BLKLOOPINIT fatal.
[ ] Saturation uses mathematical comparison (> SAT_MAX / < SAT_MIN), NOT parameterized bit-slicing?
[ ] No module-scope `integer i;` or `integer k;`? (Iterators MUST be declared locally inside their always block to prevent synthesis MULTIDRIVEN errors)
[ ] IF transposed: `s[g]` always_ff block omits `negedge rst_n` to prevent massive reset buffer tree timing violations?

If ANY box is unchecked → fix before outputting.

{iverilog_constraints}

{verilator_constraints}

OUTPUT: Code only.
"""


IIR_RTL_DESIGNER_USER_PROMPT = """
Write SystemVerilog module:

Use ONLY this DESIGN_PLAN:
{plan}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1: IIR STRUCTURE AND LATENCY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALL IIR topologies have LATENCY = 1.

Pipeline:
  sample_in → Combinational MAC → always_ff output register → result_out
               (zero cycles)       (one cycle)

DO NOT declare: BASE_LATENCY, PIPE_DEPTH, pipe_reg, valid_pipe, data_reg.
DO NOT register sample_in before the MAC — this makes LATENCY=2.

result_valid: single register, mirrors sample_valid with 1-cycle delay.
Every always_ff MUST have an else branch that clears result_valid:
  end else begin
    result_valid <= 1'b0;
  end
Without this, result_valid stays HIGH during idle and the TB checker
pops stale outputs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2: FORBIDDEN PATTERNS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. NEVER upshift the input:
   WRONG: w_new = (sample_in << COEFF_FRAC_BITS) - ...
   RIGHT: w_new = sample_in - ((A1*w1)>>>COEFF_FRAC_BITS) - ((A2*w2)>>>COEFF_FRAC_BITS);

2. NEVER shift the sum once at the end:
   WRONG: y = (B0*w + B1*w1 + B2*w2) >>> COEFF_FRAC_BITS;
   RIGHT: y = ((B0*w)>>>COEFF_FRAC_BITS) + ((B1*w1)>>>COEFF_FRAC_BITS) + ((B2*w2)>>>COEFF_FRAC_BITS);

3. NEVER wrap sample_in in a sign-extension concat:
   WRONG: assign w_new = {{16{{sample_in[DATA_WIDTH-1]}}, sample_in}} - ...;
   RIGHT: assign w_new = sample_in - ...;
   Reason: {{...}} is UNSIGNED — forces expression unsigned — >>> becomes
   logical >> on negative values — garbage output.

4. NEVER declare truncated wires as unsigned:
   WRONG: wire [DATA_WIDTH-1:0] y_sat;
   RIGHT: wire signed [DATA_WIDTH-1:0] y_sat;
   Any wire feeding a signed register MUST be declared signed.

5. NEVER use FIR pipeline scaffolding (BASE_LATENCY, pipe_reg, valid_pipe).

6. Declare combinational wires in dependency order — w_new before b0_prod:
   WRONG: wire signed [ACC_WIDTH-1:0] b0_prod = B0 * w_new;  // w_new not yet declared
          wire signed [ACC_WIDTH-1:0] w_new = sample_in - ...;
   RIGHT: wire signed [ACC_WIDTH-1:0] w_new = sample_in - ...;
          wire signed [ACC_WIDTH-1:0] b0_prod = B0 * w_new;

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3: TOPOLOGY IMPLEMENTATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

── biquad_df2t (ORDER=2, single section) ────────────────────
State: s0, s1 at ACC_WIDTH UNSATURATED. Compute y_out FIRST.

  wire signed [ACC_WIDTH-1:0] y_out, s0_next, s1_next;
  
    // CRITICAL: Use exact product width wires to prevent multiplier truncation!
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b0_prod = B0 * sample_in;
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b1_prod = B1 * sample_in;
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b2_prod = B2 * sample_in;
  
  // MANDATORY: In DF2T, feedback coefficients (A1, A2) MUST multiply the OUTPUT (y_out).
  // NEVER multiply A1/A2 by the state variables (s0, s1)! That breaks the feedback loop!
  wire signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a1_prod = A1 * y_out;
  wire signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a2_prod = A2 * y_out;

  assign y_out   = (b0_prod >>> COEFF_FRAC_BITS) + s0;
  assign s0_next = (b1_prod >>> COEFF_FRAC_BITS) - (a1_prod >>> COEFF_FRAC_BITS) + s1;
  assign s1_next = (b2_prod >>> COEFF_FRAC_BITS) - (a2_prod >>> COEFF_FRAC_BITS);

  // SATURATION LOGIC (Icarus-safe, ASIC-fast XOR tree)
  localparam signed [DATA_WIDTH-1:0] SAT_MAX_DW = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [DATA_WIDTH-1:0] SAT_MIN_DW = -(1 << (DATA_WIDTH-1));
  logic signed [DATA_WIDTH-1:0] y_sat;
  logic signed [ACC_WIDTH-1:0] y_sat_ext;

  always @(*) begin
    y_sat = y_out;                  // Implicit truncate to DATA_WIDTH
    y_sat_ext = y_sat;              // Sign-extend back to ACC_WIDTH

    if (y_out != y_sat_ext) begin
      if (y_out[ACC_WIDTH-1] == 1'b0) y_sat = SAT_MAX_DW;
      else                            y_sat = SAT_MIN_DW;
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      s0 <= 0; 
      s1 <= 0; 
      result_out <= 0; 
      result_valid <= 1'b0;
    end else if (sample_valid) begin
      s0 <= s0_next; 
      s1 <= s1_next; 
      result_out <= y_sat; 
      result_valid <= 1'b1;
    end else begin
      result_valid <= 1'b0;
    end
  end

── biquad_df1 (ORDER=2, single section) ─────────────────────
State: x_d1, x_d2 at DATA_WIDTH; y_d1, y_d2 at ACC_WIDTH UNSATURATED.

  wire signed [ACC_WIDTH-1:0] y_out;
  
  // CRITICAL: Use exact product width wires to prevent multiplier truncation!
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b0_prod = B0 * sample_in;
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b1_prod = B1 * x_d1;
  wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b2_prod = B2 * x_d2;
  wire signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a1_prod = A1 * y_d1;
  wire signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a2_prod = A2 * y_d2;

  assign y_out = (b0_prod >>> COEFF_FRAC_BITS)
               + (b1_prod >>> COEFF_FRAC_BITS)
               + (b2_prod >>> COEFF_FRAC_BITS)
               - (a1_prod >>> COEFF_FRAC_BITS)
               - (a2_prod >>> COEFF_FRAC_BITS);

  // SATURATION LOGIC (Icarus-safe, ASIC-fast XOR tree)
  localparam signed [DATA_WIDTH-1:0] SAT_MAX_DW = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [DATA_WIDTH-1:0] SAT_MIN_DW = -(1 << (DATA_WIDTH-1));
  logic signed [DATA_WIDTH-1:0] y_sat;
  logic signed [ACC_WIDTH-1:0] y_sat_ext;

  always @(*) begin
    y_sat = y_out;                  // Implicit truncate to DATA_WIDTH
    y_sat_ext = y_sat;              // Sign-extend back to ACC_WIDTH

    if (y_out != y_sat_ext) begin
      if (y_out[ACC_WIDTH-1] == 1'b0) y_sat = SAT_MAX_DW;
      else                            y_sat = SAT_MIN_DW;
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      x_d1 <= 0; x_d2 <= 0; y_d1 <= 0; y_d2 <= 0;
      result_out <= 0; result_valid <= 1'b0;
    end else if (sample_valid) begin
      x_d2 <= x_d1; x_d1 <= sample_in;
      y_d2 <= y_d1; y_d1 <= y_out;    // UNSATURATED — full ACC_WIDTH
      result_out <= y_sat; result_valid <= 1'b1;
    end else begin
      result_valid <= 1'b0;
    end
  end

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4: COEFFICIENT AND WIDTH RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- COEFF_FIXED values are already scaled integers. Use AS-IS. Never re-scale.
- Declare with signed decimal: 14'sd123, -14'sd5957. Never binary literals.
- Never negate or take absolute value of a coefficient.
- All intermediate signals at ACC_WIDTH. Never slice signed intermediate.

- CRITICAL MULTIPLIER WIDTH RULE: NEVER cast both operands of a multiplication to ACC_WIDTH!
  In SystemVerilog, `ACC_WIDTH'(a) * ACC_WIDTH'(b)` evaluates the multiplication at ACC_WIDTH bits, silently truncating the upper bits of the product.
  This causes catastrophic overflow in IIR feedback loops and destroys the frequency response.
  RIGHT: Let Verilog evaluate at the natural max width using an exact-width intermediate wire:
    wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] b0_prod = B0 * sample_in; 
    wire signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a1_prod = A1 * y_out;
  WRONG: wire signed [ACC_WIDTH-1:0] b0_prod = ACC_WIDTH'(B0) * ACC_WIDTH'(sample_in);

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5: GENERAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Module name MUST match DESIGN_PLAN MODULE field exactly.
2. All signals declared at MODULE SCOPE.
3. always_ff uses <= ONLY. Combinational uses assign or always_comb with =.
4. No loops inside always_ff — unroll explicitly.
5. All MAC in combinational logic, never in always_ff.
6. State update order: older state first (w2<=w1 before w1<=w_sat).
7. Port names MUST match DESIGN_PLAN INTERFACE field exactly.
8. UNIQUE ITERATORS: NEVER declare `integer i;` or `integer k;` at module scope. If you use a for loop, declare the iterator locally inside the `always` block to prevent Yosys/LibreLane MULTIDRIVEN errors.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6: PRE-OUTPUT CHECKLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] LATENCY=1? One always_ff stage only?
[ ] No data_reg, pipe_reg, valid_pipe, BASE_LATENCY, PIPE_DEPTH?
[ ] sample_in feeds MAC directly (no input register)?
[ ] result_valid <= sample_valid (else branch clears it)?
[ ] All truncated wires declared signed?
[ ] Combinational wires in dependency order (w_new before b0_prod)?
[ ] Each product shifted >>> COEFF_FRAC_BITS individually before summing?
[ ] sample_in never upshifted or wrapped in concat?
[ ] State update order correct (older first)?
[ ] All IIR states (s0, s1, y_d1, y_d2) store UNSATURATED ACC_WIDTH values?
[ ] Multiplier products use exact-width intermediate wires (NO ACC_WIDTH casts on both operands)?
[ ] Saturation uses implicit truncate/sign-extend XOR tree (NO > or < comparators, NO parameterized bit-slicing)?
[ ] Coefficients AS-IS in signed decimal format?
[ ] Module name matches plan?
[ ] Port names match plan INTERFACE field?
If ANY box unchecked → fix before outputting.

{iverilog_constraints}
{verilator_constraints}

OUTPUT: Code only.
"""



RTL_ERROR_FEEDBACK_TEMPLATE = """
COMPILATION FAILED:
{error_feedback}

Previous code:
{current_code}

Common Verilator fixes:
- UNUSEDPARAM: Use ALL parameters in port/signal declarations (e.g., input [DATA_WIDTH-1:0])
- UNUSEDSIGNAL: Add `/* verilator lint_off UNUSEDSIGNAL */` before signal declaration
- WIDTHTRUNC/WIDTHEXPAND: Use intermediate signal for ALL width changes
- Module parameters: Move to #(parameter ...) after module name

FIX AND RETURN CODE ONLY.
"""

ERROR_PATTERNS = """
Verilator errors:
- "UNUSEDPARAM": Use parameter in declarations: `input [DATA_WIDTH-1:0]`
- "UNUSEDSIGNAL": Add `/* verilator lint_off UNUSEDSIGNAL */` directive
- "WIDTHTRUNC": Assign to temp signal first, then extract bits
- "WIDTHEXPAND": Make sure operand widths match before operations
"""

# ===== RTL FIXER AGENT PROMPTS =====

RTL_FIXER_SYSTEM_PROMPT = "Expert SystemVerilog debugger. Fix code errors with MINIMAL changes."

RTL_FIXER_USER_PROMPT = """
Fix the following RTL compilation errors:

ERROR OUTPUT:
{error_feedback}

CURRENT CODE:
{current_code}

CONSTRAINTS:
{constraints}

INSTRUCTIONS:
1. Analyze the error messages carefully.
2. Make MINIMAL changes to fix errors.
3. DO NOT rewrite entire module - only fix problem areas.
4. Preserve all functionality and logic.
5. Output ONLY the corrected code.

Common fixes:
- UNUSEDPARAM: Use parameters in port/signal widths.
- UNUSEDSIGNAL: Add lint directive or use all bits.
- WIDTHTRUNC/EXPAND: Add intermediate signals.
- Module name: Must match filename.
- SELRANGE: Move conditional pipeline stages into generate blocks.

CODE ONLY, NO EXPLANATIONS.
"""

# ===== TB FIXER AGENT PROMPTS =====

TB_FIXER_SYSTEM_PROMPT = "You are a Python code generator. Output ONLY valid Python code. NO explanations, NO comments, NO text before or after code. Start with imports. End with EOF. Every line must be valid Python."

TB_FIXER_USER_PROMPT = """
ERROR: {error_feedback}

TESTBENCH: {current_tb}

FIXES NEEDED:
1. If error ~50%: Change OUTPUT_SCALE_FACTOR to match fixed-point shift.
2. If error ~100%: Check rtl_value = dut.output_data.value.signed_integer (NOT .integer).
3. If "buffer remaining": Ensure latency_buffer loops until empty after stimulus done.

OUTPUT PYTHON CODE ONLY - NO TEXT BEFORE OR AFTER CODE.
"""

SV_TB_FIXER_SYSTEM_PROMPT = "Expert SystemVerilog testbench debugger. Output ONLY SystemVerilog code. NO explanations."

SV_TB_FIXER_USER_PROMPT = """
Fix the SystemVerilog testbench errors.

ERROR OUTPUT:
{error_feedback}

CURRENT TESTBENCH:
{current_tb}

CONSTRAINTS:
{iverilog_constraints}

INSTRUCTIONS:
1. Make minimal changes to fix syntax/semantic errors.
2. Keep the same DUT port mapping and testbench behavior.
3. Use only SystemVerilog supported by Icarus Verilog.
4. Remove any DUT module definitions or placeholder RTL.
5. Preserve per-sample comparison prints: "Sample X: RTL=Y Golden=Z Error=E%"
6. If "$signed must be a vector": replace $signed(real) with $rtoi(real).
7. If simulation hangs: remove wait(result_valid), use bounded flush loop.
8. If timeout counters exist: replace with fixed flush + missing-outputs check.
9. Ensure latency compensation and absolute error printing are present.
10. Checker MUST be in a separate always @(posedge clk) block - never embedded
    inside the stimulus for-loop.
11. Output ONLY corrected SystemVerilog code.
"""

# ===== COCOTB TESTBENCH PROMPTS =====

COCOTB_SYSTEM_PROMPT = "Expert in cocotb verification. Output ONLY Python code."

COCOTB_REQUIREMENTS = """
KEY REQUIREMENTS:
1. IMPORTS (CRITICAL - ALL REQUIRED):
   - import cocotb
   - from cocotb.clock import Clock
   - from cocotb.triggers import RisingEdge, Timer
   - from collections import deque
   - from scipy.signal import lfilter
   - import random
   - import math
   - Do NOT import cocotb.result - it does not exist in modern cocotb.
   - Do NOT import numpy. Use scipy.signal.lfilter ONLY.
2. ERROR HANDLING:
   - Use `assert False, "error message"` for failures.
3. LATENCY:
   - Define `LATENCY = {pipeline_latency}` as a Python constant.
   - Do NOT read dut.LATENCY.
4. Use concurrent coroutines:
   - stimulus_gen(): drives inputs, queues expected results.
   - output_checker(): monitors outputs, compares with queue.
   - Run both with cocotb.start_soon().
5. Clock: `Clock(dut.clk, 10, unit="ns")` (NOT units).
6. Golden model: scipy.signal.lfilter(coeffs, [1], inputs) — plain list.
7. Drain: `while not done or queue: await RisingEdge(dut.clk)`

NEVER: numpy, np.array(), convolve, cocotb.result.
"""

COCOTB_USER_PROMPT = """
Write cocotb testbench for FIR filter using scipy.signal.lfilter ONLY (NO numpy):

Use ONLY this VERIFICATION_PLAN:
{plan}

{cocotb_requirements}

CRITICAL FIX 1 — PIPELINE DRAINING:
After stimulus finishes, output_checker MUST drain all remaining outputs.
Set done=True after stimulus, checker loops until queue empty.

CRITICAL FIX 2 — SIGNED CONVERSION:
  CORRECT: rtl_val = dut.output_data.value.signed_integer
  WRONG:   dut.output_data.value.integer
  Scale:   rtl_float = rtl_val / (2**(DATA_WIDTH-1))
  If error ~50%: use 2**DATA_WIDTH instead.

CRITICAL FIX 3 — VECTOR COUNT:
100-200 vectors only. Timeout < 180 seconds.

CODE PATTERN:
```python
@cocotb.test()
async def test_fir_filter(dut):
    # STEP 1: Generate test vectors
    test_vectors = [...]

    # STEP 2: Compute golden outputs BEFORE stimulus
    golden_outputs = signal.lfilter(COEFFS, [1], test_vectors)

    # STEP 3: Create latency buffer and flag
    latency_buffer = deque()
    done_flag = False

    # STEP 4: Start coroutines
    cocotb.start_soon(stimulus_gen(dut, test_vectors, golden_outputs,
                                   latency_buffer))
    await output_checker(dut, latency_buffer, done_flag)
```

DEBUG PRINT per sample:
print(f"Sample {{n}}: RTL={{rtl:.6f}} Golden={{g:.6f}} Error={{e:.4f}}%")

Code only.
"""

COCOTB_ERROR_FEEDBACK = """
FAILED: {error_feedback}

Previous: {current_tb}

Fix and return code only.
"""

# ===== SYSTEMVERILOG TESTBENCH PROMPTS =====

SV_TB_SYSTEM_PROMPT = "Expert SV Verification Engineer. Output ONLY raw SystemVerilog code with NO language markers or preambles."

FIR_SV_TB_USER_PROMPT = """
Write a BIT-ACCURATE, SELF-CHECKING SystemVerilog testbench using Icarus Verilog.

Use ONLY this VERIFICATION_PLAN:
{plan}

CONSTRAINTS:
{iverilog_constraints}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRATEGY: EXACT MATCH SHADOW MODEL (NO FLOATING POINT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Implement a shadow model in INTEGER ARITHMETIC that exactly mirrors
the hardware truncation rules. No reals, no tolerance.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1: CLOCK-EDGE SYNCHRONIZATION (PREVENTS 1-SAMPLE SHIFT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The shadow model MUST be computed AFTER @(posedge clk) that captures the input.

MANDATORY STIMULUS LOOP ORDER:
  1. Drive sample_in and sample_valid    ← BEFORE clock edge
  2. @(posedge clk)                      ← RTL captures HERE
  3. golden_val = calculate_golden(...)  ← AFTER edge
  4. expected_queue.push_back(golden_val)← AFTER edge

WRONG (1-sample shift):
  golden_val = calculate_golden(random_val);  // before edge!
  @(posedge clk);

RIGHT:
  @(posedge clk);
  golden_val = calculate_golden(random_val);  // after edge

ACCUM_TYPE SELF-VERIFICATION RULE:
The BASE_LATENCY comment MUST match the actual always block used for accum.

WRONG — mismatched comment and implementation:
  localparam BASE_LATENCY = 3;  // data_reg(1) + accum_reg(1) + result_out(1)
  always_comb begin              // ← but accum is combinational!
    accum = SUM(mult[...]);
  end

RIGHT:
  // If accum is always_comb:
  localparam BASE_LATENCY = 2;  // data_reg(1) + [accum=comb, 0 cycles] + result_out(1)

  // If accum is always_ff:
  localparam BASE_LATENCY = 3;  // data_reg(1) + accum_reg(1) + result_out(1)

Self-check: grep for "always_comb" and "always_ff" on the accum signal.
If accum appears in always_comb → BASE_LATENCY MUST be 2.
If accum appears in always_ff  → BASE_LATENCY MUST be 3.
A mismatch shifts result_valid by 1 cycle, causing 100% error on every sample.

RESET SEQUENCE RULE:
After de-asserting reset, drive the first stimulus sample IMMEDIATELY.
Do NOT insert an idle @(posedge clk) between rst_n=1 and the first sample.
An extra idle clock after reset shifts all RTL outputs relative to the
golden queue, causing systematic misalignment on every sample.

WRONG — idle clock after de-assertion:
  rst_n = 0;
  repeat(2) @(posedge clk);
  rst_n = 1;
  @(posedge clk);         // ← idle clock — misaligns golden queue by 1+

WRONG — waiting for negedge that never comes (FATAL HANG):
  @(negedge rst_n);       // ← rst_n starts at 0 and only goes HIGH — negedge never fires
  @(posedge rst_n);       // ← never reached — simulation hangs forever at watchdog
RIGHT:
  rst_n = 0;
  repeat(2) @(posedge clk);
  rst_n = 1;

  // MANDATORY PIPELINE FLUSH:
  // Some RTL topologies (like transposed) omit reset on pipeline registers to save ASIC timing.
  // This leaves them as 'x' in simulation. We MUST drive 100 idle clocks to flush the 'x' out.
  sample_valid = 0;
  sample_in = '0;
  repeat(100) @(posedge clk);

  // stimulus loop starts immediately after flush
  for (i = 0; i < VECTOR_COUNT; i = i+1) begin      random_val   = $random;
      sample_in    = random_val;
      sample_valid = 1;
      @(posedge clk);
      golden_val = calculate_golden(random_val);
      ...
  end

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2: CHECKER ARCHITECTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The output checker MUST be in a SEPARATE always @(posedge clk) block.
NEVER embed the checker inside the stimulus for-loop.

WRONG:
  for (i=0; i<N; i++) begin
    @(posedge clk);
    if (result_valid) rtl = result_out;  // wrong timing
  end

RIGHT:
  // Stimulus initial block — only drives inputs and pushes golden queue
  initial begin
    for (i=0; i<N; i++) begin
      @(posedge clk);
      expected_queue.push_back(calculate_golden(random_val));
    end
  end

  // Checker — independent always block
  // CRITICAL: use a DEDICATED checker_index, never reuse sample_count.
  // sample_count is driven by the stimulus loop and does not track
  // which output the checker is currently processing. Using it produces
  // wrong and duplicate sample numbers in error messages.
  integer checker_index;  // declare at module scope, initialize to 0 in stimulus initial block

MANDATORY QUEUE DECLARATION:
The expected_queue MUST be declared at module scope before any initial or always block.
This is the most common TB compilation failure — the queue is used but never declared.

REQUIRED declaration (add to the module-level variable declarations):
  reg signed [DATA_WIDTH-1:0] expected_queue [$];

The [$] suffix makes it a SystemVerilog dynamic queue (unbounded FIFO).
Without this declaration, push_back(), pop_front(), and size() all fail
with "unknown task" or "no function named" compilation errors.

WRONG — using queue without declaring it:
  // declarations section has no expected_queue
  ...
  expected_queue.push_back(golden_val);  // ← compile error: unknown task

RIGHT — declare first, then use:
  reg signed [DATA_WIDTH-1:0] expected_queue [$];  // ← at module scope
  ...
  expected_queue.push_back(golden_val);  // ← compiles correctly

  always @(posedge clk) begin
    if (result_valid) begin
      rtl_val    = result_out;
      if (expected_queue.size() > 0) begin
        golden_val = expected_queue.pop_front();
        if (rtl_val !== golden_val) begin
          $display("Sample %0d: RTL=%0d Golden=%0d Error=EXACT MISMATCH",
                   checker_index, rtl_val, golden_val);
          error_count = error_count + 1;
        end
        checker_index = checker_index + 1;
      end else begin
        $display("ERROR: Unexpected result_valid (queue empty)");
        error_count = error_count + 1;
      end
    end
  end
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3: SIMULATION TERMINATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEVER use wait(queue.size()==0) as sole termination — hangs if result_valid
never fires due to latency bug.

MANDATORY pattern:
  // Bounded flush after stimulus
  repeat(LATENCY + 10) @(posedge clk);
  #1;  // allow last always @(posedge clk) to settle before $finish

  // Check for lost outputs
  if (expected_queue.size() != 0) begin
    $display("ERROR: %0d outputs never received", expected_queue.size());
    error_count = error_count + expected_queue.size();
  end
  $finish;

ALWAYS include a watchdog:
  initial begin
    // IF folded: 1 sample takes TAPS cycles, so timeout MUST be scaled!
    #(VECTOR_COUNT * TAPS * CLOCK_PERIOD_NS * 10);
    $display("FATAL: Simulation timeout");
    $finish;
  end
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4: SHADOW MODEL — SELECT BY TOPOLOGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL PARAMETER RULE (TAPS vs ORDER):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILTER_ORDER and TAPS are NOT the same. TAPS = ORDER + 1.
The VERIFICATION_PLAN might incorrectly state TAPS = ORDER (e.g., TAPS=64 for a 64th-order filter).
If you set TAPS=64 for an ORDER=64 filter, the golden model will be missing the last tap.

MANDATORY OVERRIDE:
In your testbench parameter declarations, you MUST declare ORDER if present in the plan, 
and derive TAPS dynamically. NEVER blindly copy TAPS from the plan if it equals ORDER.

Use this exact pattern for your parameters:
  parameter ORDER = <value_from_plan>; // e.g., 64
  parameter TAPS  = ORDER + 1;         // ALWAYS ORDER + 1. e.g., 65

IF the plan does not contain ORDER, then use:
  parameter TAPS = <value_from_plan>; (Assuming plan is correct)

SELF-CHECK: Is TAPS equal to ORDER? If yes, you have a bug. TAPS must be ORDER + 1.
IF FIR (any topology):
  // Module-scope history — MUST be initialized to zero
  reg signed [DATA_WIDTH-1:0] history [0:TAPS-1];
  integer h_init;
  initial begin
    for (h_init = 0; h_init < TAPS; h_init = h_init + 1)
      history[h_init] = '0;
  end
  
  // Saturation bounds for TB (Icarus-safe mathematical comparison)
  localparam signed [ACC_WIDTH-1:0] SAT_MAX = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [ACC_WIDTH-1:0] SAT_MIN = -(1 << (DATA_WIDTH-1));

  function signed [DATA_WIDTH-1:0] calculate_golden;
    input signed [DATA_WIDTH-1:0] sample;
    reg signed [ACC_WIDTH-1:0] acc;
    reg signed [ACC_WIDTH-1:0] shifted; // CRITICAL: intermediate for saturation
    integer k;
    begin
      for (k = TAPS-1; k > 0; k = k-1) history[k] = history[k-1];
      history[0] = sample;
      acc = 0;
      for (k = 0; k < TAPS; k = k+1) begin
        // MANDATORY WIDTH CASTING to match RTL truncation behavior
        acc = acc + (ACC_WIDTH'(signed'(history[k])) * ACC_WIDTH'(signed'(coeff[k])));
      end
      
      // SHIFT and SATURATE (MUST match RTL behavior)
      shifted = acc >>> COEFF_FRAC_BITS;
      if (shifted > SAT_MAX)
        calculate_golden = SAT_MAX[DATA_WIDTH-1:0];
      else if (shifted < SAT_MIN)
        calculate_golden = SAT_MIN[DATA_WIDTH-1:0];
      else
        calculate_golden = shifted[DATA_WIDTH-1:0];
    end
  endfunction

FIR MAC PRODUCT WIDTH RULE (CRITICAL):
In Verilog, (A * B) is evaluated at max(width(A), width(B)) — NOT at ACC_WIDTH.
Products are silently truncated before accumulation if not handled correctly.
You MUST cast multiplies to ACC_WIDTH inside the golden model to match the RTL!

INTEGER VARIABLE PROHIBITION IN FUNCTIONS:
NEVER use `integer` as an intermediate for bit-extraction inside functions.
`integer` is a 32-bit type — bit-selecting on it (e.g. prod_int[15:0]) causes
an Icarus Verilog internal crash (vthread.cc assertion failure).

WRONG:
  integer prod_int;              // module scope
  prod_int = acc >>> FRAC_BITS;
  calculate_golden = prod_int[DATA_WIDTH-1:0];  // ← crashes Icarus

RIGHT:
  reg signed [ACC_WIDTH-1:0] shifted;           // inside function, correct width
  shifted = acc >>> FRAC_BITS;
  calculate_golden = shifted[DATA_WIDTH-1:0];   // ← bit-select on reg, safe

All intermediate signals inside functions MUST be declared as reg with
explicit width. Never use integer for anything that requires a bit-select.

ACC_WIDTH sizing rule for FIR:
  ACC_WIDTH >= DATA_WIDTH + COEFF_WIDTH + ceil(log2(N_multipliers))
  Where N_multipliers = TAPS for direct form, (TAPS+1)/2 for symmetric.
  Example: 41-tap symmetric → N=21 → 16+12+5 = 33 bits minimum → use 34.
  This must be set by the ARCHITECT and match in both RTL and TB.

IF FIR symmetric_direct_form (SYMMETRIC=YES):
  // Module-scope history — MUST be initialized to zero
  reg signed [DATA_WIDTH-1:0] history [0:TAPS-1];
  integer h_init;
  initial begin
    for (h_init = 0; h_init < TAPS; h_init = h_init + 1)
      history[h_init] = '0;
  end
  
  // Saturation bounds for TB (Icarus-safe mathematical comparison)
  localparam signed [ACC_WIDTH-1:0] SAT_MAX = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [ACC_WIDTH-1:0] SAT_MIN = -(1 << (DATA_WIDTH-1));

  function signed [DATA_WIDTH-1:0] calculate_golden;
    input signed [DATA_WIDTH-1:0] sample;
    reg signed [ACC_WIDTH-1:0] acc;
    reg signed [ACC_WIDTH-1:0] shifted;
    // CRITICAL FIX: Pre-adder grows width by 1 bit. Prod must be (DATA_WIDTH + 1 + COEFF_WIDTH) bits.
    reg signed [DATA_WIDTH+1+COEFF_WIDTH-1:0] prod; 
    integer k;
    begin
      // Shift history
      for (k = TAPS-1; k > 0; k = k-1) history[k] = history[k-1];
      history[0] = sample;
      acc = 0;
      
      // CRITICAL: Handle symmetric pairs and center tap separately!
      // Integer division TAPS/2 rounds down. 
      for (k = 0; k < TAPS/2; k = k+1) begin
        prod = (history[k] + history[TAPS-1-k]) * coeff[k];
        acc = acc + prod;
      end
      
      // Add center tap manually ONLY for odd number of taps
      if (TAPS % 2 != 0) begin
        prod = history[TAPS/2] * coeff[TAPS/2];
        acc = acc + prod;
      end
      
      // SHIFT and SATURATE (MUST match RTL behavior)
      shifted = acc >>> COEFF_FRAC_BITS;
      if (shifted > SAT_MAX)
        calculate_golden = SAT_MAX[DATA_WIDTH-1:0];
      else if (shifted < SAT_MIN)
        calculate_golden = SAT_MIN[DATA_WIDTH-1:0];
      else
        calculate_golden = shifted[DATA_WIDTH-1:0];
    end
  endfunction  

IF FIR folded (FOLDING=YES):
  Shadow model is IDENTICAL to direct_form — folded computes the same convolution,
  just one tap per cycle instead of all taps in parallel.
  Use the standard FIR shadow model (history shift + full MAC + saturation).
  
  THROUGHPUT RULE — CRITICAL:
  The DUT produces one output every TAPS cycles and exposes a `ready` signal.
  The TB MUST wire the `ready` signal and wait for it before driving the next sample.

  STIMULUS PATTERN for folded:
    for (i = 0; i < VECTOR_COUNT; i = i + 1) begin
      // Synchronously wait until DUT is ready for next sample
      while (dut_ready !== 1) @(posedge clk);
      
      // Drive one sample
      random_val   = $random;
      sample_in    = random_val;
      sample_valid = 1;
      @(posedge clk);
      golden_val = calculate_golden(random_val);
      expected_queue.push_back(golden_val);
      
      // De-assert valid
      sample_valid = 0;
      sample_in    = 0;
    end

  DUT INSTANTIATION for folded MUST include ready mapping:
    wire dut_ready;
    dut #(...) dut_inst (.clk(clk), .rst_n(rst_n), ..., .ready(dut_ready));

SIGN CONVENTION:
  TB MUST match RTL FEEDBACK_SIGN_CONVENTION from VERIFICATION_PLAN exactly.
  Convention A (store positive, subtract): acc - (A1*w1>>>frac)
  Convention B (store signed, add):        acc + (A1*w1>>>frac)
  NEVER mix conventions between RTL and TB.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5: COEFFICIENT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COEFFICIENT ALIGNMENT RULE (PREVENTS INDEX SHIFT):
When initializing coefficient arrays, DO NOT try to align them into neat visual columns. 
Visually misaligning the indices between RTL and TB is the #1 cause of random output mismatches.
Use 1 coefficient per line, or explicitly write the index on every single line.
WRONG (causes index shifting):
  coeff[17] = 1; coeff[18] = 0; coeff[19] = -1; // Shifted by 1 compared to RTL!
RIGHT (explicit indices, 1 per line or grouped safely):
  coeff[17] = 1;
  coeff[18] = 1;
  coeff[19] = 0;

COEFF_FIXED values are ALREADY fixed-point integers. Use AS-IS.
WRONG: coeff[k] = COEFF_FIXED[k] * (2**COEFF_FRAC_BITS)  ← 2048x too large
RIGHT: coeff[k] = COEFF_FIXED[k]

NEVER re-derive or re-quantize in the TB. Copy verbatim from VERIFICATION_PLAN.

COEFFICIENT ARRAY DECLARATION RULE (CRITICAL):
The coeff array MUST be declared as signed with COEFF_WIDTH bits.
ICARUS VERILOG ARRAY CONSTRAINT: Icarus does NOT support `localparam` arrays. 
Declare coefficient arrays as `reg signed` initialized in an `initial` block.

WRONG — unsigned, wrong width, or localparam array:
  reg [DATA_WIDTH-1:0] coeff [0:TAPS-1];   // unsigned + wrong width
  reg [COEFF_WIDTH-1:0] coeff [0:TAPS-1];  // unsigned — negative coeffs corrupted
  localparam logic signed [COEFF_WIDTH-1:0] coeff [0:TAPS-1] = '{{...}}; // Icarus error

RIGHT:
  reg signed [COEFF_WIDTH-1:0] coeff [0:TAPS-1];  // signed, correct width
  initial begin
    coeff[0] = ...; // initialize
  end

WHY: In Verilog, if ANY operand in a multiplication is unsigned, the entire
expression is evaluated unsigned. A coefficient declared as unsigned reg [7:0]
treats -1 as 255, -2 as 254, etc. The golden accumulator produces completely
wrong values for any tap with a negative coefficient. The RTL uses signed
coefficients so RTL and TB compute different results — large random-looking
errors starting at whichever sample first exercises a negative coefficient.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6: PRE-OUTPUT SELF-CHECK LIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON:
[ ] calculate_golden() called AFTER @(posedge clk)?
[ ] expected_queue pushes AFTER @(posedge clk)?
[ ] Checker in separate always @(posedge clk), NOT inside stimulus loop?
[ ] Termination uses bounded repeat(LATENCY+10) + #1 before $finish?
[ ] Watchdog timeout present?
[ ] TB localparams copied verbatim from VERIFICATION_PLAN?
[ ] ACC_WIDTH accumulator used in function (not integer)?
[ ] Arithmetic shift >>> used (not logical >>)?
[ ] No multiple drivers on DUT output wires?
[ ] COEFF_FIXED used AS-IS (not re-scaled by 2^FRAC)?
[ ] DUT instantiated with NO parameter overrides — use only:
      dut dut_inst (.clk(clk), .rst_n(rst_n), ...ports...);
    NEVER pass #(.DATA_WIDTH(...), .ACC_WIDTH(...)) — these may be
    localparams in the RTL and cannot be overridden.
    TB localparams must be defined INDEPENDENTLY and match the plan.
[ ] No integer variables used for bit-extraction inside functions?
    (integer bit-select crashes Icarus — use reg signed [ACC_WIDTH-1:0] instead)
[ ] All function-local intermediates declared as reg with explicit width?
[ ] expected_queue declared at module scope as reg signed [DATA_WIDTH-1:0] expected_queue [$]?
    (missing declaration causes "unknown task push_back" compile error)
[ ] Saturation uses mathematical comparison (> SAT_MAX / < SAT_MIN), NOT parameterized bit-slicing?
[ ] Multiplies inside functions use ACC_WIDTH'(signed'(...)) casts to match RTL truncation?
   
    
IF FIR:
[ ] history[] at module scope (not local to function)?
[ ] history[] initialized to zero in an initial block before simulation starts?
    (logic/reg defaults to X — uninitialized history produces Golden=x on every sample)
[ ] Shift order: k=TAPS-1 downto 1, then history[0]=sample?
[ ] checker_index declared separately from sample_count?
    (sample_count tracks stimulus; checker_index tracks checker pops — never mix them)
[ ] checker_index initialized to 0 in stimulus initial block?
[ ] IF folded: DUT wired with `ready` signal mapped to `dut_ready`?
[ ] IF folded: stimulus synchronously waits with `while (dut_ready !== 1) @(posedge clk);`?
[ ] IF folded: shadow model is standard direct-form (same math, different timing)?
[ ] IF folded: watchdog timeout scaled by TAPS factor?
[ ] IF folded: watchdog timeout = VECTOR_COUNT * TAPS * CLOCK_PERIOD_NS * 2?
[ ] coeff[] declared as reg signed [COEFF_WIDTH-1:0] — both signed AND correct width?
    (unsigned coeff corrupts all negative coefficients — large errors from first negative tap)
[ ] TAPS derived as ORDER + 1? (TAPS must NEVER equal ORDER for FIR filters)    

If ANY box is unchecked → fix before outputting.
⚠️ CRITICAL TIMING RULE: NEVER compute calculate_golden() before @(posedge clk).
If you compute golden before the clock edge, the TB history array shifts 1 cycle 
before the RTL data_reg, causing a 1-cycle latency mismatch that ruins the exact match.
MANDATORY STIMULUS LOOP (COPY THIS EXACTLY — DO NOT REORDER):
// Drive inputs, wait for clock, THEN compute golden. 
// The @(posedge clk) is a "wall". sample_valid must go high BEFORE the wall.
// calculate_golden must happen AFTER the wall. Never cross the streams.
for (i = 0; i < VECTOR_COUNT; i = i + 1) begin
    random_val   = $random;
    sample_in    = random_val;
    sample_valid = 1;

    @(posedge clk);  // ── THE WALL ── RTL captures sample_in HERE

    // Compute golden AFTER the edge. History shifts exactly when RTL data_reg shifts.
    golden_val = calculate_golden(random_val);  
    expected_queue.push_back(golden_val);
    sample_count = sample_count + 1;
end
sample_valid = 0;
sample_in    = 0;
repeat(LATENCY + 10) @(posedge clk);
#1;
// check queue empty, report, $finish

Code ONLY.
"""

IIR_SV_TB_USER_PROMPT = """
Write a BIT-ACCURATE, SELF-CHECKING SystemVerilog testbench using Icarus Verilog.

Use ONLY this VERIFICATION_PLAN:
{plan}

CONSTRAINTS:
{iverilog_constraints}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRATEGY: EXACT MATCH SHADOW MODEL (NO FLOATING POINT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Implement a shadow model in INTEGER ARITHMETIC. No reals, no tolerance.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1: IIR TIMING CONTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All IIR topologies have LATENCY = 1.

The RTL registers result_out ON the clock edge that captures sample_in.
result_valid fires ONE cycle after sample_valid.

MANDATORY STIMULUS LOOP ORDER — DO NOT DEVIATE:

  STEP 1: Drive sample_in and sample_valid
  STEP 2: @(posedge clk)        ← RTL captures sample, registers result_out
  STEP 3: golden = calculate_golden_output(sample)  ← AFTER edge, uses pre-edge state
  STEP 4: expected_queue.push_back(golden)          ← AFTER edge
  STEP 5: update_tb_state()                         ← AFTER edge, AFTER golden computed

WHY THIS IS CORRECT:
  At STEP 2, the RTL reads pre-edge state (w1, w2) and registers the output.
  At STEP 3, tb_state is still pre-edge (not yet updated) — matching RTL exactly.
  At STEP 5, tb_state advances to match RTL post-edge state.
  The checker fires at cycle N+1 when result_valid=1, pops golden[N], compares
  with result_out[N]. Both computed from the same pre-edge state. ✓

WRONG — golden computed before edge:
  golden = calculate_golden_output(sample);  // before @posedge
  push(golden);
  @(posedge clk);
  update_tb_state();
  // WRONG: golden pushed before edge, but checker pops it at NEXT edge.
  // Queue is one step ahead of RTL output → 1-cycle shift on every sample.

WRONG — state updated before golden computed:
  @(posedge clk);
  update_tb_state();         // state advanced first
  golden = calculate_golden_output(sample);  // uses WRONG (post-edge) state
  push(golden);
  // WRONG: golden uses N state but RTL used N-1 state → 1-cycle shift.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2: TWO-FUNCTION ARCHITECTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Split the shadow model into two constructs:

  function calculate_golden_output(sample)
    - Reads tb_state (w1_tb, w2_tb, etc.) — does NOT mutate them
    - Computes and returns the saturated output
    - Writes intermediate values (w_new_tb, last_y_new_tb) to module-scope
      staging variables so the task can use them

  task update_tb_state()
    - Reads staging variables
    - Saturates where the RTL saturates
    - Mutates tb_state (w1_tb, w2_tb, etc.)
    - Called AFTER @(posedge clk) AND AFTER calculate_golden_output

FORBIDDEN: merging state mutation into calculate_golden_output.
FORBIDDEN: calling update_tb_state before calculate_golden_output in same iteration.
FORBIDDEN: writing w1_tb, w2_tb, y1_tb, x1_tb, s0_tb, etc. inside the function.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3: SHADOW MODEL BY TOPOLOGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXED-POINT RULE: Never scale sample_in. Shift each product individually.
SATURATION: Declare as signed localparams to avoid unsigned comparison bugs:
  localparam signed [ACC_WIDTH-1:0] SAT_MAX_TB = (1 << (DATA_WIDTH-1)) - 1;
  localparam signed [ACC_WIDTH-1:0] SAT_MIN_TB = -(1 << (DATA_WIDTH-1));

── biquad_df2t (Transposed Direct Form II) ──────────────────
A0 is 1.0 (implied). Only A1 and A2 are used for feedback.
State: s0_tb, s1_tb (ACC_WIDTH, signed).

  reg signed [ACC_WIDTH-1:0] s0_tb=0, s1_tb=0;
  reg signed [ACC_WIDTH-1:0] s0_next_tb, s1_next_tb;

  function signed [DATA_WIDTH-1:0] calculate_golden_output;
    input signed [DATA_WIDTH-1:0] sample;
    reg signed [ACC_WIDTH-1:0] y_out;
    // CRITICAL: Exact-width intermediate products to prevent Verilog truncation!
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b0_prod;
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b1_prod;
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b2_prod;
    reg signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a1_prod;
    reg signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a2_prod;
    begin
        b0_prod = B0 * sample;
        b1_prod = B1 * sample;
        b2_prod = B2 * sample;
        
        y_out = (b0_prod >>> COEFF_FRAC_BITS) + s0_tb;
        
        a1_prod = A1 * y_out;
        a2_prod = A2 * y_out;
        s0_next_tb = (b1_prod >>> COEFF_FRAC_BITS) - (a1_prod >>> COEFF_FRAC_BITS) + s1_tb;
        s1_next_tb = (b2_prod >>> COEFF_FRAC_BITS) - (a2_prod >>> COEFF_FRAC_BITS);

        if      (y_out > SAT_MAX_TB) calculate_golden_output = SAT_MAX_TB[DATA_WIDTH-1:0];
        else if (y_out < SAT_MIN_TB) calculate_golden_output = SAT_MIN_TB[DATA_WIDTH-1:0];
        else                         calculate_golden_output = y_out[DATA_WIDTH-1:0];
    end
  endfunction

  task update_tb_state;
    begin
        s0_tb = s0_next_tb;
        s1_tb = s1_next_tb;
    end
  endtask

── biquad_df1 ───────────────────────────────────────────────
RTL stores UNSATURATED y_out in y_d1 at ACC_WIDTH.

  reg signed [DATA_WIDTH-1:0] x1_tb=0, x2_tb=0;
  reg signed [ACC_WIDTH-1:0]  y1_tb=0, y2_tb=0;
  reg signed [DATA_WIDTH-1:0] last_sample_tb;
  reg signed [ACC_WIDTH-1:0]  last_y_new_tb;

  function signed [DATA_WIDTH-1:0] calculate_golden_output;
    input signed [DATA_WIDTH-1:0] sample;
    reg signed [ACC_WIDTH-1:0] y_new;
    // CRITICAL: Exact-width intermediate products to prevent Verilog truncation!
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b0_prod;
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b1_prod;
    reg signed [DATA_WIDTH+COEFF_WIDTH-1:0] b2_prod;
    reg signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a1_prod;
    reg signed [ACC_WIDTH+COEFF_WIDTH-1:0]  a2_prod;
    begin
      b0_prod = B0 * sample;
      b1_prod = B1 * x1_tb;
      b2_prod = B2 * x2_tb;
      a1_prod = A1 * y1_tb;
      a2_prod = A2 * y2_tb;
      
      y_new = (b0_prod >>> COEFF_FRAC_BITS)
            + (b1_prod >>> COEFF_FRAC_BITS)
            + (b2_prod >>> COEFF_FRAC_BITS)
            - (a1_prod >>> COEFF_FRAC_BITS)
            - (a2_prod >>> COEFF_FRAC_BITS);
            
      last_sample_tb = sample;
      last_y_new_tb  = y_new;
      if      (y_new > SAT_MAX_TB) calculate_golden_output = SAT_MAX_TB[DATA_WIDTH-1:0];
      else if (y_new < SAT_MIN_TB) calculate_golden_output = SAT_MIN_TB[DATA_WIDTH-1:0];
      else                         calculate_golden_output = y_new[DATA_WIDTH-1:0];
    end
  endfunction

  task update_tb_state;
    begin
      x2_tb = x1_tb; x1_tb = last_sample_tb;
      y2_tb = y1_tb; y1_tb = last_y_new_tb;  // UNSATURATED
    end
  endtask
  
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4: STIMULUS LOOP (COPY EXACTLY — DO NOT REORDER)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

initial begin
    rst_n = 0; sample_valid = 0; sample_in = 0;
    error_count = 0; sample_count = 0; sample_index = 0;
    repeat(2) @(posedge clk);
    #1; rst_n = 1;    // MANDATORY: #1 delay before lifting reset

    for (i = 0; i < VECTOR_COUNT; i = i + 1) begin
        // STEP 1: Drive (Happens 1ns after the previous clock edge)
        random_val   = $random;
        sample_in    = random_val;
        sample_valid = 1;

        // STEP 2: Wait for Clock edge
        @(posedge clk);

        // STEP 3: Golden — uses PRE-EDGE state (not yet updated)
        golden_val = calculate_golden_output(random_val);

        // STEP 4: Push
        expected_queue.push_back(golden_val);
        sample_count = sample_count + 1;

        // STEP 5: Advance TB state
        update_tb_state();
        
        // STEP 6: Hold time delay
        #1;           // MANDATORY: Advance time before driving next inputs
    end

    sample_valid = 0;
    sample_in    = 0;
    repeat(LATENCY + 10) @(posedge clk);
    #1;

    if (expected_queue.size() != 0) begin
        $display("ERROR: %0d outputs never received", expected_queue.size());
        error_count = error_count + expected_queue.size();
    end
    $display("Test complete: %0d errors", error_count);
    $finish;
end
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5: CHECKER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You MUST explicitly declare a SystemVerilog queue to hold the expected values.
Failure to declare the queue will break the pipeline.

REQUIRED DECLARATIONS:
  reg signed [DATA_WIDTH-1:0] expected_queue [$];
  reg signed [DATA_WIDTH-1:0] golden_val;
  reg signed [DATA_WIDTH-1:0] rtl_val;

always @(posedge clk) begin
    if (result_valid) begin
        if (expected_queue.size() == 0) begin
            $display("FAIL: Unexpected result_valid (queue empty)");
            error_count = error_count + 1;
        end else begin
            rtl_val    = result_out;
            golden_val = expected_queue.pop_front();
            abs_error  = (rtl_val >= golden_val) ? (rtl_val - golden_val)
                                                 : (golden_val - rtl_val);
            
            // UNCONDITIONAL LOGGING: Print every sample for double-checking
            $display("[DATA] Sample %0d | RTL = %0d | TB_Golden = %0d | Diff = %0d", 
                     sample_index, rtl_val, golden_val, abs_error);

            if (rtl_val !== golden_val) begin
                $display("FAIL: Mismatch at Sample %0d!", sample_index);
                error_count = error_count + 1;
            end
            
            sample_index = sample_index + 1;
        end
    end
end

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6: TERMINATION AND WATCHDOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Watchdog in a separate initial block:
  initial begin
      #(VECTOR_COUNT * CLOCK_PERIOD_NS * 10);
      $display("FATAL: Simulation timeout");
      $finish;
  end

Never use wait(queue.size()==0) — hangs if result_valid never fires.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 7: COEFFICIENT AND PARAMETER RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- COEFF_FIXED AS-IS. Never re-scale, negate, or take absolute value.
- Declare as signed scalar localparams: localparam signed [COEFF_WIDTH-1:0] B0 = 123;
- Each product shifted >>> COEFF_FRAC_BITS individually. Never shift the sum.
- sample_in never scaled (no * 2^FRAC on input).
- Arithmetic shift >>> only (never logical >>).
- DUT instantiated with NO parameter overrides — port connections only.
- TB declares its own LATENCY, DATA_WIDTH, ACC_WIDTH, COEFF_FRAC_BITS
  independently, matching VERIFICATION_PLAN values.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 8: PRE-OUTPUT CHECKLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIMING (most critical):
[ ] Stimulus order is EXACTLY: drive → @posedge → golden → push → update_state → #1 hold?
[ ] golden computed AFTER @(posedge clk)?
[ ] update_tb_state() called AFTER calculate_golden_output() in same iteration?
[ ] MUST INCLUDE #1 delay at the very end of the stimulus loop?
[ ] MUST INCLUDE #1 delay before setting rst_n = 1?
[ ] No idle clock between rst_n=1 and first stimulus iteration?

SHADOW MODEL:
[ ] calculate_golden_output does NOT mutate s*_tb?
[ ] Shadow model uses Transposed DF2T (s0, s1) and NOT Direct Form II (w1, w2)?
[ ] calculate_golden_output uses pre-edge state (s0_tb) and NOT s0_next_tb?
[ ] biquad_df1: feedback stored UNSATURATED at ACC_WIDTH?
[ ] Older state updated before newer (w2_tb=w1_tb before w1_tb=w_sat_tb)?
[ ] SAT_MAX_TB / SAT_MIN_TB declared as signed localparams?

ARITHMETIC:
[ ] Each product shifted >>> COEFF_FRAC_BITS individually?
[ ] Arithmetic shift >>> (not logical >>)?
[ ] sample_in not scaled?
[ ] Coefficients AS-IS (not re-scaled)?
[ ] Multiplier products use exact-width intermediate regs inside functions (NO ACC_WIDTH casts on both operands, NO silent Verilog truncation)?

CHECKER / INFRA:
[ ] Checker in separate always @(posedge clk)?
[ ] Checker pops only when result_valid is high?
[ ] DUT instantiated with NO parameter overrides?
[ ] Watchdog in separate initial block?
[ ] Is `reg signed [DATA_WIDTH-1:0] expected_queue [$];` explicitly declared at the module level?

If ANY box unchecked → fix before outputting.

Code ONLY.
"""


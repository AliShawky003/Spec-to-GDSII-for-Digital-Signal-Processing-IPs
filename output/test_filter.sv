// test_filter.sv
// BIT-ACCURATE, SELF-CHECKING testbench for fir_filter (folded FIR)
// Generated from VERIFICATION_PLAN

module test_filter;

   // ============================================================
   // PARAMETERS (explicit defaults)
   // ============================================================
   parameter DATA_WIDTH     = 16;
   parameter ACC_WIDTH      = 37;
   parameter COEFF_WIDTH    = 16;
   parameter COEFF_FRAC_BITS = 15;
   parameter TAPS           = 17;  // from plan (no ORDER given, TAPS=17 is correct)
   parameter LATENCY        = 17;
   parameter CLOCK_PERIOD_NS = 20;
   parameter VECTOR_COUNT   = 200;

   // ============================================================
   // SIGNALS
   // ============================================================
   reg clk;
   reg rst_n;
   reg signed [DATA_WIDTH-1:0] sample_in;
   reg sample_valid;
   wire signed [DATA_WIDTH-1:0] result_out;
   wire result_valid;
   wire ready;  // folded: ready signal

   // ============================================================
   // MODULE-SCOPE VARIABLES
   // ============================================================
   integer i;                      // stimulus iterator
   integer checker_index;          // checker pop counter
   integer sample_count;           // stimulus sample count
   integer error_count;            // mismatch counter
   reg signed [DATA_WIDTH-1:0] random_val;
   reg signed [DATA_WIDTH-1:0] golden_val;
   reg signed [DATA_WIDTH-1:0] rtl_val;

   // Expected queue (MANDATORY: declare at module scope)
   reg signed [DATA_WIDTH-1:0] expected_queue [$];

   // History buffer for golden model
   reg signed [DATA_WIDTH-1:0] history [0:TAPS-1];
   integer h_init;

   // Coefficient array
   reg signed [COEFF_WIDTH-1:0] coeff [0:TAPS-1];

   // Saturation bounds
   localparam signed [ACC_WIDTH-1:0] SAT_MAX = (1 << (DATA_WIDTH-1)) - 1;
   localparam signed [ACC_WIDTH-1:0] SAT_MIN = -(1 << (DATA_WIDTH-1));

   // ============================================================
   // CLOCK GENERATION
   // ============================================================
   initial clk = 0;
   always #(CLOCK_PERIOD_NS/2) clk = ~clk;

   // ============================================================
   // DUT INSTANTIATION
   // ============================================================
   fir_filter dut (.*);

   // ============================================================
   // COEFFICIENT INITIALIZATION (from VERIFICATION_PLAN)
   // ============================================================
   initial begin
      coeff[0]  = -57;
      coeff[1]  = -12;
      coeff[2]  = 157;
      coeff[3]  = 629;
      coeff[4]  = 1498;
      coeff[5]  = 2690;
      coeff[6]  = 3944;
      coeff[7]  = 4904;
      coeff[8]  = 5264;
      coeff[9]  = 4904;
      coeff[10] = 3944;
      coeff[11] = 2690;
      coeff[12] = 1498;
      coeff[13] = 629;
      coeff[14] = 157;
      coeff[15] = -12;
      coeff[16] = -57;
   end

   // ============================================================
   // HISTORY INITIALIZATION
   // ============================================================
   initial begin
      for (h_init = 0; h_init < TAPS; h_init = h_init + 1)
         history[h_init] = '0;
   end

   // ============================================================
   // GOLDEN MODEL FUNCTION (Bit-Accurate Integer Math)
   // ============================================================
   function signed [DATA_WIDTH-1:0] calculate_golden;
      input signed [DATA_WIDTH-1:0] sample;
      reg signed [ACC_WIDTH-1:0] acc;
      reg signed [ACC_WIDTH-1:0] shifted;
      integer k;
      begin
         // Shift history
         for (k = TAPS-1; k > 0; k = k-1)
            history[k] = history[k-1];
         history[0] = sample;

         // MAC
         acc = 0;
         for (k = 0; k < TAPS; k = k+1) begin
            // CRITICAL: Cast to ACC_WIDTH to match RTL truncation behavior
            acc = acc + (ACC_WIDTH'(signed'(history[k])) * ACC_WIDTH'(signed'(coeff[k])));
         end

         // Shift and saturate
         shifted = acc >>> COEFF_FRAC_BITS;
         if (shifted > SAT_MAX)
            calculate_golden = SAT_MAX[DATA_WIDTH-1:0];
         else if (shifted < SAT_MIN)
            calculate_golden = SAT_MIN[DATA_WIDTH-1:0];
         else
            calculate_golden = shifted[DATA_WIDTH-1:0];
      end
   endfunction

   // ============================================================
   // STIMULUS (main initial block)
   // ============================================================
   initial begin
      // Initialize variables
      checker_index = 0;
      sample_count  = 0;
      error_count   = 0;
      sample_in     = '0;
      sample_valid  = 0;

      // Reset sequence
      rst_n = 0;
      repeat(2) @(posedge clk);
      rst_n = 1;

      // Pipeline flush (10 idle clocks)
      sample_valid = 0;
      sample_in    = '0;
      repeat(10) @(posedge clk);

      // Stimulus loop (folded: wait for ready before each sample)
      for (i = 0; i < VECTOR_COUNT; i = i + 1) begin
         // Wait until DUT is ready for next sample
         while (ready !== 1) @(posedge clk);

         random_val   = $random;
         sample_in    = random_val;
         sample_valid = 1;

         @(posedge clk);  //   THE WALL   RTL captures here

         // Compute golden AFTER the edge
         golden_val = calculate_golden(random_val);
         expected_queue.push_back(golden_val);
         sample_count = sample_count + 1;

         // De-assert valid
         sample_valid = 0;
         sample_in    = 0;
      end

      // Allow pipeline to drain
      repeat(LATENCY + 10) @(posedge clk);
      #1;

      // Check for lost outputs
      if (expected_queue.size() != 0) begin
         $display("ERROR: %0d outputs never received", expected_queue.size());
         error_count = error_count + expected_queue.size();
      end

      // Final report
      if (error_count == 0)
         $display("TEST PASSED: All %0d samples match exactly", sample_count);
      else
         $display("TEST FAILED: %0d mismatches out of %0d samples", error_count, sample_count);

      $finish;
   end

   // ============================================================
   // CHECKER (SEPARATE always block   NEVER embed in stimulus loop)
   // ============================================================
   always @(posedge clk) begin
      if (result_valid) begin
         rtl_val = result_out;
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

   // ============================================================
   // WATCHDOG TIMEOUT (folded: scaled by TAPS)
   // ============================================================
   initial begin
      #(VECTOR_COUNT * TAPS * CLOCK_PERIOD_NS * 2);
      $display("FATAL: Simulation timeout");
      $finish;
   end

endmodule

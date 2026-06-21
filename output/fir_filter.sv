module fir_filter (
    input  wire                     clk,
    (* max_fanout = 50 *) input  wire                     rst_n,
    input  wire signed [15:0]       sample_in,
    input  wire                     sample_valid,
    output reg  signed [15:0]       result_out,
    output reg                      result_valid,
    output wire                     ready
);

    // Parameters
    localparam TAPS = 17;
    localparam ORDER = 16;
    localparam DATA_WIDTH = 16;
    localparam COEFF_WIDTH = 16;
    localparam COEFF_FRAC_BITS = 15;
    localparam ACC_WIDTH = 37;
    localparam LATENCY = 19;
    localparam BASE_LATENCY = 2;
    localparam PIPE_DEPTH = LATENCY - BASE_LATENCY;

    // Coefficient array for folded topology
    logic signed [COEFF_WIDTH-1:0] coeffs [0:TAPS-1];

    initial begin
        coeffs[0] = -16'd57;
        coeffs[1] = -16'd12;
        coeffs[2] = 16'd157;
        coeffs[3] = 16'd629;
        coeffs[4] = 16'd1498;
        coeffs[5] = 16'd2690;
        coeffs[6] = 16'd3944;
        coeffs[7] = 16'd4904;
        coeffs[8] = 16'd5264;
        coeffs[9] = 16'd4904;
        coeffs[10] = 16'd3944;
        coeffs[11] = 16'd2690;
        coeffs[12] = 16'd1498;
        coeffs[13] = 16'd629;
        coeffs[14] = 16'd157;
        coeffs[15] = -16'd12;
        coeffs[16] = -16'd57;
    end

    // Signals
    integer i;
    integer k;
    logic signed [DATA_WIDTH-1:0] data_reg [0:TAPS-1];
    logic signed [ACC_WIDTH-1:0] accum_reg;
    logic [$clog2(TAPS)-1:0] tap_cnt;
    logic mac_valid;
    logic signed [ACC_WIDTH-1:0] scaled_acc;
    logic signed [DATA_WIDTH-1:0] result_temp;
    logic signed [DATA_WIDTH+COEFF_WIDTH-1:0] prod;

    // Ready signal
    assign ready = (tap_cnt == 0);

    // Stage 1: data shift register
    always_ff @(posedge clk or negedge rst_n) begin : shift_logic
        if (!rst_n) begin
            /* verilator lint_off BLKLOOPINIT */
            for (i = 0; i < TAPS; i = i + 1) data_reg[i] <= '0;
            /* verilator lint_on BLKLOOPINIT */
        end else if (sample_valid && ready) begin
            data_reg[0] <= sample_in;
            for (i = 1; i < TAPS; i = i + 1) data_reg[i] <= data_reg[i-1];
        end
    end

    // Stage 2: tap counter
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tap_cnt <= '0;
        end else if (sample_valid && ready) begin
            tap_cnt <= 1;
        end else if (tap_cnt != 0) begin
            if (tap_cnt == TAPS-1) tap_cnt <= '0;
            else tap_cnt <= tap_cnt + 1;
        end
    end

    // Stage 3: MAC
    always_ff @(posedge clk or negedge rst_n) begin : mac_logic
        if (!rst_n) begin
            accum_reg <= '0;
            mac_valid <= 1'b0;
        end else begin
            mac_valid <= (tap_cnt == TAPS-1);
            if (tap_cnt == 0 && sample_valid) begin
                prod = sample_in * coeffs[0];
                accum_reg <= prod;
            end else if (tap_cnt != 0) begin
                prod = data_reg[tap_cnt] * coeffs[tap_cnt];
                accum_reg <= accum_reg + prod;
            end
        end
    end

    // Output scaling
    assign scaled_acc = accum_reg >>> COEFF_FRAC_BITS;

    // Saturation logic
    localparam signed [ACC_WIDTH-1:0] SAT_MAX = (1 << (DATA_WIDTH-1)) - 1;
    localparam signed [ACC_WIDTH-1:0] SAT_MIN = -(1 << (DATA_WIDTH-1));

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

    // Output register
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) result_out <= '0;
        else if (mac_valid) result_out <= result_temp;
    end

    // Valid signal
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) result_valid <= 1'b0;
        else result_valid <= mac_valid;
    end

endmodule

#------------------------------------------#
# LibreLane PnR Constraints
#------------------------------------------#
# Clock period : 13.081284 ns
# I/O delay    : 20% of clock period  (OpenLane formula)
# All other values are constant flow-policy constraints
#------------------------------------------#

# Clock network
set clk_input clk
create_clock [get_ports $clk_input] -name clk -period 13.081284

# Clock non-idealities
set_propagated_clock [get_clocks {clk}]
set_clock_uncertainty 0.5 [get_clocks {clk}]

# Constant implementation policy constraints
set_max_transition 0.75 [current_design]
set_max_fanout     16   [current_design]
set_timing_derate -early [expr {1 - 0.05}]
set_timing_derate -late  [expr {1 + 0.05}]

# Constant clock input transition
set_input_transition 0.15 [get_ports $clk_input]

# Placeholder I/O timing — OpenLane formula: IO delay = 20% of clock period
set io_delay_value [expr { 13.081284 * 0.2 }]

# Input delays (replace with interface-specific values when known)
set_input_delay -max $io_delay_value -clock [get_clocks {clk}] [get_ports { sample_in[*] sample_valid }]
set_input_delay -min 0.0             -clock [get_clocks {clk}] [get_ports { sample_in[*] sample_valid }]

# Output delays
set_output_delay -max $io_delay_value -clock [get_clocks {clk}] [all_outputs]
set_output_delay -min 0.0             -clock [get_clocks {clk}] [all_outputs]

# Constant output load  (0.05 pF = 50 fF)
set_load 0.05 [all_outputs]

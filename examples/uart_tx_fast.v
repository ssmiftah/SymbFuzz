// UART TX with DIVIDER=1 (1 clk per bit) for fast BMC
// 8N1: start + 8 data bits (LSB first) + stop
// Frame = 10 bits = 20 transitions in clk2fflogic model
// + 2 transitions for valid_i → total ~24 steps

module uart_tx_fast (
    input  wire        clk,
    input  wire        rst,
    input  wire        valid_i,
    input  wire [7:0]  data_i,
    output reg         ready_o,
    output reg         tx_o,
    output reg         tx_done_o
);
    localparam S_IDLE  = 2'd0;
    localparam S_START = 2'd1;
    localparam S_DATA  = 2'd2;
    localparam S_STOP  = 2'd3;

    reg [1:0] state;
    reg [2:0] bit_idx;
    reg [7:0] shift_reg;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            state     <= S_IDLE;
            bit_idx   <= 0;
            shift_reg <= 0;
            tx_o      <= 1;
            ready_o   <= 1;
            tx_done_o <= 0;
        end else begin
            tx_done_o <= 0;
            case (state)
                S_IDLE: begin
                    tx_o    <= 1;
                    ready_o <= 1;
                    if (valid_i) begin
                        shift_reg <= data_i;
                        state     <= S_START;
                        ready_o   <= 0;
                    end
                end
                S_START: begin
                    tx_o    <= 0;
                    bit_idx <= 0;
                    state   <= S_DATA;
                end
                S_DATA: begin
                    tx_o      <= shift_reg[0];
                    shift_reg <= shift_reg >> 1;
                    if (bit_idx == 7)
                        state <= S_STOP;
                    else
                        bit_idx <= bit_idx + 1;
                end
                S_STOP: begin
                    tx_o      <= 1;
                    tx_done_o <= 1;
                    state     <= S_IDLE;
                    ready_o   <= 1;
                end
            endcase
        end
    end
endmodule

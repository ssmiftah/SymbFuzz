// UART Transmitter
// 8N1 format: 1 start bit, 8 data bits (LSB first), 1 stop bit
// Baud-rate divider = DIVIDER (clk cycles per bit)
//
// Interesting BMC target: find the input sequence to observe
//   tx_done_o=1 while having transmitted a specific byte (e.g. 0xA5)

module uart_tx #(
    parameter DIVIDER = 4   // clk cycles per UART bit (keep small for BMC)
)(
    input  wire        clk,
    input  wire        rst,
    // Data interface
    input  wire        valid_i,     // pulse to start transmission
    input  wire [7:0]  data_i,      // byte to transmit
    output reg         ready_o,     // 1 when idle and accepting new data
    // UART line
    output reg         tx_o,        // serial output (idle=1)
    // Status
    output reg         tx_done_o    // 1 for one cycle when frame complete
);

    // State encoding
    localparam S_IDLE  = 2'd0;
    localparam S_START = 2'd1;
    localparam S_DATA  = 2'd2;
    localparam S_STOP  = 2'd3;

    reg [1:0]  state;
    reg [2:0]  bit_idx;     // 0..7: which data bit we're sending
    reg [7:0]  shift_reg;   // data being shifted out
    reg [$clog2(DIVIDER)-1:0] baud_cnt; // baud-rate counter

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            state     <= S_IDLE;
            bit_idx   <= 0;
            shift_reg <= 0;
            baud_cnt  <= 0;
            tx_o      <= 1;
            ready_o   <= 1;
            tx_done_o <= 0;
        end else begin
            tx_done_o <= 0; // default

            case (state)
                S_IDLE: begin
                    tx_o    <= 1;
                    ready_o <= 1;
                    if (valid_i) begin
                        shift_reg <= data_i;
                        baud_cnt  <= 0;
                        state     <= S_START;
                        ready_o   <= 0;
                    end
                end

                S_START: begin
                    tx_o <= 0; // start bit (low)
                    if (baud_cnt == DIVIDER - 1) begin
                        baud_cnt <= 0;
                        bit_idx  <= 0;
                        state    <= S_DATA;
                    end else begin
                        baud_cnt <= baud_cnt + 1;
                    end
                end

                S_DATA: begin
                    tx_o <= shift_reg[0]; // LSB first
                    if (baud_cnt == DIVIDER - 1) begin
                        baud_cnt  <= 0;
                        shift_reg <= shift_reg >> 1;
                        if (bit_idx == 7) begin
                            state <= S_STOP;
                        end else begin
                            bit_idx <= bit_idx + 1;
                        end
                    end else begin
                        baud_cnt <= baud_cnt + 1;
                    end
                end

                S_STOP: begin
                    tx_o <= 1; // stop bit (high)
                    if (baud_cnt == DIVIDER - 1) begin
                        baud_cnt  <= 0;
                        state     <= S_IDLE;
                        tx_done_o <= 1;
                        ready_o   <= 1;
                    end else begin
                        baud_cnt <= baud_cnt + 1;
                    end
                end
            endcase
        end
    end

endmodule

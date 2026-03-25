// Hardware Combination Lock
// Must enter the correct 4-key sequence: 3 -> 1 -> 4 -> 1 (like pi)
// to reach the UNLOCKED state.
// Any wrong key resets back to LOCKED.
//
// BMC target: find input sequence to drive lock_open_o = 1
// Security angle: can we find a backdoor/shortcut sequence?

module lock_fsm (
    input  wire       clk,
    input  wire       rst,
    input  wire [2:0] key_i,       // 3-bit key press (0-7), 0 = no press
    input  wire       key_valid_i, // pulse when key_i is valid
    output reg        lock_open_o, // 1 when unlocked
    output reg        error_o      // 1 for one cycle on wrong key
);

    // States: waiting for each of the 4 correct digits
    localparam S_WAIT_1  = 3'd0; // expecting key 3
    localparam S_WAIT_2  = 3'd1; // expecting key 1
    localparam S_WAIT_3  = 3'd2; // expecting key 4
    localparam S_WAIT_4  = 3'd3; // expecting key 1
    localparam S_OPEN    = 3'd4; // unlocked
    localparam S_ERROR   = 3'd5; // wrong key (1-cycle penalty)

    // The correct combination: 3, 1, 4, 1
    localparam [2:0] CODE_0 = 3'd3;
    localparam [2:0] CODE_1 = 3'd1;
    localparam [2:0] CODE_2 = 3'd4;
    localparam [2:0] CODE_3 = 3'd1;

    reg [2:0] state;
    reg [3:0] open_timer; // keep open for 15 cycles then re-lock

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            state       <= S_WAIT_1;
            lock_open_o <= 0;
            error_o     <= 0;
            open_timer  <= 0;
        end else begin
            error_o <= 0; // default

            case (state)
                S_WAIT_1: begin
                    lock_open_o <= 0;
                    if (key_valid_i) begin
                        if (key_i == CODE_0)
                            state <= S_WAIT_2;
                        else begin
                            state   <= S_ERROR;
                            error_o <= 1;
                        end
                    end
                end

                S_WAIT_2: begin
                    if (key_valid_i) begin
                        if (key_i == CODE_1)
                            state <= S_WAIT_3;
                        else begin
                            state   <= S_ERROR;
                            error_o <= 1;
                        end
                    end
                end

                S_WAIT_3: begin
                    if (key_valid_i) begin
                        if (key_i == CODE_2)
                            state <= S_WAIT_4;
                        else begin
                            state   <= S_ERROR;
                            error_o <= 1;
                        end
                    end
                end

                S_WAIT_4: begin
                    if (key_valid_i) begin
                        if (key_i == CODE_3) begin
                            state       <= S_OPEN;
                            lock_open_o <= 1;
                            open_timer  <= 4'hF;
                        end else begin
                            state   <= S_ERROR;
                            error_o <= 1;
                        end
                    end
                end

                S_OPEN: begin
                    lock_open_o <= 1;
                    // Auto-lock after timer expires
                    if (open_timer == 0) begin
                        state       <= S_WAIT_1;
                        lock_open_o <= 0;
                    end else begin
                        open_timer <= open_timer - 1;
                    end
                end

                S_ERROR: begin
                    // Return to start after 1-cycle error penalty
                    state <= S_WAIT_1;
                end

                default: state <= S_WAIT_1;
            endcase
        end
    end

endmodule

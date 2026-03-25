// Simple 4-bit counter — smoke-test for SymbFuzz
module counter(
    input  wire       clk,
    input  wire       rst,
    input  wire       en,
    output reg  [3:0] count
);
    always @(posedge clk or posedge rst) begin
        if (rst)     count <= 4'b0;
        else if (en) count <= count + 4'b1;
    end
endmodule

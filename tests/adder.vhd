library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity adder is
    port (
        clk : in std_logic;
        a   : in unsigned(7 downto 0);
        b   : in unsigned(7 downto 0);
        sum : out unsigned(7 downto 0)
    );
end entity adder;

architecture rtl of adder is
    signal sum_reg : unsigned(7 downto 0);
begin
    process(clk)
    begin
        if rising_edge(clk) then
            sum_reg <= a + b;
        end if;
    end process;

    sum <= sum_reg;
end architecture rtl;

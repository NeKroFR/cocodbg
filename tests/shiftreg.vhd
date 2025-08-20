library IEEE;
use IEEE.STD_LOGIC_1164.ALL;

entity shiftreg is
    Port (
        clk   : in  STD_LOGIC;
        rst   : in  STD_LOGIC;
        din   : in  STD_LOGIC;
        dout  : out STD_LOGIC_VECTOR(7 downto 0)
    );
end shiftreg;

architecture Behavioral of shiftreg is
    signal reg : STD_LOGIC_VECTOR(7 downto 0) := (others => '0');
begin
    process(clk, rst)
    begin
        if rst = '1' then
            reg <= (others => '0');
        elsif rising_edge(clk) then
            reg <= reg(6 downto 0) & din;
        end if;
    end process;

    dout <= reg;
end Behavioral;

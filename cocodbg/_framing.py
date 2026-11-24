"""Reply line escaping so a payload line can never be mistaken for a frame marker."""

BEGIN = "<<BEGIN>>"
END = "<<END>>"


def escape_line(line: str) -> str:
    """Prefix lines that start with a marker prefix or a backslash with one backslash."""
    if line.startswith("<<") or line.startswith("\\"):
        return "\\" + line
    return line


def unescape_line(line: str) -> str:
    """Inverse of escape_line. Lines from servers that do not escape pass through unchanged."""
    if line.startswith("\\"):
        return line[1:]
    return line

from __future__ import annotations

# A minimal 5x5 block-letter font, just enough for "COMMANDER". Handwritten
# once here rather than pulled from a figlet dependency -- it's nine glyphs,
# not worth a library.
_FONT: dict[str, list[str]] = {
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"],
    "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#### ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
}

BLOCK = "█"  # solid block, swapped in for the '#' placeholders above


def banner(word: str, gap: int = 1, scale: int = 1) -> str:
    """Render `word` as block letters, 5 rows tall at scale 1; a larger scale
    makes every block `scale` cells wide and tall."""
    letters = [_FONT[ch] for ch in word.upper()]
    sep = " " * gap
    rows = []
    for row_index in range(5):
        row = sep.join(letter[row_index] for letter in letters).replace("#", BLOCK)
        row = "".join(ch * scale for ch in row)
        rows.extend([row] * scale)
    return "\n".join(rows)

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


def banner(word: str, gap: int = 1) -> str:
    """Render `word` as 5 lines of block letters."""
    letters = [_FONT[ch] for ch in word.upper()]
    sep = " " * gap
    rows = []
    for row_index in range(5):
        rows.append(sep.join(letter[row_index] for letter in letters).replace("#", BLOCK))
    return "\n".join(rows)

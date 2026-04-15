# Character set ported from https://github.com/ManlyMorgan/Split-Flap-Display

CHARS = [
    ' ', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
    'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
]

STEPS_PER_ROTATION = 2048
NUM_CHARS = len(CHARS)  # 37

# Pre-computed step position for each character
CHAR_POSITIONS: dict[str, int] = {
    char: int(i * STEPS_PER_ROTATION / NUM_CHARS)
    for i, char in enumerate(CHARS)
}


def get_position(char: str) -> int:
    """Return the step position for a given character, defaulting to blank."""
    return CHAR_POSITIONS.get(char.upper(), 0)

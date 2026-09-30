"""Error-correcting code of ECC memory, simulated: extended Hamming SEC-DED (72,64).

ECC DIMMs store every 64-bit word with 8 check bits. Seven of them are Hamming parity bits
(at codeword positions 1, 2, 4, ..., 64); the syndrome of a single flipped bit is its
position, so it can be corrected. The eighth is a parity bit over the whole codeword; it
tells a single error (overall parity wrong) from a double error (syndrome non-zero, overall
parity right), which can only be detected. For comparison, a single parity bit per word
detects any odd number of flipped bits and corrects nothing.

Codeword layout: bit 0 is the overall parity, bits 1..71 are the Hamming code.
"""

DATA_BITS = 64
PARITY_BITS = 7  # 2**7 >= 64 + 7 + 1
CODE_BITS = DATA_BITS + PARITY_BITS + 1  # 72

_DATA_POSITIONS = [p for p in range(1, CODE_BITS) if p & (p - 1)]  # not powers of two


def _bits(value: int) -> int:
    return bin(value).count("1") & 1


def encode(data: int) -> int:
    code = 0
    for i, position in enumerate(_DATA_POSITIONS):
        if data >> i & 1:
            code |= 1 << position
    syndrome = 0
    for position in range(1, CODE_BITS):
        if code >> position & 1:
            syndrome ^= position
    for j in range(PARITY_BITS):
        if syndrome >> j & 1:
            code |= 1 << (1 << j)
    return code | _bits(code)


def decode(code: int) -> tuple[int, str]:
    """Returns (data, status); status is "ok", "corrected" or "detected" (uncorrectable)."""
    syndrome = 0
    for position in range(1, CODE_BITS):
        if code >> position & 1:
            syndrome ^= position
    overall_ok = _bits(code) == 0
    if syndrome == 0 and overall_ok:
        status = "ok"
    elif not overall_ok:
        # Odd number of flips: assume one and correct it (syndrome 0 means bit 0 itself).
        if syndrome < CODE_BITS:
            code ^= 1 << syndrome
        status = "corrected"
    else:
        return _data(code), "detected"
    return _data(code), status


def _data(code: int) -> int:
    return sum(1 << i for i, position in enumerate(_DATA_POSITIONS) if code >> position & 1)


def parity_encode(data: int) -> int:
    """64 data bits and one even-parity bit (bit 64)."""
    return data | _bits(data) << DATA_BITS


def parity_decode(code: int) -> tuple[int, str]:
    data = code & ((1 << DATA_BITS) - 1)
    return data, "ok" if _bits(code) == 0 else "detected"

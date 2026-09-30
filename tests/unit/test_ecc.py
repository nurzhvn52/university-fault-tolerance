import random

from lab import ecc
from lab.ecc_experiment import run

WORDS = [0, 1, (1 << 64) - 1, 0x0123456789ABCDEF, 0xDEADBEEFCAFEBABE]


def test_codeword_has_72_bits_and_round_trips():
    for data in WORDS:
        code = ecc.encode(data)
        assert code < 1 << ecc.CODE_BITS
        assert ecc.decode(code) == (data, "ok")


def test_every_single_bit_flip_is_corrected():
    for data in WORDS:
        code = ecc.encode(data)
        for bit in range(ecc.CODE_BITS):
            assert ecc.decode(code ^ 1 << bit) == (data, "corrected")


def test_every_double_bit_flip_is_detected():
    rng = random.Random(7)
    data = 0x0123456789ABCDEF
    code = ecc.encode(data)
    for _ in range(2000):
        a, b = rng.sample(range(ecc.CODE_BITS), 2)
        assert ecc.decode(code ^ 1 << a ^ 1 << b)[1] == "detected"


def test_parity_detects_odd_flips_and_misses_even_ones():
    data = 0x0123456789ABCDEF
    code = ecc.parity_encode(data)

    assert ecc.parity_decode(code) == (data, "ok")
    assert ecc.parity_decode(code ^ 1 << 5)[1] == "detected"
    assert ecc.parity_decode(code ^ 1 << 5 ^ 1 << 9) == (data ^ 1 << 5 ^ 1 << 9, "ok")


def test_experiment_summary():
    codes = run(words=500, seed=1)["codes"]
    secded, parity = codes["SEC-DED (72,64)"], codes["parity (65,64)"]

    assert secded["1_flips"]["corrected"] == 1.0
    assert secded["2_flips"]["detected"] == 1.0
    assert parity["1_flips"]["detected"] == 1.0
    assert parity["2_flips"]["silent_corruption"] == 1.0

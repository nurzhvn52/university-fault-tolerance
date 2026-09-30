"""Bit-flip injection into memory words protected by parity and by SEC-DED ECC.

    python -m lab.ecc_experiment --words 20000 --seed 1

For 1, 2 and 3 flipped bits per stored word counts how often each code returns the right
data, corrects the error, detects it without correcting, or silently returns wrong data.
Writes results/ecc.json.
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from lab import ecc

ROOT = Path(__file__).resolve().parent.parent
CODES = {
    "parity (65,64)": (ecc.parity_encode, ecc.parity_decode, ecc.DATA_BITS + 1),
    "SEC-DED (72,64)": (ecc.encode, ecc.decode, ecc.CODE_BITS),
}


def outcome(original: int, data: int, status: str) -> str:
    if status == "detected":
        return "detected"
    if data != original:
        return "silent_corruption"
    return "corrected" if status == "corrected" else "ok"


def run(words: int, seed: int) -> dict:
    rng = random.Random(seed)
    results = {}
    for name, (encode, decode, width) in CODES.items():
        results[name] = {"overhead_bits": width - ecc.DATA_BITS}
        for flips in (1, 2, 3):
            counts: Counter[str] = Counter()
            for _ in range(words):
                data = rng.getrandbits(ecc.DATA_BITS)
                code = encode(data)
                for bit in rng.sample(range(width), flips):
                    code ^= 1 << bit
                counts[outcome(data, *decode(code))] += 1
            results[name][f"{flips}_flips"] = {
                kind: round(counts[kind] / words, 4)
                for kind in ("corrected", "detected", "silent_corruption", "ok")
            }
    return {"words": words, "seed": seed, "codes": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--words", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    result = run(args.words, args.seed)
    out = ROOT / "results" / "ecc.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for name, rows in result["codes"].items():
        print(f"{name}: {rows['overhead_bits']} check bits per 64 data bits")
        for flips in (1, 2, 3):
            print(f"  {flips} flipped: {rows[f'{flips}_flips']}")


if __name__ == "__main__":
    main()

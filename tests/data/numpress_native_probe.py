"""Opt-in UBSan reproduction against pinned pynumpress 0.1.5 native source.

Run from the checkout root with `uv run --no-sync python
 tests/data/numpress_native_probe.py`. Requires HTTPS and a clang-compatible C++
compiler. Unsafe decoding runs ONLY in a sanitizer-instrumented subprocess.
Normal pytest never compiles, downloads, or invokes this probe.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import urllib.request
from pathlib import Path

REVISION = "4164a1a1a59ab0aff72f7762bc92535912f2492c"
SOURCES = {
    "MSNumpress.hpp": "bb2256fb3643b4c42b92ff5f24e5d46acf0eb490ecb0d5ea65dfc4998e5d61e4",
    "MSNumpress.cpp": "0a07268c3da0185ddf4be03d2530a66d2f1da996aa098ba07cd0d03c0016e1c7",
    "pynumpress.pyx": "bb90c2698a80e47166696d07d6c9d250e2ef9170841362e55aded90e39f9e04f",
}
DRIVER = r"""
#include "MSNumpress.hpp"
#include <cstdlib>
#include <vector>
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    size_t count = std::strtoul(argv[1], nullptr, 10);
    std::vector<unsigned char> raw(16 + (9 * count + 1) / 2, 0);
    raw[0] = 0x3f; raw[1] = 0xf0; // fixed point 1.0, two zero seeds
    for (size_t i = 0; i < 9 * count; ++i) {
        unsigned char nibble = i % 9 == 0 ? 0 : (i % 9 == 8 ? 7 : 15);
        raw[16 + i / 2] |= i % 2 == 0 ? nibble << 4 : nibble;
    }
    std::vector<double> result;
    ms::numpress::MSNumpress::decodeLinear(raw, result);
    return result.size() == count + 2 ? 0 : 3;
}
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="numpress_ubsan_") as directory:
        root = Path(directory)
        for name, digest in SOURCES.items():
            url = f"https://raw.githubusercontent.com/mobiusklein/pynumpress/{REVISION}/pynumpress/{name}"
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Source hash mismatch: {name}")
            (root / name).write_bytes(data)
        (root / "probe.cpp").write_text(DRIVER)
        executable = root / "probe"
        subprocess.run(
            [
                "c++",
                "-std=c++11",
                "-fsanitize=signed-integer-overflow",
                "-fno-sanitize-recover=all",
                str(root / "MSNumpress.cpp"),
                str(root / "probe.cpp"),
                "-o",
                str(executable),
            ],
            check=True,
        )
        subprocess.run([str(executable), "92681"], check=True)
        failure = subprocess.run(
            [str(executable), "92682"], capture_output=True, text=True, check=False
        )
        if failure.returncode == 0 or "signed integer overflow" not in failure.stderr:
            raise RuntimeError(f"Expected sanitizer refusal, got {failure}")
        print("Pinned native source: 92,681 residuals safe; 92,682 overflows.")
        print(failure.stderr.strip())


if __name__ == "__main__":
    main()

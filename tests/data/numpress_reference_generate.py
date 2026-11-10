"""Regenerate frozen answers with the original, pinned MS-Numpress C++ code.

Opt-in provenance tool, not used by pytest. Run from the checkout root:
    uv run --no-sync python tests/data/numpress_reference_generate.py
Requires HTTPS access to raw.githubusercontent.com and a C++ compiler. Downloads
are checked by SHA-256, held in memory, and compiled in a temporary directory
beneath this file. Neither pynumpress nor MSMCP participates in generation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

REVISION = "61acddbce26db586d1cf4e873f2acf225c9d8526"
ROOT = f"https://raw.githubusercontent.com/ms-numpress/ms-numpress/{REVISION}/"
SOURCES = {
    "src/main/cpp/MSNumpress.hpp": (
        "399edb6f33c4f950def439d12dae5eef0d883e05335d38cddf8e3072c012a7e8"
    ),
    "src/main/cpp/MSNumpress.cpp": (
        "55fdaac456ee9131bc0921918d71564937fc83e79d068dc53e493a08e12fe89f"
    ),
}

# The driver uses the pointer API with generously sized buffers. In particular,
# it avoids the reference vector wrappers' empty-vector and size_t-underflow
# hazards. Decoded values are printed with round-trip precision for binary64.
DRIVER = r"""
#include <iomanip>
#include <sstream>
#include <string>
int main() {
    std::string codec;
    double fp;
    size_t n;
    while (std::cin >> codec >> fp >> n) {
        std::vector<double> input(n + 1, 0.0), decoded(10*n + 100, 0.0);
        for (size_t i = 0; i < n; ++i) std::cin >> input[i];
        std::vector<unsigned char> bytes(8 + 5*n + 100, 0);
        size_t length = 0, count = 0;
        using namespace ms::numpress::MSNumpress;
        try {
            if (codec == "linear") {
                length = encodeLinear(input.data(), n, bytes.data(), fp);
                count = decodeLinear(bytes.data(), length, decoded.data());
            } else if (codec == "pic") {
                length = encodePic(input.data(), n, bytes.data());
                count = decodePic(bytes.data(), length, decoded.data());
            } else if (codec == "slof") {
                length = encodeSlof(input.data(), n, bytes.data(), fp);
                count = decodeSlof(bytes.data(), length, decoded.data());
            } else return 2;
        } catch (const char* error) {
            std::cerr << error << '\n';
            return 3;
        }
        std::cout << std::hex << std::setfill('0');
        if (length == 0) std::cout << '-';
        for (size_t i = 0; i < length; ++i)
            std::cout << std::setw(2) << static_cast<unsigned int>(bytes[i]);
        std::cout << std::dec << ' ' << count << std::setprecision(17);
        for (size_t i = 0; i < count; ++i) std::cout << ' ' << decoded[i];
        std::cout << '\n';
    }
}
"""

CASES: list[tuple[str, str, float, list[float]]] = [
    ("linear_empty", "linear", 1000.0, []),
    ("linear_single", "linear", 1000.0, [123.4564]),
    ("linear_unsigned_seeds", "linear", 1.0, [2147483648.0, 4294967295.0]),
    ("linear_padding", "linear", 1000.0, [100.0, 101.0, 102.0]),
    ("linear_no_padding", "linear", 1000.0, [100.0, 101.0, 102.0, 103.0]),
    (
        "linear_quantization_signed_residuals",
        "linear",
        1000.0,
        [100.0004, 100.1004, 100.1996, 100.3014, 100.3996],
    ),
    (
        "linear_accumulator_beyond_uint32",
        "linear",
        1.0,
        [4294967294.0, 4294967295.0, 4294967296.0, 4294967297.0],
    ),
    (
        "linear_signed_residual_limits",
        "linear",
        1.0,
        [0.0, 0.0, 2147483647.0, 2147483646.0],
    ),
    ("pic_empty", "pic", 0.0, []),
    ("pic_padding", "pic", 0.0, [0.0]),
    ("pic_no_padding", "pic", 0.0, [0.0, 0.0]),
    (
        "pic_nibble_widths",
        "pic",
        0.0,
        [
            0.0,
            1.0,
            15.0,
            16.0,
            255.0,
            256.0,
            4095.0,
            4096.0,
            65535.0,
            65536.0,
            268435455.0,
            268435456.0,
            2147483646.0,
        ],
    ),
    ("pic_half_up_rounding", "pic", 0.0, [0.0, 0.49, 0.5, 1.49, 1.5, 2.5]),
    ("pic_encoder_upper_bound", "pic", 0.0, [2147483646.5]),
    ("slof_empty", "slof", 1000.0, []),
    ("slof_quantization", "slof", 1000.0, [0.0, 0.001, 1.0, 10.0, 100.0, 1e6]),
    ("slof_uint16_byte_order", "slof", 4000.0, [0.0, 0.5, 1.0, 10.0, 100.0, 1e6]),
]


def main() -> None:
    source_text: list[str] = []
    for path, digest in SOURCES.items():
        with urllib.request.urlopen(ROOT + path, timeout=30) as response:
            content = response.read()
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            raise ValueError(f"{path}: expected {digest}, got {actual}")
        source_text.append(content.decode().replace('#include "MSNumpress.hpp"', ""))
    source = "\n".join([*source_text, DRIVER])
    request = "\n".join(
        f"{codec} {fp:.17g} {len(values)} "
        + " ".join(format(value, ".17g") for value in values)
        for _, codec, fp, values in CASES
    )
    with tempfile.TemporaryDirectory(
        prefix="numpress_build_", dir=Path(__file__).parent
    ) as d:
        executable = Path(d) / "numpress_reference"
        subprocess.run(
            ["c++", "-std=c++11", "-O0", "-x", "c++", "-", "-o", str(executable)],
            input=source,
            text=True,
            check=True,
            timeout=60,
        )
        output = subprocess.run(
            [str(executable)],
            input=request,
            text=True,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout.splitlines()
    if len(output) != len(CASES):
        raise ValueError("Reference driver returned the wrong number of answers")
    vectors = []
    for (name, codec, fp, values), line in zip(CASES, output, strict=True):
        hex_bytes, count, *numbers = line.split()
        decoded = [float(number) for number in numbers]
        if int(count) != len(values) or len(decoded) != len(values):
            raise ValueError(f"{name}: reference encoder/decoder disagreed on length")
        vectors.append(
            {
                "name": name,
                "codec": codec,
                "fixed_point": fp,
                "input": values,
                "hex": "" if hex_bytes == "-" else hex_bytes,
                "decoded": decoded,
            }
        )
    fixture = {
        "reference_revision": REVISION,
        "source_sha256": SOURCES,
        "vectors": vectors,
    }
    target = Path(__file__).with_name("numpress_known_answers.json")
    target.write_text(json.dumps(fixture, indent=2, allow_nan=False) + "\n")
    print(f"Wrote {len(vectors)} original-reference vectors to {target}")


if __name__ == "__main__":
    main()

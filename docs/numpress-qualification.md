# MS-Numpress qualification record (PR #61)

Evidence behind the optional MS-Numpress reader: what its preflight
guarantees before native decoding, and how each claim was checked. The
suites named here are the qualification lane; the native sanitizer
probe is opt-in and never runs under pytest.

## Scope and lineage

- PR branch `codex/issue-8-numpress`, base `main`, candidate `c0877a5`.
- Work happens in the isolated worktree
  `.worktrees/numpress-qualification`. The primary checkout is not the
  integration target and is not edited from here.
- Decoding needs the optional extra (`uv sync --extra numpress`).
  Without it the reader raises `MissingDependencyError` naming the extra.

## Reader corrections

Each item is a refusal or a bound in the Python reader, not a change to
the codec's numeric definition. The named test is the regression that
holds it.

| Behaviour | Where | Regression |
|---|---|---|
| zlib data that trails or follows the stream is refused, not partly accepted | `_zlib_decompress_bounded` (`unused_data`) | `test_corrupted_zlib_is_not_partially_accepted` |
| encoded input and the native reservation are bounded before base64 or native allocation | `_decode_binary`, `_validate_numpress` | `test_oversized_encoded_input_is_rejected_before_base64_allocation`, `test_native_reservation_bound_exact_threshold`, `test_reject_native_reservation_before_scanning_integer_stream`, `test_zlib_output_limit_precedes_native_allocation` |
| linear signed-64 predictor arithmetic is checked in Python before native decoding | `_numpress_integer_count(..., linear=True)` | `test_linear_signed64_overflow_refused_before_native`, `test_linear_near_signed64_boundary_is_not_rejected` |
| decoder results must be 1-D, the declared size, and finite | `_decode_binary` | `test_native_result_contract_is_checked` |
| contradictory precision, compression, and codec terms are refused | `_parse_binary_data_array` | `test_contradictory_numpress_array_metadata_is_refused` |
| duplicate m/z or intensity arrays are refused rather than last-wins | `_parse_spectrum` | `test_duplicate_numpress_array_roles_are_refused` |
| a declared-positive array that decodes to nothing is refused | `_parse_binary_data_array` | `test_numpress_declared_nonempty_but_empty_arrays_are_refused` |

Decoded values remain the codec's: quantization, nibble widths, byte
order, and fixed-point scaling are unchanged.

## Linear signed-64 preflight

pynumpress 0.1.5 keeps the linear predictor in C++ `long long` (pinned
revision `4164a1a1a59ab0aff72f7762bc92535912f2492c`, `MSNumpress.cpp`):

```c
ints[0] = ints[1];
ints[1] = ints[2];
decodeInt(data, &di, dataSize, &half, &buff);
diff = static_cast<int>(buff);
extrapol = ints[1] + (ints[1] - ints[0]);   // line 426
y = extrapol + diff;                        // line 427
```

Every residual fits in `int32`, but the predictor does not stay there.
With two zero seeds and a constant `INT32_MAX` residual, `y` grows
quadratically and leaves signed 64-bit range at 92,682 residuals. UBSan
reported that case as signed integer overflow at line 426 for a
417,085-byte payload: every residual is individually valid, so checking
residual format alone cannot catch it.

`_numpress_integer_count` reconstructs the two predictor states in
Python integers while it already walks the nibble stream, and raises
`MalformedFileError` when `current - previous`,
`current + (current - previous)`, or that plus the residual leaves
`[-2**63, 2**63 - 1]`. The reconstruction follows the native reader:
head nibble first, `head - 8` leading nibbles of `0xF` for a negative
width, payload nibbles least significant first, and the residual read as
a two's-complement `int32`. The seeds are `uint32` and each new state is
bounds-checked before it is stored, so the check is exact rather than
merely cautious: it accepts a stream precisely when no step of this
predictor can overflow natively.

It runs in `_validate_numpress`, before the decoder is resolved with
`getattr(decoder, "decode_linear")`. Both regressions enforce that
ordering. The overflow case replaces the `pynumpress` module's
`decode_linear` with one that fails the test if reached, so a native
call would be a test failure rather than a sanitizer finding; the
adjacent accepted case calls `_validate_numpress` directly.

Boundary evidence, for both residual signs:

| residuals | payload | result |
|---|---|---|
| 92,681 | 417,081 bytes | accepted, 92,683 values |
| 92,682 | 417,085 bytes | refused before native decoding |

## Verification

Run in this worktree with `uv run --no-sync`, which reuses the synced
environment instead of reaching the network:

```
uv run --no-sync ruff format --check .     # formatted
uv run --no-sync ruff check .              # all checks passed
uv run --no-sync mypy                      # 76 source files clean
uv run --no-sync basedpyright              # 0 errors, 0 warnings
uv run --no-sync pytest                    # 1083 passed, 18 skipped
uv run --no-sync pytest tests/test_eval_notebook.py -m eval -q   # 1 passed
```

The 18 skips are optional extras absent from this environment
(`imaging`, `scipy`, `rdkit`), not Numpress behaviour. `make lint`,
`make test`, and `make eval` run the same commands through
`uv sync --extra dev`.

## Reference fixtures and the native probe

- `tests/data/numpress_known_answers.json` holds 17 vectors produced by
  the original reference implementation at revision
  `61acddbce26db586d1cf4e873f2acf225c9d8526`, with source hashes
  recorded in the fixture. `tests/data/numpress_reference_generate.py`
  regenerates them and needs a C++ compiler;
  `tests/test_numpress_known_answers.py` only reads the JSON, so the
  normal run stays offline and needs no compiler.
- `tests/data/numpress_native_probe.py` is opt-in. It downloads the
  pinned pynumpress sources, verifies their SHA-256 digests, builds
  them with `-fsanitize=signed-integer-overflow`, and decodes the
  unsafe length only inside that instrumented subprocess. pytest never
  compiles, downloads, or decodes the overflowing stream.

## Remaining limits

- The guard covers the linear predictor, where the pinned decoder's only
  reachable signed overflow lives. PIC accumulates in `int32` and SLOF
  in `int16`; both are bounded by their residual widths and by the
  allocation checks, but neither has had the same sanitizer exercise.
- The sanitizer reproduction is a qualification record, not a CI step:
  it needs a compiler and network access, so it stays opt-in and
  hash-pinned.
- This document supports review of PR #61. Passing checks here are a
  local, worktree-scoped result, not a merge approval.

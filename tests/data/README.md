`mini_numpress.chrom.mzML` is the Numpress test file from
[`pymzML`](https://github.com/pymzml/pymzML), pinned at commit
`75154f43df27d67f4bc5c994cea16bf6856c0cbb` and distributed under the MIT
license reproduced in `mini_numpress.LICENSE.txt`.

Upstream file: 20,597 bytes, SHA-256
`88f98880d80c6efa0e9ddcf50eaf0c64ebc4fded5a4428f0212e68795f370594`.
Committed copy: 20,573 bytes, SHA-256
`8021d86ba97419f2125bc54ad7870b5c3892e1d5259e44cc56b1c94a65cd1512`. The only
change is that three OpenSWATH `userParam` values recording the upstream
author's home-directory prefix read `UPSTREAM_DIR/`,
as `tests/test_no_machine_paths.py` requires. Binary arrays and all other bytes
are unchanged.

It contains 176-element MS-Numpress linear and positive integer compression
chromatogram arrays. The SLOF regression case is generated deterministically in
the test because this small upstream file does not contain a SLOF array.

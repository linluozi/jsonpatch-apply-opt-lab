# jsonpatch-apply-opt-lab

`jsonpatch` applies RFC 6902 JSON patches; `JsonPatch.apply` copies the whole document and, in 1.47, recompiles every operation on every call. This patch makes the copy JSON-specialized (with a `deepcopy` fallback) and caches compiled operations behind a signature that still notices in-place patch edits.

Upstream: https://github.com/stefankoegl/python-json-patch

## Results (CPython 3.12.3, AMD EPYC 7K62, seed 20261118)

| scale | document | ops | baseline median | optimized median | speedup |
|---|---|---|---|---|---|
| small | 4 KB | 8 | 312.521 µs | 164.520 µs | **1.900x** |
| medium | 128 KB | 48 | 7,162.776 µs | 3,252.111 µs | **2.203x** |
| large | 1 MB | 160 | 58,760.117 µs | 27,635.474 µs | **2.126x** |
| real corpus | upstream `tests.js`, 59 valid cases | — | 13.361 µs/case | 9.746 µs/case | **1.371x** |

Median of the three synthetic scales: **2.126x**. The real-corpus number is the upstream suite shipped in `benchmark/corpus/tests.js`, applied once per case (not synthetic repetition). `benchmark/raw/` keeps all 56 per-sample files; medians and p25/p75 are recomputed from them by `bench.py`.

## Differential test

`tests/test_diff.py` deduplicates by sha256 of the canonical (document, patch) pair, then compares canonical bytes:

```
unique samples=1387 byte-identical=1364 same-error=23
```

1,387 independent samples (80 upstream suite cases + 14 boundary cases + random documents), 1,364 identical results, 23 cases where both implementations raise the same exception class. Edge coverage: empty doc/list, root replacement, UTF-8 keys and values, NaN/Infinity, -0.0, 2^63 bounds, 60-level nesting, 3,000-element arrays, shared subtrees, self-referential documents, and in-place edits of `patch.patch` and operation `key` setters.

## Gates

| gate | result | reproduce |
|---|---|---|
| G1 upstream tests | 371 passed, 448 subtests, 0 failed | `cd /path/to/python-json-patch-1.47 && PYTHONPATH=/path/to/jsonpatch-apply-opt-lab/src python -m pytest tests.py property_tests.py -q` |
| G2 differential | 1,387 unique, 0 byte mismatches | `python tests/test_diff.py` |
| G3 median speedup | 2.126x (≥1.15x) | `python benchmark/bench.py --impl both` |
| G3b real corpus | 1.371x (≥1.15x) | same command, `real-corpus` row |
| G4 large scale | 2.126x (≥0.95x) | same command, `large` row |
| G5 peak RSS | 30,152 KB vs 30,400 KB = 0.992x | `/usr/bin/time -v python benchmark/bench.py --impl baseline` (then `--impl optimized`) |

Only CPython 3.12.3 exists on this machine; 3.10/3.11 are not installed, so no cross-interpreter numbers are reported.

## Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install jsonpatch==1.47 pytest
.venv/bin/python tests/test_diff.py && .venv/bin/python benchmark/bench.py --impl both
cd /path/to/python-json-patch-1.47 && git apply --check /path/to/jsonpatch-apply-opt-lab/patch.diff
```

## Environment

CPU: AMD EPYC 7K62 48-Core (4 vCPU) · RAM: 3,723 MB · Python 3.12.3 · `jsonpointer` from the baseline install · seed 20261118 · timer `time.perf_counter()` · 3 warmup + 7 timed samples per scale.

Relative standard deviation is below 10% everywhere except the optimized real-corpus row (12.2%), where each sample mixes 59 documents of very different sizes and allocation patterns; the raw samples and p25/p75 are kept so this is auditable.

## 本轮创新

Previous work on this account optimized happy-path hot loops; this is the first measurement that splits `JsonPatch.apply` cost into **per-call operation recompilation** and **whole-document copying**, and the first fix that keeps the compiled cache coherent with in-place patch edits (signature over `op`/`path` only, because `value` is read from the same dict at apply time). It is not "another upstream, same template": the invalidation contract and the aliasing/cycle-preserving copier are specific to the RFC 6902 apply pipeline, and the upstream suite (24.7% copy share) versus a 1 MB document (copy-dominated) shows that a single workload cannot justify either optimization.

## License

BSD-3-Clause, original copyright (c) 2011 Stefan Kögl; see `LICENSE`. `src/jsonpatch.py` is upstream 1.47 with the change in `patch.diff`; `benchmark/corpus/tests.js` is the upstream test fixture. Change scope: `patch.diff` (+60/−6 lines), no public API change.

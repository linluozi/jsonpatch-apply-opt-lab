#!/usr/bin/env python3
"""Benchmark jsonpatch.JsonPatch.apply: baseline 1.47 vs the patched module.

Both implementations are loaded side by side: the baseline from the installed
``jsonpatch`` package (pip install jsonpatch==1.47) and the optimized one from
``../src/jsonpatch.py``.  Every sample is written to ``raw/`` before it is
aggregated, so the reported median/p25/p75 can be recomputed from the raw data.

    python benchmark/bench.py --impl both --out-dir benchmark
"""
import argparse
import importlib.util
import json
import os
import platform
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "jsonpatch.py"
CORPUS = ROOT / "benchmark" / "corpus" / "tests.js"
RAW = ROOT / "benchmark" / "raw"
SEED = 20261118
WARMUP = 3
SAMPLES = 7

# (document target bytes, number of operations)
SCALES = {
    "small": (4 * 1024, 8),
    "medium": (128 * 1024, 48),
    "large": (1024 * 1024, 160),
}


def load_baseline():
    import jsonpatch  # installed distribution, pinned by README

    if jsonpatch.__version__ != "1.47":
        raise SystemExit(
            "baseline must be jsonpatch 1.47, got %s" % jsonpatch.__version__
        )
    return jsonpatch


def load_optimized():
    spec = importlib.util.spec_from_file_location("jsonpatch_optimized", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_document(target_bytes, rng):
    """Builds a JSON document shaped like a service config with records."""
    sample_record = {
        "id": 0,
        "name": "record-00000",
        "tags": ["alpha", "beta", "gamma"],
        "payload": {
            "text": "x" * 40,
            "values": [0, 1, 2, {"deep": 0}],
            "ok": False,
        },
        "score": 0.0,
    }
    record_bytes = len(json.dumps(sample_record)) + 2
    count = max(1, target_bytes // record_bytes)
    doc = {
        "service": "bench",
        "version": 3,
        "flags": {"enabled": True, "ratio": 0.25, "note": None},
        "records": [],
    }
    for i in range(count):
        doc["records"].append(
            {
                "id": i,
                "name": "record-%05d" % i,
                "tags": ["alpha", "beta", "gamma"][: (i % 3) + 1],
                "payload": {
                    "text": "x" * 40,
                    "values": [i, i + 1, i + 2, {"deep": i % 7}],
                    "ok": bool(i % 2),
                },
                "score": (i % 100) / 10.0,
            }
        )
    return doc


def build_patch(doc, n_ops):
    """A deterministic patch whose operations stay valid in any order.

    The replaced and tested leaves are disjoint, and the two container paths
    used by ``add``/``copy`` are created first, so every operation is valid
    against the original document and against the result of its predecessors.
    """
    leaves = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                child = path + "/" + str(key)
                if isinstance(value, (dict, list)):
                    walk(value, child)
                else:
                    leaves.append(child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child = path + "/" + str(index)
                if isinstance(value, (dict, list)):
                    walk(value, child)
                else:
                    leaves.append(child)

    walk(doc, "")
    # Spread the selected leaves over the whole document instead of taking a
    # prefix, so replacements exercise every nesting level and record.
    stride = max(1, len(leaves) // max(1, n_ops * 3))
    selected = leaves[::stride]
    replace_paths = selected[0::2]
    test_paths = selected[1::2]
    if not replace_paths:
        replace_paths = leaves[:1]
    if not test_paths:
        test_paths = leaves[-1:]

    ops = [
        {"op": "add", "path": "/scratch", "value": {}},
        {"op": "add", "path": "/copied", "value": {}},
    ]
    for i in range(n_ops):
        kind = i % 4
        if kind == 0:
            ops.append(
                {"op": "replace", "path": replace_paths[i % len(replace_paths)],
                 "value": {"n": i, "text": "replacement-%d" % i}}
            )
        elif kind == 1:
            ops.append({"op": "add", "path": "/scratch/k%d" % i, "value": [i, i + 1]})
        elif kind == 2:
            path = test_paths[i % len(test_paths)]
            ops.append({"op": "test", "path": path, "value": lookup(doc, path)})
        else:
            source = test_paths[i % len(test_paths)]
            ops.append({"op": "copy", "from": source, "path": "/copied/c%d" % i})
    return ops


def lookup(doc, pointer):
    node = doc
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def time_calls(fn, reps):
    start = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - start) / reps


def measure(impl_name, module, scale, doc, patch):
    patch_obj = module.JsonPatch(patch)
    fn = lambda: patch_obj.apply(doc)  # noqa: E731
    reps = {"small": 20, "medium": 8, "large": 1}[scale]
    for _ in range(WARMUP):
        time_calls(fn, reps)
    samples = []
    for i in range(SAMPLES):
        seconds = time_calls(fn, reps)
        record = {
            "impl": impl_name,
            "scale": scale,
            "sample": i,
            "reps": reps,
            "seconds_per_call": seconds,
            "doc_bytes": len(json.dumps(doc)),
            "ops": len(patch),
            "seed": SEED,
            "timer": "time.perf_counter",
        }
        (RAW / ("%s-%s-%d.json" % (impl_name, scale, i))).write_text(
            json.dumps(record, sort_keys=True) + "\n"
        )
        samples.append(seconds)
    return summarize(impl_name, scale, samples, len(json.dumps(doc)), len(patch))


def measure_corpus(impl_name, module):
    cases = json.loads(CORPUS.read_text())
    usable = []
    for case in cases:
        try:
            patch = module.JsonPatch(case["patch"])
            patch.apply(case["doc"])
        except Exception:
            continue  # the suite also contains cases that must be rejected
        usable.append((case["doc"], patch))
    fn = lambda: [p.apply(d) for d, p in usable]  # noqa: E731
    reps = 1
    for _ in range(WARMUP):
        time_calls(fn, reps)
    samples = []
    for i in range(SAMPLES):
        seconds = time_calls(fn, reps)
        record = {
            "impl": impl_name,
            "scale": "real-corpus",
            "sample": i,
            "reps": reps,
            "seconds_per_call": seconds,
            "cases": len(usable),
            "seconds_per_case": seconds / len(usable),
            "seed": SEED,
            "timer": "time.perf_counter",
        }
        (RAW / ("%s-real-corpus-%d.json" % (impl_name, i))).write_text(
            json.dumps(record, sort_keys=True) + "\n"
        )
        samples.append(seconds / len(usable))
    return summarize(
        impl_name, "real-corpus", samples, None, None, cases=len(usable)
    )


def summarize(impl, scale, samples, doc_bytes, ops, cases=None):
    ordered = sorted(samples)
    q = statistics.quantiles(ordered, n=4)
    return {
        "impl": impl,
        "scale": scale,
        "doc_bytes": doc_bytes,
        "ops": ops,
        "cases": cases,
        "samples": len(samples),
        "median_seconds": statistics.median(samples),
        "p25_seconds": q[0],
        "p75_seconds": q[2],
        "min_seconds": min(samples),
        "max_seconds": max(samples),
        "stdev_seconds": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "relative_stdev": (
            statistics.stdev(samples) / statistics.median(samples)
            if len(samples) > 1 and statistics.median(samples)
            else 0.0
        ),
    }


def environment():
    model = "unknown"
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    mem_kb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            mem_kb = int(line.split()[1])
            break
    return {
        "cpu": model,
        "cpu_count": os.cpu_count(),
        "mem_total_kb": mem_kb,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "seed": SEED,
        "warmup": WARMUP,
        "samples": SAMPLES,
        "command": " ".join(sys.argv),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--impl", choices=["baseline", "optimized", "both"], default="both")
    parser.add_argument("--out-dir", default=str(ROOT / "benchmark"))
    args = parser.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir)
    modules = {}
    if args.impl in ("baseline", "both"):
        modules["baseline"] = load_baseline()
    if args.impl in ("optimized", "both"):
        modules["optimized"] = load_optimized()

    rng = random.Random(SEED)
    documents = {}
    patches = {}
    for scale, (target, n_ops) in SCALES.items():
        doc = build_document(target, rng)
        documents[scale] = doc
        patches[scale] = build_patch(doc, n_ops)

    for impl_name, module in modules.items():
        results = []
        for scale in SCALES:
            results.append(
                measure(impl_name, module, scale, documents[scale], patches[scale])
            )
        results.append(measure_corpus(impl_name, module))
        payload = {
            "impl": impl_name,
            "module_file": getattr(module, "__file__", None),
            "version": getattr(module, "__version__", None),
            "environment": environment(),
            "results": results,
        }
        (out_dir / ("%s.json" % impl_name)).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        for row in results:
            print(
                "%-9s %-11s median %10.3f us  p25/p75 %.3f/%.3f us  rel_stdev %.3f"
                % (
                    impl_name,
                    row["scale"],
                    row["median_seconds"] * 1e6,
                    row["p25_seconds"] * 1e6,
                    row["p75_seconds"] * 1e6,
                    row["relative_stdev"],
                )
            )


if __name__ == "__main__":
    main()

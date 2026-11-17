#!/usr/bin/env python3
"""Differential test: jsonpatch 1.47 vs the patched module.

Every case is applied through both implementations and the results are
compared as canonical bytes.  Cases are deduplicated by sha256 over
(document, patch); the test reports the *independent* sample count, not the
number of loop iterations.  Edge cases cover empty documents, root
replacement, non-ASCII, NaN/Infinity, -0.0, huge integers, deep nesting,
aliasing and cycles.
"""
import copy
import hashlib
import importlib.util
import json
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "jsonpatch.py"
CORPUS = ROOT / "benchmark" / "corpus" / "tests.js"
SEED = 20261118


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_baseline():
    import jsonpatch

    assert jsonpatch.__version__ == "1.47"
    return jsonpatch


BASE = load_baseline()
OPT = load("jsonpatch_optimized", SRC)


def norm(value, seen=None):
    """Type-preserving canonical form; distinguishes 1/True/-0.0/NaN/keys."""
    if seen is None:
        seen = set()
    kind = type(value)
    if value is None:
        return ["none"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", value]
    if kind is float:
        return ["float", repr(value)]
    if kind is str:
        return ["str", value]
    if kind is dict or kind is list:
        marker = id(value)
        if marker in seen:
            return ["cycle", marker]  # only used by the cycle test
        seen = seen | {marker}
        if kind is dict:
            return [
                "dict",
                sorted(
                    [[type(k).__name__, repr(k), norm(v, seen)]
                     for k, v in value.items()],
                    key=lambda item: item[1],
                ),
            ]
        return ["list", [norm(v, seen) for v in value]]
    return ["repr", repr(value)]


def canon(value):
    return json.dumps(
        norm(value), ensure_ascii=False, sort_keys=True, allow_nan=True
    ).encode("utf-8")


def signature(doc, patch):
    return hashlib.sha256(canon(doc) + b"\x00" + canon(patch)).hexdigest()


# ---------------------------------------------------------------- generators
UNICODE = ["", "plain", "日本語", "naïve", "a/b~c", "\u0000x", "emoji 🧪", "x" * 300]
KEYS = ["a", "b", "c", "ключ", "日本", "a/b", "a~b", "0", ""]


def random_scalar(rng):
    return rng.choice(
        [
            None,
            True,
            False,
            0,
            1,
            -1,
            2**63 - 1,
            -(2**63),
            -0.0,
            0.5,
            float("nan"),
            float("inf"),
            rng.choice(UNICODE),
            rng.random(),
        ]
    )


def random_json(rng, depth):
    if depth <= 0:
        return random_scalar(rng)
    kind = rng.random()
    if kind < 0.45:
        node = {}
        for _ in range(rng.randint(0, 4)):
            node[rng.choice(KEYS)] = random_json(rng, depth - 1)
        return node
    if kind < 0.85:
        return [random_json(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    return random_scalar(rng)


def locations(node, path="", parent=None):
    """Yields (path, node, parent) for every existing node."""
    yield path, node, parent
    if isinstance(node, dict):
        for key, value in node.items():
            yield from locations(value, path + "/" + str(key), node)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from locations(value, path + "/" + str(index), node)


def choose_op(rng, doc):
    nodes = list(locations(doc))
    containers = [(p, v) for p, v, parent in nodes if isinstance(v, (dict, list))]
    existing = [(p, v) for p, v, parent in nodes if p != ""]
    if not existing:
        return None
    kind = rng.choice(["add", "replace", "remove", "test", "copy", "move"])
    if kind == "add":
        if not containers:
            return None
        parent_path, parent = rng.choice(containers)
        if isinstance(parent, dict):
            key = rng.choice(KEYS)
            return {"op": "add", "path": parent_path + "/" + key,
                    "value": random_json(rng, 1)}
        return {"op": "add", "path": parent_path + "/-",
                "value": random_json(rng, 1)}
    path, value = rng.choice(existing)
    if kind == "replace":
        return {"op": "replace", "path": path, "value": random_json(rng, 1)}
    if kind == "remove":
        return {"op": "remove", "path": path}
    if kind == "test":
        return {"op": "test", "path": path, "value": copy.deepcopy(value)}
    # copy / move: pick a destination that is valid add syntax
    if not containers:
        return None
    parent_path, parent = rng.choice(containers)
    if isinstance(parent, dict):
        dest = parent_path + "/" + rng.choice(KEYS)
    else:
        dest = parent_path + "/-"
    if kind == "copy":
        return {"op": "copy", "from": path, "path": dest}
    if dest.startswith(path + "/"):
        return None
    return {"op": "move", "from": path, "path": dest}


def make_case(rng):
    doc = random_json(rng, rng.choice([1, 2, 3]))
    if not isinstance(doc, (dict, list)):
        doc = {"root": doc}
    shadow = copy.deepcopy(doc)
    ops = []
    for _ in range(rng.randint(1, 4)):
        op = choose_op(rng, shadow)
        if op is None:
            continue
        try:
            shadow = BASE.apply_patch(shadow, [op])
        except Exception:
            continue
        ops.append(op)
    return doc, ops


def spec_cases():
    cases = json.loads(CORPUS.read_text())
    return [(case["doc"], case["patch"]) for case in cases]


def edge_cases():
    deep = {"leaf": 1}
    for _ in range(60):
        deep = {"n": deep}
    shared = [1, 2, 3]
    return [
        ({}, [{"op": "add", "path": "/a", "value": 1}]),
        ([], [{"op": "add", "path": "/-", "value": {"日本": "値"}}]),
        ({"a": 1}, [{"op": "replace", "path": "", "value": [1, 2]}]),
        ({"a": 1}, [{"op": "remove", "path": "/a"}]),
        ({"a": 1}, [{"op": "test", "path": "/a", "value": 1}]),
        ({"日本": {"a/b~c": [None, True, False]}},
         [{"op": "replace", "path": "/日本/a~1b~0c/0", "value": "x" * 500}]),
        ({"a": float("nan")}, [{"op": "test", "path": "/a", "value": float("nan")}]),
        ({"a": [float("inf"), -0.0, -(2**63)]},
         [{"op": "copy", "from": "/a", "path": "/b"}]),
        ({"a": {"b": {"c": {"d": 1}}}}, [{"op": "move", "from": "/a/b",
                                          "path": "/a/b/c/moved"}]),
        ({"a": shared, "b": shared}, [{"op": "replace", "path": "/a/0",
                                       "value": 9}]),
        (deep, [{"op": "replace", "path": "/" + "/".join(["n"] * 60) + "/leaf",
                 "value": 2}]),
        ({"a": [0] * 3000}, [{"op": "add", "path": "/a/-", "value": 1}]),
        ({"": {"": 1}}, [{"op": "replace", "path": "/", "value": 2}]),
        ({"a": 1}, [{"op": "add", "path": "/b", "value": 2},
                    {"op": "copy", "from": "/b", "path": "/c"},
                    {"op": "move", "from": "/c", "path": "/d"},
                    {"op": "remove", "path": "/a"}]),
    ]


def compare(doc, patch):
    try:
        expected = BASE.JsonPatch(patch).apply(doc)
    except Exception as exc:  # both sides must reject the same way
        try:
            OPT.JsonPatch(patch).apply(doc)
        except Exception as other:
            return ("error", type(exc).__name__, type(other).__name__)
        return ("error-mismatch", type(exc).__name__, "no error")
    actual = OPT.JsonPatch(patch).apply(doc)
    return ("ok", canon(expected) == canon(actual))


class DifferentialTest(unittest.TestCase):
    def test_corpus_and_random_bytes(self):
        cases = edge_cases() + spec_cases()
        rng = random.Random(SEED)
        for _ in range(1600):
            cases.append(make_case(rng))

        unique = {}
        for doc, patch in cases:
            unique.setdefault(signature(doc, patch), (doc, patch))

        compared = 0
        errors = 0
        for doc, patch in unique.values():
            status, *rest = compare(doc, patch)
            if status == "ok":
                self.assertTrue(rest[0], "byte mismatch for %r %r" % (doc, patch))
                compared += 1
            elif status == "error":
                self.assertEqual(rest[0], rest[1], "error mismatch")
                errors += 1
            else:
                self.fail("baseline rejected but optimized accepted: %r %r"
                          % (doc, patch))

        print("unique samples=%d byte-identical=%d same-error=%d"
              % (len(unique), compared, errors))
        self.assertGreaterEqual(len(unique), 1000)
        self.assertGreaterEqual(compared, 1000)

    def test_result_does_not_alias_input(self):
        doc = {"a": {"b": [1, 2, {"c": 3}]}, "d": [4]}
        patch = [{"op": "replace", "path": "/a/b/0", "value": 9}]
        for module in (BASE, OPT):
            result = module.apply_patch(doc, patch)
            self.assertIsNot(result, doc)
            result["a"]["b"][2]["c"] = 99
            result["d"].append(5)
            self.assertEqual(doc, {"a": {"b": [1, 2, {"c": 3}]}, "d": [4]})

    def test_cycles_preserved(self):
        doc = {"a": [1]}
        doc["self"] = doc
        for module in (BASE, OPT):
            result = module.apply_patch(doc, [{"op": "add", "path": "/b", "value": 2}])
            self.assertIs(result["self"], result)
            self.assertEqual(result["b"], 2)

    def test_shared_subtrees_preserved(self):
        shared = [1, {"x": 2}]
        doc = {"a": shared, "b": shared}
        for module in (BASE, OPT):
            result = module.apply_patch(doc, [{"op": "add", "path": "/c", "value": 0}])
            self.assertIs(result["a"], result["b"])
            self.assertIsNot(result["a"], shared)

    def test_patch_edits_are_visible(self):
        patch = [{"op": "add", "path": "/a", "value": 1}]
        for module in (BASE, OPT):
            compiled = module.JsonPatch(patch)
            compiled.apply({})
            # edit the exposed patch document in place
            compiled.patch[0]["path"] = "/b"
            self.assertEqual(compiled.apply({}), {"b": 1})
            # edit through an operation object's setter
            op = compiled._ops[0]
            op.key = "c"
            self.assertEqual(compiled.apply({}), {"c": 1})

    def test_aliasing_inside_result(self):
        value = {"k": [1]}
        doc = {"a": value, "b": value}
        for module in (BASE, OPT):
            result = module.apply_patch(doc, [{"op": "add", "path": "/c", "value": 1}])
            self.assertIs(result["a"], result["b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

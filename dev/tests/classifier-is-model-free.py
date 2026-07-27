#!/usr/bin/env python3
"""V-GAT-017 (the L0 half): the risk classifier has no model in it, and cannot grow one.

09 lists V-GAT-017 at `L0, L1`. The L1 half lives in classify_test.go and asserts the observable
consequence -- permute everything a model could influence and the classification comes out
byte-identical. That half is the one that matters, and it is also the half that can only ever test
the inputs somebody thought to permute.

This half tests the shape instead, and it is the cheaper of the two by a wide margin: the classifier
decides whether a human is asked before an agent changes a cluster, so the one thing it must never
do is ask a language model what it thinks. A closed allowlist over the package's imports is how that
stays true after everyone who agreed to it has moved on. The failure being prevented is not somebody
importing an SDK on purpose; it is a plausible refactor -- "let the classifier summarise the diff
for the approval prompt" -- that reaches for a chat client and quietly makes the gate a model's
opinion.

Three properties, each a different way the same thing goes wrong:

  1. CLOSED IMPORT ALLOWLIST. Every non-stdlib import is named below. An inference client, an HTTP
     client, a Kubernetes client -- none of them can appear without editing this file, which is a
     conversation rather than a diff nobody read.
  2. NO NONDETERMINISM FROM THE STANDARD LIBRARY EITHER. `time`, `math/rand`, `os` and `net/http`
     are as fatal to "the same envelope always classifies the same way" as an LLM is, and they are
     far likelier to arrive by accident. A classifier that reads the clock gates differently at
     3am.
  3. NO PROSE ON THE INPUT TYPE. `input.go` is the classifier's whole input surface. A field named
     `Intent`, `Rationale` or `Justification` on it is model-authored text, and model-authored text
     reaching a security decision is the thing V-GAT-017 exists to forbid -- the compiler enforces
     it today only because no such field exists, and that is not enforcement.

Run:  python3 dev/tests/classifier-is-model-free.py
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO / "k8s-operator" / "internal" / "broker" / "classify"
INPUT_GO = PKG / "input.go"

# Every third-party and first-party import the classifier is allowed. Deliberately short, and
# deliberately without a Kubernetes client of any kind: the classifier is handed already-resolved
# facts (see resolve.go) precisely so that it cannot go and look anything up.
ALLOWED_NON_STDLIB = {
    "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1",
    "github.com/gke-labs/kube-agents/k8s-operator/internal/scope",
    "k8s.io/apimachinery/pkg/apis/meta/v1",
    "k8s.io/apimachinery/pkg/labels",
}

# Standard-library packages that are not an inference client but destroy the same property.
FORBIDDEN_STDLIB = {
    "time": "a classifier that reads the clock gates differently at 3am",
    "math/rand": "a classification must not depend on a random draw",
    "os": "environment and filesystem are inputs the corpus cannot pin",
    "os/exec": "a subprocess is an unbounded input",
    "net/http": "the classifier is handed resolved facts; it does not fetch any",
    "net/rpc": "the classifier is handed resolved facts; it does not fetch any",
}

# Substrings that name an inference or chat client. Checked in addition to the allowlist so the
# failure message says what is actually wrong rather than "not in the list".
INFERENCE_MARKERS = (
    "genai",
    "generativelanguage",
    "vertexai",
    "aiplatform",
    "openai",
    "anthropic",
    "langchain",
    "llm",
    "ollama",
    "bedrock",
)

# Field names on the classifier's input surface that would be model-authored prose.
PROSE_FIELDS = {
    "intent",
    "rationale",
    "justification",
    "explanation",
    "reasoning",
    "prompt",
    "message",
    "text",
    "notes",
    "comment",
    "summary",
    "description",
    "requester",
    "trigger",
}

IMPORT_BLOCK = re.compile(r"^import \(\s*$(.*?)^\)\s*$", re.S | re.M)
IMPORT_LINE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)
IMPORT_SINGLE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
STRUCT_FIELD = re.compile(r"^\t([A-Z]\w*)\s+\S", re.M)

failures: list[str] = []


def imports_of(path: pathlib.Path) -> set[str]:
    text = path.read_text()
    found: set[str] = set()
    for block in IMPORT_BLOCK.findall(text):
        found.update(IMPORT_LINE.findall(block))
    found.update(IMPORT_SINGLE.findall(text))
    return found


def main() -> int:
    sources = sorted(p for p in PKG.glob("*.go") if not p.name.endswith("_test.go"))
    if not sources:
        print(f"FAIL: no non-test sources under {PKG.relative_to(REPO)}", file=sys.stderr)
        return 1

    for src in sources:
        rel = src.relative_to(REPO)
        for imp in sorted(imports_of(src)):
            lowered = imp.lower()
            marker = next((m for m in INFERENCE_MARKERS if m in lowered), None)
            if marker:
                failures.append(
                    f"{rel} imports {imp!r} -- that is an inference client, and the classifier "
                    f"decides whether a human is asked before a cluster changes (matched {marker!r})"
                )
                continue
            if imp in FORBIDDEN_STDLIB:
                failures.append(f"{rel} imports {imp!r}: {FORBIDDEN_STDLIB[imp]}")
                continue
            if "." not in imp.split("/")[0]:
                continue  # stdlib, and not on the forbidden list
            if imp not in ALLOWED_NON_STDLIB:
                failures.append(
                    f"{rel} imports {imp!r}, which is not in this check's allowlist. If the "
                    "classifier genuinely needs it, add it here and say why in the review -- the "
                    "point of the list is that widening it is a decision rather than a diff"
                )

    # 3. The input surface carries no prose.
    if not INPUT_GO.exists():
        failures.append(f"{INPUT_GO.relative_to(REPO)} does not exist; V-GAT-017 has no input surface to check")
    else:
        for field in STRUCT_FIELD.findall(INPUT_GO.read_text()):
            if field.lower() in PROSE_FIELDS:
                failures.append(
                    f"{INPUT_GO.relative_to(REPO)} declares a field named {field!r}. The classifier's "
                    "input surface carries facts, never prose: a free-text field is model-authored, "
                    "and a security decision that reads model-authored text is the model deciding"
                )

    if failures:
        print("FAIL: V-GAT-017 -- the classifier is not model-free", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: V-GAT-017 -- {len(sources)} classifier sources, "
        f"{len(ALLOWED_NON_STDLIB)} allowed non-stdlib imports, no prose on the input surface"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

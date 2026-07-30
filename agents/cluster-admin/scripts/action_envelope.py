"""Build the ActionEnvelope an agent POSTs to its own broker (06 §4.1, 06 §9).

This module is the agent-side half of a contract whose other half is Go, in
`k8s-operator/internal/broker`. It exists because 06 §9 puts the envelope construction --
including `idempotencyKey` -- in the `submit_action` / `plan_action` MCP tools, and the agent image
is Python.

WHY A SECOND IMPLEMENTATION IS ALLOWED HERE, AND WHAT MAKES IT SAFE
-------------------------------------------------------------------
A second definition site of a security rule is normally the bug. This one is unavoidable and is
therefore *joined* rather than merely duplicated:

  * The broker never trusts the key it is sent. `CompareIdempotencyKey` recomputes it over
    {agentIdentity, dryRun, operations} and refuses a mismatch -- because a caller-chosen key is a
    dedup oracle in both directions (suppress someone else's write; replay your own twice).
  * So a divergence of one byte between this file and the Go does not degrade gracefully. It makes
    every write from every agent in the fleet refused, reported as a key mismatch rather than as
    the drift it is.
  * The join is **V-BRK-028**: `dev/test_action_envelope.py` runs this module over
    `verification/fixtures/envelopes/valid/`, the same six envelopes `TestValidFixtureIdempotencyKeys`
    pins the Go side against, and asserts the same six keys. Each fixture carries the key its own
    operations hash to, so there is no golden file on either side and nothing to drift independently.

Three things a re-implementation gets wrong, all of them covered by that corpus:

  1. **The sanitizer runs before the hash.** The key is computed over operations that have been
     through the journal's §4.3.1 redaction, so a Secret's `data` values are sha256 digests by the
     time they reach the canonicaliser. Skip it and you get a different key *and* credential
     material in the hash input.
  2. **The operations are sorted**, by `op + US + group/version/kind/namespace/name`, so that a
     retry which reorders them (the caller is an LLM) is the same write.
  3. **`agentIdentity` is the agent's SCOPE identity** -- `platform/adamparco-kage`,
     `developer-team/<project>/<cluster>/<namespace>` -- and not the `system:serviceaccount:...`
     username the broker authenticates. Those are different strings for the same caller; the second
     one produces keys nothing accepts.

Nothing here talks to the network, reads a credential, or imports anything outside the standard
library. Transport is `submit_action`'s problem, not this module's.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

# --- the enums and limits, mirrored from 06 §4.1 --------------------------------------------------
# Duplicated deliberately and joined the same way as the key: `dev/test_action_envelope.py` reads
# `validOps` and friends out of `envelope.go` and asserts these sets match. A local copy that no
# check compares is how a verb the broker accepts becomes one the agent cannot spell.

API_VERSION = "kubeagents.x-k8s.io/v1alpha1"
ENVELOPE_KIND = "ActionEnvelope"

KEY_PREFIX = "sha256:"

VALID_OPS = frozenset({"create", "apply", "patch", "delete", "scale"})
VALID_PATCH_TYPES = frozenset(
    {
        "application/merge-patch+json",
        "application/json-patch+json",
        "application/apply-patch+yaml",
    }
)
VALID_REQUESTER_KINDS = frozenset({"human", "agent", "system"})
# The three below arrived in P9-T8b-4c. `envelope.go` had six closed enums and this file mirrored
# three; the join in `dev/test_action_envelope.py` was three hand-written tests naming those same
# three, so the set it could not see was exactly the set it did not check. It cost a live 400: the
# first in-cluster probe sent `trigger.source: "verification"`, which reads as a perfectly good
# word and is not one of the seven. The empty string is a MEMBER of two of these, not an oversight
# -- `platform` and `propagationPolicy` are both optional, and Go spells "absent" as `""`.
VALID_TRIGGER_SOURCES = frozenset({"chat", "watch", "alert", "cron", "delegation", "escalation", "undo"})
VALID_PLATFORMS = frozenset({"", "slack", "googlechat", "kubectl", "mesh"})
VALID_PROPAGATION = frozenset({"", "Foreground", "Background", "Orphan"})

MAX_INTENT_LEN = 512
MAX_RATIONALE_LEN = 4096
MAX_OPERATIONS = 50

_HEX32 = re.compile(r"^[0-9a-f]{32}$")

# The fields the broker's `keyOperation` carries, in the order it declares them. Anything not on
# this list is dropped before hashing -- which is what the Go side does too, by decoding the wire
# JSON into a struct. A caller that adds a field gets the key the broker will compute, not a
# different one.
_KEY_OPERATION_FIELDS = (
    "op",
    "target",
    "targetSelector",
    "cloudTarget",
    "desiredState",
    "patch",
    "delete",
    "scale",
)


class EnvelopeError(ValueError):
    """A caller-side refusal. Raised before anything is sent, never after."""


# --- RFC 8785 (JCS) -------------------------------------------------------------------------------


def canonicalize(value: Any) -> bytes:
    """Serialize `value` to RFC 8785 canonical JSON.

    Mirrors `broker.Canonicalize`. Two rules carry all the risk and both are easy to get wrong in
    Python specifically:

      * Object keys sort by **UTF-16 code unit**, not by code point and not by UTF-8 byte. Python's
        `sorted()` compares code points, which disagrees with UTF-16 for everything above U+FFFF
        versus the U+E000..U+FFFF range -- an emoji key sorts *after* U+FFFD by code point and
        *before* it by UTF-16 surrogate pair.
      * Numbers render by ECMAScript `Number::toString`, not by `repr` and not by `json.dumps`.
    """
    buf: list[str] = []
    _write(buf, value)
    return "".join(buf).encode("utf-8")


def _write(buf: list[str], v: Any) -> None:
    if v is None:
        buf.append("null")
    elif v is True:
        buf.append("true")
    elif v is False:
        buf.append("false")
    elif isinstance(v, str):
        buf.append(_canonical_string(v))
    elif isinstance(v, (int, float)):
        # int goes through float64 as well: the Go side re-decodes its own marshalled bytes with
        # `UseNumber` and then calls `Float64()`, so an integer beyond 2^53 loses precision there
        # too. Matching the lossy path is the point.
        buf.append(_es_number(float(v)))
    elif isinstance(v, (list, tuple)):
        buf.append("[")
        for i, e in enumerate(v):
            if i:
                buf.append(",")
            _write(buf, e)
        buf.append("]")
    elif isinstance(v, dict):
        buf.append("{")
        for i, k in enumerate(sorted(v, key=_utf16_key)):
            if not isinstance(k, str):
                raise EnvelopeError(f"jcs: object key {k!r} is not a string")
            if i:
                buf.append(",")
            buf.append(_canonical_string(k))
            buf.append(":")
            _write(buf, v[k])
        buf.append("}")
    else:
        # Refusing beats coercing: a canonicaliser that guesses produces a key that is stable in
        # this interpreter and different in the next.
        raise EnvelopeError(f"jcs: unsupported value of type {type(v).__name__}")


def _utf16_key(s: str) -> tuple[int, ...]:
    """The UTF-16 code unit sequence of `s`, as a tuple that sorts lexicographically.

    Not `sorted(keys)`. Python compares code points, and for code points in [U+E000, U+FFFF] versus
    the supplementary planes the two orders are opposite: U+FFFD is one code unit 0xFFFD, while
    U+1F600 is the surrogate pair D83D DE00 -- greater by code point, lesser by UTF-16. Two keys
    straddling that boundary sort one way here and the other way in the Go, which is a silent key
    divergence on exactly the inputs nobody tests by hand.
    """
    raw = s.encode("utf-16-be")
    return tuple(int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2))


_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _canonical_string(s: str) -> str:
    out = ['"']
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < "\x20":
            out.append("\\u%04x" % ord(ch))
        else:
            # Emitted raw, as UTF-8. JCS does not escape non-ASCII.
            out.append(ch)
    out.append('"')
    return "".join(out)


def _es_number(f: float) -> str:
    """ECMAScript `Number::toString(x)` base 10, which RFC 8785 §3.2.2.3 adopts by reference."""
    if f != f or f in (float("inf"), float("-inf")):
        raise EnvelopeError(f"jcs: {f} is not a finite number")
    if f == 0:
        # Covers -0.0: ECMAScript renders negative zero as "0", so `-0` and `0` canonicalize to the
        # same text. Without this branch the sign handling below would emit "-0" and split two
        # documents JCS considers identical.
        return "0"

    neg = f < 0
    if neg:
        f = -f

    # `repr` is the shortest decimal string that round-trips, which is exactly what Go's
    # `FormatFloat(f, 'e', -1, 64)` is defined to produce -- only spelled differently.
    text = repr(f)
    mantissa, _, exp_text = text.partition("e")
    exp10 = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")

    digits = int_part + frac_part
    pos = len(int_part) + exp10  # value == 0.<digits> x 10^pos

    lead = len(digits) - len(digits.lstrip("0"))
    digits = digits.lstrip("0").rstrip("0")
    pos -= lead
    if not digits:  # unreachable: f == 0 returned above
        return "0"

    k = len(digits)
    if k <= pos <= 21:
        out = digits + "0" * (pos - k)
    elif 0 < pos <= 21:
        out = digits[:pos] + "." + digits[pos:]
    elif -6 < pos <= 0:
        out = "0." + "0" * (-pos) + digits
    else:
        e = pos - 1
        sign = "-" if e < 0 else "+"
        e = abs(e)
        head = digits if k == 1 else digits[:1] + "." + digits[1:]
        out = f"{head}e{sign}{e}"

    return "-" + out if neg else out


# --- the §4.3.1 sanitizer -------------------------------------------------------------------------


def digest(body: bytes) -> str:
    """`journal.Digest` -- bare lowercase hex, no prefix."""
    return hashlib.sha256(body).hexdigest()


def _marshal_for_digest(value: Any) -> bytes:
    """Reproduce Go's `json.Marshal` for the values the digest paths see.

    Go sorts map keys and emits no spaces; `json.dumps` with `sort_keys` and tight separators is
    the same bytes for every shape that reaches here.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sanitize(obj: dict[str, Any]) -> dict[str, Any]:
    """`journal.Sanitize` -- strip what may not be journaled, digest what may not be seen.

    Two removals for two reasons, then the Secret rule. Applied here because the idempotency key is
    computed over the sanitized operation: the key has to be stable against the redaction the
    journal performs, or a record's stored payload and the payload its key was computed over
    describe different writes.
    """
    out = json.loads(json.dumps(obj))  # deep copy, same as Go's DeepCopy

    metadata = out.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("managedFields", None)
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)

    if out.get("kind") == "Secret":
        for field in ("data", "stringData"):
            raw = out.get(field)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise EnvelopeError(f"sanitize: Secret {field} is not an object")
            digested: dict[str, Any] = {}
            for k, v in raw.items():
                if isinstance(v, str):
                    digested[k] = KEY_PREFIX + digest(v.encode("utf-8"))
                else:
                    # Not a Secret in the shape we think it is. Digest its JSON rather than pass it
                    # through: the failure mode being guarded is "material reached the journal", and
                    # an unexpected type is not a reason to relax it.
                    digested[k] = KEY_PREFIX + digest(_marshal_for_digest(v))
            out[field] = digested

    return out


def _sanitize_payload(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Sanitize a bare operation payload, injecting `kind` when the payload omits it.

    The injection is load-bearing. `Sanitize` decides whether to digest `data`/`stringData` by
    reading the object's own `kind`, and an operation payload frequently has none -- a merge-patch
    body is `{"data": {...}}` with no apiVersion and no kind at all. Without this, a Secret patch
    passes through undigested and the key is computed over credential material.

    It is removed again afterwards so the injected field cannot become part of the key: a payload
    that declared its kind and one that relied on the target's must produce the same key.
    """
    injected = False
    if not payload.get("kind") and kind:
        payload = dict(payload)
        payload["kind"] = kind
        injected = True

    clean = sanitize(payload)
    if injected:
        clean.pop("kind", None)
    return clean


def _sanitize_patch_body(patch: dict[str, Any], kind: str) -> Any:
    """The three patch media types.

    Merge-patch and apply-patch bodies are objects and go straight through. A JSON Patch body is an
    ARRAY, which `Sanitize` has no shape for, so each op's `value` is digested individually when the
    op writes under a Secret's `/data` or `/stringData`. This is the one place redaction is applied
    by path rather than by field, because in a JSON Patch the field name lives in `path` and
    nowhere else.
    """
    body = patch.get("body")

    if isinstance(body, dict):
        return _sanitize_payload(body, kind)

    if isinstance(body, list):
        if kind != "Secret":
            return body
        out = []
        for entry in body:
            if not isinstance(entry, dict):
                out.append(entry)
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not (path.startswith("/data/") or path.startswith("/stringData/")):
                out.append(entry)
                continue
            if "value" not in entry:
                # A `remove` op names a key but carries no material.
                out.append(entry)
                continue
            digested = dict(entry)
            digested["value"] = KEY_PREFIX + digest(_marshal_for_digest(entry["value"]))
            out.append(digested)
        return out

    return body


# --- the key --------------------------------------------------------------------------------------


def _omitempty(value: Any) -> bool:
    """Go's `omitempty` for the field types `keyOperation` uses: "", nil, empty map, empty slice."""
    return value is None or value == "" or value == {} or value == []


def _project(source: Any, fields: tuple[tuple[str, bool], ...]) -> dict[str, Any] | None:
    """Copy the named fields, dropping the `omitempty` ones that are empty.

    `fields` is (name, omitempty). Projection rather than passthrough because the Go side decodes
    the wire JSON into a struct, which silently drops anything it has no field for -- so an
    envelope carrying an extra key must hash as if it did not.
    """
    if source is None:
        return None
    if not isinstance(source, dict):
        raise EnvelopeError(f"expected an object, got {type(source).__name__}")
    out: dict[str, Any] = {}
    for name, omitempty in fields:
        value = source.get(name)
        if omitempty and _omitempty(value):
            continue
        out[name] = value
    return out


_TARGET_FIELDS = (("group", True), ("version", False), ("kind", False), ("namespace", True), ("name", False))
_SELECTOR_FIELDS = (("group", True), ("version", False), ("kind", False), ("namespace", False), ("labelSelector", False))
_CLOUD_FIELDS = (("provider", False), ("service", False), ("resource", False), ("method", False))
_DELETE_FIELDS = (("propagationPolicy", True), ("gracePeriodSeconds", True), ("preconditions", True))
_PRECONDITION_FIELDS = (("uid", True), ("resourceVersion", True))
_SCALE_FIELDS = (("replicas", False),)


def _reduce_for_key(op: dict[str, Any]) -> dict[str, Any]:
    """`reduceForKey` -- an operation cut down to what identifies the write, payloads sanitized."""
    if not isinstance(op, dict):
        raise EnvelopeError(f"operation must be an object, got {type(op).__name__}")

    out: dict[str, Any] = {"op": op.get("op") or ""}

    target = _project(op.get("target"), _TARGET_FIELDS)
    selector = _project(op.get("targetSelector"), _SELECTOR_FIELDS)
    cloud = _project(op.get("cloudTarget"), _CLOUD_FIELDS)

    if target is not None:
        out["target"] = target
    if selector is not None:
        out["targetSelector"] = selector
    if cloud is not None:
        out["cloudTarget"] = cloud

    delete = _project(op.get("delete"), _DELETE_FIELDS)
    if delete is not None:
        preconditions = _project(delete.get("preconditions"), _PRECONDITION_FIELDS)
        if preconditions is None:
            delete.pop("preconditions", None)
        else:
            delete["preconditions"] = preconditions
        out["delete"] = delete

    scale = _project(op.get("scale"), _SCALE_FIELDS)
    if scale is not None:
        out["scale"] = scale

    kind = ""
    if target is not None:
        kind = target.get("kind") or ""
    elif selector is not None:
        kind = selector.get("kind") or ""

    desired = op.get("desiredState")
    if isinstance(desired, dict) and desired:
        out["desiredState"] = _sanitize_payload(desired, kind)

    patch = op.get("patch")
    if isinstance(patch, dict):
        out["patch"] = {"type": patch.get("type") or "", "body": _sanitize_patch_body(patch, kind)}

    return out


_US = "\x1f"


def operation_sort_key(op: dict[str, Any]) -> str:
    """The ordering of 06 §4.1: `op + US + group/version/kind/namespace/name`.

    US (0x1F) rather than a printable delimiter because it cannot occur in any of the fields it
    joins -- a `/` or `:` can, and a separator that appears inside a field makes two distinct
    operations produce the same sort key.

    A selector and a cloud target have no `name`, so they fill the last slot with the thing that
    identifies them. Without that, two selector operations differing only in their selector would
    sort equal, and the sort's stability -- i.e. the caller's original order -- would leak into the
    key.
    """
    group = version = kind = namespace = name = ""
    if op.get("target") is not None:
        t = op["target"]
        group, version, kind = t.get("group", ""), t.get("version", ""), t.get("kind", "")
        namespace, name = t.get("namespace", ""), t.get("name", "")
    elif op.get("targetSelector") is not None:
        s = op["targetSelector"]
        group, version, kind = s.get("group", ""), s.get("version", ""), s.get("kind", "")
        namespace, name = s.get("namespace", ""), s.get("labelSelector", "")
    elif op.get("cloudTarget") is not None:
        c = op["cloudTarget"]
        group, kind = c.get("provider", ""), c.get("service", "")
        name = f"{c.get('resource', '')}#{c.get('method', '')}"

    return f"{op.get('op', '')}{_US}{group}/{version}/{kind}/{namespace}/{name}"


def compute_idempotency_key(agent_identity: str, operations: list[dict[str, Any]], dry_run: bool = False) -> str:
    """Derive the key from the authenticated identity and the operations, per 06 §4.1.

    `agent_identity` is the agent's **scope identity** (`platform/<project>`,
    `<tier>/<project>/<cluster>/<namespace>`), which is what the broker hashes -- not the
    `system:serviceaccount:...` username it authenticates.
    """
    if not agent_identity:
        # Not a caller error: it means the config that carries the scope identity was not loaded.
        # A key computed over an empty identity would collide across every agent in the cluster.
        raise EnvelopeError("cannot compute an idempotency key without an agent identity")

    reduced = [_reduce_for_key(op) for op in operations]
    # Sorted, so the key does not depend on the order the caller happened to list the operations in.
    reduced.sort(key=operation_sort_key)

    canonical = canonicalize({"agentIdentity": agent_identity, "dryRun": bool(dry_run), "operations": reduced})
    return KEY_PREFIX + hashlib.sha256(canonical).hexdigest()


# --- the envelope ---------------------------------------------------------------------------------


def new_nonce() -> str:
    """A 32-hex-character nonce, for callers that have no broker-issued one.

    The broker's `GET /v1alpha1/nonce` is the real source and its value is the one to use: it is
    bound to the caller and has a TTL the anti-replay guard tracks. This exists so a test, a
    dry-run preview, or a validation pass can build a well-formed envelope without a round trip.
    """
    return secrets.token_hex(16)


def utc_now() -> str:
    """`issuedAt` in the one spelling the broker accepts: RFC 3339, UTC, trailing `Z`.

    An offset is refused there even though it names the same instant, because every downstream
    reader compares these as strings and two spellings that sort differently is a bug that only
    surfaces in an incident timeline.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(
    *,
    agent_identity: str,
    intent: str,
    operations: list[dict[str, Any]],
    requester: dict[str, Any],
    trigger: dict[str, Any],
    trace: dict[str, Any],
    nonce: str,
    rationale: str = "",
    dry_run: bool = False,
    require_approval: bool = False,
    max_objects: int | None = None,
    deadline_seconds: int | None = None,
    issued_at: str | None = None,
) -> dict[str, Any]:
    """Assemble a complete, well-formed ActionEnvelope with its `idempotencyKey` computed.

    What this deliberately does NOT do: decide anything. There is no tier, no scope, no risk class
    and no approval state here, and the broker refuses those names outright -- 03 §4.1 step 1
    derives (tier, scope) from the authenticated identity, so an envelope field claiming either
    could only ever be an attempt to override it. `agent_identity` is an input to the *hash*, never
    a claim on the wire; it does not appear in the returned envelope.
    """
    _check_client_side(intent, rationale, operations, requester, trigger, trace, nonce)

    envelope: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": ENVELOPE_KIND,
        "intent": intent,
        "operations": operations,
        "requester": requester,
        "trigger": trigger,
        "trace": trace,
        "issuedAt": issued_at or utc_now(),
        "nonce": nonce,
        "idempotencyKey": compute_idempotency_key(agent_identity, operations, dry_run),
    }
    if rationale:
        envelope["rationale"] = rationale
    if dry_run:
        envelope["dryRun"] = True
    if require_approval:
        envelope["requireApproval"] = True
    if max_objects is not None:
        envelope["maxObjects"] = max_objects
    if deadline_seconds is not None:
        envelope["deadlineSeconds"] = deadline_seconds
    return envelope


def _check_client_side(
    intent: str,
    rationale: str,
    operations: list[dict[str, Any]],
    requester: dict[str, Any],
    trigger: dict[str, Any],
    trace: dict[str, Any],
    nonce: str,
) -> None:
    """Refuse locally what the broker would refuse remotely, for the reasons a caller can act on.

    This is a courtesy, not a control: the broker validates everything again and its answer is the
    only one that counts. The point is that an agent which mis-spells `op` gets told which field,
    on the spot, instead of an HTTP 400 several turns later with the reasoning that produced it
    already out of context.
    """
    if not intent:
        raise EnvelopeError("intent is required")
    if len(intent) > MAX_INTENT_LEN:
        raise EnvelopeError(f"intent is {len(intent)} characters, the limit is {MAX_INTENT_LEN}")
    if len(rationale) > MAX_RATIONALE_LEN:
        raise EnvelopeError(f"rationale is {len(rationale)} characters, the limit is {MAX_RATIONALE_LEN}")

    if not operations:
        raise EnvelopeError("at least one operation is required")
    if len(operations) > MAX_OPERATIONS:
        raise EnvelopeError(f"{len(operations)} operations, the limit is {MAX_OPERATIONS}")

    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            raise EnvelopeError(f"operation {i} must be an object")
        verb = op.get("op")
        if verb not in VALID_OPS:
            raise EnvelopeError(f"operation {i}: op {verb!r} is not one of {sorted(VALID_OPS)}")
        shapes = [k for k in ("target", "targetSelector", "cloudTarget") if op.get(k) is not None]
        if len(shapes) != 1:
            # Both readings of a two-shape operation are defensible, and picking one silently is
            # how a single-object patch becomes a fan-out.
            raise EnvelopeError(
                f"operation {i}: exactly one of target/targetSelector/cloudTarget is required, got {shapes or 'none'}"
            )
        patch = op.get("patch")
        if isinstance(patch, dict) and patch.get("type") not in VALID_PATCH_TYPES:
            raise EnvelopeError(f"operation {i}: patch type {patch.get('type')!r} is not one of {sorted(VALID_PATCH_TYPES)}")
        # `delete`, not `deleteOptions` -- the json tag on `broker.Operation`, which is the name
        # that has to be right for the field to arrive at all.
        delete = op.get("delete")
        if isinstance(delete, dict) and (delete.get("propagationPolicy") or "") not in VALID_PROPAGATION:
            raise EnvelopeError(
                f"operation {i}: propagationPolicy {delete.get('propagationPolicy')!r} is not one of {sorted(VALID_PROPAGATION)}"
            )

    if requester.get("kind") not in VALID_REQUESTER_KINDS:
        raise EnvelopeError(f"requester.kind must be one of {sorted(VALID_REQUESTER_KINDS)}")
    if not requester.get("id"):
        raise EnvelopeError("requester.id is required")
    if (requester.get("platform") or "") not in VALID_PLATFORMS:
        raise EnvelopeError(f"requester.platform must be one of {sorted(VALID_PLATFORMS)}")
    if not trigger.get("source"):
        raise EnvelopeError("trigger.source is required")
    if trigger.get("source") not in VALID_TRIGGER_SOURCES:
        # Kept separate from the required-ness check above so the message says which of the two
        # went wrong. 01 §7's autonomy metrics are computed by grouping on this field, so a source
        # outside the seven is not a typo the broker can absorb -- it is a row nothing counts.
        raise EnvelopeError(f"trigger.source {trigger.get('source')!r} is not one of {sorted(VALID_TRIGGER_SOURCES)}")
    if not _HEX32.match(trace.get("traceId") or ""):
        raise EnvelopeError("trace.traceId must be 32 lowercase hex characters")
    if not _HEX32.match(nonce or ""):
        raise EnvelopeError("nonce must be 32 lowercase hex characters -- fetch one from GET /v1alpha1/nonce")

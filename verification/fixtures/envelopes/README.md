# Action Envelope fixtures

The 09 §7.2 fixture set for the Action Broker's request contract (06 §4.1). Three directories,
because 09 §7.2 asks for three kinds and each proves something the others cannot:

| Directory    | What it proves                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------------- |
| `valid/`     | A well-formed envelope from each tier decodes, validates, and its idempotency key reproduces byte-for-byte |
| `malformed/` | A broken envelope is refused with the **specific** reason, not a generic one                               |
| `spoofing/`  | An envelope reaching for authority it does not have is refused **loudly** — journaled and alarmed          |

## The naming convention is the assertion

Every file under `malformed/` and `spoofing/` is named `<reason>.<description>.json`, where
`<reason>` is the exact refusal reason the broker must return. The test derives the expectation
from the filename, so a fixture cannot drift from what it asserts: renaming the file changes the
assertion, and adding a file adds a case with no test edit at all.

`spoofing/` fixtures carry a second, stronger expectation the test applies to the whole directory:
each must be refused with `Journal` **and** `SecurityEvent` set. A reserved key that is merely
dropped, or refused quietly, leaves no evidence that anything tried — and the attempt is the
evidence. See `ReservedKeys` in `internal/broker/envelope.go`.

Note `reserved-key.namespace-shadowing-a-legal-target.json`. It carries a top-level `namespace`
alongside a perfectly legitimate `operations[0].target.namespace`. Only the top level is reserved
(06 §4.1: "reserved **top-level** keys"), and a check that refused both would break every real
envelope while a check that refused neither would let a caller assert its own scope.

## `valid/identities.json`

Maps each valid fixture to the `<tier>/<scope>` identity it was submitted under. The
`idempotencyKey` in each valid fixture is the **real** key for that identity — computed by
`broker.ComputeIdempotencyKey`, not a placeholder.

That makes these fixtures a golden test of the RFC 8785 canonicaliser. 06 §4.1 specifies JCS
precisely because "`json.Marshal` with sorted keys" differs from it on numbers and escapes, and a
broker that recomputes the key differently from the client that sent it refuses every legitimate
submission. If a change to `internal/broker/jcs.go` alters the output for any of these bodies, six
keys change and the test says so.

The identities are deliberately three different tiers with three different scope depths
(`platform/<project>`, `cluster-admin/<project>/<cluster>`,
`developer-team/<project>/<cluster>/<namespace>`), because the identity is an input to the key and
a bug that only shows up at one depth would otherwise hide.

`developer-team.apply-secret.json` exists to pin one specific behaviour: a Secret payload is
digested per key by `journal.Sanitize` before it reaches the hash, so credential material never
enters the key computation. Change the sanitizer and that fixture's key moves.

## Adding a fixture

Drop a file in the right directory with the right name. Nothing else. For a new `valid/` fixture,
add its identity to `identities.json` and run the test once to obtain the key — the failure prints
the computed value.

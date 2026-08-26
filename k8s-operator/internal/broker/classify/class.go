// Package classify is the risk classifier of 03 §5 and 06 §4.2: deterministic code in the broker
// that decides, for one envelope, whether it executes now, executes and pings, waits for a human,
// or is refused.
//
// # This package is not internal/router/classify
//
// There are two things called `classify` in this tree. internal/router/classify.go sorts CHAT
// EVENTS -- is this message a question, a command, noise. This package sorts WRITES. They share a
// verb and nothing else, and a plausible-looking import of the wrong one compiles: both offer a
// `Classify` on a request-shaped argument. 06 §4.2 is the contract for this one.
//
// # The classifier never reads prose
//
// Its inputs are the envelope's TARGETS and LIVE CLUSTER STATE. Not `intent`, not `rationale`, not
// `requester.displayName`, not anything a model wrote (V-GAT-017). This is the property that makes
// the class a control rather than a suggestion: an attacker who can influence the agent's prose --
// which, for an LLM reading cluster events and chat, is anyone who can write a pod annotation --
// could otherwise talk any action down to `routine`. The Input type below is the enforcement: it
// carries no prose field, so reading one is not an oversight a reviewer has to catch, it is a
// compile error.
package classify

import "fmt"

// Class is the four-valued risk class of 03 §5.1, ordered. The order IS the semantics: step 3 of
// 06 §4.2's evaluation takes the MAXIMUM over every input, so a rule can only ever raise the
// result. There is no downgrade operator in this package and adding one would defeat
// `ChangePolicy`'s stricter-only guarantee at the source.
type Class int

const (
	// ClassRoutine executes immediately and appears in the periodic digest.
	ClassRoutine Class = iota
	// ClassElevated executes immediately, notifies the owning humans at once with the undo handle,
	// and gets the longer undo retention.
	ClassElevated
	// ClassGated does NOT execute. It parks as PendingApproval and expires on the roster's TTL.
	ClassGated
	// ClassForbidden is refused outright, emits a security event, and has no approval path at all.
	// It is reachable only from steps 1 and 2 (scope, forbidden set) and from an object override --
	// never by escalation, because `+1` is capped at gated.
	ClassForbidden
)

// classNames is indexed by Class and is the wire form used in `ActionRecord`, the corpus fixtures
// and the human-facing reasons. Kept as a slice rather than a map so a new Class that forgets to
// add a name panics in tests instead of rendering as "".
var classNames = []string{"routine", "elevated", "gated", "forbidden"}

// String renders the wire form. A Class outside the enum renders visibly rather than silently:
// a classification that prints "class(7)" in a journal is a bug someone will report, and one that
// prints "routine" is a bug nobody will.
func (c Class) String() string {
	if c < 0 || int(c) >= len(classNames) {
		return fmt.Sprintf("class(%d)", int(c))
	}
	return classNames[c]
}

// ParseClass converts the wire form. The empty string is NOT routine -- an absent class in a rule
// or a fixture is a missing field, and defaulting it to the most permissive value is the exact
// error this function exists to make impossible.
func ParseClass(s string) (Class, error) {
	for i, name := range classNames {
		if s == name {
			return Class(i), nil
		}
	}
	return 0, fmt.Errorf("unknown class %q (want one of routine, elevated, gated, forbidden)", s)
}

// Max returns the stricter of two classes. This is the only combinator in the package.
func Max(a, b Class) Class {
	if b > a {
		return b
	}
	return a
}

// Escalate is the `+1` of 06 §4.2 step 4: one class stricter, CAPPED AT GATED.
//
// The cap is the whole design of the operator. `forbidden` means "no path through an agent at all,
// not even with a human saying yes" (03 §3.3), and it is a code constant. If `+1` could reach it,
// then a production label plus a novel action -- two escalations that individually mean "be
// careful" -- would compose into "this action does not exist", and a customer labelling their
// namespaces would find gated work becoming impossible rather than reviewable. Escalation makes
// things need a human; only the forbidden set removes the human's option.
func Escalate(c Class) Class {
	if c >= ClassGated {
		return c
	}
	return c + 1
}

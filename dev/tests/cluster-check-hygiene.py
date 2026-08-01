#!/usr/bin/env python3
"""Four ways a cluster-facing check script is silently wrong -- about the cluster, or about itself.

Properties 1 and 2 come from defects found in `dev/verify/brake-fanout-l2.sh` while it was being
written (P9-T7c-3c-ii-b-2-b). Properties 3 and 4 come from defects found in
`dev/verify/webhook-negatives-l2.sh` (P9-T11g-4) and `dev/verify/startup-ordering-l2.sh` (P9-T11h),
both on live runs, both by a human reading a transcript. None of the four was a defect in the
product; all four were defects in the check, and all four are the kind that a passing run cannot
distinguish from a correct one. They share a scanner because they share the part that must not go
stale: the *discovery* of which scripts talk to a cluster. A property asserted over a set that
quietly stopped growing is LSN-036.

Properties 1 and 2 ask whether a script asked the cluster the wrong question. Properties 3 and 4 ask
something narrower and worse: whether a bash construct has left the suite unable to report what it
found. A suite that cannot go red and a suite that is passing produce the same bytes; so do a suite
that cannot see the cluster and a suite watching a cluster that is genuinely broken. Neither is
visible from the exit code, which is the only thing `dev/L2-CHAIN.txt` and the verification ledger
consume.

--------------------------------------------------------------------------------------------------
PROPERTY 1 (LSN-044) -- `auth can-i` takes a resource, optionally a NAME, and never a subresource
--------------------------------------------------------------------------------------------------

`kubectl auth can-i <verb> <TYPE>/<NAME>` parses the slash as naming an OBJECT. So

    kubectl auth can-i patch actionrecords.kubeagents.x-k8s.io/status --as=$SA

asks whether the subject may patch an ActionRecord literally *named* `status`. The subresource form
is `--subresource=status`. The command is well-formed, exits 0, and prints a plausible answer.

The reason this is worth a gate rather than a code review note: `actionrecords/status` is exactly
how the ClusterRole, the ValidatingAdmissionPolicy and every comment in the tree spell that
subresource, so transcribing it into `auth can-i` is the natural motion. In P9-T7c-3c-ii-b-2-b it
happened to land on a positive assertion, where the wrong answer is a red. **The direction that
matters is the negative one.** A `want_no` written that way draws its `no` from a resource name
nobody was ever granted rather than from the policy under test -- it would pass against a
ClusterRole with `verbs: ["*"]` on the subresource, and it is how an entire authority boundary gets
"proven" by a check that never asked the question.

Two halves, because a resource is either statically resolvable or it is not:

  1a. RESOLVABLE. No positional word of an `auth can-i` invocation may contain `/`. A blanket ban
      rather than a "known subresource names" list on purpose: the failure is silent, the legitimate
      `TYPE/NAME` form has never once been used in this repository, and a list of subresource names
      is a headcount (LSN-036). If a genuine `TYPE/NAME` query is ever needed, add it to
      `LITERAL_EXEMPT` below with the reason -- a visible diff, which is the point.

      Resolvable includes `for`-lists. `for pair in "impersonate users" "escalate roles.rbac..."`
      feeding `auth can-i $pair` is an established idiom in `verify-phase2.sh` and
      `verify-phase3.sh`; those lists are enumerated and each expansion checked, so those sites get
      the strict treatment rather than the weaker runtime-guard treatment below.

  1b. COMPUTED. A script that passes a resource this file cannot resolve must carry the guard at
      runtime instead: a `*/*)` case arm that refuses the malformed shape. This is the `can()`
      helper in `brake-fanout-l2.sh`, and it is strictly stronger than 1a where it applies, since it
      also catches a slash that only appears at runtime. Without it, 1a is trivially evaded by
      assigning the string to a variable first -- not maliciously, just by refactoring a repeated
      query into a helper, which is exactly what happened here.

--------------------------------------------------------------------------------------------------
PROPERTY 2 (LSN-045) -- a script that writes to an append-only journal cannot delete its namespace
--------------------------------------------------------------------------------------------------

`kube-agents-journal-retention` denies DELETE of an `ActionRecord` to every principal except the
retention controller and the operator, **and denies it even to them unless
`status.exported.confirmed` is true**. The namespace controller is not on that list. On a cluster
with no audit sink nothing ever confirms an export -- `journal_reconciler.go` logs that the record
"will be retained indefinitely because the export is the durable record (05 §1.2)" -- so a namespace
holding one can never finish terminating, and its name can never be reused.

The suite that found this passed on run 1 and could not create its namespace on run 2. That is the
worst available failure mode for a check: correct-looking evidence, produced once, from a fixture
path that cannot be repeated. A re-run is how a green result is distinguished from a lucky one.

The tempting fix is a one-liner -- patch `status.exported.confirmed: true` onto the suite's own
records and the namespace frees itself. It is also writing an export confirmation for an export that
never happened, in shipped tooling, where it becomes the idiom the next suite copies. Declined; see
the ledger's Decisions table for 2026-07-29. The supported shape is: reuse the namespace, delete
only the objects inside it, mint identifiers per run, and select Events by `involvedObject.uid` so
residue from a previous run cannot satisfy an assertion.

**The protected set is derived, not listed.** It comes from the ValidatingAdmissionPolicies
themselves -- every resource any policy matches for `DELETE` -- and the Kind is then read out of the
CRD that declares that plural. Add a second retention policy over a second resource and this check
starts enforcing it the same day, with no edit here. That derivation is the whole difference between
this and a check that knows about ActionRecords.

--------------------------------------------------------------------------------------------------
PROPERTY 3 (LSN-064) -- a failure flag assigned inside a pipeline is assigned in a subshell
--------------------------------------------------------------------------------------------------

Nearly every assertion in `webhook-negatives-l2.sh` is invoked as `child_yaml ... | reject ...`. In
bash **every** component of a pipeline runs in a subshell, so the `fail=1` inside `reject` and
`admit` was set in a child process and discarded when it exited. The suite printed four `FAIL:`
lines and exited **0** under a `PASS` banner. Thirty-one assertions, no way to report any of them --
and `V-CTR-002` had been recorded green on that exit code since P8-T9.

    $ bash -c 'fail=0; bad(){ fail=1; }; echo x | bad; echo "fail=$fail"'
    fail=0

Nothing was wrong with the arms; all 31 really did pass. That is the point. A suite that cannot fail
is indistinguishable, from the outside, from a suite that is passing, forever. It was found only
because a new check ID was being bound and the binding required a negative control.

WHY THIS FLAGS THE LAST COMPONENT TOO. The rule "an assignment in a non-final pipeline component is
lost" is the `shopt -s lastpipe` reading. Bash's default -- and no script in this tree sets
`lastpipe`, which the scan below confirms per script rather than assuming -- runs **every**
component in a subshell, including the last, which is exactly where `reject` sat. A property that
exempted the final component would have been green on the defect it was written for. Where a script
does set `lastpipe`, the final component is exempt and the others are not.

WHY THE FAILURE-CARRYING VARIABLE IS DERIVED AND NOT THE STRING "fail". A check that greps for
`fail=` is a headcount of one (LSN-036), and it goes blind the first time a suite calls its
accumulator `problems`, `n_bad`, `violations` or `errs` -- all of which occur in this tree. Worse,
it goes blind *silently*, in the same shape as the defect it is looking for. The candidate set is
therefore read out of each script: names that the script's own exit path depends on (the operand of
`exit`, or a variable tested by a `[ ]` / `[[ ]]` / `(( ))` on an exit-bearing line or in the
condition of an `if` whose block exits), unioned with names that read as a failure flag, and
intersected with the names the script actually assigns. Add a suite with a differently-named
counter and this property covers it the day it lands.

THE ACCEPTED SAFE SHAPE, WHICH MUST NOT BE FLAGGED. The fix was not to restructure 31 call sites --
that is 31 chances to get it wrong plus the 32nd assertion someone adds next year. It was a `mktemp`
FAILFILE appended to at the single choke point every assertion already goes through, because a
**file survives the subshell** and the exit code is derived from it once at the end. Two scripts use
that shape today. It is not flagged for two independent reasons: appending to a file is not a
variable assignment, and a name bound to a `mktemp` result is dropped from the candidate set below,
since it is a handle rather than a flag.

--------------------------------------------------------------------------------------------------
PROPERTY 4 (LSN-065) -- an assignment prefix is in effect while a redirection operand is expanded
--------------------------------------------------------------------------------------------------

`startup-ordering-l2.sh` reported, from a live cluster, that the agent pod had no `.status.phase`,
that its container published no `restartCount` and that the Agent CR carried none of 08 §7(c)'s
conditions -- while the same three reads, from another shell, returned `Running`, `0` and
`AgentReady=True` instantly. Four call sites had this shape:

    IFS=$'\t' read -r phase restarts <<<"$(pod_transcript "$agent_pod")"

A variable assignment written as a **command prefix is already in effect while the redirection
operand is expanded**, so `pod_transcript` ran with `IFS=<tab>`. Every helper in that file reaches
the cluster through unquoted `$K`, which holds `kubectl --context gke-scratch-kube-agents-dev`;
unquoted expansion splits on `IFS`, and with no space in `IFS` that is **one word** -- a command
named `kubectl --context gke-scratch-kube-agents-dev`, which does not exist. The `2>/dev/null` every
status read carries ate the `command not found` and the function returned empty strings.

    K="echo hello"; f() { $K world; }
    out="$(f)"; IFS=$'\t' read -r a <<<"$out"   # a=[hello world]
    IFS=$'\t' read -r b <<<"$(f)"               # bash: echo hello: command not found · b=[]

WHY IT IS WORSE THAN A WRONG ANSWER. The failure produces exactly the output shape the suite exists
to report: an empty phase renders as "the agent pod is in phase '<unreadable>', not Running", absent
conditions render as `AgentReady=<unset>` under a paragraph quoting 08 §2.4 on fleets blinded by a
broker outage. A blind instrument and a broken product are the same four lines of red. Two sibling
helpers in the same file are character-for-character the same read against the same pod and both
passed, because they are called without the prefix.

The property is the general one the lesson asks for, not the `IFS` one: **an assignment prefix on a
command whose redirection operand is computed is a scope bug whether or not the variable is `IFS`.**
"Computed" means a here-string or an unquoted-delimiter here-document whose text contains `$(`, a
backtick or `${`, or an input redirection from a process substitution. The fix at all four sites was
to substitute into a plain variable first and read from the variable; `chaos-suite.sh` already had
that shape (`IFS=: read ... <<<"$_pair"`) and is not flagged, because a plain parameter reference is
not a computation and nothing runs while it is expanded.

ONE SITE IN THE TREE STILL HAS THIS SHAPE. `actor-grant-sweep-l2.sh:704` reads a row with
`IFS=$'\x1f' read ... <<<"$(printf '%s' "$1" | tr '\t' '\037')"` -- the same construct, found by
this property on its first run, in a file nobody had suspected. It is survivable rather than
latent-fatal only by accident: the substitution runs `printf` and `tr` as absolute-resolvable
external commands rather than through the unquoted `$K` that made the startup-ordering case
catastrophic. It is waived, not exempted -- `PREFIX_REDIRECT_WAIVERS` below carries the reason, the
waiver is keyed on the script so fixing the site makes the waiver stale and the check red, and a
passing run PRINTS the waived count so a green here is never confused with a clean tree.

--------------------------------------------------------------------------------------------------
WHY PROPERTIES 3 AND 4 GET A TOKENIZER AND THEIR FLOORS SIT ON THE SCANNER, NOT THE FINDINGS
--------------------------------------------------------------------------------------------------

Both are shape properties over shell syntax, and both are trivially evaded by a line-oriented regex:
a `|` inside a here-document's YAML block scalar, a `case` pattern alternation, an apostrophe in a
trailing comment that swallows the rest of the file. `_lex` below is a small quote-aware tokenizer
that consumes here-document bodies, `$( )`, `${ }`, backticks and process substitutions as opaque
spans, and treats an unquoted `#` at a word boundary as a comment -- which is strictly more accurate
than the whole-line `strip_comments` properties 1 and 2 use, and is why 3 and 4 read the raw source.

Unlike properties 1 and 2, a correct tree makes properties 3 and 4 report **nothing at all** -- and
a scanner that has stopped parsing reports exactly the same nothing. Their non-vacuity floors
therefore sit on the two halves of each property's *subject* rather than on its findings: how many
multi-component pipelines were parsed and how many scripts yielded a failure-carrying variable
(property 3); how many assignment-prefixed simple commands and how many computed redirection
operands were seen (property 4). An empty result set is a FAILURE here, not a vacuous pass
(LSN-035, LSN-038).

Self-test (the `¬`): `--negative-control` applies each breakage to a copy of the sources in memory
and confirms this check reports it, printing an `<n>/<n>` tally. Each row names the property it
targets and must be caught by a SINGLE finding carrying every part of that signal, so no row can be
scored by the incidental presence of some other failure. Two situations score BROKEN rather than
MISS, because neither is evidence about the property (LSN-063): a mutation whose anchor text has
moved, so the input never changed; and a row aimed at a script the targeted arm never had in its
subject set, where a quiet arm proves nothing. Properties 3 and 4 also carry inverse rows -- the
safe `chaos-suite.sh` here-string made computed, each floor tripped from its own direction -- so a
scanner that had simply stopped reading a file cannot masquerade as the correct answer.

Run:  python3 dev/tests/cluster-check-hygiene.py
      python3 dev/tests/cluster-check-hygiene.py --negative-control
"""

from __future__ import annotations

import functools
import pathlib
import re
import sys
from typing import NamedTuple

REPO = pathlib.Path(__file__).resolve().parents[2]
POLICY_DIR = REPO / "k8s-operator" / "config" / "policy"
CRD_DIR = REPO / "k8s-operator" / "config" / "crd" / "bases"
SCRIPT_ROOTS = (REPO / "dev", REPO / "k8s-operator" / "scripts")

# Non-vacuity floors. Each one is a count that only ever grows in normal work, so a floor that trips
# means the discovery stopped finding things -- which is the failure this file is most exposed to,
# since every property below is asserted over a set this file computes.
MIN_SCRIPTS = 25
MIN_CANI_SITES = 10
MIN_PROTECTED_RESOURCES = 1
MIN_WRITER_SCRIPTS = 1

# Properties 3 and 4 are clean on a correct tree, so their floors sit on the SUBJECT the scanner
# found rather than on the findings it produced -- see the docstring. Each is roughly a third of
# today's count, which is the same margin the two floors above carry.
MIN_PIPELINES = 120          # multi-component pipelines parsed                        (374 today)
MIN_FAILVAR_SCRIPTS = 20     # scripts yielding at least one failure-carrying variable   (62 today)
MIN_FAILFILE_SCRIPTS = 1     # scripts using the subshell-proof `mktemp` sink             (2 today)
MIN_ASSIGN_PREFIX_CMDS = 20  # simple commands carrying a `VAR=... cmd` assignment prefix (55 today)
MIN_COMPUTED_REDIRECTS = 10  # redirection operands computed at the point of redirection  (29 today)

# Genuine `TYPE/NAME` queries, if one is ever needed. Empty, and it has always been empty.
LITERAL_EXEMPT: dict[str, str] = {}

# Property 4 violations that are inert TODAY and are waived until their owning script is fixed.
#
# Keyed on the exact command text, so any edit to the site un-waives it and the staleness check
# below un-waives it in the other direction: a waiver that no longer matches anything is itself a
# failure, because a silently-expired exemption is a permanently green property (LSN-036). Every
# green run prints the waived count, so a passing banner can still be told apart from a clean tree.
#
# These are NOT "safe shapes". They are the shape LSN-065 is about, sitting one refactor away from
# the live defect, in files this check may not edit.
PREFIX_REDIRECT_WAIVERS: dict[str, str] = {
    "dev/verify/actor-grant-sweep-l2.sh": (
        "`IFS=$'\\x1f' read ... <<<\"$(printf '%s' \"$1\" | tr '\\t' '\\037')\"` in ask_one(). The "
        "substituted pipeline is inert under a changed IFS -- both command names are literal and "
        "the only expansion, \"$1\", is quoted -- so it does not misbehave today. It is still the "
        "LSN-065 shape: substitute into a plain variable first and read from the variable, as the "
        "four startup-ordering-l2.sh sites now do. Reported 2026-08-01 in the improvement pass that "
        "added property 4; the fix belongs to that file's owner"
    ),
}

# --------------------------------------------------------------------------------------------
# Shell source handling
# --------------------------------------------------------------------------------------------

COMMENT_LINE = re.compile(r"^\s*#")


def strip_comments(src: str) -> str:
    """Drop whole-line shell comments, keeping line numbers intact.

    Load-bearing, and for the same reason `pause-is-not-scale-to-zero.py` strips Go comments: the
    scripts that got these rules right are the ones that EXPLAIN them, and the explanation
    necessarily contains the forbidden text. `brake-fanout-l2.sh` documents both traps in its header
    -- including the literal phrase `kubectl delete ns` -- and a check that failed on the
    documentation would be teaching people to delete the documentation.

    Whole-line only. A trailing `# ...` after code is not stripped, because deciding whether a `#`
    is a comment or part of a string needs a shell parser, and guessing wrong in the permissive
    direction is how a scanner develops a blind spot someone can park a defect in. The cost is a
    false positive on `foo   # see: kubectl delete ns`, which is loud, fixable, and has never
    occurred.
    """
    return "\n".join("" if COMMENT_LINE.match(ln) else ln for ln in src.split("\n"))


def shell_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for root in SCRIPT_ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.sh")):
            out[str(p.relative_to(REPO))] = p.read_text()
    return out


# --------------------------------------------------------------------------------------------
# A quote-aware shell tokenizer, shared by properties 3 and 4
# --------------------------------------------------------------------------------------------


class Tok(NamedTuple):
    """One shell token. `body` carries a here-document's text on the `<<` operator that opened it."""

    kind: str  # "word" | "op"
    text: str
    pos: int
    body: str | None = None


# Longest-first: `;;` must beat `;`, `<<<` must beat `<<` must beat `<`, `&&` must beat `&`.
OPERATORS = (
    ";;", "&&", "||", "|&", "<<<", "<<-", "<<", ">>", ">&", "<&", ">|",
    ";", "|", "&", "(", ")", "<", ">",
)
WORD_BREAK = set(" \t\n;|&()<>")


def _skip_squote(src: str, i: int) -> int:
    j = src.find("'", i + 1)
    return len(src) if j < 0 else j + 1


def _skip_backtick(src: str, i: int) -> int:
    j = src.find("`", i + 1)
    return len(src) if j < 0 else j + 1


def _skip_dquote(src: str, i: int) -> int:
    """Past a double-quoted span. `$( )` and backticks inside it are still live and must nest."""
    n = len(src)
    i += 1
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and i + 1 < n and src[i + 1] == "(":
            i = _skip_paren(src, i + 1)
            continue
        if c == "`":
            i = _skip_backtick(src, i)
            continue
        i += 1
    return n


def _skip_paren(src: str, i: int) -> int:
    """Past a balanced `( ... )` starting at src[i] -- command substitution, arithmetic, procsub."""
    n = len(src)
    depth = 0
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            i = _skip_squote(src, i)
            continue
        if c == '"':
            i = _skip_dquote(src, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_brace(src: str, i: int) -> int:
    """Past a balanced `${ ... }` starting at src[i]."""
    n = len(src)
    depth = 0
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            i = _skip_squote(src, i)
            continue
        if c == '"':
            i = _skip_dquote(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def scan_word(src: str, i: int) -> int:
    """End of the word starting at src[i], with every quoting and nesting form kept inside it.

    A word is the unit both properties reason about: `"$(pod_transcript "$p")"` is one operand, and
    the `|` inside `$(a | b)` is not a pipeline this file has any business splitting on.
    """
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            i = _skip_squote(src, i)
            continue
        if c == '"':
            i = _skip_dquote(src, i)
            continue
        if c == "`":
            i = _skip_backtick(src, i)
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "(":
            i = _skip_paren(src, i + 1)
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            i = _skip_brace(src, i + 1)
            continue
        if c in "<>" and i + 1 < n and src[i + 1] == "(":  # `<(cmd)` / `>(cmd)`
            i = _skip_paren(src, i + 1)
            continue
        if c in WORD_BREAK:
            break
        i += 1
    return i


@functools.lru_cache(maxsize=None)
def _lex(src: str) -> list[Tok]:
    """Tokenize a shell script into words and operators.

    Memoized on the source text. Properties 3 and 4 each lex every script, and `--negative-control`
    re-runs the whole check once per mutation over a corpus most of which no mutation touched; the
    cache is what keeps that from being quadratic in an L0 check. `Tok` is a NamedTuple and no
    caller mutates the returned list, so sharing it is safe.

    Three things here are load-bearing rather than tidy, and each one is a blind spot this file
    would otherwise have:

      * HERE-DOCUMENT BODIES ARE CONSUMED. Every `$K apply -f - <<EOF` in this tree encloses YAML,
        and YAML block scalars are spelled `|`. Lexing a body as code invents pipelines that do not
        exist, and property 3 would then report them from inside a fixture.
      * A `#` AT A WORD BOUNDARY ENDS THE LINE. `strip_comments` only removes whole-line comments,
        deliberately (see its docstring). That leaves trailing comments in the stream, and one
        apostrophe in the English of a trailing comment -- "don't", which occurs -- opens a quote
        that runs to the next apostrophe somewhere down the file. Here the tokenizer already knows
        what is quoted, so it can apply the real rule instead of guessing.
      * EVERY BRANCH ADVANCES `i`. This runs over mutated sources in the negative control, including
        deliberately malformed ones; a scanner that can hang is a check that can be made to time out.
    """
    toks: list[Tok] = []
    pending: list[tuple[str, int]] = []  # (delimiter, index of the `<<` token it belongs to)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] == "\n":
            i += 2
            continue
        if c == "\n":
            toks.append(Tok("op", "\n", i))
            i += 1
            for delim, slot in pending:
                start = i
                body = ""
                while i < n:
                    eol = src.find("\n", i)
                    end = n if eol < 0 else eol
                    if src[i:end].strip() == delim:
                        body = src[start:i]
                        i = end + 1 if eol >= 0 else n
                        break
                    i = end + 1 if eol >= 0 else n
                else:
                    body = src[start:i]
                toks[slot] = toks[slot]._replace(body=body)
            pending = []
            continue
        if c in " \t":
            i += 1
            continue
        if c == "#":
            eol = src.find("\n", i)
            i = n if eol < 0 else eol
            continue
        matched = ""
        for op in OPERATORS:
            if src.startswith(op, i):
                matched = op
                break
        # `<(` and `>(` open a process substitution, which is a word, not a redirection operator.
        if matched in ("<", ">") and i + 1 < n and src[i + 1] == "(":
            matched = ""
        if matched:
            toks.append(Tok("op", matched, i))
            i += len(matched)
            if matched in ("<<", "<<-"):
                while i < n and src[i] in " \t":
                    i += 1
                j = scan_word(src, i)
                raw = src[i:j]
                toks.append(Tok("word", raw, i))
                # The body is consumed whatever the delimiter looks like -- a quoted delimiter still
                # ends the body, it only stops the body EXPANDING, which is property 4's business
                # and is read back off this word token there.
                pending.append((raw.strip("\"'").replace("\\", ""), len(toks) - 2))
                i = j
            continue
        j = scan_word(src, i)
        if j == i:
            i += 1
            continue
        toks.append(Tok("word", src[i:j], i))
        i = j
    return toks


# Compound constructs, by the word that opens them and the word that closes them. `(` is an operator
# and pushes `)`. A closer pops back to its own opener and is otherwise IGNORED -- which is what
# keeps a `case` pattern's `foo|bar)` from closing a subshell nobody opened.
OPENERS = {"if": "fi", "while": "done", "until": "done", "for": "done", "select": "done",
           "case": "esac", "{": "}"}
CLOSER_WORDS = {"fi", "done", "esac", "}"}
SEPARATORS = {";", ";;", "&&", "||", "&", "\n"}
RESERVED = set(OPENERS) | CLOSER_WORDS | {"then", "elif", "else", "do", "in", "!", "time",
                                          "function"}


def simple_commands(toks: list[Tok]) -> list[list[Tok]]:
    """Split a token run into simple commands, on separators and reserved words.

    `done <<<"$x"` lands in a command with no command word, which is how the loop-level redirection
    is kept away from the `IFS= read` inside the loop body -- they are different commands and the
    prefix on one does not reach the operand of the other. That distinction is the whole difference
    between the safe `while IFS= read ...; done <<<"$(cmd)"` idiom (nine sites here) and LSN-065.
    """
    out: list[list[Tok]] = []
    cur: list[Tok] = []
    for t in toks:
        if (t.kind == "op" and t.text in SEPARATORS | {"|", "(", ")"}) or (
            t.kind == "word" and t.text in RESERVED
        ):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def pipelines(toks: list[Tok]) -> list[tuple[list[list[Tok]], int]]:
    """Every multi-component pipeline, as its components' tokens plus the index that closed it.

    A component runs to the next separator AT THE PIPELINE'S OWN NESTING DEPTH, so
    `cmd | while read x; do bad; done` is two components and not four statements -- the `;` inside
    the loop belongs to the loop. That shape is the one property 3 exists to catch, so getting it
    wrong is getting the property wrong.

    The closing index is returned because what follows a pipeline decides whether its verdict
    escaped: `... | apply_fixture "$what" || exit 1` consumes the pipeline's exit status, and an
    exit status is the one thing a subshell cannot swallow.
    """
    out: list[tuple[list[list[Tok]], int]] = []

    class Frame:
        """One nesting level: its own statements, its own pipelines, its own opening token."""

        def __init__(self, closer: str, start: int) -> None:
            self.closer = closer
            self.start = start
            self.parts: list[list[Tok]] = []
            self.comp: list[Tok] = []
            self.pattern = False  # inside a `case` pattern list, where `|` means "or"

    frames = [Frame("", 0)]

    def flush(f: Frame, at: int) -> None:
        if f.parts:
            f.parts.append(f.comp)
            out.append((f.parts, at))
        f.parts, f.comp = [], []

    for k, t in enumerate(toks):
        f = frames[-1]
        if t.kind == "word" and t.text in OPENERS:
            frames.append(Frame(OPENERS[t.text], k))
            frames[-1].pattern = t.text == "case"
            continue
        if t.kind == "op" and t.text == "(":
            frames.append(Frame(")", k))
            continue
        if (t.kind == "word" and t.text in CLOSER_WORDS) or (t.kind == "op" and t.text == ")"):
            # A `)` that closes nothing is a `case` pattern's, not a subshell's. Popping on it
            # would unbalance every `case` in the tree and take the rest of the file with it.
            if f.pattern and t.text == ")":
                f.pattern = False
                f.comp.append(t)
                continue
            if not any(g.closer == t.text for g in frames[1:]):
                f.comp.append(t)
                continue
            while len(frames) > 1:
                g = frames.pop()
                flush(g, k)
                if g.closer == t.text:
                    # The whole compound is ONE unit of whatever encloses it: `{ echo x; fail=1; }`
                    # piped into something is a single component, and everything inside it -- every
                    # statement, not just the last -- runs in that component's subshell.
                    frames[-1].comp.extend(toks[g.start : k + 1])
                    break
            continue
        if t.kind == "op" and t.text == "|":
            if f.pattern:
                f.comp.append(t)
            else:
                f.parts.append(f.comp)
                f.comp = []
            continue
        if t.kind == "op" and t.text in SEPARATORS:
            if t.text == ";;":
                f.pattern = True  # the next `case` clause begins
            flush(f, k)
            continue
        # `then`, `do`, `in` and `else` begin a compound's BODY; the condition or the word list
        # before them is not part of the first statement of that body.
        if t.kind == "word" and t.text in ("then", "do", "in", "else", "elif") and not f.parts:
            f.comp = []
            continue
        f.comp.append(t)

    while frames:
        flush(frames.pop(), len(toks))
    return out


def functions(toks: list[Tok]) -> dict[str, list[Tok]]:
    """`name() { ... }` and `function name { ... }` -> the body's tokens."""
    out: dict[str, list[Tok]] = {}
    for k, t in enumerate(toks):
        name = ""
        brace = -1
        if (
            t.kind == "word"
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.-]*", t.text)
            and k + 3 < len(toks)
            and toks[k + 1].kind == "op"
            and toks[k + 1].text == "("
            and toks[k + 2].text == ")"
            and toks[k + 3].text == "{"
        ):
            name, brace = t.text, k + 3
        elif t.kind == "word" and t.text == "function" and k + 2 < len(toks):
            j = k + 2
            while j < len(toks) and toks[j].text in ("(", ")"):
                j += 1
            if j < len(toks) and toks[j].text == "{":
                name, brace = toks[k + 1].text, j
        if not name:
            continue
        stack: list[str] = []
        for j in range(brace, len(toks)):
            u = toks[j]
            if u.kind == "word" and u.text in OPENERS:
                stack.append(OPENERS[u.text])
            elif u.kind == "op" and u.text == "(":
                stack.append(")")
            elif (u.kind == "word" and u.text in CLOSER_WORDS) or (u.kind == "op" and u.text == ")"):
                if u.text in stack:
                    while stack and stack.pop() != u.text:
                        pass
                if not stack:
                    out[name] = toks[brace + 1 : j]
                    break
    return out


# --------------------------------------------------------------------------------------------
# Property 1 -- auth can-i
# --------------------------------------------------------------------------------------------

CANI = re.compile(r"auth\s+can-i\s+(?P<rest>[^\n]*)")
FOR_LIST = re.compile(r"^\s*for\s+(?P<var>\w+)\s+in\s+(?P<list>.+?)(?:;|\s*$)", re.MULTILINE)
QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")
VAR_REF = re.compile(r"\$\{?(\w+)\}?")
# The `*/*)` case arm that refuses a malformed resource. Written loosely enough to survive
# reformatting and tightly enough that a bare `*)` default arm does not satisfy it.
SLASH_GUARD = re.compile(r"\*\s*/\s*\*\s*\)")

# Flags of `kubectl auth can-i` that take their value as the NEXT token. Without this the value is
# read as a positional -- `-n $NSX` looked like a computed resource on the first draft of this file,
# which would have demanded a runtime guard from three scripts that never compute a resource at all.
# An unlisted separate-form flag degrades to a spurious positional: a false positive, which is loud.
VALUE_FLAGS = {
    "-n", "--namespace", "--as", "--as-group", "--as-uid", "--subresource", "--context",
    "--cluster", "--user", "--kubeconfig", "--server", "-o", "--output", "--request-timeout",
}


def _tokens(rest: str) -> list[str]:
    """Split the tail of an `auth can-i` command into shell-ish tokens, minus redirections."""
    rest = rest.split("2>")[0].split("|")[0]
    rest = rest.rstrip(")").rstrip('"').rstrip()
    return [t for t in re.split(r"\s+", rest) if t]


def _positionals(toks: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("-"):
            i += 2 if t in VALUE_FLAGS else 1
            continue
        out.append(t)
        i += 1
    return out


def for_lists(src: str) -> dict[str, list[str]]:
    """`for VAR in ...` -> the literal items, when the list is statically enumerable.

    `for pair in "impersonate users" "escalate roles.rbac..."` feeding `auth can-i $pair` is an
    established idiom here (verify-phase2, verify-phase3), and a line-scoped scan cannot see into
    it. Resolving the list turns those sites back into literal ones, which is both stricter than
    demanding a runtime guard from them and closer to what is actually being asserted.
    """
    out: dict[str, list[str]] = {}
    for fm in FOR_LIST.finditer(src):
        raw = fm.group("list").strip()
        items = [a or b for a, b in QUOTED.findall(raw)]
        if not items and not any(c in raw for c in "$`*"):
            items = raw.split()  # `for v in get list watch; do`
        if items:
            out.setdefault(fm.group("var"), []).extend(items)
    return out


def _expand(positionals: list[str], lists: dict[str, list[str]]) -> list[list[str]] | None:
    """Every concrete word-list this invocation can produce, or None if it cannot be resolved."""
    results: list[list[str]] = [[]]
    for tok in positionals:
        bare = tok.strip("\"'")
        v = VAR_REF.fullmatch(bare)
        if v and v.group(1) in lists:
            results = [r + item.split() for item in lists[v.group(1)] for r in results]
        elif "$" in bare or "`" in bare:
            return None
        else:
            results = [r + [bare] for r in results]
    return results


def check_can_i(sources: dict[str, str]) -> tuple[list[str], int]:
    failures: list[str] = []
    sites = 0

    for name, raw in sorted(sources.items()):
        src = strip_comments(raw)
        matches = list(CANI.finditer(src))
        if not matches:
            continue
        lists = for_lists(src)
        unresolved: list[str] = []

        for m in matches:
            sites += 1
            call = f"auth can-i {m.group('rest').strip()}"
            expansions = _expand(_positionals(_tokens(m.group("rest"))), lists)

            # 1b: a resource this file cannot resolve must be refused by the script at runtime.
            if expansions is None:
                unresolved.append(call)
                continue

            # 1a: no positional word may contain a slash, however it got there.
            for words in expansions:
                for w in words:
                    if "/" not in w or w in LITERAL_EXEMPT:
                        continue
                    sub = w.split("/", 1)[1]
                    via = "" if call.find(w) >= 0 else " (reached via a `for`-list)"
                    failures.append(
                        f"{name}: `{call}`{via} puts `{w}` in a positional slot. kubectl parses a "
                        f"positional `TYPE/NAME`, not `TYPE/SUBRESOURCE`, so this asks about an "
                        f"object NAMED `{sub}`. Pass `--subresource={sub}` instead. On a negative "
                        f"assertion the form is VACUOUSLY GREEN: the `no` comes from a resource "
                        f"name nobody was granted, not from the policy under test (LSN-044)"
                    )

        if unresolved and not SLASH_GUARD.search(src):
            failures.append(
                f"{name}: {len(unresolved)} `auth can-i` site(s) name a resource this check cannot "
                f"resolve (e.g. `{unresolved[0]}`), and the script carries no `*/*)` guard. A "
                f"computed resource cannot be checked statically, so the script must refuse the "
                f"malformed shape itself -- see `can()` in dev/verify/brake-fanout-l2.sh. Without "
                f"one, property 1a is evaded by the ordinary refactor of hoisting a repeated query "
                f"into a helper, which is exactly how it was evaded here (LSN-044)"
            )

    return failures, sites


# --------------------------------------------------------------------------------------------
# Property 2 -- protected resources vs namespace deletion
# --------------------------------------------------------------------------------------------

VAP_DOC = re.compile(r"^kind:\s*ValidatingAdmissionPolicy\s*$", re.MULTILINE)
RULE_CHUNK = re.compile(r"-\s*apiGroups:")
OPERATIONS = re.compile(r"operations:\s*\[([^\]]*)\]")
RESOURCES = re.compile(r"resources:\s*\[([^\]]*)\]")
CRD_KIND = re.compile(r"^    kind:\s*(\w+)\s*$", re.MULTILINE)
CRD_PLURAL = re.compile(r"^    plural:\s*(\w+)\s*$", re.MULTILINE)

# How a script says "delete a namespace". `--all` sweeps and label-selector deletes are not here:
# they do not remove the namespace, so the object survives in a namespace that still terminates.
NS_DELETE = re.compile(r"delete\s+(?:-[\w-]+(?:=\S+)?\s+)*(?:ns|namespace|namespaces)\b")


def protected_resources(policy_text: dict[str, str]) -> set[str]:
    """Every resource any ValidatingAdmissionPolicy matches for DELETE.

    Derived rather than listed: this is the single definition site for "the API server will refuse
    to delete one of these", and a second retention policy over a second resource should start
    being enforced here on the day it lands, not on the day someone remembers to edit this file.
    """
    found: set[str] = set()
    for text in policy_text.values():
        if not VAP_DOC.search(text):
            continue
        chunks = RULE_CHUNK.split(text)[1:]
        for chunk in chunks:
            ops = OPERATIONS.search(chunk)
            res = RESOURCES.search(chunk)
            if not ops or not res:
                continue
            if "DELETE" not in ops.group(1).upper():
                continue
            for r in re.findall(r"[\w.\-]+", res.group(1)):
                if "/" in r:  # a subresource is not separately deletable
                    continue
                found.add(r)
    return found


def kinds_for(plurals: set[str], crds: dict[str, str]) -> dict[str, str]:
    """plural -> Kind, read out of the CRD that declares the plural."""
    out: dict[str, str] = {}
    for text in crds.values():
        k = CRD_KIND.search(text)
        p = CRD_PLURAL.search(text)
        if k and p and p.group(1) in plurals:
            out[p.group(1)] = k.group(1)
    return out


def check_namespace_lifecycle(
    sources: dict[str, str], policies: dict[str, str], crds: dict[str, str]
) -> tuple[list[str], set[str], list[str]]:
    failures: list[str] = []
    plurals = protected_resources(policies)
    kinds = kinds_for(plurals, crds)

    unresolved = sorted(plurals - set(kinds))
    if unresolved:
        failures.append(
            f"a policy denies DELETE on {unresolved} and no CRD in config/crd/bases declares that "
            f"plural. Either the policy names a resource that does not exist -- in which case it "
            f"protects nothing -- or `make manifests` has not run. Both make the derivation below "
            f"silently narrower than the policy set it is supposed to mirror"
        )

    writers: list[str] = []
    for name, raw in sorted(sources.items()):
        src = strip_comments(raw)
        created = sorted({k for k in kinds.values() if re.search(rf"kind:\s*{k}\b", src)})
        if not created:
            continue
        writers.append(name)
        m = NS_DELETE.search(src)
        if m:
            failures.append(
                f"{name}: creates {', '.join(created)} and also runs `{m.group(0)}`. A "
                f"ValidatingAdmissionPolicy denies DELETE of those objects to the namespace "
                f"controller, so the namespace will terminate forever and the script cannot be "
                f"re-run. Reuse the namespace and delete only the objects inside it; mint "
                f"identifiers per run; select Events by `involvedObject.uid` so residue cannot "
                f"satisfy an assertion. Do NOT patch `status.exported.confirmed` to free it -- "
                f"that forges the field 05 §1.2 makes the durable record (LSN-045)"
            )

    return failures, plurals, writers


# --------------------------------------------------------------------------------------------
# Property 3 -- a failure flag assigned inside a pipeline component
# --------------------------------------------------------------------------------------------

ASSIGN_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
INCR_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\+\+|--)$")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOLLAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
LASTPIPE = re.compile(r"shopt\s+-s\s+(?:\S+\s+)*lastpipe\b")

# A name is failure-shaped if it reads as one. Deliberately a shape and not a list: the tree already
# spells this counter `fail`, `n_fail`, `nc_fail`, `l0_bad`, `b2_bad`, `problems` and `errors`, and
# the eighth spelling is the one a hardcoded list is guaranteed to miss (LSN-036). Bare `err` is
# excluded on purpose -- it would drag in `stderr`, which is a transcript, not a verdict.
FAIL_WORDS = ("fail", "bad", "problem", "violation", "error", "offender")

# The `mktemp` sink is the ACCEPTED fix for LSN-064: a file survives the subshell, so a name bound
# to one is a handle rather than a flag and belongs nowhere near this property's candidate set. It
# also happens to be spelled `FAILFILE` in both scripts that use it, which is exactly the collision
# a name-shape test has to be told about.
MKTEMP = re.compile(r"\bmktemp\b")

TEST_OPEN = {"[", "[[", "test"}


def assigned_names(toks: list[Tok]) -> dict[str, str]:
    """Every variable the script assigns -> the raw token that assigned it (for the mktemp test)."""
    out: dict[str, str] = {}
    for k, t in enumerate(toks):
        if t.kind != "word":
            continue
        m = ASSIGN_WORD.match(t.text)
        if m:
            out.setdefault(m.group(1), t.text)
            continue
        m = INCR_WORD.match(t.text)  # `(( fail++ ))`
        if m:
            out.setdefault(m.group(1), t.text)
            continue
        # `(( fail += 1 ))`, which the tokenizer splits into three words.
        if (
            IDENT.fullmatch(t.text)
            and k + 1 < len(toks)
            and toks[k + 1].kind == "word"
            and toks[k + 1].text in ("=", "+=", "-=")
        ):
            out.setdefault(t.text, t.text)
    return out


def _vars_read(seq: list[Tok]) -> set[str]:
    """Variable names a test or arithmetic expression reads.

    `[ "$fail" -ne 0 ]` spells the read with a `$`. `(( fail ))` spells it without one, because an
    arithmetic context dereferences a bare name -- and the tokenizer, which has no reason to know
    that `((` is one thing, hands it over as two `(`. A scanner that knows only the `$` dialect
    reads a suite whose verdict is arithmetic as a suite with no verdict at all, and then finds
    nothing wrong with it forever.
    """
    out: set[str] = set()
    for t in seq:
        out.update(DOLLAR_REF.findall(t.text))
    k = 0
    while k + 1 < len(seq):
        if not (seq[k].kind == "op" == seq[k + 1].kind and seq[k].text == seq[k + 1].text == "("):
            k += 1
            continue
        depth, j = 2, k + 2
        while j < len(seq) and depth:
            if seq[j].kind == "op" and seq[j].text in ("(", ")"):
                depth += 1 if seq[j].text == "(" else -1
            elif seq[j].kind == "word" and IDENT.fullmatch(seq[j].text):
                out.add(seq[j].text)
            j += 1
        k = j
    return out


def exit_path_variables(toks: list[Tok]) -> set[str]:
    """Variables the script's own exit status is derived from.

    Two sources, because a suite spells its verdict two ways. `exit "$fail"` names the variable
    outright. `[ "$fail" -eq 0 ] || exit 1` and the multi-line `if [ "$n_bad" -ne 0 ]; then ...
    exit 1; fi` name it in a test that guards an exit -- so a test only counts when an `exit`
    reaches it, which is what keeps the ordinary `[ -n "$pod" ]` out of the candidate set.
    """
    found: set[str] = set()

    # `exit $X`, and any test on the same logical line as an exit.
    line: list[Tok] = []
    for t in toks + [Tok("op", "\n", -1)]:
        if t.kind == "op" and t.text == "\n":
            texts = [u.text for u in line]
            if "exit" in texts:
                for k, u in enumerate(line):
                    if u.text == "exit" and k + 1 < len(line):
                        found.update(DOLLAR_REF.findall(line[k + 1].text))
                    if u.text in TEST_OPEN or (u.kind == "op" and u.text == "("):
                        found |= _vars_read(line[k:])
            line = []
            continue
        line.append(t)

    # The condition of an `if` whose block exits.
    stack: list[tuple[str, int]] = []
    conds: dict[int, int] = {}  # index of an `if` -> index of its `then`
    exits: set[int] = set()
    for k, t in enumerate(toks):
        if t.kind == "word" and t.text in OPENERS:
            stack.append((OPENERS[t.text], k))
        elif t.kind == "op" and t.text == "(":
            stack.append((")", k))
        elif (t.kind == "word" and t.text in CLOSER_WORDS) or (t.kind == "op" and t.text == ")"):
            if any(s == t.text for s, _ in stack):
                while stack and stack.pop()[0] != t.text:
                    pass
        elif t.kind == "word" and t.text == "then" and stack and stack[-1][0] == "fi":
            conds[stack[-1][1]] = k
        elif t.kind == "word" and t.text == "exit":
            for _, opener in stack:
                exits.add(opener)
    for opener, then in conds.items():
        if opener in exits:
            found |= _vars_read(toks[opener:then])
    return found


def failure_variables(toks: list[Tok]) -> set[str]:
    """The failure-carrying variables of one script -- derived, never the literal string `fail`."""
    assigned = assigned_names(toks)
    named = {v for v in assigned if any(w in v.lower() for w in FAIL_WORDS)}
    candidates = (named | exit_path_variables(toks)) & set(assigned)
    return {v for v in candidates if not MKTEMP.search(assigned[v])}


def component_assigns(comp: list[Tok], failvars: set[str]) -> set[str]:
    out: set[str] = set()
    for k, t in enumerate(comp):
        if t.kind != "word":
            continue
        for pat in (ASSIGN_WORD, INCR_WORD):
            m = pat.match(t.text)
            if m and m.group(1) in failvars:
                out.add(m.group(1))
        if (
            t.text in failvars
            and k + 1 < len(comp)
            and comp[k + 1].kind == "word"
            and comp[k + 1].text in ("=", "+=", "-=")
        ):
            out.add(t.text)
    return out


def returns_nonzero(body: list[Tok]) -> bool:
    """Whether a helper can hand a failure back to its caller as a status.

    A bare `return` hands back the status of the last command it ran -- which, on the branch that
    records a failure, is the `echo` inside the recorder, i.e. zero. That is not a quibble: it is
    exactly what `reject()` in webhook-negatives-l2.sh did, and it is why a `|| exit` on that call
    site would never have fired. Only an explicit non-zero operand counts.
    """
    for k, t in enumerate(body):
        if t.kind != "word" or t.text != "return":
            continue
        nxt = body[k + 1] if k + 1 < len(body) else None
        if nxt is None or nxt.kind != "word":
            continue
        if nxt.text.isdigit():
            if int(nxt.text) != 0:
                return True
        elif "$" in nxt.text:  # `return $rc` -- cannot be shown to be zero
            return True
    return False


def status_is_consumed(toks: list[Tok], closed_at: int) -> bool:
    """Whether the pipeline at `closed_at` is followed by an `|| ... exit` that ends the process.

    `return` is deliberately not accepted here. Ending the process is the only propagation that
    cannot itself be dropped by the next subshell up.
    """
    if closed_at >= len(toks) or toks[closed_at].text != "||":
        return False
    for t in toks[closed_at + 1 :]:
        if t.kind == "op" and t.text in ("\n", ";", "&&", "||"):
            return False
        if t.kind == "word" and t.text == "exit":
            return True
    return False


def component_calls(comp: list[Tok], names: set[str]) -> set[str]:
    """Functions invoked in command position anywhere in this pipeline component."""
    out: set[str] = set()
    for cmd in simple_commands(comp):
        k = 0
        while k < len(cmd) and cmd[k].kind == "word" and ASSIGN_WORD.match(cmd[k].text):
            k += 1
        if k < len(cmd) and cmd[k].kind == "word" and cmd[k].text in names:
            out.add(cmd[k].text)
    return out


def check_pipeline_failure_flag(sources: dict[str, str]) -> tuple[list[str], dict[str, int]]:
    failures: list[str] = []
    n_pipelines = 0
    n_failfile_scripts = 0
    # Which scripts this arm can actually SEE. The negative control reads it to tell a mutation the
    # arm looked at and let through (a MISS) from one aimed at a script the arm never had in its
    # subject set (a BROKEN control row, which proves nothing at all) -- LSN-063.
    failvar_names: list[str] = []

    for name, raw in sorted(sources.items()):
        toks = _lex(raw)
        assigned = assigned_names(toks)
        failvars = failure_variables(toks)
        if failvars:
            failvar_names.append(name)
        # The safe shape, counted so its disappearance is loud: a failure-SHAPED name bound to a
        # `mktemp` result is the FAILFILE sink, not a flag, and the exclusion that keeps it out of
        # `failvars` is the only thing standing between this property and a false positive on the
        # accepted fix for LSN-064.
        if any(
            any(w in v.lower() for w in FAIL_WORDS) and MKTEMP.search(a)
            for v, a in assigned.items()
        ):
            n_failfile_scripts += 1

        fns = functions(toks)
        # Transitive: a function that only calls the choke point still records a failure through it.
        taint = {f for f, body in fns.items() if component_assigns(body, failvars)}
        while True:
            grown = {
                f
                for f, body in fns.items()
                if f not in taint and component_calls(body, taint)
            }
            if not grown:
                break
            taint |= grown

        # Only where the script says so. Bash's default puts EVERY component in a subshell; the
        # narrower "all but the last" rule is true only under `lastpipe`, and asserting the narrow
        # rule unconditionally would have been green on the defect this property is named for.
        exempt_last = bool(LASTPIPE.search(raw))

        for parts, closed_at in pipelines(toks):
            n_pipelines += 1
            consumed = status_is_consumed(toks, closed_at)
            for idx, comp in enumerate(parts):
                last = idx == len(parts) - 1
                if last and exempt_last:
                    continue
                where = f"component {idx + 1} of {len(parts)}"
                lost = sorted(component_assigns(comp, failvars))
                via = sorted(component_calls(comp, taint))
                # THE ONE ESCAPE ROUTE OUT OF A SUBSHELL. A helper that records the failure AND
                # hands back a non-zero status, called from a pipeline whose status is consumed by
                # an `|| ... exit`, has reported it -- the process dies and the lost variable can
                # never be read. Both halves are required, and each is useless alone: a non-zero
                # return nobody reads is discarded exactly like the variable, and an `|| exit` on a
                # helper that always returns 0 never fires, which is precisely what `reject()` did.
                # Only the helper arm gets this: an inline `fail=1` is usually the last command in
                # its component, so it MAKES the pipeline's status zero and the `|| exit` with it.
                if via and not lost and consumed and all(returns_nonzero(fns[f]) for f in via):
                    continue
                if not lost and not via:
                    continue
                line = raw[: comp[0].pos].count("\n") + 1 if comp else 0
                snippet = " ".join(t.text for t in comp)[:80]
                if lost:
                    cause = f"`{lost[0]}` is assigned"
                else:
                    flag = sorted(_assigned_by(fns, via[0], failvars)) or ["a failure flag"]
                    cause = f"`{via[0]}()`, which assigns `{flag[0]}`, is invoked"
                failures.append(
                    f"{name}:{line}: {cause} inside {where} of the pipeline `{snippet}`. Every "
                    f"component of a bash pipeline runs in a subshell, so the assignment is "
                    f"discarded when that subshell exits and the suite CANNOT REPORT ITS OWN "
                    f"FAILURE -- it prints FAIL: lines and exits 0 under a PASS banner, which is "
                    f"the exit code the ledger records. Append to a `mktemp` FAILFILE at a single "
                    f"choke point instead and derive the exit code from it once at the end; a file "
                    f"survives the subshell (LSN-064)"
                )

    return failures, {
        "pipelines": n_pipelines,
        "failvar_scripts": len(failvar_names),
        "failfile_scripts": n_failfile_scripts,
        "failvar_names": failvar_names,
    }


def _assigned_by(
    fns: dict[str, list[Tok]], fn: str, failvars: set[str], seen: set[str] | None = None
) -> set[str]:
    """Which failure flags `fn` records, directly or through the helper it delegates to."""
    seen = set() if seen is None else seen
    if fn in seen:
        return set()
    seen.add(fn)
    body = fns.get(fn, [])
    out = component_assigns(body, failvars)
    for g in component_calls(body, set(fns)):
        out |= _assigned_by(fns, g, failvars, seen)
    return out


# --------------------------------------------------------------------------------------------
# Property 4 -- an assignment prefix in effect while a redirection operand is expanded
# --------------------------------------------------------------------------------------------

# What makes an operand COMPUTED rather than merely expanded. A bare `"$_pair"` is a parameter
# reference: nothing runs while it is substituted, which is why chaos-suite.sh's `IFS=: read ...
# <<<"$_pair"` is the safe shape and is not flagged. `$(`, a backtick and `${` all execute or
# evaluate something at the moment of redirection -- with the prefix already applied.
COMPUTED = re.compile(r"\$\(|`|\$\{")
INPUT_REDIRECTS = {"<<<", "<<", "<<-", "<"}


def check_assignment_prefix_redirect(
    sources: dict[str, str]
) -> tuple[list[str], dict[str, object]]:
    failures: list[str] = []
    n_prefix = 0
    n_computed = 0
    waived: list[str] = []
    # Same purpose as `failvar_names` above: the control has to be able to prove the arm was
    # looking at the script a row mutates before a quiet arm can be scored as a MISS.
    prefix_names: list[str] = []

    for name, raw in sorted(sources.items()):
        for cmd in simple_commands(_lex(raw)):
            k = 0
            while k < len(cmd) and cmd[k].kind == "word" and ASSIGN_WORD.match(cmd[k].text):
                k += 1
            prefix = [t.text for t in cmd[:k]]
            if k >= len(cmd):
                continue  # a bare assignment, which has no redirection and no command to prefix
            if prefix:
                n_prefix += 1
                if name not in prefix_names:
                    prefix_names.append(name)

            for idx, t in enumerate(cmd):
                if t.kind != "op" or t.text not in INPUT_REDIRECTS:
                    continue
                operand = cmd[idx + 1] if idx + 1 < len(cmd) else None
                if operand is None:
                    continue
                kind = ""
                shown = operand.text
                if t.text == "<<<" and COMPUTED.search(operand.text):
                    kind = "here-string"
                elif t.text in ("<<", "<<-"):
                    quoted = operand.text[:1] in ("'", '"') or "\\" in operand.text
                    if not quoted and t.body and COMPUTED.search(t.body):
                        kind = "here-document"
                        shown = f"<<{operand.text}"
                elif t.text == "<" and operand.text.startswith("<("):
                    kind = "process substitution"
                if not kind:
                    continue
                n_computed += 1
                if not prefix:
                    continue
                if name in PREFIX_REDIRECT_WAIVERS:
                    waived.append(name)
                    continue
                line = raw[: t.pos].count("\n") + 1
                failures.append(
                    f"{name}:{line}: `{' '.join(prefix)}` is an assignment prefix on "
                    f"`{cmd[k].text}` whose {kind} operand is computed (`{shown[:60]}`). The "
                    f"prefix is ALREADY IN EFFECT while that operand is expanded, so whatever the "
                    f"operand runs, runs under the changed variable -- with `IFS` that turns every "
                    f"unquoted `$K` in the callee into a single command name that does not exist, "
                    f"and a `2>/dev/null` on the read swallows the `command not found` so the "
                    f"caller gets empty strings and reports them as findings about the cluster. "
                    f"Substitute into a plain variable first and read from the variable (LSN-065)"
                )

    stale = sorted(set(PREFIX_REDIRECT_WAIVERS) - set(waived))
    for s in stale:
        failures.append(
            f"a property-4 waiver names {s}, and nothing in that script matches it any more. "
            f"Either the site was fixed -- in which case delete the waiver, which is the whole "
            f"point of keying it on the script -- or the scanner stopped seeing it, in which case "
            f"the waiver is now hiding a live violation. An exemption that expires silently is a "
            f"permanently green property (LSN-036)"
        )

    return failures, {"prefix_cmds": n_prefix, "computed_operands": n_computed,
                      "waived": sorted(set(waived)), "prefix_names": prefix_names}


# --------------------------------------------------------------------------------------------

def check(
    sources: dict[str, str], policies: dict[str, str], crds: dict[str, str]
) -> tuple[list[str], dict[str, object]]:
    failures: list[str] = []

    cani_failures, sites = check_can_i(sources)
    failures.extend(cani_failures)

    ns_failures, plurals, writers = check_namespace_lifecycle(sources, policies, crds)
    failures.extend(ns_failures)

    pipe_failures, pipe_stats = check_pipeline_failure_flag(sources)
    failures.extend(pipe_failures)

    pre_failures, pre_stats = check_assignment_prefix_redirect(sources)
    failures.extend(pre_failures)

    # Non-vacuity. Every property above is asserted over a set this file discovers, so a discovery
    # that finds nothing reports success -- the exact shape LSN-036 is about.
    if len(sources) < MIN_SCRIPTS:
        failures.append(
            f"VACUOUS: found {len(sources)} shell scripts under {[str(r.relative_to(REPO)) for r in SCRIPT_ROOTS]}, "
            f"expected at least {MIN_SCRIPTS}. The discovery is broken, not the tree"
        )
    if sites < MIN_CANI_SITES:
        failures.append(
            f"VACUOUS: found {sites} `auth can-i` sites, expected at least {MIN_CANI_SITES}. "
            f"Property 1 is asserting over almost nothing; fix the scanner, not the floor"
        )
    if len(plurals) < MIN_PROTECTED_RESOURCES:
        failures.append(
            f"VACUOUS: derived {len(plurals)} DELETE-protected resources from "
            f"config/policy/*.yaml, expected at least {MIN_PROTECTED_RESOURCES}. Property 2 is "
            f"asserting nothing -- either the retention policy was removed (a finding in its own "
            f"right) or the parser stopped matching"
        )
    if len(writers) < MIN_WRITER_SCRIPTS:
        failures.append(
            f"VACUOUS: no script creates a DELETE-protected object, so property 2 has no subject. "
            f"It had one when this check was written (dev/verify/brake-fanout-l2.sh). A property "
            f"with no subject passes forever"
        )

    # Properties 3 and 4 find nothing on a correct tree, so a scanner that has stopped parsing is
    # byte-identical to a clean pass. Their floors sit on the subject, not the findings.
    if pipe_stats["pipelines"] < MIN_PIPELINES:
        failures.append(
            f"VACUOUS: parsed {pipe_stats['pipelines']} multi-component pipelines, expected at "
            f"least {MIN_PIPELINES}. Property 3 has nothing to assert over -- the tokenizer stopped "
            f"splitting pipelines, which reads exactly like a tree with no piped assertions in it"
        )
    if pipe_stats["failvar_scripts"] < MIN_FAILVAR_SCRIPTS:
        failures.append(
            f"VACUOUS: derived a failure-carrying variable in {pipe_stats['failvar_scripts']} "
            f"script(s), expected at least {MIN_FAILVAR_SCRIPTS}. Property 3 is asserting over "
            f"scripts it believes have no verdict to lose; the derivation is broken, not the tree"
        )
    if pipe_stats["failfile_scripts"] < MIN_FAILFILE_SCRIPTS:
        failures.append(
            f"VACUOUS: no script records failure into a `mktemp` sink, so the safe shape property "
            f"3 must NOT flag has left the tree. It is the accepted fix for LSN-064 "
            f"(dev/verify/webhook-negatives-l2.sh, dev/verify/reader-scope-l2.sh); if it is gone, "
            f"either those suites regressed or the recognizer did"
        )
    if pre_stats["prefix_cmds"] < MIN_ASSIGN_PREFIX_CMDS:
        failures.append(
            f"VACUOUS: found {pre_stats['prefix_cmds']} commands carrying an assignment prefix, "
            f"expected at least {MIN_ASSIGN_PREFIX_CMDS}. Property 4 is the intersection of two "
            f"shapes and this is the half that names it; with no prefixes seen it cannot fire"
        )
    if pre_stats["computed_operands"] < MIN_COMPUTED_REDIRECTS:
        failures.append(
            f"VACUOUS: found {pre_stats['computed_operands']} computed redirection operands, "
            f"expected at least {MIN_COMPUTED_REDIRECTS}. That is property 4's other half -- a "
            f"scanner that no longer recognises `<<<\"$(...)\"` or an expanding here-document "
            f"reports the same clean nothing as a tree that has none"
        )

    return failures, {
        "scripts": len(sources),
        "sites": sites,
        "plurals": sorted(plurals),
        "writers": writers,
        **pipe_stats,
        **pre_stats,
    }


def read_all() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    sources = shell_sources()
    policies = {p.name: p.read_text() for p in sorted(POLICY_DIR.glob("*.yaml"))} if POLICY_DIR.exists() else {}
    crds = {p.name: p.read_text() for p in sorted(CRD_DIR.glob("*.yaml"))} if CRD_DIR.exists() else {}
    return sources, policies, crds


BRAKE = "dev/verify/brake-fanout-l2.sh"
PH3 = "dev/verify/verify-phase3.sh"
WEBHOOK = "dev/verify/webhook-negatives-l2.sh"
STARTUP = "dev/verify/startup-ordering-l2.sh"
CHAOS = "dev/verify/chaos-suite.sh"
GRANT = "dev/verify/actor-grant-sweep-l2.sh"

# What a row needs to be true of the MUTATED tree before a quiet arm may be scored a MISS. A row
# aimed at property 3 that mutates a script the derivation never classified as having a verdict is
# not evidence the property is asleep -- it is evidence the row is pointed at nothing, and scoring
# it MISS would send the next reader to debug a property that was never asked a question (LSN-063).
def _sees_p3(script: str):
    return lambda st: script in st["failvar_names"]


def _sees_p4(script: str):
    return lambda st: script in st["prefix_names"]


def negative_control() -> int:
    sources, policies, crds = read_all()

    def with_script(s: dict[str, str], name: str, text: str) -> dict[str, str]:
        return {**s, name: text}

    # Each mutation carries the SIGNAL its finding must contain -- the property it targets, not
    # merely "something failed". A control that only asks whether the failure list is non-empty
    # cannot tell a mutation caught by its own property from one intercepted by a broader one, and
    # the narrow property underneath then accumulates controls that never execute it (LSN-035).
    # `dev/tests/negative-controls-name-their-rule.py` enforces this shape across the corpus.
    mutations = [
        (
            "a subresource is passed positionally to auth can-i",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace("auth can-i get nodes --as=$SA", "auth can-i get nodes/status --as=$SA", 1),
                ),
                p,
                c,
            ),
            "`nodes/status` in a positional slot",
        ),
        (
            "the same defect hidden one indirection out, in a for-list",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        'for pair in "impersonate users"',
                        'for pair in "patch actionrecords/status" "impersonate users"',
                        1,
                    ),
                ),
                p,
                c,
            ),
            "`actionrecords/status` in a positional slot",
        ),
        (
            "a slash reaches the resource slot only after a flag whose value was misread",
            # Guards the tokenizer itself: if VALUE_FLAGS ever swallows a token it should not, the
            # resource moves out of the slot this check inspects and 1a goes quiet.
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        "auth can-i get pods --as=$SA -n kube-system",
                        "auth can-i get pods --as=$SA -n kube-system pods/status",
                        1,
                    ),
                ),
                p,
                c,
            ),
            "`pods/status` in a positional slot",
        ),
        (
            "the runtime guard on the computed resource is removed",
            lambda s, p, c: (
                with_script(s, BRAKE, re.sub(r"\*/\*\)", "__removed__)", s[BRAKE], count=1)),
                p,
                c,
            ),
            "brake-fanout-l2.sh: 1 `auth can-i` site(s) name a resource this check cannot resolve",
        ),
        (
            "a resolvable resource is hoisted into a variable, evading 1a, with no guard added",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        "auth can-i get nodes --as=$SA",
                        'auth can-i get "$RES" --as=$SA',
                        1,
                    ),
                ),
                p,
                c,
            ),
            "verify-phase3.sh: 1 `auth can-i` site(s) name a resource this check cannot resolve",
        ),
        (
            "an L2 suite that creates ActionRecords deletes its namespace",
            lambda s, p, c: (
                with_script(s, BRAKE, s[BRAKE] + '\n$K delete ns "$NS" --ignore-not-found\n'),
                p,
                c,
            ),
            "creates ActionRecord and also runs `delete ns`",
        ),
        (
            "the same, spelled `delete namespace` with a flag in between",
            lambda s, p, c: (
                with_script(s, BRAKE, s[BRAKE] + '\n$K delete --wait=false namespace "$NS"\n'),
                p,
                c,
            ),
            "runs `delete --wait=false namespace`",
        ),
        (
            "a NEW script creates the protected kind and cleans up the old way",
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/some-future-suite.sh",
                    "#!/usr/bin/env bash\n$K apply -f - <<EOF\nkind: ActionRecord\nEOF\n"
                    '$K delete ns "$NS"\n',
                ),
                p,
                c,
            ),
            "some-future-suite.sh: creates ActionRecord",
        ),
        (
            "the retention policy stops matching DELETE, so the protected set empties",
            lambda s, p, c: (
                s,
                {k: v.replace('operations: ["DELETE"]', 'operations: ["UPDATE"]') for k, v in p.items()},
                c,
            ),
            "VACUOUS: derived 0 DELETE-protected resources",
        ),
        (
            "the policy protects a resource no CRD declares",
            lambda s, p, c: (
                s,
                {k: v.replace('resources: ["actionrecords"]', 'resources: ["ghostrecords"]') for k, v in p.items()},
                c,
            ),
            "denies DELETE on ['ghostrecords'] and no CRD",
        ),
        (
            "the script discovery finds nothing",
            lambda s, p, c: ({BRAKE: s[BRAKE]}, p, c),
            "VACUOUS: found 1 shell scripts",
        ),
        (
            "comment stripping is disabled, so the documentation fails the check",
            # The inverse control: it proves strip_comments is load-bearing rather than decorative.
            # Asserted by construction below rather than by mutating the sources.
            #
            # It lands on property 2, not property 1, and the signal is how that was discovered. The
            # prose spelling of the can-i trap is `<verb> <type>/<thing>` in backticks, and a
            # backtick means command substitution, so `_expand` correctly declines to resolve it.
            # Only the `kubectl delete ns` prose in the LSN-045 header actually trips a property.
            # A control that asked only "did something fail" would have recorded this as covering
            # both, which is the whole of LSN-035 in one line.
            None,
            "creates ActionRecord and also runs `delete ns`",
        ),
        # -- Property 3 (LSN-064) -------------------------------------------------------------
        (
            "the accepted LSN-064 fix is reverted: the choke point sets a variable, not the file",
            lambda s, p, c: (
                with_script(s, WEBHOOK, s[WEBHOOK].replace('echo x >>"$FAILFILE"', "fail=1", 1)),
                p,
                c,
            ),
            (WEBHOOK, "LSN-064"),
            _sees_p3(WEBHOOK),
        ),
        (
            "the flag is not called `fail`, so only deriving it from the exit path finds it",
            # The whole reason the candidate set is derived rather than a literal (LSN-036). A
            # hardcoded `fail` is green here, and green for every suite that named its flag anything
            # else -- which is most of the ways a suite gets written after the first one.
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/derived-name-suite.sh",
                    "#!/usr/bin/env bash\n"
                    "wrecked=0\n"
                    'record() { echo "$1"; wrecked=1; }\n'
                    "list_things | record one\n"
                    'if [ "$wrecked" -ne 0 ]; then exit 1; fi\n',
                ),
                p,
                c,
            ),
            ("derived-name-suite.sh", "`wrecked`"),
            _sees_p3("dev/verify/derived-name-suite.sh"),
        ),
        (
            "the assignment is buried in a brace group on the LEFT of the pipe",
            # The parser's frame stack, asserted directly: a `{ ...; }` is one component, and the
            # statements inside it are in that component's subshell no matter how many `;` separate
            # them. A parser that reset at each `;` loses the assignment and reports nothing.
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/brace-group-suite.sh",
                    "#!/usr/bin/env bash\n"
                    "problems=0\n"
                    '{ echo x; problems=1; } | grep -q x\n'
                    'exit "$problems"\n',
                ),
                p,
                c,
            ),
            ("brace-group-suite.sh", "component 1 of 2"),
            _sees_p3("dev/verify/brace-group-suite.sh"),
        ),
        (
            "a piped recorder keeps its non-zero return but the caller stops reading it",
            lambda s, p, c: (
                with_script(
                    s,
                    BRAKE,
                    s[BRAKE].replace(
                        '| apply_fixture "Agent $NS/$AGENT" || exit 1',
                        '| apply_fixture "Agent $NS/$AGENT"',
                        1,
                    ),
                ),
                p,
                c,
            ),
            (BRAKE, 'apply_fixture "Agent $NS/$AGENT"'),
            _sees_p3(BRAKE),
        ),
        (
            "the piped recorder keeps its `|| exit 1` but returns bare, so the exit never fires",
            # The other half of the same exemption, and the half that actually bit: `reject()` in
            # webhook-negatives-l2.sh ended in a bare `return`, whose status is the preceding
            # `echo` -- zero. Every `|| exit 1` guarding it was decoration.
            lambda s, p, c: (
                with_script(s, BRAKE, s[BRAKE].replace("\n  return 1\n", "\n  return\n", 1)),
                p,
                c,
            ),
            (BRAKE, "REC_LOUD"),
            _sees_p3(BRAKE),
        ),
        (
            "the tokenizer stops splitting pipelines, so property 3 has nothing to walk",
            lambda s, p, c: ({k: v.replace("|", " ") for k, v in s.items()}, p, c),
            "VACUOUS: parsed",
            None,
        ),
        (
            "the derivation stops classifying anything as a verdict, but pipelines still parse",
            # Aimed one floor over from the row above, and it must not reach that one: the two
            # floors guard different halves of the same arm, and a control that cannot tell them
            # apart lets either half rot while the other keeps the row green.
            lambda s, p, c: (
                {BRAKE: "#!/usr/bin/env bash\n" + "true | true\n" * 200},
                p,
                c,
            ),
            "derived a failure-carrying variable in 0",
            None,
        ),
        (
            "the subshell-proof `mktemp` sink leaves the tree, taking the safe shape with it",
            lambda s, p, c: ({k: v.replace("mktemp", "mktmp") for k, v in s.items()}, p, c),
            "no script records failure into a `mktemp` sink",
            None,
        ),
        # -- Property 4 (LSN-065) -------------------------------------------------------------
        (
            "LSN-065 is restored at its original site: the here-string operand is computed again",
            lambda s, p, c: (
                with_script(
                    s,
                    STARTUP,
                    s[STARTUP].replace(
                        'read -r phase restarts <<<"$transcript"',
                        'read -r phase restarts <<<"$(pod_transcript "$agent_pod")"',
                        1,
                    ),
                ),
                p,
                c,
            ),
            (STARTUP, "LSN-065"),
            _sees_p4(STARTUP),
        ),
        (
            "the safe here-string at chaos-suite.sh:513 is made computed",
            # The inverse control for property 4's one deliberate non-finding. `IFS=: read ...
            # <<<"$_pair"` is a plain parameter reference and nothing runs while it expands, so it
            # is correctly silent -- but silent for the right reason only if making it computed is
            # loud. Without this row, a scanner that had stopped reading chaos-suite.sh entirely
            # would look exactly like the correct answer.
            lambda s, p, c: (
                with_script(
                    s,
                    CHAOS,
                    s[CHAOS].replace('<<<"$_pair"', '<<<"$(printf \'%s\' "$_pair")"', 1),
                ),
                p,
                c,
            ),
            (CHAOS, "LSN-065"),
            _sees_p4(CHAOS),
        ),
        (
            "the same defect in its here-DOCUMENT spelling, with a prefix that is not IFS",
            # The body of an unquoted here-document is expanded at the point of redirection too, so
            # the prefix is in effect for it as well. A scanner that only knew `<<<` would call
            # this clean, and the delimiter makes it look inert to a reader.
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/heredoc-prefix-suite.sh",
                    "#!/usr/bin/env bash\nLC_ALL=C cat <<EOF\nhost=$(hostname)\nEOF\n",
                ),
                p,
                c,
            ),
            ("heredoc-prefix-suite.sh", "here-document"),
            _sees_p4("dev/verify/heredoc-prefix-suite.sh"),
        ),
        (
            "the same defect spelled as a process substitution",
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/procsub-prefix-suite.sh",
                    "#!/usr/bin/env bash\nIFS=: read -r a b < <(list_pairs \"$1\")\n",
                ),
                p,
                c,
            ),
            ("procsub-prefix-suite.sh", "process substitution"),
            _sees_p4("dev/verify/procsub-prefix-suite.sh"),
        ),
        (
            "the waived site is fixed and the waiver is left behind",
            # A waiver that outlives its site stops being an exemption and becomes a blind spot over
            # whatever is written there next. It has to expire loudly or property 4 is green for
            # that whole script forever (LSN-036).
            lambda s, p, c: (
                with_script(
                    s,
                    GRANT,
                    s[GRANT].replace(
                        "<<<\"$(printf '%s' \"$1\" | tr '\\t' '\\037')\"",
                        '<<<"$row"',
                        1,
                    ),
                ),
                p,
                c,
            ),
            "a property-4 waiver names dev/verify/actor-grant-sweep-l2.sh",
            None,
        ),
        (
            "no assignment prefix survives in the tree, so property 4's naming half is empty",
            lambda s, p, c: (
                {BRAKE: "#!/usr/bin/env bash\n" + "cat <<EOF\n$(date)\nEOF\n" * 40},
                p,
                c,
            ),
            "commands carrying an assignment prefix",
            None,
        ),
        (
            "no operand is recognised as computed, so property 4's other half is empty",
            # Deliberately the mirror of the row above: this corpus is all prefixes and no computed
            # operands, that one all computed operands and no prefixes. Property 4 is an
            # INTERSECTION, and a single floor over both halves would be satisfied by either.
            lambda s, p, c: (
                {BRAKE: "#!/usr/bin/env bash\n" + 'IFS=: read -r a b <<<"$x"\n' * 40},
                p,
                c,
            ),
            "computed redirection operands",
            None,
        ),
    ]

    clean, _ = check(sources, policies, crds)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    # Three verdicts, not two. BROKEN is a row that could not ask its question -- the anchor text
    # moved, or the arm never had the mutated script in its subject set. Scoring that as MISS sends
    # the next reader to debug a property nobody actually interrogated, and scoring it as a pass
    # retires the row while leaving the count reassuringly unchanged (LSN-063).
    CAUGHT, MISS, BROKEN = "caught", "MISS  ", "BROKEN"
    rows: list[tuple[str, str, str]] = []

    for label, mutate, signal, *rest in mutations:
        visible = rest[0] if rest else None
        # Every substring must land in the SAME finding. Spread across two, they are two unrelated
        # findings that happen to co-occur, which is the thing a signal exists to rule out.
        sig = (signal,) if isinstance(signal, str) else tuple(signal)
        if mutate is None:
            # The comment-stripping control, run directly: the real tree must fail if comments are
            # NOT stripped, which is what makes stripping them a decision rather than a habit.
            found, stats = check({k: v.replace("#", " ") for k, v in sources.items()}, policies, crds)
        else:
            ms, mp, mc = mutate(dict(sources), dict(policies), dict(crds))
            if (ms, mp, mc) == (sources, policies, crds):
                rows.append((label, BROKEN, "the mutation did not apply -- its anchor text has moved"))
                continue
            found, stats = check(ms, mp, mc)
        if visible is not None and not visible(stats):
            rows.append(
                (
                    label,
                    BROKEN,
                    "the targeted arm never had the mutated script in its subject set, so this row "
                    "proves nothing either way -- fix the row, not the property",
                )
            )
            continue
        hits = [f for f in found if all(x in f for x in sig)]
        if hits:
            rows.append((label, CAUGHT, ""))
        elif not found:
            rows.append((label, MISS, "not caught at all"))
        else:
            rows.append(
                (
                    label,
                    MISS,
                    f"caught, but not by the property it targets -- no single finding carries all "
                    f"of {sig!r}; first finding was: {found[0][:100]}...",
                )
            )

    scored = [r for r in rows if r[1] == CAUGHT]
    if len(scored) != len(rows):
        print(
            f"FAIL: cluster-check-hygiene negative control -- {len(scored)}/{len(rows)} breakages "
            f"caught by the arm each targets:",
            file=sys.stderr,
        )
        for label, verdict, detail in rows:
            if verdict != CAUGHT:
                print(f"  - {verdict}: {label} ({detail})", file=sys.stderr)
        return 1

    print(
        f"PASS: cluster-check-hygiene negative control -- {len(scored)}/{len(rows)} breakages "
        f"caught by the arm each targets"
    )
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    sources, policies, crds = read_all()
    failures, stats = check(sources, policies, crds)

    if failures:
        print("FAIL: cluster-check-hygiene (LSN-044, LSN-045, LSN-064, LSN-065)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    waived = stats["waived"]
    print(
        f"PASS: cluster-check-hygiene (L0) -- {stats['sites']} `auth can-i` sites across "
        f"{stats['scripts']} shell scripts name a resource and never a subresource, every computed "
        f"resource is guarded, and the {len(stats['writers'])} script(s) that create "
        f"DELETE-protected objects ({', '.join(stats['plurals'])}) delete no namespace"
    )
    # The subject counts are printed on a GREEN run on purpose: properties 3 and 4 find nothing when
    # the tree is correct, so the only thing that distinguishes a clean pass from a scanner that has
    # gone quiet is the size of the set it looked at (LSN-035).
    print(
        f"      -- {stats['pipelines']} pipelines across {stats['failvar_scripts']} scripts with a "
        f"derived failure flag ({stats['failfile_scripts']} using the subshell-proof mktemp sink) "
        f"assign no failure flag in a subshell; {stats['prefix_cmds']} assignment-prefixed commands "
        f"and {stats['computed_operands']} computed redirection operands never coincide"
        + (f"; {len(waived)} WAIVED: {', '.join(waived)}" if waived else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

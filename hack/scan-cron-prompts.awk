# Which `"prompt"` values in the documentation are claiming to quote the cron
# roster. Driven by hack/check-docs-terminology.sh, which passes the roster ids
# through `-v idfile=<path>` (one id per line) and the documents as operands.
#
# Prints one line per prompt occurrence it has an opinion about:
#
#   R:<file>:<line>:<text>   grade this against the roster, verbatim.
#   O:<file>:<line>:<text>   this renders a roster entry that names no id the
#                            roster knows. Report it; it cannot be graded.
#
# A prompt this prints nothing for is deliberately none of the guard's business.
#
# --- Why a fence, and why a block ------------------------------------------
#
# Only text inside a fence is a rendered manifest. Prose that happens to spell a
# JSON key is a sentence about a manifest, and grading it is how this scan
# blocks a pull request that did nothing wrong: `concepts/governance-sops.md`
# carries no fence at all and already writes `"skills": ["fleet-audit"]` in a
# sentence, so one more sentence mentioning `"prompt": "` anywhere in that file
# would have made the whole document look like one malformed roster entry.
#
# Deciding per block rather than per document is the other half of that. A page
# that named a job in one section and rendered an unrelated `"prompt"` in
# another used to fail CI with an error about cron prompts, pointing at a block
# that was never claiming to quote anything.
#
# `O` closes the hole on the other side. Eliding the `"id"` line from a rendered
# entry used to remove it from the check entirely -- no error, no coverage -- so
# deleting one line was all it took to silence the guard on a quotation. A block
# that still looks like a roster entry has to name a job the roster knows.
#
# A block earns `R` by naming a roster id, or -- failing that -- by carrying
# nothing but a prompt in a document where some other fenced block does name
# one. That fallback covers a quotation trimmed down to the prompt alone: no id,
# no sibling keys, nothing for either rule above to catch, so deciding purely
# per block left it graded by nothing at all. It is confined to prompt-only
# blocks because a block carrying other keys is some other object that happens
# to have a prompt -- a LiteLLM request body is `{"model": …, "prompt": …}` --
# and confined to documents that render a roster entry because a page that never
# renders one is not quoting one.
#
# --- Why the matching looks like this ---------------------------------------
#
# The ids are compared whole, here in awk, and never become part of a pattern.
# Interpolated into an alternation instead, a single `(`, `|` or `+` in a job id
# made `grep -E` reject the pattern outright; the error went to /dev/null, the
# status went to `|| true`, and the guard reported PASS having read nothing.
#
# Matching a key wherever on the line it falls and however it is spaced is
# deliberate. An anchor that insisted on the key starting the line, followed by
# exactly one space, saw only prettier's rendering of a multi-line object -- a
# one-line `{"id": …, "prompt": …}` entry, or a hand-spaced one, went unchecked,
# which is the same silent skip the structural anchor was adopted to end.
# Prettier normalises much of this back, but only inside a fence tagged `json`,
# and CI does not run it over `.mdx` at all. What no line-based anchor can see
# is a quotation whose value sits on the line after its key; those are
# unchecked.
#
# tests/test_docs_terminology_guard.py drives this file directly.

function flush(   i, kind) {
  kind = hasid ? "R" : (cronish ? "O" : (otherkey ? "" : "?"))
  for (i = 1; i <= n; i++) {
    m++; qkind[m] = kind
    qfile[m] = pfile[i]; qline[m] = pline[i]; qtext[m] = ptext[i]
  }
  n = 0; hasid = 0; cronish = 0; otherkey = 0
}

# Held to end of file because the prompt-only fallback needs to know whether any
# block anywhere in the document named a roster id, which a block that comes
# first cannot know yet.
function endfile(   i, kind) {
  flush()
  for (i = 1; i <= m; i++) {
    kind = qkind[i]
    if (kind == "?") kind = dochasid ? "R" : ""
    if (kind != "")
      printf "%s:%s:%s:%s\n", kind, qfile[i], qline[i], qtext[i]
  }
  m = 0; dochasid = 0; infence = 0
}

BEGIN { while ((getline id < idfile) > 0) if (id != "") ids["\"" id "\""] = 1 }

FNR == 1 { endfile() }

# `>` so a fence inside a blockquote still opens and closes a block. Without it
# two blockquoted manifests were one block, and the id in the first graded the
# prompt in the second.
/^[ \t>]*(```|~~~)/ { flush(); infence = !infence; next }

{
  if (!infence) next
  rest = $0
  while (match(rest, /"id"[ \t]*:[ \t]*"[^"]*"/)) {
    # RSTART/RLENGTH are read into locals before the sub() below, which POSIX
    # does not say preserves them. gawk and onetrue awk do; mawk is what CI
    # runs, and a scan that silently stopped after the first id on a line would
    # grade a one-line entry by nothing.
    s = RSTART; l = RLENGTH
    seg = substr(rest, s, l)
    sub(/^"id"[ \t]*:[ \t]*/, "", seg)
    # An id the roster does not know is a renamed or mistyped job, which is
    # exactly what `O` is for -- and it is a roster entry whether or not a
    # sibling key survived the trim.
    if (seg in ids) { hasid = 1; dochasid = 1 } else cronish = 1
    rest = substr(rest, s + l)
  }
  if ($0 ~ /"(schedule|skills|deliver|no_agent)"[ \t]*:/) cronish = 1
  # Any JSON key on the line other than `prompt`, which switches off the
  # document-level fallback for this block. A `":"` inside the prompt text
  # itself reads as a key here and switches it off too, which is the behaviour
  # this had before the fallback existed.
  t = $0
  gsub(/"prompt"[ \t]*:/, "", t)
  if (t ~ /"[^"]+"[ \t]*:/) otherkey = 1
  if ($0 ~ /"prompt"[ \t]*:[ \t]*"/) {
    n++; pfile[n] = FILENAME; pline[n] = FNR; ptext[n] = $0
  }
}

END { endfile() }

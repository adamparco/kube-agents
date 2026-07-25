export const meta = {
  name: "verify-phase",
  description:
    "Run a set of conformance checks (by ID, from docs/design/09) in parallel and adversarially confirm each result",
  whenToUse:
    "After a unit or phase is implemented, to execute many check IDs concurrently at their assigned levels and get a per-check PASS/FAIL/DEFERRED with evidence. Use when the serial harness-verify loop would be slow — typically a phase gate or a full regress.",
  phases: [
    {
      title: "Preconditions",
      detail: "environment trust checks before any live result is believed",
    },
    {
      title: "Run",
      detail: "one agent per check ID, at that check’s assigned level",
    },
    {
      title: "Confirm",
      detail: "adversarially re-check every PASS on a negative-control check",
    },
  ],
};

// args: {
//   checkIds: ["V-CTN-004", ...]   // the checks to run
//   target:   "kind" | "gke" | "none"
//   phase:    number | string
//   skipPreconditions: boolean     // only legitimate for an L0/L1-only run
// }
//
// This workflow deliberately holds NO check definitions. They live in
// docs/design/09-verification-and-validation.md §6 and are read by the agents at run time, so this
// file cannot drift from the conformance spec the way its predecessor did — that version hardcoded
// the read-only generation's checks and silently inverted when the design became imperative.

const target = (args && args.target) || "kind";
const phaseArg = args && args.phase != null ? args.phase : "current";
const checkIds = (args && args.checkIds) || [];

if (!checkIds.length) {
  log(
    "No checkIds supplied — pass the IDs the unit claims plus every BLOCKING-ALWAYS check.",
  );
  return { aborted: "no-checks" };
}

const RESULT = {
  type: "object",
  required: ["checkId", "result", "level", "evidence"],
  properties: {
    checkId: { type: "string" },
    result: { type: "string", enum: ["pass", "fail", "deferred", "skipped"] },
    level: { type: "string", enum: ["L0", "L1", "L2", "L3", "L4"] },
    gateClass: { type: "string" },
    evidence: {
      type: "string",
      description:
        "command run and its salient output, or an artifact reference",
    },
    negativeControl: {
      type: "string",
      description: "the deliberately-bad input tried, and that it was rejected",
    },
    blocker: {
      type: "string",
      description: "required when result is deferred",
    },
    notes: { type: "string" },
  },
};

const CONFIRM = {
  type: "object",
  required: ["checkId", "upheld", "reasoning"],
  properties: {
    checkId: { type: "string" },
    upheld: { type: "boolean" },
    reasoning: { type: "string" },
    suspectedFalseGreen: { type: "boolean" },
  },
};

// ---- Preconditions -----------------------------------------------------------------------------
// Each of these exists because it has already produced a false green here. A live result that has
// not cleared them is not evidence.
if (!(args && args.skipPreconditions) && target !== "none") {
  phase("Preconditions");
  const pre = await agent(
    `Read .claude/harness/binding.md §Preconditions and docs/design/09-verification-and-validation.md §9.3.
Verify, against target "${target}", every environment precondition: deployed first-party image digests match the build under test; any freshly-created admission policy binding is actually live (poll a dry-run until it rejects); the network substrate genuinely enforces NetworkPolicy if any egress check is in scope; and the destructive-test guard matches the context with an ANCHORED pattern.
Report each as pass/fail with the command and output. Do not fix anything — just report.`,
    { label: "preconditions", phase: "Preconditions", schema: RESULT },
  );
  if (pre && pre.result === "fail") {
    log(
      `PRECONDITIONS FAILED — no live result from this run may be trusted. ${pre.evidence || ""}`,
    );
    return { aborted: "preconditions", detail: pre };
  }
}

// ---- Run + Confirm -----------------------------------------------------------------------------
// Pipelined: each check is adversarially confirmed as soon as it finishes, rather than waiting for
// the slowest check in the batch.
const results = await pipeline(
  checkIds,
  (id) =>
    agent(
      `You are running conformance check **${id}** for phase ${phaseArg} of the kube-agents build.

1. Read its definition in docs/design/09-verification-and-validation.md §6 — the assertion, the source spec section, its assigned LEVEL, and its gate class.
2. Read the source spec section it cites, so you assert the actual requirement rather than your paraphrase of the ID.
3. Run it AT ITS ASSIGNED LEVEL against target "${target}". Do NOT substitute a lower level: proving at L0 something the spec assigns to L2/L3 is the most common false green in this project. If the assigned level cannot be run here, return "deferred" with a named blocker — never "pass".
4. If the check carries a negative control (marked ¬), you MUST run the deliberately-bad input and confirm it is REJECTED. A check that only demonstrates the happy path is not evidence.
5. Return the command(s) you ran and their salient output as evidence. A pass with no evidence is a "skipped".

Consult .claude/harness/binding.md for build/test entry points and target names.`,
      { label: `run:${id}`, phase: "Run", schema: RESULT },
    ),
  (res, id) => {
    if (!res || res.result !== "pass") return { run: res, confirm: null };
    return agent(
      `Adversarially review this reported PASS for conformance check **${id}**.

Reported evidence:
${JSON.stringify(res, null, 2)}

Your job is to try to REFUTE it. Read the check in docs/design/09 §6 and the spec section it cites, then ask:
- Does the evidence actually demonstrate the asserted property, or something weaker that resembles it?
- Was it run at the assigned level, on a substrate that can actually enforce the property?
- If a negative control was required, was a deliberately-bad input genuinely rejected — or merely absent?
- Could this pass while the underlying property is broken? Consult docs/design/09 §11 for the ways this codebase has been fooled before.

Default to upheld=false if the evidence is thin. A confident PASS on weak evidence is worse than a FAIL.`,
      { label: `confirm:${id}`, phase: "Confirm", schema: CONFIRM },
    ).then((c) => ({ run: res, confirm: c }));
  },
);

// ---- Summarize ---------------------------------------------------------------------------------
const rows = results.filter(Boolean);
const failed = rows.filter((r) => r.run && r.run.result === "fail");
const deferred = rows.filter((r) => r.run && r.run.result === "deferred");
const skipped = rows.filter((r) => r.run && r.run.result === "skipped");
const refuted = rows.filter((r) => r.confirm && r.confirm.upheld === false);

log(
  `checks=${rows.length} failed=${failed.length} deferred=${deferred.length} skipped=${skipped.length} refuted-on-review=${refuted.length}`,
);
if (refuted.length) {
  log(
    "REFUTED PASSES — treat these as failures until the evidence is strengthened.",
  );
}

return {
  phase: phaseArg,
  target,
  counts: {
    total: rows.length,
    failed: failed.length,
    deferred: deferred.length,
    skipped: skipped.length,
    refuted: refuted.length,
  },
  // Green only if nothing failed, nothing was silently skipped, and no PASS was refuted on review.
  green: failed.length === 0 && skipped.length === 0 && refuted.length === 0,
  results: rows,
};

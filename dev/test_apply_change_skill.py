"""V-CTR-020 — the agent's instructions for writing describe the write path that exists.

WHAT A CHECK OVER PROSE CAN AND CANNOT DO
------------------------------------------
`apply-change/SKILL.md` is instructions for an LLM. Most of it is unfalsifiable by construction:
there is no test for whether "plan first when you are not certain" is good advice, and pretending
otherwise would produce a check that measures wording.

Three things are not prose, and those are what this file asserts. Each is a **join** — a value read
out of running code and compared against what the skill says — rather than a list restated in a
test, which is the defect [[LSN-041]] names:

    the tool names the skill calls      the @mcp.tool() functions in platform_mcp_server.py, by AST
    the parameters it promises          the same functions' signatures, by AST
    the parameters it swears are absent the same signatures, by AST

The reason to spend a check on this is that the failure mode is uniquely quiet. A skill naming a
tool the server does not register does not crash and does not log: the agent reads the instruction,
finds no such tool, and explains in fluent prose that it is unable to act. Nothing else in the
system can see that. The same is true of a signature that grows a parameter while the prose keeps
promising the old one.

The fourth arm -- no `kubectl`/`gcloud`/`git push`/`gh pr` anywhere in the body -- is a grep, and a
weak one taken alone. It is here because it is the property the whole conversion is *about*.
`submit-suggestion`, which still ships beside this skill for one more phase, is nothing but git and
`gh` commands; this skill is its replacement and was written next to it. A line left behind in a
copy-paste would be a mutating shell-out in the instructions of an agent holding no credential to
run it, which fails confusingly rather than safely -- the agent tries, is refused by something
unrelated, and reports the wrong cause.

WHY THE MCP MODULE IS PARSED AND NOT IMPORTED
----------------------------------------------
`platform_mcp_server.py` imports `mcp` and `pydantic`, and neither is installed in the check
environment ([[LSN-007]]). Parsing is enough here: every property is about the declared surface, not
about behaviour. Behaviour is V-BRK-029's, one layer down.
"""

from __future__ import annotations

import ast
import hashlib
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIERS = ("platform", "cluster-admin", "developer-team")
SKILL = "apply-change"

# 02 §2.2 -- the four things the persona says it cannot influence. The claim is only true while the
# parameter is missing, so the parameter is what gets checked.
UNINFLUENCEABLE = ("tier", "scope", "risk_class", "riskClass", "risk", "approved", "approval")

# V-CTR-011's mandated absences. An agent that could release its own gated action makes the gate
# decorative, so the skill may not name one of these as something it does.
BRAKE_VERBS = ("pause_self", "pause", "resume", "freeze", "approve", "reject", "uncontest")


def skill_path(tier: str) -> Path:
    return REPO / "agents" / tier / "skills" / SKILL / "SKILL.md"


def mcp_path(tier: str) -> Path:
    return REPO / "agents" / tier / "scripts" / "platform_mcp_server.py"


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md has no YAML frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def body(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


def declared_tools(tier: str) -> dict[str, ast.FunctionDef]:
    """Every `@mcp.tool()` function the server registers, by name."""
    tree = ast.parse(mcp_path(tier).read_text())
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and any("mcp.tool" in ast.unparse(d) for d in n.decorator_list)
    }


def params(fn: ast.FunctionDef) -> set[str]:
    return {a.arg for a in fn.args.args + fn.args.kwonlyargs} - {"self"}


def required_params(fn: ast.FunctionDef) -> set[str]:
    """Parameters with no default -- the ones the model must supply or the call does not happen.

    Positional defaults bind to the tail of `args`; keyword-only defaults are positionally aligned
    with `kwonlyargs` and are `None` where absent.
    """
    positional = fn.args.posonlyargs + fn.args.args
    required = {a.arg for a in positional[: len(positional) - len(fn.args.defaults)]}
    required |= {a.arg for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is None}
    return required - {"self"}


def tools_the_skill_calls(text: str) -> set[str]:
    """Every `name(` the skill writes in code voice -- backticked or in a fenced block."""
    called = set(re.findall(r"`([a-z_][a-z0-9_]*)\(", text))
    for block in re.findall(r"```(.*?)```", text, re.S):
        called |= set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\(", block, re.M))
    return called


class TestTheSkillExists(unittest.TestCase):
    def test_every_tier_ships_it(self):
        """02 §2.2 marks `apply-change` cross-cutting: every tier acts, scoped to its own authority."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertTrue(skill_path(tier).is_file(), f"{tier} has no {SKILL}/SKILL.md")

    def test_the_copies_are_byte_identical(self):
        digests = {t: hashlib.sha256(skill_path(t).read_bytes()).hexdigest() for t in TIERS}
        self.assertEqual(
            len(set(digests.values())),
            1,
            f"apply-change differs across tiers: {digests}. The skill deliberately states no tier-specific "
            "scope -- the broker derives scope from the authenticated identity, so a tier-specific copy "
            "would be restating something the agent is not allowed to influence.",
        )

    def test_the_frontmatter_name_matches_the_directory(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                fm = frontmatter(skill_path(tier).read_text())
                self.assertEqual(fm.get("name"), SKILL)
                self.assertTrue(fm.get("description"), "a skill with no description is a skill nothing loads")

    def test_submit_suggestion_is_gone_from_every_tier(self):
        """The inverse of the arm that stood here, and the inversion was a deliberate ruling.

        This arm used to assert that `submit-suggestion` STILL SHIPS, with a good argument: its
        retirement was scheduled at P10-T3, per tier, as each tier's shadow mode was turned off, and
        07 §5 says a path is replaced rather than deleted and swapped in the same phase that removes
        it. Deleting the GitOps path in Phase 9 -- a phase whose defining constraint is that no write
        authority exists anywhere -- looked like it would leave every tier with a dead proposal path
        and a no-op imperative one.

        P13-T5 was pulled forward from Phase 13 into Phase 9 by human ruling on the milestone halt,
        and the conversion deleted the skill. That is not a violation of the argument above; it is
        the argument reaching its real conclusion. `submit-suggestion`'s write path was a GitHub
        token living in an agent pod, and it was the one write credential the phase's own property
        did not cover, because it wrote to git rather than to a cluster. Removing it does not leave
        Phase 9 with less change capability than the design intends -- the design intends none --
        and it removes the last surface on which an agent could effect a change without an Action
        Envelope. 02 §2.1 does not allocate the skill to any tier, which is why V-CMP-020 requires
        its absence.

        Kept as an arm rather than deleted so the retirement is asserted rather than assumed: a
        directory quietly restored by `scripts/sync-upstream-skills.py` or by a bad merge would
        otherwise reappear silently. V-CMP-020 also covers this from the allocation side; this is
        the cheap local statement of it, and the two failing together is more legible than either
        alone.
        """
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertFalse(
                    (REPO / "agents" / tier / "skills" / "submit-suggestion").exists(),
                    f"{tier} still ships submit-suggestion. It was retired by P13-T5 and its replacement is "
                    "apply-change, which submits an Action Envelope instead of opening a pull request.",
                )


class TestItDescribesToolsThatExist(unittest.TestCase):
    """The join. Every name and parameter is read out of the MCP server, never listed here."""

    def setUp(self):
        self.text = skill_path("platform").read_text()
        self.tools = declared_tools("platform")

    def test_the_two_write_tools_are_registered(self):
        self.assertIn("submit_action", self.tools, "the skill's whole subject is not a registered tool")
        self.assertIn("plan_action", self.tools)

    def test_every_tool_the_skill_calls_is_one_the_server_registers(self):
        called = tools_the_skill_calls(self.text)
        # Words in code voice that are not tool calls at all.
        called -= {"str", "list", "dict", "int", "bool"}
        unknown = called - set(self.tools)
        self.assertFalse(
            unknown,
            f"the skill tells the agent to call {sorted(unknown)}, which the MCP server does not register. "
            "The agent will read the instruction, find no such tool, and explain in prose that it cannot act.",
        )

    def test_every_parameter_the_skill_promises_is_one_the_tool_takes(self):
        """Backticked identifiers that name a parameter of a tool the skill discusses."""
        mentioned = set(re.findall(r"`([a-z_][a-z0-9_]*)`", self.text))
        real = params(self.tools["submit_action"]) | params(self.tools["plan_action"])
        # Only judge words that look like parameters: snake_case or a known arg. Prose words in
        # backticks (`gcloud`, `patch`, `create`) are not claims about a signature.
        claimed = {w for w in mentioned if "_" in w}
        # These are envelope/operation field names and tool names, not tool parameters.
        claimed -= {"submit_action", "plan_action", "pause_self", "targetSelector", "cloudTarget"}
        unknown = claimed - real
        self.assertFalse(
            unknown,
            f"the skill promises parameters {sorted(unknown)} that neither tool takes. "
            f"submit_action takes {sorted(params(self.tools['submit_action']))}.",
        )

    def test_the_skill_names_every_parameter_the_tool_requires(self):
        """Derived from the signature, not listed here.

        It was `("intent", "operations")` written out until T8b-4d added a third required parameter,
        `trigger_source`, at which point the hardcoded pair kept passing while the skill was free to
        say nothing about it. A required parameter the instructions never mention is the worst of
        the three failure shapes this file covers: not a tool that is missing and not a parameter
        that does not exist, but a call the model cannot form at all, discovered at the moment it
        first tries to change something.

        **Being mentioned is not being documented**, and the difference is not pedantic: the first
        version of this asserted only that the backticked name appeared somewhere in the body, and
        both mutants for it escaped, because the same commit that deleted a parameter's definition
        left a passing reference to it two paragraphs down. What is required is the definitional
        bullet the file already uses for each of them -- `- **`name`** — ...` -- which is a shape a
        cross-reference does not have.
        """
        required = required_params(self.tools["submit_action"]) | required_params(self.tools["plan_action"])
        self.assertGreaterEqual(len(required), 3, "the tools declare fewer required parameters than 06 §9 gives them")
        for name in sorted(required):
            with self.subTest(parameter=name):
                self.assertRegex(
                    self.text,
                    re.compile(rf"^- \*\*`{re.escape(name)}`\*\* —", re.M),
                    f"the skill never defines {name}, which the tool requires. A mention elsewhere is not a "
                    "definition; the model has nothing but this text to learn the parameter from.",
                )

    def test_the_skill_names_every_value_of_the_closed_enum_it_asks_the_agent_to_pick_from(self):
        """`trigger_source` is required, closed, and unguessable -- the model has only this text.

        Read out of `action_envelope.VALID_TRIGGER_SOURCES`, which is itself held equal to the Go
        side by V-BRK-028, so this is a three-way join and not a list restated in prose. A source
        the skill omits is one the agent will never choose, and the missing one is likelier to be an
        autonomous origin than `chat` -- which biases the 01 §7 autonomy counts in exactly the
        direction that makes the platform look more human-driven than it is.
        """
        source = (REPO / "agents" / "platform" / "scripts" / "action_envelope.py").read_text()
        m = re.search(r"VALID_TRIGGER_SOURCES\s*=\s*frozenset\(\{(.*?)\}\)", source, re.S)
        self.assertTrue(m, "action_envelope.py no longer declares VALID_TRIGGER_SOURCES by that name")
        values = re.findall(r'"([^"]+)"', m.group(1))
        self.assertEqual(len(values), 7, f"06 §4.1 closes trigger.source over seven values; found {values}")
        for value in values:
            with self.subTest(source=value):
                self.assertIn(f"`{value}`", self.text, f"the skill omits the trigger source {value!r}")

    def test_require_approval_exists_and_the_skill_says_it_only_tightens(self):
        self.assertIn("require_approval", params(self.tools["submit_action"]))
        self.assertIn("require_approval", self.text)
        self.assertRegex(
            self.text,
            r"never ask for less|cannot ask for less|only goes one way",
            "the skill mentions require_approval without saying it can only ask for MORE gating; a caller "
            "that thinks it is a dial has misunderstood the one parameter it is allowed to set",
        )


class TestItPromisesNoInfluenceTheAgentDoesNotHave(unittest.TestCase):
    """02 §2.2's claims are true only while the corresponding parameter is absent."""

    def setUp(self):
        self.text = skill_path("platform").read_text()
        self.tools = declared_tools("platform")

    def test_no_tool_takes_a_tier_scope_risk_or_approval_parameter(self):
        for name, fn in self.tools.items():
            for p in params(fn):
                with self.subTest(tool=name, param=p):
                    self.assertNotIn(
                        p,
                        UNINFLUENCEABLE,
                        f"{name} takes `{p}`. The skill tells the agent it cannot influence this, and that "
                        "sentence stops being true the moment the parameter exists.",
                    )

    def test_the_skill_says_the_agent_does_not_decide_its_own_risk(self):
        self.assertRegex(
            self.text,
            r"do not decide your own risk|does not decide its own risk|not decide your own risk level",
            "02 §2.2 requires the persona to understand that risk is computed from the objects and the diff, "
            "not from its own confidence",
        )

    def test_the_skill_says_the_broker_derives_the_scope(self):
        self.assertRegex(self.text, r"derives (who you are|what you may touch|the scope)|authenticated")


class TestNoMutatingShellOutAndNoProposalPath(unittest.TestCase):
    """The conversion, stated negatively. `submit-suggestion` is git and `gh`; this must be neither."""

    FORBIDDEN = (
        (r"kubectl\s+(apply|delete|patch|scale|create|edit|replace)\b", "a mutating kubectl shell-out"),
        (r"gcloud\s+\w+\s+(create|delete|update|patch|set)\b", "a mutating gcloud shell-out"),
        (r"git\s+(push|commit|checkout\s+-b|branch)\b", "a git write -- this is not the proposal path"),
        (r"gh\s+pr\s+(create|merge)\b", "a pull request -- there is no propose verb in this path"),
    )

    @staticmethod
    def instruction_text(markdown: str) -> str:
        """What the skill tells the agent to *run*, as opposed to what it mentions.

        The distinction is not pedantry: this skill's job includes saying which commands never
        appear in this path, so a naive scan flags the sentence that states the rule and passes
        the file that breaks it.

        A skill issues a command in one of two places -- inside a fenced block, or bare in prose
        ("run `X`" without the backticks). It *mentions* one inside an inline code span. So: keep
        every fenced block, and keep prose with inline spans removed. `submit-suggestion`, which
        this replaces, puts all nine of its git and `gh` commands in fenced blocks, so the copy-
        paste this arm exists to catch lands squarely in the kept half.
        """
        text = body(markdown)
        fenced = "\n".join(re.findall(r"```(?:\w*)\n(.*?)```", text, re.S))
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        prose = re.sub(r"`[^`\n]*`", "", prose)
        return fenced + "\n" + prose

    def test_the_body_issues_no_mutating_command(self):
        for tier in TIERS:
            text = self.instruction_text(skill_path(tier).read_text())
            for pattern, what in self.FORBIDDEN:
                with self.subTest(tier=tier, forbidden=what):
                    m = re.search(pattern, text)
                    self.assertIsNone(m, f"{tier}'s apply-change issues {what}: {m.group(0) if m else ''!r}")

    # A synthetic positive control, in the format the scan reads. Every FORBIDDEN pattern appears
    # exactly once, split across the two places `instruction_text` keeps -- a fenced block and bare
    # prose -- because a control that only exercises one half cannot tell a broken fence regex from
    # a broken prose regex.
    #
    # A FINDING, RECORDED HERE AND NOT FIXED HERE. The gcloud line below reads `gcloud projects
    # create`, and it is deliberately the WEAKEST mutating gcloud command that could stand in this
    # slot. The first draft of this control used `gcloud container clusters update prod`, which is
    # the canonical GKE mutation, and the scan did not fire: `gcloud\s+\w+\s+(create|delete|update|
    # patch|set)\b` matches only the two-token `gcloud <noun> <verb>` shape, and essentially every
    # mutating gcloud command an agent would actually reach for is three tokens -- `gcloud container
    # clusters create|delete|update`, `gcloud compute instances create|delete`, `gcloud iam
    # service-accounts create`, `gcloud container node-pools create|delete`. The forbidden-gcloud
    # arm is therefore much narrower than it reads, and has been since it was written.
    #
    # Strengthening the pattern is a change to an assertion, and this repo does not change an
    # assertion in the same unit as the work that surfaced it (PROTOCOL §10.1 / Guardrail 9) -- the
    # unit that widens the regex has to run against a tree where the widened form matters, or the
    # widening is untested. Routed to `harness-improve`. The control uses a form the pattern claims
    # to catch so that this arm measures the scan as written rather than as intended; a control
    # tuned to a bug is how a bug gets a green test of its own.
    POSITIVE_CONTROL = """\
Run `kubectl get pods` first, then apply it:

```bash
kubectl apply -f manifest.yaml
gcloud projects create kage-scratch
```

Then git push the branch and open the PR with gh pr create --fill.
"""

    # The same text with every command moved into an inline span -- a skill MENTIONING the commands
    # it forbids, which is what this skill's own body does and what the scan must not flag.
    NEGATIVE_CONTROL = """\
This path never runs `kubectl apply -f manifest.yaml`, never
`gcloud container clusters update prod`, never `git push`, and never `gh pr create`.
There is no propose verb here.
"""

    def test_the_rule_is_scanning_something(self):
        """Non-vacuity, and the discrimination the scan actually turns on.

        THIS USED TO READ `agents/platform/skills/submit-suggestion/SKILL.md`, on the reasoning that
        the skill this one replaces was "still in the tree for one more phase, which makes it a free
        positive control". P13-T5 was pulled forward from Phase 13 into Phase 9 and deleted it, and
        the arm went from proving non-vacuity to raising `FileNotFoundError`. The lesson is narrow
        and worth keeping: a positive control anchored on a file that is scheduled for deletion is
        not free, it is borrowed, and the loan comes due in the one commit where the scan most needs
        to still work. The control is now synthetic and cannot be deleted out from under the scan.

        It is also strictly stronger than what it replaces, which only asserted that SOME pattern
        fired against SOME file. Two directions are asserted here, and the second is the one that
        matters: `instruction_text` exists to tell a command the skill ISSUES from one it MENTIONS,
        and a regex that flags both would redden this skill's own body -- whose job includes naming
        the commands that never appear in this path. An `instruction_text` returning "" passes the
        first direction and fails nothing; one returning the raw markdown passes the first and fails
        the second.
        """
        fired = [what for pattern, what in self.FORBIDDEN if re.search(pattern, self.instruction_text(self.POSITIVE_CONTROL))]
        self.assertEqual(
            len(fired),
            len(self.FORBIDDEN),
            f"the forbidden-command scan missed {len(self.FORBIDDEN) - len(fired)} of its own patterns against a "
            f"control that plants every one of them: fired only {fired}. The scan is reading the wrong text and "
            "test_the_body_issues_no_mutating_command is vacuous.",
        )

        spared = [what for pattern, what in self.FORBIDDEN if re.search(pattern, self.instruction_text(self.NEGATIVE_CONTROL))]
        self.assertEqual(
            spared,
            [],
            f"the scan flagged {spared} in text that only MENTIONS those commands inside inline code spans. "
            "instruction_text is not stripping spans, so any skill that documents what it refuses to run is red "
            "-- including this one.",
        )

    def test_the_skill_names_no_brake_verb_as_something_the_agent_does(self):
        """V-CTR-011: no pause/resume/freeze/approve/reject/uncontest tool anywhere in the agent surface."""
        tools = set(declared_tools("platform"))
        for verb in BRAKE_VERBS:
            with self.subTest(verb=verb):
                self.assertNotIn(verb, tools, f"a `{verb}` tool exists; an agent must not release its own gate")
        called = tools_the_skill_calls(skill_path("platform").read_text())
        self.assertFalse(
            called & set(BRAKE_VERBS),
            f"the skill instructs a brake call: {sorted(called & set(BRAKE_VERBS))}",
        )

    def test_the_skill_tells_the_agent_a_refusal_is_not_an_obstacle(self):
        """The realistic bad loop: refused, reworded, resubmitted until something sticks."""
        self.assertRegex(
            skill_path("platform").read_text(),
            r"a refusal is a decision|not an obstacle|Do not retry it in a different shape",
        )


if __name__ == "__main__":
    unittest.main()

"""
POC: multi-agent team via claude-agent-sdk.

Lead agent gets a "team" of named subagents (AgentDefinition). Lead
delegates pieces of the task to them using the built-in Task tool —
no manual orchestration code needed, SDK handles spawn/dispatch.

Run: python team.py "<task for the team>"
"""

import argparse
import anyio

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

# Shared tone rule: this team's output lands in a chat, not a document. A
# teammate's reply should read like one person's chat message — short,
# plain, no essay/report formatting — so the human can read a beat and
# decide, instead of getting a wall of text to scroll through.
CHAT_STYLE = (
    "Write like a chat message, not a report: a few short sentences, plain "
    "language, no long bullet essays or headers unless the content is truly "
    "a list. If there's a lot to say, give the short version and add "
    "'ask if you want more detail' rather than dumping everything."
)

# If a teammate needs input before it can keep going, it should sound like a
# person asking, not a form: one question, then stop and wait.
ONE_QUESTION_RULE = (
    "If you need something from the user before you can proceed, ask exactly "
    "ONE question and stop there — don't list several questions at once. If "
    "you have more than one, ask the most important/blocking one first; "
    "you'll get another turn to ask the rest once this one's answered."
)

TEAM = {
    "researcher": AgentDefinition(
        description="Gathers facts, reads code/docs, summarizes findings. No writing/editing.",
        prompt=(
            "You are the researcher. Investigate the assigned question using "
            "read-only tools (Read, Grep, Glob, WebSearch, WebFetch). Return a "
            "concise, sourced summary. Never edit files. "
            + CHAT_STYLE + " " + ONE_QUESTION_RULE
        ),
        tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
        model="sonnet",
    ),
    "coder": AgentDefinition(
        description="Writes or edits code/files for a well-scoped subtask.",
        prompt=(
            "You are the coder. Implement exactly the subtask you're given. "
            "Keep changes minimal and scoped. Report what you changed and why "
            "in a couple sentences — the code itself is the detail, the "
            "message around it should be short. "
            + CHAT_STYLE + " " + ONE_QUESTION_RULE
        ),
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        model="sonnet",
    ),
    "reviewer": AgentDefinition(
        description="Reviews code/output for correctness and quality, reports issues.",
        prompt=(
            "You are the reviewer. Check the given code/output for bugs, "
            "inconsistencies, and missed edge cases. Report only real issues, "
            "most severe first, one short line each — skip nitpicks nobody "
            "asked for. If it's fine, just say so briefly. Do not fix issues "
            "yourself. " + CHAT_STYLE + " " + ONE_QUESTION_RULE
        ),
        tools=["Read", "Grep", "Glob"],
        model="sonnet",
    ),
}

LEAD_PROMPT = (
    "You are the lead of a small team: researcher, coder, reviewer. Each exists "
    "for a reason: researcher finds facts/context before work starts so "
    "decisions aren't guesses; coder does the actual writing/editing; reviewer "
    "catches mistakes coder won't see in their own work, same as a real team's "
    "code review. "
    "\n\n"
    "This division of labor is the default pipeline, not something the user "
    "has to ask for. If the user just says 'write a function' or 'fix this "
    "bug', still run it through the team: researcher first if the task needs "
    "context you don't already have (skip if not), then coder implements, "
    "then reviewer checks coder's work before you report back — even though "
    "the user only mentioned coding, and even if the change looks trivial to "
    "you. For ANY task that produces code, coder always writes it and "
    "reviewer always checks it — you never write or judge the code yourself, "
    "no exceptions for size or simplicity. Research is the only role that's "
    "ever optional (skip it only when the task needs no fact-finding, e.g. a "
    "pure code task with no unknowns, or a question with no coding at all)."
    "\n\n"
    "Delegate via the Agent tool (subagent_type = researcher | coder | "
    "reviewer). Always delegate synchronously — never set run_in_background "
    "— and wait for each teammate's actual result before moving on. If "
    "reviewer flags a real problem, send it back to coder to fix, then "
    "re-review — don't just report the flaw and stop. "
    "\n\n"
    "If a teammate's result is a question for the user rather than "
    "finished work, do not answer it yourself, do not guess an assumption "
    "and keep going, and do not keep delegating past it. Relay ONLY the "
    "single most important/blocking question to the user, in your own "
    "words if needed, and stop your turn there — even if the teammate "
    "listed several, you pick just one and hold the rest back for a later "
    "turn. When the user replies, delegate back to the same teammate with "
    "the answer (plus a note that you're still holding other questions) so "
    "they can continue. " + ONE_QUESTION_RULE + " "
    "Do not do researcher/coder/reviewer work yourself — delegate it. "
    "\n\n"
    "This is a chat, not a document. Don't silently run the whole pipeline "
    "then dump one giant final report. Send a short message before "
    "delegating (what you're about to do), keep the final synthesis to a "
    "few short sentences — the highlights, not everything each teammate "
    "said — and end by naming 1-2 concrete next steps or questions so the "
    "user has something to react to. " + CHAT_STYLE
)


async def run(task: str) -> None:
    options = ClaudeAgentOptions(
        system_prompt=LEAD_PROMPT,
        agents=TEAM,
        # Lead only needs to delegate + synthesize.
        allowed_tools=["Agent"],
        permission_mode="acceptEdits",
        cwd=".",
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(task)

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"\n[lead] {block.text}")
                    elif isinstance(block, ToolUseBlock) and block.name == "Agent":
                        sub = block.input.get("subagent_type", "?")
                        desc = block.input.get("description", "")
                        print(f"\n[delegate -> {sub}] {desc}")
            elif isinstance(message, ResultMessage):
                print(f"\n[done] turns={message.num_turns} cost=${message.total_cost_usd:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude SDK multi-agent team POC")
    parser.add_argument("task", help="Task to hand to the team")
    args = parser.parse_args()
    anyio.run(run, args.task)


if __name__ == "__main__":
    main()

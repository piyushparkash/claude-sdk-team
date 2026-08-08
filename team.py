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

TEAM = {
    "researcher": AgentDefinition(
        description="Gathers facts, reads code/docs, summarizes findings. No writing/editing.",
        prompt=(
            "You are the researcher. Investigate the assigned question using "
            "read-only tools (Read, Grep, Glob, WebSearch, WebFetch). Return a "
            "concise, sourced summary. Never edit files."
        ),
        tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
        model="sonnet",
    ),
    "coder": AgentDefinition(
        description="Writes or edits code/files for a well-scoped subtask.",
        prompt=(
            "You are the coder. Implement exactly the subtask you're given. "
            "Keep changes minimal and scoped. Report what you changed and why."
        ),
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        model="sonnet",
    ),
    "reviewer": AgentDefinition(
        description="Reviews code/output for correctness and quality, reports issues.",
        prompt=(
            "You are the reviewer. Check the given code/output for bugs, "
            "inconsistencies, and missed edge cases. Report findings as a short "
            "bullet list, most severe first. Do not fix issues yourself."
        ),
        tools=["Read", "Grep", "Glob"],
        model="sonnet",
    ),
}

LEAD_PROMPT = (
    "You are the lead of a small team: researcher, coder, reviewer. "
    "Break the user's task into subtasks and delegate each to the right "
    "teammate via the Task tool (subagent_type = researcher | coder | reviewer). "
    "Do not do researcher/coder/reviewer work yourself — delegate it. "
    "Synthesize their results into one final answer for the user."
)


async def run(task: str) -> None:
    options = ClaudeAgentOptions(
        system_prompt=LEAD_PROMPT,
        agents=TEAM,
        # Lead only needs to delegate + synthesize.
        allowed_tools=["Task"],
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
                    elif isinstance(block, ToolUseBlock) and block.name == "Task":
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

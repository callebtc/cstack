#!/usr/bin/env python3
"""Prepare or run one pinned external review; publishing and fixes stay with the caller."""

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import os
from pathlib import Path
import re
import shutil
import signal
from string import Template
import subprocess
import sys
from urllib.parse import urlsplit


MODELS = {
    "codex": "gpt-6.2-sol",
    "claude": "claude-opus-5-5",
    "gemini": "gemini-4-neon-high",
}
REVIEW_STATUS_LINE = re.compile(r"^Review status: (?:complete|incomplete)[ \t\r]*$", re.MULTILINE)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise ValueError("Git validation failed; check the checkout and requested commits.")
    return result.stdout.strip()


def commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def review_skill_path(explicit):
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path.home() / ".agents/skills/code-review/SKILL.md",
        Path.home() / ".claude/skills/code-review/SKILL.md",
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        / "skills/code-review/SKILL.md",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ValueError("The installed code-review/SKILL.md is required.")


def private_file(path, body):
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(body)


def validate(args):
    repo = Path(args.repo).expanduser().resolve()
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("--repo must identify the review checkout root.")
    base, head = commit(repo, args.base), commit(repo, args.head)
    if git(repo, "rev-parse", "HEAD") != head:
        raise ValueError("Checkout HEAD differs from the requested published PR head.")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Use a clean review checkout; preserve the coding checkout's changes.")
    if not git(repo, "diff", "--name-only", f"{base}...{head}"):
        raise ValueError("The PR comparison is empty; no review was launched.")
    context = Path(args.context).expanduser().read_text(encoding="utf-8").strip()
    if not context:
        raise ValueError("Supply feature requirements and review context in --context.")
    url = urlsplit(args.pr)
    if url.scheme not in {"https", "http"} or not url.netloc or url.username or url.password:
        raise ValueError("Supply a canonical PR URL without embedded credentials.")
    output = Path(args.output_dir).expanduser().resolve()
    if output.is_relative_to(repo) or output.exists():
        raise ValueError("--output-dir must be a new private directory outside the repository.")
    return repo, base, head, context, output


def invocation(args, output, review_skill, base):
    if args.reviewer == "codex":
        return [
            "codex", "exec", "--model", MODELS["codex"],
            "--config", 'model_reasoning_effort="high"',
            "--config", 'approval_policy="never"', "--sandbox", "read-only",
            "--enable", "multi_agent",
            "--ephemeral", "--json", "--output-last-message", str(output / "raw-review.md"),
            "-",
        ]
    if args.reviewer == "gemini":
        settings = {
            "agentMode": "",
            "permissions": {
                "allow": [
                    "command(regex:^git rev-parse HEAD$)",
                    rf"command(regex:^git diff {base}\.\.\.HEAD$)",
                    rf"command(regex:^git log {base}\.\.HEAD --oneline$)",
                ],
                "deny": ["write_file(*)", "unsandboxed(*)", "read_url(*)", "execute_url(*)", "mcp(*)"],
            },
        }
        return [
            "agy", "--model", MODELS["gemini"], "--sandbox", "--settings", json.dumps(settings),
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--log-file", str(output / "agy.log"),
        ]
    return [
        "claude", "--print", "--model", MODELS["claude"], "--effort", "high",
        "--output-format", "json", "--no-session-persistence",
        "--permission-mode", "plan", "--permission-prompts", "none",
        "--add-dir", str(review_skill.parent),
        "--tools", "Read,Glob,Grep,Bash,Agent,Skill",
    ]


def launch(command, repo, prompt, output, timeout):
    environment = os.environ.copy()
    if command[0] == "claude":
        environment.pop("CLAUDECODE", None)
    antigravity = command[0] == "agy"
    with (
        (output / "cli.stdout").open("x") as stdout,
        (output / "cli.stderr").open("x") as stderr,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        process = subprocess.Popen(
            command, cwd=repo, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if antigravity else stdout, stderr=stderr,
            text=True, start_new_session=True, env=environment,
        )
        try:
            if antigravity:
                workers.submit(antigravity_turn, process, prompt, stdout).result(timeout=timeout)
            else:
                process.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, FutureTimeout, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise ValueError("Reviewer interrupted or timed out; review remains incomplete.") from None
    if process.returncode:
        raise ValueError("Reviewer CLI failed; inspect private CLI diagnostics and report the blocker.")


def antigravity_turn(process, prompt, stdout):
    def send(content):
        message = {"event": "user", "message": {"content": content}}
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    continued = False
    with process.stdin, process.stdout:
        try:
            send(prompt)
        except BrokenPipeError:
            process.stdin.close()
        # Keep stdin open while the Antigravity parent waits for background subagents.
        for line in process.stdout:
            stdout.write(line)
            stdout.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "result":
                continue
            result = event.get("result")
            if not continued and not process.stdin.closed and unfinished_antigravity_turn(result):
                send(
                    "Continue the same review from the evidence already collected. Complete any "
                    "pending Standards and Spec subagent reviews and wait for both results. "
                    "Return the prescribed report, including any coverage gaps."
                )
                continued = True
            else:
                process.stdin.close()
    process.wait()


def unfinished_antigravity_turn(result):
    return (
        isinstance(result, dict) and result.get("status") == "SUCCESS" and not result.get("error")
        and isinstance(result.get("response"), str)
        and not REVIEW_STATUS_LINE.search(result["response"])
    )


def antigravity_response(stdout):
    model_confirmed = False
    result = None
    with stdout.open(encoding="utf-8") as events:
        for line in events:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Antigravity returned an invalid review stream.")
            if event.get("event") == "init":
                init = event.get("init")
                if not isinstance(init, dict) or init.get("model") != MODELS["gemini"]:
                    raise ValueError("Antigravity did not report the requested model.")
                model_confirmed = True
            elif event.get("event") == "result":
                if result is not None and not unfinished_antigravity_turn(result):
                    raise ValueError("Antigravity returned another result after a terminal review.")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Antigravity returned an invalid result.")
    if not model_confirmed:
        raise ValueError("Antigravity did not report the requested model.")
    if result is None or result.get("status") != "SUCCESS" or result.get("error"):
        raise ValueError("Antigravity returned no successful final result; review remains incomplete.")
    response = result.get("response")
    if not isinstance(response, str):
        return ""
    # The final Antigravity response can include commentary from earlier in the turn.
    status = REVIEW_STATUS_LINE.search(response)
    return (response[status.start():] if status else response).strip()


def report_body(reviewer, output):
    if reviewer == "codex":
        path = output / "raw-review.md"
        if not path.is_file():
            raise ValueError("Codex returned no final review; review remains incomplete.")
        body = path.read_text(encoding="utf-8").strip()
    elif reviewer == "gemini":
        body = antigravity_response(output / "cli.stdout")
    else:
        result = json.loads((output / "cli.stdout").read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
            raise ValueError("Claude returned an unsuccessful result; review remains incomplete.")
        usage = result.get("modelUsage", {})
        if usage and not any(
            name == MODELS["claude"] or name.startswith(MODELS["claude"] + "-")
            for name in usage
        ):
            raise ValueError("Claude did not report using the requested model; inspect private diagnostics.")
        body = result.get("result", "")
        body = body.strip() if isinstance(body, str) else ""
    if not body:
        raise ValueError("Reviewer returned an empty report; review remains incomplete.")
    return body


def run(args):
    repo, base, head, context, output = validate(args)
    review_skill = review_skill_path(args.review_skill)
    template = Path(__file__).resolve().parent.parent / "references/reviewer-prompt.md"
    prompt = Template(template.read_text(encoding="utf-8")).substitute(
        pr=args.pr, round=args.round, base=base, head=head,
        review_skill_body=review_skill.read_text(encoding="utf-8"),
        context=context,
    )
    command = invocation(args, output, review_skill, base)
    if not args.dry_run and not shutil.which(command[0]):
        raise ValueError("The selected reviewer CLI is not installed or is absent from PATH.")
    output.mkdir(mode=0o700)
    private_file(output / "prompt.md", prompt)
    manifest = {
        "reviewer": args.reviewer, "requested_model": MODELS[args.reviewer],
        "effort": "high", "round": args.round, "pr": args.pr, "base": base, "head": head,
        "status": "prepared", "command": command,
    }
    private_file(output / "invocation.json", json.dumps(manifest, indent=2) + "\n")
    if args.dry_run:
        print("Prepared private prompt and invocation; no reviewer launched.")
        return 0
    try:
        body = execute_review(args, command, repo, prompt, output, head)
    except (ValueError, OSError):
        manifest["status"] = "review-incomplete"
        (output / "invocation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        raise
    complete = body.splitlines()[0].strip() == "Review status: complete"
    sections = ["## Standards", "## Spec", "## Bugs and regressions", "## Coverage"]
    if args.round > 1:
        sections.append("## Previous findings")
    complete = complete and all(section in body.splitlines() for section in sections)
    title = (
        f"External Review Loop — round {args.round}\n\n"
        f"Reviewer: {command[0]}; requested model: `{MODELS[args.reviewer]}`; effort: high.\n"
        f"Base: `{base}`; reviewed head: `{head}`.\n\n"
    )
    private_file(output / "review.md", title + body + "\n")
    manifest["status"] = "review-complete" if complete else "review-incomplete"
    (output / "invocation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Review captured privately; assess coverage, sanitize, and verify current PR revisions before publishing.")
    return 0 if complete else 2


def execute_review(args, command, repo, prompt, output, head):
    launch(command, repo, prompt, output, args.timeout_seconds)
    if git(repo, "rev-parse", "HEAD") != head or git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("The review checkout changed during review; treat the result as incomplete.")
    return report_body(args.reviewer, output)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", required=True, choices=MODELS)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--round", required=True, type=positive)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--review-skill")
    parser.add_argument("--timeout-seconds", type=positive, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        message = "Review preparation or result parsing failed; inspect private local state."
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            message = str(error)
        print(f"External review incomplete: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

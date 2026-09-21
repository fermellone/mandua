"""Bounded, read-only discovery; branch names are not semantic classifications."""

import argparse
import json
import os
import subprocess

from mandua.agent_view import agent_result, agent_scope
from mandua.errors import ManduaError
from mandua.memory_service import MemoryService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--left")
    parser.add_argument("--right")
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--format", choices=("agent", "json"), default="agent")
    args = parser.parse_args()
    if bool(args.left) != bool(args.right):
        parser.error("--left and --right must be supplied together")
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    limits = []

    def git(*argv):
        proc = subprocess.run(
            [
                "git",
                "--no-pager",
                "-C",
                args.repo,
                "-c",
                "core.quotePath=true",
                "-c",
                "core.pager=cat",
                "-c",
                "maintenance.auto=false",
                "-c",
                "log.showSignature=false",
                *argv,
            ],
            env={
                **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
            capture_output=True,
            timeout=15,
            check=False,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:1500])
        return proc.stdout.decode("utf-8", "replace")

    def clip(text, size, label):
        if len(text) > size:
            limits.append(label)
            return text[:size] + "\n[TRUNCATED]"
        return text

    def oid(ref):
        return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()

    if args.left:
        left, right = oid(args.left), oid(args.right)
        result = {}
        result["comparison"] = (
            MemoryService.open(args.repo).compare(left, right, limit=args.limit).to_dict()
        )
        result["comparison_patch"] = clip(
            git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--unified=2",
                left,
                right,
                "--",
            ),
            5000,
            "comparison_patch",
        )
        result["limits"] = limits
        if args.format == "agent":
            result["comparison"] = agent_result(result["comparison"])
            result["scope"] = agent_scope("compare")
            result["scope"]["retrieval"] += " Includes a bounded content diff."
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return

    head = oid("HEAD")
    refs_text = git(
        "for-each-ref",
        "--count=33",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    refs = [dict(zip(("ref", "oid"), line.split(" ", 1))) for line in refs_text.splitlines()]
    if len(refs) > 32:
        limits.append("refs: only first 32 shown")
        refs = refs[:32]
    notes_ref = git("for-each-ref", "--format=%(objectname)", "refs/notes/review").strip()
    revisions = git(
        "rev-list",
        "--date-order",
        f"--max-count={args.limit + 1}",
        head,
        "--branches",
        "--remotes",
        "--tags",
        "--",
    ).splitlines()
    if len(revisions) > args.limit:
        limits.append(f"history: only latest {args.limit} reachable commits shown")
    revisions = revisions[: args.limit]
    commits = []
    patches = 0
    tip_oids = {r["oid"] for r in refs}
    for rev in revisions:
        body = git("show", "-s", "--format=%B", rev).strip()
        parents = git("show", "-s", "--format=%P", rev).strip().split()
        entry = {"oid": rev, "parents": parents, "message": clip(body, 1400, f"message:{rev}")}
        if notes_ref:
            # %N returns empty when the commit has no review note.
            note = git("show", "-s", "--format=%N", "--notes=review", rev).strip()
            if note:
                entry["review"] = clip(note, 800, f"review:{rev}")
        # Excerpts expose evidence, never classify a branch as an alternative.
        wants_patch = rev in tip_oids or "memory-type: hypothesis" in body.lower()
        if wants_patch and len(parents) <= 1:
            if patches < 6:
                patch = git(
                    "show",
                    "--format=",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--unified=1",
                    rev,
                    "--",
                )
                entry["patch"] = clip(patch, 1400, f"patch:{rev}")
                patches += 1
            else:
                limits.append(f"patch omitted:{rev}")
        commits.append(entry)
    result = {
        "head": head,
        "refs": refs,
        "commits": commits,
        "scope": {
            "shallow": git("rev-parse", "--is-shallow-repository").strip() == "true",
            "review_notes_available": bool(notes_ref),
            "history_sources": "HEAD, local branches, remote-tracking refs and tags; no fetch",
            "selection": "Candidates only. Branches are not necessarily competing alternatives; ancestry is not proof of acceptance.",
        },
    }
    result["limits"] = limits
    if args.format == "agent":
        result["scope"].update(agent_scope("alternatives"))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ManduaError) as error:
        print(json.dumps({"error": str(error), "incomplete": True}, ensure_ascii=False))
        raise SystemExit(1)

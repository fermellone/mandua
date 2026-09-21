"""Inspect recorded corrections and committed file state without constructing Git commands."""

import argparse
import json
from pathlib import Path, PurePosixPath

from mandua.agent_view import agent_result, agent_scope
from mandua.errors import ManduaError
from mandua.git_runner import GitRunner
from mandua.memory_service import MemoryService
from mandua.repository import RepositoryInspector


def inspect(repo, decision_id, path, revision="HEAD", limit=20):
    service = MemoryService.open(Path(repo))
    inspector = RepositoryInspector(Path(repo))
    inspector.validate_path(PurePosixPath(path))
    current_oid = inspector.resolve_commit(revision)
    decision = service.decision(decision_id, limit=limit).to_dict()
    runner = GitRunner(Path(repo))
    limits = []

    def snapshot(oid):
        result = {"oid": oid, "path": path}
        try:
            content = runner.run_text(["cat-file", "blob", f"{oid}:{path}"]).stdout
            result["content"] = content[:3000]
            result["truncated"] = len(content) > 3000
            if result["truncated"]:
                limits.append(f"content clipped: {oid}:{path}")
            result["available"] = True
        except ManduaError as error:
            result.update(available=False, error=str(error))
            limits.append(f"content unavailable: {oid}:{path}")
        return result

    records = {}
    for item in decision["evidence"]:
        if item["kind"] not in {"decision-commit", "decision-correction"}:
            continue
        records[item["oid"]] = item
    # Prefer linked corrections and originals if the result exceeds the snapshot budget.
    oids = []
    for item in records.values():
        targets = item["details"].get("verified_corrects", [])
        if targets:
            oids.extend([item["oid"], *targets])
    oids = list(dict.fromkeys([*oids, *records]))
    if len(oids) > 12:
        limits.append(
            "Only 12 historical file snapshots shown; consult decision evidence limits too."
        )
    snapshots = []
    for oid in oids[:12]:
        entry = snapshot(oid)
        entry["ancestor_of_current"] = inspector.is_ancestor_oids(oid, current_oid)
        snapshots.append(entry)
    return {
        "decision": decision,
        "current": {"requested_revision": revision, **snapshot(current_oid)},
        "records": snapshots,
        "scope": {
            "state": "Committed content at the resolved revision; excludes uncommitted edits.",
            "selection": "Corrects is a declared link; verified_corrects also checks ancestry. Neither proves empirical truth or that the correction remains current.",
            "history": "Decision search includes other local refs; check ancestor_of_current before treating a record as integrated. No fetch.",
        },
        "limits": limits,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--decision", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=("agent", "json"), default="agent")
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    result = inspect(args.repo, args.decision, args.path, args.revision, args.limit)
    if args.format == "agent":
        result["decision"] = agent_result(result["decision"])
        result["scope"].update(agent_scope("corrections"))
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ManduaError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "incomplete": True}, ensure_ascii=False))
        raise SystemExit(1)

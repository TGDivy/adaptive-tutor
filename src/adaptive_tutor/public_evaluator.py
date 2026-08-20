"""Minimal entry point used by the protected GitHub-hosted evaluator workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import TutorError
from .runner import evaluate_public_workspace_to_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-key", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--assignment-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--dispatch-nonce", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--evaluator-kit-digest", required=True)
    parser.add_argument("--evaluator-ref", required=True)
    parser.add_argument("--workflow-digest", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--repository-id", required=True, type=int)
    arguments = parser.parse_args(argv)
    try:
        evidence = evaluate_public_workspace_to_file(
            verification_key_path=arguments.verification_key,
            workspace=arguments.workspace,
            output_path=arguments.output,
            assignment_id=arguments.assignment_id,
            branch=arguments.branch,
            commit_sha=arguments.commit_sha,
            dispatch_nonce=arguments.dispatch_nonce,
            expected_manifest_digest=arguments.manifest_digest,
            expected_evaluator_kit_digest=arguments.evaluator_kit_digest,
            evaluator_ref=arguments.evaluator_ref,
            workflow_digest=arguments.workflow_digest,
            workflow_commit=arguments.workflow_commit,
            repository_id=arguments.repository_id,
        )
    except (TutorError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    state = "passed" if evidence.learner_passed else "failed"
    print(f"Public deterministic evaluation {state}; evidence written to {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

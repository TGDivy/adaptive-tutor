"""End-to-end event orchestration across scheduling, GitHub, grading, and state."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .assignments import (
    AssignmentService,
    AssignmentValidator,
)
from .config import TutorSettings
from .curriculum import CurriculumLoader
from .db import Database
from .errors import ExternalServiceError, InfrastructureError, SecurityError
from .evaluation import EvaluationService, EvidenceNormalizer, render_review
from .generation import CurriculumAssignmentGenerator
from .github import GitHubClient
from .jobs import JobQueue
from .models import AssignmentBundle, AssignmentRequest, LearnerContext, QualitativeEvaluation
from .scheduler import AdaptiveScheduler
from .security import sha256_digest
from .time import iso_now
from .trusted_bundles import TrustedBundleStore

_CONFIDENCE = re.compile(r"(?:^|\b)confidence\s*[:=]\s*(\d{1,3})(?:\b|$)", re.I)


class TutorOrchestrator:
    def __init__(
        self,
        settings: TutorSettings,
        database: Database,
        github: GitHubClient,
        evaluations: EvaluationService,
        *,
        queue: JobQueue | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.github = github
        self.evaluations = evaluations
        self.queue = queue or JobQueue(database)
        self.assignments = AssignmentService(database)
        self.trusted_bundles = TrustedBundleStore(settings.data_dir)

    def handlers(self) -> dict[str, Any]:
        return {
            "record_submission": self.record_submission,
            "reconcile_pull_request": self.reconcile_pull_request,
            "process_ci_result": self.process_ci_result,
            "reconcile_checks": self.reconcile_checks,
            "process_learner_command": self.process_learner_command,
            "review_appeal": self.review_appeal,
        }

    def create_next_assignment(self, context: LearnerContext) -> dict[str, Any]:
        if self._paused():
            raise ValueError("Tutor is paused; run 'adaptive-tutor resume' first")
        active = self.assignments.active(self.settings.learner_id)
        if active:
            if active["status"] == "validated" and not active.get("pull_number"):
                return self._publish_assignment(active, active["bundle"], scheduler=None)
            return {"existing": True, **active}
        candidates = AdaptiveScheduler(self.database).recommend(
            self.settings.learner_id,
            self.settings.active_curriculum,
            self.settings.active_profile,
            context,
            limit=1,
        )
        if not candidates:
            raise ValueError("No schedulable concepts are available")
        candidate = candidates[0]
        recent = self.database.fetch_all(
            """
            SELECT a.slug, a.exercise_type, a.difficulty, a.created_at,
                   json_extract(a.bundle_json, '$.generator_metadata.blueprint_id') blueprint_id,
                   (SELECT ac.concept_id FROM assignment_concepts ac
                    WHERE ac.assignment_id=a.id AND ac.is_primary=1) primary_concept
            FROM assignments a
            WHERE a.learner_id=? ORDER BY a.created_at DESC LIMIT 8
            """,
            (self.settings.learner_id,),
        )
        misconceptions = self.database.fetch_all(
            """
            SELECT concept_id, description, status, severity, frequency
            FROM misconceptions WHERE learner_id=?
                AND status IN ('suspected','active','challenged','recurred')
            ORDER BY severity DESC, frequency DESC
            """,
            (self.settings.learner_id,),
        )
        package = self._curriculum_package(candidate.concept_id)
        concept = next(item for item in package.concepts if item.id == candidate.concept_id)
        references = {
            path: (package.root / "references" / path).read_text(encoding="utf-8")
            for path in concept.reference_files
        }
        concept_state = self.database.fetch_one(
            """
            SELECT mastery_estimate, uncertainty, evidence_count,
                   highest_successful_difficulty, recent_performance,
                   long_term_performance, next_review, confidence_calibration, trend
            FROM mastery WHERE learner_id=? AND concept_id=?
            """,
            (self.settings.learner_id, candidate.concept_id),
        ) or {}
        request = AssignmentRequest(
            learner_id=self.settings.learner_id,
            curriculum_id=self.settings.active_curriculum,
            profile_id=self.settings.active_profile,
            target_concepts=[candidate.concept_id],
            active_misconceptions=misconceptions,
            recent_assignments=recent,
            target_difficulty=candidate.target_difficulty,
            context=context.model_copy(update={"allowed_formats": [candidate.exercise_type]}),
            trusted_references=references,
            concept_state={candidate.concept_id: concept_state},
            selection_reason=candidate.reason,
            scheduler_factors=candidate.factors,
        )
        bundle = CurriculumAssignmentGenerator(package).generate(request)
        validation = AssignmentValidator().validate(bundle, request)
        created = self.assignments.create(request, bundle, validation)
        return self._publish_assignment(created, bundle, scheduler=candidate.model_dump())

    def _publish_assignment(
        self,
        created: dict[str, Any],
        bundle: AssignmentBundle,
        *,
        scheduler: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assignment_id = str(created["id"])
        branch = str(created["branch_name"])
        envelope = self.trusted_bundles.seal(
            assignment_id=assignment_id,
            branch=branch,
            bundle=bundle,
        )
        public_files = self.assignments.public_files(
            assignment_id,
            evaluator_binding=envelope.binding_digest,
            evaluator_key_id=envelope.key_id,
        )
        pull_body = (
            f"{bundle.summary}\n\n"
            f"Difficulty: **{bundle.difficulty}/10** · Expected time: "
            f"**{bundle.expected_minutes} minutes**\n\n"
            "Push solutions to this branch. Deterministic checks run without tutor credentials; "
            "the tutor posts structured feedback after CI evidence is available.\n\n"
            f"<!-- adaptive-tutor-assignment:{assignment_id} -->"
        )
        published = self.github.publish_assignment(
            branch=branch,
            title=f"{assignment_id}: {bundle.title}",
            body=pull_body,
            files=public_files,
        )
        now = iso_now()
        self.database.execute(
            """
            UPDATE assignments SET status='published', pull_number=?, head_sha=?,
                publication_sha=?, updated_at=? WHERE id=?
            """,
            (
                published["pull_number"],
                published["head_sha"],
                published["head_sha"],
                now,
                assignment_id,
            ),
        )
        result = {"existing": False, **created, **published}
        if scheduler is not None:
            result["scheduler"] = scheduler
        return result

    def record_submission(self, payload: dict[str, Any]) -> None:
        branch = _branch_from_ref(str(payload.get("ref", "")))
        if not branch:
            return
        assignment = self.database.fetch_one(
            "SELECT * FROM assignments WHERE branch_name=?", (branch,)
        )
        if assignment is None or payload.get("deleted"):
            return
        commit_sha = str(payload.get("after", ""))
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            raise SecurityError("Push event contains an invalid commit SHA")
        if assignment.get("publication_sha") == commit_sha:
            return
        message = str((payload.get("head_commit") or {}).get("message", ""))
        confidence_match = _CONFIDENCE.search(message)
        confidence = int(confidence_match.group(1)) if confidence_match else None
        if confidence is not None and not 0 <= confidence <= 100:
            confidence = None
        now = iso_now()
        self.database.execute(
            """
            INSERT OR IGNORE INTO attempts(
                id, assignment_id, commit_sha, stage_number, learner_confidence,
                submission_source, submitted_at
            ) VALUES (?, ?, ?, ?, ?, 'github_push', ?)
            """,
            (
                str(uuid.uuid4()),
                assignment["id"],
                commit_sha,
                assignment["current_stage"],
                confidence,
                now,
            ),
        )
        self.database.execute(
            """
            UPDATE assignments SET status='submitted', head_sha=?, updated_at=? WHERE id=?
            """,
            (commit_sha, now, assignment["id"]),
        )
        attempt = self.database.fetch_one(
            """
            SELECT id, evaluation_dispatched_at FROM attempts
            WHERE assignment_id=? AND commit_sha=? AND stage_number=?
            """,
            (assignment["id"], commit_sha, assignment["current_stage"]),
        )
        if attempt is None:  # pragma: no cover - insert/read invariant
            raise RuntimeError("Submission attempt was not recorded")
        if attempt["evaluation_dispatched_at"] is None:
            self.github.dispatch_evaluator(
                assignment_id=str(assignment["id"]),
                branch=branch,
                commit_sha=commit_sha,
            )
            self.database.execute(
                """
                UPDATE attempts SET evaluation_dispatched_at=?
                WHERE id=? AND evaluation_dispatched_at IS NULL
                """,
                (iso_now(), attempt["id"]),
            )

    def reconcile_pull_request(self, payload: dict[str, Any]) -> None:
        pull = payload.get("pull_request") or {}
        head = pull.get("head") or {}
        branch = str(head.get("ref", ""))
        assignment = self.database.fetch_one(
            "SELECT * FROM assignments WHERE branch_name=?", (branch,)
        )
        if assignment is None:
            return
        action = str(payload.get("action", ""))
        status = assignment["status"]
        completed_at = None
        if action == "closed":
            status = "completed" if pull.get("merged") else "cancelled"
            completed_at = iso_now()
        self.database.execute(
            """
            UPDATE assignments SET pull_number=?, head_sha=?, status=?,
                completed_at=COALESCE(?, completed_at), updated_at=? WHERE id=?
            """,
            (
                pull.get("number") or payload.get("number"),
                head.get("sha"),
                status,
                completed_at,
                iso_now(),
                assignment["id"],
            ),
        )

    def process_ci_result(self, payload: dict[str, Any]) -> None:
        workflow = payload.get("workflow_run") or {}
        if payload.get("action") != "completed":
            return
        run_id = int(workflow["id"])
        identity = self.github.verify_evaluator_run(run_id)
        branch = identity["branch"]
        commit_sha = identity["commit_sha"]
        assignment = self.database.fetch_one(
            "SELECT * FROM assignments WHERE id=? AND branch_name=?",
            (identity["assignment_id"], branch),
        )
        if assignment is None:
            raise SecurityError("Actions run does not match a stored assignment")
        attempt = self.database.fetch_one(
            "SELECT * FROM attempts WHERE assignment_id=? AND commit_sha=?",
            (assignment["id"], commit_sha),
        )
        if attempt is None:
            raise SecurityError("Actions run has no dispatched submission attempt")
        existing = self.database.fetch_one(
            """
            SELECT id, evaluation_json, review_external_id, review_posted_at
            FROM qualitative_evaluations
            WHERE attempt_id=? AND review_kind='initial'
            """,
            (attempt["id"],),
        )
        if workflow.get("conclusion") not in {"success", "failure"}:
            raise ExternalServiceError(
                f"Evaluation workflow ended as {workflow.get('conclusion')}", retryable=True
            )
        bundle = AssignmentBundle.model_validate_json(assignment["bundle_json"])
        if existing is None:
            evidence = EvidenceNormalizer.parse(
                self.github.download_evidence(run_id)
            )
            if evidence.assignment_id != assignment["id"] or evidence.commit_sha != commit_sha:
                raise SecurityError("Actions evidence does not match the assignment and commit")
            trusted_envelope = self.trusted_bundles.load(str(assignment["id"]))
            if (
                evidence.evaluator_binding != trusted_envelope.binding_digest
                or evidence.evaluator_key_id != trusted_envelope.key_id
            ):
                raise SecurityError("Actions evidence does not match the trusted evaluator")
            if evidence.has_operational_error:
                raise InfrastructureError(
                    "Evaluator reported an operational failure", retryable=True
                )
            automated_id = self.evaluations.persist_automated(str(attempt["id"]), evidence)
            submission = self._submission_files(bundle, commit_sha)
            prompt = self.database.fetch_one(
                """
                SELECT version, template_text FROM prompt_versions
                WHERE purpose='grading' AND active=1 ORDER BY created_at DESC LIMIT 1
                """
            )
            if prompt is None:
                raise ValueError("No active trusted grading prompt is loaded")
            references = {
                item.path: item.content for item in bundle.files if item.role == "reference"
            }
            evaluation_id, qualitative, injection_flags = self.evaluations.grade_attempt(
                learner_id=self.settings.learner_id,
                assignment_id=str(assignment["id"]),
                attempt_id=str(attempt["id"]),
                automated_evaluation_id=automated_id,
                rubric=bundle.rubric,
                references=references,
                submission=submission,
                trusted_instructions=str(prompt["template_text"]),
                prompt_version=str(prompt["version"]),
                learner_confidence=attempt["learner_confidence"],
            )
            review_posted = False
        else:
            evaluation_id = str(existing["id"])
            qualitative = QualitativeEvaluation.model_validate_json(existing["evaluation_json"])
            injection_flags = []
            review_posted = bool(existing["review_posted_at"])
        if assignment["pull_number"] and not review_posted:
            body = render_review(qualitative, injection_flags=injection_flags)
            marker = f"<!-- evaluation:{sha256_digest(qualitative.model_dump_json())} -->"
            review_id = self.github.ensure_review(
                int(assignment["pull_number"]),
                body,
                marker=marker,
                commit_sha=commit_sha,
            )
            self.database.execute(
                """
                UPDATE qualitative_evaluations
                SET review_external_id=?, review_posted_at=? WHERE id=?
                """,
                (review_id, iso_now(), evaluation_id),
            )
        self._finish_or_follow_up(assignment, attempt, qualitative)

    def reconcile_checks(self, payload: dict[str, Any]) -> None:
        """Persist check delivery activity; workflow artifacts remain authoritative."""
        check = payload.get("check_suite") or payload.get("check_run") or {}
        branch = str(check.get("head_branch") or "")
        assignment = self.database.fetch_one(
            "SELECT id FROM assignments WHERE branch_name=?", (branch,)
        )
        if assignment:
            self.database.execute(
                """
                INSERT INTO activity(id, learner_id, kind, summary, metadata_json, occurred_at)
                VALUES (?, ?, 'check_reconciled', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    self.settings.learner_id,
                    f"Check update for {assignment['id']}",
                    json.dumps(
                        {
                            "status": check.get("status"),
                            "conclusion": check.get("conclusion"),
                        },
                        sort_keys=True,
                    ),
                    iso_now(),
                ),
            )

    def process_learner_command(self, payload: dict[str, Any]) -> None:
        if payload.get("action") != "created":
            return
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        sender = payload.get("sender") or {}
        if "pull_request" not in issue:
            return
        trusted_sender = str(sender.get("login", "")).lower() == self.settings.github.owner.lower()
        trusted_sender |= str(comment.get("author_association", "")) == "OWNER"
        if not trusted_sender:
            raise SecurityError("Tutor commands are restricted to the repository owner")
        body = str(comment.get("body", "")).strip()
        if not body.lower().startswith("/tutor"):
            return
        pull_number = int(issue["number"])
        assignment = self.database.fetch_one(
            "SELECT * FROM assignments WHERE pull_number=?", (pull_number,)
        )
        if assignment is None:
            return
        command = body[len("/tutor") :].strip()
        if command == "hint":
            level, content = self.assignments.next_hint(
                str(assignment["id"]), self.settings.learner_id
            )
            self.github.post_comment(pull_number, f"### Hint {level}/5\n\n{content}")
        elif command.startswith("appeal "):
            latest = self.database.fetch_one(
                """
                SELECT q.id FROM qualitative_evaluations q
                JOIN attempts a ON a.id=q.attempt_id
                WHERE a.assignment_id=? ORDER BY q.created_at DESC LIMIT 1
                """,
                (assignment["id"],),
            )
            if latest is None:
                self.github.post_comment(pull_number, "There is no evaluation to appeal yet.")
                return
            argument = command.removeprefix("appeal ").strip()
            appeal_id = self.evaluations.create_appeal(
                str(assignment["id"]), str(latest["id"]), argument
            )
            self.queue.enqueue(
                "review_appeal",
                {"appeal_id": appeal_id, "pull_number": pull_number},
                deduplication_key=f"appeal:{appeal_id}",
            )
            self.github.post_comment(
                pull_number,
                "Appeal recorded. The original review remains preserved while an independent "
                "review is queued.",
            )
        elif command == "pause":
            self._set_paused(True)
            self.github.post_comment(pull_number, "Adaptive assignment creation is paused.")
        elif command == "resume":
            self._set_paused(False)
            self.github.post_comment(pull_number, "Adaptive assignment creation is resumed.")
        elif command == "status":
            self.github.post_comment(
                pull_number,
                f"Assignment `{assignment['id']}` is **{assignment['status']}**, stage "
                f"{assignment['current_stage']}.",
            )
        else:
            self.github.post_comment(
                pull_number,
                "Unknown command. Use `/tutor hint`, `/tutor appeal <reason>`, "
                "`/tutor status`, `/tutor pause`, or `/tutor resume`.",
            )

    def review_appeal(self, payload: dict[str, Any]) -> None:
        prompt = self.database.fetch_one(
            """
            SELECT version, template_text FROM prompt_versions
            WHERE purpose='grading' AND active=1 ORDER BY created_at DESC LIMIT 1
            """
        )
        if prompt is None:
            raise ValueError("No active trusted grading prompt is loaded")
        _, result = self.evaluations.resolve_appeal(
            str(payload["appeal_id"]),
            trusted_instructions=str(prompt["template_text"]),
            prompt_version=str(prompt["version"]),
        )
        self.github.post_comment(
            int(payload["pull_number"]),
            "## Appeal result\n\n" + render_review(result),
        )

    def _finish_or_follow_up(
        self, assignment: dict[str, Any], attempt: dict[str, Any], evaluation: Any
    ) -> None:
        now = iso_now()
        self.database.execute(
            "UPDATE attempts SET outcome=? WHERE id=?",
            ("success" if evaluation.overall_score >= 70 else "failure", attempt["id"]),
        )
        if evaluation.follow_up == "new_stage":
            next_stage = (
                int(assignment["current_stage"])
                if assignment["status"] == "follow_up"
                else self.assignments.unlock_follow_up(str(assignment["id"]))
            )
            if next_stage and assignment["pull_number"]:
                stage = self.database.fetch_one(
                    """
                    SELECT * FROM assignment_stages WHERE assignment_id=? AND stage_number=?
                    """,
                    (assignment["id"], next_stage),
                )
                if stage:
                    marker = f"<!-- adaptive-tutor-stage:{assignment['id']}:{next_stage} -->"
                    self.github.ensure_comment(
                        int(assignment["pull_number"]),
                        f"## Stage {next_stage}: {stage['title']}\n\n"
                        f"{stage['instructions']}\n\n{marker}",
                        marker=marker,
                    )
            return
        status = "reviewing" if evaluation.follow_up == "human_review" else "completed"
        self.database.execute(
            """
            UPDATE assignments SET status=?, updated_at=?, completed_at=? WHERE id=?
            """,
            (
                status,
                now,
                now if status == "completed" else None,
                assignment["id"],
            ),
        )

    def _submission_files(self, bundle: AssignmentBundle, commit_sha: str) -> dict[str, str]:
        result: dict[str, str] = {}
        total = 0
        for item in bundle.files:
            if item.role not in {"starter", "instructions", "public_test"}:
                continue
            content = self.github.get_file(item.path, commit_sha)
            total += len(content.encode())
            if total > 1_000_000:
                raise SecurityError("Submission text exceeds the qualitative-review size limit")
            result[item.path] = content
        return result

    def _curriculum_package(self, concept_id: str) -> Any:
        row = self.database.fetch_one(
            """
            SELECT c.id, cu.source_path FROM concepts c
            JOIN curricula cu ON cu.id=c.curriculum_id WHERE c.id=?
            """,
            (concept_id,),
        )
        if row is None:
            raise ValueError(f"No curriculum package found for concept {concept_id}")
        return CurriculumLoader().load(Path(str(row["source_path"])))

    def _paused(self) -> bool:
        row = self.database.fetch_one(
            "SELECT value_json FROM configuration WHERE key='paused'"
        )
        return bool(json.loads(row["value_json"])) if row else False

    def _set_paused(self, paused: bool) -> None:
        self.database.execute(
            """
            INSERT INTO configuration(key, value_json, updated_at) VALUES ('paused', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (json.dumps(paused), iso_now()),
        )


def _branch_from_ref(reference: str) -> str | None:
    prefix = "refs/heads/"
    return reference[len(prefix) :] if reference.startswith(prefix) else None

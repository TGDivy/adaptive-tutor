"""Weekly/monthly progress reporting for CLI, Markdown, and dashboard."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .db import Database
from .learner import LearnerModel
from .time import utc_now


@dataclass(frozen=True)
class ReportDocument:
    id: str
    period_type: Literal["weekly", "monthly"]
    period_start: str
    period_end: str
    data: dict[str, Any]
    markdown: str


class ReportService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def generate(
        self,
        learner_id: str,
        curriculum_id: str,
        period_type: Literal["weekly", "monthly"] = "weekly",
        *,
        end: datetime | None = None,
    ) -> ReportDocument:
        period_end = end or utc_now()
        period_start = period_end - timedelta(days=7 if period_type == "weekly" else 30)
        start_text = period_start.isoformat(timespec="seconds")
        end_text = period_end.isoformat(timespec="seconds")
        readiness = LearnerModel(self.database).readiness(learner_id, curriculum_id)
        calibration = LearnerModel(self.database).calibration(learner_id)
        activity = self.database.fetch_one(
            """
            SELECT COUNT(*) assignments,
                   COALESCE(SUM(expected_minutes), 0) planned_minutes
            FROM assignments
            WHERE learner_id=? AND created_at BETWEEN ? AND ?
            """,
            (learner_id, start_text, end_text),
        ) or {"assignments": 0, "planned_minutes": 0}
        activity["attempts"] = int(
            (
                self.database.fetch_one(
                    """
                    SELECT COUNT(*) count FROM attempts at
                    JOIN assignments a ON a.id=at.assignment_id
                    WHERE a.learner_id=? AND at.submitted_at BETWEEN ? AND ?
                    """,
                    (learner_id, start_text, end_text),
                )
                or {"count": 0}
            )["count"]
        )
        activity["hints"] = int(
            (
                self.database.fetch_one(
                    """
                    SELECT COUNT(*) count FROM hints
                    WHERE learner_id=? AND requested_at BETWEEN ? AND ?
                    """,
                    (learner_id, start_text, end_text),
                )
                or {"count": 0}
            )["count"]
        )
        movement = self.database.fetch_all(
            """
            SELECT c.id concept_id, c.name, c.domain,
                   ROUND(SUM(e.mastery_after - e.mastery_before), 4) movement,
                   COUNT(*) evidence_count
            FROM mastery_evidence e JOIN concepts c ON c.id=e.concept_id
            WHERE e.learner_id=? AND e.observed_at BETWEEN ? AND ?
                  AND e.mastery_before IS NOT NULL
            GROUP BY c.id ORDER BY ABS(SUM(e.mastery_after-e.mastery_before)) DESC
            """,
            (learner_id, start_text, end_text),
        )
        strengths = self._mastery_extremes(learner_id, curriculum_id, descending=True)
        weaknesses = self._mastery_extremes(learner_id, curriculum_id, descending=False)
        misconceptions = self.database.fetch_all(
            """
            SELECT status, COUNT(*) count FROM misconceptions
            WHERE learner_id=? GROUP BY status ORDER BY status
            """,
            (learner_id,),
        )
        difficulty = self.database.fetch_one(
            """
            SELECT ROUND(AVG(difficulty), 2) average,
                   COALESCE(MAX(difficulty), 0) highest,
                   COUNT(*) assignments
            FROM assignments WHERE learner_id=? AND created_at BETWEEN ? AND ?
            """,
            (learner_id, start_text, end_text),
        ) or {"average": 0, "highest": 0, "assignments": 0}
        retention = self.database.fetch_one(
            """
            SELECT COUNT(*) observations,
                   COALESCE(SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END), 0) successes,
                   COALESCE(SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END), 0) failures
            FROM mastery_evidence
            WHERE learner_id=? AND observed_at BETWEEN ? AND ?
            """,
            (learner_id, start_text, end_text),
        ) or {"observations": 0, "successes": 0, "failures": 0}
        retention["due_reviews"] = int(
            (
                self.database.fetch_one(
                    """
                    SELECT COUNT(*) count FROM mastery m JOIN concepts c ON c.id=m.concept_id
                    WHERE m.learner_id=? AND c.curriculum_id=? AND m.next_review <= ?
                    """,
                    (learner_id, curriculum_id, end_text),
                )
                or {"count": 0}
            )["count"]
        )
        usage = self.database.fetch_one(
            """
            SELECT COUNT(*) invocations, COALESCE(SUM(input_tokens), 0) input_tokens,
                   COALESCE(SUM(output_tokens), 0) output_tokens,
                   ROUND(COALESCE(SUM(cost_usd), 0), 6) cost_usd
            FROM model_invocations WHERE started_at BETWEEN ? AND ?
            """,
            (start_text, end_text),
        ) or {"invocations": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
        resolved = [item for item in misconceptions if item["status"] == "resolved"]
        active = [
            item
            for item in misconceptions
            if item["status"] in {"suspected", "active", "challenged", "recurred"}
        ]
        focus = [item["name"] for item in weaknesses[:3]]
        data: dict[str, Any] = {
            "study_activity": activity,
            "mastery_movement": movement,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "active_misconceptions": sum(int(item["count"]) for item in active),
            "resolved_misconceptions": sum(int(item["count"]) for item in resolved),
            "difficulty_progression": difficulty,
            "retention": retention,
            "confidence_calibration": calibration,
            "readiness_by_domain": readiness,
            "model_usage": usage,
            "recommended_focus": focus,
        }
        report_id = str(uuid.uuid4())
        markdown = render_markdown(period_type, start_text, end_text, data)
        self.database.execute(
            """
            INSERT INTO reports(
                id, learner_id, period_type, period_start, period_end,
                report_json, markdown, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(learner_id, period_type, period_start, period_end) DO UPDATE SET
                report_json=excluded.report_json, markdown=excluded.markdown,
                generated_at=excluded.generated_at
            """,
            (
                report_id,
                learner_id,
                period_type,
                start_text,
                end_text,
                json.dumps(data, sort_keys=True),
                markdown,
                end_text,
            ),
        )
        stored = self.database.fetch_one(
            """
            SELECT id FROM reports WHERE learner_id=? AND period_type=?
                AND period_start=? AND period_end=?
            """,
            (learner_id, period_type, start_text, end_text),
        )
        return ReportDocument(
            id=str(stored["id"] if stored else report_id),
            period_type=period_type,
            period_start=start_text,
            period_end=end_text,
            data=data,
            markdown=markdown,
        )

    def recent(self, learner_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT id, period_type, period_start, period_end, report_json,
                   markdown, generated_at FROM reports
            WHERE learner_id=? ORDER BY generated_at DESC LIMIT ?
            """,
            (learner_id, limit),
        )
        for row in rows:
            row["data"] = json.loads(row.pop("report_json"))
        return rows

    def _mastery_extremes(
        self, learner_id: str, curriculum_id: str, *, descending: bool
    ) -> list[dict[str, Any]]:
        direction = "DESC" if descending else "ASC"
        return self.database.fetch_all(
            f"""
            SELECT c.id concept_id, c.name, c.domain,
                   ROUND(m.mastery_estimate, 4) mastery,
                   ROUND(m.uncertainty, 4) uncertainty, ROUND(m.trend, 4) trend
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=?
            ORDER BY m.mastery_estimate {direction}, m.uncertainty ASC LIMIT 5
            """,  # noqa: S608 - direction is an internal two-value constant
            (learner_id, curriculum_id),
        )


def render_markdown(
    period_type: str, period_start: str, period_end: str, data: dict[str, Any]
) -> str:
    activity = data["study_activity"]
    difficulty = data["difficulty_progression"]
    retention = data["retention"]
    calibration = data["confidence_calibration"]
    usage = data["model_usage"]
    lines = [
        f"# {period_type.title()} Adaptive Tutor report",
        "",
        f"Period: `{period_start}` to `{period_end}`",
        "",
        "## At a glance",
        "",
        f"- Assignments started: **{activity['assignments']}**",
        f"- Submission attempts: **{activity['attempts']}**",
        f"- Planned practice: **{activity['planned_minutes']} minutes**",
        f"- Hints used: **{activity['hints']}**",
        f"- Highest difficulty: **{difficulty['highest']}/10**",
        "",
        "## Readiness by domain",
        "",
        "| Domain | Readiness | Uncertainty |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {item['domain']} | {float(item['readiness']):.0%} | "
        f"{float(item['uncertainty']):.0%} |"
        for item in data["readiness_by_domain"]
    )
    lines.extend(("", "## Mastery movement", ""))
    if data["mastery_movement"]:
        lines.extend(
            f"- **{item['name']}** ({item['domain']}): {float(item['movement']):+.1%} "
            f"from {item['evidence_count']} observation(s)"
            for item in data["mastery_movement"]
        )
    else:
        lines.append("- No mastery evidence was recorded in this period.")
    lines.extend(
        (
            "",
            "## Retention and calibration",
            "",
            f"- Retrieval observations: **{retention['observations']}** "
            f"({retention['successes']} successful, {retention['failures']} failed)",
            f"- Reviews currently due: **{retention['due_reviews']}**",
            f"- Confidence observations: **{calibration['observations']}**",
            f"- Mean absolute calibration error: "
            f"**{float(calibration['mean_absolute_error']):.1%}**",
            f"- Active misconceptions: **{data['active_misconceptions']}**",
            f"- Resolved misconceptions: **{data['resolved_misconceptions']}**",
            "",
            "## Recommended focus",
            "",
        )
    )
    lines.extend(f"- {name}" for name in data["recommended_focus"])
    lines.extend(
        (
            "",
            "## Model usage",
            "",
            f"- Invocations: **{usage['invocations']}**",
            f"- Tokens: **{usage['input_tokens']} input / {usage['output_tokens']} output**",
            f"- Recorded cost: **${float(usage['cost_usd']):.4f}**",
            "",
        )
    )
    return "\n".join(lines)

from dataclasses import dataclass


@dataclass
class ReviewResult:
    status: str
    errors: list[str]
    warnings: list[str]


class ResearchReviewService:
    def review(self, strategy: dict) -> ReviewResult:
        errors: list[str] = []
        warnings: list[str] = []
        required = ("symbol", "timeframe", "entry_rules", "exit_rules", "parameters", "documentation")
        for field in required:
            if not strategy.get(field):
                errors.append(f"MISSING_{field.upper()}")
        if strategy.get("entry_rules") == strategy.get("exit_rules") and strategy.get("entry_rules"):
            errors.append("CONTRADICTORY_IDENTICAL_ENTRY_EXIT")
        documentation = strategy.get("documentation", "")
        if len(documentation.strip()) < 20:
            warnings.append("DOCUMENTATION_TOO_SHORT")
        serialized = str(strategy.get("entry_rules", [])).lower()
        if any(term in serialized for term in ("future", "shift(-", "next_close")):
            errors.append("POTENTIAL_LOOK_AHEAD_BIAS")
        if not strategy.get("dataset_id"):
            warnings.append("DATASET_NOT_LINKED")
        return ReviewResult("PASS" if not errors else "FAIL", errors, warnings)


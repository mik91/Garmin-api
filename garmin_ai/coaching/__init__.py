"""Coaching guardrails (plan validator + readiness gate) and the coaching history.

The method is in docs/coaching-method.md; the numbers are in rules.json next to this file.
"""
from .readiness import assess_readiness
from .validator import load_rules, validate_week

__all__ = ["assess_readiness", "load_rules", "validate_week"]

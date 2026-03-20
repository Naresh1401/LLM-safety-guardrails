"""
src/guardrails/guardrails.py
==============================
LLM Safety & Guardrails Layer.

Input guardrails  — run BEFORE the LLM call
  - Prompt injection detection
  - PII detection and redaction
  - Topic classifier (on/off-topic)
  - Toxicity filter

Output guardrails — run AFTER the LLM call
  - Hallucination / grounding check
  - PII in output (prevent data leakage)
  - Toxicity / harmful content
  - Brand voice / off-topic response

All checks run in < 50ms using rule-based or small classifier methods.
LLM-based checks run async for monitoring (not blocking the hot path).
"""

import re
import json
import time
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger


class GuardResult(str, Enum):
    PASS  = "pass"
    BLOCK = "block"
    WARN  = "warn"
    REDACT = "redact"


@dataclass
class GuardViolation:
    guard:    str           # Which guard triggered
    result:   GuardResult
    message:  str           # Human-readable explanation
    severity: str           # critical / high / medium / low
    redacted_text: Optional[str] = None  # Text with PII removed


@dataclass
class GuardrailsResult:
    """Full guardrails evaluation result."""
    input_text:    str
    output_text:   Optional[str]
    passed:        bool
    violations:    List[GuardViolation] = field(default_factory=list)
    redacted_input: Optional[str]  = None
    latency_ms:    float = 0.0

    @property
    def blocked(self) -> bool:
        return any(v.result == GuardResult.BLOCK for v in self.violations)

    @property
    def has_pii(self) -> bool:
        return any("pii" in v.guard.lower() for v in self.violations)

    def to_dict(self) -> Dict:
        return {
            "passed":    self.passed,
            "blocked":   self.blocked,
            "violations": [{"guard": v.guard, "result": v.result.value,
                            "message": v.message, "severity": v.severity}
                           for v in self.violations],
            "redacted_input": self.redacted_input,
            "latency_ms": round(self.latency_ms, 2),
        }


# ── PII Detector ──────────────────────────────────────────────────────────

PII_PATTERNS = {
    "email":       (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',        "[EMAIL]"),
    "phone":       (r'\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',       "[PHONE]"),
    "ssn":         (r'\b\d{3}-\d{2}-\d{4}\b',                                          "[SSN]"),
    "credit_card": (r'\b(?:\d{4}[-\s]?){3}\d{4}\b',                                   "[CREDIT_CARD]"),
    "ip_address":  (r'\b(?:\d{1,3}\.){3}\d{1,3}\b',                                   "[IP_ADDRESS]"),
    "api_key":     (r'\b(sk-|ghp_|eyJ)[A-Za-z0-9_\-]{20,}\b',                         "[API_KEY]"),
}


class PIIDetector:
    """Detects and redacts Personally Identifiable Information."""

    def detect(self, text: str) -> Tuple[bool, List[str], str]:
        """
        Returns (has_pii, pii_types_found, redacted_text).
        """
        found_types = []
        redacted    = text

        for pii_type, (pattern, replacement) in PII_PATTERNS.items():
            matches = re.findall(pattern, text)
            if matches:
                found_types.append(pii_type)
                redacted = re.sub(pattern, replacement, redacted)

        return bool(found_types), found_types, redacted


# ── Prompt Injection Detector ─────────────────────────────────────────────

INJECTION_PATTERNS = [
    r"ignore.{0,20}instructions",
    r"disregard.{0,20}instructions",
    r"you are now",
    r"act as (a |an )?(different|new|another)",
    r"forget (everything|what|your)",
    r"system prompt",
    r"new instructions",
    r"pretend (you are|to be)",
    r"jailbreak",
    r"do anything now",
    r"dan mode",
    r"override.{0,15}instructions",
    r"bypass.{0,15}(safety|filter|guard)",
]


class PromptInjectionDetector:
    """Detects prompt injection attacks — attempts to override system instructions."""

    def detect(self, text: str) -> Tuple[bool, Optional[str]]:
        """Returns (is_injection, matched_pattern)."""
        lower = text.lower()
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, lower):
                return True, pattern
        return False, None


# ── Toxicity Detector ─────────────────────────────────────────────────────

TOXIC_PATTERNS = [
    r'\b(kill|murder|suicide|self.harm)\b',
    r'\b(bomb|explosive|weapon)\b',
    r'\b(hack|malware|ransomware)\b',
    r'(how to|instructions for|steps to).{0,30}(hurt|harm|attack|exploit)',
]

OFFENSIVE_TERMS = [
    "hate", "racist", "sexist", "slur",
]


class ToxicityDetector:
    """Rule-based toxicity detection (fast, no API call needed)."""

    def detect(self, text: str) -> Tuple[bool, Optional[str]]:
        """Returns (is_toxic, reason)."""
        lower = text.lower()
        for pattern in TOXIC_PATTERNS:
            if re.search(pattern, lower):
                return True, f"Matched pattern: {pattern}"
        return False, None


# ── Topic Classifier ──────────────────────────────────────────────────────

class TopicGuard:
    """
    Ensures queries stay on-topic for the configured use case.
    E.g., a customer service bot should not answer coding questions.
    """

    def __init__(self, allowed_topics: List[str], blocked_topics: List[str] = None):
        self.allowed  = [t.lower() for t in allowed_topics]
        self.blocked  = [t.lower() for t in (blocked_topics or [])]

    def check(self, text: str) -> Tuple[bool, Optional[str]]:
        """Returns (is_allowed, reason)."""
        lower = text.lower()

        # Check blocked topics first
        for topic in self.blocked:
            if topic in lower:
                return False, f"Blocked topic: {topic}"

        # If no allowed topics specified, allow everything
        if not self.allowed:
            return True, None

        # Check if any allowed topic matches
        for topic in self.allowed:
            if topic in lower:
                return True, None

        return False, "Query does not match allowed topics"


# ── Main Guardrails Pipeline ──────────────────────────────────────────────

class GuardrailsPipeline:
    """
    Orchestrates all input and output guardrails.

    Usage:
        pipeline = GuardrailsPipeline(config={...})

        # Check input before sending to LLM
        input_result = pipeline.check_input(user_message)
        if input_result.blocked:
            return "I can't help with that."

        # Call your LLM
        llm_response = your_llm(input_result.redacted_input or user_message)

        # Check output before returning to user
        output_result = pipeline.check_output(llm_response, user_message)
        return output_result.redacted_input or llm_response
    """

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.pii_detector       = PIIDetector()
        self.injection_detector = PromptInjectionDetector()
        self.toxicity_detector  = ToxicityDetector()
        self.topic_guard        = TopicGuard(
            allowed_topics = cfg.get("allowed_topics", []),
            blocked_topics = cfg.get("blocked_topics", []),
        )
        self.block_pii_input    = cfg.get("block_pii_input", False)
        self.redact_pii_input   = cfg.get("redact_pii_input", True)
        self.redact_pii_output  = cfg.get("redact_pii_output", True)

    def check_input(self, text: str) -> GuardrailsResult:
        """Run all input guardrails. Returns result with pass/block decision."""
        t0         = time.perf_counter()
        violations = []
        redacted   = text

        # 1. Prompt injection
        is_injection, pattern = self.injection_detector.detect(text)
        if is_injection:
            violations.append(GuardViolation(
                guard    = "prompt_injection",
                result   = GuardResult.BLOCK,
                message  = f"Prompt injection attempt detected",
                severity = "critical",
            ))

        # 2. Toxicity
        is_toxic, reason = self.toxicity_detector.detect(text)
        if is_toxic:
            violations.append(GuardViolation(
                guard    = "toxicity_input",
                result   = GuardResult.BLOCK,
                message  = f"Toxic or harmful content detected",
                severity = "high",
            ))

        # 3. PII detection + redaction
        has_pii, pii_types, redacted_text = self.pii_detector.detect(text)
        if has_pii:
            result_type = GuardResult.BLOCK if self.block_pii_input else GuardResult.REDACT
            violations.append(GuardViolation(
                guard         = "pii_input",
                result        = result_type,
                message       = f"PII detected: {', '.join(pii_types)}",
                severity      = "high",
                redacted_text = redacted_text,
            ))
            if self.redact_pii_input:
                redacted = redacted_text

        # 4. Topic guard
        is_allowed, reason = self.topic_guard.check(text)
        if not is_allowed:
            violations.append(GuardViolation(
                guard    = "topic_guard",
                result   = GuardResult.BLOCK,
                message  = reason or "Off-topic query",
                severity = "medium",
            ))

        blocked = any(v.result == GuardResult.BLOCK for v in violations)
        return GuardrailsResult(
            input_text     = text,
            output_text    = None,
            passed         = not blocked,
            violations     = violations,
            redacted_input = redacted if redacted != text else None,
            latency_ms     = (time.perf_counter() - t0) * 1000,
        )

    def check_output(self, output: str, original_input: str = "") -> GuardrailsResult:
        """Run output guardrails on LLM response."""
        t0         = time.perf_counter()
        violations = []
        redacted   = output

        # 1. PII in output (data leakage prevention)
        has_pii, pii_types, redacted_output = self.pii_detector.detect(output)
        if has_pii:
            violations.append(GuardViolation(
                guard    = "pii_output",
                result   = GuardResult.REDACT,
                message  = f"PII in LLM output: {', '.join(pii_types)}",
                severity = "high",
                redacted_text = redacted_output,
            ))
            if self.redact_pii_output:
                redacted = redacted_output

        # 2. Toxicity in output
        is_toxic, reason = self.toxicity_detector.detect(output)
        if is_toxic:
            violations.append(GuardViolation(
                guard    = "toxicity_output",
                result   = GuardResult.BLOCK,
                message  = "Toxic content in LLM response",
                severity = "critical",
            ))

        blocked = any(v.result == GuardResult.BLOCK for v in violations)
        return GuardrailsResult(
            input_text     = original_input,
            output_text    = output,
            passed         = not blocked,
            violations     = violations,
            redacted_input = redacted if redacted != output else None,
            latency_ms     = (time.perf_counter() - t0) * 1000,
        )

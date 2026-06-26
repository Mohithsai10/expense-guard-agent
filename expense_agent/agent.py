# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ambient agent that processes expense report emails.

This agent receives expense events via ADK trigger endpoints (Pub/Sub)
and routes them through a graph-based workflow:

- Expenses under $100 are auto-approved immediately.
- Expenses of $100 or more are flagged by an LLM review agent, then
  paused for human approval via ADK 2.0's RequestInput (HITL).

Built with ADK 2.0 graph-based workflows to demonstrate conditional
routing, function nodes, mixed agent/function graphs, and human-in-the-
loop approval.
"""

import base64
import json
import re

from google.adk.workflow import Workflow
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from pydantic import BaseModel, Field

from .config import config


# ---------------------------------------------------------------------------
# Pydantic schemas for structured data flow between nodes
# ---------------------------------------------------------------------------


class ExpenseData(BaseModel):
    """Expense report data extracted from the incoming email event."""

    amount: float = Field(description="Expense amount in USD")
    submitter: str = Field(description="Email of the person who submitted")
    category: str = Field(description="Expense category, e.g. travel, meals")
    description: str = Field(description="What the expense is for")
    date: str = Field(description="Date of the expense (YYYY-MM-DD)")


# ---------------------------------------------------------------------------
# Security Checkpoint helpers and node
# ---------------------------------------------------------------------------


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Redact SSNs and Credit Cards from the text description."""
    redacted_categories = []
    # SSN pattern
    ssn_pattern = re.compile(r'\b\d{3}[- ]\d{2}[- ]\d{4}\b')
    if ssn_pattern.search(text):
        redacted_categories.append("SSN")
        text = ssn_pattern.sub("[REDACTED SSN]", text)
        
    # Credit Card pattern
    cc_pattern = re.compile(r'\b(?:\d[ -]*?){13,16}\b')
    if cc_pattern.search(text):
        redacted_categories.append("CREDIT_CARD")
        text = cc_pattern.sub("[REDACTED CREDIT CARD]", text)
        
    return text, redacted_categories


def check_prompt_injection(text: str) -> bool:
    """Heuristic check for common prompt injection patterns."""
    injection_keywords = [
        "ignore previous",
        "system prompt",
        "override",
        "bypass",
        "auto-approve",
        "forget the rules",
        "you are now",
        "new instruction",
        "do not review",
        "ignore instructions"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in injection_keywords)


def security_checkpoint(node_input: dict, ctx: Context) -> Event:
    """Validate expense against PII leakage and prompt injection attempts."""
    description = node_input.get("description", "")
    
    # 1. Scrub PII
    clean_description, redacted = scrub_pii(description)
    if redacted:
        ctx.state["redacted_categories"] = redacted
        
    # Update description so that PII is removed from future nodes (LLM reviewer / logs / human approval)
    node_input["description"] = clean_description
    ctx.state["expense_data"] = node_input
    
    # 2. Check prompt injection
    if check_prompt_injection(description):
        log_entry = {
            "severity": "CRITICAL",
            "message": f"Security alert: Prompt injection attempt detected in expense description from {node_input.get('submitter', 'unknown')}",
            "alert_type": "security_checkpoint_violation",
            "submitter": node_input.get("submitter", "unknown"),
            "amount": node_input.get("amount", 0),
        }
        print(json.dumps(log_entry), flush=True)
        
        # Flag as security event to skip the LLM reviewer and route directly to human approval
        node_input["security_event"] = True
        node_input["security_reason"] = "Prompt injection attempt detected"
        ctx.state["expense_data"] = node_input
        return Event(route="FLAGGED_SECURITY", output=node_input)
        
    return Event(route="CLEAN", output=node_input)


# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def parse_expense_email(node_input: str) -> Event:
    """Parse a Pub/Sub trigger event and extract expense data.

    The trigger endpoint delivers the raw Pub/Sub message JSON. The
    expense payload lives in the ``data`` field, which may be
    base64-encoded (real Pub/Sub) or plain JSON (local testing).
    """
    try:
        event = json.loads(node_input)
    except json.JSONDecodeError:
        return Event(output={"error": f"Invalid JSON: {node_input[:200]}"})

    data = event.get("data", {})

    if isinstance(data, str):
        try:
            data = json.loads(base64.b64decode(data))
        except Exception:
            return Event(output={"error": f"Failed to decode data: {data[:200]}"})

    return Event(
        output={
            "amount": float(data.get("amount", 0)),
            "submitter": data.get("submitter", "unknown"),
            "category": data.get("category", "other"),
            "description": data.get("description", ""),
            "date": data.get("date", ""),
        }
    )


def route_by_amount(node_input: dict, ctx: Context) -> Event:
    """Route expenses based on the $100 threshold.

    Returns a routing event that the workflow uses to pick the next
    node: ``AUTO_APPROVE`` for amounts under $100, ``NEEDS_REVIEW``
    for $100 and above.

    Also stores the expense data in workflow state so the HITL
    approval node can include it in the RequestInput payload.
    """
    ctx.state["expense_data"] = node_input
    amount = node_input.get("amount", 0)
    if amount >= config.review_threshold:
        return Event(route="NEEDS_REVIEW", output=node_input)
    return Event(route="AUTO_APPROVE", output=node_input)


def auto_approve(node_input: dict) -> Event:
    """Auto-approve a low-value expense and log the decision."""
    log_entry = {
        "severity": "INFO",
        "message": (
            f"Expense auto-approved: ${node_input['amount']:.2f}"
            f" from {node_input['submitter']}"
        ),
        "decision": "approved",
        "amount": node_input["amount"],
        "submitter": node_input["submitter"],
        "category": node_input["category"],
    }
    print(json.dumps(log_entry), flush=True)
    return Event(output={"status": "approved", **node_input})


# ---------------------------------------------------------------------------
# LLM review agent (invoked only for expenses >= $100)
# ---------------------------------------------------------------------------


def emit_expense_alert(
    submitter: str,
    amount: float,
    category: str,
    risk_summary: str,
) -> dict:
    """Emit a structured log alerting finance to review a high-value expense.

    Cloud Run captures JSON stdout as structured logs in Cloud Logging.
    A log-based metric and alert policy trigger email notifications
    when these logs appear.

    Args:
        submitter: Who submitted the expense.
        amount: The expense amount in USD.
        category: The expense category.
        risk_summary: Why this expense needs review.

    Returns:
        Confirmation that the alert was emitted.
    """
    log_entry = {
        "severity": "WARNING",
        "message": (
            f"Expense review alert: ${amount:.2f} from {submitter} — {risk_summary}"
        ),
        "alert_type": "expense_review",
        "submitter": submitter,
        "amount": amount,
        "category": category,
        "risk_summary": risk_summary,
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "alert_emitted", "submitter": submitter, "amount": amount}


review_agent = LlmAgent(
    name="review_agent",
    model=config.model,
    mode="single_turn",
    instruction="""You are an expense review agent. You receive expense reports
of $100 or more that need review before approval.

Analyze the expense and:
1. Check for risk factors: unusual category for the amount, vague description,
   suspiciously round numbers, very high value (>$1000), or potential policy
   violations.
2. Call the `emit_expense_alert` tool with the submitter, amount, category,
   and a brief risk summary explaining why this expense needs human review.
3. Return a structured review.

Your review MUST include:
- **Amount**: The expense amount
- **Submitter**: Who submitted it
- **Category**: The expense category
- **Risk level**: low, medium, or high
- **Risk factors**: What flags you found (if any)
- **Recommendation**: approve, request-more-info, or escalate""",
    input_schema=ExpenseData,
    tools=[emit_expense_alert],
)


# ---------------------------------------------------------------------------
# HITL: pause the workflow for human approval
# ---------------------------------------------------------------------------


def request_approval(node_input, ctx: Context):  # type: ignore[no-untyped-def]
    """Pause the workflow and wait for a human to approve or reject.

    Yields a ``RequestInput`` that the ADK runtime surfaces to the UI.
    The workflow stays paused until someone resumes the session (via the
    approval UI or ``POST /run``). The human's response becomes the
    output of this node and flows into ``process_decision``.
    """
    expense = ctx.state.get("expense_data", {})
    yield RequestInput(
        message="Expense requires manager approval. Approve or reject.",
        payload=expense,
    )


def process_decision(node_input, ctx: Context) -> Event:  # type: ignore[no-untyped-def]
    """Process the human's approval decision and log the outcome."""
    # node_input is the response from RequestInput — the approval UI
    # sends {"decision": "approve"} or {"decision": "reject"}.
    decision = "unknown"
    if isinstance(node_input, dict):
        decision = node_input.get("decision", "unknown")
    elif isinstance(node_input, str):
        decision = "approve" if "approve" in node_input.lower() else "reject"

    approved = decision == "approve"
    expense = ctx.state.get("expense_data", {})
    status = "approved" if approved else "rejected"

    log_entry = {
        "severity": "INFO" if approved else "WARNING",
        "message": f"Expense {status} by manager",
        "decision": status,
    }
    print(json.dumps(log_entry), flush=True)

    submitter = expense.get("submitter", "unknown")
    amount = expense.get("amount", 0)
    category = expense.get("category", "")
    description = expense.get("description", "")
    date = expense.get("date", "")

    parts = [f"${amount:.2f} expense from {submitter} has been {status}."]
    if description:
        parts.append(f'"{description}" ({category}) on {date}.')
    if approved:
        parts.append(
            "The expense has been logged and will be processed for reimbursement."
        )
    else:
        parts.append(
            "The submitter will be notified and may resubmit with additional documentation."
        )

    return Event(output={"status": status, "message": " ".join(parts)})


# ---------------------------------------------------------------------------
# Graph-based workflow — the root agent
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="expense_processor",
    edges=[
        ("START", parse_expense_email, route_by_amount),
        (
            route_by_amount,
            {
                "AUTO_APPROVE": auto_approve,
                "NEEDS_REVIEW": security_checkpoint,
            },
        ),
        (
            security_checkpoint,
            {
                "CLEAN": review_agent,
                "FLAGGED_SECURITY": request_approval,
            },
        ),
        (review_agent, request_approval, process_decision),
    ],
)

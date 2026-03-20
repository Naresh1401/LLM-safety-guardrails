"""src/api/main.py"""
import os, time
from typing import Optional, Dict, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from src.guardrails.guardrails import GuardrailsPipeline

app = FastAPI(title="LLM Safety & Guardrails", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_stats = {"total_checked":0,"blocked":0,"pii_redacted":0,"injections_caught":0}

def get_pipeline():
    return GuardrailsPipeline(config={
        "redact_pii_input":  True,
        "redact_pii_output": True,
        "block_pii_input":   False,
        "allowed_topics":    [],
        "blocked_topics":    [],
    })

class InputCheckRequest(BaseModel):
    text: str
    config: Optional[Dict[str, Any]] = None

class OutputCheckRequest(BaseModel):
    output: str
    original_input: str = ""

@app.get("/health")
def health(): return {"status":"ok","timestamp":time.time()}

@app.get("/stats")
def stats(): return _stats

@app.post("/check/input")
def check_input(request: InputCheckRequest):
    cfg = GuardrailsPipeline(config=request.config or {})
    r   = cfg.check_input(request.text)
    _stats["total_checked"] += 1
    if r.blocked: _stats["blocked"] += 1
    if r.has_pii: _stats["pii_redacted"] += 1
    if any(v.guard=="prompt_injection" for v in r.violations): _stats["injections_caught"] += 1
    return r.to_dict()

@app.post("/check/output")
def check_output(request: OutputCheckRequest):
    pipeline = get_pipeline()
    r = pipeline.check_output(request.output, request.original_input)
    _stats["total_checked"] += 1
    if r.blocked: _stats["blocked"] += 1
    return r.to_dict()

@app.post("/redteam")
def run_redteam():
    from src.redteam.red_team import RedTeamRunner
    pipeline = get_pipeline()
    runner   = RedTeamRunner(pipeline)
    results  = runner.run()
    total    = len(results)
    passed   = sum(1 for r in results if r.passed)
    return {
        "passed": passed, "total": total, "pass_rate": passed/total,
        "results": [{"category":r.case.category,"description":r.case.description,
                     "expected_blocked":r.case.should_block,"was_blocked":r.blocked,
                     "passed":r.passed} for r in results]
    }
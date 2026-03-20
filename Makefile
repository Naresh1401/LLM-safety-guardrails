.PHONY: install run test redteam

install:
	pip install -r requirements.txt

run:
	uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

test:
	python tests/test_guardrails.py

redteam:
	curl -s -X POST http://localhost:8000/redteam | python3 -m json.tool

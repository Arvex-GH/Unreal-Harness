.PHONY: install dev test run doctor bridge demo clean

install:
	pip install -e .

dev:
	pip install -e . && python -m harness serve

test:
	python tests/test_pipeline.py && python tests/test_agent.py

run:
	python -m harness serve

doctor:
	python -m harness doctor

bridge:
	python -m harness install-bridge

demo:            ## run the full stack with mock editor + mock LM Studio (no UE, no model)
	python -m tests.mock_ue --port 34567 & \
	python tests/mock_lmstudio.py 1234 & \
	python -m harness serve

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; \
	rm -rf build dist *.egg-info .pytest_cache

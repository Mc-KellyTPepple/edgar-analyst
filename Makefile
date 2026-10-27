.PHONY: test ingest db questions

test:
	python -m unittest discover -s tests -t . -v

# Needs SEC_USER_AGENT, e.g. export SEC_USER_AGENT="Your Name you@example.com"
ingest:
	PYTHONPATH=src python -m edgar_analyst ingest

db:
	PYTHONPATH=src python -m edgar_analyst build-db

questions:
	PYTHONPATH=src python -m edgar_analyst gen-questions

PY      := .venv/bin/python
UVICORN := .venv/bin/uvicorn
SINK    := http://127.0.0.1:9009/hook

.PHONY: install seed run demo usage revoke clean

install: .venv/.installed

.venv/.installed: requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	touch $@

seed: install          ## reset the database and issue fresh demo keys
	$(PY) seed.py

run: install           ## start the gateway on :8000 (set WEBHOOK_URL for Slack/Discord alerts)
	$(UVICORN) gateway.app:app --port 8000

demo: install          ## fresh state, start the gateway, run the scripted scenes, stop
	@$(PY) seed.py > /dev/null
	@WEBHOOK_URL=$${WEBHOOK_URL:-$(SINK)} $(UVICORN) gateway.app:app --port 8000 --log-level warning & \
	  pid=$$!; trap "kill $$pid 2>/dev/null" EXIT; \
	  $(PY) demo.py

usage:                 ## spend report from a running gateway
	@$(PY) cli.py usage

revoke:                ## make revoke EMAIL=bob@acme.com
	@$(PY) cli.py revoke $(EMAIL)

clean:
	rm -rf gateway.db audit.jsonl .demo_keys.json __pycache__ gateway/__pycache__

#!/usr/bin/env bash
# Run the kerb rules Weave evaluation. Loads WANDB_* from /etc/environment (values are never printed).
set -e
set -a; . /etc/environment; set +a
cd ~/weave_eval
for k in WANDB_API_KEY WANDB_TEAM WANDB_PROJECT; do [ -n "${!k}" ] || { echo "$k not set"; exit 1; }; done
~/venvs/kerbquery/bin/python -c "import weave" 2>/dev/null || ~/venvs/kerbquery/bin/pip install -q weave
~/venvs/kerbquery/bin/python kerb_rules_eval.py "$@" 2>&1 | grep -v "Task failed: ValueError: Invalid project_id" | grep -E "wrote|done|DRY|https?://|rror|Traceback" || true
echo "Project: https://wandb.ai/${WANDB_TEAM}/${WANDB_PROJECT}/weave/evaluations"

#!/bin/bash
set -e

for i in 1 2 3 4 5; do
    echo "========================================="
    echo "       ADVERSARIAL STRESS RUN $i / 5"
    echo "========================================="
    docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /workspace/development/frappe-bench/apps/ism/scripts/empirical_adversarial_m1_it3.py
done

echo ""
echo "ALL 5 ADVERSARIAL STRESS RUNS COMPLETED SUCCESSFULLY WITH 0 FAILURES!"

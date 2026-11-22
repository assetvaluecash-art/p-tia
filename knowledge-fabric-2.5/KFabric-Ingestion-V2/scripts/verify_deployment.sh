#!/bin/bash
# Run on SERVER after rsync to verify all Release 20260319 changes are deployed.
# Usage: ./scripts/verify_deployment.sh

set -e
cd "$(dirname "$0")/.."

FAIL=0

check() {
  local file="$1"
  local pattern="$2"
  local desc="$3"
  if grep -q "$pattern" "$file" 2>/dev/null; then
    echo "  [OK] $desc"
  else
    echo "  [MISS] $desc ($file)"
    FAIL=1
  fi
}

echo "Verifying Release 20260319 deployment..."
echo ""

check scripts/validate_release_ready.py "nodes < 890" "Neo4j gate 890"
check scripts/validate_release_ready.py "890 allows" "Neo4j gate comment"
check scripts/_check_spine.py "find_latest_report" "_check_spine uses latest report"
check kbfabric_ingest/reports/ingestion_report.py '"spine_node": block.spine_node' "Report includes spine_node in blocks"
check kbfabric_ingest/ingest/kg_writer.py "_get_spine_entity_ids" "kg_writer spine resolution"
check kbfabric_ingest/ingest/ontology_prompt.py "SPINE HIERARCHY" "Ontology prompt spine hierarchy"
check kbfabric_ingest/llm/prompts.py "spine_node: (REQUIRED)" "LLM prompts spine_node"
check kbfabric_ingest/ingest/ie_client.py "spine_node" "ie_client spine_node"
check config/enterprise_banking_ontology.yaml "spine_node:" "Ontology few-shot spine_node"

echo ""
if [ $FAIL -eq 0 ]; then
  echo "*** ALL CHECKS PASSED ***"
  exit 0
else
  echo "*** SOME CHECKS FAILED - re-run rsync from local ***"
  exit 1
fi

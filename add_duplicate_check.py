import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('N8N_API_KEY')
WORKFLOW_ID = '63F2NuQ8tqexEHEU'
headers = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}

# Get current workflow
resp = requests.get(f'http://localhost:5678/api/v1/workflows/{WORKFLOW_ID}', headers=headers)
wf = resp.json()

# New nodes to add

# 1. Check Duplicate - Query Airtable for existing record with same tag + event_slug
check_duplicate_node = {
    "parameters": {
        "method": "GET",
        "url": "=https://api.airtable.com/v0/{{ $env.AIRTABLE_BASE_ID }}/active_event_data",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $env.AIRTABLE_API_KEY }}"}
            ]
        },
        "sendQuery": True,
        "queryParameters": {
            "parameters": [
                {"name": "filterByFormula", "value": "=AND({tag}='{{ $json.tag }}', {event_slug}='{{ $json.slug }}')"},
                {"name": "maxRecords", "value": "1"}
            ]
        },
        "options": {}
    },
    "id": "check-duplicate",
    "name": "Check Duplicate",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [650, 300]
}

# 2. IF node - Check if duplicate was found
if_duplicate_node = {
    "parameters": {
        "conditions": {
            "options": {
                "caseSensitive": True,
                "leftValue": "",
                "typeValidation": "strict"
            },
            "conditions": [
                {
                    "id": "duplicate-check",
                    "leftValue": "={{ $json.records.length }}",
                    "rightValue": 0,
                    "operator": {
                        "type": "number",
                        "operation": "gt"
                    }
                }
            ],
            "combinator": "and"
        },
        "options": {}
    },
    "id": "if-duplicate",
    "name": "IF Duplicate",
    "type": "n8n-nodes-base.if",
    "typeVersion": 2,
    "position": [870, 300]
}

# 3. Return Already Checked In - for duplicates
already_checked_in_node = {
    "parameters": {
        "jsCode": """// Player already checked in - return their current status
const existing = $json.records[0].fields;
const ctx = $node["Parse Input"].json;

return [{
  json: {
    already_checked_in: true,
    status: existing.status || "Pending",
    payment_valid: existing.payment_valid || false,
    member: existing.member || false,
    startgg: existing.startgg || false,
    name: existing.name || ctx.name,
    tag: existing.tag || ctx.tag,
    message: "Already checked in"
  }
}];"""
    },
    "id": "already-checked-in",
    "name": "Already Checked In",
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [1090, 180]
}

# Update positions of existing nodes to make room
for node in wf['nodes']:
    if node['name'] == 'Parse Input':
        node['position'] = [430, 300]
    elif node['name'] == 'eBas Check':
        node['position'] = [1090, 420]  # Move right and down (false branch)
    elif node['name'] == 'Start.gg Check':
        node['position'] = [1090, 560]  # Move right and down
    elif node['name'] == 'Wait for Both':
        node['position'] = [1310, 490]
    elif node['name'] == 'Merge Results':
        node['position'] = [1530, 490]
    elif node['name'] == 'Save to Airtable':
        node['position'] = [1750, 490]
    elif node['name'] == 'Return Results':
        node['position'] = [1970, 340]  # Center between both paths

# Add new nodes
wf['nodes'].append(check_duplicate_node)
wf['nodes'].append(if_duplicate_node)
wf['nodes'].append(already_checked_in_node)

# Update connections
wf['connections'] = {
    "Webhook": {
        "main": [[{"node": "Load Settings", "type": "main", "index": 0}]]
    },
    "Load Settings": {
        "main": [[{"node": "Parse Input", "type": "main", "index": 0}]]
    },
    "Parse Input": {
        "main": [[{"node": "Check Duplicate", "type": "main", "index": 0}]]
    },
    "Check Duplicate": {
        "main": [[{"node": "IF Duplicate", "type": "main", "index": 0}]]
    },
    "IF Duplicate": {
        "main": [
            # True branch (duplicate found) -> Already Checked In
            [{"node": "Already Checked In", "type": "main", "index": 0}],
            # False branch (no duplicate) -> continue with checks
            [{"node": "eBas Check", "type": "main", "index": 0}, {"node": "Start.gg Check", "type": "main", "index": 0}]
        ]
    },
    "eBas Check": {
        "main": [[{"node": "Wait for Both", "type": "main", "index": 0}]]
    },
    "Start.gg Check": {
        "main": [[{"node": "Wait for Both", "type": "main", "index": 1}]]
    },
    "Wait for Both": {
        "main": [[{"node": "Merge Results", "type": "main", "index": 0}]]
    },
    "Merge Results": {
        "main": [[{"node": "Save to Airtable", "type": "main", "index": 0}]]
    },
    "Save to Airtable": {
        "main": [[{"node": "Return Results", "type": "main", "index": 0}]]
    },
    "Already Checked In": {
        "main": [[{"node": "Return Results", "type": "main", "index": 0}]]
    }
}

# Create update payload
update_payload = {
    'name': wf['name'],
    'nodes': wf['nodes'],
    'connections': wf['connections'],
    'settings': wf.get('settings', {})
}

# Push update
resp = requests.put(
    f'http://localhost:5678/api/v1/workflows/{WORKFLOW_ID}',
    headers=headers,
    json=update_payload
)

if resp.status_code == 200:
    print("SUCCESS! Duplicate check added to workflow.")
    print("\nNew flow:")
    print("  Webhook -> Load Settings -> Parse Input -> Check Duplicate -> IF Duplicate")
    print("    -> TRUE: Already Checked In -> Return Results")
    print("    -> FALSE: eBas Check + Start.gg Check -> Wait -> Merge -> Save -> Return")
else:
    print(f"ERROR: {resp.status_code}")
    print(resp.text)

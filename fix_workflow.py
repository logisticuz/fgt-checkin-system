import requests
import json

API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI5MDFhZjhiMy00YzZjLTRlMDktYjI0OC0yNDQxYjBhZGJhNGYiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwiaWF0IjoxNzY1ODk0MDE4LCJleHAiOjE3NzEwNDUyMDB9.fPZMG10WadOgW1hBtxBE5Wq_HLv5BDFVyuORZfgks_A"
WORKFLOW_ID = "63F2NuQ8tqexEHEU"

headers = {"X-N8N-API-KEY": API_KEY, "Content-Type": "application/json"}

# Get current workflow
resp = requests.get(f"http://localhost:5678/api/v1/workflows/{WORKFLOW_ID}", headers=headers)
wf = resp.json()

# New Merge Results code:
# - UUID generation
# - Status always "Pending" (TO must approve payment)
# - payment_valid always false initially
merge_results_code = '''const ctx = $node["Parse Input"].json;
const ebas = $node["eBas Check"]?.json || { isMember: false };
const sgg = $node["Start.gg Check"]?.json || { isRegistered: false };

// Generate UUID
const uuid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
  const r = Math.random() * 16 | 0;
  const v = c === 'x' ? r : (r & 0x3 | 0x8);
  return v.toString(16);
});

const memberOk = ebas.isMember === true || !ctx._runEbas;
const startggOk = sgg.isRegistered === true || !ctx._runStartgg;

// Build missing array for display
const missing = [];
if (!memberOk) missing.push("Membership");
if (!startggOk) missing.push("Start.gg");

// Status is always Pending - TO must verify payment manually
// payment_valid is always false - TO sets it to true after verifying
return [{ json: {
  uuid,
  status: "Pending",
  payment_valid: false,
  missing,
  member: memberOk,
  startgg: startggOk,
  name: ctx.name,
  tag: ctx.tag,
  slug: ctx.slug,
  telefon: ctx.telefon,
  email: ctx.email
}}];'''

# New Save to Airtable columns - add UUID and payment_valid
save_columns = {
    'mappingMode': 'defineBelow',
    'value': {
        'UUID': '={{ $json.uuid }}',
        'name': '={{ $json.name }}',
        'tag': '={{ $json.tag }}',
        'telephone': '={{ $json.telefon }}',
        'email': '={{ $json.email }}',
        'member': '={{ $json.member }}',
        'startgg': '={{ $json.startgg }}',
        'payment_valid': '={{ $json.payment_valid }}',
        'status': '={{ $json.status }}',
        'event_slug': '={{ $json.slug }}'
    }
}

# Update nodes
for node in wf['nodes']:
    if node['name'] == 'Merge Results':
        node['parameters']['jsCode'] = merge_results_code
        print("Updated Merge Results")

    if node['name'] == 'Save to Airtable':
        node['parameters']['columns'] = save_columns
        print("Updated Save to Airtable")

# Create update payload
update_payload = {
    'name': wf['name'],
    'nodes': wf['nodes'],
    'connections': wf['connections'],
    'settings': wf.get('settings', {})
}

# Push update
resp = requests.put(
    f"http://localhost:5678/api/v1/workflows/{WORKFLOW_ID}",
    headers=headers,
    json=update_payload
)

if resp.status_code == 200:
    print("SUCCESS!")
else:
    print(f"ERROR: {resp.status_code}")
    print(resp.text)

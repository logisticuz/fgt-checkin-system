"""T-074: isolated, backed-up FightBox rehearsal against DEV's real nginx/n8n path."""

import argparse
import copy
import json
import random
import re
import socket
import statistics
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sim.ebas_http_sim import DUMMY_KEY, GAMES, person_tags, synthetic_people

HUB = Path(r"C:\Users\vikto\agent-hub")
PROJECT = "fgt-dev"
NETWORK = "fgt-dev_fgt-net"
BACKEND = "fgt-dev-backend-1"
N8N = "fgt-dev-n8n-1"
POSTGRES = "fgt-dev-postgres-1"
NGINX = "fgt-dev-nginx-1"
SSE_TOKEN = "synthetic-t074-sse"
SIM_HOSTS = {"t074-ebas-sim", "t074-startgg-sim"}
PNR_PATTERN = re.compile(r"[0-9]{10,}|[0-9]{6,8}[-+][0-9]{4}")


class RehearsalError(RuntimeError):
    pass


def guard_target(target):
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        raise RehearsalError("Invalid DEV target") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port != 8088
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RehearsalError("Only http://localhost:8088 or http://127.0.0.1:8088 is allowed")
    return target.rstrip("/")


def guard_environment(backend, n8n):
    if (
        backend.get("EBAS_TRANSPORT") != "stub"
        or not backend.get("EBAS_STUB_SCENARIO")
        or backend.get("INTEGRATION_ENGINE") != "n8n"
        or backend.get("MEMBERSHIP_FLOW") != "legacy"
        or n8n.get("EBAS_API_KEY") != DUMMY_KEY
        or n8n.get("EBAS_ASSOC") != "sim"
        or any(value != DUMMY_KEY for key, value in n8n.items() if key.startswith("STARTGG_"))
    ):
        raise RehearsalError("Rehearsal requires stub eBas, legacy+n8n, and dummy provider keys")
    for environment in (backend, n8n):
        if environment.get("IS_PROD", "").lower() in {"1", "true", "yes", "on"} or any(
            environment.get(key, "").lower() in {"prod", "production"} for key in ("APP_ENV", "ENV")
        ):
            raise RehearsalError("Production is refused")


def guard_person(pnr, people):
    if pnr not in people:
        raise RehearsalError("Only the scenario's synthetic identities are permitted")


def guard_scenario(path):
    source = (ROOT / "scripts" / "sim" / "ebas_fightbox.json").resolve()
    if Path(path).resolve() != source:
        raise RehearsalError("Only the shipped FightBox synthetic scenario is permitted")
    settings = json.loads(source.read_text(encoding="utf-8"))
    synthetic_people(settings)
    return settings


def guard_report(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"pnr|personnummer|socialsecurity|fingerprint|hash|token", str(key), re.I):
                raise RehearsalError("Report refused: identity, token or fingerprint field")
            guard_report(item)
    elif isinstance(value, list):
        for item in value:
            guard_report(item)
    elif isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32,64}", value):
        raise RehearsalError("Report refused: opaque identity digest")
    if PNR_PATTERN.search(json.dumps(value, ensure_ascii=False, default=str)):
        raise RehearsalError("Report refused: personal-ID-shaped data")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def guard_flows(flows):
    if not flows:
        raise RehearsalError("No active rehearsal flows")
    webhooks = set()
    for flow in flows:
        if not flow.get("id", "").startswith("T074"):
            raise RehearsalError("An original or unrelated workflow remains active")
        for node in flow.get("nodes", []):
            parameters = node.get("parameters", {})
            if node.get("credentials"):
                raise RehearsalError("Rehearsal nodes must not use stored credentials")
            if node.get("type") == "n8n-nodes-base.webhook":
                webhooks.add(parameters.get("path"))
            for value in _strings(parameters):
                if re.search(r"(?:ebas\.sverok\.se|api\.start\.gg)", value, re.I):
                    raise RehearsalError("An active workflow still points to a real provider")
            if node.get("type") == "n8n-nodes-base.httpRequest":
                url = str(parameters.get("url", "")).lstrip("=")
                host = urlsplit(url).hostname
                if host not in {"backend", "n8n", *SIM_HOSTS}:
                    raise RehearsalError("An active HTTP node has an unapproved destination")
    if not {"checkin/validate-v5", "ebas/register-v2"} <= webhooks:
        raise RehearsalError("Required real-product webhooks are missing")


def rehearsal_flows(originals):
    sources = [flow for flow in originals if flow.get("name", "").startswith("FGC THN -")]
    identifiers = {flow["id"]: "T074" + uuid.uuid4().hex[:12] for flow in sources}

    def rewrite(value):
        if isinstance(value, str):
            value = value.replace("https://ebas.sverok.se", "http://t074-ebas-sim:8080")
            value = value.replace("https://api.start.gg", "http://t074-startgg-sim:8080")
            for old, new in identifiers.items():
                value = value.replace(old, new)
            return value
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    result = []
    for original in sources:
        flow = rewrite(copy.deepcopy(original))
        flow.update(id=identifiers[original["id"]], name="T074 " + original["name"], active=False)
        flow["versionId"] = str(uuid.uuid4())
        flow.pop("activeVersionId", None)
        flow["pinData"] = {}
        result.append(flow)
    guard_flows(result)
    return result


def rate_policy(source):
    zones = dict(re.findall(r"limit_req_zone\s+\S+\s+zone=(\w+):\S+\s+rate=([^;]+);", source))
    policy = {}
    for match in re.finditer(r"location\s+([^\{]+)\{([^}]+)\}", source):
        location, body = match.groups()
        if not any(
            part in location
            for part in (
                "/api/checkin",
                "/api/participant",
                "/api/ebas",
                "/api/membership",
                "/api/player/games",
                "/member/confirm",
            )
        ):
            continue
        limit = re.search(r"limit_req\s+zone=(\w+)\s+burst=(\d+)\s+(nodelay)?", body)
        if limit:
            zone, burst, nodelay = limit.groups()
            policy[location.strip()] = {
                "rate": zones.get(zone),
                "burst": int(burst),
                "nodelay": bool(nodelay),
            }
    return policy


ACTIVE_FLOWS_JS = r'''
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync('/home/node/.n8n/database.sqlite',{readOnly:true});
const rows=db.prepare(`SELECT w.id,w.name,w.settings,w.activeVersionId,h.nodes,h.connections
 FROM workflow_entity w LEFT JOIN workflow_history h ON h.versionId=w.activeVersionId
 WHERE w.active=1 ORDER BY w.id`).all();
for(const row of rows){
 if(!row.activeVersionId || !row.nodes) throw new Error('Published version unavailable');
 row.nodes=JSON.parse(row.nodes); row.connections=JSON.parse(row.connections);
 row.settings=row.settings?JSON.parse(row.settings):{};
}
console.log(JSON.stringify(rows)); db.close();
'''

BACKUP_VOLUME = r'''
import tarfile
with tarfile.open('/backup/n8n-data.tar.gz', 'w:gz') as archive:
    archive.add('/data', arcname='data')
'''

RESTORE_VOLUME = r'''
from pathlib import Path
import os, shutil, tarfile
target=Path('/data').resolve()
assert target == Path('/data')
with tarfile.open('/backup/n8n-data.tar.gz', 'r:gz') as archive:
    for member in archive.getmembers():
        destination=(Path('/') / member.name).resolve()
        if not destination.is_relative_to(target): raise RuntimeError('Unsafe backup member')
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()
    archive.extractall('/', filter='data')
    # The safe data filter deliberately clears archive owners. Restore them only
    # after its path/link checks, or node cannot write SQLite/config files.
    for member in archive.getmembers():
        destination=Path('/') / member.name
        os.chown(destination,member.uid,member.gid,follow_symlinks=False)
'''


class Docker:
    def run(self, *arguments, input=None, timeout=180):
        result = subprocess.run(
            ["docker", *map(str, arguments)], input=input, capture_output=True, timeout=timeout
        )
        if result.returncode:
            detail = ""
            if arguments[0] == "compose":
                # Compose diagnostics contain setup metadata, never provider responses.
                lines = result.stderr.decode(errors="replace").splitlines()
                errors = [
                    line
                    for line in lines
                    if "error response" in line.lower() or line.lower().startswith("error")
                ]
                if errors:
                    detail = re.sub(r'''(["']).*?\1''', "[quoted value]", errors[-1])
                    detail = re.sub(
                        r"\b\w*(?:KEY|TOKEN|PASSWORD)\w*=\S+", "[environment]", detail, flags=re.I
                    )
                    detail = re.sub(r"[0-9a-fA-F]{32,64}|[0-9]{10,}", "[identifier]", detail)
                    detail = ": " + detail[:400]
            raise RehearsalError(
                "DEV Docker operation failed: " + " ".join(map(str, arguments[:2])) + detail
            )
        return result.stdout

    def inspect(self, name, field):
        return json.loads(self.run("inspect", name, "--format", "{{json " + field + "}}"))

    def metadata(self):
        for name in (BACKEND, N8N, POSTGRES, NGINX):
            labels = self.inspect(name, ".Config.Labels")
            if labels.get("com.docker.compose.project") != PROJECT:
                raise RehearsalError("DEV container ownership guard failed")
            if NETWORK not in self.inspect(name, ".NetworkSettings.Networks"):
                raise RehearsalError("DEV network ownership guard failed")
        volume = next(
            mount["Name"]
            for mount in self.inspect(N8N, ".Mounts")
            if mount["Destination"] == "/home/node/.n8n"
        )
        attached = (
            self.run("ps", "-a", "--filter", "volume=" + volume, "--format", "{{.Names}}")
            .decode()
            .splitlines()
        )
        if attached != [N8N]:
            raise RehearsalError("The n8n volume is attached outside the single DEV container")
        return volume

    def environment(self, name):
        # Print only non-secret configuration and booleans/dummy values, never real keys.
        if name == N8N:
            code = "const o={}; for(const[k,v]of Object.entries(process.env))if(k.startsWith('STARTGG_')||['EBAS_API_KEY','EBAS_ASSOC','APP_ENV','ENV','IS_PROD'].includes(k))o[k]=(['EBAS_API_KEY','EBAS_ASSOC'].includes(k)||k.startsWith('STARTGG_'))?(v==='sim-dummy'?'sim-dummy':v==='sim'?'sim':'NOT_DUMMY'):v; console.log(JSON.stringify(o));"
            return json.loads(self.run("exec", name, "node", "-e", code))
        code = "import os,json; keys=['EBAS_TRANSPORT','EBAS_STUB_SCENARIO','INTEGRATION_ENGINE','MEMBERSHIP_FLOW','APP_ENV','ENV','IS_PROD']; print(json.dumps({k:os.getenv(k,'') for k in keys}))"
        return json.loads(self.run("exec", name, "python", "-c", code))

    def active_flows(self):
        return json.loads(self.run("exec", N8N, "node", "--no-warnings", "-e", ACTIVE_FLOWS_JS))

    def baseline(self):
        query = "SELECT count(*),coalesce(sum(payment_amount),0) FROM event_archive;"
        output = (
            self.run("exec", POSTGRES, "psql", "-U", "fgc", "-d", "fgc_checkin", "-At", "-c", query)
            .decode()
            .strip()
        )
        count, revenue = output.split("|")
        return {"archive_rows": int(count), "revenue": float(revenue)}


def progress(message):
    print("T074 " + message, flush=True)


@contextmanager
def shared_lock():
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(HUB / "bin" / "lock.ps1"),
        "-Agent",
        "build-1",
        "-Resource",
        "dev-db-shared",
        "-Task",
        "T-074",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RehearsalError("dev-db-shared is held; no rehearsal mutations performed")
    try:
        yield
    finally:
        result = subprocess.run([*command, "-Release"], capture_output=True)
        if result.returncode:
            raise RehearsalError("Failed to release dev-db-shared")


def snapshot(docker, directory):
    volume = docker.metadata()
    directory.mkdir(parents=True, exist_ok=False)
    before = docker.baseline()
    flows = docker.active_flows()
    # A running container can retain an image ID pruned after a later DEV build.
    image = json.loads(
        docker.run("image", "inspect", "fgt-dev-backend:latest", "--format", "{{json .Id}}")
    )
    manifest = {
        "project": PROJECT,
        "volume": volume,
        "image": image,
        "baseline": before,
        "active_ids": sorted(flow["id"] for flow in flows),
    }
    progress("snapshotting DEV Postgres and stopped n8n volume")
    dump = docker.run("exec", POSTGRES, "pg_dump", "-U", "fgc", "-d", "fgc_checkin", "-Fc")
    (directory / "postgres.dump").write_bytes(dump)
    docker.run("stop", N8N)
    try:
        docker.run(
            "run",
            "--rm",
            "--user",
            "0",
            "--mount",
            f"type=volume,source={volume},target=/data,readonly",
            "--mount",
            f"type=bind,source={directory},target=/backup",
            "--entrypoint",
            "python",
            image,
            "-c",
            BACKUP_VOLUME,
        )
    finally:
        docker.run("start", N8N)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, flows


def restore_data(docker, directory, *, postgres=True):
    if not directory.resolve().is_relative_to((HUB / "backups").resolve()):
        raise RehearsalError("Only a rehearsal backup beneath agent-hub/backups can be restored")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["project"] != PROJECT or docker.metadata() != manifest["volume"]:
        raise RehearsalError("Restore ownership differs from the DEV snapshot")
    progress("restoring DEV snapshot" + (" (Postgres and n8n)" if postgres else " (n8n)"))
    docker.run("stop", BACKEND, "fgt-dev-fgt_dashboard-1", N8N)
    if postgres:
        docker.run(
            "exec",
            "-i",
            POSTGRES,
            "pg_restore",
            "--clean",
            "--if-exists",
            "-U",
            "fgc",
            "-d",
            "fgc_checkin",
            input=(directory / "postgres.dump").read_bytes(),
        )
    docker.run(
        "run",
        "--rm",
        "--user",
        "0",
        "--mount",
        f"type=volume,source={manifest['volume']},target=/data",
        "--mount",
        f"type=bind,source={directory},target=/backup,readonly",
        "--entrypoint",
        "python",
        manifest["image"],
        "-c",
        RESTORE_VOLUME,
    )


def compose(docker, devstack, *arguments, override=None):
    files = ["-f", str(devstack / "docker-compose.dev.yml")]
    if override:
        files += ["-f", str(override)]
    output = docker.run("compose", "-p", PROJECT, *files, *arguments)
    if "up" in arguments:
        # nginx resolves these upstream names when loading its configuration.
        docker.run("exec", NGINX, "nginx", "-s", "reload")
    return output


def write_override(docker, directory):
    # Docker cannot create nested bind mountpoints beneath a read-only parent.
    for source in (ROOT / "backend", ROOT / "fgt_dashboard"):
        (source / "shared").mkdir(exist_ok=True)
    provider_keys = {}
    for container in (BACKEND, N8N):
        if container == BACKEND:
            code = "import os,json; print(json.dumps([k for k in os.environ if k.startswith('STARTGG_')]))"
            keys = json.loads(docker.run("exec", container, "python", "-c", code))
        else:
            keys = list(docker.environment(container))
        provider_keys.update({key: DUMMY_KEY for key in keys if key.startswith("STARTGG_")})
    provider_keys.update(
        {key: DUMMY_KEY for key in ("STARTGG_API_KEY", "STARTGG_API_TOKEN", "STARTGG_TOKEN")}
    )
    common = {
        **provider_keys,
        "EBAS_API_KEY": DUMMY_KEY,
        "EBAS_ASSOC": "sim",
        "EBAS_ASSOCIATION_NUMBER": "sim",
        "APP_ENV": "dev",
        "ENV": "dev",
        "IS_PROD": "false",
        "INTERNAL_API_TOKEN": "synthetic-t074-internal",
    }
    data = {
        "services": {
            "n8n": {
                "environment": {
                    **common,
                    "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false",
                    "N8N_LOG_LEVEL": "warn",
                }
            },
            "backend": {
                "command": [
                    "python",
                    "-B",
                    "/sim/ebas_http_sim.py",
                    "--service",
                    "backend",
                    "--scenario",
                    "/sim/ebas_fightbox.json",
                    "--port",
                    "8000",
                ],
                "environment": {
                    **common,
                    "INTEGRATION_ENGINE": "n8n",
                    "MEMBERSHIP_FLOW": "legacy",
                    "EBAS_TRANSPORT": "stub",
                    "EBAS_STUB_SCENARIO": "/sim/ebas_fightbox.json",
                    "SSE_TOKEN": SSE_TOKEN,
                    "N8N_BASIC_AUTH_USER": "",
                    "N8N_BASIC_AUTH_PASSWORD": "",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                "volumes": [
                    f"{ROOT / 'backend'}:/app:ro",
                    f"{ROOT / 'shared'}:/app/shared:ro",
                    f"{ROOT / 'scripts' / 'sim'}:/sim:ro",
                ],
            },
            "fgt_dashboard": {
                "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
                "volumes": [
                    f"{ROOT / 'fgt_dashboard'}:/app:ro",
                    f"{ROOT / 'shared'}:/app/shared:ro",
                ],
            },
        }
    }
    path = directory / "compose.rehearsal.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


BACKEND_HELPER = r'''
import json,sys
from datetime import datetime,timezone
from shared import storage as api, postgres_api as pg
p=json.load(sys.stdin); action=p['action']; slug=p['slug']; prefix=p['prefix']
if not slug.startswith('t074-fightbox-') or not prefix.startswith('t074-'):
    raise RuntimeError('Synthetic event guard')
actor={'user_id':'synthetic-t074-to','user_name':'Synthetic rehearsal TO','role':'Owner','is_dev':True}
def rows():
    return api.get_checkins(slug) or []
def facts():
    with pg._get_pool().connection() as c:
        return [row[0] for row in c.execute('SELECT to_jsonb(a) FROM event_archive a WHERE event_slug=%s ORDER BY id',(slug,)).fetchall()]
result={}
if action=='setup':
    current=api.get_active_settings_with_id()
    if not current: raise RuntimeError('Active settings required')
    games=['SSBU','SF6','T8','Strive']
    patch={
      'active_event_slug':slug,'event_display_name':'Synthetic FightBox rehearsal',
      'tournament_name':'Synthetic FightBox rehearsal','is_active':True,
      'event_date':'2026-11-14','timezone':'Europe/Stockholm',
      'default_game':games,'swish_expected_per_game':0,'require_payment':False,
      'require_membership':True,'require_startgg':False,'offer_membership':True,
      'collect_acquisition_source':False,'startgg_event_url':None,
      'checkin_opened_at':datetime.now(timezone.utc).isoformat(),
      'event_started_at':None,'event_ended_at':None,
      'events_json':{'tournament_entrants':0,'events':[
        {'id':990001+i,'name':g,'slug':slug+'/event/'+g.lower(),'startAt':None,'numEntrants':0,'setCount':0,'gamesPlayed':0}
        for i,g in enumerate(games)]},
    }
    if not api.update_settings(current['record_id'],patch): raise RuntimeError('Settings write failed')
    # These two known synthetic profiles make the later typo cases genuine live hints.
    seeds={}
    with pg._get_pool().connection() as c:
        for role in ('member-01','member-03'):
            tag=p.get('tags',{}).get(role,prefix+role)
            phone=p.get('phones',{}).get(role,'+12025550100' if role=='member-01' else '+12025550102')
            seeds[role]=c.execute('INSERT INTO players(name,tag,email,telephone) VALUES(%s,%s,%s,%s) RETURNING uuid',
                ('Synthetic '+role,tag,tag+'@example.invalid',phone)).fetchone()[0]
    result={'settings_fields':sorted(patch),'seed_profiles':seeds}
elif action=='rows':
    result={'rows':rows()}
elif action=='duplicates':
    api.detect_live_duplicate_hints(slug)
    hints=api.get_live_duplicate_hints(slug)
    candidates=p['candidates']; found=[]
    for row in rows():
        fields=row.get('fields',row)
        tag=fields.get('tag','')
        role=p.get('typos',{prefix+role+'x':role for role in candidates}).get(tag)
        if role is None: continue
        record_id=row.get('record_id') or fields.get('record_id')
        for hint in hints.get(record_id,[]):
            candidate=hint.get('candidate_uuid')
            if candidate==candidates[role]:
                found.append({'record_id':record_id,'candidate_uuid':candidate,'tag':tag})
    if len(found)<2: raise RuntimeError('Both typo hints must be visible')
    choice=found[0]
    pair=api.get_live_duplicate_pair(choice['record_id'],choice['candidate_uuid'])
    if not pair: raise RuntimeError('Duplicate dialog pair missing')
    version=pair.get('version')
    decision=api.resolve_live_duplicate(choice['record_id'],choice['candidate_uuid'],'keep_candidate',user=actor,version=version)
    typos=p.get('typos',{})
    result={'hint_count':len(found),'decision':decision,
      'confirmed_role':typos.get(choice['tag'],'synthetic typo'),
      'untouched_roles':sorted({typos.get(hit['tag'],'synthetic typo') for hit in found if hit['record_id']!=choice['record_id']})}
elif action=='end':
    current=api.get_active_settings_with_id()
    if current['fields'].get('active_event_slug')!=slug: raise RuntimeError('Active event changed')
    api.update_settings(current['record_id'],{'event_ended_at':datetime.now(timezone.utc).isoformat()})
    result={'ended':True}
elif action=='archive':
    result=api.archive_event(slug,event_date='2026-11-14',event_display_name='Synthetic FightBox rehearsal',
        swish_expected_per_game=0,startgg_snapshot={},clear_active=True,user=actor)
    result={'summary':result,'facts':facts()}
elif action=='reopen':
    result=api.reopen_event(slug,restore_active=True,user=actor)
elif action=='backfills':
    created=[]
    for index,game in enumerate(('T8','Strive'),1):
        payload={'name':'Synthetic backfill '+str(index),'tag':prefix+'b'+str(index),
          'startgg':True,'is_guest':False,'member':True,'payment_valid':False,
          'added_via':'manual_dashboard','tournament_games_registered':[game],'status':'Ready'}
        created.append(api.begin_checkin(slug,payload))
    with pg._get_pool().connection() as c:
        checkpoint=c.execute('SELECT clock_timestamp()').fetchone()[0]
    result={'created':created,'checkpoint':checkpoint}
elif action=='stats':
    with pg._get_pool().connection() as c:
        row=c.execute('SELECT to_jsonb(s) FROM event_stats s WHERE event_slug=%s',(slug,)).fetchone()
        markers=c.execute("SELECT count(*) FROM event_stats_refresh WHERE event_slug IN(%s,'*')",(slug,)).fetchone()[0]
        audits=c.execute("SELECT count(*) FROM audit_log WHERE action='automatic_stats_recomputed' AND timestamp>=%s::timestamptz",(p.get('since','1970-01-01'),)).fetchone()[0]
    result={'stale':bool(markers),'stale_markers':markers,'automatic_refresh_audits':audits,'stats':row[0] if row else None,'facts':facts()}
else:
    raise RuntimeError('Unknown rehearsal action')
print('T074_JSON:'+json.dumps(result,default=str))
'''


def backend_action(docker, action, slug, prefix, **extra):
    helper = (
        "import json,re\ntry:\n"
        + textwrap.indent(BACKEND_HELPER, "    ")
        + "\nexcept Exception as error:\n"
        + "    message=re.sub(r'[0-9]{10,}|[0-9a-fA-F]{32,64}','[redacted]',str(error))\n"
        + "    print('T074_JSON:'+json.dumps({'helper_error':type(error).__name__,'message':message[:400]}))\n"
    )
    result = docker.run(
        "exec",
        "-i",
        BACKEND,
        "python",
        "-B",
        "-c",
        helper,
        input=json.dumps({"action": action, "slug": slug, "prefix": prefix, **extra}).encode(),
    )
    for line in result.decode().splitlines():
        if line.startswith("T074_JSON:"):
            value = json.loads(line.removeprefix("T074_JSON:"))
            if value.get("helper_error"):
                raise RehearsalError(
                    "Product helper "
                    + action
                    + ": "
                    + value["helper_error"]
                    + ": "
                    + value["message"]
                )
            return value
    raise RehearsalError("DEV product API helper returned no result")


WORKER = r'''
import json,sys,time
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None
opener=build_opener(NoRedirect)
base=sys.argv[1]
for line in sys.stdin:
    request=json.loads(line); path=request['path']; started=time.monotonic()
    if not path.startswith('/api/') or '://' in path:
        print(json.dumps({'status':0,'json':{},'elapsed_ms':0}),flush=True); continue
    data=json.dumps(request['body']).encode() if request.get('body') is not None else None
    try:
        response=opener.open(Request(base+path,data=data,method=request['method'],headers={'Content-Type':'application/json'}),timeout=100)
    except HTTPError as error: response=error
    except Exception:
        print(json.dumps({'status':0,'json':{},'elapsed_ms':(time.monotonic()-started)*1000}),flush=True); continue
    with response:
        status=response.code
        try: body=json.loads(response.read(65536))
        except Exception: body={}
    print(json.dumps({'status':status,'json':body,'elapsed_ms':(time.monotonic()-started)*1000}),flush=True)
'''


class Sender:
    def __init__(self, docker, image, name, run_id):
        self.name = name
        self.docker = docker
        self.lock = threading.Lock()
        self.statuses = Counter()
        self.process = subprocess.Popen(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                name,
                "--label",
                "t074.run=" + run_id,
                "--network",
                NETWORK,
                "--entrypoint",
                "python",
                image,
                "-u",
                "-c",
                WORKER,
                "http://nginx",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def request(self, method, path, body=None):
        with self.lock:
            try:
                self.process.stdin.write(
                    json.dumps({"method": method, "path": path, "body": body}) + "\n"
                )
                self.process.stdin.flush()
                result = json.loads(self.process.stdout.readline())
            except (OSError, ValueError):
                raise RehearsalError("A synthetic sender failed") from None
            self.statuses[result["status"]] += 1
            return result

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.docker.run("rm", "-f", self.name)
            self.process.wait(timeout=10)


def start_simulators(docker, image, run_id, prefix):
    names = []
    for service in ("ebas", "startgg"):
        name = "t074-" + service + "-sim"
        docker.run(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "t074.run=" + run_id,
            "--network",
            NETWORK,
            "--network-alias",
            name,
            "--mount",
            f"type=bind,source={ROOT / 'shared'},target=/app/shared,readonly",
            "--mount",
            f"type=bind,source={ROOT / 'scripts' / 'sim'},target=/sim,readonly",
            "--env",
            "ENV=dev",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONPATH=/app",
            "--entrypoint",
            "python",
            image,
            "-B",
            "/sim/ebas_http_sim.py",
            "--service",
            service,
            "--scenario",
            "/sim/ebas_fightbox.json",
            "--prefix",
            prefix,
        )
        if docker.inspect(name, ".HostConfig.PortBindings"):
            raise RehearsalError("A simulator must never bind any host port")
        names.append(name)
    return names


def publish_simulated_flows(docker, directory, originals):
    wait_n8n(docker)
    flows = rehearsal_flows(originals)
    path = directory / "flows.generated.json"
    path.write_text(json.dumps(flows), encoding="utf-8")
    docker.run("exec", N8N, "n8n", "unpublish:workflow", "--all")
    docker.run("cp", path, N8N + ":/tmp/t074-flows.json")
    docker.run("exec", N8N, "n8n", "import:workflow", "--input=/tmp/t074-flows.json")
    for flow in flows:
        docker.run("exec", N8N, "n8n", "publish:workflow", "--id=" + flow["id"])
    docker.run("restart", N8N)
    return flows


def wait_health(target, *, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(target + "/health", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RehearsalError("DEV did not become healthy")


def wait_n8n(docker):
    deadline = time.monotonic() + 90
    code = "require('http').get('http://127.0.0.1:5678/healthz',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1)); setTimeout(()=>process.exit(1),2500);"
    while time.monotonic() < deadline:
        try:
            docker.run("exec", N8N, "node", "-e", code, timeout=5)
            return
        except RehearsalError:
            time.sleep(2)
    raise RehearsalError("Restored DEV n8n did not become healthy")


def public_payload(pnr, role, tags, phones, *, typo=False):
    tag = tags[role] + ("x" if typo else "")
    return {
        "name": "Synthetic " + role,
        "tag": tag,
        "email": tag + "@example.invalid",
        "telephone": phones[role],
        "personnummer": pnr,
    }


class Events:
    def __init__(self, target, prefix):
        self.url = target + "/api/events/stream?" + urlencode({"token": SSE_TOKEN})
        self.prefix = prefix
        self.tags = set()
        self.count = 0
        self.connected = threading.Event()
        self.socket = None
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        try:
            with urlopen(self.url, timeout=10) as response:
                self.socket = response.fp.raw._sock
                self.socket.settimeout(None)
                self.connected.set()
                for line in response:
                    if line.startswith(b"data:"):
                        data = json.loads(line.removeprefix(b"data:"))
                        self.count += 1
                        tag = data.get("tag", "")
                        if tag.startswith(self.prefix):
                            self.tags.add(tag)
        except Exception:
            return

    def start(self):
        self.thread.start()
        if not self.connected.wait(15):
            raise RehearsalError("The real nginx/dashboard SSE path did not connect")

    def close(self):
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=5)


def run_rush(docker, target, settings, directory, manifest, *, duration, seed, run_id, report=None):
    people = synthetic_people(settings)
    prefix = "t074-" + run_id + "-"
    slug = "t074-fightbox-" + run_id
    if report is None:
        report = {}
    report.update({"event": slug, "settings_date": "2026-11-14", "checks": [], "people": []})

    def check(name, passed, detail=""):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    tags = person_tags(settings, prefix)
    phones = {
        person["key"]: f"+1202555{100 + index:04d}" for index, person in enumerate(people.values())
    }
    setup = backend_action(docker, "setup", slug, prefix, tags=tags, phones=phones)
    report["settings_fields"] = setup["settings_fields"]
    senders = []
    capabilities = {}
    completed_at = {}
    events = Events(target, prefix)
    try:
        events.start()
        progress("launching 16 sender IPs (5 people share the laptop IP)")
        senders = [
            Sender(docker, manifest["image"], "t074-" + run_id + "-sender-" + str(index), run_id)
            for index in range(16)
        ]
        roles = [(pnr, person["key"]) for pnr, person in people.items()]
        assigned = {
            role: senders[0 if index < 5 else index - 4] for index, (_, role) in enumerate(roles)
        }
        rng = random.Random(seed)
        started = time.monotonic()

        def person(pnr, role, delay=0, *, typo=False, retry=False):
            guard_person(pnr, people)
            time.sleep(max(0, started + delay - time.monotonic()))
            sender = assigned[role]
            body = public_payload(pnr, role, tags, phones, typo=typo)
            response = sender.request("POST", "/api/checkin/orchestrate", body)
            result = response["json"]
            row = {
                "role": role + ("-typo" if typo else "-retry" if retry else ""),
                "http": response["status"],
                "latency_ms": round(response["elapsed_ms"], 2),
                "expected": "invalid" if role == "invalid-checksum" else "member",
                "member": result.get("member"),
                "status": result.get("status"),
            }
            if role == "invalid-checksum":
                check("invalid identity rejected", response["status"] in {400, 422})
                row["actual"] = "rejected" if response["status"] in {400, 422} else "unexpected"
                return row, result
            check(row["role"] + " accepted", response["status"] == 200)
            record_id, token = result.get("checkin_id"), result.get("status_token")
            if not record_id:
                row["actual"] = "missing check-in"
                return row, result
            if role.startswith("register-") and not retry:
                row["expected"] = "registered member"
                registration = sender.request(
                    "POST",
                    "/api/ebas/register",
                    {
                        **body,
                        "checkin_id": record_id,
                        "status_token": token,
                        "slug": slug,
                    },
                )
                row["registration_http"] = registration["status"]
                row["registration_state"] = registration["json"].get("state", "unknown")
                check(
                    row["role"] + " registered",
                    registration["status"] == 200 and registration["json"].get("member") is True,
                )
            unknown = role in {"confirm-timeout", "non-json-unavailable", "provider-error"}
            if unknown:
                row["expected"] = "unknown/pending"
                check(
                    role + " error is unknown",
                    result.get("member") is None and result.get("status") == "Pending",
                )
            if token:
                if not typo and not retry:
                    capabilities[role] = (record_id, token)
                status = sender.request(
                    "GET", "/api/checkin/" + record_id + "/status?" + urlencode({"t": token})
                )["json"]
                row["status"] = status.get("status")
                row["lookup_state"] = status.get("member_lookup_state")
                row["member"] = None if row["lookup_state"] == "unknown" else status.get("member")
                if unknown:
                    check(
                        role + " polling stays unknown",
                        status.get("member_lookup_state")
                        not in {"not_found", "not_member", "verified_false"}
                        and status.get("status") == "Pending",
                    )
                    row["actual"] = "unknown/pending"
                else:
                    sampled_timeout = (
                        result.get("member") is None
                        and status.get("status") == "Pending"
                        and status.get("member_lookup_state")
                        not in {"not_found", "not_member", "verified_false"}
                    )
                    check(
                        row["role"] + " member or sampled timeout",
                        (status.get("member") is True and status.get("ready") is True)
                        or sampled_timeout,
                    )
                    row["actual"] = (
                        "ready member"
                        if status.get("member")
                        else "unknown/pending (sampled timeout)"
                    )
                    if sampled_timeout:
                        row["expected"] = "member or unknown on sampled timeout"
                if role in {"member-02", "member-04"} and not retry:
                    game = "T8" if role == "member-02" else "Strive"
                    picked = sender.request(
                        "PATCH",
                        "/api/player/games",
                        {"checkin_id": record_id, "status_token": token, "games": [game]},
                    )
                    check(role + " manual game", picked["status"] == 200)
            else:
                row["actual"] = "existing check-in" if retry else "no capability"
                check(row["role"] + " safe repeat", retry)
            if not typo and not retry:
                completed_at[role] = time.monotonic()
            return row, result

        progress("running the seeded 25-attempt rush through nginx")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(person, pnr, role, rng.uniform(0, duration * 0.7))
                for pnr, role in roles
            ]
            for future in as_completed(futures):
                row, _ = future.result()
                report["people"].append(row)
            extras = []
            for role in ("member-01", "member-03"):
                pnr = next(pnr for pnr, key in roles if key == role)
                extras.append(executor.submit(person, pnr, role, typo=True))
            for role in ("member-06", "member-07", "member-08"):
                pnr = next(pnr for pnr, key in roles if key == role)
                extras.append(executor.submit(person, pnr, role, retry=True))
            for future in as_completed(extras):
                row, _ = future.result()
                report["people"].append(row)
        report["rush_seconds"] = round(time.monotonic() - started, 2)
        addresses = [
            docker.inspect(sender.name, ".NetworkSettings.Networks")[NETWORK]["IPAddress"]
            for sender in senders
        ]
        check("distinct phone IPs and one shared laptop IP", len(set(addresses)) == 16)
        report["sender_ips"] = len(set(addresses))
        report["shared_laptop_people"] = 5
        rows = backend_action(docker, "rows", slug, prefix)["rows"]
        check("dashboard active rows", len(rows) == 21, str(len(rows)))
        deadline = time.monotonic() + 5
        while len(events.tags) < 21 and time.monotonic() < deadline:
            time.sleep(0.1)
        check("SSE delivered every created check-in", len(events.tags) == 21, str(len(events.tags)))
        report["sse_events"] = events.count
        report["sse_created_tags"] = len(events.tags)
        report["unknown_memberships"] = []
        for role in ("confirm-timeout", "non-json-unavailable", "provider-error"):
            record_id, token = capabilities[role]
            observed = assigned[role].request(
                "GET", "/api/checkin/" + record_id + "/status?" + urlencode({"t": token})
            )["json"]
            remains_unknown = (
                observed.get("status") == "Pending"
                and observed.get("member_lookup_state") == "unknown"
            )
            check(role + " does not auto-heal while fault remains", remains_unknown)
            report["unknown_memberships"].append(
                {
                    "role": role,
                    "status": observed.get("status"),
                    "lookup_state": observed.get("member_lookup_state"),
                    "stored_member_boolean": observed.get("member"),
                    "observed_after_s": round(time.monotonic() - completed_at[role], 2),
                }
            )
        progress("confirming a live duplicate, ending check-in and archiving")
        duplicate = backend_action(
            docker,
            "duplicates",
            slug,
            prefix,
            candidates=setup["seed_profiles"],
            typos={tags[role] + "x": role for role in ("member-01", "member-03")},
        )
        report["duplicate_hints"] = duplicate["hint_count"]
        report["duplicate_decision"] = {
            "confirmed": duplicate["confirmed_role"] + "-typo",
            "left_alone": [role + "-typo" for role in duplicate["untouched_roles"]],
        }
        backend_action(docker, "end", slug, prefix)
        first = backend_action(docker, "archive", slug, prefix)["facts"]
        first_times = {row["id"]: row["checked_in_at"] for row in first}
        check(
            "first archive row count and arrival times",
            len(first) == 21 and all(first_times.values()),
        )
        backend_action(docker, "reopen", slug, prefix)
        filled = backend_action(docker, "backfills", slug, prefix)
        second = backend_action(docker, "archive", slug, prefix)["facts"]
        check("rearchive count", len(second) == 23, str(len(second)))
        check(
            "original arrivals preserved",
            all(
                first_times.get(row["id"]) == row["checked_in_at"]
                for row in second
                if row["id"] in first_times
            ),
        )
        backfills = [row for row in second if row["id"] not in first_times]
        report["arrival_evidence"] = {
            "initial_rows": len(first),
            "initial_with_time": sum(bool(row.get("checked_in_at")) for row in first),
            "final_rows": len(second),
            "final_with_time": sum(bool(row.get("checked_in_at")) for row in second),
            "preserved_original_times": sum(
                first_times.get(row["id"]) == row["checked_in_at"]
                for row in second
                if row["id"] in first_times
            ),
            "backfills": [
                {
                    "role": "backfill-" + str(index),
                    "checked_in_at": row.get("checked_in_at"),
                    "added_via": row.get("added_via"),
                }
                for index, row in enumerate(backfills, 1)
            ],
        }
        check(
            "every rearchived row has an arrival time",
            all(row.get("checked_in_at") for row in second),
        )
        check(
            "backfills retain manual_dashboard origin",
            all(row.get("added_via") == "manual_dashboard" for row in backfills),
        )
        check(
            "backfills have their own later arrival times",
            len(backfills) == 2
            and all(
                row.get("checked_in_at") and row["checked_in_at"] > max(first_times.values())
                for row in backfills
            ),
        )
        stats = backend_action(docker, "stats", slug, prefix, since=filled["checkpoint"])
        summary = stats.get("stats") or {}
        check("Insights statistics refreshed", stats.get("stale") is False and bool(summary))
        check("free event revenue", float(summary.get("total_revenue", -1)) == 0)
        check(
            "T067 automatic recomputation recorded after rearchive",
            stats["automatic_refresh_audits"] >= 1,
        )
        check(
            "Insights participant count uses confirmed identities",
            int(summary.get("total_participants", -1))
            == len({row["player_uuid"] for row in second}),
        )
        report["stats_refresh_evidence"] = {
            "stale_markers_before_any_manual_refresh": stats["stale_markers"],
            "automatic_refresh_audits_after_rearchive": stats["automatic_refresh_audits"],
            "manual_refresh_called": False,
        }
        report["archive_rows"] = len(second)
        report["statistics"] = {
            key: summary.get(key)
            for key in ("total_participants", "total_revenue", "new_players", "returning_players")
        }
        counts = Counter()
        for sender in senders:
            counts.update(sender.statuses)
        report["http_status_counts"] = dict(
            sorted({**counts, 429: counts[429], 502: counts[502]}.items())
        )
        check("no proxy or rate-limit errors", not any(counts[code] for code in (0, 429, 502)))
        latencies = sorted(row["latency_ms"] for row in report["people"])
        report["latency_ms"] = {
            "p50": statistics.median(latencies),
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "max": max(latencies),
        }
        trace = [
            json.loads(line)
            for line in docker.run("exec", BACKEND, "cat", "/tmp/t074-latency.roles.jsonl")
            .decode()
            .splitlines()
        ]
        guard_report(trace)
        (directory / "latency.roles.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        for row in report["people"]:
            if row["role"] == "invalid-checksum":
                continue
            role = row["role"].removesuffix("-typo").removesuffix("-retry")
            calls = [
                entry
                for entry in trace
                if entry["role"] == role and entry["action"] == "confirm_membership"
            ]
            entry = calls[1 if row["role"] != role else 0]
            row["injected_ms"] = entry["injected_ms"]
            row["overhead_ms"] = round(row["latency_ms"] - entry["transport_ms"], 2)
            row["scenario_timeout"] = entry["timeout"]
        ordered = sorted(report["people"], key=lambda row: row["latency_ms"])
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        maximum = ordered[-1]
        check(
            "p95 is dominated by measured injected wait",
            p95.get("injected_ms", 0) >= p95["latency_ms"] * 0.8,
        )
        check(
            "15 second maximum is the injected timeout",
            maximum.get("scenario_timeout") is True
            and maximum.get("injected_ms") == 15000
            and maximum["overhead_ms"] < 1000,
        )
        report["latency_attribution"] = {
            "p95_role": p95["role"],
            "p95_injected_ms": p95.get("injected_ms"),
            "p95_non_scenario_ms": p95.get("overhead_ms"),
            "max_role": maximum["role"],
            "max_injected_ms": maximum.get("injected_ms"),
            "max_non_scenario_ms": maximum.get("overhead_ms"),
        }
        return report
    finally:
        events.close()
        counts = Counter()
        for sender in senders:
            counts.update(sender.statuses)
        report.setdefault(
            "http_status_counts",
            dict(sorted({**counts, 429: counts[429], 502: counts[502]}.items())),
        )
        report.setdefault("sse_events", events.count)
        report.setdefault("sse_created_tags", len(events.tags))
        latencies = sorted(row["latency_ms"] for row in report["people"])
        if latencies:
            report.setdefault(
                "latency_ms",
                {
                    "p50": statistics.median(latencies),
                    "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
                    "max": max(latencies),
                },
            )
        for sender in senders:
            sender.close()


def write_report(report, directory):
    guard_report(report)
    failed = [check for check in report.get("checks", []) if not check["passed"]]
    states = {check["name"]: check["passed"] for check in report.get("checks", [])}
    arrival_checks = [
        "first archive row count and arrival times",
        "original arrivals preserved",
        "backfills have their own later arrival times",
    ]
    arrivals = " / ".join(
        (
            "PASS"
            if states.get(name) is True
            else "FAIL" if states.get(name) is False else "not reached"
        )
        for name in arrival_checks
    )
    lines = [
        "# T-074 FightBox rehearsal",
        "",
        "Result: " + ("FAIL" if failed or report.get("error") else "PASS"),
        "",
        "Event: " + report.get("event", "not started"),
        "Backup: " + str(directory),
        "Baseline: " + json.dumps(report.get("baseline", {})),
        "Restored: " + json.dumps(report.get("restored_baseline", {})),
        "Latency ms (check-in POST): " + json.dumps(report.get("latency_ms", {})),
        "HTTP counts: " + json.dumps(report.get("http_status_counts", {})),
        "Rush seconds: " + str(report.get("rush_seconds", "not started")),
        "Sender IPs / laptop people: "
        + str(report.get("sender_ips", 0))
        + " / "
        + str(report.get("shared_laptop_people", 0)),
        "SSE events / distinct created tags: "
        + str(report.get("sse_events", 0))
        + " / "
        + str(report.get("sse_created_tags", 0)),
        "Final archive rows: " + str(report.get("archive_rows", 0)),
        "Arrival checks (all copied / originals preserved / later backfills): " + arrivals,
        "TO duplicate: "
        + (
            "confirmed via dialog API; " + str(report["duplicate_hints"]) + " typo hints"
            if report.get("duplicate_hints", 0) >= 2
            else "not reached"
        ),
        "Statistics: " + json.dumps(report.get("statistics", {}), default=str),
        "Checks: "
        + str(len(report.get("checks", [])) - len(failed))
        + " passed, "
        + str(len(failed))
        + " failed.",
        "",
        "| Synthetic role | Expected | Actual status/member | HTTP | POST ms | Injected ms | Other ms |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for person in sorted(report.get("people", []), key=lambda person: person["role"]):
        actual = (
            str(person.get("status") or person.get("actual", ""))
            + "; member="
            + ("unknown" if person.get("member") is None else str(person["member"]))
        )
        lines.append(
            "| "
            + " | ".join(
                str(value)
                for value in (
                    person["role"],
                    person.get("expected", ""),
                    actual,
                    person.get("http", ""),
                    person.get("latency_ms", ""),
                    person.get("injected_ms", ""),
                    person.get("overhead_ms", ""),
                )
            )
            + " |"
        )
    lines += [
        "",
        "Arrival/backfill evidence: " + json.dumps(report.get("arrival_evidence", {})),
        "Duplicate decision: " + json.dumps(report.get("duplicate_decision", {})),
        "T067 automatic refresh evidence: " + json.dumps(report.get("stats_refresh_evidence", {})),
        "Measured latency attribution: " + json.dumps(report.get("latency_attribution", {})),
        "Injected ms measures the unchanged scenario sleep. Other ms includes all remaining HTTP/backend/n8n overhead and any queue wait; the measured p95/max are dominated by injection.",
        "",
        "Unknown membership observations:",
    ]
    for item in report.get("unknown_memberships", []):
        lines.append(
            "- "
            + item["role"]
            + ": "
            + item["status"]
            + ", lookup="
            + item["lookup_state"]
            + ", stored member flag="
            + str(item["stored_member_boolean"])
            + "; still pending after "
            + str(item["observed_after_s"])
            + " s."
        )
    lines += [
        "Public response member=null and lookup=unknown mean unverified, never a verified non-member.",
        "TO currently sees Pending, unchecked Member and Membership missing; there is no dedicated unknown badge.",
        "Polling does not retry eBas. The TO Recheck button checks Start.gg only. With these forced faults still active, no automatic heal is expected.",
        "After provider recovery the participant must retry check-in with their PNR, or a TO must verify membership and explicitly mark Member. This run leaves all three unverified.",
        "",
        "Nginx parity: "
        + (
            "public check-in/membership/game/polling routes MATCH PROD; no rate override applied."
            if report.get("nginx_limits_match_prod")
            else "not reached"
        ),
        "DEV admin/SSE keeps its existing dev_admin limit; the table below covers the public rehearsal routes.",
        "| Location | Rate | Burst | nodelay |",
        "|---|---:|---:|---|",
    ]
    for location, policy in sorted(report.get("nginx_limits_used", {}).items()):
        lines.append(
            "| "
            + " | ".join(map(str, (location, policy["rate"], policy["burst"], policy["nodelay"])))
            + " |"
        )
    lines += ["", "All named checks:", "| Check | Result | Detail |", "|---|---|---|"]
    for item in report.get("checks", []):
        lines.append(
            "| "
            + item["name"]
            + " | "
            + ("PASS" if item["passed"] else "FAIL")
            + " | "
            + item.get("detail", "")
            + " |"
        )
    lines += ["", "Failed expectations:"]
    lines += ["- " + item["name"] + ": " + item.get("detail", "") for item in failed] or ["- None"]
    if report.get("error"):
        lines += ["", "Operation error: " + report["error"]]
    lines += [
        "",
        "Exact settings fields written: " + ", ".join(report.get("settings_fields", [])),
        "Event date 2026-11-14; free; membership required; Start.gg admission optional; SSBU/SF6/T8/Strive; no Doubles.",
        "Defaults: snapshot before writes; n8n and runtime always restored. Postgres restores only with --restore or --restore-after.",
        "",
    ]
    text = "\n".join(lines)
    guard_report(text)
    path = HUB / "reports" / ("T-074-run-" + directory.name.removeprefix("T074-") + ".md")
    path.write_text(text, encoding="utf-8")
    return path, bool(failed or report.get("error"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="http://localhost:8088")
    parser.add_argument("--devstack", type=Path, default=ROOT.parent / "devstack")
    parser.add_argument(
        "--scenario", type=Path, default=ROOT / "scripts" / "sim" / "ebas_fightbox.json"
    )
    parser.add_argument("--duration", type=float, default=150)
    parser.add_argument("--seed", type=int, default=74)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--snapshot", action="store_true", help="Backup only; do not run check-ins")
    modes.add_argument(
        "--restore",
        type=Path,
        help="Explicitly restore a rehearsal postgres.dump plus its n8n backup",
    )
    parser.add_argument(
        "--restore-after",
        action="store_true",
        help="Explicitly restore Postgres after this run; n8n/runtime always restore",
    )
    options = parser.parse_args(argv)
    target = guard_target(options.target)
    settings = None if options.restore or options.snapshot else guard_scenario(options.scenario)
    if options.duration < 0 or not options.duration < 3600:
        raise RehearsalError("Rush duration must be between 0 and 3600 seconds")
    docker = Docker()
    with shared_lock():
        docker.metadata()
        if options.restore:
            directory = options.restore.resolve().parent
            restore_data(docker, directory)
            compose(
                docker,
                options.devstack,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "backend",
                "n8n",
                "fgt_dashboard",
            )
            wait_health(target)
            wait_n8n(docker)
            progress("restore verified: " + json.dumps(docker.baseline()))
            return 0
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
        directory = HUB / "backups" / ("T074-" + run_id)
        manifest, originals = snapshot(docker, directory)
        if options.snapshot:
            progress("snapshot saved: " + str(directory / "postgres.dump"))
            return 0
        report = {"checks": [], "backup": str(directory), "baseline": manifest["baseline"]}
        changed_runtime = False
        try:
            if manifest["baseline"] != {"archive_rows": 316, "revenue": 2325.0}:
                raise RehearsalError("Expected DEV baseline is 316 archived rows / 2325 kr")
            prod = rate_policy((ROOT / "nginx.conf").read_text(encoding="utf-8"))
            dev_source = docker.run("exec", NGINX, "cat", "/etc/nginx/conf.d/default.conf").decode()
            if not prod or rate_policy(dev_source) != prod:
                raise RehearsalError(
                    "DEV check-in/membership limits differ from PROD; no rush sent"
                )
            override = write_override(docker, directory)
            changed_runtime = True
            progress("applying dummy-key DEV override and private simulator containers")
            compose(
                docker,
                options.devstack,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "backend",
                "n8n",
                "fgt_dashboard",
                override=override,
            )
            progress("starting private HTTP simulators")
            start_simulators(docker, manifest["image"], run_id, "t074-" + run_id + "-")
            progress("generating and publishing the isolated workflow copies")
            publish_simulated_flows(docker, directory, originals)
            wait_health(target)
            guard_environment(docker.environment(BACKEND), docker.environment(N8N))
            guard_flows(docker.active_flows())
            progress("hard guards passed: actual published URLs and dummy keys")
            result = run_rush(
                docker,
                target,
                settings,
                directory,
                manifest,
                duration=options.duration,
                seed=options.seed,
                run_id=run_id,
                report=report,
            )
            report.update(result)
            report["nginx_limits_match_prod"] = True
            report["nginx_limits_used"] = prod
        except Exception as error:
            report["error"] = (
                str(error)
                if isinstance(error, RehearsalError)
                else "Unexpected rehearsal operation failed: " + type(error).__name__
            )
        finally:
            if changed_runtime:
                docker.run("stop", BACKEND, "fgt-dev-fgt_dashboard-1")
                restore_data(docker, directory, postgres=options.restore_after)
                compose(
                    docker,
                    options.devstack,
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "backend",
                    "n8n",
                    "fgt_dashboard",
                )
                wait_health(target)
                wait_n8n(docker)
                owned = (
                    docker.run("ps", "-aq", "--filter", "label=t074.run=" + run_id).decode().split()
                )
                if owned:
                    docker.run("rm", "-f", *owned)
                report["restored_baseline"] = docker.baseline()
                report["restored_active_flows"] = (
                    sorted(flow["id"] for flow in docker.active_flows()) == manifest["active_ids"]
                )
                report["restored_engine_none"] = (
                    docker.environment(BACKEND).get("INTEGRATION_ENGINE") == "none"
                )
                report["checks"].extend(
                    [
                        {
                            "name": "original n8n activations restored",
                            "passed": report["restored_active_flows"],
                        },
                        {
                            "name": "runtime restored to engine none",
                            "passed": report["restored_engine_none"],
                        },
                    ]
                )
                if options.restore_after:
                    report["checks"].append(
                        {
                            "name": "DEV archive baseline restored",
                            "passed": report["restored_baseline"] == manifest["baseline"],
                        }
                    )
        path, failed = write_report(report, directory)
        progress(("FAIL " if failed else "PASS ") + str(path))
        return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RehearsalError as error:
        print("T074 refused: " + str(error), file=sys.stderr)
        raise SystemExit(2)

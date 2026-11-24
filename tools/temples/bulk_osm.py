#!/usr/bin/env python3
"""Bounded, resumable OSM fact acquisition. Nothing here installs or runs the app.

No arguments: sequential country area queries, India via Geofabrik, 28 minute
working budget plus at most 15 seconds to close the manifest. A deadline means
INCOMPLETE, never success. See RUN_BULK.md for licences, budgets and limitations.
"""
import argparse, datetime, gzip, hashlib, importlib.util, json, math, os, shutil
import signal, sqlite3, subprocess, sys, tempfile, time, urllib.error,re
import urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX_URL = 'https://download.geofabrik.de/index-v1-nogeom.json'
# Hosted-job limits: a longer run may be allowed by the environment (defaults keep the 28-minute rule).
BUDGET_CAP = int(os.environ.get('TEMPLES_BULK_BUDGET_CAP', '1680'))
REGION_CAP = int(os.environ.get('TEMPLES_BULK_REGION_CAP', '660'))
ENDPOINT = 'https://overpass.private.coffee/api/interpreter'
UA = 'NityaradhyaTempleDirectoryBulk/4.0 https://github.com/Zenith2323/nityaradhya (offline ODbL facts)'
MIB = 1024 * 1024
MAX_PART = 19_000_000
MAX_OUTPUT = 149_000_000
MAX_EXTRACT = 1800 * MIB
LARGE = set('US CA RU AU ID BR CN GB DE FR'.split())
CHILDREN = set()

def encoded(x):
    return (json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode()

def atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.partial')
    try:
        temporary.write_bytes(data); os.replace(temporary, path)
    finally:temporary.unlink(missing_ok=True)

def digest(data): return hashlib.sha256(data).hexdigest()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def log(message): print(now()+' '+message, flush=True)

class BudgetStop(Exception): pass
class HardStop(BaseException): pass

class Budget:
    def __init__(self, seconds=1680): self.end = time.monotonic()+seconds
    def left(self): return max(0, self.end-time.monotonic())
    def require(self, seconds=1):
        if self.left() < seconds: raise BudgetStop('Time budget exhausted')
    def pause(self, seconds):
        self.require(seconds+1); time.sleep(seconds)

def retained(tags):
    """Explicit allow-list: contact/opening/media/operator text never passes."""
    fixed = {'name','alt_name','old_name','loc_name','short_name','official_name',
             'religion','denomination','deity','dedicated_to','amenity','building',
             'historic','heritage','ruins','disused','abandoned','wikidata',
             'wikipedia','type','site','part_of','parent','access'}
    prefixes = ('name:', 'addr:', 'heritage:', 'ruins:', 'disused:', 'abandoned:')
    allowed_suffix = {'religion','amenity','building','historic','temple','yes'}
    blocked={'contact','phone','fax','email','website','url','opening_hours','description','image'}
    return {k:v for k,v in sorted(tags.items()) if not(set(k.split(':')) & blocked) and (k in fixed or
            k.startswith(('name:','addr:','heritage:')) or
            (k.startswith(prefixes[3:]) and k.split(':',1)[1] in allowed_suffix))}

def selected(tags):
    hindu = any('hindu' in str(tags.get(p+'religion','')).lower().split(';')
                for p in ['', 'disused:', 'abandoned:', 'ruins:'])
    worship = any(tags.get(p+'amenity') == 'place_of_worship' or
                  tags.get(p+'building') == 'temple' or bool(tags.get(p+'historic'))
                  for p in ['', 'disused:', 'abandoned:', 'ruins:'])
    return hindu and worship

def facts(elements):
    result = {}
    for e in elements:
        if e.get('type') not in ('node','way','relation') or not selected(e.get('tags',{})): continue
        row = {'type':e['type'], 'id':int(e['id']), 'tags':retained(e['tags'])}
        point = e.get('center', e)
        if point.get('lat') is not None and point.get('lon') is not None:
            row['center'] = {'lat':point['lat'], 'lon':point['lon']}
        else: row['center'] = None
        result[(row['type'],row['id'])] = row
    return [result[k] for k in sorted(result)]

def query(code):
    # Country boundaries are queried once; no worldwide box scraping. Include
    # lifecycle-tagged historic Hindu places in the same bounded response.
    if not (len(code)==2 and code.isalpha()): raise ValueError('ISO country code required')
    selects = []
    for religion in ['', 'disused:', 'abandoned:', 'ruins:']:
        for prefix in ['', 'disused:', 'abandoned:', 'ruins:']:
            for clause in ['["'+prefix+'amenity"="place_of_worship"]',
                           '["'+prefix+'building"="temple"]', '["'+prefix+'historic"]']:
                selects.append('nwr(area.a)["'+religion+'religion"="hindu"]'+clause+';')
    return '[out:json][timeout:70][maxsize:134217728];area["ISO3166-1"="'+code+'"][admin_level=2]->.a;.a out count;('+''.join(selects)+');out center tags;'

class Network:
    def __init__(self, budget): self.budget=budget; self.rate_limits=0; self.overpass_disabled=False; self.last_request=0
    def request(self, url, data=None, method=None, timeout=75):
        self.budget.require(2)
        return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method,
             headers={'User-Agent':UA,'Accept':'application/json,application/octet-stream'}),
             timeout=max(1,min(timeout,self.budget.left())))
    def json(self,url,limit=4*MIB):
        with self.request(url,timeout=20) as r: data=r.read(limit+1)
        if len(data)>limit: raise ValueError('JSON response exceeds budget')
        return json.loads(data)
    def overpass(self,code):
        if self.overpass_disabled: raise BudgetStop('Overpass disabled after repeated 429 responses')
        payload=urllib.parse.urlencode({'data':query(code)}).encode()
        for attempt in range(2):
            self.budget.pause(max(0,5-(time.monotonic()-self.last_request)))
            self.last_request=time.monotonic()
            try:
                with self.request(ENDPOINT,payload,timeout=78) as response:
                    raw=response.read(128*MIB+1)
                if len(raw)>128*MIB: raise ValueError('Overpass response too large')
                obj=json.loads(raw)
                if obj.get('remark') or 'elements' not in obj: raise ValueError('Overpass partial/error response: '+str(obj.get('remark','missing elements')))
                area_counts=[x for x in obj['elements'] if x.get('type')=='count']
                if len(area_counts)!=1 or int(area_counts[0].get('tags',{}).get('areas',area_counts[0].get('tags',{}).get('total','0')))==0:
                    raise ValueError('Country area missing; an empty area is not an empty country')
                return facts(obj['elements']), {'sourceUrl':ENDPOINT,'query':query(code),
                    'extractDate':obj.get('osm3s',{}).get('timestamp_osm_base'), 'rawSha256':digest(raw)}
            except urllib.error.HTTPError as error:
                if error.code==429:
                    self.rate_limits+=1
                    if self.rate_limits>=2:
                        self.overpass_disabled=True
                        raise BudgetStop('Repeated 429: stopped Overpass; no endpoint rotation') from error
                if error.code not in (429,502,504) or attempt: raise
                delay=max(15,int(error.headers.get('Retry-After','15')) if str(error.headers.get('Retry-After','15')).isdigit() else 15)
                self.budget.pause(min(delay,120))
        raise RuntimeError('Overpass did not complete')
    def download(self,url,path,seconds):
        parsed=urllib.parse.urlparse(url)
        if parsed.scheme!='https' or parsed.hostname!='download.geofabrik.de' or not parsed.path.endswith('.osm.pbf'):raise ValueError('Unapproved extract URL')
        self.budget.require(seconds)
        with self.request(url,method='HEAD',timeout=15) as response:
            size=int(response.headers.get('Content-Length','0'))
        if size<=0: raise BudgetStop('Extract size unknown; cannot establish disk/time budget')
        if size>MAX_EXTRACT or size*3+2*1024*MIB>shutil.disk_usage(path.parent).free:
            raise BudgetStop('Extract exceeds per-region disk budget')
        estimate=25+size/(8*MIB)+size/(12*MIB)*3
        if estimate>seconds: raise BudgetStop('HEAD size exceeds remaining per-region time budget')
        log('Downloading '+url+' ('+str(size)+' bytes; region allowance '+str(round(seconds))+' seconds)')
        started=time.monotonic();end=min(self.budget.end,started+seconds); sha=hashlib.sha256(); written=0
        try:
            with self.request(url,timeout=min(30,seconds)) as response, path.open('wb') as dest:
                date=response.headers.get('Last-Modified')
                while True:
                    if time.monotonic()>end: raise BudgetStop('Download deadline reached')
                    chunk=response.read(MIB)
                    if not chunk: break
                    written+=len(chunk)
                    if written>size or written>MAX_EXTRACT: raise BudgetStop('Download exceeds declared size')
                    sha.update(chunk); dest.write(chunk)
                    if written%(64*MIB)==0:log('Downloaded '+str(written//MIB)+' MiB')
                    elapsed=time.monotonic()-started
                    if elapsed>20 and (size-written)/max(1,written/elapsed)>end-time.monotonic():
                        raise BudgetStop('Observed transfer speed cannot finish within region budget')
            if written!=size: raise ValueError('Truncated extract')
            return {'sourceUrl':url,'extractDate':date,'extractSha256':sha.hexdigest(),'extractBytes':written}
        except BaseException:
            path.unlink(missing_ok=True); raise

def run_command(command,budget,cap):
    budget.require(2); log('Running: '+' '.join(map(str,command)))
    child=subprocess.Popen(list(map(str,command)),start_new_session=True)
    CHILDREN.add(child)
    try:
        rc=child.wait(timeout=min(cap,budget.left()))
        if rc: raise RuntimeError('Command failed (%s): %s'%(rc,command[0]))
    except BaseException:
        try: os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        child.wait(); raise
    finally: CHILDREN.discard(child)

def ensure_tools(budget,install=True):
    venv=Path(tempfile.gettempdir())/'nityaradhya-osmium-venv'
    if (venv/'bin/python').exists():os.environ.setdefault('TEMPLES_OSMIUM_PYTHON',str(venv/'bin/python'))
    if shutil.which('osmium') and (importlib.util.find_spec('osmium') or os.environ.get('TEMPLES_OSMIUM_PYTHON')): return
    if not install: raise RuntimeError('osmium-tool / Python osmium absent; --no-install selected')
    budget.require(200)
    if not shutil.which('osmium'):
        log('Installing test/data tool: apt osmium-tool (not an app dependency)')
        prefix=[] if os.geteuid()==0 else ['sudo','-n']
        run_command(prefix+['apt-get','update','-qq'],budget,90)
        run_command(prefix+['apt-get','install','-y','--no-install-recommends','osmium-tool'],budget,90)
    if not importlib.util.find_spec('osmium'):
        log('Installing data tool: Python package osmium (pyosmium)')
        # Local tool venv avoids modifying system Python or repository dependencies.
        if not (venv/'bin/python').exists(): run_command([sys.executable,'-m','venv',venv],budget,30)
        run_command([venv/'bin/python','-m','pip','install','--disable-pip-version-check','osmium>=4,<5'],budget,90)
        os.environ['TEMPLES_OSMIUM_PYTHON']=str(venv/'bin/python')

def process_pbf(path, output):
    """Called in a killable child. Bounding-box centres, never invented points.

    osmium keeps referenced objects. SQLite stores geometry instead of holding
    all node positions in Python RAM. Non-multipolygon site relations also work.
    """
    import osmium
    output=Path(output); db=sqlite3.connect(str(output.with_suffix('.sqlite')))
    db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
    db.execute('CREATE TABLE geometry (kind TEXT,id INTEGER,a REAL,b REAL,c REAL,d REAL,PRIMARY KEY(kind,id)) WITHOUT ROWID')
    results=[];pending=[];processed=0
    def bbox(points):
        return (min(x[0] for x in points),min(x[1] for x in points),max(x[0] for x in points),max(x[1] for x in points)) if points else None
    def store(kind,ident,box,tags):
        nonlocal processed
        if box: db.execute('INSERT OR REPLACE INTO geometry VALUES (?,?,?,?,?,?)',(kind,ident,*box))
        if selected(tags):
            row={'type':{'n':'node','w':'way','r':'relation'}[kind],'id':ident,'tags':retained(tags),'center':None}
            if box: row['center']={'lat':round((box[1]+box[3])/2,8),'lon':round((box[0]+box[2])/2,8)}
            results.append(row)
        processed+=1
        if processed%10000==0: db.commit()
    class Handler(osmium.SimpleHandler):
        def node(self,n):
            box=(n.location.lon,n.location.lat,n.location.lon,n.location.lat) if n.location.valid() else None
            store('n',n.id,box,dict(n.tags))
        def way(self,w):
            points=[];missing=False
            for node in w.nodes:
                entry=db.execute('SELECT a,b FROM geometry WHERE kind=? AND id=?',('n',node.ref)).fetchone()
                if entry: points.append(entry)
                else: missing=True
            store('w',w.id,bbox(points) if not missing else None,dict(w.tags))
        def relation(self,r): pending.append((r.id,dict(r.tags),[(m.type,m.ref) for m in r.members]))
    Handler().apply_file(str(path))
    for depth in range(10):
        rest=[]
        for ident,tags,members in pending:
            points=[];missing=False
            for kind,ref in members:
                box=db.execute('SELECT a,b,c,d FROM geometry WHERE kind=? AND id=?',(kind,ref)).fetchone()
                if box: points.extend([(box[0],box[1]),(box[2],box[3])])
                else: missing=True
            if missing and depth<9: rest.append((ident,tags,members));continue
            store('r',ident,bbox(points) if not missing else None,tags)
        pending=rest
        if not rest: break
    db.close();output.with_suffix('.sqlite').unlink(missing_ok=True)
    atomic(output,encoded({'elements':facts(results)}))

FILTERS=['nwr/amenity=place_of_worship','nwr/building=temple','nwr/historic']
FILTERS += ['nwr/'+prefix+key for prefix in ['disused:','abandoned:','ruins:'] for key in ['amenity=place_of_worship','building=temple','historic']]

def filter_pbf(path,work,budget):
    filtered=work/'filtered.osm.pbf';output=work/'facts.json'
    run_command(['osmium','tags-filter',path,*FILTERS,'--remove-tags','-o',filtered,'--overwrite'],budget,min(300,budget.left()))
    # Delete original before export; only one downloaded extract is ever retained.
    path.unlink()
    python=os.environ.get('TEMPLES_OSMIUM_PYTHON',sys.executable)
    run_command([python,HERE/'bulk_osm.py','--process-pbf',filtered,'--worker-output',output],budget,min(300,budget.left()))
    return json.loads(output.read_bytes())['elements']

def estimated_mb(code):
    # Planning estimates ONLY. HEAD is authoritative before each download.
    return {'IN':1638,'NP':396,'LK':110,'BD':230,'MY':350,'SG':350,'BN':350,
            'MU':15,'FJ':20,'AE':160,'OM':65,'BH':15,'QA':40,'KW':55,
            'GB':1800,'DE':4000,'FR':4000,'NL':1200,'ZA':350,'KE':150,
            'BT':30,'GY':40,'SR':35,'TT':35,'KH':160,'TH':550,'MM':300,
            'PK':250,'NZ':450}.get(code,500)

def make_plan(index,countries):
    properties=sorted((f['properties'] for f in index.get('features',[])),key=lambda p:p['id'])
    by_id={p['id']:p for p in properties}
    plans=[]
    for code,name in sorted(countries.items()):
        matches=[p for p in properties if code in p.get('iso3166-1:alpha2','').split(',')]
        main=next((p for p in matches if p.get('urls',{}).get('pbf')),None)
        extracts=[]
        if main:
            choices=[main]
            if code in LARGE:
                choices=[p for p in properties if p.get('parent')==main['id'] and p.get('urls',{}).get('pbf')]
                # A parent's special regions are part of full completion too.
                choices += [p for p in properties if p['id'] in main.get('special_regions',[]) and p not in choices]
                if code=='US':
                    # The index's five special US regions overlap its state
                    # children; they are alternatives, not extra territory.
                    choices=[p for p in choices if p['id'] not in main.get('special_regions',[])]
                if code=='GB':
                    # The pinned index explicitly says Bermuda/Falklands are
                    # convenience listings, not UK coverage. Northern Ireland
                    # is in the shared Ireland extract, not England/Scotland/Wales.
                    required=['england','ireland-and-northern-ireland','scotland','wales']
                    choices=[by_id[x] for x in required] if all(x in by_id and by_id[x].get('urls',{}).get('pbf') for x in required) else []
            for p in sorted(choices,key=lambda p:p['id']):
                extracts.append({'id':p['id'],'url':p['urls']['pbf'],'estimatedMiB':estimated_mb(code) if p is main else 400})
        size=sum(x['estimatedMiB'] for x in extracts) if extracts else None
        seconds=round(25+(size or 0)/8+(size or 0)/12*3)
        plans.append({'code':code,'name':name,'route':'pbf' if code=='IN' else 'overpass-then-pbf' if extracts else 'overpass-only',
                      'expectedMiB':size,'expectedMinutes':round((seconds if code=='IN' else 12)/60,2),
                      'fallbackMinutes':round(seconds/60,2) if extracts else None,
                      'regionSeconds':min(REGION_CAP,max(90,seconds+40)),'diskBudgetMiB':3*max((x['estimatedMiB'] for x in extracts),default=0)+2048,
                      'extracts':extracts,'estimateBasis':'Planning allowance, not measured; HEAD and live remaining budget override'})
    # Smallest known extract estimate first; no-extract queries follow these.
    # Country ISO breaks ties. Large countries
    # never use a national PBF; their child extracts are likewise smallest-first.
    return sorted(plans,key=lambda p:(p['expectedMiB'] if p['expectedMiB'] is not None else float('inf'),p['code']))

def write_parts(out,code,elements,metadata):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',code):raise ValueError('Unsafe region/extract identifier')
    rows=facts(elements);parts=[];chunk=[];size=0
    chunks=[]
    for row in rows:
        n=len(encoded(row))
        if n>MAX_PART//2: raise ValueError('Single fact too large')
        if chunk and size+n>12*MIB: chunks.append(chunk);chunk=[];size=0
        chunk.append(row);size+=n
    if chunk or not chunks: chunks.append(chunk)
    for i,chunk in enumerate(chunks,1):
        body=encoded({'schemaVersion':1,'dataset':'osm-bulk','licence':'ODbL-1.0','region':code,'elements':chunk})
        compressed=gzip.compress(body,mtime=0)
        if len(compressed)>MAX_PART: raise ValueError('Fact part exceeds 19 MB')
        used=sum(p.stat().st_size for p in sorted(out.rglob('*.gz')))
        # Leave room for the full plan, provenance and failure log. Save() also
        # guards the actual manifest plus every fact part against the total cap.
        existing=out/('facts/'+code+'-%03d.json.gz'%i)
        replacement=existing.stat().st_size if existing.exists() else 0
        if used-replacement+len(compressed)+4*MIB>MAX_OUTPUT: raise BudgetStop('Output budget reached; remaining regions not completed')
        relative='facts/'+code+'-%03d.json.gz'%i;atomic(out/relative,compressed)
        parts.append(dict(metadata,path=relative,rows=len(chunk),decodedBytes=len(body),compressedBytes=len(compressed),sha256=digest(compressed),decodedSha256=digest(body),licence='ODbL-1.0'))
    return parts

def finished(entry,out):
    if entry.get('status')!='complete' or not entry.get('files'): return False
    for f in entry['files']:
        path=(out/f['path']).resolve()
        if not path.is_relative_to(out.resolve()) or not path.is_file() or path.suffix!='.gz': return False
        try:
            if digest(gzip.decompress(path.read_bytes()))!=f['decodedSha256']: return False
        except (OSError,EOFError): return False
    return True

def acquire(plan,out,budget,network,install=True,only=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);mp=out/'manifest.json'
    manifest=json.loads(mp.read_text()) if mp.exists() else {'schemaVersion':1,'dataset':'osm-bulk','licence':'ODbL-1.0','regions':{},'failures':[]}
    manifest.update(updatedAt=now(),plan=plan,scope='OSM tagged objects only; never all temples',attribution='© OpenStreetMap contributors — ODbL 1.0',licenceUrl='https://opendatacommons.org/licenses/odbl/1-0/')
    def save():
        body=encoded(manifest)
        used=sum(p.stat().st_size for p in sorted((out/'facts').glob('*.json.gz')))
        if used+len(body)>MAX_OUTPUT:raise BudgetStop('Total fact and manifest output budget exceeded')
        atomic(mp,body)
    tools_ready=False;india_attempted=False
    try:
        for region in plan:
            code=region['code']
            if only and code not in only: continue
            if code=='IN':india_attempted=True
            prior=manifest['regions'].get(code,{})
            if finished(prior,out): log(code+' already complete and hashes verified');continue
            entry={'status':'pending','name':region['name'],'files':[],'extracts':prior.get('extracts',{}),'attemptedAt':now()}
            manifest['regions'][code]=entry;save()
            def failure(route,error):
                record={'region':code,'route':route,'time':now(),'error':str(error)}
                manifest['failures'].append(record);log(code+' '+route+': '+str(error));save()
            try:
                budget.require(10)
                # Reserve India's declared budget while preceding small countries
                # run. This avoids spending all 28 minutes on inaccessible APIs.
                india_requested=not only or 'IN' in only
                india_done=finished(manifest['regions'].get('IN',{}),out)
                if code!='IN' and india_requested and not india_done and not india_attempted and budget.left()<(760 if tools_ready else 960):
                    raise BudgetStop('Skipped to preserve India extract budget')
                previous_overpass_failure=any(f.get('region')==code and f.get('route')=='overpass' for f in manifest['failures'])
                if region['route']!='pbf' and not previous_overpass_failure:
                    try:
                        elements,meta=network.overpass(code)
                        entry['files']=write_parts(out,code,elements,dict(meta,route='overpass',fetchedAt=now()))
                        entry.update(status='complete',route='overpass',rows=len(elements));save();continue
                    except Exception as error: failure('overpass',error)
                if not region['extracts']: raise BudgetStop('No licensed extract fallback in saved Geofabrik index')
                budget.require(min(region['regionSeconds'],120))
                for extract in sorted(region['extracts'],key=lambda x:(x['estimatedMiB'],x['id'])):
                    eid=extract['id'];cached=entry['extracts'].get(eid,{})
                    if finished(cached,out): continue
                    # Some Geofabrik extracts cover several countries. Reuse a
                    # verified identical extract instead of downloading it again.
                    shared=next((other.get('extracts',{}).get(eid) for _,other in sorted(manifest['regions'].items()) if finished(other.get('extracts',{}).get(eid,{}),out) and all(f.get('sourceUrl')==extract['url'] for f in other['extracts'][eid]['files'])),None)
                    if shared:
                        entry['extracts'][eid]=dict(shared);save();continue
                    if not tools_ready:ensure_tools(budget,install);tools_ready=True
                    seconds=min(region['regionSeconds'],budget.left()-10)
                    region_started=time.monotonic()
                    workroot=out/'.work';workroot.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='region-',dir=workroot) as wd:
                        work=Path(wd);pbf=work/'source.osm.pbf'
                        meta=network.download(extract['url'],pbf,seconds)
                        child_budget=Budget(min(seconds-(time.monotonic()-region_started),budget.left()-5))
                        elements=filter_pbf(pbf,work,child_budget)
                        files=write_parts(out,code+'-'+eid,elements,dict(meta,route='geofabrik',fetchedAt=now()))
                        entry['extracts'][eid]={'status':'complete','files':files,'rows':len(elements)};save()
                # Never infer completion from one subregion, or an empty list.
                if not all(finished(entry['extracts'].get(e['id'],{}),out) for e in region['extracts']): raise ValueError('Incomplete extract route')
                entry['files']=[f for eid in sorted(entry['extracts']) for f in entry['extracts'][eid]['files']]
                entry.update(status='complete',route='geofabrik',rows=sum(f['rows'] for f in entry['files']))
            except Exception as error:
                failure('geofabrik-or-budget',error);entry.update(status='incomplete',reason=str(error))
            save()
        for r in plan:
            if not only or r['code'] in only: manifest['regions'].setdefault(r['code'],{'status':'not-started','reason':'No remaining budget'})
    finally:
        for entry in manifest['regions'].values():
            if entry.get('status')=='pending': entry.update(status='incomplete',reason='Interrupted before completion')
        manifest['complete']=all(finished(manifest['regions'].get(r['code'],{}),out) for r in plan)
        manifest['finishedAt']=now();save()
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run',action='store_true');p.add_argument('--out',type=Path,default=Path('bulk-osm-out'))
    p.add_argument('--regions',help='Optional comma-separated country ISO codes; all by default')
    p.add_argument('--budget-seconds',type=int,default=1680);p.add_argument('--no-install',action='store_true')
    p.add_argument('--process-pbf',type=Path,help=argparse.SUPPRESS);p.add_argument('--worker-output',type=Path,help=argparse.SUPPRESS)
    args=p.parse_args()
    if args.process_pbf: process_pbf(args.process_pbf,args.worker_output);return
    if not 1<=args.budget_seconds<=BUDGET_CAP: p.error('Working budget must be 1..%d seconds'%BUDGET_CAP)
    plan_path=HERE/'bulk-plan.json';saved=json.loads(plan_path.read_text())
    plan=saved['regions']
    scale=int(os.environ.get('TEMPLES_BULK_REGION_SCALE','1'))
    if scale>1:
        for item in plan: item['regionSeconds']=min(REGION_CAP,item['regionSeconds']*scale)
    if args.dry_run:
        print(json.dumps({'budgetSeconds':args.budget_seconds,'note':'Estimates, not a promise all regions finish. No network in dry-run.','regions':plan},ensure_ascii=False,indent=2));return
    budget=Budget(args.budget_seconds);network=Network(budget)
    only=set(args.regions.upper().split(',')) if args.regions else None
    unknown=(only or set())-{r['code'] for r in plan}
    if unknown: p.error('Unknown country codes: '+','.join(sorted(unknown)))
    # POSIX alarm bounds DNS, installs and C-extension processing too. Workers
    # are in their own process groups and are killed before final manifest write.
    def stop(signum,frame):
        for child in list(CHILDREN):
            try: os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        raise HardStop('Hard deadline / termination signal')
    signal.signal(signal.SIGALRM,stop);signal.signal(signal.SIGTERM,stop)
    signal.alarm(args.budget_seconds+15)
    try:
        manifest=acquire(plan,args.out,budget,network,not args.no_install,only)
        log('Complete countries: '+str(sum(x.get('status')=='complete' for x in manifest['regions'].values()))+'; see manifest for every incomplete region')
    except (BudgetStop,HardStop) as error: log(str(error))
    finally: signal.alarm(0)

if __name__=='__main__': main()

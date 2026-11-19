"""Read-only validation of plan bodies, ordering, links and file checksums."""
from pathlib import Path
import hashlib,json,re,sys

root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'02-字数与依赖校验.json').read_text(encoding='utf-8'))
rows=manifest['modules'];by_id={r['id']:r for r in rows};errors=[]
if len(rows)!=24 or len(by_id)!=24:errors.append('Expected 24 unique modules')
if len(list((root/'模块').glob('*.md')))!=24:errors.append('Unexpected module file count')
han=[];nonwhite=[]
for r in rows:
    p=root/r['file']
    if not p.is_file():errors.append(r['id']+': missing file');continue
    t=p.read_text(encoding='utf-8')
    if t.count('【执行方案本体】')!=1 or t.count('【执行方案本体结束】')!=1:
        errors.append(r['id']+': invalid markers');continue
    b=t.split('【执行方案本体】',1)[1].split('【执行方案本体结束】',1)[0]
    h=len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]',b));n=len(re.sub(r'\s','',b))
    han.append(h);nonwhite.append(n)
    if not (2000<h<3000 and 2000<n<3000):errors.append(r['id']+': body length invalid')
    if h!=r['han_characters'] or n!=r['non_whitespace_characters']:errors.append(r['id']+': count metadata mismatch')
    if hashlib.sha256(p.read_bytes()).hexdigest()!=r['sha256']:errors.append(r['id']+': module checksum mismatch')
    if len(re.findall(r'^\d+\. ',b,re.M))!=8:errors.append(r['id']+': expected 8 steps')
    if len(re.findall(r'^- ',b,re.M))!=8:errors.append(r['id']+': expected 8 acceptance cases')
    expected={x['id'] for x in rows if x['batch']==r['batch']-1}
    if set(r['dependencies'])!=expected:errors.append(r['id']+': invalid batch dependency')
    peers={x['id'] for x in rows if x['batch']==r['batch'] and x['id']!=r['id']}
    if set(r['parallel_peers'])!=peers:errors.append(r['id']+': invalid peer set')
for p in root.rglob('*.md'):
    text=p.read_text(encoding='utf-8')
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if target.startswith(('http://','https://','#')):continue
        if not (p.parent/target.split('#',1)[0]).exists():errors.append('Broken link in '+p.name)
for line in (root/'SHA256SUMS').read_text(encoding='utf-8').splitlines():
    digest,name=line.split('  ',1);p=root/name
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:errors.append('File checksum mismatch: '+name)
print(json.dumps({'ok':not errors,'modules':len(rows),'batches':len({r['batch'] for r in rows}),
 'han_range':[min(han),max(han)],'non_whitespace_range':[min(nonwhite),max(nonwhite)],
 'total_han':sum(han),'errors':errors},ensure_ascii=False))
sys.exit(1 if errors else 0)

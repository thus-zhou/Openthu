"""Build complete source archives from committed, allowlisted Git files."""
from __future__ import annotations
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    version=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    names=[x.decode('utf-8') for x in subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).split(b'\0') if x]
    if not names:raise RuntimeError('Commit the release source first')
    dirty=subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True)
    if dirty:raise RuntimeError('Release requires a clean tracked source tree')
    subprocess.run([sys.executable,'scripts/privacy_scan.py'],cwd=ROOT,check=True)
    out=ROOT/'dist';out.mkdir(exist_ok=True)
    prefix=f'Openthu-{version}'
    zip_path=out/f'{prefix}-source.zip'
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(names):
            info=zipfile.ZipInfo(prefix+'/'+name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,(ROOT/name).read_bytes())
    tar_path=out/f'{prefix}-source.tar.gz'
    with tar_path.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as zipped,tarfile.open(fileobj=zipped,mode='w') as archive:
        for name in sorted(names):
            data=(ROOT/name).read_bytes();info=tarfile.TarInfo(prefix+'/'+name);info.size=len(data)
            info.mtime=0;info.uid=info.gid=0;info.uname=info.gname='';info.mode=0o644
            archive.addfile(info,io.BytesIO(data))
    artifacts=[zip_path,tar_path,*sorted(out.glob('*.whl')),out/f'openthu-{version}.tar.gz']
    artifacts=[p for p in dict.fromkeys(artifacts) if p.is_file()]
    sums=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in artifacts)
    (out/'SHA256SUMS').write_text(sums,encoding='ascii')
    subprocess.run([sys.executable,'scripts/privacy_scan.py',*[str(p) for p in artifacts]],cwd=ROOT,check=True)
    print(json.dumps({'version':version,'source_files':len(names),'artifacts':[p.name for p in artifacts]}))


if __name__=='__main__':main()

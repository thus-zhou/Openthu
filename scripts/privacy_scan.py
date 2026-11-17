"""Scan public source or archives. Findings report locations, never secret values."""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXCLUDED={'.git','.venv','node_modules','__pycache__','.pytest_cache','.local','.state','build','dist','artifacts','logs'}
FORBIDDEN={'creds.json','credentials.json','cookies.json','session.json','token.json','auth.json','hosts.yml'}
SYNTHETIC_POSIX_USERS={
    'synthetic','synthetic-private','synthetic-marker','example','alice','someone','user','web',
}
PATTERNS={
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github-token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'api-token':re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
    'aws-access-key':re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    'jwt':re.compile(r'\beyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{18,}\b'),
    'windows-user-path':re.compile(r'(?i)[a-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|example\b|user\b)[^\s"\'<>/\\]+'),
    'posix-user-path':re.compile(r'(?<![\w.-])/(?:home|Users)/([^/\s"\'<>\[\]]+)'),
    'private-host-ip':re.compile(r'(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])'),
}


def findings(name: str,data: bytes):
    result=[]
    base=PurePosixPath(name).name.lower()
    if (base in FORBIDDEN or base=='.env' or (base.startswith('.env.') and base!='.env.example')
            or base.endswith(('.dpapi','.pem','.key','.p12','.pfx','.sqlite','.db'))):
        result.append({'file':name,'line':0,'rule':'sensitive-file'})
    if b'\0' in data[:8192]:return result
    try:text=data.decode('utf-8-sig')
    except UnicodeDecodeError:return result
    for rule,pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if rule=='posix-user-path' and match.group(1).casefold() in SYNTHETIC_POSIX_USERS:
                continue
            result.append({'file':name,'line':text.count('\n',0,match.start())+1,'rule':rule})
    return result


def source_files():
    if (ROOT/'.git').is_dir():
        result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True)
        files=[ROOT/p.decode('utf-8') for p in result.stdout.split(b'\0') if p]
        if files:
            return files
    return [p for p in ROOT.rglob('*') if p.is_file() and not any(x in EXCLUDED or x.endswith('.egg-info') for x in p.relative_to(ROOT).parts)]


def archive_files(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if not item.is_dir():yield item.filename,archive.read(item)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for item in archive.getmembers():
                if item.issym() or item.islnk():
                    raise ValueError('Release archives must not contain symbolic links')
                if item.isfile():yield item.name,archive.extractfile(item).read()
    else:raise ValueError('Expected a ZIP, wheel or tar archive')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives',type=Path,nargs='*')
    args=parser.parse_args()
    issues=[];count=0
    sources=((p.relative_to(ROOT).as_posix(),p.read_bytes()) for p in source_files()) if not args.archives else (
        (path.name+'/'+name,data) for path in args.archives for name,data in archive_files(path))
    for name,data in sources:
        count+=1;issues.extend(findings(name,data))
    print(json.dumps({'ok':not issues,'files_checked':count,'findings':issues},ensure_ascii=True))
    return 1 if issues else 0


if __name__=='__main__':raise SystemExit(main())

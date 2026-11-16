"""Record a manual handoff of an already committed task. Does not invoke AI."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT,text=True,encoding='utf-8').strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task_id')
    parser.add_argument('--to',choices=['codex','hermes'],required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.task_id):
        parser.error('Use a short task ID with letters, digits, dash or underscore')
    note=ROOT/'handoffs'/f'{args.task_id}.md'
    if not note.is_file() or note.read_text(encoding='utf-8')==(ROOT/'handoffs/TEMPLATE.md').read_text(encoding='utf-8'):
        parser.error('Create and fill the task note before recording a checkpoint')
    if git('status','--porcelain'):
        parser.error('Commit code and task note first; checkpoint must identify a clean code version')
    state={'schema':1,'task_id':args.task_id,'status':f'READY_FOR_{args.to.upper()}',
           'next_owner':args.to,'branch':git('branch','--show-current'),
           'code_commit':git('rev-parse','HEAD'),'created_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'context_file':f'handoffs/{args.task_id}.md','dispatch':'manual'}
    output=note.with_suffix('.json')
    output.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(state))
    print('Commit the checkpoint, push the branch, and give the PR URL to the user. Stop editing this task.')


if __name__=='__main__':main()

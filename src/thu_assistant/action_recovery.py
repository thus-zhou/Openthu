"""Offline journal inventory retained with block-5 runtime during a rollback.

Only metadata is shown. This command never dispatches, queries or cancels a
campus service. Use get_action_result from the retained runtime plus a verified
site adapter for actual reconciliation. Do not remove pending DPAPI journals
during rollback: deleting a local record does not undo a remote business action.
"""
import argparse,json
from .action_store import ActionStore
from .actions import effect_rows

def inventory(store):
    results=[]
    for path in sorted(store.root.glob('*.dpapi')):
        record=store.read(path.stem)
        results.append({k:record[k] for k in ('operation_id','state','revision','service_id','operation','adapter_version')}
            | {'effects':effect_rows(record),'pending':record['state'] in {'AWAITING_OFFICIAL','DISPATCHING','UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL'}})
    return {'offline':True,'dispatched':False,'remote_verified_now':False,'cancellation':'NOT_PERFORMED',
            'read_only':True,'operations':results}

def main():
    parser=argparse.ArgumentParser(description='Read-only DPAPI action journal inventory; no network or submission.')
    parser.add_argument('--state-dir',help='Exact actions-v1 directory; defaults to current application state')
    args=parser.parse_args()
    try:print(json.dumps(inventory(ActionStore(args.state_dir)),ensure_ascii=False));return 0
    except Exception as error:
        print(json.dumps({'ok':False,'error':getattr(error,'code',type(error).__name__)}));return 1

if __name__=='__main__':raise SystemExit(main())

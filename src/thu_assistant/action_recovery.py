"""Offline journal inventory retained with block-5 runtime during a rollback.

Only metadata is shown. This command never dispatches, queries or cancels a
campus service. Use get_action_result from the retained runtime plus a verified
site adapter for actual reconciliation. Do not remove pending DPAPI journals
during rollback: deleting a local record does not undo a remote business action.

One unreadable journal (corruption, a file from another Windows user, an ACL
change) must not hide every other pending transaction.  The inventory reports
each unreadable operation id and error category and keeps scanning.
"""
import argparse,json
from .action_store import ActionStore
from .actions import effect_rows
from .bridge import CampusError

def inventory(store):
    results=[];errors=[]
    for path in sorted(store.root.glob('*.dpapi')):
        try:record=store.read(path.stem)
        except CampusError as error:
            errors.append({'operation_id':path.stem,'error':error.code});continue
        except Exception:
            errors.append({'operation_id':path.stem,'error':'STATE_READ_FAILED'});continue
        results.append({k:record[k] for k in ('operation_id','state','revision','service_id','operation','adapter_version')}
            | {'effects':effect_rows(record),'pending':record['state'] in {'AWAITING_OFFICIAL','DISPATCHING','UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL'}})
    return {'ok':True,'offline':True,'dispatched':False,'remote_verified_now':False,'cancellation':'NOT_PERFORMED',
            'read_only':True,'readable':len(results),'unreadable':len(errors),'errors':errors,'operations':results}

def main():
    parser=argparse.ArgumentParser(description='Read-only DPAPI action journal inventory; no network or submission.')
    parser.add_argument('--state-dir',help='Exact actions-v1 directory; defaults to current application state')
    args=parser.parse_args()
    try:report=inventory(ActionStore(args.state_dir))
    except CampusError as error:
        print(json.dumps({'ok':False,'error':error.code},ensure_ascii=False));return 1
    except Exception:
        print(json.dumps({'ok':False,'error':'STATE_READ_FAILED'},ensure_ascii=False));return 1
    print(json.dumps(report,ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())

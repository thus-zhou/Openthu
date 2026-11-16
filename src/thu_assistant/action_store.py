"""Atomic DPAPI journals. Locks protect only local transactions, never network I/O."""
from contextlib import contextmanager
import json,os,re,time,uuid
from pathlib import Path
from learn_assistant.auth import _dpapi_protect,_dpapi_unprotect
from .bridge import CampusError
from .catalog import state_root

STATES={'PREPARED','NEEDS_USER_ACTION','AWAITING_OFFICIAL','DISPATCHING','PENDING_PAYMENT','PENDING_APPROVAL','SUCCEEDED','FAILED','UNKNOWN'}

class ActionStore:
    def __init__(self,root=None,protect=_dpapi_protect,unprotect=_dpapi_unprotect):
        self.root=Path(root) if root else state_root()/'actions-v1'
        self.protect=protect;self.unprotect=unprotect
    def path(self,operation_id):
        if not isinstance(operation_id,str) or not re.fullmatch('[a-f0-9]{32}',operation_id):raise CampusError('INVALID_OPERATION_ID')
        return self.root/(operation_id+'.dpapi')
    @contextmanager
    def lock(self,name):
        self.root.mkdir(parents=True,exist_ok=True)
        file=open(self.root/(name+'.lock'),'a+b');file.seek(0,2)
        if not file.tell():file.write(b'0');file.flush()
        deadline=time.monotonic()+3;held=False
        try:
            while not held:
                try:
                    file.seek(0)
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    held=True
                except OSError:
                    if time.monotonic()>=deadline:raise CampusError('ACTION_STORE_BUSY') from None
                    time.sleep(.05)
            yield
        finally:
            if held:
                file.seek(0)
                if os.name=='nt':msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(file,fcntl.LOCK_UN)
            file.close()
    def read(self,oid):
        try:blob=self.path(oid).read_bytes()
        except FileNotFoundError:raise CampusError('ACTION_NOT_FOUND') from None
        if not blob.startswith(b'THU-ACTION-1\0'):raise CampusError('ACTION_LOG_CORRUPT')
        try:raw=self.unprotect(blob[len(b'THU-ACTION-1\0'):])
        except Exception:raise CampusError('ACTION_LOG_DECRYPT_FAILED') from None
        if raw is None:raise CampusError('ACTION_LOG_DECRYPT_FAILED')
        try:record=json.loads(raw)
        except Exception:raise CampusError('ACTION_LOG_CORRUPT') from None
        if record.get('version')!=1 or record.get('operation_id')!=oid or record.get('state') not in STATES or type(record.get('revision')) is not int:
            raise CampusError('ACTION_LOG_CORRUPT')
        return record
    def write(self,record):
        target=self.path(record['operation_id']);self.root.mkdir(parents=True,exist_ok=True)
        try:blob=self.protect(json.dumps(record,ensure_ascii=False,sort_keys=True).encode())
        except Exception:raise CampusError('ACTION_LOG_ENCRYPT_FAILED') from None
        if not blob:raise CampusError('ACTION_LOG_ENCRYPT_FAILED')
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with open(temporary,'xb') as f:f.write(b'THU-ACTION-1\0'+blob);f.flush();os.fsync(f.fileno())
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
    def create(self,record):
        with self.lock('index'):
            for file in self.root.glob('*.dpapi'):
                old=self.read(file.stem)
                if old['account']==record['account'] and old['service_id']==record['service_id'] and old['operation']==record['operation'] and old['digest']==record['digest']:
                    if old['state'] in {'AWAITING_OFFICIAL','DISPATCHING','UNKNOWN','PENDING_PAYMENT','PENDING_APPROVAL','SUCCEEDED'} or old['state'] in {'PREPARED','NEEDS_USER_ACTION'} and old['expires_at']>time.time() and old['context']==record['context'] and old['adapter_version']==record['adapter_version']:return old
            if self.path(record['operation_id']).exists():raise CampusError('ACTION_ID_CONFLICT')
            self.write(record);return record
    def update(self,oid,change):
        self.path(oid)
        with self.lock('index'),self.lock(oid):
            record=self.read(oid);change(record);record['revision']+=1;self.write(record);return record

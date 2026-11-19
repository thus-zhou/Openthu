"""One exact renewal POST, guarded by a single-use request gate.

The request is only built from an explicitly supplied :class:`SourceContract`.
The guard hashes the actual transmitted body, checks origin, method and main
frame, and consumes exactly one gate capability; a second attempt is aborted.

Failure classification matters for writes:

* a local ``_PREPARE`` failure (origin/auth/crypto) or an aborted guard means
  the request definitely never left the page -> ``NotDispatched``;
* a resolved ``fetch`` that the guard never observed (a Service Worker can take
  the request without ``page.route`` seeing it) is *not* proof of non-delivery
  -> a plain exception, so the action engine records ``UNKNOWN`` and never
  replays it.
"""
from hashlib import sha256
import json
import re
from urllib.parse import urlencode

from ..actions import NotDispatched, RequestSpec

_PREPARE = """async ({url,body,encoding}) => {
 const u=new URL(url);
 if(u.origin!==location.origin)throw Error('ORIGIN');
 const user=JSON.parse(localStorage.getItem('userInfo')||'{}');
 const token=localStorage.getItem('primoExploreJwt');
 if(!user?.accountid||!token)throw Error('AUTH');
 const options={method:'POST',redirect:'error',credentials:'same-origin',
   headers:{PrimoAuthorization:'Bearer '+token,
     'Content-Type':encoding==='form'?'application/x-www-form-urlencoded; charset=UTF-8':'application/json'}};
 options.body=encoding==='form'?new URLSearchParams(body).toString():JSON.stringify(body);
 window.__thuRenewalSingle={url,options};
 const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(options.body));
 return Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');
}"""
_SEND = """async () => {
 const p=window.__thuRenewalSingle;delete window.__thuRenewalSingle;
 const r=await fetch(p.url,{...p.options,signal:AbortSignal.timeout(20000)});
 if(!r.ok)return {ok:false,status:r.status};
 try{return {ok:true,payload:await r.json()};}
 catch(error){return {ok:false,status:r.status};}
}"""


def specification(contract, loan_id, due_at):
    body = contract.body(loan_id, due_at)
    if contract.encoding == 'form':
        return RequestSpec(contract.url, 'POST', urlencode(body), encoding='form')
    return RequestSpec(contract.url, 'POST', json.dumps(body, ensure_ascii=False, separators=(',', ':')),
                       encoding='json')


def dispatch_exact(reader, contract, loan_id, due_at, gate, allow_synthetic=False):
    if not contract.usable(allow_synthetic):
        raise NotDispatched()
    spec = specification(contract, loan_id, due_at)
    try:
        checksum = reader.page.evaluate(_PREPARE, {'url': spec.url,
                                                   'body': contract.body(loan_id, due_at),
                                                   'encoding': contract.encoding})
    except Exception:
        # Nothing was handed to the transport yet.
        raise NotDispatched() from None
    state = {'observed': 0, 'aborted': 0}
    pattern = re.compile('^' + re.escape(spec.url) + '$')

    def route_once(route):
        state['observed'] += 1
        request = route.request
        if request.frame != reader.page.main_frame or request.url != spec.url or request.method != 'POST':
            state['aborted'] += 1
            return route.abort()
        transmitted = request.post_data or ''
        if sha256(transmitted.encode()).hexdigest() != checksum:
            state['aborted'] += 1
            return route.abort()
        if gate.consume(spec.url, request.method, transmitted):
            return route.continue_()
        state['aborted'] += 1
        return route.abort()

    reader.page.route(pattern, route_once)
    try:
        result = reader.page.evaluate(_SEND, None)
    except Exception:
        if gate.sent == 0 and state['observed'] and state['aborted']:
            raise NotDispatched() from None
        # The page may have reached the network (or a Service Worker) before the
        # failure; never let the engine call this a definite non-delivery.
        raise
    finally:
        reader.page.unroute(pattern, route_once)
    if gate.sent != 1:
        # fetch resolved without the guard observing it: delivery is unknown.
        raise RuntimeError('DISPATCH_RESULT_UNOBSERVED')
    if not isinstance(result, dict) or result.get('ok') is not True:
        return False
    return contract.accepted(result.get('payload'))

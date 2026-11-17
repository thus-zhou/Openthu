"""One exact renewal POST, guarded by a single-use request gate.

The request is only built from an explicitly supplied :class:`SourceContract`.
The guard hashes the actual transmitted body, checks origin, method and main
frame, and consumes exactly one gate capability; a second attempt is aborted.
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
_SEND = """async ({field,value}) => {
 const p=window.__thuRenewalSingle;delete window.__thuRenewalSingle;
 const r=await fetch(p.url,{...p.options,signal:AbortSignal.timeout(20000)});
 if(!r.ok)return false;
 const d=await r.json();
 let node=d;
 for(const part of String(field).split('.')){
   if(!node||typeof node!=='object'||!(part in node))return false;
   node=node[part];
 }
 return node===value&&typeof node!=='boolean';
}"""


def specification(contract, loan_id, due_at):
    body = contract.body(loan_id, due_at)
    if contract.encoding == 'form':
        return RequestSpec(contract.url, 'POST', urlencode(body), encoding='form')
    return RequestSpec(contract.url, 'POST', json.dumps(body, ensure_ascii=False, separators=(',', ':')),
                       encoding='json')


def dispatch_exact(reader, contract, loan_id, due_at, gate):
    spec = specification(contract, loan_id, due_at)
    checksum = reader.page.evaluate(_PREPARE, {'url': spec.url,
                                               'body': contract.body(loan_id, due_at),
                                               'encoding': contract.encoding})
    pattern = re.compile('^' + re.escape(spec.url) + '$')

    def route_once(route):
        request = route.request
        if request.frame != reader.page.main_frame or request.url != spec.url or request.method != 'POST':
            return route.abort()
        transmitted = request.post_data or ''
        if sha256(transmitted.encode()).hexdigest() != checksum:
            return route.abort()
        if gate.consume(spec.url, request.method, transmitted):
            return route.continue_()
        return route.abort()

    reader.page.route(pattern, route_once)
    try:
        accepted = reader.page.evaluate(_SEND, {'field': contract.success_field,
                                                'value': contract.success_value})
    finally:
        reader.page.unroute(pattern, route_once)
    if gate.sent != 1:
        raise NotDispatched()
    return accepted is True

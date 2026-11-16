"""Read-only Python port of thu-learn-lib 4.2.6 field mappings (MIT).

Copyright (c) 2019-2022 Harry Chen. See vendor/thu-learn-lib/LICENSE and
PROVENANCE.json for exact upstream revision, excerpts, hashes and modifications.
No upstream authentication, submission or upload implementation is imported.
"""
from __future__ import annotations
import base64,html,json,re
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import urlsplit,parse_qs,urljoin,urlencode
from ..errors import InterfaceDriftError,ValidationError

class Notice(TypedDict):
    id:str
    course_id:str
    title:str
    content_html:str|None
    content_text:str|None
    published_at:str|None
    expired:bool
    body_state:str
    attachments:list[dict]
    attachments_state:str
    attachment_hint:bool

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):raise ValidationError('INVALID_LEARN_IDENTIFIER')
    return value

class SafeHTML(HTMLParser):
    """Keep inert formatting only. Never return scripts, forms, URLs or handlers."""
    tags={'p','br','div','span','ul','ol','li','table','tbody','tr','th','td','strong','b','em','i','h1','h2','h3','pre','code','blockquote'}
    def __init__(self):super().__init__(convert_charrefs=True);self.out=[];self.text=[];self.ignored=0;self.links=[];self.anchor=None
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','template'}:self.ignored+=1
        if self.ignored:return
        if tag in self.tags:self.out.append('<'+tag+'>')
        if tag in {'p','div','br','li','tr'}:self.text.append('\n')
        if tag=='a':self.anchor={'href':dict(attrs).get('href',''),'text':''}
    def handle_endtag(self,tag):
        if tag in {'script','style','template'}:self.ignored=max(0,self.ignored-1);return
        if self.ignored:return
        if tag in self.tags and tag!='br':self.out.append('</'+tag+'>')
        if tag=='a' and self.anchor is not None:self.links.append(self.anchor);self.anchor=None
    def handle_data(self,data):
        if self.ignored:return
        self.out.append(html.escape(data));self.text.append(data)
        if self.anchor is not None:self.anchor['text']+=data

def render(value):
    p=SafeHTML();p.feed(value or '')
    return ''.join(p.out),''.join(p.text).strip()

def parse_notice(raw,course_id,expired):
    nid=identifier(str(raw.get('ggid') or raw.get('id') or ''))
    value=raw.get('ggnr');body=None
    if value is not None:
        try:body=base64.b64decode(value,validate=True).decode('utf-8')
        except Exception:raise InterfaceDriftError('NOTICE_BODY_ENCODING_CHANGED') from None
    safe,text=render(html.unescape(body)) if body is not None else (None,None)
    return Notice(id=nid,course_id=course_id,title=html.unescape(str(raw.get('bt') or '')),
        content_html=safe,content_text=text,published_at=raw.get('fbsj') or raw.get('fbsjStr'),expired=expired,
        body_state='READ' if body is not None else 'UNKNOWN',attachments=[],attachments_state='NOT_READ',attachment_hint=bool(raw.get('fjmc')))

def attachment_links(page,course_id,source_kind,source_id):
    p=SafeHTML();p.feed(page);out=[];seen=set()
    for link in p.links:
        u=urlsplit(urljoin('https://learn.tsinghua.edu.cn',link['href']));q=parse_qs(u.query)
        outer_id=(q.get('wjid') or q.get('fileId') or [None])[0]
        if 'downloadUrl' in q:u=urlsplit(urljoin('https://learn.tsinghua.edu.cn',q['downloadUrl'][0]));q=parse_qs(u.query)
        match=re.fullmatch(r'/b/wlxt/kczy/zy/student/downloadFile/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)',u.path)
        fid=(q.get('wjid') or q.get('fileId') or [match[2] if match else None])[0]
        if not fid:continue
        # Only known Learn download handlers, never arbitrary links or callbacks.
        if u.scheme!='https' or u.hostname!='learn.tsinghua.edu.cn' or u.port not in (None,443) or u.username or u.password:continue
        if match:
            if match[1]!=course_id or outer_id not in (None,fid):continue
        elif not re.fullmatch(r'/b/wlxt/(?:kc/wj_wjb|kj/wlkc_kjxxb|kcgg/wlkc_ggb|kczy/zy)/(?:student/)?downloadFile',u.path):continue
        identifier(fid)
        if fid in seen:continue
        seen.add(fid)
        key='wjid' if 'wjid' in q else 'fileId'
        path=u.path if match else u.path+'?'+urlencode({key:fid,'sfgk':'0'})
        out.append({'file_id':f'attachment:{source_kind}:{course_id}:{source_id}:{fid}',
            'remote_file_id':fid,'name':link['text'].strip() or '附件','course_id':course_id,
            'source_kind':source_kind,'source_id':source_id,'download_state':'NOT_DOWNLOADED','path':path})
    return out

def parse_jsonp(text,callback='thuCalendar'):
    match=re.fullmatch(r'\s*'+re.escape(callback)+r'\s*\((.*)\)\s*;?\s*',text,re.S)
    if not match:raise InterfaceDriftError('CALENDAR_JSONP_INVALID')
    try:data=json.loads(match[1])
    except Exception:raise InterfaceDriftError('CALENDAR_JSONP_INVALID') from None
    if not isinstance(data,list) or any(not isinstance(x,dict) for x in data):raise InterfaceDriftError('CALENDAR_SHAPE_CHANGED')
    return data

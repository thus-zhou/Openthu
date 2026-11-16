"""Expanded read adapter. Uses WebProvider transport/session; no password store.

Mappings ported from thu-learn-lib 4.2.6 (MIT), exact revision in vendor manifest.
"""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime,date,timedelta,timezone
import hashlib,json,re,os
from urllib.parse import urlencode
from .web import WebProvider,_decode_text,_first,_HomeworkDetailParser
from .learn_read_parsers import identifier,parse_notice,attachment_links,parse_jsonp,render
from ..errors import AuthenticationRequired,InterfaceDriftError,ValidationError,ProviderNotConfigured
from ..models import Course,CourseFile,to_dict

TZ=timezone(timedelta(hours=8))
LEARN='https://learn.tsinghua.edu.cn'
REGISTRAR='https://zhjw.cic.tsinghua.edu.cn'

class LearnReadProvider(WebProvider):
    name='web'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.read_evidence={};self._attachment_paths={}
    def begin(self):
        self._require_session()
        self.read_evidence={'complete':True,'warnings':[],'errors':[],'pages':[],
            'checked_at':datetime.now(TZ).isoformat(),'adapter_version':'learn-read-v1'}
    def list_courses(self,semester=None,query=None):
        if semester is not None:identifier(semester)
        return super().list_courses(semester,query)
    def warn(self,code,incomplete=True):
        if code not in self.read_evidence['warnings']:self.read_evidence['warnings'].append(code)
        if incomplete:self.read_evidence['complete']=False
    def failure(self,scope,error):
        if isinstance(error,AuthenticationRequired):raise error
        self.warn('PARTIAL_SOURCE_FAILURE')
        self.read_evidence['errors'].append({'scope':scope,'code':getattr(error,'code',type(error).__name__)})
    def success_json(self,response,endpoint):
        value=self._json(response,endpoint)
        if not isinstance(value,dict) or value.get('result')!='success':raise InterfaceDriftError('LEARN_SUCCESS_MARKER_MISSING')
        return value
    def table(self,endpoint,course_id):
        """DataTables offsets/counts, including detection of ignored pagination."""
        identifier(course_id);out=[];seen=set();expected=None
        for offset in range(0,10000,100):
            ao=[{'name':'wlkcid','value':course_id},{'name':'iDisplayStart','value':offset},{'name':'iDisplayLength','value':100}]
            try:payload=self.success_json(self._request('POST',endpoint,data={'aoData':json.dumps(ao)}),endpoint)
            except Exception as error:
                if not out:raise
                self.failure(course_id+':'+endpoint+':'+str(offset),error);break
            obj=payload.get('object')
            if not isinstance(obj,dict):raise InterfaceDriftError('LEARN_TABLE_MISSING')
            rows=obj.get('aaData',obj.get('resultsList'))
            if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):raise InterfaceDriftError('LEARN_ROWS_INVALID')
            total=obj.get('iTotalDisplayRecords',obj.get('iTotalRecords'))
            if total is not None:
                try:total=int(total)
                except (ValueError,TypeError):raise InterfaceDriftError('LEARN_TOTAL_INVALID') from None
                if total<0:raise InterfaceDriftError('LEARN_TOTAL_INVALID')
                if expected is not None and total!=expected:self.warn('UPSTREAM_TOTAL_CHANGED')
                expected=total
            self.read_evidence['pages'].append({'endpoint':endpoint,'course_id':course_id,'offset':offset,'rows':len(rows),'total':total})
            fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
            if rows and fingerprint in seen:self.warn('REPEATED_UPSTREAM_PAGE');break
            seen.add(fingerprint);out.extend(rows)
            if total is None:self.warn('UPSTREAM_TOTAL_UNVERIFIED');break
            if len(out)>=total:
                if len(out)!=total:self.warn('UPSTREAM_COUNT_MISMATCH')
                break
            if not rows:self.warn('MISSING_UPSTREAM_PAGE');break
            # Offset uses the actual delivered row count, including small server caps.
            if len(rows)!=100 and len(out)<total:self.warn('UNEXPECTED_SERVER_PAGE_SIZE');break
        else:self.warn('UPSTREAM_PAGE_LIMIT')
        return out
    def semesters(self):
        data=self._json(self._request('GET','/b/wlxt/kc/v_wlkc_xs_xktjb_coassb/queryxnxq'),'semesters')
        if not isinstance(data,list):raise InterfaceDriftError('SEMESTERS_SHAPE_CHANGED')
        current=self._current_semester();seen=set();items=[]
        for value in data:
            if value is None:continue # Explicit upstream v4.2.6 compatibility.
            identifier(value)
            if value in seen:self.warn('DUPLICATE_SEMESTERS',False);continue
            seen.add(value);items.append({'id':value,'current':value==current})
        return items
    def courses_for(self,parameters):
        semester=parameters.get('semester')
        if semester:identifier(semester)
        if parameters.get('course_id'):
            cid=identifier(parameters['course_id'])
            return [Course(cid,'',semester or '')],None
        courses=self.list_courses(semester=semester)
        name=parameters.get('course_name')
        if name:
            courses=[c for c in courses if c.name==name]
            if len(courses)!=1:
                return [],{'status':'NEEDS_USER_ACTION','reason':'AMBIGUOUS_COURSE' if courses else 'COURSE_NOT_FOUND',
                    'candidates':to_dict(courses),'complete':False,'items':[]}
        return courses,None
    def notices_for(self,course):
        notices={}
        for expired,suffix in ((False,'Wgq'),(True,'Ygq')):
            try:
                rows=self.table('/b/wlxt/kcgg/wlkc_ggb/student/pageListXsby'+suffix,course.id)
                for raw in rows:
                    if raw.get('wlkcid') not in (None,'',course.id):raise InterfaceDriftError('NOTICE_COURSE_MISMATCH')
                    item=parse_notice(raw,course.id,expired);item['course_name']=course.name;item['semester']=course.semester
                    if item['body_state']!='READ':self.warn('NOTICE_BODY_UNAVAILABLE')
                    if item['id'] in notices:self.warn('DUPLICATE_NOTICE_ID');continue
                    notices[item['id']]=item
            except Exception as error:self.failure(course.id+':notices:'+suffix,error)
        return list(notices.values())
    def register_attachments(self,page,course,kind,source_id):
        items=attachment_links(page,course.id,kind,source_id)
        for item in items:
            path=item.pop('path');key=item['file_id'];self._attachment_paths[key]=path
            self._file_cache[key]=CourseFile(key,course.id,course.name,item['name'],
                f'{kind}/{source_id}/{item["name"]}',0,source_kind=kind,source_id=source_id)
        return items
    def notice_detail(self,course,nid):
        identifier(nid)
        matches=[n for n in self.notices_for(course) if n['id']==nid]
        if not matches:raise ValidationError('NOTICE_NOT_FOUND_IN_COURSE')
        item=matches[0]
        page=self._request('GET','/f/wlxt/kcgg/wlkc_ggb/student/beforeViewXs',params={'wlkcid':course.id,'id':nid})
        item['attachments']=self.register_attachments(page.text,course,'notice',nid)
        item['attachments_state']='LIST_READ'
        if item['attachment_hint'] and not item['attachments']:
            item['attachments_state']='UNRESOLVED';self.warn('NOTICE_ATTACHMENT_NOT_RESOLVED')
        return item
    def _assignment_from_raw(self,raw,course_id,course_name,*,submitted,graded):
        item=super()._assignment_from_raw(raw,course_id,course_name,submitted=submitted,graded=graded)
        overdue=None
        if item.deadline:
            try:
                dt=datetime.fromisoformat(item.deadline.replace('Z','+00:00'))
                if not dt.tzinfo:dt=dt.replace(tzinfo=TZ)
                overdue=not submitted and dt<datetime.now(TZ)
            except ValueError:pass
        return replace(item,submitted=submitted,graded=graded,overdue=overdue,
            attachments_hint=bool(raw['zyfjid']) if 'zyfjid' in raw else None,
            feedback=_decode_text(raw.get('pynr')) if raw.get('pynr') is not None else None,
            graded_at=raw.get('pysj') or raw.get('pysjStr'),grade_level=raw.get('djzcj') or raw.get('ywdjzcj'))
    def assignments_for(self,course):
        out={}
        for suffix,submitted,graded in (('zyListWj',False,False),('zyListYjwg',True,False),('zyListYpg',True,True)):
            try:
                rows=self.table('/b/wlxt/kczy/zy/student/'+suffix,course.id)
                for raw in rows:
                    if raw.get('wlkcid') not in (None,'',course.id):raise InterfaceDriftError('ASSIGNMENT_COURSE_MISMATCH')
                    item=self._assignment_from_raw(raw,course.id,course.name,submitted=submitted,graded=graded)
                    if item.id in out:self.warn('ASSIGNMENT_STATUS_CHANGED_DURING_READ')
                    out[item.id]=item;self._assignment_cache[item.id]=item
            except Exception as error:self.failure(course.id+':'+suffix,error)
        return list(out.values())
    def list_assignments(self,course_id=None):
        self.begin();courses,_=self.courses_for({'course_id':course_id} if course_id else {})
        return [item for course in courses for item in self.assignments_for(course)]
    def get_assignment(self,assignment_id):
        self.begin();assignment_id=identifier(assignment_id)
        known=self._find_assignment(assignment_id)
        fresh=self.assignments_for(Course(known.course_id,known.course_name,''))
        match=[x for x in fresh if x.id==known.id]
        if not match:raise ValidationError('ASSIGNMENT_NOT_FOUND_IN_CURRENT_LIST')
        item=match[0]
        page=self._request('GET','/f/wlxt/kczy/zy/student/viewCj',params={'wlkcid':item.course_id,'xszyid':item.id})
        parser=_HomeworkDetailParser();parser.feed(page.text);details=dict(parser.records)
        payload=self.success_json(self._request('POST','/b/wlxt/kczy/zy/student/detail',data={'id':item.base_id}),'assignment_detail')
        instructions=_decode_text(payload.get('msg')) or details.get('作业说明','')
        body_state='READ' if payload.get('msg') is not None or '作业说明' in details else 'UNKNOWN'
        if body_state=='UNKNOWN':self.warn('ASSIGNMENT_BODY_UNAVAILABLE')
        files=self.register_attachments(page.text,Course(item.course_id,item.course_name,''),'assignment',item.id)
        state='UNRESOLVED' if item.attachments_hint and not files else 'LIST_READ'
        if state=='UNRESOLVED':self.warn('ASSIGNMENT_ATTACHMENT_NOT_RESOLVED')
        item=replace(item,instructions=instructions,instructions_state=body_state,attachments=tuple(files),attachments_state=state,
            late_submission_deadline=item.late_submission_deadline or details.get('补交截止时间') or None)
        self._assignment_cache[item.id]=item;return item
    def files_for(self,course,query=None):
        """Read each category (uncapped endpoint), retain the main list as a union."""
        out={};categories=[];seen_pages=set()
        for page in range(1,101):
            p=self.success_json(self._request('GET','/b/wlxt/kj/wlkc_kjflb/student/pageList',params={'wlkcid':course.id,'page':page,'rows':100}),'categories')['object']
            if not isinstance(p,dict) or not isinstance(p.get('rows'),list):raise InterfaceDriftError('FILE_CATEGORIES_INVALID')
            rows=p['rows'];finger=json.dumps(rows,sort_keys=True)
            if rows and finger in seen_pages:self.warn('REPEATED_CATEGORY_PAGE');break
            seen_pages.add(finger);categories.extend(rows)
            total=p.get('records')
            if total is None:self.warn('CATEGORY_TOTAL_UNVERIFIED');break
            if len(categories)>=int(total):break
            if not rows:self.warn('MISSING_CATEGORY_PAGE');break
        else:self.warn('CATEGORY_PAGE_LIMIT')
        for category in categories:
            cid=identifier(str(category.get('kjflid') or category.get('id') or ''))
            raw=self.success_json(self._request('GET',f'/b/wlxt/kj/wlkc_kjxxb/student/kjxxb/{course.id}/{cid}'),'category_files')['object']
            if not isinstance(raw,list):raise InterfaceDriftError('CATEGORY_FILES_INVALID')
            for row in raw:
                if not isinstance(row,list) or len(row)<14:raise InterfaceDriftError('CATEGORY_FILE_ROW_CHANGED')
                fid=identifier(str(row[7]));name=_decode_text(row[1])
                out[fid]=CourseFile(fid,course.id,course.name,name,f'{_decode_text(category.get("bt"))}/{name}',int(row[9] or 0),str(row[13] or 'application/octet-stream'),row[6])
        payload=self.success_json(self._request('GET','/b/wlxt/kj/wlkc_kjxxb/student/kjxxbByWlkcidAndSizeForStudent',params={'wlkcid':course.id,'size':200}),'files')['object']
        raw=payload if isinstance(payload,list) else payload.get('resultsList') if isinstance(payload,dict) else None
        if not isinstance(raw,list):raise InterfaceDriftError('FILES_SHAPE_CHANGED')
        if len(raw)>=200:self.warn('UNCATEGORIZED_FILES_COMPLETENESS_UNKNOWN')
        for row in raw:
            fid=identifier(str(row.get('wjid') or row.get('id') or ''));name=_decode_text(row.get('bt'))
            if fid not in out:out[fid]=CourseFile(fid,course.id,course.name,name,f'未分类/{name}',int(row.get('wjdx') or 0),str(row.get('wjlx') or 'application/octet-stream'),row.get('scsj'))
        self._file_cache.update(out)
        return [x for x in out.values() if not query or query.casefold() in (x.name+' '+x.relative_path).casefold()]
    def list_course_files(self,course_id=None,query=None):
        self.begin();courses,_=self.courses_for({'course_id':course_id} if course_id else {});items=[]
        for course in courses:
            try:items.extend(self.files_for(course,query))
            except Exception as error:self.failure(course.id+':files',error)
        return items
    def get_course_file(self,file_id):
        if file_id.startswith('attachment:') and file_id not in self._file_cache:
            parts=file_id.split(':')
            if len(parts)!=5 or parts[1] not in ('notice','assignment'):raise ValidationError('INVALID_ATTACHMENT_ID')
            _,kind,cid,sid,fid=parts
            for x in (cid,sid,fid):identifier(x)
            self.begin();course=Course(cid,'','')
            if kind=='notice':self.notice_detail(course,sid)
            else:
                self.assignments_for(course)
                if sid not in self._assignment_cache:raise ValidationError('ASSIGNMENT_NOT_FOUND_IN_COURSE')
                self.get_assignment(sid)
            if file_id not in self._file_cache:raise ValidationError('ATTACHMENT_NOT_FOUND')
        return super().get_course_file(file_id)
    def download_file(self,file_id):
        if not file_id.startswith('attachment:'):return super().download_file(identifier(file_id))
        self.get_course_file(file_id)
        response=self._request('GET',self._attachment_paths[file_id]);data=bytes(response.content)
        if not data:raise InterfaceDriftError('EMPTY_ATTACHMENT')
        return data
    def calendar(self,start_date,end_date,graduate=False):
        try:start=date.fromisoformat(start_date);end=date.fromisoformat(end_date)
        except ValueError:raise ValidationError('CALENDAR_DATE_INVALID') from None
        if start>end or (end-start).days>366:raise ValidationError('CALENDAR_RANGE_INVALID')
        # Normal official Learn -> registrar SSO; the ticket never enters output.
        response=self._request('POST','/b/wlxt/common/auth/gnt',data={'appId':'ALL_ZHJW'})
        try:ticket=response.json()
        except Exception:
            ticket=response.text.strip()
            if ticket.startswith("'") and ticket.endswith("'"):ticket=ticket[1:-1]
        if not isinstance(ticket,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{16,4096}',ticket):raise InterfaceDriftError('CALENDAR_TICKET_INVALID')
        self._request('GET',REGISTRAR+'/j_acegi_login.do',params={'url':'/','ticket':ticket},add_csrf=False)
        items=[];seen={};cursor=start
        while cursor<=end:
            stop=min(cursor+timedelta(days=27),end)
            try:
                resp=self._request('GET',REGISTRAR+'/jxmh_out.do',params={'m':'yjs_jxrl_all' if graduate else 'bks_jxrl_all',
                    'p_start_date':cursor.strftime('%Y%m%d'),'p_end_date':stop.strftime('%Y%m%d'),'jsoncallback':'thuCalendar'},add_csrf=False)
                rows=parse_jsonp(resp.text)
                self.read_evidence['pages'].append({'start_date':str(cursor),'end_date':str(stop),'rows':len(rows)})
                for row in rows:
                    event_id=row.get('id') or row.get('eventId')
                    event={'id':str(event_id) if event_id is not None else None,'date':row.get('nq'),'start_time':row.get('kssj'),
                        'end_time':row.get('jssj'),'course_name':row.get('nr'),'location':row.get('dd'),'status':row.get('fl'),
                        'source':'registrar_calendar_via_learn','timezone':'Asia/Shanghai'}
                    if not all(row.get(k) is not None for k in ('nq','kssj','jssj','nr')):raise InterfaceDriftError('CALENDAR_EVENT_FIELDS_MISSING')
                    if event_id is None:self.warn('EVENT_ID_UNAVAILABLE_NO_DEDUP',False)
                    elif event['id'] in seen:
                        if seen[event['id']]!=event:self.warn('CONFLICTING_EVENT_ID')
                        continue
                    else:seen[event['id']]=event
                    items.append(event)
            except Exception as error:self.failure(str(cursor)+':'+str(stop),error)
            cursor=stop+timedelta(days=1)
        self.read_evidence['scope']='Selected registrar teaching calendar via Learn SSO; not all teaching or exam arrangements'
        return items
    def query_learning(self,operation,parameters):
        self.begin();items=[]
        if operation=='semesters':items=self.semesters()
        elif operation=='calendar':items=self.calendar(**parameters)
        else:
            courses,choice=self.courses_for(parameters)
            if choice:return {**self.read_evidence,**choice}
            if operation in ('notice_detail','assignment_detail') and len(courses)!=1:raise ValidationError('ONE_COURSE_REQUIRED')
            for course in courses:
                try:
                    if operation=='notices':items.extend(self.notices_for(course))
                    elif operation=='notice_detail':items.append(self.notice_detail(course,parameters['notice_id']))
                    elif operation=='assignments':items.extend(to_dict(self.assignments_for(course)))
                    elif operation=='assignment_detail':
                        self.assignments_for(course);aid=parameters['assignment_id']
                        if aid not in self._assignment_cache or self._assignment_cache[aid].course_id!=course.id:raise ValidationError('ASSIGNMENT_NOT_FOUND_IN_COURSE')
                        evidence=self.read_evidence;item=self.get_assignment(aid);detail=self.read_evidence
                        evidence['complete']=evidence['complete'] and detail['complete']
                        for key in ('warnings','errors','pages'):evidence[key].extend(detail[key])
                        self.read_evidence=evidence;items.append(to_dict(item))
                    elif operation=='files':items.extend(to_dict(self.files_for(course,parameters.get('query'))))
                    else:raise ValidationError('UNREGISTERED_LEARN_QUERY')
                except Exception as error:self.failure(course.id+':'+operation,error)
        return {'items':items,**self.read_evidence}

def create_read_provider():
    return WebProvider() if os.environ.get('THU_LEARN_READ_V2','1')=='0' else LearnReadProvider()

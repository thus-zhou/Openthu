from __future__ import annotations
import argparse
import json
import sys
from .mcp_stdio import CampusAssistant
from .bridge import close_gateway

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description='Openthu 清华校园服务助手')
    p.add_argument('operation',choices=['services','discover','login','read','courses','model'])
    p.add_argument('--query');p.add_argument('--service');p.add_argument('--url');p.add_argument('--prompt')
    p.add_argument('--via-webvpn',action='store_true')
    a=p.parse_args();assistant=CampusAssistant()
    commands={'services':('list_services',{'query':a.query} if a.query else {}),
        'discover':('refresh_service_directory',{}),'login':('login',{'service_id':a.service} if a.service else {}),
        'read':('read_page',{**({'service_id':a.service} if a.service else {'url':a.url}),'via_webvpn':a.via_webvpn}),
        'courses':('list_courses',{}),'model':('model_chat',{'prompt':a.prompt or ''})}
    name,args=commands[a.operation]
    try:print(json.dumps({'ok':True,'result':assistant.call(name,args)},ensure_ascii=False,indent=2));return 0
    except Exception as e:print(json.dumps({'ok':False,'error':{'code':getattr(e,'code',type(e).__name__)}},ensure_ascii=False));return 1
    finally:
        close_gateway()
        assistant.browsers.close_all()

if __name__=='__main__':raise SystemExit(main())

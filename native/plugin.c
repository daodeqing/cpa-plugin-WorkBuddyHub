/* EasyCPA C ABI adapter. No account tokens are stored in this native layer. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#define VERSION "0.1.2"
#define PROVIDER "workbuddy-hub"
#define MAX_PENDING 64

typedef struct { void *ptr; size_t len; } Buffer;
typedef int (*HostCall)(void*,const char*,const uint8_t*,size_t,Buffer*);
typedef void (*FreeBuffer)(void*,size_t);
typedef struct { uint32_t version; void *context; HostCall call; FreeBuffer free_buffer; } HostApi;
typedef struct { uint32_t version; int (*call)(const char*,const uint8_t*,size_t,Buffer*); FreeBuffer free_buffer; void (*shutdown)(void); } PluginApi;
typedef struct Node { cJSON *value; struct Node *next; } Node;
typedef struct { uint64_t id; HANDLE event; Node *head,*tail; } Pending;
extern const unsigned char wbh_payload_start[], wbh_payload_end[];
static HMODULE module;
static HostApi host;
static CRITICAL_SECTION write_lock,pending_lock;
static HANDLE child_process,job,child_input,child_output,reader,ready_event;
static volatile LONG ready_ok;
static volatile LONG shutting_down,active_streams;
static Pending *pending[MAX_PENDING];
static uint64_t serial;
static WCHAR base_dir[32768],runtime_dir[32768],data_dir[32768];

BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID reserved) {
 (void)reserved;
 if(reason==DLL_PROCESS_ATTACH) {
  module=instance; DisableThreadLibraryCalls(instance);
  InitializeCriticalSection(&write_lock); InitializeCriticalSection(&pending_lock);
 }
 return TRUE;
}
static cJSON *obj(void){return cJSON_CreateObject();}
static const char *text(cJSON *v,const char *key){
 cJSON *x=cJSON_GetObjectItemCaseSensitive(v,key);
 return cJSON_IsString(x)?x->valuestring:"";
}
static cJSON *error_value(const char *message,int status){
 cJSON *v=obj();cJSON_AddStringToObject(v,"code","workbuddy_hub_error");
 cJSON_AddStringToObject(v,"message",message);cJSON_AddNumberToObject(v,"http_status",status);return v;
}
static char *base64(const unsigned char *data,size_t n) {
 static const char table[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
 char *out=malloc(4*((n+2)/3)+1); if(!out)return NULL;size_t j=0;
 for(size_t i=0;i<n;i+=3){
  uint32_t v=(uint32_t)data[i]<<16; if(i+1<n)v|=(uint32_t)data[i+1]<<8;if(i+2<n)v|=data[i+2];
  out[j++]=table[(v>>18)&63];out[j++]=table[(v>>12)&63];
  out[j++]=i+1<n?table[(v>>6)&63]:'=';out[j++]=i+2<n?table[v&63]:'=';
 }out[j]=0;return out;
}
static cJSON *host_rpc(const char *method,cJSON *request) {
 if(shutting_down||!host.call)return NULL;char *raw=cJSON_PrintUnformatted(request);if(!raw)return NULL;
 Buffer out={0};int rc=host.call(host.context,method,(const uint8_t*)raw,strlen(raw),&out);free(raw);
 cJSON *result=out.ptr?cJSON_ParseWithLength(out.ptr,out.len):NULL;
 if(out.ptr&&host.free_buffer)host.free_buffer(out.ptr,out.len);
 cJSON *ok=cJSON_GetObjectItemCaseSensitive(result,"ok");
 if(rc||!result||cJSON_IsFalse(ok)){cJSON_Delete(result);return NULL;}return result;
}
static int host_emit(const char *id,cJSON *chunk) {
 cJSON *req=obj();cJSON_AddStringToObject(req,"stream_id",id);
 cJSON_AddItemToObject(req,"payload",cJSON_Duplicate(chunk,1));
 cJSON *answer=host_rpc("host.stream.emit",req);cJSON_Delete(req);
 int ok=answer!=NULL;cJSON_Delete(answer);return ok;
}
static void host_close(const char *id,const char *message) {
 cJSON *req=obj();cJSON_AddStringToObject(req,"stream_id",id);cJSON_AddStringToObject(req,"error",message);
 cJSON *answer=host_rpc("host.stream.close",req);cJSON_Delete(answer);cJSON_Delete(req);
}
static int pathcat(WCHAR *out,const WCHAR *a,const WCHAR *b) {
 return swprintf(out,32768,L"%ls\\%ls",a,b)>0;
}
static int safe_mkdirs(const WCHAR *path) {
 WCHAR copy[32768];if(wcslen(path)>=32760)return 0;wcscpy(copy,path);
 for(WCHAR *p=copy+3;;p++){
  if(*p==L'\\'||*p==0){
   WCHAR saved=*p;*p=0;DWORD a=GetFileAttributesW(copy);
   if(a!=INVALID_FILE_ATTRIBUTES){if(!(a&FILE_ATTRIBUTE_DIRECTORY)||(a&FILE_ATTRIBUTE_REPARSE_POINT))return 0;}
   else if(!CreateDirectoryW(copy,NULL)&&GetLastError()!=ERROR_ALREADY_EXISTS)return 0;
   *p=saved;if(!saved)break;
  }
 }return 1;
}
static uint16_t u16(const unsigned char *p){return p[0]|((uint16_t)p[1]<<8);}
static uint32_t u32(const unsigned char *p){return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static uint32_t crc32_bytes(const unsigned char *p,size_t n){
 uint32_t crc=~0u;
 while(n--){crc^=*p++;for(int i=0;i<8;i++)crc=(crc>>1)^(0xedb88320u&-(crc&1));}
 return ~crc;
}
static int extract_payload(void) {
 WCHAR dll[32768];DWORD n=GetModuleFileNameW(module,dll,32768);
 if(!n||n>=32768)return 0;WCHAR *slash=wcsrchr(dll,L'\\');if(!slash)return 0;*slash=0;
 /* Windows host loads a temporary shadow DLL. Never persist accounts beside that copy. */
 if(wcsstr(dll,L"\\cliproxy-pluginhost\\pid-")){
  WCHAR executable[32768];DWORD count=GetModuleFileNameW(NULL,executable,32768);
  if(!count||count>=32768)return 0;WCHAR *last=wcsrchr(executable,L'\\');if(!last)return 0;*last=0;
  WCHAR plugins[32768];pathcat(plugins,executable,L"plugins");pathcat(base_dir,plugins,L"workbuddy-hub");
 }else pathcat(base_dir,dll,L"workbuddy-hub");
 pathcat(runtime_dir,base_dir,L"runtime-" VERSION);pathcat(data_dir,base_dir,L"data");
 uint64_t h=1469598103934665603ull;for(WCHAR *p=base_dir;*p;p++){h^=*p;h*=1099511628211ull;}
 WCHAR name[128];swprintf(name,128,L"Local\\WorkBuddyHub-%llx",(unsigned long long)h);
 HANDLE mutex=CreateMutexW(NULL,FALSE,name);if(!mutex)return 0;
 DWORD wait=WaitForSingleObject(mutex,60000);if(wait!=WAIT_OBJECT_0&&wait!=WAIT_ABANDONED){CloseHandle(mutex);return 0;}
 WCHAR marker[32768];pathcat(marker,runtime_dir,L".complete");
 int ok=0;
 if(GetFileAttributesW(marker)!=INVALID_FILE_ATTRIBUTES){ok=1;goto finish;}
 if(!safe_mkdirs(runtime_dir))goto finish;
 const unsigned char *zip=wbh_payload_start;size_t len=(size_t)(wbh_payload_end-wbh_payload_start),offset=0,count=0;
 while(offset+30<=len&&u32(zip+offset)==0x04034b50){
  const unsigned char *p=zip+offset;size_t size=u32(p+18),nl=u16(p+26),ex=u16(p+28),start=offset+30+nl+ex;
  if(u16(p+8)!=0||(u16(p+6)&8)||start>len||size>len-start||nl==0||nl>1024)goto finish;
  char rel[1025];memcpy(rel,p+30,nl);rel[nl]=0;
  if(strlen(rel)!=nl||rel[0]=='/'||strchr(rel,'\\')||strchr(rel,':')||strstr(rel,"../")||strstr(rel,"/..")||strcmp(rel,"..")==0)goto finish;
  if(crc32_bytes(zip+start,size)!=u32(p+14))goto finish;
  WCHAR relative[2048];if(!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,rel,-1,relative,2048))goto finish;
  for(WCHAR *s=relative;*s;s++)if(*s=='/')*s='\\';
  WCHAR target[32768],parent[32768];pathcat(target,runtime_dir,relative);wcscpy(parent,target);
  WCHAR *end=wcsrchr(parent,L'\\');if(!end)goto finish;*end=0;if(!safe_mkdirs(parent))goto finish;
  DWORD attr=GetFileAttributesW(target);if(attr!=INVALID_FILE_ATTRIBUTES&&(attr&FILE_ATTRIBUTE_REPARSE_POINT))goto finish;
  HANDLE f=CreateFileW(target,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT,NULL);
  if(f==INVALID_HANDLE_VALUE)goto finish;DWORD wrote=0;BOOL wrote_ok=WriteFile(f,zip+start,(DWORD)size,&wrote,NULL);CloseHandle(f);
  if(!wrote_ok||wrote!=size)goto finish;
  count++;offset=start+size;
 }
 if(count<500)goto finish;
 HANDLE f=CreateFileW(marker,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
 if(f==INVALID_HANDLE_VALUE)goto finish;DWORD wrote=0;WriteFile(f,VERSION,strlen(VERSION),&wrote,NULL);CloseHandle(f);ok=1;
finish:
 ReleaseMutex(mutex);CloseHandle(mutex);return ok;
}
static void queue_value(Pending *p,cJSON *v) {
 Node *node=calloc(1,sizeof(Node));if(!node){cJSON_Delete(v);return;}
 node->value=v;if(p->tail)p->tail->next=node;else p->head=node;p->tail=node;SetEvent(p->event);
}
static void fail_pending(void) {
 EnterCriticalSection(&pending_lock);
 for(int i=0;i<MAX_PENDING;i++)if(pending[i]){
  cJSON *v=obj();cJSON_AddItemToObject(v,"error",error_value("WorkBuddy backend exited; request was not replayed",502));queue_value(pending[i],v);
 }
 LeaveCriticalSection(&pending_lock);
}
static DWORD WINAPI read_loop(LPVOID unused) {
 (void)unused;char buf[8192];size_t cap=16384,used=0;char *line=malloc(cap);DWORD got;int first=1;
 if(!line){SetEvent(ready_event);return 1;}
 while(ReadFile(child_output,buf,sizeof(buf),&got,NULL)&&got){
  for(DWORD k=0;k<got;k++){
   if(used+2>=cap){cap*=2;if(cap>64*1024*1024)goto eof;char *tmp=realloc(line,cap);if(!tmp)goto eof;line=tmp;}
   if(buf[k]!='\n'){line[used++]=buf[k];continue;}
   line[used]=0;cJSON *v=cJSON_ParseWithLength(line,used);used=0;
   if(!v)goto eof;
   if(first){first=0;ready_ok=cJSON_GetObjectItemCaseSensitive(v,"ready")!=NULL;cJSON_Delete(v);SetEvent(ready_event);if(!ready_ok)goto eof;continue;}
   cJSON *rid=cJSON_GetObjectItemCaseSensitive(v,"id");uint64_t id=cJSON_IsNumber(rid)?(uint64_t)rid->valuedouble:0;
   EnterCriticalSection(&pending_lock);int found=0;
   for(int i=0;i<MAX_PENDING;i++)if(pending[i]&&pending[i]->id==id){queue_value(pending[i],v);found=1;break;}
   LeaveCriticalSection(&pending_lock);if(!found)cJSON_Delete(v);
  }
 }
eof:
 free(line);ready_ok=0;SetEvent(ready_event);fail_pending();return 0;
}
static void stop_unlocked(void) {
 if(job){TerminateJobObject(job,0);CloseHandle(job);job=NULL;}
 if(child_process){WaitForSingleObject(child_process,5000);CloseHandle(child_process);child_process=NULL;}
 if(child_input){CloseHandle(child_input);child_input=NULL;}
 if(reader){WaitForSingleObject(reader,5000);CloseHandle(reader);reader=NULL;}
 if(child_output){CloseHandle(child_output);child_output=NULL;}
 if(ready_event){CloseHandle(ready_event);ready_event=NULL;}ready_ok=0;
}
static int ensure_worker(void) {
 if(child_process&&WaitForSingleObject(child_process,0)==WAIT_TIMEOUT&&ready_ok)return 1;
 stop_unlocked();
 if(!extract_payload()||!safe_mkdirs(data_dir))return 0;
 SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};HANDLE in_read=NULL,out_write=NULL,nul=NULL;
 if(!CreatePipe(&in_read,&child_input,&sa,0)||!CreatePipe(&child_output,&out_write,&sa,0))goto fail;
 SetHandleInformation(child_input,HANDLE_FLAG_INHERIT,0);SetHandleInformation(child_output,HANDLE_FLAG_INHERIT,0);
 nul=CreateFileW(L"NUL",GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,OPEN_EXISTING,0,NULL);
 job=CreateJobObjectW(NULL,NULL);if(!job)goto fail;
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION info={0};info.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
 if(!SetInformationJobObject(job,JobObjectExtendedLimitInformation,&info,sizeof(info)))goto fail;
 WCHAR python[32768],script[32768],command[32768];pathcat(python,runtime_dir,L"runtime\\python\\python.exe");pathcat(script,runtime_dir,L"worker.py");
 if(swprintf(command,32768,L"\"%ls\" -B -I -X utf8 \"%ls\" \"%ls\"",python,script,data_dir)<0)goto fail;
 STARTUPINFOW si={0};si.cb=sizeof(si);si.dwFlags=STARTF_USESTDHANDLES;si.hStdInput=in_read;si.hStdOutput=out_write;si.hStdError=nul;
 PROCESS_INFORMATION pi={0};
 if(!CreateProcessW(python,command,NULL,NULL,TRUE,CREATE_NO_WINDOW|CREATE_SUSPENDED,NULL,data_dir,&si,&pi))goto fail;
 child_process=pi.hProcess;
 if(!AssignProcessToJobObject(job,pi.hProcess)){TerminateProcess(pi.hProcess,1);CloseHandle(pi.hThread);goto fail;}
 ResumeThread(pi.hThread);CloseHandle(pi.hThread);CloseHandle(in_read);in_read=NULL;CloseHandle(out_write);out_write=NULL;CloseHandle(nul);nul=NULL;
 ready_event=CreateEventW(NULL,TRUE,FALSE,NULL);reader=CreateThread(NULL,0,read_loop,NULL,0,NULL);
 if(!reader||WaitForSingleObject(ready_event,30000)!=WAIT_OBJECT_0||!ready_ok)goto fail;
 return 1;
fail:
 if(in_read)CloseHandle(in_read);if(out_write)CloseHandle(out_write);if(nul&&nul!=INVALID_HANDLE_VALUE)CloseHandle(nul);stop_unlocked();return 0;
}
static int write_json(cJSON *value) {
 char *raw=cJSON_PrintUnformatted(value);if(!raw)return 0;size_t n=strlen(raw);raw=realloc(raw,n+2);raw[n]='\n';raw[n+1]=0;DWORD wrote=0;
 int ok=child_input&&WriteFile(child_input,raw,(DWORD)n+1,&wrote,NULL)&&wrote==n+1;free(raw);return ok;
}
static void cancel_request(uint64_t id) {
 cJSON *v=obj(),*req=obj();cJSON_AddNumberToObject(req,"id",(double)id);cJSON_AddStringToObject(v,"method","cancel_request");cJSON_AddItemToObject(v,"request",req);
 EnterCriticalSection(&write_lock);write_json(v);LeaveCriticalSection(&write_lock);cJSON_Delete(v);
}
static cJSON *rpc(const char *method,cJSON *request,const char *stream_id,cJSON **err) {
 Pending p={0};p.event=CreateEventW(NULL,FALSE,FALSE,NULL);if(!p.event){*err=error_value("Cannot create request event",502);return NULL;}
 EnterCriticalSection(&write_lock);
 if(!ensure_worker()){LeaveCriticalSection(&write_lock);CloseHandle(p.event);*err=error_value("Cannot start bundled WorkBuddy backend",502);return NULL;}
 p.id=++serial;int index=-1;EnterCriticalSection(&pending_lock);
 for(int i=0;i<MAX_PENDING;i++)if(!pending[i]){pending[i]=&p;index=i;break;}
 LeaveCriticalSection(&pending_lock);
 if(index<0){LeaveCriticalSection(&write_lock);CloseHandle(p.event);*err=error_value("Too many active WorkBuddy requests",429);return NULL;}
 cJSON *v=obj();cJSON_AddNumberToObject(v,"id",(double)p.id);cJSON_AddStringToObject(v,"method",method);cJSON_AddItemToObject(v,"request",cJSON_Duplicate(request,1));
 int sent=write_json(v);cJSON_Delete(v);LeaveCriticalSection(&write_lock);cJSON *result=NULL;
 if(!sent){*err=error_value("Backend communication failed; request was not replayed",502);goto done;}
 for(;;){
  EnterCriticalSection(&pending_lock);Node *node=p.head;
  if(node){p.head=node->next;if(!p.head)p.tail=NULL;}
  LeaveCriticalSection(&pending_lock);
  if(!node){
   if(WaitForSingleObject(p.event,150000)!=WAIT_OBJECT_0){*err=error_value("WorkBuddy backend timed out; request was not replayed",504);cancel_request(p.id);break;}continue;
  }
  cJSON *event=node->value;free(node);cJSON *e=cJSON_GetObjectItemCaseSensitive(event,"error"),*chunk=cJSON_GetObjectItemCaseSensitive(event,"chunk"),*r=cJSON_GetObjectItemCaseSensitive(event,"result");
  if(e){*err=cJSON_Duplicate(e,1);cJSON_Delete(event);break;}
  if(chunk&&stream_id&&!host_emit(stream_id,chunk)){*err=error_value("Client cancelled the stream",499);cancel_request(p.id);cJSON_Delete(event);break;}
  if(r){result=cJSON_Duplicate(r,1);cJSON_Delete(event);break;}cJSON_Delete(event);
 }
done:
 EnterCriticalSection(&pending_lock);pending[index]=NULL;
 while(p.head){Node *n=p.head;p.head=n->next;cJSON_Delete(n->value);free(n);}
 LeaveCriticalSection(&pending_lock);CloseHandle(p.event);return result;
}
typedef struct { cJSON *request; char *stream_id; } StreamTask;
static DWORD WINAPI stream_task(LPVOID raw){
 StreamTask *task=(StreamTask*)raw;cJSON *err=NULL;
 cJSON *result=rpc("executor.execute_stream",task->request,task->stream_id,&err);
 if(!shutting_down)host_close(task->stream_id,err?text(err,"message"):"");
 cJSON_Delete(result);cJSON_Delete(err);cJSON_Delete(task->request);free(task->stream_id);free(task);
 InterlockedDecrement(&active_streams);return 0;
}
static void plugin_shutdown(void){
 InterlockedExchange(&shutting_down,1);
 EnterCriticalSection(&write_lock);stop_unlocked();LeaveCriticalSection(&write_lock);
 for(int i=0;i<250&&active_streams;i++)Sleep(20);
}
static void plugin_free(void *ptr,size_t len){(void)len;free(ptr);}
static cJSON *dispatch(const char *method,cJSON *request,cJSON **err) {
 if(!strcmp(method,"plugin.register")||!strcmp(method,"plugin.reconfigure")){
  return cJSON_Parse("{\"schema_version\":6,\"metadata\":{\"Name\":\"WorkBuddy\",\"Version\":\"" VERSION "\",\"Author\":\"WorkBuddy Hub adapter contributors; upstream ardeyouxipianyi\",\"Description\":\"WorkBuddy2API-Hub v1.7.24 adapter for EasyCPA\",\"License\":\"MIT\",\"GitHubRepository\":\"https://github.com/daodeqing/cpa-plugin-WorkBuddyHub\",\"ConfigFields\":[]},\"capabilities\":{\"auth_provider\":true,\"model_provider\":true,\"executor\":true,\"management_api\":true,\"quota_provider\":true,\"executor_model_scope\":\"oauth\",\"executor_input_formats\":[\"chat-completions\"],\"executor_output_formats\":[\"chat-completions\"]}}");
 }
 if(!strcmp(method,"auth.identifier")||!strcmp(method,"executor.identifier")||!strcmp(method,"quota.identifier")){cJSON *v=obj();cJSON_AddStringToObject(v,"identifier",PROVIDER);return v;}
 if(!strcmp(method,"quota.describe"))return cJSON_Parse("{\"supported_providers\":[\"workbuddy-hub\"],\"display_name\":\"WorkBuddy\",\"supports_reset\":false}");
 if(!strcmp(method,"management.register"))return cJSON_Parse("{\"Resources\":[{\"Path\":\"/login\",\"Description\":\"WorkBuddy sign in\"}],\"Routes\":[]}");
 if(!strcmp(method,"model.static"))return cJSON_Parse("{\"Provider\":\"" PROVIDER "\",\"Models\":[]}");
 if(!strcmp(method,"executor.count_tokens")){*err=error_value("WorkBuddy has no exact token counting endpoint",501);return NULL;}
 const char *allowed[]={"auth.login.start","auth.login.poll","auth.parse","auth.refresh","model.for_auth","quota.fetch","executor.execute","executor.execute_stream","management.handle",NULL};int known=0;
 for(int i=0;allowed[i];i++)if(!strcmp(method,allowed[i])){known=1;break;}
 if(!known){*err=error_value("Unsupported plugin method",400);return NULL;}
 const char *sid=!strcmp(method,"executor.execute_stream")?text(request,"stream_id"):NULL;
 if(sid&&!*sid){*err=error_value("stream_id required",400);return NULL;}
 if(sid){
  if(shutting_down){*err=error_value("Plugin is stopping",503);return NULL;}
  StreamTask *task=calloc(1,sizeof(StreamTask));
  if(!task){*err=error_value("Cannot allocate stream task",503);return NULL;}
  task->request=cJSON_Duplicate(request,1);task->stream_id=_strdup(sid);
  InterlockedIncrement(&active_streams);HANDLE thread=CreateThread(NULL,0,stream_task,task,0,NULL);
  if(!thread){InterlockedDecrement(&active_streams);cJSON_Delete(task->request);free(task->stream_id);free(task);*err=error_value("Cannot start stream task",503);return NULL;}
  CloseHandle(thread);
  return cJSON_Parse("{\"headers\":{\"content-type\":[\"text/event-stream\"]},\"chunks\":[]}");
 }
 cJSON *result=rpc(method,request,NULL,err);
 if(!result&&!strcmp(method,"management.handle")){
  int status=502;cJSON *s=cJSON_GetObjectItemCaseSensitive(*err,"http_status");if(cJSON_IsNumber(s))status=s->valueint;
  const char *msg=text(*err,"message");char *body=base64((const unsigned char*)msg,strlen(msg));
  result=obj();cJSON_AddNumberToObject(result,"StatusCode",status);cJSON_AddStringToObject(result,"Body",body?body:"");free(body);
  cJSON_AddItemToObject(result,"Headers",cJSON_Parse("{\"content-type\":[\"text/plain; charset=utf-8\"],\"cache-control\":[\"no-store\"],\"referrer-policy\":[\"no-referrer\"]}"));
  cJSON_Delete(*err);*err=NULL;
 }return result;
}
static int plugin_call(const char *method,const uint8_t *bytes,size_t len,Buffer *out) {
 if(!out)return 1;out->ptr=NULL;out->len=0;
 cJSON *req=len&&bytes?cJSON_ParseWithLength((const char*)bytes,len):obj(),*err=NULL,*result=NULL;
 if(!method||!req)err=error_value("Invalid plugin request",400);else result=dispatch(method,req,&err);cJSON_Delete(req);
 cJSON *envelope=obj();cJSON_AddBoolToObject(envelope,"ok",result!=NULL);
 if(result)cJSON_AddItemToObject(envelope,"result",result);else cJSON_AddItemToObject(envelope,"error",err?err:error_value("Native adapter failed",502));
 char *raw=cJSON_PrintUnformatted(envelope);cJSON_Delete(envelope);if(!raw)return 1;out->ptr=raw;out->len=strlen(raw);return result?0:1;
}
__declspec(dllexport) int cliproxy_plugin_init(const HostApi *h,PluginApi *api) {
 if(!h||h->version!=1||!api)return 1;host=*h;api->version=1;api->call=plugin_call;api->free_buffer=plugin_free;api->shutdown=plugin_shutdown;return 0;
}

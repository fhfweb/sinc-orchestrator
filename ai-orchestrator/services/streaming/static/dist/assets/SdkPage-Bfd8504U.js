import{a as B,o as j,e as y,f as v,g as t,F as _,s as E,h as L,t as c,r as f,c as R,n as k,q as P,L as U}from"./vendor-ZjHzkyv6.js";import{u as z}from"./useApi-DHBpucMU.js";import{u as D,_ as q}from"./index-BnkmTiOj.js";const K={class:"sdk-page"},H={class:"sdk-layout"},I={class:"card config-panel"},N={class:"field-group"},O={class:"lang-buttons flex gap-2"},Y=["onClick"],V={class:"field-group"},F={class:"field-label text-muted"},M={class:"endpoint-list"},J=["value"],X={class:"mono ep-path"},Q=["disabled"],W={class:"card code-panel"},Z={class:"code-panel-header flex items-center justify-between"},ee={class:"flex items-center gap-2"},te={class:"badge badge-info"},oe={class:"code-block"},se=B({__name:"SdkPage",setup(ne){const{api:T}=z(),h=D(),m=f(!1),r=f("python"),p=f(["/llm/status","/token-budgets","/health/grid"]),a=f(""),C=[{id:"python",label:"Python"},{id:"typescript",label:"TypeScript"},{id:"go",label:"Go"},{id:"curl",label:"cURL"}],d=[{path:"/llm/status",method:"GET"},{path:"/token-budgets",method:"GET"},{path:"/context/traces",method:"GET"},{path:"/entropy/metrics",method:"GET"},{path:"/memory/list",method:"GET"},{path:"/memory/prune",method:"POST"},{path:"/knowledge/list",method:"GET"},{path:"/knowledge/save",method:"POST"},{path:"/compliance/report",method:"GET"},{path:"/health/grid",method:"GET"},{path:"/usage/analytics",method:"GET"},{path:"/deployments/blue-green/status",method:"GET"},{path:"/capacity/predict",method:"GET"},{path:"/gates",method:"GET"},{path:"/twin/status",method:"GET"}],b=R(()=>p.value.length===d.length);function $(){p.value=b.value?[]:d.map(n=>n.path),u()}function u(){const n="https://your-host/api/v5/dashboard",o=p.value;r.value==="python"?a.value=`import requests

BASE_URL = "${n}"
API_KEY = "your-api-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

${o.map(e=>{const s=d.find(g=>g.path===e),l=e.replace(/\//g,"_").replace(/^_/,"").replace(/-/g,"_"),i=(s==null?void 0:s.method)??"GET";return`def ${l}():
    resp = requests.${i.toLowerCase()}(f"{BASE_URL}${e}", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()`}).join(`

`)}
`:r.value==="typescript"?a.value=`const BASE_URL = "${n}";
const API_KEY = "your-api-key";
const headers = { Authorization: \`Bearer \${API_KEY}\`, "Content-Type": "application/json" };

${o.map(e=>{const s=d.find(g=>g.path===e),l=e.replace(/\//g,"_").replace(/^_/,"").replace(/-/g,"_"),i=(s==null?void 0:s.method)??"GET";return`export async function ${l}(): Promise<any> {
  const res = await fetch(\`\${BASE_URL}${e}\`, { method: "${i}", headers });
  if (!res.ok) throw new Error(\`HTTP \${res.status}\`);
  return res.json();
}`}).join(`

`)}
`:r.value==="go"?a.value=`package noc

import (
	"encoding/json"
	"fmt"
	"net/http"
)

const baseURL = "${n}"
const apiKey = "your-api-key"

func doRequest(method, path string) (map[string]interface{}, error) {
	req, _ := http.NewRequest(method, baseURL+path, nil)
	req.Header.Set("Authorization", "Bearer "+apiKey)
	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil { return nil, err }
	defer resp.Body.Close()
	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)
	return result, nil
}

${o.map(e=>{const s=e.split("/").filter(Boolean).map(i=>i.charAt(0).toUpperCase()+i.slice(1).replace(/-([a-z])/g,(g,S)=>S.toUpperCase())).join(""),l=d.find(i=>i.path===e);return`func ${s}() (map[string]interface{}, error) {
	return doRequest("${(l==null?void 0:l.method)??"GET"}", "${e}")
}`}).join(`

`)}
`:a.value=o.map(e=>{const s=d.find(l=>l.path===e);return`# ${e}
curl -X ${(s==null?void 0:s.method)??"GET"} "${n}${e}" \\
  -H "Authorization: Bearer your-api-key" \\
  -H "Content-Type: application/json"`}).join(`

`)}async function w(){m.value=!0;try{const n=await T("/sdk/generate",{method:"POST",body:JSON.stringify({language:r.value,endpoints:p.value})});a.value=n.code??a.value,h.showToast("Code generated","ok")}catch{u(),h.showToast("Using local generator","info")}finally{m.value=!1}}function G(){navigator.clipboard.writeText(a.value),h.showToast("Copied to clipboard","ok")}function A(){const n={python:"py",typescript:"ts",go:"go",curl:"sh"},o=new Blob([a.value],{type:"text/plain"}),e=document.createElement("a");e.href=URL.createObjectURL(o),e.download=`noc-client.${n[r.value]??"txt"}`,e.click(),h.showToast("Downloaded","ok")}function x(n){return n==="GET"?"badge-ok":"badge-info"}return j(u),(n,o)=>(y(),v("div",K,[o[6]||(o[6]=t("div",{class:"page-header flex items-center justify-between"},[t("div",null,[t("h1",{class:"page-title"},"SDK Generator"),t("p",{class:"text-muted"},"Generate client code for any language and endpoint selection")])],-1)),t("div",H,[t("div",I,[o[4]||(o[4]=t("div",{class:"panel-title"},"Configuration",-1)),t("div",N,[o[2]||(o[2]=t("div",{class:"field-label text-muted"},"Language",-1)),t("div",O,[(y(),v(_,null,E(C,e=>t("button",{key:e.id,class:k(["btn",r.value===e.id?"btn-primary":"btn-ghost"]),onClick:s=>{r.value=e.id,u()}},c(e.label),11,Y)),64))])]),t("div",V,[t("div",F,[o[3]||(o[3]=L(" Endpoints ",-1)),t("button",{class:"btn btn-ghost",style:{"font-size":".7rem",padding:".15rem .4rem","margin-left":".5rem"},onClick:$},c(b.value?"None":"All"),1)]),t("div",M,[(y(),v(_,null,E(d,e=>t("label",{key:e.path,class:"endpoint-item"},[P(t("input",{type:"checkbox",value:e.path,"onUpdate:modelValue":o[0]||(o[0]=s=>p.value=s),onChange:o[1]||(o[1]=s=>u())},null,40,J),[[U,p.value]]),t("span",X,c(e.path),1),t("span",{class:k(["badge",x(e.method)]),style:{"font-size":".65rem"}},c(e.method),3)])),64))])]),t("button",{class:"btn btn-primary",style:{width:"100%"},onClick:w,disabled:m.value},c(m.value?"Generating...":"Generate from API"),9,Q)]),t("div",W,[t("div",Z,[t("div",ee,[o[5]||(o[5]=t("span",{class:"panel-title",style:{"margin-bottom":"0"}},"Generated Code",-1)),t("span",te,c(r.value),1)]),t("div",{class:"flex gap-2"},[t("button",{class:"btn btn-ghost",style:{"font-size":".78rem"},onClick:G},"Copy"),t("button",{class:"btn btn-ghost",style:{"font-size":".78rem"},onClick:A},"Download")])]),t("pre",oe,[t("code",null,c(a.value),1)])])])]))}}),ie=q(se,[["__scopeId","data-v-1a67e816"]]);export{ie as default};

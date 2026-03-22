import{a as U,o as A,e as d,f as c,g as e,t as i,q as f,v as h,F as S,s as z,m as G,n as w,r as l,c as j}from"./vendor-ZjHzkyv6.js";import{u as M}from"./useApi-cwOloB23.js";import{u as O,_ as F}from"./index-Cq-GfAgR.js";const N={class:"knowledge-page"},V={class:"page-header flex items-center justify-between"},B=["disabled"],I={class:"split-layout"},K={class:"card doc-list-panel"},Q={class:"doc-items"},E=["onClick"],J={class:"doc-item-title truncate"},W={class:"doc-item-meta flex items-center justify-between"},$={class:"badge badge-info"},H={class:"flex items-center gap-2"},Y=["title"],X={class:"text-muted",style:{"font-size":".7rem"}},Z={class:"text-muted",style:{"font-size":".7rem"}},ee={key:0,class:"text-muted",style:{padding:".75rem","font-size":".85rem","text-align":"center"}},te={class:"card editor-panel"},ne={key:0,class:"empty-editor text-muted"},se={class:"editor-header flex items-center justify-between",style:{"margin-bottom":".75rem"}},ae={style:{flex:"1","margin-right":"1rem"}},oe={class:"flex gap-2 items-center",style:{"margin-top":".35rem"}},ie={class:"quality-indicator"},le={class:"flex gap-2"},re=["disabled"],de={class:"editor-footer flex items-center justify-between"},ce={class:"text-muted",style:{"font-size":".75rem"}},ue={class:"text-muted",style:{"font-size":".75rem"}},ve=U({__name:"KnowledgePage",setup(pe){const{api:g}=M(),_=O(),m=l(!1),y=l(!1),o=l([]),a=l(null),u=l(""),v=l(""),p=l(""),r=l(""),C=[{id:"doc-001",title:"GDPR Data Retention Policy",category:"Compliance",lastUpdated:"2026-03-20",qualityScore:92,content:`# GDPR Data Retention Policy

This document outlines the data retention requirements under GDPR...

## Key Principles

- Data minimization: only collect what is necessary
- Storage limitation: delete data after its purpose is fulfilled
- Right to erasure: users can request deletion

## Retention Periods

| Data Type | Retention Period |
|-----------|------------------|
| User sessions | 90 days |
| Logs | 1 year |
| Audit trails | 7 years |`},{id:"doc-002",title:"RAG Pipeline Architecture",category:"Technical",lastUpdated:"2026-03-19",qualityScore:88,content:`# RAG Pipeline Architecture

## Overview

The RAG (Retrieval-Augmented Generation) pipeline consists of...

1. **Query Processing** — tokenize and embed the user query
2. **Retrieval** — search Qdrant vector store with top-k=5
3. **Reranking** — cross-encoder reranking for relevance
4. **Context injection** — inject retrieved docs into LLM prompt
5. **Generation** — LLM generates grounded response`},{id:"doc-003",title:"Incident Response Runbook",category:"Operations",lastUpdated:"2026-03-18",qualityScore:74,content:`# Incident Response Runbook

## Severity Levels

- **P0**: Complete outage — page on-call immediately
- **P1**: Major degradation — response within 15 min
- **P2**: Minor issues — response within 1 hour

## First Steps

1. Acknowledge the alert
2. Check Grafana dashboard for root cause
3. Check recent deployments
4. Rollback if deployment-related`},{id:"doc-004",title:"LLM Provider Failover Policy",category:"Technical",lastUpdated:"2026-03-15",qualityScore:81,content:`# LLM Provider Failover Policy

When a primary LLM provider fails circuit breaker threshold...

## Failover Order

1. OpenAI (primary)
2. Anthropic (secondary)
3. Groq (tertiary — lower cost, faster)
4. Fallback to cached responses

## Circuit Breaker Settings

- Threshold: 5 errors in 60 seconds
- Half-open probe: every 30 seconds`}],b=j(()=>u.value?o.value.filter(t=>t.title.toLowerCase().includes(u.value.toLowerCase())||t.category.toLowerCase().includes(u.value.toLowerCase())):o.value);function k(t){a.value=t,v.value=t.title,p.value=t.category,r.value=t.content}function q(){const t={id:"doc-new-"+Date.now(),title:"New Document",category:"General",lastUpdated:"just now",qualityScore:0,content:""};o.value.unshift(t),k(t)}async function P(){if(a.value){y.value=!0;try{await g("/knowledge/save",{method:"POST",body:JSON.stringify({id:a.value.id,title:v.value,category:p.value,content:r.value})});const t=o.value.findIndex(s=>s.id===a.value.id);t>=0&&(o.value[t]={...o.value[t],title:v.value,category:p.value,content:r.value,lastUpdated:"just now"},a.value=o.value[t]),_.showToast("Document saved","ok")}catch{_.showToast("Save failed","err")}finally{y.value=!1}}}async function R(){if(a.value&&confirm(`Delete "${a.value.title}"?`)){try{await g("/knowledge/delete",{method:"POST",body:JSON.stringify({id:a.value.id})})}catch{}o.value=o.value.filter(t=>t.id!==a.value.id),a.value=null,_.showToast("Document deleted","ok")}}function L(t){return t>=80?"qdot-ok":t>=60?"qdot-warn":"qdot-err"}function T(t){return t>=80?"text-ok":t>=60?"text-warn":"text-danger"}async function x(){m.value=!0;try{const t=await g("/knowledge/list");o.value=t.docs??t}catch{o.value=C}finally{m.value=!1}}return A(x),(t,s)=>(d(),c("div",N,[e("div",V,[s[4]||(s[4]=e("div",null,[e("h1",{class:"page-title"},"Knowledge Base Editor"),e("p",{class:"text-muted"},"View, edit and manage knowledge documents")],-1)),e("button",{class:"btn btn-ghost",onClick:x,disabled:m.value},i(m.value?"...":"Refresh"),9,B)]),e("div",I,[e("div",K,[e("div",{class:"doc-list-header flex items-center justify-between"},[s[5]||(s[5]=e("span",{class:"panel-section-title"},"Documents",-1)),e("button",{class:"btn btn-primary",style:{"font-size":".75rem",padding:".3rem .6rem"},onClick:q},"+ New")]),f(e("input",{class:"search-input","onUpdate:modelValue":s[0]||(s[0]=n=>u.value=n),placeholder:"Search documents...",style:{width:"100%","box-sizing":"border-box"}},null,512),[[h,u.value]]),e("div",Q,[(d(!0),c(S,null,z(b.value,n=>{var D;return d(),c("div",{key:n.id,class:w(["doc-item",{selected:((D=a.value)==null?void 0:D.id)===n.id}]),onClick:me=>k(n)},[e("div",J,i(n.title),1),e("div",W,[e("span",$,i(n.category),1),e("div",H,[e("div",{class:w(["quality-dot",L(n.qualityScore)]),title:"Quality: "+n.qualityScore},null,10,Y),e("span",X,i(n.qualityScore)+"/100",1)])]),e("div",Z,i(n.lastUpdated),1)],10,E)}),128)),b.value.length===0?(d(),c("div",ee," No documents found ")):G("",!0)])]),e("div",te,[a.value?(d(),c(S,{key:1},[e("div",se,[e("div",ae,[f(e("input",{class:"title-input","onUpdate:modelValue":s[1]||(s[1]=n=>v.value=n),placeholder:"Document title"},null,512),[[h,v.value]]),e("div",oe,[f(e("input",{class:"category-input","onUpdate:modelValue":s[2]||(s[2]=n=>p.value=n),placeholder:"Category"},null,512),[[h,p.value]]),e("div",ie,[s[6]||(s[6]=e("span",{class:"text-muted",style:{"font-size":".7rem"}},"QUALITY",-1)),e("span",{class:w(["quality-score",T(a.value.qualityScore)])},i(a.value.qualityScore)+"/100 ",3)])])]),e("div",le,[e("button",{class:"btn btn-ghost",onClick:P,disabled:y.value},i(y.value?"Saving...":"Save"),9,re),e("button",{class:"btn btn-danger",onClick:R},"Delete")])]),f(e("textarea",{class:"doc-editor","onUpdate:modelValue":s[3]||(s[3]=n=>r.value=n),placeholder:"Write document content here... Markdown supported."},null,512),[[h,r.value]]),e("div",de,[e("span",ce,i(r.value.length)+" chars · "+i(r.value.split(/\s+/).filter(Boolean).length)+" words ",1),e("span",ue,"Last updated: "+i(a.value.lastUpdated),1)])],64)):(d(),c("div",ne," Select a document to edit or create a new one "))])])]))}}),ge=F(ve,[["__scopeId","data-v-412d6f79"]]);export{ge as default};

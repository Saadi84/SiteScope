"use strict";
const $ = id => document.getElementById(id);
const PAGE_SIZE = 60;
let jobId = null, rows = [], running = false, page = 0, debounce = null, pollTimer = null;
let redirectRules = [];

const cell = (value, className="") => {
  const el = document.createElement("td");
  if (className) el.className = className;
  el.textContent = String(value ?? "");
  return el;
};
const badgeClass = status => status.startsWith("Working") ? "working" : status === "Not working" ? "broken" : status === "Blocked" ? "blocked" : "error";
function drawRow(row) {
  const tr = document.createElement("tr");
  tr.append(cell(row._index + 1));
  const url = cell("", "url-text");
  const a = document.createElement("a"); a.href=row.URL; a.target="_blank"; a.rel="noopener noreferrer"; a.textContent=row.URL;
  url.append(a); tr.append(url);
  const st = cell(""); const badge = document.createElement("span");
  badge.className="badge " + badgeClass(row.Status); badge.textContent=row.Status;
  st.append(badge); tr.append(st);
  const http = cell("", "http-pair");
  const first=document.createElement("span"); first.textContent=String(row.HTTP || "—");
  const second=document.createElement("span"); second.className="secondary"; second.textContent=" / " + String(row["Browser HTTP"] || "—");
  http.append(first,second); tr.append(http);
  tr.append(cell(row.Title || "—", "title-text"));
  const action=cell(""); const btn=document.createElement("button");btn.type="button";btn.className="details-button";
  btn.textContent="Details"; btn.dataset.index=String(row._index); btn.setAttribute("aria-expanded", "false");
  action.append(btn);tr.append(action);return tr;
}
function makeDetails(row) {
  const tr=document.createElement("tr");tr.className="detail-row";
  const td=cell("");td.colSpan=6;
  const grid=document.createElement("div");grid.className="details-grid";
  for(const [label,value] of [["Found via",row.Source||"Unknown"],["Notes",row.Notes||"No additional notes."]]){
    const el=document.createElement("div"), title=document.createElement("b"), content=document.createElement("p");
    title.textContent=label;content.textContent=value;el.append(title,content);grid.append(el);
  }
  td.append(grid);tr.append(td);return tr;
}
$("body").addEventListener("click", e => {
  const btn=e.target.closest("button[data-index]");if (!btn) return;
  const current=btn.closest("tr").nextElementSibling;
  if(current?.classList.contains("detail-row")){current.remove();btn.textContent="Details";btn.setAttribute("aria-expanded","false");return;}
  btn.closest("tr").after(makeDetails(rows[Number(btn.dataset.index)]));
  btn.textContent="Hide";btn.setAttribute("aria-expanded","true");
});
function renderTable(){
  const q=$("filter").value.trim().toLowerCase();
  const matching=q?rows.filter(r=>[r.URL,r.Status,r.Title,r.Notes,r.Source,r.HTTP,r["Browser HTTP"]]
    .some(v=>String(v??"").toLowerCase().includes(q))):rows;
  const pages=Math.max(1,Math.ceil(matching.length/PAGE_SIZE));page=Math.min(page,pages-1);
  const subset=matching.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);
  const frag=document.createDocumentFragment();
  for(const row of subset) frag.append(drawRow(row));
  if (!subset.length){const tr=document.createElement("tr"),td=cell(rows.length?"No matching pages.":"Results appear here as pages are checked.");tr.className="empty";td.colSpan=6;tr.append(td);frag.append(tr);}
  $("body").replaceChildren(frag);
  $("table-count").textContent=rows.length.toLocaleString();
  $("shown").textContent=matching.length?`Showing ${page*PAGE_SIZE+1}–${page*PAGE_SIZE+subset.length} of ${matching.length.toLocaleString()} matches` : "Showing 0 results";
  $("page-label").textContent=`Page ${page+1} of ${pages}`;
  $("prev").disabled=page===0;$("next").disabled=page>=pages-1;
}
function setRunning(value){
  running=value;for(const id of ["start","url","max-pages","delay","browser-mode","include-archives"])$(id).disabled=value;
  $("stop").disabled=!value;
}
function notice(message, style=""){$("notice-text").textContent=message;$("notice").className="notice "+style;}
async function requestJSON(url, options){
  const resp=await fetch(url,options);let data;
  try{data=await resp.json();}catch{throw new Error("Unexpected server response");}
  if(!resp.ok)throw new Error(data.error||"Request failed");return data;
}

function pageTokens(row){
  let raw="";
  try{ raw=new URL(row.URL).pathname + " " + (row.Title||""); }catch{ raw=(row.URL||"")+" "+(row.Title||""); }
  const stop=new Set(["www","com","html","htm","php","index","page","blog","the","and","for","with","from","this","that","your","our"]);
  return new Set(raw.toLowerCase().replace(/[^a-z0-9]+/g," ").split(/\s+/).filter(x=>x.length>2&&!stop.has(x)));
}
function scoreTarget(broken, candidate){
  const a=pageTokens(broken), b=pageTokens(candidate); let common=0;
  for(const t of a) if(b.has(t)) common++;
  let pathDepth=0; try{ pathDepth=new URL(candidate.URL).pathname.split("/").filter(Boolean).length; }catch{}
  return common*10 - pathDepth*.25 + (candidate.URL.endsWith("/")?0.1:0);
}
function brokenRows(){return rows.filter(r=>r.Status==="Not working");}
function workingRows(){return rows.filter(r=>String(r.Status).startsWith("Working"));}
function suggestionsFor(broken){
  const candidates=workingRows().filter(r=>r.URL!==broken.URL).map(r=>({r,score:scoreTarget(broken,r)}));
  candidates.sort((x,y)=>y.score-x.score || x.r.URL.length-y.r.URL.length);
  const top=candidates.slice(0,8).map(x=>x.r);
  try{
    const u=new URL(broken.URL); const home=workingRows().find(r=>{try{return new URL(r.URL).pathname==="/"}catch{return false}});
    if(home && !top.some(r=>r.URL===home.URL)) top.push(home);
  }catch{}
  return top;
}
function refreshRedirectAssistant(){
  const broken=brokenRows();
  $("broken-ready").textContent=`${broken.length.toLocaleString()} broken URL${broken.length===1?"":"s"} ready`;
  $("redirect-empty").hidden=broken.length>0;
  $("redirect-workspace").hidden=broken.length===0;
  if(!broken.length) return;
  const current=$("broken-select").value;
  $("broken-select").replaceChildren(...broken.map(r=>{const o=document.createElement("option");o.value=r.URL;o.textContent=r.URL;return o;}));
  if(broken.some(r=>r.URL===current)) $("broken-select").value=current;
  refreshTargetSuggestions(); renderRedirectPlan();
}
function refreshTargetSuggestions(){
  const broken=brokenRows().find(r=>r.URL===$("broken-select").value) || brokenRows()[0];
  if(!broken)return;
  const opts=suggestionsFor(broken);
  $("target-select").replaceChildren(...opts.map((r,i)=>{const o=document.createElement("option");o.value=r.URL;o.textContent=`${i+1}. ${r.Title||"Untitled"} — ${r.URL}`;return o;}));
  $("add-redirect").disabled=!opts.length;
}
function sameOrigin(a,b){try{return new URL(a).origin===new URL(b).origin}catch{return false}}
function addRedirectRule(targetOverride=""){
  const from=$("broken-select").value, to=(targetOverride||$("target-select").value||"").trim(), code=$("redirect-code").value;
  if(!from||!to){notice("Choose a broken URL and a destination first.","warn");return;}
  try{ new URL(to); }catch{notice("Destination must be a valid URL.","error");return;}
  if(from===to){notice("Source and destination cannot be the same URL.","error");return;}
  if(!sameOrigin(from,to)){notice("For safety, Redirect Assistant only accepts a destination on the same website.","warn");return;}
  const existing=redirectRules.findIndex(r=>r.from===from);
  const rule={from,to,code};
  if(existing>=0)redirectRules[existing]=rule;else redirectRules.push(rule);
  renderRedirectPlan(); notice(`Redirect rule added: ${code} ${from} → ${to}`);
}
function renderRedirectPlan(){
  const tbody=$("redirect-body"), frag=document.createDocumentFragment();
  if(!redirectRules.length){const tr=document.createElement("tr"),td=cell("No redirect rules added yet.");tr.className="empty";td.colSpan=4;tr.append(td);frag.append(tr);}else{
    redirectRules.forEach((r,i)=>{
      const tr=document.createElement("tr");
      const from=cell("","url-text"), fa=document.createElement("a");fa.href=r.from;fa.target="_blank";fa.rel="noopener noreferrer";fa.textContent=r.from;from.append(fa);tr.append(from);
      tr.append(cell(r.code));
      const to=cell("","url-text"), ta=document.createElement("a");ta.href=r.to;ta.target="_blank";ta.rel="noopener noreferrer";ta.textContent=r.to;to.append(ta);tr.append(to);
      const act=cell(""); const b=document.createElement("button");b.type="button";b.className="remove-rule";b.textContent="Remove";b.addEventListener("click",()=>{redirectRules.splice(i,1);renderRedirectPlan();});act.append(b);tr.append(act);frag.append(tr);
    });
  }
  tbody.replaceChildren(frag); $("redirect-count").textContent=`${redirectRules.length} rule${redirectRules.length===1?"":"s"}`;
  for(const id of ["copy-apache","copy-nginx","redirect-csv"])$(id).disabled=!redirectRules.length;
}
function pathWithQuery(value){const u=new URL(value);return u.pathname+(u.search||"");}
function apacheRules(){return redirectRules.map(r=>`Redirect ${r.code} ${pathWithQuery(r.from)} ${r.to}`).join("\n");}
function nginxRules(){return redirectRules.map(r=>`location = ${new URL(r.from).pathname} { return ${r.code} ${r.to}; }`).join("\n");}
async function copyText(text,label){try{await navigator.clipboard.writeText(text);notice(`${label} redirect rules copied to clipboard.`);}catch{notice("Clipboard access was blocked by the browser. Use Redirect CSV instead.","warn");}}
function downloadText(filename,text,type="text/plain;charset=utf-8"){
  const blob=new Blob([text],{type});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=filename;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},0);
}
function csvEscape(v){const s=String(v??"");return /[",\n]/.test(s)?`"${s.replace(/"/g,'""')}"`:s;}
function redirectCSV(){return "Source URL,Redirect Type,Destination URL\n"+redirectRules.map(r=>[r.from,r.code,r.to].map(csvEscape).join(",")).join("\n");}

async function detectEntryOptions(){
  const url=$("url").value.trim();
  if(!url){notice("Enter a website URL first.","warn");return;}
  $("detect-entry").disabled=true;
  $("detect-entry").textContent="Detecting…";
  notice("Checking the landing page for country, language or regional site versions…");
  try{
    const data=await requestJSON("/api/detect-entry",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    $("entry-message").textContent=data.message+(data.note?` ${data.note}`:"");
    $("entry-select").replaceChildren(...data.options.map((item,i)=>{const o=document.createElement("option");o.value=item.url;o.textContent=`${item.label} — ${item.url}`;if(i===0)o.selected=true;return o;}));
    $("entry-box").hidden=false;
    notice(data.message,data.detected?"":"");
  }catch(err){$("entry-box").hidden=true;notice(err.message,"error");}
  finally{$("detect-entry").disabled=running;$("detect-entry").textContent="◎ Detect country / language site versions";}
}
function useDetectedEntry(){
  const value=$("entry-select").value;
  if(!value)return;
  $("url").value=value;
  notice(`Site version selected: ${value}`);
  $("entry-box").hidden=true;
}
async function poll(){
  if(!jobId)return;
  try{
    const data=await requestJSON(`/api/status/${jobId}?after=${rows.length}`);
    if(data.rows.length){for(const row of data.rows){row._index=rows.length;rows.push(row);}renderTable();refreshRedirectAssistant();}
    $("discovered").textContent=(data.discovered||0).toLocaleString();
    $("unique-results").textContent=(data.unique_results||0).toLocaleString();
    $("duplicates-merged").textContent=(data.aliases_merged||0).toLocaleString();
    $("working").textContent=((data.counts.Working||0)+(data.counts["Working (browser)"]||0)).toLocaleString();
    $("not-working").textContent=(data.counts["Not working"]||0).toLocaleString();
    $("blocked").textContent=(data.counts.Blocked||0).toLocaleString();
    $("other").textContent=((data.counts["Server error"]||0)+(data.counts.Error||0)+(data.counts["Not checked"]||0)).toLocaleString();
    $("export").disabled=!data.checked;$("csv").disabled=!data.checked;
    $("activity").textContent=data.state==="running"?"Scanning in background…":`Scan ${data.state}`;
    notice(data.message,data.state==="error"?"error":(data.counts.Blocked||0)>0?"warn":"");
    if(data.has_more){pollTimer=setTimeout(poll,20);} else if(data.state==="running"){pollTimer=setTimeout(poll,1000);} else{setRunning(false);refreshRedirectAssistant();}
  }catch(err){setRunning(false);notice(err.message,"error");}
}
$("detect-entry").addEventListener("click",detectEntryOptions);
$("use-entry").addEventListener("click",useDetectedEntry);
$("form").addEventListener("submit",async e=>{
  e.preventDefault();if(running)return;
  clearTimeout(pollTimer);rows=[];redirectRules=[];jobId=null;page=0;setRunning(true);renderTable();refreshRedirectAssistant();
  for(const id of ["discovered","unique-results","duplicates-merged","working","not-working","blocked","other"])$(id).textContent="0";
  $("export").disabled=true;$("csv").disabled=true;
  notice("Starting scan; URLs will appear as they are checked. Use Stop to end a long scan.");
  try{
    const value=$("max-pages").value.trim();
    const payload={url:$("url").value.trim(),max_pages:value===""?null:Number(value),delay:Number($("delay").value),browser_mode:$("browser-mode").checked,include_archives:$("include-archives").checked};
    const data=await requestJSON("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    jobId=data.job_id;await poll();
  }catch(err){setRunning(false);notice(err.message,"error");$("activity").textContent="Could not start";}
});
$("stop").addEventListener("click",async()=>{
  if(!jobId)return;$("stop").disabled=true;
  try{await requestJSON(`/api/stop/${jobId}`,{method:"POST"});notice("Stopping after the current request…");}
  catch(err){notice(err.message,"error");$("stop").disabled=false;}
});
$("export").addEventListener("click",()=>{if(jobId&&rows.length)window.location.href=`/api/download/${jobId}`;});
$("csv").addEventListener("click",()=>{if(jobId&&rows.length)window.location.href=`/api/csv/${jobId}`;});
$("filter").addEventListener("input",()=>{clearTimeout(debounce);debounce=setTimeout(()=>{page=0;renderTable();},180);});
$("prev").addEventListener("click",()=>{if(page>0){page--;renderTable();}});$("next").addEventListener("click",()=>{page++;renderTable();});
$("broken-select").addEventListener("change",refreshTargetSuggestions);
$("add-redirect").addEventListener("click",()=>addRedirectRule());
$("use-custom-target").addEventListener("click",()=>{const v=$("custom-target").value.trim();if(v)addRedirectRule(v);else notice("Enter a destination URL first.","warn");});
$("copy-apache").addEventListener("click",()=>copyText(apacheRules(),"Apache/.htaccess"));
$("copy-nginx").addEventListener("click",()=>copyText(nginxRules(),"Nginx"));
$("redirect-csv").addEventListener("click",()=>downloadText("sitescope_redirect_plan.csv",redirectCSV(),"text/csv;charset=utf-8"));
refreshRedirectAssistant();

(function(){
const ov=document.createElement("div");ov.id="ad";ov.style.cssText="position:fixed;inset:0;z-index:30;background:linear-gradient(180deg,var(--bg1),var(--bg2));overflow:auto;padding:14px 14px 50px;display:none";document.body.appendChild(ov);
const ap=(a,b)=>api("/api/admin",Object.assign({a},b||{}));
const ask=m=>new Promise(r=>tg&&tg.showConfirm?tg.showConfirm(m,r):r(confirm(m)));
const say=(m,cb)=>{if(tg&&tg.showAlert)tg.showAlert(m,cb);else{alert(m);cb&&cb()}};
const bt=(t,x)=>`<button class="bt" data-x="${x}" style="width:100%;margin:5px 0">${t}</button>`;
const hd=(t,b)=>`<div class="top"><div class="logo" style="font-size:18px">${t}</div><button class="ib" data-x="${b||"close"}">${b?"←":"✕"}</button></div><div style="height:10px"></div>`;
const row=(l,r,x)=>`<div class="r c" data-x="${x||""}" style="display:flex;justify-content:space-between;gap:8px;padding:12px;margin-bottom:8px"><span>${l}</span><b>${r||""}</b></div>`;
const inp=(id,ph,tag)=>tag?`<textarea class="srch" id="${id}" rows="4" placeholder="${ph}" style="border-radius:16px;margin:6px 0"></textarea>`:`<input class="srch" id="${id}" placeholder="${ph}" style="margin:6px 0">`;
const shrink=f=>new Promise(res=>{const i=new Image();i.onload=()=>{const k=Math.min(1,900/Math.max(i.width,i.height)),c=document.createElement("canvas");c.width=i.width*k;c.height=i.height*k;c.getContext("2d").drawImage(i,0,0,c.width,c.height);res(c.toDataURL("image/jpeg",.85).split(",")[1])};i.src=URL.createObjectURL(f)});
const R={
 async home(){return hd("👑 Admin panel")+bt("📊 Statistika","stats")+bt("👤 Foydalanuvchilar","users")+bt("➕ Anime qo'shish","add")+bt("🎞 Animelar: qism / o'chirish","anime")+bt("📢 Kanallar","chs")+bt("📨 Xabar yuborish","bc")+(D.user.main?bt("👥 Adminlar","adm"):"")},
 async stats(){const s=await ap("stats"),g=(n,l)=>`<div class="sx"><b>${n}</b><span>${l}</span></div>`;return hd("📊 Statistika","home")+`<div class="sg">${g(s.users,"👥 Foydalanuvchi")}${g(s.today,"🆕 Bugun")}${g(s.active,"🟢 Bugun faol")}${g(s.blocked,"🚫 Bloklagan")}${g(s.animes,"🎬 Anime")}${g(s.eps,"📺 Qism")}${g(s.ratings,"⭐ Baho")}${g(s.favs,"💾 Saqlash")}</div><h2>🔥 Top ko'rilgan</h2>${s.pop.map(x=>row(esc(x))).join("")}<h2>🏆 Top reyting</h2>${s.top.map(x=>row(esc(x))).join("")}`},
 async users(a){a=a&&typeof a=="object"?a:{flt:"all",s:""};const d=await ap("users",a);return hd("👤 Foydalanuvchilar","home")+`<select class="srch" id="uf" style="margin:6px 0">${Object.entries(d.filters).map(([k,v])=>`<option value="${k}" ${k==a.flt?"selected":""}>${esc(v)}</option>`).join("")}</select><input class="srch" id="us" placeholder="Ism, @username yoki ID" value="${esc(a.s)}" style="margin:6px 0"><div class="ds">Jami: ${d.total}</div>`+d.list.map(r=>row((r.b?"🚫 ":"🟢 ")+esc(r.n||"Noma'lum")+(r.u?" @"+esc(r.u):""),"🎬"+r.vc,"user:"+r.id)).join("")},
 async user(id){const u=await ap("user",{id});if(u.error)return hd("Xato","users")+u.error;return hd("👤 "+esc(u.n||""),"users")+`<div class="pf">${u.u?"@"+esc(u.u)+"<br>":""}🆔 ${u.id}<br>📱 ${esc(u.ph||"yo'q")}<br>📅 ${esc(u.j||"—")}<br>🕐 ${esc(u.s||"—")}<br>${u.b?"🚫 botni bloklagan":"🟢 faol"}<br>💾 Saqlagan: ${u.fav} · ⭐ baholagan: ${u.rat}</div><h2>🎬 Ko'rgan animelari (${u.wn})</h2><div class="ds">${u.w.map(esc).join(", ")||"—"}</div>`+(u.b?"":inp("mtx","Xabar matni...",1)+bt("✉️ Yuborish","send:"+u.id))},
 async add(){return hd("➕ Anime qo'shish","home")+inp("at","Nomi *")+inp("ag","Janr (Jangari, Sarguzasht)")+inp("ad2","Tavsif",1)+`<div class="ds">🖼 Poster (ixtiyoriy):</div><input type="file" accept="image/*" id="ap" class="ds">`+bt("✅ Yaratish","addgo")+`<div class="ds">Yaratilgach, qismlarni botga video qilib yuborasiz.</div>`},
 async anime(){return hd("🎞 Animelar","home")+(D.animes.map(a=>row(esc(a.title)+" <small>#"+a.id+"</small>","📺"+a.eps,"ai:"+a.id)).join("")||"Bo'sh")},
 async ai(id){const a=D.animes.find(x=>x.id==id);return hd(esc(a.title),"anime")+bt("🎞 Qism qo'shish (botda)","epadd:"+id)+bt("✂️ Qismlarni o'chirish","eps:"+id)+bt("🗑 Animeni o'chirish","del:"+id)},
 async eps(id){const d=await ap("eps",{id});return hd("✂️ Qismlar","ai:"+id)+`<div class="g" style="grid-template-columns:repeat(4,1fr)">${d.nums.map(n=>`<button class="bt" data-x="epdel:${id}:${n}" style="padding:10px">🗑 ${n}</button>`).join("")||"Qism yo'q"}</div>`},
 async chs(){const d=await ap("chs"),L=(t,k)=>`<h2>${t}</h2>`+(d[k].map(c=>row(esc(c.t),"🗑",`chdel:${k}:${c.id}`)).join("")||'<div class="ds">Yo\'q</div>')+inp("c_"+k,"@username, havola yoki ID")+bt("➕ Qo'shish","chadd:"+k);return hd("📢 Kanallar","home")+L("Majburiy obuna","ch")+L("E'lon kanallari","news")+'<div class="ds">Bot kanalda admin bo\'lishi kerak.</div>'},
 async bc(){return hd("📨 Xabar yuborish","home")+inp("bt2","Xabar matni...",1)+`<select class="srch" id="bg"><option value="users">👥 Foydalanuvchilarga</option><option value="channels">📣 E'lon kanallariga</option><option value="both">👥📣 Ikkalasiga</option></select>`+bt("🚀 Hozir yuborish","bcgo")+'<div class="ds">Rasm/video yoki rejalangan xabar uchun botdagi «📨 Xabar yuborish» dan foydalaning.</div>'},
 async adm(){const d=await ap("adm");if(d.error)return hd("Xato","home")+d.error;return hd("👥 Adminlar","home")+row("👑 Asosiy",d.main)+d.env.map(i=>row("🔒 "+i,"")).join("")+d.list.map(a=>row(esc(a.n)+" "+a.id,"🗑","admdel:"+a.id)).join("")+inp("am","ID yoki @username")+bt("➕ Admin qo'shish","admadd")}
};
const H={
 close(){ov.style.display="none"},
 async send(id){const t=$("#mtx").value.trim();if(!t)return;const r=await ap("msg_user",{id,text:t});say(r.error||"✅ Yuborildi");if(!r.error)$("#mtx").value=""},
 async addgo(){const t=$("#at").value.trim();if(!t)return say("Nomini yozing");const f=$("#ap").files[0],p=f?await shrink(f):null,r=await ap("anime_add",{title:t,genre:$("#ag").value,descr:$("#ad2").value,poster:p});if(r.error)return say(r.error);load();say("✅ Yaratildi (kod "+r.id+"). Botga o'tib qismlarni video qilib yuboring.",()=>tg&&tg.close())},
 async epadd(id){await ap("ep_add",{id});say("🎞 Botga o'tib qismlarni video qilib yuboring, so'ng «✅ Tugatish» ni bosing.",()=>tg&&tg.close())},
 async epdel(id,n){if(!await ask(n+"-qism o'chirilsinmi?"))return;await ap("ep_del",{id,num:n});go("eps",id);load()},
 async del(id){if(!await ask("Anime va barcha qismlari o'chirilsinmi?"))return;await ap("anime_del",{id});await load();go("anime")},
 async chadd(k){const r=await ap("ch_add",{kind:k,ref:$("#c_"+k).value});r.error?say(r.error):go("chs")},
 async chdel(k,id){if(!await ask("O'chirilsinmi?"))return;await ap("ch_del",{kind:k,id});go("chs")},
 async bcgo(){const t=$("#bt2").value.trim();if(!t)return;if(!await ask("Xabar yuborilsinmi?"))return;const r=await ap("bc",{text:t,target:$("#bg").value});say(r.error||"🚀 Yuborish boshlandi. Hisobot botga keladi.")},
 async admadd(){const r=await ap("adm_add",{ref:$("#am").value});r.error?say(r.error):go("adm")},
 async admdel(id){if(!await ask("Admin olib tashlansinmi?"))return;await ap("adm_del",{id});go("adm")}
};
async function go(p,a){ov.style.display="block";try{ov.innerHTML=await R[p](a)}catch(e){ov.innerHTML=hd("Xato","home")+'<div class="em">Xatolik yuz berdi</div>'}ov.scrollTop=0}
ov.addEventListener("click",e=>{const b=e.target.closest("[data-x]");if(!b||!b.dataset.x)return;const[k,...r]=b.dataset.x.split(":");if(R[k])go(k,r[0]);else if(H[k])H[k](...r)});
ov.addEventListener("change",e=>{if(e.target.id=="uf"||e.target.id=="us")go("users",{flt:$("#uf").value,s:$("#us").value})});
window.openAdmin=()=>go("home");
})();

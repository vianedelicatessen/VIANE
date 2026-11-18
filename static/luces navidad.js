(()=>{
const W=1220,cv=document.getElementById('luces-canvas'),ctx=cv.getContext('2d'),img=document.querySelector('.luces-stage img'),hint=document.getElementById('luces-hint');
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
if(reduce)hint.remove();
let seed=7;const rnd=()=>(seed=(seed*16807)%2147483647)/2147483647;
const PAL=['#e0202f','#ffb21c','#1fa84f','#ffe6a8'];
const hex=h=>[1,3,5].map(i=>parseInt(h.substr(i,2),16));
const mix=(a,b,t)=>{const x=hex(a),y=hex(b);return 'rgb('+x.map((v,i)=>Math.round(v+(y[i]-v)*t))+')'};
const rgba=(h,a)=>'rgba('+hex(h)+','+a+')';

/* Recorridos de los cables (coordenadas del diseño 1220×1289) */
const swag=(x0,y0,x1,y1,s,n)=>Array.from({length:n+1},(_,i)=>{const t=i/n;return[x0+(x1-x0)*t,y0+(y1-y0)*t+4*s*t*(1-t)]});
const scal=(x0,y,x1,c,s)=>{const p=[],w=(x1-x0)/c;for(let j=0;j<c;j++)p.push(...swag(x0+w*j,y,x0+w*(j+1),y,s,24));return p};
const wav=(x,y0,y1,a,f)=>Array.from({length:61},(_,i)=>{const t=i/60;return[x+a*Math.sin(t*Math.PI*f),y0+(y1-y0)*t]});
const DEFS=[
 {p:scal(95,446,1125,5,40),g:50,a:0,s:1},
 {p:swag(25,592,1195,566,105,90),g:58,a:0,s:1},
 {p:swag(150,884,1005,852,120,70),g:56,a:0,s:1},
 {p:wav(52,640,840,16,3),g:42,a:58,s:1},
 {p:wav(1168,620,770,-16,2.4),g:42,a:-58,s:1},
 {p:swag(410,1112,745,1112,40,40),g:30,a:0,s:.55}
];
const bulbs=[];
DEFS.forEach((d,si)=>{
 const p=d.p,acc=[0];
 for(let i=1;i<p.length;i++)acc.push(acc[i-1]+Math.hypot(p[i][0]-p[i-1][0],p[i][1]-p[i-1][1]));
 const n=Math.floor(acc[acc.length-1]/d.g);let j=1;
 for(let i=0;i<n;i++){
  const s=d.g*.5+i*d.g;while(j<acc.length-1&&acc[j]<s)j++;
  const f=(s-acc[j-1])/(acc[j]-acc[j-1]||1);
  bulbs.push({x:p[j-1][0]+(p[j][0]-p[j-1][0])*f,y:p[j-1][1]+(p[j][1]-p[j-1][1])*f,ang:d.a*Math.PI/180+(rnd()-.5)*.18,sc:d.s,ci:(i+si)%4,i,n,ph:rnd()*6.28,sp:.7+rnd()*.9});
 }
});

/* Dibujo de bombillos */
const R=90;let k=1,wire=null,base=[],lit=[];
const rr=(c,x,y,w,h,r)=>{c.beginPath();c.roundRect?c.roundRect(x,y,w,h,r):c.rect(x,y,w,h)};
function glass(c,col,on){
 c.beginPath();c.moveTo(-5,10);c.bezierCurveTo(-14,15,-13,40,0,42);c.bezierCurveTo(13,40,14,15,5,10);c.closePath();
 const g=c.createRadialGradient(0,27,1,0,27,17);
 if(on){g.addColorStop(0,mix(col,'#ffffff',.9));g.addColorStop(.4,mix(col,'#ffffff',.4));g.addColorStop(1,col)}
 else{g.addColorStop(0,mix(col,'#ffffff',.2));g.addColorStop(1,mix(col,'#000000',.5))}
 c.fillStyle=g;c.fill();c.lineWidth=.8;c.strokeStyle=on?rgba(col,.9):'rgba(0,0,0,.3)';c.stroke();
 c.fillStyle='rgba(255,255,255,.8)';c.beginPath();c.ellipse(-5.6,24,1.5,7.5,.12,0,7);c.fill();
 c.fillStyle='rgba(255,255,255,.35)';c.beginPath();c.ellipse(5.2,33,1,3,-.3,0,7);c.fill();
}
function socket(c){
 const g=c.createLinearGradient(-5,0,5,0);
 g.addColorStop(0,'#5f4210');g.addColorStop(.35,'#ecc760');g.addColorStop(.62,'#b58b25');g.addColorStop(1,'#4f3609');
 c.fillStyle='#2b4f35';rr(c,-3.2,-6,6.4,5,1.4);c.fill();
 c.fillStyle=g;rr(c,-5,-1.5,10,11.5,2);c.fill();
 c.strokeStyle='rgba(55,36,4,.55)';c.lineWidth=.8;
 for(const y of[2.2,5,7.8]){c.beginPath();c.moveTo(-5,y);c.lineTo(5,y);c.stroke()}
}
function sprite(draw){
 const c=document.createElement('canvas');c.width=c.height=Math.ceil(2*R*k);
 const x=c.getContext('2d');x.setTransform(k,0,0,k,R*k,R*k);draw(x);return c;
}
const trace=(c,p,dy)=>{c.beginPath();p.forEach(([x,y],i)=>i?c.lineTo(x,y+dy):c.moveTo(x,y+dy))};
function build(){
 base=PAL.map(col=>sprite(x=>{
  x.shadowColor='rgba(60,40,20,.35)';x.shadowBlur=4*k;x.shadowOffsetX=k;x.shadowOffsetY=3*k;
  glass(x,col,false);x.shadowColor='transparent';socket(x);
 }));
 lit=PAL.map(col=>sprite(x=>{
  const h=x.createRadialGradient(0,27,2,0,27,62);
  h.addColorStop(0,rgba(col,.6));h.addColorStop(.35,rgba(col,.28));h.addColorStop(1,rgba(col,0));
  x.fillStyle=h;x.fillRect(-R,-R,2*R,2*R);glass(x,col,true);
 }));
 wire=document.createElement('canvas');wire.width=cv.width;wire.height=cv.height;
 const w=wire.getContext('2d');w.setTransform(k,0,0,k,0,0);w.lineCap=w.lineJoin='round';
 for(const d of DEFS){
  trace(w,d.p,0);w.shadowColor='rgba(40,30,20,.3)';w.shadowBlur=6*k;w.shadowOffsetY=4*k;
  w.strokeStyle='#27472f';w.lineWidth=3.4;w.stroke();
  w.shadowColor='transparent';trace(w,d.p,-.8);w.strokeStyle='rgba(255,255,255,.28)';w.lineWidth=.9;w.stroke();
 }
}
function size(){
 const r=img.getBoundingClientRect();if(!r.width)return;
 const dpr=Math.min(devicePixelRatio||1,2);
 cv.width=Math.round(r.width*dpr);cv.height=Math.round(r.height*dpr);k=cv.width/W;build();
}
new ResizeObserver(size).observe(img);

/* Animación: parpadeo suave en reposo; al arrastrar, destellos, ola de luz y balanceo */
let e=0,chase=0,T=0,last=performance.now();
function frame(now){
 const dt=Math.min(.05,(now-last)/1000);last=now;T+=dt;
 e*=Math.exp(-dt/1.1);chase+=dt*.16;
 ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,cv.width,cv.height);
 if(wire){
  ctx.drawImage(wire,0,0);ctx.setTransform(k,0,0,k,0,0);
  for(const b of bulbs){
   b.ph+=dt*b.sp*(reduce?1.2:1.6+e*4);
   const idle=.3+.7*(.5+.5*Math.sin(b.ph)),fast=.5+.5*Math.tanh(Math.sin(b.ph*1.5)*4);
   const wv=Math.pow(.5+.5*Math.cos(6.283*(b.i/b.n*1.5-chase)),6);
   const v=Math.min(1,idle*(1-e)+fast*e+wv*(.25+.55*e));
   const sw=Math.sin(T*1.8+b.ph*.3)*(.025+.17*e);
   ctx.save();ctx.translate(b.x,b.y);ctx.rotate(b.ang+sw);ctx.scale(b.sc,b.sc);
   ctx.drawImage(base[b.ci],-R,-R,2*R,2*R);
   ctx.globalAlpha=v;ctx.drawImage(lit[b.ci],-R,-R,2*R,2*R);
   ctx.restore();
  }
 }
 requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

/* Interacción: scroll, rueda, dedo y arrastre con el mouse */
let ly=scrollY,tx=0,ty=0;
function kick(d,s){if(reduce)return;e=Math.min(1,e+Math.min(.5,d/120));chase+=s*d*.003;hint.classList.add('off')}
addEventListener('scroll',()=>{const y=scrollY;kick(Math.abs(y-ly),Math.sign(y-ly));ly=y},{passive:true});
addEventListener('wheel',v=>kick(Math.abs(v.deltaY),Math.sign(v.deltaY)),{passive:true});
addEventListener('touchstart',v=>{tx=v.touches[0].clientX;ty=v.touches[0].clientY},{passive:true});
addEventListener('touchmove',v=>{const t=v.touches[0],dx=t.clientX-tx,dy=t.clientY-ty;tx=t.clientX;ty=t.clientY;kick(Math.hypot(dx,dy),-Math.sign(dy||dx))},{passive:true});
addEventListener('pointermove',v=>{if(v.buttons&&v.pointerType==='mouse')kick(Math.hypot(v.movementX,v.movementY),Math.sign(v.movementX||v.movementY))});
setTimeout(()=>hint.classList.add('off'),7000);
})();

// Ported from the jarvis-interface prototype (~/Downloads/jarvis-interface) —
// the particle-hologram rendering engine only. avatar.js drives this from
// real JARVIS state instead of the prototype's manual sliders/buttons/hand-
// tracking. Diverged from the original prototype in a couple of targeted
// spots, each with its own comment: the floor/grid/beam meshes are gone
// (explicit request — this now runs fullscreen, not in a small framed
// panel, so a projector "floor" under it stopped making sense), and the
// default camera distance is closer (fullscreen gives it room to actually
// be big). A pixel-ratio cut was tried here for a reported lag and reverted
// — it made the render visibly soft without measurably fixing the lag,
// which means fragment fill rate wasn't the real bottleneck. rig.position.y
// is lower than the original (.15, was .38) — moved down/more centered on
// request, now that it's filling the whole viewport instead of a small
// framed panel. The drag()/cursor() API below was already here, unused —
// avatar.js is what wires real pointer events to it now.
import * as T from "three";
const TAU=Math.PI*2,clamp=T.MathUtils.clamp;
export function createHologram(container,{onMetrics=()=>{},onScanEnd=()=>{}}={}){
 const renderer=new T.WebGLRenderer({alpha:true,antialias:false,powerPreference:'high-performance'});
 renderer.setClearColor(0x02090e,1);renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));container.append(renderer.domElement);
 const scene=new T.Scene(),camera=new T.PerspectiveCamera(40,1,.1,80);camera.position.set(0,.15,10);
 const rig=new T.Group();scene.add(rig);const assembly=new T.Group();rig.add(assembly);
 const colors=[],uniformColors=[];let color=new T.Color('#79edff'),quality=1.5;
 const state={mode:0,energy:.7,expansion:.12,auto:true,motion:true,hand:false,listening:false};
 // zoom was 10.6 (tuned for a ~190px-tall panel) — lower = camera closer =
 // bigger on screen. 7.6 fills a fullscreen viewport without clipping at
 // max expansion; still within the existing 7.2-16 clamp in changeZoom().
 const target={x:.23,y:0,z:0,zoom:7.6},pose={x:.23,y:0,z:0,zoom:7.6,expansion:.12,mode:0};
 let time=0,charge=0,react=0,pulseStart=-1,pulsePower=1,scanTime=-1,demoStart=-1,clock=0,last=performance.now(),fpsStart=last,frames=0,slowSeconds=0,disposed=false;
 const pointer=new T.Vector2(10,10),mouse=new T.Vector2();
 const makeLine=(opacity=.5,warm=false)=>{const m=new T.LineBasicMaterial({color:warm?0xffaf6b:color,transparent:true,opacity,blending:T.AdditiveBlending,depthWrite:false});if(!warm)colors.push(m);return m;};
 const arc=(r,a=0,b=TAU,n=128)=>new T.BufferGeometry().setFromPoints(Array.from({length:n+1},(_,i)=>{let t=a+(b-a)*i/n;return new T.Vector3(r*Math.cos(t),r*Math.sin(t),0);}));
 function ticks(r,count,length,opacity){const pts=[];for(let i=0;i<count;i++){const a=i/count*TAU,l=i%5===0?length:length*.3;pts.push(new T.Vector3(Math.cos(a)*r,Math.sin(a)*r,0),new T.Vector3(Math.cos(a)*(r+l),Math.sin(a)*(r+l),0));}return new T.LineSegments(new T.BufferGeometry().setFromPoints(pts),makeLine(opacity));}
 // A translucent shell makes surface depth visible between the mechanical layers.
 const shellMat=new T.ShaderMaterial({uniforms:{uColor:{value:color.clone()},uTime:{value:0},uEnergy:{value:.7},uScan:{value:-3}},vertexShader:`varying vec3 vNormal;varying vec3 vView;varying vec3 vPos;void main(){vPos=position;vec4 mv=modelViewMatrix*vec4(position,1.);vNormal=normalize(normalMatrix*normal);vView=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}`,fragmentShader:`uniform vec3 uColor;uniform float uTime;uniform float uEnergy;uniform float uScan;varying vec3 vNormal;varying vec3 vView;varying vec3 vPos;void main(){float rim=pow(1.-abs(dot(normalize(vNormal),normalize(vView))),2.6);float lat=pow(.5+.5*sin(vPos.y*88.),18.);float scan=exp(-pow((vPos.y-uScan)*12.,2.));float waves=.5+.5*sin(vPos.y*11.+vPos.x*7.+uTime);float alpha=rim*.24+lat*.07+scan*.4;gl_FragColor=vec4(uColor*(.7+rim*.8+scan),alpha*(.7+uEnergy*.4));}`,transparent:true,depthWrite:false,blending:T.AdditiveBlending});uniformColors.push(shellMat.uniforms.uColor);
 const shell=new T.Mesh(new T.SphereGeometry(1.19,72,48),shellMat);assembly.add(shell);
 const inner=new T.Group();assembly.add(inner);
 const nucleusMat=new T.MeshBasicMaterial({color:0xc0faff,transparent:true,opacity:.7,blending:T.AdditiveBlending,depthWrite:false});
 const nucleus=new T.Mesh(new T.IcosahedronGeometry(.08,1),nucleusMat);inner.add(nucleus);
 for(let k=0;k<6;k++){const ring=new T.Line(arc(.2+k*.035),makeLine(.4,k===2));ring.rotation.set(k*.55,k*.7,0);inner.add(ring);}
 for(let j=0;j<3;j++){const w=new T.LineSegments(new T.EdgesGeometry(new T.IcosahedronGeometry(.57+j*.16,1)),makeLine(.24+j*.04));w.rotation.set(j*.7,j*.8,j*.3);inner.add(w);}
 // Surface points have three genuine target shapes, with GPU interpolation.
 const points=8000,positions=[],neural=[],lattice=[],seeds=[];
 let seed=1337;const random=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};
 for(let i=0;i<points;i++){let y=1-2*(i+.5)/points,a=i*Math.PI*(3-Math.sqrt(5)),w=Math.sqrt(1-y*y),r=1.2+Math.sin(i*2.3)*.035;positions.push(Math.cos(a)*w*r,y*r,Math.sin(a)*w*r);
 const branch=i%7,phase=i/points*TAU*13,rr=.35+random()*1.25;neural.push(Math.sin(phase+branch)*rr,Math.cos(phase*.73+branch)*rr,Math.sin(phase*.61+branch)*rr);
 const side=i%6,u=((i%400)%20)/19*2-1,v=Math.floor((i%400)/20)/19*2-1;let p=[u,v,1];if(side===1)p=[u,v,-1];if(side===2)p=[1,u,v];if(side===3)p=[-1,u,v];if(side===4)p=[u,1,v];if(side===5)p=[u,-1,v];lattice.push(...p.map(n=>n*1.18));seeds.push(random());}
 const pgeo=new T.BufferGeometry();pgeo.setAttribute('position',new T.Float32BufferAttribute(positions,3));pgeo.setAttribute('aNeural',new T.Float32BufferAttribute(neural,3));pgeo.setAttribute('aLattice',new T.Float32BufferAttribute(lattice,3));pgeo.setAttribute('aSeed',new T.Float32BufferAttribute(seeds,1));
 const pm=new T.ShaderMaterial({uniforms:{uColor:{value:color.clone()},uTime:{value:0},uEnergy:{value:.7},uMode:{value:0},uExpansion:{value:.12},uPointer:{value:pointer},uPixel:{value:renderer.getPixelRatio()},uPulse:{value:0}},vertexShader:`attribute vec3 aNeural;attribute vec3 aLattice;attribute float aSeed;uniform float uTime;uniform float uMode;uniform float uExpansion;uniform float uEnergy;uniform float uPixel;uniform float uPulse;uniform vec2 uPointer;varying float vAlpha;varying float vSeed;void main(){vec3 p=uMode<1.?mix(position,aNeural,uMode):mix(aNeural,aLattice,uMode-1.);p*=1.+uExpansion*.38;float wave=sin(p.y*9.+uTime*2.+aSeed*6.);p+=normalize(p)*wave*(.018+uPulse*.045);vec4 mv=modelViewMatrix*vec4(p,1.);vec4 clip=projectionMatrix*mv;vec2 screen=clip.xy/clip.w;float force=exp(-dot(screen-uPointer,screen-uPointer)*20.);p+=normalize(p)*force*.2;mv=modelViewMatrix*vec4(p,1.);gl_Position=projectionMatrix*mv;gl_PointSize=clamp((8.+aSeed*12.+uEnergy*9.)*uPixel/(-mv.z),1.,6.);vAlpha=.2+.6*pow(.5+.5*sin(aSeed*40.+uTime*1.2),3.)+force*.25;vSeed=aSeed;}`,fragmentShader:`uniform vec3 uColor;varying float vAlpha;varying float vSeed;void main(){float d=length(gl_PointCoord-.5);if(d>.5)discard;vec3 c=mix(uColor,vec3(.93,1.,1.),step(.97,vSeed)*.6);gl_FragColor=vec4(c,(1.-smoothstep(.02,.5,d))*vAlpha);}`,transparent:true,blending:T.AdditiveBlending,depthWrite:false});uniformColors.push(pm.uniforms.uColor);const field=new T.Points(pgeo,pm);assembly.add(field);
 const bands=[];
 for(let j=0;j<12;j++){
 const group=new T.Group(),radius=1.45+j*.075,warm=j===3||j===9;
 const line=makeLine(j%2?.5:.3,warm);
 for(let k=0;k<3;k++){const a=k*TAU/3+j*.35;group.add(new T.Line(arc(radius,a,a+1.3,96),line));}
 if(j%3===0)group.add(ticks(radius+.025,96,.065,.4));
 if(j===1||j===7){const mat=new T.MeshBasicMaterial({color,side:T.DoubleSide,transparent:true,opacity:.25,blending:T.AdditiveBlending,depthWrite:false});colors.push(mat);for(let k=0;k<24;k++){group.add(new T.Mesh(new T.RingGeometry(radius,radius+.035,6,1,k*TAU/24,.115),mat));}}
 const baseX=j<4?.08:j%2?.9:-.8,baseY=j<4?0:j%3?.5:-.6;group.rotation.set(baseX,baseY,j*.5);group.userData={baseX,baseY,index:j};assembly.add(group);bands.push(group);
 }
 // Fine orbital tracks and moving sparks establish foreground and background motion.
 const tracks=[];for(let j=0;j<5;j++){const group=new T.Group(),radius=2.05+j*.07;group.add(new T.Line(arc(radius),makeLine(.10)));const beadMat=new T.MeshBasicMaterial({color:j%2?0xffaf6b:0xadefff});const bead=new T.Mesh(new T.SphereGeometry(.018,8,6),beadMat);group.add(bead);group.rotation.set(.5+j*.49,.3+j*.7,j*.2);group.userData={bead,radius};assembly.add(group);tracks.push(group);}
 const cage=new T.LineSegments(new T.EdgesGeometry(new T.BoxGeometry(2.45,2.45,2.45,5,5,5)),makeLine(.45));assembly.add(cage);cage.visible=false;
 const networkPts=[];for(let i=0;i<110;i++){let a=i/110*TAU*3,b=a+.7;networkPts.push(new T.Vector3(Math.cos(a)*1.4,Math.sin(a*.7)*1.4,Math.sin(a)*1.1),new T.Vector3(Math.cos(b)*1.4,Math.sin(b*.7)*1.4,Math.sin(b)*1.1));}const network=new T.LineSegments(new T.BufferGeometry().setFromPoints(networkPts),makeLine(.17));assembly.add(network);network.visible=false;
 // Halo sprite — the illuminated projection floor/grid/beam that used to
 // live here are gone entirely per explicit request ("remove the floor
 // below the hologram"); it read as a pedestal the hologram stood on,
 // which stopped fitting once this became a fullscreen floating core
 // rather than an object sitting in a framed panel.
 const gradient=document.createElement('canvas');gradient.width=gradient.height=128;const gc=gradient.getContext('2d');const gr=gc.createRadialGradient(64,64,0,64,64,64);gr.addColorStop(0,'rgba(120,235,255,.8)');gr.addColorStop(.08,'rgba(90,220,255,.35)');gr.addColorStop(.35,'rgba(50,180,255,.08)');gr.addColorStop(1,'rgba(10,100,220,0)');gc.fillStyle=gr;gc.fillRect(0,0,128,128);const glowMat=new T.SpriteMaterial({map:new T.CanvasTexture(gradient),color,transparent:true,opacity:.45,depthWrite:false,blending:T.AdditiveBlending});colors.push(glowMat);const glow=new T.Sprite(glowMat);glow.scale.set(5,5,1);assembly.add(glow);
 const starPos=[];for(let i=0;i<400;i++)starPos.push((random()-.5)*20,(random()-.5)*12,(random()-.5)*12-5);const stars=new T.Points(new T.BufferGeometry().setAttribute('position',new T.Float32BufferAttribute(starPos,3)),new T.PointsMaterial({color:0x7cb5c9,size:.015,opacity:.35,transparent:true,depthWrite:false}));scene.add(stars);
 const shockMat=new T.MeshBasicMaterial({color,transparent:true,opacity:0,side:T.DoubleSide,blending:T.AdditiveBlending,depthWrite:false});colors.push(shockMat);
 const shock=new T.Mesh(new T.RingGeometry(1.,1.025,160),shockMat);shock.visible=false;scene.add(shock);
 const sweep=new T.Mesh(new T.RingGeometry(1.16,1.25,128),new T.MeshBasicMaterial({color:0xb8f9ff,transparent:true,opacity:.7,side:T.DoubleSide,blending:T.AdditiveBlending,depthWrite:false}));sweep.rotation.x=Math.PI/2;assembly.add(sweep);sweep.visible=false;
 // Lightweight separable bloom: one scene pass, two half-resolution blur passes, one composite.
 const targetScene=new T.WebGLRenderTarget(1,1,{depthBuffer:true}),blurA=new T.WebGLRenderTarget(1,1,{depthBuffer:false}),blurB=new T.WebGLRenderTarget(1,1,{depthBuffer:false});
 const postScene=new T.Scene(),postCamera=new T.OrthographicCamera(-1,1,1,-1,0,1),quad=new T.Mesh(new T.PlaneGeometry(2,2));postScene.add(quad);
 const postVertex=`varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`;
 const blurMat=new T.ShaderMaterial({uniforms:{tInput:{value:null},uStep:{value:new T.Vector2()},uExtract:{value:1}},vertexShader:postVertex,fragmentShader:`uniform sampler2D tInput;uniform vec2 uStep;uniform float uExtract;varying vec2 vUv;vec3 read(vec2 uv){vec3 c=texture2D(tInput,uv).rgb;return mix(c,max(c-vec3(.10),vec3(0.)),uExtract);}void main(){vec3 c=read(vUv)*.227027;c+=(read(vUv+uStep*1.384615)+read(vUv-uStep*1.384615))*.316216;c+=(read(vUv+uStep*3.230769)+read(vUv-uStep*3.230769))*.070270;gl_FragColor=vec4(c,1.);}`,depthTest:false,depthWrite:false});
 const composite=new T.ShaderMaterial({uniforms:{tScene:{value:targetScene.texture},tBloom:{value:blurB.texture},uStrength:{value:1.4}},vertexShader:postVertex,fragmentShader:`uniform sampler2D tScene;uniform sampler2D tBloom;uniform float uStrength;varying vec2 vUv;void main(){vec3 c=texture2D(tScene,vUv).rgb+texture2D(tBloom,vUv).rgb*uStrength;float vignette=1.-smoothstep(.25,.9,length((vUv-.5)*vec2(1.,.8)))*.35;gl_FragColor=vec4(pow(max(c*vignette,vec3(0.)),vec3(1./2.2)),1.);}`,depthTest:false,depthWrite:false});
 // Also tried at d/3 (down from d/2) for the same lag report — reverted
 // alongside the pixel-ratio cut, same reasoning: made the bloom glow
 // visibly blockier without fixing the actual slowdown.
 function resize(){const w=container.clientWidth,h=container.clientHeight;if(!w||!h)return;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();const d=renderer.getPixelRatio();targetScene.setSize(Math.round(w*d),Math.round(h*d));blurA.setSize(Math.max(1,Math.round(w*d/2)),Math.max(1,Math.round(h*d/2)));blurB.setSize(blurA.width,blurA.height);pm.uniforms.uPixel.value=d;}
 const observer=new ResizeObserver(resize);observer.observe(container);resize();
 function draw(){renderer.setRenderTarget(targetScene);renderer.render(scene,camera);quad.material=blurMat;blurMat.uniforms.tInput.value=targetScene.texture;blurMat.uniforms.uExtract.value=1;blurMat.uniforms.uStep.value.set(1.5/blurA.width,0);renderer.setRenderTarget(blurA);renderer.render(postScene,postCamera);blurMat.uniforms.tInput.value=blurA.texture;blurMat.uniforms.uExtract.value=0;blurMat.uniforms.uStep.value.set(0,1.5/blurA.height);renderer.setRenderTarget(blurB);renderer.render(postScene,postCamera);quad.material=composite;renderer.setRenderTarget(null);renderer.render(postScene,postCamera);}
 renderer.setAnimationLoop(now=>{
 if(disposed)return;const dt=Math.min((now-last)/1000,.05);last=now;if(document.hidden)return;clock+=dt;if(state.motion)time+=dt;
 const follow=1-Math.exp(-18*dt),gentle=1-Math.exp(-7*dt);charge*=Math.exp(-3.8*dt);react+=(charge-react)*follow;
 if(demoStart>=0){const t=clock-demoStart;if(t>10){demoStart=-1;state.expansion=.12;target.x=.23;target.z=0;onMetrics({demoEnded:true});}else{target.y=Math.sin(t*.8)*2.1;target.x=.2+Math.sin(t*1.2)*.7;target.z=Math.sin(t*.6)*.4;state.expansion=.12+Math.sin(t*Math.PI/10)**2*.86;charge=.5;}}
 if(state.auto&&state.motion&&!state.hand&&demoStart<0){target.y+=dt*.11;target.x+=(.23-target.x)*dt*.3;}
 pose.x+=(target.x-pose.x)*follow;pose.y+=(target.y-pose.y)*follow;pose.z+=(target.z-pose.z)*follow;pose.zoom+=(target.zoom-pose.zoom)*follow;pose.expansion+=(state.expansion-pose.expansion)*gentle;pose.mode+=(state.mode-pose.mode)*gentle;
 const landscape=camera.aspect>1.25;const fit=landscape?1:Math.max(.7,1/camera.aspect*.7);
 camera.position.z=pose.zoom*fit;camera.position.x+=(mouse.x*.2-camera.position.x)*gentle;camera.position.y+=(.55+mouse.y*.12-camera.position.y)*gentle;camera.lookAt(0,-.28,0);
 rig.rotation.set(pose.x,pose.y,pose.z);rig.position.y=.15;assembly.scale.setScalar(1+react*.025);
 shell.rotation.y=-time*.04;shellMat.uniforms.uTime.value=time;shellMat.uniforms.uEnergy.value=state.energy;
 shell.material.opacity=1;shell.visible=pose.mode<.85;inner.rotation.set(time*.1,time*.18,0);inner.visible=pose.mode<.8;
 field.rotation.y=time*.035;pm.uniforms.uTime.value=time;pm.uniforms.uEnergy.value=state.energy;pm.uniforms.uMode.value=pose.mode;pm.uniforms.uExpansion.value=pose.expansion;pm.uniforms.uPulse.value=react;
 bands.forEach((b,i)=>{let d=b.userData;const modeFade=Math.abs(pose.mode-1);b.visible=modeFade>.03;for(const child of b.children){if(child.material.userData.baseOpacity===undefined)child.material.userData.baseOpacity=child.material.opacity;child.material.opacity=child.material.userData.baseOpacity*modeFade;}
 b.rotation.x=d.baseX+Math.sin(time*.25+i)*.04+pose.expansion*(i%2?1:-1)*.32;b.rotation.y=d.baseY+pose.expansion*(i%3-1)*.4;b.rotation.z=time*(i%2?1:-1)*(.055+i*.012)*(state.energy+.5)+i*.5;
 b.position.z=(i-5.5)*pose.expansion*.44;b.position.x=Math.cos(i*2.4)*pose.expansion*.2;b.position.y=Math.sin(i*2.4)*pose.expansion*.2;b.scale.setScalar(1+pose.expansion*.14+react*.025);});
 tracks.forEach((g,i)=>{const a=time*(.2+i*.11)*(i%2?1:-1);g.userData.bead.position.set(Math.cos(a)*g.userData.radius,Math.sin(a)*g.userData.radius,0);g.rotation.z=time*.02;g.userData.bead.scale.setScalar(1+react*2);g.scale.setScalar(1+pose.expansion*.12);});
 network.visible=pose.mode>.2&&pose.mode<1.8;network.material.opacity=Math.max(0,1-Math.abs(pose.mode-1))*.22;network.rotation.y=-time*.07;cage.visible=pose.mode>1.05;cage.material.opacity=Math.max(0,pose.mode-1)*.48;cage.rotation.set(.25,time*.05,.15);
 glow.material.opacity=.32+state.energy*.15+react*.12+(state.listening?Math.sin(clock*4)*.08:0);
 if(scanTime>=0){const progress=(clock-scanTime)/2.7;sweep.visible=progress<1;const y=-1.2+progress*2.4;shellMat.uniforms.uScan.value=y;sweep.position.y=y;sweep.scale.setScalar(Math.sqrt(Math.max(.001,1-(y/1.2)**2)));if(progress>=1){scanTime=-1;shellMat.uniforms.uScan.value=-3;onScanEnd();}}
 if(pulseStart>=0){const p=(clock-pulseStart)/.85;shock.visible=p<1;shock.position.y=.38;shock.quaternion.copy(camera.quaternion);shock.scale.setScalar(1.3+p*2.7);shockMat.opacity=(1-p)**2*.35*pulsePower;if(p>=1)pulseStart=-1;}
 composite.uniforms.uStrength.value=1.05+state.energy*.7+react*.15;draw();frames++;
 if(now-fpsStart>=1000){const fps=Math.round(frames*1000/(now-fpsStart));frames=0;fpsStart=now;if(fps<28)slowSeconds++;else slowSeconds=0;if(slowSeconds>=3&&quality>1){quality=1;renderer.setPixelRatio(1);resize();slowSeconds=0;}onMetrics({fps,energy:react,x:pose.x,y:pose.y,quality});}
 });
 function kick(amount=.4){charge=clamp(charge+amount,0,1.5);}
 function rotate(x,y,roll=0){x=target.y+Math.atan2(Math.sin(x-target.y),Math.cos(x-target.y));kick(Math.hypot(target.y-x,target.x-y)*.35);target.y=x;target.x=y;target.z=roll;}
 function setColor(hex){color.set(hex);colors.forEach(m=>m.color.copy(color));uniformColors.forEach(u=>u.value.copy(color));}
 function reset(){target.x=.23;target.y=0;target.z=0;target.zoom=10.6;mouse.set(0,0);pointer.set(10,10);}
 return {state,rotate,setColor,reset,kick,
 manipulate(delta){target.y+=delta.x;target.x=clamp(target.x+delta.y,-2,2);target.z=clamp(target.z+delta.roll,-2,2);kick(Math.hypot(delta.x,delta.y)*.6);},
 burst(power=1){pulseStart=clock;pulsePower=power;kick(power);},
 setMode(mode){state.mode={orbital:0,neural:1,lattice:2}[mode];kick(.7);},
 zoom(value){target.zoom=clamp(value,7.2,16);},
 changeZoom(delta){target.zoom=clamp(target.zoom+delta,7.2,16);},
 expand(v){const next=clamp(v,0,1);kick(Math.abs(next-state.expansion)*1.2);state.expansion=next;},
 drag(dx,dy){target.y+=dx*.009;target.x=clamp(target.x+dy*.009,-2,2);kick(Math.hypot(dx,dy)*.012);},
 cursor(x,y){mouse.set(x,y);pointer.set(x,y);},
 clearCursor(){mouse.set(0,0);pointer.set(10,10);},
 scan(){if(scanTime<0){scanTime=clock;kick(.8);return true;}return false;},
 preview(){if(demoStart>=0){demoStart=-1;state.expansion=.12;reset();return false;}demoStart=clock;return true;},
 stopPreview(){if(demoStart>=0){demoStart=-1;onMetrics({demoEnded:true});}},
 dispose(){disposed=true;renderer.setAnimationLoop(null);observer.disconnect();targetScene.dispose();blurA.dispose();blurB.dispose();scene.traverse(o=>{o.geometry?.dispose();if(o.material){for(const m of Array.isArray(o.material)?o.material:[o.material])m.dispose();}});glowMat.map.dispose();quad.geometry.dispose();blurMat.dispose();composite.dispose();renderer.dispose();}
 };
}

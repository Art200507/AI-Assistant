import * as THREE from "three";

const STATE_COLOR = {
  idle: new THREE.Color(0x4fd8ff),
  listening: new THREE.Color(0x9be9ff),
  thinking: new THREE.Color(0xffc15c),
  speaking: new THREE.Color(0xaef6ff),
};

const STATE_RING_SPEED = {
  idle: 0.15,
  listening: 0.4,
  thinking: 0.9,
  speaking: 0.6,
};

export function createAvatar(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.set(0, 0, 4.2);

  // --- core: deformable wireframe icosphere ---
  const coreGeo = new THREE.IcosahedronGeometry(1, 4);
  const posAttr = coreGeo.attributes.position;
  const basePositions = Float32Array.from(posAttr.array);
  const vertexPhase = new Float32Array(posAttr.count);
  for (let i = 0; i < vertexPhase.length; i++) vertexPhase[i] = Math.random() * Math.PI * 2;

  const coreMat = new THREE.MeshBasicMaterial({
    color: STATE_COLOR.idle,
    wireframe: true,
    transparent: true,
    opacity: 0.85,
  });
  const core = new THREE.Mesh(coreGeo, coreMat);
  scene.add(core);

  // --- inner glow sphere ---
  const glowGeo = new THREE.SphereGeometry(0.55, 24, 24);
  const glowMat = new THREE.MeshBasicMaterial({
    color: STATE_COLOR.idle,
    transparent: true,
    opacity: 0.35,
  });
  const glow = new THREE.Mesh(glowGeo, glowMat);
  scene.add(glow);

  // --- HUD rings (gyroscope style) ---
  const ringGroup = new THREE.Group();
  const ringDefs = [
    { r: 1.55, tube: 0.012, tiltX: Math.PI / 2.3, tiltY: 0.2, speed: 1 },
    { r: 1.75, tube: 0.008, tiltX: 0.4, tiltY: Math.PI / 2.6, speed: -0.7 },
    { r: 1.95, tube: 0.006, tiltX: 1.1, tiltY: 0.9, speed: 0.5 },
  ];
  const rings = ringDefs.map((def) => {
    const geo = new THREE.TorusGeometry(def.r, def.tube, 8, 96);
    const mat = new THREE.MeshBasicMaterial({
      color: STATE_COLOR.idle,
      transparent: true,
      opacity: 0.5,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = def.tiltX;
    mesh.rotation.y = def.tiltY;
    mesh.userData.speed = def.speed;
    ringGroup.add(mesh);
    return mesh;
  });
  scene.add(ringGroup);

  // --- particle shell ---
  const N = 500;
  const shellGeo = new THREE.BufferGeometry();
  const shellPos = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const r = 2.15 + Math.random() * 0.15;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    shellPos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    shellPos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    shellPos[i * 3 + 2] = r * Math.cos(phi);
  }
  shellGeo.setAttribute("position", new THREE.BufferAttribute(shellPos, 3));
  const shellMat = new THREE.PointsMaterial({
    color: STATE_COLOR.idle,
    size: 0.02,
    transparent: true,
    opacity: 0.55,
  });
  const shell = new THREE.Points(shellGeo, shellMat);
  scene.add(shell);

  let currentState = "idle";
  let targetAmplitude = 0;
  let smoothedAmplitude = 0;
  let lastStateChangeTime = performance.now();
  let elapsed = 0;
  let lastFrameTime = performance.now();

  function resize() {
    const parent = canvas.parentElement;
    const w = parent.clientWidth;
    const h = parent.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);
  resize();

  function setState(state, amplitude = 0) {
    if (state !== currentState) {
      currentState = state;
      lastStateChangeTime = performance.now();
    }
    targetAmplitude = amplitude;
  }

  function animate() {
    requestAnimationFrame(animate);
    const now = performance.now();
    const dt = Math.min(0.05, (now - lastFrameTime) / 1000);
    lastFrameTime = now;
    elapsed += dt;
    const t = elapsed;

    smoothedAmplitude += (targetAmplitude - smoothedAmplitude) * Math.min(1, dt * 8);

    const color = STATE_COLOR[currentState] || STATE_COLOR.idle;
    coreMat.color.lerp(color, 0.08);
    glowMat.color.lerp(color, 0.08);
    shellMat.color.lerp(color, 0.08);
    for (const r of rings) r.material.color.lerp(color, 0.08);

    // breathing / speaking deformation on the core
    const baseAmp = currentState === "speaking" ? smoothedAmplitude : currentState === "listening" ? 0.25 : currentState === "thinking" ? 0.18 : 0.08;
    const positions = coreGeo.attributes.position.array;
    for (let i = 0; i < vertexPhase.length; i++) {
      const idx = i * 3;
      const bx = basePositions[idx];
      const by = basePositions[idx + 1];
      const bz = basePositions[idx + 2];
      const len = Math.sqrt(bx * bx + by * by + bz * bz) || 1;
      const nx = bx / len, ny = by / len, nz = bz / len;
      const wobble = Math.sin(t * (currentState === "thinking" ? 6 : 2.2) + vertexPhase[i]) * baseAmp * 0.18;
      positions[idx] = bx + nx * wobble;
      positions[idx + 1] = by + ny * wobble;
      positions[idx + 2] = bz + nz * wobble;
    }
    coreGeo.attributes.position.needsUpdate = true;

    const pulseScale = 1 + Math.sin(t * 1.6) * 0.03 + smoothedAmplitude * 0.12;
    glow.scale.setScalar(pulseScale);
    glowMat.opacity = 0.28 + smoothedAmplitude * 0.35;

    core.rotation.y += dt * 0.12;
    core.rotation.x += dt * 0.05;

    const ringSpeed = STATE_RING_SPEED[currentState] || 0.2;
    for (const r of rings) {
      r.rotation.z += dt * ringSpeed * r.userData.speed * (1 + smoothedAmplitude);
    }
    shell.rotation.y += dt * 0.03;

    renderer.render(scene, camera);
  }
  animate();

  return { setState, resize };
}

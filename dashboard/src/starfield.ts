/**
 * GPU star field for the survey map.
 *
 * The 2D-canvas renderer projected and drew every star on the CPU each
 * frame: ~12k object allocations, a full sort, and a beginPath/arc/fill plus
 * a `fillText` glyph rasterisation per star. That is why frame rate scaled
 * with the number of stars on screen and why the discrete GPU sat idle.
 *
 * Here the star positions live in GPU buffers and are uploaded only when the
 * survey data actually changes. Camera movement updates a handful of
 * uniforms, so panning, zooming and orbiting cost no per-star CPU work at
 * all and the whole field is one `drawArrays` call.
 *
 * The projection below is a deliberate port of the canvas renderer's maths,
 * not an improvement on it: the two layers draw into the same screen space
 * and must agree exactly, or the GPU stars would drift away from the 2D
 * overlay's grid, rings and selection markers.
 */

export interface StarPoint {
  /** Sun-centred galactic coordinates, parsecs. */
  x: number;
  y: number;
  z: number;
  /** sRGB 0-255. */
  r: number;
  g: number;
  b: number;
  /** Base radius in CSS pixels before perspective scaling. */
  size: number;
  /** 0 = normal, 1 = dimmed (filtered out but still shown as context). */
  dimmed: number;
}

export interface StarfieldCamera {
  centreX: number;
  centreY: number;
  mapRadius: number;
  maxDistance: number;
  rotationX: number;
  rotationY: number;
  /** Device pixel ratio the canvas is currently sized for. */
  pixelRatio: number;
  width: number;
  height: number;
}

const VERTEX_SHADER = `#version 300 es
precision highp float;

in vec3 aPosition;
in vec3 aColor;
in float aSize;
in float aDimmed;

uniform vec2 uCentre;
uniform vec2 uViewport;
uniform float uMapRadius;
uniform float uMaxDistance;
uniform vec2 uRotation;      // x = pitch, y = yaw
uniform float uPixelRatio;

out vec3 vColor;
out float vAlpha;

void main() {
  float cosY = cos(uRotation.y);
  float sinY = sin(uRotation.y);
  float cosX = cos(uRotation.x);
  float sinX = sin(uRotation.x);

  // Same rotation order as the canvas renderer: yaw about Y, then pitch.
  float x1 = aPosition.x * cosY - aPosition.z * sinY;
  float z1 = aPosition.x * sinY + aPosition.z * cosY;
  float y1 = aPosition.y * cosX - z1 * sinX;
  float z2 = aPosition.y * sinX + z1 * cosX;

  float perspective = 1.0 / (1.0 + (z2 / uMaxDistance) * 0.22);
  vec2 screen = uCentre + vec2(
    (x1 / uMaxDistance) * uMapRadius * perspective,
    (y1 / uMaxDistance) * uMapRadius * perspective
  );

  // CSS pixels -> clip space. Y is flipped because canvas Y grows downward.
  vec2 clip = vec2(
    (screen.x / uViewport.x) * 2.0 - 1.0,
    1.0 - (screen.y / uViewport.y) * 2.0
  );
  gl_Position = vec4(clip, 0.0, 1.0);

  // Nearer stars are drawn slightly larger, matching the 2D renderer's
  // perspective term. The floor keeps distant stars from vanishing.
  gl_PointSize = max(aSize * perspective, 1.0) * uPixelRatio * 2.0;

  vColor = aColor;
  vAlpha = aDimmed > 0.5 ? 0.28 : 0.95;
}
`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;

in vec3 vColor;
in float vAlpha;
out vec4 outColor;

void main() {
  // Round, softly-edged point. Discarding outside the disc keeps stars from
  // rendering as squares and gives free antialiasing.
  vec2 offset = gl_PointCoord - vec2(0.5);
  float dist = length(offset);
  if (dist > 0.5) discard;
  float edge = smoothstep(0.5, 0.34, dist);
  // A small bright core keeps points legible when they are only 1-2px wide.
  float core = smoothstep(0.34, 0.0, dist) * 0.45;
  outColor = vec4(vColor + core, vAlpha * edge);
}
`;

function compile(gl: WebGL2RenderingContext, type: number, source: string) {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("could not create shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`shader compile failed: ${log}`);
  }
  return shader;
}

export interface Starfield {
  /** Upload a new star set. Call only when the data changes. */
  setStars(stars: StarPoint[]): void;
  /** Draw the current star set from this camera. Cheap; call per frame. */
  render(camera: StarfieldCamera): void;
  resize(width: number, height: number, pixelRatio: number): void;
  dispose(): void;
  readonly starCount: number;
}

/**
 * Create the GPU renderer, or return null when WebGL2 is unavailable.
 *
 * Returning null rather than throwing lets the caller keep the existing
 * canvas renderer as a fallback: a machine without WebGL2 should still get a
 * working map, just a slower one.
 */
export function createStarfield(canvas: HTMLCanvasElement): Starfield | null {
  const gl = canvas.getContext("webgl2", {
    alpha: true,
    antialias: false, // points are antialiased in the shader; MSAA is wasted here
    depth: false,
    premultipliedAlpha: false,
    powerPreference: "high-performance",
  }) as WebGL2RenderingContext | null;
  if (!gl) return null;

  let program: WebGLProgram | null = null;
  try {
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    program = gl.createProgram();
    if (!program) throw new Error("could not create program");
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`link failed: ${gl.getProgramInfoLog(program)}`);
    }
  } catch (error) {
    console.warn("[starfield] falling back to canvas rendering:", error);
    return null;
  }

  const vao = gl.createVertexArray();
  const buffer = gl.createBuffer();
  if (!vao || !buffer) return null;

  const loc = {
    position: gl.getAttribLocation(program, "aPosition"),
    color: gl.getAttribLocation(program, "aColor"),
    size: gl.getAttribLocation(program, "aSize"),
    dimmed: gl.getAttribLocation(program, "aDimmed"),
    centre: gl.getUniformLocation(program, "uCentre"),
    viewport: gl.getUniformLocation(program, "uViewport"),
    mapRadius: gl.getUniformLocation(program, "uMapRadius"),
    maxDistance: gl.getUniformLocation(program, "uMaxDistance"),
    rotation: gl.getUniformLocation(program, "uRotation"),
    pixelRatio: gl.getUniformLocation(program, "uPixelRatio"),
  };

  // One interleaved buffer: x, y, z, r, g, b, size, dimmed.
  const STRIDE_FLOATS = 8;
  const STRIDE = STRIDE_FLOATS * 4;
  let count = 0;

  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.enableVertexAttribArray(loc.position);
  gl.vertexAttribPointer(loc.position, 3, gl.FLOAT, false, STRIDE, 0);
  gl.enableVertexAttribArray(loc.color);
  gl.vertexAttribPointer(loc.color, 3, gl.FLOAT, false, STRIDE, 12);
  gl.enableVertexAttribArray(loc.size);
  gl.vertexAttribPointer(loc.size, 1, gl.FLOAT, false, STRIDE, 24);
  gl.enableVertexAttribArray(loc.dimmed);
  gl.vertexAttribPointer(loc.dimmed, 1, gl.FLOAT, false, STRIDE, 28);
  gl.bindVertexArray(null);

  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  // Additive-ish blending: overlapping stars brighten rather than occlude,
  // which is also why the depth sort the CPU renderer needed is unnecessary.
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

  return {
    get starCount() {
      return count;
    },
    setStars(stars: StarPoint[]) {
      const data = new Float32Array(stars.length * STRIDE_FLOATS);
      for (let i = 0; i < stars.length; i += 1) {
        const s = stars[i];
        const o = i * STRIDE_FLOATS;
        data[o] = s.x;
        data[o + 1] = s.y;
        data[o + 2] = s.z;
        data[o + 3] = s.r / 255;
        data[o + 4] = s.g / 255;
        data[o + 5] = s.b / 255;
        data[o + 6] = s.size;
        data[o + 7] = s.dimmed;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      count = stars.length;
    },
    resize(width: number, height: number, pixelRatio: number) {
      const w = Math.max(1, Math.round(width * pixelRatio));
      const h = Math.max(1, Math.round(height * pixelRatio));
      // Only touch the backing store when it actually changes: assigning
      // canvas.width every frame reallocates and clears it, which is one of
      // the costs this renderer exists to remove.
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      gl.viewport(0, 0, w, h);
    },
    render(camera: StarfieldCamera) {
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (!count || !program) return;
      gl.useProgram(program);
      gl.bindVertexArray(vao);
      gl.uniform2f(loc.centre, camera.centreX, camera.centreY);
      gl.uniform2f(loc.viewport, camera.width, camera.height);
      gl.uniform1f(loc.mapRadius, camera.mapRadius);
      gl.uniform1f(loc.maxDistance, camera.maxDistance);
      gl.uniform2f(loc.rotation, camera.rotationX, camera.rotationY);
      gl.uniform1f(loc.pixelRatio, camera.pixelRatio);
      gl.drawArrays(gl.POINTS, 0, count);
      gl.bindVertexArray(null);
    },
    dispose() {
      gl.deleteBuffer(buffer);
      gl.deleteVertexArray(vao);
      if (program) gl.deleteProgram(program);
    },
  };
}

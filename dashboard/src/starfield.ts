/**
 * GPU star field for the survey map.
 *
 * The 2D-canvas renderer projected and drew every star on the CPU each
 * frame: ~12k object allocations, a full sort, and a beginPath/arc/fill plus
 * a `fillText` glyph rasterisation per star. That is why frame rate scaled
 * with the number of stars on screen and why the discrete GPU sat idle.
 *
 * Star positions live in GPU buffers and are uploaded only when the survey
 * data changes, so camera movement costs a few uniform updates and the whole
 * field is one `drawArrays` call.
 *
 * **The markers are the real status glyphs, not generic points.** Every
 * status marker is rasterised once into a texture atlas by the same code
 * that draws the legend, and each star samples its own tile. Drawing plain
 * discs instead would have been faster to write and wrong: the map has to
 * mean the same thing as the key beside it.
 *
 * Two details are deliberate rather than incidental:
 *
 * * **Point size does not vary with depth.** The canvas renderer draws every
 *   marker at a fixed pixel size, so scaling by perspective here would make
 *   near stars loom and turn a survey chart into a game camera.
 * * **The projection is a port of the canvas maths, not an improvement.**
 *   Both layers draw into the same screen space and must agree exactly, or
 *   the GPU stars drift away from the 2D grid, rings and selection markers.
 */

export interface StarPoint {
  /** Sun-centred galactic coordinates, parsecs. */
  x: number;
  y: number;
  z: number;
  /** Index of this star's marker in the atlas. */
  tile: number;
}

export interface StarAtlas {
  image: HTMLCanvasElement;
  columns: number;
  rows: number;
  /** Tile edge length in CSS pixels; also the on-screen marker size. */
  tileCssSize: number;
}

export interface StarfieldCamera {
  centreX: number;
  centreY: number;
  mapRadius: number;
  maxDistance: number;
  rotationX: number;
  rotationY: number;
  pixelRatio: number;
  width: number;
  height: number;
}

const VERTEX_SHADER = `#version 300 es
precision highp float;

in vec3 aPosition;
in float aTile;

uniform vec2 uCentre;
uniform vec2 uViewport;
uniform float uMapRadius;
uniform float uMaxDistance;
uniform vec2 uRotation;      // x = pitch, y = yaw
uniform float uPixelRatio;
uniform float uTileSize;     // CSS pixels

out float vTile;

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
  gl_Position = vec4(
    (screen.x / uViewport.x) * 2.0 - 1.0,
    1.0 - (screen.y / uViewport.y) * 2.0,
    0.0,
    1.0
  );

  // Constant on-screen size: perspective moves stars, it does not resize
  // their markers. This is what keeps the map reading as a chart.
  gl_PointSize = uTileSize * uPixelRatio;
  vTile = aTile;
}
`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;

in float vTile;
uniform sampler2D uAtlas;
uniform vec2 uAtlasGrid;     // columns, rows

out vec4 outColor;

void main() {
  float column = mod(vTile, uAtlasGrid.x);
  float row = floor(vTile / uAtlasGrid.x);
  // gl_PointCoord runs 0..1 across the point, top-left origin, which matches
  // the atlas canvas layout.
  vec2 uv = (vec2(column, row) + gl_PointCoord) / uAtlasGrid;
  vec4 texel = texture(uAtlas, uv);
  if (texel.a < 0.01) discard;
  outColor = texel;
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
  /** Upload the marker atlas. Call whenever the atlas is rebuilt. */
  setAtlas(atlas: StarAtlas): void;
  /** Upload a new star set. Call only when the data changes. */
  setStars(stars: StarPoint[]): void;
  /** Draw the current star set from this camera. Cheap; call per frame. */
  render(camera: StarfieldCamera): void;
  /** Clear without drawing, for views the shader does not implement. */
  clear(width: number, height: number, pixelRatio: number): void;
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
    antialias: false,
    depth: false,
    premultipliedAlpha: true,
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
  const texture = gl.createTexture();
  if (!vao || !buffer || !texture) return null;

  const loc = {
    position: gl.getAttribLocation(program, "aPosition"),
    tile: gl.getAttribLocation(program, "aTile"),
    centre: gl.getUniformLocation(program, "uCentre"),
    viewport: gl.getUniformLocation(program, "uViewport"),
    mapRadius: gl.getUniformLocation(program, "uMapRadius"),
    maxDistance: gl.getUniformLocation(program, "uMaxDistance"),
    rotation: gl.getUniformLocation(program, "uRotation"),
    pixelRatio: gl.getUniformLocation(program, "uPixelRatio"),
    tileSize: gl.getUniformLocation(program, "uTileSize"),
    atlas: gl.getUniformLocation(program, "uAtlas"),
    atlasGrid: gl.getUniformLocation(program, "uAtlasGrid"),
  };

  // Interleaved: x, y, z, tile.
  const STRIDE_FLOATS = 4;
  const STRIDE = STRIDE_FLOATS * 4;
  let count = 0;
  let grid = { columns: 1, rows: 1, tileCssSize: 26 };
  let hasAtlas = false;

  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.enableVertexAttribArray(loc.position);
  gl.vertexAttribPointer(loc.position, 3, gl.FLOAT, false, STRIDE, 0);
  gl.enableVertexAttribArray(loc.tile);
  gl.vertexAttribPointer(loc.tile, 1, gl.FLOAT, false, STRIDE, 12);
  gl.bindVertexArray(null);

  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  // Canvas-sourced textures arrive premultiplied, so blend accordingly;
  // using SRC_ALPHA here would darken every marker's edges.
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

  const applyViewport = (
    width: number,
    height: number,
    pixelRatio: number,
  ) => {
    const w = Math.max(1, Math.round(width * pixelRatio));
    const h = Math.max(1, Math.round(height * pixelRatio));
    // Only touch the backing store when it changes: assigning canvas.width
    // every frame reallocates and clears it.
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
  };

  return {
    get starCount() {
      return count;
    },
    setAtlas(atlas: StarAtlas) {
      grid = {
        columns: Math.max(1, atlas.columns),
        rows: Math.max(1, atlas.rows),
        tileCssSize: atlas.tileCssSize,
      };
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        atlas.image,
      );
      // Clamp so a tile never bleeds into its neighbour; linear so markers
      // stay smooth when the device pixel ratio is fractional.
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.bindTexture(gl.TEXTURE_2D, null);
      hasAtlas = true;
    },
    setStars(stars: StarPoint[]) {
      const data = new Float32Array(stars.length * STRIDE_FLOATS);
      for (let i = 0; i < stars.length; i += 1) {
        const s = stars[i];
        const o = i * STRIDE_FLOATS;
        data[o] = s.x;
        data[o + 1] = s.y;
        data[o + 2] = s.z;
        data[o + 3] = s.tile;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      count = stars.length;
    },
    resize: applyViewport,
    clear(width: number, height: number, pixelRatio: number) {
      applyViewport(width, height, pixelRatio);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    },
    render(camera: StarfieldCamera) {
      applyViewport(camera.width, camera.height, camera.pixelRatio);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (!count || !program || !hasAtlas) return;
      gl.useProgram(program);
      gl.bindVertexArray(vao);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(loc.atlas, 0);
      gl.uniform2f(loc.atlasGrid, grid.columns, grid.rows);
      gl.uniform1f(loc.tileSize, grid.tileCssSize);
      gl.uniform2f(loc.centre, camera.centreX, camera.centreY);
      gl.uniform2f(loc.viewport, camera.width, camera.height);
      gl.uniform1f(loc.mapRadius, camera.mapRadius);
      gl.uniform1f(loc.maxDistance, camera.maxDistance);
      gl.uniform2f(loc.rotation, camera.rotationX, camera.rotationY);
      gl.uniform1f(loc.pixelRatio, camera.pixelRatio);
      gl.drawArrays(gl.POINTS, 0, count);
      gl.bindVertexArray(null);
      gl.bindTexture(gl.TEXTURE_2D, null);
    },
    dispose() {
      gl.deleteBuffer(buffer);
      gl.deleteVertexArray(vao);
      gl.deleteTexture(texture);
      if (program) gl.deleteProgram(program);
    },
  };
}

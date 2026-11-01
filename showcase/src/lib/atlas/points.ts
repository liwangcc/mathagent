import type { Box, PointBuffers } from "./targets"
import type { Frame } from "./timeline"

/* Draws every ink point in one call. Each point carries its position in every stage; the vertex
 * shader blends between the two stages of the current frame, so the CPU only sets uniforms.
 * GLSL ES 1.00 keeps WebGL 1 and 2 on the same shaders. */

export type RGB = [number, number, number]

export interface Palette {
  pen: RGB
  ink: RGB
  dim: RGB
  correct: RGB
  partial: RGB
  wrong: RGB
  pending: RGB
  accent: RGB
}

export interface PointFrame {
  frame: Frame
  time: number
  plate: Box
  pointer: { x: number; y: number; strength: number }
  /** 0 under reduced motion: no drift, no trembling. */
  motion: number
  pointSize: number
}

export interface PointRenderer {
  upload(buffers: PointBuffers): void
  resize(width: number, height: number, pixelRatio: number): void
  draw(state: PointFrame, palette: Palette): void
  dispose(): void
}

const VERTEX = `
precision highp float;
attribute vec4 a_p01;
attribute vec4 a_p23;
attribute vec4 a_p45;
attribute vec4 a_p6w;
attribute vec4 a_meta;

uniform vec2 u_resolution;
uniform float u_pixelRatio;
uniform float u_pointSize;
uniform float u_from;
uniform float u_to;
uniform float u_mix;
uniform float u_write;
uniform float u_time;
uniform float u_motion;
uniform vec3 u_pointer;
uniform vec4 u_plate;
uniform vec3 u_pen;
uniform vec3 u_ink;
uniform vec3 u_dim;
uniform vec3 u_correct;
uniform vec3 u_partial;
uniform vec3 u_wrong;
uniform vec3 u_pending;
uniform vec3 u_accent;

varying vec4 v_color;

vec2 stagePosition(float stage) {
  if (stage < 0.5) return a_p01.xy;
  if (stage < 1.5) return a_p01.zw;
  if (stage < 2.5) return a_p23.xy;
  if (stage < 3.5) return a_p23.zw;
  if (stage < 4.5) return a_p45.xy;
  if (stage < 5.5) return a_p45.zw;
  return a_p6w.xy;
}

vec3 verdictColor(float row) {
  if (row < 0.5) return u_wrong;
  if (row < 1.5) return u_correct;
  if (row < 2.5) return u_partial;
  return u_pending;
}

float seedTwinkle() {
  return fract(a_meta.y * 13.7) * 1.4;
}

/* Colour, opacity and relative size of a point in one stage. */
void stageLook(float stage, out vec3 color, out float alpha, out float size) {
  float row = a_meta.x;
  float glow = a_p6w.z;
  float matched = a_p6w.w;
  if (stage < 0.5) {
    color = u_pen; alpha = 0.92; size = 1.05;
  } else if (stage < 1.5) {
    color = row > 2.5 ? u_dim : u_ink; alpha = 0.9; size = 1.0;
  } else if (stage < 2.5) {
    color = verdictColor(row); alpha = 0.95; size = 1.0;
  } else if (stage < 3.5) {
    // Brighter stars are knowledge points with more mistakes; the heaviest warm toward amber.
    vec3 star = mix(u_dim, u_ink, glow);
    color = mix(star, u_partial, smoothstep(0.72, 1.0, glow) * 0.7);
    alpha = glow > 0.0 ? 0.42 + 0.58 * glow : 0.26;
    size = glow > 0.0 ? 0.8 + 0.95 * glow : 0.75;
    // Stars twinkle, slowly and each in its own time.
    alpha *= 1.0 - 0.22 * u_motion * (0.5 + 0.5 * sin(u_time * (1.1 + seedTwinkle()) + a_meta.y * 60.0));
  } else if (stage < 4.5) {
    color = u_ink; alpha = 0.6; size = 0.82;
  } else if (stage < 5.5) {
    color = matched > 0.5 ? u_accent : u_ink;
    alpha = matched > 0.5 ? 1.0 : 0.7;
    size = matched > 0.5 ? 1.6 : 0.88;
  } else {
    color = u_ink; alpha = 0.92; size = 1.05;
  }
}

void main() {
  float row = a_meta.x;
  float seed = a_meta.y;
  vec2 from = stagePosition(u_from);
  vec2 to = stagePosition(u_to);

  bool scanning = u_from < 0.5 && u_to > 0.5;
  bool merging = u_from > 3.5 && u_from < 4.5 && u_to > 4.5;
  // The scan reads the page top to bottom; merging sweeps the ledger left to right; the rest start at random.
  float delay = scanning ? a_meta.w : merging ? clamp((from.x - u_plate.x) / u_plate.z, 0.0, 1.0) : seed;
  float start = clamp((u_mix - delay * 0.45) / 0.55, 0.0, 1.0);
  float m = start * start * (3.0 - 2.0 * start);
  // Behind the scan line the ink turns to light at once, before it has finished moving.
  float colorMix = scanning ? smoothstep(0.0, 1.0, (u_mix - delay * 0.45) / 0.08) : m;

  vec2 travel = to - from;
  float arc = sin(3.14159265 * m);
  vec2 position = mix(from, to, m) + vec2(-travel.y, travel.x) * (seed - 0.5) * 0.42 * arc;
  position += vec2(sin(seed * 91.0 + u_time * 0.6), cos(seed * 53.0 + u_time * 0.5))
    * 22.0 * arc * smoothstep(40.0, 180.0, length(travel)) * u_motion;

  // The illegible answer keeps trembling while it is read and judged: nobody should guess at it.
  float unsettled = 0.0;
  if (row > 2.5) {
    if (u_from > 0.5 && u_from < 2.5) unsettled += 1.0 - m;
    if (u_to > 0.5 && u_to < 2.5) unsettled += m;
  }
  position += vec2(sin(u_time * 9.0 + seed * 40.0), cos(u_time * 7.0 + seed * 23.0)) * 1.7 * unsettled * u_motion;
  position += vec2(sin(u_time * 0.9 + seed * 17.0), cos(u_time * 0.8 + seed * 29.0)) * 0.45 * u_motion;

  // Ink shies away from the pointer, like dust from a breath.
  vec2 away = position - u_pointer.xy;
  float gap = length(away);
  if (u_pointer.z > 0.0 && gap < 70.0 && gap > 0.001) {
    position += away / gap * (70.0 - gap) * 0.55 * u_pointer.z;
  }

  vec3 c0; float a0; float s0;
  vec3 c1; float a1; float s1;
  stageLook(u_from, c0, a0, s0);
  stageLook(u_to, c1, a1, s1);
  vec3 color = mix(c0, c1, colorMix);
  float alpha = mix(a0, a1, colorMix);
  float size = mix(s0, s1, m);

  // Writing: a point appears when the pen reaches it; the newest ink is the nib.
  if (u_from < 0.5 && u_to < 0.5) {
    float ahead = a_meta.z - u_write;
    alpha *= 1.0 - smoothstep(-0.0005, 0.0025, ahead);
    float nib = (1.0 - smoothstep(0.0, 0.01, -ahead)) * step(ahead, 0.0) * step(u_write, 0.995);
    size *= 1.0 + 1.6 * nib;
  }

  gl_Position = vec4(position.x / u_resolution.x * 2.0 - 1.0, 1.0 - position.y / u_resolution.y * 2.0, 0.0, 1.0);
  gl_PointSize = max(1.0, u_pointSize * size * u_pixelRatio);
  v_color = vec4(color * alpha, alpha);
}
`

const FRAGMENT = `
precision mediump float;
varying vec4 v_color;
void main() {
  float edge = 1.0 - smoothstep(0.3, 0.5, length(gl_PointCoord - 0.5));
  if (edge <= 0.0) discard;
  gl_FragColor = v_color * edge;
}
`

const ATTRIBUTES = ["a_p01", "a_p23", "a_p45", "a_p6w", "a_meta"] as const
const UNIFORMS = [
  "u_resolution", "u_pixelRatio", "u_pointSize", "u_from", "u_to", "u_mix", "u_write", "u_time", "u_motion",
  "u_pointer", "u_plate", "u_pen", "u_ink", "u_dim", "u_correct", "u_partial", "u_wrong", "u_pending", "u_accent",
] as const

function compile(gl: WebGLRenderingContext, type: number, source: string) {
  const shader = gl.createShader(type)
  if (!shader) throw new Error("shader allocation failed")
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) ?? "shader compile failed")
  }
  return shader
}

/** Returns null when the browser has no WebGL; the section then shows its text alone. */
export function createPointRenderer(canvas: HTMLCanvasElement): PointRenderer | null {
  const options: WebGLContextAttributes = { alpha: true, antialias: false, premultipliedAlpha: true, powerPreference: "high-performance" }
  const gl = (canvas.getContext("webgl2", options) ?? canvas.getContext("webgl", options)) as WebGLRenderingContext | null
  if (!gl) return null

  const program = gl.createProgram()
  if (!program) return null
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX))
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT))
  gl.linkProgram(program)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) ?? "link failed")
  gl.useProgram(program)

  const uniforms = Object.fromEntries(UNIFORMS.map((name) => [name, gl.getUniformLocation(program, name)])) as Record<
    (typeof UNIFORMS)[number],
    WebGLUniformLocation | null
  >
  const buffers = ATTRIBUTES.map((name) => {
    const buffer = gl.createBuffer()
    const location = gl.getAttribLocation(program, name)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.enableVertexAttribArray(location)
    gl.vertexAttribPointer(location, 4, gl.FLOAT, false, 0, 0)
    return buffer
  })
  gl.enable(gl.BLEND)
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
  gl.clearColor(0, 0, 0, 0)

  let count = 0
  let pixelRatio = 1

  return {
    upload(points) {
      const arrays = [points.p01, points.p23, points.p45, points.p6w, points.meta]
      arrays.forEach((array, index) => {
        gl.bindBuffer(gl.ARRAY_BUFFER, buffers[index])
        gl.bufferData(gl.ARRAY_BUFFER, array, gl.STATIC_DRAW)
      })
      count = points.meta.length / 4
    },
    resize(width, height, ratio) {
      pixelRatio = ratio
      canvas.width = Math.round(width * ratio)
      canvas.height = Math.round(height * ratio)
      gl.viewport(0, 0, canvas.width, canvas.height)
      gl.uniform2f(uniforms.u_resolution, width, height)
    },
    draw(state, palette) {
      const { frame, plate, pointer } = state
      gl.uniform1f(uniforms.u_pixelRatio, pixelRatio)
      gl.uniform1f(uniforms.u_pointSize, state.pointSize)
      gl.uniform1f(uniforms.u_from, frame.from)
      gl.uniform1f(uniforms.u_to, frame.to)
      gl.uniform1f(uniforms.u_mix, frame.mix)
      gl.uniform1f(uniforms.u_write, frame.write)
      gl.uniform1f(uniforms.u_time, state.time)
      gl.uniform1f(uniforms.u_motion, state.motion)
      gl.uniform3f(uniforms.u_pointer, pointer.x, pointer.y, pointer.strength)
      gl.uniform4f(uniforms.u_plate, plate.x, plate.y, plate.w, plate.h)
      gl.uniform3fv(uniforms.u_pen, palette.pen)
      gl.uniform3fv(uniforms.u_ink, palette.ink)
      gl.uniform3fv(uniforms.u_dim, palette.dim)
      gl.uniform3fv(uniforms.u_correct, palette.correct)
      gl.uniform3fv(uniforms.u_partial, palette.partial)
      gl.uniform3fv(uniforms.u_wrong, palette.wrong)
      gl.uniform3fv(uniforms.u_pending, palette.pending)
      gl.uniform3fv(uniforms.u_accent, palette.accent)
      gl.clear(gl.COLOR_BUFFER_BIT)
      if (count) gl.drawArrays(gl.POINTS, 0, count)
    },
    dispose() {
      buffers.forEach((buffer) => gl.deleteBuffer(buffer))
      gl.deleteProgram(program)
    },
  }
}

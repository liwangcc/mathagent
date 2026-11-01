// Each courseware has its own state and mathematical renderer. The same
// definitions are consumed by the Vue preview and serialized into offline HTML.
const clamp = (v, min, max) => {
  const value = Number(v)
  return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min
}

export const COURSEWARE_MODELS = {
  fractions: {
    key: 'fractions', stage: 'primary', chapter: '分数的认识与运算', knowledgePoint: '同分母分数大小比较',
    objective: '理解分母表示平均分成几份，分子表示取了几份。', misconception: '分母越大，分数一定越大。',
    steps: ['观察两条同长度分数条', '拖动分子改变填色', '先猜大小，再显示规律', '用分母相同的规律解释'],
    interactionState: { aNumerator: 3, bNumerator: 5, denominator: 8, guessed: false, revealed: false },
    exercise: { question: '把 A 设为 6/8，B 设为 4/8，哪个分数大？为什么？', answer: '6/8 > 4/8。分母相同，比较分子；6 > 4，所以 A 的填色份数更多。' },
    checkpoints: ['两条分数条长度相同', '分子改变时填色与文本同步', '能说出分母相同时比较分子'], offlineFileName: 'P05_分数的认识与运算.html',
    render(state) {
      const denominator = clamp(state.denominator, 2, 12); const a = clamp(state.aNumerator, 0, denominator); const b = clamp(state.bNumerator, 0, denominator)
      return { denominator, a, b, result: a === b ? '=' : a > b ? '>' : '<', aFill: a / denominator, bFill: b / denominator }
    },
  },
  linear: {
    key: 'linear', stage: 'junior', chapter: '函数基础与一次函数', knowledgePoint: '一次函数图象',
    objective: '观察斜率 k 与截距 b 如何决定直线的位置。', misconception: '改变 k 只会让公式文字变化。',
    steps: ['观察坐标系中的 y=kx+b', '滑动 k 看直线绕截距倾斜', '滑动 b 看整条直线上下平移', '解释斜率和截距'],
    interactionState: { k: 1, b: 0, guessed: false, revealed: false },
    exercise: { question: '当 k = −1、b = 2 时，直线与 y 轴交于哪里？x = 2 时 y 是多少？', answer: '交点是 B（0, 2）；代入 y = −x + 2，x = 2 时 y = 0。' },
    checkpoints: ['正负斜率均可见', '截距点在 y 轴上', '公式、刻度、直线来自同一状态'], offlineFileName: 'J08_函数基础与一次函数.html',
    render(state) {
      const k = clamp(state.k, -3, 3); const b = clamp(state.b, -4, 4); const xMin = -5; const xMax = 5; const yMin = -6; const yMax = 6
      // Keep the mathematical plot area aligned with the 440×260 SVG viewBox:
      // x ∈ [-5, 5] maps to [40, 400], y ∈ [-6, 6] maps to [240, 20].
      const sx = x => 40 + (x - xMin) / (xMax - xMin) * 360; const sy = y => 130 - y * 220 / (yMax - yMin)
      return { k, b, xMin, xMax, yMin, yMax, sx, sy, line: { x1: sx(xMin), y1: sy(k * xMin + b), x2: sx(xMax), y2: sy(k * xMax + b) }, intercept: { x: sx(0), y: sy(b) } }
    },
  },
  unitCircle: {
    key: 'unitCircle', stage: 'senior', chapter: '三角函数与恒等变换', knowledgePoint: '单位圆与三角函数',
    objective: '在单位圆上理解 sin θ、cos θ 是点坐标的投影。', misconception: 'sin 和 cos 只是两个孤立的答案。',
    steps: ['旋转半径线改变角度', '观察横纵投影和坐标', '显示规律', '验证 sin²θ+cos²θ=1'],
    interactionState: { angle: 45, guessed: false, revealed: false },
    exercise: { question: 'θ = 150° 时，sin θ 和 cos θ 的符号分别是什么？它们的平方和是多少？', answer: '点 A 在第二象限，所以 sin θ > 0、cos θ < 0；半径 OA = 1，因此 sin²θ + cos²θ = 1。' },
    checkpoints: ['半径线、圆周点、投影同步', '坐标为 (cos θ, sin θ)', '平方和恒等于 1'], offlineFileName: 'H05_三角函数与恒等变换.html',
    render(state) {
      const angle = clamp(state.angle, 0, 360); const rad = angle * Math.PI / 180; const cos = Math.cos(rad); const sin = Math.sin(rad)
      const cx = 320; const cy = 180; const r = 120
      return { angle, cos, sin, cx, cy, r, px: cx + cos * r, py: cy - sin * r }
    },
  },
  area: {
    key: 'area', stage: 'primary', chapter: '长度、周长与面积', knowledgePoint: '长方形与正方形的面积',
    objective: '通过改变长和宽，理解面积是长×宽，周长是四条边的总和。', misconception: '把周长和面积混为同一个量。',
    steps: ['观察方格中的长方形', '拖动顶点改变长和宽', '比较周长与面积变化', '用公式解释规律'],
    interactionState: { width: 8, height: 5, guessed: false, revealed: false },
    exercise: { question: '长为 6、宽为 4 的长方形，面积和周长各是多少？', answer: '面积 6×4=24，周长 2×(6+4)=20。面积用平方单位，周长用长度单位。' },
    checkpoints: ['拖动顶点时长宽同步变化', '方格数与面积公式一致', '能区分面积和周长'], offlineFileName: 'P09_长度、周长与面积.html',
    render(state) {
      const width = clamp(state.width, 2, 12); const height = clamp(state.height, 2, 10)
      return { width, height, area: width * height, perimeter: 2 * (width + height), ratio: width / height }
    },
  },
  transformations: {
    key: 'transformations', stage: 'primary', chapter: '位置、方向与图形运动', knowledgePoint: '平移', knowledgePoints: ['用数对确定位置', '平移', '旋转', '轴对称与对称轴'],
    objective: '在同一坐标网格中读数对，并观察平移、轴对称和旋转如何改变点的位置。', misconception: '图形运动会改变边长；旋转只改变图形大小；数对的两个数可以随意交换。',
    steps: ['观察坐标网格与三角形顶点', '用参数控制平移或切换变换', '比较变换前后的坐标与图形', '用坐标规则解释规律'],
    interactionState: { transform: 'translation', dx: 2, dy: 1, angle: 90, axis: 'y', guessed: false, revealed: false },
    exercise: { question: '点 A(2,1) 关于 y 轴对称后的坐标是什么？将点 (1,2) 绕原点逆时针旋转 90° 后呢？', answer: '关于 y 轴：(-2,1)；绕原点逆时针 90°：(−2,1)。' },
    checkpoints: ['变换前后边长和形状不变', '平移坐标加上同一位移量', '对称/旋转坐标规则与图形一致'], offlineFileName: 'P11_位置、方向与图形运动.html',
    render(state) {
      const transform = ['translation', 'reflectX', 'reflectY', 'rotate'].includes(state.transform) ? state.transform : 'translation'
      const dx = Math.max(-4, Math.min(4, Number(state.dx) || 0)); const dy = Math.max(-4, Math.min(4, Number(state.dy) || 0))
      const angle = [90, 180, 270].includes(Number(state.angle)) ? Number(state.angle) : 90
      const axis = state.axis === 'x' ? 'x' : 'y'
      return { transform, dx, dy, angle, axis }
    },
  },
  quadratic: {
    key: 'quadratic', stage: 'junior', chapter: '二次函数', knowledgePoint: '二次函数的顶点与对称轴',
    objective: '观察 y=a(x−h)²+k 中 a、h、k 对开口、顶点和对称轴的影响。', misconception: '改变 h 或 k 只改变公式文字，图像不变。',
    steps: ['观察坐标系中的抛物线', '用参数条改变开口和顶点', '拖动顶点观察平移', '用顶点式解释规律'],
    interactionState: { a: 1, h: 0, k: 0, guessed: false, revealed: false },
    exercise: { question: 'y=−(x−2)²+3 的顶点和对称轴是什么？开口方向怎样？', answer: '顶点为 (2,3)，对称轴 x=2；a=−1<0，所以开口向下。' },
    checkpoints: ['抛物线随 a、h、k 实时变化', '顶点始终为 (h,k)', '正负 a 的开口方向正确'], offlineFileName: 'J10_二次函数.html',
    render(state) {
      let a = clamp(state.a, -2, 2); if (Math.abs(a) < 0.1) a = a < 0 ? -0.1 : 0.1
      const h = clamp(state.h, -4, 4); const k = clamp(state.k, -4, 4)
      return { a, h, k, vertex: { x: h, y: k }, axis: h, yIntercept: a * h * h + k }
    },
  },
  vectors: {
    key: 'vectors', stage: 'senior', chapter: '平面向量及其应用', knowledgePoint: '平面向量的坐标表示与投影',
    objective: '拖动向量终点，观察坐标分量、长度和投影如何由同一向量决定。', misconception: '向量的方向和坐标分量互不相关。',
    steps: ['观察原点 O 与向量 OA', '拖动点 A 改变方向和长度', '读取 x、y 分量与投影', '用坐标公式解释长度'],
    interactionState: { x: 4, y: 3, guessed: false, revealed: false },
    exercise: { question: '向量 a=(3,−4) 的长度是多少？它的 x、y 投影分别是什么？', answer: '长度 √(3²+(-4)²)=5；x 投影为 3，y 投影为 −4。' },
    checkpoints: ['拖动终点时箭头和坐标同步', '投影方向与正负号一致', '长度公式与图形一致'], offlineFileName: 'H06_平面向量及其应用.html',
    render(state) {
      const x = clamp(state.x, -5, 5); const y = clamp(state.y, -5, 5); const length = Math.hypot(x, y)
      return { x, y, length, angle: Math.atan2(y, x) * 180 / Math.PI, quadrant: x === 0 || y === 0 ? '坐标轴上' : x > 0 ? (y > 0 ? '第一象限' : '第四象限') : (y > 0 ? '第二象限' : '第三象限') }
    },
  },
  solidGeometry: {
    key: 'solidGeometry', stage: 'primary', chapter: '立体图形', knowledgePoint: '正方体、圆柱体的面棱顶点',
    objective: '从不同视角识别正方体的面、棱、顶点和圆柱体的曲面。', misconception: '把圆柱体的曲面当成平面。',
    steps: ['观察正方体与圆柱体', '拖动图形旋转视角', '切换图形核对结构', '解释面、棱、顶点'],
    interactionState: { shape: 'cube', yaw: -28, pitch: 18, locked: false, guessed: false, revealed: false },
    exercise: { question: '正方体有几个面、几条棱、几个顶点？圆柱体的侧面是什么形状？', answer: '正方体有 6 个面、12 条棱、8 个顶点；圆柱体的侧面是曲面。' },
    checkpoints: ['视角拖动后图形真实变化', '正方体面棱顶点对应', '圆柱体曲面可识别'], offlineFileName: 'P10_立体图形与体积.html',
    render(state) { return { shape: state.shape === 'cylinder' ? 'cylinder' : 'cube', yaw: Number(state.yaw) || -28, pitch: Number(state.pitch) || 18 } },
  },
  spaceRelations: {
    key: 'spaceRelations', stage: 'senior', chapter: '空间几何关系', knowledgePoint: '线线、线面、面面关系',
    objective: '通过旋转视角观察空间中的平行、垂直与相交关系。', misconception: '图上看起来不相交就认为空间中平行。',
    steps: ['观察空间图形', '拖动视角寻找关系', '切换关系类型', '用定义解释'],
    interactionState: { relation: 'line-line', yaw: -28, pitch: 18, locked: false, guessed: false, revealed: false },
    exercise: { question: '长方体中，一条棱与相邻棱是什么关系？与对面平行的面是什么关系？', answer: '相邻棱相交且垂直；相对的面互相平行。' },
    checkpoints: ['三维视角可旋转', '线线/线面/面面图形切换', '关系说明与图形一致'], offlineFileName: '空间几何关系.html',
    render(state) { return { relation: state.relation || 'line-line', yaw: Number(state.yaw) || -28, pitch: Number(state.pitch) || 18 } },
  },
  views3d: {
    key: 'views3d', stage: 'primary', chapter: '简单几何图形与测量', knowledgePoint: '从不同方向观察物体',
    objective: '用同一个长方体的长、宽、高解释正视图、俯视图和左视图。', misconception: '三个视图是三个互不相关的图形。',
    steps: ['观察长方体的长、宽、高', '切换正视图、俯视图和左视图', '长按模型旋转观察', '用两条棱解释视图面积'],
    interactionState: { view: 'front', length: 3, width: 1, height: 2, guessed: false, revealed: false },
    exercise: { question: '长方体长 3、宽 1、高 2，正视图、俯视图、左视图面积分别是多少？', answer: '正视图 3×2=6，俯视图 3×1=3，左视图 1×2=2（平方单位）。' },
    checkpoints: ['三个视图与同一长宽高对应', '视角切换和模型旋转同步', '面积单位与边长乘积一致'], offlineFileName: 'P10_立体图形与体积.html',
    render(state) { const length = clamp(state.length, 1, 10); const width = clamp(state.width, 1, 10); const height = clamp(state.height, 1, 10); return { length, width, height, frontArea: length * height, topArea: length * width, leftArea: width * height, view: ['front', 'top', 'left'].includes(state.view) ? state.view : 'front' } },
  },
  statistics: {
    key: 'statistics', stage: 'primary', chapter: '统计与数据分析', knowledgePoint: '条形图与平均数',
    objective: '用条形图比较数据，并理解平均数等于总和除以数据个数。', misconception: '平均数一定等于某一项原始数据。',
    steps: ['观察条形图', '调整一项数据', '读取平均数', '解释总和变化'], interactionState: { a: 3, b: 5, c: 4, d: 6 },
    exercise: { question: '数据 3、5、4、6 的平均数是多少？', answer: '4.5。' }, checkpoints: ['柱高与数据同步', '公式与状态同步'], offlineFileName: 'P12_统计与数据分析.html',
    render(state) { const values = ['a','b','c','d'].map(k => clamp(state[k], 0, 10)); const sum = values.reduce((a,b) => a+b, 0); return { values, sum, mean: sum / 4, max: Math.max(...values) } },
  },
  coordinateTransforms: {
    key: 'coordinateTransforms', stage: 'junior', chapter: '图形变换与坐标', knowledgePoint: '平移、旋转与轴对称',
    objective: '在坐标网格中观察平移、旋转和轴对称的对应点变化。', misconception: '变换会改变图形的边长或形状。',
    steps: ['观察原图形', '控制变换', '比较坐标', '解释规则'], interactionState: { transform: 'translation', dx: 2, dy: 1 },
    exercise: { question: '点 A(2,1) 关于 y 轴对称后的坐标是什么？', answer: '(-2,1)。' }, checkpoints: ['形状不变', '坐标同步'], offlineFileName: 'J15_图形变换与相似.html',
    render(state) { return { transform: ['translation','reflectX','reflectY','rotate'].includes(state.transform) ? state.transform : 'translation', dx: Math.max(-4, Math.min(4, Number(state.dx) || 0)), dy: Math.max(-4, Math.min(4, Number(state.dy) || 0)) } },
  },
  derivative: {
    key: 'derivative', stage: 'senior', chapter: '导数与函数变化率', knowledgePoint: '导数作为瞬时变化率',
    objective: '通过割线斜率逼近切线斜率，理解导数是瞬时变化率。', misconception: '割线斜率与切线斜率始终相等。',
    steps: ['选定 x₀', '改变 h', '观察逼近', '解释导数'], interactionState: { x0: 1, h: 1 },
    exercise: { question: 'f(x)=x² 在 x₀=2 处的导数是多少？', answer: '4。' }, checkpoints: ['差商正确', 'h 越小越逼近'], offlineFileName: 'H16_导数及其应用.html',
    render(state) { const x0 = clamp(state.x0, -3, 3); const h = clamp(state.h, 0.1, 2); const y0 = x0*x0; const y1 = (x0+h)*(x0+h); const secantSlope = (y1-y0)/h; const derivative = 2*x0; return { x0, h, y0, y1, secantSlope, derivative, delta: Math.abs(secantSlope-derivative) } },
  },
}

export function modelForCard(card) {
  const title = `${card?.chapter || ''}${card?.title || ''}${card?.knowledgePoint || ''}`
  if (/分数/.test(title)) return COURSEWARE_MODELS.fractions
  if (/函数基础|一次函数/.test(title)) return COURSEWARE_MODELS.linear
  if (/三角函数|单位圆/.test(title)) return COURSEWARE_MODELS.unitCircle
  if (/长度、周长与面积|长方形与正方形的面积/.test(title)) return COURSEWARE_MODELS.area
  if (/统计与数据分析|条形图|平均数/.test(title)) return COURSEWARE_MODELS.statistics
  if (/图形变换与坐标/.test(title)) return COURSEWARE_MODELS.coordinateTransforms
  if (/位置、方向|图形运动|数对定位|轴对称|旋转/.test(title)) return COURSEWARE_MODELS.transformations
  if (/二次函数/.test(title)) return COURSEWARE_MODELS.quadratic
  if (/平面向量/.test(title)) return COURSEWARE_MODELS.vectors
  if (/导数与函数变化率|瞬时变化率|切线斜率/.test(title)) return COURSEWARE_MODELS.derivative
  if (/立体图形|正方体|圆柱体/.test(title)) return COURSEWARE_MODELS.solidGeometry
  if (/三视图|从不同方向|简单几何图形与测量/.test(title)) return COURSEWARE_MODELS.views3d
  if (/空间几何|线线|线面|面面/.test(title)) return COURSEWARE_MODELS.spaceRelations
  return COURSEWARE_MODELS.fractions
}
